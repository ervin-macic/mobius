"""Shared checks that cancelled callers drain their worker threads under a lock."""

import asyncio
import threading


class BlockedWorker:
  """A thread body that blocks until released, then returns ``then()``.

  With ``wraps``, it runs the real callable with the call's arguments instead.
  """

  def __init__(self, then=lambda: None, *, wraps=None):
    self.then = then
    self.wraps = wraps
    self.entered = threading.Event()
    self.release = threading.Event()
    self.exited = threading.Event()

  def __call__(self, *args, **kwargs):
    self.entered.set()
    try:
      assert self.release.wait(10)
      return self.wraps(*args, **kwargs) if self.wraps else self.then()
    finally:
      self.exited.set()


async def cancel_while_worker_blocked(
  lock, operation, worker, *, while_blocked=lambda: True, cancel=None,
):
  """Cancel ``operation`` twice while ``worker`` blocks; return its outcome.

  Cancelling an ``asyncio.to_thread`` await does not stop its thread, so a
  caller that unwinds at once releases ``lock`` while the thread still writes.
  ``while_blocked`` checks state the cancelled caller must not yet undo.
  """
  task = asyncio.create_task(operation())
  try:
    assert await asyncio.to_thread(worker.entered.wait, 5)
    for _ in range(2):
      (cancel or (lambda task: task.cancel()))(task)
      await asyncio.sleep(.02)
      assert not task.done() and not worker.exited.is_set()
      assert lock.locked(), "lock released while its worker still ran"
      assert while_blocked(), "cancelled caller undid state under its worker"
  finally:
    worker.release.set()
    try:  # Not gather: it drops a cancellation's message.
      outcome = await task
    except BaseException as exc:
      outcome = exc
  assert worker.exited.is_set() and not lock.locked()
  return outcome


def assert_cancellation_drains_worker_under_lock(
  lock, operation, monkeypatch, target, attr, *,
  while_blocked=lambda: True, then=lambda: None,
):
  """Patch ``target.attr`` with a blocked worker and check a cancel drains it.

  ``then`` is the worker's own outcome once released; the cancelled caller
  must still end in its ``CancelledError``.
  """
  worker = BlockedWorker(then)
  monkeypatch.setattr(target, attr, worker)
  outcome = asyncio.run(cancel_while_worker_blocked(
    lock, operation, worker, while_blocked=while_blocked,
  ))
  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
