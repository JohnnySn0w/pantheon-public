"""Deterministic Pantheon runtime controller for persistent persona harnesses.

The controller owns process/tmux lifecycle and transition evidence.  It never
asks a model whether a quota decision is safe and never treats terminal pixels
as completion, acknowledgement, health, or quota authority.

Cooldown sources (T-5028 (c)).  Two cooldown sources exist by design. The
controller records ``runtimes/<slug>/last-attempt.json`` (record_last_attempt)
and its selection reads only that. The consume plane records its own attempts
in ``initial-viability.json`` (_candidate_cooldown_open) and reads only that.
The consume plane also honours the controller's record for the two classes
that describe the runtime rather than one plane's launch: ``runtime_blocked``
(until an operator clears it) and an unexpired ``subscription_cap`` (the
subscription itself is capped, whichever plane launches); see
_controller_cooldown_skip. Transient backoffs (process_death,
readiness_timeout, operational_failure) stay local to the plane that saw them,
and the controller does not read the consume plane's record.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import errno
import signal
import base64
import shlex
import shutil
import stat
import subprocess
import threading
import sys
import tempfile
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, NamedTuple, Sequence, TYPE_CHECKING

from tools import codex_modal
from tools import chronicle
from tools import handoff_first
from tools._lease import Lease
from tools.codex_telegram_actions import HEALTH_409_ERROR_PREFIX, PriorRuntime
from tools.codex_telegram_actions import receiver_handoff_actions as _receiver_action_order
from tools.harness_adapter.claude import classify_claude_mid_session_cap
from tools.harness_adapter.codex import classify_codex_usage_limit
# Re-exported for tools/bringup_acceptance/codex_acceptance_config_guard.py
# (rc.ensure_*); the controller's own seeding goes through prepare_launch.
from tools.harness_adapter.codex import (  # noqa: F401
    ensure_model_migration_prompts_hidden,
    ensure_project_trusted,
    ensure_update_check_disabled,
)
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.quota import cap_reset_ceiling
from tools.harness_adapter.types import (
    DeliveryReceipt,
    ExecutionRequest,
    HarnessLaunchConfig,
    PostStartAssessment,
    PostStartPaneSample,
    PostStartCommand,
    ProviderCompletionAssessment,
    ProviderCompletionProbe,
    ReceiverHookReceipt,
    ReceiverStateProof,
    TmuxTarget,
)
from tools.submit_verify import get_profile as submit_verify_profile
from tools.submit_verify import ready_for_submission
from tools.submit_verify import not_ready_reason
from tools.submit_verify import busy_reason
from tools.submit_verify import submit_pending
# T-5572: the pure initial-viability invocation-receipt validator (and the state-root
# helpers it needs) lives in a stdlib-only leaf so the ORG-052 executor validates
# receipts without loading this module; re-exported here as the same objects.
from tools.runtime_invocation_receipt import (  # noqa: F401
    DEFAULT_STATE_ROOT,
    INITIAL_VIABILITY_INVOCATION_MAX_BYTES,
    INITIAL_VIABILITY_INVOCATION_SCHEMA,
    SYSTEMD_INVOCATION_ID_RE,
    _canonical_bytes,
    _initial_viability_invocation_dir,
    _initial_viability_invocation_path,
    _persona_state,
    _read_invocation_payload_fd,
    _read_invocation_payload_no_follow_at,
    _secure_directory_fd,
    _state_root,
    _validate_initial_viability_invocation_payload,
    _validate_invocation_payload,
    _validate_systemd_invocation_id,
    validate_initial_viability_invocation_receipt,
)

if TYPE_CHECKING:
    from tools.harness_adapter.base import PantheonHarnessAdapter
    from tools.persona_bringup import PersonaBinding


SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"
TMUX_RUNTIME_SCOPE_UNIT = "pantheon-runtime-tmux.scope"
SCHEDULED_INVOCATION_TIMEOUT_SECONDS = 1945.0
SCHEDULED_INVOCATION_POLL_INTERVAL_SECONDS = 5.0
BUSCTL_JSON_STDOUT_MAX_BYTES = 65536
BUSCTL_JSON_STDERR_MAX_BYTES = 8192
_UINT64_MAX = (1 << 64) - 1
_SYSTEMD_TIMER_METADATA_KEYS = frozenset({"trigger_timer_realtime_usec", "trigger_timer_monotonic_usec"})
RUNTIME_START_TIMEOUT_SECONDS = 8.0
RUNTIME_START_POLL_SECONDS = 0.1
RUNTIME_INPUT_READY_TIMEOUT_SECONDS = 20.0
RUNTIME_INPUT_READY_POLL_SECONDS = 0.25
POST_START_TIMEOUT_SECONDS = 5.0
POST_START_ASSESSMENT_CAPTURE_COUNT = 3
POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS = RUNTIME_INPUT_READY_POLL_SECONDS
PROVIDER_COMPLETION_TIMEOUT_SECONDS = 90.0
PROVIDER_COMPLETION_POLL_SECONDS = RUNTIME_INPUT_READY_POLL_SECONDS
PROVIDER_COMPLETION_CAPTURE_COUNT = int(
    PROVIDER_COMPLETION_TIMEOUT_SECONDS / PROVIDER_COMPLETION_POLL_SECONDS
)
# T-4321: a probe the harness never took as a prompt (swallowed mid-boot) is
# re-typed after this long, at most this many times, within the same timeout.
# Both "timeout" and "after" are really CAPTURE COUNTS (seconds / poll): each
# re-type's send_keys_submit call (paste settle + submit verify, a few seconds)
# adds wall clock on top, up to ~9s over PROVIDER_COMPLETION_TIMEOUT_SECONDS.
# 5s sits past the measured history-stamp -> transcript-event gap (607 prompts:
# p99 1.7s, max 4.4s). The history-write lag itself is unmeasured.
PROVIDER_COMPLETION_RESUBMIT_AFTER_SECONDS = 5.0
PROVIDER_COMPLETION_MAX_RESUBMITS = 3
# T-4362: a submit into a runtime that has only just booted waits (at most this
# long) for the paste to render before its first Enter (send_keys_submit.sh
# --render-wait). Measured 2026-09-30 on recovery's candidate argv: the fresh TUI took
# ~2.1-4s to render a 130-char paste, and the fixed 0.7s Enter went in as a literal
# CR, which stranded the probe in the composer (T-3026 attempts 1 and 2).
FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS = 8.0
# T-5023: codex 0.147 sometimes prints this line ~5-8s after its banner, and the
# session then runs without the named MCP servers (telegram, sticker). Nothing
# else shows it: the MCP child processes stay alive, so a liveness check passes.
# A clean start prints no counterpart line, so the watch runs the whole window.
# Anchored to codex's own line (start of line, then at most a warning glyph): a
# mail or boot kick that QUOTES the text renders mid-line or after the "›"
# composer glyph, and must not read as a hit.
CODEX_MCP_STARTUP_INTERRUPTED_RE = re.compile(
    r"^[^\w\n›>]{0,4}MCP startup interrupted\.?\s*"
    r"(?:The following servers were not initialized:\s*([^\n]*))?",
    re.MULTILINE,
)
CODEX_MCP_STARTUP_WATCH_SECONDS = 60.0
CODEX_MCP_STARTUP_POLL_SECONDS = 2.0
CODEX_MCP_STARTUP_MAX_RELAUNCHES = 1
CODEX_MCP_STARTUP_HISTORY_LINES = 2000
# T-4390: submit_to_runtime bounds the send_keys_submit.sh helper at its render wait
# plus this margin. The margin covers the helper's (SUBMIT_RETRIES+1) = 3 rounds of
# settle -> Enter -> verify (0.7s settle, three tmux calls and one python3 predicate
# each: a few seconds at load ~15), several times over. The render wait is itself
# wall-clock bounded (T-4371), so only a hung tmux call can reach this limit.
SUBMIT_HELPER_TIMEOUT_MARGIN_SECONDS = 60.0
# T-5016: the session-end Chronicle duty (tools/session_chronicle.py) runs as a
# subprocess bounded by this timeout, so a stuck archive can delay a restart by
# at most this long and never block it. PANTHEON_SESSION_CHRONICLE_HOOKS=off
# turns both hooks into no-ops (the test suite's default; an operator backstop).
SESSION_CHRONICLE_TIMEOUT_SECONDS = 60.0
SESSION_CHRONICLE_REARM_TIMEOUT_SECONDS = 20.0
SESSION_CHRONICLE_HOOKS_ENV = "PANTHEON_SESSION_CHRONICLE_HOOKS"
#: T-5137: opt-in pantheon-health/v1 leg on PersonaController1. Only the exact value
#: "on" enables it; default off keeps the D-Bus surface byte-identical.
PERSONA_HEALTH_LEG_ENV = "PANTHEON_PERSONA_HEALTH_LEG"
SESSION_CHRONICLE_TOOL = Path(__file__).resolve().parent / "session_chronicle.py"
ARM_PANE_LOG_TOOL = Path(__file__).resolve().parent / "arm_pane_log.sh"
RUNTIME_SOURCE_HASH_PATHS = (
    "instance.yaml",
    "tools/claude_quota_probe.py",
    "tools/claude_statusline_capture.py",
    "tools/chronicle.py",
    "tools/mailbox_wake.py",
    "tools/persona_bringup.py",
    "tools/runtime_controller.py",
    "tools/runtime_invocation_receipt.py",
    "tools/session_chronicle.py",
    "tools/submit_verify.py",
    "tools/harness_adapter/base.py",
    "tools/harness_adapter/claude.py",
    "tools/harness_adapter/codex.py",
    "tools/harness_adapter/delivery.py",
    "tools/harness_adapter/diagnostics.py",
    "tools/harness_adapter/provider_quota.py",
    "tools/harness_adapter/quota.py",
    "tools/harness_adapter/runner.py",
    "tools/harness_adapter/types.py",
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/systemd/pantheon-persona-quota@.service",
    "tools/systemd/pantheon-persona-quota@.timer",
    "tools/t2382_live_acceptance.py",
    "tools/run_t2382_live_acceptance.sh",
    "tools/test_t2382_live_acceptance.py",
)
PROVENANCE_RECEIPT_MAX_BYTES = 1024 * 1024
SHEET_STDOUT_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class RuntimeHandle:
    profile: HarnessLaunchConfig
    tmux: TmuxTarget
    session_id: str | None
    started_at: datetime
    # T-2966 REBOUND finding #2: the launched runtime's pane-process pid, if
    # captured at start time. Default keeps this field additive/backward-
    # compatible -- every existing positional-arg RuntimeHandle(...)
    # construction across the codebase still works unchanged. `None` means
    # "no pid captured for this handle" (e.g. KEEP-layer callers that never
    # opted into pid-liveness); `_runtime_alive` falls back to tmux-only
    # liveness in that case.
    pid: int | None = None
    # Attempt ownership of the containing scope. False/None must never
    # authorize stopping a pre-existing tmux server scope.
    created_scope: bool | None = None


@dataclass(frozen=True)
class RuntimeSourceProvenance:
    revision: str
    source_hashes: Mapping[str, str]
    source_manifest_sha256: str
    runtime_start_boundary_at: datetime
    runtime_start_observed_at: datetime


class _ManagedReceiverProbe(NamedTuple):
    """Lightweight, in-memory-only receiver-presence probe result.

    Replaces the dissolved ``_ManagedReceiverProbe`` dataclass (T-2966): this is
    a plain call-boundary carrier, never persisted/serialised/recovered
    across invocations, so it isn't part of the R31->R38 self-referential
    mass -- it backs the bootstrap-time viability-selection receiver check
    (`_probe_managed_receiver` and friends), which stays live via
    `ensure_viable_runtime`.
    """

    ok: bool
    state: str
    reason: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeStart:
    scope_unit: str
    created_scope: bool


class RuntimeReplacementError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        pane_may_be_mutated: bool,
        started: RuntimeStart | None,
        original: BaseException,
    ) -> None:
        super().__init__(message)
        self.pane_may_be_mutated = pane_may_be_mutated
        self.started = started
        self.original = original


RuntimeFailureClass = Literal["subscription-cap", "operational-launch", "operational-readiness"]


class RuntimeOperationalFailure(RuntimeError):
    """Typed launch/readiness failure eligible for initial runtime fallback."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: RuntimeFailureClass,
        cause: str,
        binding_runtime: str,
        runtime_start: RuntimeStart | None = None,
        created_scope: bool = False,
        pane_may_be_mutated: bool = False,
        readiness_evidence: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.cause = cause
        self.binding_runtime = binding_runtime
        self.runtime_start = runtime_start
        self.created_scope = created_scope
        self.pane_may_be_mutated = pane_may_be_mutated
        self.readiness_evidence = dict(readiness_evidence or {})


class _RuntimeStartEvent(NamedTuple):
    """Transient started-runtime callback payload.

    Replaces the dissolved ``RuntimeReplacementStarted`` dataclass (T-2966):
    never itself persisted (its fields are unpacked into the operator
    transition-state dict by the callback that receives it), so it isn't
    part of the R31->R38 self-referential mass -- it backs
    `_replace_runtime`'s `on_started` hook, which stays live via the
    operator-swap/compensation flow.
    """

    started: RuntimeStart
    boundary: datetime


RuntimeStartedCallback = Callable[[_RuntimeStartEvent], None]


@dataclass(frozen=True)
class RuntimeViabilityAttempt:
    sequence: int
    runtime: str
    harness: str
    profile: str
    status: Literal["selected", "failed", "skipped-cooldown", "skipped-held", "skipped-recorded-sticky"]
    failure_class: RuntimeFailureClass | None
    cause: str
    readiness: Mapping[str, str]
    teardown: Mapping[str, str]
    receiver: Mapping[str, Any]
    cooldown: Mapping[str, str]
    source_provenance: Mapping[str, str]
    precheck_source_provenance: Mapping[str, str] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeViabilityReceipt:
    schema: str
    persona: str
    status: Literal["selected", "exhausted", "reused-recorded"]
    selected_runtime: str | None
    selected_harness: str | None
    selected_profile: str | None
    manifest_order: tuple[str, ...]
    attempt_limit: int
    attempts: tuple[RuntimeViabilityAttempt, ...]
    recorded_at: str


@dataclass(frozen=True)
class RuntimeViabilitySelection:
    binding: "PersonaBinding"
    adapter: "PantheonHarnessAdapter"
    handle: "RuntimeHandle"
    receipt: RuntimeViabilityReceipt


class RuntimeViabilityExhausted(RuntimeError):
    """No declared initial runtime candidate was viable."""


class RuntimeViabilityBlocked(RuntimeError):
    """A live recorded runtime exists but cannot be authenticated for reuse."""

    def __init__(
        self,
        reason: str,
        evidence: Mapping[str, Any],
        *,
        policy_skip_reason: str = "runtime_viability_blocked",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence)
        # T-4179: the COARSE consume policy-skip token persona_bringup persists
        # (bump_policy_skip) and fleet_health matches against a seat's
        # expected_policy_skip. A prior-receiver refusal gets its own token so a
        # seat expected to be runtime_viability_blocked (specialist) does not mask it.
        self.policy_skip_reason = policy_skip_reason


#: T-4179: coarse policy-skip token for a refused launch whose prior codex
#: Telegram poller was not proven absent (distinct from runtime_viability_blocked).
PRIOR_RECEIVER_POLICY_SKIP_REASON = "prior_receiver_blocked"


@dataclass(frozen=True)
class OperatorSwapReceipt:
    schema: str
    persona: str
    trigger: str
    correlation_id: str
    destination: str
    status: str
    phase: str
    source_runtime: str
    source_transition_id: str
    outgoing_harness: str
    outgoing_profile: str
    incoming_harness: str
    incoming_profile: str
    receiver_transferred: bool


@dataclass(frozen=True)
class OperatorCompensationRecoveryReceipt:
    schema: str
    persona: str
    status: Literal["recovered", "already-recovered"]
    recovery_id: str
    recovered_from_transition_id: str
    recovered_from_sha256: str
    predecessor_archive: str
    predecessor_archive_sha256: str
    source_runtime: str
    target_runtime: str
    source_runtime_proven: bool
    source_readiness_proven: bool
    target_absent_proven: bool
    target_metadata_mismatch_proven: bool
    receiver_source_present_proven: bool
    receiver_target_absent_proven: bool
    phase: str
    recovered_at: str


@dataclass(frozen=True)
class OperatorCompensationTargetLiveRecoveryReceipt:
    schema: str
    persona: str
    status: Literal["pending", "already-pending"]
    recovery_id: str
    recovered_from_transition_id: str
    recovered_from_sha256: str
    current_transition_sha256: str
    correlation_id: str
    source_runtime: str
    destination: str
    predecessor_archive: str
    predecessor_archive_sha256: str
    phase: str
    runtime_swapped: bool
    receiver_post_done: bool
    receiver_transferred: bool
    target_rearmed: bool
    target_readiness_proven: bool
    source_absent_proven: bool
    receiver_source_absent_proven: bool
    receiver_target_present_proven: bool
    chronicle_evidence: str
    expected_successor_read_evidence: str
    recovered_at: str


@dataclass(frozen=True)
class OperatorStartedUnverifiedRecoveryReceipt:
    schema: str
    persona: str
    status: Literal["pending", "already-pending"]
    recovery_id: str
    recovered_from_transition_id: str
    recovered_from_sha256: str
    correlation_id: str
    source_runtime: str
    destination: str
    predecessor_archive: str
    predecessor_archive_sha256: str
    phase: str
    runtime_swapped: bool
    receiver_pre_done: bool
    receiver_post_done: bool
    chronicle_evidence: str
    expected_successor_read_evidence: str
    recovered_at: str


@dataclass(frozen=True)
class TmuxRuntimeContext:
    """Optional non-default tmux socket/scope target for the runtime plumbing.

    Absent (``None``) everywhere the controller operates against the default
    tmux socket and the ``TMUX_RUNTIME_SCOPE_UNIT`` singleton scope -- every
    existing caller is unaffected. Only off-prod/test call sites that need a
    disposable server construct one explicitly (T-2589): a faithful
    claude<->codex runtime swap can then be exercised on a disposable tmux
    scope instead of the one production default-socket server.
    """

    socket: str | None = None
    scope_unit: str = TMUX_RUNTIME_SCOPE_UNIT

    def tmux_socket_args(self) -> tuple[str, ...]:
        return ("-L", self.socket) if self.socket else ()


def _context_kwargs(context: TmuxRuntimeContext | None) -> dict[str, TmuxRuntimeContext]:
    """Forward ``context`` only when the caller actually supplied one.

    Several existing tests assert exact mock call signatures (e.g.
    ``assert_called_once_with(binding, mock.ANY, replace=False)``) that must
    not gain an unexpected ``context=None`` kwarg -- this keeps every
    existing call site byte-identical while still letting T-2589 callers
    that DO pass a context have it reach the tmux/systemd-run argv.
    """
    return {"context": context} if context is not None else {}


@dataclass(frozen=True)
class RuntimeInputReadiness:
    """Outcome of polling a runtime pane for input readiness (T-2382).

    ``pane_sha256`` is a hash of the captured pane, never the raw text —
    controller evidence never inlines raw terminal content. ``adapter_reset_at``/
    ``adapter_cap_kind`` (T-4047) are likewise DERIVED, typed values parsed
    from the classifying pane sample -- never raw pane prose -- consistent
    with ``pane_sha256``'s own hash-only precedent.
    """

    ready: bool
    pane_sha256: str
    reason: str
    adapter_status: str = "unknown"
    adapter_cause: str | None = None
    adapter_pattern_id: str | None = None
    adapter_capture_count: int = 0
    adapter_reset_at: datetime | None = None
    adapter_cap_kind: str | None = None


@dataclass(frozen=True)
class SwapReceipt:
    trigger: Literal["operator", "planned-quota", "quota-reset", "forced-cap"]
    outgoing_profile: str
    incoming_profile: str
    handoff_kind: Literal["model-authored", "forced-factual"]
    chronicle_evidence: Path
    completed_at: datetime


class ProviderQuotaExhausted(RuntimeError):
    """Authoritative provider-cap signal; mailbox retries it without DLQ count."""

    mailbox_transient = True

    def __init__(
        self,
        message: str,
        *,
        profile: str | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.profile = profile
        self.provider_error = provider_error or message


class RuntimeTransitionPending(RuntimeError):
    """A durable transition barrier prevented this record from submission."""

    mailbox_transient = True


class RuntimeTransitionAbandoned(RuntimeTransitionPending):
    """A planned transition was abandoned before the runtime was replaced (T-4862):
    the target failed its pre-replace usability re-gate. Terminal; the outgoing
    runtime was left untouched."""


class OperationLockBusy(RuntimeError):
    """A per-persona runtime operation is already in progress."""


@dataclass(frozen=True)
class PlannedTransitionBeginResult:
    status: Literal["started", "already-active", "already-completed", "conflict"]
    phase: str | None
    transition_id: str | None


ReceiverDirection = str
ReceiverPhase = Literal["pre", "post", "rollback"]


@dataclass(frozen=True)
class ReceiverHandoffReceipt:
    schema: str
    persona: str
    account: str
    direction: str
    phase: str
    ok: bool
    operations: tuple[str, ...]
    error: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def operation_lock_path(persona: str, *, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "runtime-operation.lock"


@contextmanager
def persona_operation_lock(
    persona: str,
    *,
    state_root: Path | None = None,
    blocking: bool = True,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Serialize runtime mutation without extending mailbox's claim-only lease."""

    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    lease = Lease(
        operation_lock_path(persona, state_root=state_root),
        ttl_secs=60 * 60 * 6,
        label=f"persona-operation:{persona}",
    )
    acquired = False
    while True:
        acquired = lease.acquire()
        if acquired:
            break
        if not blocking or (deadline is not None and time.monotonic() >= deadline):
            raise OperationLockBusy(f"{persona} runtime operation is busy")
        time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            lease.release()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"source provenance path is missing or not regular: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest_hash(source_hashes: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical_bytes(source_hashes)).hexdigest()


def _valid_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_source_relative_path(value: Any) -> bool:
    """A recorded source-hash key: a non-empty repo-relative path, never absolute and
    never escaping the checkout through a ``..`` segment."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


SOURCE_KEY_DELTA_MAX_PATHS = 32


def _source_key_set_delta(
    recorded: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """T-5315: the key-set part of a source drift, bounded and sorted for evidence.
    ``added`` = paths hashed now but absent from the record; ``removed`` = recorded
    paths no longer hashed. Empty dict when the key sets are identical."""
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    if not added and not removed:
        return {}
    return {
        "source_key_set_changed": "true",
        "source_keys_added": added[:SOURCE_KEY_DELTA_MAX_PATHS],
        "source_keys_added_count": len(added),
        "source_keys_removed": removed[:SOURCE_KEY_DELTA_MAX_PATHS],
        "source_keys_removed_count": len(removed),
    }


def _validated_source_hashes_from_state(state: Mapping[str, Any]) -> dict[str, str]:
    """Validate the RECORDED source-hash map on its own (self-consistency only).

    T-5315: this deliberately does NOT compare the recorded key set against the
    current RUNTIME_SOURCE_HASH_PATHS. A change to that path list is source drift
    (the full-dict ``recorded != current`` compare in each caller sees it), handled
    exactly like a hash-content change; a caller that needs an exact match must
    compare itself (see _authenticate_recorded_selected_runtime)."""
    raw = state.get("controller_source_hashes")
    if not isinstance(raw, Mapping):
        raise ValueError("recorded-source-hashes-missing")
    if not raw:
        raise ValueError("recorded-source-hashes-missing")
    if any(not _valid_source_relative_path(key) for key in raw.keys()):
        raise ValueError("recorded-source-key-malformed")
    hashes = {str(key): value for key, value in raw.items()}
    if any(not _valid_sha256_hex(value) for value in hashes.values()):
        raise ValueError("recorded-source-hash-malformed")
    recorded_manifest = state.get("controller_source_manifest_sha256")
    if not _valid_sha256_hex(recorded_manifest):
        raise ValueError("recorded-source-manifest-malformed")
    if _source_manifest_hash(hashes) != recorded_manifest:
        raise ValueError("recorded-source-manifest-mismatch")
    return hashes


def _validated_current_source_hashes(provenance: RuntimeSourceProvenance) -> dict[str, str]:
    hashes = dict(provenance.source_hashes)
    if set(hashes.keys()) != set(RUNTIME_SOURCE_HASH_PATHS):
        raise ValueError("current-source-key-mismatch")
    if any(not _valid_sha256_hex(value) for value in hashes.values()):
        raise ValueError("current-source-hash-malformed")
    if not _valid_sha256_hex(provenance.source_manifest_sha256):
        raise ValueError("current-source-manifest-malformed")
    if _source_manifest_hash(hashes) != provenance.source_manifest_sha256:
        raise ValueError("current-source-manifest-mismatch")
    return hashes


def _runtime_viability_block(
    reason: str,
    *,
    binding: "PersonaBinding",
    runtime_state: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
    policy_skip_reason: str = "runtime_viability_blocked",
) -> RuntimeViabilityBlocked:
    evidence: dict[str, Any] = {
        "schema": "pantheon-runtime-viability-block/v1",
        "persona": binding.persona,
        "selected_runtime": _runtime_ref(binding),
        "selected_harness": binding.launch.harness,
        "selected_profile": binding.launch.profile,
        "tmux_target": str(runtime_state.get("tmux_target", "")),
        "runtime_scope_unit": str(runtime_state.get("runtime_scope_unit", "")),
        "reason": reason,
    }
    if extra:
        evidence.update(dict(extra))
    return RuntimeViabilityBlocked(reason, evidence, policy_skip_reason=policy_skip_reason)


def _raise_runtime_viability_block(
    reason: str,
    *,
    binding: "PersonaBinding",
    runtime_state: Mapping[str, Any],
    state_root: Path | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    block = _runtime_viability_block(
        reason,
        binding=binding,
        runtime_state=runtime_state,
        extra=extra,
    )
    block.evidence["evidence_log_status"] = "recorded"
    try:
        _append_controller_evidence(
            binding.persona,
            "runtime_viability_blocked",
            {
                "block_reason": reason,
                "block": block.evidence,
            },
            state_root=state_root,
        )
    except Exception as exc:
        block.evidence["evidence_log_status"] = "failed"
        block.evidence["evidence_log_error"] = f"{type(exc).__name__}: {exc}"[:500]
    raise block


def _pane_live_or_unknown(
    binding: "PersonaBinding",
    *,
    context: "TmuxRuntimeContext | None",
) -> tuple[bool, dict[str, str]]:
    try:
        live = pane_exists(binding.controller.tmux, **_context_kwargs(context))
    except Exception as exc:
        return True, {
            "pane_state": "unknown",
            "pane_error": f"{type(exc).__name__}: {exc}"[:500],
        }
    return live, {"pane_state": "present" if live else "absent"}


def _block_if_live_target(
    reason: str,
    binding: "PersonaBinding",
    *,
    runtime_state: Mapping[str, Any],
    state_root: Path | None,
    context: "TmuxRuntimeContext | None",
    extra: Mapping[str, Any] | None = None,
) -> None:
    live, evidence = _pane_live_or_unknown(binding, context=context)
    if not live:
        return
    merged = dict(evidence)
    if extra:
        merged.update(dict(extra))
    _raise_runtime_viability_block(
        reason,
        binding=binding,
        runtime_state=runtime_state,
        state_root=state_root,
        extra=merged,
    )


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repo_root), *args),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _controller_source_provenance(
    *,
    runtime_start_boundary_at: datetime,
    runtime_start_observed_at: datetime,
    repo_root: Path | None = None,
) -> RuntimeSourceProvenance:
    candidate = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    top = _run_git(candidate, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(top.stderr.strip() or "cannot inspect controller git top-level")
    observed_top = Path(top.stdout.strip()).resolve()
    if observed_top != candidate:
        raise RuntimeError(
            f"controller source root mismatch: expected {candidate}, got {observed_top}"
        )

    revision = _run_git(candidate, "rev-parse", "--verify", "HEAD")
    status = _run_git(
        candidate, "status", "--porcelain=v1", "--untracked-files=all"
    )
    for result, label in ((revision, "HEAD"), (status, "status")):
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"cannot inspect controller {label}")
    if status.stdout.strip():
        raise RuntimeError("controller source provenance requires a clean git worktree")

    observed_revision = revision.stdout.strip()
    if len(observed_revision) != 40 or any(
        char not in "0123456789abcdef" for char in observed_revision
    ):
        raise RuntimeError("controller source revision is not a full lowercase SHA-1")
    source_hashes = {
        relative: _sha256_file(candidate / relative)
        for relative in RUNTIME_SOURCE_HASH_PATHS
    }
    return RuntimeSourceProvenance(
        revision=observed_revision,
        source_hashes=source_hashes,
        source_manifest_sha256=_source_manifest_hash(source_hashes),
        runtime_start_boundary_at=runtime_start_boundary_at,
        runtime_start_observed_at=runtime_start_observed_at,
    )


_OPERATOR_STAMP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


def _manifest_bindings(manifest_path: Path, persona: str) -> tuple["PersonaBinding", ...]:
    from tools import persona_bringup

    primary = persona_bringup.load_binding(manifest_path, persona)
    return persona_bringup.configured_runtime_bindings(manifest_path, primary)


def _probe_managed_receiver(
    binding: "PersonaBinding",
    expected: Literal["present", "absent"],
    *,
    backend: Any | None,
) -> _ManagedReceiverProbe:
    if backend is None and not receiver_handoff_required(binding):
        return _ManagedReceiverProbe(True, "not-applicable", "receiver-not-managed", {})
    try:
        proof = _prove_receiver(binding, expected, backend=backend)
    except Exception as exc:
        text = str(exc)
        state = "ambiguous" if "duplicate" in text or "ambiguous" in text else "unknown"
        return _ManagedReceiverProbe(False, state, f"{type(exc).__name__}: {exc}"[:500], {})
    if proof.state == "not-applicable":
        return _ManagedReceiverProbe(True, "not-applicable", "receiver-not-applicable", dict(proof.evidence))
    ok = proof.state == expected
    state = proof.state if proof.state in {"present", "absent"} else "ambiguous"
    reason = "receiver-state-matched" if ok else f"receiver state is {proof.state}; expected {expected}"
    return _ManagedReceiverProbe(ok, state, reason, dict(proof.evidence))


def prove_managed_receiver_present(binding: "PersonaBinding") -> bool:
    return _probe_managed_receiver(binding, "present", backend=None).ok


def prove_predecessor_receiver_absent(
    binding: "PersonaBinding",
    *,
    manifest_path: Path | None = None,
) -> bool:
    if not receiver_handoff_required(binding):
        return True
    if binding.launch.harness != "codex":
        # A claude rehydrate's codex-poller check is a stop-then-prove in
        # _rehydrate_recorded_runtime_selection (T-4179), not a probe here: the
        # continuity coordinator also calls this for an OLD binding's absence.
        return True
    try:
        if manifest_path is None:
            raise RuntimeError("manifest path required for predecessor proof")
        predecessor = next(
            item
            for item in _manifest_bindings(manifest_path, binding.persona)
            if item.launch.harness == "claude"
        )
        proof = _probe_managed_receiver(predecessor, "absent", backend=None)
    except Exception:
        return False
    return proof.ok


def _target_text(target: TmuxTarget) -> str:
    pane = str(target.pane)
    if ":" in pane:
        return pane
    return f"{target.session}:{target.window}.{pane}"


def runtime_target_text(target: TmuxTarget) -> str:
    """Return the stable operator-visible tmux target for a runtime profile."""

    return _target_text(target)


def _evidence_log_path(
    persona: str,
    state_root: Path | None = None,
    *,
    name: str = "controller-evidence.jsonl",
) -> Path:
    return _persona_state(persona, state_root) / name


def _append_controller_evidence(
    persona: str,
    event: str,
    payload: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> None:
    transition = transition_state(persona, state_root=state_root)
    record = {
        "schema": "pantheon-runtime-controller-evidence/v1",
        "event": event,
        "persona": persona,
        "recorded_at": _iso(_utc_now()),
        "transition_id": transition.get("transition_id"),
        **payload,
    }
    _append_jsonl(_evidence_log_path(persona, state_root), record)


def _new_transition_id() -> str:
    return uuid.uuid4().hex


def _successor_read_evidence(evidence: Path, *, transition_id: str) -> str:
    return f"hook:runtime-transfer:{transition_id}:{evidence.name}"


def _run_tmux(
    *args: str,
    check: bool = False,
    context: TmuxRuntimeContext | None = None,
) -> subprocess.CompletedProcess[str]:
    socket_args = context.tmux_socket_args() if context is not None else ()
    return subprocess.run(
        ["tmux", *socket_args, *args],
        check=check,
        capture_output=True,
        text=True,
        shell=False,
    )


# T-5457: the consume unit sets PANTHEON_INSTANCE_MANIFEST to the live clone's manifest
# for its own use. A launched session must not inherit it, or tools run from a worktree
# inside that session (tests included) silently read the LIVE manifest. Every reader
# defaults to the instance.yaml beside its own code, which is the live clone for a live
# run. One `--unset=NAME` word each so argv classifiers skip it like any flag.
LAUNCH_UNSET_ENV = ("PANTHEON_INSTANCE_MANIFEST",)


def _runtime_command_argv(
    argv: tuple[str, ...], env: dict[str, str] | Any
) -> tuple[str, ...]:
    values = dict(env)
    ordered_keys = (("PATH",) if "PATH" in values else ()) + tuple(
        key for key in sorted(values) if key != "PATH"
    )
    environment = tuple(f"{key}={values[key]}" for key in ordered_keys)
    unset = tuple(f"--unset={key}" for key in LAUNCH_UNSET_ENV if key not in values)
    return ("/usr/bin/env", *unset, "--", *environment, *argv)


def _runtime_env_with_scope_path(
    env: Mapping[str, str] | Any,
    *,
    account_home: Path,
) -> dict[str, str]:
    scoped_path = _scope_launch_path(account_home)
    merged = dict(env)
    existing = merged.get("PATH")
    if existing is not None and existing != scoped_path:
        raise RuntimeError("runtime launch env PATH conflicts with controlled scope PATH")
    merged["PATH"] = scoped_path
    return merged


def _runtime_payload(
    binding: "PersonaBinding",
    *,
    session_id: str | None,
    started_at: datetime,
    runtime_scope_unit: str = TMUX_RUNTIME_SCOPE_UNIT,
    source_provenance: RuntimeSourceProvenance | None = None,
    provider_completion: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    launch = binding.launch
    target = binding.controller.tmux
    payload: dict[str, Any] = {
        "schema": "pantheon-runtime-state/v1",
        "persona": binding.persona,
        "role": binding.role,
        "harness": launch.harness,
        "profile": launch.profile,
        "executable": str(launch.executable),
        "model": launch.model,
        "tmux_target": _target_text(target),
        "runtime_scope_unit": runtime_scope_unit,
        "session_id": session_id,
        "started_at": _iso(started_at),
        "last_structural_event": "runtime_started",
        "last_structural_event_at": _iso(started_at),
    }
    if source_provenance is not None:
        payload.update(
            {
                "controller_source_revision": source_provenance.revision,
                "controller_source_hashes": dict(source_provenance.source_hashes),
                "controller_source_manifest_sha256": (
                    source_provenance.source_manifest_sha256
                ),
                "runtime_start_boundary_at": _iso(
                    source_provenance.runtime_start_boundary_at
                ),
                "runtime_start_observed_at": _iso(
                    source_provenance.runtime_start_observed_at
                ),
            }
        )
    if provider_completion is not None:
        payload["provider_completion"] = dict(provider_completion)
    return payload


def _handle_from_state(binding: "PersonaBinding", state: dict[str, Any]) -> RuntimeHandle:
    raw_started = state.get("started_at")
    try:
        started = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
    except ValueError:
        started = _utc_now()
    raw_session = state.get("session_id")
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=raw_session if isinstance(raw_session, str) else None,
        started_at=started,
        created_scope=False,
    )


def _state_matches(
    binding: "PersonaBinding",
    state: dict[str, Any],
    *,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    launch = binding.launch
    expected_scope_unit = context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT
    return all(
        (
            state.get("persona") == binding.persona,
            state.get("role") == binding.role,
            state.get("harness") == launch.harness,
            state.get("profile") == launch.profile,
            state.get("executable") == str(launch.executable),
            state.get("model") == launch.model,
            state.get("tmux_target") == _target_text(binding.controller.tmux),
            state.get("runtime_scope_unit") == expected_scope_unit,
        )
    )


def _tmux_no_server(detail: str) -> bool:
    """tmux's two "no server on this socket" forms (3.x): after kill-server, and
    a socket file that does not exist. Any other socket error (e.g. permission
    denied) is NOT absence."""
    detail = detail.strip().lower()
    return detail.startswith("no server running on ") or (
        detail.startswith("error connecting to ") and "no such file or directory" in detail
    )


# T-5166: tmux 3.4 `display-message -p -t <target>` does NOT fail on a missing
# target. A missing window falls back to another one (asked a:5.0, got a:3.0, rc 0),
# and a missing session prints blank fields (rc 0). So every probe prints the
# pane's own identity first and is accepted only when that identity matches the
# requested target; a mismatch or blank identity is absence.
_PANE_IDENTITY_FORMAT = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}"


def _identity_matches(requested: str, fields: list[str]) -> bool:
    """True when the probed pane identity `fields` IS the requested target text."""
    if len(fields) < 5 or not fields[0]:
        return False
    session_name, window_index, window_name, pane_index, pane_id = fields[:5]
    if requested.startswith("%"):
        return requested == pane_id
    session, sep, rest = requested.partition(":")
    if not sep:
        return False
    if session.lstrip("=") != session_name:
        return False
    window, dot, pane = rest.rpartition(".") if "." in rest else (rest, "", "")
    if window not in (window_index, window_name):
        return False
    return not dot or pane == pane_index


def _probe_pane(
    target: TmuxTarget, value_format: str, *, context: TmuxRuntimeContext | None = None
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    """display-message for `target`; returns (result, value) where value is None
    unless rc 0 AND the reported pane is exactly the requested one (T-5166)."""
    requested = _target_text(target)
    result = _run_tmux(
        "display-message", "-p", "-t", requested, f"{_PANE_IDENTITY_FORMAT}\t{value_format}",
        **_context_kwargs(context),
    )
    if result.returncode != 0:
        return result, None
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) < 6 or not _identity_matches(requested, fields):
        return result, None
    return result, fields[5].strip()


def pane_exists(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result, value = _probe_pane(target, "#{pane_dead}", context=context)
    if result.returncode == 0:
        return value == "0"
    detail = (result.stderr or result.stdout or "").strip()
    absent_markers = ("can't find pane", "can't find window", "can't find session")
    if any(marker in detail.lower() for marker in absent_markers):
        return False
    # T-4347 review: no server at all (whole-server stop/crash) is the clearest
    # absence; raising here blocked ensure_runtime from ever relaunching the seat.
    if _tmux_no_server(detail):
        return False
    raise RuntimeError(
        f"tmux pane probe failed (rc {result.returncode}): {detail or 'no detail'}"
    )


def _pane_process_pid(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> int | None:
    """Fresh pid lookup for `target`'s pane (T-2966 REBOUND finding #2).

    Deliberately a NEW, minimal helper -- not a resurrection of the
    dissolved `_tmux_pane_pid` (which raised a mass-specific
    RuntimeViabilityBlocked on failure, part of the R31->R38
    orphan-recovery apparatus). This one degrades to `None` on any
    failure: liveness-checking should read "can't confirm a pid" as "not
    alive", never raise. Called fresh on every `_runtime_alive` check
    (observation is authority, never cached) rather than trusting a
    pid captured once at start -- a respawned/replaced pane gets a new
    pid, and staleness here would silently defeat the whole point of the
    cross-check."""
    _, value = _probe_pane(target, "#{pane_pid}", context=context)
    # T-5166: a missing pane reports ANOTHER pane's pid under tmux's fallback;
    # _probe_pane returns None unless the reported pane is the requested one.
    return int(value) if value and value.isdigit() else None


def _pid_alive(pid: int) -> bool:
    """`kill -0 <pid>` (T-2966 REBOUND finding #2 spec text, literally).
    ProcessLookupError = the process is gone -- dead. PermissionError = the
    process exists but we can't signal it (different uid) -- alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _target_known(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message", "-p", "-t", _target_text(target), "#{pane_id}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _session_exists(session: str, *, context: TmuxRuntimeContext | None = None) -> bool:
    return _run_tmux("has-session", "-t", session, **_context_kwargs(context)).returncode == 0


def _window_exists(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message",
        "-p",
        "-t",
        f"{target.session}:{target.window}",
        "#{window_id}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _default_tmux_server_pid(*, context: TmuxRuntimeContext | None = None) -> int | None:
    """Return the default-socket tmux server PID without starting a server."""

    result = _run_tmux("-N", "list-sessions", "-F", "#{pid}", **_context_kwargs(context))
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).lower()
        if _tmux_no_server(detail):
            return None
        raise RuntimeError(detail or "cannot inspect default tmux server")
    raw_pids = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not raw_pids:
        raise RuntimeError("default tmux server returned no session PID evidence")
    if len(raw_pids) != 1:
        raise RuntimeError("default tmux socket reported multiple server PIDs")
    raw_pid = next(iter(raw_pids))
    if not raw_pid.isdigit() or int(raw_pid) <= 0:
        raise RuntimeError("default tmux server returned malformed PID evidence")
    return int(raw_pid)


def _pid_in_scope(pid: int, scope_unit: str, *, proc_root: Path = Path("/proc")) -> bool:
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(scope_unit in line.partition("::")[2].split("/") for line in lines)


def _run_systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _scope_state(scope_unit: str) -> tuple[str, str]:
    result = _run_systemctl_user(
        "show", "--property=LoadState", "--property=ActiveState", scope_unit
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "scope inspection failed"
        raise RuntimeError(detail)
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    return values.get("LoadState", ""), values.get("ActiveState", "")


def _prepare_runtime_scope(scope_unit: str) -> None:
    load_state, active_state = _scope_state(scope_unit)
    if active_state == "active":
        raise RuntimeError("runtime tmux scope is active without its expected server")
    if active_state == "failed" or load_state == "loaded":
        result = _run_systemctl_user("reset-failed", scope_unit)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "scope reset failed"
            raise RuntimeError(detail)


def _scope_launch_path(account_home: Path) -> str:
    """PATH override for the ``systemd-run --scope`` control-plane launch.

    ``--scope`` inherits the caller's execution environment (confirmed
    against ``systemd-run(1)`` and empirically: a live ``--setenv=PATH=``
    scope launch prints exactly the overridden value, with other inherited
    vars such as ``HOME`` passing through untouched). The persona-consume
    service's own PATH has no ``~/.bun/bin``, so any bun-based MCP plugin
    (e.g. telegram) hits ENOENT (T-2624 class). Replacing PATH here --
    rather than prepending onto whatever the caller inherited -- matches
    the existing project convention for this exact bug class (see
    ``persona_cutover_resolve.py``'s ``apply_path``). Persona-general by
    construction: derived only from *account_home*, never hardcoded to one
    account.
    """
    return (
        f"{account_home}/.bun/bin:{account_home}/.npm-global/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )


def _start_new_server_in_scope(
    target: TmuxTarget,
    *,
    cwd: Path,
    command_argv: tuple[str, ...],
    scope_unit: str,
    account_home: Path,
    context: TmuxRuntimeContext | None = None,
) -> subprocess.CompletedProcess[str]:
    socket_args = context.tmux_socket_args() if context is not None else ()
    return subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={scope_unit}",
            f"--setenv=PATH={_scope_launch_path(account_home)}",
            "tmux",
            *socket_args,
            "new-session",
            "-d",
            "-s",
            target.session,
            "-n",
            target.window,
            "-c",
            str(cwd),
            *command_argv,
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


class ScopeStopRefused(RuntimeError):
    """systemctl refused to stop a runtime scope (T-4338)."""


# `systemctl stop` exits 5 for a unit that isn't loaded: already gone, which is
# the goal state, not a refusal (measured on this host, systemd user manager).
_SYSTEMCTL_UNIT_NOT_LOADED = 5


def _stop_created_scope(scope_unit: str) -> None:
    """Stop a runtime scope, raising ScopeStopRefused if systemctl refuses.
    Before T-4338 the return code was discarded, so a refused stop was silent
    and the teardown recorded scope_stopped=true."""
    result = _run_systemctl_user("stop", scope_unit)
    if result.returncode not in (0, _SYSTEMCTL_UNIT_NOT_LOADED):
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise ScopeStopRefused(
            f"systemctl --user stop {scope_unit} refused (rc {result.returncode}): {detail}"
        )
    _run_systemctl_user("reset-failed", scope_unit)  # best effort: a gone unit has nothing to reset


def _stop_created_scope_best_effort(scope_unit: str) -> None:
    """For cleanup that raises its own failure right after: a refused stop must
    not replace that failure's class, so it is logged instead (it was silent
    before T-4338)."""
    try:
        _stop_created_scope(scope_unit)
    except ScopeStopRefused as exc:
        print(f"[runtime_controller] {exc}", file=sys.stderr)


def _wait_for_live_runtime(
    target: TmuxTarget,
    scope_unit: str,
    *,
    timeout: float = RUNTIME_START_TIMEOUT_SECONDS,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            present = pane_exists(target, **_context_kwargs(context))
        except RuntimeError:
            # T-5168: an unrecognised tmux error is "not confirmed yet", not a failed
            # start; the deadline still bounds the wait.
            present = False
        if present:
            try:
                server_pid = _default_tmux_server_pid(**_context_kwargs(context))
            except RuntimeError:
                server_pid = None
            if server_pid is not None and _pid_in_scope(server_pid, scope_unit):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RUNTIME_START_POLL_SECONDS)


def _statusline_launch_args(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None,
) -> tuple[str, ...]:
    """T-4024: ``--settings <file>`` wiring the main session's status-line capture.

    The provider-completion probe reads ``<persona_state>/quota/claude-statusline.json``
    (see the claude adapter's prepare/assess), and only a statusLine command writes
    it. Without this, completion could never be proven, so every start through
    ensure_runtime was torn down at 90s and the seat failed over. Adapters without
    ``interactive_statusline_settings`` (codex, test fakes) get nothing. A file path
    rather than inline JSON keeps argv free of spaces for seat-relaunch replay.
    Fail-open: a write error launches without it rather than blocking the start.
    """
    settings_for = getattr(adapter, "interactive_statusline_settings", None)
    if settings_for is None or binding.launch.harness != "claude":
        return ()
    quota_dir = _persona_state(binding.persona, state_root) / "quota"
    settings_path = quota_dir / "main-session-settings.json"
    settings = settings_for(output=quota_dir / "claude-statusline.json")
    if not isinstance(settings, dict):
        # Validate before touching disk: a duck-typed stand-in (e.g. a Mock
        # adapter) must not create state directories.
        return ()
    try:
        _atomic_json(settings_path, settings)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[runtime_controller] statusline wiring skipped: {exc!r}", file=sys.stderr)
        return ()
    return ("--settings", str(settings_path))


def _prepare_harness_launch(binding: "PersonaBinding", adapter: "PantheonHarnessAdapter", *, cwd: Path) -> None:
    # Exact name match, no alias/case normalization: the inline branch this
    # replaces compared `binding.launch.harness == "codex"/"claude"` exactly.
    if binding.launch.harness not in harness_integration.configured_harness_names():
        return
    integration = harness_integration.integration_for(binding.launch.harness)
    # Every in-repo integration (claude, codex, hermes) inherits prepare_launch
    # from _BaseIntegration (a no-op there). The getattr only spares an
    # out-of-repo/test integration written against the pre-T-5011 protocol,
    # which never had launch seeding: for it, skipping is the old behaviour.
    prepare = getattr(integration, "prepare_launch", None)
    if prepare is not None:
        prepare(binding, adapter, cwd=cwd)


def _session_chronicle_runner(argv: Sequence[str], *, timeout: float) -> dict[str, Any]:
    """Run one bounded session-chronicle subprocess and parse its JSON summary.
    Reuses _run_submit_helper's own-session + killpg-on-timeout boundary."""
    try:
        completed = _run_submit_helper(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "timeout_seconds": timeout}
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict) or not parsed:
        return {"status": "runner-error", "returncode": completed.returncode,
                "stderr": (completed.stderr or "")[-300:]}
    return parsed


#: T-5016 seam: tests replace this; production runs the real bounded subprocess.
_SESSION_CHRONICLE_RUNNER: Callable[..., dict[str, Any]] = _session_chronicle_runner


def _session_chronicle_end(
    binding: "PersonaBinding",
    *,
    cause: str,
    context: TmuxRuntimeContext | None = None,
    state_root: Path | None = None,
    stage_if_dead: bool = False,
) -> dict[str, Any]:
    """T-5016: archive the session that is ending in ``binding``'s pane.

    ``stage_if_dead`` (H2, review M1): the duty does nothing while the pane runs
    a live process, so a post-death call can never take a live session's tap.

    Fail-open and bounded: never raises, waits at most
    SESSION_CHRONICLE_TIMEOUT_SECONDS. The subprocess routes its own Recovery alert
    on an archive failure; a timeout or a runner error (where it could not) is
    alerted here. Returns the duty's summary (``closed_pipe`` tells the caller
    to re-arm the pane-log tap after the respawn)."""
    if os.environ.get(SESSION_CHRONICLE_HOOKS_ENV, "on") == "off":
        return {"status": "disabled"}
    try:
        argv = [
            sys.executable, str(SESSION_CHRONICLE_TOOL), "session-end",
            "--persona", binding.persona,
            "--cause", cause,
            "--account-home", str(binding.account_home),
            "--harness", str(binding.launch.harness),
            "--tmux-target", _target_text(binding.controller.tmux),
            "--persona-state-dir", str(_state_root(state_root)),
            "--cwd", str(binding.launch_cwd),
            "--stage",
            "--entrypoint", f"runtime_controller:{cause}",
        ]
        if stage_if_dead:
            argv.append("--stage-if-dead")
        if context is not None and context.socket:
            argv += ["--tmux-socket", context.socket]
        result = _SESSION_CHRONICLE_RUNNER(argv, timeout=SESSION_CHRONICLE_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            result = {"status": "runner-error"}
        if result.get("status") == "stubbed":  # the test suite's default runner (tools/conftest.py)
            return result
        if result.get("status") in ("timeout", "runner-error"):
            result["alert"] = _route_cooldown_gated_alert(
                binding.persona,
                f"runtime-controller {binding.persona}: the session-end Chronicle archive did not "
                f"complete ({result.get('status')}, cause {cause}); the restart went ahead without it.",
                "chronicle-archive-failed",
                stamp_name="chronicle-archive-alert.json",
                state_root=state_root,
                now=None,
            )
        _best_effort_controller_evidence(
            binding.persona,
            "chronicle_session_end",
            {
                "cause": cause,
                "status": str(result.get("status")),
                "closed_pipe": bool(result.get("closed_pipe")),
                "results": [
                    {k: r.get(k) for k in ("status", "key", "record", "cause", "pane_log_state")}
                    for r in result.get("results", ()) if isinstance(r, dict)
                ][:8],
            },
            state_root=state_root,
        )
        return result
    except Exception as exc:  # noqa: BLE001 -- a hook must never break a start
        try:
            print(f"[runtime_controller] {binding.persona}: session chronicle hook failed: {exc!r}"[:500],
                  file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "hook-error"}


def _session_chronicle_rearm(
    binding: "PersonaBinding",
    chronicle_end: Mapping[str, Any] | None,
    *,
    context: TmuxRuntimeContext | None = None,
) -> None:
    """T-5016: the duty closed the pane's tap before the respawn (a tap survives
    ``respawn-pane -k``), so re-arm it on the fresh session. Bounded; never
    raises. Only when a tap was attached and closed, so a seat without one gains
    none. arm_pane_log.sh talks to the default tmux server, so a runtime on a
    non-default socket is left to its own post_start."""
    try:
        if not chronicle_end or not chronicle_end.get("closed_pipe"):
            return
        if context is not None and context.socket:
            return
        _run_submit_helper(
            ["bash", str(ARM_PANE_LOG_TOOL), _target_text(binding.controller.tmux), binding.persona],
            timeout=SESSION_CHRONICLE_REARM_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[runtime_controller] {binding.persona}: pane-log re-arm failed: {exc!r}"[:500], file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def _start_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    replace: bool,
    context: TmuxRuntimeContext | None = None,
    state_root: Path | None = None,
) -> RuntimeStart:
    # ORG-106 L3a: the lowest tmux launch primitive. Every launch in this module funnels
    # here, so a deep-sleeping seat cannot be started even by a path that skipped the
    # higher chokepoints. Refusal only (OOM observation lives at the chokepoint).
    _refuse_sleeping_seat_launch(binding, state_root=state_root)
    target = binding.controller.tmux
    spec = adapter.build_interactive_launch(target, cwd=binding.launch_cwd)
    launch_argv = tuple(spec.argv) + _statusline_launch_args(binding, adapter, state_root=state_root)
    scoped_env = _runtime_env_with_scope_path(spec.env, account_home=binding.account_home)
    # T-5011 PR-A (T-4924 plan §2.4): harness-specific seeding (codex config
    # trust/update/migration/worker-guard; claude bypass consent) lives behind
    # the harness integration's prepare_launch, dispatched on the binding's
    # harness as the inline branch was. An unregistered harness seeds nothing.
    _prepare_harness_launch(binding, adapter, cwd=spec.cwd)
    command_argv = _runtime_command_argv(launch_argv, spec.env)
    scope_unit = context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None:
        if replace:
            raise RuntimeError("cannot replace a tmux target without a live server")
        # T-5016 H1 (no server, so no live pane): reconcile the session the dead
        # server's pane last ran. Runs after this branch's preflight (review M2).
        _session_chronicle_end(binding, cause="start", context=context, state_root=state_root)
        _prepare_runtime_scope(scope_unit)
        result = _start_new_server_in_scope(
            target,
            cwd=spec.cwd,
            command_argv=command_argv,
            scope_unit=scope_unit,
            account_home=binding.account_home,
            **_context_kwargs(context),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "tmux launch failed"
            raise RuntimeError(detail)
        return RuntimeStart(scope_unit=scope_unit, created_scope=True)
    if not _pid_in_scope(server_pid, scope_unit):
        raise RuntimeError(
            "default tmux server is not owned by the Pantheon runtime scope"
        )
    command_argv = _runtime_command_argv(
        launch_argv,
        scoped_env,
    )
    # T-5016 H1: whatever (re)starts this pane ends the session recorded in it;
    # archive that session first (fail-open, bounded, a no-op once archived).
    # After the preflight above (review M2), so a refused start never touches it.
    chronicle_end = _session_chronicle_end(binding, cause="start", context=context, state_root=state_root)
    try:
        if replace:
            result = _run_tmux(
                "respawn-pane",
                "-k",
                "-t",
                _target_text(target),
                "-c",
                str(spec.cwd),
                *command_argv,
                **_context_kwargs(context),
            )
        elif _target_known(target, **_context_kwargs(context)):
            result = _run_tmux(
                "respawn-pane",
                "-k",
                "-t",
                _target_text(target),
                "-c",
                str(spec.cwd),
                *command_argv,
                **_context_kwargs(context),
            )
        elif _session_exists(target.session, **_context_kwargs(context)):
            if _window_exists(target, **_context_kwargs(context)):
                raise RuntimeError(
                    "stable tmux window exists but its configured pane target is missing"
                )
            result = _run_tmux(
                "new-window",
                "-d",
                "-t",
                target.session,
                "-n",
                target.window,
                "-c",
                str(spec.cwd),
                *command_argv,
                **_context_kwargs(context),
            )
        else:
            result = _run_tmux(
                "new-session",
                "-d",
                "-s",
                target.session,
                "-n",
                target.window,
                "-c",
                str(spec.cwd),
                *command_argv,
                **_context_kwargs(context),
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "tmux launch failed"
            raise RuntimeError(detail)
    finally:
        # Success: the fresh session gets its tap back. Failure (review M2): the
        # old session may still be running in the pane, so give it its tap back
        # too. A no-op unless H1 actually closed a tap.
        _session_chronicle_rearm(binding, chronicle_end, context=context)
    return RuntimeStart(scope_unit=scope_unit, created_scope=False)


def _verify_started_runtime(
    binding: "PersonaBinding",
    started: RuntimeStart,
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> None:
    if _wait_for_live_runtime(
        binding.controller.tmux, started.scope_unit, **_context_kwargs(context)
    ):
        return
    if started.created_scope:
        _stop_created_scope_best_effort(started.scope_unit)
    _append_controller_evidence(
        binding.persona,
        "runtime_start_failed",
        {
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "tmux_target": _target_text(binding.controller.tmux),
            "runtime_scope_unit": started.scope_unit,
            "created_scope": started.created_scope,
            "reason": "runtime pane/server scope verification timed out",
        },
        state_root=state_root,
    )
    raise RuntimeError("runtime failed live tmux server scope verification")


def _capture_visible_runtime_pane(
    target: TmuxTarget, *, context: TmuxRuntimeContext | None = None
) -> str:
    """Capture the VISIBLE pane only — no scrollback.

    ``capture-pane -p`` with no ``-S``/``-E`` range defaults to the visible
    screen, which is exactly what a readiness check needs: scrollback can
    hold an idle prompt from a PRIOR turn while the live composer is still
    mid-draw, and matching against it would false-green.
    """
    result = _run_tmux(
        "capture-pane", "-p", "-t", _target_text(target), **_context_kwargs(context)
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _capture_runtime_pane_history(
    target: TmuxTarget, *, context: TmuxRuntimeContext | None = None
) -> str | None:
    """T-5023: visible screen PLUS scrollback, wrapped lines joined (-J).

    The opposite trade to `_capture_visible_runtime_pane`: the MCP startup
    warning is a one-shot line that a boot kick can scroll off the screen, so
    the watch must read history. Returns None when the capture fails (the pane
    is gone), which ends the watch: a dead pane is block_until_death's case."""
    result = _run_tmux(
        "capture-pane", "-p", "-J", "-S", f"-{CODEX_MCP_STARTUP_HISTORY_LINES}",
        "-t", _target_text(target), **_context_kwargs(context),
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _clear_runtime_pane_for_launch(
    target: TmuxTarget, *, context: TmuxRuntimeContext | None = None
) -> None:
    """T-5023: wipe the screen and scrollback of a pane about to be replaced, so
    a warning printed by the PRIOR session cannot read as the new session's.
    Best-effort: the old process is killed by the respawn that follows, and the
    pane log tap keeps its output."""
    _run_tmux("send-keys", "-R", "-t", _target_text(target), **_context_kwargs(context))
    _run_tmux("clear-history", "-t", _target_text(target), **_context_kwargs(context))


def _wait_for_codex_mcp_startup_interrupt(
    target: TmuxTarget,
    *,
    baseline: int,
    deadline: float,
    capture: Callable[[TmuxTarget], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """Poll the pane until `deadline` (a `monotonic` value) for a NEW codex
    "MCP startup interrupted" line, i.e. more of them than `baseline`. Returns
    the uninitialised server list (or a placeholder if it was cut off), or None
    when the window passes clean or the pane goes away."""
    capture_fn = capture or _capture_runtime_pane_history
    while True:
        text = capture_fn(target)
        if text is None:
            return None
        matches = list(CODEX_MCP_STARTUP_INTERRUPTED_RE.finditer(text))
        if len(matches) > baseline:
            servers = (matches[-1].group(1) or "").strip()
            return servers or "(server list not captured)"
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(CODEX_MCP_STARTUP_POLL_SECONDS, remaining))


def _post_start_sample(text: str, *, capture_index: int) -> PostStartPaneSample:
    return PostStartPaneSample(
        capture_index=capture_index,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        snippet_sha256=hashlib.sha256(text[:512].encode("utf-8")).hexdigest() if text else None,
        text=text,
    )


def _post_start_assessment_evidence(assessment: PostStartAssessment) -> Mapping[str, str]:
    # T-4173: carry the DERIVED reset_at/cap_kind (never raw pane text) with
    # the SAME keys + encoding T-4047 gave the input-readiness path
    # (_verify_runtime_input_ready), so controller_main's
    # _post_start_cap_cooldown_override can pin a post-hook cap's cooldown to
    # its real reset instead of falling through to next_weekly_reset. getattr:
    # duck-typed assessments (older fakes/adapters) may lack the fields; an
    # isinstance guard keeps a malformed reset_at from raising here (this runs
    # on the fail-closed cap-raise path, which must never crash instead).
    reset_at = getattr(assessment, "reset_at", None)
    return {
        "event": "post_start_assessment",
        "harness": assessment.harness,
        "profile": assessment.profile,
        "status": assessment.status,
        "cause": assessment.cause or "",
        "pattern_id": assessment.pattern_id or "",
        "baseline_sha256": assessment.baseline_sha256,
        "capture_count": str(assessment.capture_count),
        "changed_capture_indices": ",".join(str(item) for item in assessment.changed_capture_indices),
        "schema": assessment.schema,
        "reset_at": _iso(reset_at) if isinstance(reset_at, datetime) else "",
        "cap_kind": getattr(assessment, "cap_kind", None) or "",
    }


def _bounded_post_start_samples(
    binding: "PersonaBinding",
    *,
    context: TmuxRuntimeContext | None = None,
    capture_count: int = POST_START_ASSESSMENT_CAPTURE_COUNT,
    interval_seconds: float = POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[PostStartPaneSample, ...]:
    samples: list[PostStartPaneSample] = []
    for index in range(1, capture_count + 1):
        text = _capture_visible_runtime_pane(
            binding.controller.tmux,
            **_context_kwargs(context),
        )
        samples.append(_post_start_sample(text, capture_index=index))
        if index < capture_count:
            sleep(interval_seconds)
    return tuple(samples)


def _assess_post_start_after_commands(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    baseline: PostStartPaneSample,
    context: TmuxRuntimeContext | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> PostStartAssessment:
    """``now`` (T-4173, mirrors T-4047's `_wait_for_runtime_input_ready`):
    wall-clock source threaded into the adapter's reset-instant parsing;
    defaults to `_utc_now` for production, tests inject a fixed clock."""
    now_fn = now or _utc_now
    samples = _bounded_post_start_samples(binding, context=context, sleep=sleep)
    assess = getattr(adapter, "assess_post_start_readiness", None)
    if assess is None:
        sample_tuple = tuple(samples)
        return PostStartAssessment(
            schema="pantheon-harness-post-start-assessment/v1",
            harness=binding.launch.harness,
            profile=binding.launch.profile,
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
    return assess(
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        baseline=baseline,
        samples=samples,
        now=now_fn(),
    )


def _raise_for_post_start_assessment(
    binding: "PersonaBinding",
    started: RuntimeStart,
    assessment: PostStartAssessment,
) -> None:
    if assessment.status != "subscription-cap":
        return
    evidence = {
        **_post_start_assessment_evidence(assessment),
        "runtime_scope_unit": started.scope_unit,
        "created_scope": str(started.created_scope).lower(),
        "tmux_target": _target_text(binding.controller.tmux),
    }
    raise RuntimeOperationalFailure(
        "runtime post-start subscription cap detected",
        failure_class="subscription-cap",
        cause=assessment.cause or "post-start-subscription-cap",
        binding_runtime=_runtime_ref(binding),
        runtime_start=started,
        created_scope=started.created_scope,
        pane_may_be_mutated=True,
        readiness_evidence=evidence,
    )


def _provider_completion_evidence(assessment: ProviderCompletionAssessment) -> Mapping[str, str]:
    evidence = {
        "event": "provider_completion_assessment",
        "schema": assessment.schema,
        "harness": assessment.harness,
        "profile": assessment.profile,
        "provider_completion_status": assessment.status,
        "status": assessment.status,
        "cause": assessment.cause or "",
        "pattern_id": assessment.pattern_id or "",
        "nonce_sha256": assessment.nonce_sha256 or "",
        "prompt_sha256": assessment.prompt_sha256 or "",
        "statusline_baseline_sha256": assessment.statusline_baseline_sha256 or "",
        "statusline_observed_sha256": assessment.statusline_observed_sha256 or "",
        "capture_count": str(assessment.capture_count),
        "changed_capture_indices": ",".join(str(item) for item in assessment.changed_capture_indices),
        "assistant_event_count": str(assessment.assistant_event_count),
        "probe_resubmit_count": str(assessment.probe_resubmit_count),
    }
    if assessment.transcript_relative_path_sha256 is not None:
        evidence["transcript_relative_path_sha256"] = assessment.transcript_relative_path_sha256
    if assessment.transcript_start_offset is not None:
        evidence["transcript_start_offset"] = str(assessment.transcript_start_offset)
    if assessment.transcript_end_offset is not None:
        evidence["transcript_end_offset"] = str(assessment.transcript_end_offset)
    if assessment.probe_resubmit_skips:
        evidence["probe_resubmit_skips"] = assessment.probe_resubmit_skips
    if assessment.pane_captures_path is not None:
        evidence["pane_captures_path"] = assessment.pane_captures_path
        evidence["pane_captures_sha256"] = assessment.pane_captures_sha256 or ""
    if assessment.pane_captures_error is not None:
        evidence["pane_captures_error"] = assessment.pane_captures_error
    return evidence


# T-4362: how many raw captures a never-proven probe keeps, beyond the
# baseline and the first capture after the submit, and how much of each.
PROVIDER_COMPLETION_TAIL_CAPTURES = 3
PROVIDER_COMPLETION_CAPTURE_MAX_CHARS = 16 * 1024
# T-4376: at most this many capture files are kept per persona, newest first.
PROVIDER_COMPLETION_CAPTURE_FILES_KEPT = 20
# Shapes that must never land on disk from a pane. The probe nonce is replaced
# first by exact match. The rest are coarse on purpose: a boot pane has no
# business showing a long token, so a false redaction costs nothing. Order
# matters: the specific shapes run before the generic key=value and blob ones.
# T-4376 (rev-t4362): keyed names with a prefix or suffix (GITHUB_TOKEN=,
# MY_API_KEY=; no leading \b, which cannot match after "_"), JSON keys, an
# optional quote, passphrase, Bearer tokens of any length, bare 32+ hex, URL
# basic-auth credentials and AWS access key ids.
_PANE_CAPTURE_REDACTIONS = (
    (re.compile(r"PANTHEON_PROVIDER_COMPLETION_[0-9A-Za-z]+"), "<probe-nonce>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1<redacted>@"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "<redacted-key>"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<redacted-bot-token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 <redacted>"),
    (re.compile(r"(?i)(token|secret|password|passwd|passphrase|api[_-]?key)"
                r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"), r"\1\2<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted-hex>"),
    (re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}"), "<redacted-blob>"),
)


def _redact_pane_capture(text: str, nonce_marker: str) -> str:
    if nonce_marker:
        text = text.replace(nonce_marker, "<probe-nonce>")
    for pattern, replacement in _PANE_CAPTURE_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _write_provider_completion_captures(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
    nonce_marker: str,
    samples: Sequence[PostStartPaneSample],
    skips: str,
) -> tuple[str | None, str | None, str | None]:
    """Keep the raw panes of a probe that was never proven (T-4362).

    Evidence carries pane hashes, never pane text, so after attempt 2 nobody
    could say what the TUI showed. This writes the baseline, the first capture
    after the submit and the last few captures, redacted, to a 0600 file in
    the persona's controller state, and returns (path, sha256, error) for the
    evidence. It never raises: an observability write must not fail a launch
    (T-4376: any exception, not only OSError, comes back as the error).
    """
    try:
        return _write_provider_completion_captures_or_raise(
            binding, state_root=state_root, nonce_marker=nonce_marker, samples=samples, skips=skips)
    except Exception as exc:
        # T-4399 (rev #601 N4): an OSError is the environment (disk, permissions) and
        # its class name says enough. Anything else is a bug in this writer, and the
        # evidence records only the class, so leave the traceback in the journal.
        if not isinstance(exc, OSError):
            traceback.print_exc(file=sys.stderr)
        return None, None, type(exc).__name__


def _write_provider_completion_captures_or_raise(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
    nonce_marker: str,
    samples: Sequence[PostStartPaneSample],
    skips: str,
) -> tuple[str | None, str | None, str | None]:
    picked: dict[int, PostStartPaneSample] = {}
    for sample in (*samples[:2], *samples[-PROVIDER_COMPLETION_TAIL_CAPTURES:]):
        picked.setdefault(sample.capture_index, sample)
    parts = [
        "# pantheon provider-completion pane captures (T-4362); redacted, never evidence\n",
        f"# persona={binding.persona} runtime={binding.launch.harness}/{binding.launch.profile}"
        f" target={_target_text(binding.controller.tmux)}\n",
        f"# probe_resubmit_skips={skips or '-'}\n",
    ]
    for index in sorted(picked):
        sample = picked[index]
        # Redact, THEN cut: a secret split at the cut would leave a short tail
        # too short for any shape to match (T-4376).
        text = _redact_pane_capture(sample.text, nonce_marker)[:PROVIDER_COMPLETION_CAPTURE_MAX_CHARS]
        parts.append(
            f"\n===== capture {index} sha256={sample.sha256}"
            f" not_ready={not_ready_reason(sample.text) or '-'} =====\n{text}"
        )
    body = "".join(parts).encode("utf-8")
    directory = _persona_state(binding.persona, state_root) / "provider-completion-captures"
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{stamp}-{binding.launch.harness}-{binding.launch.profile}.txt"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    _prune_provider_completion_captures(directory)
    return str(path), hashlib.sha256(body).hexdigest(), None


def _prune_provider_completion_captures(directory: Path) -> None:
    """Keep the newest PROVIDER_COMPLETION_CAPTURE_FILES_KEPT files (T-4376: ~80 KB
    per never-proven candidate, and nothing else ever removed them). Names start
    with a UTC stamp, so name order is age order. Best effort: a file that won't
    go is left for the next prune, and a failed listing must not turn the capture
    just written into a reported write failure."""
    try:
        files = sorted(p for p in directory.glob("*.txt") if p.is_file() and not p.is_symlink())
    except OSError:
        return
    for old in files[:-PROVIDER_COMPLETION_CAPTURE_FILES_KEPT]:
        try:
            old.unlink()
        except OSError:
            pass


def _raise_for_provider_completion_assessment(
    binding: "PersonaBinding",
    started: RuntimeStart,
    assessment: ProviderCompletionAssessment,
) -> None:
    if assessment.status in ("provider-completed", "not-applicable"):
        return
    evidence = {
        **_provider_completion_evidence(assessment),
        "runtime_scope_unit": started.scope_unit,
        "created_scope": str(started.created_scope).lower(),
        "tmux_target": _target_text(binding.controller.tmux),
    }
    if assessment.status == "subscription-cap":
        raise RuntimeOperationalFailure(
            "runtime provider completion detected subscription cap",
            failure_class="subscription-cap",
            cause=assessment.cause or "provider-completion-subscription-cap",
            binding_runtime=_runtime_ref(binding),
            runtime_start=started,
            created_scope=started.created_scope,
            pane_may_be_mutated=True,
            readiness_evidence=evidence,
        )
    raise RuntimeOperationalFailure(
        "runtime provider completion was not proven",
        failure_class="operational-readiness",
        cause=assessment.cause or "provider-completion-not-proven",
        binding_runtime=_runtime_ref(binding),
        runtime_start=started,
        created_scope=started.created_scope,
        pane_may_be_mutated=True,
        readiness_evidence=evidence,
    )


def _assess_provider_completion_after_input_ready(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
    sleep: Callable[[float], None] | None = None,
) -> ProviderCompletionAssessment:
    # Resolved per call, not as a default argument: a default binds the real
    # time.sleep at import, so a test's patch of rc.time.sleep never reached
    # this poll (it only showed once a test ran more than one capture).
    sleep = sleep if sleep is not None else time.sleep
    prepare = getattr(adapter, "prepare_interactive_provider_completion", None)
    assess = getattr(adapter, "assess_interactive_provider_completion", None)
    if prepare is None or assess is None:
        return ProviderCompletionAssessment(
            schema="pantheon-harness-provider-completion/v1",
            harness=binding.launch.harness,
            profile=binding.launch.profile,
            status="not-applicable",
            cause=None,
            pattern_id=None,
            nonce_sha256=None,
            prompt_sha256=None,
            statusline_baseline_sha256=None,
            statusline_observed_sha256=None,
            capture_count=0,
            changed_capture_indices=(),
        )
    baseline = _post_start_sample(
        _capture_visible_runtime_pane(binding.controller.tmux, **_context_kwargs(context)),
        capture_index=0,
    )
    nonce_marker = f"PANTHEON_PROVIDER_COMPLETION_{uuid.uuid4().hex}"
    nonce_sha256 = hashlib.sha256(nonce_marker.encode("utf-8")).hexdigest()
    submitted_at = _utc_now()
    persona_state = _persona_state(binding.persona, state_root)
    probe = prepare(
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        persona=binding.persona,
        account_home=binding.account_home,
        state_dir=persona_state,
        pane_baseline=baseline,
        submitted_at=submitted_at,
        nonce_marker=nonce_marker,
        nonce_sha256=nonce_sha256,
    )
    if probe is None:
        return assess(
            harness=binding.launch.harness,
            profile=binding.launch.profile,
            probe=None,
            samples=(),
        )
    if not isinstance(probe, ProviderCompletionProbe):
        return ProviderCompletionAssessment(
            schema="pantheon-harness-provider-completion/v1",
            harness=binding.launch.harness,
            profile=binding.launch.profile,
            status="not-applicable",
            cause=None,
            pattern_id=None,
            nonce_sha256=None,
            prompt_sha256=None,
            statusline_baseline_sha256=None,
            statusline_observed_sha256=None,
            capture_count=0,
            changed_capture_indices=(),
        )
    handle = RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=submitted_at,
    )
    request = ExecutionRequest(prompt=probe.prompt, cwd=binding.account_home)
    submit_to_runtime(
        handle, request, context=context,
        render_wait_seconds=FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS,
    )
    # T-4321: ready_for_submission can pass while the TUI is still booting, and
    # a probe pasted then is swallowed. send_keys_submit reads the empty
    # composer as submitted, so without this the wait below can only end in
    # provider-completion-not-proven and a fallback (recovery's T-3026 handmove,
    # 2026-09-29 19:59Z). Re-type the probe only when the
    # adapter positively says the harness never took it (None = can't tell)
    # and the composer isn't still holding it.
    submitted_fn = getattr(adapter, "interactive_provider_completion_submitted", None)
    watching_for_swallow = submitted_fn is not None
    resubmit_after = max(
        1, math.ceil(PROVIDER_COMPLETION_RESUBMIT_AFTER_SECONDS / PROVIDER_COMPLETION_POLL_SECONDS)
    )
    submit_profile = (
        submit_verify_profile(_submit_verify_harness(binding.launch.harness))
        if watching_for_swallow else None
    )
    resubmits = 0
    captures_since_submit = 0
    samples: list[PostStartPaneSample] = []
    # T-4362: one label per re-type check that did not re-type, counted, so a
    # never-proven probe says which gate held it (attempt 2 showed 0 re-types
    # and nothing else).
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    def finish(result: ProviderCompletionAssessment) -> ProviderCompletionAssessment:
        skip_text = ",".join(f"{reason}={count}" for reason, count in skips.items())
        result = dataclasses.replace(
            result, probe_resubmit_count=resubmits, probe_resubmit_skips=skip_text
        )
        if result.status != "operational-readiness":
            return result
        path, digest, error = _write_provider_completion_captures(
            binding,
            state_root=state_root,
            nonce_marker=nonce_marker,
            samples=(baseline, *samples),
            skips=skip_text,
        )
        return dataclasses.replace(
            result, pane_captures_path=path, pane_captures_sha256=digest, pane_captures_error=error
        )

    last = assess(
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        probe=probe,
        samples=(),
    )
    for index in range(1, PROVIDER_COMPLETION_CAPTURE_COUNT + 1):
        text = _capture_visible_runtime_pane(
            binding.controller.tmux,
            **_context_kwargs(context),
        )
        samples.append(_post_start_sample(text, capture_index=index))
        last = assess(
            harness=binding.launch.harness,
            profile=binding.launch.profile,
            probe=probe,
            samples=tuple(samples),
        )
        if last.status in ("provider-completed", "subscription-cap", "operational-readiness"):
            return finish(last)
        captures_since_submit += 1
        if watching_for_swallow and captures_since_submit >= resubmit_after:
            submitted = submitted_fn(probe=probe)
            not_ready = not_ready_reason(text) if submitted is False else None
            # T-4459: the frame and composer are drawn mid-turn too. A running turn
            # means the harness took something; a re-type now would queue a second
            # probe behind it (T-3026 attempt 4). Skip this check; don't end the watch.
            busy = busy_reason(text) if submitted is False and not_ready is None else None
            if submitted is not False:
                # Taken (True), or can't tell (None): either way the watch ends
                # for good. None is deliberately never read as swallowed.
                skip("submitted" if submitted else "submitted_unknown")
                watching_for_swallow = False
            elif not_ready is not None:
                skip(f"not_ready:{not_ready}")
            elif busy is not None:
                skip(f"busy:{busy}")
            elif submit_pending(submit_profile, text, probe.prompt):
                skip("probe_in_composer")
            else:
                try:
                    submit_to_runtime(
                        handle, request, context=context,
                        render_wait_seconds=FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS,
                    )
                except RuntimeError:
                    # T-4321 r1 R1: send_keys_submit exits 2 when a busy pane
                    # queues the text (T-4117) and 1 on a tmux/mode failure. A
                    # re-type is a heal, so its failure must not become a launch
                    # crash (no teardown, no fallback): stop watching, don't
                    # count it, and keep waiting on what was already sent.
                    skip("retype_failed")
                    watching_for_swallow = False
                else:
                    resubmits += 1
                    captures_since_submit = 0
                    watching_for_swallow = resubmits < PROVIDER_COMPLETION_MAX_RESUBMITS
        if index < PROVIDER_COMPLETION_CAPTURE_COUNT:
            sleep(PROVIDER_COMPLETION_POLL_SECONDS)
    return finish(
        dataclasses.replace(
            last,
            status="operational-readiness",
            cause="provider-completion-not-proven",
            capture_count=len(samples),
        )
    )


def _wait_for_runtime_input_ready(
    target: TmuxTarget,
    *,
    harness: str = "claude",
    profile: str = "",
    adapter: "PantheonHarnessAdapter | None" = None,
    timeout: float = RUNTIME_INPUT_READY_TIMEOUT_SECONDS,
    poll_interval: float = RUNTIME_INPUT_READY_POLL_SECONDS,
    context: TmuxRuntimeContext | None = None,
    now: Callable[[], datetime] | None = None,
) -> RuntimeInputReadiness:
    """Poll the runtime pane until it accepts input or the deadline passes.

    Mirrors _wait_for_live_runtime's monotonic-deadline poll shape. Evidence
    always carries a hash of the captured pane, never the raw text.

    ``now`` (T-4047) is the wall-clock source threaded into the adapter's
    reset-instant parsing (`assess_post_start_readiness`'s own ``now``
    kwarg) -- defaults to `_utc_now`, real wall-clock, for production; tests
    inject a fixed/fake clock for determinism. Unrelated to ``deadline``
    above, which stays on the monotonic clock for the readiness TIMEOUT.

    T-2594: when ``harness == "codex"``, also dismisses codex's
    update-available modal (at most once) via the single shared
    ``tools.codex_modal`` path -- the SAME precise, cursor-reverified
    navigation ``tools.bringup_acceptance.harness_codex_ready`` proved live.
    T-2599: same treatment for codex's blocking model-migration modal (at
    most once) -- a SEPARATE modal that can appear later in the same
    startup sequence (codex runs the update check before app-server start,
    the migration prompt after bootstrap; see ``codex_modal``'s own module
    docstring), so both dismissal branches share one poll loop rather than
    being mutually exclusive. Structurally codex-gated: for any other
    harness (``"claude"`` by default) ``codex_gated`` is False, so NEITHER
    modal-check branch's body -- including the ``send_keys`` closures below
    -- is ever entered, and ``_run_tmux(..., "send-keys", ...)`` is never
    reachable from this function. A claude binding cannot keystroke a pane
    through this path.
    """
    codex_gated = harness == "codex"
    now_fn = now or _utc_now
    deadline = time.monotonic() + timeout
    dismissed_update_modal = False
    dismissed_migration_modal = False
    pane_text = ""
    launch_baseline = _post_start_sample("", capture_index=0)
    capture_index = 0
    while True:
        capture_index += 1
        pane_text = _capture_visible_runtime_pane(target, **_context_kwargs(context))
        pane_sha256 = hashlib.sha256(pane_text.encode("utf-8")).hexdigest()
        if adapter is not None:
            assessment = adapter.assess_post_start_readiness(
                harness=harness,
                profile=profile,
                baseline=launch_baseline,
                samples=(_post_start_sample(pane_text, capture_index=capture_index),),
                now=now_fn(),
            )
            if assessment.status == "subscription-cap":
                return RuntimeInputReadiness(
                    ready=False,
                    pane_sha256=pane_sha256,
                    reason=assessment.cause or "post-start-subscription-cap",
                    adapter_status=assessment.status,
                    adapter_cause=assessment.cause,
                    adapter_pattern_id=assessment.pattern_id,
                    adapter_capture_count=assessment.capture_count,
                    adapter_reset_at=getattr(assessment, "reset_at", None),
                    adapter_cap_kind=getattr(assessment, "cap_kind", None),
                )
        if ready_for_submission(pane_text):
            return RuntimeInputReadiness(
                ready=True,
                pane_sha256=pane_sha256,
                reason="runtime input readiness confirmed",
            )

        def _send_key(key: str) -> None:
            _run_tmux(
                "send-keys", "-t", _target_text(target), key,
                **_context_kwargs(context),
            )

        def _recapture() -> str:
            return _capture_visible_runtime_pane(target, **_context_kwargs(context))

        if (
            codex_gated
            and not dismissed_update_modal
            and codex_modal.is_codex_update_modal(pane_text)
        ):
            try:
                codex_modal.dismiss_codex_update_modal(
                    capture=_recapture, send_keys=_send_key, sleep=time.sleep,
                )
            except codex_modal.ModalNavigationFailed as exc:
                return RuntimeInputReadiness(
                    ready=False,
                    pane_sha256=pane_sha256,
                    reason=f"codex update-modal dismissal failed: {exc}",
                )
            dismissed_update_modal = True
            continue
        if (
            codex_gated
            and not dismissed_migration_modal
            and codex_modal.is_codex_model_migration_modal(pane_text)
        ):
            try:
                codex_modal.dismiss_codex_model_migration_modal(
                    capture=_recapture, send_keys=_send_key, sleep=time.sleep,
                )
            except codex_modal.ModalNavigationFailed as exc:
                return RuntimeInputReadiness(
                    ready=False,
                    pane_sha256=pane_sha256,
                    reason=f"codex model-migration-modal dismissal failed: {exc}",
                )
            dismissed_migration_modal = True
            continue
        if time.monotonic() >= deadline:
            return RuntimeInputReadiness(
                ready=False,
                pane_sha256=pane_sha256,
                reason="runtime input readiness timed out",
            )
        time.sleep(poll_interval)


def _verify_runtime_input_ready(
    binding: "PersonaBinding",
    started: RuntimeStart,
    *,
    state_root: Path | None,
    adapter: "PantheonHarnessAdapter | None" = None,
    context: TmuxRuntimeContext | None = None,
    launched_at: datetime | None = None,
) -> None:
    readiness = _wait_for_runtime_input_ready(
        binding.controller.tmux,
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        adapter=adapter,
        **_context_kwargs(context),
    )
    if readiness.ready:
        return
    if readiness.adapter_status == "subscription-cap":
        evidence = {
            "event": "post_start_assessment",
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "status": readiness.adapter_status,
            "cause": readiness.adapter_cause or "post-start-subscription-cap",
            "pattern_id": readiness.adapter_pattern_id or "",
            "capture_count": str(readiness.adapter_capture_count),
            "pane_sha256": readiness.pane_sha256,
            "tmux_target": _target_text(binding.controller.tmux),
            "runtime_scope_unit": started.scope_unit,
            "created_scope": str(started.created_scope).lower(),
            # T-4047: derived, typed values (never raw pane text) so
            # controller_main's except-branch can compute a proportionate
            # cooldown instead of always falling through to next_weekly_reset.
            "reset_at": _iso(readiness.adapter_reset_at) if readiness.adapter_reset_at else "",
            "cap_kind": readiness.adapter_cap_kind or "",
        }
        _append_controller_evidence(
            binding.persona,
            "runtime_input_subscription_cap",
            evidence,
            state_root=state_root,
        )
        raise RuntimeOperationalFailure(
            "runtime input readiness detected subscription cap",
            failure_class="subscription-cap",
            cause=readiness.adapter_cause or "post-start-subscription-cap",
            binding_runtime=_runtime_ref(binding),
            runtime_start=started,
            created_scope=started.created_scope,
            pane_may_be_mutated=True,
            readiness_evidence=evidence,
        )
    # T-4332: read the candidate's receiver window BEFORE the stop below. A clean
    # SIGTERM (which the scope stop is) makes the Telegram plugin remove its own
    # bot.pid, so compensate's later read would find nothing and send no notice.
    # The window rides on the failure's evidence instead.
    receiver_window = _candidate_receiver_window_or_unknown(
        binding, launched_at=launched_at, now=_utc_now())
    if started.created_scope:
        _stop_created_scope_best_effort(started.scope_unit)
    evidence = {
        "event": "runtime_input_not_ready",
        "harness": binding.launch.harness,
        "profile": binding.launch.profile,
        "tmux_target": _target_text(binding.controller.tmux),
        "runtime_scope_unit": started.scope_unit,
        "created_scope": str(started.created_scope).lower(),
        "pane_sha256": readiness.pane_sha256,
        "reason": readiness.reason,
        **(receiver_window or {}),
    }
    _append_controller_evidence(
        binding.persona,
        "runtime_input_not_ready",
        evidence,
        state_root=state_root,
    )
    raise RuntimeOperationalFailure(
        "runtime failed input readiness verification",
        failure_class="operational-readiness",
        cause="runtime_input_not_ready",
        binding_runtime=_runtime_ref(binding),
        runtime_start=started,
        created_scope=started.created_scope,
        pane_may_be_mutated=True,
        readiness_evidence=evidence,
    )


def _clear_stale_runtime_state(persona: str, *, state_root: Path | None) -> None:
    """Overwrite runtime.json with an empty record.

    Called only from the _replace_runtime failure path: a failed replacement
    means the respawn/respawn-pane already tore down the prior pane, so the
    OLD runtime.json now describes a runtime that no longer exists. Clearing
    it fail-closed means a later ensure_runtime never inherits stale
    "healthy" metadata instead of starting fresh.
    """
    _atomic_json(_persona_state(persona, state_root) / "runtime.json", {})


def _initial_viability_path(persona: str, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "initial-viability.json"


def initial_viability_state(persona: str, *, state_root: Path | None = None) -> dict[str, Any]:
    return _read_json(_initial_viability_path(persona, state_root))


def validate_initial_viability_state(
    persona: str,
    *,
    state_root: Path | None = None,
    expect_selected: str | None = None,
    expect_failed: str | None = None,
    expect_failure_classes: Sequence[str] | None = None,
    require_source_provenance: bool = False,
    require_receiver_receipt: bool = False,
) -> Mapping[str, Any]:
    allowed_known = {"subscription-cap", "operational-launch", "operational-readiness"}
    allowed = tuple(expect_failure_classes or ())
    unknown = sorted(set(allowed) - allowed_known)
    state = initial_viability_state(persona, state_root=state_root)
    errors: list[str] = []
    if unknown:
        errors.append(f"unknown allowed failure class: {unknown}")
    if state.get("schema") != "pantheon-runtime-initial-viability/v1":
        errors.append("schema mismatch")
    if state.get("status") != "selected":
        errors.append("initial viability did not record selected status")
    if expect_selected is not None and state.get("selected_runtime") != expect_selected:
        errors.append(f"selected runtime mismatch: {state.get('selected_runtime')!r}")
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
        errors.append("attempts missing")
    if expect_failed is not None:
        matched = [
            item
            for item in attempts
            if isinstance(item, Mapping)
            and item.get("runtime") == expect_failed
            and item.get("status") == "failed"
        ]
        if not matched:
            errors.append(f"failed runtime {expect_failed!r} not recorded")
        elif allowed:
            classes = {str(item.get("failure_class", "")) for item in matched}
            if not classes or not classes.issubset(set(allowed)) or "" in classes:
                errors.append(f"failed runtime class {sorted(classes)!r} outside allowed set {sorted(allowed)!r}")
        elif any(not item.get("failure_class") for item in matched):
            errors.append(f"failure class missing for {expect_failed!r}")
    selected_attempt = next(
        (
            item
            for item in attempts
            if isinstance(item, Mapping)
            and item.get("runtime") == state.get("selected_runtime")
            and item.get("status") == "selected"
        ),
        None,
    )
    if selected_attempt is None:
        errors.append("selected attempt missing")
    else:
        readiness = selected_attempt.get("readiness")
        if not isinstance(readiness, Mapping) or readiness.get("event") != "runtime_started":
            errors.append("selected readiness evidence missing")
        if require_source_provenance:
            provenance = selected_attempt.get("source_provenance")
            required = {"revision", "source_manifest_sha256", "runtime_start_boundary_at", "runtime_start_observed_at"}
            if not isinstance(provenance, Mapping) or not required.issubset(provenance):
                errors.append("selected source provenance missing")
        if require_receiver_receipt:
            receiver = selected_attempt.get("receiver")
            if not isinstance(receiver, Mapping) or receiver.get("ok") is not True:
                errors.append("selected receiver receipt missing")
            else:
                operations = receiver.get("operations")
                required_ops = _receiver_action_order(
                    claude_receiver_topology="runtime-owned",
                    direction="claude-to-codex",
                    phase="post",
                )
                if not isinstance(operations, list) or tuple(operations) != required_ops:
                    errors.append("selected receiver receipt operations mismatch")
    ok = not errors
    return {
        "schema": "pantheon-runtime-initial-viability-check/v1",
        "persona": persona,
        "ok": ok,
        "errors": tuple(errors),
        "state": state,
    }


def _provenance_mapping(provenance: RuntimeSourceProvenance) -> dict[str, str]:
    return {
        "revision": provenance.revision,
        "source_manifest_sha256": provenance.source_manifest_sha256,
        "runtime_start_boundary_at": _iso(provenance.runtime_start_boundary_at),
        "runtime_start_observed_at": _iso(provenance.runtime_start_observed_at),
    }


def _runtime_state_source_provenance(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> dict[str, str]:
    del context
    state = _read_json(_persona_state(binding.persona, state_root) / "runtime.json")
    launch = binding.launch
    if not state or not all(
        (
            state.get("schema") == "pantheon-runtime-state/v1",
            state.get("persona") == binding.persona,
            state.get("role") == binding.role,
            state.get("harness") == launch.harness,
            state.get("profile") == launch.profile,
            state.get("executable") == str(launch.executable),
            state.get("model") == launch.model,
            state.get("tmux_target") == _target_text(binding.controller.tmux),
            isinstance(state.get("runtime_scope_unit"), str),
            bool(state.get("runtime_scope_unit")),
        )
    ):
        raise RuntimeError("selected runtime state missing or mismatched")
    mapping = {
        "revision": state.get("controller_source_revision"),
        "source_manifest_sha256": state.get("controller_source_manifest_sha256"),
        "runtime_start_boundary_at": state.get("runtime_start_boundary_at"),
        "runtime_start_observed_at": state.get("runtime_start_observed_at"),
    }
    if not all(isinstance(value, str) and value for value in mapping.values()):
        raise RuntimeError("selected runtime state source provenance missing")
    return {key: str(value) for key, value in mapping.items()}


def _runtime_state_readiness_evidence(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
) -> Mapping[str, str]:
    state = _read_json(_persona_state(binding.persona, state_root) / "runtime.json")
    readiness: dict[str, str] = {"event": "runtime_started"}
    if not isinstance(state, Mapping):
        return readiness
    provider = state.get("provider_completion")
    if not isinstance(provider, Mapping):
        return readiness
    for key in (
        "provider_completion_status",
        "schema",
        "harness",
        "profile",
        "cause",
        "pattern_id",
        "nonce_sha256",
        "prompt_sha256",
        "statusline_baseline_sha256",
        "statusline_observed_sha256",
        "capture_count",
        "changed_capture_indices",
        "assistant_event_count",
        "probe_resubmit_count",
        "transcript_relative_path_sha256",
        "transcript_start_offset",
        "transcript_end_offset",
    ):
        if key in provider:
            readiness[key] = str(provider[key])
    return readiness


def _viability_payload(receipt: RuntimeViabilityReceipt) -> dict[str, Any]:
    return dataclasses.asdict(receipt)


def _record_initial_viability(receipt: RuntimeViabilityReceipt, *, state_root: Path | None) -> None:
    _atomic_json(_initial_viability_path(receipt.persona, state_root), _viability_payload(receipt))


def _systemd_invocation_id_from_env(env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    if "INVOCATION_ID" not in source:
        return None
    return _validate_systemd_invocation_id(source.get("INVOCATION_ID", ""))


def _ensure_invocation_parent_private(path: Path, *, state_root_path: Path) -> None:
    try:
        relative = path.relative_to(state_root_path)
    except ValueError as exc:
        raise RuntimeError("invocation receipt path escapes state root") from exc
    current = state_root_path
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError("invocation receipt directory contains unsafe component")
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError("invocation receipt directory contains symlink or non-directory")
        if st.st_uid != os.geteuid():
            raise RuntimeError("invocation receipt directory owner mismatch")
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(current, 0o700)


def _read_invocation_payload_no_follow(path: Path) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("invocation receipt is unavailable") from exc
    return _read_invocation_payload_fd(fd)


def _invocation_receipt_payload(receipt: RuntimeViabilityReceipt, invocation_id: str) -> dict[str, Any]:
    return {
        "schema": INITIAL_VIABILITY_INVOCATION_SCHEMA,
        "persona": receipt.persona,
        "invocation_id": _validate_systemd_invocation_id(invocation_id),
        "initial_viability": _viability_payload(receipt),
    }


def _publish_initial_viability_invocation_receipt(
    receipt: RuntimeViabilityReceipt,
    *,
    invocation_id: str,
    state_root: Path | None,
) -> Mapping[str, str]:
    if receipt.status != "selected":
        raise RuntimeError("only selected viability receipts can be invocation witnesses")
    path = _initial_viability_invocation_path(receipt.persona, invocation_id, state_root)
    state_root_path = _state_root(state_root)
    if not state_root_path.is_absolute() or not path.is_absolute():
        raise RuntimeError("invocation receipt state root must be absolute")
    try:
        path.relative_to(state_root_path)
    except ValueError as exc:
        raise RuntimeError("invocation receipt path escapes state root") from exc
    _ensure_invocation_parent_private(path.parent, state_root_path=state_root_path)
    payload = _invocation_receipt_payload(receipt, invocation_id)
    raw = _canonical_bytes(payload) + b"\n"
    if len(raw) > INITIAL_VIABILITY_INVOCATION_MAX_BYTES:
        raise RuntimeError("invocation receipt payload is outside the bounded range")
    parent_fd = _secure_directory_fd(path.parent, create=True)
    tmp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        tmp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                tmp_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing, existing_sha = _read_invocation_payload_no_follow_at(parent_fd, path.name)
            _validate_invocation_payload(
                existing,
                persona=receipt.persona,
                invocation_id=invocation_id,
            )
            expected_sha = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            if existing_sha != expected_sha:
                raise RuntimeError("conflicting invocation receipt already exists")
            return {
                "invocation_id": invocation_id,
                "invocation_receipt_sha256": existing_sha,
                "invocation_receipt_status": "existing-identical",
            }
        os.fsync(parent_fd)
        return {
            "invocation_id": invocation_id,
            "invocation_receipt_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
            "invocation_receipt_status": "created",
        }
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def _candidate_cooldown_open(
    state: Mapping[str, Any],
    runtime: str,
    *,
    now: datetime,
) -> str | None:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping) or attempt.get("runtime") != runtime:
            continue
        cooldown = attempt.get("cooldown")
        if not isinstance(cooldown, Mapping) or str(cooldown.get("opened", "")).lower() != "true":
            continue
        raw_until = cooldown.get("until")
        if not isinstance(raw_until, str) or not raw_until:
            continue
        try:
            until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except ValueError:
            continue
        if until > now:
            return _iso(until)
    return None


def candidate_authoritative_cap(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path,
) -> Mapping[str, str] | None:
    transition = transition_state(binding.persona, state_root=state_root)
    if (
        transition.get("breaker_open") is True
        and transition.get("capped_profile") == binding.launch.profile
    ):
        return {
            "authoritative_limit": "true",
            "source": "runtime-breaker",
            "profile": binding.launch.profile,
        }
    try:
        snapshot = adapter.quota_observer(state_root / binding.persona).read()
    except Exception as exc:
        _append_controller_evidence(
            binding.persona,
            "runtime_viability_quota_observer_unavailable",
            {
                "runtime": _runtime_ref(binding),
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            },
            state_root=state_root,
        )
        return None
    evidence = dict(getattr(snapshot, "evidence", {}) or {})
    if str(evidence.get("authoritative_limit", "false")).lower() == "true":
        return {str(key): str(value) for key, value in evidence.items()} | {
            "source": "quota-observer",
            "profile": str(getattr(snapshot, "profile", _runtime_ref(binding))),
        }
    return None


def _run_post_start_commands(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
) -> None:
    """Run the binding's declared post-runtime-start commands (T-2606 / PRE-4).

    Called once per actual runtime start -- first start AND every restart
    alike, since the callers (``ensure_runtime`` and, since T-4156,
    ``_controller_start_runtime``, both via ``_run_and_assess_post_start``)
    only reach this from the branch that just called ``_start_runtime`` --
    never on a plain reuse/attach of an already-live pane. Persona-neutral: every command, and whether the
    binding has any at all, comes from the manifest via
    ``binding.controller.post_start``; this function holds no Recovery/persona
    hardcoding. An empty ``post_start`` (today's default for every role)
    makes this a no-op loop -- zero subprocess calls, zero evidence writes,
    byte-identical to bringup before this hook existed.

    FAIL-OPEN BY CONTRACT (matches recovery-bot-supervise.sh's ``|| true`` on
    both the pane-log tap and the backgrounded boot-kick it replaces): any
    exception raised while substituting, launching, or waiting on a command
    is caught here and recorded as evidence, never re-raised -- a broken
    post-start command must not fail bringup or kill the runtime that was
    just verified live and input-ready. Foreground commands are additionally
    bounded by ``POST_START_TIMEOUT_SECONDS`` -- a hang is not just an error
    recovery-bot-supervise.sh never had to guard against, since the SPOF guard
    this hook exists to preserve must not itself become a SPOF. Background
    commands are launched via ``Popen`` in their own transient scope
    (``_background_scope_argv``, T-4812) and never waited on, matching
    recovery_boot_kick.sh's backgrounded invocation today.
    """
    if not binding.controller.post_start:
        return
    target_text = _target_text(binding.controller.tmux)
    for command in binding.controller.post_start:
        try:
            argv = [
                token.format(
                    persona=binding.persona,
                    tmux_target=target_text,
                    account_home=str(binding.account_home),
                )
                for token in command.argv
            ]
        except Exception as exc:  # noqa: BLE001 -- T-4173: fail-open by contract
            # Was (KeyError, ValueError) only; a template like "{0}" (IndexError)
            # or "{persona.x}" (AttributeError), or a non-str token, escaped and
            # crashed the controller start after the runtime was verified live.
            _append_post_start_evidence_best_effort(
                binding.persona,
                "post_start_command_skipped",
                lambda exc=exc, command=command: {
                    "argv_template": " ".join(str(token) for token in command.argv),
                    "reason": f"substitution failed: {type(exc).__name__}: {exc}",
                },
                state_root=state_root,
            )
            continue
        try:
            if command.background:
                _launch_background_scoped(
                    binding.persona,
                    argv,
                    cwd=binding.account_home,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                outcome = "launched_background"
            else:
                result = subprocess.run(
                    argv,
                    cwd=binding.account_home,
                    capture_output=True,
                    text=True,
                    timeout=POST_START_TIMEOUT_SECONDS,
                    check=False,
                )
                outcome = "ok" if result.returncode == 0 else f"exit_{result.returncode}"
        except Exception as exc:  # noqa: BLE001 -- fail-open by contract, see docstring
            outcome = f"error: {type(exc).__name__}: {exc}"
        # T-4173: best-effort per command -- an evidence-write OSError (full
        # disk, unwritable state dir) must neither crash the start nor stop the
        # remaining commands from running.
        _append_post_start_evidence_best_effort(
            binding.persona,
            "post_start_command",
            lambda argv=argv, command=command, outcome=outcome: {
                "argv": " ".join(argv),
                "background": str(command.background).lower(),
                "outcome": outcome,
            },
            state_root=state_root,
        )


def _background_scope_argv(persona: str, argv: list[str], unit: str | None = None) -> list[str]:
    """Wrap a background post_start command in its own transient scope (T-4812).

    ``start_new_session`` escapes the process group but not the cgroup. When
    the caller is ``pantheon-persona-consume@.service`` (Type=oneshot,
    KillMode=control-group), systemd kills everything left in that cgroup as
    soon as consume exits, so the artisan boot kick died between typing its text
    and pressing Enter (10-09 17:40Z). ``systemd-run --user --scope`` moves the
    process into its own scope unit, the same escape the runtime pane already
    uses (``_start_new_server_in_scope``). Without systemd-run or a user
    manager (no XDG_RUNTIME_DIR), the argv runs unwrapped as before.
    """
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None or not os.environ.get("XDG_RUNTIME_DIR"):
        return argv
    unit = unit or f"pantheon-post-start-{persona}-{uuid.uuid4().hex[:12]}"
    return [systemd_run, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", *argv]


#: T-5321: how long a background post_start launch waits for its scope to exist.
BACKGROUND_SCOPE_WAIT_SECONDS = 5.0


def _launch_background_scoped(persona: str, argv: list[str], **popen_kwargs: Any) -> subprocess.Popen:
    """Popen ``argv`` in its own transient scope and return once the scope exists (T-5321).

    ``systemd-run --scope`` only moves itself out of the caller's cgroup after its
    StartTransientUnit call completes. Returning straight after Popen left a window
    in which a Type=oneshot caller (pantheon-persona-consume@) could exit and have
    its control group killed with systemd-run still inside it -- the T-4812 failure,
    widened by host load (seen as a flake of the T-4812 regression test, 2026-10-21).
    So wait, bounded, until the unit reports active or the child has already exited.
    Fail-open: on timeout or a systemctl error the command keeps running; we just
    stop waiting.
    """
    unit = f"pantheon-post-start-{persona}-{uuid.uuid4().hex[:12]}"
    wrapped = _background_scope_argv(persona, argv, unit=unit)
    proc = subprocess.Popen(wrapped, **popen_kwargs)
    if wrapped is argv:
        return proc  # unwrapped (no systemd-run / user manager): nothing to wait for
    deadline = time.monotonic() + BACKGROUND_SCOPE_WAIT_SECONDS
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            state = subprocess.run(
                ["systemctl", "--user", "show", "--property=ActiveState", "--value", f"{unit}.scope"],
                capture_output=True, text=True, check=False,
                timeout=max(0.1, min(2.0, deadline - time.monotonic())),
            ).stdout.strip()
        except Exception:  # noqa: BLE001 -- fail-open: stop waiting, never fail the start
            break
        if state == "active":
            break
        time.sleep(0.05)
    return proc


def _append_post_start_evidence_best_effort(
    persona: str,
    event: str,
    payload: Callable[[], dict[str, Any]],
    *,
    state_root: Path | None,
) -> bool:
    """Append post-start evidence without ever raising (T-4173).

    ``payload`` is a thunk so a failure while BUILDING the record is caught
    too. A failed write is reported on stderr (journald for the controller
    unit) and swallowed: post-start is fail-open by contract, and this
    runs after the runtime was already verified live and input-ready.
    Returns whether the record was written.
    """
    try:
        _append_controller_evidence(persona, event, payload(), state_root=state_root)
        return True
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        try:
            print(
                f"runtime_controller: {persona}: {event} evidence write failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 -- stderr itself may be gone
            pass
        return False


def _record_post_start_assessment_error(
    binding: "PersonaBinding",
    *,
    phase: str,
    exc: BaseException,
    state_root: Path | None,
) -> None:
    """Record a non-cap post-start step failure (T-4173). The step is then
    treated as NOT capped and the start continues -- see
    ``_run_and_assess_post_start``."""
    _append_post_start_evidence_best_effort(
        binding.persona,
        "post_start_assessment_error",
        lambda: {
            "phase": phase,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "tmux_target": _target_text(binding.controller.tmux),
            "treated_as": "not-capped",
        },
        state_root=state_root,
    )


def _run_and_assess_post_start(
    binding: "PersonaBinding",
    runtime_start: RuntimeStart,
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Run ``binding.controller.post_start`` after a REAL runtime start, then
    assess the pane (T-4156).

    The single shared post-start step for BOTH start paths -- the consume
    path (``ensure_runtime``) and the stateless controller's start branch
    (``_controller_start_runtime``) -- so a swap-on-death relaunch gets the
    same post-launch duties (pane log, Telegram 409 re-assert, boot kick) as
    a fresh bootstrap, and the two paths cannot drift apart again. Never
    call this on an attach/reuse of an already-live pane: nothing was
    started there, so there is nothing to arm or kick.

    Policy (unchanged from the pre-extraction ``ensure_runtime`` body):
      - the commands themselves are FAIL-OPEN (``_run_post_start_commands``'
        contract: a broken hook never fails the start);
      - the post-command pane ASSESSMENT is FAIL-CLOSED: a subscription-cap
        revealed after the hooks (e.g. by the boot kick's first turn) raises
        ``RuntimeOperationalFailure(failure_class="subscription-cap")``,
        which both callers already route (bringup's selector falls through
        to the next candidate; ``controller_main`` records a
        ``subscription_cap`` cooldown and exits for a Restart=always
        reselect).
      - empty ``post_start`` stays a pure no-op: no baseline capture, no
        assessment, zero subprocess calls, zero evidence writes.

    Crash-proofing (T-4173): this runs AFTER the runtime was verified live
    and input-ready, and on the controller path an escaping exception kills
    controller_main, whose Restart=always relaunch then ATTACHES to the live
    pane (T-4101) with no post-start duties and no cap check. So every
    NON-cap failure here is caught, recorded as a
    ``post_start_assessment_error`` evidence event, and treated as NOT
    capped:
      - baseline capture fails -> fall back to an empty baseline (the same
        empty launch baseline ``_wait_for_runtime_input_ready`` uses) so the
        commands still run AND the post-hook cap check still happens;
      - the commands step raises (it is already fail-open internally; this
        is defense in depth) -> recorded, assessment still runs;
      - sampling/assessment raises -> recorded, start continues uncapped.
    The subscription-cap ``RuntimeOperationalFailure`` from
    ``_raise_for_post_start_assessment`` is raised OUTSIDE these guards and
    propagates unchanged.
    """
    if not binding.controller.post_start:
        _run_post_start_commands(binding, state_root=state_root)
        return
    try:
        baseline = _post_start_sample(
            _capture_visible_runtime_pane(binding.controller.tmux, **_context_kwargs(context)),
            capture_index=0,
        )
    except Exception as exc:  # noqa: BLE001 -- T-4173 crash-proofing, see docstring
        _record_post_start_assessment_error(
            binding, phase="baseline_capture", exc=exc, state_root=state_root
        )
        baseline = _post_start_sample("", capture_index=0)
    try:
        _run_post_start_commands(binding, state_root=state_root)
    except Exception as exc:  # noqa: BLE001 -- T-4173 crash-proofing, see docstring
        _record_post_start_assessment_error(
            binding, phase="commands", exc=exc, state_root=state_root
        )
    try:
        assessment = _assess_post_start_after_commands(
            binding,
            adapter,
            baseline=baseline,
            context=context,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 -- T-4173 crash-proofing, see docstring
        _record_post_start_assessment_error(
            binding, phase="assessment", exc=exc, state_root=state_root
        )
        return
    _raise_for_post_start_assessment(binding, runtime_start, assessment)


def ensure_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
    receiver_backend: Any | None = None,
    stop_prior_receiver: bool = True,
) -> RuntimeHandle:
    """Reuse an exact matching live pane or start the selected interactive profile.

    T-4179: before a real launch (never on live-pane reuse), a comms persona
    launching a non-codex runtime has its codex Telegram poller proven absent
    (probe, then stop + prove-absent if not proven), fail-closed as
    RuntimeViabilityBlocked. `receiver_backend` is the test seam; production
    passes None (KEEP bridge subprocess).

    ``stop_prior_receiver=False`` opts out, for a caller that owns the receiver
    handoff itself: the continuity switch coordinator starts an ISOLATED
    candidate while the old seat (and its receiver) stays authoritative, moves
    the receiver only in commit_transfer (transition_runtime_with_receiver_handoff
    pre/post), and its rollback restarts the old runtime via ensure_runtime,
    which reuses a live old pane and never restarts a stopped poller."""

    transition = transition_state(binding.persona, state_root=state_root)
    if (
        transition.get("breaker_open") is True
        and transition.get("capped_profile") == binding.launch.profile
    ):
        _append_controller_evidence(
            binding.persona,
            "breaker_blocked_capped_runtime",
            {
                "harness": binding.launch.harness,
                "provider": binding.launch.harness,
                "profile": binding.launch.profile,
                "capped_profile": transition.get("capped_profile"),
                "tmux_target": _target_text(binding.controller.tmux),
            },
            state_root=state_root,
        )
        raise ProviderQuotaExhausted(
            f"runtime profile {binding.launch.profile} has an open quota breaker",
            profile=binding.launch.profile,
        )
    state_path = _persona_state(binding.persona, state_root) / "runtime.json"
    state = _read_json(state_path)
    live = pane_exists(binding.controller.tmux, **_context_kwargs(context))
    if live:
        if not state or not _state_matches(binding, state):
            raise RuntimeError(
                "live tmux pane does not match the selected persona runtime profile"
            )
        server_pid = _default_tmux_server_pid(**_context_kwargs(context))
        if server_pid is None or not _pid_in_scope(
            server_pid, str(state["runtime_scope_unit"])
        ):
            raise RuntimeError("live tmux server is not bound to its recorded scope")
        return _handle_from_state(binding, state)
    # T-5021: an operator hold (`harness <persona> hold`) makes a runtime
    # unselectable on BOTH planes. Reusing a live pane above is not a launch;
    # every consume/wake launch funnels through here, so refuse a held one.
    _refuse_held_runtime_launch(binding, state_root=state_root)
    if state and _state_matches(binding, state):
        # A dead pane cannot inherit health from matching persisted metadata.
        state = {}
    # T-4179 CHOKEPOINT: every consume/wake launch of a runtime funnels here
    # (the initial-viability loop, the planned-swap source ensure, the
    # continuity coordinator). A codex poller started by a consume planned_swap
    # outlives its dead pane, and the record may be cleared or drifted, so the
    # evidence is record + a read-only probe. Refusal is RuntimeViabilityBlocked
    # (exit-5 policy skip), deliberately NOT RuntimeOperationalFailure: the
    # viability loop would cascade that to the codex candidate + compensate.
    if stop_prior_receiver:
        _stop_prior_harness_receiver_before_start(
            binding,
            live_argv=None,
            state_root=state_root,
            backend=receiver_backend,
            probe=True,
            refusal="viability-blocked",
        )
    boundary = _utc_now()
    try:
        start_kwargs: dict[str, Any] = {"replace": False, "state_root": state_root}
        if context is not None:
            start_kwargs["context"] = context
        runtime_start = _start_runtime(binding, adapter, **start_kwargs)
    except (RuntimeOperationalFailure, RuntimeViabilityBlocked):
        raise
    except RuntimeError as exc:
        raise RuntimeOperationalFailure(
            "runtime launch failed",
            failure_class="operational-launch",
            cause="runtime_launch_failed",
            binding_runtime=_runtime_ref(binding),
            runtime_start=None,
            created_scope=False,
            pane_may_be_mutated=False,
            readiness_evidence={"event": "runtime_launch_failed", "detail": str(exc)[:1000]},
        ) from exc
    try:
        if context is None:
            _verify_started_runtime(binding, runtime_start, state_root=state_root)
        else:
            _verify_started_runtime(binding, runtime_start, state_root=state_root, context=context)
    except RuntimeOperationalFailure:
        raise
    except RuntimeError as exc:
        if runtime_start.created_scope:
            _stop_created_scope_best_effort(runtime_start.scope_unit)
        raise RuntimeOperationalFailure(
            "runtime started verification failed",
            failure_class="operational-launch",
            cause="runtime_started_verification_failed",
            binding_runtime=_runtime_ref(binding),
            runtime_start=runtime_start,
            created_scope=runtime_start.created_scope,
            pane_may_be_mutated=True,
            readiness_evidence={
                "event": "runtime_started_verification_failed",
                "detail": str(exc)[:1000],
                "runtime_scope_unit": runtime_start.scope_unit,
            },
        ) from exc
    if context is None:
        _verify_runtime_input_ready(
            binding, runtime_start, state_root=state_root, adapter=adapter, launched_at=boundary
        )
    else:
        _verify_runtime_input_ready(
            binding,
            runtime_start,
            state_root=state_root,
            adapter=adapter,
            context=context,
            launched_at=boundary,
        )
    provider_completion = _assess_provider_completion_after_input_ready(
        binding,
        adapter,
        state_root=state_root,
        context=context,
    )
    _raise_for_provider_completion_assessment(binding, runtime_start, provider_completion)
    _run_and_assess_post_start(
        binding, runtime_start, adapter, state_root=state_root, context=context
    )
    started = _utc_now()
    provenance = _controller_source_provenance(
        runtime_start_boundary_at=boundary,
        runtime_start_observed_at=started,
    )
    _atomic_json(
        state_path,
        _runtime_payload(
            binding,
            session_id=None,
            started_at=started,
            runtime_scope_unit=runtime_start.scope_unit,
            source_provenance=provenance,
            provider_completion=_provider_completion_evidence(provider_completion),
        ),
    )
    _note_launch_breach(binding.persona, _runtime_ref(binding), state_root=state_root)
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=started,
        created_scope=runtime_start.created_scope,
    )


def _append_held_evidence_once(
    persona: str,
    event: str,
    payload: dict[str, Any],
    *,
    state_root: Path | None,
) -> None:
    """T-5021: a held-skip evidence row once per (event, runtime) per hold-set
    change, not on every consume fire. The hold set's signature is its runtimes
    and their set_ts, so a re-hold (new set_ts) logs again."""
    holds = load_holds(persona, state_root=state_root)
    signature = {
        str(ref): (info.get("set_ts") if isinstance(info, Mapping) else None)
        for ref, info in sorted(holds.items())
    }
    path = _persona_state(persona, state_root) / "held-skip-evidence.json"
    seen = _read_json(path)
    key = f"{event}:{payload.get('runtime', '')}"
    if seen.get(key) == signature:
        return
    _append_controller_evidence(persona, event, payload, state_root=state_root)
    seen[key] = signature
    _atomic_json(path, seen)


#: ORG-106 L3a: coarse consume policy-skip token and evidence cause for a deep-sleeping seat.
SEAT_SLEEP_POLICY_SKIP_REASON = "seat_sleep"
SEAT_SLEEP_CAUSE = "seat-sleep"
#: Controller wait between re-reads of a sleeping seat's record (it launches nothing).
SEAT_SLEEP_RECHECK_SECONDS = 60.0


def _seat_launch_block(persona: str, *, state_root: Path | None, now: datetime | None = None) -> Any:
    """ORG-106 L3a: the shared launch predicate (tools/seat_sleep.launch_block) for the
    plane whose personas dir is `state_root`. Fail-open: never raises."""
    from tools import seat_sleep

    return seat_sleep.launch_block(
        persona, root=seat_sleep.seat_root_for(_state_root(state_root)), now=now
    )


def _seat_sleep_block_error(persona: str, runtime: str, block: Any) -> "RuntimeViabilityBlocked":
    return RuntimeViabilityBlocked(
        SEAT_SLEEP_CAUSE,
        {
            "schema": "pantheon-runtime-viability-block/v1",
            "persona": persona,
            "reason": SEAT_SLEEP_CAUSE,
            "runtime": runtime,
            "owner": block.owner or "",
            "expires": block.expires or "",
        },
        policy_skip_reason=SEAT_SLEEP_POLICY_SKIP_REASON,
    )


def _refuse_sleeping_seat_launch(
    binding: "PersonaBinding", *, state_root: Path | None, observe_oom: bool = False
) -> None:
    """ORG-106 L3a: THE launch refusal for a deep-sleeping seat. Raises
    RuntimeViabilityBlocked (policy skip ``seat_sleep``, cause ``seat-sleep``) on an
    unexpired deep-sleep record; fail-open on a reader fault. Called by the chokepoint
    (`_refuse_held_runtime_launch`), the lowest launch primitive (`_start_runtime`) and
    `_controller_start_runtime`. With `observe_oom`, an ORG-104 OOM hold is only
    OBSERVED (evidence ``launch_attempt_oom_held``; ORG-104 rule 2 is enforced at consume,
    T-5526 ruling): a launch that then succeeds pages as ``launch_breach``."""
    block = _seat_launch_block(binding.persona, state_root=state_root)
    if block.sleeping:
        _best_effort_controller_evidence(
            binding.persona, "runtime_launch_refused_seat_sleep",
            {"runtime": _runtime_ref(binding), "cause": SEAT_SLEEP_CAUSE,
             "owner": block.owner or "", "expires": block.expires or ""},
            state_root=state_root,
        )
        raise _seat_sleep_block_error(binding.persona, _runtime_ref(binding), block)
    if observe_oom and block.oom_held:
        _best_effort_controller_evidence(
            binding.persona, "launch_attempt_oom_held",
            {"runtime": _runtime_ref(binding), "owner": block.owner or "", "reason": block.reason or ""},
            state_root=state_root,
        )
    if block.fault:
        try:
            print(f"[runtime_controller] {binding.persona}: seat-state fault (launch allowed): "
                  f"{block.fault}"[:400], file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def _note_launch_breach(
    persona: str, runtime_ref: str, *, state_root: Path | None, now: datetime | None = None
) -> None:
    """ORG-106 L3a tripwire: a launch just SUCCEEDED. If the reader still says
    launch_blocked (deep-sleep, or an OOM hold), some launcher bypassed the chokepoint (or
    the state changed mid-launch): evidence ``launch_breach`` + a cooldown-gated page.
    Never raises."""
    try:
        from tools import seat_sleep

        block = _seat_launch_block(persona, state_root=state_root)
        if block.cause is None and not block.oom_held:
            return
        _best_effort_controller_evidence(
            persona, "launch_breach",
            {"runtime": runtime_ref, "deep_sleep": block.sleeping, "oom_held": block.oom_held,
             "owner": block.owner or "", "reason": block.reason or ""},
            state_root=state_root,
        )
        _route_cooldown_gated_alert(
            persona, seat_sleep.breach_message(persona, block), f"launch-breach ({persona})",
            stamp_name="launch-breach-alert.json", state_root=state_root, now=now,
        )
    except Exception:  # noqa: BLE001 - the tripwire must never abort a live start
        pass


def _refuse_held_runtime_launch(binding: "PersonaBinding", *, state_root: Path | None) -> None:
    """T-5021: raise RuntimeViabilityBlocked (policy skip ``runtime_held``) if an
    operator hold names `binding`'s runtime. Same holds file the controller reads.
    ORG-106 L3a: also the deep-sleep refusal (`_refuse_sleeping_seat_launch`)."""
    _refuse_sleeping_seat_launch(binding, state_root=state_root, observe_oom=True)
    if _runtime_ref(binding) not in load_holds(binding.persona, state_root=state_root):
        return
    block = RuntimeViabilityBlocked(
        "runtime-held",
        {
            "schema": "pantheon-runtime-viability-block/v1",
            "persona": binding.persona,
            "reason": "runtime-held",
            "runtime": _runtime_ref(binding),
        },
        policy_skip_reason="runtime_held",
    )
    try:
        _append_held_evidence_once(
            binding.persona,
            "runtime_launch_refused_held",
            {"runtime": _runtime_ref(binding)},
            state_root=state_root,
        )
    except Exception:  # noqa: BLE001 -- the refusal itself must not depend on the log
        pass
    raise block


def _unique_candidates(candidates: Sequence["PersonaBinding"]) -> tuple["PersonaBinding", ...]:
    result: list["PersonaBinding"] = []
    seen: set[str] = set()
    for candidate in candidates:
        runtime = _runtime_ref(candidate)
        if runtime in seen:
            continue
        seen.add(runtime)
        result.append(candidate)
    return tuple(result)


def _ordered_initial_candidates(
    candidates: Sequence["PersonaBinding"],
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None,
) -> tuple["PersonaBinding", ...]:
    del state_root, context
    return _unique_candidates(candidates)


def _viability_attempt(
    *,
    sequence: int,
    binding: "PersonaBinding",
    status: Literal["selected", "failed", "skipped-cooldown", "skipped-held", "skipped-recorded-sticky"],
    failure_class: RuntimeFailureClass | None,
    cause: str,
    readiness: Mapping[str, str],
    teardown: Mapping[str, str],
    receiver: Mapping[str, Any],
    cooldown: Mapping[str, str],
    source_provenance: Mapping[str, str],
    precheck_source_provenance: Mapping[str, str] | None = None,
) -> RuntimeViabilityAttempt:
    return RuntimeViabilityAttempt(
        sequence=sequence,
        runtime=_runtime_ref(binding),
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        status=status,
        failure_class=failure_class,
        cause=cause,
        readiness=dict(readiness),
        teardown=dict(teardown),
        receiver=dict(receiver),
        cooldown=dict(cooldown),
        source_provenance=dict(source_provenance),
        precheck_source_provenance=dict(precheck_source_provenance or {}),
    )


def _runtime_viability_receipt(
    *,
    persona: str,
    status: Literal["selected", "exhausted", "reused-recorded"],
    selected: "PersonaBinding | None",
    manifest_order: tuple[str, ...],
    attempt_limit: int,
    attempts: list[RuntimeViabilityAttempt],
    now: datetime,
) -> RuntimeViabilityReceipt:
    return RuntimeViabilityReceipt(
        schema="pantheon-runtime-initial-viability/v1",
        persona=persona,
        status=status,
        selected_runtime=_runtime_ref(selected) if selected is not None else None,
        selected_harness=selected.launch.harness if selected is not None else None,
        selected_profile=selected.launch.profile if selected is not None else None,
        manifest_order=manifest_order,
        attempt_limit=attempt_limit,
        attempts=tuple(attempts),
        recorded_at=_iso(now),
    )


def run_initial_bootstrap_receiver_boundary(
    previous_failed: "PersonaBinding | None",
    selected: "PersonaBinding",
    *,
    phase: Literal["pre-candidate", "post-selected", "compensate-selected"],
    backend: Any | None = None,
    reason: str = "initial-runtime-viability",
) -> ReceiverHandoffReceipt:
    account = str(getattr(selected, "account", "") or _binding_account(selected))
    if phase == "pre-candidate":
        if previous_failed is not None and receiver_handoff_required(previous_failed):
            _prove_receiver(previous_failed, "absent", backend=backend)
        return ReceiverHandoffReceipt(
            schema="pantheon-codex-telegram-handoff/v1",
            persona=selected.persona,
            account=account,
            direction="initial-bootstrap",
            phase="pre",
            ok=True,
            operations=(),
        )
    if (
        phase == "post-selected"
        and selected.launch.harness == "codex"
        and receiver_handoff_required(selected)
    ):
        if previous_failed is not None and receiver_handoff_required(previous_failed):
            _prove_receiver(previous_failed, "absent", backend=backend)
        _prove_receiver(selected, "absent", backend=backend)
        return _run_integration_handoff(
            selected,
            "claude-to-codex",
            "post",
            backend=backend,
            reason=reason,
        )
    if (
        phase == "compensate-selected"
        and selected.launch.harness == "codex"
        and receiver_handoff_required(selected)
    ):
        return _run_integration_handoff(
            selected,
            "codex-to-claude",
            "pre",
            backend=backend,
            reason=f"{reason}:compensate",
        )
    return ReceiverHandoffReceipt(
        schema="pantheon-codex-telegram-handoff/v1",
        persona=selected.persona,
        account=account,
        direction="none",
        phase="post" if phase == "post-selected" else "pre",
        ok=True,
        operations=(),
    )


def _runtime_pane_existed(binding: Any) -> bool:
    """Was the candidate's pane already up before ensure_runtime ran? A probe
    that can't answer reads as yes, so a doubt never stops a scope (T-4338).
    Same (default) tmux context ensure_runtime is called with here."""
    try:
        return bool(pane_exists(binding.controller.tmux))
    except Exception:
        return True


def _record_refused_scope_stop(
    binding: Any,
    teardown: dict[str, str],
    exc: ScopeStopRefused,
    *,
    reason: str,
    state_root: Path | None,
) -> None:
    """T-4338: a candidate whose scope won't stop is still running, so the
    teardown fails loud instead of recording a normal failed attempt and moving
    on. That would launch the next candidate next to a live one. runtime.json is
    left alone because it still describes that live pane. What the teardown
    record gets is the refusal."""
    teardown["scope_stopped"] = "false"
    teardown["scope_stop_error"] = str(exc)[:500]
    _append_controller_evidence(
        binding.persona,
        "runtime_viability_candidate_teardown",
        {"runtime": _runtime_ref(binding), "reason": reason, **teardown},
        state_root=state_root,
    )


def _stop_scope_this_attempt_created(
    binding: Any,
    handle: RuntimeHandle,
    *,
    launched_at: datetime,
    existed_before: bool,
    state_root: Path | None,
) -> dict[str, str]:
    """T-4336: stop the runtime scope a failed receiver handoff leaves behind, as
    compensate_failed_initial_candidate does on the other failure paths. Without
    this, a codex pane whose poller never started stays up, deaf to Telegram,
    until something else rolls it back (gate A, during a hand-move).

    Only a scope THIS attempt created: ensure_runtime can also attach to a live
    runtime (T-4101), and that one is not ours to stop. `existed_before` (the pane
    was up before ensure_runtime ran) and the handle's exact ``created_scope``
    result decide that. ``started_at`` is a further guard: _handle_from_state
    falls back to now() for a missing or unparseable value, which would make an
    attached runtime look new (T-4338). The scope is the one ensure_runtime
    recorded. Raises ScopeStopRefused."""
    if existed_before or handle.started_at < launched_at:
        return {"scope_stopped": "false", "scope_stop_skipped": "attached-existing-runtime"}
    scope = _read_json(_persona_state(binding.persona, state_root) / "runtime.json").get("runtime_scope_unit")
    if not isinstance(scope, str) or not scope:
        return {"scope_stopped": "false", "scope_stop_skipped": "no-recorded-scope"}
    if handle.created_scope is not True:
        return {"scope_stopped": "false", "scope_stop_skipped": "attached-existing-runtime"}
    _stop_created_scope(scope)
    return {"scope_stopped": "true", "runtime_scope_unit": scope}


# T-4332: a claude candidate's Telegram plugin runs INSIDE the claude process
# (runtime-owned). Once it starts it can fetch operator messages and confirm them
# to Telegram before any turn reads them, so tearing the candidate down loses
# them, and Telegram never redelivers a confirmed update (recovery's T-3026 attempt
# 1: the candidate's bot.pid 3293548 was live at 20:00:12Z, the candidate was torn
# down at 20:01:02Z with no transcript, and 7847/7848 reached no session). That
# can't be undone here, so the teardown makes it visible instead.
CANDIDATE_RECEIVER_NOTICE_SENDER = "runtime-controller"
# File mtimes come from the kernel's coarse clock, which can read a few ms behind
# datetime.now(), so a bot.pid written just after the boundary can look older.
# The plugin really starts seconds after launch; an earlier session's bot.pid
# dates from that session's start, so 1s of slack can't misattribute one.
_BOT_PID_CLOCK_SLACK = timedelta(seconds=1)


def _candidate_receiver_window(
    binding: Any,
    *,
    launched_at: datetime | None,
    now: datetime,
) -> dict[str, str] | None:
    """When a torn-down claude candidate's own Telegram plugin could have taken
    operator messages, or None if it never started.

    The plugin writes ~/.claude/channels/telegram/bot.pid when it starts, so a
    bot.pid written at or after the candidate's launch boundary proves THIS
    candidate's plugin ran. That survives a crash or SIGKILL, but NOT a clean
    shutdown: on SIGTERM the plugin removes its own bot.pid (server.ts shutdown()).
    So a teardown path that stops the scope before compensate runs must read the
    window first and carry it (_verify_runtime_input_ready does, see
    _carried_receiver_window). An older bot.pid belongs to an earlier session. An
    unreadable (not missing) one is "can't tell", reported as a window, never as no."""
    if launched_at is None or not receiver_handoff_required(binding):
        return None
    if binding.launch.harness != "claude":
        return None
    if binding.communications.get("claude_receiver_topology", "systemd-user") != "runtime-owned":
        return None  # a systemd-user receiver is not torn down with the candidate
    path = Path(binding.account_home) / ".claude" / "channels" / "telegram" / "bot.pid"
    try:
        started = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {
            "receiver_window_start": "unknown",
            "receiver_window_end": _iso(now),
            "receiver_window_detail": f"bot.pid unreadable: {type(exc).__name__}",
        }
    if started < launched_at - _BOT_PID_CLOCK_SLACK:
        return None
    try:
        bot_pid = path.read_text(encoding="utf-8").strip()[:32]
    except OSError:
        bot_pid = "unreadable"
    return {
        "receiver_window_start": _iso(started),
        "receiver_window_end": _iso(now),
        "receiver_bot_pid": bot_pid,
    }


def _candidate_receiver_window_or_unknown(
    binding: Any,
    *,
    launched_at: datetime | None,
    now: datetime,
) -> dict[str, str] | None:
    """_candidate_receiver_window that never raises (T-4356, rev-t4332 r2 N-a).

    Both callers stop the candidate's scope right after this read. A raise that
    isn't an OSError (fromtimestamp's ValueError/OverflowError on an absurd mtime)
    escaped past the stop and left the candidate live, with no notice and not as a
    RuntimeOperationalFailure. Any fault degrades to an "unknown" window: can't
    tell, reported as a window, never as no (a false notice is cheap, a missed one
    is not)."""
    try:
        return _candidate_receiver_window(binding, launched_at=launched_at, now=now)
    except Exception as exc:
        return {
            "receiver_window_start": "unknown",
            "receiver_window_end": _iso(now),
            "receiver_window_detail": f"window read failed: {type(exc).__name__}",
        }


def _carried_receiver_window(failure: RuntimeOperationalFailure) -> dict[str, str] | None:
    """The window a failing step read before its own pre-raise scope stop
    (T-4332 r2), when compensate's read comes too late to see bot.pid."""
    evidence = failure.readiness_evidence
    if not evidence.get("receiver_window_start"):
        return None
    return {
        key: str(value)
        for key, value in evidence.items()
        if key.startswith("receiver_window_") or key == "receiver_bot_pid"
    }


def _notify_candidate_receiver_window(binding: Any, window: Mapping[str, str], *, reason: str) -> str:
    """ONE notice per teardown, to the seat's own inbox: the seat's next runtime
    is the one that can ask the operator to resend. Never raises; returns the
    outcome for the evidence record."""
    text = (
        f"runtime-controller {binding.persona}: the failed {_runtime_ref(binding)} candidate "
        f"({reason}, being torn down) had its own Telegram plugin running from {window['receiver_window_start']} "
        f"to {window['receiver_window_end']}. Operator messages to this seat's bot in that window "
        f"may be LOST: the plugin can confirm an update to Telegram before any turn reads it, and "
        f"Telegram never redelivers one. Ask the operator to resend anything sent then (T-4332)."
    )
    try:
        from tools import mailbox

        mailbox.send(
            binding.mailbox,
            {"kind": "candidate_receiver_window", "runtime": _runtime_ref(binding), "reason": reason,
             **window, "text": text},
            CANDIDATE_RECEIVER_NOTICE_SENDER,
            subject=text[:120],
        )
        return "sent"
    except Exception as exc:  # the teardown must finish whatever happens here
        return f"failed: {type(exc).__name__}: {exc}"[:300]


def compensate_failed_initial_candidate(
    binding: "PersonaBinding",
    *,
    failure: RuntimeOperationalFailure,
    state_root: Path | None,
    backend: Any | None = None,
    context: TmuxRuntimeContext | None = None,
    reason: str,
    launched_at: datetime | None = None,
) -> Mapping[str, str]:
    del context
    teardown: dict[str, str] = {
        "runtime_state_cleared": "false",
        "scope_stopped": "false",
        "receiver_absent_proven": "false",
    }
    receiver_window = _candidate_receiver_window_or_unknown(
        binding, launched_at=launched_at, now=_utc_now())
    if receiver_window is None:
        # A step that stopped the scope before raising (input-readiness) already
        # read the window while bot.pid still existed.
        receiver_window = _carried_receiver_window(failure)
    if receiver_window is not None:
        teardown.update(receiver_window)
        # The notice goes FIRST, ahead of the scope stop, the state clear and the
        # absence proof: any of them can raise, and a false "torn down" notice is
        # cheap where a missed one is not.
        teardown["receiver_window_notice"] = _notify_candidate_receiver_window(
            binding, receiver_window, reason=reason
        )
    if failure.runtime_start is not None:
        teardown["runtime_scope_unit"] = failure.runtime_start.scope_unit
        if failure.runtime_start.created_scope is not True:
            # T-5167: a start that REUSED a live server's scope did not create it, and
            # that scope holds the whole tmux server, other sessions included. Same
            # rule as _stop_scope_this_attempt_created and the start-path stops.
            teardown["scope_stop_skipped"] = "reused-existing-scope"
        else:
            try:
                _stop_created_scope(failure.runtime_start.scope_unit)
            except ScopeStopRefused as exc:
                _record_refused_scope_stop(binding, teardown, exc, reason=reason, state_root=state_root)
                raise
            teardown["scope_stopped"] = "true"
    _clear_stale_runtime_state(binding.persona, state_root=state_root)
    teardown["runtime_state_cleared"] = "true"
    # A mailbox-only persona (communications is None, per ORG-043) has no managed
    # receiver to prove absent; _prove_receiver would descend into the telegram
    # bridge and raise ManifestError. Gate on receiver_handoff_required so the
    # teardown short-circuits regardless of `backend` — the invariant "no
    # receiver => never prove one" must hold even when a backend is injected,
    # which is why this is tighter than _probe_managed_receiver's `backend is
    # None and not receiver_handoff_required` gate (T-3826; T-3819 incident).
    if receiver_handoff_required(binding):
        try:
            _prove_receiver(binding, "absent", backend=backend)
            teardown["receiver_absent_proven"] = "true"
        except Exception as exc:
            teardown["receiver_absent_proven"] = "false"
            teardown["receiver_absent_error"] = f"{type(exc).__name__}: {exc}"[:1000]
            raise
    else:
        teardown["receiver_absent_proven"] = "not-applicable"
    _append_controller_evidence(
        binding.persona,
        "runtime_viability_candidate_teardown",
        {
            "runtime": _runtime_ref(binding),
            "reason": reason,
            **teardown,
        },
        state_root=state_root,
    )
    return teardown


def _receiver_proof_not_applicable(binding: "PersonaBinding") -> bool:
    """T-5282: True when no receiver handoff can ever happen for `binding`, decided
    from the manifest-derived binding alone (no I/O, no exception-catching).

    No managed receiver (`receiver_handoff_required` False: no bridge entry) means
    "present" has nothing to prove, the T-3826 invariant "no receiver => never prove
    one". The one exception stays fail-closed: a seat that DECLARES a Telegram
    channel AND has a cross-harness fallback is a seat that can hand a poller to
    another harness, so a missing bridge entry there is a broken binding, not an
    absent receiver, and the bridge proof must still run (and raise)."""
    if receiver_handoff_required(binding):
        return False
    channels = str((getattr(binding.launch, "native", None) or {}).get("channels", ""))
    if "telegram" in channels:
        harness = str(binding.launch.harness)
        fallbacks = getattr(getattr(binding.controller, "policy", None), "fallbacks", ()) or ()
        if any(str(ref).split("/", 1)[0] != harness for ref in fallbacks):
            return False
    return True


def _read_only_receiver_present(binding: "PersonaBinding", *, backend: Any | None) -> Mapping[str, Any]:
    if _receiver_proof_not_applicable(binding):
        return {"ok": True, "state": "not-applicable", "evidence": {"reason": "no-managed-receiver"}}
    integration = harness_integration.integration_for(binding.launch.harness)
    proof = integration.prove_receiver_state(binding, "present", backend=backend)
    if proof.state == "not-applicable":
        return {"ok": True, "state": "not-applicable", "evidence": dict(proof.evidence)}
    if proof.state == "absent":
        return {"ok": False, "state": "absent", "evidence": dict(proof.evidence)}
    if proof.state != "present":
        raise RuntimeError(f"receiver state is {proof.state}; expected present")
    return {"ok": True, "state": "present", "evidence": dict(proof.evidence)}


def _recorded_scope_inactive(
    runtime_state: Mapping[str, Any],
    *,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    scope = runtime_state.get("runtime_scope_unit")
    if not isinstance(scope, str) or not scope:
        raise RuntimeViabilityBlocked(
            "recorded-scope-missing",
            {"schema": "pantheon-runtime-viability-block/v1", "reason": "recorded-scope-missing"},
        )
    try:
        server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    except RuntimeError as exc:
        raise RuntimeViabilityBlocked(
            "recorded-scope-absence-ambiguous",
            {
                "schema": "pantheon-runtime-viability-block/v1",
                "reason": "recorded-scope-absence-ambiguous",
                "scope_error": f"{type(exc).__name__}: {exc}"[:500],
            },
        ) from exc
    if server_pid is None:
        return True
    return not _pid_in_scope(server_pid, scope)


def _authenticate_recorded_selected_runtime(
    selected_binding: "PersonaBinding",
    runtime_state: Mapping[str, Any],
    *,
    state_root: Path,
    backend: Any | None,
    context: TmuxRuntimeContext | None,
    allow_source_drift: bool = False,
) -> tuple[Mapping[str, str], Mapping[str, str], Mapping[str, Any], str]:
    """Authenticate the recorded runtime against the current controller source.

    ``allow_source_drift`` (T-5292) is for the dead-pane rehydrate only: it LAUNCHES a
    fresh session through the current code, so a WELL-FORMED hash/manifest mismatch is
    benign there and is reported as mode ``source-drift-rerecorded`` (the caller records
    current provenance). Missing/malformed/inconsistent recorded hashes, receiver
    state, and unavailable current provenance still block. The live-pane reuse path
    never sets it: reusing a live session claims source equivalence."""
    try:
        receiver = _read_only_receiver_present(selected_binding, backend=backend)
    except Exception as exc:
        _raise_runtime_viability_block(
            "recorded-receiver-unknown",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"receiver_error": f"{type(exc).__name__}: {exc}"[:500]},
        )
    if receiver.get("ok") is not True:
        _raise_runtime_viability_block(
            "recorded-receiver-not-present",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"receiver_state": str(receiver.get("state", ""))},
        )
    try:
        provenance = _runtime_state_source_provenance(
            selected_binding,
            state_root=state_root,
            context=context,
        )
        boundary = _utc_now()
        current_source = _controller_source_provenance(
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=boundary,
        )
        precheck = _provenance_mapping(current_source)
        recorded_hashes = _validated_source_hashes_from_state(runtime_state)
        current_hashes = _validated_current_source_hashes(current_source)
    except ValueError as exc:
        _raise_runtime_viability_block(
            str(exc),
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    except RuntimeError as exc:
        _raise_runtime_viability_block(
            "current-source-provenance-unavailable",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"source_error": f"{type(exc).__name__}: {exc}"[:500]},
        )
    drifted = recorded_hashes != current_hashes
    if drifted and not allow_source_drift:
        # Strict (non-drift-tolerant) caller: keep the pre-T-5315 reason for a
        # path-list difference so dashboards and markers keep their meaning.
        _raise_runtime_viability_block(
            "recorded-source-key-mismatch"
            if set(recorded_hashes) != set(current_hashes)
            else "recorded-source-hash-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    recorded_manifest = str(runtime_state.get("controller_source_manifest_sha256", ""))
    if not drifted and recorded_manifest != current_source.source_manifest_sha256:
        _raise_runtime_viability_block(
            "recorded-source-manifest-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    if drifted:
        equivalence_mode = "source-drift-rerecorded"
    else:
        equivalence_mode = (
            "same-revision"
            if provenance.get("revision") == precheck.get("revision")
            else "ticket-only-revision"
        )
    return provenance, precheck, receiver, equivalence_mode


def _rehydrate_recorded_runtime_selection(
    selected_binding: "PersonaBinding",
    runtime_state: Mapping[str, Any],
    *,
    adapter_factory: Callable[["PersonaBinding"], "PantheonHarnessAdapter"],
    state_root: Path,
    backend: Any | None,
    context: TmuxRuntimeContext | None,
    now: datetime,
    manifest_order: tuple[str, ...],
    attempt_limit: int,
) -> RuntimeViabilitySelection:
    if not _recorded_scope_inactive(runtime_state, context=context):
        _raise_runtime_viability_block(
            "recorded-scope-still-active",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    if not prove_managed_receiver_present(selected_binding):
        _raise_runtime_viability_block(
            "recorded-receiver-not-present",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    if not prove_predecessor_receiver_absent(selected_binding):
        _raise_runtime_viability_block(
            "predecessor-receiver-present",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    provenance, precheck, receiver, equivalence_mode = _authenticate_recorded_selected_runtime(
        selected_binding,
        runtime_state,
        state_root=state_root,
        backend=backend,
        context=context,
        allow_source_drift=True,
    )
    source_drift = equivalence_mode == "source-drift-rerecorded"
    if source_drift:
        try:
            _append_controller_evidence(
                selected_binding.persona,
                "runtime_rehydrate_source_drift",
                {
                    "runtime": _runtime_ref(selected_binding),
                    "recorded_revision": str(runtime_state.get("controller_source_revision", "")),
                    "recorded_manifest_sha256": str(
                        runtime_state.get("controller_source_manifest_sha256", "")
                    ),
                    "current_revision": str(precheck.get("revision", "")),
                    "current_manifest_sha256": str(precheck.get("source_manifest_sha256", "")),
                    # T-5315: a RUNTIME_SOURCE_HASH_PATHS change is drift too; say so.
                    **_source_key_set_delta(
                        _validated_source_hashes_from_state(runtime_state),
                        dict.fromkeys(RUNTIME_SOURCE_HASH_PATHS),
                    ),
                },
                state_root=state_root,
            )
        except Exception:  # noqa: BLE001 -- the relaunch must not depend on the log
            pass
    # T-4179: rehydrate calls _start_runtime directly (not via ensure_runtime),
    # so it runs the same stop-then-prove as the chokepoint, after the record is
    # authenticated and before the launch. Probing and blocking alone would
    # leave a consume-only seat blocked on every fire while the poller runs.
    _stop_prior_harness_receiver_before_start(
        selected_binding,
        live_argv=None,
        state_root=state_root,
        backend=backend,
        probe=True,
        refusal="viability-blocked",
    )
    adapter = adapter_factory(selected_binding)
    old_raw = (_persona_state(selected_binding.persona, state_root) / "runtime.json").read_bytes()
    started: RuntimeStart | None = None
    try:
        started = _start_runtime(
            selected_binding,
            adapter,
            replace=False,
            state_root=state_root,
            **_context_kwargs(context),
        )
        _verify_started_runtime(
            selected_binding,
            started,
            state_root=state_root,
            **_context_kwargs(context),
        )
        _verify_runtime_input_ready(
            selected_binding,
            started,
            state_root=state_root,
            adapter=adapter,
            **_context_kwargs(context),
        )
    except Exception as exc:
        if started is not None and started.created_scope:
            _stop_created_scope_best_effort(started.scope_unit)
        path = _persona_state(selected_binding.persona, state_root) / "runtime.json"
        if path.read_bytes() != old_raw:
            _atomic_bytes(path, old_raw)
        try:
            _append_controller_evidence(
                selected_binding.persona,
                "runtime_rehydrate_failed",
                {
                    "runtime": _runtime_ref(selected_binding),
                    "runtime_scope_unit": str(runtime_state.get("runtime_scope_unit", "")),
                    "created_scope": str(started.created_scope if started else False).lower(),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                },
                state_root=state_root,
            )
        except Exception:
            pass
        _raise_runtime_viability_block(
            "recorded-runtime-rehydrate-failed",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"rehydrate_error": f"{type(exc).__name__}: {exc}"[:500]},
        )
    boundary = now
    if source_drift:
        # T-5292: the session was just launched through the CURRENT controller
        # source, so record that truthfully rather than the stale recorded hashes.
        source = _controller_source_provenance(
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=boundary,
        )
    else:
        source = RuntimeSourceProvenance(
            revision=str(runtime_state.get("controller_source_revision")),
            source_hashes=dict(_validated_source_hashes_from_state(runtime_state)),
            source_manifest_sha256=str(runtime_state.get("controller_source_manifest_sha256")),
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=boundary,
        )
    _atomic_json(
        _persona_state(selected_binding.persona, state_root) / "runtime.json",
        _runtime_payload(
            selected_binding,
            session_id=None,
            started_at=boundary,
            source_provenance=source,
            provider_completion={"provider_completion_status": "not-applicable"},
        ),
    )
    _append_controller_evidence(
        selected_binding.persona,
        "runtime_rehydrated",
        {
            "runtime": _runtime_ref(selected_binding),
            "source_equivalence_mode": equivalence_mode,
            "receiver_state": str(receiver.get("state", "")),
        },
        state_root=state_root,
    )
    readiness = dict(_runtime_state_readiness_evidence(selected_binding, state_root=state_root))
    readiness.update(
        {
            "source_equivalence_mode": equivalence_mode,
            "source_equivalence_reason": (
                "controller source changed since the dead runtime was recorded; the fresh "
                "session was launched from current source and re-recorded"
                if source_drift
                else "controller source hash map and manifest digest match exactly"
            ),
            "rehydrated": "true",
        }
    )
    attempt = _viability_attempt(
        sequence=1,
        binding=selected_binding,
        status="skipped-recorded-sticky",
        failure_class=None,
        cause="recorded-runtime-rehydrated",
        readiness=readiness,
        teardown={},
        receiver=receiver,
        cooldown={"opened": "false"},
        source_provenance=provenance,
        precheck_source_provenance=precheck,
    )
    receipt = _runtime_viability_receipt(
        persona=selected_binding.persona,
        status="reused-recorded",
        selected=selected_binding,
        manifest_order=manifest_order,
        attempt_limit=attempt_limit,
        attempts=[attempt],
        now=now,
    )
    return RuntimeViabilitySelection(
        selected_binding,
        adapter,
        _handle_from_state(selected_binding, _read_json(_persona_state(selected_binding.persona, state_root) / "runtime.json")),
        receipt,
    )


def _reanchor_held_recorded_selection(
    selected_binding: "PersonaBinding",
    candidates: Sequence["PersonaBinding"],
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
) -> "PersonaBinding":
    """T-5028 (g): the initial-viability record can name a runtime the operator
    has since held (specialist 2026-10-15: claude held, the live pane runs codex).
    Treating it as THE selection blocked every consume fire with
    runtime-state-missing-or-mismatched against the held runtime. While a pane is
    live and positively runs a non-held candidate (argv match), re-anchor onto
    that candidate; the reuse/mismatch checks that follow then judge it. Otherwise
    (no live pane, the pane runs the held runtime or cannot be read, or every
    candidate is held) the record is returned unchanged, so the T-5021 dead-pane
    path and live held-pane reuse are untouched."""
    holds = load_holds(selected_binding.persona, state_root=state_root)
    if _runtime_ref(selected_binding) not in holds:
        return selected_binding
    unheld = [item for item in candidates if _runtime_ref(item) not in holds]
    if not unheld:
        return selected_binding
    live, _evidence = _pane_live_or_unknown(selected_binding, context=context)
    if not live:
        return selected_binding
    anchor = next((item for item in unheld if _pane_runs_binding(item, context=context)), None)
    if anchor is None:
        # The pane does not positively run a non-held runtime (it may run the held
        # one: reusing a live held pane is unchanged, T-5021). Never a guess.
        return selected_binding
    try:
        _append_held_evidence_once(
            selected_binding.persona,
            "recorded_selection_held_reanchored",
            {"runtime": _runtime_ref(selected_binding), "reanchored_to": _runtime_ref(anchor)},
            state_root=state_root,
        )
    except Exception:  # noqa: BLE001 -- the re-anchor must not depend on the log
        pass
    return anchor


def _append_live_source_drift_once(
    binding: "PersonaBinding",
    *,
    recorded_manifest: str,
    recorded_revision: str,
    current_manifest: str,
    current_revision: str,
    state_root: Path | None,
    key_set_delta: Mapping[str, Any] | None = None,
) -> None:
    """T-5311: one ``runtime_live_source_drift`` evidence row per (recorded, current)
    manifest pair, not per consume fire (~700/day/seat). Evidence only: a write
    failure never blocks the wake. T-5315: ``key_set_delta`` (from
    _source_key_set_delta) records a RUNTIME_SOURCE_HASH_PATHS change in the row; the
    manifest pair already differs whenever the key set does, so dedup is unchanged."""
    try:
        path = _persona_state(binding.persona, state_root) / "live-source-drift-evidence.json"
        seen = _read_json(path)
        key = f"{_runtime_ref(binding)}:{recorded_manifest}:{current_manifest}"
        if seen.get(key):
            return
        _append_controller_evidence(
            binding.persona,
            "runtime_live_source_drift",
            {
                "runtime": _runtime_ref(binding),
                "recorded_revision": recorded_revision,
                "recorded_manifest_sha256": recorded_manifest,
                "current_revision": current_revision,
                "current_manifest_sha256": current_manifest,
                **dict(key_set_delta or {}),
            },
            state_root=state_root,
        )
        _atomic_json(path, {key: True})  # only the latest pair matters
    except Exception:  # noqa: BLE001 -- evidence must not block the wake
        pass


def _matching_recorded_runtime_selection(
    candidates: Sequence["PersonaBinding"],
    *,
    adapter_factory: Callable[["PersonaBinding"], "PantheonHarnessAdapter"],
    state_root: Path,
    backend: Any | None,
    context: TmuxRuntimeContext | None,
    now: datetime,
    manifest_order: tuple[str, ...],
    attempt_limit: int,
) -> RuntimeViabilitySelection | None:
    state = initial_viability_state(candidates[0].persona, state_root=state_root)
    if state.get("schema") != "pantheon-runtime-initial-viability/v1" or state.get("status") != "selected":
        runtime_path = _persona_state(candidates[0].persona, state_root) / "runtime.json"
        invocation_dir = _initial_viability_invocation_dir(candidates[0].persona, state_root)
        if (
            state.get("schema") == "pantheon-runtime-initial-viability/v1"
            and state.get("status") in {"exhausted", "selected"}
            and runtime_path.exists()
            and _read_json(runtime_path) == {}
            and invocation_dir.exists()
            and any(invocation_dir.glob("*.json"))
        ):
            raise RuntimeViabilityBlocked(
                "orphan-runtime-recovery-required",
                {
                    "schema": "pantheon-runtime-viability-block/v1",
                    "persona": candidates[0].persona,
                    "reason": "orphan-runtime-recovery-required",
                },
            )
        _block_if_live_target(
            "initial-viability-state-not-selected",
            candidates[0],
            runtime_state=state,
            state_root=state_root,
            context=context,
        )
        return None
    selected = state.get("selected_runtime")
    selected_binding = next((item for item in candidates if _runtime_ref(item) == selected), None)
    if selected_binding is None:
        _block_if_live_target(
            "selected-runtime-not-manifest-authorized",
            candidates[0],
            runtime_state=state,
            state_root=state_root,
            context=context,
            extra={"selected_runtime": str(selected)},
        )
        return None
    anchor_binding = _reanchor_held_recorded_selection(
        selected_binding, candidates, state_root=state_root, context=context
    )
    reanchored = anchor_binding is not selected_binding
    selected_binding = anchor_binding
    runtime_state = _read_json(_persona_state(selected_binding.persona, state_root) / "runtime.json")
    if runtime_state and not _state_matches(selected_binding, runtime_state, **_context_kwargs(context)):
        # T-4469: initial-viability records the runtime proven at bootstrap; a
        # completed operator swap since then legitimately runs another one. Accept
        # the operator's destination when runtime.json records exactly it AND the
        # live pane positively runs it. Otherwise the whole codex dwell blocked here
        # (runtime-state-missing-or-mismatched) and mail wakes never reached the
        # seat (the 2026-09-30 recovery drill). A dead pane never takes this branch, so
        # the consume path can't relaunch the operator's runtime behind a
        # fallback-return that just ended it.
        # T-4872: a completed planned-quota / forced-cap / quota-reset swap leaves the
        # seat on a different runtime the same way, and blocked every mail wake after
        # the 2026-10-10 specialist planned-quota swap. Same receipt checks; consume only
        # ACCEPTS the live runtime (it never pins it: controller_main stays
        # operator-only, so fallback-return keeps owning the return). Operator first.
        for transition_triggers, event in (
            (frozenset({"operator"}), "consume_selected_operator_destination"),
            (_QUOTA_TRANSITION_TRIGGERS, "consume_selected_transition_destination"),
        ):
            transition_binding = _completed_transition_destination(
                selected_binding.persona, candidates, triggers=transition_triggers, state_root=state_root
            )
            if (
                transition_binding is not None
                and _binding_runtime_ref(transition_binding) != _binding_runtime_ref(selected_binding)
                and _state_matches(transition_binding, runtime_state, **_context_kwargs(context))
                and _pane_runs_binding(transition_binding, context=context)
            ):
                evidence = {
                    "initial_viability_selected": _binding_runtime_ref(selected_binding),
                    "destination": _binding_runtime_ref(transition_binding),
                }
                if event == "consume_selected_transition_destination":
                    evidence["trigger"] = transition_state(
                        selected_binding.persona, state_root=state_root
                    ).get("trigger")
                _append_controller_evidence(
                    selected_binding.persona, event, evidence, state_root=state_root
                )
                selected_binding = transition_binding
                break
    if not runtime_state or not _state_matches(selected_binding, runtime_state, **_context_kwargs(context)):
        _block_if_live_target(
            "runtime-state-missing-or-mismatched",
            selected_binding,
            runtime_state=runtime_state or {},
            state_root=state_root,
            context=context,
        )
        return None
    if not pane_exists(selected_binding.controller.tmux, **_context_kwargs(context)):
        if reanchored:
            # T-5028 m1: the re-anchor was justified by a LIVE pane running it; that
            # pane is gone now, so never rehydrate (launch) the anchor past the
            # selection loop's cooldown checks. Fall through to that loop.
            return None
        if _runtime_ref(selected_binding) in load_holds(selected_binding.persona, state_root=state_root):
            # T-5021: rehydrate LAUNCHES the recorded runtime; a held one never is.
            # Fall through to the initial-viability loop, which skips held runtimes.
            _append_held_evidence_once(
                selected_binding.persona,
                "recorded_runtime_rehydrate_skipped_held",
                {"runtime": _runtime_ref(selected_binding)},
                state_root=state_root,
            )
            return None
        if _controller_cooldown_skip(selected_binding, state_root=state_root, now=now) is not None:
            # REBOUND 1 MINOR 1: rehydrate LAUNCHES the recorded runtime; one the
            # controller recorded as runtime_blocked or capped is left to the loop.
            return None
        return _rehydrate_recorded_runtime_selection(
            selected_binding,
            runtime_state,
            adapter_factory=adapter_factory,
            state_root=state_root,
            backend=backend,
            context=context,
            now=now,
            manifest_order=manifest_order,
            attempt_limit=attempt_limit,
        )
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None or not _pid_in_scope(
        server_pid,
        str(runtime_state.get("runtime_scope_unit", "")),
    ):
        _raise_runtime_viability_block(
            "runtime-server-not-in-recorded-scope",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={
                "server_pid": "" if server_pid is None else str(server_pid),
                "recorded_scope": str(runtime_state.get("runtime_scope_unit", "")),
            },
        )
    try:
        receiver = _read_only_receiver_present(selected_binding, backend=backend)
    except Exception as exc:
        _raise_runtime_viability_block(
            "recorded-receiver-unknown",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"receiver_error": f"{type(exc).__name__}: {exc}"[:500]},
        )
    if receiver.get("ok") is not True:
        _raise_runtime_viability_block(
            "recorded-receiver-not-present",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"receiver_state": str(receiver.get("state", ""))},
        )
    try:
        provenance = _runtime_state_source_provenance(
            selected_binding,
            state_root=state_root,
            context=context,
        )
        boundary = _utc_now()
        current_source = _controller_source_provenance(
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=boundary,
        )
        precheck = _provenance_mapping(current_source)
        recorded_hashes = _validated_source_hashes_from_state(runtime_state)
        current_hashes = _validated_current_source_hashes(current_source)
    except ValueError as exc:
        _raise_runtime_viability_block(
            str(exc),
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    except RuntimeError as exc:
        _raise_runtime_viability_block(
            "current-source-provenance-unavailable",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
            extra={"source_error": f"{type(exc).__name__}: {exc}"[:500]},
        )
    drifted = recorded_hashes != current_hashes
    if not drifted:
        recorded_manifest = str(runtime_state.get("controller_source_manifest_sha256", ""))
        if recorded_manifest != current_source.source_manifest_sha256:
            _raise_runtime_viability_block(
                "recorded-source-manifest-mismatch",
                binding=selected_binding,
                runtime_state=runtime_state,
                state_root=state_root,
            )
    if drifted:
        # T-5311: the live session was launched from older controller source. Reuse
        # only submits a prompt into it (no launch, no re-stamp: the session really
        # does run the recorded source, T-5292 P4), and identity was already proven
        # above (state match, pane, scope, receiver). Blocking here stopped the
        # consume wake on every fire after any controller merge. The R31 invariant
        # (never launch over a live target) holds: a reused selection launches nothing.
        equivalence_mode = "live-source-drift"
        _append_live_source_drift_once(
            selected_binding,
            recorded_manifest=str(runtime_state.get("controller_source_manifest_sha256", "")),
            recorded_revision=str(runtime_state.get("controller_source_revision", "")),
            current_manifest=str(precheck.get("source_manifest_sha256", "")),
            current_revision=str(precheck.get("revision", "")),
            state_root=state_root,
            key_set_delta=_source_key_set_delta(recorded_hashes, current_hashes),
        )
    else:
        equivalence_mode = (
            "same-revision"
            if provenance.get("revision") == precheck.get("revision")
            else "ticket-only-revision"
        )
    readiness = dict(_runtime_state_readiness_evidence(selected_binding, state_root=state_root))
    readiness.update(
        {
            "source_equivalence_mode": equivalence_mode,
            "source_equivalence_reason": (
                "recorded source hashes differ from current; live session reused"
                " for delivery only (T-5311)"
                if drifted
                else "controller source hash map and manifest digest match exactly"
            ),
        }
    )
    attempt = _viability_attempt(
        sequence=1,
        binding=selected_binding,
        status="skipped-recorded-sticky",
        failure_class=None,
        cause="recorded-runtime-source-drift-live" if drifted else "recorded-runtime-source-equivalent",
        readiness=readiness,
        teardown={},
        receiver=receiver,
        cooldown={"opened": "false"},
        source_provenance=provenance,
        precheck_source_provenance=precheck,
    )
    receipt = _runtime_viability_receipt(
        persona=selected_binding.persona,
        status="reused-recorded",
        selected=selected_binding,
        manifest_order=manifest_order,
        attempt_limit=attempt_limit,
        attempts=[attempt],
        now=now,
    )
    return RuntimeViabilitySelection(
        selected_binding,
        adapter_factory(selected_binding),
        _handle_from_state(selected_binding, runtime_state),
        receipt,
    )


def _controller_cooldown_skip(
    binding: "PersonaBinding", *, state_root: Path, now: datetime
) -> tuple[str, str | None] | None:
    """T-5028 (c): the consume plane's one-way read of the CONTROLLER's
    ``runtimes/<slug>/last-attempt.json``. Two cooldown sources exist by design:

    * the controller records ``last-attempt.json`` (record_last_attempt) and its
      selection reads only that;
    * the consume plane records its own attempts in initial-viability.json
      (``_candidate_cooldown_open``) and reads only that.

    Consume additionally honours the controller's record for the two classes that
    describe the RUNTIME rather than one plane's launch: ``runtime_blocked``
    (until an operator clears it; also reviewer m2) and an unexpired
    ``subscription_cap`` (the subscription itself is capped, whichever plane
    launches). Transient backoffs (process_death, readiness_timeout,
    operational_failure) stay local to the plane that saw them, and the
    controller does not read consume's record. Returns (cause, until) or None."""
    attempt = load_last_attempt(binding.persona, _binding_runtime_slug(binding), state_root=state_root)
    if _runtime_blocked_record(attempt):
        return "controller-runtime-blocked", None
    if (attempt or {}).get("failure_class") != "subscription_cap":
        return None
    until = _parse_iso_or_none((attempt or {}).get("cooldown_until"))
    if until is None or until <= now:
        return None
    return "controller-subscription-cap", _iso(until)


def ensure_viable_runtime(
    candidates: Sequence["PersonaBinding"],
    *,
    adapter_factory: Callable[["PersonaBinding"], "PantheonHarnessAdapter"],
    state_root: Path | None = None,
    backend: Any | None = None,
    context: TmuxRuntimeContext | None = None,
    now: datetime | None = None,
    cooldown_seconds: int = 900,
    attempt_limit: int | None = None,
) -> RuntimeViabilitySelection:
    invocation_id = _systemd_invocation_id_from_env()
    ordered = _ordered_initial_candidates(candidates, state_root=state_root, context=context)
    if not ordered:
        raise RuntimeViabilityExhausted("no runtime candidates configured")
    state_root_path = _state_root(state_root)
    observed_now = now or _utc_now()
    manifest_order = tuple(_runtime_ref(candidate) for candidate in _unique_candidates(candidates))
    limit = attempt_limit if attempt_limit is not None else len(ordered)
    if limit < 1:
        raise RuntimeViabilityExhausted("runtime viability attempt limit is empty")
    reused = _matching_recorded_runtime_selection(
        ordered,
        adapter_factory=adapter_factory,
        state_root=state_root_path,
        backend=backend,
        context=context,
        now=observed_now,
        manifest_order=manifest_order,
        attempt_limit=limit,
    )
    if reused is not None:
        return reused
    sleep_block = _seat_launch_block(ordered[0].persona, state_root=state_root_path, now=observed_now)
    if sleep_block.sleeping:
        # ORG-106 L3a: every candidate is skipped-held with cause seat-sleep; nothing is
        # attempted. Raised as a policy skip (seat_sleep), not exhaustion.
        sleep_attempts = [
            _viability_attempt(
                sequence=i + 1, binding=candidate, status="skipped-held", failure_class=None,
                cause=SEAT_SLEEP_CAUSE, readiness={}, teardown={}, receiver={},
                cooldown={"opened": "false"}, source_provenance={},
            )
            for i, candidate in enumerate(ordered)
        ]
        err = _seat_sleep_block_error(ordered[0].persona, _runtime_ref(ordered[0]), sleep_block)
        err.evidence["attempts"] = [
            {"runtime": a.runtime, "status": a.status, "cause": a.cause} for a in sleep_attempts
        ]
        raise err
    previous_state = initial_viability_state(ordered[0].persona, state_root=state_root_path)
    # T-5021: the consume plane honours operator holds exactly as the
    # controller's _select_viable_runtime does: a held runtime is skipped.
    holds = load_holds(ordered[0].persona, state_root=state_root_path)
    attempts: list[RuntimeViabilityAttempt] = []
    previous_failed: Any | None = None
    attempted = 0
    for candidate in ordered:
        if attempted >= limit:
            break
        runtime = _runtime_ref(candidate)
        if runtime in holds:
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="skipped-held",
                    failure_class=None,
                    cause="operator-hold",
                    readiness={},
                    teardown={},
                    receiver={},
                    cooldown={"opened": "false"},
                    source_provenance={},
                )
            )
            continue
        controller_skip = _controller_cooldown_skip(candidate, state_root=state_root_path, now=observed_now)
        if controller_skip is not None:
            cause, until = controller_skip
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="skipped-cooldown",
                    failure_class=None,
                    cause=cause,
                    readiness={},
                    teardown={},
                    receiver={},
                    # REBOUND 1 MAJOR: never opened=true here. _candidate_cooldown_open
                    # would read that back as a LOCAL cooldown and outlive an operator's
                    # clear-cooldown; each fire re-reads the controller's record instead.
                    cooldown={"opened": "false", "controller_cause": cause, "controller_until": until or ""},
                    source_provenance={},
                )
            )
            continue
        cooldown_until = _candidate_cooldown_open(previous_state, runtime, now=observed_now)
        if cooldown_until:
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="skipped-cooldown",
                    failure_class=None,
                    cause="cooldown-open",
                    readiness={},
                    teardown={},
                    receiver={},
                    cooldown={"opened": "true", "until": cooldown_until},
                    source_provenance={},
                )
            )
            continue
        attempted += 1
        boundary = _utc_now()
        provenance = _controller_source_provenance(
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=boundary,
        )
        provenance_payload = _provenance_mapping(provenance)
        adapter = adapter_factory(candidate)
        cap = candidate_authoritative_cap(candidate, adapter, state_root=state_root_path)
        if cap is not None:
            until = observed_now + timedelta(seconds=cooldown_seconds)
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="failed",
                    failure_class="subscription-cap",
                    cause="authoritative-cap",
                    readiness={str(key): str(value) for key, value in cap.items()},
                    teardown={"runtime_state_cleared": "false", "scope_stopped": "false", "receiver_absent_proven": "false"},
                    receiver={},
                    cooldown={"opened": "true", "until": _iso(until)},
                    source_provenance=provenance_payload,
                )
            )
            previous_failed = candidate
            continue
        try:
            run_initial_bootstrap_receiver_boundary(
                previous_failed,
                candidate,
                phase="pre-candidate",
                backend=backend,
            )
            # T-4179: thread the receiver seam only when set (call-shape kept
            # stable for the many ensure_runtime test doubles, as with `context`).
            ensure_kwargs: dict[str, Any] = {"state_root": state_root_path}
            if backend is not None:
                ensure_kwargs["receiver_backend"] = backend
            # T-4338: taken before ensure_runtime, which may attach to it (T-4101).
            existed_before = _runtime_pane_existed(candidate)
            handle = ensure_runtime(candidate, adapter, **ensure_kwargs)
        except ProviderQuotaExhausted as exc:
            until = observed_now + timedelta(seconds=cooldown_seconds)
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="failed",
                    failure_class="subscription-cap",
                    cause="provider-quota-exhausted",
                    readiness={"provider_error": exc.provider_error, "profile": exc.profile or candidate.launch.profile},
                    teardown={"runtime_state_cleared": "false", "scope_stopped": "false", "receiver_absent_proven": "false"},
                    receiver={},
                    cooldown={"opened": "true", "until": _iso(until)},
                    source_provenance=provenance_payload,
                )
            )
            previous_failed = candidate
            continue
        except RuntimeOperationalFailure as exc:
            until = observed_now + timedelta(seconds=cooldown_seconds)
            teardown = compensate_failed_initial_candidate(
                candidate,
                failure=exc,
                state_root=state_root_path,
                backend=backend,
                context=context,
                reason=exc.cause,
                launched_at=boundary,
            )
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="failed",
                    failure_class=exc.failure_class,
                    cause=exc.cause,
                    readiness=exc.readiness_evidence,
                    teardown=teardown,
                    receiver={},
                    cooldown={"opened": "true", "until": _iso(until)},
                    source_provenance=provenance_payload,
                )
            )
            previous_failed = candidate
            continue
        try:
            receiver_receipt = run_initial_bootstrap_receiver_boundary(
                previous_failed,
                candidate,
                phase="post-selected",
                backend=backend,
            )
        except Exception as exc:
            if candidate.launch.harness != "codex":
                raise
            receiver_teardown: dict[str, str] = {"runtime_state_cleared": "false", "scope_stopped": "false"}
            try:
                receiver = run_initial_bootstrap_receiver_boundary(
                    previous_failed,
                    candidate,
                    phase="compensate-selected",
                    backend=backend,
                )
                try:
                    receiver_teardown.update(
                        _stop_scope_this_attempt_created(
                            candidate, handle, launched_at=boundary, existed_before=existed_before,
                            state_root=state_root_path,
                        )
                    )
                except ScopeStopRefused as stop_exc:
                    _record_refused_scope_stop(
                        candidate, receiver_teardown, stop_exc,
                        reason="receiver_handoff_failed", state_root=state_root_path,
                    )
                    raise
                _clear_stale_runtime_state(candidate.persona, state_root=state_root_path)
                receiver_teardown["runtime_state_cleared"] = "true"
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"initial receiver compensation failed after {type(exc).__name__}: {exc}; "
                    f"compensation: {type(rollback_exc).__name__}: {rollback_exc}"
                ) from exc
            _append_controller_evidence(
                candidate.persona,
                "runtime_viability_candidate_teardown",
                {"runtime": _runtime_ref(candidate), "reason": "receiver_handoff_failed", **receiver_teardown},
                state_root=state_root_path,
            )
            until = observed_now + timedelta(seconds=cooldown_seconds)
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="failed",
                    failure_class="operational-readiness",
                    cause="receiver_handoff_failed",
                    readiness={"event": "receiver_handoff_failed", "detail": str(exc)[:1000]},
                    teardown=receiver_teardown,
                    receiver=dataclasses.asdict(receiver),
                    cooldown={"opened": "true", "until": _iso(until)},
                    source_provenance=provenance_payload,
                )
            )
            previous_failed = candidate
            continue
        runtime_source_provenance = _runtime_state_source_provenance(
            candidate,
            state_root=state_root_path,
            context=context,
        )
        attempts.append(
            _viability_attempt(
                sequence=len(attempts) + 1,
                binding=candidate,
                status="selected",
                failure_class=None,
                cause="runtime-ready",
                readiness=_runtime_state_readiness_evidence(candidate, state_root=state_root_path),
                teardown={},
                receiver=dataclasses.asdict(receiver_receipt),
                cooldown={"opened": "false"},
                source_provenance=runtime_source_provenance,
                precheck_source_provenance=provenance_payload,
            )
        )
        receipt = _runtime_viability_receipt(
            persona=candidate.persona,
            status="selected",
            selected=candidate,
            manifest_order=manifest_order,
            attempt_limit=limit,
            attempts=attempts,
            now=observed_now,
        )
        if invocation_id is not None:
            _publish_initial_viability_invocation_receipt(
                receipt,
                invocation_id=invocation_id,
                state_root=state_root_path,
            )
        _record_initial_viability(receipt, state_root=state_root_path)
        return RuntimeViabilitySelection(candidate, adapter, handle, receipt)
    receipt = _runtime_viability_receipt(
        persona=ordered[0].persona,
        status="exhausted",
        selected=None,
        manifest_order=manifest_order,
        attempt_limit=limit,
        attempts=attempts,
        now=observed_now,
    )
    _record_initial_viability(receipt, state_root=state_root_path)
    raise RuntimeViabilityExhausted(f"no viable runtime candidates for {ordered[0].persona}")


def recorded_runtime_handle(
    binding: "PersonaBinding",
    *,
    state_root: Path | None = None,
) -> RuntimeHandle:
    """Describe the selected runtime without starting or submitting to it.

    Forced-cap handling needs the last structural session identifier but must
    not invoke a provider that has already returned an authoritative limit.
    Missing state therefore produces a conservative synthetic handle; present
    state must match the manifest-selected runtime exactly.
    """

    state = _read_json(_persona_state(binding.persona, state_root) / "runtime.json")
    if state:
        if not _state_matches(binding, state):
            raise RuntimeError(
                "recorded runtime does not match the selected persona runtime profile"
            )
        return _handle_from_state(binding, state)
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=_utc_now(),
    )


#: Launch harness -> submit_verify profile (send_keys_submit --harness).
#: T-5011 PR-B: explicit; an unmapped harness raises instead of silently
#: getting the codex profile.
_SUBMIT_VERIFY_PROFILE_BY_HARNESS: Mapping[str, str] = {
    "claude": "claude-code",
    "codex": "codex",
    "hermes": "hermes",
}


def _submit_verify_harness(harness: str) -> str:
    """submit_verify's profile name for a launch harness (send_keys_submit --harness)."""
    try:
        return _SUBMIT_VERIFY_PROFILE_BY_HARNESS[harness]
    except KeyError:
        raise ValueError(f"no submit_verify profile for harness {harness!r}") from None


def _plain_decimal_seconds(seconds: float) -> str:
    """``seconds`` as the plain decimal send_keys_submit --render-wait accepts.

    T-4390 (rev-t4371 r2 NIT-7): f"{x:g}" gives "1e+06" for large values, which the
    CLI refuses. Fixed-point never uses an exponent; trailing zeros are dropped.
    """
    text = f"{seconds:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _is_signalable_pid(pid: object) -> bool:
    """T-5031: True only for a real child pid we may signal (a plain int > 1).

    os.killpg(1, sig) is kill(-1, sig), which hits EVERY process the account owns,
    its systemd --user manager included. A mocked Popen's MagicMock pid converts
    to 1 (``__index__``), so a test that patched Popen once SIGKILLed the whole
    seat it ran as. A real Popen or tmux pane pid is always an int > 1."""
    return type(pid) is int and pid > 1


def _run_submit_helper(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run send_keys_submit.sh bounded by ``timeout``; on timeout kill its process group.

    T-4390: subprocess.run(timeout=) kills only the bash child. A hung tmux client
    (paste-buffer, send-keys, capture-pane) is a GRANDCHILD: it would outlive the
    kill, and if its server came back it could still paste or press Enter in a pane
    the controller had moved on from. The helper runs in its own session (tmux needs
    no terminal, and the controller has none under systemd), so one killpg takes it
    and every tmux client it started. close_fds (the default) keeps the controller's
    lease and lock fds out of that tree. Re-raises subprocess.TimeoutExpired (or an
    interrupt) after the kill.
    """
    with subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            if _is_signalable_pid(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def submit_to_runtime(
    handle: RuntimeHandle,
    request: ExecutionRequest,
    *,
    persona: str | None = None,
    state_root: Path | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    context: TmuxRuntimeContext | None = None,
    render_wait_seconds: float | None = None,
) -> DeliveryReceipt:
    """Transport a prompt into the pane without claiming turn completion/ack.

    ``render_wait_seconds`` (T-4362) is for a runtime that has only just booted:
    the helper holds its first Enter until the pasted text shows in the composer.
    The helper is bounded at that wait plus SUBMIT_HELPER_TIMEOUT_MARGIN_SECONDS
    (T-4390); a timeout raises RuntimeError like any other unverified submit.
    """

    harness = _submit_verify_harness(handle.profile.harness)
    evidence_context = {
        "harness": handle.profile.harness,
        "provider": handle.profile.harness,
        "profile": handle.profile.profile,
        "tmux_target": _target_text(handle.tmux),
        "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        "message_id": message_id,
        "correlation_id": correlation_id,
    }
    if persona is not None:
        transition = transition_state(persona, state_root=state_root)
        if (
            transition.get("breaker_open") is True
            and transition.get("capped_profile") == handle.profile.profile
        ):
            _append_controller_evidence(
                persona,
                "runtime_submission_blocked",
                {
                    **evidence_context,
                    "capped_profile": transition.get("capped_profile"),
                },
                state_root=state_root,
            )
            raise ProviderQuotaExhausted(
                f"runtime profile {handle.profile.profile} has an open quota breaker",
                profile=handle.profile.profile,
            )
        _append_controller_evidence(
            persona,
            "runtime_submission_attempt",
            evidence_context,
            state_root=state_root,
        )
    helper_argv = [str(SEND_KEYS_HELPER), "--harness", harness]
    if context is not None and context.socket:
        helper_argv += ["--socket", context.socket]
    if render_wait_seconds:
        helper_argv += ["--render-wait", _plain_decimal_seconds(render_wait_seconds)]
    helper_argv += [_target_text(handle.tmux), request.prompt]
    # T-4390: a hung tmux call inside the helper must not hold the launch until
    # TimeoutStartSec. A timeout is an unverified submit, like a non-zero exit.
    timeout = SUBMIT_HELPER_TIMEOUT_MARGIN_SECONDS
    if render_wait_seconds and math.isfinite(render_wait_seconds) and render_wait_seconds > 0:
        timeout += render_wait_seconds
    failure: dict[str, Any] | None = None
    try:
        result = _run_submit_helper(helper_argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stderr or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        detail = (
            f"send_keys_submit timed out after {timeout:g}s; prompt submission was not "
            "verified (helper and its tmux clients killed)"
        )
        if partial.strip():
            detail += f"; helper stderr so far: {partial.strip()[-500:]}"
        failure = {"returncode": None, "timeout_seconds": timeout, "detail": detail}
    else:
        if result.returncode != 0:
            detail = result.stderr.strip() or "prompt submission was not verified"
            failure = {"returncode": result.returncode, "detail": detail}
    if failure is not None:
        if persona is not None:
            _append_controller_evidence(
                persona,
                "runtime_submission_failed",
                {
                    **evidence_context,
                    **failure,
                    "detail": failure["detail"][:1000],
                },
                state_root=state_root,
            )
        raise RuntimeError(failure["detail"])
    if persona is not None:
        _append_controller_evidence(
            persona,
            "runtime_submission_accepted",
            {
                **evidence_context,
                "returncode": result.returncode,
            },
            state_root=state_root,
        )
    return DeliveryReceipt(
        harness=handle.profile.harness,
        correlation_id="",
        state="accepted",
        mechanism="tmux-submit-verify",
        retryable=False,
        evidence={
            "tmux_target": _target_text(handle.tmux),
            "transport_verified": "true",
            "turn_started": "unverified",
            "acked": "false",
        },
    )


def new_drain_id() -> str:
    return uuid.uuid4().hex


def _transition_path(persona: str, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "transition.json"


def transition_state(persona: str, *, state_root: Path | None = None) -> dict[str, Any]:
    return _read_json(_transition_path(persona, state_root))


_RECOVERY_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _validate_recovery_id(recovery_id: str) -> str:
    if (
        not recovery_id
        or len(recovery_id) > 128
        or any(char not in _RECOVERY_ID_CHARS for char in recovery_id)
    ):
        raise ValueError("recovery_id must be 1-128 chars of [A-Za-z0-9._-]")
    return recovery_id


def _transition_bytes_and_state(path: Path) -> tuple[bytes, dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("runtime transition state is unavailable") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runtime transition state is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("runtime transition state must be a JSON object")
    return raw, parsed, hashlib.sha256(raw).hexdigest()


def _runtime_ref(binding: Any) -> str:
    return _runtime_reference_from_launch(binding.launch)


def _same_tmux_target(left: Any, right: Any) -> bool:
    return _target_text(left.controller.tmux) == _target_text(right.controller.tmux)


def _write_recovery_archive(
    persona: str,
    *,
    state_root: Path | None,
    recovery_id: str,
    predecessor_sha256: str,
    predecessor_state: Mapping[str, Any],
    proofs: Mapping[str, Any],
) -> tuple[Path, str]:
    archive = _persona_state(persona, state_root) / f"operator-recovery-{recovery_id}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "pantheon-runtime-operator-compensation-recovery-archive/v1",
        "persona": persona,
        "recovery_id": recovery_id,
        "predecessor_sha256": predecessor_sha256,
        "predecessor_transition": dict(predecessor_state),
        "proofs": dict(proofs),
        "archived_at": _iso(_utc_now()),
    }
    raw = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(archive, flags, 0o600)
    except FileExistsError:
        existing_raw = archive.read_bytes()
        try:
            existing = json.loads(existing_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing recovery archive is unreadable") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("schema")
            != "pantheon-runtime-operator-compensation-recovery-archive/v1"
            or existing.get("persona") != persona
            or existing.get("recovery_id") != recovery_id
            or existing.get("predecessor_sha256") != predecessor_sha256
            or existing.get("predecessor_transition") != dict(predecessor_state)
            or existing.get("proofs") != dict(proofs)
        ):
            raise RuntimeError("existing recovery archive does not match this recovery")
        return archive, hashlib.sha256(existing_raw).hexdigest()
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(archive.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return archive, hashlib.sha256(raw).hexdigest()


def _operator_recovery_receipt(
    state: Mapping[str, Any],
    *,
    status: Literal["recovered", "already-recovered"],
) -> OperatorCompensationRecoveryReceipt:
    return OperatorCompensationRecoveryReceipt(
        schema="pantheon-runtime-operator-compensation-recovery/v1",
        persona=str(state.get("persona", "")),
        status=status,
        recovery_id=str(state.get("recovery_id", "")),
        recovered_from_transition_id=str(state.get("recovered_from_transition_id", "")),
        recovered_from_sha256=str(state.get("recovered_from_sha256", "")),
        predecessor_archive=str(state.get("predecessor_archive", "")),
        predecessor_archive_sha256=str(state.get("predecessor_archive_sha256", "")),
        source_runtime=f"{state.get('outgoing_harness', '')}/{state.get('outgoing_profile', '')}",
        target_runtime=f"{state.get('incoming_harness', '')}/{state.get('incoming_profile', '')}",
        source_runtime_proven=bool(state.get("source_runtime_proven", False)),
        source_readiness_proven=bool(state.get("source_readiness_proven", False)),
        target_absent_proven=bool(state.get("target_absent_proven", False)),
        target_metadata_mismatch_proven=bool(state.get("target_metadata_mismatch_proven", False)),
        receiver_source_present_proven=bool(state.get("receiver_source_present_proven", False)),
        receiver_target_absent_proven=bool(state.get("receiver_target_absent_proven", False)),
        phase=str(state.get("phase", "")),
        recovered_at=str(state.get("recovered_at", "")),
    )


def _prove_operator_recovery_runtime(
    source_binding: Any,
    target_binding: Any,
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
    backend: Any | None,
) -> dict[str, Any]:
    runtime_state = _read_json(_persona_state(source_binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(source_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded source runtime does not match compensation recovery source")
    if _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded runtime still matches compensation recovery target")
    if not pane_exists(source_binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("source runtime pane is not live")
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None or not _pid_in_scope(server_pid, str(runtime_state["runtime_scope_unit"])):
        raise RuntimeError("live tmux server is not bound to the recorded source scope")
    source_handle = _handle_from_state(source_binding, runtime_state)
    readiness = harness_integration.integration_for(source_binding.launch.harness).prove_runtime_readiness(
        source_binding,
        source_handle,
        state_root=state_root,
        context=context,
    )
    if not readiness.ready:
        raise RuntimeError(f"source runtime readiness was not proven: {dict(readiness.evidence)}")
    same_tmux = _same_tmux_target(source_binding, target_binding)
    target_absent = False
    if not same_tmux:
        target_absent = not pane_exists(target_binding.controller.tmux, **_context_kwargs(context))
        if not target_absent:
            raise RuntimeError("target runtime pane is live")
    direction = _receiver_direction_for_bindings(source_binding, target_binding)
    receiver_source_present = False
    receiver_target_absent = False
    if direction != "none":
        _prove_receiver(source_binding, "present", backend=backend)
        _prove_receiver(target_binding, "absent", backend=backend)
        receiver_source_present = True
        receiver_target_absent = True
    return {
        "source_runtime_proven": True,
        "source_readiness_proven": True,
        "source_readiness_evidence": dict(readiness.evidence),
        "target_absent_proven": target_absent,
        "target_metadata_mismatch_proven": True,
        "target_tmux_shared_with_source": same_tmux,
        "receiver_source_present_proven": receiver_source_present,
        "receiver_target_absent_proven": receiver_target_absent,
    }


def recover_operator_compensation_failed(
    source_binding: Any,
    target_binding: Any,
    *,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    state_root: Path,
    reason: str,
    context: TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> OperatorCompensationRecoveryReceipt:
    recovery_id = _validate_recovery_id(recovery_id)
    if source_binding.persona != target_binding.persona:
        raise ValueError("source and target personas must match")
    if not reason:
        raise ValueError("recovery reason is required")
    persona = source_binding.persona
    path = _transition_path(persona, state_root)
    with persona_operation_lock(persona, state_root=state_root):
        _, state, observed_sha256 = _transition_bytes_and_state(path)
        if state.get("phase") == "recovered":
            if (
                state.get("recovery_id") == recovery_id
                and state.get("recovered_from_transition_id") == expected_transition_id
                and state.get("recovered_from_sha256") == expected_transition_sha256
                and state.get("outgoing_harness") == source_binding.launch.harness
                and state.get("outgoing_profile") == source_binding.launch.profile
                and state.get("incoming_harness") == target_binding.launch.harness
                and state.get("incoming_profile") == target_binding.launch.profile
            ):
                _prove_operator_recovery_runtime(
                    source_binding,
                    target_binding,
                    state_root=state_root,
                    context=context,
                    backend=backend,
                )
                return _operator_recovery_receipt(state, status="already-recovered")
            raise RuntimeTransitionPending("runtime transition recovery identity mismatch")
        expected = {
            "schema": "pantheon-runtime-transition/v1",
            "phase": "compensation-failed",
            "trigger": "operator",
            "transition_id": expected_transition_id,
            "outgoing_harness": source_binding.launch.harness,
            "outgoing_profile": source_binding.launch.profile,
            "incoming_harness": target_binding.launch.harness,
            "incoming_profile": target_binding.launch.profile,
        }
        mismatches = {
            key: (state.get(key), value)
            for key, value in expected.items()
            if state.get(key) != value
        }
        if mismatches:
            raise RuntimeTransitionPending("runtime transition is not the expected failed operator transition")
        if observed_sha256 != expected_transition_sha256:
            raise RuntimeTransitionPending("runtime transition digest mismatch")
        proofs = _prove_operator_recovery_runtime(
            source_binding,
            target_binding,
            state_root=state_root,
            context=context,
            backend=backend,
        )
        archive, archive_sha256 = _write_recovery_archive(
            persona,
            state_root=state_root,
            recovery_id=recovery_id,
            predecessor_sha256=observed_sha256,
            predecessor_state=state,
            proofs=proofs,
        )
        _append_controller_evidence(
            persona,
            "operator_compensation_recovery_authorized",
            {
                "recovery_id": recovery_id,
                "reason": reason,
                "recovered_from_transition_id": expected_transition_id,
                "recovered_from_sha256": observed_sha256,
                "predecessor_archive": str(archive),
                "predecessor_archive_sha256": archive_sha256,
                "source_runtime": _runtime_ref(source_binding),
                "target_runtime": _runtime_ref(target_binding),
                **proofs,
            },
            state_root=state_root,
        )
        _, reread_state, reread_sha256 = _transition_bytes_and_state(path)
        if (
            reread_state.get("phase") != "compensation-failed"
            or reread_state.get("transition_id") != expected_transition_id
            or reread_sha256 != observed_sha256
        ):
            raise RuntimeTransitionPending("runtime transition changed during compensation recovery")
        recovered_at = _iso(_utc_now())
        recovered = {
            "schema": "pantheon-runtime-transition/v1",
            "phase": "recovered",
            "trigger": "operator-recovery",
            "persona": persona,
            "recovery_id": recovery_id,
            "recovered_at": recovered_at,
            "fresh_operator_initiation_allowed": True,
            "recovered_from_transition_id": expected_transition_id,
            "recovered_from_sha256": observed_sha256,
            "predecessor_archive": str(archive),
            "predecessor_archive_sha256": archive_sha256,
            "outgoing_harness": source_binding.launch.harness,
            "outgoing_profile": source_binding.launch.profile,
            "incoming_harness": target_binding.launch.harness,
            "incoming_profile": target_binding.launch.profile,
            **proofs,
            **_preserved_policy_state(state),
        }
        _atomic_json(path, recovered)
        return _operator_recovery_receipt(recovered, status="recovered")


def _compensation_target_live_recovery_marker(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    marker = state.get("compensation_failed_target_live_recovery")
    return marker if isinstance(marker, Mapping) else None


def _format_compensation_target_live_recovery_error(exc: BaseException) -> str:
    return chronicle.scrub_credentials(f"{type(exc).__name__}: {exc}")[:1000]


def _write_compensation_target_live_recovery_archive(
    persona: str,
    *,
    state_root: Path | None,
    recovery_id: str,
    predecessor_sha256: str,
    predecessor_state: Mapping[str, Any],
    reason: str,
) -> tuple[Path, str]:
    archive = _persona_state(persona, state_root) / f"operator-compensation-target-live-recovery-{recovery_id}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "pantheon-runtime-operator-compensation-target-live-recovery-archive/v1",
        "persona": persona,
        "recovery_id": recovery_id,
        "predecessor_sha256": predecessor_sha256,
        "predecessor_transition": dict(predecessor_state),
        "reason": reason,
        "archived_at": _iso(_utc_now()),
    }
    raw = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    try:
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing_raw = archive.read_bytes()
        try:
            existing = json.loads(existing_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing target-live recovery archive is unreadable") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("schema") != "pantheon-runtime-operator-compensation-target-live-recovery-archive/v1"
            or existing.get("persona") != persona
            or existing.get("recovery_id") != recovery_id
            or existing.get("predecessor_sha256") != predecessor_sha256
            or existing.get("predecessor_transition") != dict(predecessor_state)
            or existing.get("reason") != reason
        ):
            raise RuntimeError("existing target-live recovery archive does not match this recovery")
        return archive, hashlib.sha256(existing_raw).hexdigest()
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(archive.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return archive, hashlib.sha256(raw).hexdigest()


def _validate_compensation_target_live_archive(marker: Mapping[str, Any]) -> None:
    archive = Path(str(marker.get("predecessor_archive", "")))
    expected_sha = marker.get("predecessor_archive_sha256")
    if not archive.is_file() or not isinstance(expected_sha, str):
        raise RuntimeTransitionPending("target-live recovery archive is unavailable")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RuntimeTransitionPending("target-live recovery archive digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeTransitionPending("target-live recovery archive is unreadable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != "pantheon-runtime-operator-compensation-target-live-recovery-archive/v1"
        or payload.get("recovery_id") != marker.get("recovery_id")
        or payload.get("predecessor_sha256") != marker.get("recovered_from_sha256")
        or payload.get("reason") != marker.get("reason")
        or not isinstance(payload.get("predecessor_transition"), Mapping)
    ):
        raise RuntimeTransitionPending("target-live recovery archive body mismatch")


def _validate_compensation_target_live_identity(
    state: Mapping[str, Any],
    *,
    source_binding: Any,
    target_binding: Any,
    expected_transition_id: str,
    correlation_id: str,
) -> None:
    expected = {
        "schema": "pantheon-runtime-transition/v1",
        "trigger": "operator",
        "transition_id": expected_transition_id,
        "correlation_id": correlation_id,
        "source_runtime": _runtime_ref(source_binding),
        "destination": _runtime_ref(target_binding),
        "outgoing_harness": source_binding.launch.harness,
        "outgoing_profile": source_binding.launch.profile,
        "incoming_harness": target_binding.launch.harness,
        "incoming_profile": target_binding.launch.profile,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise RuntimeTransitionPending("runtime transition is not the expected target-live recovery transition")


def _validate_compensation_target_live_initial_state(
    state: Mapping[str, Any],
    *,
    source_binding: Any,
    target_binding: Any,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    expected_predecessor_sha256: str,
    observed_sha256: str,
    correlation_id: str,
    reason: str,
) -> Mapping[str, Any]:
    del recovery_id, reason
    if observed_sha256 != expected_transition_sha256:
        raise RuntimeTransitionPending("runtime transition digest mismatch")
    if expected_predecessor_sha256 != observed_sha256:
        raise RuntimeTransitionPending("target-live predecessor digest mismatch")
    if state.get("phase") != "compensation-failed":
        raise RuntimeTransitionPending("runtime transition is not compensation-failed")
    _validate_compensation_target_live_identity(
        state,
        source_binding=source_binding,
        target_binding=target_binding,
        expected_transition_id=expected_transition_id,
        correlation_id=correlation_id,
    )
    mutation = state.get("runtime_mutation")
    if (
        not isinstance(mutation, Mapping)
        or mutation.get("status") != "compensation-failed"
        or mutation.get("pane_may_be_mutated") is not True
        or mutation.get("target") != _runtime_ref(target_binding)
    ):
        raise RuntimeTransitionPending("runtime transition has no exact target-live failed mutation")
    return mutation


def _validate_compensation_target_live_marker(
    state: Mapping[str, Any],
    marker: Mapping[str, Any],
    *,
    source_binding: Any,
    target_binding: Any,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    expected_predecessor_sha256: str,
    observed_sha256: str,
    correlation_id: str,
    reason: str,
) -> None:
    if observed_sha256 != expected_transition_sha256:
        raise RuntimeTransitionPending("runtime transition digest mismatch")
    if state.get("phase") not in {"compensation-failed", "preparing", "awaiting-successor-read"}:
        raise RuntimeTransitionPending("target-live recovery phase is not resumable")
    _validate_compensation_target_live_identity(
        state,
        source_binding=source_binding,
        target_binding=target_binding,
        expected_transition_id=expected_transition_id,
        correlation_id=correlation_id,
    )
    expected = {
        "schema": "pantheon-runtime-operator-compensation-target-live-recovery/v1",
        "recovery_id": recovery_id,
        "reason": reason,
        "recovered_from_transition_id": expected_transition_id,
        "correlation_id": correlation_id,
        "source_runtime": _runtime_ref(source_binding),
        "destination": _runtime_ref(target_binding),
    }
    if marker.get("recovered_from_sha256") != expected_predecessor_sha256:
        raise RuntimeTransitionPending("target-live predecessor digest mismatch")
    if any(marker.get(key) != value for key, value in expected.items()):
        raise RuntimeTransitionPending("target-live recovery identity mismatch")
    if not isinstance(marker.get("predecessor_archive"), str) or not isinstance(marker.get("predecessor_archive_sha256"), str):
        raise RuntimeTransitionPending("target-live recovery archive identity missing")
    _validate_compensation_target_live_archive(marker)


def _checkpoint_compensation_target_live_recovery_error(
    state: dict[str, Any],
    *,
    state_root: Path,
    persona: str,
    recovery_id: str,
    exc: BaseException,
) -> None:
    marker = dict(_compensation_target_live_recovery_marker(state) or {})
    marker["status"] = "failed"
    marker["recovery_id"] = recovery_id
    marker["last_error_at"] = _iso(_utc_now())
    marker["last_error"] = _format_compensation_target_live_recovery_error(exc)
    state["compensation_failed_target_live_recovery"] = marker
    state["recovery_error"] = marker["last_error"]
    _atomic_json(_transition_path(persona, state_root), state)


def _prove_compensation_target_live_pre_rearm_scope(
    target_binding: Any,
    mutation: Mapping[str, Any],
    *,
    context: TmuxRuntimeContext | None,
) -> dict[str, Any]:
    if not pane_exists(target_binding.controller.tmux, **_context_kwargs(context)):
        return {
            "target_pre_rearm_live": False,
            "target_pre_rearm_scope_proven": False,
        }
    scope_unit = mutation.get("scope_unit")
    if not isinstance(scope_unit, str) or not scope_unit:
        raise RuntimeError("target-live mutation scope is missing")
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None or not _pid_in_scope(server_pid, scope_unit):
        raise RuntimeError("live target tmux server is not bound to the mutation scope")
    return {
        "target_pre_rearm_live": True,
        "target_pre_rearm_scope_proven": True,
        "target_pre_rearm_scope_unit": scope_unit,
    }


def _prove_compensation_target_live_source_absence_if_distinct(
    source_binding: Any,
    target_binding: Any,
    *,
    context: TmuxRuntimeContext | None,
) -> bool:
    if _same_tmux_target(source_binding, target_binding):
        return False
    if pane_exists(source_binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("source runtime pane is live")
    return True


def _compensation_target_live_replay_requires_rearm(
    state: Mapping[str, Any],
    marker: Mapping[str, Any] | None,
) -> bool:
    return (
        marker is not None
        and state.get("phase") == "preparing"
        and state.get("runtime_swapped") is True
        and marker.get("target_rearmed") is True
        and state.get("receiver_post_done") is not True
    )


def _prove_compensation_target_live_recorded_runtime_before_rearm(
    target_binding: Any,
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
) -> tuple[RuntimeHandle, dict[str, Any]]:
    runtime_state = _read_json(_persona_state(target_binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded target runtime does not match target-live recovery")
    if not pane_exists(target_binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("target runtime pane is not live before replay rearm")
    scope_unit = runtime_state.get("runtime_scope_unit")
    if not isinstance(scope_unit, str) or not scope_unit:
        raise RuntimeError("recorded target runtime scope is missing")
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None or not _pid_in_scope(server_pid, scope_unit):
        raise RuntimeError("live target tmux server is not bound to the recorded target scope")
    return _handle_from_state(target_binding, runtime_state), {
        "target_pre_rearm_live": True,
        "target_pre_rearm_scope_proven": True,
        "target_pre_rearm_scope_unit": scope_unit,
        "target_rearm_replay_refreshed_recorded_runtime": True,
    }


def _prove_compensation_target_live_runtime_after_rearm(
    target_binding: Any,
    handle: RuntimeHandle,
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
) -> dict[str, Any]:
    runtime_state = _read_json(_persona_state(target_binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded target runtime does not match target-live recovery")
    if not pane_exists(target_binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("target runtime pane is not live after rearm")
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None or not _pid_in_scope(server_pid, str(runtime_state["runtime_scope_unit"])):
        raise RuntimeError("live target tmux server is not bound to the recorded target scope")
    readiness = harness_integration.integration_for(target_binding.launch.harness).prove_runtime_readiness(
        target_binding,
        handle,
        state_root=state_root,
        context=context,
    )
    if not readiness.ready:
        raise RuntimeError(f"target runtime readiness was not proven: {dict(readiness.evidence)}")
    return {
        "target_runtime_proven": True,
        "target_readiness_proven": True,
        "target_readiness_evidence": dict(readiness.evidence),
        "target_post_rearm_scope_proven": True,
    }


def _compensation_target_live_recovery_receipt(
    state: Mapping[str, Any],
    *,
    status: Literal["pending", "already-pending"],
    current_transition_sha256: str,
) -> OperatorCompensationTargetLiveRecoveryReceipt:
    marker = _compensation_target_live_recovery_marker(state) or {}
    return OperatorCompensationTargetLiveRecoveryReceipt(
        schema="pantheon-runtime-operator-compensation-target-live-recovery/v1",
        persona=str(state.get("persona", "")),
        status=status,
        recovery_id=str(marker.get("recovery_id", "")),
        recovered_from_transition_id=str(marker.get("recovered_from_transition_id", "")),
        recovered_from_sha256=str(marker.get("recovered_from_sha256", "")),
        current_transition_sha256=current_transition_sha256,
        correlation_id=str(state.get("correlation_id", "")),
        source_runtime=str(state.get("source_runtime", "")),
        destination=str(state.get("destination", "")),
        predecessor_archive=str(marker.get("predecessor_archive", "")),
        predecessor_archive_sha256=str(marker.get("predecessor_archive_sha256", "")),
        phase=str(state.get("phase", "")),
        runtime_swapped=bool(state.get("runtime_swapped", False)),
        receiver_post_done=bool(state.get("receiver_post_done", False)),
        receiver_transferred=bool(state.get("receiver_transferred", False)),
        target_rearmed=bool(marker.get("target_rearmed", False)),
        target_readiness_proven=bool(state.get("target_readiness_proven", False)),
        source_absent_proven=bool(state.get("source_absent_proven", False)),
        receiver_source_absent_proven=bool(state.get("receiver_source_absent_proven", False)),
        receiver_target_present_proven=bool(state.get("receiver_target_present_proven", False)),
        chronicle_evidence=str(state.get("chronicle_evidence", "")),
        expected_successor_read_evidence=str(state.get("expected_successor_read_evidence", "")),
        recovered_at=str(marker.get("recovered_at", "")),
    )


def recover_operator_compensation_failed_target_live(
    source_binding: Any,
    target_binding: Any,
    *,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    expected_predecessor_sha256: str,
    correlation_id: str,
    state_root: Path,
    reason: str,
    context: TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> OperatorCompensationTargetLiveRecoveryReceipt:
    recovery_id = _validate_recovery_id(recovery_id)
    if source_binding.persona != target_binding.persona:
        raise ValueError("source and target personas must match")
    if not reason:
        raise ValueError("recovery reason is required")
    if not correlation_id:
        raise ValueError("correlation_id is required")
    persona = source_binding.persona
    path = _transition_path(persona, state_root)
    with persona_operation_lock(persona, state_root=state_root):
        _, state, observed_sha256 = _transition_bytes_and_state(path)
        marker = _compensation_target_live_recovery_marker(state)
        preexisting_marker = marker
        if marker is None:
            mutation = _validate_compensation_target_live_initial_state(
                state,
                source_binding=source_binding,
                target_binding=target_binding,
                recovery_id=recovery_id,
                expected_transition_id=expected_transition_id,
                expected_transition_sha256=expected_transition_sha256,
                expected_predecessor_sha256=expected_predecessor_sha256,
                observed_sha256=observed_sha256,
                correlation_id=correlation_id,
                reason=reason,
            )
            archive, archive_sha256 = _write_compensation_target_live_recovery_archive(
                persona,
                state_root=state_root,
                recovery_id=recovery_id,
                predecessor_sha256=observed_sha256,
                predecessor_state=state,
                reason=reason,
            )
            _, reread_state, reread_sha256 = _transition_bytes_and_state(path)
            if reread_sha256 != observed_sha256 or reread_state != state:
                raise RuntimeTransitionPending("runtime transition changed during target-live recovery")
            marker = {
                "schema": "pantheon-runtime-operator-compensation-target-live-recovery/v1",
                "status": "in-progress",
                "recovery_id": recovery_id,
                "reason": reason,
                "correlation_id": correlation_id,
                "source_runtime": _runtime_ref(source_binding),
                "destination": _runtime_ref(target_binding),
                "recovered_from_transition_id": expected_transition_id,
                "recovered_from_sha256": observed_sha256,
                "predecessor_archive": str(archive),
                "predecessor_archive_sha256": archive_sha256,
                "started_at": _iso(_utc_now()),
            }
            state = dict(state)
            state["compensation_failed_target_live_recovery"] = marker
            state["receiver_post_done"] = bool(state.get("receiver_post_done", False))
            state["receiver_transferred"] = bool(state.get("receiver_transferred", False))
            _atomic_json(path, state)
            observed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            _validate_compensation_target_live_marker(
                state,
                marker,
                source_binding=source_binding,
                target_binding=target_binding,
                recovery_id=recovery_id,
                expected_transition_id=expected_transition_id,
                expected_transition_sha256=expected_transition_sha256,
                expected_predecessor_sha256=expected_predecessor_sha256,
                observed_sha256=observed_sha256,
                correlation_id=correlation_id,
                reason=reason,
            )
            mutation = state.get("runtime_mutation")
            if not isinstance(mutation, Mapping):
                raise RuntimeTransitionPending("target-live recovery mutation identity missing")

        direction = _receiver_direction_for_bindings(source_binding, target_binding)
        communications_managed = direction != "none"
        source_absent_proven = False

        if state.get("phase") == "awaiting-successor-read":
            if state.get("runtime_swapped") is not True:
                raise RuntimeTransitionPending("target-live recovery pending without runtime swap proof")
            runtime_state = _read_json(_persona_state(persona, state_root) / "runtime.json")
            if not runtime_state or not _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
                raise RuntimeError("recorded target runtime does not match target-live recovery")
            handle = _handle_from_state(target_binding, runtime_state)
            _prove_compensation_target_live_runtime_after_rearm(
                target_binding,
                handle,
                state_root=state_root,
                context=context,
            )
            _prove_compensation_target_live_source_absence_if_distinct(
                source_binding,
                target_binding,
                context=context,
            )
            if communications_managed:
                _prove_receiver(source_binding, "absent", backend=backend)
                _prove_receiver(target_binding, "present", backend=backend)
            _prove_started_unverified_handoff(target_binding, state)
            return _compensation_target_live_recovery_receipt(
                state,
                status="already-pending",
                current_transition_sha256=observed_sha256,
            )

        try:
            source_absent_proven = _prove_compensation_target_live_source_absence_if_distinct(
                source_binding,
                target_binding,
                context=context,
            )
            replay_requires_rearm = _compensation_target_live_replay_requires_rearm(state, preexisting_marker)
            runtime_state = _read_json(_persona_state(persona, state_root) / "runtime.json")
            if replay_requires_rearm:
                _, pre_scope = _prove_compensation_target_live_recorded_runtime_before_rearm(
                    target_binding,
                    state_root=state_root,
                    context=context,
                )
                handle = _replace_runtime(target_binding, state_root=state_root, **_context_kwargs(context))
            elif (
                state.get("runtime_swapped") is True
                and runtime_state
                and _state_matches(target_binding, runtime_state, **_context_kwargs(context))
                and pane_exists(target_binding.controller.tmux, **_context_kwargs(context))
            ):
                handle = _handle_from_state(target_binding, runtime_state)
                pre_scope = {
                    "target_pre_rearm_live": True,
                    "target_pre_rearm_scope_proven": True,
                    "target_rearm_reused_recorded_runtime": True,
                }
            else:
                pre_scope = _prove_compensation_target_live_pre_rearm_scope(
                    target_binding,
                    mutation,
                    context=context,
                )
                handle = _replace_runtime(target_binding, state_root=state_root, **_context_kwargs(context))
            proofs = _prove_compensation_target_live_runtime_after_rearm(
                target_binding,
                handle,
                state_root=state_root,
                context=context,
            )
        except Exception as exc:
            failed = dict(state)
            failed["phase"] = str(failed.get("phase", "compensation-failed"))
            failed["runtime_swapped"] = bool(failed.get("runtime_swapped", False))
            _checkpoint_compensation_target_live_recovery_error(
                failed,
                state_root=state_root,
                persona=persona,
                recovery_id=recovery_id,
                exc=exc,
            )
            raise

        state = transition_state(persona, state_root=state_root)
        marker = dict(_compensation_target_live_recovery_marker(state) or marker)
        marker["status"] = "target-rearmed"
        marker["target_rearmed"] = True
        marker["target_rearmed_at"] = _iso(_utc_now())
        state["compensation_failed_target_live_recovery"] = marker
        state["phase"] = "preparing"
        state["runtime_swapped"] = True
        state["target_rearmed"] = True
        state["target_readiness_proven"] = True
        state["source_absent_proven"] = source_absent_proven
        state["target_rearm_proofs"] = {**pre_scope, **proofs}
        state.pop("recovery_error", None)
        _mark_runtime_mutation_resolved(state)
        _atomic_json(path, state)

        if communications_managed and not state.get("receiver_post_done"):
            try:
                _prove_receiver(source_binding, "absent", backend=backend)
                post = _run_integration_handoff(
                    target_binding,
                    direction,
                    "post",
                    backend=backend,
                    reason="recover-compensation-failed-target-live",
                )
                _prove_receiver(target_binding, "present", backend=backend)
            except Exception as exc:
                _checkpoint_compensation_target_live_recovery_error(
                    dict(transition_state(persona, state_root=state_root)),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            state = transition_state(persona, state_root=state_root)
            marker = dict(_compensation_target_live_recovery_marker(state) or marker)
            marker["status"] = "receiver-posted"
            marker["receiver_posted_at"] = _iso(_utc_now())
            state["compensation_failed_target_live_recovery"] = marker
            state["receiver_post_receipt"] = dataclasses.asdict(post)
            state["receiver_post_done"] = True
            state["receiver_transferred"] = True
            state["receiver_source_absent_proven"] = True
            state["receiver_target_present_proven"] = True
            state.pop("recovery_error", None)
            _atomic_json(path, state)
        elif communications_managed and state.get("receiver_post_done") is True:
            try:
                _prove_receiver(source_binding, "absent", backend=backend)
                _prove_receiver(target_binding, "present", backend=backend)
            except Exception as exc:
                _checkpoint_compensation_target_live_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            if state.get("receiver_transferred") is not True:
                state = dict(state)
                state["receiver_transferred"] = True
                state["receiver_source_absent_proven"] = True
                state["receiver_target_present_proven"] = True
                state.pop("recovery_error", None)
                _atomic_json(path, state)

        state = transition_state(persona, state_root=state_root)
        if not isinstance(state.get("chronicle_evidence"), str):
            try:
                evidence = _write_operator_handoff(persona, str(state["transition_id"]), state, state_root=state_root)
                contract = harness_integration.integration_for(target_binding.launch.harness).successor_evidence_contract(
                    persona,
                    str(state["transition_id"]),
                    evidence,
                )
            except Exception as exc:
                _checkpoint_compensation_target_live_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            state = transition_state(persona, state_root=state_root)
            marker = dict(_compensation_target_live_recovery_marker(state) or marker)
            marker["status"] = "handoff-written"
            marker["handoff_written_at"] = _iso(_utc_now())
            state["compensation_failed_target_live_recovery"] = marker
            state["operator_handoff_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
            state["chronicle_evidence"] = str(evidence)
            state["expected_successor_read_evidence"] = contract.expected
            state["successor_read_target"] = contract.target
            state["successor_evidence_kind"] = contract.evidence_kind
            state.pop("recovery_error", None)
            _atomic_json(path, state)
        else:
            try:
                evidence = _prove_started_unverified_handoff(target_binding, state)
            except Exception as exc:
                _checkpoint_compensation_target_live_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise

        try:
            _prompt_successor(
                handle,
                evidence,
                persona=persona,
                state_root=state_root,
                **_context_kwargs(context),
            )
        except Exception as exc:
            _checkpoint_compensation_target_live_recovery_error(
                dict(transition_state(persona, state_root=state_root)),
                state_root=state_root,
                persona=persona,
                recovery_id=recovery_id,
                exc=exc,
            )
            raise

        state = transition_state(persona, state_root=state_root)
        marker = dict(_compensation_target_live_recovery_marker(state) or marker)
        marker["status"] = "pending"
        marker["recovered_at"] = _iso(_utc_now())
        state["compensation_failed_target_live_recovery"] = marker
        state["phase"] = "awaiting-successor-read"
        state["receiver_transferred"] = communications_managed
        state.pop("recovery_error", None)
        _atomic_json(path, state)
        _, final_state, final_sha = _transition_bytes_and_state(path)
        return _compensation_target_live_recovery_receipt(
            final_state,
            status="pending",
            current_transition_sha256=final_sha,
        )


def _started_unverified_recovery_marker(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    marker = state.get("started_unverified_recovery")
    return marker if isinstance(marker, Mapping) else None


class _StartedUnverifiedTargetPaneNotLive(RuntimeError):
    pass


def _write_started_unverified_recovery_archive(
    persona: str,
    *,
    state_root: Path | None,
    recovery_id: str,
    predecessor_sha256: str,
    predecessor_state: Mapping[str, Any],
    reason: str,
) -> tuple[Path, str]:
    archive = _persona_state(persona, state_root) / f"operator-started-unverified-recovery-{recovery_id}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "pantheon-runtime-operator-started-unverified-recovery-archive/v1",
        "persona": persona,
        "recovery_id": recovery_id,
        "predecessor_sha256": predecessor_sha256,
        "predecessor_transition": dict(predecessor_state),
        "reason": reason,
        "archived_at": _iso(_utc_now()),
    }
    raw = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    try:
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing_raw = archive.read_bytes()
        try:
            existing = json.loads(existing_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing started-unverified recovery archive is unreadable") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("schema") != payload["schema"]
            or existing.get("persona") != persona
            or existing.get("recovery_id") != recovery_id
            or existing.get("predecessor_sha256") != predecessor_sha256
            or existing.get("predecessor_transition") != dict(predecessor_state)
            or existing.get("reason") != reason
        ):
            raise RuntimeError("existing started-unverified recovery archive does not match this recovery")
        return archive, hashlib.sha256(existing_raw).hexdigest()
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(archive.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return archive, hashlib.sha256(raw).hexdigest()


def _started_unverified_expected_fields(
    source_binding: Any,
    target_binding: Any,
    *,
    expected_transition_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    return {
        "schema": "pantheon-runtime-transition/v1",
        "phase": "preparing",
        "trigger": "operator",
        "transition_id": expected_transition_id,
        "correlation_id": correlation_id,
        "source_runtime": _runtime_ref(source_binding),
        "destination": _runtime_ref(target_binding),
        "outgoing_harness": source_binding.launch.harness,
        "outgoing_profile": source_binding.launch.profile,
        "incoming_harness": target_binding.launch.harness,
        "incoming_profile": target_binding.launch.profile,
    }


def _validate_started_unverified_initial_state(
    state: Mapping[str, Any],
    *,
    source_binding: Any,
    target_binding: Any,
    expected_transition_id: str,
    expected_transition_sha256: str,
    observed_sha256: str,
    correlation_id: str,
) -> None:
    expected = _started_unverified_expected_fields(
        source_binding,
        target_binding,
        expected_transition_id=expected_transition_id,
        correlation_id=correlation_id,
    )
    mismatches = {key: (state.get(key), value) for key, value in expected.items() if state.get(key) != value}
    if mismatches:
        raise RuntimeTransitionPending("runtime transition is not the expected started-unverified operator transition")
    if observed_sha256 != expected_transition_sha256:
        raise RuntimeTransitionPending("runtime transition digest mismatch")
    mutation = state.get("runtime_mutation")
    if (
        state.get("receiver_pre_done") is not True
        or state.get("runtime_swapped") is not False
        or not isinstance(mutation, Mapping)
        or mutation.get("status") != "started-unverified"
        or mutation.get("pane_may_be_mutated") is not True
        or mutation.get("target") != _runtime_ref(target_binding)
    ):
        raise RuntimeTransitionPending("runtime transition is not an exact started-unverified recovery state")


def _validate_started_unverified_marker(
    state: Mapping[str, Any],
    marker: Mapping[str, Any],
    *,
    source_binding: Any,
    target_binding: Any,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    observed_sha256: str,
    correlation_id: str,
) -> None:
    if state.get("phase") not in {"preparing", "awaiting-successor-read"}:
        raise RuntimeTransitionPending("started-unverified recovery phase is not resumable")
    if observed_sha256 != expected_transition_sha256:
        raise RuntimeTransitionPending("runtime transition digest mismatch")
    expected = _started_unverified_expected_fields(
        source_binding,
        target_binding,
        expected_transition_id=expected_transition_id,
        correlation_id=correlation_id,
    )
    for key, value in expected.items():
        if key != "phase" and state.get(key) != value:
            raise RuntimeTransitionPending("started-unverified recovery identity mismatch")
    marker_expected = {
        "schema": "pantheon-runtime-operator-started-unverified-recovery/v1",
        "recovery_id": recovery_id,
        "recovered_from_transition_id": expected_transition_id,
        "correlation_id": correlation_id,
        "source_runtime": _runtime_ref(source_binding),
        "destination": _runtime_ref(target_binding),
    }
    for key, value in marker_expected.items():
        if marker.get(key) != value:
            raise RuntimeTransitionPending("started-unverified recovery identity mismatch")
    if not isinstance(marker.get("recovered_from_sha256"), str):
        raise RuntimeTransitionPending("started-unverified recovery predecessor digest missing")
    if not isinstance(marker.get("predecessor_archive"), str) or not isinstance(marker.get("predecessor_archive_sha256"), str):
        raise RuntimeTransitionPending("started-unverified recovery archive identity missing")
    _validate_started_unverified_marker_archive(marker)


def _validate_started_unverified_marker_archive(marker: Mapping[str, Any]) -> None:
    archive = Path(str(marker.get("predecessor_archive", "")))
    expected_sha = marker.get("predecessor_archive_sha256")
    if not archive.is_file() or not isinstance(expected_sha, str):
        raise RuntimeTransitionPending("started-unverified recovery archive is unavailable")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RuntimeTransitionPending("started-unverified recovery archive digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeTransitionPending("started-unverified recovery archive is unreadable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != "pantheon-runtime-operator-started-unverified-recovery-archive/v1"
        or payload.get("recovery_id") != marker.get("recovery_id")
        or payload.get("predecessor_sha256") != marker.get("recovered_from_sha256")
        or not isinstance(payload.get("predecessor_transition"), Mapping)
        or payload.get("reason") != marker.get("reason")
    ):
        raise RuntimeTransitionPending("started-unverified recovery archive body mismatch")


def _started_unverified_recovery_receipt(
    state: Mapping[str, Any],
    *,
    status: Literal["pending", "already-pending"],
) -> OperatorStartedUnverifiedRecoveryReceipt:
    marker = _started_unverified_recovery_marker(state) or {}
    return OperatorStartedUnverifiedRecoveryReceipt(
        schema="pantheon-runtime-operator-started-unverified-recovery/v1",
        persona=str(state.get("persona", "")),
        status=status,
        recovery_id=str(marker.get("recovery_id", "")),
        recovered_from_transition_id=str(marker.get("recovered_from_transition_id", "")),
        recovered_from_sha256=str(marker.get("recovered_from_sha256", "")),
        correlation_id=str(state.get("correlation_id", "")),
        source_runtime=str(state.get("source_runtime", "")),
        destination=str(state.get("destination", "")),
        predecessor_archive=str(marker.get("predecessor_archive", "")),
        predecessor_archive_sha256=str(marker.get("predecessor_archive_sha256", "")),
        phase=str(state.get("phase", "")),
        runtime_swapped=bool(state.get("runtime_swapped", False)),
        receiver_pre_done=bool(state.get("receiver_pre_done", False)),
        receiver_post_done=bool(state.get("receiver_post_done", False)),
        chronicle_evidence=str(state.get("chronicle_evidence", "")),
        expected_successor_read_evidence=str(state.get("expected_successor_read_evidence", "")),
        recovered_at=str(marker.get("recovered_at", "")),
    )


def _set_started_unverified_recovery_error(
    state: dict[str, Any],
    *,
    recovery_id: str,
    exc: BaseException,
) -> None:
    marker = dict(_started_unverified_recovery_marker(state) or {})
    marker["status"] = "failed"
    marker["last_error_at"] = _iso(_utc_now())
    marker["last_error"] = _format_started_unverified_recovery_error(exc)
    state["started_unverified_recovery"] = marker
    state["recovery_error"] = marker["last_error"]


def _format_started_unverified_recovery_error(exc: BaseException) -> str:
    return chronicle.scrub_credentials(f"{type(exc).__name__}: {exc}")[:1000]


def _persist_started_unverified_recovery_error(
    state: dict[str, Any],
    *,
    state_root: Path,
    persona: str,
    recovery_id: str,
    exc: BaseException,
) -> None:
    _set_started_unverified_recovery_error(state, recovery_id=recovery_id, exc=exc)
    _atomic_json(_transition_path(persona, state_root), state)


def _prove_started_unverified_target_runtime(
    target_binding: Any,
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
) -> RuntimeHandle:
    runtime_state = _read_json(_persona_state(target_binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded target runtime does not match started-unverified recovery target")
    if not pane_exists(target_binding.controller.tmux, **_context_kwargs(context)):
        raise _StartedUnverifiedTargetPaneNotLive("target runtime pane is not live")
    handle = _handle_from_state(target_binding, runtime_state)
    _verify_runtime_input_ready(
        target_binding,
        RuntimeStart(str(runtime_state["runtime_scope_unit"]), False),
        state_root=state_root,
        **_context_kwargs(context),
    )
    return handle


def _checkpoint_started_unverified_target_rearm(
    state: Mapping[str, Any],
    *,
    state_root: Path,
    persona: str,
    marker: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = dict(state)
    updated_marker = dict(marker)
    updated_marker["status"] = "target-rearmed"
    updated_marker["target_rearmed_at"] = _iso(_utc_now())
    checkpoint["started_unverified_recovery"] = updated_marker
    checkpoint["runtime_swapped"] = True
    checkpoint.pop("recovery_error", None)
    _atomic_json(_transition_path(persona, state_root), checkpoint)
    return checkpoint


def _prove_started_unverified_handoff(
    target_binding: Any,
    state: Mapping[str, Any],
) -> Path:
    evidence = Path(str(state.get("chronicle_evidence", "")))
    expected_sha = state.get("operator_handoff_sha256")
    if not evidence.is_file() or not isinstance(expected_sha, str):
        raise RuntimeError("persisted recovery handoff is unavailable")
    evidence_bytes = evidence.read_bytes()
    observed_sha = hashlib.sha256(evidence_bytes).hexdigest()
    if observed_sha != expected_sha:
        raise RuntimeError("persisted recovery handoff digest mismatch")
    expected_receiver_line = f"receiver_transferred: {str(state.get('receiver_transferred') is True).lower()}"
    body_lines = evidence_bytes.decode("utf-8").splitlines()
    if expected_receiver_line not in body_lines:
        raise RuntimeError("persisted recovery handoff receiver_transferred mismatch")
    contract = harness_integration.integration_for(target_binding.launch.harness).successor_evidence_contract(
        str(state.get("persona", "")),
        str(state.get("transition_id", "")),
        evidence,
    )
    if (
        state.get("expected_successor_read_evidence") != contract.expected
        or state.get("successor_read_target") != contract.target
        or state.get("successor_evidence_kind") != contract.evidence_kind
    ):
        raise RuntimeError("persisted recovery handoff contract mismatch")
    return evidence


def recover_operator_started_unverified(
    source_binding: Any,
    target_binding: Any,
    *,
    recovery_id: str,
    expected_transition_id: str,
    expected_transition_sha256: str,
    correlation_id: str,
    state_root: Path,
    reason: str,
    context: TmuxRuntimeContext | None = None,
    backend: Any | None = None,
) -> OperatorStartedUnverifiedRecoveryReceipt:
    recovery_id = _validate_recovery_id(recovery_id)
    if source_binding.persona != target_binding.persona:
        raise ValueError("source and target personas must match")
    if not reason:
        raise ValueError("recovery reason is required")
    persona = source_binding.persona
    path = _transition_path(persona, state_root)
    with persona_operation_lock(persona, state_root=state_root):
        _, state, observed_sha256 = _transition_bytes_and_state(path)
        marker = _started_unverified_recovery_marker(state)
        if state.get("phase") == "awaiting-successor-read" and marker is not None:
            _validate_started_unverified_marker(
                state,
                marker,
                source_binding=source_binding,
                target_binding=target_binding,
                recovery_id=recovery_id,
                expected_transition_id=expected_transition_id,
                expected_transition_sha256=expected_transition_sha256,
                observed_sha256=observed_sha256,
                correlation_id=correlation_id,
            )
            direction = _receiver_direction_for_bindings(source_binding, target_binding)
            communications_managed = direction != "none"
            if state.get("runtime_swapped") is not True:
                raise RuntimeTransitionPending("started-unverified recovery is pending without runtime swap proof")
            if communications_managed and state.get("receiver_post_done") is not True:
                raise RuntimeTransitionPending("started-unverified recovery is pending without receiver post proof")
            if not isinstance(state.get("chronicle_evidence"), str):
                raise RuntimeTransitionPending("started-unverified recovery is pending without handoff proof")
            _prove_started_unverified_target_runtime(target_binding, state_root=state_root, context=context)
            if communications_managed:
                _prove_receiver(source_binding, "absent", backend=backend)
                _prove_receiver(target_binding, "present", backend=backend)
            _prove_started_unverified_handoff(target_binding, state)
            return _started_unverified_recovery_receipt(state, status="already-pending")
        if marker is None:
            _validate_started_unverified_initial_state(
                state,
                source_binding=source_binding,
                target_binding=target_binding,
                expected_transition_id=expected_transition_id,
                expected_transition_sha256=expected_transition_sha256,
                observed_sha256=observed_sha256,
                correlation_id=correlation_id,
            )
            archive, archive_sha256 = _write_started_unverified_recovery_archive(
                persona,
                state_root=state_root,
                recovery_id=recovery_id,
                predecessor_sha256=observed_sha256,
                predecessor_state=state,
                reason=reason,
            )
            _, reread_state, reread_sha256 = _transition_bytes_and_state(path)
            if reread_sha256 != observed_sha256 or reread_state != state:
                raise RuntimeTransitionPending("runtime transition changed during started-unverified recovery")
            marker = {
                "schema": "pantheon-runtime-operator-started-unverified-recovery/v1",
                "status": "in-progress",
                "recovery_id": recovery_id,
                "reason": reason,
                "correlation_id": correlation_id,
                "source_runtime": _runtime_ref(source_binding),
                "destination": _runtime_ref(target_binding),
                "recovered_from_transition_id": expected_transition_id,
                "recovered_from_sha256": observed_sha256,
                "predecessor_archive": str(archive),
                "predecessor_archive_sha256": archive_sha256,
                "started_at": _iso(_utc_now()),
            }
            state = dict(state)
            state["started_unverified_recovery"] = marker
            _atomic_json(path, state)
        else:
            _validate_started_unverified_marker(
                state,
                marker,
                source_binding=source_binding,
                target_binding=target_binding,
                recovery_id=recovery_id,
                expected_transition_id=expected_transition_id,
                expected_transition_sha256=expected_transition_sha256,
                observed_sha256=observed_sha256,
                correlation_id=correlation_id,
            )
        direction = _receiver_direction_for_bindings(source_binding, target_binding)
        communications_managed = direction != "none"
        replacement: RuntimeHandle
        if state.get("runtime_swapped") is True:
            try:
                replacement = _prove_started_unverified_target_runtime(
                    target_binding,
                    state_root=state_root,
                    context=context,
                )
            except _StartedUnverifiedTargetPaneNotLive:
                try:
                    replacement = _replace_runtime(target_binding, state_root=state_root, **_context_kwargs(context))
                except Exception as exc:
                    failed = dict(state)
                    failed["phase"] = "preparing"
                    failed["runtime_swapped"] = True
                    _persist_started_unverified_recovery_error(
                        failed,
                        state_root=state_root,
                        persona=persona,
                        recovery_id=recovery_id,
                        exc=exc,
                    )
                    raise
                state = _checkpoint_started_unverified_target_rearm(
                    transition_state(persona, state_root=state_root),
                    state_root=state_root,
                    persona=persona,
                    marker=dict(_started_unverified_recovery_marker(state) or marker),
                )
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
        else:
            try:
                replacement = _replace_runtime(target_binding, state_root=state_root, **_context_kwargs(context))
            except Exception as exc:
                failed = dict(state)
                failed["phase"] = "preparing"
                failed["runtime_swapped"] = False
                _persist_started_unverified_recovery_error(
                    failed,
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            state = transition_state(persona, state_root=state_root)
            marker = dict(_started_unverified_recovery_marker(state) or marker)
            marker["status"] = "target-running"
            marker["target_replaced_at"] = _iso(_utc_now())
            state["started_unverified_recovery"] = marker
            state["runtime_swapped"] = True
            state.pop("recovery_error", None)
            _mark_runtime_mutation_resolved(state)
            _atomic_json(path, state)
        if communications_managed and not state.get("receiver_post_done"):
            try:
                _prove_receiver(source_binding, "absent", backend=backend)
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            try:
                post = _run_integration_handoff(target_binding, direction, "post", backend=backend, reason="recover-started-unverified")
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            state = transition_state(persona, state_root=state_root)
            marker = dict(_started_unverified_recovery_marker(state) or marker)
            marker["status"] = "receiver-posted"
            marker["receiver_posted_at"] = _iso(_utc_now())
            state["started_unverified_recovery"] = marker
            state["receiver_post_receipt"] = dataclasses.asdict(post)
            state["receiver_post_done"] = True
            state["receiver_transferred"] = True
            state.pop("recovery_error", None)
            _atomic_json(path, state)
        elif communications_managed and state.get("receiver_post_done") is True:
            try:
                _prove_receiver(source_binding, "absent", backend=backend)
                _prove_receiver(target_binding, "present", backend=backend)
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            if state.get("receiver_transferred") is not True:
                state = dict(state)
                state["receiver_transferred"] = True
                state.pop("recovery_error", None)
                _atomic_json(path, state)
        if not isinstance(state.get("chronicle_evidence"), str):
            try:
                evidence = _write_operator_handoff(persona, str(state["transition_id"]), state, state_root=state_root)
                contract = harness_integration.integration_for(target_binding.launch.harness).successor_evidence_contract(
                    persona,
                    str(state["transition_id"]),
                    evidence,
                )
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
            state = transition_state(persona, state_root=state_root)
            marker = dict(_started_unverified_recovery_marker(state) or marker)
            marker["status"] = "handoff-written"
            marker["handoff_written_at"] = _iso(_utc_now())
            state["started_unverified_recovery"] = marker
            state["operator_handoff_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
            state["chronicle_evidence"] = str(evidence)
            state["expected_successor_read_evidence"] = contract.expected
            state["successor_read_target"] = contract.target
            state["successor_evidence_kind"] = contract.evidence_kind
            state.pop("recovery_error", None)
            _atomic_json(path, state)
        else:
            try:
                evidence = _prove_started_unverified_handoff(target_binding, state)
            except Exception as exc:
                _persist_started_unverified_recovery_error(
                    dict(state),
                    state_root=state_root,
                    persona=persona,
                    recovery_id=recovery_id,
                    exc=exc,
                )
                raise
        try:
            _prompt_successor(
                replacement,
                evidence,
                persona=persona,
                state_root=state_root,
                **_context_kwargs(context),
            )
        except Exception as exc:
            _persist_started_unverified_recovery_error(
                dict(state),
                state_root=state_root,
                persona=persona,
                recovery_id=recovery_id,
                exc=exc,
            )
            raise
        state = transition_state(persona, state_root=state_root)
        marker = dict(_started_unverified_recovery_marker(state) or marker)
        marker["status"] = "pending"
        marker["recovered_at"] = _iso(_utc_now())
        state["started_unverified_recovery"] = marker
        state["phase"] = "awaiting-successor-read"
        state["receiver_transferred"] = communications_managed
        state.pop("recovery_error", None)
        _atomic_json(path, state)
        return _started_unverified_recovery_receipt(state, status="pending")


def clear_profile_breaker(
    persona: str,
    profile: str,
    *,
    fresh_probe_evidence: str,
    reset_passed: bool,
    state_root: Path | None = None,
) -> None:
    """Close a cap breaker only after reset and a fresh authoritative probe."""

    if not reset_passed or not fresh_probe_evidence:
        raise ValueError("breaker reset requires passed reset time and fresh probe evidence")
    path = _transition_path(persona, state_root)
    state = _read_json(path)
    if state.get("capped_profile") != profile:
        raise ValueError(f"profile {profile!r} is not the recorded capped profile")
    state["breaker_open"] = False
    state["breaker_cleared_at"] = _iso(_utc_now())
    state["breaker_clear_evidence"] = fresh_probe_evidence
    _atomic_json(path, state)


def assert_drain_allowed(
    persona: str,
    drain_id: str,
    *,
    state_root: Path | None = None,
) -> None:
    """Fail transiently before provider invocation when a transition bars a drain."""

    state = transition_state(persona, state_root=state_root)
    phase = state.get("phase")
    if phase == "compensation-failed":
        raise RuntimeTransitionPending(
            f"{persona} runtime transition compensation failed; manual recovery required"
        )
    if phase in {"preparing", "awaiting-successor-read"}:
        raise RuntimeTransitionPending(f"{persona} runtime transition is {phase}")
    if phase == "completed" and state.get("barred_drain_id") == drain_id:
        raise RuntimeTransitionPending(
            f"mailbox drain {drain_id} was claimed before the runtime transition"
        )


def _begin_transition(
    persona: str,
    *,
    trigger: str,
    outgoing_profile: str,
    incoming_profile: str,
    outgoing_harness: str | None = None,
    incoming_harness: str | None = None,
    drain_id: str | None,
    state_root: Path | None,
) -> dict[str, Any]:
    current = transition_state(persona, state_root=state_root)
    if current.get("phase") == "compensation-failed":
        raise RuntimeTransitionPending(
            f"{persona} runtime transition compensation failed; manual recovery required"
        )
    if current.get("phase") in {"preparing", "awaiting-successor-read"}:
        return current
    payload = {
        "schema": "pantheon-runtime-transition/v1",
        "transition_id": _new_transition_id(),
        "persona": persona,
        "trigger": trigger,
        "outgoing_profile": outgoing_profile,
        "incoming_profile": incoming_profile,
        "outgoing_harness": outgoing_harness,
        "incoming_harness": incoming_harness,
        "barred_drain_id": drain_id,
        "phase": "preparing",
        "runtime_swapped": False,
        "successor_read_ack": False,
        "started_at": _iso(_utc_now()),
    }
    _atomic_json(_transition_path(persona, state_root), payload)
    _append_controller_evidence(
        persona,
        "transition_started",
        {
            "trigger": trigger,
            "outgoing_harness": outgoing_harness,
            "outgoing_profile": outgoing_profile,
            "incoming_harness": incoming_harness,
            "incoming_profile": incoming_profile,
            "barred_drain_id": drain_id,
        },
        state_root=state_root,
    )
    return payload


def begin_planned_transition(
    persona: str,
    *,
    outgoing_harness: str,
    outgoing_profile: str,
    incoming_harness: str,
    incoming_profile: str,
    message_id: str | None,
    primary_resets_at: str | None,
    quota_evidence: Mapping[str, str] | None,
    state_root: Path | None = None,
) -> PlannedTransitionBeginResult:
    """Begin the planned-quota barrier without executing the cooperative swap."""

    expected = {
        "trigger": "planned-quota",
        "outgoing_harness": outgoing_harness,
        "outgoing_profile": outgoing_profile,
        "incoming_harness": incoming_harness,
        "incoming_profile": incoming_profile,
    }
    current = transition_state(persona, state_root=state_root)
    phase = current.get("phase")
    if phase in {"preparing", "awaiting-successor-read"}:
        if all(current.get(key) == value for key, value in expected.items()):
            return PlannedTransitionBeginResult(
                "already-active",
                str(phase),
                str(current.get("transition_id")) if current.get("transition_id") else None,
            )
        return PlannedTransitionBeginResult(
            "conflict",
            str(phase),
            str(current.get("transition_id")) if current.get("transition_id") else None,
        )
    if phase == "completed" and all(
        current.get(key) == value for key, value in expected.items()
    ):
        return PlannedTransitionBeginResult(
            "already-completed",
            "completed",
            str(current.get("transition_id")) if current.get("transition_id") else None,
        )

    state = _begin_transition(
        persona,
        trigger="planned-quota",
        outgoing_profile=outgoing_profile,
        incoming_profile=incoming_profile,
        outgoing_harness=outgoing_harness,
        incoming_harness=incoming_harness,
        drain_id=None,
        state_root=state_root,
    )
    if primary_resets_at:
        state["primary_resets_at"] = primary_resets_at
    if quota_evidence:
        state["quota_evidence"] = dict(quota_evidence)
    if message_id:
        state["message_id"] = message_id
    _atomic_json(_transition_path(persona, state_root), state)
    _append_controller_evidence(
        persona,
        "planned_transition_barrier_started",
        {
            "trigger": "planned-quota",
            "outgoing_harness": outgoing_harness,
            "outgoing_profile": outgoing_profile,
            "incoming_harness": incoming_harness,
            "incoming_profile": incoming_profile,
            "message_id": message_id,
            "primary_resets_at": primary_resets_at,
        },
        state_root=state_root,
    )
    return PlannedTransitionBeginResult(
        "started",
        str(state.get("phase")) if state.get("phase") else None,
        str(state.get("transition_id")) if state.get("transition_id") else None,
    )


def acknowledge_successor_read(
    persona: str,
    *,
    evidence: str,
    state_root: Path | None = None,
    observation: Mapping[str, Any] | None = None,
) -> None:
    """Record structural successor-read evidence; terminal pixels are rejected.

    ``observation`` (T-5028 (h)): when the controller itself observed the read in
    the successor's native transcript (observe_successor_read), what it saw. The
    acceptance rule (validate_successor_ack) is the same as for a typed ack."""

    if not evidence or evidence.startswith("pane:"):
        raise ValueError("successor read acknowledgement requires structural evidence")
    path = _transition_path(persona, state_root)
    state = _read_json(path)
    if state.get("phase") != "awaiting-successor-read":
        raise RuntimeError("no successor read acknowledgement is pending")
    incoming = state.get("incoming_harness")
    if isinstance(incoming, str):
        harness_integration.integration_for(incoming).validate_successor_ack(state, evidence)
    else:
        expected = state.get("expected_successor_read_evidence")
        if isinstance(expected, str) and expected and evidence != expected:
            raise ValueError("successor read acknowledgement does not match transition target")
    state["successor_read_ack"] = True
    state["successor_read_evidence"] = evidence
    if observation is not None:
        state["successor_read_observation"] = dict(observation)
    state["phase"] = "completed"
    state["completed_at"] = _iso(_utc_now())
    _atomic_json(path, state)
    row = {
        "trigger": state.get("trigger"),
        "outgoing_harness": state.get("outgoing_harness"),
        "outgoing_profile": state.get("outgoing_profile"),
        "incoming_harness": state.get("incoming_harness"),
        "incoming_profile": state.get("incoming_profile"),
        "chronicle_evidence": state.get("chronicle_evidence"),
        "successor_read_evidence": evidence,
    }
    if observation is not None:
        row["successor_read_observation"] = dict(observation)
    _append_controller_evidence(persona, "successor_read_acknowledged", row, state_root=state_root)


#: T-5028 (h): how long a successor may stay unacknowledged before ONE escalation.
SUCCESSOR_ACK_TIMEOUT_SECONDS = 1800.0

SUCCESSOR_READ_NONCE_PREFIX = "pantheon-successor-read-nonce:v1:"
_SUCCESSOR_READ_NONCE_RE = re.compile(
    rf"{re.escape(SUCCESSOR_READ_NONCE_PREFIX)}[0-9a-f]{{64}}"
)


def _valid_successor_read_nonce(value: Any) -> bool:
    return isinstance(value, str) and _SUCCESSOR_READ_NONCE_RE.fullmatch(value) is not None


def _successor_copy_is_valid(path: Path, nonce: str, expected_sha256: str) -> bool:
    """Fail closed unless the private copy is the exact published artifact."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            return False
        payload = path.read_bytes()
        trailer = (nonce + "\n").encode("utf-8")
        return payload.endswith(trailer) and hashlib.sha256(payload).hexdigest() == expected_sha256
    except OSError:
        return False


def _successor_read_proof(state: Mapping[str, Any]) -> tuple[str, str] | None:
    evidence_path = state.get("successor_evidence_path")
    nonce = state.get("successor_read_nonce")
    expected_sha256 = state.get("successor_evidence_sha256")
    if not isinstance(evidence_path, str) or not evidence_path or not _valid_successor_read_nonce(nonce):
        return None
    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        return None
    if not _successor_copy_is_valid(Path(evidence_path), nonce, expected_sha256):
        return None
    return evidence_path, nonce


def _successor_prompted_at(state: Mapping[str, Any], path: Path) -> datetime | None:
    """When the successor was prompted. A transition from before T-5028 (h) has
    no successor_prompted_at; it times out from transition.json's mtime (a
    transition stuck that long is genuinely stuck)."""
    recorded = _parse_iso_or_none(state.get("successor_prompted_at"))
    if recorded is not None:
        return recorded
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _write_transition_field_if_awaiting(
    persona: str, seen: Mapping[str, Any], key: str, value: Any, *, state_root: Path | None
) -> bool:
    """Read-modify-write one field, re-checking right before the write that the
    SAME transition is still awaiting-successor-read (review MINOR 2: an ack that
    landed in between is never overwritten). The caller holds the operation lock;
    the typed ack-read CLI takes the same lock."""
    path = _transition_path(persona, state_root)
    fresh = _read_json(path)
    if fresh.get("phase") != "awaiting-successor-read" or fresh.get("transition_id") != seen.get("transition_id"):
        return False
    fresh[key] = value
    _atomic_json(path, fresh)
    return True


#: Review MINOR 2: how long the typed ack-read waits for the operation lock (a
#: consume fire holds it across its bounded provider-completion probe).
ACK_READ_LOCK_TIMEOUT_SECONDS = 300.0


def observe_successor_read(
    persona: str,
    *,
    binding: "PersonaBinding | None",
    state_root: Path | None,
    now: datetime | None = None,
) -> str:
    """T-5028 (h): the structural ack-read. Re-entrant, one pass per consume or
    quota fire (the caller holds persona_operation_lock); never raises.

    While a transition is ``awaiting-successor-read``: if the incoming harness's
    native transcript shows a tool call naming the continuity evidence whose
    paired successful output contains the transition nonce, then a completed
    turn after the prompt, acknowledge it exactly as a typed ack-read would
    (same validator) and record what was observed. Otherwise, past
    SUCCESSOR_ACK_TIMEOUT_SECONDS, escalate ONCE per transition. The typed
    prompt stays the fallback. Returns a short label (for tests and logs)."""
    refused = ""
    try:
        path = _transition_path(persona, state_root)
        state = _read_json(path)
        if state.get("phase") != "awaiting-successor-read":
            return "not-awaiting"
        current = now or _utc_now()
        prompted_at = _successor_prompted_at(state, path)
        incoming = state.get("incoming_harness")
        proof = _successor_read_proof(state)
        evidence_path = proof[0] if proof is not None else None
        successor_nonce = proof[1] if proof is not None else None
        expected = state.get("expected_successor_read_evidence")
        if (
            binding is not None
            and prompted_at is not None
            and isinstance(incoming, str)
            and incoming in harness_integration.configured_harness_names()
            and isinstance(evidence_path, str) and evidence_path
            and isinstance(successor_nonce, str) and successor_nonce
            and isinstance(expected, str) and expected
        ):
            cached = state.get("successor_transcript")
            # Delta review: an already-live successor (target-live recovery, attach)
            # started before the prompt; its rollout lives in that day's folder.
            recorded = _read_json(_persona_state(persona, state_root) / "runtime.json")
            search_from = (
                _parse_iso_or_none(recorded.get("started_at")) if recorded.get("harness") == incoming else None
            )
            if search_from is not None and search_from.tzinfo is None:
                search_from = search_from.replace(tzinfo=timezone.utc)
            transcript, observation = harness_integration.integration_for(incoming).successor_read_observed(
                account_home=binding.account_home,
                cwd=getattr(binding, "launch_cwd", None),
                since=prompted_at,
                evidence_path=evidence_path,
                successor_nonce=successor_nonce,
                cached_transcript=Path(cached) if isinstance(cached, str) and cached else None,
                search_from=search_from,
            )
            if transcript is not None and str(transcript) != cached and observation is None:
                # Review MINOR 1: cache the resolved transcript so later fires never
                # search ~/.codex/sessions again (re-resolved only if it disappears).
                _write_transition_field_if_awaiting(
                    persona, state, "successor_transcript", str(transcript), state_root=state_root
                )
            if observation is not None:
                try:
                    acknowledge_successor_read(
                        persona, evidence=expected, state_root=state_root, observation=observation
                    )
                    return "acknowledged"
                except (ValueError, RuntimeError) as exc:
                    # The validator refused (e.g. operator handoff hash mismatch): no
                    # ack, and the timeout below still escalates once.
                    refused = f"{type(exc).__name__}: {exc}"[:300]
        if prompted_at is None or (current - prompted_at).total_seconds() < SUCCESSOR_ACK_TIMEOUT_SECONDS:
            return "ack-refused" if refused else "waiting"
        if state.get("ack_timeout_escalated_at"):
            return "escalated-already"
        transition_id = str(state.get("transition_id") or "unknown")
        _route_cooldown_gated_alert(
            persona,
            f"runtime-controller {persona}: the {incoming or '?'} successor has not acknowledged the "
            f"runtime transfer {transition_id} ({state.get('trigger')}) for "
            f"{int((current - prompted_at).total_seconds())}s: no typed ack-read and no transcript "
            f"evidence of reading {evidence_path}"
            + (f" (an observed read was refused: {refused})" if refused else "")
            + ". Mail may not be reaching the seat; check the pane.",
            f"successor-ack-timeout ({persona})",
            stamp_name=f"successor-ack-timeout-{transition_id}.json",
            state_root=state_root,
            now=current,
        )
        _write_transition_field_if_awaiting(
            persona, state, "ack_timeout_escalated_at", _iso(current), state_root=state_root
        )
        _append_controller_evidence(
            persona, "successor_ack_timeout_escalated",
            {"waited_seconds": int((current - prompted_at).total_seconds())}, state_root=state_root,
        )
        return "escalated"
    except Exception as exc:  # noqa: BLE001 -- the observer never breaks a fire
        try:
            print(f"[runtime_controller] {persona}: successor-read observer failed: {exc!r}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return "error"


def _chronicle_root() -> Path | None:
    value = os.environ.get("PANTHEON_CHRONICLE_ROOT")
    return Path(value) if value else None


def _strict_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Write a forced-failover receipt with the same fsync boundary as state."""

    _atomic_json(path, payload)
    check = _read_json(path)
    if check != payload:
        raise chronicle.ChronicleDurabilityError(
            "forced-failover receipt readability verification failed"
        )


def _forced_receipt_history_path(persona: str, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "forced-failover-history.jsonl"


def _append_forced_receipt_history(
    persona: str,
    receipt: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> None:
    _append_jsonl(_forced_receipt_history_path(persona, state_root), receipt)


def _read_forced_receipt(path: Path) -> dict[str, Any]:
    receipt = _read_json(path)
    if not receipt:
        raise chronicle.ChronicleDurabilityError("forced-failover receipt is unreadable")
    return receipt


def _completed_forced_receipt_matches(
    state: dict[str, Any],
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str,
    correlation_id: str,
) -> bool:
    receipt_ref = state.get("forced_receipt")
    if not isinstance(receipt_ref, str) or not receipt_ref:
        return False
    try:
        receipt = _read_forced_receipt(Path(receipt_ref))
    except chronicle.ChronicleDurabilityError:
        return False
    expected = {
        "schema": "pantheon-forced-failover/v1",
        "persona": target.persona,
        "transition_id": state.get("transition_id"),
        "previous_harness": current.profile.harness,
        "previous_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "chronicle_entry": state.get("chronicle_evidence"),
    }
    return all(receipt.get(key) == value for key, value in expected.items())


def _completed_forced_transition_matches(
    state: dict[str, Any],
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str,
    correlation_id: str,
) -> bool:
    return all(
        (
            state.get("phase") == "completed",
            state.get("successor_read_ack") is True,
            state.get("trigger") == "forced-cap",
            state.get("handoff_kind") == "forced-factual",
            state.get("outgoing_harness") == current.profile.harness,
            state.get("outgoing_profile") == current.profile.profile,
            state.get("incoming_harness") == target.launch.harness,
            state.get("incoming_profile") == target.launch.profile,
            state.get("message_id") == message_id,
            state.get("correlation_id") == correlation_id,
            isinstance(state.get("chronicle_evidence"), str),
            _completed_forced_receipt_matches(
                state,
                current,
                target,
                message_id=message_id,
                correlation_id=correlation_id,
            ),
        )
    )


def _record_successor_target(
    persona: str,
    state: dict[str, Any],
    evidence: Path,
    *,
    state_root: Path | None,
) -> dict[str, Any]:
    transition_id = str(state.get("transition_id") or _new_transition_id())
    state["transition_id"] = transition_id
    state["expected_successor_read_evidence"] = _successor_read_evidence(
        evidence,
        transition_id=transition_id,
    )
    state["successor_read_target"] = str(evidence)
    _atomic_json(_transition_path(persona, state_root), state)
    _append_controller_evidence(
        persona,
        "successor_read_target_recorded",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": state.get("incoming_harness"),
            "incoming_profile": state.get("incoming_profile"),
            "chronicle_evidence": str(evidence),
            "expected_successor_read_evidence": state[
                "expected_successor_read_evidence"
            ],
        },
        state_root=state_root,
    )
    return state


def _existing_chronicle(
    persona: str,
    *,
    expected_session_id: str | None,
) -> Path | None:
    root = _chronicle_root()
    entry = chronicle.latest(persona, root=root)
    if entry is None:
        return None
    if expected_session_id is not None and entry.get("uuid") != expected_session_id:
        return None
    path = chronicle.snapshot_path(persona, entry, root=root)
    if path is None or not path.is_file():
        return None
    try:
        if not path.read_text(encoding="utf-8").strip():
            return None
    except OSError:
        return None
    return path


def _replace_runtime(
    target: "PersonaBinding",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
    on_started: RuntimeStartedCallback | None = None,
) -> RuntimeHandle:
    from tools.persona_bringup import build_adapter

    adapter = build_adapter(target, harness_home=target.account_home)
    boundary = _utc_now()
    runtime_start = _start_runtime(
        target,
        adapter,
        replace=_target_known(target.controller.tmux, **_context_kwargs(context)),
        state_root=state_root,
        **_context_kwargs(context),
    )
    try:
        if on_started is not None:
            on_started(_RuntimeStartEvent(started=runtime_start, boundary=boundary))
        _verify_started_runtime(
            target, runtime_start, state_root=state_root, **_context_kwargs(context)
        )
        try:
            _verify_runtime_input_ready(
                target, runtime_start, state_root=state_root, **_context_kwargs(context)
            )
        except RuntimeError:
            _clear_stale_runtime_state(target.persona, state_root=state_root)
            raise
        started = _utc_now()
        provenance = _controller_source_provenance(
            runtime_start_boundary_at=boundary,
            runtime_start_observed_at=started,
        )
        _atomic_json(
            _persona_state(target.persona, state_root) / "runtime.json",
            _runtime_payload(
                target,
                session_id=None,
                started_at=started,
                runtime_scope_unit=runtime_start.scope_unit,
                source_provenance=provenance,
            ),
        )
        handle = RuntimeHandle(
            profile=target.launch,
            tmux=target.controller.tmux,
            session_id=None,
            started_at=started,
        )
        # T-4862: the single choke point for cooperative/forced/operator swaps (and
        # their rollback), so `harness status` never reports a stale runtime.
        # Cache only; best-effort -- a cache write must never fail a swap.
        try:
            write_active_runtime(
                target.persona,
                target,
                handle,
                pid=_pane_process_pid(target.controller.tmux, **_context_kwargs(context)),
                state_root=state_root,
            )
        except Exception:  # noqa: BLE001
            pass
        return handle
    except RuntimeReplacementError:
        raise
    except Exception as exc:
        raise RuntimeReplacementError(
            str(exc),
            pane_may_be_mutated=True,
            started=runtime_start,
            original=exc,
        ) from exc


def receiver_direction_for_transition(current_profile: Any, target_profile: Any) -> ReceiverDirection:
    outgoing = str(getattr(current_profile, "harness", ""))
    incoming = str(getattr(target_profile, "harness", ""))
    if outgoing == incoming:
        return "none"
    try:
        direction = harness_integration.integration_for(incoming).receiver_direction(current_profile, target_profile)
    except (KeyError, ValueError):
        return "none"
    return direction or "none"  # type: ignore[return-value]


def receiver_handoff_required(binding: Any) -> bool:
    communications = getattr(binding, "communications", None)
    return isinstance(communications, Mapping) and communications.get("kind") == "telegram"


def receiver_handoff_actions(
    binding: Any,
    direction: ReceiverDirection,
    phase: ReceiverPhase,
    *,
    prior_runtime: PriorRuntime = "live",
) -> tuple[str, ...]:
    return harness_integration.receiver_handoff_actions(binding, direction, phase, prior_runtime=prior_runtime)


def process_probe_matches(
    process: Mapping[str, str],
    *,
    account: str,
    session: str,
    path: str,
) -> bool:
    return (
        process.get("account") == account
        and process.get("session") == session
        and process.get("path") == path
    )


def _binding_account(binding: Any) -> str:
    return str(getattr(binding, "account", os.environ.get("USER", "")))


def _run_bridge_handoff_subprocess(
    binding: Any,
    direction: ReceiverDirection,
    phase: ReceiverPhase,
    *,
    reason: str,
    timeout_seconds: int,
    inner_timeout_seconds: int | None = None,
    prior_runtime: PriorRuntime = "live",
) -> ReceiverHandoffReceipt:
    """`timeout_seconds` bounds the subprocess. `inner_timeout_seconds` is the
    bridge's own --timeout-seconds deadline; it defaults to the same value, so
    existing callers are unchanged (T-4178 passes a shorter one).
    `prior_runtime` (T-4184) is passed to the bridge only when it is not the
    default, so existing callers' argv stays byte-identical."""
    extra = () if prior_runtime == "live" else ("--prior-runtime", prior_runtime)
    argv = (
        sys.executable,
        str(Path(__file__).resolve().parent / "codex_telegram_bridge.py"),
        "handoff",
        "--persona",
        str(getattr(binding, "persona")),
        "--direction",
        direction,
        "--phase",
        phase,
        "--reason",
        reason,
        "--timeout-seconds",
        str(timeout_seconds if inner_timeout_seconds is None else inner_timeout_seconds),
        *extra,
        "--json",
    )
    result = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("receiver handoff returned invalid JSON") from exc
    if result.returncode != 0 or payload.get("ok") is not True:
        raise RuntimeError(str(payload.get("error") or "receiver handoff failed"))
    return ReceiverHandoffReceipt(
        schema=str(payload.get("schema", "pantheon-codex-telegram-handoff/v1")),
        persona=str(payload.get("persona", getattr(binding, "persona"))),
        account=str(payload.get("account", _binding_account(binding))),
        direction=direction,
        phase=phase,
        ok=True,
        operations=tuple(str(item) for item in payload.get("operations", ())),
        error=None,
    )


def run_receiver_handoff(
    binding: Any,
    direction: ReceiverDirection,
    phase: ReceiverPhase,
    *,
    backend: Any | None = None,
    reason: str = "runtime-transition",
    timeout_seconds: int = 90,
) -> ReceiverHandoffReceipt:
    if direction == "none":
        return ReceiverHandoffReceipt(
            schema="pantheon-codex-telegram-handoff/v1",
            persona=str(getattr(binding, "persona")),
            account=_binding_account(binding),
            direction=direction,
            phase=phase,
            ok=True,
            operations=(),
        )
    if backend is None:
        return _run_bridge_handoff_subprocess(
            binding,
            direction,
            phase,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
    try:
        integration = harness_integration.integration_for(getattr(binding, "launch").harness)
        receipt = integration.run_receiver_handoff(
            binding,
            direction,
            phase,  # type: ignore[arg-type]
            backend=backend,
            reason=reason,
        )
    except AttributeError:
        receipt = harness_integration.run_receiver_handoff_actions(
            binding,
            direction,
            phase,  # type: ignore[arg-type]
            backend=backend,
        )
    return ReceiverHandoffReceipt(
        schema="pantheon-codex-telegram-handoff/v1",
        persona=str(getattr(binding, "persona")),
        account=_binding_account(binding),
        direction=direction,
        phase=phase,
        ok=receipt.ok,
        operations=receipt.operations,
    )


def _previous_runtime_binding(target: Any, current_profile: Any) -> Any:
    if dataclasses.is_dataclass(target):
        return dataclasses.replace(
            target,
            launch=current_profile,
            selected_harness=getattr(current_profile, "harness", getattr(target, "selected_harness", "")),
        )
    previous = copy.copy(target)
    previous.launch = current_profile
    previous.selected_harness = getattr(current_profile, "harness", getattr(previous, "selected_harness", ""))
    return previous


def _runtime_reference_from_launch(launch: HarnessLaunchConfig) -> str:
    return f"{launch.harness}/{launch.profile}"


def _transition_destination(target: Any) -> str:
    return _runtime_reference_from_launch(target.launch)


def _transition_matches_operator(
    state: Mapping[str, Any],
    *,
    correlation_id: str,
    target: Any,
) -> bool:
    outgoing_harness = state.get("outgoing_harness")
    outgoing_profile = state.get("outgoing_profile")
    source_runtime = f"{outgoing_harness}/{outgoing_profile}"
    return (
        state.get("trigger") == "operator"
        and state.get("correlation_id") == correlation_id
        and state.get("destination") == _transition_destination(target)
        and state.get("source_runtime") == source_runtime
        and state.get("incoming_harness") == target.launch.harness
        and state.get("incoming_profile") == target.launch.profile
    )


def _replacement_error_pane_may_be_mutated(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeReplacementError) and exc.pane_may_be_mutated


def _unresolved_runtime_mutation(state: Mapping[str, Any]) -> bool:
    mutation = state.get("runtime_mutation")
    return (
        isinstance(mutation, Mapping)
        and mutation.get("status") == "started-unverified"
        and mutation.get("pane_may_be_mutated") is True
        and state.get("runtime_swapped") is not True
    )


def _operator_runtime_started_callback(
    *,
    state: dict[str, Any],
    state_root: Path,
    target_binding: Any,
) -> RuntimeStartedCallback:
    def record(started: _RuntimeStartEvent) -> None:
        state["phase"] = "preparing"
        state["runtime_swapped"] = False
        state["runtime_mutation"] = {
            "status": "started-unverified",
            "pane_may_be_mutated": True,
            "target": _transition_destination(target_binding),
            "scope_unit": started.started.scope_unit,
            "created_scope": started.started.created_scope,
            "boundary": _iso(started.boundary),
            "recorded_at": _iso(_utc_now()),
        }
        _atomic_json(_transition_path(target_binding.persona, state_root), state)

    return record


def _mark_runtime_mutation_resolved(state: dict[str, Any]) -> None:
    mutation = state.get("runtime_mutation")
    if isinstance(mutation, Mapping):
        updated = dict(mutation)
        updated["status"] = "resolved"
        updated["pane_may_be_mutated"] = False
        updated["resolved_at"] = _iso(_utc_now())
        state["runtime_mutation"] = updated


def _mark_runtime_mutation_compensated(state: dict[str, Any]) -> None:
    mutation = state.get("runtime_mutation")
    if isinstance(mutation, Mapping):
        updated = dict(mutation)
    else:
        updated = {}
    updated["status"] = "compensated"
    updated["pane_may_be_mutated"] = False
    updated["compensated_at"] = _iso(_utc_now())
    state["runtime_mutation"] = updated


def _operator_compensate_runtime_mutation(
    *,
    state: dict[str, Any],
    current_binding: Any,
    target_binding: Any,
    state_root: Path,
    context: TmuxRuntimeContext | None,
    backend: Any | None,
    reason: str,
) -> dict[str, Any]:
    reverse_direction = _reverse_receiver_direction(current_binding, target_binding)
    communications_managed = reverse_direction != "none"
    compensation: dict[str, Any] = {"reason": reason}
    try:
        if communications_managed:
            compensation["target_pre"] = dataclasses.asdict(
                _run_integration_handoff(
                    target_binding,
                    reverse_direction,
                    "pre",
                    backend=backend,
                    reason=f"{state.get('trigger', 'operator')}:partial",
                )
            )
        _replace_runtime(current_binding, state_root=state_root, **_context_kwargs(context))
        if communications_managed:
            compensation["source_post"] = dataclasses.asdict(
                _run_integration_handoff(
                    current_binding,
                    reverse_direction,
                    "post",
                    backend=backend,
                    reason=f"{state.get('trigger', 'operator')}:rollback",
                )
            )
        state["phase"] = "compensated"
        state["runtime_swapped"] = False
        state["compensation"] = compensation
        _mark_runtime_mutation_compensated(state)
        _atomic_json(_transition_path(current_binding.persona, state_root), state)
        return compensation
    except Exception as rollback_exc:
        state["phase"] = "compensation-failed"
        state["compensation_error"] = f"{type(rollback_exc).__name__}: {rollback_exc}"
        mutation = state.get("runtime_mutation")
        if isinstance(mutation, Mapping):
            updated = dict(mutation)
            updated["status"] = "compensation-failed"
            state["runtime_mutation"] = updated
        _atomic_json(_transition_path(current_binding.persona, state_root), state)
        raise


_PRESERVED_POLICY_FIELDS = (
    "breaker_open",
    "capped_profile",
    "primary_resets_at",
    "quota_evidence",
    "breaker_opened_at",
    "breaker_cleared_at",
    "breaker_clear_evidence",
)


def _preserved_policy_state(source: Mapping[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in _PRESERVED_POLICY_FIELDS if key in source}


def _validate_operator_waiting_state(state: Mapping[str, Any]) -> None:
    if state.get("runtime_swapped") is not True:
        raise RuntimeError("operator transition is waiting without runtime swap proof")
    if not isinstance(state.get("expected_successor_read_evidence"), str):
        raise RuntimeError("operator transition is waiting without successor evidence")
    if not isinstance(state.get("chronicle_evidence"), str):
        raise RuntimeError("operator transition is waiting without handoff target")


def _write_operator_handoff(
    persona: str,
    transition_id: str,
    state: Mapping[str, Any],
    *,
    state_root: Path,
) -> Path:
    evidence = _persona_state(persona, state_root) / f"operator-handoff-{transition_id}.md"
    payload = "\n".join(
        [
            "# Runtime Transfer",
            "",
            f"persona: {persona}",
            f"transition_id: {transition_id}",
            f"trigger: {state.get('trigger', 'operator')}",
            f"correlation_id: {state.get('correlation_id', '')}",
            f"source_runtime: {state.get('source_runtime', '')}",
            f"destination: {state.get('destination', '')}",
            f"outgoing: {state.get('outgoing_harness', '')}/{state.get('outgoing_profile', '')}",
            f"incoming: {state.get('incoming_harness', '')}/{state.get('incoming_profile', '')}",
            f"runtime_swapped: {str(state.get('runtime_swapped') is True).lower()}",
            f"receiver_transferred: {str(state.get('receiver_transferred') is True).lower()}",
            f"written_at: {_iso(_utc_now())}",
            "",
        ]
    )
    fd, tmp_name = tempfile.mkstemp(prefix=f".{evidence.name}.", dir=evidence.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, evidence)
        dir_fd = os.open(evidence.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return evidence


def _operator_receipt(target: Any, state: Mapping[str, Any], *, status: str) -> OperatorSwapReceipt:
    return OperatorSwapReceipt(
        schema="pantheon-runtime-operator-target-swap/v1",
        persona=str(getattr(target, "persona")),
        trigger=str(state.get("trigger", "operator")),
        correlation_id=str(state.get("correlation_id", "")),
        destination=str(state.get("destination", _transition_destination(target))),
        status=status,
        phase=str(state.get("phase", "")),
        source_runtime=str(state.get("source_runtime", "")),
        source_transition_id=str(state.get("source_transition_id", "")),
        outgoing_harness=str(state.get("outgoing_harness", "")),
        outgoing_profile=str(state.get("outgoing_profile", "")),
        incoming_harness=str(state.get("incoming_harness", "")),
        incoming_profile=str(state.get("incoming_profile", "")),
        receiver_transferred=bool(state.get("receiver_transferred", False)),
    )


def _state_runtime_binding(template: Any, state: Mapping[str, Any], *, harness_key: str, profile_key: str) -> Any:
    launch = dataclasses.replace(
        template.launch,
        harness=str(state[harness_key]),
        profile=str(state[profile_key]),
    )
    return _previous_runtime_binding(template, launch)


def _validate_operator_retry_state(
    target_binding: Any,
    state: Mapping[str, Any],
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
    backend: Any | None,
) -> None:
    _validate_operator_waiting_state(state)
    runtime_state = _read_json(_persona_state(target_binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(target_binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded target runtime does not match operator transition")
    if not pane_exists(target_binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("target runtime pane is not live")
    if receiver_handoff_required(target_binding) and state.get("outgoing_harness") != state.get("incoming_harness"):
        _prove_receiver(target_binding, "present", backend=backend)
        source_binding = _state_runtime_binding(
            target_binding,
            state,
            harness_key="outgoing_harness",
            profile_key="outgoing_profile",
        )
        _prove_receiver(source_binding, "absent", backend=backend)


def _validate_operator_active_noop(
    binding: Any,
    transition: Mapping[str, Any],
    *,
    state_root: Path,
    context: TmuxRuntimeContext | None,
    backend: Any | None,
) -> None:
    runtime_state = _read_json(_persona_state(binding.persona, state_root) / "runtime.json")
    if not runtime_state or not _state_matches(binding, runtime_state, **_context_kwargs(context)):
        raise RuntimeError("recorded active runtime does not match requested destination")
    if not pane_exists(binding.controller.tmux, **_context_kwargs(context)):
        raise RuntimeError("active runtime pane is not live")
    if receiver_handoff_required(binding):
        _prove_receiver(binding, "present", backend=backend)
        if (
            transition.get("runtime_swapped") is True
            and transition.get("incoming_harness") == binding.launch.harness
            and transition.get("incoming_profile") == binding.launch.profile
            and transition.get("outgoing_harness") != transition.get("incoming_harness")
        ):
            source = _state_runtime_binding(
                binding,
                transition,
                harness_key="outgoing_harness",
                profile_key="outgoing_profile",
            )
            _prove_receiver(source, "absent", backend=backend)


def _prove_receiver(
    binding: Any,
    expected: Literal["present", "absent"],
    *,
    backend: Any | None,
) -> ReceiverStateProof:
    integration = harness_integration.integration_for(getattr(binding, "launch").harness)
    proof = integration.prove_receiver_state(binding, expected, backend=backend)
    if proof.state == "not-applicable":
        return proof
    if proof.state != expected:
        raise RuntimeError(f"receiver state is {proof.state}; expected {expected}")
    return proof


def _receiver_direction_for_bindings(current_binding: Any, target: Any) -> str:
    if current_binding.launch.harness == target.launch.harness:
        return "none"
    if not receiver_handoff_required(target):
        return "none"
    target_integration = harness_integration.integration_for(target.launch.harness)
    if not target_integration.communications_managed:
        return "none"
    direction = target_integration.receiver_direction(current_binding.launch, target.launch)
    return direction or "none"


def _reverse_receiver_direction(previous: Any, target: Any) -> str:
    return _receiver_direction_for_bindings(target, previous)


def _run_integration_handoff(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    backend: Any | None,
    reason: str,
    prior_runtime: PriorRuntime = "live",
) -> ReceiverHookReceipt:
    if direction == "none":
        return ReceiverHookReceipt(True, (), {"direction": "none", "phase": phase})
    integration = harness_integration.integration_for(binding.launch.harness)
    return integration.run_receiver_handoff(
        binding, direction, phase, backend=backend, reason=reason, prior_runtime=prior_runtime
    )


def transition_runtime_with_receiver_handoff(
    current: Any,
    target: "PersonaBinding",
    *,
    backend: Any | None = None,
    replace: Any | None = None,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
    reason: str = "runtime-transition",
) -> RuntimeHandle:
    current_profile = getattr(current, "profile", current)
    previous = _previous_runtime_binding(target, current_profile)
    direction = _receiver_direction_for_bindings(previous, target)
    _run_integration_handoff(target, direction, "pre", backend=backend, reason=reason)
    replace_fn = replace or (lambda item: _replace_runtime(item, state_root=state_root, **_context_kwargs(context)))
    replaced = False
    try:
        handle = replace_fn(target)
        replaced = True
        _run_integration_handoff(target, direction, "post", backend=backend, reason=reason)
        return handle
    except Exception as exc:
        compensation_errors: list[str] = []
        try:
            reverse_direction = _reverse_receiver_direction(previous, target)
            if replaced or _replacement_error_pane_may_be_mutated(exc):
                _run_integration_handoff(target, reverse_direction, "pre", backend=backend, reason=f"{reason}:partial")
                replace_fn(previous)
            _run_integration_handoff(previous, reverse_direction, "post", backend=backend, reason=f"{reason}:rollback")
        except Exception as rollback_exc:
            compensation_errors.append(f"{type(rollback_exc).__name__}: {rollback_exc}")
        if compensation_errors:
            raise RuntimeError(
                f"receiver handoff failed closed: {type(exc).__name__}: {exc}; "
                f"compensation failed: {'; '.join(compensation_errors)}"
            ) from exc
        raise


def _replace_runtime_with_receiver_handoff(
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
    reason: str = "runtime-transition",
    backend: Any | None = None,
) -> RuntimeHandle:
    return transition_runtime_with_receiver_handoff(
        current,
        target,
        backend=backend,
        state_root=state_root,
        context=context,
        reason=reason,
    )


def _operator_target_swap_resolved(
    current_binding: "PersonaBinding",
    target_binding: "PersonaBinding",
    *,
    trigger: Literal["operator"],
    correlation_id: str,
    state_root: Path,
    context: TmuxRuntimeContext | None = None,
    backend: Any | None = None,
    current_handle: RuntimeHandle | None = None,
) -> OperatorSwapReceipt:
    source_state = transition_state(current_binding.persona, state_root=state_root)
    existing_phase = source_state.get("phase")
    source_runtime = _runtime_reference_from_launch(current_binding.launch)
    if existing_phase == "compensation-failed":
        raise RuntimeTransitionPending(
            "operator runtime transition compensation failed; manual recovery required"
        )
    if existing_phase in {"preparing", "awaiting-successor-read"}:
        if _transition_matches_operator(
            source_state,
            correlation_id=correlation_id,
            target=target_binding,
        ):
            state = dict(source_state)
            transition_id = str(state["transition_id"])
            if existing_phase == "preparing" and _started_unverified_recovery_marker(state) is not None:
                raise RuntimeTransitionPending(
                    "started-unverified recovery is in progress; use --recover-started-unverified"
                )
            if existing_phase == "preparing" and _unresolved_runtime_mutation(state):
                _operator_compensate_runtime_mutation(
                    state=state,
                    current_binding=current_binding,
                    target_binding=target_binding,
                    state_root=state_root,
                    context=context,
                    backend=backend,
                    reason="retry-started-unverified",
                )
                raise RuntimeTransitionPending(
                    "operator runtime mutation compensated; fresh operator initiation required"
                )
            if existing_phase == "awaiting-successor-read":
                _validate_operator_retry_state(
                    target_binding,
                    state,
                    state_root=state_root,
                    context=context,
                    backend=backend,
                )
                return _operator_receipt(target_binding, state, status="pending")
        else:
            raise RuntimeTransitionPending("another runtime transition is already active")
    elif existing_phase == "completed":
        if _transition_matches_operator(
            source_state,
            correlation_id=correlation_id,
            target=target_binding,
        ):
            _validate_operator_retry_state(
                target_binding,
                source_state,
                state_root=state_root,
                context=context,
                backend=backend,
            )
            return _operator_receipt(target_binding, source_state, status="completed")
        state = {}
        transition_id = ""
    else:
        state = {}
        transition_id = ""

    direction_source = current_binding
    if state:
        direction_source = _state_runtime_binding(
            target_binding,
            state,
            harness_key="outgoing_harness",
            profile_key="outgoing_profile",
        )
    direction = _receiver_direction_for_bindings(direction_source, target_binding)
    communications_managed = direction != "none"
    if not state:
        current = current_handle or recorded_runtime_handle(current_binding, state_root=state_root)
        if current.profile != current_binding.launch:
            raise RuntimeError("recorded runtime does not match selected source runtime")
        if not pane_exists(current_binding.controller.tmux, **_context_kwargs(context)):
            raise RuntimeError("source runtime pane is not live")
        if communications_managed:
            _prove_receiver(current_binding, "present", backend=backend)
            _prove_receiver(target_binding, "absent", backend=backend)

        transition_id = _new_transition_id()
        source_transition_id = str(source_state.get("transition_id", ""))
        state = {
            "schema": "pantheon-runtime-transition/v1",
            "transition_id": transition_id,
            "persona": current_binding.persona,
            "trigger": trigger,
            "correlation_id": correlation_id,
            "destination": _transition_destination(target_binding),
            "source_runtime": source_runtime,
            "source_transition_id": source_transition_id,
            "outgoing_harness": current_binding.launch.harness,
            "outgoing_profile": current_binding.launch.profile,
            "incoming_harness": target_binding.launch.harness,
            "incoming_profile": target_binding.launch.profile,
            "phase": "preparing",
            "runtime_swapped": False,
            "receiver_pre_done": False,
            "receiver_post_done": False,
            "receiver_transferred": False,
            "successor_read_ack": False,
            "started_at": _iso(_utc_now()),
            **_preserved_policy_state(source_state),
        }
        _atomic_json(_transition_path(current_binding.persona, state_root), state)
        _append_controller_evidence(
            current_binding.persona,
            "operator_transition_started",
            {
                "correlation_id": correlation_id,
                "destination": _transition_destination(target_binding),
                "source_runtime": source_runtime,
                "source_transition_id": source_transition_id,
                **_preserved_policy_state(source_state),
            },
            state_root=state_root,
        )
    else:
        source_transition_id = str(state.get("source_transition_id", ""))

    replaced = bool(state.get("runtime_swapped"))
    replacement: RuntimeHandle | None = None
    try:
        if communications_managed and not state.get("receiver_pre_done"):
            pre = _run_integration_handoff(target_binding, direction, "pre", backend=backend, reason=trigger)
            state["receiver_pre_receipt"] = dataclasses.asdict(pre)
            state["receiver_pre_done"] = True
            _atomic_json(_transition_path(current_binding.persona, state_root), state)
        if not state.get("runtime_swapped"):
            replacement = _replace_runtime(
                target_binding,
                state_root=state_root,
                on_started=_operator_runtime_started_callback(
                    state=state,
                    state_root=state_root,
                    target_binding=target_binding,
                ),
                **_context_kwargs(context),
            )
            replaced = True
            state["runtime_swapped"] = True
            _mark_runtime_mutation_resolved(state)
            _atomic_json(_transition_path(current_binding.persona, state_root), state)
        else:
            replacement = recorded_runtime_handle(target_binding, state_root=state_root)
        readiness = harness_integration.integration_for(target_binding.launch.harness).prove_runtime_readiness(
            target_binding,
            replacement,
            state_root=state_root,
            context=context,
        )
        if not readiness.ready:
            raise RuntimeError(f"target runtime readiness was not proven: {dict(readiness.evidence)}")
        state["runtime_readiness"] = dict(readiness.evidence)
        if (
            communications_managed
            and state.get("receiver_pre_done")
            and state.get("runtime_swapped")
            and not state.get("receiver_post_done")
        ):
            _prove_receiver(direction_source, "absent", backend=backend)
        if communications_managed and not state.get("receiver_post_done"):
            post = _run_integration_handoff(target_binding, direction, "post", backend=backend, reason=trigger)
            state["receiver_post_receipt"] = dataclasses.asdict(post)
            state["receiver_post_done"] = True
        state.update(
            {
                "phase": "awaiting-successor-read",
                "runtime_swapped": True,
                "receiver_transferred": communications_managed,
            }
        )
        evidence = _write_operator_handoff(
            current_binding.persona,
            transition_id,
            state,
            state_root=state_root,
        )
        state["operator_handoff_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
        contract = harness_integration.integration_for(target_binding.launch.harness).successor_evidence_contract(
            current_binding.persona,
            transition_id,
            evidence,
        )
        state.update(
            {
                "chronicle_evidence": str(evidence),
                "expected_successor_read_evidence": contract.expected,
                "successor_read_target": contract.target,
                "successor_evidence_kind": contract.evidence_kind,
            }
        )
        _atomic_json(_transition_path(current_binding.persona, state_root), state)
        _prompt_successor(replacement, evidence, persona=current_binding.persona, state_root=state_root, **_context_kwargs(context))
        raise RuntimeTransitionPending("successor has not acknowledged operator runtime transfer yet")
    except RuntimeTransitionPending:
        raise
    except Exception as exc:
        compensation: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            reverse_direction = _reverse_receiver_direction(current_binding, target_binding)
            runtime_may_be_mutated = (
                replaced
                or _replacement_error_pane_may_be_mutated(exc)
                or _unresolved_runtime_mutation(state)
            )
            if runtime_may_be_mutated:
                if communications_managed:
                    compensation["target_pre"] = dataclasses.asdict(
                        _run_integration_handoff(target_binding, reverse_direction, "pre", backend=backend, reason=f"{trigger}:partial")
                    )
                _replace_runtime(current_binding, state_root=state_root, **_context_kwargs(context))
                if communications_managed:
                    compensation["source_post"] = dataclasses.asdict(
                        _run_integration_handoff(current_binding, reverse_direction, "post", backend=backend, reason=f"{trigger}:rollback")
                    )
                state["runtime_swapped"] = False
                _mark_runtime_mutation_compensated(state)
            elif communications_managed and state.get("receiver_pre_done"):
                compensation["source_post"] = dataclasses.asdict(
                    _run_integration_handoff(current_binding, reverse_direction, "post", backend=backend, reason=f"{trigger}:rollback")
                )
            state["phase"] = "compensated"
            state["compensation"] = compensation
            _atomic_json(_transition_path(current_binding.persona, state_root), state)
        except Exception as rollback_exc:
            state["phase"] = "compensation-failed"
            state["compensation_error"] = f"{type(rollback_exc).__name__}: {rollback_exc}"
            mutation = state.get("runtime_mutation")
            if isinstance(mutation, Mapping):
                updated = dict(mutation)
                updated["status"] = "compensation-failed"
                state["runtime_mutation"] = updated
            _atomic_json(_transition_path(current_binding.persona, state_root), state)
        raise


def _ack_read_command(persona: str, state_root: Path, evidence_receipt: str) -> str:
    """The shell command the successor runs to record its read receipt (T-4471).

    Module form from the repo root, the same way the controller unit runs this file:
    ``from tools import ...`` needs the repo root on sys.path, and a bare-script run
    puts tools/ there instead, so ``python3 <repo>/tools/runtime_controller.py`` dies
    on "No module named 'tools'" before argparse, from any cwd.
    """
    repo_root = Path(__file__).resolve().parent.parent
    return (
        f"cd {shlex.quote(str(repo_root))} && python3 -m tools.runtime_controller "
        f"ack-read --persona {shlex.quote(persona)} "
        f"--state-root {shlex.quote(str(state_root))} "
        f"--evidence {shlex.quote(evidence_receipt)}"
    )


def _successor_evidence_copy_path(persona: str, state_root: Path | None) -> Path:
    """One bounded active-transition copy; a later transition atomically replaces it."""
    return _persona_state(persona, state_root) / "successor-evidence.md"


def _successor_evidence_payload(evidence: Path, nonce: str) -> bytes:
    payload = evidence.read_bytes()
    if payload and not payload.endswith(b"\n"):
        payload += b"\n"
    return payload + (nonce + "\n").encode("utf-8")


def _publish_successor_evidence_copy(evidence: Path, destination: Path, nonce: str) -> str:
    payload = _successor_evidence_payload(evidence, nonce)
    _atomic_bytes(destination, payload)
    destination.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


def _prompt_successor(
    handle: RuntimeHandle,
    evidence: Path,
    *,
    persona: str,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> None:
    state_arg = _state_root(state_root)
    state = transition_state(persona, state_root=state_root)
    evidence_receipt = state.get("expected_successor_read_evidence")
    if not isinstance(evidence_receipt, str) or not evidence_receipt:
        evidence_receipt = f"hook:runtime-transfer:{evidence.name}"
    if state:
        # T-5123: publish a private, bounded successor copy before atomically
        # publishing the path and nonce together in transition state. A retry
        # reuses that pair; only an interrupted first write can leave the one
        # harmless bounded copy without state authority.
        destination = _successor_evidence_copy_path(persona, state_root)
        recorded_path = state.get("successor_evidence_path")
        recorded_nonce = state.get("successor_read_nonce")
        recorded_sha256 = state.get("successor_evidence_sha256")
        if recorded_path is None and recorded_nonce is None and recorded_sha256 is None:
            nonce = SUCCESSOR_READ_NONCE_PREFIX + secrets.token_hex(32)
            evidence_sha256 = _publish_successor_evidence_copy(evidence, destination, nonce)
        elif (
            recorded_path == str(destination)
            and _valid_successor_read_nonce(recorded_nonce)
            and isinstance(recorded_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", recorded_sha256) is not None
        ):
            nonce = recorded_nonce
            evidence_sha256 = recorded_sha256
            if not _successor_copy_is_valid(destination, nonce, evidence_sha256):
                repaired_sha256 = _publish_successor_evidence_copy(evidence, destination, nonce)
                if repaired_sha256 != evidence_sha256:
                    raise RuntimeError("successor evidence source changed after publication")
        else:
            raise RuntimeError("successor evidence proof state is malformed")
        state.update(
            {
                "successor_prompted_at": _iso(_utc_now()),
                "successor_evidence_path": str(destination),
                "successor_read_nonce": nonce,
                "successor_evidence_sha256": evidence_sha256,
            }
        )
        _atomic_json(_transition_path(persona, state_root), state)
        prompted_evidence = destination
    else:
        prompted_evidence = evidence
    submit_to_runtime(
        handle,
        ExecutionRequest(
            prompt=(
                "Runtime transfer: read the continuity evidence at "
                f"{prompted_evidence}. After reading it, record the structural receipt "
                "with this exact command before retrying the pending mailbox "
                "record:\n"
                f"{_ack_read_command(persona, state_arg, evidence_receipt)}"
            ),
            cwd=Path.cwd(),
        ),
        persona=persona,
        state_root=state_root,
        message_id=(
            str(state["message_id"]) if isinstance(state.get("message_id"), str) else None
        ),
        correlation_id=(
            str(state["correlation_id"])
            if isinstance(state.get("correlation_id"), str)
            else None
        ),
        **_context_kwargs(context),
        # T-4362: the successor has only just booted (same hazard as the probe).
        render_wait_seconds=FRESH_RUNTIME_SUBMIT_RENDER_WAIT_SECONDS,
    )
    _append_controller_evidence(
        persona,
        "successor_prompted",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": handle.profile.harness,
            "incoming_profile": handle.profile.profile,
            "chronicle_evidence": str(evidence),
            "expected_successor_read_evidence": evidence_receipt,
        },
        state_root=state_root,
    )


def _abandon_transition(
    persona: str,
    target: "PersonaBinding",
    *,
    trigger: str,
    evidence: Mapping[str, Any],
    state_root: Path | None,
    reason: str = "pre-replace-usability-gate",
) -> None:
    """Terminal phase ``abandoned`` (T-4862): no replace happened, the outgoing
    runtime is untouched. Every phase reader treats it as no-transition-active
    (they all test for preparing/awaiting-successor-read or completed).

    ``reason`` (T-5021): ``operator-hold`` when the target is held; the default
    keeps the T-4862 usability-gate record and alert byte-identical."""
    from tools import subscription_usability

    state = transition_state(persona, state_root=state_root)
    abandon_evidence = {str(k): str(v) for k, v in dict(evidence).items()}
    restored = state.get("prior_fallback_record") if trigger == "quota-reset" else None
    if isinstance(restored, dict) and restored.get("phase") == "completed" and restored.get("fallback_active") is True:
        # Return swap abandoned: the seat is STILL on the fallback. Put the
        # completed/fallback-active record back (return path stays reachable,
        # re-probed at the next due reprobe_at) and only annotate the abandon.
        state = dict(restored)
        state.update(
            {
                "last_abandon_at": _iso(_utc_now()),
                "last_abandon_reason": reason,
                "last_abandon_evidence": abandon_evidence,
            }
        )
        state.pop("prior_fallback_record", None)
    else:
        state.update(
            {
                "phase": "abandoned",
                "abandoned_at": _iso(_utc_now()),
                "abandon_reason": reason,
                "abandon_evidence": abandon_evidence,
                "runtime_swapped": False,
            }
        )
    _atomic_json(_transition_path(persona, state_root), state)
    target_ref = f"{target.launch.harness}/{target.launch.profile}"
    _append_controller_evidence(
        persona,
        "planned_swap_abandoned",
        {"trigger": trigger, "target": target_ref, "evidence": abandon_evidence,
         "fallback_record_restored": state.get("phase") == "completed"},
        state_root=state_root,
    )
    _route_cooldown_gated_alert(
        persona,
        f"runtime-controller {persona}: planned {trigger} swap to {target_ref} ABANDONED -- the target "
        + (
            f"is operator-held ({abandon_evidence}). The seat stays on its current runtime."
            if reason == "operator-hold"
            else f"was not usable at replace time ({abandon_evidence}). The seat stays on its current runtime."
        ),
        f"controller-usability ({persona})",
        stamp_name=subscription_usability.ABANDON_ALERT_STAMP,
        state_root=state_root,
        now=None,
    )
    raise RuntimeTransitionAbandoned(
        f"{persona} planned swap to {target_ref} abandoned: "
        + ("target is operator-held" if reason == "operator-hold" else "target not usable")
    )


def planned_swap(
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str | None,
    drain_id: str | None = None,
    handoff_path: Path | None = None,
    primary_resets_at: str | None = None,
    quota_evidence: dict[str, str] | None = None,
    trigger: Literal["planned-quota", "quota-reset"] = "planned-quota",
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
    pre_replace_gate: Callable[["PersonaBinding"], tuple[bool, Mapping[str, Any]]] | None = None,
) -> SwapReceipt:
    """Perform or resume a cooperative swap; finish only after read acknowledgement.

    ``pre_replace_gate`` (T-4862): called with the target immediately before the
    runtime is replaced. ``(False, evidence)`` -- or a raised exception --
    ABANDONS the transition (phase ``abandoned``, outgoing runtime untouched)."""

    persona = target.persona
    state = transition_state(persona, state_root=state_root)
    if (
        state.get("phase") == "completed"
        and state.get("successor_read_ack")
        and state.get("trigger") == trigger
        and state.get("outgoing_harness") == current.profile.harness
        and state.get("outgoing_profile") == current.profile.profile
        and state.get("incoming_harness") == target.launch.harness
        and state.get("incoming_profile") == target.launch.profile
    ):
        evidence = Path(str(state["chronicle_evidence"]))
        return SwapReceipt(
            trigger=trigger,
            outgoing_profile=str(state["outgoing_profile"]),
            incoming_profile=str(state["incoming_profile"]),
            handoff_kind="model-authored",
            chronicle_evidence=evidence,
            completed_at=datetime.fromisoformat(
                str(state["completed_at"]).replace("Z", "+00:00")
            ),
        )

    # T-4862 B1: _begin_transition overwrites a completed/fallback-active record
    # with a fresh "preparing" payload. Keep it so a pre-replace abandon of a
    # quota-reset (return) swap can restore it and not strand the seat.
    prior_fallback_record: dict[str, Any] | None = None
    if trigger == "quota-reset":
        before = transition_state(persona, state_root=state_root)
        if before.get("phase") == "completed" and before.get("fallback_active") is True:
            prior_fallback_record = dict(before)
    _begin_transition(
        persona,
        trigger=trigger,
        outgoing_profile=current.profile.profile,
        incoming_profile=target.launch.profile,
        outgoing_harness=current.profile.harness,
        incoming_harness=target.launch.harness,
        drain_id=drain_id,
        state_root=state_root,
    )
    state = transition_state(persona, state_root=state_root)
    if prior_fallback_record is not None and "prior_fallback_record" not in state:
        state["prior_fallback_record"] = prior_fallback_record  # persisted below
    expected_transition = {
        "trigger": trigger,
        "outgoing_harness": current.profile.harness,
        "outgoing_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
    }
    mismatches = {
        key: (state.get(key), expected)
        for key, expected in expected_transition.items()
        if state.get(key) != expected
    }
    if mismatches:
        raise RuntimeTransitionPending(
            "another runtime transition is already active: "
            + ", ".join(
                f"{key}={actual!r} (requested {expected!r})"
                for key, (actual, expected) in sorted(mismatches.items())
            )
        )
    if primary_resets_at:
        state["primary_resets_at"] = primary_resets_at
    if quota_evidence:
        state["quota_evidence"] = dict(quota_evidence)
    _atomic_json(_transition_path(persona, state_root), state)
    source = handoff_path or target.account_home / "HANDOFF.md"
    if not state.get("handoff_requested"):
        baseline_text = ""
        baseline_mtime_ns: int | None = None
        try:
            baseline_text = source.read_text(encoding="utf-8")
            baseline_mtime_ns = source.stat().st_mtime_ns
        except OSError:
            pass
        submit_to_runtime(
            current,
            ExecutionRequest(
                prompt=(
                    "Prepare a concise runtime-transfer handoff at "
                    f"{source}. Do not stop until the handoff is durably written."
                ),
                cwd=target.account_home,
            ),
            persona=persona,
            state_root=state_root,
            message_id=message_id,
            **_context_kwargs(context),
        )
        state["handoff_requested"] = True
        state["handoff_baseline_sha256"] = hashlib.sha256(
            baseline_text.encode("utf-8")
        ).hexdigest()
        state["handoff_baseline_mtime_ns"] = baseline_mtime_ns
        _atomic_json(_transition_path(persona, state_root), state)
        raise RuntimeTransitionPending("fresh model-authored handoff was requested")
    try:
        handoff_text = source.read_text(encoding="utf-8")
        handoff_mtime_ns = source.stat().st_mtime_ns
    except OSError as exc:
        raise RuntimeTransitionPending("verified model-authored handoff is unavailable") from exc
    if not handoff_text.strip():
        raise RuntimeTransitionPending("verified model-authored handoff is empty")
    handoff_sha256 = hashlib.sha256(handoff_text.encode("utf-8")).hexdigest()
    if (
        handoff_sha256 == state.get("handoff_baseline_sha256")
        and handoff_mtime_ns == state.get("handoff_baseline_mtime_ns")
    ):
        raise RuntimeTransitionPending("model-authored handoff has not changed since request")
    stamp = _iso(_utc_now())
    archive = chronicle.archive_required(
        persona,
        handoff_text,
        jsonl_uuid=message_id or "planned-runtime-swap",
        reason="planned-runtime-swap",
        ts=stamp,
        root=_chronicle_root(),
    )
    if _runtime_ref(target) in load_holds(persona, state_root=state_root):
        # T-5021: an operator hold set after this transition began (the handoff was
        # already requested) must still stop the replace, for both triggers and
        # with or without a usability gate. No replace, outgoing runtime untouched.
        _abandon_transition(
            persona,
            target,
            trigger=trigger,
            evidence={"runtime": _runtime_ref(target), "hold": "operator"},
            state_root=state_root,
            reason="operator-hold",
        )
    if pre_replace_gate is not None:
        try:
            gate_ok, gate_evidence = pre_replace_gate(target)
        except Exception as exc:  # noqa: BLE001 -- fail closed: an unprovable target is not swapped to
            gate_ok, gate_evidence = False, {"gate_error": f"{type(exc).__name__}: {exc}"[:300]}
        if not gate_ok:
            _abandon_transition(
                persona, target, trigger=trigger, evidence=gate_evidence, state_root=state_root
            )
    replacement = _replace_runtime_with_receiver_handoff(
        current,
        target,
        state_root=state_root,
        reason=trigger,
        **_context_kwargs(context),
    )
    state = transition_state(persona, state_root=state_root)
    state.update(
        {
            "phase": "awaiting-successor-read",
            "handoff_kind": "model-authored",
            "chronicle_evidence": str(archive),
            "runtime_swapped": True,
            "fallback_active": trigger == "planned-quota",
            "message_id": message_id,
        }
    )
    _record_successor_target(persona, state, archive, state_root=state_root)
    _append_controller_evidence(
        persona,
        "runtime_replaced",
        {
            "trigger": trigger,
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "chronicle_evidence": str(archive),
        },
        state_root=state_root,
    )
    _prompt_successor(
        replacement,
        archive,
        persona=persona,
        state_root=state_root,
        **_context_kwargs(context),
    )
    raise RuntimeTransitionPending("successor has not acknowledged the handoff yet")


def forced_swap(
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    error: ProviderQuotaExhausted,
    message_id: str,
    correlation_id: str,
    drain_id: str | None = None,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> SwapReceipt:
    """Fail closed on authoritative cap and resume only after factual read ack."""

    persona = target.persona
    state = transition_state(persona, state_root=state_root)
    if _completed_forced_transition_matches(
        state,
        current,
        target,
        message_id=message_id,
        correlation_id=correlation_id,
    ):
        evidence = Path(str(state["chronicle_evidence"]))
        _append_controller_evidence(
            persona,
            "completed_forced_transition_reused",
            {
                "outgoing_harness": current.profile.harness,
                "outgoing_profile": current.profile.profile,
                "incoming_harness": target.launch.harness,
                "incoming_profile": target.launch.profile,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "chronicle_evidence": str(evidence),
            },
            state_root=state_root,
        )
        return SwapReceipt(
            trigger="forced-cap",
            outgoing_profile=str(state["outgoing_profile"]),
            incoming_profile=str(state["incoming_profile"]),
            handoff_kind="forced-factual",
            chronicle_evidence=evidence,
            completed_at=datetime.fromisoformat(
                str(state["completed_at"]).replace("Z", "+00:00")
            ),
        )

    state = _begin_transition(
        persona,
        trigger="forced-cap",
        outgoing_profile=current.profile.profile,
        incoming_profile=target.launch.profile,
        outgoing_harness=current.profile.harness,
        incoming_harness=target.launch.harness,
        drain_id=drain_id,
        state_root=state_root,
    )
    expected_transition = {
        "trigger": "forced-cap",
        "outgoing_harness": current.profile.harness,
        "outgoing_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
    }
    mismatches = {
        key: (state.get(key), expected)
        for key, expected in expected_transition.items()
        if state.get(key) != expected
    }
    if mismatches:
        raise RuntimeTransitionPending(
            "another runtime transition is already active: "
            + ", ".join(
                f"{key}={actual!r} (requested {expected!r})"
                for key, (actual, expected) in sorted(mismatches.items())
            )
        )
    state["message_id"] = message_id
    state["correlation_id"] = correlation_id
    state["breaker_open"] = True
    state["capped_profile"] = current.profile.profile
    if not state.get("primary_resets_at"):
        # A forced cap has no provider-observed reset window to record (the
        # cap already fired) -- unlike planned_swap's caller-supplied value,
        # so this is computed from the primary harness's weekly schedule.
        # First-write-wins: a retry of this same in-flight transition must
        # not redrift the recorded reset against a later "now".
        state["primary_resets_at"] = _iso(
            cap_reset_ceiling(current.profile.harness, now=now or _utc_now())
        )
    _atomic_json(_transition_path(persona, state_root), state)
    _append_controller_evidence(
        persona,
        "breaker_opened",
        {
            "trigger": "forced-cap",
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "capped_profile": current.profile.profile,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "barred_drain_id": drain_id,
        },
        state_root=state_root,
    )

    try:
        evidence = _existing_chronicle(
            persona,
            expected_session_id=current.session_id,
        )
        model_authored_handoff = evidence is not None
        stamp = _iso(_utc_now())
        safe_provider_error = chronicle.scrub_credentials(error.provider_error)[:1000]
        if evidence is None:
            factual = (
                "# Forced runtime transfer\n\n"
                "Model-authored handoff unavailable because the provider returned "
                "an authoritative quota limit.\n\n"
                f"- persona: {persona}\n"
                f"- previous profile: {current.profile.profile}\n"
                f"- previous session: {current.session_id or 'unknown'}\n"
                f"- message id: {message_id}\n"
                f"- correlation id: {correlation_id}\n"
                f"- provider error: {safe_provider_error}\n"
            )
            evidence = chronicle.archive_required(
                persona,
                factual,
                jsonl_uuid=current.session_id or message_id,
                reason="forced-provider-cap",
                ts=stamp,
                root=_chronicle_root(),
            )
        receipt_path = _persona_state(persona, state_root) / "forced-failover.json"
        receipt = {
            "schema": "pantheon-forced-failover/v1",
            "persona": persona,
            "transition_id": state.get("transition_id"),
            "previous_harness": current.profile.harness,
            "previous_profile": current.profile.profile,
            "previous_session_id": current.session_id,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "chronicle_entry": str(evidence),
            "message_id": message_id,
            "correlation_id": correlation_id,
            "ticket_or_step_id": None,
            "repository_evidence": {},
            "last_lifecycle_event": _read_json(
                _persona_state(persona, state_root) / "runtime.json"
            ).get("last_structural_event"),
            "provider_error": safe_provider_error,
            "model_authored_handoff": model_authored_handoff,
            "created_at": stamp,
        }
        _strict_receipt(receipt_path, receipt)
        _append_forced_receipt_history(persona, receipt, state_root=state_root)
        _append_controller_evidence(
            persona,
            "forced_evidence_durable",
            {
                "outgoing_harness": current.profile.harness,
                "outgoing_profile": current.profile.profile,
                "incoming_harness": target.launch.harness,
                "incoming_profile": target.launch.profile,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "forced_receipt": str(receipt_path),
                "chronicle_evidence": str(evidence),
                "model_authored_handoff": model_authored_handoff,
            },
            state_root=state_root,
        )
    except Exception as exc:
        state = transition_state(persona, state_root=state_root)
        state["phase"] = "durability-failed"
        state["breaker_open"] = True
        state["durability_error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(_transition_path(persona, state_root), state)
        raise ProviderQuotaExhausted(
            "forced failover evidence is not durable; fallback remains unused",
            profile=current.profile.profile,
            provider_error=error.provider_error,
        ) from exc

    replacement = _replace_runtime_with_receiver_handoff(
        current,
        target,
        state_root=state_root,
        reason="forced-cap",
    )
    state = transition_state(persona, state_root=state_root)
    state.update(
        {
            "phase": "awaiting-successor-read",
            "handoff_kind": "forced-factual",
            "chronicle_evidence": str(evidence),
            "runtime_swapped": True,
            "fallback_active": True,
            "forced_receipt": str(receipt_path),
            "message_id": message_id,
            "correlation_id": correlation_id,
        }
    )
    _record_successor_target(persona, state, evidence, state_root=state_root)
    _append_controller_evidence(
        persona,
        "runtime_replaced",
        {
            "trigger": "forced-cap",
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "forced_receipt": str(receipt_path),
            "chronicle_evidence": str(evidence),
        },
        state_root=state_root,
    )
    _prompt_successor(
        replacement,
        evidence,
        persona=persona,
        state_root=state_root,
    )
    raise RuntimeTransitionPending("successor has not acknowledged chronicle evidence yet")



# ============================================================================
# T-2966 (T-2953 Phase 2 BT1) -- stateless restart-fixable controller
#
# Everything below implements notes/2026-08-18-t2953-phase1-spec.md's
# "Durable-state layout" + "Controller invocation flow" sections. The
# invariant this whole block exists to satisfy is ORG-068 SS85-88:
# externally legible, restart-fixable, stateless across sessions. Every
# helper re-reads its input from the durable substrate (files under
# ~/.local/state/pantheon/personas/<persona>/) or takes it as a parameter;
# nothing here retains state in module-level or closure variables across
# separate `controller_main` invocations.
#
# KEEP-layer reuse (T-2966 DECISION, ORG-068 SS88 justifies-via): starting a
# runtime reuses the low-level `_start_runtime` / `_verify_started_runtime` /
# `_verify_runtime_input_ready` primitives directly rather than the
# `_replace_runtime` / `ensure_runtime` wrappers -- those wrappers carry
# operator-transition-state (`transition.json`, compensation receipts)
# designed for human-supervised swaps. Routing the automatic MainPID loop
# through them would either fake transition state or reintroduce
# cross-invocation coupling, both violating the stateless invariant. The
# wrappers stay exactly as they are, serving the orthogonal operator/
# quota-swap flow (persona_bringup.py, via the existing consume-timer).
# ============================================================================

RUNTIME_FAILURE_CLASSES = (
    "subscription_cap",
    "process_death",
    "readiness_timeout",
    "operational_failure",
    "runtime_blocked",
)
SUSTAINED_ALIVE_SECONDS = 300.0
# T-5483: an operator `harness restart` marker is honoured only this long after it
# was written (it is written immediately before the kill; the controller observes
# the death within one poll). Bounded so an orphaned marker cannot mask a later
# genuine death.
OPERATOR_RESTART_MARKER_MAX_AGE_SECONDS = 180.0
MAX_SLEEP_CAP_SECONDS = 3600.0
# T-4101: when the controller finds the consume/wake path already holds the
# persona_operation_lock (it is actively launching this persona's runtime), the
# controller DEFERS its own start for this many seconds, then re-loops (re-reads
# durable state fresh, per SS88) and re-checks. Deferring rather than blocking on
# the lock is deliberate: a blocking acquire would sit on the 6h lease TTL and
# wedge this Restart=always daemon; a stomping respawn-pane -k would kill
# consume's in-flight launch (the exact specialist 2026-09-25 race). The backoff is
# small enough to re-check promptly once consume's ~90s launch/probe completes,
# and non-zero so the defer loop can never tight-loop-crash-spin.
CONTROLLER_CONTENTION_DEFER_SECONDS = 5.0
# T-5021: when every candidate is operator-held, controller_main re-reads the
# holds this often (in-process) instead of exiting into a systemd restart loop.
ALL_HELD_RECHECK_SECONDS = 60.0
# process_death / readiness_timeout / operational_failure share this backoff
# ladder (spec's failure-class table): 30s, 5min, 30min, 2h, then holds at 2h.
_TRANSIENT_FAILURE_BACKOFF_SECONDS = (30.0, 300.0, 1800.0, 7200.0)

# ----------------------------------------------------------------------------
# T-4037: mid-session subscription-cap detection. A runtime that started
# healthy and never died reads as healthy forever under the block/record loop
# above -- the cap classifiers in harness_adapter/claude.py only run at start
# (a baseline-diff requirement). Everything below adds a periodic PANE-TEXT
# check while a runtime is blocked-alive, feeding two independent actions:
# (1) publish observed state for a separate reaper (Recovery's tool, T-4024) to
#     Esc a stuck modal once the window has rolled; and
# (2) swap to a viable fallback runtime. T-5446: always on for every seat (the
#     per-role opt-in is retired). A comms persona (receiver_handoff_required) is
#     swapped only when its codex receiver is READY (`_comms_receiver_ready`):
#     the swap just SIGTERMs the pane and the next cycle's start path performs
#     the full receiver handoff (T-4184); a not-ready receiver refuses the swap
#     with an evidence event and a cooldown-gated alert instead.
#
# T-4037 REBOUND round 2 Finding 3: `_observe_and_publish_mid_session_cap`
# additionally DEBOUNCES the transition into `modal_present: true` -- a single
# classifying sample is only ever a CANDIDATE; `modal_present: true` (and the
# swap action it gates) requires two CONSECUTIVE samples, ~60s apart (this
# module's existing cap-check cadence), whose
# `ClaudeMidSessionCapModal.render_span_sha256` match exactly. Real captures
# always carry a live modal as the pane's static final content across polls;
# a false-positive text quote is, structurally, essentially never reproduced
# byte-for-byte at the SAME position across two independent 60s-apart
# captures even if it somehow passed harness_adapter.claude's single-sample
# checks once. Extra latency: up to one cap-check cycle (~60s) added to
# publish/swap on a genuine cap, against a multi-hour cap window -- acceptable
# (see this module's DECISIONS). Once CONFIRMED, an episode stays
# `modal_present: true` on later polls without re-debouncing on every sample
# (see `_observe_and_publish_mid_session_cap`'s docstring) -- debounce gates
# only the risky ENTRY transition, not continued observation of an
# already-confirmed state.
# ----------------------------------------------------------------------------
MID_SESSION_CAP_POLL_INTERVAL_SECONDS = 60.0
# Cadence for the pane-text cap check, independent of the 5s liveness poll
# above -- capture-pane is cheap but not free (a subprocess round-trip to the
# local tmux server), so it runs far less often than the liveness check.
MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS = 4 * 3600.0
# Fallback cooldown length when a cap modal is detected but no reset time
# parses from the pane. Grounded in: (a) tools/acp_bridge.py's own
# _MAX_CAP_DEFER_SECS = 6*3600.0 as the precedent upper bound for "cap-reset
# defer, never longer than this even on parse"; (b) Claude's known ~5h
# rolling usage-window magnitude as the domain basis for the guess; (c) the
# guess only needs to be roughly right -- a wrong (too-short) guess costs one
# extra reselect cycle, never a repeat of a multi-hour wedge. UPDATED (T-4047):
# this "one extra cycle" argument used to rest on the start-of-session
# classifier (classify_claude_post_start_subscription_cap) staying
# UNCHANGED and re-applying its own long next_weekly_reset cooldown as a
# backstop -- that classifier's start-up cooldown is now ALSO the parsed/
# conservative value for a non-weekly cap (see
# _post_start_cap_cooldown_override), not next_weekly_reset. The backstop
# is no longer a separate week-long catch: a too-short mid-session guess
# self-corrects on the very next attempt's own fresh pane read + parse,
# converging rather than being caught by a distinct longer cooldown. This is
# still safe because the SAME 4h constant is reused there (not a new,
# possibly-shorter value), and both call sites clamp to
# next_weekly_reset(harness, now) as an upper bound, so neither guess can
# ever exceed the pre-T-4047 worst case -- only ever shorten it.
MID_SESSION_CAP_TERMINATE_CONFIRM_TIMEOUT_SECONDS = 10.0
# Bounded wait for _runtime_alive to go False after a SIGTERM swap attempt,
# polled at the caller's own poll_interval_seconds (no separate cadence
# constant needed). Mirrors this file's existing RUNTIME_START_TIMEOUT_SECONDS
# = 8.0 (the precedent for "how long to wait for a process/pane state
# transition" in this module) -- slightly longer since a graceful SIGTERM
# exit/cleanup path can reasonably take a bit more than a fresh process
# simply appearing. Never escalates past this: this landing does not send
# SIGKILL (HO amendment A, T-4037) -- a pane still alive at the deadline
# fails safe (no swap outcome recorded) and is retried on the next cap-check
# cycle.
MID_SESSION_CAP_SCHEMA = "pantheon-persona-mid-session-cap/v1"


@dataclass(frozen=True)
class MidSessionCapObservation:
    """One pane-text observation of the mid-session-cap detector (T-4037).

    modal_present: whether the CURRENT episode is CONFIRMED present --
        requires two consecutive classifying samples with an identical
        `render_span_sha256` (T-4037 REBOUND round 2 Finding 3; see
        `_observe_and_publish_mid_session_cap`). A single classifying sample
        is a CANDIDATE ONLY and never sets this True.
    modal_kind: "session-limit" | "weekly-limit" | "opus-limit" |
        "monthly-spend-limit" when modal_present, else None.
    detected_at: first-observed-at for the CURRENT CONFIRMED episode -- this
        is the timestamp of the FIRST of the two confirming samples (i.e.
        "first sighting of the confirmed episode"), not the timestamp of the
        confirming (second) sample itself, and stays sticky across further
        consecutive present observations. Reset to a fresh value once the
        modal clears and later reappears (a new episode, needing its own
        fresh two-sample confirmation). None when modal_present is False.
    observed_at: the timestamp of THIS observation -- the freshness signal a
        consumer uses to tell a live detector loop from a stale/stopped one.
    reset_at: absolute UTC reset instant PARSED from the pane, or None -- this
        type is a raw observation and never carries a conservative-default
        value (that's the swap action's own ephemeral concept; see
        MidSessionCapSwapOutcome).
    reset_source: "parsed" when reset_at is set, else None.
    candidate_modal_kind: NON-AUTHORITATIVE diagnostic visibility only (T-4037
        REBOUND round 2 Finding 3) -- set to the classified modal_kind when
        THIS sample classified but has not yet been confirmed by a second,
        render-identical, consecutive sample; None once confirmed (folds into
        modal_kind/modal_present) or when this sample didn't classify at all.
        Consumers, INCLUDING Recovery's reaper, MUST NOT treat this as actionable
        -- the only action-trigger predicate is modal_present.
    """

    modal_present: bool
    modal_kind: str | None
    detected_at: datetime | None
    observed_at: datetime
    reset_at: datetime | None
    reset_source: str | None
    candidate_modal_kind: str | None = None


@dataclass(frozen=True)
class MidSessionCapSwapOutcome:
    """Returned by block_until_tmux_death when a mid-session cap swap fired.

    A non-None return from the block_until_death_fn seam tells controller_main
    to record failure_class="subscription_cap" with THIS cooldown (never the
    default next_weekly_reset), and to skip the sustained-alive success/
    process_death branch entirely -- the runtime was deliberately ended, not
    observed to have died on its own.
    """

    modal_kind: str
    cooldown_until: datetime
    cooldown_source: str  # "parsed" | "conservative-default"


# ----------------------------------------------------------------------------
# T-4046: fallback-return. Mirrors T-4037's SIGTERM/confirm-death/no-SIGKILL
# swap-action shape, but in the OPPOSITE direction: while running a
# LOWER-priority candidate (a fallback), periodically check whether a
# STRICTLY HIGHER-priority candidate (earlier in manifest order) has become
# viable again (its cooldown expired, not held) and, if so, gracefully end
# the current runtime so the next controller_main cycle's ordinary
# reselection picks the higher-priority one. T-5446: fleet policy, on for every
# seat (RuntimeControllerConfig.fallback_return_enabled defaults True; the
# per-role key is retired). Unlike the T-4037 swap, this has NO receiver-
# readiness gate (T-5451) -- failing back TO a
# comms-carrying primary RESTORES comms rather than losing it (see
# _fallback_return_eligible's docstring for the receiver-topology
# reasoning).
# ----------------------------------------------------------------------------
FALLBACK_RETURN_CHECK_INTERVAL_SECONDS = 60.0
# Cadence for re-deriving fallback-return eligibility while NOT yet armed.
# Symmetric with MID_SESSION_CAP_POLL_INTERVAL_SECONDS above -- eligibility
# is a couple of cheap JSON reads (via _select_viable_runtime), materially
# cheaper than a tmux capture-pane round-trip, so 60s is not a cost-driven
# choice the way it is for the pane check; it is chosen for architectural
# symmetry with this module's one existing decoupled-from-liveness cadence,
# not a distinct cost argument of its own.
FALLBACK_RETURN_MIN_DWELL_SECONDS = SUSTAINED_ALIVE_SECONDS
# Hysteresis floor (T-4046 anti-flap): a fallback-return is never even
# considered until the CURRENTLY RUNNING (lower-priority) runtime has been
# alive at least this long. Deliberately reuses SUSTAINED_ALIVE_SECONDS
# rather than a new constant -- it is already this module's own bar for
# "this attempt counts as a stable success" (see controller_main's success
# branch); a runtime that hasn't cleared that bar yet failing back would be
# indistinguishable from a flap. The cap-swap path's own cooldown (a real
# next-weekly-reset, or a parsed/conservative-default duration -- both far
# longer than this floor) is what actually prevents a failed-back primary
# from immediately re-triggering another swap; see
# _fallback_return_eligible's docstring for the non-fight argument against
# T-4037.
FALLBACK_RETURN_HANDOFF_RETRY_SECONDS = 600.0
# T-5069: after a fallback-return handoff request went unconfirmed (and no
# session-fresh HANDOFF exists), wait this long before asking again. Each
# attempt types a prompt into the live pane, so it must not be hammered; it
# also keeps the retries well inside the idle-wait horizon below.
FALLBACK_RETURN_IDLE_MAX_WAIT_SECONDS = 1800.0
# Bounded wait for an idle pane once a fallback-return has been triggered,
# before acting anyway. No existing precedent constant in this module bounds
# "how long can one interactive turn run" (RUNTIME_INPUT_READY_TIMEOUT_SECONDS
# etc. all bound STARTUP latency, a different concept) -- this is a fresh,
# empirically-chosen value, pending validation at the supervised first fire
# (T-4046 HO amendment B): long enough that a normal orchestrator/persona
# turn boundary is very likely to land inside it, short enough that a
# persistently-busy seat still fails back within roughly one order of
# magnitude of a single cap-check cycle group, rather than sitting wedged on
# the fallback indefinitely (which would silently reopen the T-4024 symptom
# this ticket exists to close). Two orders of magnitude below the
# multi-hour cap-window horizon this whole feature operates against, so it
# does not materially change aggregate failback latency even in the worst
# case.
@dataclass(frozen=True)
class FallbackReturnOutcome:
    """Returned by block_until_tmux_death when a fallback-return fired (T-4046).

    Ends a HEALTHY lower-priority runtime to return to a higher-priority one
    that has become viable again. Distinct from MidSessionCapSwapOutcome:
    that type tells controller_main to record a FAILURE (subscription_cap)
    on the ended runtime; this one is the opposite -- the ended runtime did
    nothing wrong (a planned failback is not its fault), so controller_main
    must record its own outcome as "success" (clearing any cooldown/
    consecutive-failure state), never a failure, regardless of how long the
    runtime had actually been alive before the failback fired.

    `target_ref` is `<harness>/<profile>` of the higher-priority runtime this
    fired for -- observability/logging only. controller_main's own fresh
    re-selection next cycle re-derives the actual pick from disk (SS88
    statelessness); nothing here is fed back into selection as an
    instruction.
    """

    target_ref: str


# How often block_until_tmux_death checks for a completed operator swap (T-4469).
# One transition.json read per check; the pane identity is read only when that
# file names a runtime other than the supervised one.
OPERATOR_SWAP_CHECK_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class OperatorSwapOutcome:
    """Returned by block_until_tmux_death when an OPERATOR transition replaced the
    supervised runtime in its pane (T-4469).

    An operator swap (proactive_swap_init --trigger operator) runs the new harness
    in the same tmux pane, so the harness-blind liveness check stayed true and the
    supervise loop kept evaluating the binding it STARTED. Fallback-return never
    armed, and cap detection read the pane as the old harness (the 2026-09-30 recovery
    drill). controller_main records this as a SUCCESS for the replaced runtime (a
    deliberate swap is not its fault) and returns, so Restart=always re-enters
    selection. That selection pins the operator's destination while it is live
    (_operator_pinned_runtime), so the respawn ATTACHES to it and supervises the
    right binding. `destination_ref` is observability only.
    """

    destination_ref: str


# ORG-106 L3b (T-5526): how often the supervise loop looks for a deep-sleep record, and the
# retry cadence of a drain step that must be repeated (terminate unconfirmed, receiver stuck).
SEAT_SLEEP_CHECK_INTERVAL_SECONDS = 10.0
SEAT_SLEEP_DRAIN_RETRY_SECONDS = 10.0


@dataclass(frozen=True)
class SeatSleepOutcome:
    """Returned by block_until_tmux_death when the DEEP-SLEEP drain (tools/seat_sleep_drain)
    ended the supervised session. controller_main records a SUCCESS (a drain is not the
    runtime's fault) and returns; the next process waits at the sleep gate."""

    drain_outcome: str


def _seat_sleep_session_alive(candidates: Sequence["PersonaBinding"], *, context: Any = None) -> bool:
    """Is any configured runtime's pane live? (Candidates share one tmux target.) A test seam."""
    return any(_runtime_alive(binding, context=context) for binding in candidates)


def build_seat_sleep_seams(
    persona: str,
    candidates: Sequence["PersonaBinding"],
    *,
    state_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    context: Any = None,
) -> Any:
    """ORG-106 L3b: the production seams for tools/seat_sleep_drain.run_drain. Shared by the
    controller (supervise loop and sleep gate) and the `harness sleep` verb's no-session fast
    path. The LIVE runtime is the candidate that active-runtime.json names, else the first."""
    from tools import seat_sleep, seat_sleep_drain

    cands = tuple(candidates)
    if not cands:
        raise RuntimeError(f"no runtime candidates for {persona}")
    try:
        named = read_active_runtime(persona, state_root=state_root).get("runtime") or ""
    except Exception:  # noqa: BLE001
        named = ""
    live = next((b for b in cands if _binding_runtime_ref(b) == named), cands[0])
    clock = now or _utc_now

    def submit(text: str) -> Any:
        handle = recorded_runtime_handle(live, state_root=state_root)
        return submit_to_runtime(
            handle, ExecutionRequest(prompt=text, cwd=Path(live.account_home)),
            persona=persona, state_root=state_root, context=context,
        )

    def stop_receiver() -> None:
        # The KEEP layer's own pre-step: stops the out-of-runtime (codex) Telegram receiver and
        # proves it absent, from the runtime.json record and a read-only probe (the pane is
        # already gone). Raises RuntimeOperationalFailure on failure.
        claude = next(
            (b for b in cands if receiver_handoff_required(b)
             and b.launch.harness != _OUT_OF_RUNTIME_RECEIVER_HARNESS), None)
        if claude is not None:
            _stop_prior_harness_receiver_before_start(claude, live_argv=None, state_root=state_root, probe=True)

    def clear_runtime_state() -> None:
        clear_active_runtime(persona, state_root=state_root)
        clear_mid_session_cap_state(persona, state_root=state_root)

    try:
        handoff_path: Path | None = handoff_first.binding_handoff_path(live)
    except Exception:  # noqa: BLE001
        handoff_path = None
    return seat_sleep_drain.Seams(
        persona=persona,
        root=seat_sleep.seat_root_for(_state_root(state_root)),
        runtime_ref=_binding_runtime_ref(live),
        handoff_path=handoff_path,
        alive=lambda: _seat_sleep_session_alive(cands, context=context),
        submit=submit,
        pane_idle=lambda: _pane_appears_idle(lambda: _capture_visible_runtime_pane(live.controller.tmux)),
        terminate=lambda: _terminate_runtime_pid(live, context=context),
        stop_receiver=stop_receiver,
        clear_runtime_state=clear_runtime_state,
        evidence=lambda event, payload: _best_effort_controller_evidence(
            persona, event, payload, state_root=state_root),
        alert=lambda msg, vocab: _route_cooldown_gated_alert(
            persona, msg, vocab, stamp_name="sleep-drain-alert.json", state_root=state_root, now=None),
        now=clock,
        sleep=sleep or time.sleep,
    )


def _seat_sleep_end_expected(
    persona: str, runtime_ref: str, started_at: datetime, current: datetime, state_root: Path | None,
) -> bool:
    """Death classification: was this session end the deep-sleep drain's? Never raises."""
    try:
        from tools import seat_sleep, seat_sleep_drain

        return seat_sleep_drain.sleep_end_expected(
            persona, seat_sleep.seat_root_for(_state_root(state_root)), runtime_ref,
            attempt_started_at=started_at, current=current)
    except Exception:  # noqa: BLE001
        return False


def _seat_sleep_check(
    persona: str,
    candidates: Sequence["PersonaBinding"],
    *,
    state_root: Path | None,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> "SeatSleepOutcome | None":
    """The supervise loop's periodic look at the seat-state record (ORG-106 L3b). Runs the
    drain when one is due; a SeatSleepOutcome only once the seat reached ``asleep``. Any other
    result (aborted, pending) keeps the loop supervising: a pending step is retried at the
    next check, and a session that died meanwhile is classified by controller_main."""
    from tools import harness_state as _hs
    from tools import seat_sleep, seat_sleep_drain

    try:
        root = seat_sleep.seat_root_for(_state_root(state_root))
        # One cheap record read per check; the tmux liveness probe only when a record exists.
        if seat_sleep_drain.load_sleeping(persona, root, now=now()) is None:
            return None
        if not seat_sleep_drain.needs_drain(
                persona, root, session_alive=_seat_sleep_session_alive(candidates), now=now()):
            return None
        seams = build_seat_sleep_seams(persona, candidates, state_root=state_root, now=now, sleep=sleep)
        if seat_sleep_drain.run_drain(seams) != seat_sleep_drain.ASLEEP:
            return None
        rec = _hs.load_seat_state(persona, root=root) or {}
        return SeatSleepOutcome(drain_outcome=str(rec.get("drain_outcome") or ""))
    except Exception:  # noqa: BLE001 - the supervise loop must never die on the sleep check
        return None


def _controller_persona_root(persona: str, *, state_root: Path | None = None) -> Path:
    """Root of T-2966's durable-state tree for `persona` (spec SS Durable-state
    layout). Distinct from `_persona_state` (`.../<persona>/runtime-controller`,
    the pre-existing KEEP-layer operator-swap state dir) -- the two trees are
    siblings under the persona directory, never overlapping paths."""
    return _state_root(state_root) / persona


def _runtime_slug(harness: str, profile: str) -> str:
    """`<harness>--<profile>` (double-dash survives the slash in
    ``claude/anthropic-code``) -- spec SS Durable-state layout, slug rule."""
    return f"{harness}--{profile}"


def _binding_runtime_slug(binding: "PersonaBinding") -> str:
    return _runtime_slug(binding.launch.harness, binding.launch.profile)


def _binding_runtime_ref(binding: "PersonaBinding") -> str:
    return f"{binding.launch.harness}/{binding.launch.profile}"


def _active_runtime_path(persona: str, *, state_root: Path | None = None) -> Path:
    return _controller_persona_root(persona, state_root=state_root) / "active-runtime.json"


def _last_attempt_path(persona: str, slug: str, *, state_root: Path | None = None) -> Path:
    return _controller_persona_root(persona, state_root=state_root) / "runtimes" / slug / "last-attempt.json"


def _holds_path(persona: str, *, state_root: Path | None = None) -> Path:
    return _controller_persona_root(persona, state_root=state_root) / "overrides" / "holds.json"


def _invocations_path(persona: str, *, state_root: Path | None = None, when: datetime | None = None) -> Path:
    day = (when or _utc_now()).strftime("%Y-%m-%d")
    return _controller_persona_root(persona, state_root=state_root) / "invocations" / f"{day}.jsonl"


def _parse_iso_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_active_runtime(
    persona: str,
    binding: "PersonaBinding",
    handle: RuntimeHandle,
    *,
    pid: int | None = None,
    state_root: Path | None = None,
) -> None:
    """Write the active-runtime.json cache (spec SS Durable-state layout).

    Cache/shortcut only -- NEVER authority for "is X running": that's
    tmux+pid observation (`_runtime_alive`). Written atomically via the
    pre-existing `_atomic_json` helper (KEEP, reused as directed)."""
    _atomic_json(
        _active_runtime_path(persona, state_root=state_root),
        {
            "schema": "pantheon-persona-active-runtime/v1",
            "runtime": _binding_runtime_ref(binding),
            "tmux_session": binding.controller.tmux.session,
            "pid": pid,
            "started_ts": _iso(handle.started_at),
            "started_by": "controller-selection",
            "note": "Cache only -- tmux existence + pid liveness is authoritative",
        },
    )


def _operator_restart_marker_path(persona: str, *, state_root: Path | None = None) -> Path:
    # Mirrors harness_state.operator_restart_path (pinned by a parity test).
    return _controller_persona_root(persona, state_root=state_root) / "overrides" / "operator-restart.json"


def _consume_operator_restart_marker(
    persona: str,
    selected_ref: str,
    *,
    attempt_started_at: datetime,
    current: datetime,
    state_root: Path | None = None,
) -> dict[str, Any] | None:
    """T-5483: True-ish (the marker doc) iff a fresh operator-restart marker names
    `selected_ref`, was written at/after this attempt's start, and is at most
    OPERATOR_RESTART_MARKER_MAX_AGE_SECONDS old. The marker is unlinked whenever one
    was examined (matched, stale, mismatched, malformed), so it can never suppress a
    later genuine death. Fail-open: any error means "no marker"."""
    path = _operator_restart_marker_path(persona, state_root=state_root)
    try:
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Unreadable (e.g. written by another uid) or malformed: say so, never raise.
            _best_effort_controller_evidence(
                persona, "operator_restart_marker_unreadable",
                {"error": f"{type(exc).__name__}: {exc}"[:300]}, state_root=state_root,
            )
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        try:
            path.unlink()
        except OSError as exc:
            _best_effort_controller_evidence(
                persona, "operator_restart_marker_unlink_failed",
                {"error": f"{type(exc).__name__}: {exc}"[:300]}, state_root=state_root,
            )
    except Exception:  # noqa: BLE001 -- the consume path must never raise
        return None
    ts =_parse_iso_or_none(doc.get("ts"))
    if ts is None or doc.get("runtime") != selected_ref:
        return None
    try:
        age = (current - ts).total_seconds()
        if ts < attempt_started_at or age < 0 or age > OPERATOR_RESTART_MARKER_MAX_AGE_SECONDS:
            return None
    except TypeError:  # naive vs aware
        return None
    return doc


def clear_active_runtime(persona: str, *, state_root: Path | None = None) -> None:
    """Clear the cache on observed tmux-death (spec SS Durable-state layout)."""
    try:
        _active_runtime_path(persona, state_root=state_root).unlink()
    except FileNotFoundError:
        pass


def read_active_runtime(persona: str, *, state_root: Path | None = None) -> dict[str, Any]:
    """Fast-introspection read of the cache (`harness status`'s use case).
    Empty dict if never written / already cleared -- never raises."""
    return _read_json(_active_runtime_path(persona, state_root=state_root))


def _mid_session_cap_state_path(persona: str, *, state_root: Path | None = None) -> Path:
    return _controller_persona_root(persona, state_root=state_root) / "mid-session-cap.json"


def read_mid_session_cap_state(persona: str, *, state_root: Path | None = None) -> dict[str, Any]:
    """Read the persisted `MidSessionCapObservation` for `persona`, or `{}` if
    never written / already cleared (T-4037).

    THIS IS A CROSS-ACCOUNT CONTRACT. Recovery's cap-modal reaper (a different
    persona's tool, T-4024) reads this file directly (`sudo -u <persona> cat
    .../mid-session-cap.json` or equivalent) -- it is not this persona's own
    controller talking to itself. Every field below is part of that contract;
    changing shape/meaning here is a breaking change for that consumer, not a
    local refactor.

    T-4047: `modal_present`'s frozen-pane false-positive residual is formally
    accepted-and-bounded, not structurally closed -- Recovery's reaper re-verify
    IS that bound's fifth, cross-account layer. See
    `tools.harness_adapter.claude.classify_claude_mid_session_cap`'s
    docstring for the full four-gate enumeration; not duplicated here.

    File location: `<state_root>/<persona>/mid-session-cap.json`, a sibling of
    `active-runtime.json` / `runtimes/<slug>/last-attempt.json` under the
    T-2966 stateless-controller tree (`_controller_persona_root`) -- NOT the
    older KEEP-layer `<persona>/runtime-controller/` dir (`transition.json`,
    operator-recovery archives). This state is written by the same stateless
    block/controller loop that owns the T-2966 tree, so it lives beside its
    siblings rather than beside the KEEP layer's operator-supervised-swap
    state, which this ticket's automatic loop never touches (T-4037 DECISION).

    Schema `pantheon-persona-mid-session-cap/v1` fields:
      schema (str)             -- literal `MID_SESSION_CAP_SCHEMA`; versioned,
                                   bump the suffix on any breaking shape change.
      runtime (str)             -- "<harness>/<profile>" of the runtime this
                                   observation is ABOUT (`_binding_runtime_ref`).
      modal_present (bool)      -- true iff the most recent pane read matched a
                                   known cap-modal pattern AND the debounce
                                   requirement (T-4037 REBOUND round 2 Finding
                                   3) has confirmed it: two CONSECUTIVE
                                   classifying samples with an identical
                                   render span. A single classifying sample
                                   is never sufficient -- see
                                   candidate_modal_kind below for that case.
      modal_kind (str | null)   -- "session-limit" | "weekly-limit" |
                                   "opus-limit" | "monthly-spend-limit" when
                                   modal_present, else null.
      detected_at (str | null)  -- ISO-8601 UTC; first-observed-at for the
                                   CURRENT CONFIRMED modal episode (the
                                   timestamp of the FIRST of the two
                                   confirming samples, not the confirming
                                   sample itself; sticky across further
                                   consecutive present reads). Null when
                                   modal_present is false.
      observed_at (str)         -- ISO-8601 UTC; timestamp of THIS write. A
                                   consumer's freshness signal: an old
                                   observed_at with the detector's poller
                                   otherwise expected to be running means
                                   treat the record as stale, not authoritative.
      reset_at (str | null)     -- ISO-8601 UTC; absolute reset instant PARSED
                                   from the pane, or null if the pane carried
                                   no parseable reset text. Never a
                                   conservative-default value -- that concept
                                   belongs only to the swap action's own
                                   ephemeral cooldown computation
                                   (MidSessionCapSwapOutcome), which is never
                                   written here; this file always reflects
                                   what was actually read off the pane.
      reset_source (str | null) -- "parsed" when reset_at is set, else null.
      candidate_modal_kind (str | null) -- T-4037 REBOUND round 2 Finding 3.
                                   NON-AUTHORITATIVE diagnostic field: set
                                   when THIS sample classified but has not yet
                                   been confirmed by a second, render-
                                   identical, consecutive sample. Consumers,
                                   INCLUDING Recovery's reaper, MUST NOT act on
                                   this field -- the only action-trigger
                                   predicate remains modal_present. Null
                                   whenever modal_present is true or the
                                   sample didn't classify at all.
      candidate_text_sha256 (str | null) -- T-4037 REBOUND round 2 Finding 3.
                                   INTERNAL debounce bookkeeping: sha256 of
                                   the pending (unconfirmed) candidate's
                                   render span (`ClaudeMidSessionCapModal.
                                   render_span_sha256`), compared against the
                                   NEXT sample's render span to decide
                                   confirm-vs-reset. Opaque to any consumer
                                   other than this module's own next read;
                                   never meaningful on its own. Null once
                                   confirmed or cleared.
      candidate_first_seen_at (str | null) -- T-4037 REBOUND round 2 Finding
                                   3. INTERNAL debounce bookkeeping: ISO-8601
                                   UTC timestamp of the pending candidate's
                                   first sample -- becomes `detected_at` if/
                                   when the candidate is confirmed. Null once
                                   confirmed or cleared.

    DERIVING "has the window rolled" (the reaper's own question, T-4024): this
    file deliberately does NOT store a `window_rolled` boolean -- a
    controller-computed flag would itself go stale between writes. Compute it
    at read time instead: `window_rolled = reset_at is not None and
    consumer_now >= reset_at`, OR simply `modal_present is False` on a FRESH
    (recent observed_at) read, which directly reflects the pane having
    cleared (e.g. after the reaper's own Esc, or the window rolling on its
    own) without needing reset_at at all.

    Fail-open is honored on WRITE, not on this read: an unreadable pane never
    produces a write (see `_observe_and_publish_mid_session_cap`), so this
    function returning a stale prior record on a bad detector cycle is the
    correct, honest behavior -- never a fabricated "cleared" state.
    """
    return _read_json(_mid_session_cap_state_path(persona, state_root=state_root))


def _write_mid_session_cap_state(
    persona: str,
    binding: "PersonaBinding",
    observation: MidSessionCapObservation,
    *,
    state_root: Path | None = None,
    candidate_text_sha256: str | None = None,
    candidate_first_seen_at: datetime | None = None,
) -> None:
    """Write `observation` plus the debounce bookkeeping pair (T-4037 REBOUND
    round 2 Finding 3): `candidate_text_sha256`/`candidate_first_seen_at` are
    NOT part of `MidSessionCapObservation` itself (that type models a
    CONFIRMED-or-not observation; the pending-candidate hash/timestamp is
    purely this module's own debounce bookkeeping between consecutive calls,
    read back via `read_mid_session_cap_state` in
    `_observe_and_publish_mid_session_cap`). Both default None -- every
    caller other than that function's own pending-candidate branch omits
    them, which correctly clears any stale candidate on confirm/miss/exit."""
    _atomic_json(
        _mid_session_cap_state_path(persona, state_root=state_root),
        {
            "schema": MID_SESSION_CAP_SCHEMA,
            "runtime": _binding_runtime_ref(binding),
            "modal_present": observation.modal_present,
            "modal_kind": observation.modal_kind,
            "detected_at": _iso(observation.detected_at) if observation.detected_at else None,
            "observed_at": _iso(observation.observed_at),
            "reset_at": _iso(observation.reset_at) if observation.reset_at else None,
            "reset_source": observation.reset_source,
            "candidate_modal_kind": observation.candidate_modal_kind,
            "candidate_text_sha256": candidate_text_sha256,
            "candidate_first_seen_at": (
                _iso(candidate_first_seen_at) if candidate_first_seen_at else None
            ),
        },
    )


def clear_mid_session_cap_state(persona: str, *, state_root: Path | None = None) -> None:
    """Clear the record on runtime exit (T-4037) -- mirrors `clear_active_runtime`.
    Called on EVERY exit path (ordinary death and a cap-swap alike): a dead
    pane has nothing left for the reaper to act on, and leaving a stale
    modal_present=true record risks the reaper reading it against a runtime
    that's already gone (possibly replaced by a fresh one on a different
    harness)."""
    try:
        _mid_session_cap_state_path(persona, state_root=state_root).unlink()
    except FileNotFoundError:
        pass


def _runtime_alive(binding: "PersonaBinding", *, context: "TmuxRuntimeContext | None" = None) -> bool:
    """Authoritative liveness observation for `binding`'s runtime (spec SS
    Durable-state layout: "Authority = `tmux has-session -t <session>` +
    `kill -0 <pid>`", not the active-runtime.json cache).

    T-2966 REBOUND finding #2: tmux session/pane existence alone is a
    sound liveness proxy ONLY because `_start_runtime` execs the runtime
    CLI directly as the pane's command (pane-death IS process-death in
    that specific launch shape) -- but that equivalence is a property of
    today's launch shape, not an invariant this function can assume future
    BTs preserve (a daemonizing/forking future harness, or a different
    respawn path, would silently break it with nothing to catch the drift).
    The pid re-query + `kill -0` cross-check is correct-by-construction
    regardless of launch shape. Pid is looked up FRESH here (never a
    value captured once at start) -- a respawned pane has a different pid,
    and a stale captured value would defeat the cross-check entirely.
    """
    target = binding.controller.tmux
    if not (_session_exists(target.session, context=context) and pane_exists(target, context=context)):
        return False
    pid = _pane_process_pid(target, context=context)
    if pid is None:
        # No pid capturable for this pane/launch-shape -- fall back to the
        # tmux-only check rather than treating an unrelated lookup failure
        # as "dead" (honest degrade for launch shapes we can't pid-capture).
        return True
    return _pid_alive(pid)


def load_last_attempt(
    persona: str, slug: str, *, state_root: Path | None = None
) -> dict[str, Any] | None:
    """Per-runtime last-attempt record, or None if this runtime has never
    been attempted (spec SS `runtimes/<slug>/last-attempt.json`)."""
    path = _last_attempt_path(persona, slug, state_root=state_root)
    if not path.exists():
        return None
    payload = _read_json(path)
    return payload or None


def _cooldown_until_for_failure(
    failure_class: str,
    *,
    now: datetime,
    consecutive_failures: int,
    harness: str,
) -> datetime | None:
    """Failure-class -> cooldown_until (spec SS Failure-class -> cooldown table)."""
    if failure_class == "subscription_cap":
        return cap_reset_ceiling(harness, now=now)
    if failure_class == "runtime_blocked":
        # Never expires automatically -- only `harness clear-cooldown` (BT4) clears it.
        return None
    # process_death / readiness_timeout / operational_failure: shared backoff ladder.
    index = min(max(consecutive_failures - 1, 0), len(_TRANSIENT_FAILURE_BACKOFF_SECONDS) - 1)
    return now + timedelta(seconds=_TRANSIENT_FAILURE_BACKOFF_SECONDS[index])


def record_last_attempt(
    persona: str,
    binding: "PersonaBinding",
    *,
    outcome: Literal["success", "failed"],
    failure_class: str | None = None,
    trigger: str = "controller-selection",
    operator: str | None = None,
    state_root: Path | None = None,
    now: datetime | None = None,
    cooldown_until_override: datetime | None = None,
    bump_failures: bool = True,
) -> dict[str, Any]:
    """Record a runtime attempt's outcome (spec SS `last-attempt.json`).

    `bump_failures=False` (T-4862): re-recording an already-recorded episode
    (e.g. the mid-session swap following its own cap-detect record) refreshes
    the cooldown without incrementing `consecutive_failures` again.

    `outcome="success"` clears `consecutive_failures` to 0 and
    `cooldown_until` to null (spec: only after the sustained-alive
    threshold -- `controller_main` is the caller that decides whether a
    death counts as success; this function just records what it's told).
    `outcome="failed"` increments `consecutive_failures` and computes
    `cooldown_until` from the failure-class table.

    `cooldown_until_override` (T-4037): when given on a `failed` outcome, it
    is used VERBATIM in place of `_cooldown_until_for_failure`'s table lookup
    -- the mid-session cap swap path knows the ACTUAL parsed (or conservative-
    default) reset instant, which is a materially shorter, more accurate
    value than `subscription_cap`'s normal `next_weekly_reset` mapping (that
    mapping is for a cap detected AT START, a different, longer-horizon
    signal this ticket does not change). `consecutive_failures` bookkeeping
    is unaffected either way.
    """
    now = now or _utc_now()
    slug = _binding_runtime_slug(binding)
    previous = load_last_attempt(persona, slug, state_root=state_root) or {}
    if outcome == "success":
        consecutive_failures = 0
        cooldown_until: datetime | None = None
        failure_class = None
    else:
        consecutive_failures = int(previous.get("consecutive_failures") or 0) + (1 if bump_failures else 0)
        consecutive_failures = max(consecutive_failures, 1)
        cooldown_until = (
            cooldown_until_override
            if cooldown_until_override is not None
            else _cooldown_until_for_failure(
                failure_class or "operational_failure",
                now=now,
                consecutive_failures=consecutive_failures,
                harness=binding.launch.harness,
            )
        )
    payload = {
        "schema": "pantheon-persona-runtime-last-attempt/v1",
        "runtime": _binding_runtime_ref(binding),
        "last_attempt_ts": _iso(now),
        "outcome": outcome,
        "failure_class": failure_class,
        "cooldown_until": _iso(cooldown_until) if cooldown_until is not None else None,
        "consecutive_failures": consecutive_failures,
        "provenance": {"trigger": trigger, "operator": operator},
    }
    _atomic_json(_last_attempt_path(persona, slug, state_root=state_root), payload)
    return payload


#: T-5028 (e): the one key a fail-closed holds view carries, so the malformed
#: state is visible wherever holds are listed (the health "Holds" field, status).
HOLDS_MALFORMED_KEY = "<all:holds-malformed>"
HOLDS_MALFORMED_ALERT_STAMP = "holds-malformed-alert.json"


class _FailClosedHolds(dict):
    """T-5028 (e): holds.json exists but cannot be read as a holds document.

    The operator's intent is unknowable, so EVERY runtime reads as held through
    every access path (``in``, ``[]``, ``get``), with a synthetic hold record. A
    live pane is still reused (that is not a launch); only fresh launches stop.
    ``keys``/``items``/``len``/``bool`` see the single HOLDS_MALFORMED_KEY entry,
    so the mapping is truthy and the malformed state shows wherever holds list."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        self.record = {"by": "fail-closed", "reason": f"holds-malformed: {detail}"}
        super().__init__({HOLDS_MALFORMED_KEY: self.record})

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: Any) -> dict[str, Any]:
        return self.record

    def __missing__(self, key: Any) -> dict[str, Any]:
        return self.record

    def get(self, key: Any, default: Any = None) -> dict[str, Any]:
        return self.record


def _read_holds_payload(path: Path) -> tuple[dict[str, Any] | None, str]:
    """(holds mapping, "") for a well-formed holds.json, else (None, why)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__}: {exc})"[:300]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON ({exc})"[:300]
    if not isinstance(payload, dict):
        return None, f"top level is {type(payload).__name__}, not an object"
    holds = payload.get("holds")
    if not isinstance(holds, dict):
        return None, "missing or non-object 'holds'"
    return dict(holds), ""


def _alert_holds_malformed(persona: str, path: Path, detail: str, *, state_root: Path | None) -> None:
    """Route the fail-closed state to Recovery through the existing cooldown-gated
    alert path (once per PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS). Never raises."""
    try:
        _route_cooldown_gated_alert(
            persona,
            f"runtime-controller {persona}: {path} is malformed ({detail}). Every runtime is "
            "treated as operator-held until it is fixed: live panes keep running, no fresh "
            "launch happens. Fix or rewrite the file (harness <persona> status shows it).",
            f"holds-malformed ({persona})",
            stamp_name=HOLDS_MALFORMED_ALERT_STAMP,
            state_root=state_root,
            now=None,
        )
    except Exception:  # noqa: BLE001 -- alerting is best-effort; the fail-closed read stands
        pass


def load_holds(persona: str, *, state_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Manual holds (spec SS `overrides/holds.json`) -- keyed by
    ``<harness>/<profile>`` runtime reference. Empty dict if the file
    doesn't exist (no holds set).

    T-5028 (e): a file that exists but is unreadable, invalid JSON, not an
    object, or lacks an object ``holds`` fails CLOSED: a `_FailClosedHolds`
    in which every runtime is held, plus a cooldown-gated alert to Recovery."""
    path = _holds_path(persona, state_root=state_root)
    try:
        absent = not path.exists() and not path.is_symlink()
    except OSError as exc:
        absent, holds, detail = False, None, f"unreadable ({type(exc).__name__}: {exc})"[:300]
    else:
        holds, detail = (None, "") if absent else _read_holds_payload(path)
    if absent:
        return {}
    if holds is not None:
        return holds
    _alert_holds_malformed(persona, path, detail, state_root=state_root)
    return _FailClosedHolds(detail)


def _instruction_sha256(manifest_path: Path) -> str:
    """SHA-256 of the canonical instruction artifact the controller read
    this cycle (T-2966 REBOUND finding #1; spec SS R31->R38 mapping:
    'instruction provenance ... the controller reads it + records the
    SHA-256 of what it read into the invocation log; no self-proof').

    Deliberately hashes ONLY the resolved `manifest_path` (`instance.yaml`)
    -- the literal file `controller_main` reads each cycle to decide what
    to do -- via the existing generic `_sha256_file` helper. NOT a reuse of
    `RUNTIME_SOURCE_HASH_PATHS`/`_controller_source_provenance`: that KEEP
    machinery hashes ~19 files (including this module's own source) and
    REQUIRES a clean git worktree, which is the controller proving
    something about ITSELF -- the exact self-referential shape the R31-R38
    dissolve retired. 'Instruction provenance' names what was READ to make
    the selection, not the controller's own code integrity; one file, no
    worktree precondition, safe to compute every restart-loop cycle."""
    return _sha256_file(manifest_path)


def log_invocation(
    persona: str,
    outcome: str,
    *,
    trigger: str = "systemd-restart",
    selected: str | None = None,
    skipped: Sequence[Mapping[str, Any]] | None = None,
    instruction_sha256: str | None = None,
    state_root: Path | None = None,
    when: datetime | None = None,
) -> None:
    """Append-only per-invocation audit record (spec SS
    `invocations/<yyyy-mm-dd>.jsonl`). Write-only from `controller_main`'s
    own DECISION logic -- selection never reads this back, preserving
    SS88 statelessness of the control loop. `_last_invocation_ts` (T-2969
    BT3) DOES read it back, for `LastSelectionTs` -- a read-only
    observability projection that never feeds into selection, a
    different invariant than the one this note protects.

    `instruction_sha256` (T-2966 REBOUND finding #1) is the SHA-256 of the
    manifest this cycle read (see `_instruction_sha256`) -- recorded on
    every call, since every `controller_main` cycle reads the manifest
    before it can decide selected-vs-skipped, not only on a successful
    start."""
    when = when or _utc_now()
    _append_jsonl(
        _invocations_path(persona, state_root=state_root, when=when),
        {
            "ts": _iso(when),
            "trigger": trigger,
            "outcome": outcome,
            "selected": selected,
            "skipped": list(skipped) if skipped else [],
            "instruction_sha256": instruction_sha256,
        },
    )


def _runtime_blocked_record(attempt: Mapping[str, Any] | None) -> bool:
    """T-5028 (d): a last-attempt record that blocks its runtime until an
    operator clears it (failed + failure_class runtime_blocked)."""
    return bool(attempt) and attempt.get("outcome") == "failed" and attempt.get("failure_class") == "runtime_blocked"


def _select_viable_runtime(
    persona: str,
    candidates: Sequence["PersonaBinding"],
    holds: Mapping[str, Any],
    current_time: datetime,
    *,
    state_root: Path | None = None,
    usability_probe: Callable[["PersonaBinding", datetime | None], Any] | None = None,
) -> tuple["PersonaBinding | None", list[dict[str, Any]]]:
    """Spec SS Controller invocation flow, step 2: first viable runtime in
    manifest order. A runtime is skipped if held, or if its recorded
    cooldown hasn't elapsed.

    `usability_probe` (T-4862; None = exactly the behaviour above): consulted
    ONLY for a candidate whose active cooldown is a recorded subscription_cap
    (early-reset detection). A live `usable`/`near-cap` verdict supersedes that
    cooldown (success record, trigger "early-reset-probe", plus an evidence
    row) and the candidate is selected; anything else keeps it skipped.

    T-4879: when a cap episode is in play (a candidate skipped on an active
    subscription_cap cooldown) and 2+ candidates remain viable, they are ranked
    usable > near-cap > unknown by `_rank_viable_after_cap` instead of taken in
    manifest order. Without a probe, or without a cap episode, behaviour is
    unchanged (first viable in manifest order)."""
    skipped: list[dict[str, Any]] = []
    # ORG-106 L3a: a deep-sleeping seat has NO selectable runtime, whatever the holds
    # or cooldowns say. Cause is seat-sleep (not "held" / "all-held"), so health and
    # evidence say why. Every caller (controller, cap-swap, fallback-return, planned
    # swap eligibility) reads None as "nothing to switch to".
    if candidates and _seat_launch_block(persona, state_root=state_root).sleeping:
        return None, [{"runtime": _binding_runtime_ref(b), "reason": SEAT_SLEEP_CAUSE} for b in candidates]
    # T-4879: with a probe, collect every cooldown-viable candidate so a cap-episode
    # reselection can rank them; (binding, skipped rows before it, probe already taken).
    viable: list[tuple[Any, int, Any]] = []
    cap_episode = False
    for binding in candidates:
        ref = _binding_runtime_ref(binding)
        if ref in holds:
            skipped.append({"runtime": ref, "reason": "held"})
            continue
        attempt = load_last_attempt(persona, _binding_runtime_slug(binding), state_root=state_root)
        if _runtime_blocked_record(attempt):
            # T-5028 (d): runtime_blocked never expires on its own (cooldown_until
            # is None by design); only `harness clear-cooldown` makes it selectable.
            skipped.append({"runtime": ref, "reason": "runtime-blocked"})
            continue
        cooldown_until = _parse_iso_or_none((attempt or {}).get("cooldown_until"))
        if cooldown_until is not None and cooldown_until > current_time:
            is_cap = (attempt or {}).get("failure_class") == "subscription_cap"
            cap_episode = cap_episode or is_cap
            if usability_probe is not None and is_cap:
                try:
                    probe = usability_probe(binding, cooldown_until)
                    if probe.verdict in ("usable", "near-cap"):
                        from tools import subscription_usability

                        subscription_usability.apply_probe_side_effects(
                            persona, binding, probe, state_root=state_root, now=current_time
                        )
                        viable.append((binding, len(skipped), probe))
                        continue
                except Exception:  # noqa: BLE001 -- a probe fault keeps the cooldown
                    pass
            skipped.append({"runtime": ref, "reason": f"cooldown-active-until-{_iso(cooldown_until)}"})
            continue
        viable.append((binding, len(skipped), None))
        if usability_probe is None or not cap_episode:
            break
    if not viable:
        return None, skipped
    chosen, skipped_before, _ = viable[0]
    if usability_probe is not None and cap_episode and len(viable) > 1:
        chosen, skipped_before = _rank_viable_after_cap(
            persona, viable, holds, current_time, state_root=state_root, usability_probe=usability_probe
        ) or (chosen, skipped_before)
    return chosen, skipped[:skipped_before]


def _rank_viable_after_cap(
    persona: str,
    viable: Sequence[tuple[Any, int, Any]],
    holds: Mapping[str, Any],
    current_time: datetime,
    *,
    state_root: Path | None,
    usability_probe: Callable[["PersonaBinding", datetime | None], Any],
) -> tuple[Any, int] | None:
    """T-4879: usable > near-cap > unknown among cooldown-viable candidates, via the
    SAME `subscription_usability.rank_and_select` the planned-quota path uses.
    The runtime that capped is confirmed capped, so `unknown` is allowed
    (primary_confirmed_capped=True) and a `capped` verdict is never chosen. A probe
    fault is `unknown`. None (every candidate capped) keeps manifest order, the
    pre-T-4879 behaviour -- the seat is never left dark by ranking."""
    from tools import subscription_usability

    probes: dict[str, Any] = {}
    for binding, _count, taken in viable:
        ref = _binding_runtime_ref(binding)
        probe = taken
        if probe is None:
            try:
                probe = usability_probe(binding, None)
                subscription_usability.apply_probe_side_effects(
                    persona, binding, probe, state_root=state_root, now=current_time
                )
            except Exception:  # noqa: BLE001 -- a probe fault ranks as unknown
                probe = probes.get(ref)
        if probe is not None:
            probes[ref] = probe
    decision = subscription_usability.rank_and_select(
        [binding for binding, _c, _p in viable], probes, holds=holds, primary_confirmed_capped=True
    )
    if decision.chosen is None:
        return None
    return next((b, count) for b, count, _p in viable if b is decision.chosen)


def _earliest_cooldown_expiry(
    persona: str,
    candidates: Sequence["PersonaBinding"],
    holds: Mapping[str, Any],
    *,
    state_root: Path | None = None,
) -> datetime | None:
    """Earliest cooldown_until across non-held candidates, or None if no
    candidate has a (finite) cooldown to wait out -- e.g. everything is
    held, or a cooldown is `runtime_blocked` (never auto-expires)."""
    expiries = []
    for binding in candidates:
        if _binding_runtime_ref(binding) in holds:
            continue
        attempt = load_last_attempt(persona, _binding_runtime_slug(binding), state_root=state_root)
        cooldown_until = _parse_iso_or_none((attempt or {}).get("cooldown_until"))
        if cooldown_until is not None:
            expiries.append(cooldown_until)
    return min(expiries) if expiries else None


def _terminate_runtime_pid(
    binding: "PersonaBinding", *, context: "TmuxRuntimeContext | None" = None
) -> bool:
    """Best-effort SIGTERM to `binding`'s pane pid (T-4037 swap action).

    Mirrors `_runtime_alive`'s own fresh pid lookup: the pane's process IS the
    harness CLI directly (see that function's docstring), so terminating this
    pid is equivalent to ending the runtime -- tmux tears the pane/session
    down once the process exits, which the caller confirms via `is_alive()`
    (never assumed here). Fail-open (never raises): a pid lookup failure or
    an already-gone pid is not an error -- the caller only needs "no longer
    running", and a runtime that's already gone satisfies that trivially.
    Only ever sends SIGTERM -- this landing never escalates to SIGKILL (HO
    amendment A, T-4037); a pane that doesn't die from SIGTERM within the
    caller's confirm window is left alone and retried next cap-check cycle.
    """
    pid = _pane_process_pid(binding.controller.tmux, context=context)
    if not _is_signalable_pid(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _mid_session_cap_hit(harness: str, text: str, *, now: datetime) -> Any:
    """T-5011 PR-B: the integration-provided mid-session cap classifier for
    `harness`, or None when it has none (or is not registered)."""
    if harness not in harness_integration.configured_harness_names():
        return None
    classify = getattr(harness_integration.integration_for(harness), "classify_mid_session_cap", None)
    return classify(text, now=now) if classify is not None else None


class _CodexCapSample(NamedTuple):
    """Codex banner sample shaped like ClaudeMidSessionCapModal for the shared debounce."""

    modal_kind: str
    reset_at: datetime | None
    reset_source: str | None
    render_span_sha256: str


def _observe_and_publish_mid_session_cap(
    binding: "PersonaBinding",
    *,
    capture_pane_fn: Callable[[], str],
    now: datetime,
    state_root: Path | None,
) -> MidSessionCapObservation | None:
    """One detect-and-publish cycle (T-4037). Returns the fresh observation,
    or None on a fail-open (unreadable pane) -- in which case NO state is
    written (the brief: "no detection means no action"; a bad capture must
    never assert a false "modal cleared" over a real prior observation).

    T-4037 REBOUND round 2 Finding 3 (debounce): a single classifying sample
    is only ever a CANDIDATE. `modal_present: true` -- the only predicate
    Recovery's reaper (or this module's own swap gate) may act on -- requires a
    SECOND consecutive classifying sample whose
    `ClaudeMidSessionCapModal.render_span_sha256` matches the pending
    candidate's exactly. The comparison is scoped to the modal's own tight
    render span (not the full pane) precisely so it can ever succeed on real
    captures: the pane routinely carries unrelated churn ABOVE the modal
    between 60s-apart polls (e.g. a ticking "Skipped for Nm Ns" status line)
    even while the modal itself sits frozen open.

    State-machine (each branch is mutually exclusive):
      * `modal is None` (didn't classify) -- clears any pending candidate and
        any confirmed episode alike; writes modal_present=False.
      * `modal is not None` and the PRIOR write was already a CONFIRMED
        episode (`prior_present`) -- stays confirmed. Debounce gates only the
        risky ENTRY into modal_present=True, not continued observation of an
        already-confirmed state: re-requiring 2-sample confirmation on EVERY
        poll would let one altered/missed capture flap the reaper's trigger
        for no operational reason. `detected_at` stays sticky to the
        episode's original first sighting.
      * `modal is not None`, no confirmed episode, and the PRIOR write left a
        pending candidate whose hash matches THIS sample -- CONFIRMS. This is
        the transition to modal_present=True. `detected_at` is backdated to
        the candidate's `candidate_first_seen_at` (the FIRST of the two
        confirming samples) -- "first sighting of the confirmed episode",
        matching this module's pre-existing sticky-`detected_at` semantics
        for the episode as a whole, not the instant it happened to cross the
        confirmation threshold.
      * `modal is not None` and neither of the above (a fresh first sighting,
        OR a classifying sample that does NOT match a still-pending
        candidate) -- becomes (or replaces) the pending candidate.
        modal_present stays False; `candidate_modal_kind` is set for
        diagnostic visibility only. A pane that changes between samples thus
        RESETS the candidate rather than accumulating toward confirmation
        with mismatched evidence.
    """
    try:
        text = capture_pane_fn()
    except Exception:
        text = None
    if not text:
        return None
    modal: Any = None
    if binding.launch.harness == "claude":
        modal = classify_claude_mid_session_cap(text, now=now)
    elif binding.launch.harness == "codex":
        hit = classify_codex_usage_limit(text, now=now)
        if hit is not None:
            modal = _CodexCapSample(hit.cap_kind, hit.reset_at, hit.reset_source, hit.render_span_sha256)
    else:
        # T-5011 PR-B (plan §3.2 gap 4): any other registered harness classifies
        # through its integration's classify_mid_session_cap (hermes: the
        # exhausted-balance render, no reset). claude/codex keep the exact
        # branches above; a harness with no classifier never classifies.
        hit = _mid_session_cap_hit(binding.launch.harness, text, now=now)
        if hit is not None:
            modal = _CodexCapSample(
                hit.cap_kind,
                getattr(hit, "reset_at", None),
                getattr(hit, "reset_source", None),
                hit.render_span_sha256,
            )
    prior = read_mid_session_cap_state(binding.persona, state_root=state_root)
    prior_present = bool(prior) and bool(prior.get("modal_present"))
    prior_detected_at = _parse_iso_or_none(prior.get("detected_at")) if prior else None
    prior_candidate_hash = prior.get("candidate_text_sha256") if prior else None
    prior_candidate_first_seen_at = (
        _parse_iso_or_none(prior.get("candidate_first_seen_at")) if prior else None
    )

    candidate_hash: str | None = None
    candidate_first_seen_at: datetime | None = None

    if modal is None:
        observation = MidSessionCapObservation(
            modal_present=False, modal_kind=None, detected_at=None,
            observed_at=now, reset_at=None, reset_source=None,
            candidate_modal_kind=None,
        )
    elif prior_present:
        detected_at = prior_detected_at if prior_detected_at is not None else now
        observation = MidSessionCapObservation(
            modal_present=True, modal_kind=modal.modal_kind, detected_at=detected_at,
            observed_at=now, reset_at=modal.reset_at, reset_source=modal.reset_source,
            candidate_modal_kind=None,
        )
    elif prior_candidate_hash is not None and prior_candidate_hash == modal.render_span_sha256:
        detected_at = prior_candidate_first_seen_at if prior_candidate_first_seen_at is not None else now
        observation = MidSessionCapObservation(
            modal_present=True, modal_kind=modal.modal_kind, detected_at=detected_at,
            observed_at=now, reset_at=modal.reset_at, reset_source=modal.reset_source,
            candidate_modal_kind=None,
        )
    else:
        observation = MidSessionCapObservation(
            modal_present=False, modal_kind=None, detected_at=None,
            observed_at=now, reset_at=None, reset_source=None,
            candidate_modal_kind=modal.modal_kind,
        )
        candidate_hash = modal.render_span_sha256
        candidate_first_seen_at = now
    _write_mid_session_cap_state(
        binding.persona, binding, observation, state_root=state_root,
        candidate_text_sha256=candidate_hash,
        candidate_first_seen_at=candidate_first_seen_at,
    )
    if observation.modal_present and not prior_present:
        # T-4862: the debounced ENTRY into a confirmed cap (not every poll)
        # is recorded so `harness status` shows the cap even when no swap fires.
        try:
            record_last_attempt(
                binding.persona, binding, outcome="failed", failure_class="subscription_cap",
                trigger="mid-session-cap-detect", state_root=state_root, now=now,
                cooldown_until_override=observation.reset_at
                or (now + timedelta(seconds=MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)),
            )
        except Exception:  # noqa: BLE001 -- bookkeeping must not break detection
            pass
    return observation


def _comms_receiver_ready(
    binding: "PersonaBinding",
    *,
    manifest_path: Path | None = None,
) -> tuple[bool, str]:
    """T-5451: is the codex Telegram receiver ready for a claude->codex swap?

    Delegates to `codex_telegram_bridge.receiver_readiness`, the ONE definition
    shared with `persona_installer_verify_seat` (no parallel logic). Fails
    CLOSED: an unresolvable manifest/bridge binding is "not ready", with the
    reason text carrying the error. Imports are lazy (persona_installer imports
    this module lazily too; no module-level cycle).
    """
    try:
        from tools import codex_telegram_bridge, persona_bringup, persona_installer

        path = manifest_path or persona_bringup.DEFAULT_MANIFEST
        bridge = codex_telegram_bridge.binding_from_manifest(
            persona_installer.Manifest.load(path, strict_flip_keys=False), binding.persona,
        )
        failed = [
            f"{c.name}: {c.detail}"
            for c in codex_telegram_bridge.receiver_readiness(
                bridge, managed_marker=persona_installer.MANAGED_MARKER,
            )
            if not c.ok
        ]
    except Exception as exc:  # noqa: BLE001 -- fail closed on any resolution error
        return False, f"readiness unresolvable: {type(exc).__name__}: {exc}"[:500]
    return (not failed), "; ".join(failed)


def _report_swap_receiver_not_ready(
    persona: str,
    why: str,
    *,
    state_root: Path | None,
    now: datetime | None,
) -> None:
    """T-5451: a confirmed cap modal could not be swapped because the codex
    receiver is not ready. Evidence event + alert, both behind the shared alert
    cooldown (the cap check runs every ~60s; one report per cooldown window).
    Never raises."""
    msg = (
        f"runtime-controller {persona}: a subscription-cap modal is on the live claude pane but the "
        f"mid-session swap to codex was REFUSED -- the codex Telegram receiver is not ready ({why}). "
        f"Swapping onto it would drop comms and relaunch the capped claude. Fix the install "
        f"(persona_installer apply + verify-seat); the swap fires on its own once the receiver is ready."
    )
    result = _route_cooldown_gated_alert(
        persona, msg, f"controller-swap-receiver-not-ready ({persona})",
        stamp_name="swap-receiver-not-ready-alert.json", state_root=state_root, now=now,
    )
    if result.get("alert_routed"):
        try:
            _append_controller_evidence(
                persona, "mid_session_swap_receiver_not_ready", {"reason": why[:500]}, state_root=state_root,
            )
        except Exception:  # noqa: BLE001 -- evidence is best-effort
            pass


def _note_deprecated_flip_keys(
    persona: str,
    prefix: str,
    document: Mapping[str, Any],
    *,
    state_root: Path | None,
    now: datetime | None,
) -> None:
    """T-5446 Phase A: the seat's role still carries a retired flip key set to `true`
    (tolerated, a deprecated no-op until Phase B, T-5460). One evidence line per
    cooldown window, no alert."""
    from tools.harness_adapter.diagnostics import flip_key_deprecations

    mine = [d for d in flip_key_deprecations(document) if d.startswith(prefix)]
    if not mine:
        return
    current = now or _utc_now()
    path = _persona_state(persona, state_root) / "deprecated-flip-key-note.json"
    last = _parse_iso_or_none(_read_json(path).get("last_at"))
    if last is not None and (current - last).total_seconds() < PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS:
        return
    _append_controller_evidence(persona, "deprecated_flip_key_noop", {"keys": mine}, state_root=state_root)
    _atomic_json(path, {"last_at": _iso(current)})


def _report_retired_flip_keys(
    persona: str,
    role: str,
    manifest_path: Path,
    *,
    state_root: Path | None,
    now: datetime | None = None,
) -> None:
    """T-5458 / T-5446 Phase A: a `false` (or non-bool) re-added to a retired flip key on
    THIS seat's role is ignored by the resolver (the flip stays on), so the controller
    keeps running; surface it here with one evidence event and one cooldown-gated alert.
    CI and Manifest.load still fail on the same condition. Never raises."""
    try:
        from tools import agent_role_map
        from tools.harness_adapter.diagnostics import flip_key_violations

        prefix = f"harness_runtime.roles.{role}."
        document = agent_role_map.load_instance_document(manifest_path)
        mine = [v for v in flip_key_violations(document) if v.startswith(prefix)]
        if not mine:
            _note_deprecated_flip_keys(persona, prefix, document, state_root=state_root, now=now)
            return
        msg = (
            f"runtime-controller {persona}: instance.yaml carries a retired flip key with a forbidden "
            f"value ({'; '.join(mine)[:900]}). The value is IGNORED and the flip stays on; remove the key "
            f"(T-5446, the operator TG 12274/12283)."
        )
        result = _route_cooldown_gated_alert(
            persona, msg, f"controller-retired-flip-key ({persona})",
            stamp_name="retired-flip-key-alert.json", state_root=state_root, now=now,
        )
        if result.get("alert_routed"):
            _append_controller_evidence(
                persona, "retired_flip_key_ignored", {"violations": mine}, state_root=state_root,
            )
    except Exception as exc:  # noqa: BLE001 -- reporting must never stop the controller
        try:
            print(f"[runtime_controller] {persona}: retired-flip-key report failed: {exc!r}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def _mid_session_cap_swap_eligible(
    binding: "PersonaBinding",
    *,
    candidates: Sequence["PersonaBinding"] | None,
    holds: Mapping[str, Any] | None,
    now: datetime,
    state_root: Path | None,
    usability_probe: Callable[["PersonaBinding", datetime | None], Any] | None = None,
    manifest_path: Path | None = None,
) -> bool:
    """True iff the mid-session-cap SWAP action (not detect+publish, which is
    unconditional) may fire for `binding` right now (T-4037, policy flip T-5446).

    All must hold: (1) the flag is on -- schema-defaulted True for every seat
    (T-5446; the per-role key is retired and rejected by the validator);
    (2) a fallback OTHER than `binding` itself exists and is viable RIGHT NOW,
    reusing `_select_viable_runtime` (the exact selection logic controller_main
    itself uses) rather than a parallel viability check -- a seat with no
    fallback (archivist, watchman, ...) returns False here, before anything
    else; (3) for a comms persona (`receiver_handoff_required`) the codex
    receiver is READY (`_comms_receiver_ready`). The old blanket comms
    exclusion is gone: since T-4184 the next controller_main cycle runs the
    full claude->codex receiver handoff on any codex launch, so the swap
    itself is just a SIGTERM. A not-ready receiver refuses the swap (it would
    thrash: swap -> handoff fails -> relaunch the capped claude) and raises an
    evidence event plus a cooldown-gated alert instead -- an install defect to
    surface, not paper over.
    """
    if not binding.controller.mid_session_cap_swap_enabled:
        return False
    if not candidates:
        return False
    own_ref = _binding_runtime_ref(binding)
    fallback_candidates = [c for c in candidates if _binding_runtime_ref(c) != own_ref]
    if not fallback_candidates:
        return False
    if receiver_handoff_required(binding):
        ready, why = _comms_receiver_ready(binding, manifest_path=manifest_path)
        if not ready:
            _report_swap_receiver_not_ready(binding.persona, why, state_root=state_root, now=now)
            return False
    effective_holds = holds if holds is not None else load_holds(binding.persona, state_root=state_root)
    if usability_probe is None:
        selected, _skipped = _select_viable_runtime(
            binding.persona, fallback_candidates, effective_holds, now, state_root=state_root
        )
        return selected is not None
    # T-4862: the active runtime is confirmed capped (R1) -- rank the cooldown-viable
    # fallbacks by live usability; capped ones never qualify, unknown may be attempted.
    viable = [
        c for c in fallback_candidates
        if _select_viable_runtime(
            binding.persona, [c], effective_holds, now, state_root=state_root,
            usability_probe=usability_probe,
        )[0] is not None
    ]
    if not viable:
        return False
    from tools import subscription_usability

    decision, _probes = subscription_usability.select_usable_fallback(
        binding.persona, viable, state_root=state_root, now=now,
        primary_confirmed_capped=True, allow_spend=False, holds=effective_holds,
    )
    return decision.chosen is not None


def _pane_appears_idle(capture_pane_fn: Callable[[], str]) -> bool:
    """True iff the pane appears idle right now (T-4046) -- reuses
    `ready_for_submission` (tools/submit_verify.py), the SAME harness-neutral
    predicate this module already uses for post-start readiness
    (`_wait_for_runtime_input_ready`, above): a live composer glyph with no
    blocking confirm-dialog is the existing "safe to submit into this pane"
    signal for BOTH claude (``❯``) and codex (``›``), repurposed here as
    "safe to interrupt this pane" -- a session sitting between turns and a
    session ready for a fresh submission are the same pane state. Proven
    against a REAL live-captured codex idle composer render (T-4046 HO
    amendment A), not just claude's, at
    tools/test_bringup_acceptance_harness_codex_ready.py:170-171
    (`test_post_dismiss_composer_is_ready_for_submission`, fixture
    `READY_COMPOSER_AFTER_DISMISS` captured 2026-08-05 against codex-cli
    0.136.0) -- the failback pane being checked here is usually codex.

    Fails to False (busy, never idle) on any capture exception -- an
    unreadable pane is never mistaken for an idle one; the caller's bounded
    max-wait is what keeps this from stalling failback forever on a
    persistently-unreadable pane.
    """
    try:
        text = capture_pane_fn()
    except Exception:
        return False
    return bool(text) and ready_for_submission(text)


def _fallback_return_eligible(
    binding: "PersonaBinding",
    *,
    candidates: Sequence["PersonaBinding"] | None,
    holds: Mapping[str, Any] | None,
    now: datetime,
    state_root: Path | None,
) -> "PersonaBinding | None":
    """The strictly-higher-priority candidate to fail back TO right now, or
    None (T-4046).

    All of these must hold: (1) RuntimeControllerConfig.fallback_return_enabled
    is set (schema-defaulted True for every seat, T-5446; not hand-settable); (2) `binding` is not already the first (highest-priority)
    candidate in manifest order -- nothing outranks it, so there is nothing
    to fail back TO; (3) a candidate STRICTLY BEFORE `binding` in manifest
    order is viable RIGHT NOW, reusing `_select_viable_runtime` restricted
    to that higher-priority prefix -- the exact same viability logic
    controller_main itself uses (this ticket's brief: "reuse
    _select_viable_runtime as the single source of truth"; no parallel
    viability logic).

    Deliberately has NO receiver-readiness gate (unlike
    `_mid_session_cap_swap_eligible`, T-5451): this direction is
    fallback-without-comms -> primary-with-comms, which RESTORES comms.
    Concretely, for every comms-carrying persona configured today (orchestrator,
    specialist, recovery; instance.yaml `claude_receiver_topology: runtime-owned`
    for all three), the Telegram receiver runs INSIDE the claude harness
    process itself, so an ordinary controller_main restart of the claude
    binding already re-establishes it with no separate handoff step. The
    codex side is NOT free of state, though (T-4158 correction): the
    installer only refuses to start/enable `codex_receiver_unit` at INSTALL
    time, but a consume-path bootstrap that selects codex starts that
    poller unit at runtime, and it outlives the codex pane. The controller
    start path therefore stops it and proves it absent before launching
    claude over a codex record/pane
    (`_stop_prior_harness_receiver_before_start`), fail-closed. RE-EVALUATE TRIGGER: if a
    persona's `claude_receiver_topology` is ever migrated to `systemd-user`
    (the historical shape recorded today only as
    `predecessor_claude_receiver_topology` for orchestrator), a plain runtime
    restart would NOT re-arm that separate systemd unit, and failback would
    need the KEEP layer's post-swap-to-claude receiver-restore + presence-
    proof steps (see `receiver_handoff_actions` in
    tools/codex_telegram_actions.py for the actual step names) that this
    lightweight path deliberately does not invoke -- named here, not built,
    per this ticket's scope (no new continuity machinery). This module is
    structurally guarded against embedding that KEEP-layer action-name
    vocabulary directly (see ReceiverHandoffTests in this file's test
    module), which is exactly why this docstring points at the source file
    instead of quoting the literal step strings.
    """
    if not binding.controller.fallback_return_enabled:
        return None
    if not candidates:
        return None
    own_ref = _binding_runtime_ref(binding)
    own_index = next(
        (i for i, candidate in enumerate(candidates) if _binding_runtime_ref(candidate) == own_ref),
        None,
    )
    if own_index is None or own_index == 0:
        return None
    higher_priority = candidates[:own_index]
    effective_holds = holds if holds is not None else load_holds(binding.persona, state_root=state_root)
    selected, _skipped = _select_viable_runtime(
        binding.persona, higher_priority, effective_holds, now, state_root=state_root
    )
    return selected


_QUOTA_TRANSITION_TRIGGERS = frozenset({"planned-quota", "quota-reset", "forced-cap"})


def _completed_transition_destination(
    persona: str,
    candidates: Sequence["PersonaBinding"] | None,
    *,
    triggers: frozenset[str] | set[str],
    state_root: Path | None,
) -> "PersonaBinding | None":
    """The candidate a COMPLETED transition driven by one of `triggers` swapped
    `persona` onto, or None (T-4469, generalized T-4872).

    Only phase=completed counts: an in-flight transition is not a standing choice.
    An unreadable transition.json reads as {} (no claim). A destination outside
    `candidates` is ignored. Says nothing about whether that runtime is still the
    live one; callers that act on it pair it with _pane_runs_binding.
    """
    if not candidates:
        return None
    state = transition_state(persona, state_root=state_root)
    if state.get("trigger") not in triggers or state.get("phase") != "completed":
        return None
    destination = state.get("destination")
    return next((c for c in candidates if _binding_runtime_ref(c) == destination), None)


def _operator_transition_destination(
    persona: str,
    candidates: Sequence["PersonaBinding"] | None,
    *,
    state_root: Path | None,
) -> "PersonaBinding | None":
    """The candidate a COMPLETED operator transition swapped `persona` onto, or None
    (T-4469). Operator-only: this is what pins the runtime at controller restart
    (see _operator_pinned_runtime); quota-driven destinations are NOT pinned."""
    return _completed_transition_destination(
        persona, candidates, triggers=frozenset({"operator"}), state_root=state_root
    )


def _pane_runs_binding(binding: "PersonaBinding", *, context: "TmuxRuntimeContext | None" = None) -> bool:
    """True only when the pane's live process argv POSITIVELY matches `binding`
    (T-4469). An unreadable pid or argv is False: never a guess."""
    pid = _pane_process_pid(binding.controller.tmux, **_context_kwargs(context))
    if pid is None:
        return False
    argv = _read_pane_cmdline(pid)
    return bool(argv) and _pane_argv_matches_binding(argv, binding)


def _operator_pinned_runtime(
    persona: str,
    candidates: Sequence["PersonaBinding"] | None,
    *,
    holds: Mapping[str, Any] | None,
    state_root: Path | None,
    context: "TmuxRuntimeContext | None" = None,
) -> "PersonaBinding | None":
    """The operator transition's destination while it IS the live runtime, else None
    (T-4469).

    controller_main selects this over manifest priority, so a Restart=always respawn
    after an operator swap attaches to the destination instead of respawning the
    primary over it (which would silently undo the swap). The pin lapses by itself
    once that runtime is gone (for example ended by fallback-return, whose target is
    re-derived from priority as before), and a later operator transition replaces it.
    A hold on the destination wins: a held runtime is never pinned, and neither is
    a runtime_blocked one (T-5028 m2, same as selection).
    """
    destination = _operator_transition_destination(persona, candidates, state_root=state_root)
    if destination is None:
        return None
    # T-5069: holds=None means "read them now" (matches the fallback-return and
    # cap-swap predicates); `holds or {}` would silently disable the veto.
    effective_holds = holds if holds is not None else load_holds(persona, state_root=state_root)
    if _binding_runtime_ref(destination) in effective_holds:
        return None
    if _runtime_blocked_record(
        load_last_attempt(persona, _binding_runtime_slug(destination), state_root=state_root)
    ):
        return None
    if not _pane_runs_binding(destination, context=context):
        return None
    return destination


def _classify_death(exc: BaseException | None) -> str:
    """Map a start/readiness failure onto the spec's failure-class
    vocabulary (SS Failure-class -> cooldown table). A death with no
    exception (the runtime started clean, ran, and its tmux session later
    vanished) is `process_death` -- the spec's default/general case."""
    if isinstance(exc, RuntimeOperationalFailure):
        return {
            "subscription-cap": "subscription_cap",
            "operational-launch": "operational_failure",
            "operational-readiness": "readiness_timeout",
        }.get(exc.failure_class, "operational_failure")
    return "process_death"


def _post_start_cap_cooldown_override(
    exc: BaseException | None,
    failure_class: str,
    *,
    now: datetime,
    harness: str,
) -> datetime | None:
    """T-4047: a proportionate `subscription_cap` cooldown for a START-UP cap
    (`classify_claude_post_start_subscription_cap`, reached via
    `_wait_for_runtime_input_ready`'s adapter check). The spec's
    failure-class table (`_cooldown_until_for_failure`) defaults
    `subscription_cap` to `next_weekly_reset` -- correct for a genuinely
    weekly cap, up to ~7 days disproportionate for Claude's ~5h
    session/opus rolling limit, and amplifies a failback into a runtime
    that is, in reality, still capped.

    Returns the value to pass as `record_last_attempt`'s
    `cooldown_until_override`, or `None` to keep the table's own
    `next_weekly_reset` (used whenever nothing more precise is known,
    INCLUDING a genuinely weekly cap whose reset text didn't parse, and
    any `RuntimeOperationalFailure` raised without `readiness_evidence`).

    Precedence:
      1. A reset instant PARSED off the pane -- used whenever present,
         regardless of modal kind (a parseable "resets Sunday" on a
         weekly cap is very likely SHORTER than a full week, e.g. if
         today is Saturday).
      2. Unparsed + a non-weekly kind (session/opus/unknown) --
         `MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS`, the SAME 4h
         guess T-4037's mid-session swap already uses for this exact
         "modal classified, no reset text" situation.
      3. Unparsed + weekly, or no cap-kind evidence at all -- `None`, so
         the table's weekly default stands (the historically-safe
         posture for the one case where "stay off it longer" really is
         the right guess).

    Clamp (the operator amendment, T-4047 plan review): both (1) and (2) are
    clamped to `[now, next_weekly_reset(harness, now=now)]` -- a misparse
    (e.g. a dated form resolved into next year) must never produce a
    cooldown LONGER than the weekly fallback it replaces, so this change
    can only ever SHORTEN a lockout relative to pre-T-4047 behavior,
    never lengthen one. The lower clamp is defensive: the parser's own
    skew-grace rollover (`_MID_SESSION_CAP_RESET_SKEW_GRACE_SECONDS`)
    should already prevent a genuinely-past `reset_at`, but a cooldown
    before `now` would be a no-op cooldown (or worse, read as "already
    expired" by a caller) if it ever slipped through.
    """
    if failure_class != "subscription_cap" or not isinstance(exc, RuntimeOperationalFailure):
        return None
    weekly_ceiling = cap_reset_ceiling(harness, now=now)
    reset_at = _parse_iso_or_none(exc.readiness_evidence.get("reset_at"))
    if reset_at is not None:
        return max(min(reset_at, weekly_ceiling), now)
    cap_kind = exc.readiness_evidence.get("cap_kind") or ""
    if cap_kind in ("session-limit", "opus-limit", "unknown-limit"):
        candidate = now + timedelta(seconds=MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)
        return max(min(candidate, weekly_ceiling), now)
    return None


#: T-4158: the one harness whose Telegram receiver runs OUTSIDE its runtime (a
#: separate systemd user unit, Restart=always, not bound to the pane), so killing
#: or respawning its pane does NOT stop the receiver. Every other configured
#: comms harness (claude, `claude_receiver_topology: runtime-owned`) runs its
#: receiver inside the runtime process, so a pane respawn already ends it.
_OUT_OF_RUNTIME_RECEIVER_HARNESS = "codex"


def _prior_runtime_harnesses(
    binding: "PersonaBinding",
    *,
    live_argv: Sequence[str] | None,
    state_root: Path | None,
) -> dict[str, str]:
    """Harnesses of the runtime this start replaces, keyed by evidence source
    (T-4158). Two sources, both read-only:

      * ``record``: the runtime.json record's ``harness``. Every launcher that
        can start the out-of-runtime receiver (the consume-path bootstrap and
        the transition/replace paths) writes this record, and controller_main
        rewrites it on each launch (T-4035), so after a same-harness launch it
        names the selected harness and the next restart is a no-op.
      * ``live_pane``: a configured harness name matched by basename in the
        live foreign pane's argv (the respawn-over-foreign-pane branch), for a
        record left stale by a skipped T-4035 persist.
    """
    observed: dict[str, str] = {}
    recorded = _read_json(_persona_state(binding.persona, state_root) / "runtime.json").get("harness")
    if isinstance(recorded, str) and recorded:
        observed["record"] = recorded
    if live_argv:
        bases = {os.path.basename(str(arg)) for arg in live_argv}
        hits = sorted(name for name in harness_integration.configured_harness_names() if name in bases)
        if len(hits) == 1:
            observed["live_pane"] = hits[0]
    return observed


class _OutOfRuntimeReceiverRef(NamedTuple):
    """Read-only stand-in naming the out-of-runtime receiver of `binding`'s
    persona (same persona/account/communications, launch harness swapped to
    `_OUT_OF_RUNTIME_RECEIVER_HARNESS`), so the existing receiver-state proof
    (`_probe_managed_receiver`) can be asked about the codex poller while the
    selected binding is claude (T-4179). Works for PersonaBinding and the
    test SimpleNamespace bindings alike (no dataclasses.replace on the binding)."""

    persona: str
    account: Any
    communications: Any
    launch: Any


def _out_of_runtime_receiver_ref(binding: "PersonaBinding") -> _OutOfRuntimeReceiverRef:
    return _OutOfRuntimeReceiverRef(
        persona=binding.persona,
        account=getattr(binding, "account", None),
        communications=getattr(binding, "communications", None),
        launch=dataclasses.replace(binding.launch, harness=_OUT_OF_RUNTIME_RECEIVER_HARNESS),
    )


def _probe_out_of_runtime_receiver_absent(
    binding: "PersonaBinding", *, backend: Any | None
) -> _ManagedReceiverProbe:
    """T-4179: read-only PROBE evidence source. Asks the KEEP receiver-state
    proof whether the codex poller of `binding`'s persona is absent; the bridge
    proof scans unit state AND process cmdlines (independent of runtime.json,
    so it catches a poller whose record was cleared or drifted). ``ok`` means
    proven absent (or not applicable); anything else -- present, ambiguous, or
    an unreadable proof -- is NOT ok, and callers treat it as "may be present"."""
    return _probe_managed_receiver(_out_of_runtime_receiver_ref(binding), "absent", backend=backend)


#: T-4179: how a failed prior-receiver stop is surfaced, per launch path.
#:   "operational"       -- controller start path (T-4158): RuntimeOperationalFailure,
#:                          controller_main cools the candidate down and reselects.
#:   "viability-blocked" -- consume/ensure_runtime path: RuntimeViabilityBlocked
#:                          (exit-5 policy skip). NOT RuntimeOperationalFailure, which
#:                          the initial-viability loop would cascade to the codex
#:                          candidate (and compensate) -- starting ANOTHER poller path.
#:   "alert-only"        -- controller attach branch (claude already running):
#:                          alert + evidence, the attach proceeds.
PriorReceiverRefusal = Literal["operational", "viability-blocked", "alert-only"]


def _stop_prior_harness_receiver_before_start(
    binding: "PersonaBinding",
    *,
    live_argv: Sequence[str] | None,
    state_root: Path | None,
    backend: Any | None = None,
    probe: bool = False,
    refusal: PriorReceiverRefusal = "operational",
) -> ReceiverHookReceipt | None:
    """T-4158: when the controller is about to launch `binding` over a prior
    runtime (record or live pane) of the out-of-runtime-receiver harness, run
    the KEEP layer's pre-phase receiver handoff for that direction -- the SAME
    ``<prior>-to-<selected>`` pre step the consume path's compensation runs
    (see `run_initial_bootstrap_receiver_boundary`), which stops the prior
    harness's receiver unit and proves it absent (step names live only in
    tools/codex_telegram_actions.py; ReceiverHandoffTests forbids quoting them
    here). Without it, a consume-path codex bootstrap's poller keeps polling
    under the new claude pane: two getUpdates consumers on one bot, 409s, and
    comms-dark in both directions.

    Returns the receipt, or None when not applicable: non-comms persona
    (`receiver_handoff_required` false), selected harness is itself the
    out-of-runtime-receiver harness (codex over claude: T-4184's
    `_quiesce_prior_harness_receiver_before_start` owns that direction), or no
    prior evidence names that harness (a claude-to-claude restart).

    T-4179 ``probe=True`` adds a third evidence source, ``probe``: when neither
    the record nor the live pane names codex, a read-only receiver-state proof
    (`_probe_out_of_runtime_receiver_absent`) runs; if it cannot prove the
    poller absent, the same stop runs. Probe-first keeps the common path
    read-only (no systemctl stop, no watchdog-free handoff on every launch).
    ``refusal`` picks the failure shape per launch path (see
    `PriorReceiverRefusal`); the default is T-4158's controller shape below.

    FAIL-CLOSED: any handoff failure raises `RuntimeOperationalFailure`
    (operational-launch) BEFORE the pane is touched. controller_main's
    start-failure handler then cools the selected candidate down and exits
    (Restart=always), so the next cycle reselects the prior harness -- which,
    with its receiver still running, is a coherent one-poller seat -- and
    fallback-return retries the stop after the backoff. Launching anyway
    would guarantee two pollers, which the arc forbids.
    """
    if not receiver_handoff_required(binding):
        return None
    if binding.launch.harness == _OUT_OF_RUNTIME_RECEIVER_HARNESS:
        return None
    observed = _prior_runtime_harnesses(binding, live_argv=live_argv, state_root=state_root)
    sources = sorted(src for src, harness in observed.items() if harness == _OUT_OF_RUNTIME_RECEIVER_HARNESS)
    probe_fields: dict[str, str] = {}
    if not sources and probe:
        proof = _probe_out_of_runtime_receiver_absent(binding, backend=backend)
        if proof.ok:
            return None
        sources = ["probe"]
        probe_fields = {"probe_state": proof.state, "probe_reason": proof.reason[:500]}
    if not sources:
        return None
    direction = _prior_receiver_direction(binding)
    if direction == "none":
        return None
    evidence = {
        "selected": _binding_runtime_ref(binding),
        "prior_harness": _OUT_OF_RUNTIME_RECEIVER_HARNESS,
        "prior_evidence": ",".join(sources),
        "direction": direction,
        "phase": "pre",
        "refusal": refusal,
        **probe_fields,
    }
    try:
        receipt = _run_integration_handoff(
            binding, direction, "pre", backend=backend, reason="controller-start-over-prior-harness"
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        alert = _alert_prior_receiver_stop_failed(
            binding.persona,
            _binding_runtime_ref(binding),
            detail,
            state_root=state_root,
            launch_refused=refusal != "alert-only",
        )
        try:  # best-effort: the refusal below must ALWAYS propagate
            _append_controller_evidence(
                binding.persona,
                "controller_start_prior_receiver_stop_failed",
                {**evidence, "error": detail, **alert},
                state_root=state_root,
            )
        except Exception:  # noqa: BLE001
            pass
        if refusal == "alert-only":
            return None
        if refusal == "viability-blocked":
            raise _runtime_viability_block(
                "prior-receiver-not-proven-absent",
                binding=binding,
                runtime_state={},
                extra={**evidence, "error": detail, **alert},
                policy_skip_reason=PRIOR_RECEIVER_POLICY_SKIP_REASON,
            ) from exc
        raise RuntimeOperationalFailure(
            f"prior {_OUT_OF_RUNTIME_RECEIVER_HARNESS} receiver not proven absent; refusing launch: {detail}",
            failure_class="operational-launch",
            cause="prior_receiver_stop_failed",
            binding_runtime=_binding_runtime_ref(binding),
        ) from exc
    _append_controller_evidence(
        binding.persona,
        "controller_start_prior_receiver_stopped",
        {**evidence, "operations": ",".join(receipt.operations)},
        state_root=state_root,
    )
    return receipt


def _prior_receiver_direction(
    binding: "PersonaBinding", prior_harness: str = _OUT_OF_RUNTIME_RECEIVER_HARNESS
) -> str:
    """The ``<prior harness>-to-<selected>`` receiver direction for a launch of
    `binding` over a prior runtime of `prior_harness` (T-4158; shared with the
    T-4178 post phase so both halves use one direction). T-4184 generalizes the
    prior harness: the default (the out-of-runtime-receiver harness) keeps the
    T-4158/T-4178 callers unchanged; a codex launch passes the in-runtime one
    (`_in_runtime_receiver_harness`). The direction string itself is derived by
    the harness integration, never spelled here."""
    prior_launch = dataclasses.replace(binding.launch, harness=prior_harness)
    return _receiver_direction_for_bindings(_PriorRuntimeRef(launch=prior_launch), binding)


def _in_runtime_receiver_harness() -> str | None:
    """T-4184: "the other comms harness", i.e. the configured harness whose
    Telegram receiver runs INSIDE its runtime (today claude, runtime-owned):
    every configured harness minus `_OUT_OF_RUNTIME_RECEIVER_HARNESS` that
    carries a receiver (T-5011: `_harness_carries_receiver`). Exactly
    one, else None (the caller then does nothing and logs why): with two or
    more candidates there is no single handoff direction to run."""
    others = sorted(
        name
        for name in harness_integration.configured_harness_names() - {_OUT_OF_RUNTIME_RECEIVER_HARNESS}
        if _harness_carries_receiver(name)
    )
    return others[0] if len(others) == 1 else None


def _harness_carries_receiver(name: str) -> bool:
    """T-5011 PR-B: does configured harness `name` carry a Telegram receiver
    (its integration's ``communications_managed``, the flag
    `_receiver_direction_for_bindings` already gates on)? hermes is
    mailbox-only (False), so registering it leaves claude the one in-runtime
    candidate. A name with no resolvable integration counts as a candidate,
    so an unknown harness keeps the ambiguous logged no-op, never a guess."""
    try:
        integration = harness_integration.integration_for(name)
    except ValueError:
        return True
    return getattr(integration, "communications_managed", True) is not False


def _bridge_topology_snapshot_pending_subprocess(binding: Any, *, timeout_seconds: int = 30) -> bool:
    """Production probe for `_receiver_topology_snapshot_pending`: the KEEP
    bridge's read-only ``topology-snapshot-state`` command (T-4178). Raises on
    any failure; the caller decides the policy."""
    argv = (
        sys.executable,
        str(Path(__file__).resolve().parent / "codex_telegram_bridge.py"),
        "topology-snapshot-state",
        "--persona",
        str(getattr(binding, "persona")),
        "--json",
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout_seconds)
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("topology snapshot state returned invalid JSON") from exc
    if result.returncode != 0 or payload.get("ok") is not True or not isinstance(payload.get("pending"), bool):
        raise RuntimeError(str(payload.get("error") or "topology snapshot state probe failed"))
    return payload["pending"]


def _receiver_topology_snapshot_pending(binding: Any, *, backend: Any | None) -> bool:
    """T-4178: is there an INCOMPLETE receiver topology snapshot for `binding`
    (saved unit state from a claude-to-codex pre phase, still to be restored)?

    `backend` is the receiver-handoff test seam, as elsewhere in this module:
    production passes None and asks the KEEP bridge. A test backend that does
    not model the snapshot is treated as having none (False), so backends that
    predate T-4178 keep their exact event sequences."""
    if backend is not None:
        probe = getattr(backend, "receiver_topology_snapshot_pending", None)
        return bool(probe(binding)) if probe is not None else False
    return _bridge_topology_snapshot_pending_subprocess(binding)


#: T-4178: the production post phase's bridge deadline (its presence-probe
#: retries) vs the outer subprocess timeout. The inner one is clearly shorter,
#: so the outer kill never lands mid watchdog-restore.
PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS = 60
PRIOR_RECEIVER_POST_OUTER_TIMEOUT_SECONDS = 90


def _complete_prior_harness_receiver_after_start(
    binding: "PersonaBinding",
    pre_receipt: ReceiverHookReceipt | None,
    *,
    state_root: Path | None,
    backend: Any | None = None,
) -> ReceiverHookReceipt | None:
    """T-4178: the POST half of T-4158's codex-to-claude receiver handoff.

    Gate: a comms persona (`receiver_handoff_required`) starting a runtime
    that is NOT the out-of-runtime-receiver harness (i.e. claude), AND the
    bridge reports an INCOMPLETE receiver topology snapshot. That snapshot is
    written only by a claude-to-codex PRE phase (the consume path's
    planned/forced transition), which also disables the watchdog units (e.g.
    Recovery's recovery-telegram-child-watchdog.timer/.service). The post phase then
    proves the claude receiver present, restores the watchdog units from the
    snapshot and marks the snapshot complete (step names live only in
    tools/codex_telegram_actions.py).

    The gate deliberately does NOT require that T-4158's PRE ran for this
    launch (`pre_receipt` only decides whether a skip is logged). After a
    failed restore the record says claude, so the next claude-to-claude
    restart has no PRE; it must still retry, or the watchdog stays disabled
    indefinitely. The controller holds persona_operation_lock across this
    start, so a pending snapshot seen during a claude start can only mean an
    earlier post phase failed or never ran. Cost: one read-only pending probe
    per claude start of a comms persona. With no pending snapshot (the common
    case, including the initial-bootstrap codex path, which runs no
    claude-to-codex PRE) nothing runs: the bridge's post phase would refuse
    on a missing or completed snapshot, and there is nothing to restore.

    Production (backend None) runs the bridge post phase with an inner
    deadline (`PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS`) clearly shorter than
    the outer subprocess timeout, so the outer kill cannot land mid-restore.

    Called AFTER `_run_and_assess_post_start`, i.e. only once the new claude
    runtime is input-ready and has passed the post-hook cap assessment. A
    watchdog restored before that assessment would stay enabled if the
    assessment then swapped the seat back to codex (the controller's
    codex launch runs no claude-to-codex PRE), and the watchdog self-restarts
    a session whose claude child is absent.

    FAIL-OPEN, NEVER RAISES: claude is already running with its own receiver,
    so a failure here is degraded (watchdog still off, snapshot pending), not
    dark. Raising would make controller_main treat a live, healthy start as a
    start failure. A failure writes evidence (best-effort) and routes a
    cooldown-gated alert. Returns the post receipt, or None when skipped or
    failed."""
    direction = "none"
    evidence: dict[str, Any] = {}
    try:
        if not receiver_handoff_required(binding):
            return None
        if binding.launch.harness == _OUT_OF_RUNTIME_RECEIVER_HARNESS:
            return None
        direction = _prior_receiver_direction(binding)
        evidence = {
            "selected": _binding_runtime_ref(binding),
            "prior_harness": _OUT_OF_RUNTIME_RECEIVER_HARNESS,
            "direction": direction,
            "phase": "post",
            "pre_ran": pre_receipt is not None,
        }
        if direction == "none":
            return None
        if not _receiver_topology_snapshot_pending(binding, backend=backend):
            if pre_receipt is not None:
                _best_effort_controller_evidence(
                    binding.persona,
                    "controller_start_prior_receiver_post_skipped",
                    {**evidence, "reason": "no-pending-topology-snapshot"},
                    state_root=state_root,
                )
            return None
        reason = "controller-start-over-prior-harness:post"
        if backend is None:
            receipt = _run_bridge_handoff_subprocess(
                binding,
                direction,  # type: ignore[arg-type]
                "post",
                reason=reason,
                timeout_seconds=PRIOR_RECEIVER_POST_OUTER_TIMEOUT_SECONDS,
                inner_timeout_seconds=PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS,
            )
        else:
            receipt = _run_integration_handoff(binding, direction, "post", backend=backend, reason=reason)
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        selected_ref = str(evidence.get("selected") or getattr(binding.launch, "harness", ""))
        alert = _alert_prior_receiver_restore_failed(binding.persona, selected_ref, detail, state_root=state_root)
        _best_effort_controller_evidence(
            binding.persona,
            "controller_start_prior_receiver_post_failed",
            {**evidence, "error": detail, **alert},
            state_root=state_root,
        )
        return None
    _best_effort_controller_evidence(
        binding.persona,
        "controller_start_prior_receiver_restored",
        {**evidence, "operations": ",".join(receipt.operations)},
        state_root=state_root,
    )
    return receipt


def _best_effort_controller_evidence(
    persona: str, event: str, fields: Mapping[str, Any], *, state_root: Path | None
) -> None:
    """Append controller evidence, swallowing any error (T-4178's fail-open
    post phase must never raise out of a live start)."""
    try:
        _append_controller_evidence(persona, event, fields, state_root=state_root)
    except Exception:  # noqa: BLE001
        pass


#: T-4184: evidence/alert label for where a codex-side receiver step ran.
SelectedReceiverPath = Literal["start", "attach"]


class _NoUniqueInRuntimeHarness(Exception):
    """T-4184: see `_selected_out_of_runtime_receiver_gate`."""


def _selected_out_of_runtime_receiver_gate(binding: "PersonaBinding") -> tuple[str, str] | None:
    """T-4184 shared gate: a comms persona (`receiver_handoff_required`) whose
    SELECTED harness is the out-of-runtime-receiver harness (codex). Returns
    (prior in-runtime harness, direction), or None when not applicable. Raises
    `_NoUniqueInRuntimeHarness` when the prior harness is ambiguous, so the
    caller can log it (a no-op plus evidence, never a guess)."""
    if not receiver_handoff_required(binding):
        return None
    if binding.launch.harness != _OUT_OF_RUNTIME_RECEIVER_HARNESS:
        return None
    prior = _in_runtime_receiver_harness()
    if prior is None:
        raise _NoUniqueInRuntimeHarness(
            "configured harnesses do not name exactly one in-runtime receiver harness: "
            + ",".join(sorted(harness_integration.configured_harness_names()))
        )
    direction = _prior_receiver_direction(binding, prior)
    if direction == "none":
        return None
    return prior, direction


def _quiesce_prior_harness_receiver_before_start(
    binding: "PersonaBinding",
    *,
    live_argv: Sequence[str] | None,
    state_root: Path | None,
    backend: Any | None = None,
    path: SelectedReceiverPath = "start",
) -> ReceiverHookReceipt | None:
    """T-4184 (the mirror of T-4158): before the controller launches codex for
    a comms persona, run the KEEP layer's pre phase for the in-runtime-to-codex
    direction (snapshot the watchdog and dormant claude units, then disable and
    stop them; step names live only in tools/codex_telegram_actions.py) with
    ``prior_runtime="unproven"``, which drops the claude presence proof: the
    prior claude is typically dead or capped here, and that proof would burn
    its full deadline and fail. Without this PRE a codex seat keeps the claude
    watchdog armed (it restarts the session, then pages the operator).

    Gate: claude evidence (the runtime.json record or the live foreign pane,
    via `_prior_runtime_harnesses`) OR no pending topology snapshot. The only
    skip is codex over codex with a snapshot already pending: an earlier PRE
    already quiesced the claude side and its snapshot holds the original unit
    state. A pending-probe error runs the PRE anyway (it is idempotent, and the
    bridge's snapshot merge is first-wins, so a re-run never overwrites the
    original unit state with the disabled one).

    FAIL-OPEN, NEVER RAISES: evidence plus a cooldown-gated alert, then the
    launch proceeds. Refusing would leave the persona with no runtime at all
    (unlike T-4158, where refusing kept the prior codex seat); the one-poller
    guarantee comes from the POST phase's absence proof
    (`_start_selected_receiver_after_start`), not from this step. A failed PRE
    restores what it had snapshotted inside the same bridge handoff.

    ``path`` is "start" (before a launch) or "attach" (the attach-branch heal).
    Returns the receipt, or None when skipped or failed."""
    evidence: dict[str, Any] = {}
    try:
        try:
            gate = _selected_out_of_runtime_receiver_gate(binding)
        except _NoUniqueInRuntimeHarness as exc:
            _best_effort_controller_evidence(
                binding.persona,
                "controller_start_prior_receiver_quiesce_skipped",
                {"selected": _binding_runtime_ref(binding), "path": path, "reason": str(exc)[:500]},
                state_root=state_root,
            )
            return None
        if gate is None:
            return None
        prior, direction = gate
        observed = _prior_runtime_harnesses(binding, live_argv=live_argv, state_root=state_root)
        sources = sorted(src for src, harness in observed.items() if harness == prior)
        evidence = {
            "selected": _binding_runtime_ref(binding),
            "prior_harness": prior,
            "prior_evidence": ",".join(sources),
            "direction": direction,
            "phase": "pre",
            "prior_runtime": "unproven",
            "path": path,
        }
        if not sources:
            try:
                pending = _receiver_topology_snapshot_pending(binding, backend=backend)
            except Exception as exc:  # noqa: BLE001 -- run the idempotent PRE anyway
                evidence["snapshot_pending"] = f"probe-failed: {type(exc).__name__}: {exc}"[:500]
            else:
                evidence["snapshot_pending"] = pending
                if pending:
                    _best_effort_controller_evidence(
                        binding.persona,
                        "controller_start_prior_receiver_quiesce_skipped",
                        {**evidence, "reason": "snapshot-pending"},
                        state_root=state_root,
                    )
                    return None
        receipt = _run_integration_handoff(
            binding,
            direction,
            "pre",
            backend=backend,
            reason=f"controller-{path}-selected-over-prior-harness:quiesce",
            prior_runtime="unproven",
        )
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        alert = _alert_prior_receiver_quiesce_failed(
            binding.persona, _binding_runtime_ref(binding), detail, state_root=state_root
        )
        _best_effort_controller_evidence(
            binding.persona,
            "controller_start_prior_receiver_quiesce_failed",
            {**evidence, "path": path, "error": detail, **alert},
            state_root=state_root,
        )
        return None
    _best_effort_controller_evidence(
        binding.persona,
        "controller_start_prior_receiver_quiesced",
        {**evidence, "operations": ",".join(receipt.operations)},
        state_root=state_root,
    )
    return receipt


def _start_selected_receiver_after_start(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
    backend: Any | None = None,
    path: SelectedReceiverPath = "start",
) -> ReceiverHookReceipt | None:
    """T-4184: the POST half of the controller's in-runtime-to-codex handoff:
    prove the claude receiver absent, start the codex poller, prove it present,
    and check for sustained 409s (step names live only in
    tools/codex_telegram_actions.py). Runs on EVERY comms codex controller
    start (idempotent: starting an active unit is a no-op), so a seat entered
    by a cap swap-back after T-4158 stopped the poller gets it back. The
    claude-absence proof is what guarantees one poller.

    Called AFTER `_run_and_assess_post_start`, so it never starts a poller for
    a runtime that assessment is about to abandon.

    Production (backend None) runs the bridge post phase with an inner deadline
    (`PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS`) clearly shorter than the outer
    subprocess timeout, as T-4178 does.

    FAIL-OPEN, NEVER RAISES: codex is already up; raising would make
    controller_main treat a live start as a failure. A failure means the seat
    is Telegram-dark, so it writes evidence and routes a cooldown-gated alert;
    the next codex start (or attach heal) retries. Returns the receipt, or
    None when not applicable or failed."""
    evidence: dict[str, Any] = {}
    try:
        try:
            gate = _selected_out_of_runtime_receiver_gate(binding)
        except _NoUniqueInRuntimeHarness as exc:
            _best_effort_controller_evidence(
                binding.persona,
                "controller_start_selected_receiver_skipped",
                {"selected": _binding_runtime_ref(binding), "path": path, "reason": str(exc)[:500]},
                state_root=state_root,
            )
            return None
        if gate is None:
            return None
        prior, direction = gate
        evidence = {
            "selected": _binding_runtime_ref(binding),
            "prior_harness": prior,
            "direction": direction,
            "phase": "post",
            "path": path,
        }
        reason = f"controller-{path}-selected-over-prior-harness:post"
        if backend is None:
            receipt = _run_bridge_handoff_subprocess(
                binding,
                direction,
                "post",
                reason=reason,
                timeout_seconds=PRIOR_RECEIVER_POST_OUTER_TIMEOUT_SECONDS,
                inner_timeout_seconds=PRIOR_RECEIVER_POST_INNER_TIMEOUT_SECONDS,
            )
        else:
            receipt = _run_integration_handoff(binding, direction, "post", backend=backend, reason=reason)
    except Exception as exc:  # noqa: BLE001 -- fail-open by contract
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        alert = _alert_selected_receiver_start_failed(
            binding.persona,
            _binding_runtime_ref(binding),
            detail,
            state_root=state_root,
            poller_running=HEALTH_409_ERROR_PREFIX in detail,
        )
        _best_effort_controller_evidence(
            binding.persona,
            "controller_start_selected_receiver_start_failed",
            {**evidence, "path": path, "error": detail, **alert},
            state_root=state_root,
        )
        return None
    _best_effort_controller_evidence(
        binding.persona,
        "controller_start_selected_receiver_started",
        {**evidence, "operations": ",".join(receipt.operations)},
        state_root=state_root,
    )
    return receipt


def _heal_selected_receiver_on_attach(
    binding: "PersonaBinding",
    *,
    live_argv: Sequence[str] | None,
    state_root: Path | None,
    backend: Any | None = None,
) -> None:
    """T-4184 attach-branch heal (the mirror of T-4179's): the controller is
    attaching to a live codex pane of a comms persona (e.g. one a consume path
    launched). Quiesce the claude side (same gate as the start path: skipped
    while a snapshot is pending and no claude evidence exists), then run the
    POST only if the codex receiver is not proven present, so a healthy seat
    costs two read-only probes. Alert-only, never refuses the attach, never
    raises (both steps are fail-open)."""
    try:
        if _selected_out_of_runtime_receiver_gate(binding) is None:
            return
    except _NoUniqueInRuntimeHarness:
        pass  # each step below logs the ambiguity itself
    _quiesce_prior_harness_receiver_before_start(
        binding, live_argv=live_argv, state_root=state_root, backend=backend, path="attach"
    )
    try:
        present = _probe_managed_receiver(binding, "present", backend=backend).ok
    except Exception:  # noqa: BLE001 -- an unreadable proof is "not present"
        present = False
    if present:
        return
    _start_selected_receiver_after_start(binding, state_root=state_root, backend=backend, path="attach")


#: T-4158: at most one prior-receiver refusal alert per persona per window. The
#: refusal repeats on every rung of the transient backoff ladder (and on every
#: fallback-return retry), so without a cooldown each retry would page again.
PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS = 6 * 3600


def _route_failure_alert(agent: str, msg: str, vocab: str) -> None:
    """Seam over the fleet's failure-alert chokepoint (inbox_consumer), imported
    lazily as the other monitors do. For agent ``recovery`` it takes the
    recovery-self carve-out straight to the operator, which is right here: a Recovery seat
    flapping off claude cannot triage its own alert."""
    try:
        from tools.inbox_consumer import route_failure_alert
    except ImportError:  # pragma: no cover - script-dir invocation
        from inbox_consumer import route_failure_alert  # type: ignore[no-redef]
    route_failure_alert(agent, msg, vocab)


def _alert_prior_receiver_stop_failed(
    persona: str,
    selected_ref: str,
    detail: str,
    *,
    state_root: Path | None,
    now: datetime | None = None,
    launch_refused: bool = True,
) -> dict[str, Any]:
    """Page once per `PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS` per persona when
    the controller refuses a launch because the prior codex receiver was not
    proven absent (T-4158). The controller unit never fails on this path, so
    OnFailure= never fires; this is the only signal.

    Wholly best-effort and NEVER raises: the caller must always get to raise
    its refusal (RuntimeOperationalFailure, or T-4179's RuntimeViabilityBlocked;
    ``launch_refused=False`` words it for the attach-branch heal, where nothing
    was refused) (a crash here would skip controller_main's
    attempt record, so no backoff, and a page every restart).

    Delivery is NOT confirmed: `route_failure_alert` itself swallows network,
    token and non-2xx Telegram failures and returns normally. So `alert_routed`
    means "routing was attempted without raising", and the cooldown is stamped
    after any such attempt; only a route that raises leaves it unstamped (and
    so retried on the next refusal).

    Returns evidence fields: ``alert_routed`` (bool), ``alert_suppressed``
    ("cooldown" or ""), ``alert_stamp`` ("written", "not-written" or
    "failed: <error>")."""
    if launch_refused:
        msg = (
            f"runtime-controller {persona}: refused to launch {selected_ref} over a prior codex "
            f"runtime because its Telegram poller was not proven absent ({detail}). The seat stays "
            f"on the prior harness; {selected_ref} backs off and retries. Two pollers were avoided, "
            f"but return-to-{selected_ref} is blocked until the poller stop succeeds."
        )
    else:
        # T-4179 attach-branch heal: claude is ALREADY running, so nothing was refused.
        msg = (
            f"runtime-controller {persona}: a codex Telegram poller may be running alongside the "
            f"live {selected_ref} runtime and stopping it failed ({detail}). Two pollers on one bot "
            f"(409s, comms-dark both ways) until the poller stop succeeds."
        )
    return _route_cooldown_gated_alert(
        persona,
        msg,
        f"controller-prior-receiver ({persona})",
        stamp_name="prior-receiver-alert.json",
        state_root=state_root,
        now=now,
    )


def _alert_prior_receiver_restore_failed(
    persona: str,
    selected_ref: str,
    detail: str,
    *,
    state_root: Path | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """T-4178: page once per `PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS` per persona
    when the post-launch codex-to-claude receiver completion fails (fail-open:
    the new runtime keeps running). A separate stamp from the T-4158 refusal
    alert, so one never suppresses the other. Same never-raises contract and
    return fields as `_alert_prior_receiver_stop_failed`."""
    msg = (
        f"runtime-controller {persona}: {selected_ref} is up over a prior codex runtime, but "
        f"the post-launch Telegram receiver completion failed ({detail}). Comms are degraded, "
        f"not dark: watchdog units disabled by an earlier claude-to-codex swap may still be "
        f"disabled, and the receiver topology snapshot is still pending. The runtime was left "
        f"running; the completion is retried on each {selected_ref} start while the snapshot "
        f"is pending."
    )
    return _route_cooldown_gated_alert(
        persona,
        msg,
        f"controller-prior-receiver-restore ({persona})",
        stamp_name="prior-receiver-restore-alert.json",
        state_root=state_root,
        now=now,
    )


def _alert_prior_receiver_quiesce_failed(
    persona: str,
    selected_ref: str,
    detail: str,
    *,
    state_root: Path | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """T-4184: page once per `PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS` per persona
    when the pre-launch quiesce of the claude Telegram side fails (fail-open:
    codex is launched anyway). Own stamp. Same never-raises contract and return
    fields as `_alert_prior_receiver_stop_failed`."""
    msg = (
        f"runtime-controller {persona}: launching {selected_ref} over a prior claude runtime, but "
        f"quiescing the claude Telegram side failed ({detail}). The launch proceeds: the claude "
        f"watchdog units may still be enabled and the receiver topology snapshot may be "
        f"missing, so a later return to claude may not restore them. The quiesce is retried on "
        f"the next {selected_ref} start."
    )
    return _route_cooldown_gated_alert(
        persona,
        msg,
        f"controller-prior-receiver-quiesce ({persona})",
        stamp_name="prior-receiver-quiesce-alert.json",
        state_root=state_root,
        now=now,
    )


def _alert_selected_receiver_start_failed(
    persona: str,
    selected_ref: str,
    detail: str,
    *,
    state_root: Path | None,
    now: datetime | None = None,
    poller_running: bool = False,
) -> dict[str, Any]:
    """T-4184: page once per `PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS` per persona
    when the post-launch codex poller start fails (fail-open: codex keeps
    running). Own stamp. Same never-raises contract and return fields as
    `_alert_prior_receiver_stop_failed`. ``poller_running`` (the failure was
    the 409 health step, i.e. the poller was started and proven present) words
    it as a conflict, not as a dark seat."""
    if poller_running:
        msg = (
            f"runtime-controller {persona}: codex poller running but 409 conflicts detected. "
            f"{selected_ref} is running and its Telegram poller is up, but it logged sustained "
            f"getUpdates 409 Conflicts since its start ({detail}): another consumer may be "
            f"polling the same bot. Rechecked on each {selected_ref} start or attach."
        )
    else:
        msg = (
            f"runtime-controller {persona}: codex up, Telegram DARK. {selected_ref} is running, but "
            f"starting and proving its Telegram poller failed ({detail}). The runtime was left "
            f"running; the poller start is retried on each {selected_ref} start or attach."
        )
    return _route_cooldown_gated_alert(
        persona,
        msg,
        f"controller-selected-receiver ({persona})",
        stamp_name="prior-receiver-poller-alert.json",
        state_root=state_root,
        now=now,
    )


def _prepare_codex_mcp_startup_watch(
    binding: "PersonaBinding", *, context: TmuxRuntimeContext | None = None
) -> int:
    """T-5023, run just before a codex launch: clear a pane that is about to be
    replaced, then return how many MCP-interrupted lines it still shows (the
    watch's baseline; 0 normally). Non-codex bindings: 0, and no tmux call.
    Never raises; a failure here only means a 0 baseline."""
    if binding.launch.harness != "codex":
        return 0
    target = binding.controller.tmux
    try:
        if _target_known(target, context=context):
            _clear_runtime_pane_for_launch(target, context=context)
        text = _capture_runtime_pane_history(target, context=context)
    except Exception:  # noqa: BLE001 -- the watch is best-effort
        return 0
    return len(CODEX_MCP_STARTUP_INTERRUPTED_RE.findall(text)) if text else 0


def _alert_codex_mcp_startup_interrupted(
    persona: str, servers: str, relaunches: int, *, state_root: Path | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    msg = (
        f"runtime-controller {persona}: codex MCP startup interrupted again after "
        f"{relaunches} relaunch(es); servers not initialized: {servers}. The runtime was "
        f"left running WITHOUT those tools (e.g. no Telegram reply or sticker). Restart "
        f"the seat to retry. Evidence: controller-evidence.jsonl codex_mcp_startup_* (T-5023)."
    )
    return _route_cooldown_gated_alert(
        persona,
        msg,
        f"controller-codex-mcp-startup ({persona})",
        stamp_name="codex-mcp-startup-alert.json",
        state_root=state_root,
        now=now,
    )


def _watch_codex_mcp_startup_and_relaunch(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    runtime_start: RuntimeStart,
    *,
    launched_at: float,
    baseline: int,
    state_root: Path | None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RuntimeStart:
    """T-5023: after a codex start, watch the pane until `launched_at` +
    CODEX_MCP_STARTUP_WATCH_SECONDS for codex's "MCP startup interrupted" line.
    On a hit, relaunch (respawn) at most CODEX_MCP_STARTUP_MAX_RELAUNCHES times;
    if it still fails, alert and leave the runtime running. Returns the
    RuntimeStart of the runtime that is now live. A relaunch that fails its
    start checks raises as any start does (start-failure -> cooldown ->
    reselect), so the loop is bounded either way. No-op for non-codex."""
    if binding.launch.harness != "codex":
        return runtime_start
    persona = binding.persona
    target = binding.controller.tmux
    relaunches = 0
    while True:
        servers = _wait_for_codex_mcp_startup_interrupt(
            target,
            baseline=baseline,
            deadline=launched_at + CODEX_MCP_STARTUP_WATCH_SECONDS,
            sleep=sleep,
            monotonic=monotonic,
        )
        if servers is None:
            if relaunches:
                _best_effort_controller_evidence(
                    persona, "codex_mcp_startup_recovered",
                    {"relaunches": relaunches, "tmux_target": _target_text(target)},
                    state_root=state_root,
                )
            return runtime_start
        will_relaunch = relaunches < CODEX_MCP_STARTUP_MAX_RELAUNCHES
        _best_effort_controller_evidence(
            persona, "codex_mcp_startup_interrupted",
            {
                "servers": servers[:500],
                "relaunches": relaunches,
                "will_relaunch": will_relaunch,
                "tmux_target": _target_text(target),
            },
            state_root=state_root,
        )
        if not will_relaunch:
            alert = _alert_codex_mcp_startup_interrupted(
                persona, servers, relaunches, state_root=state_root
            )
            _best_effort_controller_evidence(
                persona, "codex_mcp_startup_alert", alert, state_root=state_root
            )
            return runtime_start
        relaunches += 1
        baseline = _prepare_codex_mcp_startup_watch(binding)
        launched_at = monotonic()
        runtime_start = _start_runtime(binding, adapter, replace=True, state_root=state_root)
        _verify_started_runtime(binding, runtime_start, state_root=state_root)
        _verify_runtime_input_ready(binding, runtime_start, state_root=state_root, adapter=adapter)
        _run_and_assess_post_start(binding, runtime_start, adapter, state_root=state_root)


def _route_cooldown_gated_alert(
    persona: str,
    msg: str,
    vocab: str,
    *,
    stamp_name: str,
    state_root: Path | None,
    now: datetime | None,
) -> dict[str, Any]:
    """Shared body of the prior-receiver alerts (T-4158, generalized by T-4178):
    route at most once per `PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS` per persona
    per `stamp_name`. Never raises; see `_alert_prior_receiver_stop_failed`."""
    result: dict[str, Any] = {"alert_routed": False, "alert_suppressed": "", "alert_stamp": "not-written"}
    try:
        current = now or _utc_now()
        path = _persona_state(persona, state_root) / stamp_name
        last = _parse_iso_or_none(_read_json(path).get("last_alert_at"))
        if last is not None and (current - last).total_seconds() < PRIOR_RECEIVER_ALERT_COOLDOWN_SECONDS:
            result["alert_suppressed"] = "cooldown"
            return result
        _route_failure_alert(persona, msg[:1500], vocab)
        result["alert_routed"] = True
        try:
            _atomic_json(path, {"last_alert_at": _iso(current)})
            result["alert_stamp"] = "written"
        except Exception as exc:  # noqa: BLE001 -- stamp is best-effort
            result["alert_stamp"] = f"failed: {type(exc).__name__}: {exc}"[:500]
    except Exception as exc:  # noqa: BLE001 -- alerting is best-effort
        try:
            print(f"[runtime_controller] {persona}: prior-receiver alert failed: {exc!r}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return result


#: T-4804: upper bound on how long a swap notice may hold the controller.
SWAP_NOTICE_TIMEOUT_SECONDS = 20.0


def _notify_runtime_swap(
    persona: str,
    runtime_ref: str,
    kind: str,
    event_key: str,
    msg: str,
    *,
    state_root: Path | None,
    timeout_seconds: float | None = None,
) -> None:
    """T-4804 clause 3: one operator line per deliberate controller swap.

    Called once per swap event (cap-swap / fallback-return outcome, or a start-up
    cap), never per poll. The failure-alert chokepoint throttles its operator-facing
    "[recovery routed]" ping per VOCAB for `ROUTED_AUDIT_COOLDOWN_SECONDS` (6h) and
    backs a Recovery-self failure off 1h/4h/daily, also per vocab. A fixed vocab would
    therefore collapse a cap-swap and its return inside one window into one line, so
    the vocab is unique per event: kind + runtime + `event_key` (the reset instant
    for a cap, the event time for a return). Cost: one small key in each throttle
    state file per swap event; events are hours apart per seat, so growth is a few
    dozen bytes a day at worst and is not per-poll.

    A MUTED persona stays silent by design (`route_failure_alert` suppresses it).
    The send runs in a daemon thread bounded by `timeout_seconds`, so a stuck pane
    send can delay the caller by at most that long. Never raises. Evidence row
    records the attempt.
    """
    vocab = f"controller-swap ({persona}) {kind} {runtime_ref} {event_key}"
    outcome: dict[str, bool] = {}

    def send() -> None:
        try:
            _route_failure_alert(persona, msg[:1500], vocab)
            outcome["routed"] = True
        except Exception as exc:  # noqa: BLE001 -- alerting is best-effort
            try:
                print(f"[runtime_controller] {persona}: swap notice failed: {exc!r}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass

    try:
        worker = threading.Thread(target=send, name="swap-notice", daemon=True)
        worker.start()
        worker.join(SWAP_NOTICE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds)
    except Exception:  # noqa: BLE001
        pass
    try:
        _append_controller_evidence(
            persona, "controller_swap_notified",
            {"kind": kind, "runtime": runtime_ref, "event": event_key,
             "routed": str(outcome.get("routed", False)).lower()},
            state_root=state_root,
        )
    except Exception:  # noqa: BLE001
        pass


class _PriorRuntimeRef(NamedTuple):
    """Minimal `launch`-carrying stand-in for the runtime being replaced, so
    `_receiver_direction_for_bindings` derives the direction (T-4158)."""

    launch: Any


def _controller_start_runtime(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
    receiver_backend: Any | None = None,
) -> tuple[RuntimeStart, RuntimeHandle]:
    """Start `binding`'s runtime for the stateless controller loop, reusing
    the KEEP low-level primitives directly (see module-header DECISION).

    `receiver_backend` (T-4158) is the receiver-handoff test seam; production
    passes None, which runs the KEEP bridge subprocess."""
    from tools.persona_bringup import build_adapter

    # ORG-106 L3a: this path skips ensure_runtime's chokepoint (selection is its only
    # gate), so refuse a deep-sleeping seat here, before the attach branch too.
    _refuse_sleeping_seat_launch(binding, state_root=state_root, observe_oom=True)
    # T-4101 TOCTOU + identity re-check (constraint 4). controller_main holds the
    # persona_operation_lock across this whole call, so the consume/wake path
    # (which holds the SAME lock across its own start + ~90s provider-completion
    # probe) cannot be mid-launch here. But it may have JUST finished and released
    # the lock, leaving a live runtime in this pane. If that runtime is the
    # SELECTED one, respawn-pane -k would needlessly kill a freshly-proven session
    # (and drop the persona's in-flight context) -- the race this ticket closes --
    # so we ATTACH instead (observe its pid, return a handle; controller_main then
    # supervises it via block_until_death, touching the pane no further).
    #
    # REBOUND-1 F1 (BLOCKING): `_runtime_alive` is harness-BLIND and the
    # per-persona tmux target is harness-INDEPENDENT, so a live pane may be running
    # a DIFFERENT harness than `selected` (e.g. consume started codex while claude
    # was capped; claude's cooldown then expired and this cycle selected claude).
    # Attaching there would record the codex pane's pid under claude/anthropic-code
    # and never transition -- corrupting attempt stats + feeding a wrong harness to
    # the continuity/health monitors (the T-4023/T-4035 stale-record class). So we
    # attach ONLY when the live pane's argv confirms it is the SELECTED binding;
    # on a mismatch (or an unreadable identity) we fall through to respawn -- the
    # pre-fix correct behavior (the controller SHOULD replace a wrong-harness
    # runtime with the selected one). The check-and-(attach|respawn) is atomic
    # under the caller's lock.
    if _runtime_alive(binding):
        pid = _pane_process_pid(binding.controller.tmux)
        argv = _read_pane_cmdline(pid) if pid is not None else None
        if pid is not None and argv and _pane_argv_matches_binding(argv, binding):
            # T-4179 sticky heal: a consume-path launch may have left the codex
            # poller running under this (selected, already live) claude pane.
            # Probe it and stop it; never refuse the attach (claude is up), a
            # failed stop only alerts. No-op for non-comms/codex bindings.
            _stop_prior_harness_receiver_before_start(
                binding,
                live_argv=argv,
                state_root=state_root,
                backend=receiver_backend,
                probe=True,
                refusal="alert-only",
            )
            # T-4184 mirror heal: attaching to a live codex pane of a comms
            # persona quiesces the claude side and starts the codex poller if
            # it is not proven present. Alert-only; no-op for claude bindings.
            _heal_selected_receiver_on_attach(
                binding, live_argv=argv, state_root=state_root, backend=receiver_backend
            )
            _append_controller_evidence(
                binding.persona,
                "controller_start_attached_live_runtime",
                {
                    "harness": binding.launch.harness,
                    "profile": binding.launch.profile,
                    "tmux_target": _target_text(binding.controller.tmux),
                    "runtime_scope_unit": TMUX_RUNTIME_SCOPE_UNIT,
                    "pid": pid,
                },
                state_root=state_root,
            )
            handle = RuntimeHandle(
                profile=binding.launch,
                tmux=binding.controller.tmux,
                session_id=None,
                started_at=_utc_now(),
                pid=pid,
            )
            return RuntimeStart(scope_unit=TMUX_RUNTIME_SCOPE_UNIT, created_scope=False), handle
        # A live pane that is NOT the selected binding (foreign harness/model, or
        # an unreadable /proc identity). Do NOT mislabel-attach it -- respawn the
        # selected runtime over it (the pre-fix behavior). Logged so live-verify
        # can distinguish this from a clean attach.
        _append_controller_evidence(
            binding.persona,
            "controller_start_respawn_over_foreign_pane",
            {
                "selected": _binding_runtime_ref(binding),
                "tmux_target": _target_text(binding.controller.tmux),
                "live_pane_pid": pid if pid is not None else "",
                "live_pane_argv0": (argv[0] if argv else ""),
            },
            state_root=state_root,
        )
    else:
        argv = None
    # T-4158: before launching over a prior runtime of ANOTHER harness, stop that
    # harness's out-of-runtime Telegram receiver and prove it absent (fail-closed:
    # raises before the pane is touched). No-op for non-comms personas, same-
    # harness restarts, and the attach branch above (which returned already;
    # T-4179: it heals via its own alert-only call). probe=True also catches a
    # poller whose record was cleared or drifted (T-4179). The receipt feeds
    # T-4178's POST phase below.
    prior_receiver_pre = _stop_prior_harness_receiver_before_start(
        binding, live_argv=argv, state_root=state_root, backend=receiver_backend, probe=True
    )
    # T-4184, the mirror for a codex selection: quiesce the claude Telegram side
    # (watchdog + dormant claude units, snapshotted) before the launch. Fail-open,
    # never raises; no-op for non-comms and non-codex bindings, and for codex
    # over codex while a snapshot is already pending.
    _quiesce_prior_harness_receiver_before_start(
        binding, live_argv=argv, state_root=state_root, backend=receiver_backend
    )
    adapter = build_adapter(binding, harness_home=binding.account_home)
    # T-5023: codex only (no-op otherwise). Clear the pane being replaced and
    # note the launch time; the MCP-startup watch below reads both.
    mcp_watch_baseline = _prepare_codex_mcp_startup_watch(binding)
    mcp_watch_launched_at = time.monotonic()
    runtime_start = _start_runtime(
        binding, adapter, replace=_target_known(binding.controller.tmux), state_root=state_root
    )
    _verify_started_runtime(binding, runtime_start, state_root=state_root)
    _verify_runtime_input_ready(binding, runtime_start, state_root=state_root, adapter=adapter)
    # T-4156: a real start (fresh, or respawn over a foreign pane) gets the
    # binding's post_start duties exactly as a consume-path bootstrap does
    # (arm_pane_log, Telegram 409 re-assert, boot kick). Shared helper, same
    # policy: commands fail-open; a post-hook subscription-cap assessment
    # raises RuntimeOperationalFailure, which controller_main's start-failure
    # handler already classifies as subscription_cap -> cooldown -> exit ->
    # Restart=always reselect (the swap). The ATTACH branch above returns
    # before this point on purpose: it started nothing (T-4101 owns it).
    _run_and_assess_post_start(binding, runtime_start, adapter, state_root=state_root)
    # T-4178: the POST half of the T-4158 handoff (restore watchdog units a
    # claude-to-codex transition disabled; complete the topology snapshot).
    # After the post_start cap assessment on purpose: a watchdog is restored
    # only once this runtime is committed. Fail-open, never raises; no-op
    # unless this is a comms persona's claude start with a pending snapshot
    # (whether or not the PRE step above ran, so a failed restore is retried).
    _complete_prior_harness_receiver_after_start(
        binding, prior_receiver_pre, state_root=state_root, backend=receiver_backend
    )
    # T-4184: the POST half for a codex selection: prove claude's receiver
    # absent, start + prove the codex poller. After the post_start assessment on
    # purpose (never for a runtime about to be abandoned). Every comms codex
    # start, idempotent; fail-open, never raises.
    _start_selected_receiver_after_start(binding, state_root=state_root, backend=receiver_backend)
    # T-5023: codex can come up with its MCP servers (telegram, sticker)
    # uninitialised and nothing else notices. Watch the rest of the window from
    # launch, relaunch once on a hit, then alert. Last on purpose: the receiver
    # is a separate unit a relaunch does not touch, so it starts without waiting.
    runtime_start = _watch_codex_mcp_startup_and_relaunch(
        binding, adapter, runtime_start,
        launched_at=mcp_watch_launched_at, baseline=mcp_watch_baseline, state_root=state_root,
    )
    started = _utc_now()
    # T-2966 REBOUND finding #2: capture the pane's process pid at the
    # moment we know the runtime is up + ready. This is a point-in-time
    # snapshot for the active-runtime.json cache's pid field only --
    # `_runtime_alive` never trusts it, it re-queries fresh every check.
    pid = _pane_process_pid(binding.controller.tmux)
    handle = RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=started,
        pid=pid,
    )
    return runtime_start, handle


# T-5168: after T-4347, pane_exists raises on a tmux error it does not recognise
# (reading it as dead risked respawning over a live session). Unguarded here, that
# raise left controller_main: exit 1, Restart=always RestartSec=5, and a PERSISTENT
# error (a permission-denied socket) tripped StartLimitBurst=20/600s in ~100s and
# failed the unit, leaving the pane unsupervised. So the supervise loop reads an
# error as UNKNOWN = still alive, records the transition into and out of unknown,
# and escalates once after this many consecutive unknown reads (~60s at 5s ticks).
RUNTIME_ALIVE_UNKNOWN_ESCALATE_READS = 12


def _unknown_reads_alive(
    binding: "PersonaBinding",
    check: Callable[[], bool],
    *,
    now_fn: Callable[[], datetime],
    state_root: Path | None,
) -> Callable[[], bool]:
    """Wrap a liveness check so a RuntimeError reads as alive (T-5168). Evidence is
    written on the transition into unknown, at escalation, and on recovery; never
    once per tick. The alert goes out once per unknown streak (and is cooldown-gated)."""
    streak = {"count": 0, "escalated": False}

    def is_alive() -> bool:
        try:
            alive = check()
        except RuntimeError as exc:
            streak["count"] += 1
            detail = f"{type(exc).__name__}: {exc}"[:500]
            if streak["count"] == 1:
                _best_effort_controller_evidence(
                    binding.persona, "runtime_liveness_unknown",
                    {"runtime": _binding_runtime_ref(binding), "error": detail}, state_root=state_root)
            if streak["count"] >= RUNTIME_ALIVE_UNKNOWN_ESCALATE_READS and not streak["escalated"]:
                streak["escalated"] = True
                alert = _route_cooldown_gated_alert(
                    binding.persona,
                    f"runtime-controller {binding.persona}: the liveness probe has failed "
                    f"{streak['count']} reads in a row ({detail}). The controller is treating the "
                    f"runtime as alive and still supervising, but cannot see it. Check tmux "
                    f"socket access for the seat account (T-5168).",
                    "runtime-liveness-unknown",
                    stamp_name="runtime-liveness-unknown-alert.json",
                    state_root=state_root,
                    now=now_fn(),
                )
                _best_effort_controller_evidence(
                    binding.persona, "runtime_liveness_unknown_escalated",
                    {"runtime": _binding_runtime_ref(binding), "reads": str(streak["count"]),
                     "error": detail, "alert_routed": str(alert.get("alert_routed")).lower()},
                    state_root=state_root)
            return True
        if streak["count"]:
            _best_effort_controller_evidence(
                binding.persona, "runtime_liveness_known_again",
                {"runtime": _binding_runtime_ref(binding), "unknown_reads": str(streak["count"]),
                 "alive": str(alive).lower()}, state_root=state_root)
            streak["count"], streak["escalated"] = 0, False
        return alive

    return is_alive


def block_until_tmux_death(
    binding: "PersonaBinding",
    *,
    poll_interval_seconds: float = 5.0,
    context: "TmuxRuntimeContext | None" = None,
    sleep: Callable[[float], None] = time.sleep,
    alive_check: Callable[[], bool] | None = None,
    capture_pane_fn: Callable[[], str] | None = None,
    cap_check_interval_seconds: float = MID_SESSION_CAP_POLL_INTERVAL_SECONDS,
    now: Callable[[], datetime] | None = None,
    state_root: Path | None = None,
    candidates: Sequence["PersonaBinding"] | None = None,
    holds: Mapping[str, Any] | None = None,
    terminate_runtime_fn: Callable[..., Any] | None = None,
    terminate_confirm_timeout_seconds: float = MID_SESSION_CAP_TERMINATE_CONFIRM_TIMEOUT_SECONDS,
    fallback_return_check_interval_seconds: float = FALLBACK_RETURN_CHECK_INTERVAL_SECONDS,
    fallback_return_idle_max_wait_seconds: float = FALLBACK_RETURN_IDLE_MAX_WAIT_SECONDS,
    operator_swap_check_interval_seconds: float = OPERATOR_SWAP_CHECK_INTERVAL_SECONDS,
    usability_probe_fn: Callable[["PersonaBinding", datetime | None], Any] | None = None,
    handoff_first_fn: Callable[["PersonaBinding"], bool] | None = None,
    handoff_retry_interval_seconds: float = FALLBACK_RETURN_HANDOFF_RETRY_SECONDS,
    manifest_path: Path | None = None,
    seat_sleep_fn: Callable[[], "SeatSleepOutcome | None"] | None = None,
    seat_sleep_check_interval_seconds: float = SEAT_SLEEP_CHECK_INTERVAL_SECONDS,
) -> "MidSessionCapSwapOutcome | FallbackReturnOutcome | OperatorSwapOutcome | SeatSleepOutcome | None":
    """Spec SS Controller invocation flow, step 4: block as MainPID until
    the started runtime's tmux session dies. `alive_check`/`sleep` are
    test seams (consistent with the file's existing injectable-dependency
    convention -- `systemd_probe`, `context`, `backend`).

    T-4037: when `capture_pane_fn` is given, this ALSO periodically inspects
    the pane for a mid-session subscription-cap modal (cadence
    `cap_check_interval_seconds`, decoupled from `poll_interval_seconds` --
    pane capture is cheap but not free). `capture_pane_fn=None` (every caller
    before this ticket, and both tests above) skips detection entirely --
    byte-identical to the pre-T-4037 function. Detection+publish is
    unconditional whenever `capture_pane_fn` is given; the SWAP action (kill
    the runtime, return a `MidSessionCapSwapOutcome`) additionally requires
    `_mid_session_cap_swap_eligible` (flag + comms receiver readiness + fallback
    viability, needing `candidates`/`holds`).

    HO amendment A (T-4037): after a swap attempt, this waits up to
    `terminate_confirm_timeout_seconds` for `is_alive()` (the SAME liveness
    check the outer loop uses) to go False. SIGTERM is a request, not a
    guarantee -- if the pane is still alive at the deadline, NO swap outcome
    is returned (so the caller never records subscription_cap on a runtime
    that's still running); the loop simply continues and the next cap-check
    cycle will try again. This landing never escalates past SIGTERM/this one
    bounded wait -- no SIGKILL.

    T-4046 (fallback-return, independent of the T-4037 cap-check above --
    either, both, or neither may be active on a given call, gated by their
    own opt-in flags):

      * ARMING: every `fallback_return_check_interval_seconds` (decoupled
        from `poll_interval_seconds` for the same "not every liveness tick"
        reason as the cap-check cadence), once `binding` has been alive at
        least `FALLBACK_RETURN_MIN_DWELL_SECONDS` (measured from this
        function's own entry -- the anti-flap floor), re-derive
        `_fallback_return_eligible`. A hit arms a target + a bounded
        idle-wait deadline.
      * ARMED: checked on EVERY liveness tick (idle windows can be narrow;
        mirrors `_wait_for_runtime_input_ready`'s own every-tick pane poll
        for the same reason), eligibility is RE-DERIVED FRESH first --
        closing the window where the trigger condition evaporates between
        arming and acting (cheap relative to the pane capture below, which
        already happens every armed tick regardless). A miss disarms
        without acting. A hit checks `_pane_appears_idle`; either idle OR
        the `fallback_return_idle_max_wait_seconds` bound being reached
        fires the SAME SIGTERM/bounded-confirm-death/no-SIGKILL shape as
        the cap-check swap above, returning a `FallbackReturnOutcome`
        instead of a `MidSessionCapSwapOutcome` -- controller_main routes
        that to a SUCCESS record on the ended runtime, never a failure (a
        planned failback is not its fault).

    T-5069 (handoff-first): when `handoff_first_fn` is given, the fallback-return
    asks the live session for a fresh HANDOFF before it SIGTERMs it. The fn
    returns True when it is safe to kill (handoff confirmed, or the existing
    one is newer than the session start); False defers, and the attempt is
    retried no sooner than `handoff_retry_interval_seconds` later. After a
    confirmed handoff the pane is re-checked on the next tick: it must be idle
    again, or `fallback_return_idle_max_wait_seconds` must have elapsed (past
    that bound the kill is unconditional, as before). `None` (the
    default) skips the step, byte-identical to the pre-T-5069 function.

    T-4469 (operator swap): every `operator_swap_check_interval_seconds`,
    when `candidates` is given, a COMPLETED operator transition naming a
    different, unheld candidate that the pane now positively runs returns an
    `OperatorSwapOutcome`. The pane stays alive through such a swap (same tmux
    target), so without this the loop kept supervising a runtime that was no
    longer there. Checked before the fallback/cap logic, which would otherwise
    act on the wrong binding.
    """
    now_fn = now or _utc_now
    is_alive = _unknown_reads_alive(
        binding, alive_check or (lambda: _runtime_alive(binding, context=context)),
        now_fn=now_fn, state_root=state_root,
    )
    detect_cap = capture_pane_fn is not None
    loop_started_at = now_fn()
    last_cap_check = loop_started_at
    last_fallback_check = loop_started_at
    last_operator_check = loop_started_at
    last_sleep_check = loop_started_at
    own_ref = _binding_runtime_ref(binding)
    fallback_target: "PersonaBinding | None" = None
    fallback_deadline: datetime | None = None
    last_handoff_attempt: datetime | None = None
    handoff_confirmed = False

    while is_alive():
        sleep(poll_interval_seconds)
        current = now_fn()

        # ORG-106 L3b: a deep-sleep record (draining, or asleep next to this live session)
        # starts / resumes the drain. Checked first: nothing below may act on a sleeping seat.
        if seat_sleep_fn is not None and (
                current - last_sleep_check).total_seconds() >= seat_sleep_check_interval_seconds:
            last_sleep_check = current
            sleep_outcome = seat_sleep_fn()
            if sleep_outcome is not None:
                return sleep_outcome

        if candidates and (current - last_operator_check).total_seconds() >= operator_swap_check_interval_seconds:
            last_operator_check = current
            # Cheap reject first: the pane is read only when transition.json names
            # a different runtime.
            destination = _operator_transition_destination(binding.persona, candidates, state_root=state_root)
            if destination is not None and _binding_runtime_ref(destination) != own_ref:
                pinned = _operator_pinned_runtime(
                    binding.persona, candidates, holds=holds, state_root=state_root, context=context
                )
                if pinned is not None:
                    return OperatorSwapOutcome(destination_ref=_binding_runtime_ref(pinned))

        if fallback_target is not None:
            fallback_target = _fallback_return_eligible(
                binding, candidates=candidates, holds=holds, now=current, state_root=state_root,
            )
            if fallback_target is None:
                # The triggering condition evaporated (e.g. the target got
                # held, or re-entered cooldown) between arming and now --
                # disarm without acting; a later arming-check tick may
                # re-arm if it becomes eligible again.
                handoff_confirmed = False
                continue
            idle = capture_pane_fn is not None and _pane_appears_idle(capture_pane_fn)
            if (idle or current >= fallback_deadline) and handoff_first_fn is not None and not handoff_confirmed:
                if (
                    last_handoff_attempt is not None
                    and (current - last_handoff_attempt).total_seconds() < handoff_retry_interval_seconds
                ):
                    continue
                last_handoff_attempt = current
                if handoff_first_fn(binding):
                    # The handoff turn just ran: re-check the pane is idle on
                    # the next tick before ending the session.
                    handoff_confirmed = True
                else:
                    _append_controller_evidence(
                        binding.persona,
                        "fallback_return_deferred_no_handoff",
                        {"runtime": own_ref, "retry_after_seconds": handoff_retry_interval_seconds},
                        state_root=state_root,
                    )
                continue
            if idle or current >= fallback_deadline:
                terminate = terminate_runtime_fn or _terminate_runtime_pid
                terminate(binding, context=context)
                confirm_deadline = current + timedelta(seconds=terminate_confirm_timeout_seconds)
                while now_fn() < confirm_deadline and is_alive():
                    sleep(poll_interval_seconds)
                if is_alive():
                    # Fail safe, same shape as HO amendment A above: SIGTERM
                    # didn't take within the bounded window. Stay armed and
                    # retry on the next tick -- never escalate to SIGKILL.
                    continue
                return FallbackReturnOutcome(target_ref=_binding_runtime_ref(fallback_target))
            continue

        if detect_cap and (current - last_cap_check).total_seconds() >= cap_check_interval_seconds:
            last_cap_check = current
            observation = _observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=capture_pane_fn, now=current, state_root=state_root,
            )
            if (
                observation is not None
                and observation.modal_present
                and _mid_session_cap_swap_eligible(
                    binding, candidates=candidates, holds=holds, now=current, state_root=state_root,
                    usability_probe=usability_probe_fn, manifest_path=manifest_path,
                )
            ):
                terminate = terminate_runtime_fn or _terminate_runtime_pid
                terminate(binding, context=context)
                deadline = current + timedelta(seconds=terminate_confirm_timeout_seconds)
                while now_fn() < deadline and is_alive():
                    sleep(poll_interval_seconds)
                if not is_alive():
                    reset_at = observation.reset_at
                    return MidSessionCapSwapOutcome(
                        modal_kind=observation.modal_kind or "unknown-limit",
                        cooldown_until=reset_at
                        or (current + timedelta(seconds=MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)),
                        cooldown_source=observation.reset_source if reset_at else "conservative-default",
                    )
                # Fail safe (HO amendment A): SIGTERM didn't take within the
                # bounded window. No swap outcome, no subscription_cap
                # record -- fall through to the fallback arming-check below
                # (harmless no-op unless that opt-in is ALSO set) and resume
                # normal polling; the next cap-check cycle retries.

        if (current - last_fallback_check).total_seconds() >= fallback_return_check_interval_seconds:
            last_fallback_check = current
            if (current - loop_started_at).total_seconds() >= FALLBACK_RETURN_MIN_DWELL_SECONDS:
                target = _fallback_return_eligible(
                    binding, candidates=candidates, holds=holds, now=current, state_root=state_root,
                )
                if target is not None:
                    fallback_target = target
                    fallback_deadline = current + timedelta(seconds=fallback_return_idle_max_wait_seconds)
    return None


def _production_usability_probe(state_root: Path | None) -> Callable[["PersonaBinding", datetime | None], Any]:
    """T-4862: live probe for the restart loop / eligibility. Free tiers only
    (allow_spend=False): the controller loop never spends a subscription turn."""

    def probe(binding: "PersonaBinding", hint: datetime | None) -> Any:
        from tools import subscription_usability

        return subscription_usability.probe_usability(
            binding, state_root=state_root, now=_utc_now(), allow_spend=False, hint=hint
        )

    return probe


def _dbus_emit_health(persona: str, health_service: Any) -> None:
    """Real ORG-068 SS92 `PropertiesChanged` emission (T-2969 BT3;
    replaces the T-2966 BT1 no-op stub). `health_service` is whatever
    this cycle's `dbus_health_service_fn(persona)` returned at the top of
    `controller_main` -- `None` when D-Bus is unavailable/unreachable
    this cycle (fail-open; see `_start_persona_controller_dbus_service`),
    in which case this stays a no-op exactly like the BT1 stub was.

    Emits the FULL current property snapshot on every call rather than a
    diff against a remembered previous value: diffing would need
    `controller_main` to carry an in-memory "what did I last emit"
    value across calls, which is exactly the session-continuity state
    SS88 forbids. Six short string/array properties -- the wire cost of
    always-full is negligible against that statelessness win."""
    if health_service is None:
        return
    health_service.emit_properties_changed()


def _last_invocation_ts(
    persona: str, *, state_root: Path | None, current_time: datetime, lookback_days: int = 7
) -> str | None:
    """Ts of the most recent `log_invocation` entry, walking backward from
    `current_time` across day-boundaries (spec SS `invocations/<date>.jsonl`
    is per-day) -- the health surface's `LastSelectionTs` (T-2969 BT3).

    `log_invocation`'s own docstring calls the log "write-only -- the
    controller never reads this back"; that invariant is about
    `controller_main`'s own selection DECISION never depending on the
    log (SS88 statelessness of the control loop). Reading it here, for a
    read-only health PROJECTION that never feeds back into selection, is
    a different concern -- observability -- and doesn't touch that
    invariant."""
    for offset in range(lookback_days):
        path = _invocations_path(persona, state_root=state_root, when=current_time - timedelta(days=offset))
        if not path.exists():
            continue
        last_line: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                last_line = line
        if last_line is None:
            continue
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            continue
        ts = payload.get("ts")
        if isinstance(ts, str) and ts:
            return ts
    return None


def _last_failure_class(
    persona: str, candidates: Sequence["PersonaBinding"], *, state_root: Path | None
) -> str | None:
    """Failure class of the most recent FAILED `last-attempt.json` record
    across every candidate runtime, by `last_attempt_ts` (T-2969 BT3's
    `LastFailureClass`). A runtime's most recent attempt succeeding
    clears its own record's `failure_class` to null (`record_last_attempt`
    outcome="success" behaviour) -- this only ever surfaces classes from
    attempts that are still recorded as failed."""
    latest_ts: datetime | None = None
    latest_class: str | None = None
    for binding in candidates:
        attempt = load_last_attempt(persona, _binding_runtime_slug(binding), state_root=state_root)
        if not attempt or attempt.get("outcome") != "failed":
            continue
        failure_class = attempt.get("failure_class")
        if not failure_class:
            continue
        ts = _parse_iso_or_none(attempt.get("last_attempt_ts"))
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts, latest_class = ts, failure_class
    return latest_class


def dbus_health_properties(
    persona: str,
    *,
    manifest_path: Path | None = None,
    candidates: Sequence["PersonaBinding"] | None = None,
    state_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Real ORG-068 SS92 health-property read (T-2969 BT3; spec SS D-Bus
    health interface). Replaces the T-2966 BT1 empty-defaults stub.

    Stateless by construction: every call re-reads durable state fresh
    (holds, per-runtime last-attempts, the active-runtime cache, the
    invocation log) -- nothing here is cached in this module across
    calls, matching `controller_main`'s own SS88 discipline. Reuses
    `controller_main`'s own selection helpers (`_select_viable_runtime`,
    `_earliest_cooldown_expiry`) rather than re-deriving viability
    independently, so this health projection can never drift from what
    the controller would actually select.

    `ViabilityStatus` derivation:
      - "all-held": every candidate is held (a non-empty candidate list
        where every ref is in `holds`).
      - "viable": `_select_viable_runtime` found a startable candidate.
      - "cooldown-hold": nothing is startable right now, but
        `_earliest_cooldown_expiry` names a time it will become so.
      - "exhausted": nothing is startable and nothing will become so on
        its own (empty candidate list is the only way this happens today
        -- see T-2969 DECISIONS for why held/cooldown candidates can't
        reach this branch given BT1's landed selection logic).

    `ModalPresent`/`ModalKind`/`ModalResetAt` (T-4047): additive mirror of
    `read_mid_session_cap_state`'s own published fields -- see that
    function's docstring for the full field contract and the T-4037/T-4047
    residual-risk note. Empty-safe defaults (`False`/`""`/`""`) when no
    mid-session-cap state has ever been written for this persona.
    """
    now = now or _utc_now
    current_time = now()

    if candidates is not None:
        runtime_candidates = list(candidates)
    else:
        from tools import persona_bringup

        manifest = manifest_path or persona_bringup.DEFAULT_MANIFEST
        primary = persona_bringup.load_binding(manifest, persona)
        runtime_candidates = list(persona_bringup.configured_runtime_bindings(manifest, primary))

    holds = load_holds(persona, state_root=state_root)
    selected, _skipped = _select_viable_runtime(
        persona, runtime_candidates, holds, current_time, state_root=state_root
    )
    earliest = _earliest_cooldown_expiry(persona, runtime_candidates, holds, state_root=state_root)
    all_held = bool(runtime_candidates) and all(
        _binding_runtime_ref(binding) in holds for binding in runtime_candidates
    )

    if all_held:
        viability_status = "all-held"
    elif selected is not None:
        viability_status = "viable"
    elif earliest is not None:
        viability_status = "cooldown-hold"
    else:
        viability_status = "exhausted"

    active = read_active_runtime(persona, state_root=state_root)
    active_runtime = active.get("runtime") or "" if isinstance(active, dict) else ""

    last_selection_ts = _last_invocation_ts(persona, state_root=state_root, current_time=current_time) or ""
    last_failure_class = _last_failure_class(persona, runtime_candidates, state_root=state_root) or ""

    # T-4047: surface the T-4037 mid-session-cap detector's published state
    # (read_mid_session_cap_state) on the SAME health interface Recovery's
    # T-2959 channel-liveness monitor already reads -- additive, same
    # empty-safe pattern as active_runtime above (never `{}` from a missing
    # file, always the type-appropriate default).
    cap_state = read_mid_session_cap_state(persona, state_root=state_root)
    modal_present = bool(cap_state.get("modal_present")) if isinstance(cap_state, dict) else False
    modal_kind = (cap_state.get("modal_kind") or "") if isinstance(cap_state, dict) else ""
    modal_reset_at = (cap_state.get("reset_at") or "") if isinstance(cap_state, dict) else ""

    return {
        "ActiveRuntime": active_runtime,
        "ViabilityStatus": viability_status,
        "LastSelectionTs": last_selection_ts,
        "LastFailureClass": last_failure_class,
        "NextCooldownExpiryTs": _iso(earliest) if earliest is not None else "",
        "Holds": sorted(holds.keys()),
        "ModalPresent": modal_present,
        "ModalKind": modal_kind,
        "ModalResetAt": modal_reset_at,
        # ORG-106 L4a (T-5527): additive seat-state mirror, fail-open inside the helper.
        **_seat_state_health_properties(persona, state_root=state_root),
    }


def _seat_state_health_properties(persona: str, *, state_root: Path | None = None) -> dict[str, object]:
    from tools.persona_controller_health_dbus import seat_state_properties

    # runtime_controller's state_root is the personas dir; seat_state's is its parent.
    return seat_state_properties(
        persona, state_root=Path(state_root).parent if state_root is not None else None)


def _health_leg_enabled() -> bool:
    return os.environ.get(PERSONA_HEALTH_LEG_ENV) == "on"


def _start_persona_controller_dbus_service(persona: str, *, state_root: Path | None = None) -> Any:
    """Production D-Bus bootstrap (T-2969 BT3) -- registers
    `org.pantheon.PersonaController.<persona>` on the session bus and
    returns the exported health object, or `None` on any failure.

    Fail-open by design (SS87 restart-fixable): the controller's core
    selection/start/block/record loop must never depend on the health
    surface's availability. Any failure to reach or claim the session
    bus (no session bus reachable, name already owned, dbus-python/GLib
    absent) degrades to "no D-Bus emission this cycle" rather than
    crashing or hanging `controller_main`.

    Not wired as `controller_main`'s own default -- see that function's
    `dbus_health_service_fn` docstring for why only the `controller-main`
    CLI subcommand (systemd's real `ExecStart`) passes this in."""
    try:
        from tools.persona_controller_health_dbus import DBUS_AVAILABLE, start_service

        if not DBUS_AVAILABLE:
            return None
        if _health_leg_enabled():
            # Lazy: a flag-off controller never imports the leaf or tools.germinate.
            from tools import persona_controller_health_leg as health_leg

            return start_service(
                persona,
                lambda: {
                    **dbus_health_properties(persona, state_root=state_root),
                    **health_leg.snapshot(persona),
                },
                health_leg=True,
            )
        return start_service(
            persona,
            lambda: dbus_health_properties(persona, state_root=state_root),
        )
    except Exception:
        return None


def _persist_runtime_record_on_launch(
    binding: "PersonaBinding",
    runtime_start: RuntimeStart,
    handle: RuntimeHandle,
    *,
    boundary_at: datetime,
    state_root: Path | None,
    provenance_fn: Callable[..., RuntimeSourceProvenance] | None = None,
) -> str:
    """T-4035: write the pantheon-runtime-state/v1 runtime.json RECORD for a runtime
    controller_main just launched, with the controller's own writer (_runtime_payload)
    -- the same record ensure_runtime writes -- so a relaunch that bypasses
    ensure_runtime no longer leaves it stale (the T-4023 class, hand-repaired by the
    `reconcile` subcommand until now).

    Returns "persisted", or "skipped: <reason>" when source provenance is
    unavailable (e.g. a dirty controller worktree): the old record is then LEFT
    as-is rather than replaced by a provenance-less one, because the rehydrate /
    hand-move readers REQUIRE provenance -- a record without it would only trade the
    stale-record failure for a provenance-missing one. The caller treats any other
    exception as best-effort too: this runs on every seat's relaunch hot path."""
    provenance_fn = provenance_fn or _controller_source_provenance
    try:
        provenance = provenance_fn(
            runtime_start_boundary_at=boundary_at,
            runtime_start_observed_at=handle.started_at,
        )
    except RuntimeError as exc:
        return f"skipped: source provenance unavailable ({exc})"
    _atomic_json(
        _persona_state(binding.persona, state_root) / "runtime.json",
        _runtime_payload(
            binding,
            session_id=None,
            started_at=handle.started_at,
            runtime_scope_unit=runtime_start.scope_unit,
            source_provenance=provenance,
        ),
    )
    return "persisted"


#: T-5016: controller_main's block outcome -> the session-end cause it records.
_SESSION_END_CAUSE_BY_OUTCOME = {
    "MidSessionCapSwapOutcome": "cap-swap",
    "FallbackReturnOutcome": "fallback-return",
}


def controller_main(
    persona: str,
    *,
    manifest_path: Path | None = None,
    state_root: Path | None = None,
    trigger: str = "systemd-restart",
    candidates: Sequence["PersonaBinding"] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    max_sleep_cap_seconds: float = MAX_SLEEP_CAP_SECONDS,
    max_reselect_cycles: int | None = None,
    start_runtime_fn: Callable[["PersonaBinding"], tuple[RuntimeStart, RuntimeHandle]] | None = None,
    block_until_death_fn: Callable[
        ["PersonaBinding"],
        "MidSessionCapSwapOutcome | FallbackReturnOutcome | OperatorSwapOutcome | SeatSleepOutcome | None",
    ] | None = None,
    dbus_health_service_fn: Callable[[str], Any] | None = None,
    persist_record_fn: Callable[..., str] | None = None,
    usability_probe_fn: Callable[["PersonaBinding", datetime | None], Any] | None = None,
) -> int:
    """Stateless invocation entry point (T-2966; spec SS Controller
    invocation flow).

    `persist_record_fn` (T-4035): writes the runtime.json record after a
    successful start; defaults to `_persist_runtime_record_on_launch`. Always
    best-effort -- a skip or ANY exception is logged to stderr and the launch
    proceeds, never aborts.

    Every call re-reads durable state fresh -- `candidates`/`holds`/
    `last-attempt.json` are all re-loaded at the top of each cycle; no
    in-process memory of a prior `controller_main` call informs this one
    (ORG-068 SS88).

    Two exit shapes, matching the spec pseudo-code exactly:
      - "no viable candidate": sleeps (capped) then loops IN-PROCESS
        (the pseudo-code's tail-call) -- nothing was started, so there is
        no process-level attempt-residue a real respawn would need to
        clear.
      - a runtime was attempted (started-then-died, or failed to start):
        records the outcome and returns/exits. In production (BT2) this
        process is `Restart=always` under systemd, so returning IS the
        pseudo-code's `sys.exit(0)` -- systemd respawns into step 1 with a
        clean process. `max_reselect_cycles` only bounds the in-process
        retry loop for tests; it never turns the death path into a loop.

    `dbus_health_service_fn` (T-2969 BT3): optional factory called ONCE
    per `controller_main` invocation, returning a D-Bus health-service
    handle for `persona` (or `None`). Defaults to a true no-op
    (`lambda _persona: None`) -- this function never reaches for a real
    D-Bus connection on its own. Production wiring
    (`org.pantheon.PersonaController.<persona>` on the session bus) is
    opted into ONLY by the `controller-main` CLI subcommand below
    (systemd's actual `ExecStart`), which passes
    `_start_persona_controller_dbus_service` -- so calling this function
    directly (as every existing test in this file does) never touches a
    real bus. Fetched once, before the loop: production's `Restart=always`
    is what re-registers a fresh bus-name claim per SS87, not repeated
    calls within one process's in-process no-viable-candidate retries.
    """
    now = now or _utc_now
    sleep = sleep or time.sleep
    start_runtime_fn = start_runtime_fn or (
        lambda binding: _controller_start_runtime(binding, state_root=state_root)
    )
    # T-4037: when the caller doesn't override block_until_death_fn, build the
    # production wiring FRESH each cycle below (inside the loop) so it closes
    # over that cycle's own runtime_candidates/holds -- a bare module-level
    # default here would be computed once, before either exists. Every caller
    # that DOES pass block_until_death_fn (every test above this comment, and
    # any future caller) is unaffected -- single positional-arg call
    # convention (`block_until_death_fn(selected)`) preserved exactly.
    _use_default_block_until_death = block_until_death_fn is None
    if not _use_default_block_until_death:
        active_block_until_death_fn = block_until_death_fn
    dbus_health_service_fn = dbus_health_service_fn or (lambda _persona: None)
    health_service = dbus_health_service_fn(persona)

    from tools import persona_bringup

    manifest = manifest_path or persona_bringup.DEFAULT_MANIFEST
    # T-2966 REBOUND finding #1: hash the instruction artifact once per
    # cycle (it's the same manifest for every log_invocation call within
    # this cycle) -- see `_instruction_sha256` for why this file and not
    # the KEEP layer's RUNTIME_SOURCE_HASH_PATHS bundle.
    instruction_sha256 = _instruction_sha256(manifest)

    cycles = 0
    sleep_logged = False
    while True:
        cycles += 1
        if candidates is not None:
            runtime_candidates = candidates
        else:
            primary = persona_bringup.load_binding(manifest, persona)
            runtime_candidates = persona_bringup.configured_runtime_bindings(manifest, primary)
            _report_retired_flip_keys(persona, primary.role, manifest, state_root=state_root)

        holds = load_holds(persona, state_root=state_root)
        current_time = now()

        # ORG-106 L3a: a deep-sleeping seat is never launched. Wait in-process (no
        # Restart=always churn, no exit-1 failure count), re-reading the record each
        # pass; wake / expiry / a paused-intake downgrade falls through to normal
        # selection. L3a is enforcement only: it ends no live session (the drain is L3b;
        # a live session under deep-sleep is L4's sleep_breach).
        sleep_gate = _seat_launch_block(persona, state_root=state_root, now=current_time)
        if sleep_gate.sleeping:
            if not sleep_logged:
                sleep_logged = True
                _append_controller_evidence(
                    persona, "controller_seat_sleep_wait",
                    {"owner": sleep_gate.owner or "", "expires": sleep_gate.expires or "",
                     "reason": sleep_gate.reason or "", "trigger": trigger},
                    state_root=state_root,
                )
            # ORG-106 L3b: this is also where a controller that re-exec'd mid-drain (or a
            # sleep recorded while no session was supervised) finishes the drain.
            drain_pending = False
            try:
                from tools import seat_sleep, seat_sleep_drain

                if seat_sleep_drain.needs_drain(
                        persona, seat_sleep.seat_root_for(_state_root(state_root)),
                        session_alive=_seat_sleep_session_alive(runtime_candidates), now=current_time):
                    drain_pending = seat_sleep_drain.run_drain(build_seat_sleep_seams(
                        persona, runtime_candidates, state_root=state_root, now=now, sleep=sleep,
                    )) == seat_sleep_drain.PENDING
            except Exception as exc:  # noqa: BLE001 - the gate must keep waiting, not die
                drain_pending = True
                _best_effort_controller_evidence(
                    persona, "sleep_drain_fault", {"error": f"{type(exc).__name__}: {exc}"[:300]},
                    state_root=state_root)
            _dbus_emit_health(persona, health_service)
            sleep(min(SEAT_SLEEP_DRAIN_RETRY_SECONDS if drain_pending else SEAT_SLEEP_RECHECK_SECONDS,
                      max_sleep_cap_seconds))
            if max_reselect_cycles is not None and cycles >= max_reselect_cycles:
                return 1
            continue
        sleep_logged = False

        selected, skipped = _select_viable_runtime(
            persona, runtime_candidates, holds, current_time, state_root=state_root,
            usability_probe=usability_probe_fn,
        )
        # T-4469: a completed operator swap outranks manifest priority while its
        # destination is the live runtime. Selecting it makes the start below ATTACH
        # (the argv matches) instead of respawning the primary over the operator's
        # choice; the supervise loop then watches the right binding, so
        # fallback-return can arm on it.
        pinned = _operator_pinned_runtime(persona, runtime_candidates, holds=holds, state_root=state_root)
        if pinned is not None and (
            selected is None or _binding_runtime_ref(pinned) != _binding_runtime_ref(selected)
        ):
            _append_controller_evidence(
                persona,
                "controller_selected_operator_destination",
                {
                    "destination": _binding_runtime_ref(pinned),
                    "priority_selection": _binding_runtime_ref(selected) if selected is not None else "",
                    "trigger": trigger,
                },
                state_root=state_root,
            )
            selected = pinned

        if selected is None:
            log_invocation(
                persona, "no-viable-candidate", trigger=trigger, skipped=skipped,
                instruction_sha256=instruction_sha256, state_root=state_root,
            )
            _dbus_emit_health(persona, health_service)
            earliest = _earliest_cooldown_expiry(persona, runtime_candidates, holds, state_root=state_root)
            held_refs = [_binding_runtime_ref(binding) for binding in runtime_candidates]
            # T-5028 (d): a runtime_blocked candidate waits for an operator exactly
            # like a held one, so it joins the in-process sleep (no restart churn).
            blocked_refs = {
                _binding_runtime_ref(binding)
                for binding in runtime_candidates
                if _binding_runtime_ref(binding) not in holds
                and _runtime_blocked_record(
                    load_last_attempt(persona, _binding_runtime_slug(binding), state_root=state_root)
                )
            }
            if earliest is None and held_refs and all(ref in holds or ref in blocked_refs for ref in held_refs):
                # T-5021: every candidate is operator-held. Exiting 1 here made
                # Restart=always respawn every RestartSec until StartLimitBurst failed
                # the unit. Sleep in-process instead (holds are re-read each cycle),
                # and say so in the journal every cycle so the state stays visible.
                sleep_seconds = min(ALL_HELD_RECHECK_SECONDS, max_sleep_cap_seconds)
                try:
                    print(
                        f"[runtime_controller] {persona}: WARN all candidates held "
                        f"({', '.join(held_refs)}); sleeping {sleep_seconds:g}s"
                        + (f" (runtime_blocked: {', '.join(sorted(blocked_refs))})" if blocked_refs else ""),
                        file=sys.stderr,
                    )
                except Exception:  # noqa: BLE001 -- a closed log pipe must not abort
                    pass
                sleep(sleep_seconds)
                if max_reselect_cycles is not None and cycles >= max_reselect_cycles:
                    return 1
                continue
            if earliest is None:
                return 1
            sleep_seconds = max(min((earliest - current_time).total_seconds(), max_sleep_cap_seconds), 0.0)
            sleep(sleep_seconds)
            if max_reselect_cycles is not None and cycles >= max_reselect_cycles:
                return 1
            continue

        selected_ref = _binding_runtime_ref(selected)
        started_at = now()
        # T-4101: serialize the controller's start against the consume/wake path
        # by acquiring the SAME per-persona persona_operation_lock the consume
        # path already holds across its start + ~90s provider-completion probe
        # (persona_bringup._wake_persistent_runtime_locked). The two paths both
        # respawn into the one tmux target with no mutual exclusion today; this
        # lock makes them mutually exclusive by construction (P1: architecture-
        # over-policy -- one OS flock, not a timing-hope). NON-blocking acquire:
        # if consume holds it, it is actively launching, so a respawn here would
        # be redundant AND would stomp its in-flight claude (the specialist race), and
        # a blocking wait would sit on the 6h lease TTL and wedge this
        # Restart=always daemon. So on contention we DEFER -- log the deferred
        # cycle to controller-evidence.jsonl, back off, and re-loop (which
        # re-reads durable state fresh per SS88 and re-checks). The lock is
        # released BEFORE block_until_death below: it guards only the
        # check-and-start, never the runtime's whole (hours-long) lifetime, which
        # would otherwise starve consume forever. The atomic viability re-check
        # (constraint 4) lives inside _controller_start_runtime, which runs under
        # this lock. See CONTROLLER_CONTENTION_DEFER_SECONDS.
        try:
            with persona_operation_lock(persona, state_root=state_root, blocking=False):
                try:
                    runtime_start, handle = start_runtime_fn(selected)
                except OperationLockBusy:
                    # F3: OperationLockBusy IS a RuntimeError -- keep it out of the
                    # start-failure handler so only the outer defer path ever
                    # handles lock contention (latent hardening: today's
                    # start_runtime_fn never acquires the lock, so this cannot fire).
                    raise
                except (RuntimeOperationalFailure, RuntimeError) as exc:
                    if isinstance(exc, RuntimeViabilityBlocked) and (
                        exc.policy_skip_reason == SEAT_SLEEP_POLICY_SKIP_REASON
                    ):
                        # ORG-106 L3a: the seat went to sleep between selection and start.
                        # Not a runtime failure: no attempt record, no cooldown. Exit clean;
                        # the next process waits at the gate above.
                        log_invocation(
                            persona, "start-refused-seat-sleep", trigger=trigger, selected=selected_ref,
                            skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
                        )
                        _dbus_emit_health(persona, health_service)
                        return 0
                    failure_class = _classify_death(exc)
                    attempt_now = now()
                    # T-5483: an operator restart landing mid-start/readiness is not the
                    # runtime's fault. A cap is real evidence: never masked by the marker.
                    if failure_class != "subscription_cap":
                        restart_marker = _consume_operator_restart_marker(
                            persona, selected_ref, attempt_started_at=started_at,
                            current=attempt_now, state_root=state_root,
                        )
                        if restart_marker is not None:
                            record_last_attempt(
                                persona, selected, outcome="success", trigger=trigger,
                                state_root=state_root, now=attempt_now,
                            )
                            _best_effort_controller_evidence(
                                persona, "operator_restart_observed",
                                {"supervised": selected_ref, "phase": "start",
                                 "operator": restart_marker.get("operator"),
                                 "correlation_id": restart_marker.get("correlation_id")},
                                state_root=state_root,
                            )
                            log_invocation(
                                persona, "start-failed", trigger=trigger, selected=selected_ref,
                                skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
                            )
                            _dbus_emit_health(persona, health_service)
                            return 1
                    failed_attempt = record_last_attempt(
                        persona, selected, outcome="failed", failure_class=failure_class,
                        trigger=trigger, state_root=state_root, now=attempt_now,
                        cooldown_until_override=_post_start_cap_cooldown_override(
                            exc, failure_class, now=attempt_now, harness=selected.launch.harness,
                        ),
                    )
                    log_invocation(
                        persona, "start-failed", trigger=trigger, selected=selected_ref,
                        skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
                    )
                    _dbus_emit_health(persona, health_service)
                    if failure_class == "subscription_cap":
                        # T-4804: a START-UP cap is the same swap as a mid-session one --
                        # Restart=always reselects the next viable runtime.
                        until = failed_attempt.get("cooldown_until") or ""
                        _notify_runtime_swap(
                            persona, selected_ref, "start-cap", until,
                            f"runtime-controller {persona}: {selected_ref} hit its usage limit at "
                            f"start-up; capped until {until}. The controller moves the seat to the "
                            f"next viable runtime and reselects {selected_ref} after that time.",
                            state_root=state_root,
                        )
                    return 1

                write_active_runtime(persona, selected, handle, pid=handle.pid, state_root=state_root)
                _note_launch_breach(persona, selected_ref, state_root=state_root, now=started_at)
                # T-4035: also persist the runtime.json RECORD so it self-heals on every
                # relaunch. Best-effort by contract -- this is the fleet-wide restart hot
                # path, so nothing here may abort the launch. Logged to stderr (journald),
                # NOT log_invocation: _last_invocation_ts reads that back for LastSelectionTs.
                try:
                    persist_outcome = (persist_record_fn or _persist_runtime_record_on_launch)(
                        selected, runtime_start, handle, boundary_at=started_at, state_root=state_root,
                    )
                except Exception as exc:  # noqa: BLE001 -- best-effort, see above
                    persist_outcome = f"failed: {type(exc).__name__}: {exc}"
                if persist_outcome != "persisted":
                    try:  # the diagnostic is best-effort too: a closed log pipe must not abort
                        print(
                            f"[runtime_controller] {persona}: runtime.json record not persisted on "
                            f"launch of {selected_ref} -- {persist_outcome}"[:1000],
                            file=sys.stderr,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # T-5371: stderr alone was lost when journald rotated (mason 2026-10-17:
                    # an untracked file made every launch skip, the record went stale for
                    # days and nothing durable said why). The evidence log survives reboots.
                    _best_effort_controller_evidence(
                        persona, "runtime_record_not_persisted",
                        {"selected": selected_ref, "outcome": persist_outcome[:500]},
                        state_root=state_root,
                    )
                log_invocation(
                    persona, "started", trigger=trigger, selected=selected_ref,
                    skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
                )
                _dbus_emit_health(persona, health_service)
        except OperationLockBusy:
            # Consume/wake holds the lock -> it is mid-launch. Defer, don't stomp.
            _append_controller_evidence(
                persona,
                "controller_start_deferred_lock_contended",
                {
                    "selected": selected_ref,
                    "trigger": trigger,
                    "defer_seconds": CONTROLLER_CONTENTION_DEFER_SECONDS,
                },
                state_root=state_root,
            )
            _dbus_emit_health(persona, health_service)
            sleep(min(CONTROLLER_CONTENTION_DEFER_SECONDS, max_sleep_cap_seconds))
            if max_reselect_cycles is not None and cycles >= max_reselect_cycles:
                return 1
            continue

        if _use_default_block_until_death:
            def active_block_until_death_fn(
                binding: "PersonaBinding", _candidates=runtime_candidates,
            ) -> "MidSessionCapSwapOutcome | FallbackReturnOutcome | OperatorSwapOutcome | None":
                # T-5069: `holds` is deliberately NOT passed. The cycle-start
                # snapshot is stale by the time the supervise loop runs for
                # hours: a hold cleared while a fallback runtime was live was
                # never seen, so fallback-return never evaluated (specialist,
                # 2026-10-16). With holds=None the eligibility predicates
                # re-read holds.json on every evaluation.
                # T-4047: `sleep` was a pre-existing gap here -- this default
                # wiring silently fell through to real `time.sleep` even when
                # a caller injected a fake `sleep` at the controller_main
                # level (only the "no viable candidate" branch above honored
                # it). `alive_check`/`poll_interval_seconds` are deliberately
                # NOT threaded: block_until_tmux_death's own docstring calls
                # `alive_check` a test-only seam, and production always wants
                # the real `_runtime_alive`; tests mock it directly instead.
                return block_until_tmux_death(
                    binding, state_root=state_root, candidates=_candidates, manifest_path=manifest_path,
                    capture_pane_fn=lambda: _capture_visible_runtime_pane(binding.controller.tmux),
                    now=now, sleep=sleep, usability_probe_fn=usability_probe_fn,
                    seat_sleep_fn=lambda: _seat_sleep_check(
                        persona, _candidates, state_root=state_root, now=now, sleep=sleep),
                    handoff_first_fn=lambda b: handoff_first.auto_swap_handoff_gate(
                        b, state_root=state_root,
                        log_fn=lambda event, payload: _append_controller_evidence(
                            b.persona, event, payload, state_root=state_root,
                        ),
                    ),
                )

        block_result = active_block_until_death_fn(selected)
        swap_notice: tuple[str, str, str] | None = None

        # T-5016 H2: the supervised session has ended; archive it now (fail-open,
        # bounded). H1 at the next start then finds it archived and does nothing.
        # Review M1: an operator swap already runs the DESTINATION in this pane
        # and H1 archived the source when the swap respawned it, so H2 skips it;
        # every other outcome is staged only if the pane is dead or gone.
        if not isinstance(block_result, OperatorSwapOutcome):
            _session_chronicle_end(
                selected,
                cause=_SESSION_END_CAUSE_BY_OUTCOME.get(type(block_result).__name__, "death"),
                state_root=state_root,
                stage_if_dead=True,
            )

        # T-4037: a MidSessionCapSwapOutcome means the runtime was
        # DELIBERATELY ended by the mid-session-cap swap action, not observed
        # to have died on its own -- record subscription_cap with the
        # outcome's OWN cooldown (never next_weekly_reset) and skip the
        # sustained-alive success/process_death branch entirely, even if the
        # runtime had been alive well past SUSTAINED_ALIVE_SECONDS before the
        # swap fired (the realistic case).
        if isinstance(block_result, MidSessionCapSwapOutcome):
            prior_attempt = load_last_attempt(persona, _binding_runtime_slug(selected), state_root=state_root) or {}
            already_recorded = (
                prior_attempt.get("failure_class") == "subscription_cap"
                and (prior_attempt.get("provenance") or {}).get("trigger") == "mid-session-cap-detect"
            )
            record_last_attempt(
                persona, selected, outcome="failed", failure_class="subscription_cap",
                trigger=trigger, state_root=state_root, now=now(),
                cooldown_until_override=block_result.cooldown_until,
                bump_failures=not already_recorded,
            )
            swap_notice = (
                "cap-swap", _iso(block_result.cooldown_until),
                f"runtime-controller {persona}: {selected_ref} hit its usage limit "
                f"({block_result.modal_kind}); capped until {_iso(block_result.cooldown_until)} "
                f"({block_result.cooldown_source}). The controller moves the seat to the next viable "
                f"runtime and reselects {selected_ref} automatically after that time.",
            )
        elif isinstance(block_result, FallbackReturnOutcome):
            # T-4046: the runtime was deliberately ended by the
            # fallback-return action to return to a higher-priority
            # candidate -- record SUCCESS (never a failure/cooldown; a
            # planned failback is not this runtime's fault), exactly as the
            # natural sustained-alive branch below would for any healthy
            # death, regardless of how long `selected` had actually been
            # alive before the failback fired.
            record_last_attempt(
                persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
            )
            swap_notice = (
                "fallback-return", _iso(now()),
                f"runtime-controller {persona}: returning the seat from {selected_ref} to "
                f"{block_result.target_ref} (its cap cooldown has elapsed).",
            )
        elif isinstance(block_result, SeatSleepOutcome):
            # ORG-106 L3b: the deep-sleep drain ended this session on purpose. A SUCCESS for the
            # runtime (never process_death, never a cooldown); the next process waits at the gate.
            record_last_attempt(
                persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
            )
            _append_controller_evidence(
                persona, "controller_observed_seat_sleep",
                {"supervised": selected_ref, "drain_outcome": block_result.drain_outcome},
                state_root=state_root,
            )
        elif isinstance(block_result, OperatorSwapOutcome):
            # T-4469: an operator transition deliberately replaced this runtime in
            # its pane. A SUCCESS for it (a planned swap is not its fault), then exit:
            # Restart=always reselects, and the operator pin attaches to the
            # destination that is now live.
            record_last_attempt(
                persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
            )
            _append_controller_evidence(
                persona,
                "controller_observed_operator_swap",
                {"supervised": selected_ref, "destination": block_result.destination_ref},
                state_root=state_root,
            )
        else:
            alive_seconds = (now() - started_at).total_seconds()
            # T-5483: a deliberate operator `harness restart` is a SUCCESS at any
            # alive duration (mirrors the operator-swap treatment above).
            restart_marker = _consume_operator_restart_marker(
                persona, selected_ref, attempt_started_at=started_at, current=now(),
                state_root=state_root,
            )
            if _seat_sleep_end_expected(persona, selected_ref, started_at, now(), state_root):
                # ORG-106 L3b: the session ended inside a deep-sleep drain (marker, or a live
                # deep-sleep record). Expected, not a failure; the gate finishes the drain.
                record_last_attempt(
                    persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
                )
                _best_effort_controller_evidence(
                    persona, "sleep_end_observed", {"supervised": selected_ref}, state_root=state_root,
                )
            elif restart_marker is not None:
                record_last_attempt(
                    persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
                )
                _best_effort_controller_evidence(
                    persona,
                    "operator_restart_observed",
                    {"supervised": selected_ref, "phase": "running",
                     "operator": restart_marker.get("operator"),
                     "correlation_id": restart_marker.get("correlation_id")},
                    state_root=state_root,
                )
            elif alive_seconds >= SUSTAINED_ALIVE_SECONDS:
                record_last_attempt(
                    persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
                )
            else:
                record_last_attempt(
                    persona, selected, outcome="failed", failure_class="process_death",
                    trigger=trigger, state_root=state_root, now=now(),
                )
        clear_active_runtime(persona, state_root=state_root)
        clear_mid_session_cap_state(persona, state_root=state_root)
        _dbus_emit_health(persona, health_service)
        # T-4804: after the state cleanup, so alerting can never sit ahead of it.
        if swap_notice is not None:
            kind, event_key, text = swap_notice
            _notify_runtime_swap(
                persona, selected_ref if kind == "cap-swap" else block_result.target_ref,
                kind, event_key, text, state_root=state_root,
            )
        return 0


# ---------------------------------------------------------------------------
# T-4023: runtime.json record reconcile (out-of-band repair).
#
# [T-4035 update: controller_main now ALSO persists the record on launch
# (_persist_runtime_record_on_launch), best-effort -- it is skipped when source
# provenance is unavailable (dirty controller worktree), so this repair stays the
# fallback for that case and for any relaunch path outside controller_main.]
# Originally: the SS87 controller_main launch path wrote only the active-runtime.json
# cache, NOT the pantheon-runtime-state/v1 record (that is refreshed only by
# ensure_runtime / _rehydrate_recorded_runtime_selection / _replace_runtime). So a
# relaunch that bypasses ensure_runtime (handoff_and_restart.sh restart_request, or
# a codex-fallback->claude recovery) leaves runtime.json STALE, which (a) can make a
# Claude-only continuity-monitor gate misread the harness and (b) makes a future
# ensure_runtime/hand-move/cutover safety-block on the pane-vs-record mismatch
# (reference_handmove_fails_on_stale_recorded_runtime_scope). This subcommand
# re-derives the record from the LIVE pane matched to a configured binding and
# writes it with the controller's OWN writer (_runtime_payload) — exactly what Recovery
# did by hand in T-4022, now a tool. It is FAIL-CLOSED by design: it refuses (never
# writes a guess) unless the live pane uniquely matches one configured binding, and
# it is dry-run by default (apply=False). It does NOT touch the launch hot path;
# the durable auto-heal (persist-on-launch) is a separate supervised follow-up.
# ---------------------------------------------------------------------------


class _ReconcileRefused(Exception):
    """Internal: a fail-closed refusal to reconcile (carries the operator reason)."""


@dataclass(frozen=True)
class ReconcileResult:
    status: str  # refused | already-consistent | dry-run | reconciled
    reason: str
    matched_ref: str | None
    changed: bool
    record_path: str
    backup_path: str | None = None
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "matched_ref": self.matched_ref,
            "changed": self.changed,
            "record_path": self.record_path,
            "backup_path": self.backup_path,
        }


def _read_pane_cmdline(pid: int, *, proc_root: Path = Path("/proc")) -> list[str] | None:
    """argv of `pid` from /proc/<pid>/cmdline, or None on any read failure.

    Run in the persona's OWN account context so /proc is readable — the reconcile
    subcommand is invoked as the seat's account (like controller_main)."""
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
    return parts or None


def _pane_argv_matches_binding(argv: Sequence[str], binding: "PersonaBinding") -> bool:
    """True iff a live pane's argv is running `binding`'s harness executable (and
    its model string, when the binding pins one).

    Harness-identity signal shared by the reconcile matcher (`_match_live_binding`)
    and the T-4101 controller attach guard: the per-persona tmux target is
    harness-independent (all harnesses share one pane), and `_runtime_alive` is
    harness-blind (session/pane + `kill -0`), so adopting a live pane as a given
    binding REQUIRES this argv check -- otherwise a CODEX pane would be adopted
    (and recorded) as the SELECTED claude binding."""
    exe = str(binding.launch.executable)
    exe_base = os.path.basename(exe)
    harness_hit = any(arg == exe or os.path.basename(arg) == exe_base for arg in argv)
    model = binding.launch.model
    model_hit = (not model) or (model in " ".join(argv))
    return harness_hit and model_hit


def _match_live_binding(
    bindings: Sequence["PersonaBinding"],
    *,
    context: TmuxRuntimeContext | None = None,
    proc_root: Path = Path("/proc"),
) -> tuple["PersonaBinding", int, list[str]]:
    """Return the single configured binding the LIVE pane is running, its pane pid,
    and the pane argv — or raise _ReconcileRefused (fail-closed).

    Match signal: the runtime CLI basename in the pane argv (harness) AND the
    binding's model string in the argv (disambiguates same-harness bindings). A
    non-unique match (0 or >1) refuses rather than guessing — a wrong binding would
    write a wrong record, the exact harm this repair exists to avoid."""
    if not bindings:
        raise _ReconcileRefused("no configured bindings for persona")
    target = bindings[0].controller.tmux
    if not (_session_exists(target.session, context=context) and pane_exists(target, context=context)):
        raise _ReconcileRefused(
            f"no live pane at {_target_text(target)} — reconcile only repairs a RUNNING seat's record"
        )
    pid = _pane_process_pid(target, context=context)
    if pid is None:
        raise _ReconcileRefused(f"could not read the live pane pid for {_target_text(target)}")
    if not _pid_alive(pid):
        raise _ReconcileRefused(f"pane pid {pid} is not alive")
    argv = _read_pane_cmdline(pid, proc_root=proc_root)
    if not argv:
        raise _ReconcileRefused(f"could not read /proc/{pid}/cmdline for the live pane")
    matches: list["PersonaBinding"] = [
        binding for binding in bindings if _pane_argv_matches_binding(argv, binding)
    ]
    if len(matches) != 1:
        refs = [_binding_runtime_ref(b) for b in bindings]
        raise _ReconcileRefused(
            f"live pane argv did not UNIQUELY match one configured binding "
            f"(matched {len(matches)} of {refs}) — refusing (fail-closed); reconcile by hand per the SOP"
        )
    return matches[0], pid, argv


def reconcile_runtime_record(
    persona: str,
    *,
    manifest_path: Path | None = None,
    state_root: Path | None = None,
    apply: bool = False,
    bindings: Sequence["PersonaBinding"] | None = None,
    match_fn: Callable[..., tuple["PersonaBinding", int, list[str]]] = _match_live_binding,
    provenance_fn: Callable[..., RuntimeSourceProvenance] = _controller_source_provenance,
    now: Callable[[], datetime] = _utc_now,
    context: TmuxRuntimeContext | None = None,
    proc_root: Path = Path("/proc"),
) -> ReconcileResult:
    """Re-derive and (optionally) rewrite a persona's pantheon-runtime-state/v1
    record to match its LIVE runtime. Dry-run unless apply=True. Fail-closed:
    refuses on any ambiguity or an unclean controller worktree rather than writing
    a guess. Idempotent: a record that already matches the live binding is a no-op."""
    if bindings is None:
        # Local import: persona_bringup imports runtime_controller at module scope,
        # so a module-level import here would be circular (same reason
        # _manifest_bindings imports it locally).
        from tools import persona_bringup
        mpath = manifest_path or persona_bringup.DEFAULT_MANIFEST
        bindings = _manifest_bindings(mpath, persona)
    record_path = _persona_state(persona, state_root) / "runtime.json"
    try:
        binding, pid, _argv = match_fn(bindings, context=context, proc_root=proc_root)
    except _ReconcileRefused as exc:
        return ReconcileResult("refused", str(exc), None, False, str(record_path))

    current = _read_json(record_path)  # {} if absent/unreadable
    ref = _binding_runtime_ref(binding)
    if current.get("schema") == "pantheon-runtime-state/v1" and _state_matches(
        binding, current, context=context
    ):
        return ReconcileResult(
            "already-consistent", f"record already matches live binding {ref}", ref, False, str(record_path)
        )

    started = now()
    try:
        provenance = provenance_fn(
            runtime_start_boundary_at=started, runtime_start_observed_at=started
        )
    except RuntimeError as exc:
        return ReconcileResult(
            "refused",
            f"source provenance unavailable ({exc}) — reconcile needs a clean controller worktree",
            ref, False, str(record_path),
        )
    payload = _runtime_payload(
        binding,
        session_id=None,
        started_at=started,
        runtime_scope_unit=(context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT),
        source_provenance=provenance,
    )
    # Honest marker: this is an out-of-band REPAIR of the record, not a fresh launch.
    payload["last_structural_event"] = "record_reconciled"

    if not apply:
        return ReconcileResult(
            "dry-run",
            f"would reconcile record to live binding {ref} (pane pid {pid}); re-run with --apply to write",
            ref, True, str(record_path), payload=payload,
        )

    backup_path: str | None = None
    if current:
        backup_path = f"{record_path}.reconcile-bak-{started.strftime('%Y%m%dT%H%M%SZ')}"
        _atomic_json(Path(backup_path), current)
    _atomic_json(record_path, payload)
    written = _read_json(record_path)
    if not _state_matches(binding, written, context=context):
        return ReconcileResult(
            "refused",
            "post-write verification FAILED: the written record does not _state_match the binding",
            ref, True, str(record_path), backup_path=backup_path, payload=payload,
        )
    return ReconcileResult(
        "reconciled", f"record reconciled to live binding {ref}", ref, True,
        str(record_path), backup_path=backup_path, payload=payload,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ack = subparsers.add_parser("ack-read")
    ack.add_argument("--persona", required=True)
    ack.add_argument("--state-root", type=Path, default=None)
    ack.add_argument("--evidence", required=True)
    viability = subparsers.add_parser("initial-viability")
    viability.add_argument("--persona", required=True)
    viability.add_argument("--state-root", type=Path, default=None)
    viability.add_argument("--expect-selected")
    viability.add_argument("--expect-failed")
    viability.add_argument(
        "--expect-failure-class",
        action="append",
        choices=("subscription-cap", "operational-launch", "operational-readiness"),
        default=[],
    )
    viability.add_argument("--require-source-provenance", action="store_true")
    viability.add_argument("--require-receiver-receipt", action="store_true")
    controller = subparsers.add_parser("controller-main")
    controller.add_argument("--persona", required=True)
    controller.add_argument("--state-root", type=Path, default=None)
    controller.add_argument("--manifest", type=Path, default=None)
    controller.add_argument("--trigger", default="systemd-restart")
    # T-4023: out-of-band repair of a stale pantheon-runtime-state/v1 record.
    # Dry-run by default; --apply writes (with a timestamped backup). Run AS the
    # persona's account (so /proc + the state path + the tmux pane all resolve).
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--persona", required=True)
    reconcile.add_argument("--state-root", type=Path, default=None)
    reconcile.add_argument("--manifest", type=Path, default=None)
    reconcile.add_argument("--apply", action="store_true",
                           help="write the reconciled record (default: dry-run preview only)")
    args = parser.parse_args(argv)
    if args.command == "ack-read":
        # T-5028 review MINOR 2: the same lock the consume/quota observer runs
        # under, so its read-modify-write of transition.json can't race this ack.
        with persona_operation_lock(
            args.persona, state_root=args.state_root, blocking=True,
            timeout_seconds=ACK_READ_LOCK_TIMEOUT_SECONDS,
        ):
            acknowledge_successor_read(
                args.persona,
                evidence=args.evidence,
                state_root=args.state_root,
            )
        return 0
    if args.command == "initial-viability":
        result = validate_initial_viability_state(
            args.persona,
            state_root=args.state_root,
            expect_selected=args.expect_selected,
            expect_failed=args.expect_failed,
            expect_failure_classes=args.expect_failure_class,
            require_source_provenance=args.require_source_provenance,
            require_receiver_receipt=args.require_receiver_receipt,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "controller-main":
        # T-2966 BT1: one selection+block+record cycle of the stateless
        # controller (spec SS Controller invocation flow). BT2 wires this as
        # the ExecStart of pantheon-runtime-controller@<persona>.service
        # (Type=simple, Restart=always) -- the systemd unit is what turns
        # this single-cycle exit into the restart-fixable loop.
        return controller_main(
            args.persona,
            manifest_path=args.manifest,
            state_root=args.state_root,
            trigger=args.trigger,
            dbus_health_service_fn=lambda persona: _start_persona_controller_dbus_service(
                persona, state_root=args.state_root
            ),
            usability_probe_fn=_production_usability_probe(args.state_root),
        )
    if args.command == "reconcile":
        result = reconcile_runtime_record(
            args.persona,
            manifest_path=args.manifest,
            state_root=args.state_root,
            apply=args.apply,
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
        # exit 0 on a clean read (reconciled / already-consistent / dry-run);
        # exit 1 on a fail-closed refusal so a caller/SOP can branch on it.
        return 0 if result.status in ("reconciled", "already-consistent", "dry-run") else 1
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
