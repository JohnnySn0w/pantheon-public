"""Tests for disk_usage_monitor.py (2026-06-26 fleet-wedge-on-full prevention).

Pure-unit: read_usage and _alert are mocked so no live df dependency or Telegram
sends occur. Proves the WARN/URGENT thresholds, that OK never alerts, that alerting
is throttled per class, that dry-run never sends, and that a flagless run refuses.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.disk_usage_monitor as dum

NOW = 1_000_000_000.0
TB = 1024 ** 4


class Classify(unittest.TestCase):
    def test_ok_below_warn(self):
        self.assertEqual(dum.classify(dum.WARN_PCT - 1), "OK")

    def test_warn_at_threshold(self):
        self.assertEqual(dum.classify(dum.WARN_PCT), "WARN")

    def test_warn_band(self):
        self.assertEqual(dum.classify((dum.WARN_PCT + dum.URGENT_PCT) // 2), "WARN")

    def test_urgent_at_threshold(self):
        self.assertEqual(dum.classify(dum.URGENT_PCT), "URGENT")

    def test_urgent_above(self):
        self.assertEqual(dum.classify(99), "URGENT")


class AlertThrottle(unittest.TestCase):
    def test_should_alert_respects_cooldown(self):
        state = {}
        self.assertTrue(dum._should_alert("WARN", NOW, state))
        state["WARN"] = NOW
        self.assertFalse(dum._should_alert("WARN", NOW + 60, state))
        self.assertTrue(dum._should_alert("WARN", NOW + dum.ALERT_COOLDOWN_SECONDS + 1, state))


class CheckLoop(unittest.TestCase):
    def _run(self, used_pct, *, dry_run=False, state=None):
        # 1TB disk at the given used-percent.
        used = int(TB * used_pct / 100)
        usage = (TB, used, TB - used, float(used_pct))
        with mock.patch.object(dum, "read_usage", return_value=usage), \
             mock.patch.object(dum, "_alert") as alert, \
             mock.patch.object(dum, "_wake_recovery") as wake, \
             mock.patch.object(dum, "_top_consumers", return_value="claude:76G"), \
             mock.patch.object(dum, "_load_state", return_value=(state or {})), \
             mock.patch.object(dum, "_save_state"):
            rc = dum.check(dry_run=dry_run, now=NOW)
        return rc, alert, wake

    def test_ok_never_alerts(self):
        rc, alert, wake = self._run(50)
        self.assertEqual(rc, 0)
        alert.assert_not_called()
        wake.assert_not_called()

    def test_warn_alerts(self):
        rc, alert, wake = self._run(dum.WARN_PCT + 1)
        self.assertEqual(rc, 1)
        alert.assert_called_once()
        body = alert.call_args[0][0]
        self.assertIn("disk-warn", body)
        self.assertIn("claude:76G", body)
        # 2026-07-04: the breach also self-wakes recovery (same throttle as the alert).
        wake.assert_called_once_with("disk-warn", body)

    def test_urgent_alerts(self):
        rc, alert, wake = self._run(dum.URGENT_PCT + 1)
        self.assertEqual(rc, 1)
        alert.assert_called_once()
        self.assertIn("disk-urgent", alert.call_args[0][0])
        wake.assert_called_once()

    def test_dry_run_breach_does_not_alert(self):
        rc, alert, wake = self._run(99, dry_run=True)
        self.assertEqual(rc, 1)
        alert.assert_not_called()
        wake.assert_not_called()

    def test_breach_suppressed_within_cooldown(self):
        rc, alert, wake = self._run(99, state={"URGENT": NOW})
        self.assertEqual(rc, 1)
        alert.assert_not_called()
        wake.assert_not_called()


class WakeRecovery(unittest.TestCase):
    def test_wake_sends_to_recovery_mailbox(self):
        with mock.patch.object(dum, "_log"), \
             mock.patch("tools.mailbox.send") as send:
            dum._wake_recovery("disk-warn", "body text")
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args[0], "/home/post-office/mailbox/recovery")
        self.assertIn("body text", args[1])
        self.assertEqual(args[2], "recovery")
        self.assertIn("disk-warn", kwargs["subject"])

    def test_wake_failure_is_nonfatal(self):
        # A mailbox error must never break the alert path (fail-soft).
        with mock.patch.object(dum, "_log") as log, \
             mock.patch("tools.mailbox.send", side_effect=OSError("mailbox down")):
            dum._wake_recovery("disk-urgent", "body")  # must not raise
        self.assertTrue(any("non-fatal" in str(c) for c in log.call_args_list))


class TopConsumers(unittest.TestCase):
    def setUp(self):
        # These cases size homes only; the non-home paths have their own tests.
        p = mock.patch.object(dum, "EXTRA_PATHS", [])
        p.start()
        self.addCleanup(p.stop)

    def test_homes_are_sized_concurrently(self):
        # T-4678: the sweep was sequential, so one slow home (claude, ~42s live)
        # ate the budget and every later home went ?timeout/?skipped. Four homes
        # each taking 0.6s must all be measured inside a 1s budget.
        import time as _t

        def fake_run(cmd, **kw):
            _t.sleep(0.6)
            return mock.Mock(returncode=0, stdout="1.0G\t" + cmd[-1] + "\n")

        with mock.patch.object(dum, "AGENT_HOMES", ["a", "b", "c", "d"]), \
             mock.patch.object(dum, "DU_TOTAL_BUDGET_SEC", 1), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run):
            out = dum._top_consumers(limit=10)
        self.assertNotIn("?", out)
        for h in ("a", "b", "c", "d"):
            self.assertIn(f"{h}:1.0G", out)

    def test_du_running_past_budget_is_timeout_marked(self):
        # A du still running when the shared budget expires is marked, not waited on.
        import time as _t

        def fake_run(cmd, **kw):
            if cmd[-1].endswith("/slow"):
                _t.sleep(1.5)
            return mock.Mock(returncode=0, stdout="2.0G\t" + cmd[-1] + "\n")

        with mock.patch.object(dum, "AGENT_HOMES", ["slow", "fast"]), \
             mock.patch.object(dum, "DU_TOTAL_BUDGET_SEC", 0.5), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run):
            out = dum._top_consumers()
        self.assertIn("fast:2.0G", out)
        self.assertIn("slow:?timeout", out)

    def test_extra_paths_are_in_the_breakdown(self):
        # T-4678: the shared echo cargo target (21G on 2026-10-07) belongs to no
        # home, so a homes-only breakdown could never name it.
        def fake_run(cmd, **kw):
            size = "21G" if cmd[-1] == "/var/lib/pantheon/echo-target" else "1.0G"
            return mock.Mock(returncode=0, stdout=size + "\t" + cmd[-1] + "\n")

        with mock.patch.object(dum, "AGENT_HOMES", ["claude"]), \
             mock.patch.object(dum, "EXTRA_PATHS", ["/var/lib/pantheon/echo-target"]), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run) as run:
            out = dum._top_consumers()
        self.assertTrue(out.startswith("/var/lib/pantheon/echo-target:21G"), out)
        extra_cmd = [c.args[0] for c in run.call_args_list
                     if c.args[0][-1] == "/var/lib/pantheon/echo-target"][0]
        self.assertEqual(extra_cmd[0], "du")  # sized as the running user, no sudo

    def test_default_extra_paths_cover_both_echo_target_roots(self):
        # T-4796: the 10-09 warn's breakdown omitted echo-evidence-targets, which
        # held the 16.6G that caused it.
        defaults = dum.DEFAULT_EXTRA_PATHS.split(":")
        self.assertIn("/var/lib/pantheon/echo-target", defaults)
        self.assertIn("/var/lib/pantheon/echo-evidence-targets", defaults)

    def test_never_raises_on_failure(self):
        # Even if every du subprocess errors, must not propagate (fail-soft: the core
        # df check never depends on cross-account du). Post-T-3441 the errored homes
        # are MARKED rather than dropped, so the result is non-empty markers, not ''.
        with mock.patch.object(dum, "AGENT_HOMES", ["claude", "recovery"]), \
             mock.patch.object(dum.subprocess, "run", side_effect=OSError("boom")):
            out = dum._top_consumers()  # must not raise
        self.assertIn("claude:?err", out)
        self.assertIn("recovery:?err", out)

    def test_du_timeout_marks_home_not_dropped(self):
        # Regression (T-3441): a home whose du TIMES OUT must appear MARKED, never
        # silently absent. The 2026-09-06 miss: claude (69.5G) vanished from the
        # breakdown because its du exceeded the per-home timeout, so the live alert
        # named a 6x-smaller home as the top consumer and pointed triage at it.
        import subprocess as _sp
        with mock.patch.object(dum, "AGENT_HOMES", ["claude", "recovery"]), \
             mock.patch.object(dum.subprocess, "run",
                               side_effect=_sp.TimeoutExpired(cmd=["du"], timeout=1)):
            out = dum._top_consumers()
        self.assertIn("claude", out)
        self.assertIn("?timeout", out)

    def test_measured_and_unmeasured_both_present(self):
        # A sized home and a timed-out home in the same sweep: the sized one shows its
        # size, the timed-out one is marked AND present (never truncated out by the
        # top-N limit even though a marker sorts below every real size).
        import subprocess as _sp

        def fake_run(cmd, **kw):
            home = cmd[-1]  # ".../home/<name>"
            if home.endswith("/claude"):
                raise _sp.TimeoutExpired(cmd=cmd, timeout=1)
            return mock.Mock(returncode=0, stdout="3.0G\t" + home + "\n")

        with mock.patch.object(dum, "AGENT_HOMES", ["claude", "recovery"]), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run):
            out = dum._top_consumers()
        self.assertIn("recovery:3.0G", out)
        self.assertIn("claude:?timeout", out)

    def test_nonzero_rc_with_valid_total_is_measured(self):
        # The REAL 2026-09-06 miss (T-3441): du exits NONZERO from an unreadable
        # subdir (root-owned rootless-podman overlay dirs in the claude/claude-research
        # /codex homes) but still prints a valid grand total. That total must be USED,
        # not discarded for rc!=0 — the old `returncode == 0` guard dropped the biggest
        # home on every single run, which is what hid claude (verified live 2026-09-06).
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=1, stdout="38G\t" + cmd[-1] + "\n")
        with mock.patch.object(dum, "AGENT_HOMES", ["claude"]), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run):
            out = dum._top_consumers()
        self.assertIn("claude:38G", out)
        self.assertNotIn("?err", out)

    def test_no_stdout_is_err_marked(self):
        # A genuine failure — nonzero rc AND no total on stdout (sudo denied, path
        # gone) — is marked ?err, still never dropped.
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=1, stdout="")
        with mock.patch.object(dum, "AGENT_HOMES", ["claude"]), \
             mock.patch.object(dum.subprocess, "run", side_effect=fake_run):
            out = dum._top_consumers()
        self.assertEqual(out, "claude:?err")

    def test_total_budget_bounds_walltime(self):
        # The whole sweep must fit inside the service TimeoutStartSec (2min): with a
        # shared budget, once it is spent the remaining homes are marked ?skipped
        # rather than each waiting its own per-home timeout (8x60s would blow 2min).
        self.assertLessEqual(dum.DU_TOTAL_BUDGET_SEC, 110)
        self.assertLessEqual(dum.DU_PER_HOME_TIMEOUT_SEC, dum.DU_TOTAL_BUDGET_SEC)


class RamCpuClassifiers(unittest.TestCase):
    def test_ram_floors(self):
        MB = 1024 ** 2
        self.assertEqual(dum.classify_ram(dum.RAM_WARN_MB * MB + 1), "OK")
        self.assertEqual(dum.classify_ram(dum.RAM_WARN_MB * MB - 1), "WARN")
        self.assertEqual(dum.classify_ram(dum.RAM_URGENT_MB * MB - 1), "URGENT")

    def test_cpu_threshold(self):
        line = dum.CPU_LOAD15_WARN_MULT * 4
        self.assertEqual(dum.classify_cpu(1.0, 4), "OK")
        self.assertEqual(dum.classify_cpu(line, 4), "OK")      # exactly at line = not over
        self.assertEqual(dum.classify_cpu(line + 0.1, 4), "WARN")

    def test_readers_return_sane_values(self):
        total, avail = dum.read_meminfo()
        self.assertGreater(total, 0)
        self.assertGreaterEqual(total, avail)
        load15, cores = dum.read_load15_and_cores()
        self.assertGreaterEqual(load15, 0.0)
        self.assertGreaterEqual(cores, 1)


class RamCpuCheckLoops(unittest.TestCase):
    def _ram(self, avail_mb, *, dry_run=False, state=None):
        usage = (8 * 1024 ** 3, avail_mb * 1024 ** 2)
        with mock.patch.object(dum, "read_meminfo", return_value=usage), \
             mock.patch.object(dum, "_alert") as alert, \
             mock.patch.object(dum, "_wake_recovery") as wake, \
             mock.patch.object(dum, "_top_processes", return_value="claude 1 2 3 cargo"), \
             mock.patch.object(dum, "_load_state", return_value=(state or {})), \
             mock.patch.object(dum, "_save_state"):
            rc = dum.check_ram(dry_run=dry_run, now=NOW)
        return rc, alert, wake

    def _cpu(self, load15, *, state=None):
        with mock.patch.object(dum, "read_load15_and_cores", return_value=(load15, 4)), \
             mock.patch.object(dum, "_alert") as alert, \
             mock.patch.object(dum, "_wake_recovery") as wake, \
             mock.patch.object(dum, "_top_processes", return_value="claude 1 99 2 bun"), \
             mock.patch.object(dum, "_load_state", return_value=(state or {})), \
             mock.patch.object(dum, "_save_state"):
            rc = dum.check_cpu(dry_run=False, now=NOW)
        return rc, alert, wake

    def test_ram_ok_no_alert(self):
        rc, alert, wake = self._ram(4096)
        self.assertEqual(rc, 0)
        alert.assert_not_called()
        wake.assert_not_called()

    def test_ram_warn_alerts_and_wakes(self):
        rc, alert, wake = self._ram(800)
        self.assertEqual(rc, 1)
        alert.assert_called_once()
        self.assertIn("ram-warn", alert.call_args[0][0])
        wake.assert_called_once()
        self.assertEqual(wake.call_args[0][0], "ram-warn")

    def test_ram_urgent_alerts(self):
        rc, alert, _ = self._ram(300)
        self.assertEqual(rc, 1)
        self.assertIn("ram-urgent", alert.call_args[0][0])

    def test_ram_cooldown_suppresses(self):
        rc, alert, wake = self._ram(800, state={"ram_WARN": NOW})
        self.assertEqual(rc, 1)
        alert.assert_not_called()
        wake.assert_not_called()

    def test_ram_dry_run_no_alert(self):
        rc, alert, wake = self._ram(300, dry_run=True)
        self.assertEqual(rc, 1)
        alert.assert_not_called()

    def test_cpu_ok_no_alert(self):
        rc, alert, _ = self._cpu(2.0)
        self.assertEqual(rc, 0)
        alert.assert_not_called()

    def test_cpu_warn_alerts_and_wakes(self):
        rc, alert, wake = self._cpu(dum.CPU_LOAD15_WARN_MULT * 4 + 1)
        self.assertEqual(rc, 1)
        self.assertIn("cpu-warn", alert.call_args[0][0])
        wake.assert_called_once()
        self.assertEqual(wake.call_args[0][0], "cpu-warn")

    def test_cpu_cooldown_suppresses(self):
        rc, alert, _ = self._cpu(dum.CPU_LOAD15_WARN_MULT * 4 + 1, state={"cpu_WARN": NOW})
        self.assertEqual(rc, 1)
        alert.assert_not_called()


class RunAllIsolation(unittest.TestCase):
    def test_one_leg_failure_does_not_silence_others(self):
        # disk leg raising must not stop ram/cpu legs (fail-open per leg).
        with mock.patch.object(dum, "check", side_effect=OSError("df broke")), \
             mock.patch.object(dum, "check_ram", return_value=0) as ram, \
             mock.patch.object(dum, "check_cpu", return_value=0) as cpu:
            dum.run_all(dry_run=True)  # must not raise
        ram.assert_called_once()
        cpu.assert_called_once()


class MainModes(unittest.TestCase):
    def test_flagless_refuses(self):
        self.assertEqual(dum.main([]), 64)

    def test_dry_run_runs(self):
        with mock.patch.object(dum, "run_all", return_value=None) as chk:
            self.assertEqual(dum.main(["--dry-run"]), 0)
            chk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
