"""Server-owned lifecycle for per-chat uploads.

A new upload is a draft (`claimed: False`). The chat writer claims it in the
same commit that admits a user message naming it in `attachments`, whether the
message starts a turn or is queued (steered rows come from the queue, already
claimed). Only drafts may be discarded with `only_if_unused`, and drafts older
than `UNCLAIMED_UPLOAD_TTL` are swept the next time the chat receives an upload.
Entries without the key predate this lifecycle and are always retained.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

UNCLAIMED_UPLOAD_TTL = timedelta(days=7)


def is_draft(entry: dict) -> bool:
  return entry.get("claimed") is False


def claim_uploads(chat, attachments: list[dict] | None) -> None:
  """Mark the chat's draft uploads named by an admitted message's attachments."""
  names = {
    a.get("name") for a in (attachments or [])
    if isinstance(a, dict) and isinstance(a.get("name"), str)
  }
  if not names or not chat.uploads:
    return
  if not any(is_draft(u) and u.get("name") in names for u in chat.uploads):
    return
  chat.uploads = [
    {**u, "claimed": True} if is_draft(u) and u.get("name") in names else u
    for u in chat.uploads
  ]


def partition_expired_drafts(
  uploads: list[dict], now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
  """Split uploads into (kept, expired drafts)."""
  cutoff = (now or datetime.now(UTC)) - UNCLAIMED_UPLOAD_TTL
  kept, expired = [], []
  for entry in uploads:
    (expired if is_draft(entry) and _uploaded_before(entry, cutoff) else kept).append(entry)
  return kept, expired


def _uploaded_before(entry: dict, cutoff: datetime) -> bool:
  try:
    return datetime.fromisoformat(entry["uploaded_at"]) < cutoff
  except (KeyError, TypeError, ValueError):
    return False
