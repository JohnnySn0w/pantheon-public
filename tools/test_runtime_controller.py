"""Deterministic tests for the persistent persona runtime controller."""

from __future__ import annotations

import dataclasses
import base64
import hashlib
import io
import json
import os
import pwd
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import contextlib
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping
from unittest import mock

from tools.harness_adapter.types import (
    CommandSpec,
    DeliveryReceipt,
    DiagnosticReport,
    ExecutionRequest,
    FailoverPolicy,
    HarnessLaunchConfig,
    PostStartAssessment,
    PostStartCommand,
    RuntimeControllerConfig,
    TmuxTarget,
)
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.types import (
    ReceiverHookReceipt,
    ReceiverStateProof,
    RuntimeReadinessProof,
    SuccessorEvidenceContract,
)
from tools import codex_modal
from tools import runtime_controller as rc
from tools import mailbox
from tools import proactive_swap_init
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


REPO_ROOT = Path(__file__).resolve().parent.parent


class _ReceiverBackend:
    def __init__(self, *, fail: str | None = None, old_present_after_stop: bool = False, sustained_409: bool = False):
        self.fail = fail
        self.old_present_after_stop = old_present_after_stop
        self.sustained_409 = sustained_409
        self.events: list[str] = []
        self.killed_patterns: list[tuple[str, str, str]] = []
        self.used_target_llm = False

    def execute(self, action, binding):
        if action == self.fail:
            raise RuntimeError(f"fail {action}")
        self.events.append(f"{binding.persona}:{action}")
        if action == "probe-claude-absent" and self.old_present_after_stop:
            raise RuntimeError("old receiver still present")
        if action == "health-codex-no-409" and self.sustained_409:
            raise RuntimeError("sustained getUpdates 409")
        return {"ok": True, "action": action}

    def prove_account_process_absent(self, *, account: str, session: str, path: str):
        self.events.append(f"probe:{account}:{session}:{path}")
        return True


def _launch(harness: str) -> HarnessLaunchConfig:
    return HarnessLaunchConfig(
        harness=harness,
        profile="interactive",
        executable=Path("/usr/bin/codex" if harness == "codex" else "/usr/bin/claude"),
        model=None,
        mode="interactive",
        meter="subscription",
        quota_source="app-server" if harness == "codex" else "statusline",
        native={},
        sources=("test",),
    )


class ReceiverHandoffTests(unittest.TestCase):
    def test_plan_recovery_orders_watchdog_stop_before_receiver_cutover(self) -> None:
        binding = SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            selected_harness="codex",
            communications={
                "kind": "telegram",
                "claude_receiver_topology": "runtime-owned",
                "watchdog_units": ("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service"),
            },
        )
        actions = rc.receiver_handoff_actions(binding, "claude-to-codex", "pre")
        self.assertLess(actions.index("disable-watchdog-timer"), actions.index("disable-dormant-claude-receiver"))
        self.assertLess(actions.index("stop-watchdog-service"), actions.index("stop-dormant-claude-receiver"))
        self.assertNotIn("probe-claude-absent", actions)

    def test_non_telegram_persona_has_no_receiver_handoff(self) -> None:
        current = _binding(Path("/tmp"), harness="claude")
        target = _binding(Path("/tmp"), harness="codex")
        current.communications = None
        target.communications = None
        self.assertFalse(rc.receiver_handoff_required(target))
        self.assertEqual(rc._receiver_direction_for_bindings(current, target), "none")
        recovery_without_spec = SimpleNamespace(persona="recovery", communications=None)
        self.assertNotIn(
            "disable-watchdog-timer",
            rc.receiver_handoff_actions(recovery_without_spec, "claude-to-codex", "pre"),
        )

    def test_runtime_controller_core_does_not_embed_receiver_action_names(self) -> None:
        source = Path(rc.__file__).read_text(encoding="utf-8")
        for action in (
            "snapshot-watchdog-units",
            "restore-watchdog-units",
            "disable-watchdog-timer",
            "stop-watchdog-service",
            "disable-dormant-claude-receiver",
            "stop-dormant-claude-receiver",
            "probe-claude-present",
            "probe-claude-absent",
            "start-codex-poller",
        ):
            with self.subTest(action=action):
                self.assertNotIn(action, source)

    def test_cutover_refuses_while_old_receiver_is_present(self) -> None:
        backend = _ReceiverBackend(old_present_after_stop=True)
        binding = SimpleNamespace(persona="specialist", account="claude-research", selected_harness="codex", communications={"claude_receiver_topology": "systemd-user"})
        with self.assertRaises(RuntimeError):
            rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        self.assertNotIn("specialist:start-codex-poller", backend.events)

    def test_cutover_stops_old_before_starting_new_receiver(self) -> None:
        backend = _ReceiverBackend()
        binding = SimpleNamespace(persona="specialist", account="claude-research", selected_harness="codex", communications={"claude_receiver_topology": "systemd-user"})
        rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        rc.run_receiver_handoff(binding, "claude-to-codex", "post", backend=backend)
        self.assertLess(backend.events.index("specialist:stop-claude-receiver"), backend.events.index("specialist:start-codex-poller"))

    def test_restore_stops_new_before_starting_old_receiver(self) -> None:
        backend = _ReceiverBackend()
        binding = SimpleNamespace(persona="specialist", account="claude-research", selected_harness="claude", communications={"claude_receiver_topology": "systemd-user"})
        rc.run_receiver_handoff(binding, "codex-to-claude", "pre", backend=backend)
        rc.run_receiver_handoff(binding, "codex-to-claude", "post", backend=backend)
        self.assertLess(backend.events.index("specialist:stop-codex-poller"), backend.events.index("specialist:restore-claude-receiver"))

    def test_recovery_watchdog_restores_only_after_codex_receiver_stops(self) -> None:
        backend = _ReceiverBackend()
        binding = SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            selected_harness="claude",
            communications={
                "kind": "telegram",
                "claude_receiver_topology": "systemd-user",
                "watchdog_units": ("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service"),
            },
        )
        rc.run_receiver_handoff(binding, "codex-to-claude", "pre", backend=backend)
        rc.run_receiver_handoff(binding, "codex-to-claude", "post", backend=backend)
        self.assertLess(backend.events.index("recovery:probe-codex-absent"), backend.events.index("recovery:restore-watchdog-units"))

    def test_runtime_owned_pre_failure_restores_watchdog_inside_same_handoff(self) -> None:
        backend = _ReceiverBackend(fail="probe-claude-present")
        binding = SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            selected_harness="codex",
            communications=_telegram_comms(),
        )
        with self.assertRaises(RuntimeError):
            rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        self.assertIn("recovery:stop-watchdog-service", backend.events)
        self.assertNotIn("recovery:restore-claude-receiver", backend.events)
        self.assertIn("recovery:restore-watchdog-units", backend.events)
        self.assertLess(backend.events.index("recovery:stop-watchdog-service"), backend.events.index("recovery:restore-watchdog-units"))

    def test_systemd_pre_failure_restores_watchdog_inside_same_handoff(self) -> None:
        backend = _ReceiverBackend(fail="probe-claude-absent")
        binding = SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            selected_harness="codex",
            communications={
                "kind": "telegram",
                "claude_receiver_topology": "systemd-user",
                "watchdog_units": ("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service"),
            },
        )
        with self.assertRaises(RuntimeError):
            rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        self.assertIn("recovery:stop-watchdog-service", backend.events)
        self.assertIn("recovery:restore-claude-receiver", backend.events)
        self.assertIn("recovery:restore-watchdog-units", backend.events)
        self.assertIn("recovery:complete-receiver-topology-snapshot", backend.events)
        self.assertLess(backend.events.index("recovery:stop-claude-receiver"), backend.events.index("recovery:restore-claude-receiver"))
        self.assertLess(backend.events.index("recovery:stop-watchdog-service"), backend.events.index("recovery:restore-watchdog-units"))

    def test_sustained_409_fails_health_gate_and_rolls_back(self) -> None:
        backend = _ReceiverBackend(sustained_409=True)
        current = SimpleNamespace(persona="specialist", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        target.persona = "specialist"
        target.account = "claude-research"
        with self.assertRaises(RuntimeError):
            rc.transition_runtime_with_receiver_handoff(
                current,
                target,
                backend=backend,
                replace=lambda b: rc.RuntimeHandle(target.launch, target.controller.tmux, None, datetime.now(timezone.utc)),
            )
        self.assertIn("specialist:probe-claude-present", backend.events)
        self.assertIn("specialist:restore-watchdog-units", backend.events)
        self.assertNotIn("specialist:start-claude-receiver", backend.events)

    def test_planned_swap_runs_receiver_handoff_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff = root / "HANDOFF.md"
            handoff.write_text("handoff\n", encoding="utf-8")
            current = rc.RuntimeHandle(_launch("claude"), TmuxTarget("s", "w", "0"), None, datetime.now(timezone.utc))
            target = _binding(root, harness="codex")
            target.account = "claude-fallback"
            state = rc._begin_transition(
                target.persona,
                trigger="planned-quota",
                outgoing_profile=current.profile.profile,
                incoming_profile=target.launch.profile,
                outgoing_harness=current.profile.harness,
                incoming_harness=target.launch.harness,
                drain_id=None,
                state_root=root,
            )
            state["handoff_requested"] = True
            state["handoff_baseline_sha256"] = hashlib.sha256(b"old\n").hexdigest()
            state["handoff_baseline_mtime_ns"] = 1
            rc._atomic_json(rc._transition_path(target.persona, root), state)
            with mock.patch.object(rc, "transition_runtime_with_receiver_handoff", side_effect=RuntimeError("hooked")) as hook:
                with self.assertRaises(RuntimeError):
                    rc.planned_swap(current, target, message_id="m", handoff_path=handoff, state_root=root)
            hook.assert_called_once()

    def test_forced_swap_runs_receiver_handoff_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = rc.RuntimeHandle(_launch("claude"), TmuxTarget("s", "w", "0"), "sess", datetime.now(timezone.utc))
            target = _binding(root, harness="codex")
            target.account = "claude-fallback"
            error = rc.ProviderQuotaExhausted("capped", profile="interactive", provider_error="capped")
            with mock.patch.object(rc, "transition_runtime_with_receiver_handoff", side_effect=RuntimeError("forced-hooked")) as hook:
                with self.assertRaises(RuntimeError):
                    rc.forced_swap(current, target, error=error, message_id="m", correlation_id="c", state_root=root)
            hook.assert_called_once()

    def test_failed_handoff_keeps_or_restores_last_proven_receiver(self) -> None:
        backend = _ReceiverBackend(fail="start-codex-poller")
        current = SimpleNamespace(persona="specialist", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        target.persona = "specialist"
        target.account = "claude-research"
        with self.assertRaises(RuntimeError):
            rc.transition_runtime_with_receiver_handoff(current, target, backend=backend, replace=lambda b: rc.RuntimeHandle(target.launch, target.controller.tmux, None, datetime.now(timezone.utc)))
        self.assertIn("specialist:probe-claude-present", backend.events)
        self.assertIn("specialist:restore-watchdog-units", backend.events)
        self.assertNotIn("specialist:start-claude-receiver", backend.events)

    def test_transition_handoff_typed_mutated_replace_failure_restores_source(self) -> None:
        backend = _ReceiverBackend()
        current = SimpleNamespace(persona="recovery", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        calls: list[str] = []

        def replace(binding):
            calls.append(binding.launch.harness)
            if binding.launch.harness == "codex":
                raise _mutated_replace_error()
            return rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())

        with self.assertRaises(rc.RuntimeReplacementError):
            rc.transition_runtime_with_receiver_handoff(current, target, backend=backend, replace=replace)

        self.assertEqual(calls, ["codex", "claude"])
        self.assertIn("recovery:stop-codex-poller", backend.events)
        self.assertIn("recovery:restore-watchdog-units", backend.events)

    def test_transition_handoff_typed_prestart_failure_does_not_restore_source(self) -> None:
        backend = _ReceiverBackend()
        current = SimpleNamespace(persona="recovery", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        calls: list[str] = []

        def replace(binding):
            calls.append(binding.launch.harness)
            raise _prestart_replace_error()

        with self.assertRaises(rc.RuntimeReplacementError):
            rc.transition_runtime_with_receiver_handoff(current, target, backend=backend, replace=replace)

        self.assertEqual(calls, ["codex"])
        self.assertNotIn("recovery:stop-codex-poller", backend.events)
        self.assertIn("recovery:restore-watchdog-units", backend.events)

    def test_transition_handoff_plain_replace_failure_does_not_overclaim_mutation(self) -> None:
        backend = _ReceiverBackend()
        current = SimpleNamespace(persona="recovery", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        calls: list[str] = []

        def replace(binding):
            calls.append(binding.launch.harness)
            raise RuntimeError("plain failure")

        with self.assertRaisesRegex(RuntimeError, "plain failure"):
            rc.transition_runtime_with_receiver_handoff(current, target, backend=backend, replace=replace)

        self.assertEqual(calls, ["codex"])
        self.assertNotIn("recovery:stop-codex-poller", backend.events)

    def test_transition_handoff_custom_success_callback_runtime_handle_unchanged(self) -> None:
        backend = _ReceiverBackend()
        current = SimpleNamespace(persona="recovery", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())
        handle = rc.RuntimeHandle(target.launch, target.controller.tmux, None, rc._utc_now())

        result = rc.transition_runtime_with_receiver_handoff(
            current,
            target,
            backend=backend,
            replace=lambda binding: handle,
        )

        self.assertIs(result, handle)
        self.assertIn("recovery:start-codex-poller", backend.events)

    def test_transition_handoff_mutated_restore_failure_reports_compensation_failed(self) -> None:
        backend = _ReceiverBackend()
        current = SimpleNamespace(persona="recovery", profile=_launch("claude"))
        target = _binding(Path("/tmp"), harness="codex", communications=_telegram_comms())

        def replace(binding):
            if binding.launch.harness == "codex":
                raise _mutated_replace_error()
            raise RuntimeError("source restore failed")

        with self.assertRaisesRegex(RuntimeError, "compensation failed"):
            rc.transition_runtime_with_receiver_handoff(current, target, backend=backend, replace=replace)

        self.assertIn("recovery:stop-codex-poller", backend.events)

    def test_capped_claude_cutover_requires_no_target_cooperation(self) -> None:
        backend = _ReceiverBackend()
        binding = SimpleNamespace(persona="specialist", account="claude-research", selected_harness="codex", communications=_telegram_comms())
        rc.run_receiver_handoff(binding, "claude-to-codex", "pre", backend=backend)
        self.assertFalse(backend.used_target_llm)

    def test_external_rollback_verifies_units_and_processes_directly(self) -> None:
        backend = _ReceiverBackend()
        binding = SimpleNamespace(persona="specialist", account="claude-research", selected_harness="claude", communications={"claude_receiver_topology": "systemd-user"})
        rc.run_receiver_handoff(binding, "rollback-to-claude", "post", backend=backend)
        backend.prove_account_process_absent(account="claude-research", session="claude-research-bot", path="/home/claude-research")
        self.assertIn("probe:claude-research:claude-research-bot:/home/claude-research", backend.events)

    def test_wrong_account_process_cannot_satisfy_or_be_killed(self) -> None:
        self.assertFalse(rc.process_probe_matches({"account": "other", "session": "s", "path": "/x"}, account="claude-research", session="s", path="/x"))

    def test_load_binding_sets_launch_cwd_to_manifest_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            binding = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")

            self.assertEqual(binding.launch_cwd, root.resolve())

    def test_start_runtime_uses_binding_launch_cwd_not_process_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            adapter = _RecordingAdapter(binding)
            observed: dict[str, Path] = {}

            def start_new_server(target, *, cwd, command_argv, scope_unit, account_home, context=None):
                del target, command_argv, scope_unit, account_home, context
                observed["cwd"] = cwd
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with mock.patch.object(rc, "_default_tmux_server_pid", return_value=None), mock.patch.object(
                rc, "_prepare_runtime_scope"
            ), mock.patch.object(rc, "_start_new_server_in_scope", side_effect=start_new_server):
                rc._start_runtime(binding, adapter, replace=False)

            self.assertEqual(adapter.launch_cwd, root)
            self.assertEqual(observed["cwd"], root)


def _binding(
    root: Path,
    *,
    harness: str = "codex",
    profile: str = "interactive",
    fallbacks: tuple[str, ...] = (),
    post_start: tuple = (),
    communications: dict[str, object] | None = None,
):
    executable = Path("/usr/bin/codex" if harness == "codex" else "/usr/bin/claude")
    return SimpleNamespace(
        persona="recovery",
        role="recovery",
        account=pwd.getpwuid(os.geteuid()).pw_name,
        account_home=root,
        mailbox=root / "mail" / "recovery",
        launch_cwd=root,
        selected_harness=harness,
        configured_harness=harness,
        communications=communications,
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


def _telegram_comms() -> dict[str, object]:
    return {
        "kind": "telegram",
        "claude_receiver_topology": "runtime-owned",
        "claude_receiver_unit": "claude-telegram-fallback.service",
        "codex_receiver_unit": "codex-telegram-poller.service",
        "tmux_session": "claude-telegram-bot-fallback",
        "tmux_pane": "0.0",
        "watchdog_units": ("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service"),
    }


def _replace_and_record(binding, *, state_root: Path | None, context=None, on_started=None):
    del context
    if on_started is not None:
        on_started(
            rc._RuntimeStartEvent(
                started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                boundary=rc._utc_now(),
            )
        )
    rc._atomic_json(
        Path(state_root) / binding.persona / "runtime-controller" / "runtime.json",
        rc._runtime_payload(binding, session_id="replacement", started_at=rc._utc_now()),
    )
    return rc.RuntimeHandle(binding.launch, binding.controller.tmux, "replacement", rc._utc_now())


def _mutated_replace_error(message: str = "input readiness failed") -> rc.RuntimeReplacementError:
    original = RuntimeError(message)
    return rc.RuntimeReplacementError(
        message,
        pane_may_be_mutated=True,
        started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
        original=original,
    )


def _started_unverified_state(
    *,
    transition_id: str = "t1",
    correlation_id: str = "corr-retry",
    marker: dict[str, object] | None = None,
    runtime_swapped: bool = False,
    receiver_post_done: bool = False,
    handoff: Path | None = None,
    handoff_sha256: str | None = None,
) -> dict[str, object]:
    state: dict[str, object] = {
        "schema": "pantheon-runtime-transition/v1",
        "transition_id": transition_id,
        "phase": "preparing",
        "trigger": "operator",
        "correlation_id": correlation_id,
        "destination": "codex/fallback",
        "source_runtime": "claude/primary",
        "source_transition_id": "",
        "outgoing_harness": "claude",
        "outgoing_profile": "primary",
        "incoming_harness": "codex",
        "incoming_profile": "fallback",
        "runtime_swapped": runtime_swapped,
        "receiver_pre_done": True,
        "receiver_post_done": receiver_post_done,
        "receiver_transferred": False,
        "successor_read_ack": False,
        "runtime_mutation": {
            "status": "resolved" if runtime_swapped else "started-unverified",
            "pane_may_be_mutated": not runtime_swapped,
            "target": "codex/fallback",
            "scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT,
            "created_scope": False,
        },
    }
    if marker is not None:
        state["started_unverified_recovery"] = marker
    if handoff is not None:
        state["chronicle_evidence"] = str(handoff)
        state["operator_handoff_sha256"] = handoff_sha256 or hashlib.sha256(handoff.read_bytes()).hexdigest()
        state["expected_successor_read_evidence"] = f"hook:runtime-transfer:{transition_id}:{handoff.name}"
        state["successor_read_target"] = str(handoff)
        state["successor_evidence_kind"] = "codex"
    return state


def _started_unverified_marker(root: Path, *, transition_sha: str, recovery_id: str = "rec-1") -> dict[str, object]:
    archive = root / "recovery" / "runtime-controller" / f"operator-started-unverified-recovery-{recovery_id}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "pantheon-runtime-operator-started-unverified-recovery-archive/v1",
        "persona": "recovery",
        "recovery_id": recovery_id,
        "predecessor_sha256": transition_sha,
        "predecessor_transition": _started_unverified_state(),
        "reason": "operator-reviewed",
        "archived_at": "2026-08-01T00:00:00Z",
    }
    archive.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": "pantheon-runtime-operator-started-unverified-recovery/v1",
        "status": "in-progress",
        "recovery_id": recovery_id,
        "reason": "operator-reviewed",
        "correlation_id": "corr-retry",
        "source_runtime": "claude/primary",
        "destination": "codex/fallback",
        "recovered_from_transition_id": "t1",
        "recovered_from_sha256": transition_sha,
        "predecessor_archive": str(archive),
        "predecessor_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def _target_live_compensation_state(
    *,
    transition_id: str = "failed-target-live",
    correlation_id: str = "corr-target-live",
    source: str = "codex/fallback",
    destination: str = "claude/primary",
    marker: dict[str, object] | None = None,
    phase: str = "compensation-failed",
    runtime_swapped: bool = False,
    receiver_post_done: bool = False,
    handoff: Path | None = None,
    handoff_sha256: str | None = None,
) -> dict[str, object]:
    outgoing_harness, outgoing_profile = source.split("/", 1)
    incoming_harness, incoming_profile = destination.split("/", 1)
    state: dict[str, object] = {
        "schema": "pantheon-runtime-transition/v1",
        "transition_id": transition_id,
        "phase": phase,
        "trigger": "operator",
        "correlation_id": correlation_id,
        "destination": destination,
        "source_runtime": source,
        "source_transition_id": "",
        "outgoing_harness": outgoing_harness,
        "outgoing_profile": outgoing_profile,
        "incoming_harness": incoming_harness,
        "incoming_profile": incoming_profile,
        "runtime_swapped": runtime_swapped,
        "receiver_pre_done": True,
        "receiver_post_done": receiver_post_done,
        "receiver_transferred": receiver_post_done,
        "successor_read_ack": False,
        "runtime_mutation": {
            "status": "compensation-failed" if not runtime_swapped else "resolved",
            "pane_may_be_mutated": not runtime_swapped,
            "target": destination,
            "scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT,
            "created_scope": False,
        },
        "compensation_error": "RuntimeError: restore failed",
    }
    if marker is not None:
        state["compensation_failed_target_live_recovery"] = marker
    if handoff is not None:
        state["chronicle_evidence"] = str(handoff)
        state["operator_handoff_sha256"] = handoff_sha256 or hashlib.sha256(handoff.read_bytes()).hexdigest()
        state["expected_successor_read_evidence"] = f"hook:runtime-transfer:{transition_id}:{handoff.name}"
        state["successor_read_target"] = str(handoff)
        state["successor_evidence_kind"] = "runtime-transfer"
    return state


def _prestart_replace_error(message: str = "pre-start failed") -> rc.RuntimeReplacementError:
    original = RuntimeError(message)
    return rc.RuntimeReplacementError(
        message,
        pane_may_be_mutated=False,
        started=None,
        original=original,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _quota_snapshot(
    binding,
    root: Path,
    *,
    used: float = 95.0,
    remaining: float = 5.0,
    freshness: str = "fresh",
    evidence: dict[str, str] | None = None,
) -> runtime_quota.QuotaSnapshot:
    return runtime_quota.QuotaSnapshot(
        profile=f"{binding.launch.harness}/{binding.launch.profile}",
        windows=(
            runtime_quota.QuotaWindow(
                name="weekly",
                used_percent=used,
                remaining_percent=remaining,
                resets_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            ),
        ),
        observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        freshness=freshness,  # type: ignore[arg-type]
        evidence=evidence or {"authoritative_limit": "false", "provider": "claude"},
    )


def _quota_probe_result(
    binding,
    state_root: Path,
    *,
    run_id: str = "20260803T000000Z",
    pong: bool = True,
    cap: str | None = None,
    statusline_sha256: str = "a" * 64,
    status: str = "observed",
    session_id: str | None = None,
) -> ClaudeQuotaProbeResult:
    return ClaudeQuotaProbeResult(
        schema=CLAUDE_QUOTA_PROBE_SCHEMA,
        status=status,  # type: ignore[arg-type]
        run_id=run_id,
        account=binding.account,
        persona=binding.persona,
        session_id=session_id or claude_probe_session_id(
            binding.account, binding.persona, run_id
        ),
        tmux_target=claude_probe_tmux_target(binding.persona, run_id),
        submitted_at="2026-08-03T00:00:00Z",
        completed_at="2026-08-03T00:00:01Z",
        prompt_sha256=hashlib.sha256(
            CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")
        ).hexdigest(),
        statusline_path=str(
            state_root / binding.persona / "quota" / "claude-statusline.json"
        ),
        statusline_sha256=statusline_sha256,
        cap_sentinel=cap,
        pong_observed=pong,
    )


def _fixture_source_provenance(**kwargs) -> rc.RuntimeSourceProvenance:
    source_hashes = kwargs.get("source_hashes") or {
        relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS
    }
    return rc.RuntimeSourceProvenance(
        revision=kwargs.get("revision", "1" * 40),
        source_hashes=source_hashes,
        source_manifest_sha256=(
            kwargs["source_manifest_sha256"]
            if kwargs.get("source_manifest_sha256") is not None
            else rc._source_manifest_hash(source_hashes)
        ),
        runtime_start_boundary_at=kwargs["runtime_start_boundary_at"],
        runtime_start_observed_at=kwargs["runtime_start_observed_at"],
    )


def _provenance_patch():
    return mock.patch.object(
        rc,
        "_controller_source_provenance",
        side_effect=lambda **kwargs: _fixture_source_provenance(**kwargs),
    )


def _provenance_patch_with(
    *,
    revision: str = "1" * 40,
    source_hashes: Mapping[str, str] | None = None,
    source_manifest_sha256: str | None = None,
):
    return mock.patch.object(
        rc,
        "_controller_source_provenance",
        side_effect=lambda **kwargs: _fixture_source_provenance(
            **kwargs,
            revision=revision,
            source_hashes=source_hashes,
            source_manifest_sha256=source_manifest_sha256,
        ),
    )


def _record_handle_runtime_state(binding, handle: rc.RuntimeHandle, state_root: Path) -> rc.RuntimeHandle:
    observed = rc._utc_now()
    provenance = _fixture_source_provenance(
        runtime_start_boundary_at=handle.started_at,
        runtime_start_observed_at=observed,
    )
    rc._atomic_json(
        state_root / binding.persona / "runtime-controller" / "runtime.json",
        rc._runtime_payload(
            binding,
            session_id=handle.session_id,
            started_at=handle.started_at,
            source_provenance=provenance,
        ),
    )
    return handle


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

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        return CommandSpec(
            argv=(str(self.binding.launch.executable),),
            stdin=None,
            cwd=cwd,
        )

    def quota_observer(self, state_dir: Path):
        del state_dir
        return SimpleNamespace(read=lambda: self.quota_snapshot)

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples):
        del harness, profile
        sample_tuple = tuple(samples)
        return SimpleNamespace(
            schema="pantheon-harness-post-start-assessment/v1",
            status="unknown",
            cause=None,
            pattern_id=None,
            baseline_sha256=baseline.sha256,
            capture_count=len(sample_tuple),
            changed_capture_indices=tuple(
                sample.capture_index
                for sample in sample_tuple
                if sample.sha256 != baseline.sha256
            ),
        )


class _RecordingClaudePostStartAdapter(ClaudeHarnessAdapter):
    def __init__(self, binding, events: list[str]) -> None:
        super().__init__(binding.role, claude_home=binding.account_home, launch=binding.launch)
        self.binding = binding
        self.events = events

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        return CommandSpec(argv=(str(self.binding.launch.executable),), stdin=None, cwd=cwd)

    def quota_observer(self, state_dir: Path):
        del state_dir
        return SimpleNamespace(read=lambda: runtime_quota.QuotaSnapshot(
            profile=f"{self.binding.launch.harness}/{self.binding.launch.profile}",
            windows=(),
            observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            freshness="unavailable",
            evidence={"provider": self.binding.launch.harness},
        ))

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples) -> PostStartAssessment:
        assessment = super().assess_post_start_readiness(
            harness=harness,
            profile=profile,
            baseline=baseline,
            samples=samples,
        )
        self.events.append(f"{harness}:{assessment.status}")
        return assessment

    def prepare_interactive_provider_completion(self, **kwargs):
        del kwargs
        return None

    def assess_interactive_provider_completion(self, *, harness, profile, probe, samples):
        del probe, samples
        return SimpleNamespace(
            schema="pantheon-harness-provider-completion/v1",
            harness=harness,
            profile=profile,
            status="not-applicable",
            cause=None,
            pattern_id=None,
            nonce_sha256=None,
            prompt_sha256=None,
            statusline_baseline_sha256=None,
            statusline_observed_sha256=None,
            capture_count=0,
            changed_capture_indices=(),
            transcript_relative_path_sha256=None,
            transcript_start_offset=None,
            transcript_end_offset=None,
            assistant_event_count=0,
        )


class _RecordingAdapter(_Adapter):
    def __init__(self, binding, quota_snapshot=None) -> None:
        super().__init__(binding, quota_snapshot=quota_snapshot)
        self.launch_cwd: Path | None = None

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        self.launch_cwd = cwd
        return super().build_interactive_launch(target, cwd=cwd)


class _OperatorBackend(_ReceiverBackend):
    def __init__(
        self,
        *,
        fail: str | None = None,
        ambiguous: bool = False,
        no_comms: bool = False,
        proofs: dict[tuple[str, str], str] | None = None,
    ):
        super().__init__(fail=fail)
        self.ambiguous = ambiguous
        self.no_comms = no_comms
        self.proofs = proofs or {}

    def prove_receiver_state(self, binding, expected):
        harness = str(getattr(getattr(binding, "launch", None), "harness", getattr(binding, "selected_harness", "")))
        if self.no_comms:
            self.events.append(f"proof:{binding.persona}:{harness}:{expected}:not-applicable")
            return ReceiverStateProof("not-applicable", {"expected": expected})
        if self.ambiguous:
            self.events.append(f"proof:{binding.persona}:{harness}:{expected}:ambiguous")
            return ReceiverStateProof("ambiguous", {"expected": expected})
        state = self.proofs.get((harness, expected), expected)
        self.events.append(f"proof:{binding.persona}:{harness}:{expected}:{state}")
        return ReceiverStateProof(state, {"expected": expected, "persona": binding.persona, "harness": harness})


class _SyntheticAdapter:
    HARNESS_NAME = "synthetic"

    def __init__(self, binding, *, launch=None) -> None:
        self.binding = binding
        self.launch = launch or binding.launch

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        return CommandSpec(argv=(str(self.launch.executable),), stdin=None, cwd=cwd)

    def quota_observer(self, state_dir: Path):
        del state_dir
        return SimpleNamespace(read=lambda: runtime_quota.QuotaSnapshot(
            profile=f"{self.launch.harness}/{self.launch.profile}",
            windows=(),
            observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            freshness="unavailable",
            evidence={"provider": self.launch.harness},
        ))


class _SyntheticIntegration:
    def __init__(self, name: str, *, communications_managed: bool) -> None:
        self.name = name
        self.communications_managed = communications_managed
        self.events: list[str] = []

    def build_adapter(self, binding, *, harness_home: Path):
        del harness_home
        return _SyntheticAdapter(binding)

    def validate_launch_config(self, launch: HarnessLaunchConfig) -> None:
        if launch.harness != self.name:
            raise ValueError("wrong synthetic harness")

    def prove_runtime_readiness(self, binding, handle, *, state_root: Path, context):
        del binding, handle, state_root, context
        return RuntimeReadinessProof(True, {"synthetic": "ready"})

    def successor_evidence_contract(self, persona: str, transition_id: str, evidence: Path):
        return SuccessorEvidenceContract(
            expected=f"hook:runtime-transfer:{transition_id}:{evidence.name}",
            target=str(evidence),
            evidence_kind="synthetic",
        )

    def validate_successor_ack(self, state, evidence: str) -> None:
        if evidence != state.get("expected_successor_read_evidence"):
            raise ValueError("synthetic successor evidence mismatch")

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig):
        if not self.communications_managed:
            return None
        return f"{current.harness}-to-{target.harness}"

    def prove_receiver_state(self, binding, expected: str, *, backend=None):
        del binding, backend
        if not self.communications_managed:
            return ReceiverStateProof("not-applicable", {"expected": expected})
        return ReceiverStateProof(expected, {"expected": expected})

    def run_receiver_handoff(self, binding, direction: str, phase: str, *, backend=None, reason: str):
        del binding, backend, reason
        self.events.append(f"{direction}:{phase}")
        return ReceiverHookReceipt(True, (f"{direction}:{phase}",), {})

    def compensate_receiver_handoff(self, previous, target, direction: str, *, backend=None, reason: str):
        del previous, target, backend, reason
        self.events.append(f"compensate:{direction}")
        return ReceiverHookReceipt(True, (f"compensate:{direction}",), {})


def _operator_manifest(root: Path, *, account: str) -> Path:
    manifest = root / f"operator-{account}.yaml"
    manifest.write_text(
        f"""
manifest_version: 2
roles:
  recovery:
    persona: recovery
    account: {account}
    harness: claude
harness_runtime:
  schema: pantheon-harness-runtime/v1
  controller:
    tmux:
      session: pantheon-{{persona}}
      window: harness
      pane: "0"
    quota:
      weekly_remaining_percent: 5
  harnesses:
    claude:
      default_profile: primary
      profiles:
        primary:
          executable: /usr/bin/claude
          mode: interactive
          meter: subscription
          quota_source: statusline
    codex:
      default_profile: fallback
      profiles:
        fallback:
          executable: /usr/bin/codex
          mode: interactive
          meter: subscription
          quota_source: app-server
  roles:
    recovery:
      fallbacks: [codex/fallback]
      claude:
        profile: primary
      codex:
        profile: fallback
""",
        encoding="utf-8",
    )
    return manifest


def _telegram_operator_manifest(root: Path, *, account: str) -> Path:
    manifest = _operator_manifest(root, account=account)
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        "harness_runtime:\n",
        """
persona_installer:
  codex_telegram_bridge:
    personas:
      recovery:
        account: {account}
        claude_receiver_topology: runtime-owned
        claude_receiver_unit: claude-telegram-fallback.service
        tmux_session: claude-telegram-bot-fallback
        tmux_pane: "0.0"
        codex_receiver_unit: codex-telegram-poller.service
        watchdog_units:
          - recovery-telegram-child-watchdog.timer
          - recovery-telegram-child-watchdog.service
harness_runtime:
""".format(account=account),
        1,
    )
    manifest.write_text(text, encoding="utf-8")
    return manifest


def _synthetic_operator_manifest(root: Path, *, account: str) -> Path:
    manifest = root / f"synthetic-{account}.yaml"
    manifest.write_text(
        f"""
manifest_version: 2
roles:
  recovery:
    persona: recovery
    account: {account}
    harness: codex
harness_runtime:
  schema: pantheon-harness-runtime/v1
  controller:
    tmux:
      session: pantheon-{{persona}}
      window: harness
      pane: "0"
    quota:
      weekly_remaining_percent: 5
  harnesses:
    codex:
      default_profile: primary
      profiles:
        primary:
          executable: /usr/bin/codex
          mode: interactive
          meter: subscription
          quota_source: app-server
    synthetic:
      default_profile: fallback
      profiles:
        fallback:
          executable: /usr/bin/synthetic
          mode: interactive
          meter: subscription
          quota_source: unavailable
  roles:
    recovery:
      fallbacks: [synthetic/fallback]
      codex:
        profile: primary
      synthetic:
        profile: fallback
""",
        encoding="utf-8",
    )
    return manifest


class _ViabilityBackend:
    def __init__(self, events: list[str], *, fail: str | None = None) -> None:
        self.events = events
        self.fail = fail

    def execute(self, action: str, binding) -> dict[str, object]:
        self.events.append(f"{binding.launch.harness}:{action}")
        if action == self.fail:
            raise RuntimeError(f"fail {action}")
        return {"ok": True, "action": action}

    def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
        self.events.append(f"{binding.launch.harness}:proof-{expected}")
        return ReceiverStateProof(expected, {"expected": expected, "harness": binding.launch.harness})


class InitialViabilityTests(unittest.TestCase):
    def _selected_viability_receipt(self, *, persona: str = "recovery", runtime: str = "claude/primary") -> rc.RuntimeViabilityReceipt:
        harness, _, profile = runtime.partition("/")
        binding = _binding(Path("/tmp/runtime-fixture"), harness=harness, profile=profile)
        if persona != "recovery":
            binding = SimpleNamespace(**{**binding.__dict__, "persona": persona})
        attempt = rc.RuntimeViabilityAttempt(
            sequence=1,
            runtime=runtime,
            harness=harness,
            profile=profile,
            status="selected",
            failure_class=None,
            cause="runtime-ready",
            readiness={
                "event": "runtime_started",
                "provider_completion_status": "provider-completed" if harness == "claude" else "not-applicable",
            },
            teardown={},
            receiver={"ok": True, "operations": []},
            cooldown={"opened": "false"},
            source_provenance={
                "revision": "1" * 40,
                "source_manifest_sha256": "2" * 64,
                "runtime_start_boundary_at": "2026-08-15T00:00:00Z",
                "runtime_start_observed_at": "2026-08-15T00:00:01Z",
            },
            precheck_source_provenance={},
        )
        return rc.RuntimeViabilityReceipt(
            schema="pantheon-runtime-initial-viability/v1",
            persona=persona,
            status="selected",
            selected_runtime=runtime,
            selected_harness=harness,
            selected_profile=profile,
            manifest_order=(runtime,),
            attempt_limit=1,
            attempts=(attempt,),
            recorded_at="2026-08-15T00:00:02Z",
        )

    def _bindings(self, root: Path) -> tuple[object, object]:
        primary = _binding(
            root,
            harness="claude",
            profile="primary",
            fallbacks=("codex/fallback",),
            communications=_telegram_comms(),
        )
        fallback = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
        return primary, fallback

    def _selector_patches(self, events: list[str], *, fail_claude_readiness: bool = False):
        def start(binding, adapter, *, replace=False, context=None):
            del adapter, replace, context
            events.append(f"{binding.launch.harness}:start")
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

        def verify_input(binding, started, *, state_root=None, adapter=None, context=None):
            del state_root, adapter, context
            if fail_claude_readiness and binding.launch.harness == "claude":
                raise rc.RuntimeOperationalFailure(
                    "input not ready",
                    failure_class="operational-readiness",
                    cause="runtime_input_not_ready",
                    binding_runtime=f"{binding.launch.harness}/{binding.launch.profile}",
                    runtime_start=started,
                    created_scope=started.created_scope,
                    pane_may_be_mutated=True,
                    readiness_evidence={"event": "runtime_input_not_ready"},
                )

        return (
            mock.patch.object(rc, "pane_exists", return_value=False),
            mock.patch.object(rc, "_start_runtime", side_effect=start),
            mock.patch.object(rc, "_verify_started_runtime"),
            mock.patch.object(rc, "_verify_runtime_input_ready", side_effect=verify_input),
            mock.patch.object(rc, "_run_post_start_commands"),
            mock.patch.object(rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")),
            _provenance_patch(),
        )

    def _run_selector(
        self,
        root: Path,
        primary,
        fallback,
        *,
        events: list[str],
        backend: _ViabilityBackend | None = None,
        adapters: dict[str, _Adapter] | None = None,
        fail_claude_readiness: bool = False,
        now: datetime | None = None,
        attempt_limit: int | None = None,
    ) -> rc.RuntimeViabilitySelection:
        def adapter_for(binding):
            return (adapters or {}).get(binding.launch.harness, _Adapter(binding))

        patches = self._selector_patches(events, fail_claude_readiness=fail_claude_readiness)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            return rc.ensure_viable_runtime(
                (primary, fallback),
                adapter_factory=adapter_for,
                state_root=root,
                backend=backend or _ViabilityBackend(events),
                now=now,
                attempt_limit=attempt_limit,
            )

    def _manifest_candidates_with_post_start(self, root: Path):
        manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
        text = manifest.read_text(encoding="utf-8")
        text = text.replace(
            "fallbacks: [codex/fallback]\n      claude:\n",
            "fallbacks: [codex/fallback]\n      post_start:\n        - command: [\"/bin/true\"]\n      claude:\n",
            1,
        )
        manifest.write_text(text, encoding="utf-8")
        primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
        candidates = persona_bringup.configured_runtime_bindings(manifest, primary)
        self.assertEqual([rc._runtime_ref(candidate) for candidate in candidates], ["claude/primary", "codex/fallback"])
        self.assertTrue(candidates[0].controller.post_start)
        return candidates

    def _run_post_start_cap_selector(self, root: Path, *, mode: str = "input-cap"):
        events: list[str] = []
        candidates = self._manifest_candidates_with_post_start(root)
        cap_text = (REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        ready = "some output\n╭──────────╮\n│ box │\n╰──────────╯\n❯ \n"
        if mode == "input-cap":
            captures = [
                ("claude:input-cap", cap_text),
            ]
        elif mode == "stale-baseline":
            captures = [
                ("claude:input-ready", ready),
                ("claude:provider-baseline", ready),
                ("claude:baseline", cap_text),
                ("claude:sample-1", cap_text),
                ("claude:sample-2", cap_text),
                ("claude:sample-3", cap_text),
            ]
        elif mode == "post-start-cap":
            captures = [
                ("claude:input-ready", ready),
                ("claude:provider-baseline", ready),
                ("claude:baseline", ready),
                ("claude:sample-1", cap_text),
                ("claude:sample-2", cap_text),
                ("claude:sample-3", cap_text),
            ]
        elif mode == "ready":
            captures = [
                ("claude:input-ready", ready),
                ("claude:provider-baseline", ready),
                ("claude:baseline", ready),
                ("claude:sample-1", ready),
                ("claude:sample-2", ready),
                ("claude:sample-3", ready),
            ]
        else:
            raise AssertionError(mode)
        captures.extend(
            [
                ("codex:input-ready", ready),
                ("codex:baseline", ready),
                ("codex:sample-1", ready),
                ("codex:sample-2", ready),
                ("codex:sample-3", ready),
            ]
        )
        capture_iter = iter(captures)

        def capture(_target, *, context=None):
            del context
            label, text = next(capture_iter)
            events.append(label)
            return text

        def start(binding, adapter, *, replace=False, context=None):
            del adapter, replace, context
            events.append(f"{binding.launch.harness}:start")
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

        def post_start_run(argv, **kwargs):
            del kwargs
            events.append("post_start")
            return subprocess.CompletedProcess(argv, 0, "", "")

        original_clear = rc._clear_stale_runtime_state

        def clear(persona, *, state_root=None):
            events.append("clear-state")
            return original_clear(persona, state_root=state_root)

        def adapter_for(binding):
            if binding.launch.harness == "claude":
                return _RecordingClaudePostStartAdapter(binding, events)
            return _Adapter(binding)

        with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
            rc, "_start_runtime", side_effect=start
        ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=capture
        ), mock.patch.object(
            rc.subprocess, "run", side_effect=post_start_run
        ), mock.patch.object(rc.time, "sleep", side_effect=lambda _seconds: events.append("sleep")), mock.patch.object(
            rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")
        ), mock.patch.object(rc, "_clear_stale_runtime_state", side_effect=clear), _provenance_patch():
            selection = rc.ensure_viable_runtime(
                candidates,
                adapter_factory=adapter_for,
                state_root=root,
                backend=_ViabilityBackend(events),
                now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
            )
        return selection, events

    def _completion_manifest_candidates(self, root: Path):
        account = pwd.getpwuid(os.geteuid()).pw_name
        account_home = root / "home" / account
        account_home.mkdir(parents=True, exist_ok=True)
        manifest = _telegram_operator_manifest(root, account=account)
        text = manifest.read_text(encoding="utf-8")
        text = text.replace(
            "fallbacks: [codex/fallback]\n      claude:\n",
            "fallbacks: [codex/fallback]\n      post_start:\n        - command: [\"/bin/true\"]\n      claude:\n",
            1,
        )
        manifest.write_text(text, encoding="utf-8")
        with mock.patch.object(
            persona_bringup.pwd,
            "getpwnam",
            return_value=SimpleNamespace(pw_dir=str(account_home)),
        ):
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            candidates = persona_bringup.configured_runtime_bindings(manifest, primary)
        return candidates, account_home

    def _write_statusline(self, state_root: Path, account_home: Path, transcript: Path, *, observed_at: str = "2099-01-01T00:00:00Z") -> None:
        path = state_root / "recovery" / "runtime-controller" / "quota" / "claude-statusline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "pantheon-claude-statusline-quota/v1",
                    "observed_at": observed_at,
                    "session_id": "session-r18",
                    "transcript_path": str(transcript),
                    "rate_limits": {"ok": True},
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(transcript.is_relative_to(account_home / ".claude" / "projects"))

    def _write_transcript_event(self, transcript: Path, payload: Mapping[str, object]) -> None:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _run_provider_completion_selector(self, root: Path, *, mode: str):
        events: list[str] = []
        captured_prompt = {"marker": ""}
        candidates, account_home = self._completion_manifest_candidates(root)
        transcript = account_home / ".claude" / "projects" / "r18" / "session.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        if mode == "stale":
            marker = "PANTHEON_PROVIDER_COMPLETION_deadbeef"
            self._write_transcript_event(
                transcript,
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                },
            )
            self._write_statusline(root, account_home, transcript, observed_at="2000-01-01T00:00:00Z")
        ready = "some output\n╭──────────╮\n│ box │\n╰──────────╯\n❯ \n"
        cap_text = (REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")

        def maybe_write_success() -> None:
            marker = captured_prompt["marker"]
            if not marker:
                return
            if mode == "success":
                self._write_transcript_event(
                    transcript,
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                    },
                )
            elif mode == "prompt-only":
                self._write_transcript_event(
                    transcript,
                    {"type": "user", "message": {"role": "user", "content": marker}},
                )
            if mode in {"success", "prompt-only"}:
                self._write_statusline(root, account_home, transcript)

        captures: list[tuple[str, str, Callable[[], None] | None]] = [
            ("claude:input-ready", ready, None),
            ("claude:provider-baseline", ready, None),
        ]
        if mode == "cap":
            captures.append(("claude:provider-cap", cap_text, None))
        else:
            captures.append(("claude:provider-sample", ready, maybe_write_success))
        captures.extend(
            [
                ("claude:post-baseline", ready, None),
                ("claude:post-1", ready, None),
                ("claude:post-2", ready, None),
                ("claude:post-3", ready, None),
                ("codex:input-ready", ready, None),
                ("codex:provider-baseline", ready, None),
                ("codex:post-baseline", ready, None),
                ("codex:post-1", ready, None),
                ("codex:post-2", ready, None),
                ("codex:post-3", ready, None),
            ]
        )
        capture_iter = iter(captures)

        def capture(_target, *, context=None):
            del context
            try:
                label, text, before_return = next(capture_iter)
            except StopIteration:
                label, text, before_return = ("extra", ready, None)
            events.append(label)
            if before_return is not None:
                before_return()
            return text

        def start(binding, adapter, *, replace=False, context=None):
            del adapter, replace, context
            events.append(f"{binding.launch.harness}:start")
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

        def run(argv, **kwargs):
            del kwargs
            argv_tuple = tuple(str(item) for item in argv)
            if argv_tuple and argv_tuple[0] == str(rc.SEND_KEYS_HELPER):
                events.append("provider-submit")
                match = re.search(r"PANTHEON_PROVIDER_COMPLETION_[0-9a-f]+", argv_tuple[-1])
                if match:
                    captured_prompt["marker"] = match.group(0)
                return subprocess.CompletedProcess(argv, 0, "", "")
            events.append("post_start")
            return subprocess.CompletedProcess(argv, 0, "", "")

        original_clear = rc._clear_stale_runtime_state

        def clear(persona, *, state_root=None):
            events.append("clear-state")
            return original_clear(persona, state_root=state_root)

        def adapter_for(binding):
            if binding.launch.harness == "claude":
                return ClaudeHarnessAdapter(binding.role, claude_home=binding.account_home, launch=binding.launch)
            return _Adapter(binding)

        with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
            rc, "_start_runtime", side_effect=start
        ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
            rc, "_capture_visible_runtime_pane", side_effect=capture
        ), mock.patch.object(rc.subprocess, "run", side_effect=run), mock.patch.object(
            rc.time, "sleep", side_effect=lambda _seconds: events.append("sleep")
        ), mock.patch.object(rc, "PROVIDER_COMPLETION_CAPTURE_COUNT", 1), mock.patch.object(
            rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")
        ), mock.patch.object(rc, "_clear_stale_runtime_state", side_effect=clear), _provenance_patch():
            selection = rc.ensure_viable_runtime(
                candidates,
                adapter_factory=adapter_for,
                state_root=root,
                backend=_ViabilityBackend(events),
                now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
            )
        return selection, events, captured_prompt["marker"]

    def test_initial_viability_readiness_failure_tears_down_primary_before_manifest_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            selection = self._run_selector(
                root,
                primary,
                fallback,
                events=events,
                fail_claude_readiness=True,
            )
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertLess(events.index("claude:start"), events.index(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"))
        self.assertLess(events.index(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"), events.index("claude:proof-absent"))
        self.assertLess(events.index("claude:proof-absent"), events.index("codex:start"))
        self.assertLess(events.index("codex:proof-absent"), events.index("codex:start-codex-poller"))
        self.assertLess(events.index("codex:start-codex-poller"), events.index("codex:probe-codex-present"))

    def _write_stale_codex_runtime_state(
        self,
        root: Path,
        fallback,
        *,
        source_provenance: rc.RuntimeSourceProvenance | None = None,
    ) -> None:
        path = root / fallback.persona / "runtime-controller" / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if source_provenance is None:
            source_provenance = rc.RuntimeSourceProvenance(
                revision="1" * 40,
                source_hashes={relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS},
                source_manifest_sha256="2" * 64,
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
        rc._atomic_json(
            path,
            rc._runtime_payload(
                fallback,
                session_id=None,
                started_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
                source_provenance=source_provenance,
                provider_completion={"provider_completion_status": "not-applicable"},
            ),
        )

    def _write_stale_selected_codex_viability(self, root: Path, primary, fallback) -> None:
        rc._atomic_json(
            rc._initial_viability_path(primary.persona, root),
            {
                "schema": "pantheon-runtime-initial-viability/v1",
                "persona": primary.persona,
                "status": "selected",
                "selected_runtime": rc._runtime_ref(fallback),
                "selected_harness": fallback.launch.harness,
                "selected_profile": fallback.launch.profile,
                "manifest_order": [rc._runtime_ref(primary), rc._runtime_ref(fallback)],
                "attempt_limit": 2,
                "attempts": [
                    {
                        "sequence": 1,
                        "runtime": rc._runtime_ref(fallback),
                        "harness": fallback.launch.harness,
                        "profile": fallback.launch.profile,
                        "status": "selected",
                        "failure_class": None,
                        "cause": "runtime-ready",
                        "readiness": {"event": "runtime_started", "provider_completion_status": "not-applicable"},
                        "teardown": {},
                        "receiver": {
                            "ok": True,
                            "operations": [
                                "probe-claude-absent",
                                "start-codex-poller",
                                "probe-codex-present",
                                "health-codex-no-409",
                            ],
                        },
                        "cooldown": {"opened": "false"},
                        "source_provenance": {
                            "revision": "1" * 40,
                            "source_manifest_sha256": "2" * 64,
                            "runtime_start_boundary_at": "2026-08-14T00:00:00Z",
                            "runtime_start_observed_at": "2026-08-14T00:00:01Z",
                        },
                        "precheck_source_provenance": {},
                    }
                ],
                "recorded_at": "2026-08-14T00:00:02Z",
            },
        )

    def _reuse_patches(self, events: list[str]):
        return (
            mock.patch.object(rc, "pane_exists", return_value=True),
            mock.patch.object(rc, "_default_tmux_server_pid", return_value=123),
            mock.patch.object(rc, "_pid_in_scope", return_value=True),
            mock.patch.object(rc, "_start_runtime"),
        )

    def test_recorded_runtime_reuse_accepts_hash_equivalent_ticket_only_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = _fixture_source_provenance(
                revision="1" * 40,
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                revision="2" * 40,
                source_hashes=recorded.source_hashes,
            ):
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
        start_runtime.assert_not_called()
        self.assertEqual(reused.receipt.status, "reused-recorded")
        self.assertEqual(reused.binding.launch.harness, "codex")
        attempt = reused.receipt.attempts[0]
        self.assertEqual(attempt.cause, "recorded-runtime-source-equivalent")
        self.assertEqual(attempt.readiness["source_equivalence_mode"], "ticket-only-revision")
        self.assertEqual(attempt.source_provenance["revision"], "1" * 40)
        self.assertEqual(attempt.precheck_source_provenance["revision"], "2" * 40)
        self.assertEqual(events, ["codex:proof-present"])

    def test_recorded_runtime_reuse_rejects_hash_mismatch_without_candidate_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = _fixture_source_provenance(
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            current_hashes = dict(recorded.source_hashes)
            first_key = rc.RUNTIME_SOURCE_HASH_PATHS[0]
            current_hashes[first_key] = "b" * 64
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            before = runtime_path.read_text(encoding="utf-8")
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=current_hashes,
            ):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
            after = runtime_path.read_text(encoding="utf-8")
        start_runtime.assert_not_called()
        self.assertEqual(after, before)
        self.assertEqual(events, ["codex:proof-present"])
        self.assertEqual(caught.exception.reason, "recorded-source-hash-mismatch")
        self.assertEqual(caught.exception.evidence["selected_runtime"], "codex/fallback")

    def test_live_runtime_mismatch_is_typed_and_does_not_clear_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = _fixture_source_provenance(
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            state = json.loads(runtime_path.read_text(encoding="utf-8"))
            state["controller_source_hashes"].pop(rc.RUNTIME_SOURCE_HASH_PATHS[-1])
            runtime_path.write_text(json.dumps(state), encoding="utf-8")
            before = runtime_path.read_text(encoding="utf-8")
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
            after = runtime_path.read_text(encoding="utf-8")
        start_runtime.assert_not_called()
        self.assertEqual(after, before)
        self.assertEqual(caught.exception.reason, "recorded-source-key-mismatch")

    def test_live_malformed_initial_viability_state_blocks_without_candidate_or_state_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            rc._atomic_json(
                rc._initial_viability_path(primary.persona, root),
                {"schema": "pantheon-runtime-initial-viability/v1", "status": "malformed"},
            )
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime:
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
            evidence_path = rc._evidence_log_path(primary.persona, root)
            evidence = [
                json.loads(line)
                for line in evidence_path.read_text(encoding="utf-8").splitlines()
            ]
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "initial-viability-state-not-selected")
        self.assertTrue(any(item["event"] == "runtime_viability_blocked" for item in evidence))
        self.assertEqual(evidence[-1]["schema"], "pantheon-runtime-controller-evidence/v1")
        self.assertEqual(evidence[-1]["block_reason"], "initial-viability-state-not-selected")
        self.assertEqual(evidence[-1]["block"]["schema"], "pantheon-runtime-viability-block/v1")

    def test_runtime_viability_block_evidence_write_failure_stays_typed_without_candidate_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            rc._atomic_json(
                rc._initial_viability_path(primary.persona, root),
                {"schema": "pantheon-runtime-initial-viability/v1", "status": "malformed"},
            )
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, mock.patch.object(
                rc, "_append_controller_evidence", side_effect=OSError("disk full")
            ):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "initial-viability-state-not-selected")
        self.assertEqual(caught.exception.evidence["evidence_log_status"], "failed")
        self.assertIn("OSError", caught.exception.evidence["evidence_log_error"])

    def test_consume_contains_runtime_viability_block_without_onfailure_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(
                root,
                account=pwd.getpwuid(os.geteuid()).pw_name,
            )
            state_dir = root / "state"
            mailbox_hub = root / "mail"
            report = DiagnosticReport(
                harness="codex",
                execution_context="test",
                observations=(),
            )
            block = rc.RuntimeViabilityBlocked(
                "recorded-source-hash-mismatch",
                {
                    "schema": "pantheon-runtime-viability-block/v1",
                    "persona": "recovery",
                    "selected_runtime": "codex/fallback",
                    "reason": "recorded-source-hash-mismatch",
                    "evidence_log_status": "failed",
                },
            )
            with mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), mock.patch.object(
                persona_bringup, "wake_persistent_runtime", side_effect=block
            ), mock.patch.object(persona_bringup, "_print_json") as printed:
                code = persona_bringup.main(
                    [
                        "consume",
                        "--persona",
                        "recovery",
                        "--manifest",
                        str(manifest),
                        "--mailbox-hub",
                        str(mailbox_hub),
                        "--state-dir",
                        str(state_dir),
                    ]
                )
        self.assertEqual(code, 5)
        self.assertFalse((state_dir / "recovery" / "soft-streak.json").exists())
        payload = printed.call_args.args[0]
        self.assertEqual(payload["consume"]["skipped"], "runtime_viability_blocked")
        self.assertEqual(payload["consume"]["block"]["reason"], "recorded-source-hash-mismatch")

    def test_initial_viability_full_rollback_stale_runtime_json_does_not_reorder_before_manifest_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            selection = self._run_selector(
                root,
                primary,
                fallback,
                events=events,
                fail_claude_readiness=True,
            )
            state = rc.initial_viability_state(primary.persona, state_root=root)
            check = rc.validate_initial_viability_state(
                primary.persona,
                state_root=root,
                expect_selected=rc._runtime_ref(fallback),
                expect_failed=rc._runtime_ref(primary),
                expect_failure_classes=("subscription-cap", "operational-launch", "operational-readiness"),
                require_source_provenance=True,
                require_receiver_receipt=True,
            )
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual([attempt["runtime"] for attempt in state["attempts"]], ["claude/primary", "codex/fallback"])
        self.assertEqual(state["attempts"][0]["failure_class"], "operational-readiness")
        self.assertTrue(check["ok"], check)
        self.assertLess(events.index("claude:start"), events.index("codex:start"))

    def test_initial_viability_reuses_recorded_runtime_only_when_scope_pane_receiver_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            fresh = self._run_selector(root, primary, fallback, events=events, fail_claude_readiness=True)
            self.assertEqual(fresh.binding.launch.harness, "codex")
            before_latest = (root / primary.persona / "runtime-controller" / "initial-viability.json").read_text(
                encoding="utf-8"
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_start_runtime"
            ) as start, mock.patch.object(rc, "_run_post_start_commands") as post_start, _provenance_patch():
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            start.assert_not_called()
            post_start.assert_not_called()
            self.assertEqual(reused.receipt.status, "reused-recorded")
            self.assertEqual(reused.binding.launch.harness, "codex")
            self.assertEqual(
                (root / primary.persona / "runtime-controller" / "initial-viability.json").read_text(
                    encoding="utf-8"
                ),
                before_latest,
            )

        falsifiers = (
            ("pane-absent", {"pane": False, "scope": True}, None),
            ("scope-absent", {"pane": True, "scope": False}, None),
            ("receiver-absent", {"pane": True, "scope": True}, _ViabilityBackend([])),
        )
        for name, liveness, backend in falsifiers:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                events: list[str] = []
                primary, fallback = self._bindings(root)
                self._write_stale_codex_runtime_state(root, fallback)
                self._write_stale_selected_codex_viability(root, primary, fallback)
                if name == "receiver-absent":
                    class AbsentBackend(_ViabilityBackend):
                        def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
                            self.events.append(f"{binding.launch.harness}:proof-{expected}")
                            if binding.launch.harness == "codex" and expected == "present":
                                return ReceiverStateProof("absent", {"expected": expected})
                            return ReceiverStateProof(expected, {"expected": expected})

                    backend = AbsentBackend(events)
                else:
                    backend = _ViabilityBackend(events)
                with mock.patch.object(rc, "pane_exists", return_value=liveness["pane"]), mock.patch.object(
                    rc, "_default_tmux_server_pid", return_value=123
                ), mock.patch.object(rc, "_pid_in_scope", return_value=liveness["scope"]), _provenance_patch():
                    with self.assertRaises(rc.RuntimeViabilityBlocked):
                        self._run_selector(
                            root,
                            primary,
                            fallback,
                            events=events,
                            backend=backend,
                            fail_claude_readiness=True,
                        )
                self.assertNotIn("claude:start", events)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)

            class AmbiguousBackend(_ViabilityBackend):
                def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
                    self.events.append(f"{binding.launch.harness}:proof-{expected}")
                    if binding.launch.harness == "codex" and expected == "present":
                        return ReceiverStateProof("duplicate", {"expected": expected})
                    return ReceiverStateProof(expected, {"expected": expected})

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), _provenance_patch():
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=AmbiguousBackend(events),
                    )
            self.assertEqual(caught.exception.reason, "recorded-receiver-unknown")
            self.assertIn("receiver state is duplicate", caught.exception.evidence["receiver_error"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            viability_path = rc._initial_viability_path(primary.persona, root)
            manual_state = json.loads(viability_path.read_text(encoding="utf-8"))
            manual_state["selected_runtime"] = "manual/out-of-manifest"
            manual_state["selected_harness"] = "manual"
            manual_state["selected_profile"] = "out-of-manifest"
            viability_path.write_text(json.dumps(manual_state), encoding="utf-8")
            selection = self._run_selector(
                root,
                primary,
                fallback,
                events=events,
                fail_claude_readiness=True,
            )
            self.assertEqual(selection.binding.launch.harness, "codex")
            self.assertLess(events.index("claude:start"), events.index("codex:start"))

    def test_initial_viability_gate_rejects_codex_selected_only_without_failed_primary_or_authenticated_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            strict = rc.validate_initial_viability_state(
                primary.persona,
                state_root=root,
                expect_selected=rc._runtime_ref(fallback),
                expect_failed=rc._runtime_ref(primary),
                expect_failure_classes=("subscription-cap", "operational-launch", "operational-readiness"),
                require_source_provenance=True,
                require_receiver_receipt=True,
            )
            loose = rc.validate_initial_viability_state(
                primary.persona,
                state_root=root,
                expect_selected=rc._runtime_ref(fallback),
                require_source_provenance=True,
                require_receiver_receipt=True,
            )
        self.assertFalse(strict["ok"], strict)
        self.assertIn("failed runtime 'claude/primary' not recorded", strict["errors"])
        self.assertTrue(loose["ok"], loose)

    def test_persona_cutover_step15_rejects_live_selected_only_without_false_claude_failed_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            rc_value = rc.main(
                [
                    "initial-viability",
                    "--persona",
                    primary.persona,
                    "--state-root",
                    str(root),
                    "--expect-selected",
                    rc._runtime_ref(fallback),
                    "--expect-failed",
                    rc._runtime_ref(primary),
                    "--expect-failure-class",
                    "subscription-cap",
                    "--expect-failure-class",
                    "operational-launch",
                    "--expect-failure-class",
                    "operational-readiness",
                    "--require-source-provenance",
                    "--require-receiver-receipt",
                ]
            )
        self.assertEqual(rc_value, 1)

    def test_command_sheet_step15_preserves_authenticated_selection_route_contract(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_cutover.sh").read_text(encoding="utf-8")
        codex_branch = source.index('if [[ "$SELECTED_HARNESS" == "codex" ]]')
        viability = source.index("python3 -m tools.runtime_controller initial-viability", codex_branch)
        self.assertIn('--expect-selected "codex/interactive-chatgpt"', source[viability:])
        self.assertIn('--expect-failed "claude/interactive-subscription"', source[viability:])
        for failure_class in ("subscription-cap", "operational-launch", "operational-readiness"):
            self.assertIn(f"--expect-failure-class {failure_class}", source[viability:])
        self.assertIn("--require-source-provenance", source[viability:])
        self.assertIn("--require-receiver-receipt", source[viability:])
        self.assertNotIn("--accept-selected-only", source[viability:])
        self.assertNotIn("--allow-reused-recorded", source[viability:])

    def test_orchestrator_rollback_fresh_cutover_attempts_manifest_primary_then_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = pwd.getpwuid(os.geteuid()).pw_name
            manifest = _telegram_operator_manifest(root, account=account)
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            candidates = persona_bringup.configured_runtime_bindings(manifest, primary)
            self.assertEqual([rc._runtime_ref(candidate) for candidate in candidates], ["claude/primary", "codex/fallback"])
            events: list[str] = []
            self._write_stale_codex_runtime_state(root, candidates[1])
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime",
                side_effect=lambda binding, adapter, **kwargs: (
                    events.append(f"{binding.launch.harness}:start")
                    or rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
                ),
            ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc,
                "_verify_runtime_input_ready",
                side_effect=lambda binding, started, **kwargs: (
                    (_ for _ in ()).throw(
                        rc.RuntimeOperationalFailure(
                            "input not ready",
                            failure_class="operational-readiness",
                            cause="runtime_input_not_ready",
                            binding_runtime=rc._runtime_ref(binding),
                            runtime_start=started,
                            created_scope=started.created_scope,
                            pane_may_be_mutated=True,
                            readiness_evidence={"event": "runtime_input_not_ready"},
                        )
                    )
                    if binding.launch.harness == "claude"
                    else None
                ),
            ), mock.patch.object(rc, "_run_post_start_commands"), mock.patch.object(
                rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")
            ), _provenance_patch():
                selection = rc.ensure_viable_runtime(
                    candidates,
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                    now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                )
            state = rc.initial_viability_state("recovery", state_root=root)
            check = rc.validate_initial_viability_state(
                "recovery",
                state_root=root,
                expect_selected="codex/fallback",
                expect_failed="claude/primary",
                expect_failure_classes=("subscription-cap", "operational-launch", "operational-readiness"),
                require_source_provenance=True,
                require_receiver_receipt=True,
            )
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual([attempt["runtime"] for attempt in state["attempts"]], ["claude/primary", "codex/fallback"])
        self.assertLess(events.index("claude:start"), events.index("codex:start"))
        self.assertTrue(check["ok"], check)

    def test_initial_viability_falls_through_from_claude_post_start_cap_to_codex_with_teardown(self) -> None:
        cap_text = (REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        self.assertTrue(rc.ready_for_submission(cap_text))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events = self._run_post_start_cap_selector(root, mode="input-cap")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(state["attempts"][0]["failure_class"], "subscription-cap")
        self.assertEqual(state["attempts"][0]["cause"], "post-start-subscription-cap")
        self.assertEqual(state["attempts"][1]["runtime"], "codex/fallback")
        self.assertLess(events.index("claude:input-cap"), events.index("claude:subscription-cap"))
        self.assertNotIn("claude:baseline", events)
        self.assertNotIn("post_start", events[: events.index("claude:subscription-cap")])
        self.assertLess(events.index("claude:subscription-cap"), events.index(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"))
        self.assertLess(events.index(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"), events.index("clear-state"))
        self.assertLess(events.index("clear-state"), events.index("claude:proof-absent"))
        self.assertLess(events.index("claude:proof-absent"), events.index("codex:start"))
        self.assertLess(events.index("codex:start"), events.index("codex:start-codex-poller"))

    def test_initial_viability_uses_fresh_post_start_pane_evidence_not_old_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_selection, stale_events = self._run_post_start_cap_selector(root, mode="stale-baseline")
            stale_state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(stale_selection.binding.launch.harness, "claude")
        self.assertEqual(stale_state["selected_runtime"], "claude/primary")
        self.assertNotIn("claude:subscription-cap", stale_events)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh_selection, fresh_events = self._run_post_start_cap_selector(root, mode="post-start-cap")
            fresh_state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(fresh_selection.binding.launch.harness, "codex")
        self.assertEqual(fresh_state["attempts"][0]["cause"], "post-start-subscription-cap")
        self.assertIn("claude:subscription-cap", fresh_events)
        self.assertLess(fresh_events.index("claude:baseline"), fresh_events.index("post_start"))
        self.assertLess(fresh_events.index("post_start"), fresh_events.index("claude:subscription-cap"))

    def test_initial_viability_cap_fallthrough_preserves_single_receiver_and_attempt_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _selection, events = self._run_post_start_cap_selector(root, mode="input-cap")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(state["attempt_limit"], 2)
        self.assertEqual([attempt["runtime"] for attempt in state["attempts"]], ["claude/primary", "codex/fallback"])
        self.assertEqual(events.count("codex:start-codex-poller"), 1)
        self.assertEqual(events.count("codex:probe-codex-present"), 1)
        self.assertLess(events.index("claude:proof-absent"), events.index("codex:start-codex-poller"))
        payload = json.dumps(state, sort_keys=True)
        self.assertNotIn("You've hit your", payload)
        self.assertNotIn("What do you want to do?", payload)
        self.assertIn("claude-rate-limit-options-modal", payload)

    def test_initial_viability_requires_provider_completion_not_transport_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, marker = self._run_provider_completion_selector(root, mode="none")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(state["attempts"][0]["failure_class"], "operational-readiness")
        self.assertEqual(state["attempts"][0]["cause"], "provider-completion-not-proven")
        self.assertIn("provider-submit", events)
        self.assertLess(events.index("provider-submit"), events.index("stop:pantheon-runtime-tmux.scope"))
        self.assertLess(events.index("clear-state"), events.index("claude:proof-absent"))
        self.assertLess(events.index("claude:proof-absent"), events.index("codex:start"))
        self.assertEqual(state["attempts"][1]["readiness"]["provider_completion_status"], "not-applicable")
        payload = json.dumps(state, sort_keys=True)
        self.assertNotIn(marker, payload)
        self.assertNotIn("Provider completion probe", payload)
        self.assertNotIn("Claude ready", payload)

    def test_initial_viability_delayed_claude_cap_after_prompt_falls_through_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, _marker = self._run_provider_completion_selector(root, mode="cap")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(state["attempts"][0]["failure_class"], "subscription-cap")
        self.assertEqual(state["attempts"][0]["cause"], "provider-completion-subscription-cap")
        self.assertLess(events.index("provider-submit"), events.index("claude:provider-cap"))
        self.assertLess(events.index("claude:provider-cap"), events.index("stop:pantheon-runtime-tmux.scope"))
        self.assertLess(events.index("claude:proof-absent"), events.index("codex:start-codex-poller"))

    def test_initial_viability_provider_completion_nonce_selects_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, marker = self._run_provider_completion_selector(root, mode="success")
            state = rc.initial_viability_state("recovery", state_root=root)
            runtime = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
        self.assertEqual(selection.binding.launch.harness, "claude")
        self.assertEqual(runtime["provider_completion"]["provider_completion_status"], "provider-completed")
        self.assertEqual(state["attempts"][0]["readiness"]["provider_completion_status"], "provider-completed")
        self.assertLess(events.index("provider-submit"), events.index("post_start"))
        payload = json.dumps({"state": state, "runtime": runtime}, sort_keys=True)
        self.assertNotIn(marker, payload)
        self.assertNotIn("Provider completion probe", payload)

    def test_initial_viability_prompt_only_no_completion_is_operational_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, _events, marker = self._run_provider_completion_selector(root, mode="prompt-only")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(state["attempts"][0]["failure_class"], "operational-readiness")
        self.assertEqual(state["attempts"][0]["cause"], "provider-completion-not-proven")
        self.assertNotIn(marker, json.dumps(state, sort_keys=True))

    def test_initial_viability_rejects_stale_completion_or_old_transcript_marker(self) -> None:
        stale_uuid = SimpleNamespace(hex="deadbeef")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rc.uuid, "uuid4", return_value=stale_uuid):
            root = Path(tmp)
            selection, _events, marker = self._run_provider_completion_selector(root, mode="stale")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(marker, "PANTHEON_PROVIDER_COMPLETION_deadbeef")
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(state["attempts"][0]["failure_class"], "operational-readiness")
        self.assertNotIn(marker, json.dumps(state, sort_keys=True))

    def test_initial_viability_probe_has_no_mailbox_ack_or_telegram_receiver_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _selection, events, _marker = self._run_provider_completion_selector(root, mode="success")
        self.assertIn("provider-submit", events)
        self.assertNotIn("ack", " ".join(events))
        self.assertNotIn("telegram", " ".join(events))
        self.assertNotIn("codex:start-codex-poller", events)

    def test_initial_viability_activation_receipt_survives_later_exhausted_overwrite(self) -> None:
        invocation_id = "c" * 32
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"INVOCATION_ID": invocation_id},
            clear=False,
        ):
            root = Path(tmp)
            selection, _events, _marker = self._run_provider_completion_selector(root, mode="success")
            self.assertEqual(selection.binding.launch.harness, "claude")
            latest = root / "recovery" / "runtime-controller" / "initial-viability.json"
            exhausted = {
                "schema": "pantheon-runtime-initial-viability/v1",
                "persona": "recovery",
                "status": "exhausted",
                "selected_runtime": None,
                "selected_harness": None,
                "selected_profile": None,
                "manifest_order": ["claude/primary", "codex/fallback"],
                "attempt_limit": 2,
                "attempts": [],
                "recorded_at": "2026-08-15T00:01:00Z",
            }
            latest.write_text(json.dumps(exhausted, sort_keys=True), encoding="utf-8")
            check = rc.validate_initial_viability_invocation_receipt(
                "recovery",
                invocation_id=invocation_id,
                state_root=root,
                allowed_runtime_refs=("claude/primary", "codex/fallback"),
            )
        self.assertEqual(check["selected_runtime"], "claude/primary")
        self.assertTrue(check["invocation_receipt_sha256"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, _marker = self._run_provider_completion_selector(root, mode="success")
            latest = root / "recovery" / "runtime-controller" / "initial-viability.json"
            before_latest = latest.read_text(encoding="utf-8")
            candidates, _account_home = self._completion_manifest_candidates(root)
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_start_runtime"
            ) as start, mock.patch.object(rc, "_run_post_start_commands") as post_start, _provenance_patch():
                reused = rc.ensure_viable_runtime(
                    candidates,
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            start.assert_not_called()
            post_start.assert_not_called()
            self.assertEqual(reused.receipt.status, "reused-recorded")
            self.assertEqual(reused.binding.launch.harness, selection.binding.launch.harness)
            self.assertEqual(latest.read_text(encoding="utf-8"), before_latest)

    def test_invocation_receipt_publish_rejects_symlinked_persona_intermediate_without_outside_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            outside = root / "outside"
            state_root.mkdir()
            outside.mkdir()
            (state_root / "recovery").symlink_to(outside, target_is_directory=True)
            receipt = self._selected_viability_receipt()
            with self.assertRaisesRegex(RuntimeError, "symlink|non-directory"):
                rc._publish_initial_viability_invocation_receipt(
                    receipt,
                    invocation_id="d" * 32,
                    state_root=state_root,
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_invocation_receipt_publish_and_validate_reject_symlinked_state_root_without_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_state = root / "real-state"
            state_link = root / "state-link"
            real_state.mkdir()
            state_link.symlink_to(real_state, target_is_directory=True)
            receipt = self._selected_viability_receipt()
            with self.assertRaisesRegex(RuntimeError, "symlink|non-directory"):
                rc._publish_initial_viability_invocation_receipt(
                    receipt,
                    invocation_id="e" * 32,
                    state_root=state_link,
                )
            with self.assertRaisesRegex(RuntimeError, "symlink|non-directory"):
                rc.validate_initial_viability_invocation_receipt(
                    "recovery",
                    invocation_id="e" * 32,
                    state_root=state_link,
                    allowed_runtime_refs=("claude/primary",),
                )
            self.assertEqual(list(real_state.iterdir()), [])

    def test_invocation_receipt_publish_creates_absent_tree_securely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            receipt = self._selected_viability_receipt()
            result = rc._publish_initial_viability_invocation_receipt(
                receipt,
                invocation_id="f" * 32,
                state_root=state_root,
            )
            self.assertEqual(result["invocation_receipt_status"], "created")
            check = rc.validate_initial_viability_invocation_receipt(
                "recovery",
                invocation_id="f" * 32,
                state_root=state_root,
                allowed_runtime_refs=("claude/primary",),
            )
            self.assertEqual(check["selected_runtime"], "claude/primary")

    def test_invocation_receipt_publish_existing_correct_tree_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            receipt = self._selected_viability_receipt()
            first = rc._publish_initial_viability_invocation_receipt(
                receipt,
                invocation_id="a" * 32,
                state_root=state_root,
            )
            second = rc._publish_initial_viability_invocation_receipt(
                receipt,
                invocation_id="a" * 32,
                state_root=state_root,
            )
            self.assertEqual(first["invocation_receipt_status"], "created")
            self.assertEqual(second["invocation_receipt_status"], "existing-identical")
            self.assertEqual(first["invocation_receipt_sha256"], second["invocation_receipt_sha256"])

    def test_codex_interactive_completion_default_is_explicit_not_synthetic_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex", profile="fallback")
            adapter = _Adapter(binding)
            events: list[str] = []

            def start(binding, adapter, *, replace=False, context=None):
                del adapter, replace, context
                events.append(f"{binding.launch.harness}:start")
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", side_effect=start
            ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_capture_visible_runtime_pane", return_value="codex output\n╭──────────╮\n│ box │\n╰──────────╯\n› \n"
            ), mock.patch.object(rc, "PROVIDER_COMPLETION_CAPTURE_COUNT", 1), _provenance_patch():
                handle = rc.ensure_runtime(binding, adapter, state_root=root)
            runtime = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
        self.assertEqual(handle.profile.harness, "codex")
        self.assertEqual(runtime["provider_completion"]["provider_completion_status"], "not-applicable")

    def test_initial_viability_records_failure_class_selected_runtime_and_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._run_selector(root, primary, fallback, events=[], fail_claude_readiness=True)
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(state["selected_runtime"], "codex/fallback")
        self.assertEqual(state["selected_harness"], "codex")
        failed = state["attempts"][0]
        selected = state["attempts"][1]
        self.assertEqual(failed["failure_class"], "operational-readiness")
        self.assertEqual(failed["source_provenance"]["revision"], "1" * 40)
        self.assertEqual(selected["source_provenance"]["revision"], "1" * 40)

    def test_initial_viability_uses_manifest_order_rejects_unconfigured_and_bounds_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            with self.assertRaisesRegex(ValueError, "not configured"):
                persona_bringup.resolve_configured_runtime_target(
                    _operator_manifest(root, account=primary.account),
                    persona="recovery",
                    destination="codex/not-declared",
                    state_root=root,
                    mailbox_hub=root / "mail",
                )
            with self.assertRaises(rc.RuntimeViabilityExhausted):
                self._run_selector(
                    root,
                    primary,
                    fallback,
                    events=[],
                    fail_claude_readiness=True,
                    attempt_limit=1,
                )
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(state["attempt_limit"], 1)
        self.assertEqual([item["runtime"] for item in state["attempts"]], ["claude/primary"])
        self.assertEqual(state["status"], "exhausted")

    def test_initial_viability_cooldown_prevents_failed_primary_flap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            rc._atomic_json(
                rc._initial_viability_path("recovery", root),
                {
                    "attempts": [
                        {
                            "runtime": "claude/primary",
                            "cooldown": {"opened": "true", "until": "2026-08-14T00:15:00Z"},
                        }
                    ]
                },
            )
            events: list[str] = []
            selection = self._run_selector(
                root,
                primary,
                fallback,
                events=events,
                now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertNotIn("claude:start", events)
        self.assertIn("codex:start", events)

    def test_initial_viability_partial_codex_receiver_failure_compensates_before_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            adapters = {
                "claude": _Adapter(primary, _quota_snapshot(primary, root, evidence={"authoritative_limit": "true"})),
                "codex": _Adapter(fallback),
            }
            with self.assertRaises(rc.RuntimeViabilityExhausted):
                self._run_selector(
                    root,
                    primary,
                    fallback,
                    events=events,
                    backend=_ViabilityBackend(events, fail="start-codex-poller"),
                    adapters=adapters,
                )
            state = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
        self.assertEqual(state, {})
        self.assertLess(events.index("codex:stop-codex-poller"), events.index("codex:probe-codex-absent"))

    def test_manual_operator_swap_remains_independent_from_automatic_viability_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            rc._atomic_json(
                rc._initial_viability_path("recovery", root),
                {"attempts": [{"runtime": "claude/primary", "cooldown": {"opened": "true", "until": "2099-01-01T00:00:00Z"}}]},
            )
            current_handle = rc.RuntimeHandle(primary.launch, primary.controller.tmux, "current", rc._utc_now())
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        primary,
                        fallback,
                        trigger="operator",
                        correlation_id="manual-independent",
                        state_root=root,
                        backend=backend,
                        current_handle=current_handle,
                    )
            transition = rc.transition_state("recovery", state_root=root)
        self.assertEqual(transition["destination"], "codex/fallback")
        self.assertEqual(transition["trigger"], "operator")

    def test_initial_viability_authoritative_cap_skips_primary_without_synthetic_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            adapters = {
                "claude": _Adapter(primary, _quota_snapshot(primary, root, evidence={"authoritative_limit": "true", "provider": "claude"})),
                "codex": _Adapter(fallback),
            }
            with mock.patch.object(rc, "forced_swap", side_effect=AssertionError("forced swap must not run")):
                selection = self._run_selector(root, primary, fallback, events=events, adapters=adapters)
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertNotIn("claude:start", events)
        self.assertEqual(state["attempts"][0]["failure_class"], "subscription-cap")
        self.assertEqual(state["attempts"][0]["cause"], "authoritative-cap")

    def test_initial_viability_gate_accepts_manifest_operational_failure_set_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._atomic_json(
                rc._initial_viability_path("recovery", root),
                {
                    "schema": "pantheon-runtime-initial-viability/v1",
                    "persona": "recovery",
                    "status": "selected",
                    "selected_runtime": "codex/fallback",
                    "selected_harness": "codex",
                    "selected_profile": "fallback",
                    "attempts": [
                        {
                            "runtime": "claude/primary",
                            "status": "failed",
                            "failure_class": "subscription-cap",
                        },
                        {
                            "runtime": "codex/fallback",
                            "status": "selected",
                            "readiness": {"event": "runtime_started"},
                            "receiver": {
                                "ok": True,
                                "operations": [
                                    "probe-claude-absent",
                                    "start-codex-poller",
                                    "probe-codex-present",
                                    "health-codex-no-409",
                                ],
                            },
                            "source_provenance": {
                                "revision": "1" * 40,
                                "source_manifest_sha256": "2" * 64,
                                "runtime_start_boundary_at": "2026-08-14T00:00:00Z",
                                "runtime_start_observed_at": "2026-08-14T00:00:01Z",
                            },
                        },
                    ],
                },
            )
            ok = rc.validate_initial_viability_state(
                "recovery",
                state_root=root,
                expect_selected="codex/fallback",
                expect_failed="claude/primary",
                expect_failure_classes=("subscription-cap", "operational-launch", "operational-readiness"),
                require_source_provenance=True,
                require_receiver_receipt=True,
            )
            bad = rc.validate_initial_viability_state(
                "recovery",
                state_root=root,
                expect_selected="codex/fallback",
                expect_failed="claude/primary",
                expect_failure_classes=("operational-readiness",),
            )
            unknown = rc.validate_initial_viability_state(
                "recovery",
                state_root=root,
                expect_failed="claude/primary",
                expect_failure_classes=("structural",),
            )
        self.assertTrue(ok["ok"], ok)
        self.assertFalse(bad["ok"])
        self.assertFalse(unknown["ok"])

    def test_initial_viability_structural_errors_do_not_fallback_or_compensate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            with mock.patch.object(
                rc, "_controller_source_provenance", side_effect=RuntimeError("dirty source")
            ), mock.patch.object(rc, "_start_runtime") as start:
                with self.assertRaisesRegex(RuntimeError, "dirty source"):
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
            start.assert_not_called()
            self.assertEqual(events, [])

    def test_initial_viability_selected_provenance_uses_runtime_state_for_fresh_and_reuse(self) -> None:
        def matching_source() -> rc.RuntimeSourceProvenance:
            return _fixture_source_provenance(
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )

        def run_with_liveness(root: Path, primary, fallback, events: list[str]) -> rc.RuntimeViabilitySelection:
            pane_calls = 0

            def pane_exists(_target, *, context=None):
                nonlocal pane_calls
                del context
                pane_calls += 1
                return pane_calls == 1

            def start(binding, adapter, *, replace=False, context=None):
                del adapter, replace, context
                events.append(f"{binding.launch.harness}:start")
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

            def verify_input(binding, started, *, state_root=None, adapter=None, context=None):
                del state_root, adapter, context
                if binding.launch.harness == "claude":
                    raise rc.RuntimeOperationalFailure(
                        "input not ready",
                        failure_class="operational-readiness",
                        cause="runtime_input_not_ready",
                        binding_runtime=rc._runtime_ref(binding),
                        runtime_start=started,
                        created_scope=started.created_scope,
                        pane_may_be_mutated=True,
                        readiness_evidence={"event": "runtime_input_not_ready"},
                    )

            with mock.patch.object(rc, "pane_exists", side_effect=pane_exists), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_start_runtime", side_effect=start
            ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_verify_runtime_input_ready", side_effect=verify_input
            ), mock.patch.object(rc, "_run_post_start_commands"), mock.patch.object(
                rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")
            ), _provenance_patch():
                return rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                    now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
                )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            source = matching_source()
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=source)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_start_runtime"
            ) as start, _provenance_patch():
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            start.assert_not_called()
            self.assertEqual(reused.receipt.status, "reused-recorded")
            self.assertEqual(reused.binding.launch.harness, "codex")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            source = matching_source()
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=source)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            runtime_state = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime_state["controller_source_manifest_sha256"] = "9" * 64
            runtime_path.write_text(json.dumps(runtime_state), encoding="utf-8")
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), _provenance_patch():
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
        self.assertEqual(caught.exception.reason, "recorded-source-manifest-mismatch")
        self.assertNotIn("claude:start", events)

    def test_persona_rollback_stops_codex_receiver_before_bespoke_receiver_start(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_rollback.sh").read_text(encoding="utf-8")
        stop_index = source.index('systemctl --user stop "$CODEX_RECEIVER_UNIT"')
        prove_index = source.index('systemctl --user is-active --quiet "$CODEX_RECEIVER_UNIT"')
        checkpoint_index = source.index("rollback-staged", prove_index)
        enable_index = source.index('systemctl --user enable "$PCX_SERVICE"', checkpoint_index)
        self.assertLess(stop_index, prove_index)
        self.assertLess(prove_index, checkpoint_index)
        self.assertLess(checkpoint_index, enable_index)
        binding = SimpleNamespace(
            persona="recovery",
            account="claude-fallback",
            selected_harness="claude",
            communications=_telegram_comms(),
        )
        self.assertEqual(
            rc.receiver_handoff_actions(binding, "codex-to-claude", "pre"),
            ("stop-codex-poller", "probe-codex-absent"),
        )

    def test_rollback_path_still_stops_and_proves_codex_absent_before_claude_restart(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_rollback.sh").read_text(encoding="utf-8")
        stop_index = source.index('systemctl --user stop "$CODEX_RECEIVER_UNIT"')
        prove_index = source.index('systemctl --user is-active --quiet "$CODEX_RECEIVER_UNIT"')
        checkpoint_index = source.index("rollback-staged", prove_index)
        enable_index = source.index('systemctl --user enable "$PCX_SERVICE"', checkpoint_index)
        self.assertLess(stop_index, prove_index)
        self.assertLess(prove_index, checkpoint_index)
        self.assertLess(checkpoint_index, enable_index)

    def test_post_activation_persona_rollback_invokes_checkpoint_before_bespoke_restart(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_rollback.sh").read_text(encoding="utf-8")
        tmux_gone = source.index("confirmed: default tmux socket has no ${PCX_TMUX_SESSION} session")
        checkpoint = source.index("rollback-staged", tmux_gone)
        self.assertLess(
            tmux_gone,
            checkpoint,
        )
        self.assertLess(
            checkpoint,
            source.index('systemctl --user restart "$PCX_SERVICE"', checkpoint),
        )
        self.assertIn("--account-home \"$PCX_HOME\"", source)

    def test_cutover_post_activation_accepts_codex_selected_runtime_evidence(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_cutover.sh").read_text(encoding="utf-8")
        codex_branch = source.index('if [[ "$SELECTED_HARNESS" == "codex" ]]')
        viability = source.index("python3 -m tools.runtime_controller initial-viability", codex_branch)
        claude_branch = source.index('elif [[ "$SELECTED_HARNESS" == "claude" ]]', codex_branch)
        claude_only = source.index("pgrep -u \"$PCX_ACCOUNT\" -f", claude_branch)
        self.assertLess(viability, claude_branch)
        self.assertLess(claude_branch, claude_only)
        for failure_class in (
            "subscription-cap",
            "operational-launch",
            "operational-readiness",
        ):
            self.assertIn(f"--expect-failure-class {failure_class}", source)
        self.assertIn("--require-receiver-receipt", source)

    def test_installer_harness_override_alone_does_not_persist_initial_runtime_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            overridden = persona_bringup.load_binding(
                manifest,
                "recovery",
                harness_override="codex",
                profile_override="fallback",
                mailbox_hub=root / "mail",
            )
            self.assertEqual(persona_bringup.runtime_reference(primary), "claude/primary")
            self.assertEqual(persona_bringup.runtime_reference(overridden), "codex/fallback")
            self.assertEqual(
                persona_bringup.configured_runtime_targets(manifest, "recovery"),
                ("claude/primary", "codex/fallback"),
            )
            self.assertEqual(rc.initial_viability_state("recovery", state_root=root), {})


class OperatorTargetSwapTests(unittest.TestCase):
    def test_operator_target_swap_refuses_unconfigured_target_and_wrong_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account="definitely-not-current")
            with self.assertRaisesRegex(ValueError, "target principal"):
                persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="codex/fallback",
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=root / "state",
                    mailbox_hub=root / "mail",
                )

            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            with self.assertRaisesRegex(ValueError, "not configured"):
                persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="synthetic/unknown",
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=root / "state",
                    mailbox_hub=root / "mail",
                )

    def test_public_operator_target_swap_uses_recorded_current_for_bidirectional_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            fallback = persona_bringup.load_runtime_reference(manifest, primary, "codex/fallback")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(primary, session_id="primary", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                receipt = persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="codex/fallback",
                    trigger="operator",
                    correlation_id="to-fallback",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                )
            self.assertEqual(receipt.status, "pending")
            state = rc.transition_state("recovery", state_root=state_root)
            state["phase"] = "completed"
            state["successor_read_ack"] = True
            rc._atomic_json(rc._transition_path("recovery", state_root), state)
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(fallback, session_id="fallback", started_at=rc._utc_now()),
            )

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                receipt = persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="claude/primary",
                    trigger="operator",
                    correlation_id="to-primary",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                )
            self.assertEqual(receipt.status, "pending")
            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[0].launch.harness, "claude")
            state = rc.transition_state("recovery", state_root=state_root)
            self.assertEqual(state["source_runtime"], "codex/fallback")
            self.assertEqual(state["destination"], "claude/primary")

    def test_public_operator_target_swap_noops_when_destination_is_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            fallback = persona_bringup.load_runtime_reference(manifest, primary, "codex/fallback")
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "forced-t1",
                "phase": "completed",
                "trigger": "forced-cap",
                "runtime_swapped": True,
                "successor_read_ack": True,
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
                "breaker_open": True,
                "capped_profile": "primary",
                "primary_resets_at": "2026-08-20T00:00:00Z",
            }
            rc._atomic_json(rc._transition_path("recovery", state_root), existing)
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(fallback, session_id="fallback", started_at=rc._utc_now()),
            )
            before = rc._transition_path("recovery", state_root).read_text(encoding="utf-8")
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(rc, "_replace_runtime") as replace:
                receipt = persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="codex/fallback",
                    trigger="operator",
                    correlation_id="new-corr",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                    backend=backend,
                )
            replace.assert_not_called()
            self.assertEqual(receipt.status, "already-active")
            self.assertEqual(receipt.destination, "codex/fallback")
            self.assertEqual(rc._transition_path("recovery", state_root).read_text(encoding="utf-8"), before)
            self.assertIn("proof:recovery:codex:present:present", backend.events)
            self.assertIn("proof:recovery:claude:absent:absent", backend.events)
            self.assertNotIn("recovery:start-codex-poller", backend.events)
            evidence = _read_jsonl(rc._evidence_log_path("recovery", state_root))
            self.assertEqual(evidence[-1]["event"], "operator_target_already_active")

    def test_public_operator_target_swap_compensation_failed_bars_active_noop_before_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(primary, session_id="primary", started_at=rc._utc_now()),
            )
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "source_runtime": "claude/primary",
                "compensation_error": "RuntimeError: restore failed",
            }
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(transition_path, existing)
            before = transition_path.read_text(encoding="utf-8")
            backend = _OperatorBackend()

            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="claude/primary",
                        trigger="operator",
                        correlation_id="new-corr",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=backend,
                    )

            replace.assert_not_called()
            self.assertFalse(backend.events)
            self.assertEqual(transition_path.read_text(encoding="utf-8"), before)

    def test_public_operator_target_swap_compensation_failed_bars_failed_target_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(primary, session_id="primary", started_at=rc._utc_now()),
            )
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "source_runtime": "claude/primary",
                "compensation_error": "RuntimeError: restore failed",
            }
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(transition_path, existing)
            before = transition_path.read_text(encoding="utf-8")
            backend = _OperatorBackend()

            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="old-corr",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=backend,
                    )

            replace.assert_not_called()
            self.assertFalse(backend.events)
            self.assertEqual(transition_path.read_text(encoding="utf-8"), before)

    def test_public_operator_target_swap_noop_fails_on_invalid_runtime_or_receiver_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            fallback = persona_bringup.load_runtime_reference(manifest, primary, "codex/fallback")
            completed = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "forced-t1",
                "phase": "completed",
                "trigger": "forced-cap",
                "runtime_swapped": True,
                "successor_read_ack": True,
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
            }
            rc._atomic_json(rc._transition_path("recovery", state_root), completed)
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "recorded active runtime"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="missing-runtime",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(),
                    )
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(primary, session_id="wrong", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "recorded active runtime"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="mismatch-runtime",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(),
                    )
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(fallback, session_id="fallback", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "active runtime pane is not live"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="dead-pane",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(),
                    )
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="ambiguous-receiver",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(ambiguous=True),
                    )
                with self.assertRaisesRegex(RuntimeError, "receiver state is present; expected absent"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="source-still-present",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(proofs={("claude", "absent"): "present"}),
                    )

    def test_operator_swap_cli_returns_pending_receipt_after_successful_mechanical_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            current = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="primary", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"), mock.patch("builtins.print") as printed:
                code = proactive_swap_init.main(
                    [
                        "--manifest",
                        str(manifest),
                        "--persona",
                        "recovery",
                        "--destination",
                        "codex/fallback",
                        "--trigger",
                        "operator",
                        "--correlation-id",
                        "cli-corr",
                        "--state-root",
                        str(state_root),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(printed.call_args.args[0])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["receipt"]["status"], "pending")

    def test_operator_recovery_cli_returns_recovered_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            current = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            target = persona_bringup.load_runtime_reference(manifest, current, "codex/fallback")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="primary", started_at=rc._utc_now()),
            )
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(
                transition_path,
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "failed-cli",
                    "phase": "compensation-failed",
                    "trigger": "operator",
                    "outgoing_harness": current.launch.harness,
                    "outgoing_profile": current.launch.profile,
                    "incoming_harness": target.launch.harness,
                    "incoming_profile": target.launch.profile,
                },
            )
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch("builtins.print") as printed:
                code = proactive_swap_init.main(
                    [
                        "--recover-compensation-failed",
                        "--manifest",
                        str(manifest),
                        "--persona",
                        "recovery",
                        "--source",
                        "claude/primary",
                        "--destination",
                        "codex/fallback",
                        "--recovery-id",
                        "rec-cli",
                        "--expect-transition-id",
                        "failed-cli",
                        "--expect-transition-sha256",
                        expected_sha,
                        "--reason",
                        "operator-reviewed",
                        "--state-root",
                        str(state_root),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(printed.call_args.args[0])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["receipt"]["status"], "recovered")
            self.assertEqual(payload["receipt"]["recovery_id"], "rec-cli")

    def test_operator_started_unverified_recovery_cli_returns_pending_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(transition_path, _started_unverified_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record), mock.patch.object(
                rc, "_prompt_successor"
            ), mock.patch("builtins.print") as printed:
                code = proactive_swap_init.main(
                    [
                        "--recover-started-unverified",
                        "--manifest",
                        str(manifest),
                        "--persona",
                        "recovery",
                        "--source",
                        "claude/primary",
                        "--destination",
                        "codex/fallback",
                        "--recovery-id",
                        "rec-cli-started",
                        "--expect-transition-id",
                        "t1",
                        "--expect-transition-sha256",
                        expected_sha,
                        "--correlation-id",
                        "corr-retry",
                        "--reason",
                        "operator-reviewed",
                        "--state-root",
                        str(state_root),
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(printed.call_args.args[0])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["receipt"]["status"], "pending")
            self.assertEqual(payload["receipt"]["recovery_id"], "rec-cli-started")

    def test_operator_started_unverified_recovery_cli_requires_exact_args(self) -> None:
        with self.assertRaises(SystemExit):
            proactive_swap_init.main(["--recover-started-unverified", "--persona", "recovery"])

    def test_operator_started_unverified_recovery_cli_reports_errors(self) -> None:
        with mock.patch.object(
            proactive_swap_init.persona_bringup,
            "operator_started_unverified_recovery",
            side_effect=RuntimeError("recovery failed"),
        ), mock.patch("builtins.print") as printed:
            code = proactive_swap_init.main(
                [
                    "--recover-started-unverified",
                    "--persona",
                    "recovery",
                    "--source",
                    "claude/primary",
                    "--destination",
                    "codex/fallback",
                    "--recovery-id",
                    "rec-cli-error",
                    "--expect-transition-id",
                    "t1",
                    "--expect-transition-sha256",
                    "a" * 64,
                    "--correlation-id",
                    "corr-retry",
                    "--reason",
                    "operator-reviewed",
                ]
            )

        self.assertEqual(code, 1)
        payload = json.loads(printed.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertIn("recovery failed", payload["error"])

    def test_public_started_unverified_recovery_resolves_manifest_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            receipt = rc.OperatorStartedUnverifiedRecoveryReceipt(
                schema="pantheon-runtime-operator-started-unverified-recovery/v1",
                persona="recovery",
                status="pending",
                recovery_id="rec-wrapper",
                recovered_from_transition_id="t1",
                recovered_from_sha256="a" * 64,
                correlation_id="corr-retry",
                source_runtime="claude/primary",
                destination="codex/fallback",
                predecessor_archive="",
                predecessor_archive_sha256="",
                phase="awaiting-successor-read",
                runtime_swapped=True,
                receiver_pre_done=True,
                receiver_post_done=True,
                chronicle_evidence="",
                expected_successor_read_evidence="",
                recovered_at="2026-08-01T00:00:00Z",
            )

            with mock.patch.object(rc, "recover_operator_started_unverified", return_value=receipt) as recover:
                result = persona_bringup.operator_started_unverified_recovery(
                    manifest_path=manifest,
                    persona="recovery",
                    source="claude/primary",
                    destination="codex/fallback",
                    recovery_id="rec-wrapper",
                    expected_transition_id="t1",
                    expected_transition_sha256="a" * 64,
                    correlation_id="corr-retry",
                    reason="operator-reviewed",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                )

            self.assertEqual(result.recovery_id, "rec-wrapper")
            args = recover.call_args.args
            self.assertEqual(persona_bringup.runtime_reference(args[0]), "claude/primary")
            self.assertEqual(persona_bringup.runtime_reference(args[1]), "codex/fallback")

    def test_recovered_phase_selects_outgoing_source_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            rc._atomic_json(
                rc._transition_path("recovery", state_root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "phase": "recovered",
                    "trigger": "operator-recovery",
                    "recovery_id": "rec-select",
                    "outgoing_harness": "codex",
                    "outgoing_profile": "fallback",
                    "incoming_harness": "claude",
                    "incoming_profile": "primary",
                },
            )

            selected = persona_bringup.select_recorded_runtime_binding(
                manifest,
                primary,
                state_dir=state_root,
            )

            self.assertEqual(persona_bringup.runtime_reference(selected), "codex/fallback")

    def test_operator_completion_gate_accepts_only_completed_receipt(self) -> None:
        gate = proactive_swap_init.operator_completion_gate(
            {
                "ok": True,
                "receipt": {
                    "status": "completed",
                    "correlation_id": "corr-1",
                    "destination": "codex/fallback",
                },
            },
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertTrue(gate.ok)
        self.assertEqual(gate.status, "completed")
        self.assertEqual(gate.correlation_id, "corr-1")
        pending = proactive_swap_init.operator_completion_gate(
            {"ok": True, "receipt": {"status": "pending", "correlation_id": "corr-1", "destination": "codex/fallback"}},
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertFalse(pending.ok)

    def test_operator_completion_gate_requires_expected_identity(self) -> None:
        payload = {
            "ok": True,
            "receipt": {
                "status": "completed",
                "correlation_id": "corr-1",
                "destination": "codex/fallback",
            },
        }
        for expected_correlation_id, expected_destination in (
            ("wrong", "codex/fallback"),
            ("corr-1", "claude/primary"),
            ("", "codex/fallback"),
            ("corr-1", ""),
        ):
            with self.subTest(expected_correlation_id=expected_correlation_id, expected_destination=expected_destination):
                gate = proactive_swap_init.operator_completion_gate(
                    payload,
                    expected_correlation_id=expected_correlation_id,
                    expected_destination=expected_destination,
                )
                self.assertFalse(gate.ok)

    def test_operator_completion_gate_rejects_pending_malformed_missing_and_already_active(self) -> None:
        cases = (
            {"ok": True, "receipt": {"status": "pending"}},
            {"ok": True, "receipt": {"status": "already-active"}},
            {"ok": True},
            {"ok": False, "receipt": {"status": "completed"}},
            [],
        )
        for payload in cases:
            with self.subTest(payload=payload):
                if isinstance(payload, dict):
                    gate = proactive_swap_init.operator_completion_gate(
                        payload,
                        expected_correlation_id="corr-1",
                        expected_destination="codex/fallback",
                    )
                    self.assertFalse(gate.ok)
                else:
                    with self.assertRaises(ValueError):
                        proactive_swap_init.parse_operator_receipt(json.dumps(payload))
        with self.assertRaises(ValueError):
            proactive_swap_init.parse_operator_receipt("{not-json")

    def test_operator_completion_gate_cli_exit_codes_and_bounded_errors(self) -> None:
        stdout = io.StringIO()
        code = proactive_swap_init.completion_gate_main(
            stdin=io.StringIO(
                json.dumps(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "completed",
                            "correlation_id": "corr-1",
                            "destination": "codex/fallback",
                        },
                    }
                )
            ),
            stdout=stdout,
            stderr=io.StringIO(),
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])

        stdout = io.StringIO()
        code = proactive_swap_init.completion_gate_main(
            stdin=io.StringIO(json.dumps({"ok": True, "receipt": {"status": "pending", "correlation_id": "corr-1", "destination": "codex/fallback"}})),
            stdout=stdout,
            stderr=io.StringIO(),
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertLessEqual(len(payload["error"]), 240)

    def test_operator_completion_gate_cli_rejects_wrong_identity_and_oversized_input(self) -> None:
        stdout = io.StringIO()
        code = proactive_swap_init.completion_gate_main(
            stdin=io.StringIO(
                json.dumps(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "completed",
                            "correlation_id": "other",
                            "destination": "codex/fallback",
                        },
                    }
                )
            ),
            stdout=stdout,
            stderr=io.StringIO(),
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

        stdout = io.StringIO()
        code = proactive_swap_init.completion_gate_main(
            stdin=io.StringIO("x" * (proactive_swap_init.OPERATOR_RECEIPT_LIMIT_BYTES + 1)),
            stdout=stdout,
            stderr=io.StringIO(),
            expected_correlation_id="corr-1",
            expected_destination="codex/fallback",
        )
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertLessEqual(len(payload["error"]), 240)

    def test_operator_target_swap_refuses_mismatched_runtime_or_ambiguous_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
                communications=_telegram_comms(),
            )
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                {"persona": "recovery", "role": current.role, "harness": "codex", "profile": "other"},
            )
            with self.assertRaisesRegex(RuntimeError, "runtime"):
                rc._operator_target_swap_resolved(
                    current,
                    target,
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=root,
                    backend=_OperatorBackend(),
                )

            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-2",
                        state_root=root,
                        backend=_OperatorBackend(ambiguous=True),
                    )

    def test_operator_target_swap_uses_durable_controller_transition_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=backend,
                    )
            self.assertLess(backend.events.index("proof:recovery:claude:absent:absent"), backend.events.index("recovery:start-codex-poller"))
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["trigger"], "operator")
            self.assertEqual(state["correlation_id"], "corr-1")
            self.assertEqual(state["destination"], "codex/fallback")
            self.assertTrue(state["runtime_swapped"])
            self.assertTrue(state["receiver_transferred"])
            self.assertEqual(state["phase"], "awaiting-successor-read")
            evidence = Path(state["chronicle_evidence"])
            self.assertTrue(evidence.is_file())
            self.assertEqual(evidence.parent, root / "recovery" / "runtime-controller")
            self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), state["operator_handoff_sha256"])

    def test_operator_target_swap_does_not_use_missing_or_stale_home_handoff_as_ack_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            stale = root / "HANDOFF.md"
            stale.write_text("stale outgoing handoff\n", encoding="utf-8")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            evidence = Path(state["chronicle_evidence"])
            self.assertNotEqual(evidence, stale)
            self.assertTrue(evidence.is_file())
            self.assertIn(state["transition_id"], evidence.name)
            self.assertIn(evidence.name, state["expected_successor_read_evidence"])

    def test_operator_successor_ack_requires_handoff_file_hash_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            evidence = state["expected_successor_read_evidence"]
            handoff = Path(state["chronicle_evidence"])
            handoff.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                rc.acknowledge_successor_read("recovery", evidence=evidence, state_root=root)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            handoff.unlink()
            with self.assertRaisesRegex(ValueError, "unavailable"):
                rc.acknowledge_successor_read("recovery", evidence=evidence, state_root=root)

    def test_operator_target_swap_backend_none_uses_bridge_subprocess_probes_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )

            def fake_run(argv, **kwargs):
                del kwargs
                command = argv[2]
                if command == "receiver-state":
                    expected = argv[argv.index("--expected") + 1]
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "state": expected, "evidence": {"expected": expected}}), "")
                if command == "handoff":
                    phase = argv[argv.index("--phase") + 1]
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "operations": [f"bridge-{phase}"]}), "")
                raise AssertionError(argv)

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                harness_integration.subprocess, "run", side_effect=fake_run
            ) as bridge_run, mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=None,
                    )
            commands = [call.args[0][2] for call in bridge_run.call_args_list]
            self.assertEqual(commands, ["receiver-state", "receiver-state", "handoff", "receiver-state", "handoff"])

            def failing_run(argv, **kwargs):
                del kwargs
                return subprocess.CompletedProcess(argv, 1, json.dumps({"ok": False, "error": "probe failed"}), "")

            root2 = Path(tmp) / "fail"
            current2 = _binding(root2, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target2 = _binding(root2, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root2 / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current2, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                harness_integration.subprocess, "run", side_effect=failing_run
            ), mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(RuntimeError, "probe failed"):
                    rc._operator_target_swap_resolved(
                        current2,
                        target2,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root2,
                        backend=None,
                    )
            replace.assert_not_called()

    def test_operator_runtime_owned_swap_uses_single_replace_and_absent_proof_only_after_runtime_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-runtime-owned",
                        state_root=root,
                        backend=backend,
                    )
            replace.assert_called_once()
            events = backend.events
            self.assertNotIn("recovery:respawn-pane-to-codex", events)
            self.assertNotIn("recovery:respawn-pane-to-claude", events)
            self.assertNotIn("recovery:enable-claude-receiver", events)
            self.assertNotIn("recovery:start-claude-receiver", events)
            pre_present = events.index("recovery:probe-claude-present")
            before_post_absent = events.index("proof:recovery:claude:absent:absent")
            codex_start = events.index("recovery:start-codex-poller")
            self.assertLess(pre_present, before_post_absent)
            self.assertLess(before_post_absent, codex_start)

    def test_operator_successful_replacement_resolves_started_unverified_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-marker",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            self.assertTrue(state["runtime_swapped"])
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertEqual(state["runtime_mutation"]["status"], "resolved")
            self.assertFalse(state["runtime_mutation"]["pane_may_be_mutated"])

    def test_operator_mutated_readiness_failure_restores_source_and_records_compensated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            calls: list[str] = []

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                del context
                calls.append(binding.launch.harness)
                if binding.launch.harness == "codex":
                    if on_started is not None:
                        on_started(
                            rc._RuntimeStartEvent(
                                started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                                boundary=rc._utc_now(),
                            )
                        )
                    raise _mutated_replace_error()
                return _replace_and_record(binding, state_root=state_root)

            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ):
                with self.assertRaises(rc.RuntimeReplacementError):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-mutated",
                        state_root=root,
                        backend=backend,
                    )
            self.assertEqual(calls, ["codex", "claude"])
            self.assertIn("recovery:stop-codex-poller", backend.events)
            self.assertIn("recovery:restore-watchdog-units", backend.events)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensated")
            self.assertFalse(state["runtime_swapped"])
            self.assertEqual(state["runtime_mutation"]["status"], "compensated")

    def test_operator_restore_failure_records_compensation_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                del state_root, context
                if binding.launch.harness == "codex":
                    if on_started is not None:
                        on_started(
                            rc._RuntimeStartEvent(
                                started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                                boundary=rc._utc_now(),
                            )
                        )
                    raise _mutated_replace_error()
                raise RuntimeError("restore failed")

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ):
                with self.assertRaises(rc.RuntimeReplacementError):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-restore-fail",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensation-failed")
            self.assertIn("compensation_error", state)

    def test_operator_prestart_failure_does_not_restore_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            calls: list[str] = []

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                del state_root, context, on_started
                calls.append(binding.launch.harness)
                raise _prestart_replace_error()

            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ):
                with self.assertRaises(rc.RuntimeReplacementError):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-prestart",
                        state_root=root,
                        backend=backend,
                    )
            self.assertEqual(calls, ["codex"])
            self.assertNotIn("recovery:stop-codex-poller", backend.events)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensated")
            self.assertNotIn("runtime_mutation", state)

    def test_operator_retry_with_unresolved_started_unverified_compensates_without_new_target_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "t1",
                    "phase": "preparing",
                    "trigger": "operator",
                    "correlation_id": "corr-retry",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "source_transition_id": "",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                    "runtime_swapped": False,
                    "receiver_pre_done": True,
                    "receiver_post_done": False,
                    "receiver_transferred": False,
                    "successor_read_ack": False,
                    "runtime_mutation": {
                        "status": "started-unverified",
                        "pane_may_be_mutated": True,
                        "scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT,
                        "created_scope": False,
                    },
                },
            )
            calls: list[str] = []

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                del context, on_started
                calls.append(binding.launch.harness)
                if binding.launch.harness == "codex":
                    raise AssertionError("target replace must not retry")
                return _replace_and_record(binding, state_root=state_root)

            backend = _OperatorBackend()
            with mock.patch.object(rc, "_replace_runtime", side_effect=replace):
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "fresh operator"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-retry",
                        state_root=root,
                        backend=backend,
                    )
            self.assertEqual(calls, ["claude"])
            self.assertIn("recovery:stop-codex-poller", backend.events)
            self.assertIn("recovery:restore-watchdog-units", backend.events)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensated")
            self.assertEqual(state["runtime_mutation"]["status"], "compensated")

            state["phase"] = "preparing"
            state["runtime_mutation"]["status"] = "started-unverified"
            state["runtime_mutation"]["pane_may_be_mutated"] = True
            rc._atomic_json(rc._transition_path("recovery", root), state)
            with mock.patch.object(rc, "_replace_runtime", side_effect=RuntimeError("restore failed")):
                with self.assertRaisesRegex(RuntimeError, "restore failed"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-retry",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensation-failed")
            self.assertEqual(state["runtime_mutation"]["status"], "compensation-failed")

    def test_operator_retry_with_explicit_started_unverified_recovery_marker_is_barred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            base = _started_unverified_state()
            marker = _started_unverified_marker(root, transition_sha="0" * 64)
            base["started_unverified_recovery"] = marker
            rc._atomic_json(rc._transition_path("recovery", root), base)

            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "recover-started-unverified"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-retry",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )

            replace.assert_not_called()

    def test_started_unverified_recovery_success_checkpoints_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _started_unverified_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            backend = _OperatorBackend()

            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record) as replace, mock.patch.object(
                rc, "_prompt_successor"
            ) as prompt:
                receipt = rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256=expected_sha,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=backend,
                )

            self.assertEqual(receipt.status, "pending")
            replace.assert_called_once()
            prompt.assert_called_once()
            events = backend.events
            self.assertLess(
                events.index("proof:recovery:claude:absent:absent"),
                events.index("recovery:start-codex-poller"),
            )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertTrue(state["runtime_swapped"])
            self.assertTrue(state["receiver_post_done"])
            self.assertTrue(state["receiver_transferred"])
            self.assertEqual(state["runtime_mutation"]["status"], "resolved")
            self.assertIn("started_unverified_recovery", state)
            handoff_body = Path(state["chronicle_evidence"]).read_text(encoding="utf-8")
            self.assertIn("receiver_transferred: true", handoff_body)
            self.assertNotIn("receiver_transferred: false", handoff_body)

    def test_started_unverified_recovery_reuses_archive_after_crash_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            predecessor = _started_unverified_state()
            rc._atomic_json(transition_path, predecessor)
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            archive = root / "recovery" / "runtime-controller" / "operator-started-unverified-recovery-rec-1.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive_payload = {
                "schema": "pantheon-runtime-operator-started-unverified-recovery-archive/v1",
                "persona": "recovery",
                "recovery_id": "rec-1",
                "predecessor_sha256": expected_sha,
                "predecessor_transition": predecessor,
                "reason": "operator-reviewed",
                "archived_at": "2026-08-01T00:00:00Z",
            }
            archive.write_text(json.dumps(archive_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record), mock.patch.object(
                rc, "_prompt_successor"
            ):
                receipt = rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256=expected_sha,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            self.assertEqual(receipt.status, "pending")
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["started_unverified_recovery"]["predecessor_archive_sha256"], archive_sha)

    def test_started_unverified_recovery_source_absent_failure_resumes_without_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _started_unverified_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            backend = _OperatorBackend(proofs={("claude", "absent"): "present"})

            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record) as replace, mock.patch.object(
                rc, "_prompt_successor"
            ) as prompt:
                with self.assertRaisesRegex(RuntimeError, "receiver state is present"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=expected_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=backend,
                    )

            replace.assert_called_once()
            prompt.assert_not_called()
            self.assertNotIn("recovery:start-codex-poller", backend.events)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "preparing")
            self.assertTrue(state["runtime_swapped"])
            self.assertFalse(state["receiver_post_done"])
            self.assertIn("recovery_error", state)
            retry_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as retry_replace, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_prompt_successor"
            ):
                retry = rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256=retry_sha,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            retry_replace.assert_not_called()
            self.assertEqual(retry.status, "pending")

    def test_started_unverified_recovery_dead_target_rearms_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="dead-target", started_at=rc._utc_now()),
            )
            state = _started_unverified_state(runtime_swapped=True)
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()
            backend = _OperatorBackend()
            transition_writes: list[dict[str, object]] = []
            original_atomic_json = rc._atomic_json

            def record_transition(path: Path, payload: object) -> None:
                original_atomic_json(path, payload)
                if path == rc._transition_path("recovery", root):
                    transition_writes.append(json.loads(path.read_text(encoding="utf-8")))

            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ) as replace, mock.patch.object(rc, "_prompt_successor") as prompt, mock.patch.object(
                rc, "_atomic_json", side_effect=record_transition
            ):
                receipt = rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256=current_sha,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=backend,
                )

            self.assertEqual(receipt.status, "pending")
            replace.assert_called_once()
            prompt.assert_called_once()
            self.assertIn(
                "target-rearmed",
                [
                    item.get("started_unverified_recovery", {}).get("status")
                    for item in transition_writes
                ],
            )
            events = backend.events
            self.assertLess(
                events.index("proof:recovery:claude:absent:absent"),
                events.index("recovery:start-codex-poller"),
            )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertTrue(state["runtime_swapped"])
            self.assertTrue(state["receiver_post_done"])
            self.assertTrue(state["receiver_transferred"])
            handoff_body = Path(state["chronicle_evidence"]).read_text(encoding="utf-8")
            self.assertIn("receiver_transferred: true", handoff_body)

    def test_started_unverified_recovery_live_target_does_not_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="live-target", started_at=rc._utc_now()),
            )
            state = _started_unverified_state(runtime_swapped=True)
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_prompt_successor"
            ):
                receipt = rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256=current_sha,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            replace.assert_not_called()
            self.assertEqual(receipt.status, "pending")

    def test_started_unverified_recovery_non_dead_target_proof_error_does_not_replace(self) -> None:
        for case in ("metadata", "input-ready", "archive"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
                target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
                runtime_target = current if case == "metadata" else target
                rc._atomic_json(
                    root / "recovery" / "runtime-controller" / "runtime.json",
                    rc._runtime_payload(runtime_target, session_id="target", started_at=rc._utc_now()),
                )
                state = _started_unverified_state(runtime_swapped=True)
                marker = _started_unverified_marker(root, transition_sha="original")
                if case == "archive":
                    marker["predecessor_archive_sha256"] = "0" * 64
                state["started_unverified_recovery"] = marker
                rc._atomic_json(rc._transition_path("recovery", root), state)
                current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

                with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                    rc, "pane_exists", return_value=True
                ), mock.patch.object(rc, "_verify_runtime_input_ready", side_effect=RuntimeError("input readiness failed")):
                    if case == "archive":
                        expected = rc.RuntimeTransitionPending
                        pattern = "archive digest"
                    elif case == "metadata":
                        expected = RuntimeError
                        pattern = "recorded target runtime"
                    else:
                        expected = RuntimeError
                        pattern = "input readiness"
                    with self.assertRaisesRegex(expected, pattern):
                        rc.recover_operator_started_unverified(
                            current,
                            target,
                            recovery_id="rec-1",
                            expected_transition_id="t1",
                            expected_transition_sha256=current_sha,
                            correlation_id="corr-retry",
                            state_root=root,
                            reason="operator-reviewed",
                            backend=_OperatorBackend(),
                        )

                replace.assert_not_called()

    def test_started_unverified_recovery_rearm_failure_checkpoints_without_receiver_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="dead-target", started_at=rc._utc_now()),
            )
            state = _started_unverified_state(runtime_swapped=True)
            marker = _started_unverified_marker(root, transition_sha="original")
            state["started_unverified_recovery"] = marker
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()
            secret = "sk-ant-" + ("A" * 32)
            message = f"replacement failed token={secret} " + ("x" * 1500)

            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_replace_runtime", side_effect=RuntimeError(message)
            ), mock.patch.object(rc, "_run_integration_handoff") as receiver_post, mock.patch.object(
                rc, "_write_operator_handoff"
            ) as handoff, mock.patch.object(rc, "_prompt_successor") as prompt:
                with self.assertRaisesRegex(RuntimeError, "replacement failed"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            receiver_post.assert_not_called()
            handoff.assert_not_called()
            prompt.assert_not_called()
            state = rc.transition_state("recovery", state_root=root)
            recovery = state["started_unverified_recovery"]
            self.assertEqual(state["phase"], "preparing")
            self.assertTrue(state["runtime_swapped"])
            self.assertFalse(state["receiver_post_done"])
            self.assertFalse(state["receiver_transferred"])
            self.assertEqual(recovery["status"], "failed")
            self.assertEqual(recovery["recovery_id"], marker["recovery_id"])
            self.assertEqual(recovery["predecessor_archive"], marker["predecessor_archive"])
            self.assertEqual(recovery["predecessor_archive_sha256"], marker["predecessor_archive_sha256"])
            self.assertEqual(recovery["source_runtime"], "claude/primary")
            self.assertEqual(recovery["destination"], "codex/fallback")
            self.assertLessEqual(len(state["recovery_error"]), 1000)
            self.assertLessEqual(len(recovery["last_error"]), 1000)
            self.assertNotIn(secret, state["recovery_error"])
            self.assertNotIn(secret, recovery["last_error"])

    def test_started_unverified_recovery_resume_reproves_skipped_receiver_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()),
            )
            state = _started_unverified_state(runtime_swapped=True, receiver_post_done=True)
            rc._atomic_json(rc._transition_path("recovery", root), state)
            state_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_write_operator_handoff"
            ) as handoff:
                with self.assertRaisesRegex(RuntimeError, "receiver state is absent"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(proofs={("codex", "present"): "absent"}),
                    )

            self.assertNotEqual(state_sha, current_sha)
            replace.assert_not_called()
            handoff.assert_not_called()

    def test_started_unverified_recovery_rejects_stale_receiver_fact_in_existing_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()),
            )
            state = _started_unverified_state(runtime_swapped=True, receiver_post_done=True)
            handoff = rc._write_operator_handoff("recovery", "t1", state, state_root=root)
            self.assertIn("receiver_transferred: false", handoff.read_text(encoding="utf-8"))
            state["chronicle_evidence"] = str(handoff)
            state["operator_handoff_sha256"] = hashlib.sha256(handoff.read_bytes()).hexdigest()
            state["expected_successor_read_evidence"] = f"hook:runtime-transfer:t1:{handoff.name}"
            state["successor_read_target"] = str(handoff)
            state["successor_evidence_kind"] = "runtime-transfer"
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_prompt_successor"
            ) as prompt:
                with self.assertRaisesRegex(RuntimeError, "receiver_transferred mismatch"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            replace.assert_not_called()
            prompt.assert_not_called()

    def test_started_unverified_recovery_rejects_stale_checkpoint_digest_and_bad_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            rc._atomic_json(runtime_path, rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()))
            handoff = root / "bad-handoff.md"
            handoff.write_text("changed\n", encoding="utf-8")
            state = _started_unverified_state(
                runtime_swapped=True,
                receiver_post_done=True,
                handoff=handoff,
                handoff_sha256="0" * 64,
            )
            rc._atomic_json(rc._transition_path("recovery", root), state)
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with self.assertRaisesRegex(rc.RuntimeTransitionPending, "digest mismatch"):
                rc.recover_operator_started_unverified(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="t1",
                    expected_transition_sha256="0" * 64,
                    correlation_id="corr-retry",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )
            with mock.patch.object(rc, "_verify_runtime_input_ready"), mock.patch.object(
                rc, "pane_exists", return_value=True
            ), mock.patch.object(rc, "_prompt_successor") as prompt:
                with self.assertRaisesRegex(RuntimeError, "handoff digest"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )
            prompt.assert_not_called()

    def test_started_unverified_recovery_rejects_bad_marker_archive_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            state = _started_unverified_state(runtime_swapped=True)
            marker = _started_unverified_marker(root, transition_sha="original")
            marker["predecessor_archive_sha256"] = "0" * 64
            state["started_unverified_recovery"] = marker
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "archive digest"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            replace.assert_not_called()

    def test_started_unverified_recovery_already_pending_requires_full_terminal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            state = _started_unverified_state(runtime_swapped=True, receiver_post_done=False)
            state["phase"] = "awaiting-successor-read"
            state["started_unverified_recovery"] = _started_unverified_marker(root, transition_sha="original")
            rc._atomic_json(rc._transition_path("recovery", root), state)
            current_sha = hashlib.sha256(rc._transition_path("recovery", root).read_bytes()).hexdigest()

            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "receiver post"):
                    rc.recover_operator_started_unverified(
                        current,
                        target,
                        recovery_id="rec-1",
                        expected_transition_id="t1",
                        expected_transition_sha256=current_sha,
                        correlation_id="corr-retry",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            replace.assert_not_called()

    def test_operator_compensation_failed_state_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "source_runtime": "claude/primary",
                "source_transition_id": "",
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
                "runtime_swapped": False,
                "receiver_pre_done": True,
                "runtime_mutation": {
                    "status": "compensation-failed",
                    "pane_may_be_mutated": True,
                },
                "compensation_error": "RuntimeError: restore failed",
            }
            path = rc._transition_path("recovery", root)
            rc._atomic_json(path, existing)
            before = path.read_text(encoding="utf-8")
            backend = _OperatorBackend()
            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="new-corr",
                        state_root=root,
                        backend=backend,
                    )
            replace.assert_not_called()
            self.assertFalse(backend.events)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_operator_compensation_recovery_shared_tmux_archives_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            rc._atomic_json(runtime_path, rc._runtime_payload(current, session_id="source", started_at=rc._utc_now()))
            failed = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "source_runtime": "claude/primary",
                "source_transition_id": "",
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
                "runtime_swapped": False,
                "receiver_pre_done": True,
                "runtime_mutation": {
                    "status": "compensation-failed",
                    "pane_may_be_mutated": True,
                },
                "compensation_error": "RuntimeError: restore failed",
            }
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, failed)
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            backend = _OperatorBackend()

            with mock.patch.object(rc, "pane_exists", return_value=True) as pane_exists, mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True):
                receipt = rc.recover_operator_compensation_failed(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="failed-t1",
                    expected_transition_sha256=expected_sha,
                    state_root=root,
                    reason="operator-reviewed",
                    backend=backend,
                )

            self.assertEqual(receipt.status, "recovered")
            self.assertEqual(receipt.phase, "recovered")
            self.assertTrue(receipt.source_runtime_proven)
            self.assertTrue(receipt.source_readiness_proven)
            self.assertFalse(receipt.target_absent_proven)
            self.assertTrue(receipt.target_metadata_mismatch_proven)
            self.assertTrue(receipt.receiver_source_present_proven)
            self.assertTrue(receipt.receiver_target_absent_proven)
            self.assertEqual(pane_exists.call_count, 1)
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "recovered")
            self.assertTrue(state["fresh_operator_initiation_allowed"])
            self.assertEqual(state["outgoing_harness"], "claude")
            self.assertEqual(state["incoming_harness"], "codex")
            archive = Path(receipt.predecessor_archive)
            self.assertTrue(archive.exists())
            archive_payload = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(archive_payload["predecessor_sha256"], expected_sha)
            self.assertEqual(archive_payload["predecessor_transition"]["phase"], "compensation-failed")
            evidence = (root / "recovery" / "runtime-controller" / "controller-evidence.jsonl").read_text(encoding="utf-8")
            self.assertIn("operator_compensation_recovery_authorized", evidence)

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True):
                repeat = rc.recover_operator_compensation_failed(
                    current,
                    target,
                    recovery_id="rec-1",
                    expected_transition_id="failed-t1",
                    expected_transition_sha256=expected_sha,
                    state_root=root,
                    reason="operator-reviewed",
                    backend=backend,
                )
            self.assertEqual(repeat.status, "already-recovered")

    def test_operator_compensation_failed_target_live_rearms_shared_tmux_and_recovers_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            backend = _OperatorBackend()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ) as replace, mock.patch.object(rc, "_prompt_successor") as prompt:
                receipt = rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=expected_sha,
                    expected_predecessor_sha256=expected_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=backend,
                )

            self.assertEqual(receipt.status, "pending")
            self.assertEqual(receipt.destination, "claude/primary")
            replace.assert_called_once()
            prompt.assert_called_once()
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertTrue(state["runtime_swapped"])
            self.assertTrue(state["receiver_post_done"])
            self.assertTrue(state["receiver_transferred"])
            self.assertTrue(state["target_readiness_proven"])
            self.assertEqual(state["runtime_mutation"]["status"], "resolved")
            runtime_state = json.loads((root / "recovery" / "runtime-controller" / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["harness"], "claude")
            self.assertIn("proof:recovery:codex:absent:absent", backend.events)
            self.assertIn("proof:recovery:claude:present:present", backend.events)
            self.assertLess(
                backend.events.index("proof:recovery:codex:absent:absent"),
                backend.events.index("recovery:probe-claude-present"),
            )
            handoff_body = Path(state["chronicle_evidence"]).read_text(encoding="utf-8")
            self.assertIn("runtime_swapped: true", handoff_body)
            self.assertIn("receiver_transferred: true", handoff_body)

    def test_operator_compensation_failed_target_live_initial_recovery_replaces_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            events: list[str] = []

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                events.append("replace")
                return _replace_and_record(binding, state_root=state_root, context=context, on_started=on_started)

            def receiver_post(binding, direction, phase, *, backend, reason):
                del binding, direction, backend, reason
                events.append(f"receiver-{phase}")
                return ReceiverHookReceipt(True, ("post",), {})

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ) as replace_mock, mock.patch.object(
                rc, "_run_integration_handoff", side_effect=receiver_post
            ), mock.patch.object(rc, "_prompt_successor"):
                rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=expected_sha,
                    expected_predecessor_sha256=expected_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            replace_mock.assert_called_once()
            self.assertEqual(events, ["replace", "receiver-post"])

    def test_operator_compensation_failed_target_live_replay_rearms_after_receiver_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            predecessor_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            sessions: list[str] = []

            def replace(binding, *, state_root: Path | None, context=None, on_started=None):
                if on_started is not None:
                    on_started(
                        rc._RuntimeStartEvent(
                            started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                            boundary=rc._utc_now(),
                        )
                    )
                session_id = f"replacement-{len(sessions) + 1}"
                sessions.append(session_id)
                rc._atomic_json(
                    Path(state_root) / binding.persona / "runtime-controller" / "runtime.json",
                    rc._runtime_payload(binding, session_id=session_id, started_at=rc._utc_now()),
                )
                return rc.RuntimeHandle(binding.launch, binding.controller.tmux, session_id, rc._utc_now())

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ), mock.patch.object(rc, "_run_integration_handoff", side_effect=RuntimeError("receiver timeout")):
                with self.assertRaisesRegex(RuntimeError, "receiver timeout"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=predecessor_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            failed_state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(failed_state["phase"], "preparing")
            self.assertTrue(failed_state["runtime_swapped"])
            self.assertFalse(failed_state["receiver_post_done"])
            self.assertTrue(failed_state["compensation_failed_target_live_recovery"]["target_rearmed"])
            replay_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace
            ) as replay_replace, mock.patch.object(rc, "_prompt_successor"):
                receipt = rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=replay_sha,
                    expected_predecessor_sha256=predecessor_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            self.assertEqual(receipt.status, "pending")
            replay_replace.assert_called_once()
            self.assertEqual(sessions, ["replacement-1", "replacement-2"])
            runtime_state = json.loads((root / "recovery" / "runtime-controller" / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["session_id"], "replacement-2")
            state = rc.transition_state("recovery", state_root=root)
            self.assertTrue(state["receiver_post_done"])
            self.assertTrue(state["receiver_transferred"])
            self.assertEqual(receipt.recovered_from_sha256, predecessor_sha)

    def test_operator_compensation_failed_target_live_replay_refresh_requires_recorded_target_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            predecessor_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_run_integration_handoff", side_effect=RuntimeError("receiver timeout")):
                with self.assertRaisesRegex(RuntimeError, "receiver timeout"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=predecessor_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            (root / "recovery" / "runtime-controller" / "runtime.json").unlink()
            replay_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace, mock.patch.object(rc, "_run_integration_handoff") as handoff, mock.patch.object(
                rc, "_prompt_successor"
            ) as prompt:
                with self.assertRaisesRegex(RuntimeError, "recorded target runtime"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=replay_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )
            replace.assert_not_called()
            handoff.assert_not_called()
            prompt.assert_not_called()

    def test_operator_compensation_failed_target_live_replay_rearm_failure_checkpoints_without_receiver_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            predecessor_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            secret = "sk-ant-" + ("C" * 32)

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_run_integration_handoff", side_effect=RuntimeError("receiver timeout")):
                with self.assertRaisesRegex(RuntimeError, "receiver timeout"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=predecessor_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            replay_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=RuntimeError(f"rearm failed {secret} " + "x" * 1500)
            ), mock.patch.object(rc, "_run_integration_handoff") as handoff, mock.patch.object(
                rc, "_write_operator_handoff"
            ) as write_handoff, mock.patch.object(rc, "_prompt_successor") as prompt:
                with self.assertRaisesRegex(RuntimeError, "rearm failed"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=replay_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            handoff.assert_not_called()
            write_handoff.assert_not_called()
            prompt.assert_not_called()
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "preparing")
            self.assertTrue(state["runtime_swapped"])
            self.assertFalse(state["receiver_post_done"])
            self.assertEqual(state["compensation_failed_target_live_recovery"]["status"], "failed")
            self.assertLessEqual(len(state["recovery_error"]), 1000)
            self.assertNotIn(secret, state["recovery_error"])

    def test_operator_compensation_failed_target_live_already_pending_replay_does_not_rearm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            predecessor_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=predecessor_sha,
                    expected_predecessor_sha256=predecessor_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            pending_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace:
                receipt = rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=pending_sha,
                    expected_predecessor_sha256=predecessor_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            self.assertEqual(receipt.status, "already-pending")
            replace.assert_not_called()

    def test_operator_compensation_failed_target_live_replay_binds_original_predecessor_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            predecessor_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor", side_effect=RuntimeError("prompt failed")):
                with self.assertRaisesRegex(RuntimeError, "prompt failed"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=predecessor_sha,
                        expected_predecessor_sha256=predecessor_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=backend,
                    )
            failed_state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(failed_state["phase"], "preparing")
            self.assertEqual(failed_state["compensation_failed_target_live_recovery"]["status"], "failed")
            replay_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace, mock.patch.object(rc, "_prompt_successor") as prompt:
                receipt = rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=replay_sha,
                    expected_predecessor_sha256=predecessor_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            self.assertEqual(receipt.status, "pending")
            replace.assert_not_called()
            prompt.assert_called_once()
            self.assertEqual(receipt.recovered_from_sha256, predecessor_sha)

            current_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(rc.RuntimeTransitionPending, "predecessor digest"):
                rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=current_sha,
                    expected_predecessor_sha256="0" * 64,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

    def test_operator_compensation_failed_target_live_rejects_digest_identity_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            before = transition_path.read_text(encoding="utf-8")

            cases = (
                ("digest mismatch", {"expected_transition_sha256": "0" * 64}),
                ("predecessor digest", {"expected_predecessor_sha256": "0" * 64}),
                ("expected target-live recovery transition", {"correlation_id": "wrong"}),
            )
            for pattern, overrides in cases:
                kwargs = {
                    "recovery_id": "rec-target",
                    "expected_transition_id": "failed-target-live",
                    "expected_transition_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                    "expected_predecessor_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                    "correlation_id": "corr-target-live",
                    "state_root": root,
                    "reason": "operator-reviewed",
                    "backend": _OperatorBackend(),
                    **overrides,
                }
                with self.subTest(pattern=pattern):
                    with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(rc, "_prompt_successor") as prompt:
                        with self.assertRaisesRegex(rc.RuntimeTransitionPending, pattern):
                            rc.recover_operator_compensation_failed_target_live(source, target, **kwargs)
                    replace.assert_not_called()
                    prompt.assert_not_called()
                    self.assertEqual(transition_path.read_text(encoding="utf-8"), before)

    def test_operator_compensation_failed_target_live_rearm_failure_checkpoints_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            secret = "sk-ant-" + ("B" * 32)

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=RuntimeError(f"rearm failed {secret} " + "x" * 1500)
            ), mock.patch.object(rc, "_run_integration_handoff") as handoff, mock.patch.object(
                rc, "_write_operator_handoff"
            ) as write_handoff, mock.patch.object(rc, "_prompt_successor") as prompt:
                with self.assertRaisesRegex(RuntimeError, "rearm failed"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=expected_sha,
                        expected_predecessor_sha256=expected_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            handoff.assert_not_called()
            write_handoff.assert_not_called()
            prompt.assert_not_called()
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "compensation-failed")
            self.assertFalse(state["runtime_swapped"])
            marker = state["compensation_failed_target_live_recovery"]
            self.assertEqual(marker["status"], "failed")
            self.assertLessEqual(len(state["recovery_error"]), 1000)
            self.assertNotIn(secret, state["recovery_error"])

    def test_operator_compensation_failed_target_live_stale_runtime_replaced_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(source, session_id="stale-source", started_at=rc._utc_now()),
            )
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                rc.recover_operator_compensation_failed_target_live(
                    source,
                    target,
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=expected_sha,
                    expected_predecessor_sha256=expected_sha,
                    correlation_id="corr-target-live",
                    state_root=root,
                    reason="operator-reviewed",
                    backend=_OperatorBackend(),
                )

            replace.assert_called_once()
            runtime_state = json.loads((root / "recovery" / "runtime-controller" / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["harness"], "claude")
            self.assertEqual(runtime_state["profile"], "primary")

    def test_operator_compensation_failed_target_live_distinct_tmux_source_present_fails_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            target = SimpleNamespace(
                **{
                    **vars(target),
                    "controller": RuntimeControllerConfig(
                        tmux=TmuxTarget("pantheon-recovery-target", "harness", "0"),
                        policy=target.controller.policy,
                        post_start=target.controller.post_start,
                    ),
                }
            )
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace, mock.patch.object(rc, "_prompt_successor") as prompt:
                with self.assertRaisesRegex(RuntimeError, "source runtime pane is live"):
                    rc.recover_operator_compensation_failed_target_live(
                        source,
                        target,
                        recovery_id="rec-target",
                        expected_transition_id="failed-target-live",
                        expected_transition_sha256=expected_sha,
                        expected_predecessor_sha256=expected_sha,
                        correlation_id="corr-target-live",
                        state_root=root,
                        reason="operator-reviewed",
                        backend=_OperatorBackend(),
                    )

            replace.assert_not_called()
            prompt.assert_not_called()

    def test_operator_compensation_failed_target_live_receiver_repair_requires_source_absent_then_target_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",), communications=_telegram_comms())
            target = _binding(root, harness="claude", profile="primary", communications=_telegram_comms())
            for proofs, pattern in (
                ({("codex", "absent"): "present"}, "receiver state is present"),
                ({("claude", "present"): "absent"}, "receiver state is absent"),
            ):
                with self.subTest(pattern=pattern):
                    transition_path = rc._transition_path("recovery", root)
                    rc._atomic_json(transition_path, _target_live_compensation_state())
                    expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
                    with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                        rc, "_default_tmux_server_pid", return_value=123
                    ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                        rc, "_replace_runtime", side_effect=_replace_and_record
                    ), mock.patch.object(rc, "_write_operator_handoff") as write_handoff, mock.patch.object(
                        rc, "_prompt_successor"
                    ) as prompt:
                        with self.assertRaisesRegex(RuntimeError, pattern):
                            rc.recover_operator_compensation_failed_target_live(
                                source,
                                target,
                                recovery_id="rec-target",
                                expected_transition_id="failed-target-live",
                                expected_transition_sha256=expected_sha,
                                expected_predecessor_sha256=expected_sha,
                                correlation_id="corr-target-live",
                                state_root=root,
                                reason="operator-reviewed",
                                backend=_OperatorBackend(proofs=proofs),
                            )
                    write_handoff.assert_not_called()
                    prompt.assert_not_called()

    def test_operator_compensation_failed_target_live_does_not_self_ack_and_completed_retry_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            source = persona_bringup.load_runtime_reference(manifest, primary, "codex/fallback")
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                receipt = persona_bringup.operator_compensation_failed_target_live_recovery(
                    manifest_path=manifest,
                    persona="recovery",
                    source="codex/fallback",
                    destination="claude/primary",
                    recovery_id="rec-target",
                    expected_transition_id="failed-target-live",
                    expected_transition_sha256=expected_sha,
                    expected_predecessor_sha256=expected_sha,
                    correlation_id="corr-target-live",
                    reason="operator-reviewed",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                    backend=_OperatorBackend(),
                )
            self.assertEqual(receipt.status, "pending")
            state = rc.transition_state("recovery", state_root=state_root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertFalse(state["successor_read_ack"])

            with mock.patch("builtins.print") as printed:
                code = proactive_swap_init.completion_gate_main(
                    stdin=io.StringIO(json.dumps({"ok": True, "receipt": dataclasses.asdict(receipt)})),
                    stdout=sys.stdout,
                    expected_correlation_id="corr-target-live",
                    expected_destination="claude/primary",
                )
            self.assertEqual(code, 1)
            self.assertIn("not completed", printed.call_args.args[0])

            rc.acknowledge_successor_read(
                "recovery",
                evidence=state["expected_successor_read_evidence"],
                state_root=state_root,
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True):
                completed = persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="claude/primary",
                    trigger="operator",
                    correlation_id="corr-target-live",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                    backend=_OperatorBackend(),
                )
            self.assertEqual(completed.status, "completed")

    def test_operator_compensation_failed_target_live_cli_returns_pending_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            transition_path = rc._transition_path("recovery", state_root)
            rc._atomic_json(transition_path, _target_live_compensation_state())
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"), mock.patch("builtins.print") as printed:
                code = proactive_swap_init.main(
                    [
                        "--recover-compensation-failed-target-live",
                        "--manifest",
                        str(manifest),
                        "--persona",
                        "recovery",
                        "--source",
                        "codex/fallback",
                        "--destination",
                        "claude/primary",
                        "--recovery-id",
                        "rec-target",
                        "--expect-transition-id",
                        "failed-target-live",
                        "--expect-transition-sha256",
                        expected_sha,
                        "--expect-predecessor-sha256",
                        expected_sha,
                        "--correlation-id",
                        "corr-target-live",
                        "--reason",
                        "operator-reviewed",
                        "--state-root",
                        str(state_root),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(printed.call_args.args[0])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["receipt"]["status"], "pending")
            self.assertEqual(payload["receipt"]["destination"], "claude/primary")

    def test_operator_compensation_failed_target_live_cli_requires_predecessor_sha(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit):
                proactive_swap_init.main(
                    [
                        "--recover-compensation-failed-target-live",
                        "--persona",
                        "recovery",
                        "--source",
                        "codex/fallback",
                        "--destination",
                        "claude/primary",
                        "--recovery-id",
                        "rec-target",
                        "--expect-transition-id",
                        "failed-target-live",
                        "--expect-transition-sha256",
                        "0" * 64,
                        "--correlation-id",
                        "corr-target-live",
                        "--reason",
                        "operator-reviewed",
                    ]
                )
        self.assertIn("--expect-predecessor-sha256", stderr.getvalue())

    def test_operator_compensation_recovery_digest_mismatch_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(
                transition_path,
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "failed-t1",
                    "phase": "compensation-failed",
                    "trigger": "operator",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                },
            )
            before = transition_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(rc.RuntimeTransitionPending, "digest mismatch"):
                rc.recover_operator_compensation_failed(
                    current,
                    target,
                    recovery_id="rec-2",
                    expected_transition_id="failed-t1",
                    expected_transition_sha256="0" * 64,
                    state_root=root,
                    reason="operator-reviewed",
                )

            self.assertEqual(transition_path.read_text(encoding="utf-8"), before)
            self.assertFalse((root / "recovery" / "runtime-controller" / "operator-recovery-rec-2.json").exists())
            self.assertFalse((root / "recovery" / "runtime-controller" / "controller-evidence.jsonl").exists())

    def test_operator_compensation_recovery_rejects_archive_body_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="source", started_at=rc._utc_now()),
            )
            transition_path = rc._transition_path("recovery", root)
            failed = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-collision",
                "phase": "compensation-failed",
                "trigger": "operator",
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
                "compensation_error": "RuntimeError: restore failed",
            }
            rc._atomic_json(transition_path, failed)
            expected_sha = hashlib.sha256(transition_path.read_bytes()).hexdigest()
            archive = root / "recovery" / "runtime-controller" / "operator-recovery-rec-collision.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-runtime-operator-compensation-recovery-archive/v1",
                        "persona": "recovery",
                        "recovery_id": "rec-collision",
                        "predecessor_sha256": expected_sha,
                        "predecessor_transition": {
                            **failed,
                            "compensation_error": "RuntimeError: different failure",
                        },
                        "proofs": {},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            before = transition_path.read_text(encoding="utf-8")

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=123
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "existing recovery archive"):
                    rc.recover_operator_compensation_failed(
                        current,
                        target,
                        recovery_id="rec-collision",
                        expected_transition_id="failed-collision",
                        expected_transition_sha256=expected_sha,
                        state_root=root,
                        reason="operator-reviewed",
                    )

            self.assertEqual(transition_path.read_text(encoding="utf-8"), before)
            self.assertEqual(rc.transition_state("recovery", state_root=root)["phase"], "compensation-failed")
            self.assertFalse((root / "recovery" / "runtime-controller" / "controller-evidence.jsonl").exists())

    def test_operator_unresolved_mutation_with_different_transition_remains_barred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "t1",
                    "phase": "preparing",
                    "trigger": "operator",
                    "correlation_id": "corr-existing",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "source_transition_id": "",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                    "runtime_swapped": False,
                    "receiver_pre_done": True,
                    "runtime_mutation": {"status": "started-unverified", "pane_may_be_mutated": True},
                },
            )
            with mock.patch.object(rc, "_replace_runtime") as replace:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-other",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            replace.assert_not_called()

    def test_operator_target_swap_is_idempotent_by_correlation_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()),
            )
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "t1",
                    "phase": "awaiting-successor-read",
                    "trigger": "operator",
                    "correlation_id": "corr-1",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "source_transition_id": "",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                    "runtime_swapped": True,
                    "receiver_transferred": True,
                    "chronicle_evidence": str(root / "operator-handoff-t1.md"),
                    "expected_successor_read_evidence": "hook:runtime-transfer:t1:operator-handoff-t1.md",
                },
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(rc, "_replace_runtime") as replace:
                receipt = rc._operator_target_swap_resolved(
                    current,
                    target,
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=root,
                    backend=_OperatorBackend(),
                )
            self.assertEqual(receipt.status, "pending")
            replace.assert_not_called()
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc._operator_target_swap_resolved(
                    current,
                    target,
                    trigger="operator",
                    correlation_id="other",
                    state_root=root,
                    backend=_OperatorBackend(),
                )

    def test_operator_target_swap_retry_validates_live_runtime_and_receiver_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            state = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "t1",
                "phase": "awaiting-successor-read",
                "trigger": "operator",
                "correlation_id": "corr-1",
                "destination": "codex/fallback",
                "source_runtime": "claude/primary",
                "source_transition_id": "",
                "outgoing_harness": "claude",
                "outgoing_profile": "primary",
                "incoming_harness": "codex",
                "incoming_profile": "fallback",
                "runtime_swapped": True,
                "receiver_transferred": True,
                "chronicle_evidence": str(root / "operator-handoff-t1.md"),
                "expected_successor_read_evidence": "hook:runtime-transfer:t1:operator-handoff-t1.md",
            }
            rc._atomic_json(rc._transition_path("recovery", root), state)
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "recorded target runtime"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "pane is not live"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(ambiguous=True),
                    )

    def test_operator_target_swap_preserves_source_runtime_across_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",), communications=_telegram_comms())
            target = _binding(root, harness="codex", profile="fallback", communications=_telegram_comms())
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "transition_id": "t1",
                    "phase": "preparing",
                    "trigger": "operator",
                    "correlation_id": "corr-1",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "source_transition_id": "source-t1",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                    "runtime_swapped": False,
                    "receiver_pre_done": False,
                    "receiver_post_done": False,
                },
            )
            backend = _OperatorBackend()
            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record) as replace, mock.patch.object(
                rc, "_prompt_successor"
            ):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=backend,
                    )
            replace.assert_called_once()
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["source_transition_id"], "source-t1")
            self.assertTrue(state["receiver_pre_done"])
            self.assertTrue(state["runtime_swapped"])
            self.assertEqual(state["phase"], "awaiting-successor-read")

    def test_operator_target_swap_retries_after_preparing_written_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_append_controller_evidence", side_effect=RuntimeError("crash-before-handoff")
            ), self.assertRaisesRegex(RuntimeError, "crash-before-handoff"):
                rc._operator_target_swap_resolved(
                    current,
                    target,
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=root,
                    backend=backend,
                )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "preparing")
            self.assertFalse(state["receiver_pre_done"])
            self.assertFalse(backend.events)

            with mock.patch.object(rc, "_replace_runtime", side_effect=_replace_and_record) as replace, mock.patch.object(
                rc, "_prompt_successor"
            ):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=backend,
                    )
            replace.assert_called_once()
            state = rc.transition_state("recovery", state_root=root)
            self.assertTrue(state["runtime_swapped"])
            self.assertEqual(state["phase"], "awaiting-successor-read")

    def test_public_operator_target_swap_retries_after_pre_swap_crash_with_target_runtime_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_root = root / "state"
            primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
            target = persona_bringup.load_runtime_reference(manifest, primary, "codex/fallback")
            rc._atomic_json(
                state_root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(target, session_id="target", started_at=rc._utc_now()),
            )
            rc._atomic_json(
                rc._transition_path("recovery", state_root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "t1",
                    "phase": "preparing",
                    "trigger": "operator",
                    "correlation_id": "corr-1",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "source_transition_id": "",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                    "runtime_swapped": True,
                    "receiver_pre_done": True,
                    "receiver_post_done": False,
                    "receiver_transferred": False,
                    "successor_read_ack": False,
                },
            )
            backend = _OperatorBackend()
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime"
            ) as replace, mock.patch.object(rc, "_prompt_successor"):
                receipt = persona_bringup.operator_target_swap(
                    manifest_path=manifest,
                    persona="recovery",
                    destination="codex/fallback",
                    trigger="operator",
                    correlation_id="corr-1",
                    state_root=state_root,
                    mailbox_hub=root / "mail",
                    backend=backend,
                )
            replace.assert_not_called()
            self.assertEqual(receipt.status, "pending")
            self.assertIn("proof:recovery:claude:absent:absent", backend.events)
            self.assertLess(
                backend.events.index("proof:recovery:claude:absent:absent"),
                backend.events.index("recovery:start-codex-poller"),
            )
            self.assertNotIn("recovery:disable-claude-receiver", backend.events)
            state = rc.transition_state("recovery", state_root=state_root)
            self.assertTrue(state["receiver_post_done"])
            self.assertEqual(state["source_runtime"], "claude/primary")

            state["phase"] = "preparing"
            state["receiver_post_done"] = False
            state["receiver_transferred"] = False
            rc._atomic_json(rc._transition_path("recovery", state_root), state)
            with mock.patch.object(rc, "pane_exists", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                    persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="codex/fallback",
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=state_root,
                        mailbox_hub=root / "mail",
                        backend=_OperatorBackend(proofs={("claude", "absent"): "ambiguous"}),
                    )

    def test_operator_target_swap_does_not_mutate_cap_breaker_or_reset_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            seeded = {
                "phase": "completed",
                "trigger": "forced-cap",
                "transition_id": "forced-t1",
                "breaker_open": True,
                "capped_profile": "primary",
                "primary_resets_at": "2026-08-20T00:00:00Z",
                "quota_evidence": {"provider": "claude", "cap": "true"},
            }
            rc._atomic_json(rc._transition_path("recovery", root), seeded)
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["trigger"], "operator")
            self.assertEqual(state["source_transition_id"], "forced-t1")
            self.assertTrue(state["breaker_open"])
            self.assertEqual(state["capped_profile"], "primary")
            self.assertEqual(state["primary_resets_at"], "2026-08-20T00:00:00Z")
            self.assertEqual(state["quota_evidence"], seeded["quota_evidence"])

    def test_operator_target_swap_to_capped_target_makes_no_response_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="codex", profile="fallback", fallbacks=("claude/primary",))
            target = _binding(root, harness="claude", profile="primary")
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                rc._runtime_payload(current, session_id="s1", started_at=rc._utc_now()),
            )
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=_replace_and_record
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        current,
                        target,
                        trigger="operator",
                        correlation_id="corr-1",
                        state_root=root,
                        backend=_OperatorBackend(),
                    )
            state = rc.transition_state("recovery", state_root=root)
            self.assertEqual(state["phase"], "awaiting-successor-read")
            self.assertNotIn("response_claimed", state)
            self.assertFalse(state.get("successor_read_ack", False))

    def test_quota_policy_may_independently_request_another_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            target = _binding(root, harness="codex", profile="fallback")
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "phase": "awaiting-successor-read",
                    "trigger": "operator",
                    "correlation_id": "operator-1",
                    "destination": "codex/fallback",
                    "source_runtime": "claude/primary",
                    "outgoing_harness": "claude",
                    "outgoing_profile": "primary",
                    "incoming_harness": "codex",
                    "incoming_profile": "fallback",
                },
            )
            result = rc.begin_planned_transition(
                "recovery",
                outgoing_harness=current.launch.harness,
                outgoing_profile=current.launch.profile,
                incoming_harness=target.launch.harness,
                incoming_profile=target.launch.profile,
                message_id="m1",
                primary_resets_at=None,
                quota_evidence={},
                state_root=root,
            )
            self.assertEqual(result.status, "conflict")
            self.assertEqual(rc.transition_state("recovery", state_root=root)["trigger"], "operator")

    def test_controller_core_has_no_specialist_recovery_or_claude_codex_pair_conditionals(self) -> None:
        text = (REPO_ROOT / "tools" / "runtime_controller.py").read_text(encoding="utf-8")
        self.assertNotIn('("specialist", "claude-research")', text)
        self.assertNotIn('("recovery", "claude-fallback")', text)
        self.assertNotIn('outgoing == "claude" and incoming == "codex"', text)
        self.assertNotIn('outgoing == "codex" and incoming == "claude"', text)
        for action in (
            "snapshot-watchdog-units",
            "restore-watchdog-units",
            "disable-watchdog-timer",
            "stop-watchdog-service",
            "disable-dormant-claude-receiver",
            "stop-dormant-claude-receiver",
            "probe-claude-present",
            "probe-claude-absent",
            "start-codex-poller",
            "start-claude-receiver",
        ):
            with self.subTest(action=action):
                self.assertNotIn(action, text)

    def test_manifest_declared_third_harness_uses_generic_adapter_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _synthetic_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            fake = _SyntheticIntegration("synthetic", communications_managed=False)
            with harness_integration.registered_integration(fake):
                current, target = persona_bringup.resolve_configured_runtime_target(
                    manifest,
                    persona="recovery",
                    destination="synthetic/fallback",
                    state_root=root / "state",
                    mailbox_hub=root / "mail",
                )
                adapter = persona_bringup.build_adapter(target, harness_home=root)
                self.assertEqual(adapter.binding.launch.harness, "synthetic")
                proof = fake.prove_runtime_readiness(target, rc.RuntimeHandle(target.launch, target.controller.tmux, None, rc._utc_now()), state_root=root, context=None)
                self.assertTrue(proof.ready)
                self.assertEqual(current.launch.harness, "codex")
                self.assertEqual(target.launch.harness, "synthetic")
                self.assertEqual(rc._receiver_direction_for_bindings(current, target), "none")
                contract = fake.successor_evidence_contract("recovery", "t1", root / "handoff.md")
                fake.validate_successor_ack({"expected_successor_read_evidence": contract.expected}, contract.expected)
                rc._atomic_json(
                    root / "state" / "recovery" / "runtime-controller" / "runtime.json",
                    rc._runtime_payload(current, session_id="current", started_at=rc._utc_now()),
                )
                with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                    rc, "_replace_runtime", side_effect=_replace_and_record
                ), mock.patch.object(rc, "_prompt_successor"):
                    receipt = persona_bringup.operator_target_swap(
                        manifest_path=manifest,
                        persona="recovery",
                        destination="synthetic/fallback",
                        trigger="operator",
                        correlation_id="synthetic-corr",
                        state_root=root / "state",
                        mailbox_hub=root / "mail",
                    )
                self.assertEqual(receipt.status, "pending")
                self.assertEqual(fake.events, [])
                state = rc.transition_state("recovery", state_root=root / "state")
                self.assertEqual(state["incoming_harness"], "synthetic")
                self.assertFalse(state["receiver_transferred"])

    def test_automatic_quota_reset_restore_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = _binding(root, harness="codex", profile="fallback")
            primary = _binding(root, harness="claude", profile="primary")
            mailbox.provision(fallback.mailbox)
            current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "session-fallback", rc._utc_now())
            recovery = {"primary_profile": "claude/primary", "reset_passed_at": "2026-08-03T12:00:00Z"}
            with mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    current,
                    Path(state_root),
                ),
            ), mock.patch.object(
                rc, "planned_swap", side_effect=rc.RuntimeTransitionPending("handoff")
            ) as planned, _provenance_patch():
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        fallback,
                        adapter=_Adapter(fallback),
                        primary_binding=primary,
                        recovery_evidence=recovery,
                        state_dir=root,
                        cwd=root,
                    )
            self.assertEqual(planned.call_args.kwargs["trigger"], "quota-reset")


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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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
                rc, "pane_exists", side_effect=(False, True)
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
                rc, "pane_exists", return_value=False
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
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
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
            with mock.patch.object(rc, "pane_exists") as pane, mock.patch.object(
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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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

    def test_existing_server_respawn_injects_scope_path_without_shell(self) -> None:
        account_home = Path("/tmp/t2950-account")
        binding = _binding(account_home, harness="claude")
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = CommandSpec(
            argv=("/usr/bin/claude", "--model", "value with space"),
            stdin=None,
            cwd=Path("/tmp/cwd with space"),
            env={"PANTHEON_TEST": "env value with space"},
        )
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ) as session_exists, mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed) as run:
            rc._start_runtime(binding, adapter, replace=False)

        session_exists.assert_not_called()
        argv = run.call_args.args
        self.assertEqual(argv[:6], ("respawn-pane", "-k", "-t", "pantheon-recovery:harness.0", "-c", "/tmp/cwd with space"))
        self.assertEqual(
            argv[-7:],
            (
                "/usr/bin/env",
                "--",
                "PATH=/tmp/t2950-account/.bun/bin:/tmp/t2950-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
                "PANTHEON_TEST=env value with space",
                "/usr/bin/claude",
                "--model",
                "value with space",
            ),
        )

    def test_replace_runtime_respawn_injects_scope_path(self) -> None:
        account_home = Path("/tmp/t2950-replace-account")
        binding = _binding(account_home, harness="claude")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed) as run:
            rc._start_runtime(binding, _Adapter(binding), replace=True)

        argv = run.call_args.args
        self.assertEqual(argv[:6], ("respawn-pane", "-k", "-t", "pantheon-recovery:harness.0", "-c", "/tmp/t2950-replace-account"))
        self.assertEqual(argv[6:9], ("/usr/bin/env", "--", "PATH=/tmp/t2950-replace-account/.bun/bin:/tmp/t2950-replace-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))

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

    def test_existing_scoped_server_new_session_injects_scope_path_without_second_scope(self) -> None:
        account_home = Path("/tmp/t2950-new-session-account")
        binding = _binding(account_home, harness="claude")
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
        ) as scoped_start:
            started = rc._start_runtime(binding, _Adapter(binding), replace=False)

        self.assertFalse(started.created_scope)
        argv = tmux.call_args.args
        self.assertEqual(argv[:8], ("new-session", "-d", "-s", "pantheon-recovery", "-n", "harness", "-c", "/tmp/t2950-new-session-account"))
        self.assertEqual(argv[8:11], ("/usr/bin/env", "--", "PATH=/tmp/t2950-new-session-account/.bun/bin:/tmp/t2950-new-session-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))
        scoped_start.assert_not_called()

    def test_existing_scoped_server_new_window_injects_scope_path(self) -> None:
        account_home = Path("/tmp/t2950-new-window-account")
        binding = _binding(account_home, harness="claude")
        completed = subprocess.CompletedProcess(("tmux",), 0, "", "")
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(
            rc, "_target_known", return_value=False
        ), mock.patch.object(
            rc, "_session_exists", return_value=True
        ), mock.patch.object(
            rc, "_window_exists", return_value=False
        ), mock.patch.object(rc, "_run_tmux", return_value=completed) as tmux:
            started = rc._start_runtime(binding, _Adapter(binding), replace=False)

        self.assertFalse(started.created_scope)
        argv = tmux.call_args.args
        self.assertEqual(argv[:8], ("new-window", "-d", "-t", "pantheon-recovery", "-n", "harness", "-c", "/tmp/t2950-new-window-account"))
        self.assertEqual(argv[8:11], ("/usr/bin/env", "--", "PATH=/tmp/t2950-new-window-account/.bun/bin:/tmp/t2950-new-window-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))

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

    def test_runtime_command_argv_inherits_scope_path_in_child_process(self) -> None:
        argv = rc._runtime_command_argv(
            (
                "/usr/bin/python3",
                "-c",
                "import os; print(os.environ['PATH']); print(os.environ['PANTHEON_TEST'])",
            ),
            rc._runtime_env_with_scope_path(
                {"PANTHEON_TEST": "value with space"},
                account_home=Path("/tmp/t2950-real-child"),
            ),
        )
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "/tmp/t2950-real-child/.bun/bin:/tmp/t2950-real-child/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
                "value with space",
            ],
        )

    def test_existing_server_rejects_conflicting_adapter_path_before_tmux_mutation(self) -> None:
        binding = _binding(Path("/tmp/t2950-conflict"), harness="claude")
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = CommandSpec(
            argv=("/usr/bin/claude",),
            stdin=None,
            cwd=Path("/tmp/t2950-conflict"),
            env={"PATH": "/unsafe/bin"},
        )
        with mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(
            rc, "_target_known"
        ) as target_known, mock.patch.object(
            rc, "_run_tmux"
        ) as tmux, mock.patch.object(
            rc, "_start_new_server_in_scope"
        ) as scoped_start:
            with self.assertRaisesRegex(RuntimeError, "PATH conflicts"):
                rc._start_runtime(binding, adapter, replace=False)

        target_known.assert_not_called()
        tmux.assert_not_called()
        scoped_start.assert_not_called()

    def test_no_server_rejects_conflicting_adapter_path_before_scope_mutation(self) -> None:
        binding = _binding(Path("/tmp/t2950-no-server-conflict"), harness="claude")
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = CommandSpec(
            argv=("/usr/bin/claude",),
            stdin=None,
            cwd=Path("/tmp/t2950-no-server-conflict"),
            env={"PATH": "/unsafe/bin"},
        )
        with mock.patch.object(
            rc, "_default_tmux_server_pid"
        ) as server_pid, mock.patch.object(
            rc, "_prepare_runtime_scope"
        ) as prepare, mock.patch.object(
            rc, "_start_new_server_in_scope"
        ) as scoped_start, mock.patch.object(
            rc, "_run_tmux"
        ) as tmux:
            with self.assertRaisesRegex(RuntimeError, "PATH conflicts"):
                rc._start_runtime(binding, adapter, replace=False)

        server_pid.assert_not_called()
        prepare.assert_not_called()
        scoped_start.assert_not_called()
        tmux.assert_not_called()

    def test_codex_conflicting_adapter_path_fails_before_config_seeding(self) -> None:
        binding = _binding(Path("/tmp/t2950-codex-conflict"), harness="codex")
        adapter = mock.Mock()
        adapter.build_interactive_launch.return_value = CommandSpec(
            argv=("/usr/bin/codex",),
            stdin=None,
            cwd=Path("/tmp/t2950-codex-conflict"),
            env={"PATH": "/unsafe/bin"},
        )
        with mock.patch.object(
            rc, "ensure_project_trusted"
        ) as trusted, mock.patch.object(
            rc, "ensure_update_check_disabled"
        ) as update, mock.patch.object(
            rc, "ensure_model_migration_prompts_hidden"
        ) as migration, mock.patch.object(
            rc, "_default_tmux_server_pid"
        ) as server_pid, mock.patch.object(
            rc, "_prepare_runtime_scope"
        ) as prepare, mock.patch.object(
            rc, "_start_new_server_in_scope"
        ) as scoped_start:
            with self.assertRaisesRegex(RuntimeError, "PATH conflicts"):
                rc._start_runtime(binding, adapter, replace=False)

        trusted.assert_not_called()
        update.assert_not_called()
        migration.assert_not_called()
        server_pid.assert_not_called()
        prepare.assert_not_called()
        scoped_start.assert_not_called()

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
            with mock.patch.object(rc, "pane_exists", return_value=True):
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
                rc, "pane_exists", return_value=True
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
            with mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    handle,
                    Path(state_root),
                ),
            ), mock.patch.object(
                rc, "submit_to_runtime", return_value=receipt
            ) as submit, mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=True
            ), _provenance_patch():
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
            with mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    handle,
                    Path(state_root),
                ),
            ), mock.patch.object(
                rc, "submit_to_runtime"
            ) as submit, mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=False
            ), _provenance_patch():
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
            fallback_handle = rc.RuntimeHandle(
                fallback.launch,
                fallback.controller.tmux,
                "session-fallback",
                rc._utc_now(),
            )
            receipt = DeliveryReceipt(
                harness="codex",
                correlation_id="c1",
                state="accepted",
                mechanism="tmux-submit-verify",
                retryable=False,
                evidence={"acked": "false"},
            )
            with mock.patch.object(
                rc, "forced_swap", side_effect=AssertionError("forced swap must not run")
            ) as forced, mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    fallback_handle,
                    Path(state_root),
                ),
            ) as ensure, mock.patch.object(
                persona_bringup, "build_adapter", return_value=_Adapter(fallback)
            ), mock.patch.object(
                rc, "submit_to_runtime", return_value=receipt
            ), mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=True
            ), _provenance_patch():
                result = wake_persistent_runtime(
                    binding,
                    adapter=_Adapter(binding, snapshot),
                    fallback_binding=fallback,
                    state_dir=root / "state",
                    cwd=root,
                )

            forced.assert_not_called()
            ensure.assert_called_once()
            self.assertIs(ensure.call_args.args[0], fallback)
            self.assertEqual(result["harness"], "codex")
            self.assertEqual(result["delivery"].correlation_id, "c1")
            self.assertTrue((binding.mailbox / "new" / message_id).is_file())

    def test_quota_check_95_used_initiates_one_planned_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            mailbox.provision(binding.mailbox)
            probe = _quota_probe_result(binding, root / "state")
            snapshot = _quota_snapshot(
                binding,
                root,
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": probe.statusline_path,
                    "sha256": str(probe.statusline_sha256),
                    "session_id": probe.session_id,
                },
            )
            current = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            with mock.patch.object(rc, "ensure_runtime") as ensure, mock.patch.object(
                rc, "planned_swap"
            ) as planned:
                result = persona_bringup.quota_check(
                    binding,
                    adapter=_Adapter(binding, snapshot),
                    fallback_binding=fallback,
                    primary_binding=binding,
                    state_dir=root / "state",
                    cwd=root,
                    now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                    claude_probe_runner=lambda **_: probe,
                    manifest_path=REPO_ROOT / "instance.yaml",
                )

            self.assertEqual(result.decision, "planned-transition-started")
            self.assertTrue(result.acted)
            ensure.assert_not_called()
            planned.assert_not_called()
            state = rc.transition_state("recovery", state_root=root / "state")
            self.assertEqual(state["phase"], "preparing")
            self.assertEqual(state["trigger"], "planned-quota")
            self.assertEqual(state["primary_resets_at"], "2026-08-05T00:00:00Z")

            repeat = persona_bringup.quota_check(
                binding,
                adapter=_Adapter(binding, snapshot),
                fallback_binding=fallback,
                primary_binding=binding,
                state_dir=root / "state",
                cwd=root,
                now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                claude_probe_runner=lambda **_: probe,
                manifest_path=REPO_ROOT / "instance.yaml",
            )
            self.assertEqual(repeat.decision, "transition-active-noop")
            self.assertEqual(
                state["transition_id"],
                rc.transition_state("recovery", state_root=root / "state")["transition_id"],
            )

            with mock.patch.object(
                rc, "ensure_runtime", return_value=current
            ) as ensure, mock.patch.object(
                rc, "planned_swap", side_effect=rc.RuntimeTransitionPending("handoff")
            ) as planned:
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(binding, _quota_snapshot(binding, root, remaining=80.0)),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )

            ensure.assert_called_once()
            planned.assert_called_once()

    def test_quota_check_949_used_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            probe = _quota_probe_result(binding, root / "state")
            snapshot = _quota_snapshot(
                binding,
                root,
                used=94.9,
                remaining=5.1,
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": probe.statusline_path,
                    "sha256": str(probe.statusline_sha256),
                    "session_id": probe.session_id,
                },
            )

            result = persona_bringup.quota_check(
                binding,
                adapter=_Adapter(binding, snapshot),
                fallback_binding=fallback,
                primary_binding=binding,
                state_dir=root / "state",
                cwd=root,
                now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                claude_probe_runner=lambda **_: probe,
                manifest_path=REPO_ROOT / "instance.yaml",
            )

            self.assertEqual(result.decision, "stay")
            self.assertFalse(result.acted)
            self.assertEqual(rc.transition_state("recovery", state_root=root / "state"), {})

    def test_quota_check_unknown_evidence_no_actuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            probe = _quota_probe_result(binding, root / "state")
            cases = (
                runtime_quota.QuotaSnapshot(
                    profile="claude/primary",
                    windows=(),
                    observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                    freshness="fresh",
                    evidence={
                        "path": probe.statusline_path,
                        "sha256": str(probe.statusline_sha256),
                        "session_id": probe.session_id,
                    },
                ),
                _quota_snapshot(
                    binding,
                    root,
                    freshness="stale",
                    evidence={
                        "path": probe.statusline_path,
                        "sha256": str(probe.statusline_sha256),
                        "session_id": probe.session_id,
                    },
                ),
                runtime_quota.QuotaSnapshot(
                    profile="claude/primary",
                    windows=(),
                    observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                    freshness="unavailable",
                    evidence={"reason": "malformed"},
                ),
            )
            for snapshot in cases:
                with self.subTest(freshness=snapshot.freshness, windows=snapshot.windows):
                    result = persona_bringup.quota_check(
                        binding,
                        adapter=_Adapter(binding, snapshot),
                        fallback_binding=fallback,
                        primary_binding=binding,
                        state_dir=root / "state",
                        cwd=root,
                        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                        claude_probe_runner=lambda **_: probe,
                        manifest_path=REPO_ROOT / "instance.yaml",
                    )
                    self.assertEqual(result.decision, "unknown")
                    self.assertFalse(result.acted)
                    self.assertEqual(rc.transition_state("recovery", state_root=root / "state"), {})

    def test_quota_check_probe_failures_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            good_probe = _quota_probe_result(binding, root / "state")
            snapshot = _quota_snapshot(
                binding,
                root,
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": good_probe.statusline_path,
                    "sha256": str(good_probe.statusline_sha256),
                    "session_id": good_probe.session_id,
                },
            )
            probes = (
                _quota_probe_result(binding, root / "state", statusline_sha256="b" * 64),
                _quota_probe_result(binding, root / "state", pong=False),
                _quota_probe_result(binding, root / "state", cap="weekly limit"),
            )
            for probe in probes:
                with self.subTest(probe=probe):
                    result = persona_bringup.quota_check(
                        binding,
                        adapter=_Adapter(binding, snapshot),
                        fallback_binding=fallback,
                        primary_binding=binding,
                        state_dir=root / "state",
                        cwd=root,
                        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                        claude_probe_runner=lambda **_: probe,
                        manifest_path=REPO_ROOT / "instance.yaml",
                    )
                    self.assertEqual(result.decision, "unknown")
                    self.assertFalse(result.acted)
                    self.assertEqual(rc.transition_state("recovery", state_root=root / "state"), {})

    def test_quota_check_existing_transition_or_fallback_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            for phase in ("preparing", "awaiting-successor-read"):
                with self.subTest(phase=phase):
                    state_path = rc._transition_path("recovery", root / "state")
                    rc._atomic_json(
                        state_path,
                        {
                            "phase": phase,
                            "trigger": "planned-quota",
                            "transition_id": f"t-{phase}",
                        },
                    )
                    result = persona_bringup.quota_check(
                        binding,
                        adapter=_Adapter(binding),
                        fallback_binding=fallback,
                        primary_binding=binding,
                        state_dir=root / "state",
                        cwd=root,
                        manifest_path=REPO_ROOT / "instance.yaml",
                    )
                    self.assertEqual(result.decision, "transition-active-noop")
            rc._atomic_json(
                rc._transition_path("recovery", root / "state"),
                {
                    "phase": "completed",
                    "trigger": "planned-quota",
                    "transition_id": "t-completed",
                    "fallback_active": True,
                },
            )
            result = persona_bringup.quota_check(
                binding,
                adapter=_Adapter(binding),
                fallback_binding=fallback,
                primary_binding=binding,
                state_dir=root / "state",
                cwd=root,
                manifest_path=REPO_ROOT / "instance.yaml",
            )
            self.assertEqual(result.decision, "fallback-active-noop")

    def test_quota_check_serializes_with_consume_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(
                root,
                harness="claude",
                profile="primary",
                fallbacks=("codex/fallback",),
            )
            fallback = _binding(root, harness="codex", profile="fallback")
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(binding.mailbox, {"message": "work"}, "tester")
            probe = _quota_probe_result(binding, root / "state")
            snapshot = _quota_snapshot(
                binding,
                root,
                evidence={
                    "authoritative_limit": "false",
                    "provider": "claude",
                    "path": probe.statusline_path,
                    "sha256": str(probe.statusline_sha256),
                    "session_id": probe.session_id,
                },
            )
            current = rc.RuntimeHandle(
                binding.launch,
                binding.controller.tmux,
                "session-1",
                rc._utc_now(),
            )
            wake_entered_runtime = threading.Event()
            release_wake = threading.Event()
            wake_errors: queue.Queue[BaseException] = queue.Queue()

            def blocking_ensure_runtime(selected, adapter, *, state_root=None):
                del adapter
                wake_entered_runtime.set()
                if not release_wake.wait(timeout=5):
                    raise AssertionError("public wake did not receive release signal")
                return _record_handle_runtime_state(selected, current, Path(state_root))

            def run_public_wake() -> None:
                try:
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(
                            binding,
                            _quota_snapshot(binding, root, used=20.0, remaining=80.0),
                        ),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below.
                    wake_errors.put(exc)

            with mock.patch.object(
                rc, "ensure_runtime", side_effect=blocking_ensure_runtime
            ) as ensure, mock.patch(
                "tools.mailbox_wake.session_key", return_value="session-1"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=False
            ), _provenance_patch():
                wake_thread = threading.Thread(target=run_public_wake)
                wake_thread.start()
                try:
                    self.assertTrue(
                        wake_entered_runtime.wait(timeout=5),
                        "public wake did not enter runtime path",
                    )
                    with mock.patch.object(
                        sys,
                        "argv",
                        [
                            "persona_bringup.py",
                            "quota-check",
                            "--persona",
                            "recovery",
                            "--state-dir",
                            str(root / "state"),
                            "--mailbox-hub",
                            str(root / "mail"),
                        ],
                    ), mock.patch.object(
                        persona_bringup,
                        "build_adapter",
                        return_value=_Adapter(binding, snapshot),
                    ), mock.patch.object(
                        persona_bringup, "run_claude_quota_probe", lambda **_: probe
                    ), mock.patch.object(persona_bringup, "_print_json") as print_json:
                        code = persona_bringup.main()
                    self.assertEqual(code, 0)
                    busy = print_json.call_args.args[0]
                    self.assertEqual(busy.decision, "unknown")
                    self.assertEqual(busy.reason, "operation-busy")
                    self.assertEqual(
                        rc.transition_state("recovery", state_root=root / "state"),
                        {},
                    )
                finally:
                    release_wake.set()
                    wake_thread.join(timeout=5)
                self.assertFalse(wake_thread.is_alive(), "public wake thread hung")
                ensure.assert_called_once()
            if not wake_errors.empty():
                raise wake_errors.get()

            result = persona_bringup.quota_check(
                binding,
                adapter=_Adapter(binding, snapshot),
                fallback_binding=fallback,
                primary_binding=binding,
                state_dir=root / "state",
                cwd=root,
                now=datetime(2026, 8, 3, tzinfo=timezone.utc),
                claude_probe_runner=lambda **_: probe,
                manifest_path=REPO_ROOT / "instance.yaml",
            )
            self.assertEqual(result.decision, "planned-transition-started")
            self.assertEqual(
                rc.transition_state("recovery", state_root=root / "state")["message_id"],
                message_id,
            )

            with mock.patch.object(
                rc, "ensure_runtime", return_value=current
            ) as ensure, mock.patch.object(
                rc, "planned_swap", side_effect=rc.RuntimeTransitionPending("handoff")
            ) as planned, _provenance_patch():
                with self.assertRaises(rc.RuntimeTransitionPending):
                    wake_persistent_runtime(
                        binding,
                        adapter=_Adapter(
                            binding,
                            _quota_snapshot(binding, root, used=20.0, remaining=80.0),
                        ),
                        fallback_binding=fallback,
                        state_dir=root / "state",
                        cwd=root,
                    )
            ensure.assert_called_once()
            planned.assert_called_once()
            self.assertEqual(planned.call_args.kwargs["trigger"], "planned-quota")

    def test_persona_bringup_quota_check_cli_unsupported_orchestrator_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                (
                    sys.executable,
                    str(REPO_ROOT / "tools" / "persona_bringup.py"),
                    "quota-check",
                    "--persona",
                    "orchestrator",
                    "--state-dir",
                    str(root / "state"),
                    "--mailbox-hub",
                    str(root / "mail"),
                ),
                check=False,
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["supported"])
            self.assertEqual(payload["decision"], "unsupported")
            self.assertFalse((root / "state" / "orchestrator").exists())

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
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    current,
                    Path(state_root),
                ),
            ), mock.patch.object(
                rc,
                "planned_swap",
                side_effect=rc.RuntimeTransitionPending("return handoff"),
            ) as planned, _provenance_patch():
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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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
                rc, "pane_exists", side_effect=(False, False)
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
                rc, "pane_exists", side_effect=(False, True)
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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
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
            state["chronicle_evidence"] = str(evidence)
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

    def test_begin_transition_rejects_compensation_failed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "compensation_error": "RuntimeError: restore failed",
            }
            path = rc._transition_path("recovery", root)
            rc._atomic_json(path, existing)
            before = path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                rc._begin_transition(
                    "recovery",
                    trigger="planned-quota",
                    outgoing_profile="primary",
                    incoming_profile="fallback",
                    outgoing_harness="claude",
                    incoming_harness="codex",
                    drain_id="d1",
                    state_root=root,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_assert_drain_allowed_rejects_compensation_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._atomic_json(
                rc._transition_path("recovery", root),
                {
                    "schema": "pantheon-runtime-transition/v1",
                    "transition_id": "failed-t1",
                    "phase": "compensation-failed",
                    "compensation_error": "RuntimeError: restore failed",
                },
            )
            with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                rc.assert_drain_allowed("recovery", "d1", state_root=root)

    def test_planned_swap_over_compensation_failed_does_not_prompt_replace_or_overwrite(self) -> None:
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
            existing = {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "failed-t1",
                "phase": "compensation-failed",
                "trigger": "operator",
                "correlation_id": "old-corr",
                "destination": "codex/fallback",
                "compensation_error": "RuntimeError: restore failed",
            }
            path = rc._transition_path("recovery", root)
            rc._atomic_json(path, existing)
            before = path.read_text(encoding="utf-8")

            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "submit_to_runtime"
            ) as prompt:
                with self.assertRaisesRegex(rc.RuntimeTransitionPending, "manual recovery"):
                    rc.planned_swap(
                        current,
                        target,
                        message_id="m1",
                        drain_id="d1",
                        state_root=root,
                    )

            replace.assert_not_called()
            prompt.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), before)

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
                rc.chronicle, "archive_required", side_effect=archive_required
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
                rc.transition_state("recovery", state_root=root)["chronicle_evidence"],
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
            rc, "_existing_chronicle", return_value=evidence
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
            with mock.patch.object(rc, "_existing_chronicle", return_value=None), mock.patch.object(
                rc.chronicle,
                "archive_required",
                side_effect=rc.chronicle.ChronicleDurabilityError("fsync failed"),
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
                rc, "_existing_chronicle", return_value=None
            ), mock.patch.object(
                rc, "_replace_runtime", return_value=replacement
            ), mock.patch.object(rc, "_prompt_successor"), mock.patch.dict(
                rc.os.environ, {"PANTHEON_CHRONICLE_ROOT": str(root / "chronicle")}
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
                rc, "_existing_chronicle", return_value=evidence
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

    def test_successor_must_ack_chronicle_read_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "_existing_chronicle", return_value=evidence), mock.patch.object(
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
            self.assertEqual(receipt.chronicle_evidence, evidence)
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
            self.assertEqual(receipt.chronicle_evidence, evidence)

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
                rc, "_existing_chronicle", return_value=new_evidence
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
            with mock.patch.object(rc, "_existing_chronicle", return_value=evidence), mock.patch.object(
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

    def test_forced_path_uses_existing_chronicle_entry_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "existing.md"
            evidence.write_text("facts", encoding="utf-8")
            current, target, error = self._current_and_target(root)
            replacement = rc.RuntimeHandle(
                target.launch, target.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "_existing_chronicle", return_value=evidence), mock.patch.object(
                rc.chronicle, "archive_required"
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
                rc.transition_state("recovery", state_root=root)["chronicle_evidence"],
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
                {"PANTHEON_CHRONICLE_ROOT": str(root / "chronicle")},
            ), mock.patch.object(rc, "_existing_chronicle", return_value=None), mock.patch.object(
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
            body = Path(state["chronicle_evidence"]).read_text()
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
            fallback_handle = rc.RuntimeHandle(
                fallback.launch, fallback.controller.tmux, "s-fallback", rc._utc_now()
            )
            with mock.patch.object(
                rc, "recorded_runtime_handle", return_value=current
            ), mock.patch.object(
                rc, "forced_swap", side_effect=AssertionError("forced swap must not run")
            ) as forced, mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: _record_handle_runtime_state(
                    selected,
                    fallback_handle,
                    Path(state_root),
                ),
            ) as ensure, mock.patch.object(
                persona_bringup, "build_adapter", return_value=_Adapter(fallback)
            ), mock.patch.object(
                rc,
                "submit_to_runtime",
                return_value=DeliveryReceipt(
                    harness="codex",
                    correlation_id="correlation-1",
                    state="accepted",
                    mechanism="tmux-submit-verify",
                    retryable=False,
                    evidence={},
                ),
            ), mock.patch(
                "tools.mailbox_wake.session_key", return_value="1:2"
            ), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=True
            ), _provenance_patch():
                result = wake_persistent_runtime(
                    binding,
                    adapter=_Adapter(binding, snapshot),
                    fallback_binding=fallback,
                    state_dir=root / "state",
                    cwd=root,
                )

            forced.assert_not_called()
            ensure.assert_called_once()
            self.assertEqual(result["delivery"].correlation_id, "correlation-1")
            self.assertEqual(
                result["runtime_viability"].attempts[0].failure_class,
                "subscription-cap",
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
                rc, "_existing_chronicle", return_value=evidence
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
            with mock.patch.object(rc, "_existing_chronicle", return_value=None), mock.patch.object(
                rc.chronicle,
                "archive_required",
                side_effect=rc.chronicle.ChronicleDurabilityError("index fsync failed"),
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
                    rc.chronicle.ChronicleDurabilityError("snapshot/index failure")
                    if failure_point == "archive"
                    else mock.DEFAULT
                )
                with mock.patch.object(rc, "_existing_chronicle", return_value=existing), mock.patch.object(
                    rc.chronicle,
                    "archive_required",
                    side_effect=archive_error,
                ), mock.patch.object(
                    rc,
                    "_strict_receipt",
                    side_effect=(
                        rc.chronicle.ChronicleDurabilityError("receipt rename failure")
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
        self._assert_forwards_context(rc.pane_exists, (target,))

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
            rc, "pane_exists", return_value=True
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
            rc, "pane_exists", return_value=True
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
                rc.chronicle, "archive_required", return_value=archive
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
    (real tmux/systemd-run start) and `chronicle.archive_required` are
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
                rc.chronicle, "archive_required", return_value=archive
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
                rc.chronicle, "archive_required", return_value=archive
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


# ============================================================================
# T-2966 (T-2953 Phase 2 BT1) -- stateless restart-fixable controller tests
# ============================================================================


def _stateless_binding(root: Path, *, harness: str = "codex", profile: str = "interactive") -> SimpleNamespace:
    return _binding(root, harness=harness, profile=profile)


class DurableStateHelperTests(unittest.TestCase):
    def test_runtime_slug_uses_double_dash_separator(self) -> None:
        self.assertEqual(rc._runtime_slug("claude", "anthropic-code"), "claude--anthropic-code")

    def test_active_runtime_roundtrip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
            self.assertEqual(rc.read_active_runtime("orchestrator", state_root=root), {})
            rc.write_active_runtime("orchestrator", binding, handle, pid=4242, state_root=root)
            cached = rc.read_active_runtime("orchestrator", state_root=root)
            self.assertEqual(cached["schema"], "pantheon-persona-active-runtime/v1")
            self.assertEqual(cached["runtime"], "claude/anthropic-code")
            self.assertEqual(cached["pid"], 4242)
            rc.clear_active_runtime("orchestrator", state_root=root)
            self.assertEqual(rc.read_active_runtime("orchestrator", state_root=root), {})
            # clearing twice must not raise (spec: cleared on tmux-death observation)
            rc.clear_active_runtime("orchestrator", state_root=root)

    def test_last_attempt_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rc.load_last_attempt("orchestrator", "codex--interactive", state_root=Path(tmp)))

    def test_record_last_attempt_success_clears_failure_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root)
            now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
            rc.record_last_attempt(
                "orchestrator", binding, outcome="failed", failure_class="process_death",
                state_root=root, now=now,
            )
            payload = rc.record_last_attempt(
                "orchestrator", binding, outcome="success", state_root=root,
                now=now + timedelta(minutes=10),
            )
            self.assertEqual(payload["outcome"], "success")
            self.assertEqual(payload["consecutive_failures"], 0)
            self.assertIsNone(payload["cooldown_until"])
            self.assertIsNone(payload["failure_class"])

    def test_record_last_attempt_subscription_cap_cooldown_is_next_weekly_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
            payload = rc.record_last_attempt(
                "orchestrator", binding, outcome="failed", failure_class="subscription_cap",
                state_root=root, now=now,
            )
            expected = runtime_quota.next_weekly_reset("codex", now=now)
            self.assertEqual(payload["cooldown_until"], rc._iso(expected))
            self.assertEqual(payload["consecutive_failures"], 1)

    def test_record_last_attempt_process_death_backoff_ladder_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root)
            now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
            seen_deltas = []
            for _ in range(5):
                payload = rc.record_last_attempt(
                    "orchestrator", binding, outcome="failed", failure_class="process_death",
                    state_root=root, now=now,
                )
                until = datetime.fromisoformat(payload["cooldown_until"])
                seen_deltas.append((until - now).total_seconds())
            # 30s, 5min, 30min, 2h, 2h (ladder holds at the last rung)
            self.assertEqual(seen_deltas, [30.0, 300.0, 1800.0, 7200.0, 7200.0])

    def test_record_last_attempt_runtime_blocked_never_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root)
            payload = rc.record_last_attempt(
                "orchestrator", binding, outcome="failed", failure_class="runtime_blocked", state_root=root,
            )
            self.assertIsNone(payload["cooldown_until"])

    def test_load_holds_empty_when_file_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rc.load_holds("orchestrator", state_root=Path(tmp)), {})

    def test_load_holds_reads_written_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._atomic_json(
                rc._holds_path("orchestrator", state_root=root),
                {
                    "schema": "pantheon-persona-runtime-holds/v1",
                    "holds": {"codex/interactive-chatgpt": {"set_ts": "x", "set_by": "operator:operator", "reason": "r"}},
                },
            )
            holds = rc.load_holds("orchestrator", state_root=root)
            self.assertIn("codex/interactive-chatgpt", holds)

    def test_log_invocation_appends_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            when = datetime(2026, 8, 18, 4, 22, 0, tzinfo=timezone.utc)
            rc.log_invocation(
                "orchestrator", "started", selected="claude/anthropic-code",
                skipped=[{"runtime": "codex/interactive-chatgpt", "reason": "held"}],
                state_root=root, when=when,
            )
            path = rc._invocations_path("orchestrator", state_root=root, when=when)
            self.assertEqual(path.name, "2026-08-18.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["outcome"], "started")
            self.assertEqual(record["selected"], "claude/anthropic-code")
            self.assertEqual(record["skipped"][0]["reason"], "held")
            # T-2966 REBOUND finding #1: absent an explicit hash, the field
            # is present (schema-stable) but null -- never silently omitted.
            self.assertIn("instruction_sha256", record)
            self.assertIsNone(record["instruction_sha256"])

    def test_log_invocation_records_instruction_sha256_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            when = datetime(2026, 8, 18, 4, 22, 0, tzinfo=timezone.utc)
            rc.log_invocation(
                "orchestrator", "started", selected="claude/anthropic-code",
                instruction_sha256="a" * 64, state_root=root, when=when,
            )
            path = rc._invocations_path("orchestrator", state_root=root, when=when)
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["instruction_sha256"], "a" * 64)

    def test_instruction_sha256_matches_sha256_of_manifest_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "instance.yaml"
            manifest.write_text("persona: orchestrator\nruntime_binding: {}\n", encoding="utf-8")
            expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.assertEqual(rc._instruction_sha256(manifest), expected)
            # Changing the artifact changes the recorded hash -- proves this
            # is a real content hash, not a path-derived stand-in.
            manifest.write_text("persona: orchestrator\nruntime_binding: {other: true}\n", encoding="utf-8")
            self.assertNotEqual(rc._instruction_sha256(manifest), expected)


class SelectViableRuntimeTests(unittest.TestCase):
    def test_selects_first_candidate_with_no_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            selected, skipped = rc._select_viable_runtime(
                "orchestrator", [claude, codex], {}, rc._utc_now(), state_root=root
            )
            self.assertIs(selected, claude)
            self.assertEqual(skipped, [])

    def test_skips_held_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            holds = {"claude/anthropic-code": {"reason": "known-bad"}}
            selected, skipped = rc._select_viable_runtime(
                "orchestrator", [claude, codex], holds, rc._utc_now(), state_root=root
            )
            self.assertIs(selected, codex)
            self.assertEqual(skipped, [{"runtime": "claude/anthropic-code", "reason": "held"}])

    def test_skips_cooldown_active_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=now,
            )
            selected, skipped = rc._select_viable_runtime(
                "orchestrator", [claude, codex], {}, now, state_root=root
            )
            self.assertIs(selected, codex)
            self.assertEqual(len(skipped), 1)
            self.assertTrue(skipped[0]["reason"].startswith("cooldown-active-until-"))

    def test_no_viable_candidate_returns_none_with_full_skip_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now = rc._utc_now()
            for binding in (claude, codex):
                rc.record_last_attempt(
                    "orchestrator", binding, outcome="failed", failure_class="process_death",
                    state_root=root, now=now,
                )
            selected, skipped = rc._select_viable_runtime(
                "orchestrator", [claude, codex], {}, now, state_root=root
            )
            self.assertIsNone(selected)
            self.assertEqual(len(skipped), 2)

    def test_earliest_cooldown_expiry_ignores_held_runtimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death", state_root=root, now=now,
            )
            rc.record_last_attempt(
                "orchestrator", codex, outcome="failed", failure_class="process_death", state_root=root, now=now,
            )
            holds = {"claude/anthropic-code": {"reason": "x"}}
            earliest = rc._earliest_cooldown_expiry("orchestrator", [claude, codex], holds, state_root=root)
            # only codex's cooldown counts since claude is held
            codex_attempt = rc.load_last_attempt("orchestrator", rc._runtime_slug("codex", "interactive-chatgpt"), state_root=root)
            self.assertEqual(rc._iso(earliest), codex_attempt["cooldown_until"])


class ClassifyDeathTests(unittest.TestCase):
    def test_subscription_cap_failure_maps_to_subscription_cap(self) -> None:
        exc = rc.RuntimeOperationalFailure(
            "capped", failure_class="subscription-cap", cause="x", binding_runtime="codex/interactive-chatgpt",
        )
        self.assertEqual(rc._classify_death(exc), "subscription_cap")

    def test_operational_launch_failure_maps_to_operational_failure(self) -> None:
        exc = rc.RuntimeOperationalFailure(
            "launch failed", failure_class="operational-launch", cause="x", binding_runtime="claude/anthropic-code",
        )
        self.assertEqual(rc._classify_death(exc), "operational_failure")

    def test_operational_readiness_failure_maps_to_readiness_timeout(self) -> None:
        exc = rc.RuntimeOperationalFailure(
            "not ready", failure_class="operational-readiness", cause="x", binding_runtime="claude/anthropic-code",
        )
        self.assertEqual(rc._classify_death(exc), "readiness_timeout")

    def test_generic_exception_maps_to_process_death(self) -> None:
        self.assertEqual(rc._classify_death(RuntimeError("tmux vanished")), "process_death")

    def test_no_exception_maps_to_process_death(self) -> None:
        self.assertEqual(rc._classify_death(None), "process_death")


class RuntimeAliveTests(unittest.TestCase):
    """T-2966 REBOUND finding #2: `_runtime_alive`'s tmux + pid dual-authority
    cross-check (spec: 'Authority = `tmux has-session -t <session>` +
    `kill -0 <pid>`')."""

    def test_pane_process_pid_parses_tmux_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(
                rc, "_run_tmux",
                return_value=subprocess.CompletedProcess(args=(), returncode=0, stdout="4242\n", stderr=""),
            ):
                self.assertEqual(rc._pane_process_pid(binding.controller.tmux), 4242)

    def test_pane_process_pid_returns_none_on_tmux_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(
                rc, "_run_tmux",
                return_value=subprocess.CompletedProcess(args=(), returncode=1, stdout="", stderr="no such pane"),
            ):
                self.assertIsNone(rc._pane_process_pid(binding.controller.tmux))

    def test_pane_process_pid_returns_none_on_non_numeric_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(
                rc, "_run_tmux",
                return_value=subprocess.CompletedProcess(args=(), returncode=0, stdout="\n", stderr=""),
            ):
                self.assertIsNone(rc._pane_process_pid(binding.controller.tmux))

    def test_pid_alive_true_for_own_pid(self) -> None:
        self.assertTrue(rc._pid_alive(os.getpid()))

    def test_pid_alive_false_for_process_lookup_error(self) -> None:
        with mock.patch.object(rc.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(rc._pid_alive(999999))

    def test_pid_alive_true_for_permission_error(self) -> None:
        with mock.patch.object(rc.os, "kill", side_effect=PermissionError):
            self.assertTrue(rc._pid_alive(1))

    def test_runtime_alive_false_when_tmux_pane_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_session_exists", return_value=False), mock.patch.object(
                rc, "pane_exists", return_value=False
            ):
                self.assertFalse(rc._runtime_alive(binding))

    def test_runtime_alive_true_when_tmux_alive_and_no_pid_capturable(self) -> None:
        """No pid capturable (e.g. a launch shape without a resolvable pane
        pid) falls back to tmux-only liveness rather than reporting dead on
        an unrelated lookup failure."""
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_session_exists", return_value=True), mock.patch.object(
                rc, "pane_exists", return_value=True
            ), mock.patch.object(rc, "_pane_process_pid", return_value=None):
                self.assertTrue(rc._runtime_alive(binding))

    def test_runtime_alive_false_when_pane_alive_but_pid_dead(self) -> None:
        """THE regression this finding protects: tmux still reports the
        pane/session as present, but the pid behind it is gone (e.g. a
        future daemonizing/forking launch shape where pane-death no longer
        coincides with process-death). Pane-only liveness would wrongly say
        'alive' here; the pid cross-check is what catches it."""
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_session_exists", return_value=True), mock.patch.object(
                rc, "pane_exists", return_value=True
            ), mock.patch.object(rc, "_pane_process_pid", return_value=4242), mock.patch.object(
                rc, "_pid_alive", return_value=False
            ):
                self.assertFalse(rc._runtime_alive(binding))

    def test_runtime_alive_true_when_pane_and_pid_both_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_session_exists", return_value=True), mock.patch.object(
                rc, "pane_exists", return_value=True
            ), mock.patch.object(rc, "_pane_process_pid", return_value=4242), mock.patch.object(
                rc, "_pid_alive", return_value=True
            ):
                self.assertTrue(rc._runtime_alive(binding))

    def test_runtime_alive_requeries_pid_fresh_each_call_not_cached(self) -> None:
        """Two calls to `_runtime_alive` must each re-query the pid -- a
        respawned pane gets a new pid, and a cached/stale value would
        defeat the cross-check."""
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            pids = iter([111, 222])
            with mock.patch.object(rc, "_session_exists", return_value=True), mock.patch.object(
                rc, "pane_exists", return_value=True
            ), mock.patch.object(
                rc, "_pane_process_pid", side_effect=lambda *a, **k: next(pids)
            ) as pid_lookup, mock.patch.object(rc, "_pid_alive", return_value=True):
                rc._runtime_alive(binding)
                rc._runtime_alive(binding)
            self.assertEqual(pid_lookup.call_count, 2)


class ControllerStartRuntimeTests(unittest.TestCase):
    """T-2966 REBOUND finding #2: `_controller_start_runtime` captures the
    pane pid into the returned `RuntimeHandle` once the runtime is verified
    up + ready."""

    def test_captures_pane_pid_into_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            with mock.patch("tools.persona_bringup.build_adapter", return_value=mock.Mock()), \
                 mock.patch.object(rc, "_target_known", return_value=False), \
                 mock.patch.object(rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)), \
                 mock.patch.object(rc, "_verify_started_runtime"), \
                 mock.patch.object(rc, "_verify_runtime_input_ready"), \
                 mock.patch.object(rc, "_pane_process_pid", return_value=7777):
                _started, handle = rc._controller_start_runtime(binding, state_root=root)
            self.assertEqual(handle.pid, 7777)

    def test_pid_is_none_when_pane_pid_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            with mock.patch("tools.persona_bringup.build_adapter", return_value=mock.Mock()), \
                 mock.patch.object(rc, "_target_known", return_value=False), \
                 mock.patch.object(rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)), \
                 mock.patch.object(rc, "_verify_started_runtime"), \
                 mock.patch.object(rc, "_verify_runtime_input_ready"), \
                 mock.patch.object(rc, "_pane_process_pid", return_value=None):
                _started, handle = rc._controller_start_runtime(binding, state_root=root)
            self.assertIsNone(handle.pid)


class BlockUntilTmuxDeathTests(unittest.TestCase):
    def test_polls_until_alive_check_returns_false(self) -> None:
        calls = {"n": 0}

        def alive_check() -> bool:
            calls["n"] += 1
            return calls["n"] <= 3

        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            rc.block_until_tmux_death(
                binding, alive_check=alive_check, sleep=sleeps.append, poll_interval_seconds=1.5
            )
        self.assertEqual(calls["n"], 4)
        self.assertEqual(sleeps, [1.5, 1.5, 1.5])

    def test_returns_immediately_if_already_dead(self) -> None:
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            rc.block_until_tmux_death(binding, alive_check=lambda: False, sleep=sleeps.append)
        self.assertEqual(sleeps, [])


class ControllerMainCycleTests(unittest.TestCase):
    """Exercises `controller_main` via its injectable seams (`candidates`,
    `start_runtime_fn`, `block_until_death_fn`, `now`, `sleep`) -- no real
    tmux/systemd process is ever touched, matching the file's existing
    test-seam convention."""

    def _fake_start(self, binding) -> tuple:
        started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
        return started, handle

    def test_selects_starts_blocks_and_records_success_when_sustained_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            base = datetime(2026, 8, 18, 4, 0, 0, tzinfo=timezone.utc)
            clock = {"t": base}

            def now():
                return clock["t"]

            def block(binding):
                clock["t"] = base + timedelta(seconds=rc.SUSTAINED_ALIVE_SECONDS + 1)

            rc_result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=now, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block,
            )
            self.assertEqual(rc_result, 0)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "success")
            self.assertEqual(attempt["consecutive_failures"], 0)
            # active-runtime.json is cleared after the (observed) death
            self.assertEqual(rc.read_active_runtime("orchestrator", state_root=root), {})
            invocations = (root / "orchestrator" / "invocations").glob("*.jsonl")
            events = [json.loads(l)["outcome"] for f in invocations for l in f.read_text().splitlines()]
            self.assertEqual(events, ["started"])

    def test_started_invocation_records_instruction_sha256_of_manifest_read_this_cycle(self) -> None:
        """T-2966 REBOUND finding #1: ticket + spec both require the
        controller to record the SHA-256 of the instruction (manifest) it
        read into the invocation log -- no self-proof, one file, computed
        fresh every cycle (not the KEEP layer's RUNTIME_SOURCE_HASH_PATHS
        bundle, which hashes the controller's own source under a
        clean-worktree precondition -- a different, heavier concept)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "instance.yaml"
            manifest.write_text("persona: orchestrator\n", encoding="utf-8")
            expected_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")

            rc.controller_main(
                "orchestrator", candidates=[claude], manifest_path=manifest, state_root=root,
                sleep=lambda s: None, start_runtime_fn=self._fake_start,
                block_until_death_fn=lambda binding: None,
            )
            invocations = list((root / "orchestrator" / "invocations").glob("*.jsonl"))
            records = [json.loads(l) for f in invocations for l in f.read_text().splitlines()]
            self.assertEqual([r["outcome"] for r in records], ["started"])
            self.assertEqual(records[0]["instruction_sha256"], expected_hash)

    def test_no_viable_candidate_invocation_also_records_instruction_sha256(self) -> None:
        """Every cycle reads the manifest before it can even decide
        selected-vs-skipped -- the hash belongs on every log_invocation
        call, not only a successful start."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "instance.yaml"
            manifest.write_text("persona: orchestrator\n", encoding="utf-8")
            expected_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=rc._utc_now(),
            )

            rc.controller_main(
                "orchestrator", candidates=[claude], manifest_path=manifest, state_root=root,
                sleep=lambda s: None, max_reselect_cycles=1,
            )
            invocations = list((root / "orchestrator" / "invocations").glob("*.jsonl"))
            records = [json.loads(l) for f in invocations for l in f.read_text().splitlines()]
            self.assertEqual([r["outcome"] for r in records], ["no-viable-candidate"])
            self.assertEqual(records[0]["instruction_sha256"], expected_hash)

    def test_active_runtime_cache_pid_field_is_populated_not_always_null(self) -> None:
        """T-2966 REBOUND finding #2 side-benefit: the spec's example
        active-runtime.json payload shows a real pid ("pid": 1985639);
        before this fix `write_active_runtime`'s pid kwarg was never
        threaded through from controller_main, so the field was always
        null in production. Proves handle.pid flows end-to-end into the
        cache."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")

            def start_with_pid(binding):
                started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
                handle = rc.RuntimeHandle(
                    binding.launch, binding.controller.tmux, None, rc._utc_now(), pid=54321,
                )
                return started, handle

            captured = {}

            def block_and_capture(binding):
                captured["cache"] = rc.read_active_runtime("orchestrator", state_root=root)

            rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                sleep=lambda s: None, start_runtime_fn=start_with_pid,
                block_until_death_fn=block_and_capture,
            )
            self.assertEqual(captured["cache"]["pid"], 54321)

    def test_records_process_death_when_not_sustained_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            base = datetime(2026, 8, 18, 4, 0, 0, tzinfo=timezone.utc)
            clock = {"t": base}

            def now():
                return clock["t"]

            def block(binding):
                clock["t"] = base + timedelta(seconds=5)  # died almost immediately

            rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=now, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block,
            )
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "failed")
            self.assertEqual(attempt["failure_class"], "process_death")
            self.assertEqual(attempt["consecutive_failures"], 1)

    def test_start_failure_records_attempt_without_active_runtime_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")

            def failing_start(binding):
                raise rc.RuntimeOperationalFailure(
                    "capped at start", failure_class="subscription-cap",
                    cause="x", binding_runtime="codex/interactive-chatgpt",
                )

            result = rc.controller_main(
                "orchestrator", candidates=[codex], state_root=root,
                sleep=lambda s: None, start_runtime_fn=failing_start,
                block_until_death_fn=lambda binding: None,
            )
            self.assertEqual(result, 1)
            attempt = rc.load_last_attempt("orchestrator", "codex--interactive-chatgpt", state_root=root)
            self.assertEqual(attempt["outcome"], "failed")
            self.assertEqual(attempt["failure_class"], "subscription_cap")
            self.assertEqual(rc.read_active_runtime("orchestrator", state_root=root), {})

    def test_no_viable_candidate_sleeps_then_bounded_loop_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death", state_root=root, now=now,
            )
            sleeps: list[float] = []
            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=lambda: now, sleep=sleeps.append, max_reselect_cycles=1,
            )
            self.assertEqual(result, 1)
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(sleeps[0], 30.0)  # first process_death backoff rung

    def test_held_runtime_is_never_selected_even_off_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            rc._atomic_json(
                rc._holds_path("orchestrator", state_root=root),
                {"schema": "pantheon-persona-runtime-holds/v1", "holds": {"claude/anthropic-code": {"reason": "x"}}},
            )
            selected_holder = {}

            def capture_start(binding):
                selected_holder["binding"] = binding
                return self._fake_start(binding)

            rc.controller_main(
                "orchestrator", candidates=[claude, codex], state_root=root,
                sleep=lambda s: None, start_runtime_fn=capture_start,
                block_until_death_fn=lambda binding: None,
            )
            self.assertIs(selected_holder["binding"], codex)


class ORG068InvariantTests(unittest.TestCase):
    """Proves the ORG-068 SS85-88 acceptance bar (restart-fixable, stateless
    across sessions, externally legible) for the T-2966 stateless controller.
    """

    def _fake_start(self, binding) -> tuple:
        started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
        return started, handle

    def test_restart_fixable_kill_mid_flight_then_reinvoke_recovers_from_substrate(self) -> None:
        """SS87: 'if restarted without warning, it just recovers.' Simulate a
        controller_main invocation that starts claude then is killed before
        ever recording an outcome (the process died, no clean exit -- exactly
        what a `kill -9` on the controller's own PID looks like). A second,
        wholly separate `controller_main` call (fresh Python call, standing
        in for a fresh systemd-respawned process) must recover purely by
        re-reading the durable substrate: it sees claude's active-runtime.json
        cache is stale (no death was ever recorded), decides via a
        liveness re-check, and proceeds -- it does NOT require any in-memory
        carry-over from the killed invocation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")

            # Cycle 1: starts claude, then the process is "killed" -- simulated
            # by block_until_death_fn raising instead of returning, so
            # controller_main never reaches its own record/clear/return
            # tail. This is the exact "restarted without warning" shape.
            def killed(binding):
                raise SystemExit("simulated kill -9")

            with self.assertRaises(SystemExit):
                rc.controller_main(
                    "orchestrator", candidates=[claude, codex], state_root=root,
                    sleep=lambda s: None, start_runtime_fn=self._fake_start,
                    block_until_death_fn=killed,
                )
            # active-runtime.json is stale (claude, never cleared) -- exactly
            # what a real kill -9 leaves behind.
            stale_cache = rc.read_active_runtime("orchestrator", state_root=root)
            self.assertEqual(stale_cache["runtime"], "claude/anthropic-code")
            # No last-attempt was ever recorded for claude -- the killed
            # invocation never reached that step.
            self.assertIsNone(rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root))

            # Cycle 2: a fresh `controller_main` call (standing in for the
            # systemd-respawned process) must recover by construction --
            # it re-reads durable state (no cooldown recorded for claude
            # yet) and reselects claude again, purely from the substrate.
            recovered = {}

            def capture_start(binding):
                recovered["binding"] = binding
                return self._fake_start(binding)

            rc.controller_main(
                "orchestrator", candidates=[claude, codex], state_root=root,
                sleep=lambda s: None, start_runtime_fn=capture_start,
                block_until_death_fn=lambda binding: None,
            )
            self.assertIs(recovered["binding"], claude)
            # And the second, clean invocation DID clear the stale cache.
            self.assertEqual(rc.read_active_runtime("orchestrator", state_root=root), {})

    def test_stateless_two_independent_invocations_share_no_in_process_memory(self) -> None:
        """SS88: 'no runtime state that needs continuity between sessions.'
        Two `controller_main` calls against two DIFFERENT durable-state
        roots, in the same Python process, back to back. If any module-level
        or closure-captured state leaked between invocations, the second
        call's decision would be contaminated by the first's. It isn't --
        each reads only its own state_root."""
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            root_a, root_b = Path(tmp_a), Path(tmp_b)
            claude = _stateless_binding(root_a, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root_b, harness="codex", profile="interactive-chatgpt")

            now = rc._utc_now()
            # persona "orchestrator" is on cooldown in root_a only.
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root_a, now=now,
            )

            picked = {}

            def capture_start(binding):
                picked["binding"] = binding
                started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
                handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
                return started, handle

            # root_b's "orchestrator" has never been attempted -- must select codex
            # cleanly, unaffected by root_a's cooldown for a same-named persona.
            rc.controller_main(
                "orchestrator", candidates=[codex], state_root=root_b,
                sleep=lambda s: None, start_runtime_fn=capture_start,
                block_until_death_fn=lambda binding: None, now=lambda: now,
            )
            self.assertIs(picked["binding"], codex)
            self.assertIsNone(rc.load_last_attempt("orchestrator", "codex--interactive-chatgpt", state_root=root_a))

    def test_dbus_health_service_fn_defaults_to_a_true_noop(self) -> None:
        """T-2969 DECISION: `controller_main` never reaches for a real
        D-Bus connection unless a caller opts in via
        `dbus_health_service_fn` -- only the `controller-main` CLI
        subcommand (production/systemd's real ExecStart) passes the real
        bootstrap (`_start_persona_controller_dbus_service`). Every other
        test in this class calls `controller_main` without this kwarg and
        must keep passing unaffected by BT3 -- this test makes that
        contract explicit rather than merely implicit in their passing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                sleep=lambda s: None, start_runtime_fn=self._fake_start,
                block_until_death_fn=lambda binding: None,
            )
            self.assertEqual(result, 0)

    def test_dbus_health_service_fn_called_once_and_emits_on_started_and_death_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            service = _FakeHealthService()
            factory_calls: list[str] = []

            def factory(persona: str) -> "_FakeHealthService":
                factory_calls.append(persona)
                return service

            rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                sleep=lambda s: None, start_runtime_fn=self._fake_start,
                block_until_death_fn=lambda binding: None,
                dbus_health_service_fn=factory,
            )
            # called exactly once per controller_main invocation, not once
            # per emission -- a fresh process (BT2's Restart=always) is
            # what re-registers the bus name, not a fresh factory call.
            self.assertEqual(factory_calls, ["orchestrator"])
            # started + death-cleared -- two emissions for a clean
            # start-then-die cycle (spec SS Controller invocation flow
            # steps 3 and 5).
            self.assertEqual(service.emit_count, 2)

    def test_dbus_emit_fires_on_start_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            service = _FakeHealthService()

            def failing_start(binding):
                raise rc.RuntimeOperationalFailure(
                    "boom", failure_class="operational-launch", cause="x",
                    binding_runtime="claude/anthropic-code",
                )

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                sleep=lambda s: None, start_runtime_fn=failing_start,
                block_until_death_fn=lambda binding: None,
                dbus_health_service_fn=lambda persona: service,
            )
            self.assertEqual(result, 1)
            self.assertEqual(service.emit_count, 1)

    def test_dbus_emit_fires_on_no_viable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=now,
            )
            service = _FakeHealthService()

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                sleep=lambda s: None, now=lambda: now, max_reselect_cycles=1,
                dbus_health_service_fn=lambda persona: service,
            )
            self.assertEqual(result, 1)
            self.assertEqual(service.emit_count, 1)


class _FakeHealthService:
    """Stand-in for `persona_controller_health_dbus.PersonaController1Object`
    -- `controller_main` only ever calls `emit_properties_changed()` on
    whatever `dbus_health_service_fn` returns, so a call-counter is a
    sufficient double for every `ControllerMainCycleTests` wiring test."""

    def __init__(self) -> None:
        self.emit_count = 0

    def emit_properties_changed(self) -> None:
        self.emit_count += 1


class DbusHealthPropertiesTests(unittest.TestCase):
    """`dbus_health_properties` (T-2969 BT3; spec SS D-Bus health
    interface) -- the real property computation that replaced the T-2966
    BT1 empty-defaults stub. Every scenario constructs its own isolated
    `state_root` + explicit `candidates` (this file's existing test-seam
    convention), never the ambient default manifest/state-root."""

    def test_never_attempted_candidate_is_viable_with_empty_active_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["ActiveRuntime"], "")
            self.assertEqual(props["ViabilityStatus"], "viable")
            self.assertEqual(props["LastFailureClass"], "")
            self.assertEqual(props["NextCooldownExpiryTs"], "")
            self.assertEqual(props["Holds"], [])
            self.assertEqual(props["LastSelectionTs"], "")

    def test_active_runtime_reflects_the_written_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            handle = rc.RuntimeHandle(claude.launch, claude.controller.tmux, None, rc._utc_now())
            rc.write_active_runtime("orchestrator", claude, handle, pid=4242, state_root=root)
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["ActiveRuntime"], "claude/anthropic-code")

    def test_single_candidate_on_cooldown_reports_cooldown_hold_and_next_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=now,
            )
            expected_expiry = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)["cooldown_until"]
            props = rc.dbus_health_properties(
                "orchestrator", candidates=[claude], state_root=root, now=lambda: now,
            )
            self.assertEqual(props["ViabilityStatus"], "cooldown-hold")
            self.assertEqual(props["NextCooldownExpiryTs"], expected_expiry)
            self.assertEqual(props["LastFailureClass"], "process_death")

    def test_all_candidates_held_reports_all_held_and_holds_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc._atomic_json(
                rc._holds_path("orchestrator", state_root=root),
                {
                    "schema": "pantheon-persona-runtime-holds/v1",
                    "holds": {
                        "claude/anthropic-code": {
                            "set_ts": "2026-08-19T00:00:00Z",
                            "set_by": "operator:operator",
                            "reason": "known broken",
                        },
                    },
                },
            )
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["ViabilityStatus"], "all-held")
            self.assertEqual(props["Holds"], ["claude/anthropic-code"])

    def test_empty_candidate_list_reports_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            props = rc.dbus_health_properties("orchestrator", candidates=[], state_root=root)
            self.assertEqual(props["ViabilityStatus"], "exhausted")
            self.assertEqual(props["NextCooldownExpiryTs"], "")
            self.assertEqual(props["Holds"], [])

    def test_last_failure_class_is_the_most_recent_failed_attempt_across_runtimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            earlier = datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)
            later = datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=earlier,
            )
            rc.record_last_attempt(
                "orchestrator", codex, outcome="failed", failure_class="subscription_cap",
                state_root=root, now=later,
            )
            props = rc.dbus_health_properties(
                "orchestrator", candidates=[claude, codex], state_root=root, now=lambda: later,
            )
            self.assertEqual(props["LastFailureClass"], "subscription_cap")

    def test_last_failure_class_ignores_a_subsequent_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="process_death",
                state_root=root, now=now,
            )
            rc.record_last_attempt(
                "orchestrator", claude, outcome="success",
                state_root=root, now=now + timedelta(minutes=10),
            )
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["LastFailureClass"], "")

    def test_last_selection_ts_reads_the_latest_invocation_log_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            when = datetime(2026, 8, 19, 3, 0, 0, tzinfo=timezone.utc)
            rc.log_invocation(
                "orchestrator", "started", selected="claude/anthropic-code",
                state_root=root, when=when,
            )
            props = rc.dbus_health_properties(
                "orchestrator", candidates=[claude], state_root=root, now=lambda: when,
            )
            self.assertEqual(props["LastSelectionTs"], rc._iso(when))

    def test_last_selection_ts_falls_back_across_a_day_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            yesterday = datetime(2026, 8, 18, 23, 0, 0, tzinfo=timezone.utc)
            today = datetime(2026, 8, 19, 0, 30, 0, tzinfo=timezone.utc)
            rc.log_invocation(
                "orchestrator", "no-viable-candidate", state_root=root, when=yesterday,
            )
            props = rc.dbus_health_properties(
                "orchestrator", candidates=[claude], state_root=root, now=lambda: today,
            )
            self.assertEqual(props["LastSelectionTs"], rc._iso(yesterday))


class ChronicleCompatRemovedTest(unittest.TestCase):
    """T-3090 (Phase 5): the T-3039 graveyard -> chronicle read-both compat
    for on-disk transition/receipt state is fully stripped. Write-new is the
    only path now; these are the negative-path / fail-close proofs that an
    old-format file is passed through RAW (not silently promoted to the new
    key), so downstream consumers correctly treat it as missing evidence
    rather than misreading stale data as valid."""

    def _legacy_transition(self, root: Path, persona: str = "recovery") -> Path:
        path = rc._transition_path(persona, root)
        rc._atomic_json(
            path,
            {
                "schema": "pantheon-runtime-transition/v1",
                "phase": "awaiting-successor-read",
                "trigger": "operator",
                # legacy key only -- no chronicle_evidence -- simulates a file
                # written by the pre-T-3039 code.
                "handoff_or_graveyard": "/tmp/legacy-handoff.md",
            },
        )
        return path

    def test_normalize_chronicle_state_symbol_removed(self) -> None:
        self.assertFalse(hasattr(rc, "_normalize_chronicle_state"))

    def test_transition_state_does_not_promote_legacy_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._legacy_transition(root)
            state = rc.transition_state("recovery", state_root=root)
            self.assertNotIn("chronicle_evidence", state)
            self.assertEqual(state["handoff_or_graveyard"], "/tmp/legacy-handoff.md")

    def test_transition_bytes_and_state_does_not_promote_legacy_key(self) -> None:
        # Separate raw-parse path (used by the sha256-verified read/write cycle) --
        # doesn't go through _read_json, so it needs its own proof.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._legacy_transition(root)
            _raw, state, _sha = rc._transition_bytes_and_state(path)
            self.assertNotIn("chronicle_evidence", state)

    def test_read_json_does_not_promote_legacy_receipt_entry_key(self) -> None:
        # _read_forced_receipt() / _completed_forced_receipt_matches() both read
        # a forced-failover receipt through _read_json -- same chokepoint, proven
        # directly here against the legacy `graveyard_entry` receipt key.
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "forced-failover.json"
            rc._atomic_json(receipt_path, {"graveyard_entry": "/tmp/legacy-evidence.md"})
            receipt = rc._read_json(receipt_path)
            self.assertNotIn("chronicle_entry", receipt)
            self.assertEqual(receipt["graveyard_entry"], "/tmp/legacy-evidence.md")

    def test_chronicle_root_env_var_uses_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = str(Path(tmp) / "new-root")
            with mock.patch.dict(rc.os.environ, {"PANTHEON_CHRONICLE_ROOT": new_dir}):
                self.assertEqual(rc._chronicle_root(), Path(new_dir))

    def test_chronicle_root_ignores_legacy_graveyard_env_var(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = str(Path(tmp) / "old-root")
            env = dict(rc.os.environ)
            env.pop("PANTHEON_CHRONICLE_ROOT", None)
            env["PANTHEON_GRAVEYARD_ROOT"] = old_dir
            with mock.patch.dict(rc.os.environ, env, clear=True):
                self.assertIsNone(rc._chronicle_root())

    def test_chronicle_root_absent_env_returns_none(self) -> None:
        env = {k: v for k, v in rc.os.environ.items()
               if k not in ("PANTHEON_CHRONICLE_ROOT", "PANTHEON_GRAVEYARD_ROOT")}
        with mock.patch.dict(rc.os.environ, env, clear=True):
            self.assertIsNone(rc._chronicle_root())


if __name__ == "__main__":
    unittest.main()
