"""Tests for tools.inbox_consumer."""

from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Bootstrap: same sys.path trick as inbox_consumer.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.inbox_consumer as inbox_consumer  # noqa: E402 (module handle, T-2206)
from tools.inbox_consumer import (
    INJECT_PROMPT,
    INJECT_ENTER_DELAY,
    IDLE_SENTINELS,
    PENDING_PASTE_SENTINEL,
    CONSECUTIVE_SKIP_ALERT_THRESHOLD,
    ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS,
    ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS,
    PANE_WEDGE_SENTINELS,
    DELIVERY_MODE,
    MAILBOX_HUB,
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
    deliver,
    format_prompt,
    inject,
    list_eligible_tickets,
    log_outcome,
    mailbox_inbox_for,
    main,
    pick_ticket,
    pull_repo_or_warn,
    send_mailbox,
)
from tools import mailbox


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
# Delivery-mode dispatch (T-1828 R2): mirrors keep_going.py's DeliveryModeTest
# (T-1827 R1) at the step-8 ticket-poke call site. specialist rides mailbox.send();
# every other agent (and any agent absent from DELIVERY_MODE) stays on the
# byte-identical send-keys path via the untouched inject().
# ---------------------------------------------------------------------------


class TestDeliveryMode(unittest.TestCase):
    def test_orchestrator_default_mode_uses_send_keys_not_mailbox(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run, \
             patch("tools.inbox_consumer.send_mailbox") as mock_send:
            deliver("orchestrator", "claude-telegram-bot", "some prompt")
        self.assertEqual(mock_run.call_count, 1, "default mode must still pane-inject")
        mock_send.assert_not_called()

    def test_recovery_default_mode_uses_send_keys_not_mailbox(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run, \
             patch("tools.inbox_consumer.send_mailbox") as mock_send:
            deliver("recovery", "claude-telegram-bot-fallback", "some prompt")
        self.assertEqual(mock_run.call_count, 1)
        mock_send.assert_not_called()

    def test_specialist_mode_uses_mailbox_not_send_keys(self):
        with patch("subprocess.run") as mock_run, \
             patch("tools.inbox_consumer.send_mailbox", return_value=True) as mock_send:
            deliver("specialist", "claude-research-bot", "some prompt")
        mock_run.assert_not_called()
        mock_send.assert_called_once_with("specialist", "some prompt")

    def test_unknown_agent_falls_back_to_send_keys(self):
        # Fail-open contract: an agent absent from DELIVERY_MODE must never
        # silently drop delivery — it falls through to send-keys.
        # Uses a deliberately nonexistent persona: metis/atlas used to serve as
        # the stand-in here, but T-2144 listed every real persona explicitly so
        # the coverage test can tell "deliberately send-keys" from "never
        # considered". A contract test must not depend on a real name staying
        # unclassified, or completing the map breaks the contract test.
        unknown = "no-such-persona-t2144"
        self.assertNotIn(unknown, DELIVERY_MODE)
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run, \
             patch("tools.inbox_consumer.send_mailbox") as mock_send:
            deliver(unknown, "suborch1-main", "some prompt")
        self.assertEqual(mock_run.call_count, 1)
        mock_send.assert_not_called()

    def test_specialist_mailbox_send_failure_raises_mailbox_unreachable(self):
        with patch("tools.inbox_consumer.send_mailbox", return_value=False):
            with self.assertRaises(SkipCycle) as ctx:
                deliver("specialist", "claude-research-bot", "some prompt")
        self.assertEqual(ctx.exception.vocab, "mailbox_unreachable")


# ---------------------------------------------------------------------------
# send_mailbox()/mailbox_inbox_for() in isolation, against the REAL
# tools/mailbox.py send()/consume() — always a tmp-dir inbox, NEVER the real
# hub at /home/the-ancestor/mailbox. Mirrors keep_going.py's
# MailboxDeliveryUnitTest (T-1827 R1).
# ---------------------------------------------------------------------------


class TestMailboxDeliveryUnit(unittest.TestCase):
    def test_mailbox_inbox_for_matches_hub_convention(self):
        self.assertEqual(mailbox_inbox_for("specialist"), MAILBOX_HUB / "specialist")

    def test_send_mailbox_delivers_verbatim_payload(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d) / "specialist"
            mailbox.provision(inbox)  # T-2216: send() no longer auto-provisions
            prompt = format_prompt(
                TicketInfo(id="T-9999", title="Probe", created_str="2026-07-27T00:00:00Z",
                           depends_on=[], labels=[], body="## Ask\n\nDo the thing.",
                           path=Path("/dev/null"), serves=None),
                "specialist",
            )
            ok = send_mailbox("specialist", prompt, inbox=inbox)
            self.assertTrue(ok)
            got = []
            res = mailbox.consume(inbox, lambda m: got.append(m))
            self.assertFalse(res["skipped"])
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["payload"], prompt, "payload must be verbatim")
            # T-1935 forensics precedent (also honored by keep_going): stamp the
            # PRODUCER, not the account persona — inbox_consumer is a distinct
            # producer from keep_going, so its sender stamp must differ.
            self.assertEqual(got[0]["sender"], "inbox_consumer")


# ---------------------------------------------------------------------------
# Redelivery safety (T-1828 R2 REQUIRED coverage): the ticket-poke payload is
# a durable state-pointer, and delivering it twice must never corrupt state.
# Two independent layers, neither touched by this diff:
#   (a) producer-side dedupe — mailbox.send()'s T-1935 dedupe_pending default
#       collapses a same-content resend while the prior message still sits
#       unconsumed in new/.
#   (b) consumer-side at-least-once safety — a redelivered (already-consumed)
#       message just re-points at durable state; ticket.py's claim guard
#       already refuses a second claim on a non-inbox ticket with a clean,
#       non-corrupting TicketError (proven by the existing, untouched
#       tools/test_ticket.py:589 test_claim_from_active_refuses — cited here,
#       not re-implemented; ticket.py is out of this ticket's file scope).
# ---------------------------------------------------------------------------


class TestRedeliverySafety(unittest.TestCase):
    def test_same_prompt_sent_twice_before_consume_dedupes_to_one_message(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d) / "specialist"
            mailbox.provision(inbox)  # T-2216: send() no longer auto-provisions
            prompt = "[inbox-auto-consumer] T-9999 ('Probe') is in your inbox; target=specialist."
            self.assertTrue(send_mailbox("specialist", prompt, inbox=inbox))
            self.assertTrue(send_mailbox("specialist", prompt, inbox=inbox))
            new_files = list((inbox / "new").glob("*"))
            self.assertEqual(len(new_files), 1,
                              "mailbox.send()'s pending-window dedupe (T-1935) must "
                              "collapse an identical unconsumed resend to one message")

    def test_redelivered_message_payload_is_unchanged_state_pointer_text(self):
        # The payload text itself carries the safety property: it never
        # instructs sequenced/imperative action beyond "claim via ticket.py"
        # against the durable ticket state, so replaying it is always safe.
        # Byte-exact guard lives in TestInjectPromptByteExact; this asserts
        # the delivered mailbox payload is that exact text, unmodified by the
        # mailbox transport.
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d) / "specialist"
            mailbox.provision(inbox)  # T-2216: send() no longer auto-provisions
            prompt = format_prompt(
                TicketInfo(id="T-9999", title="Probe", created_str="2026-07-27T00:00:00Z",
                           depends_on=[], labels=[], body="## Ask\n\nDo the thing.",
                           path=Path("/dev/null"), serves=None),
                "specialist",
            )
            send_mailbox("specialist", prompt, inbox=inbox)
            got = []
            mailbox.consume(inbox, lambda m: got.append(m))
            self.assertEqual(got[0]["payload"], _EXPECTED_INJECT_PROMPT
                              .replace("T-NNNN", "T-9999").replace("<title>", "Probe")
                              .replace("<agent>", "specialist")
                              .replace("<body>", "Do the thing."))


# ---------------------------------------------------------------------------
# Full-cycle: main() routes specialist through mailbox, other agents unchanged.
# Extends the TestFullCycle pattern (tmux-only) with the mailbox leg.
# ---------------------------------------------------------------------------


class TestFullCycleDeliveryRouting(unittest.TestCase):
    def _health_ok(self):
        health_resp = MagicMock()
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)
        health_resp.read.return_value = json.dumps({
            "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
        }).encode()
        return health_resp

    def test_specialist_cycle_calls_mailbox_send_not_tmux_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0199", "specialist", title="Specialist probe")
            log_path = Path(tmp) / "inbox_consumer.log"
            mailbox_inbox = Path(tmp) / "specialist-mailbox"
            mailbox.provision(mailbox_inbox)  # T-2216: send() no longer auto-provisions

            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", log_path),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"specialist": "claude-research-bot"}),
                patch("tools.inbox_consumer.mailbox_inbox_for", return_value=mailbox_inbox),
                patch("urllib.request.urlopen") as mock_urlopen,
                patch("subprocess.run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run,
                patch("tools.inbox_consumer.check_dispatch_locked"),
                patch("tools.inbox_consumer._stamp_auto_delivered"),
            ):
                mock_urlopen.return_value = self._health_ok()
                result = main("specialist")

            self.assertEqual(result, 0)
            # No tmux helper call for the ticket-poke leg.
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(len(inject_calls), 0, "specialist must not pane-inject the poke")
            got = []
            mailbox.consume(mailbox_inbox, lambda m: got.append(m))
            self.assertEqual(len(got), 1)
            self.assertIn("T-0199", got[0]["payload"])
            self.assertIn("Specialist probe", got[0]["payload"])
            log_content = log_path.read_text()
            self.assertIn("specialist pulled T-0199", log_content)

    def test_orchestrator_cycle_still_uses_tmux_helper_not_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0198", "orchestrator", title="Orchestrator ticket")
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
                patch("tools.inbox_consumer.send_mailbox") as mock_send,
                patch("tools.inbox_consumer.check_dispatch_locked"),
                patch("tools.inbox_consumer._stamp_auto_delivered"),
            ):
                mock_urlopen.return_value = self._health_ok()
                result = main("orchestrator")

            self.assertEqual(result, 0)
            mock_send.assert_not_called()
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(len(inject_calls), 1)


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
# T-3010: concurrent state-flip race between scan + deliver.
# ---------------------------------------------------------------------------


class TestConcurrentStateFlip(unittest.TestCase):
    """T-3010: ticket state flipped from inbox -> done|active|missing between
    list_eligible_tickets (scan) and deliver(). Must skip delivery, not poke
    a stale ticket. Vocab: concurrent_state_flip, NEVER_ALERT (benign race)."""

    def _run_cycle_with_flip(self, flip_to: str | None) -> tuple[int, list, MagicMock]:
        """Run main("orchestrator") with the ticket state flipped just before deliver.
        Returns (result, tmux_calls, log_outcome_mock)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-9910", "orchestrator", title="Flip test")

            send_result = MagicMock(returncode=0, stdout="", stderr="")

            def flipped_state(ticket_id: str):
                if ticket_id.strip() == "T-9910":
                    return flip_to  # None means missing; else "done"/"active"
                return "inbox"

            with (
                patch("tools.inbox_consumer.AUTONOMY_OFF_PATH", Path(tmp) / "autonomy_off"),
                patch("tools.inbox_consumer.CAP_HIT_PATH", Path(tmp) / "no_cap_hit.json"),
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.LOG_PATH", Path(tmp) / "inbox_consumer.log"),
                patch("tools.inbox_consumer.TMUX_TARGET_BY_AGENT", {"orchestrator": "claude-telegram-bot"}),
                patch("urllib.request.urlopen") as mock_urlopen,
                patch("subprocess.run", return_value=send_result) as mock_run,
                patch("tools.inbox_consumer.time"),
                patch("tools.inbox_consumer.check_dispatch_locked"),
                patch("tools.inbox_consumer._stamp_auto_delivered"),
                patch("tools.inbox_consumer.log_outcome") as mock_log,
                patch("tools.inbox_consumer.check_consecutive_skips"),
                # scan (via list_eligible_tickets) still sees inbox on disk;
                # only the pre-deliver re-check is patched to return the flipped state.
                patch("tools.inbox_consumer._ticket_state_on_disk", side_effect=flipped_state),
            ):
                health_resp = MagicMock()
                health_resp.__enter__ = lambda s: s
                health_resp.__exit__ = MagicMock(return_value=False)
                health_resp.read.return_value = json.dumps({
                    "last_inbound_at": {"orchestrator": None, "specialist": None, "recovery": None}
                }).encode()
                mock_urlopen.return_value = health_resp

                result = main("orchestrator")

            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            return result, inject_calls, mock_log

    def _assert_skip_flip(self, result, inject_calls, mock_log):
        self.assertEqual(result, 0)
        self.assertEqual(len(inject_calls), 0,
                         "deliver() must not fire when ticket flipped between scan + deliver")
        # log_outcome called with skip:concurrent_state_flip on the exception path.
        outcomes = [call.args[1] for call in mock_log.call_args_list if len(call.args) >= 2]
        self.assertIn("skip:concurrent_state_flip", outcomes)

    def test_state_flipped_to_done_skips_deliver(self):
        self._assert_skip_flip(*self._run_cycle_with_flip("done"))

    def test_state_flipped_to_active_skips_deliver(self):
        self._assert_skip_flip(*self._run_cycle_with_flip("active"))

    def test_state_flipped_to_missing_skips_deliver(self):
        # Ticket file disappeared entirely between scan + deliver — same treatment.
        self._assert_skip_flip(*self._run_cycle_with_flip(None))

    def test_vocab_is_never_alert(self):
        # Regression: the skip vocab must be classified as never-alert so a
        # benign race doesn't trigger the 6-consecutive-skip alert.
        from tools.inbox_consumer import NEVER_ALERT_SKIP_VOCABS
        self.assertIn("concurrent_state_flip", NEVER_ALERT_SKIP_VOCABS)


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


class TestRouteFailureAlertMuteT2978(unittest.TestCase):
    """ORG-076 §7 / T-2978: a MUTED persona's failure alerts are suppressed at the
    route_failure_alert chokepoint (both the via-Recovery and the recovery-self-direct
    paths), while an UNMUTED persona routes normally. Fail-open: is_muted default
    is False, so pre-existing behavior is unchanged until a persona is muted."""

    def test_muted_agent_alert_is_suppressed(self):
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._persona_is_muted", return_value=True), \
             patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("mnemosyne", "mnemosyne wedged", "restart-failure (mnemosyne)")
        via_recovery.assert_not_called()
        direct.assert_not_called()

    def test_muted_recovery_self_is_also_suppressed(self):
        # uniform mute: muting recovery suppresses even the recovery-self direct-to-operator path.
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._persona_is_muted", return_value=True), \
             patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("recovery", "recovery wedged", "restart-failure (recovery)")
        via_recovery.assert_not_called()
        direct.assert_not_called()

    def test_unmuted_agent_routes_normally(self):
        from tools.inbox_consumer import route_failure_alert
        with patch("tools.inbox_consumer._persona_is_muted", return_value=False), \
             patch("tools.inbox_consumer._send_alert_via_recovery") as via_recovery, \
             patch("tools.inbox_consumer._send_telegram_alert") as direct:
            route_failure_alert("orchestrator", "orchestrator wedged", "restart-failure (orchestrator)")
        via_recovery.assert_called_once()
        direct.assert_not_called()


class TestRoutedAuditThrottleT2237Rev2(unittest.TestCase):
    """T-2237 rev2 part 2: the `[recovery routed]` AUDIT ping is throttled per vocab.

    From the 2026-08-02 flood — the operator got "a ton" of these, one per routed
    alert. The audit states a standing fact about the ROUTING ("Recovery has it, you
    are not on the decision path"), not a per-alert event, so N alerts of one
    class in a window are worth one ping.
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = self.d / "audit.json"
        p = patch("tools.inbox_consumer.ROUTED_AUDIT_STATE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _route(self, vocab="mail_staleness", pane_ok=True, mailbox_ok=False):
        # T-3474: the mailbox leg defaults False so a pane_ok=False route still
        # exercises the operator-fallback (both legs fail); pass mailbox_ok=True to
        # exercise the cross-account recovery-inbox routing path.
        from tools.inbox_consumer import _send_alert_via_recovery
        with patch("tools.inbox_consumer._send_to_recovery_pane", return_value=pane_ok) as pane, \
             patch("tools.inbox_consumer._deliver_alert_to_recovery_mailbox", return_value=mailbox_ok), \
             patch("tools.inbox_consumer._send_telegram_info") as info, \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery("something is wedged", vocab)
        return pane, info, alert

    def test_first_alert_of_a_vocab_audits(self):
        _, info, _ = self._route()
        info.assert_called_once()
        self.assertIn("[recovery routed]", info.call_args[0][0])

    def test_the_flood_collapses_to_one_ping(self):
        """The regression itself: mail_staleness alerts one inbox at a time,
        every 5 minutes, and each one minted its own audit."""
        self._route()
        infos = [self._route()[1].call_count for _ in range(5)]
        self.assertEqual(infos, [0, 0, 0, 0, 0])

    def test_the_PANE_injection_is_never_throttled(self):
        """Only the audit is noise. Recovery must receive every single alert — a
        throttle that swallowed the routing itself would drop real work."""
        self._route()
        pane, info, _ = self._route()
        pane.assert_called_once()
        info.assert_not_called()

    def test_a_DIFFERENT_vocab_is_not_suppressed_by_the_first(self):
        """Per-vocab, not global: a class the operator has not heard about must not
        inherit another class's cooldown."""
        self._route(vocab="mail_staleness")
        _, info, _ = self._route(vocab="cap-autoswitch (orchestrator)")
        info.assert_called_once()

    def test_it_audits_again_once_the_cooldown_expires(self):
        """A cooldown that never expires is a mute — the routing is still live."""
        import tools.inbox_consumer as ic
        self._route()
        stale = time.time() - ic.ROUTED_AUDIT_COOLDOWN_SECONDS - 1
        self.path.write_text(json.dumps({"mail_staleness": stale}))
        _, info, _ = self._route()
        info.assert_called_once()

    def test_the_FALLBACK_path_is_never_throttled(self):
        """recovery-pane unreachable means Recovery did NOT get it and the operator IS on the
        decision path. That is the one message this change must never touch."""
        self._route()  # burn the cooldown for this vocab
        # T-3474: a true operator-fallback is now BOTH legs failing (mailbox_ok=False).
        _, info, alert = self._route(pane_ok=False, mailbox_ok=False)
        self.assertEqual(alert.call_count, 1)
        self.assertIn("[fallback: recovery pane+mailbox unreachable]", alert.call_args[0][0])
        info.assert_not_called()

    def test_a_suppressed_audit_does_not_slide_the_window(self):
        """Otherwise a class alerting faster than the cooldown would be muted
        forever — the stamp must mark the last SENT ping, not the last look."""
        self._route()
        first = json.loads(self.path.read_text())["mail_staleness"]
        self._route()
        self.assertEqual(json.loads(self.path.read_text())["mail_staleness"], first)

    def test_unreadable_state_fails_toward_SENDING(self):
        self.path.write_text("{not json")
        _, info, _ = self._route()
        info.assert_called_once()

    def test_it_refuses_to_touch_production_state_from_a_test(self):
        """The structural gate, same as _log_recovery_routing: a test that does not
        redirect the path cannot reach Recovery's real cooldowns — which would
        suppress live audit pings, and a missing ping is indistinguishable from
        no alert at all."""
        import tools.inbox_consumer as ic
        self.assertTrue(ic._routed_audit_state_is_production(
            ic._ROUTED_AUDIT_STATE_PATH_DEFAULT))
        self.assertFalse(ic._routed_audit_state_is_production(self.path))
        # The write must be checked against the FILE, not against
        # _load_routed_audit_state — the read gate independently returns {} for
        # the production path under test, so asserting on the loaded value
        # passes even with the write gate deleted. (Caught by mutation: this
        # test's first version survived removing the very gate it names, and the
        # mutation run stamped Recovery's real state file to prove it.)
        real = ic._ROUTED_AUDIT_STATE_PATH_DEFAULT
        before = real.read_bytes() if real.exists() else None
        ic._record_routed_audit("mutation-canary", real)
        after = real.read_bytes() if real.exists() else None
        self.assertEqual(after, before, "a test must not write production state")
        if after is not None:
            self.assertNotIn(b"mutation-canary", after)


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
        audit = alert.call_args[0][0]
        self.assertIn("[recovery routed]", audit)
        # TG 5380 fix: the audit now LEADS with the alert's own first line (so the
        # operator sees which channel/subject is alerting), rather than the vocab.
        self.assertIn("Orchestrator's loop is blocked", audit)

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
                 patch("tools.inbox_consumer._deliver_alert_to_recovery_mailbox",
                       return_value=False), \
                 patch("tools.inbox_consumer._send_telegram_alert") as alert:
                vocab = check_orchestrator_health()
        self.assertEqual(vocab, "pane_wedge")
        alert.assert_called_once()
        delivered = alert.call_args[0][0]
        # T-3474: operator-fallback now requires BOTH the pane AND the recovery-mailbox
        # leg to fail (mailbox mocked False above); the prefix names both.
        self.assertIn("[fallback: recovery pane+mailbox unreachable]", delivered)
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
        # Every monitored agent is probed exactly once each.
        called_agents = [c[0][0] for c in probe.call_args_list]
        self.assertIn("orchestrator", called_agents)
        self.assertIn("specialist", called_agents)
        self.assertIn("recovery", called_agents)  # T-0559: recovery self-probes too
        self.assertIn("metis", called_agents)  # T-1940: ORG-043 seats probed
        self.assertIn("atlas", called_agents)
        self.assertEqual(probe.call_count, 5)

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

    def test_inbox_consumer_routes_specialist_targeted_ticket_via_mailbox_not_tmux_pane(self):
        # T-1828 R2: specialist's step-8 ticket-poke now rides DELIVERY_MODE="mailbox"
        # (T-0102-era behavior — specialist on tmux send-keys like every other agent —
        # is superseded for specialist specifically; orchestrator/recovery are unaffected, see
        # tests 4/5 below). mailbox_inbox_for is patched to a tmp dir so this
        # never touches the real hub.
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0200", "specialist", title="Specialist task")
            log_path = Path(tmp) / "inbox_consumer.log"
            mailbox_inbox = Path(tmp) / "specialist-mailbox"
            mailbox.provision(mailbox_inbox)  # T-2216: send() no longer auto-provisions

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
                patch("tools.inbox_consumer.mailbox_inbox_for", return_value=mailbox_inbox),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                main("specialist")

            # No tmux helper call at all for specialist's ticket-poke leg.
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(inject_calls, [], "specialist must not pane-inject the poke")
            got = []
            mailbox.consume(mailbox_inbox, lambda m: got.append(m))
            self.assertEqual(len(got), 1)
            self.assertIn("T-0200", got[0]["payload"])

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
        # T-1828 R2: the idle-pane gate (capture-pane) still runs unchanged for
        # specialist (it gates whether to deliver at all), but the delivery LEG
        # itself is now mailbox, not tmux send-keys — specialist is on
        # DELIVERY_MODE="mailbox". mailbox_inbox_for is patched to a tmp dir.
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_ticket(tickets_root, "T-0201", "specialist", title="Specialist integration test")
            log_path = Path(tmp) / "inbox_consumer.log"
            mailbox_inbox = Path(tmp) / "specialist-mailbox"
            mailbox.provision(mailbox_inbox)  # T-2216: send() no longer auto-provisions

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
                patch("tools.inbox_consumer.mailbox_inbox_for", return_value=mailbox_inbox),
                patch("urllib.request.urlopen", return_value=resp),
                patch("subprocess.run", side_effect=fake_run) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                result = main("specialist")

            # (a) return 0
            self.assertEqual(result, 0)

            # (b) no tmux helper call at all — the ticket-poke leg is mailbox-only
            inject_calls = [c for c in mock_run.call_args_list
                            if str(c[0][0][0]).endswith("send_keys_submit.sh")]
            self.assertEqual(inject_calls, [])

            # (c)/(d) mailbox payload contains ticket id + body text + target=specialist
            got = []
            mailbox.consume(mailbox_inbox, lambda m: got.append(m))
            self.assertEqual(len(got), 1)
            prompt_text = got[0]["payload"]
            self.assertIn("T-0201", prompt_text)
            self.assertIn("Do the thing", prompt_text)
            self.assertIn("target=specialist", prompt_text)

            # (e) log contains pulled entry
            log_content = log_path.read_text()
            self.assertIn("specialist pulled T-0201", log_content)


class TestWardRoutingObservabilityT2206(unittest.TestCase):
    """T-2206: the recovery-pane fallback was unmeasurable AND unattributable.

    RATE: the `[fallback: recovery-pane unreachable]` prefix only ever existed
    inside a Telegram message, and the Bot API exposes no history — so "does
    this fire routinely?" could not be answered after the fact. A fallback that
    fires routinely is not a fallback, it is the primary path.

    MODE: _send_to_recovery_pane captured the helper's stdout and stderr and
    discarded both, returning only `returncode == 0`. Session-name drift, a
    pane present but wedged, and the probe itself erroring were
    indistinguishable from outside.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._log = Path(self._tmp.name) / "recovery_alert_routing.log"
        self._p = patch.object(inbox_consumer, "RECOVERY_ROUTING_LOG", self._log)
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()
        self._tmp.cleanup()

    def _run(self, rc: int, stderr: str = ""):
        completed = subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)
        # The T-0891 suppression guard is kept in the diagnosed helper too
        # (defence in depth — a missing mock once flooded Recovery's REAL pane with
        # 14 alerts), so a test that wants the real body must opt in explicitly.
        with patch.object(inbox_consumer, "_live_telegram_suppressed", return_value=False), \
             patch.object(inbox_consumer, "_resolve_tmux_target", return_value="recovery-sess"), \
             patch.object(inbox_consumer, "subprocess") as sp:
            sp.run.return_value = completed
            return inbox_consumer._send_to_recovery_pane_diagnosed("alert text")

    def test_failure_reason_names_target_rc_and_stderr(self) -> None:
        ok, why = self._run(1, "pane is in a tmux mode; declining to verify")
        self.assertFalse(ok)
        self.assertIn("target=recovery-sess", why)
        self.assertIn("rc=1", why)
        self.assertIn("declining to verify", why,
                      "the helper's stderr must survive — it names the mode")

    def test_success_reason_is_recorded_too(self) -> None:
        ok, why = self._run(0)
        self.assertTrue(ok)
        self.assertIn("rc=0", why)

    def test_probe_error_is_a_distinct_third_mode(self) -> None:
        """OSError used to PROPAGATE instead of falling back — losing the alert
        entirely rather than rerouting it."""
        with patch.object(inbox_consumer, "_live_telegram_suppressed", return_value=False), \
             patch.object(inbox_consumer, "_resolve_tmux_target", return_value="recovery-sess"), \
             patch.object(inbox_consumer, "subprocess") as sp:
            sp.run.side_effect = OSError("helper missing")
            ok, why = inbox_consumer._send_to_recovery_pane_diagnosed("x")
        self.assertFalse(ok, "a probe error must fall back, not raise")
        self.assertIn("probe-error", why)

    def test_both_outcomes_are_logged_so_a_RATE_is_computable(self) -> None:
        """A rate needs a denominator: 3 fallbacks means nothing without the
        routed count beside it."""
        inbox_consumer._log_recovery_routing("routed", "orchestrator-health", "target=t rc=0")
        inbox_consumer._log_recovery_routing("fallback", "acp-dlq", "target=t rc=1 stderr='x'")
        text = self._log.read_text(encoding="utf-8")
        self.assertEqual(len(text.strip().splitlines()), 2)
        self.assertIn("routed", text)
        self.assertIn("fallback", text)
        self.assertIn("acp-dlq", text)

    def test_logging_failure_never_suppresses_the_alert(self) -> None:
        """Observability must degrade, not paging. A full disk or an unwritable
        path must not be able to swallow an operational alert."""
        with patch.object(inbox_consumer, "RECOVERY_ROUTING_LOG",
                          Path("/nonexistent-root-xyz/nope.log")):
            inbox_consumer._log_recovery_routing("fallback", "v", "why")  # must not raise

    def test_suppressed_context_does_not_write_a_routing_line(self) -> None:
        """Under test suppression nothing is actually routed, so logging a
        decision would fabricate history."""
        with patch.object(inbox_consumer, "_live_telegram_suppressed", return_value=True):
            self.assertTrue(inbox_consumer._send_to_recovery_pane("x", vocab="v"))
        self.assertFalse(self._log.exists(), "suppressed run must not log a routing decision")


class TestRoutingLogRefusesToRecordTestRunsT2351(unittest.TestCase):
    """T-2351: the routing log is a MEASUREMENT surface and it was measuring us.

    123 of the log's 136 lines on 2026-07-29 were written by the test suite
    itself — `TestSendToWardPaneHelperDelegation` sets
    PANTHEON_DISABLE_LIVE_TELEGRAM=0 to exercise the real injection body, which
    also re-enabled the production log write, so each full-suite run appended
    routed/fallback/routed/routed. The real numbers underneath were 13 alerts,
    all routed, zero fallback.

    It was read as data: T-2282 measured "103 routed / 24 fallback" from it and
    T-2351 was filed reporting 24 peer alerts a day paging the operator. Zero of them
    were real. These tests pin the gate that makes that impossible.

    Both cases patch _RECOVERY_ROUTING_LOG_DEFAULT alongside RECOVERY_ROUTING_LOG, so
    the production-path branch is exercised without any risk of touching the
    real log.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._log = Path(self._tmp.name) / "recovery_alert_routing.log"
        self.addCleanup(self._tmp.cleanup)

    def test_test_context_does_not_append_to_the_production_log(self) -> None:
        """The defect itself: under a test harness, a write aimed at the
        production default path must be dropped."""
        with patch.object(inbox_consumer, "_RECOVERY_ROUTING_LOG_DEFAULT", self._log), \
             patch.object(inbox_consumer, "RECOVERY_ROUTING_LOG", self._log):
            inbox_consumer._log_recovery_routing("fallback", "", "target=t rc=1 stderr=''")
        self.assertFalse(
            self._log.exists(),
            "a test run appended to the PRODUCTION routing log — this is the "
            "T-2351 defect: the measurement surface records the suite as if it "
            "were production history",
        )

    def test_force_send_override_does_not_reopen_the_hole(self) -> None:
        """PANTHEON_DISABLE_LIVE_TELEGRAM=0 means 'run the real body', never
        'record this as production history'. Gating the log on
        _live_telegram_suppressed() is what let the force-send tests write."""
        with patch.dict(os.environ, {"PANTHEON_DISABLE_LIVE_TELEGRAM": "0"}), \
             patch.object(inbox_consumer, "_RECOVERY_ROUTING_LOG_DEFAULT", self._log), \
             patch.object(inbox_consumer, "RECOVERY_ROUTING_LOG", self._log):
            self.assertFalse(
                inbox_consumer._live_telegram_suppressed(),
                "precondition: the override must really be forcing send-mode, "
                "otherwise this test passes for the wrong reason",
            )
            inbox_consumer._log_recovery_routing("fallback", "", "target=t rc=1 stderr=''")
        self.assertFalse(self._log.exists(),
                         "force-send opted the suite back into the production log")

    def test_a_redirected_path_still_logs_so_the_gate_is_not_vacuous(self) -> None:
        """The gate must suppress the PRODUCTION path only. A guard that blocked
        every write under test would make the existing T-2206 content
        assertions pass by writing nothing at all."""
        other = Path(self._tmp.name) / "redirected.log"
        with patch.object(inbox_consumer, "_RECOVERY_ROUTING_LOG_DEFAULT", self._log), \
             patch.object(inbox_consumer, "RECOVERY_ROUTING_LOG", other):
            inbox_consumer._log_recovery_routing("routed", "orchestrator-health", "target=t rc=0")
        self.assertTrue(other.exists(), "a redirected log must still be written")
        self.assertIn("orchestrator-health", other.read_text(encoding="utf-8"))


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
        audit = alert.call_args[0][0]
        self.assertIn("[recovery routed]", audit)
        # TG 5380 fix: the audit now LEADS with the alert's own first line (so the
        # operator sees WHICH channel/subject is alerting), not only the vocab class.
        self.assertIn("operational alert text", audit)
        self.assertTrue(audit.startswith("[pantheon] some operational alert text"))

    def test_audit_names_the_channel_from_alert_body(self) -> None:
        # TG 5380 (the operator): a routed audit must let him tell WHICH channel is
        # alerting — the prior audit interpolated only the vocab class. The audit
        # now surfaces the alert body's own first line (agent + symptom), and drops
        # the rest of a multi-line body.
        from tools.inbox_consumer import _send_alert_via_recovery
        body = ("📵 [channel-liveness] specialist: inbound Telegram poller DOWN — "
                "Comms-dark; inbound DMs not received.\n(second line of detail)")
        with patch("tools.inbox_consumer._send_to_recovery_pane", return_value=True), \
             patch("tools.inbox_consumer._routed_audit_throttled", return_value=False), \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery(body, vocab="channel-liveness")
        audit = alert.call_args[0][0]
        self.assertIn("specialist", audit)                        # WHICH channel
        self.assertIn("inbound Telegram poller DOWN", audit)  # the symptom
        self.assertIn("[recovery routed]", audit)                 # still the routing note
        self.assertNotIn("second line of detail", audit)      # only the first line

    def test_recovery_pane_and_mailbox_failure_falls_back_with_retry_flag(self) -> None:
        # T-3474: operator-fallback now requires BOTH legs to fail.
        from tools.inbox_consumer import _send_alert_via_recovery
        with patch("tools.inbox_consumer._send_to_recovery_pane",
                   return_value=False), \
             patch("tools.inbox_consumer._deliver_alert_to_recovery_mailbox",
                   return_value=False), \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery("[pantheon] some operational alert text", vocab="test-class")
        alert.assert_called_once()
        delivered = alert.call_args[0][0]
        self.assertIn("[fallback: recovery pane+mailbox unreachable]", delivered)
        self.assertIn("operational alert text", delivered)

    def test_recovery_pane_miss_routes_via_mailbox_not_operator(self) -> None:
        # T-3474 REGRESSION: a cross-account pane miss must reach Recovery via its INBOX
        # (audit-ping to the operator, NOT the [fallback] bypass). Before the fix, specialist's
        # acp-dlq alerts pane-missed and went straight to the operator (routed=0/fallback=28).
        from tools.inbox_consumer import _send_alert_via_recovery
        with patch("tools.inbox_consumer._send_to_recovery_pane", return_value=False), \
             patch("tools.inbox_consumer._deliver_alert_to_recovery_mailbox",
                   return_value=True) as mbox, \
             patch("tools.inbox_consumer._send_telegram_info") as info, \
             patch("tools.inbox_consumer._send_telegram_alert") as alert:
            _send_alert_via_recovery("[pantheon] some operational alert text", vocab="acp-dlq (specialist)")
        mbox.assert_called_once()          # the recovery-inbox leg was used
        alert.assert_not_called()          # NOT the direct-operator bypass
        info.assert_called_once()          # audit-only "[recovery routed]" ping
        self.assertIn("[recovery routed]", info.call_args[0][0])

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
        audit = alert.call_args[0][0]
        self.assertIn("[recovery routed]", audit)
        # TG 5380 fix: audit leads with the alert's own summary (names the subject),
        # not the vocab class.
        self.assertIn("consecutive cycles", audit)

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

    def test_t3003_lock_maintenance_ordered_before_recent_telegram_gate(self):
        """T-3003 regression: pull_repo_or_warn must run before check_dispatch_locked,
        and check_dispatch_locked (stale-lock release + one-in-flight enforcement) must
        run before check_recent_inbound. With the old order (recent_telegram first,
        dispatch-lock second, pull last) a long active conversation short-circuited every
        cycle so the lock cleanup never ran, auto_delivered flags accumulated (>1
        in-flight), then dumped all at once on the first lull (the 5-flag burst). Lock
        maintenance must run every cycle independent of user activity; the mutex must
        scan the pulled (current) tree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            manager = MagicMock()
            with patch("tools.inbox_consumer.LOG_PATH", tmppath / "log"), \
                 patch("tools.inbox_consumer.check_agent_health", return_value=None), \
                 patch("tools.inbox_consumer.check_autonomy_off", manager.autonomy), \
                 patch("tools.inbox_consumer.check_cap_hit", manager.cap), \
                 patch("tools.inbox_consumer.pull_repo_or_warn", manager.pull), \
                 patch("tools.inbox_consumer.check_dispatch_locked", manager.dispatch), \
                 patch("tools.inbox_consumer.check_recent_inbound", manager.recent), \
                 patch("tools.inbox_consumer.list_eligible_tickets",
                       side_effect=SkipCycle("empty_inbox")), \
                 patch("tools.inbox_consumer.surface_active_ticket", return_value=None):
                main("recovery")
            order = [c[0] for c in manager.mock_calls
                     if c[0] in ("pull", "dispatch", "recent")]
            self.assertEqual(
                order, ["pull", "dispatch", "recent"],
                f"expected pull -> dispatch_locked -> recent_inbound; got {order}. "
                "The dispatch-lock maintenance must precede the recent_telegram gate "
                "(else cleanup is skipped during active conversations and flags pile up).",
            )

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
        """Skip-on-None: when NEITHER a transition-log ts NOR a file mtime is
        available (missing/fake path → stat() fails), staleness cannot be proven
        → not stale."""
        from tools.inbox_consumer import _is_auto_delivered_stale
        ticket = TicketInfo(
            id="T-0001", title="Test", created_str="2026-06-02T08:00:00Z",
            depends_on=[], labels=[],
            body="## Ask\nDo thing.\n",  # No transition log section
            path=Path("/fake/T-0001.md"),  # stat() fails → no mtime signal either
        )
        self.assertFalse(_is_auto_delivered_stale(ticket, threshold_seconds=28800))

    def test_no_transition_log_but_old_file_mtime_is_stale(self):
        """Regression (T-2971): a ticket with NO transition-log ts but a real file
        whose mtime is well past the threshold IS stale. The skip-on-None early
        return wrongly bailed BEFORE consulting mtime, so a stale no-transition-log
        auto_delivered ticket wedged the dispatch mutex forever — T-2374 held
        cairn's lock 19 days. Fix: fall back to file mtime (Arm 2) when log_ts is None."""
        from tools.inbox_consumer import _is_auto_delivered_stale
        import os
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            ticket_path = Path(tmp) / "T-2374.md"
            ticket_path.write_text("## Ask\nDo thing.\n", encoding="utf-8")  # no transition log
            old_epoch = (datetime.now(timezone.utc) - timedelta(hours=10)).timestamp()
            os.utime(ticket_path, (old_epoch, old_epoch))  # mtime 10h ago (> 8h threshold)
            ticket = TicketInfo(
                id="T-2374", title="Test", created_str="2026-07-30T18:00:00Z",
                depends_on=[], labels=[], body="## Ask\nDo thing.\n", path=ticket_path,
            )
            self.assertTrue(_is_auto_delivered_stale(ticket, threshold_seconds=28800))

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

    def test_t2336_stamp_commit_is_pathspec_scoped_and_locked(self):
        """T-2336: the stamp's bare `git commit -m msg` was the writer4 shape —
        with a concurrent writer's files staged, it commits THEM under its own
        message (audit-chain corruption: message describes work the commit does
        not contain). Two independent defenses asserted here: the commit must be
        PATHSPEC-scoped (`-- <ticket path>`, so foreign staged files cannot be
        swallowed even by a lock-bypassing writer), and the whole add+commit+push
        must run under git_mutation_lock (so lock-taking writers serialize).

        T-3325: this test exercises the sg-wrapped shell-string shape
        specifically (its assertions check substrings of a shell command),
        so `_git_direct_writable` is pinned to False here — without the pin
        this test's branch would depend on the real repo checkout's actual
        `.git` permission bits at run time, which is exactly the kind of
        nondeterminism T-3325's detector introduces if left unpinned. The
        direct-write branch has its own dedicated tests below.
        """
        from tools.inbox_consumer import _stamp_auto_delivered, _list_auto_delivered_unresolved_for_agent
        from tools import inbox_consumer as ic
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            path = _write_ticket_with_auto_delivered(
                tickets_root, "T-0002", "orchestrator", state="inbox",
                auto_delivered=False, auto_delivered_at=None,
                last_transition="2026-06-02T04:00:00Z",
            )
            commands: list[str] = []

            def capture_run(cmd, **kw):
                commands.append(cmd[-1] if isinstance(cmd, list) else str(cmd))
                return MagicMock(returncode=0, stderr="", stdout="")

            lock_cm = MagicMock()
            with patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root), \
                 patch("tools.inbox_consumer._git_direct_writable", return_value=False), \
                 patch("tools.inbox_consumer.subprocess.run", side_effect=capture_run), \
                 patch("tools.inbox_consumer.git_mutation_lock", return_value=lock_cm) as lock_fn, \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                from tools.inbox_consumer import TicketInfo
                info = TicketInfo(id="T-0002", title="t", created_str="2026-06-02",
                                  depends_on=[], labels=[], body="", path=path)
                ok = _stamp_auto_delivered(info)
            self.assertTrue(ok)
            commit_cmds = [c for c in commands if " commit " in c]
            self.assertEqual(len(commit_cmds), 1)
            self.assertIn(" -- ", commit_cmds[0])
            self.assertIn("T-0002.md", commit_cmds[0].split(" -- ", 1)[1])
            self.assertTrue(lock_fn.called, "add+commit+push must run under git_mutation_lock")
            lock_cm.__enter__.assert_called_once()
            lock_cm.__exit__.assert_called_once()

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
            # T-2336: _clear_auto_delivered now takes the REAL git_mutation_lock,
            # and `patch("tools.inbox_consumer.subprocess.run")` patches the
            # GLOBAL subprocess module — so without isolation the lock's
            # `git rev-parse --git-common-dir` gets a MagicMock and mkdir()s a
            # literal `MagicMock/` tree in the repo root (the exact
            # broad-subprocess-mock poisoning _git_common_dir's docstring
            # warns about). PANTHEON_GIT_MUTATION_LOCKFILE is the documented
            # test-isolation override.
            os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = str(Path(tmp) / "git-ops.lock")
            self.addCleanup(lambda: os.environ.pop("PANTHEON_GIT_MUTATION_LOCKFILE", None))
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

    def test_clear_uses_git_direct_when_writable_no_sg_invoked(self):
        """T-3325 Defect 1 parity: `_clear_auto_delivered` shares the same
        direct-vs-sg detector as `_stamp_auto_delivered` — when the process
        can write `.git/` directly, the sg wrap must not be invoked."""
        from tools.inbox_consumer import _clear_auto_delivered, TicketInfo
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            (repo_root / ".git").mkdir(parents=True)
            tickets_root = repo_root / "tickets"
            path = _write_ticket_with_auto_delivered(
                tickets_root, "T-0001", "orchestrator", state="active",
                auto_delivered=True, auto_delivered_at="2026-06-02T04:00:00Z",
                last_transition="2026-06-02T04:00:00Z",
            )
            ticket = TicketInfo(id="T-0001", title="t", created_str="2026-06-02",
                                 depends_on=[], labels=[], body="", path=path)

            commands: list[list[str]] = []

            def capture_run(cmd, **kw):
                commands.append(cmd)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("tools._git_utils.REPO_ROOT", repo_root), \
                 patch("tools.inbox_consumer.subprocess.run", side_effect=capture_run), \
                 patch("tools.inbox_consumer.git_mutation_lock", return_value=MagicMock()), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                ok = _clear_auto_delivered(ticket)

            self.assertTrue(ok)
            self.assertTrue(commands, "expected add + commit subprocess calls")
            for cmd in commands:
                self.assertEqual(cmd[0], "git",
                                  f"sg wrap must not be invoked when .git/ is directly writable: {cmd}")

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
# T-3325: _git_direct_writable detector tests.
# ---------------------------------------------------------------------------


class TestGitDirectWritable(unittest.TestCase):
    def test_returns_true_when_git_dir_owned_and_writable(self):
        """A freshly-created, self-owned `.git/` dir is directly writable —
        the common case (an owned checkout, e.g. codex's own worktree)."""
        from tools.inbox_consumer import _git_direct_writable
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".git").mkdir()
            self.assertTrue(_git_direct_writable(repo_root))

    def test_returns_false_when_git_dir_not_writable(self):
        """Removing the write bit on `.git/` must flip the detector to False
        — the T-0514 stale-supplementary-group case looks exactly like this
        from the calling process's point of view (a real, kernel-level
        permission denial), even though `/etc/group` disagrees."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses filesystem permission checks")
        from tools.inbox_consumer import _git_direct_writable
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            git_dir = repo_root / ".git"
            git_dir.mkdir()
            os.chmod(git_dir, 0o500)
            try:
                self.assertFalse(_git_direct_writable(repo_root))
            finally:
                os.chmod(git_dir, 0o700)  # restore so tempdir cleanup can remove it


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

    def test_stamp_returns_false_on_commit_failure_fails_loud(self):
        """T-3325 Defect 2: commit/add failure → returns False (fail-loud).

        Previously this returned True ("write established local mutex; inject
        proceeds"). The frontmatter write still succeeds — this agent's own
        next-cycle scan still sees the lock locally — but the stamp never
        reached origin, so the caller must not report success.
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
            # Git leg failed → returns False (fail-loud, T-3325)
            self.assertFalse(result)
            # The local stamp write itself still succeeded, though — the
            # mutex is intact for THIS agent's own next-cycle reads.
            text = path.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
            self.assertEqual(fields.get("auto_delivered"), "true")

    def test_stamp_returns_false_on_push_failure_fails_loud(self):
        """T-3325 Defect 2: push failure (add + commit succeed) → returns False.

        Previously a push failure was logged and swallowed (returned True).
        Add/commit succeeding but push failing is exactly the "committed
        locally, HO's ff-only pull never sees it" gap the fix closes.
        """
        from tools.inbox_consumer import _stamp_auto_delivered
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            (repo_root / ".git").mkdir(parents=True)
            tickets_root = repo_root / "tickets"
            path = _write_ticket(tickets_root, "T-0001", "orchestrator", state="active")
            ticket = TicketInfo(
                id="T-0001", title="Test", created_str="2026-06-02T10:00:00Z",
                depends_on=[], labels=[], body="", path=path,
            )
            with patch("tools._git_utils.REPO_ROOT", repo_root), \
                 patch("tools.inbox_consumer.subprocess.run",
                       return_value=MagicMock(returncode=0, stdout="", stderr="")), \
                 patch("tools.inbox_consumer.git_mutation_lock", return_value=MagicMock()), \
                 patch("tools._git_utils.push_with_retry",
                       return_value=(False, "non-ff, retries exhausted")):
                result = _stamp_auto_delivered(ticket)
            self.assertFalse(result)

    def test_stamp_uses_git_direct_when_writable_no_sg_invoked(self):
        """T-3325 Defect 1: when the process can write `.git/` directly (e.g.
        codex on its own owned worktree, T-3324), the `sg pantheon-shared`
        wrap must be skipped entirely — invoking it is what broke codex
        ('Invalid password for group pantheon-shared')."""
        from tools.inbox_consumer import _stamp_auto_delivered
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            (repo_root / ".git").mkdir(parents=True)  # real, owned, writable dir
            tickets_root = repo_root / "tickets"
            path = _write_ticket(tickets_root, "T-0001", "orchestrator", state="inbox")
            ticket = TicketInfo(
                id="T-0001", title="t", created_str="2026-06-02",
                depends_on=[], labels=[], body="", path=path,
            )

            commands: list[list[str]] = []

            def capture_run(cmd, **kw):
                commands.append(cmd)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("tools._git_utils.REPO_ROOT", repo_root), \
                 patch("tools.inbox_consumer.subprocess.run", side_effect=capture_run), \
                 patch("tools.inbox_consumer.git_mutation_lock", return_value=MagicMock()), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                ok = _stamp_auto_delivered(ticket)

            self.assertTrue(ok)
            self.assertTrue(commands, "expected add + commit subprocess calls")
            for cmd in commands:
                self.assertEqual(cmd[0], "git",
                                  f"sg wrap must not be invoked when .git/ is directly writable: {cmd}")

    def test_stamp_falls_back_to_sg_when_direct_write_unavailable(self):
        """T-3325 Defect 1 fallback: when the process cannot write `.git/`
        directly (T-0514's stale-supplementary-group case), the `sg
        pantheon-shared` wrap is still used — this path must keep working
        for the case it was built for."""
        from tools.inbox_consumer import _stamp_auto_delivered
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            (repo_root / ".git").mkdir(parents=True)
            tickets_root = repo_root / "tickets"
            path = _write_ticket(tickets_root, "T-0001", "orchestrator", state="inbox")
            ticket = TicketInfo(
                id="T-0001", title="t", created_str="2026-06-02",
                depends_on=[], labels=[], body="", path=path,
            )

            commands: list[list[str]] = []

            def capture_run(cmd, **kw):
                commands.append(cmd)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("tools._git_utils.REPO_ROOT", repo_root), \
                 patch("tools.inbox_consumer._git_direct_writable", return_value=False), \
                 patch("tools.inbox_consumer.subprocess.run", side_effect=capture_run), \
                 patch("tools.inbox_consumer.git_mutation_lock", return_value=MagicMock()), \
                 patch("tools._git_utils.push_with_retry", return_value=(True, "ok")):
                ok = _stamp_auto_delivered(ticket)

            self.assertTrue(ok)
            self.assertTrue(any(cmd[0] == "sg" for cmd in commands),
                             f"expected sg-wrapped calls, got: {commands}")

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

    def test_stamp_write_failure_after_delivery_warns_and_does_not_skip(self):
        """main(): delivery happens FIRST; a later stamp-write failure warns.

        CONTRACT REVERSED BY T-2152 — this test previously asserted T-0717's
        stamp-first order (write failure → SkipCycle, NO inject) whose stated goal
        was "never deliver twice". That goal is still respected in the common case,
        but the order traded a benign failure for a sticky one:

          - OLD (stamp→deliver): a failed DELIVERY left the stamp behind, so
            check_dispatch_locked treated a poke that never arrived as in-flight and
            blocked the agent's entire queue for 8h. Fired live on 2026-07-28
            (mnemosyne / T-1825) and is silent, because dispatch_locked is
            correctly in NEVER_ALERT_SKIP_VOCABS for the genuinely-in-flight case.
          - NEW (deliver→stamp): a failed STAMP may cause a re-delivery next cycle.
            Self-healing, and guarded three ways — durable state-pointer payload
            (T-1828 R2), mailbox.send() dedupe_pending (T-1935), and ticket.py's
            claim guard refusing a second claim on a non-inbox ticket.

        So inject IS now expected to have happened, and main() must NOT report a
        skip — that would hide a delivery that really occurred.

        T-3325: main()'s exit code now DOES reflect the stamp failure (was
        always 0 regardless). The outcome-log vocab is unchanged on purpose
        (still "pulled <id>", never "skip:*") — the delivery happened and the
        log must say so — but the process exit code flips to 1 so the unit's
        OnFailure= alert fires, which is the whole point of T-3325's fix.
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
            # T-3325: a stamp failure now fails loud on the exit code.
            self.assertEqual(result, 1)
            # T-2152: delivery precedes the stamp, so inject DID happen.
            self.assertEqual(
                len(inject_called), 1,
                "delivery must occur before the stamp — a stamp failure cannot "
                "retroactively un-deliver the poke (T-2152)",
            )
            log_content = log_path.read_text()
            self.assertNotIn(
                "skip:stamp_write_failed", log_content,
                "must NOT log a skip: the agent was poked; logging a skip would "
                "hide a delivery that happened",
            )
            self.assertIn("pulled T-0002", log_content)

    def test_stamp_success_injects_and_returns_zero(self):
        """main(): stamp success (full write+add+commit+push) → inject
        happens and the process exits 0.

        T-3325: renamed from `test_stamp_commit_failure_still_injects`. That
        name and docstring described a commit-failure scenario, but the test
        body mocks `_stamp_auto_delivered` to return True directly rather
        than exercising a real commit failure — so it was always really
        testing "stamp succeeded → normal success path", which is what its
        assertions check. Since a commit/push failure now returns False (see
        `test_stamp_returns_false_on_commit_failure_fails_loud` and
        `test_stamp_write_failure_after_delivery_warns_and_does_not_skip`
        for that path), the old name+docstring were the stale part, not the
        test's behavior.
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


from tools.inbox_consumer import (  # noqa: E402 (module-tail import, matches the T-0102 block above)
    _AGENT_HEALTH_CONFIG,
)


class TestSeatRouting(unittest.TestCase):
    """T-1919: metis/atlas (ORG-043 seats) registered for inbox auto-delivery.

    Mirrors the T-0102 specialist precedent (TestArgusRouting above). Until these
    registrations landed, the seats had no inbox-consumer at all: no ~10-min
    repo-freshness pull and no ticket auto-delivery.
    """

    SEATS = {"metis": ("suborch1-main", "suborch1"), "atlas": ("suborch2-main", "suborch2")}

    # -- TMUX_TARGET_BY_AGENT (the load-bearing map for a seat's own cycle) --

    def test_seats_present_in_tmux_target_map(self) -> None:
        for seat in self.SEATS:
            with self.subTest(seat=seat):
                self.assertIn(seat, TMUX_TARGET_BY_AGENT)
                self.assertIsInstance(TMUX_TARGET_BY_AGENT[seat], str)
                self.assertTrue(len(TMUX_TARGET_BY_AGENT[seat]) > 0)

    def test_seat_tmux_target_resolves_to_real_session_not_fallback(self) -> None:
        """_resolve_tmux_target must NOT fall through to f"claude-{agent}-bot".

        The fallback yields "claude-metis-bot", which is not a live session, so
        every cycle would inject nowhere and skip tmux_unreachable.
        """
        for seat, (session, _user) in self.SEATS.items():
            with self.subTest(seat=seat):
                resolved = _resolve_tmux_target(seat)
                self.assertEqual(resolved, session)
                self.assertNotEqual(resolved, f"claude-{seat}-bot")

    def test_seat_env_var_override_wired(self) -> None:
        with patch.dict(os.environ, {"PANTHEON_METIS_TMUX_TARGET": "custom-metis-target"}):
            with patch(
                "tools.inbox_consumer.TMUX_TARGET_BY_AGENT",
                {"metis": os.environ.get("PANTHEON_METIS_TMUX_TARGET", "suborch1-main")},
            ):
                self.assertEqual(_resolve_tmux_target("metis"), "custom-metis-target")

    def test_tmux_target_is_bare_session_name_not_window_suffixed(self) -> None:
        """Regression guard for the T-0717 Recovery class: this map feeds
        send_keys_submit.sh and takes BARE session names; the ":0"-suffixed
        form belongs only in _AGENT_HEALTH_CONFIG["pane_target"]. Mixing them
        produced tmux_unreachable on every Recovery cycle."""
        for seat in self.SEATS:
            with self.subTest(seat=seat):
                self.assertNotIn(":", TMUX_TARGET_BY_AGENT[seat])

    # -- _AGENT_HEALTH_CONFIG (armed-but-unprobed until T-1940) -------------

    def test_seats_present_in_health_config_with_specialist_shape(self) -> None:
        for seat, (session, user) in self.SEATS.items():
            with self.subTest(seat=seat):
                cfg = _AGENT_HEALTH_CONFIG[seat]
                self.assertEqual(cfg["pane_target"], f"{session}:0")
                self.assertEqual(cfg["unix_user"], user)
                self.assertIn("last_alert_path", cfg)
                self.assertIn("wedge_state_path", cfg)

    def test_seats_are_not_bridge_clients(self) -> None:
        """Seats launch bare `claude` with no --channels, like specialist. True would
        emit permanent false <seat>_disconnected alerts."""
        for seat in self.SEATS:
            with self.subTest(seat=seat):
                self.assertIs(_AGENT_HEALTH_CONFIG[seat]["is_bridge_client"], False)

    def test_seats_do_not_set_alert_only(self) -> None:
        """alert_only is Recovery's no-backstop special case (T-0559), not a
        default for non-HO agents."""
        for seat in self.SEATS:
            with self.subTest(seat=seat):
                self.assertNotIn("alert_only", _AGENT_HEALTH_CONFIG[seat])

    def test_seat_state_paths_are_distinct_per_agent(self) -> None:
        """Shared state paths would let one seat's throttle/wedge state mask
        the other's."""
        paths = [
            _AGENT_HEALTH_CONFIG[s][k]
            for s in self.SEATS
            for k in ("last_alert_path", "wedge_state_path")
        ]
        self.assertEqual(len(paths), len(set(paths)))


# ---------------------------------------------------------------------------
# T-2065: the >24h active-ticket REMINDER must honour DELIVERY_MODE, not
# unconditionally tmux-inject. Before this change specialist received new-ticket
# pokes over the mailbox (T-1828 R2) but this reminder over the very
# pane-inject path R2 exists to move it off.
# ---------------------------------------------------------------------------


class TestDeliverBeforeStampOrdering(unittest.TestCase):
    """T-2152: a FAILED delivery must leave NO auto_delivered stamp. The old
    stamp-first order left a phantom-delivered ticket holding the dispatch mutex,
    blocking the agent's whole queue for 8h (live: mnemosyne/T-1825, 2026-07-28)."""

    def test_failed_delivery_leaves_no_stamp(self):
        from tools import inbox_consumer as ic
        with patch.object(ic, "deliver", side_effect=ic.SkipCycle("mailbox_unreachable")), \
             patch.object(ic, "_stamp_auto_delivered") as mock_stamp:
            with self.assertRaises(ic.SkipCycle):
                ic.deliver("mnemosyne", "pane", "poke")
        mock_stamp.assert_not_called()

    def test_stamp_happens_only_after_delivery_returns(self):
        """Ordering guard: the stamp call must not precede the deliver call."""
        from tools import inbox_consumer as ic
        import inspect
        src = inspect.getsource(ic.main)
        d = src.index("deliver(agent, tmux_target, prompt)")
        s = src.index("_stamp_auto_delivered(ticket)")
        self.assertLess(
            d, s,
            "deliver() must precede _stamp_auto_delivered() — reversing this "
            "reintroduces the phantom-delivered mutex block (T-2152)",
        )

    def test_stamp_failure_after_delivery_does_not_raise(self):
        """The agent has already been poked; raising would log it as a skip and
        hide a delivery that happened. Re-delivery next cycle is safe."""
        from tools import inbox_consumer as ic
        import inspect
        src = inspect.getsource(ic.main)
        tail = src[src.index("_stamp_auto_delivered(ticket)"):]
        self.assertNotIn(
            'raise SkipCycle("stamp_write_failed")', tail,
            "a stamp failure AFTER successful delivery must warn, not raise",
        )


class TestMnemosyneDeliveryMode(unittest.TestCase):
    """T-2146: mnemosyne is pure-ACP — her ticket pokes must go via the mailbox
    (her bridge polls it), never a tmux pane."""

    def test_mnemosyne_is_mailbox_mode(self):
        from tools.inbox_consumer import DELIVERY_MODE
        self.assertEqual(DELIVERY_MODE.get("mnemosyne"), "mailbox")

    def test_mnemosyne_poke_never_touches_tmux(self):
        with patch("tools.inbox_consumer.send_mailbox", return_value=True) as mock_mb, \
             patch("subprocess.run") as mock_run:
            deliver("mnemosyne", "claude-telegram-bot-exocortex", "poke")
        mock_mb.assert_called_once()
        send_keys = [c for c in mock_run.call_args_list
                     if len(c[0][0]) > 1 and c[0][0][1] == "send-keys"]
        self.assertEqual(
            send_keys, [],
            "a pane-inject for mnemosyne would land in the operator's own telegram "
            "surface (mailbox_wake.AGENT_PANE maps her to "
            "claude-telegram-bot-exocortex:0) — mailbox mode must return first",
        )

    def test_codex_flipped_to_mailbox(self):
        """T-2144: codex IS flipped now. The T-2142 gate — which account runs its
        consumer unit — was resolved by measurement: codex's own account carries a
        pantheon-config clone, working systemd user timers
        (pantheon-mailbox-wake@codex.timer fires every 5min) and the
        unit-failure-alert OnFailure target. Mailbox, not send-keys: codex runs its
        own supervisor and sandbox, so there is no claude pane to inject into."""
        from tools.inbox_consumer import DELIVERY_MODE
        self.assertEqual(DELIVERY_MODE.get("codex"), "mailbox")


class TestActiveSurfaceHonoursDeliveryMode(unittest.TestCase):
    def setUp(self):
        from tools.inbox_consumer import surface_active_ticket
        self.surface_active_ticket = surface_active_ticket

    @staticmethod
    def _no_tmux(cmd, **kwargs):
        """Any tmux call returns success; send-keys presence is asserted by the
        tests, so this must never itself fail the delivery path."""
        if "capture-pane" in cmd:
            return MagicMock(returncode=0, stdout="? for shortcuts", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_mailbox_mode_agent_reminder_goes_via_mailbox_not_send_keys(self):
        """specialist (DELIVERY_MODE=mailbox) must get the reminder over the mailbox
        — the whole point of the ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "specialist")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.DELIVERY_MODE", {"specialist": "mailbox"}),
                patch("tools.inbox_consumer.send_mailbox", return_value=True) as mock_mb,
                patch("subprocess.run", side_effect=self._no_tmux) as mock_run,
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("specialist", "fake-target", state_path)

            self.assertIsNotNone(result)
            self.assertIn("T-0100", result)
            mock_mb.assert_called_once()
            self.assertEqual(mock_mb.call_args[0][0], "specialist")
            self.assertIn("T-0100", mock_mb.call_args[0][1])
            send_keys = [c for c in mock_run.call_args_list
                         if len(c[0][0]) > 1 and c[0][0][1] == "send-keys"]
            self.assertEqual(
                send_keys, [],
                "a mailbox-mode agent must NOT also receive a tmux pane-inject",
            )

    def test_send_keys_agent_reminder_unchanged(self):
        """Default agents keep the pane-inject path — this change is additive."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "orchestrator")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.DELIVERY_MODE", {"specialist": "mailbox"}),
                patch("tools.inbox_consumer.send_mailbox") as mock_mb,
                patch("subprocess.run", side_effect=self._no_tmux),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("orchestrator", "fake-target", state_path)

            self.assertIsNotNone(result)
            mock_mb.assert_not_called()

    def test_mailbox_failure_fails_open_and_does_not_burn_the_rate_limit(self):
        """A failed send must return None AND leave the state file untouched —
        otherwise the 24h cooldown is consumed by a delivery that never
        arrived, silently swallowing the reminder for a full day."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "specialist")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.DELIVERY_MODE", {"specialist": "mailbox"}),
                patch("tools.inbox_consumer.send_mailbox", return_value=False),
                patch("subprocess.run", side_effect=self._no_tmux),
                patch("tools.inbox_consumer.time"),
            ):
                result = self.surface_active_ticket("specialist", "fake-target", state_path)

            self.assertIsNone(result, "delivery failure must fail open, not raise")
            self.assertFalse(
                state_path.exists(),
                "rate-limit state must NOT be written when delivery failed",
            )

    def test_reminder_payload_is_a_durable_state_pointer(self):
        """Redelivery safety (mirrors the T-1828 R2 requirement for the
        new-ticket poke): the reminder tells the agent to CHECK durable ticket
        state rather than to assume it, so replaying it is a no-op once the
        ticket has moved."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            _write_active_ticket(tickets_root, "T-0100", "specialist")
            state_path = Path(tmp) / "active_surface_log.json"

            with (
                patch("tools.inbox_consumer.TICKETS_ROOT", tickets_root),
                patch("tools.inbox_consumer.DELIVERY_MODE", {"specialist": "mailbox"}),
                patch("tools.inbox_consumer.send_mailbox", return_value=True) as mock_mb,
                patch("subprocess.run", side_effect=self._no_tmux),
                patch("tools.inbox_consumer.time"),
            ):
                self.surface_active_ticket("specialist", "fake-target", state_path)

            payload = mock_mb.call_args[0][1]
            self.assertIn("Check status", payload)
            self.assertIn("T-0100", payload)


class TestPaneIdleStateThreeState(unittest.TestCase):
    """T-2158 condition 5 / T-2157: idle vs generating vs unknown.

    The load-bearing distinction: _bottom_region_shows_live_input is TRUE for a
    generating pane (it answers 'loop alive', not 'idle'). Using it as an idle
    check would re-kick busy agents every sweep. These tests pin the split.
    """

    def setUp(self):
        from tools import inbox_consumer as _ic
        self.ic = _ic

    def test_generating_pane_is_False_not_True(self):
        pane = "some scrollback\n> working\n  esc to interrupt · manual mode on\n"
        self.assertIs(self.ic.pane_idle_state(pane), False)

    def test_generating_pane_would_pass_the_liveness_predicate(self):
        """Proves the two predicates genuinely differ — the reason this exists."""
        pane = "some scrollback\n> working\n  esc to interrupt · manual mode on\n"
        self.assertTrue(self.ic._bottom_region_shows_live_input(pane))
        self.assertIs(self.ic.pane_idle_state(pane), False)

    def test_idle_prompt_variants_are_True(self):
        for sentinel in ("? for shortcuts", "ctrl+t to hide tasks"):
            with self.subTest(sentinel=sentinel):
                self.assertIs(self.ic.pane_idle_state(f"x\n>\n  {sentinel} · manual\n"), True)

    def test_generating_wins_when_both_sentinels_present(self):
        """Precedence guard. A pane can show a task-list line AND 'esc to
        interrupt'. Generating must win: a busy agent misread as idle causes kick
        spam and false alerts, whereas an idle agent misread as busy costs one
        wasted sample. If someone reorders the checks, this fails."""
        pane = "work\n>\n  ctrl+t to hide tasks · esc to interrupt · manual mode on\n"
        self.assertIs(self.ic.pane_idle_state(pane), False)

    def test_modal_footer_is_unknown_not_idle(self):
        footer = self.ic._WEDGE_DIALOG_FOOTERS[0]
        pane = f"dialog text\n? for shortcuts\n{footer}\n"
        self.assertIsNone(self.ic.pane_idle_state(pane))

    def test_empty_or_unrecognised_is_unknown(self):
        self.assertIsNone(self.ic.pane_idle_state(""))
        self.assertIsNone(self.ic.pane_idle_state("   \n  \n"))
        self.assertIsNone(self.ic.pane_idle_state("codex-style TUI\nno known sentinel\n"))

    def test_sentinel_only_in_scrollback_does_not_set_state(self):
        """T-0716: a peer dialog or an alert quoting the sentinel must not be read
        as this pane's current state — only the bottom region counts."""
        pane = "esc to interrupt\n" + "\n".join(f"filler {i}" for i in range(20))
        self.assertIsNone(self.ic.pane_idle_state(pane))

    # --- T-3579: manage-view (background-agent/shells) → BUSY (defer, not inject) ---
    def test_manage_view_is_False_busy(self):
        """The real 13:17 atlas footer: background agent active, main loop
        idle-waiting → 'N shell · ↓ to manage', NO 'esc to interrupt'. Was None
        (waker/poker missed it → false exit-8/exit-1); now False so consumers
        DEFER. Falsifier: background-agent-working pane → defer, not exit-8."""
        pane = ("work\n"
                "  ◯ general-purpose  Inserting tests  11m · ↓ 133k tokens\n"
                "❯ \n"
                "  ⏸ manual mode on · 1 shell · ← for agents · ↓ to manage\n")
        self.assertIs(self.ic.pane_idle_state(pane), False)

    def test_generating_still_wins_over_manage_view(self):
        """Precedence: 'esc to interrupt' (generating) is checked before the
        manage-view; both → False anyway, but generating must not be masked."""
        pane = "work\n❯\n  ⏸ manual mode on · esc to interrupt · ← for agents · ↓ to manage\n"
        self.assertIs(self.ic.pane_idle_state(pane), False)

    def test_idle_composer_still_wakes_not_manage_view(self):
        """Falsifier: a genuinely idle agent composer ('? for shortcuts', NO
        '↓ to manage') must STILL classify True (wakeable) — the manage-view
        change must not swallow the idle case."""
        pane = "work\n❯\n  ⏸ manual mode on · ? for shortcuts · ← for agents\n"
        self.assertIs(self.ic.pane_idle_state(pane), True)

    def test_for_agents_alone_is_not_the_trigger(self):
        """'← for agents' appears on BOTH the idle view and the manage-view, so it
        must NOT be the busy trigger — only '↓ to manage' is. An idle view with
        '← for agents' stays True."""
        pane = "work\n❯\n  ? for shortcuts · ← for agents\n"
        self.assertIs(self.ic.pane_idle_state(pane), True)

    def test_manage_view_sentinel_only_in_scrollback_is_None(self):
        """T-0716: '↓ to manage' quoted in scrollback (not the bottom region) must
        not be read as current state."""
        pane = "↓ to manage\n" + "\n".join(f"filler {i}" for i in range(20))
        self.assertIsNone(self.ic.pane_idle_state(pane))


import tools.inbox_consumer as ic  # noqa: E402  (module handle for coverage tests)


class TestDeliveryCoverage(unittest.TestCase):
    """T-2144: a persona with no ticket delivery must be LOUD, not silent.

    codex and mnemosyne each went their entire existence without ticket delivery
    and nothing said so. The gap was invisible precisely because their MAILBOXES
    worked — agent-to-agent mail landed fine, only the ticket poke was missing.
    This test is the thing that would have said so.
    """

    def test_every_known_persona_is_classified(self):
        from tools.agent_role_map import AGENT_ROLE_MAP
        known = set(AGENT_ROLE_MAP)
        classified = set(ic.DELIVERY_MODE) | set(ic.DELIVERY_EXEMPT)
        missing = sorted(known - classified)
        self.assertEqual(
            missing, [],
            "persona(s) with NO ticket-delivery classification: %r. Add to "
            "DELIVERY_MODE (how they receive) or DELIVERY_EXEMPT (why they do "
            "not). Falling through to the send-keys default silently is how "
            "codex went undelivered for its whole existence (T-2144)." % (missing,))

    def test_no_persona_is_both_delivered_and_exempt(self):
        both = sorted(set(ic.DELIVERY_MODE) & set(ic.DELIVERY_EXEMPT))
        self.assertEqual(both, [], "contradictory classification: %r" % (both,))

    def test_delivery_modes_are_known_values(self):
        bad = {a: m for a, m in ic.DELIVERY_MODE.items()
               if m not in ("send-keys", "mailbox")}
        self.assertEqual(bad, {}, "unknown delivery mode(s): %r" % (bad,))

    def test_exemptions_carry_a_reason(self):
        for agent, reason in ic.DELIVERY_EXEMPT.items():
            with self.subTest(agent=agent):
                self.assertTrue(str(reason).strip(),
                                f"{agent} is exempt with no stated reason")

    def test_help_string_lists_every_delivered_persona(self):
        """The --agent help was stale for as long as the gaps existed."""
        import re
        src = open(ic.__file__).read()
        m = re.search(r'help=\("?Agent name \(([^)]*)\)', src)
        self.assertIsNotNone(m, "could not locate the --agent help string")
        listed = {x.strip() for x in m.group(1).split(",")}
        for agent in ic.DELIVERY_MODE:
            with self.subTest(agent=agent):
                self.assertIn(agent, listed,
                              f"{agent} has delivery but is absent from --agent help; "
                              "that surface is what makes a gap visible")



if __name__ == "__main__":
    unittest.main()
