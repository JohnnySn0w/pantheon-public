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

import dataclasses
import getpass
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import runtime_controller as rc
from tools.bringup_acceptance import codex_acceptance_config_guard as config_guard
from tools.bringup_acceptance import harness_codex_ready as hcr
from tools.bringup_acceptance import harness_codex_rc_ready as hrc
from tools.bringup_acceptance.types import HardStop
from tools.harness_adapter.codex import (
    ensure_model_migration_prompts_hidden,
    ensure_project_trusted,
    ensure_update_check_disabled,
)
from tools.harness_adapter.types import TmuxTarget
from tools.submit_verify import ready_for_submission


# T-5317: the drivers converge the worker-guard hook (real codex binary) before
# the config snapshot. These tests use fake bindings and mock the startup
# writers, so stub it module-wide; TestT5317Converge covers the call itself.
_converge_patches = [mock.patch.object(hcr, "converge_worker_guard_hook"), mock.patch.object(hrc, "converge_worker_guard_hook")]


def setUpModule() -> None:
    for p in _converge_patches:
        p.start()


def tearDownModule() -> None:
    for p in reversed(_converge_patches):
        p.stop()


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
        self.assertIn("sandbox: danger-full-access", text)
        self.assertIn("approval_policy: never", text)


def _config_fingerprint(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


class _FakeCodexAdapter:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def build_interactive_launch(self, _target: TmuxTarget, *, cwd: Path) -> object:
        return SimpleNamespace(cwd=self.cwd, argv=("/bin/true",), env={})


class CodexAcceptanceConfigGuardIntegrationTest(unittest.TestCase):
    def test_codex_acceptance_steps_restore_config_fingerprint_after_ready_and_rc_ready(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account-home"
            codex_home = account_home / ".codex"
            codex_home.mkdir(parents=True)
            config_path = codex_home / "config.toml"
            original = 'approval_policy = "never"\n'
            config_path.write_text(original, encoding="utf-8")
            before = _config_fingerprint(config_path)
            assert before is not None
            ready_work = root / "ready-work"
            rc_work = root / "rc-work"
            ready_adapter = _FakeCodexAdapter(ready_work)
            rc_adapter = _FakeCodexAdapter(rc_work)

            def binding(work_root: Path) -> object:
                return SimpleNamespace(
                    controller=SimpleNamespace(tmux=target),
                    account_home=account_home,
                    persona="fake-persona",
                    launch=SimpleNamespace(harness="codex"),
                    launch_cwd=work_root,
                )

            def mutate_startup(fake_binding: object, _adapter: object, *, replace: bool, context: object) -> rc.RuntimeStart:
                del replace, context
                ensure_project_trusted(codex_home, Path(fake_binding.launch_cwd))
                ensure_update_check_disabled(codex_home)
                ensure_model_migration_prompts_hidden(codex_home)
                return rc.RuntimeStart(scope_unit="fake.scope", created_scope=True)

            common = (
                mock.patch.object(rc, "_start_runtime", side_effect=mutate_startup),
                mock.patch.object(rc, "_verify_started_runtime", return_value=None),
            )
            with common[0], common[1], \
                 mock.patch.object(hcr.persona_bringup, "load_binding", return_value=binding(ready_work)), \
                 mock.patch.object(hcr.persona_bringup, "build_adapter", return_value=ready_adapter), \
                 mock.patch.object(hcr, "_wait_for_codex_ready", return_value=READY_COMPOSER_AFTER_DISMISS), \
                 mock.patch.object(hcr, "_teardown_disposable_tmux_context", return_value=[]), \
                 mock.patch.object(hcr, "converge_worker_guard_hook") as ready_converge:
                hcr.run_codex_ready_bringup(work_root=ready_work, account="testaccount")

            self.assertEqual(_config_fingerprint(config_path), before)
            # T-5317: the driver really wires converge, with its own binding.
            ready_converge.assert_called_once()
            self.assertEqual(ready_converge.call_args.args[0], codex_home)
            self.assertEqual(ready_converge.call_args.args[1].launch_cwd, ready_work)

            fake_readiness = rc.RuntimeInputReadiness(
                ready=True,
                pane_sha256="readyhash",
                reason="runtime input readiness confirmed",
            )
            with mock.patch.object(rc, "_start_runtime", side_effect=mutate_startup), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(hrc.persona_bringup, "load_binding", return_value=binding(rc_work)), \
                 mock.patch.object(hrc.persona_bringup, "build_adapter", return_value=rc_adapter), \
                 mock.patch.object(rc, "_capture_visible_runtime_pane", return_value=MODAL_CURSOR_ON_OPTION_3), \
                 mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=fake_readiness), \
                 mock.patch.object(hrc, "_teardown_disposable_tmux_context", return_value=[]), \
                 mock.patch.object(hrc, "converge_worker_guard_hook") as rc_converge:
                hrc.run_codex_rc_ready_bringup(work_root=rc_work, account="testaccount")
            rc_converge.assert_called_once()
            self.assertEqual(rc_converge.call_args.args[0], codex_home)
            self.assertEqual(rc_converge.call_args.args[1].launch_cwd, rc_work)

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(_config_fingerprint(config_path), before)


class CodexAcceptanceConfigGuardUnitTest(unittest.TestCase):
    def test_prefix_snapshots_restore_after_each_writer_boundary_and_preexisting_noops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            original = 'approval_policy = "never"\n'
            config_path.write_text(original, encoding="utf-8")
            project_cwd = root / "project"
            project_cwd.mkdir()

            baseline = config_path.read_bytes()
            expectation = config_guard.build_startup_config_expectation(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            self.assertEqual([prefix.prefix_index for prefix in expectation.prefixes], [0, 1, 2, 3])
            self.assertEqual(
                [result.name for result in expectation.writer_results],
                [
                    "ensure_project_trusted",
                    "ensure_update_check_disabled",
                    "ensure_model_migration_prompts_hidden",
                ],
            )
            for prefix in expectation.prefixes[1:]:
                with self.subTest(prefix=prefix.prefix_index):
                    config_path.write_bytes(prefix.snapshot.content)
                    guard = config_guard.ReversibleAcceptanceConfigGuard(
                        codex_home=codex_home,
                        project_cwd=project_cwd,
                        owner="test",
                    )
                    guard.expectation = expectation
                    guard.restore()
                    self.assertEqual(config_path.read_bytes(), baseline)

            ensure_project_trusted(codex_home, project_cwd)
            ensure_update_check_disabled(codex_home)
            preexisting = config_path.read_bytes()
            expectation = config_guard.build_startup_config_expectation(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            self.assertFalse(expectation.writer_results[0].modified)
            self.assertFalse(expectation.writer_results[1].modified)
            self.assertEqual(expectation.prefixes[-1].snapshot.content, preexisting)

    def test_config_guard_rejects_same_bytes_mode_or_owner_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text('approval_policy = "never"\n', encoding="utf-8")
            project_cwd = root / "project"
            project_cwd.mkdir()
            expectation = config_guard.build_startup_config_expectation(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            prefix = expectation.prefixes[1].snapshot
            config_path.write_bytes(prefix.content)
            current_mode = config_path.stat().st_mode & 0o777
            config_path.chmod(0o600 if current_mode != 0o600 else 0o644)
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            guard.expectation = expectation
            with self.assertRaises(config_guard.AcceptanceConfigGuardError):
                guard.restore()

            owner_drift = dataclasses.replace(
                prefix,
                uid=(prefix.uid or 0) + 1,
            )
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            guard.expectation = expectation
            with mock.patch.object(config_guard, "snapshot_config_bytes", return_value=owner_drift):
                with self.assertRaises(config_guard.AcceptanceConfigGuardError):
                    guard.restore()

    def test_config_guard_preflight_refuses_unparseable_symlink_or_nonregular_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_cwd = root / "project"
            project_cwd.mkdir()

            bad_home = root / "bad" / ".codex"
            bad_home.mkdir(parents=True)
            bad_config = bad_home / "config.toml"
            bad_config.write_text("this is not [valid toml\n", encoding="utf-8")
            with self.assertRaisesRegex(
                config_guard.AcceptanceConfigGuardError,
                "refuses unparseable config.toml",
            ):
                config_guard.ReversibleAcceptanceConfigGuard(
                    codex_home=bad_home,
                    project_cwd=project_cwd,
                    owner="test",
                ).__enter__()
            self.assertEqual(bad_config.read_text(encoding="utf-8"), "this is not [valid toml\n")

            symlink_home = root / "symlink" / ".codex"
            symlink_home.mkdir(parents=True)
            outside = root / "outside.toml"
            outside.write_text('approval_policy = "never"\n', encoding="utf-8")
            (symlink_home / "config.toml").symlink_to(outside)
            with self.assertRaisesRegex(
                config_guard.AcceptanceConfigGuardError,
                "refuses symlink config target",
            ):
                config_guard.ReversibleAcceptanceConfigGuard(
                    codex_home=symlink_home,
                    project_cwd=project_cwd,
                    owner="test",
                ).__enter__()
            self.assertEqual(outside.read_text(encoding="utf-8"), 'approval_policy = "never"\n')

            directory_home = root / "directory" / ".codex"
            directory_config = directory_home / "config.toml"
            directory_config.mkdir(parents=True)
            with self.assertRaisesRegex(
                config_guard.AcceptanceConfigGuardError,
                "refuses non-regular config target",
            ):
                config_guard.ReversibleAcceptanceConfigGuard(
                    codex_home=directory_home,
                    project_cwd=project_cwd,
                    owner="test",
                ).__enter__()
            self.assertTrue(directory_config.is_dir())

    def test_codex_acceptance_config_guard_refuses_concurrent_drift_duplicate_or_unparseable_config(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account-home"
            codex_home = account_home / ".codex"
            codex_home.mkdir(parents=True)
            config_path = codex_home / "config.toml"
            config_path.write_text('approval_policy = "never"\n', encoding="utf-8")
            work_root = root / "ready-work"
            fake_binding = SimpleNamespace(
                controller=SimpleNamespace(tmux=target),
                account_home=account_home,
                persona="fake-persona",
                launch=SimpleNamespace(harness="codex"),
                launch_cwd=work_root,
            )
            fake_adapter = _FakeCodexAdapter(work_root)

            def drift_startup(_binding: object, _adapter: object, *, replace: bool, context: object) -> rc.RuntimeStart:
                del replace, context
                ensure_project_trusted(codex_home, work_root)
                with config_path.open("a", encoding="utf-8") as handle:
                    handle.write('\n[projects."/concurrent-drift"]\ntrust_level = "trusted"\n')
                return rc.RuntimeStart(scope_unit="fake.scope", created_scope=True)

            with mock.patch.object(hcr.persona_bringup, "load_binding", return_value=fake_binding), \
                 mock.patch.object(hcr.persona_bringup, "build_adapter", return_value=fake_adapter), \
                 mock.patch.object(rc, "_start_runtime", side_effect=drift_startup), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(hcr, "_wait_for_codex_ready", return_value=READY_COMPOSER_AFTER_DISMISS), \
                 mock.patch.object(hcr, "_teardown_disposable_tmux_context", return_value=[]):
                with self.assertRaises(Exception) as ctx:
                    hcr.run_codex_ready_bringup(work_root=work_root, account="testaccount")

            self.assertIn("config", str(ctx.exception).lower())
            self.assertIn("/concurrent-drift", config_path.read_text(encoding="utf-8"))

    def test_config_guard_refuses_replaced_symlink_nonregular_and_failed_post_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            secret_original = 'approval_policy = "never"\n# secret-config-bytes\n'
            config_path.write_text(secret_original, encoding="utf-8")
            project_cwd = root / "project"
            project_cwd.mkdir()
            expectation = config_guard.build_startup_config_expectation(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )

            config_path.unlink()
            outside = root / "outside.toml"
            outside.write_text("outside\n", encoding="utf-8")
            config_path.symlink_to(outside)
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            guard.expectation = expectation
            with self.assertRaises(config_guard.AcceptanceConfigGuardError) as symlink_ctx:
                guard.restore()
            self.assertNotIn("secret-config-bytes", str(symlink_ctx.exception))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

            config_path.unlink()
            config_path.mkdir()
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home,
                project_cwd=project_cwd,
                owner="test",
            )
            guard.expectation = expectation
            with self.assertRaises(config_guard.AcceptanceConfigGuardError):
                guard.restore()
            config_path.rmdir()
            config_path.write_bytes(expectation.prefixes[1].snapshot.content)

            with mock.patch.object(config_guard, "_snapshot_matches", return_value=False):
                with self.assertRaises(config_guard.AcceptanceConfigGuardError) as verify_ctx:
                    config_guard.restore_config_snapshot_atomically(
                        config_path=config_path,
                        desired=expectation.before,
                    )
            self.assertNotIn("secret-config-bytes", str(verify_ctx.exception))


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


class TestT5317Converge(unittest.TestCase):
    def test_converge_runs_before_snapshot_so_in_run_writer_is_a_noop(self) -> None:
        """A writer outside the modeled prefixes (the worker-guard hook) would
        make restore refuse with drift; converging it first keeps the post-run
        config on a modeled prefix and restores to the converged bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text('approval_policy = "never"\n', encoding="utf-8")
            project_cwd = root / "project"
            project_cwd.mkdir()
            hook_block = '\n[hooks.fake_guard]\ncommand = "guard"\n'

            def hook_writer() -> None:  # idempotent, like ensure_worker_guard_hook
                text = config_path.read_text(encoding="utf-8")
                if hook_block not in text:
                    config_path.write_text(text + hook_block, encoding="utf-8")

            calls: list[str] = []
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home, project_cwd=project_cwd, owner="test",
                converge=lambda: (calls.append("converge"), hook_writer()),
            )
            guard.__enter__()
            self.assertEqual(calls, ["converge"])
            converged = config_path.read_bytes()
            # The run: the replayed writers, then the hook writer again (no-op).
            ensure_project_trusted(codex_home, project_cwd)
            hook_writer()
            guard.restore()
            self.assertEqual(config_path.read_bytes(), converged)

    def test_without_converge_an_unmodeled_writer_is_refused_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text('approval_policy = "never"\n', encoding="utf-8")
            project_cwd = root / "project"
            project_cwd.mkdir()
            guard = config_guard.ReversibleAcceptanceConfigGuard(
                codex_home=codex_home, project_cwd=project_cwd, owner="test",
            )
            guard.__enter__()
            ensure_project_trusted(codex_home, project_cwd)
            with config_path.open("a", encoding="utf-8") as fh:
                fh.write('\n[hooks.fake_guard]\ncommand = "guard"\n')
            with self.assertRaises(config_guard.AcceptanceConfigGuardError):
                guard.restore()

    def test_converge_runs_before_the_expectation_is_built(self) -> None:
        order: list[str] = []
        guard = config_guard.ReversibleAcceptanceConfigGuard(
            codex_home=Path("/h/.codex"), project_cwd=Path("/p"), owner="t",
            converge=lambda: order.append("converge"),
        )
        with mock.patch.object(
            config_guard, "build_startup_config_expectation",
            side_effect=lambda **_: order.append("snapshot"),
        ):
            guard.__enter__()
        self.assertEqual(order, ["converge", "snapshot"])

    def test_fallback_swap_guard_converges_the_codex_binding(self) -> None:
        from tools.bringup_acceptance import harness_fallback_swap as hfs

        def binding(harness: str) -> SimpleNamespace:
            return SimpleNamespace(
                selected_harness=harness, account_home=Path("/acct"),
                launch_cwd=Path("/work"), controller=SimpleNamespace(tmux=None),
                launch=SimpleNamespace(executable=f"/bin/{harness}"),
            )

        claude_b, codex_b = binding("claude"), binding("codex")
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = SimpleNamespace(cwd="/work")
        with mock.patch.object(hfs.persona_bringup, "build_adapter", return_value=adapter):
            guard = hfs._codex_config_guard(claude_b, codex_b)
        self.assertIsNotNone(guard.converge)
        with mock.patch.object(hfs, "converge_worker_guard_hook") as conv:
            guard.converge()
        conv.assert_called_once_with(Path("/acct/.codex"), codex_b)

    def test_converge_helper_passes_the_bindings_account_and_executable(self) -> None:
        binding = SimpleNamespace(
            account_home=Path("/acct"), launch=SimpleNamespace(executable="/bin/codex-x"),
        )
        with mock.patch("tools.harness_adapter.codex.ensure_worker_guard_hook") as hook:
            config_guard.converge_worker_guard_hook(Path("/acct/.codex"), binding)
        hook.assert_called_once_with(
            Path("/acct/.codex"), Path("/acct"), executable=Path("/bin/codex-x"),
        )

if __name__ == "__main__":
    unittest.main()
