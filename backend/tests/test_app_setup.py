"""Restoration is observable and stays outside readiness/settlement."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import app_setup


async def until(predicate):
  async def poll():
    while not predicate():
      await asyncio.sleep(0.01)
  await asyncio.wait_for(poll(), 3)


@pytest.fixture
def setup(monkeypatch, tmp_path):
  monkeypatch.setattr(app_setup, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: None)
  monkeypatch.setattr(app_setup.fs_locks, "_lifecycle_lock", asyncio.Lock())
  return app_setup.Runner()


@pytest.mark.asyncio
@pytest.mark.parametrize("codes,state", [([0], "ready"), ([1, 0, 0], "ready"), ([2], "conflict"), ([1, 3], "failed"), ([1, 0, 1], "failed")])
async def test_protocol_and_persistence(setup, codes, state):
  calls = []
  async def run(action):
    calls.append(action)
    return codes[len(calls)-1], "diagnostic"
  await setup.step("step", run)
  assert calls == ["check", "apply", "check"][:len(codes)]
  assert app_setup.status() == {"step": {"state": state, "output": "diagnostic"}}


@pytest.mark.asyncio
async def test_script_and_tail(setup, tmp_path):
  code, output = await app_setup.command(["/bin/sh", "-c", "printf 'Inst foo\n%10000s' x"], plan=True)
  assert code == -1 and len(output) == 8192
  code, output = await app_setup.command([sys.executable, "-m", "app.app_python_env", str(tmp_path), "7", str(tmp_path)])
  assert code == 0, output


@pytest.mark.asyncio
async def test_apt_combines_constraints_and_refuses_conflict(setup, monkeypatch):
  command = AsyncMock(side_effect=[(100, "missing lists"), (0, "updated"), (100, "unmet dependencies")])
  monkeypatch.setattr(app_setup, "command", command)
  requirements = ["foo (>= 2)", "foo (<< 2)"]
  await setup.step("apt", lambda action: app_setup.apt(requirements, action))
  assert command.await_count == 3
  assert command.call_args.args[0] == ["sudo", "-n", "apt-get", "--simulate", "--no-remove", "satisfy", *requirements]
  assert app_setup.status()["apt"]["state"] == "conflict"
  command.reset_mock()
  command.side_effect = [(-1, "Inst foo"), (0, "updated"), (-1, "Inst foo"), (0, "installed"), (0, "")]
  await setup.step("apt", lambda action: app_setup.apt(requirements, action))
  assert command.call_args_list[3].args[0] == ["sudo", "-n", "apt-get", "--yes", "--no-remove", "satisfy", *requirements]
  assert app_setup.status()["apt"]["state"] == "ready"
  command.reset_mock()
  command.side_effect = [(100, "missing"), (100, "repository unavailable")]
  await setup.step("apt", lambda action: app_setup.apt(requirements, action))
  assert command.call_args.args[0] == ["sudo", "-n", "apt-get", "update", "--error-on=any"]
  assert app_setup.status()["apt"]["state"] == "failed"


@pytest.mark.asyncio
async def test_nonblocking_readiness_and_settlement(setup, monkeypatch):
  import httpx
  ready = False
  record = {"state": "swapped", "operation": {"id": "replacement"}}
  async def get(*args, **kwargs):
    return SimpleNamespace(status_code=200 if ready else 503)
  monkeypatch.setattr(httpx.AsyncClient, "get", get)
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: record)
  real_sleep = asyncio.sleep
  async def tick(_):
    await real_sleep(0)
  monkeypatch.setattr(app_setup.asyncio, "sleep", tick)
  ran = asyncio.Event()
  async def reconcile():
    ran.set()
  setup.reconcile = AsyncMock(side_effect=reconcile)
  task = asyncio.create_task(setup.serve())
  try:
    for _ in range(10):
      await real_sleep(0)
    setup.reconcile.assert_not_awaited()
    ready = True
    for _ in range(10):
      await real_sleep(0)
    setup.reconcile.assert_not_awaited()
    record = None
    await asyncio.wait_for(ran.wait(), 2)
    assert setup.reconcile.await_count == 1
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_authenticated_status_and_rerun(client, auth, setup, monkeypatch, tmp_path):
  setup.record("old", "pending", "interrupted")
  assert client.get("/api/setup").status_code == 401
  assert client.get("/api/setup", headers=auth).json()["old"]["state"] == "pending"
  calls = []
  monkeypatch.setattr(app_setup, "request_run", lambda **kwargs: calls.append(kwargs))
  assert client.post("/api/setup/rerun", headers=auth).status_code == 202
  assert calls == [{"cancel": True}]
  for corrupt in ["{", "null", "[]"]:
    (tmp_path / "setup-status.json").write_text(corrupt)
    assert client.get("/api/setup", headers=auth).json() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_reconcile_accepted_and_instance_steps_sequentially(setup, monkeypatch, tmp_path, db, malformed):
  from app import models
  accepted = tmp_path / "accepted"
  accepted.mkdir()
  instance = tmp_path / "customizations"
  instance.mkdir()
  for root, package in [(accepted, "foo (>= 2)"), (instance, "bar")]:
    (root / "mobius.json").write_text(json.dumps({
      "setup": {"steps": ["restore.sh"], "apt": [package]},
      "source_files": ["restore.sh"],
    }))
    (root / "restore.sh").write_text("#!/bin/sh\nexit 0\n")
  row = models.App(name="Test", slug="setup-test", source_dir="/never-read-dirty", runtime_revision="a" * 64)
  db.add(row)
  db.commit()
  monkeypatch.setattr(app_setup.applied_app_runtime, "runtime_root", lambda app: accepted)
  if malformed:
    (accepted / "mobius.json").write_text("[]")
  events = []
  async def apt(requirements, action):
    events.append(("apt", requirements, action))
    return 0, ""
  async def command(argv, cwd):
    events.append(("script", cwd, argv[-1]))
    return 0, ""
  monkeypatch.setattr(app_setup, "apt", apt)
  monkeypatch.setattr(app_setup, "command", command)
  await setup.reconcile()
  if malformed:
    assert events == [("script", instance, "check")]
    assert app_setup.status()["apt"]["state"] == "failed"
  else:
    assert events == [("apt", ["foo (>= 2)", "bar"], "check"),
                      ("script", accepted, "check"), ("script", instance, "check")]


@pytest.mark.asyncio
async def test_new_update_binding_prevents_apply(setup, monkeypatch):
  bound = None
  checked = asyncio.Event()
  applied = asyncio.Event()
  monkeypatch.setattr(app_setup.platform_update, "read_prepared_update", lambda: bound)
  async def run(action):
    nonlocal bound
    if action == "check":
      if not applied.is_set():
        bound = {"state": "swapped", "operation": {"id": "update"}}
        checked.set()
        return 1, ""
    else:
      applied.set()
    return 0, ""
  task = asyncio.create_task(setup.step("script", run))
  try:
    await checked.wait()
    await asyncio.sleep(0.02)
    assert not applied.is_set()
    bound = None
    await asyncio.wait_for(task, 2)
    assert applied.is_set()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", [False, True])
async def test_shutdown_reaps_group_without_waiting_for_pipe_eof(tmp_path, escape):
  import signal
  from app.process_groups import _has_exited
  child, leader = tmp_path / "child", tmp_path / "leader"
  child_code = f"import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open({str(child)!r},'w').write(str(os.getpid())); time.sleep(600)"
  code = f"import subprocess,sys,os,time; subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session={escape}); open({str(leader)!r},'w').write(str(os.getpid())); time.sleep(600)"
  task = asyncio.create_task(app_setup.command([sys.executable, "-c", code]))
  try:
    await until(lambda: child.exists() and leader.exists())
    task.cancel()
    asyncio.get_running_loop().call_later(0.01, task.cancel)  # shutdown during rerun
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert _has_exited(int(leader.read_text()))
    if not escape:
      await until(lambda: _has_exited(int(child.read_text())))
    else:
      assert not _has_exited(int(child.read_text()))
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if child.exists() and not _has_exited(int(child.read_text())):
      os.kill(int(child.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_complete_status_and_rerun_cancels_running_step(setup, tmp_path, monkeypatch):
  import httpx
  from app.routes.settings import rerun_dependency_setup
  from app.process_groups import _has_exited
  monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=SimpleNamespace(status_code=200)))
  monkeypatch.setattr(app_setup, "_runner", setup)
  root = tmp_path / "customizations"
  root.mkdir()
  (root / "mobius.json").write_text(json.dumps({"setup": {"steps": ["first.sh", "second.sh"]}}))
  (root / "first.sh").write_text('#!/bin/sh\ntest -f release && exit 0\necho $$ > pid\nsleep 600\n')
  (root / "second.sh").write_text('#!/bin/sh\nexit 0\n')
  setup.record("instance:second.sh", "failed", "previous diagnostic")
  setup.record("removed", "failed")
  task = asyncio.create_task(setup.serve())
  try:
    await until(lambda: (root / "pid").exists())
    state = app_setup.status()
    assert set(state) == {"instance:first.sh", "instance:second.sh"}
    assert state["instance:first.sh"]["running"] is True
    assert state["instance:second.sh"] == {"state": "pending", "output": "previous diagnostic"}
    pid = int((root / "pid").read_text())
    # Accepting a declaration only queues a pass: it never interrupts a
    # running step (an apt install must not be cut off midway).
    app_setup.request_run()
    await asyncio.sleep(0.3)
    assert not _has_exited(pid)
    (root / "release").touch()
    await rerun_dependency_setup(None)
    await until(lambda: app_setup.status()["instance:second.sh"]["state"] == "ready")
    assert _has_exited(pid)
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pointer_is_read_after_pin_and_python_waits_for_apply_rollback(setup, db, tmp_path, monkeypatch):
  from app import models, fs_locks
  row = models.App(name="Python", slug="python-restore", source_dir=str(tmp_path), runtime_revision="a" * 64)
  db.add(row)
  db.commit()
  root = tmp_path / "accepted"
  root.mkdir()
  (root / "mobius.json").write_text('{"python":{"lock":"requirements.lock"}}')
  events = []
  original_pin = app_setup.applied_app_runtime.hold_runtime_async
  async def pin(app_id):
    # A pointer changed after the ID snapshot but before the read pin.
    row.runtime_revision = "b" * 64
    db.commit()
    return await original_pin(app_id)
  def runtime(app):
    assert app.runtime_revision == "b" * 64
    return root
  async def command(*args, **kwargs):
    assert fs_locks.install_uninstall_lock().locked()
    events.append("restored")
    return 0, ""
  monkeypatch.setattr(app_setup.applied_app_runtime, "hold_runtime_async", pin)
  monkeypatch.setattr(app_setup.applied_app_runtime, "runtime_root", runtime)
  execute = AsyncMock(side_effect=command)
  monkeypatch.setattr(app_setup, "command", execute)
  original_step = setup.step
  async def step(key, run):
    async with fs_locks.install_uninstall_lock():
      task = asyncio.create_task(original_step(key, run))
      await asyncio.sleep(0.02)
      execute.assert_not_awaited()
      events.append("rollback")  # Apply unpublishes its provisional env
    await task
  monkeypatch.setattr(setup, "step", step)
  await setup.reconcile()
  assert events == ["rollback", "restored"]
  assert app_setup.status()[f"app:{row.id}:python"]["state"] == "ready"


@pytest.mark.asyncio
async def test_cancelled_cleanup_finishes_the_kill_and_keeps_the_cancel_message(monkeypatch):
  from tests.cancel_helpers import BlockedWorker

  worker = BlockedWorker()
  monkeypatch.setattr(app_setup, "terminate_process_group", worker)
  closed = []
  process = SimpleNamespace(
    pid=4321, _transport=SimpleNamespace(close=lambda: closed.append(True)),
  )
  task = asyncio.create_task(app_setup.cleanup(process))
  assert await asyncio.to_thread(worker.entered.wait, 5)
  for _ in range(2):
    task.cancel("server stopping")
    await asyncio.sleep(.02)
    assert not task.done() and not closed
  worker.release.set()
  with pytest.raises(asyncio.CancelledError, match="^server stopping$"):
    await task
  assert worker.exited.is_set() and closed == [True]
