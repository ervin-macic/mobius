"""Durable, host-isolated Connect output and terminal identity."""
import asyncio
import time

import pytest

from app import connect_output
from app.routes import connect


@pytest.fixture(autouse=True)
def clear_connect_state():
  connect._pair_limiter.reset()
  connect._channels.clear()
  connect._commands.clear()
  yield
  connect._channels.clear()
  connect._commands.clear()


def host(client, auth):
  created = client.post('/api/connect/hosts', headers=auth, json={'name': 'Box'}).json()
  paired = client.post('/api/connect/pair', json={'code': created['pairing_code']}).json()
  ch = connect._Channel()
  connect._channels[created['id']] = ch
  return created['id'], {'Authorization': 'Bearer ' + paired['token']}, ch


@pytest.mark.asyncio
async def test_full_output_survives_memory_reset_and_pages(client, auth):
  host_id, token, ch = host(client, auth)
  rid = 'a' * 16
  task = asyncio.create_task(connect.exec_on_host(
    host_id, connect.ExecBody(cmd='echo yes', request_id=rid, stream=True),
    _owner=object()))
  await ch.queue.get()
  connect._mark_command_started(host_id, rid)
  await task
  chunks = [{'seq': i, 'stream': 'stdout', 'text': 'x' * 1000}
            for i in range(600)]
  r = client.post('/api/connect/output', headers=token,
                  json={'request_id': rid, 'chunks': chunks})
  assert r.status_code == 200, r.text
  assert r.json()['next'] == 600
  connect._runner_result(host_id, connect.ResultBody(
    request_id=rid, stdout='head', output_seq=600))
  connect._commands.clear()
  first = client.get(f'/api/connect/hosts/{host_id}/commands/{rid}/output',
                     headers=auth).json()
  assert len(first['chunks']) < 600
  assert first['next'] < first['available_next'] == 600
  assert first['output_complete'] is True
  second = client.get(f'/api/connect/hosts/{host_id}/commands/{rid}/output',
                      headers=auth, params={'after': first['next']}).json()
  assert len(first['chunks']) + len(second['chunks']) == 600
  assert second['next'] == 600
  assert first['result']['stdout'] == 'head'


@pytest.mark.asyncio
async def test_late_upload_and_archived_retry(client, auth):
  host_id, token, ch = host(client, auth)
  rid = 'b' * 16
  task = asyncio.create_task(connect.exec_on_host(
    host_id, connect.ExecBody(cmd='true', request_id=rid, stream=True),
    _owner=object()))
  await ch.queue.get()
  connect._mark_command_started(host_id, rid)
  await task
  connect._runner_result(host_id, connect.ResultBody(
    request_id=rid, output_seq=2))
  endpoint = f'/api/connect/hosts/{host_id}/commands/{rid}/output'
  assert client.get(endpoint, headers=auth).json()['output_complete'] is False
  r = client.post('/api/connect/output', headers=token, json={
    'request_id': rid, 'chunks': [
      {'seq': 0, 'stream': 'stdout', 'text': 'a'},
      {'seq': 1, 'stream': 'stdout', 'text': 'b'}]})
  assert r.status_code == 200 and r.json()['next'] == 2
  assert client.get(endpoint, headers=auth).json()['output_complete'] is True
  connect._channels.clear()
  retry = await connect.exec_on_host(
    host_id, connect.ExecBody(cmd='true', request_id=rid, stream=True),
    _owner=object())
  assert retry['state'] == 'finished'
  with pytest.raises(connect.HTTPException) as conflict:
    await connect.exec_on_host(host_id, connect.ExecBody(
      cmd='false', request_id=rid, stream=True), _owner=object())
  assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_expired_unseen_admission_never_enqueues(client, auth):
  host_id, _token, ch = host(client, auth)
  err = await connect.exec_on_host(host_id, connect.ExecBody(
    cmd='true', request_id='c' * 16, stream=True,
    admission_deadline=time.time() - 1), _owner=object())
  assert err.status_code == 504
  assert b'command_expired' in err.body
  assert ch.queue.empty()


def test_ledger_is_host_scoped_and_exact_seq(tmp_path, monkeypatch):
  class Settings:
    data_dir = str(tmp_path)
  monkeypatch.setattr(connect_output, 'get_settings', lambda: Settings())
  a, b = 'h_' + 'a' * 16, 'h_' + 'b' * 16
  rid = 'd' * 16
  connect_output.append(a, rid, [{'seq': 0, 'stream': 'stdout', 'text': 'first'}])
  connect_output.append(a, rid, [{'seq': 0, 'stream': 'stdout', 'text': 'first'}])
  with pytest.raises(ValueError):
    connect_output.append(a, rid, [{'seq': 0, 'stream': 'stdout', 'text': 'changed'}])
  assert connect_output.view(a, rid, 0)[1]['chunks'][0]['text'] == 'first'
  assert connect_output.view(b, rid, 0)[1]['chunks'] == []
  assert (tmp_path / 'shared/connect/output' / f'{a}.sqlite3').stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_unknown_cross_host_and_disk_failure_do_not_ack(client, auth, monkeypatch):
  host_a, token_a, _ = host(client, auth)
  host_b, token_b, _ = host(client, auth)
  rid = 'e' * 16
  payload = {'request_id': rid, 'chunks': [
    {'seq': 0, 'stream': 'stdout', 'text': 'secret'}]}
  # Host authentication is scoped to its own known command. No arbitrary
  # upload can create an output ledger entry.
  assert client.post('/api/connect/output', headers=token_b,
                     json=payload).status_code == 409
  command = connect._ActiveCommand(rid, 60, cmd='true')
  connect._commands[host_a] = {rid: command}
  monkeypatch.setattr(connect_output, 'append', lambda *args: (_ for _ in ()).throw(OSError('full')))
  failed = client.post('/api/connect/output', headers=token_a, json=payload)
  assert failed.status_code == 503
  assert connect_output.view(host_a, rid, 0)[1]['chunks'] == []


@pytest.mark.asyncio
async def test_recent_history_survives_without_storing_command_text(client, auth):
  host_id, _token, ch = host(client, auth)
  rid = 'f' * 16
  task = asyncio.create_task(connect.exec_on_host(
    host_id, connect.ExecBody(cmd='echo private-token', request_id=rid, stream=True),
    _owner=object()))
  await ch.queue.get()
  connect._mark_command_started(host_id, rid)
  await task
  connect._runner_result(host_id, connect.ResultBody(request_id=rid))
  host_view = connect._public_host(connect._load_host(host_id))
  listed = await connect.list_host_commands(host_id, _owner=object())
  assert [item['id'] for item in host_view['recent_commands']] == [rid]
  assert listed['recent'] == host_view['recent_commands']
  assert host_view['recent_commands'][0]['label'] is None
  db_bytes = connect_output._path(host_id).read_bytes()
  assert b'private-token' not in db_bytes
  assert b'private-token' not in connect._host_path(host_id).read_bytes()


def test_conflicting_seq_replay_is_rejected_without_partial_append(tmp_path, monkeypatch):
  class Settings:
    data_dir = str(tmp_path)
  monkeypatch.setattr(connect_output, 'get_settings', lambda: Settings())
  host_id = 'h_' + 'a' * 16
  rid = 'c' * 16
  connect_output.append(host_id, rid, [
    {'seq': 0, 'stream': 'stdout', 'text': 'first'}])
  with pytest.raises(ValueError):
    connect_output.append(host_id, rid, [
      {'seq': 1, 'stream': 'stdout', 'text': 'later'},
      {'seq': 0, 'stream': 'stdout', 'text': 'changed'}])
  assert [c['seq'] for c in connect_output.view(host_id, rid, 0)[1]['chunks']] == [0]


@pytest.mark.asyncio
async def test_ledger_read_failure_is_explicit_503(client, auth, monkeypatch):
  import sqlite3
  host_id, _token, ch = host(client, auth)
  rid = '1' * 16
  task = asyncio.create_task(connect.exec_on_host(
    host_id, connect.ExecBody(cmd='true', request_id=rid, stream=True),
    _owner=object()))
  await ch.queue.get()
  connect._mark_command_started(host_id, rid)
  await task
  monkeypatch.setattr(connect_output, 'view',
                      lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('disk')))
  response = client.get(f'/api/connect/hosts/{host_id}/commands/{rid}/output',
                        headers=auth)
  assert response.status_code == 503
  assert response.json()['detail'] == 'Connect history is unavailable.'


def test_output_page_does_not_materialize_unread_text(monkeypatch):
  loaded=[]
  class Max:
    def fetchone(self):return (4095,)
  class DB:
    def execute(self,query,params):
      if 'MAX(seq)' in query:return Max()
      def rows():
        for seq in range(4096):
          loaded.append(seq)
          yield (seq,'stdout','x'*65536)
      return rows()
    def close(self):pass
  page=connect_output._page(DB(),'f'*16,0)
  assert len(loaded)<=9
  assert sum(len(c['text']) for c in page['chunks'])<=connect_output.PAGE_CHARS
  assert page['has_more'] and page['next']<page['available_next']


def test_reading_history_creates_no_empty_ledger(client, auth):
  host_id, _token, _ch = host(client, auth)
  assert client.get('/api/connect/hosts', headers=auth).status_code == 200
  listed = client.get(f'/api/connect/hosts/{host_id}/commands', headers=auth)
  assert listed.json() == {'running': [], 'recent': []}
  assert not connect_output._path(host_id).exists()


def test_reader_treats_a_not_yet_initialized_ledger_as_empty(tmp_path, monkeypatch):
  # A writer creates the private file before its schema commits; a concurrent
  # history read in that window must not fail the whole host listing.
  class Settings:
    data_dir = str(tmp_path)
  monkeypatch.setattr(connect_output, 'get_settings', lambda: Settings())
  host_id = 'h_' + 'c' * 16
  connect_output._create_private(connect_output._path(host_id))
  assert connect_output.recent(host_id) == []
  assert connect_output.finished(host_id, 'e' * 16) is None
  assert connect_output.view(host_id, 'e' * 16, 0)[0] is None
