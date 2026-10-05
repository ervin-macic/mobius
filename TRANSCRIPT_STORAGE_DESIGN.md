# Per-message transcript storage

Chat transcripts move from one whole-transcript JSON value per chat
(`chats.messages`) to one `chat_messages` row per item, keyed by
`(chat_id, seq)`. The change ships over two releases (expand/contract), so
that at every committed state the previous release can be rolled back to and
sees every chat completely. No updater, deployment controller or database
"floor" takes part.

## Release 1 (this release)

### Rows are the authority; `chats.messages` is their mirror

- `chat_messages` holds each item's exact JSON text (`body`, the last column)
  and small lookup projections computed by `transcript_rows.attributes`:
  `message_key` (`str(id)`), the exact `message_id`, `client_id`
  (`chat_writer.cid_of`), `role`, the exact `ts` and `flags` (`EDIT_PREVIEW`,
  `HIDDEN`, `GOAL_COMPLETION`, `PROSE`, `DERIVED_CID`). Rows are dense from 0.
- Every row mutation goes through a `chat_writer` domain command calling
  `transcript_rows`; `chat_writer.create_chat` is the only way to create a chat.
  Mutations mark the chat dirty in its Session. One `before_commit` listener
  (`transcript_rows._mirror_changed_transcripts`) then runs, per changed chat,
  one SQL statement that rewrites `chats.messages` as
  `'[' || group_concat(body, ', ' ORDER BY seq) || ']'`, recomputes
  `has_messages` and moves `updated_at`. Bodies are default `json.dumps` text,
  so this is byte-for-byte `json.dumps(list)`: exactly what the previous
  release writes and decodes itself. No caller can skip it.
- Cost: each committing transaction rewrites a changed chat's whole legacy
  value, the previous release's own write cost, in SQLite rather than Python.
  `SQLITE_MAX_LENGTH` bounds one chat's value, as it always did.

### Detecting the previous release's writes without scanning

`chat_transcript_state` (one primary-key row per chat) marks a chat whose rows
are authoritative. Trigger `chats_messages_written` deletes the marker whenever
any writer updates `chats.messages`; the commit hook re-inserts it right after
this release's own update. So:

| Previous release does | Effect |
|---|---|
| updates `messages` (its ORM) | marker deleted, chat reconverted |
| inserts a chat | no marker, chat converted |
| deletes or purges a chat | `chats_deleted` removes rows, search entries, damage copies, marker and its own search documents |
| renames a chat | title trigger updates the search entry |

`chats` itself is unchanged: no column, rename or rebuild. Every trigger names
only columns the previous release maps, and its startup checks (`create_all`,
the migration ledger, `mapped_schema_gaps`, the readiness table probe) ignore
the additions. Its own search tables are left to it; it reconciles them from
`updated_at` after a rollback.

### Conversion never delays readiness

- `transcript_rows.convert` makes one chat authoritative in one transaction:
  delete stale rows, parse the legacy bytes, insert rows, insert the marker.
  Valid legacy values are not rewritten (they decode equal).
- A background task (`chat_writer.convert_remaining_transcripts`) submits one
  `ConvertNextTranscript` writer command per chat in id order. A chat whose
  conversion fails is recorded (`/api/debug/status` → `transcript_conversion`,
  per chat) and skipped, never retried by the loop and never hidden behind a
  placeholder: its legacy value stays authoritative and later chats still
  convert. Projections are total over arbitrary JSON, so such a failure means
  a bug, which stays visible.
- The disk rule is split by cause, not by path. The background loop is
  deferrable bulk work that grows the database by about the legacy
  transcript size, so it honours the platform's existing critical-disk
  verdict (the floor that already defers agent turns): it stops, and the next
  boot resumes it (the disk-pressure code has no recovery hook, and nothing
  re-arms it on a timer; the marker table is its durable progress). A
  conversion that serves a request (a reader's, or inline in a writer
  command) needs that one chat and is bounded by it, so it never consults
  the floor; SQLite's own `SQLITE_FULL` is its bound, and a failure leaves
  the legacy value authoritative. No reader is refused because of the floor.
- Any conversion that fails while serving a request raises
  `transcript_rows.TranscriptUnavailable`, which one app-level handler maps
  to 503 for every route, sync or async: "being prepared" only when the
  conversion simply did not finish in time, otherwise "couldn't be prepared;
  the error is recorded in diagnostics".
- Once the loop leaves no chat unconverted, a per-process fact
  (`transcript_rows.conversion_settled`) ends per-request checks: nothing in
  this process can unconvert a chat, because its own mirror re-marks.
- A chat read or written before then is converted on demand through one seam,
  `transcript_rows.require_rows`: the writer converts inline; a worker thread
  waits for the writer's `ConvertTranscript`; the event-loop thread never
  waits and raises `TranscriptNotConverted`. Event-loop callers await
  `transcript_rows.ensure_converted_async` first: the chat routers do so for
  their path's `chat_id` (`routes.chats.converted_path_chat`), and the few
  background coroutines that read transcripts call it explicitly.
- The commit mirror's changed-chat set belongs to the root transaction: it
  survives a rolled-back savepoint and a failed, retried commit, and is
  cleared only when the root transaction ends (commit, rollback or close).
- A legacy value that is not a JSON list keeps its exact bytes in
  `chat_transcript_damage`, written in the same transaction that replaces the
  chat's rows (and so its mirror) with a visible recovery placeholder.

### Reads and search

- `transcript_rows.history(chat)` is a position-addressed view sized when
  opened; iteration streams one statement (one snapshot); an index that has
  since vanished raises `IndexError`. Targeted reads (`at`, `client_message_seq`,
  `assistant_index`, `max_timestamp`, `metadata`) never decode ordinary bodies.
  Detail and log read owners pin one SQLite snapshot with `pin_read_snapshot`.
- `chat_search_entries` (stripped title at seq -1, one row per prose item,
  FTS5) is maintained by triggers on `chat_messages` and `chats.title` for
  every writer. Search reads entry text as bytes and decodes with
  replacement, so an escaped lone surrogate cannot fail a query.
- Boot checks the transcript triggers (`schema_migrations.TRANSCRIPT_TRIGGERS`,
  one `sqlite_master` read) while `chats.messages` exists and reinstalls any
  that are missing from 0086's frozen DDL, logging it. When the detection
  trigger itself was missing, the same transaction clears every conversion
  marker, so each chat re-converts from `chats.messages`, which is exact in
  every case (this release's mirror or the previous release's newer write): a later table rebuild
  would otherwise silently stop detecting the previous release's writes. A
  test applies every migration and asserts they remain.
  Search only reads, applying drawer visibility at query time.

### The next release's database

The previous release's NOT NULL column has no default, so `create_chat`
supplies a placeholder that the commit hook replaces; the ORM mapping has no
default of its own and is skipped by `mapped_schema_gaps` (column info
`legacy_transcript`). When the column is absent (`transcript_rows.legacy_present`
is false, after release 2 dropped it) this release reads and writes rows only,
so rolling back from release 2 to release 1 is safe. SQLite is the only
supported database; migration `0086_transcript_rows` refuses others.

## Release 2 (later)

- Refuse before any write when `chats.messages` exists and any chat lacks a
  marker (`transcript_conversion_incomplete`); the updater rolls back and
  release 1 finishes converting.
- One migration: drop `chats_messages_written`, recreate `chats_deleted`
  without the marker and old-search lines, drop `chat_transcript_state` and
  the previous release's search tables, clear and drop `chats.messages`.
- Remove `chats_messages_written` from `TRANSCRIPT_TRIGGERS` (boot's guard then
  checks the permanent triggers always), and delete `legacy_present` and its
  branches, the mirror clause of the commit
  hook, `require_rows`, `convert`, the conversion commands and task, the
  placeholder supply, the `legacy_messages` mapping and its gap-check skip.
  `chat_transcript_damage` rows stay. Change `reflection-evidence.py` to count
  rows.
- The previous release refuses a release-2 database by its own schema check
  (missing mapped column) before writing anything.
