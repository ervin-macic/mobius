"""Healthy startup advises away clean pages of the main SQLite file."""

import fcntl
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app import database, file_cache
from app.runtime_supervisors import RuntimeSupervisors


def test_startup_advice_uses_exact_main_filename_without_connecting(tmp_path, monkeypatch):
  path = tmp_path / "main with spaces and %20.db"
  engine = create_engine(f"sqlite:///{path}")
  calls = []
  monkeypatch.setattr(database, "engine", engine)
  monkeypatch.setattr(
    file_cache, "reclaim_file_cache",
    lambda paths: calls.append(paths) or {"files": 1},
  )
  try:
    with engine.begin() as connection:
      connection.exec_driver_sql("CREATE TABLE example (value TEXT)")
      connection.exec_driver_sql(
        "ATTACH DATABASE ? AS extra", (str(tmp_path / "attached.db"),),
      )
    monkeypatch.setattr(
      engine, "connect", lambda: pytest.fail("cache advice must not open SQLite"),
    )
    assert database.reclaim_startup_database_file_cache() == {"files": 1}
    assert calls == [[str(path)]]
  finally:
    engine.dispose()


@pytest.mark.parametrize("url", [
  "sqlite://", "sqlite:///:memory:",
  "sqlite:///file:startup-memory?mode=memory&cache=shared&uri=true",
  "sqlite:///relative.db",
  "sqlite:///file:/absolute.db?mode=ro&uri=true",
])
def test_ambiguous_or_memory_databases_are_not_file_cache_targets(monkeypatch, url):
  engine = create_engine(url)
  monkeypatch.setattr(database, "engine", engine)
  monkeypatch.setattr(
    engine, "connect", lambda: pytest.fail("cache advice must not open SQLite"),
  )
  monkeypatch.setattr(
    file_cache, "reclaim_file_cache",
    lambda _: pytest.fail("ambiguous database must not produce a file target"),
  )
  try:
    assert database.reclaim_startup_database_file_cache() is None
  finally:
    engine.dispose()


def test_non_sqlite_database_is_not_opened_for_cache_advice(monkeypatch):
  monkeypatch.setattr(database, "engine", SimpleNamespace(
    dialect=SimpleNamespace(name="postgresql"),
    connect=lambda: pytest.fail("must not connect to a remote database"),
  ))
  assert database.reclaim_startup_database_file_cache() is None


def test_advice_is_read_only_and_preserves_wal_contents_and_cache_policy(
  tmp_path, monkeypatch,
):
  path = tmp_path / "main.db"
  engine = create_engine(f"sqlite:///{path}")
  monkeypatch.setattr(database, "engine", engine)
  advised = []

  def advise(fd, offset, length, advice):
    assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    assert (offset, length, advice) == (0, 0, os.POSIX_FADV_DONTNEED)
    advised.append(os.fstat(fd).st_ino)

  monkeypatch.setattr(os, "posix_fadvise", advise)
  try:
    with engine.connect() as keeper:
      assert keeper.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
      keeper.exec_driver_sql("CREATE TABLE example (value TEXT)")
      keeper.exec_driver_sql("INSERT INTO example VALUES ('retained')")
      keeper.commit()
      wal = Path(str(path) + "-wal")
      before = path.read_bytes(), wal.read_bytes()
      cache_size = keeper.exec_driver_sql("PRAGMA cache_size").scalar()
      result = database.reclaim_startup_database_file_cache()
      assert result["files"] == 1
      assert advised == [path.stat().st_ino]
      assert (path.read_bytes(), wal.read_bytes()) == before
      assert keeper.exec_driver_sql("PRAGMA cache_size").scalar() == cache_size
      assert keeper.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
      assert keeper.exec_driver_sql("SELECT value FROM example").scalar() == "retained"
  finally:
    engine.dispose()


def supervisors():
  return RuntimeSupervisors(
    settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.startup-cache"),
    restart_authorization=None, restart_fallback_chats=[],
  )


@pytest.mark.asyncio
async def test_database_cache_advice_runs_off_loop_at_boot(monkeypatch, caplog):
  owner = supervisors()
  loop_thread = threading.get_ident()
  calls = []
  monkeypatch.setattr(file_cache, "reclaim_background_work_cache", lambda _: None)

  def advise():
    calls.append(threading.get_ident())
    return {
      "files": 1, "advised_file_bytes": 4096,
      "file_cache_before_bytes": 9000, "file_cache_after_bytes": 5000,
    }

  monkeypatch.setattr(database, "reclaim_startup_database_file_cache", advise)
  caplog.set_level(logging.INFO, logger="test.startup-cache")
  owner.reclaim_boot_file_cache()
  await owner._tasks["boot-file-cache-reclaim"]
  assert len(calls) == 1 and calls[0] != loop_thread
  [line] = [r.getMessage() for r in caplog.records if "database file cache" in r.getMessage()]
  assert "advised_bytes=4096" in line
  assert "cgroup_file_before=9000" in line and "cgroup_file_after=5000" in line
  assert "reclaimed" not in line
  await owner.stop()


@pytest.mark.asyncio
async def test_optional_cache_advice_failures_stay_inside_the_boot_task(monkeypatch):
  owner = supervisors()
  calls = []

  def fail_tools(_):
    raise OSError("tools unavailable")

  def fail_database():
    calls.append("attempted")
    raise OSError("optional advice unavailable")

  monkeypatch.setattr(file_cache, "reclaim_background_work_cache", fail_tools)
  monkeypatch.setattr(database, "reclaim_startup_database_file_cache", fail_database)
  owner.reclaim_boot_file_cache()
  task = owner._tasks["boot-file-cache-reclaim"]
  await task
  assert calls == ["attempted"]
  assert task.exception() is None
  await owner.stop()
