"""Deterministic tests for the persistent persona runtime controller."""

from __future__ import annotations

import dataclasses
import base64
import hashlib
import inspect
import io
import json
import math
import os
import pwd
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import tempfile
import threading
import time
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
from tools.harness_adapter import claude as claude_adapter_module
from tools.harness_adapter import codex as codex_adapter_module
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
# T-4046: real live-captured idle codex pane (2026-08-05, codex-cli 0.136.0 --
# see that module's own header comment for provenance), reused rather than
# hand-inventing a new codex idle string -- see FallbackReturnEligibleTests'
# sibling suite below for why this matters (the fallback pane being checked
# for idleness is usually codex, not claude).
from tools.test_bringup_acceptance_harness_codex_ready import READY_COMPOSER_AFTER_DISMISS


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
    mid_session_cap_swap: bool = False,
    fallback_return: bool = False,
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
            mid_session_cap_swap_enabled=mid_session_cap_swap,
            fallback_return_enabled=fallback_return,
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


def _submit_helper_into(fake):
    """T-4390: submit_to_runtime runs send_keys_submit.sh through
    rc._run_submit_helper (Popen, so a timeout can kill the helper's whole process
    group), not subprocess.run. Route that seam into the same fake a test already
    gives subprocess.run, so the helper call lands in the same call record. Without
    this, a test that only stubs subprocess.run would run the REAL helper against a
    fixture pane name such as pantheon-recovery:harness.0."""
    return mock.patch.object(
        rc, "_run_submit_helper", side_effect=lambda argv, **kwargs: fake(argv, **kwargs)
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

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples, now=None):
        del harness, profile, now  # T-4047: accepted for interface parity, unused by this fake
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
            reset_at=None,
            cap_kind=None,
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

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples, now=None) -> PostStartAssessment:
        # T-4662: the controller passes its wall clock here, and the fixture's
        # bare "resets 2:50pm (UTC)" resolves to None within the 15-min skew
        # grace (14:50-15:05Z daily). Pin to the selector's fixed instant.
        del now
        assessment = super().assess_post_start_readiness(
            harness=harness,
            profile=profile,
            baseline=baseline,
            samples=samples,
            now=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
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
            probe_resubmit_count=0,
            probe_resubmit_skips="",
            pane_captures_path=None,
            pane_captures_sha256=None,
            pane_captures_error=None,
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

    def run_receiver_handoff(self, binding, direction: str, phase: str, *, backend=None, reason: str, prior_runtime: str = "live"):
        del binding, backend, reason, prior_runtime
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


class _PollerBackend(_ViabilityBackend):
    """T-4179: a stateful codex poller. The codex receiver-state proof reports
    the real state (present until the stop action runs); the KEEP handoff's
    prove-absent step fails while it is still present. ``stop_ineffective``
    models a stop that does not take (unit restarts, stray process)."""

    def __init__(self, events: list[str], *, poller_present: bool, stop_ineffective: bool = False) -> None:
        super().__init__(events)
        self.poller_present = poller_present
        self.stop_ineffective = stop_ineffective

    def execute(self, action: str, binding) -> dict[str, object]:
        result = super().execute(action, binding)
        if action == "stop-codex-poller" and not self.stop_ineffective:
            self.poller_present = False
        if action == "probe-codex-absent" and self.poller_present:
            raise RuntimeError("codex receiver still present")
        return result

    def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
        if binding.launch.harness != "codex":
            return super().prove_receiver_state(binding, expected)
        self.events.append(f"codex:proof-{expected}")
        state = "present" if self.poller_present else "absent"
        return ReceiverStateProof(state, {"harness": "codex"})


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
        def start(binding, adapter, *, replace=False, context=None, state_root=None):
            del adapter, replace, context
            events.append(f"{binding.launch.harness}:start")
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

        real_verify = rc._verify_runtime_input_ready

        def verify_input(binding, started, *, state_root=None, adapter=None, context=None, launched_at=None):
            if fail_claude_readiness and binding.launch.harness == "claude":
                # T-4332 r2: the REAL function with only the readiness wait forced
                # to not-ready, so its pre-raise scope stop (and the receiver-window
                # read before it) run exactly as in production.
                not_ready = rc.RuntimeInputReadiness(
                    ready=False, pane_sha256="0" * 64, reason="runtime input readiness timed out"
                )
                with mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=not_ready):
                    real_verify(
                        binding, started, state_root=state_root, adapter=adapter,
                        context=context, launched_at=launched_at,
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

        def start(binding, adapter, *, replace=False, context=None, state_root=None):
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

    def _run_provider_completion_selector(
        self,
        root: Path,
        *,
        mode: str,
        capture_count: int = 1,
        history_at: int | None = None,
        pane_after_submit: str | None = None,
        resubmit_rc: int = 0,
    ):
        """T-4321 knobs: `history_at` lands the probe's history entry k captures
        after the first submit (write lag); `pane_after_submit` replaces every pane
        captured after the provider baseline; `resubmit_rc` is send_keys_submit's
        exit code for every submit after the first."""
        events: list[str] = []
        self._submit_argvs: list[tuple[str, ...]] = []
        captured_prompt = {"marker": ""}
        candidates, account_home = self._completion_manifest_candidates(root)
        transcript = account_home / ".claude" / "projects" / "r18" / "session.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        # T-4321 modes. Claude Code records each submitted prompt in
        # ~/.claude/history.jsonl, so these seed it with an unrelated older
        # entry and decide per submit whether the probe "landed".
        history = account_home / ".claude" / "history.jsonl"
        if mode in {"swallowed-once", "swallowed-always", "accepted-slow", "pending-in-composer"}:
            history.write_text(
                json.dumps({"display": "an older prompt", "pastedContents": {}, "timestamp": 1}) + "\n",
                encoding="utf-8",
            )
        submits = {"count": 0, "captures_after": 0}
        # Which candidate is live, and how many panes it has captured so far.
        phase = {"harness": "", "captures": 0}

        def write_probe_history() -> None:
            with history.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"display": f"Provider completion probe: {captured_prompt['marker']}",
                                "pastedContents": {}, "timestamp": 2})
                    + "\n"
                )

        def land_submit() -> None:
            submits["count"] += 1
            marker = captured_prompt["marker"]
            lands = mode == "accepted-slow" or (mode == "swallowed-once" and submits["count"] >= 2)
            if not lands:
                return
            write_probe_history()
            if mode == "swallowed-once":
                self._write_transcript_event(
                    transcript,
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                    },
                )
                self._write_statusline(root, account_home, transcript)

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
        # The probe's text still sitting in the composer (never re-type into it).
        pending = (
            "some output\n╭──────────╮\n│ box │\n╰──────────╯\n"
            "❯ Provider completion probe. Reply with exactly this marker and no extra text\n"
        )
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
            phase["captures"] += 1
            if submits["count"] >= 1:
                submits["captures_after"] += 1
                if history_at is not None and submits["captures_after"] == history_at:
                    write_probe_history()
            override = pane_after_submit if pane_after_submit is not None else (
                pending if mode == "pending-in-composer" else None)
            # Only the claude candidate's panes after its input-ready and provider
            # baseline captures; the codex fallback always sees a ready pane.
            if override is not None and phase["harness"] == "claude" and phase["captures"] > 2:
                return override
            return text

        def start(binding, adapter, *, replace=False, context=None, state_root=None):
            del adapter, replace, context
            events.append(f"{binding.launch.harness}:start")
            phase.update(harness=binding.launch.harness, captures=0)
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

        def run(argv, **kwargs):
            del kwargs
            argv_tuple = tuple(str(item) for item in argv)
            if argv_tuple and argv_tuple[0] == str(rc.SEND_KEYS_HELPER):
                events.append("provider-submit")
                self._submit_argvs.append(argv_tuple)
                match = re.search(r"PANTHEON_PROVIDER_COMPLETION_[0-9a-f]+", argv_tuple[-1])
                if match:
                    captured_prompt["marker"] = match.group(0)
                land_submit()
                if resubmit_rc and submits["count"] >= 2:
                    return subprocess.CompletedProcess(
                        argv, resubmit_rc, "",
                        "[send_keys_submit] ERROR: prompt submit unconfirmed after 2 Enter retries")
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
        ), mock.patch.object(rc.subprocess, "run", side_effect=run), _submit_helper_into(run), mock.patch.object(
            rc.time, "sleep", side_effect=lambda _seconds: events.append("sleep")
        ), mock.patch.object(rc, "PROVIDER_COMPLETION_CAPTURE_COUNT", capture_count), mock.patch.object(
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

    def test_initial_viability_teardown_reports_the_candidates_receiver_window(self) -> None:
        # T-4332 wiring: the viability loop must hand the launch boundary to the
        # teardown, or a plugin that started inside the candidate goes unreported.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            mailbox.provision(primary.mailbox)
            bot_pid = root / ".claude" / "channels" / "telegram" / "bot.pid"

            def plugin_starts(binding, *args, **kwargs):
                del args, kwargs
                if binding.launch.harness == "claude":
                    bot_pid.parent.mkdir(parents=True, exist_ok=True)
                    bot_pid.write_text("4242\n", encoding="utf-8")

            def stop_like_sigterm(scope):
                # A scope stop is a SIGTERM, and the plugin removes its bot.pid on
                # a clean shutdown (server.ts shutdown()).
                events.append(f"stop:{scope}")
                bot_pid.unlink(missing_ok=True)

            patches = self._selector_patches(events, fail_claude_readiness=True)
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(rc, "_verify_started_runtime", side_effect=plugin_starts))
                stack.enter_context(mock.patch.object(rc, "_stop_created_scope", side_effect=stop_like_sigterm))
                selection = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=_Adapter,
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            state = rc.initial_viability_state("recovery", state_root=root)
            notices = sorted((primary.mailbox / "new").iterdir())
        self.assertEqual(selection.binding.launch.harness, "codex")
        teardown = state["attempts"][0]["teardown"]
        self.assertEqual(teardown["receiver_bot_pid"], "4242")
        self.assertEqual(teardown["receiver_window_notice"], "sent")
        self.assertEqual(len(notices), 1)

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

    def _live_drift_run(self, *, key_mismatch: str | None = None, repeat: int = 1,
                        second_hash: str | None = None, receiver: dict | None = None,
                        in_scope: bool = True):
        """Live pane + recorded hashes that differ from current (T-5311).

        ``key_mismatch`` (T-5315): "removed" drops a current path from the record
        (a path newly added to RUNTIME_SOURCE_HASH_PATHS), "added" records a path the
        current list no longer hashes. Either way the recorded manifest is recomputed
        (a self-consistent record) and every shared path's content is unchanged, so
        the ONLY drift is the key set. Full evidence rows land in self.last_evidence."""
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
            if key_mismatch is None:
                current_hashes[rc.RUNTIME_SOURCE_HASH_PATHS[0]] = "b" * 64
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            runtime_path = root / "recovery" / "runtime-controller" / "runtime.json"
            before = runtime_path.read_text(encoding="utf-8")
            if key_mismatch is not None:
                doc = json.loads(before)
                _mutate_recorded_key_set(doc, key_mismatch)
                runtime_path.write_text(json.dumps(doc), encoding="utf-8")
                before = runtime_path.read_text(encoding="utf-8")
            pane, pid, scope, start = self._reuse_patches(events)
            results: list = []
            error = None
            extra = contextlib.ExitStack()
            if receiver is not None:
                extra.enter_context(mock.patch.object(rc, "_read_only_receiver_present", return_value=receiver))
            if not in_scope:
                scope = mock.patch.object(rc, "_pid_in_scope", return_value=False)
            with extra, pane, pid, scope, start as start_runtime:
                for index in range(repeat):
                    hashes = dict(current_hashes)
                    if index and second_hash:
                        hashes[rc.RUNTIME_SOURCE_HASH_PATHS[0]] = second_hash
                    with _provenance_patch_with(source_hashes=hashes):
                        try:
                            results.append(
                                rc.ensure_viable_runtime(
                                    (primary, fallback),
                                    adapter_factory=lambda binding: _Adapter(binding),
                                    state_root=root,
                                    backend=_ViabilityBackend(events),
                                )
                            )
                        except rc.RuntimeViabilityBlocked as exc:
                            error = exc
            after = runtime_path.read_text(encoding="utf-8")
            self.last_evidence = _evidence_records(root)
            return results, error, start_runtime, before, after, _evidence_events(root)

    def test_live_pane_source_drift_is_reused_without_launch_or_restamp(self) -> None:
        results, error, start, before, after, events = self._live_drift_run()
        self.assertIsNone(error)
        start.assert_not_called()
        self.assertEqual(after, before)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].receipt.status, "reused-recorded")
        attempt = results[0].receipt.attempts[0]
        self.assertEqual(attempt.readiness["source_equivalence_mode"], "live-source-drift")
        self.assertNotIn("match exactly", attempt.readiness["source_equivalence_reason"])
        self.assertNotIn("runtime_viability_blocked", events)

    def test_live_pane_source_drift_evidence_once_per_manifest_pair(self) -> None:
        _r, error, _s, _b, _a, events = self._live_drift_run(repeat=3)
        self.assertIsNone(error)
        self.assertEqual(events.count("runtime_live_source_drift"), 1)
        _r, error, _s, _b, _a, events = self._live_drift_run(repeat=2, second_hash="c" * 64)
        self.assertIsNone(error)
        self.assertEqual(events.count("runtime_live_source_drift"), 2)

    def test_live_pane_key_set_drift_is_reused(self) -> None:
        """T-5315: a RUNTIME_SOURCE_HASH_PATHS change (recorded key set differs from
        current) is source drift like any other: the live session is reused for
        delivery, nothing launches, runtime.json is untouched, and the once-per-pair
        evidence row records the key-set delta."""
        last = rc.RUNTIME_SOURCE_HASH_PATHS[-1]
        for variant, added, removed in (
            ("removed", [last], []),
            ("added", [], [_STALE_SOURCE_KEY]),
        ):
            with self.subTest(variant=variant):
                results, error, start, before, after, events = self._live_drift_run(
                    key_mismatch=variant, repeat=2
                )
                self.assertIsNone(error)
                start.assert_not_called()
                self.assertEqual(after, before)
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0].receipt.status, "reused-recorded")
                attempt = results[0].receipt.attempts[0]
                self.assertEqual(attempt.readiness["source_equivalence_mode"], "live-source-drift")
                self.assertEqual(attempt.cause, "recorded-runtime-source-drift-live")
                self.assertNotIn("runtime_viability_blocked", events)
                rows = [r for r in self.last_evidence if r["event"] == "runtime_live_source_drift"]
                self.assertEqual(len(rows), 1)  # dedup per manifest pair unchanged
                self.assertEqual(rows[0]["source_key_set_changed"], "true")
                self.assertEqual(rows[0]["source_keys_added"], added)
                self.assertEqual(rows[0]["source_keys_removed"], removed)
                self.assertEqual(rows[0]["source_keys_added_count"], len(added))
                self.assertEqual(rows[0]["source_keys_removed_count"], len(removed))

    def test_live_pane_content_only_drift_evidence_has_no_key_delta(self) -> None:
        _r, error, _s, _b, _a, _e = self._live_drift_run()
        self.assertIsNone(error)
        rows = [r for r in self.last_evidence if r["event"] == "runtime_live_source_drift"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("source_key_set_changed", rows[0])

    def test_live_pane_drift_with_receiver_absent_still_blocks(self) -> None:
        results, error, start, _b, _a, _e = self._live_drift_run(
            receiver={"ok": False, "state": "absent"}
        )
        self.assertEqual(results, [])
        self.assertEqual(error.reason, "recorded-receiver-not-present")
        start.assert_not_called()

    def test_live_pane_drift_with_server_out_of_scope_still_blocks(self) -> None:
        results, error, start, _b, _a, _e = self._live_drift_run(in_scope=False)
        self.assertEqual(results, [])
        self.assertEqual(error.reason, "runtime-server-not-in-recorded-scope")
        start.assert_not_called()

    def _dead_pane_drift_run(
        self,
        *,
        drift: bool = True,
        corrupt_recorded: str | None = None,
        receiver_ok: bool = True,
        scope_inactive: bool = True,
        held: bool = False,
    ):
        """T-5292: consume finds the recorded runtime's pane DEAD while the controller
        source hashes have moved since it was recorded. Returns (result, error,
        start_mock, runtime_json_after, evidence_events, current_hashes)."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = _fixture_source_provenance(
                revision="1" * 40,
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            current_hashes = dict(recorded.source_hashes)
            if drift:
                current_hashes[rc.RUNTIME_SOURCE_HASH_PATHS[0]] = "b" * 64
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            runtime_path = root / fallback.persona / "runtime-controller" / "runtime.json"
            if corrupt_recorded == "missing":
                state = json.loads(runtime_path.read_text(encoding="utf-8"))
                del state["controller_source_hashes"]
                runtime_path.write_text(json.dumps(state), encoding="utf-8")
            elif corrupt_recorded == "malformed":
                state = json.loads(runtime_path.read_text(encoding="utf-8"))
                state["controller_source_hashes"][rc.RUNTIME_SOURCE_HASH_PATHS[0]] = "nothex"
                runtime_path.write_text(json.dumps(state), encoding="utf-8")
            elif corrupt_recorded == "key-popped-stale-manifest":
                # Self-inconsistent record: a key removed WITHOUT recomputing the manifest.
                state = json.loads(runtime_path.read_text(encoding="utf-8"))
                state["controller_source_hashes"].pop(rc.RUNTIME_SOURCE_HASH_PATHS[-1])
                runtime_path.write_text(json.dumps(state), encoding="utf-8")
            elif corrupt_recorded in {"removed", "added"}:
                # T-5315: a self-consistent record from an older/newer path list.
                state = json.loads(runtime_path.read_text(encoding="utf-8"))
                _mutate_recorded_key_set(state, corrupt_recorded)
                runtime_path.write_text(json.dumps(state), encoding="utf-8")

            def fake_start(binding, *args, **kwargs):
                observed = rc._utc_now()
                rc._atomic_json(
                    runtime_path,
                    rc._runtime_payload(
                        binding,
                        session_id=None,
                        started_at=observed,
                        runtime_scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
                        source_provenance=_fixture_source_provenance(
                            revision="2" * 40,
                            source_hashes=current_hashes,
                            runtime_start_boundary_at=observed,
                            runtime_start_observed_at=observed,
                        ),
                        provider_completion={"provider_completion_status": "not-applicable"},
                    ),
                )
                return mock.MagicMock(scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT, created_scope=False)

            receiver = {"ok": receiver_ok, "state": "present" if receiver_ok else "absent", "evidence": {}}
            holds = {rc._runtime_ref(fallback): {"reason": "test"}} if held else {}
            result = error = None
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(rc, "pane_exists", return_value=False))
                stack.enter_context(mock.patch.object(rc, "_recorded_scope_inactive", return_value=scope_inactive))
                stack.enter_context(mock.patch.object(rc, "prove_managed_receiver_present", return_value=True))
                stack.enter_context(mock.patch.object(rc, "prove_predecessor_receiver_absent", return_value=True))
                stack.enter_context(mock.patch.object(rc, "_read_only_receiver_present", return_value=receiver))
                stack.enter_context(mock.patch.object(rc, "_stop_prior_harness_receiver_before_start"))
                stack.enter_context(mock.patch.object(rc, "_verify_started_runtime"))
                stack.enter_context(mock.patch.object(rc, "_verify_runtime_input_ready"))
                stack.enter_context(mock.patch.object(rc, "load_holds", return_value=holds))
                stack.enter_context(
                    _provenance_patch_with(revision="2" * 40, source_hashes=current_hashes)
                )
                start = stack.enter_context(mock.patch.object(rc, "_start_runtime", side_effect=fake_start))
                try:
                    result = rc._matching_recorded_runtime_selection(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=None,
                        context=None,
                        now=datetime(2026, 10, 20, 20, 0, 0, tzinfo=timezone.utc),
                        manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                        attempt_limit=2,
                    )
                except rc.RuntimeViabilityBlocked as exc:
                    error = exc
            after = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.last_evidence = _evidence_records(root)
            return result, error, start, after, _evidence_events(root), current_hashes

    def test_dead_pane_source_drift_rehydrates_with_current_provenance(self) -> None:
        result, error, start, after, events, current = self._dead_pane_drift_run()
        self.assertIsNone(error)
        start.assert_called_once()
        self.assertEqual(result.receipt.status, "reused-recorded")
        attempt = result.receipt.attempts[0]
        self.assertEqual(attempt.cause, "recorded-runtime-rehydrated")
        self.assertEqual(attempt.readiness["source_equivalence_mode"], "source-drift-rerecorded")
        self.assertNotIn("match exactly", attempt.readiness["source_equivalence_reason"])
        self.assertEqual(after["controller_source_hashes"], current)
        self.assertEqual(after["controller_source_revision"], "2" * 40)
        self.assertEqual(after["controller_source_manifest_sha256"], rc._source_manifest_hash(current))
        self.assertIn("runtime_rehydrate_source_drift", events)
        self.assertIn("runtime_rehydrated", events)

    def test_dead_pane_identical_source_keeps_exact_match_record(self) -> None:
        result, error, start, after, events, current = self._dead_pane_drift_run(drift=False)
        self.assertIsNone(error)
        start.assert_called_once()
        attempt = result.receipt.attempts[0]
        self.assertIn(attempt.readiness["source_equivalence_mode"], {"same-revision", "ticket-only-revision"})
        self.assertIn("match exactly", attempt.readiness["source_equivalence_reason"])
        self.assertNotIn("runtime_rehydrate_source_drift", events)

    def test_dead_pane_corrupt_recorded_hashes_still_block(self) -> None:
        for corruption, reason in (
            ("missing", "recorded-source-hashes-missing"),
            ("malformed", "recorded-source-hash-malformed"),
            # T-5315: a popped key with a stale manifest is a self-inconsistent record.
            ("key-popped-stale-manifest", "recorded-source-manifest-mismatch"),
        ):
            with self.subTest(corruption=corruption):
                result, error, start, _after, _events, _current = self._dead_pane_drift_run(
                    corrupt_recorded=corruption
                )
                self.assertIsNone(result)
                self.assertEqual(error.reason, reason)
                start.assert_not_called()

    def test_dead_pane_key_set_drift_rehydrates_with_current_provenance(self) -> None:
        """T-5315: a self-consistent record whose key set differs from the current
        RUNTIME_SOURCE_HASH_PATHS rehydrates as source-drift-rerecorded and re-records
        the FULL current key set, with the key delta in the drift event."""
        last = rc.RUNTIME_SOURCE_HASH_PATHS[-1]
        for variant, added, removed in (
            ("removed", [last], []),
            ("added", [], [_STALE_SOURCE_KEY]),
        ):
            with self.subTest(variant=variant):
                result, error, start, after, events, current = self._dead_pane_drift_run(
                    drift=False, corrupt_recorded=variant
                )
                self.assertIsNone(error)
                start.assert_called_once()
                attempt = result.receipt.attempts[0]
                self.assertEqual(attempt.readiness["source_equivalence_mode"], "source-drift-rerecorded")
                self.assertEqual(after["controller_source_hashes"], current)
                self.assertEqual(set(after["controller_source_hashes"]), set(rc.RUNTIME_SOURCE_HASH_PATHS))
                self.assertEqual(
                    after["controller_source_manifest_sha256"], rc._source_manifest_hash(current)
                )
                self.assertIn("runtime_rehydrated", events)
                rows = [r for r in self.last_evidence if r["event"] == "runtime_rehydrate_source_drift"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["source_key_set_changed"], "true")
                self.assertEqual(rows[0]["source_keys_added"], added)
                self.assertEqual(rows[0]["source_keys_removed"], removed)

    def test_dead_pane_drift_with_receiver_absent_still_blocks(self) -> None:
        result, error, start, _after, _events, _current = self._dead_pane_drift_run(receiver_ok=False)
        self.assertIsNone(result)
        self.assertEqual(error.reason, "recorded-receiver-not-present")
        start.assert_not_called()

    def test_dead_pane_drift_with_scope_still_active_still_blocks(self) -> None:
        result, error, start, _after, _events, _current = self._dead_pane_drift_run(scope_inactive=False)
        self.assertIsNone(result)
        self.assertEqual(error.reason, "recorded-scope-still-active")
        start.assert_not_called()

    def test_dead_pane_drift_with_held_runtime_is_not_rehydrated(self) -> None:
        result, error, start, _after, events, _current = self._dead_pane_drift_run(held=True)
        self.assertIsNone(result)
        self.assertIsNone(error)
        start.assert_not_called()
        self.assertNotIn("runtime_rehydrate_source_drift", events)

    def test_authenticate_recorded_runtime_drift_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = _fixture_source_provenance(
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            current_hashes = dict(recorded.source_hashes)
            current_hashes[rc.RUNTIME_SOURCE_HASH_PATHS[0]] = "b" * 64
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            state = json.loads(
                (root / fallback.persona / "runtime-controller" / "runtime.json").read_text(encoding="utf-8")
            )
            receiver = {"ok": True, "state": "present", "evidence": {}}
            with mock.patch.object(rc, "_read_only_receiver_present", return_value=receiver), _provenance_patch_with(
                source_hashes=current_hashes
            ):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc._authenticate_recorded_selected_runtime(
                        fallback, state, state_root=root, backend=None, context=None
                    )
                self.assertEqual(caught.exception.reason, "recorded-source-hash-mismatch")
                *_head, mode = rc._authenticate_recorded_selected_runtime(
                    fallback, state, state_root=root, backend=None, context=None, allow_source_drift=True
                )
                # T-5315: the strict (non-drift-tolerant) path keeps the key-mismatch
                # reason for a self-consistent record from a different path list.
                keyed = json.loads(json.dumps(state))
                _mutate_recorded_key_set(keyed, "removed")
                with self.assertRaises(rc.RuntimeViabilityBlocked) as strict:
                    rc._authenticate_recorded_selected_runtime(
                        fallback, keyed, state_root=root, backend=None, context=None
                    )
                self.assertEqual(strict.exception.reason, "recorded-source-key-mismatch")
                *_head, keyed_mode = rc._authenticate_recorded_selected_runtime(
                    fallback, keyed, state_root=root, backend=None, context=None, allow_source_drift=True
                )
        self.assertEqual(mode, "source-drift-rerecorded")
        self.assertEqual(keyed_mode, "source-drift-rerecorded")

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
        # T-5315: a popped key WITHOUT a recomputed manifest is a self-inconsistent
        # record (a self-consistent key-set change is drift: _live_drift_run).
        self.assertEqual(caught.exception.reason, "recorded-source-manifest-mismatch")

    def _operator_swapped_to_codex(
        self, root: Path, primary, fallback, *, trigger: str = "operator", phase: str = "completed"
    ) -> rc.RuntimeSourceProvenance:
        """T-4469 drill shape: bootstrap proved claude (initial-viability), then an
        operator swap put codex in the pane, so runtime.json now records codex."""
        recorded = _fixture_source_provenance(
            runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
            runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
        )
        self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
        self._write_stale_selected_codex_viability(root, primary, fallback)
        path = rc._initial_viability_path(primary.persona, root)
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(
            selected_runtime=rc._runtime_ref(primary),
            selected_harness=primary.launch.harness,
            selected_profile=primary.launch.profile,
        )
        rc._atomic_json(path, state)
        _write_operator_transition(root, rc._runtime_ref(fallback), trigger=trigger, phase=phase)
        return recorded

    def test_consume_reuses_the_live_operator_destination_over_the_bootstrap_pick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback)
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            evidence = _evidence_events(root)
        start_runtime.assert_not_called()
        self.assertEqual(reused.receipt.status, "reused-recorded")
        self.assertEqual(reused.binding.launch.harness, "codex")
        self.assertIn("consume_selected_operator_destination", evidence)

    def test_consume_blocks_when_the_pane_is_not_running_the_operator_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback)
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("claude")):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
            evidence = _evidence_events(root)
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "runtime-state-missing-or-mismatched")
        # The block itself is evidenced, so the log exists: the NotIn is not vacuous.
        self.assertIn("runtime_viability_blocked", evidence)
        self.assertNotIn("consume_selected_operator_destination", evidence)

    def test_consume_never_relaunches_the_operator_destination_behind_a_dead_pane(self) -> None:
        """After fallback-return ends the operator's codex, runtime.json still records
        codex and transition.json still names it. A dead pane must NOT take the
        override (which would rehydrate codex right behind the failback)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._operator_swapped_to_codex(root, primary, fallback)
            with mock.patch.object(rc, "_pane_process_pid", return_value=None), mock.patch.object(
                rc, "pane_exists", return_value=False
            ), mock.patch.object(rc, "_rehydrate_recorded_runtime_selection") as rehydrate:
                result = rc._matching_recorded_runtime_selection(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=None,
                    context=None,
                    now=datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc),
                    manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                    attempt_limit=2,
                )
            evidence = _evidence_events(root)
        self.assertIsNone(result)
        rehydrate.assert_not_called()
        self.assertNotIn("consume_selected_operator_destination", evidence)

    def _consume_after_quota_swap(self, trigger: str, phase: str, live: str):
        """T-4872: consume after a quota-driven swap onto codex; `live` is the harness
        the pane positively runs. Returns (reused|None, reason|None, evidence, start)."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback, trigger=trigger, phase=phase)
            pane, pid, scope, start = self._reuse_patches(events)
            reused = reason = None
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs(live)):
                try:
                    reused = rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
                except rc.RuntimeViabilityBlocked as caught:
                    reason = caught.reason
            evidence_path = rc._evidence_log_path(primary.persona, root)
            rows = [
                json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()
            ] if evidence_path.exists() else []
        return reused, reason, rows, start_runtime

    def test_consume_accepts_a_completed_quota_swap_destination(self) -> None:
        for trigger in ("planned-quota", "forced-cap", "quota-reset"):
            with self.subTest(trigger=trigger):
                reused, reason, rows, start_runtime = self._consume_after_quota_swap(trigger, "completed", "codex")
                start_runtime.assert_not_called()
                self.assertIsNone(reason)
                self.assertEqual(reused.receipt.status, "reused-recorded")
                self.assertEqual(reused.binding.launch.harness, "codex")
                picked = [r for r in rows if r["event"] == "consume_selected_transition_destination"]
                self.assertEqual(len(picked), 1)
                self.assertEqual(picked[0]["trigger"], trigger)
                self.assertNotIn("consume_selected_operator_destination", [r["event"] for r in rows])

    @staticmethod
    def _real_begin_transition_record(root: Path, persona: str, trigger: str, outgoing, incoming) -> None:
        """T-5794: transition.json exactly as the REAL planned-swap flow leaves it:
        _begin_transition, then the swap's awaiting-successor-read update, then the
        successor ack's completion. No step writes `destination` (the fixture
        _write_operator_transition hand-wrote it, which hid the live defect)."""
        rc._begin_transition(
            persona, trigger=trigger,
            outgoing_profile=outgoing.launch.profile, incoming_profile=incoming.launch.profile,
            outgoing_harness=outgoing.launch.harness, incoming_harness=incoming.launch.harness,
            drain_id=None, state_root=root,
        )
        path = rc._transition_path(persona, root)
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(
            phase="completed", runtime_swapped=True, fallback_active=trigger == "planned-quota",
            successor_read_ack=True, completed_at="2026-11-01T17:08:43Z",
        )
        rc._atomic_json(path, state)
        assert "destination" not in json.loads(path.read_text(encoding="utf-8"))

    def test_consume_accepts_real_begin_transition_record_without_destination(self) -> None:
        """T-5794 live shape (recovery/archivist 2026-11-01): the completed quota record has
        incoming_* but no destination; consume must follow it, not block."""
        for trigger in ("planned-quota", "forced-cap", "quota-reset"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("INVOCATION_ID", None)
                root = Path(tmp)
                events: list[str] = []
                primary, fallback = self._bindings(root)
                recorded = self._operator_swapped_to_codex(root, primary, fallback, trigger=trigger)
                self._real_begin_transition_record(root, primary.persona, trigger, primary, fallback)
                pane, pid, scope, start = self._reuse_patches(events)
                with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                    source_hashes=recorded.source_hashes,
                ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                    reused = rc.ensure_viable_runtime(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=_ViabilityBackend(events),
                    )
                evidence = _evidence_events(root)
                start_runtime.assert_not_called()
                self.assertEqual(reused.binding.launch.harness, "codex")
                self.assertIn("consume_selected_transition_destination", evidence)

    def test_consume_follows_controller_fallback_return_receipt(self) -> None:
        """T-5794 orchestrator shape: an older planned-quota record onto codex
        (fallback_active=True) is left behind; the controller's fallback-return then
        relaunches the primary. The receipt controller_main now writes must let
        consume follow the live primary instead of blocking against codex."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            # runtime.json records codex in this scaffold; flip the roles so the
            # recorded/live runtime is the RETURN destination.
            recorded = self._operator_swapped_to_codex(root, primary, fallback, trigger="planned-quota")
            self._real_begin_transition_record(root, primary.persona, "planned-quota", primary, fallback)
            self.assertTrue(rc._record_fallback_return_transition(
                primary.persona, primary, rc._runtime_ref(fallback), state_root=root,
                now=datetime(2026, 11, 1, 17, 1, 26, tzinfo=timezone.utc),
            ))
            record = rc.transition_state(primary.persona, state_root=root)
            self.assertEqual(
                (record["trigger"], record["phase"], record["destination"], record["fallback_active"]),
                ("fallback-return", "completed", rc._runtime_ref(fallback), False),
            )
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                )
            start_runtime.assert_not_called()
            self.assertEqual(reused.binding.launch.harness, "codex")
            self.assertIn("consume_selected_transition_destination", _evidence_events(root))

    def test_fallback_return_receipt_never_overwrites_an_in_flight_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            for phase in ("preparing", "awaiting-successor-read", "compensation-failed"):
                with self.subTest(phase=phase):
                    _write_operator_transition(root, rc._runtime_ref(fallback), phase=phase, trigger="planned-quota")
                    self.assertFalse(rc._record_fallback_return_transition(
                        primary.persona, fallback, rc._runtime_ref(primary), state_root=root,
                        now=datetime(2026, 11, 1, 17, 2, tzinfo=timezone.utc),
                    ))
                    state = rc.transition_state(primary.persona, state_root=root)
                    self.assertEqual((state["phase"], state["trigger"]), (phase, "planned-quota"))

    def test_consume_accepts_live_natural_cap_swap_without_transition_receipt(self) -> None:
        """T-5744: a mid-session cap exits before a transition receipt exists.
        The active cap record, exact runtime.json, and positive live-pane argv are
        the fail-closed proof that mail may wake the controller-selected fallback."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback)
            (root / "recovery" / "runtime-controller" / "transition.json").unlink()
            now = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
            rc.record_last_attempt(
                primary.persona,
                primary,
                outcome="failed",
                failure_class="subscription_cap",
                now=now,
                cooldown_until_override=now + timedelta(days=2),
                state_root=root,
            )
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: _Adapter(binding),
                    state_root=root,
                    backend=_ViabilityBackend(events),
                    now=now,
                )
            evidence = _evidence_events(root)
        start_runtime.assert_not_called()
        self.assertEqual(reused.receipt.status, "reused-recorded")
        self.assertEqual(reused.binding.launch.harness, "codex")
        self.assertIn("consume_selected_cap_fallback", evidence)

    def test_consume_rejects_expired_natural_cap_swap_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback)
            (root / "recovery" / "runtime-controller" / "transition.json").unlink()
            now = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class="subscription_cap",
                now=now - timedelta(hours=2), cooldown_until_override=now - timedelta(hours=1),
                state_root=root,
            )
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback), adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root, backend=_ViabilityBackend(events), now=now,
                    )
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "runtime-state-missing-or-mismatched")

    def test_consume_rejects_expired_cap_even_when_primary_is_held(self) -> None:
        """A hold may make the fallback viable, but it cannot revive stale cap proof."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = self._operator_swapped_to_codex(root, primary, fallback)
            (root / "recovery" / "runtime-controller" / "transition.json").unlink()
            now = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class="subscription_cap",
                now=now - timedelta(hours=2), cooldown_until_override=now - timedelta(hours=1),
                state_root=root,
            )
            holds_path = rc._holds_path(primary.persona, state_root=root)
            holds_path.parent.mkdir(parents=True, exist_ok=True)
            holds_path.write_text(json.dumps({
                "schema": "pantheon-persona-runtime-holds/v1",
                "holds": {rc._runtime_ref(primary): {"reason": "unrelated-operator-hold"}},
            }), encoding="utf-8")
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, _provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ), mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_viable_runtime(
                        (primary, fallback), adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root, backend=_ViabilityBackend(events), now=now,
                    )
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "runtime-state-missing-or-mismatched")

    def test_consume_still_blocks_an_in_flight_quota_swap(self) -> None:
        for phase in ("preparing", "awaiting-successor-read"):
            with self.subTest(phase=phase):
                reused, reason, rows, start_runtime = self._consume_after_quota_swap("planned-quota", phase, "codex")
                start_runtime.assert_not_called()
                self.assertIsNone(reused)
                self.assertEqual(reason, "runtime-state-missing-or-mismatched")
                events = [r["event"] for r in rows]
                self.assertIn("runtime_viability_blocked", events)
                self.assertNotIn("consume_selected_transition_destination", events)

    def test_consume_blocks_a_quota_swap_when_the_pane_runs_something_else(self) -> None:
        reused, reason, rows, start_runtime = self._consume_after_quota_swap("planned-quota", "completed", "claude")
        start_runtime.assert_not_called()
        self.assertIsNone(reused)
        self.assertEqual(reason, "runtime-state-missing-or-mismatched")
        self.assertNotIn("consume_selected_transition_destination", [r["event"] for r in rows])

    def test_consume_never_relaunches_a_quota_destination_behind_a_dead_pane(self) -> None:
        for trigger in ("planned-quota", "forced-cap", "quota-reset"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                primary, fallback = self._bindings(root)
                self._operator_swapped_to_codex(root, primary, fallback, trigger=trigger)
                with mock.patch.object(rc, "_pane_process_pid", return_value=None), mock.patch.object(
                    rc, "pane_exists", return_value=False
                ), mock.patch.object(rc, "_rehydrate_recorded_runtime_selection") as rehydrate:
                    result = rc._matching_recorded_runtime_selection(
                        (primary, fallback),
                        adapter_factory=lambda binding: _Adapter(binding),
                        state_root=root,
                        backend=None,
                        context=None,
                        now=datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc),
                        manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                        attempt_limit=2,
                    )
                self.assertIsNone(result)
                rehydrate.assert_not_called()
                self.assertNotIn("consume_selected_transition_destination", _evidence_events(root))

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
        # T-4173: the consume path's post-hook cap readiness now carries the
        # derived cap_kind/reset_at too (additive keys on the persisted attempt).
        fresh_readiness = fresh_state["attempts"][0]["readiness"]
        self.assertEqual(fresh_readiness["cap_kind"], "session-limit")
        self.assertIsNotNone(rc._parse_iso_or_none(fresh_readiness["reset_at"]))
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

    def test_initial_viability_resubmits_a_probe_swallowed_during_boot(self) -> None:
        # T-4321: recovery's T-3026 handmove (2026-09-29) pasted the probe mid-boot.
        # It never reached ~/.claude/history.jsonl, submit-verify still
        # reported it submitted, and the seat fell over to codex. A probe with
        # no history entry after the grace window is re-submitted.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, marker = self._run_provider_completion_selector(
                root, mode="swallowed-once", capture_count=60
            )
            runtime = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "claude")
        self.assertEqual(events.count("provider-submit"), 2)
        self.assertEqual(runtime["provider_completion"]["provider_completion_status"], "provider-completed")
        self.assertEqual(runtime["provider_completion"]["probe_resubmit_count"], "1")
        # The SELECTED attempt carries the count too (readiness allowlist).
        self.assertEqual(state["attempts"][0]["readiness"]["probe_resubmit_count"], "1")
        # The poll used the patched sleep (a default bound at import would sleep for real).
        self.assertGreaterEqual(events.count("sleep"), self._resubmit_after())
        self.assertNotIn(marker, json.dumps(runtime, sort_keys=True))

    def test_probe_submits_wait_for_the_paste_to_render(self) -> None:
        # T-4362: T-3026 attempts 1 and 2 stranded the probe in a freshly booted
        # composer (the fixed-delay Enter went in as a CR). The first submit and
        # every re-type pass send_keys_submit's render gate.
        with tempfile.TemporaryDirectory() as tmp:
            self._run_provider_completion_selector(
                Path(tmp), mode="swallowed-once", capture_count=60
            )
        self.assertEqual(len(self._submit_argvs), 2)
        for argv in self._submit_argvs:
            self.assertEqual(argv[argv.index("--render-wait") + 1],
                             f"{rc.FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS:g}")

    def test_initial_viability_probe_resubmits_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, _marker = self._run_provider_completion_selector(
                root, mode="swallowed-always", capture_count=100
            )
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(events.count("provider-submit"), 1 + rc.PROVIDER_COMPLETION_MAX_RESUBMITS)
        readiness = state["attempts"][0]["readiness"]
        self.assertEqual(state["attempts"][0]["cause"], "provider-completion-not-proven")
        self.assertEqual(readiness["probe_resubmit_count"], str(rc.PROVIDER_COMPLETION_MAX_RESUBMITS))

    def test_initial_viability_accepted_probe_is_never_resubmitted(self) -> None:
        # The prompt reached history but no reply came: a slow or dead
        # provider, not a swallowed paste. Re-typing would only queue a
        # duplicate turn.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, events, _marker = self._run_provider_completion_selector(
                root, mode="accepted-slow", capture_count=60
            )
            state = rc.initial_viability_state("recovery", state_root=root)
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(events.count("provider-submit"), 1)
        self.assertEqual(state["attempts"][0]["readiness"]["probe_resubmit_count"], "0")

    def test_initial_viability_no_history_file_is_not_read_as_swallowed(self) -> None:
        # No history.jsonl means "can't tell", never "swallowed".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _selection, events, _marker = self._run_provider_completion_selector(
                root, mode="none", capture_count=60
            )
        self.assertEqual(events.count("provider-submit"), 1)

    def test_initial_viability_never_retypes_into_a_composer_holding_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _selection, events, _marker = self._run_provider_completion_selector(
                root, mode="pending-in-composer", capture_count=60
            )
        self.assertEqual(events.count("provider-submit"), 1)

    def _resubmit_after(self) -> int:
        return math.ceil(rc.PROVIDER_COMPLETION_RESUBMIT_AFTER_SECONDS / rc.PROVIDER_COMPLETION_POLL_SECONDS)

    def test_initial_viability_history_inside_the_grace_is_never_resubmitted(self) -> None:
        # T-4321 r1 R2(a): a taken probe whose history entry lands late, but inside
        # the grace, must not be typed twice. Just past the grace it is re-typed.
        grace = self._resubmit_after()
        for landed_at, submits in ((10, 1), (grace - 1, 1), (grace, 1), (grace + 1, 2)):
            with self.subTest(history_at=landed_at), tempfile.TemporaryDirectory() as tmp:
                _selection, events, _marker = self._run_provider_completion_selector(
                    Path(tmp), mode="swallowed-always", capture_count=grace * 2, history_at=landed_at
                )
                self.assertEqual(events.count("provider-submit"), submits)

    def test_initial_viability_resubmits_are_spaced_by_the_grace(self) -> None:
        # T-4321 r1 R2(b): each re-type waits a full grace after the previous one.
        grace = self._resubmit_after()
        with tempfile.TemporaryDirectory() as tmp:
            _selection, events, _marker = self._run_provider_completion_selector(
                Path(tmp), mode="swallowed-always", capture_count=grace * 5
            )
        gaps, since = [], None
        for event in events:
            if event == "provider-submit":
                if since is not None:
                    gaps.append(since)
                since = 0
            elif since is not None and (event.startswith(("claude:", "codex:")) or event == "extra"):
                since += 1
        self.assertEqual(len(gaps), rc.PROVIDER_COMPLETION_MAX_RESUBMITS)
        self.assertTrue(all(gap >= grace for gap in gaps), gaps)

    def test_initial_viability_never_retypes_into_a_pane_that_cannot_take_input(self) -> None:
        # T-4321 r1 R2(c): the re-type is gated on the same readiness predicate as
        # the first submit: a confirm dialog (its ❯ is a selector) or a frame with
        # no composer glyph gets nothing typed into it.
        dialog = ("some output\n╭──────────────────────────╮\n│ Do you want to proceed? │\n"
                  "│ ❯ 1. Yes                 │\n│   2. No                  │\n"
                  "╰──────────────────────────╯\n Enter to confirm · Esc to cancel\n")
        no_glyph = "some output\n╭──────────╮\n│ Loading… │\n╰──────────╯\n"
        # T-4459: frame + ❯ drawn over a RUNNING turn (attempt 4's second probe).
        busy = "✻ Cogitating… (4s · esc to interrupt)\n╭──────────╮\n❯ \n╰──────────╯\n"
        for label, pane in (("dialog", dialog), ("no-glyph", no_glyph), ("busy", busy)):
            with self.subTest(pane=label), tempfile.TemporaryDirectory() as tmp:
                _selection, events, _marker = self._run_provider_completion_selector(
                    Path(tmp), mode="swallowed-always", capture_count=self._resubmit_after() * 3,
                    pane_after_submit=pane,
                )
                self.assertEqual(events.count("provider-submit"), 1)

    def test_initial_viability_failed_retype_still_fails_over_cleanly(self) -> None:
        # T-4321 r1 R1: send_keys_submit exits 2 when a busy pane queues the text
        # (T-4117) and 1 on a tmux/mode failure; submit_to_runtime raises on both.
        # A failed re-type must stop the watch, not crash the launch: the
        # candidate is still torn down and the fallback selected.
        for code in (2, 1):
            with self.subTest(resubmit_rc=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                selection, events, _marker = self._run_provider_completion_selector(
                    root, mode="swallowed-always", capture_count=self._resubmit_after() * 5,
                    resubmit_rc=code,
                )
                state = rc.initial_viability_state("recovery", state_root=root)
                self.assertEqual(selection.binding.launch.harness, "codex")
                self.assertEqual(events.count("provider-submit"), 2)
                self.assertIn(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}", events)
                self.assertEqual(state["attempts"][0]["cause"], "provider-completion-not-proven")
                self.assertEqual(state["attempts"][0]["readiness"]["probe_resubmit_count"], "0")

    def _skips(self, readiness: Mapping[str, str]) -> dict[str, int]:
        pairs = [item.split("=") for item in readiness.get("probe_resubmit_skips", "").split(",") if item]
        return {reason: int(count) for reason, count in pairs}

    def test_never_proven_probe_records_why_each_retype_check_held(self) -> None:
        # T-4362: T-3026 attempt 2 ended with probe_resubmit_count 0 and nothing
        # to say which gate held the re-type. Each held check is now labelled.
        grace = self._resubmit_after()
        dialog = ("some output\n╭──────────────────────────╮\n│ Do you want to proceed? │\n"
                  "│ ❯ 1. Yes                 │\n╰──────────────────────────╯\n"
                  " Enter to confirm · Esc to cancel\n")
        no_glyph = "some output\n╭──────────╮\n│ Loading… │\n╰──────────╯\n"
        cases = (
            ("dialog", {"mode": "swallowed-always", "pane_after_submit": dialog}, "not_ready:dialog_footer"),
            ("no-glyph", {"mode": "swallowed-always", "pane_after_submit": no_glyph}, "not_ready:no_composer_glyph"),
            ("no-frame", {"mode": "swallowed-always", "pane_after_submit": "booting ❯\n"}, "not_ready:no_frame"),
            ("empty", {"mode": "swallowed-always", "pane_after_submit": ""}, "not_ready:empty"),
            ("pending", {"mode": "pending-in-composer"}, "probe_in_composer"),
            ("busy", {"mode": "swallowed-always",
                      "pane_after_submit": "✻ Cogitating… (esc to interrupt)\n╭──────────╮\n❯ \n╰──────────╯\n"},
             "busy:generating"),
        )
        for label, kwargs, reason in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._run_provider_completion_selector(root, capture_count=grace * 3, **kwargs)
                readiness = rc.initial_viability_state("recovery", state_root=root)["attempts"][0]["readiness"]
                # Every capture from the grace on is a held check.
                self.assertEqual(self._skips(readiness), {reason: grace * 3 - grace + 1})

    def test_never_proven_probe_records_a_watch_that_ended(self) -> None:
        grace = self._resubmit_after()
        cases = (
            ("taken", {"mode": "accepted-slow"}, {"submitted": 1}),
            ("no-history", {"mode": "none"}, {"submitted_unknown": 1}),
            ("retype-failed", {"mode": "swallowed-always", "resubmit_rc": 2}, {"retype_failed": 1}),
        )
        for label, kwargs, expected in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._run_provider_completion_selector(root, capture_count=grace * 5, **kwargs)
                readiness = rc.initial_viability_state("recovery", state_root=root)["attempts"][0]["readiness"]
                self.assertEqual(self._skips(readiness), expected)

    def test_never_proven_probe_keeps_its_panes_in_a_private_file_not_evidence(self) -> None:
        # T-4362 ask (1): the raw panes go to a 0600 file under the persona's
        # controller state; the attempt evidence carries only its path and hash.
        grace = self._resubmit_after()
        pane = "some output\n╭──────────╮\n│ Loading… │\n╰──────────╯\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _selection, _events, marker = self._run_provider_completion_selector(
                root, mode="swallowed-always", capture_count=grace * 2, pane_after_submit=pane
            )
            state = rc.initial_viability_state("recovery", state_root=root)
            readiness = state["attempts"][0]["readiness"]
            path = Path(readiness["pane_captures_path"])
            body = path.read_bytes()
            mode = stat.S_IMODE(path.stat().st_mode)
            dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
        self.assertEqual(path.parent, root / "recovery" / "runtime-controller" / "provider-completion-captures")
        self.assertEqual((mode, dir_mode), (0o600, 0o700))
        self.assertEqual(readiness["pane_captures_sha256"], hashlib.sha256(body).hexdigest())
        text = body.decode("utf-8")
        # The baseline, the first capture after the submit and the last three.
        last = grace * 2
        headers = re.findall(r"===== capture (\d+) ", text)
        self.assertEqual([int(item) for item in headers], [0, 1, last - 2, last - 1, last])
        self.assertIn("not_ready=no_composer_glyph", text)
        self.assertIn("│ Loading… │", text)
        self.assertNotIn(marker, text)
        # The evidence itself never carries pane text.
        self.assertNotIn("Loading", json.dumps(state, sort_keys=True))

    def test_proven_probe_writes_no_pane_captures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection, _events, _marker = self._run_provider_completion_selector(root, mode="success")
            runtime = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
            captures = root / "recovery" / "runtime-controller" / "provider-completion-captures"
            self.assertFalse(captures.exists())
        self.assertEqual(selection.binding.launch.harness, "claude")
        self.assertNotIn("pane_captures_path", runtime["provider_completion"])

    def test_pane_capture_redacts_before_the_cut(self) -> None:
        # T-4376: cutting first left the head of a key that straddled the cut,
        # too short for any shape to match. Here the cut leaves "sk-" + 7 chars,
        # under both the sk- minimum (16) and the blob minimum (40).
        cut = rc.PROVIDER_COMPLETION_CAPTURE_MAX_CHARS
        prefix = "x " * ((cut - 10) // 2)
        text = prefix + "sk-" + "Q" * 40
        self.assertEqual(len(prefix) + 3 + 7, cut)
        sample = rc._post_start_sample(text, capture_index=1)
        binding = _binding(Path("/nonexistent"))
        with tempfile.TemporaryDirectory() as tmp:
            path, _sha, error = rc._write_provider_completion_captures(
                binding, state_root=Path(tmp), nonce_marker="", samples=(sample,), skips="")
            body = Path(path).read_text(encoding="utf-8")
        self.assertIsNone(error)
        self.assertNotIn("QQQ", body)
        self.assertNotIn("sk-", body)

    def test_pane_capture_writer_never_raises(self) -> None:
        # T-4376: the docstring promised "never raises" but only OSError was caught.
        # T-4399 (rev #601 MG): more than OSError and ValueError, so a catch narrowed
        # back to those two fails here; and (N4) a writer BUG leaves a traceback on
        # stderr, while an environment fault (OSError) stays a one-word class name.
        binding = _binding(Path("/nonexistent"))
        sample = rc._post_start_sample("pane", capture_index=1)
        for exc, traced in ((ValueError("bug"), True), (KeyError("bug"), True),
                            (TypeError("bug"), True), (OSError("disk full"), False)):
            with self.subTest(type(exc).__name__), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(rc, "not_ready_reason", side_effect=exc), \
                    mock.patch.object(rc.sys, "stderr", new_callable=io.StringIO) as err:
                result = rc._write_provider_completion_captures(
                    binding, state_root=Path(tmp), nonce_marker="", samples=(sample,), skips="")
            self.assertEqual(result, (None, None, type(exc).__name__))
            self.assertEqual("Traceback" in err.getvalue(), traced)

    def test_pane_capture_writer_lets_an_interrupt_through(self) -> None:
        # T-4399 (rev #601 MG2): "never raises" means no Exception; an interrupt or exit
        # must still stop the controller, so a catch widened to BaseException fails here.
        binding = _binding(Path("/nonexistent"))
        sample = rc._post_start_sample("pane", capture_index=1)
        for exc in (KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(type(exc).__name__), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(rc, "not_ready_reason", side_effect=exc):
                with self.assertRaises(type(exc)):
                    rc._write_provider_completion_captures(
                        binding, state_root=Path(tmp), nonce_marker="", samples=(sample,), skips="")

    def test_pane_captures_are_pruned_to_the_newest(self) -> None:
        # T-4376: ~80 KB per never-proven candidate, and nothing removed them.
        binding = _binding(Path("/nonexistent"))
        sample = rc._post_start_sample("pane", capture_index=1)
        keep = rc.PROVIDER_COMPLETION_CAPTURE_FILES_KEPT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = rc._persona_state(binding.persona, root) / "provider-completion-captures"
            directory.mkdir(parents=True)
            old = [directory / f"20260101T0000{i:02d}000000Z-claude-x.txt" for i in range(keep + 5)]
            for p in old:
                p.write_text("old", encoding="utf-8")
            path, _sha, error = rc._write_provider_completion_captures(
                binding, state_root=root, nonce_marker="", samples=(sample,), skips="")
            remaining = sorted(p.name for p in directory.iterdir())
        self.assertIsNone(error)
        self.assertEqual(len(remaining), keep)
        self.assertEqual(remaining[-1], Path(path).name)
        self.assertEqual(remaining[0], old[6].name)  # the 6 oldest went

    def test_a_failed_prune_does_not_report_the_written_capture_as_failed(self) -> None:
        binding = _binding(Path("/nonexistent"))
        sample = rc._post_start_sample("pane", capture_index=1)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(Path, "glob", side_effect=PermissionError("listing")):
            path, sha, error = rc._write_provider_completion_captures(
                binding, state_root=Path(tmp), nonce_marker="", samples=(sample,), skips="")
            self.assertTrue(Path(path).is_file())
        self.assertIsNone(error)
        self.assertIsNotNone(sha)

    def test_pane_capture_write_failure_never_fails_the_launch(self) -> None:
        grace = self._resubmit_after()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A file where the captures directory should be: mkdir fails.
            blocker = root / "recovery" / "runtime-controller" / "provider-completion-captures"
            blocker.parent.mkdir(parents=True)
            blocker.write_text("not a directory", encoding="utf-8")
            selection, _events, _marker = self._run_provider_completion_selector(
                root, mode="swallowed-always", capture_count=grace * 2,
                pane_after_submit="some output\n",
            )
            readiness = rc.initial_viability_state("recovery", state_root=root)["attempts"][0]["readiness"]
        self.assertEqual(selection.binding.launch.harness, "codex")
        self.assertEqual(readiness["pane_captures_error"], "FileExistsError")
        self.assertNotIn("pane_captures_path", readiness)

    def test_pane_capture_redaction(self) -> None:
        marker = "PANTHEON_PROVIDER_COMPLETION_0123abcd"
        cases = (
            (f"❯ reply with {marker} now", "❯ reply with <probe-nonce> now"),
            ("PANTHEON_PROVIDER_COMPLETION_ffff9999", "<probe-nonce>"),
            ("key sk-ant-api03-AbCdEfGhIjKlMnOpQr here", "key <redacted-key> here"),
            ("bot 123456789:AAEhBOweik6ad9r_Qvxx1234567890abcdE ok", "bot <redacted-bot-token> ok"),
            ("TOKEN=hunter2 and password: swordfish", "TOKEN=<redacted> and password: <redacted>"),
            ("blob " + "z" * 48, "blob <redacted-blob>"),
            ("blob " + "a" * 48, "blob <redacted-hex>"),  # all-hex: the hex shape wins
            ("╭──────────╮ plain ❯ text 42", "╭──────────╮ plain ❯ text 42"),
            # T-4376 (rev-t4362): shapes the first cut missed.
            ("GITHUB_TOKEN=ghp_abc123 MY_API_KEY=xyz", "GITHUB_TOKEN=<redacted> MY_API_KEY=<redacted>"),
            ('{"token": "abc", "api_key": "k1"}', '{"token": "<redacted>", "api_key": "<redacted>"}'),
            ("passphrase: hunter2", "passphrase: <redacted>"),
            ("Authorization: Bearer abc.def", "Authorization: Bearer <redacted>"),
            ("id " + "0f" * 16 + " ok", "id <redacted-hex> ok"),
            ("git clone https://recovery:s3cret@github.com/x", "git clone https://<redacted>@github.com/x"),
            ("key AKIAABCDEFGHIJKLMNOP here", "key <redacted-aws-key> here"),
            # T-4399 (rev #601 MA): the temporary-credential prefix too, not only AKIA.
            ("key ASIAABCDEFGHIJKLMNOP here", "key <redacted-aws-key> here"),
            ("↓ 3 tokens) · 12k tokens", "↓ 3 tokens) · 12k tokens"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(rc._redact_pane_capture(raw, marker), expected)

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

            def start(binding, adapter, *, replace=False, context=None, state_root=None):
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

    def test_initial_viability_receiver_failure_stops_the_codex_scope_it_launched(self) -> None:
        # T-4336: a failed start-codex-poller used to leave the codex pane up, deaf to
        # Telegram, until something else rolled it back.
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
            attempt = rc.initial_viability_state("recovery", state_root=root)["attempts"][-1]
            evidence_path = root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
            evidence = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        stop = f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"
        self.assertEqual(events.count(stop), 1)
        self.assertLess(events.index("codex:probe-codex-absent"), events.index(stop))
        self.assertEqual(attempt["cause"], "receiver_handoff_failed")
        self.assertEqual(
            attempt["teardown"],
            {"runtime_state_cleared": "true", "scope_stopped": "true", "runtime_scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT},
        )
        teardown = [e for e in evidence if e.get("event") == "runtime_viability_candidate_teardown"][-1]
        self.assertEqual(teardown["reason"], "receiver_handoff_failed")
        self.assertEqual(teardown["scope_stopped"], "true")

    def test_receiver_failure_leaves_a_runtime_it_only_attached_to(self) -> None:
        # T-4101: ensure_runtime may attach to a live runtime; that scope isn't ours to stop.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, fallback = self._bindings(root)
            boundary = rc._utc_now()
            attached = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, None,
                                        boundary - timedelta(hours=1))
            rc._atomic_json(root / "recovery" / "runtime-controller" / "runtime.json",
                            {"runtime_scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT})
            with mock.patch.object(rc, "_stop_created_scope") as stop:
                result = rc._stop_scope_this_attempt_created(fallback, attached, launched_at=boundary,
                                                             existed_before=False, state_root=root)
                stop.assert_not_called()
                self.assertEqual(result["scope_stop_skipped"], "attached-existing-runtime")
                fresh = dataclasses.replace(attached, started_at=boundary)
                (root / "recovery" / "runtime-controller" / "runtime.json").unlink()
                result = rc._stop_scope_this_attempt_created(fallback, fresh, launched_at=boundary,
                                                             existed_before=False, state_root=root)
                stop.assert_not_called()
                self.assertEqual(result["scope_stop_skipped"], "no-recorded-scope")

    def _receiver_failure_selector(self, root, events, *, pane_up_before=False, stop=None, fresh_attach=False):
        """A codex-only viability run (claude capped) whose start-codex-poller fails."""
        primary, fallback = self._bindings(root)
        adapters = {
            "claude": _Adapter(primary, _quota_snapshot(primary, root, evidence={"authoritative_limit": "true"})),
            "codex": _Adapter(fallback),
        }
        with contextlib.ExitStack() as stack:
            for p in self._selector_patches(events):
                stack.enter_context(p)
            if pane_up_before:
                # Only the pre-launch check: a live pane is otherwise blocked earlier.
                stack.enter_context(mock.patch.object(rc, "_runtime_pane_existed", return_value=True))
            if fresh_attach:
                # An attached runtime whose runtime.json had no usable started_at:
                # _handle_from_state falls back to now(), so it looks brand new.
                def attach(binding, adapter, **kwargs):
                    del adapter, kwargs
                    rc._atomic_json(root / "recovery" / "runtime-controller" / "runtime.json",
                                    {"runtime_scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT})
                    return rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())

                stack.enter_context(mock.patch.object(rc, "ensure_runtime", side_effect=attach))
            if stop is not None:
                stack.enter_context(mock.patch.object(rc, "_stop_created_scope", side_effect=stop))
            return rc.ensure_viable_runtime(
                (primary, fallback),
                adapter_factory=lambda binding: adapters[binding.launch.harness],
                state_root=root,
                backend=_ViabilityBackend(events, fail="start-codex-poller"),
            )

    def _teardown_evidence(self, root):
        path = root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r.get("event") == "runtime_viability_candidate_teardown"]

    def test_receiver_failure_never_stops_a_pane_that_was_up_before_even_if_it_looks_new(self) -> None:
        # T-4338 NIT 1: started_at alone misreads an attached runtime as created.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            with self.assertRaises(rc.RuntimeViabilityExhausted):
                self._receiver_failure_selector(root, events, pane_up_before=True, fresh_attach=True)
            attempt = rc.initial_viability_state("recovery", state_root=root)["attempts"][-1]
        self.assertNotIn(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}", events)
        self.assertEqual(attempt["teardown"]["scope_stop_skipped"], "attached-existing-runtime")

    def test_the_pre_launch_pane_check_reads_doubt_as_up(self) -> None:
        _, fallback = self._bindings(Path("/nonexistent"))
        for probe, expected in ((False, False), (True, True), (RuntimeError("tmux gone"), True)):
            with self.subTest(probe=repr(probe)):
                kwargs = {"side_effect": probe} if isinstance(probe, Exception) else {"return_value": probe}
                with mock.patch.object(rc, "pane_exists", **kwargs):
                    self.assertIs(rc._runtime_pane_existed(fallback), expected)

    def test_the_loop_asks_whether_the_pane_was_up_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            order: list[str] = []
            real_ensure = rc.ensure_runtime
            with mock.patch.object(rc, "_runtime_pane_existed",
                                   side_effect=lambda b: order.append("pre-check") or False), \
                    mock.patch.object(rc, "ensure_runtime",
                                      side_effect=lambda *a, **k: order.append("ensure") or real_ensure(*a, **k)):
                with self.assertRaises(rc.RuntimeViabilityExhausted):
                    self._receiver_failure_selector(root, events)
        self.assertEqual(order[:2], ["pre-check", "ensure"])

    def test_a_refused_scope_stop_in_the_receiver_branch_fails_loud_with_a_record(self) -> None:
        # T-4338 NIT 2 + the missing D2 test: a candidate still running must not be
        # recorded as torn down, and the viability run must not move on beside it.
        def refuse(scope):
            raise rc.ScopeStopRefused(f"systemctl --user stop {scope} refused (rc 1): Access denied")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            with self.assertRaisesRegex(RuntimeError, "initial receiver compensation failed.*refused"):
                self._receiver_failure_selector(root, events, stop=refuse)
            teardown = self._teardown_evidence(root)[-1]
            runtime_state = rc._read_json(root / "recovery" / "runtime-controller" / "runtime.json")
        self.assertEqual(teardown["reason"], "receiver_handoff_failed")
        self.assertEqual(teardown["scope_stopped"], "false")
        self.assertIn("Access denied", teardown["scope_stop_error"])
        self.assertEqual(teardown["runtime_state_cleared"], "false")
        # Kept: it still describes the live pane.
        self.assertEqual(runtime_state["runtime_scope_unit"], rc.TMUX_RUNTIME_SCOPE_UNIT)

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

            def start(binding, adapter, *, replace=False, context=None, state_root=None):
                del adapter, replace, context
                events.append(f"{binding.launch.harness}:start")
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

            def verify_input(binding, started, *, state_root=None, adapter=None, context=None, launched_at=None):
                del state_root, adapter, context, launched_at
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


class CompensateFailedInitialCandidateReceiverGateTests(unittest.TestCase):
    """T-3826: compensate_failed_initial_candidate must not attempt a receiver
    proof for a mailbox-only persona (communications is None, per ORG-043).

    Regression for the T-3819 mason reconcile crash: the unconditional
    _prove_receiver descended into the telegram bridge and raised ManifestError
    for a persona with no managed receiver, wedging persona-consume every tick.
    """

    def _failure(self):
        return rc.RuntimeOperationalFailure(
            "runtime failed input readiness verification",
            failure_class="operational-readiness",
            cause="input-readiness",
            binding_runtime="claude/interactive",
            runtime_start=None,
        )

    def test_skips_receiver_proof_for_mailbox_only_persona(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", communications=None)
            binding.persona = "mason"
            with mock.patch.object(rc, "_prove_receiver") as prove:
                teardown = rc.compensate_failed_initial_candidate(
                    binding,
                    failure=self._failure(),
                    state_root=root,
                    backend=None,
                    reason="test-mailbox-only",
                )
            prove.assert_not_called()
            self.assertEqual(teardown["receiver_absent_proven"], "not-applicable")

    def test_skips_receiver_proof_for_mailbox_only_persona_even_with_backend(self) -> None:
        # The invariant "no receiver => never prove one" must hold regardless of
        # an injected backend — the sole production caller passes backend through,
        # which is why the gate is `receiver_handoff_required(binding)` alone and
        # not _probe_managed_receiver's `backend is None and not ...` form.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", communications=None)
            binding.persona = "mason"
            with mock.patch.object(rc, "_prove_receiver") as prove:
                teardown = rc.compensate_failed_initial_candidate(
                    binding,
                    failure=self._failure(),
                    state_root=root,
                    backend=object(),
                    reason="test-mailbox-only-backend",
                )
            prove.assert_not_called()
            self.assertEqual(teardown["receiver_absent_proven"], "not-applicable")

    def test_proves_receiver_for_telegram_persona(self) -> None:
        # Guard must not over-skip: a telegram-managed persona still proves absent.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex", communications=_telegram_comms())
            with mock.patch.object(rc, "_prove_receiver") as prove:
                teardown = rc.compensate_failed_initial_candidate(
                    binding,
                    failure=self._failure(),
                    state_root=root,
                    backend=None,
                    reason="test-telegram",
                )
            prove.assert_called_once()
            self.assertEqual(teardown["receiver_absent_proven"], "true")


class CandidateReceiverWindowT4332Tests(unittest.TestCase):
    """T-4332: a claude candidate's runtime-owned Telegram plugin can confirm
    operator messages before any turn reads them, so its teardown must record the
    window and send ONE notice to the seat's own inbox (the loss is otherwise
    silent: Telegram never redelivers a confirmed update)."""

    LAUNCH = datetime(2026, 9, 29, 20, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bot_pid = self.root / ".claude" / "channels" / "telegram" / "bot.pid"
        self.inbox = self.root / "mail" / "recovery"
        mailbox.provision(self.inbox)

    def _plugin_started(self, at: datetime, pid: str = "3293548") -> None:
        self.bot_pid.parent.mkdir(parents=True, exist_ok=True)
        self.bot_pid.write_text(pid + "\n", encoding="utf-8")
        os.utime(self.bot_pid, (at.timestamp(), at.timestamp()))

    def _teardown(self, binding=None, *, launched_at=LAUNCH, prove=None):
        binding = binding or _binding(self.root, harness="claude", communications=_telegram_comms())
        failure = rc.RuntimeOperationalFailure(
            "provider completion was not proven",
            failure_class="operational-readiness",
            cause="provider-completion-not-proven",
            binding_runtime="claude/interactive",
            runtime_start=None,
        )
        with mock.patch.object(rc, "_prove_receiver", side_effect=prove):
            return rc.compensate_failed_initial_candidate(
                binding, failure=failure, state_root=self.root, backend=None,
                reason="provider-completion-not-proven", launched_at=launched_at,
            )

    def _notices(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.inbox / "new").iterdir())]

    def _evidence(self) -> dict:
        path = self.root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if r.get("event") == "runtime_viability_candidate_teardown"][-1]

    def test_a_candidate_whose_plugin_started_records_the_window_and_notifies_once(self) -> None:
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        teardown = self._teardown()
        self.assertEqual(teardown["receiver_window_start"], rc._iso(self.LAUNCH + timedelta(seconds=12)))
        self.assertIn("receiver_window_end", teardown)
        self.assertEqual(teardown["receiver_bot_pid"], "3293548")
        self.assertEqual(teardown["receiver_window_notice"], "sent")
        self.assertEqual(teardown["receiver_absent_proven"], "true")
        evidence = self._evidence()
        self.assertEqual(evidence["receiver_window_start"], teardown["receiver_window_start"])
        self.assertEqual(evidence["receiver_window_notice"], "sent")
        notices = self._notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["sender"], rc.CANDIDATE_RECEIVER_NOTICE_SENDER)
        payload = notices[0]["payload"]
        self.assertEqual(payload["kind"], "candidate_receiver_window")
        self.assertIn("resend", payload["text"])
        self.assertIn(teardown["receiver_window_start"], payload["text"])

    def test_a_bot_pid_older_than_the_launch_is_an_earlier_session(self) -> None:
        self._plugin_started(self.LAUNCH - timedelta(seconds=5))
        teardown = self._teardown()
        self.assertNotIn("receiver_window_start", teardown)
        self.assertNotIn("receiver_window_notice", teardown)
        self.assertEqual(self._notices(), [])

    def test_a_bot_pid_a_coarse_clock_tick_before_the_boundary_still_counts(self) -> None:
        # The kernel stamps mtimes from a coarse clock that can lag datetime.now().
        self._plugin_started(self.LAUNCH - timedelta(milliseconds=500))
        self.assertEqual(self._teardown()["receiver_window_notice"], "sent")

    def test_no_bot_pid_means_the_plugin_never_started(self) -> None:
        teardown = self._teardown()
        self.assertNotIn("receiver_window_start", teardown)
        self.assertEqual(self._notices(), [])

    def test_no_launch_boundary_means_no_claim(self) -> None:
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        self.assertNotIn("receiver_window_start", self._teardown(launched_at=None))
        self.assertEqual(self._notices(), [])

    def test_only_a_runtime_owned_claude_receiver_dies_with_the_candidate(self) -> None:
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        systemd_user = dict(_telegram_comms(), claude_receiver_topology="systemd-user")
        cases = {
            "codex candidate": _binding(self.root, harness="codex", communications=_telegram_comms()),
            "systemd-user receiver": _binding(self.root, harness="claude", communications=systemd_user),
            "mailbox-only persona": _binding(self.root, harness="claude", communications=None),
        }
        for label, binding in cases.items():
            with self.subTest(case=label):
                self.assertNotIn("receiver_window_start", self._teardown(binding))
        self.assertEqual(self._notices(), [])

    def test_an_unreadable_bot_pid_is_cant_tell_not_no(self) -> None:
        real_stat = Path.stat

        def stat(path, *a, **k):
            if path == self.bot_pid:
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *a, **k)

        with mock.patch.object(Path, "stat", stat):
            teardown = self._teardown()
        self.assertEqual(teardown["receiver_window_start"], "unknown")
        self.assertIn("PermissionError", teardown["receiver_window_detail"])
        self.assertEqual(teardown["receiver_window_notice"], "sent")

    def test_a_failed_notice_never_breaks_the_teardown(self) -> None:
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        shutil.rmtree(self.inbox)
        teardown = self._teardown()
        self.assertTrue(teardown["receiver_window_notice"].startswith("failed: "))
        self.assertEqual(teardown["runtime_state_cleared"], "true")
        self.assertEqual(teardown["receiver_absent_proven"], "true")

    def test_the_notice_does_not_depend_on_the_absence_proof(self) -> None:
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        with self.assertRaisesRegex(RuntimeError, "still present"):
            self._teardown(prove=RuntimeError("receiver state is present; still present"))
        self.assertEqual(len(self._notices()), 1)

    def test_a_clean_stop_before_the_raise_still_reports_the_window(self) -> None:
        # r2 B1: input-readiness stops a created scope BEFORE raising, and the
        # plugin removes bot.pid on that SIGTERM. The window must be read first and
        # carried to the teardown, through the REAL _verify_runtime_input_ready.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        binding = _binding(self.root, harness="claude", communications=_telegram_comms())
        started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
        not_ready = rc.RuntimeInputReadiness(
            ready=False, pane_sha256="c" * 64, reason="runtime input readiness timed out"
        )
        with mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=not_ready), \
                mock.patch.object(rc, "_stop_created_scope", side_effect=lambda scope: self.bot_pid.unlink(missing_ok=True)), \
                mock.patch.object(rc, "_prove_receiver"):
            with self.assertRaises(rc.RuntimeOperationalFailure) as caught:
                rc._verify_runtime_input_ready(binding, started, state_root=self.root, launched_at=self.LAUNCH)
            self.assertFalse(self.bot_pid.exists())
            teardown = rc.compensate_failed_initial_candidate(
                binding, failure=caught.exception, state_root=self.root, backend=None,
                reason=caught.exception.cause, launched_at=self.LAUNCH,
            )
        started_at = rc._iso(self.LAUNCH + timedelta(seconds=12))
        self.assertEqual(caught.exception.readiness_evidence["receiver_window_start"], started_at)
        self.assertEqual(teardown["receiver_window_start"], started_at)
        self.assertEqual(teardown["receiver_window_notice"], "sent")
        self.assertEqual(len(self._notices()), 1)

    def _verify_then_compensate(self, stat):
        """The real _verify_runtime_input_ready (not ready), then the real compensate.
        The scope stop removes bot.pid, as the plugin's own SIGTERM shutdown does;
        `stat` stands in for Path.stat until the stop runs."""
        binding = _binding(self.root, harness="claude", communications=_telegram_comms())
        started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
        not_ready = rc.RuntimeInputReadiness(
            ready=False, pane_sha256="c" * 64, reason="runtime input readiness timed out"
        )
        real_stat, stops = Path.stat, []

        def stop(scope):
            stops.append(scope)
            self.bot_pid.unlink(missing_ok=True)

        def patched_stat(path, *a, **k):
            return stat(path, *a, **k) if not stops and path == self.bot_pid else real_stat(path, *a, **k)

        with mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=not_ready), \
                mock.patch.object(rc, "_stop_created_scope", side_effect=stop), \
                mock.patch.object(Path, "stat", patched_stat), \
                mock.patch.object(rc, "_prove_receiver"):
            with self.assertRaises(rc.RuntimeOperationalFailure) as caught:
                rc._verify_runtime_input_ready(binding, started, state_root=self.root, launched_at=self.LAUNCH)
            stops_before_raise = list(stops)
            teardown = rc.compensate_failed_initial_candidate(
                binding, failure=caught.exception, state_root=self.root, backend=None,
                reason=caught.exception.cause, launched_at=self.LAUNCH,
            )
        return caught.exception, teardown, stops_before_raise

    def test_a_window_read_raising_a_non_os_error_still_stops_the_scope(self) -> None:
        # T-4356 N-a (rev-t4332 r2): fromtimestamp on an absurd mtime raises
        # ValueError/OverflowError. The unguarded pre-raise read skipped the scope
        # stop and escaped as a non-RuntimeOperationalFailure (stops=0, notices=0).
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        for label, mtime in (("OverflowError", 1e300), ("ValueError", float("nan"))):
            with self.subTest(label):
                for p in (self.inbox / "new").iterdir():
                    p.unlink()
                self._plugin_started(self.LAUNCH + timedelta(seconds=12))
                failure, teardown, stops = self._verify_then_compensate(
                    lambda path, *a, **k: SimpleNamespace(st_mtime=mtime))
                self.assertEqual(stops, [rc.TMUX_RUNTIME_SCOPE_UNIT])
                self.assertEqual(failure.readiness_evidence["receiver_window_start"], "unknown")
                self.assertIn("window read failed", failure.readiness_evidence["receiver_window_detail"])
                self.assertEqual(teardown["receiver_window_start"], "unknown")
                self.assertEqual(teardown["receiver_window_notice"], "sent")
                self.assertEqual(len(self._notices()), 1)

    def test_compensates_own_window_read_never_skips_the_stop(self) -> None:
        # T-4356: the same guard on compensate's read. On a provider-completion
        # failure (T-3026 attempt 2's path) nothing stopped the scope before the
        # raise, so compensate's read is the first, and a raise there skipped its stop.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        binding = _binding(self.root, harness="claude", communications=_telegram_comms())
        failure = rc.RuntimeOperationalFailure(
            "provider completion was not proven",
            failure_class="operational-readiness",
            cause="provider-completion-not-proven",
            binding_runtime="claude/interactive",
            runtime_start=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
        )
        real_stat, stops = Path.stat, []

        def patched_stat(path, *a, **k):
            return SimpleNamespace(st_mtime=1e300) if path == self.bot_pid else real_stat(path, *a, **k)

        with mock.patch.object(Path, "stat", patched_stat), \
                mock.patch.object(rc, "_stop_created_scope", side_effect=stops.append), \
                mock.patch.object(rc, "_prove_receiver"):
            teardown = rc.compensate_failed_initial_candidate(
                binding, failure=failure, state_root=self.root, backend=None,
                reason="provider-completion-not-proven", launched_at=self.LAUNCH,
            )
        self.assertEqual(stops, [rc.TMUX_RUNTIME_SCOPE_UNIT])
        self.assertEqual(teardown["scope_stopped"], "true")
        self.assertEqual(teardown["receiver_window_start"], "unknown")
        self.assertIn("OverflowError", teardown["receiver_window_detail"])
        self.assertEqual(len(self._notices()), 1)

    def test_a_carried_unknown_window_still_notifies_once(self) -> None:
        # T-4356 N-b: the pre-raise read could not tell (bot.pid unreadable), the stop
        # then removed bot.pid, so compensate's own read finds nothing. The carried
        # "unknown" window is the only record left, and it must still notify.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))

        def denied(path, *a, **k):
            raise PermissionError(13, "Permission denied")

        failure, teardown, stops = self._verify_then_compensate(denied)
        self.assertEqual(stops, [rc.TMUX_RUNTIME_SCOPE_UNIT])
        self.assertFalse(self.bot_pid.exists())
        self.assertEqual(failure.readiness_evidence["receiver_window_start"], "unknown")
        self.assertEqual(teardown["receiver_window_start"], "unknown")
        self.assertIn("PermissionError", teardown["receiver_window_detail"])
        self.assertEqual(teardown["receiver_window_notice"], "sent")
        notices = self._notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("from unknown to", notices[0]["payload"]["text"])

    def test_the_notice_goes_out_before_the_scope_stop(self) -> None:
        # r2 N2: a stop that raises (e.g. a refused stop) must not skip the notice.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        binding = _binding(self.root, harness="claude", communications=_telegram_comms())
        failure = rc.RuntimeOperationalFailure(
            "provider completion was not proven",
            failure_class="operational-readiness",
            cause="provider-completion-not-proven",
            binding_runtime="claude/interactive",
            runtime_start=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
        )
        with mock.patch.object(rc, "_stop_created_scope", side_effect=RuntimeError("stop refused")):
            with self.assertRaisesRegex(RuntimeError, "stop refused"):
                rc.compensate_failed_initial_candidate(
                    binding, failure=failure, state_root=self.root, backend=None,
                    reason="provider-completion-not-proven", launched_at=self.LAUNCH,
                )
        self.assertEqual(len(self._notices()), 1)

    def test_a_refused_scope_stop_keeps_the_notice_and_records_it(self) -> None:
        # T-4338 on top of T-4332 r2: a refusal is ScopeStopRefused, recorded by
        # _record_refused_scope_stop and re-raised. The notice already went out, once,
        # and the refusal's evidence row says so next to scope_stopped=false.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        binding = _binding(self.root, harness="claude", communications=_telegram_comms())
        failure = rc.RuntimeOperationalFailure(
            "provider completion was not proven",
            failure_class="operational-readiness",
            cause="provider-completion-not-proven",
            binding_runtime="claude/interactive",
            runtime_start=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True),
        )
        refusal = rc.ScopeStopRefused("systemctl stop exited 1: Access denied")
        with mock.patch.object(rc, "_stop_created_scope", side_effect=refusal):
            with self.assertRaises(rc.ScopeStopRefused):
                rc.compensate_failed_initial_candidate(
                    binding, failure=failure, state_root=self.root, backend=None,
                    reason="provider-completion-not-proven", launched_at=self.LAUNCH,
                )
        self.assertEqual(len(self._notices()), 1)
        evidence = self._evidence()
        self.assertEqual(evidence["scope_stopped"], "false")
        self.assertIn("Access denied", evidence["scope_stop_error"])
        self.assertEqual(evidence["receiver_window_notice"], "sent")
        self.assertEqual(evidence["receiver_window_start"], rc._iso(self.LAUNCH + timedelta(seconds=12)))

    def test_a_notice_send_raising_a_non_os_error_never_breaks_the_teardown(self) -> None:
        # r2 N1/O2: the guard is `except Exception`, not just OSError (MailboxNotFound).
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        with mock.patch.object(mailbox, "send", side_effect=ValueError("unsafe inbox path")):
            teardown = self._teardown()
        self.assertEqual(teardown["receiver_window_notice"], "failed: ValueError: unsafe inbox path")
        self.assertEqual(teardown["receiver_absent_proven"], "true")

    def test_the_notice_is_sent_exactly_once(self) -> None:
        # r2 N1/O3: count send CALLS; the inbox's pending-dedupe would hide a double send.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        with mock.patch.object(mailbox, "send", return_value="mid") as send:
            self._teardown()
        self.assertEqual(send.call_count, 1)

    def test_the_window_ends_at_the_teardown_time(self) -> None:
        # r2 N1/O5: the end is when the teardown ran, not the launch.
        self._plugin_started(self.LAUNCH + timedelta(seconds=12))
        teardown_time = self.LAUNCH + timedelta(seconds=95)
        with mock.patch.object(rc, "_utc_now", return_value=teardown_time):
            teardown = self._teardown()
        self.assertEqual(teardown["receiver_window_end"], rc._iso(teardown_time))


def _in_nested_pid_namespace() -> bool:
    """True when this process already runs inside a non-root pid namespace (T-5166)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return len(line.split()) > 2
    except OSError:
        pass
    return False


class ScopeStopT4338Tests(unittest.TestCase):
    """T-4338 NIT 2: _stop_created_scope discarded systemctl's return code, so a
    refused stop was silent and the teardown recorded scope_stopped=true."""

    SCOPE = "pantheon-runtime-tmux.scope"

    def _systemctl(self, stop_rc: int, stderr: str = ""):
        calls: list[tuple[str, ...]] = []

        def run(*args):
            calls.append(args)
            rc_ = stop_rc if args[0] == "stop" else 0
            return subprocess.CompletedProcess(("systemctl", "--user", *args), rc_, "", stderr if args[0] == "stop" else "")

        return calls, mock.patch.object(rc, "_run_systemctl_user", side_effect=run)

    def test_a_clean_stop_resets_failed(self) -> None:
        calls, patch = self._systemctl(0)
        with patch:
            rc._stop_created_scope(self.SCOPE)
        self.assertEqual(calls, [("stop", self.SCOPE), ("reset-failed", self.SCOPE)])

    def test_an_already_gone_scope_is_not_a_refusal(self) -> None:
        # Measured: `systemctl --user stop <absent unit>` exits 5 (not loaded).
        calls, patch = self._systemctl(5, "Unit pantheon-runtime-tmux.scope not loaded.")
        with patch:
            rc._stop_created_scope(self.SCOPE)
        self.assertEqual(calls[0], ("stop", self.SCOPE))

    def test_a_refused_stop_raises_with_the_rc_and_the_reason(self) -> None:
        calls, patch = self._systemctl(1, "Access denied")
        with patch, self.assertRaisesRegex(rc.ScopeStopRefused, r"rc 1\): Access denied"):
            rc._stop_created_scope(self.SCOPE)
        self.assertEqual(calls, [("stop", self.SCOPE)])

    def test_best_effort_logs_a_refusal_instead_of_raising(self) -> None:
        _, patch = self._systemctl(1, "Access denied")
        err = io.StringIO()
        with patch, contextlib.redirect_stderr(err):
            rc._stop_created_scope_best_effort(self.SCOPE)
        self.assertIn("refused (rc 1)", err.getvalue())

    def test_pane_probe_distinguishes_absence_from_tmux_error(self) -> None:
        target = rc.TmuxTarget("seat", "runtime", "0")
        absent = subprocess.CompletedProcess([], 1, "", "can't find pane: seat:runtime.0")
        errored = subprocess.CompletedProcess([], 1, "", "error connecting to /unwritable/tmux")
        with mock.patch.object(rc, "_run_tmux", return_value=absent):
            self.assertFalse(rc.pane_exists(target))
        with mock.patch.object(rc, "_run_tmux", return_value=errored):
            with self.assertRaisesRegex(RuntimeError, "tmux pane probe failed"):
                rc.pane_exists(target)

    def test_pane_probe_accepts_only_the_requested_pane(self) -> None:
        # T-5166: rc 0 is not presence. Fixtures are real tmux 3.4 outputs.
        target = rc.TmuxTarget("a", "5", "0")
        def out(stdout):
            return subprocess.CompletedProcess([], 0, stdout, "")
        cases = {
            "a\t3\twork\t0\t%1\t0\n": False,   # missing window a:5 fell back to a:3
            "\t\t\t\t\t\n": False,              # missing session: blank fields
            "a\t5\twork\t1\t%2\t0\n": False,   # right window, wrong pane
            "a\t5\twork\t0\t%2\t1\n": False,   # the pane, but dead
            "a\t5\twork\t0\t%2\t0\n": True,
        }
        for stdout, want in cases.items():
            with self.subTest(stdout=stdout), mock.patch.object(rc, "_run_tmux", return_value=out(stdout)):
                self.assertIs(rc.pane_exists(target), want)
        named = rc.TmuxTarget("a", "work", "0")
        with mock.patch.object(rc, "_run_tmux", return_value=out("a\t5\twork\t0\t%2\t0\n")):
            self.assertTrue(rc.pane_exists(named))

    @unittest.skipUnless(shutil.which("tmux") and shutil.which("bwrap"), "needs tmux + bwrap")
    def test_pane_probe_against_real_tmux_in_a_pid_namespace(self) -> None:
        # T-5166: real tmux, on a private socket, inside a bwrap pid namespace with
        # TMUX unset, so nothing can touch a live server (reference_mock_popen_pid_kill_minus_one).
        script = textwrap.dedent("""
            import subprocess as sp, sys
            sys.path.insert(0, sys.argv[1])
            from tools import runtime_controller as rc
            S = ["tmux", "-L", "t5166"]
            quiet = dict(stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL)  # the server must not hold our pipes
            try:
                sp.run(S + ["new-session", "-d", "-s", "a", "sleep 60"], check=True, **quiet)
                sp.run(S + ["new-window", "-t", "a:3", "-n", "work", "sleep 60"], check=True, **quiet)
                ctx, T = rc.TmuxRuntimeContext(socket="t5166"), rc.TmuxTarget
                print([rc.pane_exists(T("a", "0", "0"), context=ctx), rc.pane_exists(T("a", "3", "0"), context=ctx),
                       rc.pane_exists(T("a", "work", "0"), context=ctx), rc.pane_exists(T("a", "5", "0"), context=ctx),
                       rc.pane_exists(T("zz", "0", "0"), context=ctx), rc.pane_exists(T("a", "3", "9"), context=ctx),
                       rc._pane_process_pid(T("a", "5", "0"), context=ctx)])
            finally:
                sp.run(S + ["kill-server"], **quiet)
        """)
        # A minimal env and a private TMUX_TMPDIR keep the probe off any live server's
        # socket. Not a --tmpfs /tmp: a checkout under /tmp (a worktree) would vanish
        # and the import would fail (T-5377).
        tmux_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmux_dir.cleanup)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
               "LANG": "C.UTF-8", "TMUX_TMPDIR": tmux_dir.name}
        argv = [sys.executable, "-c", script, str(Path(__file__).resolve().parent.parent)]
        if not _in_nested_pid_namespace():
            # Not sandboxed yet: give the probe its own pid namespace. When the suite
            # already runs in one (the documented way), a nested bwrap fails on the
            # host's /proc ("open /proc/<pid>/ns/ns") and hangs, so run it directly.
            argv = ["bwrap", "--dev-bind", "/", "/", "--unshare-pid", "--die-with-parent", *argv]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
        if proc.returncode != 0 and "bwrap" in proc.stderr:
            self.skipTest(f"bwrap unavailable here: {proc.stderr.strip()[:200]}")
        self.assertEqual(proc.stdout.strip(), "[True, True, True, False, False, False, None]", proc.stderr)

    def test_pane_probe_reads_no_tmux_server_as_absent(self) -> None:
        # T-4347 review: after a whole-server stop/crash (the recovery case) real
        # tmux 3.4 prints these two forms; raising here made ensure_runtime unable
        # to relaunch the seat. A socket error that is NOT a missing server
        # (e.g. permission denied) must still raise into the unknown/up guards.
        target = rc.TmuxTarget("seat", "runtime", "0")
        for stderr in (
            "no server running on /tmp/tmux-1001/default",
            "error connecting to /tmp/tmux-1001/default (No such file or directory)",
        ):
            with self.subTest(stderr=stderr), mock.patch.object(
                rc, "_run_tmux", return_value=subprocess.CompletedProcess([], 1, "", stderr)
            ):
                self.assertFalse(rc.pane_exists(target))
        denied = subprocess.CompletedProcess(
            [], 1, "", "error connecting to /tmp/tmux-1001/default (Permission denied)")
        with mock.patch.object(rc, "_run_tmux", return_value=denied):
            with self.assertRaisesRegex(RuntimeError, "tmux pane probe failed"):
                rc.pane_exists(target)

    def test_tmux_error_is_unknown_up_at_both_attach_guards(self) -> None:
        binding = _binding(Path("/tmp"), harness="codex")
        with mock.patch.object(rc, "pane_exists", side_effect=RuntimeError("tmux denied")):
            self.assertTrue(rc._runtime_pane_existed(binding))
            live, evidence = rc._pane_live_or_unknown(binding, context=None)
        self.assertTrue(live)
        self.assertEqual(evidence["pane_state"], "unknown")

    def test_existing_scope_without_a_pane_is_never_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex")
            now = rc._utc_now()
            handle = rc.RuntimeHandle(
                binding.launch, binding.controller.tmux, None, now, created_scope=False
            )
            rc._atomic_json(
                root / "recovery" / "runtime-controller" / "runtime.json",
                {"runtime_scope_unit": self.SCOPE},
            )
            with mock.patch.object(rc, "_stop_created_scope") as stop:
                result = rc._stop_scope_this_attempt_created(
                    binding, handle, launched_at=now, existed_before=False, state_root=root
                )
        stop.assert_not_called()
        self.assertEqual(result["scope_stop_skipped"], "attached-existing-runtime")

    def test_verify_started_cleanup_keeps_original_failure_on_stop_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _binding(Path(tmp), harness="codex")
            started = rc.RuntimeStart(self.SCOPE, True)
            with mock.patch.object(rc, "_wait_for_live_runtime", return_value=False), \
                    mock.patch.object(rc, "_stop_created_scope", side_effect=rc.ScopeStopRefused("denied")):
                with self.assertRaisesRegex(RuntimeError, "failed live tmux"):
                    rc._verify_started_runtime(binding, started, state_root=Path(tmp))

    def test_input_ready_cleanup_keeps_operational_failure_on_stop_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex")
            started = rc.RuntimeStart(self.SCOPE, True)
            not_ready = rc.RuntimeInputReadiness(False, "a" * 64, "not ready")
            with mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=not_ready), \
                    mock.patch.object(rc, "_stop_created_scope", side_effect=rc.ScopeStopRefused("denied")):
                with self.assertRaises(rc.RuntimeOperationalFailure) as caught:
                    rc._verify_runtime_input_ready(binding, started, state_root=root)
        self.assertEqual(caught.exception.cause, "runtime_input_not_ready")

    def test_rehydrate_cleanup_keeps_viability_failure_on_stop_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex", communications=None)
            state_path = root / "recovery" / "runtime-controller" / "runtime.json"
            rc._atomic_json(state_path, {"runtime_scope_unit": self.SCOPE})
            started = rc.RuntimeStart(self.SCOPE, True)
            with mock.patch.object(rc, "_recorded_scope_inactive", return_value=True), \
                    mock.patch.object(rc, "prove_managed_receiver_present", return_value=True), \
                    mock.patch.object(rc, "prove_predecessor_receiver_absent", return_value=True), \
                    mock.patch.object(
                        rc, "_authenticate_recorded_selected_runtime",
                        return_value=({"revision": "r"}, {"revision": "r"}, {}, "same-revision"),
                    ), mock.patch.object(rc, "_stop_prior_harness_receiver_before_start"), \
                    mock.patch.object(rc, "_start_runtime", return_value=started), \
                    mock.patch.object(rc, "_verify_started_runtime", side_effect=RuntimeError("not up")), \
                    mock.patch.object(rc, "_stop_created_scope", side_effect=rc.ScopeStopRefused("denied")):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc._rehydrate_recorded_runtime_selection(
                        binding,
                        {"runtime_scope_unit": self.SCOPE},
                        adapter_factory=_Adapter,
                        state_root=root,
                        backend=None,
                        context=None,
                        now=rc._utc_now(),
                        manifest_order=(rc._runtime_ref(binding),),
                        attempt_limit=1,
                    )
        self.assertEqual(caught.exception.reason, "recorded-runtime-rehydrate-failed")

    def test_a_cleanup_site_keeps_its_own_failure_class_on_a_refused_stop(self) -> None:
        # ensure_runtime's verification-failure cleanup: before T-4338 a refused stop
        # was silent; it must still end in the RuntimeOperationalFailure it raises.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex")

            def refuse(scope):
                raise rc.ScopeStopRefused(f"systemctl --user stop {scope} refused (rc 1): x")

            with mock.patch.object(rc, "pane_exists", return_value=False), \
                    mock.patch.object(rc, "_start_runtime", return_value=rc.RuntimeStart(self.SCOPE, True)), \
                    mock.patch.object(rc, "_verify_started_runtime", side_effect=RuntimeError("not up")), \
                    mock.patch.object(rc, "_stop_created_scope", side_effect=refuse) as stop, \
                    _provenance_patch():
                with self.assertRaises(rc.RuntimeOperationalFailure) as caught:
                    rc.ensure_runtime(binding, _Adapter(binding), state_root=root)
        stop.assert_called_once_with(self.SCOPE)
        self.assertEqual(caught.exception.cause, "runtime_started_verification_failed")

    def test_compensate_fails_loud_on_a_refused_stop_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude", communications=None)
            state = root / "recovery" / "runtime-controller" / "runtime.json"
            rc._atomic_json(state, {"runtime_scope_unit": self.SCOPE})
            failure = rc.RuntimeOperationalFailure(
                "provider completion was not proven",
                failure_class="operational-readiness",
                cause="provider-completion-not-proven",
                binding_runtime="claude/interactive",
                runtime_start=rc.RuntimeStart(self.SCOPE, True),
            )

            def refuse(scope):
                raise rc.ScopeStopRefused(f"systemctl --user stop {scope} refused (rc 1): Access denied")

            with mock.patch.object(rc, "_stop_created_scope", side_effect=refuse):
                with self.assertRaises(rc.ScopeStopRefused):
                    rc.compensate_failed_initial_candidate(
                        binding, failure=failure, state_root=root, backend=None,
                        reason="provider-completion-not-proven",
                    )
            rows = [json.loads(line) for line in
                    (root / "recovery" / "runtime-controller" / "controller-evidence.jsonl").read_text().splitlines()]
            kept = rc._read_json(state)
        teardown = [r for r in rows if r.get("event") == "runtime_viability_candidate_teardown"][-1]
        self.assertEqual(teardown["scope_stopped"], "false")
        self.assertIn("Access denied", teardown["scope_stop_error"])
        self.assertEqual(teardown["runtime_state_cleared"], "false")
        self.assertEqual(kept, {"runtime_scope_unit": self.SCOPE})


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

            start.assert_called_once_with(binding, mock.ANY, replace=False, state_root=root)
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

            start.assert_called_once_with(binding, mock.ANY, replace=False, state_root=root)
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

            start.assert_called_once_with(binding, mock.ANY, replace=False, state_root=root)
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
            with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
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
            # T-4362: the render gate is opt-in; a plain submit does not pass it.
            self.assertNotIn("--render-wait", argv)

    def test_fresh_runtime_submit_passes_the_render_wait(self) -> None:
        # T-4362: send_keys_submit's first Enter waits for the paste to render.
        binding = _binding(Path("/tmp"))
        handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
            rc.submit_to_runtime(handle, ExecutionRequest(prompt="work", cwd=Path("/tmp")),
                                 render_wait_seconds=8.0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--render-wait") + 1], "8")
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
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
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
            start.assert_called_once_with(target, mock.ANY, replace=True, state_root=root)

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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(codex_adapter_module, "ensure_update_check_disabled"), mock.patch.object(
            codex_adapter_module, "ensure_model_migration_prompts_hidden"
        ), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            argv[-8:],
            (
                "/usr/bin/env",
                "--unset=PANTHEON_INSTANCE_MANIFEST",
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
        self.assertEqual(argv[6:10], ("/usr/bin/env", "--unset=PANTHEON_INSTANCE_MANIFEST", "--", "PATH=/tmp/t2950-replace-account/.bun/bin:/tmp/t2950-replace-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))

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
            codex_adapter_module, "ensure_project_trusted"
        ) as ensure_trusted, mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ) as ensure_update_check, mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden") as ensure_migration, mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_migration.assert_called_once_with(binding.account_home / ".codex")

    def test_codex_harness_calls_ensure_worker_guard_hook(self) -> None:
        # T-4166: every codex runtime gets the worker-guard wired + trusted pre-launch.
        binding = _binding(Path("/tmp"), harness="codex")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(
            codex_adapter_module, "ensure_worker_guard_hook"
        ) as ensure_guard:
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_guard.assert_called_once_with(
            binding.account_home / ".codex",
            binding.account_home,
            executable=Path(binding.launch.executable),
        )

    def test_claude_harness_never_calls_ensure_worker_guard_hook(self) -> None:
        binding = _binding(Path("/tmp"), harness="claude")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
            rc, "_session_exists"
        ), mock.patch.object(
            rc, "_default_tmux_server_pid", return_value=123
        ), mock.patch.object(
            rc, "_pid_in_scope", return_value=True
        ), mock.patch.object(rc, "_run_tmux", return_value=completed), mock.patch.object(
            codex_adapter_module, "ensure_worker_guard_hook"
        ) as ensure_guard:
            rc._start_runtime(binding, _Adapter(binding), replace=False)
        ensure_guard.assert_not_called()

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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ) as ensure_update_check, mock.patch.object(
            codex_adapter_module, "ensure_model_migration_prompts_hidden"
        ) as ensure_migration, mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            codex_adapter_module, "ensure_project_trusted"
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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(codex_adapter_module, "ensure_update_check_disabled"), mock.patch.object(
            codex_adapter_module, "ensure_model_migration_prompts_hidden"
        ), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            codex_adapter_module, "ensure_project_trusted"
        ), mock.patch.object(codex_adapter_module, "ensure_update_check_disabled"), mock.patch.object(
            codex_adapter_module, "ensure_model_migration_prompts_hidden"
        ), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
        self.assertEqual(argv[8:12], ("/usr/bin/env", "--unset=PANTHEON_INSTANCE_MANIFEST", "--", "PATH=/tmp/t2950-new-session-account/.bun/bin:/tmp/t2950-new-session-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))
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
        self.assertEqual(argv[8:12], ("/usr/bin/env", "--unset=PANTHEON_INSTANCE_MANIFEST", "--", "PATH=/tmp/t2950-new-window-account/.bun/bin:/tmp/t2950-new-window-account/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"))

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
        ) as run, mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
            argv[-7:],
            [
                "/usr/bin/env",
                "--unset=PANTHEON_INSTANCE_MANIFEST",
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

    def test_runtime_command_argv_drops_inherited_instance_manifest(self) -> None:
        """T-5457: the launched session must not inherit the consume unit's live manifest."""
        argv = rc._runtime_command_argv(
            ("/usr/bin/python3", "-c", "import os; print(os.environ.get('PANTHEON_INSTANCE_MANIFEST', 'unset'))"),
            {"PANTHEON_TEST": "x"},
        )
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env={**os.environ, "PANTHEON_INSTANCE_MANIFEST": "/live/clone/instance.yaml"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "unset")

    def test_runtime_command_argv_keeps_explicit_instance_manifest(self) -> None:
        argv = rc._runtime_command_argv(
            ("/usr/bin/true",), {"PANTHEON_INSTANCE_MANIFEST": "/chosen/instance.yaml"}
        )
        self.assertEqual(
            argv,
            ("/usr/bin/env", "--", "PANTHEON_INSTANCE_MANIFEST=/chosen/instance.yaml", "/usr/bin/true"),
        )

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
            codex_adapter_module, "ensure_project_trusted"
        ) as trusted, mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ) as update, mock.patch.object(
            codex_adapter_module, "ensure_model_migration_prompts_hidden"
        ) as migration, mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"), mock.patch.object(
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

    def _wake_with_unknown_pane(self, harness: str, deliverable: bool | None):
        """T-4292 harness: pane verdict UNKNOWN (None); optionally stub the fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness=harness)
            mailbox.provision(binding.mailbox)
            message_id = mailbox.send(binding.mailbox, {"message": "do the work"},
                                      "tester", subject="work")
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux,
                                      "session-1", rc._utc_now())
            patches = [
                mock.patch.object(
                    rc, "ensure_runtime",
                    side_effect=lambda selected, adapter, *, state_root=None:
                        _record_handle_runtime_state(selected, handle, Path(state_root))),
                mock.patch.object(rc, "submit_to_runtime"),
                mock.patch("tools.mailbox_wake.session_key", return_value="1:2"),
                mock.patch("tools.mailbox_wake.pane_is_idle", return_value=None),
            ]
            if deliverable is not None:
                patches.append(mock.patch("tools.mailbox_wake.unknown_pane_deliverable",
                                          return_value=deliverable))
            with contextlib.ExitStack() as stack:
                entered = [stack.enter_context(pt) for pt in patches]
                stack.enter_context(_provenance_patch())
                result = wake_persistent_runtime(binding, adapter=_Adapter(binding),
                                                 state_dir=root / "state", cwd=root, now=100.0)
            submit = entered[1]
            if deliverable is not None:
                # rev-t4284 round 2, B2: the fallback must get the SAME pane the idle
                # probe read, and this binding's harness (a swapped harness or target
                # would defer a codex persona forever).
                entered[4].assert_called_once_with(entered[3].call_args.args[0], harness)
            queued = (binding.mailbox / "new" / message_id).is_file()
            marker = (binding.mailbox / ".wake_marker").is_file()
            return result, submit, queued, marker

    def test_interactive_wake_defers_on_unknown_claude_pane(self) -> None:
        # T-4292: UNKNOWN (a permission/confirm modal, an unrecognised screen) used to
        # fall through and inject, so a permission dialog could receive the paste plus
        # Enter. A claude pane needs a True verdict. No fallback stub: the real
        # unknown_pane_deliverable must say no for claude without touching tmux.
        result, submit, queued, marker = self._wake_with_unknown_pane("claude", None)
        self.assertEqual(result["action"], "deferred")
        self.assertIn("T-4292", result["reason"])
        submit.assert_not_called()
        self.assertTrue(queued)
        self.assertFalse(marker)

    def test_interactive_wake_codex_unknown_pane_needs_ready_evidence(self) -> None:
        # codex reads UNKNOWN on every screen (the classifier knows claude's sentinels),
        # so codex falls back to its own ready-composer evidence: with it -> submit,
        # without it -> defer.
        result, submit, _q, _m = self._wake_with_unknown_pane("codex", True)
        submit.assert_called_once()
        result, submit, queued, marker = self._wake_with_unknown_pane("codex", False)
        self.assertEqual(result["action"], "deferred")
        submit.assert_not_called()
        self.assertTrue(queued)
        self.assertFalse(marker)

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

    def test_persona_bringup_quota_check_cli_unsupported_no_fallback_json(self) -> None:
        # T-5446: support is derived from manifest fallbacks. Watchman has no
        # fallback, so this path remains side-effect free. (Archivist gained a
        # Codex fallback in T-5744 and is no longer an unsupported fixture.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                (
                    sys.executable,
                    str(REPO_ROOT / "tools" / "persona_bringup.py"),
                    "quota-check",
                    "--persona",
                    "watchman",
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
            self.assertFalse((root / "state" / "watchman").exists())

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
            ) as popen, mock.patch.object(
                rc.shutil, "which", return_value="/usr/bin/systemd-run"
            ), mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
                rc._run_post_start_commands(binding, state_root=root)

            run.assert_not_called()
            popen.assert_called_once()
            argv = popen.call_args.args[0]
            # T-4812: wrapped in its own transient scope so a oneshot caller's
            # control-group kill can't take it down.
            self.assertEqual(argv[:5], ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect"])
            self.assertTrue(argv[5].startswith("--unit=pantheon-post-start-recovery-"))
            self.assertEqual(
                argv[6:],
                [
                    f"{root}/pantheon-config/tools/recovery_boot_kick.sh",
                    "pantheon-recovery:harness.0",
                    "recovery",
                ],
            )
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            records = _read_jsonl(rc._evidence_log_path("recovery", root))
            self.assertEqual(records[0]["outcome"], "launched_background")
            # evidence records the command, not the scope wrapper
            self.assertTrue(records[0]["argv"].startswith(f"{root}/pantheon-config/tools/recovery_boot_kick.sh"))

    def test_background_scope_falls_back_unwrapped_without_systemd_run(self) -> None:
        with mock.patch.object(rc.shutil, "which", return_value=None):
            self.assertEqual(rc._background_scope_argv("recovery", ["kick"]), ["kick"])
        with mock.patch.object(rc.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rc._background_scope_argv("recovery", ["kick"]), ["kick"])

    @unittest.skipUnless(
        shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR")
        and subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True).returncode in (0, 1),
        "needs a reachable systemd user manager",
    )
    def test_background_scope_survives_oneshot_control_group_kill(self) -> None:
        """T-4812 regression, against real systemd: a background command launched
        from a Type=oneshot unit outlives the unit's control-group kill."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "survived"
            child = (
                "import subprocess, sys; sys.path.insert(0, sys.argv[1]);"
                "from tools import runtime_controller as rc;"
                "rc._launch_background_scoped('t4812test',"
                " ['sh', '-c', 'sleep 3; touch ' + sys.argv[2]],"
                " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
                " stderr=subprocess.DEVNULL, start_new_session=True)"
            )
            repo = str(Path(__file__).resolve().parents[1])
            done = subprocess.run(
                ["systemd-run", "--user", "--wait", "--quiet", "--collect",
                 "-p", "Type=oneshot", "-p", "KillMode=control-group",
                 f"--setenv=XDG_RUNTIME_DIR={os.environ['XDG_RUNTIME_DIR']}",
                 sys.executable, "-c", child, repo, str(marker)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertTrue(marker.exists(), "background command died with the oneshot's cgroup")

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

    def test_subscription_cap_status_threads_now_and_carries_parsed_reset_and_kind(self) -> None:
        """T-4047: the `now` param reaches the adapter's own `now=` kwarg,
        and RuntimeInputReadiness carries the DERIVED reset_at/cap_kind
        forward -- never raw pane text."""
        target = rc.TmuxTarget("pantheon-recovery", "harness", "0")
        fixed_now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        seen_now: list[datetime | None] = []

        class _FakeAdapter:
            def assess_post_start_readiness(self, *, harness, profile, baseline, samples, now=None):
                del harness, profile, baseline
                seen_now.append(now)
                sample = next(iter(samples))
                return rc.PostStartAssessment(
                    schema="pantheon-harness-post-start-assessment/v1",
                    harness="claude",
                    profile="interactive-subscription",
                    status="subscription-cap",
                    cause="post-start-subscription-cap",
                    pattern_id="claude-rate-limit-options-modal",
                    baseline_sha256="x",
                    capture_count=1,
                    changed_capture_indices=(sample.capture_index,),
                    cap_kind="session-limit",
                    reset_at=datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc),
                )

        with mock.patch.object(rc, "_capture_visible_runtime_pane", return_value="cap pane\n"):
            readiness = rc._wait_for_runtime_input_ready(
                target, adapter=_FakeAdapter(), now=lambda: fixed_now,
            )
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.adapter_status, "subscription-cap")
        self.assertEqual(readiness.adapter_cap_kind, "session-limit")
        self.assertEqual(
            readiness.adapter_reset_at, datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(seen_now, [fixed_now])


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

    def test_subscription_cap_evidence_includes_reset_at_and_cap_kind(self) -> None:
        """T-4047: the derived (never-raw-text) reset_at/cap_kind fields
        reach RuntimeOperationalFailure's readiness_evidence, which is what
        controller_main's cooldown-override helper reads."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            reset_at = datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc)
            readiness = rc.RuntimeInputReadiness(
                ready=False, pane_sha256="d" * 64, reason="post-start-subscription-cap",
                adapter_status="subscription-cap", adapter_cause="post-start-subscription-cap",
                adapter_pattern_id="claude-rate-limit-options-modal", adapter_capture_count=3,
                adapter_reset_at=reset_at, adapter_cap_kind="session-limit",
            )
            with mock.patch.object(
                rc, "_wait_for_runtime_input_ready", return_value=readiness
            ), mock.patch.object(rc, "_stop_created_scope") as stop:
                with self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                    rc._verify_runtime_input_ready(binding, started, state_root=root)
            self.assertEqual(ctx.exception.readiness_evidence["reset_at"], rc._iso(reset_at))
            self.assertEqual(ctx.exception.readiness_evidence["cap_kind"], "session-limit")
            # subscription-cap never stops the created scope (existing
            # behavior, unaffected by this ticket -- verify unchanged).
            stop.assert_not_called()

    def test_subscription_cap_evidence_empty_when_reset_and_kind_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root)
            started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)
            readiness = rc.RuntimeInputReadiness(
                ready=False, pane_sha256="e" * 64, reason="post-start-subscription-cap",
                adapter_status="subscription-cap", adapter_cause="post-start-subscription-cap",
                adapter_pattern_id="claude-rate-limit-options-modal", adapter_capture_count=1,
            )
            with mock.patch.object(rc, "_wait_for_runtime_input_ready", return_value=readiness):
                with self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                    rc._verify_runtime_input_ready(binding, started, state_root=root)
            self.assertEqual(ctx.exception.readiness_evidence["reset_at"], "")
            self.assertEqual(ctx.exception.readiness_evidence["cap_kind"], "")


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
            # T-4362: the successor has only just booted; its prompt waits for the
            # paste to render before the first Enter.
            self.assertEqual(submit.call_args.kwargs["render_wait_seconds"],
                             rc.FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS)

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
            ), mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
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
        ) as run, mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
        ) as run, mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
            codex_adapter_module, "ensure_update_check_disabled"
        ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
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
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
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
        with mock.patch.object(rc.subprocess, "run", return_value=completed) as run, _submit_helper_into(run):
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

    def test_prompt_successor_ack_read_command_reaches_argparse_from_any_cwd(self) -> None:
        # T-4471: the prompt calls its ack-read line "this exact command". The old
        # bare-script form (python3 <repo>/tools/runtime_controller.py) died on
        # "No module named 'tools'" before argparse from EVERY cwd, so a successor
        # that followed it literally never acked. Run the rendered line itself
        # (plus --help, so nothing is recorded) from a cwd outside the repo.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state root"
            root.mkdir()
            binding = _binding(root)
            handle = rc.RuntimeHandle(
                binding.launch, binding.controller.tmux, None, rc._utc_now()
            )
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(
                    handle, root / "evidence.md", persona="recovery", state_root=root
                )
            command = submit.call_args.args[1].prompt.splitlines()[-1]
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            result = subprocess.run(
                ["bash", "-c", command + " --help"],
                cwd=elsewhere,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertIn("--evidence", result.stdout)

    def test_ack_read_command_quotes_each_argument(self) -> None:
        # T-4471: a state root with a space stays one argv element.
        command = rc._ack_read_command(
            "recovery", Path("/tmp/state root"), "hook:runtime-transfer:x:y.md"
        )
        _, _, invocation = command.partition(" && ")
        self.assertEqual(
            shlex.split(invocation),
            [
                "python3", "-m", "tools.runtime_controller", "ack-read",
                "--persona", "recovery",
                "--state-root", "/tmp/state root",
                "--evidence", "hook:runtime-transfer:x:y.md",
            ],
        )
        self.assertEqual(
            shlex.split(command.partition(" && ")[0]),
            ["cd", str(Path(rc.__file__).resolve().parent.parent)],
        )


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
            archive.write_text("# Archived transfer\n", encoding="utf-8")
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
            ) as run, _submit_helper_into(run):
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
            archive.write_text("# Archived transfer\n", encoding="utf-8")
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
            ) as run, _submit_helper_into(run):
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


def _stateless_binding(
    root: Path,
    *,
    harness: str = "codex",
    profile: str = "interactive",
    communications: dict[str, object] | None = None,
    mid_session_cap_swap: bool = False,
    fallback_return: bool = False,
) -> SimpleNamespace:
    return _binding(
        root, harness=harness, profile=profile,
        communications=communications, mid_session_cap_swap=mid_session_cap_swap,
        fallback_return=fallback_return,
    )


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

    def test_record_last_attempt_cooldown_until_override_bypasses_weekly_reset(self) -> None:
        """T-4037: the mid-session cap swap path knows the ACTUAL parsed reset
        instant, which is materially shorter than subscription_cap's normal
        next_weekly_reset mapping -- the override must win outright."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            override = now + timedelta(hours=2)
            payload = rc.record_last_attempt(
                "orchestrator", binding, outcome="failed", failure_class="subscription_cap",
                state_root=root, now=now, cooldown_until_override=override,
            )
            self.assertEqual(payload["cooldown_until"], rc._iso(override))
            self.assertNotEqual(
                payload["cooldown_until"],
                rc._iso(runtime_quota.next_weekly_reset("claude", now=now)),
            )
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
    def test_authentication_loss_is_permanent_and_selects_codex_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now = rc._utc_now()
            record = rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="authentication_loss",
                state_root=root, now=now,
            )
            selected, skipped = rc._select_viable_runtime(
                "orchestrator", [claude, codex], {}, now + timedelta(days=365), state_root=root
            )
            self.assertIsNone(record["cooldown_until"])
            self.assertIs(selected, codex)
            self.assertEqual(skipped, [{"runtime": "claude/anthropic-code", "reason": "authentication-loss"}])

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


class PostStartCapCooldownOverrideTests(unittest.TestCase):
    """T-4047: the proportionate start-up subscription_cap cooldown decision,
    including the plan-review clamp amendment (never exceed next_weekly_reset,
    never land before `now`)."""

    def _exc(self, **evidence: str) -> "rc.RuntimeOperationalFailure":
        return rc.RuntimeOperationalFailure(
            "capped at start", failure_class="subscription-cap", cause="x",
            binding_runtime="claude/anthropic-code", readiness_evidence=evidence,
        )

    def test_non_subscription_cap_failure_class_returns_none(self) -> None:
        exc = self._exc(reset_at=rc._iso(datetime(2026, 9, 27, 14, 0, 0, tzinfo=timezone.utc)))
        self.assertIsNone(
            rc._post_start_cap_cooldown_override(
                exc, "process_death", now=datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc),
                harness="claude",
            )
        )

    def test_non_runtime_operational_failure_returns_none(self) -> None:
        self.assertIsNone(
            rc._post_start_cap_cooldown_override(
                RuntimeError("tmux vanished"), "subscription_cap",
                now=datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc), harness="claude",
            )
        )

    def test_no_evidence_at_all_returns_none_stays_on_weekly(self) -> None:
        """Pins the pre-existing controller_main test's expectation
        (test_start_failure_records_attempt_without_active_runtime_write):
        a RuntimeOperationalFailure raised without readiness_evidence must
        still fall through to next_weekly_reset."""
        exc = rc.RuntimeOperationalFailure(
            "capped at start", failure_class="subscription-cap", cause="x",
            binding_runtime="codex/interactive-chatgpt",
        )
        self.assertIsNone(
            rc._post_start_cap_cooldown_override(
                exc, "subscription_cap",
                now=datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc), harness="codex",
            )
        )

    def test_parsed_reset_at_wins_over_conservative_default_and_weekly(self) -> None:
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        reset_at = now + timedelta(hours=3)
        exc = self._exc(reset_at=rc._iso(reset_at), cap_kind="session-limit")
        result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        self.assertEqual(result, reset_at)

    def test_unparsed_session_kind_uses_conservative_default(self) -> None:
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        exc = self._exc(cap_kind="session-limit")
        result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        self.assertEqual(
            result, now + timedelta(seconds=rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)
        )

    def test_unparsed_opus_and_unknown_kinds_also_use_conservative_default(self) -> None:
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        for kind in ("opus-limit", "unknown-limit"):
            with self.subTest(kind=kind):
                exc = self._exc(cap_kind=kind)
                result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
                self.assertEqual(
                    result, now + timedelta(seconds=rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)
                )

    def test_unparsed_weekly_kind_returns_none_stays_on_weekly(self) -> None:
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        exc = self._exc(cap_kind="weekly-limit")
        self.assertIsNone(
            rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        )

    def test_far_future_misparse_is_clamped_to_next_weekly_reset(self) -> None:
        """the operator amendment: a dated-form misparse resolved into next year
        must never produce a cooldown longer than the weekly fallback it
        replaces."""
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        far_future = now + timedelta(days=400)
        exc = self._exc(reset_at=rc._iso(far_future), cap_kind="session-limit")
        result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        self.assertEqual(result, runtime_quota.next_weekly_reset("claude", now=now))
        self.assertLess(result, far_future)

    def test_conservative_default_is_also_clamped_to_weekly_ceiling(self) -> None:
        """The 4h conservative default is always far shorter than a week in
        practice, but the clamp applies uniformly regardless -- pin it."""
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        exc = self._exc(cap_kind="session-limit")
        result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        self.assertLessEqual(result, runtime_quota.next_weekly_reset("claude", now=now))

    def test_past_parsed_reset_floors_at_now_never_goes_negative(self) -> None:
        """The parser's own skew-grace rollover should already prevent a
        genuinely-past reset_at, but this is a defensive floor: if one ever
        slipped through (e.g. a future format the parser doesn't roll
        forward), cooldown_until must never land before `now`."""
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        past = now - timedelta(hours=2)
        exc = self._exc(reset_at=rc._iso(past), cap_kind="session-limit")
        result = rc._post_start_cap_cooldown_override(exc, "subscription_cap", now=now, harness="claude")
        self.assertEqual(result, now)
        self.assertGreaterEqual(result, now)


class RuntimeAliveTests(unittest.TestCase):
    """T-2966 REBOUND finding #2: `_runtime_alive`'s tmux + pid dual-authority
    cross-check (spec: 'Authority = `tmux has-session -t <session>` +
    `kill -0 <pid>`')."""

    def test_pane_process_pid_parses_tmux_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(
                rc, "_run_tmux",
                return_value=subprocess.CompletedProcess(
                    args=(), returncode=0, stdout="pantheon-recovery\t1\tharness\t0\t%3\t4242\n", stderr=""),
            ):
                self.assertEqual(rc._pane_process_pid(binding.controller.tmux), 4242)

    def test_pane_process_pid_ignores_another_panes_pid(self) -> None:
        # T-5166: tmux falls back to another window on a missing target (rc 0).
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(
                rc, "_run_tmux",
                return_value=subprocess.CompletedProcess(
                    args=(), returncode=0, stdout="pantheon-recovery\t0\tshell\t0\t%0\t4242\n", stderr=""),
            ):
                self.assertIsNone(rc._pane_process_pid(binding.controller.tmux))

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


class _CapAfterHooksAdapter(_Adapter):
    """Fake adapter whose post-hook pane assessment reports a subscription
    cap -- the 'cap modal revealed by the boot kick's first turn' shape."""

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples, now=None):
        del now
        sample_tuple = tuple(samples)
        return rc.PostStartAssessment(
            schema="pantheon-harness-post-start-assessment/v1",
            harness=harness,
            profile=profile,
            status="subscription-cap",
            cause="post-start-subscription-cap",
            pattern_id="claude-rate-limit-options-modal",
            baseline_sha256=baseline.sha256,
            capture_count=len(sample_tuple),
            changed_capture_indices=tuple(s.capture_index for s in sample_tuple),
        )


class ControllerStartRuntimePostStartTests(unittest.TestCase):
    """T-4156: `_controller_start_runtime`'s START branch (fresh start, and
    respawn over a foreign pane) runs `binding.controller.post_start` via the
    same `_run_and_assess_post_start` helper `ensure_runtime` uses; the
    ATTACH branch runs none. No real tmux: every tmux/pane seam is mocked and
    subprocess.run/Popen are mocks."""

    ARM = "{account_home}/pantheon-config/tools/arm_pane_log.sh"
    PATCH409 = "{account_home}/pantheon-config/tools/telegram_poller_409_patch.sh"
    KICK = "{account_home}/pantheon-config/tools/recovery_boot_kick.sh"

    def _recovery_like_post_start(self) -> tuple:
        return (
            PostStartCommand(argv=(self.ARM, "{tmux_target}", "{persona}")),
            PostStartCommand(argv=(self.PATCH409, "{account_home}")),
            PostStartCommand(argv=(self.KICK, "{tmux_target}", "{persona}"), background=True),
        )

    def _post_start_binding(self, root: Path, post_start: tuple | None = None):
        return _binding(
            root, harness="claude", profile="anthropic-code",
            post_start=self._recovery_like_post_start() if post_start is None else post_start,
        )

    def _start_patches(self, binding, *, adapter=None, alive=False, pids=(7777,), cmdline=None):
        """Common seams for a controller START: the low-level start/verify
        primitives are mocked, the pane capture is a constant, and the bounded
        post-hook sampler is sleep-free."""
        from tools import persona_bringup

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            persona_bringup, "build_adapter", return_value=adapter or _Adapter(binding)
        ))
        stack.enter_context(mock.patch.object(rc, "_runtime_alive", return_value=alive))
        stack.enter_context(mock.patch.object(rc, "_target_known", return_value=alive))
        start = stack.enter_context(mock.patch.object(
            rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        ))
        stack.enter_context(mock.patch.object(rc, "_verify_started_runtime"))
        stack.enter_context(mock.patch.object(rc, "_verify_runtime_input_ready"))
        stack.enter_context(mock.patch.object(rc, "_pane_process_pid", side_effect=list(pids)))
        stack.enter_context(mock.patch.object(rc, "_read_pane_cmdline", return_value=cmdline))
        capture = stack.enter_context(mock.patch.object(
            rc, "_capture_visible_runtime_pane", return_value="pane text\n"
        ))
        real_sampler = rc._bounded_post_start_samples
        stack.enter_context(mock.patch.object(
            rc, "_bounded_post_start_samples",
            side_effect=lambda b, **kw: real_sampler(b, **{**kw, "sleep": lambda _s: None}),
        ))
        return stack, start, capture

    def _post_start_records(self, root: Path) -> list[dict[str, object]]:
        path = rc._evidence_log_path("recovery", root)
        if not path.exists():
            return []
        return [r for r in _read_jsonl(path) if str(r["event"]).startswith("post_start")]

    def test_start_branch_runs_post_start_in_order_with_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root)
            calls: list[tuple[str, list[str]]] = []
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            stack, start, _capture = self._start_patches(binding)
            with stack, mock.patch.object(
                rc.subprocess, "run",
                side_effect=lambda argv, **kw: (calls.append(("run", argv)), completed)[1],
            ), mock.patch.object(
                rc.subprocess, "Popen",
                side_effect=lambda argv, **kw: (calls.append(("popen", argv)), mock.Mock())[1],
            ), mock.patch.object(
                rc, "_assess_post_start_after_commands", wraps=rc._assess_post_start_after_commands
            ) as assess, mock.patch.object(rc.shutil, "which", return_value=None):
                # unwrapped fallback: this test pins order; the scope wrapper is
                # pinned in PostStartHookTests (T-4812)
                runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)

            start.assert_called_once()
            self.assertEqual(handle.pid, 7777)
            self.assertFalse(runtime_start.created_scope)
            self.assertEqual(
                calls,
                [
                    ("run", [f"{root}/pantheon-config/tools/arm_pane_log.sh", "pantheon-recovery:harness.0", "recovery"]),
                    ("run", [f"{root}/pantheon-config/tools/telegram_poller_409_patch.sh", str(root)]),
                    ("popen", [f"{root}/pantheon-config/tools/recovery_boot_kick.sh", "pantheon-recovery:harness.0", "recovery"]),
                ],
            )
            records = self._post_start_records(root)
            self.assertEqual(
                [(r["event"], r["background"], r["outcome"]) for r in records],
                [
                    ("post_start_command", "false", "ok"),
                    ("post_start_command", "false", "ok"),
                    ("post_start_command", "true", "launched_background"),
                ],
            )
            # the post-hook pane assessment (the fail-closed gate) ran once, with a
            # pre-hook baseline, and passed (the fake adapter reports "unknown").
            assess.assert_called_once()
            self.assertEqual(assess.call_args.kwargs["baseline"].capture_index, 0)

    def test_start_branch_orders_verify_then_post_start_then_pid_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root)
            manager = mock.Mock()
            stack, _start, _capture = self._start_patches(binding)
            with stack, mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ) as ready, mock.patch.object(
                rc, "_run_and_assess_post_start"
            ) as helper, mock.patch.object(rc, "_pane_process_pid", return_value=9) as pid:
                manager.attach_mock(ready, "ready")
                manager.attach_mock(helper, "post_start")
                manager.attach_mock(pid, "pid")
                runtime_start, _handle = rc._controller_start_runtime(binding, state_root=root)
            self.assertEqual([c[0] for c in manager.mock_calls], ["ready", "post_start", "pid"])
            helper.assert_called_once_with(binding, runtime_start, mock.ANY, state_root=root)

    def test_attach_branch_runs_no_post_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root)
            stack, start, capture = self._start_patches(
                binding, alive=True, pids=(4242,), cmdline=[str(binding.launch.executable)]
            )
            with stack, mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen, mock.patch.object(rc, "_run_and_assess_post_start") as helper:
                _runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)

            self.assertEqual(handle.pid, 4242)
            start.assert_not_called()
            helper.assert_not_called()
            run.assert_not_called()
            popen.assert_not_called()
            capture.assert_not_called()
            self.assertEqual(self._post_start_records(root), [])

    def test_respawn_over_foreign_pane_runs_post_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root)
            stack, start, _capture = self._start_patches(
                binding, alive=True, pids=(5555, 7777), cmdline=["/usr/bin/codex"]
            )
            with stack, mock.patch.object(
                rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ) as run, mock.patch.object(rc.subprocess, "Popen") as popen:
                _runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)

            self.assertEqual(start.call_args.kwargs["replace"], True)
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(run.call_count, 2)
            popen.assert_called_once()
            events = [json.loads(line)["event"] for line in rc._evidence_log_path("recovery", root).read_text().splitlines()]
            self.assertEqual(
                events,
                ["controller_start_respawn_over_foreign_pane"] + ["post_start_command"] * 3,
            )

    def test_empty_post_start_is_a_no_op_with_zero_evidence_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root, post_start=())
            stack, _start, capture = self._start_patches(binding)
            with stack, mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen"
            ) as popen, mock.patch.object(rc, "_assess_post_start_after_commands") as assess:
                _runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)

            self.assertEqual(handle.pid, 7777)
            run.assert_not_called()
            popen.assert_not_called()
            capture.assert_not_called()  # no baseline sample taken
            assess.assert_not_called()
            self.assertFalse(rc._evidence_log_path("recovery", root).exists())

    def test_failing_command_is_fail_open_start_still_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(root)
            stack, _start, _capture = self._start_patches(binding)
            with stack, mock.patch.object(
                rc.subprocess, "run", side_effect=FileNotFoundError("no such hook")
            ), mock.patch.object(rc.subprocess, "Popen", side_effect=OSError("popen boom")):
                _runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)

            self.assertEqual(handle.pid, 7777)
            outcomes = [r["outcome"] for r in self._post_start_records(root)]
            self.assertEqual(len(outcomes), 3)
            self.assertIn("FileNotFoundError", outcomes[0])
            self.assertIn("FileNotFoundError", outcomes[1])
            self.assertIn("OSError", outcomes[2])

    def test_background_command_goes_through_popen_never_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._post_start_binding(
                root,
                post_start=(PostStartCommand(argv=(self.KICK, "{tmux_target}", "{persona}"), background=True),),
            )
            proc = mock.Mock()
            stack, _start, _capture = self._start_patches(binding)
            with stack, mock.patch.object(rc.subprocess, "run") as run, mock.patch.object(
                rc.subprocess, "Popen", return_value=proc
            ) as popen:
                rc._controller_start_runtime(binding, state_root=root)

            run.assert_not_called()
            popen.assert_called_once()
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            # Never waited on for COMPLETION. T-5321 polls only to learn whether the
            # child already exited while it waits (bounded) for the scope to exist.
            proc.wait.assert_not_called()
            proc.communicate.assert_not_called()

    def test_t5321_background_launch_waits_until_scope_is_active(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        states = iter(["", "activating", "active"])
        with mock.patch.object(rc.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}), \
             mock.patch.object(rc.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(rc.subprocess, "run",
                               side_effect=lambda *a, **k: mock.Mock(stdout=next(states))) as run, \
             mock.patch.object(rc.time, "sleep"):
            rc._launch_background_scoped("recovery", ["kick"], start_new_session=True)
        argv = popen.call_args.args[0]
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        self.assertEqual(run.call_count, 3)
        self.assertIn(f"{unit}.scope", run.call_args.args[0])
        proc.wait.assert_not_called()

    def test_t5321_background_launch_wait_is_bounded_and_fail_open(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        clock = iter([0.0] + [float(i) for i in range(1, 100)])
        with mock.patch.object(rc.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}), \
             mock.patch.object(rc.subprocess, "Popen", return_value=proc), \
             mock.patch.object(rc.subprocess, "run", return_value=mock.Mock(stdout="")) as run, \
             mock.patch.object(rc.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(rc.time, "sleep"):
            self.assertIs(rc._launch_background_scoped("recovery", ["kick"]), proc)
        self.assertLessEqual(run.call_count, int(rc.BACKGROUND_SCOPE_WAIT_SECONDS) + 1)
        with mock.patch.object(rc.shutil, "which", return_value="/usr/bin/systemd-run"), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}), \
             mock.patch.object(rc.subprocess, "Popen", return_value=proc), \
             mock.patch.object(rc.subprocess, "run", side_effect=OSError("no systemctl")):
            self.assertIs(rc._launch_background_scoped("recovery", ["kick"]), proc)

    def test_t5321_unwrapped_launch_does_not_wait(self) -> None:
        with mock.patch.object(rc.shutil, "which", return_value=None), \
             mock.patch.object(rc.subprocess, "Popen") as popen, \
             mock.patch.object(rc.subprocess, "run") as run:
            rc._launch_background_scoped("recovery", ["kick"])
        self.assertEqual(popen.call_args.args[0], ["kick"])
        run.assert_not_called()

    def test_post_hook_cap_assessment_is_fail_closed_and_swaps_via_controller_main(self) -> None:
        """Policy: a subscription-cap revealed AFTER the hooks raises out of
        _controller_start_runtime; controller_main's start-failure handler
        records a subscription_cap cooldown on the capped candidate, never
        supervises it, and returns 1 (Restart=always). The next invocation
        reselects the fallback -- the swap -- with no crash."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = self._post_start_binding(root)
            codex = _binding(root, harness="codex", profile="interactive-chatgpt")
            completed = mock.Mock(returncode=0, stdout="", stderr="")

            def block_must_not_run(_binding):
                raise AssertionError("a capped start must not be supervised")

            stack, _start, _capture = self._start_patches(claude, adapter=_CapAfterHooksAdapter(claude))
            with stack, mock.patch.object(rc.subprocess, "run", return_value=completed) as run, \
                    mock.patch.object(rc.subprocess, "Popen") as popen:
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                    # default start_runtime_fn -> real _controller_start_runtime
                    block_until_death_fn=block_must_not_run,
                    persist_record_fn=lambda *a, **k: "persisted",
                )

            self.assertEqual(result, 1)
            # hooks DID run (fail-open, before the assessment) ...
            self.assertEqual(run.call_count, 2)
            popen.assert_called_once()
            # ... and the cap was recorded as subscription_cap with a cooldown.
            attempt = rc.load_last_attempt("recovery", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "failed")
            self.assertEqual(attempt["failure_class"], "subscription_cap")
            self.assertIsNotNone(attempt["cooldown_until"])
            self.assertEqual(rc.read_active_runtime("recovery", state_root=root), {})

            # Next (Restart=always) invocation swaps to the fallback candidate.
            selected: list[str] = []

            def fake_start(binding):
                selected.append(rc._binding_runtime_ref(binding))
                return (
                    rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                    rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()),
                )

            rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                start_runtime_fn=fake_start, block_until_death_fn=lambda b: None,
                persist_record_fn=lambda *a, **k: "persisted",
            )
            self.assertEqual(selected, ["codex/interactive-chatgpt"])


class _RealClaudeAssessAdapter(_Adapter):
    """T-4173: fake launch adapter whose post-hook assessment is the REAL
    ClaudeHarnessAdapter classifier + reset parser; records the `now` it is
    handed so the plumbing is observable."""

    def __init__(self, binding, seen_now: list) -> None:
        super().__init__(binding)
        self._real = ClaudeHarnessAdapter(binding.role, claude_home=binding.account_home)
        self._seen_now = seen_now

    def assess_post_start_readiness(self, *, harness, profile, baseline, samples, now=None):
        self._seen_now.append(now)
        return self._real.assess_post_start_readiness(
            harness=harness, profile=profile, baseline=baseline, samples=samples, now=now,
        )


class PostStartCapResetAtTests(unittest.TestCase):
    """T-4173 Part 1: a subscription cap found by the POST-HOOK assessment
    carries the derived reset_at/cap_kind into the raise's evidence, so
    controller_main pins the cooldown to the real reset (a 5h session cap ->
    ~5h off claude) instead of next_weekly_reset (up to a week)."""

    # Monday 09:50 UTC + the fixture's "resets 2:50pm (UTC)" -> exactly 5h,
    # while next_weekly_reset is days away (the pre-fix lockout).
    FIXED_NOW = datetime(2026, 9, 28, 9, 50, 0, tzinfo=timezone.utc)
    WEEKLY_UNPARSEABLE = (
        "  ⎿  You've hit your weekly limit · resets whenever the moon says so\n"
        "\n"
        "❯ /rate-limit-options\n"
        "\n"
        "What do you want to do?\n"
        "\n"
        "❯ 1. Stop and wait for limit to reset\n"
        "  2. Upgrade your plan\n"
        "\n"
    )

    # Borrow T-4156's controller-start seams (not inherited: that would re-run its tests).
    ARM = ControllerStartRuntimePostStartTests.ARM
    PATCH409 = ControllerStartRuntimePostStartTests.PATCH409
    KICK = ControllerStartRuntimePostStartTests.KICK
    _recovery_like_post_start = ControllerStartRuntimePostStartTests._recovery_like_post_start
    _post_start_binding = ControllerStartRuntimePostStartTests._post_start_binding
    _start_patches = ControllerStartRuntimePostStartTests._start_patches

    def _run_capped_controller(self, root: Path, *, adapter_factory, cap_text: str):
        claude = self._post_start_binding(root)
        codex = _binding(root, harness="codex", profile="interactive-chatgpt")
        adapter = adapter_factory(claude)
        stack, _start, capture = self._start_patches(claude, adapter=adapter)
        with stack, mock.patch.object(
            rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ), mock.patch.object(rc.subprocess, "Popen"), mock.patch.object(
            rc, "_utc_now", return_value=self.FIXED_NOW
        ):
            # baseline (pre-hook) is an idle composer; every post-hook sample is the cap.
            capture.side_effect = ["❯ \n"] + [cap_text] * 10
            result = rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root,
                now=lambda: self.FIXED_NOW, sleep=lambda s: None,
                block_until_death_fn=lambda b: (_ for _ in ()).throw(
                    AssertionError("a capped start must not be supervised")
                ),
                persist_record_fn=lambda *a, **k: "persisted",
            )
        self.assertEqual(result, 1)
        attempt = rc.load_last_attempt("recovery", "claude--anthropic-code", state_root=root)
        self.assertEqual(attempt["failure_class"], "subscription_cap")
        return attempt

    def test_post_hook_5h_session_cap_cooldown_pinned_to_parsed_reset(self) -> None:
        cap_text = (REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        seen_now: list = []
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._run_capped_controller(
                Path(tmp),
                adapter_factory=lambda b: _RealClaudeAssessAdapter(b, seen_now),
                cap_text=cap_text,
            )
        expected = datetime(2026, 9, 28, 14, 50, 0, tzinfo=timezone.utc)
        self.assertEqual(expected - self.FIXED_NOW, timedelta(hours=5))
        self.assertEqual(attempt["cooldown_until"], rc._iso(expected))
        weekly = runtime_quota.next_weekly_reset("claude", now=self.FIXED_NOW)
        self.assertGreater(weekly - expected, timedelta(days=5))  # the lockout this fixes
        # `now` plumbing: the adapter got a concrete clock, not None.
        self.assertTrue(seen_now)
        self.assertTrue(all(n == self.FIXED_NOW for n in seen_now), seen_now)

    def test_post_hook_weekly_cap_unparsed_keeps_weekly_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._run_capped_controller(
                Path(tmp),
                adapter_factory=lambda b: _RealClaudeAssessAdapter(b, []),
                cap_text=self.WEEKLY_UNPARSEABLE,
            )
        self.assertEqual(
            attempt["cooldown_until"],
            rc._iso(runtime_quota.next_weekly_reset("claude", now=self.FIXED_NOW)),
        )

    def test_post_hook_cap_without_reset_or_kind_keeps_weekly_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._run_capped_controller(
                Path(tmp), adapter_factory=_CapAfterHooksAdapter, cap_text="capped\n",
            )
        self.assertEqual(
            attempt["cooldown_until"],
            rc._iso(runtime_quota.next_weekly_reset("claude", now=self.FIXED_NOW)),
        )

    def test_evidence_carries_reset_at_and_cap_kind_with_t4047_encoding(self) -> None:
        base = dict(
            schema="pantheon-harness-post-start-assessment/v1", harness="claude",
            profile="anthropic-code", status="subscription-cap", cause="c", pattern_id="p",
            baseline_sha256="0" * 64, capture_count=3, changed_capture_indices=(1, 2, 3),
        )
        reset_at = datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc)
        full = rc._post_start_assessment_evidence(
            rc.PostStartAssessment(**base, reset_at=reset_at, cap_kind="session-limit")
        )
        self.assertEqual(full["reset_at"], rc._iso(reset_at))
        self.assertEqual(full["cap_kind"], "session-limit")
        empty = rc._post_start_assessment_evidence(rc.PostStartAssessment(**base))
        self.assertEqual((empty["reset_at"], empty["cap_kind"]), ("", ""))
        # duck-typed assessment without the fields (older fakes): empty, no raise.
        legacy = rc._post_start_assessment_evidence(SimpleNamespace(**base))
        self.assertEqual((legacy["reset_at"], legacy["cap_kind"]), ("", ""))


class PostStartCrashProofingTests(unittest.TestCase):
    """T-4173 Part 2 (T-4156 reviewer F2): no NON-cap exception may escape
    `_run_and_assess_post_start` on the controller start path -- each is
    recorded and treated as not-capped, the start returns a handle -- while
    the subscription-cap raise still propagates."""

    # Borrow T-4156's controller-start seams (not inherited: that would re-run its tests).
    ARM = ControllerStartRuntimePostStartTests.ARM
    PATCH409 = ControllerStartRuntimePostStartTests.PATCH409
    KICK = ControllerStartRuntimePostStartTests.KICK
    _recovery_like_post_start = ControllerStartRuntimePostStartTests._recovery_like_post_start
    _post_start_binding = ControllerStartRuntimePostStartTests._post_start_binding
    _start_patches = ControllerStartRuntimePostStartTests._start_patches

    def _records(self, root: Path, event: str) -> list[dict]:
        path = rc._evidence_log_path("recovery", root)
        if not path.exists():
            return []
        return [r for r in _read_jsonl(path) if r["event"] == event]

    def _start(self, root: Path, *, post_start=None, adapter_factory=None, capture_side_effect=None,
               extra=()):
        binding = self._post_start_binding(root, post_start=post_start)
        adapter = adapter_factory(binding) if adapter_factory else None
        stack, _start, capture = self._start_patches(binding, adapter=adapter)
        with stack, mock.patch.object(
            rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ) as run, mock.patch.object(rc.subprocess, "Popen") as popen, contextlib.ExitStack() as more:
            for patcher in extra:
                more.enter_context(patcher)
            if capture_side_effect is not None:
                capture.side_effect = capture_side_effect
            runtime_start, handle = rc._controller_start_runtime(binding, state_root=root)
        return handle, run, popen

    def test_baseline_capture_oserror_is_recorded_commands_and_cap_check_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle, run, popen = self._start(
                root, capture_side_effect=[OSError("tmux gone")] + ["pane\n"] * 10
            )
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(run.call_count, 2)
            popen.assert_called_once()
            errors = self._records(root, "post_start_assessment_error")
            self.assertEqual([e["phase"] for e in errors], ["baseline_capture"])
            self.assertIn("OSError: tmux gone", errors[0]["error"])
            self.assertEqual(errors[0]["treated_as"], "not-capped")

    def test_baseline_capture_oserror_does_not_mask_a_real_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                self._start(
                    root, adapter_factory=_CapAfterHooksAdapter,
                    capture_side_effect=[OSError("tmux gone")] + ["pane\n"] * 10,
                )
            self.assertEqual(ctx.exception.failure_class, "subscription-cap")
            self.assertEqual(len(self._records(root, "post_start_assessment_error")), 1)

    def test_sample_capture_oserror_is_recorded_and_start_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle, run, _popen = self._start(
                root, adapter_factory=_CapAfterHooksAdapter,
                capture_side_effect=["pane\n", OSError("capture-pane failed")],
            )
            # even with a cap-reporting adapter: the assessment never completed,
            # so it is recorded and treated as NOT capped (fail-open).
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(run.call_count, 2)
            errors = self._records(root, "post_start_assessment_error")
            self.assertEqual([e["phase"] for e in errors], ["assessment"])
            self.assertIn("capture-pane failed", errors[0]["error"])

    def test_adapter_assessment_exception_is_recorded_and_start_returns(self) -> None:
        class _Boom(_Adapter):
            def assess_post_start_readiness(self, **kwargs):
                raise AttributeError("adapter bug")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle, _run, _popen = self._start(root, adapter_factory=_Boom)
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(
                [e["phase"] for e in self._records(root, "post_start_assessment_error")],
                ["assessment"],
            )

    def test_template_index_and_attribute_errors_are_recorded_skips(self) -> None:
        post_start = (
            PostStartCommand(argv=("/bin/x", "{0}")),               # IndexError
            PostStartCommand(argv=("/bin/y", "{persona.nope}")),    # AttributeError
            PostStartCommand(argv=(self.ARM, "{tmux_target}", "{persona}")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle, run, _popen = self._start(root, post_start=post_start)
            self.assertEqual(handle.pid, 7777)
            # the two bad templates are skipped, the good one still runs.
            self.assertEqual(run.call_count, 1)
            skips = self._records(root, "post_start_command_skipped")
            self.assertEqual(len(skips), 2)
            self.assertIn("IndexError", skips[0]["reason"])
            self.assertIn("AttributeError", skips[1]["reason"])
            self.assertEqual(len(self._records(root, "post_start_command")), 1)

    def test_evidence_write_oserror_is_best_effort_and_all_commands_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            handle, run, popen = self._start(
                root,
                extra=(
                    mock.patch.object(rc, "_append_controller_evidence", side_effect=OSError("ENOSPC")),
                    mock.patch.object(rc.sys, "stderr", stderr),
                ),
            )
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(run.call_count, 2)
            popen.assert_called_once()
            self.assertEqual(stderr.getvalue().count("post_start_command evidence write failed"), 3)

    def test_evidence_write_oserror_does_not_swallow_the_cap_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                self._start(
                    root, adapter_factory=_CapAfterHooksAdapter,
                    extra=(
                        mock.patch.object(rc, "_append_controller_evidence", side_effect=OSError("ENOSPC")),
                        mock.patch.object(rc.sys, "stderr", io.StringIO()),
                    ),
                )
            self.assertEqual(ctx.exception.failure_class, "subscription-cap")

    def test_commands_step_unexpected_exception_is_recorded_and_assessment_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                rc, "_run_post_start_commands", side_effect=RuntimeError("unexpected")
            ), self.assertRaises(rc.RuntimeOperationalFailure):
                self._start(root, adapter_factory=_CapAfterHooksAdapter)
            self.assertEqual(
                [e["phase"] for e in self._records(root, "post_start_assessment_error")],
                ["commands"],
            )

    def test_controller_main_survives_capture_error_and_supervises(self) -> None:
        """End to end: a capture blip during post-start no longer kills
        controller_main; it records the runtime active and supervises it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = self._post_start_binding(root)
            supervised: list[str] = []
            stack, _start, capture = self._start_patches(claude)
            with stack, mock.patch.object(
                rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ), mock.patch.object(rc.subprocess, "Popen"):
                capture.side_effect = OSError("tmux gone")
                rc.controller_main(
                    "recovery", candidates=[claude], state_root=root, sleep=lambda s: None,
                    block_until_death_fn=lambda b: supervised.append(rc._binding_runtime_ref(b)),
                    persist_record_fn=lambda *a, **k: "persisted",
                )
            self.assertEqual(supervised, ["claude/anthropic-code"])
            self.assertEqual(
                [e["phase"] for e in self._records(root, "post_start_assessment_error")],
                ["baseline_capture", "assessment"],
            )
class ControllerStartPriorReceiverTests(unittest.TestCase):
    """T-4158: before the controller launches claude over a prior codex
    runtime (record or live pane), it stops the codex Telegram poller and
    proves it absent via the KEEP pre-phase handoff, fail-closed; codex over
    claude, claude-to-claude, non-comms personas and the attach branch are
    untouched. Every tmux/pane seam is mocked (shared with the T-4156 suite)."""

    def setUp(self) -> None:
        # Never page for real: every refusal path routes through this seam.
        patcher = mock.patch.object(rc, "_route_failure_alert")
        self.route = patcher.start()
        self.addCleanup(patcher.stop)

    def _patches(self, binding, **kw):
        return ControllerStartRuntimePostStartTests._start_patches(self, binding, **kw)

    def _record(self, root: Path, harness: str) -> None:
        path = rc._persona_state("recovery", root) / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"harness": harness}), encoding="utf-8")

    def _events(self, root: Path) -> list[dict[str, object]]:
        path = rc._evidence_log_path("recovery", root)
        if not path.exists():
            return []
        return [r for r in _read_jsonl(path) if str(r["event"]).startswith("controller_start_prior_receiver")]

    def _claude(self, root: Path, *, comms: bool = True):
        return _binding(
            root, harness="claude", profile="anthropic-code",
            communications=_telegram_comms() if comms else None,
        )

    def test_claude_over_codex_record_stops_and_proves_poller_absent_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _ReceiverBackend()
            order: list[str] = []
            stack, start, _capture = self._patches(binding)
            start.side_effect = lambda *a, **k: (
                order.append("start"), rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            )[1]
            original_execute = backend.execute
            backend.execute = lambda action, b: (order.append(action), original_execute(action, b))[1]
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)

            self.assertEqual(backend.events, ["recovery:stop-codex-poller", "recovery:probe-codex-absent"])
            self.assertEqual(order, ["stop-codex-poller", "probe-codex-absent", "start"])
            record, skipped = self._events(root)
            # T-4178: this backend models no pending snapshot, so the post phase is skipped.
            self.assertEqual(skipped["event"], "controller_start_prior_receiver_post_skipped")
            self.assertEqual(record["event"], "controller_start_prior_receiver_stopped")
            self.assertEqual(record["direction"], "codex-to-claude")
            self.assertEqual(record["prior_evidence"], "record")
            self.assertEqual(record["operations"], "stop-codex-poller,probe-codex-absent")

    def test_claude_over_live_foreign_codex_pane_stops_poller_before_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)  # no record: the live pane is the only evidence
            backend = _ReceiverBackend()
            stack, start, _capture = self._patches(
                binding, alive=True, pids=(5555, 7777), cmdline=["/usr/bin/codex", "--foo"]
            )
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)

            self.assertEqual(backend.events, ["recovery:stop-codex-poller", "recovery:probe-codex-absent"])
            self.assertEqual(start.call_args.kwargs["replace"], True)
            record, skipped = self._events(root)
            self.assertEqual(skipped["event"], "controller_start_prior_receiver_post_skipped")  # T-4178
            self.assertEqual(record["prior_evidence"], "live_pane")

    def test_poller_not_proven_absent_refuses_launch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _ReceiverBackend(fail="probe-codex-absent")
            stack, start, _capture = self._patches(
                binding, alive=True, pids=(5555,), cmdline=["/usr/bin/codex"]
            )
            with stack, self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)

            self.assertEqual(ctx.exception.failure_class, "operational-launch")
            self.assertEqual(ctx.exception.cause, "prior_receiver_stop_failed")
            start.assert_not_called()  # the live codex pane is never touched
            (record,) = self._events(root)
            self.assertEqual(record["event"], "controller_start_prior_receiver_stop_failed")
            self.assertIn("probe-codex-absent", str(record["error"]))
            self.assertIs(record["alert_routed"], True)
            self.assertEqual(record["alert_stamp"], "written")
            self.route.assert_called_once()
            agent, msg, vocab = self.route.call_args.args
            self.assertEqual(agent, "recovery")
            self.assertIn("claude/anthropic-code", msg)
            self.assertEqual(vocab, "controller-prior-receiver (recovery)")

    def test_refusal_alert_fires_once_then_cooldown_suppresses_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            for _ in range(3):  # three rungs of the backoff ladder
                stack, start, _capture = self._patches(binding)
                with stack, self.assertRaises(rc.RuntimeOperationalFailure):
                    rc._controller_start_runtime(
                        binding, state_root=root, receiver_backend=_ReceiverBackend(fail="stop-codex-poller")
                    )
                start.assert_not_called()
            self.route.assert_called_once()
            self.assertEqual([r["alert_routed"] for r in self._events(root)], [True, False, False])
            self.assertEqual([r["alert_suppressed"] for r in self._events(root)], ["", "cooldown", "cooldown"])

            # After the window it pages again.
            later = rc._utc_now() + timedelta(seconds=rc.PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS + 1)
            self.assertTrue(rc._alert_prior_receiver_stop_failed("recovery", "claude/x", "d", state_root=root, now=later)["alert_routed"])
            self.assertEqual(self.route.call_count, 2)

    def test_failed_alert_route_is_not_stamped_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.route.side_effect = [OSError("telegram down"), None]
            first = rc._alert_prior_receiver_stop_failed("recovery", "claude/x", "d", state_root=root)
            self.assertEqual(first, {"alert_routed": False, "alert_suppressed": "", "alert_stamp": "not-written"})
            self.assertTrue(rc._alert_prior_receiver_stop_failed("recovery", "claude/x", "d", state_root=root)["alert_routed"])
            self.assertEqual(self.route.call_count, 2)

    def test_stamp_write_oserror_never_masks_the_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            real_atomic = rc._atomic_json

            def atomic(path, payload):
                if path.name == "prior-receiver-alert.json":
                    raise OSError(30, "Read-only file system")
                return real_atomic(path, payload)

            stack, start, _capture = self._patches(binding)
            with stack, mock.patch.object(rc, "_atomic_json", side_effect=atomic), \
                    self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                rc._controller_start_runtime(
                    binding, state_root=root, receiver_backend=_ReceiverBackend(fail="probe-codex-absent")
                )
            self.assertEqual(ctx.exception.cause, "prior_receiver_stop_failed")
            start.assert_not_called()
            self.route.assert_called_once()
            (record,) = self._events(root)
            self.assertIs(record["alert_routed"], True)
            self.assertIn("OSError", str(record["alert_stamp"]))

    def test_unwritable_evidence_log_never_masks_the_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            stack, start, _capture = self._patches(binding)
            with stack, mock.patch.object(rc, "_atomic_json", side_effect=OSError("ro")), \
                    mock.patch.object(rc, "_append_controller_evidence", side_effect=OSError("ro")), \
                    self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                rc._controller_start_runtime(
                    binding, state_root=root, receiver_backend=_ReceiverBackend(fail="stop-codex-poller")
                )
            self.assertEqual(ctx.exception.cause, "prior_receiver_stop_failed")
            start.assert_not_called()

    def test_handoff_subprocess_timeout_refuses_launch_pane_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            stack, start, _capture = self._patches(
                binding, alive=True, pids=(5555,), cmdline=["/usr/bin/codex"]
            )
            timeout = subprocess.TimeoutExpired(cmd=["codex_telegram_bridge.py", "handoff"], timeout=90)
            # production path: receiver_backend=None -> KEEP bridge subprocess
            with stack, mock.patch.object(
                harness_integration.subprocess, "run", side_effect=timeout
            ) as run, self.assertRaises(rc.RuntimeOperationalFailure) as ctx:
                rc._controller_start_runtime(binding, state_root=root)

            run.assert_called_once()
            self.assertIn("handoff", run.call_args.args[0])
            self.assertIn("codex-to-claude", run.call_args.args[0])
            self.assertEqual(ctx.exception.cause, "prior_receiver_stop_failed")
            self.assertIsInstance(ctx.exception.__cause__, subprocess.TimeoutExpired)
            start.assert_not_called()
            (record,) = self._events(root)
            self.assertIn("TimeoutExpired", str(record["error"]))

    def test_failure_policy_via_controller_main_cools_claude_and_reselects_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = self._claude(root)
            codex = _binding(root, harness="codex", profile="interactive-chatgpt", communications=_telegram_comms())
            self._record(root, "codex")
            stack, start, _capture = self._patches(claude)
            with stack, mock.patch.object(
                rc, "_run_integration_handoff", side_effect=RuntimeError("codex receiver still present")
            ):
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                    # default start_runtime_fn -> real _controller_start_runtime
                    block_until_death_fn=lambda b: self.fail("a refused start must not be supervised"),
                    persist_record_fn=lambda *a, **k: "persisted",
                )
            self.assertEqual(result, 1)
            start.assert_not_called()
            attempt = rc.load_last_attempt("recovery", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "failed")
            self.assertEqual(attempt["failure_class"], "operational_failure")
            self.assertIsNotNone(attempt["cooldown_until"])

            selected: list[str] = []

            def fake_start(binding):
                selected.append(rc._binding_runtime_ref(binding))
                return (
                    rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                    rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()),
                )

            rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                start_runtime_fn=fake_start, block_until_death_fn=lambda b: None,
                persist_record_fn=lambda *a, **k: "persisted",
            )
            self.assertEqual(selected, ["codex/interactive-chatgpt"])

    def test_claude_to_claude_restart_runs_no_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "claude")
            stack, start, _capture = self._patches(
                binding, alive=True, pids=(5555, 7777), cmdline=["/usr/bin/claude", "--model", "other"]
            )
            # T-4179: the start path now PROBES for a codex poller; none present.
            backend = _PollerBackend([], poller_present=False)
            with stack, mock.patch.object(rc, "_run_integration_handoff") as handoff:
                # foreign-pane respawn (argv matches, but patch forces a mismatch)
                with mock.patch.object(rc, "_pane_argv_matches_binding", return_value=False):
                    rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            self.assertEqual(backend.events, ["codex:proof-absent"])
            handoff.assert_not_called()
            start.assert_called_once()
            self.assertEqual(self._events(root), [])

    def test_codex_over_claude_runs_no_codex_poller_stop(self) -> None:
        # T-4184 supersedes "codex over claude is unchanged": that direction now
        # runs its own claude-to-codex quiesce + poller start
        # (ControllerStartCodexOverClaudeReceiverTests); T-4158's stop of the
        # codex poller must still never run for a codex selection.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex", profile="interactive-chatgpt", communications=_telegram_comms())
            self._record(root, "claude")
            stack, start, _capture = self._patches(
                binding, alive=True, pids=(5555, 7777), cmdline=["/usr/bin/claude"]
            )
            backend = _ReceiverBackend()
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            start.assert_called_once()
            self.assertNotIn("recovery:stop-codex-poller", backend.events)
            self.assertLess(
                backend.events.index("recovery:stop-dormant-claude-receiver"),
                backend.events.index("recovery:start-codex-poller"),
            )
            self.assertEqual(
                [e["event"] for e in self._events(root)],
                ["controller_start_prior_receiver_quiesced"],  # no T-4158 stop record
            )

    def test_non_comms_persona_over_codex_record_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root, comms=False)
            self._record(root, "codex")
            stack, start, _capture = self._patches(binding)
            with stack, mock.patch.object(rc, "_run_integration_handoff") as handoff:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=_ReceiverBackend())
            handoff.assert_not_called()
            start.assert_called_once()
            self.assertEqual(self._events(root), [])

    def _attach(self, root: Path, binding, backend):
        stack, start, _capture = self._patches(
            binding, alive=True, pids=(4242,), cmdline=[str(binding.launch.executable)]
        )
        with stack:
            _rs, handle = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
        start.assert_not_called()  # attach, never a respawn
        self.assertEqual(handle.pid, 4242)
        return handle

    def test_attach_branch_heals_codex_record_by_stopping_poller(self) -> None:
        # T-4179 (supersedes T-4158's "attach runs no handoff"): the sticky
        # state -- consume launched claude over a live codex poller, the
        # controller then attaches to that claude pane -- is healed here.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _PollerBackend([], poller_present=True)
            self._attach(root, binding, backend)
            self.assertEqual(backend.events, ["claude:stop-codex-poller", "claude:probe-codex-absent"])
            self.assertFalse(backend.poller_present)
            (record,) = self._events(root)
            self.assertEqual(record["event"], "controller_start_prior_receiver_stopped")
            self.assertEqual(record["refusal"], "alert-only")

    def test_attach_branch_probe_finds_poller_with_claude_record_and_stops_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "claude")  # record drifted to claude; only the probe sees it
            backend = _PollerBackend([], poller_present=True)
            self._attach(root, binding, backend)
            self.assertEqual(
                backend.events,
                ["codex:proof-absent", "claude:stop-codex-poller", "claude:probe-codex-absent"],
            )
            (record,) = self._events(root)
            self.assertEqual(record["prior_evidence"], "probe")

    def test_attach_branch_failed_stop_alerts_but_still_attaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            backend = _PollerBackend([], poller_present=True, stop_ineffective=True)
            self._attach(root, binding, backend)  # no raise: claude is already running
            (record,) = self._events(root)
            self.assertEqual(record["event"], "controller_start_prior_receiver_stop_failed")
            self.assertEqual(record["refusal"], "alert-only")
            self.route.assert_called_once()
            _agent, msg, _vocab = self.route.call_args.args
            self.assertIn("alongside the live claude/anthropic-code", msg)
            self.assertNotIn("refused", msg)

    def test_attach_branch_no_poller_is_probe_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            backend = _PollerBackend([], poller_present=False)
            with mock.patch.object(rc, "_run_integration_handoff") as handoff:
                self._attach(root, binding, backend)
            self.assertEqual(backend.events, ["codex:proof-absent"])
            handoff.assert_not_called()
            self.assertEqual(self._events(root), [])

    def test_start_path_probe_catches_poller_with_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)  # no runtime.json, no live pane
            backend = _PollerBackend([], poller_present=True)
            stack, start, _capture = self._patches(binding)
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            self.assertEqual(
                backend.events,
                ["codex:proof-absent", "claude:stop-codex-poller", "claude:probe-codex-absent"],
            )
            start.assert_called_once()
            # T-4178's POST logs its own (skip) record after a PRE; this test
            # is about the PRE's evidence source.
            (record,) = [e for e in self._events(root) if e["event"] == "controller_start_prior_receiver_stopped"]
            self.assertEqual(record["prior_evidence"], "probe")
            self.assertEqual(record["refusal"], "operational")


class ConsumePathPriorReceiverTests(unittest.TestCase):
    """T-4179: the consume/wake launch path (ensure_runtime, via the
    initial-viability loop, and the rehydrate path) proves the codex Telegram
    poller absent before launching claude, fail-closed as
    RuntimeViabilityBlocked (exit-5 policy skip, no cascade to codex)."""

    def setUp(self) -> None:
        patcher = mock.patch.object(rc, "_route_failure_alert")
        self.route = patcher.start()
        self.addCleanup(patcher.stop)

    def _bindings(self, root: Path):
        return InitialViabilityTests._bindings(self, root)

    def _record(self, root: Path, harness: str) -> None:
        path = rc._persona_state("recovery", root) / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"harness": harness}), encoding="utf-8")

    def _events(self, root: Path) -> list[dict[str, object]]:
        path = rc._evidence_log_path("recovery", root)
        if not path.exists():
            return []
        return [r for r in _read_jsonl(path) if str(r["event"]).startswith("controller_start_prior_receiver")]

    def _select(self, root: Path, backend: "_PollerBackend", *, candidates=None):
        primary, fallback = self._bindings(root)
        patches = InitialViabilityTests._selector_patches(self, backend.events)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return rc.ensure_viable_runtime(
                tuple(candidates or (primary, fallback)),
                adapter_factory=_Adapter,
                state_root=root,
                backend=backend,
                now=datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc),
            )

    def test_planned_swap_then_codex_death_stops_poller_before_claude_launch(self) -> None:
        # planned_swap left runtime.json=codex and a running poller; the codex
        # pane died; consume's viability loop now launches claude.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            backend = _PollerBackend([], poller_present=True)
            selection = self._select(root, backend)
            self.assertEqual(selection.binding.launch.harness, "claude")
            self.assertFalse(backend.poller_present)
            events = backend.events
            self.assertLess(events.index("claude:stop-codex-poller"), events.index("claude:probe-codex-absent"))
            self.assertLess(events.index("claude:probe-codex-absent"), events.index("claude:start"))
            self.assertNotIn("codex:start", events)
            (record,) = self._events(root)
            self.assertEqual(record["event"], "controller_start_prior_receiver_stopped")
            self.assertEqual(record["prior_evidence"], "record")
            self.assertEqual(record["refusal"], "viability-blocked")

    def test_cleared_runtime_record_with_live_poller_is_caught_by_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # no runtime.json at all
            backend = _PollerBackend([], poller_present=True)
            self._select(root, backend)
            events = backend.events
            self.assertLess(events.index("codex:proof-absent"), events.index("claude:stop-codex-poller"))
            self.assertLess(events.index("claude:probe-codex-absent"), events.index("claude:start"))
            (record,) = self._events(root)
            self.assertEqual(record["prior_evidence"], "probe")
            self.assertEqual(record["probe_state"], "unknown")

    def test_failed_stop_raises_viability_blocked_without_codex_cascade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            backend = _PollerBackend([], poller_present=True, stop_ineffective=True)
            with self.assertRaises(rc.RuntimeViabilityBlocked) as ctx:
                self._select(root, backend)
            self.assertEqual(ctx.exception.reason, "prior-receiver-not-proven-absent")
            self.assertEqual(ctx.exception.policy_skip_reason, "prior_receiver_blocked")
            self.assertEqual(ctx.exception.evidence["refusal"], "viability-blocked")
            self.assertIs(ctx.exception.evidence["alert_routed"], True)
            # No launch at all, and NO cascade: codex is never started and no
            # compensation runs (the only receiver actions are the refused stop).
            self.assertNotIn("claude:start", backend.events)
            self.assertNotIn("codex:start", backend.events)
            self.assertFalse([e for e in backend.events if e.startswith("codex:") and "proof" not in e])
            self.assertEqual(
                [e for e in backend.events if e.startswith("claude:")],
                ["claude:stop-codex-poller", "claude:probe-codex-absent"],
            )
            self.assertTrue(backend.poller_present)
            self.route.assert_called_once()
            self.assertIn("refused to launch", self.route.call_args.args[1])
            self.assertFalse(rc.initial_viability_state("recovery", state_root=root).get("status") == "selected")

    def test_blocked_consume_exits_5_policy_skip(self) -> None:
        # The exit-5 contract: persona_bringup maps RuntimeViabilityBlocked to
        # a policy skip. Drive the wake entry point with the real ensure_runtime.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            primary, fallback = self._bindings(root)
            backend = _PollerBackend([], poller_present=True, stop_ineffective=True)
            patches = InitialViabilityTests._selector_patches(self, backend.events)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                with self.assertRaises(rc.RuntimeViabilityBlocked):
                    wake_persistent_runtime(
                        primary,
                        adapter=_Adapter(primary),
                        fallback_binding=fallback,
                        receiver_backend=backend,
                        state_dir=root,
                        cwd=root,
                    )
            self.assertNotIn("codex:start", backend.events)

    def test_no_poller_is_probe_only_no_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _PollerBackend([], poller_present=False)
            with mock.patch.object(rc, "_run_integration_handoff", wraps=rc._run_integration_handoff) as handoff:
                self._select(root, backend)
            self.assertIn("codex:proof-absent", backend.events)
            self.assertNotIn("claude:stop-codex-poller", backend.events)
            self.assertFalse(
                [c for c in handoff.call_args_list if c.kwargs.get("reason") == "controller-start-over-prior-harness"]
            )
            self.assertEqual(self._events(root), [])

    def test_non_comms_persona_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            binding = _binding(root, harness="claude", profile="primary", communications=None)
            backend = _PollerBackend([], poller_present=True)
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            ) as start, mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                rc.ensure_runtime(binding, _Adapter(binding), state_root=root, receiver_backend=backend)
            start.assert_called_once()
            self.assertEqual(backend.events, [])
            self.assertTrue(backend.poller_present)

    def test_codex_launch_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            _primary, fallback = self._bindings(root)
            backend = _PollerBackend([], poller_present=True)
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            ) as start, mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                rc.ensure_runtime(fallback, _Adapter(fallback), state_root=root, receiver_backend=backend)
            start.assert_called_once()
            self.assertEqual(backend.events, [])

    def test_live_pane_reuse_runs_no_probe(self) -> None:
        # Consume fires every ~2 min; reuse of a live pane must cost nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, _fallback = self._bindings(root)
            rc._atomic_json(
                rc._persona_state("recovery", root) / "runtime.json",
                rc._runtime_payload(primary, session_id=None, started_at=rc._utc_now()),
            )
            backend = _PollerBackend([], poller_present=True)
            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=1
            ), mock.patch.object(rc, "_pid_in_scope", return_value=True):
                rc.ensure_runtime(primary, _Adapter(primary), state_root=root, receiver_backend=backend)
            self.assertEqual(backend.events, [])

    def test_predecessor_proof_for_claude_is_unchanged_and_probe_free(self) -> None:
        # The continuity coordinator calls this for an OLD binding's absence;
        # T-4179 keeps it probe-free (rehydrate stops-then-proves instead).
        with tempfile.TemporaryDirectory() as tmp:
            primary, _fallback = self._bindings(Path(tmp))
            with mock.patch.object(rc, "_probe_managed_receiver") as probe:
                self.assertTrue(rc.prove_predecessor_receiver_absent(primary))
            probe.assert_not_called()

    def _rehydrate(self, root: Path, backend: "_PollerBackend", start):
        primary, _fallback = self._bindings(root)
        rc._atomic_json(
            rc._persona_state("recovery", root) / "runtime.json",
            rc._runtime_payload(primary, session_id=None, started_at=rc._utc_now()),
        )
        with mock.patch.object(rc, "_recorded_scope_inactive", return_value=True), mock.patch.object(
            rc, "prove_managed_receiver_present", return_value=True
        ), mock.patch.object(rc, "_start_runtime", side_effect=start), mock.patch.object(
            rc,
            "_authenticate_recorded_selected_runtime",
            return_value=({"revision": "r"}, {"revision": "r"}, {"state": "present"}, "same-revision"),
        ), self.assertRaises(rc.RuntimeViabilityBlocked) as ctx:
            rc._rehydrate_recorded_runtime_selection(
                primary,
                {"runtime_scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT},
                adapter_factory=_Adapter,
                state_root=root,
                backend=backend,
                context=None,
                now=rc._utc_now(),
                manifest_order=(rc._runtime_ref(primary),),
                attempt_limit=1,
            )
        return ctx.exception

    def test_rehydrate_claude_stops_poller_then_launches(self) -> None:
        # Reviewer finding 4: rehydrate must heal (stop-then-prove), not only
        # block, else a consume-only seat is blocked on every fire.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _PollerBackend([], poller_present=True)

            def start(*_a, **_k):
                backend.events.append("claude:start")
                raise RuntimeError("SENTINEL-stop-after-start")  # end the test past the launch

            exc = self._rehydrate(root, backend, start)
            self.assertEqual(exc.reason, "recorded-runtime-rehydrate-failed")  # the sentinel, not a refusal
            self.assertEqual(
                backend.events,
                ["codex:proof-absent", "claude:stop-codex-poller", "claude:probe-codex-absent", "claude:start"],
            )
            self.assertFalse(backend.poller_present)
            (record,) = self._events(root)
            self.assertEqual(record["event"], "controller_start_prior_receiver_stopped")
            self.route.assert_not_called()

    def test_rehydrate_failed_stop_blocks_with_distinct_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _PollerBackend([], poller_present=True, stop_ineffective=True)
            start = mock.Mock()
            exc = self._rehydrate(root, backend, start)
            self.assertEqual(exc.reason, "prior-receiver-not-proven-absent")
            self.assertEqual(exc.policy_skip_reason, rc.PRIOR_RECEIVER_POLICY_SKIP_REASON)
            start.assert_not_called()
            self.route.assert_called_once()

    def test_prior_receiver_block_persists_distinct_policy_skip_reason(self) -> None:
        # Reviewer finding 2: specialist's expected_policy_skip is
        # runtime_viability_blocked; a prior-receiver refusal must persist its
        # OWN coarse token so fleet_health renders it WARN, not OK+expected.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            backend = _PollerBackend([], poller_present=True, stop_ineffective=True)
            with self.assertRaises(rc.RuntimeViabilityBlocked) as ctx:
                self._select(root, backend)
            block = ctx.exception
            self.assertEqual(block.policy_skip_reason, "prior_receiver_blocked")

            manifest = _telegram_operator_manifest(root, account=pwd.getpwuid(os.geteuid()).pw_name)
            state_dir = root / "state"
            report = DiagnosticReport(harness="claude", execution_context="test", observations=())
            with mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), mock.patch.object(
                persona_bringup, "wake_persistent_runtime", side_effect=block
            ), mock.patch.object(persona_bringup, "_print_json") as printed:
                code = persona_bringup.main([
                    "consume", "--persona", "recovery", "--manifest", str(manifest),
                    "--mailbox-hub", str(root / "mail"), "--state-dir", str(state_dir),
                ])
            self.assertEqual(code, 5)
            self.assertEqual(printed.call_args.args[0]["consume"]["skipped"], "prior_receiver_blocked")
            self.assertEqual(
                persona_bringup.load_policy_skip(state_dir, "recovery")["reason"], "prior_receiver_blocked"
            )

    def test_other_viability_blocks_keep_the_coarse_reason(self) -> None:
        block = rc.RuntimeViabilityBlocked("recorded-source-hash-mismatch", {})
        self.assertEqual(block.policy_skip_reason, "runtime_viability_blocked")

    def test_coordinator_opt_out_skips_the_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            primary, _fallback = self._bindings(root)
            backend = _PollerBackend([], poller_present=True)
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            ) as start, mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), _provenance_patch():
                rc.ensure_runtime(
                    primary, _Adapter(primary), state_root=root,
                    receiver_backend=backend, stop_prior_receiver=False,
                )
            start.assert_called_once()
            self.assertEqual(backend.events, [])
            self.assertTrue(backend.poller_present)




class _SnapshotReceiverBackend(_ReceiverBackend):
    """`_ReceiverBackend` plus the T-4178 pending-topology-snapshot probe."""

    def __init__(self, *, pending: bool | Exception = True, **kw):
        super().__init__(**kw)
        self.pending = pending
        self.pending_probes = 0

    def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
        # T-4179: the controller start path now PROBES for a codex poller
        # before launching claude. This backend models none running (T-4178's
        # scenarios drive the PRE through the record/live-pane evidence), so
        # the probe never falls through to the real bridge subprocess.
        if binding.launch.harness == "codex":
            return ReceiverStateProof("absent", {"harness": "codex"})
        return ReceiverStateProof(expected, {"harness": binding.launch.harness})

    def receiver_topology_snapshot_pending(self, binding):
        self.pending_probes += 1
        if isinstance(self.pending, Exception):
            raise self.pending
        return self.pending


class ControllerStartPriorReceiverPostTests(unittest.TestCase):
    """T-4178: after the controller launches claude over a prior codex runtime
    (T-4158's PRE ran), and only once claude is input-ready and past the
    post_start cap assessment, it runs the codex-to-claude POST phase (claude
    receiver present, watchdog restore, snapshot complete) when a claude-to-
    codex PRE left a pending topology snapshot. Safe no-op otherwise; fail-open
    with a cooldown-gated alert on failure."""

    POST_OPS = [
        "recovery:probe-claude-present",
        "recovery:restore-watchdog-units",
        "recovery:complete-receiver-topology-snapshot",
    ]
    PRE_OPS = ["recovery:stop-codex-poller", "recovery:probe-codex-absent"]

    def setUp(self) -> None:
        patcher = mock.patch.object(rc, "_route_failure_alert")
        self.route = patcher.start()
        self.addCleanup(patcher.stop)

    def _patches(self, binding, **kw):
        return ControllerStartRuntimePostStartTests._start_patches(self, binding, **kw)

    def _record(self, root: Path, harness: str) -> None:
        ControllerStartPriorReceiverTests._record(self, root, harness)

    def _events(self, root: Path) -> list[dict[str, object]]:
        return ControllerStartPriorReceiverTests._events(self, root)

    def _claude(self, root: Path, *, comms: bool = True):
        return ControllerStartPriorReceiverTests._claude(self, root, comms=comms)

    def test_claude_over_codex_runs_pre_before_launch_and_post_after_input_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _SnapshotReceiverBackend(pending=True)
            order: list[str] = []
            original_execute = backend.execute
            backend.execute = lambda action, b: (order.append(action), original_execute(action, b))[1]
            stack, start, _capture = self._patches(binding)
            start.side_effect = lambda *a, **k: (
                order.append("start"), rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            )[1]
            with stack, mock.patch.object(
                rc, "_verify_runtime_input_ready", side_effect=lambda *a, **k: order.append("input-ready")
            ), mock.patch.object(
                rc, "_run_and_assess_post_start", side_effect=lambda *a, **k: order.append("post_start")
            ):
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)

            self.assertEqual(
                order,
                [
                    "stop-codex-poller", "probe-codex-absent", "start", "input-ready", "post_start",
                    "probe-claude-present", "restore-watchdog-units", "complete-receiver-topology-snapshot",
                ],
            )
            self.assertEqual(backend.events, self.PRE_OPS + self.POST_OPS)
            stopped, restored = self._events(root)
            self.assertEqual(stopped["event"], "controller_start_prior_receiver_stopped")
            self.assertEqual(restored["event"], "controller_start_prior_receiver_restored")
            self.assertEqual(restored["direction"], "codex-to-claude")
            self.assertEqual(restored["phase"], "post")
            self.assertEqual(
                restored["operations"],
                "probe-claude-present,restore-watchdog-units,complete-receiver-topology-snapshot",
            )
            self.route.assert_not_called()

    def test_no_pending_snapshot_is_a_safe_no_op(self) -> None:
        """The common initial-bootstrap case: codex ran POST only, so no
        claude-to-codex PRE snapshot exists; the bridge post phase would refuse
        on it, so the controller must not run it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _SnapshotReceiverBackend(pending=False)
            stack, start, _capture = self._patches(binding)
            with stack:
                _rs, handle = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            self.assertEqual(handle.pid, 7777)
            self.assertEqual(backend.events, self.PRE_OPS)
            self.assertEqual(backend.pending_probes, 1)
            stopped, skipped = self._events(root)
            self.assertEqual(skipped["event"], "controller_start_prior_receiver_post_skipped")
            self.assertEqual(skipped["reason"], "no-pending-topology-snapshot")
            self.route.assert_not_called()

    def test_post_failure_is_fail_open_with_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _SnapshotReceiverBackend(pending=True, fail="probe-claude-present")
            stack, start, _capture = self._patches(binding)
            with stack:
                _rs, handle = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            start.assert_called_once()
            self.assertEqual(handle.pid, 7777)  # the live claude start is returned, not failed
            self.assertEqual(backend.events, self.PRE_OPS)  # no restore/complete after a failed proof
            _stopped, failed = self._events(root)
            self.assertEqual(failed["event"], "controller_start_prior_receiver_post_failed")
            self.assertIn("probe-claude-present", str(failed["error"]))
            self.assertIs(failed["alert_routed"], True)
            self.route.assert_called_once()
            agent, msg, vocab = self.route.call_args.args
            self.assertEqual(agent, "recovery")
            self.assertIn("degraded, not dark", msg)
            self.assertEqual(vocab, "controller-prior-receiver-restore (recovery)")

            # A second failing start inside the window routes no second page.
            stack, _start, _capture = self._patches(binding)
            with stack:
                rc._controller_start_runtime(
                    binding, state_root=root,
                    receiver_backend=_SnapshotReceiverBackend(pending=True, fail="restore-watchdog-units"),
                )
            self.route.assert_called_once()
            self.assertEqual(self._events(root)[-1]["alert_suppressed"], "cooldown")

    def test_restore_alert_has_its_own_cooldown_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(rc._alert_prior_receiver_stop_failed("recovery", "claude/x", "d", state_root=root)["alert_routed"])
            self.assertTrue(rc._alert_prior_receiver_restore_failed("recovery", "claude/x", "d", state_root=root)["alert_routed"])
            self.assertEqual(self.route.call_count, 2)

    def test_snapshot_probe_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _SnapshotReceiverBackend(pending=RuntimeError("snapshot unreadable"))
            stack, _start, _capture = self._patches(binding)
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            self.assertEqual(backend.events, self.PRE_OPS)
            self.assertEqual(self._events(root)[-1]["event"], "controller_start_prior_receiver_post_failed")
            self.route.assert_called_once()

    def test_unwritable_evidence_never_raises_out_of_a_live_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            real_append = rc._append_controller_evidence

            def append(persona, event, fields, **kw):
                if "restored" in event or "post_" in event:
                    raise OSError("ro")
                return real_append(persona, event, fields, **kw)

            for backend in (
                _SnapshotReceiverBackend(pending=True),
                _SnapshotReceiverBackend(pending=True, fail="complete-receiver-topology-snapshot"),
                _SnapshotReceiverBackend(pending=False),
            ):
                stack, _start, _capture = self._patches(binding)
                with stack, mock.patch.object(rc, "_append_controller_evidence", side_effect=append):
                    _rs, handle = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
                self.assertEqual(handle.pid, 7777)

    def test_post_start_cap_assessment_failure_skips_post_phase(self) -> None:
        """Ordering: a watchdog is never restored for a runtime the post_start
        assessment then rejects (the seat swaps back to codex)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            backend = _SnapshotReceiverBackend(pending=True)
            stack, _start, _capture = self._patches(binding)
            cap = rc.RuntimeOperationalFailure(
                "capped", failure_class="subscription_cap", cause="cap", binding_runtime="claude/anthropic-code"
            )
            with stack, mock.patch.object(rc, "_run_and_assess_post_start", side_effect=cap), \
                    self.assertRaises(rc.RuntimeOperationalFailure):
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
            self.assertEqual(backend.events, self.PRE_OPS)
            self.assertEqual(backend.pending_probes, 0)

    def _claude_to_claude_start(self, root: Path, binding, backend) -> None:
        """A claude-to-claude restart (respawn over a foreign claude pane):
        T-4158's PRE does not run (the record says claude)."""
        stack, _start, _capture = self._patches(
            binding, alive=True, pids=(5555, 7777), cmdline=["/usr/bin/claude", "--model", "other"]
        )
        with stack, mock.patch.object(rc, "_pane_argv_matches_binding", return_value=False):
            rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)

    def test_failed_restore_is_retried_by_a_later_claude_to_claude_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "codex")
            failing = _SnapshotReceiverBackend(pending=True, fail="probe-claude-present")
            stack, _start, _capture = self._patches(binding)
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=failing)
            self.assertEqual(self._events(root)[-1]["event"], "controller_start_prior_receiver_post_failed")
            self.assertIn("retried on each", self.route.call_args.args[1])

            # The launch rewrote the record to claude; the next restart has no PRE ...
            self._record(root, "claude")
            retry = _SnapshotReceiverBackend(pending=True)
            self._claude_to_claude_start(root, binding, retry)
            # ... but still retries the post phase, and restores.
            self.assertEqual(retry.events, self.POST_OPS)
            restored = self._events(root)[-1]
            self.assertEqual(restored["event"], "controller_start_prior_receiver_restored")
            self.assertIs(restored["pre_ran"], False)

    def test_claude_to_claude_without_pending_snapshot_is_a_silent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "claude")
            backend = _SnapshotReceiverBackend(pending=False)
            self._claude_to_claude_start(root, binding, backend)
            self.assertEqual(backend.pending_probes, 1)
            self.assertEqual(backend.events, [])
            self.assertEqual(self._events(root), [])  # no skip record without a PRE
            self.route.assert_not_called()

    def test_production_post_phase_inner_deadline_is_shorter_than_outer_timeout(self) -> None:
        """LOW (timeout race): the bridge's presence-probe deadline must end
        clearly before the outer subprocess kill, so the kill never lands
        mid watchdog-restore. Other callers keep inner == outer."""
        binding = self._claude(Path("/tmp"))
        receipt = json.dumps({"ok": True, "operations": ["x"]})
        with mock.patch.object(rc, "_receiver_topology_snapshot_pending", return_value=True), \
                mock.patch.object(
                    rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=receipt, stderr="")
                ) as run:
            got = rc._complete_prior_harness_receiver_after_start(binding, None, state_root=Path("/nonexistent"))
        self.assertEqual(got.operations, ("x",))
        argv = run.call_args.args[0]
        self.assertIn("handoff", argv)
        self.assertEqual(argv[argv.index("--phase") + 1], "post")
        self.assertEqual(argv[argv.index("--direction") + 1], "codex-to-claude")
        inner = int(argv[argv.index("--timeout-seconds") + 1])
        outer = run.call_args.kwargs["timeout"]
        self.assertEqual(inner, rc.PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS)
        self.assertEqual(outer, rc.PRIOR_RECEIVER_POST_OUTER_TIMEOUT_SECONDS)
        self.assertLessEqual(inner, outer - 20)
        # Default callers are unchanged: inner == outer.
        with mock.patch.object(
            rc.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=receipt, stderr="")
        ) as run:
            rc._run_bridge_handoff_subprocess(binding, "codex-to-claude", "pre", reason="r", timeout_seconds=90)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--timeout-seconds") + 1], "90")
        self.assertEqual(run.call_args.kwargs["timeout"], 90)

    def test_no_probe_where_post_cannot_apply(self) -> None:
        """The attach branch, codex over claude and a non-comms persona never
        even probe the snapshot, let alone run the post phase."""
        cases = {
            "attach": dict(harness="claude", record="codex", comms=True, patches="attach", force_mismatch=False),
            "codex-over-claude": dict(
                harness="codex", record="claude", comms=True,
                patches=dict(alive=True, pids=(5555, 7777), cmdline=["/usr/bin/claude"]),
                force_mismatch=False,
            ),
            "non-comms": dict(harness="claude", record="codex", comms=False, patches={}, force_mismatch=False),
        }
        for name, case in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if case["harness"] == "claude":
                    binding = self._claude(root, comms=case["comms"])
                else:
                    binding = _binding(
                        root, harness="codex", profile="interactive-chatgpt", communications=_telegram_comms()
                    )
                self._record(root, case["record"])
                patches = case["patches"]
                if patches == "attach":
                    patches = dict(alive=True, pids=(4242,), cmdline=[str(binding.launch.executable)])
                backend = _SnapshotReceiverBackend(pending=True)
                stack, _start, _capture = self._patches(binding, **patches)
                with stack, mock.patch.object(
                    rc, "_pane_argv_matches_binding", return_value=False
                ) if case["force_mismatch"] else contextlib.nullcontext():
                    rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
                self.assertEqual(backend.pending_probes, 0)  # the POST never runs here
                if name == "attach":
                    # T-4179 sticky heal: the attach branch stops the codex
                    # poller named by the record (PRE only; no POST on attach).
                    self.assertEqual(backend.events, self.PRE_OPS)
                    self.assertEqual(
                        [e["event"] for e in self._events(root)], ["controller_start_prior_receiver_stopped"]
                    )
                elif name == "codex-over-claude":
                    # T-4184: the claude-to-codex handoff runs instead; the
                    # codex-to-claude POST (restore + complete) never does.
                    self.assertNotIn("recovery:restore-watchdog-units", backend.events)
                    self.assertNotIn("recovery:complete-receiver-topology-snapshot", backend.events)
                    self.assertIn("recovery:start-codex-poller", backend.events)
                else:
                    self.assertEqual(backend.events, [])
                    self.assertEqual(self._events(root), [])
                self.route.assert_not_called()

    def test_production_snapshot_probe_parses_bridge_json(self) -> None:
        binding = SimpleNamespace(persona="recovery")
        ok = mock.Mock(returncode=0, stdout=json.dumps({"ok": True, "pending": False}), stderr="")
        with mock.patch.object(rc.subprocess, "run", return_value=ok) as run:
            self.assertFalse(rc._receiver_topology_snapshot_pending(binding, backend=None))
        argv = run.call_args.args[0]
        self.assertIn("topology-snapshot-state", argv)
        self.assertEqual(argv[argv.index("--persona") + 1], "recovery")
        for stdout, code in (
            (json.dumps({"ok": False, "error": "snapshot unreadable"}), 1),
            ("not json", 0),
            (json.dumps({"ok": True}), 0),
        ):
            with self.subTest(stdout=stdout), mock.patch.object(
                rc.subprocess, "run", return_value=mock.Mock(returncode=code, stdout=stdout, stderr="")
            ), self.assertRaises(RuntimeError):
                rc._receiver_topology_snapshot_pending(binding, backend=None)


class _CodexSeatBackend:
    """T-4184: a stateful model of one comms seat's Telegram side, driven by the
    KEEP action names (the real bridge is never called). State: the claude
    watchdog units (enabled), the receiver topology snapshot (first-wins merge,
    as the bridge's `_snapshot_units`), the codex poller, and whether the claude
    receiver (runtime-owned: inside the claude session) is present. ``fail`` is
    a set of actions that raise."""

    def __init__(
        self,
        *,
        watchdog: bool = True,
        claude_present: bool = False,
        poller: bool = False,
        snapshot: dict | None = None,
        fail: tuple[str, ...] = (),
        pending_error: Exception | None = None,
    ) -> None:
        self.watchdog = watchdog
        self.claude_present = claude_present
        self.poller = poller
        self.snapshot = snapshot  # {"watchdog": bool, "complete": bool} or None
        self.fail = set(fail)
        self.pending_error = pending_error
        self.events: list[str] = []
        self.pending_probes = 0

    def execute(self, action, binding):
        self.events.append(action)
        if action in self.fail:
            raise RuntimeError(f"fail {action}")
        if action == "snapshot-watchdog-units":
            if self.snapshot is None or self.snapshot["complete"]:
                self.snapshot = {"watchdog": self.watchdog, "complete": False}
            # else: first-wins, the original state is kept
        elif action in ("disable-watchdog-timer", "stop-watchdog-service"):
            self.watchdog = False
        elif action == "restore-watchdog-units":
            if self.snapshot is None or self.snapshot["complete"]:
                raise RuntimeError("receiver topology snapshot missing or complete")
            self.watchdog = self.snapshot["watchdog"]
        elif action == "complete-receiver-topology-snapshot":
            if self.snapshot is None:
                raise RuntimeError("receiver topology snapshot missing")
            self.snapshot = {**self.snapshot, "complete": True}
        elif action == "probe-claude-present" and not self.claude_present:
            raise RuntimeError("claude receiver absent")
        elif action == "probe-claude-absent" and self.claude_present:
            raise RuntimeError("claude receiver still present")
        elif action == "start-codex-poller":
            self.poller = True
        elif action in ("probe-codex-present", "health-codex-no-409") and not self.poller:
            raise RuntimeError("codex receiver unit is not active")
        elif action == "stop-codex-poller":
            self.poller = False
        elif action == "probe-codex-absent" and self.poller:
            raise RuntimeError("codex receiver still present")
        return {"ok": True, "action": action}

    def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
        self.events.append(f"proof:{binding.launch.harness}:{expected}")
        present = self.poller if binding.launch.harness == "codex" else self.claude_present
        return ReceiverStateProof("present" if present else "absent", {"harness": binding.launch.harness})

    def receiver_topology_snapshot_pending(self, binding):
        self.pending_probes += 1
        if self.pending_error is not None:
            raise self.pending_error
        return self.snapshot is not None and not self.snapshot["complete"]


class CodexUnprovenPriorRuntimeActionTests(unittest.TestCase):
    """T-4184: the KEEP action ordering's ``prior_runtime`` keyword."""

    WATCHDOG = ("recovery-telegram-child-watchdog.timer", "recovery-telegram-child-watchdog.service")

    def test_unproven_pre_drops_only_the_claude_presence_proof(self) -> None:
        from tools.codex_telegram_actions import receiver_handoff_actions

        live = receiver_handoff_actions(
            claude_receiver_topology="runtime-owned", watchdog_units=self.WATCHDOG,
            direction="claude-to-codex", phase="pre",
        )
        unproven = receiver_handoff_actions(
            claude_receiver_topology="runtime-owned", watchdog_units=self.WATCHDOG,
            direction="claude-to-codex", phase="pre", prior_runtime="unproven",
        )
        self.assertEqual(
            live,
            (
                "snapshot-watchdog-units", "disable-watchdog-timer", "stop-watchdog-service",
                "snapshot-dormant-claude-unit", "disable-dormant-claude-receiver",
                "stop-dormant-claude-receiver", "probe-claude-present",
            ),
        )  # the default is byte-identical to the pre-T-4184 ordering
        self.assertEqual(unproven, live[:-1])
        self.assertNotIn("probe-claude-present", unproven)

    def test_unproven_is_a_no_op_for_every_other_phase_and_topology(self) -> None:
        from tools.codex_telegram_actions import receiver_handoff_actions

        for topology in ("runtime-owned", "systemd-user"):
            for direction in ("claude-to-codex", "codex-to-claude"):
                for phase in ("pre", "post"):
                    if (topology, direction, phase) == ("runtime-owned", "claude-to-codex", "pre"):
                        continue
                    kw = dict(claude_receiver_topology=topology, watchdog_units=self.WATCHDOG, direction=direction, phase=phase)
                    with self.subTest(topology=topology, direction=direction, phase=phase):
                        self.assertEqual(
                            receiver_handoff_actions(**kw, prior_runtime="unproven"), receiver_handoff_actions(**kw)
                        )

    def test_unknown_prior_runtime_is_refused(self) -> None:
        from tools.codex_telegram_actions import receiver_handoff_actions

        with self.assertRaises(ValueError):
            receiver_handoff_actions(
                claude_receiver_topology="runtime-owned", direction="claude-to-codex", phase="pre",
                prior_runtime="dead",  # type: ignore[arg-type]
            )

    def test_prior_runtime_threads_through_integration_and_controller(self) -> None:
        binding = SimpleNamespace(persona="recovery", communications=_telegram_comms(), launch=_launch("codex"))
        self.assertIn("probe-claude-present", rc.receiver_handoff_actions(binding, "claude-to-codex", "pre"))
        self.assertNotIn(
            "probe-claude-present",
            rc.receiver_handoff_actions(binding, "claude-to-codex", "pre", prior_runtime="unproven"),
        )
        backend = _ReceiverBackend()
        receipt = rc._run_integration_handoff(
            binding, "claude-to-codex", "pre", backend=backend, reason="r", prior_runtime="unproven"
        )
        self.assertNotIn("probe-claude-present", receipt.operations)
        self.assertIn("stop-dormant-claude-receiver", receipt.operations)

    def test_production_subprocess_argv_carries_the_flag_only_when_unproven(self) -> None:
        binding = SimpleNamespace(persona="recovery", communications=_telegram_comms(), launch=_launch("codex"))
        ok = mock.Mock(returncode=0, stdout=json.dumps({"ok": True, "operations": []}), stderr="")
        with mock.patch.object(harness_integration.subprocess, "run", return_value=ok) as run:
            rc._run_integration_handoff(binding, "claude-to-codex", "pre", backend=None, reason="r")
            rc._run_integration_handoff(
                binding, "claude-to-codex", "pre", backend=None, reason="r", prior_runtime="unproven"
            )
        default_argv, unproven_argv = (call.args[0] for call in run.call_args_list)
        self.assertNotIn("--prior-runtime", default_argv)
        self.assertEqual(unproven_argv[unproven_argv.index("--prior-runtime") + 1], "unproven")
        with mock.patch.object(rc.subprocess, "run", return_value=ok) as run:
            rc._run_bridge_handoff_subprocess(binding, "claude-to-codex", "pre", reason="r", timeout_seconds=90)
            rc._run_bridge_handoff_subprocess(
                binding, "claude-to-codex", "pre", reason="r", timeout_seconds=90, prior_runtime="unproven"
            )
        default_argv, unproven_argv = (call.args[0] for call in run.call_args_list)
        self.assertNotIn("--prior-runtime", default_argv)
        self.assertEqual(unproven_argv[unproven_argv.index("--prior-runtime") + 1], "unproven")


class ControllerStartCodexOverClaudeReceiverTests(unittest.TestCase):
    """T-4184 (option (a)): when the controller launches codex for a comms
    persona it runs the claude-to-codex PRE (quiesce: snapshot + disable the
    claude watchdog, prior_runtime=unproven) before the launch and the POST
    (claude absent, start + prove the codex poller) after the post_start
    assessment. Both fail-open with cooldown-gated alerts. Every tmux/pane seam
    is mocked; the receiver backend is a stateful fake, never the bridge."""

    PRE_OPS = [
        "snapshot-watchdog-units", "disable-watchdog-timer", "stop-watchdog-service",
        "snapshot-dormant-claude-unit", "disable-dormant-claude-receiver", "stop-dormant-claude-receiver",
    ]
    POST_OPS = ["probe-claude-absent", "start-codex-poller", "probe-codex-present", "health-codex-no-409"]

    def setUp(self) -> None:
        patcher = mock.patch.object(rc, "_route_failure_alert")
        self.route = patcher.start()
        self.addCleanup(patcher.stop)

    def _patches(self, binding, **kw):
        return ControllerStartRuntimePostStartTests._start_patches(self, binding, **kw)

    def _record(self, root: Path, harness: str) -> None:
        ControllerStartPriorReceiverTests._record(self, root, harness)

    def _events(self, root: Path) -> list[dict[str, object]]:
        path = rc._evidence_log_path("recovery", root)
        if not path.exists():
            return []
        return [
            r for r in _read_jsonl(path)
            if str(r["event"]).startswith(("controller_start_prior_receiver", "controller_start_selected_receiver"))
        ]

    def _codex(self, root: Path, *, comms: bool = True):
        return _binding(
            root, harness="codex", profile="interactive-chatgpt",
            communications=_telegram_comms() if comms else None,
        )

    def _claude(self, root: Path):
        return ControllerStartPriorReceiverTests._claude(self, root)

    def _start(self, binding, root: Path, backend, *, order: list[str] | None = None, on_start=None, **patch_kw):
        stack, start, _capture = self._patches(binding, **patch_kw)
        if order is not None:
            original_execute = backend.execute
            backend.execute = lambda action, b: (order.append(action), original_execute(action, b))[1]

        def fake_start(*a, **k):
            if order is not None:
                order.append("start")
            if on_start is not None:
                on_start()
            return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)

        start.side_effect = fake_start
        post = mock.patch.object(
            rc, "_run_and_assess_post_start",
            side_effect=lambda *a, **k: order.append("post_start") if order is not None else None,
        ) if order is not None else contextlib.nullcontext()
        with stack, post:
            result = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
        return result, start

    # -- start path -----------------------------------------------------------

    def test_codex_over_claude_record_quiesces_before_launch_and_starts_poller_after_post_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend(watchdog=True)
            order: list[str] = []
            (_rs, handle), start = self._start(self._codex(root), root, backend, order=order)

            self.assertEqual(order, self.PRE_OPS + ["start", "post_start"] + self.POST_OPS)
            self.assertNotIn("probe-claude-present", backend.events)  # prior_runtime=unproven
            self.assertEqual(backend.pending_probes, 0)  # claude evidence: no pending probe needed
            self.assertFalse(backend.watchdog)
            self.assertEqual(backend.snapshot, {"watchdog": True, "complete": False})
            self.assertTrue(backend.poller)
            self.assertEqual(handle.pid, 7777)
            quiesced, started = self._events(root)
            self.assertEqual(quiesced["event"], "controller_start_prior_receiver_quiesced")
            self.assertEqual(quiesced["direction"], "claude-to-codex")
            self.assertEqual(quiesced["prior_evidence"], "record")
            self.assertEqual(quiesced["prior_runtime"], "unproven")
            self.assertEqual(started["event"], "controller_start_selected_receiver_started")
            self.assertEqual(started["operations"], ",".join(self.POST_OPS))
            self.route.assert_not_called()

    def test_codex_over_live_claude_pane_quiesces_via_live_pane_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # no record: the live foreign pane is the only evidence
            backend = _CodexSeatBackend(watchdog=True, claude_present=True)
            (_rs, _handle), start = self._start(
                self._codex(root), root, backend,
                on_start=lambda: setattr(backend, "claude_present", False),  # the respawn ends claude
                alive=True, pids=(5555, 7777), cmdline=["/usr/bin/claude", "--foo"],
            )
            self.assertEqual(start.call_args.kwargs["replace"], True)
            self.assertEqual(backend.events, self.PRE_OPS + self.POST_OPS)
            self.assertEqual(backend.pending_probes, 0)
            self.assertEqual(self._events(root)[0]["prior_evidence"], "live_pane")
            self.assertTrue(backend.poller)

    def test_no_post_when_the_post_start_cap_assessment_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend()
            stack, start, _capture = self._patches(self._codex(root))
            cap = rc.RuntimeOperationalFailure(
                "capped", failure_class="subscription_cap", cause="cap", binding_runtime="codex/interactive-chatgpt"
            )
            with stack, mock.patch.object(rc, "_run_and_assess_post_start", side_effect=cap), \
                    self.assertRaises(rc.RuntimeOperationalFailure):
                rc._controller_start_runtime(self._codex(root), state_root=root, receiver_backend=backend)
            self.assertEqual(backend.events, self.PRE_OPS)
            self.assertFalse(backend.poller)
            self.assertNotIn(
                "controller_start_selected_receiver_started", [e["event"] for e in self._events(root)]
            )

    def test_codex_over_codex_with_pending_snapshot_skips_pre_but_runs_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            backend = _CodexSeatBackend(watchdog=False, snapshot={"watchdog": True, "complete": False})
            self._start(self._codex(root), root, backend)
            self.assertEqual(backend.pending_probes, 1)
            self.assertEqual(backend.events, self.POST_OPS)
            self.assertEqual(backend.snapshot, {"watchdog": True, "complete": False})
            skipped, started = self._events(root)
            self.assertEqual(skipped["event"], "controller_start_prior_receiver_quiesce_skipped")
            self.assertEqual(skipped["reason"], "snapshot-pending")
            self.assertEqual(started["event"], "controller_start_selected_receiver_started")

    def test_codex_over_codex_without_snapshot_runs_pre(self) -> None:
        """E.g. an initial consume-path codex bootstrap: it ran only the POST,
        so the claude watchdog was never quiesced and no snapshot exists."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            for snapshot in (None, {"watchdog": True, "complete": True}):
                with self.subTest(snapshot=snapshot):
                    backend = _CodexSeatBackend(watchdog=True, snapshot=snapshot)
                    self._start(self._codex(root), root, backend)
                    self.assertEqual(backend.pending_probes, 1)
                    self.assertEqual(backend.events, self.PRE_OPS + self.POST_OPS)
                    self.assertEqual(backend.snapshot, {"watchdog": True, "complete": False})
                    self.assertFalse(backend.watchdog)

    def test_pending_probe_error_runs_pre_anyway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            backend = _CodexSeatBackend(pending_error=RuntimeError("snapshot unreadable"))
            self._start(self._codex(root), root, backend)
            self.assertEqual(backend.events, self.PRE_OPS + self.POST_OPS)
            quiesced = self._events(root)[0]
            self.assertEqual(quiesced["event"], "controller_start_prior_receiver_quiesced")
            self.assertIn("probe-failed", str(quiesced["snapshot_pending"]))

    def test_pre_failure_is_fail_open_with_alert_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend(watchdog=True, fail=("stop-watchdog-service",))
            (_rs, handle), start = self._start(self._codex(root), root, backend)
            start.assert_called_once()  # launched anyway
            self.assertEqual(handle.pid, 7777)
            # the KEEP handoff restored what it had snapshotted, then the POST ran
            self.assertTrue(backend.watchdog)
            self.assertEqual(backend.snapshot, {"watchdog": True, "complete": True})
            self.assertTrue(backend.poller)
            failed = self._events(root)[0]
            self.assertEqual(failed["event"], "controller_start_prior_receiver_quiesce_failed")
            self.assertIn("stop-watchdog-service", str(failed["error"]))
            self.assertIs(failed["alert_routed"], True)
            self.route.assert_called_once()
            agent, msg, vocab = self.route.call_args.args
            self.assertEqual(agent, "recovery")
            self.assertIn("launch proceeds", msg)
            self.assertEqual(vocab, "controller-prior-receiver-quiesce (recovery)")
            self.assertTrue((rc._persona_state("recovery", root) / "prior-receiver-quiesce-alert.json").exists())

            # A second failing start inside the window routes no second page.
            self._start(self._codex(root), root, _CodexSeatBackend(fail=("disable-watchdog-timer",)))
            self.route.assert_called_once()
            self.assertEqual(
                [e["alert_suppressed"] for e in self._events(root) if e["event"].endswith("quiesce_failed")],
                ["", "cooldown"],
            )

    def test_post_failure_returns_the_handle_and_alerts_telegram_dark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend(fail=("start-codex-poller",))
            (_rs, handle), start = self._start(self._codex(root), root, backend)
            start.assert_called_once()
            self.assertEqual(handle.pid, 7777)
            self.assertFalse(backend.poller)
            failed = self._events(root)[-1]
            self.assertEqual(failed["event"], "controller_start_selected_receiver_start_failed")
            self.assertIn("start-codex-poller", str(failed["error"]))
            self.route.assert_called_once()
            agent, msg, vocab = self.route.call_args.args
            self.assertEqual(agent, "recovery")
            self.assertIn("codex up, Telegram DARK", msg)
            self.assertEqual(vocab, "controller-selected-receiver (recovery)")
            self.assertTrue((rc._persona_state("recovery", root) / "prior-receiver-poller-alert.json").exists())

    def test_post_409_health_failure_is_worded_as_a_conflict_not_dark(self) -> None:
        from tools.codex_telegram_actions import HEALTH_409_ERROR_PREFIX

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend()
            original_execute = backend.execute

            def execute(action, b):
                if action == "health-codex-no-409":
                    raise RuntimeError(f"BridgeError: {HEALTH_409_ERROR_PREFIX}: 2 getUpdates 409 Conflict lines")
                return original_execute(action, b)

            backend.execute = execute
            (_rs, handle), _start = self._start(self._codex(root), root, backend)
            self.assertEqual(handle.pid, 7777)
            self.assertTrue(backend.poller)
            self.route.assert_called_once()
            msg = self.route.call_args.args[1]
            self.assertIn("codex poller running but 409 conflicts detected", msg)
            self.assertNotIn("DARK", msg)

    def test_unwritable_evidence_and_stamps_never_raise_out_of_a_live_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            for fail in ((), ("stop-watchdog-service", "start-codex-poller")):
                with self.subTest(fail=fail), mock.patch.object(
                    rc, "_append_controller_evidence", side_effect=OSError("ro")
                ), mock.patch.object(rc, "_atomic_json", side_effect=OSError("ro")):
                    (_rs, handle), _start = self._start(self._codex(root), root, _CodexSeatBackend(fail=fail))
                self.assertEqual(handle.pid, 7777)

    def test_non_comms_codex_and_claude_selection_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend()
            with mock.patch.object(rc, "_run_integration_handoff") as handoff, \
                    mock.patch.object(rc, "_run_bridge_handoff_subprocess") as bridge:
                self._start(self._codex(root, comms=False), root, backend)
            handoff.assert_not_called()
            bridge.assert_not_called()
            self.assertEqual(backend.events, [])
            self.assertEqual(self._events(root), [])

            # A claude selection never reaches the T-4184 steps (T-4158/T-4178 own it).
            self._record(root, "codex")
            claude_backend = _CodexSeatBackend(poller=True, claude_present=False)
            with mock.patch.object(rc, "_quiesce_prior_harness_receiver_before_start", wraps=rc._quiesce_prior_harness_receiver_before_start) as q, \
                    mock.patch.object(rc, "_start_selected_receiver_after_start", wraps=rc._start_selected_receiver_after_start) as p:
                self._start(self._claude(root), root, claude_backend)
            self.assertEqual(claude_backend.events[:2], ["stop-codex-poller", "probe-codex-absent"])
            self.assertNotIn("start-codex-poller", claude_backend.events)
            self.assertNotIn("snapshot-watchdog-units", claude_backend.events)
            self.assertFalse([e for e in self._events(root) if "quiesce" in e["event"] or "selected_receiver" in e["event"]])
            q.assert_called_once()
            p.assert_called_once()

    def test_ambiguous_in_runtime_harness_is_a_logged_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            backend = _CodexSeatBackend()
            with mock.patch.object(
                harness_integration, "configured_harness_names", return_value=frozenset({"claude", "codex", "other"})
            ):
                self._start(self._codex(root), root, backend)
            self.assertEqual(backend.events, [])
            skipped = [e["event"] for e in self._events(root)]
            self.assertEqual(
                skipped,
                ["controller_start_prior_receiver_quiesce_skipped", "controller_start_selected_receiver_skipped"],
            )

    def test_production_path_pre_passes_unproven_and_post_uses_inner_outer_timeouts(self) -> None:
        """backend None: PRE through the integration's bridge subprocess with
        --prior-runtime unproven; POST through `_run_bridge_handoff_subprocess`
        with the 60s inner / 90s outer deadlines. Both subprocess.run seams are
        mocked, so no real bridge runs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "claude")
            ok = mock.Mock(returncode=0, stdout=json.dumps({"ok": True, "operations": ["x"]}), stderr="")
            stack, start, _capture = self._patches(self._codex(root))
            # (rc.subprocess and harness_integration.subprocess are one module)
            with stack, mock.patch.object(rc.subprocess, "run", return_value=ok) as run:
                rc._controller_start_runtime(self._codex(root), state_root=root)
            pre_call, post_call = [c for c in run.call_args_list if "handoff" in c.args[0]]
            pre_argv = pre_call.args[0]
            self.assertEqual(pre_argv[pre_argv.index("--phase") + 1], "pre")
            self.assertEqual(pre_argv[pre_argv.index("--direction") + 1], "claude-to-codex")
            self.assertEqual(pre_argv[pre_argv.index("--prior-runtime") + 1], "unproven")
            post_argv = post_call.args[0]
            self.assertEqual(post_argv[post_argv.index("--phase") + 1], "post")
            self.assertNotIn("--prior-runtime", post_argv)
            self.assertEqual(int(post_argv[post_argv.index("--timeout-seconds") + 1]), rc.PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS)
            self.assertEqual(post_call.kwargs["timeout"], rc.PRIOR_RECEIVER_POST_OUTER_TIMEOUT_SECONDS)

    # -- lifecycle across swaps -------------------------------------------------

    def test_round_trip_claude_codex_claude_restores_watchdog_and_completes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seat = _CodexSeatBackend(watchdog=True, claude_present=False)  # claude capped/dead
            self._record(root, "claude")
            self._start(self._codex(root), root, seat)
            self.assertEqual((seat.watchdog, seat.poller), (False, True))
            self._record(root, "codex")  # controller_main persists the codex record

            # claude's cooldown expires: T-4158 PRE stops the poller, T-4178 POST restores.
            self._start(self._claude(root), root, seat, on_start=lambda: setattr(seat, "claude_present", True))
            self.assertTrue(seat.watchdog)
            self.assertFalse(seat.poller)
            self.assertEqual(seat.snapshot, {"watchdog": True, "complete": True})
            self.assertIn("controller_start_prior_receiver_restored", [e["event"] for e in self._events(root)])

    def test_repeated_swaps_with_a_middle_post_failure_still_restore_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seat = _CodexSeatBackend(watchdog=True)
            self._record(root, "claude")
            self._start(self._codex(root), root, seat)  # snapshot {enabled}, watchdog off
            self._record(root, "codex")

            seat.fail = {"restore-watchdog-units"}  # the codex-to-claude POST fails
            self._start(self._claude(root), root, seat, on_start=lambda: setattr(seat, "claude_present", True))
            self.assertFalse(seat.watchdog)
            self.assertEqual(seat.snapshot, {"watchdog": True, "complete": False})
            self._record(root, "claude")

            seat.fail = set()
            seat.claude_present = False  # claude caps; codex again, over a claude record + pending snapshot
            self._start(self._codex(root), root, seat)
            self.assertEqual(seat.snapshot, {"watchdog": True, "complete": False})  # first-wins
            self._record(root, "codex")

            self._start(self._claude(root), root, seat, on_start=lambda: setattr(seat, "claude_present", True))
            self.assertTrue(seat.watchdog)  # the ORIGINAL enabled state, not the disabled one
            self.assertEqual(seat.snapshot, {"watchdog": True, "complete": True})

    def test_cap_swap_back_after_t4158_stop_restarts_the_poller(self) -> None:
        """The dark-seat bug: codex seat (poller up) -> controller tries claude
        (T-4158 stops the poller) -> claude caps at post_start -> the controller
        reselects codex. Before T-4184 that codex seat had ZERO pollers."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seat = _CodexSeatBackend(watchdog=False, poller=True, snapshot={"watchdog": True, "complete": False})
            self._record(root, "codex")
            stack, _start, _capture = self._patches(self._claude(root))
            cap = rc.RuntimeOperationalFailure(
                "capped", failure_class="subscription_cap", cause="cap", binding_runtime="claude/anthropic-code"
            )
            with stack, mock.patch.object(rc, "_run_and_assess_post_start", side_effect=cap), \
                    self.assertRaises(rc.RuntimeOperationalFailure):
                rc._controller_start_runtime(self._claude(root), state_root=root, receiver_backend=seat)
            self.assertFalse(seat.poller)  # T-4158 stopped it; T-4178's POST never ran

            self._start(self._codex(root), root, seat)
            self.assertTrue(seat.poller)
            self.assertFalse(seat.watchdog)  # still quiesced; snapshot untouched
            self.assertEqual(seat.snapshot, {"watchdog": True, "complete": False})

    # -- attach branch ------------------------------------------------------------

    def _attach(self, root: Path, backend):
        binding = self._codex(root)
        stack, start, _capture = self._patches(binding, alive=True, pids=(4242,), cmdline=["/usr/bin/codex"])
        with stack:
            _rs, handle = rc._controller_start_runtime(binding, state_root=root, receiver_backend=backend)
        start.assert_not_called()  # attach, never a respawn
        self.assertEqual(handle.pid, 4242)
        return handle

    def test_attach_heal_healthy_seat_is_probe_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            seat = _CodexSeatBackend(watchdog=False, poller=True, snapshot={"watchdog": True, "complete": False})
            self._attach(root, seat)
            self.assertEqual(seat.events, ["proof:codex:present"])
            self.assertEqual(seat.pending_probes, 1)

    def test_attach_heal_quiesces_and_starts_a_missing_poller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            seat = _CodexSeatBackend(watchdog=True, poller=False)  # no snapshot, poller down
            self._attach(root, seat)
            self.assertEqual(seat.events, self.PRE_OPS + ["proof:codex:present"] + self.POST_OPS)
            self.assertEqual((seat.watchdog, seat.poller), (False, True))
            events = self._events(root)
            self.assertEqual({e["path"] for e in events}, {"attach"})

    def test_attach_heal_failure_alerts_but_still_attaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._record(root, "codex")
            seat = _CodexSeatBackend(
                watchdog=False, poller=False, snapshot={"watchdog": True, "complete": False},
                fail=("start-codex-poller",),
            )
            self._attach(root, seat)  # no raise
            failed = self._events(root)[-1]
            self.assertEqual(failed["event"], "controller_start_selected_receiver_start_failed")
            self.assertEqual(failed["path"], "attach")
            self.route.assert_called_once()
            self.assertIn("Telegram DARK", self.route.call_args.args[1])

    def test_claude_attach_runs_no_codex_heal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = self._claude(root)
            self._record(root, "claude")
            seat = _CodexSeatBackend(claude_present=True)
            stack, _start, _capture = self._patches(binding, alive=True, pids=(4242,), cmdline=["/usr/bin/claude"])
            with stack:
                rc._controller_start_runtime(binding, state_root=root, receiver_backend=seat)
            self.assertEqual(seat.events, ["proof:codex:absent"])  # T-4179's probe only
            self.assertEqual(seat.pending_probes, 0)


class MidSessionCapStateIOTests(unittest.TestCase):
    """T-4037: mid-session-cap detector state file roundtrip."""

    def test_read_absent_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rc.read_mid_session_cap_state("orchestrator", state_root=Path(tmp)), {})

    def test_write_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            observation = rc.MidSessionCapObservation(
                modal_present=True, modal_kind="session-limit", detected_at=now,
                observed_at=now, reset_at=now + timedelta(hours=2), reset_source="parsed",
            )
            rc._write_mid_session_cap_state("orchestrator", binding, observation, state_root=root)
            state = rc.read_mid_session_cap_state("orchestrator", state_root=root)
            self.assertEqual(state["schema"], rc.MID_SESSION_CAP_SCHEMA)
            self.assertEqual(state["runtime"], "claude/anthropic-code")
            self.assertTrue(state["modal_present"])
            self.assertEqual(state["modal_kind"], "session-limit")
            self.assertEqual(state["detected_at"], rc._iso(now))
            self.assertEqual(state["observed_at"], rc._iso(now))
            self.assertEqual(state["reset_at"], rc._iso(now + timedelta(hours=2)))
            self.assertEqual(state["reset_source"], "parsed")

    def test_clear_removes_file_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            observation = rc.MidSessionCapObservation(
                modal_present=False, modal_kind=None, detected_at=None,
                observed_at=now, reset_at=None, reset_source=None,
            )
            rc._write_mid_session_cap_state("orchestrator", binding, observation, state_root=root)
            rc.clear_mid_session_cap_state("orchestrator", state_root=root)
            self.assertEqual(rc.read_mid_session_cap_state("orchestrator", state_root=root), {})
            rc.clear_mid_session_cap_state("orchestrator", state_root=root)  # must not raise


class TerminateRuntimePidTests(unittest.TestCase):
    """T-4037: the SIGTERM-only swap-action primitive."""

    def test_terminate_sends_sigterm_when_pid_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                 mock.patch.object(rc.os, "kill") as mkill:
                self.assertTrue(rc._terminate_runtime_pid(binding))
                mkill.assert_called_once_with(4242, rc.signal.SIGTERM)

    def test_terminate_returns_false_when_no_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_pane_process_pid", return_value=None), \
                 mock.patch.object(rc.os, "kill") as mkill:
                self.assertFalse(rc._terminate_runtime_pid(binding))
                mkill.assert_not_called()

    def test_terminate_returns_false_on_process_lookup_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            with mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                 mock.patch.object(rc.os, "kill", side_effect=ProcessLookupError):
                self.assertFalse(rc._terminate_runtime_pid(binding))


class MidSessionCapSwapEligibleTests(unittest.TestCase):
    """T-4037: the swap-action gate -- flag, comms-exclusion, fallback viability."""

    def test_false_when_flag_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            self.assertFalse(
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_false_when_comms_carrying_and_receiver_not_ready(self) -> None:
        # T-5451: the blanket comms exclusion became a receiver-readiness gate
        # (ready-path and alert behaviour: tools/test_t5451_flip_policy.py).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code",
                communications=_telegram_comms(), mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            with mock.patch.object(rc, "_comms_receiver_ready", return_value=(False, "x")), \
                 mock.patch.object(rc, "_route_failure_alert"):
                self.assertFalse(
                    rc._mid_session_cap_swap_eligible(
                        claude, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                    )
                )

    def test_false_when_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            self.assertFalse(
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=None, holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_false_when_only_self_in_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            self.assertFalse(
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude], holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_false_when_only_fallback_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            holds = {"codex/interactive-chatgpt": {"reason": "x"}}
            self.assertFalse(
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude, codex], holds=holds, now=rc._utc_now(), state_root=root
                )
            )

    def test_true_when_flag_on_not_comms_and_fallback_viable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            self.assertTrue(
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                )
            )


class FallbackReturnEligibleTests(unittest.TestCase):
    """T-4046: the failback-trigger predicate -- flag, priority-order, and
    fallback (higher-priority) viability. Deliberately does NOT test a
    comms-exclusion -- there is none by design (see
    _fallback_return_eligible's own docstring)."""

    def test_false_when_flag_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            self.assertIsNone(
                rc._fallback_return_eligible(
                    codex, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_none_when_already_first_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", fallback_return=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            self.assertIsNone(
                rc._fallback_return_eligible(
                    claude, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_none_when_binding_not_in_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            other = _stateless_binding(
                root, harness="codex", profile="other-profile", fallback_return=True,
            )
            self.assertIsNone(
                rc._fallback_return_eligible(
                    other, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_none_when_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            self.assertIsNone(
                rc._fallback_return_eligible(
                    codex, candidates=None, holds={}, now=rc._utc_now(), state_root=root
                )
            )

    def test_true_when_higher_priority_viable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            result = rc._fallback_return_eligible(
                codex, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
            )
            self.assertIs(result, claude)

    def test_false_when_higher_priority_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            holds = {"claude/anthropic-code": {"reason": "x"}}
            self.assertIsNone(
                rc._fallback_return_eligible(
                    codex, candidates=[claude, codex], holds=holds, now=rc._utc_now(), state_root=root
                )
            )

    def test_false_while_higher_priority_in_cooldown_true_after_it_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            cooldown_until = now + timedelta(hours=2)
            rc.record_last_attempt(
                "recovery", claude, outcome="failed", failure_class="subscription_cap",
                state_root=root, now=now, cooldown_until_override=cooldown_until,
            )
            self.assertIsNone(
                rc._fallback_return_eligible(
                    codex, candidates=[claude, codex], holds={}, now=now, state_root=root
                )
            )
            self.assertIsNone(
                rc._fallback_return_eligible(
                    codex, candidates=[claude, codex], holds={},
                    now=cooldown_until - timedelta(seconds=1), state_root=root,
                )
            )
            result = rc._fallback_return_eligible(
                codex, candidates=[claude, codex], holds={},
                now=cooldown_until + timedelta(seconds=1), state_root=root,
            )
            self.assertIs(result, claude)

    def test_multi_hop_chain_picks_first_viable_higher_priority_not_just_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            gpt = _stateless_binding(root, harness="codex", profile="gpt-mid")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            holds = {"claude/anthropic-code": {"reason": "x"}}
            result = rc._fallback_return_eligible(
                codex, candidates=[claude, gpt, codex], holds=holds, now=rc._utc_now(), state_root=root
            )
            self.assertIs(result, gpt)


class ObserveAndPublishMidSessionCapTests(unittest.TestCase):
    """T-4037: single-cycle detect+publish, fail-open, sticky detected_at,
    and (REBOUND round 2 Finding 3) the two-sample debounce that gates the
    transition into modal_present=True."""

    def _session_cap_text(self) -> str:
        return (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt"
        ).read_text(encoding="utf-8")

    def _spend_limit_text(self) -> str:
        return (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_spend_limit_modal_pane.txt"
        ).read_text(encoding="utf-8")

    def test_unreadable_pane_raising_returns_none_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")

            def capture_pane_fn() -> str:
                raise RuntimeError("tmux hiccup")

            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=capture_pane_fn, now=rc._utc_now(), state_root=root,
            )
            self.assertIsNone(result)
            self.assertEqual(rc.read_mid_session_cap_state("recovery", state_root=root), {})

    def test_empty_pane_text_returns_none_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: "", now=rc._utc_now(), state_root=root,
            )
            self.assertIsNone(result)
            self.assertEqual(rc.read_mid_session_cap_state("recovery", state_root=root), {})

    def test_non_claude_harness_never_classifies_a_modal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(),
                now=rc._utc_now(), state_root=root,
            )
            self.assertIsNotNone(result)
            self.assertFalse(result.modal_present)

    # -- T-4037 REBOUND round 2 Finding 3: two-sample debounce ---------------

    def test_single_classifying_sample_does_not_set_modal_present(self) -> None:
        """A lone classifying sample is only a CANDIDATE -- modal_present
        must stay False, detected_at must stay None, and the diagnostic
        candidate_modal_kind field (never actionable) reflects the pending
        classification."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=now, state_root=root,
            )
            self.assertIsNotNone(result)
            self.assertFalse(result.modal_present)
            self.assertIsNone(result.modal_kind)
            self.assertIsNone(result.detected_at)
            self.assertEqual(result.candidate_modal_kind, "session-limit")
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])
            self.assertEqual(state["candidate_modal_kind"], "session-limit")
            self.assertTrue(state["candidate_text_sha256"])

    def test_two_identical_consecutive_samples_confirm_modal_present(self) -> None:
        """Second CONSECUTIVE, render-identical sample confirms -- and
        detected_at anchors to the FIRST sighting (the candidate's own
        timestamp), not the confirming sample's timestamp."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            first = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            second = first + timedelta(seconds=60)
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=first, state_root=root,
            )
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=second, state_root=root,
            )
            self.assertTrue(result.modal_present)
            self.assertEqual(result.modal_kind, "session-limit")
            self.assertEqual(result.detected_at, first)
            self.assertEqual(result.observed_at, second)
            self.assertIsNone(result.candidate_modal_kind)
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertTrue(state["modal_present"])
            self.assertIsNone(state["candidate_text_sha256"])

    def test_two_classifying_but_different_samples_do_not_confirm(self) -> None:
        """A pane that CHANGES between samples resets the candidate rather
        than accumulating toward confirmation with mismatched evidence --
        the second (different) sample becomes the NEW pending candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            first = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            second = first + timedelta(seconds=60)
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=first, state_root=root,
            )
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._spend_limit_text(), now=second, state_root=root,
            )
            self.assertFalse(result.modal_present)
            self.assertEqual(result.candidate_modal_kind, "monthly-spend-limit")
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])

    def test_classify_then_non_classify_clears_pending_candidate(self) -> None:
        """A non-classifying sample clears any pending candidate -- a later
        reclassification is a FRESH first sighting, not a confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            first = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            second = first + timedelta(seconds=60)
            third = first + timedelta(seconds=120)
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=first, state_root=root,
            )
            cleared = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: "idle prompt, no modal", now=second, state_root=root,
            )
            self.assertIsNone(cleared.candidate_modal_kind)
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=third, state_root=root,
            )
            self.assertFalse(result.modal_present)
            self.assertEqual(result.candidate_modal_kind, "session-limit")
            self.assertIsNone(result.detected_at)

    def test_detected_at_is_sticky_across_consecutive_present_observations(self) -> None:
        """Confirms on the second identical sample, then stays present +
        anchored to the ORIGINAL first sighting on a later, already-confirmed
        poll -- debounce gates only the entry transition, not continued
        observation of an already-confirmed episode."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            first = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            second = first + timedelta(minutes=5)
            third = first + timedelta(minutes=10)
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=first, state_root=root,
            )
            confirmed = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=second, state_root=root,
            )
            self.assertTrue(confirmed.modal_present)
            self.assertEqual(confirmed.detected_at, first)
            self.assertEqual(confirmed.observed_at, second)
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=third, state_root=root,
            )
            self.assertTrue(result.modal_present)
            self.assertEqual(result.detected_at, first)
            self.assertEqual(result.observed_at, third)

    def test_detected_at_resets_after_modal_clears_and_reappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            first = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            confirmed_first = first + timedelta(seconds=60)
            cleared = first + timedelta(minutes=5)
            reappeared = first + timedelta(minutes=10)
            confirmed_reappeared = reappeared + timedelta(seconds=60)
            # Establish + confirm an initial episode.
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=first, state_root=root,
            )
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(),
                now=confirmed_first, state_root=root,
            )
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: "idle prompt, no modal", now=cleared, state_root=root,
            )
            # Reappearance needs its OWN fresh two-sample confirmation.
            rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(), now=reappeared, state_root=root,
            )
            result = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: self._session_cap_text(),
                now=confirmed_reappeared, state_root=root,
            )
            self.assertTrue(result.modal_present)
            self.assertEqual(result.detected_at, reappeared)


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

    def test_no_capture_pane_fn_means_detection_entirely_skipped(self) -> None:
        """No capture_pane_fn (the two tests above's shape) -- detection never
        runs, and the function still returns None -- backward compatible."""
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp))
            result = rc.block_until_tmux_death(
                binding, alive_check=lambda: False, sleep=lambda s: None,
            )
            self.assertIsNone(result)


class BlockUntilTmuxDeathMidSessionCapTests(unittest.TestCase):
    """T-4037: pane-text cap detection, swap gating, and the amendment-A
    bounded confirm-death wait, all exercised through block_until_tmux_death's
    injectable seams (alive_check, sleep, now, capture_pane_fn,
    terminate_runtime_fn) -- no real tmux process is ever touched."""

    def _session_cap_text(self) -> str:
        return (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt"
        ).read_text(encoding="utf-8")

    def _spend_limit_text(self) -> str:
        return (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_spend_limit_modal_pane.txt"
        ).read_text(encoding="utf-8")

    def _clock(self, start: datetime) -> tuple[Callable[[], datetime], Callable[[float], None]]:
        state = {"t": start}

        def now() -> datetime:
            return state["t"]

        def sleep(seconds: float) -> None:
            state["t"] = state["t"] + timedelta(seconds=seconds)

        return now, sleep

    def test_cap_check_runs_only_at_cadence_not_every_liveness_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 20  # 20 * 5s poll = 100s total elapsed

            capture_calls: list[datetime] = []

            def capture_pane_fn() -> str:
                capture_calls.append(now())
                return "idle prompt, no modal"

            rc.block_until_tmux_death(
                binding, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=capture_pane_fn, cap_check_interval_seconds=60.0,
                now=now, state_root=root,
            )
            # 100s elapsed at 60s cadence -> exactly one cap-check (at the 60s mark).
            self.assertEqual(len(capture_calls), 1)

    def test_capture_pane_raising_is_fail_open_no_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))

            def capture_pane_fn() -> str:
                raise RuntimeError("tmux hiccup")

            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 1

            result = rc.block_until_tmux_death(
                binding, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=capture_pane_fn, cap_check_interval_seconds=0.0,
                now=now, state_root=root,
            )
            self.assertIsNone(result)
            self.assertEqual(rc.read_mid_session_cap_state("recovery", state_root=root), {})

    # -- T-4037 REBOUND round 2 Finding 3: debounce, exercised at the ---------
    # -- block_until_tmux_death integration level ----------------------------

    def test_single_cap_check_cycle_never_terminates_even_when_swap_eligible(self) -> None:
        """ONE classifying cap-check cycle is only a candidate --
        block_until_tmux_death must not terminate (or return a swap outcome)
        off a single sample, even when the swap flag is on, the persona is
        not comms-carrying, and a fallback is viable."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 1  # exactly ONE cap-check cycle

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: self._session_cap_text(), cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])
            self.assertEqual(state["candidate_modal_kind"], "session-limit")

    def test_shared_flag_on_codex_binding_never_swaps_even_with_cap_shaped_pane_text(self) -> None:
        """T-4047 (item 6): mid_session_cap_swap_enabled resolves per ROLE
        (persona_bringup.load_binding -> resolve_controller_config), so a
        persona's claude and codex bindings share the SAME flag value (see
        RuntimeControllerConfig's field docstring). Proves the sharing is
        harmless for the non-claude binding: even with the flag on, a
        viable fallback, and pane text that WOULD classify for claude,
        across enough cap-check cycles to clear the two-sample debounce,
        the harness gate inside _observe_and_publish_mid_session_cap
        (classify only when binding.launch.harness == "claude") keeps
        modal_present permanently False for codex -- so the swap branch
        never fires, structurally, not because the flag happens to be off."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", mid_session_cap_swap=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 2  # two full cap-check cycles -- enough to
                # clear the two-sample debounce if this could ever classify

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep, poll_interval_seconds=60.0,
                capture_pane_fn=lambda: self._session_cap_text(), cap_check_interval_seconds=60.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])

    def test_two_different_classifying_samples_never_confirm_or_terminate(self) -> None:
        """Two classifying but DIFFERENT samples (a pane that changes between
        polls) must never confirm modal_present or trigger a terminate --
        the second sample resets the candidate instead of confirming it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}
            texts = [self._session_cap_text(), self._spend_limit_text()]
            capture_calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 2

            def capture_pane_fn() -> str:
                text = texts[min(capture_calls["n"], len(texts) - 1)]
                capture_calls["n"] += 1
                return text

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=capture_pane_fn, cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])

    def test_modal_detected_but_swap_not_eligible_publishes_only_never_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # mid_session_cap_swap defaults False -- not eligible.
            binding = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 2

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                binding, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: self._session_cap_text(), cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[binding], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertTrue(state["modal_present"])

    def test_swap_confirmed_dead_returns_outcome_with_parsed_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            alive_state = {"alive": True}

            def alive_check() -> bool:
                return alive_state["alive"]

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)
                alive_state["alive"] = False  # simulate a successful SIGTERM

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: self._session_cap_text(), cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsInstance(result, rc.MidSessionCapSwapOutcome)
            self.assertEqual(result.modal_kind, "session-limit")
            self.assertEqual(result.cooldown_source, "parsed")
            self.assertEqual(result.cooldown_until, datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc))
            self.assertEqual(len(terminate_calls), 1)

    def test_swap_confirmed_dead_uses_conservative_default_when_unparseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            start = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            now, sleep = self._clock(start)
            alive_state = {"alive": True}

            def alive_check() -> bool:
                return alive_state["alive"]

            def terminate_runtime_fn(b, **kwargs) -> None:
                alive_state["alive"] = False

            # T-4037 REBOUND round 1 Finding 1: classification requires modal
            # CHROME, not just framing phrases -- this fixture carries the real
            # spend-limit modal's chrome (credit balance / adjust-limit / footer,
            # footer as the LAST line per round 2 Finding 1, "❯"-adjacent option
            # per round 2 Finding 2) so it still classifies, but deliberately
            # omits any "Resets ..." date/time text so the RESET-TIME parse
            # still fails and the conservative-default path this test exists to
            # exercise still fires.
            unparseable_text = (
                "You've hit your monthly spend limit\n"
                "What do you want to do?                        Usage credit balance: $8.08\n"
                "❯ Adjust monthly spend limit:        ← or → to adjust\n"
                "Wait for limit to reset\n"
                "Enter to confirm · Esc to cancel\n"
            )

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: unparseable_text, cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsInstance(result, rc.MidSessionCapSwapOutcome)
            self.assertEqual(result.cooldown_source, "conservative-default")
            # T-4037 REBOUND round 2 Finding 3 (debounce): confirmation needs a
            # SECOND identical classifying sample -- the cap-check (and thus the
            # conservative-default base instant) fires after the SECOND
            # sleep(5.0), at start+10s, not the first.
            self.assertEqual(
                result.cooldown_until,
                start + timedelta(seconds=10.0)
                + timedelta(seconds=rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS),
            )

    def test_prose_quoting_modal_phrases_never_terminates_or_writes_modal_present(self) -> None:
        """T-4037 REBOUND Finding 1 test (d): a pane that merely quotes the
        cap modal's sentinel + framing phrases in ordinary prose -- the
        reviewer's false-positive shape -- on a flag-on, non-comms,
        fallback-viable binding must produce NO terminate call and must NOT
        write modal_present:true to the cross-account state file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 2

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            prose_text = (
                "Reviewing the T-4037 false positive: a captured pane read\n"
                "  You've hit your session limit · resets 2:50pm (UTC)\n"
                "and the classifier also keys on /rate-limit-options plus the phrases\n"
                "\"what do you want to do?\", \"stop and wait for limit to reset\", and\n"
                "\"upgrade your plan\" -- none of which means a modal is actually open.\n"
            )

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: prose_text, cap_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])
            state = rc.read_mid_session_cap_state("recovery", state_root=root)
            self.assertFalse(state["modal_present"])

    def test_swap_attempt_that_does_not_die_fails_safe_never_returns_outcome(self) -> None:
        """HO amendment A: SIGTERM is a request, not a guarantee. A pane still
        alive at the confirm deadline must NOT produce a swap outcome, must
        NOT record subscription_cap, and must NOT escalate to SIGKILL -- the
        loop just continues (retried on a later cap-check cycle)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            # T-4037 REBOUND round 2 Finding 3 (debounce): confirmation now
            # needs a SECOND identical classifying sample before the swap
            # attempt fires at all, shifting every subsequent is_alive() call
            # by one outer-loop iteration versus the pre-debounce count --
            # <=5 (was <=4) keeps this test's fake "alive" through the whole
            # post-terminate confirm-death window: 1 unconfirmed sample, 1
            # confirming sample (fires terminate), 2 calls inside the
            # confirm-wait loop, 1 post-loop fail-safe check == 5 True calls,
            # then the 6th (outer loop) call ends the function.
            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 5  # stays "alive" through the whole confirm window

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)  # never flips alive -- SIGTERM did not take

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep, poll_interval_seconds=5.0,
                capture_pane_fn=lambda: self._session_cap_text(), cap_check_interval_seconds=5.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
                terminate_confirm_timeout_seconds=10.0,
            )
            self.assertIsNone(result)
            self.assertEqual(len(terminate_calls), 1)


class BlockUntilTmuxDeathFallbackReturnTests(unittest.TestCase):
    """T-4046: the failback trigger/arm/idle-wait/act loop, exercised through
    block_until_tmux_death's injectable seams -- no real tmux process is ever
    touched. Idle-pane text is the REAL live-captured codex composer render
    from tools.test_bringup_acceptance_harness_codex_ready.
    READY_COMPOSER_AFTER_DISMISS (T-4046 HO amendment A: prove
    ready_for_submission recognizes an idle CODEX pane, not just claude's,
    using a real capture rather than an invented string)."""

    def _clock(self, start: datetime) -> tuple[Callable[[], datetime], Callable[[float], None]]:
        state = {"t": start}

        def now() -> datetime:
            return state["t"]

        def sleep(seconds: float) -> None:
            state["t"] = state["t"] + timedelta(seconds=seconds)

        return now, sleep

    def test_fires_when_target_viable_and_pane_idle_after_min_dwell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            alive_state = {"alive": True}

            def alive_check() -> bool:
                return alive_state["alive"]

            def terminate_runtime_fn(b, **kwargs) -> None:
                alive_state["alive"] = False  # simulate a successful SIGTERM

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsInstance(result, rc.FallbackReturnOutcome)
            self.assertEqual(result.target_ref, "claude/anthropic-code")

    def _handoff_scenario(self, handoff_results: list[bool], *, retry: float = 600.0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            alive = {"v": True, "ticks": 0}
            asked: list[Any] = []
            terminated: list[Any] = []
            results = list(handoff_results)

            def alive_check() -> bool:
                alive["ticks"] += 1
                return alive["v"] and alive["ticks"] <= 40

            def handoff_first_fn(b) -> bool:
                asked.append(b)
                return results.pop(0) if results else True

            def terminate(b, **kwargs) -> None:
                terminated.append(b)
                alive["v"] = False

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate,
                handoff_first_fn=handoff_first_fn, handoff_retry_interval_seconds=retry,
            )
            return result, asked, terminated

    def test_handoff_first_confirmed_then_terminates(self) -> None:
        """T-5069: the swap asks for a handoff, and only kills after it is confirmed."""
        result, asked, terminated = self._handoff_scenario([True])
        self.assertIsInstance(result, rc.FallbackReturnOutcome)
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(terminated), 1)

    def test_handoff_unconfirmed_defers_then_retries_after_interval(self) -> None:
        result, asked, terminated = self._handoff_scenario([False, True], retry=1.0)
        self.assertIsInstance(result, rc.FallbackReturnOutcome)
        self.assertEqual(len(asked), 2)
        self.assertEqual(len(terminated), 1)

    def test_handoff_never_confirmed_never_terminates(self) -> None:
        result, asked, terminated = self._handoff_scenario([False] * 50, retry=1.0)
        self.assertIsNone(result)
        self.assertEqual(terminated, [])
        self.assertGreaterEqual(len(asked), 2)

    def test_unconfirmed_handoff_not_retried_inside_retry_interval(self) -> None:
        result, asked, terminated = self._handoff_scenario([False] * 50, retry=10_000_000.0)
        self.assertIsNone(result)
        self.assertEqual(len(asked), 1)
        self.assertEqual(terminated, [])

    def test_does_not_fire_while_target_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 3

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            holds = {"claude/anthropic-code": {"reason": "x"}}
            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds=holds,
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])

    def test_does_not_fire_while_target_in_cooldown_then_fires_once_expired(self) -> None:
        """Also the T-4037/T-4046 non-fight proof: a swap-recorded
        subscription_cap cooldown on the higher-priority runtime blocks
        failback until that SAME cooldown genuinely expires."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            start = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            now, sleep = self._clock(start)
            alive_state = {"alive": True}

            def alive_check() -> bool:
                return alive_state["alive"]

            def terminate_runtime_fn(b, **kwargs) -> None:
                alive_state["alive"] = False

            rc.record_last_attempt(
                "recovery", claude, outcome="failed", failure_class="subscription_cap",
                state_root=root, now=start,
                cooldown_until_override=start + timedelta(seconds=450),
            )

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            # tick1 (t=300s): dwell satisfied, but claude's cooldown (until
            # t=450s) is still active -- no arm.
            # tick2 (t=600s): cooldown has expired -- arms.
            # tick3 (t=900s): armed tick, idle pane -- fires.
            self.assertIsInstance(result, rc.FallbackReturnOutcome)

    def test_does_not_fire_when_already_first_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code", fallback_return=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 3

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                claude, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])

    def test_waits_for_idle_up_to_bound_then_acts_anyway(self) -> None:
        """T-4046 HO amendment B: the bound-reached -> act-anyway path,
        exercised explicitly -- a persistently busy pane must not block
        failback forever, but must not be interrupted before the bound
        either."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            start = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            now, sleep = self._clock(start)
            alive_state = {"alive": True}

            def alive_check() -> bool:
                return alive_state["alive"]

            terminate_ats: list[datetime] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_ats.append(now())
                alive_state["alive"] = False

            busy_text = "no frame, no glyph -- definitely not idle\n"

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: busy_text,
                fallback_return_check_interval_seconds=0.0,
                fallback_return_idle_max_wait_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS * 2,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsInstance(result, rc.FallbackReturnOutcome)
            self.assertEqual(len(terminate_ats), 1)
            # Armed at tick1 (t=dwell); the bound is 2*dwell past THAT --
            # acting must land at tick3 (t=3*dwell), never earlier.
            self.assertEqual(
                terminate_ats[0],
                start + timedelta(seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS * 3),
            )

    def test_flag_off_makes_it_inert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 3

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])

    def test_min_dwell_blocks_failback_before_runtime_has_been_alive_long_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 3  # 3 * 60s = 180s, well under the 300s dwell floor

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep, poll_interval_seconds=60.0,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
            )
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])

    def test_confirm_death_fail_safe_stays_armed_no_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                return calls["n"] <= 4  # stays alive through arm + act + confirm-wait

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)  # never flips alive -- SIGTERM did not take

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds={},
                terminate_runtime_fn=terminate_runtime_fn,
                terminate_confirm_timeout_seconds=10.0,
            )
            self.assertIsNone(result)
            self.assertEqual(len(terminate_calls), 1)

    def test_disarms_without_terminating_when_target_becomes_ineligible_before_idle(self) -> None:
        """T-4047 (item 5): armed for failback -> the target goes ineligible
        (a hold appears) on the very next tick, BEFORE the fallback pane is
        ever checked for idle -- must disarm silently (continue, no
        terminate call), never fall through to the idle/act branch below it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            now, sleep = self._clock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
            holds: dict[str, Any] = {}
            calls = {"n": 0}

            def alive_check() -> bool:
                calls["n"] += 1
                if calls["n"] == 2:
                    # A hold appears on claude between the arm-tick (call 1)
                    # and the re-check tick (call 2) -- simulates an
                    # operator (or another mechanism) intervening mid-arm.
                    holds["claude/anthropic-code"] = {"reason": "test-hold-injected-after-arm"}
                return calls["n"] <= 2

            terminate_calls: list[Any] = []

            def terminate_runtime_fn(b, **kwargs) -> None:
                terminate_calls.append(b)

            result = rc.block_until_tmux_death(
                codex, alive_check=alive_check, sleep=sleep,
                poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                # No capture_pane_fn -- the disarm branch must return via
                # `continue` before ever reaching the idle-pane check, so
                # this proves it never even LOOKS at pane content.
                fallback_return_check_interval_seconds=0.0,
                now=now, state_root=root, candidates=[claude, codex], holds=holds,
                terminate_runtime_fn=terminate_runtime_fn,
            )
            # tick1 (t=dwell): dwell satisfied, claude viable -> ARMS.
            # tick2 (t=2*dwell): re-check sees claude now held -> DISARMS
            #   (continue) without ever checking idle or terminating.
            # tick3: is_alive() returns False -> loop exits cleanly.
            self.assertIsNone(result)
            self.assertEqual(terminate_calls, [])


def _write_operator_transition(
    root: Path, destination: str, *, phase: str = "completed", trigger: str = "operator",
) -> None:
    """A transition.json in the shape proactive_swap_init leaves behind (T-4469)."""
    rc._atomic_json(
        rc._transition_path("recovery", root),
        {
            "schema": "pantheon-runtime-transition/v1",
            "persona": "recovery",
            "trigger": trigger,
            "phase": phase,
            "destination": destination,
            "transition_id": "t4469",
        },
    )


def _pane_runs(*harnesses: str):
    """A `_pane_runs_binding` stand-in: the pane positively runs these harnesses only."""
    return lambda binding, **kwargs: binding.launch.harness in harnesses


def _evidence_events(root: Path) -> list[str]:
    path = rc._evidence_log_path("recovery", root)
    if not path.exists():
        return []
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]


def _evidence_records(root: Path) -> list[dict]:
    path = rc._evidence_log_path("recovery", root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_STALE_SOURCE_KEY = "tools/retired_from_source_hash_paths.py"


def _mutate_recorded_key_set(state: dict, variant: str) -> None:
    """T-5315: make a runtime.json record a SELF-CONSISTENT different path list.
    "removed": the record lacks the last current path (a path newly hashed);
    "added": the record carries a path the current list no longer hashes. The
    manifest is recomputed so only the key set differs from current."""
    hashes = state["controller_source_hashes"]
    if variant == "removed":
        hashes.pop(rc.RUNTIME_SOURCE_HASH_PATHS[-1])
    elif variant == "added":
        hashes[_STALE_SOURCE_KEY] = "d" * 64
    else:
        raise AssertionError(variant)
    state["controller_source_manifest_sha256"] = rc._source_manifest_hash(hashes)


class SourceHashValidatorT5315Tests(unittest.TestCase):
    """T-5315: recorded-state validation is self-consistency only; the CURRENT
    provenance validator stays strict against RUNTIME_SOURCE_HASH_PATHS."""

    @staticmethod
    def _state(hashes: dict) -> dict:
        return {
            "controller_source_hashes": hashes,
            "controller_source_manifest_sha256": rc._source_manifest_hash(hashes),
        }

    def _full(self) -> dict:
        return {relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS}

    def test_self_consistent_different_key_set_is_accepted(self) -> None:
        hashes = self._full()
        hashes.pop(rc.RUNTIME_SOURCE_HASH_PATHS[-1])
        hashes["tools/some_retired_path.py"] = "b" * 64
        self.assertEqual(rc._validated_source_hashes_from_state(self._state(hashes)), hashes)

    def test_empty_map_is_missing(self) -> None:
        with self.assertRaises(ValueError) as caught:
            rc._validated_source_hashes_from_state(self._state({}))
        self.assertEqual(str(caught.exception), "recorded-source-hashes-missing")

    def test_malformed_and_traversal_keys_are_rejected(self) -> None:
        for bad in ("", "/etc/passwd", "../outside.py", "tools/../../x.py", "tools//x.py",
                    "./tools/x.py", "tools/"):
            with self.subTest(key=bad):
                hashes = self._full()
                hashes[bad] = "a" * 64
                with self.assertRaises(ValueError) as caught:
                    rc._validated_source_hashes_from_state(self._state(hashes))
                self.assertEqual(str(caught.exception), "recorded-source-key-malformed")

    def test_non_string_key_is_rejected(self) -> None:
        hashes: dict = self._full()
        hashes[7] = "a" * 64
        state = {"controller_source_hashes": hashes, "controller_source_manifest_sha256": "a" * 64}
        with self.assertRaises(ValueError) as caught:
            rc._validated_source_hashes_from_state(state)
        self.assertEqual(str(caught.exception), "recorded-source-key-malformed")

    def test_manifest_mismatch_is_rejected(self) -> None:
        state = self._state(self._full())
        state["controller_source_hashes"].pop(rc.RUNTIME_SOURCE_HASH_PATHS[0])
        with self.assertRaises(ValueError) as caught:
            rc._validated_source_hashes_from_state(state)
        self.assertEqual(str(caught.exception), "recorded-source-manifest-mismatch")

    def test_current_validator_still_rejects_key_set_mismatch(self) -> None:
        boundary = datetime(2026, 10, 21, tzinfo=timezone.utc)
        for mutate in ("removed", "added"):
            with self.subTest(mutate=mutate):
                hashes = self._full()
                if mutate == "removed":
                    hashes.pop(rc.RUNTIME_SOURCE_HASH_PATHS[-1])
                else:
                    hashes["tools/not_hashed.py"] = "a" * 64
                provenance = rc.RuntimeSourceProvenance(
                    revision="1" * 40,
                    source_hashes=hashes,
                    source_manifest_sha256=rc._source_manifest_hash(hashes),
                    runtime_start_boundary_at=boundary,
                    runtime_start_observed_at=boundary,
                )
                with self.assertRaises(ValueError) as caught:
                    rc._validated_current_source_hashes(provenance)
                self.assertEqual(str(caught.exception), "current-source-key-mismatch")

    def test_key_set_delta_is_sorted_and_bounded(self) -> None:
        self.assertEqual(rc._source_key_set_delta({"a": 1}, {"a": 2}), {})
        recorded = {f"r{i:03d}": 1 for i in range(50)}
        current = {f"c{i:03d}": 1 for i in range(40, -1, -1)}
        delta = rc._source_key_set_delta(recorded, current)
        self.assertEqual(delta["source_keys_added_count"], 41)
        self.assertEqual(delta["source_keys_removed_count"], 50)
        self.assertEqual(len(delta["source_keys_added"]), rc.SOURCE_KEY_DELTA_MAX_PATHS)
        self.assertEqual(delta["source_keys_added"], sorted(delta["source_keys_added"]))
        self.assertEqual(delta["source_keys_added"][0], "c000")


class OperatorSwapT4469Tests(unittest.TestCase):
    """T-4469: an operator swap (proactive_swap_init --trigger operator) runs the new
    harness in the SAME pane, so the harness-blind liveness check stayed true and the
    controller kept supervising the runtime it started. These pin the three repairs:
    the supervise loop notices the swap, the respawn pins the live destination, and
    the pane identity is read positively (never guessed)."""

    def _clock(self, start: datetime) -> tuple[Callable[[], datetime], Callable[[float], None]]:
        state = {"t": start}

        def now() -> datetime:
            return state["t"]

        def sleep(seconds: float) -> None:
            state["t"] = state["t"] + timedelta(seconds=seconds)

        return now, sleep

    def _pair(self, root: Path) -> tuple[Any, Any]:
        claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
        codex = _stateless_binding(
            root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
        )
        return claude, codex

    def _alive_for(self, ticks: int) -> Callable[[], bool]:
        calls = {"n": 0}

        def alive_check() -> bool:
            calls["n"] += 1
            return calls["n"] <= ticks

        return alive_check

    # -- _operator_transition_destination ---------------------------------

    def test_destination_is_the_candidate_a_completed_operator_transition_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            self.assertIsNone(rc._operator_transition_destination("recovery", [claude, codex], state_root=root))
            _write_operator_transition(root, "codex/interactive-chatgpt")
            self.assertIs(
                rc._operator_transition_destination("recovery", [claude, codex], state_root=root), codex
            )
            self.assertIsNone(rc._operator_transition_destination("recovery", [], state_root=root))

    def test_destination_ignores_in_flight_non_operator_and_unknown_transitions(self) -> None:
        cases = {
            "in-flight": dict(phase="started"),
            "non-operator": dict(trigger="cap"),
            "not-a-candidate": dict(destination="codex/some-other-profile"),
        }
        for label, overrides in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude, codex = self._pair(root)
                destination = overrides.pop("destination", "codex/interactive-chatgpt")
                _write_operator_transition(root, destination, **overrides)
                self.assertIsNone(
                    rc._operator_transition_destination("recovery", [claude, codex], state_root=root)
                )

    # -- _pane_runs_binding / _operator_pinned_runtime ---------------------

    def test_pane_runs_binding_needs_a_positive_argv_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex = self._pair(Path(tmp))
            with mock.patch.object(rc, "_pane_process_pid", return_value=None):
                self.assertFalse(rc._pane_runs_binding(codex))
            with mock.patch.object(rc, "_pane_process_pid", return_value=4242), mock.patch.object(
                rc, "_read_pane_cmdline", return_value=None
            ):
                self.assertFalse(rc._pane_runs_binding(codex))
            with mock.patch.object(rc, "_pane_process_pid", return_value=4242), mock.patch.object(
                rc, "_read_pane_cmdline", return_value=["node", "/usr/bin/codex", "--profile", "x"]
            ):
                self.assertTrue(rc._pane_runs_binding(codex))
                self.assertFalse(rc._pane_runs_binding(claude))

    def test_pin_requires_the_live_pane_and_yields_to_a_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            _write_operator_transition(root, "codex/interactive-chatgpt")
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                self.assertIs(
                    rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root), codex
                )
                self.assertIsNone(
                    rc._operator_pinned_runtime(
                        "recovery", [claude, codex],
                        holds={"codex/interactive-chatgpt": {"reason": "x"}}, state_root=root,
                    )
                )
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("claude")):
                self.assertIsNone(
                    rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root)
                )

    def test_pin_is_operator_only_never_a_quota_destination(self) -> None:
        """T-4872: consume tolerates a quota destination, but controller_main's pin
        (fallback-return owns the return) pins only an OPERATOR destination."""
        for trigger in ("planned-quota", "forced-cap", "quota-reset"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude, codex = self._pair(root)
                with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                    _write_operator_transition(root, "codex/interactive-chatgpt", trigger=trigger)
                    self.assertIsNone(
                        rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root)
                    )
                    self.assertIsNone(
                        rc._operator_transition_destination("recovery", [claude, codex], state_root=root)
                    )
                    # Same fixture, operator trigger: still pinned (both sides of the guard).
                    _write_operator_transition(root, "codex/interactive-chatgpt")
                    self.assertIs(
                        rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root), codex
                    )

    # -- block_until_tmux_death --------------------------------------------

    def test_supervise_loop_returns_operator_swap_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            _write_operator_transition(root, "codex/interactive-chatgpt")
            now, sleep = self._clock(datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc))
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                result = rc.block_until_tmux_death(
                    claude, alive_check=self._alive_for(10), sleep=sleep,
                    poll_interval_seconds=rc.OPERATOR_SWAP_CHECK_INTERVAL_SECONDS,
                    now=now, state_root=root, candidates=[claude, codex], holds={},
                )
            self.assertIsInstance(result, rc.OperatorSwapOutcome)
            self.assertEqual(result.destination_ref, "codex/interactive-chatgpt")

    def test_supervise_loop_ignores_transitions_it_must_not_act_on(self) -> None:
        cases = {
            "names-the-supervised-runtime": dict(destination="claude/anthropic-code"),
            "in-flight": dict(phase="started"),
            "non-operator": dict(trigger="cap"),
            "pane-not-running-it": dict(pane=()),
            "destination-held": dict(holds={"codex/interactive-chatgpt": {"reason": "x"}}),
        }
        for label, overrides in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude, codex = self._pair(root)
                pane = overrides.pop("pane", ("codex", "claude"))
                holds = overrides.pop("holds", {})
                destination = overrides.pop("destination", "codex/interactive-chatgpt")
                _write_operator_transition(root, destination, **overrides)
                now, sleep = self._clock(datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc))
                with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs(*pane)):
                    result = rc.block_until_tmux_death(
                        claude, alive_check=self._alive_for(10), sleep=sleep,
                        poll_interval_seconds=rc.OPERATOR_SWAP_CHECK_INTERVAL_SECONDS,
                        now=now, state_root=root, candidates=[claude, codex], holds=holds,
                    )
                self.assertIsNone(result)

    def test_operator_swap_is_checked_before_fallback_return_can_terminate_the_pane(self) -> None:
        """The dangerous ordering: the controller supervises codex (fallback-return
        armed-eligible, idle pane) while an operator has already swapped the pane
        back to claude. Fallback-return would SIGTERM the pane's process -- now the
        operator's claude. The swap check must win."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            _write_operator_transition(root, "claude/anthropic-code")
            now, sleep = self._clock(datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc))
            terminate_calls: list[Any] = []
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("claude")):
                result = rc.block_until_tmux_death(
                    codex, alive_check=self._alive_for(10), sleep=sleep,
                    poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                    capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                    fallback_return_check_interval_seconds=0.0,
                    now=now, state_root=root, candidates=[claude, codex], holds={},
                    terminate_runtime_fn=lambda b, **kwargs: terminate_calls.append(b),
                )
            self.assertIsInstance(result, rc.OperatorSwapOutcome)
            self.assertEqual(result.destination_ref, "claude/anthropic-code")
            self.assertEqual(terminate_calls, [])

    def test_held_operator_destination_is_not_pinned_when_holds_are_read_from_disk(self) -> None:
        """T-5069 REBOUND: with holds=None (the controller_main wiring) the T-4469 veto
        "a held runtime is never pinned" must still hold, so the holds are read from disk."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            _write_operator_transition(root, "claude/anthropic-code")
            holds_file = rc._holds_path("recovery", state_root=root)
            holds_file.parent.mkdir(parents=True, exist_ok=True)
            holds_file.write_text(json.dumps({"holds": {"claude/anthropic-code": {"reason": "x"}}}))
            now, sleep = self._clock(datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc))
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("claude")):
                result = rc.block_until_tmux_death(
                    codex, alive_check=self._alive_for(10), sleep=sleep,
                    poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                    capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                    now=now, state_root=root, candidates=[claude, codex], holds=None,
                )
            self.assertNotIsInstance(result, rc.OperatorSwapOutcome)

    # -- controller_main ----------------------------------------------------

    def _fake_start(self, started: list[str]):
        def start(binding) -> tuple:
            started.append(rc._binding_runtime_ref(binding))
            runtime_start = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
            return runtime_start, handle

        return start

    def test_default_block_wiring_sees_a_hold_cleared_while_supervising(self) -> None:
        """T-5069: the default block_until_tmux_death wiring must evaluate
        fallback-return against the holds on disk NOW, not the snapshot taken
        when the controller cycle started. Before the fix a claude hold set at
        cycle start (so codex was selected) was never seen as cleared, and the
        fallback-return stayed dead until a hand restart."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", fallback_return=True,
            )
            holds_file = rc._holds_path("orchestrator", state_root=root)
            holds_file.parent.mkdir(parents=True, exist_ok=True)
            holds_file.write_text(json.dumps({"holds": {"claude/anthropic-code": {"reason": "x"}}}))
            clock = {"t": datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)}
            alive = {"v": True, "ticks": 0}
            real_block = rc.block_until_tmux_death
            started: list[str] = []

            def alive_check() -> bool:
                # Bounded so a regression fails the assert instead of hanging.
                alive["ticks"] += 1
                return alive["v"] and alive["ticks"] <= 20

            def block_with_clear(binding, **kwargs):
                # The operator clears the hold after the cycle has started.
                holds_file.write_text(json.dumps({"holds": {}}))
                kwargs.update(
                    alive_check=alive_check,
                    capture_pane_fn=lambda: READY_COMPOSER_AFTER_DISMISS,
                    poll_interval_seconds=rc.FALLBACK_RETURN_MIN_DWELL_SECONDS,
                    fallback_return_check_interval_seconds=0.0,
                    terminate_runtime_fn=lambda b, **kw: alive.update(v=False),
                )
                return real_block(binding, **kwargs)

            def sleep(seconds: float) -> None:
                clock["t"] = clock["t"] + timedelta(seconds=seconds)

            with (
                mock.patch.object(rc, "block_until_tmux_death", side_effect=block_with_clear),
                mock.patch.object(rc.handoff_first, "auto_swap_handoff_gate", return_value=True),
            ):
                rc.controller_main(
                    "orchestrator", candidates=[claude, codex], state_root=root,
                    now=lambda: clock["t"], sleep=sleep, max_reselect_cycles=1,
                    start_runtime_fn=self._fake_start(started),
                )
            self.assertEqual(started, ["codex/interactive-chatgpt"])
            self.assertFalse(alive["v"], "fallback-return never terminated the idle fallback pane")
            attempt = rc.load_last_attempt("orchestrator", "codex--interactive-chatgpt", state_root=root)
            self.assertEqual(attempt["outcome"], "success")

    def test_respawn_pins_the_live_operator_destination_over_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            _write_operator_transition(root, "codex/interactive-chatgpt")
            started: list[str] = []
            now = lambda: datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc)  # noqa: E731
            with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs("codex")):
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root,
                    now=now, sleep=lambda s: None,
                    start_runtime_fn=self._fake_start(started), block_until_death_fn=lambda b: None,
                )
            self.assertEqual(result, 0)
            self.assertEqual(started, ["codex/interactive-chatgpt"])
            self.assertIn("controller_selected_operator_destination", _evidence_events(root))

    def test_respawn_keeps_priority_when_the_pin_does_not_hold(self) -> None:
        cases = {
            "pane-not-running-it": dict(pane=("claude",), holds={}),
            "destination-held": dict(pane=("codex",), holds={"codex/interactive-chatgpt": {"reason": "x"}}),
        }
        for label, case in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude, codex = self._pair(root)
                _write_operator_transition(root, "codex/interactive-chatgpt")
                rc._atomic_json(
                    rc._holds_path("recovery", state_root=root),
                    {"schema": "pantheon-persona-runtime-holds/v1", "holds": case["holds"]},
                )
                started: list[str] = []
                now = lambda: datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc)  # noqa: E731
                with mock.patch.object(rc, "_pane_runs_binding", side_effect=_pane_runs(*case["pane"])):
                    rc.controller_main(
                        "recovery", candidates=[claude, codex], state_root=root,
                        now=now, sleep=lambda s: None,
                        start_runtime_fn=self._fake_start(started), block_until_death_fn=lambda b: None,
                    )
                self.assertEqual(started, ["claude/anthropic-code"])
                self.assertNotIn("controller_selected_operator_destination", _evidence_events(root))

    def test_operator_swap_outcome_records_success_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            base = datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc)
            clock = {"t": base}

            def block(binding):
                # Well under SUSTAINED_ALIVE_SECONDS: the outcome type, not elapsed
                # time, must drive the success record.
                clock["t"] = base + timedelta(seconds=20)
                return rc.OperatorSwapOutcome(destination_ref="codex/interactive-chatgpt")

            started: list[str] = []
            result = rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root,
                now=lambda: clock["t"], sleep=lambda s: None,
                start_runtime_fn=self._fake_start(started), block_until_death_fn=block,
            )
            self.assertEqual(result, 0)
            attempt = rc.load_last_attempt("recovery", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "success")
            self.assertIsNone(attempt["cooldown_until"])
            self.assertIsNone(attempt["failure_class"])
            self.assertIn("controller_observed_operator_swap", _evidence_events(root))
            self.assertEqual(rc.read_active_runtime("recovery", state_root=root), {})


class ControllerMainCycleTests(unittest.TestCase):
    """Exercises `controller_main` via its injectable seams (`candidates`,
    `start_runtime_fn`, `block_until_death_fn`, `now`, `sleep`) -- no real
    tmux/systemd process is ever touched, matching the file's existing
    test-seam convention."""

    def _fake_start(self, binding) -> tuple:
        started = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
        return started, handle

    def test_default_block_until_death_wiring_threads_sleep_end_to_end(self) -> None:
        """T-4047 (item 3, pre-existing gap): controller_main's DEFAULT
        block_until_death_fn closure -- exercised here by omitting
        block_until_death_fn entirely -- silently fell through to real
        time.sleep because it never forwarded controller_main's own `sleep`
        param. `_runtime_alive` is mocked directly (block_until_tmux_death's
        own docstring calls it a test-only seam; production never needs to
        override it) so this touches zero real tmux, per the destructive
        side-effect warning. `now` is FROZEN so the loop never crosses
        either the 60s cap-check or 60s fallback-check gate -- proving
        `_capture_visible_runtime_pane` (the other real-tmux call inside the
        default closure) is never actually invoked either."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            fixed_now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            sleep_calls: list[float] = []

            with mock.patch.object(
                rc, "_runtime_alive", side_effect=[True, False]
            ), mock.patch.object(rc, "_capture_visible_runtime_pane") as capture:
                result = rc.controller_main(
                    "orchestrator", candidates=[claude], state_root=root,
                    now=lambda: fixed_now, sleep=sleep_calls.append,
                    start_runtime_fn=self._fake_start,
                    # block_until_death_fn intentionally OMITTED -- exercises
                    # the real default closure built inside controller_main.
                )
            self.assertEqual(result, 0)
            # Proves controller_main's `sleep` reached block_until_tmux_death's
            # default `poll_interval_seconds` (5.0) -- pre-fix this silently
            # fell through to real time.sleep(5.0) instead.
            self.assertEqual(sleep_calls, [5.0])
            capture.assert_not_called()
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["failure_class"], "process_death")

    # --- T-4035: persist the runtime.json record on launch (best-effort) -------------
    @staticmethod
    def _fake_provenance(**kw) -> "rc.RuntimeSourceProvenance":
        return rc.RuntimeSourceProvenance(
            revision="a" * 40, source_hashes={"tools/runtime_controller.py": "b" * 64},
            source_manifest_sha256="c" * 64, **kw,
        )

    @staticmethod
    def _record_path(root, binding) -> Path:
        # keyed by the BINDING's persona (as ensure_runtime is) -- the fixture binding is
        # "recovery" while these tests drive controller_main("orchestrator"); in production they agree.
        return rc._persona_state(binding.persona, root) / "runtime.json"

    def _run_t4035(self, root, binding, **overrides):
        """controller_main with a death that snapshots runtime.json WHILE the runtime is
        up (the record's whole point) and a stderr capture."""
        record = self._record_path(root, binding)
        seen: dict[str, object] = {}

        def block(_binding):
            seen["record"] = json.loads(record.read_text()) if record.exists() else None

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = rc.controller_main(
                "orchestrator", candidates=[binding], state_root=root, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block, **overrides,
            )
        return result, seen.get("record"), err.getvalue()

    STALE = {"schema": "pantheon-runtime-state/v1", "persona": "orchestrator", "harness": "codex",
             "profile": "interactive", "model": "stale", "started_at": "2026-01-01T00:00:00Z"}

    def test_t4035_launch_persists_record_over_a_stale_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc._atomic_json(self._record_path(root, claude), self.STALE)
            with mock.patch.object(rc, "_controller_source_provenance",
                                   side_effect=self._fake_provenance):
                result, record, err = self._run_t4035(root, claude)
            self.assertEqual(result, 0)
            self.assertTrue(rc._state_matches(claude, record), record)
            self.assertEqual(record["last_structural_event"], "runtime_started")
            self.assertEqual(record["runtime_scope_unit"], rc.TMUX_RUNTIME_SCOPE_UNIT)
            self.assertEqual(record["controller_source_revision"], "a" * 40)
            self.assertEqual(err, "")

    def test_t4035_no_provenance_leaves_old_record_and_launch_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc._atomic_json(self._record_path(root, claude), self.STALE)
            with mock.patch.object(
                rc, "_controller_source_provenance",
                side_effect=RuntimeError("controller source provenance requires a clean git worktree"),
            ):
                result, record, err = self._run_t4035(root, claude)
            self.assertEqual(result, 0)
            self.assertEqual(record, self.STALE)  # NOT replaced by a provenance-less record
            self.assertIn("skipped: source provenance unavailable", err)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertIsNotNone(attempt)  # the launch went all the way through
            # T-5371: the skip is also in the durable evidence log, not only stderr.
            evidence = root / "orchestrator" / "runtime-controller" / "controller-evidence.jsonl"
            rows = [json.loads(l) for l in evidence.read_text().splitlines()]
            skips = [r for r in rows if r.get("event") == "runtime_record_not_persisted"]
            self.assertEqual(len(skips), 1, rows)
            self.assertEqual(skips[0]["selected"], "claude/anthropic-code")
            self.assertIn("requires a clean git worktree", skips[0]["outcome"])

    def test_t5371_persisted_launch_writes_no_skip_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            result, _record, _err = self._run_t4035(
                root, claude, persist_record_fn=lambda *a, **k: "persisted")
            self.assertEqual(result, 0)
            evidence = root / "orchestrator" / "runtime-controller" / "controller-evidence.jsonl"
            text = evidence.read_text() if evidence.exists() else ""
            self.assertNotIn("runtime_record_not_persisted", text)

    def test_t4035_persist_exception_never_aborts_the_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")

            def boom(*_a, **_k):
                raise OSError("disk full")

            result, _record, err = self._run_t4035(root, claude, persist_record_fn=boom)
            self.assertEqual(result, 0)
            self.assertIn("failed: OSError: disk full", err)
            invocations = (root / "orchestrator" / "invocations").glob("*.jsonl")
            events = [json.loads(l)["outcome"] for f in invocations for l in f.read_text().splitlines()]
            self.assertEqual(events, ["started"])  # no extra invocation outcome (LastSelectionTs)

    def test_t4035_broken_stderr_on_the_skip_log_never_aborts_the_launch(self) -> None:
        """Codex review: the skip/failure diagnostic itself must be best-effort too --
        a closed log pipe (BrokenPipeError on stderr) may not abort a relaunch."""
        class _BrokenStream(io.StringIO):
            def write(self, _s):
                raise BrokenPipeError("log pipe closed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            with contextlib.redirect_stderr(_BrokenStream()):
                result = rc.controller_main(
                    "orchestrator", candidates=[claude], state_root=root, sleep=lambda s: None,
                    start_runtime_fn=self._fake_start, block_until_death_fn=lambda b: None,
                    persist_record_fn=lambda *a, **k: "skipped: test",
                )
            self.assertEqual(result, 0)
            self.assertIsNotNone(rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root))

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

    def test_swap_outcome_records_subscription_cap_and_skips_success_branch(self) -> None:
        """T-4037: even when the runtime lived well past SUSTAINED_ALIVE_SECONDS
        before the swap fired (the realistic case -- a mid-session cap hits
        after a long healthy stretch), a MidSessionCapSwapOutcome from
        block_until_death_fn must NEVER reach the sustained-alive success
        branch, and must use the outcome's own cooldown, not next_weekly_reset."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            base = datetime(2026, 9, 27, 4, 0, 0, tzinfo=timezone.utc)
            clock = {"t": base}

            def now():
                return clock["t"]

            swap_cooldown = base + timedelta(hours=2)

            def block(binding):
                # runtime lived well past SUSTAINED_ALIVE_SECONDS before the swap --
                # the success branch would fire here if the bypass didn't hold.
                clock["t"] = base + timedelta(seconds=rc.SUSTAINED_ALIVE_SECONDS * 3)
                return rc.MidSessionCapSwapOutcome(
                    modal_kind="session-limit", cooldown_until=swap_cooldown, cooldown_source="parsed",
                )

            rc_result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=now, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block,
            )
            self.assertEqual(rc_result, 0)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["outcome"], "failed")
            self.assertEqual(attempt["failure_class"], "subscription_cap")
            self.assertEqual(attempt["cooldown_until"], rc._iso(swap_cooldown))
            self.assertNotEqual(
                attempt["cooldown_until"],
                rc._iso(runtime_quota.next_weekly_reset("claude", now=base)),
            )

    def test_fallback_return_outcome_records_success_not_failure(self) -> None:
        """T-4046: a FallbackReturnOutcome from block_until_death_fn must
        record `selected` (the ended, lower-priority runtime) as a SUCCESS --
        never a failure/cooldown -- regardless of how long it had actually
        been alive (a planned failback is not the runtime's fault)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            base = datetime(2026, 9, 27, 4, 0, 0, tzinfo=timezone.utc)
            clock = {"t": base}

            def now():
                return clock["t"]

            def block(binding):
                # Fires almost immediately -- well under SUSTAINED_ALIVE_SECONDS --
                # proving the outcome type, not elapsed time, drives the branch.
                clock["t"] = base + timedelta(seconds=5)
                return rc.FallbackReturnOutcome(target_ref="claude/anthropic-code")

            rc_result = rc.controller_main(
                "orchestrator", candidates=[codex], state_root=root,
                now=now, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block,
            )
            self.assertEqual(rc_result, 0)
            attempt = rc.load_last_attempt("orchestrator", "codex--interactive-chatgpt", state_root=root)
            self.assertEqual(attempt["outcome"], "success")
            self.assertEqual(attempt["consecutive_failures"], 0)
            self.assertIsNone(attempt["cooldown_until"])
            self.assertIsNone(attempt["failure_class"])

    def test_fallback_return_outcome_leaves_completed_transition_receipt(self) -> None:
        """T-5794: controller_main's fallback-return branch replaces the stale
        fallback_active record with a completed fallback-return receipt naming the
        return destination."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            rc._atomic_json(rc._transition_path("orchestrator", root), {
                "schema": "pantheon-runtime-transition/v1", "persona": "orchestrator",
                "trigger": "planned-quota", "phase": "completed", "fallback_active": True,
                "incoming_harness": "codex", "incoming_profile": "interactive-chatgpt",
            })
            with mock.patch.object(rc, "_notify_runtime_swap"):
                rc.controller_main(
                    "orchestrator", candidates=[codex], state_root=root, sleep=lambda s: None,
                    start_runtime_fn=self._fake_start,
                    block_until_death_fn=lambda b: rc.FallbackReturnOutcome(target_ref="claude/anthropic-code"),
                )
            state = rc.transition_state("orchestrator", state_root=root)
            self.assertEqual(
                (state["trigger"], state["phase"], state["destination"], state["fallback_active"],
                 state["source_runtime"]),
                ("fallback-return", "completed", "claude/anthropic-code", False, "codex/interactive-chatgpt"),
            )

    def test_mid_session_cap_state_cleared_on_both_ordinary_death_and_swap(self) -> None:
        # Two INDEPENDENT personas -- avoids the second call's candidate
        # colliding with a cooldown the first call's process_death recording
        # would otherwise leave behind (a shared persona + a constant `now` +
        # a no-op `sleep` would starve _select_viable_runtime forever with no
        # max_reselect_cycles bound; irrelevant to what this test checks).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = datetime(2026, 9, 27, 4, 0, 0, tzinfo=timezone.utc)
            observation = rc.MidSessionCapObservation(
                modal_present=True, modal_kind="session-limit", detected_at=base,
                observed_at=base, reset_at=base + timedelta(hours=1), reset_source="parsed",
            )

            claude_a = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc._write_mid_session_cap_state("persona-a", claude_a, observation, state_root=root)

            def block_ordinary(binding):
                return None

            rc.controller_main(
                "persona-a", candidates=[claude_a], state_root=root,
                now=lambda: base, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block_ordinary,
            )
            self.assertEqual(rc.read_mid_session_cap_state("persona-a", state_root=root), {})

            claude_b = _stateless_binding(root, harness="claude", profile="anthropic-code")
            rc._write_mid_session_cap_state("persona-b", claude_b, observation, state_root=root)

            def block_swap(binding):
                return rc.MidSessionCapSwapOutcome(
                    modal_kind="session-limit", cooldown_until=base + timedelta(hours=2),
                    cooldown_source="parsed",
                )

            rc.controller_main(
                "persona-b", candidates=[claude_b], state_root=root,
                now=lambda: base, sleep=lambda s: None,
                start_runtime_fn=self._fake_start, block_until_death_fn=block_swap,
            )
            self.assertEqual(rc.read_mid_session_cap_state("persona-b", state_root=root), {})

    def test_default_block_until_death_fn_threads_mid_session_cap_context(self) -> None:
        """No block_until_death_fn override -- controller_main must build the
        default wiring itself, threading state_root/candidates/holds/now and a
        pane-capture function into block_until_tmux_death every cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            captured: dict[str, Any] = {}

            def fake_block_until_tmux_death(binding, **kwargs):
                captured["binding"] = binding
                captured.update(kwargs)
                return None

            with mock.patch.object(rc, "block_until_tmux_death", fake_block_until_tmux_death):
                rc.controller_main(
                    "orchestrator", candidates=[claude], state_root=root,
                    sleep=lambda s: None, start_runtime_fn=self._fake_start,
                )
            self.assertIs(captured["binding"], claude)
            self.assertEqual(captured["state_root"], root)
            self.assertEqual(list(captured["candidates"]), [claude])
            # T-5069: holds are deliberately NOT snapshotted; the eligibility
            # predicates re-read them from disk on every evaluation.
            self.assertNotIn("holds", captured)
            self.assertTrue(callable(captured["handoff_first_fn"]))
            self.assertTrue(callable(captured["capture_pane_fn"]))
            self.assertTrue(callable(captured["now"]))

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

    def test_start_failure_with_parsed_reset_uses_proportionate_cooldown_not_weekly(self) -> None:
        """T-4047 PRIORITY: the central proof -- a start-up cap with a
        parsed reset instant gets THAT cooldown, materially shorter than
        next_weekly_reset, end-to-end through controller_main."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            fixed_now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
            reset_at = fixed_now + timedelta(hours=3)

            def failing_start(binding):
                raise rc.RuntimeOperationalFailure(
                    "capped at start", failure_class="subscription-cap",
                    cause="post-start-subscription-cap", binding_runtime="claude/anthropic-code",
                    readiness_evidence={"reset_at": rc._iso(reset_at), "cap_kind": "session-limit"},
                )

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=lambda: fixed_now, sleep=lambda s: None,
                start_runtime_fn=failing_start, block_until_death_fn=lambda binding: None,
            )
            self.assertEqual(result, 1)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(attempt["failure_class"], "subscription_cap")
            self.assertEqual(attempt["cooldown_until"], rc._iso(reset_at))
            self.assertNotEqual(
                attempt["cooldown_until"],
                rc._iso(runtime_quota.next_weekly_reset("claude", now=fixed_now)),
            )

    def test_start_failure_unparsed_session_kind_uses_conservative_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            fixed_now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)

            def failing_start(binding):
                raise rc.RuntimeOperationalFailure(
                    "capped at start", failure_class="subscription-cap",
                    cause="post-start-subscription-cap", binding_runtime="claude/anthropic-code",
                    readiness_evidence={"cap_kind": "session-limit"},
                )

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=lambda: fixed_now, sleep=lambda s: None,
                start_runtime_fn=failing_start, block_until_death_fn=lambda binding: None,
            )
            self.assertEqual(result, 1)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(
                attempt["cooldown_until"],
                rc._iso(fixed_now + timedelta(seconds=rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)),
            )

    def test_start_failure_unparsed_weekly_kind_still_uses_next_weekly_reset(self) -> None:
        """Regression pin: the ONE case that must keep pre-T-4047 behavior --
        a genuinely weekly, unparseable cap stays on next_weekly_reset."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            fixed_now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)

            def failing_start(binding):
                raise rc.RuntimeOperationalFailure(
                    "capped at start", failure_class="subscription-cap",
                    cause="post-start-subscription-cap", binding_runtime="claude/anthropic-code",
                    readiness_evidence={"cap_kind": "weekly-limit"},
                )

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root,
                now=lambda: fixed_now, sleep=lambda s: None,
                start_runtime_fn=failing_start, block_until_death_fn=lambda binding: None,
            )
            self.assertEqual(result, 1)
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertEqual(
                attempt["cooldown_until"],
                rc._iso(runtime_quota.next_weekly_reset("claude", now=fixed_now)),
            )

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

    # --- T-4101: serialize the controller start against the consume/wake path ---
    @staticmethod
    def _evidence_events(root: Path, persona: str) -> list[str]:
        path = rc._evidence_log_path(persona, root)
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text().splitlines() if line.strip()]

    def test_t4101_defers_and_does_not_respawn_while_consume_holds_the_lock(self) -> None:
        """T-4101 (the specialist 2026-09-25 race): while the consume/wake path holds
        persona_operation_lock (mid start + ~90s probe), the controller path MUST
        NOT respawn-pane -k the shared target -- it defers. With the lock HELD by
        this test, controller_main never enters the (default) start path, so no
        respawn-pane tmux call fires, block_until_death never runs, and the cycle
        is logged to controller-evidence.jsonl. No real tmux: _run_tmux is a fake
        CompletedProcess and pane liveness is mocked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            tmux_calls: list[tuple] = []

            def fake_run_tmux(*args, **kwargs):
                tmux_calls.append(args)
                return subprocess.CompletedProcess(list(args), 0, "", "")

            def block_must_not_run(_binding):
                raise AssertionError("block_until_death must not run on a deferred cycle")

            # Simulate consume holding the per-persona lock across its own launch.
            with rc.persona_operation_lock("orchestrator", state_root=root):
                with mock.patch.object(rc, "_run_tmux", side_effect=fake_run_tmux), \
                        mock.patch.object(rc, "pane_exists", return_value=True), \
                        mock.patch.object(rc, "_target_known", return_value=True), \
                        mock.patch.object(rc, "_runtime_alive", return_value=True):
                    result = rc.controller_main(
                        "orchestrator", candidates=[claude], state_root=root,
                        sleep=lambda s: None, max_reselect_cycles=1,
                        # default start_runtime_fn (real _controller_start_runtime) on
                        # purpose: if the defer failed, its respawn-pane -k would fire.
                        block_until_death_fn=block_must_not_run,
                    )
            self.assertEqual(result, 1)  # deferred cycle started nothing
            respawns = [c for c in tmux_calls if c and c[0] == "respawn-pane"]
            self.assertEqual(respawns, [], "controller stomped consume's pane")
            self.assertIn("controller_start_deferred_lock_contended", self._evidence_events(root, "orchestrator"))
            # And the lock is free again after controller_main returned (no leak/wedge).
            with rc.persona_operation_lock("orchestrator", state_root=root, blocking=False):
                pass

    def test_t4101_uncontended_start_acquires_lock_without_self_deadlock(self) -> None:
        """T-4101 constraint 2/3: on the normal (uncontended) controller start the
        acquire must NOT self-deadlock or block -- it starts, records, and returns,
        and the lock is released (re-acquirable) afterward."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            started: list = []

            def tracking_start(binding):
                started.append(binding)
                return self._fake_start(binding)

            result = rc.controller_main(
                "orchestrator", candidates=[claude], state_root=root, sleep=lambda s: None,
                start_runtime_fn=tracking_start, block_until_death_fn=lambda b: None,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(started), 1)  # started exactly once, no deadlock
            attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=root)
            self.assertIsNotNone(attempt)
            # lock released after the (uncontended) start span
            with rc.persona_operation_lock("orchestrator", state_root=root, blocking=False):
                pass

    def test_t4101_attaches_to_a_live_runtime_when_harness_matches(self) -> None:
        """T-4101 constraint 4 (TOCTOU re-check): if the consume/wake path already
        finished and left a viable runtime of the SELECTED harness in the pane, the
        controller (now holding the lock) must NOT respawn-pane -k over it -- it
        attaches and supervises. Exercises the REAL default start path
        (_controller_start_runtime); the live pane argv confirms the selected
        harness (claude), so the attach is legitimate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            tmux_calls: list[tuple] = []

            def fake_run_tmux(*args, **kwargs):
                tmux_calls.append(args)
                return subprocess.CompletedProcess(list(args), 0, "", "")

            seen: dict[str, object] = {}

            def block(_binding):
                # capture the active-runtime cache WHILE up (controller_main clears
                # it after this returns).
                seen["cache"] = rc.read_active_runtime("orchestrator", state_root=root)

            with mock.patch.object(rc, "_run_tmux", side_effect=fake_run_tmux), \
                    mock.patch.object(rc, "pane_exists", return_value=True), \
                    mock.patch.object(rc, "_target_known", return_value=True), \
                    mock.patch.object(rc, "_runtime_alive", return_value=True), \
                    mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                    mock.patch.object(rc, "_read_pane_cmdline", return_value=[str(claude.launch.executable)]):
                result = rc.controller_main(
                    "orchestrator", candidates=[claude], state_root=root, sleep=lambda s: None,
                    # default start_runtime_fn -> real _controller_start_runtime
                    block_until_death_fn=block,
                    persist_record_fn=lambda *a, **k: "persisted",
                )
            self.assertEqual(result, 0)
            respawns = [c for c in tmux_calls if c and c[0] == "respawn-pane"]
            self.assertEqual(respawns, [], "controller respawned over an already-live runtime")
            # attach evidence is keyed on the BINDING's persona (fixture: "recovery";
            # in production binding.persona == the controller_main persona).
            events = self._evidence_events(root, claude.persona)
            self.assertIn("controller_start_attached_live_runtime", events)
            self.assertNotIn("controller_start_respawn_over_foreign_pane", events)
            # the attached pane's observed pid is carried into the active-runtime cache
            self.assertEqual(seen["cache"]["pid"], 4242)

    def test_t4101_respawns_over_a_wrong_harness_pane_instead_of_mislabel_attach(self) -> None:
        """T-4101 REBOUND-1 F1 (RED against the pre-fix attach): the live pane is a
        DIFFERENT harness than the SELECTED binding (consume started codex while
        claude was capped; claude's cooldown then expired and this cycle selects
        claude). `_runtime_alive` is harness-blind, so the pre-fix attach would
        adopt the codex pane and record it as claude/anthropic-code with the codex
        pid -- corrupting continuity state. The fix must instead RESPAWN the
        selected (claude) runtime over the foreign pane."""
        from tools import persona_bringup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            start_replace: list[bool] = []

            def fake_start_runtime(binding, adapter, *, replace, **kw):
                # stand in for the low-level respawn: record that a real start
                # (respawn) was issued for the SELECTED binding, and with what shape.
                start_replace.append(replace)
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)

            seen: dict[str, object] = {}

            def block(_binding):
                seen["cache"] = rc.read_active_runtime("orchestrator", state_root=root)

            with mock.patch.object(persona_bringup, "build_adapter", return_value=object()), \
                    mock.patch.object(rc, "_start_runtime", side_effect=fake_start_runtime), \
                    mock.patch.object(rc, "_verify_started_runtime", return_value=None), \
                    mock.patch.object(rc, "_verify_runtime_input_ready", return_value=None), \
                    mock.patch.object(rc, "pane_exists", return_value=True), \
                    mock.patch.object(rc, "_target_known", return_value=True), \
                    mock.patch.object(rc, "_runtime_alive", return_value=True), \
                    mock.patch.object(rc, "_pane_process_pid", side_effect=[5555, 7777]), \
                    mock.patch.object(rc, "_read_pane_cmdline", return_value=[str(codex.launch.executable)]):
                result = rc.controller_main(
                    "orchestrator", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                    # default start_runtime_fn -> real _controller_start_runtime
                    block_until_death_fn=block,
                    persist_record_fn=lambda *a, **k: "persisted",
                )
            self.assertEqual(result, 0)
            # RESPAWNED the selected runtime (did NOT mislabel-attach the codex pane).
            self.assertEqual(start_replace, [True], "controller failed to respawn over the foreign pane")
            events = self._evidence_events(root, claude.persona)
            self.assertIn("controller_start_respawn_over_foreign_pane", events)
            self.assertNotIn("controller_start_attached_live_runtime", events)
            # active-runtime is claude (the selected/respawned harness), NOT recorded
            # carrying the codex pane's pid (5555) under a claude label.
            self.assertEqual(seen["cache"]["runtime"], "claude/anthropic-code")
            self.assertNotEqual(seen["cache"].get("pid"), 5555)  # not the codex pane pid
            self.assertEqual(seen["cache"].get("pid"), 7777)  # the freshly respawned pane


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
            # T-4047: no mid-session-cap state ever written -> empty-safe defaults.
            self.assertEqual(props["ModalPresent"], False)
            self.assertEqual(props["ModalKind"], "")
            self.assertEqual(props["ModalResetAt"], "")

    def test_seat_state_properties_are_published_additively(self) -> None:
        """ORG-106 L4a (T-5527): six SeatState* keys, fail-open to active with no record."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "personas"
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["SeatState"], "active")
            self.assertEqual(props["SeatStateSources"], [])
            self.assertEqual(props["SeatStateFault"], "")
            rec = root / "orchestrator" / "overrides" / "seat-state.json"
            rec.parent.mkdir(parents=True)
            rec.write_text(json.dumps({
                "schema": "pantheon-seat-state/v1", "state": "paused-intake", "phase": "set",
                "reason": "freeze", "owner": "operator", "indefinite": True, "expires": None,
                "set_ts": "2026-10-26T00:00:00Z", "phase_ts": "2026-10-26T00:00:00Z"}), encoding="utf-8")
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["SeatState"], "paused-intake")
            self.assertEqual(props["SeatStateOwner"], "operator")
            self.assertEqual(props["ViabilityStatus"], "viable")  # existing semantics intact

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

    def test_modal_present_surfaces_kind_and_reset_at_from_published_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
            reset_at = now + timedelta(hours=2)
            observation = rc.MidSessionCapObservation(
                modal_present=True, modal_kind="session-limit", detected_at=now,
                observed_at=now, reset_at=reset_at, reset_source="parsed",
            )
            rc._write_mid_session_cap_state("orchestrator", claude, observation, state_root=root)
            props = rc.dbus_health_properties("orchestrator", candidates=[claude], state_root=root)
            self.assertEqual(props["ModalPresent"], True)
            self.assertEqual(props["ModalKind"], "session-limit")
            self.assertEqual(props["ModalResetAt"], rc._iso(reset_at))

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


class ReconcileRuntimeRecordT4023(unittest.TestCase):
    """T-4023: out-of-band reconcile of the pantheon-runtime-state/v1 record from
    the live pane. Fail-closed, dry-run by default, idempotent, backs up before it
    overwrites. match_fn + provenance_fn are injected so these stay hermetic."""

    def _prov(self, dt=None):
        dt = dt or rc._utc_now()
        return SimpleNamespace(
            revision="0" * 40, source_hashes={"tools/runtime_controller.py": "y"},
            source_manifest_sha256="deadbeef",
            runtime_start_boundary_at=dt, runtime_start_observed_at=dt)

    def _prov_fn(self):
        return lambda **kw: self._prov()

    def _record_path(self, root, persona="recovery"):
        return Path(root) / persona / "runtime-controller" / "runtime.json"

    # --- reconcile_runtime_record ---

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=False, bindings=[b],
                match_fn=lambda bs, **kw: (b, 4242, ["/usr/bin/claude"]),
                provenance_fn=self._prov_fn())
            self.assertEqual(r.status, "dry-run")
            self.assertTrue(r.changed)
            self.assertIsNotNone(r.payload)
            self.assertFalse(self._record_path(tmp).exists())

    def test_apply_writes_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=True, bindings=[b],
                match_fn=lambda bs, **kw: (b, 4242, ["/usr/bin/claude"]),
                provenance_fn=self._prov_fn())
            self.assertEqual(r.status, "reconciled")
            written = rc._read_json(self._record_path(tmp))
            self.assertEqual(written.get("schema"), "pantheon-runtime-state/v1")
            self.assertTrue(rc._state_matches(b, written))
            self.assertEqual(written.get("last_structural_event"), "record_reconciled")
            self.assertIsNone(r.backup_path)

    def test_already_consistent_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            path = self._record_path(tmp)
            path.parent.mkdir(parents=True, exist_ok=True)
            rc._atomic_json(path, rc._runtime_payload(
                b, session_id=None, started_at=rc._utc_now(), source_provenance=self._prov()))
            before = path.read_text()
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=True, bindings=[b],
                match_fn=lambda bs, **kw: (b, 1, ["/usr/bin/claude"]),
                provenance_fn=self._prov_fn())
            self.assertEqual(r.status, "already-consistent")
            self.assertFalse(r.changed)
            self.assertEqual(path.read_text(), before)

    def test_apply_backs_up_stale_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            path = self._record_path(tmp)
            path.parent.mkdir(parents=True, exist_ok=True)
            rc._atomic_json(path, {"schema": "pantheon-runtime-state/v1",
                                   "harness": "codex", "stale": True})
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=True, bindings=[b],
                match_fn=lambda bs, **kw: (b, 1, ["/usr/bin/claude"]),
                provenance_fn=self._prov_fn())
            self.assertEqual(r.status, "reconciled")
            self.assertIsNotNone(r.backup_path)
            self.assertTrue(Path(r.backup_path).exists())
            self.assertEqual(rc._read_json(Path(r.backup_path)).get("harness"), "codex")
            self.assertTrue(rc._state_matches(b, rc._read_json(path)))

    def test_refused_when_match_fails_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            def _boom(bs, **kw):
                raise rc._ReconcileRefused("no live pane")
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=True, bindings=[b],
                match_fn=_boom, provenance_fn=self._prov_fn())
            self.assertEqual(r.status, "refused")
            self.assertFalse(r.changed)
            self.assertIn("no live pane", r.reason)
            self.assertFalse(self._record_path(tmp).exists())

    def test_refused_on_dirty_worktree_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _binding(Path(tmp), harness="claude")
            def _dirty(**kw):
                raise RuntimeError("controller source provenance requires a clean git worktree")
            r = rc.reconcile_runtime_record(
                "recovery", state_root=Path(tmp), apply=True, bindings=[b],
                match_fn=lambda bs, **kw: (b, 1, ["/usr/bin/claude"]),
                provenance_fn=_dirty)
            self.assertEqual(r.status, "refused")
            self.assertIn("worktree", r.reason)
            self.assertFalse(self._record_path(tmp).exists())

    # --- _match_live_binding (tmux/pid mocked; /proc faked) ---

    def _proc_with_cmdline(self, tmp, pid, args):
        d = Path(tmp) / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in args) + b"\x00")
        return Path(tmp)

    def test_match_unique_claude_over_codex_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = _binding(Path(tmp), harness="claude")
            codex = _binding(Path(tmp), harness="codex")
            proc = self._proc_with_cmdline(tmp, 555, ["/usr/bin/claude", "--model", "claude-opus-4-8"])
            with mock.patch.object(rc, "_session_exists", return_value=True), \
                 mock.patch.object(rc, "pane_exists", return_value=True), \
                 mock.patch.object(rc, "_pane_process_pid", return_value=555), \
                 mock.patch.object(rc, "_pid_alive", return_value=True):
                b, pid, _argv = rc._match_live_binding([claude, codex], proc_root=proc)
            self.assertIs(b, claude)
            self.assertEqual(pid, 555)

    def test_match_refuses_no_live_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = _binding(Path(tmp), harness="claude")
            with mock.patch.object(rc, "_session_exists", return_value=False), \
                 mock.patch.object(rc, "pane_exists", return_value=False):
                with self.assertRaises(rc._ReconcileRefused):
                    rc._match_live_binding([claude], proc_root=Path(tmp))

    def test_match_refuses_when_argv_matches_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = _binding(Path(tmp), harness="claude")
            proc = self._proc_with_cmdline(tmp, 7, ["/usr/bin/somethingelse"])
            with mock.patch.object(rc, "_session_exists", return_value=True), \
                 mock.patch.object(rc, "pane_exists", return_value=True), \
                 mock.patch.object(rc, "_pane_process_pid", return_value=7), \
                 mock.patch.object(rc, "_pid_alive", return_value=True):
                with self.assertRaises(rc._ReconcileRefused):
                    rc._match_live_binding([claude], proc_root=proc)

    def test_match_refuses_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            b1 = _binding(Path(tmp), harness="claude", profile="a")
            b2 = _binding(Path(tmp), harness="claude", profile="b")
            proc = self._proc_with_cmdline(tmp, 9, ["/usr/bin/claude"])
            with mock.patch.object(rc, "_session_exists", return_value=True), \
                 mock.patch.object(rc, "pane_exists", return_value=True), \
                 mock.patch.object(rc, "_pane_process_pid", return_value=9), \
                 mock.patch.object(rc, "_pid_alive", return_value=True):
                with self.assertRaises(rc._ReconcileRefused):
                    rc._match_live_binding([b1, b2], proc_root=proc)


class _StatuslineClaudeAdapter(_RecordingAdapter):
    """A claude-launching fake that carries the REAL adapter's statusline settings."""

    def __init__(self, binding) -> None:
        super().__init__(binding)
        self._real = ClaudeHarnessAdapter("specialist", claude_home=binding.account_home)

    def interactive_statusline_settings(self, *, output: Path) -> dict:
        return self._real.interactive_statusline_settings(output=output)


class StatuslineLaunchWiringTests(unittest.TestCase):
    """T-4024: the provider-completion probe reads <persona_state>/quota/
    claude-statusline.json, which only a statusLine command writes. The main
    session never had one, so completion could never be proven and every claude
    start through ensure_runtime was torn down at 90s (-> codex failover)."""

    def test_wired_command_writes_where_the_probe_reads(self) -> None:
        # End to end: run the wired statusLine command the way Claude Code does
        # (shell, JSON on stdin) and let the REAL adapter assess the probe.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            binding = _binding(root, harness="claude")
            adapter = ClaudeHarnessAdapter("specialist", claude_home=root)
            args = rc._statusline_launch_args(binding, adapter, state_root=state_root)
            self.assertEqual(args[0], "--settings")
            settings = json.loads(Path(args[1]).read_text(encoding="utf-8"))
            command = settings["statusLine"]["command"]
            self.assertEqual(settings["statusLine"]["type"], "command")

            transcript = root / ".claude" / "projects" / "p" / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")
            marker = "PANTHEON_PROVIDER_COMPLETION_t4024"
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona=binding.persona,
                account_home=root,
                state_dir=rc._persona_state(binding.persona, state_root),
                pane_baseline=rc._post_start_sample("ready\n❯ \n", capture_index=0),
                submitted_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                nonce_marker=marker,
                nonce_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            )
            assert probe is not None
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": marker}}) + "\n")
                handle.write(json.dumps({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                }) + "\n")
            payload = {"session_id": "s-1", "transcript_path": str(transcript), "model": {"id": "m"}}
            ran = subprocess.run(
                command, shell=True, input=json.dumps(payload), capture_output=True, text=True, timeout=30
            )
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(ran.stdout, "")  # empty status line: the TUI is unchanged
            assessment = adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(rc._post_start_sample("ready changed\n❯ \n", capture_index=1),),
            )
        self.assertEqual(assessment.status, "provider-completed")

    def test_non_claude_harness_gets_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="codex")
            adapter = ClaudeHarnessAdapter("specialist", claude_home=root)
            self.assertEqual(rc._statusline_launch_args(binding, adapter, state_root=root / "state"), ())
            self.assertFalse((root / "state").exists())

    def test_duck_typed_stand_in_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            self.assertEqual(rc._statusline_launch_args(binding, mock.Mock(), state_root=root / "state"), ())
            self.assertFalse((root / "state").exists())

    def test_settings_write_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocker = root / "state"
            blocker.write_text("not a directory", encoding="utf-8")
            binding = _binding(root, harness="claude")
            adapter = ClaudeHarnessAdapter("specialist", claude_home=root)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(rc._statusline_launch_args(binding, adapter, state_root=blocker), ())
            self.assertIn("statusline wiring skipped", err.getvalue())

    def test_new_server_launch_carries_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            seen: dict[str, tuple] = {}

            def start_new_server(target, *, cwd, command_argv, scope_unit, account_home, context=None):
                seen["argv"] = tuple(command_argv)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with mock.patch.object(rc, "_default_tmux_server_pid", return_value=None), mock.patch.object(
                rc, "_prepare_runtime_scope"
            ), mock.patch.object(rc, "_start_new_server_in_scope", side_effect=start_new_server):
                rc._start_runtime(binding, _StatuslineClaudeAdapter(binding), replace=False, state_root=root / "state")
        settings = str(root / "state" / "recovery" / "runtime-controller" / "quota" / "main-session-settings.json")
        self.assertIn("--settings", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--settings") + 1], settings)

    def test_claude_launch_preaccepts_bypass_dialog_in_user_settings(self) -> None:
        # T-4935: the bypass warning dialog is gated on USER settings only
        # (--settings/flagSettings does not count), so the consent key must land
        # in <account_home>/.claude/settings.json before the seat launches.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            with mock.patch.object(rc, "_default_tmux_server_pid", return_value=None), mock.patch.object(
                rc, "_prepare_runtime_scope"
            ), mock.patch.object(
                rc, "_start_new_server_in_scope",
                return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
            ):
                rc._start_runtime(binding, _StatuslineClaudeAdapter(binding), replace=False, state_root=root / "state")
            data = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIs(data["skipDangerousModePermissionPrompt"], True)

    def test_claude_launch_logs_when_bypass_consent_cannot_be_ensured(self) -> None:
        # T-4953: a False return (e.g. the controller cannot write another
        # account's ~/.claude) must be visible, not swallowed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            err = io.StringIO()
            with mock.patch.object(claude_adapter_module, "ensure_bypass_consent", return_value=False), mock.patch.object(
                rc, "_default_tmux_server_pid", return_value=None
            ), mock.patch.object(rc, "_prepare_runtime_scope"), mock.patch.object(
                rc, "_start_new_server_in_scope",
                return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
            ), contextlib.redirect_stderr(err):
                rc._start_runtime(binding, _StatuslineClaudeAdapter(binding), replace=False, state_root=root / "state")
        self.assertIn("bypass consent NOT ensured", err.getvalue())

    def test_respawn_launch_carries_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            calls: list[tuple] = []

            def run_tmux(*args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with mock.patch.object(rc, "_default_tmux_server_pid", return_value=123), mock.patch.object(
                rc, "_pid_in_scope", return_value=True
            ), mock.patch.object(rc, "_run_tmux", side_effect=run_tmux):
                rc._start_runtime(binding, _StatuslineClaudeAdapter(binding), replace=True, state_root=root / "state")
        respawn = [c for c in calls if c and c[0] == "respawn-pane"]
        self.assertEqual(len(respawn), 1)
        self.assertIn("--settings", respawn[0])


class RehydrateRecordedRuntimeSelectionTests(unittest.TestCase):
    """T-4157: regression coverage for the recorded-runtime REHYDRATE call shape.

    ``_rehydrate_recorded_runtime_selection`` called ``_verify_runtime_input_ready``
    with three positional arguments against a two-positional signature -- a
    ``TypeError`` on every rehydrate attempt, always raised AFTER ``_start_runtime``
    had already launched a pane, which the ``except`` handler then turned into a
    permanent ``recorded-runtime-rehydrate-failed`` viability block. This spy binds
    every call against the REAL function's signature via
    ``inspect.signature(...).bind()``, so the test fails on the exact call-shape
    mismatch itself (a ``TypeError`` from ``bind()``) rather than on an incidental
    exception from inside the real body.
    """

    def test_verify_runtime_input_ready_call_shape_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _binding(root, harness="claude")
            rc._atomic_json(
                rc._persona_state(binding.persona, root) / "runtime.json",
                rc._runtime_payload(binding, session_id="prior", started_at=rc._utc_now()),
            )

            adapter = _Adapter(binding)
            runtime_start = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            sentinel = RuntimeError("SENTINEL-rehydrate-probe")
            real_verify = rc._verify_runtime_input_ready
            bound_calls: list[inspect.BoundArguments] = []

            def spy(*args, **kwargs):
                bound = inspect.signature(real_verify).bind(*args, **kwargs)
                bound.apply_defaults()
                bound_calls.append(bound)
                raise sentinel

            with mock.patch.object(rc, "_recorded_scope_inactive", return_value=True), mock.patch.object(
                rc, "prove_managed_receiver_present", return_value=True
            ), mock.patch.object(
                rc, "prove_predecessor_receiver_absent", return_value=True
            ), mock.patch.object(
                rc,
                "_authenticate_recorded_selected_runtime",
                return_value=({"revision": "r"}, {"revision": "r"}, {"state": "present"}, "same-revision"),
            ), mock.patch.object(
                rc, "_start_runtime", return_value=runtime_start
            ), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(
                rc, "_verify_runtime_input_ready", side_effect=spy
            ):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as ctx:
                    rc._rehydrate_recorded_runtime_selection(
                        binding,
                        {"runtime_scope_unit": rc.TMUX_RUNTIME_SCOPE_UNIT},
                        adapter_factory=lambda selected: adapter,
                        state_root=root,
                        backend=None,
                        context=None,
                        now=rc._utc_now(),
                        manifest_order=(rc._runtime_ref(binding),),
                        attempt_limit=1,
                    )

            self.assertEqual(ctx.exception.reason, "recorded-runtime-rehydrate-failed")
            rehydrate_error = ctx.exception.evidence["rehydrate_error"]
            self.assertIn("SENTINEL-rehydrate-probe", rehydrate_error)
            self.assertNotIn("TypeError", rehydrate_error)

            self.assertEqual(len(bound_calls), 1)
            bound = bound_calls[0]
            self.assertIs(bound.arguments["binding"], binding)
            self.assertIs(bound.arguments["started"], runtime_start)
            self.assertIs(bound.arguments["adapter"], adapter)


class SubmitHelperTimeoutTests(unittest.TestCase):
    """T-4390: a hung tmux call inside send_keys_submit.sh must not hold a controller
    launch until TimeoutStartSec. The helper runs bounded (render wait + margin), a
    timeout is an unverified submit like a non-zero exit, and the kill takes the
    helper's whole process group, so no orphaned tmux client can press Enter later.
    These run a REAL fake helper (a bash script), not a mock of the seam."""

    def _fake_helper(self, root: Path, body: str) -> Path:
        script = root / "fake_send_keys_submit.sh"
        script.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def _handle(self, root: Path) -> rc.RuntimeHandle:
        binding = _binding(root)
        return rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())

    def _submit(self, root: Path, script: Path, *, margin: float = 0.5) -> None:
        with mock.patch.object(rc, "SEND_KEYS_HELPER", script), mock.patch.object(
            rc, "SUBMIT_HELPER_TIMEOUT_MARGIN_SECONDS", margin
        ):
            rc.submit_to_runtime(
                self._handle(root),
                ExecutionRequest(prompt="work", cwd=root),
                persona="recovery",
                state_root=root,
            )

    @staticmethod
    def _gone(pid: int, *, within: float = 5.0) -> bool:
        """True once ``pid`` no longer runs (absent, or a zombie awaiting its reaper)."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return True
            if state == "Z":
                return True
            time.sleep(0.05)
        return False

    def test_a_helper_that_hangs_is_an_unverified_submit_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self._fake_helper(root, "echo '[send_keys_submit] pasting' >&2\nsleep 30\n")
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as caught:
                self._submit(root, script)
            self.assertLess(time.monotonic() - started, 10)
            self.assertNotIsInstance(caught.exception, subprocess.TimeoutExpired)
            self.assertIn("timed out after 0.5s", str(caught.exception))
            # The partial stderr survives into the detail (it names where it hung).
            self.assertIn("[send_keys_submit] pasting", str(caught.exception))
            events = _read_jsonl(root / "recovery" / "runtime-controller" / "controller-evidence.jsonl")
            failed = [e for e in events if e["event"] == "runtime_submission_failed"]
            self.assertEqual(len(failed), 1)
            self.assertIsNone(failed[0]["returncode"])
            self.assertEqual(failed[0]["timeout_seconds"], 0.5)
            self.assertIn("timed out", failed[0]["detail"])
            self.assertNotIn("runtime_submission_accepted", [e["event"] for e in events])

    def test_the_timeout_kills_the_helpers_tmux_children_too(self) -> None:
        # A hung `tmux send-keys` is the helper's CHILD: killing only bash (what
        # subprocess.run(timeout=) does) would orphan it, still able to deliver.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pidfile = root / "grandchild.pid"
            script = self._fake_helper(
                root, f"sleep 300 &\necho $! > {shlex.quote(str(pidfile))}\nwait\n"
            )
            with self.assertRaises(RuntimeError):
                self._submit(root, script)
            grandchild = int(pidfile.read_text().strip())
            gone = self._gone(grandchild)
            if not gone:
                os.kill(grandchild, signal.SIGKILL)
            self.assertTrue(gone, "the helper's child survived the timeout")

    def test_a_helper_that_exits_in_time_is_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok = self._fake_helper(root, "exit 0\n")
            self._submit(root, ok, margin=30.0)
            bad = self._fake_helper(root, "echo 'submit unconfirmed' >&2\nexit 2\n")
            with self.assertRaisesRegex(RuntimeError, "^submit unconfirmed$"):
                self._submit(root, bad, margin=30.0)
            events = _read_jsonl(root / "recovery" / "runtime-controller" / "controller-evidence.jsonl")
            failed = [e for e in events if e["event"] == "runtime_submission_failed"]
            self.assertEqual([e["returncode"] for e in failed], [2])
            self.assertNotIn("timeout_seconds", failed[0])

    def test_the_bound_is_the_render_wait_plus_the_margin(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for render_wait, want in ((None, 60.0), (8.0, 68.0), (float("nan"), 60.0),
                                      (float("inf"), 60.0), (-5.0, 60.0)):
                with self.subTest(render_wait=render_wait), mock.patch.object(
                    rc, "_run_submit_helper", return_value=completed
                ) as run:
                    rc.submit_to_runtime(
                        self._handle(root),
                        ExecutionRequest(prompt="work", cwd=root),
                        render_wait_seconds=render_wait,
                    )
                self.assertEqual(run.call_args.kwargs["timeout"], want)

    def test_render_wait_is_passed_as_a_plain_decimal(self) -> None:
        # rev-t4371 r2 NIT-7: f"{x:g}" wrote exponents the helper's CLI refuses.
        for value, want in ((8.0, "8"), (0.5, "0.5"), (1e-05, "0.00001"),
                            (1e6, "1000000"), (2.25, "2.25"), (1e-07, "0")):
            with self.subTest(value=value):
                self.assertEqual(rc._plain_decimal_seconds(value), want)
                self.assertRegex(rc._plain_decimal_seconds(value), r"^[0-9]+([.][0-9]+)?$")
        # And the controller's argv uses it (not the formatter alone).
        completed = subprocess.CompletedProcess([], 0, "", "")
        binding = _binding(Path("/tmp"))
        handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now())
        with mock.patch.object(rc, "_run_submit_helper", return_value=completed) as run:
            rc.submit_to_runtime(handle, ExecutionRequest(prompt="work", cwd=Path("/tmp")),
                                 render_wait_seconds=1e-05)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--render-wait") + 1], "0.00001")



class SignalTargetGuardTests(unittest.TestCase):
    """T-5031: a mocked pid must never reach kill(). os.killpg(1) is kill(-1),
    which SIGKILLs every process of the account running the suite (a seat's
    systemd --user manager included). os.kill/os.killpg are patched in every
    test here, so even a regression cannot signal a real process."""

    def test_only_real_child_pids_are_signalable(self) -> None:
        for pid, want in ((4242, True), (2, True), (1, False), (0, False), (-1, False),
                          (True, False), (None, False), ("4242", False), (4242.0, False),
                          (mock.MagicMock(), False), (mock.Mock(), False)):
            with self.subTest(pid=pid):
                self.assertIs(rc._is_signalable_pid(pid), want)

    def test_a_mocked_popen_failure_never_kills_the_account(self) -> None:
        # The T-5031 shape: Popen patched with a MagicMock, so communicate()'s
        # result does not unpack and the except path runs with pid == MagicMock.
        with mock.patch.object(rc.subprocess, "Popen"), \
                mock.patch.object(rc.os, "killpg") as killpg:
            with self.assertRaises(Exception):
                rc._run_submit_helper(["/bin/true"], timeout=1.0)
        killpg.assert_not_called()

    def test_a_real_pid_still_gets_its_group_killed_on_timeout(self) -> None:
        process = mock.MagicMock()
        process.pid = 4242
        process.communicate.side_effect = subprocess.TimeoutExpired(["x"], 1.0)
        popen = mock.MagicMock()
        popen.return_value.__enter__.return_value = process
        with mock.patch.object(rc.subprocess, "Popen", popen), \
                mock.patch.object(rc.os, "killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                rc._run_submit_helper(["/bin/true"], timeout=1.0)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        process.wait.assert_called_once()

    def test_terminate_runtime_pid_refuses_a_non_child_pid(self) -> None:
        binding = _binding(Path("/tmp"), harness="codex")
        for pid in (1, 0, mock.MagicMock()):
            with self.subTest(pid=pid), \
                    mock.patch.object(rc, "_pane_process_pid", return_value=pid), \
                    mock.patch.object(rc.os, "kill") as kill:
                self.assertFalse(rc._terminate_runtime_pid(binding))
                kill.assert_not_called()
        with mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                mock.patch.object(rc.os, "kill") as kill:
            self.assertTrue(rc._terminate_runtime_pid(binding))
        kill.assert_called_once_with(4242, signal.SIGTERM)


# T-5023: the codex MCP-startup watch reads and clears a REAL tmux pane. Every
# test in this module defaults to a codex binding, so stub its two tmux seams
# module-wide (capture -> "pane gone", clear -> no-op) to keep the suite
# hermetic; CodexMcpStartupWatchTests below patch them explicitly.
_MCP_WATCH_MODULE_PATCHES: list = []
_REAL_CLEAR = rc._clear_runtime_pane_for_launch  # bound at import, before setUpModule


def setUpModule() -> None:
    for name, kwargs in (
        ("_capture_runtime_pane_history", {"return_value": None}),
        ("_clear_runtime_pane_for_launch", {}),
    ):
        patcher = mock.patch.object(rc, name, **kwargs)
        patcher.start()
        _MCP_WATCH_MODULE_PATCHES.append(patcher)


def tearDownModule() -> None:
    while _MCP_WATCH_MODULE_PATCHES:
        _MCP_WATCH_MODULE_PATCHES.pop().stop()


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


_MCP_LINE = (
    "⚠ MCP startup interrupted. The following servers were not initialized: "
    "codex_apps, sticker, telegram\n"
)


class CodexMcpStartupWatchTests(unittest.TestCase):
    """T-5023: detect codex's 'MCP startup interrupted' pane line after a codex
    launch, relaunch once, then alert; never for a claude binding."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = _FakeClock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- the poll loop ---------------------------------------------------

    def _wait(self, captures, *, baseline=0, window=60.0):
        it = iter(captures)
        last = [None]

        def capture(_target):
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        return rc._wait_for_codex_mcp_startup_interrupt(
            rc.TmuxTarget("s", "w", "0"), baseline=baseline,
            deadline=self.clock.monotonic() + window, capture=capture,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic,
        )

    def test_hit_returns_the_uninitialised_servers(self) -> None:
        self.assertEqual(
            self._wait(["banner\n", "banner\n" + _MCP_LINE]),
            "codex_apps, sticker, telegram",
        )

    def test_clean_window_returns_none_after_the_deadline(self) -> None:
        self.assertIsNone(self._wait(["banner\n›\n"]))
        self.assertGreaterEqual(self.clock.t, 1060.0)
        self.assertLess(self.clock.t, 1060.0 + rc.CODEX_MCP_STARTUP_POLL_SECONDS)

    def test_prior_session_lines_at_baseline_do_not_count(self) -> None:
        self.assertIsNone(self._wait([_MCP_LINE + "banner\n"], baseline=1))
        self.assertEqual(
            self._wait([_MCP_LINE + "banner\n", _MCP_LINE + "banner\n" + _MCP_LINE], baseline=1),
            "codex_apps, sticker, telegram",
        )

    def test_quoted_warning_in_pane_input_is_not_a_hit(self) -> None:
        # A mail or boot kick quoting the warning renders after the composer
        # glyph or mid-sentence; only codex's own line counts.
        quoted = (
            "banner\n"
            "› [mail] T-5023: watch for 'MCP startup interrupted. The following "
            "servers were not initialized: telegram'\n"
            "  see MCP startup interrupted. The following servers were not initialized: x\n"
        )
        self.assertIsNone(self._wait([quoted]))
        self.assertEqual(
            self._wait([quoted, quoted + "⚠ " + _MCP_LINE]),
            "codex_apps, sticker, telegram",
        )

    def test_pane_gone_ends_the_watch_immediately(self) -> None:
        self.assertIsNone(self._wait([None]))
        self.assertEqual(self.clock.t, 1000.0)

    def test_cut_off_server_list_still_counts_as_a_hit(self) -> None:
        self.assertEqual(
            self._wait(["MCP startup interrupted"]), "(server list not captured)"
        )

    # -- prepare (pre-launch clear + baseline) ----------------------------

    def test_prepare_is_a_no_op_for_claude(self) -> None:
        binding = _binding(self.root, harness="claude", profile="anthropic-code")
        with mock.patch.object(rc, "_target_known") as known, \
                mock.patch.object(rc, "_capture_runtime_pane_history") as capture:
            self.assertEqual(rc._prepare_codex_mcp_startup_watch(binding), 0)
        known.assert_not_called()
        capture.assert_not_called()

    def test_prepare_clears_a_known_codex_pane_then_counts(self) -> None:
        binding = _binding(self.root, harness="codex")
        with mock.patch.object(rc, "_target_known", return_value=True), \
                mock.patch.object(rc, "_clear_runtime_pane_for_launch") as clear, \
                mock.patch.object(rc, "_capture_runtime_pane_history", return_value=""):
            self.assertEqual(rc._prepare_codex_mcp_startup_watch(binding), 0)
        clear.assert_called_once()

    def test_prepare_never_raises(self) -> None:
        binding = _binding(self.root, harness="codex")
        with mock.patch.object(rc, "_target_known", side_effect=OSError("tmux gone")):
            self.assertEqual(rc._prepare_codex_mcp_startup_watch(binding), 0)

    def test_clear_resets_screen_and_history_on_the_target(self) -> None:
        with mock.patch.object(rc, "_run_tmux") as run:
            _REAL_CLEAR(rc.TmuxTarget("s", "w", "0"))
        self.assertEqual(
            [c.args[:2] for c in run.call_args_list],
            [("send-keys", "-R"), ("clear-history", "-t")],
        )

    # -- watch + bounded relaunch ----------------------------------------

    def _run_watch(self, binding, captures):
        it = iter(captures)
        state = {"last": None}

        def capture(_target, **_kw):
            try:
                state["last"] = next(it)
            except StopIteration:
                pass
            return state["last"]

        start = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        relaunched = rc.RuntimeStart("relaunched.scope", True)
        stack = contextlib.ExitStack()
        mocks = {
            name: stack.enter_context(mock.patch.object(rc, name, **kw))
            for name, kw in (
                ("_capture_runtime_pane_history", {"side_effect": capture}),
                ("_prepare_codex_mcp_startup_watch", {"return_value": 0}),
                ("_start_runtime", {"return_value": relaunched}),
                ("_verify_started_runtime", {}),
                ("_verify_runtime_input_ready", {}),
                ("_run_and_assess_post_start", {}),
                ("_route_failure_alert", {}),
            )
        }
        with stack:
            result = rc._watch_codex_mcp_startup_and_relaunch(
                binding, object(), start, launched_at=self.clock.monotonic(), baseline=0,
                state_root=self.root, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
            )
        return result, start, relaunched, mocks

    def _events(self) -> list[str]:
        path = rc._evidence_log_path("recovery", self.root)
        if not path.exists():
            return []
        return [str(r["event"]) for r in _read_jsonl(path) if str(r["event"]).startswith("codex_mcp")]

    def test_claude_binding_is_never_watched(self) -> None:
        binding = _binding(self.root, harness="claude", profile="anthropic-code")
        result, start, _relaunched, mocks = self._run_watch(binding, [_MCP_LINE])
        self.assertIs(result, start)
        mocks["_capture_runtime_pane_history"].assert_not_called()
        mocks["_start_runtime"].assert_not_called()

    def test_clean_codex_start_is_not_relaunched(self) -> None:
        result, start, _relaunched, mocks = self._run_watch(_binding(self.root), ["banner\n"])
        self.assertIs(result, start)
        mocks["_start_runtime"].assert_not_called()
        self.assertEqual(self._events(), [])

    def test_hit_relaunches_once_and_records_recovery(self) -> None:
        result, _start, relaunched, mocks = self._run_watch(
            _binding(self.root), ["banner\n" + _MCP_LINE, "banner\n"]
        )
        self.assertIs(result, relaunched)
        mocks["_start_runtime"].assert_called_once()
        self.assertIs(mocks["_start_runtime"].call_args.kwargs["replace"], True)
        mocks["_verify_runtime_input_ready"].assert_called_once()
        mocks["_run_and_assess_post_start"].assert_called_once()
        mocks["_prepare_codex_mcp_startup_watch"].assert_called_once()
        mocks["_route_failure_alert"].assert_not_called()
        self.assertEqual(
            self._events(), ["codex_mcp_startup_interrupted", "codex_mcp_startup_recovered"]
        )

    def test_repeat_hit_is_bounded_then_alerts_and_keeps_running(self) -> None:
        result, _start, relaunched, mocks = self._run_watch(
            _binding(self.root), ["banner\n" + _MCP_LINE]
        )
        self.assertIs(result, relaunched)
        self.assertEqual(mocks["_start_runtime"].call_count, rc.CODEX_MCP_STARTUP_MAX_RELAUNCHES)
        mocks["_route_failure_alert"].assert_called_once()
        self.assertIn("codex_apps, sticker, telegram", mocks["_route_failure_alert"].call_args.args[1])
        self.assertEqual(
            self._events(),
            ["codex_mcp_startup_interrupted", "codex_mcp_startup_interrupted", "codex_mcp_startup_alert"],
        )

    def test_controller_start_runs_the_watch_after_the_receiver_start(self) -> None:
        from tools import persona_bringup

        binding = _binding(self.root, harness="codex")
        order: list[str] = []
        start = rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
        with mock.patch.object(persona_bringup, "build_adapter", return_value=_Adapter(binding)), \
                mock.patch.object(rc, "_runtime_alive", return_value=False), \
                mock.patch.object(rc, "_target_known", return_value=False), \
                mock.patch.object(rc, "_start_runtime", return_value=start), \
                mock.patch.object(rc, "_verify_started_runtime"), \
                mock.patch.object(rc, "_verify_runtime_input_ready"), \
                mock.patch.object(rc, "_run_and_assess_post_start"), \
                mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                mock.patch.object(rc, "_read_pane_cmdline", return_value=None), \
                mock.patch.object(rc, "_prepare_codex_mcp_startup_watch", return_value=0), \
                mock.patch.object(
                    rc, "_start_selected_receiver_after_start",
                    side_effect=lambda *a, **k: order.append("receiver"),
                ), \
                mock.patch.object(
                    rc, "_watch_codex_mcp_startup_and_relaunch",
                    side_effect=lambda b, a, rs, **k: (order.append("watch"), rs)[1],
                ) as watch:
            runtime_start, _handle = rc._controller_start_runtime(binding, state_root=self.root)
        self.assertIs(runtime_start, start)
        self.assertEqual(order, ["receiver", "watch"])
        self.assertEqual(watch.call_args.kwargs["baseline"], 0)


class SuperviseUnknownLivenessT5168Tests(unittest.TestCase):
    """T-5168: a tmux error in the supervise loop is UNKNOWN = alive, never an exit."""

    def _run(self, reads):
        seq = list(reads)

        def alive_check() -> bool:
            value = seq.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _stateless_binding(root)
            with mock.patch.object(rc, "_route_failure_alert") as alert:
                rc.block_until_tmux_death(binding, alive_check=alive_check, sleep=lambda _s: None,
                                          state_root=root)
            path = rc._persona_state(binding.persona, root) / "runtime-controller" / "controller-evidence.jsonl"
            if not path.exists():
                path = next(root.rglob("controller-evidence.jsonl"), path)
            events = [json.loads(l)["event"] for l in path.read_text().splitlines()] if path.exists() else []
        return seq, alert, events

    def test_a_persistent_error_keeps_supervising_and_escalates_once(self) -> None:
        err = RuntimeError("tmux pane probe failed (rc 1): Permission denied")
        n = rc.RUNTIME_ALIVE_UNKNOWN_ESCALATE_READS
        left, alert, events = self._run([True] + [err] * (n * 2) + [True, False])
        self.assertEqual(left, [])  # the loop ran to the real death, it did not raise
        alert.assert_called_once()
        self.assertIn("liveness probe has failed", alert.call_args.args[1])
        self.assertEqual(events, ["runtime_liveness_unknown", "runtime_liveness_unknown_escalated",
                                  "runtime_liveness_known_again"])

    def test_a_short_error_burst_records_but_does_not_alert(self) -> None:
        err = RuntimeError("tmux pane probe failed (rc 1): transient")
        left, alert, events = self._run([True, err, err, True, False])
        self.assertEqual(left, [])
        alert.assert_not_called()
        self.assertEqual(events, ["runtime_liveness_unknown", "runtime_liveness_known_again"])

    def test_wait_for_live_runtime_reads_a_tmux_error_as_not_yet(self) -> None:
        target = rc.TmuxTarget("seat", "runtime", "0")
        with mock.patch.object(rc, "pane_exists", side_effect=[RuntimeError("tmux denied"), True]), \
                mock.patch.object(rc, "_default_tmux_server_pid", return_value=42), \
                mock.patch.object(rc, "_pid_in_scope", return_value=True), \
                mock.patch.object(rc.time, "sleep"):
            self.assertTrue(rc._wait_for_live_runtime(target, rc.TMUX_RUNTIME_SCOPE_UNIT, timeout=30))


class CompensateCreatedScopeT5167Tests(unittest.TestCase):
    """T-5167: compensate_failed_initial_candidate stops only a scope this attempt created."""

    def _compensate(self, created_scope):
        with tempfile.TemporaryDirectory() as tmp:
            binding = _binding(Path(tmp), harness="codex")
            failure = rc.RuntimeOperationalFailure(
                "runtime started verification failed",
                failure_class="operational-launch",
                cause="runtime_started_verification_failed",
                binding_runtime=rc._runtime_ref(binding),
                runtime_start=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, created_scope),
                created_scope=created_scope,
                pane_may_be_mutated=True,
                readiness_evidence={"event": "runtime_started_verification_failed"},
            )
            with mock.patch.object(rc, "_stop_created_scope") as stop, \
                    mock.patch.object(rc, "_candidate_receiver_window_or_unknown", return_value=None), \
                    mock.patch.object(rc, "_carried_receiver_window", return_value=None), \
                    mock.patch.object(rc, "_prove_receiver"):
                teardown = rc.compensate_failed_initial_candidate(
                    binding, failure=failure, state_root=Path(tmp), reason=failure.cause)
        return teardown, stop

    def test_a_reused_scope_is_not_stopped(self) -> None:
        teardown, stop = self._compensate(False)
        stop.assert_not_called()
        self.assertEqual(teardown["scope_stopped"], "false")
        self.assertEqual(teardown["scope_stop_skipped"], "reused-existing-scope")
        self.assertEqual(teardown["runtime_state_cleared"], "true")

    def test_a_created_scope_is_still_stopped(self) -> None:
        teardown, stop = self._compensate(True)
        stop.assert_called_once_with(rc.TMUX_RUNTIME_SCOPE_UNIT)
        self.assertEqual(teardown["scope_stopped"], "true")
        self.assertNotIn("scope_stop_skipped", teardown)


if __name__ == "__main__":
    unittest.main()
