"""The self-hosted container-only upgrade request (scripts/request-container-upgrade.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "request-container-upgrade.py"
SPEC = importlib.util.spec_from_file_location("request_container_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
     "-c", "commit.gpgsign=false", *args],
    capture_output=True, text=True, check=True,
  ).stdout.strip()


def _commit(
  repo: Path, name: str, lock: str | None = None, *, bridge: bool = False,
) -> str:
  (repo / name).write_text(name + "\n")
  if lock is not None:
    (repo / "backend").mkdir(exist_ok=True)
    (repo / "backend/requirements.lock").write_text(lock)
  if bridge:
    for required in upgrade.BRIDGE_FILES:
      (repo / required).parent.mkdir(parents=True, exist_ok=True)
      (repo / required).write_text("1\n")
  _git(repo, "add", "-A")
  _git(repo, "commit", "-q", "-m", name)
  return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def install(tmp_path, monkeypatch):
  """An installation running image ``old``, source at release ``old``, with
  ``new`` fetched as origin/main and an idle helper."""
  data = tmp_path / "data"
  platform = data / "platform"
  platform.mkdir(parents=True)
  _git(platform, "init", "-q", "-b", "main")
  old = _commit(platform, "old-release", lock="pkg==1\n")
  new = _commit(platform, "new-release", lock="pkg==2\n", bridge=True)
  _git(platform, "reset", "-q", "--hard", old)
  _git(platform, "branch", "-f", "upstream", old)
  _git(platform, "update-ref", "refs/remotes/origin/main", new)
  control = data / "mobius-rebuild"
  (control / "inbox").mkdir(parents=True)
  (control / "status.json").write_text(json.dumps({"state": "succeeded"}))
  build_info = tmp_path / "build-info.json"
  build_info.write_text(json.dumps({"sha": old}))
  monkeypatch.setattr(upgrade, "DATA", data)
  monkeypatch.setattr(upgrade, "PLATFORM", platform)
  monkeypatch.setattr(upgrade, "CONTROL", control)
  monkeypatch.setattr(upgrade, "BUILD_INFO", build_info)
  monkeypatch.setattr(upgrade, "RECONCILE_LOCK", data / ".platform-reconcile.lock")
  # The fixture's first release stands in for the real #1311 floor.
  monkeypatch.setattr(upgrade, "SERVED_CODE_FLOOR", old)
  return {"data": data, "control": control, "old": old, "new": new, "platform": platform}


def _request(install) -> Path:
  return install["control"] / "inbox" / "request.json"


def test_requests_only_the_container_for_the_fetched_release(install, capsys):
  assert upgrade.main([]) == 0

  assert json.loads(_request(install).read_text()) == {
    "version": 1, "expected_sha": install["new"],
  }
  assert "install the update from Settings" in capsys.readouterr().out
  # Nothing else changed: the source still serves its own release.
  assert _git(install["platform"], "rev-parse", "HEAD") == install["old"]


def test_check_reports_readiness_without_requesting(install, capsys):
  assert upgrade.main(["--check"]) == 0
  assert "Ready" in capsys.readouterr().out
  assert not _request(install).exists()


def test_an_explicit_target_must_be_a_complete_fetched_release(install):
  assert upgrade.main(["--target", install["new"][:12]]) == 1
  assert upgrade.main(["--target", "f" * 40]) == 1
  assert not _request(install).exists()
  assert upgrade.main(["--target", install["new"]]) == 0


@pytest.mark.parametrize("case", [
  "same_image", "older_than_source_release", "pending_update", "parked_update",
  "helper_running", "request_queued", "helper_missing", "target_without_bridge",
  "no_official_history", "served_code_too_old", "target_packages_differ",
])
def test_refuses_without_touching_the_inbox_when_a_precondition_fails(
  install, case, capsys,
):
  data, control = install["data"], install["control"]
  upgrade_target: list[str] = []
  if case == "same_image":
    upgrade.BUILD_INFO.write_text(json.dumps({"sha": install["new"]}))
  elif case == "older_than_source_release":
    # The installed source took packages from an official release newer than
    # the requested target.
    later = _commit(install["platform"], "later-release", lock="pkg==3\n", bridge=True)
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", later)
    _git(install["platform"], "branch", "-f", "upstream", later)
    upgrade_target.append(install["new"])
  elif case == "target_without_bridge":
    bare = _commit(install["platform"], "release-before-bridge", lock="pkg==2\n")
    _git(install["platform"], "reset", "-q", "--hard", install["old"])
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", bare)
  elif case == "served_code_too_old":
    upgrade.SERVED_CODE_FLOOR = install["new"]
  elif case == "target_packages_differ":
    # An explicit historical target whose packages are not the latest's.
    first = _commit(install["platform"], "first-bridge", lock="pkg==2\n", bridge=True)
    latest = _commit(install["platform"], "latest", lock="pkg==4\n", bridge=True)
    _git(install["platform"], "reset", "-q", "--hard", install["old"])
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", latest)
    upgrade_target.append(first)
  elif case == "no_official_history":
    _git(install["platform"], "update-ref", "-d", "refs/remotes/origin/main")
    upgrade_target.append(install["new"])
  elif case == "pending_update":
    (data / ".platform-prepared-update.json").write_text("{}")
  elif case == "parked_update":
    (data / ".platform-conflict").write_text("x")
  elif case == "helper_running":
    (control / "status.json").write_text(json.dumps({"state": "replacing"}))
  elif case == "request_queued":
    _request(install).write_text("{}")
  elif case == "helper_missing":
    (control / "status.json").unlink()

  before = _request(install).read_text() if _request(install).exists() else None
  args = ["--target", upgrade_target[0]] if upgrade_target else []

  assert upgrade.main(args) == 1

  after = _request(install).read_text() if _request(install).exists() else None
  assert after == before
  assert "Container upgrade not requested" in capsys.readouterr().err


@pytest.mark.parametrize("installed,allowed", [(None, False), (5, False), (True, False), (6, True)])
def test_an_uncertified_helper_is_asked_to_reinstall_instead_of_upgrading(
  install, installed, allowed, capsys,
):
  # Production 2026-10-12: a helper whose installer never certified the
  # release's protocol ran its old cutover again. Refuse before the inbox.
  target = _commit(install["platform"], "helper-protocol-release", lock="pkg==2\n", bridge=True)
  (install["platform"] / "deployment").mkdir(exist_ok=True)
  (install["platform"] / "deployment/self-hosted-helper.required").write_text("6\n")
  _git(install["platform"], "add", "-A")
  _git(install["platform"], "commit", "-q", "-m", "require helper protocol 6")
  target = _git(install["platform"], "rev-parse", "HEAD")
  _git(install["platform"], "reset", "-q", "--hard", install["old"])
  _git(install["platform"], "update-ref", "refs/remotes/origin/main", target)
  status = {"state": "succeeded"}
  if installed is not None:
    status["helper_protocol"] = installed
  (install["control"] / "status.json").write_text(json.dumps(status))

  assert (upgrade.main([]) == 0) is allowed
  assert _request(install).exists() is allowed
  if not allowed:
    assert "install-rebuild-helper.sh" in capsys.readouterr().err


def test_a_release_marker_on_a_local_commit_does_not_block_the_upgrade(install):
  """Older updaters could record a local merge commit as the installed
  release. The target's own history still proves its packages are newer."""
  local = _commit(install["platform"], "local-reconcile")
  _git(install["platform"], "branch", "-f", "upstream", local)

  assert upgrade.main(["--check"]) == 0


def test_a_local_package_declaration_does_not_block_the_upgrade(install):
  (install["platform"] / "backend/requirements.lock").write_text("pkg==1\nlocal==1\n")

  assert upgrade.main(["--check"]) == 0


def test_a_committed_local_package_edit_under_a_local_release_marker_is_allowed(install):
  """The marker can name a local commit that also carries a local package
  declaration; neither makes the declaration an official newer release."""
  local = _commit(install["platform"], "local-packages", lock="pkg==1\nlocal==1\n")
  _git(install["platform"], "branch", "-f", "upstream", local)

  assert upgrade.main(["--check"]) == 0


def test_checks_and_request_hold_the_updater_lock(install, monkeypatch):
  held = []
  real = upgrade.check_no_pending_update

  def observed():
    import fcntl
    with open(upgrade.RECONCILE_LOCK, "a+") as other:
      try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(False)
      except BlockingIOError:
        held.append(True)
    return real()

  monkeypatch.setattr(upgrade, "check_no_pending_update", observed)
  assert upgrade.main([]) == 0
  assert held == [True]


def test_refuses_a_shallow_checkout_whose_history_cannot_prove_anything(
  install, tmp_path, monkeypatch, capsys,
):
  """Image seeds are shallow; an input missing from shallow history is not
  proof that it is local."""
  shallow = tmp_path / "shallow"
  _git(install["platform"], "branch", "-f", "release", install["new"])
  subprocess.run(
    ["git", "clone", "-q", "--depth", "2", "--branch", "release",
     f"file://{install['platform']}", str(shallow)],
    check=True,
  )
  _git(shallow, "reset", "-q", "--hard", install["old"])
  _git(shallow, "branch", "-f", "upstream", install["old"])
  _git(shallow, "update-ref", "refs/remotes/origin/main", install["new"])
  monkeypatch.setattr(upgrade, "PLATFORM", shallow)

  assert upgrade.main(["--check"]) == 1
  assert "shallow history" in capsys.readouterr().err
