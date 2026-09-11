"""Tests for tools/priority_list_health_watchdog.py (T-3020 BT4, ORG-078 §4
"persistent broken/empty priority-list state" fleet-detector leg).

Isolation pattern mirrors tools/test_acp_bridge_backlog_watchdog.py /
tools/test_credential_expiry_monitor.py: STATE_PATH + HEALTH_LOG_PATH
patched onto per-test tmp paths; the Recovery-routing primitive
(tools.mailbox.send) is patched directly rather than exercising a real
Maildir, per those same files' convention for the alert-transmit boundary.

Run from repo root:
    python3 -m unittest tools.test_priority_list_health_watchdog -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.priority_list_health_watchdog as watchdog

NOW = time.time()


def _entry(*, parse_ok: bool, parse_fail_class: str | None = None) -> dict:
    return {"ts": "2026-08-22T00:00:00Z", "parse_ok": parse_ok, "parse_fail_class": parse_fail_class}


class _WatchdogBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        state_patch = mock.patch.object(watchdog, "STATE_PATH", self.tmp_path / "state.json")
        state_patch.start()
        self.addCleanup(state_patch.stop)

        log_patch = mock.patch.object(watchdog, "HEALTH_LOG_PATH", self.tmp_path / "health.jsonl")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _write_log(self, entries: list[dict]) -> None:
        watchdog.HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with watchdog.HEALTH_LOG_PATH.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


class TestReadLastEntries(_WatchdogBase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(watchdog.read_last_entries(watchdog.HEALTH_LOG_PATH, 24), [])

    def test_reads_last_n_only(self) -> None:
        self._write_log([_entry(parse_ok=True) for _ in range(30)] + [_entry(parse_ok=False, parse_fail_class="x")])
        entries = watchdog.read_last_entries(watchdog.HEALTH_LOG_PATH, 5)
        self.assertEqual(len(entries), 5)
        self.assertFalse(entries[-1]["parse_ok"])

    def test_malformed_lines_are_skipped_not_raised(self) -> None:
        watchdog.HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with watchdog.HEALTH_LOG_PATH.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_entry(parse_ok=True)) + "\n")
            f.write("{not json\n")
            f.write(json.dumps(_entry(parse_ok=False, parse_fail_class="yaml-parse-fail")) + "\n")
        entries = watchdog.read_last_entries(watchdog.HEALTH_LOG_PATH, 10)
        self.assertEqual(len(entries), 2)


class TestPersistentBrokenClass(unittest.TestCase):
    def test_empty_log_does_not_trigger(self) -> None:
        self.assertIsNone(watchdog.persistent_broken_class([], 24))

    def test_fewer_than_window_entries_does_not_trigger(self) -> None:
        entries = [_entry(parse_ok=False, parse_fail_class="sentinel-missing") for _ in range(10)]
        self.assertIsNone(watchdog.persistent_broken_class(entries, 24))

    def test_all_broken_same_class_triggers(self) -> None:
        entries = [_entry(parse_ok=False, parse_fail_class="sentinel-missing") for _ in range(24)]
        self.assertEqual(watchdog.persistent_broken_class(entries, 24), "sentinel-missing")

    def test_recovered_once_in_window_does_not_trigger(self) -> None:
        entries = [_entry(parse_ok=False, parse_fail_class="sentinel-missing") for _ in range(23)]
        entries.append(_entry(parse_ok=True))
        self.assertIsNone(watchdog.persistent_broken_class(entries, 24))

    def test_mixed_fail_classes_does_not_trigger(self) -> None:
        entries = [_entry(parse_ok=False, parse_fail_class="sentinel-missing") for _ in range(12)]
        entries += [_entry(parse_ok=False, parse_fail_class="yaml-parse-fail") for _ in range(12)]
        self.assertIsNone(watchdog.persistent_broken_class(entries, 24))

    def test_healthy_entries_never_trigger(self) -> None:
        entries = [_entry(parse_ok=True) for _ in range(24)]
        self.assertIsNone(watchdog.persistent_broken_class(entries, 24))


class TestCheck(_WatchdogBase):
    def test_no_persistent_broken_returns_0_no_alert(self) -> None:
        self._write_log([_entry(parse_ok=True) for _ in range(24)])
        with mock.patch.object(watchdog, "_route_recovery") as mroute:
            rc = watchdog.check(dry_run=False, now=NOW)
        self.assertEqual(rc, 0)
        mroute.assert_not_called()

    def test_persistent_broken_alerts_recovery(self) -> None:
        self._write_log([_entry(parse_ok=False, parse_fail_class="yaml-parse-fail") for _ in range(24)])
        with mock.patch.object(watchdog, "_route_recovery") as mroute:
            rc = watchdog.check(dry_run=False, now=NOW)
        self.assertEqual(rc, 1)
        mroute.assert_called_once()
        self.assertIn("yaml-parse-fail", mroute.call_args[0][0])

    def test_dry_run_never_alerts(self) -> None:
        self._write_log([_entry(parse_ok=False, parse_fail_class="yaml-parse-fail") for _ in range(24)])
        with mock.patch.object(watchdog, "_route_recovery") as mroute:
            rc = watchdog.check(dry_run=True, now=NOW)
        self.assertEqual(rc, 1)
        mroute.assert_not_called()

    def test_cooldown_suppresses_repeat_alert_same_class(self) -> None:
        self._write_log([_entry(parse_ok=False, parse_fail_class="yaml-parse-fail") for _ in range(24)])
        with mock.patch.object(watchdog, "_route_recovery") as mroute:
            watchdog.check(dry_run=False, now=NOW)
            watchdog.check(dry_run=False, now=NOW + 60)  # well within cooldown
        mroute.assert_called_once()

    def test_class_change_bypasses_cooldown(self) -> None:
        with mock.patch.object(watchdog, "_route_recovery") as mroute:
            self._write_log([_entry(parse_ok=False, parse_fail_class="yaml-parse-fail") for _ in range(24)])
            watchdog.check(dry_run=False, now=NOW)
            self._write_log([_entry(parse_ok=False, parse_fail_class="sentinel-missing") for _ in range(24)])
            watchdog.check(dry_run=False, now=NOW + 60)
        self.assertEqual(mroute.call_count, 2)


class TestRouteWard(unittest.TestCase):
    def test_route_recovery_sends_urgent_mailbox_message(self) -> None:
        with mock.patch("tools.mailbox.send") as msend:
            watchdog._route_recovery("priority-list persistently broken: yaml-parse-fail")
        msend.assert_called_once()
        args, kwargs = msend.call_args
        self.assertEqual(args[0], watchdog.RECOVERY_INBOX)
        self.assertEqual(kwargs.get("severity"), "urgent")


class TestMain(unittest.TestCase):
    def test_refuses_bare_invocation(self) -> None:
        rc = watchdog.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_flag_accepted(self) -> None:
        with mock.patch.object(watchdog, "check", return_value=0) as mcheck:
            rc = watchdog.main(["--dry-run"])
        self.assertEqual(rc, 0)
        mcheck.assert_called_once()

    def test_check_flag_accepted(self) -> None:
        with mock.patch.object(watchdog, "check", return_value=1) as mcheck:
            rc = watchdog.main(["--check"])
        self.assertEqual(rc, 0)  # a monitor must not fail its own timer
        mcheck.assert_called_once()


if __name__ == "__main__":
    unittest.main()
