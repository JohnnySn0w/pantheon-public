#!/usr/bin/env python3
"""Unittest coverage for the T-1985 dead-letter observability in
tools/acp_bridge_backlog_watchdog.py.

The defect: mailbox._alert_owner sends a DLQ quarantine notice back into the
SAME inbox the bridge is failing to drain, where a genuinely-parked seat can
never see it. The fix extends the (external, lease-free) backlog watchdog to
scan failed/ and route a dead-letter alert Recovery-side via
inbox_consumer.route_failure_alert (the T-0722 per-agent failure path) rather
than to the dead seat's own inbox.

unittest (not pytest) so it runs under `python3 -m unittest`.
"""
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import acp_bridge_backlog_watchdog as wd


class ReadFailedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "specialist"
        (self.inbox / "new").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_absent_failed_dir_is_healthy_zero(self):
        # failed/ is created lazily on first quarantine, so absent == 0, NOT a
        # can't-check alert (the deliberate asymmetry with read_backlog).
        count, names = wd.read_failed(self.inbox)
        self.assertEqual(count, 0)
        self.assertEqual(names, [])

    def test_counts_files_in_failed_dir(self):
        failed = self.inbox / "failed"
        failed.mkdir()
        (failed / "01784790321641723172.1.000000.keep_going").write_text("{}")
        (failed / "01784792146517838782.1.000000.keep_going").write_text("{}")
        count, names = wd.read_failed(self.inbox)
        self.assertEqual(count, 2)
        self.assertEqual(len(names), 2)


class _AlertRecorder:
    """Captures route_failure_alert / _alert calls without sending anything."""
    def __init__(self):
        self.recovery_calls = []   # (agent, msg, vocab)
        self.operator_calls = []  # (text,)


class CheckFailedRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "specialist"
        (self.inbox / "new").mkdir(parents=True)
        self.rec = _AlertRecorder()

        # Route the Recovery-side path and the direct-operator path into the
        # recorder, and give the watchdog an isolated cooldown-state file.
        self._orig_route = wd._route_dlq_recovery
        self._orig_alert = wd._alert
        self._orig_state_path = wd.STATE_PATH
        wd._route_dlq_recovery = lambda agent, body: self.rec.recovery_calls.append((agent, body))
        wd._alert = lambda text: self.rec.operator_calls.append(text)
        wd.STATE_PATH = Path(self._tmp.name) / "alert_state.json"

    def tearDown(self):
        wd._route_dlq_recovery = self._orig_route
        wd._alert = self._orig_alert
        wd.STATE_PATH = self._orig_state_path
        self._tmp.cleanup()

    def test_failed_dir_nonempty_routes_via_recovery(self):
        failed = self.inbox / "failed"
        failed.mkdir()
        (failed / "01784790321641723172.1.000000.keep_going").write_text("{}")

        rc = wd.check("specialist", str(self.inbox), dry_run=False)

        self.assertEqual(rc, 1, "a non-empty failed/ is a breach")
        self.assertEqual(len(self.rec.recovery_calls), 1, "the DLQ alert routes Recovery-side, exactly once")
        agent, body = self.rec.recovery_calls[0]
        self.assertEqual(agent, "specialist")
        self.assertIn("acp-dlq", body.lower())
        self.assertEqual(self.rec.operator_calls, [],
                         "an empty new/ must not fire the direct-operator backlog alert")

    def test_failed_dir_empty_no_alert(self):
        # new/ empty, failed/ absent -> fully healthy, no alert of either kind.
        rc = wd.check("specialist", str(self.inbox), dry_run=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.rec.recovery_calls, [])
        self.assertEqual(self.rec.operator_calls, [])

    def test_failed_alert_respects_cooldown(self):
        failed = self.inbox / "failed"
        failed.mkdir()
        (failed / "01784790321641723172.1.000000.keep_going").write_text("{}")

        now = 1_000_000.0
        wd.check("specialist", str(self.inbox), dry_run=False, now=now)
        # Second check within the cooldown window must not re-route.
        wd.check("specialist", str(self.inbox), dry_run=False, now=now + 60)

        self.assertEqual(len(self.rec.recovery_calls), 1,
                         "second check inside the cooldown must not re-alert")

    def test_dry_run_sends_nothing(self):
        failed = self.inbox / "failed"
        failed.mkdir()
        (failed / "01784790321641723172.1.000000.keep_going").write_text("{}")

        rc = wd.check("specialist", str(self.inbox), dry_run=True)

        self.assertEqual(rc, 1, "dry-run still reports the breach in the return code")
        self.assertEqual(self.rec.recovery_calls, [], "dry-run must transmit nothing")


if __name__ == "__main__":
    unittest.main()
