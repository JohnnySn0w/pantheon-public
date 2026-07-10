"""Tests for credential_expiry_monitor.py (Jun-2026 401 outage prevention).

Pure-unit: _read_credential is mocked so no live credential files or Telegram
sends occur. Proves the classifier flags the exact Specialist failure (empty refresh
token) while treating a valid-refresh + near/post access-token expiry as OK (no
nightly false alarms), and that alerting is throttled per (agent, class).
"""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.credential_expiry_monitor as cem

NOW = 1_000_000_000.0
HOUR = 3600.0


def _cred(refresh="rt-valid", exp_offset_h=5.0):
    """Build an oauth dict expiring exp_offset_h hours from NOW."""
    return {"accessToken": "at", "refreshToken": refresh, "expiresAt": int((NOW + exp_offset_h * HOUR) * 1000)}


class Classify(unittest.TestCase):
    def _classify_with(self, status, oauth):
        with mock.patch.object(cem, "_read_credential", return_value=(status, oauth)):
            return cem.classify("specialist", {"sudo_user": "claude-research"}, now=NOW)

    def test_no_refresh_is_flagged(self):
        cls, detail = self._classify_with("ok", _cred(refresh=""))
        self.assertEqual(cls, "NO_REFRESH")
        self.assertIn("auto-renew", detail)

    def test_whitespace_refresh_is_no_refresh(self):
        cls, _ = self._classify_with("ok", _cred(refresh="   "))
        self.assertEqual(cls, "NO_REFRESH")

    def test_valid_refresh_future_expiry_is_ok(self):
        cls, detail = self._classify_with("ok", _cred(refresh="rt", exp_offset_h=5))
        self.assertEqual(cls, "OK")
        self.assertIn("valid for", detail)

    def test_valid_refresh_PAST_expiry_is_still_ok(self):
        # Access token expired but refresh present → auto-renews; NOT an alert.
        cls, detail = self._classify_with("ok", _cred(refresh="rt", exp_offset_h=-3))
        self.assertEqual(cls, "OK")
        self.assertIn("auto-renew", detail)

    def test_unreadable_is_malformed(self):
        cls, _ = self._classify_with("unreadable (rc=1)", None)
        self.assertEqual(cls, "MALFORMED")

    def test_missing_expiresAt_is_malformed(self):
        cls, _ = self._classify_with("ok", {"accessToken": "at", "refreshToken": "rt"})
        self.assertEqual(cls, "MALFORMED")


class AlertThrottle(unittest.TestCase):
    def test_should_alert_respects_cooldown(self):
        state = {}
        self.assertTrue(cem._should_alert("specialist", "NO_REFRESH", NOW, state))
        state["specialist:NO_REFRESH"] = NOW
        self.assertFalse(cem._should_alert("specialist", "NO_REFRESH", NOW + 60, state))
        self.assertTrue(
            cem._should_alert("specialist", "NO_REFRESH", NOW + cem.ALERT_COOLDOWN_SECONDS + 1, state)
        )


class CheckLoop(unittest.TestCase):
    def setUp(self):
        # Isolate state file to a temp path so tests never touch real state.
        self._tmp = mock.patch.object(cem, "STATE_PATH", Path("/nonexistent/cem_state.json"))
        self._tmp.start()
        self.addCleanup(self._tmp.stop)
        # One bad (specialist) + one good (orchestrator) account.
        self._cfg = mock.patch.object(
            cem, "CREDENTIALED_ACCOUNTS",
            {"specialist": "claude-research", "orchestrator": "claude"},
        )
        self._cfg.start()
        self.addCleanup(self._cfg.stop)

    def _patched_classify(self):
        def fake(agent, cfg, now=None):
            return ("NO_REFRESH", "empty refresh") if agent == "specialist" else ("OK", "fine")
        return mock.patch.object(cem, "classify", side_effect=fake)

    def test_alerts_only_on_problem_agent(self):
        with self._patched_classify(), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        self.assertEqual(problems, 1)
        self.assertEqual(malert.call_count, 1)
        self.assertIn("specialist", malert.call_args[0][0])

    def test_dry_run_never_alerts(self):
        with self._patched_classify(), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=True, now=NOW)
        self.assertEqual(problems, 1)
        malert.assert_not_called()

    def test_cooldown_suppresses_repeat(self):
        with self._patched_classify(), \
             mock.patch.object(cem, "_load_state", return_value={"specialist:NO_REFRESH": NOW - 60}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_alert") as malert:
            cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        malert.assert_not_called()


class MainModes(unittest.TestCase):
    def test_default_safe_refuses_no_flag(self):
        self.assertEqual(cem.main([]), 64)

    def test_dry_run_all_exits_zero(self):
        with mock.patch.object(cem, "check", return_value=0):
            self.assertEqual(cem.main(["--all", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
