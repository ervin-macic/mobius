"""Seed a link-invitation grant as one issued before link invitations were retired.

Installations upgraded from that release still hold these rows: they must list,
revoke and, until the one-day link expires, redeem.
"""

import secrets
from datetime import timedelta

from app.browser_access import BrowserAccessGrant, BrowserAccessInvite, _hash_secret
from app.timeutil import now_naive_utc


def link_grant(db, owner, label: str) -> tuple[BrowserAccessGrant, str]:
  """Return (grant, one-time invite secret) for a stored link-invitation grant."""
  now = now_naive_utc()
  secret = secrets.token_urlsafe(32)
  grant = BrowserAccessGrant(
    id=secrets.token_urlsafe(24), owner_id=owner.id, label=label,
    kind="invitation", created_at=now,
  )
  db.add(grant)
  db.add(BrowserAccessInvite(
    id=secrets.token_urlsafe(24), grant_id=grant.id, secret_hash=_hash_secret(secret),
    owner_token_epoch=owner.token_epoch, created_at=now, expires_at=now + timedelta(days=1),
  ))
  db.commit()
  return grant, secret
