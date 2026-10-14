"""Await work to its end even when the awaiting caller is cancelled.

Cancelling an ``asyncio.to_thread`` await does not stop its thread, and
cancelling a coroutine mid-way can skip its rollback. A caller that holds a
lock, or that rolls back once the work fails, must therefore not unwind until
the work has settled. ``settle`` is the one place that rule lives:

* cancellation always wins: a cancelled caller re-raises its own original
  ``CancelledError`` (message included) once the work has ended, whatever the
  work's outcome was;
* the work's outcome then has no taker, so a failure is logged once
  (``log_abandoned_failure``, naming the work by ``what``), unless
  ``abandoned`` is given, in which case it receives the finished task instead.

``Teardown`` applies the same rule to a sequence of cleanup steps, such as a
turn's ``finally``: each step runs to its end, a cancellation landing in one
is held until every step has run, and a step's failure is logged.

Policy for thread work under a lock:

* Per call by default: each lock-held thread call goes through
  ``run_settled``. A cancel then lands at the next call boundary, so it never
  starts a new step, such as a public GitHub write, after the caller is gone.
* A whole section, ``async with lock: await settle(section())``, only where an
  in-process rollback must run once the section fails, as App Apply and the
  tombstone purge do. A settled section also finishes steps that had not
  started when the cancel came.
* A plain ``asyncio.to_thread`` (or Starlette's ``run_in_threadpool``, or
  ``anyio.to_thread.run_sync``) only for a read, marked on its line with
  ``# unsettled: read-only (<what it reads>)``. ``tests/test_settled.py``
  enforces this for direct calls inside lock blocks; it cannot see thread work
  reached through a called coroutine, and a marker is a reviewed claim.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import HTTPException

log = logging.getLogger("mobius.settled")

T = TypeVar("T")


async def settle(
  work: Awaitable[T],
  *,
  what: str = "Work",
  abandoned: Callable[[asyncio.Future[T]], Any] | None = None,
) -> T:
  """Await ``work`` to its end; a cancelled caller re-raises only once it settles."""
  task = asyncio.ensure_future(work)
  cancel: asyncio.CancelledError | None = None
  while not task.done():
    try:
      await asyncio.wait({task})
    except asyncio.CancelledError as exc:
      cancel = cancel or exc
  if cancel is None:
    return task.result()
  if not task.cancelled():
    if abandoned is None:
      log_abandoned_failure(task, what)
    else:
      try:
        abandoned(task)
      except Exception:  # The cancellation still wins.
        log.exception("Cleaning up %s after its caller was cancelled failed", what)
  raise cancel


async def run_settled(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
  """Run ``fn`` in a worker thread that finishes before its caller unwinds."""
  return await settle(
    asyncio.to_thread(fn, *args, **kwargs),
    what=getattr(fn, "__qualname__", None) or repr(fn),
  )


def log_abandoned_failure(task: asyncio.Future, what: str = "Work") -> None:
  """Log the failure of work whose caller was cancelled, if it failed.

  An ``HTTPException`` is an expected refusal that the work already handled,
  so it goes to info; anything else is an error with its traceback.
  """
  error = task.exception()
  if isinstance(error, HTTPException):
    log.info("%s refused after its caller was cancelled: %s", what, error.detail)
  elif error is not None:
    log.error("%s failed after its caller was cancelled", what, exc_info=error)


class Teardown:
  """Cleanup steps that each run to their end, whatever cancels the caller.

  Create it at the top of a ``finally``. A cancellation that lands in a step
  is held, so it cannot skip the steps after it; ``finish`` raises it once
  they have all run, unless the block is already unwinding a cancellation,
  which then propagates as the original.
  """

  def __init__(self, logger: logging.Logger, context: str) -> None:
    self._log = logger
    self._context = context
    self._unwinding = _unwinding(sys._getframe(1))
    self.cancel: asyncio.CancelledError | None = None

  def defer(self, cancel: asyncio.CancelledError) -> None:
    """Hold a cancellation caught by the caller's own cleanup code."""
    self.cancel = self.cancel or cancel

  async def step(
    self, work: Awaitable[Any], what: str, *, level: int = logging.ERROR,
  ) -> bool:
    """Run ``work`` to its end; return whether it succeeded, logging if not."""
    succeeded = False

    async def logged() -> None:
      nonlocal succeeded
      try:
        await work
        succeeded = True
      except Exception:
        self._log.log(
          level, "%s failed during teardown %s", what, self._context,
          exc_info=True,
        )

    try:
      await settle(logged())
    except asyncio.CancelledError as exc:
      self.defer(exc)
    return succeeded

  def finish(self) -> None:
    """Raise a held cancellation, unless the original one is propagating."""
    if self.cancel is not None and not isinstance(
      self._unwinding, asyncio.CancelledError,
    ):
      raise self.cancel


def _unwinding(frame) -> BaseException | None:
  """The exception unwinding through ``frame``, if any.

  ``sys.exc_info`` also reports an exception a caller further up the stack is
  only handling. One unwinding this frame has already passed through it, so
  its traceback holds the frame.
  """
  error = sys.exc_info()[1]
  trace = error.__traceback__ if error is not None else None
  while trace is not None:
    if trace.tb_frame is frame:
      return error
    trace = trace.tb_next
  return None
