"""Unit tests for tools/activity_monitor.py — ORG-011 Phase B / T-0223.

Run from repo root:
    python3 -m unittest tools.test_activity_monitor -v

Covers:
  - cooldown semantics (recent / old / missing / malformed state)
  - latest_commit_ts + latest_ledger_entry_ts_for_agent
  - count_actionable_tickets frontmatter parser
  - detect_await_state regex
  - operator_last_inbound_age_seconds parser
  - check_p1a + check_p1b composition
  - main flow with mocked subprocess + urlopen
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.activity_monitor as am
from tools.activity_monitor import (
    AGENT_CONFIG,
    P1A_IDLE_THRESHOLD_SECONDS,
    P1B_OPERATOR_ABSENT_THRESHOLD_SECONDS,
    P1D_RATE_LIMIT_AGE_SECONDS,
    P1D_COOLDOWN_SECONDS,
    ACTIVITY_MONITOR_COOLDOWN_SECONDS,
    AWAIT_STATE_REGEX,
    RATE_LIMIT_REGEX,
    capture_pane,
    check_p1d,
    count_actionable_tickets,
    detect_await_state,
    detect_rate_limit_in_pane,
    in_cooldown,
    latest_rate_limit_assistant_ts,
    operator_last_inbound_age_seconds,
    main,
    write_poke_state,
)


# ---------------------------------------------------------------------------
# AGENT_CONFIG sanity
# ---------------------------------------------------------------------------


class TestAgentConfig(unittest.TestCase):
    def test_orchestrator_present(self) -> None:
        self.assertIn("orchestrator", AGENT_CONFIG)
        cfg = AGENT_CONFIG["orchestrator"]
        self.assertEqual(cfg["tmux_target_pane"], "claude-telegram-bot:0")
        self.assertEqual(cfg["sudo_user"], "claude")
        self.assertEqual(cfg["ledger_agent_id"], "orchestrator")
        self.assertIn("orchestrator", cfg["ticket_targets"])
        # P1.c (ORG-037 Layer 3): orchestrator must carry a teams_dir to be eligible.
        self.assertEqual(cfg["teams_dir"], Path("/home/claude/.claude/teams"))

    def test_specialist_recovery_not_yet_covered(self) -> None:
        # ORG-011 Phase B is orchestrator-only initially; specialist + recovery come post-empirical.
        self.assertNotIn("specialist", AGENT_CONFIG)
        self.assertNotIn("recovery", AGENT_CONFIG)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestInCooldown(unittest.TestCase):
    def test_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(in_cooldown(Path(tmp) / "x.json", 1800))

    def test_recent_state_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(json.dumps({"ts": int(time.time()) - 60}))
            self.assertTrue(in_cooldown(p, 1800))

    def test_old_state_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(json.dumps({"ts": int(time.time()) - 3600}))
            self.assertFalse(in_cooldown(p, 1800))

    def test_malformed_state_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text("not json")
            self.assertFalse(in_cooldown(p, 1800))


class TestWritePokeState(unittest.TestCase):
    def test_writes_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "state.json"
            write_poke_state(p, "orchestrator", "p1a")
            data = json.loads(p.read_text())
            self.assertEqual(data["agent"], "orchestrator")
            self.assertEqual(data["predicate"], "p1a")
            self.assertIsInstance(data["ts"], int)
            # Legacy call → consecutive_ineffective defaults to 0, no pre-mtime.
            self.assertEqual(data["consecutive_ineffective"], 0)
            self.assertNotIn("pre_poke_jsonl_mtime", data)

    def test_writes_bounded_poke_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            write_poke_state(p, "orchestrator", "p1c",
                             pre_poke_jsonl_mtime=12345, consecutive_ineffective=2,
                             escalated=True)
            data = am.read_poke_state(p)
            self.assertEqual(data["pre_poke_jsonl_mtime"], 12345)
            self.assertEqual(data["consecutive_ineffective"], 2)
            self.assertTrue(data["escalated"])

    def test_read_poke_state_absent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(am.read_poke_state(Path(tmp) / "nope.json"))


class TestPokeEffectivenessDecision(unittest.TestCase):
    """T-1363 — bounded-poke: suppress once pokes stop advancing the session.

    The poke-once-vs-skip fork is the max_ineffective parameter (N), so these
    pin both N=1 (poke-once-then-escalate) and N=0 (skip-pokes) ends.
    """

    def test_first_fire_no_prev_pokes(self) -> None:
        suppress, consec, _ = am.poke_effectiveness_decision(None, 1000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)

    def test_effective_poke_resets_counter(self) -> None:
        # prior poke recorded pre-mtime 1000; jsonl now 2000 → it advanced → worked.
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 3}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 2000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)

    def test_ineffective_increments_below_threshold(self) -> None:
        # pre-mtime 1000, jsonl still 1000 → didn't advance → ineffective. N=2 → no suppress yet.
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 0}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 2)
        self.assertFalse(suppress)
        self.assertEqual(consec, 1)

    def test_ineffective_suppresses_at_threshold_n1(self) -> None:
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 0}
        suppress, consec, reason = am.poke_effectiveness_decision(prev, 1000, "p1c", 1)
        self.assertTrue(suppress)
        self.assertEqual(consec, 1)
        self.assertIn("suppress", reason)

    def test_accumulates_across_cycles(self) -> None:
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 2}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 3)
        self.assertTrue(suppress)   # 2 + 1 = 3 >= 3
        self.assertEqual(consec, 3)

    def test_n_zero_skips_pokes_entirely(self) -> None:
        # The 'poke less' extreme: N=0 → suppress on the very first fire, never poke.
        suppress, consec, _ = am.poke_effectiveness_decision(None, 1000, "p1c", 0)
        self.assertTrue(suppress)
        self.assertEqual(consec, 0)

    def test_predicate_mismatch_resets(self) -> None:
        # Last poke was a different predicate → don't carry its counter.
        prev = {"predicate": "p1a", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 5}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)


class TestDispatchPoke(unittest.TestCase):
    """T-1363 — _dispatch_poke: legacy passthrough (flag off) + bounded suppress."""

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "pantheon_config_path": Path("/x")}

    def test_legacy_path_pokes_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()
            self.assertEqual(am.read_poke_state(sp)["predicate"], "p1a")

    def test_legacy_path_send_failure_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=False):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 1)

    def test_bounded_first_fire_pokes_and_records_premtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=False), \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()
            data = am.read_poke_state(sp)
            self.assertEqual(data["pre_poke_jsonl_mtime"], 5000)
            self.assertEqual(data["consecutive_ineffective"], 0)

    def test_bounded_suppresses_when_prior_ineffective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            # Prior poke recorded pre-mtime 5000; jsonl STILL 5000 → ineffective.
            am.write_poke_state(sp, "orchestrator", "p1c", pre_poke_jsonl_mtime=5000,
                                consecutive_ineffective=0)
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=False), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_not_called()  # SUPPRESSED — the token-saving behavior
            self.assertTrue(am.read_poke_state(sp)["escalated"])
            alert.assert_called_once()  # escalates to Recovery instead of re-poking

    def test_bounded_cyber_blocked_suppresses_even_when_jsonl_advanced(self) -> None:
        """The pre-poke gate: a cyber-blocked session emits refusals that ADVANCE
        the jsonl, which the effectiveness signal would read as 'poke worked'.
        The gate must suppress BEFORE that check — never poke a cyber-blocked
        session (it needs a restart)."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            # Prior poke at mtime 5000; jsonl ADVANCED to 9000 (refusals) → the
            # effectiveness check alone would call this "effective" and re-poke.
            am.write_poke_state(sp, "orchestrator", "p1c", pre_poke_jsonl_mtime=5000,
                                consecutive_ineffective=0)
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_not_called()  # gate wins over the (fooled) effectiveness signal
            self.assertTrue(am.read_poke_state(sp)["escalated"])
            alert.assert_called_once()  # cyber → escalate to Recovery for restart

    def test_escalation_alerts_once_then_stays_quiet(self) -> None:
        """Bounded escalation: alert Recovery ONCE on the escalated transition, NOT
        every subsequent cycle (else the alert is the same unbounded-repeat the
        poke bounding was meant to kill)."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True), \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                # Three consecutive cycles, cyber-blocked the whole time.
                for _ in range(3):
                    am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                      bounded_enabled=True, max_ineffective=1)
            self.assertEqual(alert.call_count, 1, "alert fires once, then bounded-quiet")

    def test_escalation_routing_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True), \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert", side_effect=RuntimeError("recovery down")), \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0, "escalation routing failure must not wedge the monitor")
            self.assertTrue(am.read_poke_state(sp)["escalated"])  # still marks escalated

    def test_legacy_path_ignores_cyber_gate(self) -> None:
        """Ship-dark: with bounded off, the pre-poke gate does NOT run — legacy
        behavior is byte-for-byte unchanged even for a cyber-blocked session."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=True) as gate:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()  # legacy pokes regardless
            gate.assert_not_called()   # gate not even consulted when bounded off


# ---------------------------------------------------------------------------
# await-state regex
# ---------------------------------------------------------------------------


class TestDetectAwaitState(unittest.TestCase):
    def test_matches_waiting_on(self) -> None:
        self.assertTrue(detect_await_state("I'm waiting on your call."))

    def test_matches_awaiting_your(self) -> None:
        self.assertTrue(detect_await_state("Standing by; awaiting your ratification."))

    def test_matches_ready_when_you(self) -> None:
        self.assertTrue(detect_await_state("Ready when you say go."))

    def test_no_match(self) -> None:
        self.assertFalse(detect_await_state("Implementation landed at d4e0f4d. Tests green."))

    def test_case_insensitive(self) -> None:
        self.assertTrue(detect_await_state("WAITING ON your decision"))


# ---------------------------------------------------------------------------
# count_actionable_tickets
# ---------------------------------------------------------------------------


def _ticket_text(target: str, depends_on: str = "[]") -> str:
    return (
        "---\n"
        f"id: T-9999\n"
        f"target: {target}\n"
        f"depends_on: {depends_on}\n"
        "---\n"
        "# test\n"
    )


class TestCountActionableTickets(unittest.TestCase):
    """Cross-account read via sudo + awk pipeline. Mock subprocess.run for fixtures."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_counts_orchestrator_target_only(self) -> None:
        # awk pipeline output: one line per ticket "<path>|<target>|<depends_on>|<not_before>|<labels>"
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|recovery|[]||\n"
            "/r/tickets/active/T-1002.md|orchestrator|[]||\n"
            "/r/tickets/active/T-1003.md|specialist|[]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator", "orchestrator"}), "claude")
        self.assertEqual(count, 2)

    def test_skips_depends_on_unmet(self) -> None:
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[T-0099]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_skips_not_before_in_future(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stdout = (
            f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{future}|\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_counts_not_before_in_past(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stdout = f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{past}|\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_counts_not_before_absent(self) -> None:
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]|null|\n"
            "/r/tickets/inbox/T-1002.md|orchestrator|[]|~|\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 3)

    def test_date_only_not_before_parsed_as_midnight_utc(self) -> None:
        future_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        past_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        stdout = (
            f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{future_date}|\n"
            f"/r/tickets/inbox/T-1001.md|orchestrator|[]|{past_date}|\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_unparseable_not_before_counted(self) -> None:
        # Defensive: unparseable values fall back to existing-behavior (counted).
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]|garbage-not-a-date|\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_returns_zero_on_subprocess_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("", returncode=1)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_returns_zero_on_empty_output(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_returns_zero_on_subprocess_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="find", timeout=30)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_sudo_user_threads_into_argv(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude-research")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-nu", "claude-research"])

    def test_skips_interactive_required_label(self) -> None:
        # Ticket with interactive-required label must NOT be counted even when
        # target matches, deps are empty, and not_before is absent.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||[activity-monitor, interactive-required]\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_counts_non_interactive_label(self) -> None:
        # Ticket without interactive-required label must be counted normally.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||[activity-monitor, p1a]\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_interactive_required_in_mixed_queue(self) -> None:
        # Queue: one interactive-required (skipped) + one plain (counted) -> 1.
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||[interactive-required]\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||[p1a, predicate-fix]\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# bridge inbound age parser
# ---------------------------------------------------------------------------


class TestOperatorLastInboundAge(unittest.TestCase):
    def test_recent_inbound_short_age(self) -> None:
        now = datetime.now(timezone.utc)
        iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        # Reformat to bridge style (+00:00 instead of +0000)
        iso = iso[:-2] + ":" + iso[-2:]
        health = {"last_inbound_at": {"orchestrator": iso}}
        age = operator_last_inbound_age_seconds(health, "orchestrator")
        self.assertIsNotNone(age)
        self.assertLess(age, 10)

    def test_old_inbound_long_age(self) -> None:
        health = {"last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"}}
        age = operator_last_inbound_age_seconds(health, "orchestrator")
        self.assertIsNotNone(age)
        self.assertGreater(age, 30 * 24 * 3600)  # > 30 days

    def test_missing_key_returns_none(self) -> None:
        health = {"last_inbound_at": {}}
        self.assertIsNone(operator_last_inbound_age_seconds(health, "orchestrator"))

    def test_malformed_string_returns_none(self) -> None:
        health = {"last_inbound_at": {"orchestrator": "not-a-timestamp"}}
        self.assertIsNone(operator_last_inbound_age_seconds(health, "orchestrator"))


# ---------------------------------------------------------------------------
# check_p1a
# ---------------------------------------------------------------------------


class TestCheckP1a(unittest.TestCase):
    def _cfg(self, repo_path: Path) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": repo_path,
            "bridge_inbound_key": "orchestrator",
        }

    def test_idle_with_queue_fires(self) -> None:
        old_ts = int(time.time()) - 7200  # 2h ago
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=old_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_recent_ledger_suppresses_fire(self) -> None:
        """Recent ledger entry by Orchestrator suppresses fire."""
        recent_ts = int(time.time()) - 60  # 1m ago
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=recent_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_recent_jsonl_mtime_suppresses_fire(self) -> None:
        """Jsonl mtime within threshold suppresses fire even when ledger is old.

        Catches the b9-orchestration case: Orchestrator is producing tokens (jsonl mtime
        bumping) but not ledger-ing during mid-cycle.
        """
        recent_jsonl = int(time.time()) - 30  # 30s ago
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=recent_jsonl), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_idle_but_empty_queue_does_not_fire(self) -> None:
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=0), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "count_actionable_tickets", return_value=0):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_no_activity_signal_fail_open(self) -> None:
        # All probes return None → fail-open (don't fire).
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=None), \
             patch.object(am, "latest_jsonl_mtime", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_commit_ts_not_consulted_per_t0283(self) -> None:
        """Per T-0283: latest_commit_ts was dropped because it's repo-wide and
        not Orchestrator-tagged. Recovery-side commits used to keep Orchestrator looking active.

        This test asserts the function no longer calls latest_commit_ts even
        if it's monkey-patched to return recent activity.
        """
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_commit_ts") as commit_probe, \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=old_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            commit_probe.return_value = int(time.time()) - 60  # recent, but irrelevant
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        # P1.a fires because ledger+jsonl are old, regardless of commit.
        self.assertTrue(fired)
        # latest_commit_ts must NOT have been called.
        commit_probe.assert_not_called()


# ---------------------------------------------------------------------------
# T-0644: blocked_by:event exclusion + terminal-state cooldown stretch.
# ---------------------------------------------------------------------------


class TestCountActionableTicketsT0644(unittest.TestCase):
    """T-0644: extend count_actionable_tickets to also exclude tickets with
    `blocked_by: event` frontmatter (event-driven holds gated on a structural
    witness, not on HO-availability).
    """

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_skips_blocked_by_event(self) -> None:
        # New 6-field shape: path|target|deps|nb|labels|blocked_by
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]|||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]|||event\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1, "blocked_by:event must be excluded from count")

    def test_blocked_by_other_values_counted(self) -> None:
        # Only the literal 'event' value triggers the exclusion. depends_on
        # (which is a separate gate already enforced) shouldn't double-trip.
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||labels|other\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 2)

    def test_backward_compat_5_field_lines(self) -> None:
        # Pre-T-0644 fixtures (5-field, no blocked_by column) must still parse.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)


class TestLatestTerminalStateLedger(unittest.TestCase):
    """T-0644: scan ledger for HO-tagged entries whose action contains
    'terminal_state' or 'queue_audit'."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_returns_ts_of_terminal_state_entry(self) -> None:
        # Newest-first (the tool tacs the file).
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-28T03:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
            '{"agent":"orchestrator","ts":"2026-05-27T22:00:00Z","action":"t0123_resolved_x"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        from datetime import datetime, timezone
        expected = int(datetime(2026, 5, 28, 3, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_returns_none_when_no_terminal_state_entry(self) -> None:
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-28T03:00:00Z","action":"t0644_filed"}\n'
            '{"agent":"orchestrator","ts":"2026-05-27T22:00:00Z","action":"t0123_resolved_x"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)

    def test_queue_audit_action_also_matches(self) -> None:
        stdout = '{"agent":"orchestrator","ts":"2026-05-28T01:00:00Z","action":"ho_queue_audit_post_arc"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNotNone(ts)

    def test_other_agent_entries_skipped(self) -> None:
        stdout = '{"agent":"recovery","ts":"2026-05-28T01:00:00Z","action":"recovery_terminal_state_audit"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)

    def test_command_reads_ledger_union_subfolder_shards(self) -> None:
        # T-0148: the probe must read across the per-week subfolder shards
        # (ledger/*/*.jsonl) plus the legacy flat file, not just {year}.jsonl.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        shell_cmd = run.call_args[0][0][-1]
        self.assertIn("/r/ledger/*/*.jsonl", shell_cmd)
        self.assertIn("/r/ledger/2026.jsonl", shell_cmd)
        self.assertIn("tac", shell_cmd)

    def test_returns_max_ts_not_first_when_stale_emitted_first(self) -> None:
        # T-0148 regression (review finding #1): the union emits legacy lines
        # last → tac yields legacy FIRST, so in a transition window a STALE
        # (older) matching entry can appear before the newest. The probe must
        # return max(ts), NOT first-match. OLD entry first, NEW entry second.
        from datetime import datetime, timezone
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-01T00:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
            '{"agent":"orchestrator","ts":"2026-05-28T12:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        expected = int(datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)


class TestLatestLedgerEntryTs(unittest.TestCase):
    """T-0148: latest_ledger_entry_ts_for_agent reads the ledger union and must
    return the newest matching ts regardless of source-concatenation order."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_returns_max_ts_not_first_when_stale_emitted_first(self) -> None:
        # Review finding #1, sibling probe: OLD entry first, NEW entry second →
        # must return the NEW ts (max), not the first-after-tac stale one.
        from datetime import datetime, timezone
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-01T00:00:00Z","action":"x"}\n'
            '{"agent":"orchestrator","ts":"2026-05-29T09:00:00Z","action":"y"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_ledger_entry_ts_for_agent(Path("/r"), "claude", "orchestrator")
        expected = int(datetime(2026, 5, 29, 9, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_other_agent_skipped_returns_none(self) -> None:
        stdout = '{"agent":"recovery","ts":"2026-05-29T09:00:00Z","action":"y"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_ledger_entry_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)


class TestLedgerUnionTacCmd(unittest.TestCase):
    """T-0148: _ledger_union_tac_cmd shell-string shape."""

    def test_includes_subfolder_glob_and_legacy_and_tac(self) -> None:
        cmd = am._ledger_union_tac_cmd(Path("/repo"), 500)
        self.assertIn("/repo/ledger/*/*.jsonl", cmd)
        self.assertIn("/repo/ledger/2026.jsonl", cmd)
        self.assertIn("| tac | head -n 500", cmd)

    def test_n_parameter_threaded_through(self) -> None:
        cmd = am._ledger_union_tac_cmd(Path("/repo"), 42)
        self.assertIn("head -n 42", cmd)


class TestCheckP1aTerminalStateStretch(unittest.TestCase):
    """T-0644: when the latest agent ledger entry is a terminal-state audit,
    P1.a uses ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS instead of
    the normal idle threshold, so we don't re-poke HO every cycle on the
    same terminal-state ack."""

    def _cfg(self) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": Path("/x"),
            "bridge_inbound_key": "orchestrator",
        }

    def test_terminal_state_ack_within_4hr_suppresses_fire(self) -> None:
        # ledger entry 90 min ago (>60 min default threshold; would normally fire).
        # But it's a terminal-state ack, so stretched threshold (4hr) applies → no fire.
        ack_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_terminal_state_ack_past_4hr_does_fire(self) -> None:
        # ack was 5hr ago → past stretched threshold → fire.
        ack_ts = int(time.time()) - 5 * 60 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_jsonl_activity_after_terminal_state_uses_normal_threshold(self) -> None:
        # ack was 5hr ago, but jsonl mtime is 90 min ago — the latest activity
        # is the jsonl write, which is NOT a terminal-state ack. Use normal
        # 60min threshold; 90min idle → fire.
        old_ack_ts = int(time.time()) - 5 * 60 * 60
        jsonl_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=jsonl_ts), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=old_ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_no_terminal_state_uses_normal_threshold(self) -> None:
        # Old ledger, no terminal-state ack at all → normal 60min threshold.
        old_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)


# ---------------------------------------------------------------------------
# check_p1b
# ---------------------------------------------------------------------------


class TestCheckP1b(unittest.TestCase):
    def _cfg(self, repo_path: Path) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": repo_path,
            "bridge_inbound_key": "orchestrator",
        }

    def test_all_three_predicates_fire(self) -> None:
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body",
                          return_value="Standing by; awaiting your ratification."), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_operator_recent_inbound_suppresses(self) -> None:
        # Recent inbound (now)
        now = datetime.now(timezone.utc)
        iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        iso = iso[:-2] + ":" + iso[-2:]
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": iso},
             }):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_no_await_state_suppresses(self) -> None:
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body",
                          return_value="Implementation landed; tests green."), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_empty_queue_suppresses(self) -> None:
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body",
                          return_value="awaiting your call"), \
             patch.object(am, "count_actionable_tickets", return_value=0):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_bridge_unreachable_fail_open(self) -> None:
        with patch.object(am, "fetch_bridge_health", return_value=None):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    # ---- T-0297: tool_use interactive-menu await-state ----

    def test_tool_use_askuserquestion_fires_via_t0297(self) -> None:
        """Latest assistant turn = AskUserQuestion tool_use → P1.b fires even
        when text-content body doesn't match the await regex (or is empty)."""
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body", return_value=""), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value="AskUserQuestion"), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_tool_use_enterplanmode_fires_via_t0297(self) -> None:
        """EnterPlanMode tool_use also triggers await-state per the allowlist."""
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body", return_value=""), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value="EnterPlanMode"), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_text_signal_alone_still_fires(self) -> None:
        """Existing text-regex signal continues to work; tool_use returns None."""
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body",
                          return_value="Standing by; awaiting your ratification."), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_neither_signal_does_not_fire(self) -> None:
        """Text doesn't match + tool_use is None → no await-state → no fire."""
        with patch.object(am, "fetch_bridge_health", return_value={
                "last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"},
             }), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_message_body",
                          return_value="Implementation landed; tests green."), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)


class TestLatestAssistantInteractiveToolUse(unittest.TestCase):
    """T-0297 helper unit tests."""

    def _jsonl_result(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_askuserquestion_detected(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": []}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertEqual(name, "AskUserQuestion")

    def test_enterplanmode_detected(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "EnterPlanMode", "input": {}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertEqual(name, "EnterPlanMode")

    def test_non_blocking_tool_use_returns_none(self):
        """Bash/Read/etc. are not user-blocking — return None even though it's a tool_use."""
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_text_only_assistant_returns_none(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "text", "text": "All done."}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_subprocess_failure_returns_none(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([], returncode=1)
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_picks_most_recent_assistant(self):
        """tac order — first assistant entry in tac output is the most recent
        in file order. An older AskUserQuestion shouldn't fire if the most-recent
        assistant turn is text."""
        recent_text = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T23:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Implementing now."}]},
        })
        older_aq = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:00:00.000Z",
            "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}]},
        })
        # tac emits most-recent file line first.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([recent_text, older_aq])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)  # Most-recent assistant was text, not tool_use.


class TestDetectCyberRefusal(unittest.TestCase):
    """T-1363 increment 3 — cyber-safeguard refusal detector. Fixture mirrors the
    REAL 2026-06-22 Specialist refusal jsonl (stop_details.category == 'cyber')."""

    def _jsonl_result(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    # The real captured shape (explanation prose trimmed — we key on structure).
    CYBER_REFUSAL = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {
            "role": "assistant", "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber",
                             "explanation": "...blocked under Anthropic's Usage Policy..."},
            "content": [{"type": "text", "text": "..."}],
        },
    }

    def test_pure_classifier_true_on_cyber(self):
        self.assertTrue(am.is_cyber_refusal_message(self.CYBER_REFUSAL))

    def test_pure_classifier_false_on_other_refusal_category(self):
        msg = json.loads(json.dumps(self.CYBER_REFUSAL))
        msg["message"]["stop_details"]["category"] = "harmful_content"
        self.assertFalse(am.is_cyber_refusal_message(msg),
                         "only the cyber category routes to restart")

    def test_pure_classifier_false_on_non_refusal(self):
        ok = {"type": "assistant", "message": {"stop_reason": "end_turn",
              "content": [{"type": "text", "text": "done"}]}}
        self.assertFalse(am.is_cyber_refusal_message(ok))

    def test_pure_classifier_false_on_missing_stop_details(self):
        msg = {"type": "assistant", "message": {"stop_reason": "refusal"}}
        self.assertFalse(am.is_cyber_refusal_message(msg))

    def test_detect_true_when_latest_assistant_is_cyber_refusal(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([json.dumps(self.CYBER_REFUSAL)])
            self.assertTrue(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_false_when_recovered(self):
        """A newer non-refusal assistant turn = recovered → not blocked (tac order:
        most-recent line first)."""
        recovered = json.dumps({"type": "assistant", "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "back"}]}})
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([recovered, json.dumps(self.CYBER_REFUSAL)])
            self.assertFalse(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_skips_trailing_non_assistant_lines(self):
        """A user/tool_result line after the refusal must not hide it — the detector
        scans to the most-recent ASSISTANT line."""
        user_retry = json.dumps({"type": "user", "message": {"content": "retry"}})
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([user_retry, json.dumps(self.CYBER_REFUSAL)])
            self.assertTrue(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_fails_safe_on_subprocess_error(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([], returncode=1)
            self.assertFalse(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P1.d — rate-limit signal sources
# ---------------------------------------------------------------------------


class TestRateLimitRegex(unittest.TestCase):
    def test_matches_claude_code_synthetic_error(self) -> None:
        self.assertIsNotNone(RATE_LIMIT_REGEX.search(
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
        ))

    def test_matches_bare_rate_limited(self) -> None:
        self.assertIsNotNone(RATE_LIMIT_REGEX.search("rate limited"))
        self.assertIsNotNone(RATE_LIMIT_REGEX.search("Rate Limited"))

    def test_no_match_on_normal_output(self) -> None:
        self.assertIsNone(RATE_LIMIT_REGEX.search(
            "Implementation landed at d4e0f4d. Tests green. Ready when you say go."
        ))

    def test_no_match_on_429_text_without_rate_limit_phrase(self) -> None:
        # An HTTP 429 in tool output that doesn't contain the rate-limit phrase
        # shouldn't trip the regex (it's specifically about Claude Code's
        # synthetic-assistant-message error class).
        self.assertIsNone(RATE_LIMIT_REGEX.search("HTTP 429 too many requests on j17 echo-test"))


class TestDetectRateLimitInPane(unittest.TestCase):
    def test_bottom_match_fires(self) -> None:
        pane = (
            "earlier tool calls\n"
            "more output\n"
            "I'll handle this now.\n"
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited\n"
        )
        self.assertTrue(detect_rate_limit_in_pane(pane))

    def test_top_only_match_with_lots_of_activity_below_does_not_fire(self) -> None:
        # rate-limit phrase is in the FIRST line; tail-cap is 60 — if the pane
        # has > 60 lines of activity since then, the match is outside the tail.
        lines = ["API Error: Rate limited at start of session"]
        lines.extend(f"normal activity line {i}" for i in range(70))
        pane = "\n".join(lines)
        self.assertFalse(detect_rate_limit_in_pane(pane, tail_lines=60))

    def test_match_within_tail_window_fires(self) -> None:
        # rate-limit phrase WITHIN the bottom 60 lines.
        lines = [f"normal activity line {i}" for i in range(50)]
        lines.append("API Error: Server is temporarily limiting requests · Rate limited")
        lines.extend(f"prompt frame line {i}" for i in range(3))
        pane = "\n".join(lines)
        self.assertTrue(detect_rate_limit_in_pane(pane, tail_lines=60))


class TestCapturePane(unittest.TestCase):
    def test_returns_stdout_on_success(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="pane contents", stderr="")
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertEqual(out, "pane contents")

    def test_returns_none_on_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="no session")
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertIsNone(out)

    def test_returns_none_on_timeout(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=10)
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertIsNone(out)

    def test_sudo_user_threads_into_argv(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            capture_pane("claude-telegram-bot:0", "claude-research")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-nu", "claude-research"])


class TestLatestRateLimitAssistantTs(unittest.TestCase):
    def _jsonl_run(self, lines: list[str], returncode: int = 0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_finds_structured_rate_limit_entry(self) -> None:
        """Real Claude Code rate-limit entries have isApiErrorMessage + error=rate_limit."""
        entry = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "API Error: Rate limited"}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        # 2026-05-12T13:08:13.737Z → epoch.
        expected = int(datetime(2026, 5, 12, 13, 8, 13, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_text_only_fallback_matches(self) -> None:
        """If structured flags absent but text content matches, still detect."""
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "API Error: Server is temporarily limiting requests · Rate limited"}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNotNone(ts)

    def test_returns_none_when_no_rate_limit_entry(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "Normal response."}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNone(ts)

    def test_returns_none_on_subprocess_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([], returncode=1)
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNone(ts)

    def test_picks_most_recent_when_multiple(self) -> None:
        """tac order — first matching line in tac output is the most recent in file order."""
        recent = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T14:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Rate limited"}]},
        })
        older = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T13:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Rate limited"}]},
        })
        # tac would emit the most-recent file line first.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([recent, older])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        expected = int(datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)


# ---------------------------------------------------------------------------
# check_p1d composition
# ---------------------------------------------------------------------------


class TestCheckP1d(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/home/claude/.claude/projects/-home-claude"),
            "pantheon_config_path": Path("/home/claude/pantheon-config"),
        }

    def test_fires_when_pane_shows_rate_limit_and_stale(self) -> None:
        # Pane has the rate-limit string; jsonl ts is 10min ago; threshold 5min.
        stale_ts = int(time.time()) - 10 * 60
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=stale_ts):
            self.assertTrue(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_pane_clean(self) -> None:
        with patch.object(am, "capture_pane", return_value="Implementation landed at d4e0f4d. Tests green."):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_too_recent(self) -> None:
        recent_ts = int(time.time()) - 30  # 30s ago
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=recent_ts):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_pane_unavailable_fail_open(self) -> None:
        with patch.object(am, "capture_pane", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_jsonl_missing(self) -> None:
        # Pane shows rate-limit, but no jsonl found — could be a fresh agent;
        # without timestamp we can't gate on staleness, so don't fire.
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_jsonl_has_no_rate_limit_entry(self) -> None:
        # Pane shows it, but jsonl doesn't have the structured entry — possibly
        # stale pane state from before logs were rotated. Don't fire.
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


class TestCheckP1c(unittest.TestCase):
    """ORG-037 Layer 3 (T-1319) — active-team + dispatcher-turn-firing-stopped.

    The narrow "forgot-to-arm-Layer-1" residual. The whole point of the design
    is that it fires ONLY in the genuine stall quadrant and never in steady
    state — these tests pin both directions.
    """

    def _cfg(self, with_teams: bool = True) -> dict:
        cfg = {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": Path("/x"),
            "bridge_inbound_key": "orchestrator",
        }
        if with_teams:
            cfg["teams_dir"] = Path("/x/teams")
        return cfg

    def test_fires_on_active_team_stale_jsonl_fresh_teammate(self) -> None:
        stale = int(time.time()) - 3600  # 1h ago (> 30min threshold)
        fresh_team = int(time.time()) - 600  # 10min ago — newer than main jsonl
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=fresh_team):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertTrue(fired)

    def test_no_team_suppresses(self) -> None:
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes", return_value=False), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_fresh_jsonl_suppresses(self) -> None:
        """A working Layer-1 keeps the main jsonl fresh → never fires. This is
        the narrowing that prevents recreating the broad P1.c surface."""
        fresh = int(time.time()) - 60  # 1min ago — within threshold
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=fresh), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=0), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_recent_ledger_suppresses(self) -> None:
        """Ledger activity (even without a fresh jsonl) counts as activity."""
        stale_jsonl = int(time.time()) - 3600
        recent_ledger = int(time.time()) - 60
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale_jsonl), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=recent_ledger), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_stale_teammate_suppresses_when_sharpening_on(self) -> None:
        """§7 sharpening: a teams dir whose newest artifact is OLDER than the
        dispatcher's last turn = a long-dead stale dir, not a dropped sub-result
        → the legit-idle-with-stale-dir false fire is eliminated."""
        stale = int(time.time()) - 3600
        older_team = stale - 600  # teammate artifact OLDER than the main jsonl
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=older_team):
            fired = am.check_p1c(
                self._cfg(), stale_threshold_seconds=1800, require_fresh_teammate=True
            )
        self.assertFalse(fired)

    def test_bare_predicate_fires_without_fresh_teammate(self) -> None:
        """Sharpening OFF (the documented brittle-fallback) → fires on
        active-team + stale alone; the teammate probe is not even consulted."""
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime") as nt:
            fired = am.check_p1c(
                self._cfg(), stale_threshold_seconds=1800, require_fresh_teammate=False
            )
        self.assertTrue(fired)
        nt.assert_not_called()

    def test_no_teams_dir_never_fires(self) -> None:
        """An agent without teams_dir configured is ineligible — bails before
        any probe (no spurious subprocess)."""
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes") as ati, \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale):
            fired = am.check_p1c(self._cfg(with_teams=False), stale_threshold_seconds=1800)
        self.assertFalse(fired)
        ati.assert_not_called()

    def test_no_activity_signal_fail_open(self) -> None:
        """Both jsonl + ledger unavailable → can't determine activity → fail-open."""
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=None), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=None), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)


class TestTeamHelpers(unittest.TestCase):
    """The active_team_inboxes + newest_team_artifact_mtime probes (T-1319)."""

    def _run(self, stdout: str = "", rc: int = 0):
        return MagicMock(returncode=rc, stdout=stdout, stderr="")

    def test_active_team_inboxes_true(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("/home/claude/.claude/teams/session-x/inboxes\n")
            self.assertTrue(am.active_team_inboxes(Path("/t"), "claude"))

    def test_active_team_inboxes_false_on_empty(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("")
            self.assertFalse(am.active_team_inboxes(Path("/t"), "claude"))

    def test_active_team_inboxes_fail_closed_on_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run", side_effect=OSError):
            self.assertFalse(am.active_team_inboxes(Path("/t"), "claude"))

    def test_newest_team_artifact_mtime_parses_float(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("1782600000.1234\n")
            self.assertEqual(am.newest_team_artifact_mtime(Path("/t"), "claude"), 1782600000)

    def test_newest_team_artifact_mtime_none_on_empty(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("")
            self.assertIsNone(am.newest_team_artifact_mtime(Path("/t"), "claude"))

    def test_newest_team_artifact_mtime_none_on_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertIsNone(am.newest_team_artifact_mtime(Path("/t"), "claude"))


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        for k in (
            "PANTHEON_STATE_DIR",
            "ACTIVITY_MONITOR_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1D_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1A_IDLE_SECONDS",
            "ACTIVITY_MONITOR_P1B_OPERATOR_SECONDS",
            "ACTIVITY_MONITOR_P1D_AGE_SECONDS",
            "ACTIVITY_MONITOR_BRIDGE_HEALTH_URL",
            "PANTHEON_LAYER3_ENABLED",
            "ACTIVITY_MONITOR_P1C_STALE_SECONDS",
            "ACTIVITY_MONITOR_P1C_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1C_REQUIRE_FRESH_TEAMMATE",
            "ACTIVITY_MONITOR_BOUNDED_POKE_ENABLED",
            "ACTIVITY_MONITOR_MAX_INEFFECTIVE_POKES",
        ):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_cooldown_short_circuits(self) -> None:
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"ts": int(time.time()) - 60, "predicate": "p1a"}))
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b") as p1b, \
             patch.object(am, "check_p1d") as p1d:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        # Cooldown should have short-circuited before predicates were even checked.
        p1a.assert_not_called()
        p1b.assert_not_called()
        p1d.assert_not_called()

    def test_p1a_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertTrue(state.exists())
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1a")

    def test_p1b_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=True), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1b")

    def test_p1d_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        # Retry-inject prompt is the one sent to the pane.
        called_prompt = poke.call_args[0][1]
        self.assertIn("rate-limit", called_prompt.lower())
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1d")

    def test_p1a_takes_precedence_over_p1b(self) -> None:
        """When both fire, P1.a wins (stronger signal: completely-idle)."""
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1b", return_value=True) as p1b, \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        # Neither P1.b nor P1.d should be checked (short-circuit on P1.a true).
        p1b.assert_not_called()
        p1d.assert_not_called()

    def test_p1b_takes_precedence_over_p1d(self) -> None:
        """When P1.a is false but P1.b + P1.d both fire, P1.b wins."""
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=True), \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        p1d.assert_not_called()

    def test_p1d_cooldown_shorter_than_p1a(self) -> None:
        """P1.d cooldown (15min) is per-predicate — recent P1.d fire blocks
        re-fire within 15min; a 16min-old P1.d fire DOESN'T (whereas equivalent
        old P1.a fire would still block at 16min)."""
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        # 16 min ago — past P1.d's 15min cooldown but inside P1.a's 30min
        state.write_text(json.dumps({"ts": int(time.time()) - 16 * 60, "predicate": "p1d"}))
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        # Cooldown lookup keyed on last-predicate=p1d → 15min → expired → fire allowed.
        p1d.assert_called()
        poke.assert_called_once()

    def test_p1a_cooldown_still_blocks_at_20min(self) -> None:
        """P1.a's 30min cooldown still blocks at 20min."""
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"ts": int(time.time()) - 20 * 60, "predicate": "p1a"}))
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b") as p1b, \
             patch.object(am, "check_p1d") as p1d:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1a.assert_not_called()
        p1b.assert_not_called()
        p1d.assert_not_called()

    def test_no_predicate_bails_clean(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "send_poke") as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists())

    def test_p1c_disabled_by_default_not_evaluated(self) -> None:
        """Ship dark: with PANTHEON_LAYER3_ENABLED unset, check_p1c is never
        even called (the `p1c_enabled and ...` short-circuit) — committed code
        is inert everywhere until a per-agent flag flip."""
        self.assertNotIn("PANTHEON_LAYER3_ENABLED", os.environ)
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "check_p1c") as p1c, \
             patch.object(am, "send_poke") as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1c.assert_not_called()
        poke.assert_not_called()

    def test_p1c_enabled_fires_pokes_and_records(self) -> None:
        os.environ["PANTHEON_LAYER3_ENABLED"] = "1"
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "check_p1c", return_value=True), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        # The liveness-backstop prompt is the one sent to the pane.
        called_prompt = poke.call_args[0][1]
        self.assertIn("liveness backstop", called_prompt.lower())
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1c")

    def test_p1c_only_reached_after_abd_false(self) -> None:
        """Precedence: P1.c is the last gate — an earlier predicate firing
        short-circuits before check_p1c is consulted, even when enabled."""
        os.environ["PANTHEON_LAYER3_ENABLED"] = "1"
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1c") as p1c, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        p1c.assert_not_called()

    def test_poke_failure_returns_1(self) -> None:
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "send_poke", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 1)
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists())

    def test_env_override_threshold(self) -> None:
        """ACTIVITY_MONITOR_P1A_IDLE_SECONDS env override is threaded through."""
        os.environ["ACTIVITY_MONITOR_P1A_IDLE_SECONDS"] = "60"
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            p1a.return_value = False
            main(["--agent", "orchestrator"])
        kwargs = p1a.call_args[1] if p1a.call_args[1] else {}
        positional = p1a.call_args[0]
        actual = positional[1] if len(positional) > 1 else kwargs.get("idle_threshold_seconds")
        self.assertEqual(actual, 60)

    def test_p1d_env_override_age(self) -> None:
        """ACTIVITY_MONITOR_P1D_AGE_SECONDS env override is threaded through."""
        os.environ["ACTIVITY_MONITOR_P1D_AGE_SECONDS"] = "120"
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d") as p1d:
            p1d.return_value = False
            main(["--agent", "orchestrator"])
        positional = p1d.call_args[0]
        actual = positional[1] if len(positional) > 1 else p1d.call_args[1].get("age_threshold_seconds")
        self.assertEqual(actual, 120)


# ---------------------------------------------------------------------------
# T-0538: send_poke() now delegates to send_keys_submit.sh via sudo-wrapped bash -c
# ---------------------------------------------------------------------------


class TestSendPoke(unittest.TestCase):
    """T-0538: send_poke() must invoke the target user's send_keys_submit.sh
    via sudo -nu + XDG_RUNTIME_DIR in the bash -c string."""

    def _cfg(self) -> dict:
        return {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/home/claude/.claude/projects/-home-claude"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator", "orchestrator"}),
            "pantheon_config_path": Path("/home/claude/pantheon-config"),
            "bridge_inbound_key": "orchestrator",
        }

    def _make_run(self, returncode=0):
        return MagicMock(returncode=returncode, stdout="", stderr="")

    def test_send_poke_uses_sudo_nu(self):
        # T-0538: command must start with sudo -nu <sudo_user>
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["sudo", "-nu", "claude"])

    def test_send_poke_invokes_helper_path(self):
        # T-0538: bash -c string must reference the target's helper path
        expected_helper = str(Path("/home/claude/pantheon-config") / "tools" / "send_keys_submit.sh")
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        # cmd is: ["sudo", "-nu", user, "bash", "-c", "<shell string>"]
        bash_c_str = cmd[-1]
        self.assertIn(expected_helper, bash_c_str)

    def test_send_poke_includes_xdg_runtime_dir(self):
        # T-0538: XDG_RUNTIME_DIR must be set in the bash -c string (cross-account tmux)
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        bash_c_str = cmd[-1]
        self.assertIn("XDG_RUNTIME_DIR", bash_c_str)

    def test_send_poke_includes_pane_and_prompt(self):
        # T-0538: pane target and prompt text both in bash -c string
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "my poke text")
        cmd = mock_run.call_args[0][0]
        bash_c_str = cmd[-1]
        self.assertIn("claude-telegram-bot:0", bash_c_str)
        self.assertIn("my poke text", bash_c_str)

    def test_send_poke_success_returns_true(self):
        # T-0538: exit 0 → True
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertTrue(result)

    def test_send_poke_failure_returns_false(self):
        # T-0538: exit ≠ 0 → False
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(1)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_timeout_returns_false(self):
        # T-0538: TimeoutExpired → False
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=10)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_os_error_returns_false(self):
        # T-0538: OSError → False
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=OSError("no such file")):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_single_subprocess_call(self):
        # T-0538: single call (helper does text+enter+verify internally), not two
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "prompt")
        self.assertEqual(mock_run.call_count, 1)


class TestCapStateClassifier(unittest.TestCase):
    """T-1369: the two measured cap strings classify OPPOSITELY, and discussing
    a cap string must never read as being capped."""

    def _turn(self, text, err=True):
        return {"type": "assistant", "isApiErrorMessage": err,
                "message": {"content": [{"type": "text", "text": text}]}}

    def test_session_limit_verbatim_specimen(self):
        # orchestrator, 38x, 2026-08-02. U+00B7 separator — a paraphrase regex misses it.
        kind, hint = am.classify_cap_message(
            self._turn("You've hit your session limit \u00b7 resets 9:40am (UTC)"))
        self.assertEqual(kind, "session_limit")
        self.assertEqual(hint, "9:40am (UTC)")

    def test_spend_limit_verbatim_specimen(self):
        # atlas, 59x, the 48h outage (T-2387).
        kind, hint = am.classify_cap_message(self._turn(
            "You've hit your monthly spend limit \u00b7 raise it at "
            "claude.ai/settings/usage?from=cc_cli_limit_message"))
        self.assertEqual(kind, "spend_limit")
        self.assertIsNone(hint)

    def test_discussing_a_cap_string_is_not_being_capped(self):
        # Load-bearing: this fleet's tickets QUOTE both wire strings verbatim.
        # Without the isApiErrorMessage guard, recovery reads as permanently capped.
        self.assertIsNone(am.classify_cap_message(self._turn(
            "the specimen is: You've hit your session limit \u00b7 resets 5pm (UTC)",
            err=False)))

    def test_non_assistant_and_malformed_are_none(self):
        self.assertIsNone(am.classify_cap_message({"type": "user"}))
        self.assertIsNone(am.classify_cap_message(
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": None}}))

    def test_ordinary_api_error_is_not_a_cap(self):
        # -32603 entitlement, 500s, etc. — different class, different routing (T-2386).
        self.assertIsNone(am.classify_cap_message(self._turn(
            "Internal error: Your organization does not have access to Claude.")))


class TestCapPrePokeGate(unittest.TestCase):
    """T-1369 dispatch branches. MEASURED motivation: the waker kicked a
    session-capped orchestrator 8x over 3.5h — every kick delivered, every turn an
    error. Bounded, correct, useless."""

    def _cfg(self):
        return {"jsonl_dir": Path("/nonexistent"), "sudo_user": "claude",
                "pane_target": "x:0"}

    def _run(self, cap, sp):
        with patch.object(am, "send_poke", return_value=True) as poke, \
             patch.object(am, "_session_cap_state", return_value=cap), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False) as cyber, \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "latest_jsonl_mtime", return_value=5000):
            rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                   bounded_enabled=True, max_ineffective=1)
        return rc, poke, alert, cyber

    def test_session_limit_suppresses_poke_AND_escalation(self):
        # Self-clears at the printed time — paging a human for a state with an
        # expiry is the alarm that teaches people to skip alarms.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, _ = self._run(("session_limit", "9:40am (UTC)"),
                                           Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        alert.assert_not_called()

    def test_spend_limit_suppresses_poke_and_escalates(self):
        # Holds until a human raises it — alert + hold, exactly once.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, _ = self._run(("spend_limit", None),
                                           Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        alert.assert_called_once()
        self.assertIn("spend-cap", str(alert.call_args))

    def test_no_cap_falls_through_to_cyber_gate(self):
        # The gate must be a pass-through when healthy — the cyber check still runs.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, cyber = self._run(None, Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        cyber.assert_called_once()
        poke.assert_called_once()  # healthy path still pokes

    def test_ship_dark_flag_off_bypasses_cap_gate(self):
        # bounded_enabled=False must reproduce legacy behaviour exactly — the
        # cap gate lives inside the bounded block.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cap_state") as capfn:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P",
                                       Path(tmp) / "s.json",
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            capfn.assert_not_called()
            poke.assert_called_once()


class CapModalDetectorT1369(unittest.TestCase):
    """T-1369 branch 4: the pane-only /rate-limit-options modal.

    Fixture is the REAL pane captured off orchestrator 2026-08-02 while it sat wedged
    ~50 min past its own printed reset — not a hand-typed approximation. That
    distinction already paid: the real capture carries a second '❯' on the shell
    prompt line ('❯ /rate-limit-options') above the options, which a first-match
    cursor scan misreads as "cursor not on option 1".
    """

    SPECIMEN = REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt"

    def _specimen(self):
        return self.SPECIMEN.read_text(encoding="utf-8")

    def test_the_specimen_still_carries_what_the_test_relies_on(self):
        """A fixture that quietly loses its distinguishing feature turns every
        assertion below into a tautology."""
        raw = self._specimen()
        self.assertIn("\x1b", raw, "specimen should still be the -e capture")
        self.assertEqual(raw.count("❯"), 2,
                         "specimen must keep BOTH the prompt glyph and the "
                         "menu cursor — that pair is the bug it encodes")

    def test_real_specimen_is_safe_to_dismiss(self):
        self.assertEqual(am.classify_cap_modal(self._specimen()), am.MODAL_SAFE)

    def test_ansi_stripped_form_classifies_identically(self):
        """Production capture_pane uses `-p` without `-e`, so the live text has
        no escapes. Both forms must take the same path."""
        stripped = am._strip_ansi(self._specimen())
        self.assertNotIn("\x1b", stripped)
        self.assertEqual(am.classify_cap_modal(stripped), am.MODAL_SAFE)

    def test_cursor_on_option_2_is_UNSAFE(self):
        """The money branch. Enter confirms whatever is highlighted, and option
        2 buys a plan — the operator's lane."""
        text = am._strip_ansi(self._specimen())
        moved = (text.replace("❯ 1. Stop", "  1. Stop")
                     .replace("  2. Upgrade", "❯ 2. Upgrade"))
        self.assertEqual(am.classify_cap_modal(moved), am.MODAL_UNSAFE)

    def test_no_cursor_on_any_option_is_UNSAFE(self):
        text = am._strip_ansi(self._specimen()).replace("❯ 1. Stop", "  1. Stop")
        self.assertEqual(am.classify_cap_modal(text), am.MODAL_UNSAFE)

    def test_a_different_menu_is_NOT_this_modal(self):
        """'What do you want to do?' alone is a generic TUI prompt; matching it
        would make every future menu look like the cap modal."""
        self.assertIsNone(am.classify_cap_modal(
            "What do you want to do?\n❯ 1. Retry\n  2. Abort\n"))

    def test_ordinary_pane_and_empty_input_are_None(self):
        self.assertIsNone(am.classify_cap_modal("╭─────╮\n│ > │\n╰─────╯"))
        self.assertIsNone(am.classify_cap_modal(""))
        self.assertIsNone(am.classify_cap_modal(None))

    def test_no_configured_pane_degrades_instead_of_shelling_out(self):
        """This seam sits on the path EVERY poke takes. A cfg without a pane
        (specialist-a2a has none by construction) must return None rather than raise
        or run a live capture against a sibling's session."""
        with patch("tools.activity_monitor.capture_pane") as cap:
            self.assertIsNone(am._session_cap_modal({"sudo_user": "claude"}))
            self.assertIsNone(am._session_cap_modal({"tmux_target_pane": ""}))
            cap.assert_not_called()

    def test_the_cap_ERROR_text_alone_is_not_the_modal(self):
        """The jsonl branch owns that state. A session-limit line on the pane
        without the menu must not trigger a keystroke."""
        self.assertIsNone(am.classify_cap_modal(
            "You've hit your session limit · resets 2:50pm (UTC)"))


class CapModalDispatchT1369(unittest.TestCase):
    """The dispatch branch: dismiss when safe, never touch the keyboard when not,
    and bound the repeats."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = self.d / "poke.json"
        self.cfg = {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": str(self.d),
            "pantheon_config_path": str(REPO_ROOT),
        }

    def _dispatch(self, modal, **patches):
        stack = [
            patch("tools.activity_monitor._session_cap_modal", return_value=modal),
            patch("tools.activity_monitor.latest_jsonl_mtime", return_value=1),
            patch("tools.activity_monitor._session_cap_state", return_value=None),
            patch("tools.activity_monitor._session_cyber_blocked", return_value=False),
        ]
        with contextlib.ExitStack() as es:
            for p in stack:
                es.enter_context(p)
            sender = es.enter_context(
                patch("tools.activity_monitor.send_modal_confirm",
                      return_value=patches.get("send_ok", True)))
            poker = es.enter_context(patch("tools.activity_monitor.send_poke"))
            alerter = es.enter_context(
                patch("tools.activity_monitor._route_failure_alert"))
            rc = am._dispatch_poke(
                self.cfg, "orchestrator", "idle", "hello", self.state,
                bounded_enabled=True, max_ineffective=3)
        return rc, sender, poker, alerter

    def test_safe_modal_sends_a_bare_enter_and_never_a_poke(self):
        rc, sender, poker, _ = self._dispatch(am.MODAL_SAFE)
        self.assertEqual(rc, 0)
        sender.assert_called_once()
        poker.assert_not_called()

    def test_unsafe_modal_sends_NOTHING_and_escalates(self):
        """The load-bearing one: option 2 spends money."""
        rc, sender, poker, alerter = self._dispatch(am.MODAL_UNSAFE)
        self.assertEqual(rc, 0)
        sender.assert_not_called()
        poker.assert_not_called()
        alerter.assert_called_once()
        self.assertIn("spends", alerter.call_args[0][1])

    def test_dismissals_are_BOUNDED_then_escalate(self):
        for i in range(am.MAX_MODAL_DISMISSALS):
            _, sender, _, _ = self._dispatch(am.MODAL_SAFE)
            sender.assert_called_once()
            self.assertEqual(
                json.loads(self.state.read_text())["modal_dismissals"], i + 1)
        _, sender, _, alerter = self._dispatch(am.MODAL_SAFE)
        sender.assert_not_called()
        alerter.assert_called_once()

    def test_no_modal_falls_through_to_the_existing_gates(self):
        """The new branch must not swallow the jsonl cap / cyber / poke paths."""
        rc, sender, poker, _ = self._dispatch(None)
        sender.assert_not_called()
        poker.assert_called_once()

    def test_a_failed_send_reports_rc_1(self):
        rc, sender, _, _ = self._dispatch(am.MODAL_SAFE, send_ok=False)
        self.assertEqual(rc, 1)
        sender.assert_called_once()


class ProductiveLivenessT2387(unittest.TestCase):
    """T-2387: a seat erroring every turn must stop reading as ALIVE.

    atlas hit the monthly spend cap and produced 23 turns / 23 errors on 07-30
    and 22 / 22 on 07-31 — 59 identical failures over two days — while every
    check asking "did it produce a turn" answered YES throughout. jsonl mtime
    advances on an error exactly as it does on real work.

    The fix keys on the OUTCOME BOOLEAN `isApiErrorMessage`, not on a status
    code or an error string, so it covers the spend cap (429), api-health (400),
    model-outage (404) and classes nobody has met. The tree already carries four
    detectors keyed to specific error identities — one of them a literal
    substring match on "monthly spend limit" — and each new class arrived as a
    new narrow detector. That is the pattern this breaks.
    """

    ERR = {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429,
           "timestamp": "2026-07-31T12:00:00.000Z"}
    OK = {"type": "assistant", "timestamp": "2026-07-29T09:00:00.000Z"}
    USER = {"type": "user", "timestamp": "2026-07-31T12:00:01.000Z"}

    def _run(self, records):
        """Drive latest_productive_jsonl_mtime over canned records, newest-first."""
        with patch("tools.context_fill_monitor.find_latest_jsonl",
                   return_value=Path("/fake/s.jsonl")), \
             patch("tools.context_fill_monitor.read_recent_records",
                   return_value=records):
            return am.latest_productive_jsonl_mtime(Path("/fake"), "codex", "atlas")

    def test_THE_REGRESSION_all_error_turns_report_no_productive_activity(self):
        """59 error turns and nothing else → None, not a fresh timestamp."""
        self.assertIsNone(self._run([dict(self.ERR) for _ in range(59)]))

    def test_error_turns_do_not_mask_an_older_real_turn(self):
        """Newest-first: errors on top, a real turn beneath → the REAL one wins."""
        got = self._run([dict(self.ERR), dict(self.ERR), dict(self.OK)])
        self.assertEqual(got, int(datetime(2026, 7, 29, 9, 0,
                                           tzinfo=timezone.utc).timestamp()))

    def test_non_assistant_records_are_skipped_not_counted(self):
        """A user record is not the seat doing work."""
        self.assertIsNone(self._run([dict(self.USER), dict(self.ERR)]))

    def test_CONTROL_a_healthy_seat_still_reads_active(self):
        """Without this, the guards above could pass by always returning None —
        which would mark every seat idle and poke the whole fleet."""
        got = self._run([dict(self.OK)])
        self.assertIsNotNone(got)
        self.assertEqual(got, int(datetime(2026, 7, 29, 9, 0,
                                           tzinfo=timezone.utc).timestamp()))

    def test_read_failure_returns_None_fail_open(self):
        with patch("tools.context_fill_monitor.find_latest_jsonl", return_value=None):
            self.assertIsNone(
                am.latest_productive_jsonl_mtime(Path("/fake"), "codex", "atlas"))


class ProductiveLivenessShipDarkT2387(unittest.TestCase):
    """The switch is default-OFF and only the LIVENESS sites are switched.

    Poke-effectiveness asks a DIFFERENT question — "did the session take a turn
    at all" — for which an error turn legitimately COUNTS (the poke landed).
    Those callers must keep the turn-counting signal. Conflating the two is the
    defect; swapping both would be the same defect with the sign flipped.
    """

    def test_THE_DEFAULT_ITSELF_is_OFF_when_the_env_var_is_absent(self):
        """The other switch tests PATCH the flag, so none of them exercise the
        DEFAULT — flipping the module default to "1" passed all of them. Caught
        by mutating the source during the non-vacuity pass, which is the whole
        argument for running one. Re-derive the constant under a clean env.
        """
        import importlib
        with patch.dict(os.environ, {}, clear=True):
            reloaded = importlib.reload(am)
            try:
                self.assertFalse(
                    reloaded.PRODUCTIVE_LIVENESS_ENABLED,
                    "T-2387 must ship DARK — arming changes which seats look idle "
                    "and therefore which get poked (T-1126 supervised first-fire)")
            finally:
                importlib.reload(am)  # restore for the rest of the suite

    def test_arming_requires_exactly_1_not_any_truthy_string(self):
        import importlib
        for val, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with patch.dict(os.environ,
                            {"PANTHEON_PRODUCTIVE_LIVENESS_ENABLED": val}, clear=True):
                reloaded = importlib.reload(am)
                try:
                    self.assertEqual(reloaded.PRODUCTIVE_LIVENESS_ENABLED, expected,
                                     f"env value {val!r}")
                finally:
                    importlib.reload(am)

    def test_default_is_OFF_and_delegates_to_the_unchanged_signal(self):
        with patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", False), \
             patch.object(am, "latest_jsonl_mtime", return_value=1234) as legacy, \
             patch.object(am, "latest_productive_jsonl_mtime") as productive:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 1234)
        legacy.assert_called_once()
        productive.assert_not_called()

    def test_armed_uses_the_outcome_aware_signal(self):
        with patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", True), \
             patch.object(am, "latest_jsonl_mtime") as legacy, \
             patch.object(am, "latest_productive_jsonl_mtime", return_value=99) as prod:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 99)
        prod.assert_called_once()
        legacy.assert_not_called()

    def test_poke_effectiveness_sites_still_use_the_turn_counting_signal(self):
        """Guards the split itself, by reading the source: every remaining
        bare `latest_jsonl_mtime(` CALL must be a poke-effectiveness site."""
        import re
        src = Path(am.__file__).read_text(encoding="utf-8")
        body = src.split("def liveness_jsonl_mtime", 1)[1].split("\n\n\n", 1)[1]
        calls = re.findall(r"^\s*(?:cur_mtime|pre_poke_jsonl_mtime)\s*=\s*latest_jsonl_mtime\(",
                           body, re.M)
        self.assertGreaterEqual(
            len(calls), 6,
            "expected the poke-effectiveness sites to keep latest_jsonl_mtime; "
            "if they were switched to the productive signal, an error turn would "
            "stop counting as 'the poke landed' and pokes would look ineffective")
        # ...and the two LIVENESS sites must have been switched. Both halves of
        # the split are asserted; checking only one would let a wholesale swap
        # in either direction pass.
        self.assertEqual(
            body.count("liveness_jsonl_mtime("), 3,
            "expected exactly the three liveness sites (check_p1a, Layer-3 "
            "dormancy, check_tui_wedge) to use the switched signal — a wedge "
            "measures a PRODUCTIVE-turn freeze, so an error turn must not reset "
            "it (T-1369 adds the third; same T-2387 reason as the first two)")


class TuiWedgeDetectorT1369(unittest.TestCase):
    """T-1369 final branch — the TUI-alive-but-not-emitting wedge detector.

    Threshold is MEASURED (tui_wedge_sampler, orchestrator n=110 max 779s); the tests
    below drive the composition, not the number. The number is env-overridable
    and exercised at a small value so the duration gate is testable."""

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "agent": "orchestrator"}

    # ---- _pane_fp ----
    def test_pane_fp_none_is_none(self) -> None:
        self.assertIsNone(am._pane_fp(None))

    def test_pane_fp_equal_for_same_text_differs_for_different(self) -> None:
        self.assertEqual(am._pane_fp("abc"), am._pane_fp("abc"))
        self.assertNotEqual(am._pane_fp("abc"), am._pane_fp("abd"))

    # ---- pane_is_animating ----
    def test_animating_true_when_pane_changes(self) -> None:
        with patch.object(am, "capture_pane", side_effect=["screen-A", "screen-B"]), \
             patch.object(am.time, "sleep"):
            self.assertTrue(am.pane_is_animating(self._cfg(), 0))

    def test_animating_false_when_pane_still(self) -> None:
        with patch.object(am, "capture_pane", side_effect=["idle> ", "idle> "]), \
             patch.object(am.time, "sleep"):
            self.assertFalse(am.pane_is_animating(self._cfg(), 0))

    def test_animating_none_when_capture_fails(self) -> None:
        # A failed capture is NOT evidence of motion — must return None, never True.
        with patch.object(am, "capture_pane", side_effect=["screen-A", None]), \
             patch.object(am.time, "sleep"):
            self.assertIsNone(am.pane_is_animating(self._cfg(), 0))

    def test_animating_none_when_no_pane_configured(self) -> None:
        self.assertIsNone(am.pane_is_animating({"sudo_user": "claude"}, 0))

    # ---- check_tui_wedge composition ----
    def _frozen(self, seconds_ago: int):
        """Patch liveness so the seat looks frozen `seconds_ago` seconds."""
        return patch.object(am, "liveness_jsonl_mtime",
                            return_value=int(time.time()) - seconds_ago)

    def test_fires_on_long_freeze_moving_pane_no_known_cause(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertTrue(fired)

    def test_duration_gate_not_fired_when_recent(self) -> None:
        with self._frozen(60), \
             patch.object(am, "pane_is_animating", return_value=True) as anim:
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        anim.assert_not_called()  # cheap gate short-circuits before the probe

    def test_fail_open_when_no_mtime(self) -> None:
        with patch.object(am, "liveness_jsonl_mtime", return_value=0):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_defers_to_cap_modal(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=am.MODAL_SAFE), \
             patch.object(am, "pane_is_animating", return_value=True) as anim:
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        anim.assert_not_called()  # a capped seat is not even probed for motion

    def test_defers_to_cap_state(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=("spend_limit", None)), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_defers_to_cyber_block(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=True), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_still_pane_not_a_wedge(self) -> None:
        # Long freeze, no known cause, but pane STILL → idle-at-prompt, p1a's job.
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "pane_is_animating", return_value=False):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_capture_failure_does_not_fire(self) -> None:
        # animating is None (capture failed) → must NOT fire (fail-open).
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "pane_is_animating", return_value=None):
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        # Distinguish the fail-open (capture failed) reason from the "pane STILL"
        # reason — gives the dedicated None branch teeth (else it is an
        # equivalent-outcome mutant since `not None` also returns not-fired).
        self.assertIn("capture", reason)


class TuiWedgeMainT1369(unittest.TestCase):
    """T-1369 — main() wiring for the wedge branch: ship-dark by default, and
    when enabled it escalates a restart RECOMMENDATION (never auto-restarts) and
    pre-empts the poke predicates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        for k in ("PANTHEON_STATE_DIR", "ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_ship_dark_flag_off_never_calls_wedge(self) -> None:
        os.environ.pop("ACTIVITY_MONITOR_TUI_WEDGE_ENABLED", None)
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge") as wedge, \
             patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        wedge.assert_not_called()

    def test_enabled_wedge_escalates_and_preempts_p1a(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "check_p1a") as p1a:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        alert.assert_called_once()          # paged Recovery with the restart recommendation
        p1a.assert_not_called()             # wedge pre-empts the poke predicates
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertTrue(json.loads(state.read_text())["escalated"])

    def test_wedge_page_suppressed_when_quiet_attributable(self) -> None:
        # T-2632 PART 2 wiring: a fired wedge whose quiet is ATTRIBUTABLE to a
        # declared signal (e.g. an active context_fill_hold) must NOT page Recovery —
        # the freeze is explained (a held session sitting in a long turn), not a
        # wedge. Downgrades the ALERT only; this branch never restarts. This is the
        # suppress-direction mutation guard (without the gate, alert WOULD fire).
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "quiet_is_attributable",
                          return_value=(True, "context_fill_hold active")), \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "check_p1a") as p1a:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        alert.assert_not_called()   # attributable quiet → page SUPPRESSED
        p1a.assert_not_called()     # wedge branch still preempts the poke predicates
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists(), "no escalated state written on a suppressed page")

    def test_enabled_wedge_pages_once_across_cycles(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "_route_failure_alert") as alert:
            for _ in range(3):
                main(["--agent", "orchestrator"])
        self.assertEqual(alert.call_count, 1, "one page per episode, then bounded-quiet")

    def test_enabled_not_wedged_falls_through_to_predicates(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(False, "fresh")), \
             patch.object(am, "check_p1a", return_value=False) as p1a, \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1a.assert_called_once()  # not wedged → normal predicate flow runs


class QuietAttributionTests(unittest.TestCase):
    """T-2632 PART 2 (asserted-only core): quiet_is_attributable — a fail-safe
    query that reports whether a seat's quiet is explained by a DECLARED signal.
    The load-bearing property is the fail-safe direction: any error/ambiguity must
    return (False, ...) so the wedge signal proceeds — never masked."""

    def test_attributable_when_hold_active(self) -> None:
        with patch("tools.context_fill_hold.hold_status",
                   return_value={"active": True, "reason": "operator soak"}):
            ok, reason = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertTrue(ok)
        self.assertIn("operator soak", reason)

    def test_unattributed_when_no_hold_and_no_signals(self) -> None:
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertFalse(ok)
        self.assertIn("unattributed", reason)

    def test_injected_signal_attributes(self) -> None:
        # The wiring pass's unit-disabled probe plugs in here.
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable(
                "specialist", now=1000,
                signals=[lambda a, n: (True, "session unit disabled (TG-6694 pause)")],
            )
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_injected_signal_none_falls_through_to_unattributed(self) -> None:
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable(
                "specialist", now=1000, signals=[lambda a, n: None]
            )
        self.assertFalse(ok)

    def test_failsafe_a_raising_signal_never_attributes(self) -> None:
        # A broken signal must be swallowed and treated as 'no signal' — erring
        # toward NOT attributable (wedge proceeds), never toward masking.
        def boom(a, n):
            raise RuntimeError("probe blew up")
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, _ = am.quiet_is_attributable("orchestrator", now=1000, signals=[boom])
        self.assertFalse(ok)

    def test_failsafe_hold_lookup_error_never_masks(self) -> None:
        # If the hold lookup itself throws, quiet must read as UNATTRIBUTED so the
        # wedge still escalates — the T-2615 'never mask a wedge' invariant.
        with patch("tools.context_fill_hold.hold_status", side_effect=OSError("state dir gone")):
            ok, _ = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertFalse(ok)

    def test_first_truthy_signal_wins_hold_precedes_injected(self) -> None:
        # Asserted hold is checked before injected signals; its reason is returned.
        called = []
        with patch("tools.context_fill_hold.hold_status",
                   return_value={"active": True, "reason": "held"}):
            ok, reason = am.quiet_is_attributable(
                "orchestrator", now=1000,
                signals=[lambda a, n: called.append(1) or (True, "should not reach")],
            )
        self.assertTrue(ok)
        self.assertIn("held", reason)
        self.assertEqual(called, [], "hold short-circuits before injected signals")


if __name__ == "__main__":
    unittest.main()

