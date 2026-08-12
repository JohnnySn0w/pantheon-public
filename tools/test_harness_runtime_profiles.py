"""Focused tests for the T-2382 Increment 1 runtime-profile seam."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.harness_adapter.diagnostics import (
    BasicHarnessDiagnosticProbe,
    RuntimeProfileError,
    resolve_controller_config,
    resolve_launch_config,
)
from tools.harness_adapter.provider_quota import (
    ACCEPTANCE_OVERLAY_SCHEMA,
    ClaudeAcceptanceQuotaOverlayObserver,
    ClaudeStatuslineQuotaObserver,
    UnavailableQuotaObserver,
)
from tools.harness_adapter.quota import FailoverPolicy, decide_failover
from tools.harness_adapter.types import ExecutionRequest, HarnessLaunchConfig, TmuxTarget


REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _make_executable(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_acceptance_overlay(root: Path, run_id: str) -> tuple[Path, Path, Path]:
    state_dir = root / "state"
    canonical = state_dir / "quota" / "claude-statusline.json"
    overlay = state_dir / "acceptance" / run_id / "quota-overlay.json"
    canonical_bytes = b'{"canonical":true}\n'
    canonical.parent.mkdir(parents=True)
    overlay.parent.mkdir(parents=True)
    canonical.write_bytes(canonical_bytes)
    overlay.write_text(
        json.dumps(
            {
                "schema": ACCEPTANCE_OVERLAY_SCHEMA,
                "run_id": run_id,
                "created_at": _iso(NOW - timedelta(seconds=1)),
                "canonical_path": str(canonical.resolve()),
                "canonical_sha256_at_install": _sha256_bytes(canonical_bytes),
                "payload": {
                    "observed_at": _iso(NOW),
                    "limits": {
                        "weekly": {
                            "remaining_percent": 75.0,
                            "resets_at": _iso(NOW + timedelta(days=1)),
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    overlay.chmod(0o600)
    return state_dir, canonical, overlay


def _manifest(exe: str = "{account_home}/bin/claude") -> dict:
    return {
        "manifest_version": 2,
        "harness_runtime": {
            "schema": "pantheon-harness-runtime/v1",
            "controller": {
                "tmux": {
                    "session": "pantheon-{persona}",
                    "window": "harness",
                    "pane": "0",
                },
                "quota": {"weekly_remaining_percent": 5},
            },
            "harnesses": {
                "claude": {
                    "default_profile": "interactive-subscription",
                    "profiles": {
                        "interactive-subscription": {
                            "executable": exe,
                            "mode": "interactive",
                            "meter": "subscription",
                            "quota_source": "statusline",
                        },
                        "batch-sdk": {
                            "executable": exe,
                            "mode": "batch",
                            "meter": "sdk-credit",
                            "quota_source": "unavailable",
                        },
                    },
                },
                "codex": {
                    "default_profile": "interactive-chatgpt",
                    "profiles": {
                        "interactive-chatgpt": {
                            "executable": "/tmp/fake-codex",
                            "mode": "interactive",
                            "meter": "subscription",
                            "quota_source": "app-server",
                        }
                    },
                },
            },
            "roles": {
                "recovery": {
                    "fallbacks": ["codex/interactive-chatgpt"],
                    "claude": {
                        "profile": "interactive-subscription",
                        "model": "manifest-model",
                        "native": {"permission_mode": "default"},
                    },
                    "codex": {
                        "profile": "interactive-chatgpt",
                        "model": None,
                        "native": {
                            "sandbox": "workspace-write",
                            "config_overrides": {"approval_policy": "never"},
                        },
                    },
                }
            },
        },
        "roles": {
            "recovery": {
                "persona": "recovery",
                "account": "claude-fallback",
                "harness": "claude-code",
            }
        },
    }


class RuntimeProfileResolutionTests(unittest.TestCase):
    def test_manifest_runtime_resolves_absolute_executable_for_bound_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp)
            exe = _make_executable(account_home, "bin/claude")
            launch = resolve_launch_config(
                _manifest(),
                role="recovery",
                account_home=account_home,
                selected_harness="claude-code",
            )
            self.assertEqual(launch.executable, exe)
            self.assertTrue(launch.executable.is_absolute())

    def test_env_clean_diagnosis_and_exec_use_identical_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp)
            exe = _make_executable(account_home, "bin/claude")
            launch = resolve_launch_config(
                _manifest(),
                role="recovery",
                account_home=account_home,
                selected_harness="claude",
            )
            adapter = ClaudeHarnessAdapter(
                "recovery",
                claude_home=account_home,
                launch=launch,
            )
            with mock.patch.dict(os.environ, {"PATH": "/does/not/exist"}, clear=True):
                report = BasicHarnessDiagnosticProbe(
                    adapter,
                    cwd=REPO_ROOT,
                    execution_context="clean-env",
                ).collect()
            executable_obs = next(
                obs for obs in report.observations if obs.check_id == "claude.executable"
            )
            command = adapter.build_interactive_launch(
                TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
            )
            self.assertEqual(executable_obs.evidence["path"], str(exe))
            self.assertEqual(command.argv[0], str(exe))
            self.assertEqual(command.argv.count("--permission-mode"), 1)
            self.assertEqual(command.argv[-2:], ("--permission-mode", "default"))

    def test_missing_configured_executable_fails_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp)
            launch = resolve_launch_config(
                _manifest(),
                role="recovery",
                account_home=account_home,
                selected_harness="claude",
            )
            adapter = ClaudeHarnessAdapter(
                "recovery",
                claude_home=account_home,
                launch=launch,
            )
            with mock.patch("subprocess.Popen") as popen:
                report = BasicHarnessDiagnosticProbe(
                    adapter,
                    cwd=REPO_ROOT,
                    execution_context="no-spawn",
                ).collect()
            executable_obs = next(
                obs for obs in report.observations if obs.check_id == "claude.executable"
            )
            self.assertEqual(executable_obs.status, "fail")
            popen.assert_not_called()

    def test_unknown_native_launch_key_fails_closed(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["claude"]["native"] = {
            "sandbox": "workspace-write"
        }
        with self.assertRaisesRegex(RuntimeProfileError, "sandbox"):
            resolve_launch_config(
                manifest,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

    def test_role_harness_change_selects_corresponding_runtime_without_identity_change(self) -> None:
        manifest = _manifest()
        claude = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        codex = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="codex",
        )
        controller_a = resolve_controller_config(
            manifest,
            persona="recovery",
            role="recovery",
        )
        controller_b = resolve_controller_config(
            manifest,
            persona="recovery",
            role="recovery",
        )
        self.assertEqual((claude.harness, claude.profile), ("claude", "interactive-subscription"))
        self.assertEqual((codex.harness, codex.profile), ("codex", "interactive-chatgpt"))
        self.assertEqual(controller_a.tmux, controller_b.tmux)
        self.assertEqual(manifest["roles"]["recovery"]["persona"], "recovery")

    def test_model_override_precedence_is_explicit_and_warned(self) -> None:
        launch = resolve_launch_config(
            _manifest(),
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
            model_override="operator-model",
        )
        self.assertEqual(launch.model, "operator-model")
        self.assertIn("override.model", launch.sources)

    def test_generic_systemd_unit_contains_no_vendor_binary_or_model_flag(self) -> None:
        text = (REPO_ROOT / "tools/systemd/pantheon-persona-consume@.service").read_text(
            encoding="utf-8"
        )
        exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1)
        exec_line = exec_lines[0]
        self.assertIn("tools/persona_bringup.py consume --persona %i", exec_line)
        self.assertNotIn(" --model ", exec_line)
        self.assertNotIn("/bin/claude", exec_line)
        self.assertNotIn("/bin/codex", exec_line)

    def test_claude_default_profile_is_interactive_subscription_not_batch_sdk(self) -> None:
        launch = resolve_launch_config(
            _manifest(),
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        adapter = ClaudeHarnessAdapter("recovery", launch=launch)
        command = adapter.build_interactive_launch(
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        )
        self.assertEqual(launch.profile, "interactive-subscription")
        self.assertEqual(launch.mode, "interactive")
        self.assertNotIn("-p", command.argv)
        self.assertEqual(command.argv.count("--permission-mode"), 1)
        self.assertEqual(command.argv[-2:], ("--permission-mode", "default"))

    def test_claude_non_default_permission_mode_is_emitted_once(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["claude"][
            "native"
        ]["permission_mode"] = "acceptEdits"
        launch = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        command = ClaudeHarnessAdapter(
            "recovery", launch=launch
        ).build_interactive_launch(
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        )
        self.assertEqual(command.argv.count("--permission-mode"), 1)
        self.assertEqual(command.argv[-1], "acceptEdits")

    def test_claude_permission_mode_must_be_non_empty_string(self) -> None:
        for value in ("", None, 7):
            with self.subTest(value=value):
                manifest = _manifest()
                manifest["harness_runtime"]["roles"]["recovery"]["claude"][
                    "native"
                ]["permission_mode"] = value
                with self.assertRaisesRegex(RuntimeProfileError, "permission_mode"):
                    resolve_launch_config(
                        manifest,
                        role="recovery",
                        account_home=Path("/tmp/account"),
                        selected_harness="claude",
                    )

    def test_claude_channels_unset_argv_is_byte_identical_to_baseline(self) -> None:
        # T-2602 default-preserving guard: every current caller omits channels,
        # so the full argv tuple must be unchanged, not merely "no --channels".
        launch = resolve_launch_config(
            _manifest(),
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        command = ClaudeHarnessAdapter(
            "recovery", launch=launch
        ).build_interactive_launch(
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        )
        self.assertEqual(
            command.argv,
            (
                str(launch.executable),
                "--model",
                "manifest-model",
                "--permission-mode",
                "default",
            ),
        )
        self.assertNotIn("--channels", command.argv)

    def test_claude_channels_is_emitted_once(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["claude"][
            "native"
        ]["channels"] = "plugin:telegram@claude-plugins-official"
        launch = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        command = ClaudeHarnessAdapter(
            "recovery", launch=launch
        ).build_interactive_launch(
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        )
        self.assertEqual(command.argv.count("--channels"), 1)
        self.assertEqual(
            command.argv,
            (
                str(launch.executable),
                "--channels",
                "plugin:telegram@claude-plugins-official",
                "--model",
                "manifest-model",
                "--permission-mode",
                "default",
            ),
        )

    def test_claude_channels_must_be_non_empty_string(self) -> None:
        for value in ("", None, 7):
            with self.subTest(value=value):
                manifest = _manifest()
                manifest["harness_runtime"]["roles"]["recovery"]["claude"][
                    "native"
                ]["channels"] = value
                with self.assertRaisesRegex(RuntimeProfileError, "channels"):
                    resolve_launch_config(
                        manifest,
                        role="recovery",
                        account_home=Path("/tmp/account"),
                        selected_harness="claude",
                    )

    def test_codex_native_channels_key_still_rejected(self) -> None:
        # T-2602 is claude-only (codex has no --channels analog); confirm the
        # codex native allowlist ({sandbox, config_overrides}) is untouched.
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["codex"][
            "native"
        ]["channels"] = "plugin:telegram@claude-plugins-official"
        with self.assertRaisesRegex(RuntimeProfileError, "channels"):
            resolve_launch_config(
                manifest,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="codex",
            )

    def test_batch_sdk_requires_explicit_profile_and_distinct_meter(self) -> None:
        launch = resolve_launch_config(
            _manifest(),
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
            profile_override="batch-sdk",
        )
        adapter = ClaudeHarnessAdapter("recovery", launch=launch)
        command = adapter.build_exec(ExecutionRequest(prompt="hello", cwd=REPO_ROOT))
        self.assertEqual(launch.meter, "sdk-credit")
        self.assertEqual(launch.mode, "batch")
        self.assertIn("-p", command.argv)

    def test_meter_quota_source_pair_fails_closed_when_incompatible(self) -> None:
        manifest = _manifest()
        profile = manifest["harness_runtime"]["harnesses"]["claude"]["profiles"][
            "interactive-subscription"
        ]
        profile["quota_source"] = "app-server"
        with self.assertRaisesRegex(RuntimeProfileError, "incompatible"):
            resolve_launch_config(
                manifest,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

    def test_tmux_target_is_persona_stable_across_harness_and_model_changes(self) -> None:
        manifest = _manifest()
        first = resolve_controller_config(manifest, persona="recovery", role="recovery")
        manifest["harness_runtime"]["roles"]["recovery"]["claude"][
            "model"
        ] = "different"
        second = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(first.tmux, second.tmux)
        self.assertEqual(first.tmux.session, "pantheon-recovery")

    def test_role_without_tmux_override_matches_global_template_byte_identical(self) -> None:
        manifest = _manifest()
        self.assertNotIn("tmux", manifest["harness_runtime"]["roles"]["recovery"])
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(
            controller.tmux,
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0"),
        )

    def test_role_tmux_override_replaces_session_only(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = {
            "session": "claude-telegram-bot-fallback"
        }
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(
            controller.tmux,
            TmuxTarget(session="claude-telegram-bot-fallback", window="harness", pane="0"),
        )

    def test_role_tmux_override_covers_window_and_pane(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = {
            "session": "claude-telegram-bot-fallback",
            "window": "fallback-window",
            "pane": "1",
        }
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(
            controller.tmux,
            TmuxTarget(session="claude-telegram-bot-fallback", window="fallback-window", pane="1"),
        )

    def test_role_tmux_override_rejects_non_mapping(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = "bogus"
        with self.assertRaisesRegex(RuntimeProfileError, "must be a mapping"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_tmux_override_rejects_unknown_key(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = {"bogus": "x"}
        with self.assertRaisesRegex(RuntimeProfileError, "not recognized"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_tmux_override_rejects_empty_string(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = {"session": ""}
        with self.assertRaisesRegex(RuntimeProfileError, "non-empty string"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_tmux_override_rejects_runtime_variable(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["tmux"] = {
            "session": "{harness}-x"
        }
        with self.assertRaisesRegex(RuntimeProfileError, "must not vary"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_without_post_start_matches_no_hooks_byte_identical(self) -> None:
        manifest = _manifest()
        self.assertNotIn("post_start", manifest["harness_runtime"]["roles"]["recovery"])
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(controller.post_start, ())

    def test_role_post_start_declares_command_list(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["{account_home}/pantheon-config/tools/arm_pane_log.sh", "{tmux_target}", "{persona}"]},
        ]
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(len(controller.post_start), 1)
        entry = controller.post_start[0]
        self.assertEqual(
            entry.argv,
            (
                "{account_home}/pantheon-config/tools/arm_pane_log.sh",
                "{tmux_target}",
                "{persona}",
            ),
        )
        self.assertFalse(entry.background)

    def test_role_post_start_background_flag_parses(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {
                "command": ["{account_home}/pantheon-config/tools/recovery_boot_kick.sh", "{tmux_target}", "{persona}"],
                "background": True,
            },
        ]
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertTrue(controller.post_start[0].background)

    def test_role_post_start_preserves_declared_order(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["first"]},
            {"command": ["second"], "background": True},
        ]
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(controller.post_start[0].argv, ("first",))
        self.assertEqual(controller.post_start[1].argv, ("second",))
        self.assertTrue(controller.post_start[1].background)

    def test_role_post_start_rejects_non_list(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = "bogus"
        with self.assertRaisesRegex(RuntimeProfileError, "must be a list"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_non_mapping_entry(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = ["bogus"]
        with self.assertRaisesRegex(RuntimeProfileError, "must be a mapping"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_unknown_key(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["x"], "bogus": True}
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "not recognized"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_empty_command(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": []}
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "non-empty list"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_non_string_command_items(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["ok", 5]}
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "non-empty list"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_unknown_substitution_token(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["{bogus}"]}
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "supports only"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_role_post_start_rejects_non_bool_background(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["post_start"] = [
            {"command": ["x"], "background": "yes"}
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "must be a bool"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")

    def test_fallback_profile_must_exist_and_cannot_self_cycle(self) -> None:
        missing = _manifest()
        missing["harness_runtime"]["roles"]["recovery"]["fallbacks"] = [
            "codex/missing"
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "does not exist"):
            resolve_launch_config(
                missing,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

        cycle = _manifest()
        cycle["harness_runtime"]["roles"]["recovery"]["fallbacks"] = [
            "codex/interactive-chatgpt",
            "codex/interactive-chatgpt",
        ]
        with self.assertRaisesRegex(RuntimeProfileError, "self-cycles"):
            resolve_launch_config(
                cycle,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

    def test_role_with_no_fallbacks_key_resolves_cleanly(self) -> None:
        manifest = _manifest()
        del manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"]
        launch = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        self.assertEqual(launch.harness, "claude")

    def test_role_fallbacks_null_resolves_cleanly(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"] = None
        launch = resolve_launch_config(
            manifest,
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness="claude",
        )
        self.assertEqual(launch.harness, "claude")

    def test_role_fallbacks_rejects_non_list(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"] = "codex/interactive-chatgpt"
        with self.assertRaisesRegex(RuntimeProfileError, "must be strings"):
            resolve_launch_config(
                manifest,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

    def test_role_fallbacks_rejects_non_string_items(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"] = [5]
        with self.assertRaisesRegex(RuntimeProfileError, "must be strings"):
            resolve_launch_config(
                manifest,
                role="recovery",
                account_home=Path("/tmp/account"),
                selected_harness="claude",
            )

    # -- T-2635: resolve_controller_config() carries its OWN separate copy of
    # the fallbacks validation (_validate_fallbacks above guards
    # resolve_launch_config only) -- and that copy's "no fallbacks key"/"null
    # fallbacks" defaults were empty TUPLEs, which unconditionally fail the
    # very next `isinstance(raw_fallbacks, list)` check. Confirmed live
    # against the real instance.yaml: every persona except recovery (whose
    # recovery role happens to declare an explicit fallbacks list)
    # raised RuntimeProfileError merely from resolving their controller
    # config, discovered chasing T-2633's Specialist cutover (specialist has no
    # fallbacks key, by design -- Specialist was the first persona to actually
    # exercise this path). These two tests are the coverage gap that let the
    # second, buggy copy of this logic through; they mirror
    # test_role_with_no_fallbacks_key_resolves_cleanly /
    # test_role_fallbacks_null_resolves_cleanly above, against
    # resolve_controller_config instead of resolve_launch_config.
    def test_controller_config_role_with_no_fallbacks_key_resolves_cleanly(self) -> None:
        manifest = _manifest()
        del manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"]
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(controller.policy.fallbacks, ())

    def test_controller_config_role_fallbacks_null_resolves_cleanly(self) -> None:
        manifest = _manifest()
        manifest["harness_runtime"]["roles"]["recovery"]["fallbacks"] = None
        controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
        self.assertEqual(controller.policy.fallbacks, ())


class RuntimeLaunchAndDescriptorTests(unittest.TestCase):
    def _launch(self, harness: str) -> HarnessLaunchConfig:
        return resolve_launch_config(
            _manifest(),
            role="recovery",
            account_home=Path("/tmp/account"),
            selected_harness=harness,
        )

    def test_claude_interactive_launch_contains_no_print_flag(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
        command = adapter.build_interactive_launch(
            TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        )
        self.assertNotIn("-p", command.argv)
        self.assertNotIn("--print", command.argv)

    def test_codex_interactive_launch_uses_same_operator_tmux_plane(self) -> None:
        adapter = CodexHarnessAdapter("recovery", launch=self._launch("codex"))
        target = TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
        command = adapter.build_interactive_launch(target)
        self.assertEqual(command.env["PANTHEON_TMUX_SESSION"], target.session)
        self.assertEqual(command.env["PANTHEON_TMUX_WINDOW"], target.window)
        self.assertNotIn("exec", command.argv)

    def test_claude_quota_observer_uses_persona_state_not_terminal_text(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
        with mock.patch.dict(os.environ, {}, clear=True):
            observer = adapter.quota_observer(Path("/tmp/persona-state"))
        self.assertIsInstance(observer, ClaudeStatuslineQuotaObserver)
        self.assertEqual(
            observer.path,
            Path("/tmp/persona-state/quota/claude-statusline.json"),
        )
        self.assertEqual(observer.profile, "claude/interactive-subscription")

    def test_claude_quota_observer_one_acceptance_var_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            canonical = state_dir / "quota" / "claude-statusline.json"
            adapter = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
            with mock.patch.dict(
                os.environ,
                {"PANTHEON_ACCEPTANCE_RUN_ID": "20260803T120000Z"},
                clear=True,
            ):
                observer = adapter.quota_observer(state_dir)
                snapshot = observer.read()

            self.assertIsInstance(observer, UnavailableQuotaObserver)
            self.assertEqual(snapshot.freshness, "unavailable")
            self.assertEqual(snapshot.evidence["reason"], "invalid-acceptance-overlay-config")
            self.assertFalse(canonical.exists())

    def test_claude_quota_observer_uses_valid_acceptance_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_id = "20260803T120000Z"
            state_dir, _, overlay = _write_acceptance_overlay(Path(tmp), run_id)
            adapter = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
            with mock.patch.dict(
                os.environ,
                {
                    "PANTHEON_ACCEPTANCE_RUN_ID": run_id,
                    "PANTHEON_ACCEPTANCE_QUOTA_OVERLAY": str(overlay),
                },
                clear=True,
            ):
                observer = adapter.quota_observer(state_dir)

            self.assertIsInstance(observer, ClaudeAcceptanceQuotaOverlayObserver)
            self.assertEqual(observer.path, overlay)
            self.assertEqual(observer.run_id, run_id)

    def test_claude_quota_observer_rejects_parent_traversal_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir, _, valid_overlay = _write_acceptance_overlay(
                Path(tmp), "20260803T120000Z"
            )
            envelope = json.loads(valid_overlay.read_text(encoding="utf-8"))
            envelope["run_id"] = ".."
            overlay = state_dir / "quota-overlay.json"
            overlay.write_text(json.dumps(envelope), encoding="utf-8")
            overlay.chmod(0o600)
            adapter = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
            with mock.patch.dict(
                os.environ,
                {
                    "PANTHEON_ACCEPTANCE_RUN_ID": "..",
                    "PANTHEON_ACCEPTANCE_QUOTA_OVERLAY": str(overlay),
                },
                clear=True,
            ):
                observer = adapter.quota_observer(state_dir)
                snapshot = observer.read()

            self.assertIsInstance(observer, UnavailableQuotaObserver)
            self.assertEqual(snapshot.freshness, "unavailable")
            self.assertEqual(snapshot.evidence["reason"], "invalid-acceptance-overlay-config")
            self.assertEqual(decide_failover(snapshot, FailoverPolicy()), "unknown")

    def test_codex_quota_observer_uses_app_server_reader(self) -> None:
        adapter = CodexHarnessAdapter(
            "recovery",
            codex_home=Path("/tmp/account"),
            launch=self._launch("codex"),
        )
        observer = adapter.quota_observer(Path("/tmp/persona-state"))
        self.assertEqual(observer.profile, "codex/interactive-chatgpt")
        with mock.patch(
            "tools.harness_adapter.codex.read_codex_app_server_rate_limits",
            return_value=None,
        ) as reader:
            snapshot = observer.read()
        self.assertEqual(snapshot.freshness, "unavailable")
        reader.assert_called_once_with(
            Path("/tmp/fake-codex"),
            codex_home=Path("/tmp/account/.codex"),
        )

    def test_auth_descriptor_never_claims_live_status_without_probe(self) -> None:
        for adapter in (
            ClaudeHarnessAdapter("recovery", launch=self._launch("claude")),
            CodexHarnessAdapter("recovery", launch=self._launch("codex")),
        ):
            with self.subTest(adapter=adapter.HARNESS_NAME):
                descriptor = adapter.auth_descriptor()
                self.assertEqual(descriptor.observed_state, "unverified")
                self.assertFalse(descriptor.authoritative)
                self.assertTrue(adapter.auth_status()["descriptor_only"])

    def test_memory_descriptor_reports_available_disabled_without_enabling(self) -> None:
        descriptor = CodexHarnessAdapter(
            "recovery",
            launch=self._launch("codex"),
        ).describe_surfaces(REPO_ROOT).memory
        self.assertTrue(descriptor.available)
        self.assertFalse(descriptor.enabled)

    def test_mcp_descriptor_preserves_native_scope_and_redacts_auth_material(self) -> None:
        descriptor = CodexHarnessAdapter(
            "recovery",
            codex_home=Path("/tmp/account"),
            launch=self._launch("codex"),
        ).describe_surfaces(REPO_ROOT).mcp
        self.assertEqual(descriptor.scope, ("user",))
        self.assertIn("config.toml", str(descriptor.config_paths[0]))
        self.assertNotIn("auth.json", str(descriptor.evidence))

    def test_role_descriptor_requires_current_schema_and_preserves_native_modes(self) -> None:
        descriptor = CodexHarnessAdapter(
            "recovery",
            codex_home=Path("/tmp/account"),
            launch=self._launch("codex"),
        ).describe_surfaces(REPO_ROOT).roles
        self.assertIn("multi_agent.spawn_agent", descriptor.delegation_modes)
        self.assertEqual(descriptor.evidence["schema"], "codex-config-toml")

    def test_namespaced_probe_records_execution_context_and_raw_exit(self) -> None:
        adapter = CodexHarnessAdapter("recovery", launch=self._launch("codex"))
        report = BasicHarnessDiagnosticProbe(
            adapter,
            cwd=REPO_ROOT,
            execution_context="unit-test-context",
        ).collect()
        executable_obs = next(
            obs for obs in report.observations if obs.check_id == "codex.executable"
        )
        self.assertEqual(executable_obs.evidence["execution_context"], "unit-test-context")
        self.assertEqual(executable_obs.evidence["raw_exit"], "not-run")

    def test_not_abstracted_surfaces_remain_provider_native(self) -> None:
        claude = ClaudeHarnessAdapter("recovery", launch=self._launch("claude"))
        codex = CodexHarnessAdapter("recovery", launch=self._launch("codex"))
        self.assertIn("permission", claude.sandbox_config["mechanism"])
        self.assertEqual(codex.sandbox_config["approval_policy"], "never")
        self.assertNotEqual(claude.profiles_config["mechanism"], codex.profiles_config["mechanism"])


class RuntimeManifestFileTests(unittest.TestCase):
    def test_instance_yaml_runtime_schema_parses_independently(self) -> None:
        manifest = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
        runtime = manifest["harness_runtime"]
        self.assertEqual(runtime["schema"], "pantheon-harness-runtime/v1")
        for role, binding in manifest["roles"].items():
            harness = binding.get("harness")
            if not isinstance(harness, str):
                continue
            normalized = "claude" if harness == "claude-code" else harness
            if role in runtime["roles"]:
                self.assertIn(normalized, runtime["roles"][role])

    def test_tmux_template_rejects_runtime_variable(self) -> None:
        manifest = copy.deepcopy(_manifest())
        manifest["harness_runtime"]["controller"]["tmux"]["session"] = "{persona}-{harness}"
        with self.assertRaisesRegex(RuntimeProfileError, "must not vary"):
            resolve_controller_config(manifest, persona="recovery", role="recovery")


if __name__ == "__main__":
    unittest.main()
