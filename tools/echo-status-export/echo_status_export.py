#!/usr/bin/env python3
"""Read-only Echo daemon status export (T-4420).

Runs on the claude account (where the live Echo daemon runs) from a systemd
--user timer. Each run:

1. Fetches the daemon status snapshot over the config-control channel: publish
   ``{"request_id": <id>, "op": "get", "resource": "status"}`` on
   ``echo/config/request`` and wait for the reply with the same ``request_id``
   on ``echo/config/reply`` (echo-prismatic crates/echo/src/config_control.rs:
   REQUEST_TOPIC / REPLY_TOPIC, ConfigRequest / ConfigReply,
   handle_status_get). ``ECHO_STATUS_REQUEST_TOPIC`` /
   ``ECHO_STATUS_REPLY_TOPIC`` switch to the daemon's read-only status pair
   ``echo/config/status/request`` / ``echo/config/status/reply`` (T-4431) once
   the daemon serves it; only those two pairs are accepted (status_topics).
   The echo binary has no ``status`` subcommand (its CLI is
   only ``--config`` / ``--log-format``, crates/echo/src/main.rs), so there is
   no CLI to wrap. The script talks MQTT 3.1.1 itself (stdlib only; paho is
   not installed on the host). Credentials never go on a command line, where
   other accounts could read them in /proc.
2. Sanitizes the snapshot: drops denied keys and paths, replaces each
   secret-looking value with ``"<redacted>"`` (listing its path in
   ``sanitizer.redacted_paths``), and refuses to write the snapshot at all if
   any value matches a high-confidence credential pattern.
3. Writes ``echo-status.json`` atomically into the export dir (tmp in the
   same dir, chmod 0640, fsync, rename). The export dir is a claude-owned
   mode 2750 subdir, so group readers can read but not replace the files. On
   success it also refreshes ``echo-status.last-good.json``. On any failure
   (no reply, broker unreachable, protocol error, bad config, daemon error,
   sanitizer refusal, an unexpected exporter exception) it writes an error
   record in place of the status, so the file never goes stale silently, and
   it leaves last-good alone. When a good snapshot is from a new daemon boot
   (``status.started_at`` changed), the old last-good is first archived to
   ``boots/echo-status.boot-<started_at>.json`` (newest 30 kept), so per-boot
   counters survive restarts; ``--boots`` reads them back (T-4442).
4. Summarizes the daemon's journal for its current or most recent run (the
   unit's current or last invocation, never the whole system boot) into
   ``echo-journal-summary.json``: unique WARN/ERROR signatures with counts and
   first/last seen, with no payload text. The status and journal exports run
   independently: a failure in one does not skip the other.

All configuration comes from the environment (see echo-status-export.env.example).
Nothing from the environment except broker host/port, the status topics and
the unit name is written to the export.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from typing import Any, Callable, Iterable

SCHEMA_STATUS = "echo-status-export/1"
SCHEMA_JOURNAL = "echo-journal-summary/1"

REQUEST_TOPIC = "echo/config/request"  # config_control.rs REQUEST_TOPIC
REPLY_TOPIC = "echo/config/reply"  # config_control.rs REPLY_TOPIC
# T-4431 Layer 2: the daemon's read-only status pair (accepts only op=get
# resource=status). Opt in via ECHO_STATUS_REQUEST_TOPIC / ECHO_STATUS_REPLY_TOPIC.
STATUS_REQUEST_TOPIC = "echo/config/status/request"
STATUS_REPLY_TOPIC = "echo/config/status/reply"
ALLOWED_TOPIC_PAIRS = ((REQUEST_TOPIC, REPLY_TOPIC), (STATUS_REQUEST_TOPIC, STATUS_REPLY_TOPIC))

STATUS_FILE = "echo-status.json"
LAST_GOOD_FILE = "echo-status.last-good.json"
JOURNAL_FILE = "echo-journal-summary.json"

HASH_CACHE_FILE = ".echo-bin-hash-cache.json"

# Per-boot archive (T-4442): <export dir>/boots/echo-status.boot-<started_at>.json
BOOTS_DIR = "boots"
BOOT_PREFIX = "echo-status.boot-"
BOOTS_KEEP = 30

DEFAULT_EXPORT_DIR = "/var/lib/pantheon/echo-evidence/status"

FILE_MODE = 0o640
# claude-owned, group read-only, setgid so files keep the pantheon-shared group.
DIR_MODE = 0o2750
# Leftover tmp files older than this are removed at startup.
STALE_TMP_S = 600
# Upper bound on ECHO_STATUS_TIMEOUT_S (see fetch_status).
MAX_STATUS_TIMEOUT_S = 20.0


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (CONNECT / SUBSCRIBE / PUBLISH qos0 / DISCONNECT)
# ---------------------------------------------------------------------------


class MqttError(Exception):
    """Broker-level failure (refused connection, bad CONNACK, protocol error)."""


class NoReply(Exception):
    """The daemon did not answer within the timeout."""


def _enc_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def _packet(ptype: int, body: bytes) -> bytes:
    return bytes([ptype]) + _enc_len(len(body)) + body


def build_connect(client_id: str, username: str | None, password: str | None, keepalive: int = 30) -> bytes:
    flags = 0x02  # clean session
    payload = _enc_str(client_id)
    if username:
        flags |= 0x80
        payload += _enc_str(username)
        if password is not None:
            flags |= 0x40
            payload += _enc_str(password)
    var = _enc_str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    return _packet(0x10, var + payload)


def build_subscribe(packet_id: int, topic: str) -> bytes:
    return _packet(0x82, struct.pack("!H", packet_id) + _enc_str(topic) + b"\x00")


def build_publish(topic: str, payload: bytes) -> bytes:
    return _packet(0x30, _enc_str(topic) + payload)


class _Conn:
    def __init__(self, sock: socket.socket, deadline: float):
        self.sock = sock
        self.deadline = deadline

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise NoReply("no reply")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout as e:
                raise NoReply("no reply") from e
            if not chunk:
                raise MqttError("broker closed connection")
            buf += chunk
        return buf

    def read_packet(self) -> tuple[int, bytes]:
        header = self._recv_exact(1)[0]
        mult, length = 1, 0
        for _ in range(4):
            b = self._recv_exact(1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise MqttError("malformed remaining length")
        return header, self._recv_exact(length) if length else b""


def mqtt_request(
    host: str,
    port: int,
    request: dict,
    *,
    timeout: float,
    username: str | None = None,
    password: str | None = None,
    request_topic: str = REQUEST_TOPIC,
    reply_topic: str = REPLY_TOPIC,
) -> dict:
    """Publish ``request`` and return the reply whose request_id matches.

    Subscribes to the reply topic and waits for SUBACK before publishing, so
    the reply cannot be missed. The reply topic is shared by every config
    client, so replies with other request_ids are skipped.
    Raises NoReply on timeout and MqttError on everything else: broker
    failures, socket errors mid-exchange (e.g. a reset) and malformed packets.
    """
    deadline = time.monotonic() + timeout
    rid = request["request_id"]
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except (OSError, OverflowError, ValueError) as e:
        raise MqttError(f"broker unreachable ({type(e).__name__})") from e
    try:
        return _exchange(sock, _Conn(sock, deadline), request, rid, username, password, request_topic, reply_topic)
    except (NoReply, MqttError):
        raise
    except socket.timeout as e:
        raise NoReply("no reply") from e
    except (struct.error, IndexError) as e:
        raise MqttError("malformed packet from broker") from e
    except (OSError, ValueError) as e:
        raise MqttError(f"broker protocol/io error ({type(e).__name__})") from e
    finally:
        try:
            sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        sock.close()


def _exchange(
    sock: socket.socket,
    conn: _Conn,
    request: dict,
    rid: str,
    username: str | None,
    password: str | None,
    request_topic: str,
    reply_topic: str,
) -> dict:
    client_id = f"echo-status-export-{uuid.uuid4().hex[:12]}"
    sock.sendall(build_connect(client_id, username, password))
    ptype, body = conn.read_packet()
    if ptype != 0x20 or len(body) < 2:
        raise MqttError("unexpected packet instead of CONNACK")
    if body[1] != 0:
        raise MqttError(f"broker refused connection (CONNACK rc={body[1]})")
    sock.sendall(build_subscribe(1, reply_topic))
    while True:
        ptype, body = conn.read_packet()
        if ptype == 0x90:
            if body[-1:] == b"\x80":
                raise MqttError("broker refused subscription to reply topic")
            break
    sock.sendall(build_publish(request_topic, json.dumps(request).encode()))
    while True:
        ptype, body = conn.read_packet()
        if ptype >> 4 != 3:  # not PUBLISH
            continue
        qos = (ptype >> 1) & 0x03
        tlen = struct.unpack("!H", body[:2])[0]  # struct.error on a short body
        topic = body[2 : 2 + tlen].decode("utf-8", "replace")
        off = 2 + tlen + (2 if qos else 0)
        if topic != reply_topic:
            continue
        try:
            reply = json.loads(body[off:])
        except ValueError:
            continue
        if isinstance(reply, dict) and reply.get("request_id") == rid:
            return reply


def status_topics(env: dict) -> tuple[str, str]:
    """Return (request_topic, reply_topic) from the environment.

    A pair allowlist, not free-form topics: the get must never be published
    onto a command topic, and a mixed pair can never get a reply. Unset or
    empty means the shared config-control pair (as for ECHO_BIN and the creds).
    """
    topics = (
        env.get("ECHO_STATUS_REQUEST_TOPIC") or REQUEST_TOPIC,
        env.get("ECHO_STATUS_REPLY_TOPIC") or REPLY_TOPIC,
    )
    if topics not in ALLOWED_TOPIC_PAIRS:
        raise MqttError("bad config: ECHO_STATUS_REQUEST_TOPIC / ECHO_STATUS_REPLY_TOPIC")
    return topics


def fetch_status(env: dict) -> dict:
    """Send the one request this tool ever sends: op=get resource=status.

    The op and resource are hard-coded. The exporter is read-only toward
    Echo and never issues a set/put/delete.
    """
    request = {
        "request_id": f"echo-status-export-{uuid.uuid4()}",
        "op": "get",
        "resource": "status",
    }
    try:
        port = int(env.get("ECHO_MQTT_PORT", "1883"))
        timeout = float(env.get("ECHO_STATUS_TIMEOUT_S", "10"))
        if not 0 < port < 65536 or not 0 < timeout:
            raise ValueError("out of range")
    except ValueError as e:
        raise MqttError("bad config: ECHO_MQTT_PORT / ECHO_STATUS_TIMEOUT_S") from e
    request_topic, reply_topic = status_topics(env)
    # Capped so status + journal (JOURNAL_TIMEOUT_S) fit in the unit's
    # TimeoutStartSec=120.
    timeout = min(timeout, MAX_STATUS_TIMEOUT_S)
    return mqtt_request(
        env.get("ECHO_MQTT_HOST", "127.0.0.1"),
        port,
        request,
        timeout=timeout,
        username=env.get("ECHO_MQTT_USERNAME") or None,
        password=env.get("ECHO_MQTT_PASSWORD") or None,
        request_topic=request_topic,
        reply_topic=reply_topic,
    )


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

# Keys dropped wherever they appear (case-insensitive). The status snapshot
# carries none of these today (crates/echo/src/status.rs StatusSnapshot and
# its field groups). This list guards against a future field-group adding
# one, since ADR-217's extension pattern serializes new fields automatically.
DENY_KEYS = frozenset(
    k.lower()
    for k in (
        # utterance / transcript / model text
        "text", "transcript", "transcription", "utterance", "utterance_text",
        "raw_text", "normalized_text", "content", "prompt", "response",
        "reply_text", "message_text", "words", "audio", "pcm",
        # credentials
        "password", "passwd", "secret", "client_secret", "token", "api_key",
        "apikey", "access_token", "refresh_token", "auth_token", "bearer",
        "authorization", "credentials", "private_key", "cookie", "session_key",
    )
)

# Dotted paths dropped from the snapshot (list indices are skipped).
DENY_PATHS = frozenset(
    {
        # status.rs ActiveSpeakerEntry.speaker_id: the enrolled identity of
        # whoever spoke last. It is not needed for deploy verification, and a
        # group-readable file should not log who is speaking. tier + since stay.
        "active_speaker.speaker_id",
    }
)

# High-confidence credential patterns. A hit anywhere refuses the whole
# snapshot: something is badly wrong upstream, and the export must not guess
# which neighbouring values leaked with it.
_REFUSE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic style keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),  # JWT
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # URL userinfo user:password@
    re.compile(r"\bBearer\s+\S{8,}", re.I),
]
# Lower-confidence patterns. A hit replaces only that value with "<redacted>".
_REDACT_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]|glpat|xox[abpr])[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"[?&](?:token|key|api_key|access_token|secret|sig)=", re.I),
]
# Candidate token runs for the entropy check. "/" is deliberately NOT a token
# char: RFC 6901 pointers such as "/properties/brightness_pct/anyOf/1/maximum"
# (status.rs DriftHeldTool.pointer) split into short runs and never trip it.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+_=-]{32,}")

REDACTED = "<redacted>"

REFUSE = "refuse"
REDACT = "redact"


class SecretSuspected(Exception):
    """A high-confidence credential pattern matched. Carries the path only."""

    def __init__(self, path: str):
        super().__init__(f"credential-like value at {path}")
        self.path = path


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def secret_class(value: str) -> str | None:
    """Classify a string: REFUSE, REDACT, or None when it looks benign.

    REFUSE for the explicit high-confidence patterns (PEM private key, sk-,
    JWT, URL userinfo, Bearer). REDACT for the other token patterns and for a
    high-entropy run of 32+ token chars that mixes upper case, lower case and
    digits. Lowercase hex (uuids, hashes), RFC 3339 timestamps, model ids and
    RFC 6901 pointers do not trip it.
    """
    if any(p.search(value) for p in _REFUSE_PATTERNS):
        return REFUSE
    if any(p.search(value) for p in _REDACT_PATTERNS):
        return REDACT
    for run in _TOKEN_RUN.findall(value):
        if (
            re.search(r"[a-z]", run)
            and re.search(r"[A-Z]", run)
            and re.search(r"[0-9]", run)
            and _entropy(run) >= 4.0
        ):
            return REDACT
    return None


def looks_secret(value: str) -> bool:
    return secret_class(value) is not None


def sanitize(
    obj: Any,
    dropped: list[str] | None = None,
    _path: str = "",
    redacted: list[str] | None = None,
) -> Any:
    """Return a copy of ``obj`` without denied keys/paths or secret-like values.

    Appends each dropped path to ``dropped`` and each redacted path to
    ``redacted``. A secret-like string value becomes "<redacted>"; a
    secret-like key drops its entry (recorded as ``<parent>.<key>``).
    Raises SecretSuspected (naming the path, never the value) when any key or
    value matches a high-confidence credential pattern.
    """
    if dropped is None:
        dropped = []
    if redacted is None:
        redacted = []
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            child = f"{_path}.{ks}" if _path else ks
            if ks.lower() in DENY_KEYS or _strip_indices(child) in DENY_PATHS:
                dropped.append(child)
                continue
            cls = secret_class(ks)
            if cls is not None:
                where = f"{_path}.<key>" if _path else "<key>"
                if cls == REFUSE:
                    raise SecretSuspected(where)
                redacted.append(where)
                continue
            out[ks] = sanitize(v, dropped, child, redacted)
        return out
    if isinstance(obj, list):
        return [sanitize(v, dropped, f"{_path}[{i}]", redacted) for i, v in enumerate(obj)]
    if isinstance(obj, str):
        cls = secret_class(obj)
        if cls == REFUSE:
            raise SecretSuspected(_path or "<root>")
        if cls == REDACT:
            redacted.append(_path or "<root>")
            return REDACTED
    return obj


def _strip_indices(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path)


# ---------------------------------------------------------------------------
# Export dir + atomic write
# ---------------------------------------------------------------------------


class ExportDirError(Exception):
    """The export dir is unsafe to write into (wrong owner, not a dir)."""


def prepare_export_dir(directory: str) -> None:
    """Create the claude-owned mode 2750 export subdir if missing, then check it.

    The parent (/var/lib/pantheon/echo-evidence) is group-writable by every
    pantheon-shared member, so any member could replace a file written there.
    The subdir is owned by the exporter and not group-writable: members can
    read the exports but cannot replace or delete them. On Linux a new subdir
    of a setgid dir inherits the group and the setgid bit, so mkdir with 0750
    normally already yields 2750. The explicit chmod covers a dir that did not
    (the unit's RestrictSUIDSGID= refuses a chmod that sets setgid, so the
    fallback is 0750 and _fchgrp then sets the group per file).
    Refuses (ExportDirError) a dir that is a symlink or owned by someone else,
    e.g. one another member pre-created.
    """
    try:
        os.mkdir(directory, 0o750)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise ExportDirError(f"export dir is not a directory: {directory}")
    if st.st_uid != os.getuid():
        raise ExportDirError(f"export dir not owned by the exporter: {directory}")
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            try:
                os.chmod(directory, DIR_MODE & 0o777)
            except OSError:
                print(f"warning: could not chmod {directory}", file=sys.stderr)
    _remove_stale_tmp(directory)


def _remove_stale_tmp(directory: str, max_age_s: float = STALE_TMP_S) -> None:
    """Remove ``.{name}.*.tmp`` leftovers (from a killed run) older than max_age_s."""
    prefixes = tuple(f".{n}." for n in (STATUS_FILE, LAST_GOOD_FILE, JOURNAL_FILE, HASH_CACHE_FILE))
    prefixes += (f".{BOOT_PREFIX}",)
    cutoff = time.time() - max_age_s
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for e in entries:
        if not (e.name.startswith(prefixes) and e.name.endswith(".tmp")):
            continue
        try:
            if e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(e.path)
        except OSError:
            pass


def atomic_write_json(directory: str, name: str, data: dict, group: str | None = None) -> str:
    """Write ``data`` to directory/name atomically with mode 0640.

    tmp file in the same dir -> fchmod 0640 -> (chgrp if needed) -> fsync ->
    rename -> fsync dir. The mode is set before the fsync so the durable
    inode already has its final mode. The export dir is setgid
    pantheon-shared, so the group is normally inherited; ``group`` is a
    fallback for a dir that isn't.
    """
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fchmod(f.fileno(), FILE_MODE)
            if group:
                _fchgrp(f.fileno(), directory, group)
            os.fsync(f.fileno())
        final = os.path.join(directory, name)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return final


def _fchgrp(fd: int, directory: str, group: str) -> None:
    """chgrp the new file only when the dir did not already hand it the group.

    A setgid dir gives new files its group. Under the unit's sandboxing
    (PrivateUsers=yes is implied in a --user manager) other groups show up as
    `nobody`, so comparing gids by name there would fail. Comparing with the
    dir's gid works either way. A failed chgrp is a warning: the file is
    still written.
    """
    import grp

    dst = os.stat(directory)
    st = os.fstat(fd)
    if dst.st_mode & stat.S_ISGID and st.st_gid == dst.st_gid:
        return
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return
    if st.st_gid != gid:
        try:
            os.fchown(fd, -1, gid)
        except OSError:
            print(f"warning: could not chgrp export to {group}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Status export
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def binary_identity(path: str, cache_dir: str | None = None) -> dict:
    """sha256 + mtime of the deployed echo binary.

    The status snapshot carries no version or build id (status.rs), so this
    is the build identity for deploy verification. The hash is cached in
    ``cache_dir``/.echo-bin-hash-cache.json keyed on (path, st_ino, st_size,
    st_mtime_ns), so the binary is re-read only after a deploy.
    """
    try:
        st = os.stat(path)
    except OSError as e:
        return {"path": path, "error": type(e).__name__}
    key = [path, st.st_ino, st.st_size, st.st_mtime_ns]
    cache_path = os.path.join(cache_dir, HASH_CACHE_FILE) if cache_dir else None
    digest = None
    if cache_path:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("sha256"), str):
                digest = cached["sha256"]
        except (OSError, ValueError):
            pass
    if digest is None:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            after = os.stat(path)
        except OSError as e:
            return {"path": path, "error": type(e).__name__}
        digest = h.hexdigest()
        stable = [path, after.st_ino, after.st_size, after.st_mtime_ns] == key
        if cache_path and stable:
            try:
                atomic_write_json(cache_dir, HASH_CACHE_FILE, {"key": key, "sha256": digest})
            except OSError:
                pass
    return {"path": path, "sha256": digest, "mtime": _iso(st.st_mtime)}


def _transport(env: dict) -> str:
    """Evidence string naming the topics fetch_status uses for ``env``."""
    try:
        request_topic, reply_topic = status_topics(env)
    except MqttError:
        return "mqtt op=get resource=status (bad topic config)"
    return f"mqtt op=get resource=status request={request_topic} reply={reply_topic}"


def build_status_record(
    env: dict, fetch: Callable[[dict], dict], cache_dir: str | None = None
) -> tuple[dict, bool]:
    """Return (record, ok). ok=True only for a sanitized, successful snapshot.

    Never raises: any failure, including an unexpected exception, becomes
    ``record["error"]`` so echo-status.json cannot go stale silently.
    """
    record: dict[str, Any] = {
        "schema": SCHEMA_STATUS,
        "exported_at": utc_now_iso(),
        "source": {
            "transport": _transport(env),
            "broker": f"{env.get('ECHO_MQTT_HOST', '127.0.0.1')}:{env.get('ECHO_MQTT_PORT', '1883')}",
        },
    }
    try:
        if env.get("ECHO_BIN"):
            record["daemon_binary"] = binary_identity(env["ECHO_BIN"], cache_dir)
        try:
            reply = fetch(env)
        except NoReply:
            record["error"] = "no reply"
            return record, False
        except MqttError as e:
            record["error"] = str(e)
            return record, False
        if not isinstance(reply, dict):
            record["error"] = "malformed reply"
            return record, False
        if not reply.get("ok"):
            err = str(reply.get("error") or "daemon returned ok:false")[:200]
            record["error"] = "daemon error: " + (REDACTED if looks_secret(err) else err)
            return record, False
        dropped: list[str] = []
        redacted: list[str] = []
        try:
            status = sanitize(reply.get("data") or {}, dropped, redacted=redacted)
        except SecretSuspected as e:
            record["error"] = f"sanitizer refused: {e}"
            return record, False
        record["status"] = status
        record["sanitizer"] = {"dropped_paths": sorted(dropped), "redacted_paths": sorted(redacted)}
        return record, True
    except Exception as e:  # noqa: BLE001 - last resort: never leave the file stale
        record.pop("status", None)
        record.pop("sanitizer", None)
        record["error"] = f"exporter error ({type(e).__name__})"
        return record, False


def _out_dir(env: dict) -> str:
    return env.get("ECHO_EXPORT_DIR") or DEFAULT_EXPORT_DIR


# Used with fullmatch: '$' alone would also accept a trailing newline.
_STARTED_AT = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?Z")
_BOOT_FILE = re.compile(re.escape(BOOT_PREFIX) + r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)\.json")


def boot_file_name(started_at: Any) -> str | None:
    """``echo-status.boot-2026-09-30T13-31-03Z.json`` for a UTC RFC 3339
    ``started_at`` (fraction dropped, ':' -> '-'); None if it is not one.

    Only a strict UTC timestamp is accepted, so the value can never steer the
    path. Names of the same shape sort in boot order.
    """
    if not isinstance(started_at, str):
        return None
    mm = _STARTED_AT.fullmatch(started_at)
    if not mm:
        return None
    day, h, mi, s = mm.groups()
    return f"{BOOT_PREFIX}{day}T{h}-{mi}-{s}Z.json"


def _started_at(record: Any) -> Any:
    status = record.get("status") if isinstance(record, dict) else None
    return status.get("started_at") if isinstance(status, dict) else None


def _read_own_json(path: str) -> Any:
    """Read a JSON file from the exporter's own dir without following a symlink.

    Returns None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as f:
        return json.load(f)


def archive_previous_boot(out_dir: str, new_record: dict, group: str | None = None) -> str | None:
    """Archive the current last-good to boots/ when ``new_record`` is a new boot.

    Called before last-good is replaced by ``new_record``. When the two
    ``status.started_at`` values differ, the old last-good (the final good
    reading of the previous boot) is written atomically to
    ``boots/echo-status.boot-<old started_at>.json``. If that file already
    exists it is replaced only when the old last-good has a later
    ``exported_at``. Then the boots are pruned to the newest BOOTS_KEEP.
    Returns the archive path, or None when nothing was archived. A missing
    ``started_at`` on either side archives nothing and warns.
    """
    old = _read_own_json(os.path.join(out_dir, LAST_GOOD_FILE))
    if old is None:
        return None
    old_start, new_start = _started_at(old), _started_at(new_record)
    if old_start is None or new_start is None:
        print("warning: status.started_at missing; previous boot not archived", file=sys.stderr)
        return None
    if old_start == new_start:
        return None
    name = boot_file_name(old_start)
    if name is None:
        print("warning: last-good status.started_at is not a UTC timestamp; not archived", file=sys.stderr)
        return None
    boots = os.path.join(out_dir, BOOTS_DIR)
    prepare_export_dir(boots)
    path = os.path.join(boots, name)
    try:
        existing = _read_own_json(path)
    except (OSError, ValueError):
        existing = None  # unreadable or malformed: replace it
    if isinstance(existing, dict) and str(existing.get("exported_at", "")) >= str(old.get("exported_at", "")):
        prune_boots(boots)
        return None
    atomic_write_json(boots, name, old, group)
    prune_boots(boots)
    return path


def prune_boots(boots: str, keep: int = BOOTS_KEEP) -> list[str]:
    """Delete all but the ``keep`` newest boot files (by started_at in the name)."""
    names = sorted(n for n in os.listdir(boots) if _BOOT_FILE.fullmatch(n))
    removed = names[: max(0, len(names) - keep)]
    for n in removed:
        try:
            os.unlink(os.path.join(boots, n))
        except FileNotFoundError:
            pass
    return removed


def export_status(env: dict, fetch: Callable[[dict], dict] = fetch_status) -> dict:
    out_dir = _out_dir(env)
    group = env.get("ECHO_EXPORT_GROUP", "pantheon-shared")
    prepare_export_dir(out_dir)
    record, ok = build_status_record(env, fetch, out_dir)
    atomic_write_json(out_dir, STATUS_FILE, record, group)
    if ok:
        # Keep the previous boot's final reading before last-good moves on to
        # the new boot. A failed archive is a warning: last-good still refreshes.
        try:
            archive_previous_boot(out_dir, record, group)
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not archive the previous boot ({type(e).__name__})", file=sys.stderr)
        # echo-status.json is already current, so a failed last-good refresh
        # is a warning, not a failed export.
        try:
            atomic_write_json(out_dir, LAST_GOOD_FILE, record, group)
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not refresh {LAST_GOOD_FILE} ({type(e).__name__})", file=sys.stderr)
    return record


# ---------------------------------------------------------------------------
# Journal summary
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The level must be the first token of the tracing header, or the second after
# an RFC 3339 timestamp ("<ts> LEVEL target: msg"). An INFO line whose message
# merely contains "ERROR" does not match, and neither does a Pretty-format
# continuation line whose first token is not a timestamp ("Status: ERROR ...").
_LEVEL_LINE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}T\S+\s+)?(WARN|ERROR)\s+(.*)$")
_SPAN_PREFIX = re.compile(r"^(?:[\w-]+\{[^}]*\}:?\s*)+")
_TARGET = re.compile(r"^([A-Za-z_][\w-]*(?:::[\w-]+)*):\s+(.*)$")
_FIELD_SEP = re.compile(r",\s+[A-Za-z_][\w.]*:\s")
# tracing's text format appends span/event fields as " key=value" after the
# message; cut before the key so it does not end up in the signature.
_KV_FIELD = re.compile(r"\s+[A-Za-z_][\w.]*=")
# "(" catches a parenthesised error: "CSM query failed ({e}) — kept".
_CUT_CHARS = re.compile(r"[\"'`={\[<(]")
# A segment ending in one of these words is the end of the static text: what
# follows its ": " is the error value ("profile persist failed: {e}"). Matched
# on the whole last word, so "append failures" keeps "failures".
_FAILURE_TAIL = re.compile(r"\b(?:failed|failure|failures|error|errors|errored|err)$", re.I)
# A leading "<label>: " segment made of one token (tracing-style subsystem
# labels such as "claude.classify" or "reference_wrapper") stays readable.
# A leading token that looks like a Home Assistant entity id (domain.object)
# is not a label and still becomes <id>.
_LEAD_LABEL = re.compile(r"^[A-Za-z][\w.-]*$")
_HA_DOMAIN_LEAD = re.compile(
    r"^(?:ai_task|air_quality|alarm_control_panel|assist_satellite|automation|binary_sensor|"
    r"button|calendar|camera|climate|conversation|counter|cover|date|datetime|device_tracker|"
    r"event|fan|group|humidifier|image|image_processing|input_\w+|lawn_mower|light|lock|"
    r"media_player|notify|number|person|remote|scene|schedule|script|select|sensor|siren|stt|"
    r"sun|switch|tag|text|time|timer|todo|tts|update|vacuum|valve|wake_word|water_heater|"
    r"weather|zone)\.",
    re.I,
)
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.]+Z?\b")
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"(?<!\w)\d+(?:\.\d+)?")
_PANIC = re.compile(r"panicked at ([^\s:]+)")
# Dotted / underscored identifiers (entity ids like light.bedroom_lamp, file
# names, snake_case names) can carry user-chosen names; they become <id>.
_IDENT = re.compile(r"\b\w+(?:[._]\w+)+\b")
# An em-dash (U+2014) clause separator: what follows is usually upstream detail.
_EM_DASH = " \u2014 "

MAX_SIG_LEN = 160
MAX_SIGNATURES = 200
JOURNAL_GREP = "WARN|ERROR|panicked"
# T-4727: 60 s was too tight under heavy cargo builds on the 4-core host.
# Budget (unit TimeoutStartSec=120): systemctl 10 + journalctl 75 + status 20
# = 105 s worst case, leaving 15 s for startup and writes.
JOURNAL_TIMEOUT_S = 75


def normalize_message(msg: str) -> str:
    """Reduce a log message to its static template prefix.

    Most echo WARN/ERROR sites read "<static text>: {e}" or
    "<subsystem>: <static text>: {e}" (the subsystem is also in the tracing
    target). The steps:

    1. Cut at the first em-dash clause, field separator (", key: " or a
       trailing " key=value" tracing field), quote, '=', '{', '[', '<' or
       '('. The '(' drops a parenthesised error value.
    2. Keep at most the text before the second ': '.
    3. If what is left is "<text ending in a failure word>: <rest>", drop
       <rest>: it is the error value ("profile persist failed: {e}" becomes
       "profile persist failed").
    4. Replace uuids, timestamps, hex ids and numbers with placeholders, and
       dotted/underscored identifiers with <id>, except a single-token
       leading "<label>: " (e.g. "claude.classify: ...") that is not shaped
       like a Home Assistant entity id.
    """
    cut = msg.find(_EM_DASH)
    if cut >= 0:
        msg = msg[:cut]
    for pat in (_FIELD_SEP, _KV_FIELD):
        m = pat.search(msg)
        if m:
            msg = msg[: m.start()]
    m = _CUT_CHARS.search(msg)
    if m:
        msg = msg[: m.start()]
    parts = msg.split(": ")[:2]
    if len(parts) == 2 and _FAILURE_TAIL.search(parts[0].rstrip()):
        parts = parts[:1]
    msg = ": ".join(parts)
    msg = _UUID.sub("<uuid>", msg)
    msg = _TS.sub("<ts>", msg)
    msg = _HEX.sub("<hex>", msg)
    msg = _NUM.sub("<n>", msg)
    head, sep, tail = msg.partition(": ")
    if sep and _LEAD_LABEL.match(head) and not _HA_DOMAIN_LEAD.match(head):
        msg = head + sep + _IDENT.sub("<id>", tail)
    else:
        msg = _IDENT.sub("<id>", msg)
    msg = re.sub(r"\s+", " ", msg).strip(" ,;:-")
    return msg[:MAX_SIG_LEN]


def parse_log_line(text: str, priority: int | None = None) -> tuple[str, str, str] | None:
    """Return (level, target, normalized message) for WARN/ERROR/PANIC lines, else None.

    Handles tracing's JSON format ({"level","target","fields":{"message"}}),
    its text formats ("<ts> LEVEL [spans:] target: message ..."), and Rust
    panics. Falls back to the journal PRIORITY (3=ERROR, 4=WARN) when the
    text carries no level.
    """
    text = _ANSI.sub("", text).strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "level" in obj:
            level = str(obj.get("level", "")).upper()
            if level not in ("WARN", "ERROR"):
                return None
            fields = obj.get("fields") if isinstance(obj.get("fields"), dict) else {}
            message = str(fields.get("message", ""))
            return level, str(obj.get("target", "?")), normalize_message(message)
    pm = _PANIC.search(text)
    if pm:
        loc = _NUM.sub("<n>", pm.group(1))
        return "PANIC", "rust", f"panicked at {loc}"
    m = _LEVEL_LINE.search(text)
    if m:
        level, rest = m.group(1), m.group(2)
    elif priority is not None and priority <= 4:
        level, rest = ("ERROR" if priority <= 3 else "WARN"), text
    else:
        return None
    rest = _SPAN_PREFIX.sub("", rest)
    tm = _TARGET.match(rest)
    target, message = (tm.group(1), tm.group(2)) if tm else ("?", rest)
    return level, target, normalize_message(message)


def summarize_journal(entries: Iterable[dict]) -> list[dict]:
    """Group journal JSON entries into signature records."""
    sigs: dict[tuple[str, str, str], dict] = {}
    for e in entries:
        msg = e.get("MESSAGE")
        if isinstance(msg, list):  # journald emits non-UTF-8 as a byte list
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except (TypeError, ValueError):
                continue
        if not isinstance(msg, str):
            continue
        try:
            prio = int(e.get("PRIORITY")) if e.get("PRIORITY") is not None else None
        except (TypeError, ValueError):
            prio = None
        parsed = parse_log_line(msg, prio)
        if not parsed:
            continue
        level, target, message = parsed
        if looks_secret(message):
            message = "<redacted: secret-like>"
        key = (level, target, message)
        ts = _journal_ts(e.get("__REALTIME_TIMESTAMP"))
        rec = sigs.get(key)
        if rec is None:
            sigs[key] = {
                "level": level,
                "target": target,
                "signature": message,
                "count": 1,
                "first_seen": ts,
                "last_seen": ts,
            }
        else:
            rec["count"] += 1
            if ts:
                rec["first_seen"] = min(filter(None, [rec["first_seen"], ts]))
                rec["last_seen"] = max(filter(None, [rec["last_seen"], ts]))
    return sorted(sigs.values(), key=lambda r: (-r["count"], r["level"], r["target"], r["signature"]))


def _journal_ts(us: Any) -> str | None:
    try:
        t = int(us) / 1_000_000
    except (TypeError, ValueError):
        return None
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SYSTEMCTL_TIMEOUT_S = 10
_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
_UNIX_TS = re.compile(r"^@\d+$")
_ACTIVE_STATE = re.compile(r"^[a-z-]{1,32}$")
_SHOW_PROPS = ("InvocationID", "ActiveState", "ActiveEnterTimestamp")


def _timeout_desc(e: subprocess.TimeoutExpired) -> str:
    """Name the subprocess that timed out, with its timeout (T-4727)."""
    cmd = e.cmd[0] if isinstance(e.cmd, (list, tuple)) and e.cmd else e.cmd
    return f"{os.path.basename(str(cmd))}: TimeoutExpired after {e.timeout:g}s"


class UnitNeverStarted(Exception):
    """The unit has neither an InvocationID nor an ActiveEnterTimestamp."""


def _systemctl_show(
    unit: str, scope: str, props: Iterable[str] = _SHOW_PROPS, timeout: float = SYSTEMCTL_TIMEOUT_S
) -> dict[str, str]:
    """Return ``systemctl [--user] show --timestamp=unix -p <prop>... <unit>``
    parsed as {prop: value} (one ``key=value`` line per property).

    ``--timestamp=unix`` makes timestamps ``@<epoch>``. Raises RuntimeError on
    a non-zero exit and OSError / subprocess.TimeoutExpired as subprocess.run
    does.
    """
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd += ["show", "--timestamp=unix"]
    for prop in props:
        cmd += ["-p", prop]
    cmd += ["--", unit]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"systemctl show exited {res.returncode}")
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def journal_window(
    unit: str, scope: str, show: Callable[[str, str], dict[str, str]] = _systemctl_show
) -> dict[str, str]:
    """Pick the journal window for the unit's current or most recent run.

    One ``systemctl show`` call reads InvocationID, ActiveState and
    ActiveEnterTimestamp. systemd keeps InvocationID after the unit stops, so
    a stopped or failed daemon still scans its last run. Returns
    {"invocation_id": id, "active_state": state} when systemd reports an id
    (journal match ``_SYSTEMD_INVOCATION_ID``, which tags the unit's own
    output for both system and --user units), else
    {"since": "@<epoch>", "active_state": state} from ActiveEnterTimestamp.
    Raises UnitNeverStarted when neither is available (the unit never ran
    under this manager, or ECHO_JOURNAL_UNIT / ECHO_JOURNAL_SCOPE is wrong),
    and passes through the query's RuntimeError / OSError / TimeoutExpired.
    Never widens to the whole boot.
    """
    props = show(unit, scope)
    state = props.get("ActiveState", "")
    window: dict[str, str] = {}
    inv = props.get("InvocationID", "")
    since = props.get("ActiveEnterTimestamp", "")
    if _INVOCATION_ID.match(inv):
        window["invocation_id"] = inv
    elif _UNIX_TS.match(since):
        window["since"] = since
    else:
        raise UnitNeverStarted("unit never started (or wrong ECHO_JOURNAL_UNIT/SCOPE)")
    window["active_state"] = state if _ACTIVE_STATE.match(state) else "unknown"
    return window


def journal_cmd(unit: str, scope: str, window: dict[str, str]) -> list[str]:
    """Build the journalctl command for ``window`` (see journal_window).

    The scan is always bounded to the unit's current or most recent run:
    never ``-b``, which on a long-lived host is the whole system boot.
    """
    if _INVOCATION_ID.match(window.get("invocation_id", "")):
        bound = [f"_SYSTEMD_INVOCATION_ID={window['invocation_id']}"]
    elif _UNIX_TS.match(window.get("since", "")):
        bound = [f"--since={window['since']}"]
    else:
        raise ValueError("journal window needs a 32-hex invocation_id or an @<epoch> since")
    cmd = ["journalctl"]
    if scope == "user":
        cmd.append("--user")
    cmd += [
        "-u", unit, *bound,
        # journald filters server-side: only candidate lines reach Python, and
        # only the three fields the summary uses are serialized.
        f"--grep={JOURNAL_GREP}",
        "-o", "json",
        "--output-fields=MESSAGE,PRIORITY,__REALTIME_TIMESTAMP",
        "--no-pager", "-q",
    ]
    return cmd


def read_journal(
    unit: str, scope: str, window: dict[str, str], timeout: float = JOURNAL_TIMEOUT_S
) -> Iterable[dict]:
    """Stream journal entries one line at a time (never the whole output at once).

    journalctl exits 1 when --grep matches nothing; with no output that is an
    empty result, not an error. Raises RuntimeError on any other failure and
    subprocess.TimeoutExpired when journalctl runs past ``timeout``.
    """
    cmd = journal_cmd(unit, scope, window)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    n = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                n += 1
                yield entry
        rc = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if rc != 0 and not (rc == 1 and n == 0):
        raise RuntimeError(f"journalctl exited {rc}")


class _Counted:
    """Iterate ``it`` while counting items, so the summary streams."""

    def __init__(self, it: Iterable[dict]):
        self._it = it
        self.n = 0

    def __iter__(self):
        for x in self._it:
            self.n += 1
            yield x


def export_journal(
    env: dict,
    reader: Callable[[str, str, dict[str, str]], Iterable[dict]] = read_journal,
    window_fn: Callable[[str, str], dict[str, str]] = journal_window,
) -> dict:
    out_dir = _out_dir(env)
    group = env.get("ECHO_EXPORT_GROUP", "pantheon-shared")
    unit = env.get("ECHO_JOURNAL_UNIT", "echo.service")
    scope = env.get("ECHO_JOURNAL_SCOPE", "user")
    prepare_export_dir(out_dir)
    record: dict[str, Any] = {
        "schema": SCHEMA_JOURNAL,
        "exported_at": utc_now_iso(),
        "unit": unit,
        "scope": scope,
        "grep": JOURNAL_GREP,
    }
    try:
        window = window_fn(unit, scope)
    except UnitNeverStarted as e:
        record["error"] = str(e)
    except RuntimeError as e:
        record["error"] = f"systemctl: {e}"
    except subprocess.TimeoutExpired as e:
        record["error"] = _timeout_desc(e)
    except OSError as e:
        record["error"] = f"systemctl: {type(e).__name__}"
    except Exception as e:  # noqa: BLE001 - never leave the summary stale
        record["error"] = f"exporter error ({type(e).__name__})"
    if "error" in record:
        atomic_write_json(out_dir, JOURNAL_FILE, record, group)
        return record
    # Which run is scanned: {"invocation_id": ...} or {"since": "@<epoch>"},
    # plus the unit's "active_state" when the summary was taken.
    record["window"] = window
    try:
        counted = _Counted(reader(unit, scope, window))
        sigs = summarize_journal(counted)
    except RuntimeError as e:
        record["error"] = str(e)
    except subprocess.TimeoutExpired as e:
        record["error"] = _timeout_desc(e)
    except OSError as e:
        record["error"] = type(e).__name__
    except Exception as e:  # noqa: BLE001 - never leave the summary stale
        record["error"] = f"exporter error ({type(e).__name__})"
    else:
        # Entries journald returned for --grep (candidates, before level parsing).
        record["entries_matched"] = counted.n
        record["totals"] = {
            lvl: sum(s["count"] for s in sigs if s["level"] == lvl) for lvl in ("ERROR", "WARN", "PANIC")
        }
        record["signatures"] = sigs[:MAX_SIGNATURES]
        record["truncated"] = max(0, len(sigs) - MAX_SIGNATURES)
    atomic_write_json(out_dir, JOURNAL_FILE, record, group)
    return record


def _summary(name: str, fn: Callable[[dict], dict], env: dict) -> tuple[str, bool]:
    """Run one export; report its outcome. An exception here means the file
    itself could not be written (e.g. unsafe export dir)."""
    try:
        rec = fn(env)
    except Exception as e:  # noqa: BLE001 - the other export must still run
        return f"{name}=FAILED ({type(e).__name__}: {e})", False
    if "error" in rec:
        return f"{name}=error: {rec['error']}", True
    if "signatures" in rec:
        return f"{name}={len(rec['signatures'])} signatures", True
    return f"{name}=ok", True


# ---------------------------------------------------------------------------
# Reader-side verification (--verify)
# ---------------------------------------------------------------------------

VERIFY_OK = 0
VERIFY_DAEMON_ERROR = 1  # trusted and fresh, but the record carries "error"
VERIFY_ALERT = 2  # missing, untrusted or stale: do not use the file


class Untrusted(Exception):
    """A reader-side trust check failed; the message is the one-line reason."""


def _owner_uid(owner: str) -> int:
    import pwd

    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        raise Untrusted(f"no such user: {owner}") from None


def _check_trusted(p: str, want_dir: bool, uid: int, owner: str) -> None:
    """lstat ``p``: not a symlink, a dir / regular file, owned by ``uid``, not
    group- or world-writable. Raises Untrusted (FileNotFoundError passes
    through as Untrusted too, with its type name)."""
    try:
        st = os.lstat(p)
    except OSError as e:
        raise Untrusted(f"{p}: {type(e).__name__}") from e
    if stat.S_ISLNK(st.st_mode):
        raise Untrusted(f"{p}: is a symlink")
    if want_dir and not stat.S_ISDIR(st.st_mode):
        raise Untrusted(f"{p}: not a directory")
    if not want_dir and not stat.S_ISREG(st.st_mode):
        raise Untrusted(f"{p}: not a regular file")
    if st.st_uid != uid:
        raise Untrusted(f"{p}: owned by uid {st.st_uid}, expected {owner} ({uid})")
    if st.st_mode & 0o022:
        raise Untrusted(f"{p}: group/world-writable (mode {stat.S_IMODE(st.st_mode):o})")


def _read_trusted_json(path: str, uid: int, owner: str) -> Any:
    """_check_trusted the file, then open it O_NOFOLLOW, re-check the owner on
    the open fd, and parse it. Raises Untrusted."""
    _check_trusted(path, False, uid, owner)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as f:
            if os.fstat(f.fileno()).st_uid != uid:
                raise Untrusted(f"{path}: replaced during check")
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Untrusted(f"{path}: unreadable or malformed ({type(e).__name__})") from e


def verify_export(directory: str, max_age_s: float, owner: str = "claude") -> tuple[int, str]:
    """Check echo-status.json from a reader's side. Returns (exit code, reason).

    Run by any pantheon-shared reader (it needs no write access). The export
    dir and the file must not be symlinks, must be a directory / regular file
    owned by ``owner``, and must not be group- or world-writable; the file
    must parse, and ``exported_at`` must be at most ``max_age_s`` old. Any
    failure is VERIFY_ALERT. A trusted, fresh record with an ``error`` key is
    VERIFY_DAEMON_ERROR.
    """
    try:
        uid = _owner_uid(owner)
        path = os.path.join(directory, STATUS_FILE)
        _check_trusted(directory, True, uid, owner)
        record = _read_trusted_json(path, uid, owner)
    except Untrusted as e:
        return VERIFY_ALERT, str(e)
    try:
        ts = _dt.datetime.strptime(record["exported_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (ValueError, KeyError, TypeError) as e:
        return VERIFY_ALERT, f"{path}: unreadable or malformed ({type(e).__name__})"
    age = (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds()
    if age > max_age_s:
        return VERIFY_ALERT, f"stale: exported_at {record['exported_at']} is {int(age)} s old (max {int(max_age_s)})"
    if age < -60:
        return VERIFY_ALERT, f"exported_at {record['exported_at']} is in the future"
    if "error" in record:
        return VERIFY_DAEMON_ERROR, f"fresh ({int(age)} s) but error: {record['error']}"
    return VERIFY_OK, f"ok: fresh ({int(age)} s), owner {owner}, not a symlink"


# T-4337 label-hold counters (crates/echo/src/status.rs MetricsRollup).
DEFAULT_BOOT_KEYS = (
    "metrics.stt_label_final_count",
    "metrics.stt_label_held_count",
    "metrics.stt_label_hold_max_ms",
)
_MAX_KEY = re.compile(r"(?:^|_)max(?:_|$)")


def _dotted(obj: Any, key: str) -> Any:
    for part in key.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def read_boots(directory: str, keys: Iterable[str], owner: str = "claude") -> list[dict]:
    """Collect one row per archived boot plus the current boot, oldest first.

    Every dir and file read passes the same trust checks as --verify (lstat
    not a symlink, owned by ``owner``, not group/world-writable, O_NOFOLLOW).
    A missing boots/ dir means no archived boots yet. The current boot comes
    from echo-status.json, or from echo-status.last-good.json when the current
    record carries an error. Row: {started_at, exported_at, source, values}.
    When the current record has an error and last-good fails its trust check
    (or is missing), a warning goes to stderr and the last row is a
    placeholder with ``unavailable: True``, source
    ``current: unavailable (<reason>)`` and all values None.
    Raises Untrusted for any other failed check.
    """
    keys = list(keys)
    uid = _owner_uid(owner)
    _check_trusted(directory, True, uid, owner)
    rows: list[dict] = []

    def row(rec: Any, source: str) -> dict:
        status = rec.get("status") if isinstance(rec, dict) else None
        return {
            "started_at": _started_at(rec),
            "exported_at": rec.get("exported_at") if isinstance(rec, dict) else None,
            "source": source,
            "values": {k: _dotted(status, k) for k in keys},
        }

    boots = os.path.join(directory, BOOTS_DIR)
    if os.path.lexists(boots):
        _check_trusted(boots, True, uid, owner)
        try:
            names = sorted(os.listdir(boots))
        except OSError as e:
            raise Untrusted(f"{boots}: {type(e).__name__}") from e
        for name in names:
            if _BOOT_FILE.fullmatch(name):
                rows.append(row(_read_trusted_json(os.path.join(boots, name), uid, owner), f"{BOOTS_DIR}/{name}"))
    current = _read_trusted_json(os.path.join(directory, STATUS_FILE), uid, owner)
    source = STATUS_FILE
    if not isinstance(current, dict) or "status" not in current:
        try:
            current = _read_trusted_json(os.path.join(directory, LAST_GOOD_FILE), uid, owner)
            source = f"{LAST_GOOD_FILE} (current has error)"
        except Untrusted as e:
            reason = f"current has error; last-good: {e}"
            print(f"warning: current boot unavailable: {reason}", file=sys.stderr)
            rows.append({"started_at": None, "exported_at": None, "source": f"current: unavailable ({reason})",
                         "values": {k: None for k in keys}, "unavailable": True})
            current = None
    if current is not None:
        cur = row(current, source)
        # The current boot supersedes an archive of the same boot.
        rows = [r for r in rows if r["started_at"] != cur["started_at"]]
        rows.append(cur)
    return rows


def boot_totals(rows: list[dict], keys: Iterable[str]) -> dict[str, Any]:
    """Aggregate each numeric key across rows, only where that means something.

    By the key's last segment: one with a ``max`` word (e.g.
    stt_label_hold_max_ms) takes the max; else one ending in ``_count`` is
    summed; anything else (p50_ms, p95_ms, a mean, a gauge) has no meaningful
    total and gets None. None also when no row has a number."""
    out: dict[str, Any] = {}
    for k in keys:
        last = k.rsplit(".", 1)[-1]
        nums = [r["values"][k] for r in rows if _is_num(r["values"].get(k))]
        if not nums:
            out[k] = None
        elif _MAX_KEY.search(last):
            out[k] = max(nums)
        elif last.endswith("_count"):
            out[k] = sum(nums)
        else:
            out[k] = None
    return out


def format_boots(rows: list[dict], keys: list[str]) -> str:
    totals = boot_totals(rows, keys)
    header = ["started_at", "final_exported_at", *[k.rsplit(".", 1)[-1] for k in keys], "source"]

    def cell(v: Any) -> str:
        return "-" if v is None else str(v)

    table = [header]
    for r in rows:
        table.append([cell(r["started_at"]), cell(r["exported_at"]), *[cell(r["values"][k]) for k in keys],
                      r["source"]])
    n = sum(1 for r in rows if not r.get("unavailable"))
    label = f"TOTAL ({n} boots; current unavailable)" if n != len(rows) else f"TOTAL ({n} boots)"
    table.append([label, "", *[cell(totals[k]) for k in keys], ""])
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip() for line in table)


def _positive_seconds(value: str) -> float:
    """argparse type for --max-age: a finite number of seconds greater than 0."""
    import argparse
    import math

    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not math.isfinite(f) or f <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds > 0, got {value!r}")
    return f


def main(argv: list[str] | None = None) -> int:
    """Run the status export and the journal export independently.

    Exit status is 1 only when an export could not write its file at all, so
    systemd marks the run failed; an error record written in place is 0.
    With ``--verify`` it instead checks the export from a reader's side (see
    verify_export) and exits 0 ok, 1 daemon error, 2 alert. With ``--boots``
    it prints the per-boot table (see read_boots) and exits 0, or 2 when a
    trust check fails. A current boot that is unavailable only because the
    last-good fallback failed is shown as a row and still exits 0.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Echo read-only status export (T-4420).")
    ap.add_argument("--verify", action="store_true", help="check the export as a reader; write nothing")
    ap.add_argument("--max-age", type=_positive_seconds, default=300.0, help="--verify: max exported_at age in seconds")
    ap.add_argument("--boots", action="store_true",
                    help="print the per-boot counters (archived boots + current) with totals; write nothing")
    ap.add_argument("--key", action="append", default=None,
                    help="--boots: dotted key under status, repeatable "
                         f"(default {' '.join(DEFAULT_BOOT_KEYS)})")
    ap.add_argument("--dir", default=None,
                    help=f"--verify/--boots: export dir (default $ECHO_EXPORT_DIR or {DEFAULT_EXPORT_DIR})")
    ap.add_argument("--owner", default="claude", help="--verify/--boots: expected owner of the export")
    args = ap.parse_args([] if argv is None else argv)
    if args.boots:
        keys = args.key or list(DEFAULT_BOOT_KEYS)
        try:
            rows = read_boots(args.dir or _out_dir(dict(os.environ)), keys, args.owner)
        except Untrusted as e:
            print(f"echo-status-export --boots: {e}")
            return VERIFY_ALERT
        print(format_boots(rows, keys))
        return VERIFY_OK
    if args.verify:
        code, reason = verify_export(args.dir or _out_dir(dict(os.environ)), args.max_age, args.owner)
        print(f"echo-status-export --verify: {reason}")
        return code
    env = dict(os.environ)
    status_line, status_ok = _summary("status", export_status, env)
    journal_line, journal_ok = _summary("journal", export_journal, env)
    print(f"echo-status-export: {status_line} {journal_line}")
    return 0 if status_ok and journal_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
