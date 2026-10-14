"""Durable per-app unread activity derived from app-attributed notifications."""

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


class ActivityMarker(NamedTuple):
  """An unread marker written in the caller's transaction, as the list reports it."""
  app_id: int
  version: int
  # ``created_at`` names the app lifetime, alongside ``storage_generation``
  # and ``token_nonce`` elsewhere, because an id can be reused after deletion.
  # It is safe here because activity rows are deleted with their app.
  app_created_at: str


def mark_from_notification(
  db: Session, *, source_type: str, source_id: str | None,
) -> ActivityMarker | None:
  """Mark a live app unread inside the caller's notification transaction.

  Returns the written marker so the caller can publish it without a second
  read after commit. The update-first, savepoint-backed insert is race-safe
  when an existing app receives its first two notifications concurrently,
  without committing the caller's transaction.
  """
  if source_type != "app" or source_id is None:
    return None
  try:
    app_id = int(source_id)
  except (TypeError, ValueError):
    return None
  # SQLite INTEGER keys are signed 64-bit values.  Reject values outside that
  # bindable range before binding so malformed attribution cannot turn an
  # otherwise valid notification into a persistence failure.
  if app_id <= 0 or app_id > (2**63 - 1):
    return None
  # Read the stored column, not an ORM instance that may still hold an
  # unflushed timezone-aware value, so the lifetime string matches the list.
  app_created_at = db.execute(
    select(models.App.created_at).where(
      models.App.id == app_id, models.App.deleted_at.is_(None),
    )
  ).scalar_one_or_none()
  if app_created_at is None:
    return None

  now = now_naive_utc()
  bump = (
    update(models.AppActivityState)
    .where(models.AppActivityState.app_id == app_id)
    .values(
      activity_at=now,
      activity_version=models.AppActivityState.activity_version + 1,
      unseen=True,
    )
    .returning(models.AppActivityState.activity_version)
  )
  version = db.execute(bump).scalar_one_or_none()
  if version is None:
    try:
      with db.begin_nested():
        state = models.AppActivityState(app_id=app_id, activity_at=now, unseen=True)
        db.add(state)
        db.flush()
        version = state.activity_version
    except IntegrityError:
      # Another first notification inserted the singleton row after our
      # UPDATE. The savepoint kept the outer Notification insert intact; make
      # this event the winning latest marker.
      version = db.execute(bump).scalar_one()
  return ActivityMarker(app_id, version, app_created_at.isoformat())


def mark_seen(
  db: Session, app_id: int, seen_through_version: int, app_created_at: datetime,
) -> int | None:
  """Acknowledge only activity the opening shell actually observed.

  A newer notification can race the acknowledgement request. Bounding the
  update by its observed monotonic version keeps that newer event unread instead of
  letting a late acknowledgement erase it. Check the app lifetime in the same
  SQL statement, not just in the route's earlier identity read.
  """
  row = db.execute(
    update(models.AppActivityState)
    .where(
      models.AppActivityState.app_id == app_id,
      models.AppActivityState.activity_version <= seen_through_version,
      models.AppActivityState.unseen.is_(True),
      exists().where(
        models.App.id == app_id,
        models.App.created_at == app_created_at,
        models.App.deleted_at.is_(None),
      ),
    )
    .values(unseen=False)
    .returning(models.AppActivityState.activity_version)
  ).first()
  return row[0] if row else None


def annotate_apps(db: Session, apps: list[models.App]) -> list[models.App]:
  """Attach the response-only ``has_unseen_activity`` flag to app rows."""
  ids = [app.id for app in apps]
  unseen_by_id = {}
  if ids:
    unseen_by_id = {
      row.app_id: row.activity_version
      for row in db.query(
        models.AppActivityState.app_id,
        models.AppActivityState.activity_version,
      ).filter(
        models.AppActivityState.app_id.in_(ids),
        models.AppActivityState.unseen.is_(True),
      ).all()
    }
  for app in apps:
    app.has_unseen_activity = app.id in unseen_by_id
    app.unseen_activity_version = unseen_by_id.get(app.id)
  return apps
