#!/usr/bin/env python3
"""Tests for tools/health_signal_writer.py (T-3652 / RFC-003).

Hermetic, plain `unittest`. This slice covers the pure request-validation core
(RFC R2/R3/R4/R5): framing, schema/duplicate-key/size validation, the reserved +
identifier + refusal logic, allowlist model validation, server-derived leaf, and the
REFUSE_NAMESPACE seam. Socket/SO_PEERCRED/canary/installer legs are later slices.
"""
import json
import os
import struct
import sys
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import health_signal_writer as hsw  # noqa: E402


def _body(**kw) -> bytes:
    return json.dumps(kw, separators=(",", ":")).encode("utf-8")


def _valid_allowlist() -> hsw.Allowlist:
    return hsw.Allowlist.from_mapping(
        {
            "version": 1,
            "writers": [
                {"uid": 1001, "account": "claude-fallback", "persona": "recovery",
                 "signal_kinds": ["signal"], "store_path": "/home/post-office/mailbox/recovery/tmp"},
                {"uid": 1000, "account": "claude", "persona": "orchestrator",
                 "signal_kinds": ["signal", "deploy-health"],
                 "store_path": "/home/post-office/mailbox/orchestrator/tmp"},
            ],
        }
    )


class FramingTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        body = b'{"v":1}'
        framed = hsw.frame(body)
        self.assertEqual(hsw.parse_length_prefix(framed[: hsw.LEN_PREFIX_SIZE]), len(body))
        self.assertEqual(framed[hsw.LEN_PREFIX_SIZE:], body)

    def test_len_prefix_is_unsigned_big_endian(self):
        # L1: a high-bit length decodes to its true unsigned value, never negative.
        prefix = struct.pack(">I", 0xFFFFFFFF)
        self.assertEqual(hsw.parse_length_prefix(prefix), 0xFFFFFFFF)

    def test_frame_rejects_oversized_body(self):
        with self.assertRaises(ValueError):
            hsw.frame(b"x" * (hsw.MAX_FRAME + 1))

    def test_wrong_size_prefix_is_none(self):
        self.assertIsNone(hsw.parse_length_prefix(b"\x00\x00"))


class ParseValidateBodyTests(unittest.TestCase):
    def test_valid(self):
        req, refusal = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload={"x": 1}))
        self.assertIsNone(refusal)
        self.assertEqual(req.signal_kind, "signal")

    def test_extra_top_level_key_malformed(self):
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload={}, extra=1))
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_missing_key_malformed(self):
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal"))
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_wrong_version_malformed(self):
        _, r = hsw.parse_and_validate_body(_body(v=2, signal_kind="signal", payload={}))
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_non_object_malformed(self):
        _, r = hsw.parse_and_validate_body(b"[]")
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_bad_utf8_malformed(self):
        _, r = hsw.parse_and_validate_body(b"\xff\xfe")
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_duplicate_top_level_key_malformed(self):
        # Isolate the dup-key defense: BOTH values are v=1 (schema-valid), so nothing
        # but duplicate-detection can reject it. (A {"v":1,"v":2} body would be caught
        # by the v!=1 check even with a broken hook — a weak test that passes for the
        # wrong reason.) hand-craft raw since a Python dict literal dedupes.
        raw = b'{"v":1,"v":1,"signal_kind":"signal","payload":{}}'
        _, r = hsw.parse_and_validate_body(raw)
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_duplicate_nested_key_malformed(self):
        raw = b'{"v":1,"signal_kind":"signal","payload":{"a":1,"a":2}}'
        _, r = hsw.parse_and_validate_body(raw)
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_nonfinite_float_rejected(self):
        # NaN/Infinity/-Infinity are Python-json extensions, not valid JSON — reject
        # so they never round-trip into a stored record as an unparseable constant.
        for tok in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(tok=tok):
                raw = b'{"v":1,"signal_kind":"signal","payload":{"x":%s}}' % tok.encode()
                _, r = hsw.parse_and_validate_body(raw)
                self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_nested_payload_malformed(self):
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload={"a": {"b": 1}}))
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_payload_array_value_malformed(self):
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload={"a": [1, 2]}))
        self.assertEqual(r, hsw.REFUSE_MALFORMED)

    def test_oversized_payload_str(self):
        big = "z" * (hsw.MAX_PAYLOAD_STR_BYTES + 1)
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload={"a": big}))
        self.assertEqual(r, hsw.REFUSE_OVERSIZED)

    def test_too_many_payload_keys(self):
        payload = {f"k{i}": i for i in range(hsw.MAX_PAYLOAD_KEYS + 1)}
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="signal", payload=payload))
        self.assertEqual(r, hsw.REFUSE_OVERSIZED)

    def test_oversized_frame(self):
        _, r = hsw.parse_and_validate_body(b"x" * (hsw.MAX_FRAME + 1))
        self.assertEqual(r, hsw.REFUSE_OVERSIZED)

    def test_bad_identifier_slash(self):
        # H3: IDENTIFIER_RE permits 'a/b', so the explicit guard must reject it.
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="a/b", payload={}))
        self.assertEqual(r, hsw.REFUSE_BAD_IDENTIFIER)

    def test_bad_identifier_traversal(self):
        _, r = hsw.parse_and_validate_body(_body(v=1, signal_kind="../x", payload={}))
        self.assertEqual(r, hsw.REFUSE_BAD_IDENTIFIER)

    def test_bad_identifier_overlong(self):
        _, r = hsw.parse_and_validate_body(
            _body(v=1, signal_kind="x" * (hsw.MAX_IDENTIFIER_LENGTH + 1), payload={})
        )
        self.assertEqual(r, hsw.REFUSE_BAD_IDENTIFIER)


class AllowlistTests(unittest.TestCase):
    def test_valid_loads(self):
        al = _valid_allowlist()
        self.assertEqual(len(al), 2)
        self.assertEqual(al.account_for_uid(1001).account, "claude-fallback")
        self.assertIsNone(al.account_for_uid(9999))

    def test_duplicate_uid_rejected(self):
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 1, "writers": [
                {"uid": 1001, "account": "a", "persona": "a", "signal_kinds": ["signal"], "store_path": "/x"},
                {"uid": 1001, "account": "b", "persona": "b", "signal_kinds": ["signal"], "store_path": "/y"},
            ]})

    def test_non_int_uid_rejected(self):
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 1, "writers": [
                {"uid": "1001", "account": "a", "persona": "a", "signal_kinds": ["signal"], "store_path": "/x"},
            ]})

    def test_bool_uid_rejected(self):
        # bool is an int subclass — `true` must not be accepted as uid 1.
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 1, "writers": [
                {"uid": True, "account": "a", "persona": "a", "signal_kinds": ["signal"], "store_path": "/x"},
            ]})

    def test_reserved_kind_in_signal_kinds_rejected(self):
        # H1: config listing a reserved kind fails closed at load.
        for reserved in ("canary", "heartbeat", "Canary", "CANARY"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(hsw.SignalWriterConfigError):
                    hsw.Allowlist.from_mapping({"version": 1, "writers": [
                        {"uid": 1001, "account": "a", "persona": "a",
                         "signal_kinds": ["signal", reserved], "store_path": "/x"},
                    ]})

    def test_slash_in_account_rejected(self):
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 1, "writers": [
                {"uid": 1001, "account": "a/b", "persona": "a", "signal_kinds": ["signal"], "store_path": "/x"},
            ]})

    def test_missing_store_path_rejected(self):
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 1, "writers": [
                {"uid": 1001, "account": "a", "persona": "a", "signal_kinds": ["signal"]},
            ]})

    def test_bad_version_rejected(self):
        with self.assertRaises(hsw.SignalWriterConfigError):
            hsw.Allowlist.from_mapping({"version": 2, "writers": []})


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.al = _valid_allowlist()
        self.root = Path("/tmp/hsw-test-tree")  # never written; leaf is a path only

    def test_valid_yields_server_derived_leaf(self):
        leaf, refusal = hsw.authorize(
            uid=1001, body=_body(v=1, signal_kind="signal", payload={"ok": True}),
            allowlist=self.al, tree_root=self.root,
        )
        self.assertIsNone(refusal)
        self.assertEqual(leaf, self.root / "claude-fallback" / "signal")

    def test_unlisted_uid(self):
        _, r = hsw.authorize(uid=4242, body=_body(v=1, signal_kind="signal", payload={}),
                             allowlist=self.al, tree_root=self.root)
        self.assertEqual(r, hsw.REFUSE_UNLISTED)

    def test_unlisted_signal_kind(self):
        # listed uid, kind not in that account's set
        _, r = hsw.authorize(uid=1001, body=_body(v=1, signal_kind="deploy-health", payload={}),
                             allowlist=self.al, tree_root=self.root)
        self.assertEqual(r, hsw.REFUSE_UNLISTED)

    def test_reserved_kind_refused(self):
        for kind in ("canary", "heartbeat", "Canary", "CANARY", "HeartBeat"):
            with self.subTest(kind=kind):
                _, r = hsw.authorize(uid=1001, body=_body(v=1, signal_kind=kind, payload={}),
                                     allowlist=self.al, tree_root=self.root)
                self.assertEqual(r, hsw.REFUSE_RESERVED)

    def test_reserved_takes_precedence_over_unlisted(self):
        # 'canary' is not in the account's signal_kinds either, but RESERVED wins (H1).
        _, r = hsw.authorize(uid=1001, body=_body(v=1, signal_kind="canary", payload={}),
                             allowlist=self.al, tree_root=self.root)
        self.assertEqual(r, hsw.REFUSE_RESERVED)

    def test_bad_identifier_slash_refused(self):
        _, r = hsw.authorize(uid=1001, body=_body(v=1, signal_kind="a/b", payload={}),
                             allowlist=self.al, tree_root=self.root)
        self.assertEqual(r, hsw.REFUSE_BAD_IDENTIFIER)

    def test_body_cannot_override_uid_authority(self):
        # D-1: a body whose payload claims another account/uid is ignored — the leaf
        # is derived from the kernel-attested uid arg (1001 -> claude-fallback), NOT
        # the payload's "account":"claude"/"uid":1000.
        body = b'{"v":1,"signal_kind":"signal","payload":{"account":"claude","uid":1000}}'
        leaf, r = hsw.authorize(uid=1001, body=body, allowlist=self.al, tree_root=self.root)
        self.assertIsNone(r)
        self.assertEqual(leaf, self.root / "claude-fallback" / "signal")
        self.assertNotIn("claude/", str(leaf.relative_to(self.root)))

    def test_namespace_guard_via_seam(self):
        # M3: feed the leaf-builder an escaping leaf past the wire validators.
        def escaping_builder(tree_root, account, signal_kind):
            return tree_root / account / ".." / ".." / "etc" / "x"
        _, r = hsw.authorize(
            uid=1001, body=_body(v=1, signal_kind="signal", payload={}),
            allowlist=self.al, tree_root=self.root, leaf_builder=escaping_builder,
        )
        self.assertEqual(r, hsw.REFUSE_NAMESPACE)

    def test_namespace_guard_allows_in_namespace_seam(self):
        # A seam that stays in-namespace must NOT trip REFUSE_NAMESPACE.
        def nested_builder(tree_root, account, signal_kind):
            return tree_root / account / "sub" / signal_kind
        leaf, r = hsw.authorize(
            uid=1001, body=_body(v=1, signal_kind="signal", payload={}),
            allowlist=self.al, tree_root=self.root, leaf_builder=nested_builder,
        )
        self.assertIsNone(r)
        self.assertIsNotNone(leaf)


class ProbeWorkerTests(unittest.TestCase):
    def test_probe_ok_on_writable_dir_and_no_leftover(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = Path(d)
            outcome = hsw.probe_store_once(store, nonce="n1")
            self.assertEqual(outcome, "ok")
            # transient: the probe file is unlinked, nothing persists.
            self.assertEqual(list(store.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory write perms")
    def test_probe_fail_on_nonwritable_dir(self):
        # A real O_EXCL attempt against a non-writable dir genuinely returns EACCES.
        # (This is the PROBE's real syscall — unlike the MED-1 VERIFY, there is no
        # mode-bit-proxy concern here; the attempt either succeeds or it doesn't.)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "ro"
            store.mkdir()
            os.chmod(store, 0o500)  # r-x: owner cannot create files
            try:
                self.assertEqual(hsw.probe_store_once(store, nonce="n2"), "fail")
            finally:
                os.chmod(store, 0o700)  # restore so TemporaryDirectory cleanup works

    def test_probe_fail_on_missing_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(hsw.probe_store_once(Path(d) / "nope", nonce="n3"), "fail")

    def test_timeout_returns_probe_outcome_when_fast(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            outcome = hsw.run_probe_with_timeout(Path(d), nonce="fast", timeout_s=2.0)
            self.assertEqual(outcome, "ok")

    def test_slow_probe_is_inconclusive_and_isolated(self):
        # MED-2: a probe that blocks past the timeout -> "inconclusive", and the
        # CALLER unblocks near the timeout (not after the full slow duration) — the
        # isolation guarantee (a hung fsync must not stall the caller).
        import time

        def slow_probe(store_path, *, nonce):
            time.sleep(5.0)
            return "ok"

        start = time.monotonic()
        outcome = hsw.run_probe_with_timeout(
            Path("/tmp"), nonce="slow", timeout_s=0.2, probe_fn=slow_probe
        )
        elapsed = time.monotonic() - start
        self.assertEqual(outcome, "inconclusive")
        self.assertLess(elapsed, 2.0)  # unblocked at ~timeout, not after 5s

    def test_sweep_removes_only_own_prefix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = Path(d)
            (store / f"{hsw.CANARY_PROBE_PREFIX}aaa").write_text("x")
            (store / f"{hsw.CANARY_PROBE_PREFIX}bbb").write_text("x")
            (store / "1700000000.M1.mailhost").write_text("real maildir file")  # foreign
            removed = hsw.sweep_stale_probes(store)
            self.assertEqual(removed, 2)
            self.assertEqual([p.name for p in store.iterdir()], ["1700000000.M1.mailhost"])

    def test_sweep_missing_dir_is_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(hsw.sweep_stale_probes(Path(d) / "gone"), 0)


class CanaryStampTests(unittest.TestCase):
    def _stamp(self, outcome, prev=None, now="2026-09-12T12:00:00Z"):
        return hsw.build_canary_stamp(
            outcome=outcome, prev=prev, boot_id="boot-abc",
            monotonic_since_boot=123.4, wall_clock_written_at=now,
            probe_store_path="/home/post-office/mailbox/recovery/tmp",
        )

    def test_ok_stamps_zero_counter(self):
        s = self._stamp("ok")
        self.assertEqual(s["probe_ok"], "ok")
        self.assertEqual(s["consecutive_inconclusive"], 0)
        self.assertIsNone(s["inconclusive_since"])

    def test_fail_is_a_fresh_stamp_not_absence(self):
        # NEW-1: a failed probe still produces a fresh stamp (probe_ok=fail), so
        # store-dead = fresh fail is readable; it is NOT "no write".
        s = self._stamp("fail")
        self.assertEqual(s["probe_ok"], "fail")
        self.assertEqual(s["consecutive_inconclusive"], 0)
        self.assertIsNone(s["inconclusive_since"])
        self.assertEqual(s["schema"], hsw.CANARY_SCHEMA)

    def test_first_inconclusive_starts_streak(self):
        s = self._stamp("inconclusive", now="2026-09-12T12:00:00Z")
        self.assertEqual(s["consecutive_inconclusive"], 1)
        self.assertEqual(s["inconclusive_since"], "2026-09-12T12:00:00Z")

    def test_consecutive_inconclusive_increments_and_carries_since(self):
        s1 = self._stamp("inconclusive", now="2026-09-12T12:00:00Z")
        s2 = self._stamp("inconclusive", prev=s1, now="2026-09-12T12:01:00Z")
        s3 = self._stamp("inconclusive", prev=s2, now="2026-09-12T12:02:00Z")
        self.assertEqual([s1["consecutive_inconclusive"], s2["consecutive_inconclusive"],
                          s3["consecutive_inconclusive"]], [1, 2, 3])
        # since is the START of the streak, carried forward unchanged.
        self.assertEqual(s3["inconclusive_since"], "2026-09-12T12:00:00Z")

    def test_ok_resets_streak(self):
        s1 = self._stamp("inconclusive", now="2026-09-12T12:00:00Z")
        s2 = self._stamp("inconclusive", prev=s1, now="2026-09-12T12:01:00Z")
        s3 = self._stamp("ok", prev=s2, now="2026-09-12T12:02:00Z")
        self.assertEqual(s3["consecutive_inconclusive"], 0)
        self.assertIsNone(s3["inconclusive_since"])

    def test_fail_resets_streak(self):
        s1 = self._stamp("inconclusive", now="2026-09-12T12:00:00Z")
        s2 = self._stamp("fail", prev=s1, now="2026-09-12T12:01:00Z")
        self.assertEqual(s2["consecutive_inconclusive"], 0)
        self.assertIsNone(s2["inconclusive_since"])

    def test_streak_restarts_after_reset(self):
        # inconclusive, ok, inconclusive -> the second inconclusive is a NEW streak
        # (since = its own timestamp, count = 1), not a continuation.
        s1 = self._stamp("inconclusive", now="2026-09-12T12:00:00Z")
        s2 = self._stamp("ok", prev=s1, now="2026-09-12T12:01:00Z")
        s3 = self._stamp("inconclusive", prev=s2, now="2026-09-12T12:02:00Z")
        self.assertEqual(s3["consecutive_inconclusive"], 1)
        self.assertEqual(s3["inconclusive_since"], "2026-09-12T12:02:00Z")

    def test_invalid_outcome_raises(self):
        with self.assertRaises(ValueError):
            self._stamp("maybe")

    def test_no_prev_inconclusive_starts_at_one(self):
        count, since = hsw.next_inconclusive_tracking(None, "inconclusive", now_iso="2026-09-12T12:00:00Z")
        self.assertEqual((count, since), (1, "2026-09-12T12:00:00Z"))


class SocketTransportTests(unittest.TestCase):
    def _allowlist_for_self(self):
        return hsw.Allowlist.from_mapping({"version": 1, "writers": [
            {"uid": os.getuid(), "account": "self-acct", "persona": "self",
             "signal_kinds": ["signal"], "store_path": "/tmp/store"},
        ]})

    def test_read_peer_uid_over_socketpair(self):
        import socket
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(hsw.read_peer_uid(a), os.getuid())
        finally:
            a.close(); b.close()

    def test_read_frame_valid(self):
        import socket
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            body = b'{"v":1,"signal_kind":"signal","payload":{}}'
            b.sendall(hsw.frame(body))
            got, refusal = hsw.read_request_frame(a, read_deadline_s=1.0)
            self.assertIsNone(refusal)
            self.assertEqual(got, body)
        finally:
            a.close(); b.close()

    def test_read_frame_oversized_length(self):
        import socket
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            b.sendall(struct.pack(">I", hsw.MAX_FRAME + 1))  # prefix only
            _, refusal = hsw.read_request_frame(a, read_deadline_s=1.0)
            self.assertEqual(refusal, hsw.REFUSE_OVERSIZED)
        finally:
            a.close(); b.close()

    def test_read_frame_truncated_body_malformed(self):
        import socket
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            b.sendall(struct.pack(">I", 100))  # promise 100 bytes...
            b.sendall(b"only-a-few")           # ...send fewer
            b.close()                          # then EOF
            _, refusal = hsw.read_request_frame(a, read_deadline_s=1.0)
            self.assertEqual(refusal, hsw.REFUSE_MALFORMED)
        finally:
            a.close()

    def test_read_frame_timeout_malformed(self):
        import socket
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # send nothing -> the deadline elapses -> REFUSE_MALFORMED (no hang)
            _, refusal = hsw.read_request_frame(a, read_deadline_s=0.15)
            self.assertEqual(refusal, hsw.REFUSE_MALFORMED)
        finally:
            a.close(); b.close()

    def _roundtrip(self, allowlist, body, tree_root, writes):
        """Run handle_connection on one socketpair end in a thread; client sends
        `body` framed and reads the response. `writes` records (leaf, data)."""
        import socket, threading
        srv, cli = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        def leaf_writer(leaf, data):
            writes.append((leaf, data))

        outcome = {}

        def run():
            outcome["refusal"] = hsw.handle_connection(
                srv, allowlist=allowlist, tree_root=tree_root,
                leaf_writer=leaf_writer, now_iso_fn=lambda: "2026-09-12T12:00:00Z",
                read_deadline_s=1.0,
            )

        t = threading.Thread(target=run)
        t.start()
        cli.sendall(hsw.frame(body))
        resp = b""
        cli.settimeout(2.0)
        try:
            while True:
                chunk = cli.recv(4096)
                if not chunk:
                    break
                resp += chunk
        except (TimeoutError, OSError):
            pass
        t.join(2.0)
        cli.close()
        doc = json.loads(resp[hsw.LEN_PREFIX_SIZE:]) if resp else None
        return outcome.get("refusal"), doc

    def test_handle_connection_success_writes_leaf(self):
        import tempfile
        writes = []
        with tempfile.TemporaryDirectory() as d:
            refusal, doc = self._roundtrip(
                self._allowlist_for_self(),
                b'{"v":1,"signal_kind":"signal","payload":{"cpu":42}}',
                Path(d), writes,
            )
        self.assertIsNone(refusal)
        self.assertTrue(doc["ok"])
        self.assertEqual(len(writes), 1)
        leaf, data = writes[0]
        self.assertEqual(leaf, Path(doc["leaf"]))
        rec = json.loads(data)
        # server-derived provenance, not from the body
        self.assertEqual(rec["account"], "self-acct")
        self.assertEqual(rec["uid"], os.getuid())
        self.assertEqual(rec["payload"], {"cpu": 42})

    def test_handle_connection_reserved_writes_nothing(self):
        import tempfile
        writes = []
        with tempfile.TemporaryDirectory() as d:
            refusal, doc = self._roundtrip(
                self._allowlist_for_self(),
                b'{"v":1,"signal_kind":"canary","payload":{}}', Path(d), writes,
            )
        self.assertEqual(refusal, hsw.REFUSE_RESERVED)
        self.assertFalse(doc["ok"])
        self.assertEqual(writes, [])  # nothing written on refusal

    def test_handle_connection_unlisted_kind_writes_nothing(self):
        import tempfile
        writes = []
        with tempfile.TemporaryDirectory() as d:
            refusal, doc = self._roundtrip(
                self._allowlist_for_self(),
                b'{"v":1,"signal_kind":"nope","payload":{}}', Path(d), writes,
            )
        self.assertEqual(refusal, hsw.REFUSE_UNLISTED)
        self.assertEqual(writes, [])

    def test_atomic_write_leaf_and_symlink_not_followed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root / "outside.txt"
            outside.write_text("SECRET-UNTOUCHED")
            leaf = root / "acct" / "signal"
            leaf.parent.mkdir(parents=True)
            leaf.symlink_to(outside)  # leaf is a symlink to a file outside
            hsw.atomic_write_leaf(leaf, b"new-data")
            # the symlink was replaced by a regular file (not followed) ...
            self.assertFalse(leaf.is_symlink())
            self.assertEqual(leaf.read_bytes(), b"new-data")
            # ... and the symlink target was NOT overwritten.
            self.assertEqual(outside.read_text(), "SECRET-UNTOUCHED")

    def test_atomic_write_leaf_world_readable_under_hardened_umask(self):
        # R1: signal files must be 0644 (world-readable for cross-account verify) even
        # though the unit runs UMask=0077. fchmod must override the umask.
        import tempfile
        old = os.umask(0o077)  # simulate the hardened service umask
        try:
            with tempfile.TemporaryDirectory() as d:
                leaf = Path(d) / "acct" / "signal"
                hsw.atomic_write_leaf(leaf, b"data")
                self.assertEqual(os.stat(leaf).st_mode & 0o777, 0o644)
        finally:
            os.umask(old)

    @unittest.skipUnless(
        os.environ.get("HSW_CROSS_UID_ACCT"),
        "set HSW_CROSS_UID_ACCT=<a sudo-able account> to run the real cross-uid test",
    )
    def test_socket_peercred_cross_uid(self):
        # R8 net-new: a helper running as a DIFFERENT uid connects; the server must
        # authorize by the kernel-attested peer uid, not anything the helper claims.
        import socket, subprocess, tempfile, threading, shutil
        acct = os.environ["HSW_CROSS_UID_ACCT"]
        if not shutil.which("sudo"):
            self.skipTest("sudo unavailable")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.chmod(d, 0o777)
        sock_path = os.path.join(d, "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path); os.chmod(sock_path, 0o666); srv.listen(1)
        seen = {}

        def accept_once():
            conn, _ = srv.accept()
            seen["uid"] = hsw.read_peer_uid(conn)
            conn.close()

        th = threading.Thread(target=accept_once); th.start()
        helper = (
            "import socket,sys;"
            f"c=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);c.connect({sock_path!r});c.close()"
        )
        rc = subprocess.run(["sudo", "-n", "-u", acct, "python3", "-c", helper],
                            capture_output=True, timeout=10)
        th.join(5.0); srv.close()
        if rc.returncode != 0:
            self.skipTest(f"cross-uid helper could not run: {rc.stderr.decode()[:200]}")
        import pwd
        self.assertEqual(seen.get("uid"), pwd.getpwnam(acct).pw_uid)
        self.assertNotEqual(seen.get("uid"), os.getuid())


class ConnectionGateTests(unittest.TestCase):
    def test_global_cap(self):
        g = hsw.ConnectionGate(max_conns=2, per_uid=99)
        self.assertTrue(g.try_acquire(1))
        self.assertTrue(g.try_acquire(2))
        self.assertFalse(g.try_acquire(3))  # global cap hit
        g.release(1)
        self.assertTrue(g.try_acquire(3))   # freed a slot

    def test_per_uid_cap(self):
        g = hsw.ConnectionGate(max_conns=99, per_uid=2)
        self.assertTrue(g.try_acquire(7))
        self.assertTrue(g.try_acquire(7))
        self.assertFalse(g.try_acquire(7))  # per-uid cap hit
        self.assertTrue(g.try_acquire(8))   # a different uid is unaffected
        g.release(7)
        self.assertTrue(g.try_acquire(7))

    def test_release_unknown_uid_is_noop(self):
        g = hsw.ConnectionGate(max_conns=2, per_uid=2)
        g.release(999)  # never acquired — must not go negative
        self.assertTrue(g.try_acquire(1))
        self.assertTrue(g.try_acquire(2))
        self.assertFalse(g.try_acquire(3))


class AllowlistFileReaderTests(unittest.TestCase):
    def _loader(self, root: Path):
        from tools.host_control.trusted_files import RootOwnedFileLoader
        # test tree is owned by the test uid, not root -> use the test ctor.
        return RootOwnedFileLoader._open_for_test((root,), expected_owner_uid=os.getuid())

    _VALID = (
        '{"version":1,"writers":['
        '{"uid":1001,"account":"claude-fallback","persona":"recovery",'
        '"signal_kinds":["signal"],"store_path":"/home/post-office/mailbox/recovery/tmp"},'
        '{"uid":1000,"account":"claude","persona":"orchestrator",'
        '"signal_kinds":["signal"],"store_path":"/home/post-office/mailbox/orchestrator/tmp"}]}'
    )

    def test_load_valid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = root / "writers.conf"
            conf.write_text(self._VALID); os.chmod(conf, 0o600)
            loader = self._loader(root)
            try:
                al = hsw.load_allowlist(conf, loader=loader)
            finally:
                loader.close()
            self.assertEqual(len(al), 2)
            self.assertEqual(al.account_for_uid(1001).account, "claude-fallback")

    def test_load_rejects_reserved_kind(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = root / "writers.conf"
            conf.write_text(self._VALID.replace('["signal"]', '["signal","canary"]', 1)); os.chmod(conf, 0o600)
            loader = self._loader(root)
            try:
                with self.assertRaises(hsw.SignalWriterConfigError):
                    hsw.load_allowlist(conf, loader=loader)
            finally:
                loader.close()

    def test_load_rejects_symlinked_file(self):
        # O_NOFOLLOW: a symlinked allowlist path is refused (ORG-050/R4).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            real = root / "real.conf"
            real.write_text(self._VALID); os.chmod(real, 0o600)
            link = root / "link.conf"
            link.symlink_to(real)
            loader = self._loader(root)
            try:
                with self.assertRaises(hsw.SignalWriterConfigError):
                    hsw.load_allowlist(link, loader=loader)
            finally:
                loader.close()

    def test_load_rejects_bad_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = root / "writers.conf"
            conf.write_text("not json {{{"); os.chmod(conf, 0o600)
            loader = self._loader(root)
            try:
                with self.assertRaises(hsw.SignalWriterConfigError):
                    hsw.load_allowlist(conf, loader=loader)
            finally:
                loader.close()


class VerifyStoreTests(unittest.TestCase):
    def test_verify_true_on_writable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(hsw.verify_store_writable(Path(d), timeout_s=2.0))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory write perms")
    def test_verify_false_on_nonwritable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "ro"
            store.mkdir()
            os.chmod(store, 0o500)
            try:
                self.assertFalse(hsw.verify_store_writable(store, timeout_s=2.0))
            finally:
                os.chmod(store, 0o700)


class ReloadTests(unittest.TestCase):
    def _loader(self, root):
        from tools.host_control.trusted_files import RootOwnedFileLoader
        return RootOwnedFileLoader._open_for_test((root,), expected_owner_uid=os.getuid())

    def _write(self, root, name, text):
        p = root / name
        p.write_text(text)
        os.chmod(p, 0o600)
        return p

    _V1 = ('{"version":1,"writers":[{"uid":1001,"account":"a","persona":"a",'
           '"signal_kinds":["signal"],"store_path":"/x"}]}')
    _V2 = ('{"version":1,"writers":[{"uid":1001,"account":"a","persona":"a",'
           '"signal_kinds":["signal"],"store_path":"/x"},'
           '{"uid":1002,"account":"b","persona":"b","signal_kinds":["signal"],"store_path":"/y"}]}')
    _BAD = ('{"version":1,"writers":[{"uid":1001,"account":"a","persona":"a",'
            '"signal_kinds":["signal","canary"],"store_path":"/x"}]}')  # reserved kind

    def test_reload_valid_swaps(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = self._write(root, "w.conf", self._V1)
            cur = hsw.load_allowlist(conf, loader=self._loader(root))
            self.assertEqual(len(cur), 1)
            self._write(root, "w.conf", self._V2)  # grow to 2 writers
            new, ok = hsw.reload_allowlist(cur, conf, loader=self._loader(root))
            self.assertTrue(ok)
            self.assertEqual(len(new), 2)

    def test_reload_invalid_keeps_last_good(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = self._write(root, "w.conf", self._V1)
            cur = hsw.load_allowlist(conf, loader=self._loader(root))
            self._write(root, "w.conf", self._BAD)  # now invalid (reserved kind)
            new, ok = hsw.reload_allowlist(cur, conf, loader=self._loader(root))
            self.assertFalse(ok)
            self.assertIs(new, cur)  # last-good kept, same object

    def test_reload_missing_file_keeps_last_good(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            conf = self._write(root, "w.conf", self._V1)
            cur = hsw.load_allowlist(conf, loader=self._loader(root))
            new, ok = hsw.reload_allowlist(cur, root / "gone.conf", loader=self._loader(root))
            self.assertFalse(ok)
            self.assertIs(new, cur)


class ServerTests(unittest.TestCase):
    def _allowlist_for_self(self):
        return hsw.Allowlist.from_mapping({"version": 1, "writers": [
            {"uid": os.getuid(), "account": "self-acct", "persona": "self",
             "signal_kinds": ["signal"], "store_path": "/tmp/store"},
        ]})

    def _listener(self):
        import socket, tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.sock")
        lis = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        lis.bind(path)
        lis.listen(1)
        return lis, path, d

    def _client_send(self, path, body):
        import socket, threading
        holder = {}

        def run():
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(3.0)
            c.connect(path)
            try:
                # sendall is inside the try: a gate-shed server can close the
                # socket before the send finishes (BrokenPipe), which must read
                # as 'no response', not kill the thread and leave resp unset.
                c.sendall(hsw.frame(body))
                holder["resp"] = c.recv(4096)
            except (TimeoutError, OSError):
                holder["resp"] = b""
            c.close()

        th = threading.Thread(target=run)
        th.start()
        return th, holder

    def test_serve_one_success_writes_leaf(self):
        import tempfile, shutil
        lis, path, d = self._listener()
        writes = []
        try:
            with tempfile.TemporaryDirectory() as tree:
                server = hsw.SignalWriterServer(
                    lis, self._allowlist_for_self(), tree_root=Path(tree),
                    leaf_writer=lambda leaf, data: writes.append((leaf, data)),
                )
                th, holder = self._client_send(path, b'{"v":1,"signal_kind":"signal","payload":{}}')
                status = server.serve_one(accept_timeout=3.0)
                th.join(3.0)
            self.assertEqual(status, "ok")
            self.assertEqual(len(writes), 1)
            self.assertTrue(json.loads(holder["resp"][hsw.LEN_PREFIX_SIZE:])["ok"])
        finally:
            lis.close(); shutil.rmtree(d, ignore_errors=True)

    def test_serve_one_gate_full_sheds(self):
        import shutil
        lis, path, d = self._listener()
        try:
            gate = hsw.ConnectionGate(max_conns=0, per_uid=99)  # always full
            server = hsw.SignalWriterServer(lis, self._allowlist_for_self(), gate=gate)
            th, holder = self._client_send(path, b'{"v":1,"signal_kind":"signal","payload":{}}')
            status = server.serve_one(accept_timeout=3.0)
            th.join(3.0)
            self.assertEqual(status, "gated")
            self.assertEqual(server.shed_count, 1)
            self.assertEqual(holder["resp"], b"")  # connection closed, no response
        finally:
            lis.close(); shutil.rmtree(d, ignore_errors=True)

    def test_serve_one_idle_on_timeout(self):
        import shutil
        lis, path, d = self._listener()
        try:
            server = hsw.SignalWriterServer(lis, self._allowlist_for_self())
            self.assertEqual(server.serve_one(accept_timeout=0.15), "idle")  # no client
        finally:
            lis.close(); shutil.rmtree(d, ignore_errors=True)

    def test_request_reload_flag_processed(self):
        # M7 flag mechanics through the server: request_reload() sets the flag;
        # serve_one()'s pre-accept _maybe_reload consumes it and atomic-swaps via the
        # injected reload_loader. Start empty, reload to a 1-writer conf.
        import tempfile, shutil
        from tools.host_control.trusted_files import RootOwnedFileLoader
        lis, path, d = self._listener()
        try:
            with tempfile.TemporaryDirectory() as cd:
                root = Path(cd)
                conf = root / "w.conf"
                conf.write_text('{"version":1,"writers":[{"uid":1001,"account":"a","persona":"a",'
                                '"signal_kinds":["signal"],"store_path":"/x"}]}')
                os.chmod(conf, 0o600)
                loader = RootOwnedFileLoader._open_for_test((root,), expected_owner_uid=os.getuid())
                start = hsw.Allowlist.from_mapping({"version": 1, "writers": []})
                server = hsw.SignalWriterServer(lis, start, allowlist_path=conf, reload_loader=loader)
                self.assertEqual(len(server.allowlist), 0)
                self.assertIsNone(server._maybe_reload())  # no flag yet -> no-op
                server.request_reload()
                self.assertTrue(server._maybe_reload())    # flag set -> valid swap
                self.assertEqual(len(server.allowlist), 1)
                self.assertIsNone(server._maybe_reload())  # flag cleared -> no repeat
                loader.close()
        finally:
            lis.close(); shutil.rmtree(d, ignore_errors=True)


class UnitFileTests(unittest.TestCase):
    """Structural lint of the staged .service unit — guards the deployment security
    posture (RFC-003 R1/D-8/MED-3) against drift."""

    def _unit(self):
        root = Path(__file__).resolve().parent.parent
        p = root / "tools" / "host_control" / "dist" / "pantheon-health-signal-writer.service"
        self.assertTrue(p.is_file(), f"staged unit missing: {p}")
        return p.read_text()

    def test_required_hardening_directives_present(self):
        u = self._unit()
        for directive in (
            "User=pantheon-healthsig",
            "Group=pantheon-healthsig",
            "SupplementaryGroups=pantheon-mailbox",   # R1 hub traverse
            "ProtectHome=tmpfs",                      # MED-3 (must reach the store)
            "CapabilityBoundingSet=",                 # ORG-052: no caps
            "NoNewPrivileges=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "RuntimeDirectory=pantheon",              # M1
            "StateDirectory=pantheon/health-signals",
            "StateDirectoryMode=0755",                # world-readable signal tree
            "UMask=0077",
        ):
            self.assertIn(directive, u, f"missing hardening directive: {directive}")

    def test_no_runtimemaxsec(self):
        # D-8: no ACTIVE RuntimeMaxSec= directive (a comment explaining the choice is
        # fine — check only non-comment directive lines).
        active = [ln.strip() for ln in self._unit().splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertFalse(
            any(ln.startswith("RuntimeMaxSec=") for ln in active),
            "RuntimeMaxSec must not be set as a directive (D-8)",
        )

    def test_protecthome_not_full_yes(self):
        # MED-3: ProtectHome=yes would block the store probe entirely.
        u = self._unit()
        self.assertNotIn("ProtectHome=yes", u)

    def test_hub_root_not_bound(self):
        # MED-3: the unit must NOT bind the hub root (that would expose every maildir
        # RW); only per-store_path binds, injected at install. The staged unit carries
        # only the injection marker, never a literal hub-root bind.
        u = self._unit()
        self.assertNotIn("BindPaths=/home/post-office/mailbox\n", u)
        self.assertNotIn("BindReadOnlyPaths=", u)  # RO would break the O_EXCL probe

    def test_is_marked_staged(self):
        # Must be clearly a staged artifact (nothing installs it here).
        self.assertIn("STAGED ARTIFACT", self._unit())


class ResponseTests(unittest.TestCase):
    def test_response_roundtrip(self):
        framed = hsw.build_response(ok=True, leaf="/var/lib/pantheon/health-signals/recovery/signal", refusal=None)
        n = hsw.parse_length_prefix(framed[: hsw.LEN_PREFIX_SIZE])
        doc = json.loads(framed[hsw.LEN_PREFIX_SIZE:])
        self.assertEqual(n, len(framed) - hsw.LEN_PREFIX_SIZE)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["v"], hsw.PROTOCOL_VERSION)
        self.assertIsNone(doc["refusal"])

    def test_refusal_response(self):
        framed = hsw.build_response(ok=False, leaf=None, refusal=hsw.REFUSE_RESERVED)
        doc = json.loads(framed[hsw.LEN_PREFIX_SIZE:])
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["refusal"], "REFUSE_RESERVED")


if __name__ == "__main__":
    unittest.main()
