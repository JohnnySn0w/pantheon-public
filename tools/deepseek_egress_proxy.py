#!/usr/bin/env python3
"""Hostname-bound, key-holding DeepSeek reverse proxy (T-6192).

The proxy is only half of the boundary.  The companion UID firewall makes this
TLS loopback listener the DeepSeek seat's sole network destination; this module
then reduces that destination to two measured request shapes and one immutable
upstream TLS identity.  It never logs request/response bodies, headers, keys or
URLs.  No command in this module installs or changes a live firewall.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import http.client
import io
import json
import os
import re
import socket
import socketserver
import ssl
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


POLICY_SCHEMA = "pantheon-deepseek-egress/v1"
HEALTH_SCHEMA = "pantheon-deepseek-egress-health/v1"
FIREWALL_SCHEMA = "pantheon-deepseek-egress-firewall/v1"
GATE_SCHEMA = "pantheon-deepseek-egress-gate/v1"
HEALTH_PATH = "/__pantheon_health"
CLIENT_DUMMY_AUTH = "Bearer proxy-held-dummy"
MAX_POLICY_BYTES = 64 * 1024
MAX_KEY_BYTES = 16 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_SENSITIVE_RESPONSE = _HOP_BY_HOP | frozenset(
    {"authorization", "proxy-authorization", "set-cookie", "www-authenticate", "location"}
)


class PolicyError(ValueError):
    """Policy bytes are missing, ambiguous or wider than the reviewed shape."""


class RequestError(ValueError):
    """A request or upstream response is outside the reviewed shape."""


class Deadline:
    """One monotonic request deadline shared by every blocking operation."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if seconds <= 0:
            raise RequestError("total timeout invalid")
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        remaining = self._end - self._clock()
        if remaining <= 0:
            raise RequestError("total request timeout")
        return remaining


@dataclasses.dataclass(frozen=True)
class Route:
    id: str
    method: str
    local_path: str
    upstream_path: str
    json_body: bool

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "local_path": self.local_path,
            "upstream_path": self.upstream_path,
            "json": self.json_body,
        }


def _strict_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        unknown = sorted(set(raw) - expected)
        raise PolicyError(f"{label} keys mismatch missing={missing} unknown={unknown}")


def _positive_int(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 < value <= maximum):
        raise PolicyError(f"{label} must be an integer in 1..{maximum}")
    return value


@dataclasses.dataclass(frozen=True)
class ProxyPolicy:
    local_authority: str
    upstream_hostname: str
    upstream_port: int
    routes: tuple[Route, ...]
    request_body_bytes: int
    response_body_bytes: int
    header_bytes: int
    connect_seconds: int
    read_seconds: int
    total_seconds: int
    seat_account: str
    proxy_account: str
    digest: str

    @property
    def local_hostname(self) -> str:
        return self.local_authority.rsplit(":", 1)[0]

    @property
    def local_port(self) -> int:
        return int(self.local_authority.rsplit(":", 1)[1])

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "local_authority": self.local_authority,
            "upstream_hostname": self.upstream_hostname,
            "upstream_port": self.upstream_port,
            "routes": [r.as_mapping() for r in self.routes],
            "limits": {
                "request_body_bytes": self.request_body_bytes,
                "response_body_bytes": self.response_body_bytes,
                "header_bytes": self.header_bytes,
            },
            "timeouts": {
                "connect_seconds": self.connect_seconds,
                "read_seconds": self.read_seconds,
                "total_seconds": self.total_seconds,
            },
            "accounts": {"seat": self.seat_account, "proxy": self.proxy_account},
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProxyPolicy":
        if not isinstance(raw, Mapping):
            raise PolicyError("policy must be an object")
        _strict_keys(
            raw,
            {"schema", "local_authority", "upstream_hostname", "upstream_port", "routes", "limits", "timeouts", "accounts"},
            "policy",
        )
        if raw["schema"] != POLICY_SCHEMA:
            raise PolicyError("policy schema mismatch")
        authority = raw["local_authority"]
        upstream = raw["upstream_hostname"]
        if not isinstance(authority, str) or authority.count(":") != 1:
            raise PolicyError("local_authority must be hostname:port")
        local_host, local_port_raw = authority.rsplit(":", 1)
        if (
            not local_host
            or local_host.lower() != local_host
            or local_host == upstream
            or not local_host.endswith(".invalid")
            or not local_port_raw.isdecimal()
        ):
            raise PolicyError("local_authority must be a distinct lowercase .invalid hostname and port")
        _positive_int(int(local_port_raw), "local port", maximum=65535)
        if (
            not isinstance(upstream, str)
            or upstream != "api.deepseek.com"
            or upstream.lower() != upstream
        ):
            raise PolicyError("upstream hostname is not the pinned DeepSeek API hostname")
        upstream_port = _positive_int(raw["upstream_port"], "upstream_port", maximum=65535)
        if upstream_port != 443:
            raise PolicyError("upstream_port must be 443")

        routes_raw = raw["routes"]
        if not isinstance(routes_raw, list) or not routes_raw:
            raise PolicyError("routes must be a non-empty list")
        routes: list[Route] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(routes_raw):
            if not isinstance(item, Mapping):
                raise PolicyError(f"route {index} must be an object")
            _strict_keys(item, {"id", "method", "local_path", "upstream_path", "json"}, f"route {index}")
            route_id, method = item["id"], item["method"]
            local_path, upstream_path = item["local_path"], item["upstream_path"]
            json_body = item["json"]
            if not isinstance(route_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", route_id):
                raise PolicyError(f"route {index} id invalid")
            if method not in {"GET", "POST"}:
                raise PolicyError(f"route {index} method invalid")
            for label, path in (("local", local_path), ("upstream", upstream_path)):
                if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path or "//" in path:
                    raise PolicyError(f"route {index} {label} path invalid")
            if not isinstance(json_body, bool) or json_body != (method == "POST"):
                raise PolicyError(f"route {index} json flag invalid")
            key = (method, local_path)
            if key in seen or any(r.id == route_id for r in routes):
                raise PolicyError("duplicate route")
            seen.add(key)
            routes.append(Route(route_id, method, local_path, upstream_path, json_body))
        required = {
            ("POST", "/v1/chat/completions", "/v1/chat/completions"),
            ("GET", "/v1/user/balance", "/user/balance"),
        }
        if {(r.method, r.local_path, r.upstream_path) for r in routes} != required:
            raise PolicyError("routes must equal the measured chat and balance routes")

        limits, timeouts, accounts = raw["limits"], raw["timeouts"], raw["accounts"]
        if not all(isinstance(v, Mapping) for v in (limits, timeouts, accounts)):
            raise PolicyError("limits, timeouts and accounts must be objects")
        _strict_keys(limits, {"request_body_bytes", "response_body_bytes", "header_bytes"}, "limits")
        _strict_keys(timeouts, {"connect_seconds", "read_seconds", "total_seconds"}, "timeouts")
        _strict_keys(accounts, {"seat", "proxy"}, "accounts")
        request_cap = _positive_int(limits["request_body_bytes"], "request_body_bytes", maximum=16 * 1024 * 1024)
        response_cap = _positive_int(limits["response_body_bytes"], "response_body_bytes", maximum=64 * 1024 * 1024)
        header_cap = _positive_int(limits["header_bytes"], "header_bytes", maximum=64 * 1024)
        connect = _positive_int(timeouts["connect_seconds"], "connect_seconds", maximum=30)
        read = _positive_int(timeouts["read_seconds"], "read_seconds", maximum=300)
        total = _positive_int(timeouts["total_seconds"], "total_seconds", maximum=600)
        if total < max(connect, read):
            raise PolicyError("total timeout must cover connect and read")
        seat, proxy = accounts["seat"], accounts["proxy"]
        if seat != "deepseek-fallback" or proxy != "pantheon-deepseek-proxy" or seat == proxy:
            raise PolicyError("account identities are not the reviewed pair")
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return cls(authority, upstream, upstream_port, tuple(routes), request_cap, response_cap, header_cap, connect, read, total, seat, proxy, digest)


def _open_regular(
    path: Path,
    cap: int,
    label: str,
    *,
    expected_owners: set[int] | None = None,
    allowed_modes: set[int] | None = None,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PolicyError(f"{label} unavailable:{type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PolicyError(f"{label} is not regular")
        if st.st_size > cap:
            raise PolicyError(f"{label} oversize")
        mode = stat.S_IMODE(st.st_mode)
        if expected_owners is not None and st.st_uid not in expected_owners:
            raise PolicyError(f"{label} owner mismatch")
        if allowed_modes is not None and mode not in allowed_modes:
            raise PolicyError(f"{label} mode mismatch")
        return fd, st
    except Exception:
        os.close(fd)
        raise


def _read_regular(
    path: Path,
    cap: int,
    label: str,
    *,
    expected_owners: set[int] | None = None,
    allowed_modes: set[int] | None = None,
) -> bytes:
    fd, _st = _open_regular(
        path,
        cap,
        label,
        expected_owners=expected_owners,
        allowed_modes=allowed_modes,
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            try:
                chunk = os.read(fd, min(65536, cap + 1 - total))
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > cap:
            raise PolicyError(f"{label} oversize")
        return data
    finally:
        os.close(fd)


def load_policy(path: Path, *, expected_owner: int | None = None) -> ProxyPolicy:
    owners = None if expected_owner is None else {expected_owner}
    raw = _read_regular(
        path,
        MAX_POLICY_BYTES,
        "policy",
        expected_owners=owners,
        allowed_modes={0o400, 0o440, 0o444},
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PolicyError("policy is not canonical JSON") from None
    policy = ProxyPolicy.from_mapping(value)
    canonical = json.dumps(policy.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if raw != canonical:
        raise PolicyError("policy bytes are not canonical")
    return policy


def load_provider_key(path: Path) -> str:
    raw = _read_regular(
        path,
        MAX_KEY_BYTES,
        "provider credential",
        expected_owners={os.geteuid()},
        allowed_modes={0o400},
    )
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise PolicyError("provider credential is not text") from None
    if not key or any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        raise PolicyError("provider credential has invalid shape")
    return key


def load_tls_server_context(
    cert_file: Path,
    cert_key_file: Path,
    *,
    expected_owners: set[int],
) -> ssl.SSLContext:
    """Load a verified inode through procfs so path replacement cannot race us."""

    cert_fd, _ = _open_regular(
        cert_file,
        MAX_POLICY_BYTES,
        "proxy certificate",
        expected_owners=expected_owners,
        allowed_modes={0o400, 0o440, 0o444},
    )
    try:
        key_fd, _ = _open_regular(
            cert_key_file,
            MAX_POLICY_BYTES,
            "proxy certificate key",
            expected_owners=expected_owners,
            allowed_modes={0o400},
        )
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(f"/proc/self/fd/{cert_fd}", f"/proc/self/fd/{key_fd}")
            return context
        except (OSError, ssl.SSLError):
            raise PolicyError("proxy certificate material invalid") from None
        finally:
            os.close(key_fd)
    finally:
        os.close(cert_fd)


@dataclasses.dataclass(frozen=True)
class ClientRequest:
    method: str
    target: str
    route_id: str
    upstream_path: str
    content_length: int
    content_type: str | None
    accept: str | None


def _header_pairs(head: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    if b"\x00" in head or b"\r\n\r\n" not in head or not head.endswith(b"\r\n\r\n"):
        raise RequestError("malformed request head")
    for byte in head:
        if byte < 32 and byte not in (10, 13):
            raise RequestError("control byte in request head")
        if byte == 127:
            raise RequestError("control byte in request head")
    lines = head[:-4].split(b"\r\n")
    if not lines or any(line.startswith((b" ", b"\t")) for line in lines[1:]):
        raise RequestError("obs-fold refused")
    pairs: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN.fullmatch(name):
            raise RequestError("invalid header name")
        try:
            text = value.decode("ascii").strip()
        except UnicodeDecodeError:
            raise RequestError("non-ascii header value") from None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise RequestError("control byte in header value")
        pairs.append((name.decode("ascii").lower(), text))
    return lines[0], pairs


def parse_client_request(head: bytes, policy: ProxyPolicy, *, allow_health: bool = False) -> ClientRequest:
    if len(head) > policy.header_bytes:
        raise RequestError("request headers oversize")
    request_line, pairs = _header_pairs(head)
    try:
        line = request_line.decode("ascii")
    except UnicodeDecodeError:
        raise RequestError("request line is not ascii") from None
    fields = line.split(" ")
    if len(fields) != 3 or fields[2] != "HTTP/1.1":
        raise RequestError("request line invalid")
    method, target, _version = fields
    if method == "CONNECT" or method not in {"GET", "POST"}:
        raise RequestError("method refused")
    if not target.startswith("/") or target.startswith("//") or "?" in target or "#" in target:
        raise RequestError("origin-form path required")
    by_name: dict[str, list[str]] = {}
    for name, value in pairs:
        by_name.setdefault(name, []).append(value)
    for unique in ("host",):
        if len(by_name.get(unique, [])) != 1:
            raise RequestError(f"exactly one {unique} required")
    for optional_unique in ("content-type", "accept", "authorization"):
        if len(by_name.get(optional_unique, [])) > 1:
            raise RequestError(f"duplicate {optional_unique} refused")
    if by_name["host"][0].lower() != policy.local_authority:
        raise RequestError("host mismatch")
    if "transfer-encoding" in by_name:
        raise RequestError("transfer encoding refused")
    lengths = by_name.get("content-length", [])
    if method == "POST" and len(lengths) != 1:
        raise RequestError("exactly one content-length required for POST")
    if method == "GET" and len(lengths) > 1:
        raise RequestError("duplicate content-length refused")
    length_text = lengths[0] if lengths else "0"
    if not length_text.isdecimal():
        raise RequestError("content length invalid")
    length = int(length_text)
    if length > policy.request_body_bytes:
        raise RequestError("request body oversize")
    if allow_health and method == "GET" and target == HEALTH_PATH:
        if length != 0:
            raise RequestError("health body refused")
        return ClientRequest(method, target, "health", target, 0, None, None)
    route = next((r for r in policy.routes if r.method == method and r.local_path == target), None)
    if route is None:
        raise RequestError("unknown method/path")
    authorization = by_name.get("authorization", [])
    if authorization != [CLIENT_DUMMY_AUTH]:
        raise RequestError("fixed client authorization required")
    content_type = by_name.get("content-type", [None])[0]
    if route.json_body:
        if length == 0 or content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError("json request required")
    elif length != 0:
        raise RequestError("body refused")
    return ClientRequest(method, target, route.id, route.upstream_path, length, content_type, by_name.get("accept", [None])[0])


def prepare_upstream_request(request: ClientRequest, body: bytes, *, provider_key: str, policy: ProxyPolicy) -> bytes:
    if request.route_id == "health":
        raise RequestError("health is local only")
    if len(body) != request.content_length or len(body) > policy.request_body_bytes:
        raise RequestError("request body framing mismatch")
    if request.content_type and request.content_type.split(";", 1)[0].strip().lower() == "application/json":
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestError("request body is not JSON") from None
        if not isinstance(parsed, Mapping):
            raise RequestError("request JSON must be an object")
    lines = [
        f"{request.method} {request.upstream_path} HTTP/1.1",
        f"Host: {policy.upstream_hostname}",
        f"Authorization: Bearer {provider_key}",
        "Connection: close",
        f"Content-Length: {len(body)}",
    ]
    if request.content_type:
        lines.append(f"Content-Type: {request.content_type}")
    if request.accept:
        lines.append(f"Accept: {request.accept}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def sanitize_upstream_response(
    status: int,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    policy: ProxyPolicy,
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    if not isinstance(status, int) or not (100 <= status <= 599) or 300 <= status <= 399:
        raise RequestError("upstream status refused")
    if len(body) > policy.response_body_bytes:
        raise RequestError("upstream response oversize")
    header_bytes = 0
    by_name: dict[str, list[str]] = {}
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            raise RequestError("upstream header invalid")
        try:
            encoded = f"{name}: {value}\r\n".encode("ascii")
        except UnicodeEncodeError:
            raise RequestError("upstream header invalid") from None
        header_bytes += len(encoded)
        if header_bytes > policy.header_bytes:
            raise RequestError("upstream headers oversize")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name + value):
            raise RequestError("upstream header control byte")
        lower = name.lower()
        by_name.setdefault(lower, []).append(value)
    if "transfer-encoding" in by_name:
        raise RequestError("upstream transfer encoding refused")
    lengths = by_name.get("content-length", [])
    if len(lengths) != 1 or not lengths[0].isdecimal() or int(lengths[0]) != len(body):
        raise RequestError("upstream body framing mismatch")
    content_types = by_name.get("content-type", [])
    if len(content_types) > 1:
        raise RequestError("duplicate upstream content-type")
    kept: list[tuple[str, str]] = []
    if content_types:
        kept.append(("Content-Type", content_types[0]))
    kept.append(("Content-Length", str(len(body))))
    return status, tuple(kept), body


@dataclasses.dataclass(frozen=True)
class HealthState:
    state: str
    policy_digest: str
    observed_at: int

    @classmethod
    def ready(cls, digest: str, *, observed_at: int | None = None) -> "HealthState":
        if not _HEX64.fullmatch(digest):
            raise ValueError("health digest invalid")
        timestamp = int(time.time()) if observed_at is None else observed_at
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("health timestamp invalid")
        return cls("ready", digest, timestamp)

    @classmethod
    def unknown(cls, digest: str, *, observed_at: int | None = None) -> "HealthState":
        timestamp = int(time.time()) if observed_at is None else observed_at
        return cls("unknown", digest if _HEX64.fullmatch(digest) else "0" * 64, timestamp)

    def as_mapping(self) -> dict[str, str | int]:
        return {"schema": HEALTH_SCHEMA, "state": self.state, "policy_digest": self.policy_digest, "observed_at": self.observed_at}

    @classmethod
    def from_mapping(cls, raw: Any) -> "HealthState":
        if not isinstance(raw, Mapping) or set(raw) != {"schema", "state", "policy_digest", "observed_at"}:
            raise ValueError("health shape invalid")
        if raw["schema"] != HEALTH_SCHEMA or raw["state"] not in {"ready", "unknown"}:
            raise ValueError("health enum invalid")
        digest = raw["policy_digest"]
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ValueError("health digest invalid")
        observed_at = raw["observed_at"]
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
            raise ValueError("health timestamp invalid")
        return cls(raw["state"], digest, observed_at)


@dataclasses.dataclass(frozen=True)
class FirewallState:
    state: str
    policy_digest: str
    v4_block_sha256: str
    v6_block_sha256: str
    placement: str
    observed_at: int

    @classmethod
    def enforced(
        cls,
        digest: str,
        v4_hash: str,
        v6_hash: str,
        *,
        observed_at: int | None = None,
    ) -> "FirewallState":
        timestamp = int(time.time()) if observed_at is None else observed_at
        if not all(isinstance(value, str) and _HEX64.fullmatch(value) for value in (digest, v4_hash, v6_hash)):
            raise ValueError("firewall digest invalid")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("firewall timestamp invalid")
        return cls("enforced", digest, v4_hash, v6_hash, "before-loopback-and-established", timestamp)

    @classmethod
    def unknown(cls, digest: str, *, observed_at: int | None = None) -> "FirewallState":
        timestamp = int(time.time()) if observed_at is None else observed_at
        safe_digest = digest if isinstance(digest, str) and _HEX64.fullmatch(digest) else "0" * 64
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("firewall timestamp invalid")
        return cls("unknown", safe_digest, "0" * 64, "0" * 64, "unknown", timestamp)

    def as_mapping(self) -> dict[str, str | int]:
        return {
            "schema": FIREWALL_SCHEMA,
            "state": self.state,
            "policy_digest": self.policy_digest,
            "v4_block_sha256": self.v4_block_sha256,
            "v6_block_sha256": self.v6_block_sha256,
            "placement": self.placement,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "FirewallState":
        expected = {"schema", "state", "policy_digest", "v4_block_sha256", "v6_block_sha256", "placement", "observed_at"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("firewall state shape invalid")
        if raw["schema"] != FIREWALL_SCHEMA or raw["state"] not in {"enforced", "unknown"}:
            raise ValueError("firewall state enum invalid")
        if raw["state"] == "unknown":
            if raw["placement"] != "unknown" or raw["v4_block_sha256"] != "0" * 64 or raw["v6_block_sha256"] != "0" * 64:
                raise ValueError("firewall unknown state invalid")
            return cls.unknown(raw["policy_digest"], observed_at=raw["observed_at"])
        if raw["placement"] != "before-loopback-and-established":
            raise ValueError("firewall placement invalid")
        return cls.enforced(
            raw["policy_digest"],
            raw["v4_block_sha256"],
            raw["v6_block_sha256"],
            observed_at=raw["observed_at"],
        )


_PROBE_EXPECT = {
    "raw_external": "rejected",
    "raw_external_v6": "rejected",
    "dns": "rejected",
    "other_loopback": "rejected",
    "other_loopback_v6": "rejected",
    "proxy": "connected",
}
_REJECT_ERRNOS = frozenset({errno.ECONNREFUSED, errno.EPERM, errno.EACCES})
_DNS_ROOT_QUERY = bytes.fromhex("5eed01000001000000000000" "00" "0002" "0001")


def classify_probe(
    checks: Any,
    health: HealthState | None,
    firewall: FirewallState | None,
    *,
    expected_digest: str,
    expected_v4_hash: str,
    expected_v6_hash: str,
    now: int | None = None,
    max_age: int = 30,
) -> str:
    if not isinstance(checks, Mapping):
        return "unknown"
    normalized = {name: checks.get(name) for name in _PROBE_EXPECT}
    if any(normalized[name] == "connected" for name, want in _PROBE_EXPECT.items() if want == "rejected"):
        return "open"
    if (
        health is None
        or health.state != "ready"
        or health.policy_digest != expected_digest
        or not _HEX64.fullmatch(expected_digest)
        or (int(time.time()) if now is None else now) - health.observed_at > max_age
        or health.observed_at > (int(time.time()) if now is None else now) + 1
        or firewall is None
        or firewall.state != "enforced"
        or firewall.policy_digest != expected_digest
        or firewall.v4_block_sha256 != expected_v4_hash
        or firewall.v6_block_sha256 != expected_v6_hash
        or firewall.placement != "before-loopback-and-established"
        or (int(time.time()) if now is None else now) - firewall.observed_at > max_age
        or firewall.observed_at > (int(time.time()) if now is None else now) + 1
    ):
        return "unknown"
    return "enforced" if normalized == _PROBE_EXPECT else "unknown"


def _connect_verdict(
    address: tuple[str, int],
    timeout: float,
    *,
    connector: Callable[[tuple[str, int], float], socket.socket] = socket.create_connection,
) -> str:
    try:
        sock = connector(address, timeout)
    except OSError as exc:
        if not isinstance(exc, socket.timeout) and exc.errno in _REJECT_ERRNOS:
            return "rejected"
        raise
    sock.close()
    return "connected"


def read_local_health(
    policy: ProxyPolicy,
    ca_file: Path,
    *,
    timeout: float = 2.0,
    connector: Callable[[tuple[str, int], float], socket.socket] = socket.create_connection,
) -> HealthState:
    """Read the value-free local endpoint with exact CA, SNI, Host and framing."""

    ca_fd, _ = _open_regular(
        ca_file,
        MAX_POLICY_BYTES,
        "proxy CA",
        expected_owners={0, os.geteuid()},
        allowed_modes={0o400, 0o440, 0o444},
    )
    try:
        context = ssl.create_default_context(cafile=f"/proc/self/fd/{ca_fd}")
    except (OSError, ssl.SSLError):
        raise RequestError("proxy CA invalid") from None
    finally:
        os.close(ca_fd)
    raw = connector(("127.0.0.1", policy.local_port), timeout)
    try:
        raw.settimeout(timeout)
        with context.wrap_socket(raw, server_hostname=policy.local_hostname) as tls:
            tls.sendall(
                (
                    f"GET {HEALTH_PATH} HTTP/1.1\r\n"
                    f"Host: {policy.local_authority}\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
            response = http.client.HTTPResponse(tls)
            response.begin()
            if response.status != 200:
                raise RequestError("proxy health status refused")
            headers = tuple(response.getheaders())
            lengths = [value for name, value in headers if name.lower() == "content-length"]
            if len(lengths) != 1 or not lengths[0].isdecimal() or int(lengths[0]) > 4096:
                raise RequestError("proxy health framing refused")
            body = response.read(4097)
            if len(body) != int(lengths[0]):
                raise RequestError("proxy health partial")
    except (OSError, ssl.SSLError, http.client.HTTPException, json.JSONDecodeError):
        raise RequestError("proxy health unavailable") from None
    finally:
        raw.close()
    try:
        return HealthState.from_mapping(json.loads(body.decode("ascii")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RequestError("proxy health malformed") from None


def load_gate_manifest(path: Path, *, expected_owner: int = 0) -> Mapping[str, str]:
    raw = _read_regular(
        path,
        MAX_POLICY_BYTES,
        "egress gate manifest",
        expected_owners={expected_owner},
        allowed_modes={0o400, 0o440, 0o444},
    )
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PolicyError("egress gate manifest malformed") from None
    expected = {"schema", "policy_digest", "required", "v4_block_sha256", "v6_block_sha256", "firewall_health", "client_auth"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema") != GATE_SCHEMA or value.get("required") != "enforced":
        raise PolicyError("egress gate manifest shape mismatch")
    for key in ("policy_digest", "v4_block_sha256", "v6_block_sha256"):
        if not isinstance(value.get(key), str) or not _HEX64.fullmatch(value[key]):
            raise PolicyError("egress gate manifest digest invalid")
    if value.get("firewall_health") != "/run/pantheon-deepseek-egress/firewall.json":
        raise PolicyError("egress gate firewall health path mismatch")
    if value.get("client_auth") != "proxy-held-dummy":
        raise PolicyError("egress gate client auth mismatch")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if raw != canonical:
        raise PolicyError("egress gate manifest is not canonical")
    return value  # type: ignore[return-value]


def load_firewall_state(path: Path, *, expected_owner: int = 0) -> FirewallState:
    raw = _read_regular(
        path,
        MAX_POLICY_BYTES,
        "firewall health",
        expected_owners={expected_owner},
        allowed_modes={0o400, 0o440, 0o444},
    )
    try:
        return FirewallState.from_mapping(json.loads(raw.decode("ascii")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PolicyError("firewall health malformed") from None


def probe_boundary(
    policy: ProxyPolicy,
    ca_file: Path,
    *,
    manifest_path: Path | None = None,
    firewall_state: FirewallState | None = None,
    now: int | None = None,
    timeout: float = 2.0,
) -> tuple[str, dict[str, str | None]]:
    """Run the launch/watcher boundary probe as the seat UID."""

    results: dict[str, str | None] = {}
    health: HealthState | None = None
    manifest: Mapping[str, str] | None = None
    if manifest_path is not None:
        try:
            manifest = load_gate_manifest(manifest_path, expected_owner=0 if os.geteuid() != 0 else os.geteuid())
            if firewall_state is None:
                firewall_state = load_firewall_state(Path(manifest["firewall_health"]))
        except (OSError, PolicyError, ValueError):
            manifest = None
    try:
        results["raw_external"] = _connect_verdict(("1.1.1.1", 443), timeout)
    except OSError:
        results["raw_external"] = None
    try:
        results["raw_external_v6"] = _connect_verdict(("2606:4700:4700::1111", 443), timeout)
    except OSError:
        results["raw_external_v6"] = None

    dns_results: list[str] = []
    dns_error = False
    try:
        dns_results.append(_connect_verdict(("127.0.0.53", 53), timeout))
    except OSError:
        dns_error = True
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.settimeout(timeout)
        try:
            udp.connect(("127.0.0.53", 53))
            udp.send(_DNS_ROOT_QUERY)
            udp.recv(512)
            dns_results.append("connected")
        except OSError as exc:
            if not isinstance(exc, socket.timeout) and exc.errno in _REJECT_ERRNOS:
                dns_results.append("rejected")
            else:
                dns_error = True
    finally:
        udp.close()
    results["dns"] = "connected" if "connected" in dns_results else ("rejected" if dns_results and not dns_error else None)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            results["other_loopback"] = _connect_verdict(listener.getsockname(), timeout)
        except OSError:
            results["other_loopback"] = None
    finally:
        listener.close()
    listener6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        listener6.bind(("::1", 0))
        listener6.listen(1)
        try:
            results["other_loopback_v6"] = _connect_verdict(listener6.getsockname()[:2], timeout)
        except OSError:
            results["other_loopback_v6"] = None
    except OSError:
        results["other_loopback_v6"] = None
    finally:
        listener6.close()
    try:
        health = read_local_health(policy, ca_file, timeout=timeout)
        results["proxy"] = "connected"
    except (OSError, PolicyError, RequestError):
        results["proxy"] = None
    if manifest is None:
        verdict = "open" if any(results.get(name) == "connected" for name, wanted in _PROBE_EXPECT.items() if wanted == "rejected") else "unknown"
    else:
        verdict = classify_probe(
            results,
            health,
            firewall_state,
            expected_digest=policy.digest,
            expected_v4_hash=manifest["v4_block_sha256"],
            expected_v6_hash=manifest["v6_block_sha256"],
            now=now,
            max_age=max(5, int(timeout * 5)),
        )
    return verdict, results


def _read_head(sock: socket.socket, cap: int, deadline: Deadline) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        sock.settimeout(deadline.remaining())
        try:
            chunk = sock.recv(min(4096, cap + 1 - len(data)))
        except socket.timeout:
            raise RequestError("request header timeout") from None
        if not chunk:
            raise RequestError("request ended before headers")
        data.extend(chunk)
        if len(data) > cap:
            raise RequestError("request headers oversize")
    end = data.index(b"\r\n\r\n") + 4
    return bytes(data[:end]), bytes(data[end:])


def read_request_body(
    sock: socket.socket,
    initial: bytes,
    length: int,
    *,
    cap: int,
    timeout: float | None = None,
    deadline: Deadline | None = None,
) -> bytes:
    if length > cap or len(initial) > cap:
        raise RequestError("request body oversize")
    if len(initial) > length:
        raise RequestError("pipelined or overlong request refused")
    if deadline is None:
        if timeout is None:
            raise RequestError("request body deadline missing")
        deadline = Deadline(timeout)
    body = bytearray(initial)
    while len(body) < length:
        sock.settimeout(deadline.remaining())
        try:
            chunk = sock.recv(length - len(body))
        except socket.timeout:
            raise RequestError("request body timeout") from None
        if not chunk:
            raise RequestError("partial request body")
        body.extend(chunk)
    return bytes(body)


class _DeadlineSocketIO(io.RawIOBase):
    """Unbuffered socket reader that reapplies one absolute deadline per recv."""

    def __init__(self, sock: socket.socket, deadline: Deadline, read_timeout: float):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        self._read_timeout = read_timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        self._sock.settimeout(min(self._read_timeout, self._deadline.remaining()))
        try:
            return self._sock.recv_into(buffer)
        except socket.timeout:
            raise RequestError("upstream response deadline") from None


class _DeadlineHTTPSocket:
    """The narrow socket surface consumed by http.client.HTTPResponse."""

    def __init__(self, sock: socket.socket, deadline: Deadline, read_timeout: float):
        self._sock = sock
        self._deadline = deadline
        self._read_timeout = read_timeout

    def makefile(self, mode: str, buffering: int | None = None) -> io.BufferedReader:
        if mode != "rb":
            raise RequestError("upstream response mode refused")
        return io.BufferedReader(_DeadlineSocketIO(self._sock, self._deadline, self._read_timeout))


def upstream_exchange(
    policy: ProxyPolicy,
    raw_request: bytes,
    *,
    ssl_context: ssl.SSLContext | None = None,
    connector: Callable[[tuple[str, int], float], socket.socket] = socket.create_connection,
    deadline: Deadline | None = None,
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    deadline = deadline or Deadline(policy.total_seconds)
    connect_timeout = min(float(policy.connect_seconds), deadline.remaining())
    try:
        raw = connector((policy.upstream_hostname, policy.upstream_port), connect_timeout)
    except (OSError, socket.timeout):
        raise RequestError("upstream connect refused") from None
    try:
        raw.settimeout(min(float(policy.read_seconds), deadline.remaining()))
        context = ssl_context or ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=policy.upstream_hostname) as tls:
            tls.settimeout(min(float(policy.read_seconds), deadline.remaining()))
            tls.sendall(raw_request)
            deadline.remaining()
            response = http.client.HTTPResponse(
                _DeadlineHTTPSocket(tls, deadline, float(policy.read_seconds))  # type: ignore[arg-type]
            )
            try:
                response.begin()
                deadline.remaining()
                if sum(len(k) + len(v) + 4 for k, v in response.getheaders()) > policy.header_bytes:
                    raise RequestError("upstream headers oversize")
                body = response.read(policy.response_body_bytes + 1)
                deadline.remaining()
            except (http.client.HTTPException, http.client.IncompleteRead, OSError, socket.timeout, RequestError):
                raise RequestError("upstream response incomplete") from None
            return sanitize_upstream_response(response.status, tuple(response.getheaders()), body, policy)
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _wire_response(status: int, headers: Sequence[tuple[str, str]], body: bytes) -> bytes:
    reason = http.client.responses.get(status, "Error")
    lines = [f"HTTP/1.1 {status} {reason}", *(f"{k}: {v}" for k, v in headers), "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "ProxyServer" = self.server  # type: ignore[assignment]
        route_id, verdict, status = "none", "refused", 400
        try:
            deadline = Deadline(server.policy.total_seconds)
            head, initial = _read_head(self.request, server.policy.header_bytes, deadline)
            request = parse_client_request(head, server.policy, allow_health=True)
            route_id = request.route_id
            body = read_request_body(
                self.request,
                initial,
                request.content_length,
                cap=server.policy.request_body_bytes,
                deadline=deadline,
            )
            if route_id == "health":
                payload = json.dumps(
                    HealthState.ready(server.policy.digest).as_mapping(),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode() + b"\n"
                status, headers = 200, (("Content-Type", "application/json"), ("Content-Length", str(len(payload))))
                self.request.sendall(_wire_response(status, headers, payload))
                verdict = "local-health"
                return
            upstream = prepare_upstream_request(request, body, provider_key=server.provider_key, policy=server.policy)
            status, headers, response_body = server.exchange(server.policy, upstream, deadline=deadline)
            self.request.sendall(_wire_response(status, headers, response_body))
            verdict = "forwarded"
        except (OSError, PolicyError, RequestError, ValueError):
            payload = b'{"error":"request_refused"}\n'
            try:
                self.request.sendall(_wire_response(502 if route_id != "none" else 400, (("Content-Type", "application/json"), ("Content-Length", str(len(payload)))), payload))
            except OSError:
                pass
        finally:
            print(f"deepseek_egress_proxy: route={route_id} verdict={verdict} status_class={status // 100}xx", flush=True)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        bind: tuple[str, int],
        policy: ProxyPolicy,
        provider_key: str,
        cert_file: Path,
        cert_key_file: Path,
        *,
        exchange: Callable[..., tuple[int, tuple[tuple[str, str], ...], bytes]] = upstream_exchange,
        max_concurrency: int = 16,
    ):
        if bind[0] != "127.0.0.1":
            raise PolicyError("proxy bind must be IPv4 loopback")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise PolicyError("max concurrency invalid")
        self.policy = policy
        self.provider_key = provider_key
        self.exchange = exchange
        self._slots = threading.BoundedSemaphore(max_concurrency)
        super().__init__(bind, _ProxyHandler, bind_and_activate=False)
        context = load_tls_server_context(cert_file, cert_key_file, expected_owners={0, os.geteuid()})

        def sni_guard(_sslobj: ssl.SSLObject, server_name: str | None, _context: ssl.SSLContext) -> int | None:
            if server_name != policy.local_hostname:
                return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
            return None

        context.sni_callback = sni_guard
        self._tls_context = context
        self.server_bind()
        self.server_activate()

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        tls: ssl.SSLSocket | None = None
        try:
            tls = self._tls_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            handshake_deadline = Deadline(float(min(self.policy.connect_seconds, self.policy.total_seconds)))
            tls.settimeout(handshake_deadline.remaining())
            tls.do_handshake()
            handshake_deadline.remaining()
            super().process_request_thread(tls, client_address)
        except (OSError, RequestError, ssl.SSLError):
            self.shutdown_request(tls if tls is not None else request)
        finally:
            self._slots.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--policy", type=Path, required=True)
    serve.add_argument("--credential", type=Path, required=True)
    serve.add_argument("--cert", type=Path, required=True)
    serve.add_argument("--cert-key", type=Path, required=True)
    serve.add_argument("--bind", default="127.0.0.1")
    gate = sub.add_parser("gate")
    gate.add_argument("--policy", type=Path, required=True)
    gate.add_argument("--ca", type=Path, required=True)
    gate.add_argument("--manifest", type=Path, required=True)
    gate.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.command == "gate":
        try:
            policy = load_policy(args.policy, expected_owner=0 if os.geteuid() != 0 else os.geteuid())
            verdict, _findings = probe_boundary(policy, args.ca, manifest_path=args.manifest, timeout=args.timeout)
        except (OSError, PolicyError, RequestError, ValueError):
            verdict = "unknown"
        print(f"deepseek_egress_proxy: gate={verdict}")
        return 0 if verdict == "enforced" else 1
    try:
        policy = load_policy(args.policy, expected_owner=0 if os.geteuid() != 0 else os.geteuid())
        key = load_provider_key(args.credential)
        server = ProxyServer((args.bind, policy.local_port), policy, key, args.cert, args.cert_key)
    except (OSError, PolicyError, ValueError) as exc:
        print(f"deepseek_egress_proxy: startup=refused reason={type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
