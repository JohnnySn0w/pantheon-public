"""Tests for memory_snapshot_coverage_monitor.py (T-2838 / T-1871 snapshot rot).

Pure-unit: the systemd + git probe boundaries (_systemctl_show, _git_head_epoch)
are mocked so no live systemd introspection, cross-account sudo, or Telegram sends
occur. Proves the classifier keys on TIMER health (not commit age) — so a healthy
timer on an idle seat with a stale HEAD is HEALTHY (the archivist-2026-08-11 case,
the whole reason for the design) — and flags a genuinely stopped timer as STALLED.
"""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.memory_snapshot_coverage_monitor as m

NOW = 1_700_000_000.0
DAY = 86400.0
HOUR = 3600.0
CFG = {"user": "someuser", "timer": "pantheon-agent-state-snapshot.timer", "repo": "/repo"}


def _show_out(active="active", enabled="enabled", load="loaded", next_dt="2026-08-12 00:00:00"):
    """Build a fake `systemctl show` stdout block."""
    nxt = f"NextElapseUSecRealtime={('Wed ' + next_dt + ' UTC') if next_dt else ''}"
    return f"LoadState={load}\nActiveState={active}\nUnitFileState={enabled}\n{nxt}\n"


def _classify(*, show=None, rc=0, err="", head_epoch=None, now=NOW):
    with mock.patch.object(m, "_systemctl_show", return_value=(rc, show if show is not None else _show_out(), err)), \
         mock.patch.object(m, "_git_head_epoch", return_value=head_epoch):
        return m.classify("seat", CFG, now=now)


class TimestampParsing(unittest.TestCase):
    def test_parses_utc_datetime(self):
        _props, epoch = m._parse_next_elapse(_show_out(next_dt="2026-08-12 00:00:00"))
        # 2026-08-12T00:00:00Z
        import calendar
        self.assertEqual(epoch, calendar.timegm(time.strptime("2026-08-12 00:00:00", "%Y-%m-%d %H:%M:%S")))

    def test_empty_next_elapse_is_none(self):
        _props, epoch = m._parse_next_elapse(_show_out(next_dt=""))
        self.assertIsNone(epoch)

    def test_props_dict_populated(self):
        props, _ = m._parse_next_elapse(_show_out(active="inactive", enabled="disabled"))
        self.assertEqual(props["ActiveState"], "inactive")
        self.assertEqual(props["UnitFileState"], "disabled")


class ClassifyTimerHealth(unittest.TestCase):
    def test_healthy_timer_near_future_fire(self):
        # next fire ~a few hours out
        nxt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW + 6 * HOUR))
        cls, detail = _classify(show=_show_out(next_dt=nxt), head_epoch=int(NOW - 2 * HOUR))
        self.assertEqual(cls, "HEALTHY")
        self.assertIn("active+enabled", detail)

    def test_idle_seat_stale_head_but_healthy_timer_is_HEALTHY(self):
        # THE design case: archivist 2026-08-11 — HEAD 13 days old, timer firing fine.
        nxt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW + 30 * 60))
        cls, _ = _classify(show=_show_out(next_dt=nxt), head_epoch=int(NOW - 13 * DAY))
        self.assertEqual(cls, "HEALTHY")

    def test_inactive_timer_is_stalled(self):
        cls, detail = _classify(show=_show_out(active="inactive"), head_epoch=int(NOW - DAY))
        self.assertEqual(cls, "STALLED")
        self.assertIn("not running", detail)

    def test_disabled_timer_is_stalled(self):
        cls, _ = _classify(show=_show_out(enabled="disabled"), head_epoch=int(NOW - DAY))
        self.assertEqual(cls, "STALLED")

    def test_no_scheduled_fire_is_stalled(self):
        cls, detail = _classify(show=_show_out(next_dt=""), head_epoch=int(NOW - DAY))
        self.assertEqual(cls, "STALLED")
        self.assertIn("NO scheduled", detail)

    def test_next_fire_far_in_past_is_stalled(self):
        nxt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 2 * DAY))
        cls, detail = _classify(show=_show_out(next_dt=nxt), head_epoch=int(NOW - 2 * DAY))
        self.assertEqual(cls, "STALLED")
        self.assertIn("PAST", detail)

    def test_next_fire_within_grace_past_is_healthy(self):
        # just-missed by < grace window → not stalled (about to run)
        nxt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 30 * 60))
        cls, _ = _classify(show=_show_out(next_dt=nxt), head_epoch=int(NOW - HOUR))
        self.assertEqual(cls, "HEALTHY")

    def test_next_fire_absurdly_far_is_misfire_window(self):
        nxt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW + 5 * DAY))
        cls, detail = _classify(show=_show_out(next_dt=nxt), head_epoch=int(NOW - HOUR))
        self.assertEqual(cls, "MISFIRE_WINDOW")
        self.assertIn("mis-scheduled", detail)


class ClassifyFallback(unittest.TestCase):
    def test_systemd_unreadable_recent_commit_is_unverifiable_ok(self):
        cls, detail = _classify(rc=1, err="sudo: a password is required", head_epoch=int(NOW - DAY))
        self.assertEqual(cls, "UNVERIFIABLE_OK")
        self.assertIn("recently alive", detail)

    def test_systemd_unreadable_stale_commit_is_unverifiable_stale(self):
        cls, detail = _classify(rc=1, err="boom", head_epoch=int(NOW - 10 * DAY))
        self.assertEqual(cls, "UNVERIFIABLE_STALE")
        self.assertIn("fallback window", detail)

    def test_systemd_unreadable_no_commit_is_unverifiable_stale(self):
        cls, detail = _classify(rc=1, err="boom", head_epoch=None)
        self.assertEqual(cls, "UNVERIFIABLE_STALE")
        self.assertIn("cannot be confirmed", detail)

    def test_load_not_found_is_fallback(self):
        cls, _ = _classify(show="LoadState=not-found\nActiveState=\nUnitFileState=\n", head_epoch=int(NOW - DAY))
        self.assertEqual(cls, "UNVERIFIABLE_OK")


class AlertThrottle(unittest.TestCase):
    def test_should_alert_respects_cooldown(self):
        state = {}
        self.assertTrue(m._should_alert("archivist", "STALLED", NOW, state))
        state["archivist:STALLED"] = NOW
        self.assertFalse(m._should_alert("archivist", "STALLED", NOW + 60, state))
        self.assertTrue(m._should_alert("archivist", "STALLED", NOW + m.ALERT_COOLDOWN_SECONDS + 1, state))

    def test_alert_classes_are_the_actionable_ones(self):
        self.assertEqual(m.ALERT_CLASSES, {"STALLED", "MISFIRE_WINDOW", "UNVERIFIABLE_STALE"})
        self.assertNotIn("HEALTHY", m.ALERT_CLASSES)
        self.assertNotIn("UNVERIFIABLE_OK", m.ALERT_CLASSES)


class CheckLoop(unittest.TestCase):
    def test_check_alerts_only_on_problem_classes_and_throttles(self):
        alerts = []
        with mock.patch.object(m, "classify", side_effect=[("HEALTHY", "ok"), ("STALLED", "bad")]), \
             mock.patch.object(m, "_alert", side_effect=lambda s, c, d: alerts.append((s, c))), \
             mock.patch.object(m, "_load_state", return_value={}), \
             mock.patch.object(m, "_save_state"):
            problems = m.check(["recovery", "archivist"], dry_run=False, now=NOW)
        self.assertEqual(problems, 1)
        self.assertEqual(alerts, [("archivist", "STALLED")])

    def test_dry_run_sends_no_alert(self):
        with mock.patch.object(m, "classify", return_value=("STALLED", "bad")), \
             mock.patch.object(m, "_alert", side_effect=AssertionError("must not alert in dry-run")), \
             mock.patch.object(m, "_load_state", return_value={}), \
             mock.patch.object(m, "_save_state"):
            problems = m.check(["recovery"], dry_run=True, now=NOW)
        self.assertEqual(problems, 1)


class Cli(unittest.TestCase):
    def test_flagless_run_refuses(self):
        self.assertEqual(m.main([]), 64)

    def test_all_exits_zero_even_with_problems(self):
        with mock.patch.object(m, "check", return_value=3):
            self.assertEqual(m.main(["--all", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
