"""T-6192: strict, key-holding DeepSeek egress proxy contract."""

from __future__ import annotations

import json
import errno
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tools import deepseek_egress_proxy as proxy_mod
from tools.harness_adapter.provider_quota import read_deepseek_balance
from tools.deepseek_egress_proxy import (
    HealthState,
    PolicyError,
    ProxyServer,
    ProxyPolicy,
    RequestError,
    classify_probe,
    load_policy,
    load_provider_key,
    parse_client_request,
    prepare_upstream_request,
    sanitize_upstream_response,
    upstream_exchange,
)


def _policy(*, local_port: int = 8448, request_cap: int = 65536, response_cap: int = 1048576, total_timeout: int = 60) -> ProxyPolicy:
    return ProxyPolicy.from_mapping(
        {
            "schema": "pantheon-deepseek-egress/v1",
            "local_authority": f"deepseek-proxy.invalid:{local_port}",
            "upstream_hostname": "api.deepseek.com",
            "upstream_port": 443,
            "routes": [
                {"id": "chat", "method": "POST", "local_path": "/v1/chat/completions", "upstream_path": "/v1/chat/completions", "json": True},
                {"id": "balance", "method": "GET", "local_path": "/v1/user/balance", "upstream_path": "/user/balance", "json": False},
            ],
            "limits": {"request_body_bytes": request_cap, "response_body_bytes": response_cap, "header_bytes": 16384},
            "timeouts": {"connect_seconds": 1, "read_seconds": 1, "total_seconds": total_timeout},
            "accounts": {"seat": "deepseek-fallback", "proxy": "pantheon-deepseek-proxy"},
        }
    )


def _head(request_line: bytes, *headers: bytes) -> bytes:
    return request_line + b"\r\n" + b"\r\n".join(headers) + b"\r\n\r\n"


def test_policy_refuses_empty_missing_unknown_and_host_sni_split(tmp_path: Path):
    with pytest.raises(PolicyError):
        ProxyPolicy.from_mapping({})
    raw = json.loads(json.dumps(_policy().as_mapping()))
    raw["unknown"] = True
    with pytest.raises(PolicyError):
        ProxyPolicy.from_mapping(raw)


def test_policy_and_tls_files_refuse_symlink_nonregular_wrong_mode_and_owner(tmp_path: Path):
    policy = _policy()
    canonical = json.dumps(policy.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(canonical)
    policy_path.chmod(0o444)
    assert load_policy(policy_path, expected_owner=os.geteuid()).digest == policy.digest
    with pytest.raises(PolicyError):
        load_policy(policy_path, expected_owner=os.geteuid() + 1)
    policy_path.chmod(0o666)
    with pytest.raises(PolicyError):
        load_policy(policy_path, expected_owner=os.geteuid())
    policy_path.unlink()
    target = tmp_path / "target"
    target.write_bytes(canonical)
    policy_path.symlink_to(target)
    with pytest.raises(PolicyError):
        load_policy(policy_path, expected_owner=os.geteuid())

    ca, cert, key = _make_ca_and_cert(tmp_path, "mode", "deepseek-proxy.invalid")
    cert.chmod(0o444)
    key.chmod(0o400)
    assert isinstance(proxy_mod.load_tls_server_context(cert, key, expected_owners={os.geteuid()}), ssl.SSLContext)
    with pytest.raises(PolicyError):
        proxy_mod.load_tls_server_context(cert, key, expected_owners={os.geteuid() + 1})
    key.chmod(0o644)
    with pytest.raises(PolicyError):
        proxy_mod.load_tls_server_context(cert, key, expected_owners={os.geteuid()})
    key.chmod(0o400)
    key_link = tmp_path / "key-link"
    key_link.symlink_to(key)
    with pytest.raises(PolicyError):
        proxy_mod.load_tls_server_context(cert, key_link, expected_owners={os.geteuid()})

    cert_dir = tmp_path / "cert-dir"
    cert_dir.mkdir()
    with pytest.raises(PolicyError):
        proxy_mod.load_tls_server_context(cert_dir, key, expected_owners={os.geteuid()})


def test_deadline_is_absolute_across_operations():
    values = iter([10.0, 10.2, 10.8, 11.01])
    deadline = proxy_mod.Deadline(1.0, clock=lambda: next(values))
    assert deadline.remaining() == pytest.approx(0.8)
    assert deadline.remaining() == pytest.approx(0.2)
    with pytest.raises(RequestError):
        deadline.remaining()


def _make_ca_and_cert(tmp_path: Path, name: str, hostname: str) -> tuple[Path, Path, Path]:
    ca_key = tmp_path / "ca.key"
    ca_cert = tmp_path / "ca.pem"
    if not ca_cert.exists():
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=T6192 Test CA", "-keyout", ca_key, "-out", ca_cert],
            check=True,
            capture_output=True,
        )
    key = tmp_path / f"{name}.key"
    csr = tmp_path / f"{name}.csr"
    cert = tmp_path / f"{name}.pem"
    ext = tmp_path / f"{name}.ext"
    ext.write_text(f"subjectAltName=DNS:{hostname}\n", encoding="ascii")
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={hostname}", "-keyout", key, "-out", csr],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-days", "1", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key, "-CAcreateserial", "-extfile", ext, "-out", cert],
        check=True,
        capture_output=True,
    )
    cert.chmod(0o444)
    key.chmod(0o400)
    return ca_cert, cert, key


class _FakeTLSUpstream:
    def __init__(self, tmp_path: Path, response: bytes):
        self.ca, cert, key = _make_ca_and_cert(tmp_path, "upstream", "api.deepseek.com")
        self.response = response
        self.request = b""
        self.sni = None
        self.ready = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        context.sni_callback = lambda _obj, name, _ctx: setattr(self, "sni", name)
        self.context = context
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.ready.set()
        raw, _ = self.sock.accept()
        with self.context.wrap_socket(raw, server_side=True) as conn:
            conn.settimeout(2)
            chunks = []
            while b"\r\n\r\n" not in b"".join(chunks):
                chunks.append(conn.recv(4096))
            head, initial = b"".join(chunks).split(b"\r\n\r\n", 1)
            length = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            body = bytearray(initial)
            while len(body) < length:
                body.extend(conn.recv(length - len(body)))
            self.request = head + b"\r\n\r\n" + bytes(body)
            conn.sendall(self.response)
        self.sock.close()

    def start(self):
        self.thread.start()
        assert self.ready.wait(1)


def test_real_tls_proxy_to_fake_tls_upstream_binds_both_sni_and_host_and_key(tmp_path: Path, capsys):
    upstream = _FakeTLSUpstream(
        tmp_path,
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
    )
    upstream.start()
    ca, proxy_cert, proxy_key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    key_file = tmp_path / "provider-key"
    key_file.write_text("real-provider-key\n", encoding="ascii")
    key_file.chmod(0o400)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    proxy_port = probe.getsockname()[1]
    probe.close()
    policy = _policy(local_port=proxy_port)
    upstream_context = ssl.create_default_context(cafile=str(upstream.ca))

    def exchange(p, raw, *, deadline=None):
        return upstream_exchange(
            p,
            raw,
            deadline=deadline,
            ssl_context=upstream_context,
            connector=lambda _addr, timeout: socket.create_connection(("127.0.0.1", upstream.port), timeout),
        )

    server = ProxyServer(("127.0.0.1", proxy_port), policy, load_provider_key(key_file), proxy_cert, proxy_key, exchange=exchange)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=2) as raw:
        with client_context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
            client.sendall(
                _head(
                    b"POST /v1/chat/completions HTTP/1.1",
                    f"Host: deepseek-proxy.invalid:{proxy_port}".encode(),
                    b"Content-Type: application/json",
                    b"Content-Length: 2",
                    b"Authorization: Bearer proxy-held-dummy",
                )
                + b"{}"
            )
            response = b""
            while chunk := client.recv(4096):
                response += chunk
    server.shutdown()
    server.server_close()
    thread.join(2)
    upstream.thread.join(2)
    assert b"200 OK" in response and response.endswith(b"{}")
    assert upstream.sni == "api.deepseek.com"
    assert b"Host: api.deepseek.com" in upstream.request
    assert b"Authorization: Bearer real-provider-key" in upstream.request
    assert b"proxy-held-dummy" not in upstream.request
    logs = capsys.readouterr()
    assert "real-provider-key" not in logs.out + logs.err
    assert "proxy-held-dummy" not in logs.out + logs.err


def test_real_tls_proxy_restarts_immediately_after_handled_connection(tmp_path: Path):
    ca, cert, key = _make_ca_and_cert(tmp_path, "restart", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    policy = _policy(local_port=port)

    first = ProxyServer(("127.0.0.1", port), policy, "provider-key", cert, key)
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
        with client_context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
            client.sendall(
                _head(
                    f"GET {proxy_mod.HEALTH_PATH} HTTP/1.1".encode(),
                    f"Host: deepseek-proxy.invalid:{port}".encode(),
                )
            )
            response = b""
            while chunk := client.recv(4096):
                response += chunk
    assert b"200 OK" in response
    first.shutdown()
    first.server_close()
    thread.join(2)

    second = ProxyServer(("127.0.0.1", port), policy, "provider-key", cert, key)
    try:
        with pytest.raises(OSError):
            ProxyServer(("127.0.0.1", port), policy, "provider-key", cert, key)
    finally:
        second.server_close()


def test_inbound_sni_mismatch_is_refused_before_upstream(tmp_path: Path):
    ca, cert, key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    calls = []
    server = ProxyServer(("127.0.0.1", port), _policy(local_port=port), "key", cert, key, exchange=lambda *a: calls.append(a))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(ca))
    context.check_hostname = False
    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
        with pytest.raises(ssl.SSLError):
            context.wrap_socket(raw, server_hostname="attacker.invalid")
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert calls == []


def test_proxy_refuses_non_loopback_bind(tmp_path: Path):
    _ca, cert, key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    with pytest.raises(PolicyError):
        ProxyServer(("0.0.0.0", 8448), _policy(), "key", cert, key)
    with pytest.raises(PolicyError):
        ProxyServer(("192.0.2.10", 8448), _policy(), "key", cert, key)


def test_silent_tls_client_does_not_block_valid_health(tmp_path: Path):
    ca, cert, key = _make_ca_and_cert(tmp_path, "silent", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = ProxyServer(
        ("127.0.0.1", port),
        _policy(local_port=port),
        "key",
        cert,
        key,
        max_concurrency=2,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    silent = socket.create_connection(("127.0.0.1", port), timeout=2)
    context = ssl.create_default_context(cafile=str(ca))
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as raw:
            raw.settimeout(0.5)
            with context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
                client.sendall(
                    _head(
                        f"GET {proxy_mod.HEALTH_PATH} HTTP/1.1".encode(),
                        f"Host: deepseek-proxy.invalid:{port}".encode(),
                    )
                )
                assert b"200 OK" in client.recv(4096)
        assert time.monotonic() - started < 0.5
    finally:
        silent.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_tls_handshake_workers_are_bounded_and_expire(tmp_path: Path):
    _ca, cert, key = _make_ca_and_cert(tmp_path, "bounded", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = ProxyServer(
        ("127.0.0.1", port),
        _policy(local_port=port),
        "key",
        cert,
        key,
        max_concurrency=1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = socket.create_connection(("127.0.0.1", port), timeout=2)
    second = None
    try:
        first.settimeout(1.5)
        second = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        second.settimeout(0.5)
        assert second.recv(1) == b""
        assert first.recv(1) == b""
    finally:
        first.close()
        if second is not None:
            second.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_proxy_bounds_concurrency_and_total_request_time(tmp_path: Path):
    ca, cert, key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    entered, release = threading.Event(), threading.Event()

    def exchange(_policy, _raw, *, deadline=None):
        assert deadline is not None
        entered.set()
        assert release.wait(2)
        return 200, (("Content-Type", "application/json"), ("Content-Length", "2")), b"{}"

    server = ProxyServer(("127.0.0.1", port), _policy(local_port=port, total_timeout=1), "key", cert, key, exchange=exchange, max_concurrency=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(ca))
    request = _head(
        b"POST /v1/chat/completions HTTP/1.1",
        f"Host: deepseek-proxy.invalid:{port}".encode(),
        b"Content-Type: application/json",
        b"Content-Length: 2",
        b"Authorization: Bearer proxy-held-dummy",
    ) + b"{}"

    first_result = []

    def first_client():
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
                client.sendall(request)
                first_result.append(client.recv(4096))

    first = threading.Thread(target=first_client)
    first.start()
    assert entered.wait(1)
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
        with pytest.raises((OSError, ssl.SSLError)):
            context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid")
    assert time.monotonic() - started < 1
    release.set()
    first.join(2)
    assert b"200" in first_result[0]

    # The client can receive the response just before process_request_thread's
    # finally block returns the sole slot. Prove the worker has released it
    # before the next handshake; otherwise this control races the concurrency
    # refusal it just tested and intermittently sees an expected EOF here.
    slot_deadline = time.monotonic() + 1
    while not server._slots.acquire(blocking=False):
        assert time.monotonic() < slot_deadline
        time.sleep(0.01)
    server._slots.release()

    with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
        with context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
            started = time.monotonic()
            client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n")
            response = client.recv(4096)
            assert time.monotonic() - started < 2
            assert b"400" in response
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("shape", ["missing", "empty", "symlink", "directory", "wrong-mode"])
def test_provider_credential_shape_and_mode_fail_closed(tmp_path: Path, shape: str):
    path = tmp_path / "credential"
    if shape == "empty":
        path.write_bytes(b"")
        path.chmod(0o400)
    elif shape == "symlink":
        target = tmp_path / "target"
        target.write_text("secret", encoding="ascii")
        path.symlink_to(target)
    elif shape == "directory":
        path.mkdir()
    elif shape == "wrong-mode":
        path.write_text("secret", encoding="ascii")
        path.chmod(0o644)
    with pytest.raises(PolicyError) as caught:
        load_provider_key(path)
    assert "secret" not in str(caught.value)
    raw = json.loads(json.dumps(_policy().as_mapping()))
    raw["upstream_sni"] = "attacker.invalid"
    with pytest.raises(PolicyError):
        ProxyPolicy.from_mapping(raw)


@pytest.mark.parametrize(
    "head",
    [
        _head(b"CONNECT api.deepseek.com:443 HTTP/1.1", b"Host: deepseek-proxy.invalid:8448"),
        _head(b"POST https://api.deepseek.com/v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: application/json"),
        _head(b"POST /v1/chat/completions?x=1 HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: application/json"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Host: attacker.invalid", b"Content-Length: 2", b"Content-Type: application/json"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Length: 2", b"Content-Type: application/json"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Transfer-Encoding: chunked", b"Content-Type: application/json"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: text/plain"),
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: deepseek-proxy.invalid:8448\r\n X: folded\r\n\r\n",
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: deepseek-proxy.invalid:8448\x00\r\n\r\n",
    ],
)
def test_request_grammar_refuses_ambiguous_or_hostile_shapes(head: bytes):
    with pytest.raises(RequestError):
        parse_client_request(head, _policy())


def test_request_grammar_accepts_only_measured_routes_and_json():
    policy = _policy()
    request = parse_client_request(
        _head(
            b"POST /v1/chat/completions HTTP/1.1",
            b"Host: deepseek-proxy.invalid:8448",
            b"Content-Type: application/json",
            b"Content-Length: 2",
            b"Authorization: Bearer proxy-held-dummy",
            b"Connection: keep-alive",
        ),
        policy,
    )
    assert request.route_id == "chat"
    assert request.content_length == 2
    assert request.upstream_path == "/v1/chat/completions"

    balance = parse_client_request(
        _head(
            b"GET /v1/user/balance HTTP/1.1",
            b"Host: deepseek-proxy.invalid:8448",
            b"Authorization: Bearer proxy-held-dummy",
        ),
        policy,
    )
    assert balance.content_length == 0 and balance.route_id == "balance"
    with pytest.raises(RequestError):
        parse_client_request(
            _head(
                b"GET /v1/user/balance HTTP/1.1",
                b"Host: deepseek-proxy.invalid:8448",
                b"Content-Length: 1",
                b"Authorization: Bearer proxy-held-dummy",
            ),
            policy,
        )


@pytest.mark.parametrize(
    "head",
    [
        _head(b"DELETE /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 0"),
        _head(b"GET /unknown HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 0"),
        _head(b"GET /v1/user/balance HTTP/1.1", b"Content-Length: 0"),
        _head(b"GET /v1/user/balance HTTP/1.1", b"hOsT: deepseek-proxy.invalid:8448", b"HOST: deepseek-proxy.invalid:8448", b"Content-Length: 0"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: application/json", b"content-TYPE: application/json"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: application/json", b"Accept: application/json", b"ACCEPT: */*"),
        _head(b"POST /v1/chat/completions HTTP/1.1", b"Host: deepseek-proxy.invalid:8448", b"Content-Length: 2", b"Content-Type: application/json", b"Authorization: Bearer one", b"AUTHORIZATION: Bearer two"),
    ],
)
def test_unknown_method_path_and_missing_or_case_duplicate_host_are_refused(head: bytes):
    with pytest.raises(RequestError):
        parse_client_request(head, _policy())


def test_upstream_request_replaces_auth_and_strips_hop_by_hop_headers():
    policy = _policy()
    request = parse_client_request(
        _head(
            b"POST /v1/chat/completions HTTP/1.1",
            b"Host: deepseek-proxy.invalid:8448",
            b"Content-Type: application/json",
            b"Content-Length: 2",
            b"Authorization: Bearer proxy-held-dummy",
            b"Connection: keep-alive",
        ),
        policy,
    )
    raw = prepare_upstream_request(request, b"{}", provider_key="real-provider-key", policy=policy)
    assert b"Host: api.deepseek.com" in raw
    assert b"Authorization: Bearer real-provider-key" in raw
    assert b"proxy-held-dummy" not in raw
    assert b"Connection: keep-alive" not in raw


def test_response_refuses_redirect_and_strips_sensitive_headers():
    with pytest.raises(RequestError):
        sanitize_upstream_response(302, [("Location", "https://attacker.invalid")], b"", _policy())
    status, headers, body = sanitize_upstream_response(
        200,
        [("Content-Type", "application/json"), ("Content-Length", "2"), ("Set-Cookie", "secret"), ("Connection", "close"), ("X-Request-Id", "metadata")],
        b"{}",
        _policy(),
    )
    assert status == 200 and body == b"{}"
    assert headers == (("Content-Type", "application/json"), ("Content-Length", "2"))


def test_response_cap_plus_one_and_partial_upstream_fail_closed():
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Content-Type", "application/json")], b"12345", _policy(response_cap=4))
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Content-Length", "8")], b"{}", _policy())
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Content-Length", "2"), ("Content-Length", "2")], b"{}", _policy())
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Transfer-Encoding", "chunked")], b"{}", _policy())
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Content-Length", "2"), ("Content-Type", "application/json"), ("Content-Type", "text/plain")], b"{}", _policy())
    with pytest.raises(RequestError):
        sanitize_upstream_response(200, [("Content-Length", "2"), ("X-Fill", "x" * 20000)], b"{}", _policy())


def test_upstream_response_absolute_deadline_refuses_slow_drip_but_allows_bounded_streaming():
    class PlainContext:
        @staticmethod
        def wrap_socket(raw, *, server_hostname):
            assert server_hostname == "api.deepseek.com"
            return raw

    def exchange(delays: tuple[float, ...], deadline_seconds: float):
        client, upstream = socket.socketpair()

        def serve():
            try:
                upstream.recv(4096)
                upstream.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4\r\n\r\n")
                for delay, byte in zip(delays, b"data", strict=True):
                    time.sleep(delay)
                    upstream.sendall(bytes([byte]))
            except OSError:
                pass
            finally:
                upstream.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            return upstream_exchange(
                _policy(),
                b"GET /user/balance HTTP/1.1\r\nHost: api.deepseek.com\r\n\r\n",
                ssl_context=PlainContext(),
                connector=lambda _address, _timeout: client,
                deadline=proxy_mod.Deadline(deadline_seconds),
            )
        finally:
            thread.join(2)

    started = time.monotonic()
    with pytest.raises(RequestError):
        exchange((0.2, 0.2, 0.2, 0.2), 0.5)
    assert time.monotonic() - started < 0.7
    assert exchange((0.02, 0.02, 0.02, 0.02), 0.5)[2] == b"data"


def test_request_body_exact_cap_plus_one_partial_disconnect_and_timeout():
    left, right = socket.socketpair()
    try:
        right.sendall(b"1234")
        assert proxy_mod.read_request_body(left, b"", 4, cap=4, timeout=0.1) == b"1234"
    finally:
        left.close()
        right.close()
    left, right = socket.socketpair()
    try:
        with pytest.raises(RequestError):
            proxy_mod.read_request_body(left, b"12345", 5, cap=4, timeout=0.1)
        right.close()
        with pytest.raises(RequestError):
            proxy_mod.read_request_body(left, b"1", 4, cap=4, timeout=0.1)
    finally:
        left.close()
    left, right = socket.socketpair()
    try:
        with pytest.raises(RequestError):
            proxy_mod.read_request_body(left, b"", 1, cap=4, timeout=0.01)
    finally:
        left.close()
        right.close()


def test_health_and_probe_are_value_free_fail_closed_and_repeatable():
    policy = _policy()
    health = HealthState.ready(policy.digest, observed_at=100)
    assert health.as_mapping() == {"schema": "pantheon-deepseek-egress-health/v1", "state": "ready", "policy_digest": policy.digest, "observed_at": 100}
    checks = {"raw_external": "rejected", "raw_external_v6": "rejected", "dns": "rejected", "other_loopback": "rejected", "other_loopback_v6": "rejected", "proxy": "connected"}
    firewall = proxy_mod.FirewallState.enforced(policy.digest, "1" * 64, "2" * 64, observed_at=100)
    kwargs = {"expected_digest": policy.digest, "expected_v4_hash": "1" * 64, "expected_v6_hash": "2" * 64, "now": 105, "max_age": 10}
    assert classify_probe(checks, health, firewall, **kwargs) == "enforced"
    assert classify_probe(checks, health, firewall, **kwargs) == "enforced"
    assert classify_probe({**checks, "raw_external": "connected"}, health, firewall, **kwargs) == "open"
    assert classify_probe(checks, health, firewall, **{**kwargs, "expected_digest": "0" * 64}) == "unknown"
    assert classify_probe(checks, health, firewall, **{**kwargs, "now": 111}) == "unknown"
    assert classify_probe({k: v for k, v in checks.items() if k != "raw_external_v6"}, health, firewall, **kwargs) == "unknown"
    wrong_firewall = proxy_mod.FirewallState.enforced(policy.digest, "3" * 64, "2" * 64, observed_at=100)
    assert classify_probe(checks, health, wrong_firewall, **kwargs) == "unknown"
    with pytest.raises(ValueError):
        HealthState.from_mapping({"schema": "pantheon-deepseek-egress-health/v1", "state": "ready", "policy_digest": policy.digest})


def test_health_serialization_contains_no_secret_shaped_bytes():
    policy = _policy()
    payload = json.dumps(HealthState.ready(policy.digest, observed_at=100).as_mapping())
    assert "Bearer" not in payload
    assert "provider-key" not in payload
    assert set(json.loads(payload)) == {"schema", "state", "policy_digest", "observed_at"}


def test_generic_network_unreachable_is_unknown_not_policy_rejection():
    for error in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        with pytest.raises(OSError):
            proxy_mod._connect_verdict(("192.0.2.1", 443), 0.01, connector=lambda *_args, error=error: (_ for _ in ()).throw(OSError(error, "unreachable")))


def test_local_health_never_calls_upstream_or_injects_auth_and_refreshes_timestamp(tmp_path: Path, monkeypatch):
    ca, cert, key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    calls = []
    server = ProxyServer(
        ("127.0.0.1", port),
        _policy(local_port=port),
        "provider-secret",
        cert,
        key,
        exchange=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(ca))
    now = [100]
    monkeypatch.setattr(proxy_mod.time, "time", lambda: now[0])

    def read_health() -> tuple[bytes, HealthState]:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="deepseek-proxy.invalid") as client:
                client.sendall(_head(b"GET /__pantheon_health HTTP/1.1", f"Host: deepseek-proxy.invalid:{port}".encode(), b"Content-Length: 0"))
                response = b""
                while chunk := client.recv(4096):
                    response += chunk
        body = response.split(b"\r\n\r\n", 1)[1]
        return response, HealthState.from_mapping(json.loads(body))

    response, first = read_health()
    now[0] = 120
    _response2, second = read_health()
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert calls == []
    assert b"provider-secret" not in response
    assert first.observed_at == 100
    assert second.observed_at == 120 and second.state == "ready"


def test_real_urllib_balance_shape_uses_custom_ca_and_proxy_mapping(tmp_path: Path, monkeypatch):
    body = json.dumps({"is_available": True, "balance_infos": []}).encode()
    upstream = _FakeTLSUpstream(
        tmp_path,
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body,
    )
    upstream.start()
    ca, cert, key = _make_ca_and_cert(tmp_path, "proxy", "deepseek-proxy.invalid")
    upstream_context = ssl.create_default_context(cafile=str(upstream.ca))

    def exchange(policy, raw, *, deadline=None):
        return upstream_exchange(
            policy,
            raw,
            deadline=deadline,
            ssl_context=upstream_context,
            connector=lambda _addr, timeout: socket.create_connection(("127.0.0.1", upstream.port), timeout),
        )

    server = ProxyServer(("127.0.0.1", 8448), _policy(), "real-provider-key", cert, key, exchange=exchange)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_getaddrinfo = socket.getaddrinfo

    def local_dns(host, port, *args, **kwargs):
        if host == "deepseek-proxy.invalid":
            host = "127.0.0.1"
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.setattr(socket, "getaddrinfo", local_dns)
    try:
        result = read_deepseek_balance(
            tmp_path / "missing.env",
            base_url="https://deepseek-proxy.invalid:8448/v1",
            key_holding_proxy=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        upstream.thread.join(2)
    assert result == {"is_available": True, "balance_infos": []}
    assert upstream.request.startswith(b"GET /user/balance HTTP/1.1\r\n")
    assert b"Authorization: Bearer real-provider-key" in upstream.request
    assert b"proxy-held-dummy" not in upstream.request
