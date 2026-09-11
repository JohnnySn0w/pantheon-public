#!/usr/bin/env python3
"""Unittest coverage for the T-3473 auto-clear of provably-resolved DLQ leftovers
in tools/acp_bridge_backlog_watchdog.py.

The defect: when quarantine_is_leftover(...) is True the DLQ alert ALREADY tells a
human "these are leftovers from an already-resolved fault, clear them" — but a human
had to hand-move them, and the alert re-fired (falling back to the operator when the recovery
pane was unreachable) until they did. The fix moves provably-resolved leftovers into
failed/.cleared-<date>-auto-resolved/ automatically and does NOT alert, while NEVER
auto-clearing a possibly-live fault (leftover False/None).

Fail-safe direction that must never invert:
- leftover True  -> auto-clear (when enabled), no alert
- leftover False -> stays, alert fires (possibly still stuck)
- leftover None  -> stays, alert fires (can't tell — treat as not-resolved)

Dark-ship: gated behind PANTHEON_ACP_DLQ_AUTOCLEAR / wd.AUTOCLEAR_ENABLED, default off.

unittest (not pytest) so it runs under `python3 -m unittest`.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import acp_bridge_backlog_watchdog as wd


def _touch(path: Path, mtime: float) -> None:
    path.write_text("{}")
    os.utime(path, (mtime, mtime))


class _Rec:
    def __init__(self):
        self.recovery_calls = []
        self.operator_calls = []


class AutoClearCheckTest(unittest.TestCase):
    """End-to-end through check(): the leftover discriminator + flag drive disposition."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "specialist"
        (self.inbox / "new").mkdir(parents=True)
        (self.inbox / "cur").mkdir(parents=True)
        (self.inbox / "failed").mkdir(parents=True)
        self.rec = _Rec()

        self._orig_route = wd._route_dlq_recovery
        self._orig_alert = wd._alert
        self._orig_state_path = wd.STATE_PATH
        self._orig_flag = wd.AUTOCLEAR_ENABLED
        wd._route_dlq_recovery = lambda agent, body: self.rec.recovery_calls.append((agent, body))
        wd._alert = lambda text: self.rec.operator_calls.append(text)
        wd.STATE_PATH = Path(self._tmp.name) / "alert_state.json"

    def tearDown(self):
        wd._route_dlq_recovery = self._orig_route
        wd._alert = self._orig_alert
        wd.STATE_PATH = self._orig_state_path
        wd.AUTOCLEAR_ENABLED = self._orig_flag
        self._tmp.cleanup()

    def _leftover_file(self, name: str, mtime: float) -> Path:
        p = self.inbox / "failed" / name
        _touch(p, mtime)
        return p

    def _acked_file(self, name: str, mtime: float) -> Path:
        p = self.inbox / "cur" / name
        _touch(p, mtime)
        return p

    # ---- leftover True: resolved -> auto-clear, no alert ----------------------

    def test_resolved_leftover_autoclears_and_does_not_alert(self):
        wd.AUTOCLEAR_ENABLED = True
        self._leftover_file("01784790321641723172.1.000000.keep_going", mtime=1000.0)
        # An ack NEWER than the newest quarantine => quarantine_is_leftover True.
        self._acked_file("01784790400000000000.1.000000.keep_going", mtime=2000.0)

        rc = wd.check("specialist", str(self.inbox), dry_run=False)

        # The loose file is gone from failed/ (moved, not deleted)...
        loose = [n for n in os.listdir(self.inbox / "failed")
                 if (self.inbox / "failed" / n).is_file()]
        self.assertEqual(loose, [], "resolved leftover must be moved out of failed/")
        cleared_dirs = [d for d in os.listdir(self.inbox / "failed")
                        if d.startswith(".cleared-") and d.endswith("-auto-resolved")]
        self.assertEqual(len(cleared_dirs), 1, "a dated cleared/ subdir must exist")
        moved = os.listdir(self.inbox / "failed" / cleared_dirs[0])
        self.assertIn("01784790321641723172.1.000000.keep_going", moved,
                      "message preserved (move-not-delete) for diagnostics")
        # ...and NO alert of either kind fired.
        self.assertEqual(self.rec.recovery_calls, [],
                         "an auto-cleared resolved leftover must not alert Recovery")
        self.assertEqual(self.rec.operator_calls, [],
                         "an auto-cleared resolved leftover must not alert the operator")
        self.assertEqual(rc, 0, "clean new/ + auto-cleared failed/ is not a breach")

    def test_flag_off_does_not_autoclear_even_when_resolved(self):
        wd.AUTOCLEAR_ENABLED = False  # dark-ship default
        self._leftover_file("01784790321641723172.1.000000.keep_going", mtime=1000.0)
        self._acked_file("01784790400000000000.1.000000.keep_going", mtime=2000.0)

        rc = wd.check("specialist", str(self.inbox), dry_run=False)

        loose = [n for n in os.listdir(self.inbox / "failed")
                 if (self.inbox / "failed" / n).is_file()]
        self.assertEqual(len(loose), 1, "flag off: the leftover must stay put")
        self.assertEqual(len(self.rec.recovery_calls), 1,
                         "flag off: the DLQ alert fires exactly as before")
        self.assertEqual(rc, 1)

    # ---- leftover False: possibly-live -> never auto-clear, alert -------------

    def test_unresolved_fault_never_autoclears_and_alerts(self):
        wd.AUTOCLEAR_ENABLED = True
        # Quarantine is NEWER than the only ack => nothing acked since => leftover False.
        self._acked_file("01784790000000000000.1.000000.keep_going", mtime=1000.0)
        self._leftover_file("01784790400000000000.1.000000.keep_going", mtime=2000.0)

        rc = wd.check("specialist", str(self.inbox), dry_run=False)

        loose = [n for n in os.listdir(self.inbox / "failed")
                 if (self.inbox / "failed" / n).is_file()]
        self.assertEqual(len(loose), 1,
                         "a possibly-live fault must NEVER be auto-cleared")
        self.assertEqual(len(self.rec.recovery_calls), 1,
                         "a possibly-live fault still alerts Recovery")
        self.assertEqual(rc, 1)

    # ---- leftover None: can't tell -> never auto-clear, alert -----------------

    def test_indeterminate_never_autoclears_and_alerts(self):
        wd.AUTOCLEAR_ENABLED = True
        # No acked messages at all in cur/ => quarantine_is_leftover None.
        self._leftover_file("01784790400000000000.1.000000.keep_going", mtime=2000.0)

        rc = wd.check("specialist", str(self.inbox), dry_run=False)

        loose = [n for n in os.listdir(self.inbox / "failed")
                 if (self.inbox / "failed" / n).is_file()]
        self.assertEqual(len(loose), 1,
                         "can't-tell must NEVER be auto-cleared (fail-safe)")
        self.assertEqual(len(self.rec.recovery_calls), 1,
                         "can't-tell still alerts Recovery")
        self.assertEqual(rc, 1)

    # ---- dry-run: report, but do not move ------------------------------------

    def test_dry_run_reports_but_moves_nothing(self):
        wd.AUTOCLEAR_ENABLED = True
        self._leftover_file("01784790321641723172.1.000000.keep_going", mtime=1000.0)
        self._acked_file("01784790400000000000.1.000000.keep_going", mtime=2000.0)

        rc = wd.check("specialist", str(self.inbox), dry_run=True)

        loose = [n for n in os.listdir(self.inbox / "failed")
                 if (self.inbox / "failed" / n).is_file()]
        self.assertEqual(len(loose), 1, "dry-run must not move the file")
        self.assertEqual(self.rec.recovery_calls, [], "dry-run must transmit nothing")


class AutoClearUnitTest(unittest.TestCase):
    """Direct coverage of auto_clear_leftovers()'s move-not-delete + fail-through."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "specialist"
        (self.inbox / "failed").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_all_and_preserves_content(self):
        p = self.inbox / "failed" / "01784790321641723172.1.000000.keep_going"
        p.write_text('{"payload": "keep"}')
        cleared, failed_to_clear = wd.auto_clear_leftovers(
            self.inbox, [p.name], now=1_700_000_000.0, dry_run=False
        )
        self.assertEqual(cleared, [p.name])
        self.assertEqual(failed_to_clear, [])
        self.assertFalse(p.exists(), "source moved out of failed/ root")
        dest = self.inbox / "failed" / ".cleared-2023-11-14-auto-resolved" / p.name
        self.assertTrue(dest.exists(), "moved into the dated cleared/ subdir")
        self.assertEqual(dest.read_text(), '{"payload": "keep"}',
                         "content preserved (move-not-delete)")

    def test_dry_run_moves_nothing_but_reports(self):
        p = self.inbox / "failed" / "01784790321641723172.1.000000.keep_going"
        p.write_text("{}")
        cleared, failed_to_clear = wd.auto_clear_leftovers(
            self.inbox, [p.name], now=1_700_000_000.0, dry_run=True
        )
        self.assertEqual(cleared, [p.name], "dry-run reports what WOULD move")
        self.assertEqual(failed_to_clear, [])
        self.assertTrue(p.exists(), "dry-run must not actually move")

    def test_move_failure_falls_through_to_failed_to_clear(self):
        # A name that does not exist on disk => os.replace raises => reported as
        # failed_to_clear (the caller then alerts; never silently lost).
        cleared, failed_to_clear = wd.auto_clear_leftovers(
            self.inbox, ["does-not-exist.msg"], now=1_700_000_000.0, dry_run=False
        )
        self.assertEqual(cleared, [])
        self.assertEqual(failed_to_clear, ["does-not-exist.msg"])


if __name__ == "__main__":
    unittest.main()
