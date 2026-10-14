#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container rebuild."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/var/lib/mobius-rebuild")
# Version 4 lives at its own versioned path. The released v3 worker keeps
# reading config.json, so neither worker ever reads the other's schema and
# the installer's durable worker selection is the single migration commit.
CONFIG = Path("/etc/mobius-rebuild/config.v4.json")
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
WORKER_REVISION = 15
# The frozen launcher runs the worker selected here; see offer_worker().
WORKERS = STATE_DIR / "workers"
WORKER_INDEX = STATE_DIR / "workers.json"
WORKER_IN_IMAGE = "/app/platform-baked/scripts/mobius-rebuild-host.py"
MAX_WORKER_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 4096
# The installer records the helper protocol it completed (units, config,
# launcher and admission helper together). Workers only report it; the app
# refuses to dispatch a replacement until it meets the served source's
# deployment/self-hosted-helper.required.
HELPER_PROTOCOL = STATE_DIR / "helper-protocol"
ADMISSION_CODE = Path("/usr/local/libexec/mobius-boot-admission.py")
ADMISSION_ROOT = STATE_DIR / "admission"
ADMISSION_CODE_TARGET = "/run/mobius-boot-admission.py"
ADMISSION_STATE_TARGET = "/run/mobius-admission"
COMPOSE_MUTATION_SECONDS = 300
TARGET_HEALTH_SECONDS = 600
ROLLBACK_HEALTH_SECONDS = 600
FAILED_TARGET_LOG = STATE_DIR / "failed-target.json"
FAILED_TARGET_LOG_BYTES = 32 * 1024
FAILED_TARGET_LOG_SECONDS = 5
DEPLOYMENT_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}")
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
FULL_CID_RE = re.compile(r"[0-9a-f]{64}")
ATTEMPT_PROJECT_PREFIX = "mobius-admission-"
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
    # Version 4 freezes the live app's exact Docker name, proven by the
    # installer from its Compose labels. Every attempt reuses it so a raw
    # Compose invocation of the original project conflicts instead of starting
    # a second unwrapped app. Generated names keep their separator and index 1.
    if (set(value) != {"version", "project", "data_dir", "deployment_name", "name_source"}
            or value["version"] != 4):
        raise ValueError("invalid fixed deployment configuration; reinstall the host helper")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", str(value["project"])):
        raise ValueError("invalid Compose project")
    if (not isinstance(value["deployment_name"], str)
            or not DEPLOYMENT_NAME_RE.fullmatch(value["deployment_name"])
            or value["name_source"] not in {"explicit", "generated"}):
        raise ValueError("invalid frozen deployment container name")
    if (value["name_source"] == "generated" and value["deployment_name"] not in {
            value["project"] + "-app-1", value["project"] + "_app_1"}):
        raise ValueError("generated deployment name must preserve the original project at index 1")
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












































def installed_helper_protocol(*, trusted_uid: int = 0) -> int:
    """The protocol a completed installer run certified; 0 when unknown."""
    try:
        info = HELPER_PROTOCOL.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != trusted_uid
                or info.st_mode & 0o022):
            return 0
        value = HELPER_PROTOCOL.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return 0
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,5}", value) else 0


def persistent_data_dir(kind: str, source: str) -> str:
    """The host directory that IS the app's /data whether or not it runs.

    A bind mount's source is that directory. A plain ``local`` volume keeps
    its data in Docker's Mountpoint. A ``local`` volume created with bind
    options (type=none, o=bind, device=DIR) is only *mounted* at its
    Mountpoint while a container uses it: once the app container is removed
    -- exactly when a replacement or rollback needs the data -- that path is
    a stale, unrelated directory on the host's root filesystem. Host reads,
    writes, watches and ``docker run --mount type=bind`` must use DIR.
    Anything the host cannot reach while the app is stopped is refused.
    """
    if kind == "bind":
        path = source
    elif kind == "volume":
        volume = json.loads(docker_command(
            ["docker", "volume", "inspect", "--format", "{{json .}}", source],
            timeout=30,
        ).stdout)
        options = volume.get("Options") or {}
        if volume.get("Name") != source or volume.get("Driver") != "local":
            raise RuntimeError("the /data volume must use Docker's local driver so the "
                               "host helper can reach it while the app is stopped")
        if not options:
            path = volume.get("Mountpoint", "")
        elif (set(options) <= {"type", "o", "device"} and options.get("type") == "none"
              and {"bind", "rbind"} & set(str(options.get("o", "")).split(","))
              and "ro" not in str(options.get("o", "")).split(",")):
            path = options.get("device", "")
        else:
            raise RuntimeError("the /data volume's driver options hide its data from the "
                               "host while the app is stopped; use a bind mount or a "
                               "plain local volume")
    else:
        raise RuntimeError("replacement cannot preserve this /data mount type")
    if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path):
        raise RuntimeError("the persistent /data directory is not reachable on the host")
    return os.path.realpath(path)


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
    if "operation_id" in fields and fields["operation_id"] != current.get("operation_id"):
        current.update(failure_code=None, failure_detail=None, evidence_capture=None)
    current.update(fields)
    current["handoff"] = HANDOFF_VERSION
    current["request_versions"] = REQUEST_VERSIONS
    current["worker_revision"] = WORKER_REVISION
    current["helper_protocol"] = installed_helper_protocol()
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER", "")
    current["launcher_revision"] = int(launcher) if launcher.isdigit() else None
    current.pop("runtime_overlay", None)
    current["updated_at"] = now()
    _atomic_json(STATUS, current)
    _atomic_json(config_value["control_dir"] / "status.json", current, 0o644)
    return current


def docker_command(args: list[str], *, timeout: float = 10, check: bool = True,
                   **kwargs) -> subprocess.CompletedProcess:
    """Bound Docker and its Compose plugin, including inherited output pipes."""
    if timeout <= 0:
        raise subprocess.TimeoutExpired(args, timeout)
    # Files, not pipes: a surviving descendant cannot keep communicate() open.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        with subprocess.Popen(args, stdout=out, stderr=err,
                              start_new_session=True, **kwargs) as process:
            try:
                process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            out.seek(0)
            err.seek(0)
            result = subprocess.CompletedProcess(
                args, process.returncode, out.read().decode(errors="replace"),
                err.read().decode(errors="replace"),
            )
    if check:
        result.check_returncode()
    return result


def _bounded_docker_logs(cid: str) -> tuple[bytes, bytes, bool, bool]:
    """Keep at most one small prefix of each Docker output stream in total.

    Pipes are drained only until the byte or wall-clock limit; the process
    group is then killed, including a Docker CLI descendant holding a pipe.
    A timed-out prefix is still useful evidence, but is always incomplete.
    """
    args = ["docker", "logs", "--timestamps", "--tail", "1000", cid]
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + FAILED_TARGET_LOG_SECONDS
    truncated = False
    timed_out = False
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, sink in ((process.stdout, out), (process.stderr, err)):
                selector.register(pipe, selectors.EVENT_READ, sink)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready = selector.select(remaining)
                if not ready:
                    timed_out = True
                    break
                for key, _ in ready:
                    budget = FAILED_TARGET_LOG_BYTES - len(out) - len(err)
                    if budget <= 0:
                        truncated = True
                        break
                    chunk = os.read(key.fd, min(4096, budget + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk[:budget])
                        if len(chunk) > budget or len(out) + len(err) >= FAILED_TARGET_LOG_BYTES:
                            truncated = True
                            break
                if truncated:
                    break
        if timed_out or truncated or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=1)
        completed = True
        if process.returncode and not (truncated or timed_out):
            raise subprocess.CalledProcessError(process.returncode, args)
        return bytes(out), bytes(err), truncated or timed_out, timed_out
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()


def capture_failed_target(operation: str, cid: str, image: str) -> str:
    """Best-effort, root-private evidence for this journaled target only."""
    if not CONTAINER_RE.fullmatch(cid):
        return "failed"
    try:
        try:
            previous = (read_json(FAILED_TARGET_LOG)
                        if FAILED_TARGET_LOG.stat().st_size <= 128 * 1024 else {})
        except (OSError, ValueError):
            previous = {}
        if previous.get("operation_id") == operation and previous.get("container_id") == cid:
            return "saved"  # interrupted rollback replay must not erase evidence
        stdout, stderr, truncated, timed_out = _bounded_docker_logs(cid)
        _atomic_json(FAILED_TARGET_LOG, {
            "operation_id": operation, "container_id": cid, "target_image": image[:128],
            "captured_at": now(), "truncated": truncated, "timed_out": timed_out,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        })
        return "saved"
    except Exception:
        return "failed"


def compose(config_value: dict, *args: str, image: str | None = None,
            check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or IMAGE
    return docker_command(
        ["docker", "compose", "-p", config_value["project"],
         "-f", str(COMPOSE), "-f", str(OVERRIDE), *args],
        cwd=CONFIG.parent, env=env, check=check, timeout=timeout,
    )


def admission_store(config_value: dict, transaction: dict):
    """Load the installer-pinned gate, never code from the writable platform."""
    for path in (ADMISSION_CODE.parent, ADMISSION_CODE, ADMISSION_ROOT):
        item = path.lstat()
        if path.is_symlink() or item.st_uid != 0 or item.st_mode & 0o022:
            raise RuntimeError("the boot admission helper is not root-controlled; reinstall it")
    spec = importlib.util.spec_from_file_location("mobius_boot_admission", ADMISSION_CODE)
    if spec is None or spec.loader is None:
        raise RuntimeError("boot admission helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AdmissionStore(
        ADMISSION_ROOT / transaction["operation_id"], config_value["data_dir"],
    )


def compose_file_document(value):
    """Encode frozen literal values for one subsequent Compose parse.

    This is a file-format boundary, not a second environment resolution.
    Image argv is literal; resolved Compose output is decoded before mixing
    it with image metadata. Keys are not interpolated by Compose.
    """
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [compose_file_document(item) for item in value]
    if isinstance(value, dict):
        return {key: compose_file_document(item) for key, item in value.items()}
    return value


def wrapped_configuration(config_value: dict, transaction: dict, role: str,
                          token: str, image: str) -> dict:
    """Preserve the resolved command and PID-1 chain behind a root boot gate."""
    image_config = json.loads(inspect_image(image, "{{json .Config}}"))
    resolved = transaction.get("target_topology" if role == "target" else "source_topology")
    if resolved is None:
        env = {**os.environ, "MOBIUS_IMAGE": image}
        result = docker_command(
            ["docker", "compose", "-p", config_value["project"], "-f", str(COMPOSE),
             "config", "--format", "json"], env=env, cwd=CONFIG.parent,
        )
        resolved = json.loads(result.stdout)
    # `compose config` escapes every dollar in its rendered JSON, including
    # mapping keys. Recover literal values before adding raw image argv.
    resolved = json.loads(json.dumps(resolved).replace("$$", "$"))
    service = resolved["services"]["app"]
    # The Docker name is deployment identity, frozen from the proven live app.
    # A topology may not silently rename it: an old checkout's raw Compose
    # would otherwise create its unwrapped app under the vacated old name.
    deployment_name = config_value["deployment_name"]
    declared = service.get("container_name")
    if (declared != deployment_name if config_value["name_source"] == "explicit"
            else declared is not None or resolved.get("name") != config_value["project"]):
        raise RuntimeError("the app container name differs from the frozen deployment; "
                           "renaming requires an explicit host migration")
    entrypoint = service.get("entrypoint")
    command = service.get("command")
    if command is None:
        # An explicit Compose entrypoint suppresses the image CMD. Preserve
        # that contract before replacing the entrypoint with admission.
        command = (image_config.get("Cmd") or []) if entrypoint is None else []
    if entrypoint is None:
        entrypoint = image_config.get("Entrypoint") or []
    if (not isinstance(entrypoint, list) or not isinstance(command, list)
            or not entrypoint + command
            or any(not isinstance(value, str) or "\0" in value for value in entrypoint + command)):
        raise RuntimeError("cannot preserve the image's original startup command")
    data_mounts = [v for v in service.get("volumes", []) if v.get("target") == "/data"]
    if len(data_mounts) != 1 or data_mounts[0].get("read_only", False):
        raise RuntimeError("replacement must preserve one writable /data mount")
    data_mount = data_mounts[0]
    source = data_mount.get("source", "")
    if data_mount.get("type") == "volume":
        source = resolved.get("volumes", {}).get(source, {}).get("name")
        if not source:
            raise RuntimeError("persistent data volume has no immutable resource name")
    if persistent_data_dir(data_mount.get("type"), source) != os.path.realpath(config_value["data_dir"]):
        raise RuntimeError("the trusted host data path is not this deployment's persistent "
                           "/data; rerun scripts/install-rebuild-helper.sh")
    prefix = ["python3", "-I", "-S", ADMISSION_CODE_TARGET, "enter",
              "--state-dir", ADMISSION_STATE_TARGET, "--data-dir", "/data",
              "--token", token, "--"]
    app = {**service,
        "image": image,
        "container_name": deployment_name,
        "entrypoint": prefix + entrypoint,
        "command": command,
        "labels": {**service.get("labels", {}),
                   "io.mobius.admission.project": config_value["project"],
                   "io.mobius.admission.operation": transaction["operation_id"],
                   "io.mobius.admission.role": role, "io.mobius.admission.token": token},
        "volumes": [*service.get("volumes", []),
            {"type": "bind", "source": str(ADMISSION_CODE),
             "target": ADMISSION_CODE_TARGET, "read_only": True},
            {"type": "bind", "source": str(ADMISSION_ROOT / transaction["operation_id"]),
             "target": ADMISSION_STATE_TARGET, "read_only": False},
        ],
    }
    # Compose must never reconcile other attempts or own shared resources.
    # Its per-generation project contains one app and references the original
    # deployment's named resources externally. Delayed old Create calls cannot
    # make a later project's scale-down touch the admitted successor.
    for key in ("build", "depends_on", "links", "volumes_from"):
        if key in {"links", "volumes_from"} and app.get(key):
            raise RuntimeError("cross-service container references need an explicit topology migration")
        app.pop(key, None)
    for key in ("network_mode", "pid", "ipc"):
        if str(app.get(key, "")).startswith("service:"):
            raise RuntimeError("shared service namespaces need an explicit topology migration")
    networks = {}
    for key, value in resolved.get("networks", {}).items():
        if key in app.get("networks", {}):
            networks[key] = {"name": value["name"], "external": True}
    volumes = {}
    for volume in app.get("volumes", []):
        if volume.get("type") == "volume":
            name = volume.get("source")
            if not name or name not in resolved.get("volumes", {}):
                raise RuntimeError("anonymous app volumes cannot be preserved across recovery")
            volumes[name] = {"name": resolved["volumes"][name]["name"], "external": True}
    result = {"name": ATTEMPT_PROJECT_PREFIX + token, "services": {"app": app},
              "networks": networks, "volumes": volumes}
    if app.get("configs") or app.get("secrets"):
        raise RuntimeError("app configs/secrets require an explicit preserved-resource topology")
    return result


def verify_wrapped_container(cid: str, expected: dict) -> dict:
    """Bind admission only to the exact created container and frozen wrapper."""
    result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", cid])
    return verify_attempt_item(json.loads(result.stdout), cid, expected)


def verify_attempt_item(item: dict, cid: str, expected: dict) -> dict:
    """Docker-owned metadata only; nothing here is read from container-writable state.

    Compose sets its project label itself and it overrides service labels, so
    the per-attempt project proves which immutable attempt file created it.
    """
    config_value = item.get("Config") or {} if isinstance(item, dict) else {}
    labels = config_value.get("Labels") or {}
    app = expected["services"]["app"]
    if (not isinstance(item, dict) or not FULL_CID_RE.fullmatch(cid) or item.get("Id") != cid
            or item.get("Image") != app["image"]
            or config_value.get("Entrypoint") != app["entrypoint"]
            or (config_value.get("Cmd") if config_value.get("Cmd") is not None else []) != app["command"]
            or any(labels.get(k) != v for k, v in app["labels"].items())
            or labels.get("com.docker.compose.project") != expected["name"]
            or item.get("Name") != "/" + app["container_name"]):
        raise RuntimeError("container identity or pre-entrypoint admission coverage is unconfirmed")
    mounts = {mount.get("Destination"): mount for mount in item.get("Mounts", [])}
    for volume in app["volumes"]:
        actual = mounts.get(volume["target"], {})
        if volume["type"] == "volume":
            name = expected["volumes"][volume["source"]]["name"]
            if actual.get("Type") != "volume" or actual.get("Name") != name:
                raise RuntimeError("container volume identity changed")
            continue
        if (actual.get("Type") != "bind" or actual.get("Source") != volume["source"]
                or actual.get("RW") != (not volume.get("read_only", False))):
            raise RuntimeError("container admission mount does not match the root-controlled gate")
    return item


def remove_exact_container(cid: str) -> None:
    """Fence one immutable Docker resource; transport errors never mean absent."""
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError("container removal requires an exact full ID")
    result = docker_command(["docker", "container", "rm", "--force", cid],
                            timeout=COMPOSE_MUTATION_SECONDS, check=False)
    if result.returncode == 0:
        return
    # Docker's exact-resource not-found response also fences a late start of
    # that ID. Never interpret generic inspection/list failures as this proof.
    message = result.stderr.strip().lower()
    if result.returncode == 1 and message in {
            f"error response from daemon: no such container: {cid}",
            f"error response from daemon: no such object: {cid}"}:
        return
    result.check_returncode()


def inspect_image(image: str, template: str) -> str:
    result = docker_command(
        ["docker", "image", "inspect", "--format", template, image],
        timeout=30,
    )
    return result.stdout.strip()


class AttemptIdentityMismatch(RuntimeError):
    """Docker or gate identity disagrees with root-private host records.

    Never resolved by mutation: no Docker call is made on a foreign or tampered
    identity. Recovery stays journaled for explicit Host inspection.
    """


def literal_document(value):
    """Exact inverse of compose_file_document for one frozen attempt file."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, list):
        return [literal_document(item) for item in value]
    if isinstance(value, dict):
        return {key: literal_document(item) for key, item in value.items()}
    return value


def attempt_path(token: str) -> Path:
    if not TOKEN_RE.fullmatch(str(token)):
        raise AttemptIdentityMismatch("invalid admission attempt token")
    return CONFIG.parent / "attempts" / (token + ".json")


def read_attempt_file(token: str) -> tuple[bytes, dict]:
    """Root-owned immutable attempt config: (exact bytes, literal expected)."""
    path = attempt_path(token)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024 * 1024):
            raise AttemptIdentityMismatch("untrusted admission attempt configuration")
        raw = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    expected = literal_document(json.loads(raw))
    app = expected.get("services", {}).get("app", {})
    if (expected.get("name") != ATTEMPT_PROJECT_PREFIX + token
            or app.get("labels", {}).get("io.mobius.admission.token") != token):
        raise AttemptIdentityMismatch("admission attempt configuration names another token")
    return raw, expected


def current_attempt(config_value: dict) -> dict | None:
    """The live attempt pointer, from root-private files only (never the gate)."""
    try:
        raw = OVERRIDE.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError):
        return None  # the installer's legacy YAML override: original project
    if not isinstance(value, dict):
        return None
    labels = value.get("services", {}).get("app", {}).get("labels", {})
    token = labels.get("io.mobius.admission.token") if isinstance(labels, dict) else None
    if token is None:
        return None
    attempt_raw, expected = read_attempt_file(str(token))
    if (attempt_raw != raw
            or expected["services"]["app"].get("container_name") != config_value["deployment_name"]):
        raise AttemptIdentityMismatch("current admission identity is not the frozen attempt")
    return expected


def name_holder(name: str, *, timeout: float = 10) -> tuple[str, dict] | None:
    """Exact Docker name lookup; only Docker's own not-found response is absence."""
    result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", name],
                            check=False, timeout=timeout)
    if result.returncode:
        if result.returncode == 1 and result.stderr.strip().lower() in {
                f"error response from daemon: no such container: {name}",
                f"error response from daemon: no such object: {name}"}:
            return None
        result.check_returncode()
    item = json.loads(result.stdout)
    if not isinstance(item, dict) or item.get("Name") != "/" + name:
        return None  # Docker matched an ID prefix: nothing holds this exact name
    cid = item.get("Id")
    if not isinstance(cid, str) or not FULL_CID_RE.fullmatch(cid):
        raise AttemptIdentityMismatch("Docker returned an invalid container identity")
    return cid, item


def observe_exact(cid: str) -> dict | None:
    """One immutable container ID: inspected item, or None only when Docker says absent."""
    if not FULL_CID_RE.fullmatch(str(cid)):
        raise AttemptIdentityMismatch("exact container observation requires a full ID")
    result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", cid],
                            check=False)
    if result.returncode:
        if result.returncode == 1 and result.stderr.strip().lower() in {
                f"error response from daemon: no such container: {cid}",
                f"error response from daemon: no such object: {cid}"}:
            return None
        result.check_returncode()
    item = json.loads(result.stdout)
    if not isinstance(item, dict) or item.get("Id") != cid:
        raise AttemptIdentityMismatch("Docker returned a different container identity")
    return item


def tombstoned(token: str) -> str | None:
    """The retired attempt file digest, if this exact token was ever retired."""
    return read_tombstones()["tokens"].get(token)


def classify_holder(item: dict, cid: str, current: dict | None) -> str:
    """current | retired | foreign. Retired needs the frozen config to match exactly."""
    labels = (item.get("Config") or {}).get("Labels") or {}
    token = labels.get("io.mobius.admission.token")
    # Retirement wins: the live pointer can still name a token whose exact
    # container was removed, and its late duplicate Create is never current.
    if isinstance(token, str) and TOKEN_RE.fullmatch(token):
        digest = tombstoned(token)
        if digest is not None:
            raw, expected = read_attempt_file(token)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise AttemptIdentityMismatch("retired attempt configuration changed")
            verify_attempt_item(item, cid, expected)
            return "retired"
    if current is not None and token == current["services"]["app"]["labels"]["io.mobius.admission.token"]:
        verify_attempt_item(item, cid, current)
        return "current"
    return "foreign"


def app_container(config_value: dict) -> tuple[str, str]:
    current = current_attempt(config_value)
    if current:
        held = name_holder(config_value["deployment_name"])
        if held is None:
            raise RuntimeError("the recorded Möbius app container is not running")
        cid, item = held
        if classify_holder(item, cid, current) != "current":
            raise AttemptIdentityMismatch("another container holds the deployment name")
        return cid, item["Image"]
    result = compose(config_value, "ps", "-q", "app")
    cid = result.stdout.strip()
    if not FULL_CID_RE.fullmatch(cid):
        raise RuntimeError("the app container identity is missing or ambiguous")
    inspected = docker_command(
        ["docker", "container", "inspect", "--format", "{{.Image}}", cid],
        timeout=30,
    )
    return cid, inspected.stdout.strip()


def container_health(config_value: dict, timeout: float = 10) -> tuple[str, str, str]:
    """Re-discover after a stale ID; errors never mean the container is absent.

    Compose can delete/rename containers between discovery and inspect. Retry
    the whole observation, within the same two-query budget, not the old ID.
    Multiple candidates are ambiguous until Compose finishes its mutation.
    A retired attempt holding the stable name is not the current app; any
    other foreign holder raises AttemptIdentityMismatch without retry.
    """
    deadline = time.monotonic() + 2 * timeout
    for attempt in range(3):
        try:
            budget = min(timeout, max(0, deadline - time.monotonic()) / 2)
            if budget <= 0:
                raise subprocess.TimeoutExpired("Docker container observation", 2 * timeout)
            current = current_attempt(config_value)
            if current:
                held = name_holder(config_value["deployment_name"], timeout=budget)
                if held is None:
                    return "", "", "missing"
                cid, item = held
                kind = classify_holder(item, cid, current)
                if kind == "retired":
                    return "", "", "missing"
                if kind != "current":
                    raise AttemptIdentityMismatch("another container holds the deployment name")
                state = item.get("State") or {}
                health = (state.get("Health") or {}).get("Status")
                status = state.get("Status")
                return cid, item["Image"], (health if status == "running" and health else status)
            result = compose(config_value, "ps", "-a", "-q", "app", timeout=budget)
            ids = result.stdout.split()
            if not ids:
                return "", "", "missing"
            if len(ids) != 1:
                raise ValueError("Compose app container identity is ambiguous")
            cid = ids[0]
            probe = docker_command(
                ["docker", "container", "inspect", "--format",
                 "{{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", cid],
                check=True, timeout=min(timeout, max(0, deadline - time.monotonic())),
            )
            image, state, *health = probe.stdout.strip().split()
            return cid, image, (health[0] if state == "running" and health else state)
        except (OSError, ValueError, subprocess.SubprocessError):
            if attempt == 2 or time.monotonic() >= deadline:
                raise
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def wait_healthy(config_value: dict, timeout: int = TARGET_HEALTH_SECONDS) -> bool:
    """False means not serviceable within budget, not proof of a crashed boot."""
    deadline = time.monotonic() + timeout
    probe_failed = False
    while time.monotonic() < deadline:
        try:
            _cid, _image, health = container_health(
                config_value, timeout=min(10, max(0, deadline - time.monotonic()) / 2),
            )
            probe_failed = False
            if health == "healthy":
                return True
        except (OSError, ValueError, subprocess.SubprocessError):
            # A transient daemon/Compose query error is not a failed boot.
            probe_failed = True
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    if probe_failed:
        raise subprocess.TimeoutExpired("Docker health observation", timeout)
    return False


class ProvenanceRejected(RuntimeError):
    """A well-formed observation proves the served image is not acceptable."""


class ProvenanceUnconfirmed(RuntimeError):
    """The served image could not be proved either correct or incorrect."""


def verify_served_generation(cid: str, expected_sha: str) -> None:
    """Prove the running container serves the requested immutable image."""
    try:
        result = docker_command(
            ["docker", "exec", cid, "curl", "-fsS",
             "http://127.0.0.1:8000/api/version"],
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProvenanceUnconfirmed("build provenance could not be observed") from exc
    if result.returncode != 0:
        raise ProvenanceUnconfirmed("build provenance could not be observed")
    try:
        version = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceUnconfirmed("the container returned invalid build provenance") from exc
    if (not isinstance(version, dict) or not isinstance(version.get("sha"), str)
            or not SHA_RE.fullmatch(version["sha"])):
        raise ProvenanceUnconfirmed("the container returned invalid build provenance")
    if version["sha"] != expected_sha:
        raise ProvenanceRejected("the container is not serving the requested image revision")
    try:
        mounts = docker_command(
            ["docker", "container", "inspect", "--format", "{{json .Mounts}}", cid],
            check=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProvenanceUnconfirmed("mount provenance could not be observed") from exc
    try:
        mounted = json.loads(mounts.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceUnconfirmed("the container returned invalid mount provenance") from exc
    if not isinstance(mounted, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("Destination"), str)
            for item in mounted):
        raise ProvenanceUnconfirmed("the container returned invalid mount provenance")
    if any(item.get("Destination") == "/app/runtime" for item in mounted):
        raise ProvenanceRejected("the container is not using the image's protected runtime")


def _docker_root() -> Path:
    result = docker_command(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        timeout=30,
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
    if command == "rearm-cutover":
        raise RuntimeError("only the boot admission gate may prepare recovery authorization")
    invocation = ["python3", "-P", "/app/runtime/restart_ledger.py",
                  command, operation]
    result = docker_command(
        ["docker", "exec", cid, *invocation],
        check=False,
    )
    if result.returncode == 0:
        return True
    if not image:
        return False
    # A failed replacement may not stay alive long enough for docker exec.
    # The prior verified image carries the same frozen helper; mount only the
    # persistent data root and run no entrypoint or application code.
    result = docker_command(
        ["docker", "run", "--rm", "--mount",
         f"type=bind,src={config_value['data_dir']},dst=/data",
         "--entrypoint", "python3", image, *invocation[1:]],
        check=False,
    )
    return result.returncode == 0


def request_drain(config_value: dict, operation: str, cid: str) -> None:
    if not restart_ledger(config_value, cid, "open-cutover", operation):
        raise RuntimeError("the running image does not support safe Host cutover")
    result = docker_command(
        ["docker", "exec", cid, "python3",
         "/data/platform/backend/scripts/prepare-container-cutover.py", operation],
        check=False, timeout=90,
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
    docker_command(["docker", "image", "rm", target_ref], check=False, timeout=30)
    _atomic_json(IMAGES, {
        **state,
        "sha_refs": [ref for ref in state["sha_refs"] if ref != target_ref],
    })


def retain_images(target_ref: str, rollback_image_id: str | None = None) -> None:
    state = _image_state()
    obsolete = [ref for ref in state["sha_refs"] if ref != target_ref]
    old_rollback = state["rollback_image_id"]
    if old_rollback and old_rollback != rollback_image_id:
        obsolete.append(old_rollback)
    if obsolete:
        # One bounded, non-force removal; references still used by containers
        # fail harmlessly. Never remove images outside our recorded references.
        docker_command(["docker", "image", "rm", *obsolete], check=False)
    _atomic_json(IMAGES, {
        "sha_refs": [target_ref],
        "rollback_tag": ROLLBACK_TAG,
        "rollback_image_id": rollback_image_id,
    })


def never_started(cid: str, image: str) -> bool:
    """A created container is retryable only before its first actual boot."""
    result = docker_command(["docker", "container", "inspect", "--format",
                             "{{json .}}", cid])
    item = json.loads(result.stdout)
    if not isinstance(item, dict) or not isinstance(item.get("State"), dict):
        raise ValueError("invalid Docker container state")
    state = item["State"]
    return (item.get("Id") == cid and item.get("Image") == image
            and state.get("Status") == "created"
            and state.get("StartedAt") == "0001-01-01T00:00:00Z"
            and item.get("RestartCount") == 0)


def tombstone_path() -> Path:
    return STATE_DIR / "admission-tombstones.json"


def read_tombstones() -> dict:
    """Root-private retired-attempt history: {"version": 1, "tokens": {token: sha256}}.

    Entries are never removed: several timed-out no-start Creates for one
    token can each land later. Malformed history fails closed (no removal).
    """
    path = tombstone_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"version": 1, "tokens": {}}
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise AttemptIdentityMismatch("untrusted retired-attempt history")
    value = json.loads(path.read_text(encoding="utf-8"))
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if (set(value) != {"version", "tokens"} or value["version"] != 1 or not isinstance(tokens, dict)
            or any(not TOKEN_RE.fullmatch(str(k)) or not isinstance(v, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in tokens.items())):
        raise AttemptIdentityMismatch("malformed retired-attempt history")
    return value


def retire_token(token: str) -> None:
    """Durably record that this exact attempt config may only ever be an orphan."""
    raw, _ = read_attempt_file(token)
    digest = hashlib.sha256(raw).hexdigest()
    history = read_tombstones()
    recorded = history["tokens"].get(token)
    if recorded is not None:
        if recorded != digest:
            raise AttemptIdentityMismatch("retired attempt configuration changed")
        return
    history["tokens"][token] = digest
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _durable_write(tombstone_path(), json.dumps(history, sort_keys=True,
                                                separators=(",", ":")).encode(), 0o600)


JOURNAL_ATTEMPT_KEYS = {"role", "generation", "token", "image", "attempt_sha256", "cid"}


def journal_attempts(transaction: dict, role: str | None = None) -> list[dict]:
    """Root-private attempt identities, appended before the gate learns them.

    Schema: {"role", "generation" (1-based per role), "token" (32 hex),
    "image" (the role's journaled image), "attempt_sha256" (exact immutable
    attempt file bytes), "cid" (null until Create/discovery, before bind)}.
    """
    records = transaction.get("attempts", [])
    if not isinstance(records, list):
        raise AttemptIdentityMismatch("invalid journaled attempts")
    counts, tokens, selected = {"target": 0, "rollback": 0}, set(), []
    for record in records:
        if not isinstance(record, dict) or set(record) != JOURNAL_ATTEMPT_KEYS:
            raise AttemptIdentityMismatch("invalid journaled attempt")
        kind = record["role"]
        if kind not in counts:
            raise AttemptIdentityMismatch("invalid journaled attempt role")
        counts[kind] += 1
        image = transaction["target_image" if kind == "target" else "previous_image"]
        if (type(record["generation"]) is not int or record["generation"] != counts[kind]
                or not TOKEN_RE.fullmatch(str(record["token"])) or record["token"] in tokens
                or record["image"] != image
                or not re.fullmatch(r"[0-9a-f]{64}", str(record["attempt_sha256"]))
                or (record["cid"] is not None and not FULL_CID_RE.fullmatch(str(record["cid"])))):
            raise AttemptIdentityMismatch("invalid journaled attempt")
        tokens.add(record["token"])
        if role is None or kind == role:
            selected.append(record)
    return selected


def checked_gate(gate, transaction: dict) -> dict:
    """Observe the container-writable gate and refuse any disagreement with the
    root journal BEFORE a Docker mutation. Gate quiescence flags are never
    used to choose or skip a host action."""
    state = gate.observe()
    journal_attempts(transaction)
    if (state["operation"] != transaction["operation_id"]
            or state["source"]["cid"] != transaction["source_container"]
            or state["source"]["image"] != transaction["previous_image"]):
        raise AttemptIdentityMismatch("admission and replacement journals disagree")
    for role in ("target", "rollback"):
        records = journal_attempts(transaction, role)
        slot = state["slots"][role]
        attempts = slot["attempts"]
        if len(attempts) > len(records) or len(records) > len(attempts) + 1:
            raise AttemptIdentityMismatch("admission attempts disagree with the root journal")
        for record, attempt in zip(records, attempts):
            if ((attempt["generation"], attempt["token"], attempt["image"])
                    != (record["generation"], record["token"], record["image"])
                    or (attempt["cid"] is not None and attempt["cid"] != record["cid"])):
                raise AttemptIdentityMismatch("admission attempt identity disagrees with the root journal")
        if len(records) > len(attempts) and records[-1]["cid"] is not None:
            raise AttemptIdentityMismatch("journaled attempt was bound before allocation")
        if slot["consumed"] is not None and slot["consumed"]["generation"] != len(attempts):
            raise AttemptIdentityMismatch("admission consumed an unexpected generation")
    return state


def journaled_attempt(record: dict) -> tuple[bytes, dict]:
    raw, expected = read_attempt_file(record["token"])
    if hashlib.sha256(raw).hexdigest() != record["attempt_sha256"]:
        raise AttemptIdentityMismatch("journaled attempt configuration changed")
    return raw, expected


def journaled_expected(record: dict) -> dict:
    return journaled_attempt(record)[1]


def write_attempt_file(expected: dict) -> bytes:
    """Create (or re-verify) one immutable root-private attempt file."""
    directory = CONFIG.parent / "attempts"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError("untrusted attempt configuration directory")
    # The journal will outlive this call: publish the directory entry before
    # any attempt can be referenced. Also sync on replay after a failed mkdir
    # publication; existence alone does not prove the parent entry is durable.
    _fsync_dir(directory.parent)
    token = expected["services"]["app"]["labels"]["io.mobius.admission.token"]
    path = attempt_path(token)
    raw = json.dumps(compose_file_document(expected)).encode()
    if path.exists() or path.is_symlink():
        existing, _ = read_attempt_file(token)
        if existing != raw:
            raise RuntimeError("immutable attempt configuration changed")
    else:
        _durable_write(path, raw, 0o600)
    return raw


def discover_attempt(expected: dict) -> str:
    """This attempt's container under the stable name, or '' when vacant.

    Called only at a creation boundary. A retired attempt's late Create that
    holds the vacant name is removed by its exact verified ID; any other
    holder is foreign and refused without mutation.
    """
    held = name_holder(expected["services"]["app"]["container_name"])
    if held is None:
        return ""
    cid, item = held
    kind = classify_holder(item, cid, expected)
    if kind == "current":
        return cid
    if kind == "retired":
        remove_exact_container(cid)
        return ""
    raise AttemptIdentityMismatch("another container holds the deployment name")


def create_attempt(config_value: dict, expected: dict) -> str:
    raw = write_attempt_file(expected)
    _durable_write(OVERRIDE, raw, 0o600)
    path = attempt_path(expected["services"]["app"]["labels"]["io.mobius.admission.token"])
    cid = discover_attempt(expected)
    if not cid:
        try:
            docker_command(["docker", "compose", "-p", expected["name"], "-f", str(path),
                            "up", "--no-start", "--no-recreate", "--no-build", "--no-deps", "app"],
                           cwd=CONFIG.parent, timeout=COMPOSE_MUTATION_SECONDS)
        except subprocess.CalledProcessError:
            # A name conflict can be a retired attempt's late Create. Classify
            # (and exactly remove) it once; recovery, not a loop here, retries.
            discover_attempt(expected)
            raise
        cid = discover_attempt(expected)
    if not cid:
        raise RuntimeError("created container identity is not yet observable")
    return cid


def require_absent(cids: list[str]) -> None:
    """Independent Docker proof that predecessors are gone (not gate flags)."""
    for cid in cids:
        if observe_exact(cid) is not None:
            raise RuntimeError("a predecessor container still exists; the next boot cannot open")


def predecessors(transaction: dict, role: str) -> list[str]:
    cids = [transaction["source_container"]]
    if role == "rollback":
        cids += [r["cid"] for r in journal_attempts(transaction, "target") if r["cid"]]
    return cids


def retire_attempt(config_value: dict, transaction: dict, gate, role: str, record: dict,
                   attempt: dict, *, remove_admitted: bool) -> bool:
    """Cancel (or, during rollback, remove) one journaled attempt.

    Returns False when the wrapper already admitted it and the caller must
    observe rather than cancel. CLOSE is durable before the tombstone, and
    the tombstone before any exact removal or nil-container fence.
    """
    token = record["token"]
    admitted = attempt["state"] == "admitted" or not gate.close_pending(role, token)
    if admitted and not remove_admitted:
        return False
    retire_token(token)
    cid = record["cid"]
    if cid is None:
        if admitted:
            raise AttemptIdentityMismatch("admitted attempt has no journaled container")
        # Never issued (issue requires a bound container): no Start exists.
        # A still-pending no-start Create stays inert behind this CLOSED gate
        # and its tombstone; a later creation boundary removes it exactly.
        gate.fenced(role, token, None)
        return True
    if attempt["cid"] is None and not admitted:
        # A crash can leave the root journal ahead of gate.bind. Complete that
        # exact identity on the CLOSED attempt before fencing it, including
        # replay after its container was already removed. Never reopen it.
        gate.bind(role, token, cid)
    item = observe_exact(cid)
    if item is not None:
        verify_attempt_item(item, cid, journaled_expected(record))
        if role == "target":
            evidence = capture_failed_target(transaction["operation_id"], cid, record["image"])
            try:
                write_status(config_value, evidence_capture=evidence)
            except OSError:
                pass  # diagnostics cannot hold service recovery hostage
        remove_exact_container(cid)
    if admitted:
        gate.quiesce(cid)
    else:
        gate.fenced(role, token, cid)
    return True


def new_attempt(config_value: dict, transaction: dict, gate, role: str) -> dict:
    """Immutable attempt file, then root journal, then gate allocation."""
    records = journal_attempts(transaction, role)
    image = transaction["target_image" if role == "target" else "previous_image"]
    token = uuid.uuid4().hex
    raw = write_attempt_file(wrapped_configuration(config_value, transaction, role, token, image))
    record = {"role": role, "generation": len(records) + 1, "token": token, "image": image,
              "attempt_sha256": hashlib.sha256(raw).hexdigest(), "cid": None}
    transaction.setdefault("attempts", []).append(record)
    write_transaction(transaction)
    gate.allocate(role, token, image)
    return record


def prepare_attempt(config_value: dict, transaction: dict, role: str) -> str:
    """Allocate/create/bind/start once; never repeat an ambiguous start.

    Identities come from the root journal; the container-writable gate is
    cross-checked first and only arbitrates admission versus cancellation.
    Only a durably CLOSED attempt is removed, by its journaled exact ID.
    """
    gate = admission_store(config_value, transaction)
    state = checked_gate(gate, transaction)
    records = journal_attempts(transaction, role)
    record = records[-1] if records else None
    if record is not None and len(state["slots"][role]["attempts"]) < record["generation"]:
        gate.allocate(role, record["token"], record["image"])  # journal led the gate
        state = checked_gate(gate, transaction)
    slot = state["slots"][role]
    if slot["consumed"]:
        return record["cid"]
    image = transaction["target_image" if role == "target" else "previous_image"]
    if record is not None:
        attempt = slot["attempts"][-1]
        if attempt["issued"] or attempt["state"] == "closed":
            if not retire_attempt(config_value, transaction, gate, role, record, attempt,
                                  remove_admitted=False):
                return record["cid"]  # the wrapper won admission; do not touch it
            record = None
    if record is None:
        record = new_attempt(config_value, transaction, gate, role)
    # Docker, not gate flags, proves every predecessor is gone before this
    # generation may take the stable name or open.
    require_absent(predecessors(transaction, role))
    raw, expected = journaled_attempt(record)
    _durable_write(OVERRIDE, raw, 0o600)  # live pointer: exact immutable attempt bytes
    cid = record["cid"]
    if cid is None:
        cid = create_attempt(config_value, expected)
        record["cid"] = cid
        write_transaction(transaction)
    else:
        verify_wrapped_container(cid, expected)
    gate.bind(role, record["token"], cid)
    if not never_started(cid, image):
        # A wrapper invocation before OPEN was denied. Do not infer admission
        # or reuse that process; only explicit closed cancellation may retry.
        attempt = checked_gate(gate, transaction)["slots"][role]["attempts"][-1]
        retire_attempt(config_value, transaction, gate, role, record, attempt, remove_admitted=False)
        raise RuntimeError("pre-admission container already ran; cancellation recorded")
    gate.open(role, record["token"])
    if not gate.issue_start(role, record["token"]):
        raise RuntimeError("start was already issued; reconcile its admission")
    docker_command(["docker", "start", cid], timeout=COMPOSE_MUTATION_SECONDS)
    return cid


def quiesce_target(config_value: dict, transaction: dict) -> None:
    """Retire every journaled target before granting the separate rollback slot."""
    gate = admission_store(config_value, transaction)
    state = checked_gate(gate, transaction)
    records = journal_attempts(transaction, "target")
    if records and len(state["slots"]["target"]["attempts"]) < records[-1]["generation"]:
        gate.allocate("target", records[-1]["token"], records[-1]["image"])
        state = checked_gate(gate, transaction)
    for record, attempt in zip(records, state["slots"]["target"]["attempts"]):
        retire_attempt(config_value, transaction, gate, "target", record, attempt,
                       remove_admitted=True)


def prepare_admission(config_value: dict, transaction: dict):
    gate = admission_store(config_value, transaction)
    # A missing receipt after a failed drain must not authorize removal of a
    # still-serviceable source. A completed drain or exact evidence that the
    # source is already down permits service-only restoration, never handoff.
    service_only = transaction.get("drain_confirmed") is True
    if not service_only:
        cid = transaction["source_container"]
        try:
            result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", cid],
                                    check=False)
            if result.returncode == 0:
                item = json.loads(result.stdout)
                service_only = (item.get("Id") == cid and item.get("Image") == transaction["previous_image"]
                                and item.get("State", {}).get("Status") in {"created", "exited", "dead", "restarting"})
            else:
                service_only = result.returncode == 1 and result.stderr.strip().lower() in {
                    f"error response from daemon: no such container: {cid}",
                    f"error response from daemon: no such object: {cid}"}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Unknown source state never grants service-only cutover.
    gate.prepare_for_recovery(transaction["operation_id"], transaction["source_container"],
                              transaction["previous_image"], allow_service_only=service_only)
    state = checked_gate(gate, transaction)
    source = transaction["source_container"]
    source_token = transaction.get("source_token")
    expected = None
    if source_token is not None:
        retire_token(source_token)  # the previous live attempt; never current again
        _, expected = read_attempt_file(source_token)
    item = observe_exact(source)
    if item is not None:
        if expected is not None:
            verify_attempt_item(item, source, expected)
        elif item.get("Image") != transaction["previous_image"]:
            raise AttemptIdentityMismatch("the journaled source changed identity")
        remove_exact_container(source)
    if source not in state["quiesced"]:
        gate.quiesce(source)
    return gate


def prepare_rollback(config_value: dict, transaction: dict) -> str:
    if transaction.get("admission_version") != 1:
        raise RuntimeError("unwrapped legacy rollback requires explicit Host recovery")
    prepare_admission(config_value, transaction)
    quiesce_target(config_value, transaction)
    return prepare_attempt(config_value, transaction, "rollback")


def rollback(config_value: dict, operation: str, expected: str,
             code: str, detail: str, previous_image: str | None = None) -> int:
    transaction = read_transaction()
    if (transaction is None or transaction["operation_id"] != operation
            or transaction["expected_sha"] != expected
            or (previous_image is not None and previous_image != transaction["previous_image"])):
        raise RuntimeError("rollback does not own the replacement transaction")
    if transaction.get("outcome"):
        promote_topology(transaction)
        write_status(config_value, **transaction["outcome"])
        clear_transaction()
        return 0 if transaction["outcome"]["state"] == "succeeded" else 1
    # Preserve the original failure, except when later evidence resolves an
    # earlier unknown observation into a definite replacement failure.
    if not (transaction.get("failure_code") == "observation_unconfirmed"
            and code in {"replacement_failed", "readiness_budget_exhausted"}):
        code = transaction.get("failure_code", code)
        detail = transaction.get("failure_detail", detail)
    transaction.update(failure_code=code, failure_detail=detail)
    write_transaction(transaction)
    if transaction.get("admission_version") != 1:
        # Observe an old already-running recovery, but never retrofit a gate
        # to an issued unwrapped start or reconstruct its consumed authority.
        cid, image, health = container_health(config_value)
        if (transaction.get("phase") != "replacement_started"
                and image == transaction["previous_image"] and health == "healthy"):
            finish_verified(config_value, transaction, cid, image, state="rolled_back",
                            code=code, message=f"The previous container was restored: {detail}")
        else:
            status_code = ("rollback_wrong_image" if image and
                           (image not in {transaction["previous_image"], transaction.get("target_image")}
                            or (transaction.get("phase") == "rollback_started"
                                and image != transaction["previous_image"]))
                           else "legacy_admission_unconfirmed")
            write_status(config_value, state="needs_recovery", code=status_code,
                         message="An unwrapped interrupted replacement needs explicit Host recovery; "
                                 "its restart permission cannot be reconstructed.")
        return 1
    transaction["phase"] = "rollback_started"
    write_transaction(transaction)
    write_status(config_value, operation_id=operation, state="verifying", expected_sha=expected,
                 request_nonce=transaction.get("request_nonce"), code=code,
                 message="Replacement failed; restoring the previous container.")
    cid = prepare_rollback(config_value, transaction)
    if wait_healthy(config_value, ROLLBACK_HEALTH_SECONDS):
        current_cid, image, health = container_health(config_value)
        if current_cid != cid or image != transaction["previous_image"] or health != "healthy":
            raise RuntimeError("rollback identity or readiness changed")
        finish_verified(config_value, transaction, cid, image, state="rolled_back",
                        code=code, message=f"The previous container was restored: {detail}")
    else:
        write_status(config_value, state="needs_recovery", code="rollback_failed",
                     message="The admitted rollback is not yet healthy; recovery will observe it.")
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
    """Activate trusted checkout bytes without downgrading or retrying a trial.

    A still-pending exact candidate has never been tried: the launcher removes
    it before a trial. Only that candidate may be activated at high_water.
    """
    revision, refusal = _checked(source)
    if refusal:
        return refusal
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        index = json.loads(WORKER_INDEX.read_text(encoding="utf-8"))
    except FileNotFoundError:
        index = {"version": 1, "high_water": 0, "active": None, "candidate": None}
    digest = hashlib.sha256(source).hexdigest()
    active = index.get("active") or {}
    candidate = index.get("candidate") or {}
    high_water = int(index.get("high_water", 0))
    if active.get("sha256") == digest:
        return f"current: revision {revision}"
    if int(active.get("revision", 0)) > revision:
        return f"kept: a newer worker (revision {active['revision']}) is installed"
    pending_exact = (
        revision == high_water
        and candidate.get("revision") == revision
        and candidate.get("sha256") == digest
    )
    if revision <= high_water and not pending_exact:
        return (f"rejected: revision {revision} was already tried or superseded; "
                "install a higher worker revision")
    entry = _worker_entry(source, revision, "checkout")
    _publish_index({**index, "high_water": max(high_water, revision), "active": entry,
                    "candidate": None})
    return f"installed: revision {revision}"


def adopt_self() -> int:
    """The installer's locked seed operation; success means a usable active worker."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        if os.environ.get("MOBIUS_REBUILD_LOCK_HELD") != "1":
            fcntl.flock(lock, fcntl.LOCK_EX)
        outcome = seed_worker(Path(__file__).read_bytes())
    print(outcome)
    return 0 if outcome.startswith(("installed:", "current:", "kept:")) else 1


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

    created = docker_command(
        ["docker", "create", "--network", "none", "--entrypoint", "/bin/false",
         "--label", "mobius-rebuild.worker-extract=1", image_id],
        check=True, timeout=30,
    )
    cid = created.stdout.strip()
    try:
        archive = _bounded_output(
            ["docker", "cp", f"{cid}:{WORKER_IN_IMAGE}", "-"],
            MAX_WORKER_BYTES + 64 * 1024, timeout=30,
        )
    finally:
        docker_command(["docker", "rm", "-f", "-v", cid], check=False, timeout=30)
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
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER")
    if launcher is not None and (not launcher.isdigit() or int(launcher) < 2):
        # A legacy launcher cannot retain this trial's recovery owner. Leave
        # both status and request untouched: its unchanged-status success path
        # puts the candidate back instead of consuming the one-shot trial.
        print("Reinstall the reviewed host helper before another replacement; "
              "launcher revision 2 is required.", file=sys.stderr)
        return 0
    if not ADMISSION_CODE.is_file() or not ADMISSION_ROOT.is_dir():
        print("Reinstall the reviewed host helper: boot admission revision 5 is required.", file=sys.stderr)
        return 0
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
    lock = LOCK.open("a+")
    try:
        acquire_lock(lock)
    except BlockingIOError:
        lock.close()
        # The lock owner owns the shared status too. Leave both its operation
        # and this still-queued request untouched.
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
            docker_command(["docker", "pull", image_ref], timeout=3600)
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
                except ProvenanceRejected as exc:
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
            docker_command(["docker", "tag", previous, ROLLBACK_TAG], timeout=30)
            transaction = transaction_record(operation, expected, nonce,
                                             previous, digest)
            transaction.update(admission_version=1, source_container=cid)
            return execute_replacement(config_value, transaction)
        except ProvenanceUnconfirmed:
            write_status(config_value, operation_id=operation,
                         state="needs_recovery" if TRANSACTION.exists() else "failed",
                         expected_sha=expected, code="observation_unconfirmed",
                         message="Target provenance could not be observed; no outcome was inferred.")
            return 1
        except subprocess.TimeoutExpired:
            write_status(config_value, state="needs_recovery" if TRANSACTION.exists() else "failed",
                         code="observation_timed_out", message="Docker did not finish in time.")
            return 1
        except Exception as exc:
            detail = str(exc)[:300]
            if TRANSACTION.exists():
                # Drain may have accepted a one-shot boot authorization even
                # when its response (or the following status write) failed.
                # The source still running does not prove cancellation; keep
                # recovery ownership without pretending replacement started.
                write_status(config_value, operation_id=operation,
                             state="needs_recovery", expected_sha=expected,
                             code="replacement_failed", message=detail)
                return 1
            if image_ref and pulled_recorded:
                discard_pulled_image(image_ref)
            write_status(config_value, operation_id=operation, state="failed",
                         expected_sha=expected, code="replacement_failed", message=detail)
            return 1
        finally:
            # Only the verified claim is ever removed; whatever is still in the
            # inbox may be a newer request and stays for the next run or withdrawal.
            if claim_verified:
                _discard_claim(claimed)


def execute_replacement(config_value: dict, transaction: dict) -> int:
    """The shared official/manual cutover owner; caller holds replace.lock.

    Every recoverable input, including manual topology, is durable before
    drain. Exceptions retain the journal: only reconciled evidence can settle
    an ambiguous Docker response. No shell caller may perform its own rollback.
    """
    if TRANSACTION.exists():
        raise RuntimeError("an unresolved replacement already owns the host")
    operation = transaction["operation_id"]
    expected = transaction["expected_sha"]
    previous, target = transaction["previous_image"], transaction["target_image"]
    cid = transaction["source_container"]
    admission_store(config_value, transaction)
    for role, image in (("target", target), ("rollback", previous)):
        wrapped_configuration(config_value, transaction, role, uuid.uuid4().hex, image)
    # Bind a wrapped source to its root-private attempt record before drain;
    # its token is retired at removal so a late duplicate Create stays inert.
    transaction.setdefault("attempts", [])
    transaction["source_token"] = None
    current = current_attempt(config_value)
    if current is not None:
        held = name_holder(config_value["deployment_name"])
        if held is None or held[0] != cid or classify_holder(held[1], cid, current) != "current":
            raise RuntimeError("the source is not the recorded current admission attempt")
        transaction["source_token"] = current["services"]["app"]["labels"]["io.mobius.admission.token"]
    journal_attempts(transaction)
    write_transaction(transaction)
    try:
        request_drain(config_value, operation, cid)
        transaction["drain_confirmed"] = True
        transaction["phase"] = "replacement_started"
        write_transaction(transaction)
        write_status(config_value, operation_id=operation, state="replacing", expected_sha=expected,
                     request_nonce=transaction.get("request_nonce"), code=None,
                     message="Replacing the container under boot admission control.")
        prepare_admission(config_value, transaction)
        prepare_attempt(config_value, transaction, "target")
        if not wait_healthy(config_value):
            return rollback(config_value, operation, expected, "readiness_budget_exhausted",
                            "the new container was not serviceable within the readiness budget", previous)
        cid, current = app_container(config_value)
        if current != target:
            raise RuntimeError("the new container does not run the verified image")
        verify_served_generation(cid, expected)
        return 0 if finish_verified(config_value, transaction, cid, target,
                                    state="succeeded", code=None,
                                    message="Container rebuilt successfully.") else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        code = "observation_unconfirmed"
        if isinstance(exc, ProvenanceRejected):
            code = "replacement_failed"
            transaction.update(failure_code=code, failure_detail=str(exc)[:300])
            write_transaction(transaction)
        write_status(config_value, operation_id=operation, state="needs_recovery", expected_sha=expected,
                     request_nonce=transaction.get("request_nonce"), code=code,
                     message=f"The replacement remains journaled for recovery: {str(exc)[:180]}")
        return 1


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


def cutover_boot_consumed(config_value: dict, operation: str, *,
                          trusted_uid: int = 0, trusted_gid: int = 0,
                          expected_boot_id: str | None = None) -> bool:
    """Read-only proof that this boot consumed the exact cutover authorization.

    A receipt alone is inert, but accepted.json can authorize a future boot.
    Never settle an ambiguous source boot merely because its image is healthy.
    """
    path = config_value["data_dir"] / ".restart-ledger"
    directory = None
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if (info.st_uid != trusted_uid or info.st_gid != trusted_gid
                or info.st_mode & 0o022):
            return False

        def read(name: str) -> bytes:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                item = os.fstat(fd)
                if (not stat.S_ISREG(item.st_mode) or item.st_size > 65536
                        or item.st_uid != trusted_uid or item.st_gid != trusted_gid
                        or item.st_mode & 0o022):
                    raise ValueError("untrusted cutover evidence")
                raw = os.read(fd, 65537)
                if len(raw) > 65536:
                    raise ValueError("oversized cutover evidence")
                return raw
            finally:
                os.close(fd)

        def accepted_absent() -> bool:
            try:
                os.stat("accepted.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return True
            return False

        if not accepted_absent():
            return False
        boot = read("boot-id").decode().strip()
        ack = json.loads(read("ack.json"))
        token = r"[A-Za-z0-9._:-]{8,160}"
        if (not isinstance(ack, dict) or ack.get("version") != 1
                or ack.get("action") != "external_cutover"
                or ack.get("cutover_id") != operation
                or ack.get("target_boot_id") != boot
                or (expected_boot_id is not None and boot != expected_boot_id)
                or not re.fullmatch(token, boot)
                or not isinstance(ack.get("nonce"), str)
                or not re.fullmatch(token, ack["nonce"])
                or not isinstance(ack.get("source_boot_id"), str)
                or not re.fullmatch(token, ack["source_boot_id"])
                or ack.get("source_boot_id") == boot):
            return False
        receipt_absent = False
        try:
            receipt = json.loads(read("cutover-receipt.json"))
        except FileNotFoundError:
            receipt_absent = True  # a prior finalize may already have retired it
            receipt = None
        if not receipt_absent and (
                not isinstance(receipt, dict) or receipt.get("version") != 1
                or receipt.get("action") != "external_cutover"
                or receipt.get("cutover_id") != operation
                or receipt.get("nonce") != ack["nonce"]):
            return False
        after = path.lstat()
        return (accepted_absent() and read("boot-id").decode().strip() == boot
                and (after.st_dev, after.st_ino) == (info.st_dev, info.st_ino))
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        if directory is not None:
            os.close(directory)


def finish_verified(config_value: dict, transaction: dict, cid: str, image: str,
                    *, state: str, code: str | None, message: str) -> bool:
    """Commit service success before receipt retirement or optional housekeeping.

    Finalization and the journal cannot be atomic together. A crash in between
    therefore replays an honest degraded outcome, never authorizes another boot.
    """
    expected_boot_id = None
    if transaction.get("admission_version") == 1:
        gate_state = checked_gate(admission_store(config_value, transaction), transaction)
        role = "target" if state == "succeeded" else "rollback"
        slot = gate_state["slots"][role]
        records = journal_attempts(transaction, role)
        if (not slot["consumed"] or len(records) != len(slot["attempts"])
                or records[-1]["cid"] != cid):
            raise RuntimeError("healthy container has no matching admission")
        expected_boot_id = slot["consumed"]["boot_id"]
    if not cutover_boot_consumed(config_value, transaction["operation_id"],
                                 expected_boot_id=expected_boot_id):
        write_status(config_value, state="needs_recovery", code="handoff_boot_unconfirmed",
                     message="The container is healthy, but this boot has not proven consumption of "
                             "the exact handoff. Keep the transaction for manual Host recovery.")
        return False
    transaction["outcome"] = {
        "state": state, "code": "handoff_finalize_unconfirmed",
        "message": ("Exact chat handoff finalization is unconfirmed; check affected chats; "
                    f"manual Resume may be needed. {message}")[:300],
        "operation_id": transaction["operation_id"],
        "expected_sha": transaction["expected_sha"],
        "request_nonce": transaction.get("request_nonce"),
        "failure_code": transaction.get("failure_code"),
        "failure_detail": transaction.get("failure_detail"),
    }
    write_transaction(transaction)
    try:
        finalized = restart_ledger(config_value, cid, "finalize-cutover",
                                   transaction["operation_id"], image=image)
    except Exception:
        finalized = False
    if finalized:
        transaction["outcome"].update(code=code, message=message[:300])
        write_transaction(transaction)
    if state == "succeeded":
        # These are best-effort after the service outcome is durable. Their
        # failure cannot turn an already verified replacement into a rollback.
        try:
            retain_images(f"{IMAGE}:sha-{transaction['expected_sha']}",
                          transaction["previous_image"])
            transaction["outcome"]["worker_adoption"] = adopt_from_image(image)
        except Exception:
            transaction["outcome"]["worker_adoption"] = "not adopted: image housekeeping failed"
    settle_transaction(config_value, transaction, **transaction["outcome"])
    return True


def promote_topology(transaction: dict) -> None:
    if transaction.get("outcome", {}).get("state") == "succeeded" and transaction.get("target_topology"):
        _durable_write(COMPOSE, json.dumps(transaction["target_topology"]).encode(), 0o600)


def settle_transaction(config_value: dict, transaction: dict, **outcome) -> None:
    transaction["outcome"] = {
        **outcome, "operation_id": transaction["operation_id"],
        "expected_sha": transaction["expected_sha"],
        "request_nonce": transaction.get("request_nonce"),
    }
    write_transaction(transaction)
    promote_topology(transaction)
    write_status(config_value, **transaction["outcome"])
    clear_transaction()


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
    """Settle this operation, observing an existing boot rather than replaying it."""
    operation = transaction["operation_id"]
    expected = transaction["expected_sha"]
    fields = {"request_nonce": transaction.get("request_nonce")}
    # Older workers kept the failure only in status. Adopt it only when it
    # belongs to this journal, never from a later queued request.
    if "failure_code" not in transaction:
        try:
            status = read_json(STATUS)
        except (OSError, ValueError):
            status = {}
        if status.get("operation_id") == operation and status.get("state") == "needs_recovery":
            transaction.update(failure_code=status.get("code") or "worker_interrupted",
                               failure_detail=status.get("message") or "the replacement worker stopped")
            write_transaction(transaction)
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=transaction.get("failure_code"), **fields,
                 message="Recovering an interrupted replacement.")
    # A completed worker can be interrupted between publishing its outcome
    # and removing the journal. Reconciliation must not undo that success.
    outcome = transaction.get("outcome")
    if outcome:
        promote_topology(transaction)
        write_status(config_value, **outcome)
        clear_transaction()
        return
    previous = transaction["previous_image"]
    try:
        cid, current, health = container_health(config_value)
        # A config-only manual cutover can retain the same image. The old
        # source is never evidence of a completed target admission.
        if (transaction.get("phase") != "rollback_started"
                and cid != transaction.get("source_container")
                and current == transaction.get("target_image")):
            if health in {"starting", "unhealthy", "running", "restarting"}:
                if wait_healthy(config_value):
                    cid, current, health = container_health(config_value)
                else:
                    rollback(config_value, operation, expected,
                             "readiness_budget_exhausted",
                             "the interrupted replacement was not serviceable within the readiness budget",
                             previous)
                    return
            if health == "healthy" and current == transaction["target_image"]:
                try:
                    verify_served_generation(cid, expected)
                except ProvenanceRejected as exc:
                    rollback(config_value, operation, expected,
                             "replacement_failed", str(exc)[:300], previous)
                    return
                except (ProvenanceUnconfirmed, OSError, ValueError, subprocess.SubprocessError):
                    write_status(config_value, operation_id=operation,
                                 state="needs_recovery", expected_sha=expected,
                                 code="observation_unconfirmed", **fields,
                                 message="Target provenance could not be observed; recovery is still needed.")
                    return
                finish_verified(config_value, transaction, cid, current,
                                state="succeeded", code=None,
                                message="Container rebuilt successfully.")
                return
        rollback(config_value, operation, expected, "worker_interrupted",
                 "the replacement worker stopped before it finished", previous)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        write_status(config_value, operation_id=operation, state="needs_recovery",
                     expected_sha=expected, code="rollback_failed", **fields,
                     message=f"Recovery could not finish: {str(exc)[:200]}")


def publish_capabilities() -> int:
    """Installer publication only: no requests, Docker, drains or recovery."""
    config_value = config()
    with LOCK.open("a+") as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            return 1
        if TRANSACTION.exists():
            return 1
        try:
            current = read_json(STATUS)
        except FileNotFoundError:
            current = {}
        if current.get("state") in ACTIVE_STATES:
            return 1
        # write_status preserves the exact settled outcome while refreshing
        # only the capability fields. A racing operation owns the same lock.
        write_status(config_value)
    return 0


def reconcile() -> int:
    config_value = config()
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Status and the journal belong to the lock owner. A previous owner
        # may have settled and cleared the journal before we acquired it.
        try:
            current = read_json(STATUS)
        except (OSError, ValueError, json.JSONDecodeError):
            current = None
        # A hard power loss can strand the already-claimed request before or
        # after the first status write. Once no worker owns the lock, it is no
        # longer runnable and must not accumulate in the root-controlled area.
        for claimed in config_value["control_dir"].glob(".request-*.json"):
            if re.fullmatch(r"\.request-[0-9a-f]{32}\.json", claimed.name):
                _discard_claim(claimed)
        # Extraction containers are never started; one left by a killed
        # worker only holds a reference to its image.
        try:
            leftovers = docker_command(
                ["docker", "ps", "-aq", "--filter",
                 "label=mobius-rebuild.worker-extract=1"],
                check=False, timeout=5,
            ).stdout.split()
            if leftovers:
                docker_command(["docker", "rm", "-f", "-v", *leftovers],
                               check=False, timeout=5)
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


def data_source(cid: str) -> int:
    """Installer discovery: the live app's stable /data and Docker's view of it."""
    if not FULL_CID_RE.fullmatch(cid):
        print("invalid container id", file=sys.stderr)
        return 2
    mounts = json.loads(docker_command(
        ["docker", "container", "inspect", "--format", "{{json .Mounts}}", cid], timeout=30,
    ).stdout) or []
    data = [mount for mount in mounts if mount.get("Destination") == "/data"]
    if len(data) != 1 or not data[0].get("RW"):
        print("the app must have exactly one writable /data mount", file=sys.stderr)
        return 1
    mount = data[0]
    try:
        stable = persistent_data_dir(
            mount.get("Type"), mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source"))
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"data_dir": stable, "mount_source": mount.get("Source")},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "data-source" and os.geteuid() == 0:
        raise SystemExit(data_source(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "publish-capabilities" and os.geteuid() == 0:
        raise SystemExit(publish_capabilities())
    if len(sys.argv) == 2 and sys.argv[1] == "run" and os.geteuid() == 0:
        raise SystemExit(run())
    if len(sys.argv) == 2 and sys.argv[1] == "reconcile" and os.geteuid() == 0:
        raise SystemExit(reconcile())
    if len(sys.argv) == 2 and sys.argv[1] == "adopt-self" and os.geteuid() == 0:
        # The installer holds the replacement lock for its whole installation;
        # a standalone invocation takes it inside adopt_self().
        raise SystemExit(adopt_self())
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
