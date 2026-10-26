"""Broker-side ``ExecutionSource.OperationSpec`` domain (ORG-052 section 3;
Unit 5a, T-2917).

The per-seat executor never receives an operation in a request: after the
broker calls ``Execute(operation_handle, expected_operation_digest)``, the
executor fetches the server-resolved, schema-versioned specification
through this broker-only internal interface, ``"ss" -> "ssss"``:

    (schema_version, execution_kind, recomputed_digest, spec_json)

Authorization (rechecked in code; bus policy is defence in depth):

* the caller's bus-derived uid must be the uid the ROOT-OWNED registry
  assigns to the operation's TARGET seat; the registry, never a request
  argument, selects which executor may read which operation;
* the operation must be in ``dispatch_intent`` or ``dispatched``, so a spec
  is readable only after the broker has durably written the staged receipt
  and write-ahead rollback intents (ORG-052 section 6 ordering);
* the caller's expected digest must equal the digest recomputed here.

The spec digest is deterministic over the spec payload (no timestamps,
no record digest), so it is stable across every state transition and
recovery retry and needs no storage: the broker recomputes it from the
journal. Every operation in the spec is re-checked against the executor
envelope at this execution point, not only at plan time (ORG-082 R3).

T-2917 5d.2 (ruling T-2917:D13): ONE operation can have TWO specs, chosen by
the caller's expected digest with no signature change: its own (served once
per journaled arm when mutating) and, for an apply, the read-only
``reconcile`` probe spec the live-state reconciler dispatches, which is
re-fetchable and never claimed. A rollback spec carries the restorable
mutations of its bound receipt, and its operations' ``desired.content`` is
elided (sha256 kept; ruling T-2917:D16) so plan content plus before-snapshots
stay under ``SPEC_JSON_LIMIT``.

T-2917 5d.2 follow-up (ruling T-2917:D28): every reconcile probe spec carries
``probe_nonce``, the operation's count of journaled ``reconcile_probe``
events at the time of the probe (journal-derived: restart-safe and
deterministic, no clock and no randomness). Each recorded probe therefore
moves the next probe to a NEW digest, so the executor's ``(handle, digest)``
table can never deduplicate a second probe to the first probe's retained,
finished execution and hand back stale observations.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Final, Mapping

from tools.host_control.contracts import (
    HostControlError,
    InvalidTransition,
    OperationRecord,
    PlanEnvelope,
    PolicyDenied,
    RegistryError,
    canonical_json,
    sha256_json,
)
from tools.host_control.envelope import require_executor_envelope
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import restorable_for_rollback
from tools.host_control.registry import SeatRegistry
from tools.persona_installer_contracts import (
    ALLOWED_TARGET_ROOTS_KEY,
    InstallOperation,
    MutationRecord,
)

EXECUTION_SPEC_SCHEMA: Final = "pantheon-host-execution-spec/v1"
# Closed execution kinds served in this revision. ``reconcile`` (T-2917
# 5d.2) is the read-only live-state probe of an apply; no operation RECORD
# has that kind (the journal's CHECK admits apply/rollback/verify only).
EXECUTION_KINDS: Final = frozenset(("apply", "verify", "rollback", "reconcile"))
RECORD_KINDS: Final = frozenset(("apply", "verify", "rollback"))
DISPATCHABLE_STATES: Final = frozenset(("dispatch_intent", "dispatched"))
SPEC_JSON_LIMIT: Final = 1024 * 1024
# T-2917:D28: the reconcile probe nonce's closed range (transcribed in the
# executor engine; a test pins it)
MAX_PROBE_NONCE: Final = 2**31 - 1

_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")


def _operation_dict(operation: InstallOperation, *, elide_content: bool = False) -> Mapping[str, object]:
    desired = dict(operation.desired)
    if elide_content:
        desired.pop("content", None)
    return {
        "operation_id": operation.operation_id,
        "layer": operation.layer,
        "kind": operation.kind,
        "target": operation.target,
        "precondition": dict(operation.precondition),
        "desired": desired,
        "inverse": None if operation.inverse is None else dict(operation.inverse),
        "privilege": operation.privilege,
        "owner": operation.owner,
        "provenance": dict(operation.provenance),
        "execution_scope": operation.execution_scope,
    }


def _mutation_dict(mutation: MutationRecord) -> Mapping[str, object]:
    return {
        "operation_id": mutation.operation_id,
        "kind": mutation.kind,
        "target": mutation.target,
        "owner": mutation.owner,
        "changed": mutation.changed,
        "before": dict(mutation.before),
        "after_fingerprint": mutation.after_fingerprint,
    }


def build_spec_payload(
    record: OperationRecord,
    plan: PlanEnvelope,
    rollback_mutations: tuple[MutationRecord, ...] = (),
    *,
    execution_kind: str | None = None,
    probe_nonce: int | None = None,
) -> Mapping[str, object]:
    """The deterministic execution specification for one operation (its own
    kind, or ``reconcile`` for an apply's read-only live-state probe)."""

    kind = record.kind if execution_kind is None else execution_kind
    if record.kind not in RECORD_KINDS or kind not in EXECUTION_KINDS:
        raise HostControlError("operation kind is not executable in this revision")
    if kind != record.kind and not (kind == "reconcile" and record.kind == "apply"):
        raise HostControlError("execution kind does not belong to this operation")
    if kind != "rollback" and rollback_mutations:
        raise HostControlError("rollback mutations on a non-rollback spec")
    if (kind == "reconcile") != (probe_nonce is not None):
        raise HostControlError("a probe nonce belongs to (and only to) a reconcile spec")
    if probe_nonce is not None and (
        isinstance(probe_nonce, bool) or not isinstance(probe_nonce, int) or not 0 <= probe_nonce <= MAX_PROBE_NONCE
    ):
        raise HostControlError("invalid reconcile probe nonce")
    if record.plan_handle != plan.plan_handle or record.plan_digest != plan.digest:
        raise HostControlError("operation does not match its stored plan")
    if record.target_seat_id != plan.target_seat_id:
        raise HostControlError("operation target does not match its stored plan")
    operations = plan.installer_plan.operations
    require_executor_envelope(operation.kind for operation in operations)
    declared_roots = plan.installer_plan.provenance.get(ALLOWED_TARGET_ROOTS_KEY, "")
    payload: dict[str, object] = {
        "schema": EXECUTION_SPEC_SCHEMA,
        "operation_handle": record.operation_handle,
        "execution_kind": kind,
        "target_seat_id": record.target_seat_id,
        "plan_handle": plan.plan_handle,
        "plan_digest": plan.digest,
        "effect_digest": plan.effect_digest,
        "manifest_revision": plan.manifest_revision,
        "framework_revision": plan.framework_revision,
        "receipt_handle": record.receipt_handle,
        "receipt_digest": record.receipt_digest,
        "allowed_roots": sorted(item for item in declared_roots.split(",") if item),
        "operations": [
            _operation_dict(operation, elide_content=kind == "rollback")
            for operation in operations
        ],
        "rollback_mutations": [_mutation_dict(item) for item in rollback_mutations],
    }
    if probe_nonce is not None:
        payload["probe_nonce"] = probe_nonce
    return payload


def spec_digest(payload: Mapping[str, object]) -> str:
    return sha256_json(payload)


class BrokerExecutionSource:
    def __init__(
        self,
        *,
        journal: BrokerJournal,
        registry: SeatRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._journal = journal
        self._registry = registry
        self._clock = clock

    def spec_for(self, record: OperationRecord) -> Mapping[str, object]:
        """Broker-internal: the spec payload for an operation record (used by
        the dispatcher to compute ``expected_operation_digest``)."""

        plan = self._journal.get_plan(
            subject=record.owner,
            plan_handle=record.plan_handle,
            expected_digest=record.plan_digest,
        )
        mutations: tuple[MutationRecord, ...] = ()
        if record.kind == "rollback":
            # T-2917 5d.2: ONLY the restorable mutations (intents x changed,
            # D6 owned rule), the same set the dispatcher journaled intents for
            mutations = restorable_for_rollback(self._journal, record)
        return build_spec_payload(record, plan, mutations)

    def expected_digest(self, record: OperationRecord) -> str:
        return spec_digest(self.spec_for(record))

    def reconcile_spec_for(self, record: OperationRecord) -> Mapping[str, object]:
        """Broker-internal (T-2917 5d.2): the read-only ``reconcile`` probe
        spec of an APPLY operation (the same plan operations, never claimed)."""

        if record.kind != "apply":
            raise HostControlError("only an apply operation has a reconcile probe spec")
        plan = self._journal.get_plan(
            subject=record.owner,
            plan_handle=record.plan_handle,
            expected_digest=record.plan_digest,
        )
        # T-2917:D28: a fresh digest per recorded probe (never a stale dedup)
        nonce = self._journal.count_events(record.operation_handle, ("reconcile_probe",))
        return build_spec_payload(record, plan, execution_kind="reconcile", probe_nonce=nonce)

    def reconcile_digest(self, record: OperationRecord) -> str:
        return spec_digest(self.reconcile_spec_for(record))

    def operation_spec(
        self,
        caller_uid: int,
        operation_handle: str,
        expected_digest: str,
    ) -> tuple[str, str, str, str]:
        if not isinstance(operation_handle, str) or _HEX_256.fullmatch(operation_handle) is None:
            raise HostControlError("invalid operation handle")
        if not isinstance(expected_digest, str) or _HEX_256.fullmatch(expected_digest) is None:
            raise HostControlError("invalid expected operation digest")
        record = self._journal.load_operation_unbound(operation_handle)
        try:
            target = self._registry.seat(record.target_seat_id)
        except RegistryError as exc:
            raise PolicyDenied("target_denied") from exc
        if caller_uid != target.uid:
            # Only the registry-selected executor of the TARGET seat may
            # read this operation; never the requesting subject per se.
            raise PolicyDenied("subject_denied")
        if record.state not in DISPATCHABLE_STATES:
            raise InvalidTransition("operation is not dispatchable")
        payload = self.spec_for(record)
        digest = spec_digest(payload)
        if digest != expected_digest and record.kind == "apply":
            # T-2917 5d.2 (T-2917:D13): the digest selects the spec; the
            # read-only reconcile probe is re-fetchable and never claimed
            probe = self.reconcile_spec_for(record)
            if spec_digest(probe) == expected_digest:
                encoded = canonical_json(probe)
                if len(encoded.encode("utf-8")) > SPEC_JSON_LIMIT:
                    raise HostControlError("execution spec exceeds size limit")
                return (EXECUTION_SPEC_SCHEMA, "reconcile", expected_digest, encoded)
        if digest != expected_digest:
            raise HostControlError("operation digest does not match expectation")
        encoded = canonical_json(payload)
        if len(encoded.encode("utf-8")) > SPEC_JSON_LIMIT:
            raise HostControlError("execution spec exceeds size limit")
        if record.kind != "verify" and not self._journal.claim_spec_serve(
            current=record,
            created_at=self._clock().isoformat(),
        ):
            # T-2917 5b review S4: a MUTATING spec is served once per
            # operation, plus once per journaled re-arm (5d.2, reachable only
            # from the reconciler after a live-state proof); any other fetch
            # (a re-trigger of an apply parked for the reconciler) is
            # refused. Read-only verify may be re-fetched (its re-dispatch is
            # safe).
            raise InvalidTransition("execution spec already served for this operation")
        return (EXECUTION_SPEC_SCHEMA, record.kind, digest, encoded)
