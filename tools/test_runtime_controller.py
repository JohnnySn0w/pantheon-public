"""Deterministic tests for the persistent persona runtime controller."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pwd
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.harness_adapter.types import (
    CommandSpec,
    DeliveryReceipt,
    DiagnosticReport,
    ExecutionRequest,
    FailoverPolicy,
    HarnessLaunchConfig,
    PostStartCommand,
    RuntimeControllerConfig,
    TmuxTarget,
)
from tools import codex_modal
from tools import runtime_controller as rc
from tools import mailbox
from tools.harness_adapter.types import ExecutionResult, SessionHandle
from tools.harness_adapter import quota as runtime_quota
from tools import persona_bringup
from tools.claude_quota_probe import (
    ClaudeQuotaProbeError,
    PROMPT as CLAUDE_QUOTA_PROBE_PROMPT,
    PROBE_SCHEMA as CLAUDE_QUOTA_PROBE_SCHEMA,
    ProbeResult as ClaudeQuotaProbeResult,
    probe_result_path as claude_probe_result_path,
    probe_session_id as claude_probe_session_id,
    probe_tmux_target as claude_probe_tmux_target,
)
from tools.persona_bringup import consume_persona_mailbox, wake_persistent_runtime


def _binding(
    root: Path,
    *,
    harness: str = "codex",
    profile: str = "interactive",
    fallbacks: tuple[str, ...] = (),
    post_start: tuple = (),
):
    executable = Path("/usr/bin/codex" if harness == "codex" else "/usr/bin/claude")
    return SimpleNamespace(
        persona="recovery",
        role="recovery",
        account=pwd.getpwuid(os.geteuid()).pw_name,
        account_home=root,
        mailbox=root / "mail" / "recovery",
        selected_harness=harness,
        configured_harness=harness,
        launch=HarnessLaunchConfig(
            harness=harness,
            profile=profile,
            executable=executable,
            model=None,
            mode="interactive",
            meter="subscription",
            quota_source="app-server" if harness == "codex" else "statusline",
            native={},
            sources=("test",),
        ),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget("pantheon-recovery", "harness", "0"),
            policy=FailoverPolicy(5.0, fallbacks),
            post_start=post_start,
        ),
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fixture_source_provenance(**kwargs) -> rc.RuntimeSourceProvenance:
    source_hashes = {relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS}
    return rc.RuntimeSourceProvenance(
        revision="1" * 40,
        source_hashes=source_hashes,
        source_manifest_sha256=rc._source_manifest_hash(source_hashes),
        runtime_start_boundary_at=kwargs["runtime_start_boundary_at"],
        runtime_start_observed_at=kwargs["runtime_start_observed_at"],
    )


def _provenance_patch():
    return mock.patch.object(
        rc,
        "_controller_source_provenance",
        side_effect=lambda **kwargs: _fixture_source_provenance(**kwargs),
    )


class _Adapter:
    def __init__(self, binding, quota_snapshot=None) -> None:
        self.binding = binding
        self.quota_snapshot = quota_snapshot or runtime_quota.QuotaSnapshot(
            profile=f"{binding.launch.harness}/{binding.launch.profile}",
            windows=(),
            observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            freshness="unavailable",
            evidence={"reason": "test-unavailable"},
        )

    def build_interactive_launch(self, target: TmuxTarget) -> CommandSpec:
        return CommandSpec(
            argv=(str(self.binding.launch.executable),),
            stdin=None,
            cwd=Path("/tmp"),
        )

    def quota_observer(self, state_dir: Path):
        del state_dir
        return SimpleNamespace(read=lambda: self.quota_snapshot)


class RuntimeLifecycleTests(unittest.TestCase):
    def test_recorded_runtime_handle_never_starts_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(rc, "_start_runtime") as start:
                handle = rc.recorded_runtime_handle(binding, state_root=root)

            self.assertIsNone(handle.session_id)
            self.assertEqual(handle.profile, binding.launch)
            start.assert_not_called()

    def test_replacement_creates_missing_stable_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(rc, "_target_known", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ) as start, mock.patch.object(
                rc, "_verify_started_runtime"
            ) as verify, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                rc._replace_runtime(binding, state_root=root)

            start.assert_called_once_with(binding, mock.ANY, replace=False)
            verify.assert_called_once()

    def test_ensure_runtime_starts_and_records_exact_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ) as start, mock.patch.object(
                rc, "_verify_started_runtime"
            ) as verify, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                handle = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            start.assert_called_once_with(binding, mock.ANY, replace=False)
            verify.assert_called_once()
            self.assertEqual(handle.profile.profile, "interactive")
            state = json.loads(
                (root / "recovery" / "runtime-controller" / "runtime.json").read_text()
            )
            self.assertEqual(state["executable"], "/usr/bin/codex")
            self.assertEqual(state["tmux_target"], "pantheon-recovery:harness.0")

    def test_ensure_runtime_reuses_matching_healthy_tmux_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(
                rc, "_pane_exists", side_effect=(False, True)
            ), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ) as start, mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(
                rc, "_pid_in_scope", return_value=True
            ), _provenance_patch():
                first = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
                second = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            start.assert_called_once_with(binding, mock.ANY, replace=False)
            self.assertEqual(second.profile, first.profile)
            self.assertEqual(second.tmux, first.tmux)

    def test_ensure_runtime_respawns_target_session_when_companion_keeps_server_alive(
        self,
    ) -> None:
        """T-2658 load-bearing correctness proof (team-lead requirement A).

        The control-plane restart primitive (tools/handoff_and_restart.sh
        --supervisor control-plane) is "tmux kill-session on the TARGET
        session only, then fire pantheon-persona-consume@<persona>". Specialist's
        acp-bridge companion unit (T-2648) holds its OWN tmux session on the
        SAME default tmux socket/server, so killing only specialist's session
        leaves the default server alive, owned by the SAME control-plane
        scope. This test proves the resulting ensure_runtime() call takes
        the "server alive, in scope, target session dead" branch of
        _start_runtime -- respawning a FRESH session onto the EXISTING scope,
        never recreating the scope from scratch (_start_new_server_in_scope
        must NOT be called) -- which is exactly what makes companion-unit
        teardown/rejoin unnecessary for a plain restart (unlike the
        from-scratch T-2633 cutover, which owns the whole server).

        Sibling proof at the _start_runtime level (same branch, narrower
        entrypoint):
        test_existing_scoped_server_creates_session_without_second_scope.
        This test exercises the full ensure_runtime() production entrypoint
        (the one persona_bringup.py consume actually calls) and additionally
        asserts the resulting runtime.json state, closing the gap between
        "the branch takes the right tmux action" and "the persisted state
        after a restart is correct".
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
            with mock.patch.object(
                rc, "_pane_exists", return_value=False
            ), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(
                rc, "_pid_in_scope", return_value=True
            ), mock.patch.object(
                rc, "_target_known", return_value=False
            ), mock.patch.object(
                rc, "_session_exists", return_value=False
            ), mock.patch.object(
                rc, "_run_tmux", return_value=completed
            ) as tmux, mock.patch.object(
                rc, "_start_new_server_in_scope"
            ) as scoped_start, mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                handle = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            # The respawn landed via new-session on the EXISTING scope-owned
            # server -- never via a second, from-scratch scope.
            self.assertEqual(tmux.call_args.args[0], "new-session")
            scoped_start.assert_not_called()
            self.assertEqual(handle.tmux, binding.controller.tmux)
            state = json.loads(
                (root / "recovery" / "runtime-controller" / "runtime.json").read_text()
            )
            self.assertEqual(state["runtime_scope_unit"], rc.TMUX_RUNTIME_SCOPE_UNIT)
            self.assertEqual(state["tmux_target"], "pantheon-recovery:harness.0")
            self.assertEqual(state["last_structural_event"], "runtime_started")

    def test_ensure_runtime_rejects_live_profile_mismatch_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            rc._atomic_json(
                state_path,
                {
                    "persona": "recovery",
                    "role": "recovery",
                    "harness": "claude",
                    "profile": "other",
                    "executable": "/usr/bin/claude",
                    "model": None,
                    "tmux_target": "pantheon-recovery:harness.0",
                },
            )
            with mock.patch.object(rc, "_pane_exists", return_value=True), mock.patch.object(
                rc, "_start_runtime"
            ) as start:
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
            start.assert_not_called()

    def test_open_quota_breaker_prevents_capped_profile_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, profile="primary")
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "completed",
                    "breaker_open": True,
                    "capped_profile": "primary",
                },
            )
            with mock.patch.object(rc, "_pane_exists") as pane, mock.patch.object(
                rc, "_start_runtime"
            ) as start:
                with self.assertRaises(rc.ProviderQuotaExhausted):
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
            pane.assert_not_called()
            start.assert_not_called()
            with self.assertRaises(ValueError):
                rc.clear_profile_breaker(
                    "recovery",
                    "primary",
                    fresh_probe_evidence="",
                    reset_passed=True,
                    state_root=root,
                )
            rc.clear_profile_breaker(
                "recovery",
                "primary",
                fresh_probe_evidence="quota:weekly:fresh:healthy",
                reset_passed=True,
                state_root=root,
            )
            self.assertFalse(rc.transition_state("recovery", state_root=root)["breaker_open"])

    def test_controller_uses_send_keys_submit_helper_not_raw_tmux_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _binding(Path(tmp))
            handle = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                None,
                rc._utc_now(),
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
                receipt = rc.submit_to_runtime(
                    handle,
                    ExecutionRequest(prompt="work", cwd=Path("/tmp")),
                )
            self.assertEqual(receipt.state, "accepted")
            self.assertEqual(receipt.evidence["turn_started"], "unverified")
            self.assertEqual(receipt.evidence["acked"], "false")
            argv = run.call_args.args[0]
            self.assertEqual(Path(argv[0]), rc.SEND_KEYS_HELPER)
            self.assertNotEqual(argv[0], "tmux")
            self.assertEqual(argv[-2:], ["pantheon-recovery:harness.0", "work"])

    def test_submission_confirmation_does_not_claim_turn_completion(self) -> None:
        binding = _binding(Path("/tmp"))
        handle = rc.RuntimeHandle(
            binding.launch,
            binding.controller.tmux,
            None,
            rc._utc_now(),
        )
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=completed):
            receipt = rc.submit_to_runtime(
                handle,
                ExecutionRequest(prompt="work", cwd=Path("/tmp")),
            )

        self.assertEqual(receipt.state, "accepted")
        self.assertEqual(receipt.evidence["turn_started"], "unverified")
        self.assertEqual(receipt.evidence["acked"], "false")

    def test_runtime_state_records_profile_session_and_structural_event_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            state = json.loads(
                (root / "recovery" / "runtime-controller" / "runtime.json").read_text()
            )
            self.assertEqual(state["profile"], "interactive")
            self.assertIn("session_id", state)
            self.assertEqual(state["last_structural_event"], "runtime_started")
            self.assertEqual(
                state["runtime_scope_unit"], rc.TMUX_RUNTIME_SCOPE_UNIT
            )
            self.assertTrue(state["last_structural_event_at"].endswith("Z"))
            self.assertEqual(state["controller_source_revision"], "1" * 40)
            self.assertEqual(
                state["controller_source_manifest_sha256"],
                rc._source_manifest_hash(state["controller_source_hashes"]),
            )
            self.assertTrue(state["runtime_start_boundary_at"].endswith("Z"))
            self.assertTrue(state["runtime_start_observed_at"].endswith("Z"))

    def test_operator_attach_target_survives_harness_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, False
                )
            ) as start, mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                replacement = rc._replace_runtime(target, state_root=root)

            self.assertEqual(current.controller.tmux, target.controller.tmux)
            self.assertEqual(replacement.tmux, current.controller.tmux)
            start.assert_called_once_with(target, mock.ANY, replace=True)

    def test_dead_known_target_is_respawned_without_duplicate_session(self) -> None:
        binding = _binding(Path("/tmp"))
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ) as session_exists, mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed) as run, mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(rc, "ensure_update_check_disabled"), mock.patch.object(
            rc, "ensure_model_migration_prompts_hidden"
        ):
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        session_exists.assert_not_called()
        self.assertEqual(run.call_args.args[0], "respawn-pane")
        self.assertIn("pantheon-recovery:harness.0", run.call_args.args)

    def test_codex_harness_ensures_project_trusted_with_resolved_cwd(self) -> None:
        # T-2594: the TRUST-modal-safety config lever. Must be called with
        # the ACTUAL resolved launch cwd (spec.cwd), never a hardcoded guess.
        binding = _binding(Path("/tmp"), harness="codex")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            rc, "ensure_project_trusted"
        ) as ensure_trusted, mock.patch.object(
            rc, "ensure_update_check_disabled"
        ), mock.patch.object(rc, "ensure_model_migration_prompts_hidden"):
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_trusted.assert_called_once_with(binding.account_home / ".codex", Path("/tmp"))

    def test_codex_harness_calls_ensure_update_check_disabled(self) -> None:
        # T-2597 wedges #2/#3. Same config-seed path as ensure_project_trusted.
        binding = _binding(Path("/tmp"), harness="codex")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ) as ensure_update_check, mock.patch.object(rc, "ensure_model_migration_prompts_hidden"):
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_update_check.assert_called_once_with(binding.account_home / ".codex")

    def test_codex_harness_calls_ensure_model_migration_prompts_hidden(self) -> None:
        # T-2597 wedge #4. Same config-seed path as ensure_project_trusted.
        binding = _binding(Path("/tmp"), harness="codex")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ), mock.patch.object(rc, "ensure_model_migration_prompts_hidden") as ensure_migration:
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_migration.assert_called_once_with(binding.account_home / ".codex")

    def test_claude_harness_never_calls_ensure_update_check_disabled_or_migration(self) -> None:
        binding = _binding(Path("/tmp"), harness="claude")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ) as ensure_update_check, mock.patch.object(
            rc, "ensure_model_migration_prompts_hidden"
        ) as ensure_migration:
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_update_check.assert_not_called()
        ensure_migration.assert_not_called()

    def test_claude_harness_never_calls_ensure_project_trusted(self) -> None:
        binding = _binding(Path("/tmp"), harness="claude")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            rc, "ensure_project_trusted"
        ) as ensure_trusted:
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_trusted.assert_not_called()

    def test_default_tmux_server_probe_deduplicates_session_rows(self) -> None:
        result = subprocess.CompletedProcess(
            ("tmux",), 0, "4321\n4321\n", ""
        )
        with mock.patch.object(rc, "_run_tmux", return_value=result) as run:
            self.assertEqual(rc._default_tmux_server_pid(), 4321)
        run.assert_called_once_with("-N", "list-sessions", "-F", "#{pid}")

    def test_default_tmux_server_probe_treats_only_known_absence_as_none(self) -> None:
        absent = subprocess.CompletedProcess(
            ("tmux",),
            1,
            "",
            "error connecting to /tmp/tmux-1000/default (No such file or directory)\n",
        )
        with mock.patch.object(rc, "_run_tmux", return_value=absent):
            self.assertIsNone(rc._default_tmux_server_pid())

        unknown = subprocess.CompletedProcess(("tmux",), 1, "", "permission denied\n")
        with mock.patch.object(rc, "_run_tmux", return_value=unknown):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                rc._default_tmux_server_pid()

    def test_default_tmux_server_probe_refuses_multiple_server_pids(self) -> None:
        result = subprocess.CompletedProcess(("tmux",), 0, "4321\n9876\n", "")
        with mock.patch.object(rc, "_run_tmux", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "multiple server PIDs"):
                rc._default_tmux_server_pid()

    def test_external_default_server_refuses_before_any_tmux_mutation(self) -> None:
        binding = _binding(Path("/tmp"))
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=False
        ), mock.patch.object(
            rc, "_target_known"
        ) as target_known, mock.patch.object(
            rc, "_run_tmux"
        ) as tmux, mock.patch.object(
            rc, "_start_new_server_in_scope"
        ) as scoped_start, mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(rc, "ensure_update_check_disabled"), mock.patch.object(
            rc, "ensure_model_migration_prompts_hidden"
        ):
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                rc._start_runtime(binding, _Adapter(binding), replace=False)
        target_known.assert_not_called()
        tmux.assert_not_called()
        scoped_start.assert_not_called()

    def test_existing_scoped_server_creates_session_without_second_scope(self) -> None:
        binding = _binding(Path("/tmp"))
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(
            rc, "_target_known", return_value=False
        ), mock.patch.object(
            rc, "_session_exists", return_value=False
        ), mock.patch.object(
            rc, "_run_tmux", return_value=completed
        ) as tmux, mock.patch.object(
            rc, "_start_new_server_in_scope"
        ) as scoped_start, mock.patch.object(
            rc, "ensure_project_trusted"
        ), mock.patch.object(rc, "ensure_update_check_disabled"), mock.patch.object(
            rc, "ensure_model_migration_prompts_hidden"
        ):
            started = rc._start_runtime(binding, _Adapter(binding), replace=False)
        self.assertFalse(started.created_scope)
        self.assertEqual(tmux.call_args.args[0], "new-session")
        scoped_start.assert_not_called()

    def test_new_server_uses_exact_scope_and_direct_runtime_argv(self) -> None:
        binding = _binding(Path("/tmp"))
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = CommandSpec(
            argv=("/usr/bin/codex", "--model", "value with space"),
            stdin=None,
            cwd=Path("/tmp/cwd with space"),
            env={"PANTHEON_TEST": "env value with space"},
        )
        completed = subprocess.CompletedProcess(("systemd-run",), 0, "", "")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=None
        ), mock.patch.object(rc, "_prepare_runtime_scope") as prepare, mock.patch.object(
            rc.subprocess, "run", return_value=completed
        ) as run, mock.patch.object(rc, "ensure_project_trusted"), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ), mock.patch.object(rc, "ensure_model_migration_prompts_hidden"):
            started = rc._start_runtime(binding, adapter, replace=False)
        self.assertTrue(started.created_scope)
        prepare.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[:6],
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={rc.TMUX_RUNTIME_SCOPE_UNIT}",
            ],
        )
        self.assertEqual(
            argv[-6:],
            [
                "/usr/bin/env",
                "--",
                "PANTHEON_TEST=env value with space",
                "/usr/bin/codex",
                "--model",
                "value with space",
            ],
        )
        self.assertIn(
            "--setenv=PATH=/tmp/.bun/bin:/tmp/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            argv,
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_runtime_command_argv_matches_real_env_surface(self) -> None:
        argv = rc._runtime_command_argv(
            ("/usr/bin/python3", "-c", "import os; print(os.environ['PANTHEON_TEST'])"),
            {"PANTHEON_TEST": "value with space"},
        )
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "value with space")

    def test_active_scope_without_expected_server_refuses_without_reset(self) -> None:
        with mock.patch.object(
            rc, "_scope_state", return_value=("loaded", "active")
        ), mock.patch.object(rc, "_run_systemctl_user") as systemctl:
            with self.assertRaisesRegex(RuntimeError, "active without"):
                rc._prepare_runtime_scope(rc.TMUX_RUNTIME_SCOPE_UNIT)
        systemctl.assert_not_called()

    def test_failed_scope_residue_is_reset_before_retry(self) -> None:
        completed = subprocess.CompletedProcess(("systemctl",), 0, "", "")
        with mock.patch.object(
            rc, "_scope_state", return_value=("loaded", "failed")
        ), mock.patch.object(
            rc, "_run_systemctl_user", return_value=completed
        ) as systemctl:
            rc._prepare_runtime_scope(rc.TMUX_RUNTIME_SCOPE_UNIT)
        systemctl.assert_called_once_with(
            "reset-failed", rc.TMUX_RUNTIME_SCOPE_UNIT
        )

    def test_created_scope_failure_is_cleaned_and_evidenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            with mock.patch.object(
                rc, "_wait_for_live_runtime", return_value=False
            ), mock.patch.object(rc, "_stop_created_scope") as stop:
                with self.assertRaisesRegex(RuntimeError, "scope verification"):
                    rc._verify_started_runtime(binding, started, state_root=root)
            stop.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)
            records = _read_jsonl(
                root
                / "recovery"
                / "runtime-controller"
                / "controller-evidence.jsonl"
            )
            self.assertEqual(records[-1]["event"], "runtime_start_failed")
            self.assertTrue(records[-1]["created_scope"])

    def test_live_legacy_runtime_without_scope_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                {
                    "persona": "recovery",
                    "role": "recovery",
                    "harness": "codex",
                    "profile": "interactive",
                    "executable": "/usr/bin/codex",
                    "model": None,
                    "tmux_target": "pantheon-recovery:harness.0",
                },
            )
            with mock.patch.object(rc, "_pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

    def test_live_matching_runtime_with_unowned_server_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(
                    binding,
                    session_id=None,
                    started_at=rc._utc_now(),
                ),
            )
            with mock.patch.object(
                rc, "_pane_exists", return_value=True
            ), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "not bound"):
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

    def test_interactive_wake_leaves_message_unclaimed_until_tui_consumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(
                binding.mailbox,
                {"message": "do the work"},
                "tester",
                subject="work",
            )
            handle = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            receipt = DeliveryReceipt(
                harness="codex",
                correlation_id="",
                state="accepted",
                mechanism="tmux-submit-verify",
                retryable=False,
                evidence={"acked": "false"},
            )
            with mock.patch.object(rc, "ensure_runtime", return_value=handle), mock.patch.object(
                rc, "submit_to_runtime", return_value=receipt
            ) as submit, mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=True
            ):
                result = wake_persistent_runtime(
                    binding,
                    adapter=_Adapter(binding),
                    state_dir=root / "state",
                    cwd=root,
                    now=100.0,
                )
            self.assertEqual(result["action"], "wake")
            self.assertFalse(result["mailbox_claimed"])
            self.assertFalse(result["acked"])
            self.assertTrue((binding.mailbox / "new" / message_id).is_file())
            self.assertFalse((binding.mailbox / "cur" / message_id).exists())
            self.assertTrue((binding.mailbox / ".wake_marker").is_file())
            submit.assert_called_once()

    def test_interactive_wake_defers_on_confirmed_busy_pane(self) -> None:
        # T-2681/T-2730: a mid-turn (generating) pane cannot accept a submit — the
        # inject would fail 'submit unconfirmed', submit_to_runtime would raise, and the
        # consume unit would record a FALSE failure (the seat is healthy, just busy, and
        # drains its own mailbox when the turn ends). wake_persistent_runtime must DEFER
        # like mailbox_wake.run() rather than attempt a doomed inject: NO submit, marker
        # NOT advanced, message left in new/ so the next fire retries once idle.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(
                binding.mailbox,
                {"message": "do the work"},
                "tester",
                subject="work",
            )
            handle = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            with mock.patch.object(rc, "ensure_runtime", return_value=handle), mock.patch.object(
                rc, "submit_to_runtime"
            ) as submit, mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=False
            ):
                result = wake_persistent_runtime(
                    binding,
                    adapter=_Adapter(binding),
                    state_dir=root / "state",
                    cwd=root,
                    now=100.0,
                )
            self.assertEqual(result["action"], "deferred")
            submit.assert_not_called()
            # message stays queued (unclaimed) and NO wake marker is written, so the
            # next consume fire re-evaluates to "wake" once the pane goes idle.
            self.assertTrue((binding.mailbox / "new" / message_id).is_file())
            self.assertFalse((binding.mailbox / ".wake_marker").is_file())

    def test_authoritative_cap_never_reinvokes_capped_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", profile="primary")
            fallback = _binding(root, harness="codex", profile="fallback")
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(
                binding.mailbox,
                {"message": "do work", "correlation_id": "c1"},
                "tester",
                subject="work",
            )
            snapshot = runtime_quota.QuotaSnapshot(
                profile="claude/primary",
                windows=(),
                observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                freshness="unavailable",
                evidence={"authoritative_limit": "true", "provider": "claude"},
            )
            current = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            with mock.patch.object(
                rc, "recorded_runtime_handle", return_value=current
            ), mock.patch.object(
                rc, "forced_swap", side_effect=rc.RuntimeTransitionPending("read")
            ) as forced, mock.patch.object(rc, "ensure_runtime") as ensure:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(binding, snapshot),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )

            ensure.assert_not_called()
            self.assertEqual(forced.call_args.kwargs["message_id"], message_id)
            self.assertEqual(forced.call_args.kwargs["correlation_id"], "c1")

    def test_five_percent_starts_cooperative_handoff_after_runtime_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", profile="primary")
            fallback = _binding(root, harness="codex", profile="fallback")
            mailbox.provision(binding.mailbox)
            reset = datetime(2026, 8, 5, tzinfo=timezone.utc)
            snapshot = runtime_quota.QuotaSnapshot(
                profile="claude/primary",
                windows=(
                    runtime_quota.QuotaWindow(
                        name="weekly",
                        used_percent=95.0,
                        remaining_percent=5.0,
                        resets_at=reset,
                    ),
                ),
                observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                freshness="fresh",
                evidence={"authoritative_limit": "false", "provider": "claude"},
            )
            current = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            with mock.patch.object(rc, "ensure_runtime", return_value=current) as ensure, mock.patch.object(
                rc, "planned_swap", side_effect=rc.RuntimeTransitionPending("handoff")
            ) as planned, mock.patch.object(rc, "submit_to_runtime") as submit:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(binding, snapshot),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )

            ensure.assert_called_once()
            submit.assert_not_called()
            self.assertEqual(planned.call_args.kwargs["primary_resets_at"], "2026-08-05T00:00:00Z")

    def test_completed_swap_selects_manifest_allowed_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "completed",
                    "runtime_swapped": True,
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                },
            )
            with mock.patch.object(
                persona_bringup, "load_runtime_reference", return_value=target
            ) as load:
                selected = persona_bringup.select_recorded_runtime_binding(
                    root / "instance.yaml",
                    binding,
                    state_dir=root,
                )

            self.assertIs(selected, target)
            load.assert_called_once_with(
                root / "instance.yaml", binding, "codex/fallback"
            )

    def test_quota_reset_preparing_selects_outgoing_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "preparing",
                    "trigger": "quota-reset",
                    "runtime_swapped": False,
                    "outgoing_harness": "codex",
                    "outgoing_profile": "fallback",
                    "incoming_harness": "claude",
                    "incoming_profile": "primary",
                },
            )
            with mock.patch.object(
                persona_bringup, "load_runtime_reference", return_value=fallback
            ) as load:
                selected = persona_bringup.select_recorded_runtime_binding(
                    root / "instance.yaml",
                    primary,
                    state_dir=root,
                )

            self.assertIs(selected, fallback)
            load.assert_called_once_with(
                root / "instance.yaml", primary, "codex/fallback"
            )

    def test_primary_recovery_requires_reset_and_fresh_post_reset_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            reset = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
            state = {
                "phase": "completed",
                "trigger": "planned-quota",
                "fallback_active": True,
                "primary_resets_at": "2026-08-03T12:00:00Z",
            }
            rc._atomic_json(rc._transition_path("recovery", root), state)

            probe_run_id = "20260803T120000Z"
            probe_session = claude_probe_session_id(
                primary.account, "recovery", probe_run_id
            )
            quota_path = root / "recovery" / "quota" / "claude-statusline.json"
            quota_path.parent.mkdir(parents=True, exist_ok=True)
            quota_path.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-03T12:01:00Z",
                        "session_id": probe_session,
                        "rate_limits": {
                            "seven_day": {"used_percentage": 1.0}
                        },
                    }
                ),
                encoding="utf-8",
            )
            quota_hash = hashlib.sha256(quota_path.read_bytes()).hexdigest()
            probe_result = ClaudeQuotaProbeResult(
                schema=CLAUDE_QUOTA_PROBE_SCHEMA,
                status="observed",
                run_id=probe_run_id,
                account=primary.account,
                persona="recovery",
                session_id=probe_session,
                tmux_target=claude_probe_tmux_target("recovery", probe_run_id),
                submitted_at="2026-08-03T12:00:30Z",
                completed_at="2026-08-03T12:01:01Z",
                prompt_sha256=hashlib.sha256(
                    CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")
                ).hexdigest(),
                statusline_path=str(quota_path.resolve()),
                statusline_sha256=quota_hash,
                cap_sentinel=None,
                pong_observed=True,
            )
            result_path = claude_probe_result_path(root, "recovery", probe_run_id)

            def _run_probe(**kwargs):
                self.assertEqual(kwargs["account"], primary.account)
                self.assertEqual(kwargs["persona"], "recovery")
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(dataclasses.asdict(probe_result)), encoding="utf-8"
                )
                return probe_result

            probe_runner = mock.Mock(side_effect=_run_probe)

            healthy = runtime_quota.QuotaSnapshot(
                profile="claude/primary",
                windows=(
                    runtime_quota.QuotaWindow(
                        name="weekly",
                        used_percent=1.0,
                        remaining_percent=99.0,
                        resets_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                    ),
                ),
                observed_at=reset + timedelta(minutes=1),
                freshness="fresh",
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": str(quota_path.resolve()),
                    "sha256": quota_hash,
                    "session_id": probe_session,
                },
            )
            with mock.patch.object(
                persona_bringup,
                "build_adapter",
                return_value=_Adapter(primary, healthy),
            ):
                self.assertIsNone(
                    persona_bringup.primary_recovery_evidence(
                        primary,
                        state_dir=root,
                        now=reset - timedelta(seconds=1),
                        claude_probe_runner=probe_runner,
                    )
                )
                evidence = persona_bringup.primary_recovery_evidence(
                    primary,
                    state_dir=root,
                    now=reset + timedelta(minutes=2),
                    claude_probe_runner=probe_runner,
                )

            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["primary_profile"], "claude/primary")
            self.assertEqual(evidence["probe_freshness"], "fresh")
            self.assertEqual(evidence["probe_weekly_remaining_percent"], "99.0")
            probe_runner.assert_called_once()

            stale = dataclasses.replace(healthy, freshness="stale")
            with mock.patch.object(
                persona_bringup,
                "build_adapter",
                return_value=_Adapter(primary, stale),
            ):
                self.assertIsNone(
                    persona_bringup.primary_recovery_evidence(
                        primary,
                        state_dir=root,
                        now=reset + timedelta(minutes=2),
                        claude_probe_runner=probe_runner,
                    )
                )
            probe_runner.assert_called_once()

    def test_primary_recovery_gate_now_accepts_forced_cap_trigger(self) -> None:
        # T-2584: a forced-cap fallback must be able to auto-return once its
        # schedule-computed primary_resets_at has passed and health is
        # fresh -- mirrors the planned-quota test above with trigger flipped.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            reset = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "completed",
                    "trigger": "forced-cap",
                    "fallback_active": True,
                    "primary_resets_at": "2026-08-03T12:00:00Z",
                },
            )

            probe_run_id = "20260803T120000Z"
            probe_session = claude_probe_session_id(
                primary.account, "recovery", probe_run_id
            )
            quota_path = root / "recovery" / "quota" / "claude-statusline.json"
            quota_path.parent.mkdir(parents=True, exist_ok=True)
            quota_path.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-03T12:01:00Z",
                        "session_id": probe_session,
                        "rate_limits": {
                            "seven_day": {"used_percentage": 1.0}
                        },
                    }
                ),
                encoding="utf-8",
            )
            quota_hash = hashlib.sha256(quota_path.read_bytes()).hexdigest()
            probe_result = ClaudeQuotaProbeResult(
                schema=CLAUDE_QUOTA_PROBE_SCHEMA,
                status="observed",
                run_id=probe_run_id,
                account=primary.account,
                persona="recovery",
                session_id=probe_session,
                tmux_target=claude_probe_tmux_target("recovery", probe_run_id),
                submitted_at="2026-08-03T12:00:30Z",
                completed_at="2026-08-03T12:01:01Z",
                prompt_sha256=hashlib.sha256(
                    CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")
                ).hexdigest(),
                statusline_path=str(quota_path.resolve()),
                statusline_sha256=quota_hash,
                cap_sentinel=None,
                pong_observed=True,
            )
            result_path = claude_probe_result_path(root, "recovery", probe_run_id)

            def _run_probe(**kwargs):
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(dataclasses.asdict(probe_result)), encoding="utf-8"
                )
                return probe_result

            probe_runner = mock.Mock(side_effect=_run_probe)

            healthy = runtime_quota.QuotaSnapshot(
                profile="claude/primary",
                windows=(
                    runtime_quota.QuotaWindow(
                        name="weekly",
                        used_percent=1.0,
                        remaining_percent=99.0,
                        resets_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                    ),
                ),
                observed_at=reset + timedelta(minutes=1),
                freshness="fresh",
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": str(quota_path.resolve()),
                    "sha256": quota_hash,
                    "session_id": probe_session,
                },
            )
            with mock.patch.object(
                persona_bringup,
                "build_adapter",
                return_value=_Adapter(primary, healthy),
            ):
                self.assertIsNone(
                    persona_bringup.primary_recovery_evidence(
                        primary,
                        state_dir=root,
                        now=reset - timedelta(seconds=1),
                        claude_probe_runner=probe_runner,
                    )
                )
                evidence = persona_bringup.primary_recovery_evidence(
                    primary,
                    state_dir=root,
                    now=reset + timedelta(minutes=2),
                    claude_probe_runner=probe_runner,
                )

            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["primary_profile"], "claude/primary")

    def test_primary_recovery_gate_rejects_a_trigger_outside_the_allowlist(self) -> None:
        # Regression: the gate is an explicit allowlist of the two return-
        # eligible triggers, not "any trigger with fallback_active+reset".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "completed",
                    "trigger": "operator-manual",
                    "fallback_active": True,
                    "primary_resets_at": "2026-08-03T12:00:00Z",
                },
            )
            self.assertIsNone(
                persona_bringup.primary_recovery_evidence(
                    primary,
                    state_dir=root,
                    now=datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
                )
            )

    def test_primary_recovery_persists_typed_probe_failure_and_stays_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            reset = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "completed",
                    "trigger": "planned-quota",
                    "fallback_active": True,
                    "primary_resets_at": "2026-08-03T12:00:00Z",
                },
            )

            timeout = subprocess.TimeoutExpired(("tmux", "capture-pane"), 20)

            def failed_runner(**kwargs):
                raise ClaudeQuotaProbeError("probe command timed out: tmux") from timeout

            evidence = persona_bringup.primary_recovery_evidence(
                primary,
                state_dir=root,
                now=reset + timedelta(minutes=2),
                claude_probe_runner=failed_runner,
            )
            self.assertIsNone(evidence)
            failure = (
                root
                / "recovery"
                / "acceptance"
                / "20260803T120000Z"
                / "primary-quota-probe"
                / "failure.json"
            )
            payload = json.loads(failure.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "pantheon-claude-quota-probe-failure/v1")
            self.assertEqual(payload["account"], primary.account)
            self.assertIn("command timed out", payload["error"])

    def test_recovery_evidence_starts_controller_mediated_return_to_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = _binding(root, harness="codex", profile="fallback")
            primary = _binding(root, harness="claude", profile="primary")
            mailbox.provision(fallback.mailbox)
            current = rc.RuntimeHandle(
                fallback.launch,
                fallback.controller.tmux,
                "session-fallback",
                rc._utc_now(),
            )
            recovery = {
                "primary_profile": "claude/primary",
                "reset_passed_at": "2026-08-03T12:00:00Z",
                "probe_observed_at": "2026-08-03T12:01:00Z",
            }
            with mock.patch.object(
                rc, "ensure_runtime", return_value=current
            ), mock.patch.object(
                rc,
                "planned_swap",
                side_effect=rc.RuntimeTransitionPending("return handoff"),
            ) as planned:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        fallback,
                        adapter=_Adapter(fallback),
                        primary_binding=primary,
                        recovery_evidence=recovery,
                        state_dir=root,
                        cwd=root,
                    )

            self.assertIs(planned.call_args.args[1], primary)
            self.assertEqual(planned.call_args.kwargs["trigger"], "quota-reset")
            self.assertEqual(planned.call_args.kwargs["quota_evidence"], recovery)

    def test_default_interactive_consume_never_invokes_headless_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            mailbox.provision(binding.mailbox)
            report = DiagnosticReport(
                harness="codex",
                execution_context="test",
                observations=(),
            )
            with mock.patch.object(persona_bringup, "load_binding", return_value=binding), mock.patch.object(
                persona_bringup, "diagnose_persona", return_value=report
            ), mock.patch.object(
                persona_bringup, "build_adapter", return_value=_Adapter(binding)
            ), mock.patch.object(
                persona_bringup, "wake_persistent_runtime", return_value={"action": "empty"}
            ) as wake, mock.patch.object(
                persona_bringup.HarnessRunner, "run"
            ) as headless, mock.patch.object(persona_bringup, "_print_json"):
                code = persona_bringup.main(
                    [
                        "consume",
                        "--persona",
                        "recovery",
                        "--state-dir",
                        str(root / "state"),
                        "--mailbox-hub",
                        str(root / "mail"),
                    ]
                )
            self.assertEqual(code, 0)
            wake.assert_called_once()
            headless.assert_not_called()


class PostStartHookTests(unittest.TestCase):
    """T-2606 / PRE-4: the generic post-runtime-start hook.

    Unit tests of _run_post_start_commands (substitution, foreground vs
    background invocation, fail-open) plus ensure_runtime integration tests
    proving the hook fires on the exact branch that starts a runtime (first
    start AND restart) and not on plain pane-reuse.
    """

    def test_default_empty_post_start_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            with mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen:
                rc._run_post_start_commands(binding, state_root=root)
            run.assert_not_called()
            popen.assert_not_called()
            evidence_path = rc._evidence_log_path("recovery", root)
            self.assertFalse(evidence_path.exists())

    def test_foreground_command_runs_with_substituted_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                post_start=(
                    PostStartCommand(
                        argv=(
                            "{account_home}/pantheon-config/tools/arm_pane_log.sh",
                            "{tmux_target}",
                            "{persona}",
                        ),
                    ),
                ),
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen:
                rc._run_post_start_commands(binding, state_root=root)

            popen.assert_not_called()
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(
                argv,
                [
                    f"{root}/pantheon-config/tools/arm_pane_log.sh",
                    "pantheon-recovery:harness.0",
                    "recovery",
                ],
            )
            self.assertEqual(run.call_args.kwargs["timeout"], rc.POST_START_TIMEOUT_SECONDS)
            self.assertEqual(run.call_args.kwargs["cwd"], root)
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event"], "post_start_command")
            self.assertEqual(records[0]["background"], "false")
            self.assertEqual(records[0]["outcome"], "ok")

    def test_background_command_launched_via_popen_never_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                post_start=(
                    PostStartCommand(
                        argv=(
                            "{account_home}/pantheon-config/tools/recovery_boot_kick.sh",
                            "{tmux_target}",
                            "{persona}",
                        ),
                        background=True,
                    ),
                ),
            )
            with mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen:
                rc._run_post_start_commands(binding, state_root=root)

            run.assert_not_called()
            popen.assert_called_once()
            argv = popen.call_args.args[0]
            self.assertEqual(
                argv,
                [
                    f"{root}/pantheon-config/tools/recovery_boot_kick.sh",
                    "pantheon-recovery:harness.0",
                    "recovery",
                ],
            )
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertEqual(records[0]["outcome"], "launched_background")

    def test_foreground_command_nonzero_exit_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("some-script",)),)
            )
            completed = mock.Mock(returncode=1, stdout="", stderr="boom")
            with mock.patch.object(rc.subprocess, "run", return_value=completed):
                rc._run_post_start_commands(binding, state_root=root)  # must not raise
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertEqual(records[0]["outcome"], "exit_1")

    def test_foreground_command_exception_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("missing-binary",)),)
            )
            with mock.patch.object(
                rc.subprocess, "run", side_effect=FileNotFoundError("no such file")
            ):
                rc._run_post_start_commands(binding, state_root=root)  # must not raise
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertIn("FileNotFoundError", records[0]["outcome"])

    def test_foreground_command_timeout_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("slow-script",)),)
            )
            with mock.patch.object(
                rc.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="slow-script", timeout=5.0),
            ):
                rc._run_post_start_commands(binding, state_root=root)  # must not raise
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertIn("TimeoutExpired", records[0]["outcome"])

    def test_background_command_launch_exception_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                post_start=(PostStartCommand(argv=("missing-binary",), background=True),),
            )
            with mock.patch.object(
                rc.subprocess, "Popen", side_effect=FileNotFoundError("no such file")
            ):
                rc._run_post_start_commands(binding, state_root=root)  # must not raise
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertIn("FileNotFoundError", records[0]["outcome"])

    def test_unresolvable_template_is_skipped_not_raised(self) -> None:
        # Defense in depth: resolve_controller_config already rejects unknown
        # tokens at manifest-load time, but a directly-constructed
        # PostStartCommand (as any caller could hand ensure_runtime) must not
        # crash the runtime start either.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("{bogus}",)),)
            )
            with mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen:
                rc._run_post_start_commands(binding, state_root=root)  # must not raise
            run.assert_not_called()
            popen.assert_not_called()
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertEqual(records[0]["event"], "post_start_command_skipped")

    def test_ensure_runtime_fires_hooks_after_readiness_on_first_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("hook",)),)
            )
            manager = mock.Mock()
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc,
                "_start_runtime",
                return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ) as verify_started, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ) as verify_ready, mock.patch.object(
                rc, "_run_post_start_commands"
            ) as hooks, _provenance_patch():
                manager.attach_mock(verify_started, "verify_started")
                manager.attach_mock(verify_ready, "verify_ready")
                manager.attach_mock(hooks, "hooks")
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            hooks.assert_called_once_with(binding, state_root=root)
            self.assertEqual(
                [call[0] for call in manager.mock_calls],
                ["verify_started", "verify_ready", "hooks"],
            )

    def test_ensure_runtime_reruns_hooks_on_restart_not_only_first_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("hook",)),)
            )
            with mock.patch.object(
                rc, "_pane_exists", side_effect=(False, False)
            ), mock.patch.object(
                rc,
                "_start_runtime",
                return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(
                rc, "_run_post_start_commands"
            ) as hooks, _provenance_patch():
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            self.assertEqual(hooks.call_count, 2)

    def test_ensure_runtime_does_not_rerun_hooks_on_plain_pane_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root, post_start=(PostStartCommand(argv=("hook",)),)
            )
            with mock.patch.object(
                rc, "_pane_exists", side_effect=(False, True)
            ), mock.patch.object(
                rc,
                "_start_runtime",
                return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(
                rc, "_run_post_start_commands"
            ) as hooks, mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(
                rc, "_pid_in_scope", return_value=True
            ), _provenance_patch():
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            self.assertEqual(hooks.call_count, 1)

    def test_ensure_runtime_succeeds_even_if_post_start_command_errors(self) -> None:
        # Integration proof of fail-open: the REAL _run_post_start_commands
        # (not mocked) with a guaranteed-failing foreground command must not
        # prevent ensure_runtime from returning a handle or writing state.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                post_start=(
                    PostStartCommand(argv=("/nonexistent/binary-t2606",)),
                ),
            )
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc,
                "_start_runtime",
                return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                handle = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)

            self.assertIsNotNone(handle)
            state = json.loads(
                (root / "recovery" / "runtime-controller" / "runtime.json").read_text()
            )
            self.assertEqual(state["profile"], "interactive")
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            post_start_records = [r for r in records if r["event"] == "post_start_command"]
            self.assertEqual(len(post_start_records), 1)
            self.assertIn("FileNotFoundError", post_start_records[0]["outcome"])


class RuntimeInputReadinessWaitTests(unittest.TestCase):
    """Direct unit tests of _wait_for_runtime_input_ready (T-2382): a fake
    _capture_visible_runtime_pane sequence, no real tmux, no real sleeping."""

    def test_ready_after_n_polls_returns_hashed_evidence(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        panes = ["not yet framed\n", "still not ready\n", "╭───╮\n❯ \n" + "─" * 25]
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=panes
        ) as capture, mock.patch.object(rc.time, "sleep") as sleep:
            readiness = rc._wait_for_runtime_input_ready(
                target, timeout=5.0, poll_interval=0.01
            )
        self.assertTrue(readiness.ready)
        self.assertEqual(capture.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            readiness.pane_sha256,
            hashlib.sha256(panes[-1].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(readiness.reason, "runtime input readiness confirmed")

    def test_never_ready_fails_closed_on_deadline(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        # First monotonic() call establishes the deadline; the second (checked
        # right after the first capture) is made to already exceed it, so the
        # loop exits after exactly one capture — deterministic, no real wait.
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value="never ready\n"
        ) as capture, mock.patch.object(
            rc.time, "monotonic", side_effect=[0.0, 100.0]
        ), mock.patch.object(rc.time, "sleep") as sleep:
            readiness = rc._wait_for_runtime_input_ready(target)
        self.assertFalse(readiness.ready)
        capture.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(
            readiness.pane_sha256,
            hashlib.sha256(b"never ready\n").hexdigest(),
        )
        self.assertEqual(readiness.reason, "runtime input readiness timed out")


# --------------------------------------------------------------------------- #
# T-2594: codex-gated update-modal dismissal inside                          #
# _wait_for_runtime_input_ready, sharing tools.codex_modal's single audited  #
# navigation with tools.bringup_acceptance.harness_codex_ready (T-2572).     #
# --------------------------------------------------------------------------- #

_T2594_MODAL_OPTION_1 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "  3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

_T2594_MODAL_OPTION_2 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "  1. Update now (runs `npm install -g @openai/codex`)\n"
    "› 2. Skip\n"
    "  3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

_T2594_MODAL_OPTION_3 = (
    "\n"
    "  ✨ Update available! 0.136.0 -> 0.146.1\n"
    "\n"
    "  1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "› 3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)

_T2594_READY_COMPOSER_AFTER_DISMISS = "╭───╮\n❯ \n" + "─" * 25


class RuntimeInputReadinessCodexModalTests(unittest.TestCase):
    def test_codex_gated_dismisses_modal_then_reaches_ready(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        captures = iter(
            [
                _T2594_MODAL_OPTION_1,  # 1st poll: modal, not ready
                _T2594_MODAL_OPTION_1,  # dismiss's own capture #1
                _T2594_MODAL_OPTION_2,  # capture #2
                _T2594_MODAL_OPTION_3,  # capture #3 -> sends Enter
                _T2594_READY_COMPOSER_AFTER_DISMISS,  # re-poll: ready
            ]
        )
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=lambda *a, **k: next(captures)
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(
                target, harness="codex", timeout=5.0, poll_interval=0.01
            )
        self.assertTrue(readiness.ready)
        sent_keys = [call.args[3] for call in run_tmux.call_args_list]
        self.assertEqual(sent_keys, ["Down", "Down", "Enter"])
        for call in run_tmux.call_args_list:
            self.assertEqual(call.args[0], "send-keys")

    def test_codex_gated_dismissal_exhausted_reports_ready_false(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value=_T2594_MODAL_OPTION_1
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(
                target, harness="codex", timeout=5.0, poll_interval=0.01
            )
        self.assertFalse(readiness.ready)
        self.assertIn("codex update-modal dismissal failed", readiness.reason)
        sent_keys = [call.args[3] for call in run_tmux.call_args_list]
        self.assertNotIn("Enter", sent_keys)
        self.assertLessEqual(len(sent_keys), codex_modal.MAX_MODAL_NAV_ATTEMPTS)

    def test_non_codex_harness_never_reaches_send_keys_even_with_modal_shaped_text(
        self,
    ) -> None:
        """Structural gate proof (T-2594): the highest-risk property.

        With harness left at its default (non-codex), the pane forever
        renders the exact update-modal text -- and _run_tmux (the ONLY path
        by which a keystroke could reach a live pane) must never be called
        even once. codex_gated=False short-circuits the `and` before
        is_codex_update_modal is even evaluated, so this is a structural
        guarantee, not a behavioral coincidence.
        """
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value=_T2594_MODAL_OPTION_1
        ), mock.patch.object(
            rc.time, "monotonic", side_effect=[0.0, 100.0]
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(target)
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.reason, "runtime input readiness timed out")
        run_tmux.assert_not_called()

    def test_explicit_claude_harness_never_reaches_send_keys(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value=_T2594_MODAL_OPTION_1
        ), mock.patch.object(
            rc.time, "monotonic", side_effect=[0.0, 100.0]
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(target, harness="claude")
        self.assertFalse(readiness.ready)
        run_tmux.assert_not_called()


# --------------------------------------------------------------------------- #
# T-2599: codex-gated model-migration-modal dismissal, same treatment as     #
# T-2594's update-modal branch above -- both live inside                     #
# _wait_for_runtime_input_ready's single poll loop.                          #
# --------------------------------------------------------------------------- #

_T2599_MODAL_NOT_YET_VISIBLE = "plain pane, modal not rendered yet\n"

# Live-captured verbatim (T-2599 wet-run, 2026-08-08): `tmux capture-pane -p`
# against a real disposable tmux pane running the real pinned /usr/bin/codex
# 0.146.0 against the real account home, forced `--model gpt-5.4` to hit the
# live active gpt-5.4 -> gpt-5.6-terra preset. NOT a composition.
_T2599_MODAL_LIVE_MARKDOWN = (
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


class RuntimeInputReadinessMigrationModalTests(unittest.TestCase):
    def test_codex_gated_dismisses_migration_modal_then_reaches_ready(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        captures = iter(
            [
                _T2599_MODAL_LIVE_MARKDOWN,  # 1st poll: modal, not ready
                _T2599_MODAL_LIVE_MARKDOWN,  # dismiss's own capture -> sends Enter
                _T2594_READY_COMPOSER_AFTER_DISMISS,  # re-poll: ready
            ]
        )
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=lambda *a, **k: next(captures)
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(
                target, harness="codex", timeout=5.0, poll_interval=0.01
            )
        self.assertTrue(readiness.ready)
        sent_keys = [call.args[3] for call in run_tmux.call_args_list]
        self.assertEqual(sent_keys, ["Enter"])

    def test_codex_gated_dismissal_exhausted_reports_ready_false(self) -> None:
        """Mirrors the update-modal exhaustion test's shape, adapted to the
        migration modal's presence-check (not cursor-nav) loop: the OUTER
        poll's own capture sees the modal (entering the branch), but every
        capture INSIDE dismiss_codex_model_migration_modal's own bounded
        loop sees it flicker away -- so it never re-confirms and exhausts."""
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        captures = iter(
            [_T2599_MODAL_LIVE_MARKDOWN] + [_T2599_MODAL_NOT_YET_VISIBLE] * 10
        )
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=lambda *a, **k: next(captures)
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(
                target, harness="codex", timeout=5.0, poll_interval=0.01
            )
        self.assertFalse(readiness.ready)
        self.assertIn("codex model-migration-modal dismissal failed", readiness.reason)
        run_tmux.assert_not_called()

    def test_both_modals_dismissed_in_sequence_within_one_poll_loop(self) -> None:
        """Codex's real startup order: update-check runs before app-server
        start, the migration prompt after bootstrap -- so a single bringup
        can hit both, in that order. Proves neither branch's "at most once"
        bool interferes with the other."""
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        captures = iter(
            [
                _T2594_MODAL_OPTION_3,  # 1st poll: update modal, cursor already on skip
                _T2594_MODAL_OPTION_3,  # dismiss's own capture -> sends Enter
                _T2599_MODAL_LIVE_MARKDOWN,  # re-poll: migration modal now showing
                _T2599_MODAL_LIVE_MARKDOWN,  # dismiss's own capture -> sends Enter
                _T2594_READY_COMPOSER_AFTER_DISMISS,  # re-poll: ready
            ]
        )
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=lambda *a, **k: next(captures)
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(
                target, harness="codex", timeout=5.0, poll_interval=0.01
            )
        self.assertTrue(readiness.ready)
        sent_keys = [call.args[3] for call in run_tmux.call_args_list]
        self.assertEqual(sent_keys, ["Enter", "Enter"])

    def test_non_codex_harness_never_reaches_send_keys_even_with_migration_modal_shaped_text(
        self,
    ) -> None:
        """Structural gate proof (T-2599, mirrors T-2594's own): with harness
        left at its default (non-codex), _run_tmux (the ONLY path by which a
        keystroke could reach a live pane) must never be called even once,
        even though the pane forever renders migration-modal-shaped text."""
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        with mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value=_T2599_MODAL_LIVE_MARKDOWN
        ), mock.patch.object(
            rc.time, "monotonic", side_effect=[0.0, 100.0]
        ), mock.patch.object(rc, "_run_tmux") as run_tmux, mock.patch.object(rc.time, "sleep"):
            readiness = rc._wait_for_runtime_input_ready(target)
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.reason, "runtime input readiness timed out")
        run_tmux.assert_not_called()


class VerifyRuntimeInputReadyTests(unittest.TestCase):
    """_verify_runtime_input_ready (T-2382): mirrors _verify_started_runtime's
    fail-closed shape — stop the created scope, emit evidence, raise."""

    def test_threads_binding_harness_into_the_wait_call(self) -> None:
        # T-2594: the codex-gating decision at _wait_for_runtime_input_ready
        # is only ever correct if it reflects the ACTUAL binding, not a
        # default guess -- this is the one wiring point that could silently
        # defeat the gate.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex")
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            readiness = rc.RuntimeInputReadiness(
                ready=True, pane_sha256="a" * 64, reason="runtime input readiness confirmed"
            )
            with mock.patch.object(
                rc, "_wait_for_runtime_input_ready", return_value=readiness
            ) as wait:
                rc._verify_runtime_input_ready(binding, started, state_root=root)
            self.assertEqual(wait.call_args.kwargs.get("harness"), "codex")

    def test_ready_returns_cleanly_no_scope_stop_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            readiness = rc.RuntimeInputReadiness(
                ready=True, pane_sha256="a" * 64, reason="runtime input readiness confirmed"
            )
            with mock.patch.object(
                rc, "_wait_for_runtime_input_ready", return_value=readiness
            ), mock.patch.object(rc, "_stop_created_scope") as stop:
                rc._verify_runtime_input_ready(binding, started, state_root=root)
            stop.assert_not_called()
            evidence_path = root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
            self.assertFalse(evidence_path.exists())

    def test_not_ready_stops_created_scope_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            readiness = rc.RuntimeInputReadiness(
                ready=False, pane_sha256="b" * 64, reason="runtime input readiness timed out"
            )
            with mock.patch.object(
                rc, "_wait_for_runtime_input_ready", return_value=readiness
            ), mock.patch.object(rc, "_stop_created_scope") as stop:
                with self.assertRaisesRegex(RuntimeError, "input readiness"):
                    rc._verify_runtime_input_ready(binding, started, state_root=root)
            stop.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)
            records = _read_jsonl(
                root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
            )
            self.assertEqual(records[-1]["event"], "runtime_input_not_ready")
            self.assertEqual(records[-1]["pane_sha256"], "b" * 64)
            self.assertEqual(records[-1]["reason"], "runtime input readiness timed out")
            self.assertTrue(records[-1]["created_scope"])

    def test_not_ready_without_created_scope_does_not_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            readiness = rc.RuntimeInputReadiness(
                ready=False, pane_sha256="c" * 64, reason="runtime input readiness timed out"
            )
            with mock.patch.object(
                rc, "_wait_for_runtime_input_ready", return_value=readiness
            ), mock.patch.object(rc, "_stop_created_scope") as stop:
                with self.assertRaises(RuntimeError):
                    rc._verify_runtime_input_ready(binding, started, state_root=root)
            stop.assert_not_called()


class RuntimeInputReadinessControllerWiringTests(unittest.TestCase):
    """Full-stack fake-TUI tests (T-2382): ensure_runtime / _replace_runtime
    must not publish runtime.json before input-readiness confirms, must
    publish after, and must fail closed (no publish, scope stopped, stale
    metadata cleared on replace) when the pane never becomes ready. No real
    tmux — _capture_visible_runtime_pane is faked."""

    def test_ensure_runtime_does_not_publish_before_ready_then_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            panes = ["not ready yet\n", "╭───╮\n❯ \n" + "─" * 25]
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_capture_visible_runtime_pane", side_effect=panes
            ) as capture, mock.patch.object(rc.time, "sleep"), _provenance_patch():
                handle = rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
            self.assertEqual(capture.call_count, 2)
            self.assertIsNotNone(handle)
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text())
            self.assertEqual(state["profile"], "interactive")

    def test_ensure_runtime_never_ready_fails_closed_no_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_capture_visible_runtime_pane", return_value="never ready\n"
            ), mock.patch.object(
                rc.time, "monotonic", side_effect=[0.0, 100.0]
            ), mock.patch.object(rc.time, "sleep"), mock.patch.object(
                rc, "_stop_created_scope"
            ) as stop, _provenance_patch():
                with self.assertRaisesRegex(RuntimeError, "input readiness"):
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
            self.assertFalse(state_path.exists())
            stop.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)

    def test_replace_runtime_publishes_after_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _binding(root)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            panes = ["╭───╮\n❯ \n" + "─" * 25]
            with mock.patch.object(rc, "_target_known", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_capture_visible_runtime_pane", side_effect=panes
            ), mock.patch.object(rc.time, "sleep"), _provenance_patch():
                rc._replace_runtime(target, state_root=root)
            self.assertTrue(state_path.exists())

    def test_replace_runtime_never_ready_clears_stale_metadata_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _binding(root)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            # Pre-seed stale metadata describing the runtime that was just
            # respawned out from under this replace attempt.
            rc._atomic_json(state_path, {"persona": "recovery", "stale": True})
            with mock.patch.object(rc, "_target_known", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(
                    rc.TMUX_RUNTIME_SCOPE_UNIT, True
                )
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_capture_visible_runtime_pane", return_value="never ready\n"
            ), mock.patch.object(
                rc.time, "monotonic", side_effect=[0.0, 100.0]
            ), mock.patch.object(rc.time, "sleep"), mock.patch.object(
                rc, "_stop_created_scope"
            ) as stop, _provenance_patch():
                with self.assertRaisesRegex(RuntimeError, "input readiness"):
                    rc._replace_runtime(target, state_root=root)
            stop.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)
            self.assertTrue(state_path.exists())
            self.assertEqual(json.loads(state_path.read_text()), {})


class TransitionBarrierTests(unittest.TestCase):
    def test_successor_prompt_carries_structural_ack_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            handle = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            evidence = root / "continuity note.md"
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(
                    handle,
                    evidence,
                    persona="recovery",
                    state_root=root,
                )

            prompt = submit.call_args.args[1].prompt
            self.assertIn("ack-read --persona recovery", prompt)
            self.assertIn(f"--state-root {root}", prompt)
            self.assertIn("hook:runtime-transfer:continuity note.md", prompt)

    def test_ack_read_cli_completes_waiting_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._begin_transition(
                "recovery",
                trigger="planned-quota",
                outgoing_profile="primary",
                incoming_profile="fallback",
                state_root=root,
                drain_id="d1",
            )
            state = rc.transition_state("recovery", state_root=root)
            state["phase"] = "awaiting-successor-read"
            rc._atomic_json(rc._transition_path("recovery", root), state)

            result = rc.main(
                [
                    "ack-read",
                    "--persona",
                    "recovery",
                    "--state-root",
                    str(root),
                    "--evidence",
                    "hook:runtime-transfer:continuity.md",
                ]
            )

            self.assertEqual(result, 0)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "completed")
            self.assertTrue(state["successor_read_ack"])

    def test_successor_ack_must_match_recorded_transition_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "handoff.md"
            rc._begin_transition(
                "recovery",
                trigger="forced-cap",
                outgoing_profile="primary",
                incoming_profile="fallback",
                outgoing_harness="claude",
                incoming_harness="codex",
                state_root=root,
                drain_id="d1",
            )
            state = rc.transition_state("recovery", state_root=root)
            state["phase"] = "awaiting-successor-read"
            state["handoff_or_graveyard"] = str(evidence)
            state = rc._record_successor_target(
                "recovery", state, evidence, state_root=root
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                rc.acknowledge_successor_read(
                    "recovery",
                    evidence="hook:runtime-transfer:wrong:handoff.md",
                    state_root=root,
                )

            rc.acknowledge_successor_read(
                "recovery",
                evidence=str(state["expected_successor_read_evidence"]),
                state_root=root,
            )
            completed = rc.transition_state("recovery", state_root=root)
            self.assertEqual(completed["phase"], "completed")
            self.assertEqual(
                completed["successor_read_evidence"],
                state["expected_successor_read_evidence"],
            )

    def test_same_claimed_drain_stays_barred_after_successor_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._begin_transition(
                "recovery",
                trigger="planned-quota",
                outgoing_profile="claude",
                incoming_profile="codex",
                drain_id="drain-old",
                state_root=root,
            )
            for drain in ("drain-old", "drain-new"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.assert_drain_allowed("recovery", drain, state_root=root)
            state = rc.transition_state("recovery", state_root=root)
            state["phase"] = "awaiting-successor-read"
            rc._atomic_json(rc._transition_path("recovery", root), state)
            rc.acknowledge_successor_read(
                "recovery", evidence="hook:handoff-read:receipt-1", state_root=root
            )
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.assert_drain_allowed("recovery", "drain-old", state_root=root)
            rc.assert_drain_allowed("recovery", "drain-new", state_root=root)

    def test_planned_failover_stops_claims_until_handoff_read_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._begin_transition(
                "recovery",
                trigger="planned-quota",
                outgoing_profile="primary",
                incoming_profile="fallback",
                drain_id="d1",
                state_root=root,
            )
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.assert_drain_allowed("recovery", "d2", state_root=root)
            state = rc.transition_state("recovery", state_root=root)
            state["phase"] = "awaiting-successor-read"
            rc._atomic_json(rc._transition_path("recovery", root), state)
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.assert_drain_allowed("recovery", "d2", state_root=root)
            rc.acknowledge_successor_read(
                "recovery",
                evidence="hook:runtime-transfer:handoff",
                state_root=root,
            )
            rc.assert_drain_allowed("recovery", "d2", state_root=root)

    def test_planned_failover_archives_verified_model_handoff_before_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            handoff = root / "HANDOFF.md"
            handoff.write_text("# Transfer\nCurrent work is durable.\n", encoding="utf-8")
            archive = root / "archive.md"
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            replacement = rc.RuntimeHandle(
                target.launch,
                target.controller.tmux,
                None,
                rc._utc_now(),
            )
            events = []

            def archive_required(*args, **kwargs):
                events.append("archive")
                return archive

            def replace_runtime(*args, **kwargs):
                events.append("replace")
                return replacement

            with mock.patch.object(
                rc.graveyard, "archive_required", side_effect=archive_required
            ), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace_runtime
            ), mock.patch.object(rc, "_prompt_successor"), mock.patch.object(
                rc, "submit_to_runtime"
            ) as submit:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                    )
                self.assertEqual(events, [])
                submit.assert_called_once()

                handoff.write_text(
                    "# Transfer\nCurrent work is durable and freshly reviewed.\n",
                    encoding="utf-8",
                )
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                    )

            self.assertEqual(events, ["archive", "replace"])
            self.assertEqual(
                rc.transition_state("recovery", state_root=root)["handoff_or_graveyard"],
                str(archive),
            )

    def test_stale_unchanged_handoff_cannot_satisfy_new_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            handoff = root / "HANDOFF.md"
            handoff.write_text("# Old transfer\n", encoding="utf-8")
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            with mock.patch.object(rc, "submit_to_runtime"), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace:
                with self.assertRaisesRegex(
                    rc.RuntimeTransitionPending, "fresh model-authored"
                ):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        handoff_path=handoff,
                        state_root=root,
                    )
                with self.assertRaisesRegex(
                    rc.RuntimeTransitionPending, "has not changed"
                ):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        handoff_path=handoff,
                        state_root=root,
                    )
            replace.assert_not_called()

    def test_planned_swap_without_verified_handoff_never_replaces_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            missing = root / "missing-handoff"
            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "submit_to_runtime"
            ) as request_handoff:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=missing,
                        state_root=root,
                    )
            replace.assert_not_called()
            request_handoff.assert_called_once()

    def test_planned_failover_requeues_remaining_records_from_same_claimed_drain(self) -> None:
        class TransitioningRunner:
            def __init__(self, state_root: Path) -> None:
                self.calls = 0
                self.state_root = state_root

            def run(self, adapter, request):
                self.calls += 1
                rc._begin_transition(
                    "recovery",
                    trigger="planned-quota",
                    outgoing_profile="primary",
                    incoming_profile="fallback",
                    drain_id="fixed-drain",
                    state_root=self.state_root,
                )
                state = rc.transition_state("recovery", state_root=self.state_root)
                state["phase"] = "awaiting-successor-read"
                rc._atomic_json(rc._transition_path("recovery", self.state_root), state)
                rc.acknowledge_successor_read(
                    "recovery",
                    evidence="hook:handoff-read:first-record",
                    state_root=self.state_root,
                )
                return ExecutionResult(
                    harness="codex",
                    status="completed",
                    final_message="first complete",
                    session=SessionHandle("session-1", None, request.cwd, True),
                    usage=None,
                    model=None,
                    returncode=0,
                    error=None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "mail" / "recovery"
            mailbox.provision(inbox)
            first = mailbox.send(inbox, {"message": "one"}, "tester", subject="one")
            second = mailbox.send(inbox, {"message": "two"}, "tester", subject="two")
            binding = SimpleNamespace(
                persona="recovery",
                role="recovery",
                selected_harness="codex",
                configured_harness="codex",
                pinned_rules=root / "recovery.md",
                mailbox=inbox,
                launch=SimpleNamespace(model=None),
            )
            binding.pinned_rules.write_text("recovery rules", encoding="utf-8")
            runner = TransitioningRunner(root / "state")
            with mock.patch.object(rc, "new_drain_id", return_value="fixed-drain"):
                stats, outcomes = consume_persona_mailbox(
                    binding,
                    adapter=object(),
                    runner=runner,
                    state_dir=root / "state",
                    cwd=Path("/tmp"),
                    reply_hub=root / "mail",
                )

            self.assertEqual(runner.calls, 1)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(stats["processed"], [first])
            self.assertEqual(stats["errors"][0]["id"], second)
            self.assertTrue((inbox / "cur" / first).is_file())
            self.assertTrue((inbox / "new" / second).is_file())
            self.assertFalse((inbox / ".dlq" / f"{second}.json").exists())


class ForcedFailoverTests(unittest.TestCase):
    def _current_and_target(self, root: Path):
        current_binding = _binding(root, harness="claude", profile="primary")
        target = _binding(root, harness="codex", profile="fallback")
        current = rc.RuntimeHandle(
            current_binding.launch,
            current_binding.controller.tmux,
            "session-1",
            rc._utc_now(),
        )
        error = rc.ProviderQuotaExhausted(
            "weekly cap", profile="primary", provider_error="authoritative cap"
        )
        return current, target, error

    def _complete_forced_transition(
        self,
        root: Path,
        *,
        message_id: str = "m1",
        correlation_id: str = "c1",
    ):
        evidence = root / f"{message_id}.md"
        evidence.write_text("facts", encoding="utf-8")
        current, target, error = self._current_and_target(root)
        replacement = rc.RuntimeHandle(
            target.launch, target.controller.tmux, None, rc._utc_now()
        )
        with mock.patch.object(
            rc, "_existing_graveyard", return_value=evidence
        ), mock.patch.object(
            rc, "_replace_runtime", return_value=replacement
        ), mock.patch.object(rc, "_prompt_successor"):
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.forced_swap(
                    current,
                    target,
                    error=error,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    drain_id="d1",
                    state_root=root,
                )
        state = rc.transition_state("recovery", state_root=root)
        rc.acknowledge_successor_read(
            "recovery",
            evidence=state["expected_successor_read_evidence"],
            state_root=root,
        )
        return current, target, error, evidence

    def test_durability_failure_keeps_breaker_open_and_fallback_unused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error = self._current_and_target(root)
            with mock.patch.object(rc, "_existing_graveyard", return_value=None), mock.patch.object(
                rc.graveyard,
                "archive_required",
                side_effect=rc.graveyard.GraveyardDurabilityError("fsync failed"),
            ), mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaises(rc.ProviderQuotaExhausted):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                    )
            replace.assert_not_called()
            state = rc.transition_state("recovery", state_root=root)
            self.assertTrue(state["breaker_open"])
            self.assertEqual(state["phase"], "durability-failed")

    def test_records_schedule_based_primary_resets_at_from_injected_now(self) -> None:
        # T-2584: forced_swap has no provider-observed reset window to record
        # (the cap already fired) -- it must compute one from the primary
        # harness's weekly schedule instead, using the injected clock only.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error = self._current_and_target(root)
            fixed_now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(
                rc, "_existing_graveyard", return_value=None
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(rc, "_prompt_successor"), mock.patch.dict(
                rc.os.environ, {"PANTHEON_GRAVEYARD_ROOT": str(root / "graveyard")}
            ):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                        now=fixed_now,
                    )

            state = rc.transition_state("recovery", state_root=root)
            expected = runtime_quota.next_weekly_reset(
                current.profile.harness, now=fixed_now
            )
            self.assertEqual(state["primary_resets_at"], rc._iso(expected))

    def test_retry_does_not_redrift_an_already_recorded_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            first_now = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
            second_now = datetime(2026, 1, 16, 10, 0, tzinfo=timezone.utc)
            with mock.patch.object(
                rc, "_existing_graveyard", return_value=evidence
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                        now=first_now,
                    )
                first_recorded = rc.transition_state("recovery", state_root=root)[
                    "primary_resets_at"
                ]
                # Same in-flight transition (not yet acked) retried with a
                # later "now" -- must NOT redrift the already-recorded value.
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d2",
                        state_root=root,
                        now=second_now,
                    )
            second_recorded = rc.transition_state("recovery", state_root=root)[
                "primary_resets_at"
            ]
            self.assertEqual(first_recorded, second_recorded)
            self.assertEqual(
                first_recorded,
                rc._iso(runtime_quota.next_weekly_reset(current.profile.harness, now=first_now)),
            )

    def test_successor_must_ack_graveyard_read_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "_existing_graveyard", return_value=evidence), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                    )
                replace.assert_called_once()
                state = rc.transition_state("recovery", state_root=root)
                self.assertEqual(state["phase"], "awaiting-successor-read")
                rc.acknowledge_successor_read(
                    "recovery",
                    evidence=state["expected_successor_read_evidence"],
                    state_root=root,
                )
                receipt = rc.forced_swap(
                    current,
                    target,
                    error=error,
                    message_id="m1",
                    correlation_id="c1",
                    drain_id="d2",
                    state_root=root,
                )
            self.assertEqual(receipt.trigger, "forced-cap")
            self.assertEqual(receipt.handoff_or_graveyard, evidence)
            replace.assert_called_once()

    def test_completed_forced_fast_path_requires_exact_transition_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error, evidence = self._complete_forced_transition(root)

            receipt = rc.forced_swap(
                current,
                target,
                error=error,
                message_id="m1",
                correlation_id="c1",
                drain_id="d2",
                state_root=root,
            )
            self.assertEqual(receipt.trigger, "forced-cap")
            self.assertEqual(receipt.handoff_or_graveyard, evidence)

            base = rc.transition_state("recovery", state_root=root)
            for key, value in (
                ("trigger", "planned-quota"),
                ("outgoing_harness", "codex"),
                ("outgoing_profile", "other-primary"),
                ("incoming_harness", "claude"),
                ("incoming_profile", "other-fallback"),
                ("message_id", "other-message"),
                ("correlation_id", "other-correlation"),
            ):
                mutated = dict(base)
                mutated[key] = value
                with self.subTest(key=key):
                    self.assertFalse(
                        rc._completed_forced_transition_matches(
                            mutated,
                            current,
                            target,
                            message_id="m1",
                            correlation_id="c1",
                        )
                    )

    def test_completed_forced_swap_with_wrong_correlation_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error, _ = self._complete_forced_transition(root)
            new_evidence = root / "new.md"
            new_evidence.write_text("new facts", encoding="utf-8")
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(
                rc, "_existing_graveyard", return_value=new_evidence
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="different-correlation",
                        drain_id="d3",
                        state_root=root,
                    )

            replace.assert_called_once()
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertEqual(state["correlation_id"], "different-correlation")

    def test_quota_exception_is_mailbox_transient_and_requeues_without_dlq_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "mail" / "recovery"
            mailbox.provision(inbox)
            message_id = mailbox.send(inbox, {"message": "work"}, "tester")

            def capped(record):
                raise rc.ProviderQuotaExhausted(
                    "cap", profile="primary", provider_error="authoritative"
                )

            stats = mailbox.consume(inbox, capped)

            self.assertEqual(stats["processed"], [])
            self.assertTrue((inbox / "new" / message_id).is_file())
            self.assertFalse((inbox / "cur" / message_id).exists())
            self.assertFalse((inbox / ".attempts" / message_id).exists())

    def test_forced_receipt_contains_only_observed_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("observed handoff", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                {
                    **rc._runtime_payload(
                        SimpleNamespace(
                            persona="recovery",
                            role="recovery",
                            launch=current.profile,
                            controller=SimpleNamespace(tmux=current.tmux),
                        ),
                        session_id="session-1",
                        started_at=rc._utc_now(),
                    ),
                    "last_structural_event": "turn_failed",
                },
            )
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "_existing_graveyard", return_value=evidence), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        state_root=root,
                    )

            receipt_path = root / "recovery" / "runtime-controller" / "forced-failover.json"
            receipt = json.loads(receipt_path.read_text())
            self.assertIsNone(receipt["ticket_or_step_id"])
            self.assertEqual(receipt["repository_evidence"], {})
            self.assertEqual(receipt["last_lifecycle_event"], "turn_failed")
            self.assertTrue(receipt["model_authored_handoff"])
            self.assertNotIn("cost", receipt)
            self.assertNotIn("completion", receipt)

    def test_forced_path_uses_existing_graveyard_entry_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "_existing_graveyard", return_value=evidence), mock.patch.object(
                rc.graveyard, "archive_required"
            ) as archive, mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        state_root=root,
                    )

            archive.assert_not_called()
            self.assertEqual(
                rc.transition_state("recovery", state_root=root)["handoff_or_graveyard"],
                str(evidence),
            )

    def test_forced_path_creates_minimal_emergency_entry_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.dict(
                rc.os.environ,
                {"PANTHEON_GRAVEYARD_ROOT": str(root / "graveyard")},
            ), mock.patch.object(rc, "_existing_graveyard", return_value=None), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        state_root=root,
                    )

            state = rc.transition_state("recovery", state_root=root)
            body = Path(state["handoff_or_graveyard"]).read_text()
            self.assertIn("Model-authored handoff unavailable", body)
            self.assertIn("message id: m1", body)
            self.assertNotIn("repository", body)

    def test_forced_receipt_history_retains_multiple_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._complete_forced_transition(
                root, message_id="m1", correlation_id="c1"
            )
            self._complete_forced_transition(
                root, message_id="m2", correlation_id="c2"
            )

            history = _read_jsonl(
                root
                / "recovery"
                / "runtime-controller"
                / "forced-failover-history.jsonl"
            )
            latest = json.loads(
                (
                    root
                    / "recovery"
                    / "runtime-controller"
                    / "forced-failover.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual([item["message_id"] for item in history], ["m1", "m2"])
            self.assertEqual(latest["message_id"], "m2")

    def test_forced_retry_preserves_message_and_correlation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", profile="primary")
            fallback = _binding(root, harness="codex", profile="fallback")
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(
                binding.mailbox,
                {"message": "work", "correlation_id": "correlation-1"},
                "tester",
            )
            snapshot = runtime_quota.QuotaSnapshot(
                profile="claude/primary",
                windows=(),
                observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                freshness="unavailable",
                evidence={"authoritative_limit": "true"},
            )
            current = rc.RuntimeHandle(
                binding.launch, binding.controller.tmux, "s1", rc._utc_now()
            )
            with mock.patch.object(
                rc, "recorded_runtime_handle", return_value=current
            ), mock.patch.object(
                rc, "forced_swap", side_effect=rc.RuntimeTransitionPending("read")
            ) as forced:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(binding, snapshot),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )

            self.assertEqual(forced.call_args.kwargs["message_id"], message_id)
            self.assertEqual(
                forced.call_args.kwargs["correlation_id"], "correlation-1"
            )
            self.assertTrue((binding.mailbox / "new" / message_id).is_file())

    def test_submission_context_blocks_forbidden_capped_submission_after_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error = self._current_and_target(root)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            fallback_handle = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                rc, "_existing_graveyard", return_value=evidence
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=fallback_handle
            ), mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                    )
                with self.assertRaises(rc.ProviderQuotaExhausted):
                    rc.submit_to_runtime(
                        current,
                        ExecutionRequest(prompt="forbidden", cwd=root),
                        persona="recovery",
                        state_root=root,
                        message_id="m1",
                        correlation_id="c1",
                    )

            events = _read_jsonl(
                root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
            )
            self.assertEqual(run.call_count, 1)
            breaker_index = next(
                index
                for index, event in enumerate(events)
                if event["event"] == "breaker_opened"
            )
            after_breaker = events[breaker_index + 1 :]
            fallback_submissions = [
                event
                for event in after_breaker
                if event["event"] == "runtime_submission_accepted"
                and event["harness"] == "codex"
                and event["profile"] == "fallback"
            ]
            blocked_submissions = [
                event
                for event in after_breaker
                if event["event"] == "runtime_submission_blocked"
                and event["harness"] == "claude"
                and event["profile"] == "primary"
            ]
            forbidden_attempts = [
                event
                for event in after_breaker
                if event["event"] == "runtime_submission_attempt"
                and event["harness"] == "claude"
                and event["profile"] == "primary"
            ]
            forbidden_accepted = [
                event
                for event in after_breaker
                if event["event"] == "runtime_submission_accepted"
                and event["harness"] == "claude"
                and event["profile"] == "primary"
            ]
            self.assertEqual(len(fallback_submissions), 1)
            self.assertEqual(len(blocked_submissions), 1)
            self.assertEqual(forbidden_attempts, [])
            self.assertEqual(forbidden_accepted, [])
            self.assertEqual(blocked_submissions[0]["message_id"], "m1")

    def test_cap_circuit_breaker_prevents_remaining_claims_from_invoking_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, target, error = self._current_and_target(root)
            with mock.patch.object(rc, "_existing_graveyard", return_value=None), mock.patch.object(
                rc.graveyard,
                "archive_required",
                side_effect=rc.graveyard.GraveyardDurabilityError("index fsync failed"),
            ), mock.patch.object(rc, "_replace_runtime"):
                with self.assertRaises(rc.ProviderQuotaExhausted):
                    rc.forced_swap(
                        current,
                        target,
                        error=error,
                        message_id="m1",
                        correlation_id="c1",
                        drain_id="d1",
                        state_root=root,
                    )
            current_binding = _binding(root, harness="claude", profile="primary")
            with mock.patch.object(rc, "_start_runtime") as start:
                with self.assertRaises(rc.ProviderQuotaExhausted):
                    rc.ensure_runtime(
                        current_binding,
                        _Adapter(current_binding),
                        state_root=root,
                    )
            start.assert_not_called()

    def test_forced_archive_or_receipt_failure_blocks_fallback_and_requeues_transiently(self) -> None:
        for failure_point in ("archive", "receipt"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                current, target, error = self._current_and_target(root)
                evidence = root / "existing.md"
                evidence.write_text("facts", encoding="utf-8")
                existing = None if failure_point == "archive" else evidence
                archive_error = (
                    rc.graveyard.GraveyardDurabilityError("snapshot/index failure")
                    if failure_point == "archive"
                    else mock.DEFAULT
                )
                with mock.patch.object(rc, "_existing_graveyard", return_value=existing), mock.patch.object(
                    rc.graveyard,
                    "archive_required",
                    side_effect=archive_error,
                ), mock.patch.object(
                    rc,
                    "_strict_receipt",
                    side_effect=(
                        rc.graveyard.GraveyardDurabilityError("receipt rename failure")
                        if failure_point == "receipt"
                        else None
                    ),
                ), mock.patch.object(rc, "_replace_runtime") as replace:
                    with self.assertRaises(rc.ProviderQuotaExhausted) as caught:
                        rc.forced_swap(
                            current,
                            target,
                            error=error,
                            message_id="m1",
                            correlation_id="c1",
                            drain_id="d1",
                            state_root=root,
                        )

                self.assertTrue(caught.exception.mailbox_transient)
                replace.assert_not_called()
                self.assertTrue(
                    rc.transition_state("recovery", state_root=root)["breaker_open"]
                )


class TmuxRuntimeContextThreadingTests(unittest.TestCase):
    """T-2589: optional named-socket/scope-unit seam for off-prod testability.

    Every case pins TWO things: the DEFAULT path is byte-identical to the
    pre-T-2589 call shape (no new kwargs leak in anywhere), and a supplied
    ``TmuxRuntimeContext`` actually reaches the tmux/systemd-run argv.
    """

    def test_run_tmux_default_omits_socket_flag(self) -> None:
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc._run_tmux("has-session", "-t", "x")
        self.assertEqual(run.call_args.args[0], ["tmux", "has-session", "-t", "x"])

    def test_run_tmux_with_context_prepends_socket_flag(self) -> None:
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        context = rc.TmuxRuntimeContext(socket="disposable-sock")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc._run_tmux("has-session", "-t", "x", context=context)
        self.assertEqual(
            run.call_args.args[0],
            ["tmux", "-L", "disposable-sock", "has-session", "-t", "x"],
        )

    def test_run_tmux_context_without_socket_still_omits_flag(self) -> None:
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        context = rc.TmuxRuntimeContext(scope_unit="only-scope-differs")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc._run_tmux("has-session", "-t", "x", context=context)
        self.assertEqual(run.call_args.args[0], ["tmux", "has-session", "-t", "x"])

    def _assert_forwards_context(self, func, args) -> None:
        context = rc.TmuxRuntimeContext(socket="disposable-sock")
        completed = subprocess.CompletedProcess(("tmux",), 0, "123\n", "")
        with mock.patch.object(rc, "_run_tmux", return_value=completed) as run:
            func(*args, context=context)
        self.assertIs(run.call_args.kwargs.get("context"), context)
        with mock.patch.object(rc, "_run_tmux", return_value=completed) as run:
            func(*args)
        self.assertNotIn("context", run.call_args.kwargs)

    def test_pane_exists_forwards_context_only_when_given(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        self._assert_forwards_context(rc._pane_exists, (target,))

    def test_target_known_forwards_context_only_when_given(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        self._assert_forwards_context(rc._target_known, (target,))

    def test_session_exists_forwards_context_only_when_given(self) -> None:
        self._assert_forwards_context(rc._session_exists, ("pantheon-recovery",))

    def test_window_exists_forwards_context_only_when_given(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        self._assert_forwards_context(rc._window_exists, (target,))

    def test_default_tmux_server_pid_forwards_context_only_when_given(self) -> None:
        self._assert_forwards_context(rc._default_tmux_server_pid, ())

    def test_capture_visible_runtime_pane_forwards_context_only_when_given(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        self._assert_forwards_context(rc._capture_visible_runtime_pane, (target,))

    def test_start_new_server_in_scope_default_omits_socket_flag(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        completed = subprocess.CompletedProcess(("systemd-run",), 0, "", "")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc._start_new_server_in_scope(
                target,
                cwd=Path("/tmp"),
                command_argv=("/usr/bin/codex",),
                scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
                account_home=Path("/home/claude-research"),
            )
        argv = run.call_args.args[0]
        self.assertNotIn("-L", argv)
        self.assertEqual(
            argv[6],
            "--setenv=PATH=/home/claude-research/.bun/bin:"
            "/home/claude-research/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        )
        self.assertEqual(argv[7], "tmux")
        self.assertEqual(argv[8], "new-session")

    def test_start_new_server_in_scope_with_context_inserts_socket_flag(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        completed = subprocess.CompletedProcess(("systemd-run",), 0, "", "")
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc._start_new_server_in_scope(
                target,
                cwd=Path("/tmp"),
                command_argv=("/usr/bin/codex",),
                scope_unit=context.scope_unit,
                account_home=Path("/home/claude-research"),
                context=context,
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[5], "--unit=disposable.scope")
        self.assertEqual(
            argv[6],
            "--setenv=PATH=/home/claude-research/.bun/bin:"
            "/home/claude-research/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        )
        self.assertEqual(argv[7], "tmux")
        self.assertEqual(argv[8:10], ["-L", "disposable-sock"])
        self.assertEqual(argv[10], "new-session")

    def test_start_new_server_in_scope_setenv_path_is_persona_general(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        completed = subprocess.CompletedProcess(("systemd-run",), 0, "", "")
        for account_home in (Path("/home/claude-research"), Path("/home/claude-fallback")):
            with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
                rc._start_new_server_in_scope(
                    target,
                    cwd=Path("/tmp"),
                    command_argv=("/usr/bin/codex",),
                    scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
                    account_home=account_home,
                )
            argv = run.call_args.args[0]
            self.assertIn(
                f"--setenv=PATH={account_home}/.bun/bin:"
                f"{account_home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
                argv,
            )

    def test_scope_launch_path_is_persona_general(self) -> None:
        self.assertEqual(
            rc._scope_launch_path(Path("/home/claude-research")),
            "/home/claude-research/.bun/bin:/home/claude-research/.npm-global/bin:"
            "/usr/local/bin:/usr/bin:/bin",
        )
        self.assertEqual(
            rc._scope_launch_path(Path("/home/claude-fallback")),
            "/home/claude-fallback/.bun/bin:/home/claude-fallback/.npm-global/bin:"
            "/usr/local/bin:/usr/bin:/bin",
        )

    def test_start_runtime_with_context_uses_custom_scope_for_new_server(self) -> None:
        binding = _binding(Path("/tmp"))
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        completed = subprocess.CompletedProcess(("systemd-run",), 0, "", "")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=None
        ) as server_pid, mock.patch.object(
            rc, "_prepare_runtime_scope"
        ) as prepare, mock.patch.object(
            rc.subprocess, "run", return_value=completed
        ) as run, mock.patch.object(rc, "ensure_project_trusted"), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ), mock.patch.object(rc, "ensure_model_migration_prompts_hidden"):
            started = rc._start_runtime(
                binding, _Adapter(binding), replace=False, context=context
            )
        self.assertEqual(started.scope_unit, "disposable.scope")
        prepare.assert_called_once_with("disposable.scope")
        self.assertIs(server_pid.call_args.kwargs.get("context"), context)
        argv = run.call_args.args[0]
        self.assertIn("--unit=disposable.scope", argv)
        tmux_index = argv.index("tmux")
        self.assertEqual(argv[tmux_index + 1 : tmux_index + 3], ["-L", "disposable-sock"])

    def test_start_runtime_with_context_forwards_to_existing_server_checks(self) -> None:
        binding = _binding(Path("/tmp"))
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ) as pid_in_scope, mock.patch.object(
            rc, "_target_known", return_value=False
        ) as target_known, mock.patch.object(
            rc, "_session_exists", return_value=False
        ) as session_exists, mock.patch.object(
            rc, "_run_tmux", return_value=completed
        ) as run, mock.patch.object(rc, "ensure_project_trusted"), mock.patch.object(
            rc, "ensure_update_check_disabled"
        ), mock.patch.object(rc, "ensure_model_migration_prompts_hidden"):
            started = rc._start_runtime(
                binding, _Adapter(binding), replace=False, context=context
            )
        self.assertEqual(started.scope_unit, "disposable.scope")
        pid_in_scope.assert_called_once_with(123, "disposable.scope")
        self.assertIs(target_known.call_args.kwargs.get("context"), context)
        self.assertIs(session_exists.call_args.kwargs.get("context"), context)
        self.assertIs(run.call_args.kwargs.get("context"), context)

    def test_wait_for_live_runtime_forwards_context_to_pane_and_pid_checks(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        context = rc.TmuxRuntimeContext(socket="disposable-sock")
        with mock.patch.object(
            rc, "_pane_exists", return_value=True
        ) as pane_exists, mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ) as server_pid, mock.patch.object(rc, "_pid_in_scope", return_value=True):
            self.assertTrue(
                rc._wait_for_live_runtime(target, rc.TMUX_RUNTIME_SCOPE_UNIT, context=context)
            )
        self.assertIs(pane_exists.call_args.kwargs.get("context"), context)
        self.assertIs(server_pid.call_args.kwargs.get("context"), context)

    def test_wait_for_live_runtime_default_omits_context_kwarg(self) -> None:
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        with mock.patch.object(
            rc, "_pane_exists", return_value=True
        ) as pane_exists, mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ) as server_pid, mock.patch.object(rc, "_pid_in_scope", return_value=True):
            self.assertTrue(rc._wait_for_live_runtime(target, rc.TMUX_RUNTIME_SCOPE_UNIT))
        self.assertNotIn("context", pane_exists.call_args.kwargs)
        self.assertNotIn("context", server_pid.call_args.kwargs)

    def test_verify_started_runtime_forwards_context_to_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            context = rc.TmuxRuntimeContext(socket="disposable-sock")
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            with mock.patch.object(rc, "_wait_for_live_runtime", return_value=True) as wait:
                rc._verify_started_runtime(binding, started, state_root=root, context=context)
            self.assertIs(wait.call_args.kwargs.get("context"), context)

    def test_replace_runtime_forwards_context_to_start_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
            with mock.patch.object(
                rc, "_target_known", return_value=False
            ) as target_known, mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart("disposable.scope", True)
            ) as start, mock.patch.object(
                rc, "_verify_started_runtime"
            ) as verify, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ) as ready, _provenance_patch():
                rc._replace_runtime(binding, state_root=root, context=context)
            self.assertIs(target_known.call_args.kwargs.get("context"), context)
            self.assertIs(start.call_args.kwargs.get("context"), context)
            self.assertIs(verify.call_args.kwargs.get("context"), context)
            self.assertIs(ready.call_args.kwargs.get("context"), context)

    def test_submit_to_runtime_adds_socket_flag_when_context_given(self) -> None:
        binding = _binding(Path("/tmp"))
        handle = rc.RuntimeHandle(
            binding.launch, binding.controller.tmux, None, rc._utc_now()
        )
        context = rc.TmuxRuntimeContext(socket="disposable-sock")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc.submit_to_runtime(
                handle,
                ExecutionRequest(prompt="work", cwd=Path("/tmp")),
                context=context,
            )
        argv = run.call_args.args[0]
        self.assertIn("--socket", argv)
        self.assertEqual(argv[argv.index("--socket") + 1], "disposable-sock")
        self.assertEqual(argv[-2:], ["pantheon-recovery:harness.0", "work"])

    def test_submit_to_runtime_without_socket_context_omits_flag(self) -> None:
        binding = _binding(Path("/tmp"))
        handle = rc.RuntimeHandle(
            binding.launch, binding.controller.tmux, None, rc._utc_now()
        )
        context = rc.TmuxRuntimeContext(scope_unit="only-scope-differs")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run:
            rc.submit_to_runtime(
                handle,
                ExecutionRequest(prompt="work", cwd=Path("/tmp")),
                context=context,
            )
        argv = run.call_args.args[0]
        self.assertNotIn("--socket", argv)

    def test_prompt_successor_forwards_context_only_when_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            handle = rc.RuntimeHandle(
                binding.launch, binding.controller.tmux, None, rc._utc_now()
            )
            evidence = root / "evidence.md"
            context = rc.TmuxRuntimeContext(socket="disposable-sock")
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(
                    handle, evidence, persona="recovery", state_root=root, context=context
                )
            self.assertIs(submit.call_args.kwargs.get("context"), context)
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(handle, evidence, persona="recovery", state_root=root)
            self.assertNotIn("context", submit.call_args.kwargs)


class StateMatchesScopeUnitTests(unittest.TestCase):
    """T-2589: _state_matches must compare against the CALLER'S scope, not
    always the production singleton -- otherwise a disposable-scope swap's
    own runtime.json would never match its own binding."""

    def _base_state(self, binding, *, runtime_scope_unit: str) -> dict[str, object]:
        return {
            "persona": binding.persona,
            "role": binding.role,
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "executable": str(binding.launch.executable),
            "model": binding.launch.model,
            "tmux_target": rc._target_text(binding.controller.tmux),
            "runtime_scope_unit": runtime_scope_unit,
        }

    def test_default_compares_against_singleton(self) -> None:
        binding = _binding(Path("/tmp"))
        state = self._base_state(binding, runtime_scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT)
        self.assertTrue(rc._state_matches(binding, state))

    def test_default_rejects_non_singleton_scope_unit(self) -> None:
        binding = _binding(Path("/tmp"))
        state = self._base_state(binding, runtime_scope_unit="disposable.scope")
        self.assertFalse(rc._state_matches(binding, state))

    def test_context_compares_against_its_own_scope_unit(self) -> None:
        binding = _binding(Path("/tmp"))
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        state = self._base_state(binding, runtime_scope_unit="disposable.scope")
        self.assertTrue(rc._state_matches(binding, state, context=context))
        state["runtime_scope_unit"] = rc.TMUX_RUNTIME_SCOPE_UNIT
        self.assertFalse(rc._state_matches(binding, state, context=context))


class PlannedSwapContextForwardingTests(unittest.TestCase):
    """T-2589: planned_swap's context reaches _replace_runtime, byte-identical
    default when omitted. Mirrors test_planned_failover_archives_verified_
    model_handoff_before_exit's two-call (request-then-verified) shape."""

    def _run_two_phase_swap(self, *, context: "rc.TmuxRuntimeContext | None"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            handoff = root / "HANDOFF.md"
            handoff.write_text("# Transfer\nCurrent work is durable.\n", encoding="utf-8")
            archive = root / "archive.md"
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            kwargs = {} if context is None else {"context": context}
            with mock.patch.object(
                rc.graveyard, "archive_required", return_value=archive
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ) as replace, mock.patch.object(
                rc, "_prompt_successor"
            ) as prompt_successor, mock.patch.object(
                rc, "submit_to_runtime"
            ) as submit:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                        **kwargs,
                    )
                # Phase 1's handoff-request submit_to_runtime call happened above,
                # on `current` -- capture it before phase 2 makes no further
                # submit_to_runtime call of its own (that one lives inside the
                # mocked _prompt_successor).
                handoff_request_call = submit.call_args
                handoff.write_text(
                    "# Transfer\nCurrent work is durable and freshly reviewed.\n",
                    encoding="utf-8",
                )
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                        **kwargs,
                    )
            return replace, prompt_successor, handoff_request_call

    def test_context_forwarded_to_replace_runtime(self) -> None:
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        replace, _prompt_successor, _handoff_call = self._run_two_phase_swap(context=context)
        self.assertIs(replace.call_args.kwargs.get("context"), context)

    def test_default_omits_context_kwarg_to_replace_runtime(self) -> None:
        replace, _prompt_successor, _handoff_call = self._run_two_phase_swap(context=None)
        self.assertNotIn("context", replace.call_args.kwargs)

    def test_context_forwarded_to_prompt_successor(self) -> None:
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        _replace, prompt_successor, _handoff_call = self._run_two_phase_swap(context=context)
        self.assertIs(prompt_successor.call_args.kwargs.get("context"), context)

    def test_default_omits_context_kwarg_to_prompt_successor(self) -> None:
        _replace, prompt_successor, _handoff_call = self._run_two_phase_swap(context=None)
        self.assertNotIn("context", prompt_successor.call_args.kwargs)

    def test_context_forwarded_to_handoff_request_submit(self) -> None:
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        _replace, _prompt_successor, handoff_call = self._run_two_phase_swap(context=context)
        self.assertIs(handoff_call.kwargs.get("context"), context)

    def test_default_omits_context_kwarg_from_handoff_request_submit(self) -> None:
        _replace, _prompt_successor, handoff_call = self._run_two_phase_swap(context=None)
        self.assertNotIn("context", handoff_call.kwargs)


class PlannedSwapEndToEndSocketTests(unittest.TestCase):
    """T-2589: un-mocked proof that BOTH real submit_to_runtime calls inside
    planned_swap (the direct handoff-request on `current`, and the one
    inside the real _prompt_successor on `replacement`) actually carry the
    disposable socket into the send_keys_submit.sh argv -- not just that the
    context kwarg is passed between mocked-out layers. Only `_replace_runtime`
    (real tmux/systemd-run start) and `graveyard.archive_required` are
    mocked; `_prompt_successor` and `submit_to_runtime` run for real, with
    `subprocess.run` intercepted at the one seam both of them ultimately
    call (Reviewer-requested sharpening, T-2589 rebound)."""

    def test_both_real_submits_carry_the_disposable_socket(self) -> None:
        context = rc.TmuxRuntimeContext(socket="disposable-sock", scope_unit="disposable.scope")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            handoff = root / "HANDOFF.md"
            handoff.write_text("# Transfer\nCurrent work is durable.\n", encoding="utf-8")
            archive = root / "archive.md"
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                rc.graveyard, "archive_required", return_value=archive
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(
                rc.subprocess, "run", return_value=completed
            ) as run:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                        context=context,
                    )
                handoff.write_text(
                    "# Transfer\nCurrent work is durable and freshly reviewed.\n",
                    encoding="utf-8",
                )
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                        context=context,
                    )

            # Exactly two real submit_to_runtime -> subprocess.run calls
            # happened: phase 1's handoff request (on `current`) and phase
            # 2's real _prompt_successor prompt (on `replacement`). Neither
            # is mocked out in this test, so both must independently carry
            # the socket all the way to the send_keys_submit.sh argv.
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                argv = call.args[0]
                self.assertEqual(Path(argv[0]), rc.SEND_KEYS_HELPER)
                self.assertIn("--socket", argv)
                self.assertEqual(argv[argv.index("--socket") + 1], "disposable-sock")

    def test_default_neither_real_submit_carries_a_socket_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_binding = _binding(root, harness="claude", profile="primary")
            target = _binding(root, harness="codex", profile="fallback")
            handoff = root / "HANDOFF.md"
            handoff.write_text("# Transfer\nCurrent work is durable.\n", encoding="utf-8")
            archive = root / "archive.md"
            current = rc.RuntimeHandle(
                current_binding.launch,
                current_binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                rc.graveyard, "archive_required", return_value=archive
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(
                rc.subprocess, "run", return_value=completed
            ) as run:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                    )
                handoff.write_text(
                    "# Transfer\nCurrent work is durable and freshly reviewed.\n",
                    encoding="utf-8",
                )
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        handoff_path=handoff,
                        state_root=root,
                    )

            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                argv = call.args[0]
                self.assertNotIn("--socket", argv)


if __name__ == "__main__":
    unittest.main()
