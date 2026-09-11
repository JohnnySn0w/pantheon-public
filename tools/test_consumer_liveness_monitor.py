"""tests/test_consumer_liveness_monitor.py — T-2605.

The load-bearing tests, in priority order:
  1. FRESHNESS: one exit_epoch, checked at two `now`s, must flip LIVE -> STALLED. This is the
     ticket's sharpest correctness point ("a liveness answer true at T says nothing at T+cadence")
     and the whole reason this is its own mechanism, not a token bolted onto a queue detector.
  2. UNKNOWN never silent-greens, and is streak-gated (a query glitch is not an incident).
  3. NO_TIMER / DISABLED are report-only — this module does not double-page the alarms
     delivery_reachability already owns.
  4. STALLED alerts on threshold cross with NO streak (real timestamp, not a flapping poll).
  5. "could not ask" (rc!=0) is kept distinct from "asked, found nothing" (rc==0, empty).
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import consumer_liveness_monitor as clm

CAD = clm.CADENCE_SECS          # 600
THR = CAD * clm.STALL_MULTIPLE  # 1800
NOW = 1_800_000_000


class FakeOps:
    """Records alerts; feeds scripted enablement + service state per agent."""

    def __init__(self, *, now=NOW, enablement=None, service=None):
        self._now = now
        self._enablement = enablement or {}
        self._service = service or {}
        self.alerts: list[tuple[str, str, str]] = []

    def now(self):
        return self._now

    def timer_enablement(self, account, agent):
        return self._enablement.get(agent)

    def service_last_exit(self, account, agent):
        return self._service.get(agent)

    def alert(self, msg, vocab, agent):
        self.alerts.append((agent, vocab, msg))


SUB = {"orchestrator": "claude", "recovery": "claude-fallback", "atlas": "suborch2"}


def _run(ops, agents, state_dir, **kw):
    return clm.check_all(ops=ops, state_dir=Path(state_dir), agents=agents,
                         substrate=SUB, dry_run=kw.pop("dry_run", False), **kw)


class TestFreshness(unittest.TestCase):
    """(1) The freshness-correlation guard — the reason this module exists as its own thing."""

    def test_same_exit_flips_when_now_advances(self):
        exit_epoch = NOW - 5 * 60  # completed 5 min ago as of NOW
        service = {"recovery": {"exit_epoch": exit_epoch, "result": "success", "active_state": "inactive"}}

        fresh = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                   service=service["recovery"], now=NOW)
        self.assertEqual(fresh["verdict"], clm.LIVE)

        # SAME exit_epoch, checked well past the threshold — must go STALLED with no code change.
        later = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                   service=service["recovery"], now=exit_epoch + THR + 60)
        self.assertEqual(later["verdict"], clm.STALLED)

    def test_boundary_is_strictly_greater_than_threshold(self):
        exit_epoch = NOW - THR  # exactly at threshold -> still LIVE
        at = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                service={"exit_epoch": exit_epoch, "result": "success"}, now=NOW)
        self.assertEqual(at["verdict"], clm.LIVE)
        over = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                  service={"exit_epoch": exit_epoch - 1, "result": "success"}, now=NOW)
        self.assertEqual(over["verdict"], clm.STALLED)


class TestVerdicts(unittest.TestCase):

    def test_live_when_recent(self):
        with TemporaryDirectory() as d:
            ops = FakeOps(enablement={"recovery": "enabled"},
                          service={"recovery": {"exit_epoch": NOW - 60, "result": "success"}})
            res = _run(ops, ["recovery"], d)
            self.assertEqual(res[0]["verdict"], clm.LIVE)
            self.assertEqual(ops.alerts, [])

    def test_stalled_alerts_immediately_no_streak(self):
        with TemporaryDirectory() as d:
            ops = FakeOps(enablement={"recovery": "enabled"},
                          service={"recovery": {"exit_epoch": NOW - THR - 300, "result": "success"}})
            res = _run(ops, ["recovery"], d)
            self.assertEqual(res[0]["verdict"], clm.STALLED)
            self.assertTrue(res[0]["alerted"])
            self.assertEqual(len(ops.alerts), 1)
            self.assertEqual(ops.alerts[0][0], "recovery")

    def test_no_timer_is_report_only(self):
        """delivery_reachability (MISSING) owns this — must NOT alert here (no double-page)."""
        with TemporaryDirectory() as d:
            ops = FakeOps(enablement={"atlas": "missing"})
            res = _run(ops, ["atlas"], d)
            self.assertEqual(res[0]["verdict"], clm.NO_TIMER)
            self.assertFalse(res[0]["alerted"])
            self.assertEqual(ops.alerts, [])

    def test_disabled_is_report_only(self):
        with TemporaryDirectory() as d:
            ops = FakeOps(enablement={"recovery": "disabled"})
            res = _run(ops, ["recovery"], d)
            self.assertEqual(res[0]["verdict"], clm.DISABLED)
            self.assertEqual(ops.alerts, [])


class TestUnknown(unittest.TestCase):

    def test_unmeasurable_is_never_green(self):
        # enablement None == could-not-ask -> UNKNOWN, not LIVE.
        v = clm.classify_agent("recovery", account="claude-fallback", enablement=None,
                               service=None, now=NOW)
        self.assertEqual(v["verdict"], clm.UNKNOWN)

    def test_enabled_but_no_completed_run_is_unknown_not_green(self):
        v = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                               service={"exit_epoch": None, "result": ""}, now=NOW)
        self.assertEqual(v["verdict"], clm.UNKNOWN)

    def test_activation_window_race_is_live_not_unknown(self):
        # T-3099: exit_epoch is transiently None while the oneshot is mid-cycle (systemd clears
        # the prior ExecMainExitTimestamp during activation). A sample caught in that window is
        # a live consumer, NOT a never-fired one — so a running ActiveState must read LIVE, not
        # UNKNOWN. Mutation guard: revert the T-3099 branch and each running case flips to UNKNOWN.
        for active in ("activating", "active", "reloading"):
            v = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                   service={"exit_epoch": None, "result": "",
                                            "active_state": active}, now=NOW)
            self.assertEqual(v["verdict"], clm.LIVE,
                             f"exit=None + ActiveState={active} is a mid-run sample, must be LIVE")

    def test_not_running_with_no_exit_stays_unknown(self):
        # The other half of the T-3099 split: exit=None while NOT running (inactive) is a genuine
        # never-fired/never-completed consumer and must stay UNKNOWN — LIVE cannot mask a real
        # stall. A hung activation that timed out to 'failed' likewise falls through to UNKNOWN.
        for inert in ("inactive", "failed", ""):
            v = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                   service={"exit_epoch": None, "result": "",
                                            "active_state": inert}, now=NOW)
            self.assertEqual(v["verdict"], clm.UNKNOWN,
                             f"exit=None + ActiveState={inert!r} is never-fired, must stay UNKNOWN")

    def test_unknown_streak_gated_then_alerts(self):
        with TemporaryDirectory() as d:
            ops = FakeOps(enablement={"recovery": None})
            # First two checks: UNKNOWN, suppressed.
            for i in range(clm.UNKNOWN_ALERT_STREAK - 1):
                res = _run(ops, ["recovery"], d)
                self.assertEqual(res[0]["verdict"], clm.UNKNOWN)
                self.assertFalse(res[0]["alerted"], f"poll {i} should be suppressed")
            self.assertEqual(ops.alerts, [])
            # The Nth consecutive UNKNOWN pages.
            res = _run(ops, ["recovery"], d)
            self.assertTrue(res[0]["alerted"])
            self.assertEqual(len(ops.alerts), 1)

    def test_non_unknown_resets_streak(self):
        with TemporaryDirectory() as d:
            unknown = FakeOps(enablement={"recovery": None})
            _run(unknown, ["recovery"], d)  # streak 1
            state = json.loads((Path(d) / clm._STATE_NAME).read_text())
            self.assertEqual(state["recovery"]["unknown_streak"], 1)
            # A LIVE reading resets the streak to 0.
            live = FakeOps(enablement={"recovery": "enabled"},
                           service={"recovery": {"exit_epoch": NOW - 60, "result": "success"}})
            _run(live, ["recovery"], d)
            state = json.loads((Path(d) / clm._STATE_NAME).read_text())
            self.assertEqual(state["recovery"]["unknown_streak"], 0)


class TestCooldownAndMute(unittest.TestCase):

    def test_repeat_stalled_throttled_by_cooldown(self):
        with TemporaryDirectory() as d:
            svc = {"recovery": {"exit_epoch": NOW - THR - 300, "result": "success"}}
            ops = FakeOps(enablement={"recovery": "enabled"}, service=svc)
            _run(ops, ["recovery"], d, cooldown_secs=6 * 3600)
            self.assertEqual(len(ops.alerts), 1)
            # Same verdict again within cooldown -> throttled.
            _run(ops, ["recovery"], d, cooldown_secs=6 * 3600)
            self.assertEqual(len(ops.alerts), 1)

    def test_mute_file_silences_but_expires(self):
        with TemporaryDirectory() as d:
            (Path(d) / clm._DISABLE_NAME).write_text(json.dumps(
                {"reason": "planned pve2 window", "until": "2999-01-01T00:00:00Z"}))
            ops = FakeOps(enablement={"recovery": "enabled"},
                          service={"recovery": {"exit_epoch": NOW - THR - 999, "result": "success"}})
            res = _run(ops, ["recovery"], d)
            self.assertEqual(res[0]["verdict"], clm.DISABLED)
            self.assertEqual(ops.alerts, [])

    def test_expired_mute_does_not_apply(self):
        with TemporaryDirectory() as d:
            (Path(d) / clm._DISABLE_NAME).write_text(json.dumps(
                {"reason": "old window", "until": "2000-01-01T00:00:00Z"}))
            ops = FakeOps(enablement={"recovery": "enabled"},
                          service={"recovery": {"exit_epoch": NOW - THR - 999, "result": "success"}})
            res = _run(ops, ["recovery"], d)
            self.assertEqual(res[0]["verdict"], clm.STALLED)  # lapsed mute -> monitor speaks


class TestTimestampParse(unittest.TestCase):

    def test_unix_at_form(self):
        self.assertEqual(clm._parse_systemd_ts("@1786294929"), 1786294929)

    def test_human_utc_form(self):
        # 'Sun 2026-08-09 17:02:09 UTC' == epoch 1786294929
        self.assertEqual(clm._parse_systemd_ts("Sun 2026-08-09 17:02:09 UTC"), 1786294929)

    def test_never_and_garbage_are_none(self):
        for v in ("", "n/a", "0", "not-a-date", "@notanumber"):
            self.assertIsNone(clm._parse_systemd_ts(v), v)


class TestSeatRoster(unittest.TestCase):

    def test_empty_roster_is_unknown_not_empty(self):
        with TemporaryDirectory() as d:
            ops = FakeOps()
            res = clm.check_all(ops=ops, state_dir=Path(d), agents=[], substrate=SUB)
            self.assertEqual(res[0]["verdict"], clm.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
