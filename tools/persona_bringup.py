#!/usr/bin/env python3
"""Harness-neutral persona diagnosis and headless bring-up.

The persona/role comes from ``instance.yaml``.  The harness can be overridden
explicitly for recovery, which lets a quota-capped Claude persona run a bounded
turn on Codex without rewriting the durable instance binding.

Examples:

    python3 tools/persona_bringup.py diagnose --persona recovery --harness codex
    printf '%s\n' 'Report your readiness.' |
      python3 tools/persona_bringup.py run --persona recovery --harness codex --ephemeral

Provider-specific execution stays in ``tools.harness_adapter``.  This module
owns only persona binding, role orientation, diagnostics, and result rendering.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import hashlib
import inspect
import json
import os
import pwd
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.harness_adapter import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    DiagnosticObservation,
    DiagnosticReport,
    ExecutionRequest,
    HarnessRunner,
)
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.types import HarnessLaunchConfig, RuntimeControllerConfig
from tools.harness_adapter.diagnostics import (
    BasicHarnessDiagnosticProbe,
    resolve_controller_config,
    resolve_launch_config,
)
from tools.harness_adapter.delivery import record_submitted
from tools.harness_adapter.quota import (
    FailoverPolicy,
    PrimaryHealthProbe,
    QuotaSnapshot,
    QuotaWindow,
    decide_failover,
    decide_fallback_recovery,
)
from tools import codex_telegram_bridge
from tools import mailbox as pantheon_mailbox
from tools import mailbox_wake
from tools.persona_brief import PersonaBriefError, resolve_persona_brief_path
from tools import runtime_controller
from tools.claude_quota_probe import (
    ClaudeQuotaProbeError,
    PROMPT as CLAUDE_QUOTA_PROBE_PROMPT,
    ProbeResult as ClaudeQuotaProbeResult,
    probe_result_path as claude_probe_result_path,
    probe_session_id as claude_probe_session_id,
    probe_tmux_target as claude_probe_tmux_target,
    read_probe_result as read_claude_probe_result,
    run_probe as run_claude_quota_probe,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "instance.yaml"
DEFAULT_MAILBOX_HUB = Path("/home/the-ancestor/mailbox")
DEFAULT_QUOTA_CHECK_PERSONAS = frozenset({"specialist", "recovery"})
QUOTA_CHECK_LOCK_TIMEOUT_SECONDS = 0.0
BACKOFF_DELAYS_SECONDS = (120, 300, 900, 3600, 14400, 21600)

# Cry-wolf discriminator for the consume timer's soft exits 3/4/5 (T-2586).
# Each consume fire that ends in a soft/backoff-handled failure bumps a
# consecutive-failure streak; a genuine consume success clears it. Only once the
# streak reaches this threshold does consume emit EXIT_SOFT_PERSISTENT (a HARD
# code the unit does NOT list in SuccessExitStatus), so OnFailure=unit-failure
# pages Recovery for a *sustained* failure rather than on every transient blip. The
# soft codes (3/4/5) are marked SuccessExitStatus on the unit, so an isolated /
# self-healing fire (the measured cry-wolf: fail then recover next fire, streak
# never reaching the threshold) does not page. Deliberately a separate state
# file from the backoff record so exit-3 (diagnostic fail, which never touches
# backoff) does not gain a spurious consume retry-delay.
#
# T-3209 split: the streak above is now tracked as TWO independent counters
# because a single shared streak leaks across an unrelated skip class. A seat
# stuck in a policy-skip loop (RuntimeViabilityBlocked / RuntimeTransitionPending
# / ProviderQuotaExhausted at the wake/consume call — none of which bump or
# clear) never reaches the full-success clear below, so on such a seat any
# isolated exit-3 diagnostic blip (e.g. a ~7s dangling-symlink window during a
# claude auto-update) is permanent: it can never be undone by a later success
# that never comes, and the NEXT unrelated blip pages on top of it. The gate
# stage (diagnostics, run unconditionally every fire before command dispatch)
# and the attempt stage (the actual consume/wake call) are therefore split:
#   - gate-stage streak (`*_diag_streak`, exit-3 class): cleared the moment
#     diagnostics next pass, regardless of what the attempt stage does next --
#     "the stage that failed has recovered" is answerable independent of
#     downstream outcome.
#   - attempt-stage streak (`*_soft_streak`, exit-4/5 class): unchanged --
#     cleared only by a genuine consume/wake success, since a policy-skip is
#     not evidence the attempt stage would have succeeded.
SOFT_FAILURE_ALERT_STREAK = 3
EXIT_SOFT_PERSISTENT = 6


class HarnessTurnTransientError(RuntimeError):
    """Retryable harness/provider failure that is not mailbox poison."""

    mailbox_transient = True


@dataclass(frozen=True)
class PersonaBinding:
    role: str
    persona: str
    account: str
    configured_harness: str
    selected_harness: str
    account_home: Path
    pinned_rules: Path
    mailbox: Path
    launch_cwd: Path
    launch: HarnessLaunchConfig
    controller: RuntimeControllerConfig
    communications: Mapping[str, Any] | None = None
    standing_instructions: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class QuotaCheckResult:
    persona: str
    supported: bool
    decision: Literal[
        "unsupported",
        "unknown",
        "stay",
        "planned-transition-started",
        "transition-active-noop",
        "fallback-active-noop",
    ]
    acted: bool
    reason: str
    quota_freshness: str | None = None
    weekly_remaining_percent: float | None = None
    transition_phase: str | None = None
    evidence: Mapping[str, str] = dataclasses.field(default_factory=dict)


def normalize_harness(value: str) -> str:
    try:
        return harness_integration.normalize_harness(value)
    except ValueError as exc:
        raise ValueError(f"unsupported harness {value!r}") from exc


def _communications_binding(data: Mapping[str, Any], *, persona: str, account: str) -> Mapping[str, Any] | None:
    installer = data.get("persona_installer")
    bridge = installer.get("codex_telegram_bridge") if isinstance(installer, Mapping) else None
    personas = bridge.get("personas") if isinstance(bridge, Mapping) else None
    raw = personas.get(persona) if isinstance(personas, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    if raw.get("account") != account:
        raise ValueError("communications account guard mismatch")
    topology = raw.get("claude_receiver_topology")
    if topology not in {"runtime-owned", "systemd-user"}:
        raise ValueError("communications claude_receiver_topology must be runtime-owned or systemd-user")
    predecessor_topology = raw.get("predecessor_claude_receiver_topology", topology)
    if predecessor_topology not in {"runtime-owned", "systemd-user"}:
        raise ValueError("communications predecessor_claude_receiver_topology must be runtime-owned or systemd-user")
    tmux_pane = raw.get("tmux_pane")
    if not isinstance(tmux_pane, str) or not codex_telegram_bridge.TMUX_PANE_RE.fullmatch(tmux_pane):
        raise ValueError("communications tmux_pane must be an explicit window.pane target")
    return {
        "kind": "telegram",
        "claude_receiver_topology": topology,
        "predecessor_claude_receiver_topology": predecessor_topology,
        "claude_receiver_unit": str(raw["claude_receiver_unit"]),
        "codex_receiver_unit": str(raw["codex_receiver_unit"]),
        "tmux_session": str(raw["tmux_session"]),
        "tmux_pane": tmux_pane,
        "watchdog_units": tuple(str(item) for item in raw.get("watchdog_units", ())),
    }


def _standing_instruction_reference(
    data: Mapping[str, Any],
    *,
    role: str,
    persona: str,
    selected_harness: str,
    account_home: Path,
) -> Mapping[str, str] | None:
    del role, selected_harness
    installer = data.get("persona_installer")
    raw = installer.get("codex_standing_instructions") if isinstance(installer, Mapping) else None
    personas = raw.get("personas") if isinstance(raw, Mapping) else None
    item = personas.get(persona) if isinstance(personas, Mapping) else None
    if not isinstance(item, Mapping):
        return None
    target = item.get("target")
    override = item.get("override_target")
    source = item.get("source")
    source_sha = item.get("source_sha256")
    if not all(isinstance(value, str) and value for value in (target, override, source, source_sha)):
        raise ValueError("codex standing instruction source/target fields must be non-empty strings")
    return {
        "schema": str(raw.get("schema", "")),
        "source": source,
        "source_sha256": source_sha,
        "target": str(account_home / target),
        "override_target": str(account_home / override),
        "override_must_be_absent": str(item.get("override_must_be_absent") is True).lower(),
    }


def load_binding(
    manifest_path: Path,
    persona: str,
    *,
    harness_override: str | None = None,
    executable_override: Path | None = None,
    model_override: str | None = None,
    profile_override: str | None = None,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
) -> PersonaBinding:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, dict):
        raise ValueError(f"{manifest_path}: roles mapping missing")

    for role, raw_binding in roles.items():
        if not isinstance(raw_binding, dict) or raw_binding.get("persona") != persona:
            continue
        account = raw_binding.get("account")
        configured_harness = raw_binding.get("harness")
        if not isinstance(account, str) or not account:
            raise ValueError(f"{manifest_path}: {role}.account missing")
        if not isinstance(configured_harness, str) or not configured_harness:
            raise ValueError(f"{manifest_path}: {role}.harness missing")
        selected = normalize_harness(harness_override or configured_harness)
        try:
            account_home = Path(pwd.getpwnam(account).pw_dir)
        except KeyError:
            account_home = Path("/home") / account
        launch = resolve_launch_config(
            data,
            role=str(role),
            account_home=account_home,
            selected_harness=selected,
            executable_override=executable_override,
            model_override=model_override,
            profile_override=profile_override,
            bound_persona=persona,
            bound_mailbox=mailbox_hub / persona,
            mailbox_hub=mailbox_hub,
        )
        controller = resolve_controller_config(
            data,
            persona=persona,
            role=str(role),
        )
        try:
            pinned_rules = resolve_persona_brief_path(
                repo_root=REPO_ROOT,
                account_home=account_home,
                persona=persona,
                persona_brief_ref=raw_binding.get("persona_brief_ref"),
            )
        except PersonaBriefError as exc:
            raise ValueError(f"{manifest_path}: {role}.persona_brief_ref invalid: {exc}") from exc
        return PersonaBinding(
            role=str(role),
            persona=persona,
            account=account,
            configured_harness=normalize_harness(configured_harness),
            selected_harness=selected,
            account_home=account_home,
            pinned_rules=pinned_rules,
            mailbox=mailbox_hub / persona,
            launch_cwd=manifest_path.parent.resolve(),
            launch=launch,
            controller=controller,
            communications=_communications_binding(data, persona=persona, account=account),
            standing_instructions=_standing_instruction_reference(
                data,
                role=str(role),
                persona=persona,
                selected_harness=selected,
                account_home=account_home,
            ),
        )
    raise ValueError(f"{manifest_path}: persona {persona!r} is not bound")


def quota_check_personas(manifest_path: Path) -> frozenset[str]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    installer = data.get("persona_installer") if isinstance(data, dict) else None
    quota = installer.get("quota_check") if isinstance(installer, dict) else None
    personas = quota.get("personas") if isinstance(quota, dict) else None
    if personas is None:
        return DEFAULT_QUOTA_CHECK_PERSONAS
    if not (
        isinstance(personas, list)
        and all(isinstance(item, str) and item for item in personas)
    ):
        raise ValueError("persona_installer.quota_check.personas must be a list of persona strings")
    return frozenset(personas)


def quota_check_supported(
    binding: PersonaBinding,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[bool, str]:
    if binding.persona not in quota_check_personas(manifest_path):
        return False, "persona-not-allowlisted"
    if not binding.controller.policy.fallbacks:
        return False, "no-manifest-fallback"
    return True, "supported"


def build_adapter(
    binding: PersonaBinding,
    *,
    harness_home: Path,
):
    return harness_integration.integration_for(binding.selected_harness).build_adapter(
        binding,
        harness_home=harness_home,
    )


def runtime_reference(binding: PersonaBinding) -> str:
    return f"{binding.launch.harness}/{binding.launch.profile}"


def load_runtime_reference(
    manifest_path: Path,
    binding: PersonaBinding,
    reference: str,
) -> PersonaBinding:
    """Resolve one validated harness/profile reference for the same persona."""

    try:
        harness, profile = reference.split("/", 1)
    except ValueError as exc:
        raise ValueError(f"runtime reference {reference!r} must be harness/profile") from exc
    return load_binding(
        manifest_path,
        binding.persona,
        harness_override=harness,
        profile_override=profile,
        mailbox_hub=binding.mailbox.parent,
    )


def first_fallback_binding(
    manifest_path: Path,
    binding: PersonaBinding,
) -> PersonaBinding | None:
    """Return the first configured fallback distinct from the active runtime."""

    active = runtime_reference(binding)
    for reference in binding.controller.policy.fallbacks:
        target = load_runtime_reference(manifest_path, binding, reference)
        if runtime_reference(target) != active:
            return target
    return None


def configured_runtime_bindings(
    manifest_path: Path,
    binding: PersonaBinding,
) -> tuple[PersonaBinding, ...]:
    """Return the primary plus declared fallbacks in manifest order."""

    result: list[PersonaBinding] = [binding]
    seen = {runtime_reference(binding)}
    for reference in binding.controller.policy.fallbacks:
        target = load_runtime_reference(manifest_path, binding, reference)
        resolved = runtime_reference(target)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(target)
    return tuple(result)


def configured_runtime_targets(manifest_path: Path, persona: str) -> tuple[str, ...]:
    binding = load_binding(manifest_path, persona)
    return tuple(runtime_reference(item) for item in configured_runtime_bindings(manifest_path, binding))


def resolve_configured_runtime_target(
    manifest_path: Path,
    *,
    persona: str,
    destination: str,
    state_root: Path | None = None,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
) -> tuple[PersonaBinding, PersonaBinding]:
    primary = load_binding(manifest_path, persona, mailbox_hub=mailbox_hub)
    configured = (runtime_reference(primary), *primary.controller.policy.fallbacks)
    if destination not in configured:
        raise ValueError(f"destination {destination!r} is not configured for {persona}")
    current = select_recorded_runtime_binding(manifest_path, primary, state_dir=runtime_controller._state_root(state_root))
    if destination == runtime_reference(primary):
        target = primary
    else:
        target = load_runtime_reference(manifest_path, primary, destination)
    return current, target


def operator_target_swap(
    *,
    manifest_path: Path,
    persona: str,
    destination: str,
    trigger: Literal["operator"],
    correlation_id: str,
    state_root: Path,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
    context: runtime_controller.TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> runtime_controller.OperatorSwapReceipt:
    current, target = resolve_configured_runtime_target(
        manifest_path,
        persona=persona,
        destination=destination,
        state_root=state_root,
        mailbox_hub=mailbox_hub,
    )
    actor = pwd.getpwuid(os.geteuid()).pw_name
    if actor != current.account:
        raise ValueError("operator target principal does not match manifest account")
    existing = runtime_controller.transition_state(persona, state_root=state_root)
    if existing.get("phase") == "compensation-failed":
        raise runtime_controller.RuntimeTransitionPending(
            f"{persona} runtime transition compensation failed; manual recovery required"
        )
    matching_existing = (
        existing.get("trigger") == trigger
        and existing.get("correlation_id") == correlation_id
        and existing.get("destination") == destination
        and existing.get("phase") in {"preparing", "awaiting-successor-read", "completed"}
    )
    if destination == runtime_reference(current) and not matching_existing:
        runtime_controller._validate_operator_active_noop(
            current,
            existing,
            state_root=state_root,
            context=context,
            backend=backend,
        )
        runtime_controller._append_controller_evidence(
            persona,
            "operator_target_already_active",
            {
                "correlation_id": correlation_id,
                "destination": destination,
                "active_runtime": runtime_reference(current),
            },
            state_root=state_root,
        )
        return runtime_controller.OperatorSwapReceipt(
            schema="pantheon-runtime-operator-target-swap/v1",
            persona=persona,
            trigger=trigger,
            correlation_id=correlation_id,
            destination=destination,
            status="already-active",
            phase=str(existing.get("phase", "")),
            source_runtime=runtime_reference(current),
            source_transition_id=str(existing.get("transition_id", "")),
            outgoing_harness=current.launch.harness,
            outgoing_profile=current.launch.profile,
            incoming_harness=current.launch.harness,
            incoming_profile=current.launch.profile,
            receiver_transferred=False,
        )
    handle = None if matching_existing else runtime_controller.recorded_runtime_handle(current, state_root=state_root)
    try:
        return runtime_controller._operator_target_swap_resolved(
            current,
            target,
            trigger=trigger,
            correlation_id=correlation_id,
            state_root=state_root,
            context=context,
            current_handle=handle,
            backend=backend,
        )
    except runtime_controller.RuntimeTransitionPending:
        state = runtime_controller.transition_state(persona, state_root=state_root)
        if (
            state.get("phase") == "awaiting-successor-read"
            and state.get("trigger") == trigger
            and state.get("correlation_id") == correlation_id
            and state.get("destination") == destination
        ):
            return runtime_controller._operator_receipt(target, state, status="pending")
        raise


def operator_compensation_recovery(
    *,
    manifest_path: Path,
    persona: str,
    source: str,
    destination: str,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    reason: str,
    state_root: Path,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
    context: runtime_controller.TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> runtime_controller.OperatorCompensationRecoveryReceipt:
    primary = load_binding(manifest_path, persona, mailbox_hub=mailbox_hub)
    configured = (runtime_reference(primary), *primary.controller.policy.fallbacks)
    if source not in configured:
        raise ValueError(f"source {source!r} is not configured for {persona}")
    if destination not in configured:
        raise ValueError(f"destination {destination!r} is not configured for {persona}")
    source_binding = (
        primary
        if source == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, source)
    )
    target_binding = (
        primary
        if destination == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, destination)
    )
    actor = pwd.getpwuid(os.geteuid()).pw_name
    if actor != source_binding.account:
        raise ValueError("operator recovery principal does not match manifest account")
    return runtime_controller.recover_operator_compensation_failed(
        source_binding,
        target_binding,
        recovery_id=recovery_id,
        expected_transition_id=expected_transition_id,
        expected_transition_sha256=expected_transition_sha256,
        state_root=state_root,
        reason=reason,
        context=context,
        backend=backend,
    )


def operator_compensation_failed_target_live_recovery(
    *,
    manifest_path: Path,
    persona: str,
    source: str,
    destination: str,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    expected_predecessor_sha256: str,
    correlation_id: str,
    reason: str,
    state_root: Path,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
    context: runtime_controller.TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> runtime_controller.OperatorCompensationTargetLiveRecoveryReceipt:
    primary = load_binding(manifest_path, persona, mailbox_hub=mailbox_hub)
    configured = (runtime_reference(primary), *primary.controller.policy.fallbacks)
    if source not in configured:
        raise ValueError(f"source {source!r} is not configured for {persona}")
    if destination not in configured:
        raise ValueError(f"destination {destination!r} is not configured for {persona}")
    source_binding = (
        primary
        if source == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, source)
    )
    target_binding = (
        primary
        if destination == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, destination)
    )
    actor = pwd.getpwuid(os.geteuid()).pw_name
    if actor != source_binding.account:
        raise ValueError("operator recovery principal does not match manifest account")
    return runtime_controller.recover_operator_compensation_failed_target_live(
        source_binding,
        target_binding,
        recovery_id=recovery_id,
        expected_transition_id=expected_transition_id,
        expected_transition_sha256=expected_transition_sha256,
        expected_predecessor_sha256=expected_predecessor_sha256,
        correlation_id=correlation_id,
        state_root=state_root,
        reason=reason,
        context=context,
        backend=backend,
    )


def operator_started_unverified_recovery(
    *,
    manifest_path: Path,
    persona: str,
    source: str,
    destination: str,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    correlation_id: str,
    reason: str,
    state_root: Path,
    mailbox_hub: Path = DEFAULT_MAILBOX_HUB,
    context: runtime_controller.TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> runtime_controller.OperatorStartedUnverifiedRecoveryReceipt:
    primary = load_binding(manifest_path, persona, mailbox_hub=mailbox_hub)
    configured = (runtime_reference(primary), *primary.controller.policy.fallbacks)
    if source not in configured:
        raise ValueError(f"source {source!r} is not configured for {persona}")
    if destination not in configured:
        raise ValueError(f"destination {destination!r} is not configured for {persona}")
    source_binding = (
        primary
        if source == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, source)
    )
    target_binding = (
        primary
        if destination == runtime_reference(primary)
        else load_runtime_reference(manifest_path, primary, destination)
    )
    actor = pwd.getpwuid(os.geteuid()).pw_name
    if actor != source_binding.account:
        raise ValueError("operator recovery principal does not match manifest account")
    return runtime_controller.recover_operator_started_unverified(
        source_binding,
        target_binding,
        recovery_id=recovery_id,
        expected_transition_id=expected_transition_id,
        expected_transition_sha256=expected_transition_sha256,
        correlation_id=correlation_id,
        state_root=state_root,
        reason=reason,
        context=context,
        backend=backend,
    )


def select_recorded_runtime_binding(
    manifest_path: Path,
    binding: PersonaBinding,
    *,
    state_dir: Path,
) -> PersonaBinding:
    """Keep a swapped fallback active until an explicit recovery decision."""

    state = runtime_controller.transition_state(
        binding.persona,
        state_root=state_dir,
    )
    phase = state.get("phase")
    if phase in {"preparing", "recovered"}:
        harness = state.get("outgoing_harness")
        profile = state.get("outgoing_profile")
    elif phase in {"awaiting-successor-read", "completed"} and state.get(
        "runtime_swapped"
    ) is True:
        harness = state.get("incoming_harness")
        profile = state.get("incoming_profile")
    else:
        return binding
    if not isinstance(harness, str) or not isinstance(profile, str):
        raise ValueError("runtime transition state is missing selected harness/profile")
    reference = f"{harness}/{profile}"
    if reference == runtime_reference(binding):
        return binding
    allowed = set(binding.controller.policy.fallbacks)
    if reference not in allowed:
        raise ValueError(
            f"recorded fallback {reference!r} is no longer allowed by the manifest"
        )
    return load_runtime_reference(manifest_path, binding, reference)


def primary_recovery_evidence(
    primary: PersonaBinding,
    *,
    state_dir: Path,
    now: datetime | None = None,
    claude_probe_runner=run_claude_quota_probe,
) -> dict[str, str] | None:
    """Return fresh reset+health proof before a fallback may return to primary."""

    state = runtime_controller.transition_state(primary.persona, state_root=state_dir)
    if not (
        state.get("phase") == "completed"
        and state.get("fallback_active") is True
        # T-2584: forced-cap now records a schedule-computed
        # primary_resets_at at swap time (runtime_controller.forced_swap),
        # so it can return through the same reset+health gate as a planned
        # failover once that instant passes -- everything downstream of
        # this check already only reads reset+health, not the trigger.
        and state.get("trigger") in ("planned-quota", "forced-cap")
    ):
        return None
    raw_reset = state.get("primary_resets_at")
    if not isinstance(raw_reset, str):
        return None
    try:
        reset = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
    except ValueError:
        return None
    if reset.tzinfo is None:
        return None
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    observed_now = observed_now.astimezone(timezone.utc)
    if observed_now < reset.astimezone(timezone.utc):
        return None

    probe_result: ClaudeQuotaProbeResult | None = None
    if primary.selected_harness == "claude":
        # One attempt per half-hour bucket bounds subscription turns while
        # allowing a failed/reset-delayed provider to be rechecked later.
        bucket = observed_now.replace(
            minute=(observed_now.minute // 30) * 30,
            second=0,
            microsecond=0,
        )
        probe_run_id = bucket.strftime("%Y%m%dT%H%M%SZ")
        result_path = claude_probe_result_path(
            state_dir, primary.persona, probe_run_id
        )
        failure_path = result_path.with_name("failure.json")
        probe_result = read_claude_probe_result(result_path)
        if probe_result is None and failure_path.exists():
            return None
        if probe_result is None:
            try:
                probe_result = claude_probe_runner(
                    account=primary.account,
                    persona=primary.persona,
                    executable=primary.launch.executable,
                    repo_root=REPO_ROOT,
                    state_root=state_dir,
                    run_id=probe_run_id,
                )
            except ClaudeQuotaProbeError as exc:
                _atomic_write_json(
                    failure_path,
                    {
                        "schema": "pantheon-claude-quota-probe-failure/v1",
                        "run_id": probe_run_id,
                        "account": primary.account,
                        "persona": primary.persona,
                        "attempted_at": observed_now.isoformat().replace("+00:00", "Z"),
                        "error": str(exc)[:1000],
                    },
                )
                return None
        if (
            probe_result.status != "observed"
            or probe_result.account != primary.account
            or probe_result.persona != primary.persona
            or probe_result.tmux_target
            != claude_probe_tmux_target(primary.persona, probe_run_id)
            or probe_result.session_id
            != claude_probe_session_id(
                primary.account, primary.persona, probe_run_id
            )
            or probe_result.prompt_sha256
            != hashlib.sha256(CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")).hexdigest()
            or probe_result.pong_observed is not True
            or probe_result.cap_sentinel is not None
        ):
            return None
    adapter = build_adapter(primary, harness_home=primary.account_home)
    fresh = adapter.quota_observer(state_dir / primary.persona).read()
    if probe_result is not None:
        observed_session = fresh.evidence.get("session_id")
        if (
            fresh.evidence.get("path") != probe_result.statusline_path
            or fresh.evidence.get("sha256") != probe_result.statusline_sha256
            or observed_session != probe_result.session_id
        ):
            return None
    policy = FailoverPolicy(
        weekly_remaining_percent=primary.controller.policy.weekly_remaining_percent,
        fallbacks=primary.controller.policy.fallbacks,
    )
    healthy = (
        decide_failover(fresh, policy) == "stay"
        and str(fresh.evidence.get("authoritative_limit", "false")).lower()
        != "true"
    )
    probe = PrimaryHealthProbe(
        healthy=healthy,
        observed_at=fresh.observed_at,
        freshness=fresh.freshness,
        evidence=dict(fresh.evidence),
    )
    recorded_reset = QuotaSnapshot(
        profile=runtime_reference(primary),
        windows=(
            QuotaWindow(
                name="weekly",
                used_percent=0.0,
                remaining_percent=0.0,
                resets_at=reset,
            ),
        ),
        observed_at=reset,
        freshness="fresh",
        evidence={"source": "persisted-transition-reset"},
    )
    if (
        decide_fallback_recovery(recorded_reset, probe, now=observed_now)
        != "return-to-primary"
    ):
        return None
    weekly_remaining = _weekly_remaining_percent(fresh)
    return {
        "primary_profile": runtime_reference(primary),
        "reset_passed_at": raw_reset,
        "probe_observed_at": fresh.observed_at.isoformat().replace("+00:00", "Z"),
        "probe_freshness": fresh.freshness,
        **(
            {"probe_weekly_remaining_percent": str(weekly_remaining)}
            if weekly_remaining is not None
            else {}
        ),
        **{f"probe_{key}": str(value) for key, value in fresh.evidence.items()},
        **(
            {
                "probe_run_id": probe_result.run_id,
                "probe_result_path": str(
                    claude_probe_result_path(
                        state_dir, primary.persona, probe_result.run_id
                    )
                ),
                "probe_result_sha256": hashlib.sha256(
                    claude_probe_result_path(
                        state_dir, primary.persona, probe_result.run_id
                    ).read_bytes()
                ).hexdigest(),
                "probe_pong_observed": str(probe_result.pong_observed).lower(),
            }
            if probe_result is not None
            else {}
        ),
    }


def _weekly_remaining_percent(snapshot: QuotaSnapshot) -> float | None:
    for window in snapshot.windows:
        if window.name.strip().lower().replace(" ", "-") in {
            "week",
            "weekly",
            "weekly-quota",
            "weekly_remaining",
            "weekly-remaining",
            "7d",
            "7-day",
            "seven-day",
        } and isfinite(float(window.remaining_percent)):
            return float(window.remaining_percent)
    return None


def _weekly_reset_at(snapshot: QuotaSnapshot) -> str | None:
    for window in snapshot.windows:
        if window.name.strip().lower().replace(" ", "-") in {
            "week",
            "weekly",
            "weekly-quota",
            "weekly_remaining",
            "weekly-remaining",
            "7d",
            "7-day",
            "seven-day",
        }:
            return window.resets_at.isoformat().replace("+00:00", "Z")
    return None


def refresh_primary_quota(
    binding: PersonaBinding,
    *,
    state_dir: Path,
    now: datetime | None = None,
    claude_probe_runner=run_claude_quota_probe,
    adapter=None,
) -> QuotaSnapshot | None:
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    observed_now = observed_now.astimezone(timezone.utc)
    probe_result: ClaudeQuotaProbeResult | None = None
    if binding.selected_harness == "claude":
        bucket = observed_now.replace(
            minute=(observed_now.minute // 30) * 30,
            second=0,
            microsecond=0,
        )
        probe_run_id = bucket.strftime("%Y%m%dT%H%M%SZ")
        result_path = claude_probe_result_path(state_dir, binding.persona, probe_run_id)
        failure_path = result_path.with_name("failure.json")
        probe_result = read_claude_probe_result(result_path)
        if probe_result is None and failure_path.exists():
            return None
        if probe_result is None:
            try:
                probe_result = claude_probe_runner(
                    account=binding.account,
                    persona=binding.persona,
                    executable=binding.launch.executable,
                    repo_root=REPO_ROOT,
                    state_root=state_dir,
                    run_id=probe_run_id,
                )
            except ClaudeQuotaProbeError as exc:
                _atomic_write_json(
                    failure_path,
                    {
                        "schema": "pantheon-claude-quota-probe-failure/v1",
                        "run_id": probe_run_id,
                        "account": binding.account,
                        "persona": binding.persona,
                        "attempted_at": observed_now.isoformat().replace("+00:00", "Z"),
                        "error": str(exc)[:1000],
                    },
                )
                return None
        if (
            probe_result.status != "observed"
            or probe_result.account != binding.account
            or probe_result.persona != binding.persona
            or probe_result.tmux_target
            != claude_probe_tmux_target(binding.persona, probe_run_id)
            or probe_result.session_id
            != claude_probe_session_id(binding.account, binding.persona, probe_run_id)
            or probe_result.prompt_sha256
            != hashlib.sha256(CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")).hexdigest()
            or probe_result.pong_observed is not True
            or probe_result.cap_sentinel is not None
        ):
            return None
    quota_adapter = adapter or build_adapter(binding, harness_home=binding.account_home)
    fresh = quota_adapter.quota_observer(state_dir / binding.persona).read()
    if probe_result is not None:
        observed_session = fresh.evidence.get("session_id")
        if (
            fresh.evidence.get("path") != probe_result.statusline_path
            or fresh.evidence.get("sha256") != probe_result.statusline_sha256
            or observed_session != probe_result.session_id
        ):
            return None
    return fresh


def evaluate_planned_quota_swap(
    binding: PersonaBinding,
    quota_snapshot: QuotaSnapshot,
    *,
    fallback_binding: PersonaBinding | None,
    state_dir: Path,
) -> tuple[str, float | None, str | None]:
    del state_dir
    decision = decide_failover(
        quota_snapshot,
        FailoverPolicy(
            weekly_remaining_percent=binding.controller.policy.weekly_remaining_percent,
            fallbacks=binding.controller.policy.fallbacks,
        ),
    )
    weekly_remaining = _weekly_remaining_percent(quota_snapshot)
    if decision == "planned-failover" and fallback_binding is None:
        return "unknown", weekly_remaining, None
    return decision, weekly_remaining, _weekly_reset_at(quota_snapshot)


def quota_check(
    binding: PersonaBinding,
    *,
    adapter,
    fallback_binding: PersonaBinding | None,
    primary_binding: PersonaBinding,
    state_dir: Path,
    cwd: Path,
    now: datetime | None = None,
    claude_probe_runner=run_claude_quota_probe,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> QuotaCheckResult:
    del cwd
    supported, support_reason = quota_check_supported(binding, manifest_path=manifest_path)
    if not supported:
        return QuotaCheckResult(
            persona=binding.persona,
            supported=False,
            decision="unsupported",
            acted=False,
            reason=support_reason,
        )
    try:
        with runtime_controller.persona_operation_lock(
            binding.persona,
            state_root=state_dir,
            blocking=False,
            timeout_seconds=QUOTA_CHECK_LOCK_TIMEOUT_SECONDS,
        ):
            transition = runtime_controller.transition_state(
                binding.persona,
                state_root=state_dir,
            )
            phase = transition.get("phase")
            if phase in {"preparing", "awaiting-successor-read"}:
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="transition-active-noop",
                    acted=False,
                    reason="transition-active",
                    transition_phase=str(phase),
                )
            if phase == "completed" and transition.get("fallback_active") is True:
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="fallback-active-noop",
                    acted=False,
                    reason="fallback-active",
                    transition_phase="completed",
                )
            if runtime_reference(binding) != runtime_reference(primary_binding):
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="fallback-active-noop",
                    acted=False,
                    reason="selected-runtime-is-fallback",
                    transition_phase=str(phase) if phase else None,
                )
            fresh = refresh_primary_quota(
                primary_binding,
                state_dir=state_dir,
                now=now,
                claude_probe_runner=claude_probe_runner,
                adapter=adapter,
            )
            if fresh is None:
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="unknown",
                    acted=False,
                    reason="probe-unavailable",
                )
            decision, weekly_remaining, weekly_reset = evaluate_planned_quota_swap(
                binding,
                fresh,
                fallback_binding=fallback_binding,
                state_dir=state_dir,
            )
            if decision == "unknown":
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="unknown",
                    acted=False,
                    reason="unknown-quota-evidence",
                    quota_freshness=fresh.freshness,
                    weekly_remaining_percent=weekly_remaining,
                    evidence=dict(fresh.evidence),
                )
            if decision == "stay":
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="stay",
                    acted=False,
                    reason="above-reserve",
                    quota_freshness=fresh.freshness,
                    weekly_remaining_percent=weekly_remaining,
                    evidence=dict(fresh.evidence),
                )
            if fallback_binding is None:
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="unknown",
                    acted=False,
                    reason="no-fallback",
                    quota_freshness=fresh.freshness,
                    weekly_remaining_percent=weekly_remaining,
                    evidence=dict(fresh.evidence),
                )
            message_id, _ = _oldest_pending_identity(binding.mailbox)
            barrier = runtime_controller.begin_planned_transition(
                binding.persona,
                outgoing_harness=binding.launch.harness,
                outgoing_profile=binding.launch.profile,
                incoming_harness=fallback_binding.launch.harness,
                incoming_profile=fallback_binding.launch.profile,
                message_id=message_id,
                primary_resets_at=weekly_reset,
                quota_evidence=dict(fresh.evidence),
                state_root=state_dir,
            )
            if barrier.status == "started":
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="planned-transition-started",
                    acted=True,
                    reason="reserve-threshold",
                    quota_freshness=fresh.freshness,
                    weekly_remaining_percent=weekly_remaining,
                    transition_phase=barrier.phase,
                    evidence=dict(fresh.evidence),
                )
            if barrier.status in {"already-active", "already-completed"}:
                return QuotaCheckResult(
                    persona=binding.persona,
                    supported=True,
                    decision="transition-active-noop",
                    acted=False,
                    reason=barrier.status,
                    quota_freshness=fresh.freshness,
                    weekly_remaining_percent=weekly_remaining,
                    transition_phase=barrier.phase,
                    evidence=dict(fresh.evidence),
                )
            return QuotaCheckResult(
                persona=binding.persona,
                supported=True,
                decision="unknown",
                acted=False,
                reason="transition-conflict",
                quota_freshness=fresh.freshness,
                weekly_remaining_percent=weekly_remaining,
                transition_phase=barrier.phase,
                evidence=dict(fresh.evidence),
            )
    except runtime_controller.OperationLockBusy:
        return QuotaCheckResult(
            persona=binding.persona,
            supported=True,
            decision="unknown",
            acted=False,
            reason="operation-busy",
        )


def diagnose_persona(
    binding: PersonaBinding,
    *,
    harness_home: Path,
    cwd: Path,
    execution_context: str,
) -> DiagnosticReport:
    adapter = build_adapter(binding, harness_home=harness_home)
    basic = BasicHarnessDiagnosticProbe(
        adapter,
        cwd=cwd,
        execution_context=execution_context,
    ).collect()
    mailbox_usable = (
        binding.mailbox.is_dir()
        and (binding.mailbox / "new").is_dir()
        and os.access(binding.mailbox / "new", os.R_OK | os.W_OK | os.X_OK)
    )
    consume_lock = binding.mailbox / ".consumer.lock"
    consume_lease_usable = (
        os.access(consume_lock, os.R_OK | os.W_OK)
        if consume_lock.exists()
        else mailbox_usable
        and os.access(binding.mailbox, os.W_OK | os.X_OK)
    )
    prompt_exists = binding.pinned_rules.is_file() and os.access(
        binding.pinned_rules, os.R_OK
    )
    current_account = getpass.getuser()
    observations = (
        DiagnosticObservation(
            check_id="persona.manifest_binding",
            status="pass",
            summary=f"{binding.persona} resolves to role {binding.role}",
            authoritative=True,
            evidence={
                "persona": binding.persona,
                "role": binding.role,
                "account": binding.account,
            },
        ),
        DiagnosticObservation(
            check_id="persona.harness_selection",
            status=(
                "pass"
                if binding.selected_harness == binding.configured_harness
                else "warn"
            ),
            summary=(
                "using configured harness"
                if binding.selected_harness == binding.configured_harness
                else "using explicit recovery harness override"
            ),
            authoritative=True,
            evidence={
                "configured": binding.configured_harness,
                "selected": binding.selected_harness,
            },
        ),
        DiagnosticObservation(
            check_id="persona.role_prompt",
            status="pass" if prompt_exists else "fail",
            summary="tracked persona brief readable" if prompt_exists else "persona brief missing",
            authoritative=True,
            evidence={"path": str(binding.pinned_rules)},
        ),
        DiagnosticObservation(
            check_id="persona.mailbox",
            status="pass" if mailbox_usable else "fail",
            summary="persona mailbox is readable and writable" if mailbox_usable else "persona mailbox unavailable",
            authoritative=True,
            evidence={"path": str(binding.mailbox)},
        ),
        DiagnosticObservation(
            check_id="persona.consume_lease",
            status="pass" if consume_lease_usable else "fail",
            summary=(
                "mailbox consumer lease is usable"
                if consume_lease_usable
                else "mailbox consumer lease is not writable"
            ),
            authoritative=True,
            evidence={
                "path": str(consume_lock),
                "exists": str(consume_lock.exists()).lower(),
            },
        ),
        DiagnosticObservation(
            check_id="persona.execution_account",
            status="pass" if current_account == binding.account else "warn",
            summary=(
                "running on bound Unix account"
                if current_account == binding.account
                else "running on a temporary recovery host account"
            ),
            authoritative=True,
            evidence={
                "bound_account": binding.account,
                "execution_account": current_account,
            },
        ),
    )
    return DiagnosticReport(
        harness=basic.harness,
        execution_context=execution_context,
        observations=observations + basic.observations,
    )


def orientation_prompt(binding: PersonaBinding, task_prompt: str) -> str:
    return f"""Pantheon persona binding

You are {binding.persona}, the persona bound to role {binding.role}.
The selected execution harness is {binding.selected_harness}; the durable
instance binding normally selects {binding.configured_harness}.  Harness is an
implementation detail, not your identity.  Do not call yourself Codex or claim
to be the separate `codex` persona.

Read and follow the tracked persona brief at:
{binding.pinned_rules}

Your durable inbound mailbox is:
{binding.mailbox}

This invocation is a bounded recovery turn.  Preserve role scope and attribution.
Do not push, alter live services, or consume mailbox work unless the task below
explicitly asks you to.

Task:
{task_prompt}
"""


def continuation_prompt(binding: PersonaBinding, task_prompt: str) -> str:
    return f"""Pantheon persona continuation

You remain {binding.persona}, role {binding.role}, currently executed through
the {binding.selected_harness} harness. Preserve that persona identity and role
scope while handling this next durable mailbox task:

{task_prompt}
"""


def _state_path(state_dir: Path, persona: str) -> Path:
    return state_dir / f"{persona}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_session_id(state_dir: Path, persona: str) -> str | None:
    path = _state_path(state_dir, persona)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = raw.get("session_id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def save_session_id(state_dir: Path, binding: PersonaBinding, session_id: str) -> None:
    payload = {
        "persona": binding.persona,
        "role": binding.role,
        "harness": binding.selected_harness,
        "session_id": session_id,
    }
    _atomic_write_json(_state_path(state_dir, binding.persona), payload)


def _backoff_path(state_dir: Path, persona: str) -> Path:
    return state_dir / f"{persona}.backoff.json"


def load_backoff(state_dir: Path, persona: str) -> dict[str, Any]:
    try:
        raw = json.loads(_backoff_path(state_dir, persona).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def backoff_remaining(
    state_dir: Path,
    persona: str,
    *,
    now: float | None = None,
) -> float:
    state = load_backoff(state_dir, persona)
    next_retry = state.get("next_retry_epoch")
    if not isinstance(next_retry, (int, float)):
        return 0.0
    current = time.time() if now is None else now
    return max(0.0, float(next_retry) - current)


def record_backoff_failure(
    state_dir: Path,
    persona: str,
    error: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    fingerprint = hashlib.sha256(error.encode("utf-8")).hexdigest()
    previous = load_backoff(state_dir, persona)
    attempts = (
        int(previous.get("attempts", 0)) + 1
        if previous.get("fingerprint") == fingerprint
        else 1
    )
    delay = BACKOFF_DELAYS_SECONDS[min(attempts - 1, len(BACKOFF_DELAYS_SECONDS) - 1)]
    state = {
        "attempts": attempts,
        "fingerprint": fingerprint,
        "last_error": error[:1000],
        "next_retry_epoch": current + delay,
        "delay_seconds": delay,
    }
    _atomic_write_json(_backoff_path(state_dir, persona), state)
    return state


def clear_backoff(state_dir: Path, persona: str) -> None:
    try:
        _backoff_path(state_dir, persona).unlink()
    except FileNotFoundError:
        pass


def _soft_streak_path(state_dir: Path, persona: str) -> Path:
    return state_dir / f"{persona}.softstreak.json"


def load_soft_streak(state_dir: Path, persona: str) -> int:
    try:
        raw = json.loads(_soft_streak_path(state_dir, persona).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    count = raw.get("count") if isinstance(raw, dict) else None
    return count if isinstance(count, int) and count >= 0 else 0


def bump_soft_streak(state_dir: Path, persona: str) -> int:
    """Increment and persist the consecutive attempt-stage soft-failure streak;
    return the new count.

    Counts consecutive consume fires that ended in an attempt-stage soft
    failure (exit 4/5: an OSError transport failure or consume finishing with
    errors) — the diagnostic gate stage (exit 3) is tracked separately by
    bump_diag_streak (T-3209), since a policy-skip (RuntimeViabilityBlocked /
    RuntimeTransitionPending / ProviderQuotaExhausted) is not evidence this
    stage would have succeeded and must not clear it. Cleared by
    clear_soft_streak only on a genuine consume/wake success.
    """
    count = load_soft_streak(state_dir, persona) + 1
    _atomic_write_json(_soft_streak_path(state_dir, persona), {"count": count})
    return count


def clear_soft_streak(state_dir: Path, persona: str) -> None:
    try:
        _soft_streak_path(state_dir, persona).unlink()
    except FileNotFoundError:
        pass


def _diag_streak_path(state_dir: Path, persona: str) -> Path:
    return state_dir / f"{persona}.diagstreak.json"


def load_diag_streak(state_dir: Path, persona: str) -> int:
    try:
        raw = json.loads(_diag_streak_path(state_dir, persona).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    count = raw.get("count") if isinstance(raw, dict) else None
    return count if isinstance(count, int) and count >= 0 else 0


def bump_diag_streak(state_dir: Path, persona: str) -> int:
    """Increment and persist the consecutive gate-stage (diagnostic, exit-3)
    soft-failure streak; return the new count.

    Independent of bump_soft_streak's attempt-stage counter (T-3209): the
    diagnostics stage runs unconditionally every consume fire, before command
    dispatch, so whether it passed THIS fire is answerable regardless of what
    the attempt stage does next. Cleared by clear_diag_streak the moment
    diagnostics next pass, not only on a full consume/wake success.
    """
    count = load_diag_streak(state_dir, persona) + 1
    _atomic_write_json(_diag_streak_path(state_dir, persona), {"count": count})
    return count


def clear_diag_streak(state_dir: Path, persona: str) -> None:
    try:
        _diag_streak_path(state_dir, persona).unlink()
    except FileNotFoundError:
        pass


def _policy_skip_path(state_dir: Path, persona: str) -> Path:
    return state_dir / f"{persona}.policyskip.json"


def load_policy_skip(state_dir: Path, persona: str) -> dict[str, Any]:
    """Read the consecutive policy-skip streak for *persona* -- {reason,
    count, since} -- or {} if absent/corrupt (T-3210: the visibility gap
    T-3209 left unaddressed). A "policy skip" is an attempt-stage fire that
    never actually ran a wake/consume attempt: backoff, or one of the
    runtime-controller policy exceptions (RuntimeTransitionPending /
    ProviderQuotaExhausted / RuntimeViabilityBlocked). This is a THIRD,
    independent axis from bump_diag_streak (gate-stage/exit-3) and
    bump_soft_streak (attempt-stage genuine failure/exit-4/5): a policy skip
    is neither a diagnostic failure nor a transport failure, it is the
    attempt stage never being reached at all -- structurally invisible to
    both of the other two counters, which is exactly the gap this makes
    visible."""
    try:
        raw = json.loads(_policy_skip_path(state_dir, persona).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def bump_policy_skip(state_dir: Path, persona: str, reason: str) -> dict[str, Any]:
    """Increment and persist *persona*'s consecutive policy-skip streak,
    keyed by *reason*; return the new state ({"reason", "count", "since"}).

    A reason CHANGE resets the streak to 1 with a fresh `since`: the count
    answers "how long has THIS specific skip reason been firing", not "how
    long has SOME skip reason been firing" -- a seat that flips between
    runtime_transition and runtime_viability_blocked is telling two
    different stories, not a continuous one."""
    previous = load_policy_skip(state_dir, persona)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if previous.get("reason") == reason and isinstance(previous.get("count"), int):
        count = previous["count"] + 1
        since = previous.get("since") or now
    else:
        count = 1
        since = now
    state = {"reason": reason, "count": count, "since": since}
    _atomic_write_json(_policy_skip_path(state_dir, persona), state)
    return state


def clear_policy_skip(state_dir: Path, persona: str) -> None:
    try:
        _policy_skip_path(state_dir, persona).unlink()
    except FileNotFoundError:
        pass


def _record_task(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
        return json.dumps(payload, indent=2, sort_keys=True)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True)


def _result_cache_path(state_dir: Path, persona: str, record_id: str) -> Path:
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
    return state_dir / "completed" / persona / f"{digest}.json"


def _load_cached_result(
    state_dir: Path,
    persona: str,
    record_id: str,
) -> dict[str, str] | None:
    try:
        raw = json.loads(
            _result_cache_path(state_dir, persona, record_id).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("record_id") != record_id:
        return None
    required = ("record_id", "correlation_id", "status", "message", "session_id")
    if not all(isinstance(raw.get(key), str) for key in required):
        return None
    return {key: str(raw[key]) for key in required}


def _save_cached_result(
    state_dir: Path,
    persona: str,
    outcome: dict[str, str],
) -> None:
    _atomic_write_json(
        _result_cache_path(state_dir, persona, outcome["record_id"]),
        outcome,
    )


def _reply_inbox(record: dict[str, Any], reply_hub: Path) -> Path | None:
    payload = record.get("payload")
    if isinstance(payload, dict):
        route = payload.get("reply_route")
        if isinstance(route, dict):
            raw_inbox = route.get("recipient_inbox")
            if isinstance(raw_inbox, str) and raw_inbox:
                candidate = Path(raw_inbox)
                if candidate.is_absolute():
                    return candidate
    sender = record.get("sender")
    if isinstance(sender, str) and sender:
        candidate = reply_hub / sender
        if pantheon_mailbox.exists(candidate):
            return candidate
    return None


def process_mail_record(
    record: dict[str, Any],
    binding: PersonaBinding,
    *,
    adapter,
    runner: HarnessRunner,
    state_dir: Path,
    cwd: Path,
    reply_hub: Path,
) -> dict[str, str]:
    """Run one durable mailbox record and send a correlated result.

    A raised exception leaves the source message unacknowledged under the
    mailbox module's at-least-once contract.
    """

    record_id = str(record.get("id", "unknown"))
    payload = record.get("payload")
    correlation_id = (
        str(payload.get("correlation_id"))
        if isinstance(payload, dict) and payload.get("correlation_id")
        else record_id
    )
    cached = _load_cached_result(state_dir, binding.persona, record_id)
    submitted_state = "cached" if cached is not None else "durably_queued"
    if cached is None:
        session_id = load_session_id(state_dir, binding.persona)
        task = _record_task(record)
        prompt = (
            continuation_prompt(binding, task)
            if session_id
            else orientation_prompt(binding, task)
        )
        request = ExecutionRequest(
            prompt=prompt,
            cwd=cwd,
            model=binding.launch.model,
            session_id=session_id,
            ephemeral=False,
        )

        def _on_event(event) -> None:
            nonlocal submitted_state
            receipt = record_submitted(
                binding.mailbox,
                message_id=record_id,
                correlation_id=correlation_id,
                event=event,
            )
            if receipt.state == "submitted":
                submitted_state = "submitted"

        try:
            supports_events = "on_event" in inspect.signature(runner.run).parameters
        except (TypeError, ValueError):
            supports_events = False
        result = (
            runner.run(adapter, request, on_event=_on_event)
            if supports_events
            else runner.run(adapter, request)
        )
        if result.status != "completed":
            detail = result.error.message if result.error else result.status
            if result.error is not None and result.error.retryable:
                raise HarnessTurnTransientError(
                    f"{binding.persona} turn failed: {detail}"
                )
            raise RuntimeError(f"{binding.persona} turn failed: {detail}")
        result_session_id = result.session.session_id if result.session else ""
        cached = {
            "record_id": record_id,
            "correlation_id": correlation_id,
            "status": result.status,
            "message": result.final_message or "",
            "session_id": result_session_id,
        }
        _save_cached_result(state_dir, binding.persona, cached)
    if cached["session_id"]:
        save_session_id(state_dir, binding, cached["session_id"])

    reply_inbox = _reply_inbox(record, reply_hub)
    reply_id = ""
    if reply_inbox is not None:
        reply_id = pantheon_mailbox.send(
            str(reply_inbox),
            {
                "kind": "persona_result",
                "correlation_id": cached["correlation_id"],
                "persona": binding.persona,
                "role": binding.role,
                "harness": binding.selected_harness,
                "status": cached["status"],
                "message": cached["message"],
                "session_id": cached["session_id"] or None,
            },
            binding.persona,
            subject=f"result:{cached['correlation_id']}",
        )
    return {
        "record_id": record_id,
        "correlation_id": cached["correlation_id"],
        "reply_id": reply_id,
        "status": cached["status"],
        "delivery_state": submitted_state,
    }


def consume_persona_mailbox(
    binding: PersonaBinding,
    *,
    adapter,
    runner: HarnessRunner,
    state_dir: Path,
    cwd: Path,
    reply_hub: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    outcomes: list[dict[str, str]] = []
    # mailbox.consume claims the entire visible batch before calling handlers.
    # A transition started by one handler therefore has to bar the rest of this
    # exact drain explicitly; merely stopping future claims is too late.
    drain_id = runtime_controller.new_drain_id()

    def _handler(record: dict[str, Any]) -> None:
        with runtime_controller.persona_operation_lock(
            binding.persona,
            state_root=state_dir,
            blocking=True,
        ):
            runtime_controller.assert_drain_allowed(
                binding.persona,
                drain_id,
                state_root=state_dir,
            )
            outcomes.append(
                process_mail_record(
                    record,
                    binding,
                    adapter=adapter,
                    runner=runner,
                    state_dir=state_dir,
                    cwd=cwd,
                    reply_hub=reply_hub,
                )
            )

    stats = pantheon_mailbox.consume(str(binding.mailbox), _handler)
    return stats, outcomes


def wake_persistent_runtime(
    binding: PersonaBinding,
    *,
    adapter,
    fallback_binding: PersonaBinding | None = None,
    primary_binding: PersonaBinding | None = None,
    runtime_candidates: Sequence[PersonaBinding] | None = None,
    receiver_backend: Any | None = None,
    recovery_evidence: dict[str, str] | None = None,
    state_dir: Path,
    cwd: Path,
    now: float | None = None,
) -> dict[str, Any]:
    with runtime_controller.persona_operation_lock(
        binding.persona,
        state_root=state_dir,
        blocking=True,
    ):
        return _wake_persistent_runtime_locked(
            binding,
            adapter=adapter,
            fallback_binding=fallback_binding,
            primary_binding=primary_binding,
            runtime_candidates=runtime_candidates,
            receiver_backend=receiver_backend,
            recovery_evidence=recovery_evidence,
            state_dir=state_dir,
            cwd=cwd,
            now=now,
        )


def _wake_persistent_runtime_locked(
    binding: PersonaBinding,
    *,
    adapter,
    fallback_binding: PersonaBinding | None = None,
    primary_binding: PersonaBinding | None = None,
    runtime_candidates: Sequence[PersonaBinding] | None = None,
    receiver_backend: Any | None = None,
    recovery_evidence: dict[str, str] | None = None,
    state_dir: Path,
    cwd: Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Ensure the interactive TUI and nudge it without claiming mailbox work.

    The persona running inside the TUI remains the only mailbox consumer.  A
    successful tmux submission is therefore reported as transport acceptance,
    never as a started turn, completed turn, or mailbox acknowledgement.
    """

    drain_id = runtime_controller.new_drain_id()
    transition = runtime_controller.transition_state(
        binding.persona,
        state_root=state_dir,
    )
    if transition.get("phase") == "preparing":
        trigger = transition.get("trigger")
        if trigger not in {"planned-quota", "quota-reset"}:
            raise runtime_controller.RuntimeTransitionPending(
                f"{binding.persona} runtime transition is preparing"
            )
        target = fallback_binding if trigger == "planned-quota" else primary_binding
        if target is None:
            raise runtime_controller.RuntimeTransitionPending(
                "cooperative transition has no manifest-authorized target"
            )
        handle = runtime_controller.ensure_runtime(
            binding,
            adapter,
            state_root=state_dir,
        )
        message_id, _ = _oldest_pending_identity(binding.mailbox)
        runtime_controller.planned_swap(
            handle,
            target,
            message_id=message_id,
            drain_id=drain_id,
            trigger=trigger,
            state_root=state_dir,
        )

    candidates = tuple(runtime_candidates or (binding, *(item for item in (fallback_binding,) if item is not None)))

    def _adapter_for(candidate: PersonaBinding):
        if runtime_reference(candidate) == runtime_reference(binding):
            return adapter
        return build_adapter(candidate, harness_home=candidate.account_home)

    selection = runtime_controller.ensure_viable_runtime(
        candidates,
        adapter_factory=_adapter_for,
        state_root=state_dir,
        backend=receiver_backend,
    )
    binding = selection.binding
    adapter = selection.adapter
    handle = selection.handle
    quota_snapshot = adapter.quota_observer(state_dir / binding.persona).read()

    runtime_controller.assert_drain_allowed(
        binding.persona,
        drain_id,
        state_root=state_dir,
    )
    if primary_binding is not None and recovery_evidence is not None:
        message_id, _ = _oldest_pending_identity(binding.mailbox)
        runtime_controller.planned_swap(
            handle,
            primary_binding,
            message_id=message_id,
            drain_id=drain_id,
            quota_evidence=recovery_evidence,
            trigger="quota-reset",
            state_root=state_dir,
        )
    quota_decision = decide_failover(
        quota_snapshot,
        FailoverPolicy(
            weekly_remaining_percent=binding.controller.policy.weekly_remaining_percent,
            fallbacks=binding.controller.policy.fallbacks,
        ),
    )
    target = runtime_controller.runtime_target_text(handle.tmux)
    box = binding.mailbox
    new_dir = box / "new"
    marker = box / ".wake_marker"
    observed_at = time.time() if now is None else now
    current_session = mailbox_wake.session_key(target)
    pane_idle = mailbox_wake.pane_is_idle(target)
    action, names, reason = mailbox_wake.decide(
        new_dir,
        marker,
        current_session,
        observed_at,
        pane_idle,
    )
    result: dict[str, Any] = {
        "action": action,
        "reason": reason,
        "unread": len(names),
        "tmux_target": target,
        "profile": binding.launch.profile,
        "harness": binding.launch.harness,
        "runtime_viability": selection.receipt,
        "mailbox_claimed": False,
        "acked": False,
        "quota_decision": quota_decision,
        "quota_freshness": quota_snapshot.freshness,
    }
    if action != "wake":
        return result

    # T-2681/T-2730: a CONFIRMED-BUSY pane (mid-turn, generating) cannot accept a
    # submit — send_keys_submit's Enter never confirms, submit_to_runtime raises
    # RuntimeError, and the consume unit records a FALSE failure (the seat is healthy,
    # just busy, and drains its own mailbox when the turn ends). mailbox_wake.run()
    # already DEFERS on exactly this condition (its T-2681 guard, mailbox_wake.py); this
    # path computed pane_idle above but never acted on it. Mirror the guard: DEFER
    # without injecting and WITHOUT advancing the marker, so the next consume fire
    # retries once the pane goes idle (a newer message / condition-5 idle-rekick
    # re-evaluates to "wake"). Only a CONFIRMED-busy pane (False) defers; None
    # (unknown / foreign substrate / unreadable) and True (idle) fall through and inject
    # as before — identical to the waker's None-never-blocks contract.
    if pane_idle is False:
        result["action"] = "deferred"
        result["reason"] = "pane busy (mid-turn); deferring to next consume fire (T-2681)"
        return result

    prompt = mailbox_wake.build_persona_kick(
        binding.persona,
        names,
        mailbox_wake._senders(new_dir, names),
        mailbox_wake._queued_subjects(new_dir, names),
        inbox=box,
        tools_dir=binding.account_home / "pantheon-config" / "tools",
        bare_group=binding.launch.harness == "codex",
    )
    message_id, correlation_id = _oldest_pending_identity(binding.mailbox)
    receipt = runtime_controller.submit_to_runtime(
        handle,
        ExecutionRequest(
            prompt=prompt,
            cwd=cwd,
            model=binding.launch.model,
            session_id=handle.session_id,
        ),
        persona=binding.persona,
        state_root=state_dir,
        message_id=message_id,
        correlation_id=correlation_id,
    )
    mailbox_wake._write_marker(
        marker,
        names[-1],
        current_session,
        observed_at,
        idle_kicked=reason == mailbox_wake.IDLE_REKICK_REASON,
    )
    result["delivery"] = receipt
    return result


def _oldest_pending_identity(inbox: Path) -> tuple[str | None, str | None]:
    """Read, but never claim, the oldest durable mailbox identity."""

    try:
        candidates = sorted(path for path in (inbox / "new").iterdir() if path.is_file())
    except OSError:
        return None, None
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        message_id = str(record.get("id") or path.name)
        payload = record.get("payload")
        correlation = (
            str(payload.get("correlation_id"))
            if isinstance(payload, dict) and payload.get("correlation_id")
            else message_id
        )
        return message_id, correlation
    return None, None


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), indent=2, sort_keys=True))


def _common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--persona", required=True)
    parser.add_argument(
        "--harness",
        choices=("claude", "claude-code", "codex"),
        default=os.environ.get("PANTHEON_HARNESS_OVERRIDE"),
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("PANTHEON_RUNTIME_PROFILE_OVERRIDE"),
    )
    parser.add_argument(
        "--executable",
        type=Path,
        default=(
            Path(os.environ["PANTHEON_HARNESS_EXECUTABLE_OVERRIDE"])
            if os.environ.get("PANTHEON_HARNESS_EXECUTABLE_OVERRIDE")
            else None
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PANTHEON_MODEL_OVERRIDE"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(os.environ.get("PANTHEON_INSTANCE_MANIFEST", DEFAULT_MANIFEST)),
    )
    parser.add_argument(
        "--harness-home",
        type=Path,
        default=Path(os.environ.get("PANTHEON_HARNESS_HOME", Path.home())),
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path(os.environ.get("PANTHEON_PERSONA_CWD", REPO_ROOT)),
    )
    parser.add_argument(
        "--mailbox-hub",
        type=Path,
        default=Path(os.environ.get("PANTHEON_MAILBOX_HUB", DEFAULT_MAILBOX_HUB)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnose_parser = subparsers.add_parser("diagnose")
    _common_parser(diagnose_parser)

    run_parser = subparsers.add_parser("run")
    _common_parser(run_parser)
    run_parser.add_argument("--prompt")
    run_parser.add_argument("--prompt-file", type=Path)
    run_parser.add_argument("--ephemeral", action="store_true")
    run_parser.add_argument("--fresh", action="store_true")
    run_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "PANTHEON_PERSONA_STATE_DIR",
                Path.home() / ".local" / "state" / "pantheon" / "personas",
            )
        ),
    )

    consume_parser = subparsers.add_parser("consume")
    _common_parser(consume_parser)
    consume_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "PANTHEON_PERSONA_STATE_DIR",
                Path.home() / ".local" / "state" / "pantheon" / "personas",
            )
        ),
    )
    consume_parser.add_argument(
        "--reply-hub",
        type=Path,
        default=Path(os.environ.get("PANTHEON_MAILBOX_HUB", DEFAULT_MAILBOX_HUB)),
    )

    quota_parser = subparsers.add_parser("quota-check")
    _common_parser(quota_parser)
    quota_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "PANTHEON_PERSONA_STATE_DIR",
                Path.home() / ".local" / "state" / "pantheon" / "personas",
            )
        ),
    )

    args = parser.parse_args(argv)
    try:
        binding = load_binding(
            args.manifest,
            args.persona,
            harness_override=args.harness,
            executable_override=args.executable,
            model_override=args.model,
            profile_override=args.profile,
            mailbox_hub=args.mailbox_hub,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    primary_binding = binding

    explicit_runtime_override = any(
        value is not None
        for value in (args.harness, args.profile, args.executable, args.model)
    )
    if args.command in {"consume", "run"} and not explicit_runtime_override:
        try:
            binding = select_recorded_runtime_binding(
                args.manifest,
                binding,
                state_dir=args.state_dir,
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            parser.error(str(exc))

    if args.command == "quota-check":
        supported, reason = quota_check_supported(binding, manifest_path=args.manifest)
        if not supported:
            _print_json(
                QuotaCheckResult(
                    persona=binding.persona,
                    supported=False,
                    decision="unsupported",
                    acted=False,
                    reason=reason,
                )
            )
            return 0
        adapter = build_adapter(binding, harness_home=args.harness_home)
        fallback = first_fallback_binding(args.manifest, binding)
        result = quota_check(
            binding,
            adapter=adapter,
            fallback_binding=fallback,
            primary_binding=primary_binding,
            state_dir=args.state_dir,
            cwd=args.cwd,
            manifest_path=args.manifest,
        )
        _print_json(result)
        return 0

    report = diagnose_persona(
        binding,
        harness_home=args.harness_home,
        cwd=args.cwd,
        execution_context=f"{getpass.getuser()}:{args.cwd}",
    )
    if args.command == "diagnose":
        _print_json(report)
        return 1 if any(item.status == "fail" for item in report.observations) else 0

    if any(item.status == "fail" for item in report.observations):
        _print_json(report)
        if args.command == "consume":
            streak = bump_diag_streak(args.state_dir, binding.persona)
            if streak >= SOFT_FAILURE_ALERT_STREAK:
                return EXIT_SOFT_PERSISTENT
        return 3
    if args.command == "consume":
        # Diagnostics passed this fire (T-3209): the gate stage has recovered,
        # regardless of what the attempt stage below does next (backoff-skip,
        # policy-skip, transport failure, or success) -- clear its streak now.
        clear_diag_streak(args.state_dir, binding.persona)
        adapter = build_adapter(binding, harness_home=args.harness_home)
        fallback = first_fallback_binding(args.manifest, binding)
        recovery = (
            primary_recovery_evidence(primary_binding, state_dir=args.state_dir)
            if runtime_reference(binding) != runtime_reference(primary_binding)
            else None
        )
        remaining = backoff_remaining(args.state_dir, binding.persona)
        if remaining > 0:
            policy_skip = bump_policy_skip(args.state_dir, binding.persona, "backoff")
            _print_json(
                {
                    "binding": binding,
                    "diagnostics": report,
                    "consume": {
                        "skipped": "backoff",
                        "remaining_seconds": int(remaining),
                        "state": load_backoff(args.state_dir, binding.persona),
                        "policy_skip_streak": policy_skip,
                    },
                    "outcomes": [],
                }
            )
            return 0
        try:
            if binding.launch.mode == "interactive":
                runtime_candidates = configured_runtime_bindings(args.manifest, primary_binding)
                wake = wake_persistent_runtime(
                    binding,
                    adapter=adapter,
                    fallback_binding=fallback,
                    primary_binding=primary_binding,
                    runtime_candidates=runtime_candidates,
                    recovery_evidence=recovery,
                    state_dir=args.state_dir,
                    cwd=args.cwd,
                )
                stats: dict[str, Any] = {
                    "mode": "interactive-wake",
                    **wake,
                }
                outcomes: list[dict[str, str]] = []
            else:
                stats, outcomes = consume_persona_mailbox(
                    binding,
                    adapter=adapter,
                    runner=HarnessRunner(),
                    state_dir=args.state_dir,
                    cwd=args.cwd,
                    reply_hub=args.reply_hub,
                )
        except runtime_controller.RuntimeTransitionPending as exc:
            policy_skip = bump_policy_skip(args.state_dir, binding.persona, "runtime_transition")
            _print_json(
                {
                    "binding": binding,
                    "diagnostics": report,
                    "consume": {
                        "mode": "interactive-wake",
                        "skipped": "runtime_transition",
                        "reason": str(exc),
                        "policy_skip_streak": policy_skip,
                    },
                    "outcomes": [],
                }
            )
            return 0
        except runtime_controller.ProviderQuotaExhausted as exc:
            policy_skip = bump_policy_skip(args.state_dir, binding.persona, "quota_breaker_open")
            _print_json(
                {
                    "binding": binding,
                    "diagnostics": report,
                    "consume": {
                        "mode": "interactive-wake",
                        "skipped": "quota_breaker_open",
                        "profile": exc.profile,
                        "policy_skip_streak": policy_skip,
                    },
                    "outcomes": [],
                }
            )
            return 0
        except runtime_controller.RuntimeViabilityBlocked as exc:
            policy_skip = bump_policy_skip(
                args.state_dir, binding.persona, "runtime_viability_blocked"
            )
            _print_json(
                {
                    "binding": binding,
                    "diagnostics": report,
                    "consume": {
                        "mode": "interactive-wake",
                        "skipped": "runtime_viability_blocked",
                        "reason": exc.reason,
                        "block": exc.evidence,
                        "policy_skip_streak": policy_skip,
                    },
                    "outcomes": [],
                }
            )
            return 5
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            state = record_backoff_failure(
                args.state_dir,
                binding.persona,
                error,
            )
            streak = bump_soft_streak(args.state_dir, binding.persona)
            # A transport-level failure is a genuine (non-policy) outcome --
            # the attempt stage was reached and reached past any policy gate,
            # so the policy-skip streak (T-3210) no longer describes this
            # seat's state and must clear, same posture as the success tail.
            clear_policy_skip(args.state_dir, binding.persona)
            _print_json(
                {
                    "binding": binding,
                    "diagnostics": report,
                    "consume": {
                        "skipped": "transport_error",
                        "error": error,
                        "state": state,
                        "soft_streak": streak,
                    },
                    "outcomes": [],
                }
            )
            return EXIT_SOFT_PERSISTENT if streak >= SOFT_FAILURE_ALERT_STREAK else 5
        _print_json(
            {
                "binding": binding,
                "diagnostics": report,
                "consume": stats,
                "outcomes": outcomes,
            }
        )
        if stats.get("errors"):
            record_backoff_failure(
                args.state_dir,
                binding.persona,
                json.dumps(stats["errors"], sort_keys=True),
            )
            streak = bump_soft_streak(args.state_dir, binding.persona)
            # Same genuine-non-policy-outcome reasoning as the OSError branch
            # above (T-3210).
            clear_policy_skip(args.state_dir, binding.persona)
            return EXIT_SOFT_PERSISTENT if streak >= SOFT_FAILURE_ALERT_STREAK else 4
        clear_backoff(args.state_dir, binding.persona)
        clear_soft_streak(args.state_dir, binding.persona)
        clear_policy_skip(args.state_dir, binding.persona)
        return 0
    if args.prompt is not None and args.prompt_file is not None:
        parser.error("--prompt and --prompt-file are mutually exclusive")
    if args.prompt_file is not None:
        task_prompt = args.prompt_file.read_text(encoding="utf-8")
    elif args.prompt is not None:
        task_prompt = args.prompt
    else:
        task_prompt = sys.stdin.read()
    if not task_prompt.strip():
        parser.error("run requires a non-empty prompt via --prompt, --prompt-file, or stdin")

    session_id = None
    if not args.ephemeral and not args.fresh:
        session_id = load_session_id(args.state_dir, binding.persona)
    prompt = (
        continuation_prompt(binding, task_prompt)
        if session_id
        else orientation_prompt(binding, task_prompt)
    )
    request = ExecutionRequest(
        prompt=prompt,
        cwd=args.cwd,
        model=binding.launch.model,
        session_id=session_id,
        ephemeral=args.ephemeral,
    )
    adapter = build_adapter(binding, harness_home=args.harness_home)
    if binding.launch.mode == "interactive":
        if args.ephemeral:
            parser.error(
                "--ephemeral requires an explicitly selected batch runtime profile"
            )
        handle = runtime_controller.ensure_runtime(
            binding,
            adapter,
            state_root=args.state_dir,
        )
        receipt = runtime_controller.submit_to_runtime(handle, request)
        _print_json(
            {
                "binding": binding,
                "diagnostics": report,
                "delivery": receipt,
            }
        )
        return 0
    result = HarnessRunner().run(adapter, request)
    if (
        result.status == "completed"
        and result.session is not None
        and not args.ephemeral
    ):
        save_session_id(args.state_dir, binding, result.session.session_id)
    _print_json({"binding": binding, "diagnostics": report, "result": result})
    return 0 if result.status == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
