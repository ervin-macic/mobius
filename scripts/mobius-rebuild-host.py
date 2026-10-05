#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container rebuild."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/var/lib/mobius-rebuild")
CONFIG = Path("/etc/mobius-rebuild/config.json")
COMPOSE = Path("/etc/mobius-rebuild/compose.yml")
OVERRIDE = Path("/etc/mobius-rebuild/image.override.yml")
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "replace.lock"
IMAGES = STATE_DIR / "images.json"
# A replacement in progress, written before the running app is drained and
# removed only once its outcome is settled; reconcile() finishes or reverses
# whatever an interrupted worker left. Its schema is shared by every worker
# revision: keep it backward compatible.
TRANSACTION = STATE_DIR / "transaction.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{12,64}$")
IMAGE = "ghcr.io/mobius-os/mobius"
IMAGE_SOURCE = "https://github.com/mobius-os/mobius"
ROLLBACK_TAG = f"{IMAGE}:mobius-rebuild-last-good"
# Helper-owned local tag pointed at the verified image ID right before Compose
# starts it, so a concurrent pull of the release tag cannot change what runs.
TARGET_TAG = f"{IMAGE}:mobius-rebuild-target"
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
HANDOFF_VERSION = "external-cutover-v1"
# Version 2 requests carry the app's nonce, echoed as ``request_nonce`` so the
# app can tell its exact replacement's outcome from any earlier one.
REQUEST_VERSIONS = [1, 2]
# The frozen launcher (scripts/mobius-rebuild-launcher.py) adopts a worker from
# each requested official image only when this number is higher than every
# worker it has run. Increase it with every change to this file; never lower
# it. The launcher reads it as text, so keep it a plain literal on one line.
WORKER_REVISION = 2
# The frozen launcher runs the worker selected here; see offer_worker().
WORKERS = STATE_DIR / "workers"
WORKER_INDEX = STATE_DIR / "workers.json"
WORKER_IN_IMAGE = "/app/platform-baked/scripts/mobius-rebuild-host.py"
MAX_WORKER_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 4096
_REVISION_LINE = re.compile(rb"^WORKER_REVISION\s*=.*$", re.MULTILINE)
_REVISION_EXACT = re.compile(rb"^WORKER_REVISION = ([1-9][0-9]{0,5})$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def acquire_lock(lock, timeout: float = 2.0) -> None:
    """Acquire the worker lock, allowing a boot reconciler to finish first."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_config(value: dict, *, trusted_uid: int = 0) -> dict:
    if set(value) != {"version", "project", "data_dir"} or value["version"] != 3:
        raise ValueError("invalid fixed deployment configuration")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", str(value["project"])):
        raise ValueError("invalid Compose project")
    data_dir = Path(str(value["data_dir"]))
    control = data_dir / "mobius-rebuild"
    for path in (CONFIG.parent, CONFIG, COMPOSE, OVERRIDE, control):
        if path.is_symlink():
            raise ValueError("replacement configuration may not use symlinks")
        stat = path.stat()
        if stat.st_uid != trusted_uid or stat.st_mode & 0o022:
            raise ValueError("replacement configuration is not root-controlled")
    if not data_dir.is_absolute() or not data_dir.is_dir():
        raise ValueError("invalid persistent data directory")
    return {**value, "data_dir": data_dir, "control_dir": control}


def config() -> dict:
    return validate_config(read_json(CONFIG))












































def write_status(config_value: dict, **fields) -> dict:
    current = {
        "supported": True, "operation_id": None, "state": "idle",
        "expected_sha": None, "code": None, "message": None,
        "handoff": HANDOFF_VERSION,
    }
    try:
        current.update(read_json(STATUS))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    current.update(fields)
    current["handoff"] = HANDOFF_VERSION
    current["request_versions"] = REQUEST_VERSIONS
    current["worker_revision"] = WORKER_REVISION
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER", "")
    current["launcher_revision"] = int(launcher) if launcher.isdigit() else None
    current.pop("runtime_overlay", None)
    current["updated_at"] = now()
    _atomic_json(STATUS, current)
    _atomic_json(config_value["control_dir"] / "status.json", current, 0o644)
    return current


def compose(config_value: dict, *args: str, image: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or IMAGE
    return subprocess.run(
        ["docker", "compose", "-p", config_value["project"],
         "-f", str(COMPOSE), "-f", str(OVERRIDE), *args],
        cwd=CONFIG.parent, env=env, text=True, capture_output=True, check=check,
    )


def inspect_image(image: str, template: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", template, image],
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def app_container(config_value: dict) -> tuple[str, str]:
    result = compose(config_value, "ps", "-q", "app")
    cid = result.stdout.strip()
    if not cid:
        raise RuntimeError("the recorded Möbius app container is not running")
    inspected = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{.Image}}", cid],
        text=True, capture_output=True, check=True,
    )
    return cid, inspected.stdout.strip()




def wait_healthy(config_value: dict, timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = compose(config_value, "ps", "-q", "app", check=False)
        cid = result.stdout.strip()
        if cid:
            probe = subprocess.run(
                ["docker", "container", "inspect", "--format",
                 "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cid],
                text=True, capture_output=True,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "healthy":
                return True
        time.sleep(3)
    return False


def verify_served_generation(cid: str, expected_sha: str) -> None:
    """Prove the running container serves the requested immutable image."""
    result = subprocess.run(
        ["docker", "exec", cid, "curl", "-fsS",
         "http://127.0.0.1:8000/api/version"],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("the container did not expose build provenance")
    try:
        version = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("the container returned invalid build provenance") from exc
    if not isinstance(version, dict) or version.get("sha") != expected_sha:
        raise RuntimeError("the container is not serving the requested image revision")
    mounts = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{json .Mounts}}", cid],
        text=True, capture_output=True, check=True,
    )
    try:
        mounted = json.loads(mounts.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("the container returned invalid mount provenance") from exc
    if any(item.get("Destination") == "/app/runtime" for item in mounted):
        raise RuntimeError("the container is not using the image's protected runtime")


def _docker_root() -> Path:
    result = subprocess.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        text=True, capture_output=True, check=True,
    )
    return Path(result.stdout.strip())


def require_pull_space(current_image: str) -> None:
    current_size = int(inspect_image(current_image, "{{.Size}}"))
    required = max(512 * 1024 * 1024, current_size)
    free = shutil.disk_usage(_docker_root()).free
    if free < required:
        raise RuntimeError(
            f"not enough Docker storage to pull safely (need {required // 1048576} MiB free)"
        )


def restart_ledger(config_value: dict, cid: str, command: str,
                   operation: str, *, image: str | None = None) -> bool:
    """Run one root ledger command, even when the app is crash-looping."""
    invocation = ["python3", "-P", "/app/runtime/restart_ledger.py",
                  command, operation]
    result = subprocess.run(
        ["docker", "exec", cid, *invocation],
        text=True, capture_output=True,
    )
    if result.returncode == 0:
        return True
    if not image:
        return False
    # A failed replacement may not stay alive long enough for docker exec.
    # The prior verified image carries the same frozen helper; mount only the
    # persistent data root and run no entrypoint or application code.
    result = subprocess.run(
        ["docker", "run", "--rm", "--mount",
         f"type=bind,src={config_value['data_dir']},dst=/data",
         "--entrypoint", "python3", image, *invocation[1:]],
        text=True, capture_output=True,
    )
    return result.returncode == 0


def request_drain(config_value: dict, operation: str, cid: str) -> None:
    if not restart_ledger(config_value, cid, "open-cutover", operation):
        raise RuntimeError("the running image does not support safe Host cutover")
    result = subprocess.run(
        ["docker", "exec", cid, "python3",
         "/data/platform/backend/scripts/prepare-container-cutover.py", operation],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("the running server could not complete a safe chat drain")
    if not restart_ledger(config_value, cid, "accept-cutover", operation):
        raise RuntimeError("the root supervisor did not accept the chat handoff")


def _image_state() -> dict:
    try:
        value = read_json(IMAGES)
        refs = value.get("sha_refs", [])
        rollback_id = value.get("rollback_image_id")
        return {
            "sha_refs": [
                ref for ref in refs
                if isinstance(ref, str) and ref.startswith(f"{IMAGE}:sha-")
            ],
            "rollback_image_id": (
                rollback_id
                if isinstance(rollback_id, str) and rollback_id.startswith("sha256:")
                else None
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {"sha_refs": [], "rollback_image_id": None}


def record_pulled_image(target_ref: str) -> None:
    state = _image_state()
    refs = sorted({*state["sha_refs"], target_ref})
    _atomic_json(IMAGES, {**state, "sha_refs": refs})


def discard_pulled_image(target_ref: str) -> None:
    state = _image_state()
    subprocess.run(["docker", "image", "rm", target_ref], capture_output=True, text=True)
    _atomic_json(IMAGES, {
        **state,
        "sha_refs": [ref for ref in state["sha_refs"] if ref != target_ref],
    })


def retain_images(target_ref: str, rollback_image_id: str | None = None) -> None:
    state = _image_state()
    for ref in state["sha_refs"]:
        if ref != target_ref:
            subprocess.run(["docker", "image", "rm", ref], capture_output=True, text=True)
    old_rollback = state["rollback_image_id"]
    if old_rollback and old_rollback != rollback_image_id:
        # Non-force removal fails harmlessly if another tag or container still
        # owns the image; the helper never removes unrelated references.
        subprocess.run(
            ["docker", "image", "rm", old_rollback], capture_output=True, text=True,
        )
    _atomic_json(IMAGES, {
        "sha_refs": [target_ref],
        "rollback_tag": ROLLBACK_TAG,
        "rollback_image_id": rollback_image_id,
    })


def rollback(config_value: dict, operation: str, expected: str,
             code: str, detail: str, previous_image: str | None = None) -> int:
    """Restore the previous container; a settled outcome retires the
    transaction, while needs_recovery keeps it for the next reconcile.

    With ``previous_image``, the restore counts only when the healthy
    container runs exactly that image ID."""
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=code,
                 message="Replacement failed; restoring the previous container.")
    try:
        cid, _current = app_container(config_value)
    except Exception:
        cid = ""
    handoff_rearmed = restart_ledger(
        config_value, cid, "rearm-cutover", operation, image=ROLLBACK_TAG,
    )
    compose(config_value, "up", "-d", "--no-build", "--no-deps",
            "--force-recreate", "app", image=ROLLBACK_TAG)
    if wait_healthy(config_value, 120):
        cid, current = app_container(config_value)
        if previous_image and current != previous_image:
            write_status(
                config_value, operation_id=operation, state="needs_recovery",
                expected_sha=expected, code="rollback_wrong_image",
                message=("The restored container is not the recorded previous "
                         f"image. Original failure: {detail}")[:300],
            )
            return 1
        handoff_finalized = restart_ledger(
            config_value, cid, "finalize-cutover", operation,
            image=ROLLBACK_TAG,
        )
        if handoff_finalized:
            status_code = code
            message = f"The previous container was restored: {detail}"
        elif not handoff_rearmed:
            status_code = "handoff_rearm_failed"
            message = (
                "The previous container was restored, but exact active-chat "
                "continuation could not be re-armed; affected chats may need "
                f"manual Resume. Original failure: {detail}"
            )
        else:
            status_code = "handoff_finalize_failed"
            message = (
                "The previous container was restored, but the Host could not "
                "verify and retire the exact chat handoff receipt. Check the "
                f"affected chats. Original failure: {detail}"
            )
        clear_transaction()
        write_status(config_value, operation_id=operation, state="rolled_back",
                     expected_sha=expected, code=status_code,
                     message=message[:300])
        return 1
    write_status(config_value, operation_id=operation, state="needs_recovery",
                 expected_sha=expected, code="rollback_failed",
                 message=f"Replacement and rollback failed: {detail}"[:300])
    return 1


def worker_revision(source: bytes) -> int | None:
    """A worker's declared revision, read as text and never by running it:
    0 for a worker from before revisions, None when the declaration is
    malformed or repeated."""
    lines = _REVISION_LINE.findall(source)
    if not lines:
        return 0
    match = _REVISION_EXACT.fullmatch(lines[0].rstrip(b"\r"))
    return int(match[1]) if len(lines) == 1 and match else None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_write(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _worker_entry(source: bytes, revision: int, origin: str) -> dict:
    """Store ``source`` durably and describe it for the selection record."""
    WORKERS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(WORKERS, 0o700)
    digest = hashlib.sha256(source).hexdigest()
    name = f"{revision}-{digest[:16]}.py"
    _durable_write(WORKERS / name, source, 0o700)
    return {"revision": revision, "file": name, "sha256": digest,
            "origin": origin[:80]}


def _publish_index(index: dict) -> None:
    """Publish the selection record. Worker files are small and never
    deleted: a running candidate and the record may still name any of them."""
    _durable_write(
        WORKER_INDEX, json.dumps(index, separators=(",", ":")).encode(), 0o600,
    )


def _checked(source: bytes) -> tuple[int | None, str | None]:
    """The revision of a worker that may be adopted, or why it may not."""
    revision = worker_revision(source)
    if revision is None:
        return None, "rejected: malformed WORKER_REVISION"
    if revision == 0:
        return None, "not adopted: the image predates self-updating workers"
    if len(source) > MAX_WORKER_BYTES:
        return None, "rejected: worker too large"
    try:
        compile(source, "mobius-rebuild-host.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        return None, f"rejected: worker does not compile ({exc.__class__.__name__})"
    return revision, None


def offer_worker(source: bytes, origin: str) -> str:
    """Offer a verified image's worker to the launcher as its candidate.

    Only a revision above every revision offered or installed so far is
    offered, so a worker the launcher tried and dropped is never offered
    again, and no official image can bring back an older one. Returns a short
    outcome for status."""
    if not os.environ.get("MOBIUS_REBUILD_LAUNCHER"):
        return "not adopted: this helper predates the launcher"
    revision, refusal = _checked(source)
    if refusal:
        return refusal
    try:
        index = json.loads(WORKER_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "not adopted: the launcher's worker record is unreadable"
    high_water = int(index.get("high_water", 0))
    if revision <= high_water:
        return f"not adopted: revision {revision} is not newer than {high_water}"
    entry = _worker_entry(source, revision, origin)
    _publish_index({**index, "high_water": revision, "candidate": entry})
    return f"offered: revision {revision} runs the next replacement"


def seed_worker(source: bytes) -> str:
    """Install this trusted checkout's worker as the active one, unless a
    newer worker was already adopted (reinstalling never downgrades)."""
    revision, refusal = _checked(source)
    if refusal:
        return refusal
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        index = json.loads(WORKER_INDEX.read_text(encoding="utf-8"))
    except FileNotFoundError:
        index = {"version": 1, "high_water": 0, "active": None, "candidate": None}
    digest = hashlib.sha256(source).hexdigest()
    if (index.get("active") or {}).get("sha256") == digest:
        return f"current: revision {revision}"
    if revision <= int(index.get("high_water", 0)) and index.get("active"):
        return f"kept: a newer worker (revision {index['high_water']}) is installed"
    entry = _worker_entry(source, revision, "checkout")
    _publish_index({**index, "high_water": revision, "active": entry,
                    "candidate": None})
    return f"installed: revision {revision}"


def _bounded_output(args: list[str], limit: int, timeout: float = 300) -> bytes:
    """Run ``args`` and collect at most ``limit`` bytes of its output within
    ``timeout`` seconds; the child is killed as soon as either is exceeded."""
    import select

    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    with subprocess.Popen(args, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            assert process.stdout is not None
            fd = process.stdout.fileno()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("reading the image's worker timed out")
                if not select.select([fd], [], [], remaining)[0]:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("the image's worker is too large")
                chunks.append(chunk)
            if process.wait(timeout=max(deadline - time.monotonic(), 0.1)) != 0:
                raise RuntimeError("the image's worker could not be read")
        except BaseException:
            process.kill()
            raise
    return b"".join(chunks)


def worker_from_image(image_id: str) -> bytes:
    """The worker inside the exact verified image, without starting it."""
    import io
    import tarfile

    created = subprocess.run(
        ["docker", "create", "--network", "none", "--entrypoint", "/bin/false",
         "--label", "mobius-rebuild.worker-extract=1", image_id],
        text=True, capture_output=True, check=True, timeout=300,
    )
    cid = created.stdout.strip()
    try:
        archive = _bounded_output(
            ["docker", "cp", f"{cid}:{WORKER_IN_IMAGE}", "-"],
            MAX_WORKER_BYTES + 64 * 1024,
        )
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True,
                       text=True, timeout=300)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            members = tar.getmembers()
            if (len(members) != 1 or members[0].type != tarfile.REGTYPE
                    or members[0].name != Path(WORKER_IN_IMAGE).name
                    or members[0].size > MAX_WORKER_BYTES):
                raise RuntimeError("the image's worker is not one regular file")
            handle = tar.extractfile(members[0])
            source = handle.read(MAX_WORKER_BYTES + 1) if handle else b""
    except tarfile.TarError as exc:
        raise RuntimeError("the image's worker archive is invalid") from exc
    if len(source) != members[0].size:
        raise RuntimeError("the image's worker archive is truncated")
    return source


def adopt_from_image(image_id: str) -> str:
    """After a verified replacement, adopt the worker that image carries."""
    try:
        return offer_worker(worker_from_image(image_id), image_id)
    except Exception as exc:  # adoption never fails a finished replacement
        return f"not adopted: {str(exc)[:160]}"


def parse_request(payload: dict) -> tuple[str, str | None]:
    """The requested target and, for a version 2 request, the app's nonce."""
    version = payload.get("version")
    keys = {"version", "expected_sha"} | ({"nonce"} if version == 2 else set())
    if version not in REQUEST_VERSIONS or set(payload) != keys:
        raise ValueError("invalid replacement request")
    expected = str(payload["expected_sha"])
    if not SHA_RE.fullmatch(expected):
        raise ValueError("invalid replacement target")
    nonce = str(payload["nonce"]) if version == 2 else None
    if nonce is not None and not OPERATION_RE.fullmatch(nonce):
        raise ValueError("invalid replacement nonce")
    return expected, nonce


def read_request(request: Path) -> tuple[dict | None, tuple[int, int, bytes] | None]:
    """The queued request's payload (None when unreadable) and the identity of
    the exact file read (device, inode, bytes), or ``(None, None)`` when
    nothing is queued."""
    try:
        info = os.lstat(request)
    except FileNotFoundError:
        return None, None
    raw = _read_regular(request, info)
    try:
        value = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError):
        value = None
    payload = value if isinstance(value, dict) else None
    return payload, (info.st_dev, info.st_ino, raw or b"")


def _read_regular(path: Path, info: os.stat_result) -> bytes | None:
    """A bounded read of exactly the regular file ``info`` describes; None for
    a link, pipe, oversized or swapped file (still claimed, then refused)."""
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    return raw if len(raw) <= MAX_REQUEST_BYTES else None


def claim_request(
    request: Path, claimed: Path, identity: tuple[int, int, bytes],
) -> bool:
    """Take exactly the request this worker read out of the inbox.

    The app reuses the inbox path and may withdraw a request and queue a newer
    one at any moment, so a rename alone can take the wrong file. The claimed
    file counts as this request only when both its inode and its bytes match
    what was read. Returns False when the request read was withdrawn; any
    other file taken is put back, or kept beside it, never deleted."""
    try:
        os.replace(request, claimed)
    except FileNotFoundError:
        return False
    try:
        info = os.lstat(claimed)
        verified = (info.st_dev, info.st_ino) == identity[:2] and (
            (_read_regular(claimed, info) or b"") == identity[2]
        )
    except OSError:
        verified = False
    if verified:
        return True
    return_unverified_request(request, claimed)
    return False


def _discard_claim(claimed: Path) -> None:
    """Remove a claimed request file, link or pipe. A claimed directory is
    left in the root-owned control directory rather than deleted blindly."""
    try:
        claimed.unlink(missing_ok=True)
    except (IsADirectoryError, PermissionError):
        pass


def return_unverified_request(request: Path, claimed: Path) -> None:
    """Put a file this worker did not read back in the inbox without
    overwriting a newer one; failing that, keep it under a durable name."""
    try:
        os.link(claimed, request)
    except OSError:
        os.replace(claimed, claimed.with_name(f".unreturned-{claimed.name}"))
    else:
        claimed.unlink()


def run() -> int:
    config_value = config()
    request = config_value["control_dir"] / "inbox" / "request.json"
    if not os.path.lexists(request):
        return 0
    operation = uuid.uuid4().hex
    # Claim inside the root-owned control directory. The inbox and its parent
    # are guaranteed to share a filesystem, unlike /data and STATE_DIR, so the
    # atomic rename also works when operators place Docker data on a separate
    # mount. Moving out of the app-writable inbox prevents later replacement.
    claimed = config_value["control_dir"] / f".request-{operation}.json"
    claim_verified = False
    expected = None
    previous = None
    image_ref = None
    pulled_recorded = False
    replacement_started = False
    lock = LOCK.open("a+")
    try:
        acquire_lock(lock)
    except BlockingIOError:
        lock.close()
        write_status(config_value, operation_id=operation, state="failed",
                     expected_sha=expected, code="already_running",
                     message="Another container rebuild is already running.")
        return 1
    # Everything below, including rollback and transaction settlement after
    # a failure, runs under the lock the installer and reconcile also take.
    with lock:
        try:
            if TRANSACTION.exists():
                # An interrupted replacement is settled first: this unit's
                # ExecStopPost reconcile recovers it, and the request stays
                # queued for the next run.
                return 0
            payload, identity = read_request(request)
            if identity is None:
                return 0  # withdrawn before this worker looked
            try:
                expected, nonce = parse_request(payload or {})
            except ValueError:
                expected = nonce = None
            if expected:
                # Publish this operation's nonce before claiming: while the
                # request is in the inbox the app sees it queued, and once it
                # is gone the status already names it, so the app never
                # mistakes a claimed request for one that ended.
                write_status(config_value, operation_id=operation, state="queued",
                             expected_sha=expected, request_nonce=nonce, code=None,
                             message="Container rebuild queued.")
            # Reconciliation uses this same lock when removing abandoned
            # claims. Claim only after ownership is established so a boot-time
            # reconcile can never mistake a live worker's request for debris.
            if not claim_request(request, claimed, identity):
                # Whatever the rename took is back in the inbox or kept aside.
                if expected:
                    write_status(config_value, operation_id=operation, state="failed",
                                 expected_sha=expected, request_nonce=nonce,
                                 code="withdrawn",
                                 message="The request was withdrawn before it started.")
                return 1
            claim_verified = True
            if not expected:
                raise ValueError("invalid replacement request")
            cid, previous = app_container(config_value)
            image_ref = f"{IMAGE}:sha-{expected}"
            require_pull_space(previous)
            write_status(config_value, operation_id=operation, state="preparing",
                         expected_sha=expected, code=None,
                         message="Downloading and checking the official image.")
            subprocess.run(["docker", "pull", image_ref], check=True,
                           text=True, capture_output=True)
            record_pulled_image(image_ref)
            pulled_recorded = True
            # Bind everything that follows to the exact image this pull
            # produced: labels, deployment and worker adoption.
            digest = inspect_image(image_ref, "{{.Id}}")
            revision = inspect_image(
                digest, '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            )
            source = inspect_image(
                digest, '{{index .Config.Labels "org.opencontainers.image.source"}}',
            )
            architecture = inspect_image(digest, "{{.Architecture}}")
            if revision != expected or source != IMAGE_SOURCE or architecture != "amd64":
                raise RuntimeError("the downloaded image is not the requested official amd64 release")
            if previous == digest:
                try:
                    verify_served_generation(cid, expected)
                except RuntimeError as exc:
                    write_status(
                        config_value, operation_id=operation, state="failed",
                        expected_sha=expected, code="provenance_failed",
                        message=str(exc)[:300],
                    )
                    return 1
                retain_images(image_ref, _image_state()["rollback_image_id"])
                write_status(
                    config_value, operation_id=operation, state="no_change",
                    expected_sha=expected, code=None,
                    message="This container already uses that official image.",
                    worker_adoption=adopt_from_image(digest),
                )
                return 0
            subprocess.run(["docker", "tag", previous, ROLLBACK_TAG], check=True,
                           text=True, capture_output=True)
            transaction = transaction_record(operation, expected, nonce,
                                             previous, digest)
            # From here an interruption can leave chats drained or the app
            # removed; reconcile() settles it from this record.
            write_transaction(transaction)
            request_drain(config_value, operation, cid)
            write_status(config_value, operation_id=operation, state="replacing",
                         expected_sha=expected, code=None,
                         message="Rebuilding the container.")
            replacement_started = True
            subprocess.run(["docker", "tag", digest, TARGET_TAG], check=True,
                           text=True, capture_output=True)
            compose(config_value, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=TARGET_TAG)
            write_status(config_value, operation_id=operation, state="verifying",
                         expected_sha=expected, message="Checking the new container.")
            if not wait_healthy(config_value):
                result = rollback(
                    config_value, operation, expected,
                    "health_check_failed", "the new container was unhealthy",
                    previous,
                )
                discard_pulled_image(image_ref)
                return result
            cid, current = app_container(config_value)
            if current != digest:
                raise RuntimeError("the new container does not run the verified image")
            verify_served_generation(cid, expected)
            handoff_finalized = restart_ledger(
                config_value, cid, "finalize-cutover", operation,
                image=digest,
            )
            retain_images(image_ref, previous)
            if handoff_finalized:
                status_code = None
                message = "Container rebuilt successfully."
            else:
                status_code = "handoff_finalize_failed"
                message = (
                    "Container rebuilt successfully, but the Host could not "
                    "verify and retire the exact chat handoff receipt. Check "
                    "the affected chats."
                )
            clear_transaction()
            write_status(config_value, operation_id=operation, state="succeeded",
                         expected_sha=expected, code=status_code,
                         message=message,
                         worker_adoption=adopt_from_image(digest))
            return 0
        except Exception as exc:
            detail = str(exc)[:300]
            if replacement_started and previous and expected:
                try:
                    result = rollback(config_value, operation, expected,
                                      "replacement_failed", detail, previous)
                    if image_ref and pulled_recorded:
                        discard_pulled_image(image_ref)
                    return result
                except Exception as rollback_exc:
                    detail = f"{detail}; rollback failed: {str(rollback_exc)[:160]}"
                    write_status(config_value, operation_id=operation,
                                 state="needs_recovery", expected_sha=expected,
                                 code="rollback_failed", message=detail[:300])
                    return 1
            if image_ref and pulled_recorded:
                discard_pulled_image(image_ref)
            # A failure before replacement leaves the running app in place.
            clear_transaction()
            write_status(config_value, operation_id=operation, state="failed",
                         expected_sha=expected, code="replacement_failed", message=detail)
            return 1
        finally:
            # Only the verified claim is ever removed; whatever is still in the
            # inbox may be a newer request and stays for the next run or withdrawal.
            if claim_verified:
                _discard_claim(claimed)


def transaction_record(operation: str, expected: str, nonce: str | None,
                       previous: str, target: str) -> dict:
    return {
        "version": 1, "operation_id": operation, "expected_sha": expected,
        "request_nonce": nonce, "previous_image": previous,
        # Unused here, but revision 1 workers (a launcher's fallback)
        # recognise a journal only with it.
        "target_image": target,
    }


def write_transaction(value: dict) -> None:
    _durable_write(
        TRANSACTION, json.dumps(value, separators=(",", ":")).encode(), 0o600,
    )


def clear_transaction() -> None:
    TRANSACTION.unlink(missing_ok=True)
    _fsync_dir(TRANSACTION.parent)


def read_transaction() -> dict | None:
    try:
        value = read_json(TRANSACTION)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    ok = (
        value.get("version") == 1
        and OPERATION_RE.fullmatch(str(value.get("operation_id") or ""))
        and SHA_RE.fullmatch(str(value.get("expected_sha") or ""))
        and str(value.get("previous_image") or "").startswith("sha256:")
    )
    return value if ok else None


def recover(config_value: dict, transaction: dict) -> None:
    """Restore the previous container a worker left mid-replacement, so the
    app can request the update again."""
    operation = transaction["operation_id"]
    expected = transaction["expected_sha"]
    fields = {"request_nonce": transaction.get("request_nonce")}
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=None, **fields,
                 message="Recovering an interrupted replacement.")
    # Restore exactly the journaled previous image, whatever the tag says now.
    previous = str(transaction.get("previous_image") or "")
    try:
        if not previous.startswith("sha256:"):
            raise RuntimeError("no recorded previous image")
        subprocess.run(["docker", "tag", previous, ROLLBACK_TAG], check=True,
                       text=True, capture_output=True, timeout=60)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        write_status(
            config_value, operation_id=operation, state="needs_recovery",
            expected_sha=expected, code="rollback_failed", **fields,
            message="The interrupted replacement could not select the "
                    "recorded previous image to restore.",
        )
        return
    rollback(config_value, operation, expected, "worker_interrupted",
             "the replacement worker stopped before it finished", previous)


def reconcile() -> int:
    config_value = config()
    try:
        current = read_json(STATUS)
    except (OSError, ValueError, json.JSONDecodeError):
        current = None
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # A hard power loss can strand the already-claimed request before or
        # after the first status write. Once no worker owns the lock, it is no
        # longer runnable and must not accumulate in the root-controlled area.
        for claimed in config_value["control_dir"].glob(".request-*.json"):
            if re.fullmatch(r"\.request-[0-9a-f]{32}\.json", claimed.name):
                _discard_claim(claimed)
        # Extraction containers are never started; one left by a killed
        # worker only holds a reference to its image.
        try:
            leftovers = subprocess.run(
                ["docker", "ps", "-aq", "--filter",
                 "label=mobius-rebuild.worker-extract=1"],
                text=True, capture_output=True, timeout=60,
            ).stdout.split()
            if leftovers:
                subprocess.run(["docker", "rm", "-f", "-v", *leftovers],
                               text=True, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass  # best effort; reconcile's real work follows
        transaction = read_transaction()
        if transaction is not None:
            recover(config_value, transaction)
        elif current is None:
            write_status(config_value)
        elif current.get("state") in ACTIVE_STATES:
            write_status(config_value, state="failed", code="worker_interrupted",
                         message="The host replacement worker stopped unexpectedly.")
        else:
            # Installation and boot reconciliation also refresh the controller
            # capability receipt.  Otherwise an upgraded helper can remain
            # invisible behind an idle status written by the previous binary.
            write_status(config_value)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "run" and os.geteuid() == 0:
        raise SystemExit(run())
    if len(sys.argv) == 2 and sys.argv[1] == "reconcile" and os.geteuid() == 0:
        raise SystemExit(reconcile())
    if len(sys.argv) == 2 and sys.argv[1] == "adopt-self" and os.geteuid() == 0:
        # The installer seeds the launcher with this trusted checkout's worker.
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The installer holds the replacement lock for its whole installation
        # and says so; otherwise take it here.
        with LOCK.open("a+") as lock:
            if os.environ.get("MOBIUS_REBUILD_LOCK_HELD") != "1":
                fcntl.flock(lock, fcntl.LOCK_EX)
            outcome = seed_worker(Path(__file__).read_bytes())
        print(outcome)
        raise SystemExit(1 if outcome.startswith("rejected") else 0)
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
