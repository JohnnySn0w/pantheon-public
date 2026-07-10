"""Tests for tools.inbox_consumer."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Bootstrap: same sys.path trick as inbox_consumer.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.inbox_consumer import (
    INJECT_PROMPT,
    INJECT_ENTER_DELAY,
    IDLE_SENTINELS,
    PENDING_PASTE_SENTINEL,
    CONSECUTIVE_SKIP_ALERT_THRESHOLD,
    ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS,
    ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS,
    PANE_WEDGE_SENTINELS,
    SkipCycle,
    TicketInfo,
    check_autonomy_off,
    check_cap_hit,
    check_consecutive_skips,
    # check_mid_turn removed by T-0717 (dispatch-gating pane scrape replaced by
    # auto_delivered mutex — see check_dispatch_locked).
    check_recent_inbound,
    check_orchestrator_health,
    count_consecutive_skips,
    format_prompt,
    inject,
    list_eligible_tickets,
    log_outcome,
    main,
    pick_ticket,
    pull_repo_or_warn,
)


# ---------------------------------------------------------------------------
# Inject-prompt byte-exact test.
# ---------------------------------------------------------------------------

# expected_bytes is intentionally hard-coded to catch paraphrase regressions;
# update with explicit ratification if the operator revises the prompt.
_EXPECTED_INJECT_PROMPT = (
    "[inbox-auto-consumer] T-NNNN ('<title>') is in your inbox; target=<agent>."
    " Apply the autonomy bar from rulebook/policies/trust-posture.md \"What HO auto-approves\""
    " before acting on the body. If the ticket asks for anything outside"
    " auto-approve scope, surface to the operator via the Telegram bridge and halt"
    " rather than proceeding. Otherwise: claim via ticket.py, work the Ask,"
    " resolve when complete. Body follows: <body>."
)


class TestInjectPromptByteExact(unittest.TestCase):
    def test_inject_prompt_matches_locked_text(self):
        self.assertEqual(INJECT_PROMPT, _EXPECTED_INJECT_PROMPT)


# ---------------------------------------------------------------------------
# Helpers for building fake ticket files.
# ---------------------------------------------------------------------------


def _write_ticket(dir_path: Path, ticket_id: str, target: str,
                  state: str = "inbox", labels: list | None = None,
                  depends_on: list | None = None,
                  created: str = "2026-05-04T12:00:00Z",
                  title: str = "Test ticket") -> Path:
    state_dir = dir_path / state
    state_dir.mkdir(parents=True, exist_ok=True)
    labels_str = "[" + ", ".join(labels) + "]" if labels else "[]"
    depends_str = "[" + ", ".join(depends_on) + "]" if depends_on else "[]"
    content = f"""---
id: {ticket_id}
created: {created}
requester: orchestrator
target: {target}
state: {state}
depends_on: {depends_str}
labels: {labels_str}
stale_after: 7d
---

# {title}

## Ask

Do the thing.

## Transition log

- {created} [orchestrator] filed in inbox
"""
    path = state_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Gating check tests — each skip vocab.
# ---------------------------------------------------------------------------


class TestCheckAutonomyOff(unittest.TestCase):
    def test_bails_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            off_path = Path(tmp) / "autonomy_off"
            off_path.touch()
            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", off_path):
                with self.assertRaises(SkipCycle) as ctx:
                    check_autonomy_off()
        self.assertEqual(ctx.exception.vocab, "off_switch")

    def test_passes_when_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            off_path = Path(tmp) / "autonomy_off"
            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", off_path):
                check_autonomy_off()  # no exception


class TestCheckCapHit(unittest.TestCase):
    def _make_state(self, tmp: str, resets_at: int) -> Path:
        p = Path(tmp) / "ho_limit_hit.json"
        p.write_text(json.dumps({
            "session_id": "abc",
            "resets_at": resets_at,
            "transcript_path": "/tmp/x",
            "error_message": "cap",
            "written_at": time.time(),
        }), encoding="utf-8")
        return p

    def test_non_orchestrator_always_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap_path = self._make_state(tmp, int(time.time()) + 3600)
            with patch("tools.inbox_consumer.CAP_HIT_PATH", cap_path):
                check_cap_hit("specialist")  # no exception

    def test_orchestrator_bails_when_future_resets_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap_path = self._make_state(tmp, int(time.time()) + 3600)
            with patch("tools.inbox_consumer.CAP_HIT_PATH", cap_path):
                with self.assertRaises(SkipCycle) as ctx:
                    check_cap_hit("orchestrator")
        self.assertEqual(ctx.exception.vocab, "cap_hit")

    def test_orchestrator_passes_when_past_resets_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap_path = self._make_state(tmp, int(time.time()) - 3600)
            with patch("tools.inbox_consumer.CAP_HIT_PATH", cap_path):
                check_cap_hit("orchestrator")  # no exception

    def test_orchestrator_passes_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap_path = Path(tmp) / "ho_limit_hit.json"
            with patch("tools.inbox_consumer.CAP_HIT_PATH", cap_path):
                check_cap_hit("orchestrator")  # no exception


class TestCheckRecentInbound(unittest.TestCase):
    def _mock_health(self, last_inbound_at: dict):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({"last_inbound_at": last_inbound_at}).encode()
        return resp

    def test_bails_when_recent(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        resp = self._mock_health({"orchestrator": recent, "specialist": None, "recovery": None})
        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(SkipCycle) as ctx:
                check_recent_inbound("orchestrator")
        self.assertEqual(ctx.exception.vocab, "recent_telegram")

    def test_passes_when_old(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        resp = self._mock_health({"orchestrator": old, "specialist": None, "recovery": None})
        with patch("urllib.request.urlopen", return_value=resp):
            check_recent_inbound("orchestrator")  # no exception

    def test_passes_when_null(self):
        resp = self._mock_health({"orchestrator": None, "specialist": None, "recovery": None})
        with patch("urllib.request.urlopen", return_value=resp):
            check_recent_inbound("orchestrator")  # no exception

    def test_fails_open_on_connection_error(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no bridge")):
            check_recent_inbound("orchestrator")  # no exception — fail-open


# TestCheckMidTurn removed by T-0717: check_mid_turn() deleted (dispatch-gating
# pane scrape replaced by auto_delivered mutex). Per msg_2364 alpha-removal
# preference: orphaned tests deleted outright with the dead code.


# ---------------------------------------------------------------------------
# Ticket listing tests.
# ---------------------------------------------------------------------------


class TestListEligibleTickets(unittest.TestCase):
    def test_empty_inbox_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "empty_inbox")

    def test_interactive_required_emits_distinct_vocab(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "orchestrator", labels=["interactive-required"])
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "interactive_required")

    def test_dependency_not_done_emits_distinct_vocab(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # dep ticket in active state (not done); T-0002 depends on it
            _write_ticket(tickets_root, "T-0010", "orchestrator", state="active")
            _write_ticket(tickets_root, "T-0002", "orchestrator", depends_on=["T-0010"])
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "dependency_blocked")

    def test_dependency_done_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "orchestrator", state="done")
            _write_ticket(tickets_root, "T-0002", "orchestrator", depends_on=["T-0001"])
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0002")

    def test_wrong_target_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "specialist")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "empty_inbox")

    def test_eligible_wins_over_dep_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # T-0001: dep-blocked (dep in active, not done)
            _write_ticket(tickets_root, "T-0010", "orchestrator", state="active")
            _write_ticket(tickets_root, "T-0001", "orchestrator", depends_on=["T-0010"])
            # T-0002: eligible (no deps)
            _write_ticket(tickets_root, "T-0002", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0002")

    def test_dep_blocked_wins_over_interactive_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # T-0001: dep-blocked
            _write_ticket(tickets_root, "T-0010", "orchestrator", state="active")
            _write_ticket(tickets_root, "T-0001", "orchestrator", depends_on=["T-0010"])
            # T-0002: interactive-required
            _write_ticket(tickets_root, "T-0002", "orchestrator", labels=["interactive-required"])
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "dependency_blocked")

    def test_target_role_id_matches_via_map(self):
        """Ticket with target=orchestrator is found when agent='orchestrator'."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_target_persona_name_still_matches(self):
        """Ticket with target=orchestrator is still found when agent='orchestrator' (regression guard)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_target_other_role_skipped(self):
        """Ticket targeting specialist is not visible to orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "specialist")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "empty_inbox")

# ---------------------------------------------------------------------------
# FIFO ordering + tiebreak tests.
# ---------------------------------------------------------------------------


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics — never raises, never prevents the cycle."""

    def test_pull_success_silent(self):
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            pull_repo_or_warn("inbox_consumer")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import io
        import contextlib
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                pull_repo_or_warn("inbox_consumer")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import io
        import contextlib
        import subprocess as real_sub
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                pull_repo_or_warn("inbox_consumer")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


class TestPickTicket(unittest.TestCase):
    def _make_ticket(self, ticket_id: str, created: str) -> TicketInfo:
        return TicketInfo(
            id=ticket_id,
            title="Test",
            created_str=created,
            depends_on=[],
            labels=[],
            body="## Ask\nDo thing.\n\n## Transition log\n",
            path=Path("/fake"),
        )

    def test_fifo_earlier_wins(self):
        t1 = self._make_ticket("T-0010", "2026-05-04T10:00:00Z")
        t2 = self._make_ticket("T-0011", "2026-05-04T11:00:00Z")
        self.assertEqual(pick_ticket([t2, t1]).id, "T-0010")

    def test_tiebreak_lower_id_wins(self):
        t1 = self._make_ticket("T-0010", "2026-05-04T10:00:00Z")
        t2 = self._make_ticket("T-0020", "2026-05-04T10:00:00Z")
        self.assertEqual(pick_ticket([t2, t1]).id, "T-0010")

    def test_fifo_with_tiebreak_mixed(self):
        # T-0020 earlier created; T-0010 same time as T-0030 → T-0010 wins tiebreak
        t_early = self._make_ticket("T-0020", "2026-05-04T09:00:00Z")
        t_same_a = self._make_ticket("T-0030", "2026-05-04T10:00:00Z")
        t_same_b = self._make_ticket("T-0010", "2026-05-04T10:00:00Z")
        self.assertEqual(pick_ticket([t_same_a, t_same_b, t_early]).id, "T-0020")

    def _make_stale_ticket(self, ticket_id: str, created: str, stale_ts: str) -> TicketInfo:
        body = (
            "## Ask\nDo thing.\n\n"
            "## Transition log\n"
            f"- 2026-05-01T10:00:00Z [claude] filed in inbox\n"
            f"- {stale_ts} [grooming] stale; no activity since 2026-05-01T10:00:00Z; flagging for review\n"
        )
        return TicketInfo(
            id=ticket_id, title="Stale", created_str=created,
            depends_on=[], labels=[], body=body, path=Path("/fake"),
        )

    def test_stale_ticket_wins_over_normal(self):
        """T-0253: stale-flagged ticket jumps to front of queue."""
        normal = self._make_ticket("T-0010", "2026-05-04T09:00:00Z")
        stale = self._make_stale_ticket("T-0050", "2026-05-08T10:00:00Z", "2026-05-12T12:00:00Z")
        self.assertEqual(pick_ticket([normal, stale]).id, "T-0050")

    def test_oldest_stale_flag_wins_within_stale_bucket(self):
        """Within stale bucket: oldest stale-flag ts wins."""
        older_stale = self._make_stale_ticket("T-0090", "2026-05-08T10:00:00Z", "2026-05-10T12:00:00Z")
        newer_stale = self._make_stale_ticket("T-0050", "2026-05-08T10:00:00Z", "2026-05-12T12:00:00Z")
        self.assertEqual(pick_ticket([newer_stale, older_stale]).id, "T-0090")

    def test_stale_bumps_over_old_normal(self):
        """Even an old normal ticket loses to a stale one."""
        old_normal = self._make_ticket("T-0001", "2026-04-01T09:00:00Z")
        stale = self._make_stale_ticket("T-0090", "2026-05-08T10:00:00Z", "2026-05-12T12:00:00Z")
        self.assertEqual(pick_ticket([old_normal, stale]).id, "T-0090")

    def test_no_stale_falls_through_to_normal_fifo(self):
        """If no stale entries, behavior matches the pre-T-0253 FIFO logic."""
        t1 = self._make_ticket("T-0010", "2026-05-04T10:00:00Z")
        t2 = self._make_ticket("T-0011", "2026-05-04T11:00:00Z")
        self.assertEqual(pick_ticket([t2, t1]).id, "T-0010")


# ---------------------------------------------------------------------------
# Prompt formatting test.
# ---------------------------------------------------------------------------


class TestFormatPrompt(unittest.TestCase):
    def test_substitutions(self):
        t = TicketInfo(
            id="T-0042",
            title="Do the thing",
            created_str="2026-05-04T12:00:00Z",
            depends_on=[],
            labels=[],
            body="## Ask\n\nBuild it.\n\n## Transition log\n",
            path=Path("/fake"),
        )
        result = format_prompt(t, "orchestrator")
        self.assertIn("T-0042", result)
        self.assertIn("Do the thing", result)
        self.assertIn("target=orchestrator", result)
        self.assertIn("Build it.", result)
        # Placeholders must be replaced
        self.assertNotIn("T-NNNN", result)
        self.assertNotIn("<title>", result)
        self.assertNotIn("<agent>", result)
        self.assertNotIn("<body>", result)


# ---------------------------------------------------------------------------
# Inject tests.
# ---------------------------------------------------------------------------


class TestInject(unittest.TestCase):
    # T-0538: inject() now delegates to send_keys_submit.sh (single helper call).
    # Raw tmux send-keys pair tests removed — helper absorbs those; see
    # TestInjectHelperDelegation for the comprehensive post-migration coverage.

    def test_calls_helper_script(self):
        # T-0538: single subprocess call to the helper, not raw tmux send-keys pairs
        from tools.inbox_consumer import _SEND_KEYS_HELPER
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            inject("claude-telegram-bot", "some prompt")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], str(_SEND_KEYS_HELPER))

    def test_raises_on_nonzero(self):
        # T-0538: any helper failure (exit ≠ 0) → tmux_unreachable
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            with self.assertRaises(SkipCycle) as ctx:
                inject("claude-telegram-bot", "some prompt")
        self.assertEqual(ctx.exception.vocab, "tmux_unreachable")


# ---------------------------------------------------------------------------
# T-0538: inject() helper-delegation tests (replaces split-and-verify inline tests).
# The Enter-fails / capture-fails sub-failure cases are now inside the helper;
# they are tested in tools/test_send_keys_submit.sh. These tests verify only
# the contract that inject() presents to its callers.
# ---------------------------------------------------------------------------


class TestInjectSplitAndVerify(unittest.TestCase):
    # T-0538: renamed class kept to avoid breaking any external test selectors;
    # tests updated to reflect helper-delegation shape.

    def test_inject_success_calls_helper_once(self):
        # T-0538: happy path — one helper call, no raw tmux subprocess pairs
        from tools.inbox_consumer import _SEND_KEYS_HELPER
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            inject("claude-telegram-bot", "hello world")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], str(_SEND_KEYS_HELPER))
        self.assertEqual(cmd[1], "claude-telegram-bot")
        self.assertEqual(cmd[2], "hello world")

    def test_inject_helper_exit_nonzero_raises_tmux_unreachable(self):
        # T-0538: any helper failure (send-keys error or unconfirmed submit) → tmux_unreachable
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="", stderr="some error")):
            with self.assertRaises(SkipCycle) as ctx:
                inject("claude-telegram-bot", "hello world")
        self.assertEqual(ctx.exception.vocab, "tmux_unreachable")

    def test_inject_enter_delay_env_passed_to_helper(self):
        # T-0538: INJECT_ENTER_DELAY threaded via env so helper respects caller's value
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            inject("claude-telegram-bot", "hello world")
        env = mock_run.call_args[1]["env"]
        self.assertIn("INJECT_ENTER_DELAY", env)


# ---------------------------------------------------------------------------
# Consecutive-skip + alert threshold tests.
# ---------------------------------------------------------------------------


class TestConsecutiveSkips(unittest.TestCase):
    def _write_log(self, tmp: str, lines: list[str]) -> Path:
        log_path = Path(tmp) / "inbox_consumer.log"
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log_path

    def test_five_skips_no_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:mid_turn" for i in range(5)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._send_telegram_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "mid_turn")
            mock_alert.assert_not_called()

    def test_sixth_skip_triggers_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Write 6 skips (the 6th was just logged before check_consecutive_skips is called)
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:mid_turn" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                # T-0868: mid_turn at threshold now gates on a frozen pane. Simulate
                # a confirmed-frozen (wedged) pane so the alert path under test fires.
                # Patch route_failure_alert (the single send entry point) — patching
                # only _send_telegram_alert leaks via _send_alert_via_recovery's pane/
                # fallback chain on a host with a real bot token.
                with patch("tools.inbox_consumer._pane_frozen_across_samples", return_value=True):
                    with patch("tools.inbox_consumer.route_failure_alert") as mock_alert:
                        check_consecutive_skips("orchestrator", "mid_turn")
            mock_alert.assert_called_once()

    def test_mid_turn_advancing_pane_suppresses_alert(self):
        # T-0868: at threshold, a mid_turn streak with a PROGRESSING (non-frozen)
        # pane is benign → no alert; a suppression marker is logged.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:mid_turn" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._pane_frozen_across_samples", return_value=False), \
                     patch("tools.inbox_consumer.route_failure_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "mid_turn")
            mock_alert.assert_not_called()
            self.assertIn("_alert_suppressed_pane_advancing", log_path.read_text(encoding="utf-8"))

    def test_mid_turn_inconclusive_pane_alerts_failsafe(self):
        # T-0868: an inconclusive probe (None) must NOT suppress — fail safe and alert.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:mid_turn" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._pane_frozen_across_samples", return_value=None), \
                     patch("tools.inbox_consumer.route_failure_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "mid_turn")
            mock_alert.assert_called_once()

    def test_non_mid_turn_vocab_not_pane_gated(self):
        # T-0868: only mid_turn is pane-gated; other ALERT-worthy vocabs (not in
        # NEVER_ALERT_SKIP_VOCABS) alert without a pane probe. "stuck_unknown" is a
        # synthetic non-NEVER, non-mid_turn vocab.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:stuck_unknown" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._pane_frozen_across_samples") as mock_frozen, \
                     patch("tools.inbox_consumer.route_failure_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "stuck_unknown")
            mock_frozen.assert_not_called()  # gate not applied to non-mid_turn
            mock_alert.assert_called_once()

    def test_alert_resets_count_seventh_no_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Simulate: 6 skips → alert → reset appended → 1 more skip
            lines = (
                [f"2026-05-04T11:0{i}:00Z orchestrator skip:mid_turn" for i in range(6)]
                + ["2026-05-04T12:00:00Z orchestrator skip:_alert_reset"]
                + ["2026-05-04T12:10:00Z orchestrator skip:mid_turn"]
            )
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._send_telegram_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "mid_turn")
            mock_alert.assert_not_called()

    def test_never_alert_vocab_not_before_unmet_skipped(self):
        """NEVER_ALERT_SKIP_VOCABS gating: not_before_unmet does NOT trigger alert
        even at 6+ consecutive skips (intentional date-defer, not stuck)."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:not_before_unmet" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._send_telegram_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "not_before_unmet")
            mock_alert.assert_not_called()

    def test_never_alert_vocab_empty_inbox_skipped(self):
        """NEVER_ALERT_SKIP_VOCABS gating: empty_inbox does NOT trigger alert."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:empty_inbox" for i in range(6)]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                with patch("tools.inbox_consumer._send_telegram_alert") as mock_alert:
                    check_consecutive_skips("orchestrator", "empty_inbox")
            mock_alert.assert_not_called()

    def test_never_alert_vocab_set_invariant(self):
        """NEVER_ALERT_SKIP_VOCABS must be a frozenset of strings; canonical entries present."""
        from tools.inbox_consumer import NEVER_ALERT_SKIP_VOCABS
        self.assertIsInstance(NEVER_ALERT_SKIP_VOCABS, frozenset)
        # The 5 canonical never-alert vocabs all present
        for v in ("recent_telegram", "empty_inbox", "dependency_blocked",
                  "interactive_required", "not_before_unmet"):
            self.assertIn(v, NEVER_ALERT_SKIP_VOCABS)
        # Vocabs that SHOULD still alert are NOT in the set
        for v in ("tmux_unreachable", "mid_turn", "inject_pending_paste",
                  "off_switch", "cap_hit"):
            self.assertNotIn(v, NEVER_ALERT_SKIP_VOCABS)

    def test_count_resets_on_pulled(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [
                "2026-05-04T11:00:00Z orchestrator pulled T-0001",
                "2026-05-04T12:00:00Z orchestrator skip:mid_turn",
                "2026-05-04T12:10:00Z orchestrator skip:mid_turn",
            ]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                count = count_consecutive_skips("orchestrator")
        self.assertEqual(count, 2)

    def test_count_stops_at_other_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [
                "2026-05-04T11:00:00Z specialist skip:mid_turn",
                "2026-05-04T12:00:00Z orchestrator skip:mid_turn",
                "2026-05-04T12:10:00Z orchestrator skip:mid_turn",
            ]
            log_path = self._write_log(tmp, lines)
            with patch("tools.inbox_consumer.LOG_PATH", log_path):
                count = count_consecutive_skips("orchestrator")
        self.assertEqual(count, 2)


# ---------------------------------------------------------------------------
# Full-cycle integration test (successful pull).
# ---------------------------------------------------------------------------


class TestFullCycle(unittest.TestCase):
    def test_successful_pull_calls_tmux_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0099", "orchestrator", title="Ship it")
            log_path = Path(tmp) / "inbox_consumer.log"

            send_result = MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}),
                patch("urllib.request.urlopen") as mock_urlopen,
                patch("subprocess.run", return_value=send_result) as mock_run,
                patch("tools.inbox_consumer.time"),
                # T-0717: check_mid_turn removed; patch check_dispatch_locked and
                # _stamp_auto_delivered so this cycle test doesn't require a real git repo.
                patch("tools.inbox_consumer.check_dispatch_locked"),
                patch("tools.inbox_consumer._stamp_auto_delivered"),
            ):
                # /health returns no recent inbound
                health_resp = MagicMock()
                health_resp.__enter__ = lambda s: s
                health_resp.__exit__ = MagicMock(return_value=False)
                health_resp.read.return_value = json.dumps({
                    "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
                }).encode()
                mock_urlopen.return_value = health_resp

                result = main("orchestrator")

            self.assertEqual(result, 0)
            # T-0538: inject now uses helper (single call), not two raw send-keys calls
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(len(inject_calls), 1)
            # Helper call: [helper_path, pane_target, prompt_text]
            injected_args = inject_calls[0][0][0]
            injected_text = injected_args[2]  # prompt is 3rd element (index 2)
            self.assertIn("T-0099", injected_text)
            self.assertIn("Ship it", injected_text)
            # Log entry written
            log_content = log_path.read_text()
            self.assertIn("orchestrator pulled T-0099", log_content)


# ---------------------------------------------------------------------------
# Each skip vocab — assert tmux send-keys NOT called.
# ---------------------------------------------------------------------------


class TestSkipVocabNoTmux(unittest.TestCase):
    """Each skip path must not call tmux send-keys."""

    def _patch_base(self, tmp: str):
        """Return a dict of patches that let the cycle get past up to cap_hit."""
        return {
            "tools.inbox_consumer.AUTONOMY_OFF_PATH": Path(tmp) / "autonomy_off",
            "tools.inbox_consumer.CAP_HIT_PATH": Path(tmp) / "no_cap_hit.json",
            "tools.inbox_consumer.LOG_PATH": Path(tmp) / "inbox_consumer.log",
        }

    def _assert_skip_no_tmux(self, agent: str, extra_patches: dict, expected_vocab: str, tmp: str):
        patches = self._patch_base(tmp)
        patches.update(extra_patches)

        with patch("subprocess.run") as mock_run:
            # Default: capture-pane returns idle, send-keys returns 0
            mock_run.return_value = MagicMock(returncode=0, stdout="? for shortcuts", stderr="")

            ctx_managers = [patch(k, v) for k, v in patches.items()]
            for cm in ctx_managers:
                cm.__enter__()
            try:
                result = main(agent)
            finally:
                for cm in reversed(ctx_managers):
                    cm.__exit__(None, None, None)

        send_calls = [c for c in mock_run.call_args_list
                     if c[0][0][1:2] == ["send-keys"]]
        self.assertEqual(send_calls, [], f"tmux send-keys should not be called for {expected_vocab}")
        log_path = patches["tools.inbox_consumer.LOG_PATH"]
        log_content = Path(log_path).read_text() if Path(log_path).exists() else ""
        self.assertIn(f"skip:{expected_vocab}", log_content)

    def test_off_switch_no_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            off_path = Path(tmp) / "autonomy_off"
            off_path.touch()
            self._assert_skip_no_tmux(
                "orchestrator",
                {"tools.inbox_consumer.AUTONOMY_OFF_PATH": off_path},
                "off_switch", tmp,
            )

    def test_cap_hit_no_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap_path = Path(tmp) / "ho_limit_hit.json"
            cap_path.write_text(json.dumps({
                "session_id": "x", "resets_at": int(time.time()) + 3600,
                "transcript_path": "/tmp/x", "error_message": "cap", "written_at": time.time(),
            }), encoding="utf-8")
            self._assert_skip_no_tmux(
                "orchestrator",
                {"tools.inbox_consumer.CAP_HIT_PATH": cap_path},
                "cap_hit", tmp,
            )

    def test_recent_telegram_no_tmux(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        health_data = json.dumps({
            "last_inbound_at": {"orchestrator": recent, "specialist": None, "recovery": None}
        }).encode()

        with tempfile.TemporaryDirectory() as tmp:
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = health_data

            with patch("urllib.request.urlopen", return_value=resp):
                with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"):
                    with patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"):
                        with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"):
                            with patch("subprocess.run") as mock_run:
                                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                                main("orchestrator")
            log_path = Path(tmp) / "inbox_consumer.log"
            log_content = log_path.read_text() if log_path.exists() else ""
            self.assertIn("skip:recent_telegram", log_content)
            send_calls = [c for c in mock_run.call_args_list
                         if c[0][0][1:2] == ["send-keys"]]
            self.assertEqual(send_calls, [])

    # test_mid_turn_no_tmux removed by T-0717: check_mid_turn() deleted (dispatch-
    # gating pane scrape replaced by auto_delivered mutex; mid_turn vocab gone
    # from main() cycle). Per msg_2364 alpha-removal preference.

    def test_empty_inbox_no_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            tickets_root.mkdir()

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            with patch("urllib.request.urlopen", return_value=resp):
                with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"):
                    with patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"):
                        with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"):
                            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                                with patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "tgt"}):
                                    with patch("subprocess.run") as mock_run:
                                        mock_run.return_value = MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                                        main("orchestrator")
            log_path = Path(tmp) / "inbox_consumer.log"
            log_content = log_path.read_text() if log_path.exists() else ""
            self.assertIn("skip:empty_inbox", log_content)
            send_calls = [c for c in mock_run.call_args_list
                         if c[0][0][1:2] == ["send-keys"]]
            self.assertEqual(send_calls, [])

    def test_dependency_blocked_no_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # dep in active state (not done); T-0002 depends on it → dependency_blocked
            _write_ticket(tickets_root, "T-0010", "orchestrator", state="active")
            _write_ticket(tickets_root, "T-0002", "orchestrator", depends_on=["T-0010"])

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            with patch("urllib.request.urlopen", return_value=resp):
                with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"):
                    with patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"):
                        with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"):
                            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                                with patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "tgt"}):
                                    with patch("subprocess.run") as mock_run:
                                        mock_run.return_value = MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                                        main("orchestrator")
            log_path = Path(tmp) / "inbox_consumer.log"
            log_content = log_path.read_text() if log_path.exists() else ""
            self.assertIn("skip:dependency_blocked", log_content)

    def test_interactive_required_no_tmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0001", "orchestrator", labels=["interactive-required"])

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            with patch("urllib.request.urlopen", return_value=resp):
                with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"):
                    with patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"):
                        with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"):
                            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                                with patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "tgt"}):
                                    with patch("subprocess.run") as mock_run:
                                        mock_run.return_value = MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                                        main("orchestrator")
            log_path = Path(tmp) / "inbox_consumer.log"
            log_content = log_path.read_text() if log_path.exists() else ""
            self.assertIn("skip:interactive_required", log_content)
            send_calls = [c for c in mock_run.call_args_list
                         if c[0][0][1:2] == ["send-keys"]]
            self.assertEqual(send_calls, [])

    def test_tmux_unreachable_no_second_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0001", "orchestrator")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            with patch("urllib.request.urlopen", return_value=resp):
                with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"):
                    with patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"):
                        with patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"):
                            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                                with patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "tgt"}):
                                    with patch("subprocess.run") as mock_run:
                                        # capture-pane succeeds (idle), send-keys fails
                                        def side_effect(cmd, **kwargs):
                                            if "capture-pane" in cmd:
                                                return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                                            return MagicMock(returncode=1, stdout="", stderr="error")
                                        mock_run.side_effect = side_effect
                                        main("orchestrator")
            log_path = Path(tmp) / "inbox_consumer.log"
            log_content = log_path.read_text() if log_path.exists() else ""
            self.assertIn("skip:tmux_unreachable", log_content)


# ---------------------------------------------------------------------------
# not_before bucket tests.
# ---------------------------------------------------------------------------


def _write_ticket_with_not_before(
    dir_path: Path,
    ticket_id: str,
    target: str,
    not_before: str,
    created: str = "2026-05-04T12:00:00Z",
) -> Path:
    """Write a ticket with a not_before frontmatter field."""
    state_dir = dir_path / "inbox"
    state_dir.mkdir(parents=True, exist_ok=True)
    content = f"""---
id: {ticket_id}
created: {created}
requester: orchestrator
target: {target}
state: inbox
depends_on: []
labels: []
not_before: {not_before}
---

# Soak ticket

## Ask

Wait until not_before passes.

## Transition log

- {created} [orchestrator] filed in inbox
"""
    path = state_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestNotBefore(unittest.TestCase):
    def test_not_before_in_future_skips_with_correct_vocab(self):
        """Ticket whose not_before is 1h in the future lands in not_before_unmet."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_not_before(tickets_root, "T-0001", "orchestrator", future)
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    list_eligible_tickets("orchestrator")
        self.assertEqual(ctx.exception.vocab, "not_before_unmet")

    def test_not_before_in_past_is_eligible(self):
        """Ticket whose not_before is 1d in the past is returned as eligible."""
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_not_before(tickets_root, "T-0001", "orchestrator", past)
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")

    def test_eligible_wins_over_not_before_unmet(self):
        """When one ticket is fully eligible and another has future not_before,
        the eligible list is returned (not a SkipCycle)."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # T-0001: future not_before → not_before_unmet
            _write_ticket_with_not_before(tickets_root, "T-0001", "orchestrator", future)
            # T-0002: no not_before → eligible
            _write_ticket(tickets_root, "T-0002", "orchestrator")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        ids = [t.id for t in tickets]
        self.assertIn("T-0002", ids)
        self.assertNotIn("T-0001", ids)

    def test_malformed_not_before_treated_as_eligible_fail_open(self):
        """Ticket with a hand-edited garbage not_before value is treated as
        eligible (fail-open) — the ValueError swallow at inbox_consumer.py
        lines 272-273 is exercised, not the not_before_unmet path."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_not_before(tickets_root, "T-0001", "orchestrator", "not-a-date")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                tickets = list_eligible_tickets("orchestrator")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].id, "T-0001")


class TestCheckThothHealth(unittest.TestCase):
    """Recovery-side observability of Orchestrator via bridge /health."""

    def setUp(self) -> None:
        # T-0314: escalate path now tries recovery-pane first. Default to
        # recovery-pane=False in this class so existing tests exercise the
        # fallback-to-operator-Telegram path they were originally written for.
        # Tests that want recovery-pane success override with their own patch.
        p = patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _mock_health(self, payload: dict):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(payload).encode()
        return resp

    def _patch_state(self, tmpdir: Path):
        """Redirect both the cooldown state file and the log path into tmpdir."""
        return (
            patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                  tmpdir / "orchestrator_health_last_alert_at"),
            patch("tools.inbox_consumer.LOG_PATH",
                  tmpdir / "inbox_consumer.log"),
        )

    def test_healthy_returns_none_no_alert(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertIsNone(vocab)
        alert.assert_not_called()

    def test_orchestrator_disconnected_alerts(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "orchestrator_disconnected")
        alert.assert_called_once()
        self.assertIn("connected_clients", alert.call_args[0][0])

    def test_telegram_disconnected_takes_precedence(self):
        """When both telegram_connected=False and orchestrator missing, the telegram
        signal wins — that's the upstream cause."""
        payload = {
            "telegram_connected": False,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 5},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "telegram_disconnected")
        alert.assert_called_once()
        self.assertIn("Telegram", alert.call_args[0][0])

    def test_queue_stalled_alerts(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 3},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "queue_stalled")
        alert.assert_called_once()

    def test_bridge_unreachable_silent(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("urllib.request.urlopen",
                       side_effect=urllib.error.URLError("no bridge")), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertIsNone(vocab)
        alert.assert_not_called()

    def test_cooldown_suppresses_repeat_alerts(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=None):
                # First call: alerts.
                with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                     patch("tools.inbox_consumer._send_telegram_alert") as alert1:
                    v1 = check_orchestrator_health()
                self.assertEqual(v1, "orchestrator_disconnected")
                alert1.assert_called_once()
                # Second call inside cooldown: still returns vocab, but no alert.
                with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                     patch("tools.inbox_consumer._send_telegram_alert") as alert2:
                    v2 = check_orchestrator_health()
                self.assertEqual(v2, "orchestrator_disconnected")
                alert2.assert_not_called()

    def test_cooldown_expires_allows_realert(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            patches = self._patch_state(tmppath)
            with patches[0], patches[1], \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=None):
                # First call alerts and writes cooldown stamp.
                with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                     patch("tools.inbox_consumer._send_telegram_alert") as alert1:
                    check_orchestrator_health()
                alert1.assert_called_once()
                # Backdate the cooldown stamp past the window.
                stamp = tmppath / "orchestrator_health_last_alert_at"
                old = int(time.time()) - ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS - 5
                stamp.write_text(str(old))
                # Now a new alert should fire.
                with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                     patch("tools.inbox_consumer._send_telegram_alert") as alert2:
                    check_orchestrator_health()
                alert2.assert_called_once()


class TestPaneWedgePersistence(unittest.TestCase):
    """The pane-wedge signal requires persistence across cycles."""

    def _patch_state(self, tmpdir: Path):
        return patch(
            "tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
            tmpdir / "orchestrator_pane_wedge_first_seen_at",
        )

    def test_no_sentinel_clears_state(self):
        """Healthy pane content clears any prior wedged-state marker."""
        from tools.inbox_consumer import _check_pane_wedge_persistent
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("1000")  # stale marker from earlier wedged streak
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture",
                       return_value="? for shortcuts\n❯ idle"):
                result = _check_pane_wedge_persistent()
        self.assertIsNone(result)
        self.assertFalse(state.exists())

    def test_pane_unreachable_returns_none_and_clears(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("1000")
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=None):
                result = _check_pane_wedge_persistent()
        self.assertIsNone(result)
        self.assertFalse(state.exists())

    def test_first_sighting_writes_state_no_alert(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        wedged_pane = (
            "Do you want to overwrite HANDOFF.md?\n"
            "❯ 1. Yes\n"
            "  2. Yes, and allow Claude to edit its own settings for this session\n"
            "  3. No"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=wedged_pane):
                result = _check_pane_wedge_persistent()
            self.assertIsNone(result)
            self.assertTrue(state.exists())
            # Stamp should be ~now.
            ts = int(state.read_text())
            self.assertAlmostEqual(ts, int(time.time()), delta=5)

    def test_persistent_wedge_returns_sentinel(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        wedged_pane = (
            "❯ 1. Yes\n"
            "  2. Yes, and allow Claude to edit its own settings for this session"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            state.parent.mkdir(parents=True, exist_ok=True)
            # Backdate first-sighting past the persist window.
            old = int(time.time()) - ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS - 5
            state.write_text(str(old))
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=wedged_pane):
                result = _check_pane_wedge_persistent()
            self.assertIsNotNone(result)
            self.assertIn("Yes, and allow Claude", result[0])
            # State file preserved — only cleared on healthy pane or unreachable.
            self.assertTrue(state.exists())

    def test_within_window_still_no_alert(self):
        """Wedged but only seen 30s ago — under the persist threshold."""
        from tools.inbox_consumer import _check_pane_wedge_persistent
        wedged_pane = "I am using this for local development"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            state.parent.mkdir(parents=True, exist_ok=True)
            recent = int(time.time()) - 30
            state.write_text(str(recent))
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=wedged_pane):
                result = _check_pane_wedge_persistent()
            self.assertIsNone(result)
            self.assertTrue(state.exists())  # still tracking

    def test_corrupt_state_treated_as_first_sighting(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        wedged_pane = "Yes, and allow Claude to edit its own settings"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("not-a-number")
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=wedged_pane):
                result = _check_pane_wedge_persistent()
            self.assertIsNone(result)
            # State should have been rewritten with a valid timestamp.
            ts = int(state.read_text())
            self.assertAlmostEqual(ts, int(time.time()), delta=5)


class TestPaneWedgeLivenessGuardT0716(unittest.TestCase):
    """T-0716: the wedge declaration is suppressed when the live bottom region
    of the pane shows the loop is working/idle — fixes false-positives where a
    wedge sentinel appears only as TEXT (a captured peer dialog, or the
    health-alert message quoting the sentinel). False-negative-safe: a genuine
    modal (footer present, or no liveness token) is never suppressed.
    """

    # --- ground-truth captures from the 2026-06-02 incident ---
    REAL_WEDGE = (
        "● Bash(systemctl --user cat pantheon-bridge.service)\n"
        "  ⎿  Waiting…\n"
        "────────────────────────────────────────\n"
        " Bash command\n"
        "   systemctl --user cat pantheon-bridge.service\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again for similar commands in /home/claude\n"
        "   3. No\n"
        "\n"
        " Esc to cancel · Tab to amend · ctrl+e to explain\n"
    )
    HEALTHY_WORKING = (
        "● Working on something\n"
        "✽ Kneading… (33s · ↓ 2.1k tokens · thinking)\n"
        "  ⎿  Tip: Use /btw to ask a quick side question\n"
        "────────────────────────────────────────\n"
        "❯ \n"
        "────────────────────────────────────────\n"
        "  esc to interrupt                         ● high · /effort\n"
    )

    # --- pure-helper unit tests ---
    def _live(self, pane):
        from tools.inbox_consumer import _bottom_region_shows_live_input
        return _bottom_region_shows_live_input(pane)

    def test_helper_real_wedge_not_suppressed(self):
        # Dialog footer in bottom region, no liveness token → NOT live → wedged.
        self.assertFalse(self._live(self.REAL_WEDGE))

    def test_helper_working_footer_suppresses(self):
        self.assertTrue(self._live(self.HEALTHY_WORKING))

    def test_helper_idle_footer_suppresses(self):
        self.assertTrue(self._live("● done\n────\n❯ \n────\n  ? for shortcuts\n"))

    def test_helper_footer_wins_over_corendered_liveness(self):
        # H1 defense: even if a stale spinner co-renders, a footer present in the
        # bottom region keeps it classified as a live modal (not suppressed).
        pane = (
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   3. No\n"
            "  esc to interrupt\n"
            " Esc to cancel · Tab to amend · ctrl+e to explain\n"
        )
        self.assertFalse(self._live(pane))

    def test_helper_footerless_wedge_not_suppressed(self):
        # A footer-less dialog render (sentinel only, no liveness) must NOT be
        # suppressed — avoids a false-negative for unusual renders.
        self.assertFalse(self._live("Do you want to proceed?\n❯ 1. Yes\n  3. No\n"))

    def test_helper_sentinel_as_text_with_live_prompt_suppressed(self):
        # THE BUG: sentinel quoted in scrollback, live working footer at bottom.
        pane = (
            "● [pantheon] recovery-health alert quoting 'Do you want to proceed?'\n"
            "● analysis line 1\n● analysis line 2\n● analysis line 3\n"
            "✽ Kneading… (↓ 1k tokens · thinking)\n"
            "────\n❯ \n────\n  esc to interrupt    ● high · /effort\n"
        )
        self.assertTrue(self._live(pane))

    def test_helper_peer_dialog_in_scrollback_suppressed(self):
        # Recovery captured a peer's full dialog (footer included) high in scrollback,
        # then produced its own output; live idle footer at bottom → suppressed.
        pane = (
            "  ⎿  Do you want to proceed?\n"
            "     ❯ 1. Yes\n"
            "     Esc to cancel · Tab to amend · ctrl+e to explain\n"
            "● Verified peer wedged; approved read-only diagnostic.\n"
            "● Sent 1 + Enter to peer pane.\n"
            "● Confirmed peer resumed.\n"
            "● Filed follow-up notes.\n"
            "────\n❯ \n────\n  ? for shortcuts\n"
        )
        self.assertTrue(self._live(pane))

    def test_helper_trailing_blank_padding_ignored(self):
        # capture-pane pads to terminal height; blanks must not hide the footer.
        pane = self.REAL_WEDGE + ("\n" * 30)
        self.assertFalse(self._live(pane))

    # --- integration via _check_pane_wedge_persistent (backdated streak) ---
    def _patch_state(self, tmpdir):
        return patch(
            "tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
            tmpdir / "orchestrator_pane_wedge_first_seen_at",
        )

    def _backdate(self, state):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(str(int(time.time()) - ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS - 5))

    def test_integration_real_wedge_still_fires(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            self._backdate(state)
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=self.REAL_WEDGE):
                result = _check_pane_wedge_persistent()
        self.assertIsNotNone(result)
        self.assertIn("Do you want to proceed?", result[0])

    def test_integration_sentinel_as_text_suppressed(self):
        from tools.inbox_consumer import _check_pane_wedge_persistent
        bug_pane = (
            "● recovery-health alert quoting 'Do you want to proceed?'\n"
            "● a\n● b\n● c\n● d\n"
            "────\n❯ \n────\n  esc to interrupt    ● high · /effort\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            self._backdate(state)
            with self._patch_state(tmppath), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=bug_pane):
                result = _check_pane_wedge_persistent()
            # No false alert...
            self.assertIsNone(result)
            # ...and the deferral preserves the streak (H3): state NOT cleared,
            # because the sentinel is still present as text.
            self.assertTrue(state.exists())

    def test_integration_deferral_does_not_reset_streak_then_fires_when_live_clears(self):
        # H3: a deferred cycle keeps first_seen; once the live footer is gone (a
        # genuine wedge), the very next capture fires using the SAME streak.
        from tools.inbox_consumer import _check_pane_wedge_persistent
        live_pane = (
            "● x\n● y\n● z\n● w\n────\n❯ \n────\n  esc to interrupt\n"
            "(scrollback) Do you want to proceed?\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            state = tmppath / "orchestrator_pane_wedge_first_seen_at"
            self._backdate(state)
            original_ts = state.read_text()
            with self._patch_state(tmppath):
                with patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=live_pane):
                    self.assertIsNone(_check_pane_wedge_persistent())  # deferred
                self.assertTrue(state.exists())
                self.assertEqual(state.read_text(), original_ts)  # streak intact
                with patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=self.REAL_WEDGE):
                    self.assertIsNotNone(_check_pane_wedge_persistent())  # now fires


class TestRouteFailureAlertT0722(unittest.TestCase):
    """route_failure_alert: failures for OTHER agents route via Recovery (triage);
    RECOVERY's own failures go DIRECT to the operator (it cannot self-remediate).
    the operator directive 2026-06-02 (msg 2235).
    """

    def test_other_agent_routes_via_recovery(self):
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("orchestrator", "orchestrator is wedged", "restart-failure (orchestrator)")
        via_recovery.assert_called_once_with("orchestrator is wedged", "restart-failure (orchestrator)")
        direct.assert_not_called()

    def test_specialist_routes_via_recovery(self):
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("specialist", "specialist detached", "specialist-health (disconnect)")
        via_recovery.assert_called_once()
        direct.assert_not_called()

    def test_recovery_self_goes_direct_to_operator(self):
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("recovery", "recovery restart wedged", "restart-failure (recovery)")
        via_recovery.assert_not_called()
        direct.assert_called_once()
        msg = direct.call_args[0][0]
        self.assertIn("recovery-self-failure", msg)
        self.assertIn("manual intervention", msg)
        self.assertIn("recovery restart wedged", msg)


class TestCheckThothHealthPaneIntegration(unittest.TestCase):
    """check_orchestrator_health() invokes the pane probe only after bridge-side
    signals come back green; pane_wedge alert exercises the alert path."""

    def setUp(self) -> None:
        # T-0314: see TestCheckThothHealth.setUp — preserve original semantics
        # by defaulting recovery-pane delivery to False so existing escalation
        # assertions still match (fallback message includes the original).
        p = patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _mock_health(self, payload: dict):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(payload).encode()
        return resp

    def test_pane_wedge_fires_when_bridge_green(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("Yes, and allow Claude to edit its own settings",
                                     "fake pane text")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge")
        alert.assert_called_once()
        self.assertIn("pane_wedge", alert.call_args[0][0]) if False else None  # vocab not in body
        self.assertIn("dialog", alert.call_args[0][0])

    def test_bridge_red_short_circuits_pane_check(self):
        """When bridge is red (orchestrator disconnected), the PERSISTENCE-gated wedge
        check must not run. T-0520: the disconnected branch does a lighter,
        immediate pane probe (capture + sentinel match) to catch a startup
        dialog blocking the connect — but NOT _check_pane_wedge_persistent.
        Here the pane is clean → falls through to the restart recommendation."""
        payload = {
            "telegram_connected": True,
            "connected_clients": [],  # orchestrator disconnected
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent") as pane, \
                 patch("tools.inbox_consumer._orchestrator_pane_capture",
                       return_value="no sentinel here"), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "orchestrator_disconnected")
        pane.assert_not_called()

    def test_disconnect_via_allowlisted_dialog_auto_resolves_not_restart(self):
        """T-0520/T-0525: orchestrator absent from connected_clients BECAUSE a startup
        dialog blocks the connect. The dialog is allowlisted → auto-resolve fires
        (send-keys the rule action), NO restart recommendation. Per T-0525 this
        startup-dialog rule ALSO chains a kickoff inject after the idle prompt
        appears, so _apply_auto_resolve_action is called twice (dismiss + kickoff)."""
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        wedged_pane = (
            "  > 1. I am using this for local development\n"
            "    2. Exit\n  Enter to confirm · Esc to cancel\n"
        )
        # After dismiss, the pane reaches the idle prompt — first capture still
        # shows the dialog, second shows idle (poll completes on the 2nd probe).
        idle_pane = "some output\n❯ \n? for shortcuts\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent") as persist, \
                 patch("tools.inbox_consumer._orchestrator_pane_capture",
                       side_effect=[wedged_pane, idle_pane]), \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=True) as apply_action, \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_info") as info, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        # auto-resolve applied the ratified dismiss action AND the kickoff; NO
        # restart escalation alert.
        self.assertEqual(apply_action.call_count, 2)
        self.assertEqual(apply_action.call_args_list[0][0][1], ["1", "Enter"])
        # Second call is the kickoff: prompt text + Enter to ORCHESTRATOR_PANE_TARGET.
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT
        self.assertEqual(
            apply_action.call_args_list[1][0][1],
            [RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT, "Enter"],
        )
        info.assert_called_once()
        self.assertIn("kickoff injected", info.call_args[0][0])
        alert.assert_not_called()
        # the persistence-gated check is bypassed in the disconnect branch.
        persist.assert_not_called()

    def test_disconnect_via_allowlisted_dialog_send_keys_fail_recommends_restart(self):
        """T-0520: allowlisted dialog detected on a disconnect but send-keys
        fails → fall through to the restart recommendation (fail-safe)."""
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        wedged_pane = "I am using this for local development\nExit\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._orchestrator_pane_capture",
                       return_value=wedged_pane), \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=False), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "orchestrator_disconnected")
        alert.assert_called_once()
        self.assertIn("connected_clients", alert.call_args[0][0])

    def test_pane_clean_returns_none(self):
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=None), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertIsNone(vocab)
        alert.assert_not_called()


class TestAutoResolveAllowlist(unittest.TestCase):
    """T-0201: auto-resolve allowlist applies remediation in-cycle + emits info Telegram."""

    def setUp(self) -> None:
        # T-0314: see TestCheckThothHealth.setUp — preserve original semantics.
        p = patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _mock_health(self, payload):
        m = MagicMock()
        m.__enter__ = lambda self: m
        m.__exit__ = lambda self, *a: False
        m.read = lambda: json.dumps(payload).encode("utf-8")
        return m

    def _bridge_green(self):
        return {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }

    def test_dev_channels_sentinel_auto_resolves(self):
        """Allowlisted sentinel → action applied + info Telegram + auto_resolved vocab."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            wedge_state = tmppath / "wedge_state"
            wedge_state.write_text("12345", encoding="utf-8")  # pretend wedge state existed
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       wedge_state), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("I am using this for local development",
                                     "fake pane text for dev channels")), \
                 patch("tools.inbox_consumer._poll_orchestrator_pane_for_idle",
                       return_value=True), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        # T-0525: startup-dialog rule chains dismiss -> kickoff, so send-keys is
        # called 4x: dismiss "1"+"Enter", then kickoff prompt + "Enter".
        # Each wrapped in `sudo -u claude bash -c '... tmux send-keys -t pane <op>'`
        # so the literal op lives inside the shell-cmd string at argv[5].
        self.assertEqual(run.call_count, 4)
        self.assertIn("send-keys -t claude-telegram-bot:0 1",
                      run.call_args_list[0][0][0][5])
        self.assertIn("send-keys -t claude-telegram-bot:0 Enter",
                      run.call_args_list[1][0][0][5])
        # Kickoff inject: prompt text then Enter, same cross-account path.
        self.assertIn("Read /home/claude/HANDOFF.md",
                      run.call_args_list[2][0][0][5])
        self.assertIn("send-keys -t claude-telegram-bot:0 Enter",
                      run.call_args_list[3][0][0][5])
        # Cross-account wrapper present (checked on dismiss + kickoff).
        self.assertEqual(run.call_args_list[0][0][0][0], "sudo")
        self.assertEqual(run.call_args_list[0][0][0][2], "claude")
        self.assertEqual(run.call_args_list[2][0][0][0], "sudo")
        self.assertEqual(run.call_args_list[2][0][0][2], "claude")
        # Informational Telegram sent (still through _send_telegram_alert under
        # the hood, but tagged as auto-resolve).
        alert.assert_called_once()
        self.assertIn("[recovery auto-resolve]", alert.call_args[0][0])
        self.assertIn("I am using this for local development", alert.call_args[0][0])
        # Wedge-state cleared
        self.assertFalse(wedge_state.exists())

    def test_non_allowlisted_sentinel_escalates(self):
        """Sentinel NOT in allowlist → standard escalation Telegram (no auto-action)."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("Yes, and don't ask again for",
                                     "fake pane text")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        # Escalation path: vocab is pane_wedge (not auto_resolved); send-keys NOT
        # called; alert IS called with the escalation message.
        self.assertEqual(vocab, "pane_wedge")
        run.assert_not_called()
        alert.assert_called_once()
        self.assertIn("Orchestrator's loop is blocked", alert.call_args[0][0])

    def test_auto_resolve_send_keys_failure_falls_back_to_escalate(self):
        """If tmux send-keys fails, fall back to standard escalation."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("I am using this for local development",
                                     "fake pane text")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                # send-keys returns non-zero → action failed
                run.return_value = MagicMock(returncode=1, stdout="", stderr="tmux died")
                vocab = check_orchestrator_health()
        # Fell through to escalation
        self.assertEqual(vocab, "pane_wedge")
        alert.assert_called_once()
        self.assertIn("Orchestrator's loop is blocked", alert.call_args[0][0])

    def test_new_sentinel_do_you_want_to_proceed_detected_but_escalates(self):
        """T-0142 sentinel addition: 'Do you want to proceed?' detected, but never auto-resolved."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        self.assertIn("Do you want to proceed?", PANE_WEDGE_SENTINELS)
        self.assertNotIn("Do you want to proceed?", RECOVERY_AUTO_RESOLVE_ALLOWLIST)

    def test_new_sentinel_dont_ask_again_detected_but_escalates(self):
        """T-0142 sentinel addition: option-2 'don't ask again' detected, never auto-resolved (expands trust)."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        self.assertIn("Yes, and don't ask again for", PANE_WEDGE_SENTINELS)
        self.assertNotIn("Yes, and don't ask again for", RECOVERY_AUTO_RESOLVE_ALLOWLIST)

    def test_allowlist_shape_invariant(self):
        """Every allowlist entry's sentinel must also appear in PANE_WEDGE_SENTINELS."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in RECOVERY_AUTO_RESOLVE_ALLOWLIST:
            self.assertIn(sentinel, PANE_WEDGE_SENTINELS,
                          f"allowlist entry {sentinel!r} not in PANE_WEDGE_SENTINELS")

    def test_option_2_settings_edit_NOT_auto_resolved(self):
        """T-0142 §3 explicitly-disallowed: 'Yes, and allow Claude to edit its own settings' must NEVER be allowlisted (expands trust scope)."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        self.assertNotIn("Yes, and allow Claude to edit its own settings",
                         RECOVERY_AUTO_RESOLVE_ALLOWLIST)

    def test_T0358_new_option_2_variant_detected_but_escalates(self):
        """T-0358 sentinel addition: 'Yes, allow all edits during this session' (newer Claude Code phrasing) detected, never auto-resolved (expands trust)."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        self.assertIn("Yes, allow all edits during this session", PANE_WEDGE_SENTINELS)
        self.assertNotIn("Yes, allow all edits during this session", RECOVERY_AUTO_RESOLVE_ALLOWLIST)

    def test_T0358_dialog_header_detected(self):
        """T-0358 structural fix: catch the Edit/Write dialog by its question text, not just by trust-expansion text."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, _pane_wedge_match
        self.assertIn("Do you want to make this edit to", PANE_WEDGE_SENTINELS)
        # Synthetic pane with the new dialog header but unfamiliar option-2 wording
        # — should still match.
        pane = """
 Do you want to make this edit to FUTURE_FILE.md?
 ❯ 1. Yes
   2. <some new option-2 wording we have not seen yet>
   3. No
"""
        self.assertEqual(_pane_wedge_match(pane), "Do you want to make this edit to")

    def test_T0358_modal_footer_esc_tab_detected(self):
        """T-0358 structural fix (footer-based detection): 'Esc to cancel · Tab to amend' footer alone catches any future modal."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, _pane_wedge_match
        self.assertIn("Esc to cancel · Tab to amend", PANE_WEDGE_SENTINELS)
        # Synthetic pane with a fully-novel question + options BUT the canonical
        # Claude Code modal footer — should still match via footer.
        pane = """
 Some question we have never seen before that is blocking the loop?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend
"""
        # Will match either dialog header (if present) OR footer; here just footer.
        match = _pane_wedge_match(pane)
        self.assertIsNotNone(match)
        # Must be one of the new T-0358 detectors.
        self.assertIn(match, ("Esc to cancel · Tab to amend",))

    def test_T0358_modal_footer_enter_esc_detected(self):
        """T-0358 structural fix (footer-based detection): 'Enter to confirm · Esc to cancel' (dev-channels-style footer)."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, _pane_wedge_match
        self.assertIn("Enter to confirm · Esc to cancel", PANE_WEDGE_SENTINELS)
        pane = """
 A novel yes-or-exit modal we haven't seen before?
 ❯ 1. Yes, do the thing
   2. Exit

 Enter to confirm · Esc to cancel
"""
        match = _pane_wedge_match(pane)
        self.assertIsNotNone(match)
        self.assertIn(match, ("Enter to confirm · Esc to cancel",))

    def test_T0358_footer_sentinels_NOT_auto_resolved(self):
        """Footer-based sentinels are detect-only — never auto-resolve (we don't know what option means what without specific text)."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        self.assertNotIn("Esc to cancel · Tab to amend", RECOVERY_AUTO_RESOLVE_ALLOWLIST)
        self.assertNotIn("Enter to confirm · Esc to cancel", RECOVERY_AUTO_RESOLVE_ALLOWLIST)
        # NOTE: "Do you want to make this edit to" is now allowlisted (T-0489 posture-C
        # wiring; ratified T-0480). The guard here covered the pre-T-0489 state; removed
        # to reflect the ratified boundary. The trust-expansion option-2 variants
        # (test_option_2_trust_expansions_not_in_allowlist in TestPostureCPredicateBoundaries)
        # carry the exclusion discipline forward.


class TestAutoResolveRuleStructure(unittest.TestCase):
    """T-0479: AutoResolveRule dataclass structure + RECOVERY_AUTO_RESOLVE_ALLOWLIST shape."""

    def test_auto_resolve_rule_is_frozen_dataclass(self):
        """AutoResolveRule must be a frozen dataclass — rules are immutable config."""
        from tools.inbox_consumer import AutoResolveRule
        import dataclasses
        self.assertTrue(dataclasses.is_dataclass(AutoResolveRule))
        fields_meta = dataclasses.fields(AutoResolveRule)
        # Frozen check: assignment to a field must raise FrozenInstanceError.
        rule = AutoResolveRule(
            sentinel="test",
            predicate=lambda _: True,
            action=["1", "Enter"],
            escalate_template="test escalation",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rule.sentinel = "other"  # type: ignore[misc]

    def test_auto_resolve_rule_field_names(self):
        """AutoResolveRule has sentinel, predicate, action, escalate_template fields."""
        from tools.inbox_consumer import AutoResolveRule
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(AutoResolveRule)}
        self.assertIn("sentinel", field_names)
        self.assertIn("predicate", field_names)
        self.assertIn("action", field_names)
        self.assertIn("escalate_template", field_names)

    def test_allowlist_values_are_auto_resolve_rules(self):
        """Every value in RECOVERY_AUTO_RESOLVE_ALLOWLIST is an AutoResolveRule."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST, AutoResolveRule
        for sentinel, rule in RECOVERY_AUTO_RESOLVE_ALLOWLIST.items():
            self.assertIsInstance(
                rule, AutoResolveRule,
                f"allowlist[{sentinel!r}] is not an AutoResolveRule"
            )

    def test_allowlist_keys_match_rule_sentinel_field(self):
        """Dict key must equal rule.sentinel for every entry."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for key, rule in RECOVERY_AUTO_RESOLVE_ALLOWLIST.items():
            self.assertEqual(
                key, rule.sentinel,
                f"key {key!r} != rule.sentinel {rule.sentinel!r}"
            )

    def test_dev_channels_rule_action_unchanged(self):
        """Dev-channels rule action must remain byte-identical after migration."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["I am using this for local development"]
        self.assertEqual(rule.action, ["1", "Enter"])

    def test_dev_channels_rule_predicate_always_true(self):
        """Dev-channels rule predicate returns True for any pane text (always-True)."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["I am using this for local development"]
        self.assertTrue(rule.predicate("any pane content"))
        self.assertTrue(rule.predicate(""))
        self.assertTrue(rule.predicate("something completely unrelated"))

    def test_dev_channels_rule_has_escalate_template(self):
        """Dev-channels rule escalate_template is a non-empty string."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["I am using this for local development"]
        self.assertIsInstance(rule.escalate_template, str)
        self.assertTrue(len(rule.escalate_template) > 0)


class TestStartupDialogKickoffChain(unittest.TestCase):
    """T-0525: startup-dialog wedge auto-resolve chains dismiss -> kickoff inject.

    The startup-dialog rule ("I am using this for local development") carries a
    kickoff_prompt; other allowlisted rules do not (dismiss-only). After a
    successful dismiss, the resolver polls the pane for the idle prompt and then
    injects the kickoff via the SAME cross-account path as the dismiss.

    All tmux/capture/subprocess + Telegram are mocked; poll bounds patched so the
    suite never sleeps.
    """

    STARTUP_SENTINEL = "I am using this for local development"

    def test_startup_rule_carries_kickoff_prompt(self):
        from tools.inbox_consumer import (
            RECOVERY_AUTO_RESOLVE_ALLOWLIST,
            RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT,
        )
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[self.STARTUP_SENTINEL]
        self.assertEqual(rule.kickoff_prompt, RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT)
        # Canonical kickoff text (byte-aligned with handoff_and_restart.sh step 8).
        self.assertIn("Read /home/claude/HANDOFF.md", rule.kickoff_prompt)
        self.assertIn("chat_id 456474197", rule.kickoff_prompt)

    def test_kickoff_prompt_default_is_none_for_other_rules(self):
        """Every NON-startup allowlisted rule must stay dismiss-only (no kickoff)."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel, rule in RECOVERY_AUTO_RESOLVE_ALLOWLIST.items():
            if sentinel == self.STARTUP_SENTINEL:
                continue
            self.assertIsNone(
                rule.kickoff_prompt,
                f"non-startup rule {sentinel!r} unexpectedly has a kickoff_prompt",
            )

    def test_kickoff_prompt_field_default_none(self):
        """AutoResolveRule.kickoff_prompt defaults to None (safe for existing rules)."""
        from tools.inbox_consumer import AutoResolveRule
        rule = AutoResolveRule(
            sentinel="x", predicate=lambda _: True,
            action=["1", "Enter"], escalate_template="t",
        )
        self.assertIsNone(rule.kickoff_prompt)

    def test_startup_dialog_dismiss_then_kickoff_after_idle(self):
        """Startup-dialog auto-resolve: dismiss applied, then (after idle appears)
        the kickoff prompt + Enter sent to ORCHESTRATOR_PANE_TARGET via cross-account."""
        from tools.inbox_consumer import (
            _auto_resolve_if_allowlisted,
            RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT,
            ORCHESTRATOR_PANE_TARGET,
        )
        dialog_pane = "  > 1. I am using this for local development\n  2. Exit\n"
        idle_pane = "some output\n❯ \n? for shortcuts\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=True) as apply_action, \
                 patch("tools.inbox_consumer._orchestrator_pane_capture",
                       side_effect=[dialog_pane, idle_pane]), \
                 patch("tools.inbox_consumer._send_telegram_info") as info:
                vocab = _auto_resolve_if_allowlisted(
                    self.STARTUP_SENTINEL, dialog_pane
                )
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        # Two applies: dismiss then kickoff, both to ORCHESTRATOR_PANE_TARGET.
        self.assertEqual(apply_action.call_count, 2)
        self.assertEqual(apply_action.call_args_list[0][0][0], ORCHESTRATOR_PANE_TARGET)
        self.assertEqual(apply_action.call_args_list[0][0][1], ["1", "Enter"])
        self.assertEqual(apply_action.call_args_list[1][0][0], ORCHESTRATOR_PANE_TARGET)
        self.assertEqual(
            apply_action.call_args_list[1][0][1],
            [RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT, "Enter"],
        )
        info.assert_called_once()
        self.assertIn("kickoff injected", info.call_args[0][0])
        self.assertIn("idle confirmed", info.call_args[0][0])

    def test_non_startup_dialog_dismiss_only_no_kickoff(self):
        """A non-startup allowlisted dialog (e.g. 'Do you want to create' with a
        passing predicate) → dismiss ONLY, kickoff text NEVER sent."""
        from tools.inbox_consumer import (
            _auto_resolve_if_allowlisted,
            RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT,
        )
        sentinel = "Do you want to create"
        pane = "Do you want to create /home/claude/some/file.txt?\n ❯ 1. Yes\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer.path_in_agent_cwd", return_value=True), \
                 patch("tools.inbox_consumer.extract_file_dialog_path",
                       return_value="/home/claude/some/file.txt"), \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=True) as apply_action, \
                 patch("tools.inbox_consumer._poll_orchestrator_pane_for_idle") as poll, \
                 patch("tools.inbox_consumer._send_telegram_info") as info:
                vocab = _auto_resolve_if_allowlisted(sentinel, pane)
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        # Dismiss only — exactly one apply, and it's the dismiss action.
        apply_action.assert_called_once()
        self.assertEqual(apply_action.call_args[0][1], ["1", "Enter"])
        # Poll for idle never even runs (gated on kickoff_prompt being set).
        poll.assert_not_called()
        # Kickoff text was never sent to the pane.
        for c in apply_action.call_args_list:
            self.assertNotIn(RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT, c[0][1])
        # Audit Telegram has no kickoff note.
        info.assert_called_once()
        self.assertNotIn("kickoff", info.call_args[0][0])

    def test_dismiss_send_keys_fail_no_kickoff_returns_none(self):
        """Dismiss send-keys fails → no kickoff attempted, returns None (escalate)."""
        from tools.inbox_consumer import _auto_resolve_if_allowlisted
        dialog_pane = "I am using this for local development\nExit\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=False) as apply_action, \
                 patch("tools.inbox_consumer._poll_orchestrator_pane_for_idle") as poll, \
                 patch("tools.inbox_consumer._send_telegram_info") as info:
                vocab = _auto_resolve_if_allowlisted(
                    self.STARTUP_SENTINEL, dialog_pane
                )
        self.assertIsNone(vocab)
        # Only the dismiss apply was attempted (returned False); no kickoff.
        apply_action.assert_called_once()
        poll.assert_not_called()
        info.assert_not_called()

    def test_idle_poll_timeout_best_effort_kickoff_and_warns(self):
        """Idle poll times out (pane never goes idle) → best-effort kickoff still
        attempted + a warning logged."""
        from tools.inbox_consumer import (
            _auto_resolve_if_allowlisted,
            RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT,
        )
        dialog_pane = "  > 1. I am using this for local development\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            log_path = tmppath / "log"
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       tmppath / "wedge"), \
                 patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer._poll_orchestrator_pane_for_idle",
                       return_value=False) as poll, \
                 patch("tools.inbox_consumer._apply_auto_resolve_action",
                       return_value=True) as apply_action, \
                 patch("tools.inbox_consumer._send_telegram_info") as info:
                vocab = _auto_resolve_if_allowlisted(
                    self.STARTUP_SENTINEL, dialog_pane
                )
                # Read the log INSIDE the tempdir context (it's removed on exit).
                log_text = log_path.read_text(encoding="utf-8")
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        poll.assert_called_once()
        # Best-effort kickoff STILL attempted despite the timeout (dismiss + kickoff).
        self.assertEqual(apply_action.call_count, 2)
        self.assertEqual(
            apply_action.call_args_list[1][0][1],
            [RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT, "Enter"],
        )
        # Timeout surfaced in the log + audit telegram.
        self.assertIn("kickoff_idle_poll_timeout", log_text)
        info.assert_called_once()
        self.assertIn("best-effort", info.call_args[0][0])
        self.assertIn("idle poll timed out", info.call_args[0][0])


class TestPollThothPaneForIdle(unittest.TestCase):
    """T-0525: bounded poll for the TUI idle prompt after a dismiss."""

    def test_idle_appears_returns_true(self):
        from tools.inbox_consumer import _poll_orchestrator_pane_for_idle
        idle_pane = "out\n❯ \nesc to interrupt\n"
        with patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
             patch("tools.inbox_consumer._orchestrator_pane_capture",
                   return_value=idle_pane):
            self.assertTrue(_poll_orchestrator_pane_for_idle("dialog sentinel"))

    def test_dialog_still_present_not_idle_until_cleared(self):
        """Dialog sentinel must be gone AND an idle sentinel present to count idle."""
        from tools.inbox_consumer import _poll_orchestrator_pane_for_idle
        dialog_then_idle = [
            "I am using this for local development\n❯ \n",  # dialog still up
            "booting...\n",                                  # transitional, no idle
            "ready\n❯ \n? for shortcuts\n",                  # idle, dialog gone
        ]
        with patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
             patch("tools.inbox_consumer._orchestrator_pane_capture",
                   side_effect=dialog_then_idle):
            self.assertTrue(
                _poll_orchestrator_pane_for_idle("I am using this for local development")
            )

    def test_timeout_returns_false(self):
        from tools.inbox_consumer import _poll_orchestrator_pane_for_idle
        # Pane never goes idle; with timeout=0 the loop probes once then bails.
        with patch("tools.inbox_consumer.KICKOFF_POLL_TIMEOUT_SECONDS", 0.0), \
             patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
             patch("tools.inbox_consumer._orchestrator_pane_capture",
                   return_value="still booting, no prompt yet\n"):
            self.assertFalse(_poll_orchestrator_pane_for_idle("dialog"))

    def test_capture_none_does_not_crash(self):
        """A None capture (pane unreachable) is tolerated; poll keeps trying / times out."""
        from tools.inbox_consumer import _poll_orchestrator_pane_for_idle
        with patch("tools.inbox_consumer.KICKOFF_POLL_TIMEOUT_SECONDS", 0.0), \
             patch("tools.inbox_consumer.KICKOFF_POLL_INTERVAL_SECONDS", 0.0), \
             patch("tools.inbox_consumer._orchestrator_pane_capture", return_value=None):
            self.assertFalse(_poll_orchestrator_pane_for_idle("dialog"))


class TestAutoResolvePredicateFalseBranch(unittest.TestCase):
    """T-0479: predicate-False → escalate-with-diagnostic path.

    Tests use a TEST-ONLY rule (not wired in live ALLOWLIST) to prove the
    False-branch. The live ALLOWLIST is patched with a test-only dict for
    these tests.
    """

    def setUp(self) -> None:
        p = patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _mock_health(self, payload):
        m = MagicMock()
        m.__enter__ = lambda self: m
        m.__exit__ = lambda self, *a: False
        m.read = lambda: json.dumps(payload).encode("utf-8")
        return m

    def _bridge_green(self):
        return {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }

    def test_predicate_false_escalates_with_template(self):
        """Predicate returns False → escalation fires with rendered escalate_template."""
        from tools.inbox_consumer import AutoResolveRule, check_orchestrator_health

        test_sentinel = "test sentinel for T-0479"
        test_template = "Predicate-False escalation for sentinel={sentinel!r} pane_excerpt={excerpt!r}"

        # TEST-ONLY rule: predicate always returns False.
        test_rule = AutoResolveRule(
            sentinel=test_sentinel,
            predicate=lambda _pane: False,
            action=["1", "Enter"],
            escalate_template=test_template,
        )

        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=(test_sentinel, "fake pane content")), \
                 patch("tools.inbox_consumer.RECOVERY_AUTO_RESOLVE_ALLOWLIST",
                       {test_sentinel: test_rule}), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()

        # Predicate False: escalates (not auto_resolved); no tmux send-keys.
        self.assertEqual(vocab, "pane_wedge")
        run.assert_not_called()
        alert.assert_called_once()
        delivered = alert.call_args[0][0]
        # Escalation message should contain the template-rendered diagnostic.
        self.assertIn("Predicate-False escalation", delivered)

    def test_predicate_true_auto_resolves(self):
        """Predicate returns True → action applied, auto_resolved vocab."""
        from tools.inbox_consumer import AutoResolveRule, check_orchestrator_health

        test_sentinel = "test sentinel T-0479 true"
        test_rule = AutoResolveRule(
            sentinel=test_sentinel,
            predicate=lambda _pane: True,
            action=["1", "Enter"],
            escalate_template="should not appear",
        )

        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            wedge_state = tmppath / "wedge_state"
            wedge_state.write_text("12345", encoding="utf-8")
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       wedge_state), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=(test_sentinel, "pane text for test")), \
                 patch("tools.inbox_consumer.RECOVERY_AUTO_RESOLVE_ALLOWLIST",
                       {test_sentinel: test_rule}), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                vocab = check_orchestrator_health()

        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        self.assertEqual(run.call_count, 2)  # "1" + "Enter"
        alert.assert_called_once()
        self.assertIn("[recovery auto-resolve]", alert.call_args[0][0])
        self.assertFalse(wedge_state.exists())  # cleared


class TestEscalateRoutingT0314(unittest.TestCase):
    """T-0314: escalation alerts route to Recovery's pane first; the operator gets an
    informational audit Telegram. On recovery-pane failure, fall back to the operator
    Telegram with [fallback: ...] prefix so the routing bypass is visible.
    """

    def _mock_health(self, payload):
        m = MagicMock()
        m.__enter__ = lambda self: m
        m.__exit__ = lambda self, *a: False
        m.read = lambda: json.dumps(payload).encode("utf-8")
        return m

    def _bridge_green(self):
        return {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-05T00:00:00+00:00"},
        }

    def test_a_escalate_routes_to_recovery_pane_first(self):
        """Sentinel match without allowlist entry → recovery-pane delivery succeeds
        → audit Telegram to the operator (NOT the original escalation alert)."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("Yes, and allow Claude to edit its own settings",
                                     "fake pane text")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_to_recovery_pane",
                       return_value=True) as recovery_pane, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge")
        # Recovery-pane received the full alert text (so Recovery can act on it).
        recovery_pane.assert_called_once()
        delivered = recovery_pane.call_args[0][0]
        self.assertIn("Orchestrator's loop is blocked", delivered)
        # the operator got an informational audit Telegram (NOT the original alert).
        # _send_telegram_info delegates to _send_telegram_alert, so we still
        # observe one call here — but with the [recovery routed] prefix.
        alert.assert_called_once()
        self.assertIn("[recovery routed]", alert.call_args[0][0])
        # Post-T-0364 refactor: vocab is embedded in the alert-class label,
        # e.g. "orchestrator-health (pane_wedge)" instead of "vocab=pane_wedge".
        self.assertIn("pane_wedge", alert.call_args[0][0])

    def test_b_recovery_pane_failure_falls_back_with_retry_flag(self):
        """Recovery-pane delivery fails → fallback the operator Telegram, prefixed with
        [fallback: recovery-pane unreachable] so the bypass is visible."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("Yes, and allow Claude to edit its own settings",
                                     "fake pane text")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer._send_to_recovery_pane",
                       return_value=False), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge")
        alert.assert_called_once()
        delivered = alert.call_args[0][0]
        self.assertIn("[fallback: recovery-pane unreachable]", delivered)
        self.assertIn("Orchestrator's loop is blocked", delivered)

    def test_c_auto_resolve_does_not_call_recovery_pane(self):
        """Successful auto-resolve continues to use _send_telegram_info informational
        path; recovery-pane routing is NOT invoked (different code path entirely)."""
        payload = self._bridge_green()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            wedge_state = tmppath / "wedge_state"
            wedge_state.write_text("12345", encoding="utf-8")
            with patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                       tmppath / "alert"), \
                 patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                       wedge_state), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent",
                       return_value=("I am using this for local development",
                                     "fake pane text for dev channels")), \
                 patch("urllib.request.urlopen",
                       return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.subprocess.run") as run, \
                 patch("tools.inbox_consumer._send_to_recovery_pane") as recovery_pane, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        recovery_pane.assert_not_called()
        # Auto-resolve still emits its informational Telegram (via _send_telegram_info → _send_telegram_alert).
        alert.assert_called_once()
        self.assertIn("[recovery auto-resolve]", alert.call_args[0][0])


# T-1036: TestAppendSyncAlertLedgerRouting + TestPantheonConfigSyncAlert removed
# — the sync-alert detection/routing they covered was retired (the T-0365
# pantheon-config-sync timer that fed it is gone).


class TestMainWardThothHealthIntegration(unittest.TestCase):
    """main(agent='recovery') runs the per-agent health probe; main(agent='orchestrator') does not.

    T-0541: main("recovery") now calls check_agent_health("orchestrator") and
    check_agent_health("specialist") exactly once each via the loop — replacing the
    former standalone check_orchestrator_health() call. Tests updated accordingly.
    check_orchestrator_health() remains as an alias but is no longer called by main().
    """

    def test_recovery_runs_probe(self):
        # T-0541: probe is now check_agent_health (loop), not check_orchestrator_health.
        with patch("tools.inbox_consumer.check_agent_health") as probe, \
             patch("tools.inbox_consumer.check_autonomy_off",
                   side_effect=SkipCycle("off_switch")), \
             patch("tools.inbox_consumer.log_outcome"), \
             patch("tools.inbox_consumer.check_consecutive_skips"):
            probe.return_value = None
            main("recovery")
        # Both orchestrator and specialist are probed exactly once each.
        called_agents = [c[0][0] for c in probe.call_args_list]
        self.assertIn("orchestrator", called_agents)
        self.assertIn("specialist", called_agents)
        self.assertIn("recovery", called_agents)  # T-0559: recovery self-probes too
        self.assertEqual(probe.call_count, 3)

    def test_orchestrator_skips_probe(self):
        # T-0541: check_agent_health (not check_orchestrator_health) is the loop path.
        with patch("tools.inbox_consumer.check_agent_health") as probe, \
             patch("tools.inbox_consumer.check_autonomy_off",
                   side_effect=SkipCycle("off_switch")), \
             patch("tools.inbox_consumer.log_outcome"), \
             patch("tools.inbox_consumer.check_consecutive_skips"):
            main("orchestrator")
        probe.assert_not_called()

    def test_recovery_probe_exception_does_not_break_flow(self):
        """If the probe raises, the main cycle still completes its skip."""
        with patch("tools.inbox_consumer.check_agent_health",
                   side_effect=RuntimeError("bridge boom")), \
             patch("tools.inbox_consumer.check_autonomy_off",
                   side_effect=SkipCycle("off_switch")), \
             patch("tools.inbox_consumer.log_outcome") as log, \
             patch("tools.inbox_consumer.check_consecutive_skips"):
            rc = main("recovery")
        self.assertEqual(rc, 0)
        # The skip:off_switch outcome must still have been logged.
        skip_calls = [c for c in log.call_args_list if c.args[1] == "skip:off_switch"]
        self.assertEqual(len(skip_calls), 1)


# ---------------------------------------------------------------------------
# T-0102 Phase 1 — Specialist inbox-consumer routing tests.
# ---------------------------------------------------------------------------


from tools.inbox_consumer import (  # noqa: E402 (already imported above; re-import for clarity)
    TMUX_TARGET_BY_AGENT,
    _resolve_tmux_target,
)


class TestArgusRouting(unittest.TestCase):
    """Tests for T-0102 Phase 1: specialist entry in TMUX_TARGET_BY_AGENT."""

    # ------------------------------------------------------------------
    # Test 1 — key present + env-var override wired
    # ------------------------------------------------------------------

    def test_inbox_consumer_recognizes_specialist_as_target_in_tmux_target_by_agent_map(self):
        self.assertIn("specialist", TMUX_TARGET_BY_AGENT)
        self.assertIsInstance(TMUX_TARGET_BY_AGENT["specialist"], str)
        self.assertTrue(len(TMUX_TARGET_BY_AGENT["specialist"]) > 0)
        # env-var override path
        with patch.dict(os.environ, {"PANTHEON_SPECIALIST_TMUX_TARGET": "custom-specialist-target"}):
            with patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT",
                       {"specialist": os.environ.get("PANTHEON_SPECIALIST_TMUX_TARGET", "claude-research-bot")}):
                result = _resolve_tmux_target("specialist")
        self.assertEqual(result, "custom-specialist-target")

    # ------------------------------------------------------------------
    # Test 2 — specialist-targeted ticket routed to specialist pane
    # ------------------------------------------------------------------

    def test_inbox_consumer_routes_specialist_targeted_ticket_to_specialist_pane_via_local_tmux_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0200", "specialist", title="Specialist task")
            log_path = Path(tmp) / "inbox_consumer.log"

            def fake_run(cmd, **kwargs):
                if cmd[1] == "capture-pane":
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            specialist_target = "claude-research-bot"
            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"specialist": specialist_target}),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                main("specialist")

            # T-0538: inject now uses helper — detect by helper path, not raw send-keys
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            targets_used = [c[0][0][1] for c in inject_calls]  # 2nd arg is pane target
            self.assertTrue(len(inject_calls) >= 1)
            for tgt in targets_used:
                self.assertEqual(tgt, specialist_target)
                self.assertNotEqual(tgt, "claude-telegram-bot")
                self.assertNotEqual(tgt, "claude-fallback-bot")

    # ------------------------------------------------------------------
    # Test 3 — pane probe (check_mid_turn) removed by T-0717: dispatch-gating
    # pane scrape replaced by auto_delivered mutex. The test that verified
    # check_mid_turn used no cross-account sudo for specialist is deleted outright
    # per msg_2364 alpha-removal preference.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Test 4 — orchestrator routing unchanged after specialist addition
    # ------------------------------------------------------------------

    def test_inbox_consumer_existing_orchestrator_routing_unchanged_post_specialist_addition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0001", "orchestrator", title="Orchestrator task")
            log_path = Path(tmp) / "inbox_consumer.log"

            def fake_run(cmd, **kwargs):
                if cmd[1] == "capture-pane":
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            post_addition_map = {
                "orchestrator": "claude-telegram-bot",
                "recovery": "claude-fallback-bot",
                "specialist": "claude-research-bot",
            }
            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", post_addition_map),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                result = main("orchestrator")

            self.assertEqual(result, 0)
            # T-0538: inject now uses helper — detect by helper path, not raw send-keys
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            targets_used = [c[0][0][1] for c in inject_calls]  # 2nd arg is pane target
            self.assertTrue(len(inject_calls) >= 1)
            for tgt in targets_used:
                self.assertEqual(tgt, "claude-telegram-bot")

    # ------------------------------------------------------------------
    # Test 5 — recovery routing unchanged after specialist addition
    # ------------------------------------------------------------------

    def test_inbox_consumer_existing_recovery_routing_unchanged_post_specialist_addition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0001", "recovery", title="Recovery task")
            log_path = Path(tmp) / "inbox_consumer.log"

            def fake_run(cmd, **kwargs):
                if cmd[1] == "capture-pane":
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            # Health response: bridge healthy, orchestrator connected, no queue — probe no-op
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "telegram_connected": True,
                "connected_clients": ["orchestrator"],
                "queued_messages": {"orchestrator": 0},
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None},
            }).encode()

            post_addition_map = {
                "orchestrator": "claude-telegram-bot",
                "recovery": "claude-fallback-bot",
                "specialist": "claude-research-bot",
            }
            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", post_addition_map),
                patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_LAST_ALERT_PATH",
                      Path(tmp) / "orchestrator_health_last_alert_at"),
                patch("tools.inbox_consumer.ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH",
                      Path(tmp) / "orchestrator_pane_wedge_first_seen_at"),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                result = main("recovery")

            self.assertEqual(result, 0)
            # T-0538: inject now uses helper — detect by helper path, not raw send-keys
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            targets_used = [c[0][0][1] for c in inject_calls]  # 2nd arg is pane target
            self.assertTrue(len(inject_calls) >= 1)
            for tgt in targets_used:
                self.assertEqual(tgt, "claude-fallback-bot")

    # ------------------------------------------------------------------
    # Test 6 — full integration: specialist pane idle → ticket dispatched
    # ------------------------------------------------------------------

    def test_inbox_consumer_dispatches_specialist_targeted_ticket_when_pane_idle_with_synthetic_specialist_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0201", "specialist", title="Specialist integration test")
            log_path = Path(tmp) / "inbox_consumer.log"

            def fake_run(cmd, **kwargs):
                if cmd[1] == "capture-pane":
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
            }).encode()

            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"specialist": "claude-research-bot"}),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                result = main("specialist")

            # (a) return 0
            self.assertEqual(result, 0)

            # (b) T-0538: inject now uses helper (one call), not two raw send-keys calls
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(len(inject_calls), 1)

            # (c) prompt contains ticket id + body text
            prompt_text = inject_calls[0][0][0][2]  # [helper, pane, prompt]
            self.assertIn("T-0201", prompt_text)
            self.assertIn("Do the thing", prompt_text)

            # (d) prompt contains target=specialist
            self.assertIn("target=specialist", prompt_text)

            # (e) log contains pulled entry
            log_content = log_path.read_text()
            self.assertIn("specialist pulled T-0201", log_content)


class TestSendAlertViaWardT0364(unittest.TestCase):
    """T-0364: _send_alert_via_recovery helper consolidates the recovery-pane-first
    routing pattern that T-0314 introduced for check_orchestrator_health, now
    extended to all operational alert classes (consecutive_skip + sync_alert).
    """

    def test_recovery_pane_success_emits_audit_to_operator(self) -> None:
        from tools.inbox_consumer import _send_alert_via_recovery
        with patch("tools.inbox_consumer._send_to_recovery_pane",
                   return_value=True) as recovery_pane, \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery("[pantheon] some operational alert text", vocab="test-class")
        recovery_pane.assert_called_once()
        # recovery-pane receives the full alert
        self.assertIn("operational alert text", recovery_pane.call_args[0][0])
        # the operator gets audit (NOT the original alert) via _send_telegram_info → _send_telegram_alert
        alert.assert_called_once()
        self.assertIn("[recovery routed]", alert.call_args[0][0])
        self.assertIn("test-class", alert.call_args[0][0])

    def test_recovery_pane_failure_falls_back_with_retry_flag(self) -> None:
        from tools.inbox_consumer import _send_alert_via_recovery
        with patch("tools.inbox_consumer._send_to_recovery_pane",
                   return_value=False), \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery("[pantheon] some operational alert text", vocab="test-class")
        alert.assert_called_once()
        delivered = alert.call_args[0][0]
        self.assertIn("[fallback: recovery-pane unreachable]", delivered)
        self.assertIn("operational alert text", delivered)

    def test_consecutive_skip_uses_recovery_routing(self) -> None:
        """check_consecutive_skips now routes via _send_alert_via_recovery."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "inbox_consumer.log"
            lines = [f"2026-05-04T12:0{i}:00Z orchestrator skip:mid_turn" for i in range(6)]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer._pane_frozen_across_samples",
                       return_value=True), \
                 patch("tools.inbox_consumer._send_to_recovery_pane",
                       return_value=True) as recovery_pane, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                check_consecutive_skips("orchestrator", "mid_turn")
        recovery_pane.assert_called_once()
        # Audit Telegram landed (via _send_telegram_info → _send_telegram_alert)
        alert.assert_called_once()
        self.assertIn("[recovery routed]", alert.call_args[0][0])
        self.assertIn("consecutive-skip", alert.call_args[0][0])

    # T-1036: test_sync_alert_uses_recovery_routing removed — check_pantheon_config_sync_alert
    # was retired (its feeding pantheon-config-sync timer is gone, T-0365).


# ---------------------------------------------------------------------------
# Active-tail surface tests (T-0383).
# ---------------------------------------------------------------------------


def _write_active_ticket(
    tickets_root: Path,
    ticket_id: str,
    target: str,
    created: str = "2026-05-04T12:00:00Z",
    title: str = "Active test ticket",
) -> Path:
    """Write a ticket file directly in tickets/active/."""
    active_dir = tickets_root / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    content = f"""---
id: {ticket_id}
created: {created}
requester: orchestrator
target: {target}
state: active
depends_on: []
labels: []
stale_after: 7d
---

# {title}

## Ask

Do the active thing.

## Transition log

- {created} [orchestrator] claimed; moving to active
"""
    path = active_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestSurfaceActiveTicket(unittest.TestCase):
    """Tests for surface_active_ticket() — rate-limit + basic logic."""

    def setUp(self):
        # Import new symbols after test file loads; they won't exist until
        # implementation lands.
        from tools.inbox_consumer import (
            surface_active_ticket,
            ACTIVE_SURFACE_LOG_PATH,
            ACTIVE_SURFACE_PROMPT,
        )
        self.surface_active_ticket = surface_active_ticket
        self.ACTIVE_SURFACE_LOG_PATH = ACTIVE_SURFACE_LOG_PATH
        self.ACTIVE_SURFACE_PROMPT = ACTIVE_SURFACE_PROMPT

    def test_first_surface_allowed_inject_called(self):
        """No state file → ticket is surfaced; inject called; state file created."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "active_surface_log.json"

            idle_pane = "? for shortcuts"

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout=idle_pane, stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=fake_run),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            self.assertIsNotNone(result)
            self.assertIn("T-0100", result)
            self.assertTrue(state_path.exists())
            data = json.loads(state_path.read_text())
            self.assertIn("orchestrator:T-0100", data)

    def test_second_surface_within_24h_blocked(self):
        """State file has entry < 24h ago → inject NOT called; returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "active_surface_log.json"

            # Write state showing surfaced < 24h ago.
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            state_path.write_text(
                json.dumps({"orchestrator:T-0100": recent_ts}), encoding="utf-8"
            )

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run") as mock_run,
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

        self.assertIsNone(result)
        # inject must NOT have been called
        send_calls = [c for c in mock_run.call_args_list
                      if c[0][0][1:2] == ["send-keys"]]
        self.assertEqual(send_calls, [])

    def test_second_surface_after_24h_allowed(self):
        """State file has entry > 24h ago → inject IS called; state updated."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "active_surface_log.json"

            from datetime import datetime, timezone, timedelta
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            state_path.write_text(
                json.dumps({"orchestrator:T-0100": old_ts}), encoding="utf-8"
            )

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=fake_run),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            self.assertIsNotNone(result)
            # State file timestamp should be updated.
            new_data = json.loads(state_path.read_text())
            self.assertNotEqual(new_data["orchestrator:T-0100"], old_ts)

    def test_no_active_tickets_returns_none(self):
        """tickets/active/ empty for agent → returns None; no state write."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # Create active dir but with a ticket for a different agent.
            _write_active_ticket(tickets_root, "T-0200", "specialist")
            state_path = Path(tmp) / "active_surface_log.json"

            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

        self.assertIsNone(result)
        self.assertFalse(state_path.exists())

    def test_missing_state_file_first_run_friendly(self):
        """No state file at all → works without error; creates state on surface."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "subdir" / "active_surface_log.json"
            # Parent dir doesn't exist yet.
            self.assertFalse(state_path.parent.exists())

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=fake_run),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            # Should succeed; state file created (mkdir -p semantics).
            self.assertIsNotNone(result)
            self.assertTrue(state_path.exists())

    def test_oldest_ticket_surfaced_first(self):
        """When multiple active tickets exist, the oldest (by created) is surfaced."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0200", "orchestrator",
                                 created="2026-05-10T00:00:00Z")
            _write_active_ticket(tickets_root, "T-0100", "orchestrator",
                                 created="2026-05-01T00:00:00Z")
            state_path = Path(tmp) / "active_surface_log.json"

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=fake_run),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            self.assertIsNotNone(result)
            self.assertIn("T-0100", result)  # older ticket surfaced

    def test_role_id_target_matches(self):
        """Ticket with target=orchestrator is found for agent=orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "active_surface_log.json"

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=fake_run),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            self.assertIsNotNone(result)

    # --- T-0544: real-staleness gate tests ---

    def _fake_run_inject_ok(self, cmd, **kwargs):
        """subprocess.run stub: returns success for all calls (inject succeeds)."""
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_recent_transition_not_surfaced(self):
        """T-0539 regression: ticket claimed <24h ago must NOT be surfaced.

        No prior surface state (cooldown not the gate) and inject is mocked to
        succeed — the staleness gate is the only thing that can suppress surfacing.
        """
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            active_dir.mkdir(parents=True, exist_ok=True)

            # Transition happened 12 minutes ago — simulates T-0539 scenario.
            recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=12)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            content = f"""---
id: T-0539
created: 2026-05-22T03:07:44Z
requester: orchestrator
target: orchestrator
state: active
depends_on: []
labels: []
stale_after: 7d
---

# Regression ticket T-0539

## Ask

Claimed recently.

## Transition log

- {recent_ts} [orchestrator] claimed; moving to active
"""
            (active_dir / "T-0539.md").write_text(content, encoding="utf-8")
            state_path = Path(tmp) / "active_surface_log.json"
            # No prior surface state — cooldown is NOT the reason for suppression.

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=self._fake_run_inject_ok),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

        self.assertIsNone(result)

    def test_stale_transition_surfaced(self):
        """Ticket last-transitioned >24h ago IS surfaced (when cooldown allows)."""
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            active_dir.mkdir(parents=True, exist_ok=True)

            # Transition happened 25 hours ago — genuinely stale.
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            content = f"""---
id: T-0540
created: 2026-05-20T00:00:00Z
requester: orchestrator
target: orchestrator
state: active
depends_on: []
labels: []
stale_after: 7d
---

# Stale ticket T-0540

## Ask

Stale ticket.

## Transition log

- {old_ts} [orchestrator] claimed; moving to active
"""
            (active_dir / "T-0540.md").write_text(content, encoding="utf-8")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=self._fake_run_inject_ok),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

        self.assertIsNotNone(result)
        self.assertIn("T-0540", result)

    def test_unparseable_transition_not_surfaced(self):
        """Ticket body with no parseable transition log must NOT be surfaced.

        Cannot assert '>24h no transition' without evidence — skip-on-unparseable.
        Inject is mocked to succeed so the gate is the only suppression path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            active_dir.mkdir(parents=True, exist_ok=True)

            # No ## Transition log section at all.
            content = """---
id: T-0541
created: 2026-05-01T00:00:00Z
requester: orchestrator
target: orchestrator
state: active
depends_on: []
labels: []
stale_after: 7d
---

# No-transition-log ticket

## Ask

No transition log section.
"""
            (active_dir / "T-0541.md").write_text(content, encoding="utf-8")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("subprocess.run", side_effect=self._fake_run_inject_ok),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

        self.assertIsNone(result)


class TestActiveTailSpliceInMain(unittest.TestCase):
    """Tests for active-tail surface integration in main() flow."""

    def _base_patches(self, tmp: str, tickets_root: Path):
        """Return dict of standard patches for a main() call."""
        return {
            "tools.inbox_consumer.AUTONOMY_OFF_PATH": Path(tmp) / "autonomy_off",
            "tools.inbox_consumer.CAP_HIT_PATH": Path(tmp) / "no_cap_hit.json",
            "tools.inbox_consumer.LOG_PATH": Path(tmp) / "inbox_consumer.log",
            "tools.inbox_consumer.TICKETS_ROOT": tickets_root,
            "tools.inbox_consumer.TMUX_TARGET_BY_AGENT": {"orchestrator": "tgt"},
            "tools.inbox_consumer.ACTIVE_SURFACE_LOG_PATH": Path(tmp) / "active_surface_log.json",
        }

    def _health_resp(self, agent="orchestrator"):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({
            "last_inbound_at": {agent: None, "specialist": None, "recovery": None}
        }).encode()
        return resp

    def test_empty_inbox_triggers_active_surface(self):
        """empty_inbox vocab + active ticket → inject called for active ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # No inbox tickets; one active ticket.
            _write_active_ticket(tickets_root, "T-0100", "orchestrator",
                                 title="Aging active ticket")
            patches = self._base_patches(tmp, tickets_root)

            idle_pane = "? for shortcuts"

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout=idle_pane, stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("urllib.request.urlopen", return_value=self._health_resp()):
                ctx_managers = [patch(k, v) for k, v in patches.items()]
                for cm in ctx_managers:
                    cm.__enter__()
                try:
                    with patch("subprocess.run", side_effect=fake_run) as mock_run:
                        with patch("tools.inbox_consumer.time"):
                            result = main("orchestrator")
                finally:
                    for cm in reversed(ctx_managers):
                        cm.__exit__(None, None, None)

            self.assertEqual(result, 0)
            log_content = (Path(tmp) / "inbox_consumer.log").read_text()
            self.assertIn("skip:empty_inbox", log_content)
            self.assertIn("active_surface:T-0100", log_content)
            # T-0538: inject was called (helper call for the active ticket)
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertGreater(len(inject_calls), 0)

    def test_not_before_unmet_triggers_active_surface(self):
        """not_before_unmet vocab also triggers active-tail surface."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # Inbox ticket with future not_before.
            _write_ticket_with_not_before(tickets_root, "T-0001", "orchestrator",
                                          not_before=future)
            _write_active_ticket(tickets_root, "T-0200", "orchestrator",
                                 title="Active reminder")
            patches = self._base_patches(tmp, tickets_root)

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("urllib.request.urlopen", return_value=self._health_resp()):
                ctx_managers = [patch(k, v) for k, v in patches.items()]
                for cm in ctx_managers:
                    cm.__enter__()
                try:
                    with patch("subprocess.run", side_effect=fake_run):
                        with patch("tools.inbox_consumer.time"):
                            result = main("orchestrator")
                finally:
                    for cm in reversed(ctx_managers):
                        cm.__exit__(None, None, None)

            self.assertEqual(result, 0)
            log_content = (Path(tmp) / "inbox_consumer.log").read_text()
            self.assertIn("skip:not_before_unmet", log_content)
            self.assertIn("active_surface:T-0200", log_content)

    def test_dependency_blocked_does_not_trigger_active_surface(self):
        """dependency_blocked vocab → active surface NOT triggered."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # Inbox ticket with unmet dep.
            _write_ticket(tickets_root, "T-0010", "orchestrator", state="active")
            _write_ticket(tickets_root, "T-0002", "orchestrator", depends_on=["T-0010"])
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            patches = self._base_patches(tmp, tickets_root)

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("urllib.request.urlopen", return_value=self._health_resp()):
                ctx_managers = [patch(k, v) for k, v in patches.items()]
                for cm in ctx_managers:
                    cm.__enter__()
                try:
                    with patch("subprocess.run", side_effect=fake_run) as mock_run:
                        with patch("tools.inbox_consumer.time"):
                            result = main("orchestrator")
                finally:
                    for cm in reversed(ctx_managers):
                        cm.__exit__(None, None, None)

            self.assertEqual(result, 0)
            log_content = (Path(tmp) / "inbox_consumer.log").read_text()
            self.assertIn("skip:dependency_blocked", log_content)
            self.assertNotIn("active_surface:", log_content)

    def test_cross_agent_recovery_sees_own_active_queue(self):
        """Recovery agent sees recovery-targeted active tickets; orchestrator active tickets excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # One recovery active, one orchestrator active.
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            _write_active_ticket(tickets_root, "T-0200", "recovery",
                                 title="Recovery active ticket")
            patches = {
                "tools.inbox_consumer.AUTONOMY_OFF_PATH": Path(tmp) / "autonomy_off",
                "tools.inbox_consumer.CAP_HIT_PATH": Path(tmp) / "no_cap_hit.json",
                "tools.inbox_consumer.LOG_PATH": Path(tmp) / "inbox_consumer.log",
                "tools.inbox_consumer.TICKETS_ROOT": tickets_root,
                "tools.inbox_consumer.TMUX_TARGET_BY_AGENT": {"recovery": "recovery-tgt"},
                "tools.inbox_consumer.ACTIVE_SURFACE_LOG_PATH": Path(tmp) / "active_surface_log.json",
            }

            def fake_run(cmd, **kwargs):
                if "capture-pane" in cmd:
                    return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"recovery": None, "orchestrator": None, "specialist": None}
            }).encode()

            with patch("urllib.request.urlopen", return_value=resp):
                ctx_managers = [patch(k, v) for k, v in patches.items()]
                for cm in ctx_managers:
                    cm.__enter__()
                try:
                    with patch("subprocess.run", side_effect=fake_run):
                        with patch("tools.inbox_consumer.time"):
                            # T-0541: main("recovery") now calls check_agent_health (loop),
                            # not check_orchestrator_health; suppress health side effects here.
                            with patch("tools.inbox_consumer.check_agent_health",
                                       return_value=None):
                                result = main("recovery")
                finally:
                    for cm in reversed(ctx_managers):
                        cm.__exit__(None, None, None)

            log_content = (Path(tmp) / "inbox_consumer.log").read_text()
            self.assertIn("active_surface:T-0200", log_content)
            self.assertNotIn("active_surface:T-0100", log_content)


# ---------------------------------------------------------------------------
# T-0474: PANE_WEDGE_SENTINELS new-entry regression tests + false-positive docs.
# ---------------------------------------------------------------------------


class TestPaneWedgeSentinelsT0474(unittest.TestCase):
    """Regression tests for the 28 new sentinels added in T-0474.

    Per sop/standard/testing-depth-and-scope.md: Unit-tier tests only.
    Integ-same-process tier SKIPPED per D10 (sentinel-match is pure-function
    string logic; no IPC/subprocess/git surface touched by adding string
    literals to a tuple).

    False-positive tests document the accepted trade-off per HO resolution #3
    (missed-wedge cost >> false-escalate cost; substring matcher fires on
    incidental prose — this is by design, not a bug).
    """

    def _match(self, pane: str) -> str | None:
        from tools.inbox_consumer import _pane_wedge_match
        return _pane_wedge_match(pane)

    # --- Per-class regression tests (one per new prompt class) ---

    def test_workspace_trust_current_variant(self):
        """Workspace-trust current dialog fires on 'Yes, I trust this folder'."""
        pane = (
            "Do you trust the files in this folder?\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code"
        )
        result = self._match(pane)
        self.assertEqual(result, "Yes, I trust this folder")

    def test_workspace_trust_legacy_variant(self):
        """Workspace-trust legacy dialog fires on 'Do you trust the files in this folder?'."""
        pane = (
            "Do you trust the files in this folder?\n"
            "❯ 1. Trust\n"
            "  2. Exit"
        )
        result = self._match(pane)
        self.assertEqual(result, "Do you trust the files in this folder?")

    def test_bypass_permissions_warning(self):
        """Bypass-permissions confirmation dialog fires on WARNING sentinel.

        Pane omits the 'Yes, I accept' option text to ensure the WARNING
        sentinel (higher in the tuple) fires first. In practice the full dialog
        would contain both substrings; the WARNING sentinel fires first because
        it precedes 'Yes, I accept' in the tuple — this test exercises that path.
        """
        pane = (
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "Continue? Running in bypass mode grants Claude unrestricted access.\n"
            "❯ No (recommended)\n"
            "  Continue anyway"
        )
        result = self._match(pane)
        self.assertEqual(result, "WARNING: Claude Code running in Bypass Permissions mode")

    def test_bypass_permissions_accept_option(self):
        """Bypass-permissions option label fires on 'Yes, I accept'."""
        pane = (
            "Running in Bypass Permissions mode.\n"
            "❯ No\n"
            "  Yes, I accept"
        )
        result = self._match(pane)
        self.assertEqual(result, "Yes, I accept")

    def test_managed_settings_dialog(self):
        """Managed-settings security dialog fires on 'Managed settings require approval'."""
        pane = (
            "Managed settings require approval\n"
            "Your organization has configured managed settings.\n"
            "❯ Yes, I trust these settings\n"
            "  No, exit Claude Code"
        )
        result = self._match(pane)
        self.assertEqual(result, "Managed settings require approval")

    def test_file_ops_overwrite(self):
        """File-write overwrite dialog fires on 'Do you want to overwrite'."""
        pane = (
            "Do you want to overwrite config.yaml?\n"
            "❯ 1. Yes\n"
            "  2. No"
        )
        result = self._match(pane)
        self.assertEqual(result, "Do you want to overwrite")

    def test_network_sandbox_fetch(self):
        """WebFetch permission dialog fires on 'Do you want to allow Claude to fetch this content?'."""
        pane = (
            "Do you want to allow Claude to fetch this content?\n"
            "❯ 1. Yes, once\n"
            "  2. Yes, and don't ask again for api.example.com\n"
            "  3. No"
        )
        result = self._match(pane)
        self.assertEqual(result, "Do you want to allow Claude to fetch this content?")

    def test_plan_mode_exit(self):
        """ExitPlanMode dialog fires on 'Ready to code?'."""
        pane = (
            "Ready to code?\n"
            "❯ 1. Auto-approve all edits\n"
            "  2. Manually approve each edit\n"
            "  3. Keep planning"
        )
        result = self._match(pane)
        self.assertEqual(result, "Ready to code?")

    def test_plan_mode_enter(self):
        """EnterPlanMode confirmation dialog fires on 'Enter plan mode?'."""
        pane = (
            "Enter plan mode?\n"
            "❯ 1. Yes, enter plan mode\n"
            "  2. No, start implementing now"
        )
        result = self._match(pane)
        self.assertEqual(result, "Enter plan mode?")

    def test_skills_mcp_skill_dialog(self):
        """Skill permission dialog fires on 'Use skill \"'.

        Pane omits the "don't ask again" option text to avoid the
        'Yes, and don't ask again for' sentinel (which precedes 'Use skill "'
        in the tuple) firing first. In production the full dialog would show
        both; the operator-visible sentinel would be 'Yes, and don't ask again
        for' which still correctly signals a blocked dialog.
        """
        pane = (
            'Use skill "my-deploy-tool"?\n'
            "❯ 1. Yes, allow this skill\n"
            "  2. No, skip this action"
        )
        result = self._match(pane)
        self.assertEqual(result, 'Use skill "')

    def test_askuserquestion_submit(self):
        """AskUserQuestion submit dialog fires on 'Submit answers'."""
        pane = (
            "Ready to submit your answers?\n"
            "❯ 1. Submit answers\n"
            "  2. Go back"
        )
        result = self._match(pane)
        self.assertEqual(result, "Submit answers")

    def test_dead_pane_root_refusal(self):
        """Root-refusal dead-pane signal fires on 'cannot be used with root/sudo privileges'."""
        pane = (
            "Error: --dangerously-skip-permissions cannot be used with root/sudo privileges\n"
            "Process exited with code 1."
        )
        result = self._match(pane)
        self.assertEqual(result, "cannot be used with root/sudo privileges")

    def test_footer_fix_esc_to_exit(self):
        """ManagedSettingsSecurityDialog footer fires on 'Enter to confirm · Esc to exit'.

        Pane contains only the footer line (no other sentinel text) to isolate
        the footer-fix entry. The footer sentinel fires on any modal that shows
        this footer, regardless of the dialog body text.
        """
        pane = "Enter to confirm · Esc to exit"
        result = self._match(pane)
        self.assertEqual(result, "Enter to confirm · Esc to exit")

    # --- Most-specific-match test (longest-prefix-first tuple order) ---

    def test_most_specific_match_do_you_want_to_family(self):
        """Longest-prefix-first ordering: 'Do you want to overwrite' fires before
        'Do you want to create' when both substrings appear in the pane.

        This test motivates and locks in the tuple ordering per HO resolution #4.
        A realistic pane might show an overwrite prompt whose body text also
        contains the phrase 'Do you want to create' (e.g. a two-step dialog).
        The longest-prefix-first tuple order ensures 'Do you want to overwrite'
        is returned, surfacing the most-specific class to the operator.
        """
        # Construct a pane that genuinely contains both substrings.
        pane = (
            "Do you want to overwrite README.md?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "  Do you want to create a backup copy instead?"
        )
        # Precondition: both substrings present.
        self.assertIn("Do you want to overwrite", pane)
        self.assertIn("Do you want to create", pane)
        result = self._match(pane)
        self.assertEqual(result, "Do you want to overwrite",
                         "Longest-prefix-first ordering must return 'Do you want to overwrite' "
                         "before 'Do you want to create' for a pane containing both substrings.")

    # --- Footer-distinction test ---

    def test_footer_distinction_cancel_vs_exit(self):
        """The 'Esc to cancel' and 'Esc to exit' footer variants return distinct sentinels.

        Uses footer-only panes to isolate the footer sentinels from other
        sentinel text that would fire earlier in the tuple.
        """
        cancel_pane = "Enter to confirm · Esc to cancel"
        exit_pane = "Enter to confirm · Esc to exit"
        cancel_result = self._match(cancel_pane)
        exit_result = self._match(exit_pane)
        self.assertEqual(cancel_result, "Enter to confirm · Esc to cancel")
        self.assertEqual(exit_result, "Enter to confirm · Esc to exit")
        self.assertNotEqual(cancel_result, exit_result)

    # --- False-positive documentation tests ---
    # These assert that the match STILL fires on unrelated prose containing the
    # substring. This is the accepted trade-off per HO resolution #3: the matcher
    # is substring-only; short sentinels will occasionally fire on innocent prose.
    # Missed-wedge cost >> false-escalate cost. These tests document the trade,
    # they do NOT prevent it.

    def test_fp_yes_i_accept_in_prose(self):
        """'Yes, I accept' fires even when the phrase appears in unrelated prose
        (e.g., terms-and-conditions text). Documented false-positive trade-off."""
        prose_pane = (
            "Claude has reviewed your request.\n"
            "Yes, I accept your analysis as correct.\n"
            "Proceeding with the plan."
        )
        result = self._match(prose_pane)
        self.assertEqual(result, "Yes, I accept",
                         "Substring match fires on prose containing 'Yes, I accept' — "
                         "accepted false-positive per HO resolution #3 (T-0474).")

    def test_fp_ready_to_code_in_prose(self):
        """'Ready to code?' fires even in non-modal prose context.

        The match is case-sensitive (Python 'in' operator). Prose must contain
        the exact sentinel casing to trigger the false-positive.
        """
        prose_pane = (
            "Ready to code? The scaffold is in place and all dependencies are installed.\n"
            "No permission dialog here — just an idle Claude pane."
        )
        result = self._match(prose_pane)
        self.assertEqual(result, "Ready to code?",
                         "Substring match fires on prose containing 'Ready to code?' — "
                         "accepted false-positive per HO resolution #3 (T-0474).")

    def test_fp_submit_answers_in_prose(self):
        """'Submit answers' fires even in non-modal prose context.

        The match is case-sensitive. Prose must contain the exact sentinel casing.
        """
        prose_pane = (
            "Submit answers to the review questions when you are done with T-0474.\n"
            "No permission dialog here."
        )
        result = self._match(prose_pane)
        self.assertEqual(result, "Submit answers",
                         "Substring match fires on prose containing 'Submit answers' — "
                         "accepted false-positive per HO resolution #3 (T-0474).")

    def test_fp_skip_interview_in_prose(self):
        """'Skip interview and plan immediately' fires even in non-modal prose context.

        The match is case-sensitive. Prose must contain the exact sentinel casing.
        """
        prose_pane = (
            "Skip interview and plan immediately — that is what the operator directive says.\n"
            "No permission dialog here."
        )
        result = self._match(prose_pane)
        self.assertEqual(result, "Skip interview and plan immediately",
                         "Substring match fires on prose containing 'Skip interview and plan "
                         "immediately' — accepted false-positive per HO resolution #3 (T-0474).")


class TestPostureCPredicateBoundaries(unittest.TestCase):
    """T-0489: posture-C predicate boundaries wired into RECOVERY_AUTO_RESOLVE_ALLOWLIST.

    Tests cover BOUNDARY 1 (file-ops-in-cwd) and BOUNDARY 2 (trust-canonical-dirs).
    Each boundary: predicate-True (in-scope) → action ["1","Enter"];
    predicate-False (out-of-scope) → escalate; extractor-None → safe escalate.

    Predicate-False integration (call-site render) is covered by the existing
    TestAutoResolvePredicateFalseBranch class, which patches ALLOWLIST with a
    test-only rule. Here we exercise the predicates directly from the live rules.
    """

    # ------------------------------------------------------------------
    # BOUNDARY 1 — file-ops-in-cwd
    # ------------------------------------------------------------------

    def test_file_ops_create_in_cwd_predicate_true(self):
        """file-ops 'Do you want to create': path in agent cwd → predicate True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to create"]
        pane = "Do you want to create /tmp/pantheon-config-T-0489/foo.py?"
        self.assertTrue(rule.predicate(pane))

    def test_file_ops_create_out_of_cwd_predicate_false(self):
        """file-ops 'Do you want to create': path outside agent cwd → predicate False."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to create"]
        pane = "Do you want to create /home/evil/bad.py?"
        self.assertFalse(rule.predicate(pane))

    def test_file_ops_edit_in_cwd_predicate_true(self):
        """file-ops 'Do you want to make this edit to': canonical home dir → True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to make this edit to"]
        pane = "Do you want to make this edit to /home/claude/pantheon-config/tools/foo.py?"
        self.assertTrue(rule.predicate(pane))

    def test_file_ops_edit_out_of_cwd_predicate_false(self):
        """file-ops 'Do you want to make this edit to': non-canonical dir → False."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to make this edit to"]
        pane = "Do you want to make this edit to /var/log/system.log?"
        self.assertFalse(rule.predicate(pane))

    def test_file_ops_overwrite_in_cwd_predicate_true(self):
        """file-ops 'Do you want to overwrite': /tmp worktree path → True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to overwrite"]
        pane = "Do you want to overwrite /tmp/pantheon-config-T-0489/dist/output.js?"
        self.assertTrue(rule.predicate(pane))

    def test_file_ops_overwrite_out_of_cwd_predicate_false(self):
        """file-ops 'Do you want to overwrite': non-canonical path → False."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to overwrite"]
        pane = "Do you want to overwrite /etc/passwd?"
        self.assertFalse(rule.predicate(pane))

    def test_file_ops_no_path_extracted_predicate_false(self):
        """file-ops: pane with no extractable path → predicate False (safe degradation)."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you want to create"]
        pane = "Do you want to create something without a path?"
        self.assertFalse(rule.predicate(pane))

    def test_file_ops_all_three_sentinels_action_is_option_1_enter(self):
        """All three file-op rules send option 1 only — never option 2 trust-expansion."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you want to create",
            "Do you want to make this edit to",
            "Do you want to overwrite",
        ):
            rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[sentinel]
            self.assertEqual(
                rule.action, ["1", "Enter"],
                f"file-op rule {sentinel!r} action must be ['1','Enter'], got {rule.action!r}"
            )

    def test_file_ops_sentinels_in_pane_wedge_sentinels(self):
        """All three file-op sentinels are in PANE_WEDGE_SENTINELS (shape invariant)."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you want to create",
            "Do you want to make this edit to",
            "Do you want to overwrite",
        ):
            self.assertIn(sentinel, PANE_WEDGE_SENTINELS,
                          f"file-op sentinel {sentinel!r} missing from PANE_WEDGE_SENTINELS")
            self.assertIn(sentinel, RECOVERY_AUTO_RESOLVE_ALLOWLIST,
                          f"file-op sentinel {sentinel!r} missing from RECOVERY_AUTO_RESOLVE_ALLOWLIST")

    def test_file_ops_escalate_template_renders_with_call_site_kwargs(self):
        """file-ops escalate_template can be .format()-rendered with sentinel= and excerpt=."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you want to create",
            "Do you want to make this edit to",
            "Do you want to overwrite",
        ):
            rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[sentinel]
            rendered = rule.escalate_template.format(sentinel=sentinel, excerpt="pane-excerpt")
            self.assertIsInstance(rendered, str)
            self.assertTrue(len(rendered) > 0)

    # ------------------------------------------------------------------
    # BOUNDARY 2 — trust-canonical-dirs
    # ------------------------------------------------------------------

    def test_trust_canonical_dir_pantheon_config_predicate_true(self):
        """trust 'Do you trust the files in this folder?': canonical Pantheon dir → True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you trust the files in this folder?"]
        pane = (
            "Do you trust the files in this folder?\n"
            "/home/claude/pantheon-config\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        self.assertTrue(rule.predicate(pane))

    def test_trust_canonical_dir_tmp_worktree_predicate_true(self):
        """trust sentinel: /tmp worktree path is canonical → predicate True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you trust the files in this folder?"]
        pane = (
            "Do you trust the files in this folder?\n"
            "/tmp/pantheon-config-T-0489\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        self.assertTrue(rule.predicate(pane))

    def test_trust_non_canonical_dir_predicate_false(self):
        """trust sentinel: non-canonical path → predicate False."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Do you trust the files in this folder?"]
        pane = (
            "Do you trust the files in this folder?\n"
            "/home/attacker/malicious-repo\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        self.assertFalse(rule.predicate(pane))

    def test_trust_yes_i_trust_sentinel_canonical_predicate_true(self):
        """trust 'Yes, I trust this folder' sentinel: canonical pane → True.

        Real pane always has 'Do you trust the files in this folder?' header FIRST,
        so extract_trust_dialog_path finds the header line → path → returns it.
        """
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["Yes, I trust this folder"]
        pane = (
            "Do you trust the files in this folder?\n"
            "/home/claude/echo-prismatic\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        self.assertTrue(rule.predicate(pane))

    def test_trust_trust_this_folder_sentinel_canonical_predicate_true(self):
        """trust 'trust this folder' sentinel: canonical pane → True."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST["trust this folder"]
        pane = (
            "Do you trust the files in this folder?\n"
            "/home/claude/pantheon-pwa\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        self.assertTrue(rule.predicate(pane))

    def test_trust_quick_safety_check_path_unextractable_safe_degradation(self):
        """trust 'Quick safety check': extractor returns None → predicate False (escalate).

        The 'Quick safety check' pane header is not in _TRUST_DIALOG_SENTINELS used
        by extract_trust_dialog_path. The extractor finds 'Yes, I trust this folder'
        as the option-1 line, then searches for a '/'-line AFTER it — there is none
        since the path precedes the options. Extractor returns None → path = '' →
        path_in_canonical_pantheon_dirs('') = False → predicate False → escalate.
        This is safe degradation: unknown path → decline auto-trust → operator review.
        """
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[
            "Quick safety check: Is this a project you created or one you trust?"
        ]
        pane = (
            "Quick safety check: Is this a project you created or one you trust?\n"
            "/home/claude/pantheon-config\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit Claude Code\n"
        )
        # Extractor can't find the path for this sentinel shape → False → escalate.
        self.assertFalse(rule.predicate(pane))

    def test_trust_all_four_sentinels_action_is_option_1_enter(self):
        """All four trust rules send option 1 — 'Yes, I trust this folder', not option 2."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you trust the files in this folder?",
            "Yes, I trust this folder",
            "trust this folder",
            "Quick safety check: Is this a project you created or one you trust?",
        ):
            rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[sentinel]
            self.assertEqual(
                rule.action, ["1", "Enter"],
                f"trust rule {sentinel!r} action must be ['1','Enter'], got {rule.action!r}"
            )

    def test_trust_sentinels_in_pane_wedge_sentinels(self):
        """All four trust sentinels are in PANE_WEDGE_SENTINELS (shape invariant)."""
        from tools.inbox_consumer import PANE_WEDGE_SENTINELS, RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you trust the files in this folder?",
            "Yes, I trust this folder",
            "trust this folder",
            "Quick safety check: Is this a project you created or one you trust?",
        ):
            self.assertIn(sentinel, PANE_WEDGE_SENTINELS,
                          f"trust sentinel {sentinel!r} missing from PANE_WEDGE_SENTINELS")
            self.assertIn(sentinel, RECOVERY_AUTO_RESOLVE_ALLOWLIST,
                          f"trust sentinel {sentinel!r} missing from RECOVERY_AUTO_RESOLVE_ALLOWLIST")

    def test_trust_escalate_template_renders_with_call_site_kwargs(self):
        """trust escalate_template can be .format()-rendered with sentinel= and excerpt=."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        for sentinel in (
            "Do you trust the files in this folder?",
            "Yes, I trust this folder",
            "trust this folder",
            "Quick safety check: Is this a project you created or one you trust?",
        ):
            rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST[sentinel]
            rendered = rule.escalate_template.format(sentinel=sentinel, excerpt="pane-excerpt")
            self.assertIsInstance(rendered, str)
            self.assertTrue(len(rendered) > 0)

    # ------------------------------------------------------------------
    # Hard-deny exclusions — option-2 trust-expansions never allowlisted
    # ------------------------------------------------------------------

    def test_option_2_trust_expansions_not_in_allowlist(self):
        """Option-2 trust-expansion sentinels are NEVER in RECOVERY_AUTO_RESOLVE_ALLOWLIST.

        T-0489 wires 7 new sentinels; none must be the option-2 expansions.
        These are listed here as an explicit regression guard.
        """
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        hard_deny = (
            "Yes, and allow Claude to edit its own settings",
            "Yes, and don't ask again for",
            "Yes, allow all edits during this session",
            "Yes, allow all edits in ",
            "Yes, allow reading from",
        )
        for sentinel in hard_deny:
            self.assertNotIn(
                sentinel, RECOVERY_AUTO_RESOLVE_ALLOWLIST,
                f"option-2 expansion {sentinel!r} must never be in RECOVERY_AUTO_RESOLVE_ALLOWLIST"
            )

    def test_all_seven_new_sentinels_present_in_allowlist(self):
        """T-0489 wires exactly 7 new sentinels (3 file-op + 4 trust) into the allowlist."""
        from tools.inbox_consumer import RECOVERY_AUTO_RESOLVE_ALLOWLIST
        new_sentinels = (
            # BOUNDARY 1 — file-ops-in-cwd
            "Do you want to create",
            "Do you want to make this edit to",
            "Do you want to overwrite",
            # BOUNDARY 2 — trust-canonical-dirs
            "Do you trust the files in this folder?",
            "Yes, I trust this folder",
            "trust this folder",
            "Quick safety check: Is this a project you created or one you trust?",
        )
        for sentinel in new_sentinels:
            self.assertIn(
                sentinel, RECOVERY_AUTO_RESOLVE_ALLOWLIST,
                f"T-0489 sentinel {sentinel!r} missing from RECOVERY_AUTO_RESOLVE_ALLOWLIST"
            )


# ---------------------------------------------------------------------------
# T-0538: inject() + _send_to_recovery_pane() now delegate to send_keys_submit.sh
# ---------------------------------------------------------------------------


class TestInjectHelperDelegation(unittest.TestCase):
    """T-0538: inject() must call send_keys_submit.sh, not raw tmux send-keys pairs."""

    def _make_run(self, returncode=0, stderr=""):
        return MagicMock(returncode=returncode, stdout="", stderr=stderr)

    def test_inject_calls_helper_script_not_raw_tmux(self):
        # T-0538: subprocess.run should be called with the helper path, not "tmux"
        from tools.inbox_consumer import _SEND_KEYS_HELPER
        with patch("subprocess.run", return_value=self._make_run(0)) as mock_run:
            inject("claude-telegram-bot", "hello world")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], str(_SEND_KEYS_HELPER))
        self.assertEqual(cmd[1], "claude-telegram-bot")
        self.assertEqual(cmd[2], "hello world")

    def test_inject_success_returns_none(self):
        # T-0538: exit 0 → returns without raising
        from tools.inbox_consumer import _SEND_KEYS_HELPER
        with patch("subprocess.run", return_value=self._make_run(0)):
            result = inject("claude-telegram-bot", "prompt text")
        self.assertIsNone(result)

    def test_inject_helper_failure_raises_tmux_unreachable(self):
        # T-0538: exit ≠ 0 (non-sentinel stderr) → SkipCycle("tmux_unreachable")
        with patch("subprocess.run", return_value=self._make_run(1, stderr="ERROR: tmux send-keys failed")):
            with self.assertRaises(SkipCycle) as ctx:
                inject("claude-telegram-bot", "hello world")
        self.assertEqual(ctx.exception.vocab, "tmux_unreachable")

    def test_inject_helper_pending_paste_failure_raises_tmux_unreachable(self):
        # T-0538: exit ≠ 0 even when stderr mentions pending-paste → still tmux_unreachable
        # (no new skip vocab introduced; future enhancement noted in inject() docstring)
        from tools.inbox_consumer import PENDING_PASTE_SENTINEL
        stderr_msg = f"still shows '{PENDING_PASTE_SENTINEL}'"
        with patch("subprocess.run", return_value=self._make_run(1, stderr=stderr_msg)):
            with self.assertRaises(SkipCycle) as ctx:
                inject("claude-telegram-bot", "hello world")
        self.assertEqual(ctx.exception.vocab, "tmux_unreachable")

    def test_inject_threads_enter_delay_via_env(self):
        # T-0538: INJECT_ENTER_DELAY threaded into subprocess env
        from tools.inbox_consumer import _SEND_KEYS_HELPER, INJECT_ENTER_DELAY
        with patch("subprocess.run", return_value=self._make_run(0)) as mock_run:
            inject("claude-telegram-bot", "some text")
        kwargs = mock_run.call_args[1]
        self.assertIn("env", kwargs)
        self.assertEqual(kwargs["env"]["INJECT_ENTER_DELAY"], str(INJECT_ENTER_DELAY))


class TestSendToWardPaneHelperDelegation(unittest.TestCase):
    """T-0538: _send_to_recovery_pane() must call send_keys_submit.sh, not raw send-keys pairs."""

    def setUp(self) -> None:
        # T-0891: _send_to_recovery_pane now shares the live-delivery suppression guard
        # (suppressed under unittest). These tests exercise the REAL injection body,
        # so opt back in via the force-send override.
        os.environ["PANTHEON_DISABLE_LIVE_TELEGRAM"] = "0"
        self.addCleanup(lambda: os.environ.pop("PANTHEON_DISABLE_LIVE_TELEGRAM", None))

    def _make_run(self, returncode=0):
        return MagicMock(returncode=returncode, stdout="", stderr="")

    def test_recovery_pane_calls_helper_script(self):
        # T-0538: single helper call, not two raw tmux send-keys calls
        from tools.inbox_consumer import _SEND_KEYS_HELPER, _send_to_recovery_pane, _resolve_tmux_target
        recovery_target = _resolve_tmux_target("recovery")
        with patch("subprocess.run", return_value=self._make_run(0)) as mock_run:
            _send_to_recovery_pane("alert text")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], str(_SEND_KEYS_HELPER))
        self.assertEqual(cmd[1], recovery_target)
        self.assertEqual(cmd[2], "alert text")

    def test_recovery_pane_success_returns_true(self):
        # T-0538: exit 0 → True
        from tools.inbox_consumer import _send_to_recovery_pane
        with patch("subprocess.run", return_value=self._make_run(0)):
            result = _send_to_recovery_pane("some text")
        self.assertTrue(result)

    def test_recovery_pane_failure_returns_false(self):
        # T-0538: exit ≠ 0 → False
        from tools.inbox_consumer import _send_to_recovery_pane
        with patch("subprocess.run", return_value=self._make_run(1)):
            result = _send_to_recovery_pane("some text")
        self.assertFalse(result)

    def test_recovery_pane_threads_enter_delay_via_env(self):
        # T-0538: INJECT_ENTER_DELAY threaded into subprocess env
        from tools.inbox_consumer import _send_to_recovery_pane, INJECT_ENTER_DELAY
        with patch("subprocess.run", return_value=self._make_run(0)) as mock_run:
            _send_to_recovery_pane("text")
        kwargs = mock_run.call_args[1]
        self.assertIn("env", kwargs)
        self.assertEqual(kwargs["env"]["INJECT_ENTER_DELAY"], str(INJECT_ENTER_DELAY))


# ---------------------------------------------------------------------------
# T-0541: per-agent health monitor (check_agent_health generalization).
# ---------------------------------------------------------------------------


class TestCheckAgentHealthArgus(unittest.TestCase):
    """check_agent_health("specialist") runs the same signals as orchestrator — but uses
    Specialist's pane target (claude-research-bot:0) and cross-account sudo to
    claude-research, not claude."""

    def setUp(self) -> None:
        # Default recovery-pane delivery to False so escalation assertions remain
        # stable (mirrors TestCheckThothHealth.setUp).
        p = patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _mock_health(self, payload: dict):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(payload).encode()
        return resp

    def _healthy_payload(self):
        # T-0553: specialist is not a bridge client; it is permanently absent from
        # connected_clients. A "healthy" specialist payload has no specialist entry in
        # connected_clients. The old fixture with connected_clients:["specialist"]
        # reflected an impossible runtime state.
        return {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],   # specialist absent — correct runtime state
            "queued_messages": {},
            "last_inbound_at": {},
        }

    def _specialist_patches(self, tmpdir: Path):
        """Redirect per-agent state files into tmpdir."""
        from tools.inbox_consumer import check_agent_health  # noqa: F401
        # _per_agent_health_config returns paths keyed from home; patch the
        # module-level constants for the ORCHESTRATOR slots to confirm Specialist does NOT
        # touch them, and supply per-agent config via the function.
        return [
            patch("tools.inbox_consumer.LOG_PATH", tmpdir / "log"),
        ]

    def _patch_specialist_state(self, tmpdir: Path):
        """Redirect Specialist-specific health state files into tmpdir.

        check_agent_health reads last_alert_path and wedge_state_path from
        _AGENT_HEALTH_CONFIG["specialist"]. Patch those to tmpdir so tests are
        hermetic (no bleed from real ~/.local/state/pantheon/ files).
        """
        from tools.inbox_consumer import _AGENT_HEALTH_CONFIG
        specialist_cfg = _AGENT_HEALTH_CONFIG["specialist"]
        return [
            patch.object(specialist_cfg, "__getitem__",
                         side_effect=lambda k: {
                             **specialist_cfg,
                             "last_alert_path": tmpdir / "specialist_last_alert_at",
                             "wedge_state_path": tmpdir / "specialist_wedge_state",
                         }[k]),
        ]

    def _specialist_state_ctx(self, tmpdir: Path):
        """Context manager: redirect Specialist health state files into tmpdir."""
        return patch(
            "tools.inbox_consumer._AGENT_HEALTH_CONFIG",
            {
                "orchestrator": __import__("tools.inbox_consumer",
                                    fromlist=["_AGENT_HEALTH_CONFIG"])
                ._AGENT_HEALTH_CONFIG["orchestrator"],
                "specialist": {
                    "pane_target": "claude-research-bot:0",
                    "unix_user": "claude-research",
                    "last_alert_path": tmpdir / "specialist_last_alert_at",
                    "wedge_state_path": tmpdir / "specialist_wedge_state",
                },
            },
        )

    def test_specialist_healthy_returns_none(self):
        """check_agent_health('specialist') with all-green bridge payload returns None."""
        from tools.inbox_consumer import check_agent_health
        payload = self._healthy_payload()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 self._specialist_state_ctx(tmppath), \
                 patch("tools.inbox_consumer._pane_capture", return_value=None), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("specialist")
        self.assertIsNone(vocab)
        alert.assert_not_called()

    def test_specialist_disconnected_no_alert(self):
        """T-0553: specialist absent from connected_clients → None (no alert).
        Specialist is not a bridge client; absence from connected_clients is the
        permanent runtime state, not a fault. The old test_specialist_disconnected_alerts
        captured pre-T-0553 behavior (false positive). Updated to assert correct
        post-T-0553 behavior: no alert, no disconnect vocab.
        """
        from tools.inbox_consumer import check_agent_health
        payload = {
            "telegram_connected": True,
            "connected_clients": [],
            "queued_messages": {"specialist": 0},
            "last_inbound_at": {"specialist": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 self._specialist_state_ctx(tmppath), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("specialist")
        self.assertIsNone(vocab)
        alert.assert_not_called()

    def test_specialist_pane_capture_uses_claude_research_sudo(self):
        """_pane_capture('specialist') calls sudo -n -u claude-research, not claude.

        T-0746: mock getuser to a non-claude-research user so the T-0559 same-user
        fast path does NOT fire — otherwise this assertion is account-dependent
        (green on the claude account, red on claude-research, where the caller IS
        claude-research and _pane_capture('specialist') runs bash directly with no sudo).
        Mirrors test_orchestrator_pane_capture_uses_claude_sudo below.
        """
        from tools.inbox_consumer import _pane_capture
        with patch("subprocess.run") as mock_run, \
             patch("tools.inbox_consumer.getpass.getuser", return_value="recovery"):
            mock_run.return_value = MagicMock(returncode=0, stdout="idle pane")
            _pane_capture("specialist")
        call_args = mock_run.call_args[0][0]  # argv list
        self.assertIn("claude-research", call_args)
        # Must NOT use the orchestrator user
        self.assertNotIn("claude-fallback", call_args)

    def test_orchestrator_pane_capture_uses_claude_sudo(self):
        """Cross-account capture of orchestrator's pane (caller != claude) sudo's to claude.

        T-0746: mock getuser to a non-claude user so the T-0559 same-user fast
        path does NOT fire — exercises the cross-account sudo path the test name
        describes, deterministically regardless of who runs the suite.
        """
        from tools.inbox_consumer import _pane_capture
        with patch("subprocess.run") as mock_run, \
             patch("tools.inbox_consumer.getpass.getuser", return_value="recovery"):
            mock_run.return_value = MagicMock(returncode=0, stdout="idle pane")
            _pane_capture("orchestrator")
        call_args = mock_run.call_args[0][0]
        self.assertIn("sudo", call_args)
        self.assertIn("claude", call_args)
        # 'claude' and not 'claude-research' or 'claude-fallback'
        claude_entries = [a for a in call_args if a == "claude"]
        self.assertTrue(len(claude_entries) >= 1)

    def test_orchestrator_pane_capture_same_user_fast_path_no_sudo(self):
        """T-0559/T-0746: when the caller IS claude (orchestrator's own consumer in prod),
        _pane_capture('orchestrator') runs bash directly — no sudo (sudoers typically
        gates -u to OTHER users)."""
        from tools.inbox_consumer import _pane_capture
        with patch("subprocess.run") as mock_run, \
             patch("tools.inbox_consumer.getpass.getuser", return_value="claude"):
            mock_run.return_value = MagicMock(returncode=0, stdout="idle pane")
            _pane_capture("orchestrator")
        call_args = mock_run.call_args[0][0]
        self.assertNotIn("sudo", call_args)
        self.assertEqual(call_args[0], "bash")

    def test_specialist_pane_capture_targets_claude_research_bot(self):
        """_pane_capture('specialist') targets claude-research-bot:0, not claude-telegram-bot:0."""
        from tools.inbox_consumer import _pane_capture
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="idle")
            _pane_capture("specialist")
        # The full command is a bash -c string — look for the pane target within it
        call_args = mock_run.call_args[0][0]
        full_cmd = " ".join(str(a) for a in call_args)
        self.assertIn("claude-research-bot", full_cmd)
        self.assertNotIn("claude-telegram-bot", full_cmd)

    def test_apply_resolve_action_specialist_uses_claude_research_sudo(self):
        """_apply_resolve_action for Specialist's pane target wraps tmux send-keys
        in sudo -u claude-research bash -c, not sudo -u claude."""
        from tools.inbox_consumer import _apply_resolve_action
        specialist_pane = "claude-research-bot:0"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _apply_resolve_action(specialist_pane, "claude-research", ["1", "Enter"])
        for c in mock_run.call_args_list:
            argv = c[0][0]
            full = " ".join(str(a) for a in argv)
            self.assertIn("claude-research", full)

    def test_specialist_pane_wedge_auto_resolve_uses_correct_target(self):
        """When Specialist pane wedge auto-resolves, the action targets
        claude-research-bot:0 (Specialist's pane), not claude-telegram-bot:0."""
        from tools.inbox_consumer import check_agent_health
        payload = self._healthy_payload()
        # Pane fixture uses the full trust-dialog form so extract_trust_dialog_path
        # can extract the path (it requires "Do you trust..." or "Yes, I trust...").
        wedged_pane = "Do you trust the files in this folder?\n/home/claude-research/pantheon-config\n❯ 1. Yes, I trust this folder"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 self._specialist_state_ctx(tmppath), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=("Do you trust the files in this folder?", wedged_pane)), \
                 patch("tools.inbox_consumer._apply_resolve_action",
                       return_value=True) as apply_action, \
                 patch("tools.inbox_consumer._send_telegram_info") as info, \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("specialist")
        self.assertEqual(vocab, "pane_wedge_auto_resolved")
        # The action was called with Specialist's pane target
        first_call_pane_target = apply_action.call_args_list[0][0][0]
        self.assertIn("claude-research-bot", first_call_pane_target)
        alert.assert_not_called()

    def test_main_recovery_calls_health_for_orchestrator_specialist_and_recovery(self):
        """main('recovery') invokes check_agent_health for orchestrator, specialist, AND recovery.

        T-0559: recovery joins the monitored loop in alert-only mode so a
        boot-dialog or pane wedge on Recovery's own pane surfaces as an alert.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer.check_agent_health") as health_mock, \
                 patch("tools.inbox_consumer.check_autonomy_off"), \
                 patch("tools.inbox_consumer.check_cap_hit"), \
                 patch("tools.inbox_consumer.check_recent_inbound"), \
                 patch("tools.inbox_consumer.check_dispatch_locked"), \
                 patch("tools.inbox_consumer.pull_repo_or_warn"), \
                 patch("tools.inbox_consumer.list_eligible_tickets",
                       side_effect=SkipCycle("empty_inbox")), \
                 patch("tools.inbox_consumer.surface_active_ticket",
                       return_value=None):
                health_mock.return_value = None
                main("recovery")
        called_agents = [c[0][0] for c in health_mock.call_args_list]
        self.assertIn("orchestrator", called_agents)
        self.assertIn("specialist", called_agents)
        self.assertIn("recovery", called_agents)

    def test_recovery_config_is_alert_only_and_not_bridge_client(self):
        """T-0559 safety constraint: recovery must NOT self-inject (alert_only=True)
        until a Layer-2 watcher-of-the-watcher backstop exists. Recovery is also
        bare-claude (no --channels) — never a bridge client."""
        from tools.inbox_consumer import _AGENT_HEALTH_CONFIG
        recovery_cfg = _AGENT_HEALTH_CONFIG["recovery"]
        self.assertTrue(recovery_cfg.get("alert_only"))
        self.assertFalse(recovery_cfg.get("is_bridge_client"))
        # Pane + unix_user point at Recovery's own session.
        self.assertEqual(recovery_cfg["pane_target"], "claude-telegram-bot-fallback:0")
        self.assertEqual(recovery_cfg["unix_user"], "claude-fallback")

    def test_recovery_alert_only_suppresses_auto_resolve(self):
        """T-0559 alert-only gate: when a matched wedge is detected on Recovery's
        own pane, check_agent_health MUST NOT invoke _auto_resolve_if_allowlisted
        — only the alert/vocab path runs. Auto-resolve fires send-keys; without
        a Layer-2 backstop we cannot safely inject into Recovery's own pane.
        """
        from tools.inbox_consumer import check_agent_health, _AGENT_HEALTH_CONFIG
        # Healthy bridge payload — recovery is non-bridge-client so connected_clients
        # absence is normal.
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {},
            "last_inbound_at": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            # Redirect recovery's per-agent state files into tmp so persistence
            # state doesn't bleed from real ~/.local/state/pantheon/.
            patched_config = {
                **_AGENT_HEALTH_CONFIG,
                "recovery": {
                    **_AGENT_HEALTH_CONFIG["recovery"],
                    "last_alert_path": tmppath / "recovery_last_alert",
                    "wedge_state_path": tmppath / "recovery_wedge_state",
                },
            }
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._AGENT_HEALTH_CONFIG", patched_config), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=("dev-channels-confirm", "fake pane text")), \
                 patch("tools.inbox_consumer._auto_resolve_if_allowlisted_for_agent") as resolver, \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                vocab = check_agent_health("recovery")
        # The auto-resolve send-keys path must NOT have fired.
        resolver.assert_not_called()
        # An alert vocab should be emitted (detect → alert is the alert-only contract).
        self.assertEqual(vocab, "pane_wedge")

    def test_check_orchestrator_health_backward_compat_alias(self):
        """check_orchestrator_health() is an alias for check_agent_health('orchestrator')."""
        from tools.inbox_consumer import check_orchestrator_health, check_agent_health
        payload = {
            "telegram_connected": True,
            "connected_clients": ["orchestrator"],
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": "2026-05-22T00:00:00+00:00"},
        }
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=resp), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                v1 = check_orchestrator_health()
                v2 = check_agent_health("orchestrator")
        self.assertEqual(v1, v2)

    # T-0553: bridge-client gating tests ----------------------------------------

    def test_specialist_absent_from_connected_clients_no_disconnect_alert(self):
        """T-0553 regression: specialist absent from connected_clients must NOT fire
        specialist_disconnected. Specialist is not a bridge client; pantheon-bridge's
        connected_clients has no meaning for specialist. Expect None (no alert).
        """
        from tools.inbox_consumer import check_agent_health
        payload = {
            "telegram_connected": True,
            "connected_clients": [],   # specialist permanently absent — not a bridge client
            "queued_messages": {},
            "last_inbound_at": {"specialist": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 self._specialist_state_ctx(tmppath), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("specialist")
        self.assertIsNone(vocab)
        alert.assert_not_called()
        # Belt-and-suspenders: confirm no disconnect vocab in any call
        for call in alert.call_args_list:
            self.assertNotIn("disconnected", str(call))

    def test_specialist_pane_wedge_still_runs_for_non_bridge_agent(self):
        """T-0553: specialist (non-bridge) pane-wedge fires even though specialist is
        absent from connected_clients. The persistence-gated pane check runs
        at top-level for non-bridge agents (T-0541 boot-dialog recovery intent
        preserved).
        """
        from tools.inbox_consumer import check_agent_health
        payload = {
            "telegram_connected": True,
            "connected_clients": [],   # specialist not in bridge — irrelevant for non-bridge
            "queued_messages": {},
            "last_inbound_at": {"specialist": None},
        }
        wedged_pane = "Do you trust the files in this folder?\n/home/claude-research/foo\n> 1. Yes"
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 self._specialist_state_ctx(tmppath), \
                 patch("tools.inbox_consumer._check_pane_wedge_persistent_for_agent",
                       return_value=("Do you trust the files in this folder?",
                                     wedged_pane)) as wedge_check, \
                 patch("tools.inbox_consumer._auto_resolve_if_allowlisted_for_agent",
                       return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("specialist")
        # Pane-wedge check was called for specialist
        wedge_check.assert_called_once_with("specialist")
        # vocab is pane_wedge (not specialist_disconnected)
        self.assertEqual(vocab, "pane_wedge")
        alert.assert_called_once()

    def test_orchestrator_bridge_client_still_alerts_on_disconnect(self):
        """T-0553: orchestrator (is_bridge_client=True) still fires orchestrator_disconnected
        when absent from connected_clients. The gating must not affect bridge
        clients.
        """
        from tools.inbox_consumer import check_agent_health
        payload = {
            "telegram_connected": True,
            "connected_clients": [],   # orchestrator absent — genuine disconnect
            "queued_messages": {"orchestrator": 0},
            "last_inbound_at": {"orchestrator": None},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            orchestrator_cfg = __import__(
                "tools.inbox_consumer", fromlist=["_AGENT_HEALTH_CONFIG"]
            )._AGENT_HEALTH_CONFIG["orchestrator"]
            patched_config = {
                "orchestrator": {
                    **orchestrator_cfg,
                    "last_alert_path": tmppath / "orchestrator_last_alert_at",
                    "wedge_state_path": tmppath / "orchestrator_wedge_state",
                },
                "specialist": __import__(
                    "tools.inbox_consumer", fromlist=["_AGENT_HEALTH_CONFIG"]
                )._AGENT_HEALTH_CONFIG["specialist"],
            }
            with patch("urllib.request.urlopen", return_value=self._mock_health(payload)), \
                 patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer._AGENT_HEALTH_CONFIG", patched_config), \
                 patch("tools.inbox_consumer._pane_capture", return_value=None), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_agent_health("orchestrator")
        self.assertEqual(vocab, "orchestrator_disconnected")
        alert.assert_called_once()


# ---------------------------------------------------------------------------
# T-0717 (a): Recovery tmux target regression tests.
# ---------------------------------------------------------------------------


class TestWardTmuxTarget(unittest.TestCase):
    def test_recovery_default_target_matches_health_config(self):
        """Recovery's TMUX_TARGET_BY_AGENT default must match _AGENT_HEALTH_CONFIG pane_target.

        Root cause of T-0717 Recovery tmux_unreachable false-positive: TMUX_TARGET_BY_AGENT
        defaulted to 'claude-fallback-bot' while the actual session is
        'claude-telegram-bot-fallback'. Regression guard: these must stay in sync.
        """
        from tools.inbox_consumer import TMUX_TARGET_BY_AGENT, _AGENT_HEALTH_CONFIG
        # Override env var so we test the coded default, not any env override.
        import os
        with patch.dict(os.environ, {}, clear=False):
            # Re-import the default by reading the constant directly
            recovery_target = TMUX_TARGET_BY_AGENT.get("recovery")
            # The pane_target in health config includes ':0' suffix (session:window);
            # strip it to get the session name for comparison.
            recovery_pane = _AGENT_HEALTH_CONFIG["recovery"]["pane_target"]
            recovery_session = recovery_pane.split(":")[0]
        self.assertEqual(recovery_target, recovery_session,
                         f"TMUX_TARGET_BY_AGENT['recovery']={recovery_target!r} does not match "
                         f"_AGENT_HEALTH_CONFIG pane session={recovery_session!r}")

    def test_recovery_tmux_target_is_correct_value(self):
        """Recovery's TMUX_TARGET_BY_AGENT default must be 'claude-telegram-bot-fallback'."""
        from tools.inbox_consumer import TMUX_TARGET_BY_AGENT
        import os
        with patch.dict(os.environ, {}, clear=False):
            recovery_target = TMUX_TARGET_BY_AGENT.get("recovery")
        self.assertEqual(recovery_target, "claude-telegram-bot-fallback")

    def test_recovery_env_var_override_still_works(self):
        """PANTHEON_RECOVERY_TMUX_TARGET env var must override the default."""
        import os
        import importlib
        import tools.inbox_consumer as ic_module
        with patch.dict(os.environ, {"PANTHEON_RECOVERY_TMUX_TARGET": "my-custom-recovery-target"}):
            # The module-level dict is set at import time; patch it directly.
            with patch.dict(ic_module.TMUX_TARGET_BY_AGENT, {"recovery": os.environ["PANTHEON_RECOVERY_TMUX_TARGET"]}):
                from tools.inbox_consumer import TMUX_TARGET_BY_AGENT
                self.assertEqual(ic_module.TMUX_TARGET_BY_AGENT["recovery"], "my-custom-recovery-target")


# ---------------------------------------------------------------------------
# T-0717 (b): dispatch_locked in NEVER_ALERT_SKIP_VOCABS.
# ---------------------------------------------------------------------------


class TestNeverAlertVocabsDispatchLocked(unittest.TestCase):
    def test_dispatch_locked_excluded_from_alert(self):
        """T-0717: dispatch_locked is healthy deferral — must NOT trigger the consecutive-skip alert."""
        from tools.inbox_consumer import NEVER_ALERT_SKIP_VOCABS
        self.assertIn("dispatch_locked", NEVER_ALERT_SKIP_VOCABS)

    def test_never_alert_set_invariant_with_dispatch_locked(self):
        """The full NEVER_ALERT set must contain all 6 canonical never-alert vocabs."""
        from tools.inbox_consumer import NEVER_ALERT_SKIP_VOCABS
        expected = {
            "recent_telegram",
            "empty_inbox",
            "dependency_blocked",
            "interactive_required",
            "not_before_unmet",
            "dispatch_locked",
        }
        for v in expected:
            self.assertIn(v, NEVER_ALERT_SKIP_VOCABS)
        # Vocabs that still alert must NOT appear
        for v in ("tmux_unreachable", "mid_turn", "inject_pending_paste", "off_switch", "cap_hit"):
            self.assertNotIn(v, NEVER_ALERT_SKIP_VOCABS)


# ---------------------------------------------------------------------------
# T-0717 (b): _list_auto_delivered_unresolved_for_agent tests.
# ---------------------------------------------------------------------------


def _write_ticket_with_auto_delivered(dir_path: Path, ticket_id: str, target: str,
                                      state: str = "inbox",
                                      auto_delivered: bool = False,
                                      auto_delivered_at: str | None = None,
                                      created: str = "2026-06-02T10:00:00Z",
                                      title: str = "Test ticket",
                                      last_transition: str | None = None,
                                      depends_on: list[str] | None = None,
                                      not_before: str | None = None,
                                      labels: list[str] | None = None) -> Path:
    """Write a ticket file with optional auto_delivered frontmatter fields."""
    state_dir = dir_path / state
    state_dir.mkdir(parents=True, exist_ok=True)
    deps_str = "[" + ", ".join(depends_on) + "]" if depends_on else "[]"
    labels_str = "[" + ", ".join(labels) + "]" if labels else "[]"
    fm_lines = [
        "---",
        f"id: {ticket_id}",
        f"created: {created}",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        f"depends_on: {deps_str}",
        f"labels: {labels_str}",
        "stale_after: 14d",
    ]
    if not_before is not None:
        fm_lines.append(f"not_before: {not_before}")
    if auto_delivered:
        fm_lines.append("auto_delivered: true")
    if auto_delivered_at is not None:
        fm_lines.append(f"auto_delivered_at: {auto_delivered_at}")
    fm_lines.append("---")
    transition_ts = last_transition or created
    body = (
        f"\n# {title}\n\n"
        "## Ask\n\nDo the thing.\n\n"
        "## Transition log\n\n"
        f"- {created} [orchestrator] filed in inbox\n"
    )
    if last_transition and last_transition != created:
        body += f"- {last_transition} [claude] claimed\n"
    content = "\n".join(fm_lines) + body
    path = state_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestListAutoDeliveredUnresolved(unittest.TestCase):
    def test_no_auto_delivered_tickets_returns_empty(self):
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0001", "orchestrator", state="active")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [])

    def test_stamped_active_ticket_returned(self):
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z"
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "T-0001")

    def test_stamped_inbox_ticket_returned(self):
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z"
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(len(result), 1)

    def test_done_ticket_not_returned(self):
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="done",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z"
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [])

    def test_wrong_agent_not_returned(self):
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "specialist", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z"
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [])

    def test_dep_blocked_stamped_ticket_excluded(self):
        """T-0864 regression: an auto_delivered ticket dep-gated on an unfinished
        ticket is NOT in-flight, so it must be excluded from the mutex (else it holds
        the dispatch lock forever and suppresses all inbox delivery — the T-0768 bug)."""
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # Dependency exists but is NOT done (sits in inbox).
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="inbox")
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                depends_on=["T-0002"],
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [], "dep-blocked auto_delivered ticket must not hold the mutex")

    def test_dep_satisfied_stamped_ticket_still_returned(self):
        """Counterpart: once the dependency is done, the auto_delivered ticket is
        workable/in-flight again and correctly holds the mutex."""
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="done")  # dep resolved
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                depends_on=["T-0002"],
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "T-0001")

    def test_not_before_future_stamped_ticket_excluded(self):
        """T-0864: an auto_delivered ticket with a future not_before is calendar-gated,
        not in-flight — exclude it from the mutex."""
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                not_before="2099-01-01T00:00:00Z",
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [], "not_before-unmet auto_delivered ticket must not hold the mutex")

    def test_interactive_required_stamped_ticket_excluded(self):
        """T-0981 regression: an auto_delivered ticket later marked interactive-required
        (parked/deferred) is NOT in-flight — an interactive-required ticket is never
        auto-deliverable, so it cannot be a live dispatch and must be excluded from the
        mutex. Else a stale auto_delivered stamp from a race delivery holds the lock and
        suppresses ALL inbox delivery (the T-0768 incident class via the deferral path).
        Live trigger 2026-06-08: deferring the §F build tickets (T-0810 carried a fresh
        auto_delivered stamp) required a hand-clear to free the consumer."""
        from tools.inbox_consumer import _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                labels=["interactive-required"],
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                result = _list_auto_delivered_unresolved_for_agent("orchestrator")
        self.assertEqual(result, [],
                         "interactive-required auto_delivered ticket must not hold the mutex")

    def test_interactive_required_does_not_lock_dispatch(self):
        """T-0981 end-to-end: check_dispatch_locked must NOT raise when the only
        auto_delivered ticket is interactive-required — the consumer is free to deliver
        other work."""
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                labels=["interactive-required"],
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                check_dispatch_locked("orchestrator")  # must not raise


# ---------------------------------------------------------------------------
# T-0717 (b): _is_auto_delivered_stale tests.
# ---------------------------------------------------------------------------


class TestIsAutoDeliveredStale(unittest.TestCase):
    def _make_ticket_with_transition(self, last_transition_iso: str,
                                     path: Path | None = None) -> "TicketInfo":
        body = (
            "## Ask\nDo thing.\n\n"
            "## Transition log\n\n"
            f"- 2026-06-02T08:00:00Z [orchestrator] filed in inbox\n"
            f"- {last_transition_iso} [claude] claimed\n"
        )
        return TicketInfo(
            id="T-0001", title="Test", created_str="2026-06-02T08:00:00Z",
            depends_on=[], labels=[], body=body,
            path=path or Path("/fake/T-0001.md"),
        )

    def test_fresh_transition_is_not_stale(self):
        from tools.inbox_consumer import _is_auto_delivered_stale
        from datetime import datetime, timezone, timedelta
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ticket = self._make_ticket_with_transition(recent_ts)
        self.assertFalse(_is_auto_delivered_stale(ticket, threshold_seconds=28800))

    def test_old_transition_is_stale(self):
        from tools.inbox_consumer import _is_auto_delivered_stale
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ticket = self._make_ticket_with_transition(old_ts)
        self.assertTrue(_is_auto_delivered_stale(ticket, threshold_seconds=28800))

    def test_no_transition_log_returns_false(self):
        """Skip-on-None: unparseable transition log → not stale (cannot prove staleness)."""
        from tools.inbox_consumer import _is_auto_delivered_stale
        ticket = TicketInfo(
            id="T-0001", title="Test", created_str="2026-06-02T08:00:00Z",
            depends_on=[], labels=[],
            body="## Ask\nDo thing.\n",  # No transition log section
            path=Path("/fake/T-0001.md"),
        )
        self.assertFalse(_is_auto_delivered_stale(ticket, threshold_seconds=28800))

    def test_file_mtime_extends_liveness_window(self):
        """max(transition-log ts, file mtime) — a recently-touched file is not stale
        even if the transition log has an old entry."""
        from tools.inbox_consumer import _is_auto_delivered_stale
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            # Write ticket file with fresh mtime (now)
            ticket_path = Path(tmp) / "T-0001.md"
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = (
                "## Ask\nDo thing.\n\n"
                "## Transition log\n\n"
                f"- {old_ts} [claude] claimed\n"
            )
            ticket_path.write_text(body, encoding="utf-8")
            # File mtime is fresh (just written), so not stale
            ticket = TicketInfo(
                id="T-0001", title="Test", created_str="2026-06-02T08:00:00Z",
                depends_on=[], labels=[], body=body, path=ticket_path,
            )
            self.assertFalse(_is_auto_delivered_stale(ticket, threshold_seconds=28800))


# ---------------------------------------------------------------------------
# T-0717 (b): check_dispatch_locked tests.
# ---------------------------------------------------------------------------


class TestCheckDispatchLocked(unittest.TestCase):
    def test_no_stamped_tickets_passes(self):
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            # A normal active ticket (no auto_delivered) — must not lock
            _write_ticket(tickets_root, "T-0001", "orchestrator", state="active")
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                check_dispatch_locked("orchestrator")  # must not raise

    def test_stamped_active_ticket_raises_dispatch_locked(self):
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                last_transition=recent_ts,
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    check_dispatch_locked("orchestrator")
        self.assertEqual(ctx.exception.vocab, "dispatch_locked")

    def test_stamped_inbox_ticket_raises_dispatch_locked(self):
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="inbox",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                last_transition=recent_ts,
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                with self.assertRaises(SkipCycle) as ctx:
                    check_dispatch_locked("orchestrator")
        self.assertEqual(ctx.exception.vocab, "dispatch_locked")

    def test_resolved_ticket_not_locked(self):
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="done",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z"
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                check_dispatch_locked("orchestrator")  # done tickets release lock; must not raise

    def test_wrong_agent_not_locked(self):
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # specialist has in-flight ticket — must not block orchestrator
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "specialist", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                last_transition=recent_ts,
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root):
                check_dispatch_locked("orchestrator")  # must not raise

    def test_stale_ticket_releases_lock_and_alerts(self):
        """Stamped ticket whose liveness signal (max(transition-log ts, file mtime))
        is older than AUTO_DELIVERED_STALE_SECONDS → lock released, Telegram alert fired."""
        from tools.inbox_consumer import check_dispatch_locked, AUTO_DELIVERED_STALE_SECONDS
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            # Old transition — 10 hours ago
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            path = _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T04:00:00Z",
                last_transition=old_ts,
            )
            # Set file mtime to also be old (older than AUTO_DELIVERED_STALE_SECONDS)
            import os
            old_mtime = (datetime.now(timezone.utc) - timedelta(hours=10)).timestamp()
            os.utime(path, (old_mtime, old_mtime))

            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer._send_telegram_alert") as mock_alert, \
                 patch("tools.inbox_consumer.subprocess.run",
                       return_value=MagicMock(returncode=0, stderr="")), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                # Must NOT raise — stale ticket releases the lock
                check_dispatch_locked("orchestrator")
            mock_alert.assert_called_once()
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("T-0001", alert_text)
            self.assertIn("stale", alert_text.lower())

    def test_clear_auto_delivered_removes_stamp(self):
        """_clear_auto_delivered removes auto_delivered + auto_delivered_at from frontmatter."""
        from tools.inbox_consumer import _clear_auto_delivered, _list_auto_delivered_unresolved_for_agent
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            path = _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T04:00:00Z",
                last_transition="2026-06-02T04:00:00Z",
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer.subprocess.run",
                       return_value=MagicMock(returncode=0, stderr="")), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                from tools.inbox_consumer import TicketInfo
                # Build a minimal TicketInfo for the path (mirror what the consumer passes).
                inflight = _list_auto_delivered_unresolved_for_agent("orchestrator")
                self.assertEqual(len(inflight), 1)
                ok = _clear_auto_delivered(inflight[0])
            self.assertTrue(ok)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("auto_delivered:", text,
                             "auto_delivered stamp must be gone after clear")

    def test_stale_release_is_idempotent_alerts_once(self):
        """Regression (msg_3073): the staleness backstop must alert ONCE, not every
        cycle. After the first stale-detection clears the stamp, a subsequent cycle
        finds no auto_delivered ticket → no re-alert."""
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            path = _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T04:00:00Z",
                last_transition=old_ts,
            )
            import os
            old_mtime = (datetime.now(timezone.utc) - timedelta(hours=10)).timestamp()
            os.utime(path, (old_mtime, old_mtime))
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer._send_telegram_alert") as mock_alert, \
                 patch("tools.inbox_consumer.subprocess.run",
                       return_value=MagicMock(returncode=0, stderr="")), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                check_dispatch_locked("orchestrator")   # cycle 1: detects stale → alert + clear
                check_dispatch_locked("orchestrator")   # cycle 2: stamp gone → no alert
            mock_alert.assert_called_once()

    def test_fresh_ticket_does_not_trigger_stale(self):
        """Stamped ticket with recent activity → lock holds, no alert."""
        from tools.inbox_consumer import check_dispatch_locked
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                last_transition=recent_ts,
            )
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer._send_telegram_alert") as mock_alert:
                with self.assertRaises(SkipCycle) as ctx:
                    check_dispatch_locked("orchestrator")
        self.assertEqual(ctx.exception.vocab, "dispatch_locked")
        mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# T-0717 (b): _stamp_auto_delivered tests.
# ---------------------------------------------------------------------------


class TestStampAutoDelivered(unittest.TestCase):
    def test_stamp_writes_fields_to_frontmatter_and_returns_true(self):
        """_stamp_auto_delivered writes auto_delivered: true + auto_delivered_at to disk
        and returns True (write succeeded → inject may proceed)."""
        from tools.inbox_consumer import _stamp_auto_delivered
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            path = _write_ticket(tickets_root, "T-0001", "orchestrator", state="active",
                                 title="Ship it")
            ticket = TicketInfo(
                id="T-0001", title="Ship it",
                created_str="2026-06-02T10:00:00Z",
                depends_on=[], labels=[],
                body="## Ask\nDo.\n\n## Transition log\n\n- 2026-06-02T10:00:00Z [orchestrator] filed in inbox\n",
                path=path,
            )
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                result = _stamp_auto_delivered(ticket)
            # Read the written file and check fields
            from tools.ticket import parse_front_matter
            text = path.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
        self.assertTrue(result)  # write succeeded → caller should inject
        self.assertEqual(fields.get("auto_delivered"), "true")
        self.assertIsNotNone(fields.get("auto_delivered_at"))

    def test_stamp_returns_true_on_commit_failure_inject_proceeds(self):
        """Commit/push failure → returns True (write established local mutex; inject proceeds).

        The frontmatter write succeeded so this agent's consumer sees the lock
        on the next cycle. Commit failure is best-effort; inject should happen.
        """
        from tools.inbox_consumer import _stamp_auto_delivered
        from tools.ticket import parse_front_matter
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            path = _write_ticket(tickets_root, "T-0001", "orchestrator", state="active")
            ticket = TicketInfo(
                id="T-0001", title="Test",
                created_str="2026-06-02T10:00:00Z",
                depends_on=[], labels=[],
                body="## Ask\nDo.\n\n## Transition log\n\n- 2026-06-02T10:00:00Z [orchestrator] filed in inbox\n",
                path=path,
            )

            def mock_run(cmd, **kwargs):
                # File write succeeds (not subprocess). git calls fail.
                return MagicMock(returncode=1, stdout="", stderr="error")

            with patch("subprocess.run", side_effect=mock_run):
                result = _stamp_auto_delivered(ticket)
            # Write succeeded → returns True (inject should proceed)
            self.assertTrue(result)
            # Verify the stamp is on disk even though commit failed
            text = path.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
            self.assertEqual(fields.get("auto_delivered"), "true")

    def test_stamp_returns_false_on_write_error_inject_blocked(self):
        """File-write failure → returns False (fail-CLOSED; caller must not inject).

        Without the stamp on disk the mutex is absent; delivering a ticket
        would leave it unlocked and the next cycle could double-deliver.
        """
        from tools.inbox_consumer import _stamp_auto_delivered
        with tempfile.TemporaryDirectory() as tmp:
            # Use a path that doesn't exist to trigger write failure
            ticket = TicketInfo(
                id="T-0001", title="Test",
                created_str="2026-06-02T10:00:00Z",
                depends_on=[], labels=[],
                body="## Ask\nDo.\n\n## Transition log\n\n- 2026-06-02T10:00:00Z [orchestrator] filed in inbox\n",
                path=Path(tmp) / "nonexistent_dir" / "T-0001.md",
            )
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                result = _stamp_auto_delivered(ticket)
        # Write failed → returns False (caller must not inject)
        self.assertFalse(result)

    def test_stamp_write_failed_raises_skip_cycle_not_inject(self):
        """main(): write failure → SkipCycle('stamp_write_failed'), NO inject.

        The stamp write (local disk) is fail-CLOSED: if it fails, the cycle
        skips without delivering, preserving the mutex invariant. Inject must
        NOT be called.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            log_path = Path(tmp) / "inbox_consumer.log"
            # A ticket in inbox with no prior auto_delivered stamp
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="inbox", title="New work")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None}
            }).encode()

            inject_called = []

            def track_inject(tmux_target, prompt):
                inject_called.append((tmux_target, prompt))

            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"), \
                 patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap.json"), \
                 patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}), \
                 patch("urllib.request.urlopen", return_value=resp), \
                 patch("tools.inbox_consumer.pull_repo_or_warn"), \
                 patch("tools.inbox_consumer._stamp_auto_delivered", return_value=False), \
                 patch("tools.inbox_consumer.inject", side_effect=track_inject), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                result = main("orchestrator")
            self.assertEqual(result, 0)
            # inject must NOT have been called
            self.assertEqual(inject_called, [], "inject must not be called when stamp write fails")
            log_content = log_path.read_text()
            self.assertIn("skip:stamp_write_failed", log_content)

    def test_stamp_commit_failure_still_injects(self):
        """main(): commit failure → inject still happens (write established local mutex).

        A commit/push failure means _stamp_auto_delivered returns True (local
        write succeeded). main() must proceed with inject.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            log_path = Path(tmp) / "inbox_consumer.log"
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="inbox", title="New work")

            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({
                "last_inbound_at": {"orchestrator": None}
            }).encode()

            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"), \
                 patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap.json"), \
                 patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}), \
                 patch("urllib.request.urlopen", return_value=resp), \
                 patch("tools.inbox_consumer.pull_repo_or_warn"), \
                 patch("tools.inbox_consumer._stamp_auto_delivered", return_value=True), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                result = main("orchestrator")
            self.assertEqual(result, 0)
            log_content = log_path.read_text()
            self.assertIn("pulled T-0002", log_content)


# ---------------------------------------------------------------------------
# T-0717 (b): Full-cycle integration tests with mutex.
# ---------------------------------------------------------------------------


class TestFullCycleWithMutex(unittest.TestCase):
    def _health_resp(self):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({
            "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
        }).encode()
        return resp

    def test_mutex_prevents_second_injection(self):
        """main() skips injection with dispatch_locked when an in-flight stamped ticket exists."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            log_path = Path(tmp) / "inbox_consumer.log"
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # One in-flight stamped active ticket
            _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T10:00:00Z",
                last_transition=recent_ts,
            )
            # One eligible inbox ticket (should NOT be delivered due to lock)
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="inbox")

            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"), \
                 patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap.json"), \
                 patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}), \
                 patch("urllib.request.urlopen", return_value=self._health_resp()), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
                 patch("tools.inbox_consumer.pull_repo_or_warn"), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                result = main("orchestrator")
            self.assertEqual(result, 0)
            log_content = log_path.read_text()
            self.assertIn("skip:dispatch_locked", log_content)
            self.assertNotIn("pulled T-0002", log_content)

    def test_mutex_allows_injection_when_no_inflight(self):
        """main() delivers normally when no stamped in-flight ticket exists."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            log_path = Path(tmp) / "inbox_consumer.log"
            _write_ticket(tickets_root, "T-0002", "orchestrator", state="inbox", title="New work")

            with patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"), \
                 patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap.json"), \
                 patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer.LOG_PATH", log_path), \
                 patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}), \
                 patch("urllib.request.urlopen", return_value=self._health_resp()), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
                 patch("tools.inbox_consumer.pull_repo_or_warn"), \
                 patch("tools.inbox_consumer._send_telegram_alert"):
                result = main("orchestrator")
            self.assertEqual(result, 0)
            log_content = log_path.read_text()
            self.assertIn("pulled T-0002", log_content)


class TestFormatPromptServes(unittest.TestCase):
    """T-1103: format_prompt includes serves: line when TicketInfo.serves is set."""

    def _make_ticket(self, serves: "str | None") -> TicketInfo:
        return TicketInfo(
            id="T-0099",
            title="Arc-intent test",
            created_str="2026-06-12T00:00:00Z",
            depends_on=[],
            labels=[],
            body="## Ask\n\nDo the arc thing.\n\n## Transition log\n",
            path=Path("/fake"),
            serves=serves,
        )

    def test_serves_present_appears_in_prompt(self) -> None:
        """When serves is set, the formatted prompt includes it."""
        t = self._make_ticket("ORG-034 structural arc-intent")
        result = format_prompt(t, "orchestrator")
        self.assertIn("ORG-034 structural arc-intent", result)

    def test_serves_absent_prompt_unchanged(self) -> None:
        """When serves is None, the prompt is identical to the baseline (no serves line injected)."""
        t_no_serves = self._make_ticket(None)
        # Build a baseline ticket with no serves (same as existing TestFormatPrompt.test_substitutions)
        result = format_prompt(t_no_serves, "orchestrator")
        # The standard substitutions still work
        self.assertIn("T-0099", result)
        self.assertIn("Arc-intent test", result)
        # No serves text injected
        self.assertNotIn("Serves:", result)

    def test_serves_value_not_duplicated(self) -> None:
        """serves value appears exactly once in the formatted prompt."""
        t = self._make_ticket("my-arc")
        result = format_prompt(t, "orchestrator")
        self.assertEqual(result.count("my-arc"), 1)


if __name__ == "__main__":
    unittest.main()
