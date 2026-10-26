"""The broker's live-state reconciler (ORG-052 section 6; T-2917 5d.2).

Implements the ``operations.LiveStateReconciler`` protocol, unchanged, for a
mutating operation whose executor result was LOST (the executor no longer
holds the execution, or it refused a serve the broker had already made).
Rulings: T-2917:D5 (the reconciler lands here), D6 (cleanup authority),
D12 (the probe-clean rule), D13 (the read-only ``reconcile`` probe), D17
(the re-arm cap).

Decision table (``ReconcileOutcome.detail`` is a closed code,
``operations.RECONCILE_DETAIL_CODES``):

* verify: never routed here (the driver re-dispatches it under the
  lifetime cap); refused loudly if it is;
* the current spec arm was never served: ``clean_prestate`` /
  ``journal-clean``; the caller re-dispatches with NO re-arm;
* rollback, served and lost: ``clean_prestate`` / ``rollback-redispatch``
  (restores are idempotent per mutation; the caller re-arms) until the
  re-arm cap, then ``irreconcilable`` / ``rearm-cap``;
* apply, served and lost: the read-only probe observes the live host. When
  the FIRST mutating operation in plan order is a seat-root file operation
  whose live target is NOT at its desired state, nothing after it can have
  run: ``clean_prestate`` / ``probe-clean`` (the caller re-arms; at the cap,
  ``irreconcilable`` / ``rearm-cap``). Any other case is
  ``irreconcilable`` / ``lost-result-mutating``: the operation parks in
  ``manual_recovery`` carrying the bounded probe report;
* D6: a broker-synthesized timer-enable intent
  (``cleanup_authority=requires-owned-mutation-record``) is NEVER cleaned
  here: a lost result carries no owned mutation record, so each such intent
  is recorded in the report as ``d6-no-owned-record``. This reconciler has
  no cleanup path at all: it never returns ``restored`` or
  ``complete_poststate``.

A transient failure to observe (no executor, busy, ambiguous transport, a
probe still running at its bound) raises ``ReconcilePending``: nothing is
decided and nothing changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Protocol

from tools.host_control.contracts import (
    HostControlError,
    OperationReceipt,
    OperationRecord,
    ReconcileOutcome,
    RecoveryRefused,
    RollbackIntent,
)
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
from tools.host_control.journal import MAX_SPEC_REARMS, BrokerJournal
from tools.host_control.operations import REQUIRES_OWNED_RECORD, bounded_report
from tools.persona_installer_contracts import OperationEvidence

# the capability classes whose operations can change the host
MUTATING_CLASSES: Final = frozenset(("files.seat_root", "user_systemd.activate", "mailbox.probe"))
FILE_CLASS: Final = "files.seat_root"


class ReconcilePending(HostControlError):
    """The live state could not be observed right now; nothing was decided
    and nothing changed (retry later)."""


@dataclass(frozen=True)
class ProbeReport:
    """The read-only ``reconcile`` execution's typed observations."""

    evidence: tuple[OperationEvidence, ...]
    complete: bool
    detail: str = ""

    def observation(self, operation_id: str) -> OperationEvidence | None:
        for item in self.evidence:
            if item.operation_id == operation_id:
                return item
        return None


class ReconcileProbePort(Protocol):
    def probe(self, operation: OperationRecord) -> ProbeReport: ...


def _d6_entries(intents: tuple[RollbackIntent, ...]) -> tuple[OperationEvidence, ...]:
    return tuple(
        OperationEvidence(
            intent.operation_id,
            0,
            intent.kind,
            False,
            "d6-no-owned-record: never cleaned without the executor's owned mutation record",
            {"cleanup_authority": REQUIRES_OWNED_RECORD, "action": "none"},
        )
        for intent in intents
        if intent.inverse.get("cleanup_authority") == REQUIRES_OWNED_RECORD
    )


class BrokerLiveStateReconciler:
    def __init__(
        self,
        *,
        journal: BrokerJournal,
        probe: ReconcileProbePort,
        max_rearms: int = MAX_SPEC_REARMS,
        now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
    ) -> None:
        self._journal = journal
        self._probe = probe
        self._max_rearms = min(int(max_rearms), MAX_SPEC_REARMS)
        self._now = now

    def reconcile(
        self,
        operation: OperationRecord,
        staged_receipt: OperationReceipt,
        unresolved_intents: tuple[RollbackIntent, ...],
    ) -> ReconcileOutcome:
        if operation.kind == "verify":
            raise RecoveryRefused("a verify is re-dispatched, never reconciled")
        if operation.kind not in ("apply", "rollback"):
            raise RecoveryRefused("operation kind has no reconciliation")
        handle = operation.operation_handle
        served = self._journal.count_events(handle, ("spec_served",))
        rearmed = self._journal.count_events(handle, ("spec_rearmed",))
        d6 = _d6_entries(unresolved_intents)
        if served <= rearmed:
            # the current arm was never consumed: no executor holds this
            # spec, so nothing can have run under it
            return ReconcileOutcome("clean_prestate", (), d6, "journal-clean")
        if operation.kind == "rollback":
            if rearmed >= self._max_rearms:
                return self._park(operation, "rearm-cap", d6, ())
            return ReconcileOutcome("clean_prestate", (), d6, "rollback-redispatch")
        report = self._probe.probe(operation)  # ReconcilePending propagates
        self._record_probe(operation, report)
        if self._probe_clean(operation, report):
            if rearmed >= self._max_rearms:
                return self._park(operation, "rearm-cap", d6, report.evidence)
            return ReconcileOutcome("clean_prestate", (), d6, "probe-clean")
        return self._park(operation, "lost-result-mutating", d6, report.evidence)

    # ------------------------------------------------------------------

    @staticmethod
    def _park(
        operation: OperationRecord,
        code: str,
        d6: tuple[OperationEvidence, ...],
        observed: tuple[OperationEvidence, ...],
    ) -> ReconcileOutcome:
        return ReconcileOutcome("irreconcilable", (), tuple(observed) + d6, code)

    def _probe_clean(self, operation: OperationRecord, report: ProbeReport) -> bool:
        """T-2917:D12: the first mutating operation in plan order is a file
        operation whose live target is observed NOT at its desired state."""

        if not report.complete:
            return False
        plan = self._journal.get_plan(
            subject=operation.owner,
            plan_handle=operation.plan_handle,
            expected_digest=operation.plan_digest,
        )
        first = next(
            (
                item
                for item in plan.installer_plan.operations
                if EXECUTOR_KIND_CLASSES.get(item.kind) in MUTATING_CLASSES
            ),
            None,
        )
        if first is None or EXECUTOR_KIND_CLASSES.get(first.kind) != FILE_CLASS:
            return False
        observed = report.observation(first.operation_id)
        if observed is None or not observed.ok or observed.kind != first.kind:
            return False
        return (
            observed.evidence.get("observation") == "file-target"
            and observed.evidence.get("at_desired") == "false"
        )

    def _record_probe(self, operation: OperationRecord, report: ProbeReport) -> None:
        self._journal.append_event(
            current=operation,
            event_kind="reconcile_probe",
            payload={
                "complete": report.complete,
                "detail": report.detail[:200],
                "report": bounded_report(report.evidence),
            },
            created_at=self._now(),
        )
