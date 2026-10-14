"""Installer migration boundaries; never invokes Docker or host systemd."""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install-rebuild-helper.sh"
SOURCE = INSTALLER.read_text()
# The checkout's worker revision: what this installer migrates hosts to.
NEW = int(re.search(r"^WORKER_REVISION = ([0-9]+)$",
                    (ROOT / "scripts/mobius-rebuild-host.py").read_text(), re.M).group(1))
PROTOCOL = int((ROOT / "deployment/self-hosted-helper.required").read_text())


def _heredoc(tag: str) -> str:
    return SOURCE.split(f"<<'{tag}'\n", 1)[1].split(f"\n{tag}\n", 1)[0]


def _gate_install(tmp_path: Path):
    """Execute the real installer Python, redirecting only absolute host paths."""
    for name in ("var/lib/mobius-rebuild", "usr/local"):
        (tmp_path / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    code = _heredoc("PY_GATE")
    for prefix in ("/var", "/usr"):
        code = code.replace(f'"{prefix}', f'"{tmp_path}{prefix}')
    source = tmp_path / "admission-source.py"
    if not source.exists():
        source.write_text("#!/usr/bin/env python3\nprint('pinned')\n")
    manual = tmp_path / "manual-source.py"
    if not manual.exists():
        manual.write_text("#!/usr/bin/env python3\nprint('manual')\n")
    result = subprocess.run(
        [sys.executable, "-c", code, str(source), str(manual)], capture_output=True, text=True,
    )
    return result, source, tmp_path / "usr/local/libexec/mobius-boot-admission.py", tmp_path / "var/lib/mobius-rebuild/admission"


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_pinned_code_and_private_state_survive_atomic_reinstall(tmp_path):
    result, source, destination, state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == source.read_bytes()
    adapter = destination.with_name("mobius-manual-cutover.py")
    assert adapter.read_bytes() == (tmp_path / "manual-source.py").read_bytes()
    assert adapter.stat().st_uid == adapter.stat().st_gid == 0
    assert stat.S_IMODE(adapter.stat().st_mode) == 0o755
    assert destination.stat().st_uid == destination.stat().st_gid == 0
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert state.stat().st_uid == state.stat().st_gid == 0
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    marker = state / "consumed.json"
    marker.write_text('{"consumed":true}')
    lock = state / "admission.lock"
    lock.touch(mode=0o600)
    inode = lock.stat().st_ino
    with destination.open("rb") as pinned:
        old = pinned.read()
        source.write_text("#!/usr/bin/env python3\nprint('new')\n")
        result, *_ = _gate_install(tmp_path)
        assert result.returncode == 0, result.stderr
        pinned.seek(0)
        assert pinned.read() == old  # existing bind-mount inode is not overwritten
    assert destination.read_bytes() == source.read_bytes()
    assert marker.read_text() == '{"consumed":true}'
    assert lock.stat().st_ino == inode
    assert not list(destination.parent.glob(".mobius-*.py.*"))


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_unprivileged_process_cannot_replace_code_or_forge_admission():
    program = """
import os, sys
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
# These must succeed before a denial counts: both installed-parent chains
# must be reachable, and the same process must really be able to write.
with open(sys.argv[1], 'rb') as handle:
    assert handle.read()
with open(sys.argv[2], 'w') as handle:
    handle.write('reachable')
for path in sys.argv[3:]:
    try:
        with open(path, 'w') as handle:
            handle.write('forged')
    except PermissionError:
        continue
    raise SystemExit('unprivileged write succeeded')
"""
    # Own the whole hierarchy directly under /tmp. Pytest's private ancestors
    # otherwise make every write fail before reaching the installed boundary.
    with tempfile.TemporaryDirectory(prefix="mobius-admission-permissions-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o755)
        result, _source, destination, state = _gate_install(root)
        assert result.returncode == 0, result.stderr
        for name in ("var", "var/lib", "var/lib/mobius-rebuild", "usr", "usr/local", "usr/local/libexec"):
            (root / name).chmod(0o755)
        control = state.parent / "writable-control"
        control.mkdir()
        control.chmod(0o777)
        before = destination.read_bytes()
        attempt = subprocess.run(
            [sys.executable, "-c", program, str(destination), str(control / "probe"),
             str(destination), str(state / "forged.json")],
            capture_output=True, text=True,
        )
        assert attempt.returncode == 0, attempt.stderr
        assert (control / "probe").read_text() == "reachable"
        assert destination.read_bytes() == before
        assert not (state / "forged.json").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
@pytest.mark.parametrize("unsafe", ["state_symlink", "state_writable", "state_owner", "code_symlink", "code_writable", "parent_symlink"])
def test_pin_refuses_untrusted_boundaries_without_replacing_code(tmp_path, unsafe):
    result, source, destination, state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    original = destination.read_bytes()
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    if unsafe == "state_symlink":
        state.rmdir()
        state.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "state_writable":
        state.chmod(0o777)
    elif unsafe == "state_owner":
        os.chown(state, 65534, 65534)
    elif unsafe == "code_symlink":
        destination.unlink()
        destination.symlink_to(victim)
    elif unsafe == "code_writable":
        destination.chmod(0o777)
    else:
        moved = tmp_path / "moved-libexec"
        destination.parent.rename(moved)
        destination.parent.symlink_to(moved, target_is_directory=True)
    source.write_text("print('must not install')\n")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert "root-controlled" in result.stderr
    assert victim.read_text() == "unchanged"
    if unsafe != "code_symlink":
        assert destination.read_bytes() == original


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises actual root ownership")
def test_invalid_gate_source_does_not_replace_installed_code(tmp_path):
    result, source, destination, _state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    original = destination.read_bytes()
    source.write_text("not valid python !!!")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert destination.read_bytes() == original


def _refusal() -> str:
    return "refuse_unresolved_transaction() {" + SOURCE.split(
        "refuse_unresolved_transaction() {", 1,
    )[1].split("# Never freeze", 1)[0]


@pytest.mark.parametrize("kind", ["legacy", "wrapped", "corrupt", "broken_symlink", "directory"])
def test_existing_transaction_refuses_before_controller_changes(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir()
    tx = state / "transaction.json"
    if kind == "broken_symlink":
        tx.symlink_to(state / "missing")
    elif kind == "directory":
        tx.mkdir()
    else:
        tx.write_text({"legacy": '{"phase":"rollback_started"}',
                       "wrapped": '{"admission_protocol":1}',
                       "corrupt": 'broken'}[kind])
    code = _refusal().replace("/var/lib/mobius-rebuild", str(state))
    published = tmp_path / "controller-changed"
    result = subprocess.run(["bash", "-eu", "-c", code + f'\ntouch "{published}"'],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "Unresolved replacement transaction" in result.stderr
    assert not published.exists()
    assert tx.exists() or tx.is_symlink()
    first_check = SOURCE.index("\nrefuse_unresolved_transaction\n")
    assert first_check < SOURCE.index("systemctl stop")
    assert first_check < SOURCE.index("install -d -m 0700")
    assert SOURCE.index("flock -w 30 8") < SOURCE.index("flock -w 30 9") < SOURCE.index("# BEGIN PINNED")
    assert SOURCE.index("publish-capabilities") < SOURCE.index("flock -u 8")
    assert SOURCE.index("# END PINNED") < SOURCE.index("MOBIUS_REBUILD_LOCK_HELD=1")


def test_transaction_appearing_at_lock_acquisition_refuses_publication(tmp_path):
    state = tmp_path / "state"
    etc = tmp_path / "etc"
    state.mkdir()
    code = _refusal() + SOURCE.split("umask 077\n", 1)[1].split("# BEGIN PINNED", 1)[0]
    code = code.replace("/var/lib/mobius-rebuild", str(state)).replace("/etc/mobius-rebuild", str(etc))
    code = '''systemctl() { if [ "$1" = show ]; then printf 'LoadState=not-found\\nActiveState=inactive\\nJob=\\n'; fi; }
flock() { if [ "${3:-}" = 9 ]; then printf '{"phase":"rollback_started"}' >"$STATE/transaction.json"; fi; }
''' + code + '\ntouch "$PUBLISHED"\n'
    published = tmp_path / "controller-changed"
    result = subprocess.run(["bash", "-eu", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "STATE": str(state), "PUBLISHED": str(published),
                                 "RESOLVED": str(tmp_path / "resolved")})
    assert result.returncode == 1
    assert "Unresolved replacement transaction" in result.stderr
    assert not published.exists()
    assert (state / "transaction.json").exists()


def test_installer_holds_dispatch_fence_while_replacement_lock_is_owned(tmp_path):
    start = SOURCE.index("exec 8>>/var/lib/mobius-rebuild/candidate.lock")
    end = SOURCE.index("\nrefuse_unresolved_transaction\n", start)
    locking = SOURCE[start:end].replace("/var/lib/mobius-rebuild", str(tmp_path))
    process = subprocess.Popen(
        ["bash", "-eu", "-c", locking + "\npython3 -c 'import os; os.fstat(8); os.fstat(9)'\nprintf 'locked\\n'; read -r release"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        for name in ("candidate.lock", "replace.lock"):
            with (tmp_path / name).open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.communicate("release\n", timeout=5)
        assert process.returncode == 0
        for name in ("candidate.lock", "replace.lock"):
            with (tmp_path / name).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_busy_dispatch_refuses_without_waiting_forever_or_stopping_recovery(tmp_path):
    start = SOURCE.index("exec 8>>/var/lib/mobius-rebuild/candidate.lock")
    end = SOURCE.index("\nrefuse_unresolved_transaction\n", start)
    # Keep the real refusal path, shorten only its documented 30-second budget.
    locking = SOURCE[start:end].replace("/var/lib/mobius-rebuild", str(tmp_path)).replace("-w 30", "-w 0")
    with (tmp_path / "candidate.lock").open("a") as active:
        fcntl.flock(active, fcntl.LOCK_EX)
        result = subprocess.run(["bash", "-eu", "-c", locking], capture_output=True, text=True, timeout=5)
        assert result.returncode == 1
        assert "controller dispatch is active" in result.stderr
        assert not (tmp_path / "replace.lock").exists()
        # The installer never terminates the existing lock owner.
        assert active.fileno() >= 0


@pytest.mark.parametrize("app,accepted", [
    ({"image": "original", "entrypoint": ["/usr/bin/tini", "--"]}, True),
    ({"entrypoint": ["python3", "/run/mobius-boot-admission.py"]}, False),
    ({"volumes": [{"target": "/run/mobius-admission"}]}, False),
    ({"labels": {"io.mobius.admission.token": "stale-attempt"}}, False),
])
def test_base_snapshot_cannot_capture_attempt_wrapper(tmp_path, app, accepted):
    config = tmp_path / "compose.json"
    config.write_text(json.dumps({"services": {"app": app}}))
    result = subprocess.run([sys.executable, "-c", _heredoc("PY_BASE"), str(config)],
                            capture_output=True, text=True)
    assert (result.returncode == 0) is accepted
    # Helper-created live containers may be wrapped: copy only their base,
    # never resolve the currently active per-attempt image override into it.
    snapshot = SOURCE.split('if [[ -n $FROZEN_SOURCE ]]; then\n  cp ', 1)[1].split('PUBLISH=(', 1)[0]
    assert snapshot.startswith('"$FROZEN_SOURCE" "$STAGE/compose.yml"')
    assert "image.override.yml" not in snapshot


def test_gate_is_reviewed_input_and_revision_is_six():
    for section in (SOURCE.split("ls-files --error-unmatch", 1)[1].split("# The running", 1)[0],
                    SOURCE.split("diff --quiet HEAD --", 1)[1].split("ARGS=()", 1)[0]):
        assert "scripts/mobius-boot-admission.py" in section
        assert "scripts/mobius-manual-cutover.py" in section
    assert SOURCE.count("scripts/mobius-rebuild-launcher.py scripts/mobius-boot-admission.py") == 3
    assert "# Helper protocol revision: 6 " in SOURCE
    assert (ROOT / "deployment/self-hosted-helper.required").read_text().strip() == "6"


def test_installer_does_not_boot_or_clear_application_state():
    # Inspect executable shell, excluding generated systemd units and Python.
    shell = SOURCE
    for tag in ("EOF", "PY", "PY_GATE", "PY_BASE", "PY_PUBLISH"):
        shell = re.sub(r"<<'?" + tag + r"'?\n.*?\n" + tag + r"\n", "\n", shell, flags=re.S)
    assert not re.search(r"docker\s+(?:start|restart|stop|rm|kill|run)\b", shell)
    assert not re.search(r"docker compose[^\n]*\b(?:up|down|start|restart)\b", shell)
    assert not re.search(r"^/usr/local/libexec/mobius-rebuild-host (?:reconcile|run)$", shell, re.M)
    assert '/usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities' in shell
    assert ".restart-ledger" not in shell
    assert "-L /var/lib/mobius-rebuild/transaction.json ]]; then" in shell  # refusal, never unlink
    assert not re.search(r"(?:rm|unlink)[^\n]*transaction.json", shell)
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


@pytest.mark.parametrize("legacy,admitted,accepted", [
    ("a" * 64, "", True),
    ("", "b" * 64, True),
    ("a" * 64, "a" * 64, True),
    ("a" * 64, "b" * 64, False),
    ("", "", False),
    ("short-id", "", False),
    ("", "b" * 64 + "\n" + "c" * 64, False),
])
def test_discovery_is_exact_union_of_legacy_and_admission_projects(tmp_path, legacy, admitted, accepted):
    function = "discover_app() {" + SOURCE.split("discover_app() {", 1)[1].split("\nCID=$(discover_app)", 1)[0]
    fake_docker = '''docker() {
  case "$*" in
    *io.mobius.admission.project*) printf '%s\\n' "$ADMITTED" ;;
    *com.docker.compose.project*) printf '%s\\n' "$LEGACY" ;;
    *) return 99 ;;
  esac
}
'''
    result = subprocess.run(["bash", "-eu", "-c", fake_docker + function + "\nCID=$(discover_app)\nprintf '%s' \"$CID\""],
                            env={**os.environ, "PROJECT": "original", "LEGACY": legacy, "ADMITTED": admitted},
                            capture_output=True, text=True)
    assert (result.returncode == 0) is accepted
    if accepted:
        assert result.stdout == (legacy or admitted)


def test_discovery_does_not_treat_failed_query_as_empty_project():
    function = "discover_app() {" + SOURCE.split("discover_app() {", 1)[1].split("\nCID=$(discover_app)", 1)[0]
    result = subprocess.run(["bash", "-eu", "-c", "docker() { return 1; }\n" + function + "\ndiscover_app"],
                            env={**os.environ, "PROJECT": "original"}, capture_output=True, text=True)
    assert result.returncode == 1


def _wrapped_identity(tmp_path, *, deployment="original-app-1", source="generated"):
    frozen = tmp_path / "etc/mobius-rebuild"
    attempts = frozen / "attempts"
    attempts.mkdir(parents=True)
    cid, token, operation = "a" * 64, "b" * 32, "c" * 32
    image = "sha256:" + "d" * 64
    # The attempt has its own Compose project but the frozen deployment name.
    attempt_project = "mobius-admission-" + token
    labels = {"io.mobius.admission.project": "original", "io.mobius.admission.operation": operation,
              "io.mobius.admission.role": "rollback", "io.mobius.admission.token": token}
    entrypoint = ["python3", "-I", "-S", "/run/mobius-boot-admission.py", "enter",
                  "--state-dir", "/run/mobius-admission", "--data-dir", "/data",
                  "--token", token, "--", "/usr/bin/tini", "-s", "--"]
    app = {"image": image, "container_name": deployment, "labels": labels,
           "entrypoint": entrypoint, "command": ["./scripts/entrypoint.sh"]}
    expected = {"name": attempt_project, "services": {"app": app}}
    (frozen / "config.v4.json").write_text(json.dumps({
        "version": 4, "project": "original", "data_dir": "/host/data",
        "deployment_name": deployment, "name_source": source}))
    (frozen / "image.override.yml").write_text(json.dumps(expected))
    attempt = attempts / (token + ".json")
    attempt.write_text(json.dumps(expected))
    actual = {"Id": cid, "Image": image, "Name": "/" + deployment, "Entrypoint": entrypoint,
              "Cmd": app["command"], "Labels": {**labels,
              "com.docker.compose.project": attempt_project, "com.docker.compose.service": "app",
              "com.docker.compose.container-number": "1", "com.docker.compose.oneoff": "False",
              "com.docker.compose.project.working_dir": str(attempts),
              "com.docker.compose.project.config_files": str(attempt)},
              "Mounts": [
                  {"Destination": "/run/mobius-boot-admission.py", "Source": "/usr/local/libexec/mobius-boot-admission.py", "Type": "bind", "RW": False},
                  {"Destination": "/run/mobius-admission", "Source": "/var/lib/mobius-rebuild/admission/" + operation, "Type": "bind", "RW": True},
                  {"Destination": "/data", "Source": "/host/data", "Type": "volume", "RW": True},
              ]}
    code = _heredoc("PY_ADMISSION").replace("/etc/mobius-rebuild", str(frozen))
    if os.geteuid() != 0:
        # Unprivileged runs substitute only the trusted owner; root runs keep 0.
        assert code.count("info.st_uid == 0") == 1
        code = code.replace("info.st_uid == 0", f"info.st_uid == {os.geteuid()}")
    return frozen, attempt, actual, code, cid


@pytest.mark.parametrize("mutation", [None, "owner", "compose_project", "token", "entrypoint", "command",
                                     "image", "code_writable", "state_readonly", "state_source", "data_source",
                                     "duplicate_mount", "config_files", "immutable_changed", "immutable_symlink",
                                     "immutable_writable", "immutable_directory_symlink", "config_v3",
                                     "name_source", "live_renamed", "token_derived_name",
                                     "attempt_shares_original_project", "frozen_name_changed",
                                     "stable_spelling", "frozen_data_dir", "working_dir"])
def test_wrapped_reinstall_requires_frozen_identity_and_immutable_attempt(tmp_path, mutation):
    frozen, attempt, actual, code, cid = _wrapped_identity(tmp_path)
    configuration = json.loads((frozen / "config.v4.json").read_text())
    if mutation == "owner":
        (frozen / "config.v4.json").write_text(json.dumps({**configuration, "project": "wrong"}))
    elif mutation == "config_v3":
        # Admission-managed apps exist only with frozen v4 identity: no shim.
        (frozen / "config.v4.json").unlink()
        (frozen / "config.json").write_text(json.dumps({"version": 3, "project": "original", "data_dir": "/host/data"}))
    elif mutation == "name_source":
        (frozen / "config.v4.json").write_text(json.dumps({**configuration, "name_source": "renamed"}))
    elif mutation == "frozen_name_changed":
        (frozen / "config.v4.json").write_text(json.dumps({**configuration, "deployment_name": "original-app-2"}))
    elif mutation == "live_renamed":
        actual["Name"] = "/renamed-app"
    elif mutation in ("token_derived_name", "attempt_shares_original_project"):
        document = json.loads(attempt.read_text())
        if mutation == "token_derived_name":
            # The superseded per-attempt name would let raw Compose add a writer.
            name = "mobius-admission-" + "b" * 32
            document["services"]["app"]["container_name"] = name
            actual["Name"] = "/" + name
        else:
            document["name"] = "original"
            actual["Labels"]["com.docker.compose.project"] = "original"
        attempt.write_text(json.dumps(document))
        (frozen / "image.override.yml").write_text(json.dumps(document))
    elif mutation == "compose_project":
        actual["Labels"]["com.docker.compose.project"] = "original"
    elif mutation == "token":
        actual["Labels"]["io.mobius.admission.token"] = "e" * 32
    elif mutation == "entrypoint":
        actual["Entrypoint"] = ["./scripts/entrypoint.sh"]
    elif mutation == "command":
        actual["Cmd"] = ["unexpected"]
    elif mutation == "image":
        actual["Image"] = "sha256:" + "e" * 64
    elif mutation == "code_writable":
        actual["Mounts"][0]["RW"] = True
    elif mutation == "state_readonly":
        actual["Mounts"][1]["RW"] = False
    elif mutation == "state_source":
        actual["Mounts"][1]["Source"] = "/data/forged-state"
    elif mutation == "data_source":
        actual["Mounts"][2]["Source"] = "/wrong-data"
    elif mutation == "duplicate_mount":
        actual["Mounts"].append(actual["Mounts"][0])
    elif mutation == "working_dir":
        # Not where Compose put this attempt's project (the worker's cwd).
        actual["Labels"]["com.docker.compose.project.working_dir"] = str(frozen)
    elif mutation == "config_files":
        actual["Labels"]["com.docker.compose.project.config_files"] = "/arbitrary/label/path"
    elif mutation == "immutable_changed":
        attempt.write_text("{}")
    elif mutation == "immutable_symlink":
        attempt.unlink()
        attempt.symlink_to(frozen / "image.override.yml")
    elif mutation == "immutable_writable":
        attempt.chmod(0o666)
    elif mutation == "immutable_directory_symlink":
        relocated = frozen / "other"
        attempt.parent.rename(relocated)
        attempt.parent.symlink_to(relocated, target_is_directory=True)
    # The resolved stable source and Docker's view of /data; the frozen
    # data_dir must be one of them (the Mountpoint spelling predates protocol 6).
    paths = {"stable_spelling": ["/srv/data", "/host/data"],
             "frozen_data_dir": ["/srv/data", "/host/mountpoint"]}.get(mutation, ["/host/data", "/host/data"])
    if mutation == "frozen_data_dir":
        actual["Mounts"][2]["Source"] = "/host/mountpoint"
    result = subprocess.run([sys.executable, "-c", code, "original", cid, json.dumps(actual), *paths],
                            capture_output=True, text=True)
    assert (result.returncode == 0) is (mutation in {None, "stable_spelling"}), result.stderr


def test_wrapped_reinstall_preserves_current_pointer_not_wrapped_base():
    assert ('if [[ $ADMISSION_MANAGED == 0 ]]; then\ncat >"$STAGE/image.override.yml"' in SOURCE)
    assert 'cp "$FROZEN_SOURCE" "$STAGE/compose.yml"' in SOURCE
    assert '[[ $(discover_app) == "$CID" ]]' in SOURCE
    locked = SOURCE.split("flock -w 30 9", 1)[1]
    assert locked.index("verify_admission_identity") < locked.index("# BEGIN PINNED")
    assert 'MOBIUS_REBUILD_LAUNCHER=2 \\\n  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities' in SOURCE


@pytest.mark.skipif(os.geteuid() != 0, reason="exercises root-pinned helper publication")
def test_invalid_adapter_source_prevents_publishing_either_helper(tmp_path):
    result, gate_source, destination, _state = _gate_install(tmp_path)
    assert result.returncode == 0, result.stderr
    adapter = destination.with_name("mobius-manual-cutover.py")
    old_gate, old_adapter = destination.read_bytes(), adapter.read_bytes()
    gate_source.write_text("print('new gate')\n")
    (tmp_path / "manual-source.py").write_text("invalid python !!!")
    result, *_ = _gate_install(tmp_path)
    assert result.returncode != 0
    assert destination.read_bytes() == old_gate
    assert adapter.read_bytes() == old_adapter


def test_generated_unit_budgets_cover_pull_full_cutover_and_recovery(tmp_path):
    import ast
    import configparser

    source = (ROOT / "scripts/mobius-rebuild-host.py").read_text()
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    pulls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "docker_command" and node.args
             and isinstance(node.args[0], ast.List)
             and any(isinstance(value, ast.Constant) and value.value == "pull"
                     for value in node.args[0].elts)]
    assert len(pulls) == 1
    pull_budget = next(ast.literal_eval(item.value) for item in pulls[0].keywords if item.arg == "timeout")
    # Initial cutover: remove source; create/start target; fence failed target;
    # create/start rollback. Reconciliation budgets the same full recovery pass.
    mutation_budget = 6 * constants["COMPOSE_MUTATION_SECONDS"]
    readiness_budget = constants["TARGET_HEALTH_SECONDS"] + constants["ROLLBACK_HEALTH_SECONDS"]
    recovery_budget = mutation_budget + readiness_budget + 600
    run_budget = pull_budget + recovery_budget + 1800
    start = SOURCE.index('cat >"$STAGE/mobius-rebuild.service"')
    end = SOURCE.index("for unit in mobius-rebuild.service", start)
    subprocess.run(["bash", "-eu", "-c", SOURCE[start:end]], check=True,
                   env={**os.environ, "DATA_SOURCE": str(tmp_path / "data"), "STAGE": str(tmp_path)})
    main, recovery = configparser.ConfigParser(), configparser.ConfigParser()
    main.read(tmp_path / "mobius-rebuild.service")
    recovery.read(tmp_path / "mobius-rebuild-reconcile.service")
    assert main["Service"].getint("TimeoutStartSec") == run_budget == 9000
    assert main["Service"].getint("TimeoutStopSec") == recovery_budget == 3600
    assert recovery["Service"].getint("TimeoutStartSec") == recovery_budget


def _dispatch_quiescence():
    return SOURCE.split('# BEGIN MIGRATION DISPATCH QUIESCENCE\n', 1)[1].split(
        '# END MIGRATION DISPATCH QUIESCENCE', 1,
    )[0]


def _fake_systemd():
    # Files are isolated unit state, not the host service manager. A selected
    # delayed child marks the oneshot activating until it is explicitly resumed.
    return r'''systemctl() {
  printf '%s\n' "$*" >>"$FIXTURE/calls"
  local verb=$1 unit=$2 active=inactive job= load=loaded
  case "$verb" in
    show)
      [[ ! -f "$FIXTURE/$unit.unreadable" ]] || return 1
      [[ ! -f "$FIXTURE/$unit.state" ]] || active=$(cat "$FIXTURE/$unit.state")
      [[ ! -f "$FIXTURE/$unit.job" ]] || job=$(cat "$FIXTURE/$unit.job")
      [[ ! -f "$FIXTURE/$unit.missing" ]] || load=not-found
      printf 'Job=%s\nActiveState=%s\nLoadState=%s\n' "$job" "$active" "$load"
      ;;
    stop)
      [[ ! -f "$FIXTURE/$unit.stop-fails" ]] || return 1
      printf inactive >"$FIXTURE/$unit.state"
      if [[ $unit == mobius-rebuild-reconcile.timer && -f "$FIXTURE/late-selection" ]]; then
        printf activating >"$FIXTURE/mobius-rebuild.service.state"
      fi
      ;;
    start) printf active >"$FIXTURE/$unit.state" ;;
    *) return 99 ;;
  esac
}
'''


def _run_quiescence(tmp_path, tail='touch "$FIXTURE/published"', extra=''):
    return subprocess.run(
        ['bash', '-eu', '-c', _fake_systemd() + extra + _dispatch_quiescence() + '\n' + tail],
        env={**os.environ, 'FIXTURE': str(tmp_path), 'RESOLVED': str(tmp_path / 'resolved')},
        capture_output=True, text=True, timeout=5,
    )


@pytest.mark.parametrize('service', ['mobius-rebuild.service', 'mobius-rebuild-reconcile.service'])
@pytest.mark.parametrize('state,job', [('active', ''), ('activating', ''), ('deactivating', ''),
                                    ('inactive', '71'), ('failed', '71')])
def test_migration_refuses_active_or_queued_legacy_services_and_restores_dispatch(tmp_path, service, state, job):
    (tmp_path / 'mobius-rebuild.path.state').write_text('active')
    (tmp_path / 'mobius-rebuild-reconcile.timer.state').write_text('inactive')
    (tmp_path / (service + '.state')).write_text(state)
    (tmp_path / (service + '.job')).write_text(job)
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1, result.stderr
    assert 'leave recovery running' in result.stderr
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.path.state').read_text() == 'active'
    assert (tmp_path / 'mobius-rebuild-reconcile.timer.state').read_text() == 'inactive'
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert not any(line.startswith(('stop mobius-rebuild.service', 'stop mobius-rebuild-reconcile.service'))
                   for line in calls)
    assert (tmp_path / (service + '.state')).read_text() == state


def test_selection_just_before_dispatch_stop_is_observed_before_mutation(tmp_path):
    (tmp_path / 'late-selection').touch()
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.service.state').read_text() == 'activating'


@pytest.mark.parametrize('problem', ['stop-fails', 'unreadable'])
def test_dispatch_failure_refuses_publication_and_restores_previous_timer(tmp_path, problem):
    (tmp_path / 'mobius-rebuild.path.state').write_text('inactive')
    (tmp_path / 'mobius-rebuild-reconcile.timer.state').write_text('active')
    (tmp_path / ('mobius-rebuild-reconcile.timer.' + problem)).touch()
    result = _run_quiescence(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / 'published').exists()
    assert (tmp_path / 'mobius-rebuild.path.state').read_text() == 'inactive'
    assert (tmp_path / 'mobius-rebuild-reconcile.timer.state').read_text() == 'active'


def test_quiescent_dispatch_is_held_until_publication_and_restored_on_failure(tmp_path):
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        (tmp_path / (unit + '.state')).write_text('active')
    tail = '''[[ $(cat "$FIXTURE/mobius-rebuild.path.state") == inactive ]]
[[ $(cat "$FIXTURE/mobius-rebuild-reconcile.timer.state") == inactive ]]
touch "$FIXTURE/published"
exit 42
'''
    result = _run_quiescence(tmp_path, tail)
    assert result.returncode == 42
    assert (tmp_path / 'published').exists()
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        assert (tmp_path / (unit + '.state')).read_text() == 'active'
    assert SOURCE.index('# END MIGRATION DISPATCH QUIESCENCE') < SOURCE.index('install -d -m 0700')


def test_launcher1_selected_child_before_lock_is_not_fenced_by_lock_order_alone(tmp_path):
    # Deterministic negative control: a selected legacy child is paused BEFORE
    # flock. Neither the new dispatch lock nor a bounded worker flock timeout
    # constrains this pause. It can execute after both installer locks release.
    child = r'''
import fcntl, pathlib, sys
root = pathlib.Path(sys.argv[1])
(root / 'mobius-rebuild.service.state').write_text('activating')
print('selected-before-lock', flush=True)
sys.stdin.readline()
with (root / 'replace.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (root / 'legacy-ran').write_text((root / 'config').read_text())
'''
    process = subprocess.Popen([sys.executable, '-c', child, str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'selected-before-lock'
        with (tmp_path / 'candidate.lock').open('a') as dispatch, (tmp_path / 'replace.lock').open('a') as replacement:
            fcntl.flock(dispatch, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(replacement, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Actual new preflight refuses before changing config or code,
            # despite both cooperative locks being available to the installer.
            result = _run_quiescence(tmp_path)
            assert result.returncode == 1
            assert not (tmp_path / 'published').exists()
            assert process.poll() is None  # installer did not kill active recovery
            (tmp_path / 'config').write_text('new config if lock-only installer published')
        process.communicate('\n', timeout=5)
        assert process.returncode == 0
        assert (tmp_path / 'legacy-ran').read_text() == 'new config if lock-only installer published'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_incomplete_systemd_observation_cannot_prove_quiescence(tmp_path):
    result = _run_quiescence(tmp_path, extra="""
systemctl() { printf 'LoadState=loaded\\nActiveState=inactive\\n'; }
""")
    assert result.returncode == 1
    assert 'Cannot inspect dispatch unit' in result.stderr
    assert not (tmp_path / 'published').exists()


def test_successful_publication_does_not_restore_obsolete_dispatch_state(tmp_path):
    result = _run_quiescence(tmp_path, tail="""
systemctl start mobius-rebuild.path
systemctl start mobius-rebuild-reconcile.timer
MIGRATION_COMPLETE=1
""")
    assert result.returncode == 0, result.stderr
    for unit in ('mobius-rebuild.path', 'mobius-rebuild-reconcile.timer'):
        assert (tmp_path / (unit + '.state')).read_text() == 'active'


@pytest.mark.parametrize("observed_command", [None, [], ["unexpected"], False])
def test_wrapped_reinstall_preserves_literal_argv_and_empty_command(tmp_path, observed_command):
    frozen, attempt, actual, code, cid = _wrapped_identity(tmp_path)
    document = json.loads(attempt.read_text())
    app = document["services"]["app"]
    literal = "echo ${MOBIUS_BOOT_ID:-fallback}; echo $$pid"
    app["entrypoint"].extend(["/bin/sh", "-c", literal.replace("$", "$$")])
    app["command"] = []
    actual["Entrypoint"] = app["entrypoint"][:-1] + [literal]
    actual["Cmd"] = observed_command
    attempt.write_text(json.dumps(document))
    (frozen / "image.override.yml").write_text(json.dumps(document))
    result = subprocess.run([sys.executable, "-c", code, "original", cid, json.dumps(actual),
                             "/host/data", "/host/data"],
                            capture_output=True, text=True)
    assert (result.returncode == 0) is (observed_command is None or observed_command == []), result.stderr


def _identity(tmp_path, *, managed=False, name="/original-app-1", explicit=None, labels=None,
              config=None):
    """Run the real deployment-name freeze against isolated JSON fixtures."""
    frozen = tmp_path / "etc/mobius-rebuild"
    frozen.mkdir(parents=True, exist_ok=True)
    if config is not None:
        # A version-3 document at the v4 path is still refused as version 3.
        (frozen / "config.v4.json").write_text(json.dumps(config))
    app = {"image": "original"}
    if explicit is not None:
        app["container_name"] = explicit
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"services": {"app": app}}))
    observed = {"com.docker.compose.project": "original", "com.docker.compose.service": "app",
                "com.docker.compose.container-number": "1", "com.docker.compose.oneoff": "False"}
    observed.update(labels or {})
    observed = {key: value for key, value in observed.items() if value is not None}
    code = _heredoc("PY_IDENTITY").replace("/etc/mobius-rebuild", str(frozen))
    result = subprocess.run(
        [sys.executable, "-c", code, "original", "1" if managed else "0", str(resolved),
         json.dumps({"Name": name, "Labels": observed})],
        capture_output=True, text=True,
    )
    return result, (json.loads(result.stdout) if result.returncode == 0 else None)


@pytest.mark.parametrize("name,explicit,labels,frozen", [
    ("/mobius-prod", "mobius-prod", {}, ("mobius-prod", "explicit")),
    # An explicit name need not follow the Compose pattern or the label number.
    ("/mobius-prod", "mobius-prod", {"com.docker.compose.container-number": "3"}, ("mobius-prod", "explicit")),
    ("/original-app-1", None, {}, ("original-app-1", "generated")),
    ("/original_app_1", None, {}, ("original_app_1", "generated")),
    # A non-first replica index is preserved only as an explicit stable name.
    ("/original-app-2", "original-app-2", {"com.docker.compose.container-number": "2"},
     ("original-app-2", "explicit")),
])
def test_legacy_app_freezes_its_unrenamed_original_name(tmp_path, name, explicit, labels, frozen):
    result, identity = _identity(tmp_path, name=name, explicit=explicit, labels=labels)
    assert result.returncode == 0, result.stderr
    assert identity == {"deployment_name": frozen[0], "name_source": frozen[1]}


@pytest.mark.parametrize("name,explicit,labels", [
    ("/renamed", None, {}),                                       # docker rename of a generated app
    ("/original-app-2", None, {}),                                # name disagrees with its label number
    ("/original-app-1-x", None, {}),
    ("/other-app-1", None, {}),                                   # another project's generated name
    ("/original-app-run-0123abcd", None, {}),                     # compose run one-off
    ("/original-app-1", None, {"com.docker.compose.oneoff": "True"}),
    ("/original-app-1", None, {"com.docker.compose.service": "caddy"}),
    ("/original-app-1", None, {"com.docker.compose.project": "other"}),
    ("/original-app-1", None, {"com.docker.compose.container-number": None}),
    ("/original-app-01", None, {"com.docker.compose.container-number": "01"}),
    ("/original-app-1", "mobius-prod", {}),                       # explicit name not the live name
    ("/mobius-prod", "mobius-renamed", {}),                       # renamed explicit source
    ("/original-app-1", "original-app-1/x", {}),                  # invalid explicit name
    ("original-app-1", None, {}),                                 # not Docker's absolute name form
    ("/original-app-1", "", {}),
    ("/" + "m" * 129, "m" * 129, {}),                             # beyond the host's name bound
])
def test_legacy_app_with_renamed_or_unproven_name_is_refused(tmp_path, name, explicit, labels):
    result, _ = _identity(tmp_path, name=name, explicit=explicit, labels=labels)
    assert result.returncode != 0
    assert "Refusing to freeze the deployment container name" in result.stderr


@pytest.mark.parametrize("name,number", [
    ("/original-app-2", "2"), ("/original_app_2", "2"), ("/original-app-10", "10"),
])
def test_generated_index_other_than_one_requires_explicit_stable_name(tmp_path, name, number):
    # Raw Compose on an emptied original project recreates index 1, so a frozen
    # index-2 name could never collide with it: refuse, never install that hole.
    result, _ = _identity(tmp_path, name=name, labels={"com.docker.compose.container-number": number})
    assert result.returncode != 0
    assert f"generated app index {number}" in result.stderr
    assert f"set the app's container_name to {name[1:]!r}" in result.stderr
    assert "rerun this installer" in result.stderr
    # The documented migration path is then accepted as an explicit name.
    result, identity = _identity(tmp_path, name=name, explicit=name[1:],
                                 labels={"com.docker.compose.container-number": number})
    assert result.returncode == 0, result.stderr
    assert identity == {"deployment_name": name[1:], "name_source": "explicit"}


def _v4(**changes):
    return {"version": 4, "project": "original", "data_dir": "/host/data",
            "deployment_name": "original-app-1", "name_source": "generated", **changes}


@pytest.mark.parametrize("config,name,explicit", [
    (_v4(), "/original-app-1", None),
    (_v4(deployment_name="original_app_1"), "/original_app_1", None),
    (_v4(deployment_name="mobius-prod", name_source="explicit"), "/mobius-prod", "mobius-prod"),
])
def test_managed_reinstall_preserves_frozen_name_exactly(tmp_path, config, name, explicit):
    # Attempt labels replace the original project/number labels; frozen fields win.
    labels = {"com.docker.compose.project": "mobius-admission-" + "b" * 32}
    result, identity = _identity(tmp_path, managed=True, name=name, explicit=explicit,
                                 labels=labels, config=config)
    assert result.returncode == 0, result.stderr
    assert identity == {"deployment_name": config["deployment_name"],
                        "name_source": config["name_source"]}


@pytest.mark.parametrize("config,name,explicit", [
    ({"version": 3, "project": "original", "data_dir": "/host/data"}, "/original-app-1", None),
    (_v4(project="other"), "/original-app-1", None),
    (_v4(), "/original-app-2", None),                             # live name differs from frozen
    (_v4(), "/mobius-admission-" + "b" * 32, None),               # superseded per-attempt name
    (_v4(), "/original-app-1", "original-app-1"),                 # base gained an explicit name
    (_v4(deployment_name="renamed"), "/renamed", None),           # generated source must match pattern
    (_v4(deployment_name="original-app-2"), "/original-app-2", None),  # generated index must be 1
    (_v4(deployment_name="original_app_2"), "/original_app_2", None),
    (_v4(deployment_name="mobius-prod", name_source="explicit"), "/mobius-prod", None),
    (_v4(deployment_name="mobius-prod", name_source="explicit"), "/mobius-prod", "mobius-renamed"),
    (_v4(name_source="observed"), "/original-app-1", None),
    (_v4(deployment_name=None), "/original-app-1", None),
])
def test_managed_reinstall_refuses_inconsistent_or_renamed_identity(tmp_path, config, name, explicit):
    result, _ = _identity(tmp_path, managed=True, name=name, explicit=explicit, config=config)
    assert result.returncode != 0
    assert "Refusing to freeze the deployment container name" in result.stderr


def test_config_v4_freezes_only_the_validated_identity(tmp_path):
    frozen = tmp_path / "etc/mobius-rebuild"
    frozen.mkdir(parents=True)
    code = _heredoc("PY")
    staged = frozen / "config.v4.json"
    identity = {"deployment_name": "original_app_1", "name_source": "generated"}
    result = subprocess.run([sys.executable, "-c", code, "original", "/host/data", json.dumps(identity),
                             str(staged)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(staged.read_text()) == {
        "version": 4, "project": "original", "data_dir": "/host/data", **identity}
    before = staged.read_bytes()
    result = subprocess.run([sys.executable, "-c", code, "original", "/host/data",
                             json.dumps({**identity, "extra": True}), str(staged)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert staged.read_bytes() == before


def test_deployment_name_is_frozen_before_mutation_and_rechecked_under_locks():
    preflight = SOURCE.index("\nIDENTITY=$(deployment_identity)\n")
    assert SOURCE.index("expected-networks") < preflight < SOURCE.index("umask 077")
    assert preflight < SOURCE.index("systemctl stop")
    locked = SOURCE.split("flock -w 30 9", 1)[1]
    recheck = locked.index('[[ $(deployment_identity) == "$IDENTITY" ]]')
    assert locked.index('[[ $(discover_app) == "$CID" ]]') < recheck < locked.index("# BEGIN PINNED")
    assert locked.index("verify_admission_identity") < recheck
    assert 'python3 - "$PROJECT" "$DATA_SOURCE" "$IDENTITY" "$STAGE/config.v4.json" <<\'PY\'' in SOURCE
    # Released config.json is version 3 only; config.v4.json is version 4 only.
    assert '(("config.v4.json", 4), ("config.json", 3))' in _heredoc("PY_EXISTING")
    assert 'configuration.get("version") == 4' in _heredoc("PY_ADMISSION")


def test_identity_recheck_refuses_a_rename_during_preflight(tmp_path):
    line = SOURCE.split('[[ $(deployment_identity) == "$IDENTITY" ]]', 1)[1].split("\n\n", 1)[0]
    code = ('deployment_identity() { printf \'{"deployment_name":"renamed","name_source":"generated"}\'; }\n'
            'IDENTITY=\'{"deployment_name":"original-app-1","name_source":"generated"}\'\n'
            '[[ $(deployment_identity) == "$IDENTITY" ]]' + line + '\ntouch "$PUBLISHED"\n')
    published = tmp_path / "published"
    result = subprocess.run(["bash", "-eu", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PUBLISHED": str(published)})
    assert result.returncode == 1
    assert "deployment name changed" in result.stderr
    assert not published.exists()


def test_installer_preserves_existing_root_controller_state():
    # Host-owned removal tombstones and other records under the private state
    # directory are never deleted or reset; the only removal is its temp file.
    shell = SOURCE
    for tag in ("EOF", "PY", "PY_GATE", "PY_BASE", "PY_IDENTITY", "PY_ADMISSION", "PY_OWNER", "PY_DATA",
                "PY_PUBLISH"):
        shell = re.sub(r"<<'?" + tag + r"'?\n.*?\n" + tag + r"\n", "\n", shell, flags=re.S)
    removals = re.findall(r"\b(?:rm|unlink|rmdir|truncate|shred)\b[^\n]*", shell)
    allowed = ('rm -f "$RESOLVED"', '[[ -z ${STAGE:-} ]] || rm -rf -- "$STAGE"')
    assert removals and all(item in allowed or item.startswith('rm -f "$RESOLVED"\'')
                            or item == 'rm -rf -- "$STAGE"' for item in removals), removals
    assert "\nSTAGE=$(mktemp -d)\n" in SOURCE
    assert not re.search(r">\s*/var/lib/mobius-rebuild/(?!candidate\.lock|replace\.lock)", shell)
    for tag in ("PY_GATE", "PY_IDENTITY", "PY", "PY_PUBLISH"):
        # The publisher may remove only its own unrenamed temporary file.
        code = _heredoc(tag).replace("os.unlink(temporary)", "")
        assert not re.search(r"rmtree|\.unlink\((?!missing_ok)|os\.remove|os\.rmdir", code), tag



# ---------------------------------------------------------------------------
# Staged publication and the single migration commit, in a disposable host.
#
# The real publication section, the real new worker (scripts/mobius-rebuild-
# host.py) and the real launcher 2 run with every host path rewritten under the
# test directory. Docker and systemd are fakes; nothing outside tmp_path is
# touched, and no worker path that reaches Docker is executed. These are
# simulations, not the historical code: the released revision-10 worker is
# represented only by its config predicate (copied from the release), revision
# 12 (used only by unreleased review heads, reading config.json as version 4)
# by its config predicate and launcher-1 run refusal, and the released
# launcher 1 by an opaque installed stub plus a re-implementation of its
# candidate settle rule in _reboot_dispatch. The new worker and launcher 2 are
# the real checkout files.

HOST_PREFIXES = ("/var/lib/mobius-rebuild", "/etc/mobius-rebuild", "/etc/systemd/system",
                 "/usr/local/libexec")
PUBLICATION = SOURCE[SOURCE.index("# BEGIN DURABLE STAGED PUBLICATION"):
                     SOURCE.index("MIGRATION_COMPLETE=1\n") + len("MIGRATION_COMPLETE=1\n")]
COMMIT = 'MOBIUS_REBUILD_LOCK_HELD=1 \\\n  /usr/bin/python3 -I -S "$ROOT/scripts/mobius-rebuild-host.py" adopt-self\n'
CLEANUP = "migration_cleanup() {" + SOURCE.split("migration_cleanup() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
RELEASED_LAUNCHER = b"#!/usr/bin/python3\n# released launcher revision 1 (opaque stub)\nLAUNCHER_REVISION = 1\n"
RELEASED_WORKER = '''WORKER_REVISION = 10
import json
def config():
    value = json.loads(open({config!r}).read())
    if set(value) != {{"version", "project", "data_dir"}} or value["version"] != 3:
        raise ValueError("invalid fixed deployment configuration")
    return value
'''
PUBLISHED_12 = '''WORKER_REVISION = 12
import json, os, sys
def config():
    value = json.loads(open({config!r}).read())
    if (set(value) != {{"version", "project", "data_dir", "deployment_name", "name_source"}}
            or value["version"] != 4):
        raise ValueError("invalid fixed deployment configuration; reinstall the host helper")
    return value
def run():
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER")
    if launcher is not None and (not launcher.isdigit() or int(launcher) < 2):
        return 0
    config()
    raise SystemExit("would replace")
if __name__ == "__main__" and sys.argv[1:] == ["run"]:
    raise SystemExit(run())
'''
IDENTITY = {"deployment_name": "original-app-1", "name_source": "generated"}
# os.replace is the publication boundary inside every durable_publish call.
# A shared counter spans both phases: SIGKILL the whole section (no cleanup
# runs) or fail it (the shell cleanup runs) at the Nth replacement overall.
HOOK = r'''
import json, os, signal, sys
_kill = int(os.environ.get("KILL_REPLACE") or 0)
_fail = int(os.environ.get("FAIL_REPLACE") or 0)
_replace = os.replace
def _next():
    path = os.environ["REPLACE_COUNTER"]
    count = int(open(path).read() or 0) + 1 if os.path.exists(path) else 1
    open(path, "w").write(str(count))
    return count
def _hooked(source, destination):
    count = _next()
    if os.environ.get("EVENTS"):
        index = json.load(open(os.environ["INDEX"]))
        with open(os.environ["EVENTS"], "a") as log:
            log.write(f"replace {source} {destination} {index['active']['revision']}\n")
    if count == _kill:
        os.kill(int(os.environ["SECTION_PID"]), signal.SIGKILL)
        os._exit(137)
    if count == _fail:
        raise OSError("injected publication failure")
    return _replace(source, destination)
os.replace = _hooked
if os.environ.get("EVENTS"):
    _fsync = os.fsync
    def _logged_fsync(fd):
        with open(os.environ["EVENTS"], "a") as log:
            log.write("fsync " + os.readlink(f"/proc/self/fd/{fd}") + "\n")
        return _fsync(fd)
    os.fsync = _logged_fsync
source = sys.stdin.read()
sys.argv[0] = "-"
exec(compile(source, "<stdin>", "exec"), {"__name__": "__main__"})
'''
UNITS = ("mobius-rebuild.service", "mobius-rebuild.path", "mobius-rebuild-reconcile.service",
         "mobius-rebuild-reconcile.timer")


def _rewrite(text: str, host: Path) -> str:
    for prefix in HOST_PREFIXES:
        text = text.replace(prefix, f"{host}{prefix}")
    text = text.replace("/usr/bin/python3", sys.executable)
    uid, gid = os.getuid(), os.getgid()
    text = (text.replace("OWNER = (0, 0)", f"OWNER = ({uid}, {gid})")
                .replace("TRUSTED_UID = 0", f"TRUSTED_UID = {uid}")
                .replace("os.geteuid() == 0", "True").replace("os.geteuid() != 0", "False")
                .replace("trusted_uid: int = 0", f"trusted_uid: int = {uid}")
                .replace("info.st_uid == 0", f"info.st_uid == {uid}"))
    if uid != 0:
        text = text.replace("install -d -o root -g root ", "install -d ")
    # Every host path must now live under the disposable root.
    for prefix in HOST_PREFIXES:
        for match in re.finditer(re.escape(prefix), text):
            assert text[:match.start()].endswith(str(host)), f"unrewritten host path {prefix}"
    return text


def _digest(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _load(path: Path, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Host:
    """One disposable released (or managed) helper host."""

    def __init__(self, tmp_path: Path, *, candidate=None, managed=False):
        self.root = host = tmp_path / "host"
        self.managed = managed
        for name, mode in (("etc/mobius-rebuild", 0o700), ("var/lib/mobius-rebuild", 0o700),
                           ("var/lib/mobius-rebuild/workers", 0o700),
                           ("var/lib/mobius-rebuild/admission", 0o700),
                           ("usr/local/libexec", 0o755), ("etc/systemd/system", 0o755),
                           ("data/mobius-rebuild", 0o755), ("data/mobius-rebuild/inbox", 0o700),
                           ("src/scripts", 0o755), ("tmp", 0o700)):
            (host / name).mkdir(parents=True, exist_ok=True)
            (host / name).chmod(mode)
        self.etc, self.state = host / "etc/mobius-rebuild", host / "var/lib/mobius-rebuild"
        (host / "usr/local/libexec/mobius-boot-admission.py").write_text("# pinned gate\n")
        self.worker = _rewrite((ROOT / "scripts/mobius-rebuild-host.py").read_text(), host).encode()
        self.launcher = _rewrite((ROOT / "scripts/mobius-rebuild-launcher.py").read_text(), host).encode()
        (host / "src/scripts/mobius-rebuild-host.py").write_bytes(self.worker)
        (host / "src/scripts/mobius-rebuild-launcher.py").write_bytes(self.launcher)
        self.legacy = self.etc / "config.json"
        self.v4 = self.etc / "config.v4.json"
        self.expected_v4 = {"version": 4, "project": "original", "data_dir": str(host / "data"), **IDENTITY}
        self.compose = b"services:\n  app:\n    image: original\n"
        files = {"compose.yml": self.compose}
        if managed:
            # A helper-managed host already runs this worker and launcher with
            # v4 and an immutable attempt pointer as its live override.
            files["config.v4.json"] = json.dumps(self.expected_v4).encode()
            files["image.override.yml"] = json.dumps({"name": "mobius-admission-" + "b" * 32}).encode()
            launcher, active, high_water = self.launcher, self._store(NEW, self.worker), NEW
            for unit in UNITS:
                (host / "etc/systemd/system" / unit).write_text("# installed by the previous run\n")
        else:
            files["config.json"] = json.dumps({"version": 3, "project": "original",
                                               "data_dir": str(host / "data")}).encode()
            files["image.override.yml"] = b"services:\n  app:\n    image: ${MOBIUS_IMAGE}\n"
            launcher, high_water = RELEASED_LAUNCHER, 10
            active = self._store(10, RELEASED_WORKER.format(config=str(self.legacy)).encode())
        for name, data in files.items():
            (self.etc / name).write_bytes(data)
            (self.etc / name).chmod(0o600)
        self.before = dict(files)
        (host / "usr/local/libexec/mobius-rebuild-host").write_bytes(launcher)
        (host / "usr/local/libexec/mobius-rebuild-host").chmod(0o755)
        entry = None
        if candidate == "published12":
            entry, high_water = self._store(12, PUBLISHED_12.format(config=str(self.legacy)).encode()), 12
        elif candidate == "exact14":
            entry, high_water = self._store(NEW, self.worker), NEW
        elif candidate == "tried14":
            high_water = NEW  # an exact trial of this revision was already consumed
        self.write_index({"version": 1, "high_water": high_water, "active": active, "candidate": entry})

    def _store(self, revision: int, data: bytes) -> dict:
        digest = _digest(data)
        path = self.state / "workers" / f"{revision}-{digest[:16]}.py"
        path.write_bytes(data)
        path.chmod(0o700)
        return {"revision": revision, "file": path.name, "sha256": digest, "origin": "test"}

    def index(self) -> dict:
        return json.loads((self.state / "workers.json").read_text())

    def write_index(self, value: dict) -> None:
        (self.state / "workers.json").write_text(json.dumps(value))
        (self.state / "workers.json").chmod(0o600)

    def run(self, **inject):
        host = self.root
        frozen = f'{self.etc}/compose.yml' if self.managed else ''
        prelude = f'''set -euo pipefail
export SECTION_PID=$$
ROOT={host}/src; PROJECT=original; DATA_SOURCE={host}/data; CURRENT_IMAGE=sha256:{"a" * 64}
ADMISSION_MANAGED={1 if self.managed else 0}; FROZEN_SOURCE={frozen}; ARGS=(-p original)
APP_UID={os.getuid()}; APP_GID={os.getgid()}; IDENTITY='{json.dumps(IDENTITY, separators=(",", ":"))}'
CONFIG_V4={self.v4}; HELPER_PROTOCOL={PROTOCOL}
RESOLVED=$(mktemp)
point() {{
  [[ -z ${{EVENTS:-}} ]] || printf 'point %s\n' "${{1// /_}}" >>"$EVENTS"
  [[ ${{KILL_AT:-}} != "$1" ]] || kill -9 $$
  [[ ${{FAIL_AT:-}} != "$1" ]]
}}
systemctl() {{ printf '%s\\n' "$*" >>"$CALLS"; point "systemctl $1"; }}
flock() {{ point "flock $*"; }}
docker() {{ cat "$COMPOSE_OUT"; }}
python3() {{ if [[ ${{1:-}} == - ]]; then shift; "$PY" -c "$HOOK" "$@"; else "$PY" "$@"; fi; }}
DISPATCH_UNITS=(mobius-rebuild.path mobius-rebuild-reconcile.timer)
DISPATCH_RESTORE=(start start)
MIGRATION_COMPLETE=0
'''
        section = _rewrite(PUBLICATION, host)
        commit = _rewrite(COMMIT, host)
        assert section.count(commit) == 1
        section = section.replace(commit, "point before-commit\n" + commit)
        script = prelude + _rewrite(CLEANUP, host) + "trap migration_cleanup EXIT\n" + section
        calls, counter = host / "calls", host / "replace-counter"
        calls.write_text("")
        counter.unlink(missing_ok=True)
        (host / "compose.out").write_bytes(self.compose)
        # A private temp directory per run: a killed run may leave its own.
        self.last_tmp = Path(tempfile.mkdtemp(dir=host / "tmp"))
        env = {"PATH": os.environ["PATH"], "TMPDIR": str(self.last_tmp), "CALLS": str(calls),
               "PY": sys.executable, "HOOK": HOOK, "HOME": str(host / "tmp"),
               "COMPOSE_OUT": str(host / "compose.out"), "REPLACE_COUNTER": str(counter),
               "INDEX": str(self.state / "workers.json"),
               **{key: str(value) for key, value in inject.items()}}
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        return result, calls.read_text().splitlines()

    def selected(self):
        """What reboot-enabled dispatch would run: (launcher 2?, active, candidate)."""
        installed = (self.root / "usr/local/libexec/mobius-rebuild-host").read_bytes()
        assert installed in (RELEASED_LAUNCHER, self.launcher), "torn or foreign launcher"
        index = self.index()
        if installed == self.launcher:
            module = _load(self.root / "src/scripts/mobius-rebuild-launcher.py", "launcher_under_test")
            assert module.load_index() is not None, "launcher 2 cannot load the durable selection"
        return installed == self.launcher, index["active"], index.get("candidate")

    def accepts_own_config(self, entry: dict) -> bool:
        module = _load(self.state / "workers" / entry["file"], "worker_" + entry["sha256"][:12])
        try:
            module.config()
        except (OSError, ValueError, KeyError):
            return False
        return True

    def run_under_launcher1(self, entry: dict):
        """Execute the recorded worker's real `run` exactly as launcher 1 would."""
        request = self.root / "data/mobius-rebuild/inbox/request.json"
        request.write_text('{"version":1,"expected_sha":"' + "a" * 40 + '"}')
        status = self.state / "status.json"
        before = status.read_bytes() if status.exists() else b""
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(self.state / "workers" / entry["file"]), "run"],
            env={"PATH": "/usr/bin:/bin", "MOBIUS_REBUILD_LAUNCHER": "1"},
            capture_output=True, text=True, timeout=30)
        after = status.read_bytes() if status.exists() else b""
        preserved = request.exists()
        request.unlink(missing_ok=True)
        return result.returncode, before == after, preserved

    def assert_coherent(self):
        launcher2, active, candidate = self.selected()
        assert self.accepts_own_config(active), "the selected worker rejects the config it reads"
        # The app dispatches only to a certified protocol, so certification
        # must never precede the selected worker, launcher and units it implies.
        if (self.state / "helper-protocol").exists():
            assert launcher2 and active["revision"] == NEW
        # Launcher 2 never runs beside the released worker: it is published
        # only after the new worker is durably selected.
        assert not (launcher2 and active["revision"] < NEW)
        if active["revision"] == NEW:
            # The commit happened only after every input it reads was durable.
            assert json.loads(self.v4.read_text()) == self.expected_v4
            for unit in UNITS:
                assert (self.root / "etc/systemd/system" / unit).is_file()
            if not launcher2:
                # Commit before launcher 2: launcher 1 runs the new worker,
                # which refuses replacement and leaves the request queued.
                assert self.run_under_launcher1(active) == (0, True, True)
        if "config.json" in self.before:
            assert self.legacy.read_bytes() == self.before["config.json"]
        else:
            assert not self.legacy.exists()
        if self.managed:
            assert (self.etc / "image.override.yml").read_bytes() == self.before["image.override.yml"]

    def assert_migrated(self):
        self.assert_coherent()
        launcher2, active, candidate = self.selected()
        assert launcher2 and active["sha256"] == _digest(self.worker) and candidate is None
        assert self.index()["high_water"] == NEW
        assert (self.state / "helper-protocol").read_text() == f"{PROTOCOL}\n"
        for name, mode in (("config.v4.json", 0o600), ("compose.yml", 0o600), ("image.override.yml", 0o600)):
            assert stat.S_IMODE((self.etc / name).stat().st_mode) == mode
        assert stat.S_IMODE((self.root / "usr/local/libexec/mobius-rebuild-host").stat().st_mode) == 0o755
        assert stat.S_IMODE((self.root / "etc/systemd/system/mobius-rebuild.path").stat().st_mode) == 0o644
        assert not list(self.last_tmp.iterdir()), "staging or preflight files leaked"


def _reboot_dispatch(host: _Host) -> None:
    """Run what reboot-enabled dispatch runs for a pending candidate.

    Only launcher 1 can be installed beside a pending candidate (launcher 2
    follows the commit, which clears it). Launcher 1 executes the candidate's
    real `run`; its released settle rule puts an untried candidate back when the
    status is unchanged and the run returned 0, and otherwise drops it.
    """
    launcher2, _active, candidate = host.selected()
    if candidate is None:
        return
    assert not launcher2
    code, unchanged, _preserved = host.run_under_launcher1(candidate)
    index = host.index()
    if not (unchanged and code == 0 and index["high_water"] == candidate["revision"]):
        index["candidate"] = None
    host.write_index(index)


PRE_COMMIT = ([("KILL_REPLACE", n) for n in range(1, 8)] + [("FAIL_REPLACE", n) for n in range(1, 8)]
              + [("KILL_AT", "before-commit"), ("FAIL_AT", "before-commit")])
# Replacement 9 is the protocol certification, after dispatch is restored.
POST_COMMIT = ([("KILL_REPLACE", 8), ("FAIL_REPLACE", 8), ("KILL_REPLACE", 9), ("FAIL_REPLACE", 9)]
               + [(mode, point) for point in ("systemctl daemon-reload", "flock -u 9", "flock -u 8",
                                              "systemctl enable") for mode in ("KILL_AT", "FAIL_AT")])


@pytest.mark.parametrize("mode,point", PRE_COMMIT + POST_COMMIT)
def test_every_interruption_leaves_a_coherent_worker_and_rerun_converges(tmp_path, mode, point):
    host = _Host(tmp_path)
    result, calls = host.run(**{mode: point})
    assert result.returncode != 0
    host.assert_coherent()
    launcher2, active, _ = host.selected()
    assert (active["revision"] == NEW) is ((mode, point) in POST_COMMIT)
    # Launcher 2 is the first replacement after the commit (the 8th overall).
    assert launcher2 is ((mode, point) in POST_COMMIT and point not in (8,))
    if mode.startswith("FAIL"):
        # A shell failure restores the previous dispatch onto a coherent pair.
        assert calls[-2:] == ["start mobius-rebuild.path", "start mobius-rebuild-reconcile.timer"]
    else:
        # SIGKILL restores nothing; enabled units then dispatch after a reboot.
        assert "start mobius-rebuild.path" not in calls
    result, _ = host.run()
    assert result.returncode == 0, result.stderr
    host.assert_migrated()


def test_uninterrupted_publication_commits_once_after_every_input(tmp_path):
    host = _Host(tmp_path)
    result, calls = host.run()
    assert result.returncode == 0, result.stderr
    host.assert_migrated()
    assert json.loads(host.v4.read_text()) == host.expected_v4
    assert (host.etc / "compose.yml").read_bytes() == host.compose
    assert calls == ["daemon-reload", "enable mobius-rebuild-reconcile.service",
                     "enable --now mobius-rebuild-reconcile.timer", "enable --now mobius-rebuild.path"]
    status = json.loads((host.state / "status.json").read_text())
    assert status["worker_revision"] == NEW and status["launcher_revision"] == 2
    # A second run is idempotent: same selection, same inputs.
    before = {path: path.read_bytes() for path in (host.v4, host.etc / "compose.yml",
                                                   host.state / "workers.json")}
    result, _ = host.run()
    assert result.returncode == 0, result.stderr
    assert f"current: revision {NEW}" in result.stdout
    assert {path: path.read_bytes() for path in before} == before


# Every interruption point with the exact pending revision-14 candidate (the
# one a premature launcher 2 could burn); a sample with an older candidate.
@pytest.mark.parametrize("candidate,mode,point",
                         [("exact14", mode, point) for mode, point in PRE_COMMIT + POST_COMMIT]
                         + [("published12", mode, point) for mode, point in PRE_COMMIT[::3] + POST_COMMIT[:4]])
def test_pending_image_candidate_across_reboot_is_not_burned_and_converges(tmp_path, candidate, mode, point):
    host = _Host(tmp_path, candidate=candidate)
    pending = host.index()["candidate"]
    result, _ = host.run(**{mode: point})
    assert result.returncode != 0
    host.assert_coherent()
    _reboot_dispatch(host)
    index = host.index()
    if (mode, point) in PRE_COMMIT:
        # Launcher 1 runs the candidate, which refuses without consuming its
        # one trial: the exact candidate and high-water mark are unchanged.
        assert index["candidate"] == pending and index["high_water"] == pending["revision"]
    else:
        # The commit consumed the pending candidate: an exact one became active.
        assert index["candidate"] is None and index["active"]["revision"] == NEW
        if candidate == "exact14":
            assert index["active"]["sha256"] == pending["sha256"]
    host.assert_coherent()
    result, _ = host.run()
    assert result.returncode == 0, result.stderr
    host.assert_migrated()
    if candidate == "exact14":
        # The retry activated exactly the pending candidate's bytes and file.
        assert host.index()["active"]["file"] == pending["file"]
        assert (f"installed: revision {NEW}" in result.stdout) is ((mode, point) in PRE_COMMIT)


def test_failed_exact_trial_is_never_readmitted_and_released_pair_stays_coherent(tmp_path):
    host = _Host(tmp_path, candidate="tried14")
    result, calls = host.run()
    assert result.returncode != 0
    assert f"rejected: revision {NEW} was already tried or superseded" in result.stdout + result.stderr
    # The refusal is at the commit: the released selection keeps its own config
    # and launcher; staged new inputs are inert to it; dispatch is restored.
    assert host.index()["active"]["revision"] == 10 and host.index()["high_water"] == NEW
    assert host.selected()[0] is False
    host.assert_coherent()
    assert calls[-2:] == ["start mobius-rebuild.path", "start mobius-rebuild-reconcile.timer"]


@pytest.mark.parametrize("mode,point", [(None, None), ("KILL_REPLACE", 1), ("KILL_REPLACE", 2),
                                        ("FAIL_REPLACE", 2), ("KILL_AT", "before-commit"),
                                        ("KILL_REPLACE", 7), ("KILL_AT", "flock -u 9")])
def test_managed_reinstall_preserves_frozen_identity_and_attempt_pointer(tmp_path, mode, point):
    host = _Host(tmp_path, managed=True)
    result, _ = host.run(**({mode: point} if mode else {}))
    assert (result.returncode == 0) is (mode is None), result.stderr
    if mode == "KILL_REPLACE" and point == 7:
        # The live attempt pointer is preserved, not republished, so launcher 2
        # is the seventh replacement (see the ordering test): this kill lands
        # after the commit, immediately before launcher publication.
        assert (host.etc / "image.override.yml").read_bytes() == host.before["image.override.yml"]
        assert host.index()["active"]["sha256"] == _digest(host.worker)
    host.assert_coherent()
    result, _ = host.run()
    assert result.returncode == 0, result.stderr
    host.assert_migrated()
    assert json.loads(host.v4.read_text()) == host.expected_v4
    assert (host.etc / "image.override.yml").read_bytes() == host.before["image.override.yml"]


@pytest.mark.parametrize("managed", [False, True])
def test_each_input_is_durable_before_the_next_and_launcher_follows_the_commit(tmp_path, managed):
    host = _Host(tmp_path, managed=managed)
    events = tmp_path / "events"
    result, _ = host.run(EVENTS=events)
    assert result.returncode == 0, result.stderr
    log = [line.split(" ") for line in events.read_text().splitlines()]
    replaces = [index for index, item in enumerate(log) if item[0] == "replace"]
    destinations = [(log[index][2], int(log[index][3])) for index in replaces]
    # A managed host preserves its live attempt pointer, so it stages one file
    # fewer and launcher 2 is its seventh replacement, not its eighth.
    staged = [str(host.v4), str(host.etc / "compose.yml")] + (
        [] if managed else [str(host.etc / "image.override.yml")]) + [
        str(host.root / "etc/systemd/system" / unit) for unit in UNITS]
    launcher = str(host.root / "usr/local/libexec/mobius-rebuild-host")
    # Phase 1 under the previous selection; launcher 2 only after the commit.
    before = NEW if managed else 10
    certified = str(host.state / "helper-protocol")
    assert destinations == [(path, before) for path in staged] + [(launcher, NEW), (certified, NEW)]
    assert len(destinations) == (8 if managed else 9)
    commit = log.index(["point", "before-commit"])
    assert replaces[-3] < commit < replaces[-2]
    # Certification is the last replacement, after dispatch is restored.
    enabled = max(i for i, item in enumerate(log) if item == ["point", "systemctl_enable"])
    assert enabled < replaces[-1]
    # The inbox and its parents are synced before the first replacement.
    synced_first = {item[1] for item in log[:replaces[0]] if item[0] == "fsync"}
    assert {str(host.root / "data"), str(host.root / "data/mobius-rebuild"),
            str(host.root / "data/mobius-rebuild/inbox")} <= synced_first
    for position, index in enumerate(replaces):
        temporary, destination = log[index][1], log[index][2]
        start = replaces[position - 1] if position else 0
        assert temporary in [item[1] for item in log[start:index] if item[0] == "fsync"], \
            f"{destination} renamed before its data was synced"
        following = replaces[position + 1] if position + 1 < len(replaces) else len(log)
        assert os.path.dirname(destination) in [item[1] for item in log[index + 1:following]
                                                if item[0] == "fsync"], \
            f"{destination} rename not synced before the next step"


def test_commit_separates_staging_from_launcher_publication():
    commit = SOURCE.index(COMMIT)
    staged = SOURCE.index('durable_publish "${PUBLISH[@]}" --')
    launcher = SOURCE.index('durable_publish "$ROOT/scripts/mobius-rebuild-launcher.py"')
    assert SOURCE.count('scripts/mobius-rebuild-host.py" adopt-self') == 1
    assert SOURCE.count("<<'PY_PUBLISH'") == 1  # one durable helper for both phases
    assert (SOURCE.index("flock -w 30 9") < SOURCE.index("# END PINNED")
            < SOURCE.index("durable_publish() {") < staged < commit < launcher
            < SOURCE.index("# END DURABLE STAGED PUBLICATION"))
    assert "mobius-rebuild-launcher.py" not in SOURCE[SOURCE.index("PUBLISH=("):staged]
    between = SOURCE[SOURCE.index("\n", SOURCE.index('"$DATA_SOURCE/mobius-rebuild" "$DATA_SOURCE"', staged)):commit]
    assert all(line.startswith("#") or not line.strip() for line in between.splitlines())
    after = SOURCE[commit + len(COMMIT):SOURCE.index("MIGRATION_COMPLETE=1\n")]
    commands = [line for line in after.splitlines()
                if line and not line[0].isspace() and not line.startswith("#")]
    assert commands == [
        'durable_publish "$ROOT/scripts/mobius-rebuild-launcher.py" \\', "systemctl daemon-reload",
        "flock -u 9", "exec 9>&-", "MOBIUS_REBUILD_LAUNCHER=2 \\", "flock -u 8", "exec 8>&-",
        "systemctl enable mobius-rebuild-reconcile.service",
        "systemctl enable --now mobius-rebuild-reconcile.timer", "systemctl enable --now mobius-rebuild.path",
        # Certification last, then its capability refresh (see test_host_data_dir).
        "printf '%s\\n' \"$HELPER_PROTOCOL\" >\"$STAGE/helper-protocol\"",
        'durable_publish "$STAGE/helper-protocol" /var/lib/mobius-rebuild/helper-protocol 0600 --',
        "MOBIUS_REBUILD_LAUNCHER=2 \\"]
    # Nothing writes the released worker's config, in place or by rename.
    assert not re.search(r"(?:>|replace\(|mv )[^\n]*etc/mobius-rebuild/config\.json", SOURCE)
    assert "cat >/etc/" not in SOURCE and "mv -f" not in SOURCE and "install -D" not in SOURCE
    # The worker reads only the versioned path; the adapter matches it.
    assert 'CONFIG = Path("/etc/mobius-rebuild/config.v4.json")' in (ROOT / "scripts/mobius-rebuild-host.py").read_text()
    assert "CONFIG = Path('/etc/mobius-rebuild/config.v4.json')" in (ROOT / "scripts/mobius-manual-cutover.py").read_text()


def test_new_worker_revision_exceeds_every_published_revision():
    # Unreleased review heads used revisions 12 and 13, and #1806 released 14.
    # The checkout must be strictly newer, or a host holding that high-water
    # mark (for example a pending revision-14 candidate) would refuse it.
    text = (ROOT / "scripts/mobius-rebuild-host.py").read_text()
    assert re.search(r"^WORKER_REVISION = 15$", text, re.M)


@pytest.mark.parametrize("problem", ["writable", "foreign_owner"])
def test_publication_refuses_an_untrusted_destination_before_replacing_anything(tmp_path, problem):
    host = _Host(tmp_path)
    target = host.root / "etc/systemd/system"
    if problem == "writable":
        target.chmod(0o777)
    elif os.getuid() == 0:
        os.chown(target, 65534, 65534)
    else:
        pytest.skip("ownership change needs root")
    result, _ = host.run()
    assert result.returncode != 0
    assert "Publication directory is not root-controlled" in result.stderr
    # Validation happens before the first replacement: nothing was staged.
    assert not host.v4.exists()
    assert host.index()["active"]["revision"] == 10
    host.assert_coherent()


def _existing(tmp_path, files, *, project="original", data="/host/data",
              mount="/var/lib/docker/volumes/original_data/_data", directory_mode=0o700):
    etc = tmp_path / "etc/mobius-rebuild"
    etc.mkdir(parents=True)
    etc.chmod(directory_mode)
    for name, value in files.items():
        if isinstance(value, Path):
            (etc / name).symlink_to(value)
            continue
        (etc / name).write_text(value if isinstance(value, str) else json.dumps(value))
        (etc / name).chmod(0o600)
    code = _heredoc("PY_EXISTING").replace("/etc/mobius-rebuild", str(etc)).replace(
        "TRUSTED_UID = 0", f"TRUSTED_UID = {os.getuid()}")
    return subprocess.run([sys.executable, "-c", code, project, data, mount], capture_output=True, text=True)


V3 = {"version": 3, "project": "original", "data_dir": "/host/data"}
V4 = {"version": 4, "project": "original", "data_dir": "/host/data", **IDENTITY}


@pytest.mark.parametrize("files", [{}, {"config.json": V3}, {"config.v4.json": V4},
                                   {"config.json": V3, "config.v4.json": V4}])
def test_existing_configuration_matching_this_deployment_is_accepted(tmp_path, files):
    result = _existing(tmp_path, files)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,base", [("config.json", V3), ("config.v4.json", V4)])
def test_existing_configuration_naming_the_volume_mountpoint_migrates_to_the_stable_path(
        tmp_path, name, base):
    # Helper protocol 5 and earlier recorded Docker's Mountpoint for a
    # bind-option volume. It is the same data, so reinstalling proceeds and
    # rewrites the stable spelling instead of refusing as another topology.
    mount = "/var/lib/docker/volumes/original_data/_data"
    result = _existing(tmp_path, {name: {**base, "data_dir": mount}}, mount=mount)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("files,reason", [
    ({"config.json": {**V3, "project": "other"}}, "different Compose project or data directory"),
    ({"config.json": {**V3, "data_dir": "/other/data"}}, "different Compose project or data directory"),
    ({"config.v4.json": {**V4, "project": "other"}}, "different Compose project or data directory"),
    ({"config.v4.json": {**V4, "data_dir": "/other/data"}}, "different Compose project or data directory"),
    ({"config.json": V3, "config.v4.json": {**V4, "data_dir": "/other/data"}}, "different Compose project"),
    ({"config.json": V4}, "unexpected schema"),        # an unreleased v4 written at the released path
    ({"config.v4.json": V3}, "unexpected schema"),
    ({"config.json": "not json"}, "unexpected schema"),
    ({"config.json": ["list"]}, "unexpected schema"),
])
def test_existing_configuration_for_another_topology_refuses_before_staging(tmp_path, files, reason):
    result = _existing(tmp_path, files)
    assert result.returncode != 0
    assert reason in result.stderr


def test_existing_configuration_must_be_root_controlled(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(V3))
    assert "not root-controlled" in _existing(tmp_path / "a", {"config.json": target}).stderr
    assert "directory is not root-controlled" in _existing(tmp_path / "b", {}, directory_mode=0o777).stderr
    result = _existing(tmp_path / "c", {"config.json": V3})
    (tmp_path / "c/etc/mobius-rebuild/config.json").chmod(0o666)
    code = _heredoc("PY_EXISTING").replace("/etc/mobius-rebuild", str(tmp_path / "c/etc/mobius-rebuild"))
    code = code.replace("TRUSTED_UID = 0", f"TRUSTED_UID = {os.getuid()}")
    result = subprocess.run([sys.executable, "-c", code, "original", "/host/data", "/host/data"],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "not root-controlled" in result.stderr


def test_invariance_is_checked_for_every_host_before_any_controller_change():
    definition = SOURCE.index("\ncheck_existing_configuration() {\npython3 - \"$PROJECT\" \"$DATA_SOURCE\" "
                              "\"$DATA_MOUNT_SOURCE\" <<'PY_EXISTING'\n")
    # Not only for frozen-source hosts: both calls are unconditional top-level
    # commands (set -e makes a refusal fatal), never inside a branch.
    calls = [match.start() for match in re.finditer(r"^check_existing_configuration$", SOURCE, re.M)]
    assert len(calls) == 2
    preflight, locked = calls
    assert SOURCE.index("DATA_SOURCE=$(python3") < definition < preflight < SOURCE.index("umask 077")
    assert preflight < SOURCE.index("systemctl stop")
    # Rechecked under both locks: a concurrent installer may have written one.
    assert (SOURCE.index("flock -w 30 9") < SOURCE.index("[[ $(deployment_identity) == \"$IDENTITY\" ]]")
            < locked < SOURCE.index("# BEGIN PINNED BOOT ADMISSION INSTALL")
            < SOURCE.index("# BEGIN DURABLE STAGED PUBLICATION"))
    assert "PY_OWNER" not in SOURCE and "PY_DATA" not in SOURCE


@pytest.mark.parametrize("name,value,accepted", [("config.json", {**V3, "project": "other"}, False),
                                                 ("config.v4.json", {**V4, "data_dir": "/other/data"}, False),
                                                 ("config.json", V3, True), ("config.v4.json", V4, True)])
def test_configuration_appearing_at_lock_acquisition_refuses_publication(tmp_path, name, value, accepted):
    # The real locked preflight, from umask through the recheck, with only
    # systemd, flock and the already-tested Docker identity probes faked.
    state, etc = tmp_path / "state", tmp_path / "etc"
    definition = SOURCE[SOURCE.index("check_existing_configuration() {"):SOURCE.index("\n}\n", SOURCE.index(
        "\nPY_EXISTING\n")) + 3]
    code = definition + _refusal() + SOURCE.split("umask 077\n", 1)[1].split("# BEGIN PINNED", 1)[0]
    code = (code.replace("/var/lib/mobius-rebuild", str(state)).replace("/etc/mobius-rebuild", str(etc))
                .replace("TRUSTED_UID = 0", f"TRUSTED_UID = {os.getuid()}"))
    code = '''systemctl() { if [ "$1" = show ]; then printf 'LoadState=not-found\\nActiveState=inactive\\nJob=\\n'; fi; }
flock() { if [ "${3:-}" = 9 ]; then printf '%s' "$WRITTEN" >"$ETC/$NAME"; fi; }
discover_app() { printf '%s\\n' "$CID"; }
deployment_identity() { printf '%s\\n' "$IDENTITY"; }
PROJECT=original DATA_SOURCE=/host/data DATA_MOUNT_SOURCE=/host/data CID=c ADMISSION_MANAGED=0 IDENTITY=frozen
''' + code + '\ntouch "$PUBLISHED"\n'
    published = tmp_path / "controller-changed"
    result = subprocess.run(["bash", "-eu", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "ETC": str(etc), "NAME": name, "WRITTEN": json.dumps(value),
                                 "PUBLISHED": str(published), "RESOLVED": str(tmp_path / "resolved")})
    if accepted:
        # Control: the same harness reaches publication for this deployment.
        assert result.returncode == 0, result.stderr
        assert published.exists()
        return
    assert result.returncode != 0
    assert "different Compose project or data directory" in result.stderr
    assert not published.exists()
    assert not (state / "admission").exists() and not (state / "workers.json").exists()
