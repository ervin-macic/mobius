"""Host admission topology and non-dispatch installation publication."""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_mobius_rebuild_host import host
from tests.test_admission_host_recovery import PowerLoss, incident  # noqa: F401 (fixture)


@pytest.fixture
def case(incident, monkeypatch):
    """The shared real-gate incident, with version-4 frozen naming and the
    Docker semantics that matter for a stable name across attempt projects:
    Compose owns its project label, and Create conflicts on a held name."""
    c = incident
    c.config.update(deployment_name="original-app", name_source="explicit")
    base = c.docker.command

    def command(args, **kwargs):
        args = list(args)
        if args[1] == "compose" and "up" in args:
            project = args[args.index("-p") + 1]
            path = [args[i + 1] for i, part in enumerate(args) if part == "-f"][0]
            name = json.loads(Path(path).read_text())["services"]["app"]["container_name"]
            holder = [v for v in c.docker.containers.values() if v["Name"] == "/" + name]
            if holder and (holder[0]["Config"].get("Labels") or {}).get(
                    "com.docker.compose.project") != project:
                c.docker.calls.append((args, kwargs))
                return c.docker._reply(args, code=1, check=kwargs.get("check", True), error=(
                    f'Error response from daemon: Conflict. The container name "/{name}" is already in use'))
            before = set(c.docker.containers)
            result = base(args, **kwargs)
            for cid in set(c.docker.containers) - before:
                labels = {**c.docker.containers[cid]["Config"]["Labels"],
                          "com.docker.compose.project": project, "com.docker.compose.service": "app"}
                c.docker.containers[cid]["Config"]["Labels"] = labels
                c.docker.history[cid]["Config"]["Labels"] = dict(labels)
            return result
        return base(args, **kwargs)

    c.docker.command = command
    monkeypatch.setattr(c.host, "docker_command", command)
    return c


def test_prepared_gate_survives_retirement_of_original_ledger_receipt(case):
    c = case
    c.host.prepare_admission(c.config, c.tx)
    before = c.gate.observe()
    c.legacy.CUTOVER_RECEIPT_PATH.unlink()
    c.host.prepare_admission(c.config, c.tx)
    assert c.gate.observe() == before
    assert len(c.docker.removes) == 1


@pytest.mark.parametrize('source_status,drain_confirmed,allowed', [
    ('running', False, False), ('running', True, True),
    ('exited', False, True), ('restarting', False, True), ('missing', False, True),
])
def test_missing_initial_receipt_never_removes_undrained_serviceable_source(
        case, source_status, drain_confirmed, allowed):
    c = case
    c.legacy.CUTOVER_RECEIPT_PATH.unlink()
    c.tx['drain_confirmed'] = drain_confirmed
    source = c.tx['source_container']
    if source_status == 'missing':
        del c.docker.containers[source]
    else:
        c.docker.containers[source]['State']['Status'] = source_status
    if not allowed:
        with pytest.raises(RuntimeError):
            c.host.prepare_admission(c.config, c.tx)
        assert not c.docker.removes and not c.docker.starts
        return
    c.host.prepare_admission(c.config, c.tx)
    state = c.gate.observe()
    assert state['ledger_degraded'] is True
    assert state['receipt'] is None and state['source_handoff_valid'] is False
    assert source in state['quiesced']


def test_settlement_requires_gate_boot_not_only_a_matching_legacy_pair(case):
    c = case
    cid = c.host.prepare_rollback(c.config, c.tx)
    assert c.docker.complete_start(cid)
    ack = json.loads(c.legacy.ACK_PATH.read_text())
    ack['target_boot_id'] = 'unrelated-boot-12345678'
    os.chmod(c.legacy.ACK_PATH, 0o600)
    os.chmod(c.legacy.BOOT_PATH, 0o600)
    c.legacy.ACK_PATH.write_text(json.dumps(ack))
    c.legacy.BOOT_PATH.write_text(ack['target_boot_id'])
    assert c.host.cutover_boot_consumed(c.config, c.tx['operation_id'])
    assert not c.host.finish_verified(c.config, c.tx, cid, c.tx['previous_image'],
                                     state='rolled_back', code='replacement_failed', message='test')
    assert c.host.read_json(c.host.STATUS)['code'] == 'handoff_boot_unconfirmed'
    assert c.host.TRANSACTION.exists()


def test_new_proven_rejection_supersedes_old_unknown_observation(case, monkeypatch):
    c = case
    c.tx['phase'] = 'replacement_started'
    c.host.write_transaction(c.tx)
    c.host.prepare_admission(c.config, c.tx)
    cid = c.host.prepare_attempt(c.config, c.tx, 'target')
    assert c.docker.complete_start(cid)
    c.host.write_status(c.config, operation_id=c.tx['operation_id'], state='needs_recovery',
                        code='observation_unconfirmed', message='prior unknown observation')
    def reject(*_):
        raise c.host.ProvenanceRejected('wrong served revision')
    monkeypatch.setattr(c.host, 'verify_served_generation', reject)
    c.host.reconcile()
    status = c.host.read_json(c.host.STATUS)
    assert status['state'] == 'rolled_back'
    assert status['code'] == 'replacement_failed'


def topology(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    config = {'project': 'mobius', 'data_dir': data, 'control_dir': tmp_path / 'control',
              'deployment_name': 'mobius-app-1', 'name_source': 'generated'}
    config['control_dir'].mkdir()
    tx = {'operation_id': 'a' * 32}
    resolved = {'name': 'mobius', 'services': {'app': {'image': 'old', 'volumes': [
        {'type': 'bind', 'source': str(data), 'target': '/data'}],
        'networks': {'edge': {'aliases': ['app', 'mobius']}},
        'environment': {'KEEP': 'value'}, 'restart': 'unless-stopped',
        'build': {'context': '/ignored'}, 'depends_on': {'other': {}}}},
        'networks': {'edge': {'name': 'existing-edge'}}}
    monkeypatch.setattr(host, 'inspect_image', lambda *_: json.dumps({
        'Entrypoint': ['/usr/bin/tini', '--'], 'Cmd': ['/app/start']}))
    monkeypatch.setattr(host, 'docker_command', lambda args, **kw:
                        subprocess.CompletedProcess(args, 0, json.dumps(resolved), ''))
    return config, tx, resolved


def test_attempt_scope_preserves_argv_and_shared_resources_without_reconciliation(tmp_path, monkeypatch):
    config, tx, _ = topology(tmp_path, monkeypatch)
    result = host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)
    app = result['services']['app']
    assert result['name'] == 'mobius-admission-' + 'b' * 32
    assert app['container_name'] == 'mobius-app-1'  # the frozen generated name, not per attempt
    assert app['entrypoint'][-3:] == ['--', '/usr/bin/tini', '--']
    assert app['command'] == ['/app/start']
    assert app['environment'] == {'KEEP': 'value'}
    assert app['networks']['edge']['aliases'] == ['app', 'mobius']
    assert result['networks'] == {'edge': {'name': 'existing-edge', 'external': True}}
    assert 'build' not in app and 'depends_on' not in app
    assert app['labels']['io.mobius.admission.project'] == 'mobius'


@pytest.mark.parametrize('mutation', ['different-data', 'readonly-data', 'anonymous', 'secret', 'namespace'])
def test_preflight_rejects_unpreserved_resource_contract(tmp_path, monkeypatch, mutation):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    app = resolved['services']['app']
    if mutation == 'different-data':
        app['volumes'][0]['source'] = '/different-data'
    elif mutation == 'readonly-data':
        app['volumes'][0]['read_only'] = True
    elif mutation == 'anonymous':
        app['volumes'].append({'type': 'volume', 'target': '/other'})
    elif mutation == 'secret':
        app['secrets'] = ['credential']
    else:
        app['network_mode'] = 'service:other'
    with pytest.raises(RuntimeError):
        host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)


def test_manual_roles_use_different_durable_topology_snapshots(tmp_path, monkeypatch):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    tx['source_topology'] = copy.deepcopy(resolved)
    tx['target_topology'] = copy.deepcopy(resolved)
    tx['target_topology']['services']['app']['environment'] = {'NEW': 'target'}
    monkeypatch.setattr(host, 'docker_command', lambda *_a, **_k: pytest.fail('re-read topology instead of frozen input'))
    source = host.wrapped_configuration(config, tx, 'rollback', 'b' * 32, 'sha256:' + 'c' * 64)
    target = host.wrapped_configuration(config, tx, 'target', 'd' * 32, 'sha256:' + 'e' * 64)
    assert source['services']['app']['environment'] == {'KEEP': 'value'}
    assert target['services']['app']['environment'] == {'NEW': 'target'}


def test_capability_publication_never_dispatches_and_preserves_outcome(tmp_path, monkeypatch):
    config, _, _ = topology(tmp_path, monkeypatch)
    monkeypatch.setattr(host, 'config', lambda: config)
    monkeypatch.setattr(host, 'LOCK', tmp_path / 'lock')
    monkeypatch.setattr(host, 'STATUS', tmp_path / 'status')
    monkeypatch.setattr(host, 'TRANSACTION', tmp_path / 'transaction')
    monkeypatch.setattr(host, 'docker_command', lambda *_a, **_k: pytest.fail('installer called Docker'))
    monkeypatch.setenv('MOBIUS_REBUILD_LAUNCHER', '2')
    host.STATUS.write_text(json.dumps({'state': 'rolled_back', 'operation_id': 'old', 'code': 'original'}))
    assert host.publish_capabilities() == 0
    result = host.read_json(host.STATUS)
    assert result['state'] == 'rolled_back' and result['operation_id'] == 'old'
    assert result['code'] == 'original' and result['launcher_revision'] == 2
    before = host.STATUS.read_bytes()
    host.TRANSACTION.write_text('{}')
    assert host.publish_capabilities() == 1
    assert host.STATUS.read_bytes() == before


def test_exact_removal_does_not_guess_from_transport_failure(monkeypatch):
    cid = 'a' * 64
    def response(message):
        monkeypatch.setattr(host, 'docker_command', lambda args, **kw:
                            subprocess.CompletedProcess(args, 1, '', message))
    response('Error response from daemon: No such container: ' + cid)
    host.remove_exact_container(cid)
    for message in ('connection reset', 'No such container', 'Error response from daemon: No such container: other'):
        response(message)
        with pytest.raises(subprocess.CalledProcessError):
            host.remove_exact_container(cid)


@pytest.mark.parametrize("entrypoint,command,expected", [
    (["/custom-start"], None, ["/custom-start"]),
    (["/custom-start"], ["--flag"], ["/custom-start", "--flag"]),
    ([], ["/custom-command"], ["/custom-command"]),
    (None, [], ["/usr/bin/tini", "--"]),
])
def test_wrapper_preserves_compose_entrypoint_command_overrides(tmp_path, monkeypatch, entrypoint, command, expected):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    resolved["services"]["app"].update(entrypoint=entrypoint, command=command)
    result = host.wrapped_configuration(config, tx, "target", "b" * 32, "sha256:" + "c" * 64)
    app = result["services"]["app"]
    original = app["entrypoint"][app["entrypoint"].index("--") + 1:] + app["command"]
    assert original == expected


def test_same_image_cutover_recovery_does_not_mistake_source_for_target(case):
    c = case
    c.tx.update(target_image=c.tx['previous_image'], phase='replacement_started')
    c.host.write_transaction(c.tx)
    c.host.recover(c.config, c.tx)
    result = c.host.read_json(c.host.STATUS)
    assert result['state'] == 'rolled_back'
    assert not c.host.TRANSACTION.exists()
    assert c.tx['source_container'] in c.docker.removes
    assert len(c.docker.starts) == 1


@pytest.mark.parametrize('observed', [None, [], ['unexpected'], '', False])
def test_docker_null_command_is_only_equivalent_to_an_empty_argv(case, monkeypatch, observed):
    c = case
    c.host.prepare_admission(c.config, c.tx)
    attempt = c.gate.allocate('target', 'd' * 32, c.tx['target_image'])
    expected = c.host.wrapped_configuration(c.config, c.tx, 'target', attempt['token'], c.tx['target_image'])
    expected['services']['app']['command'] = []
    cid = c.host.create_attempt(c.config, expected)
    c.docker.containers[cid]['Config']['Cmd'] = observed
    if observed is None or observed == []:
        assert c.host.verify_wrapped_container(cid, expected)['Id'] == cid
    else:
        with pytest.raises(RuntimeError, match='identity or pre-entrypoint'):
            c.host.verify_wrapped_container(cid, expected)


def test_compose_file_boundary_keeps_image_argv_and_resolved_values_literal(tmp_path, monkeypatch):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    original = ['/bin/sh', '-c', 'echo ${MOBIUS_BOOT_ID:-fallback} $ordinary $$pid']
    monkeypatch.setattr(host, 'inspect_image', lambda *_: json.dumps({'Entrypoint': original, 'Cmd': None}))
    resolved['services']['app']['environment'] = {'VALUE': '$$LITERAL', '$$KEY': '$$$$two'}
    resolved['services']['app']['labels'] = {'literal': '$${LABEL:-not-a-default}'}
    expected = host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)
    app = expected['services']['app']
    assert app['entrypoint'][-3:] == original
    assert app['command'] == []
    assert app['environment'] == {'VALUE': '$LITERAL', '$KEY': '$$two'}
    document = host.compose_file_document(expected)
    assert document['services']['app']['entrypoint'][-1] == original[-1].replace('$', '$$')
    assert document['services']['app']['environment'] == {'VALUE': '$$LITERAL', '$KEY': '$$$$two'}
    assert document['services']['app']['labels']['literal'] == '$${LABEL:-not-a-default}'
    assert host.compose_file_document(expected) == document  # pure; no repeated escaping


def test_real_compose_parse_cannot_reinterpret_frozen_runtime_values(tmp_path, monkeypatch):
    import shutil
    executable = os.environ.get('MOBIUS_TEST_COMPOSE')
    compose = [executable] if executable else ['docker', 'compose']
    if not shutil.which(compose[0]):
        pytest.skip('Compose parser unavailable; hosted recovery runs the daemon proof')
    config, tx, resolved = topology(tmp_path, monkeypatch)
    original = ['/bin/sh', '-c', 'echo ${MOBIUS_TEST_FROZEN:-fallback} $ordinary $$pid']
    monkeypatch.setattr(host, 'inspect_image', lambda *_: json.dumps({'Entrypoint': original, 'Cmd': None}))
    resolved['services']['app']['environment'] = {'VALUE': '$$MOBIUS_TEST_FROZEN', 'DOUBLE': '$$$$pid'}
    resolved['services']['app']['labels'] = {'literal': '$${MOBIUS_TEST_FROZEN:-fallback}'}
    expected = host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)
    path = tmp_path / 'attempt.json'
    path.write_text(json.dumps(host.compose_file_document(expected)))
    result = subprocess.run([*compose, '-f', str(path), 'config', '--format', 'json'],
                            env={**os.environ, 'MOBIUS_TEST_FROZEN': 'MUST_NOT_SUBSTITUTE'},
                            check=True, capture_output=True, text=True, timeout=15)
    # `config` emits a re-readable, escaped document, not raw Docker argv.
    actual = json.loads(result.stdout.replace('$$', '$'))['services']['app']
    for key in ('entrypoint', 'command', 'environment', 'labels'):
        assert actual[key] == expected['services']['app'][key]


# --- Host identity boundary: root-private journal, stable name, tombstones ---

FOREIGN = 'f' * 64


def mutations(c):
    return (len(c.docker.removes), len(c.docker.starts), len(c.docker.creates))


def edit_gate(c, change):
    path = c.gate.state_dir / 'state.json'
    state = json.loads(path.read_text())
    change(state)
    path.write_text(json.dumps(state))


def issued_target(c, *, enter=False):
    c.docker.auto_enter = enter
    c.host.prepare_admission(c.config, c.tx)
    cid = c.host.prepare_attempt(c.config, c.tx, 'target')
    return cid


def add_foreign(c, name='/unrelated-service', labels=None, image='sha256:' + 'f' * 64):
    c.docker.containers[FOREIGN] = {
        'Id': FOREIGN, 'Image': image, 'Name': name, 'Config': {'Labels': labels or {}},
        'Mounts': [], 'RestartCount': 0, 'State': {'Status': 'running'}}


def orphan_of(c, cid, new_cid):
    """A delayed accepted no-start Create of the same immutable attempt file."""
    item = copy.deepcopy(c.docker.history[cid])
    item.update(Id=new_cid, State={'Status': 'created', 'StartedAt': '0001-01-01T00:00:00Z'})
    c.docker.containers[new_cid] = item
    return new_cid


def test_stable_name_and_unique_project_come_from_frozen_deployment(case):
    c = case
    cid = issued_target(c)
    item = c.docker.containers[cid]
    token = item['Config']['Labels']['io.mobius.admission.token']
    assert item['Name'] == '/original-app'
    assert item['Config']['Labels']['com.docker.compose.project'] == 'mobius-admission-' + token
    record = c.host.read_transaction()['attempts'][0]
    assert record == {'role': 'target', 'generation': 1, 'token': token,
                      'image': c.tx['target_image'], 'cid': cid,
                      'attempt_sha256': record['attempt_sha256']}
    assert c.host.OVERRIDE.read_bytes() == c.host.attempt_path(token).read_bytes()


@pytest.mark.parametrize('declared,generated_name,refused', [
    ('renamed-app', None, True), (None, None, True), ('original-app', None, False)])
def test_topology_cannot_silently_rename_the_frozen_app(case, declared, generated_name, refused):
    c = case
    tx = dict(c.tx, target_topology=copy.deepcopy(c.docker.topology))
    app = tx['target_topology']['services']['app']
    if declared is None:
        app.pop('container_name')
    else:
        app['container_name'] = declared
    if refused:
        with pytest.raises(RuntimeError, match='frozen deployment'):
            c.host.wrapped_configuration(c.config, tx, 'target', 'b' * 32, c.tx['target_image'])
    else:
        c.host.wrapped_configuration(c.config, tx, 'target', 'b' * 32, c.tx['target_image'])


def test_generated_name_requires_the_original_project_and_no_declared_name(case):
    c = case
    c.config.update(deployment_name='original-app-1', name_source='generated')
    tx = dict(c.tx, target_topology=copy.deepcopy(c.docker.topology))
    with pytest.raises(RuntimeError, match='frozen deployment'):
        c.host.wrapped_configuration(c.config, tx, 'target', 'b' * 32, c.tx['target_image'])
    tx['target_topology']['services']['app'].pop('container_name')
    wrapped = c.host.wrapped_configuration(c.config, tx, 'target', 'b' * 32, c.tx['target_image'])
    assert wrapped['services']['app']['container_name'] == 'original-app-1'
    tx['target_topology']['name'] = 'other-project'
    with pytest.raises(RuntimeError, match='frozen deployment'):
        c.host.wrapped_configuration(c.config, tx, 'target', 'b' * 32, c.tx['target_image'])


def test_container_writable_gate_cannot_select_foreign_container_for_removal(case):
    c = case
    issued_target(c)
    add_foreign(c)
    edit_gate(c, lambda s: s['slots']['target']['attempts'][-1].update(cid=FOREIGN))
    before = mutations(c)
    with pytest.raises(c.host.AttemptIdentityMismatch):
        c.host.prepare_attempt(c.config, c.host.read_transaction(), 'target')
    with pytest.raises(c.host.AttemptIdentityMismatch):
        c.host.quiesce_target(c.config, c.host.read_transaction())
    assert FOREIGN in c.docker.containers and mutations(c) == before
    c.host.reconcile()
    assert FOREIGN in c.docker.containers and mutations(c) == before
    assert c.host.read_json(c.host.STATUS)['state'] == 'needs_recovery'


@pytest.mark.parametrize('tamper', ['token', 'image', 'extra', 'consumed'])
def test_gate_identity_tamper_refuses_before_any_docker_mutation(case, tamper):
    c = case
    cid = issued_target(c)

    def change(state):
        slot = state['slots']['target']
        attempt = slot['attempts'][-1]
        if tamper == 'token':
            attempt['token'] = 'e' * 32
        elif tamper == 'image':
            attempt['image'] = 'sha256:' + 'e' * 64
        elif tamper == 'extra':
            attempt.update(state='closed', fenced=True)
            state['quiesced'].append(cid)
            slot['attempts'].append({'generation': 2, 'token': 'e' * 32, 'image': attempt['image'],
                                     'cid': None, 'state': 'allocated', 'issued': False, 'fenced': False})
        else:
            attempt.update(state='admitted', boot_id='forged-boot-12345678', continuation=False)
            slot['consumed'] = {'generation': 2, 'boot_id': 'forged-boot-12345678'}
    edit_gate(c, change)
    before = mutations(c)
    for operation in (c.host.prepare_attempt, c.host.prepare_rollback):
        args = (c.config, c.host.read_transaction()) + (('target',) if operation is c.host.prepare_attempt else ())
        with pytest.raises(RuntimeError):
            operation(*args)
    assert mutations(c) == before and cid in c.docker.containers


def test_forged_quiescence_flags_never_skip_exact_removal_before_rollback(case):
    c = case
    cid = issued_target(c)
    c.docker.containers[cid]['State'] = {'Status': 'running', 'Health': {'Status': 'unhealthy'}}
    # Container root marks its own target closed, fenced and removed.
    edit_gate(c, lambda s: (s['slots']['target']['attempts'][-1].update(state='closed', fenced=True),
                            s['quiesced'].append(cid)))
    c.docker.auto_enter = True
    rollback = c.host.prepare_rollback(c.config, c.host.read_transaction())
    assert cid in c.docker.removes and cid not in c.docker.containers
    calls = [args for args, _ in c.docker.calls]
    removed = calls.index(['docker', 'container', 'rm', '--force', cid])
    assert removed < calls.index(['docker', 'start', rollback])
    assert c.docker.starts == [cid, rollback]
    assert c.host.read_tombstones()['tokens'].keys() >= {
        c.docker.history[cid]['Config']['Labels']['io.mobius.admission.token']}


def test_rollback_does_not_open_while_a_journaled_target_still_exists(case, monkeypatch):
    c = case
    cid = issued_target(c)
    original = c.host.remove_exact_container
    # Docker reports success but the exact container is still observable.
    monkeypatch.setattr(c.host, 'remove_exact_container', lambda target: None if target == cid else original(target))
    with pytest.raises(RuntimeError, match='predecessor container still exists'):
        c.host.prepare_rollback(c.config, c.host.read_transaction())
    rollback = c.gate.observe()['slots']['rollback']['attempts']
    assert all(not attempt['issued'] for attempt in rollback)
    assert c.docker.starts == [cid]


def test_foreign_holder_of_vacant_name_is_refused_not_removed(case):
    c = case
    c.host.prepare_admission(c.config, c.tx)
    add_foreign(c, name='/original-app', labels={'com.docker.compose.project': 'original'})
    before = mutations(c)
    with pytest.raises(c.host.AttemptIdentityMismatch):
        c.host.prepare_attempt(c.config, c.tx, 'target')
    assert FOREIGN in c.docker.containers and mutations(c) == before


def test_foreign_holder_never_becomes_the_current_app(case):
    c = case
    cid = issued_target(c, enter=True)
    del c.docker.containers[cid]
    add_foreign(c, name='/original-app', labels={'com.docker.compose.project': 'original'})
    with pytest.raises(c.host.AttemptIdentityMismatch):
        c.host.container_health(c.config)
    with pytest.raises(c.host.AttemptIdentityMismatch):
        c.host.app_container(c.config)


def test_repeated_late_creates_of_one_retired_token_are_removed_exactly(case):
    c = case
    target = issued_target(c)
    token = c.docker.history[target]['Config']['Labels']['io.mobius.admission.token']
    c.host.quiesce_target(c.config, c.host.read_transaction())
    assert token in c.host.read_tombstones()['tokens'] and target in c.docker.removes
    first = orphan_of(c, target, 'a1' * 32)
    rollback1 = c.host.prepare_attempt(c.config, c.host.read_transaction(), 'rollback')
    assert first in c.docker.removes and rollback1 != first
    # Gen-1 rollback start is lost; a second late Create of the SAME target
    # token lands while the name is vacant again.
    second = orphan_of(c, target, 'a2' * 32)
    c.docker.pending.clear()
    c.docker.auto_enter = True
    rollback2 = c.host.prepare_attempt(c.config, c.host.read_transaction(), 'rollback')
    assert second in c.docker.removes and rollback2 not in (rollback1, second)
    assert token in c.host.read_tombstones()['tokens']  # history is never dropped
    assert len(c.host.read_tombstones()['tokens']) == 2


def test_retired_token_with_different_configuration_is_foreign(case):
    c = case
    target = issued_target(c)
    c.host.quiesce_target(c.config, c.host.read_transaction())
    orphan = orphan_of(c, target, 'a1' * 32)
    c.docker.containers[orphan]['Image'] = 'sha256:' + 'e' * 64
    before = mutations(c)
    with pytest.raises(RuntimeError):
        c.host.prepare_attempt(c.config, c.host.read_transaction(), 'rollback')
    assert orphan in c.docker.containers and mutations(c) == before


def test_retired_holder_reads_as_missing_so_recovery_reaches_the_creation_boundary(case):
    c = case
    target = issued_target(c)
    c.host.quiesce_target(c.config, c.host.read_transaction())
    orphan_of(c, target, 'a1' * 32)
    assert c.host.container_health(c.config) == ('', '', 'missing')


@pytest.mark.parametrize('method', ['allocate', 'bind'])
def test_journal_leads_gate_and_replay_reuses_the_same_identity(case, monkeypatch, method):
    c = case
    c.host.prepare_admission(c.config, c.tx)
    original = getattr(c.gate, method)
    def crash(*_args, **_kwargs):
        raise PowerLoss(method)
    monkeypatch.setattr(c.gate, method, crash)
    with pytest.raises(PowerLoss):
        c.host.prepare_attempt(c.config, c.tx, 'target')
    monkeypatch.setattr(c.gate, method, original)
    journal = c.host.read_transaction()
    (record,) = journal['attempts']
    assert (record['cid'] is None) == (method == 'allocate')
    c.docker.auto_enter = False
    cid = c.host.prepare_attempt(c.config, journal, 'target')
    (attempt,) = c.gate.observe()['slots']['target']['attempts']
    assert attempt['token'] == record['token'] and attempt['cid'] == cid
    assert len(c.docker.creates) == 1 and c.docker.starts == [cid]


def test_existing_journaled_source_attempt_is_retired_before_removal(case):
    c = case
    c.host.prepare_admission(c.config, c.tx)
    cid = c.host.prepare_attempt(c.config, c.tx, 'target')
    token = c.docker.containers[cid]['Config']['Labels']['io.mobius.admission.token']
    assert c.host.current_attempt(c.config)['services']['app']['labels'][
        'io.mobius.admission.token'] == token
    assert c.host.app_container(c.config) == (cid, c.tx['target_image'])


# --- Nil-container cancellation (gate API: fenced(role, token, None)) ---

def failed_creation(c):
    c.host.prepare_admission(c.config, c.tx)
    c.docker.fault = 'create-before'
    with pytest.raises(PowerLoss):
        c.host.prepare_attempt(c.config, c.tx, 'target')
    (record,) = c.host.read_transaction()['attempts']
    assert record['cid'] is None and not c.docker.creates
    return record


def test_never_created_attempt_is_cancelled_without_recreating_it(case):
    c = case
    record = failed_creation(c)
    c.host.quiesce_target(c.config, c.host.read_transaction())
    (attempt,) = c.gate.observe()['slots']['target']['attempts']
    assert (attempt['state'], attempt['fenced'], attempt['cid']) == ('closed', True, None)
    assert not c.docker.creates and not c.docker.starts
    assert record['token'] in c.host.read_tombstones()['tokens']
    c.docker.auto_enter = True
    rollback = c.host.prepare_attempt(c.config, c.host.read_transaction(), 'rollback')
    assert c.docker.starts == [rollback]


@pytest.mark.parametrize('crash', ['close_pending', 'retire_token', 'fenced'])
def test_nil_cancellation_crashes_converge_once(case, monkeypatch, crash):
    c = case
    record = failed_creation(c)
    owner = c.host if crash == 'retire_token' else c.gate
    original = getattr(owner, crash)
    fired = []
    def interrupt(*args, **kwargs):
        if not fired:
            fired.append(True)
            original(*args, **kwargs)
            raise PowerLoss(crash)
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, crash, interrupt)
    with pytest.raises(PowerLoss):
        c.host.quiesce_target(c.config, c.host.read_transaction())
    c.host.quiesce_target(c.config, c.host.read_transaction())
    (attempt,) = c.gate.observe()['slots']['target']['attempts']
    assert (attempt['state'], attempt['fenced'], attempt['cid']) == ('closed', True, None)
    assert record['token'] in c.host.read_tombstones()['tokens']
    assert not c.docker.creates and not c.docker.removes[1:]


@pytest.mark.parametrize('point', ['remove-before', 'remove-after'])
def test_bound_cancellation_crash_keeps_tombstone_before_exact_removal(case, point):
    c = case
    target = issued_target(c)
    token = c.docker.containers[target]['Config']['Labels']['io.mobius.admission.token']
    c.docker.fault = point
    with pytest.raises(PowerLoss):
        c.host.quiesce_target(c.config, c.host.read_transaction())
    assert token in c.host.read_tombstones()['tokens']
    assert c.gate.observe()['slots']['target']['attempts'][0]['state'] == 'closed'
    c.host.quiesce_target(c.config, c.host.read_transaction())
    assert target not in c.docker.containers
    assert c.gate.observe()['slots']['target']['attempts'][0]['fenced'] is True


def test_named_data_volume_does_not_replace_frozen_container_name(tmp_path, monkeypatch):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    resolved['services']['app']['volumes'] = [
        {'type': 'volume', 'source': 'app_data', 'target': '/data'}]
    resolved['volumes'] = {'app_data': {'name': 'persistent_app_data'}}
    def execute(args, **kwargs):
        if args[1:3] == ['volume', 'inspect']:
            assert args[-1] == 'persistent_app_data'
            return subprocess.CompletedProcess(args, 0, json.dumps({
                'Name': 'persistent_app_data', 'Driver': 'local',
                'Mountpoint': str(config['data_dir']), 'Options': None}), '')
        assert 'config' in args
        return subprocess.CompletedProcess(args, 0, json.dumps(resolved), '')
    monkeypatch.setattr(host, 'docker_command', execute)
    expected = host.wrapped_configuration(config, tx, 'target', 'b' * 32, 'sha256:' + 'c' * 64)
    assert expected['services']['app']['container_name'] == config['deployment_name']
    assert expected['volumes'] == {'app_data': {'name': 'persistent_app_data', 'external': True}}


@pytest.mark.parametrize('name', ['mobius-app-2', 'mobius_app_2', 'foreign-app-1'])
def test_worker_refuses_generated_name_that_cannot_fence_original_compose(tmp_path, monkeypatch, name):
    from tests.test_mobius_rebuild_host import _frozen
    config, _ = _frozen(tmp_path, monkeypatch)
    config.update(deployment_name=name, name_source='generated')
    with pytest.raises(ValueError, match='generated'):
        host.validate_config(config, trusted_uid=os.getuid())


@pytest.mark.parametrize('stalled', [False, True])
def test_managed_health_discovery_respects_remaining_wait_budget(case, monkeypatch, stalled):
    c = case
    cid = issued_target(c, enter=True)
    assert c.docker.complete_start(cid)
    original = c.host.docker_command
    started = c.clock[0]
    budgets = []

    def inspect(args, **kwargs):
        assert args == ['docker', 'container', 'inspect', '--format', '{{json .}}',
                        c.config['deployment_name']]
        budget = kwargs.get('timeout', 10)
        budgets.append((c.clock[0] - started, budget))
        if stalled:
            c.clock[0] += budget
            raise subprocess.TimeoutExpired(args, budget)
        return original(args, **kwargs)

    monkeypatch.setattr(c.host, 'docker_command', inspect)
    if stalled:
        with pytest.raises(subprocess.TimeoutExpired):
            c.host.wait_healthy(c.config, timeout=1)
    else:
        assert c.host.wait_healthy(c.config, timeout=1)
    assert budgets
    assert all(0 < budget <= min(.5, 1 - elapsed) for elapsed, budget in budgets)
    assert c.clock[0] - started <= 1
