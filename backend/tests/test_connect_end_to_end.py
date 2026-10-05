"""Disposable real-HTTP Connect contract with the stdlib runner.

No runner config/service installation is used: one in-process uvicorn server
and one explicit _serve_connection thread talk only over loopback TCP.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
import uvicorn

from app import connect_runner
from app.main import app
from app.routes import connect


def _request(base, method, path, *, token=None, body=None, timeout=4):
  data = None if body is None else json.dumps(body).encode('utf-8')
  req = urllib.request.Request(base + path, data=data, method=method)
  if data is not None:
    req.add_header('Content-Type', 'application/json')
  if token:
    req.add_header('Authorization', 'Bearer ' + token)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as response:
      return response.status, json.loads(response.read().decode('utf-8'))
  except urllib.error.HTTPError as exc:
    return exc.code, json.loads(exc.read().decode('utf-8'))


def _until(predicate, timeout=8):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    value = predicate()
    if value:
      return value
    time.sleep(0.05)
  raise AssertionError('Connect condition did not complete before deadline')


@pytest.fixture
def real_connect(auth, monkeypatch):
  # The autouse conftest owns disposable DATABASE_URL/DATA_DIR; this server
  # deliberately disables only lifespan/bootstrap, not authentication/routes.
  connect._channels.clear()
  connect._commands.clear()
  monkeypatch.setattr(connect, '_STREAM_ROTATION_SECONDS', 1.0)
  config = uvicorn.Config(
    app, host='127.0.0.1', port=0, lifespan='off',
    log_level='error', access_log=False,
  )
  server = uvicorn.Server(config)
  thread = threading.Thread(target=server.run, daemon=True, name='connect-test-http')
  thread.start()
  try:
    _until(lambda: server.started and server.servers, timeout=5)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    owner = auth['Authorization'].split(' ', 1)[1]
    status, created = _request(base, 'POST', '/api/connect/hosts',
                               token=owner, body={'name': 'Disposable runner'})
    assert status == 200, created
    status, paired = _request(base, 'POST', '/api/connect/pair',
                              body={'code': created['pairing_code']})
    assert status == 200, paired
    host_id = created['id']
    stop = threading.Event()
    runner = threading.Thread(
      target=connect_runner._serve_connection,
      args=({'url': base, 'host_id': host_id, 'token': paired['token']}, stop),
      daemon=True, name='connect-test-runner',
    )
    runner.start()
    _until(lambda: _request(base, 'GET', '/api/connect/hosts', token=owner)[1]
           ['hosts'][0]['online'], timeout=8)
    yield base, owner, host_id, runner
  finally:
    if 'stop' in locals():
      stop.set()
    server.should_exit = True
    server.force_exit = True
    thread.join(timeout=4)
    if 'runner' in locals():
      runner.join(timeout=4)
    assert not thread.is_alive(), 'test HTTP server did not stop'
    assert 'runner' not in locals() or not runner.is_alive(), 'runner did not stop'
    connect._channels.clear()
    connect._commands.clear()


def _exec(base, owner, host_id, *, request_id, **work):
  return _request(
    base, 'POST', f'/api/connect/hosts/{host_id}/exec', token=owner,
    body={'request_id': request_id, 'stream': True,
          'admission_deadline': time.time() + 10, **work}, timeout=12,
  )


def _output(base, owner, host_id, request_id, after=0):
  return _request(
    base, 'GET',
    f'/api/connect/hosts/{host_id}/commands/{request_id}/output?after={after}&wait=0',
    token=owner,
  )


@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell/process contract')
def test_real_runner_full_unicode_output_pages_and_rotated_retry(real_connect, tmp_path):
  base, owner, host_id, _runner = real_connect
  marker = tmp_path / 'runs.txt'
  expected = 'Ω' + 'x' * 1_100_000 + '終'
  script = (
    "python3 - <<'PY'\n"
    "from pathlib import Path\n"
    f"Path({str(marker)!r}).open('a').write('one\\n')\n"
    "import sys\n"
    "sys.stdout.write('Ω' + 'x' * 1100000 + '終')\n"
    "PY\n"
  )
  rid = uuid.uuid4().hex
  admission_deadline = time.time() + 10
  first_channel = connect._channels[host_id]
  status, started = _exec(base, owner, host_id, request_id=rid,
                          script=script, timeout=15,
                          admission_deadline=admission_deadline)
  assert status == 200, started
  assert started['request_id'] == rid
  chunks = []
  pages = 0
  cursor = 0
  deadline = time.monotonic() + 15
  terminal = None
  while time.monotonic() < deadline:
    status, page = _output(base, owner, host_id, rid, cursor)
    assert status == 200, page
    pages += 1
    chunks.extend(page['chunks'])
    assert page['next'] >= cursor
    cursor = page['next']
    terminal = page['result']
    if terminal is not None and page['output_complete'] and not page['has_more']:
      break
    time.sleep(0.05)
  assert terminal is not None and page['output_complete'], page
  assert terminal['exit_code'] == 0 and terminal['outcome'] == 'completed'
  assert terminal['truncated'] is True
  assert len(terminal['stdout']) <= 60_000
  assert terminal['stdout'].startswith('Ω') and terminal['stdout'].endswith('終')
  assert pages >= 3
  assert ''.join(c['text'] for c in chunks if c['stream'] == 'stdout') == expected
  assert cursor == page['output_seq'] == page['available_next']
  _until(lambda: connect._channels.get(host_id) is not None
         and connect._channels[host_id] is not first_channel, timeout=6)
  status, retried = _exec(base, owner, host_id, request_id=rid,
                          script=script, timeout=15,
                          admission_deadline=admission_deadline)
  assert status == 200 and retried['state'] == 'finished', retried
  assert marker.read_text() == 'one\n'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process cancellation')
def test_real_runner_parallel_quick_and_exact_cancel(real_connect):
  base, owner, host_id, _runner = real_connect
  slow_id = uuid.uuid4().hex
  quick_id = uuid.uuid4().hex
  status, slow = _exec(base, owner, host_id, request_id=slow_id,
                       cmd='sleep 20; echo TOO_LATE', timeout=25)
  assert status == 200 and slow['request_id'] == slow_id, slow
  status, quick = _exec(base, owner, host_id, request_id=quick_id,
                        cmd='printf quick', timeout=10)
  assert status == 200 and quick['request_id'] == quick_id, quick
  status, canceled = _request(
    base, 'POST', f'/api/connect/hosts/{host_id}/commands/{slow_id}/cancel',
    token=owner, body={})
  assert status == 200 and canceled['request_id'] == slow_id, canceled
  def finished(rid):
    status, page = _output(base, owner, host_id, rid)
    assert status == 200, page
    return page['result']
  quick_result = _until(lambda: finished(quick_id), timeout=8)
  slow_result = _until(lambda: finished(slow_id), timeout=8)
  assert quick_result['exit_code'] == 0
  assert quick_result['stdout'] == 'quick'
  assert slow_result['outcome'] == 'canceled'
  assert 'TOO_LATE' not in slow_result['stdout']
