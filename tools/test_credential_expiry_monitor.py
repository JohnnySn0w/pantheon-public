"""Tests for credential_expiry_monitor.py (Jun-2026 401 outage prevention).

Pure-unit: _read_credential is mocked so no live credential files or Telegram
sends occur. Proves the classifier flags the exact Specialist failure (empty refresh
token) while treating a valid-refresh + near/post access-token expiry as OK (no
nightly false alarms), and that alerting is throttled per (agent, class).
"""
from __future__ import annotations

import json
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


def _cred(refresh="rt-valid", exp_offset_h=5.0, rte_offset_h=None):
    """Build an oauth dict; access token expires exp_offset_h hours from NOW.

    rte_offset_h: if set, add refreshTokenExpiresAt that many hours from NOW (the
    ~30-day refresh cliff). Default None = field absent (pre-cliff-detection creds)."""
    c = {"accessToken": "at", "refreshToken": refresh, "expiresAt": int((NOW + exp_offset_h * HOUR) * 1000)}
    if rte_offset_h is not None:
        c["refreshTokenExpiresAt"] = int((NOW + rte_offset_h * HOUR) * 1000)
    return c


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

    def test_refresh_cliff_within_window_is_near_cliff(self):
        # refresh token expires in 2 days — inside the 3-day warn window (T-2973 cascade)
        cls, detail = self._classify_with("ok", _cred(refresh="rt", rte_offset_h=48))
        self.assertEqual(cls, "NEAR_CLIFF")
        self.assertIn("cliff", detail.lower())

    def test_refresh_cliff_already_passed_is_near_cliff(self):
        # refresh token already expired — the atlas/orchestrator cascade state
        cls, _ = self._classify_with("ok", _cred(refresh="rt", rte_offset_h=-5))
        self.assertEqual(cls, "NEAR_CLIFF")

    def test_refresh_cliff_far_out_is_ok(self):
        # refresh token expires in 10 days — well outside the window
        cls, _ = self._classify_with("ok", _cred(refresh="rt", rte_offset_h=240))
        self.assertEqual(cls, "OK")

    def test_absent_refreshTokenExpiresAt_is_ok(self):
        # pre-cliff-detection creds (no field) must still classify OK — no false cliff
        cls, _ = self._classify_with("ok", _cred(refresh="rt", exp_offset_h=5))
        self.assertEqual(cls, "OK")

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
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        self.assertEqual(problems, 1)
        self.assertEqual(malert.call_count, 1)
        self.assertIn("specialist", malert.call_args[0][0])

    def test_dry_run_never_alerts(self):
        with self._patched_classify(), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=True, now=NOW)
        self.assertEqual(problems, 1)
        malert.assert_not_called()
        mwake.assert_not_called()

    def test_cooldown_suppresses_repeat(self):
        with self._patched_classify(), \
             mock.patch.object(cem, "_load_state", return_value={"specialist:NO_REFRESH": NOW - 60}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        malert.assert_not_called()
        mwake.assert_not_called()

    def test_skip_list_suppresses_alert_for_offline_seat(self):
        # T-2973: an intentionally offline/retired seat (in PANTHEON_CRED_ALERT_SKIP)
        # must NOT alert/wake even with a bad credential, and must not count as a problem.
        with self._patched_classify(), \
             mock.patch.object(cem, "CREDENTIAL_ALERT_SKIP", {"specialist"}), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        malert.assert_not_called()
        mwake.assert_not_called()
        self.assertEqual(problems, 0)  # skipped agent is not a counted problem

    def test_registry_mute_suppresses_alert(self):
        # T-2978: a persona muted via the unified registry (is_muted) is suppressed
        # even with the legacy env skip EMPTY — the registry is the go-forward path.
        with self._patched_classify(), \
             mock.patch.object(cem, "CREDENTIAL_ALERT_SKIP", set()), \
             mock.patch.object(cem, "_persona_is_muted", side_effect=lambda a: a == "specialist"), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        malert.assert_not_called()
        mwake.assert_not_called()
        self.assertEqual(problems, 0)

    def test_registry_mute_does_not_suppress_other_agents(self):
        # Muting specialist via the registry must leave a different bad agent still alerting.
        def fake(agent, cfg, now=None):
            return ("MALFORMED", "no accessToken")
        with mock.patch.object(cem, "classify", side_effect=fake), \
             mock.patch.object(cem, "CREDENTIAL_ALERT_SKIP", set()), \
             mock.patch.object(cem, "_persona_is_muted", side_effect=lambda a: a == "specialist"), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery"), \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        self.assertEqual(malert.call_count, 1)
        self.assertIn("orchestrator", malert.call_args[0][0])
        self.assertEqual(problems, 1)

    def test_skip_list_does_not_suppress_other_agents(self):
        # Skipping specialist must leave a DIFFERENT bad agent still alerting.
        def fake(agent, cfg, now=None):
            return ("MALFORMED", "no accessToken")  # both bad
        with mock.patch.object(cem, "classify", side_effect=fake), \
             mock.patch.object(cem, "CREDENTIAL_ALERT_SKIP", {"specialist"}), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_wake_recovery") as mwake, \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        # only orchestrator alerts; specialist is skipped
        self.assertEqual(malert.call_count, 1)
        self.assertIn("orchestrator", malert.call_args[0][0])
        self.assertEqual(problems, 1)


class WardWake(unittest.TestCase):
    """T-1873: the first (un-throttled) alert also wakes Recovery's mailbox so the
    relogin legwork starts in minutes, not hours. Delivery seam always mocked —
    tests must never write the real ORG-026 hub."""

    def test_wake_fires_alongside_first_operator_alert(self):
        with mock.patch("tools.mailbox.send") as msend:
            cem._wake_recovery("orchestrator", "MALFORMED", "credential no accessToken field")
        args, kwargs = msend.call_args
        self.assertEqual(args[0], "/home/the-ancestor/mailbox/recovery")
        self.assertIn("credential-MALFORMED on orchestrator", args[1])
        self.assertIn("agent-oauth-relogin", args[1])
        self.assertEqual(kwargs["subject"], "auth-dead: orchestrator (MALFORMED)")

    def test_wake_skipped_when_recovery_is_the_dead_agent(self):
        with mock.patch("tools.mailbox.send") as msend:
            cem._wake_recovery("recovery", "NO_REFRESH", "empty refresh")
        msend.assert_not_called()

    def test_wake_failure_is_nonfatal_and_alert_path_survives(self):
        def fake(agent, cfg, now=None):
            return ("NO_REFRESH", "empty refresh") if agent == "specialist" else ("OK", "fine")
        with mock.patch.object(cem, "classify", side_effect=fake), \
             mock.patch.object(cem, "_load_state", return_value={}), \
             mock.patch.object(cem, "_save_state"), \
             mock.patch.object(
                 cem, "CREDENTIALED_ACCOUNTS",
                 {"specialist": "claude-research", "orchestrator": "claude"},
             ), \
             mock.patch("tools.mailbox.send", side_effect=PermissionError("hub")), \
             mock.patch.object(cem, "_track_login_event"), \
             mock.patch.object(cem, "_alert") as malert:
            problems = cem.check(["specialist", "orchestrator"], dry_run=False, now=NOW)
        self.assertEqual(problems, 1)
        self.assertEqual(malert.call_count, 1)


class LoginEventTracking(unittest.TestCase):
    """T-1874: credential-fingerprint changes are recorded as login events so
    auth-deaths can be correlated against fleet login activity."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        for p in [
            mock.patch.object(cem, "STATE_DIR", d),
            mock.patch.object(cem, "STATE_PATH", d / "state.json"),
            mock.patch.object(cem, "LOGIN_EVENTS_PATH", d / "login_events.jsonl"),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _events(self):
        if not cem.LOGIN_EVENTS_PATH.exists():
            return []
        return [json.loads(l) for l in cem.LOGIN_EVENTS_PATH.read_text().splitlines()]

    def test_first_observation_logs_baseline_then_stays_quiet(self):
        oauth = {"accessToken": "a", "refreshToken": "R1", "refreshTokenExpiresAt": 111}
        state = {}
        with mock.patch.object(cem, "_read_credential", return_value=("ok", oauth)):
            cem._track_login_event("orchestrator", {}, NOW, state)
            cem._track_login_event("orchestrator", {}, NOW + 3600, state)
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0]["baseline"])
        self.assertIn("fp:orchestrator", state)

    def test_rotation_logs_event_with_old_and_new(self):
        state = {}
        o1 = {"accessToken": "a", "refreshToken": "R1", "refreshTokenExpiresAt": 111}
        o2 = {"accessToken": "b", "refreshToken": "R2", "refreshTokenExpiresAt": 222}
        with mock.patch.object(cem, "_read_credential", side_effect=[("ok", o1), ("ok", o2)]):
            cem._track_login_event("specialist", {}, NOW, state)
            cem._track_login_event("specialist", {}, NOW + 60, state)
        evs = self._events()
        self.assertEqual(len(evs), 2)
        self.assertFalse(evs[1]["baseline"])
        self.assertNotEqual(evs[1]["old_fp"], evs[1]["new_fp"])

    def test_blanked_writeback_is_fingerprinted_as_blanked(self):
        """The 2026-07-20 orchestrator signature: parseable file, empty tokens."""
        state = {"fp:orchestrator": "abc@111"}
        blanked = {"accessToken": "", "refreshToken": "", "refreshTokenExpiresAt": 999}
        with mock.patch.object(cem, "_read_credential",
                               return_value=("no accessToken field", blanked)):
            cem._track_login_event("orchestrator", {}, NOW, state)
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["new_fp"], "blanked")

    def test_unreadable_cred_neither_logs_nor_clobbers_state(self):
        state = {"fp:orchestrator": "abc@111"}
        with mock.patch.object(cem, "_read_credential",
                               return_value=("unreadable (rc=1: )", None)):
            cem._track_login_event("orchestrator", {}, NOW, state)
        self.assertEqual(self._events(), [])
        self.assertEqual(state["fp:orchestrator"], "abc@111")

    def test_blanked_cred_still_classifies_malformed(self):
        """Guard the classify() contract across the _read_credential change."""
        blanked = {"accessToken": "", "refreshToken": "", "refreshTokenExpiresAt": 999}
        with mock.patch.object(cem, "_read_credential",
                               return_value=("no accessToken field", blanked)):
            cls, detail = cem.classify("orchestrator", {}, now=NOW)
        self.assertEqual(cls, "MALFORMED")
        self.assertIn("no accessToken", detail)


class MainModes(unittest.TestCase):
    def test_default_safe_refuses_no_flag(self):
        self.assertEqual(cem.main([]), 64)

    def test_dry_run_all_exits_zero(self):
        with mock.patch.object(cem, "check", return_value=0):
            self.assertEqual(cem.main(["--all", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
