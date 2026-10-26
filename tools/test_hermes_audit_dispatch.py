#!/usr/bin/env python3
"""Tests for tools/hermes_audit_dispatch.py (T-5354)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_audit_dispatch as had  # noqa: E402

TIP = "a" * 40


class FakeOps:
    def __init__(self, *, tip=TIP, wm=None, flying=0, dispatch_ok=True):
        self.tip, self.wm, self.flying, self.dispatch_ok = tip, wm, flying, dispatch_ok
        self.dispatches = 0

    def origin_tip_sha(self):
        return self.tip

    def watermark_coverage(self, tip_sha):
        return self.wm

    def runs_in_flight(self):
        return self.flying

    def dispatch(self):
        self.dispatches += 1
        return self.dispatch_ok


UNCOVERED = {"covered_by": None, "oldest_unaudited_epoch": 1}


class TickTest(unittest.TestCase):
    def test_covered_tip_dispatches_nothing(self):
        ops = FakeOps(wm={"covered_by": {"id": 7}, "oldest_unaudited_epoch": None})
        self.assertEqual(had.tick(ops)["verdict"], had.COVERED)
        self.assertEqual(ops.dispatches, 0)

    def test_uncovered_tip_with_nothing_in_flight_dispatches_once(self):
        ops = FakeOps(wm=UNCOVERED)
        self.assertEqual(had.tick(ops)["verdict"], had.DISPATCHED)
        self.assertEqual(ops.dispatches, 1)

    def test_a_queued_or_running_audit_blocks_a_second_dispatch(self):
        ops = FakeOps(wm=UNCOVERED, flying=1)
        self.assertEqual(had.tick(ops)["verdict"], had.IN_FLIGHT)
        self.assertEqual(ops.dispatches, 0)

    def test_unmeasurable_states_skip_and_never_dispatch(self):
        for ops in (FakeOps(tip=None), FakeOps(wm=None), FakeOps(wm=UNCOVERED, flying=None)):
            self.assertEqual(had.tick(ops)["verdict"], had.SKIP)
            self.assertEqual(ops.dispatches, 0)

    def test_dry_run_decides_but_does_not_dispatch(self):
        ops = FakeOps(wm=UNCOVERED)
        self.assertEqual(had.tick(ops, dry_run=True)["verdict"], had.DISPATCHED)
        self.assertEqual(ops.dispatches, 0)

    def test_failed_dispatch_is_reported_and_exits_nonzero(self):
        ops = FakeOps(wm=UNCOVERED, dispatch_ok=False)
        self.assertEqual(had.tick(ops)["verdict"], had.DISPATCH_FAILED)
        orig = had.Ops
        had.Ops = lambda: ops
        try:
            self.assertEqual(had.main([]), 1)
        finally:
            had.Ops = orig


class CountFreshTest(unittest.TestCase):
    NOW = 1_800_000_000

    def _run(self, age_secs):
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(self.NOW - age_secs, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"created_at": ts}

    def test_a_weeks_old_queued_run_does_not_block_dispatch(self):
        self.assertEqual(had.count_fresh([self._run(39 * 86400)], now=self.NOW), 0)

    def test_a_recent_run_counts(self):
        self.assertEqual(had.count_fresh([self._run(60), self._run(39 * 86400)], now=self.NOW), 1)

    def test_an_unparseable_timestamp_counts_as_fresh(self):
        self.assertEqual(had.count_fresh([{"created_at": "garbage"}], now=self.NOW), 1)


if __name__ == "__main__":
    unittest.main()
