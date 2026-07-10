"""Unit tests for tools/restart_request.py.

Run from repo root:
    python3 -m unittest tools.test_restart_request -v

Covers (per T-0309 + ORG-020 Phase 2):
  - Valid agent → tombstone written with correct shape
  - Tombstone fields: requested_at (recent epoch), agent (matches arg), reason='operator'
  - Unknown agent → exits 2 (argparse error)
  - HANDOFF.md missing → refuses unless --force
  - HANDOFF.md fresh → proceeds
  - HANDOFF.md stale (>10 min) → refuses unless --force
  - --force bypasses staleness check
  - --force bypasses missing-HANDOFF check
  - Tombstone already present → overwrite (idempotent re-request)
  - State dir created if absent
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestRestartRequestTombstoneWrite(unittest.TestCase):
    """Verify tombstone is written with correct shape for known agents."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.handoff_dir = self.tmp / "handoff"
        self.handoff_dir.mkdir(parents=True)
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self._tmp.cleanup()

    def _fresh_handoff(self, agent: str) -> Path:
        """Write a fresh HANDOFF.md for the agent and return its path."""
        import tools.restart_request as rr
        cfg = rr.AGENT_CONFIG[agent]
        hf = Path(self.tmp / f"HANDOFF_{agent}.md")
        hf.write_text("# HANDOFF\nFresh handoff.", encoding="utf-8")
        return hf

    def _run(self, argv: list[str]) -> int:
        import tools.restart_request as rr
        return rr.main(argv)

    def test_recovery_writes_tombstone(self) -> None:
        """--agent recovery with fresh HANDOFF → tombstone written."""
        import tools.restart_request as rr
        hf = self.handoff_dir / "HANDOFF_recovery.md"
        hf.write_text("fresh", encoding="utf-8")
        rc = self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        self.assertEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertTrue(tombstone.exists(), "Tombstone should have been written")

    def test_tombstone_shape(self) -> None:
        """Tombstone JSON has required fields: requested_at, agent, reason=operator."""
        import tools.restart_request as rr
        hf = self.handoff_dir / "HANDOFF_orchestrator.md"
        hf.write_text("fresh", encoding="utf-8")
        self._run(["--agent", "orchestrator", "--handoff-file", str(hf)])
        tombstone = self.state_dir / "restart_requested_orchestrator.json"
        self.assertTrue(tombstone.exists())
        data = json.loads(tombstone.read_text())
        self.assertEqual(data["agent"], "orchestrator")
        self.assertEqual(data["reason"], "operator")
        self.assertIn("requested_at", data)
        now = int(time.time())
        self.assertAlmostEqual(data["requested_at"], now, delta=5)

    def test_specialist_writes_tombstone(self) -> None:
        """--agent specialist writes correct tombstone path."""
        import tools.restart_request as rr
        hf = self.handoff_dir / "HANDOFF_specialist.md"
        hf.write_text("fresh", encoding="utf-8")
        self._run(["--agent", "specialist", "--handoff-file", str(hf)])
        tombstone = self.state_dir / "restart_requested_specialist.json"
        self.assertTrue(tombstone.exists())
        data = json.loads(tombstone.read_text())
        self.assertEqual(data["agent"], "specialist")

    def test_state_dir_created_if_absent(self) -> None:
        """State dir is created if it doesn't exist."""
        import tools.restart_request as rr
        hf = self.handoff_dir / "HANDOFF_recovery.md"
        hf.write_text("fresh", encoding="utf-8")
        self.assertFalse(self.state_dir.exists())
        self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        self.assertTrue(self.state_dir.exists())

    def test_tombstone_overwritten_on_repeat_request(self) -> None:
        """A second invocation for the same agent overwrites the existing tombstone."""
        import tools.restart_request as rr
        hf = self.handoff_dir / "HANDOFF_recovery.md"
        hf.write_text("fresh", encoding="utf-8")
        tombstone = self.state_dir / "restart_requested_recovery.json"
        # Write first
        self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        first_ts = json.loads(tombstone.read_text())["requested_at"]
        # Tiny sleep to ensure ts differs
        time.sleep(0.05)
        # Write second
        self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        second_ts = json.loads(tombstone.read_text())["requested_at"]
        # Both should succeed; second should be >= first
        self.assertGreaterEqual(second_ts, first_ts)


class TestRestartRequestHandoffValidation(unittest.TestCase):
    """HANDOFF.md freshness gate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> int:
        import tools.restart_request as rr
        return rr.main(argv)

    def test_missing_handoff_refuses(self) -> None:
        """Missing HANDOFF.md → rc=1, no tombstone."""
        nonexistent = self.tmp / "no_such_HANDOFF.md"
        rc = self._run(["--agent", "recovery", "--handoff-file", str(nonexistent)])
        self.assertNotEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertFalse(tombstone.exists())

    def test_missing_handoff_force_proceeds(self) -> None:
        """Missing HANDOFF.md + --force → proceeds (tombstone written)."""
        nonexistent = self.tmp / "no_such_HANDOFF.md"
        rc = self._run(["--agent", "recovery", "--handoff-file", str(nonexistent), "--force"])
        self.assertEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertTrue(tombstone.exists())

    def test_stale_handoff_refuses(self) -> None:
        """HANDOFF.md mtime > 10 min → refuses without --force."""
        hf = self.tmp / "HANDOFF.md"
        hf.write_text("stale handoff", encoding="utf-8")
        # Set mtime to 15 minutes ago
        stale_ts = time.time() - 900
        os.utime(str(hf), (stale_ts, stale_ts))
        rc = self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        self.assertNotEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertFalse(tombstone.exists())

    def test_stale_handoff_force_proceeds(self) -> None:
        """HANDOFF.md mtime > 10 min + --force → proceeds."""
        hf = self.tmp / "HANDOFF.md"
        hf.write_text("stale but forced", encoding="utf-8")
        stale_ts = time.time() - 900
        os.utime(str(hf), (stale_ts, stale_ts))
        rc = self._run(["--agent", "recovery", "--handoff-file", str(hf), "--force"])
        self.assertEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertTrue(tombstone.exists())

    def test_fresh_handoff_proceeds(self) -> None:
        """HANDOFF.md mtime < 10 min → proceeds normally."""
        hf = self.tmp / "HANDOFF.md"
        hf.write_text("fresh handoff", encoding="utf-8")
        rc = self._run(["--agent", "recovery", "--handoff-file", str(hf)])
        self.assertEqual(rc, 0)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        self.assertTrue(tombstone.exists())


class TestRestartRequestArgv(unittest.TestCase):
    """Argument parsing edge cases."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self._tmp.cleanup()

    def test_unknown_agent_exits_two(self) -> None:
        """--agent unknown → argparse exits 2."""
        import tools.restart_request as rr
        with patch("sys.stderr"):
            rc = rr.main(["--agent", "echo"])
        self.assertEqual(rc, 2)

    def test_all_valid_agents_accepted(self) -> None:
        """recovery, orchestrator, specialist are all valid agent names."""
        import tools.restart_request as rr
        hf = self.tmp / "HANDOFF.md"
        hf.write_text("fresh", encoding="utf-8")
        for agent in ("recovery", "orchestrator", "specialist"):
            with self.subTest(agent=agent):
                rc = rr.main(["--agent", agent, "--handoff-file", str(hf), "--force"])
                self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
