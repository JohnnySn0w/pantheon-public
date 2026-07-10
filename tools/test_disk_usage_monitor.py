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


class WakeWard(unittest.TestCase):
    def test_wake_sends_to_recovery_mailbox(self):
        with mock.patch.object(dum, "_log"), \
             mock.patch("tools.mailbox.send") as send:
            dum._wake_recovery("disk-warn", "body text")
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args[0], "/home/the-ancestor/mailbox/recovery")
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
    def test_never_raises_on_failure(self):
        # Even if every du subprocess errors, returns '' rather than propagating.
        with mock.patch.object(dum.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(dum._top_consumers(), "")


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
