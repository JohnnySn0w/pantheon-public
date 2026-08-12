"""Tests for tools/submit_verify.py — harness-aware submit-verify (T-1048).

Covers the pure predicate (both harness strategies), the shared verify
orchestration (success/retry/fail/send-failure paths) driven through injected
side-effects, the env-override tunables, and the CLI exit-code contract.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import submit_verify as sv  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles for the injected side-effects.                                 #
# --------------------------------------------------------------------------- #

class FakePane:
    """Models the send_text/send_enter/capture trio with a configurable pane-state
    sequence. capture() returns successive states (last repeats), so a test can
    say "first capture still pending, second is clean"."""

    def __init__(self, pane_states, *, text_ok=True, enter_ok=True):
        self._states = list(pane_states)
        self._cap_idx = 0
        self.text_ok = text_ok
        self.enter_ok = enter_ok
        self.text_calls = 0
        self.enter_calls = 0

    def send_text(self, pane, text):
        self.text_calls += 1
        return self.text_ok

    def send_enter(self, pane):
        self.enter_calls += 1
        return self.enter_ok

    def capture(self, pane):
        if not self._states:
            return ""
        idx = min(self._cap_idx, len(self._states) - 1)
        self._cap_idx += 1
        return self._states[idx]


def _no_sleep(_secs):
    return None


# --------------------------------------------------------------------------- #
# Profiles.                                                                    #
# --------------------------------------------------------------------------- #

class TestProfiles(unittest.TestCase):
    def test_known_profiles_present(self):
        self.assertIn("claude-code", sv.PROFILES)
        self.assertIn("codex", sv.PROFILES)

    def test_get_profile_returns_dict(self):
        self.assertEqual(sv.get_profile("claude-code")["pending_strategy"],
                         "composer_holds_text")
        self.assertEqual(sv.get_profile("codex")["pending_strategy"],
                         "composer_holds_text")

    def test_get_profile_unknown_raises(self):
        with self.assertRaises(ValueError):
            sv.get_profile("nope")

    def test_every_profile_has_required_fields(self):
        for name, prof in sv.PROFILES.items():
            self.assertIn("pending_strategy", prof, name)
            self.assertIn("settle_delay_s", prof, name)
            self.assertIn("submit_retries", prof, name)


# --------------------------------------------------------------------------- #
# Pending predicate — composer + paste sentinel (claude-code).                #
# --------------------------------------------------------------------------- #

class TestClaudeCodePendingPredicate(unittest.TestCase):
    def setUp(self):
        self.profile = sv.get_profile("claude-code")

    def test_pending_when_sentinel_present(self):
        pane = "some output\n[Pasted text +12 lines]\n❯ "
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_pending_when_uncollapsed_composer_holds_injected_text(self):
        text = "Drain the Pantheon mailbox and acknowledge exactly one message"
        pane = f"some output\n❯ {text}\n"
        self.assertTrue(sv.submit_pending(self.profile, pane, text))

    def test_not_pending_when_composer_cleared(self):
        pane = "some output\n❯ \n"
        self.assertFalse(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_matching_history_above_live_composer_is_not_pending(self):
        text = "Drain the Pantheon mailbox and acknowledge exactly one message"
        pane = f"❯ {text}\n● completed\n❯ \n"
        self.assertFalse(sv.submit_pending(self.profile, pane, text))

    def test_profile_carries_composer_fallback(self):
        self.assertEqual(self.profile["composer_glyph"], "❯")
        self.assertEqual(self.profile["needle_len"], 48)
        self.assertEqual(self.profile["paste_sentinel_scope"], "whole_pane")


# --------------------------------------------------------------------------- #
# Pending predicate — composer_holds_text (codex).                            #
# --------------------------------------------------------------------------- #

class TestComposerPredicate(unittest.TestCase):
    def setUp(self):
        self.profile = sv.get_profile("codex")

    def test_profile_keeps_history_safe_sentinel_scope(self):
        self.assertEqual(
            self.profile["paste_sentinel_scope"], "composer_region")

    def test_pending_when_composer_holds_text(self):
        text = "[inbound Telegram DM from the operator | chat_id=456 | message_id=7] hi there"
        pane = f"codex output\n› {text}\n"
        self.assertTrue(sv.submit_pending(self.profile, pane, text))

    def test_not_pending_when_composer_cleared(self):
        text = "[inbound Telegram DM from the operator | chat_id=456 | message_id=7] hi there"
        # After submit the composer reverts to its placeholder (no injected framing).
        pane = "codex output\n› Type a message\n"
        self.assertFalse(sv.submit_pending(self.profile, pane, text))

    def test_not_pending_when_no_composer_line(self):
        text = "something distinctive enough to exceed the needle length easily here"
        pane = "codex output with no glyph at all\n"
        self.assertFalse(sv.submit_pending(self.profile, pane, text))

    def test_pending_robust_to_reflow_whitespace(self):
        text = "[inbound Telegram DM from the operator | chat_id=456 | message_id=7] hello"
        # TUI padded/reflowed the composer line with extra spaces.
        pane = "›   [inbound   Telegram   DM   from   the operator   | chat_id=456 | message_id=7] hello"
        self.assertTrue(sv.submit_pending(self.profile, pane, text))

    def test_empty_injection_not_pending(self):
        pane = "› \n"
        self.assertFalse(sv.submit_pending(self.profile, pane, "   "))

    def test_not_pending_when_submitted_text_lingers_in_scrollback(self):
        # T-1054 regression: after submit, the injected text scrolls UP into
        # history on a glyph-prefixed line, while the LIVE composer below is the
        # placeholder. Scoping to the LAST glyph line must report "submitted";
        # an all-glyph-lines join would falsely see the scrollback copy as pending.
        text = "[inbound Telegram DM from the operator | chat_id=456 | message_id=7] hi there"
        pane = (
            "codex output\n"
            f"› {text}\n"          # our just-submitted message, now in history
            "• replied\n"
            "› Type a message\n"   # the LIVE composer: placeholder, text cleared
        )
        self.assertFalse(sv.submit_pending(self.profile, pane, text))

    def test_pending_ignores_higher_glyph_lines(self):
        # Dual of the above: still-pending must be detected on the LAST glyph
        # line even when an unrelated earlier glyph line exists in scrollback.
        text = "[inbound Telegram DM from the operator | chat_id=456 | message_id=9] do X"
        pane = (
            "› an older history line\n"
            "some output\n"
            f"› {text}\n"          # the LIVE composer still holding our text
        )
        self.assertTrue(sv.submit_pending(self.profile, pane, text))


# --------------------------------------------------------------------------- #
# Unknown strategy.                                                            #
# --------------------------------------------------------------------------- #

class TestUnknownStrategy(unittest.TestCase):
    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            sv.submit_pending({"pending_strategy": "bogus"}, "pane", "text")


# --------------------------------------------------------------------------- #
# ready_for_submission — runtime input readiness predicate (T-2382).          #
# --------------------------------------------------------------------------- #

class TestReadyForSubmission(unittest.TestCase):
    """Distinct from submit_pending: this asks "would a submit right now
    actually be accepted?", not "did my already-injected text go through?".
    Mirrors recovery_boot_kick.sh's idle-confirm conjunction (frame + idle glyph
    + not-a-modal), generalized to accept either harness's composer glyph."""

    def test_ready_claude_code_idle_prompt(self):
        pane = "some output\n╭──────────╮\n│ box │\n╰──────────╯\n❯ \n"
        self.assertTrue(sv.ready_for_submission(pane))

    def test_ready_codex_idle_prompt(self):
        pane = "codex output\n" + ("─" * 25) + "\n› \n"
        self.assertTrue(sv.ready_for_submission(pane))

    def test_not_ready_when_dialog_present(self):
        pane = (
            "some output\n╭──────────╮\n│ box │\n╰──────────╯\n"
            "Enter to confirm, Esc to cancel\n❯ \n"
        )
        self.assertFalse(sv.ready_for_submission(pane))

    def test_not_ready_when_empty(self):
        self.assertFalse(sv.ready_for_submission(""))

    def test_not_ready_when_junk_pane(self):
        self.assertFalse(sv.ready_for_submission("random junk with no frame or glyph\n"))

    def test_not_ready_when_fresh_tui_not_yet_framed(self):
        # A composer glyph can render before the TUI has finished drawing its
        # prompt-frame box — must not false-green on the glyph alone.
        pane = "starting up...\n❯ \n"
        self.assertFalse(sv.ready_for_submission(pane))

    def test_not_ready_when_frame_present_but_no_glyph(self):
        pane = "╭──────────╮\n│ box │\n╰──────────╯\nno composer glyph yet\n"
        self.assertFalse(sv.ready_for_submission(pane))


# --------------------------------------------------------------------------- #
# Verify orchestration (claude-code).                                         #
# --------------------------------------------------------------------------- #

class TestSubmitVerifyClaudeCode(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))
        self.profile["settle_delay_s"] = 0  # no real sleeping

    def _run(self, fake):
        return sv.submit_verify(
            self.profile, "p:0.0", "Hello, agent",
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep,
        )

    def test_success_first_try(self):
        fake = FakePane(["clean pane — no sentinel"])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.text_calls, 1)
        self.assertEqual(fake.enter_calls, 1)

    def test_success_on_retry(self):
        fake = FakePane(["[Pasted text — still pending", "clean pane"])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)  # initial + one retry

    def test_fail_after_retries(self):
        fake = FakePane(["[Pasted text — stuck"])  # always pending
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("unconfirmed", r.detail)
        # retries=2 -> attempts 0,1,2 = 3 Enters
        self.assertEqual(fake.enter_calls, 3)

    def test_send_text_failure_no_enter(self):
        fake = FakePane(["clean"], text_ok=False)
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("text", r.detail)
        self.assertEqual(fake.enter_calls, 0)

    def test_send_enter_failure(self):
        fake = FakePane(["clean"], enter_ok=False)
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("Enter", r.detail)
        self.assertEqual(fake.text_calls, 1)
        self.assertEqual(fake.enter_calls, 1)


# --------------------------------------------------------------------------- #
# Verify orchestration (codex) — same algorithm, different predicate.         #
# --------------------------------------------------------------------------- #

class TestSubmitVerifyCodex(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("codex"))
        self.profile["settle_delay_s"] = 0

    def test_codex_success_when_composer_clears(self):
        text = "[inbound Telegram DM from the operator | chat_id=9 | message_id=1] yo"
        fake = FakePane([f"› {text}", "› Type a message"])  # pending, then cleared
        r = sv.submit_verify(
            self.profile, "codex-session", text,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep,
        )
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)

    def test_codex_fail_when_composer_never_clears(self):
        text = "[inbound Telegram DM from the operator | chat_id=9 | message_id=1] yo"
        fake = FakePane([f"› {text}"])  # composer always holds the text
        r = sv.submit_verify(
            self.profile, "codex-session", text,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep,
        )
        self.assertFalse(r.ok)
        self.assertIn("unconfirmed", r.detail)


class TestCodexCollapsedPaste(unittest.TestCase):
    """T-2408: large pastes collapse to "[Pasted Content N chars]" and REPLACE
    the composer glyph line — the needle check goes blind exactly then.

    The fixture is TONIGHT'S REAL PANE (2026-08-02 10:57Z), reflow and all —
    two collapsed kicks sat at an idle prompt for hours while submit-verify had
    reported both submitted. A 4.5h comms gap, ended by one manual Enter.
    """

    # Verbatim shape from the live capture: no '›' line anywhere, the collapsed
    # marker fused with reflowed tail text, status bar at rest.
    REAL_PANE_PENDING = (
        "  current turn first.\n"
        "  [Pasted Content 1012 chars]e/codex/pantheon-config/tools'); import mailbox; "
        "print(mailbox.consume('/home/the-ancestor/mailbox/codex', lambda m: ...\n"
        "  ORG-026 Path A). Run it bare — your session holds the pantheon-mailbox group natively\n"
        "  [Pasted Content 1012 chars] #2e/codex/pantheon-config/tools'); import mailbox; ...\n"
        "\n"
        "  gpt-5.6-sol xhigh fast · ~ · Main [default]\n"
    )
    # Post-submit shape (also measured): marker gone, placeholder composer back.
    REAL_PANE_SUBMITTED = (
        "\u25e6 Working (0s \u2022 esc to interrupt)\n"
        "\n"
        "\u203a Run /review on my current changes\n"
        "\n"
        "  gpt-5.6-sol xhigh fast · ~ · Main [default]\n"
    )
    WAKE_TEXT = ("You have 4 unread inter-agent messages in your mailbox (from "
                 "mail_staleness, orchestrator). QUEUED, oldest first ...")

    def setUp(self):
        self.profile = sv.get_profile("codex")

    def test_collapsed_paste_reads_PENDING(self):
        # The defect: this read False ("submitted") while the paste sat unsent.
        self.assertTrue(sv.submit_pending(
            self.profile, self.REAL_PANE_PENDING, self.WAKE_TEXT))

    def test_submitted_pane_reads_not_pending(self):
        # And the guard must not over-fire: post-submit, placeholder composer,
        # no marker → not pending. A false PENDING here retries Enter into a
        # composer whose placeholder suggestion an Enter might RUN.
        self.assertFalse(sv.submit_pending(
            self.profile, self.REAL_PANE_SUBMITTED, self.WAKE_TEXT))

    def test_marker_in_history_above_live_composer_is_not_pending(self):
        # T-1054's scroll-up hazard applied to the sentinel: a lingering marker
        # ABOVE a live '›' composer line is history, not a pending paste.
        pane = ("  [Pasted Content 900 chars] old submitted thing\n"
                "\u203a Type a message\n"
                "  status\n")
        self.assertFalse(sv.submit_pending(self.profile, pane, self.WAKE_TEXT))

    def test_marker_ON_the_composer_line_is_pending(self):
        """T-2408 second specimen, measured ~1h after the first fix landed: the
        collapsed block can render ON the '›' composer line itself. The first
        fix scoped the sentinel to lines AFTER the last glyph line and would
        have false-greened this shape. One specimen is provenance, not
        coverage."""
        pane = ("\u203a [Pasted Content 1020 chars], the hub must be in your "
                "config.toml [sandbox_workspace_write] writable_roots.)\n"
                "  first.\n"
                "\n"
                "  gpt-5.6-sol xhigh fast · ~ · Main [default]\n")
        self.assertTrue(sv.submit_pending(self.profile, pane, self.WAKE_TEXT))

    def test_short_injection_needle_path_unchanged(self):
        # Short texts never collapse; the composer-needle path must still work.
        text = "[inbound Telegram DM from the operator | chat_id=9] yo"
        self.assertTrue(sv.submit_pending(
            self.profile, f"\u203a {text}\n  status\n", text))
        self.assertFalse(sv.submit_pending(
            self.profile, "\u203a Type a message\n  status\n", text))


# --------------------------------------------------------------------------- #
# Env overrides.                                                              #
# --------------------------------------------------------------------------- #

class TestEnvOverrides(unittest.TestCase):
    def test_env_overrides_applied(self):
        with mock.patch.dict(os.environ,
                             {"INJECT_ENTER_DELAY": "0.01", "SUBMIT_RETRIES": "5"}):
            prof = sv._profile_with_env_overrides("claude-code")
        self.assertEqual(prof["settle_delay_s"], 0.01)
        self.assertEqual(prof["submit_retries"], 5)

    def test_env_overrides_ignore_garbage(self):
        with mock.patch.dict(os.environ,
                             {"INJECT_ENTER_DELAY": "abc", "SUBMIT_RETRIES": "x"}):
            prof = sv._profile_with_env_overrides("codex")
        # falls back to profile defaults, no crash
        self.assertEqual(prof["settle_delay_s"], sv.DEFAULT_SETTLE_DELAY_S)
        self.assertEqual(prof["submit_retries"], sv.DEFAULT_SUBMIT_RETRIES)

    def test_no_env_uses_defaults(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("INJECT_ENTER_DELAY", "SUBMIT_RETRIES")}
        with mock.patch.dict(os.environ, clean, clear=True):
            prof = sv._profile_with_env_overrides("claude-code")
        self.assertEqual(prof["settle_delay_s"], sv.DEFAULT_SETTLE_DELAY_S)


# --------------------------------------------------------------------------- #
# CLI exit-code contract.                                                      #
# --------------------------------------------------------------------------- #

class TestCLI(unittest.TestCase):
    def test_cli_success_returns_0(self):
        with mock.patch.object(sv, "tmux_send_text", return_value=True), \
             mock.patch.object(sv, "tmux_send_enter", return_value=True), \
             mock.patch.object(sv, "tmux_capture", return_value="clean pane"), \
             mock.patch.object(sv.time, "sleep", _no_sleep), \
             mock.patch.dict(os.environ, {"INJECT_ENTER_DELAY": "0"}):
            rc = sv.main(["--harness", "claude-code", "--", "p:0.0", "hello"])
        self.assertEqual(rc, 0)

    def test_cli_unconfirmed_returns_1(self):
        with mock.patch.object(sv, "tmux_send_text", return_value=True), \
             mock.patch.object(sv, "tmux_send_enter", return_value=True), \
             mock.patch.object(sv, "tmux_capture", return_value="[Pasted text stuck"), \
             mock.patch.object(sv.time, "sleep", _no_sleep), \
             mock.patch.dict(os.environ, {"INJECT_ENTER_DELAY": "0"}):
            rc = sv.main(["--harness", "claude-code", "--", "p:0.0", "hello"])
        self.assertEqual(rc, 1)

    def test_cli_send_text_failure_returns_1(self):
        with mock.patch.object(sv, "tmux_send_text", return_value=False), \
             mock.patch.object(sv, "tmux_send_enter", return_value=True), \
             mock.patch.object(sv, "tmux_capture", return_value="clean"), \
             mock.patch.dict(os.environ, {"INJECT_ENTER_DELAY": "0"}):
            rc = sv.main(["--harness", "claude-code", "--", "p:0.0", "hello"])
        self.assertEqual(rc, 1)

    def test_cli_rejects_unknown_harness(self):
        with self.assertRaises(SystemExit):
            sv.main(["--harness", "bogus", "--", "p:0.0", "hello"])


# --------------------------------------------------------------------------- #
# CLI --pending-check mode (the seam send_keys_submit.sh actually uses).        #
# --------------------------------------------------------------------------- #

class TestPendingCheckCLI(unittest.TestCase):
    """Predicate-only mode: pane text on stdin -> 'pending'/'submitted' on stdout,
    exit 0. This is the watchdog-safe seam (no tmux I/O in python)."""

    def _run(self, argv, stdin_text):
        import io
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = sv.main(argv, stdin=io.StringIO(stdin_text))
        return rc, out.getvalue().strip()

    def test_claude_code_pending(self):
        rc, verdict = self._run(
            ["--harness", "claude-code", "--pending-check", "--text", "hi"],
            "output\n[Pasted text +3 lines]\n❯ ")
        self.assertEqual(rc, 0)
        self.assertEqual(verdict, "pending")

    def test_claude_code_submitted(self):
        rc, verdict = self._run(
            ["--harness", "claude-code", "--pending-check", "--text", "hi"],
            "output\n❯ \n")
        self.assertEqual(rc, 0)
        self.assertEqual(verdict, "submitted")

    def test_codex_pending_uses_text(self):
        text = "[inbound Telegram DM from the operator | chat_id=5 | message_id=2] yo"
        rc, verdict = self._run(
            ["--harness", "codex", "--pending-check", "--text", text],
            f"› {text}\n")
        self.assertEqual(rc, 0)
        self.assertEqual(verdict, "pending")

    def test_codex_submitted_after_clear(self):
        text = "[inbound Telegram DM from the operator | chat_id=5 | message_id=2] yo"
        rc, verdict = self._run(
            ["--harness", "codex", "--pending-check", "--text", text],
            "› Type a message\n")
        self.assertEqual(rc, 0)
        self.assertEqual(verdict, "submitted")

    def test_empty_pane_is_submitted_claude_code(self):
        # Matches the legacy bash behavior: an empty capture has no sentinel.
        rc, verdict = self._run(
            ["--harness", "claude-code", "--pending-check", "--text", "hi"], "")
        self.assertEqual(rc, 0)
        self.assertEqual(verdict, "submitted")


if __name__ == "__main__":
    unittest.main()
