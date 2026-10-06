"""Contract tests for the unregistered browser-access persistence foundation."""

import re
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.browser_access import (
  BrowserAccessGrant, BrowserAccessInvite, BrowserAccessSession,
  SESSION_IDLE_TTL, logout_session, redeem_invitation, renew_session,
  BrowserLineage, is_live, revoke_grant,
)
from app.models import Owner
from app.database import SessionLocal
from app.timeutil import now_naive_utc
from tests.browser_access_fixtures import link_grant


def _owner(db, name):
  owner = Owner(username=name, hashed_password="unused")
  db.add(owner)
  db.commit()
  return owner


def _live(db, grant_id, owner_id, session_id=None):
  return is_live(db, BrowserLineage(grant_id, session_id), owner_id)


def _denied(action):
  with pytest.raises(HTTPException) as error:
    action()
  assert error.value.status_code == 401


def test_invitation_is_one_use_and_session_is_idle_expiring_not_grant_expiring(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  secret, session, received_grant, received_owner = redeem_invitation(db, invitation)
  assert received_grant.id == grant.id
  assert received_owner.id == owner.id
  assert session.secret_hash != secret
  assert session.idle_expires_at - session.created_at == SESSION_IDLE_TTL
  _denied(lambda: redeem_invitation(db, invitation))
  db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).update({
    BrowserAccessInvite.expires_at: now_naive_utc() - timedelta(days=2),
  })
  db.commit()
  assert renew_session(db, secret)[0].id == grant.id
  assert _live(db, grant.id, owner.id)
  session.idle_expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: renew_session(db, secret))
  assert _live(db, grant.id, owner.id)


def test_expired_invitation_never_consumes_or_issues_session(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  invite = db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
  invite.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  _denied(lambda: redeem_invitation(db, invitation))
  db.refresh(invite)
  assert invite.consumed_at is None
  assert db.query(BrowserAccessSession).count() == 0


def test_revocation_is_idempotent_and_cannot_affect_another_grant(db):
  owner = _owner(db, "owner")
  grant_a, invite_a = link_grant(db, owner, "a")
  grant_b, invite_b = link_grant(db, owner, "b")
  secret_a, _, _, _ = redeem_invitation(db, invite_a)
  secret_b, _, _, _ = redeem_invitation(db, invite_b)
  revoked_at = revoke_grant(db, grant_a.id, owner.id).revoked_at
  assert revoked_at is not None
  assert revoke_grant(db, grant_a.id, owner.id).revoked_at == revoked_at
  _denied(lambda: renew_session(db, secret_a))
  assert not _live(db, grant_a.id, owner.id)
  assert renew_session(db, secret_b)[0].id == grant_b.id
  assert _live(db, grant_b.id, owner.id)


def test_owner_epoch_change_invalidates_session_but_not_other_permission(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  secret, _, _, _ = redeem_invitation(db, invitation)
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: renew_session(db, secret))
  assert _live(db, grant.id, owner.id)


def test_owner_epoch_change_invalidates_unredeemed_invitation(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  owner.token_epoch += 1
  db.commit()
  _denied(lambda: redeem_invitation(db, invitation))
  assert db.query(BrowserAccessSession).count() == 0
  invite = db.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
  assert invite.consumed_at is None


def test_session_liveness_checks_lineage_expiry_epoch_and_logout(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant, invitation = link_grant(db, owner, "recipient")
  other_grant, _ = link_grant(db, owner, "other recipient")
  secret, session, _, _ = redeem_invitation(db, invitation)
  assert _live(db, grant.id, owner.id, session.id)
  assert not _live(db, other_grant.id, owner.id, session.id)
  assert not _live(db, grant.id, other.id, session.id)
  assert not _live(db, grant.id, owner.id, "missing")
  logout_session(db, secret)
  logout_session(db, secret)
  logout_session(db, "unknown-secret-with-adequate-length")
  logout_session(db, None)
  assert not _live(db, grant.id, owner.id, session.id)
  _denied(lambda: renew_session(db, secret))
  assert _live(db, grant.id, owner.id)


def test_session_liveness_denies_idle_expiry_and_owner_epoch_change(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  _, session, _, _ = redeem_invitation(db, invitation)
  session.idle_expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  assert not _live(db, grant.id, owner.id, session.id)
  session.idle_expires_at = now_naive_utc() + SESSION_IDLE_TTL
  owner.token_epoch += 1
  db.commit()
  assert not _live(db, grant.id, owner.id, session.id)


def test_revoking_a_link_grant_voids_its_unredeemed_invite(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  revoke_grant(db, grant.id, owner.id)
  _denied(lambda: redeem_invitation(db, invitation))
  assert db.query(BrowserAccessSession).count() == 0


@pytest.mark.parametrize("claim", [None, "", 123, [], {}, True, "x" * 65])
def test_malformed_grant_id_claim_fails_closed(claim):
  _denied(lambda: BrowserLineage.from_claims({"browser_grant": claim}))
  if claim is not None:
    _denied(lambda: BrowserLineage.from_claims({"browser_grant": "grant", "browser_session": claim}))


def test_retired_epoch_claim_is_ignored_but_never_stands_alone(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  _, session, _, _ = redeem_invitation(db, invitation)
  # Bearers minted before the epoch retired keep working until revoked.
  legacy = {"browser_grant": grant.id, "browser_grant_epoch": 0, "browser_session": session.id}
  assert BrowserLineage.from_claims(legacy) == BrowserLineage(grant.id, session.id)
  assert BrowserLineage.from_claims({"sub": "owner"}) is None
  _denied(lambda: BrowserLineage.from_claims({"browser_grant_epoch": 0}))
  _denied(lambda: BrowserLineage.from_claims({"browser_session": session.id}))
  revoke_grant(db, grant.id, owner.id)
  assert not is_live(db, BrowserLineage.from_claims(legacy), owner.id)


def test_revocation_is_terminal_nothing_clears_revoked_at():
  """A grant id is sufficient lineage only because revocation is permanent.

  If grants ever need pause/resume, reintroduce a generation number rather
  than clearing ``revoked_at`` (see browser_access module docstring).
  """
  import ast
  from pathlib import Path
  offenders = []
  for path in Path(__file__).resolve().parents[1].joinpath("app").rglob("*.py"):
    source = path.read_text(encoding="utf-8")
    if "BrowserAccessGrant" not in source and "browser_access_grants" not in source:
      continue
    if re.search(r"revoked_at\s*=\s*NULL", source, re.IGNORECASE):
      offenders.append(f"{path.name}: SQL clears revoked_at")
    for node in ast.walk(ast.parse(source)):
      clears = False
      if isinstance(node, ast.Assign):
        clears = any(isinstance(t, ast.Attribute) and t.attr == "revoked_at" for t in node.targets) and (
          isinstance(node.value, ast.Constant) and node.value.value is None)
      elif isinstance(node, ast.Call) and not (
        isinstance(node.func, ast.Attribute) and node.func.attr in ("filter_by", "get", "pop")
      ):
        clears = any(k.arg == "revoked_at" and isinstance(k.value, ast.Constant) and k.value.value is None
                     for k in node.keywords)
      elif isinstance(node, ast.Dict):
        clears = any(
          ((isinstance(k, ast.Attribute) and k.attr == "revoked_at")
           or (isinstance(k, ast.Constant) and k.value == "revoked_at"))
          and isinstance(v, ast.Constant) and v.value is None
          for k, v in zip(node.keys, node.values)
        )
      if clears:
        offenders.append(f"{path.name}:{node.lineno}")
  assert offenders == []


def test_wrong_owner_cannot_revoke_and_invite_remains_redeemable(db):
  owner = _owner(db, "owner")
  other = _owner(db, "other")
  grant, invitation = link_grant(db, owner, "recipient")
  _denied(lambda: revoke_grant(db, grant.id, other.id))
  assert db.get(BrowserAccessGrant, grant.id).revoked_at is None
  assert redeem_invitation(db, invitation)[2].id == grant.id


def test_two_sessions_preloading_same_invite_still_only_redeem_once(db):
  owner = _owner(db, "owner")
  grant, invitation = link_grant(db, owner, "recipient")
  first = SessionLocal()
  second = SessionLocal()
  try:
    assert first.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
    assert second.query(BrowserAccessInvite).filter_by(grant_id=grant.id).one()
    assert redeem_invitation(first, invitation)[2].id == grant.id
    _denied(lambda: redeem_invitation(second, invitation))
    assert db.query(BrowserAccessSession).count() == 1
  finally:
    first.close()
    second.close()


def test_browser_tables_migrate_without_current_model_metadata(tmp_path):
  from sqlalchemy import create_engine, text
  from app.schema_migrations import _add_browser_access_tables
  engine = create_engine(f"sqlite:///{tmp_path / 'bare-browser.db'}")
  with engine.begin() as connection:
    connection.execute(text("CREATE TABLE owner (id INTEGER PRIMARY KEY, username VARCHAR, token_epoch INTEGER NOT NULL)"))
    connection.execute(text("INSERT INTO owner VALUES (1, 'old-owner', 0)"))
  _add_browser_access_tables(engine)
  _add_browser_access_tables(engine)
  with engine.begin() as connection:
    connection.execute(text("INSERT INTO browser_access_grants (id,owner_id,label,epoch,created_at) VALUES ('guest',1,'Alice',0,CURRENT_TIMESTAMP)"))
    assert connection.execute(text("SELECT label FROM browser_access_grants")).scalar_one() == "Alice"


@pytest.mark.asyncio
async def test_ending_a_grant_runs_every_stop_step_when_one_fails(db, monkeypatch):
  from app import app_services, chat, browser_access
  from app.routes import connect
  owner = _owner(db, "owner")
  grant, _ = link_grant(db, owner, "recipient")
  grant = revoke_grant(db, grant.id, owner.id)
  calls = []

  def broken_commands(grant_id):
    raise OSError("ledger unavailable")

  async def stop_calls(grant_id):
    calls.append("calls")

  async def stop_runs(grant_id, session):
    calls.append("runs")

  monkeypatch.setattr(connect, "cancel_browser_grant_commands", broken_commands)
  monkeypatch.setattr(app_services, "cancel_browser_grant_calls", stop_calls)
  monkeypatch.setattr(chat, "stop_browser_grant_runs", stop_runs)
  ended = await browser_access.end_grant(db, grant)
  assert calls == ["calls", "runs"]
  assert isinstance(ended.stop_error, OSError)
  assert ended.stop_pending


@pytest.mark.asyncio
async def test_ending_a_grant_still_records_cleanup_after_a_step_breaks_the_session(db, monkeypatch):
  from app import account_browser_access, app_services, chat, browser_access
  from app.browser_access import BrowserAccessGrant
  from app.routes import connect
  owner = _owner(db, "owner")
  grant = BrowserAccessGrant(
    id="g" * 24, owner_id=owner.id, label="Shared", kind="account",
    recipient_handle="friend", remote_status="active",
  )
  db.add(grant)
  db.commit()
  grant = revoke_grant(db, grant.id, owner.id)

  async def broken_runs(grant_id, session):
    # A failed flush leaves the session needing a rollback.
    session.add(BrowserAccessGrant(id="h" * 24, owner_id=None, label="x"))
    session.flush()

  async def unregister(session, row):
    return "revoked"

  async def no_calls(grant_id):
    return None

  monkeypatch.setattr(connect, "cancel_browser_grant_commands", lambda grant_id: [])
  monkeypatch.setattr(app_services, "cancel_browser_grant_calls", no_calls)
  monkeypatch.setattr(chat, "stop_browser_grant_runs", broken_runs)
  monkeypatch.setattr(account_browser_access, "unregister", unregister)
  ended = await browser_access.end_grant(db, grant)
  assert ended.stop_error is not None
  assert not ended.directory_cleanup_pending
  db.expire_all()
  assert db.get(BrowserAccessGrant, grant.id).remote_status == "revoked"
