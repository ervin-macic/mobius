"""Host recovery against real gate/legacy state and asynchronous fake Docker.

No Docker/root access: python -m pytest --noconftest this_file.py.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "1" * 64
OLD_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "b" * 64
OPERATION = "a" * 32
SOURCE_BOOT = "source-boot-12345678"
NOW = 1001.0


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_for(data):
    module = load(ROOT / "backend/runtime/restart_ledger.py", "recovery_legacy")
    module.DATA_DIR, module.LEDGER_DIR = data, data / ".restart-ledger"
    for attr, filename in {
        "INTENT_PATH": ".restart-continuation-intent.json", "REQUEST_PATH": ".platform-restart-requested",
        "MANAGED_CUTOVER_REQUEST_PATH": ".managed-cutover-request.json",
    }.items():
        setattr(module, attr, data / filename)
    for attr, filename in {
        "ACCEPTED_PATH": "accepted.json", "ACK_PATH": "ack.json", "BOOT_PATH": "boot-id",
        "CUTOVER_CHALLENGE_PATH": "cutover-challenge.json", "CUTOVER_RECEIPT_PATH": "cutover-receipt.json",
    }.items():
        setattr(module, attr, module.LEDGER_DIR / filename)
    module.SUPERVISOR_UID, module.SUPERVISOR_GID = os.getuid(), os.getgid()
    return module


class PowerLoss(BaseException):
    pass


class Docker:
    """Start request acceptance and actual gate entry are independent events."""
    def __init__(self, host, gate, legacy, topology, clock):
        self.host, self.gate, self.legacy = host, gate, legacy
        self.topology, self.clock = topology, clock
        self.containers = {}
        self.history = {}
        self.calls, self.creates, self.starts, self.removes, self.entries = [], [], [], [], []
        self.pending = []
        self.auto_enter = True
        self.fault = None
        self.start_timeout = False
        self.remove_error = None
        self.inspect_error = None
        self.duplicate_discovery = False
        self.before_remove = None
        self.inspect_hook = None
        self.counter = 10
        self.containers[SOURCE] = {
            "Id": SOURCE, "Image": OLD_IMAGE, "Name": "/original-app", "Config": {"Labels": {
                "com.docker.compose.project": "original", "com.docker.compose.service": "app",
                "com.docker.compose.container-number": "1", "com.docker.compose.oneoff": "False"}}, "Mounts": [],
            "RestartCount": 0, "State": {"Status": "running", "StartedAt": "2026-10-09T00:00:00Z",
                                         "Health": {"Status": "healthy"}},
        }

    def hit(self, name):
        if self.fault == name:
            self.fault = None
            raise PowerLoss(name)

    def sleep(self, seconds):
        self.clock[0] += seconds
        if self.auto_enter:
            for cid in list(self.pending):
                self.complete_start(cid)

    def complete_start(self, cid, *, stale_process=False, before_legacy=False):
        if cid in self.pending:
            self.pending.remove(cid)
        item = self.containers.get(cid)
        if item is None and not stale_process:
            return False  # exact removal fences a not-yet-executed daemon Start
        item = item or self.history[cid]
        token = item["Config"]["Labels"]["io.mobius.admission.token"]
        boot = f"boot-{cid[-12:]}-{len(self.entries):08d}"
        try:
            result = self.gate.enter(token, boot, now=self.clock[0])
        except RuntimeError:
            self.entries.append((cid, "denied", False))
            if cid in self.containers:
                item["State"]["Status"] = "exited"
            return False
        authorized = False if before_legacy else self.legacy.begin_boot(boot, now=self.clock[0])
        self.entries.append((cid, result, authorized))
        if cid in self.containers:
            item["State"] = {"Status": "running", "StartedAt": "2026-10-09T00:01:00Z",
                             "Health": {"Status": "healthy"}}
        return True

    def _reply(self, args, output="", code=0, error="", check=True):
        result = subprocess.CompletedProcess(args, code, stdout=output, stderr=error)
        if check:
            result.check_returncode()
        return result

    def command(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs))
        check = kwargs.get("check", True)
        if args[1:3] == ["image", "inspect"]:
            assert args[-2] == "{{json .Config}}"
            return self._reply(args, json.dumps({"Entrypoint": ["/sbin/tini", "--", "/original-entrypoint"],
                                                "Cmd": ["serve", "argument with spaces"]}))
        if args[1] == "compose":
            if "config" in args:
                return self._reply(args, json.dumps(self.topology))
            if "ps" in args:
                output = SOURCE if SOURCE in self.containers else ""
                return self._reply(args, output)
            assert "up" in args
            assert all(flag in args for flag in ("--no-start", "--no-recreate", "--no-build", "--no-deps"))
            assert "--force-recreate" not in args and "--remove-orphans" not in args
            paths = [args[i + 1] for i, part in enumerate(args) if part == "-f"]
            assert len(paths) == 1 and Path(paths[0]) != self.host.OVERRIDE
            # Compose resolves one level of escaped literal dollars at Create.
            expected = json.loads(Path(paths[0]).read_text().replace("$$", "$"))
            app = expected["services"]["app"]
            token = app["labels"]["io.mobius.admission.token"]
            assert args[args.index("-p") + 1] == expected["name"] == "mobius-admission-" + token
            assert set(expected["services"]) == {"app"}
            assert expected["networks"]["default"] == {"name": "original_default", "external": True}
            self.hit("create-before")
            existing = [c for c in self.containers.values() if c["Name"] == "/" + app["container_name"]]
            if existing and any(c["Config"].get("Labels", {}).get("com.docker.compose.project") != expected["name"]
                                for c in existing):
                return self._reply(args, code=1, error="Conflict: container name already in use", check=check)
            if not existing:
                self.counter += 1
                cid = f"{self.counter:064x}"
                item = {
                    "Id": cid, "Image": app["image"], "Name": "/" + app["container_name"],
                    "Config": {"Entrypoint": app["entrypoint"], "Cmd": app["command"], "Labels": {**app["labels"],
                               "com.docker.compose.project": expected["name"],
                               "com.docker.compose.service": "app",
                               "com.docker.compose.container-number": "1",
                               "com.docker.compose.oneoff": "False"}},
                    "Mounts": [{"Type": volume["type"], "Source": volume["source"],
                                "Destination": volume["target"], "RW": not volume.get("read_only", False)}
                               for volume in app["volumes"]],
                    "RestartCount": 0,
                    "State": {"Status": "created", "StartedAt": "0001-01-01T00:00:00Z"},
                }
                self.containers[cid] = item
                self.history[cid] = copy.deepcopy(item)
                self.creates.append((cid, expected["name"], token, Path(paths[0]).read_bytes()))
            self.hit("create-after")
            return self._reply(args)
        if args[1:3] == ["container", "inspect"]:
            identity = args[-1]
            if self.inspect_hook:
                self.inspect_hook(args)
            if self.inspect_error:
                return self._reply(args, code=1, error=self.inspect_error, check=check)
            candidates = [v for k, v in self.containers.items()
                          if k == identity or v["Name"].removeprefix("/") == identity]
            if not candidates:
                return self._reply(args, code=1,
                                   error=f"Error response from daemon: No such container: {identity}", check=check)
            item = candidates[0]
            template = args[-2]
            if template == "{{.Id}}":
                output = item["Id"]
                if self.duplicate_discovery:
                    output += "\n" + "f" * 64
            elif template == "{{json .}}":
                output = json.dumps(item)
                if self.duplicate_discovery:
                    output += "\n" + json.dumps({**item, "Id": "f" * 64})
            elif template == "{{.Image}}":
                output = item["Image"]
            else:
                assert template.startswith("{{.Image}} {{.State.Status}}")
                state = item["State"]
                output = f"{item['Image']} {state['Status']} {state.get('Health', {}).get('Status', '')}"
            return self._reply(args, output, check=check)
        if args[1:3] == ["container", "rm"]:
            cid = args[-1]
            assert len(cid) == 64
            if self.before_remove:
                self.before_remove(cid)
            self.hit("remove-before")
            if self.remove_error:
                return self._reply(args, code=1, error=self.remove_error, check=check)
            if cid not in self.containers:
                return self._reply(args, code=1,
                                   error=f"Error response from daemon: No such container: {cid}", check=check)
            self.removes.append(cid)
            del self.containers[cid]
            self.hit("remove-after")
            return self._reply(args)
        if args[1] == "start":
            cid = args[-1]
            self.hit("start-before")
            assert cid in self.containers
            self.starts.append(cid)
            self.pending.append(cid)
            self.hit("start-after")
            if self.start_timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return self._reply(args)
        if args[1] == "exec":
            assert args[-2] == "finalize-cutover"
            assert args[2] in self.containers
            success = self.legacy.finalize_cutover(args[-1], now=self.clock[0])
            return self._reply(args, code=0 if success else 1, check=check)
        if args[1] == "ps":
            assert "label=mobius-rebuild.worker-extract=1" in args
            return self._reply(args)
        raise AssertionError(f"unimplemented fake Docker command: {args}")


@pytest.fixture
def incident(tmp_path, monkeypatch):
    host = load(ROOT / "scripts/mobius-rebuild-host.py", "admission_host_test")
    module = load(ROOT / "scripts/mobius-boot-admission.py", "admission_gate_test")
    data, state, config_root = (tmp_path / name for name in ("data", "host-state", "host-config"))
    for path in (data, state, config_root):
        path.mkdir(mode=0o700)
    control = data / "mobius-rebuild"
    (control / "inbox").mkdir(parents=True)
    paths = {"STATE_DIR": state, "CONFIG": config_root / "config.json", "COMPOSE": config_root / "compose.json",
             "OVERRIDE": config_root / "image.override.json", "STATUS": state / "status.json",
             "LOCK": state / "replace.lock", "TRANSACTION": state / "transaction.json",
             "IMAGES": state / "images.json", "FAILED_TARGET_LOG": state / "failed-target.json",
             "ADMISSION_ROOT": state / "admission", "ADMISSION_CODE": ROOT / "scripts/mobius-boot-admission.py"}
    for name, path in paths.items():
        monkeypatch.setattr(host, name, path)
    host.ADMISSION_ROOT.mkdir(mode=0o700)
    config = {"version": 4, "project": "original", "data_dir": data, "control_dir": control,
              "deployment_name": "original-app", "name_source": "explicit"}
    topology = {"name": "original", "services": {"app": {
        "image": OLD_IMAGE, "container_name": "original-app", "networks": {"default": {}},
        "volumes": [{"type": "bind", "source": str(data), "target": "/data"}],
        "restart": "unless-stopped",
    }}, "networks": {"default": {"name": "original_default"}}}
    host.COMPOSE.write_text(json.dumps(topology))
    host.OVERRIDE.write_text(json.dumps({"services": {"app": {"image": OLD_IMAGE}}}))
    legacy = legacy_for(data)
    legacy.begin_boot(SOURCE_BOOT, now=1000)
    legacy.open_cutover(OPERATION, now=1000)
    legacy.INTENT_PATH.write_text(json.dumps({
        "version": 1, "action": "external_cutover", "cutover_id": OPERATION,
        "nonce": "original-nonce-12345678", "source_boot_id": SOURCE_BOOT, "created_at": 1000,
    }))
    assert legacy.accept_cutover(OPERATION, now=NOW)
    tx = host.transaction_record(OPERATION, "b" * 40, "original-nonce-12345678", OLD_IMAGE, NEW_IMAGE)
    tx.update(admission_version=1, source_container=SOURCE, phase="rollback_started")
    host.write_transaction(tx)
    gate = module.AdmissionStore(host.ADMISSION_ROOT / OPERATION, data, os.getuid(), os.getgid())
    clock = [NOW]
    docker = Docker(host, gate, legacy, topology, clock)
    monkeypatch.setattr(host, "config", lambda: config)
    monkeypatch.setattr(host, "admission_store", lambda *_: gate)
    monkeypatch.setattr(host, "docker_command", docker.command)
    monkeypatch.setattr(host.time, "time", lambda: clock[0])
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host.time, "sleep", docker.sleep)
    monkeypatch.setattr(host, "ROLLBACK_HEALTH_SECONDS", 12)
    monkeypatch.setattr(host, "_bounded_docker_logs", lambda *_: (b"fake target evidence", b"", False, False))
    monkeypatch.setattr(host, "verify_served_generation", lambda *_: None)
    monkeypatch.setattr(host, "retain_images", lambda *_: None)
    monkeypatch.setattr(host, "adopt_from_image", lambda *_: "test")
    proof = host.cutover_boot_consumed
    monkeypatch.setattr(host, "cutover_boot_consumed", lambda c, op, **kwargs:
                        proof(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid(), **kwargs))
    return SimpleNamespace(host=host, gate=gate, module=module, legacy=legacy, config=config,
                           tx=tx, docker=docker, clock=clock)


def prepare(case):
    return case.host.prepare_rollback(case.config, case.host.read_transaction())


def rollback_attempts(case):
    return case.gate.observe()["slots"]["rollback"]["attempts"]


def recover(case):
    transaction = case.host.read_transaction()
    if transaction:
        case.host.recover(case.config, transaction)


def test_start_request_does_not_atomically_consume_admission(incident):
    c = incident
    cid = prepare(c)
    assert c.docker.starts == [cid] and c.docker.pending == [cid]
    assert c.gate.observe()["slots"]["rollback"]["consumed"] is None
    assert c.legacy.ACCEPTED_PATH.exists()
    assert c.docker.complete_start(cid)
    assert c.docker.entries == [(cid, "continuation", True)]


@pytest.mark.parametrize("method", ["allocate", "bind", "open", "issue_start"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_gate_transition_crash_reconcile_is_causal(incident, monkeypatch, method, when):
    c = incident
    original, fired = getattr(c.gate, method), [False]
    def interrupt(*args, **kwargs):
        if not fired[0] and when == "before":
            fired[0] = True
            raise PowerLoss(method)
        result = original(*args, **kwargs)
        if not fired[0]:
            fired[0] = True
            raise PowerLoss(method)
        return result
    monkeypatch.setattr(c.gate, method, interrupt)
    with pytest.raises(PowerLoss):
        prepare(c)
    monkeypatch.setattr(c.gate, method, original)
    for _ in range(3):
        c.host.reconcile()
    assert len([e for e in c.docker.entries if e[1] == "continuation"]) == 1
    assert not c.host.TRANSACTION.exists()
    assert c.host.read_json(c.host.STATUS)["state"] == "rolled_back"
    starts = list(c.docker.starts)
    c.host.reconcile()
    assert c.docker.starts == starts
    assert len(starts) == len(set(starts))  # never repeat Start on ambiguous CID


@pytest.mark.parametrize("boundary", ["create-before", "create-after", "start-before", "start-after",
                                        "remove-before", "remove-after"])
def test_docker_submission_and_response_crashes_reconcile(incident, boundary):
    c = incident
    c.docker.fault = boundary
    with pytest.raises(PowerLoss):
        prepare(c)
    for _ in range(3):
        c.host.reconcile()
    assert not c.host.TRANSACTION.exists()
    assert c.host.read_json(c.host.STATUS)["state"] == "rolled_back"
    assert len([e for e in c.docker.entries if e[1] == "continuation"]) == 1
    assert len(c.docker.starts) == len(set(c.docker.starts))
    assert all(attempt["state"] == "closed" and attempt["fenced"]
               for attempt in rollback_attempts(c)[:-1])


def target_replacement(case):
    transaction = case.host.read_transaction()
    transaction.update(phase="replacement_started", drain_confirmed=True, source_token=None)
    case.host.write_transaction(transaction)
    case.host.prepare_admission(case.config, transaction)
    return transaction


def assert_target_interruption_recovers_once(case):
    # Unlike rollback, a created target is retired through quiesce_target, not
    # resumed through prepare_attempt. Both paths must replay journal-leading
    # gate transitions without stranding the only service or repeating Start.
    for _ in range(3):
        case.host.reconcile()
    status = case.host.read_json(case.host.STATUS)
    assert status["state"] in {"rolled_back", "succeeded"}, status
    assert not case.host.TRANSACTION.exists()
    running = [c for c in case.docker.containers.values() if c["State"]["Status"] == "running"]
    assert len(running) == 1 and running[0]["State"]["Health"]["Status"] == "healthy"
    assert len([e for e in case.docker.entries if e[1] == "continuation"]) == 1
    starts = list(case.docker.starts)
    assert len(starts) == len(set(starts))
    case.host.reconcile()
    assert case.docker.starts == starts


@pytest.mark.parametrize("already_exists", [False, True])
def test_first_attempt_directory_survives_power_loss_after_journal_publication(
        incident, monkeypatch, already_exists):
    case = incident
    transaction = target_replacement(case)
    assert not case.docker.containers
    directory = case.host.CONFIG.parent / "attempts"
    if already_exists:
        # A previous mkdir survived in memory, but its parent was never synced.
        directory.mkdir(mode=0o700)
    synced = []
    sync = case.host._fsync_dir
    def trace_sync(path):
        sync(path)
        synced.append(path)
    monkeypatch.setattr(case.host, "_fsync_dir", trace_sync)
    allocate = case.gate.allocate
    def interrupt(*_args):
        raise PowerLoss("journal published before allocation")
    monkeypatch.setattr(case.gate, "allocate", interrupt)
    with pytest.raises(PowerLoss):
        case.host.prepare_attempt(case.config, transaction, "target")
    record = case.host.read_transaction()["attempts"][0]
    assert case.host.attempt_path(record["token"]).exists()
    before_crash = list(synced)
    # Persistence model, not a physical power cut: lose only the new directory
    # entry if its parent was not synced before the durable journal reference.
    if case.host.CONFIG.parent not in before_crash:
        shutil.rmtree(directory)
    monkeypatch.setattr(case.gate, "allocate", allocate)
    assert_target_interruption_recovers_once(case)
    assert before_crash.index(case.host.CONFIG.parent) < before_crash.index(directory)
    assert before_crash.index(directory) < before_crash.index(case.host.STATE_DIR)


@pytest.mark.parametrize("error,after_sync", [(OSError, False), (PowerLoss, False), (PowerLoss, True)])
def test_attempt_directory_sync_failure_does_not_publish_identity_and_retry_recovers(
        incident, monkeypatch, error, after_sync):
    case = incident
    transaction = target_replacement(case)
    directory = case.host.CONFIG.parent / "attempts"
    sync = case.host._fsync_dir
    def interrupt(path):
        if path == directory.parent:
            if after_sync:
                sync(path)
            raise error("attempt directory parent sync")
        sync(path)
    monkeypatch.setattr(case.host, "_fsync_dir", interrupt)
    with pytest.raises(error, match="attempt directory parent sync"):
        case.host.prepare_attempt(case.config, transaction, "target")
    assert directory.is_dir()
    assert not list(directory.iterdir())
    assert not case.host.read_transaction().get("attempts")
    assert not case.gate.observe()["slots"]["target"]["attempts"]
    assert not case.docker.creates and not case.docker.starts
    monkeypatch.setattr(case.host, "_fsync_dir", sync)
    assert_target_interruption_recovers_once(case)


@pytest.mark.parametrize("method", ["allocate", "bind", "open", "issue_start"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_target_gate_transition_crash_retires_or_settles_without_stranding_service(
        incident, monkeypatch, method, when):
    case = incident
    transaction = target_replacement(case)
    original = getattr(case.gate, method)
    def interrupt(*args, **kwargs):
        if when == "after":
            original(*args, **kwargs)
        raise PowerLoss(method)
    monkeypatch.setattr(case.gate, method, interrupt)
    with pytest.raises(PowerLoss):
        case.host.prepare_attempt(case.config, transaction, "target")
    monkeypatch.setattr(case.gate, method, original)
    assert_target_interruption_recovers_once(case)


@pytest.mark.parametrize("boundary", ["create-before", "create-after", "start-before", "start-after"])
def test_target_docker_interruption_retires_or_settles_without_stranding_service(incident, boundary):
    case = incident
    transaction = target_replacement(case)
    case.docker.fault = boundary
    with pytest.raises(PowerLoss):
        case.host.prepare_attempt(case.config, transaction, "target")
    assert_target_interruption_recovers_once(case)


@pytest.mark.parametrize("when", ["before", "after"])
def test_recovery_binding_of_closed_target_replays_after_another_crash(incident, monkeypatch, when):
    case = incident
    transaction = target_replacement(case)
    bind = case.gate.bind
    def before_bind(*_args):
        raise PowerLoss("before bind")
    monkeypatch.setattr(case.gate, "bind", before_bind)
    with pytest.raises(PowerLoss):
        case.host.prepare_attempt(case.config, transaction, "target")
    target = transaction["attempts"][0]["cid"]
    def recovery_bind(role, token, cid):
        attempt = case.gate.observe()["slots"][role]["attempts"][-1]
        assert (role, cid, attempt["state"], attempt["issued"]) == ("target", target, "closed", False)
        if when == "after":
            bind(role, token, cid)
        raise PowerLoss("recovery bind")
    monkeypatch.setattr(case.gate, "bind", recovery_bind)
    with pytest.raises(PowerLoss):
        case.host.quiesce_target(case.config, case.host.read_transaction())
    assert target in case.docker.containers and not case.docker.starts
    monkeypatch.setattr(case.gate, "bind", bind)
    assert_target_interruption_recovers_once(case)


@pytest.mark.parametrize("age,continued", [(601, True), (3601, False)])
def test_actual_wrapper_entry_refreshes_or_serves_without_continuation(incident, age, continued):
    c = incident
    cid = prepare(c)
    receipt = c.legacy.CUTOVER_RECEIPT_PATH.read_bytes()
    c.clock[0] += age
    assert c.docker.complete_start(cid)
    assert c.docker.entries[-1][2] is continued
    for _ in range(3):
        c.host.reconcile()
    assert c.docker.starts == [cid]
    assert c.host.TRANSACTION.exists() is not continued
    if not continued:
        assert not c.legacy.ACK_PATH.exists()
        assert c.legacy.CUTOVER_RECEIPT_PATH.read_bytes() == receipt
        assert c.host.read_json(c.host.STATUS)["code"] == "handoff_boot_unconfirmed"


def test_admitted_rollback_is_never_host_retried_or_removed(incident):
    c = incident
    cid = prepare(c)
    c.docker.complete_start(cid)
    c.docker.containers[cid]["State"]["Status"] = "exited"
    for _ in range(3):
        recover(c)
    assert c.docker.starts == [cid]
    assert cid not in c.docker.removes
    assert c.host.TRANSACTION.exists()
    assert c.gate.observe()["slots"]["rollback"]["consumed"]


def test_admitted_missing_rollback_is_not_recreated(incident):
    c = incident
    cid = prepare(c)
    c.docker.complete_start(cid)
    del c.docker.containers[cid]
    for _ in range(2):
        recover(c)
    assert c.docker.starts == [cid]
    assert len(c.docker.creates) == 1
    assert c.host.TRANSACTION.exists()


def test_late_start_after_host_observation_wins_no_reauthorization(incident, monkeypatch):
    """New-protocol counterpart of old consumed-witness refresh race."""
    c = incident
    c.docker.start_timeout = True
    with pytest.raises(subprocess.TimeoutExpired):
        cid = prepare(c)
    cid = c.docker.starts[0]
    c.docker.start_timeout = False
    observe, raced = c.gate.observe, [False]
    def consume_after_observation():
        snapshot = observe()
        slot = snapshot["slots"]["rollback"]
        if slot["attempts"] and not slot["consumed"] and not raced[0]:
            raced[0] = True
            c.docker.complete_start(cid)
        return snapshot
    monkeypatch.setattr(c.gate, "observe", consume_after_observation)
    assert prepare(c) == cid
    assert raced[0]
    assert c.docker.starts == [cid] and cid not in c.docker.removes
    assert not c.legacy.ACCEPTED_PATH.exists()
    assert not c.legacy.begin_boot("unrelated-later-boot-12345678", now=c.clock[0])


def test_close_wins_before_late_entry_and_successor_consumes_once(incident):
    """Host closes before removal; delayed consumer cannot enter legacy code."""
    c = incident
    first = prepare(c)
    seen = []
    def enter_during_remove(cid):
        if cid == first:
            seen.append(c.docker.complete_start(cid))
    c.docker.before_remove = enter_during_remove
    second = prepare(c)
    assert seen == [False]
    assert first != second
    assert c.docker.complete_start(second)
    assert c.docker.entries == [(first, "denied", False), (second, "continuation", True)]
    assert not c.legacy.ACCEPTED_PATH.exists()
    assert not c.legacy.begin_boot("unrelated-next-boot-12345678", now=c.clock[0])


def test_target_admission_is_fenced_before_separate_rollback(incident):
    c = incident
    c.host.prepare_admission(c.config, c.tx)
    target = c.host.prepare_attempt(c.config, c.tx, "target")
    c.docker.complete_start(target)
    target_boot = c.gate.observe()["slots"]["target"]["consumed"]["boot_id"]
    c.docker.containers[target]["State"]["Status"] = "exited"
    rollback = prepare(c)
    assert target in c.docker.removes
    state = c.gate.observe()
    assert state["slots"]["target"]["consumed"]["boot_id"] == target_boot
    assert target in state["quiesced"] and state["slots"]["rollback"]["consumed"] is None
    c.docker.complete_start(rollback)
    ack = json.loads(c.legacy.ACK_PATH.read_text())
    assert ack["source_boot_id"] == target_boot
    assert len(c.docker.starts) == 2
    assert not c.docker.complete_start(target, stale_process=True)


@pytest.mark.parametrize("error", ["Cannot connect to the Docker daemon", "permission denied", "context deadline exceeded"])
def test_removal_errors_do_not_mean_fenced_or_allow_new_generation(incident, error):
    c = incident
    cid = prepare(c)
    c.docker.remove_error = error
    with pytest.raises(subprocess.CalledProcessError):
        prepare(c)
    attempts = rollback_attempts(c)
    assert len(attempts) == 1 and attempts[0]["state"] == "closed" and not attempts[0]["fenced"]
    assert c.docker.starts == [cid]
    assert not c.docker.complete_start(cid)


@pytest.mark.parametrize("corruption", ["image", "entrypoint", "mount", "label", "name"])
def test_discovered_wrong_identity_never_opens_or_starts(incident, corruption):
    c = incident
    c.docker.fault = "create-after"
    with pytest.raises(PowerLoss):
        prepare(c)
    item = next(v for k, v in c.docker.containers.items() if k != SOURCE)
    if corruption == "image":
        item["Image"] = "sha256:" + "f" * 64
    elif corruption == "entrypoint":
        item["Config"]["Entrypoint"] = ["/unguarded-entrypoint"]
    elif corruption == "mount":
        item["Mounts"][0]["Source"] = "/wrong-data"
    elif corruption == "label":
        item["Config"]["Labels"]["io.mobius.admission.token"] = "f" * 32
    else:
        item["Name"] = "/foreign-name"
    # A changed name means the expected name is now absent, not permission to
    # adopt the foreign container. Other corruption must explicitly reject.
    if corruption == "name":
        c.docker.inspect_error = "identity observation unavailable"
        expected_error = subprocess.CalledProcessError
    else:
        expected_error = RuntimeError
    with pytest.raises(expected_error):
        prepare(c)
    assert not c.docker.starts
    assert c.gate.observe()["slots"]["rollback"]["consumed"] is None


def test_duplicate_identity_response_is_not_arbitrarily_adopted(incident):
    c = incident
    c.docker.fault = "create-after"
    with pytest.raises(PowerLoss):
        prepare(c)
    c.docker.duplicate_discovery = True
    with pytest.raises((RuntimeError, ValueError, subprocess.SubprocessError)):
        prepare(c)
    assert not c.docker.starts


def test_pre_admission_lost_bound_container_remains_no_recreate(incident, monkeypatch):
    c = incident
    bind = c.gate.bind
    def bind_then_crash(*args):
        bind(*args)
        raise PowerLoss()
    monkeypatch.setattr(c.gate, "bind", bind_then_crash)
    with pytest.raises(PowerLoss):
        prepare(c)
    monkeypatch.setattr(c.gate, "bind", bind)
    cid = rollback_attempts(c)[0]["cid"]
    del c.docker.containers[cid]
    recover(c)
    assert len(c.docker.creates) == 1
    assert not c.docker.starts
    assert c.host.TRANSACTION.exists()


@pytest.mark.parametrize("timing", ["before-prepare", "between-prepare-and-removal"])
@pytest.mark.parametrize("boundary", ["after-unlink", "after-ack", "after-boot"])
def test_source_autoreboot_or_partial_consume_restores_only_service(incident, timing, boundary):
    c = incident
    receipt = json.loads(c.legacy.CUTOVER_RECEIPT_PATH.read_text())
    if timing == "between-prepare-and-removal":
        c.gate.prepare(OPERATION, receipt, SOURCE, OLD_IMAGE, SOURCE_BOOT)
    if boundary == "after-boot":
        assert c.legacy.begin_boot("unexpected-source-boot-12345678", now=c.clock[0])
    else:
        c.legacy.ACCEPTED_PATH.unlink()
        if boundary == "after-ack":
            c.legacy._write_json(c.legacy.ACK_PATH,
                                 {**receipt, "target_boot_id": "unexpected-source-boot-12345678"}, 0o444)
    cid = prepare(c)
    assert c.gate.observe()["source_handoff_valid"] is False
    assert c.docker.complete_start(cid)
    assert c.docker.entries[-1] == (cid, "service_only", False)
    c.host.reconcile()
    c.host.reconcile()
    assert c.host.TRANSACTION.exists()
    assert c.docker.starts == [cid]
    assert c.host.read_json(c.host.STATUS)["code"] == "handoff_boot_unconfirmed"


def test_consumer_paused_before_acceptance_replace_blocks_host_retry(incident, monkeypatch):
    """Counterpart of old accepted.json replace race, through actual host retry."""
    import threading
    c = incident
    cid = prepare(c)
    paused, resume, observing, host_done = (threading.Event() for _ in range(4))
    results, errors = [], []
    replace = c.module.os.replace
    def pause_replace(source, target, **kwargs):
        if target == "accepted.json":
            paused.set()
            assert resume.wait(5)
        return replace(source, target, **kwargs)
    monkeypatch.setattr(c.module.os, "replace", pause_replace)
    def consume():
        try:
            c.docker.complete_start(cid)
        except BaseException as exc:
            errors.append(exc)
    def retry():
        try:
            observing.set()
            results.append(prepare(c))
        except BaseException as exc:
            errors.append(exc)
        finally:
            host_done.set()
    consumer = threading.Thread(target=consume, daemon=True)
    worker = threading.Thread(target=retry, daemon=True)
    consumer.start()
    try:
        assert paused.wait(5)
        worker.start()
        assert observing.wait(5)
        assert not host_done.wait(0.1)
    finally:
        resume.set()
        consumer.join(5)
        if worker.ident is not None:
            worker.join(5)
    assert not consumer.is_alive() and not worker.is_alive()
    assert not errors
    assert results == [cid]
    assert c.docker.starts == [cid] and cid not in c.docker.removes
    assert not c.legacy.ACCEPTED_PATH.exists()
    assert not c.legacy.begin_boot("unrelated-next-boot-12345678", now=c.clock[0])


def test_prepare_interrupted_after_directory_before_state_is_recoverable(incident, monkeypatch):
    c = incident
    save, fired = c.gate._save, [False]
    def fail_first_prepare(directory, state):
        if not fired[0]:
            fired[0] = True
            raise PowerLoss("operation-before-state-publication")
        return save(directory, state)
    monkeypatch.setattr(c.gate, "_save", fail_first_prepare)
    with pytest.raises(PowerLoss):
        prepare(c)
    assert SOURCE in c.docker.containers  # no destructive step was authorized
    assert c.gate.state_dir.is_dir() and not (c.gate.state_dir / "state.json").exists()
    for _ in range(2):
        c.host.reconcile()
    assert not c.host.TRANSACTION.exists()
    assert c.host.read_json(c.host.STATUS)["state"] == "rolled_back"


def test_missing_name_observation_is_rechecked_without_mutation(incident, monkeypatch):
    c = incident
    cid = prepare(c)
    c.docker.complete_start(cid)
    command, stale = c.docker.command, [True]
    def stale_once(args, **kwargs):
        if (stale[0] and args[1:3] == ["container", "inspect"]
                and args[-1] == c.config["deployment_name"]):
            # Managed discovery now inspects name, identity and state together,
            # not an ID followed by a second health lookup. A transient absent
            # observation still cannot authorize a repeated boot or removal.
            stale[0] = False
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=
                f"Error response from daemon: No such container: {args[-1]}")
        return command(args, **kwargs)
    monkeypatch.setattr(c.host, "docker_command", stale_once)
    recover(c)
    assert not stale[0]
    assert not c.host.TRANSACTION.exists()
    assert c.docker.starts == [cid] and cid not in c.docker.removes


def test_observation_transport_failure_does_not_allocate_or_remove(incident):
    c = incident
    cid = prepare(c)
    c.docker.complete_start(cid)
    snapshot = c.gate.observe()
    c.docker.inspect_error = "Cannot connect to the Docker daemon"
    recover(c)
    assert c.gate.observe() == snapshot
    assert c.docker.starts == [cid] and cid not in c.docker.removes
    assert c.host.TRANSACTION.exists()
    assert c.host.read_json(c.host.STATUS)["state"] == "needs_recovery"


@pytest.mark.parametrize("delay,continued", [(601, True), (3601, False)])
def test_delayed_prestart_recovery_fences_old_attempt_before_refresh(incident, delay, continued):
    c = incident
    c.docker.start_timeout = True
    with pytest.raises(subprocess.TimeoutExpired):
        prepare(c)
    old = c.docker.starts[0]
    c.docker.start_timeout = False
    c.clock[0] += delay
    c.host.reconcile()
    attempts = rollback_attempts(c)
    assert len(attempts) == 2 and attempts[0]["state"] == "closed" and attempts[0]["fenced"]
    new = attempts[1]["cid"]
    assert c.docker.starts == [old, new]
    assert c.docker.entries[-1] == (new, "continuation" if continued else "service_only", continued)
    ack = c.legacy.ACK_PATH.read_bytes() if c.legacy.ACK_PATH.exists() else None
    assert not c.docker.complete_start(old, stale_process=True)
    assert (c.legacy.ACK_PATH.read_bytes() if c.legacy.ACK_PATH.exists() else None) == ack
    c.host.reconcile()
    assert c.docker.starts == [old, new]
    assert c.host.TRANSACTION.exists() is not continued


def test_admission_then_prelegacy_death_cannot_be_host_rearmed(incident):
    c = incident
    cid = prepare(c)
    assert c.docker.complete_start(cid, before_legacy=True)
    assert c.legacy.ACCEPTED_PATH.exists()
    recover(c)
    assert c.host.TRANSACTION.exists()
    assert c.docker.starts == [cid]
    assert cid not in c.docker.removes
    # Autonomous restart, not a second host Start. Gate cleans the leftover
    # cutover acceptance before the untouched legacy begin_boot sees it.
    assert c.docker.complete_start(cid)
    assert c.docker.entries[-1] == (cid, "service_only", False)
    assert not c.legacy.ACCEPTED_PATH.exists()
    recover(c)
    assert c.host.TRANSACTION.exists() and c.docker.starts == [cid]


# Lost cutover evidence costs continuation, never service: repeated
# reconciliation restores exactly one healthy service-only generation, never a
# continuation, journal clear, rearm or new-generation loop.
def source_watchdog_restart(case):
    """Source abandoned-cutover watchdog: ORDINARY restart for the drained runs,
    accepted by the real root poller just before the host's exact removal."""
    request = {"version": 1, "nonce": "original-nonce-12345678", "source_boot_id": SOURCE_BOOT,
               "created_at": NOW + 90, "runs": []}
    case.legacy.INTENT_PATH.write_text(json.dumps(request))
    case.legacy.REQUEST_PATH.write_text(json.dumps({"nonce": request["nonce"], "source_boot_id": SOURCE_BOOT}))
    assert case.legacy.accept(SOURCE_BOOT, now=NOW + 90)
    case.clock[0] = NOW + 91


def assert_one_service_without_handoff(case, role):
    c = case
    for _ in range(4):
        c.host.reconcile()
    slot = c.gate.observe()["slots"][role]
    assert len(slot["attempts"]) == 1 and slot["consumed"]["generation"] == 1
    assert slot["attempts"][0]["continuation"] is False
    cid = slot["attempts"][0]["cid"]
    assert c.docker.starts == [cid] and len(c.docker.creates) == 1
    assert cid not in c.docker.removes
    assert c.docker.entries == [(cid, "service_only", False)]
    assert c.docker.containers[cid]["State"]["Health"]["Status"] == "healthy"
    assert not c.legacy.ACCEPTED_PATH.exists() and not c.legacy.ACK_PATH.exists()
    assert c.host.TRANSACTION.exists()
    status = c.host.read_json(c.host.STATUS)
    assert (status["state"], status["code"]) == ("needs_recovery", "handoff_boot_unconfirmed")
    snapshot = c.gate.observe()
    c.host.reconcile()
    assert c.gate.observe() == snapshot and c.docker.starts == [cid]


def test_source_watchdog_restart_pending_at_removal_restores_one_rollback(incident):
    c = incident
    source_watchdog_restart(c)
    assert_one_service_without_handoff(c, "rollback")
    assert c.gate.observe()["source_handoff_valid"] is False


@pytest.mark.parametrize("damage", ["missing", "reaccepted"])
def test_receipt_lost_after_gate_prepare_restores_one_rollback(incident, damage):
    c = incident
    c.host.prepare_admission(c.config, c.host.read_transaction())
    assert c.gate.observe()["source_handoff_valid"] is True
    receipt = json.loads(c.legacy.CUTOVER_RECEIPT_PATH.read_text())
    c.legacy.CUTOVER_RECEIPT_PATH.unlink()
    if damage == "reaccepted":
        replacement = {**receipt, "nonce": "reaccepted-nonce-12345678"}
        c.legacy._write_json(c.legacy.ACCEPTED_PATH, replacement, 0o600)
        c.legacy._write_json(c.legacy.CUTOVER_RECEIPT_PATH, replacement, 0o444)
    assert_one_service_without_handoff(c, "rollback")


def test_source_watchdog_restart_pending_at_removal_admits_target_service_only(incident):
    c = incident
    source_watchdog_restart(c)
    c.host.prepare_admission(c.config, c.tx)
    target = c.host.prepare_attempt(c.config, c.tx, "target")
    assert c.docker.complete_start(target)
    assert c.docker.entries == [(target, "service_only", False)]
    assert not c.legacy.ACCEPTED_PATH.exists() and not c.legacy.ACK_PATH.exists()
    c.tx["phase"] = "replacement_started"
    c.host.write_transaction(c.tx)
    # The healthy service-only target keeps serving: no rollback, no rearm.
    assert_one_service_without_handoff(c, "target")
    assert c.docker.removes == [SOURCE]
    assert c.gate.observe()["slots"]["rollback"]["attempts"] == []


# The disposable Docker proof's synchronization (scripts/test-rebuild-recovery-
# docker.py) must wait out exactly the real wrapper's causal order: gate
# consumption is durable BEFORE the legacy exec writes ack.json and boot-id.
NONCE = "original-nonce-12345678"


def proof_harness():
    return load(ROOT / "scripts/test-rebuild-recovery-docker.py", "recovery_docker_harness")


def admitted_target_before_legacy(case, monkeypatch, *, enter=True):
    """A real target attempt whose gate entry (optionally) consumed, legacy not yet run."""
    transaction = target_replacement(case)
    case.docker.auto_enter = False
    cid = case.host.prepare_attempt(case.config, transaction, "target")
    if enter:
        assert case.docker.complete_start(cid, before_legacy=True)
        assert case.docker.entries == [(cid, "continuation", False)]
    sleeps = []
    def sleep(seconds):  # deterministic fixture clock; schedules hook here
        sleeps.append(seconds)
        case.clock[0] += seconds
    monkeypatch.setattr(time, "sleep", sleep)
    return transaction, cid, sleeps


def wait_for_target(case, harness, *, nonce=NONCE, seconds=5):
    return harness.await_admitted_boot(case.host, case.config, case.gate, "target", OPERATION,
                                       SOURCE_BOOT, nonce, seconds=seconds)


@pytest.mark.parametrize("consumed_first", [True, False])
def test_docker_proof_waits_for_exact_legacy_ack_after_gate_consumption(
        incident, monkeypatch, consumed_first):
    c, harness = incident, proof_harness()
    _, cid, sleeps = admitted_target_before_legacy(c, monkeypatch, enter=consumed_first)
    if consumed_first:
        # The old harness asserted ACK here, right after consumption: invalid.
        consumed = c.gate.observe()["slots"]["target"]["consumed"]
        assert consumed is not None and not c.legacy.ACK_PATH.exists()
        assert c.legacy.BOOT_PATH.read_text().strip() == SOURCE_BOOT
        assert not c.host.cutover_boot_consumed(c.config, OPERATION, expected_boot_id=consumed["boot_id"])
    pacing = sleeps.copy()
    def scheduled(seconds):
        pacing.append(seconds)
        c.clock[0] += seconds
        if len(pacing) == 2 and not consumed_first:
            assert c.docker.complete_start(cid, before_legacy=True)
        if len(pacing) == 4:  # the exec'd legacy entrypoint runs late
            boot = c.gate.observe()["slots"]["target"]["consumed"]["boot_id"]
            assert c.legacy.begin_boot(boot, now=c.clock[0])
    monkeypatch.setattr(time, "sleep", scheduled)
    consumed = wait_for_target(c, harness)
    assert len(pacing) == 4
    assert consumed == c.gate.observe()["slots"]["target"]["consumed"]
    assert c.legacy.BOOT_PATH.read_text().strip() == consumed["boot_id"]
    ack = json.loads(c.legacy.ACK_PATH.read_text())
    assert (ack["target_boot_id"], ack["source_boot_id"], ack["nonce"]) == (
        consumed["boot_id"], SOURCE_BOOT, NONCE)
    assert not c.legacy.ACCEPTED_PATH.exists()
    # Observation only: no second Start, entry or authorization.
    assert c.docker.starts == [cid] and len(c.docker.entries) == 1
    assert not c.legacy.begin_boot("unrelated-later-boot-12345678", now=c.clock[0])


@pytest.mark.parametrize("damage", ["foreign-boot", "unadmitted-boot", "wrong-operation",
                                    "accepted-left", "other-nonce", "changed-consumption",
                                    "boot-id-missing"])
def test_docker_proof_refuses_mismatched_ledger_at_once(incident, monkeypatch, damage):
    c, harness = incident, proof_harness()
    _, cid, sleeps = admitted_target_before_legacy(c, monkeypatch,
                                                   enter=damage != "unadmitted-boot")
    consumed = c.gate.observe()["slots"]["target"]["consumed"]
    nonce, error = NONCE, AssertionError
    if damage in {"foreign-boot", "unadmitted-boot"}:
        c.legacy.begin_boot("foreign-boot-12345678", now=c.clock[0])
    elif damage != "changed-consumption":  # that one stays mid-wait
        assert c.legacy.begin_boot(consumed["boot_id"], now=c.clock[0])
    if damage == "wrong-operation":
        ack = json.loads(c.legacy.ACK_PATH.read_text())
        c.legacy.ACK_PATH.unlink()
        c.legacy._write_json(c.legacy.ACK_PATH, {**ack, "cutover_id": "b" * 32}, 0o444)
    elif damage == "accepted-left":
        receipt = json.loads(c.legacy.CUTOVER_RECEIPT_PATH.read_text())
        c.legacy._write_json(c.legacy.ACCEPTED_PATH, receipt, 0o600)
    elif damage == "other-nonce":
        nonce = "another-nonce-12345678"
    elif damage == "changed-consumption":
        observe, views = c.gate.observe, []
        def drifting():
            state = observe()
            views.append(state)
            if len(views) == 2:
                state["slots"]["target"]["consumed"] = {**consumed, "boot_id": "other-boot-12345678"}
            return state
        monkeypatch.setattr(c.gate, "observe", drifting)
    elif damage == "boot-id-missing":
        c.legacy.BOOT_PATH.unlink()
        error = FileNotFoundError
    reason = {"foreign-boot": "other than the admitted", "unadmitted-boot": "other than the admitted",
              "wrong-operation": "exact cutover acknowledgement",
              "accepted-left": "exact cutover acknowledgement", "other-nonce": "another handoff",
              "changed-consumption": "consumed admission changed"}.get(damage)
    with pytest.raises(error, match=reason):
        wait_for_target(c, harness, nonce=nonce)
    # Fails on the first contradictory observation, not by exhausting retries.
    assert len(sleeps) == (1 if damage == "changed-consumption" else 0)
    assert c.docker.starts == [cid]


@pytest.mark.parametrize("enter", [True, False])
def test_docker_proof_times_out_when_admitted_boot_never_commits(incident, monkeypatch, enter):
    c, harness = incident, proof_harness()
    _, cid, sleeps = admitted_target_before_legacy(c, monkeypatch, enter=enter)
    before = c.clock[0]
    with pytest.raises(AssertionError, match="not consumed and committed"):
        wait_for_target(c, harness, seconds=3)
    assert 3 <= c.clock[0] - before < 3.5
    assert not c.legacy.ACK_PATH.exists() and c.legacy.ACCEPTED_PATH.exists()
    assert c.legacy.BOOT_PATH.read_text().strip() == SOURCE_BOOT
    assert (c.gate.observe()["slots"]["target"]["consumed"] is not None) is enter
    assert c.docker.starts == [cid]
