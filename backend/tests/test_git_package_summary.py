"""Incoming discovery binds every byte without retaining opaque payloads."""
import asyncio
import dataclasses
import json
import subprocess
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import app_git, install
from tests.test_git_package_stream import _commit, _icon, _tree, repo
from tests.test_apps_install import bypass_url_validation


URL = 'https://example.invalid/package/mobius.json'


def _full(repo, commit, **kwargs):
  return install.read_git_install_candidate(repo, commit, URL, strict=False, **kwargs)


def test_summary_is_full_candidate_identity_without_retained_bodies(repo):
  tree = _tree(); commit = _commit(repo, tree)
  full = _full(repo, commit)
  summary = install.read_git_package_summary(repo, commit)
  assert summary.commit == full.commit
  assert summary.manifest == full.manifest
  assert summary.source_digest == full.source_digest
  assert summary.icon_warning == full.candidate.icon_warning
  assert all(isinstance(value, bytes) for value in full.candidate.static_assets.values())
  # A borrowed file object would fail deepcopy; only settled metadata/source leaves.
  fields = dataclasses.asdict(summary)
  assert set(fields) == {'commit', 'manifest', 'source_digest', 'icon_warning'}


@pytest.mark.parametrize('field', ['manifest', 'capability', 'entry', 'source', 'icon', 'static', 'seed', 'inline', 'job'])
def test_every_package_input_changes_the_same_incoming_identity(repo, field):
  tree = _tree(); before_commit = _commit(repo, tree)
  before = install.read_git_package_summary(repo, before_commit)
  if field in ('manifest', 'capability', 'inline'):
    manifest = json.loads(tree['mobius.json'])
    if field == 'manifest': manifest['name'] = 'Changed'
    elif field == 'capability': manifest['permissions']['manage_apps'] = True
    else: manifest['storage_seeds']['inline.json'] = {'value': 3}
    tree['mobius.json'] = json.dumps(manifest).encode()
  elif field == 'icon': tree['icon.png'] = _icon('blue')
  else:
    name = {'entry': 'index.jsx', 'source': 'cards.js', 'static': 'large.bin', 'seed': 'seed.json', 'job': 'fetch.sh'}[field]
    tree[name] += b'\nchanged\n'
  commit = _commit(repo, tree)
  summary = install.read_git_package_summary(repo, commit)
  assert summary.source_digest != before.source_digest
  assert summary.source_digest == _full(repo, commit).source_digest


@pytest.mark.parametrize('missing', ['mobius.json', 'index.jsx', 'cards.js', 'large.bin', 'seed.json', 'fetch.sh', 'icon.png'])
def test_missing_incoming_inputs_have_identical_closed_failure(repo, missing):
  tree = _tree(); tree.pop(missing); commit = _commit(repo, tree)
  with pytest.raises(ValueError) as full:
    _full(repo, commit)
  with pytest.raises(ValueError) as summary:
    install.read_git_package_summary(repo, commit)
  assert str(summary.value) == str(full.value)


@pytest.mark.parametrize('body', [b'broken json', b'{"id":"incomplete"}', b'[]', b'null'])
def test_invalid_incoming_manifest_has_same_failure(repo, body):
  tree = _tree(); tree['mobius.json'] = body; commit = _commit(repo, tree)
  with pytest.raises((ValueError, HTTPException)) as full:
    _full(repo, commit)
  with pytest.raises(type(full.value)) as summary:
    install.read_git_package_summary(repo, commit)
  assert str(summary.value) == str(full.value)


def test_invalid_icon_remains_nonfatal_and_identity_exact(repo):
  tree = _tree(); tree['icon.png'] = b'not an image'; commit = _commit(repo, tree)
  full = _full(repo, commit); summary = install.read_git_package_summary(repo, commit)
  assert summary.icon_warning and summary.icon_warning == full.candidate.icon_warning
  assert summary.source_digest == full.source_digest


def test_summary_never_materializes_opaque_asset_or_seed(repo, monkeypatch):
  tree = _tree(); tree['seed.json'] = b'public seed bytes\x00\xff' * 20000
  tree['index.jsx'] += b' // source' * 10000
  tree['cards.js'] += b' // module' * 10000
  commit = _commit(repo, tree); expected = _full(repo, commit).source_digest
  original = app_git.GitTreeBlob.read_bytes
  def read(blob):
    assert blob.size < 4096, 'Opaque inputs must stream'
    return original(blob)
  monkeypatch.setattr(app_git.GitTreeBlob, 'read_bytes', read)
  assert install.read_git_package_summary(repo, commit).source_digest == expected


def test_failed_incoming_digest_closes_spool(repo, monkeypatch):
  commit = _commit(repo, _tree()); seen = []
  def fail(blob):
    seen.append(blob._stream)
    raise RuntimeError('synthetic hash failure')
    yield b''
  monkeypatch.setattr(app_git.GitTreeBlob, 'chunks', fail)
  with pytest.raises(RuntimeError, match='synthetic hash failure'):
    install.read_git_package_summary(repo, commit)
  assert seen and all(stream.closed for stream in seen)


def test_summary_uses_reviewed_commit_not_later_working_source(repo):
  tree = _tree(); commit = _commit(repo, tree); expected = _full(repo, commit)
  (repo / 'cards.js').write_bytes(b'unaccepted source')
  summary = install.read_git_package_summary(repo, commit.upper())
  assert summary.source_digest == expected.source_digest


def test_missing_commit_does_not_fetch_or_fall_back(repo):
  with pytest.raises(ValueError, match='reviewed Git commit is unavailable'):
    install.read_git_package_summary(repo, 'not-a-reviewed-commit')


def test_full_and_summary_fetch_share_origin_guard_before_any_fetch(repo):
  subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://example.invalid/actual.git'], check=True)
  wrong = ('https://example.invalid/wrong.git', 'main')
  with patch('app.app_git.fetch_origin_ref') as fetch, patch('app.install._derive_repo_ref', return_value=wrong):
    for reader in [install.fetch_git_install_candidate, install.fetch_git_package_summary]:
      with pytest.raises(ValueError, match='origin does not match'):
        reader(repo, URL)
    fetch.assert_not_called()


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('changed', [False, True])
def test_real_update_route_streams_source_or_rereads_exact_legacy_commit(
  client, auth, bypass_url_validation, monkeypatch, legacy, changed,
):
  from pathlib import Path
  from app.config import get_settings
  from tests.test_apps_install import _install_v1, JSX

  base = 'https://incoming-stream.test/package/'
  manifest = {
    'id': 'incoming-stream', 'name': 'Incoming Stream', 'version': '1.0.0',
    'description': 'Synthetic route fixture', 'entry': 'index.jsx',
  }
  body = JSX + '\n// synthetic padding' * 1000
  response = _install_v1(client, auth, base, manifest, body)
  assert response.status_code == 201, response.text
  source = Path(get_settings().data_dir) / 'apps' / manifest['id']
  recorded = {'index.jsx': body.encode()}
  if not legacy:
    recorded['mobius.json'] = json.dumps(manifest).encode()
  app_git.record_upstream(source, recorded, base+'mobius.json', '1.0.0')
  incoming = body.replace('ok', 'different') if changed else body
  commit = _commit(source, {
    'mobius.json': json.dumps(manifest).encode(), 'index.jsx': incoming.encode(),
  })

  def fetched(repo, url):
    return install.read_git_package_summary(repo, commit)
  monkeypatch.setattr(install, 'fetch_git_package_summary', fetched)
  def full_fetch(*args, **kwargs):
    pytest.fail('Update discovery must not fetch the full install candidate')
  monkeypatch.setattr(install, 'fetch_git_install_candidate', full_fetch)
  original_reader = install.read_git_install_candidate
  legacy_reads = []
  def full_reader(repo, ref, url, **kwargs):
    assert legacy, 'Modern discovery must not materialize incoming source'
    assert ref == commit, 'Legacy must use the candidate already reviewed'
    legacy_reads.append(ref)
    return original_reader(repo, ref, url, **kwargs)
  monkeypatch.setattr(install, 'read_git_install_candidate', full_reader)
  if not legacy:
    def forbidden(*args, **kwargs):
      pytest.fail('Modern route must not retain either full source tree')
    monkeypatch.setattr(app_git, 'read_ref_tree', forbidden)
    original_bytes = app_git.GitTreeBlob.read_bytes
    def bytes_without_source(blob):
      assert blob.size != len(incoming.encode()), 'Incoming source must stream'
      return original_bytes(blob)
    monkeypatch.setattr(app_git.GitTreeBlob, 'read_bytes', bytes_without_source)
  checked = client.get(f"/api/apps/{response.json()['id']}/update-check", headers=auth)
  assert checked.status_code == 200, checked.text
  assert checked.json()['update_available'] is changed
  assert legacy_reads == ([commit] if legacy else [])


@pytest.mark.parametrize('missing', ['mobius.json', 'index.jsx', 'cards.js', 'large.bin', 'seed.json', 'fetch.sh', 'icon.png'])
def test_listed_but_unavailable_incoming_object_fails_identically(repo, missing):
  tree = _tree(); commit = _commit(repo, tree)
  oid = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', commit+':'+missing], text=True).strip()
  # Remove only this test's own loose object, not the declared tree path.
  (repo / '.git' / 'objects' / oid[:2] / oid[2:]).unlink()
  with pytest.raises(ValueError) as full:
    _full(repo, commit)
  with pytest.raises(ValueError) as summary:
    install.read_git_package_summary(repo, commit)
  assert type(full.value) is type(summary.value)
  assert str(full.value) == str(summary.value)
