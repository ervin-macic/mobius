"""A saved owner-input card is the single source of its owner notification."""

import asyncio

import pytest

from app import models
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink
from app.chat_writer import StartTurn, get_writer
from app.database import SessionLocal


@pytest.fixture
def card_sink(chat, db):
  owner = db.query(models.Owner).first()
  db.add(models.PushSubscription(
    id="sub-card", owner_id=owner.id, endpoint="https://push.example/card",
    p256dh="p256", auth="auth",
  ))
  db.commit()
  run_id = f"card-{chat.id}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Prepare the change", "ts": 1},
  )).result(timeout=5)
  return ChatEventSink(create_broadcast(chat.id), chat.id, run_token=run_id)


@pytest.fixture
def pushes(monkeypatch):
  sent = []
  monkeypatch.setattr(
    "app.push.send_push", lambda subscription, payload: sent.append(payload) or True,
  )
  return sent


def _save(sink, question_id, text, **extra):
  async def go():
    await sink.publish_question({
      "type": "question",
      "question_id": question_id,
      "questions": [{"id": "q", "question": text, "options": []}],
      **extra,
    })
    await asyncio.gather(*list(sink._side_tasks))
  asyncio.run(go())


def _card_notifications(chat_id):
  with SessionLocal() as db:
    return db.query(models.Notification).filter(
      models.Notification.source_id == chat_id,
    ).all()


@pytest.mark.parametrize("extra", [{}, {"response_mode": "continuation"}])
def test_saving_a_card_sends_exactly_one_owner_notification(
  chat, card_sink, pushes, extra,
):
  """Native questions and continuation cards (question, approval, restart,
  secure input) share this save path, so each notifies once by itself."""
  _save(card_sink, "card-1", "Which color should the header use?", **extra)

  [row] = _card_notifications(chat.id)
  assert row.title == "Möbius needs your answer"
  assert row.body == "Which color should the header use?"
  assert row.source_type == "agent"
  assert row.target == f"/shell/?chat={chat.id}&focus=question"
  [payload] = pushes
  assert payload["id"] == row.id
  assert payload["tag"] == f"agent:{chat.id}:owner-input"


def test_resaving_the_same_card_does_not_notify_twice(chat, card_sink, pushes):
  _save(card_sink, "card-1", "Ship it?")
  _save(card_sink, "card-1", "Ship it?")

  assert len(_card_notifications(chat.id)) == 1
  assert len(pushes) == 1


def test_a_newer_card_replaces_the_older_push_on_the_device(chat, card_sink, pushes):
  _save(card_sink, "card-1", "First question?")
  _save(card_sink, "card-2", "Second question?")

  assert len(_card_notifications(chat.id)) == 2
  assert [p["tag"] for p in pushes] == [f"agent:{chat.id}:owner-input"] * 2


def test_long_prompts_are_shortened_for_the_notification_body(chat, card_sink, pushes):
  _save(card_sink, "card-1", "word " * 40)

  [row] = _card_notifications(chat.id)
  assert len(row.body) == 80
  assert row.body.endswith("…")


def test_a_card_that_fails_to_save_sends_no_notification(
  chat, card_sink, pushes, monkeypatch,
):
  from app import chat_event_sink

  async def fail(_ack):
    raise RuntimeError("commit dropped")

  monkeypatch.setattr(chat_event_sink, "_await_ack", fail)
  with pytest.raises(RuntimeError):
    _save(card_sink, "card-1", "Ship it?")

  assert _card_notifications(chat.id) == []
  assert pushes == []


def test_watching_owner_gets_the_history_row_but_no_push(
  chat, card_sink, pushes, monkeypatch,
):
  monkeypatch.setattr(
    "app.presence.has_watchers", lambda chat_id: chat_id == chat.id,
  )
  _save(card_sink, "card-1", "Ship it?")

  assert len(_card_notifications(chat.id)) == 1
  assert pushes == []
