"""Server-owned lifecycle for per-chat uploads.

A new upload is a draft (`claimed: False`). The chat writer claims it in the
same commit that admits a user message naming it in `attachments`, whether the
message starts a turn or is queued (steered rows come from the queue, already
claimed). Cancelling a queued message releases its files back to drafts unless
another message still names them. Only drafts can be deleted, and drafts older
than `UNCLAIMED_UPLOAD_TTL` are swept when the chat next receives an upload or
a message. Entries without the key predate this lifecycle and are always kept,
because nothing records whether a sent message uses them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

UNCLAIMED_UPLOAD_TTL = timedelta(days=7)


def is_draft(entry: dict) -> bool:
  return entry.get("claimed") is False


def attachment_names(attachments: list[dict] | None) -> set[str]:
  return {
    a["name"] for a in (attachments or [])
    if isinstance(a, dict) and isinstance(a.get("name"), str)
  }


def _set_claimed(chat, names: set[str], claimed: bool) -> None:
  if not names or not any(
    u.get("name") in names and is_draft(u) == claimed for u in chat.uploads or []
  ):
    return
  chat.uploads = [
    {**u, "claimed": claimed} if u.get("name") in names and "claimed" in u else u
    for u in chat.uploads
  ]


def claim_uploads(chat, attachments: list[dict] | None) -> None:
  """Mark the chat's draft uploads named by an admitted message's attachments."""
  _set_claimed(chat, attachment_names(attachments), True)


def release_uploads(chat, removed_rows: list[dict]) -> None:
  """Return a cancelled message's files to drafts unless another row names them."""
  names = set()
  for row in removed_rows:
    names |= attachment_names(row.get("attachments"))
  for row in [*(chat.messages or []), *(chat.pending_messages or [])]:
    names -= attachment_names(row.get("attachments"))
  _set_claimed(chat, names, False)


def partition_expired_drafts(
  uploads: list[dict], *, keep: set[str] = frozenset(), now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
  """Split uploads into (kept, expired drafts); names in `keep` never expire."""
  cutoff = (now or datetime.now(UTC)) - UNCLAIMED_UPLOAD_TTL
  kept, expired = [], []
  for entry in uploads:
    stale = (is_draft(entry) and entry.get("name") not in keep
             and _uploaded_before(entry, cutoff))
    (expired if stale else kept).append(entry)
  return kept, expired


def _uploaded_before(entry: dict, cutoff: datetime) -> bool:
  try:
    return datetime.fromisoformat(entry["uploaded_at"]) < cutoff
  except (KeyError, TypeError, ValueError):
    return False
