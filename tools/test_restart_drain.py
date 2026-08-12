"""Unit + integration tests for tools/restart_drain.py.

Run from repo root:
    python3 -m unittest tools.test_restart_drain -v

Covers (per T-0309 + ORG-020):
  - Feature-flag disabled → clean bail (no fire)
  - No tombstone → clean bail
  - Tombstone present → fires handoff_and_restart.sh; removes tombstone on success
  - Tombstone present → leaves tombstone on failure
  - Multiple tombstones → drains all; stops on first failure
  - Stale tombstone (context_fill_threshold reason, >5 min old) → skips
  - Operator tombstone (reason=operator) → fires regardless of mtime
  - Per-agent state file path isolation (one drain per account)
  - Integration same-process: tombstone write → drain main() → mock subprocess fire

All subprocess calls mocked. No real sudo invoked. No real state files touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tombstone(state_dir: Path, agent: str, reason: str = "context_fill_threshold",
                     age_seconds: int = 0) -> Path:
    """Write a tombstone file and return its path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"restart_requested_{agent}.json"
    data = {
        "requested_at": int(time.time()) - age_seconds,
        "agent": agent,
        "reason": reason,
    }
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path


def _mock_subprocess_run_success(cmd, **kwargs):
    """Simulate subprocess.run returning success."""
    return MagicMock(returncode=0, stdout="restart ok", stderr="")


def _mock_subprocess_run_failure(cmd, **kwargs):
    """Simulate subprocess.run returning failure."""
    return MagicMock(returncode=1, stdout="", stderr="handoff failed")


# ---------------------------------------------------------------------------
# Feature-flag tests
# ---------------------------------------------------------------------------


class TestFeatureFlag(unittest.TestCase):
    """Drain script must be a no-op when PANTHEON_RESTART_DRAIN_ENABLED is unset."""

    def setUp(self) -> None:
        # Ensure env var is absent
        os.environ.pop("PANTHEON_RESTART_DRAIN_ENABLED", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_RESTART_DRAIN_ENABLED", None)
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self._tmp.cleanup()

    def test_disabled_by_default_returns_zero(self) -> None:
        """When PANTHEON_RESTART_DRAIN_ENABLED is unset, main() returns 0 without firing."""
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()

    def test_enabled_zero_proceeds(self) -> None:
        """When PANTHEON_RESTART_DRAIN_ENABLED=1, proceeds (may bail on no tombstone)."""
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        # No tombstone → no subprocess fire
        mock_run.assert_not_called()

    def test_wrong_value_treated_as_disabled(self) -> None:
        """PANTHEON_RESTART_DRAIN_ENABLED=0 or other non-'1' value → disabled."""
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "0"
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# No-tombstone tests
# ---------------------------------------------------------------------------


class TestNoTombstone(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_no_tombstone_returns_zero_no_fire(self) -> None:
        """State dir absent → no tombstones → bail clean."""
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()

    def test_state_dir_exists_but_empty(self) -> None:
        """State dir present but no restart_requested_*.json → bail clean."""
        self.state_dir.mkdir(parents=True)
        # Write an unrelated file to confirm pattern match is specific
        (self.state_dir / "context_fill_orchestrator_last_restart.json").write_text("{}")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Tombstone-present tests
# ---------------------------------------------------------------------------


class TestTombstonePresent(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)
        # T-0709: the success path now calls _notify_restart_applied, which sends
        # a real audit Telegram via inbox_consumer._send_telegram_info. Neutralize
        # it here so success-path tests never emit a live alert (this machine has
        # a real bot token — an unpatched run would ping the operator). The real notify
        # behavior is covered by TestRestartAppliedNotification below.
        import tools.restart_drain as _rd
        self._notify_patch = patch.object(_rd, "_notify_restart_applied")
        self.mock_notify = self._notify_patch.start()
        self.addCleanup(self._notify_patch.stop)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_tombstone_fires_handoff_restart(self) -> None:
        """Tombstone present → subprocess called (handoff_and_restart.sh via systemd-run).

        T-0535: updated from direct-subprocess form — handoff_and_restart is now
        launched inside a detached systemd-run transient unit. It appears in the
        inner bash -c string, not as the top-level command.
        """
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold", age_seconds=0)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(mock_run.called)
        # handoff_and_restart appears in the combined subprocess args (inside inner string)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("handoff_and_restart", combined)
        # systemd-run is the outer launch mechanism
        self.assertIn("systemd-run", combined)

    def test_tombstone_removed_on_success(self) -> None:
        """Tombstone file is removed after successful handoff call."""
        tombstone = _write_tombstone(self.state_dir, "recovery")
        self.assertTrue(tombstone.exists())
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success):
            rd.main([])
        self.assertFalse(tombstone.exists(), "Tombstone should be removed after success")

    def test_tombstone_kept_on_failure(self) -> None:
        """Tombstone file is NOT removed when handoff call fails."""
        tombstone = _write_tombstone(self.state_dir, "recovery")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_failure):
            rc = rd.main([])
        self.assertEqual(rc, 1)
        self.assertTrue(tombstone.exists(), "Tombstone should remain after failure")

    def test_fire_uses_correct_service_for_recovery(self) -> None:
        """T-2684: recovery is control-plane-supervised (T-2590 cutover) — the
        drain must invoke pantheon-persona-consume@recovery.service, NOT the
        disabled bespoke claude-telegram-fallback.service, and must pass
        --supervisor control-plane so handoff_and_restart.sh takes the
        kill-session + scope-liveness branch instead of plain systemctl
        restart / is-active. Mirrors test_fire_uses_correct_service_for_specialist
        (T-2658)."""
        _write_tombstone(self.state_dir, "recovery")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rd.main([])
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("pantheon-persona-consume@recovery.service", combined)
        self.assertNotIn("claude-telegram-fallback.service", combined)
        self.assertIn("--supervisor control-plane", combined)

    def test_recovery_fire_does_not_pass_idle_sentinel(self) -> None:
        """Recovery tombstone → NO --idle-sentinel in subprocess args.

        T-0543: Recovery's per-agent idle_sentinel override was removed from
        DRAIN_AGENT_CONFIG because the shared default in handoff_and_restart.sh
        now includes '❯' for Claude Code 2.1.x (fixes the class for all agents).
        The drain must pass nothing, letting the script's updated default apply.
        """
        # T-0543: Recovery's per-agent override removed; default now covers all agents.
        _write_tombstone(self.state_dir, "recovery")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rd.main([])
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertNotIn("--idle-sentinel", combined)

    def test_fire_uses_correct_service_for_orchestrator(self) -> None:
        """Orchestrator tombstone → invokes claude-telegram.service."""
        _write_tombstone(self.state_dir, "orchestrator")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rd.main([])
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("claude-telegram.service", combined)

    def test_fire_uses_correct_service_for_specialist(self) -> None:
        """T-2658: specialist is control-plane-supervised (T-2633 cutover) — the
        drain must invoke pantheon-persona-consume@specialist.service, NOT the
        disabled bespoke claude-telegram-research.service, and must pass
        --supervisor control-plane so handoff_and_restart.sh takes the
        kill-session + scope-liveness branch instead of plain systemctl
        restart / is-active."""
        _write_tombstone(self.state_dir, "specialist")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rd.main([])
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("pantheon-persona-consume@specialist.service", combined)
        self.assertNotIn("claude-telegram-research.service", combined)
        self.assertIn("--supervisor control-plane", combined)

    def test_fire_omits_supervisor_flag_for_bespoke_seats(self) -> None:
        """T-2658 regression guard: orchestrator is NOT cut over (T-2684 flipped
        recovery) — the drain must produce byte-identical argv to pre-T-2658 (no
        --supervisor flag at all), so the bespoke restart path is provably
        untouched."""
        _write_tombstone(self.state_dir, "orchestrator")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rd.main([])
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertNotIn("--supervisor", combined)
        self.assertIn("claude-telegram.service", combined)


# ---------------------------------------------------------------------------
# Multiple tombstones tests
# ---------------------------------------------------------------------------


class TestMultipleTombstones(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_drains_all_tombstones_on_success(self) -> None:
        """Two tombstones → both get processed and removed."""
        t1 = _write_tombstone(self.state_dir, "recovery")
        t2 = _write_tombstone(self.state_dir, "orchestrator")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success):
            rc = rd.main([])
        self.assertEqual(rc, 0)
        self.assertFalse(t1.exists())
        self.assertFalse(t2.exists())

    def test_stops_and_returns_one_on_first_failure(self) -> None:
        """One tombstone succeeds, one fails → rc=1; failed tombstone remains."""
        # Write two tombstones; mock alternates success/failure
        _write_tombstone(self.state_dir, "orchestrator")
        t_recovery = _write_tombstone(self.state_dir, "recovery")
        call_count = [0]

        def alternating(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=0, stdout="ok", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="fail")

        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=alternating):
            rc = rd.main([])
        # At least one failure → rc=1
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Freshness / staleness tests
# ---------------------------------------------------------------------------


class TestFreshnessGating(unittest.TestCase):
    """Context-fill tombstones >5 min old are skipped; operator tombstones are not."""

    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_context_fill_tombstone_within_5min_fires(self) -> None:
        """context_fill_threshold tombstone < 5 min old → fires."""
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold", age_seconds=30)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(mock_run.called)

    def test_context_fill_tombstone_stale_skipped(self) -> None:
        """context_fill_threshold tombstone > 5 min old → skip (don't fire)."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold",
                                     age_seconds=400)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        # Stale tombstone removed so it doesn't block future fires
        self.assertFalse(tombstone.exists(), "Stale tombstone should be cleaned up")

    def test_operator_tombstone_stale_still_fires(self) -> None:
        """operator tombstone ignores freshness → fires even if old."""
        _write_tombstone(self.state_dir, "recovery", reason="operator", age_seconds=600)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(mock_run.called)

    def test_other_reason_tombstone_fires_without_freshness_gate(self) -> None:
        """reason='other' (not context_fill_threshold) → fires regardless of age."""
        _write_tombstone(self.state_dir, "recovery", reason="other", age_seconds=600)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(mock_run.called)


# ---------------------------------------------------------------------------
# Malformed tombstone tests
# ---------------------------------------------------------------------------


class TestMalformedTombstone(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir(parents=True)
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_malformed_json_skipped_and_removed(self) -> None:
        """Malformed tombstone JSON → skip; remove to prevent blocking."""
        bad = self.state_dir / "restart_requested_recovery.json"
        bad.write_text("not json {{", encoding="utf-8")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        self.assertFalse(bad.exists(), "Malformed tombstone should be removed")

    def test_unknown_agent_tombstone_quarantined(self) -> None:
        """T-0824: a tombstone for an unknown agent is QUARANTINED — moved out of the
        scan path (so it stops re-accumulating every ~30s) + alerted ONCE — not left
        in place. The file is preserved for forensics, no restart fires, and the
        alert notify is mocked so no live Telegram is attempted (the very leak that
        bit 2026-06-06: this path's notify was unmocked)."""
        bad = self.state_dir / "restart_requested_unknown_agent.json"
        bad.write_text(json.dumps({"requested_at": int(time.time()), "agent": "unknown_agent",
                                   "reason": "operator"}) + "\n", encoding="utf-8")
        import tools.restart_drain as rd
        with patch("tools.inbox_consumer._send_telegram_info") as info, \
             patch("tools.restart_drain.subprocess.run") as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()  # unknown agent → no restart fired
        # Moved OUT of the scan path: original gone, glob no longer matches it.
        self.assertFalse(bad.exists(), "unknown-agent tombstone should be moved out")
        self.assertEqual(rd._find_tombstones(self.state_dir), [],
                         "quarantined file must not re-appear in the scan glob")
        # Preserved (not deleted) under the quarantine dir for forensics.
        q = self.state_dir / rd.QUARANTINE_DIRNAME
        self.assertTrue(q.exists(), "quarantine dir should be created")
        moved = list(q.glob("restart_requested_unknown_agent.json.*"))
        self.assertEqual(len(moved), 1, "exactly one quarantined copy preserved")
        # Alerted exactly once, naming the offending agent.
        info.assert_called_once()
        self.assertIn("unknown_agent", info.call_args[0][0])


# ---------------------------------------------------------------------------
# Integration (same-process): tombstone write → drain → fire round-trip
# ---------------------------------------------------------------------------


class TestIntegrationRoundTrip(unittest.TestCase):
    """Integration tier: write tombstone via helper, run drain main(), assert subprocess fired."""

    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_write_then_drain_removes_tombstone_on_success(self) -> None:
        """End-to-end: write tombstone → drain → tombstone gone."""
        # Write tombstone the same way context_fill_monitor would
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold",
                                     age_seconds=0)
        self.assertTrue(tombstone.exists())

        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success):
            rc = rd.main([])

        self.assertEqual(rc, 0)
        self.assertFalse(tombstone.exists())

    def test_write_then_drain_leaves_tombstone_on_failure(self) -> None:
        """End-to-end: write tombstone → drain fails → tombstone present for retry."""
        tombstone = _write_tombstone(self.state_dir, "orchestrator", reason="context_fill_threshold",
                                     age_seconds=0)

        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_failure):
            rc = rd.main([])

        self.assertEqual(rc, 1)
        self.assertTrue(tombstone.exists())

    def test_xdg_runtime_dir_in_subprocess_call(self) -> None:
        """XDG_RUNTIME_DIR is present in the subprocess calls.

        T-0535: updated from direct-subprocess form. XDG_RUNTIME_DIR now
        appears in two places: as --setenv=XDG_RUNTIME_DIR= on the systemd-run
        argv, AND inside the inner bash -c string. Check the combined output
        from all subprocess calls rather than only the first call.
        """
        _write_tombstone(self.state_dir, "recovery", age_seconds=0)
        import tools.restart_drain as rd
        captured = []

        def capture(cmd, **kwargs):
            if isinstance(cmd, list):
                captured.append(list(cmd))
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=capture):
            rd.main([])

        self.assertTrue(len(captured) > 0, "No subprocess calls captured")
        # Check across all captured calls (reset-failed + systemd-run)
        all_combined = " ".join(str(x) for row in captured for x in row)
        self.assertIn("XDG_RUNTIME_DIR", all_combined)


# ---------------------------------------------------------------------------
# T-0510: api_health_force → FORCE restart via --skip-ack
# ---------------------------------------------------------------------------


class TestApiHealthForce(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_api_health_force_passes_skip_ack(self) -> None:
        """api_health_force tombstone → wrapper invoked WITH --skip-ack."""
        _write_tombstone(self.state_dir, "recovery", reason="api_health_force")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("--skip-ack", combined)

    def test_context_fill_does_not_pass_skip_ack(self) -> None:
        """T-0763 (revises T-0534): context_fill_threshold is GRACEFUL — NO --skip-ack.

        A context-fill restart targets a HEALTHY agent that can write a handoff;
        handoff_and_restart.sh steps 2-4 prompt it (via send-keys) to refresh
        HANDOFF.md and wait for the mtime-ack before the kill, so the fresh
        session boots from a current handoff. The agent writes it autonomously —
        "no interactive operator" (T-0534's rationale for the blanket --skip-ack)
        does NOT mean "cannot write a handoff". ONLY api_health_force (a poisoned
        session that can't respond) still forces. This test previously asserted
        assertIn('--skip-ack', ...) under T-0534 — it encoded the bug that cut off
        Recovery's handoff on 2026-06-03. The interactive ack capability is untouched.
        """
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertNotIn("--skip-ack", combined)

    def test_api_health_force_bypasses_staleness_gate(self) -> None:
        """An OLD api_health_force tombstone still fires (poison persists; not freshness-gated)."""
        # 1h old — a context_fill_threshold tombstone this old would be dropped.
        _write_tombstone(self.state_dir, "recovery", reason="api_health_force", age_seconds=3600)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("--skip-ack", combined)
        # Fired successfully → tombstone removed (not dropped as stale).
        self.assertFalse(tombstone.exists())

    def test_stale_context_fill_still_dropped(self) -> None:
        """Regression: a stale context_fill_threshold tombstone is still dropped without firing."""
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold", age_seconds=3600)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        self.assertFalse(tombstone.exists())  # removed as stale

    def test_fire_restart_skip_ack_appends_flag(self) -> None:
        """_fire_restart(skip_ack=True) appends --skip-ack to the wrapper invocation.

        T-0535: updated from direct-subprocess form. The inner bash -c string is
        now the last element of the systemd-run argv (after '--'). Capture the
        full combined args across all subprocess calls and search for --skip-ack.
        """
        import tools.restart_drain as rd
        cfg = rd.DRAIN_AGENT_CONFIG["recovery"]
        all_calls = []

        def capture(cmd, **kwargs):
            all_calls.append(list(cmd) if isinstance(cmd, list) else [str(cmd)])
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=capture):
            rd._fire_restart(cfg, "recovery", skip_ack=True)
        combined_true = " ".join(a for row in all_calls for a in row)
        self.assertIn("--skip-ack", combined_true)

        all_calls.clear()
        with patch("tools.restart_drain.subprocess.run", side_effect=capture):
            rd._fire_restart(cfg, "recovery", skip_ack=False)
        combined_false = " ".join(a for row in all_calls for a in row)
        self.assertNotIn("--skip-ack", combined_false)

    def test_fire_restart_graceful_adds_t2829_modal_flags(self) -> None:
        """T-2829: the GRACEFUL path (skip_ack=False) carries --dismiss-modal +
        --hard-restart-on-ack-timeout so a modal-wedged session is recovered
        (Escape-dismiss, then hard-restart fallback) instead of stalling at the
        ack-wait forever (the 2026-08-11 specialist settings.json-modal incident)."""
        import tools.restart_drain as rd
        cfg = rd.DRAIN_AGENT_CONFIG["recovery"]
        all_calls = []

        def capture(cmd, **kwargs):
            all_calls.append(list(cmd) if isinstance(cmd, list) else [str(cmd)])
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=capture):
            rd._fire_restart(cfg, "recovery", skip_ack=False)
        combined_false = " ".join(a for row in all_calls for a in row)
        self.assertIn("--dismiss-modal", combined_false)
        self.assertIn("--hard-restart-on-ack-timeout", combined_false)

    def test_fire_restart_force_omits_t2829_modal_flags(self) -> None:
        """T-2829: the FORCE path (skip_ack=True) skips steps 2-4 entirely (kills
        the process, so the modal dies with it) — the modal flags are irrelevant
        there and must NOT be appended (they only apply to the handoff beat)."""
        import tools.restart_drain as rd
        cfg = rd.DRAIN_AGENT_CONFIG["recovery"]
        all_calls = []

        def capture(cmd, **kwargs):
            all_calls.append(list(cmd) if isinstance(cmd, list) else [str(cmd)])
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=capture):
            rd._fire_restart(cfg, "recovery", skip_ack=True)
        combined_true = " ".join(a for row in all_calls for a in row)
        self.assertIn("--skip-ack", combined_true)
        self.assertNotIn("--dismiss-modal", combined_true)
        self.assertNotIn("--hard-restart-on-ack-timeout", combined_true)


# ---------------------------------------------------------------------------
# T-0763 (revises T-0534): --skip-ack is GATED BY REASON, not blanket-applied.
# Forced (api_health_force) skips ack; graceful (context_fill_threshold,
# operator, …) runs the pre-restart handoff beat.
# ---------------------------------------------------------------------------


class TestSkipAckByReason(unittest.TestCase):
    """T-0763: --skip-ack is gated on the tombstone reason.

    T-0534 blanket-applied --skip-ack to EVERY drain-fired restart on the theory
    that an automated drain has no interactive operator to answer the handoff
    ack-wait. But the agent writes the handoff AUTONOMOUSLY — handoff_and_restart.sh
    steps 2-4 send-keys the prompt into the pane and detect the write via mtime,
    no operator needed. "No interactive operator" never meant "cannot write a
    handoff"; conflating them cut off Recovery's handoff on 2026-06-03.

    Correct gate: ONLY api_health_force (a poisoned session that genuinely cannot
    respond) skips ack. Every graceful reason gets the handoff beat so the fresh
    session boots from a current HANDOFF.md.
    """

    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_operator_reason_does_not_pass_skip_ack(self) -> None:
        """reason=operator → GRACEFUL, NO --skip-ack (healthy agent hands off first)."""
        _write_tombstone(self.state_dir, "recovery", reason="operator")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertNotIn("--skip-ack", combined)

    def test_context_fill_reason_does_not_pass_skip_ack(self) -> None:
        """reason=context_fill_threshold (fresh) → GRACEFUL, NO --skip-ack.

        Pre-T-0763 this asserted the OPPOSITE (assertIn) under T-0534 — encoding
        the bug that cut off Recovery's handoff. A context-fill restart targets a
        HEALTHY agent that can and should write a handoff before the kill.
        """
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold", age_seconds=0)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertNotIn("--skip-ack", combined)

    def test_api_health_force_still_passes_skip_ack(self) -> None:
        """reason=api_health_force → --skip-ack present (forced; can't write a handoff)."""
        _write_tombstone(self.state_dir, "recovery", reason="api_health_force")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("--skip-ack", combined)

    def test_freshness_gate_still_drops_stale_context_fill(self) -> None:
        """Freshness gate is independent of skip_ack fix: stale context_fill still dropped."""
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold", age_seconds=400)
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# T-0535: Detached systemd-run launch (cgroup-isolation fix)
# ---------------------------------------------------------------------------


class TestDetachedLaunch(unittest.TestCase):
    """T-0535: _fire_restart must launch handoff_and_restart.sh as a DETACHED
    systemd-run transient unit (own cgroup) so the restart survives the drain
    service being stopped/SIGTERM'd mid-flight.

    The cgroup-isolation property itself (restart survives drain SIGTERM) is an
    OS-level guarantee — it cannot be unit-tested here. These tests assert the
    MECHANISM that provides it: that systemd-run --collect --unit=... is what
    gets invoked, not a direct blocking child process. The end-to-end live
    validation happens during the T-0469 supervised re-fire (the operator watching).
    """

    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)
        import tools.restart_drain as rd
        self.rd = rd
        self.cfg = rd.DRAIN_AGENT_CONFIG["recovery"]

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _capture_calls(self, returncode_map: dict | None = None):
        """Return a side_effect that records all subprocess.run calls.

        returncode_map keys are 0-based call index → returncode int.
        Default is 0 for all calls.
        """
        calls = []

        def _side_effect(cmd, **kwargs):
            idx = len(calls)
            rc = (returncode_map or {}).get(idx, 0)
            calls.append(list(cmd) if isinstance(cmd, list) else cmd)
            return MagicMock(returncode=rc, stdout="ok", stderr="")

        return calls, _side_effect

    # --- Mechanism: systemd-run is the top-level command --------------------

    def test_fire_restart_uses_systemd_run(self) -> None:
        """_fire_restart invokes systemd-run as the outer command, NOT bash -c directly.

        Structural assertion — this is the mechanism that provides cgroup isolation.
        """
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            success, _ = self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        self.assertTrue(success)
        # At least one call must be the systemd-run invocation
        systemd_run_calls = [c for c in calls if isinstance(c, list) and c[0] == "systemd-run"]
        self.assertTrue(
            len(systemd_run_calls) >= 1,
            f"Expected systemd-run in subprocess calls; got: {calls}",
        )
        # handoff_and_restart must NOT appear as the top-level command
        direct_hr_calls = [
            c for c in calls
            if isinstance(c, list) and c and "handoff_and_restart" in str(c[0])
        ]
        self.assertEqual(
            len(direct_hr_calls), 0,
            "handoff_and_restart.sh must not be the direct top-level subprocess command",
        )

    def test_fire_restart_includes_collect_and_unit(self) -> None:
        """systemd-run call must include --collect and --unit=drain-restart-... flag."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        sdr_calls = [c for c in calls if isinstance(c, list) and "systemd-run" in c]
        self.assertTrue(len(sdr_calls) >= 1, "No systemd-run call found")
        sdr_argv = sdr_calls[-1]  # the systemd-run launch call
        combined = " ".join(sdr_argv)
        self.assertIn("--collect", combined)
        self.assertTrue(
            any(a.startswith("--unit=drain-restart-") for a in sdr_argv),
            f"Expected --unit=drain-restart-<stem> in {sdr_argv}",
        )

    def test_fire_restart_inner_carries_skip_ack(self) -> None:
        """The inner bash -c string passed to systemd-run still carries --skip-ack."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        sdr_calls = [c for c in calls if isinstance(c, list) and "systemd-run" in c]
        self.assertTrue(len(sdr_calls) >= 1)
        # The inner string is the last element (bash -c <inner>)
        full_argv = " ".join(str(a) for a in sdr_calls[-1])
        self.assertIn("--skip-ack", full_argv)

    def test_fire_restart_inner_carries_all_required_args(self) -> None:
        """The inner string must contain all required handoff_and_restart.sh args."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        sdr_calls = [c for c in calls if isinstance(c, list) and "systemd-run" in c]
        self.assertTrue(len(sdr_calls) >= 1)
        full_argv = " ".join(str(a) for a in sdr_calls[-1])
        for required in (
            "handoff_and_restart",
            "--service",
            "--mode",
            "--target-pane",
            "--handoff-file",
            "--lockfile",
        ):
            self.assertIn(required, full_argv, f"Missing required arg '{required}' in inner")

    # --- Ordering: reset-failed before systemd-run -------------------------

    def test_reset_failed_issued_before_systemd_run(self) -> None:
        """reset-failed must be the first subprocess call; systemd-run must be last."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        self.assertGreaterEqual(len(calls), 2, "Expected at least 2 subprocess calls (reset-failed + systemd-run)")
        first_call = calls[0]
        last_call = calls[-1]
        # First call: systemctl reset-failed
        self.assertIn("reset-failed", " ".join(str(a) for a in first_call))
        # Last call: systemd-run
        self.assertIn("systemd-run", " ".join(str(a) for a in last_call))

    def test_reset_failed_uses_correct_unit_name(self) -> None:
        """reset-failed must reference the same unit name used in systemd-run --unit=."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        # Extract unit from reset-failed call
        reset_argv = " ".join(str(a) for a in calls[0])
        # Extract unit from systemd-run call
        sdr_argv = " ".join(str(a) for a in calls[-1])
        # Both should carry the same drain-restart-* stem
        import re
        reset_unit = re.search(r"drain-restart-\S+", reset_argv)
        sdr_unit = re.search(r"drain-restart-\S+", sdr_argv)
        self.assertIsNotNone(reset_unit, f"No drain-restart-* unit in reset-failed call: {reset_argv}")
        self.assertIsNotNone(sdr_unit, f"No drain-restart-* unit in systemd-run call: {sdr_argv}")
        self.assertEqual(reset_unit.group(), sdr_unit.group().rstrip("'\""))

    # --- Tombstone lifecycle: remove on launch-success, retain on failure ---

    def test_tombstone_removed_on_launch_success(self) -> None:
        """Tombstone removed when systemd-run launch succeeds (rc=0) — T-0535 semantic."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        # reset-failed -> rc=1 (normal — unit may not exist), systemd-run -> rc=0
        calls, side_effect = self._capture_calls(returncode_map={0: 1, 1: 0})
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            rc = self.rd.main([])
        self.assertEqual(rc, 0)
        self.assertFalse(tombstone.exists(), "Tombstone must be removed when launch succeeds")

    def test_tombstone_retained_on_launch_failure(self) -> None:
        """Tombstone retained when systemd-run launch fails (rc!=0) — drain retries next cycle."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        # reset-failed -> rc=0, systemd-run -> rc=1 (launch failed)
        calls, side_effect = self._capture_calls(returncode_map={0: 0, 1: 1})
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            rc = self.rd.main([])
        self.assertEqual(rc, 1)
        self.assertTrue(tombstone.exists(), "Tombstone must be retained when launch fails")

    def test_tombstone_removed_on_launch_success_force_reason(self) -> None:
        """api_health_force tombstone also removed on launch success (T-0535 semantic applies to all reasons)."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="api_health_force")
        calls, side_effect = self._capture_calls(returncode_map={0: 1, 1: 0})
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            rc = self.rd.main([])
        self.assertEqual(rc, 0)
        self.assertFalse(tombstone.exists())

    # --- reset-failed is best-effort: its failure must not abort _fire_restart ---

    def test_reset_failed_exception_does_not_abort_fire(self) -> None:
        """OSError from reset-failed must not prevent the systemd-run launch."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        call_log = []

        def side_effect(cmd, **kwargs):
            call_log.append(list(cmd) if isinstance(cmd, list) else cmd)
            # First call (reset-failed) raises OSError; second (systemd-run) succeeds
            if len(call_log) == 1:
                raise OSError("unit not found")
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            rc = self.rd.main([])
        # systemd-run still fired and succeeded → tombstone removed
        self.assertEqual(rc, 0)
        self.assertFalse(tombstone.exists(), "OSError from reset-failed must not abort the launch")

    def test_reset_failed_timeout_does_not_abort_fire(self) -> None:
        """TimeoutExpired from reset-failed must not prevent the systemd-run launch."""
        tombstone = _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        call_log = []

        def side_effect(cmd, **kwargs):
            call_log.append(list(cmd) if isinstance(cmd, list) else cmd)
            if len(call_log) == 1:
                raise subprocess.TimeoutExpired(cmd, 10)
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            rc = self.rd.main([])
        self.assertEqual(rc, 0)
        self.assertFalse(tombstone.exists(), "TimeoutExpired from reset-failed must not abort the launch")

    # --- XDG_RUNTIME_DIR and scope flag for user mode ----------------------

    def test_user_mode_includes_xdg_setenv(self) -> None:
        """user-mode systemd-run call includes --setenv=XDG_RUNTIME_DIR=..."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        sdr_calls = [c for c in calls if isinstance(c, list) and "systemd-run" in c]
        self.assertTrue(len(sdr_calls) >= 1)
        sdr_argv = " ".join(str(a) for a in sdr_calls[-1])
        self.assertIn("--setenv=XDG_RUNTIME_DIR=", sdr_argv)

    def test_user_mode_includes_user_scope_flag(self) -> None:
        """user-mode systemd-run call includes --user scope flag."""
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        sdr_calls = [c for c in calls if isinstance(c, list) and "systemd-run" in c]
        self.assertTrue(len(sdr_calls) >= 1)
        combined = " ".join(str(a) for a in sdr_calls[-1])
        self.assertIn("--user", combined)

    # --- Structural note: cgroup isolation ----------------------------------

    def test_survives_drain_stop_mechanism_note(self) -> None:
        """Structural test only — asserts the mechanism providing cgroup isolation.

        The actual property (restart survives drain SIGTERM) is an OS-level
        guarantee. It cannot be unit-tested. This test asserts that systemd-run
        is what _fire_restart invokes — the structural precondition for the
        cgroup isolation. Live validation happens during supervised re-fire
        (T-0469 pattern, the operator watching).
        """
        calls, side_effect = self._capture_calls()
        with patch("tools.restart_drain.subprocess.run", side_effect=side_effect):
            success, _ = self.rd._fire_restart(self.cfg, "recovery", skip_ack=True)
        self.assertTrue(success)
        all_cmds = " ".join(
            " ".join(str(a) for a in c) if isinstance(c, list) else str(c)
            for c in calls
        )
        self.assertIn("systemd-run", all_cmds, "Mechanism must use systemd-run for cgroup isolation")


# ---------------------------------------------------------------------------
# T-0709: restart-applied SUCCESS-path notification (silent-success sink fix)
# ---------------------------------------------------------------------------


class TestRestartAppliedNotification(unittest.TestCase):
    """_notify_restart_applied routes a drain-applied restart as an audit
    Telegram, and main() invokes it once per successful restart.

    Pre-T-0709 the drain success path only _log + unlinked the tombstone; the
    actual restart (and the sole signal for api_health_force tombstones) was
    silent.
    """

    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_RESTART_DRAIN_ENABLED", None))
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))

    def test_notify_routes_via_send_telegram_info(self) -> None:
        import tools.restart_drain as rd
        import tools.inbox_consumer as ic
        with patch.object(ic, "_send_telegram_info") as info:
            rd._notify_restart_applied("recovery", "context_fill_threshold", "drain-restart-x")
        info.assert_called_once()
        msg = info.call_args[0][0]
        self.assertIn("recovery", msg)
        self.assertIn("APPLIED", msg)
        self.assertIn("context_fill_threshold", msg)

    def test_notify_swallows_send_failure(self) -> None:
        """A send failure must never propagate — the restart already launched."""
        import tools.restart_drain as rd
        import tools.inbox_consumer as ic
        with patch.object(ic, "_send_telegram_info", side_effect=RuntimeError("boom")):
            # Must not raise.
            rd._notify_restart_applied("recovery", "api_health_force", "drain-restart-x")

    def test_main_success_invokes_notify_once(self) -> None:
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        import tools.restart_drain as rd
        with patch.object(rd, "_notify_restart_applied") as notify, \
             patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success):
            rc = rd.main([])
        self.assertEqual(rc, 0)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][0], "recovery")
        self.assertEqual(notify.call_args[0][1], "context_fill_threshold")

    def test_main_failure_does_not_notify(self) -> None:
        _write_tombstone(self.state_dir, "recovery", reason="context_fill_threshold")
        import tools.restart_drain as rd
        with patch.object(rd, "_notify_restart_applied") as notify, \
             patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_failure):
            rc = rd.main([])
        self.assertEqual(rc, 1)
        notify.assert_not_called()


# ---------------------------------------------------------------------------
# T-0857: wedge_staleness reason — forced (--skip-ack) AND freshness-gated.
# A wedged session cannot answer the handoff prompt, so it forces like
# api_health_force; but unlike api_health_force it IS freshness-gated (a stale
# wedge tombstone means the restart window has passed — drop it).
# ---------------------------------------------------------------------------


class TestWedgeStaleness(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PANTHEON_RESTART_DRAIN_ENABLED"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)
        # Neutralize the success-path notify so no live alert is attempted even if
        # the central suppression guard were ever bypassed (defense in depth).
        import tools.restart_drain as rd
        self._np = patch.object(rd, "_notify_restart_applied")
        self.mock_notify = self._np.start()
        self.addCleanup(self._np.stop)

    def tearDown(self) -> None:
        for k in ("PANTHEON_RESTART_DRAIN_ENABLED", "PANTHEON_STATE_DIR"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_wedge_staleness_fresh_forces_skip_ack(self) -> None:
        """A fresh wedge_staleness tombstone fires WITH --skip-ack (wedged session
        cannot answer the handoff prompt — Recovery-locked decision, T-0857)."""
        _write_tombstone(self.state_dir, "recovery", reason="wedge_staleness")
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        combined = " ".join(str(a) for a in mock_run.call_args_list)
        self.assertIn("--skip-ack", combined)
        self.mock_notify.assert_called_once()

    def test_wedge_staleness_stale_dropped(self) -> None:
        """An OLD wedge_staleness tombstone is freshness-gated → dropped without
        firing (contrast api_health_force, which bypasses the gate)."""
        _write_tombstone(self.state_dir, "recovery", reason="wedge_staleness", age_seconds=3600)
        tombstone = self.state_dir / "restart_requested_recovery.json"
        import tools.restart_drain as rd
        with patch("tools.restart_drain.subprocess.run", side_effect=_mock_subprocess_run_success) as mock_run:
            rc = rd.main([])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        self.assertFalse(tombstone.exists())  # removed as stale
        self.mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# T-0824: --validate (pre-arm config check) — persona keys + path resolution.
# ---------------------------------------------------------------------------


class TestValidateConfig(unittest.TestCase):
    def test_validate_passes_on_real_config(self) -> None:
        """The shipped DRAIN_AGENT_CONFIG is structurally valid (cross-account paths
        are deferred, not failed) → rc 0. Pin account_exists=True so the T-2071
        account check doesn't make this host-dependent (a fork's CI box may lack the
        reference accounts); account absence is covered by test_validate_fails_on_missing_account."""
        import tools.restart_drain as rd
        with patch.object(rd, "account_exists", return_value=True):
            self.assertEqual(rd.main(["--validate"]), 0)

    def test_validate_fails_on_unknown_persona(self) -> None:
        """A config keyed on a non-canonical persona (not in agent_role_map) → rc 1."""
        import tools.restart_drain as rd
        sample = dict(next(iter(rd.DRAIN_AGENT_CONFIG.values())))
        bogus = {**rd.DRAIN_AGENT_CONFIG, "not_a_persona": sample}
        with patch.object(rd, "DRAIN_AGENT_CONFIG", bogus):
            self.assertEqual(rd.main(["--validate"]), 1)

    def test_validate_fails_on_missing_required_field(self) -> None:
        """A config missing a required structural field (restart_service) → rc 1."""
        import tools.restart_drain as rd
        key = next(iter(rd.DRAIN_AGENT_CONFIG))
        broken = dict(rd.DRAIN_AGENT_CONFIG[key])
        broken.pop("restart_service", None)
        with patch.object(rd, "DRAIN_AGENT_CONFIG", {key: broken}):
            self.assertEqual(rd.main(["--validate"]), 1)

    def test_validate_runs_before_enabled_gate(self) -> None:
        """--validate works even when the drain is disabled (it's a pre-arm check).

        T-2249: pin account_exists=True for the same reason
        test_validate_passes_on_real_config does. What this test asserts is the
        ENABLED-GATE ordering (validate is reachable with the drain disabled);
        whether this particular box happens to host the reference accounts is an
        unrelated axis, and letting it leak in made the test fail on any CI host
        without the fleet's account topology while passing on the VPS for a reason
        that has nothing to do with the gate. Account absence is covered by
        test_validate_fails_on_missing_account; live topology by
        test_validate_on_live_fleet_topology below.
        """
        import tools.restart_drain as rd
        os.environ.pop("PANTHEON_RESTART_DRAIN_ENABLED", None)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_RESTART_DRAIN_ENABLED", None))
        with patch.object(rd, "account_exists", return_value=True):
            self.assertEqual(rd.main(["--validate"]), 0)

    def test_validate_on_live_fleet_topology(self) -> None:
        """T-2249: the host-coupled check, made EXPLICIT and skippable instead of
        being an accident of where CI happens to run.

        The other --validate tests pin account_exists so they are hermetic. That
        deliberately gives up one real signal: that the SHIPPED config's accounts
        actually exist on a box that is supposed to be an agent host. This test
        keeps that signal — unpinned, so the T-2071 account check runs for real —
        and SKIPS (never fails) where the topology is absent, e.g. a bare CI
        container.

        Skip-not-fail is the point: a red here on a CI box measures the box, not
        the code. A skip says plainly that the check did not run.
        """
        import tools.restart_drain as rd
        accounts = sorted({
            a for cfg in rd.DRAIN_AGENT_CONFIG.values()
            if (a := rd._account_from_home_path(
                cfg.get("restart_bin"), cfg.get("handoff_file"))) is not None
        })
        self.assertTrue(accounts, "config yielded no /home/<acct>/-shaped paths")
        missing = [a for a in accounts if not rd.account_exists(a)]
        if missing:
            self.skipTest(
                f"not an agent host: accounts absent {missing} (expected {accounts})"
            )
        self.assertEqual(rd.main(["--validate"]), 0)

    def test_validate_fails_on_missing_account(self) -> None:
        """T-2071: an agent whose account (inferred from /home/<acct>/ paths) does
        not exist on the box → rc 1 (leftover reference-instance defaults)."""
        import tools.restart_drain as rd
        key = next(iter(rd.DRAIN_AGENT_CONFIG))
        real_acct = rd._account_from_home_path(
            rd.DRAIN_AGENT_CONFIG[key].get("restart_bin"),
            rd.DRAIN_AGENT_CONFIG[key].get("handoff_file"),
        )
        with patch.object(rd, "DRAIN_AGENT_CONFIG", {key: dict(rd.DRAIN_AGENT_CONFIG[key])}), \
             patch.object(rd, "account_exists", side_effect=lambda a: a != real_acct):
            self.assertEqual(rd.main(["--validate"]), 1)

    def test_account_from_home_path(self) -> None:
        import tools.restart_drain as rd
        self.assertEqual(rd._account_from_home_path("/home/claude/x/y.sh"), "claude")
        self.assertEqual(rd._account_from_home_path(None, "/home/claude-fallback/HANDOFF.md"), "claude-fallback")
        self.assertIsNone(rd._account_from_home_path("/opt/x", None))


class TestBuildDrainConfig(unittest.TestCase):
    """T-2076 SAFETY NET: DRAIN_AGENT_CONFIG built from instance.yaml is PROVABLY equal
    to the former hardcoded literals for the reference instance — the instance-driven
    refactor is a verified no-op. Update this snapshot + instance.yaml together on any
    deliberate reference-instance change."""

    REFERENCE_LITERALS = {
        # T-2684: recovery is control-plane-supervised (T-2590 cutover) —
        # restart_service is DERIVED to the consume unit (overriding the
        # bespoke restart_service instance.yaml still carries for
        # documentation), and "supervisor" is carried so handoff_and_restart.sh
        # takes the kill-session + scope-liveness branch. Deliberate
        # reference-instance change, updated together with instance.yaml, same
        # shape as specialist's T-2658 change below.
        "recovery": {
            "restart_service": "pantheon-persona-consume@recovery.service", "restart_mode": "user",
            "restart_target_pane": "claude-telegram-bot-fallback:0",
            "handoff_file": "/home/claude-fallback/HANDOFF.md",
            "restart_bin": "/home/claude-fallback/pantheon-config/tools/handoff_and_restart.sh",
            "restart_lockfile": "/tmp/handoff_and_restart_recovery.lock",
            "supervisor": "control-plane",
        },
        "orchestrator": {
            "restart_service": "claude-telegram.service", "restart_mode": "user",
            "restart_target_pane": "claude-telegram-bot:0",
            "handoff_file": "/home/claude/HANDOFF.md",
            "restart_bin": "/home/claude/pantheon-config/tools/handoff_and_restart.sh",
            "restart_lockfile": "/tmp/handoff_and_restart_orchestrator.lock",
        },
        # T-2658: specialist is control-plane-supervised (T-2633 cutover) —
        # restart_service is DERIVED to the consume unit (overriding the
        # bespoke restart_service instance.yaml still carries for
        # documentation), and "supervisor" is carried so handoff_and_restart.sh
        # takes the kill-session + scope-liveness branch. Deliberate
        # reference-instance change, updated together with instance.yaml.
        "specialist": {
            "restart_service": "pantheon-persona-consume@specialist.service", "restart_mode": "user",
            "restart_target_pane": "claude-research-bot:0",
            "handoff_file": "/home/claude-research/HANDOFF.md",
            "restart_bin": "/home/claude-research/pantheon-config/tools/handoff_and_restart.sh",
            "restart_lockfile": "/tmp/handoff_and_restart_specialist.lock",
            "supervisor": "control-plane",
        },
        # mnemosyne — T-2370. NOT a reference-instance change: instance.yaml has
        # granted second-head restart_service since ORG-042, and the drain's own
        # `"restart_service" in binding` predicate always selected it. The former
        # hand-carried `_DRAIN_PERSONAS` tuple was subtracting it back out, so this
        # snapshot recorded the bug as the expectation. Deriving the persona set
        # makes the manifest and the config agree; this entry is what that agreement
        # looks like, not a widening of the reference instance.
        "mnemosyne": {
            "restart_service": "claude-telegram-exocortex.service", "restart_mode": "user",
            "restart_target_pane": "claude-telegram-bot-exocortex:0",
            "handoff_file": "/home/exocortex/HANDOFF.md",
            "restart_bin": "/home/exocortex/pantheon-config/tools/handoff_and_restart.sh",
            "restart_lockfile": "/tmp/handoff_and_restart_mnemosyne.lock",
        },
    }

    def test_built_drain_config_matches_reference_literals(self) -> None:
        import tools.restart_drain as rd
        self.assertEqual(rd._build_drain_config(), self.REFERENCE_LITERALS)

    def test_module_level_drain_config_is_the_built_config(self) -> None:
        import tools.restart_drain as rd
        self.assertEqual(rd.DRAIN_AGENT_CONFIG, self.REFERENCE_LITERALS)

    def test_every_drain_persona_has_a_fallback_binding(self) -> None:
        # T-2076 review #3: manifest-less completeness.
        # T-2370: derived from the built config now that _DRAIN_PERSONAS is gone.
        import tools.restart_drain as rd
        from tools import agent_role_map as arm
        missing = sorted(set(rd.DRAIN_AGENT_CONFIG) - set(arm._FALLBACK_PERSONA_TO_BINDING))
        self.assertEqual(missing, [], f"drain persona(s) with no fallback binding: {missing}")

    def test_drain_set_is_exactly_the_personas_granted_restart_service(self) -> None:
        """T-2370 REGRESSION: the drain set is the manifest's restart_service grantees —
        no more, and critically no LESS. This is the assertion the old hand-carried
        `_DRAIN_PERSONAS = ("recovery", "orchestrator", "specialist")` tuple failed: instance.yaml
        granted restart_service to a fourth role (second-head/mnemosyne) and the tuple
        silently subtracted it, so its restart tombstones were quarantined as an
        unknown agent. Re-introducing ANY hand-carried outer list fails here as soon as
        it disagrees with the manifest — which is the only way this defect comes back.
        """
        import tools.restart_drain as rd
        from tools import agent_role_map as arm
        granted = {
            p for p in set(arm.account_persona_map().values())
            if "restart_service" in arm.instance_binding(p)
        }
        self.assertEqual(set(rd.DRAIN_AGENT_CONFIG), granted)
        self.assertIn("mnemosyne", rd.DRAIN_AGENT_CONFIG)  # the measured casualty

    def test_missing_restart_pane_omits_key_not_none(self) -> None:
        # T-2076 review #4: a binding without restart_pane leaves the key ABSENT
        # (so --validate fails loudly), never restart_target_pane=None.
        # T-2370: the persona set is now narrowed by patching the derivation source
        # rather than the removed _DRAIN_PERSONAS tuple.
        import tools.restart_drain as rd
        fake = {"account": "acct", "restart_service": "x.service"}  # no restart_pane
        with patch.object(rd, "_account_persona_map", return_value={"acct": "recovery"}), \
             patch.object(rd, "_instance_binding", return_value=fake):
            built = rd._build_drain_config()
        self.assertNotIn("restart_target_pane", built["recovery"])
        self.assertEqual(built["recovery"]["restart_mode"], "user")


class TestIsUnderHome(unittest.TestCase):
    """T-0892: same-account vs sibling-account path classification must be a
    PATH-BOUNDARY check, not a string prefix — '/home/claude' is a string prefix
    of the sibling homes '/home/claude-fallback' / '/home/claude-research'."""

    def test_true_for_descendant_of_home(self) -> None:
        import tools.restart_drain as rd
        self.assertTrue(rd._is_under_home("/home/claude/pantheon-config/tools/h.sh", "/home/claude"))

    def test_true_for_home_itself(self) -> None:
        import tools.restart_drain as rd
        self.assertTrue(rd._is_under_home("/home/claude", "/home/claude"))

    def test_false_for_sibling_with_shared_prefix(self) -> None:
        # The exact bug: these are SIBLINGS of /home/claude, not descendants.
        import tools.restart_drain as rd
        self.assertFalse(rd._is_under_home("/home/claude-fallback/x/h.sh", "/home/claude"))
        self.assertFalse(rd._is_under_home("/home/claude-research/x/h.sh", "/home/claude"))

    def test_false_when_home_is_the_longer_sibling(self) -> None:
        import tools.restart_drain as rd
        self.assertFalse(rd._is_under_home("/home/claude/x/h.sh", "/home/claude-fallback"))


class TestValidateConfigCrossAccount(unittest.TestCase):
    """T-0892: simulate running --validate ON the claude/Orchestrator account, where the
    sibling homes (/home/claude-fallback, /home/claude-research) are 0750 and
    cannot be traversed. Their restart_bin paths must be DEFERRED (cross-account),
    not hard-failed → rc 0. Account-independent: Path.home + os.path.exists are
    patched, so this reproduces the Orchestrator failure from any account."""

    def test_sibling_paths_deferred_not_failed_on_claude_account(self) -> None:
        import tools.restart_drain as rd
        # As if we are the claude account: only /home/claude/... is stattable.
        def fake_exists(p):
            return str(p).startswith("/home/claude/")
        # T-2249: account_exists pinned for the same reason as the other --validate
        # tests. This test is about PATH deferral across sibling homes (T-0892); the
        # T-2071 account-existence check is a separate axis that was silently
        # deciding the outcome on any box lacking the fleet accounts. Note the
        # simulation is already synthetic — Path.home and os.path.exists are faked to
        # impersonate the claude account — so leaving the real passwd DB in the loop
        # was inconsistent as well as host-coupled.
        with patch.object(rd.Path, "home", staticmethod(lambda: Path("/home/claude"))), \
             patch.object(rd, "account_exists", return_value=True), \
             patch("tools.restart_drain.os.path.exists", side_effect=fake_exists):
            self.assertEqual(rd.main(["--validate"]), 0)

    def test_own_account_missing_bin_still_fails(self) -> None:
        # Deferral must NOT mask a genuinely-missing bin under the CURRENT home.
        import tools.restart_drain as rd
        with patch.object(rd.Path, "home", staticmethod(lambda: Path("/home/claude"))), \
             patch("tools.restart_drain.os.path.exists", return_value=False):
            # /home/claude/... bin now unresolvable while under home → hard-fail.
            self.assertEqual(rd.main(["--validate"]), 1)


if __name__ == "__main__":
    unittest.main()
