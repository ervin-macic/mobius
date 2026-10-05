"""The boot transaction's failure report is what an operator and a repair agent
have to go on, so it must carry the failing command's own explanation."""

import json
import os
import subprocess
from types import SimpleNamespace

from app import platform_boot, platform_update


def _git_refusal() -> subprocess.CalledProcessError:
  return subprocess.CalledProcessError(
    128, ["git", "read-tree", "-n", "-m", "-u", "before", "target"],
    output="", stderr="error: Entry 'backend/app/x.py' not uptodate. Cannot merge.\n",
  )


def test_failure_detail_carries_git_stderr_through_a_wrapping_error():
  try:
    try:
      raise _git_refusal()
    except subprocess.CalledProcessError as exc:
      raise platform_update.BootTransactionError("could not swap in the update") from exc
  except platform_update.BootTransactionError as wrapped:
    detail = platform_boot.failure_detail(wrapped)

  assert detail.startswith("BootTransactionError('could not swap in the update')")
  assert "stderr: error: Entry 'backend/app/x.py' not uptodate. Cannot merge." in detail


def test_activate_failure_is_printed_and_recorded_with_git_stderr(
  tmp_path, monkeypatch, capsys,
):
  log = tmp_path / "platform-boot.jsonl"
  marker = tmp_path / "boot-transaction"
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_update, "BOOT_TRANSACTION_MARKER", marker)
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-1")

  def refuse(_repo):
    raise _git_refusal()

  monkeypatch.setattr(platform_update, "settle_prepared_update_for_this_image", refuse)

  umask = os.umask(0)
  os.umask(umask)
  try:
    assert platform_boot.main(["platform_boot", "activate"]) == 1
  finally:
    os.umask(umask)  # main sets the boot's umask for its whole process

  assert "not uptodate. Cannot merge." in capsys.readouterr().err
  assert not marker.exists()  # a failed transaction never publishes its protocol
  [record] = [json.loads(line) for line in log.read_text().splitlines()]
  assert record["boot_id"] == "boot-1"
  assert record["command"] == "activate" and record["ok"] is False
  assert "not uptodate. Cannot merge." in record["detail"]


def test_boot_log_keeps_only_the_most_recent_runs(tmp_path, monkeypatch):
  log = tmp_path / "platform-boot.jsonl"
  monkeypatch.setattr(platform_boot, "_BOOT_LOG_RECORDS", 3)
  for number in range(5):
    platform_boot.record_boot_run("guard", ok=True, detail=f"run {number}", log=log)

  details = [json.loads(line)["detail"] for line in log.read_text().splitlines()]
  assert details == ["run 2", "run 3", "run 4"]
  assert sorted(path.name for path in tmp_path.iterdir()) == ["platform-boot.jsonl"]


def test_an_unwritable_boot_log_never_fails_the_boot(tmp_path, capsys):
  missing_dir = tmp_path / "absent" / "platform-boot.jsonl"

  platform_boot.record_boot_run("activate", ok=True, detail="none", log=missing_dir)

  assert "could not record this run" in capsys.readouterr().err


def test_a_damaged_boot_log_never_fails_a_settled_boot(tmp_path, monkeypatch):
  """A boot that settled the platform must exit 0 whatever the log holds;
  otherwise every later boot would refuse to start on the same file."""
  log = tmp_path / "platform-boot.jsonl"
  log.write_bytes(b'{"command": "guard"}\n\xff\xfe not text\n')
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_update, "boot_guard_sync", lambda: "clean")

  umask = os.umask(0)
  os.umask(umask)
  try:
    assert platform_boot.main(["platform_boot", "guard"]) == 0
  finally:
    os.umask(umask)

  last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
  assert last["command"] == "guard" and last["ok"] is True


def test_an_unexpected_recording_error_never_escapes(tmp_path, monkeypatch, capsys):
  def unserializable(_record):
    raise TypeError("not serializable")

  monkeypatch.setattr(platform_boot, "json", SimpleNamespace(dumps=unserializable))
  platform_boot.record_boot_run("guard", ok=True, detail="clean", log=tmp_path / "log")

  assert "could not record this run" in capsys.readouterr().err
  assert list(tmp_path.iterdir()) == []


def test_a_failed_boot_log_write_leaves_no_staged_file(tmp_path, monkeypatch, capsys):
  log = tmp_path / "platform-boot.jsonl"

  def refuse(_staged, _log):
    raise OSError("disk full")

  monkeypatch.setattr(platform_boot.os, "replace", refuse)
  platform_boot.record_boot_run("guard", ok=True, detail="clean", log=log)

  assert "could not record this run" in capsys.readouterr().err
  assert list(tmp_path.iterdir()) == []
