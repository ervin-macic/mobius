"""Hot paths caught stalling the event loop keep their database work off it.

The stall recorder caught the event loop frozen for seconds inside these
paths' database reads while the disk was saturated. On the loop, one slow read
freezes every request and stream; in a worker thread it only slows its own.
"""

import asyncio

from app import delegations


def _running_on_event_loop() -> bool:
  try:
    asyncio.get_running_loop()
  except RuntimeError:
    return False
  return True


def test_parent_wake_reads_the_parent_off_the_event_loop(monkeypatch):
  seen = []

  def plan(parent_chat_id, source_work_id):
    seen.append(_running_on_event_loop())
    return None

  monkeypatch.setattr(delegations, "_plan_parent_wake", plan)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is False
  assert seen == [False]


def test_parent_wake_starts_the_planned_continuation_on_the_loop(monkeypatch):
  import app.chat as chat_mod
  import app.chat_start as chat_start

  plan = delegations._ParentWakePlan(
    root_run_id="root", run_token="token", source_work_id="source-run",
    activity_id="activity",
  )
  monkeypatch.setattr(delegations, "_plan_parent_wake", lambda *_a: plan)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  started = []

  async def start(**kwargs):
    started.append((_running_on_event_loop(), kwargs))
    return True

  monkeypatch.setattr(chat_start, "start_programmatic_activity_continuation", start)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is True
  assert started == [(True, {
    "chat_id": "parent-chat", "root_run_id": "root", "run_token": "token",
    "source_work_id": "source-run", "activity_id": "activity",
    "_transition_lock_held": True,
  })]


def test_parent_wake_skips_the_history_scan_without_a_running_run(
  chat, db, monkeypatch,
):
  # The pre-upgrade carrier it looks for always owns a running ChatRun in the
  # parent; without one, rereading the whole transcript every recovery pass
  # is pure cost that grows with the chat.
  from app import models

  read = []
  real_history = delegations.transcript_rows.history

  def history(parent):
    read.append(parent.id)
    return real_history(parent)

  monkeypatch.setattr(delegations.transcript_rows, "history", history)

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == []

  db.add(models.ChatRun(
    id="orphan-run", root_run_id="orphan-run", chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False,
  ))
  db.commit()

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == [chat.id]
