"""Account sign-in must not outrun revocation or exchange proof for owner authority."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import account_browser_access as account, browser_access as access, models
from app.config import get_settings

ROOT = '/api/connect/browser-access'


@pytest.fixture
def account_flow(client, db, auth, monkeypatch):
    from app.routes.browser_access import _limiter
    _limiter.reset()
    monkeypatch.setattr(get_settings(), 'frontend_origin', 'https://shared.example')
    monkeypatch.setattr(get_settings(), 'mobius_account_origin', 'https://identity.example')
    client.base_url = 'https://shared.example'
    client.headers['Origin'] = 'https://shared.example'
    owner = db.query(models.Owner).one()
    db.add(models.IdentityAccountLink(owner_id=owner.id, access_token_encrypted='synthetic-link-generation', scopes_json=[]))
    db.commit()
    grant = access.BrowserAccessGrant(
        id='account-grant-fixture-0001', owner_id=owner.id, label='Sam',
        kind='account', issuer='https://identity.example', subject='user-sam',
        grantor_binding=access.account_binding(db, owner.id),
        recipient_handle='sam-person', origin='https://shared.example', remote_status='active',
    )
    db.add(grant)
    db.commit()
    started = client.get(ROOT + '/session/account/start', params={'grant_id': grant.id}, follow_redirects=False)
    assert started.status_code == 303, started.text
    query = {key: values[0] for key, values in parse_qs(urlsplit(started.headers['location']).query).items()}
    proof = {
        'iss': grant.issuer, 'sub': grant.subject, 'aud': 'mobius-shared-browser',
        'origin': grant.origin, 'grant_id': grant.id, 'nonce': query['nonce'],
        'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    }
    original = httpx.AsyncClient

    def issuer_response(body=None, *, before_response=None, status=200):
        def handle(request):
            assert str(request.url) == 'https://identity.example/shared-access/token'
            assert 'authorization' not in request.headers
            if before_response:
                before_response()
            return httpx.Response(status, json=body if body is not None else proof)
        monkeypatch.setattr(account.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))

    def callback():
        return client.get(ROOT + '/session/account/callback', params={
            'state': query['state'], 'code': 'synthetic-code-for-isolated-test',
        }, follow_redirects=False)

    return client, owner, grant, proof, issuer_response, callback


@pytest.mark.parametrize('field,wrong', [
    ('iss', 'https://another-identity.example'), ('sub', 'different-person'),
    ('aud', 'mobius-runtime-enroll'), ('origin', 'https://other-instance.example'),
    ('grant_id', 'another-grant'), ('nonce', 'another-flow'),
    ('expires_at', '2001-01-01T00:00:00Z'), ('expires_at', '2099-01-01T00:00:00Z'),
    ('expires_at', None), ('expires_at', 'invalid'),
])
def test_wrong_proof_never_creates_a_browser_session(account_flow, db, field, wrong):
    _, _, _, proof, respond, callback = account_flow
    respond({**proof, field: wrong})
    result = callback()
    assert result.status_code == 401, result.text
    assert db.query(access.BrowserAccessSession).count() == 0


@pytest.mark.parametrize('change', ['grant', 'owner_epoch'])
def test_revocation_while_issuer_exchange_is_in_flight_wins(account_flow, db, change):
    _, owner, grant, _, respond, callback = account_flow
    def revoke_during_exchange():
        if change == 'grant':
            access.revoke_grant(db, grant.id, owner.id)
        else:
            owner.token_epoch += 1
            db.commit()
    respond(before_response=revoke_during_exchange)
    result = callback()
    if result.status_code == 303:
        client = account_flow[0]
        result = client.post(ROOT + '/session/account/finalize', json={'pending_id': result.headers['location'].split('=', 1)[1]})
    assert result.status_code == 401, result.text
    assert db.query(access.BrowserAccessSession).count() == 0


def test_code_proof_creates_only_attributed_session_and_cannot_replay(account_flow, db, auth):
    client, owner, grant, _, respond, callback = account_flow
    respond()
    result = callback()
    assert result.status_code == 303, result.text
    assert result.headers['location'].startswith('/shell/shared#account-finalize=')
    pending_id = result.headers['location'].split('=', 1)[1]
    final = client.post(ROOT + '/session/account/finalize', json={'pending_id': pending_id})
    assert final.status_code == 200, final.text
    session = client.post(ROOT + '/session')
    assert session.status_code == 200, session.text
    from app.auth import decode_access_token
    token = session.json()['access_token']
    claims = decode_access_token(token)
    assert claims['browser_grant'] == grant.id
    assert claims['browser_session']
    guest = {'Authorization': 'Bearer ' + token}
    assert client.post(ROOT + '/accounts', json={'recipient_handle': 'another-person'}, headers=guest).status_code == 403
    assert callback().status_code == 401
    assert db.query(access.BrowserAccessSession).count() == 1
    access.revoke_grant(db, grant.id, owner.id)
    assert client.post(ROOT + '/session').status_code == 401
    assert client.get('/api/chats', headers=guest).status_code == 401
    assert client.get('/api/chats', headers=auth).status_code == 200


def test_account_recipient_handle_uses_existing_issuer_hyphen_contract(client, db, auth, monkeypatch):
    from app.routes import browser_access as routes
    monkeypatch.setattr(get_settings(), 'frontend_origin', 'https://shared.example')
    async def remote(db, owner_id, method, suffix, payload=None):
        return httpx.Response(200, json={
            'issuer': access.issuer_origin(), 'subject': 'stable-user',
            'handle': payload['recipient_handle'], 'grant_id': payload['grant_id'],
            'origin': 'https://shared.example',
        })
    monkeypatch.setattr(account, 'issuer_request', remote)
    owner = db.query(models.Owner).one()
    db.add(models.IdentityAccountLink(owner_id=owner.id, access_token_encrypted='synthetic-link-generation', scopes_json=[]))
    db.commit()
    accepted = client.post(ROOT + '/accounts', headers=auth, json={'recipient_handle': 'sam-person'})
    assert accepted.status_code == 200, accepted.text
    refused = client.post(ROOT + '/accounts', headers=auth, json={'recipient_handle': 'sam_person'})
    assert refused.status_code == 422, refused.text


def test_cross_site_callback_defers_cookie_switch_until_same_origin_finalization(account_flow, db):
    client, owner, _, _, respond, callback = account_flow
    previous_grant, invitation = access.create_invitation(db, owner, 'Previous person')
    previous_secret, previous_session, _, _ = access.redeem_invitation(db, invitation)
    # Cross-site callback deliberately lacks the old SameSite=Strict cookie.
    respond()
    verified = callback()
    assert verified.status_code == 303, verified.text
    assert 'mobius_shared_browser=' not in verified.headers.get('set-cookie', '')
    assert access.is_live(db, access.BrowserLineage(previous_grant.id, previous_session.id), owner.id)
    pending_id = verified.headers['location'].split('=', 1)[1]
    client.cookies.set('mobius_shared_browser', previous_secret,
        domain='shared.example', path=ROOT + '/session')
    wrong_tab = client.post(ROOT + '/session/account/finalize', json={'pending_id': 'another-tab-pending-id-0000'})
    assert wrong_tab.status_code == 401
    assert 'set-cookie' not in wrong_tab.headers
    foreign_origin = client.post(ROOT + '/session/account/finalize',
        json={'pending_id': pending_id}, headers={'Origin': 'https://identity.example'})
    assert foreign_origin.status_code == 403
    final = client.post(ROOT + '/session/account/finalize', json={'pending_id': pending_id})
    assert final.status_code == 200, final.text
    assert not access.is_live(db, access.BrowserLineage(previous_grant.id, previous_session.id), owner.id)
    assert client.post(ROOT + '/session/account/finalize', json={'pending_id': pending_id}).status_code == 401


def test_verified_flow_cannot_finalize_after_local_grant_revocation(account_flow, db):
    client, owner, grant, _, respond, callback = account_flow
    respond()
    result = callback()
    assert result.status_code == 303, result.text
    pending_id = result.headers['location'].split('=', 1)[1]
    access.revoke_grant(db, grant.id, owner.id)
    final = client.post(ROOT + '/session/account/finalize', json={'pending_id': pending_id})
    assert final.status_code == 401
    assert db.query(access.BrowserAccessSession).count() == 0


@pytest.mark.parametrize("directory_status", [204, 401])
def test_unlink_retains_retry_until_remote_command_stop_is_confirmed(client, auth, db, monkeypatch, directory_status):
    from app import models, browser_access as access
    from app.config import get_settings
    from app.routes import browser_access as routes, connect
    monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
    client.base_url = "https://shared.example"
    client.headers["Origin"] = "https://shared.example"
    owner = db.query(models.Owner).one()
    db.add(models.IdentityAccountLink(owner_id=owner.id,
        access_token_encrypted="fixture-only", scopes_json=[]))
    db.commit()
    grant = access.BrowserAccessGrant(id="unconfirmed-stop-grant", owner_id=owner.id,
        label="Test", kind="account", remote_status="active")
    db.add(grant)
    db.commit()
    async def cleanup(*args, **kwargs):
        return httpx.Response(directory_status)
    monkeypatch.setattr(account, "issuer_request", cleanup)
    monkeypatch.setattr(connect, "cancel_browser_grant_commands",
        lambda grant_id: [{"request_id": "pending-test-command", "remote_confirmed": False}])
    response = client.delete("/api/identity/link", headers=auth)
    assert response.status_code == 502
    assert "cleanup is pending" in response.json()["detail"]
    db.refresh(grant)
    assert grant.revoked_at is not None
    assert grant.remote_status == ("revoked" if directory_status == 204 else "cleanup_pending")
    assert db.get(models.IdentityAccountLink, owner.id) is not None


def test_account_migration_preserves_legacy_grant_and_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from app.schema_migrations import _add_browser_account_grants
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE browser_access_grants (id VARCHAR(64) PRIMARY KEY, label TEXT, epoch INTEGER)"))
        conn.execute(text("INSERT INTO browser_access_grants VALUES ('legacy', 'Existing recipient', 4)"))
    _add_browser_account_grants(engine)
    _add_browser_account_grants(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, label, epoch, kind, issuer, subject FROM browser_access_grants")).one()
        assert tuple(row) == ('legacy', 'Existing recipient', 4, 'invitation', None, None)
    pending = {c['name'] for c in inspect(engine).get_columns('browser_account_pending')}
    assert pending == set(access.BrowserAccountPending.__table__.columns.keys())
    engine.dispose()
