"""Unhandled route exceptions must leave a bounded trace agents can read.

uvicorn's own "Exception in ASGI application" report reaches only the
container's stdout. The request-error telemetry middleware, which already sees
every route exception, writes the traceback to the shared chat log once per
route and exception type per window, so a crash loop cannot flood the file.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_logging, main


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__()
    self.records: list[logging.LogRecord] = []

  def emit(self, record):
    self.records.append(record)


@pytest.fixture
def chat_log(monkeypatch):
  """Route the shared chat-log handler into memory; no database involved."""
  handler = _Capture()
  monkeypatch.setattr(chat_logging, "_chat_log_handler", handler)
  chat_logger = logging.getLogger("moebius.chat")
  monkeypatch.setattr(chat_logger, "handlers", [])
  monkeypatch.setattr(chat_logger, "level", chat_logger.level)
  return handler


def _crashing_app(monkeypatch):
  monkeypatch.setattr(main.activity, "record_request_error", lambda *a: None)
  app = FastAPI()

  @app.get("/crash/{item}")
  def crash(item: str):
    raise RuntimeError(f"boom {item}")

  @app.get("/other")
  def other():
    raise KeyError("missing")

  @app.get("/mixed/{kind}")
  def mixed(kind: str):
    raise (ValueError if kind == "value" else LookupError)(kind)

  @app.get("/cancelled")
  async def cancelled():
    raise asyncio.CancelledError()

  app.add_middleware(main._RequestErrorTelemetryMiddleware)
  return app


def _crashing_client(monkeypatch):
  return TestClient(_crashing_app(monkeypatch), raise_server_exceptions=False)


def test_route_crash_writes_its_traceback_to_the_chat_log(chat_log, monkeypatch):
  client = TestClient(_crashing_app(monkeypatch), raise_server_exceptions=True)

  # The middleware records the crash and re-raises it; it must not swallow it.
  with pytest.raises(RuntimeError, match="boom secret-name"):
    client.get("/crash/secret-name")

  [record] = chat_log.records
  assert record.levelno == logging.ERROR
  # The route template, never the raw path, identifies the failure.
  assert record.getMessage() == "unhandled exception in route GET /crash/{item}"
  assert isinstance(record.exc_info[1], RuntimeError)
  assert "boom secret-name" in chat_log.format(record)


def test_crash_burst_writes_one_traceback_per_route_and_type_per_window(
  chat_log, monkeypatch,
):
  clock = [1000.0]
  monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
  client = _crashing_client(monkeypatch)

  for i in range(50):
    client.get(f"/crash/{i}")
  client.get("/other")
  assert [type(r.exc_info[1]) for r in chat_log.records] == [
    RuntimeError, KeyError,
  ]

  clock[0] += main._RequestErrorTelemetryMiddleware._TRACEBACK_WINDOW_SEC
  client.get("/crash/again")
  assert len(chat_log.records) == 3


def test_one_route_logs_each_exception_type_separately(chat_log, monkeypatch):
  client = _crashing_client(monkeypatch)

  for kind in ("value", "lookup", "value", "lookup"):
    assert client.get(f"/mixed/{kind}").status_code == 500

  assert [type(r.exc_info[1]) for r in chat_log.records] == [
    ValueError, LookupError,
  ]
  assert {r.getMessage() for r in chat_log.records} == {
    "unhandled exception in route GET /mixed/{kind}",
  }


def test_cancelled_request_writes_nothing_to_the_chat_log(chat_log, monkeypatch):
  app = _crashing_app(monkeypatch)
  scope = {
    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
    "method": "GET", "scheme": "http", "path": "/cancelled",
    "raw_path": b"/cancelled", "root_path": "", "query_string": b"",
    "headers": [], "client": ("test", 1), "server": ("test", 80),
  }

  async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}

  async def send(message):
    pass

  # Cancellation (e.g. shutdown with a request in flight) is not a crash.
  with pytest.raises(asyncio.CancelledError):
    asyncio.run(app(scope, receive, send))
  assert chat_log.records == []
