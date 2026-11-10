"""Tests for tools/submit_verify.py — harness-aware submit-verify (T-1048).

Covers the pure predicate (both harness strategies), the shared verify
orchestration (success/retry/fail/send-failure paths) driven through injected
side-effects, the env-override tunables, and the CLI exit-code contract.
"""
from __future__ import annotations

import io
import os
import sys
import unittest
import unittest.mock
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
        # T-4293: the placeholder is text IN the composer (it used to sit above "❯"
        # here, a synthetic layout the live TUI never draws).
        pane = "some output\n" + "─" * 60 + "\n❯ [Pasted text #1 +12 lines]\n" + "─" * 60 + "\n"
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    # --- T-4293: an OLD marker in scrollback is not our paste ---
    _RULE = "─" * 60
    _FOOTER = "  ⏸ manual mode on · ← for agents"

    def test_stale_marker_above_an_empty_composer_is_not_pending(self):
        """The T-4293 case: an earlier wake's marker is still on screen in the
        transcript. Whole-pane read it as our paste unsubmitted: false rc 2/8, the
        wake marker not advanced, a duplicate kick plus an OnFailure page."""
        pane = (f"❯ [Pasted text #3 +3 lines]\n● Done. Mail drained.\n✻ Worked for 19s\n"
                f"{self._RULE}\n❯ \n{self._RULE}\n{self._FOOTER}\n")
        self.assertFalse(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_stale_marker_above_a_composer_holding_our_pending_paste(self):
        pane = (f"❯ [Pasted text #3 +3 lines]\n● Done.\n"
                f"{self._RULE}\n❯ [Pasted text #4 +3 lines]\n{self._RULE}\n{self._FOOTER}\n")
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_marker_on_a_wrapped_composer_line_is_pending(self):
        pane = (f"● earlier\n{self._RULE}\n❯ {'x' * 70}\n  [Pasted text #2 +5 lines]\n"
                f"{self._RULE}\n{self._FOOTER}\n")
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_marker_split_across_a_wrap_is_pending(self):
        pane = (f"{self._RULE}\n❯ {'x' * 70} [Pasted\n  text #2 +5 lines]\n"
                f"{self._RULE}\n{self._FOOTER}\n")
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_marker_below_the_composer_border_is_not_pending(self):
        pane = (f"{self._RULE}\n❯ \n{self._RULE}\n"
                "  ⏸ manual mode on · ← for agents · ↓ to manage\n  ● main\n"
                "  ◯ impl-t4293  Grep '[Pasted text' in submit_verify   2m · ↓ 9.1k tokens\n")
        self.assertFalse(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_real_rendered_pending_paste_is_pending(self):
        """T-4319: artisan's rendered pending paste (tools/testdata): the placeholder sits
        IN the composer region (after the last line starting with the glyph), as T-4293
        scoped it."""
        pane = (Path(__file__).resolve().parent / "testdata"
                / "artisan_pending_paste_composer_rendered.txt").read_text(encoding="utf-8")
        self.assertTrue(sv.submit_pending(self.profile, pane, "irrelevant"))

    def test_no_visible_composer_falls_back_to_the_whole_pane(self):
        """Fail closed as before when no composer line is visible (a modal, or the
        old boxed frame, where the line starts with '│')."""
        for pane in ("[Pasted text #1 +4 lines]\n Esc to cancel · Tab to amend\n",
                     "╭────╮\n│ ❯  │\n╰────╯\n[Pasted text #1 +4 lines]\n"):
            with self.subTest(pane=pane):
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

    # T-4141 (artisan, 2026-09-28): an earlier poke sat UNSUBMITTED in the composer, and
    # the relaunch prompt was pasted onto the end of it. The ❯ line began with the
    # stale text, so our text sat on WRAPPED continuation lines (no glyph). Checking
    # only the glyph line false-passed the relaunch prompt as delivered.
    _STALE = ("[activity-monitor P1.b] You appear to be in self-imposed wait-state while "
              "the operator is away (>15min since the last inbound). Per sop/standard/x.md:")
    _OURS = ("[ctxfill graceful relaunch] Please write a fresh /home/x/HANDOFF.md "
             "capturing your live state NOW")

    def test_pending_when_text_appended_to_stale_composer_text(self):
        pane = ("● earlier output\n" + "─" * 60 + "\n"
                f"❯ {self._STALE[:70]}\n  {self._STALE[70:]} {self._OURS[:40]}\n"
                f"  {self._OURS[40:]}\n" + "─" * 60 + "\n  ⏸ manual mode on\n")
        self.assertTrue(sv.submit_pending(self.profile, pane, self._OURS))

    def test_pending_when_the_needle_wraps_mid_word(self):
        pane = f"❯ {self._STALE} {self._OURS[:20]}\n  {self._OURS[20:]}\n"
        self.assertTrue(sv.submit_pending(self.profile, pane, self._OURS))

    def test_submitted_text_in_history_above_an_empty_wrapped_composer(self):
        pane = (f"> {self._OURS}\n● working on it\n" + "─" * 60 + "\n❯ \n"
                + "─" * 60 + "\n  ⏸ manual mode on\n")
        self.assertFalse(sv.submit_pending(self.profile, pane, self._OURS))

    def test_text_echoed_below_the_composer_border_is_not_pending(self):
        # The live-kick stand-in (bringup_acceptance) reads a tty that ECHOES input
        # below its bottom border and leaves it there after submit.
        pane = "╭───╮\n❯ \n" + "─" * 25 + f"\n{self._OURS}\n"
        self.assertFalse(sv.submit_pending(self.profile, pane, self._OURS))

    # T-4141 should_repress: one more Enter, only when it would submit OUR text.
    def _composer(self, footer):
        return ("● earlier output\n" + "─" * 60 + "\n"
                f"❯ {self._STALE[:70]}\n  {self._STALE[70:]} {self._OURS}\n"
                + "─" * 60 + f"\n  {footer}\n")

    def test_repress_when_our_text_is_stuck_under_a_shells_only_footer(self):
        pane = self._composer("⏸ manual mode on · 2 shells · ↓ to manage")
        self.assertTrue(sv.should_repress(self.profile, pane, self._OURS))

    def test_hold_while_generating(self):
        pane = self._composer("✻ Working… (12s · esc to interrupt)")
        self.assertFalse(sv.should_repress(self.profile, pane, self._OURS))

    def test_hold_while_a_background_agent_runs(self):
        pane = self._composer("⏸ manual mode on · ↓ to manage") + \
            "  ◯ impl-4135  Polling for pytest smoke completion … 17m 10s · ↓ 235.6k tokens\n"
        self.assertFalse(sv.should_repress(self.profile, pane, self._OURS))

    def test_hold_on_a_modal(self):
        pane = self._composer("Enter to confirm · Esc to cancel")
        self.assertFalse(sv.should_repress(self.profile, pane, self._OURS))

    def test_hold_when_our_text_is_not_in_the_composer(self):
        # Sentinel-only pending (collapsed paste / stale marker) is NOT enough:
        # an Enter on an empty composer could run a ghost suggestion.
        pane = "─" * 60 + "\n❯ [Pasted text #1 +4 lines]\n" + "─" * 60 + "\n  ? for shortcuts\n"
        self.assertTrue(sv.submit_pending(self.profile, pane, self._OURS))
        self.assertFalse(sv.should_repress(self.profile, pane, self._OURS))

    def test_codex_never_represses(self):
        pane = f"› {self._OURS}\n"
        self.assertFalse(sv.should_repress(sv.get_profile("codex"), pane, self._OURS))

    def test_repress_check_cli(self):
        import io
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = sv.main(["--harness", "claude-code", "--repress-check", "--text", self._OURS],
                         stdin=io.StringIO(self._composer("? for shortcuts")))
        self.assertEqual((rc, out.getvalue().strip()), (0, "repress"))

    def test_profile_carries_composer_fallback(self):
        self.assertEqual(self.profile["composer_glyph"], "❯")
        self.assertEqual(self.profile["needle_len"], 48)
        self.assertEqual(self.profile["paste_sentinel_scope"], "composer_box")


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


class TestHermesBusyComposerPredicate(unittest.TestCase):
    TEXT = "Resume the queued recovery check and report its result."

    def test_busy_status_prefixed_composer_keeps_pending_text(self):
        profile = sv.get_profile("hermes")
        pane = f"turn output\n☤ ❯ {self.TEXT}\n⏱ 12s\n"
        self.assertTrue(
            sv.submit_pending(profile, pane, self.TEXT),
            "pending text in the measured busy composer must not read submitted",
        )

    def test_prefix_allowance_is_hermes_only(self):
        claude = sv.get_profile("claude-code")
        codex = sv.get_profile("codex")
        self.assertFalse(sv.submit_pending(claude, f"☤ ❯ {self.TEXT}\n", self.TEXT))
        self.assertFalse(sv.submit_pending(codex, f"☤ › {self.TEXT}\n", self.TEXT))
        self.assertTrue(sv.submit_pending(claude, f"❯ {self.TEXT}\n", self.TEXT))
        self.assertTrue(sv.submit_pending(codex, f"› {self.TEXT}\n", self.TEXT))


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

    def test_not_ready_reason_names_the_first_failing_condition(self):
        # T-4362: the label a never-proven probe records for a held re-type.
        # It must agree with ready_for_submission on every pane.
        frame = "╭──────────╮\n│ box │\n╰──────────╯\n"
        cases = (
            ("", "empty"),
            ("starting up...\n❯ \n", "no_frame"),
            # T-4376 (rev-t4362 M11): a pane failing BOTH frame and glyph names the
            # frame; swapping the two checks would name the glyph.
            ("random junk with neither\n", "no_frame"),
            (frame + "no composer glyph yet\n", "no_composer_glyph"),
            (frame + "Enter to confirm · Esc to cancel\n❯ 1. Yes\n", "dialog_footer"),
            ("Esc to cancel\n❯ \n", "no_frame"),
            ("some output\n" + frame + "❯ \n", None),
            ("codex output\n" + ("─" * 25) + "\n› \n", None),
        )
        for pane, reason in cases:
            with self.subTest(pane=pane):
                self.assertEqual(sv.not_ready_reason(pane), reason)
                self.assertEqual(sv.ready_for_submission(pane), reason is None)


# --------------------------------------------------------------------------- #
# Verify orchestration (claude-code).                                         #
# --------------------------------------------------------------------------- #

class TestBusyReason(unittest.TestCase):
    """T-4459: the frame and ❯ are drawn mid-turn, so readiness alone passes a busy
    pane. busy_reason reads the bottom region the way should_repress does."""

    _FRAME = "╭────────────────────────────╮\n❯ \n╰────────────────────────────╯"

    def test_idle_pane_is_not_busy(self):
        self.assertIsNone(sv.busy_reason(self._FRAME + "\n  ? for shortcuts"))

    def test_generating_footer_is_busy(self):
        pane = "✻ Cogitating… (4s · esc to interrupt)\n" + self._FRAME
        self.assertTrue(sv.ready_for_submission(pane), "the attempt-4 shape passes readiness")
        self.assertEqual(sv.busy_reason(pane), "generating")

    def test_background_agents_are_busy(self):
        self.assertEqual(
            sv.busy_reason(self._FRAME + "\n  Waiting for 2 background agents"),
            "background_agents",
        )

    def test_dialog_footer_is_busy(self):
        self.assertEqual(
            sv.busy_reason(self._FRAME + "\nEnter to confirm · Esc to cancel"),
            "dialog_footer",
        )

    def test_unloadable_sentinels_read_as_busy_never_idle(self):
        with mock.patch.object(sv, "_inbox_consumer", side_effect=ImportError("gone")):
            self.assertEqual(sv.busy_reason(self._FRAME), "unclassifiable")

    def _ready_cli(self, pane: str) -> tuple[int, str]:
        import io
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = sv.main(["--harness", "claude-code", "--ready-check"], stdin=io.StringIO(pane))
        return rc, out.getvalue().strip()

    def test_ready_check_cli_verdicts(self):
        self.assertEqual(self._ready_cli(self._FRAME), (0, "ready"))
        self.assertEqual(
            self._ready_cli("✻ Cogitating… (esc to interrupt)\n" + self._FRAME),
            (0, "busy:generating"),
        )
        self.assertEqual(self._ready_cli("booting..."), (0, "not_ready:no_frame"))


class TestEnterRefusal(unittest.TestCase):
    """T-4832: the pre-Enter re-check reads the bottom region only."""

    PERMISSION = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend\n"
    PLAN = (" Would you like to proceed?\n ❯ 1. Yes, and auto-accept edits\n"
            "   2. Yes, manually approve edits\n   3. No, keep planning\n"
            "   4. Tell Claude what to change\n")

    def test_permission_dialog_at_the_bottom_refuses(self):
        self.assertEqual(sv.enter_refusal(self.PERMISSION), "dialog")

    def test_plan_dialog_at_the_bottom_refuses(self):
        self.assertEqual(sv.enter_refusal(self.PLAN), "dialog")

    def test_dialog_words_above_the_bottom_region_do_not_refuse(self):
        prose = "● quoted: No, keep planning / Esc to cancel\n" + "".join(
            f"  line {i}\n" for i in range(sv._ENTER_CHECK_BOTTOM_LINES)) + "❯ hello\n"
        self.assertIsNone(sv.enter_refusal(prose))

    def test_plan_phrase_outside_an_option_row_does_not_refuse(self):
        # In the bottom region, but prose or a quoted string, not the dialog's option row.
        prose = '  assertIn("No, keep planning", out)\n● The plan dialog offers No, keep planning.\n❯ hi\n'
        self.assertIsNone(sv.enter_refusal(prose))

    def test_composer_and_empty_capture_do_not_refuse(self):
        self.assertIsNone(sv.enter_refusal("❯ hello\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"))
        self.assertIsNone(sv.enter_refusal(""))

    def test_cli_mode(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = sv.main(["--harness", "claude-code", "--enter-check"], stdin=io.StringIO(self.PLAN))
        self.assertEqual((rc, out.getvalue()), (0, "refuse:dialog\n"))


class TestAutoModeNudge(unittest.TestCase):
    """T-4646: Claude Code 2.1.289's startup prompt, from specialist's pane at 08:49Z on
    2026-10-05 (blank lines dropped). It has the frame run and a ❯ but no confirm
    footer, so readiness passed and send_keys_submit's fallback Enter picked "Yes"."""

    NUDGE = (
        "─" * 80 + "\n"
        " Make auto mode your default permission mode?\n"
        " Auto mode lets Claude handle permission prompts automatically. Claude checks each\n"
        " tool call for risky actions and prompt injection before executing, runs the ones it\n"
        " assesses as lower-risk, and blocks the rest.\n"
        " ❯ Yes, set auto mode as my default permission mode\n"
        "   No, keep manual\n"
    )
    # Some other option list with no footer at all: not a known dialog, so the paste
    # may go ahead, but after the render wait the missing composer footer stops the Enter.
    OPTION_LIST = "─" * 80 + "\n Pick one\n ❯ 1. Continue\n   2. Stop\n"
    COMPOSER = "─" * 80 + "\n❯ \n" + "─" * 80 + "\n"

    def test_nudge_is_not_ready(self):
        self.assertEqual(sv.not_ready_reason(self.NUDGE), "dialog_footer")

    def test_nudge_refuses_the_paste_and_the_enter(self):
        self.assertEqual(sv.paste_refusal(self.NUDGE), "dialog")
        self.assertEqual(sv.enter_anyway_refusal(self.NUDGE, "claude-code"), "dialog")

    def test_option_list_without_footer_refuses_only_the_enter(self):
        self.assertIsNone(sv.paste_refusal(self.OPTION_LIST))
        self.assertEqual(sv.enter_anyway_refusal(self.OPTION_LIST, "claude-code"),
                         "no_composer_footer")

    def test_composer_footers_pass(self):
        for footer in ("  ⏸ manual mode on (shift+tab to cycle)",
                       "  ⏸ manual mode on · ← for agents",
                       "  ⏵⏵ accept edits on (shift+tab to cycle)",
                       "  ? for shortcuts",
                       "✻ Cogitating… (4s · esc to interrupt)"):
            with self.subTest(footer=footer):
                self.assertIsNone(sv.enter_anyway_refusal(self.COMPOSER + footer, "claude-code"))
                self.assertIsNone(sv.paste_refusal(self.COMPOSER + footer))

    def test_footer_check_is_claude_code_only(self):
        self.assertIsNone(sv.enter_anyway_refusal("codex output\n" + "─" * 25 + "\n› \n", "codex"))

    def test_empty_capture_is_no_evidence(self):
        for pane in ("", "   \n\n"):
            self.assertIsNone(sv.paste_refusal(pane))
            self.assertIsNone(sv.enter_anyway_refusal(pane, "claude-code"))

    def _cli(self, flag: str, pane: str) -> tuple[int, str]:
        import io
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            rc = sv.main(["--harness", "claude-code", flag], stdin=io.StringIO(pane))
        return rc, out.getvalue().strip()

    def test_cli_verdicts(self):
        self.assertEqual(self._cli("--paste-check", self.NUDGE), (0, "refuse:dialog"))
        self.assertEqual(self._cli("--paste-check", self.OPTION_LIST), (0, "ok"))
        self.assertEqual(self._cli("--enter-anyway-check", self.OPTION_LIST),
                         (0, "refuse:no_composer_footer"))
        self.assertEqual(self._cli("--enter-anyway-check", self.COMPOSER + "  ? for shortcuts"),
                         (0, "ok"))


class TestSubmitVerifyClaudeCode(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))
        self.profile.pop("render_wait_s")  # exercise the post-Enter algorithm in isolation
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
        fake = FakePane(["[Pasted text — still pending", "[Pasted text — still pending", "clean pane"])  # post-Enter, pre-retry (T-5380 r2), post-retry
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
        # T-6130 gate: pre-paste capture (blank), render gate (pending); then
        # post-Enter, pre-retry (T-5380 r2), cleared.
        fake = FakePane(["› Type a message", f"› {text}", f"› {text}", f"› {text}", "› Type a message"])
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
             mock.patch.object(sv, "tmux_capture",
                               side_effect=["❯ hello", "clean pane", "clean pane"]), \
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
            "output\n❯ [Pasted text #1 +3 lines]\n")  # T-4293: the marker is IN the composer
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


# --------------------------------------------------------------------------- #
# T-5380: head-scrolled composer. A long single-line paste scrolls its head out #
# of the composer's visible rows; the verifier must still see it as pending.    #
# Fixtures are the real captures from recovery's 2026-10-22 strands (T-5148 after   #
# "Compacted while idle", T-5221 after a plain turn end), prompts rebuilt from  #
# inbox_consumer.INJECT_PROMPT.                                                 #
# --------------------------------------------------------------------------- #

_RULE = "\u2500" * 80
_T5148_BODY = (
    "Replace or explicitly correct the three new unreachable citations reported on "
    "2026-10-18: 5709d4ef8 in T-2518, 4d96be980 in T-4506, and 052ad9ed1 in T-4988. "
    "Resolve each against the landed/rebased commit and rerun tools/cited_hash_lint.py. "
    "Do not baseline these new findings.")
_T5221_BODY = (
    "ORG-076 repair episode ep-org076-store-liveness-20261019T130708Z-70d1c779: "
    "cycle-rate-low (broken) on org076-store-liveness. Measurement succeeded and found "
    "a bad state (broken): open the playbook for this fault_id. Route: other_harness -> "
    "watchman. Attempts budget 2.")


def _inject_prompt(tid, title, body):
    import inbox_consumer as ic
    return (ic.INJECT_PROMPT.replace("T-NNNN", tid).replace("<title>", title)
            .replace("<agent>", "recovery").replace("<body>", body))


_PROMPT_5148 = _inject_prompt("T-5148", "cited hash lint fixes", _T5148_BODY)
_PROMPT_5221 = _inject_prompt("T-5221", "ORG-076 repair episode", _T5221_BODY)

_PANE_5148_STUCK = "\n".join([
    "  \u23bf  Skills restored (pantheon-sop-adr-buildout-planning)",
    "\u25cf Compacted while idle, before the prompt cache expired",
    "                                        \u2714 Update installed \u00b7 Restart to update",
    _RULE,
    "\u276f auto-approve scope, surface to the operator via the Telegram bridge and halt",
    "  rather than proceeding. Otherwise: claim via ticket.py, work the Ask,",
    "  resolve when complete. Body follows: Replace or explicitly correct the three",
    "  new unreachable citations reported on 2026-10-18: 5709d4ef8 in T-2518,",
    "  4d96be980 in T-4506, and 052ad9ed1 in T-4988. Resolve each against the",
    "  landed/rebased commit and rerun tools/cited_hash_lint.py. Do not baseline",
    "  these new findings..",
    _RULE,
    "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
])
_PANE_5221_STUCK = "\n".join([
    "\u273b Saut\u00e9ed for 37s \u00b7 done 14:45",
    "  new task? /clear to save 347.4k to\u2026 \u00b7 \u2714 Update installed \u00b7 Restart to update",
    _RULE,
    "\u276f anything outside auto-approve scope, surface to the operator via the Telegram",
    "  bridge and halt rather than proceeding. Otherwise: claim via ticket.py, work",
    "  the Ask, resolve when complete. Body follows: ORG-076 repair episode",
    "  ep-org076-store-liveness-20261019T130708Z-70d1c779: cycle-rate-low (broken)",
    "  on org076-store-liveness. Measurement succeeded and found a bad state",
    "  (broken): open the playbook for this fault_id. Route: other_harness ->",
    "  watchman. Attempts budget 2..",
    _RULE,
    "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
])


def _submitted_pane(prompt_lines):
    """Prompt echoed into history above an EMPTY composer: a real submit."""
    return "\n".join(["\u276f " + prompt_lines[0], *("  " + l for l in prompt_lines[1:]),
                       "\u25cf On it.", "", _RULE, "\u276f ", _RULE,
                       "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"])


class TestHeadScrolledComposer(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))
        self.profile.pop("render_wait_s")  # exercise the post-Enter algorithm in isolation
        self.profile["settle_delay_s"] = 0

    def _run(self, fake, prompt):
        return sv.submit_verify(
            self.profile, "p:0.0", prompt,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep)

    def test_compacted_while_idle_capture_is_pending(self):
        self.assertTrue(sv.submit_pending(self.profile, _PANE_5148_STUCK, _PROMPT_5148))

    def test_plain_turn_end_capture_is_pending(self):
        self.assertTrue(sv.submit_pending(self.profile, _PANE_5221_STUCK, _PROMPT_5221))

    def test_enter_swallowed_then_retry_lands(self):
        fake = FakePane([_PANE_5148_STUCK, _PANE_5148_STUCK, _submitted_pane(["done"])])
        r = self._run(fake, _PROMPT_5148)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(fake.enter_calls, 2)

    def test_every_enter_swallowed_reports_unconfirmed(self):
        fake = FakePane([_PANE_5221_STUCK])
        r = self._run(fake, _PROMPT_5221)
        self.assertFalse(r.ok)
        self.assertEqual(fake.enter_calls, 3)  # initial + 2 retries, then give up
        self.assertIn("unconfirmed", r.detail)

    def test_already_submitted_sends_exactly_one_enter(self):
        # The extra-Enter hazard: a submitted prompt (in history, empty composer) must
        # not read as pending, so no retry Enter goes into a live seat.
        fake = FakePane([_submitted_pane(_PROMPT_5148.split(". "))])
        r = self._run(fake, _PROMPT_5148)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 1)

    def test_tail_in_history_above_empty_composer_is_not_pending(self):
        pane = _submitted_pane([_PROMPT_5221[-120:]])
        self.assertFalse(sv.submit_pending(self.profile, pane, _PROMPT_5221))

    def test_tail_below_composer_border_is_not_pending(self):
        pane = "\n".join(["\u276f ", _RULE, "  " + _PROMPT_5221[-60:]])
        self.assertFalse(sv.submit_pending(self.profile, pane, _PROMPT_5221))

    def test_short_text_unchanged(self):
        pane = "\n".join([_RULE, "\u276f Hello, agent", _RULE])
        self.assertTrue(sv.submit_pending(self.profile, pane, "Hello, agent"))
        self.assertFalse(sv.submit_pending(
            self.profile, "\n".join([_RULE, "\u276f ", _RULE]), "Hello, agent"))

    def test_should_repress_head_scrolled_idle_pane(self):
        self.assertTrue(sv.should_repress(self.profile, _PANE_5148_STUCK, _PROMPT_5148))

    def test_should_not_repress_head_scrolled_generating_pane(self):
        busy = _PANE_5148_STUCK.replace("(shift+tab to cycle)", "esc to interrupt")
        self.assertFalse(sv.should_repress(self.profile, busy, _PROMPT_5148))

    def test_pending_check_cli_sees_head_scrolled_pane(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = sv.main(["--harness", "claude-code", "--pending-check", "--text", _PROMPT_5148],
                         stdin=io.StringIO(_PANE_5148_STUCK))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "pending")


# --------------------------------------------------------------------------- #
# T-5380 round 2: fresh pending check before every RETRY Enter; edge cases.    #
# --------------------------------------------------------------------------- #

class TestPreRetryRecheck(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))
        self.profile.pop("render_wait_s")  # exercise retry semantics in isolation
        self.profile["settle_delay_s"] = 0

    def _run(self, fake, prompt=None):
        return sv.submit_verify(
            self.profile, "p:0.0", prompt or _PROMPT_5148,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep)

    def test_late_landing_enter_gets_no_retry_enter(self):
        # capture 1 (after Enter 1): still pending. capture 2 (pre-retry): the first
        # Enter landed late, composer empty. Exactly one Enter, result ok.
        fake = FakePane([_PANE_5148_STUCK, _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 1)
        self.assertEqual(r.attempts, 1)

    def test_retry_still_fires_when_pre_retry_check_reads_pending(self):
        # captures: post-Enter-1 pending, pre-retry pending -> Enter 2 -> submitted.
        fake = FakePane([_PANE_5148_STUCK, _PANE_5148_STUCK, _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)

    def test_first_enter_is_unconditional(self):
        # No capture happens before Enter 1, so a pane that already looks submitted
        # still gets exactly the one Enter.
        fake = FakePane([_submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 1)

    def test_empty_pre_retry_capture_is_no_vote(self):
        # Pending after Enter 1, then "" at the pre-retry check (a failed capture):
        # no composer region reads "submitted", which must not skip the retry Enter.
        fake = FakePane([_PANE_5148_STUCK, "", _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)

    def test_whitespace_only_pre_retry_capture_is_no_vote(self):
        fake = FakePane([_PANE_5148_STUCK, " \n\t\n", _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)

    def test_paste_rendering_after_the_post_enter_capture_is_not_confirmed(self):
        # T-5610 (orchestrator 2026-10-26 18:28Z): on a heavy pane the collapsed paste had
        # not rendered when the post-Enter capture ran, so the composer looked empty
        # and the wake read "submit confirmed". The placeholder then appeared and sat
        # unsubmitted. The confirm re-sample sees it and the retry Enter submits it.
        fake = FakePane([_submitted_pane(["done"]), _PANE_5148_STUCK, _PANE_5148_STUCK,
                         _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)

    def test_late_landing_after_the_last_retry_is_confirmed(self):
        # T-5651 (specialist keep-going 2026-10-27 04:46Z): the seat was finishing a turn,
        # every check in the ~3-4s retry window still saw the text, and the Enter landed
        # after. specialist answered the nudge, but the unit failed and paged Recovery. A final
        # late re-check sees the cleared composer: ok, with no extra Enter.
        fake = FakePane([_PANE_5148_STUCK] * 5 + [_submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok, r)
        self.assertEqual(fake.enter_calls, 3)
        self.assertIn("late", r.detail)

    def test_submit_landing_well_after_the_first_late_look_is_confirmed(self):
        # T-5656 (recovery's own consume 2026-10-27 06:35Z, T-5651 live): the wake landed
        # but later than retries + one 3s look, so the unit paged. The late look is a
        # bounded poll: a cleared composer on the 4th late sample still confirms,
        # with no extra Enter.
        fake = FakePane([_PANE_5148_STUCK] * 8 + [_submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok, r)
        self.assertEqual(fake.enter_calls, 3)
        self.assertIn("late", r.detail)

    def test_late_poll_is_bounded_and_sends_no_enter(self):
        # All-pending: the late poll sleeps at most LATE_CONFIRM_TOTAL_S in
        # LATE_CONFIRM_POLL_S steps, sends no Enter, and reports unconfirmed.
        slept = []
        fake = FakePane([_PANE_5221_STUCK])
        r = sv.submit_verify(
            self.profile, "p:0.0", _PROMPT_5221,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=slept.append)
        self.assertFalse(r.ok)
        self.assertEqual(fake.enter_calls, 3)
        late = [s for s in slept if s == sv.LATE_CONFIRM_POLL_S]
        self.assertGreaterEqual(len(late), 2)
        self.assertLessEqual(sum(late), sv.LATE_CONFIRM_TOTAL_S)
        self.assertGreater(sum(late) + sv.LATE_CONFIRM_POLL_S, sv.LATE_CONFIRM_TOTAL_S)

    def test_empty_late_sample_is_no_vote(self):
        # An empty capture inside the late poll casts no vote; the poll keeps looking.
        fake = FakePane([_PANE_5148_STUCK] * 5 + ["", _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok, r)
        self.assertEqual(fake.enter_calls, 3)

    def test_all_pending_still_exhausts_retries(self):
        fake = FakePane([_PANE_5221_STUCK])
        r = self._run(fake, _PROMPT_5221)
        self.assertFalse(r.ok)
        self.assertEqual(fake.enter_calls, 3)


class TestEmptyPostEnterCapture(unittest.TestCase):
    """T-5412: an empty or whitespace-only capture right after an Enter is UNKNOWN,
    never "submitted". A real pane is never empty; a failed capture reads ""."""

    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))
        self.profile.pop("render_wait_s")  # exercise post-Enter empty-capture handling
        self.profile["settle_delay_s"] = 0

    def _run(self, fake):
        return sv.submit_verify(
            self.profile, "p:0.0", _PROMPT_5148,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=_no_sleep)

    def test_pending_then_empty_forever_is_unverified(self):
        fake = FakePane([_PANE_5148_STUCK, ""])
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("unverified: empty capture", r.detail)
        self.assertEqual(fake.enter_calls, 3)

    def test_all_empty_is_unverified(self):
        fake = FakePane([""])
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("unverified: empty capture", r.detail)
        self.assertEqual(fake.enter_calls, 3)

    def test_whitespace_only_is_unverified(self):
        fake = FakePane([" \n\t\n"])
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertIn("unverified: empty capture", r.detail)

    def test_one_blank_read_is_re_read_before_it_counts(self):
        # A single blank read (a redraw in flight) is re-read once; the re-read
        # sees the submit, so it is confirmed on Enter 1.
        sleeps = []
        fake = FakePane(["", _submitted_pane(["done"])])
        r = sv.submit_verify(
            self.profile, "p:0.0", _PROMPT_5148,
            send_text=fake.send_text, send_enter=fake.send_enter,
            capture=fake.capture, sleep=sleeps.append)
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, "submit confirmed")
        self.assertEqual(fake.enter_calls, 1)
        self.assertIn(sv.EMPTY_RECAPTURE_S, sleeps)

    def test_empty_post_enter_then_real_submit_on_pre_retry_is_ok(self):
        fake = FakePane(["", "", _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 1)
        self.assertEqual(r.attempts, 1)

    def test_empty_then_pending_is_plain_unconfirmed(self):
        fake = FakePane(["", _PANE_5148_STUCK])
        r = self._run(fake)
        self.assertFalse(r.ok)
        self.assertNotIn("unverified", r.detail)

    def test_empty_post_enter_then_real_submit_after_retry_enter_is_ok(self):
        # "" post-Enter 1 and its re-read, "" at pre-retry (no vote), Enter 2, real
        # submitted capture.
        fake = FakePane(["", "", "", _submitted_pane(["done"])])
        r = self._run(fake)
        self.assertTrue(r.ok)
        self.assertEqual(fake.enter_calls, 2)


class TestHeadScrolledEdgeCases(unittest.TestCase):
    def setUp(self):
        self.profile = dict(sv.get_profile("claude-code"))

    def test_tail_wrapped_across_two_rows_mid_word_is_pending(self):
        # (a) The last 48 characters split across two composer rows, mid-word.
        # Force a mid-word split: cut the final 30 raw characters at an arbitrary point.
        cut = len(_PROMPT_5221) - 11
        pane = "\n".join([
            _RULE,
            "\u276f " + _PROMPT_5221[len(_PROMPT_5221) - 90:cut],
            "  " + _PROMPT_5221[cut:],
            _RULE,
            "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"])
        self.assertTrue(sv.submit_pending(self.profile, pane, _PROMPT_5221))
        self.assertNotEqual(_PROMPT_5221[cut - 1], " ")  # really mid-word

    def test_short_injection_pending_in_composer(self):
        # (b1) shorter than the 48-char needle: head == tail == whole text.
        pane = "\n".join([_RULE, "\u276f poke T-1", _RULE])
        self.assertTrue(sv.submit_pending(self.profile, pane, "poke T-1"))

    def test_short_injection_in_history_above_empty_composer_is_not_pending(self):
        # (b2) the same short text after it submitted and scrolled into history.
        pane = "\n".join(["\u276f poke T-1", "\u25cf ok", "", _RULE, "\u276f ", _RULE,
                          "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"])
        self.assertFalse(sv.submit_pending(self.profile, pane, "poke T-1"))


# --------------------------------------------------------------------------- #
# T-4987 (a): codex composer rendered twice, a duplicate block BELOW the footer. #
# --------------------------------------------------------------------------- #

_CODEX_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_panes"
_WATCHMAN_FOOTER = "  gpt-5.6-sol high · ~/pantheon-config"


def derive_duplicated_composer(idle_text: str, notice: str, width: int = 78,
                               dup_rows: int = 6, cut: int = 40) -> str:
    """Provenance of watchman_duplicated_composer_2026-10-22_synthetic.txt.

    No capture of the 2026-10-22 watchman pane exists, so that fixture is SYNTHETIC,
    derived from the real watchman capture watchman_idle_2026-10-15.txt (80 columns).
    Its idle composer line is replaced by the wake notice wrapped at the composer width.
    Below the footer, a second render of the same composer is appended, scrolled by one
    row, so it opens '› (from inbox_consumer).' as in Recovery's 10-22 note. That render is
    cut off at the pane bottom (the overflow), so it holds neither the notice's head nor
    its tail. The notice is a frozen build_kick("watchman", ...) output
    (watchman_wake_notice_2026-10-22_synthetic.injected.txt)."""
    import textwrap
    rows: list[str] = []
    for part in notice.split("\n"):
        rows += textwrap.wrap(part, width=width) or [""]
    composer = ["› " + rows[0]] + ["  " + r for r in rows[1:]]
    dup = ["› " + rows[1]] + ["  " + r for r in rows[2:1 + dup_rows]]
    dup[-1] = dup[-1][:cut]
    lines = idle_text.splitlines()
    k = max(i for i, ln in enumerate(lines) if ln.startswith("› "))
    return "\n".join(lines[:k] + composer + lines[k + 1:] + dup) + "\n"


def load_codex_fixture(name: str) -> str:
    return (_CODEX_FIXTURES / name).read_text(encoding="utf-8")


class TestCodexDuplicatedComposer(unittest.TestCase):
    """T-4987 (a), the watchman 2026-10-22 specimen. The wake notice sat unsubmitted
    while the inject-time check read 'submitted': the LAST '›' line (where the composer
    region starts) was a duplicate render below the footer that holds neither needle.
    The live composer above the footer holds our text, and must read pending."""

    def setUp(self):
        self.profile = sv.get_profile("codex")
        self.notice = load_codex_fixture("watchman_wake_notice_2026-10-22_synthetic.injected.txt")
        self.pane = load_codex_fixture("watchman_duplicated_composer_2026-10-22_synthetic.txt")
        self.idle = load_codex_fixture("watchman_idle_2026-10-15.txt")

    def test_fixture_provenance(self):
        self.assertEqual(derive_duplicated_composer(self.idle, self.notice), self.pane)
        below = self.pane.split(_WATCHMAN_FOOTER, 1)[1]
        self.assertTrue(below.lstrip("\n").startswith("› (from inbox_consumer)."))
        compact = "".join(below.split())
        self.assertNotIn("".join(self.notice[:48].split()), compact)
        self.assertNotIn("".join(self.notice.split())[-40:], compact)

    def test_footer_recognizer_matches_health_export(self):
        import health_export
        self.assertEqual(sv._CODEX_FOOTER_RE.pattern, health_export._CODEX_FOOTER_RE.pattern)

    def test_duplicated_composer_reads_pending(self):
        self.assertTrue(sv.submit_pending(self.profile, self.pane, self.notice))

    def test_cli_pending_check_reads_pending(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = sv.main(["--harness", "codex", "--pending-check", "--text", self.notice],
                         stdin=io.StringIO(self.pane))
        self.assertEqual((rc, out.getvalue().strip()), (0, "pending"))

    def test_inject_time_verify_is_not_confirmed(self):
        result = sv.submit_verify(
            dict(self.profile, settle_delay_s=0), "watchman", self.notice,
            send_text=lambda p, t: True, send_enter=lambda p: True,
            capture=lambda p: self.pane, sleep=lambda s: None)
        self.assertFalse(result.ok, result)
        self.assertIn("unconfirmed", result.detail)

    def test_submitted_then_redrawn_reads_submitted(self):
        # T-1054: the stale renders now sit in history above a fresh composer + footer.
        pane = self.pane + "\n› Improve documentation in @filename\n\n" + _WATCHMAN_FOOTER + "\n"
        self.assertFalse(sv.submit_pending(self.profile, pane, self.notice))

    def test_fragment_below_footer_alone_is_not_pending(self):
        # The composer above the footer shows the placeholder. A fragment of our text
        # below the footer is not two renders agreeing, so it gets no Enter retry.
        below = self.pane.split(_WATCHMAN_FOOTER, 1)[1]
        pane = self.idle.split(_WATCHMAN_FOOTER, 1)[0] + _WATCHMAN_FOOTER + below
        self.assertFalse(sv.submit_pending(self.profile, pane, self.notice))

    def test_foreign_text_below_footer_keeps_old_reading(self):
        above = self.pane.split(_WATCHMAN_FOOTER, 1)[0]
        pane = above + _WATCHMAN_FOOTER + "\n› rm -rf build and push to main please\n"
        self.assertFalse(sv.submit_pending(self.profile, pane, self.notice))

    def test_claude_profile_unchanged(self):
        # The duplicate-render rule is codex-only (a claude composer has a border).
        pane = self.pane.replace("›", "❯")
        self.assertFalse(sv.submit_pending(sv.get_profile("claude-code"), pane, self.notice))


class TestCodexRealCaptures(unittest.TestCase):
    """T-4987 (a): real codex captures from /home/pantheon-shared/t4983, ANSI-stripped
    (what `capture-pane -p` returns)."""

    def setUp(self):
        self.profile = sv.get_profile("codex")

    def test_specialist_stuck_dm_reads_pending(self):
        pane = load_codex_fixture("specialist_stuck_tgdm_2026-10-14.txt")
        dm = ("[inbound Telegram DM from the operator | chat_id=424242424 | message_id=4541] Can we "
              "also test deepseek? I have 2$ in credits plus api token  (Reply via the telegram "
              "MCP `reply` tool with chat_id=\"424242424\". For attachments, call the telegram "
              "MCP `download_attachment` tool with attachment_file_id, then read the returned "
              "local path.)")
        self.assertTrue(sv.submit_pending(self.profile, pane, dm))
        # Text in history above the composer is not pending.
        self.assertFalse(sv.submit_pending(
            self.profile, pane,
            "Replied to the operator with the broader goal: evaluating OpenRouter-hosted"))

    def test_busy_baseline_is_generating_and_not_pending(self):
        pane = load_codex_fixture("codex_busy_baseline_2026-10-13.txt")
        self.assertFalse(sv.submit_pending(
            self.profile, pane, "T-4987 an unrelated injected text for the busy baseline"))
        self.assertEqual(sv.busy_reason(pane), "generating")


class ClockedPane(FakePane):
    """FakePane whose capture() is a function of fake time (advanced by sleep()),
    so a test can say "the paste renders 3s after it was typed"."""

    def __init__(self, render_after_s=None, *, text, submit_on_enter=True):
        super().__init__([])
        self.t = 0.0
        self.text = text
        self.render_after_s = render_after_s
        self.typed_at = None
        self.submit_on_enter = submit_on_enter
        self.submitted = False
        self.events = []

    def sleep(self, secs):
        self.t += secs

    def send_text(self, pane, text):
        self.typed_at = self.t
        self.events.append(("text", self.t))
        return super().send_text(pane, text)

    def send_enter(self, pane):
        self.events.append(("enter", self.t))
        rendered = (self.typed_at is not None and self.render_after_s is not None
                    and self.t - self.typed_at >= self.render_after_s)
        # A fresh TUI swallows an Enter that lands before the paste has rendered.
        if rendered and self.submit_on_enter:
            self.submitted = True
        return super().send_enter(pane)

    def capture(self, pane):
        if self.submitted or self.typed_at is None:
            return "› Type a message"
        if self.render_after_s is not None and self.t - self.typed_at >= self.render_after_s:
            return f"› {self.text}"
        return "› Type a message"


class GlyphClockedPane(ClockedPane):
    """ClockedPane with a configurable composer glyph (claude-code / hermes use U+276F)."""

    def __init__(self, render_after_s=None, *, text, glyph):
        super().__init__(render_after_s, text=text)
        self.glyph = glyph

    def capture(self, pane):
        return super().capture(pane).replace("›", self.glyph, 1)


class TestRenderGateOptIn(unittest.TestCase):
    """T-6132: Claude gates by default; Hermes remains measurement-gated opt-in."""

    TEXT = "You just (re)started. Resume the standing instructions and report back."

    def run_sv(self, harness, env):
        pane = GlyphClockedPane(render_after_s=3.0, text=self.TEXT, glyph="❯")
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("RENDER_WAIT_S", "INJECT_ENTER_DELAY", "SUBMIT_RETRIES"):
                if k not in env:
                    os.environ.pop(k, None)
            prof = sv._profile_with_env_overrides(harness)
        prof["settle_delay_s"] = 0
        r = sv.submit_verify(prof, "pane", self.TEXT, send_text=pane.send_text,
                             send_enter=pane.send_enter, capture=pane.capture, sleep=pane.sleep)
        return r, pane

    def test_late_render_false_passes_today_without_the_gate(self):
        # The hole the ticket names: Enter lands before the paste renders (swallowed),
        # the blank composer reads "submitted", and nothing was submitted.
        r, pane = self.run_sv("hermes", {})
        self.assertTrue(r.ok)
        self.assertFalse(pane.submitted, "hermes: expected the false-pass")

    def test_claude_default_gate_closes_late_render_false_pass(self):
        r, pane = self.run_sv("claude-code", {})
        self.assertTrue(r.ok, r)
        self.assertTrue(pane.submitted)
        first_enter = next(t for kind, t in pane.events if kind == "enter")
        self.assertGreaterEqual(first_enter, 3.0)

    def test_render_wait_env_closes_it(self):
        for harness in ("claude-code", "hermes"):
            r, pane = self.run_sv(harness, {"RENDER_WAIT_S": "8"})
            self.assertTrue(r.ok, (harness, r))
            self.assertTrue(pane.submitted, harness)
            first_enter = next(t for kind, t in pane.events if kind == "enter")
            self.assertGreaterEqual(first_enter, 3.0, harness)

    def test_bad_or_zero_render_wait_env_preserves_the_claude_default(self):
        for val in ("", "abc", "-1", "0", "nan"):
            with mock.patch.dict(os.environ, {"RENDER_WAIT_S": val}):
                prof = sv._profile_with_env_overrides("claude-code")
            self.assertEqual(prof.get("render_wait_s"), 8.0, val)

    def test_render_wait_env_is_finite_and_capped(self):
        # reviewer (T-6132): inf would make the gate poll forever on a never-rendered text
        for val, want in (("inf", sv.MAX_RENDER_WAIT_S), ("1e999", sv.MAX_RENDER_WAIT_S),
                          ("600", sv.MAX_RENDER_WAIT_S), (" 8\n", 8.0)):
            with mock.patch.dict(os.environ, {"RENDER_WAIT_S": val}):
                prof = sv._profile_with_env_overrides("claude-code")
            self.assertEqual(prof.get("render_wait_s"), want, repr(val))

    def test_profile_defaults_match_measured_rollout(self):
        self.assertEqual(sv.get_profile("claude-code")["render_wait_s"], 8.0)
        self.assertNotIn("render_wait_s", sv.get_profile("hermes"))


class TestRenderGate(unittest.TestCase):
    """T-6130: the fresh-TUI render race (consultant 2026-11-03 22:57Z)."""

    TEXT = "You (codex) just (re)started. Resume the standing instructions and report."

    @staticmethod
    def watchman_pending_fixture():
        """T-5739: Recovery's 2026-10-29 two-line collapsed wake capture shape."""
        return (
            Path(__file__).with_name("testdata")
            / "watchman_pending_collapsed_wake_2026-10-29.txt"
        ).read_text(encoding="utf-8")

    def run_sv(self, pane, profile=None):
        prof = dict(profile or sv.get_profile("codex"))
        prof["settle_delay_s"] = 0
        return sv.submit_verify(prof, "codex-session", self.TEXT, send_text=pane.send_text,
                                send_enter=pane.send_enter, capture=pane.capture,
                                sleep=pane.sleep)

    def test_codex_profile_gates_and_other_profiles_do_not(self):
        self.assertEqual(sv.get_profile("codex")["render_wait_s"], 8.0)
        self.assertEqual(sv.get_profile("claude-code")["render_wait_s"], 8.0)
        self.assertNotIn("render_wait_s", sv.get_profile("hermes"))

    def test_late_render_incident_replay_enter_waits_for_the_text(self):
        # The text renders 3s after typing. The old algorithm sent Enter at ~0s
        # (swallowed), read the blank composer as submitted and returned ok=True.
        pane = ClockedPane(render_after_s=3.0, text=self.TEXT)
        r = self.run_sv(pane)
        self.assertTrue(r.ok, r)
        self.assertTrue(pane.submitted, "the Enter was sent before the paste rendered")
        first_enter_t = next(t for kind, t in pane.events if kind == "enter")
        self.assertGreaterEqual(first_enter_t, 3.0)

    def test_text_that_never_renders_is_unverified_and_sends_no_enter(self):
        pane = ClockedPane(render_after_s=None, text=self.TEXT)
        r = self.run_sv(pane)
        self.assertFalse(r.ok)
        self.assertIn("never observed", r.detail)
        self.assertEqual(pane.enter_calls, 0)
        self.assertLessEqual(pane.t, 8.5, "gate is bounded by render_wait_s")

    def test_blank_pane_never_confirms(self):
        # Every capture empty (a failed capture reads ""): not a vote, not a confirm.
        fake = FakePane([""])
        r = self.run_sv_fake(fake)
        self.assertFalse(r.ok)
        self.assertEqual(fake.enter_calls, 0)

    def run_sv_fake(self, fake):
        prof = dict(sv.get_profile("codex"))
        prof["settle_delay_s"] = 0
        return sv.submit_verify(prof, "codex-session", self.TEXT, send_text=fake.send_text,
                                send_enter=fake.send_enter, capture=fake.capture,
                                sleep=_no_sleep)

    def test_retry_does_not_repaste_over_its_own_pending_text(self):
        # An earlier call pasted the text and gave up; the caller retries. The
        # composer already holds it: no second send_text, just Enter + verify.
        fake = FakePane([f"› {self.TEXT}", f"› {self.TEXT}", f"› {self.TEXT}", "› Type a message",
                         "› Type a message", "› Type a message"])
        r = self.run_sv_fake(fake)
        self.assertTrue(r.ok, r)
        self.assertEqual(fake.text_calls, 0)
        self.assertGreaterEqual(fake.enter_calls, 1)

    def test_collapsed_paste_counts_as_rendered(self):
        collapsed = "› [Pasted Content 1020 chars]"
        fake = FakePane([collapsed, collapsed, "› Type a message", "› Type a message",
                         "› Type a message"])
        r = self.run_sv_fake(fake)
        self.assertTrue(r.ok, r)

    def test_watchman_two_line_collapsed_wake_is_pending(self):
        pane = self.watchman_pending_fixture()
        self.assertTrue(sv.submit_pending(sv.get_profile("codex"), pane, self.TEXT))

    def test_acceptance_waits_for_watchman_collapsed_wake_to_clear(self):
        pending = self.watchman_pending_fixture()
        cleared = "› Type a message\n\n  gpt-5.6-sol xhigh fast · ~ · Main [default]\n"
        # The first capture discovers an earlier stuck wake, so the verifier
        # must not paste a duplicate. The first Enter still leaves Recovery's exact
        # two-line collapsed shape in the composer; only the second clears it.
        # Two cleared samples are required before runtime_submission_accepted.
        fake = FakePane([pending, pending, pending, cleared, cleared])

        r = self.run_sv_fake(fake)

        self.assertTrue(r.ok, r)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(fake.text_calls, 0)
        self.assertEqual(fake.enter_calls, 2)

    def test_claude_code_gate_refuses_enter_when_text_never_renders(self):
        prof = dict(sv.get_profile("claude-code"))
        prof["settle_delay_s"] = 0
        pane = ClockedPane(render_after_s=None, text="hello there general")
        r = sv.submit_verify(
            prof, "p", "hello there general",
            send_text=pane.send_text, send_enter=pane.send_enter,
            capture=pane.capture, sleep=pane.sleep)
        self.assertFalse(r.ok)
        self.assertIn("never observed", r.detail)
        self.assertEqual(pane.enter_calls, 0)
        self.assertLessEqual(pane.t, 8.5)


class TestHarnessVendoredCopy(unittest.TestCase):
    def test_codex_harness_copy_is_byte_identical(self):
        # T-6130: the harness copy had rotted since the T-4755 import. It ships
        # standalone to the mirror, so it is a copy, pinned equal to this source.
        root = Path(__file__).resolve().parent.parent
        a = (root / "tools" / "submit_verify.py").read_bytes()
        b = (root / "harness" / "codex" / "codex_telegram" / "submit_verify.py").read_bytes()
        self.assertEqual(a, b, "re-vendor: cp tools/submit_verify.py harness/codex/codex_telegram/")



class TestFirstPasteRenderWaitSource(unittest.TestCase):
    """T-6293: the harness profile is the one source of the first-paste render bound."""

    def test_profiles_that_carry_the_gate_return_their_own_bound(self):
        for harness in ("claude-code", "codex"):
            self.assertEqual(sv.first_paste_render_wait(harness),
                             sv.get_profile(harness)["render_wait_s"])

    def test_profile_without_a_gate_falls_back_to_the_default_bound(self):
        self.assertNotIn("render_wait_s", sv.get_profile("hermes"))
        self.assertEqual(sv.first_paste_render_wait("hermes"), sv.DEFAULT_RENDER_WAIT_S)

    def test_cli_prints_the_bound_as_plain_decimal(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = sv.main(["--harness", "claude-code", "--print-first-paste-render-wait"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "8")

if __name__ == "__main__":
    unittest.main()
