#!/usr/bin/env python3
"""Disposable hosted proof: real Compose, Docker, boot gate, and frozen ledger.

Only source-only provenance/adoption work is stubbed. This runs no Docker on a
production Host and makes no registry writes. The proxies relay real daemon
traffic byte for byte and apply one schedule rule each; they never emulate
Docker state or answer on the daemon's behalf:

* ResponseBarrier forwards one exact request and withholds its 2xx response,
  so the daemon HAS performed the operation when the CLI times out.
* CreateRequestBarrier withholds the stable-name Create REQUEST itself, so the
  daemon has NOT seen it when the CLI times out and is killed. After the real
  host durably fences that attempt, deliver() sends the identical captured
  bytes on a fresh daemon connection: the daemon completing an abandoned
  Create late. This is one modelled delay (request held in transit), not every
  daemon-side failure or reordering.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
DOCKER_SOCKET = "/var/run/docker.sock"
NEVER_STARTED = "0001-01-01T00:00:00Z"
MUTATING = {"rm", "start", "stop", "kill", "restart", "rename", "pause", "unpause",
            "update", "create", "run", "up", "down"}


def command(*args, timeout=60, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=timeout, **kwargs)


def mounted(path):
    """Kernel mount table, not os.path.ismount: Docker's bind-option volume
    mount shares the root filesystem with its Mountpoint on hosted runners."""
    target = os.path.realpath(path)
    with open("/proc/self/mountinfo", encoding="utf-8") as table:
        return any(line.split()[4].replace("\\040", " ") == target for line in table)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def eventually(predicate, seconds=20):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError("disposable Docker condition did not become true")


def await_admitted_boot(host, config, gate, role, operation, source_boot, nonce, seconds=30):
    """Wait for the admitted boot's legacy begin-boot, then prove its handoff once.

    The gate durably consumes its slot BEFORE exec'ing the legacy entrypoint,
    whose begin-boot retires accepted.json, writes ack.json and replaces
    boot-id LAST. Only those causal gaps are waited out: boot-id still naming
    the source boot. Any other boot, a changed consumption, a missing/unreadable
    ledger file or a proof mismatch fails at once instead of being retried.
    """
    boot_path = Path(config["data_dir"]) / ".restart-ledger" / "boot-id"
    deadline, seen = time.monotonic() + seconds, None
    while True:
        # boot-id before the gate: a committed boot implies a prior consumption.
        current = boot_path.read_text().strip()
        consumed = gate.observe()["slots"][role]["consumed"]
        if seen is not None and consumed != seen:
            raise AssertionError("the gate's consumed admission changed")
        seen = consumed
        if consumed is not None and current == consumed["boot_id"]:
            break
        if current != source_boot:
            raise AssertionError("the ledger committed a boot other than the admitted one")
        if time.monotonic() >= deadline:
            raise AssertionError(f"the {role} admission was not consumed and committed "
                                 f"by legacy begin-boot within {seconds}s")
        time.sleep(.2)
    if not host.cutover_boot_consumed(config, operation, expected_boot_id=consumed["boot_id"]):
        raise AssertionError("the admitted boot did not prove the exact cutover acknowledgement")
    ack = json.loads((boot_path.parent / "ack.json").read_bytes())
    if (ack.get("nonce"), ack.get("source_boot_id")) != (nonce, source_boot):
        raise AssertionError("the acknowledgement names another handoff")
    return consumed


def http_message(buffer, *, eof=False):
    """One complete HTTP/1.x message at the start of buffer, else None.

    Returns (start line, lowercase header fields, decoded body, wire length).
    Proof traffic is small; anything beyond the bound is an error, not data.
    """
    end = buffer.find(b"\r\n\r\n")
    if end < 0:
        if len(buffer) > 65536:
            raise AssertionError("HTTP header exceeds the proof bound")
        return None
    start, *lines = buffer[:end].split(b"\r\n")
    fields = {}
    for line in lines:
        name, colon, value = line.partition(b":")
        if not colon:
            raise AssertionError(f"malformed HTTP header {line[:80]!r}")
        fields[name.strip().lower()] = value.strip()
    offset = end + 4
    if b"chunked" in fields.get(b"transfer-encoding", b"").lower():
        body = b""
        while True:
            line_end = buffer.find(b"\r\n", offset)
            if line_end < 0:
                return None
            size = int(buffer[offset:line_end].split(b";")[0], 16)
            offset = line_end + 2
            if size == 0:
                trailer = buffer.find(b"\r\n\r\n", offset - 2)
                return None if trailer < 0 else (start, fields, body, trailer + 4)
            if len(buffer) < offset + size + 2:
                return None
            if buffer[offset + size:offset + size + 2] != b"\r\n":
                raise AssertionError("malformed HTTP chunk")
            body += buffer[offset:offset + size]
            offset += size + 2
    if b"content-length" in fields:
        size = int(fields[b"content-length"])
        if size > 1024 * 1024:
            raise AssertionError("HTTP body exceeds the proof bound")
        if len(buffer) < offset + size:
            return None
        return start, fields, buffer[offset:offset + size], offset + size
    status = re.match(rb"HTTP/1\.[01] ([0-9]{3})", start)
    if status is None or status[1][:1] == b"1" or status[1] in {b"204", b"304"}:
        return start, fields, b"", offset  # requests and bodiless responses
    if not eof:
        return None  # a close-delimited response body
    return start, fields, buffer[offset:], len(buffer)


class DockerProxy:
    """Byte-transparent Unix-socket relay to the real daemon with one rule.

    Every relay failure is collected and re-raised in the scenario thread
    (check() or exit); a killed CLI's broken pipe is the only ignored error.
    Threads are bounded by a per-connection deadline and joined on exit.
    """

    connection_seconds = 60

    def __init__(self, path, upstream=DOCKER_SOCKET):
        self.path, self.upstream = Path(path), str(upstream)
        self.stop = threading.Event()
        self.guard = threading.Lock()
        self.errors, self.threads, self.sockets = [], [], []
        self.listener = None

    def __enter__(self):
        if not Path(self.upstream).is_socket():
            raise RuntimeError("disposable runner lacks the local Docker Unix socket")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(32)
        self.listener.settimeout(.2)
        self._spawn(self._accept)
        return self

    def _spawn(self, target, *args):
        thread = threading.Thread(target=self._guarded, args=(target, *args), daemon=True,
                                  name=f"docker-proxy{target.__name__}")
        with self.guard:
            self.threads.append(thread)
        thread.start()

    def _guarded(self, target, *args):
        try:
            target(*args)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the bounded Docker CLI was killed mid-exchange
        except BaseException as exc:
            if not (self.stop.is_set() and isinstance(exc, (OSError, ValueError))):
                with self.guard:
                    self.errors.append(exc)

    def _track(self, sock):
        with self.guard:
            self.sockets.append(sock)
        return sock

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            self._spawn(self._serve, self._track(client))

    def _serve(self, client):
        daemon = self._track(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        try:
            daemon.connect(self.upstream)
            self._relay(client, daemon, time.monotonic() + self.connection_seconds)
        finally:  # a closed side always reaches the other peer
            daemon.close()
            client.close()

    def _relay(self, client, daemon, until):
        raise NotImplementedError

    def check(self):
        with self.guard:
            errors = list(self.errors)
        if errors:
            raise AssertionError(f"Docker proxy failed: {errors!r}") from errors[0]

    def __exit__(self, exc_type, *_):
        self.stop.set()
        try:
            if self.listener is not None:
                self.listener.close()
            with self.guard:
                threads = list(self.threads)
            for thread in threads:
                thread.join(timeout=2)
            with self.guard:
                sockets = list(self.sockets)
            for sock in sockets:  # unblock a stuck send without fd reuse races
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            for thread in threads:
                thread.join(timeout=2)
            for sock in sockets:
                sock.close()
        finally:
            self.path.unlink(missing_ok=True)
        leaked = [thread.name for thread in threads if thread.is_alive()]
        if leaked:
            with self.guard:
                self.errors.append(AssertionError(f"proxy threads leaked: {leaked}"))
        if exc_type is None:
            self.check()
        elif self.errors:
            print(f"Docker proxy also failed: {self.errors!r}", file=sys.stderr, flush=True)
        return False


class ResponseBarrier(DockerProxy):
    """Forward one exact Docker API request, then withhold its 2xx response."""

    def __init__(self, path, method, cid, action="", upstream=DOCKER_SOCKET):
        super().__init__(path, upstream)
        self.pattern = (method.encode() + rb" /(?:v[0-9.]+/)?containers/" +
                        re.escape(cid.encode()) +
                        (rb"/" + action.encode() if action else b"") + rb"(?:[? /]|\r|\n)")
        self.accepted = threading.Event()

    def _relay(self, client, daemon, until):
        watched, scan, held = False, b"", b""
        while not self.stop.is_set() and time.monotonic() < until:
            readable, _, _ = select.select([client, daemon], [], [], .2)
            if client in readable:
                data = client.recv(65536)
                if not data:
                    return
                scan = (scan + data)[-8192:]
                if re.search(self.pattern, scan):
                    watched = True
                daemon.sendall(data)
            if daemon in readable:
                data = daemon.recv(65536)
                if not data:
                    return
                if not watched:
                    client.sendall(data)
                elif not self.accepted.is_set():
                    held += data
                    if len(held) > 65536:
                        raise AssertionError("Docker response unexpectedly large")
                    if b"\r\n\r\n" in held:
                        status = held.split(b"\r\n", 1)[0]
                        if not re.match(rb"HTTP/1\.[01] 2[0-9][0-9]", status):
                            raise AssertionError(f"Docker operation failed: {status!r}")
                        self.accepted.set()
                # Everything after the watched request stays withheld.


class CreateRequestBarrier(DockerProxy):
    """Hold the stable-name Create REQUEST in transit, then deliver it late.

    The daemon never sees the request while its CLI is alive. Only after that
    CLI was killed may deliver() send the identical captured bytes, once, on
    a fresh daemon connection; the response proves the late Create landed.
    """

    def __init__(self, path, name, upstream=DOCKER_SOCKET):
        super().__init__(path, upstream)
        self.pattern = re.compile(rb"POST /(?:v[0-9.]+/)?containers/create\?(?:[^ ]*&)?name="
                                  + re.escape(name.encode()) + rb"(?:&[^ ]*)? HTTP/1\.[01]")
        self.captured, self.abandoned = threading.Event(), threading.Event()
        self.request = self.body = None
        self.delivered = False

    def _relay(self, client, daemon, until):
        pending, raw, holding, sources = b"", False, False, [client, daemon]
        while not self.stop.is_set() and time.monotonic() < until:
            readable, _, _ = select.select(sources, [], [], .2)
            if client in readable:
                data = client.recv(65536)
                if not data:
                    if holding:
                        self.abandoned.set()  # the CLI died awaiting its Create
                    return
                if holding:
                    continue  # nothing after the held Create reaches the daemon
                if raw:
                    daemon.sendall(data)
                    continue
                pending += data
                while not holding and (message := http_message(pending)) is not None:
                    start, fields, body, size = message
                    request, pending = pending[:size], pending[size:]
                    if self.pattern.fullmatch(start):
                        with self.guard:
                            if self.request is not None:
                                raise AssertionError("a second stable-name Create was sent")
                            self.request, self.body = request, body
                        holding = True
                        self.captured.set()
                    else:
                        daemon.sendall(request)
                        if fields.get(b"upgrade"):  # hijacked stream: no more HTTP
                            raw = True
                            daemon.sendall(pending)
                            pending = b""
                            break
            if daemon in readable:
                data = daemon.recv(65536)
                if not data:
                    if not holding:
                        return
                    sources = [client]  # keep waiting for the CLI to die
                    continue
                client.sendall(data)

    def deliver(self, timeout=30):
        """Send the captured Create unchanged after its CLI is gone; return its CID."""
        self.check()
        if self.request is None or not self.abandoned.is_set() or self.delivered:
            raise AssertionError("only an abandoned, undelivered captured Create may land late")
        self.delivered = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon:
            daemon.settimeout(timeout)
            daemon.connect(self.upstream)
            daemon.sendall(self.request)
            response, until = b"", time.monotonic() + timeout
            while (message := http_message(response)) is None:
                if time.monotonic() > until:
                    raise TimeoutError("the daemon did not answer the late Create")
                data = daemon.recv(65536)
                if not data:
                    message = http_message(response, eof=True)
                    if message is None:
                        raise AssertionError("the daemon closed before answering the late Create")
                    break
                response += data
        start, _, body, _ = message
        if not re.fullmatch(rb"HTTP/1\.[01] 201(?: .*)?", start):
            raise AssertionError(f"late Create failed: {start!r} {body[:300]!r}")
        cid = json.loads(body).get("Id")
        if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise AssertionError(f"late Create returned no exact ID: {body[:300]!r}")
        return cid


def main():
    if (os.environ.get("MOBIUS_DOCKER_RECOVERY_PROOF") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.geteuid() != 0):
        raise SystemExit("refusing outside explicitly opted-in root disposable hosted runner")
    command("docker", "info", timeout=15)
    import pytest
    from app import restart_ledger as platform_ledger
    from tests.test_restart_ledger import _bind, _load_supervisor

    host = load(ROOT / "scripts/mobius-rebuild-host.py", "recovery_docker_host")
    token = uuid.uuid4().hex[:10]
    image_base = f"mobius-recovery-proof-{token}"
    scenarios = []
    patch = pytest.MonkeyPatch()
    original_docker = host.docker_command
    original_wait = host.wait_healthy
    original_quiesce = host.quiesce_target

    def cleanup():
        for project in reversed(scenarios):
            found = set()
            for label in (f"com.docker.compose.project={project}",
                          f"io.mobius.admission.project={project}",
                          f"io.mobius.recovery-proof.project={project}"):
                found.update(subprocess.run(["docker", "ps", "-aq", "--filter",
                                             f"label={label}"], capture_output=True,
                                            text=True, timeout=15).stdout.split())
            if found:
                subprocess.run(["docker", "rm", "-f", "-v", *sorted(found)],
                               capture_output=True, timeout=30)
            subprocess.run(["docker", "network", "rm", f"{project}_default"],
                           capture_output=True, timeout=15)
            subprocess.run(["docker", "volume", "rm", "-f", f"{project}_data"],
                           capture_output=True, timeout=15)
        for role in ("source", "target"):
            subprocess.run(["docker", "image", "rm", "-f", f"{image_base}:{role}"],
                           capture_output=True, timeout=15)

    try:
        with tempfile.TemporaryDirectory(prefix=f"recovery-docker-{token}-") as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text('''FROM python:3.12-alpine
ARG ROLE
LABEL recovery-proof-role=${ROLE}
COPY restart_ledger.py /app/runtime/restart_ledger.py
STOPSIGNAL SIGKILL
ENTRYPOINT ["/bin/sh", "-c", "[ ${RESTART_BEFORE_BOOT:-0} != 1 ] || exit 2; python3 /app/runtime/restart_ledger.py begin-boot ${MOBIUS_BOOT_ID:-boot-$(cat /proc/sys/kernel/random/uuid)} && exec sleep 3600"]
HEALTHCHECK --interval=1s --timeout=1s --retries=2 CMD test -f /data/ready
''')
            (root / "restart_ledger.py").write_bytes(
                (ROOT / "backend/runtime/restart_ledger.py").read_bytes())
            for role in ("source", "target"):
                command("docker", "build", "-q", "--build-arg", f"ROLE={role}",
                        "-t", f"{image_base}:{role}", str(root), timeout=300)
            source, target = (command("docker", "image", "inspect", "-f", "{{.Id}}",
                                      f"{image_base}:{role}").stdout.strip()
                              for role in ("source", "target"))
            assert source != target
            gate_code = root / "mobius-boot-admission.py"
            gate_code.write_bytes((ROOT / "scripts/mobius-boot-admission.py").read_bytes())
            gate_code.chmod(0o644)

            def scenario(name, kind, *, bind_option_volume=False):
                # Proxies opened by a schedule close (and re-raise their relay
                # errors) at the end of the scenario that owns them.
                with contextlib.ExitStack() as stack:
                    run_scenario(name, kind, stack, bind_option_volume)

            def run_scenario(name, kind, stack, bind_option_volume):
                print(f"recovery Docker scenario: {name}", flush=True)
                project = f"recovery-{token}-{name}"
                scenarios.append(project)
                base = root / name
                base.mkdir()
                data, state = base / "data", base / "state"
                data.mkdir()
                state.mkdir()
                (data / "mobius-rebuild" / "inbox").mkdir(parents=True)
                admission_root = state / "admission"
                admission_root.mkdir(mode=0o700)
                compose_file, override = base / "compose.yml", base / "override.json"
                if bind_option_volume:
                    # Production's layout (2026-10-09/12): a named local
                    # volume bound to a separate disk. Docker mounts it at its
                    # Mountpoint only while a container uses it.
                    compose_file.write_text(f'''services:
  app:
    image: ${{MOBIUS_IMAGE}}
    restart: unless-stopped
    volumes:
      - app_data:/data
volumes:
  app_data:
    name: {project}_data
    driver: local
    driver_opts: {{type: none, o: bind, device: {data}}}
''')
                else:
                    compose_file.write_text('''services:
  app:
    image: ${MOBIUS_IMAGE}
    restart: unless-stopped
    volumes:
      - ./data:/data
''')
                override.write_text('{}\n')
                patch.setattr(host, "CONFIG", base / "config.v4.json")
                patch.setattr(host, "COMPOSE", compose_file)
                patch.setattr(host, "OVERRIDE", override)
                patch.setattr(host, "STATE_DIR", state)
                patch.setattr(host, "STATUS", state / "status.json")
                patch.setattr(host, "LOCK", state / "replace.lock")
                patch.setattr(host, "TRANSACTION", state / "transaction.json")
                patch.setattr(host, "FAILED_TARGET_LOG", state / "failed-target.json")
                patch.setattr(host, "ADMISSION_CODE", gate_code)
                patch.setattr(host, "ADMISSION_ROOT", admission_root)
                patch.setattr(host, "COMPOSE_MUTATION_SECONDS", 4)
                patch.setattr(host, "ROLLBACK_HEALTH_SECONDS", 12)
                patch.setattr(host, "wait_healthy", lambda c, timeout=12: original_wait(c, timeout))
                patch.setattr(host, "verify_served_generation", lambda *_: None)
                patch.setattr(host, "retain_images", lambda *_: None)
                patch.setattr(host, "adopt_from_image", lambda *_: "not part of proof")
                calls = []

                def traced(args, **kwargs):
                    calls.append(list(args))  # observation only
                    return original_docker(args, **kwargs)

                patch.setattr(host, "docker_command", traced)
                patch.setattr(host, "quiesce_target", original_quiesce)
                failures = []  # hook errors that recover() would otherwise swallow
                config = {"version": 4, "project": project, "data_dir": data,
                          "deployment_name": f"{project}-app-1", "name_source": "generated",
                          "control_dir": data / "mobius-rebuild"}
                patch.setattr(host, "config", lambda: config)
                host.compose(config, "up", "-d", "--no-build", "app", image=source, timeout=30)
                source_cid, source_image, _ = host.container_health(config)
                assert source_image == source
                config["deployment_name"] = command("docker", "inspect", "-f", "{{.Name}}",
                                                    source_cid).stdout.strip().removeprefix("/")
                mountpoint = None
                if bind_option_volume:
                    mountpoint = command("docker", "volume", "inspect", "-f", "{{.Mountpoint}}",
                                         f"{project}_data").stdout.strip()
                    assert mounted(mountpoint) and mountpoint != str(data)
                    # The installer's resolver names the stable device.
                    assert host.persistent_data_dir("volume", f"{project}_data") == os.path.realpath(data)
                ledger = _load_supervisor()
                _bind(ledger, data, patch)
                eventually(lambda: ledger.BOOT_PATH.exists())
                source_boot = ledger.BOOT_PATH.read_text().strip()
                operation, nonce = uuid.uuid4().hex, uuid.uuid4().hex
                now = time.time()
                assert ledger.open_cutover(operation, now=now)
                platform_ledger.publish_cutover_intent(boot_id=source_boot, nonce=nonce,
                    cutover_id=operation, runs=[], now=now)
                assert ledger.accept_cutover(operation, now=now)
                tx = host.transaction_record(operation, "a" * 40, nonce, source, target)
                tx.update(phase="replacement_started", source_container=source_cid)
                if kind != "legacy":
                    tx["admission_version"] = 1
                host.write_transaction(tx)
                accepted = ledger.ACCEPTED_PATH.read_bytes()
                (data / "ready").touch()

                if kind == "legacy":
                    # The old incident is still observable, but an unwrapped
                    # transaction now refuses automatic adoption/restart.
                    with ResponseBarrier(base / "proxy.sock", "DELETE", source_cid) as barrier:
                        with patch.context() as envpatch:
                            envpatch.setenv("DOCKER_HOST", f"unix://{barrier.path}")
                            try:
                                host.compose(config, "up", "-d", "--no-build", "--no-deps",
                                             "--force-recreate", "app", image=target, timeout=20)
                            except subprocess.TimeoutExpired:
                                assert barrier.accepted.is_set(), "DELETE was not accepted"
                            else:
                                raise AssertionError("same Compose call did not time out")
                    assert subprocess.run(["docker", "inspect", source_cid],
                                          capture_output=True, timeout=10).returncode != 0
                    _, image, health = host.container_health(config)
                    assert (image, health) == (target, "created")
                    host.reconcile()
                    assert host.read_json(host.STATUS)["code"] == "legacy_admission_unconfirmed"
                    assert host.TRANSACTION.exists()
                    assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                    return

                if kind == "remove-timeout":
                    with ResponseBarrier(base / "proxy.sock", "DELETE", source_cid) as barrier:
                        def timeout_remove(args, **kwargs):
                            if args[1:3] == ["container", "rm"] and args[-1] == source_cid:
                                kwargs["env"] = {**os.environ, "DOCKER_HOST": f"unix://{barrier.path}"}
                            return original_docker(args, **kwargs)
                        patch.setattr(host, "docker_command", timeout_remove)
                        try:
                            host.prepare_admission(config, tx)
                        except subprocess.TimeoutExpired:
                            assert barrier.accepted.is_set(), "exact source remove not accepted"
                        else:
                            raise AssertionError("source remove response did not time out")
                        patch.setattr(host, "docker_command", original_docker)
                    assert subprocess.run(["docker", "inspect", source_cid],
                                          capture_output=True, timeout=10).returncode != 0
                    assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                    if mountpoint:
                        # With no container, the old installer's data_dir is a
                        # hidden directory that does not hold the live ledger.
                        assert not mounted(mountpoint)
                        assert not Path(mountpoint, ".restart-ledger/cutover-receipt.json").exists()
                elif kind in {"start-timeout", "delayed-entry"}:
                    gate = host.prepare_admission(config, tx)
                    target_cid, start_accepted = None, False
                    def timeout_start(args, **kwargs):
                        nonlocal target_cid, start_accepted
                        if args[1] != "start":
                            return original_docker(args, **kwargs)
                        # Let the real host allocate, journal, create, bind,
                        # open and issue. The test intercepts only transport,
                        # never fabricates gate-only identities.
                        target_cid = args[-1]
                        lock = None
                        with ResponseBarrier(base / "proxy.sock", "POST", target_cid, "start") as barrier:
                            if kind == "delayed-entry":
                                lock = (admission_root / operation / "lock").open("r+b")
                                fcntl.flock(lock, fcntl.LOCK_EX)
                            kwargs["env"] = {**os.environ, "DOCKER_HOST": f"unix://{barrier.path}"}
                            try:
                                return original_docker(args, **kwargs)
                            finally:
                                start_accepted = barrier.accepted.is_set()
                                if lock is not None:
                                    try:
                                        # Freeze the wrapper waiting on the
                                        # gate before the host durably CLOSEs
                                        # and removes its exact container.
                                        assert start_accepted
                                        command("docker", "pause", target_cid, timeout=15)
                                    finally:
                                        fcntl.flock(lock, fcntl.LOCK_UN)
                                        lock.close()
                    patch.setattr(host, "docker_command", timeout_start)
                    try:
                        host.prepare_attempt(config, tx, "target")
                    except subprocess.TimeoutExpired:
                        assert start_accepted, "Docker Start was not accepted"
                    else:
                        raise AssertionError("accepted Docker Start did not time out")
                    finally:
                        patch.setattr(host, "docker_command", original_docker)
                    if kind == "delayed-entry":
                        assert gate.observe()["slots"]["target"]["consumed"] is None
                        assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                        host.prepare_attempt(config, tx, "target")
                    # Gate consumption is durable before the legacy exec, so
                    # wait for that exact boot's ACK and boot-id, not just it.
                    await_admitted_boot(host, config, gate, "target", operation,
                                        source_boot, nonce)
                    assert ledger.ACK_PATH.exists()
                elif kind in {"delayed-create", "wrong-holder"}:
                    # Schedule: (1) the real host journals, allocates and asks
                    # Compose to Create the target under the stable name; the
                    # proxy holds that REQUEST, the CLI times out and is
                    # killed while the daemon has never seen it. (2) The real
                    # host's quiesce_target durably CLOSEs the unbound,
                    # unissued attempt, tombstones its token->digest and
                    # fences it without a container. (3) Only then does the
                    # same captured request reach the daemon, so the OLD
                    # immutable config holds the vacant stable name.
                    name = config["deployment_name"]
                    gate = host.prepare_admission(config, tx)
                    barrier = stack.enter_context(CreateRequestBarrier(base / "proxy.sock", name))
                    routed = []

                    def held_create(args, **kwargs):
                        # Only the target attempt's own Compose Create is
                        # routed through the barrier; nothing is fabricated.
                        if args[:2] == ["docker", "compose"] and "--no-start" in args and not routed:
                            routed.append(list(args))
                            kwargs["env"] = {**os.environ, "DOCKER_HOST": f"unix://{barrier.path}"}
                        return traced(args, **kwargs)

                    patch.setattr(host, "docker_command", held_create)
                    try:
                        host.prepare_attempt(config, tx, "target")
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        raise AssertionError("the held target Create did not time out")
                    finally:
                        patch.setattr(host, "docker_command", traced)
                    barrier.check()
                    assert barrier.captured.is_set(), "Compose never sent the stable-name Create"
                    assert barrier.abandoned.wait(10), "the killed Compose CLI kept its connection"
                    [record] = host.journal_attempts(tx, "target")
                    assert record["cid"] is None and routed[0][3] == "mobius-admission-" + record["token"]
                    sent = json.loads(barrier.body).get("Labels") or {}
                    assert sent.get("io.mobius.admission.token") == record["token"], sent
                    assert sent.get("com.docker.compose.project") == routed[0][3], sent
                    assert host.name_holder(name) is None, "the held Create reached the daemon early"
                    assert [(a["token"], a["state"], a["cid"], a["issued"], a["fenced"])
                            for a in gate.observe()["slots"]["target"]["attempts"]] == [
                        (record["token"], "allocated", None, False, False)]
                    orphans, wrong, marks = [], [], {}

                    def land():
                        orphan = barrier.deliver()
                        item = host.observe_exact(orphan)
                        # Docker-owned metadata: the old frozen attempt file,
                        # not a lookalike, now holds the stable name.
                        host.verify_attempt_item(item, orphan, host.journaled_expected(record))
                        assert (item["State"]["Status"], item["State"]["StartedAt"]) == (
                            "created", NEVER_STARTED)
                        assert item["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
                        orphans.append(orphan)

                    def fence_then_schedule(config_value, transaction):
                        try:
                            original_quiesce(config_value, transaction)
                            # Independent reads of the durable fence.
                            assert [(a["token"], a["state"], a["cid"], a["issued"], a["fenced"])
                                    for a in gate.observe()["slots"]["target"]["attempts"]] == [
                                (record["token"], "closed", None, False, True)]
                            info = host.tombstone_path().lstat()
                            assert (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                                    and stat.S_IMODE(info.st_mode) == 0o600)
                            tombstones = json.loads(host.tombstone_path().read_bytes())
                            assert tombstones["tokens"][record["token"]] == record["attempt_sha256"]
                            if kind == "delayed-create" and not orphans:
                                land()  # between the fence and the next creation
                            elif kind == "wrong-holder" and not wrong:
                                # A label-identical holder of the retired token
                                # that lacks the frozen admission wrapper.
                                expected = host.journaled_expected(record)
                                labels = {**expected["services"]["app"]["labels"],
                                          "com.docker.compose.project": expected["name"]}
                                wrong.append(command(
                                    "docker", "create", "--name", name,
                                    *(f"--label={key}={value}" for key, value in labels.items()),
                                    target).stdout.strip())
                                marks["refusal"] = len(calls)
                        except BaseException as exc:
                            failures.append(exc)
                            raise

                    patch.setattr(host, "quiesce_target", fence_then_schedule)

                    def settle_hook():
                        if failures:
                            raise AssertionError("scheduled host hook failed") from failures[0]

                    def refused(holder, reason):
                        status = host.read_json(host.STATUS)
                        assert (status["state"], status["code"]) == ("needs_recovery",
                                                                     "rollback_failed"), status
                        assert reason in status["message"], status
                        item = host.observe_exact(holder)
                        assert item is not None and item["Name"] == "/" + name
                        assert (item["State"]["Status"], item["State"]["StartedAt"]) == (
                            "created", NEVER_STARTED)
                        touched = [args for args in calls[marks["refusal"]:]
                                   if MUTATING & set(args[1:]) and (holder in args or name in args)]
                        assert not touched, touched
                        assert host.TRANSACTION.exists()
                        assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                        slot = gate.observe()["slots"]["rollback"]
                        assert slot["consumed"] is None
                        assert all(not a["issued"] and a["cid"] is None for a in slot["attempts"])

                    if kind == "wrong-holder":
                        host.reconcile()
                        settle_hook()
                        assert wrong and not orphans
                        refused(wrong[0], "admission coverage is unconfirmed")
                        command("docker", "rm", "--force", wrong[0])  # explicit operator repair
                        wrong.append(command(
                            "docker", "create", "--name", name,
                            f"--label=io.mobius.recovery-proof.project={project}",
                            source).stdout.strip())
                        marks["refusal"] = len(calls)
                        host.reconcile()
                        settle_hook()
                        refused(wrong[1], "another container holds the deployment name")
                        command("docker", "rm", "--force", wrong[1])  # explicit operator repair
                        # The abandoned Create now lands while no worker runs
                        # and the live pointer already names the rollback.
                        land()
                else:
                    raise AssertionError(kind)

                host.reconcile()
                if failures:
                    raise AssertionError("scheduled host hook failed") from failures[0]
                status = host.read_json(host.STATUS)
                if kind in {"start-timeout", "delayed-entry"}:
                    assert status["state"] == "succeeded", status
                    assert ledger.ACK_PATH.exists()
                else:
                    assert status["state"] == "rolled_back", status
                    assert ledger.ACK_PATH.exists()
                assert not host.TRANSACTION.exists()
                if mountpoint:
                    # Recovery read the live ledger through the stable path
                    # after the source was gone, so the exact chat handoff
                    # survived (the 2026-10-12 incident lost it here).
                    assert host.admission_store(config, tx).observe()["source_handoff_valid"] is True
                first_boot = ledger.BOOT_PATH.read_bytes()
                host.reconcile()
                assert ledger.BOOT_PATH.read_bytes() == first_boot, "reconcile replayed a boot"
                if kind == "delayed-entry":
                    history = gate.observe()["slots"]["target"]["attempts"]
                    assert len(history) >= 2 and history[0]["fenced"]
                    assert history[0]["cid"] != history[-1]["cid"]
                    assert history[0]["token"] != history[-1]["token"]
                if kind in {"delayed-create", "wrong-holder"}:
                    [orphan] = orphans
                    assert host.observe_exact(orphan) is None, "the late orphan survived"
                    assert ["docker", "container", "rm", "--force", orphan] in calls, (
                        "the late orphan was not removed by its exact ID")
                    slots = gate.observe()["slots"]
                    assert slots["target"]["consumed"] is None
                    assert [(a["state"], a["cid"], a["issued"], a["fenced"])
                            for a in slots["target"]["attempts"]] == [("closed", None, False, True)]
                    [rollback] = slots["rollback"]["attempts"]
                    assert slots["rollback"]["consumed"]["generation"] == rollback["generation"] == 1
                    holder, item = host.name_holder(name)
                    assert holder == rollback["cid"] != orphan
                    assert item["Image"] == source
                    assert item["Config"]["Labels"]["io.mobius.admission.role"] == "rollback"
                    assert item["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
                    assert all(host.observe_exact(cid) is None for cid in wrong)
                    assert host.read_tombstones()["tokens"][record["token"]] == record["attempt_sha256"]

            try:
                scenario("legacy-incident", "legacy")
                scenario("remove-ambiguity", "remove-timeout")
                scenario("bind-option-volume", "remove-timeout", bind_option_volume=True)
                scenario("accepted-start", "start-timeout")
                scenario("delayed-entry", "delayed-entry")
                scenario("delayed-create", "delayed-create")
                scenario("wrong-holder", "wrong-holder")
                print("real Docker/Compose, admission, and frozen ledger proof passed")
            finally:
                cleanup()  # stop mounted containers before deleting scratch data
    finally:
        patch.undo()
        cleanup()  # also covers interrupted image build


if __name__ == "__main__":
    main()
