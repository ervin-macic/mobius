#!/usr/bin/env python3
"""Ask the self-hosted replacement helper for a container-only upgrade.

An installation whose running image predates the image-owned boot transaction
(``backend/runtime/boot-protocol``) cannot take a release that changes Python
packages: its served code refuses to install source for packages its image
lacks. This moves only the container to a newer official image and leaves the
served source untouched. The new image's boot transaction then checks that
unchanged source against itself (an image newer than the source's release is
allowed) and probes it, with the image's own release as the floor. After that,
Settings' ordinary update installs the release, because its packages are
already in the image.

Run it inside the Möbius container as the ``mobius`` user, from the fetched
upstream so an older installation needs nothing new installed::

    git -C /data/platform fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
    # (add --unshallow when the checkout is still the image's shallow seed)
    git -C /data/platform show origin/main:scripts/request-container-upgrade.py \
      | python3 - [--check] [--target <40-hex official release>]

It changes nothing but the helper's inbox, and only after these checks pass:
the target is a fetched official release that ships this bridge, descends
from the running image, and is not older than any official Python package
declaration of the installed source; no update is prepared or parked; and the
helper is installed and idle. They hold the updater's lock while checking and
requesting. The helper must also have certified the target's
``deployment/self-hosted-helper.required`` protocol, so an outdated helper is
asked to be reinstalled instead of running its old cutover. The helper then drains chats, replaces the
container, verifies it, and restores the previous container if it is not
healthy. See scripts/CONTAINER-REBUILD.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PLATFORM = DATA / "platform"
CONTROL = DATA / "mobius-rebuild"
BUILD_INFO = Path(os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"))
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
PYTHON_INPUTS = ("backend/requirements.txt", "backend/requirements.lock")
BRIDGE_FILES = ("backend/runtime/boot-protocol", "scripts/request-container-upgrade.py")
RECONCILE_LOCK = DATA / ".platform-reconcile.lock"
# Served code from this release on discounts package inputs the running image
# already carries (#1311), so after the upgrade it accepts Settings' update.
SERVED_CODE_FLOOR = "531c08dc98f10dbd4ca0a11b6430c97b0a5e3056"
SHA_RE = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """A precondition does not hold; nothing was requested."""


def git(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(PLATFORM), *args],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )


def resolve(ref: str) -> str:
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def resolve_blob(commit: str, path: str) -> str:
    out = git("rev-parse", "--verify", "--quiet", f"{commit}:{path}")
    return out.stdout.strip() if out.returncode == 0 else ""


def descends(ancestor: str, descendant: str) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def running_image() -> str:
    try:
        sha = str(json.loads(BUILD_INFO.read_text(encoding="utf-8")).get("sha") or "")
    except (OSError, ValueError):
        sha = ""
    if not SHA_RE.fullmatch(sha):
        raise Refused("this container does not record its own release")
    return sha


def declared_in_history(commit: str, path: str, data: bytes) -> bool:
    """Whether ``path`` held exactly ``data`` at some commit ``commit`` contains.

    Mirrors the boot transaction's own judgment
    (``platform_update._declared_in_history``), which the target image applies
    before it serves the unchanged source.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    blob = subprocess.run(
        ["git", "-C", str(PLATFORM), "hash-object", "--stdin"],
        input=data, capture_output=True, check=True, env=env, timeout=120,
    ).stdout.decode().strip()
    history = subprocess.run(
        ["git", "-C", str(PLATFORM), "log", "-m", "--format=", "--raw", "--no-abbrev",
         "--no-renames", commit, "--", path],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )
    if history.returncode != 0:
        return False
    for line in history.stdout.splitlines():
        fields = line.split("\t", 1)[0].split()
        if len(fields) >= 4 and blob in fields[2:4]:
            return True
    return False


def check_target(target: str) -> str:
    image = running_image()
    if target == image:
        raise Refused("this container already runs that release")
    official = resolve("refs/remotes/origin/main")
    if not official:
        raise Refused("fetch origin main first; no official history is available")
    # The checks below reason from what history does not contain, which only
    # a complete clone can show; image seeds are shallow.
    if git("rev-parse", "--is-shallow-repository").stdout.strip() != "false":
        raise Refused(
            "the platform checkout has shallow history; fetch it completely "
            "first (git fetch --unshallow origin)"
        )
    if not descends(target, official):
        raise Refused("the target is not part of the fetched official history")
    if not descends(image, target):
        raise Refused("the target is not newer than the running container")
    # The source's own updater finishes the crossing, so it must be new enough
    # to count the upgraded image's packages as installed.
    if not descends(SERVED_CODE_FLOOR, "HEAD"):
        raise Refused(
            "the installed platform code predates 23 September 2026 (#1311) and "
            "could not install the release afterwards; update it first"
        )
    # Settings then targets the current official release, so the new image
    # must carry exactly that release's packages.
    for path in PYTHON_INPUTS:
        if resolve_blob(target, path) != resolve_blob(official, path):
            raise Refused(
                "the target's Python packages differ from the latest official "
                "release's; use the latest release"
            )
    # Only a release that ships this bridge also judges the unchanged source
    # the way these checks do; an older target could not finish the crossing.
    for required in BRIDGE_FILES:
        if git("cat-file", "-e", f"{target}:{required}").returncode != 0:
            raise Refused("the target release predates the container-only upgrade")
    # The target image's boot serves the unchanged source only if none of its
    # package inputs comes from an official release newer than the target:
    # an input declared in the target's history is older or equal, and one
    # never declared officially is a local declaration.
    for path in PYTHON_INPUTS:
        try:
            served = (PLATFORM / path).read_bytes()
        except OSError:
            continue
        if declared_in_history(target, path, served):
            continue
        if declared_in_history(official, path, served):
            raise Refused(
                "the installed source declares newer Python packages than the "
                "target; use the latest official release"
            )
    return image


def check_no_pending_update() -> None:
    for name in (".platform-prepared-update.json", ".platform-conflict"):
        if (DATA / name).exists():
            raise Refused(
                "an update is prepared or waiting for its resolver; finish or "
                "cancel it in Settings first"
            )


def required_helper_protocol(target: str) -> int:
    """The host helper protocol the target release requires.

    Every release that ships this bridge also ships the requirement, so 0 (no
    requirement) is only a defensive reading. Any requirement, even one older
    than certification, refuses an uncertified helper: the old helper's
    cutover path is the hazard whatever the target.
    """
    shown = git("show", f"{target}:deployment/self-hosted-helper.required")
    if shown.returncode != 0:
        return 0
    try:
        return int(shown.stdout.strip())
    except ValueError:
        raise Refused("the target release's host helper requirement is unreadable") from None


def check_helper_idle(target: str) -> Path:
    inbox = CONTROL / "inbox"
    try:
        status = json.loads((CONTROL / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(
            "the host replacement helper is not installed; run "
            "scripts/install-rebuild-helper.sh on the host"
        ) from None
    if not inbox.is_dir() or not os.access(inbox, os.W_OK | os.X_OK):
        raise Refused("the helper inbox is not writable from this container")
    if str(status.get("state") or "idle") in ACTIVE_STATES:
        raise Refused("a container replacement is already running")
    required = required_helper_protocol(target)
    installed = status.get("helper_protocol")
    if required and not (isinstance(installed, int) and not isinstance(installed, bool)
                         and installed >= required):
        raise Refused(
            "the host replacement helper is older than this release requires; "
            "update the Möbius checkout on the host and rerun "
            "sudo scripts/install-rebuild-helper.sh there first"
        )
    if (inbox / "request.json").exists():
        raise Refused("a container replacement request is already queued")
    return inbox


def write_request(inbox: Path, target: str) -> None:
    """Publish one request without overwriting a request queued meanwhile.

    Version 1 carries only the release: every installed helper accepts it, and
    no prepared update exists for an operation to confirm.
    """
    temp = inbox / f".request-{secrets.token_hex(12)}.tmp"
    payload = json.dumps({"version": 1, "expected_sha": target}, separators=(",", ":"))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, inbox / "request.json")
        except FileExistsError:
            raise Refused("a container replacement request is already queued") from None
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", help="official release to move the container to (default: origin/main)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run every check and report, but request nothing",
    )
    args = parser.parse_args(argv)
    try:
        target = resolve(args.target) if args.target else resolve("refs/remotes/origin/main")
        if not target or (args.target and not SHA_RE.fullmatch(args.target)):
            raise Refused("name a fetched, complete 40-character official release")
        # The updater prepares updates under this lock; holding it while
        # checking and requesting keeps Settings from preparing one in between.
        # One prepared afterwards is settled by the new image's own boot.
        with open(RECONCILE_LOCK, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            image = check_target(target)
            check_no_pending_update()
            inbox = check_helper_idle(target)
            if args.check:
                print(
                    f"Ready: a container-only upgrade from {image[:12]} to "
                    f"{target[:12]} would be requested."
                )
                return 0
            write_request(inbox, target)
    except Refused as exc:
        print(f"Container upgrade not requested: {exc}.", file=sys.stderr)
        return 1
    print(
        f"Requested a container-only upgrade from {image[:12]} to {target[:12]}. "
        "Möbius restarts once the helper has pulled and checked the image; "
        "afterwards, install the update from Settings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
