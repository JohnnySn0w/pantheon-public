"""Wire-tier contract of ``org.pantheon.HostExecutor1`` (T-2917 5b).

Imports no dbus binding (testable without dbus-python) and nothing from the
broker journal. Shared by the executor service and the broker-side client.

Every name here is PROVISIONAL-PENDING-RATIFICATION (T-2917 ruling i3; bus
name scheme ruling i5): ORG-052 section 3 pins the signatures
``Execute "ss" -> "s"`` and ``ExecutionGet "s" -> "ss"`` but not the strings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Mapping

from tools.host_control.contracts import (
    HostControlError,
    Unsupported,
    _evidence_from_dict,
    _mutation_from_dict,
    canonical_json,
    resolve_error_name,
)
from tools.persona_installer_contracts import MutationRecord, OperationEvidence

BUS_NAME_PREFIX: Final = "org.pantheon.HostExecutor1"
OBJECT_PATH: Final = "/org/pantheon/HostExecutor1"
EXECUTOR_INTERFACE: Final = "org.pantheon.HostExecutor1.Executor"
ERROR_NAMESPACE: Final = "org.pantheon.HostExecutor1.Error"

DENIED_ERROR: Final = f"{ERROR_NAMESPACE}.Denied"
BUSY_ERROR: Final = f"{ERROR_NAMESPACE}.Busy"
UNKNOWN_EXECUTION_ERROR: Final = f"{ERROR_NAMESPACE}.UnknownExecution"
INVALID_SPEC_ERROR: Final = f"{ERROR_NAMESPACE}.InvalidSpec"
UNSUPPORTED_ERROR: Final = f"{ERROR_NAMESPACE}.Unsupported"
FAILED_ERROR: Final = f"{ERROR_NAMESPACE}.Failed"
INTERNAL_ERROR: Final = f"{ERROR_NAMESPACE}.Internal"

CLOSED_ERROR_NAMES: Final = frozenset(
    (
        DENIED_ERROR,
        BUSY_ERROR,
        UNKNOWN_EXECUTION_ERROR,
        INVALID_SPEC_ERROR,
        UNSUPPORTED_ERROR,
        FAILED_ERROR,
        INTERNAL_ERROR,
    )
)

EXECUTION_RESULT_SCHEMA: Final = "pantheon-host-execution-result/v1"
# T-2917 5d.1 (gap m3, ruling T-2917:D14): ``refused`` is a PRE-ENGINE
# refusal (broker identity re-check, spec fetch, spec parse): the engine
# never ran, so the executor touched nothing. The broker classifies it from
# its own journal (was the spec served after the latest dispatch?), so a
# "spec already served" refusal is never recorded as a clean failure.
EXECUTION_STATES: Final = frozenset(("running", "succeeded", "failed", "refused"))
TERMINAL_EXECUTION_STATES: Final = frozenset(("succeeded", "failed", "refused"))
RESULT_JSON_LIMIT: Final = 1024 * 1024
# Review R1, the size arithmetic. Two hard 1 MiB ceilings sit downstream of
# an execution's records:
#   * this result JSON (RESULT_JSON_LIMIT), = records + ~0.3 KiB envelope;
#   * the broker's receipt_json CHECK (journal, 1048576 bytes), = the SAME
#     mutation/evidence dicts under the same canonical_json + ~1 KiB
#     envelope (handles, digests, subject, revisions, timestamps).
# The executor engine therefore budgets the canonical-JSON size of its
# records at RECORD_BUDGET_BYTES = 768 KiB and refuses an operation BEFORE
# it commits when its projected record would not fit. That leaves >= 255
# KiB of headroom under both ceilings for the two envelopes, list
# separators and the per-operation evidence reserve below.
RECORD_BUDGET_BYTES: Final = 768 * 1024
# T-2917 5c.2: one execution's wall-clock cap. The broker driver's collect
# timeout (tools/host_control/driver.py DEFAULT_COLLECT_TIMEOUT, 3600 s) is
# deliberately larger, so the broker never gives up on an execution the
# executor is still entitled to finish; a test pins the ordering.
#
# A HARD deadline since 5d.1 (review L3, ruling T-2917:D11), in two layers:
#   (a) the shared clamp: the engine checks the deadline between operations,
#       refuses up front a unit-fire whose budget would cross it, passes
#       every timed port call a timeout within the remaining time, and binds
#       the deadline into the user-bus adapter (``bind_deadline``), which
#       clamps every call and job wait, timed or not, and refuses a call once
#       the deadline has passed; the direct-fire child gets min(120 s, left).
#       An operation that starts just before the deadline therefore overruns
#       it by at most one clamped call;
#   (b) the watchdog backstop: the executor service arms a timer per
#       execution at budget + HARD_DEADLINE_GRACE_SECONDS; a run still going
#       then makes the executor process EXIT (EXIT_HARD_DEADLINE). It never
#       fabricates a result: the broker sees UnknownExecution and goes to its
#       live-state reconciler.
# Ordering, pinned by a test: budget + grace < the broker's collect timeout.
EXECUTOR_RUN_BUDGET_SECONDS: Final = 3300.0
HARD_DEADLINE_GRACE_SECONDS: Final = 120.0
EXIT_HARD_DEADLINE: Final = 70
EVIDENCE_RESERVE_BYTES: Final = 4 * 1024
_DETAIL_LIMIT: Final = 512
_MESSAGE_LIMIT: Final = 200
_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")


class ExecutorDenied(HostControlError):
    """The caller or the fetched spec failed an identity check."""


class ExecutorBusy(HostControlError):
    """A different execution is running; at most one at a time."""


class UnknownExecution(HostControlError):
    """No execution with that handle is held (e.g. after a restart)."""


class InvalidSpec(HostControlError):
    """The fetched spec failed schema, digest, target or envelope checks."""


_ERROR_NAME_BY_TYPE: Mapping[type, str] = {
    ExecutorDenied: DENIED_ERROR,
    ExecutorBusy: BUSY_ERROR,
    UnknownExecution: UNKNOWN_EXECUTION_ERROR,
    InvalidSpec: INVALID_SPEC_ERROR,
    Unsupported: UNSUPPORTED_ERROR,
    HostControlError: FAILED_ERROR,
}


def error_name_for(exc: BaseException) -> str:
    return resolve_error_name(exc, _ERROR_NAME_BY_TYPE, INTERNAL_ERROR)


def error_body_for(exc: BaseException) -> str:
    if isinstance(exc, HostControlError):
        return str(exc)[:_MESSAGE_LIMIT] or "failed"
    return "internal error"


def bus_name_for_uid(uid: int) -> str:
    """``org.pantheon.HostExecutor1.U<uid>``: derived from the REGISTRY uid
    (ruling i5, no registry schema change). The ``U`` keeps the final name
    element legal: a D-Bus name element may not start with a digit."""

    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise HostControlError("invalid executor uid")
    return f"{BUS_NAME_PREFIX}.U{uid}"


def is_execution_handle(value: object) -> bool:
    return isinstance(value, str) and _HEX_256.fullmatch(value) is not None


@dataclass(frozen=True)
class ExecutionResult:
    """What the executor returns: typed records only, never a receipt."""

    ok: bool
    mutations: tuple[MutationRecord, ...]
    evidence: tuple[OperationEvidence, ...]
    detail: str


def _mutation_dict(mutation: MutationRecord) -> dict[str, object]:
    return {
        "operation_id": mutation.operation_id,
        "kind": mutation.kind,
        "target": mutation.target,
        "owner": mutation.owner,
        "changed": mutation.changed,
        "before": dict(mutation.before),
        "after_fingerprint": mutation.after_fingerprint,
    }


def _evidence_dict(evidence: OperationEvidence) -> dict[str, object]:
    return {
        "operation_id": evidence.operation_id,
        "layer": evidence.layer,
        "kind": evidence.kind,
        "ok": evidence.ok,
        "detail": evidence.detail,
        "evidence": dict(evidence.evidence),
    }


def record_size(
    mutation: MutationRecord | None,
    evidence: OperationEvidence | None,
) -> int:
    """Canonical-JSON byte size of one operation's records (plus list
    separators): the unit of the engine's RECORD_BUDGET_BYTES."""

    size = 0
    if mutation is not None:
        size += len(canonical_json(_mutation_dict(mutation)).encode("utf-8")) + 1
    if evidence is not None:
        size += len(canonical_json(_evidence_dict(evidence)).encode("utf-8")) + 1
    return size


def encode_result(
    *,
    operation_handle: str,
    spec_digest: str,
    result: ExecutionResult,
) -> str:
    """Canonical bounded result JSON.

    COMMITTED MUTATIONS ARE NEVER DROPPED (review R1). The engine's record
    budget keeps a result well under the limit; if one is still oversize,
    it degrades loudly as a failure: evidence detail is stripped first,
    then the evidence list, and the mutation list is always kept.

    The last branch below (mutations alone over the limit) is UNREACHABLE
    for an engine result (carry-in N4): the engine refuses an operation
    before it commits when its projected record would cross
    ``RECORD_BUDGET_BYTES`` (768 KiB), so the mutations of one result fit in
    768 KiB plus at most one small post-commit record, far under
    ``RESULT_JSON_LIMIT`` (1 MiB) with the ~0.3 KiB envelope; a test pins the
    arithmetic. It stays as defence in depth: the payload would still carry
    every mutation, the broker would refuse to decode it, and the operation
    would stay ``dispatched`` for reconciliation: loud, never a silently
    unrestorable receipt."""

    payload = {
        "schema": EXECUTION_RESULT_SCHEMA,
        "operation_handle": operation_handle,
        "spec_digest": spec_digest,
        "ok": bool(result.ok),
        "mutations": [_mutation_dict(item) for item in result.mutations],
        "evidence": [_evidence_dict(item) for item in result.evidence],
        "detail": str(result.detail)[:_DETAIL_LIMIT],
    }
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) <= RESULT_JSON_LIMIT:
        return encoded
    payload.update(
        ok=False,
        detail="execution result exceeds size limit; evidence detail dropped, mutations kept",
        evidence=[
            {
                "operation_id": item.operation_id,
                "layer": item.layer,
                "kind": item.kind,
                "ok": False,
                "detail": "",
                "evidence": {},
            }
            for item in result.evidence
        ],
    )
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) <= RESULT_JSON_LIMIT:
        return encoded
    payload.update(
        evidence=[],
        detail="execution result exceeds size limit; evidence dropped, mutations kept",
    )
    return canonical_json(payload)


def decode_result(
    raw: str,
    *,
    operation_handle: str,
    spec_digest: str,
    operation_kinds: Mapping[str, str],
) -> ExecutionResult:
    """Broker-side validation of an executor result. Every record must name
    an operation of THIS spec with its declared kind; anything else is a
    malformed result (the broker never appends unbound records)."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > RESULT_JSON_LIMIT:
        raise HostControlError("execution result exceeds size limit")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise HostControlError("execution result is not JSON") from exc
    expected = {
        "schema",
        "operation_handle",
        "spec_digest",
        "ok",
        "mutations",
        "evidence",
        "detail",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise HostControlError("invalid execution result fields")
    if value["schema"] != EXECUTION_RESULT_SCHEMA:
        raise HostControlError("invalid execution result schema")
    if value["operation_handle"] != operation_handle or value["spec_digest"] != spec_digest:
        raise HostControlError("execution result is bound to another operation")
    if not isinstance(value["ok"], bool) or not isinstance(value["detail"], str):
        raise HostControlError("invalid execution result scalars")
    if not isinstance(value["mutations"], list) or not isinstance(value["evidence"], list):
        raise HostControlError("invalid execution result records")
    mutations = tuple(_mutation_from_dict(item) for item in value["mutations"])
    evidence = tuple(_evidence_from_dict(item) for item in value["evidence"])
    for record in (*mutations, *evidence):
        if operation_kinds.get(record.operation_id) != record.kind:
            raise HostControlError("execution record names no operation of this spec")
    ok = value["ok"] and all(item.ok for item in evidence)
    return ExecutionResult(
        ok=ok,
        mutations=mutations,
        evidence=evidence,
        detail=value["detail"][:_DETAIL_LIMIT],
    )
