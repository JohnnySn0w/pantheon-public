from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Literal, Mapping, Protocol

from tools import runtime_controller
from tools.germinate.planner_sandbox.wal import (
    BytesPayload,
    EffectObservation,
    EffectState,
    IntentRecord,
    PrefixClassification,
    ReconcileError,
    RunReceiptEntry,
    StructuralPrefix,
    WalStore,
    canonical_json_bytes,
)
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.types import TmuxTarget
from tools.persona_bringup import PersonaBinding
from tools.runtime_controller import RuntimeHandle, TmuxRuntimeContext
from tools.submit_verify import ready_for_submission


RuntimeEffectOperation = Literal["tools.runtime_controller.ensure_runtime"]
RuntimeStateSchema = Literal["pantheon-runtime-state/v1"]

RUNTIME_EFFECT_OPERATION: Final[RuntimeEffectOperation] = "tools.runtime_controller.ensure_runtime"
RUNTIME_STATE_SCHEMA: Final[RuntimeStateSchema] = "pantheon-runtime-state/v1"


class RuntimeEffectStatus(Enum):
    NOT_OBSERVED = "not_observed"
    DEAD_MATCHING = "dead_matching"
    LIVE_MATCHING = "live_matching"
    DEAD_MISMATCHED = "dead_mismatched"
    LIVE_MISMATCHED = "live_mismatched"
    PANE_PID_MISMATCH = "pane_pid_mismatch"
    SERVER_SCOPE_MISMATCH = "server_scope_mismatch"
    UNKNOWN = "unknown"


class RuntimeEffectDecision(Enum):
    REUSE = "reuse"
    COMPLETE = "complete"
    RETRY = "retry"
    COMPENSATION_REQUIRED = "compensation_required"
    REFUSE = "refuse"


class RuntimeStateReadStatus(Enum):
    ABSENT = "absent"
    VALID_BYTES = "valid_bytes"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeEffectIdentity:
    operation: RuntimeEffectOperation
    wal_op_id: str
    persona: str
    role: str
    account: str
    harness: str
    profile: str
    executable: str
    model: str | None
    tmux_target: str
    runtime_scope_unit: str
    runtime_state_path_sha256: str


@dataclass(frozen=True)
class ObservedRuntimeIdentity:
    schema: RuntimeStateSchema
    persona: str
    role: str
    harness: str
    profile: str
    executable: str
    model: str | None
    tmux_target: str
    runtime_scope_unit: str


@dataclass(frozen=True)
class RuntimeStateReadResult:
    status: RuntimeStateReadStatus
    path: Path
    raw_bytes: bytes | None
    reason: str


@dataclass(frozen=True)
class RuntimeEffectObservation:
    status: RuntimeEffectStatus
    observed_at: str
    runtime_state_bytes_sha256: str | None
    runtime_state: Mapping[str, object] | None
    observed_identity: ObservedRuntimeIdentity | None
    session_id: str | None
    parsed_started_at: datetime | None
    pane_exists: bool | None
    pane_pid: int | None
    pane_pid_alive: bool | None
    tmux_server_pid: int | None
    server_in_scope: bool | None
    readiness_ready: bool
    readiness_reason: str
    pane_sha256: str | None
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class RuntimeEffectResult:
    decision: RuntimeEffectDecision
    reason: str
    identity: RuntimeEffectIdentity
    observation: RuntimeEffectObservation
    intent_ref: str | None
    intent_digest: str | None
    receipt_ref: str | None
    manifest_entry: RunReceiptEntry | None
    handle: RuntimeHandle | None
    prefix: StructuralPrefix


class EnsureRuntimeFn(Protocol):
    def __call__(
        self,
        binding: PersonaBinding,
        adapter: PantheonHarnessAdapter,
        *,
        state_root: Path | None = None,
        context: TmuxRuntimeContext | None = None,
    ) -> RuntimeHandle: ...


class RuntimeStatePathFn(Protocol):
    def __call__(self, persona: str, state_root: Path | None = None) -> Path: ...


class PaneExistsFn(Protocol):
    def __call__(self, target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool: ...


class PanePidFn(Protocol):
    def __call__(self, target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> int | None: ...


class ServerPidFn(Protocol):
    def __call__(self, *, context: TmuxRuntimeContext | None = None) -> int | None: ...


class PidInScopeFn(Protocol):
    def __call__(self, pid: int, scope_unit: str) -> bool: ...


class CapturePaneFn(Protocol):
    def __call__(self, target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> str: ...


ReadinessFn = Callable[[str], bool]


def expected_runtime_identity(
    binding: PersonaBinding,
    *,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeEffectIdentity:
    state_path = _resolved_runtime_state_path(binding, state_root=state_root)
    return RuntimeEffectIdentity(
        operation=RUNTIME_EFFECT_OPERATION,
        wal_op_id=_wal_op_id(binding),
        persona=binding.persona,
        role=binding.role,
        account=binding.account,
        harness=binding.launch.harness,
        profile=binding.launch.profile,
        executable=str(binding.launch.executable),
        model=binding.launch.model,
        tmux_target=_binding_tmux_target(binding),
        runtime_scope_unit=_scope_unit(context),
        runtime_state_path_sha256=_sha256_text(str(state_path.resolve(strict=False))),
    )


def runtime_effect_identity_digest(identity: RuntimeEffectIdentity) -> str:
    return _sha256_bytes(canonical_json_bytes(asdict(identity)))


def observed_identity_matches_expected(
    observed: ObservedRuntimeIdentity | None,
    expected: RuntimeEffectIdentity,
) -> bool:
    if observed is None:
        return False
    return (
        observed.schema == RUNTIME_STATE_SCHEMA
        and observed.persona == expected.persona
        and observed.role == expected.role
        and observed.harness == expected.harness
        and observed.profile == expected.profile
        and observed.executable == expected.executable
        and observed.model == expected.model
        and observed.tmux_target == expected.tmux_target
        and observed.runtime_scope_unit == expected.runtime_scope_unit
    )


class RuntimeControllerProbe:
    def __init__(
        self,
        binding: PersonaBinding,
        *,
        state_root: Path | None = None,
        context: TmuxRuntimeContext | None = None,
        now: Callable[[], datetime] | None = None,
        runtime_state_path_fn: RuntimeStatePathFn | None = None,
        pane_exists_fn: PaneExistsFn | None = None,
        pane_pid_fn: PanePidFn | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None,
        server_pid_fn: ServerPidFn | None = None,
        pid_in_scope_fn: PidInScopeFn | None = None,
        capture_pane_fn: CapturePaneFn | None = None,
        readiness_fn: ReadinessFn | None = None,
    ) -> None:
        self._binding = binding
        self._state_root = state_root
        self._context = context
        self._now = now
        self._runtime_state_path_fn = runtime_state_path_fn or _runtime_state_path
        self._pane_exists_fn = pane_exists_fn or runtime_controller.pane_exists
        self._pane_pid_fn = pane_pid_fn or runtime_controller._pane_process_pid
        self._pid_alive_fn = pid_alive_fn or runtime_controller._pid_alive
        self._server_pid_fn = server_pid_fn or runtime_controller._default_tmux_server_pid
        self._pid_in_scope_fn = pid_in_scope_fn or runtime_controller._pid_in_scope
        self._capture_pane_fn = capture_pane_fn or runtime_controller._capture_visible_runtime_pane
        self._readiness_fn = readiness_fn or ready_for_submission

    def observe_identity(self, expected: RuntimeEffectIdentity) -> RuntimeEffectObservation:
        observed_at = _observed_at(self._now)
        path = self._runtime_state_path_fn(self._binding.persona, self._state_root) / "runtime.json"
        read = _read_runtime_state_bytes(path)
        if read.status is RuntimeStateReadStatus.ABSENT:
            return _unknown_or_absent_observation(
                RuntimeEffectStatus.NOT_OBSERVED,
                observed_at=observed_at,
                reason=read.reason,
                path=path,
            )
        if read.status is RuntimeStateReadStatus.UNKNOWN or read.raw_bytes is None:
            return _unknown_or_absent_observation(
                RuntimeEffectStatus.UNKNOWN,
                observed_at=observed_at,
                reason=read.reason,
                path=path,
            )
        try:
            state = _parse_provider_state(read.raw_bytes)
            observed = _observed_identity_from_state(state)
            parsed_started_at = _parse_started_at(state["started_at"])
            raw_session_id = state["session_id"]
            session_id = raw_session_id if isinstance(raw_session_id, str) else None
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            return _unknown_or_absent_observation(
                RuntimeEffectStatus.UNKNOWN,
                observed_at=observed_at,
                reason=f"invalid-runtime-state:{exc}",
                path=path,
            )

        digest = _sha256_bytes(read.raw_bytes)
        matches = observed_identity_matches_expected(observed, expected)
        try:
            pane_exists = self._pane_exists_fn(self._binding.controller.tmux, context=self._context)
            if not pane_exists:
                return RuntimeEffectObservation(
                    status=RuntimeEffectStatus.DEAD_MATCHING if matches else RuntimeEffectStatus.DEAD_MISMATCHED,
                    observed_at=observed_at,
                    runtime_state_bytes_sha256=digest,
                    runtime_state=state,
                    observed_identity=observed,
                    session_id=session_id,
                    parsed_started_at=parsed_started_at,
                    pane_exists=False,
                    pane_pid=None,
                    pane_pid_alive=None,
                    tmux_server_pid=None,
                    server_in_scope=None,
                    readiness_ready=False,
                    readiness_reason="pane-not-live",
                    pane_sha256=None,
                    evidence={"runtime_state_path": str(path), "reason": "pane-not-live"},
                )

            pane_pid = self._pane_pid_fn(self._binding.controller.tmux, context=self._context)
            pane_pid_alive = self._pid_alive_fn(pane_pid) if pane_pid is not None else False
            server_pid = self._server_pid_fn(context=self._context)
            server_in_scope = (
                self._pid_in_scope_fn(server_pid, expected.runtime_scope_unit)
                if server_pid is not None
                else False
            )
            pane_text = self._capture_pane_fn(self._binding.controller.tmux, context=self._context)
            pane_sha256 = _sha256_text(pane_text)
            readiness_ready = bool(self._readiness_fn(pane_text))
        except Exception as exc:
            return _unknown_or_absent_observation(
                RuntimeEffectStatus.UNKNOWN,
                observed_at=observed_at,
                reason=f"probe-error:{exc}",
                path=path,
            )

        readiness_reason = f"ready_for_submission:{str(readiness_ready).lower()}"
        if not matches:
            status = RuntimeEffectStatus.LIVE_MISMATCHED
        elif not pane_pid_alive:
            status = RuntimeEffectStatus.PANE_PID_MISMATCH
        elif not server_in_scope:
            status = RuntimeEffectStatus.SERVER_SCOPE_MISMATCH
        elif not readiness_ready:
            status = RuntimeEffectStatus.UNKNOWN
        else:
            status = RuntimeEffectStatus.LIVE_MATCHING
        return RuntimeEffectObservation(
            status=status,
            observed_at=observed_at,
            runtime_state_bytes_sha256=digest,
            runtime_state=state,
            observed_identity=observed,
            session_id=session_id,
            parsed_started_at=parsed_started_at,
            pane_exists=True,
            pane_pid=pane_pid,
            pane_pid_alive=pane_pid_alive,
            tmux_server_pid=server_pid,
            server_in_scope=server_in_scope,
            readiness_ready=readiness_ready,
            readiness_reason=readiness_reason,
            pane_sha256=pane_sha256,
            evidence={
                "runtime_state_path": str(path),
                "runtime_state_bytes_sha256": digest,
                "pane_sha256": pane_sha256,
                "readiness_reason": readiness_reason,
            },
        )


def apply_ensure_runtime_effect(
    store: WalStore,
    binding: PersonaBinding,
    adapter: PantheonHarnessAdapter,
    *,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
    ensure_runtime_fn: EnsureRuntimeFn | None = None,
    probe: RuntimeControllerProbe | None = None,
    now: Callable[[], datetime] | None = None,
) -> RuntimeEffectResult:
    identity = expected_runtime_identity(binding, state_root=state_root, context=context)
    active_probe = probe or RuntimeControllerProbe(binding, state_root=state_root, context=context, now=now)
    observation = active_probe.observe_identity(identity)
    classification = _classify(store, identity, observation)
    decision = _decision_for(classification.structural_prefix, observation.status)
    initial_prefix = classification.structural_prefix

    if decision is RuntimeEffectDecision.COMPENSATION_REQUIRED:
        return _result(decision, "compensation-required", identity, observation, classification, binding)
    if decision is RuntimeEffectDecision.REFUSE:
        return _result(decision, "refuse", identity, observation, classification, binding)

    if classification.structural_prefix is StructuralPrefix.BEFORE_INTENT:
        intent_ref, intent_digest = _write_intent(store, identity, now=now)
        classification = _classify(store, identity, observation)
        classification = PrefixClassification(
            run_key=classification.run_key,
            op_id=classification.op_id,
            structural_prefix=classification.structural_prefix,
            effect_state=classification.effect_state,
            intent_ref=intent_ref,
            intent_digest=intent_digest,
            receipt_ref=classification.receipt_ref,
            manifest_entry=classification.manifest_entry,
            reason=classification.reason,
            success_authorized=classification.success_authorized,
        )

    if _apply_may_call_provider(initial_prefix, observation.status):
        provider = _resolve_ensure_runtime_fn(ensure_runtime_fn)
        provider(binding, adapter, state_root=state_root, context=context)
        fresh = active_probe.observe_identity(identity)
        fresh_classification = _classify(store, identity, fresh)
        fresh_decision = _decision_for(fresh_classification.structural_prefix, fresh.status)
        if fresh.status is not RuntimeEffectStatus.LIVE_MATCHING:
            return _result(fresh_decision, "provider-returned-without-live-match", identity, fresh, fresh_classification, binding)
        return _complete(store, identity, fresh, fresh_classification, binding=binding, now=now)

    if decision is RuntimeEffectDecision.REUSE and initial_prefix is StructuralPrefix.BEFORE_INTENT:
        provider = _resolve_ensure_runtime_fn(ensure_runtime_fn)
        provider(binding, adapter, state_root=state_root, context=context)
        fresh = active_probe.observe_identity(identity)
        fresh_classification = _classify(store, identity, fresh)
        if fresh.status is not RuntimeEffectStatus.LIVE_MATCHING:
            return _result(
                _decision_for(fresh_classification.structural_prefix, fresh.status),
                "reuse-provider-returned-without-live-match",
                identity,
                fresh,
                fresh_classification,
                binding,
            )
        return _complete(store, identity, fresh, fresh_classification, binding=binding, now=now)

    if decision is RuntimeEffectDecision.COMPLETE:
        return _complete(store, identity, observation, classification, binding=binding, now=now)
    if decision is RuntimeEffectDecision.REUSE:
        return _result(decision, "reuse", identity, observation, classification, binding)
    return _result(decision, "retry", identity, observation, classification, binding)


def reconcile_runtime_effect(
    store: WalStore,
    binding: PersonaBinding,
    *,
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
    probe: RuntimeControllerProbe | None = None,
    now: Callable[[], datetime] | None = None,
) -> RuntimeEffectResult:
    identity = expected_runtime_identity(binding, state_root=state_root, context=context)
    active_probe = probe or RuntimeControllerProbe(binding, state_root=state_root, context=context, now=now)
    observation = active_probe.observe_identity(identity)
    classification = _classify(store, identity, observation)
    decision = _decision_for(classification.structural_prefix, observation.status)

    if classification.structural_prefix is StructuralPrefix.BEFORE_INTENT:
        if decision is RuntimeEffectDecision.REUSE:
            return _result(decision, "reuse-current-handle-without-backfill", identity, observation, classification, binding)
        return _result(decision, decision.value, identity, observation, classification, binding)
    if decision is RuntimeEffectDecision.COMPLETE:
        return _complete(store, identity, observation, classification, binding=binding, now=now)
    return _result(decision, decision.value, identity, observation, classification, binding)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed_at(now: Callable[[], datetime] | None) -> str:
    return _iso((now or _utc_now)())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_state_path(persona: str, state_root: Path | None = None) -> Path:
    return runtime_controller._persona_state(persona, state_root)


def _resolved_runtime_state_path(binding: PersonaBinding, *, state_root: Path | None) -> Path:
    return _runtime_state_path(binding.persona, state_root) / "runtime.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_payload_bytes(payload: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(dict(payload))


def _binding_tmux_target(binding: PersonaBinding) -> str:
    return runtime_controller.runtime_target_text(binding.controller.tmux)


def _scope_unit(context: TmuxRuntimeContext | None) -> str:
    return context.scope_unit if context is not None else runtime_controller.TMUX_RUNTIME_SCOPE_UNIT


def _wal_op_id(binding: PersonaBinding) -> str:
    raw = f"runtime-{binding.persona}"
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in raw)
    return safe[:64]


def _read_runtime_state_bytes(path: Path) -> RuntimeStateReadResult:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return RuntimeStateReadResult(RuntimeStateReadStatus.ABSENT, path, None, "runtime-json-absent")
    except OSError as exc:
        return RuntimeStateReadResult(RuntimeStateReadStatus.UNKNOWN, path, None, f"open-failed:{exc.errno}")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return RuntimeStateReadResult(RuntimeStateReadStatus.UNKNOWN, path, None, "not-regular")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
        after = os.fstat(fd)
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return RuntimeStateReadResult(RuntimeStateReadStatus.UNKNOWN, path, None, "racy-missing-after-read")
        before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        path_key = (path_stat.st_dev, path_stat.st_ino, path_stat.st_size, path_stat.st_mtime_ns)
        if before_key != after_key or after_key != path_key:
            return RuntimeStateReadResult(RuntimeStateReadStatus.UNKNOWN, path, None, "racy-runtime-json")
        return RuntimeStateReadResult(RuntimeStateReadStatus.VALID_BYTES, path, raw, "valid")
    except OSError as exc:
        return RuntimeStateReadResult(RuntimeStateReadStatus.UNKNOWN, path, None, f"read-failed:{exc.errno}")
    finally:
        os.close(fd)


def _parse_provider_state(raw: bytes) -> Mapping[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate-key:{key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid-json:{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("runtime-state-not-object")
    return parsed


def _parse_started_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("started-at-not-string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("started-at-invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("started-at-naive")
    return parsed


def _observed_identity_from_state(state: Mapping[str, object]) -> ObservedRuntimeIdentity:
    if state.get("schema") != RUNTIME_STATE_SCHEMA:
        raise ValueError("schema-mismatch")
    required = ("persona", "role", "harness", "profile", "executable", "tmux_target", "runtime_scope_unit")
    values: dict[str, str] = {}
    for key in required:
        value = state.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key}-not-string")
        values[key] = value
    model = state.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("model-not-string-or-null")
    session_id = state.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session-id-not-string-or-null")
    _parse_started_at(state.get("started_at"))
    return ObservedRuntimeIdentity(
        schema=RUNTIME_STATE_SCHEMA,
        persona=values["persona"],
        role=values["role"],
        harness=values["harness"],
        profile=values["profile"],
        executable=values["executable"],
        model=model,
        tmux_target=values["tmux_target"],
        runtime_scope_unit=values["runtime_scope_unit"],
    )


def _observation_to_wal_effect(observation: RuntimeEffectObservation) -> EffectObservation:
    if observation.status is RuntimeEffectStatus.LIVE_MATCHING:
        return EffectObservation(applied=True, postcondition_satisfied=True, evidence=observation.evidence)
    if observation.status in {
        RuntimeEffectStatus.LIVE_MISMATCHED,
        RuntimeEffectStatus.PANE_PID_MISMATCH,
        RuntimeEffectStatus.SERVER_SCOPE_MISMATCH,
    }:
        return EffectObservation(applied=True, postcondition_satisfied=False, evidence=observation.evidence)
    if observation.status in {
        RuntimeEffectStatus.NOT_OBSERVED,
        RuntimeEffectStatus.DEAD_MATCHING,
        RuntimeEffectStatus.DEAD_MISMATCHED,
    }:
        return EffectObservation(applied=False, postcondition_satisfied=False, evidence=observation.evidence)
    return EffectObservation(applied=None, postcondition_satisfied=None, evidence=observation.evidence)


def _intent_preimage(identity: RuntimeEffectIdentity) -> BytesPayload:
    payload = asdict(identity)
    raw = _canonical_payload_bytes(payload)
    return BytesPayload(
        kind="bytes",
        bytes_b64=base64.b64encode(raw).decode("ascii"),
        sha256=_sha256_bytes(raw),
        payload=payload,
    )


def _unsupported_inverse(identity: RuntimeEffectIdentity) -> BytesPayload:
    payload: Mapping[str, object] = {
        "schema": "runtime-effect-inverse/v1",
        "operation": identity.operation,
        "wal_op_id": identity.wal_op_id,
        "compensation": "unsupported",
    }
    raw = _canonical_payload_bytes(payload)
    return BytesPayload(
        kind="bytes",
        bytes_b64=base64.b64encode(raw).decode("ascii"),
        sha256=_sha256_bytes(raw),
        payload=payload,
    )


def _receipt_payload(identity: RuntimeEffectIdentity, observation: RuntimeEffectObservation) -> Mapping[str, object]:
    return {
        "schema": "runtime-effect-receipt/v1",
        "identity_digest": runtime_effect_identity_digest(identity),
        "runtime_state_bytes_sha256": observation.runtime_state_bytes_sha256,
        "observed_at": observation.observed_at,
        "session_id": observation.session_id,
        "started_at": _iso(observation.parsed_started_at) if observation.parsed_started_at is not None else None,
        "pane_pid": observation.pane_pid,
        "pane_sha256": observation.pane_sha256,
        "readiness_ready": observation.readiness_ready,
        "readiness_reason": observation.readiness_reason,
    }


def _postcondition_result(identity: RuntimeEffectIdentity, observation: RuntimeEffectObservation) -> Mapping[str, object]:
    return {
        "satisfied": True,
        "identity_digest": runtime_effect_identity_digest(identity),
        "runtime_state_bytes_sha256": observation.runtime_state_bytes_sha256,
        "observed_at": observation.observed_at,
        "status": observation.status.value,
    }


def _write_intent(
    store: WalStore,
    identity: RuntimeEffectIdentity,
    *,
    now: Callable[[], datetime] | None,
) -> tuple[str, str]:
    return store.write_intent(
        op_id=identity.wal_op_id,
        effect_domain="external",
        preimage=_intent_preimage(identity),
        inverse=_unsupported_inverse(identity),
        postcondition_check={
            "schema": "runtime-effect-postcondition/v1",
            "identity_digest": runtime_effect_identity_digest(identity),
            "required_status": RuntimeEffectStatus.LIVE_MATCHING.value,
        },
        created_at=_observed_at(now),
    )


def _write_receipt(
    store: WalStore,
    classification: PrefixClassification,
    identity: RuntimeEffectIdentity,
    observation: RuntimeEffectObservation,
    *,
    now: Callable[[], datetime] | None,
) -> tuple[str, str]:
    if observation.status is not RuntimeEffectStatus.LIVE_MATCHING:
        raise RuntimeError("receipt-requires-live-matching-observation")
    if classification.intent_ref is None or classification.intent_digest is None:
        raise RuntimeError("receipt-requires-intent")
    return store.write_receipt(
        op_id=identity.wal_op_id,
        intent_ref=classification.intent_ref,
        intent_digest=classification.intent_digest,
        postcondition_result=_postcondition_result(identity, observation),
        receipt_payload=_receipt_payload(identity, observation),
        committed_at=_observed_at(now),
    )


def _commit_manifest(
    store: WalStore,
    identity: RuntimeEffectIdentity,
    receipt_ref: str,
    *,
    now: Callable[[], datetime] | None,
) -> RunReceiptEntry:
    committed_at = _observed_at(now)
    return store.commit_receipt_reference(
        op_id=identity.wal_op_id,
        receipt_ref=receipt_ref,
        committed_at=committed_at,
        updated_at=committed_at,
    )


class _IntentValidationError(ReconcileError):
    pass


class _ValidatingWalProbe:
    def __init__(self, identity: RuntimeEffectIdentity, observation: RuntimeEffectObservation) -> None:
        self._identity = identity
        self._observation = observation

    def observe(self, intent: IntentRecord) -> EffectObservation:
        _validate_intent_record(intent, self._identity)
        return _observation_to_wal_effect(self._observation)


def _validate_intent_record(intent: IntentRecord, identity: RuntimeEffectIdentity) -> None:
    if intent.op_id != identity.wal_op_id:
        raise _IntentValidationError("intent-op-id-mismatch")
    if intent.effect_domain != "external":
        raise _IntentValidationError("intent-effect-domain-mismatch")
    payload = _decode_payload(intent.preimage, "intent-preimage")
    if payload != asdict(identity):
        raise _IntentValidationError("intent-identity-payload-mismatch")
    if _sha256_bytes(_canonical_payload_bytes(payload)) != runtime_effect_identity_digest(identity):
        raise _IntentValidationError("intent-identity-digest-mismatch")
    inverse = _decode_payload(intent.inverse, "intent-inverse")
    expected_inverse: Mapping[str, object] = {
        "schema": "runtime-effect-inverse/v1",
        "operation": identity.operation,
        "wal_op_id": identity.wal_op_id,
        "compensation": "unsupported",
    }
    if inverse != expected_inverse:
        raise _IntentValidationError("intent-inverse-mismatch")
    expected_postcondition: Mapping[str, object] = {
        "schema": "runtime-effect-postcondition/v1",
        "identity_digest": runtime_effect_identity_digest(identity),
        "required_status": RuntimeEffectStatus.LIVE_MATCHING.value,
    }
    if dict(intent.postcondition_check) != dict(expected_postcondition):
        raise _IntentValidationError("intent-postcondition-mismatch")


def _decode_payload(payload: BytesPayload, label: str) -> Mapping[str, object]:
    if payload.kind != "bytes":
        raise _IntentValidationError(f"{label}-kind-mismatch")
    try:
        raw = base64.b64decode(payload.bytes_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise _IntentValidationError(f"{label}-base64-invalid") from exc
    if _sha256_bytes(raw) != payload.sha256:
        raise _IntentValidationError(f"{label}-digest-mismatch")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _IntentValidationError(f"{label}-json-invalid") from exc
    if not isinstance(decoded, dict):
        raise _IntentValidationError(f"{label}-not-object")
    if payload.payload is None or dict(payload.payload) != decoded:
        raise _IntentValidationError(f"{label}-payload-mismatch")
    return decoded


def _classify(
    store: WalStore,
    identity: RuntimeEffectIdentity,
    observation: RuntimeEffectObservation,
) -> PrefixClassification:
    try:
        return store.classify_prefix(op_id=identity.wal_op_id, probe=_ValidatingWalProbe(identity, observation))
    except ReconcileError:
        return PrefixClassification(
            run_key="unknown",
            op_id=identity.wal_op_id,
            structural_prefix=StructuralPrefix.REFUSE,
            effect_state=EffectState.UNKNOWN_REFUSE,
            intent_ref=None,
            intent_digest=None,
            receipt_ref=None,
            manifest_entry=None,
            reason="reconcile-error",
            success_authorized=False,
        )


def _handle_from_observation(
    binding: PersonaBinding,
    observation: RuntimeEffectObservation,
) -> RuntimeHandle | None:
    if observation.status is not RuntimeEffectStatus.LIVE_MATCHING or observation.parsed_started_at is None:
        return None
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=observation.session_id,
        started_at=observation.parsed_started_at,
        pid=observation.pane_pid,
    )


def _resolve_ensure_runtime_fn(ensure_runtime_fn: EnsureRuntimeFn | None) -> EnsureRuntimeFn:
    if ensure_runtime_fn is not None:
        return ensure_runtime_fn
    return runtime_controller.ensure_runtime


def _decision_for(prefix: StructuralPrefix, status: RuntimeEffectStatus) -> RuntimeEffectDecision:
    if prefix is StructuralPrefix.REFUSE:
        return RuntimeEffectDecision.REFUSE
    if status is RuntimeEffectStatus.UNKNOWN:
        return RuntimeEffectDecision.REFUSE
    if status in {
        RuntimeEffectStatus.DEAD_MISMATCHED,
        RuntimeEffectStatus.LIVE_MISMATCHED,
        RuntimeEffectStatus.PANE_PID_MISMATCH,
        RuntimeEffectStatus.SERVER_SCOPE_MISMATCH,
    }:
        return RuntimeEffectDecision.COMPENSATION_REQUIRED
    if prefix is StructuralPrefix.BEFORE_INTENT:
        if status is RuntimeEffectStatus.LIVE_MATCHING:
            return RuntimeEffectDecision.REUSE
        return RuntimeEffectDecision.RETRY
    if prefix is StructuralPrefix.INTENT_ONLY:
        if status is RuntimeEffectStatus.LIVE_MATCHING:
            return RuntimeEffectDecision.COMPLETE
        return RuntimeEffectDecision.RETRY
    if prefix is StructuralPrefix.APPLIED_NO_RECEIPT:
        return RuntimeEffectDecision.COMPLETE if status is RuntimeEffectStatus.LIVE_MATCHING else RuntimeEffectDecision.REFUSE
    if prefix is StructuralPrefix.ORPHAN_RECEIPT:
        return RuntimeEffectDecision.COMPLETE if status is RuntimeEffectStatus.LIVE_MATCHING else RuntimeEffectDecision.REFUSE
    if prefix is StructuralPrefix.MANIFEST_REFERENCED:
        return RuntimeEffectDecision.REUSE if status is RuntimeEffectStatus.LIVE_MATCHING else RuntimeEffectDecision.REFUSE
    return RuntimeEffectDecision.REFUSE


def _apply_may_call_provider(prefix: StructuralPrefix, status: RuntimeEffectStatus) -> bool:
    return (
        (prefix is StructuralPrefix.BEFORE_INTENT and status in {RuntimeEffectStatus.NOT_OBSERVED, RuntimeEffectStatus.DEAD_MATCHING})
        or (prefix is StructuralPrefix.INTENT_ONLY and status in {RuntimeEffectStatus.NOT_OBSERVED, RuntimeEffectStatus.DEAD_MATCHING})
    )


def _complete(
    store: WalStore,
    identity: RuntimeEffectIdentity,
    observation: RuntimeEffectObservation,
    classification: PrefixClassification,
    *,
    binding: PersonaBinding,
    now: Callable[[], datetime] | None,
) -> RuntimeEffectResult:
    if classification.structural_prefix is StructuralPrefix.ORPHAN_RECEIPT and classification.receipt_ref is not None:
        receipt_ref = classification.receipt_ref
    elif classification.structural_prefix is StructuralPrefix.MANIFEST_REFERENCED:
        return _result(RuntimeEffectDecision.REUSE, "reuse", identity, observation, classification, binding)
    else:
        receipt_ref, _receipt_digest = _write_receipt(store, classification, identity, observation, now=now)
    manifest_entry = _commit_manifest(store, identity, receipt_ref, now=now)
    updated = PrefixClassification(
        run_key=classification.run_key,
        op_id=classification.op_id,
        structural_prefix=StructuralPrefix.MANIFEST_REFERENCED,
        effect_state=classification.effect_state,
        intent_ref=classification.intent_ref,
        intent_digest=classification.intent_digest,
        receipt_ref=receipt_ref,
        manifest_entry=manifest_entry,
        reason="manifest-referenced",
        success_authorized=True,
    )
    return _result(RuntimeEffectDecision.COMPLETE, "complete", identity, observation, updated, binding)


def _result(
    decision: RuntimeEffectDecision,
    reason: str,
    identity: RuntimeEffectIdentity,
    observation: RuntimeEffectObservation,
    classification: PrefixClassification,
    binding: PersonaBinding | None,
) -> RuntimeEffectResult:
    return RuntimeEffectResult(
        decision=decision,
        reason=reason,
        identity=identity,
        observation=observation,
        intent_ref=classification.intent_ref,
        intent_digest=classification.intent_digest,
        receipt_ref=classification.receipt_ref,
        manifest_entry=classification.manifest_entry,
        handle=_handle_from_observation(binding, observation) if binding is not None else None,
        prefix=classification.structural_prefix,
    )


def _unknown_or_absent_observation(
    status: RuntimeEffectStatus,
    *,
    observed_at: str,
    reason: str,
    path: Path,
) -> RuntimeEffectObservation:
    return RuntimeEffectObservation(
        status=status,
        observed_at=observed_at,
        runtime_state_bytes_sha256=None,
        runtime_state=None,
        observed_identity=None,
        session_id=None,
        parsed_started_at=None,
        pane_exists=None,
        pane_pid=None,
        pane_pid_alive=None,
        tmux_server_pid=None,
        server_in_scope=None,
        readiness_ready=False,
        readiness_reason=reason,
        pane_sha256=None,
        evidence={"runtime_state_path": str(path), "reason": reason},
    )
