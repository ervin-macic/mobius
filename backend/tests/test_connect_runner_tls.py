"""TLS trust belongs to one connection attempt, not to each command POST."""
import ssl
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import connect_runner as runner


@pytest.fixture
def https_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-receive-credentials")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, cert_path, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_shared_context_checks_hostname_trust_and_refuses_redirects(https_server):
    port, cert, received = https_server
    ctx = ssl.create_default_context(cafile=str(cert))
    url = f"https://localhost:{port}"
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    assert runner._post(url + "/ok", {}, token="test-only", context=ctx) == {}
    with pytest.raises(urllib.error.URLError) as mismatch:
        runner._post(f"https://127.0.0.1:{port}/bad", {}, context=ctx)
    assert isinstance(mismatch.value.reason, ssl.SSLCertVerificationError)
    with pytest.raises(urllib.error.URLError) as untrusted:
        runner._post(url + "/untrusted", {}, context=ssl.create_default_context())
    assert isinstance(untrusted.value.reason, ssl.SSLCertVerificationError)
    with pytest.raises(urllib.error.HTTPError) as redirected:
        runner._post(url + "/redirect", {}, token="test-only", context=ctx)
    assert redirected.value.code == 302
    assert received == [("/ok", "Bearer test-only"), ("/redirect", "Bearer test-only")]


def test_parallel_posts_reuse_context_without_mutating_it(https_server):
    port, cert, received = https_server
    ctx = ssl.create_default_context(cafile=str(cert))
    stats = ctx.cert_store_stats()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: runner._post(
            f"https://localhost:{port}/{n}", {}, context=ctx), range(12)))
    assert results == [{}] * 12
    assert len(received) == 12
    assert ctx.cert_store_stats() == stats
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_every_command_message_uses_its_own_connection_context(monkeypatch):
    calls = []

    def post(url, payload, **kwargs):
        calls.append((url, kwargs))
        return {"next": 1}

    monkeypatch.setattr(runner, "_post", post)
    monkeypatch.setattr(runner._CommandRunner, "_wake_result_worker",
                        lambda self: self.flush_pending_results())
    contexts = [object(), object()]
    for n, ctx in enumerate(contexts):
        owner = runner._CommandRunner(f"https://host{n}.test", f"test{n}", context=ctx)
        owner.live_output = True
        owner._post_started("one")
        command = runner._Command("one", 30)
        command.output = runner._CommandOutput()
        command.output.append("stdout", "chunk")
        assert owner.flush_output(command, drain=True)
        owner._post_result(command, "done", "", 0, "completed")
        assert owner.pending_messages() == []
    assert len(calls) == 6
    for n in range(2):
        for url, kwargs in calls[n * 3:(n + 1) * 3]:
            assert url.startswith(f"https://host{n}.test/")
            assert kwargs["context"] is contexts[n]
            assert kwargs["token"] == f"test{n}"


def test_disconnect_confirmation_uses_the_connection_context(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_post",
                        lambda url, payload, **kwargs: calls.append((url, kwargs)))
    monkeypatch.setattr(runner, "_remove_connection", lambda *_args: 1)
    ctx = object()
    owner = runner._CommandRunner("https://host.test", "test-only", context=ctx)
    runner._handle_disconnect({}, "https://host.test", "test-only", owner, "d1")
    assert calls == [("https://host.test/api/connect/result",
                      {"token": "test-only", "context": ctx})]


def test_reconnect_keeps_the_connection_context_and_pending_results(monkeypatch):
    contexts, streams, posts = [], [], []
    stop, first_post = threading.Event(), threading.Event()
    monkeypatch.setattr(runner.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(runner, "STREAM_HEALTHY_SECONDS", 0)
    # Deliver results on the calling thread so the order of attempts is exact.
    monkeypatch.setattr(runner._CommandRunner, "_wake_result_worker",
                        lambda self: self.flush_pending_results())

    def create_context():
        value = object()
        contexts.append(value)
        return value

    monkeypatch.setattr(runner.ssl, "create_default_context", create_context)

    def post(url, payload, **kwargs):
        posts.append((payload["request_id"], kwargs["context"]))
        first_post.set()
        if len(posts) == 1:
            raise urllib.error.URLError("first stream lost")
        return {}

    monkeypatch.setattr(runner, "_post", post)

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            if len(streams) == 1:
                yield b'data: {"type":"exec","request_id":"expired","not_after":1}\n'
                assert first_post.wait(5)
            else:
                stop.set()
                yield b": heartbeat\n"

    def open_url(_request, **kwargs):
        streams.append(kwargs["context"])
        return Stream()

    monkeypatch.setattr(runner, "_open_url", open_url)
    runner._serve_connection(
        {"url": "https://host.test", "token": "test-only", "host_id": "h_test"},
        stop,
    )
    # One trust snapshot per connection, shared by every stream and POST.
    assert len(contexts) == 1
    assert streams == contexts * 2
    assert posts == [("expired", contexts[0]), ("expired", contexts[0])]
