"""Tests for tools.codex_modal (T-2594) -- the shared codex update-modal
detection + precise dismissal logic.

Fixtures mirror the real, live-captured pane text in
``tools/test_bringup_acceptance_harness_codex_ready.py`` (captured
2026-08-05 against codex-cli 0.136.0) -- duplicated here rather than
imported cross-test-file (tracked-acceptable duplication, same convention
``harness_codex_ready.py``'s own module docstring already cites for its
``_EnvOverride`` cousin).
"""

from __future__ import annotations

import unittest

from tools import codex_modal


MODAL_CURSOR_ON_OPTION_1 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "  Release notes: https://github.com/openai/codex/releases/latest\n"
    "\n"
    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "  3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

MODAL_CURSOR_ON_OPTION_2 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "  Release notes: https://github.com/openai/codex/releases/latest\n"
    "\n"
    "  1. Update now (runs `npm install -g @openai/codex`)\n"
    "› 2. Skip\n"
    "  3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

MODAL_CURSOR_ON_OPTION_3 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "  Release notes: https://github.com/openai/codex/releases/latest\n"
    "\n"
    "  1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "› 3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

READY_COMPOSER_AFTER_DISMISS = (
    "╭─────────────────────────────────────────────────╮\n"
    "│ ✨ Update available! 0.136.0 -> 0.146.1         │\n"
    "│ Run npm install -g @openai/codex to update.     │\n"
    "│                                                 │\n"
    "│ See full release notes:                         │\n"
    "│ https://github.com/openai/codex/releases/latest │\n"
    "╰─────────────────────────────────────────────────╯\n"
    "\n"
    "╭─────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.136.0)                │\n"
    "│                                           │\n"
    "│ model:     gpt-5.5   /model to change     │\n"
    "│ directory: /tmp/t2572-investigate-1820108 │\n"
    "╰─────────────────────────────╯\n"
    "\n"
    "  Tip: Switch models or reasoning effort quickly with /model.\n"
    "\n"
    "\n"
    "› Use /skills to list available skills\n"
    "\n"
    "  gpt-5.5 default · /tmp/t2572-investigate-1820108\n"
)

NOT_READY_PLAIN_TEXT = "plain text with no frame or composer glyph\n"


# --------------------------------------------------------------------------- #
# T-2599: model-migration modal fixtures.                                    #
#                                                                              #
# MIGRATION_MODAL_NO_OPT_OUT and MIGRATION_MODAL_OPT_OUT_MENU are NOT live-   #
# captured -- codex's currently-active live presets both carry               #
# migration_markdown (see codex_modal.py's module docstring), so neither of  #
# THESE two non-markdown shapes is reachable on this host today. They are    #
# byte-identical to codex-rs's own insta snapshot fixtures at the pinned     #
# 0.146.0 tag (compiler-verified real render output, not hand-invented):     #
# codex-rs/tui/src/snapshots/codex_tui__model_migration__tests__             #
# model_migration_prompt_gpt5_codex.snap (no-opt-out) and                    #
# ..._model_migration_prompt.snap (opt-out menu). Kept as coverage for the   #
# two non-markdown shapes the needle must still recognize.                   #
#                                                                              #
# MIGRATION_MODAL_LIVE_MARKDOWN IS live-captured (T-2599 wet-run,            #
# 2026-08-08): real pinned /usr/bin/codex 0.146.0, real account, forced      #
# `--model gpt-5.4` to hit the live active gpt-5.4 -> gpt-5.6-terra preset,  #
# `tmux capture-pane -p` verbatim (trailing blank pane-height padding        #
# trimmed, same convention every other fixture in this file already uses).  #
# This is the acceptance gate's real evidence: rendered pane text proven to  #
# match source prediction, not source prediction alone.                     #
# --------------------------------------------------------------------------- #

MIGRATION_MODAL_NO_OPT_OUT = (
    "\n"
    "> Codex just got an upgrade. Introducing gpt-5.1-codex-max.\n"
    "\n"
    "  We recommend switching from gpt-5-codex to\n"
    "  gpt-5.1-codex-max.\n"
    "\n"
    "  Codex-optimized flagship for deep and fast reasoning.\n"
    "  Learn more about gpt-5.1-codex-max at\n"
    "  https://www.codex.com/models/gpt-5.1-codex-max\n"
    "\n"
    "  Press enter to continue\n"
)

MIGRATION_MODAL_OPT_OUT_MENU = (
    "\n"
    "> Codex just got an upgrade. Introducing gpt-5.1-codex-max.\n"
    "\n"
    "  Upgrade to gpt-5.2-codex for the latest and greatest\n"
    "  agentic coding model.\n"
    "\n"
    "  You can continue using gpt-5.1-codex-mini if you prefer.\n"
    "\n"
    "  Choose how you'd like Codex to proceed.\n"
    "\n"
    "› 1. Try new model\n"
    "  2. Use existing model\n"
    "\n"
    "  Use ↑/↓ to move, press enter to confirm\n"
)

# Live-captured verbatim (T-2599 wet-run, 2026-08-08): `tmux capture-pane -p`
# against a real disposable tmux pane running the real pinned /usr/bin/codex
# 0.146.0 against the real account home, forced `--model gpt-5.4` to hit the
# live active gpt-5.4 -> gpt-5.6-terra preset. Trailing blank pane-height
# padding trimmed (same convention as every other fixture in this file).
# NOT a composition -- this is what the pane actually showed.
MIGRATION_MODAL_LIVE_MARKDOWN = (
    "\n"
    "  GPT-5.4 will be deprecated soon\n"
    "\n"
    "  Codex now uses GPT-5.6 Terra in place of GPT-5.4. Switch to GPT-5.6 Terra to continue.\n"
    "\n"
    "  Choose how you'd like Codex to proceed.\n"
    "\n"
    "› 1. Try new model\n"
    "  2. Use existing model\n"
    "\n"
    "  Use ↑/↓ to move, press enter to confirm\n"
)


class IsCodexUpdateModalTest(unittest.TestCase):
    def test_detects_modal_regardless_of_cursor_position(self) -> None:
        for fixture in (
            MODAL_CURSOR_ON_OPTION_1, MODAL_CURSOR_ON_OPTION_2, MODAL_CURSOR_ON_OPTION_3,
        ):
            self.assertTrue(codex_modal.is_codex_update_modal(fixture))

    def test_compact_post_dismiss_notice_is_not_the_modal(self) -> None:
        self.assertFalse(codex_modal.is_codex_update_modal(READY_COMPOSER_AFTER_DISMISS))

    def test_plain_text_is_not_the_modal(self) -> None:
        self.assertFalse(codex_modal.is_codex_update_modal(NOT_READY_PLAIN_TEXT))


class CursorOnSkipUntilNextVersionTest(unittest.TestCase):
    def test_cursor_on_option_1_is_false(self) -> None:
        self.assertFalse(codex_modal.cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_1))

    def test_cursor_on_option_2_is_false(self) -> None:
        self.assertFalse(codex_modal.cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_2))

    def test_cursor_on_option_3_is_true(self) -> None:
        self.assertTrue(codex_modal.cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_3))

    def test_ready_composer_is_false(self) -> None:
        self.assertFalse(
            codex_modal.cursor_on_skip_until_next_version(READY_COMPOSER_AFTER_DISMISS)
        )


class DismissCodexUpdateModalTest(unittest.TestCase):
    def test_navigates_from_option_1_with_two_downs_then_enter(self) -> None:
        captures = iter(
            [MODAL_CURSOR_ON_OPTION_1, MODAL_CURSOR_ON_OPTION_2, MODAL_CURSOR_ON_OPTION_3]
        )
        sent: list[str] = []
        codex_modal.dismiss_codex_update_modal(
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Down", "Down", "Enter"])

    def test_never_sends_enter_before_cursor_confirmed_on_option_3(self) -> None:
        sent: list[str] = []
        codex_modal.dismiss_codex_update_modal(
            capture=lambda: MODAL_CURSOR_ON_OPTION_3,
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Enter"])

    def test_bounded_navigation_fails_instead_of_spinning_forever(self) -> None:
        sent: list[str] = []
        with self.assertRaises(codex_modal.ModalNavigationFailed):
            codex_modal.dismiss_codex_update_modal(
                capture=lambda: MODAL_CURSOR_ON_OPTION_1,
                send_keys=sent.append,
                sleep=lambda _s: None,
            )
        self.assertNotIn("Enter", sent)
        self.assertLessEqual(len(sent), codex_modal.MAX_MODAL_NAV_ATTEMPTS)

    def test_max_attempts_and_poll_interval_are_overridable(self) -> None:
        sent: list[str] = []
        slept: list[float] = []
        with self.assertRaises(codex_modal.ModalNavigationFailed):
            codex_modal.dismiss_codex_update_modal(
                capture=lambda: MODAL_CURSOR_ON_OPTION_1,
                send_keys=sent.append,
                sleep=slept.append,
                max_attempts=2,
                poll_interval_s=1.5,
            )
        self.assertEqual(sent, ["Down", "Down"])
        self.assertEqual(slept, [1.5, 1.5])


class IsCodexModelMigrationModalTest(unittest.TestCase):
    def test_detects_no_opt_out_shape(self) -> None:
        self.assertTrue(codex_modal.is_codex_model_migration_modal(MIGRATION_MODAL_NO_OPT_OUT))

    def test_detects_opt_out_menu_shape(self) -> None:
        self.assertTrue(
            codex_modal.is_codex_model_migration_modal(MIGRATION_MODAL_OPT_OUT_MENU)
        )

    def test_detects_live_markdown_opt_out_shape(self) -> None:
        self.assertTrue(
            codex_modal.is_codex_model_migration_modal(MIGRATION_MODAL_LIVE_MARKDOWN)
        )

    def test_update_modal_is_not_the_migration_modal(self) -> None:
        self.assertFalse(codex_modal.is_codex_model_migration_modal(MODAL_CURSOR_ON_OPTION_1))

    def test_ready_composer_is_not_the_migration_modal(self) -> None:
        self.assertFalse(
            codex_modal.is_codex_model_migration_modal(READY_COMPOSER_AFTER_DISMISS)
        )

    def test_plain_text_is_not_the_migration_modal(self) -> None:
        self.assertFalse(codex_modal.is_codex_model_migration_modal(NOT_READY_PLAIN_TEXT))


class DismissCodexModelMigrationModalTest(unittest.TestCase):
    def test_sends_enter_once_needle_confirmed(self) -> None:
        sent: list[str] = []
        codex_modal.dismiss_codex_model_migration_modal(
            capture=lambda: MIGRATION_MODAL_LIVE_MARKDOWN,
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Enter"])

    def test_never_sends_enter_before_needle_confirmed(self) -> None:
        captures = iter([NOT_READY_PLAIN_TEXT, NOT_READY_PLAIN_TEXT, MIGRATION_MODAL_NO_OPT_OUT])
        sent: list[str] = []
        slept: list[float] = []
        codex_modal.dismiss_codex_model_migration_modal(
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=slept.append,
        )
        self.assertEqual(sent, ["Enter"])
        self.assertEqual(len(slept), 2)

    def test_bounded_attempts_fails_instead_of_spinning_forever(self) -> None:
        sent: list[str] = []
        with self.assertRaises(codex_modal.ModalNavigationFailed):
            codex_modal.dismiss_codex_model_migration_modal(
                capture=lambda: NOT_READY_PLAIN_TEXT,
                send_keys=sent.append,
                sleep=lambda _s: None,
            )
        self.assertEqual(sent, [])

    def test_max_attempts_and_poll_interval_are_overridable(self) -> None:
        sent: list[str] = []
        slept: list[float] = []
        with self.assertRaises(codex_modal.ModalNavigationFailed):
            codex_modal.dismiss_codex_model_migration_modal(
                capture=lambda: NOT_READY_PLAIN_TEXT,
                send_keys=sent.append,
                sleep=slept.append,
                max_attempts=2,
                poll_interval_s=1.5,
            )
        self.assertEqual(sent, [])
        self.assertEqual(slept, [1.5, 1.5])


if __name__ == "__main__":
    unittest.main()
