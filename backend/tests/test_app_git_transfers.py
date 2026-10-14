"""App Git transfers run outside every app lock, against a real HTTP server.

``git http-backend`` serves bare repositories over HTTP. Its pack responses can
be paused, throttled or stalled, so these tests exercise Git's own transport,
the low-speed limit and the transfer ceiling rather than a mocked subprocess.
"""

import asyncio
import collections
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app import app_git, fs_locks, git_transfers, install, models, pending_update
from app.config import get_settings
from tests.test_apps_install import (  # noqa: F401 - fixture
  JSX, _fake_async_client, _fixture_commit, bypass_url_validation,
)


class _GitHandler(BaseHTTPRequestHandler):
  def log_message(self, *_args):
    pass

  def do_GET(self):
    self._serve()

  def do_POST(self):
    self._serve()

  def _serve(self):
    server = self.server
    path, _, query = self.path.partition("?")
    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
    out = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env={
      "PATH": os.environ["PATH"], "HOME": str(server.root), "GIT_CONFIG_NOSYSTEM": "1",
      "GIT_PROJECT_ROOT": str(server.root), "GIT_HTTP_EXPORT_ALL": "1",
      "PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": self.command,
      "CONTENT_TYPE": self.headers.get("Content-Type", ""),
      "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1",
      "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
      "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
    }).stdout
    head, _, payload = out.partition(b"\r\n\r\n")
    status, headers = 200, []
    for line in head.decode().splitlines():
      key, _, value = line.partition(":")
      if key.lower() == "status":
        status = int(value.split()[0])
      else:
        headers.append((key, value.strip()))
    self.send_response(status)
    for key, value in headers:
      self.send_header(key, value)
    self.end_headers()
    name = path.strip("/").split("/")[0].removesuffix(".git")
    pack = path.endswith("/git-upload-pack")
    if pack:
      with server.lock:
        server.packs[name] += 1
        paused = name in server.pause_next
        server.pause_next.discard(name)
      if paused:
        server.waiting.set()
        server.flowing.wait()
    for start in range(0, len(payload), 4096):
      if pack and server.rate:
        time.sleep(4096 / server.rate)
      try:
        self.wfile.write(payload[start:start + 4096])
        self.wfile.flush()
      except (BrokenPipeError, ConnectionResetError):
        return


class _GitServer(ThreadingHTTPServer):
  daemon_threads = True

  def __init__(self, root: Path):
    super().__init__(("127.0.0.1", 0), _GitHandler)
    self.root = root
    self.lock = threading.Lock()
    self.reset()

  def reset(self):
    self.flowing = threading.Event()
    self.flowing.set()
    self.waiting = threading.Event()
    self.pause_next: set[str] = set()
    self.rate = 0
    self.packs = collections.Counter()

  def pause(self, name: str) -> None:
    """Hold the next pack response for ``name`` until ``release``."""
    self.flowing.clear()
    self.waiting.clear()
    self.pause_next.add(name)

  def release(self) -> None:
    self.flowing.set()

  def url(self, name: str) -> str:
    return f"http://127.0.0.1:{self.server_address[1]}/{name}.git"

  def publish(self, name: str, *, big: bool = False) -> dict:
    """Serve one app package repository; returns its manifest."""
    manifest = {
      "id": name, "name": name.title(), "version": "1.0.0",
      "description": "Served over HTTP", "entry": "index.jsx",
    }
    work = self.root / f"{name}-work"
    if big:
      subprocess.run(["git", "clone", "-q", str(self.big), str(work)], check=True)
    else:
      subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    self.commit(name, manifest, JSX)
    subprocess.run(
      ["git", "clone", "-q", "--bare", str(work), str(self.root / f"{name}.git")],
      check=True,
    )
    return manifest

  def commit(self, name: str, manifest: dict, jsx: str, *, push: bool = False) -> str:
    work = self.root / f"{name}-work"
    (work / "mobius.json").write_text(json.dumps(manifest))
    (work / "index.jsx").write_text(jsx)
    commit = _fixture_commit(work, manifest["version"])
    if push:
      subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(self.root / f"{name}.git"), "HEAD:main"],
        check=True,
      )
    return commit


@pytest.fixture(scope="module")
def _served(tmp_path_factory):
  root = tmp_path_factory.mktemp("served-git")
  big = root / "big-base"
  subprocess.run(["git", "init", "-q", "-b", "main", str(big)], check=True)
  # About 5 MB that compression cannot shrink, so a transfer takes real time.
  for index in range(5):
    (big / f"blob-{index}.bin").write_bytes(os.urandom(1024 * 1024))
  _fixture_commit(big, "large history")
  server = _GitServer(root)
  server.big = big
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  yield server
  server.release()
  server.shutdown()


@pytest.fixture
def git_server(_served, monkeypatch):
  _served.reset()
  # Map a catalog URL's GitHub repository onto the served repository.
  real = install._derive_repo_ref

  def served(url):
    derived = real(url)
    if derived is None:
      return None
    repo, ref = derived
    return _served.url(repo.rsplit("/", 1)[1].removesuffix(".git")), ref

  monkeypatch.setattr(install, "_derive_repo_ref", served)
  # Module-level asyncio locks bind to the first loop that contends them.
  monkeypatch.setattr(fs_locks, "_lifecycle_lock", asyncio.Lock())
  monkeypatch.setattr(fs_locks, "_source_locks", weakref.WeakValueDictionary())
  yield _served
  _served.release()


@pytest.fixture
def transfers(monkeypatch):
  """Every network transfer, with the app locks held when it started."""
  calls = []
  real = app_git._transfer

  def guarded(stage, url, ref, **kwargs):
    calls.append((
      fs_locks._lifecycle_lock.locked(),
      [key for key, lock in list(fs_locks._source_locks.items()) if lock.locked()],
    ))
    return real(stage, url, ref, **kwargs)

  monkeypatch.setattr(app_git, "_transfer", guarded)
  return calls


def _base(name: str) -> str:
  return f"https://raw.githubusercontent.com/test/{name}/main/"


def _responses(name: str, manifest: dict, jsx: str = JSX) -> dict:
  return {
    _base(name) + "mobius.json": (200, json.dumps(manifest).encode()),
    _base(name) + "index.jsx": (200, jsx.encode()),
  }


def _repo(name: str) -> Path:
  return Path(get_settings().data_dir) / "apps" / name


def _no_leftovers() -> bool:
  root = git_transfers.git_transfer_root()
  return not root.exists() or not any(root.iterdir())


def _fsck(repo: Path) -> None:
  app_git._run(repo, "fsck", "--connectivity-only", "--no-dangling")


def _run_async(scenario, *responses):
  """Run concurrent requests on one event loop, as the server does."""
  from app.main import app as application

  merged = {}
  for response in responses:
    merged.update(response)

  async def main():
    async with httpx.AsyncClient(
      transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
      with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(merged)):
        return await scenario(client)

  return asyncio.run(main())


def _stage_clone(url: str, ref: str = "main"):
  return install._stage(app_git.stage_clone, url, ref, git_transfers.git_transfer_root())


async def _paused(server: _GitServer) -> None:
  assert await asyncio.to_thread(server.waiting.wait, 30), "transfer never started"


def test_no_transfer_runs_under_an_app_lock(
  client, auth, git_server, transfers, bypass_url_validation,
):
  """Fresh, reviewed and URL installs, update-check, preview, resolver, resolve."""
  manifest = git_server.publish("invariant")
  responses = _responses("invariant", manifest)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
    fresh = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("invariant") + "mobius.json",
    })
  assert fresh.status_code == 201, fresh.text
  app_id = fresh.json()["id"]
  repo = _repo("invariant")
  assert len(transfers) == 1

  v2 = {**manifest, "version": "2.0.0"}
  head = git_server.commit("invariant", v2, JSX.replace("ok", "v2"), push=True)
  checked = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
  assert checked.status_code == 200 and checked.json()["update_available"] is True
  preview = client.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth)
  assert preview.status_code == 200, preview.text
  assert preview.json()["upstream_commit"] == head
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses("invariant", v2, JSX.replace("ok", "v2")),
  )):
    reviewed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("invariant") + "mobius.json",
      "update_app_id": app_id,
      "reviewed_upstream_commit": head,
      "reviewed_source_digest": preview.json()["source_digest"],
    })
  assert reviewed.status_code == 201, reviewed.text
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH) == head

  v3 = {**manifest, "version": "3.0.0"}
  head = git_server.commit("invariant", v3, JSX.replace("ok", "v3"), push=True)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses("invariant", v3, JSX.replace("ok", "v3")),
  )):
    by_url = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("invariant") + "mobius.json",
    })
  assert by_url.status_code == 201, by_url.text
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH) == head

  # A legacy depth-one fetch left upstream grafted, hiding its merge base. Both
  # resolver paths complete the history before they take a lock; neither has a
  # pending update to act on afterwards.
  v4 = git_server.commit("invariant", {**manifest, "version": "4.0.0"}, JSX, push=True)
  app_git._run(repo, "fetch", "-q", "origin", f"+main:refs/heads/{app_git.UPSTREAM_BRANCH}")
  for request in (
    lambda: client.post(f"/api/apps/{app_id}/conflict-resolver-chat", headers=auth),
    lambda: client.post("/api/apps/resolve-update", headers=auth, json={
      "source_dir": str(repo),
    }),
  ):
    (repo / ".git" / "shallow").write_text(v4 + "\n")
    count = len(transfers)
    assert request().status_code in (200, 404, 409)
    assert len(transfers) == count + 1
    assert not (repo / ".git" / "shallow").exists()

  # Install, check, preview, URL update, resolver, resolve; the reviewed
  # install found the previewed commit already present.
  assert len(transfers) == 6
  assert all(call == (False, []) for call in transfers), transfers
  assert _no_leftovers()
  _fsck(repo)


def test_bootstrap_transfers_before_installing(git_server, transfers, db, monkeypatch):
  from app import bootstrap

  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  git_server.publish("bootstrap-source")
  monkeypatch.setattr(install, "_derive_repo_ref", lambda _url: (
    git_server.url("bootstrap-source"), "main",
  ))
  staged = []

  async def install_from_manifest(_db, **kwargs):
    staged.append(kwargs["prepared"].staged_clone)
    assert staged[-1].path.is_dir()
    raise RuntimeError("stop after the transfer")

  monkeypatch.setattr(bootstrap, "install_from_manifest", install_from_manifest)
  asyncio.run(bootstrap.ensure_bootstrap_apps_installed(db))
  assert staged and len(transfers) == len(staged)
  assert all(call == (False, []) for call in transfers)
  assert not any(clone.path.exists() for clone in staged)


def test_slow_install_blocks_no_other_app(
  client, auth, git_server, monkeypatch, bypass_url_validation,
):
  """While one large transfer is paused, other lifecycle work completes."""
  big = git_server.publish("large", big=True)
  other = git_server.publish("checked")
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses("checked", other),
  )):
    checked_app = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("checked") + "mobius.json",
    })
  assert checked_app.status_code == 201, checked_app.text
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client({
    "https://doomed.test/app/index.jsx": (200, JSX.encode()),
  })):
    doomed = client.post("/api/apps/install", headers=auth, json={
      "manifest": {**other, "id": "doomed", "name": "Doomed"},
      "raw_base": "https://doomed.test/app/",
    })
  assert doomed.status_code == 201, doomed.text
  git_server.commit("checked", {**other, "version": "2.0.0"}, JSX, push=True)

  # Shrink the local-command ceiling: the transfer is held to its own.
  local_ceiling = 3
  real_run = app_git._run
  monkeypatch.setattr(app_git, "_run", lambda *args, timeout=app_git._GIT_TIMEOUT, **kwargs: real_run(
    *args, timeout=local_ceiling if timeout == app_git._GIT_TIMEOUT else timeout, **kwargs,
  ))
  git_server.pause("large")

  async def scenario(http):
    started = time.monotonic()
    large = asyncio.create_task(http.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("large") + "mobius.json",
    }))
    await _paused(git_server)
    small = await asyncio.wait_for(http.post("/api/apps/install", headers=auth, json={
      "manifest": {**other, "id": "small", "name": "Small"},
      "raw_base": "https://small.test/app/",
    }), 20)
    removed = await asyncio.wait_for(
      http.delete(f"/api/apps/{doomed.json()['id']}", headers=auth), 20,
    )
    checked = await asyncio.wait_for(
      http.get(f"/api/apps/{checked_app.json()['id']}/update-check", headers=auth), 20,
    )
    assert not large.done(), "the paused transfer must still be running"
    await asyncio.sleep(max(0, local_ceiling + 1 - (time.monotonic() - started)))
    git_server.release()
    return small, removed, checked, await asyncio.wait_for(large, 120), time.monotonic() - started

  small, removed, checked, large, elapsed = _run_async(
    scenario, _responses("large", big),
    {"https://small.test/app/index.jsx": (200, JSX.encode())},
  )
  assert small.status_code == 201, small.text
  assert removed.status_code == 204, removed.text
  assert checked.status_code == 200 and checked.json()["update_available"] is True
  assert large.status_code == 201, large.text
  assert elapsed > local_ceiling
  assert (_repo("large") / "blob-4.bin").stat().st_size == 1024 * 1024
  assert _no_leftovers()


def test_stalled_transfer_fails_fast_and_cleanly(git_server, monkeypatch):
  git_server.publish("stalled", big=True)
  monkeypatch.setattr(app_git, "_GIT_LOW_SPEED_TIME", 2)
  git_server.pause("stalled")
  started = time.monotonic()
  with pytest.raises(app_git.GitTransferFailed):
    asyncio.run(_stage_clone(git_server.url("stalled"), "main"))
  assert time.monotonic() - started < 30
  assert _no_leftovers()


def test_transfer_ceiling_stops_every_git_process(git_server, monkeypatch):
  git_server.publish("trickle", big=True)
  git_server.rate = 8192  # above the low-speed limit, far too slow for 5 MB
  monkeypatch.setattr(app_git, "_GIT_TRANSFER_TIMEOUT", 2)
  groups = []
  real_popen = subprocess.Popen

  class Recording(real_popen):
    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      groups.append(self.pid)

  monkeypatch.setattr(app_git.subprocess, "Popen", Recording)
  with pytest.raises(app_git.GitTransferTimeout):
    asyncio.run(_stage_clone(git_server.url("trickle"), "main"))
  deadline = time.monotonic() + 5
  for group in groups:
    while True:
      try:
        os.killpg(group, 0)
      except ProcessLookupError:
        break
      assert time.monotonic() < deadline, f"git process group {group} survived"
      time.sleep(0.05)
  assert _no_leftovers()


def _install_served(client, auth, git_server, name: str) -> tuple[int, dict]:
  manifest = git_server.publish(name)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, manifest),
  )):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert response.status_code == 201, response.text
  return response.json()["id"], manifest


@pytest.mark.parametrize("change", ["purge", "rename", "origin"])
def test_source_change_during_transfer_refuses_the_import(
  client, auth, git_server, bypass_url_validation, change,
):
  name = f"moving-{change}"
  app_id, manifest = _install_served(client, auth, git_server, name)
  head = git_server.commit(name, {**manifest, "version": "2.0.0"}, JSX, push=True)
  repo = _repo(name)
  git_server.pause(name)

  async def scenario(http):
    preview = asyncio.create_task(
      http.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth),
    )
    await _paused(git_server)
    # The same locked mutations uninstall, rename and origin migration make.
    async with fs_locks.source_dir_lock(str(repo)):
      if change == "purge":
        shutil.rmtree(repo)
      elif change == "rename":
        repo.rename(repo.with_name(name + "-moved"))
      else:
        app_git.set_origin_url(repo, git_server.url("elsewhere"))
    git_server.release()
    return await asyncio.wait_for(preview, 60)

  preview = _run_async(scenario)
  assert preview.status_code == 409, preview.text
  if change == "origin":
    assert app_git._resolve_commit(repo, head) is None
    _fsck(repo)
  else:
    assert not repo.exists(), "an import must never recreate a moved repository"
  assert _no_leftovers()


def test_concurrent_update_checks_import_one_commit(
  client, auth, git_server, bypass_url_validation,
):
  app_id, manifest = _install_served(client, auth, git_server, "twice")
  git_server.commit("twice", {**manifest, "version": "2.0.0"}, JSX.replace("ok", "v2"), push=True)

  async def scenario(http):
    return await asyncio.gather(*(
      http.get(f"/api/apps/{app_id}/update-check", headers=auth) for _ in range(2)
    ))

  first, second = _run_async(scenario)
  assert first.status_code == second.status_code == 200
  assert first.json()["update_available"] is second.json()["update_available"] is True
  assert first.json()["candidate_source_digest"] == second.json()["candidate_source_digest"]
  assert git_server.packs["twice"] >= 3
  _fsck(_repo("twice"))
  assert _no_leftovers()


def test_reinstall_at_same_path_during_transfer(
  client, auth, git_server, bypass_url_validation,
):
  app_id, manifest = _install_served(client, auth, git_server, "again")
  v2 = {**manifest, "version": "2.0.0"}
  git_server.commit("again", v2, JSX.replace("ok", "v2"), push=True)
  git_server.pause("again")

  async def scenario(http):
    checking = asyncio.create_task(http.get(f"/api/apps/{app_id}/update-check", headers=auth))
    await _paused(git_server)
    removed = await asyncio.wait_for(http.delete(f"/api/apps/{app_id}", headers=auth), 20)
    reinstalled = await asyncio.wait_for(http.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("again") + "mobius.json",
    }), 30)
    git_server.release()
    return removed, reinstalled, await asyncio.wait_for(checking, 60)

  removed, reinstalled, checked = _run_async(scenario, _responses("again", v2, JSX.replace("ok", "v2")))
  assert removed.status_code == 204, removed.text
  assert reinstalled.status_code == 201, reinstalled.text
  assert reinstalled.json()["id"] == app_id
  assert checked.status_code == 200, checked.text
  _fsck(_repo("again"))
  assert _no_leftovers()


def test_two_concurrent_fresh_installs_of_one_package(
  auth, git_server, bypass_url_validation, db,
):
  manifest = git_server.publish("racing")
  git_server.pause("racing")

  async def scenario(http):
    request = lambda: http.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("racing") + "mobius.json",
    })
    first = asyncio.create_task(request())
    await _paused(git_server)
    second = await asyncio.wait_for(request(), 30)
    git_server.release()
    return second, await asyncio.wait_for(first, 60)

  second, first = _run_async(scenario, _responses("racing", manifest))
  assert second.status_code == 201, second.text
  assert first.status_code == 201, first.text
  assert {second.json()["mode"], first.json()["mode"]} == {"install", "update"}
  assert db.query(models.App).filter_by(slug="racing").count() == 1
  _fsck(_repo("racing"))
  assert _no_leftovers()


def test_process_killed_during_staging_leaves_a_clean_repo(
  client, auth, git_server, bypass_url_validation, tmp_path,
):
  _app_id, manifest = _install_served(client, auth, git_server, "killed")
  git_server.commit("killed", {**manifest, "version": "2.0.0"}, JSX, push=True)
  repo = _repo("killed")
  before = app_git._run(repo, "for-each-ref").stdout
  git_server.pause("killed")
  backend = Path(__file__).resolve().parents[1]
  child = subprocess.Popen([sys.executable, "-c", (
    "import sys; from pathlib import Path; from app import app_git;"
    "app_git.stage_fetch(sys.argv[1], sys.argv[2], 'main', Path(sys.argv[3]))"
  ), str(repo), git_server.url("killed"), str(git_transfers.git_transfer_root())], cwd=backend)
  try:
    assert git_server.waiting.wait(30)
    os.kill(child.pid, signal.SIGKILL)
    child.wait(10)
    # Its Git child runs in its own session; a dying host would lose it too.
    subprocess.run(["pkill", "-KILL", "-f", str(git_transfers.git_transfer_root())])
  finally:
    git_server.release()
  assert not _no_leftovers(), "a killed process leaves its staging behind"
  assert app_git._run(repo, "for-each-ref").stdout == before
  _fsck(repo)
  from app import startup
  startup._sweep_git_transfers(None)
  assert _no_leftovers()


def test_killed_import_leaves_the_repo_consistent(tmp_path, monkeypatch, git_server):
  """SIGKILL the local import mid-pack; refs stay put and a retry completes."""
  manifest = git_server.publish("import-kill")
  repo = tmp_path / "live"
  app_git.adopt_clone(
    app_git.stage_clone(git_server.url("import-kill"), "main", tmp_path / "t"), repo,
  )
  head = git_server.commit("import-kill", {**manifest, "version": "2.0.0"}, JSX, push=True)
  incoming = app_git.stage_fetch(repo, git_server.url("import-kill"), "main", tmp_path / "t")
  before = app_git._run(repo, "for-each-ref").stdout
  # Hold the local upload-pack so the import is still running when killed.
  hook = tmp_path / "slow-pack"
  hook.write_text("#!/bin/sh\nsleep 30\nexec \"$@\"\n")
  hook.chmod(0o755)
  config = Path(os.environ["GIT_CONFIG_GLOBAL"])
  original = config.read_text() if config.exists() else ""
  config.write_text(original + f"[uploadpack]\n\tpackObjectsHook = {hook}\n")
  fetches = []
  real_popen = subprocess.Popen

  class Recording(real_popen):
    def __init__(self, args, *rest, **kwargs):
      super().__init__(args, *rest, **kwargs)
      if "fetch" in args:
        fetches.append(self.pid)

  errors = []

  def run_import():
    try:
      app_git.import_staged(repo, incoming)
    except subprocess.CalledProcessError as exc:
      errors.append(exc)

  with monkeypatch.context() as patched:
    patched.setattr(app_git.subprocess, "Popen", Recording)
    importing = threading.Thread(target=run_import)
    importing.start()
    deadline = time.monotonic() + 10
    while not fetches and time.monotonic() < deadline:
      time.sleep(0.05)
    time.sleep(0.5)
    os.killpg(fetches[0], signal.SIGKILL)
    importing.join(10)
  config.write_text(original)
  assert errors, "the killed import must fail"
  assert app_git._run(repo, "for-each-ref").stdout == before
  _fsck(repo)
  assert app_git.import_staged(repo, incoming) == head
  _fsck(repo)


def test_visible_merge_base_needs_no_transfer_in_a_shallow_repo(
  client, auth, git_server, transfers, bypass_url_validation,
):
  """Resolver preparation stays offline when the shallow boundary hides nothing."""
  from app.routes import apps as apps_routes

  _app_id, _manifest = _install_served(client, auth, git_server, "visible-base")
  repo = _repo("visible-base")
  (repo / ".git" / "shallow").write_text(app_git.head_sha(repo, "main") + "\n")
  git_server.release()
  shutil.rmtree(git_server.root / "visible-base.git")  # no network to reach
  count = len(transfers)
  asyncio.run(apps_routes._complete_history(repo))
  assert len(transfers) == count
  assert (repo / ".git" / "shallow").exists()


@pytest.mark.parametrize("stage", ["clone", "fetch"])
def test_cancelled_transfer_still_discards_its_staging(
  client, auth, git_server, bypass_url_validation, stage,
):
  name = f"cancelled-{stage}"
  if stage == "clone":
    git_server.publish(name, big=True)
    start = lambda: _stage_clone(git_server.url(name), "main")
  else:
    _app_id, manifest = _install_served(client, auth, git_server, name)
    git_server.commit(name, {**manifest, "version": "2.0.0"}, JSX, push=True)
    start = lambda: install.fetch_app_commit(_repo(name), "main")
  git_server.pause(name)

  async def scenario():
    task = asyncio.create_task(start())
    await _paused(git_server)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert not _no_leftovers(), "the transfer thread is still running"
    git_server.release()
    deadline = time.monotonic() + 60
    while not _no_leftovers() and time.monotonic() < deadline:
      await asyncio.sleep(0.1)

  asyncio.run(scenario())
  assert _no_leftovers()


def test_cancelled_import_keeps_the_source_lock_until_it_finishes(
  client, auth, git_server, bypass_url_validation, monkeypatch,
):
  _app_id, manifest = _install_served(client, auth, git_server, "cancel-import")
  head = git_server.commit("cancel-import", {**manifest, "version": "2.0.0"}, JSX, push=True)
  repo = _repo("cancel-import")
  entered, finish = threading.Event(), threading.Event()
  real_import = app_git.import_staged

  def slow_import(*args):
    entered.set()
    finish.wait(30)
    return real_import(*args)

  monkeypatch.setattr(app_git, "import_staged", slow_import)

  async def scenario():
    task = asyncio.create_task(install.fetch_app_commit(repo, "main"))
    assert await asyncio.to_thread(entered.wait, 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    # The request is gone; its shared import still holds the source lock.
    await asyncio.sleep(0.2)
    assert fs_locks.source_dir_lock(str(repo)).locked()
    finish.set()
    deadline = time.monotonic() + 30
    while fs_locks.source_dir_lock(str(repo)).locked() and time.monotonic() < deadline:
      await asyncio.sleep(0.05)
    assert not fs_locks.source_dir_lock(str(repo)).locked()
    while git_transfers._FETCHES and time.monotonic() < deadline:
      await asyncio.sleep(0.05)

  asyncio.run(scenario())
  assert app_git._resolve_commit(repo, head) == head
  assert _no_leftovers()


def _status(repo: Path) -> dict:
  from app import source_status
  return source_status.build_app_status({"id": 1, "source_dir": str(repo), "slug": repo.name})


def test_transfers_keep_origin_branch_for_source_status(
  client, auth, git_server, bypass_url_validation,
):
  """Source status compares an app with its last-fetched origin branch."""
  app_id, manifest = _install_served(client, auth, git_server, "status")
  repo = _repo("status")
  installed = _status(repo)
  first = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  assert installed["origin"]["ref"] == "origin/main"
  assert installed["origin"]["sha"] == installed["base_sha"] == first
  assert installed["state"] == "aligned"

  v2 = {**manifest, "version": "2.0.0"}
  head = git_server.commit("status", v2, JSX.replace("ok", "v2"), push=True)
  checked = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
  assert checked.status_code == 200 and checked.json()["update_available"] is True
  fetched = _status(repo)
  assert fetched["origin"]["sha"] == head
  assert fetched["base_sha"] == first

  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses("status", v2, JSX.replace("ok", "v2")),
  )):
    updated = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base("status") + "mobius.json",
    })
  assert updated.status_code == 201, updated.text
  current = _status(repo)
  assert current["origin"]["sha"] == current["base_sha"] == head
  assert current["state"] == "aligned"


def test_git_app_with_a_missing_icon_keeps_its_updates_available(
  client, auth, git_server, bypass_url_validation,
):
  """A declared icon the package lacks warns in discovery as it does in install."""
  name = "iconless"
  git_server.publish(name)
  manifest = {
    "id": name, "name": "Iconless", "version": "1.0.0",
    "description": "Declares an icon it does not ship", "entry": "index.jsx",
    "icon": "icon.png",
  }
  git_server.commit(name, manifest, JSX, push=True)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, manifest),
  )):
    installed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert installed.status_code == 201, installed.text
  assert any("icon" in warning for warning in installed.json()["warnings"])
  app_id = installed.json()["id"]

  v2 = {**manifest, "version": "2.0.0"}
  head = git_server.commit(name, v2, JSX.replace("ok", "v2"), push=True)
  checked = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
  assert checked.status_code == 200, checked.text
  assert checked.json()["update_available"] is True
  preview = client.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth)
  assert preview.status_code == 200, preview.text
  assert preview.json()["upstream_commit"] == head
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, v2, JSX.replace("ok", "v2")),
  )):
    reviewed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
      "update_app_id": app_id,
      "reviewed_upstream_commit": head,
      "reviewed_source_digest": preview.json()["source_digest"],
    })
  assert reviewed.status_code == 201, reviewed.text
  assert app_git.head_sha(_repo(name), app_git.UPSTREAM_BRANCH) == head


def test_concurrent_fetches_share_one_transfer(
  client, auth, git_server, transfers, bypass_url_validation,
):
  _app_id, manifest = _install_served(client, auth, git_server, "shared")
  head = git_server.commit("shared", {**manifest, "version": "2.0.0"}, JSX, push=True)
  repo = _repo("shared")
  count = len(transfers)
  git_server.pause("shared")

  async def scenario():
    first = asyncio.create_task(install.fetch_app_commit(repo, "main"))
    await _paused(git_server)
    second = asyncio.create_task(install.fetch_app_commit(repo, "main"))
    await asyncio.sleep(0.2)
    git_server.release()
    return await asyncio.gather(first, second)

  assert asyncio.run(scenario()) == [head, head]
  assert len(transfers) == count + 1
  assert not git_transfers._FETCHES
  assert _no_leftovers()


def test_stored_address_installs_a_renamed_git_package(
  client, auth, git_server, bypass_url_validation,
):
  """The pre-clone check sees the installed app's permanent package identity."""
  name = "rename-pkg"
  git_server.publish(name)
  manifest = {
    "id": "before-rename", "name": "Rename", "version": "1.0.0",
    "description": "Renamed package", "entry": "index.jsx",
    "package_id": "urn:uuid:9e136d55-9631-585a-aa75-a745a5dc8f2e",
  }
  git_server.commit(name, manifest, JSX, push=True)
  base = _base(name)
  responses = {
    **_responses(name, manifest),
    f"https://api.github.com/repos/test/{name}": (
      200, json.dumps({"id": 101, "full_name": f"test/{name}"}).encode(),
    ),
  }
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
    first = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
    assert first.status_code == 201, first.text
    renamed = {**manifest, "id": "after-rename", "version": "2.0.0"}
    git_server.commit(name, renamed, JSX, push=True)
    responses[base + "mobius.json"] = (200, json.dumps(renamed).encode())
    result = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base.rstrip("/") + "#manifest-id=before-rename",
    })
  assert result.status_code == 201, result.text
  assert result.json()["id"] == first.json()["id"]


def test_transfers_run_on_their_own_bounded_pool(
  client, auth, git_server, monkeypatch, bypass_url_validation,
):
  """A long transfer never occupies the default pool the server shares."""
  _app_id, manifest = _install_served(client, auth, git_server, "pooled")
  git_server.commit("pooled", {**manifest, "version": "2.0.0"}, JSX, push=True)
  threads = []
  real_transfer = app_git._transfer

  def recorded(*args, **kwargs):
    threads.append(threading.current_thread().name)
    return real_transfer(*args, **kwargs)

  monkeypatch.setattr(app_git, "_transfer", recorded)
  asyncio.run(install.fetch_app_commit(_repo("pooled"), "main"))
  assert threads and all(name.startswith("git-transfer") for name in threads)
  assert git_transfers._TRANSFER_EXECUTOR._max_workers == git_transfers._GIT_TRANSFER_WORKERS


def test_history_falls_back_to_the_origin_branch(
  client, auth, git_server, monkeypatch, bypass_url_validation,
):
  """A recorded upstream no longer fetchable by id still completes history."""
  from app.routes import apps as apps_routes

  _app_id, manifest = _install_served(client, auth, git_server, "fallback")
  repo = _repo("fallback")
  v2 = git_server.commit("fallback", {**manifest, "version": "2.0.0"}, JSX, push=True)
  app_git._run(repo, "fetch", "-q", "origin", f"+main:refs/heads/{app_git.UPSTREAM_BRANCH}")
  (repo / ".git" / "shallow").write_text(v2 + "\n")
  requested = []
  real_fetch = install.fetch_app_commit

  async def by_id_gone(source_dir, ref, **kwargs):
    requested.append(ref)
    if ref == v2:
      raise app_git.GitTransferFailed("not our ref")
    return await real_fetch(source_dir, ref, **kwargs)

  monkeypatch.setattr(install, "fetch_app_commit", by_id_gone)
  asyncio.run(apps_routes._complete_history(repo))
  assert requested == [v2, "main"]
  assert not (repo / ".git" / "shallow").exists()
  app_git.require_merge_history(repo)


def test_git_install_fetches_no_package_file_over_http_or_under_the_lock(
  client, auth, git_server, bypass_url_validation, monkeypatch,
):
  """The package is read from the staged clone: only the pre-clone manifest
  check and the identity lookup use HTTP, and neither holds the lock."""
  name = "one-download"
  git_server.publish(name)
  manifest = {
    "id": name, "name": "One Download", "version": "1.0.0",
    "description": "Downloaded once", "entry": "index.jsx",
    "package_id": "urn:uuid:4c5d8a2e-0f43-5d0e-9b1a-6e2f7c3d9a10",
  }
  git_server.commit(name, manifest, JSX, push=True)
  requests = []
  real_get = install._http_get

  async def recorded(client_, url, *args, **kwargs):
    requests.append((url, fs_locks._lifecycle_lock.locked()))
    return await real_get(client_, url, *args, **kwargs)

  monkeypatch.setattr(install, "_http_get", recorded)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client({
    **_responses(name, manifest),
    f"https://api.github.com/repos/test/{name}": (
      200, json.dumps({"id": 102, "full_name": f"test/{name}"}).encode(),
    ),
  })):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert response.status_code == 201, response.text
  assert [url for url, _locked in requests] == [
    _base(name) + "mobius.json", f"https://api.github.com/repos/test/{name}",
  ]
  assert not any(locked for _url, locked in requests)
  assert response.json()["package_id"] == manifest["package_id"]


def test_push_between_staging_and_install_installs_the_staged_commit(
  client, auth, git_server, bypass_url_validation, monkeypatch,
):
  """Raw file caches may already serve a newer push; the clone still decides."""
  name = "pushed-meanwhile"
  manifest = git_server.publish(name)
  staged_head = app_git.head_sha(git_server.root / f"{name}.git", "main")
  v2 = {**manifest, "version": "2.0.0"}
  real_stage = install._stage

  async def stage_then_push(*args):
    staged = await real_stage(*args)
    git_server.commit(name, v2, JSX.replace("ok", "pushed"), push=True)
    return staged

  monkeypatch.setattr(install, "_stage", stage_then_push)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, v2, JSX.replace("ok", "pushed")),
  )):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert response.status_code == 201, response.text
  assert response.json()["version"] == "1.0.0"
  repo = _repo(name)
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH) == staged_head
  assert (repo / "index.jsx").read_text() == JSX
  assert _no_leftovers()


@pytest.mark.parametrize("missing", ["repository", "branch"])
def test_permanent_fetch_failures_are_not_retryable(
  client, auth, git_server, bypass_url_validation, monkeypatch, missing,
):
  name = f"permanent-{missing}"
  manifest = git_server.publish(name)
  if missing == "repository":
    monkeypatch.setattr(install, "_derive_repo_ref", lambda _url: (
      git_server.url("no-such-repository"), "main",
    ))
  else:
    monkeypatch.setattr(install, "_derive_repo_ref", lambda _url: (
      git_server.url(name), "no-such-branch",
    ))
  with pytest.raises(app_git.GitTransferFailed) as raised:
    asyncio.run(_stage_clone(*install._derive_repo_ref("")))
  assert raised.value.transient is False
  assert install.transient_failure(raised.value) == (False, None)

  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, manifest),
  )):
    failed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert failed.status_code == 409, failed.text
  assert failed.json()["detail"]["code"] == "git_install_unavailable"
  assert failed.json()["detail"]["retryable"] is False
  # A permanent failure never tells the owner to retry.
  assert "retry" not in failed.json()["detail"]["message"].lower()
  assert _no_leftovers()


def test_unreachable_and_stalled_transfers_stay_retryable(git_server, monkeypatch):
  with socket.socket() as closed:
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
  with pytest.raises(app_git.GitTransferFailed) as refused:
    asyncio.run(_stage_clone(f"http://127.0.0.1:{port}/gone.git"))
  assert install.transient_failure(refused.value) == (True, None)

  git_server.publish("stalls", big=True)
  monkeypatch.setattr(app_git, "_GIT_LOW_SPEED_TIME", 2)
  git_server.pause("stalls")
  with pytest.raises(app_git.GitTransferFailed) as stalled:
    asyncio.run(_stage_clone(git_server.url("stalls")))
  assert install.transient_failure(stalled.value) == (True, None)
  assert _no_leftovers()


@pytest.mark.parametrize(("stderr", "transient"), [
  ("fatal: couldn't find remote ref refs/heads/gone", False),
  ("remote: Repository not found.\nfatal: repository 'x' not found", False),
  ("fatal: could not read Username for 'https://github.com': "
   "terminal prompts disabled", False),
  ("fatal: unable to access 'x': The requested URL returned error: 404", False),
  ("fatal: write error: No space left on device", False),
  ("fatal: unable to access 'x': The requested URL returned error: 503", True),
  ("fatal: unable to access 'x': Failed to connect to host port 443", True),
  ("error: RPC failed; curl 56 Recv failure: Connection reset by peer", True),
])
def test_fetch_errors_are_classified_from_git_text(stderr, transient):
  assert app_git._transient_fetch_error(stderr) is transient


@pytest.mark.parametrize(("errno_", "transient"), [
  (socket.EAI_AGAIN, True), (socket.EAI_NONAME, False),
])
def test_unresolved_git_host_is_transient_only_while_the_resolver_fails(
  monkeypatch, errno_, transient,
):
  def resolve(*_args):
    raise socket.gaierror(errno_, "name resolution failed")

  monkeypatch.setattr(app_git.socket, "getaddrinfo", resolve)
  stderr = "fatal: unable to access 'x': Could not resolve host: example.test"
  assert app_git._transient_fetch_error(stderr) is transient


def test_changed_capabilities_refuse_a_git_install_before_any_transfer(
  client, auth, git_server, transfers, bypass_url_validation,
):
  name = "capabilities-first"
  manifest = git_server.publish(name)
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(
    _responses(name, manifest),
  )):
    refused = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
      "reviewed_capability_digest": "0" * 64,
    })
  assert refused.status_code == 409, refused.text
  assert refused.json()["detail"]["code"] == "capability_changed"
  assert transfers == []


def test_present_commit_check_accepts_the_same_package_and_names_a_mismatch(
  tmp_path, caplog,
):
  """A package fetched over HTTP (a publication handoff) is adopted from a
  present Git commit only when that commit holds the same package."""
  from fastapi import HTTPException

  base = "https://raw.githubusercontent.com/test/present/main/"
  repo = tmp_path / "present"
  subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
  manifest = {
    "id": "present", "name": "Present", "version": "1.0.0",
    "description": "Present commit", "entry": "index.jsx",
  }

  def commit(manifest_text: str, jsx: str) -> str:
    (repo / "mobius.json").write_text(manifest_text)
    (repo / "index.jsx").write_text(jsx)
    return _fixture_commit(repo, "c")

  same = commit(json.dumps(manifest), JSX)
  candidate = install.read_git_install_candidate(
    repo, same, base + "mobius.json",
  ).candidate
  install._require_git_candidate(repo, same, candidate)

  for changed, reason in (
    (commit(json.dumps(manifest), JSX.replace("ok", "drift")), "package files changed"),
    (commit(json.dumps({**manifest, "version": "2.0.0"}), JSX), "manifest changed"),
    (commit(json.dumps({"id": "present"}), JSX), "invalid mobius.json"),
  ):
    with pytest.raises(HTTPException) as refused:
      install._require_git_candidate(repo, changed, candidate)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "git_source_mismatch"
    assert refused.value.detail["reason"].startswith(reason)
    assert reason in caplog.text

def test_update_batch_entries_run_the_real_reviewed_install(
  client, auth, git_server, transfers, bypass_url_validation, tmp_path,
):
  """Two reviewed entries through install_from_manifest, transfers unlocked."""
  from uuid import uuid4

  from app import app_update_batches

  updates, responses = [], {}
  for name in ("batch-one", "batch-two"):
    app_id, manifest = _install_served(client, auth, git_server, name)
    v2 = {**manifest, "version": "2.0.0"}
    head = git_server.commit(name, v2, JSX.replace("ok", "v2"), push=True)
    responses.update(_responses(name, v2, JSX.replace("ok", "v2")))
    with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
      access = client.post("/api/apps/preview", headers=auth, json={
        "manifest_url": _base(name) + "mobius.json",
      })
    assert access.status_code == 200, access.text
    preview = client.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth)
    assert preview.status_code == 200, preview.text
    assert preview.json()["upstream_commit"] == head
    updates.append({
      "app_id": app_id, "manifest_url": _base(name) + "mobius.json",
      "upstream_commit": head, "source_digest": preview.json()["source_digest"],
      "capability_digest": access.json()["capability_digest"],
    })

  async def scenario(_http):
    owner = app_update_batches.AppUpdateBatches(str(tmp_path))
    await owner.start()
    from app.database import SessionLocal
    with SessionLocal() as db:
      accepted = owner.submit(app_update_batches.ReviewedUpdateBatch(
        id=uuid4(), updates=updates,
      ), db)
    while owner.tasks:
      await asyncio.gather(*list(owner.tasks.values()), return_exceptions=True)
    return owner.snapshot(accepted["id"])

  final = _run_async(scenario, responses)
  assert final["phase"] == "complete"
  assert [(e["status"], e["version"]) for e in final["entries"]] == [
    ("updated", "2.0.0"), ("updated", "2.0.0"),
  ], final
  for update, name in zip(updates, ("batch-one", "batch-two")):
    assert app_git.head_sha(_repo(name), app_git.UPSTREAM_BRANCH) == update["upstream_commit"]
  assert all(call == (False, []) for call in transfers), transfers
  assert _no_leftovers()


def test_update_batch_entry_that_really_conflicts_names_the_reviewed_commit(
  client, auth, git_server, transfers, bypass_url_validation, tmp_path,
):
  """A real conflict leaves a receipt for exactly the reviewed commit."""
  from uuid import uuid4

  from app import app_update_batches

  name = "batch-conflict"
  app_id, manifest = _install_served(client, auth, git_server, name)
  repo = _repo(name)
  # The owner's local edit and the release change the same line.
  (repo / "index.jsx").write_text(JSX.replace("ok", "local edit"))
  v2 = {**manifest, "version": "2.0.0"}
  head = git_server.commit(name, v2, JSX.replace("ok", "v2"), push=True)
  responses = _responses(name, v2, JSX.replace("ok", "v2"))
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
    access = client.post("/api/apps/preview", headers=auth, json={
      "manifest_url": _base(name) + "mobius.json",
    })
  assert access.status_code == 200, access.text
  preview = client.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth)
  assert preview.status_code == 200, preview.text
  update = {
    "app_id": app_id, "manifest_url": _base(name) + "mobius.json",
    "upstream_commit": head, "source_digest": preview.json()["source_digest"],
    "capability_digest": access.json()["capability_digest"],
  }

  async def scenario(_http):
    owner = app_update_batches.AppUpdateBatches(str(tmp_path))
    await owner.start()
    from app.database import SessionLocal
    with SessionLocal() as db:
      accepted = owner.submit(app_update_batches.ReviewedUpdateBatch(
        id=uuid4(), updates=[update],
      ), db)
    while owner.tasks:
      await asyncio.gather(*list(owner.tasks.values()), return_exceptions=True)
    await owner.refresh_results(accepted["id"])
    return owner.snapshot(accepted["id"])

  final = _run_async(scenario, responses)
  entry = final["entries"][0]
  assert (entry["status"], entry["version"]) == ("conflict", "1.0.0"), final
  assert pending_update.read_pending_conflict_update_receipt(
    repo, app_id=app_id, upstream_commit=head,
  ) is not None
  # The one landing rule agrees with the installer's result.
  assert app_update_batches._settled_outcomes({app_id: head})[app_id]["status"] == "conflict"
  assert (repo / "index.jsx").read_text() == JSX.replace("ok", "local edit")
  assert all(call == (False, []) for call in transfers), transfers
