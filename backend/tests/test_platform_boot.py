"""The boot transaction's failure report is what an operator and a repair agent
have to go on, so it must carry the failing command's own explanation."""

import json
import subprocess

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

  assert platform_boot.main(["platform_boot", "activate"]) == 1

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
