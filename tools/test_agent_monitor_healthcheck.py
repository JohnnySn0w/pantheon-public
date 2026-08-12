#!/usr/bin/env python3
"""Tests for tools/agent_monitor_healthcheck.py (T-1885).

`probe_healthcheck` (the network call) and `_restart_service` (the subprocess
call) are mocked at their module boundary; `_alert_operator` is mocked so no
live Telegram send can leak. State lives under a TemporaryDirectory via
PANTHEON_STATE_DIR — never touches the real ~/.local/state/pantheon/.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tools import agent_monitor_healthcheck as amh


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_patch = mock.patch.dict(amh.os.environ, {"PANTHEON_STATE_DIR": self._tmp.name})
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        # This class exercises the state-machine (cooldown/escalation/reset), not
        # the ship-dark gate itself — arm it by default so the pre-existing
        # restart-path assertions keep testing what they were built to test.
        # RemediateGateTest below covers the gate in isolation, both states.
        self._gate_patch = mock.patch.object(amh, "REMEDIATE_ENABLED", True)
        self._gate_patch.start()
        self.addCleanup(self._gate_patch.stop)

    def _run(self, statuses, *, dry_run=False, now=10_000, restart_ok=True):
        """Run `check()` once per status in `statuses` (each call crosses a fresh
        'timer fire' the way the real oneshot does — the counter lives in the
        state file, not in-process). Returns (restart_calls, alert_calls)."""
        with mock.patch.object(amh, "probe_healthcheck") as probe, \
             mock.patch.object(amh, "_restart_service", return_value=(restart_ok, "ok")) as restart, \
             mock.patch.object(amh, "_alert_operator") as alert:
            for status in statuses:
                probe.return_value = (status, "detail")
                amh.check(dry_run=dry_run, now=now)
        return restart, alert

    def test_healthy_no_restart(self):
        restart, alert = self._run(["HEALTHY"])
        restart.assert_not_called()
        alert.assert_not_called()

    def test_single_blip_no_restart(self):
        # Below N=3 consecutive — must not restart on a lone stall.
        restart, alert = self._run(["STALLED"])
        restart.assert_not_called()
        alert.assert_not_called()

    def test_two_consecutive_still_no_restart(self):
        restart, _ = self._run(["STALLED", "STALLED"])
        restart.assert_not_called()

    def test_n_consecutive_triggers_restart(self):
        restart, _ = self._run(["STALLED", "STALLED", "STALLED"])
        restart.assert_called_once()

    def test_healthy_resets_consecutive_counter(self):
        # Two stalls, then a healthy read, then one more stall — must NOT sum to
        # 3 across the reset; the healthy probe must zero the counter.
        restart, _ = self._run(["STALLED", "STALLED", "HEALTHY", "STALLED"])
        restart.assert_not_called()

    def test_single_restart_does_not_alert(self):
        # A lone confirmed-stall restart is routine (the mechanism working) —
        # per HO disposition, only the SECOND restart-within-window escalates.
        _, alert = self._run(["STALLED", "STALLED", "STALLED"])
        alert.assert_not_called()

    def test_second_restart_within_window_escalates_alert(self):
        # First confirmed stall -> restart #1 (no alert). Advance past the
        # per-restart cooldown but stay inside the 2x-cooldown escalation
        # window, confirm a second stall -> restart #2 -> escalate.
        cooldown = amh.RESTART_COOLDOWN_SECONDS
        with mock.patch.object(amh, "probe_healthcheck") as probe, \
             mock.patch.object(amh, "_restart_service", return_value=(True, "ok")) as restart, \
             mock.patch.object(amh, "_alert_operator") as alert:
            probe.return_value = ("STALLED", "d")
            amh.check(dry_run=False, now=10_000)
            amh.check(dry_run=False, now=10_000)
            amh.check(dry_run=False, now=10_000)  # restart #1
            probe.return_value = ("STALLED", "d")
            amh.check(dry_run=False, now=10_000 + cooldown + 1)
            amh.check(dry_run=False, now=10_000 + cooldown + 1)
            amh.check(dry_run=False, now=10_000 + cooldown + 1)  # restart #2 -> escalate
        self.assertEqual(restart.call_count, 2)
        alert.assert_called_once()

    def test_restart_cooldown_blocks_refire(self):
        # A confirmed stall inside the cooldown window must not re-fire the
        # restart command (would restart-storm on a slow recovery).
        with mock.patch.object(amh, "probe_healthcheck") as probe, \
             mock.patch.object(amh, "_restart_service", return_value=(True, "ok")) as restart:
            probe.return_value = ("STALLED", "d")
            amh.check(dry_run=False, now=10_000)
            amh.check(dry_run=False, now=10_000)
            amh.check(dry_run=False, now=10_000)  # restart #1
            probe.return_value = ("STALLED", "d")
            amh.check(dry_run=False, now=10_000 + 5)
            amh.check(dry_run=False, now=10_000 + 5)
            amh.check(dry_run=False, now=10_000 + 5)  # still within cooldown -> no re-fire
        self.assertEqual(restart.call_count, 1)

    def test_dry_run_never_restarts(self):
        restart, alert = self._run(["STALLED", "STALLED", "STALLED"], dry_run=True)
        restart.assert_not_called()
        alert.assert_not_called()

    def test_restart_recorded_in_state_and_resets_stall_counter(self):
        self._run(["STALLED", "STALLED", "STALLED"])
        state = json.loads((Path(self._tmp.name) / "agent_monitor_healthcheck.json").read_text())
        self.assertEqual(state["consecutive_stalls"], 0)
        self.assertEqual(state["last_restart_ts"], 10_000)
        self.assertEqual(state["consecutive_restarts"], 1)

    def test_restart_forensics_jsonl_appended(self):
        self._run(["STALLED", "STALLED", "STALLED"])
        log_path = Path(self._tmp.name) / "agent_monitor_healthcheck_restarts.jsonl"
        self.assertTrue(log_path.exists())
        line = json.loads(log_path.read_text().strip().splitlines()[-1])
        self.assertEqual(line["ts"], 10_000)
        self.assertIn("detail", line)

    def test_failed_restart_dispatch_not_recorded_as_cooldown(self):
        # A FAILED restart command (systemctl error) must not block an
        # immediate retry on the next poll — no cooldown state written.
        restart, _ = self._run(["STALLED", "STALLED", "STALLED"], restart_ok=False)
        restart.assert_called_once()
        state = json.loads((Path(self._tmp.name) / "agent_monitor_healthcheck.json").read_text())
        self.assertNotIn("last_restart_ts", state)

    def test_failed_restart_dispatch_escalates(self):
        # T-2084: a restart that FAILS to dispatch is the genuinely urgent case
        # — remediation is broken. Before T-2084 it alerted nobody at all.
        _, alert = self._run(["STALLED", "STALLED", "STALLED"], restart_ok=False)
        alert.assert_called_once()
        self.assertIn("FAILED to restart", alert.call_args[0][0])

    def test_failed_restart_alert_rate_limited_within_cooldown(self):
        # A failed dispatch deliberately writes no cooldown, so the next poll
        # retries immediately. The ALERT must not follow that cadence.
        with mock.patch.object(amh, "probe_healthcheck", return_value=("STALLED", "d")), \
             mock.patch.object(amh, "_restart_service", return_value=(False, "boom")), \
             mock.patch.object(amh, "_alert_operator") as alert:
            for _ in range(6):  # two full trips past the N=3 threshold
                amh.check(dry_run=False, now=10_000)
        alert.assert_called_once()

    def test_recurrence_alert_does_not_claim_remediation_failed(self):
        # The restarts here all SUCCEED; the wedge just recurs. The pre-T-2084
        # wording ("the watchdog restart is NOT clearing the wedge") read as an
        # outage and cost an operator look that wasn't needed.
        cooldown = amh.RESTART_COOLDOWN_SECONDS
        with mock.patch.object(amh, "probe_healthcheck", return_value=("STALLED", "d")), \
             mock.patch.object(amh, "_restart_service", return_value=(True, "ok")), \
             mock.patch.object(amh, "_alert_operator") as alert:
            for _ in range(3):
                amh.check(dry_run=False, now=10_000)
            for _ in range(3):
                amh.check(dry_run=False, now=10_000 + cooldown + 1)
        alert.assert_called_once()
        msg = alert.call_args[0][0]
        self.assertNotIn("NOT clearing", msg)
        self.assertIn("SUCCEEDED", msg)


class RemediateGateTest(unittest.TestCase):
    """SHIP-DARK gate (T-1885 REBOUND round 1): matches ancestor_liveness_monitor's
    / channel_liveness_monitor's PANTHEON_*_REMEDIATE_ENABLED shape — default OFF,
    only the live `systemctl restart` is gated (detection/counter/dry-run unaffected)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_patch = mock.patch.dict(amh.os.environ, {"PANTHEON_STATE_DIR": self._tmp.name})
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def _run_to_threshold(self, *, enabled: bool, now: int = 10_000):
        with mock.patch.object(amh, "probe_healthcheck", return_value=("STALLED", "d")), \
             mock.patch.object(amh, "_restart_service", return_value=(True, "ok")) as restart, \
             mock.patch.object(amh, "_alert_operator") as alert, \
             mock.patch.object(amh, "REMEDIATE_ENABLED", enabled):
            amh.check(dry_run=False, now=now)
            amh.check(dry_run=False, now=now)
            amh.check(dry_run=False, now=now)  # crosses STALL_CONSECUTIVE_THRESHOLD
        return restart, alert

    def test_gate_off_default_no_restart_even_at_threshold(self):
        # Default posture (flag unset -> REMEDIATE_ENABLED False): detection
        # confirms the wedge but the live restart must NOT fire.
        restart, alert = self._run_to_threshold(enabled=False)
        restart.assert_not_called()
        alert.assert_not_called()

    def test_gate_off_leaves_no_restart_state(self):
        self._run_to_threshold(enabled=False)
        state = json.loads((Path(self._tmp.name) / "agent_monitor_healthcheck.json").read_text())
        self.assertNotIn("last_restart_ts", state)

    def test_gate_on_restarts_as_built(self):
        restart, _ = self._run_to_threshold(enabled=True)
        restart.assert_called_once()

    def test_env_var_default_is_off(self):
        # No PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED set anywhere in this process
        # -> module-level REMEDIATE_ENABLED reads False, matching the sibling
        # monitors' default-OFF ship-dark posture.
        self.assertFalse(amh.REMEDIATE_ENABLED)


class ProbeHealthcheckTest(unittest.TestCase):
    def test_healthy_on_200(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch.object(amh.urllib.request, "urlopen", return_value=resp):
            status, detail = amh.probe_healthcheck("http://127.0.0.1:4820/", 5)
        self.assertEqual(status, "HEALTHY")

    def test_stalled_on_non_200(self):
        resp = mock.MagicMock()
        resp.status = 503
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch.object(amh.urllib.request, "urlopen", return_value=resp):
            status, detail = amh.probe_healthcheck("http://127.0.0.1:4820/", 5)
        self.assertEqual(status, "STALLED")
        self.assertIn("503", detail)

    def test_stalled_on_timeout(self):
        import socket
        with mock.patch.object(amh.urllib.request, "urlopen", side_effect=socket.timeout("timed out")):
            status, detail = amh.probe_healthcheck("http://127.0.0.1:4820/", 5)
        self.assertEqual(status, "STALLED")

    def test_stalled_on_connection_error(self):
        import urllib.error
        with mock.patch.object(amh.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            status, detail = amh.probe_healthcheck("http://127.0.0.1:4820/", 5)
        self.assertEqual(status, "STALLED")


class CliTest(unittest.TestCase):
    def test_refuses_flagless_run(self):
        # Default-safe (T-0261): no --check/--dry-run must refuse, not silently run.
        rc = amh.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_flag_accepted(self):
        with mock.patch.object(amh, "check") as chk:
            rc = amh.main(["--dry-run"])
        self.assertEqual(rc, 0)
        chk.assert_called_once()
        self.assertTrue(chk.call_args.kwargs.get("dry_run"))

    def test_check_flag_accepted(self):
        with mock.patch.object(amh, "check") as chk:
            rc = amh.main(["--check"])
        self.assertEqual(rc, 0)
        chk.assert_called_once()
        self.assertFalse(chk.call_args.kwargs.get("dry_run"))


class BootGraceT2045(unittest.TestCase):
    """T-2045: a stall probed within BOOT_GRACE_SECONDS of the monitored unit's
    own activation is the documented ~8s post-boot git-remote-check transient
    and must contribute NO evidence toward the N-consecutive restart trigger.
    Falsified at landing (recorded in the resolve note): with the guard disabled
    (grace=0) the within-grace scenario restarts — the suppression is
    load-bearing, not decorative."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_patch = mock.patch.dict(amh.os.environ, {"PANTHEON_STATE_DIR": self._tmp.name})
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self._gate_patch = mock.patch.object(amh, "REMEDIATE_ENABLED", True)
        self._gate_patch.start()
        self.addCleanup(self._gate_patch.stop)

    def _stalls(self, n, *, started, now=10_000):
        with mock.patch.object(amh, "probe_healthcheck", return_value=("STALLED", "detail")), \
             mock.patch.object(amh, "_restart_service", return_value=(True, "ok")) as restart, \
             mock.patch.object(amh, "_alert_operator"), \
             mock.patch.object(amh, "_service_active_enter_ts", return_value=started):
            for _ in range(n):
                amh.check(now=now)
        return restart

    def test_stalls_within_boot_grace_never_count(self):
        # Unit activated 5s ago — well inside the 30s grace. Even THRESHOLD
        # consecutive stalls must not restart, and the counter must stay 0
        # (suppressed upstream of counting, not merely at the decision).
        restart = self._stalls(amh.STALL_CONSECUTIVE_THRESHOLD, started=10_000 - 5)
        restart.assert_not_called()
        self.assertEqual(int(amh._load_state().get("consecutive_stalls", 0)), 0)

    def test_stalls_outside_boot_grace_count_and_restart(self):
        # Green control for the falsification: same scenario, unit activated
        # long ago — the trigger works exactly as before the hardening.
        restart = self._stalls(amh.STALL_CONSECUTIVE_THRESHOLD, started=10_000 - 300)
        restart.assert_called_once()

    def test_unknown_activation_time_fails_open(self):
        # An unreadable clock must NOT suppress evidence: None = not-in-grace,
        # detection proceeds. (A detector's suppression must be affirmatively
        # justified — same posture as _flock_held / the T-2257 lesson.)
        restart = self._stalls(amh.STALL_CONSECUTIVE_THRESHOLD, started=None)
        restart.assert_called_once()

    def test_active_enter_parse(self):
        fake = SimpleNamespace(
            returncode=0, stdout="ActiveEnterTimestamp=Tue 2026-07-29 04:12:03 UTC\n", stderr="")
        with mock.patch.object(amh.subprocess, "run", return_value=fake):
            ts = amh._service_active_enter_ts()
        self.assertIsInstance(ts, int)
        # mktime interprets the struct in LOCAL time, which is correct here:
        # systemctl renders in the box's local zone, and `now` comes from the
        # same box's clock — both sides share whatever zone the host runs.
        self.assertGreater(ts, 1_700_000_000)

    def test_active_enter_empty_value_is_none(self):
        fake = SimpleNamespace(returncode=0, stdout="ActiveEnterTimestamp=\n", stderr="")
        with mock.patch.object(amh.subprocess, "run", return_value=fake):
            self.assertIsNone(amh._service_active_enter_ts())


if __name__ == "__main__":
    unittest.main()
