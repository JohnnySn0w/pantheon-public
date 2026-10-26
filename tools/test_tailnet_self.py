#!/usr/bin/env python3
"""Tests for tools/tailnet_self.py (T-5385)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import tailnet_self as ts  # noqa: E402


def _run(stdout="", rc=0, exc=None):
    def run(argv, **kw):
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, rc, stdout, "")
    return run


# Shapes from swarm 2026-10-23: logged out still prints JSON (rc 0 or 1).
NEEDS_LOGIN = json.dumps({"BackendState": "NeedsLogin", "Self": {"Online": False}})
RUNNING = json.dumps({"BackendState": "Running", "Self": {"Online": True}})


class StateTests(unittest.TestCase):
    def test_needs_login_is_off(self):
        self.assertEqual(ts.off_tailnet(_run(NEEDS_LOGIN, rc=1)), "NeedsLogin")

    def test_running_is_on(self):
        self.assertIsNone(ts.off_tailnet(_run(RUNNING)))

    def test_unknown_never_suppresses(self):
        for run in (_run(exc=FileNotFoundError("tailscale")), _run(exc=subprocess.TimeoutExpired("t", 10)),
                    _run("not json"), _run(json.dumps({"Self": {}})), _run("")):
            self.assertIsNone(ts.off_tailnet(run))
            self.assertIsNone(ts.backend_state(run))


class ReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "tailnet-self.json"
        self.sent = []

    def _report(self, monitor, now, send=None):
        return ts.report_off_tailnet(monitor, "NeedsLogin", detail="ssh timed out", now=now,
                                     send=send or (lambda s, b: self.sent.append((s, b))), path=self.path)

    def test_one_alert_per_outage_across_monitors(self):
        self.assertTrue(self._report("pve2-heat", 1000))
        self.assertFalse(self._report("ci-runner-disk", 1300))
        self.assertFalse(self._report("geo-hook", 1600))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("swarm off tailnet (NeedsLogin)", self.sent[0][0])
        doc = json.loads(self.path.read_text())
        self.assertEqual(set(doc["monitors"]), {"pve2-heat", "ci-runner-disk", "geo-hook"})

    def test_a_long_outage_reminds_after_the_realert_window(self):
        self._report("pve2-heat", 1000)
        self.assertTrue(self._report("pve2-heat", 1000 + ts.REALERT_SECS))
        self.assertEqual(len(self.sent), 2)

    def test_reachable_rearms_the_next_outage(self):
        self._report("pve2-heat", 1000)
        ts.note_reachable(self.path)
        self.assertTrue(self._report("pve2-heat", 1100))
        self.assertEqual(len(self.sent), 2)

    def test_a_mail_failure_does_not_raise_and_retries_next_time(self):
        def boom(s, b):
            raise OSError("hub down")
        self.assertFalse(self._report("pve2-heat", 1000, send=boom))
        self.assertTrue(self._report("pve2-heat", 1100))

    def test_note_reachable_with_no_outage_is_a_noop(self):
        ts.note_reachable(self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
