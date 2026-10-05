"""Every default first-boot app installs on an empty instance.

First boot only logs a failed default-app install, so a platform contract change
that an app's `main` no longer satisfies would otherwise ship silently. The
install test fetches the packages from GitHub, so the hermetic suite skips it;
CI runs it as its own step with MOBIUS_LIVE_BOOTSTRAP=1.

Installing resolves each app's repository id through api.github.com. Shared CI
runners exhaust GitHub's unauthenticated rate limit, so CI passes its workflow
token as GITHUB_TOKEN and the test sends it on api.github.com requests only.
"""

import asyncio
import logging
import os

import httpx
import pytest

from app import models
from app.bootstrap import _CORE_BOOTSTRAP_APPS, ensure_bootstrap_apps_installed



class _GitHubApiToken(httpx.Auth):
  """Bearer token for requests whose TLS server name is api.github.com.

  The installer connects to a pinned IP and names the real host through the
  `sni_hostname` extension, so that is the host the certificate is checked
  against. Raw-content and redirect hops to other hosts never see the token.
  """

  def __init__(self, token: str):
    self._token = token

  def auth_flow(self, request):
    if request.extensions.get("sni_hostname") == "api.github.com":
      request.headers["Authorization"] = f"Bearer {self._token}"
    yield request


def _authenticate_github_api(monkeypatch, token: str) -> None:
  auth = _GitHubApiToken(token)

  class AuthenticatedClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
      kwargs.setdefault("auth", auth)
      super().__init__(*args, **kwargs)

  monkeypatch.setattr(httpx, "AsyncClient", AuthenticatedClient)


def test_github_api_token_reaches_only_api_github_com(monkeypatch):
  seen: dict[str, str | None] = {}

  def record(request: httpx.Request) -> httpx.Response:
    seen[request.extensions["sni_hostname"]] = request.headers.get(
      "Authorization",
    )
    return httpx.Response(200)

  async def fetch_from_both_hosts():
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as cli:
      for host in ("api.github.com", "raw.githubusercontent.com"):
        await cli.get(
          "https://192.0.2.1/", headers={"Host": host},
          extensions={"sni_hostname": host},
        )

  _authenticate_github_api(monkeypatch, "workflow-token")
  asyncio.run(fetch_from_both_hosts())

  assert seen == {
    "api.github.com": "Bearer workflow-token",
    "raw.githubusercontent.com": None,
  }


@pytest.mark.skipif(
  os.environ.get("MOBIUS_LIVE_BOOTSTRAP") != "1",
  reason="fetches the first-boot apps from GitHub",
)
def test_empty_instance_installs_every_bootstrap_app(
  db, monkeypatch, caplog,
):
  monkeypatch.delenv("MOEBIUS_SKIP_BOOTSTRAP", raising=False)
  token = os.environ.get("GITHUB_TOKEN")
  if token:
    _authenticate_github_api(monkeypatch, token)
  caplog.set_level(logging.ERROR, logger="mobius.bootstrap")

  asyncio.run(ensure_bootstrap_apps_installed(db))

  assert [record.getMessage() for record in caplog.records] == []
  installed = {
    row.slug
    for row in db.query(models.App).filter(models.App.deleted_at.is_(None))
  }
  assert {app.manifest_id for app in _CORE_BOOTSTRAP_APPS} <= installed
