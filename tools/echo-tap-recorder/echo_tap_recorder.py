#!/usr/bin/env python3
"""Standing passive recorder of the live Echo tap (T-5260).

Runs on the claude account (where the live Echo daemon and broker client live) as a
long-running systemd --user service. It connects to the Echo MQTT broker, subscribes at
QoS 0 to ``echo/+/tap/#`` and ``echo/stt/final``, and appends every message it receives as
one JSON line to an hourly file in a claude-owned mode 2750 subdir of the pantheon-shared
evidence dir, so other seats can read live tap traffic after a deploy without the HO
recording it for them.

Read-only by construction: the module holds no code that builds an MQTT PUBLISH packet.
The only packets it ever writes are CONNECT, SUBSCRIBE, PINGREQ and DISCONNECT. It talks
MQTT 3.1.1 itself (stdlib only; paho is not installed and mosquitto_sub would put the
password in argv, readable by other accounts in /proc).

Self-contained on purpose (T-5260 DECISION): it does not import echo_status_export's MQTT
helpers. The install copies one file into ~/.local/lib/echo-tap-recorder/, so a cross-dir
import would complicate install, and that module carries build_publish, which this process
must not have.

What is dropped:
- Topics whose tap name is in ECHO_TAP_EXCLUDE (default ``tts_raw``: base64 s16le TTS PCM,
  echo-prismatic crates/echo-adapter-speechmatics-tts/src/lib.rs publish_tts_raw). They are
  counted, not stubbed. ``tts_raw_eos`` carries no audio and is kept.
- Payloads above ECHO_TAP_MAX_PAYLOAD_BYTES (default 64 KiB) are replaced by a stub
  ``{"ts", "topic", "dropped": "oversize", "bytes": N}``.
No redaction: on-host taps are internal (ORG-105 section 3).

Record: ``{"ts": "<receive time, UTC, microseconds>Z", "topic": ..., "payload": <JSON>}``,
``payload`` is ``{"_non_json": "<utf-8 text, replacement chars>"}`` when not JSON, and
``"retained": true`` is added for a retained message. Lines are written with one O_APPEND
write each, so a reader tailing the file sees them at once; fsync runs every
ECHO_TAP_FSYNC_S seconds, on rotation and on shutdown. A local write error (ENOSPC, EIO,
EACCES) drops that message, counts it in ``write_errors`` and closes the file so the next
message reopens it; it never ends the MQTT session. A partial line is truncated away (or,
failing that, ended with a newline) so the next record starts clean.

Files: ``tap-YYYYMMDDTHH.jsonl`` (UTC hour), mode 0640. Retention runs at startup and on
every rotation: files whose hour ended more than ECHO_TAP_RETENTION_DAYS ago are deleted,
then the oldest files are deleted until the total is at most ECHO_TAP_MAX_BYTES. The
current hour's file is never deleted, and only regular files matching that exact name
pattern inside the owned dir are ever touched.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time
from typing import Callable

DEFAULT_DIR = "/var/lib/pantheon/echo-evidence/tap"
TOPICS = ("echo/+/tap/#", "echo/stt/final")
FILE_MODE = 0o640
DIR_MODE = 0o2750
FILE_RE = re.compile(r"^tap-(\d{8})T(\d{2})\.jsonl$")

EXCLUDED = "excluded"
OVERSIZE = "oversize"
KEEP = "keep"

PROG = "echo-tap-recorder"


def log(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    pass


def _num(env: dict, key: str, default, cast=int, lo=None, hi=None):
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise ConfigError(f"{key} is not a number: {raw!r}") from None
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError(f"{key} out of range: {raw!r}")
    return v


class Config:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self) -> str:
        shown = {k: v for k, v in self.__dict__.items() if k != "password"}
        shown["password"] = "<set>" if self.password else None
        return f"Config({shown})"

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        exclude = frozenset(x.strip() for x in (env.get("ECHO_TAP_EXCLUDE") or "").split(",") if x.strip())
        return cls(
            host=(env.get("ECHO_MQTT_HOST") or "").strip() or "127.0.0.1",
            port=_num(env, "ECHO_MQTT_PORT", 1883, int, 1, 65535),
            username=env.get("ECHO_MQTT_USERNAME") or None,
            password=env.get("ECHO_MQTT_PASSWORD") or None,
            directory=(env.get("ECHO_TAP_DIR") or "").strip() or DEFAULT_DIR,
            group=(env.get("ECHO_TAP_GROUP") or "").strip() or "pantheon-shared",
            retention_days=_num(env, "ECHO_TAP_RETENTION_DAYS", 7, float, 0.001),
            max_bytes=_num(env, "ECHO_TAP_MAX_BYTES", 2 * 1024 ** 3, int, 1),
            max_payload=_num(env, "ECHO_TAP_MAX_PAYLOAD_BYTES", 64 * 1024, int, 1),
            exclude=exclude or frozenset({"tts_raw"}),
            fsync_s=_num(env, "ECHO_TAP_FSYNC_S", 60.0, float, 0.0),
            stats_s=_num(env, "ECHO_TAP_STATS_S", 600.0, float, 0.01),
        )


# ---------------------------------------------------------------------------
# Classification and records
# ---------------------------------------------------------------------------


def tap_name(topic: str) -> str | None:
    """``echo/<ctx>/tap/<name>`` -> ``<name>`` (mirrors echo-j17-bridge mapping.rs tap_name)."""
    if not topic.startswith("echo/"):
        return None
    _, sep, rest = topic[5:].partition("/tap/")
    return rest if sep else None


def classify(topic: str, size: int, cfg: Config) -> str:
    name = tap_name(topic)
    if name is not None and name.split("/", 1)[0] in cfg.exclude:
        return EXCLUDED
    if size > cfg.max_payload:
        return OVERSIZE
    return KEEP


def iso_us(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_record(topic: str, payload: bytes, ts: float, retained: bool = False) -> dict:
    """``retained: true`` is added only for a retained message (the broker replayed a stored
    value on subscribe, so ``ts`` is not when it was published); it is omitted otherwise."""
    try:
        parsed = json.loads(payload)
    except ValueError:  # includes UnicodeDecodeError and empty input
        parsed = {"_non_json": payload.decode("utf-8", "replace")}
    rec = {"ts": iso_us(ts), "topic": topic, "payload": parsed}
    if retained:
        rec["retained"] = True
    return rec


def make_stub(topic: str, size: int, ts: float) -> dict:
    return {"ts": iso_us(ts), "topic": topic, "dropped": "oversize", "bytes": size}


def file_name(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("tap-%Y%m%dT%H.jsonl")


# ---------------------------------------------------------------------------
# Output dir and retention
# ---------------------------------------------------------------------------


class TapDirError(Exception):
    """The tap dir is unsafe to write into (symlink, not a dir, wrong owner)."""


def prepare_tap_dir(directory: str) -> None:
    """Create the recorder-owned mode 2750 subdir if missing, then check it.

    Same contract as echo_status_export.prepare_export_dir (T-4420): the parent is
    group-writable by every pantheon-shared member, so the subdir is owned by the recorder
    and not group-writable; members read the files but cannot replace or delete them.
    A new subdir of a setgid dir inherits the group and setgid bit. Refuses a symlink, a
    non-directory, or a dir owned by someone else (e.g. one another member pre-created).
    """
    try:
        os.mkdir(directory, 0o750)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise TapDirError(f"tap dir is not a directory (or is a symlink): {directory}")
    if st.st_uid != os.getuid():
        raise TapDirError(f"tap dir not owned by the recorder: {directory}")
    if stat.S_IMODE(st.st_mode) != DIR_MODE:
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            try:
                os.chmod(directory, DIR_MODE & 0o777)
            except OSError:
                log(f"warning: could not chmod {directory}")


def _hour_start(name: str) -> float | None:
    mt = FILE_RE.match(name)
    if not mt:
        return None
    try:
        d = _dt.datetime.strptime(mt.group(1) + mt.group(2), "%Y%m%d%H")
    except ValueError:
        return None
    return d.replace(tzinfo=_dt.timezone.utc).timestamp()


def apply_retention(directory: str, now: float, retention_days: float, max_bytes: int) -> list[str]:
    """Delete expired tap files, then the oldest until the total fits. Returns removed names.

    Only regular files (never symlinks or dirs) whose name matches tap-YYYYMMDDTHH.jsonl are
    considered; the file for the current hour is never removed.
    """
    current = file_name(now)
    files = []
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        log(f"retention: cannot list {directory}: {e}")
        return []
    for e in entries:
        start = _hour_start(e.name)
        if start is None:
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        files.append((e.name, start, size))
    files.sort()
    removed: list[str] = []

    def remove(name: str) -> bool:
        try:
            os.unlink(os.path.join(directory, name))
        except FileNotFoundError:
            pass
        except OSError as err:
            log(f"retention: could not delete {name}: {err}")
            return False
        removed.append(name)
        return True

    cutoff = now - retention_days * 86400
    kept = []
    for name, start, size in files:
        if name != current and start + 3600 <= cutoff and remove(name):
            continue
        kept.append((name, size))
    total = sum(s for _, s in kept)
    for name, size in kept:
        if total <= max_bytes:
            break
        if name != current and remove(name):
            total -= size
    return removed


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _fchgrp(fd: int, directory: str, group: str) -> None:
    """chgrp a new file only when the dir did not already hand it the group (T-4420)."""
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
            log(f"warning: could not chgrp tap file to {group}")


class TapWriter:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self.fd: int | None = None
        self.name: str | None = None
        self.last_fsync = 0.0
        self.dirty = False
        self.stats = {"written": 0, "excluded": 0, "oversize": 0, "write_errors": 0, "rotations": 0}
        self._next_err_log: float | None = None

    def start(self) -> None:
        """Retention at startup (before the first message)."""
        self._retention(self.clock())

    def _retention(self, now: float) -> None:
        removed = apply_retention(self.cfg.directory, now, self.cfg.retention_days, self.cfg.max_bytes)
        if removed:
            log(f"retention: deleted {len(removed)} file(s): {', '.join(removed)}")

    def _open(self, name: str) -> None:
        path = os.path.join(self.cfg.directory, name)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, FILE_MODE)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise OSError(f"refusing tap file not a regular file owned by the recorder: {path}")
            if stat.S_IMODE(st.st_mode) != FILE_MODE:
                os.fchmod(fd, FILE_MODE)
            _fchgrp(fd, self.cfg.directory, self.cfg.group)
        except BaseException:
            os.close(fd)
            raise
        self.fd, self.name = fd, name

    def _close_current(self) -> None:
        if self.fd is None:
            return
        try:
            if self.dirty:
                os.fsync(self.fd)
        finally:
            os.close(self.fd)
            self.fd, self.dirty = None, False

    def _ensure_file(self, now: float) -> None:
        name = file_name(now)
        if name == self.name and self.fd is not None:
            return
        rotating = self.name is not None and name != self.name
        self._close_current()
        self._open(name)
        self.last_fsync = now
        if rotating:
            self.stats["rotations"] += 1
            self._retention(now)

    def _write(self, obj: dict, now: float) -> None:
        self._ensure_file(now)
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        view = memoryview(data)
        start = None
        try:
            start = os.fstat(self.fd).st_size  # O_APPEND: the line lands at the current end
            while view:
                n = os.write(self.fd, view)
                view = view[n:]
        except OSError:
            if len(view) < len(data):
                self._untear(start)
            raise
        self.dirty = True
        if now - self.last_fsync >= self.cfg.fsync_s:
            os.fsync(self.fd)
            self.last_fsync, self.dirty = now, False

    def _untear(self, start: int | None) -> None:
        """After a partial line: cut it off, or at least end it so the next record starts clean."""
        try:
            if start is None:
                raise OSError("unknown line start")
            os.ftruncate(self.fd, start)
            return
        except OSError:
            pass
        try:
            os.write(self.fd, b"\n")
        except OSError:
            pass

    def _write_error(self, e: OSError) -> None:
        """Drop the message, count it, close the file so the next message reopens it.

        A write error is a local disk problem, not a broker problem, so it never ends the
        MQTT session. Logged at most once per stats interval.
        """
        self.stats["write_errors"] += 1
        fd, self.fd, self.name, self.dirty = self.fd, None, None, False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        mono = time.monotonic()
        if self._next_err_log is None or mono >= self._next_err_log:
            log(f"write error, message dropped (write_errors={self.stats['write_errors']}): {e}")
            self._next_err_log = mono + self.cfg.stats_s

    def handle(self, topic: str, payload: bytes, retained: bool = False) -> None:
        now = self.clock()
        kind = classify(topic, len(payload), self.cfg)
        if kind == EXCLUDED:
            self.stats["excluded"] += 1
            return
        try:
            if kind == OVERSIZE:
                self._write(make_stub(topic, len(payload), now), now)
                self.stats["oversize"] += 1
                return
            self._write(make_record(topic, payload, now, retained), now)
        except OSError as e:
            self._write_error(e)
            return
        self.stats["written"] += 1

    def close(self) -> None:
        self._close_current()
        self.name = None


# ---------------------------------------------------------------------------
# Minimal read-only MQTT 3.1.1 client: CONNECT / SUBSCRIBE / PINGREQ / DISCONNECT only.
# There is deliberately no PUBLISH builder here (T-5260).
# ---------------------------------------------------------------------------


class MqttError(Exception):
    """Broker-level failure: refused, closed, malformed, or keepalive lost."""


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


def build_connect(client_id: str, username: str | None, password: str | None, keepalive: int) -> bytes:
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


def build_subscribe(packet_id: int, topics) -> bytes:
    body = struct.pack("!H", packet_id) + b"".join(_enc_str(t) + b"\x00" for t in topics)
    return _packet(0x82, body)


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


def client_id() -> str:
    return f"{PROG}-{socket.gethostname()}-{os.getpid()}"


class _Stream:
    """Incremental packet parser over a socket with a deadline-aware read."""

    def __init__(self, sock: socket.socket, stop: threading.Event | None = None):
        self.sock = sock
        self.stop = stop
        self.buf = bytearray()

    def parse(self):
        if len(self.buf) < 2:
            return None
        i, mult, length = 1, 1, 0
        while True:
            if i > 4:
                raise MqttError("malformed remaining length")
            if i >= len(self.buf):
                return None
            b = self.buf[i]
            i += 1
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        if len(self.buf) < i + length:
            return None
        h, body = self.buf[0], bytes(self.buf[i:i + length])
        del self.buf[:i + length]
        return h, body

    def fill(self, timeout: float) -> bool:
        """Read what is available within timeout. False on timeout; raises on close."""
        r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        if not r:
            return False
        chunk = self.sock.recv(262144)
        if not chunk:
            raise MqttError("broker closed connection")
        self.buf += chunk
        return True

    def read_packet(self, timeout: float):
        """Wait up to timeout for one packet, in short slices so a stop request ends it."""
        deadline = time.monotonic() + timeout
        while True:
            pk = self.parse()
            if pk is not None:
                return pk
            if self.stop is not None and self.stop.is_set():
                raise MqttError("stopping")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MqttError("timed out waiting for broker")
            self.fill(min(remaining, 0.25))


class Recorder:
    def __init__(
        self,
        cfg: Config,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.time,
        keepalive: float = 30.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
    ):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.writer = TapWriter(cfg, clock)
        self.keepalive = keepalive
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.stats = {"reconnects": 0, "connect_failures": 0, "pings": 0, "sessions": 0}
        self._subscribed = False
        self._next_stats = time.monotonic() + cfg.stats_s

    def stats_line(self) -> str:
        w = self.writer.stats
        return (f"stats written={w['written']} excluded={w['excluded']} oversize={w['oversize']} "
                f"write_errors={w['write_errors']} reconnects={self.stats['reconnects']} "
                f"file={self.writer.name or '-'}")

    def _maybe_stats(self) -> None:
        if time.monotonic() >= self._next_stats:
            log(self.stats_line())
            self._next_stats = time.monotonic() + self.cfg.stats_s

    def run(self) -> None:
        self.writer.start()
        failures = 0
        try:
            while not self.stop.is_set():
                self._subscribed = False
                try:
                    self._session()
                except (MqttError, OSError) as e:
                    if self.stop.is_set():
                        break
                    log(f"connection lost: {e}")
                if self.stop.is_set():
                    break
                if self._subscribed:
                    # A working session dropped: count it and retry fast.
                    self.stats["reconnects"] += 1
                    failures = 0
                else:
                    self.stats["connect_failures"] += 1
                delay = min(self.backoff_cap, self.backoff_base * (2 ** failures))
                failures += 1
                log(f"reconnect in {delay:.1f}s (reconnects={self.stats['reconnects']}, "
                    f"connect_failures={self.stats['connect_failures']})")
                self.stop.wait(delay)
        finally:
            log(self.stats_line())
            self.writer.close()

    def _session(self) -> None:
        """One broker connection: CONNECT, SUBSCRIBE, then receive until it ends.

        Sets self._subscribed once the SUBACK granted every topic. Returns only when stop
        is set; otherwise raises MqttError / OSError.
        """
        ka = max(1, int(-(-self.keepalive // 1)))
        # A connect to a black-holed host blocks up to this long, inside TimeoutStopSec=35.
        sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=5)
        try:
            st = _Stream(sock, self.stop)
            sock.sendall(build_connect(client_id(), self.cfg.username, self.cfg.password, ka))
            h, body = st.read_packet(10)
            if h != 0x20 or len(body) < 2:
                raise MqttError("unexpected packet instead of CONNACK")
            if body[1] != 0:
                raise MqttError(f"broker refused connection (CONNACK rc={body[1]})")
            sock.sendall(build_subscribe(1, TOPICS))
            while True:
                h, body = st.read_packet(10)
                if h == 0x90:
                    if len(body) < 2 + len(TOPICS) or any(c == 0x80 for c in body[2:]):
                        raise MqttError("broker refused a subscription (SUBACK 0x80)")
                    break
                if h >> 4 == 3:
                    self._on_publish(h, body)
            self._subscribed = True
            self.stats["sessions"] += 1
            log(f"connected to {self.cfg.host}:{self.cfg.port}, subscribed {' '.join(TOPICS)}")
            self._loop(sock, st)
        finally:
            if self.stop.is_set():
                try:
                    sock.sendall(DISCONNECT)
                except OSError:
                    pass
            sock.close()

    def _loop(self, sock: socket.socket, st: _Stream) -> None:
        next_ping = time.monotonic() + self.keepalive
        ping_deadline = None
        while not self.stop.is_set():
            self._maybe_stats()
            now = time.monotonic()
            if ping_deadline is not None and now >= ping_deadline:
                raise MqttError("no PINGRESP within keepalive; connection presumed dead")
            if now >= next_ping:
                sock.sendall(PINGREQ)
                self.stats["pings"] += 1
                next_ping = now + self.keepalive
                if ping_deadline is None:
                    ping_deadline = now + self.keepalive
            wake = min(next_ping, ping_deadline or next_ping, now + 0.25)
            st.fill(wake - time.monotonic())
            while True:
                pk = st.parse()
                if pk is None:
                    break
                h, body = pk
                if h >> 4 == 3:
                    self._on_publish(h, body)
                elif h == 0xD0:
                    ping_deadline = None

    def _on_publish(self, h: int, body: bytes) -> None:
        qos = (h >> 1) & 0x03
        if len(body) < 2:
            raise MqttError("malformed PUBLISH")
        tlen = struct.unpack("!H", body[:2])[0]
        off = 2 + tlen + (2 if qos else 0)
        if off > len(body):
            raise MqttError("malformed PUBLISH")
        topic = body[2:2 + tlen].decode("utf-8", "replace")
        self.writer.handle(topic, body[off:], retained=bool(h & 0x01))


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ) if env is None else env
    try:
        cfg = Config.from_env(env)
    except ConfigError as e:
        log(f"bad config: {e}")
        return 2
    try:
        prepare_tap_dir(cfg.directory)
    except (TapDirError, OSError) as e:
        log(f"refusing to record: {e}")
        return 1
    stop = threading.Event()

    def _term(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log(f"recording {' '.join(TOPICS)} from {cfg.host}:{cfg.port} into {cfg.directory} "
        f"(exclude={','.join(sorted(cfg.exclude))}, max_payload={cfg.max_payload}, "
        f"retention={cfg.retention_days}d/{cfg.max_bytes}B)")
    Recorder(cfg, stop=stop).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
