"""Focused transport, lifecycle, and durable-output runner regressions."""
from contextlib import closing
import http.client
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse

import pytest

from app import connect_runner as runner


def _run_inline(command, monkeypatch):
    finished = threading.Event()
    results = []
    def post(url, payload, **_kwargs):
        if url.endswith('/result'):
            results.append(payload)
            finished.set()
        return {'ok': True}
    monkeypatch.setattr(runner, '_post', post)
    command_runner = runner._CommandRunner('https://example.test', 'token')
    command_runner.start({'request_id': 'inline-test', 'cmd': command,
                          'timeout': 5, 'not_after': time.time() + 10})
    assert finished.wait(10)
    return results[0]


def test_post_wraps_raw_incomplete_read(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            raise http.client.IncompleteRead(b'part')
    monkeypatch.setattr(runner, '_open_url', lambda *a, **k: Response())
    with pytest.raises(urllib.error.URLError):
        runner._post('https://example.test/api', {})


@pytest.mark.parametrize('error', [
    http.client.IncompleteRead(b'part'),
    urllib.error.URLError('transport lost'),
])
def test_stream_read_failure_reconnects_instead_of_crashing(monkeypatch, capsys, error):
    stopped = threading.Event()
    attempts = []
    delays = []

    class BrokenStream:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def __iter__(self):
            raise error

    def open_stream(*_args):
        attempts.append(1)
        if len(attempts) == 1:
            return BrokenStream()
        stopped.set()
        return io.BytesIO()

    monkeypatch.setattr(runner, '_open_stream', open_stream)
    monkeypatch.setattr(runner.time, 'sleep', delays.append)
    runner._serve_connection({'url': 'https://example.test', 'token': 'test'}, stopped)

    assert len(attempts) == 2
    assert delays == [1]
    assert 'connection lost (' in capsys.readouterr().out


def test_runner_logs_command_identity_without_copying_command_text(monkeypatch, capsys):
    stopped = threading.Event()
    event = {'type': 'exec', 'request_id': 'a' * 16,
             'cmd': 'private-command-text-' + 'x' * 100_000}
    stream = io.BytesIO(('data: ' + json.dumps(event) + '\n').encode())
    accepted = []

    def start(_runner, work):
        accepted.append(work)
        stopped.set()

    monkeypatch.setattr(runner, '_open_stream', lambda *_args: stream)
    monkeypatch.setattr(runner._CommandRunner, 'start', start)
    runner._serve_connection({'url': 'https://example.test', 'token': 'test'}, stopped)

    assert accepted == [event]
    logged = capsys.readouterr().out
    assert 'Starting command ' + event['request_id'] in logged
    assert 'private-command-text' not in logged
    assert len(logged) < 500


def test_stream_inventory_uses_post_body_and_small_metadata_query(monkeypatch):
    calls = []
    context = object()
    sentinel = object()
    def opened(request, **kwargs):
        calls.append((request, kwargs))
        return sentinel
    monkeypatch.setattr(runner, '_open_url', opened)
    active = ['a' * 64 for _ in range(300)]
    pending = ['p' * 64 for _ in range(300)]
    metadata = [('protocol', '4'), ('release', '5'), ('capability', 'inventory_body')]
    assert runner._open_stream('https://example.test', 'token', metadata,
                               active, pending, context) is sentinel
    [(request, kwargs)] = calls
    assert request.get_method() == 'POST'
    assert len(request.full_url) < 300
    assert 'active_request_id=' not in request.full_url
    assert json.loads(request.data) == {
        'active_request_ids': active, 'pending_result_ids': pending,
    }
    assert request.get_header('Authorization') == 'Bearer token'
    assert request.get_header('Accept') == 'text/event-stream'
    assert kwargs['context'] is context
    assert kwargs['timeout'] == runner.STREAM_READ_TIMEOUT_SECONDS


@pytest.mark.parametrize('status', [404, 405])
def test_stream_inventory_falls_back_only_for_missing_post_route(monkeypatch, status):
    calls = []
    sentinel = object()
    def opened(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, status, 'old', {}, io.BytesIO())
        return sentinel
    monkeypatch.setattr(runner, '_open_url', opened)
    assert runner._open_stream('https://example.test', 'token', [('protocol', '4')],
                               ['one'], ['two'], object()) is sentinel
    assert [request.get_method() for request in calls] == ['POST', 'GET']
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1].full_url).query) == {
        'protocol': ['4'], 'active_request_id': ['one'],
        'pending_result_id': ['two'],
    }
    assert calls[1].get_header('Authorization') == 'Bearer token'


def test_stream_inventory_never_falls_back_on_auth_or_transport_error(monkeypatch):
    calls = []
    def opened(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, 'denied', {}, io.BytesIO())
    monkeypatch.setattr(runner, '_open_url', opened)
    with pytest.raises(urllib.error.HTTPError) as denied:
        runner._open_stream('https://example.test', 'token', [], ['one'], [], None)
    assert denied.value.code == 401
    assert len(calls) == 1


def test_output_spools_large_unicode_in_bounded_batches_and_retries():
    with closing(runner._CommandOutput()) as output:
        for _ in range(5000):
            output.append('stdout', '😀' * 20)
        first = output.take_batch()
        assert len(first) <= 4096
        assert len(json.dumps({'request_id': 'r', 'chunks': first}).encode()) <= runner._OUTPUT_BATCH_BYTES
        assert output.take_batch() == first
        output.acknowledge(first[-1]['seq'])
        second = output.take_batch()
        assert second[0]['seq'] == first[-1]['seq'] + 1
        assert output.next_seq == 5000


def test_output_acknowledges_server_cursor_not_attempted_last(monkeypatch):
    command = runner._CommandRunner('https://example.test', 'token')
    command.live_output = True
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'one')
        output.append('stdout', 'two')
        record = runner._Command('r', 1)
        record.output = output
        replies = iter([{'ok': True, 'next': 1}, {'ok': True, 'next': 2}])
        monkeypatch.setattr(runner, '_post', lambda *a, **k: next(replies))
        assert command.flush_output(record, drain=False)
        assert [c['seq'] for c in output.take_batch()] == [1]
        assert command.flush_output(record, drain=True)
        assert output.take_batch() == []


def test_async_start_deduplicates_while_ack_is_blocked(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    reported = threading.Event()
    spawns = []
    command = runner._CommandRunner('https://example.test', 'token')
    def state(_):
        entered.set()
        release.wait(2)
    monkeypatch.setattr(command, '_post_started', state)
    def spawn(*_args, **kwargs):
        kwargs['before_spawn']()
        spawns.append(1)
    monkeypatch.setattr(runner, '_spawn_command', spawn)
    monkeypatch.setattr(command, '_wake_result_worker', reported.set)
    event = {'request_id': 'x', 'cmd': 'echo x', 'not_after': time.time() + 10}
    start = time.monotonic()
    command.start(event)
    assert time.monotonic() - start < 0.5
    assert entered.wait(1)
    for _ in range(600):
        command.start(event)
    command.cancel('x')
    release.set()
    assert reported.wait(1)
    assert spawns == []


def test_blocked_first_popen_does_not_launch_expired_second_or_block_cancel(monkeypatch):
    entered, release, first_published = (threading.Event() for _ in range(3))
    launches = []
    now = [100.0]
    monkeypatch.setattr(runner.time, 'time', lambda: now[0])
    command = runner._CommandRunner('https://example.test', 'token')
    monkeypatch.setattr(command, '_post_started', lambda _id: None)
    monkeypatch.setattr(command, '_wake_result_worker', lambda: None)

    class Proc:
        pass

    def popen(_command, **_kwargs):
        launches.append(_command)
        entered.set()
        assert release.wait(2)
        return Proc()

    def wait(record):
        first_published.set()
        command._post_result(record, '', '', 130, 'canceled')

    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(command, '_wait', wait)
    command.start({'request_id': 'first', 'cmd': 'echo first', 'not_after': 200})
    assert entered.wait(1)
    now[0] = 101.0
    command.start({'request_id': 'second', 'cmd': 'echo second', 'not_after': 100.5})
    # Duplicate delivery of either accepted id cannot introduce another launch.
    command.start({'request_id': 'first', 'cmd': 'echo first', 'not_after': 200})
    command.start({'request_id': 'second', 'cmd': 'echo second', 'not_after': 200})
    deadline = time.monotonic() + 1
    while not any(m['request_id'] == 'second' for m in command.pending_messages()):
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)
    assert command.cancel('first')  # must return while its Popen is blocked
    assert len(launches) == 1
    release.set()
    assert first_published.wait(1)
    results = {m['request_id']: m for m in command.pending_messages()}
    assert results['second']['outcome'] == 'expired'
    assert results['second']['exit_code'] == 124
    assert results['first']['outcome'] == 'canceled'


def test_cancel_before_spawn_eligibility_refuses_launch(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    command = runner._CommandRunner('https://example.test', 'token')
    monkeypatch.setattr(command, '_wake_result_worker', lambda: None)
    def acknowledge(_id):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(command, '_post_started', acknowledge)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('launched'))
    command.start({'request_id': 'cancel-before', 'cmd': 'echo no',
                   'not_after': time.time() + 10})
    assert entered.wait(1)
    assert command.cancel('cancel-before')
    release.set()
    deadline = time.monotonic() + 1
    while not command.pending_messages():
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)
    [result] = command.pending_messages()
    assert (result['outcome'], result['exit_code']) == ('canceled', 130)


def test_cancel_during_popen_is_seen_after_process_publication(monkeypatch):
    entered, release, observed = (threading.Event() for _ in range(3))
    command = runner._CommandRunner('https://example.test', 'token')
    monkeypatch.setattr(command, '_post_started', lambda _id: None)
    monkeypatch.setattr(command, '_wake_result_worker', lambda: None)
    proc = object()
    def popen(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return proc
    def wait(record):
        assert record.proc is proc
        assert record.stop_reason == 'canceled'
        command._post_result(record, '', '', 130, 'canceled')
        observed.set()
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(command, '_wait', wait)
    command.start({'request_id': 'during', 'cmd': 'echo no',
                   'not_after': time.time() + 10})
    assert entered.wait(1)
    assert command.cancel('during')
    release.set()
    assert observed.wait(1)
    assert command.pending_messages()[0]['outcome'] == 'canceled'


def test_long_command_refused_after_preparation_removes_private_file(tmp_path, monkeypatch):
    mkstemp = runner.tempfile.mkstemp
    monkeypatch.setattr(runner.tempfile, 'mkstemp',
                        lambda **kwargs: mkstemp(dir=tmp_path, **kwargs))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('launched'))
    with pytest.raises(runner._StartRefused):
        runner._spawn_command('exit 0\n#' + 'x' * 150_000, None,
                              before_spawn=lambda: (_ for _ in ()).throw(
                                  runner._StartRefused('expired')))
    assert list(tmp_path.iterdir()) == []


def test_final_result_is_retained_before_output_drain(monkeypatch):
    command = runner._CommandRunner('https://example.test', 'token')
    class Proc:
        returncode = 0
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'hello')
        record = runner._Command('r', 1)
        record.proc = Proc()
        record.output = output
        command.active['r'] = record
        monkeypatch.setattr(runner, '_supervise_process', lambda *a, **k: None)
        monkeypatch.setattr(command, 'flush_output', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('drain')))
        monkeypatch.setattr(command, '_wake_result_worker', lambda: None)
        command._wait(record)
        [result] = command.pending_messages()
        assert result['stdout'] == 'hello'
        assert result['outcome'] == 'completed'
        assert result['exit_code'] == 0
        assert result['output_seq'] == 1
        # The final result waits for its undelivered output to drain.
        assert list(command.outbox) == [record]
        assert record.output is output


def test_spool_read_failure_downgrades_pending_result(monkeypatch):
    command = runner._CommandRunner('https://example.test', 'token')
    command.live_output = True
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'first')
        record = runner._Command('r', 1)
        record.output = output
        record.result = {'type': 'result', 'request_id': 'r', 'stdout': 'first',
                         'stderr': '', 'exit_code': 0, 'outcome': 'completed',
                         'output_seq': 1}
        command.outbox.append(record)
        def fail(*_args, **_kwargs):
            output.output_error = 'disk read failed'
            return False
        monkeypatch.setattr(command, 'flush_output', fail)
        sent = []
        monkeypatch.setattr(runner, '_post', lambda _url, payload, **_kw: sent.append(dict(payload)))
        assert command.flush_pending_results()
        assert sent[0]['outcome'] == 'completed'
        assert sent[0]['exit_code'] == 0
        assert 'disk read failed' in sent[0]['output_error']
        assert 'output_seq' not in sent[0]


def test_config_replacement_preserves_old_on_serialization_failure(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"old":true}')
    monkeypatch.setattr(runner, 'CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(runner, 'CONFIG_PATH', str(path))
    with pytest.raises(TypeError):
        runner._save_config({'bad': object()})
    assert path.read_text() == '{"old":true}'


def test_download_validation_preserves_old_runner(tmp_path, monkeypatch):
    path = tmp_path / 'runner.py'
    path.write_text('good')
    monkeypatch.setattr(runner, 'CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(runner, 'RUNNER_PATH', str(path))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b'<html>proxy error</html>'
    monkeypatch.setattr(runner, '_open_url', lambda *a, **k: Response())
    with pytest.raises(ValueError):
        runner._self_download('https://example.test')
    assert path.read_text() == 'good'


def test_script_stdin_is_utf8_on_posix(monkeypatch):
    monkeypatch.setattr(runner.os, 'name', 'posix')
    class Pipe(io.BytesIO):
        def close(self):
            self.written = self.getvalue()
            super().close()
    pipe = Pipe()
    runner._feed_stdin(pipe, '雪')
    assert pipe.closed
    assert pipe.written == '雪'.encode('utf-8')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell command-file contract')
def test_long_inline_command_uses_file_without_changing_stdin(monkeypatch):
    command = "printf '%s|%s|雪\\n' \"$0\" \"${1-unset}\"\n#" + 'x' * 150_000
    result = _run_inline(command, monkeypatch)
    assert (result['stdout'], result['stderr'], result['exit_code'], result['timed_out']) == ('/bin/sh|unset|雪\n', '', 0, False)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell command-file contract')
def test_long_inline_command_preserves_exit_and_trap(monkeypatch):
    command = "trap 'printf trapped >&2' EXIT; exit 7\n#" + 'x' * 150_000
    result = _run_inline(command, monkeypatch)
    assert (result['stdout'], result['stderr'], result['exit_code'], result['timed_out']) == ('', 'trapped', 7, False)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell command-file contract')
def test_long_inline_command_keeps_stdin_inherited():
    proc, command_file = runner._spawn_command('exit 0\n#' + 'x' * 150_000, None)
    with proc:
        try:
            assert proc.stdin is None
            assert proc.wait(timeout=5) == 0
        finally:
            os.unlink(command_file)


def test_spool_index_stays_constant_size_and_ack_reclaims_scratch():
    with closing(runner._CommandOutput()) as output:
        for _ in range(20_000):
            output.append('stdout', 'x')
        assert not hasattr(output, 'chunks')
        assert output.read_offset == 0 and output.has_pending()
        seen = 0
        while output.has_pending():
            batch = output.take_batch()
            assert len(batch) <= 4096
            assert batch[0]['seq'] == seen
            seen += len(batch)
            output.acknowledge(batch[-1]['seq'])
        assert seen == 20_000
        assert output.spool.seek(0, 2) == 0
        output.append('stdout', 'after reclaim')
        assert output.take_batch()[0]['seq'] == 20_000


def test_large_inline_nul_cannot_change_os_rejection_into_script_execution():
    with pytest.raises(ValueError, match='NUL'):
        runner._spawn_command('echo must-not-run\n#' + 'x' * 100_000 + '\x00', None)


def test_acknowledged_output_stays_delivered_when_scratch_reclaim_fails():
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'acknowledged')
        original = output.spool
        class NoTruncate:
            def __getattr__(self, name):
                return getattr(original, name)
            def truncate(self):
                raise OSError('scratch reclaim unavailable')
        output.spool = NoTruncate()
        output.acknowledge(0)
        assert not output.has_pending()
        assert output.output_error is None
        output.append('stdout', 'next')
        assert output.take_batch() == [{'seq': 1, 'stream': 'stdout', 'text': 'next'}]


@pytest.mark.parametrize('status', [409, 422])
def test_rejected_output_cannot_be_reported_as_complete(monkeypatch, status):
    command = runner._CommandRunner('https://example.test', 'token')
    command.live_output = True
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'known preview')
        record = runner._Command('r', 1)
        record.output = output
        record.result = {'type': 'result', 'request_id': 'r',
                         'outcome': 'completed', 'exit_code': 0,
                         'stdout': 'known preview', 'stderr': '', 'output_seq': 1}
        command.outbox.append(record)
        sent = []
        def post(url, payload, **_kwargs):
            if url.endswith('/output'):
                raise urllib.error.HTTPError(url, status, 'rejected', {}, io.BytesIO())
            sent.append(dict(payload))
            return {'ok': True}
        monkeypatch.setattr(runner, '_post', post)
        assert command.flush_pending_results()
        assert not command.active and not command.pending_messages()
        assert sent[0]['outcome'] == 'completed' and sent[0]['exit_code'] == 0
        assert 'HTTP %s' % status in sent[0]['output_error']
        assert 'output_seq' not in sent[0]


def test_invalid_spool_record_length_is_bounded_and_explicit():
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'text')
        output.spool.seek(0)
        output.spool.write(runner._SPOOL_HEADER.pack(0, 0xffffffff))
        assert output.take_batch() == []
        assert 'invalid output spool record length' in output.output_error


@pytest.mark.parametrize('status', [None, 503, 422])
def test_output_scratch_lives_until_result_delivery_settles(monkeypatch, status):
    command = runner._CommandRunner('https://example.test', 'token')
    command.live_output = True
    monkeypatch.setattr(command, '_wake_result_worker', lambda: None)
    with closing(runner._CommandOutput()) as output:
        output.append('stdout', 'retained')
        record = runner._Command('scratch', 1)
        record.output = output
        command._post_result(record, 'retained', '', 0, 'completed', output_seq=1)
        assert not output.spool.closed

        def post(url, _payload, **_kwargs):
            if url.endswith('/output'):
                return {'ok': True, 'next': 1}
            if status is not None:
                raise urllib.error.HTTPError(url, status, 'test', {}, io.BytesIO())
            return {'ok': True}

        monkeypatch.setattr(runner, '_post', post)
        assert command.flush_pending_results() is (status != 503)
        if status == 503:
            assert not output.spool.closed
            assert command.pending_messages()
            status = None
            assert command.flush_pending_results()
        assert output.spool.closed
        assert not command.pending_messages()


def test_output_without_pending_chunks_closes_before_delivery(monkeypatch):
    command = runner._CommandRunner('https://example.test', 'token')
    monkeypatch.setattr(command, '_wake_result_worker', lambda: None)
    output = runner._CommandOutput()
    record = runner._Command('empty', 1)
    record.output = output
    command._post_result(record, '', '', 124, 'expired')
    assert output.spool.closed
    assert command.pending_messages()[0]['outcome'] == 'expired'


def test_late_escaped_child_output_cannot_reopen_released_scratch():
    output = runner._CommandOutput()
    output.append('stdout', 'captured before delivery')
    output.close()
    output.append('stdout', 'late escaped descendant')
    assert output.spool.closed
    assert not output.has_pending()
    assert output.take_batch() == []
    assert output.next_seq == 1


def test_pipe_reader_closes_its_own_pipe_after_eof():
    pipe = io.BytesIO(b'last bytes')
    with closing(runner._CommandOutput()) as output:
        runner._pump_stream(pipe, 'stdout', output)
        assert pipe.closed
        assert output.final_streams() == ('last bytes', '', False)


@pytest.mark.parametrize('base', ['https://controller.example', 'https://192.0.2.42:8443'])
def test_pair_success_names_granted_instance_not_machine_alias(monkeypatch, capsys, base):
    saved = []
    monkeypatch.setattr(runner, '_post', lambda *_args, **_kwargs: {
        'host_id': 'test-host', 'token': 'private-host-token', 'name': 'Mac',
    })
    monkeypatch.setattr(runner, '_add_connection', saved.append)
    connection = runner._pair(base + '/', 'private-pair-code')
    assert capsys.readouterr().out == 'Granted command access to %s.\n' % base
    assert connection['name'] == 'Mac'
    assert saved == [connection]


def test_pair_does_not_announce_access_when_saving_fails(monkeypatch, capsys):
    monkeypatch.setattr(runner, '_post', lambda *_args, **_kwargs: {
        'host_id': 'test-host', 'token': 'private-host-token', 'name': 'Mac',
    })
    def failed_save(_connection):
        raise OSError('Disk full')
    monkeypatch.setattr(runner, '_add_connection', failed_save)
    with pytest.raises(OSError, match='Disk full'):
        runner._pair('https://controller.example', 'private-pair-code')
    assert capsys.readouterr().out == ''
