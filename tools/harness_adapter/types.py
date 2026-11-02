"""Typed lifecycle contracts shared by Pantheon harness bindings.

These types normalize evidence that the control plane genuinely needs across
harnesses: how to start/resume a turn, structural progress, terminal outcome,
usage, and durable inbound delivery.  Provider policy (sandbox semantics,
permission modes, native doctor checks) deliberately stays outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping


HarnessName = str
EventKind = Literal[
    "session_started",
    "turn_started",
    "progress",
    "tool_started",
    "tool_completed",
    "subagent_started",
    "subagent_completed",
    "message_completed",
    "turn_completed",
    "turn_failed",
    "retry_scheduled",
    "error",
]
ExecutionStatus = Literal["completed", "failed", "interrupted"]
ErrorCategory = Literal[
    "auth",
    "quota",
    "rate_limit",
    "approval",
    "sandbox",
    "config",
    "tool",
    "provider",
    "interrupted",
    "unknown",
]
DeliveryState = Literal[
    "accepted",
    "durably_queued",
    "submitted",
    "acked",
    "rejected",
    "failed",
]
DiagnosticStatus = Literal["pass", "warn", "fail", "unavailable"]
RuntimeMode = Literal["interactive", "batch"]
RuntimeMeter = Literal["subscription", "sdk-credit", "api", "enterprise"]
QuotaSource = Literal["app-server", "statusline", "provider-api", "unavailable"]
QuotaFreshness = Literal["fresh", "stale", "unavailable"]
PostStartAssessmentStatus = Literal["ready", "subscription-cap", "authentication-loss", "unknown"]
ProviderCompletionStatus = Literal[
    "provider-completed",
    "subscription-cap",
    "authentication-loss",
    "operational-readiness",
    "not-applicable",
    "unknown",
]


@dataclass(frozen=True)
class HookCapability:
    event: str
    matchers: bool
    can_block: bool
    can_continue: bool
    handler_types: tuple[str, ...]


@dataclass(frozen=True)
class HarnessCapabilities:
    harness: HarnessName
    session_resume_by_id: bool
    session_resume_last: bool
    structured_progress: bool
    structured_output_schema: bool
    local_generated_memory: bool
    native_stop_continuation: bool
    inbound_mechanisms: tuple[str, ...]
    hook_events: Mapping[str, HookCapability]


@dataclass(frozen=True)
class CommandSpec:
    """A shell-free subprocess invocation.

    Prompt text belongs on ``stdin`` so it is absent from process listings and
    does not need shell quoting.
    """

    argv: tuple[str, ...]
    stdin: str | None
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessLaunchConfig:
    harness: HarnessName
    profile: str
    executable: Path
    model: str | None
    mode: RuntimeMode
    meter: RuntimeMeter
    quota_source: QuotaSource
    native: Mapping[str, Any]
    sources: tuple[str, ...]
    writable_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class TmuxTarget:
    session: str
    window: str
    pane: str


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: float
    remaining_percent: float
    resets_at: datetime


@dataclass(frozen=True)
class QuotaSnapshot:
    profile: str
    windows: tuple[QuotaWindow, ...]
    observed_at: datetime
    freshness: QuotaFreshness
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class FailoverPolicy:
    weekly_remaining_percent: float
    fallbacks: tuple[str, ...]


@dataclass(frozen=True)
class PostStartCommand:
    """One persona-neutral command a role wants run after its runtime comes up.

    ``argv`` is an unsubstituted argv template (no shell): elements may
    reference ``{persona}``, ``{tmux_target}``, and ``{account_home}`` --
    substitution happens at invocation time in ``runtime_controller.py``,
    which has the live binding in hand (T-2606 / PRE-4). ``background``
    mirrors the two recovery-bot-supervise.sh call shapes it replaces: the
    pane-log tap runs foreground (fast, bounded), the boot-kick runs
    backgrounded so it can never block bringup.
    """

    argv: tuple[str, ...]
    background: bool = False


@dataclass(frozen=True)
class RuntimeControllerConfig:
    tmux: TmuxTarget
    policy: FailoverPolicy
    post_start: tuple[PostStartCommand, ...] = ()
    # T-4037: the mid-session-cap SWAP action (kill the capped runtime + let
    # reselection pick the fallback). Detection+publish (read-only, cheap) is
    # unconditional; the swap action is gated by this flag, which T-5446 made
    # fleet policy (default True, not hand-settable).
    #
    # T-4047 NOTE (shared-flag safety): this resolves per ROLE, not per
    # harness -- `persona_bringup.load_binding` -> `resolve_controller_config`
    # is keyed by `(persona, role)` only (no harness parameter), so a
    # persona's claude AND codex bindings for the same role get the
    # byte-identical flag value. This is harmless by construction, not by
    # convention: the swap action requires BOTH this flag AND a CONFIRMED
    # modal (`_observe_and_publish_mid_session_cap` classifies ONLY when
    # `binding.launch.harness == "claude"` -- structurally never true for a
    # codex binding, whatever this flag says; see
    # `test_non_claude_harness_never_classifies_a_modal` and
    # `test_shared_flag_on_codex_binding_never_swaps_even_with_cap_shaped_pane_text`
    # in tools/test_runtime_controller.py).
    # T-5446: ON by default for every seat; the per-role manifest key is retired
    # (rejected by the validator), so production always resolves True.
    mid_session_cap_swap_enabled: bool = True
    # T-4046: the fallback-RETURN action (the opposite direction of
    # mid_session_cap_swap_enabled above -- end a running LOWER-priority
    # runtime once a strictly higher-priority one becomes viable again).
    # T-5446: fleet policy, default True, not hand-settable.
    #
    # T-4047 NOTE (shared-flag safety): same per-ROLE resolution as
    # mid_session_cap_swap_enabled above, harmless for the same reason but a
    # DIFFERENT gate: `_fallback_return_eligible` returns None outright for
    # whichever binding is currently first (`own_index == 0`) in manifest
    # order, regardless of this flag's value -- there is nothing
    # higher-priority to fail back TO. See
    # `test_none_when_already_first_choice` in tools/test_runtime_controller.py.
    fallback_return_enabled: bool = True


@dataclass(frozen=True)
class RuntimeReadinessProof:
    ready: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class PostStartPaneSample:
    capture_index: int
    sha256: str
    snippet_sha256: str | None
    text: str


@dataclass(frozen=True)
class PostStartAssessment:
    schema: str
    harness: str
    profile: str
    status: PostStartAssessmentStatus
    cause: str | None
    pattern_id: str | None
    baseline_sha256: str
    capture_count: int
    changed_capture_indices: tuple[int, ...]
    # T-4047: DERIVED, typed values parsed from the classifying pane sample --
    # never raw pane text (consistent with this module's existing hash-only
    # evidence convention). Both default None -- every status other than
    # "subscription-cap" leaves them unset; only ClaudeHarnessAdapter's
    # subscription-cap branch populates them today.
    reset_at: datetime | None = None
    cap_kind: str | None = None


@dataclass(frozen=True)
class ProviderCompletionProbe:
    """Transient provider probe input.

    ``prompt`` and ``nonce_marker`` are intentionally raw transient values.
    Controller evidence must serialize only the hashes below.
    """

    prompt: str
    nonce_marker: str
    nonce_sha256: str
    prompt_sha256: str
    provider_state: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ProviderCompletionAssessment:
    schema: str
    harness: str
    profile: str
    status: ProviderCompletionStatus
    cause: str | None
    pattern_id: str | None
    nonce_sha256: str | None
    prompt_sha256: str | None
    statusline_baseline_sha256: str | None
    statusline_observed_sha256: str | None
    capture_count: int
    changed_capture_indices: tuple[int, ...]
    transcript_relative_path_sha256: str | None = None
    transcript_start_offset: int | None = None
    transcript_end_offset: int | None = None
    assistant_event_count: int = 0
    # T-4321: how many times the controller re-typed a probe that never
    # reached the harness's prompt history (swallowed during boot).
    probe_resubmit_count: int = 0
    # T-4362: why each re-type check did not re-type, as "reason=count" pairs
    # in first-seen order, and the private file holding the last raw pane
    # captures of a probe that was never proven (evidence keeps hashes only).
    probe_resubmit_skips: str = ""
    pane_captures_path: str | None = None
    pane_captures_sha256: str | None = None
    pane_captures_error: str | None = None


@dataclass(frozen=True)
class SuccessorEvidenceContract:
    expected: str
    target: str
    evidence_kind: str


@dataclass(frozen=True)
class ReceiverStateProof:
    state: Literal["present", "absent", "ambiguous", "not-applicable"]
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class ReceiverHookReceipt:
    ok: bool
    operations: tuple[str, ...]
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class ForcedFailoverReceipt:
    previous_session_id: str | None
    chronicle_entry: Path
    message_id: str
    correlation_id: str
    ticket_or_step_id: str | None
    repository_evidence: Mapping[str, str]
    last_lifecycle_event: str | None
    provider_error: str
    created_at: datetime


@dataclass(frozen=True)
class StandingInstructionsDescriptor:
    filename: str
    discovered: tuple[Path, ...]
    load_order: tuple[str, ...]
    authoritative: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class AuthDescriptor:
    mechanisms: tuple[str, ...]
    material_paths: tuple[Path, ...]
    observed_state: Literal["unverified", "available", "unavailable"]
    authoritative: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class MemoryDescriptor:
    available: bool
    enabled: bool | None
    readable: bool | None
    writable: bool | None
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class McpDescriptor:
    config_paths: tuple[Path, ...]
    configured_servers: tuple[str, ...]
    scope: tuple[str, ...]
    authoritative: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class RoleDescriptor:
    config_paths: tuple[Path, ...]
    configured_roles: tuple[str, ...]
    delegation_modes: tuple[str, ...]
    authoritative: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class HarnessSurfaceDescriptors:
    standing: StandingInstructionsDescriptor
    auth: AuthDescriptor
    memory: MemoryDescriptor
    mcp: McpDescriptor
    roles: RoleDescriptor


@dataclass(frozen=True)
class ExecutionRequest:
    prompt: str
    cwd: Path
    model: str | None = None
    session_id: str | None = None
    output_schema: Path | None = None
    ephemeral: bool = False


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    name: str | None
    cwd: Path | None
    resumable: bool


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ProviderBillingEvidence:
    provider: HarnessName
    api_key_source: str | None
    total_cost_usd: float | None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    source_events: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessEvent:
    harness: HarnessName
    kind: EventKind
    session_id: str | None
    message: str | None
    usage: Usage | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionError:
    category: ErrorCategory
    message: str
    retryable: bool
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionResult:
    harness: HarnessName
    status: ExecutionStatus
    final_message: str | None
    session: SessionHandle | None
    usage: Usage | None
    model: str | None
    returncode: int
    error: ExecutionError | None
    provider_billing: ProviderBillingEvidence | None = None
    command_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class InboundRequest:
    message: str
    correlation_id: str
    source: str
    reply_route: Mapping[str, str]


@dataclass(frozen=True)
class DeliveryReceipt:
    harness: HarnessName
    correlation_id: str
    state: DeliveryState
    mechanism: str
    retryable: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class DiagnosticObservation:
    check_id: str
    status: DiagnosticStatus
    summary: str
    authoritative: bool
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class DiagnosticReport:
    harness: HarnessName
    execution_context: str
    observations: tuple[DiagnosticObservation, ...]
    launch: HarnessLaunchConfig | None = None
    controller: RuntimeControllerConfig | None = None
    surfaces: HarnessSurfaceDescriptors | None = None
