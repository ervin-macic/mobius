"""Wake open long-lived streams only when their access may have changed.

Two kinds of stream outlive the request that authorized them: brokered MCP
exchanges and shared-browser event streams (chat, notifications, app events).
Their access depends on a few kinds of rows: a connection (enabled, healthy,
same generation), the owner's sign-in epoch, one browser grant and, for a
browser stream, one browser session, plus the owner's mobius.you account link
that an account grant is bound to. Polling those rows per open stream costs
database queries on the event loop, so instead every committed session write
that touches one of those tables advances one in-process revision. An open
stream waits on that revision and rechecks its access, in a worker thread,
only after it moves (see ``until_revoked``).

Inserts never revoke an existing stream, so only updates and deletes count;
an unrelated column change on a watched row merely costs one extra recheck.
Writes from another process (an operator script or direct SQL) cannot reach
this signal, so callers keep a slow safety recheck to bound that delay.
"""

from __future__ import annotations

import asyncio
import threading

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from collections.abc import AsyncIterator, Callable

from app import models
from app.browser_access import BrowserAccessGrant, BrowserAccessSession

# Every table an access check passed to ``until_revoked`` reads must be listed
# here, or a change to it would go unseen until the slow safety recheck.
ACCESS_TABLES = frozenset({
  models.Connector.__tablename__,
  models.Owner.__tablename__,
  BrowserAccessGrant.__tablename__,
  BrowserAccessSession.__tablename__,
  models.IdentityAccountLink.__tablename__,
})
_PENDING_KEY = "mobius_access_changed"

# Bound on how long a change written by another process can go unseen.
OUT_OF_PROCESS_RECHECK_SECONDS = 30.0

_lock = threading.Lock()
_revision = 0
_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []


def current_revision() -> int:
  """Revision to capture before validating access an exchange will rely on."""
  with _lock:
    return _revision


def notify_access_changed() -> None:
  """Advance the revision and wake every waiting exchange, from any thread."""
  global _revision
  with _lock:
    _revision += 1
    waiters = list(_waiters)
    _waiters.clear()
  for loop, future in waiters:
    try:
      loop.call_soon_threadsafe(_wake, future)
    except RuntimeError:
      pass  # That loop already closed; nothing on it is waiting any more.


def _wake(future: asyncio.Future) -> None:
  if not future.done():
    future.set_result(None)


async def wait_for_change(seen: int | None, timeout: float) -> int:
  """Return the revision once it differs from ``seen``, or after ``timeout``."""
  loop = asyncio.get_running_loop()
  future = loop.create_future()
  entry = (loop, future)
  with _lock:
    if _revision != seen:
      return _revision
    _waiters.append(entry)
  try:
    await asyncio.wait({future}, timeout=timeout)
  finally:
    with _lock:
      if entry in _waiters:
        _waiters.remove(entry)
  return current_revision()


class AccessRevoked(Exception):
  """The access behind an open stream is gone."""


async def until_revoked(
  iterator: AsyncIterator,
  is_active: Callable[[], bool],
  *,
  checked: int | None,
  recheck_seconds: float,
) -> AsyncIterator:
  """Forward ``iterator`` items, raising ``AccessRevoked`` once access ends.

  ``is_active`` is a blocking database check; it runs in a worker thread,
  and only after an access-relevant commit or once ``recheck_seconds`` pass
  without one, so neither an idle nor a busy stream costs the event loop a
  query per item. ``checked`` is the revision captured before the caller's
  own passing check, or None to check before the first item. A revocation
  closes the stream even while it is idle. An item is forwarded only while
  the last passing check covers the latest access change.
  """
  loop = asyncio.get_running_loop()
  # The safety deadline moves only after a passing check, so a busy stream
  # still rechecks out-of-process changes on schedule.
  recheck_at = loop.time() + recheck_seconds
  next_item = None
  try:
    while True:
      if next_item is None:
        next_item = asyncio.create_task(anext(iterator))
      change = asyncio.create_task(wait_for_change(
        checked, timeout=max(0.0, recheck_at - loop.time()),
      ))
      try:
        await asyncio.wait({next_item, change}, return_when=asyncio.FIRST_COMPLETED)
      finally:
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)
      revision = current_revision()
      if revision != checked or loop.time() >= recheck_at:
        if not await asyncio.to_thread(is_active):
          raise AccessRevoked
        checked = revision
        recheck_at = loop.time() + recheck_seconds
        continue
      if not next_item.done():
        continue  # The wait ended a hair before the deadline; wait again.
      try:
        item = next_item.result()
      except StopAsyncIteration:
        return
      next_item = None
      yield item
  finally:
    if next_item is not None and not next_item.done():
      next_item.cancel()
      await asyncio.gather(next_item, return_exceptions=True)
    await iterator.aclose()


def _touches_access(instance) -> bool:
  table = getattr(inspect(instance).mapper.local_table, "name", None)
  return table in ACCESS_TABLES


@event.listens_for(Session, "after_flush")
def _mark_flushed_access_change(session, _flush_context) -> None:
  changed = any(_touches_access(obj) for obj in session.deleted) or any(
    _touches_access(obj) and session.is_modified(obj, include_collections=False)
    for obj in session.dirty
  )
  if changed:
    session.info[_PENDING_KEY] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_statement_access_change(state) -> None:
  if not (state.is_update or state.is_delete):
    return
  table = getattr(state.statement, "table", None)
  if getattr(table, "name", None) in ACCESS_TABLES:
    state.session.info[_PENDING_KEY] = True


@event.listens_for(Session, "after_commit")
def _publish_committed_access_change(session) -> None:
  # SQLAlchemy also fires this when a savepoint is released; the change is not
  # visible to other sessions until the outermost transaction commits.
  if session.in_nested_transaction():
    return
  if session.info.pop(_PENDING_KEY, False):
    notify_access_changed()


@event.listens_for(Session, "after_transaction_end")
def _forget_uncommitted_access_change(session, transaction) -> None:
  # Only the outermost transaction ends the unit of work; a rolled-back
  # savepoint must not drop a change its committed parent still carries.
  if transaction.parent is None:
    session.info.pop(_PENDING_KEY, None)
