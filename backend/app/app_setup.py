"""Sequential, post-readiness restoration of accepted dependency declarations."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import sys

from app import app_python_env, applied_app_runtime, models, platform_update, fs_locks, settled
from app.config import get_settings
from app.database import SessionLocal
from app.manifest_contract import job_interpreter, validate_setup
from app.storage_io import atomic_write
from app.process_groups import terminate_process_group

log = logging.getLogger(__name__)
_runner = None


def request_run(*, cancel: bool = False) -> None:
  """Queue another pass. Only an explicit rerun (``cancel``) interrupts the
  running one: an accepted declaration must never cut off an apt install."""
  if _runner is not None:
    _runner.wake.set()
    if cancel and _runner.active is not None and not _runner.active.cancelling():
      _runner.active.cancel()


def status() -> dict:
  path = Path(get_settings().data_dir) / "setup-status.json"
  try:
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


async def command(argv, cwd=None, *, plan=False) -> tuple[int, str]:
  """Keep only an output tail; reap the whole process group on shutdown."""
  process = await asyncio.create_subprocess_exec(
    *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"},
  )
  tail = b""
  changes = False
  prefix = b""
  try:
    while chunk := await process.stdout.read(4096):
      tail = (tail + chunk)[-8192:]
      for part in chunk.splitlines(keepends=True):
        prefix = (prefix + part)[:5]
        changes |= prefix in (b"Inst ", b"Conf ")
        if part.endswith(b"\n"):
          prefix = b""
    code = await process.wait()
    return (-1 if plan and changes and code == 0 else code), app_python_env.redact(tail.decode(errors="replace"))
  finally:
    await cleanup(process)


async def cleanup(process):
  """Always kill the group; never wait for a descendant to close a pipe."""
  try:
    # Even repeated cancellation must finish the kill before unwinding.
    await settled.run_settled(
      terminate_process_group, process.pid, logger=log, label="setup",
    )
  finally:
    # asyncio.Process has no public close API. Close inherited pipes locally;
    # asyncio's child watcher reaps the leader independently of pipe EOF.
    process._transport.close()


async def apt(requirements, action) -> tuple[int, str]:
  # One solver transaction for ALL managed requirements. Never remove packages
  # or opt into forced downgrades/held-package changes.
  args = ["apt-get", "--no-remove", "satisfy", *requirements]
  simulation = ["sudo", "-n", *args[:1], "--simulate", *args[1:]]
  if action == "check":
    code, output = await command(simulation, plan=True)
    return (0 if code == 0 else 1), output
  # Fresh containers intentionally have no package lists. Refresh only when
  # not already satisfied, then prove the combined request is solvable before
  # installing anything. A repository/network failure is not a conflict.
  code, output = await command(["sudo", "-n", "apt-get", "update", "--error-on=any"])
  if code:
    return 3, output
  code, output = await command(simulation, plan=True)
  if code not in (0, -1):
    return 2, output
  return await command(["sudo", "-n", *args[:1], "--yes", *args[1:]])


class Runner:
  def __init__(self):
    self.wake = asyncio.Event()
    self.wake.set()
    self.active = None
    self.states = {}

  def save(self):
    path = Path(get_settings().data_dir) / "setup-status.json"
    atomic_write(path, json.dumps(self.states), mode=0o600)

  def record(self, key, state, output="", *, running=False):
    self.states[key] = {"state": state, "output": output}
    if running:
      self.states[key]["running"] = True
    self.save()

  async def step(self, key, run):
    try:
      await self.wait_settled()
      self.record(key, "pending", running=True)
      code, output = await run("check")
      if code == 1:
        await self.wait_settled()
        code, output = await run("apply")
        if code == 0:
          code, output = await run("check")
      self.record(key, "ready" if code == 0 else "conflict" if code == 2 else "failed", output)
    except Exception as exc:
      self.record(key, "failed", app_python_env.redact(str(exc)))
    finally:
      if self.states.get(key, {}).pop("running", False):
        self.save()

  async def reconcile(self):
    data = Path(get_settings().data_dir)
    with SessionLocal() as db:
      ids = [row.id for row in db.query(models.App.id).filter(models.App.deleted_at.is_(None)).order_by(models.App.id)]
    with ExitStack() as pins:
      declarations, errors = [], {}
      for app_id in ids:
        try:
          async with fs_locks.install_uninstall_lock():
            pins.enter_context(await applied_app_runtime.hold_runtime_async(app_id))
            # Read the pointer only AFTER acquiring the pin (GC cannot remove
            # this version) and lifecycle lock (Apply cannot roll it back).
            with SessionLocal() as db:
              app = db.get(models.App, app_id)
              if app is None or app.deleted_at or not app.runtime_revision:
                continue
              root = await settled.run_settled(applied_app_runtime.runtime_root, app)
            path = root / "mobius.json"
            manifest = json.loads(path.read_text()) if path.exists() else {}
            validate_setup(manifest)
            declarations.append((f"app:{app_id}", root, manifest, app_id))
        except Exception as exc:
          errors[f"app:{app_id}"] = {"state": "failed", "output": str(exc)}
      root = data / "customizations"
      path = root / "mobius.json"
      if path.exists():
        try:
          manifest = json.loads(path.read_text())
          validate_setup(manifest)
          declarations.append(("instance", root, manifest, None))
        except Exception as exc:
          errors["instance"] = {"state": "failed", "output": str(exc)}
      requirements = [r for _, _, m, _ in declarations for r in m.get("setup", {}).get("apt", [])]
      jobs = []
      if errors:
        errors["apt"] = {"state": "failed", "output": "Cannot solve all requirements: a declaration is unreadable"}
      elif requirements:
        jobs.append(("apt", lambda action: apt(requirements, action)))
      for owner, root, manifest, app_id in declarations:
        for script in manifest.get("setup", {}).get("steps", []):
          async def run(action, root=root, script=script):
            path = root / script
            if not path.resolve().is_relative_to(root.resolve()):
              raise ValueError("Setup script escapes its source")
            interpreter = job_interpreter(path.read_bytes())
            return await command([*interpreter, str(path), action], root)
          jobs.append((f"{owner}:{script}", run))
        if app_id is not None and manifest.get("python"):
          async def python(action, app_id=app_id, root=root):
            # The same boundary covers Apply publication AND rollback. Never
            # reuse an env that Apply could still unpublish on failure.
            async with fs_locks.install_uninstall_lock():
              # The lock can wait out a long Apply; an update may have
              # started meanwhile.
              await self.wait_settled()
              code, output = await command([
                sys.executable, "-m", "app.app_python_env", str(data), str(app_id), str(root),
              ])
              return (3 if code else 0), output
          jobs.append((f"{owner}:python", python))
      previous = {k: v.get("output", "") for k, v in status().items() if isinstance(v, dict)}
      self.states = {key: {"state": "pending", "output": previous.get(key, "")} for key, _ in jobs}
      self.states.update(errors)
      self.save()  # publish the COMPLETE pass before any step can hang
      for key, run in jobs:
        await self.step(key, run)

  async def wait_settled(self):
    # A timed-out observer is NOT a settlement decision. Keep waiting for
    # actual settlement/cancellation, and recheck before each mutation.
    while True:
      record = await asyncio.to_thread(platform_update.read_prepared_update)
      if not record or not (record["operation"] or record["state"] == "swapped"):
        return
      await asyncio.sleep(1)

  async def serve(self):
    import httpx
    # Lifespan completing does not mean the server is listening yet; a step
    # may call the local API. Probe actual readiness first.
    async with httpx.AsyncClient(trust_env=False) as client:
      while True:
        try:
          response = await client.get(f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/ready")
          if response.status_code == 200:
            break
        except httpx.HTTPError:
          pass
        await asyncio.sleep(1)
    while True:
      await self.wake.wait()
      await self.wait_settled()
      self.wake.clear()
      self.active = asyncio.create_task(self.reconcile())
      try:
        await self.active
      except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
          raise
      except Exception:
        log.exception("Dependency restoration failed")
      finally:
        self.active = None


def start() -> asyncio.Task:
  global _runner
  _runner = Runner()
  return asyncio.create_task(_runner.serve(), name="dependency-restoration")
