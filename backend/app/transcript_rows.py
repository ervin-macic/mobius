"""Position-addressed transcript persistence and its legacy mirror.

Each saved transcript item is a ``chat_messages`` row keyed by
``(chat_id, seq)``; rows are dense from 0. Optional ``id``/``cid`` values are
lookup hints, never keys. Every write goes through a ``chat_writer`` domain
command, which calls the mutation functions below inside its transaction and
leaves commit/rollback to that command.

Two-release storage change (TRANSCRIPT_STORAGE_DESIGN.md). While the previous
release's ``chats.messages`` column exists (``legacy_present``):

* A chat's rows are authoritative only once a ``chat_transcript_state`` row
  exists for it. Until then ``chats.messages`` is, and ``require_rows``
  converts the chat (through the writer) before any read or write of rows.
* The ``before_commit`` hook below rewrites ``chats.messages`` from the rows
  of every chat changed in the transaction, so the previous image can be
  rolled back to at any committed state and sees every transcript.
* The schema trigger ``chats_messages_written`` deletes the state row whenever
  ``chats.messages`` is updated. The hook re-inserts it right after its own
  update; the previous image never does, so exactly the chats it changed (or
  created) are converted again when this release returns.

Search entries and purge cleanup are maintained by schema triggers (see
``schema_migrations._add_transcript_rows``) and need no code here.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from sqlalchemy import (
  DateTime, Text, bindparam, delete, event, func, insert, inspect, select, text, update,
)
from sqlalchemy.orm import Session, object_session

from app import models

EDIT_PREVIEW = 1
HIDDEN = 8
GOAL_COMPLETION = 16
PROSE = 32
DERIVED_CID = 64
ATTACHMENTS = 128

_PROSE_ROLES = ("user", "assistant")
# Set by chat_writer on the actor's own Session: conversion there runs inline,
# inside the command's transaction.
WRITER_SESSION = "transcript_writer"
_DIRTY = "transcript_dirty"
# New chats whose initial rows this transaction wrote; settled at commit.
_NEW_TRANSCRIPTS = "transcript_new_chats"
_M = models.ChatMessage


class TranscriptUnavailable(RuntimeError):
  """A chat's conversion, needed to serve a request, did not complete.

  ``in_progress`` distinguishes a conversion that has not finished in time
  from one that failed (its error is recorded in the conversion
  diagnostics). The app maps both to 503 with an honest message.
  """

  def __init__(self, chat_id: str, cause: BaseException):
    self.chat_id = chat_id
    self.in_progress = isinstance(cause, TimeoutError)
    super().__init__(f"chat {chat_id} transcript unavailable: {type(cause).__name__}: {cause}")


class TranscriptNotConverted(RuntimeError):
  """A caller that may not wait asked for an unconverted chat's rows.

  On the event-loop thread, await ``ensure_converted_async`` before opening
  the transcript.
  """


# -- Projections -------------------------------------------------------------

def _is_goal_tool(block) -> bool:
  from app.goal_plans import UPDATE_GOAL_TOOLS
  tool = block.get("tool")
  return block.get("type") == "tool" and isinstance(tool, str) and tool in UPDATE_GOAL_TOOLS


def attributes(body) -> dict:
  """Lookup projections of one body; the body itself stays authoritative.

  Total over arbitrary JSON: legacy transcripts may hold any value anywhere,
  and a projection must never be the reason a chat cannot be converted.
  """
  if not isinstance(body, dict):
    return {"message_key": None, "message_id": None, "client_id": None,
            "role": None, "ts": None, "flags": 0}
  from app.chat_writer import cid_of
  # Truthiness, as every reader of `hidden` (and the search rule) uses it.
  hidden = bool(body.get("hidden"))
  flags = HIDDEN if hidden else 0
  if body.get("attachments"):
    flags |= ATTACHMENTS
  blocks = body.get("blocks")
  for block in blocks if isinstance(blocks, list) else []:
    if not isinstance(block, dict):
      continue
    if block.get("attachments"):
      flags |= ATTACHMENTS
    if block.get("type") == "tool" and isinstance(block.get("edit_preview"), dict):
      flags |= EDIT_PREVIEW
    if _is_goal_tool(block):
      flags |= GOAL_COMPLETION
  role = body.get("role") if isinstance(body.get("role"), str) else None
  content = body.get("content")
  # Search prose: the previous release's indexing rule, decided only here.
  # The row triggers copy `content` for rows carrying this flag.
  if not hidden and role in _PROSE_ROLES and isinstance(content, str) and content.strip():
    flags |= PROSE
  # Exactly the writers' identity (cid_of), so one equality lookup matches
  # what every dedupe compares; flagged when derived so coordinates never
  # present a cid the body does not carry.
  client_id = cid_of(body)
  if client_id is not None and not body.get("cid"):
    flags |= DERIVED_CID
  message_id = body.get("id")
  return {"message_key": None if message_id is None else _key(message_id),
          "message_id": message_id, "client_id": client_id, "role": role,
          "ts": body.get("ts"), "flags": flags}


def _key(value) -> str:
  # str() is the identity comparison the writers always used; the ASCII JSON
  # escape makes any id (even a lone surrogate) a storable equality key.
  return json.dumps(str(value))


def _row(chat_id, seq, body) -> dict:
  return {"chat_id": chat_id, "seq": seq, "body": body, **attributes(body)}


def _id(chat) -> str:
  return chat if isinstance(chat, str) else chat.id


def damaged_messages() -> list[dict]:
  return [{
    "role": "assistant",
    "content": "This chat's stored transcript is damaged. "
               "Its original bytes have been preserved for recovery.",
    "blocks": [{"type": "text",
                "content": "Damaged transcript — original preserved for recovery."}],
    "transcript_damage": True,
  }]


# -- Legacy column and conversion -------------------------------------------

_LEGACY_BY_ENGINE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def legacy_present(db) -> bool:
  """Whether this database still has the previous release's column.

  A schema fact read once per engine. It is false only after the next
  release dropped the column; every database this release creates has it.
  """
  engine = db.get_bind().engine
  known = _LEGACY_BY_ENGINE.get(engine)
  if known is None:
    with engine.connect() as conn:
      known = "messages" in {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(chats)")
      }
    _LEGACY_BY_ENGINE[engine] = known
  return known


_ALL_CONVERTED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def conversion_settled(bind) -> bool:
  """True once every chat on this engine is known converted (or no legacy).

  Recorded by ``mark_all_converted`` after background conversion leaves no
  chat behind. Nothing in this process can unconvert a chat afterwards (its
  own mirror re-marks), so per-request checks stop querying.
  """
  engine = bind.engine
  return bool(_ALL_CONVERTED.get(engine))


def mark_all_converted(db) -> None:
  if unconverted_count(db) == 0:
    _ALL_CONVERTED[db.get_bind().engine] = True


def reset_conversion_facts() -> None:
  """Forget per-engine facts; tests reuse one engine across databases."""
  _ALL_CONVERTED.clear()
  _LEGACY_BY_ENGINE.clear()


def is_converted(db, chat_id: str) -> bool:
  if not legacy_present(db) or conversion_settled(db.get_bind()):
    return True
  return db.execute(text(
    "SELECT 1 FROM chat_transcript_state WHERE chat_id = :id"
  ), {"id": chat_id}).first() is not None


def needs_conversion(db, chat_id: str) -> bool:
  """Whether this chat exists and its rows are not yet authoritative."""
  return not is_converted(db, chat_id) and db.execute(
    text("SELECT 1 FROM chats WHERE id = :id"), {"id": chat_id},
  ).first() is not None


def convert(db, chat_id: str) -> bool:
  """Make one chat's rows authoritative from its legacy value, in ``db``'s
  transaction. Returns whether anything was converted.

  Valid JSON is not rewritten: its rows decode to the same values. Damaged
  bytes are preserved before the legacy value is replaced by the visible
  placeholder that the rows then hold.
  """
  if is_converted(db, chat_id):
    return False
  raw = db.execute(text(
    "SELECT CAST(messages AS BLOB) FROM chats WHERE id = :id"
  ), {"id": chat_id}).first()
  if raw is None:
    return False
  raw = bytes(raw[0] or b"")
  db.execute(delete(_M).where(_M.chat_id == chat_id))
  error = None
  try:
    messages = json.loads(raw)
    if messages is None:
      # The previous release displayed a JSON null as an empty chat; the
      # mirror rewrites it as the empty list the rows now hold.
      messages = []
      _changed(db, chat_id)
    elif not isinstance(messages, list):
      error = "The stored transcript is not a message list"
  except (ValueError, UnicodeError) as exc:
    error = f"The stored transcript is not valid JSON: {exc}"
  if error is None:
    _insert(db, chat_id, 0, messages)
    db.execute(text("INSERT INTO chat_transcript_state(chat_id) VALUES (:id)"),
               {"id": chat_id})
    return True
  db.add(models.ChatTranscriptDamage(chat_id=chat_id, raw=raw, error=error))
  _insert(db, chat_id, 0, damaged_messages())
  _changed(db, chat_id)
  return True


def live_working_set(db) -> list[str]:
  """Unconverted chats that boot recovery and the startup sweeps read.

  Unconverted chats only come into existence while older code runs, so they
  all exist at boot. This set is the work actually in flight: chats with a
  non-terminal run, chats with queued messages, and both ends of every
  delegation whose parent or child has a non-terminal run. Its size is
  bounded by in-flight work, not by history (an idle open Goal is not in
  flight: its next turn, wake or steer converts what it reads first).
  Everything else is converted by its first reader or in the background.
  """
  if not legacy_present(db) or conversion_settled(db.get_bind()):
    return []
  m = models
  running = set(db.execute(select(m.ChatRun.chat_id).where(
    m.ChatRun.status.in_(m.NONTERMINAL_RUN_STATUSES),
  )).scalars())
  live = running | set(db.execute(select(m.Chat.id).where(
    m.Chat.pending_messages.cast(Text).not_in(("[]", "null")),
  )).scalars())
  for parent, child in db.execute(select(m.Delegation.parent_chat_id, m.Delegation.child_chat_id)).all():
    if parent in running or child in running:
      live.update(chat for chat in (parent, child) if chat)
  return sorted(chat_id for chat_id in live if needs_conversion(db, chat_id))


def next_unconverted(db, after: str | None) -> str | None:
  """The next chat in id order without authoritative rows (keyset scan)."""
  return db.execute(text(
    "SELECT c.id FROM chats c WHERE (:after IS NULL OR c.id > :after) "
    "AND NOT EXISTS (SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id) "
    "ORDER BY c.id LIMIT 1"
  ), {"after": after}).scalar()


def unconverted_count(db) -> int:
  if not legacy_present(db) or conversion_settled(db.get_bind()):
    return 0
  return db.execute(text(
    "SELECT COUNT(*) FROM chats c WHERE NOT EXISTS "
    "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id)"
  )).scalar()


def _on_event_loop() -> bool:
  try:
    asyncio.get_running_loop()
  except RuntimeError:
    return False
  return True


def require_rows(db, chat) -> None:
  """The one seam that makes a chat's rows authoritative before use.

  The writer converts inline. A worker thread waits for the writer to
  convert. The event-loop thread must never wait: it raises, and async
  callers await ``ensure_converted_async`` first.
  """
  chat_id = _id(chat)
  if not isinstance(chat, str) and inspect(chat).pending:
    db.flush()  # A chat created in this session is converted from birth.
  if db.info.get(WRITER_SESSION):
    convert(db, chat_id)
    return
  if not needs_conversion(db, chat_id):
    return  # Converted, or absent (the caller's own lookup reports that).
  if _on_event_loop():
    raise TranscriptNotConverted(
      f"chat {chat_id} is not converted; await ensure_converted_async first",
    )
  raw = db.connection().connection.driver_connection
  if raw.in_transaction:
    # Waiting would deadlock on this session's own lock or keep reading the
    # snapshot that predates the conversion.
    raise RuntimeError(
      "require_rows needs the chat converted before this session's transaction",
    )
  from app.chat_writer import ConvertTranscript, get_writer, wait_ack
  try:
    wait_ack(get_writer().submit(ConvertTranscript(chat_id=chat_id)))
  except Exception as exc:
    raise TranscriptUnavailable(chat_id, exc) from exc


async def ensure_converted_async(chat_id: str, db=None) -> None:
  """Await the writer's conversion of one chat; for event-loop callers."""
  from app.chat_writer import ConvertTranscript, await_ack, get_writer
  from app.database import engine
  if conversion_settled(engine if db is None else db.get_bind()):
    return
  if db is None:
    from app.database import SessionLocal
    with SessionLocal() as session:
      needed = needs_conversion(session, chat_id)
  else:
    needed = needs_conversion(db, chat_id)
  if needed:
    try:
      await await_ack(get_writer().submit(ConvertTranscript(chat_id=chat_id)))
    except Exception as exc:
      raise TranscriptUnavailable(chat_id, exc) from exc


# -- The legacy mirror: one write path ---------------------------------------

def _changed(db, chat) -> None:
  db.info.setdefault(_DIRTY, set()).add(_id(chat))


_MIRROR = text(
  "UPDATE chats SET messages = '[' || coalesce((SELECT group_concat(body, ', ' ORDER BY seq) "
  "FROM chat_messages WHERE chat_id = :id), '') || ']', "
  "has_messages = EXISTS (SELECT 1 FROM chat_messages WHERE chat_id = :id), "
  "updated_at = :now WHERE id = :id"
).bindparams(bindparam("now", type_=DateTime))
_SCALARS = text(
  "UPDATE chats SET has_messages = EXISTS (SELECT 1 FROM chat_messages WHERE chat_id = :id), "
  "updated_at = :now WHERE id = :id"
).bindparams(bindparam("now", type_=DateTime))
_MARK_CONVERTED = text(
  "INSERT OR IGNORE INTO chat_transcript_state(chat_id) SELECT id FROM chats WHERE id = :id"
)


@event.listens_for(Session, "before_commit")
def _mirror_changed_transcripts(session) -> None:
  """Derive each changed chat's legacy value and scalars from its rows.

  Each body holds default ``json.dumps`` text, so ``'[' + ', '.join(bodies)
  + ']'`` is exactly ``json.dumps(list)``: the bytes the previous release
  writes and decodes itself. One statement per changed chat per root commit.

  The changed set belongs to the root transaction (cleared only when it
  ends, below), so a rolled-back savepoint or a failed and retried commit
  still mirrors every chat the committing transaction changed. Mirroring a
  chat whose savepoint change was rolled back is harmless: the mirror is
  derived from the rows as they commit.
  """
  if session.in_nested_transaction():
    return  # A savepoint release; the root commit mirrors.
  session.flush()
  dirty = session.info.get(_DIRTY)
  if not dirty:
    return
  legacy = legacy_present(session)
  now = datetime.now(UTC)
  for chat_id in sorted(dirty):
    params = {"id": chat_id, "now": now}
    if legacy:
      session.execute(_MIRROR, params)
      # chats_messages_written just deleted the state row; this release's own
      # mirror leaves the chat converted.
      session.execute(_MARK_CONVERTED, {"id": chat_id})
    else:
      session.execute(_SCALARS, params)


@event.listens_for(Session, "after_commit")
def _settle_new_chat_transcripts(session) -> None:
  for chat in session.info.pop(_NEW_TRANSCRIPTS, ()):
    chat.__dict__.pop("_initial_transcript", None)


@event.listens_for(Session, "after_transaction_end")
def _forget_changes_with_their_transaction(session, transaction) -> None:
  # Root commit, rollback or close; never a savepoint inside it.
  if transaction.parent is None:
    session.info.pop(_DIRTY, None)
    session.info.pop(_NEW_TRANSCRIPTS, None)


# -- Mutations (writer domain commands only) --------------------------------

def _insert(db, chat_id: str, start: int, messages) -> None:
  rows = [_row(chat_id, start + i, body) for i, body in enumerate(messages)]
  if rows:
    db.execute(insert(_M.__table__), rows)


def _size(db, chat_id: str) -> int:
  last = db.execute(select(func.max(_M.seq)).where(_M.chat_id == chat_id)).scalar()
  return 0 if last is None else last + 1


def initialize_new(chat, messages) -> None:
  """Attach a new chat's initial transcript; ``chat_writer.create_chat`` owns
  this. The chat has no session yet, so its rows are written when the
  session it joins next flushes (below). It is authoritative from birth.
  """
  chat._initial_transcript = list(messages)




@event.listens_for(Session, "before_flush")
def _write_new_chat_transcripts(session, _context, _instances) -> None:
  new = [obj for obj in session.new
         if isinstance(obj, models.Chat) and hasattr(obj, "_initial_transcript")]
  if not new:
    return
  legacy = legacy_present(session)
  for chat in new:
    # Kept on the object until the transaction commits: a failed flush or
    # commit rolls these rows back, and a retry of the same object writes
    # them again.
    messages = chat._initial_transcript
    session.info.setdefault(_NEW_TRANSCRIPTS, []).append(chat)
    if legacy:
      # The previous release's NOT NULL column has no default on databases
      # it created; the commit mirror replaces this placeholder.
      chat.legacy_messages = []
      session.execute(text("INSERT INTO chat_transcript_state(chat_id) VALUES (:id)"),
                      {"id": chat.id})
    _insert(session, chat.id, 0, messages)
    _changed(session, chat.id)


def append(db, chat, message) -> int:
  require_rows(db, chat)
  chat_id = _id(chat)
  seq = _size(db, chat_id)
  _insert(db, chat_id, seq, [message])
  _changed(db, chat_id)
  return seq


def append_many(db, chat, messages) -> None:
  require_rows(db, chat)
  chat_id = _id(chat)
  _insert(db, chat_id, _size(db, chat_id), list(messages))
  _changed(db, chat_id)


def _stored_text(db, chat_id: str, seq: int):
  return db.execute(select(_M.body.cast(Text)).where(
    _M.chat_id == chat_id, _M.seq == seq,
  )).scalar()


def update_at(db, chat, index: int, body) -> None:
  require_rows(db, chat)
  chat_id = _id(chat)
  stored = _stored_text(db, chat_id, index)
  if stored is None:
    raise IndexError(index)
  if stored == json.dumps(body):
    return
  db.execute(update(_M).where(_M.chat_id == chat_id, _M.seq == index).values(
    body=body, **attributes(body),
  ))
  _changed(db, chat_id)


def replace_all(db, chat, messages) -> None:
  """Explicit whole-history operations rewrite only positions that changed."""
  require_rows(db, chat)
  chat_id = _id(chat)
  messages = list(messages)
  stored = dict(db.execute(select(_M.seq, _M.body.cast(Text)).where(
    _M.chat_id == chat_id,
  )).all())
  changed = False
  for index, body in enumerate(messages):
    if index not in stored:
      continue
    if stored[index] != json.dumps(body):
      db.execute(update(_M).where(_M.chat_id == chat_id, _M.seq == index).values(
        body=body, **attributes(body),
      ))
      changed = True
  if len(messages) > len(stored):
    _insert(db, chat_id, len(stored), messages[len(stored):])
    changed = True
  elif len(messages) < len(stored):
    db.execute(delete(_M).where(_M.chat_id == chat_id, _M.seq >= len(messages)))
    changed = True
  if changed:
    _changed(db, chat_id)


# -- Reads -------------------------------------------------------------------

def pin_read_snapshot(db, *chats) -> None:
  """Keep one read owner's metadata and body windows on one SQLite snapshot.

  Python's legacy sqlite3 transaction mode issues no BEGIN for SELECT, so a
  concurrently settled reply could otherwise move a page's coordinates
  between its reads. The chats read under it are converted first: a snapshot
  taken before a conversion would never see that chat's rows.
  """
  for chat in chats:
    require_rows(db, chat)
  connection = db.connection()
  if connection.dialect.name == "sqlite":
    raw = connection.connection.driver_connection
    if not raw.in_transaction:
      connection.exec_driver_sql("BEGIN")


def count(db, chat) -> int:
  require_rows(db, chat)
  return _size(db, _id(chat))


def at(db, chat, index: int):
  """The body at ``index`` (negative from the end); None outside the range."""
  require_rows(db, chat)
  chat_id = _id(chat)
  if index < 0:
    index += _size(db, chat_id)
  if index < 0:
    return None
  row = db.execute(select(_M.body).where(_M.chat_id == chat_id, _M.seq == index)).first()
  return None if row is None else row[0]


def _window(db, chat_id: str, start: int, end: int) -> list:
  return list(db.execute(select(_M.body).where(
    _M.chat_id == chat_id, _M.seq >= start, _M.seq < end,
  ).order_by(_M.seq)).scalars())


def _stream(db, chat_id: str, *, descending: bool = False,
            below: int | None = None) -> Iterator[tuple[int, object]]:
  # One statement is one consistent SQLite snapshot, without row-count batches.
  order = _M.seq.desc() if descending else _M.seq
  condition = _M.chat_id == chat_id
  if below is not None:
    condition = condition & (_M.seq < below)
  yield from db.execute(select(_M.seq, _M.body).where(condition).order_by(order)).tuples()


def iterate(db, chat) -> Iterator:
  require_rows(db, chat)
  return (body for _seq, body in _stream(db, _id(chat)))


def reverse_iter(db, chat) -> Iterator[tuple[int, object]]:
  require_rows(db, chat)
  return _stream(db, _id(chat), descending=True)


def read_all(db, chat) -> list:
  require_rows(db, chat)
  return [body for _seq, body in _stream(db, _id(chat))]


def assistant_index(db, chat, message) -> int:
  """Position of the assistant row this message updates, or -1 to append."""
  require_rows(db, chat)
  chat_id = _id(chat)
  key = message.get("id") if isinstance(message, dict) else None
  if key is not None:
    found = db.execute(select(_M.seq).where(
      _M.chat_id == chat_id, _M.role == "assistant", _M.message_key == _key(key),
    ).order_by(_M.seq).limit(1)).scalar()
    if found is not None:
      return found
  size = _size(db, chat_id)
  if not size:
    return -1
  last = db.execute(select(_M.role, _M.message_id).where(
    _M.chat_id == chat_id, _M.seq == size - 1,
  )).first()
  if last.role == "assistant" and (key is None or last.message_id is None):
    return size - 1
  return -1


def client_message_seq(db, chat, client_id: str, *, role: str = "user") -> int | None:
  """Position of the first ``role`` row whose cid (``cid_of``) matches."""
  require_rows(db, chat)
  return db.execute(select(_M.seq).where(
    _M.chat_id == _id(chat), _M.role == role, _M.client_id == client_id,
  ).order_by(_M.seq).limit(1)).scalar()


def attachment_bodies(db, chat) -> list:
  """Bodies of the rows that name attachments (on the message or a block).

  Upload release asks whether anything still names a file; only these rows
  can, so it never decodes the rest of the history.
  """
  require_rows(db, chat)
  return list(db.execute(select(_M.body).where(
    _M.chat_id == _id(chat), _M.flags.op("&")(ATTACHMENTS) != 0,
  ).order_by(_M.seq)).scalars())


def max_timestamp(db, chat):
  """Largest numeric ``ts`` (never bool), exact as stored; 0 when none."""
  require_rows(db, chat)
  value = db.execute(text(
    "SELECT ts FROM chat_messages WHERE chat_id = :id "
    "AND json_type(ts) IN ('integer', 'real') "
    "ORDER BY CAST(ts AS REAL) DESC LIMIT 1"
  ), {"id": _id(chat)}).scalar()
  return 0 if value is None else json.loads(value)


def metadata(db, chat) -> list[dict]:
  """Identity and lifecycle coordinates without hydrating ordinary bodies."""
  require_rows(db, chat)
  chat_id = _id(chat)
  result, goal_rows = [], []
  for seq, message_id, client_id, role, ts, flags in db.execute(select(
    _M.seq, _M.message_id, _M.client_id, _M.role, _M.ts, _M.flags,
  ).where(_M.chat_id == chat_id).order_by(_M.seq)).tuples():
    message = {"role": role, "ts": ts, "hidden": bool(flags & HIDDEN)}
    if message_id is not None:
      message["id"] = message_id
    if client_id is not None and not flags & DERIVED_CID:
      message["cid"] = client_id
    if flags & GOAL_COMPLETION:
      goal_rows.append(seq)
    result.append(message)
  if goal_rows:
    # Goal placement needs those rows' blocks; only they are decoded.
    for seq, body in db.execute(select(_M.seq, _M.body).where(
      _M.chat_id == chat_id, _M.seq.in_(goal_rows),
    )).tuples():
      result[seq]["blocks"] = body.get("blocks", []) if isinstance(body, dict) else []
  return result


class History(Sequence):
  """A position-addressed view of one chat's rows, sized when opened.

  Iteration streams the current rows; indexing a position that has since
  vanished raises IndexError instead of returning a placeholder.
  """

  def __init__(self, chat):
    self.chat = chat
    self.db = object_session(chat)
    if self.db is None:
      raise RuntimeError("Transcript reads require the chat's database session")
    require_rows(self.db, chat)
    self.size = _size(self.db, chat.id)

  def __len__(self):
    return self.size

  def __getitem__(self, key):
    if isinstance(key, slice):
      start, stop, step = key.indices(self.size)
      if step == 1:
        return _window(self.db, self.chat.id, start, stop)
      return [self[index] for index in range(start, stop, step)]
    index = key + self.size if key < 0 else key
    if index < 0 or index >= self.size:
      raise IndexError(key)
    row = self.db.execute(select(_M.body).where(
      _M.chat_id == self.chat.id, _M.seq == index,
    )).first()
    if row is None:
      raise IndexError(key)
    return row[0]

  def __iter__(self):
    # Bounded by the size taken at opening, so iteration never yields more
    # items than len(); positions removed since then are simply absent.
    if not self.size:
      return iter(())  # An empty transcript is known from opening; read nothing.
    return (body for _seq, body in _stream(self.db, self.chat.id, below=self.size))

  def __reversed__(self):
    if not self.size:
      return iter(())
    return (body for _seq, body in _stream(
      self.db, self.chat.id, descending=True, below=self.size))


def history(chat) -> History:
  return History(chat)
