"""Local broker requests must not allocate unused public TLS trust stores."""
from __future__ import annotations

import asyncio
import http.server
import socketserver
import ssl
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import runtime_identity
from app.providers import MobiusProvider


def _self_signed_server_context(directory) -> ssl.SSLContext:
  key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
  name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "broker")])
  now = datetime.now(timezone.utc)
  cert = (
    x509.CertificateBuilder().subject_name(name).issuer_name(name)
    .public_key(key.public_key()).serial_number(x509.random_serial_number())
    .not_valid_before(now - timedelta(minutes=1))
    .not_valid_after(now + timedelta(days=1))
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("broker")]), False)
    .sign(key, hashes.SHA256())
  )
  cert_path, key_path = directory / "cert.pem", directory / "key.pem"
  cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
  key_path.write_bytes(key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
  ))
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  context.load_cert_chain(cert_path, key_path)
  return context


@pytest.fixture
def broker_socket(tmp_path, monkeypatch, request):
  calls = []

  class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
      calls.append(self.path)
      if self.path == "/redirect":
        self.send_response(302)
        self.send_header("Location", "https://must-not-contact.invalid/")
        body = b""
      else:
        self.send_response(200)
        body = b'{"linked":true,"balance":42}'
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, *_args):
      pass

  # Keep the AF_UNIX pathname below its small OS limit even in long pytest roots.
  with tempfile.TemporaryDirectory(prefix="broker-", dir="/tmp") as directory:
    path = directory + "/socket"
    server = socketserver.UnixStreamServer(path, Handler)
    if getattr(request, "param", False):
      server.socket = _self_signed_server_context(tmp_path).wrap_socket(
        server.socket, server_side=True,
      )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("MOBIUS_IDENTITY_BROKER_SOCKET", path)
    # Public trust/proxy configuration must not be needed by local HTTP.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-contact.invalid:9999")
    try:
      yield calls
    finally:
      server.shutdown()
      worker.join(timeout=5)
      server.server_close()


def test_socket_context_has_no_unused_roots_and_never_disables_verification():
  context = runtime_identity.private_socket_tls_context()
  assert context.verify_mode == ssl.CERT_REQUIRED
  assert context.check_hostname is True
  assert context.cert_store_stats()["x509_ca"] == 0
  assert runtime_identity.private_socket_tls_context() is not context


def test_provider_checks_use_private_socket_without_ca_bundle(broker_socket):
  provider = MobiusProvider()
  assert provider._identity() == {"linked": True, "balance": 42}
  assert provider.trial_status() == {"linked": True, "balance": 42}
  assert broker_socket == ["/identity", "/v1/balance"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_broker_client_keeps_socket_transport_and_refuses_redirects(
  broker_socket, asynchronous,
):
  async def async_request():
    async with runtime_identity.broker_async_client(timeout=2.0) as client:
      response = await client.get("/redirect")
      with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
      assert response.status_code == 302
    assert client.is_closed

  if asynchronous:
    asyncio.run(async_request())
  else:
    with runtime_identity.broker_client(timeout=2.0) as client:
      response = client.get("/redirect")
      with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
      assert response.status_code == 302
    assert client.is_closed
  assert broker_socket == ["/redirect"]


def test_community_and_contribution_brokers_use_private_socket_without_ca_bundle(
  broker_socket,
):
  from app.community_broker import CommunityBrokerClient
  from app.contribution_broker import CONTRIBUTION_PREFIX, ContributionBrokerClient

  contribution = CONTRIBUTION_PREFIX + "/ctr_" + "0" * 32
  community = asyncio.run(CommunityBrokerClient().request("GET", "/identity"))
  contributed = asyncio.run(
    ContributionBrokerClient().request("GET", contribution),
  )
  assert community[:2] == ({"linked": True, "balance": 42}, 200)
  assert contributed[:2] == ({"linked": True, "balance": 42}, 200)
  assert broker_socket == ["/identity", contribution]


def test_broker_request_retains_json_contract(broker_socket):
  assert asyncio.run(runtime_identity.broker_request("GET", "/identity")) == {
    "linked": True, "balance": 42,
  }


@pytest.mark.parametrize("broker_socket", [True], indirect=True)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_accidental_https_over_socket_still_rejects_untrusted_certificate(
  broker_socket, asynchronous,
):
  async def async_request():
    async with runtime_identity.broker_async_client(timeout=2.0) as client:
      with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await client.get("https://broker/identity")

  if asynchronous:
    asyncio.run(async_request())
  else:
    with runtime_identity.broker_client(timeout=2.0) as client:
      with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        client.get("https://broker/identity")
  assert broker_socket == []
