"""Cross-process ownership boundary for Codex rollout storage.

Every Codex turn, usage probe, and session fork holds a shared advisory lock
while it runs. Retention takes the exclusive side non-blockingly, so it can
never unlink an old rollout while one of them may be resuming or writing it. A Codex
process idle between turns holds no lock; ``codex_home_in_use`` covers it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from app import settled


# Codex SDK waits can occupy every default asyncio worker. Lock acquisition is
# part of starting/closing that SDK, so it needs a tiny independent executor or
# it can deadlock behind the work it is meant to protect.
_LOCK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
  max_workers=8,
  thread_name_prefix="mobius-codex-session-lock",
)


@dataclass
class CodexSessionLock:
  fd: int

  def release(self) -> None:
    if self.fd < 0:
      return
    fd, self.fd = self.fd, -1
    try:
      fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
      os.close(fd)


def _open_lock(data_dir: str | Path) -> int:
  home = Path(data_dir) / "cli-auth" / "codex"
  home.mkdir(parents=True, exist_ok=True)
  # Lock the existing directory inode itself. Startup retention must remain
  # able to unlink stale rollouts at block/inode ENOSPC; creating a new lock
  # file at that moment would defeat the recovery path it protects.
  return os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def acquire_codex_session_activity(data_dir: str | Path) -> CodexSessionLock:
  """Acquire the shared launcher side, waiting out a short active sweep."""
  fd = _open_lock(data_dir)
  try:
    fcntl.flock(fd, fcntl.LOCK_SH)
  except BaseException:
    os.close(fd)
    raise
  return CodexSessionLock(fd)


async def acquire_codex_session_activity_async(
  data_dir: str | Path,
) -> CodexSessionLock:
  """Acquire without blocking the event loop or leaking on cancellation."""
  return await settled.settle(
    asyncio.get_running_loop().run_in_executor(
      _LOCK_EXECUTOR, acquire_codex_session_activity, data_dir,
    ),
    abandoned=_release_abandoned,
  )


def _release_abandoned(acquired: asyncio.Future[CodexSessionLock]) -> None:
  """A cancelled caller never takes the lock it was acquiring: release it."""
  if acquired.exception() is None:
    acquired.result().release()
  else:
    settled.log_abandoned_failure(acquired, "Codex session lock acquire")


def codex_home_in_use(data_dir: str | Path, *, proc_root: Path = Path("/proc")) -> bool:
  """Whether any process holds a file inside CODEX_HOME open.

  Möbius launchers hold the shared lock, but a Codex CLI started from an
  agent's shell does not. Every Codex process keeps its rollout or SQLite
  stores open while it works, so an open descriptor beneath CODEX_HOME is the
  evidence that covers those too. The sweep's own lock descriptor names the
  directory itself, not a path inside it.
  """
  home = os.path.join(os.path.realpath(Path(data_dir) / "cli-auth" / "codex"), "")
  try:
    pids = [name for name in os.listdir(proc_root) if name.isdigit()]
  except OSError:
    return False
  for pid in pids:
    fd_dir = proc_root / pid / "fd"
    try:
      descriptors = os.listdir(fd_dir)
    except OSError:
      continue
    for descriptor in descriptors:
      try:
        if os.readlink(fd_dir / descriptor).startswith(home):
          return True
      except OSError:
        continue
  return False


def try_acquire_codex_session_sweep(
  data_dir: str | Path,
) -> CodexSessionLock | None:
  """Acquire exclusive retention ownership, or return when any launcher runs."""
  fd = _open_lock(data_dir)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    os.close(fd)
    return None
  except BaseException:
    os.close(fd)
    raise
  return CodexSessionLock(fd)
