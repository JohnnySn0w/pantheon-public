"""Broker-side durable operation lifecycle (ORG-052 sections 5 and 6).

State transitions are pure functions over records (this module); the journal
owns persistence. The approval service's ValidateAndBind, the executor call,
and live-state reconciliation are narrow injected seams: this unit is
testable with fakes and the real implementations land in later sub-tickets
(approval journal; executors T-2917 arc units) without rework.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Literal, Protocol

from tools.host_control.contracts import (
    COMPENSATION_APPROVAL_SENTINEL,
    COMPENSATION_KEY_PREFIX,
    OPERATION_RECEIPT_SCHEMA,
    OPERATION_SCHEMA,
    OPERATION_STATES,
    OPERATION_TRANSITIONS,
    TERMINAL_OPERATION_STATES,
    ApprovalUnavailable,
    BindOutcome,
    HostControlError,
    InvalidTransition,
    VERIFY_APPROVAL_SENTINEL,
    VERIFY_AUTHORITY_SENTINEL,
    OperationReceipt,
    OperationRecord,
    PlanEnvelope,
    PolicyDenied,
    ReconcileOutcome,
    RecoveryRefused,
    RollbackIntent,
    UnknownHandle,
    sha256_json,
)
from tools.host_control.envelope import (
    require_executor_envelope,
    require_revision_implemented,
)
from tools.host_control.executor.wire import BUSY_ERROR
from tools.host_control.journal import BrokerJournal
from tools.host_control.registry import SeatRegistry
from tools.persona_installer_contracts import MutationRecord, OperationEvidence


_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")

_OPERATION_REQUEST_SCHEMA = "pantheon-host-operation-request/v1"


class ApprovalBinder(Protocol):
    """Narrow seam for HostApproval1.Consumer.ValidateAndBind (ORG-052 §5).

    Implementations return a BindOutcome or raise ApprovalUnavailable on
    transport/outage failure. Denial and unavailability are deliberately
    distinct: denial terminates the operation refused; unavailability leaves
    it PREPARED and retryable (approval-authority outage fails closed).
    """

    def validate_and_bind(
        self,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome: ...


class ExecutorPort(Protocol):
    """Value-in/value-out executor seam.

    Receives frozen typed records and returns a bounded execution reference.
    It is handed no journal, no connection, and no append callback: the
    'executors never write the broker journal' constraint is the type
    signature, not a convention.
    """

    def execute(
        self,
        operation: OperationRecord,
        staged_receipt: OperationReceipt,
        intents: tuple[RollbackIntent, ...],
    ) -> str: ...


class LiveStateReconciler(Protocol):
    """Deterministic live-state reconciliation seam (ORG-052 §6).

    Given the staged receipt and unresolved write-ahead intents, report which
    of the four closed outcomes describes the live host. The actual host
    inspection/restore lands with the executor units; this unit owns the
    mapping from outcome to durable resolution.
    """

    def reconcile(
        self,
        operation: OperationRecord,
        staged_receipt: OperationReceipt,
        unresolved_intents: tuple[RollbackIntent, ...],
    ) -> ReconcileOutcome: ...


RecoveryAction = Literal[
    "authorized",
    "refused",
    "dispatched",
    "redispatched",
    "completed",
    "failed",
    "dispatch_pending",
    "redispatch_pending",
    "terminal",
]


@dataclass(frozen=True)
class RecoveryOutcome:
    record: OperationRecord
    action: RecoveryAction


def transition(
    record: OperationRecord,
    to_state: str,
    *,
    updated_at: str,
    authority_record_id: str | None = None,
) -> OperationRecord:
    """Pure state transition; raises InvalidTransition outside the table."""

    if record.state not in OPERATION_STATES or to_state not in OPERATION_STATES:
        raise InvalidTransition("unknown operation state")
    if to_state not in OPERATION_TRANSITIONS[record.state]:
        raise InvalidTransition(
            f"illegal transition {record.state} -> {to_state}"
        )
    changes: dict[str, object] = {"state": to_state, "updated_at": updated_at}
    if authority_record_id is not None:
        changes["authority_record_id"] = authority_record_id
    values = {
        field.name: getattr(record, field.name)
        for field in dataclasses.fields(record)
        if field.name != "digest"
    }
    values.update(changes)
    return OperationRecord.create(**values)


class OperationService:
    def __init__(
        self,
        *,
        registry: SeatRegistry,
        journal: BrokerJournal,
        binder: ApprovalBinder,
        reconciler: LiveStateReconciler,
        clock: Callable[[], datetime],
        handle_factory: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._journal = journal
        self._binder = binder
        self._reconciler = reconciler
        self._clock = clock
        self._handle_factory = handle_factory

    # ------------------------------------------------------------------
    # prepare (PREPARED, durably synced before ValidateAndBind)
    # ------------------------------------------------------------------

    def prepare_apply(
        self,
        authenticated_uid: int,
        plan_handle: str,
        plan_digest: str,
        idempotency_key: str,
        approval_handle: str,
    ) -> tuple[OperationRecord, bool]:
        plan_handle = _bounded_hex(plan_handle, "plan handle")
        plan_digest = _bounded_hex(plan_digest, "plan digest")
        idempotency_key = _bounded_scalar(idempotency_key, "idempotency key")
        # T-2917 ruling f2: the canonical approval handle is 64 lowercase
        # hex (HostApproval1 mints token_hex(32) and enforces _closed_hex);
        # kept in step with the host MCP adapter by LandedParityTests.
        approval_handle = _bounded_hex(approval_handle, "approval handle")
        subject = self._registry.subject_for_uid(authenticated_uid)
        plan = self._journal.get_plan(
            subject=subject,
            plan_handle=plan_handle,
            expected_digest=plan_digest,
        )
        now = self._now()
        self._require_unexpired(plan, now)
        # T-2917 5b: fail-closed whole-plan refusal of any non-admitted
        # kind INSIDE a broker plan, again at prepare (the planner already
        # partitioned; OperationSpec re-checks at the execution point).
        require_executor_envelope(op.kind for op in plan.installer_plan.operations)
        request_digest = sha256_json(
            {
                "schema": _OPERATION_REQUEST_SCHEMA,
                "kind": "apply",
                "plan_handle": plan_handle,
                "plan_digest": plan_digest,
                "approval_handle": approval_handle,
            }
        )
        record = OperationRecord.create(
            schema=OPERATION_SCHEMA,
            operation_handle=self._new_handle(),
            kind="apply",
            state="prepared",
            owner=subject,
            target_seat_id=plan.target_seat_id,
            plan_handle=plan.plan_handle,
            plan_digest=plan.digest,
            receipt_handle=None,
            receipt_digest=None,
            operation_families=plan.operation_families,
            approval_handle=approval_handle,
            authority_record_id=None,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        return self._journal.put_operation_or_replay(record=record)

    def prepare_rollback(
        self,
        authenticated_uid: int,
        receipt_handle: str,
        idempotency_key: str,
        approval_handle: str,
    ) -> tuple[OperationRecord, bool]:
        receipt_handle = _bounded_hex(receipt_handle, "receipt handle")
        idempotency_key = _bounded_scalar(idempotency_key, "idempotency key")
        # T-2917 ruling f2: the canonical approval handle is 64 lowercase
        # hex (HostApproval1 mints token_hex(32) and enforces _closed_hex);
        # kept in step with the host MCP adapter by LandedParityTests.
        approval_handle = _bounded_hex(approval_handle, "approval handle")
        subject = self._registry.subject_for_uid(authenticated_uid)
        receipt = self._journal.get_receipt(
            subject=subject,
            receipt_handle=receipt_handle,
        )
        if receipt.phase not in ("complete", "failed"):
            # T-2917 5d.2 (ruling T-2917:D15): the terminal receipt of an
            # apply, complete OR failed (a failed apply's committed changes
            # are restorable too); never a staged receipt.
            raise HostControlError(
                "rollback requires a complete or failed receipt"
            )
        source = self._journal.get_operation(
            subject=subject,
            operation_handle=receipt.operation_handle,
        )
        if source.kind != "apply":
            # T-2917 5b: a verify receipt records observations, never
            # mutations; only an apply receipt is restorable.
            raise HostControlError("rollback requires an apply receipt")
        compensation = self._journal.compensation_ref(source.operation_handle)
        if compensation is not None and compensation[1] not in ("failed", "refused"):
            # T-4964: the broker already compensates this receipt. A grant-
            # bound rollback is admitted only once that compensation ended
            # ``failed`` (e.g. its whole-set drift refusal) or ``refused``
            # (it never ran anything); while it is live, completed, or parked
            # in manual_recovery it is refused.
            raise HostControlError(
                "receipt is compensated by the broker; see the apply's "
                "compensation_operation_handle"
            )
        if not restorable_mutations(
            receipt, self._journal.intents_for(source.operation_handle)
        ):
            # T-2917 5d.2: restorable = the source apply's write-ahead
            # intents intersect its CHANGED mutation records (D6 owned-
            # record rule for a synthesized timer intent).
            raise HostControlError("receipt holds no restorable mutation")
        rollback_plan = self._journal.get_plan(
            subject=subject,
            plan_handle=receipt.plan_handle,
            expected_digest=receipt.plan_digest,
        )
        require_executor_envelope(
            op.kind for op in rollback_plan.installer_plan.operations
        )
        now = self._now()
        request_digest = sha256_json(
            {
                "schema": _OPERATION_REQUEST_SCHEMA,
                "kind": "rollback",
                "receipt_handle": receipt.receipt_handle,
                "receipt_digest": receipt.digest,
                "approval_handle": approval_handle,
            }
        )
        record = OperationRecord.create(
            schema=OPERATION_SCHEMA,
            operation_handle=self._new_handle(),
            kind="rollback",
            state="prepared",
            owner=subject,
            target_seat_id=receipt.target_seat_id,
            plan_handle=receipt.plan_handle,
            plan_digest=receipt.plan_digest,
            receipt_handle=receipt.receipt_handle,
            receipt_digest=receipt.digest,
            operation_families=source.operation_families,
            approval_handle=approval_handle,
            authority_record_id=None,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        return self._journal.put_operation_or_replay(record=record)

    def prepare_verify(
        self,
        authenticated_uid: int,
        seat_id: str,
        plan_handle: str,
        idempotency_key: str,
    ) -> tuple[OperationRecord, bool]:
        """PREPARED verify operation for the caller's OWN stored plan
        (T-2917 5b, ruling B2/B3).

        Verify is policy-authorized: the stored plan already carries the
        subject's policy decision for this target, and ORG-052 section 5
        binds ValidateAndBind to apply and rollback only. The record
        therefore carries the closed sentinels, never a caller value. A
        plan holding any kind without a read-only verify handler in this
        revision is refused as Unsupported BEFORE a record is written.
        """

        seat_id = _bounded_scalar(seat_id, "seat id")
        plan_handle = _bounded_hex(plan_handle, "plan handle")
        idempotency_key = _bounded_scalar(idempotency_key, "idempotency key")
        subject = self._registry.subject_for_uid(authenticated_uid)
        plan = self._journal.get_plan(subject=subject, plan_handle=plan_handle)
        if plan.target_seat_id != seat_id:
            raise PolicyDenied("target_denied")
        now = self._now()
        self._require_unexpired(plan, now)
        require_executor_envelope(op.kind for op in plan.installer_plan.operations)
        require_revision_implemented(
            "verify",
            (operation.kind for operation in plan.installer_plan.operations),
        )
        request_digest = sha256_json(
            {
                "schema": _OPERATION_REQUEST_SCHEMA,
                "kind": "verify",
                "seat_id": seat_id,
                "plan_handle": plan.plan_handle,
                "plan_digest": plan.digest,
            }
        )
        record = OperationRecord.create(
            schema=OPERATION_SCHEMA,
            operation_handle=self._new_handle(),
            kind="verify",
            state="prepared",
            owner=subject,
            target_seat_id=plan.target_seat_id,
            plan_handle=plan.plan_handle,
            plan_digest=plan.digest,
            receipt_handle=None,
            receipt_digest=None,
            operation_families=plan.operation_families,
            approval_handle=VERIFY_APPROVAL_SENTINEL,
            authority_record_id=None,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        return self._journal.put_operation_or_replay(record=record)

    def require_rollback_implemented(
        self,
        authenticated_uid: int,
        receipt_handle: str,
    ) -> None:
        """Ruling B4 for Rollback (T-2917 5d.2): refuse (Unsupported) a
        rollback whose receipt's stored plan holds a class this revision
        cannot roll back, BEFORE prepare. Subject-bound read only."""

        subject = self._registry.subject_for_uid(authenticated_uid)
        receipt = self._journal.get_receipt(
            subject=subject,
            receipt_handle=_bounded_hex(receipt_handle, "receipt handle"),
        )
        plan = self._journal.get_plan(
            subject=subject,
            plan_handle=receipt.plan_handle,
            expected_digest=receipt.plan_digest,
        )
        require_revision_implemented(
            "rollback",
            (operation.kind for operation in plan.installer_plan.operations),
        )

    def require_apply_implemented(
        self,
        authenticated_uid: int,
        plan_handle: str,
        plan_digest: str,
    ) -> None:
        """Ruling B4: refuse (Unsupported) an Apply whose stored plan holds
        a class this revision does not execute, BEFORE prepare and so
        before any approval grant could bind. Subject-bound read only."""

        subject = self._registry.subject_for_uid(authenticated_uid)
        plan = self._journal.get_plan(
            subject=subject,
            plan_handle=_bounded_hex(plan_handle, "plan handle"),
            expected_digest=_bounded_hex(plan_digest, "plan digest"),
        )
        require_revision_implemented(
            "apply",
            (operation.kind for operation in plan.installer_plan.operations),
        )

    def revision_implements(self, record: OperationRecord) -> bool:
        """Driver-side re-check of ruling B4 for a recovered operation."""

        plan = self._journal.get_plan(
            subject=record.owner,
            plan_handle=record.plan_handle,
            expected_digest=record.plan_digest,
        )
        try:
            require_revision_implemented(
                record.kind,
                (operation.kind for operation in plan.installer_plan.operations),
            )
        except HostControlError:
            return False
        return True

    # ------------------------------------------------------------------
    # authorize (ValidateAndBind, identical tuple on every retry)
    # ------------------------------------------------------------------

    def authorize(self, operation_handle: str) -> OperationRecord:
        record = self._journal.load_operation_unbound(operation_handle)
        if record.state != "prepared":
            raise InvalidTransition(
                "authorize requires a prepared operation"
            )
        if record.kind == "verify":
            # Ruling B2: policy-authorized read-only verify; the binder is
            # never called and no grant exists to bind.
            new = transition(
                record,
                "authorized",
                updated_at=self._now().isoformat(),
                authority_record_id=VERIFY_AUTHORITY_SENTINEL,
            )
            self._journal.transition_operation(
                current=record,
                new=new,
                event_kind="verify_authorized",
                payload={"approval_class": "none"},
            )
            return new
        if record.approval_handle == COMPENSATION_APPROVAL_SENTINEL:
            return self._authorize_compensation(record)
        outcome = self._binder.validate_and_bind(
            record.approval_handle,
            record.plan_handle,
            record.plan_digest,
            record.operation_handle,
        )
        now = self._now().isoformat()
        if outcome.state == "bound":
            authority = _bounded_scalar(
                outcome.authority_record_id,
                "authority record id",
            )
            new = transition(
                record,
                "authorized",
                updated_at=now,
                authority_record_id=authority,
            )
            self._journal.transition_operation(
                current=record,
                new=new,
                event_kind="bound",
                payload={"authority_record_id": authority},
            )
            return new
        if outcome.state == "denied":
            new = transition(record, "refused", updated_at=now)
            self._journal.transition_operation(
                current=record,
                new=new,
                event_kind="bind_denied",
                payload={"reason_code": str(outcome.reason_code)[:128]},
            )
            return new
        raise HostControlError("invalid bind outcome state")

    def _authorize_compensation(self, record: OperationRecord) -> OperationRecord:
        """T-4964 (ORG-052 section 5: "That compensating action does not
        need a second grant because it is part of the originally approved
        transaction"). The binder is NEVER called: the compensation inherits
        the failed apply's authority record, re-derived here from the
        journal. Any mismatch ends it ``refused`` (loud, never bound)."""

        now = self._now().isoformat()
        authority: str | None = None
        source_handle = ""
        try:
            receipt = self._journal.get_receipt(
                subject=record.owner,
                receipt_handle=str(record.receipt_handle),
            )
            source_handle = receipt.operation_handle
            source = self._journal.get_operation(
                subject=record.owner,
                operation_handle=source_handle,
            )
            if (
                receipt.phase == "failed"
                and receipt.digest == record.receipt_digest
                and source.kind == "apply"
                and source.state == "failed"
                and source.plan_handle == record.plan_handle
                and source.plan_digest == record.plan_digest
                and source.authority_record_id is not None
                and source.authority_record_id != VERIFY_AUTHORITY_SENTINEL
                and record.idempotency_key == COMPENSATION_KEY_PREFIX + source_handle
            ):
                authority = source.authority_record_id
        except HostControlError:
            authority = None
        if authority is None:
            new = transition(record, "refused", updated_at=now)
            self._journal.transition_operation(
                current=record,
                new=new,
                event_kind="compensation_refused",
                payload={"reason_code": "compensation-source-mismatch"},
            )
            return new
        new = transition(
            record,
            "authorized",
            updated_at=now,
            authority_record_id=authority,
        )
        self._journal.transition_operation(
            current=record,
            new=new,
            event_kind="compensation_authorized",
            payload={
                "source_operation_handle": source_handle,
                "authority_record_id": authority,
            },
        )
        return new

    # ------------------------------------------------------------------
    # dispatch (staged receipt + intents durable BEFORE the executor call)
    # ------------------------------------------------------------------

    def dispatch(
        self,
        operation_handle: str,
        executor: ExecutorPort,
    ) -> OperationRecord:
        record = self._journal.load_operation_unbound(operation_handle)
        if record.state != "authorized":
            raise InvalidTransition("dispatch requires an authorized operation")
        plan = self._journal.get_plan(
            subject=record.owner,
            plan_handle=record.plan_handle,
            expected_digest=record.plan_digest,
        )
        now = self._now().isoformat()
        staged_receipt = self._build_receipt(
            record,
            plan,
            phase="staged",
            mutations=(),
            evidence=(),
            created_at=now,
        )
        if record.kind == "verify":
            # Read-only: nothing to invert, so the write-ahead set is
            # deliberately empty (T-2917 5b). Apply keeps its refusal of a
            # plan that declares no invertible operation.
            intents: tuple[RollbackIntent, ...] = ()
        elif record.kind == "rollback":
            # T-2917 5d.2: one restore intent per restorable mutation
            intents = _rollback_restore_intents(
                record,
                restorable_for_rollback(self._journal, record),
                created_at=now,
            )
        else:
            intents = self._plan_rollback_intents(record, plan, created_at=now)
        with_intent = transition(record, "dispatch_intent", updated_at=now)
        self._journal.record_dispatch_intent(
            current=record,
            new=with_intent,
            receipt=staged_receipt,
            intents=intents,
        )
        # The write-ahead truth is durably synced; only now may the
        # executor be invoked (ORG-052 section 6 ordering).
        execution_ref = self._execute_or_void(executor, with_intent, staged_receipt, intents)
        execution_ref = _bounded_scalar(execution_ref, "execution reference")
        dispatched_at = self._now().isoformat()
        dispatched = transition(
            with_intent,
            "dispatched",
            updated_at=dispatched_at,
        )
        self._journal.transition_operation(
            current=with_intent,
            new=dispatched,
            event_kind="dispatched",
            payload={"execution_ref": execution_ref},
        )
        return dispatched

    def resume_dispatch(
        self,
        operation_handle: str,
        executor: ExecutorPort,
        *,
        definitive_refusal: bool = False,
    ) -> OperationRecord:
        """Driver-only resume of an executor hand-off (T-2917 5b, B11).

        * ``dispatch_intent``: (re)issue the executor call and record
          ``dispatched``. Allowed for a read-only verify, or for apply ONLY
          when the caller knows the earlier call never reached an executor
          (a definitive refusal: Busy, no owner, identity mismatch), because
          then no host mutation can have happened.
        * ``dispatched``: re-dispatch is allowed for a read-only verify
          only. A mutating operation already dispatched is NEVER
          re-dispatched here; it waits for the 5d reconciler.
        """

        record = self._journal.load_operation_unbound(operation_handle)
        staged = self._journal.get_staged_receipt(operation_handle)
        intents = self._journal.unresolved_intents(operation_handle)
        if record.state == "dispatch_intent":
            if record.kind != "verify" and not definitive_refusal:
                raise RecoveryRefused(
                    "mutating dispatch is ambiguous; awaiting the live-state reconciler"
                )
            if record.kind != "verify":
                # T-2917:D27: no mutating Execute goes unmarked
                self._journal.append_event(
                    current=record,
                    event_kind="execute_retry",
                    payload={"definitive_refusal": True},
                    created_at=self._now().isoformat(),
                )
            execution_ref = _bounded_scalar(
                self._execute_or_void(executor, record, staged, intents), "execution reference"
            )
            dispatched = transition(record, "dispatched", updated_at=self._now().isoformat())
            self._journal.transition_operation(
                current=record,
                new=dispatched,
                event_kind="dispatched",
                payload={"execution_ref": execution_ref, "resumed": True},
            )
            return dispatched
        if record.state == "dispatched" and record.kind == "verify":
            execution_ref = _bounded_scalar(
                executor.execute(record, staged, intents), "execution reference"
            )
            self._journal.append_event(
                current=record,
                event_kind="redispatched",
                payload={"execution_ref": execution_ref, "read_only": True},
                created_at=self._now().isoformat(),
            )
            return record
        raise RecoveryRefused(
            "operation cannot be re-dispatched; awaiting the live-state reconciler"
        )

    def retry_identical_dispatch(
        self,
        operation_handle: str,
        executor: ExecutorPort,
    ) -> OperationRecord:
        """Driver-only (T-2917 5d.2; E29 P4): re-issue the IDENTICAL
        Execute for a mutating operation left in ``dispatch_intent`` (the
        earlier Execute's reply was lost). Safe without any live-state proof
        because the executor deduplicates an identical (handle, digest) to
        the execution it already holds, and the broker serves a mutating
        spec at most once per journaled arm: a restarted executor gets the
        serve REFUSED (state ``refused``) and the driver reconciles. A
        ``execute_retry`` marker is journaled BEFORE the call, so serve
        ordering stays classifiable (T-2917:D14)."""

        record = self._journal.load_operation_unbound(operation_handle)
        if record.state != "dispatch_intent" or record.kind == "verify":
            raise RecoveryRefused("identical retry requires a mutating operation in dispatch_intent")
        staged = self._journal.get_staged_receipt(operation_handle)
        intents = self._journal.unresolved_intents(operation_handle)
        self._journal.append_event(
            current=record,
            event_kind="execute_retry",
            payload={"identical": True},
            created_at=self._now().isoformat(),
        )
        execution_ref = _bounded_scalar(
            self._execute_or_void(executor, record, staged, intents), "execution reference"
        )
        dispatched = transition(record, "dispatched", updated_at=self._now().isoformat())
        self._journal.transition_operation(
            current=record,
            new=dispatched,
            event_kind="dispatched",
            payload={"execution_ref": execution_ref, "identical_retry": True},
        )
        return dispatched

    # ------------------------------------------------------------------
    # result append (typed evidence + terminal state, broker-only)
    # ------------------------------------------------------------------

    def append_result(
        self,
        operation_handle: str,
        *,
        ok: bool,
        mutations: tuple[MutationRecord, ...],
        evidence: tuple[OperationEvidence, ...],
        detail: str = "",
        compensate: bool = False,
    ) -> OperationRecord:
        """Append the typed result and terminal state. ``compensate``
        (T-4964) is passed ONLY by the driver's collected-result path: a
        FAILED apply whose receipt holds restorable mutations then gets a
        broker-created compensation, atomically with its terminal. Every
        other failure path (reconcile ``restored``, pre-engine refusal,
        verify cap) leaves it False; manual_recovery parks never reach here."""

        record = self._journal.load_operation_unbound(operation_handle)
        if record.state not in ("dispatch_intent", "dispatched"):
            raise InvalidTransition(
                "result append requires a dispatched operation"
            )
        staged = self._journal.get_staged_receipt(operation_handle)
        now = self._now().isoformat()
        terminal_state = "completed" if ok else "failed"
        receipt = OperationReceipt.create(
            schema=OPERATION_RECEIPT_SCHEMA,
            receipt_handle=self._new_handle(),
            operation_handle=record.operation_handle,
            phase="complete" if ok else "failed",
            plan_handle=staged.plan_handle,
            plan_digest=staged.plan_digest,
            effect_digest=staged.effect_digest,
            subject=record.owner,
            target_seat_id=staged.target_seat_id,
            manifest_revision=staged.manifest_revision,
            framework_revision=staged.framework_revision,
            mutations=tuple(mutations),
            evidence=tuple(evidence),
            created_at=now,
        )
        new = transition(record, terminal_state, updated_at=now)
        compensation: OperationRecord | None = None
        compensation_error: str | None = None
        if compensate and not ok:
            try:
                compensation = self._compensation_for_failure(record, receipt, created_at=now)
            except HostControlError as exc:
                # T-4964: never block the failed result on its compensation;
                # the problem rides the terminal event (loud, journaled)
                compensation_error = type(exc).__name__
        payload: dict[str, object] = {
            "ok": ok,
            "detail": detail[:512],
            "receipt_handle": receipt.receipt_handle,
            "evidence_count": len(evidence),
        }
        if compensation is not None:
            payload["compensation_operation_handle"] = compensation.operation_handle
        if compensation_error is not None:
            payload["compensation_error"] = compensation_error[:128]
        self._journal.record_terminal(
            current=record,
            new=new,
            receipt=receipt,
            payload=payload,
            compensation=compensation,
        )
        return new

    def _compensation_for_failure(
        self,
        record: OperationRecord,
        receipt: OperationReceipt,
        *,
        created_at: str,
    ) -> OperationRecord | None:
        """T-4964: the PREPARED compensation of a failed apply, or None.
        Only an apply qualifies (a rollback, including a compensation, is
        never compensated), only with a non-empty restorable set (the D6
        owned-record rule, ``restorable_mutations``), and only when this
        revision implements rollback for every class in the plan."""

        if record.kind != "apply":
            return None
        if not restorable_mutations(
            receipt, self._journal.intents_for(record.operation_handle)
        ):
            return None
        plan = self._journal.get_plan(
            subject=record.owner,
            plan_handle=record.plan_handle,
            expected_digest=record.plan_digest,
        )
        try:
            require_revision_implemented(
                "rollback",
                (operation.kind for operation in plan.installer_plan.operations),
            )
        except HostControlError:
            return None
        return OperationRecord.create(
            schema=OPERATION_SCHEMA,
            operation_handle=self._new_handle(),
            kind="rollback",
            state="prepared",
            owner=record.owner,
            target_seat_id=record.target_seat_id,
            plan_handle=record.plan_handle,
            plan_digest=record.plan_digest,
            receipt_handle=receipt.receipt_handle,
            receipt_digest=receipt.digest,
            operation_families=record.operation_families,
            approval_handle=COMPENSATION_APPROVAL_SENTINEL,
            authority_record_id=None,
            idempotency_key=COMPENSATION_KEY_PREFIX + record.operation_handle,
            request_digest=sha256_json(
                {
                    "schema": _OPERATION_REQUEST_SCHEMA,
                    "kind": "rollback",
                    "compensation_of": record.operation_handle,
                    "receipt_handle": receipt.receipt_handle,
                    "receipt_digest": receipt.digest,
                    "approval_handle": COMPENSATION_APPROVAL_SENTINEL,
                }
            ),
            created_at=created_at,
            updated_at=created_at,
        )

    # ------------------------------------------------------------------
    # recovery (ORG-052 section 6 cuts, each deterministic)
    # ------------------------------------------------------------------

    def recover(
        self,
        operation_handle: str,
        *,
        executor: ExecutorPort | None = None,
    ) -> RecoveryOutcome:
        try:
            record = self._journal.load_operation_unbound(operation_handle)
        except UnknownHandle as exc:
            raise RecoveryRefused(
                "no broker record for operation handle; any bound grant "
                "remains consumed; manual intervention required"
            ) from exc
        except HostControlError as exc:
            self._journal.mark_manual_recovery(
                operation_handle,
                f"corrupt operation record: {type(exc).__name__}",
                marked_at=self._now().isoformat(),
            )
            raise RecoveryRefused(
                "corrupt broker record for operation handle; parked in "
                "manual_recovery; any bound grant remains consumed"
            ) from exc
        if record.state in TERMINAL_OPERATION_STATES:
            return RecoveryOutcome(record=record, action="terminal")
        if record.state == "prepared":
            resumed = self.authorize(record.operation_handle)
            action = "authorized" if resumed.state == "authorized" else "refused"
            return RecoveryOutcome(record=resumed, action=action)
        if record.state == "authorized":
            if executor is None:
                return RecoveryOutcome(record=record, action="dispatch_pending")
            dispatched = self.dispatch(record.operation_handle, executor)
            return RecoveryOutcome(record=dispatched, action="dispatched")
        return self._reconcile(record, executor)

    def _reconcile(
        self,
        record: OperationRecord,
        executor: ExecutorPort | None,
    ) -> RecoveryOutcome:
        staged = self._journal.get_staged_receipt(record.operation_handle)
        intents = self._journal.unresolved_intents(record.operation_handle)
        outcome = self._reconciler.reconcile(record, staged, intents)
        if outcome.outcome == "clean_prestate":
            if executor is None:
                return RecoveryOutcome(
                    record=record,
                    action="redispatch_pending",
                )
            self._rearm_if_served(record, outcome)
            if record.kind != "verify":
                # T-2917:D27: every reconciler re-dispatch (journal-clean or
                # re-armed) is a journaled Execute issuance
                self._journal.append_event(
                    current=record,
                    event_kind="redispatch_intent",
                    payload={"detail": outcome.detail[:64]},
                    created_at=self._now().isoformat(),
                )
            execution_ref = _bounded_scalar(
                self._execute_or_void(executor, record, staged, intents),
                "execution reference",
            )
            now = self._now().isoformat()
            if record.state == "dispatch_intent":
                dispatched = transition(record, "dispatched", updated_at=now)
                self._journal.transition_operation(
                    current=record,
                    new=dispatched,
                    event_kind="dispatched",
                    payload={"execution_ref": execution_ref, "recovery": True},
                )
                return RecoveryOutcome(record=dispatched, action="dispatched")
            self._journal.append_event(
                current=record,
                event_kind="redispatched",
                payload={"execution_ref": execution_ref, "recovery": True},
                created_at=now,
            )
            return RecoveryOutcome(record=record, action="redispatched")
        if outcome.outcome == "complete_poststate":
            completed = self.append_result(
                record.operation_handle,
                ok=True,
                mutations=outcome.mutations,
                evidence=outcome.evidence,
                detail=outcome.detail,
            )
            return RecoveryOutcome(record=completed, action="completed")
        if outcome.outcome == "restored":
            failed = self.append_result(
                record.operation_handle,
                ok=False,
                mutations=outcome.mutations,
                evidence=outcome.evidence,
                detail=outcome.detail,
            )
            return RecoveryOutcome(record=failed, action="failed")
        if outcome.outcome == "irreconcilable":
            now = self._now().isoformat()
            parked = transition(record, "manual_recovery", updated_at=now)
            payload: dict[str, object] = {"detail": outcome.detail[:512]}
            if outcome.evidence:
                # T-2917 5d.2: the bounded live-state probe report rides
                # the loud refusal (operator input for manual recovery)
                payload["report"] = bounded_report(outcome.evidence)
            self._journal.transition_operation(
                current=record,
                new=parked,
                event_kind="irreconcilable",
                payload=payload,
            )
            raise RecoveryRefused(
                "live state is irreconcilable with the staged receipt and "
                "rollback intents; operation parked in manual_recovery"
            )
        raise HostControlError("invalid reconcile outcome")

    def _execute_or_void(
        self,
        executor: ExecutorPort,
        record: OperationRecord,
        staged: OperationReceipt,
        intents: tuple[RollbackIntent, ...],
    ) -> str:
        """Issue one MUTATING Execute (T-2917 5d.2 follow-up, ruling
        T-2917:D29). When the executor answers with a definitive ``Busy``
        (its table proves it created no execution for this (handle,
        digest)), the issuance just marked is voided in the journal so the
        D27 lifetime count does not over-park a later genuine refusal.
        ``Denied`` (and every other failure) is never voided. A failure to
        journal the void keeps the issuance counted: over-counting only ever
        routes to the reconciler, never to a clean failure."""

        try:
            return executor.execute(record, staged, intents)
        except Exception as exc:
            if record.kind != "verify" and getattr(exc, "name", None) == BUSY_ERROR:
                try:
                    self._journal.void_execute_issuance(
                        current=record,
                        refusal="Busy",
                        created_at=self._now().isoformat(),
                    )
                except HostControlError:
                    pass
            raise

    def _rearm_if_served(self, record: OperationRecord, outcome: ReconcileOutcome) -> None:
        """T-2917 5d.2 (README n3): a served mutating spec is re-dispatched
        ONLY through a journaled, capped re-arm, and only for a reconcile
        outcome that names a re-arm proof. A spec never served needs none
        (the journal-clean case)."""

        if record.kind == "verify":
            return
        handle = record.operation_handle
        served = self._journal.count_events(handle, ("spec_served",))
        rearmed = self._journal.count_events(handle, ("spec_rearmed",))
        if served <= rearmed:
            return
        reason = REARM_REASON_BY_DETAIL.get(outcome.detail)
        if reason is None:
            raise RecoveryRefused(
                "a served spec is re-dispatched only after a re-arm proof"
            )
        self._journal.rearm_spec_serve(
            current=record,
            reason=reason,
            created_at=self._now().isoformat(),
        )

    # ------------------------------------------------------------------
    # subject-facing read (OperationGet)
    # ------------------------------------------------------------------

    def get_operation(
        self,
        authenticated_uid: int,
        operation_handle: str,
        expected_digest: str | None = None,
    ) -> OperationRecord:
        subject = self._registry.subject_for_uid(authenticated_uid)
        return self._journal.get_operation(
            subject=subject,
            operation_handle=_bounded_hex(operation_handle, "operation handle"),
            expected_digest=expected_digest,
        )

    def compensation_handle(self, record: OperationRecord) -> str | None:
        """T-4964: the handle of the broker-created compensation of this
        (already subject-bound) apply record, or None. Read-time only: it
        is never part of the record or its digest."""

        if record.kind != "apply":
            return None
        compensation = self._journal.compensation_ref(record.operation_handle)
        if compensation is None or compensation[2:] != (record.owner.uid, record.owner.seat_id):
            return None
        return compensation[0]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def now_iso(self) -> str:
        """The service clock as an aware-UTC ISO timestamp (driver events)."""

        return self._now().isoformat()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise HostControlError("operation clock must return aware datetime")
        return now.astimezone(timezone.utc)

    def _new_handle(self) -> str:
        handle = self._handle_factory()
        if not isinstance(handle, str) or _HEX_256.fullmatch(handle) is None:
            raise HostControlError("invalid generated operation handle")
        return handle

    @staticmethod
    def _require_unexpired(plan: PlanEnvelope, now: datetime) -> None:
        # Plans expire; OPERATIONS deliberately never do. An in-flight
        # operation's resolution is reconciliation against the staged
        # receipt and unresolved intents, never a timeout: an expiring
        # operation could abandon a dispatched mutation with rollback
        # intents outstanding, which is exactly the state ORG-052 section 6
        # exists to prevent. Grant expiry is the approval service's check
        # at bind time.
        try:
            expires = datetime.fromisoformat(plan.expires_at)
        except ValueError as exc:
            raise HostControlError("invalid plan expiry timestamp") from exc
        if expires.tzinfo is None:
            raise HostControlError("invalid plan expiry timestamp")
        if expires <= now:
            raise HostControlError("plan is expired")

    def _build_receipt(
        self,
        record: OperationRecord,
        plan: PlanEnvelope,
        *,
        phase: str,
        mutations: tuple[MutationRecord, ...],
        evidence: tuple[OperationEvidence, ...],
        created_at: str,
    ) -> OperationReceipt:
        return OperationReceipt.create(
            schema=OPERATION_RECEIPT_SCHEMA,
            receipt_handle=self._new_handle(),
            operation_handle=record.operation_handle,
            phase=phase,
            plan_handle=plan.plan_handle,
            plan_digest=plan.digest,
            effect_digest=plan.effect_digest,
            subject=record.owner,
            target_seat_id=record.target_seat_id,
            manifest_revision=plan.manifest_revision,
            framework_revision=plan.framework_revision,
            mutations=mutations,
            evidence=evidence,
            created_at=created_at,
        )

    @staticmethod
    def _plan_rollback_intents(
        record: OperationRecord,
        plan: PlanEnvelope,
        *,
        created_at: str,
    ) -> tuple[RollbackIntent, ...]:
        # Write-ahead intents carry the plan's server-owned inverse
        # specifications (ORG-049): at broker tier no live host inspection
        # has happened yet, so the intent is the declared inverse action,
        # not an observed before-state snapshot.
        #
        # D6 (T-2917 5c): a timer-enable declares no inverse (its before-state
        # is only knowable at the executor), so the broker SYNTHESIZES one
        # write-ahead intent for it, kind-limited to timer-enable. It records
        # that an enable may be issued and that the before-state basis is the
        # executor's precondition check; it is NEVER cleanup authority on its
        # own: undoing an enable needs the executor's owned mutation record.
        intents = []
        seq = 0
        for operation in plan.installer_plan.operations:
            if operation.inverse:
                inverse = {str(key): value for key, value in operation.inverse.items()}
            elif operation.kind in SYNTHESIZED_INTENT_KINDS:
                inverse = dict(SYNTHESIZED_TIMER_ENABLE_INVERSE)
            else:
                continue
            seq += 1
            intents.append(
                RollbackIntent(
                    operation_handle=record.operation_handle,
                    seq=seq,
                    operation_id=operation.operation_id,
                    kind=operation.kind,
                    target=operation.target,
                    owner=operation.owner,
                    inverse=inverse,
                    created_at=created_at,
                )
            )
        if not intents:
            raise HostControlError(
                "plan declares no invertible operations; refusing dispatch"
            )
        return tuple(intents)


# D6 (T-2917 5c): the only kind that gets a broker-synthesized intent, and
# its exact inverse shape (pinned by a test).
SYNTHESIZED_INTENT_KINDS = frozenset(("timer-enable",))
SYNTHESIZED_TIMER_ENABLE_INVERSE = MappingProxyType(
    {
        "action": "timer-enable-rollback",
        "synthesized_by": "broker",
        "before_basis": "executor-precondition",
        "cleanup_authority": "requires-owned-mutation-record",
    }
)
REQUIRES_OWNED_RECORD = "requires-owned-mutation-record"

# T-2917 5d.2: the reconcile outcome detail codes (closed, PROVISIONAL) and
# the re-arm reason each clean outcome licenses. ``journal-clean`` needs no
# re-arm: the spec was never served.
RECONCILE_DETAIL_CODES = frozenset(
    (
        "journal-clean",
        "probe-clean",
        "rollback-redispatch",
        "lost-result-mutating",
        "d6-no-owned-record",
        "rearm-cap",
    )
)
REARM_REASON_BY_DETAIL = MappingProxyType(
    {
        "probe-clean": "reconcile-probe-clean",
        "rollback-redispatch": "rollback-redispatch",
    }
)
RESTORE_INVERSE_ACTION = "restore-from-receipt"
RESTORE_CLEANUP_AUTHORITY = "receipt-mutation"
_REPORT_LIMIT = 16 * 1024


def restorable_mutations(
    receipt: OperationReceipt,
    source_intents: tuple[RollbackIntent, ...],
) -> tuple[MutationRecord, ...]:
    """T-2917 5d.2 (rulings T-2917:D6/D15): the mutations a rollback may
    restore = the receipt's CHANGED mutations that have a write-ahead
    intent in the source apply naming the same operation, kind, target and
    owner. A broker-synthesized timer-enable intent is NEVER authority on
    its own (D6): its mutation must also be the executor's OWNED record
    (``before.owned == "true"`` and ``before.activation_intent == "false"``)."""

    by_id = {intent.operation_id: intent for intent in source_intents}
    restorable = []
    for mutation in receipt.mutations:
        if not mutation.changed:
            continue
        intent = by_id.get(mutation.operation_id)
        if intent is None:
            continue
        if (intent.kind, intent.target, intent.owner) != (
            mutation.kind,
            mutation.target,
            mutation.owner,
        ):
            continue
        synthesized = (
            intent.kind in SYNTHESIZED_INTENT_KINDS
            or intent.inverse.get("cleanup_authority") == REQUIRES_OWNED_RECORD
        )
        if synthesized and (
            mutation.before.get("owned") != "true"
            or mutation.before.get("activation_intent") != "false"
        ):
            continue
        restorable.append(mutation)
    return tuple(restorable)


def restorable_for_rollback(
    journal: BrokerJournal,
    record: OperationRecord,
) -> tuple[MutationRecord, ...]:
    """The restorable set of a ROLLBACK operation's bound receipt (one
    definition for the dispatcher's intents and the served spec)."""

    if record.kind != "rollback" or record.receipt_handle is None:
        raise HostControlError("not a rollback operation")
    receipt = journal.get_receipt(
        subject=record.owner,
        receipt_handle=record.receipt_handle,
    )
    if receipt.digest != record.receipt_digest:
        raise HostControlError("rollback receipt digest drift")
    return restorable_mutations(receipt, journal.intents_for(receipt.operation_handle))


def _rollback_restore_intents(
    record: OperationRecord,
    restorable: tuple[MutationRecord, ...],
    *,
    created_at: str,
) -> tuple[RollbackIntent, ...]:
    """The rollback operation's own write-ahead intents (T-2917 5d.2): one
    per restorable mutation, restoring from the bound receipt."""

    if not restorable:
        raise HostControlError("receipt holds no restorable mutation")
    return tuple(
        RollbackIntent(
            operation_handle=record.operation_handle,
            seq=index,
            operation_id=mutation.operation_id,
            kind=mutation.kind,
            target=mutation.target,
            owner=mutation.owner,
            inverse={
                "action": RESTORE_INVERSE_ACTION,
                "source_receipt_handle": str(record.receipt_handle),
                "cleanup_authority": RESTORE_CLEANUP_AUTHORITY,
            },
            created_at=created_at,
        )
        for index, mutation in enumerate(restorable, start=1)
    )


def bounded_report(evidence: tuple[OperationEvidence, ...]) -> list[dict[str, object]]:
    """A bounded, JSON-safe projection of probe evidence for the journal."""

    report: list[dict[str, object]] = []
    used = 0
    for item in evidence:
        entry = {
            "operation_id": item.operation_id[:128],
            "kind": item.kind[:128],
            "ok": bool(item.ok),
            "detail": item.detail[:200],
            "evidence": {
                str(key)[:64]: str(value)[:200]
                for key, value in list(item.evidence.items())[:16]
            },
        }
        size = len(repr(entry))
        if used + size > _REPORT_LIMIT:
            report.append({"truncated": True})
            break
        used += size
        report.append(entry)
    return report


def _bounded_scalar(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in value
        )
    ):
        raise HostControlError(f"invalid {field_name}")
    return value


def _bounded_hex(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise HostControlError(f"invalid {field_name}")
    return value
