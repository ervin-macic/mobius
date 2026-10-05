"""The update's first boot: recovery, sweeps and resumed turns on chats the
previous release wrote last (so they are unconverted), and the disk tier the
background conversion stops at.

These began as reproductions in the update-path audit. Every chat a test
"unconverts" stands for one the previous release wrote just before the boot.
"""

import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import agent_admission, chat as chat_mod, chat_writer, models, resource_pressure, startup
from app import transcript_rows
from app.chat_writer import create_chat
from app.config import get_settings
from app.database import SessionLocal, engine
from tests.test_drain_for_restart import _live_turn, _run, _seed


def _unconvert(*chat_ids):
  with engine.begin() as conn:
    if chat_ids:
      for chat_id in chat_ids:
        conn.exec_driver_sql("DELETE FROM chat_transcript_state WHERE chat_id = ?", (chat_id,))
    else:
      conn.exec_driver_sql("DELETE FROM chat_transcript_state")


def _converted(chat_id):
  with engine.connect() as conn:
    return conn.exec_driver_sql(
      "SELECT 1 FROM chat_transcript_state WHERE chat_id = ?", (chat_id,),
    ).first() is not None


def _seed_helper(parent="up-parent", child="up-child"):
  with SessionLocal() as db:
    db.add(create_chat(id=parent, title="Parent", provider="codex",
                       messages=[{"role": "user", "content": "coordinate"}]))
    db.add(models.ChatRun(id=f"{parent}-run", root_run_id=f"{parent}-run",
                          chat_id=parent, status="running", provider="codex"))
    db.add(create_chat(id=child, title="Helper", provider="codex",
                       messages=[{"role": "user", "content": "task"},
                                 {"role": "assistant", "content": "done",
                                  "blocks": [{"type": "text", "content": "report"}]}]))
    db.add(models.Delegation(
      id=f"{child}-deleg", parent_chat_id=parent, parent_root_run_id=f"{parent}-run",
      task_key="t", child_chat_id=child, provider="codex", scope="write", cwd="/data",
      startup_prompt="task", prompt_sha256=hashlib.sha256(b"task").hexdigest(),
      notify_parent_on_complete=True))
    db.commit()


def _boot_context():
  return startup.StartupContext(
    app=SimpleNamespace(state=SimpleNamespace(reconciliation_failed=False)),
    settings=get_settings(), boot_id="transcript-update-boot",
    init_db=startup.DatabaseBootResult,
    install_pm_commit_launcher=lambda _s, _t: False,
    assert_provider_defaults=lambda _n: None,
    logger=logging.getLogger("test.transcript.update.boot"),
  )


def test_the_live_working_set_names_running_queued_goal_and_helper_chats():
  _seed("live-running")
  _seed_helper("live-parent", "live-child")
  with SessionLocal() as db:
    db.add(create_chat(id="live-queued", title="q", pending_messages=[{"role": "user", "content": "x"}]))
    db.add(create_chat(id="live-idle", title="i", messages=[{"role": "user", "content": "x"}]))
    db.commit()
  _unconvert()
  with SessionLocal() as db:
    assert transcript_rows.live_working_set(db) == [
      "live-child", "live-parent", "live-queued", "live-running",
    ]


@pytest.mark.asyncio
async def test_first_boot_recovers_a_running_turn_an_orphan_and_a_helper(caplog, monkeypatch):
  """A turn running across the update, an auto-resume orphan and an attached
  helper, all written last by the previous release, behind 200 earlier chats
  the background loop would reach first."""
  cid, nonce = "boot-running-turn", "boot-nonce"
  _seed(cid, messages=[
    {"role": "user", "content": "do work", "ts": 1},
    {"role": "assistant", "content": "partial", "ts": 2,
     "blocks": [{"type": "text", "content": "partial"}]},
  ])
  with SessionLocal() as db:
    db.get(models.ChatRun, f"rt-{cid}").restart_nonce = nonce
    db.add(create_chat(id="boot-orphan", title="o", messages=[{"role": "user", "content": "go", "ts": 1}]))
    db.add(models.ChatRun(id=f"{chat_mod._AUTO_RESUME_RUN_PREFIX}boot-orphan", chat_id="boot-orphan",
                          status="running", provider="claude",
                          started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)))
    db.commit()
  _seed_helper("boot-parent", "boot-child")
  for i in range(200):
    with SessionLocal() as db:
      db.add(create_chat(id=f"aaa-{i:04d}", title="x",
                         messages=[{"role": "user", "content": "x" * 2000, "ts": 1}]))
      db.commit()
  _unconvert()
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", ())
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", tuple(
    startup.StartupTask("read restart authorization",
                        lambda ctx: setattr(ctx, "restart_authorization", nonce))
    if task.name == "read restart authorization" else task
    for task in startup.DATABASE_STARTUP_TASKS
    if task.name != "verify app identity cutover"))
  context = _boot_context()
  with caplog.at_level(logging.WARNING):
    await startup.run_startup_plan(context)
    # The audit mode unconverts again before each later sweep, so these
    # direct (test-side) reads only hold in an ordinary run.
    if os.environ.get("MOBIUS_TEST_UNCONVERT_ON_REQUEST") != "1":
      for chat_id in (cid, "boot-orphan", "boot-parent", "boot-child"):
        assert _converted(chat_id)
      # A resumed coordinator reads its helper without tripping the loop guard.
      from app.delegations import active_parent_context
      with SessionLocal() as db:
        assert "boot-child-deleg" in active_parent_context(db, "boot-parent", "boot-parent-run")
    task = chat_writer._transcript_conversion_task
    if task is not None:
      await task
  assert "TranscriptNotConverted" not in caplog.text
  assert _run(cid)["status"] == "parked"


@pytest.mark.asyncio
async def test_one_unconverted_orphan_never_blocks_every_restart_resume(monkeypatch):
  from app.runner_registry import registry
  cid, nonce = "sweep-park-a", "sweep-nonce"
  _, sink, _handle = _live_turn(cid)
  await chat_mod.drain_all_for_restart(restart_nonce=nonce)
  assert _run(cid)["status"] == "parked"
  registry.reset_for_tests()
  chat_mod.unregister_active_sink(cid, sink)
  chat_mod._restart_draining_chats.clear()
  chat_mod.draining = False
  with SessionLocal() as db:
    db.add(create_chat(id="sweep-orphan-b", title="b", messages=[{"role": "user", "content": "go", "ts": 1}]))
    db.add(models.ChatRun(id=f"{chat_mod._AUTO_RESUME_RUN_PREFIX}orphan-b", chat_id="sweep-orphan-b",
                          status="running", provider="claude",
                          started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)))
    db.commit()
  _unconvert()
  scheduled = []

  async def record(chat_id, park_token=None, *, restart_authorization=None):
    scheduled.append(chat_id)
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", record)
  with SessionLocal() as db:
    await chat_mod.sweep_reset_parks(db, restart_authorization=nonce)
  assert cid in scheduled


@pytest.mark.asyncio
async def test_a_failing_orphan_candidate_is_isolated(monkeypatch):
  from app.runner_registry import registry
  cid, nonce = "sweep-park-c", "sweep-nonce-c"
  _, sink, _handle = _live_turn(cid)
  await chat_mod.drain_all_for_restart(restart_nonce=nonce)
  registry.reset_for_tests()
  chat_mod.unregister_active_sink(cid, sink)
  chat_mod._restart_draining_chats.clear()
  chat_mod.draining = False
  with SessionLocal() as db:
    db.add(create_chat(id="sweep-bad", title="b", messages=[]))
    db.add(models.ChatRun(id=f"{chat_mod._AUTO_RESUME_RUN_PREFIX}bad", chat_id="sweep-bad",
                          status="running", provider="claude",
                          started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)))
    db.commit()
  real = chat_mod._auto_resume_recovery

  def failing(db, chat, physical):
    if physical.chat_id == "sweep-bad":
      raise RuntimeError("one bad candidate")
    return real(db, chat, physical)

  monkeypatch.setattr(chat_mod, "_auto_resume_recovery", failing)
  scheduled = []

  async def record(chat_id, park_token=None, *, restart_authorization=None):
    scheduled.append(chat_id)
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", record)
  with SessionLocal() as db:
    await chat_mod.sweep_reset_parks(db, restart_authorization=nonce)
  assert cid in scheduled


@pytest.mark.asyncio
async def test_a_resumed_parent_turn_converts_its_helpers_before_reading_them():
  from app.delegations import active_parent_context, ensure_parent_helpers_converted
  _seed_helper()
  _unconvert("up-child")
  with SessionLocal() as db:
    with pytest.raises(transcript_rows.TranscriptNotConverted):
      active_parent_context(db, "up-parent", "up-parent-run")
  await ensure_parent_helpers_converted("up-parent")
  with SessionLocal() as db:
    assert "up-child-deleg" in active_parent_context(db, "up-parent", "up-parent-run")


@pytest.mark.asyncio
async def test_background_conversion_stops_before_turns_are_deferred(monkeypatch):
  """The update's own bulk work must never push the volume into the tier where
  agent admission defers every turn: it stops at "constrained"."""
  mib = 1024 * 1024
  total = 1024 * mib
  body = "x" * 4000
  for i in range(60):
    with SessionLocal() as db:
      db.add(create_chat(id=f"disk-{i:03d}", title="t",
                         messages=[{"role": "user", "content": body, "ts": n} for n in range(50)]))
      db.commit()
  with engine.begin() as conn:
    conn.exec_driver_sql("DELETE FROM chat_transcript_state")
    conn.exec_driver_sql("DELETE FROM chat_messages")
  with engine.connect() as conn:
    # Free pages would absorb the growth; measure growth of the file itself.
    conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.exec_driver_sql("VACUUM")
  with engine.begin() as conn:
    legacy = conn.exec_driver_sql("SELECT sum(length(messages)) FROM chats").scalar()
  path = engine.url.database

  def db_bytes():
    return sum(os.path.getsize(p) for p in (path, path + "-wal") if os.path.exists(p))

  base = db_bytes()
  start_free = int(total * 0.10) + int(legacy * 0.5)   # constrained tier is 10%

  def facts(_d=None, **_kw):
    free = start_free - (db_bytes() - base)
    disk = {"available": True, "path": "/data", "total_bytes": total,
            "used_bytes": total - free, "free_bytes": free}
    return {"facts": {"disk": disk},
            "pressure": {"disk": resource_pressure._disk_pressure(disk), "memory": {"state": "normal"}}}

  monkeypatch.setattr(resource_pressure, "resource_status", facts)
  await chat_writer.convert_remaining_transcripts()
  assert chat_writer.transcript_conversion_status["state"] == "blocked"
  assert agent_admission._deferral(facts()) is None
  assert facts()["pressure"]["disk"]["state"] != "critical"


def test_a_disk_blocked_conversion_is_rearmed_only_by_recovered_pressure(monkeypatch):
  started = []
  monkeypatch.setattr(chat_writer, "start_transcript_conversion", lambda: started.append(1))
  monkeypatch.setitem(chat_writer.transcript_conversion_status, "state", "blocked")
  monkeypatch.setattr(chat_writer, "_transcript_conversion_task", None)
  assert chat_writer.rearm_transcript_conversion("constrained") is False
  assert chat_writer.rearm_transcript_conversion("normal") is True
  monkeypatch.setitem(chat_writer.transcript_conversion_status, "state", "done")
  assert chat_writer.rearm_transcript_conversion("normal") is False
  assert started == [1]


def test_a_legacy_null_converts_to_an_empty_transcript():
  with SessionLocal() as db:
    db.add(create_chat(id="null-chat", title="n", messages=[]))
    db.commit()
  with engine.begin() as conn:
    conn.exec_driver_sql("UPDATE chats SET messages = 'null' WHERE id = 'null-chat'")
  chat_writer.wait_ack(chat_writer.get_writer().submit(chat_writer.ConvertTranscript(chat_id="null-chat")))
  with engine.connect() as conn:
    assert conn.exec_driver_sql(
      "SELECT messages FROM chats WHERE id = 'null-chat'").scalar() == "[]"
    assert conn.exec_driver_sql(
      "SELECT COUNT(*) FROM chat_transcript_damage WHERE chat_id = 'null-chat'").scalar() == 0
  assert _converted("null-chat")
