"""Release-1 dual format: rows are authoritative, ``chats.messages`` mirrors them.

These tests pin the contracts that make the previous release safe to roll
back to at any committed state: the legacy mirror is byte-identical to what
that release would write, its writes are detected without scanning, chats it
wrote are converted on demand or in the background, and damaged values keep
their bytes. Raw SQL stands in for the previous release, which writes only
``chats`` through its ORM.
"""

import asyncio
import json
import math
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import chat_search, chat_writer, models, transcript_rows as rows
from app.chat_writer import create_chat
from app.database import SessionLocal, engine


TYPED = [
  {"role": "user", "content": "héllo ☃", "ts": 1, "cid": "c-1"},
  {"role": "assistant", "id": "a-1", "content": "floats", "ts": 2,
   "blocks": [{"type": "text", "content": "x"}], "values": [1.0, -0.0, 1e308, 0.1]},
  {"role": "user", "content": "big", "ts": 3, "n": 2**63 + 1, "lone": "\ud800"},
  "a bare string item",
  7,
  None,
  {"role": "assistant", "hidden": True, "content": "hidden reminder", "ts": 4},
]


def canonical(value):
  return json.dumps(value, sort_keys=True)


def legacy_text(chat_id):
  with engine.connect() as conn:
    return conn.execute(text("SELECT messages FROM chats WHERE id = :id"), {"id": chat_id}).scalar()


def converted(chat_id):
  with engine.connect() as conn:
    return conn.execute(text(
      "SELECT 1 FROM chat_transcript_state WHERE chat_id = :id"), {"id": chat_id},
    ).first() is not None


def stored_rows(chat_id):
  with engine.connect() as conn:
    return [json.loads(body) for (body,) in conn.execute(text(
      "SELECT body FROM chat_messages WHERE chat_id = :id ORDER BY seq"), {"id": chat_id})]


def seed(chat_id="dual", messages=None, title="Dual chat"):
  with SessionLocal() as db:
    db.add(create_chat(id=chat_id, title=title, messages=messages or []))
    db.commit()
  return chat_id


def previous_release_writes(chat_id, messages):
  """The previous release's ORM update: it never names the marker table."""
  with engine.begin() as conn:
    conn.execute(text("UPDATE chats SET messages = :m, has_messages = :h WHERE id = :id"),
                 {"m": json.dumps(messages), "h": bool(messages), "id": chat_id})


def previous_release_inserts(source_id, chat_id, messages):
  """A previous-release INSERT: every chats column, no marker row."""
  with engine.begin() as conn:
    columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(chats)")]
    others = [c for c in columns if c not in ("id", "messages")]
    conn.exec_driver_sql(
      f"INSERT INTO chats (id, messages, {', '.join(others)}) "
      f"SELECT ?, ?, {', '.join(others)} FROM chats WHERE id = ?",
      (chat_id, json.dumps(messages), source_id),
    )


def convert_through_writer(chat_id):
  chat_writer.wait_ack(chat_writer.get_writer().submit(
    chat_writer.ConvertTranscript(chat_id=chat_id)))


# -- One write path ----------------------------------------------------------

def test_mirror_is_byte_identical_to_the_previous_release_serializer():
  chat_id = seed(messages=TYPED[:2])
  with SessionLocal() as db:
    rows.append_many(db, chat_id, TYPED[2:])
    db.commit()
  assert legacy_text(chat_id) == json.dumps(TYPED)
  decoded = json.loads(legacy_text(chat_id))
  assert canonical(decoded) == canonical(TYPED)
  assert math.copysign(1, decoded[1]["values"][1]) == -1  # -0.0 survives
  assert decoded[2]["n"] == 2**63 + 1


def test_every_row_change_moves_updated_at_and_has_messages():
  chat_id = seed(messages=[{"role": "user", "content": "a"}])
  with SessionLocal() as db:
    before = db.get(models.Chat, chat_id).updated_at
    rows.update_at(db, chat_id, 0, {"role": "user", "content": "edited"})
    db.commit()
    chat = db.get(models.Chat, chat_id)
    assert chat.updated_at > before
    rows.replace_all(db, chat_id, [])
    db.commit()
    db.expire_all()
    assert db.get(models.Chat, chat_id).has_messages is False
  assert legacy_text(chat_id) == "[]"


def test_a_rolled_back_change_leaves_rows_and_mirror_untouched():
  chat_id = seed(messages=[{"role": "user", "content": "kept"}])
  with SessionLocal() as db:
    rows.append(db, chat_id, {"role": "assistant", "content": "discarded"})
    db.rollback()
    db.commit()
  assert stored_rows(chat_id) == [{"role": "user", "content": "kept"}]
  assert json.loads(legacy_text(chat_id)) == stored_rows(chat_id)


def test_replace_all_rewrites_only_changed_positions():
  chat_id = seed(messages=[{"role": "user", "content": str(i)} for i in range(4)])
  statements = []
  from sqlalchemy import event
  listener = lambda *a: statements.append(a[2])
  event.listen(engine, "before_cursor_execute", listener)
  try:
    with SessionLocal() as db:
      rows.replace_all(db, chat_id, [
        {"role": "user", "content": "0"}, {"role": "user", "content": "changed"},
      ])
      db.commit()
  finally:
    event.remove(engine, "before_cursor_execute", listener)
  assert sum(s.startswith("UPDATE chat_messages") for s in statements) == 1
  assert sum(s.startswith("DELETE FROM chat_messages") for s in statements) == 1
  assert [m["content"] for m in stored_rows(chat_id)] == ["0", "changed"]


# -- Detecting the previous release's writes ---------------------------------

def test_this_release_never_marks_its_own_writes_for_reconversion():
  chat_id = seed(messages=[{"role": "user", "content": "a"}])
  with SessionLocal() as db:
    rows.append(db, chat_id, {"role": "assistant", "content": "b"})
    db.commit()
  assert converted(chat_id)


def test_previous_release_update_insert_and_delete_are_detected_exactly():
  kept = seed("kept", [{"role": "user", "content": "untouched"}])
  changed = seed("changed", [{"role": "user", "content": "old"}])
  deleted = seed("deleted", [{"role": "user", "content": "secret prose"}])
  with SessionLocal() as db:
    damaged = models.ChatTranscriptDamage(chat_id=deleted, raw=b"x", error="e")
    db.add(damaged)
    db.commit()
  previous_release_writes(changed, [{"role": "user", "content": "old"},
                                    {"role": "assistant", "content": "from previous"}])
  previous_release_inserts(kept, "inserted", [{"role": "user", "content": "new chat"}])
  with engine.begin() as conn:
    conn.execute(text("DELETE FROM chats WHERE id = :id"), {"id": deleted})
  with engine.connect() as conn:
    unconverted = {row[0] for row in conn.execute(text(
      "SELECT id FROM chats c WHERE NOT EXISTS "
      "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id)"))}
    leftovers = [conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE chat_id = :id"),
                              {"id": deleted}).scalar()
                 for table in ("chat_messages", "chat_search_entries",
                               "chat_transcript_damage", "chat_transcript_state")]
  assert unconverted == {changed, "inserted"}
  assert leftovers == [0, 0, 0, 0]
  for chat_id in (changed, "inserted"):
    convert_through_writer(chat_id)
    assert converted(chat_id)
    assert stored_rows(chat_id) == json.loads(legacy_text(chat_id))
  assert stored_rows(changed)[-1]["content"] == "from previous"


def test_purge_removes_every_transcript_derived_row_without_foreign_keys():
  from datetime import timedelta
  from app.chat_retention import purge_expired_chat_tombstones
  from app.timeutil import now_naive_utc
  chat_id = seed(messages=[{"role": "user", "content": "purgeable words"}])
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    chat.deleted_at = now_naive_utc() - timedelta(days=30)
    db.commit()
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 0
    purge_expired_chat_tombstones(db)
  with engine.connect() as conn:
    assert conn.execute(text("SELECT COUNT(*) FROM chat_messages")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM chat_search_entries")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM chat_transcript_state")).scalar() == 0


# -- Conversion ---------------------------------------------------------------

def test_conversion_keeps_legacy_bytes_and_types_exactly():
  chat_id = seed(messages=[])
  odd_bytes = '[{"role":"user","content":"spaced",  "ts":1.0}, 5]'
  previous_release_writes(chat_id, [])
  with engine.begin() as conn:
    conn.execute(text("UPDATE chats SET messages = :m WHERE id = :id"), {"m": odd_bytes, "id": chat_id})
  convert_through_writer(chat_id)
  assert legacy_text(chat_id) == odd_bytes
  assert canonical(stored_rows(chat_id)) == canonical(json.loads(odd_bytes))
  assert isinstance(stored_rows(chat_id)[0]["ts"], float)


@pytest.mark.parametrize("raw", [b"{not json", b'{"a": 1}', b"\xff\xfe broken"])
def test_damaged_legacy_bytes_are_preserved_before_the_placeholder_replaces_them(raw):
  chat_id = seed(messages=[{"role": "user", "content": "x"}])
  with engine.begin() as conn:
    conn.execute(text("UPDATE chats SET messages = :m WHERE id = :id"), {"m": raw, "id": chat_id})
  convert_through_writer(chat_id)
  with engine.connect() as conn:
    saved = conn.execute(text(
      "SELECT raw FROM chat_transcript_damage WHERE chat_id = :id"), {"id": chat_id}).scalar()
  assert bytes(saved) == raw
  assert stored_rows(chat_id) == rows.damaged_messages()
  assert json.loads(legacy_text(chat_id)) == rows.damaged_messages()
  assert converted(chat_id)


def test_worker_thread_reader_converts_through_the_writer():
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "written by previous"}])
  result = {}

  def read():
    with SessionLocal() as db:
      chat = db.get(models.Chat, chat_id)
      result["messages"] = list(rows.history(chat))

  worker = threading.Thread(target=read)
  worker.start()
  worker.join()
  assert result["messages"] == [{"role": "user", "content": "written by previous"}]
  assert converted(chat_id)


def test_event_loop_never_waits_for_a_conversion():
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "previous"}])

  async def on_loop():
    with SessionLocal() as db:
      chat = db.get(models.Chat, chat_id)
      with pytest.raises(rows.TranscriptNotConverted):
        rows.history(chat)
      await rows.ensure_converted_async(chat_id)
      return list(rows.history(chat))

  assert asyncio.run(on_loop()) == [{"role": "user", "content": "previous"}]


def test_reader_inside_its_own_transaction_refuses_to_wait():
  chat_id = seed(messages=[])
  other = seed("other", messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "previous"}])
  with SessionLocal() as db:
    db.get(models.Chat, other).title = "holds a write lock"
    db.flush()
    with pytest.raises(RuntimeError, match="before this session's transaction"):
      rows.count(db, chat_id)
    db.rollback()


def test_writer_commands_convert_inline_before_mutating():
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "previous"}])
  chat_writer.wait_ack(chat_writer.get_writer().submit(chat_writer.ReplaceTranscript(
    chat_id=chat_id, messages=[{"role": "user", "content": "previous"},
                               {"role": "assistant", "content": "reply"}])))
  assert converted(chat_id)
  assert [m["content"] for m in stored_rows(chat_id)] == ["previous", "reply"]
  assert json.loads(legacy_text(chat_id)) == stored_rows(chat_id)


def test_background_conversion_resumes_from_durable_state_and_finishes():
  ids = [seed(f"bg-{i}", messages=[]) for i in range(5)]
  for chat_id in ids:
    previous_release_writes(chat_id, [{"role": "user", "content": f"from {chat_id}"}])
  convert_through_writer(ids[2])  # A reader got there first.
  asyncio.run(chat_writer.convert_remaining_transcripts())
  assert chat_writer.transcript_conversion_status["state"] == "done"
  assert all(converted(chat_id) for chat_id in ids)
  with SessionLocal() as db:
    assert rows.unconverted_count(db) == 0


def test_background_conversion_pauses_at_the_critical_disk_floor(monkeypatch):
  import app.resource_pressure as pressure
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "x"}])
  monkeypatch.setattr(pressure, "resource_status",
                      lambda _dir: {"pressure": {"disk": {"state": "critical"}}})
  asyncio.run(chat_writer.convert_remaining_transcripts())
  assert chat_writer.transcript_conversion_status["state"] == "blocked"
  assert not converted(chat_id)


@pytest.mark.parametrize("fault", ["after_rows_deleted", "after_rows_inserted"])
def test_a_crash_inside_conversion_leaves_the_legacy_value_authoritative(monkeypatch, fault):
  chat_id = seed(messages=[{"role": "user", "content": "row"}])
  previous_release_writes(chat_id, [{"role": "user", "content": "legacy"}])
  real_insert = rows._insert

  def failing_insert(db, cid, start, messages):
    if fault == "after_rows_inserted":
      real_insert(db, cid, start, messages)
    raise RuntimeError("simulated crash")

  monkeypatch.setattr(rows, "_insert", failing_insert)
  with pytest.raises(RuntimeError):
    convert_through_writer(chat_id)
  assert not converted(chat_id)
  assert json.loads(legacy_text(chat_id)) == [{"role": "user", "content": "legacy"}]
  monkeypatch.setattr(rows, "_insert", real_insert)
  convert_through_writer(chat_id)
  assert stored_rows(chat_id) == [{"role": "user", "content": "legacy"}]


def test_a_crash_inside_the_mirror_hook_commits_nothing(monkeypatch):
  chat_id = seed(messages=[{"role": "user", "content": "before"}])
  original = rows._MARK_CONVERTED
  monkeypatch.setattr(rows, "_MARK_CONVERTED", text("SELECT no_such_function()"))
  with SessionLocal() as db:
    rows.append(db, chat_id, {"role": "assistant", "content": "lost"})
    with pytest.raises(Exception):
      db.commit()
    db.rollback()
  monkeypatch.setattr(rows, "_MARK_CONVERTED", original)
  assert stored_rows(chat_id) == [{"role": "user", "content": "before"}]
  assert json.loads(legacy_text(chat_id)) == stored_rows(chat_id)
  assert converted(chat_id)


# -- Search ---------------------------------------------------------------------

def test_search_entries_follow_every_row_change_and_title(db):
  chat_id = seed(messages=[{"role": "user", "content": "alpha words"}], title="Gamma title")
  assert [hit["id"] for hit in chat_search.search(db, "alpha")] == [chat_id]
  rows.update_at(db, chat_id, 0, {"role": "user", "content": "beta words"})
  db.commit()
  assert chat_search.search(db, "alpha") == []
  assert [hit["id"] for hit in chat_search.search(db, "beta")] == [chat_id]
  rows.replace_all(db, chat_id, [])
  db.commit()
  assert chat_search.search(db, "beta") == []
  db.get(models.Chat, chat_id).title = "Delta title"
  db.commit()
  assert chat_search.search(db, "gamma") == []
  assert [hit["id"] for hit in chat_search.search(db, "delta")] == [chat_id]


def test_search_never_drops_results_after_many_chats_change(db):
  ids = [seed(f"many-{i:02d}", messages=[{"role": "user", "content": "needle"}]) for i in range(25)]
  assert {hit["id"] for hit in chat_search.search(db, "needle", limit=50)} == set(ids)
  for chat_id in ids:
    rows.append(db, chat_id, {"role": "assistant", "content": "haystack"})
  db.commit()
  assert {hit["id"] for hit in chat_search.search(db, "haystack", limit=50)} == set(ids)


def test_search_reaches_previous_release_prose_after_conversion(db):
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "converted prose"}])
  assert chat_search.search(db, "converted") == []
  convert_through_writer(chat_id)
  assert [hit["id"] for hit in chat_search.search(db, "converted")] == [chat_id]


# -- Reads --------------------------------------------------------------------

def test_a_view_raises_for_a_position_that_has_since_vanished():
  chat_id = seed(messages=[{"role": "user", "content": str(i)} for i in range(3)])
  with SessionLocal() as reader:
    view = rows.history(reader.get(models.Chat, chat_id))
    assert len(view) == 3
    with SessionLocal() as writer:
      rows.replace_all(writer, chat_id, [{"role": "user", "content": "0"}])
      writer.commit()
    with pytest.raises(IndexError):
      view[2]
    assert [m["content"] for m in view] == ["0"]


def test_cid_lookup_and_timestamp_helpers_never_decode_history(monkeypatch):
  chat_id = seed(messages=[{"role": "user", "content": "x" * 1000, "ts": i, "cid": f"c{i}"}
                           for i in range(50)])
  decoded = []
  real = models.TranscriptJSONText.process_result_value
  monkeypatch.setattr(models.TranscriptJSONText, "process_result_value",
                      lambda self, value, dialect: decoded.append(1) or real(self, value, dialect))
  with SessionLocal() as db:
    assert rows.client_message_seq(db, chat_id, "c7") == 7
    assert rows.max_timestamp(db, chat_id) == 49
  assert decoded == []  # Lookups read projections and raw ts text only.


def test_cid_less_legacy_user_rows_match_their_derived_identity():
  chat_id = seed(messages=[{"role": "user", "content": "old", "ts": 5}])
  with SessionLocal() as db:
    assert rows.client_message_seq(db, chat_id, "legacy-5") == 0
    assert "cid" not in rows.metadata(db, chat_id)[0]


# -- The next release's database ---------------------------------------------

def test_this_release_serves_a_database_without_the_legacy_column(tmp_path):
  """Release 2 drops chats.messages; rolling back to this release stays safe."""
  from app.database import Base
  from app.schema_migrations import _add_transcript_rows, _create_chat_search_tables, mapped_schema_gaps
  other = create_engine(f"sqlite:///{tmp_path / 'release2.db'}")
  Base.metadata.create_all(other)
  with other.begin() as conn:
    conn.exec_driver_sql("ALTER TABLE chats DROP COLUMN messages")
  _create_chat_search_tables(other)
  _add_transcript_rows(other)
  assert mapped_schema_gaps(other) == []
  Session = sessionmaker(bind=other)
  with Session() as db:
    db.add(create_chat(id="r2", title="Release two", messages=[{"role": "user", "content": "hi"}]))
    db.commit()
    rows.append(db, "r2", {"role": "assistant", "content": "still works"})
    db.commit()
    assert [m["content"] for m in rows.read_all(db, "r2")] == ["hi", "still works"]
    assert db.get(models.Chat, "r2").has_messages is True
    assert rows.unconverted_count(db) == 0
  other.dispose()


def test_the_search_trigger_matches_the_prose_flag():
  import inspect
  from app import schema_migrations
  assert "prose_flag = 32" in inspect.getsource(schema_migrations._add_transcript_rows)
  assert rows.PROSE == 32


def test_the_migration_creates_the_same_transcript_tables_as_the_models(tmp_path):
  from app.database import Base
  from app.schema_migrations import _add_transcript_rows
  import re
  def schema(eng):
    with eng.connect() as conn:
      return sorted(re.sub(r"\s*([(),])\s*", r"\1", " ".join(sql.split())) for (sql,) in conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type IN ('table', 'index') AND tbl_name IN "
        "('chat_messages', 'chat_transcript_state', 'chat_transcript_damage') AND sql IS NOT NULL"))
  by_models = create_engine(f"sqlite:///{tmp_path / 'models.db'}")
  Base.metadata.create_all(by_models)
  by_migration = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
  with by_migration.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE chats (id VARCHAR(64) PRIMARY KEY, title TEXT, messages JSON NOT NULL)")
  _add_transcript_rows(by_migration)
  strip = lambda statements: [s.replace("IF NOT EXISTS ", "") for s in statements]
  assert strip(schema(by_migration)) == strip(schema(by_models))


@pytest.mark.asyncio
async def test_boot_on_a_previous_release_database_converts_in_the_background(caplog, monkeypatch):
  """The real database startup plan serves at once; no task trips on, or
  waits for, chats the previous release wrote; conversion then completes."""
  import logging
  from types import SimpleNamespace
  from app import startup
  from app.config import get_settings
  ids = [seed(f"boot-{i}", messages=[]) for i in range(3)]
  for chat_id in ids:
    previous_release_writes(chat_id, [{"role": "user", "content": f"boot {chat_id}"}])
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", ())
  # The identity cutover proof is filesystem state this fixture does not own.
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", tuple(
    task for task in startup.DATABASE_STARTUP_TASKS if task.name != "verify app identity cutover"))
  context = startup.StartupContext(
    app=SimpleNamespace(state=SimpleNamespace(reconciliation_failed=False)),
    settings=get_settings(), boot_id="transcript-boot",
    init_db=startup.DatabaseBootResult,
    install_pm_commit_launcher=lambda _s, _t: False,
    assert_provider_defaults=lambda _n: None,
    logger=logging.getLogger("test.transcript.boot"),
  )
  with caplog.at_level(logging.WARNING):
    result = await startup.run_startup_plan(context)
    assert result.serviceable
    task = chat_writer._transcript_conversion_task
    assert task is not None
    await task
  assert "TranscriptNotConverted" not in caplog.text
  assert "start transcript conversion" not in context.failed_tasks
  assert all(converted(chat_id) for chat_id in ids)
  assert stored_rows(ids[0]) == [{"role": "user", "content": f"boot {ids[0]}"}]


# -- Review round: transaction lifecycle, failure isolation, disk, triggers --

def test_a_rolled_back_savepoint_never_drops_an_earlier_mirror():
  chat_id = seed(messages=[{"role": "user", "content": "first"}])
  with SessionLocal() as db:
    rows.append(db, chat_id, {"role": "assistant", "content": "kept"})
    nested = db.begin_nested()
    db.get(models.Chat, chat_id).title = "rolled back"
    nested.rollback()
    db.commit()
  assert [m["content"] for m in stored_rows(chat_id)] == ["first", "kept"]
  assert json.loads(legacy_text(chat_id)) == stored_rows(chat_id)


def test_a_failed_then_retried_commit_still_mirrors(monkeypatch):
  chat_id = seed(messages=[{"role": "user", "content": "one"}])
  real = rows._MARK_CONVERTED
  with SessionLocal() as db:
    rows.append(db, chat_id, {"role": "user", "content": "two"})
    monkeypatch.setattr(rows, "_MARK_CONVERTED", text("SELECT no_such_function()"))
    with pytest.raises(Exception):
      db.commit()
    monkeypatch.setattr(rows, "_MARK_CONVERTED", real)
    db.commit()  # The same transaction, retried.
  assert [m["content"] for m in stored_rows(chat_id)] == ["one", "two"]
  assert json.loads(legacy_text(chat_id)) == stored_rows(chat_id)
  assert converted(chat_id)


def test_closing_a_session_forgets_its_changed_chats():
  chat_id = seed(messages=[])
  db = SessionLocal()
  rows.append(db, chat_id, {"role": "user", "content": "never committed"})
  db.close()
  assert rows._DIRTY not in db.info
  assert stored_rows(chat_id) == []


def test_projections_are_total_over_arbitrary_json():
  odd = {"role": ["x"], "content": {"a": 1}, "hidden": [1], "cid": ["c"], "id": {"k": 1},
         "ts": [1], "blocks": [{"type": "tool", "tool": ["x"], "edit_preview": "no"}, 5, None]}
  attrs = rows.attributes(odd)
  assert attrs["flags"] & rows.HIDDEN and attrs["role"] is None
  for body in (odd, [], "s", 1, None, {"blocks": "notalist"}):
    rows.attributes(body)


def test_a_chat_that_fails_to_convert_never_stops_later_chats(monkeypatch):
  ids = [seed(f"iso-{i}", messages=[]) for i in range(3)]
  for chat_id in ids:
    previous_release_writes(chat_id, [{"role": "user", "content": chat_id}])
  real = rows.attributes

  def failing(body):
    if isinstance(body, dict) and body.get("content") == "iso-1":
      raise RuntimeError("projection bug")
    return real(body)

  monkeypatch.setattr(rows, "attributes", failing)
  asyncio.run(chat_writer.convert_remaining_transcripts())
  status = chat_writer.transcript_conversion_status
  assert status["state"] == "done"
  assert set(status["failed"]) == {"iso-1"} and "projection bug" in status["failed"]["iso-1"]
  assert converted("iso-0") and converted("iso-2") and not converted("iso-1")
  # Not hidden behind a placeholder: the legacy value stays authoritative.
  assert json.loads(legacy_text("iso-1")) == [{"role": "user", "content": "iso-1"}]
  with SessionLocal() as db:
    assert not rows.conversion_settled(db.get_bind())


def test_a_reader_is_never_refused_by_the_background_disk_floor(monkeypatch):
  """The floor defers bulk background work only; serving a request converts
  that one chat, bounded by SQLite's own SQLITE_FULL."""
  import app.resource_pressure as pressure
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "x"}])
  monkeypatch.setattr(pressure, "resource_status",
                      lambda _dir: {"pressure": {"disk": {"state": "critical"}}})
  convert_through_writer(chat_id)
  assert converted(chat_id)


def test_a_failed_conversion_answers_an_honest_503(client, auth, monkeypatch):
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "x"}])

  def failing(body):
    raise RuntimeError("projection bug")

  monkeypatch.setattr(rows, "attributes", failing)
  response = client.get(f"/api/chats/{chat_id}", headers=auth)
  assert response.status_code == 503
  assert "couldn't be prepared" in response.json()["detail"]
  assert "projection bug" in chat_writer.transcript_conversion_status["failed"][chat_id]
  # Worker-thread readers (sync routes anywhere) raise the same mapped error.
  result = {}

  def read():
    with SessionLocal() as db:
      try:
        rows.history(db.get(models.Chat, chat_id))
      except rows.TranscriptUnavailable as exc:
        result["error"] = exc

  worker = threading.Thread(target=read)
  worker.start()
  worker.join()
  assert result["error"].in_progress is False


def test_finished_background_conversion_stops_per_request_checks():
  chat_id = seed(messages=[])
  previous_release_writes(chat_id, [{"role": "user", "content": "x"}])
  with SessionLocal() as db:
    assert not rows.conversion_settled(db.get_bind())
  asyncio.run(chat_writer.convert_remaining_transcripts())
  statements = []
  from sqlalchemy import event
  listener = lambda *a: statements.append(a[2])
  event.listen(engine, "before_cursor_execute", listener)
  try:
    asyncio.run(rows.ensure_converted_async(chat_id))
  finally:
    event.remove(engine, "before_cursor_execute", listener)
  assert statements == []


def test_search_survives_prose_with_a_lone_surrogate(db):
  chat_id = seed(messages=[{"role": "user", "content": "gamma \ud800 delta"}])
  hits = chat_search.search(db, "gamma")
  assert [hit["id"] for hit in hits] == [chat_id]


def test_title_entries_are_stripped(db):
  chat_id = seed(messages=[], title="  Padded title\n")
  text_value = db.execute(text(
    "SELECT text FROM chat_search_entries WHERE chat_id = :id AND seq = -1"), {"id": chat_id}).scalar()
  assert text_value == "Padded title"


def test_every_migration_leaves_the_transcript_triggers_in_place(tmp_path):
  from app.database import Base
  from app.schema_migrations import TRANSCRIPT_TRIGGERS, run_migrations
  fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
  Base.metadata.create_all(fresh)
  run_migrations(fresh)
  with fresh.connect() as conn:
    present = {row[0] for row in conn.exec_driver_sql(
      "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
  assert set(TRANSCRIPT_TRIGGERS) <= present


def test_boot_reinstalls_a_lost_transcript_trigger_and_recovers_missed_writes():
  from app.schema_migrations import ensure_transcript_triggers
  missed = seed("missed", messages=[{"role": "user", "content": "f1"}])
  with engine.begin() as conn:
    conn.exec_driver_sql("DROP TRIGGER chats_messages_written")
  # The previous release writes while detection is gone: the marker stays.
  previous_release_writes(missed, [{"role": "user", "content": "f1"},
                                   {"role": "user", "content": "appended by previous"}])
  assert converted(missed)
  assert ensure_transcript_triggers(engine) == ["chats_messages_written"]
  assert ensure_transcript_triggers(engine) == []
  # Every chat re-converts from its exact legacy value; nothing is lost.
  assert not converted(missed)
  convert_through_writer(missed)
  assert [m["content"] for m in stored_rows(missed)] == ["f1", "appended by previous"]
  # Detection works again.
  chat_id = seed(messages=[{"role": "user", "content": "x"}])
  previous_release_writes(chat_id, [])
  assert not converted(chat_id)


def test_a_failed_commit_never_loses_a_new_chats_initial_transcript(monkeypatch):
  real = rows._MARK_CONVERTED
  chat = create_chat(id="retried", title="Retried", messages=[{"role": "user", "content": "kept"}])
  db = SessionLocal()
  db.add(chat)
  monkeypatch.setattr(rows, "_MARK_CONVERTED", text("SELECT no_such_function()"))
  with pytest.raises(Exception):
    db.commit()
  db.rollback()
  monkeypatch.setattr(rows, "_MARK_CONVERTED", real)
  db.add(chat)  # The caller retries with the same object.
  db.commit()
  db.close()
  assert stored_rows("retried") == [{"role": "user", "content": "kept"}]
  assert json.loads(legacy_text("retried")) == stored_rows("retried")


def test_iteration_never_yields_more_than_its_length():
  chat_id = seed(messages=[{"role": "user", "content": "0"}])
  with SessionLocal() as reader:
    view = rows.history(reader.get(models.Chat, chat_id))
    with SessionLocal() as writer:
      rows.append(writer, chat_id, {"role": "user", "content": "1"})
      writer.commit()
    assert len(list(view)) == len(view) == 1
    assert len(list(reversed(view))) == 1
