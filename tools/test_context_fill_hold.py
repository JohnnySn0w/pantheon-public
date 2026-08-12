#!/usr/bin/env python3
"""Tests for tools/context_fill_hold.py (T-2615)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools import context_fill_hold as cfh


class TestStateDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))

    def test_hold_path_honours_env_override(self) -> None:
        path = cfh.hold_path_for_agent("orchestrator")
        self.assertEqual(path, Path(self._tmp.name) / "context_fill_orchestrator_hold.json")


class TestSetClearStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))

    def test_no_hold_is_inactive(self) -> None:
        self.assertFalse(cfh.hold_active("orchestrator"))
        self.assertEqual(cfh.hold_status("orchestrator"), {"active": False})

    def test_set_then_status_active_with_metadata(self) -> None:
        cfh.set_hold("orchestrator", hours=1, reason="deliberate hold, T-2615 incident", set_by="recovery", now=1_000_000.0)
        status = cfh.hold_status("orchestrator", now=1_000_000.0 + 60)
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "deliberate hold, T-2615 incident")
        self.assertEqual(status["set_by"], "recovery")
        self.assertEqual(status["ttl_seconds"], 3600)
        self.assertAlmostEqual(status["remaining_seconds"], 3600 - 60, delta=1)
        self.assertTrue(cfh.hold_active("orchestrator", now=1_000_000.0 + 60))

    def test_default_hours_is_two(self) -> None:
        data = cfh.set_hold("orchestrator", now=1_000_000.0)
        self.assertEqual(data["ttl_seconds"], 2 * 60 * 60)

    def test_expired_hold_is_inactive_bounded_mask_invariant(self) -> None:
        """A hold past its TTL must read as inactive -- a lapsed hold letting
        the safety restart re-engage is correct behavior, not a bug."""
        cfh.set_hold("orchestrator", hours=2, now=1_000_000.0)
        just_past = 1_000_000.0 + 2 * 3600 + 1
        status = cfh.hold_status("orchestrator", now=just_past)
        self.assertFalse(status["active"])
        self.assertTrue(status.get("expired"))
        self.assertFalse(cfh.hold_active("orchestrator", now=just_past))

    def test_hours_clamped_to_eight_hour_hard_cap(self) -> None:
        data = cfh.set_hold("orchestrator", hours=100, now=1_000_000.0)
        self.assertEqual(data["ttl_seconds"], 8 * 60 * 60)

    def test_boundary_exactly_eight_hours_not_clamped(self) -> None:
        data = cfh.set_hold("orchestrator", hours=8, now=1_000_000.0)
        self.assertEqual(data["ttl_seconds"], 8 * 60 * 60)

    def test_non_positive_hours_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cfh.set_hold("orchestrator", hours=0, now=1_000_000.0)
        with self.assertRaises(ValueError):
            cfh.set_hold("orchestrator", hours=-1, now=1_000_000.0)

    def test_clear_removes_active_hold(self) -> None:
        cfh.set_hold("orchestrator", hours=1, now=1_000_000.0)
        self.assertTrue(cfh.hold_active("orchestrator", now=1_000_000.0))
        existed = cfh.clear_hold("orchestrator")
        self.assertTrue(existed)
        self.assertFalse(cfh.hold_active("orchestrator", now=1_000_000.0))

    def test_clear_when_absent_returns_false_and_does_not_raise(self) -> None:
        self.assertFalse(cfh.clear_hold("orchestrator"))

    def test_agents_are_independent(self) -> None:
        cfh.set_hold("orchestrator", hours=1, now=1_000_000.0)
        self.assertTrue(cfh.hold_active("orchestrator", now=1_000_000.0))
        self.assertFalse(cfh.hold_active("specialist", now=1_000_000.0))

    def test_malformed_state_file_reads_inactive_fail_safe(self) -> None:
        """Mirrors cap_switch_paused's fail-safe shape: any doubt -> not held,
        never silently held forever on a corrupt file."""
        cfh.hold_path_for_agent("orchestrator").parent.mkdir(parents=True, exist_ok=True)
        cfh.hold_path_for_agent("orchestrator").write_text("{not json", encoding="utf-8")
        self.assertFalse(cfh.hold_active("orchestrator"))

    def test_missing_ttl_field_reads_inactive(self) -> None:
        path = cfh.hold_path_for_agent("orchestrator")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"agent": "orchestrator", "ts": 1000000}', encoding="utf-8")
        self.assertFalse(cfh.hold_active("orchestrator", now=1_000_000.0))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))

    def test_set_via_cli_then_status_reports_active(self) -> None:
        rc = cfh.main(["--agent", "orchestrator", "--hours", "1", "--reason", "test hold"])
        self.assertEqual(rc, 0)
        self.assertTrue(cfh.hold_active("orchestrator"))

    def test_status_flag_does_not_error_when_no_hold(self) -> None:
        rc = cfh.main(["--agent", "orchestrator", "--status"])
        self.assertEqual(rc, 0)

    def test_clear_flag_via_cli(self) -> None:
        cfh.main(["--agent", "orchestrator", "--hours", "1"])
        self.assertTrue(cfh.hold_active("orchestrator"))
        rc = cfh.main(["--agent", "orchestrator", "--clear"])
        self.assertEqual(rc, 0)
        self.assertFalse(cfh.hold_active("orchestrator"))

    def test_bad_hours_is_argv_error(self) -> None:
        rc = cfh.main(["--agent", "orchestrator", "--hours", "0"])
        self.assertEqual(rc, 2)
        self.assertFalse(cfh.hold_active("orchestrator"))

    def test_unknown_agent_is_argv_error(self) -> None:
        rc = cfh.main(["--agent", "nobody"])
        self.assertEqual(rc, 2)

    def test_status_and_clear_mutually_exclusive(self) -> None:
        rc = cfh.main(["--agent", "orchestrator", "--status", "--clear"])
        self.assertEqual(rc, 2)

    def test_missing_agent_is_argv_error(self) -> None:
        rc = cfh.main([])
        self.assertEqual(rc, 2)

    def test_cli_hours_above_cap_clamps_not_errors(self) -> None:
        rc = cfh.main(["--agent", "orchestrator", "--hours", "100"])
        self.assertEqual(rc, 0)
        status = cfh.hold_status("orchestrator")
        self.assertEqual(status["ttl_seconds"], 8 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
