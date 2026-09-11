"""Deterministic Pantheon runtime controller for persistent persona harnesses.

The controller owns process/tmux lifecycle and transition evidence.  It never
asks a model whether a quota decision is safe and never treats terminal pixels
as completion, acknowledgement, health, or quota authority.
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
import errno
import base64
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, NamedTuple, Sequence, TYPE_CHECKING

from tools import codex_modal
from tools import chronicle
from tools._lease import Lease
from tools.codex_telegram_actions import receiver_handoff_actions as _receiver_action_order
from tools.harness_adapter.codex import (
    ensure_model_migration_prompts_hidden,
    ensure_project_trusted,
    ensure_update_check_disabled,
)
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.quota import next_weekly_reset
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
from tools.submit_verify import ready_for_submission

if TYPE_CHECKING:
    from tools.harness_adapter.base import PantheonHarnessAdapter
    from tools.persona_bringup import PersonaBinding


DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "pantheon" / "personas"
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
RUNTIME_SOURCE_HASH_PATHS = (
    "instance.yaml",
    "tools/claude_quota_probe.py",
    "tools/claude_statusline_capture.py",
    "tools/chronicle.py",
    "tools/mailbox_wake.py",
    "tools/persona_bringup.py",
    "tools/runtime_controller.py",
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
INITIAL_VIABILITY_INVOCATION_SCHEMA = "pantheon-runtime-initial-viability-invocation/v1"
INITIAL_VIABILITY_INVOCATION_MAX_BYTES = 256 * 1024
PROVENANCE_RECEIPT_MAX_BYTES = 1024 * 1024
SHEET_STDOUT_MAX_BYTES = 256 * 1024
SYSTEMD_INVOCATION_ID_RE = re.compile(r"[0-9a-f]{32}")


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
    status: Literal["selected", "failed", "skipped-cooldown", "skipped-recorded-sticky"]
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

    def __init__(self, reason: str, evidence: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence)


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
    controller evidence never inlines raw terminal content.
    """

    ready: bool
    pane_sha256: str
    reason: str
    adapter_status: str = "unknown"
    adapter_cause: str | None = None
    adapter_pattern_id: str | None = None
    adapter_capture_count: int = 0


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


def _state_root(state_root: Path | None = None) -> Path:
    if state_root is not None:
        return state_root
    configured = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    return Path(configured) if configured else DEFAULT_STATE_ROOT


def _persona_state(persona: str, state_root: Path | None = None) -> Path:
    return _state_root(state_root) / persona / "runtime-controller"


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


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def _validated_source_hashes_from_state(state: Mapping[str, Any]) -> dict[str, str]:
    raw = state.get("controller_source_hashes")
    if not isinstance(raw, Mapping):
        raise ValueError("recorded-source-hashes-missing")
    if set(raw.keys()) != set(RUNTIME_SOURCE_HASH_PATHS):
        raise ValueError("recorded-source-key-mismatch")
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
    return RuntimeViabilityBlocked(reason, evidence)


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


def _runtime_command_argv(
    argv: tuple[str, ...], env: dict[str, str] | Any
) -> tuple[str, ...]:
    values = dict(env)
    ordered_keys = (("PATH",) if "PATH" in values else ()) + tuple(
        key for key in sorted(values) if key != "PATH"
    )
    environment = tuple(f"{key}={values[key]}" for key in ordered_keys)
    return ("/usr/bin/env", "--", *environment, *argv)


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


def pane_exists(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message", "-p", "-t", _target_text(target), "#{pane_dead}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and result.stdout.strip() == "0"


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
    result = _run_tmux(
        "display-message", "-p", "-t", _target_text(target), "#{pane_pid}",
        **_context_kwargs(context),
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


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
        no_server = (
            detail.startswith("no server running on ")
            or (
                detail.startswith("error connecting to ")
                and "no such file or directory" in detail
            )
        )
        if no_server:
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


def _stop_created_scope(scope_unit: str) -> None:
    _run_systemctl_user("stop", scope_unit)
    _run_systemctl_user("reset-failed", scope_unit)


def _wait_for_live_runtime(
    target: TmuxTarget,
    scope_unit: str,
    *,
    timeout: float = RUNTIME_START_TIMEOUT_SECONDS,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pane_exists(target, **_context_kwargs(context)):
            try:
                server_pid = _default_tmux_server_pid(**_context_kwargs(context))
            except RuntimeError:
                server_pid = None
            if server_pid is not None and _pid_in_scope(server_pid, scope_unit):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RUNTIME_START_POLL_SECONDS)


def _start_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    replace: bool,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeStart:
    target = binding.controller.tmux
    spec = adapter.build_interactive_launch(target, cwd=binding.launch_cwd)
    scoped_env = _runtime_env_with_scope_path(spec.env, account_home=binding.account_home)
    if binding.launch.harness == "codex":
        # TRUST modal -> config, never a keystroke (T-2594). spec.cwd is the
        # ACTUAL resolved launch cwd, never a hardcoded guess -- see
        # tools.harness_adapter.codex.ensure_project_trusted's own docstring.
        ensure_project_trusted(binding.account_home / ".codex", spec.cwd)
        # Update-picker/banner suppression (T-2597 wedges #2/#3) -- same
        # config-seed path as the trust fix above.
        ensure_update_check_disabled(binding.account_home / ".codex")
        # Model-migration prompt suppression for known-hideable active
        # presets (T-2597 wedge #4) -- see ensure_model_migration_prompts_
        # hidden's own docstring for the documented residual gap.
        ensure_model_migration_prompts_hidden(binding.account_home / ".codex")
    command_argv = _runtime_command_argv(spec.argv, spec.env)
    scope_unit = context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None:
        if replace:
            raise RuntimeError("cannot replace a tmux target without a live server")
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
        spec.argv,
        scoped_env,
    )
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
        _stop_created_scope(started.scope_unit)
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


def _post_start_sample(text: str, *, capture_index: int) -> PostStartPaneSample:
    return PostStartPaneSample(
        capture_index=capture_index,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        snippet_sha256=hashlib.sha256(text[:512].encode("utf-8")).hexdigest() if text else None,
        text=text,
    )


def _post_start_assessment_evidence(assessment: PostStartAssessment) -> Mapping[str, str]:
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
) -> PostStartAssessment:
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
    }
    if assessment.transcript_relative_path_sha256 is not None:
        evidence["transcript_relative_path_sha256"] = assessment.transcript_relative_path_sha256
    if assessment.transcript_start_offset is not None:
        evidence["transcript_start_offset"] = str(assessment.transcript_start_offset)
    if assessment.transcript_end_offset is not None:
        evidence["transcript_end_offset"] = str(assessment.transcript_end_offset)
    return evidence


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
    sleep: Callable[[float], None] = time.sleep,
) -> ProviderCompletionAssessment:
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
    submit_to_runtime(
        handle,
        ExecutionRequest(prompt=probe.prompt, cwd=binding.account_home),
        context=context,
    )
    samples: list[PostStartPaneSample] = []
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
            return last
        if index < PROVIDER_COMPLETION_CAPTURE_COUNT:
            sleep(PROVIDER_COMPLETION_POLL_SECONDS)
    return dataclasses.replace(
        last,
        status="operational-readiness",
        cause="provider-completion-not-proven",
        capture_count=len(samples),
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
) -> RuntimeInputReadiness:
    """Poll the runtime pane until it accepts input or the deadline passes.

    Mirrors _wait_for_live_runtime's monotonic-deadline poll shape. Evidence
    always carries a hash of the captured pane, never the raw text.

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
    if started.created_scope:
        _stop_created_scope(started.scope_unit)
    evidence = {
        "event": "runtime_input_not_ready",
        "harness": binding.launch.harness,
        "profile": binding.launch.profile,
        "tmux_target": _target_text(binding.controller.tmux),
        "runtime_scope_unit": started.scope_unit,
        "created_scope": str(started.created_scope).lower(),
        "pane_sha256": readiness.pane_sha256,
        "reason": readiness.reason,
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


def _validate_systemd_invocation_id(value: str) -> str:
    if not SYSTEMD_INVOCATION_ID_RE.fullmatch(value):
        raise RuntimeError("systemd INVOCATION_ID is malformed")
    return value


def _systemd_invocation_id_from_env(env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    if "INVOCATION_ID" not in source:
        return None
    return _validate_systemd_invocation_id(source.get("INVOCATION_ID", ""))


def _initial_viability_invocation_dir(persona: str, state_root: Path | None) -> Path:
    return _persona_state(persona, state_root) / "initial-viability-invocations"


def _initial_viability_invocation_path(
    persona: str,
    invocation_id: str,
    state_root: Path | None,
) -> Path:
    return _initial_viability_invocation_dir(persona, state_root) / f"{_validate_systemd_invocation_id(invocation_id)}.json"


def _secure_directory_fd(path: Path, *, mode: int = 0o700, create: bool) -> int:
    if not path.is_absolute():
        raise RuntimeError("invocation receipt directory must be absolute")
    parts = path.parts
    if not parts or parts[0] != "/":
        raise RuntimeError("invocation receipt directory must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in parts[1:]:
            if part in {"", ".", ".."}:
                raise RuntimeError("invocation receipt directory contains unsafe component")
            try:
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            except FileNotFoundError as exc:
                if not create:
                    raise RuntimeError("invocation receipt directory is missing") from exc
                os.mkdir(part, mode, dir_fd=fd)
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RuntimeError("invocation receipt directory contains symlink or non-directory") from exc
                raise
            st = os.fstat(next_fd)
            if not stat.S_ISDIR(st.st_mode):
                os.close(next_fd)
                raise RuntimeError("invocation receipt directory component is not a directory")
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


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


def _read_invocation_payload_no_follow_at(parent_fd: int, name: str) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError("invocation receipt is unavailable") from exc
    return _read_invocation_payload_fd(fd)


def _read_invocation_payload_fd(fd: int) -> tuple[dict[str, Any], str]:
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("invocation receipt is not a regular file")
        if st.st_uid != os.geteuid():
            raise RuntimeError("invocation receipt owner mismatch")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise RuntimeError("invocation receipt mode mismatch")
        if st.st_size <= 0 or st.st_size > INITIAL_VIABILITY_INVOCATION_MAX_BYTES:
            raise RuntimeError("invocation receipt size is outside the bounded range")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(INITIAL_VIABILITY_INVOCATION_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > INITIAL_VIABILITY_INVOCATION_MAX_BYTES:
        raise RuntimeError("invocation receipt size is outside the bounded range")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invocation receipt JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invocation receipt payload is not an object")
    return payload, hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _invocation_receipt_payload(receipt: RuntimeViabilityReceipt, invocation_id: str) -> dict[str, Any]:
    return {
        "schema": INITIAL_VIABILITY_INVOCATION_SCHEMA,
        "persona": receipt.persona,
        "invocation_id": _validate_systemd_invocation_id(invocation_id),
        "initial_viability": _viability_payload(receipt),
    }


def _validate_invocation_payload(
    payload: Mapping[str, Any],
    *,
    persona: str,
    invocation_id: str,
) -> Mapping[str, Any]:
    if payload.get("schema") != INITIAL_VIABILITY_INVOCATION_SCHEMA:
        raise RuntimeError("invocation receipt schema mismatch")
    if payload.get("persona") != persona:
        raise RuntimeError("invocation receipt persona mismatch")
    if payload.get("invocation_id") != _validate_systemd_invocation_id(invocation_id):
        raise RuntimeError("invocation receipt id mismatch")
    viability = payload.get("initial_viability")
    if not isinstance(viability, Mapping):
        raise RuntimeError("invocation receipt missing viability payload")
    if viability.get("schema") != "pantheon-runtime-initial-viability/v1":
        raise RuntimeError("invocation viability schema mismatch")
    if viability.get("persona") != persona:
        raise RuntimeError("invocation viability persona mismatch")
    if viability.get("status") != "selected":
        raise RuntimeError("invocation viability is not selected")
    return viability


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


def validate_initial_viability_invocation_receipt(
    persona: str,
    *,
    invocation_id: str,
    state_root: Path | None,
    allowed_runtime_refs: Sequence[str],
    require_receiver_receipt: bool = False,
    receiver_operations_by_runtime: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Any]:
    path = _initial_viability_invocation_path(persona, invocation_id, state_root)
    state_root_path = _state_root(state_root)
    if not state_root_path.is_absolute() or not path.is_absolute():
        raise RuntimeError("invocation receipt state root must be absolute")
    try:
        path.relative_to(state_root_path)
    except ValueError as exc:
        raise RuntimeError("invocation receipt path escapes state root") from exc
    parent_fd = _secure_directory_fd(path.parent, create=False)
    try:
        payload, payload_sha = _read_invocation_payload_no_follow_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)
    return _validate_initial_viability_invocation_payload(
        persona,
        invocation_id=invocation_id,
        payload=payload,
        payload_sha=payload_sha,
        allowed_runtime_refs=allowed_runtime_refs,
        require_receiver_receipt=require_receiver_receipt,
        receiver_operations_by_runtime=receiver_operations_by_runtime,
    )


def _validate_initial_viability_invocation_payload(
    persona: str,
    *,
    invocation_id: str,
    payload: Mapping[str, Any],
    payload_sha: str,
    allowed_runtime_refs: Sequence[str],
    require_receiver_receipt: bool = False,
    receiver_operations_by_runtime: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Any]:
    viability = _validate_invocation_payload(
        payload,
        persona=persona,
        invocation_id=invocation_id,
    )
    allowed = tuple(allowed_runtime_refs)
    if not allowed or len(set(allowed)) != len(allowed):
        raise RuntimeError("allowed runtime refs must be nonempty and deduplicated")
    selected = viability.get("selected_runtime")
    if selected not in allowed:
        raise RuntimeError("selected runtime is not manifest-authorized")
    attempts = viability.get("attempts")
    if not isinstance(attempts, list):
        raise RuntimeError("invocation viability attempts missing")
    selected_attempts = [
        item
        for item in attempts
        if isinstance(item, Mapping)
        and item.get("runtime") == selected
        and item.get("status") == "selected"
    ]
    if len(selected_attempts) != 1:
        raise RuntimeError("selected invocation attempt missing or duplicated")
    selected_attempt = selected_attempts[0]
    readiness = selected_attempt.get("readiness")
    if not isinstance(readiness, Mapping):
        raise RuntimeError("selected readiness evidence missing")
    if not readiness.get("provider_completion_status"):
        raise RuntimeError("selected provider completion status missing")
    provenance = selected_attempt.get("source_provenance")
    required_provenance = {
        "revision",
        "source_manifest_sha256",
        "runtime_start_boundary_at",
        "runtime_start_observed_at",
    }
    if not isinstance(provenance, Mapping) or not required_provenance.issubset(provenance):
        raise RuntimeError("selected source provenance missing")
    allowed_failures = {"subscription-cap", "operational-launch", "operational-readiness"}
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise RuntimeError("invocation viability attempt is malformed")
        if attempt.get("status") == "failed" and attempt.get("failure_class") not in allowed_failures:
            raise RuntimeError("failed attempt has unknown failure class")
    receiver = selected_attempt.get("receiver")
    expected_receiver_ops = dict(receiver_operations_by_runtime or {})
    expected_ops = tuple(str(item) for item in expected_receiver_ops.get(str(selected), ()))
    if require_receiver_receipt or expected_ops:
        if not isinstance(receiver, Mapping) or receiver.get("ok") is not True:
            raise RuntimeError("selected receiver receipt missing")
        operations = receiver.get("operations")
        if not isinstance(operations, list):
            raise RuntimeError("selected receiver receipt operations missing")
        if expected_ops and tuple(str(item) for item in operations) != expected_ops:
            raise RuntimeError("selected receiver receipt operations mismatch")
    return {
        "schema": "pantheon-runtime-initial-viability-invocation-check/v1",
        "persona": persona,
        "invocation_id": invocation_id,
        "ok": True,
        "selected_runtime": str(selected),
        "selected_harness": str(viability.get("selected_harness", "")),
        "selected_profile": str(viability.get("selected_profile", "")),
        "provider_completion_status": str(readiness.get("provider_completion_status", "")),
        "invocation_receipt_sha256": payload_sha,
        "attempt_count": str(len(attempts)),
    }


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
    alike, since the caller (``ensure_runtime``) only reaches this from the
    branch that just called ``_start_runtime`` -- never on a plain reuse of
    an already-live pane. Persona-neutral: every command, and whether the
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
    commands are launched via ``Popen`` and never waited on, matching
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
        except (KeyError, ValueError) as exc:
            _append_controller_evidence(
                binding.persona,
                "post_start_command_skipped",
                {
                    "argv_template": " ".join(command.argv),
                    "reason": f"substitution failed: {type(exc).__name__}: {exc}",
                },
                state_root=state_root,
            )
            continue
        try:
            if command.background:
                subprocess.Popen(
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
        _append_controller_evidence(
            binding.persona,
            "post_start_command",
            {
                "argv": " ".join(argv),
                "background": str(command.background).lower(),
                "outcome": outcome,
            },
            state_root=state_root,
        )


def ensure_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeHandle:
    """Reuse an exact matching live pane or start the selected interactive profile."""

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
    if state and _state_matches(binding, state):
        # A dead pane cannot inherit health from matching persisted metadata.
        state = {}
    boundary = _utc_now()
    try:
        start_kwargs: dict[str, Any] = {"replace": False}
        if context is not None:
            start_kwargs["context"] = context
        runtime_start = _start_runtime(binding, adapter, **start_kwargs)
    except RuntimeOperationalFailure:
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
            _stop_created_scope(runtime_start.scope_unit)
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
        _verify_runtime_input_ready(binding, runtime_start, state_root=state_root, adapter=adapter)
    else:
        _verify_runtime_input_ready(
            binding,
            runtime_start,
            state_root=state_root,
            adapter=adapter,
            context=context,
        )
    provider_completion = _assess_provider_completion_after_input_ready(
        binding,
        adapter,
        state_root=state_root,
        context=context,
    )
    _raise_for_provider_completion_assessment(binding, runtime_start, provider_completion)
    if binding.controller.post_start:
        baseline = _post_start_sample(
            _capture_visible_runtime_pane(binding.controller.tmux, **_context_kwargs(context)),
            capture_index=0,
        )
        _run_post_start_commands(binding, state_root=state_root)
        assessment = _assess_post_start_after_commands(
            binding,
            adapter,
            baseline=baseline,
            context=context,
        )
        _raise_for_post_start_assessment(binding, runtime_start, assessment)
    else:
        _run_post_start_commands(binding, state_root=state_root)
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
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=started,
    )


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
    status: Literal["selected", "failed", "skipped-cooldown", "skipped-recorded-sticky"],
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


def compensate_failed_initial_candidate(
    binding: "PersonaBinding",
    *,
    failure: RuntimeOperationalFailure,
    state_root: Path | None,
    backend: Any | None = None,
    context: TmuxRuntimeContext | None = None,
    reason: str,
) -> Mapping[str, str]:
    del context
    teardown: dict[str, str] = {
        "runtime_state_cleared": "false",
        "scope_stopped": "false",
        "receiver_absent_proven": "false",
    }
    if failure.runtime_start is not None:
        _stop_created_scope(failure.runtime_start.scope_unit)
        teardown["scope_stopped"] = "true"
        teardown["runtime_scope_unit"] = failure.runtime_start.scope_unit
    _clear_stale_runtime_state(binding.persona, state_root=state_root)
    teardown["runtime_state_cleared"] = "true"
    try:
        _prove_receiver(binding, "absent", backend=backend)
        teardown["receiver_absent_proven"] = "true"
    except Exception as exc:
        teardown["receiver_absent_proven"] = "false"
        teardown["receiver_absent_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        raise
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


def _read_only_receiver_present(binding: "PersonaBinding", *, backend: Any | None) -> Mapping[str, Any]:
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
) -> tuple[Mapping[str, str], Mapping[str, str], Mapping[str, Any], str]:
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
    if recorded_hashes != current_hashes:
        _raise_runtime_viability_block(
            "recorded-source-hash-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    recorded_manifest = str(runtime_state.get("controller_source_manifest_sha256", ""))
    if recorded_manifest != current_source.source_manifest_sha256:
        _raise_runtime_viability_block(
            "recorded-source-manifest-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
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
    )
    adapter = adapter_factory(selected_binding)
    old_raw = (_persona_state(selected_binding.persona, state_root) / "runtime.json").read_bytes()
    started: RuntimeStart | None = None
    try:
        started = _start_runtime(
            selected_binding,
            adapter,
            replace=False,
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
            adapter,
            started,
            state_root=state_root,
            **_context_kwargs(context),
        )
    except Exception as exc:
        if started is not None and started.created_scope:
            _stop_created_scope(started.scope_unit)
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
            "source_equivalence_reason": "controller source hash map and manifest digest match exactly",
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
    runtime_state = _read_json(_persona_state(selected_binding.persona, state_root) / "runtime.json")
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
    if recorded_hashes != current_hashes:
        _raise_runtime_viability_block(
            "recorded-source-hash-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
    recorded_manifest = str(runtime_state.get("controller_source_manifest_sha256", ""))
    if recorded_manifest != current_source.source_manifest_sha256:
        _raise_runtime_viability_block(
            "recorded-source-manifest-mismatch",
            binding=selected_binding,
            runtime_state=runtime_state,
            state_root=state_root,
        )
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
                "controller source hash map and manifest digest match exactly"
            ),
        }
    )
    attempt = _viability_attempt(
        sequence=1,
        binding=selected_binding,
        status="skipped-recorded-sticky",
        failure_class=None,
        cause="recorded-runtime-source-equivalent",
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
    previous_state = initial_viability_state(ordered[0].persona, state_root=state_root_path)
    attempts: list[RuntimeViabilityAttempt] = []
    previous_failed: Any | None = None
    attempted = 0
    for candidate in ordered:
        if attempted >= limit:
            break
        runtime = _runtime_ref(candidate)
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
            handle = ensure_runtime(candidate, adapter, state_root=state_root_path)
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
            try:
                receiver = run_initial_bootstrap_receiver_boundary(
                    previous_failed,
                    candidate,
                    phase="compensate-selected",
                    backend=backend,
                )
                _clear_stale_runtime_state(candidate.persona, state_root=state_root_path)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"initial receiver compensation failed after {type(exc).__name__}: {exc}; "
                    f"compensation: {type(rollback_exc).__name__}: {rollback_exc}"
                ) from exc
            until = observed_now + timedelta(seconds=cooldown_seconds)
            attempts.append(
                _viability_attempt(
                    sequence=len(attempts) + 1,
                    binding=candidate,
                    status="failed",
                    failure_class="operational-readiness",
                    cause="receiver_handoff_failed",
                    readiness={"event": "receiver_handoff_failed", "detail": str(exc)[:1000]},
                    teardown={"runtime_state_cleared": "true"},
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


def submit_to_runtime(
    handle: RuntimeHandle,
    request: ExecutionRequest,
    *,
    persona: str | None = None,
    state_root: Path | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    context: TmuxRuntimeContext | None = None,
) -> DeliveryReceipt:
    """Transport a prompt into the pane without claiming turn completion/ack."""

    harness = "claude-code" if handle.profile.harness == "claude" else "codex"
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
    helper_argv += [_target_text(handle.tmux), request.prompt]
    result = subprocess.run(
        helper_argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "prompt submission was not verified"
        if persona is not None:
            _append_controller_evidence(
                persona,
                "runtime_submission_failed",
                {
                    **evidence_context,
                    "returncode": result.returncode,
                    "detail": detail[:1000],
                },
                state_root=state_root,
            )
        raise RuntimeError(detail)
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
) -> None:
    """Record structural successor-read evidence; terminal pixels are rejected."""

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
    state["phase"] = "completed"
    state["completed_at"] = _iso(_utc_now())
    _atomic_json(path, state)
    _append_controller_evidence(
        persona,
        "successor_read_acknowledged",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": state.get("incoming_harness"),
            "incoming_profile": state.get("incoming_profile"),
            "chronicle_evidence": state.get("chronicle_evidence"),
            "successor_read_evidence": evidence,
        },
        state_root=state_root,
    )


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
        return RuntimeHandle(
            profile=target.launch,
            tmux=target.controller.tmux,
            session_id=None,
            started_at=started,
        )
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


def receiver_handoff_actions(binding: Any, direction: ReceiverDirection, phase: ReceiverPhase) -> tuple[str, ...]:
    return harness_integration.receiver_handoff_actions(binding, direction, phase)


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
) -> ReceiverHandoffReceipt:
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
        str(timeout_seconds),
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
) -> ReceiverHookReceipt:
    if direction == "none":
        return ReceiverHookReceipt(True, (), {"direction": "none", "phase": phase})
    integration = harness_integration.integration_for(binding.launch.harness)
    return integration.run_receiver_handoff(binding, direction, phase, backend=backend, reason=reason)


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
    submit_to_runtime(
        handle,
        ExecutionRequest(
            prompt=(
                "Runtime transfer: read the continuity evidence at "
                f"{evidence}. After reading it, record the structural receipt "
                "with this exact command before retrying the pending mailbox "
                "record:\n"
                f"python3 {Path(__file__).resolve()} ack-read --persona "
                f"{shlex.quote(persona)} --state-root {shlex.quote(str(state_arg))} "
                f"--evidence {shlex.quote(evidence_receipt)}"
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
) -> SwapReceipt:
    """Perform or resume a cooperative swap; finish only after read acknowledgement."""

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
            next_weekly_reset(current.profile.harness, now=now or _utc_now())
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
MAX_SLEEP_CAP_SECONDS = 3600.0
# process_death / readiness_timeout / operational_failure share this backoff
# ladder (spec's failure-class table): 30s, 5min, 30min, 2h, then holds at 2h.
_TRANSIENT_FAILURE_BACKOFF_SECONDS = (30.0, 300.0, 1800.0, 7200.0)


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
        return next_weekly_reset(harness, now=now)
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
) -> dict[str, Any]:
    """Record a runtime attempt's outcome (spec SS `last-attempt.json`).

    `outcome="success"` clears `consecutive_failures` to 0 and
    `cooldown_until` to null (spec: only after the sustained-alive
    threshold -- `controller_main` is the caller that decides whether a
    death counts as success; this function just records what it's told).
    `outcome="failed"` increments `consecutive_failures` and computes
    `cooldown_until` from the failure-class table.
    """
    now = now or _utc_now()
    slug = _binding_runtime_slug(binding)
    previous = load_last_attempt(persona, slug, state_root=state_root) or {}
    if outcome == "success":
        consecutive_failures = 0
        cooldown_until: datetime | None = None
        failure_class = None
    else:
        consecutive_failures = int(previous.get("consecutive_failures") or 0) + 1
        cooldown_until = _cooldown_until_for_failure(
            failure_class or "operational_failure",
            now=now,
            consecutive_failures=consecutive_failures,
            harness=binding.launch.harness,
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


def load_holds(persona: str, *, state_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Manual holds (spec SS `overrides/holds.json`) -- keyed by
    ``<harness>/<profile>`` runtime reference. Empty dict if the file
    doesn't exist (no holds set)."""
    path = _holds_path(persona, state_root=state_root)
    if not path.exists():
        return {}
    payload = _read_json(path)
    holds = payload.get("holds")
    return dict(holds) if isinstance(holds, dict) else {}


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


def _select_viable_runtime(
    persona: str,
    candidates: Sequence["PersonaBinding"],
    holds: Mapping[str, Any],
    current_time: datetime,
    *,
    state_root: Path | None = None,
) -> tuple["PersonaBinding | None", list[dict[str, Any]]]:
    """Spec SS Controller invocation flow, step 2: first viable runtime in
    manifest order. A runtime is skipped if held, or if its recorded
    cooldown hasn't elapsed."""
    skipped: list[dict[str, Any]] = []
    for binding in candidates:
        ref = _binding_runtime_ref(binding)
        if ref in holds:
            skipped.append({"runtime": ref, "reason": "held"})
            continue
        attempt = load_last_attempt(persona, _binding_runtime_slug(binding), state_root=state_root)
        cooldown_until = _parse_iso_or_none((attempt or {}).get("cooldown_until"))
        if cooldown_until is not None and cooldown_until > current_time:
            skipped.append({"runtime": ref, "reason": f"cooldown-active-until-{_iso(cooldown_until)}"})
            continue
        return binding, skipped
    return None, skipped


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


def _controller_start_runtime(
    binding: "PersonaBinding", *, state_root: Path | None
) -> tuple[RuntimeStart, RuntimeHandle]:
    """Start `binding`'s runtime for the stateless controller loop, reusing
    the KEEP low-level primitives directly (see module-header DECISION)."""
    from tools.persona_bringup import build_adapter

    adapter = build_adapter(binding, harness_home=binding.account_home)
    runtime_start = _start_runtime(
        binding, adapter, replace=_target_known(binding.controller.tmux)
    )
    _verify_started_runtime(binding, runtime_start, state_root=state_root)
    _verify_runtime_input_ready(binding, runtime_start, state_root=state_root, adapter=adapter)
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


def block_until_tmux_death(
    binding: "PersonaBinding",
    *,
    poll_interval_seconds: float = 5.0,
    context: "TmuxRuntimeContext | None" = None,
    sleep: Callable[[float], None] = time.sleep,
    alive_check: Callable[[], bool] | None = None,
) -> None:
    """Spec SS Controller invocation flow, step 4: block as MainPID until
    the started runtime's tmux session dies. `alive_check`/`sleep` are
    test seams (consistent with the file's existing injectable-dependency
    convention -- `systemd_probe`, `context`, `backend`)."""
    is_alive = alive_check or (lambda: _runtime_alive(binding, context=context))
    while is_alive():
        sleep(poll_interval_seconds)


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

    return {
        "ActiveRuntime": active_runtime,
        "ViabilityStatus": viability_status,
        "LastSelectionTs": last_selection_ts,
        "LastFailureClass": last_failure_class,
        "NextCooldownExpiryTs": _iso(earliest) if earliest is not None else "",
        "Holds": sorted(holds.keys()),
    }


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
        return start_service(
            persona,
            lambda: dbus_health_properties(persona, state_root=state_root),
        )
    except Exception:
        return None


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
    block_until_death_fn: Callable[["PersonaBinding"], None] | None = None,
    dbus_health_service_fn: Callable[[str], Any] | None = None,
) -> int:
    """Stateless invocation entry point (T-2966; spec SS Controller
    invocation flow).

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
    block_until_death_fn = block_until_death_fn or block_until_tmux_death
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
    while True:
        cycles += 1
        if candidates is not None:
            runtime_candidates = candidates
        else:
            primary = persona_bringup.load_binding(manifest, persona)
            runtime_candidates = persona_bringup.configured_runtime_bindings(manifest, primary)

        holds = load_holds(persona, state_root=state_root)
        current_time = now()

        selected, skipped = _select_viable_runtime(
            persona, runtime_candidates, holds, current_time, state_root=state_root
        )

        if selected is None:
            log_invocation(
                persona, "no-viable-candidate", trigger=trigger, skipped=skipped,
                instruction_sha256=instruction_sha256, state_root=state_root,
            )
            _dbus_emit_health(persona, health_service)
            earliest = _earliest_cooldown_expiry(persona, runtime_candidates, holds, state_root=state_root)
            if earliest is None:
                return 1
            sleep_seconds = max(min((earliest - current_time).total_seconds(), max_sleep_cap_seconds), 0.0)
            sleep(sleep_seconds)
            if max_reselect_cycles is not None and cycles >= max_reselect_cycles:
                return 1
            continue

        selected_ref = _binding_runtime_ref(selected)
        started_at = now()
        try:
            _runtime_start, handle = start_runtime_fn(selected)
        except (RuntimeOperationalFailure, RuntimeError) as exc:
            failure_class = _classify_death(exc)
            record_last_attempt(
                persona, selected, outcome="failed", failure_class=failure_class,
                trigger=trigger, state_root=state_root, now=now(),
            )
            log_invocation(
                persona, "start-failed", trigger=trigger, selected=selected_ref,
                skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
            )
            _dbus_emit_health(persona, health_service)
            return 1

        write_active_runtime(persona, selected, handle, pid=handle.pid, state_root=state_root)
        log_invocation(
            persona, "started", trigger=trigger, selected=selected_ref,
            skipped=skipped, instruction_sha256=instruction_sha256, state_root=state_root,
        )
        _dbus_emit_health(persona, health_service)

        block_until_death_fn(selected)

        alive_seconds = (now() - started_at).total_seconds()
        if alive_seconds >= SUSTAINED_ALIVE_SECONDS:
            record_last_attempt(
                persona, selected, outcome="success", trigger=trigger, state_root=state_root, now=now()
            )
        else:
            record_last_attempt(
                persona, selected, outcome="failed", failure_class="process_death",
                trigger=trigger, state_root=state_root, now=now(),
            )
        clear_active_runtime(persona, state_root=state_root)
        _dbus_emit_health(persona, health_service)
        return 0

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
    args = parser.parse_args(argv)
    if args.command == "ack-read":
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
        )
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
