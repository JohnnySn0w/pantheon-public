"""Tests for echo_tap_recorder (T-5260). Run: python3 -m pytest tools/echo-tap-recorder -q
from the repo root, or python3 -m unittest test_echo_tap_recorder from this directory.

Fixture: fixtures/t3571-tap-sample.log holds 14 real tap lines (one utterance end to end
plus a conversation state change) copied from the 2026-10-17 wet-run passive capture, in
that capture's "<epoch> <topic> <payload>" form. The tts_raw payload is built from the
publisher's shape (echo-prismatic crates/echo-adapter-speechmatics-tts/src/lib.rs
publish_tts_raw); no tts_raw message was ever captured. Tests never read /var, a seat home
or an absolute /tmp path: every dir is a tempfile dir.
"""

import base64
import errno
import datetime as _dt
import io
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import echo_tap_recorder as m  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "t3571-tap-sample.log")


def load_fixture():
    out = []
    with open(FIXTURE, encoding="utf-8") as f:
        for line in f:
            epoch, topic, payload = line.rstrip("\n").split(" ", 2)
            out.append((topic, payload.encode("utf-8")))
    return out


def tts_raw_payload(n_bytes=96000):
    pcm = base64.b64encode(b"\x01\x00" * (n_bytes // 2)).decode()
    return json.dumps({
        "envelope": {"event_id": "e", "schema_version": 1, "producer": "speechmatics-tts",
                     "producer_timestamp": "2026-10-17T00:32:21+00:00",
                     "correlation_id": "c", "capture_context_id": "home"},
        "data": {"voice": "sarah", "sample_rate": 16000, "sample_count": n_bytes // 2,
                 "chunk_index": 0, "start_sample": 0, "is_final": False, "pcm": pcm},
    }).encode()


def epoch(y, mo, d, h, mi=0, s=0):
    return _dt.datetime(y, mo, d, h, mi, s, tzinfo=_dt.timezone.utc).timestamp()


def read_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


# ---------------------------------------------------------------------------
# MQTT wire helpers for the fake broker
# ---------------------------------------------------------------------------


def _enc_len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def publish_packet(topic, payload, qos=0):
    t = topic.encode()
    body = struct.pack("!H", len(t)) + t + (b"\x00\x01" if qos else b"") + payload
    return bytes([0x30 | (qos << 1)]) + _enc_len(len(body)) + body


CONNACK_OK = b"\x20\x02\x00\x00"
PINGRESP = b"\xd0\x00"


def suback(pid, codes):
    body = struct.pack("!H", pid) + bytes(codes)
    return b"\x90" + _enc_len(len(body)) + body


def split_packets(data):
    """Split a client byte stream into (first_byte, body) packets."""
    out, i = [], 0
    while i < len(data):
        h = data[i]
        i += 1
        mult, length = 1, 0
        while True:
            b = data[i]
            i += 1
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        out.append((h, data[i:i + length]))
        i += length
    return out


class FakeBroker:
    """A scripted MQTT broker on 127.0.0.1. Each accepted connection runs the next script.

    A script is a callable(conn, broker) that talks to one client connection. Every byte any
    client sends is appended to broker.received (all connections), for the never-PUBLISH scan.
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.received = bytearray()
        self.connections = 0
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        while self.scripts:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            script = self.scripts.pop(0)
            self.connections += 1
            try:
                script(_Recorder(conn, self), self)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        self.done.set()

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass


class _Recorder:
    """Wraps a broker-side connection and records what the client sent."""

    def __init__(self, conn, broker):
        self.conn = conn
        self.broker = broker
        self.buf = bytearray()
        conn.settimeout(5)

    def send(self, data):
        self.conn.sendall(data)

    def read_packet(self, timeout=5.0):
        self.conn.settimeout(timeout)
        while True:
            pk = self._try_parse()
            if pk is not None:
                return pk
            chunk = self.conn.recv(65536)
            if not chunk:
                return None
            with self.broker.lock:
                self.broker.received += chunk
            self.buf += chunk

    def _try_parse(self):
        if len(self.buf) < 2:
            return None
        i, mult, length = 1, 1, 0
        while True:
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

    def drain(self, seconds):
        """Read (and record) whatever the client sends for `seconds`."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                pk = self.read_packet(timeout=max(0.01, end - time.monotonic()))
            except socket.timeout:
                return
            if pk is None:
                return


def handshake(c):
    pk = c.read_packet()
    assert pk[0] == 0x10, pk
    c.send(CONNACK_OK)
    pk = c.read_packet()
    assert pk[0] == 0x82, pk
    pid = struct.unpack("!H", pk[1][:2])[0]
    c.send(suback(pid, [0, 0]))
    return pk


def serve_then_close(messages, linger=0.1):
    def script(c, broker):
        handshake(c)
        for topic, payload in messages:
            c.send(publish_packet(topic, payload))
        c.drain(linger)
    return script


def serve_until_stop(messages, answer_ping=True):
    """Handshake, send messages, then answer PINGREQs until the client goes away."""
    def script(c, broker):
        handshake(c)
        for topic, payload in messages:
            c.send(publish_packet(topic, payload))
        while True:
            try:
                pk = c.read_packet(timeout=10)
            except socket.timeout:
                return
            if pk is None or pk[0] == 0xE0:
                return
            if pk[0] == 0xC0 and answer_ping:
                c.send(PINGRESP)
    return script


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_cfg(d, port=1, **kw):
    env = {
        "ECHO_MQTT_HOST": "127.0.0.1",
        "ECHO_MQTT_PORT": str(port),
        "ECHO_TAP_DIR": d,
    }
    env.update({k: str(v) for k, v in kw.items()})
    return m.Config.from_env(env)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = m.Config.from_env({})
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 1883)
        self.assertIsNone(cfg.username)
        self.assertIsNone(cfg.password)
        self.assertEqual(cfg.directory, "/var/lib/pantheon/echo-evidence/tap")
        self.assertEqual(cfg.retention_days, 7)
        self.assertEqual(cfg.max_bytes, 2 * 1024 ** 3)
        self.assertEqual(cfg.max_payload, 64 * 1024)
        self.assertEqual(cfg.exclude, frozenset({"tts_raw"}))

    def test_empty_values_fall_back_to_defaults(self):
        cfg = m.Config.from_env({"ECHO_MQTT_USERNAME": "", "ECHO_MQTT_PASSWORD": "",
                                 "ECHO_TAP_RETENTION_DAYS": "", "ECHO_TAP_EXCLUDE": ""})
        self.assertIsNone(cfg.username)
        self.assertEqual(cfg.retention_days, 7)
        self.assertEqual(cfg.exclude, frozenset({"tts_raw"}))

    def test_exclude_list_parsed(self):
        cfg = m.Config.from_env({"ECHO_TAP_EXCLUDE": "tts_raw, stt_partial"})
        self.assertEqual(cfg.exclude, frozenset({"tts_raw", "stt_partial"}))

    def test_bad_numbers_rejected(self):
        for k, v in (("ECHO_TAP_RETENTION_DAYS", "0"), ("ECHO_TAP_MAX_BYTES", "-1"),
                     ("ECHO_TAP_MAX_PAYLOAD_BYTES", "x"), ("ECHO_MQTT_PORT", "70000")):
            with self.assertRaises(m.ConfigError, msg=k):
                m.Config.from_env({k: v})

    def test_password_never_in_repr(self):
        cfg = m.Config.from_env({"ECHO_MQTT_USERNAME": "u", "ECHO_MQTT_PASSWORD": "hunter2-secret"})
        self.assertNotIn("hunter2-secret", repr(cfg))


# ---------------------------------------------------------------------------
# Classification and record format
# ---------------------------------------------------------------------------


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = m.Config.from_env({})

    def test_tap_name(self):
        self.assertEqual(m.tap_name("echo/home/tap/tts_raw"), "tts_raw")
        self.assertEqual(m.tap_name("echo/home/tap/enrollment.offer_resolved"), "enrollment.offer_resolved")
        self.assertIsNone(m.tap_name("echo/stt/final"))

    def test_tts_raw_excluded_but_eos_kept(self):
        self.assertEqual(m.classify("echo/home/tap/tts_raw", 100, self.cfg), m.EXCLUDED)
        self.assertEqual(m.classify("echo/home/tap/tts_raw_eos", 100, self.cfg), m.KEEP)
        self.assertEqual(m.classify("echo/home/tap/tts_raw/sub", 100, self.cfg), m.EXCLUDED)

    def test_fixture_topics_all_kept(self):
        for topic, payload in load_fixture():
            self.assertEqual(m.classify(topic, len(payload), self.cfg), m.KEEP, topic)

    def test_excluded_wins_over_oversize(self):
        self.assertEqual(m.classify("echo/home/tap/tts_raw", 10 ** 6, self.cfg), m.EXCLUDED)

    def test_oversize(self):
        self.assertEqual(m.classify("echo/home/tap/stt", 64 * 1024, self.cfg), m.KEEP)
        self.assertEqual(m.classify("echo/home/tap/stt", 64 * 1024 + 1, self.cfg), m.OVERSIZE)

    def test_record_json_payload(self):
        topic, payload = load_fixture()[5]
        self.assertEqual(topic, "echo/stt/final")
        rec = m.make_record(topic, payload, epoch(2026, 10, 17, 0, 32, 18) + 0.060417)
        self.assertEqual(rec["topic"], "echo/stt/final")
        self.assertEqual(rec["ts"], "2026-10-17T00:32:18.060417Z")
        self.assertEqual(rec["payload"]["transcript"], "That's fine.")
        self.assertEqual(list(rec), ["ts", "topic", "payload"])

    def test_record_non_json(self):
        rec = m.make_record("echo/x/tap/y", b"not json \xff", 0.0)
        self.assertEqual(rec["payload"], {"_non_json": "not json �"})

    def test_record_empty_payload_is_non_json(self):
        rec = m.make_record("echo/x/tap/y", b"", 0.0)
        self.assertEqual(rec["payload"], {"_non_json": ""})

    def test_stub(self):
        rec = m.make_stub("echo/x/tap/y", 70000, 0.0)
        self.assertEqual(rec, {"ts": "1970-01-01T00:00:00.000000Z", "topic": "echo/x/tap/y",
                               "dropped": "oversize", "bytes": 70000})


# ---------------------------------------------------------------------------
# Output dir
# ---------------------------------------------------------------------------


class TestDir(unittest.TestCase):
    def test_creates_missing_dir_mode(self):
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "tap")
            m.prepare_tap_dir(d)
            st = os.lstat(d)
            self.assertTrue(stat.S_ISDIR(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode) & 0o777, 0o750)

    def test_existing_dir_ok(self):
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "tap")
            os.mkdir(d, 0o700)
            m.prepare_tap_dir(d)
            m.prepare_tap_dir(d)  # repeated fire is fine

    def test_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as parent:
            real = os.path.join(parent, "real")
            os.mkdir(real)
            link = os.path.join(parent, "tap")
            os.symlink(real, link)
            with self.assertRaises(m.TapDirError):
                m.prepare_tap_dir(link)

    def test_refuses_regular_file(self):
        with tempfile.TemporaryDirectory() as parent:
            p = os.path.join(parent, "tap")
            open(p, "w").close()
            with self.assertRaises(m.TapDirError):
                m.prepare_tap_dir(p)

    def test_refuses_foreign_owner(self):
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "tap")
            os.mkdir(d)
            with mock.patch.object(m.os, "getuid", return_value=os.getuid() + 4242):
                with self.assertRaises(m.TapDirError):
                    m.prepare_tap_dir(d)


# ---------------------------------------------------------------------------
# Writer: rotation and file properties
# ---------------------------------------------------------------------------


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "tap")
        m.prepare_tap_dir(self.d)
        self.cfg = make_cfg(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_name(self):
        self.assertEqual(m.file_name(epoch(2026, 10, 21, 4, 59, 59)), "tap-20261021T04.jsonl")

    def test_writes_all_fixture_lines_mode_0640(self):
        clock = Clock(epoch(2026, 10, 17, 0, 32))
        w = m.TapWriter(self.cfg, clock)
        for topic, payload in load_fixture():
            w.handle(topic, payload)
        path = os.path.join(self.d, "tap-20261017T00.jsonl")
        recs = read_records(path)
        self.assertEqual(len(recs), 14)
        self.assertEqual({r["topic"] for r in recs} >= {"echo/stt/final", "echo/home/tap/utterance"}, True)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertEqual(w.stats["written"], 14)
        w.close()

    def test_flushed_per_line_reader_sees_it(self):
        clock = Clock(epoch(2026, 10, 17, 0, 32))
        w = m.TapWriter(self.cfg, clock)
        w.handle("echo/home/tap/vad", b'{"a":1}')
        # A separate reader sees the line before close().
        self.assertEqual(len(read_records(os.path.join(self.d, "tap-20261017T00.jsonl"))), 1)
        w.close()

    def test_excluded_counted_not_written(self):
        clock = Clock(epoch(2026, 10, 17, 0, 32))
        w = m.TapWriter(self.cfg, clock)
        w.handle("echo/home/tap/tts_raw", tts_raw_payload())
        w.handle("echo/home/tap/tts_raw_eos", b'{"data":{"chunk_index":3}}')
        w.close()
        recs = read_records(os.path.join(self.d, "tap-20261017T00.jsonl"))
        self.assertEqual([r["topic"] for r in recs], ["echo/home/tap/tts_raw_eos"])
        self.assertEqual(w.stats["excluded"], 1)
        self.assertNotIn("pcm", open(os.path.join(self.d, "tap-20261017T00.jsonl")).read())

    def test_excluded_only_creates_no_file(self):
        clock = Clock(epoch(2026, 10, 17, 0, 32))
        w = m.TapWriter(self.cfg, clock)
        w.handle("echo/home/tap/tts_raw", tts_raw_payload(10))
        w.close()
        self.assertEqual(os.listdir(self.d), [])

    def test_oversize_stub(self):
        cfg = make_cfg(self.d, ECHO_TAP_MAX_PAYLOAD_BYTES=100)
        clock = Clock(epoch(2026, 10, 17, 0, 32))
        w = m.TapWriter(cfg, clock)
        big = b'{"x":"' + b"a" * 200 + b'"}'
        w.handle("echo/home/tap/utterance", big)
        w.close()
        (rec,) = read_records(os.path.join(self.d, "tap-20261017T00.jsonl"))
        self.assertEqual(rec["dropped"], "oversize")
        self.assertEqual(rec["bytes"], len(big))
        self.assertNotIn("payload", rec)
        self.assertEqual(w.stats["oversize"], 1)

    def test_hourly_rotation(self):
        clock = Clock(epoch(2026, 10, 17, 0, 59, 59))
        w = m.TapWriter(self.cfg, clock)
        w.handle("echo/home/tap/vad", b'{"n":1}')
        clock.t = epoch(2026, 10, 17, 1, 0, 0)
        w.handle("echo/home/tap/vad", b'{"n":2}')
        w.close()
        self.assertEqual(sorted(os.listdir(self.d)), ["tap-20261017T00.jsonl", "tap-20261017T01.jsonl"])
        self.assertEqual(read_records(os.path.join(self.d, "tap-20261017T01.jsonl"))[0]["payload"], {"n": 2})
        self.assertEqual(w.stats["rotations"], 1)

    def test_restart_within_hour_appends(self):
        clock = Clock(epoch(2026, 10, 17, 0, 10))
        w = m.TapWriter(self.cfg, clock)
        w.handle("echo/home/tap/vad", b'{"n":1}')
        w.close()
        w2 = m.TapWriter(self.cfg, clock)
        w2.handle("echo/home/tap/vad", b'{"n":2}')
        w2.close()
        self.assertEqual(len(read_records(os.path.join(self.d, "tap-20261017T00.jsonl"))), 2)

    def test_refuses_symlinked_current_file(self):
        target = os.path.join(self.tmp.name, "elsewhere")
        open(target, "w").close()
        os.symlink(target, os.path.join(self.d, "tap-20261017T00.jsonl"))
        w = m.TapWriter(self.cfg, Clock(epoch(2026, 10, 17, 0, 10)))
        with redirect_stderr(io.StringIO()):
            w.handle("echo/home/tap/vad", b"{}")  # refused (O_NOFOLLOW): dropped and counted
        self.assertEqual(w.stats["write_errors"], 1)
        self.assertEqual(w.stats["written"], 0)
        self.assertEqual(open(target).read(), "")

    def test_fsync_cadence(self):
        clock = Clock(epoch(2026, 10, 17, 0, 10))
        cfg = make_cfg(self.d, ECHO_TAP_FSYNC_S=60)
        w = m.TapWriter(cfg, clock)
        with mock.patch.object(m.os, "fsync", wraps=os.fsync) as fs:
            w.handle("echo/home/tap/vad", b"{}")
            n0 = fs.call_count
            clock.t += 10
            w.handle("echo/home/tap/vad", b"{}")
            self.assertEqual(fs.call_count, n0)
            clock.t += 60
            w.handle("echo/home/tap/vad", b"{}")
            self.assertGreater(fs.call_count, n0)
            w.close()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name, size=10):
        with open(os.path.join(self.d, name), "wb") as f:
            f.write(b"x" * size)

    def test_empty_dir(self):
        self.assertEqual(m.apply_retention(self.d, epoch(2026, 10, 21, 4), 7, 1000), [])

    def test_age(self):
        now = epoch(2026, 10, 21, 4, 30)
        self.touch("tap-20261014T03.jsonl")  # ended 04:00 on the 14th: 7 days + 30 min old
        self.touch("tap-20261014T04.jsonl")  # ends 05:00 on the 14th: within 7 days
        self.touch("tap-20261021T04.jsonl")
        removed = m.apply_retention(self.d, now, 7, 10 ** 9)
        self.assertEqual(removed, ["tap-20261014T03.jsonl"])
        self.assertEqual(sorted(os.listdir(self.d)), ["tap-20261014T04.jsonl", "tap-20261021T04.jsonl"])

    def test_total_bytes_oldest_first_never_current(self):
        now = epoch(2026, 10, 21, 4, 30)
        for h in ("01", "02", "03"):
            self.touch(f"tap-20261021T{h}.jsonl", 100)
        self.touch("tap-20261021T04.jsonl", 500)  # current, alone over the cap
        removed = m.apply_retention(self.d, now, 7, 250)
        self.assertEqual(removed, ["tap-20261021T01.jsonl", "tap-20261021T02.jsonl", "tap-20261021T03.jsonl"])
        self.assertEqual(os.listdir(self.d), ["tap-20261021T04.jsonl"])

    def test_total_bytes_stops_when_under(self):
        now = epoch(2026, 10, 21, 4, 30)
        for h in ("01", "02", "03", "04"):
            self.touch(f"tap-20261021T{h}.jsonl", 100)
        removed = m.apply_retention(self.d, now, 7, 250)
        self.assertEqual(removed, ["tap-20261021T01.jsonl", "tap-20261021T02.jsonl"])

    def test_never_touches_non_matching_names(self):
        now = epoch(2026, 10, 21, 4, 30)
        keep = ["tap-20201014T03.jsonl.bak", "notes.md", "tap-2020101T03.jsonl", "xtap-20201014T03.jsonl",
                "tap-20201014T03.json", ".tap-20201014T03.jsonl", "tap-20201014T3.jsonl"]
        for n in keep:
            self.touch(n, 10 ** 6)
        os.mkdir(os.path.join(self.d, "tap-20201014T05.jsonl"))  # a dir with a matching name
        removed = m.apply_retention(self.d, now, 7, 1)
        self.assertEqual(removed, [])
        self.assertEqual(len(os.listdir(self.d)), len(keep) + 1)

    def test_never_follows_symlink(self):
        now = epoch(2026, 10, 21, 4, 30)
        outside = tempfile.TemporaryDirectory()
        try:
            victim = os.path.join(outside.name, "victim")
            open(victim, "w").close()
            os.symlink(victim, os.path.join(self.d, "tap-20201014T03.jsonl"))
            m.apply_retention(self.d, now, 7, 1)
            self.assertTrue(os.path.exists(victim))
        finally:
            outside.cleanup()

    def test_writer_runs_retention_at_startup_and_rotation(self):
        d = os.path.join(self.d, "tap")
        m.prepare_tap_dir(d)
        old = os.path.join(d, "tap-20261001T00.jsonl")
        open(old, "w").close()
        cfg = make_cfg(d)
        clock = Clock(epoch(2026, 10, 21, 4, 59))
        w = m.TapWriter(cfg, clock)
        w.start()
        self.assertFalse(os.path.exists(old))
        old2 = os.path.join(d, "tap-20261002T00.jsonl")
        open(old2, "w").close()
        w.handle("echo/home/tap/vad", b"{}")
        self.assertTrue(os.path.exists(old2))  # no rotation yet
        clock.t = epoch(2026, 10, 21, 5, 0)
        w.handle("echo/home/tap/vad", b"{}")
        self.assertFalse(os.path.exists(old2))
        w.close()


# ---------------------------------------------------------------------------
# MQTT client: wire, read-only, reconnect, keepalive
# ---------------------------------------------------------------------------


ALLOWED_TYPES = {1, 8, 12, 14}  # CONNECT, SUBSCRIBE, PINGREQ, DISCONNECT


class TestWire(unittest.TestCase):
    def test_no_publish_builder(self):
        self.assertFalse(hasattr(m, "build_publish"))
        names = [n for n in dir(m) if "publish" in n.lower() and callable(getattr(m, n))]
        self.assertEqual(names, [])

    def test_connect_clean_session_and_creds(self):
        pk = m.build_connect("cid", "user", "pw", 30)
        (h, body), = split_packets(pk)
        self.assertEqual(h, 0x10)
        self.assertEqual(body[:6], b"\x00\x04MQTT")
        self.assertEqual(body[6], 4)
        self.assertEqual(body[7], 0x80 | 0x40 | 0x02)
        self.assertEqual(struct.unpack("!H", body[8:10])[0], 30)

    def test_subscribe_qos0(self):
        pk = m.build_subscribe(1, m.TOPICS)
        (h, body), = split_packets(pk)
        self.assertEqual(h, 0x82)
        i, topics = 2, []
        while i < len(body):
            n = struct.unpack("!H", body[i:i + 2])[0]
            topics.append((body[i + 2:i + 2 + n].decode(), body[i + 2 + n]))
            i += 3 + n
        self.assertEqual(topics, [("echo/+/tap/#", 0), ("echo/stt/final", 0)])

    def test_client_id_unique_shape(self):
        cid = m.client_id()
        self.assertTrue(cid.startswith("echo-tap-recorder-"))
        self.assertIn(str(os.getpid()), cid)


class RecorderHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "tap")
        m.prepare_tap_dir(self.d)
        self.brokers = []

    def tearDown(self):
        for b in self.brokers:
            b.close()
        self.tmp.cleanup()

    def broker(self, scripts):
        b = FakeBroker(scripts)
        self.brokers.append(b)
        return b

    def run_recorder(self, broker, stop_when, timeout=10.0, keepalive=30.0, **kw):
        cfg = make_cfg(self.d, port=broker.port, **kw)
        stop = threading.Event()
        err = io.StringIO()
        rec = m.Recorder(cfg, stop=stop, keepalive=keepalive, backoff_base=0.05, backoff_cap=0.2)
        t = threading.Thread(target=lambda: rec.run(), daemon=True)
        with redirect_stderr(err):
            t.start()
            end = time.monotonic() + timeout
            while time.monotonic() < end and not stop_when(rec):
                time.sleep(0.02)
            stop.set()
            t.join(5)
        self.assertFalse(t.is_alive(), "recorder did not stop")
        return rec, err.getvalue()

    def all_records(self):
        out = []
        for n in sorted(os.listdir(self.d)):
            out += read_records(os.path.join(self.d, n))
        return out

    def assert_read_only(self, broker):
        with broker.lock:
            data = bytes(broker.received)
        self.assertTrue(data)
        types = {h >> 4 for h, _ in split_packets(data)}
        self.assertLessEqual(types, ALLOWED_TYPES, f"client sent packet types {types}")
        self.assertNotIn(3, types)


class TestRecorderLive(RecorderHarness):
    def test_records_tap_and_stt_final_and_never_publishes(self):
        msgs = load_fixture() + [("echo/home/tap/tts_raw", tts_raw_payload()),
                                 ("echo/home/tap/odd", b"\x00not-json")]
        b = self.broker([serve_until_stop(msgs)])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 15)
        recs = self.all_records()
        topics = [r["topic"] for r in recs]
        self.assertIn("echo/stt/final", topics)
        self.assertIn("echo/home/tap/utterance", topics)
        self.assertNotIn("echo/home/tap/tts_raw", topics)
        self.assertEqual(recs[-1]["payload"], {"_non_json": "\x00not-json"})
        self.assertEqual(rec.writer.stats["excluded"], 1)
        self.assert_read_only(b)
        # Clean stop sends DISCONNECT.
        b.done.wait(5)
        with b.lock:
            self.assertEqual(split_packets(bytes(b.received))[-1][0], 0xE0)

    def test_reconnects_after_broker_drop(self):
        fx = load_fixture()
        b = self.broker([serve_then_close(fx[:2]), serve_until_stop(fx[2:4])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 4)
        self.assertEqual(len(self.all_records()), 4)
        self.assertEqual(rec.stats["reconnects"], 1)
        self.assertIn("reconnect", err)
        self.assert_read_only(b)

    def test_reconnects_after_missed_pingresp(self):
        fx = load_fixture()
        b = self.broker([serve_until_stop(fx[:1], answer_ping=False), serve_until_stop(fx[1:2])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 2, keepalive=0.3)
        self.assertEqual(rec.stats["reconnects"], 1)
        self.assertIn("PINGRESP", err)
        self.assert_read_only(b)
        with b.lock:
            types = [h >> 4 for h, _ in split_packets(bytes(b.received))]
        self.assertIn(12, types)  # PINGREQ was sent

    def test_answered_pings_keep_session(self):
        b = self.broker([serve_until_stop([])])
        rec, err = self.run_recorder(b, lambda r: r.stats["pings"] >= 3, keepalive=0.2)
        self.assertEqual(rec.stats["reconnects"], 0)
        self.assert_read_only(b)

    def test_connack_refused_backs_off_and_retries(self):
        def refuse(c, broker):
            c.read_packet()
            c.send(b"\x20\x02\x00\x05")
        fx = load_fixture()
        b = self.broker([refuse, refuse, serve_until_stop(fx[:1])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 1)
        self.assertIn("rc=5", err)
        self.assertEqual(b.connections, 3)

    def test_suback_failure_reconnects(self):
        def bad_sub(c, broker):
            c.read_packet()
            c.send(CONNACK_OK)
            pk = c.read_packet()
            c.send(suback(struct.unpack("!H", pk[1][:2])[0], [0x80, 0]))
            c.drain(0.2)
        b = self.broker([bad_sub, serve_until_stop(load_fixture()[:1])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 1)
        self.assertIn("SUBACK", err)

    def test_unreachable_broker_backs_off(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens here

        class Dummy:
            pass
        d = Dummy()
        d.port = port
        rec, err = self.run_recorder(d, lambda r: r.stats["connect_failures"] >= 3, timeout=5)
        self.assertGreaterEqual(rec.stats["connect_failures"], 3)

    def test_password_not_in_stderr(self):
        b = self.broker([serve_until_stop(load_fixture()[:1])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 1,
                                     ECHO_MQTT_USERNAME="u", ECHO_MQTT_PASSWORD="pw-very-secret")
        self.assertNotIn("pw-very-secret", err)

    def test_stats_line(self):
        b = self.broker([serve_until_stop(load_fixture()[:2])])
        rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 2,
                                     ECHO_TAP_STATS_S=0.05)
        self.assertRegex(err, r"stats written=\d+ excluded=\d+ oversize=\d+ write_errors=\d+ "
                              r"reconnects=\d+ file=")


# ---------------------------------------------------------------------------
# r1 review fixes: write errors, retained flag, malformed PUBLISH, parser, stop
# ---------------------------------------------------------------------------


def _flaky(real, plan):
    """os.write/os.open stand-in: plan maps call number -> 'raise:<errno>' or 'partial'."""
    calls = {"n": 0}

    def f(*a, **kw):
        calls["n"] += 1
        step = plan.get(calls["n"])
        if step == "partial":
            fd, data = a
            return real(fd, bytes(data[:10]))
        if step:
            raise OSError(step, os.strerror(step))
        return real(*a, **kw)
    return f


class TestWriteErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "tap")
        m.prepare_tap_dir(self.d)
        self.cfg = make_cfg(self.d)
        self.path = os.path.join(self.d, "tap-20261017T00.jsonl")
        self.clock = Clock(epoch(2026, 10, 17, 0, 10))

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]  # every line must parse

    def test_enospc_once_dropped_counted_then_recovers(self):
        w = m.TapWriter(self.cfg, self.clock)
        w.handle("echo/home/tap/vad", b'{"n":1}')
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(m.os, "write",
                                                     side_effect=_flaky(os.write, {1: errno.ENOSPC})):
            w.handle("echo/home/tap/vad", b'{"n":2}')  # must not raise
            w.handle("echo/home/tap/vad", b'{"n":3}')
        w.close()
        self.assertEqual(w.stats["write_errors"], 1)
        self.assertEqual(w.stats["written"], 2)
        self.assertEqual([r["payload"]["n"] for r in self.lines()], [1, 3])
        self.assertIn("write error", err.getvalue())

    def test_partial_write_leaves_no_torn_line(self):
        w = m.TapWriter(self.cfg, self.clock)
        w.handle("echo/home/tap/vad", b'{"n":1}')
        with redirect_stderr(io.StringIO()), mock.patch.object(
                m.os, "write", side_effect=_flaky(os.write, {1: "partial", 2: errno.EIO})):
            w.handle("echo/home/tap/vad", b'{"n":2}')
            w.handle("echo/home/tap/vad", b'{"n":3}')
        w.close()
        self.assertEqual(w.stats["write_errors"], 1)
        self.assertEqual([r["payload"]["n"] for r in self.lines()], [1, 3])

    def test_partial_write_newline_fallback_when_truncate_fails(self):
        w = m.TapWriter(self.cfg, self.clock)
        w.handle("echo/home/tap/vad", b'{"n":1}')
        with redirect_stderr(io.StringIO()), \
                mock.patch.object(m.os, "write", side_effect=_flaky(os.write, {1: "partial", 2: errno.EIO})), \
                mock.patch.object(m.os, "ftruncate", side_effect=OSError(errno.EIO, "x")):
            w.handle("echo/home/tap/vad", b'{"n":2}')
            w.handle("echo/home/tap/vad", b'{"n":3}')
        w.close()
        with open(self.path, encoding="utf-8") as f:
            raw = f.read().splitlines()
        # The torn fragment sits alone on its own line; the next record starts clean.
        self.assertEqual(len(raw), 3)
        self.assertEqual(json.loads(raw[0])["payload"], {"n": 1})
        self.assertEqual(json.loads(raw[2])["payload"], {"n": 3})

    def test_open_eacces_dropped_counted_then_reopens(self):
        w = m.TapWriter(self.cfg, self.clock)
        with redirect_stderr(io.StringIO()), mock.patch.object(
                m.os, "open", side_effect=_flaky(os.open, {1: errno.EACCES})):
            w.handle("echo/home/tap/vad", b'{"n":1}')
            w.handle("echo/home/tap/vad", b'{"n":2}')
        w.close()
        self.assertEqual(w.stats["write_errors"], 1)
        self.assertEqual([r["payload"]["n"] for r in self.lines()], [2])

    def test_error_log_rate_limited(self):
        w = m.TapWriter(self.cfg, self.clock)
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(
                m.os, "write", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            for _ in range(50):
                w.handle("echo/home/tap/vad", b"{}")
        w.close()
        self.assertEqual(w.stats["write_errors"], 50)
        self.assertEqual(err.getvalue().count("write error"), 1)


class _CaptureWriter:
    def __init__(self):
        self.got = []

    def handle(self, topic, payload, retained=False):
        self.got.append((topic, payload, retained))


def _bare_recorder():
    r = m.Recorder.__new__(m.Recorder)
    r.writer = _CaptureWriter()
    return r


class TestPublishParsing(unittest.TestCase):
    def test_retained_flag_recorded(self):
        r = _bare_recorder()
        (h, body), = split_packets(publish_packet("echo/home/tap/vad", b'{"a":1}'))
        r._on_publish(h | 0x01, body)
        r._on_publish(h, body)
        self.assertEqual([g[2] for g in r.writer.got], [True, False])
        self.assertIs(m.make_record("t", b"{}", 0.0, retained=True)["retained"], True)
        self.assertNotIn("retained", m.make_record("t", b"{}", 0.0))

    def test_retained_written_to_file(self):
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "tap")
            m.prepare_tap_dir(d)
            w = m.TapWriter(make_cfg(d), Clock(epoch(2026, 10, 17, 0, 10)))
            w.handle("echo/home/tap/vad", b"{}", retained=True)
            w.handle("echo/home/tap/vad", b"{}")
            w.close()
            recs = read_records(os.path.join(d, "tap-20261017T00.jsonl"))
            self.assertEqual([r.get("retained") for r in recs], [True, None])

    def test_topic_length_past_body_rejected(self):
        r = _bare_recorder()
        with self.assertRaises(m.MqttError):
            r._on_publish(0x30, struct.pack("!H", 50) + b"echo/x")
        # QoS 1: the topic fits but the packet id does not.
        with self.assertRaises(m.MqttError):
            r._on_publish(0x32, struct.pack("!H", 6) + b"echo/x" + b"\x00")
        self.assertEqual(r.writer.got, [])

    def test_qos1_packet_id_skipped(self):
        r = _bare_recorder()
        (h, body), = split_packets(publish_packet("echo/stt/final", b'{"x":1}', qos=1))
        r._on_publish(h, body)
        self.assertEqual(r.writer.got, [("echo/stt/final", b'{"x":1}', False)])


class TestStreamParser(unittest.TestCase):
    def test_byte_at_a_time(self):
        big = b'{"a":1}' * 30000  # 210 KB: a 3-byte remaining length
        data = publish_packet("echo/a/tap/x", big, qos=1) + publish_packet("echo/stt/final", b"{}") + PINGRESP
        st = m._Stream(None)
        got = []
        for b in data:
            st.buf.append(b)
            while (pk := st.parse()) is not None:
                got.append(pk)
        self.assertEqual([h for h, _ in got], [0x32, 0x30, 0xD0])
        self.assertEqual(st.buf, bytearray())
        r = _bare_recorder()
        for h, body in got[:2]:
            r._on_publish(h, body)
        self.assertEqual(r.writer.got[0][:2], ("echo/a/tap/x", big))
        self.assertEqual(r.writer.got[1][:2], ("echo/stt/final", b"{}"))

    def test_malformed_five_byte_length(self):
        st = m._Stream(None)
        st.buf = bytearray(b"\x30\xff\xff\xff\xff\x01")
        with self.assertRaises(m.MqttError):
            st.parse()

    def test_incomplete_length_waits(self):
        st = m._Stream(None)
        st.buf = bytearray(b"\x30\xff\xff")
        self.assertIsNone(st.parse())


class TestRecorderWriteErrorLive(RecorderHarness):
    def test_write_error_does_not_reconnect(self):
        b = self.broker([serve_until_stop(load_fixture()[:3])])
        with mock.patch.object(m.os, "write", side_effect=_flaky(os.write, {1: errno.ENOSPC})):
            rec, err = self.run_recorder(b, lambda r: r.writer.stats["written"] >= 2,
                                         ECHO_TAP_STATS_S=0.05)
        self.assertEqual(rec.stats["reconnects"], 0)
        self.assertEqual(rec.writer.stats["write_errors"], 1)
        self.assertEqual(len(self.all_records()), 2)
        self.assertRegex(err, r"write_errors=1")
        self.assert_read_only(b)


class TestStopDuringHandshake(RecorderHarness):
    def test_stop_while_waiting_for_connack(self):
        def silent(c, broker):
            c.read_packet()  # CONNECT
            c.drain(8)  # never answer
        b = self.broker([silent])
        stop = threading.Event()
        rec = m.Recorder(make_cfg(self.d, port=b.port), stop=stop)
        t = threading.Thread(target=rec.run, daemon=True)
        with redirect_stderr(io.StringIO()):
            t.start()
            time.sleep(0.3)
            t0 = time.monotonic()
            stop.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 2.0)


class TestShippedFiles(unittest.TestCase):
    def test_env_example_parses_to_defaults(self):
        env = {}
        with open(os.path.join(HERE, "echo-tap-recorder.env.example"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k] = v
        cfg = m.Config.from_env(env)
        dflt = m.Config.from_env({})
        for k in ("host", "port", "directory", "retention_days", "max_bytes", "max_payload",
                  "exclude", "fsync_s", "stats_s"):
            self.assertEqual(getattr(cfg, k), getattr(dflt, k), k)

    def test_unit_file(self):
        with open(os.path.join(HERE, "echo-tap-recorder.service"), encoding="utf-8") as f:
            unit = f.read()
        for line in ("Type=simple", "EnvironmentFile=%h/.config/echo-tap-recorder.env",
                     "ExecStart=/usr/bin/python3 %h/.local/lib/echo-tap-recorder/echo_tap_recorder.py",
                     "Restart=always", "RestartSec=10", "RestartPreventExitStatus=1 2",
                     "TimeoutStopSec=35", "Nice=10", "NoNewPrivileges=yes",
                     "ReadWritePaths=/var/lib/pantheon/echo-evidence", "WantedBy=default.target"):
            self.assertIn("\n" + line + "\n", unit, line)


class TestMain(unittest.TestCase):
    def test_bad_config_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = m.main([], env={"ECHO_TAP_RETENTION_DAYS": "nope"})
        self.assertEqual(rc, 2)

    def test_bad_dir_exits_1(self):
        with tempfile.TemporaryDirectory() as parent:
            link = os.path.join(parent, "tap")
            os.symlink(parent, link)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = m.main([], env={"ECHO_TAP_DIR": link})
            self.assertEqual(rc, 1)
            self.assertIn("refusing", err.getvalue())


if __name__ == "__main__":
    unittest.main()
