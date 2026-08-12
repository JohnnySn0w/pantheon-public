"""Tests for tools.bringup_acceptance.harness_codex_ready (T-2572).

Two tiers:

* Pure-function tests for modal detection / precise dismissal navigation,
  run against REAL pane text captured live 2026-08-05 against codex-cli
  0.136.0 on a disposable tmux socket (see module docstring) -- these are
  the tests that actually prove the "never sends a wrong key" safety
  property, independent of the stand-in fixture's own honesty limits.
* `_wait_for_codex_ready` orchestration tests with fully injected
  capture/send_keys/sleep/monotonic (no tmux, no real time) -- mirrors
  tools.submit_verify's own "fully unit-testable" design.

Plus one self-skipping real-tmux/real-systemd-user disposable-scope
integration test (mirrors test_bringup_acceptance_harness_fallback_swap.py's
own LiveKickDisposableScopeLiveTests), using the safe stand-in script.

No live production systemd/tmux server or real mailbox hub is ever touched.
"""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import runtime_controller as rc
from tools.bringup_acceptance import harness_codex_ready as hcr
from tools.bringup_acceptance.types import HardStop
from tools.submit_verify import ready_for_submission


def _skip_unless_disposable_tmux_available(test: unittest.TestCase) -> None:
    if shutil.which("tmux") is None or shutil.which("systemd-run") is None:
        test.skipTest("tmux/systemd-run not available in this environment")
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if probe.stdout.strip() not in ("running", "degraded"):
        test.skipTest(
            f"no usable systemd --user session in this environment: "
            f"stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )


# --------------------------------------------------------------------------- #
# Real pane-text fixtures -- captured live 2026-08-05 against codex-cli      #
# 0.136.0 on a disposable tmux socket (T-2572 plan investigation). Trailing  #
# blank padding lines (tmux fills the pane height) are trimmed; irrelevant   #
# to every predicate under test, which only look at specific lines/needles. #
# --------------------------------------------------------------------------- #

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

# T-2599: model-migration modal fixture. Live-captured verbatim (T-2599
# wet-run, 2026-08-08): `tmux capture-pane -p` against a real disposable
# tmux pane running the real pinned /usr/bin/codex 0.146.0 against the real
# account home, forced `--model gpt-5.4` to hit the live active gpt-5.4 ->
# gpt-5.6-terra preset. NOT a composition -- this is what the pane actually
# showed, confirmed via `codex_modal.is_codex_model_migration_modal` and a
# real dismissal (Enter -> ready_for_submission True) against the live pane.
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


# --------------------------------------------------------------------------- #
# Fixture sanity -- the fixtures themselves must exercise the predicates the #
# way the live investigation actually observed them.                        #
# --------------------------------------------------------------------------- #


class FixtureSanityTest(unittest.TestCase):
    def test_raw_modal_is_not_ready_for_submission(self) -> None:
        # No frame edge on the raw modal -- ready_for_submission must not
        # false-positive on the picker's own "›" selection cursor.
        self.assertFalse(ready_for_submission(MODAL_CURSOR_ON_OPTION_1))
        self.assertFalse(ready_for_submission(MODAL_CURSOR_ON_OPTION_3))

    def test_post_dismiss_composer_is_ready_for_submission(self) -> None:
        self.assertTrue(ready_for_submission(READY_COMPOSER_AFTER_DISMISS))


# --------------------------------------------------------------------------- #
# _is_codex_update_modal / _cursor_on_skip_until_next_version               #
# --------------------------------------------------------------------------- #


class ModalDetectionTest(unittest.TestCase):
    def test_detects_modal_regardless_of_cursor_position(self) -> None:
        for fixture in (
            MODAL_CURSOR_ON_OPTION_1, MODAL_CURSOR_ON_OPTION_2, MODAL_CURSOR_ON_OPTION_3,
        ):
            self.assertTrue(hcr._is_codex_update_modal(fixture))

    def test_compact_post_dismiss_notice_is_not_the_modal(self) -> None:
        # The durable-dismissal compact banner lacks "Skip until next
        # version" entirely -- must not be mistaken for the interactive
        # picker (would otherwise trigger a pointless/dangerous dismiss).
        self.assertFalse(hcr._is_codex_update_modal(READY_COMPOSER_AFTER_DISMISS))

    def test_plain_text_is_not_the_modal(self) -> None:
        self.assertFalse(hcr._is_codex_update_modal(NOT_READY_PLAIN_TEXT))


class CursorPositionTest(unittest.TestCase):
    def test_cursor_on_option_1_is_false(self) -> None:
        self.assertFalse(hcr._cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_1))

    def test_cursor_on_option_2_is_false(self) -> None:
        self.assertFalse(hcr._cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_2))

    def test_cursor_on_option_3_is_true(self) -> None:
        self.assertTrue(hcr._cursor_on_skip_until_next_version(MODAL_CURSOR_ON_OPTION_3))

    def test_ready_composer_is_false(self) -> None:
        self.assertFalse(hcr._cursor_on_skip_until_next_version(READY_COMPOSER_AFTER_DISMISS))


# --------------------------------------------------------------------------- #
# T-2599: _is_codex_model_migration_modal                                    #
# --------------------------------------------------------------------------- #


class MigrationModalDetectionTest(unittest.TestCase):
    def test_detects_live_markdown_opt_out_shape(self) -> None:
        self.assertTrue(hcr._is_codex_model_migration_modal(MIGRATION_MODAL_LIVE_MARKDOWN))

    def test_update_modal_is_not_the_migration_modal(self) -> None:
        self.assertFalse(hcr._is_codex_model_migration_modal(MODAL_CURSOR_ON_OPTION_1))

    def test_ready_composer_is_not_the_migration_modal(self) -> None:
        self.assertFalse(hcr._is_codex_model_migration_modal(READY_COMPOSER_AFTER_DISMISS))

    def test_plain_text_is_not_the_migration_modal(self) -> None:
        self.assertFalse(hcr._is_codex_model_migration_modal(NOT_READY_PLAIN_TEXT))


# --------------------------------------------------------------------------- #
# _dismiss_codex_update_modal -- the precision safety property.              #
# --------------------------------------------------------------------------- #


class DismissModalTest(unittest.TestCase):
    def test_navigates_from_option_1_with_two_downs_then_enter(self) -> None:
        # Fresh modal always starts cursor-on-1 (live-observed). Script:
        # capture -> option1 (not-3) -> Down -> capture -> option2 (not-3)
        # -> Down -> capture -> option3 -> Enter.
        captures = iter(
            [MODAL_CURSOR_ON_OPTION_1, MODAL_CURSOR_ON_OPTION_2, MODAL_CURSOR_ON_OPTION_3]
        )
        sent: list[str] = []
        hcr._dismiss_codex_update_modal(
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Down", "Down", "Enter"])

    def test_never_sends_enter_before_cursor_confirmed_on_option_3(self) -> None:
        # Cursor already on option 3 on first capture -- Enter fires
        # immediately, zero Down presses (still verified, not assumed).
        sent: list[str] = []
        hcr._dismiss_codex_update_modal(
            capture=lambda: MODAL_CURSOR_ON_OPTION_3,
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Enter"])

    def test_bounded_navigation_hardstops_instead_of_spinning_forever(self) -> None:
        # Cursor never reports on option 3 -- must HardStop within the
        # bounded attempt budget, never send a bare/blind Enter.
        sent: list[str] = []
        with self.assertRaises(HardStop):
            hcr._dismiss_codex_update_modal(
                capture=lambda: MODAL_CURSOR_ON_OPTION_1,
                send_keys=sent.append,
                sleep=lambda _s: None,
            )
        self.assertNotIn("Enter", sent)
        self.assertLessEqual(len(sent), hcr._MAX_MODAL_NAV_ATTEMPTS)


# --------------------------------------------------------------------------- #
# T-2599: _dismiss_codex_model_migration_modal -- always-Enter, still never  #
# blind (confirms the needle before every send).                            #
# --------------------------------------------------------------------------- #


class DismissMigrationModalTest(unittest.TestCase):
    def test_sends_enter_once_needle_confirmed(self) -> None:
        sent: list[str] = []
        hcr._dismiss_codex_model_migration_modal(
            capture=lambda: MIGRATION_MODAL_LIVE_MARKDOWN,
            send_keys=sent.append,
            sleep=lambda _s: None,
        )
        self.assertEqual(sent, ["Enter"])

    def test_bounded_attempts_hardstops_instead_of_spinning_forever(self) -> None:
        sent: list[str] = []
        with self.assertRaises(HardStop):
            hcr._dismiss_codex_model_migration_modal(
                capture=lambda: NOT_READY_PLAIN_TEXT,
                send_keys=sent.append,
                sleep=lambda _s: None,
            )
        self.assertEqual(sent, [])


# --------------------------------------------------------------------------- #
# _wait_for_codex_ready -- fully injected orchestration, no tmux/real time.  #
# --------------------------------------------------------------------------- #


class WaitForCodexReadyTest(unittest.TestCase):
    def test_immediate_ready_no_modal(self) -> None:
        result = hcr._wait_for_codex_ready(
            timeout_s=10.0, poll_interval_s=0.1,
            capture=lambda: READY_COMPOSER_AFTER_DISMISS,
            send_keys=lambda _k: self.fail("send_keys must not be called when already ready"),
            sleep=lambda _s: None,
            monotonic=iter([0.0, 0.0, 0.0, 0.0]).__next__,
        )
        self.assertEqual(result, READY_COMPOSER_AFTER_DISMISS)

    def test_dismisses_modal_then_reaches_ready(self) -> None:
        captures = iter(
            [
                MODAL_CURSOR_ON_OPTION_1,  # 1st poll: modal, not ready
                MODAL_CURSOR_ON_OPTION_1,  # _dismiss_codex_update_modal's own capture #1
                MODAL_CURSOR_ON_OPTION_2,  # its capture #2
                MODAL_CURSOR_ON_OPTION_3,  # its capture #3 -> sends Enter
                READY_COMPOSER_AFTER_DISMISS,  # re-poll after dismissal: ready
            ]
        )
        sent: list[str] = []
        result = hcr._wait_for_codex_ready(
            timeout_s=10.0, poll_interval_s=0.1,
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=lambda _s: None,
            monotonic=iter([0.0] * 10).__next__,
        )
        self.assertEqual(result, READY_COMPOSER_AFTER_DISMISS)
        self.assertEqual(sent, ["Down", "Down", "Enter"])

    def test_only_dismisses_once_even_if_modal_reappears(self) -> None:
        # A SECOND unrecognized/reappearing modal must never trigger a
        # second blind dismiss attempt -- falls through to timeout instead.
        dismiss_calls: list[str] = []

        def _fake_dismiss(**_kwargs: object) -> None:
            dismiss_calls.append("called")

        clock = iter([0.0, 0.0, 0.0, 100.0])  # 4th monotonic() call is past deadline
        with mock.patch.object(hcr, "_dismiss_codex_update_modal", side_effect=_fake_dismiss):
            with self.assertRaises(HardStop):
                hcr._wait_for_codex_ready(
                    timeout_s=10.0, poll_interval_s=0.1,
                    capture=lambda: MODAL_CURSOR_ON_OPTION_1,
                    send_keys=lambda _k: None,
                    sleep=lambda _s: None,
                    monotonic=clock.__next__,
                )
        self.assertEqual(dismiss_calls, ["called"])

    def test_timeout_on_unrecognized_non_ready_state_hardstops_with_pane_text(self) -> None:
        clock = iter([0.0, 0.0, 50.0])
        with self.assertRaises(HardStop) as ctx:
            hcr._wait_for_codex_ready(
                timeout_s=10.0, poll_interval_s=0.1,
                capture=lambda: NOT_READY_PLAIN_TEXT,
                send_keys=lambda _k: self.fail("must never guess a dismissal for an unknown state"),
                sleep=lambda _s: None,
                monotonic=clock.__next__,
            )
        self.assertIn(NOT_READY_PLAIN_TEXT.strip(), str(ctx.exception))

    def test_dismisses_migration_modal_then_reaches_ready(self) -> None:
        captures = iter(
            [
                MIGRATION_MODAL_LIVE_MARKDOWN,  # 1st poll: modal, not ready
                MIGRATION_MODAL_LIVE_MARKDOWN,  # dismiss's own capture -> sends Enter
                READY_COMPOSER_AFTER_DISMISS,  # re-poll after dismissal: ready
            ]
        )
        sent: list[str] = []
        result = hcr._wait_for_codex_ready(
            timeout_s=10.0, poll_interval_s=0.1,
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=lambda _s: None,
            monotonic=iter([0.0] * 10).__next__,
        )
        self.assertEqual(result, READY_COMPOSER_AFTER_DISMISS)
        self.assertEqual(sent, ["Enter"])

    def test_dismisses_both_modals_in_sequence(self) -> None:
        # Codex's real startup order: update-check before app-server start,
        # migration prompt after bootstrap -- a single bringup can hit both.
        captures = iter(
            [
                MODAL_CURSOR_ON_OPTION_3,  # 1st poll: update modal, cursor on skip
                MODAL_CURSOR_ON_OPTION_3,  # dismiss's own capture -> sends Enter
                MIGRATION_MODAL_LIVE_MARKDOWN,  # re-poll: migration modal now showing
                MIGRATION_MODAL_LIVE_MARKDOWN,  # dismiss's own capture -> sends Enter
                READY_COMPOSER_AFTER_DISMISS,  # re-poll: ready
            ]
        )
        sent: list[str] = []
        result = hcr._wait_for_codex_ready(
            timeout_s=10.0, poll_interval_s=0.1,
            capture=lambda: next(captures),
            send_keys=sent.append,
            sleep=lambda _s: None,
            monotonic=iter([0.0] * 10).__next__,
        )
        self.assertEqual(result, READY_COMPOSER_AFTER_DISMISS)
        self.assertEqual(sent, ["Enter", "Enter"])


# --------------------------------------------------------------------------- #
# codex_ready_status -- the false-green mode-guard.                          #
# --------------------------------------------------------------------------- #


class CodexReadyStatusTest(unittest.TestCase):
    def test_real_cli_and_ready_reads_proven(self) -> None:
        self.assertEqual(
            hcr.codex_ready_status({"mode": "real-cli", "ready": True}), "proven",
        )

    def test_real_cli_but_not_ready_reads_deferred(self) -> None:
        self.assertEqual(
            hcr.codex_ready_status({"mode": "real-cli", "ready": False}), "deferred",
        )

    def test_stand_in_never_reads_proven_even_if_ready_true(self) -> None:
        self.assertEqual(
            hcr.codex_ready_status({"mode": "stand-in", "ready": True}), "deferred",
        )


# --------------------------------------------------------------------------- #
# Disposable-context guard.                                                  #
# --------------------------------------------------------------------------- #


class DisposableContextGuardTest(unittest.TestCase):
    def test_empty_socket_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            hcr._assert_disposable_tmux_context(rc.TmuxRuntimeContext(socket=None))

    def test_production_scope_unit_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            hcr._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket="something", scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT)
            )

    def test_wrong_namespace_socket_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            hcr._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket="not-my-namespace-x", scope_unit=f"{hcr._SCOPE_PREFIX}x.scope")
            )

    def test_wrong_namespace_scope_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            hcr._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket=f"{hcr._SOCKET_PREFIX}x", scope_unit="not-my-namespace-x.scope")
            )

    def test_none_context_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            hcr._assert_disposable_tmux_context(None)

    def test_well_formed_disposable_context_passes(self) -> None:
        hcr._assert_disposable_tmux_context(
            rc.TmuxRuntimeContext(
                socket=f"{hcr._SOCKET_PREFIX}abc123", scope_unit=f"{hcr._SCOPE_PREFIX}abc123.scope",
            )
        )  # must not raise

    def test_new_disposable_tmux_context_is_well_formed(self) -> None:
        context = hcr._new_disposable_tmux_context("abc123")
        self.assertTrue(context.socket.startswith(hcr._SOCKET_PREFIX))
        self.assertTrue(context.scope_unit.startswith(hcr._SCOPE_PREFIX))


# --------------------------------------------------------------------------- #
# _VersionJsonSnapshot -- residue discipline for ~/.codex/version.json.      #
# --------------------------------------------------------------------------- #


class VersionJsonSnapshotTest(unittest.TestCase):
    def test_restores_pre_existing_file_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            path = codex_home / "version.json"
            original = b'{"latest_version":"1.2.3","last_checked_at":"x"}'
            path.write_bytes(original)
            with hcr._VersionJsonSnapshot(codex_home):
                path.write_text('{"dismissed_version":"9.9.9"}', encoding="utf-8")
                self.assertNotEqual(path.read_bytes(), original)
            self.assertEqual(path.read_bytes(), original)

    def test_restores_pre_existing_file_even_on_exception(self) -> None:
        # A distinct, non-NotImplementedError-related exception type --
        # NotImplementedError is itself a RuntimeError subclass, so a bare
        # `assertRaises(RuntimeError)` here would false-pass against an
        # unimplemented __enter__/__exit__ stub.
        class _Boom(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            path = codex_home / "version.json"
            original = b'{"latest_version":"1.2.3"}'
            path.write_bytes(original)
            with self.assertRaises(_Boom):
                with hcr._VersionJsonSnapshot(codex_home):
                    path.write_text("mutated", encoding="utf-8")
                    raise _Boom("boom")
            self.assertEqual(path.read_bytes(), original)

    def test_removes_file_that_did_not_exist_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            path = codex_home / "version.json"
            self.assertFalse(path.exists())
            with hcr._VersionJsonSnapshot(codex_home):
                path.write_text('{"dismissed_version":"9.9.9"}', encoding="utf-8")
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())


# --------------------------------------------------------------------------- #
# Stand-in script + manifest fixture sanity.                                 #
# --------------------------------------------------------------------------- #


class StandInScriptTest(unittest.TestCase):
    def test_written_script_is_executable_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stand-in-codex.sh"
            hcr._write_stand_in_script(path)
            self.assertTrue(path.stat().st_mode & 0o111)
            check = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_rendered_modal_matches_the_real_captured_fixture_shape(self) -> None:
        proc = subprocess.Popen(
            ["bash", str(_write_temp_stand_in())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            proc.stdin.close()  # no dismiss line ever sent -- only need the modal
            out = proc.stdout.readline() + proc.stdout.read()
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        self.assertTrue(hcr._is_codex_update_modal(out))


def _write_temp_stand_in() -> Path:
    import tempfile as _tempfile

    fd = _tempfile.NamedTemporaryFile(suffix=".sh", delete=False)
    path = Path(fd.name)
    fd.close()
    hcr._write_stand_in_script(path)
    return path


class ManifestFixtureTest(unittest.TestCase):
    def test_manifest_carries_production_faithful_native_config(self) -> None:
        text = hcr._fixture_manifest_text(persona="p", account="claude")
        self.assertIn("sandbox: workspace-write", text)
        self.assertIn("approval_policy: never", text)


# --------------------------------------------------------------------------- #
# Live disposable-scope integration (real tmux + real systemd --user, safe   #
# stand-in only). Mirrors LiveKickDisposableScopeLiveTests.                  #
# --------------------------------------------------------------------------- #


class CodexReadyDisposableScopeLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        _skip_unless_disposable_tmux_available(self)

    def test_stand_in_mode_reaches_ready_tears_down_cleanly_and_stays_deferred(self) -> None:
        account = getpass.getuser()
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "codex-ready"
            evidence = hcr.run_codex_ready_bringup(work_root=work_root, account=account)

            self.assertEqual(evidence["mode"], hcr._MODE_STAND_IN)
            self.assertTrue(evidence["ready"])

            # Structural false-green guard: stand-in never reads "proven".
            self.assertEqual(hcr.codex_ready_status(evidence), "deferred")

            # The driver removes its own work_root.
            self.assertFalse(work_root.exists())

            # Zero-residue: the disposable scope/socket are really gone.
            scope_unit = evidence["context"]["scope_unit"]
            socket_name = evidence["context"]["socket"]
            show = subprocess.run(
                ["systemctl", "--user", "show", "--property=ActiveState", scope_unit],
                check=False, capture_output=True, text=True,
            )
            self.assertNotIn("ActiveState=active", show.stdout)
            has_session = subprocess.run(
                ["tmux", "-L", socket_name, "has-session"],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(has_session.returncode, 0)


if __name__ == "__main__":
    unittest.main()
