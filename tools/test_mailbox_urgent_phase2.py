"""test_mailbox_urgent_phase2.py — ORG-026 mailbox severity Phase 2 (T-1641).

Covers the urgent-path SEND-side mechanics: the build-not-activate kill-switch,
the per-(sender,recipient) rate-limit (downgrade-never-drop), and the
`.urgent_pending` marker write. Every mechanic here is gated by
`mailbox.urgent_phase2_enabled()`; the single highest-value property this file
proves is that with the flag UNSET, `send()` is byte-for-byte identical to the
pre-T-1641 (Phase 1) behavior — that identity IS the build-not-activate boundary,
not merely documentation of intent (see notes/2026-07-08-mailbox-severity-design-
resolution.md and T-1641's brief).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # bare-import the SUT
import mailbox  # noqa: E402


def _enabled():
    """Context manager stand-in: patch the env var this module reads."""
    return mock.patch.dict(os.environ, {mailbox._URGENT_PHASE2_ENV: "1"})


class FlagOffIsPhase1IdenticalTest(unittest.TestCase):
    """The load-bearing property: flag unset -> zero Phase-2 side effects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_flag_defaults_off(self):
        # No env var set at all (the real deployed default) -> disabled.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            self.assertFalse(mailbox.urgent_phase2_enabled())

    def test_urgent_send_with_flag_off_writes_no_marker(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            mailbox.send(self.inbox, {"x": 1}, sender="orchestrator",
                        subject="wedge", severity="urgent")
        self.assertFalse((self.inbox / ".urgent_pending").exists(),
                         "flag-off must not create the marker (Phase 1 shape)")

    def test_urgent_send_with_flag_off_writes_no_rate_sidecar(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            for _ in range(10):
                mailbox.send(self.inbox, {"x": 1}, sender="orchestrator",
                            subject="wedge", severity="urgent",
                            dedupe_pending=False)
        self.assertFalse((self.inbox / ".urgent_rate").exists(),
                         "flag-off must not create rate-limit bookkeeping")

    def test_urgent_send_with_flag_off_never_downgrades(self):
        # Even far past what would be the rate limit once enabled, flag-off
        # never touches severity — it stays exactly what the caller passed.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            ids = [mailbox.send(self.inbox, {"x": i}, sender="orchestrator",
                                subject=f"s{i}", severity="urgent",
                                dedupe_pending=False)
                   for i in range(20)]
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(len(got), 20)
        self.assertTrue(all(m["severity"] == "urgent" for m in got))


class MarkerWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_urgent_send_writes_marker(self):
        with _enabled():
            mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator",
                               subject="disk 91%", severity="urgent")
        marker = self.inbox / ".urgent_pending"
        self.assertTrue(marker.exists())
        rec = json.loads(marker.read_text())
        self.assertEqual(rec["sender"], "orchestrator")
        self.assertEqual(rec["subject"], "disk 91%")
        self.assertEqual(rec["msgid"], mid)

    def test_normal_send_writes_no_marker(self):
        with _enabled():
            mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="fyi")
        self.assertFalse((self.inbox / ".urgent_pending").exists(),
                         "normal severity must never touch the urgent marker")

    def test_second_urgent_overwrites_marker(self):
        with _enabled():
            mailbox.send(self.inbox, {"x": 1}, sender="orchestrator",
                        subject="first", severity="urgent",
                        dedupe_pending=False)
            mid2 = mailbox.send(self.inbox, {"x": 2}, sender="orchestrator",
                                subject="second", severity="urgent",
                                dedupe_pending=False)
        rec = json.loads((self.inbox / ".urgent_pending").read_text())
        self.assertEqual(rec["msgid"], mid2, "marker records the latest urgent")


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def _send_n_urgent(self, n, sender="orchestrator", now_fn=None):
        results = []
        for i in range(n):
            mid = mailbox.send(self.inbox, {"i": i}, sender=sender,
                               subject=f"s{i}", severity="urgent",
                               dedupe_pending=False)
            results.append(mid)
        return results

    def test_within_limit_stays_urgent(self):
        with _enabled(), mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", 3):
            self._send_n_urgent(3)
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertTrue(all(m["severity"] == "urgent" for m in got))

    def test_excess_downgrades_never_drops(self):
        with _enabled(), mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", 3):
            self._send_n_urgent(5)
        got = []
        res = mailbox.consume(self.inbox, lambda m: got.append(m))
        # NEVER DROP: all 5 must still be delivered.
        self.assertEqual(len(got), 5)
        self.assertEqual(res["errors"], [])
        severities = [m["severity"] for m in got]
        self.assertEqual(severities.count("urgent"), 3,
                         "only the first N in-window stay urgent")
        self.assertEqual(severities.count("normal"), 2,
                         "excess is downgraded, not dropped")

    def test_downgrade_is_logged(self):
        with _enabled(), mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", 1):
            self._send_n_urgent(2)
        log = self.inbox / ".urgent_rate" / "orchestrator.downgrades.jsonl"
        self.assertTrue(log.exists())
        lines = log.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["sender"], "orchestrator")

    def test_window_reset_allows_urgent_again(self):
        now = [1000.0]
        with _enabled(), mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", 1), \
             mock.patch.object(mailbox, "URGENT_RATE_LIMIT_WINDOW_SECONDS", 60), \
             mock.patch("time.time", side_effect=lambda: now[0]):
            self._send_n_urgent(1)
            now[0] += 61  # past the window
            self._send_n_urgent(1)
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual([m["severity"] for m in got], ["urgent", "urgent"],
                         "a fresh window must not carry over the prior count")

    def test_rate_limit_is_per_sender(self):
        with _enabled(), mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", 1):
            self._send_n_urgent(2, sender="orchestrator")
            self._send_n_urgent(2, sender="recovery")
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        by_sender = {}
        for m in got:
            by_sender.setdefault(m["sender"], []).append(m["severity"])
        self.assertEqual(by_sender["orchestrator"].count("urgent"), 1)
        self.assertEqual(by_sender["recovery"].count("urgent"), 1)

    def test_rate_limit_bookkeeping_failure_fails_open_to_urgent(self):
        # A sidecar that cannot be read/written must never be the reason a
        # genuinely urgent message gets silently downgraded — fail open toward
        # KEEPING urgent, the same direction backoff_active() and the DLQ
        # bookkeeping already fail in this file.
        with _enabled(), mock.patch.object(
            mailbox, "_check_urgent_rate_limit", side_effect=OSError("boom")
        ):
            mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator",
                               subject="s", severity="urgent")
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(got[0]["severity"], "urgent")


if __name__ == "__main__":
    unittest.main()
