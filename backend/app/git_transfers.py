"""Git transfers into private staging, outside any app lock.

Clones and fetches run in their own small thread pool, each into a private
staging repository under ``apps/.git-transfers`` that boot sweeps. Only the
local import of a fetched commit takes the app's source lock, and concurrent
requests for the same commit share one transfer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import shutil
from pathlib import Path
from typing import Callable

from app import app_git, fs_locks, settled
from app.config import get_settings


def git_transfer_root() -> Path:
  """Private staging for Git transfers; swept at boot, never a live app."""
  return Path(get_settings().data_dir) / "apps" / ".git-transfers"


def discard_staged(staged: app_git.Staged | None) -> None:
  if staged is not None:
    shutil.rmtree(staged.path, ignore_errors=True)


# Transfers may each hold a thread for the full transfer ceiling, so they get
# their own small pool instead of the default one the rest of the server uses.
_GIT_TRANSFER_WORKERS = 4


_TRANSFER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
  max_workers=_GIT_TRANSFER_WORKERS, thread_name_prefix="git-transfer",
)


async def _stage(stage: Callable[..., app_git.Staged], *args) -> app_git.Staged:
  """Run a staging transfer; a cancelled caller still discards its result.

  The transfer thread cannot be interrupted, so cancellation hands its
  eventual staging directory to a callback instead of leaking it on disk.
  """
  loop = asyncio.get_running_loop()
  transfer = loop.run_in_executor(
    _TRANSFER_EXECUTOR, functools.partial(stage, *args),
  )
  try:
    return await asyncio.shield(transfer)
  except asyncio.CancelledError:
    transfer.add_done_callback(
      lambda done: not done.cancelled() and done.exception() is None
      and loop.run_in_executor(None, discard_staged, done.result())
    )
    raise


# One transfer per (repository, origin, ref) at a time; concurrent requests,
# such as parallel Store update checks, share it instead of repeating it.
_FETCHES: dict[tuple[str, str, str], asyncio.Future] = {}


def _forget_fetch(key: tuple[str, str, str], done: asyncio.Future) -> None:
  if _FETCHES.get(key) is done:
    del _FETCHES[key]
  if not done.cancelled():
    done.exception()  # Retrieved even when every waiting request has gone.


class OriginMismatch(ValueError):
  """The update source names another repository than the app's Git origin.

  The transfer would read a different repository into this app's object
  database, so it is refused before any network work.
  """


async def fetch_app_commit(
  source_dir: str | Path, ref: str, *, expected_origin: str | None = None,
) -> str:
  """Make ``ref``'s commit present, with full history, in an app repository.

  Callers hold no app lock: the transfer runs in a private staging repository
  and only the local import takes this app's source lock, after checking the
  repository and its origin are still the ones the transfer started from.
  """
  repo = Path(source_dir)
  url = await asyncio.to_thread(app_git.origin_url, repo)
  if expected_origin is not None and not app_git.same_origin(url, expected_origin):
    if url is not None:
      raise OriginMismatch("installed Git origin does not match update source")
    raise ValueError("installed Git origin does not match update source")
  present = await asyncio.to_thread(app_git.present_commit, repo, ref)
  if present is not None:
    return present
  if url is None:
    raise RuntimeError("source repository has no origin")
  key = (str(repo.resolve()), url, ref)
  shared = _FETCHES.get(key)
  if shared is None or shared.get_loop() is not asyncio.get_running_loop():
    shared = asyncio.ensure_future(_fetch_and_import(repo, url, ref))
    _FETCHES[key] = shared
    shared.add_done_callback(lambda done: _forget_fetch(key, done))
  # A request that goes away leaves the shared transfer running for the rest.
  return await asyncio.shield(shared)


async def _fetch_and_import(repo: Path, url: str, ref: str) -> str:
  staged = await _stage(app_git.stage_fetch, repo, url, ref, git_transfer_root())
  try:
    async with fs_locks.source_dir_lock(str(repo)):
      return await settled.run_settled(app_git.import_staged, repo, staged)
  finally:
    await settled.run_settled(discard_staged, staged)
