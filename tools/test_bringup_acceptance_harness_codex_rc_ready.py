"""Tests for tools.bringup_acceptance.harness_codex_rc_ready (T-2596).

Three tiers, mirroring tools/test_bringup_acceptance_harness_codex_ready.py's
own (T-2572) shape:

* Pure-function tests for the anti-false-green gates
  (``_await_modal_present``, ``codex_rc_ready_status``) with fully injected
  capture/sleep/monotonic -- no tmux, no real time.
* A fully-mocked wiring test proving the driver calls the REAL
  ``runtime_controller._wait_for_runtime_input_ready`` directly (the flagged
  T-2594 gap) rather than reimplementing its own poll loop -- no tmux/systemd
  required.
* One self-skipping real-tmux/real-systemd-user disposable-scope integration
  test (mirrors hcr's own CodexReadyDisposableScopeLiveTests), using the safe
  stand-in script, that actually drives the real rc entrypoint against a live
  pane and asserts the three-gate evidence.

No live production systemd/tmux server or real mailbox hub is ever touched.
"""

from __future__ import annotations

import getpass
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import runtime_controller as rc
from tools.bringup_acceptance import harness_codex_rc_ready as m
from tools.bringup_acceptance.types import HardStop
from tools.harness_adapter.types import TmuxTarget


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


MODAL_TEXT = (
    "\n"
    "  ✨ Update available! 0.0.0 -> 0.0.1\n"
    "\n"
    "  1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "› 3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

NOT_MODAL_TEXT = "plain text with no frame or composer glyph\n"

READY_COMPOSER_0147_NO_MODAL = (
    "╭─────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.147.0)  │\n"
    "│                             │\n"
    "│ model:     gpt-5.5          │\n"
    "│ directory: /tmp/t2953-r25   │\n"
    "╰─────────────────────────────╯\n"
    "\n"
    "› Use /skills to list available skills\n"
    "\n"
    "  gpt-5.5 default · /tmp/t2953-r25\n"
)

MALFORMED_READYISH_TEXT = "OpenAI Codex v0.147.0\n› prompt without frame\n"


# --------------------------------------------------------------------------- #
# Gate 1 -- _await_modal_present.                                            #
# --------------------------------------------------------------------------- #


class AwaitModalPresentTest(unittest.TestCase):
    def test_returns_pane_text_once_modal_appears(self) -> None:
        captures = iter([NOT_MODAL_TEXT, NOT_MODAL_TEXT, MODAL_TEXT])
        result = m._await_modal_present(
            capture=lambda: next(captures),
            sleep=lambda _s: None,
            monotonic=iter([0.0, 0.0, 0.0, 0.0]).__next__,
            timeout_s=10.0,
            poll_interval_s=0.1,
        )
        self.assertEqual(result, MODAL_TEXT)

    def test_immediate_modal_returns_without_polling(self) -> None:
        result = m._await_modal_present(
            capture=lambda: MODAL_TEXT,
            sleep=lambda _s: self.fail("must not sleep when modal is already present"),
            monotonic=iter([0.0, 0.0]).__next__,
            timeout_s=10.0,
            poll_interval_s=0.1,
        )
        self.assertEqual(result, MODAL_TEXT)

    def test_hardstops_if_modal_never_appears(self) -> None:
        # This is the structural "no bypass" guard: a stand-in (or account
        # state) that never renders the modal can never reach the rc-path
        # proof -- it fails loudly here, not silently downstream.
        clock = iter([0.0, 0.0, 0.0, 50.0])
        with self.assertRaises(HardStop) as ctx:
            m._await_modal_present(
                capture=lambda: NOT_MODAL_TEXT,
                sleep=lambda _s: None,
                monotonic=clock.__next__,
                timeout_s=10.0,
                poll_interval_s=0.1,
            )
        self.assertIn("never appeared", str(ctx.exception))


# --------------------------------------------------------------------------- #
# codex_rc_ready_status -- the false-green mode-guard.                       #
# --------------------------------------------------------------------------- #


class CodexRcReadyStatusTest(unittest.TestCase):
    def test_real_cli_and_ready_reads_proven(self) -> None:
        self.assertEqual(
            m.codex_rc_ready_status(
                {"mode": "real-cli", "ready": True, "rc_ready_outcome": "dismissal_not_needed"}
            ),
            "proven",
        )

    def test_real_cli_but_not_ready_reads_deferred(self) -> None:
        self.assertEqual(
            m.codex_rc_ready_status({"mode": "real-cli", "ready": False}), "deferred",
        )

    def test_stand_in_never_reads_proven_even_if_ready_true(self) -> None:
        self.assertEqual(
            m.codex_rc_ready_status({"mode": "stand-in", "ready": True}), "deferred",
        )


# --------------------------------------------------------------------------- #
# Disposable-context guard -- distinct T-2596 namespace prefix.              #
# --------------------------------------------------------------------------- #


class DisposableContextGuardTest(unittest.TestCase):
    def test_empty_socket_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            m._assert_disposable_tmux_context(rc.TmuxRuntimeContext(socket=None))

    def test_production_scope_unit_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            m._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket="something", scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT)
            )

    def test_wrong_namespace_socket_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            m._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket="not-my-namespace-x", scope_unit=f"{m._SCOPE_PREFIX}x.scope")
            )

    def test_wrong_namespace_scope_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            m._assert_disposable_tmux_context(
                rc.TmuxRuntimeContext(socket=f"{m._SOCKET_PREFIX}x", scope_unit="not-my-namespace-x.scope")
            )

    def test_none_context_hardstops(self) -> None:
        with self.assertRaises(HardStop):
            m._assert_disposable_tmux_context(None)

    def test_well_formed_disposable_context_passes(self) -> None:
        m._assert_disposable_tmux_context(
            rc.TmuxRuntimeContext(
                socket=f"{m._SOCKET_PREFIX}abc123", scope_unit=f"{m._SCOPE_PREFIX}abc123.scope",
            )
        )  # must not raise

    def test_new_disposable_tmux_context_is_well_formed(self) -> None:
        context = m._new_disposable_tmux_context("abc123")
        self.assertTrue(context.socket.startswith(m._SOCKET_PREFIX))
        self.assertTrue(context.scope_unit.startswith(m._SCOPE_PREFIX))

    def test_prefix_is_distinct_from_hcr_and_fallback_swap(self) -> None:
        from tools.bringup_acceptance import harness_codex_ready as hcr

        self.assertNotEqual(m._SOCKET_PREFIX, hcr._SOCKET_PREFIX)
        self.assertNotEqual(m._SCOPE_PREFIX, hcr._SCOPE_PREFIX)
        self.assertFalse(m._SOCKET_PREFIX.startswith(hcr._SOCKET_PREFIX))
        self.assertFalse(hcr._SOCKET_PREFIX.startswith(m._SOCKET_PREFIX))


# --------------------------------------------------------------------------- #
# Manifest fixture sanity.                                                    #
# --------------------------------------------------------------------------- #


class ManifestFixtureTest(unittest.TestCase):
    def test_manifest_carries_production_faithful_native_config(self) -> None:
        text = m._fixture_manifest_text(persona="p", account="claude")
        self.assertIn("sandbox: workspace-write", text)
        self.assertIn("approval_policy: never", text)


# --------------------------------------------------------------------------- #
# Wiring proof -- the driver calls the REAL rc._wait_for_runtime_input_ready #
# directly, not a hand-rolled loop. Fully mocked collaborators; no tmux or   #
# systemd required.                                                          #
# --------------------------------------------------------------------------- #


class DriverRoutesThroughRealEntrypointTest(unittest.TestCase):
    def test_calls_rc_wait_for_runtime_input_ready_with_codex_harness(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")
        fake_readiness = rc.RuntimeInputReadiness(
            ready=True, pane_sha256="deadbeef", reason="runtime input readiness confirmed",
        )

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            fake_binding = SimpleNamespace(
                controller=SimpleNamespace(tmux=target),
                account_home=Path(tmp) / "account-home",
                persona="fake-persona",
                launch=SimpleNamespace(harness="codex"),
            )

            with mock.patch.object(m.persona_bringup, "load_binding", return_value=fake_binding), \
                 mock.patch.object(m.persona_bringup, "build_adapter", return_value=object()), \
                 mock.patch.object(
                     rc, "_start_runtime",
                     return_value=rc.RuntimeStart(scope_unit="fake.scope", created_scope=True),
                 ), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(rc, "_capture_visible_runtime_pane", return_value=MODAL_TEXT), \
                 mock.patch.object(
                     rc, "_wait_for_runtime_input_ready", return_value=fake_readiness,
                 ) as mock_wait, \
                 mock.patch.object(m, "_teardown_disposable_tmux_context", return_value=[]):
                evidence = m.run_codex_rc_ready_bringup(work_root=work_root, account="testaccount")

        mock_wait.assert_called_once()
        call_args, call_kwargs = mock_wait.call_args
        self.assertEqual(call_args[0], target)
        self.assertEqual(call_kwargs["harness"], "codex")
        self.assertTrue(evidence["modal_confirmed_present"])
        self.assertTrue(evidence["rc_readiness_ready"])
        self.assertEqual(evidence["rc_readiness_reason"], "runtime input readiness confirmed")
        # The real dismiss function was never called (rc's wait was fully
        # replaced by the mock above) -- gate 3 correctly withholds "ready".
        self.assertEqual(evidence["dismiss_call_count"], 0)
        self.assertFalse(evidence["ready"])

    def test_gate_3_requires_dismissal_to_have_actually_fired(self) -> None:
        """Reaching readiness alone must not be enough -- the dismissal spy
        must have fired at least once through rc's own codex-gated branch
        for evidence["ready"] to be True. Simulates rc reaching ready WITHOUT
        ever dismissing (e.g. a future regression bypassing the branch)."""
        target = TmuxTarget(session="s", window="w", pane="0")
        fake_readiness = rc.RuntimeInputReadiness(
            ready=True, pane_sha256="deadbeef", reason="runtime input readiness confirmed",
        )

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            fake_binding = SimpleNamespace(
                controller=SimpleNamespace(tmux=target),
                account_home=Path(tmp) / "account-home",
                persona="fake-persona",
                launch=SimpleNamespace(harness="codex"),
            )

            with mock.patch.object(m.persona_bringup, "load_binding", return_value=fake_binding), \
                 mock.patch.object(m.persona_bringup, "build_adapter", return_value=object()), \
                 mock.patch.object(
                     rc, "_start_runtime",
                     return_value=rc.RuntimeStart(scope_unit="fake.scope", created_scope=True),
                 ), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(rc, "_capture_visible_runtime_pane", return_value=MODAL_TEXT), \
                 mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=fake_readiness), \
                 mock.patch.object(m, "_teardown_disposable_tmux_context", return_value=[]):
                evidence = m.run_codex_rc_ready_bringup(work_root=work_root, account="testaccount")

        self.assertTrue(evidence["rc_readiness_ready"])
        self.assertEqual(evidence["dismiss_call_count"], 0)
        self.assertFalse(evidence["dismissed_via_rc"])
        self.assertFalse(evidence["ready"])
        self.assertEqual(m.codex_rc_ready_status({**evidence, "mode": "real-cli"}), "deferred")


class CodexRcReadyOutcomeTest(unittest.TestCase):
    def _fake_driver_context(self, tmp: str, target: TmuxTarget, *, executable: Path) -> tuple[Path, object]:
        work_root = Path(tmp) / "work"
        fake_binding = SimpleNamespace(
            controller=SimpleNamespace(tmux=target),
            account_home=Path(tmp) / "account-home",
            persona="fake-persona",
            launch=SimpleNamespace(harness="codex"),
        )

        class FakeAdapter:
            def build_interactive_launch(self, _target: TmuxTarget, *, cwd: Path) -> object:
                return SimpleNamespace(cwd=work_root, argv=(str(executable),), env={})

        return work_root, fake_binding, FakeAdapter()

    def test_codex_rc_ready_accepts_real_cli_ready_composer_when_update_modal_not_offered(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")
        fake_readiness = rc.RuntimeInputReadiness(
            ready=True,
            pane_sha256="readyhash",
            reason="runtime input readiness confirmed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.147.0\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            work_root, fake_binding, fake_adapter = self._fake_driver_context(
                tmp, target, executable=executable,
            )
            with mock.patch.object(m.persona_bringup, "load_binding", return_value=fake_binding), \
                 mock.patch.object(m.persona_bringup, "build_adapter", return_value=fake_adapter), \
                 mock.patch.object(
                     rc,
                     "_start_runtime",
                     return_value=rc.RuntimeStart(scope_unit="fake.scope", created_scope=True),
                 ), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(
                     rc, "_capture_visible_runtime_pane", return_value=READY_COMPOSER_0147_NO_MODAL,
                 ), \
                 mock.patch.object(
                     rc, "_wait_for_runtime_input_ready", return_value=fake_readiness,
                 ), \
                 mock.patch.object(m, "_teardown_disposable_tmux_context", return_value=[]):
                evidence = m.run_codex_rc_ready_bringup(
                    work_root=work_root,
                    account="testaccount",
                    codex_executable=executable,
                )
        self.assertEqual(evidence["rc_ready_outcome"], "dismissal_not_needed")
        self.assertTrue(evidence["ready"])
        self.assertEqual(m.codex_rc_ready_status(evidence), "proven")

    def test_codex_rc_ready_requires_rc_dismissal_when_update_modal_is_present(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")
        fake_readiness = rc.RuntimeInputReadiness(
            ready=True,
            pane_sha256="readyhash",
            reason="runtime input readiness confirmed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.148.1\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            work_root, fake_binding, fake_adapter = self._fake_driver_context(
                tmp, target, executable=executable,
            )
            with mock.patch.object(m.persona_bringup, "load_binding", return_value=fake_binding), \
                 mock.patch.object(m.persona_bringup, "build_adapter", return_value=fake_adapter), \
                 mock.patch.object(
                     rc,
                     "_start_runtime",
                     return_value=rc.RuntimeStart(scope_unit="fake.scope", created_scope=True),
                 ), \
                 mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                 mock.patch.object(rc, "_capture_visible_runtime_pane", return_value=MODAL_TEXT), \
                 mock.patch.object(
                     rc, "_wait_for_runtime_input_ready", return_value=fake_readiness,
                 ), \
                 mock.patch.object(m, "_teardown_disposable_tmux_context", return_value=[]):
                evidence = m.run_codex_rc_ready_bringup(
                    work_root=work_root,
                    account="testaccount",
                    codex_executable=executable,
                )
        self.assertEqual(evidence["rc_ready_outcome"], "failed")
        self.assertFalse(evidence["ready"])
        self.assertEqual(m.codex_rc_ready_status(evidence), "deferred")

    def test_codex_rc_ready_rejects_hidden_unknown_malformed_or_version_mismatch(self) -> None:
        target = TmuxTarget(session="s", window="w", pane="0")
        fake_readiness = rc.RuntimeInputReadiness(
            ready=True,
            pane_sha256="readyhash",
            reason="runtime input readiness confirmed",
        )
        for pane_text, version_text in (
            ("", "codex-cli 0.147.0\n"),
            (MALFORMED_READYISH_TEXT, "codex-cli 0.147.0\n"),
            (READY_COMPOSER_0147_NO_MODAL, "not a codex version\n"),
        ):
            with self.subTest(pane=repr(pane_text), version=version_text.strip()):
                with tempfile.TemporaryDirectory() as tmp:
                    executable = Path(tmp) / "codex"
                    executable.write_text(
                        f"#!/bin/sh\nprintf {version_text!r}\n", encoding="utf-8",
                    )
                    executable.chmod(0o755)
                    work_root, fake_binding, fake_adapter = self._fake_driver_context(
                        tmp, target, executable=executable,
                    )
                    with mock.patch.object(m.persona_bringup, "load_binding", return_value=fake_binding), \
                         mock.patch.object(m.persona_bringup, "build_adapter", return_value=fake_adapter), \
                         mock.patch.object(
                             rc,
                             "_start_runtime",
                             return_value=rc.RuntimeStart(scope_unit="fake.scope", created_scope=True),
                         ), \
                         mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                         mock.patch.object(rc, "_capture_visible_runtime_pane", return_value=pane_text), \
                         mock.patch.object(
                             rc, "_wait_for_runtime_input_ready", return_value=fake_readiness,
                         ), \
                         mock.patch.object(m, "_teardown_disposable_tmux_context", return_value=[]):
                        evidence = m.run_codex_rc_ready_bringup(
                            work_root=work_root,
                            account="testaccount",
                            codex_executable=executable,
                        )
                self.assertEqual(evidence["rc_ready_outcome"], "failed")
                self.assertFalse(evidence["ready"])

    def test_version_timeout_and_executable_path_mismatch_are_typed_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            executable.chmod(0o755)
            evidence = m.collect_codex_executable_version_evidence(executable, timeout_s=0.01)
            self.assertTrue(evidence.timed_out)
            self.assertFalse(evidence.ok)
            readiness = rc.RuntimeInputReadiness(
                ready=True,
                pane_sha256="readyhash",
                reason="runtime input readiness confirmed",
            )
            outcome, reason = m._classify_rc_ready_outcome(
                mode=m._MODE_REAL_CLI,
                codex_executable=executable,
                version_evidence=evidence,
                captured_samples=[READY_COMPOSER_0147_NO_MODAL],
                readiness=readiness,
                update_dismiss_call_count=0,
            )
            self.assertEqual(outcome, "failed")
            self.assertIn("timed out", reason)

            mismatch = m.CodexExecutableVersionEvidence(
                executable=Path(tmp) / "other-codex",
                argv=(str(Path(tmp) / "other-codex"), "--version"),
                returncode=0,
                stdout_sha256="0" * 64,
                stderr_sha256=None,
                stdout_excerpt="codex-cli 0.148.0\n",
                stderr_excerpt="",
                parsed_semver="0.148.0",
                timed_out=False,
                ok=True,
                reason="version evidence parsed",
            )
            outcome, reason = m._classify_rc_ready_outcome(
                mode=m._MODE_REAL_CLI,
                codex_executable=executable,
                version_evidence=mismatch,
                captured_samples=[READY_COMPOSER_0147_NO_MODAL],
                readiness=readiness,
                update_dismiss_call_count=0,
            )
            self.assertEqual(outcome, "failed")
            self.assertIn("does not match", reason)


# --------------------------------------------------------------------------- #
# Live disposable-scope integration (real tmux + real systemd --user, safe   #
# stand-in only). Mirrors hcr's own CodexReadyDisposableScopeLiveTests --    #
# this is the actual live proof of the rc-path wiring.                       #
# --------------------------------------------------------------------------- #


class CodexRcReadyDisposableScopeLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        _skip_unless_disposable_tmux_available(self)

    def test_stand_in_mode_drives_real_rc_path_and_tears_down_cleanly(self) -> None:
        account = getpass.getuser()
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "codex-rc-ready"
            evidence = m.run_codex_rc_ready_bringup(work_root=work_root, account=account)

            self.assertEqual(evidence["mode"], m._MODE_STAND_IN)

            # Gate 1: the modal was really on the live pane.
            self.assertTrue(evidence["modal_confirmed_present"])
            # Gate 2: a real dismissal fired through rc's own codex-gated
            # branch (the spy wraps the REAL codex_modal.dismiss_codex_
            # update_modal -- never a stub).
            self.assertTrue(evidence["dismissed_via_rc"])
            self.assertGreaterEqual(evidence["dismiss_call_count"], 1)
            # rc's own real readiness function reached ready via its own
            # success reason -- not a modal-dismissal-failed path.
            self.assertTrue(evidence["rc_readiness_ready"])
            self.assertEqual(evidence["rc_readiness_reason"], "runtime input readiness confirmed")
            self.assertTrue(evidence["ready"])

            # Gate 3 (structural false-green guard): stand-in never reads
            # "proven" regardless of how clean the run was.
            self.assertEqual(m.codex_rc_ready_status(evidence), "deferred")

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
