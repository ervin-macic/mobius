"""App-reported unread counts shown as a pill on the app's sidebar row."""

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models

# The pill renders "99+" past 99; this only bounds what an app may store.
MAX_BADGE_COUNT = 1_000_000
MAX_BADGE_VERSION = 2**63 - 1


def set_count(
  db: Session, app_id: int, count: int, version: int | None = None,
) -> bool:
  """Record the app's badge inside the caller's transaction.

  ``version`` orders concurrent reports: a value that only increases (for
  example a nanosecond timestamp taken before counting). A report not newer
  than the stored one is ignored, so reports landing out of order cannot leave
  a stale count. An unversioned report (the owner, or an app that does not
  order its reports) always applies and resets the ordering, which is also the
  recovery path if an app's versions ever restart lower. Returns whether the
  shown count changed, so callers only nudge live shells for real changes.
  """
  state = models.AppBadgeState
  row = db.get(state, app_id)
  if row is None:
    try:
      with db.begin_nested():
        db.add(state(app_id=app_id, count=count, version=version))
        db.flush()
      return count > 0
    except IntegrityError:
      # A concurrent first report created the row; order against it below.
      row = db.get(state, app_id)
  previous = row.count
  newer = True if version is None else or_(
    state.version.is_(None), state.version < version,
  )
  values = {"count": count, "version": version}
  applied = db.execute(
    update(state).where(state.app_id == app_id, newer).values(**values)
  ).rowcount
  return bool(applied) and previous != count


def clear(db: Session, app_id: int) -> None:
  """Forget the app's badge, e.g. when its data is wiped."""
  db.query(models.AppBadgeState).filter(
    models.AppBadgeState.app_id == app_id,
  ).delete(synchronize_session=False)


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach the response-only ``badge_count`` to app rows."""
  ids = [app.id for app in apps]
  counts = {}
  if ids:
    counts = dict(
      db.query(models.AppBadgeState.app_id, models.AppBadgeState.count)
      .filter(models.AppBadgeState.app_id.in_(ids))
      .all()
    )
  for app in apps:
    app.badge_count = counts.get(app.id, 0)
  return apps
