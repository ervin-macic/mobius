"""Apps report their own unread count for the sidebar pill."""

from app import models
from app.auth import create_app_token
from app.broadcast import get_system_broadcast


def _app(db, slug):
  app = models.App(
    slug=slug, source_dir=f"/tmp/mobius-tests/{slug}",
    name=slug, description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _service_auth(db, app, service="private"):
  owner = db.query(models.Owner).first()
  token = create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
    service=service,
  )
  return {"Authorization": f"Bearer {token}"}


def _badge(client, auth, app_id):
  row = next(a for a in client.get("/api/apps/", headers=auth).json()
             if a["id"] == app_id)
  return row["badge_count"]


def test_app_sets_and_clears_its_own_badge_with_one_nudge_per_change(
  client, auth, db,
):
  app = _app(db, "badge-social")
  service = _service_auth(db, app)
  assert _badge(client, auth, app.id) == 0

  bus = get_system_broadcast()
  events = bus.subscribe()
  try:
    assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                      json={"count": 5}).status_code == 204
    assert events.get_nowait() == {"type": "app_activity", "appId": str(app.id)}
    # Re-reporting the same count is a no-op: no refetch storm for live shells.
    assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                      json={"count": 5}).status_code == 204
    assert events.empty()
  finally:
    bus.unsubscribe(events)

  assert _badge(client, auth, app.id) == 5
  assert client.get(f"/api/apps/{app.id}", headers=auth).json()["badge_count"] == 5

  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 0}).status_code == 204
  assert _badge(client, auth, app.id) == 0


def test_app_cannot_badge_a_sibling_and_counts_are_bounded(client, auth, db):
  mine = _app(db, "badge-mine")
  other = _app(db, "badge-other")
  service = _service_auth(db, mine)
  denied = client.put(f"/api/apps/{other.id}/badge", headers=service,
                      json={"count": 1})
  assert denied.status_code == 403
  assert client.put(f"/api/apps/{mine.id}/badge", headers=auth,
                    json={"count": -1}).status_code == 422
  assert _badge(client, auth, other.id) == 0


def test_concurrent_first_report_keeps_the_latest_count(db):
  from app import app_badge
  app = _app(db, "badge-race")
  # Simulate a peer inserting the row between this request's UPDATE and INSERT.
  original_get = db.get

  def get_then_peer_inserts(model, key):
    if model is models.AppBadgeState:
      db.get = original_get
      with db.begin_nested():
        db.add(models.AppBadgeState(app_id=app.id, count=2))
      return None
    return original_get(model, key)

  db.get = get_then_peer_inserts
  try:
    assert app_badge.set_count(db, app.id, 7) is True
  finally:
    db.get = original_get
  db.commit()
  assert db.get(models.AppBadgeState, app.id).count == 7


def test_public_service_invocation_can_badge_only_its_own_app(client, auth, db):
  # Unread items typically arrive as a public request (a peer delivering a
  # message), so the service answering it must be able to report the count.
  mine = _app(db, "badge-public")
  other = _app(db, "badge-public-other")
  public = _service_auth(db, mine, service="public")
  assert client.put(f"/api/apps/{mine.id}/badge", headers=public,
                    json={"count": 1}).status_code == 204
  assert _badge(client, auth, mine.id) == 1
  assert client.put(f"/api/apps/{other.id}/badge", headers=public,
                    json={"count": 1}).status_code == 403


def test_out_of_order_reports_cannot_overwrite_a_newer_count(client, auth, db):
  app = _app(db, "badge-order")
  service = _service_auth(db, app)
  put = lambda body: client.put(  # noqa: E731
    f"/api/apps/{app.id}/badge", headers=service, json=body,
  ).status_code

  # A delivery computed 2 at version 10; a read receipt computed 0 at 11 and
  # landed first. The late, older report must not resurrect the count.
  assert put({"count": 0, "version": 11}) == 204
  assert put({"count": 2, "version": 10}) == 204
  assert _badge(client, auth, app.id) == 0
  # An equal version is the same snapshot, not newer.
  assert put({"count": 5, "version": 11}) == 204
  assert _badge(client, auth, app.id) == 0
  assert put({"count": 3, "version": 12}) == 204
  assert _badge(client, auth, app.id) == 3
  # Clearing keeps the ordering, so a stale report cannot reappear after it.
  assert put({"count": 0, "version": 13}) == 204
  assert put({"count": 3, "version": 12}) == 204
  assert _badge(client, auth, app.id) == 0
  # An unversioned report (for example the owner) always applies and resets
  # the ordering, so an app whose versions restarted lower can recover.
  assert client.put(f"/api/apps/{app.id}/badge", headers=auth,
                    json={"count": 4}).status_code == 204
  assert _badge(client, auth, app.id) == 4
  assert put({"count": 1, "version": 1}) == 204
  assert _badge(client, auth, app.id) == 1


def test_wiping_app_data_forgets_its_badge_and_ordering(client, auth, db):
  app = _app(db, "badge-wipe")
  service = _service_auth(db, app)
  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 4, "version": 500}).status_code == 204
  assert client.delete(f"/api/apps/{app.id}/data", headers=auth).status_code in (200, 204)
  assert _badge(client, auth, app.id) == 0
  # The fresh app's first report applies even with a lower version.
  db.refresh(app)
  service = _service_auth(db, app)
  assert client.put(f"/api/apps/{app.id}/badge", headers=service,
                    json={"count": 2, "version": 3}).status_code == 204
  assert _badge(client, auth, app.id) == 2
