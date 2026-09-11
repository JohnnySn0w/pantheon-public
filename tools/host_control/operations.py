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
from typing import Callable, Literal, NoReturn, Protocol

from tools.host_control.contracts import (
    OPERATION_RECEIPT_SCHEMA,
    OPERATION_SCHEMA,
    OPERATION_STATES,
    OPERATION_TRANSITIONS,
    TERMINAL_OPERATION_STATES,
    ApprovalUnavailable,
    BindOutcome,
    HostControlError,
    InvalidTransition,
    OperationReceipt,
    OperationRecord,
    PlanEnvelope,
    ReconcileOutcome,
    RecoveryRefused,
    RollbackIntent,
    UnknownHandle,
    sha256_json,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.registry import SeatRegistry
from tools.persona_installer import MutationRecord, OperationEvidence


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
        approval_handle = _bounded_scalar(approval_handle, "approval handle")
        subject = self._registry.subject_for_uid(authenticated_uid)
        plan = self._journal.get_plan(
            subject=subject,
            plan_handle=plan_handle,
            expected_digest=plan_digest,
        )
        now = self._now()
        self._require_unexpired(plan, now)
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
        approval_handle = _bounded_scalar(approval_handle, "approval handle")
        subject = self._registry.subject_for_uid(authenticated_uid)
        receipt = self._journal.get_receipt(
            subject=subject,
            receipt_handle=receipt_handle,
        )
        if receipt.phase != "complete":
            raise HostControlError(
                "rollback requires a complete receipt"
            )
        source = self._journal.get_operation(
            subject=subject,
            operation_handle=receipt.operation_handle,
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

    def prepare_verify(self, *args: object, **kwargs: object) -> NoReturn:
        # Directed relaxation (broker-po, T-2909 disposition 3): "verify" is
        # accepted at the SCHEMA tier so adding its path never needs a
        # journal migration, but the service refuses it until T-2917 lands
        # the verify path.
        raise HostControlError(
            "verify operations are not implemented in this unit; "
            "the verify path is owned by T-2917"
        )

    # ------------------------------------------------------------------
    # authorize (ValidateAndBind, identical tuple on every retry)
    # ------------------------------------------------------------------

    def authorize(self, operation_handle: str) -> OperationRecord:
        record = self._journal.load_operation_unbound(operation_handle)
        if record.state != "prepared":
            raise InvalidTransition(
                "authorize requires a prepared operation"
            )
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
        execution_ref = executor.execute(with_intent, staged_receipt, intents)
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
    ) -> OperationRecord:
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
        self._journal.record_terminal(
            current=record,
            new=new,
            receipt=receipt,
            payload={
                "ok": ok,
                "detail": detail[:512],
                "receipt_handle": receipt.receipt_handle,
                "evidence_count": len(evidence),
            },
        )
        return new

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
            execution_ref = _bounded_scalar(
                executor.execute(record, staged, intents),
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
            self._journal.transition_operation(
                current=record,
                new=parked,
                event_kind="irreconcilable",
                payload={"detail": outcome.detail[:512]},
            )
            raise RecoveryRefused(
                "live state is irreconcilable with the staged receipt and "
                "rollback intents; operation parked in manual_recovery"
            )
        raise HostControlError("invalid reconcile outcome")

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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
        intents = []
        seq = 0
        for operation in plan.installer_plan.operations:
            if not operation.inverse:
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
                    inverse={
                        str(key): value
                        for key, value in operation.inverse.items()
                    },
                    created_at=created_at,
                )
            )
        if not intents:
            raise HostControlError(
                "plan declares no invertible operations; refusing dispatch"
            )
        return tuple(intents)


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
