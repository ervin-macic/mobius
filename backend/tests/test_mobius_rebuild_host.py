"""Trust and failure-boundary tests for the installed host worker."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "mobius-rebuild-host.py"
INSTALLER = Path(__file__).parents[2] / "scripts" / "install-rebuild-helper.sh"
ENTRYPOINT = Path(__file__).parents[1] / "scripts" / "entrypoint.sh"
SPEC = importlib.util.spec_from_file_location("mobius_rebuild_host", SCRIPT)
assert SPEC and SPEC.loader
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


def test_entrypoint_restores_host_control_after_compatibility_chown():
  source = ENTRYPOINT.read_text(encoding="utf-8")

  broad_chown = source.index("if ! _own_as_mobius /data 2>/dev/null; then")
  control_hardening = source.index("chown -R root:root /data/mobius-rebuild")
  inbox_grant = source.index(
    "chown -R mobius:mobius /data/mobius-rebuild/inbox",
  )

  assert broad_chown < control_hardening < inbox_grant


def test_installer_enables_boot_time_reconciliation():
  source = INSTALLER.read_text(encoding="utf-8")

  assert 'mkdir -p "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'chown "$APP_UID:$APP_GID" "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'chmod 0700 "$DATA_SOURCE/mobius-rebuild/inbox"' in source
  assert 'install -d -o "$APP_UID"' not in source
  assert "mobius-rebuild-reconcile.service" in source
  assert "ExecStart=/usr/local/libexec/mobius-rebuild-host reconcile" in source
  assert "Before=mobius-rebuild.path" in source
  assert "WantedBy=multi-user.target" in source
  assert "systemctl enable mobius-rebuild-reconcile.service" in source
  assert 'bootstrap-runtime "$CID"' not in source
  assert "MOBIUS_RUNTIME_OVERLAY" not in source
  assert "target: /app/runtime" not in source
  assert "FROZEN_SOURCE=/etc/mobius-rebuild/compose.yml" in source
  assert '"com.docker.compose.project.environment_file"' in source
  assert 'ARGS+=(--env-file "$file")' in source
  assert "CURRENT_IMAGE=$(docker inspect" in source
  assert 'MOBIUS_IMAGE="$CURRENT_IMAGE" docker compose' in source


def _frozen(tmp_path: Path, monkeypatch) -> tuple[dict, Path]:
  etc = tmp_path / "etc"
  data = tmp_path / "data"
  control = data / "mobius-rebuild"
  inbox = control / "inbox"
  etc.mkdir()
  inbox.mkdir(parents=True)
  config_path = etc / "config.json"
  compose = etc / "compose.yml"
  override = etc / "image.override.yml"
  for path in (config_path, compose, override):
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)
  control.chmod(0o755)
  monkeypatch.setattr(host, "CONFIG", config_path)
  monkeypatch.setattr(host, "COMPOSE", compose)
  monkeypatch.setattr(host, "OVERRIDE", override)
  value = {"version": 3, "project": "mobius", "data_dir": str(data)}
  return value, control


def test_frozen_config_accepts_minimal_root_owned_boundary(tmp_path, monkeypatch):
  value, control = _frozen(tmp_path, monkeypatch)

  result = host.validate_config(value, trusted_uid=os.getuid())

  assert result["project"] == "mobius"
  assert result["control_dir"] == control
  assert set(value) == {"version", "project", "data_dir"}


def test_frozen_config_rejects_group_writable_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  host.COMPOSE.chmod(0o620)

  with pytest.raises(ValueError, match="not root-controlled"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_frozen_config_rejects_symlinked_input(tmp_path, monkeypatch):
  value, _control = _frozen(tmp_path, monkeypatch)
  target = host.COMPOSE.with_name("mutable.yml")
  target.write_text("{}\n", encoding="utf-8")
  host.COMPOSE.unlink()
  host.COMPOSE.symlink_to(target)

  with pytest.raises(ValueError, match="may not use symlinks"):
    host.validate_config(value, trusted_uid=os.getuid())


def test_served_generation_requires_the_image_runtime(monkeypatch):
  def execute(args, **_kwargs):
    payload = '{"sha":"' + "a" * 40 + '"}' if "curl" in args else "[]"
    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

  monkeypatch.setattr(host, "docker_command", execute)
  host.verify_served_generation("cid", "a" * 40)

  def mounted(args, **_kwargs):
    payload = (
      '{"sha":"' + "a" * 40 + '"}' if "curl" in args else
      '[{"Destination":"/app/runtime"}]'
    )
    return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

  monkeypatch.setattr(host, "docker_command", mounted)
  with pytest.raises(RuntimeError, match="image's protected runtime"):
    host.verify_served_generation("cid", "a" * 40)


def test_replacement_verifies_provenance_before_retiring_chat_handoff():
  source = SCRIPT.read_text(encoding="utf-8")
  run = source[source.index("def run()") : source.index("def reconcile()")]
  healthy = run.index("if not wait_healthy(config_value)")
  provenance = run.index("verify_served_generation(", healthy)
  finalize = run.index("finish_verified(", provenance)

  assert healthy < provenance < finalize


def _worker_paths(tmp_path: Path, monkeypatch):
  # Most controller tests simulate a consumed boot; real-ledger tests below
  # restore the actual read-only proof against a private on-disk ledger.
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda *_a, **_k: True)
  state = tmp_path / "state"
  inbox = tmp_path / "control" / "inbox"
  state.mkdir()
  inbox.mkdir(parents=True)
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "LOCK", state / "replace.lock")
  monkeypatch.setattr(host, "STATUS", state / "status.json")
  monkeypatch.setattr(host, "IMAGES", state / "images.json")
  monkeypatch.setattr(host, "TRANSACTION", state / "transaction.json")
  data = tmp_path / "data"
  data.mkdir()
  config = {
    "project": "mobius", "control_dir": inbox.parent, "data_dir": data,
  }
  monkeypatch.setattr(host, "config", lambda: config)
  return config, inbox


def test_compose_never_mounts_a_generated_runtime(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  calls = []

  def execute(args, **kwargs):
    calls.append((args, kwargs))
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

  monkeypatch.setattr(host, "docker_command", execute)
  host.compose(config, "ps")

  _args, kwargs = calls[0]
  assert "MOBIUS_RUNTIME_OVERLAY" not in kwargs["env"]




























def test_no_change_does_not_drain_active_chats(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "c" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "request_drain",
    lambda *_args: (_ for _ in ()).throw(AssertionError("no-change must not drain")),
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 0
  assert statuses[-1]["state"] == "no_change"




def test_request_is_claimed_on_the_control_filesystem(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "e" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  real_replace = host.os.replace
  claims = []

  def same_filesystem_replace(source, target):
    if Path(source) == request:
      claims.append(Path(target))
      assert Path(target).parent == config["control_dir"]
    return real_replace(source, target)

  monkeypatch.setattr(host.os, "replace", same_filesystem_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert len(claims) == 1
  assert not claims[0].exists()


def test_worker_locks_before_exposing_claim_to_reconcile(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "1" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  order = []
  real_replace = host.os.replace
  real_flock = host.fcntl.flock

  def record_flock(fd, operation):
    order.append("lock")
    return real_flock(fd, operation)

  def record_replace(source, target):
    if Path(source) == request:
      order.append("claim")
    return real_replace(source, target)

  monkeypatch.setattr(host.fcntl, "flock", record_flock)
  monkeypatch.setattr(host.os, "replace", record_replace)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert order[:2] == ["lock", "claim"]


def test_worker_waits_for_boot_reconcile_before_claiming(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "2" * 40
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  attempts = []

  def reconcile_then_release(_fd, _operation):
    attempts.append("lock")
    if len(attempts) == 1:
      raise BlockingIOError

  monkeypatch.setattr(host.fcntl, "flock", reconcile_then_release)
  monkeypatch.setattr(host.time, "monotonic", lambda: 0)
  monkeypatch.setattr(host.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "same"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "same"
  ))
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(host, "write_status", lambda _config, **fields: fields)

  assert host.run() == 0
  assert attempts == ["lock", "lock"]
  assert not request.exists()


def test_failed_request_claim_is_terminal_and_retryable(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(
    f'{{"version":1,"expected_sha":"{"f" * 40}"}}', encoding="utf-8",
  )
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )
  monkeypatch.setattr(
    host.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("claim failed")),
  )

  assert host.run() == 1
  assert statuses[-1]["state"] == "failed"
  # It was never claimed, so it may be another request by now: it stays for
  # the owner to withdraw instead of being deleted unverified.
  assert request.exists()




@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_replacement_drains_then_rolls_back_after_cutover_error(tmp_path, monkeypatch, cleanup_fails):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "d" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  monkeypatch.setattr(host, "app_container", lambda _config: ("cid", "sha256:old"))
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "sha256:new"
  ))
  snapshots = iter([("cid", "sha256:new", "dead"), ("old", "sha256:old", "healthy")])
  monkeypatch.setattr(host, "container_health", lambda *_a: next(snapshots))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: None)
  if cleanup_fails:
    monkeypatch.setattr(host, "discard_pulled_image", lambda *_a:
                        (_ for _ in ()).throw(RuntimeError("cleanup failed")))
  order = []
  ready = inbox / "ready"
  monkeypatch.setattr(
    host, "request_drain", lambda *_args: order.append("drain") or ready,
  )
  monkeypatch.setattr(host, "restart_ledger", lambda *_args, **_kwargs: True)

  def compose(_config, *args, image=None, **_kwargs):
    order.append(f"compose:{image}")
    if image == host.TARGET_TAG:
      raise RuntimeError("cutover failed")

  monkeypatch.setattr(host, "compose", compose)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.run() == 1
  assert order == [
    "drain",
    f"compose:{host.TARGET_TAG}",
    "compose:sha256:old",
  ]
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == "replacement_failed"


@pytest.mark.parametrize("outcome_write_fails", [False, True])
def test_verified_success_never_rolls_back_for_handoff_or_outcome_write_failure(
  tmp_path, monkeypatch, outcome_write_fails,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  expected = "9" * 40
  (inbox / "request.json").write_text(
    f'{{"version":1,"expected_sha":"{expected}"}}', encoding="utf-8",
  )
  # The running container holds the previous image until Compose replaces it.
  replaced = []
  monkeypatch.setattr(
    host, "app_container", lambda _config: ("cid", "new" if replaced else "old"),
  )
  monkeypatch.setattr(host, "require_pull_space", lambda _image: None)
  monkeypatch.setattr(host.subprocess, "run", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "inspect_image", lambda _image, template: (
    expected if "revision" in template else
    host.IMAGE_SOURCE if "source" in template else
    "amd64" if "Architecture" in template else "new"
  ))
  monkeypatch.setattr(host, "request_drain", lambda *_args: None)
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: replaced.append(1))
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_args: None)
  monkeypatch.setattr(
    host, "restart_ledger",
    lambda _config, _cid, command, _operation, **_kwargs:
      command != "finalize-cutover",
  )
  monkeypatch.setattr(host, "adopt_from_image", lambda _image: "not adopted: test")
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  write = host.write_transaction

  def persist(value):
    if outcome_write_fails and value.get("outcome"):
      raise OSError("outcome write failed")
    write(value)

  monkeypatch.setattr(host, "write_transaction", persist)
  monkeypatch.setattr(host, "rollback", lambda *a, **k: pytest.fail("verified service must not roll back"))
  assert host.run() == int(outcome_write_fails)
  assert replaced == [1]
  if outcome_write_fails:
    assert statuses[-1]["state"] == "needs_recovery"
    assert statuses[-1]["code"] == "outcome_record_failed"
    assert host.TRANSACTION.exists()
  else:
    assert statuses[-1]["state"] == "succeeded"
    assert statuses[-1]["code"] == "handoff_finalize_unconfirmed"
    assert "finalization is unconfirmed" in statuses[-1]["message"]


@pytest.mark.parametrize(
  ("rearmed", "finalized", "expected_code", "message_fragment"),
  [
    (False, False, "handoff_finalize_unconfirmed", "manual Resume may be needed"),
    (True, False, "handoff_finalize_unconfirmed", "finalization is unconfirmed"),
  ],
)
def test_healthy_rollback_reports_degraded_chat_handoff(
  tmp_path, monkeypatch, rearmed, finalized, expected_code, message_fragment,
):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  operation = "a" * 32
  expected = "b" * 40
  host.write_transaction(host.transaction_record(operation, expected, None, "sha256:old", "sha256:new"))
  snapshots = iter([("new", "sha256:new", "dead"), ("old", "sha256:old", "healthy")])
  monkeypatch.setattr(host, "container_health", lambda *_a: next(snapshots))
  monkeypatch.setattr(host, "docker_command", lambda *_a, **_k: None)
  monkeypatch.setattr(host, "compose", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(host, "wait_healthy", lambda *_args, **_kwargs: True)

  def ledger(_config, _cid, command, _operation, **_kwargs):
    assert _operation == operation
    return rearmed if command == "rearm-cutover" else finalized

  monkeypatch.setattr(host, "restart_ledger", ledger)
  statuses = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: statuses.append(fields) or fields,
  )

  assert host.rollback(
    config, operation, expected, "health_check_failed", "new image unhealthy",
  ) == 1
  assert statuses[-1]["state"] == "rolled_back"
  assert statuses[-1]["code"] == expected_code
  assert message_fragment in statuses[-1]["message"]


def test_reconcile_marks_interrupted_active_worker_failed(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text('{"state":"verifying"}', encoding="utf-8")
  abandoned = inbox.parent / f'.request-{"a" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written[-1]["code"] == "worker_interrupted"
  assert not abandoned.exists()


def test_reconcile_cleans_claim_abandoned_before_first_status(tmp_path, monkeypatch):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  abandoned = inbox.parent / f'.request-{"b" * 32}.json'
  abandoned.write_text("{}", encoding="utf-8")
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]
  assert not abandoned.exists()


def test_reconcile_refreshes_an_idle_controller_capability_receipt(
  tmp_path, monkeypatch,
):
  _config, _inbox = _worker_paths(tmp_path, monkeypatch)
  host.STATUS.write_text(
    '{"state":"idle","handoff":"external-cutover-v1"}', encoding="utf-8",
  )
  written = []
  monkeypatch.setattr(
    host, "write_status", lambda _config, **fields: written.append(fields) or fields,
  )

  assert host.reconcile() == 0
  assert written == [{}]


def test_drain_requires_root_open_prepare_accept_order(tmp_path, monkeypatch):
  data = tmp_path / "data"
  data.mkdir()
  operation = "a" * 32
  order = []

  def ledger(_config, _cid, command, value, **_kwargs):
    order.append(command)
    assert value == operation
    return True

  def execute(args, **_kwargs):
    order.append("prepare")
    assert args[-1] == operation
    return subprocess.CompletedProcess([], 0)

  monkeypatch.setattr(host, "restart_ledger", ledger)
  monkeypatch.setattr(host.subprocess, "run", execute)

  result = host.request_drain(
    {"data_dir": data, "control_dir": data / "mobius-rebuild"},
    operation,
    "container",
  )

  assert result is None
  assert order == ["open-cutover", "prepare", "accept-cutover"]


def test_the_helper_accepts_both_request_versions_and_echoes_only_the_nonce():
  assert host.parse_request({"version": 1, "expected_sha": "a" * 40}) == ("a" * 40, None)
  assert host.parse_request({
    "version": 2, "expected_sha": "a" * 40, "nonce": "b" * 32,
  }) == ("a" * 40, "b" * 32)
  for invalid in (
    {"version": 1, "expected_sha": "a" * 40, "nonce": "b" * 32},
    {"version": 2, "expected_sha": "a" * 40},
    {"version": 2, "expected_sha": "a" * 40, "nonce": "not-a-nonce"},
    {"version": 3, "expected_sha": "a" * 40},
  ):
    with pytest.raises(ValueError):
      host.parse_request(invalid)


def test_the_helper_advertises_the_request_versions_it_accepts(tmp_path, monkeypatch):
  monkeypatch.setattr(host, "STATE_DIR", tmp_path / "state")
  monkeypatch.setattr(host, "STATUS", tmp_path / "state" / "status.json")
  control = tmp_path / "control"
  control.mkdir()

  status = host.write_status({"control_dir": control}, state="idle")

  assert status["request_versions"] == [1, 2]


def test_the_helper_names_a_request_in_its_status_before_claiming_it(
  tmp_path, monkeypatch,
):
  """The app treats a request gone from the inbox and absent from the status
  as never claimed, so the helper must publish its nonce first."""
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  nonce = "a" * 32
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "3" * 40, "nonce": nonce,
  }), encoding="utf-8")
  order = []
  real_replace = host.os.replace

  def record_replace(source, target):
    if Path(source) == request:
      order.append("claim")
    return real_replace(source, target)

  def record_status(_config, **fields):
    if fields.get("request_nonce") == nonce and fields.get("state") == "queued":
      order.append("named")
    return fields

  monkeypatch.setattr(host.os, "replace", record_replace)
  monkeypatch.setattr(host, "write_status", record_status)
  monkeypatch.setattr(
    host, "app_container",
    lambda _config: (_ for _ in ()).throw(RuntimeError("stop after the claim")),
  )

  host.run()

  assert order[:2] == ["named", "claim"]


def _requeue(request: Path, content: str) -> None:
  """What the app's withdraw-then-Finish does: a new file at the same path."""
  temp = request.with_name(".app-request.tmp")
  temp.write_text(content, encoding="utf-8")
  os.replace(temp, request)


def test_a_newer_request_that_replaced_the_one_read_stays_queued(
  tmp_path, monkeypatch,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  nonce = "b" * 32
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": nonce,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})
  statuses = []

  def status(_config, **fields):
    statuses.append(fields)
    if fields.get("state") == "queued":
      _requeue(request, newer)  # withdrawn and re-queued before the claim
    return fields

  monkeypatch.setattr(host, "write_status", status)

  assert host.run() == 1
  assert (statuses[-1]["state"], statuses[-1]["code"]) == ("failed", "withdrawn")
  assert statuses[-1]["request_nonce"] == nonce
  assert request.read_text(encoding="utf-8") == newer
  assert not list(_config["control_dir"].glob(".request-*"))


def test_a_request_queued_after_a_withdrawal_survives_the_failed_claim(
  tmp_path, monkeypatch,
):
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      request.unlink()  # withdrawn before the claim
    elif fields.get("code") == "withdrawn":
      _requeue(request, newer)  # a newer Finish before the worker cleans up
    return fields

  monkeypatch.setattr(host, "write_status", status)

  assert host.run() == 1
  assert request.read_text(encoding="utf-8") == newer


def test_a_request_rewritten_in_place_is_returned_not_run(tmp_path, monkeypatch):
  """Same inode, different bytes: the file claimed is not the request read."""
  _config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  rewritten = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      with open(request, "w", encoding="utf-8") as handle:  # same inode
        handle.write(rewritten)
    return fields

  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(
    host, "app_container",
    lambda _config: (_ for _ in ()).throw(AssertionError("must not run")),
  )

  assert host.run() == 1
  assert request.read_text(encoding="utf-8") == rewritten


def test_an_unverified_claim_that_cannot_be_returned_is_kept(tmp_path, monkeypatch):
  config, inbox = _worker_paths(tmp_path, monkeypatch)
  request = inbox / "request.json"
  request.write_text(json.dumps({
    "version": 2, "expected_sha": "4" * 40, "nonce": "b" * 32,
  }), encoding="utf-8")
  newer = json.dumps({"version": 2, "expected_sha": "5" * 40, "nonce": "c" * 32})

  def status(_config, **fields):
    if fields.get("state") == "queued":
      _requeue(request, newer)
    return fields

  def no_link(*_args):
    raise PermissionError("link refused")

  monkeypatch.setattr(host, "write_status", status)
  monkeypatch.setattr(host.os, "link", no_link)

  assert host.run() == 1
  kept = list(config["control_dir"].glob(".unreturned-*"))
  assert [path.read_text(encoding="utf-8") for path in kept] == [newer]


@pytest.fixture
def interrupted_rollback(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  transaction = host.transaction_record(
    "1" * 32, "a" * 40, "2" * 32, "sha256:previous", "sha256:target",
  )
  transaction.update(phase="rollback_started", failure_code="checkout_failed",
                     failure_detail="target checkout failed", handoff_rearmed=True)
  host.write_transaction(transaction)
  calls = []
  monkeypatch.setattr(host, "compose", lambda *a, **k: calls.append(("compose", k)))
  monkeypatch.setattr(host, "docker_command", lambda *a, **k: calls.append(("docker", a)))
  monkeypatch.setattr(host, "restart_ledger", lambda _c, cid, command, operation, **k:
                      calls.append((command, operation, k)) or True)
  return config, transaction, calls


@pytest.mark.parametrize("health", ["healthy", "starting", "unhealthy", "running"])
def test_recovery_observes_exact_previous_boot_without_recreating_or_rearming(
  interrupted_rollback, monkeypatch, health,
):
  config, transaction, calls = interrupted_rollback
  observed = [health]
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], observed[0]))

  def finish_boot(_config, timeout):
    assert timeout == host.ROLLBACK_HEALTH_SECONDS
    observed[0] = "healthy"
    return True

  monkeypatch.setattr(host, "wait_healthy", finish_boot)
  host.recover(config, transaction)
  assert calls == [("finalize-cutover", transaction["operation_id"],
                    {"image": transaction["previous_image"]})]
  status = host.read_json(host.STATUS)
  assert status["state"] == "rolled_back"
  assert status["code"] == "checkout_failed"
  assert "target checkout failed" in status["message"]
  assert status["request_nonce"] == transaction["request_nonce"]
  assert status["operation_id"] == transaction["operation_id"]
  assert host.read_transaction() is None


def test_rollback_timeout_preserves_receipt_and_original_error_then_can_settle(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], "unhealthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  host.recover(config, transaction)
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["failure_detail"] == "target checkout failed"
  assert not calls
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous-container", transaction["previous_image"], "healthy"))
  host.recover(config, host.read_transaction())
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["code"] == "checkout_failed"


@pytest.mark.parametrize("health", ["dead", "exited", "missing"])
def test_failed_rollback_boot_cannot_authorize_another_boot(
  interrupted_rollback, monkeypatch, health,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("", "" if health == "missing" else transaction["previous_image"], health))
  host.recover(config, transaction)
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["phase"] == "rollback_started"


def test_first_rollback_is_journaled_before_rearm_and_uses_exact_image(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  snapshots = iter([
    ("target", transaction["target_image"], "dead"),
    ("previous", transaction["previous_image"], "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(snapshots))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)

  def ledger(_config, cid, command, operation, **kwargs):
    assert operation == transaction["operation_id"]
    assert host.read_transaction()["phase"] == "rollback_started"
    calls.append((command, operation, kwargs))
    return True

  monkeypatch.setattr(host, "restart_ledger", ledger)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "new", "new")
  assert [call[0] for call in calls] == ["docker", "rearm-cutover", "compose", "finalize-cutover"]
  assert calls[2][1]["image"] == transaction["previous_image"]


def test_rollback_refuses_another_operations_receipt(interrupted_rollback):
  config, transaction, calls = interrupted_rollback
  with pytest.raises(RuntimeError, match="does not own"):
    host.rollback(config, "3" * 32, transaction["expected_sha"], "x", "y")
  assert not calls
  assert host.read_transaction() == transaction


def test_consumed_boot_with_uncertain_finalization_settles_without_rearming(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous", transaction["previous_image"], "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: False)
  host.recover(config, transaction)
  assert host.read_json(host.STATUS)["code"] == "handoff_finalize_unconfirmed"
  assert host.read_json(host.STATUS)["state"] == "rolled_back"
  assert host.read_transaction() is None
  assert host.read_json(host.STATUS)["operation_id"] == transaction["operation_id"]
  assert not calls


def test_interrupted_settlement_replays_success_without_touching_container(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  clear = host.clear_transaction
  monkeypatch.setattr(host, "clear_transaction", lambda: (_ for _ in ()).throw(InterruptedError()))
  with pytest.raises(InterruptedError):
    host.settle_transaction(config, transaction, state="succeeded", code=None, message="done")
  monkeypatch.setattr(host, "clear_transaction", clear)
  host.recover(config, host.read_transaction())
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None
  assert not calls


def test_health_wait_does_not_treat_startup_unhealthy_as_terminal(monkeypatch):
  snapshots = iter(["starting", "unhealthy", "unhealthy", "healthy"])
  monkeypatch.setattr(host, "container_health", lambda *a, **k: ("cid", "image", next(snapshots)))
  monkeypatch.setattr(host.time, "sleep", lambda _seconds: None)
  assert host.wait_healthy({}, timeout=1)


def test_every_health_query_is_bounded_by_remaining_observation(monkeypatch):
  calls = []

  def command(args, **kwargs):
    calls.append(kwargs["timeout"])
    return subprocess.CompletedProcess(args, 0, stdout="image running healthy", stderr="")

  monkeypatch.setattr(host, "docker_command", command)
  assert host.wait_healthy({"project": "test"}, timeout=1)
  assert len(calls) == 2 and all(0 < value <= 0.5 for value in calls)


def test_docker_timeout_kills_descendants_without_waiting_for_inherited_pipes(tmp_path):
  import sys
  import time

  marker = tmp_path / "descendant-finished"
  child = f"import time; time.sleep(1); open({str(marker)!r}, 'w').close()"
  parent = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
  )
  started = time.monotonic()
  with pytest.raises(subprocess.TimeoutExpired):
    host.docker_command([sys.executable, "-c", parent], timeout=0.2)
  assert time.monotonic() - started < 3
  time.sleep(1.1)
  assert not marker.exists()


def test_generated_units_allow_the_bounded_recovery_budget(tmp_path):
  import configparser
  from app import platform_activation

  source = INSTALLER.read_text()
  start = source.index("cat >/etc/systemd/system/mobius-rebuild.service")
  end = source.index("chmod 0644 /etc/systemd/system/mobius-rebuild.service", start)
  # Execute the installer's actual unit heredocs, redirecting only their
  # destination. No root installation, Docker, or systemctl is invoked.
  units = source[start:end].replace("/etc/systemd/system/", f"{tmp_path}/")
  subprocess.run(["bash", "-eu", "-c", units], check=True,
                 env={**os.environ, "DATA_SOURCE": str(tmp_path / "data")})
  main = configparser.ConfigParser()
  main.read(tmp_path / "mobius-rebuild.service")
  reconcile = configparser.ConfigParser()
  reconcile.read(tmp_path / "mobius-rebuild-reconcile.service")
  assert main["Service"]["ExecStopPost"] == "/usr/local/libexec/mobius-rebuild-host reconcile"
  assert reconcile["Service"]["ExecStart"] == "/usr/local/libexec/mobius-rebuild-host reconcile"
  assert main["Service"]["Type"] == "oneshot"
  assert "TimeoutStartSec" not in main["Service"]  # don't cap legitimate pulls
  assert main["Service"].getint("TimeoutStopSec") == 900
  assert reconcile["Service"].getint("TimeoutStartSec") == 900
  assert host.ROLLBACK_HEALTH_SECONDS == 300
  assert host.WORKER_REVISION > 2
  marker = SCRIPT.parents[1] / "deployment" / "self-hosted-helper.required"
  assert marker.read_text().strip() == "2"
  assert "# Helper protocol revision: 2 " in source
  impact = platform_activation.classify_activation(
    ["deployment/self-hosted-helper.required"], deployment="self_hosted",
  )
  assert impact["level"] == "host_maintenance"


def test_legacy_running_rollback_adopts_only_its_own_failure_status(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  for key in ("phase", "failure_code", "failure_detail"):
    transaction.pop(key)
  host.write_transaction(transaction)
  host.write_status(config, operation_id=transaction["operation_id"], state="needs_recovery",
                    code="old_failure", message="original rollback timed out")
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("previous", transaction["previous_image"], "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  host.recover(config, transaction)
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["code"] == "old_failure"
  assert "original rollback timed out" in host.read_json(host.STATUS)["message"]


def test_missing_previous_image_never_rearms_or_launches(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  monkeypatch.setattr(host, "container_health", lambda _c: ("target", transaction["target_image"], "dead"))
  monkeypatch.setattr(host, "docker_command", lambda *a, **k:
                      (_ for _ in ()).throw(subprocess.CalledProcessError(1, "docker tag")))
  host.recover(config, transaction)
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction() is not None


def test_wrong_image_after_rollback_never_finalizes_receipt(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction.pop("phase")
  host.write_transaction(transaction)
  snapshots = iter([
    ("target", transaction["target_image"], "dead"),
    ("unexpected", "sha256:unrelated", "healthy"),
  ])
  monkeypatch.setattr(host, "container_health", lambda _c: next(snapshots))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  host.rollback(config, transaction["operation_id"], transaction["expected_sha"], "x", "y")
  assert "finalize-cutover" not in [call[0] for call in calls]
  assert host.read_json(host.STATUS)["code"] == "rollback_wrong_image"
  assert host.read_transaction() is not None


def test_settlement_io_failure_cannot_roll_back_a_completed_success(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction["outcome"] = {
    "state": "succeeded", "code": None, "operation_id": transaction["operation_id"],
    "expected_sha": transaction["expected_sha"], "request_nonce": transaction["request_nonce"],
  }
  host.write_transaction(transaction)
  assert host.rollback(config, transaction["operation_id"], transaction["expected_sha"],
                       "replacement_failed", "status write failed") == 0
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None


def test_interrupted_target_boot_gets_observed_before_rollback(interrupted_rollback, monkeypatch):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  host.write_transaction(transaction)
  health = ["unhealthy"]
  monkeypatch.setattr(host, "container_health", lambda _c:
                      ("target", transaction["target_image"], health[0]))

  def finish_target(_config):
    health[0] = "healthy"
    return True

  monkeypatch.setattr(host, "wait_healthy", finish_target)
  adopted = []
  monkeypatch.setattr(host, "adopt_from_image", lambda image: adopted.append(image) or "adopted")
  monkeypatch.setattr(host, "retain_images", lambda *_args: None)
  monkeypatch.setattr(host, "verify_served_generation", lambda *_a: None)
  host.recover(config, transaction)
  assert [call[0] for call in calls] == ["finalize-cutover"]
  assert host.read_json(host.STATUS)["state"] == "succeeded"
  assert host.read_transaction() is None
  assert adopted == [transaction["target_image"]]
  assert host.read_json(host.STATUS)["worker_adoption"] == "adopted"


def test_transient_health_query_failure_does_not_undo_boot(monkeypatch):
  probes = iter([subprocess.CalledProcessError(1, "docker inspect"), "healthy"])

  def observe(*args, **kwargs):
    result = next(probes)
    if isinstance(result, Exception):
      raise result
    return "cid", "image", result

  monkeypatch.setattr(host, "container_health", observe)
  monkeypatch.setattr(host.time, "sleep", lambda _delay: None)
  assert host.wait_healthy({}, timeout=1)


def test_unknown_target_health_at_deadline_preserves_boot_and_receipt(
  interrupted_rollback, monkeypatch,
):
  config, transaction, calls = interrupted_rollback
  transaction["phase"] = "replacement_started"
  host.write_transaction(transaction)
  clock = [0.0]
  probes = [0]
  monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(host.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

  def observe(*args, **kwargs):
    probes[0] += 1
    if probes[0] == 1:
      return "target", transaction["target_image"], "starting"
    raise subprocess.CalledProcessError(1, "docker inspect")

  monkeypatch.setattr(host, "container_health", observe)
  host.recover(config, transaction)
  assert clock[0] == 180
  assert not calls
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_transaction()["phase"] == "replacement_started"


def test_installer_stops_before_publishing_units_when_seeding_refuses(tmp_path):
  source = INSTALLER.read_text()
  start = source.index("MOBIUS_REBUILD_LOCK_HELD=1")
  end = source.index("install -D", start)
  python = tmp_path / "python"
  python.write_text("#!/bin/sh\n[ \"$MOBIUS_REBUILD_LOCK_HELD\" = 1 ] || exit 99\nexit 1\n")
  python.chmod(0o700)
  seed = source[start:end].replace("/usr/bin/python3", str(python))
  published = tmp_path / "units-published"
  result = subprocess.run(
    ["bash", "-eu", "-c", seed + '\n touch "$PUBLISHED"'],
    env={**os.environ, "ROOT": str(tmp_path), "PUBLISHED": str(published)},
    capture_output=True, text=True,
  )
  assert result.returncode == 1
  assert not published.exists()


def _real_cutover(tmp_path, monkeypatch, *, consume=True):
  import time
  from tests.test_restart_ledger import _bind, _load_supervisor
  from app import restart_ledger as platform_ledger

  proof = host.cutover_boot_consumed
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  ledger = _load_supervisor()
  ledger_root = tmp_path / "ledger-data"
  ledger_root.mkdir()
  config["data_dir"] = ledger_root
  _bind(ledger, ledger_root, monkeypatch)
  monkeypatch.setattr(host, "cutover_boot_consumed", lambda c, op:
                      proof(c, op, trusted_uid=os.getuid(), trusted_gid=os.getgid()))
  now = time.time()
  operation, expected, nonce = "1" * 32, "a" * 40, "2" * 32
  source_boot, target_boot = "source-boot-1234", "target-boot-1234"
  ledger.begin_boot(source_boot, now=now)
  assert ledger.open_cutover(operation, now=now + 1)
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot, nonce=nonce, cutover_id=operation, runs=[], now=now + 1,
  )
  assert ledger.accept_cutover(operation, now=now + 2)
  if consume:
    assert ledger.begin_boot(target_boot, now=now + 3)
    assert not ledger.ACCEPTED_PATH.exists()
  transaction = host.transaction_record(operation, expected, nonce, "sha256:previous", "sha256:target")
  host.write_transaction(transaction)
  return config, transaction, ledger, now


@pytest.mark.parametrize("state", ["succeeded", "rolled_back"])
@pytest.mark.parametrize("crash", ["before_finalize", "after_finalize", "after_clean_write"])
def test_verified_service_crash_replays_honest_outcome_with_real_receipt(
  tmp_path, monkeypatch, state, crash,
):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  operation, nonce = transaction["operation_id"], transaction["request_nonce"]
  code = "checkout_failed" if state == "rolled_back" else None
  if code:
    transaction.update(failure_code=code, failure_detail="target checkout failed")
  host.write_transaction(transaction)
  writes = [0]
  write = host.write_transaction

  def persist(value):
    if not value.get("outcome"):
      write(value)
      return
    writes[0] += 1
    if writes[0] == 2 and crash == "after_finalize":
      assert not ledger.CUTOVER_RECEIPT_PATH.exists()
      raise KeyboardInterrupt
    write(value)
    if writes[0] == 2 and crash == "after_clean_write":
      raise KeyboardInterrupt

  def finalize(_config, _cid, command, owning_operation, **kwargs):
    assert command == "finalize-cutover" and owning_operation == operation
    assert host.read_transaction()["outcome"]["code"] == "handoff_finalize_unconfirmed"
    if crash == "before_finalize":
      raise KeyboardInterrupt
    return ledger.finalize_cutover(operation, now=now + 4)

  monkeypatch.setattr(host, "write_transaction", persist)
  monkeypatch.setattr(host, "restart_ledger", finalize)
  image = transaction["previous_image"] if state == "rolled_back" else transaction["target_image"]
  monkeypatch.setattr(host, "container_health", lambda _c: ("cid", image, "healthy"))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: True)
  monkeypatch.setattr(host, "verify_served_generation", lambda *a: None)
  with pytest.raises(KeyboardInterrupt):
    if state == "rolled_back":
      host.rollback(config, operation, transaction["expected_sha"], code, "target checkout failed")
    else:
      host.recover(config, transaction)
  assert ledger.CUTOVER_RECEIPT_PATH.exists() == (crash == "before_finalize")
  monkeypatch.setattr(host, "write_transaction", write)

  def forbidden(*args, **kwargs):
    pytest.fail("replay must not query, finalize, rearm, recreate or adopt")

  for name in ("container_health", "restart_ledger", "compose", "adopt_from_image", "cutover_boot_consumed"):
    monkeypatch.setattr(host, name, forbidden)
  host.recover(config, host.read_transaction())
  status = host.read_json(host.STATUS)
  assert status["state"] == state
  assert status["code"] == (code if crash == "after_clean_write" else "handoff_finalize_unconfirmed")
  assert status["operation_id"] == operation and status["request_nonce"] == nonce
  assert status["failure_code"] == code
  assert host.read_transaction() is None
  # A leftover receipt by itself cannot authorize a later boot.
  assert not ledger.begin_boot("unrelated-boot-1234", now=now + 5)


@pytest.mark.parametrize("failure", ["finalize", "cleanup", "adoption"])
def test_verified_success_side_effect_failure_never_becomes_rollback(
  interrupted_rollback, monkeypatch, failure,
):
  config, transaction, calls = interrupted_rollback

  def fail(*args, **kwargs):
    assert host.read_transaction()["outcome"]["state"] == "succeeded"
    raise RuntimeError("side effect failed")

  monkeypatch.setattr(host, "retain_images", fail if failure == "cleanup" else lambda *a: None)
  monkeypatch.setattr(host, "adopt_from_image", fail if failure == "adoption" else lambda *a: "adopted")
  if failure == "finalize":
    monkeypatch.setattr(host, "restart_ledger", fail)
  host.finish_verified(config, transaction, "cid", transaction["target_image"],
                       state="succeeded", code=None, message="Container rebuilt successfully.")
  status = host.read_json(host.STATUS)
  assert status["state"] == "succeeded"
  assert status["code"] == ("handoff_finalize_unconfirmed" if failure == "finalize" else None)
  assert host.read_transaction() is None
  assert not any(call[0] in {"compose", "rearm-cutover"} for call in calls)


@pytest.mark.parametrize("pending", ["same_boot", "rearmed"])
def test_pending_accepted_authorization_prevents_terminal_outcome(tmp_path, monkeypatch, pending):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch, consume=pending != "same_boot")
  if pending == "rearmed":
    assert ledger.rearm_cutover(transaction["operation_id"], now=now + 4)
  assert ledger.ACCEPTED_PATH.exists()
  before = ledger.ACCEPTED_PATH.read_bytes()
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: pytest.fail("must not finalize pending authorization"))
  assert not host.finish_verified(config, transaction, "cid", transaction["previous_image"],
                                  state="rolled_back", code="failed", message="Previous image healthy.")
  assert "outcome" not in host.read_transaction()
  assert host.read_json(host.STATUS)["state"] == "needs_recovery"
  assert host.read_json(host.STATUS)["code"] == "handoff_boot_unconfirmed"
  assert ledger.ACCEPTED_PATH.read_bytes() == before


@pytest.mark.parametrize("invalid", [
  "operation", "nonce", "version", "action", "source", "target", "boot",
  "receipt_nonce", "receipt_operation", "receipt_null", "malformed", "missing_ack",
  "symlink_ack", "symlink_ledger", "writable_ack", "writable_ledger", "read_error",
  "accepted_stat_error", "accepted_symlink", "changed_boot", "wrong_owner",
  "oversized_ack", "fifo_ack", "receipt_symlink", "numeric_nonce", "numeric_source",
])
def test_consumed_boot_proof_refuses_uncertain_or_untrusted_evidence(
  tmp_path, monkeypatch, invalid,
):
  config, transaction, ledger, _now = _real_cutover(tmp_path, monkeypatch)
  ack = json.loads(ledger.ACK_PATH.read_text())
  updates = {
    "operation": ("cutover_id", "9" * 32), "nonce": ("nonce", "!"),
    "version": ("version", 2), "action": ("action", "restart"),
    "source": ("source_boot_id", "!"), "target": ("target_boot_id", "another-boot-1234"),
    "numeric_nonce": ("nonce", 12345678), "numeric_source": ("source_boot_id", 12345678),
  }
  if invalid in updates:
    key, value = updates[invalid]
    ack[key] = value
    ledger._write_json(ledger.ACK_PATH, ack, 0o444)
    if invalid == "numeric_nonce":
      ledger.CUTOVER_RECEIPT_PATH.unlink()  # type check must stand on its own
  elif invalid == "boot":
    ledger._atomic_write(ledger.BOOT_PATH, b"another-boot-1234", 0o444)
  elif invalid in {"receipt_nonce", "receipt_operation", "receipt_null"}:
    receipt = json.loads(ledger.CUTOVER_RECEIPT_PATH.read_text())
    if invalid == "receipt_null":
      receipt = None
    else:
      receipt["nonce" if invalid == "receipt_nonce" else "cutover_id"] = "different-token-1234"
    ledger._atomic_write(ledger.CUTOVER_RECEIPT_PATH, json.dumps(receipt).encode(), 0o600)
  elif invalid == "receipt_symlink":
    target = ledger.CUTOVER_RECEIPT_PATH.with_name("copied-receipt")
    ledger.CUTOVER_RECEIPT_PATH.rename(target)
    ledger.CUTOVER_RECEIPT_PATH.symlink_to(target)
  elif invalid == "oversized_ack":
    ledger._atomic_write(ledger.ACK_PATH, b" " * 65537, 0o444)
  elif invalid == "fifo_ack":
    ledger.ACK_PATH.unlink()
    os.mkfifo(ledger.ACK_PATH, 0o444)
  elif invalid == "malformed":
    ledger._atomic_write(ledger.ACK_PATH, b"{", 0o444)
  elif invalid == "missing_ack":
    ledger.ACK_PATH.unlink()
  elif invalid == "symlink_ack":
    target = ledger.ACK_PATH.with_name("copied-ack")
    ledger.ACK_PATH.rename(target)
    ledger.ACK_PATH.symlink_to(target)
  elif invalid == "symlink_ledger":
    target = ledger.LEDGER_DIR.with_name("copied-ledger")
    ledger.LEDGER_DIR.rename(target)
    ledger.LEDGER_DIR.symlink_to(target, target_is_directory=True)
  elif invalid in {"writable_ack", "writable_ledger"}:
    (ledger.ACK_PATH if invalid == "writable_ack" else ledger.LEDGER_DIR).chmod(0o777)
  elif invalid == "read_error":
    monkeypatch.setattr(host.os, "read", lambda *_a: (_ for _ in ()).throw(PermissionError()))
  elif invalid == "accepted_stat_error":
    stat = host.os.stat
    def deny(path, *args, **kwargs):
      if path == "accepted.json":
        raise PermissionError
      return stat(path, *args, **kwargs)
    monkeypatch.setattr(host.os, "stat", deny)
  elif invalid == "accepted_symlink":
    ledger.ACCEPTED_PATH.symlink_to(tmp_path / "missing")
  elif invalid == "changed_boot":
    read = host.os.read
    boot_reads = [0]
    def changing(fd, size):
      raw = read(fd, size)
      if raw.strip() == b"target-boot-1234":
        boot_reads[0] += 1
        if boot_reads[0] > 1:
          return b"changed-boot-1234"
      return raw
    monkeypatch.setattr(host.os, "read", changing)
  elif invalid == "wrong_owner":
    fstat = host.os.fstat
    def changed_owner(fd):
      fields = list(fstat(fd))
      fields[4] = os.getuid() + 1
      return os.stat_result(fields)
    monkeypatch.setattr(host.os, "fstat", changed_owner)
  assert not host.cutover_boot_consumed(config, transaction["operation_id"])
  assert "outcome" not in host.read_transaction()


def test_missing_retired_receipt_allows_only_degraded_settlement(tmp_path, monkeypatch):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  assert ledger.finalize_cutover(transaction["operation_id"], now=now + 4)
  monkeypatch.setattr(host, "restart_ledger", lambda _c, _cid, _cmd, op, **_k:
                      ledger.finalize_cutover(op, now=now + 5))
  assert host.finish_verified(config, transaction, "cid", transaction["previous_image"],
                              state="rolled_back", code="checkout_failed", message="Restored.")
  assert host.read_json(host.STATUS)["code"] == "handoff_finalize_unconfirmed"
  assert host.read_transaction() is None


def test_new_operation_does_not_inherit_previous_failure_detail(tmp_path, monkeypatch):
  config, _inbox = _worker_paths(tmp_path, monkeypatch)
  host.write_status(config, operation_id="1" * 32, failure_code="old", failure_detail="old error")
  host.write_status(config, operation_id="2" * 32, state="queued")
  status = host.read_json(host.STATUS)
  assert status["failure_code"] is None and status["failure_detail"] is None


def test_consumed_rollback_ack_may_have_different_source_than_original_receipt(tmp_path, monkeypatch):
  config, transaction, ledger, now = _real_cutover(tmp_path, monkeypatch)
  assert ledger.rearm_cutover(transaction["operation_id"], now=now + 4)
  assert ledger.begin_boot("rollback-boot-1234", now=now + 5)
  ack = json.loads(ledger.ACK_PATH.read_text())
  receipt = json.loads(ledger.CUTOVER_RECEIPT_PATH.read_text())
  assert ack["source_boot_id"] != receipt["source_boot_id"]
  assert host.cutover_boot_consumed(config, transaction["operation_id"])


def test_consumed_proof_rechecks_pending_authorization_after_snapshot(tmp_path, monkeypatch):
  config, transaction, ledger, _now = _real_cutover(tmp_path, monkeypatch)
  stat = host.os.stat
  checks = [0]

  def appearing(path, *args, **kwargs):
    if path == "accepted.json":
      checks[0] += 1
      if checks[0] == 2:
        return ledger.ACK_PATH.lstat()  # the second lookup now finds a file
    return stat(path, *args, **kwargs)

  monkeypatch.setattr(host.os, "stat", appearing)
  assert not host.cutover_boot_consumed(config, transaction["operation_id"])
  assert checks[0] == 2
