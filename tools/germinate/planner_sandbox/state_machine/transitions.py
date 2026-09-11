"""Machine-checkable transition table for the parked-resume state machine.

Closed, declarative spec of every admissible (frm -> to) edge, satisfying
ORG-080 C2(a)/(b)/(c)/(e) by construction -- see
`tests/test_state_machine_c2.py::test_coverage_exhaustion_refuses_incomplete_closure`
for the machine-checked proof.

Not every edge here is realized by `reconciler.reconcile()`. reconcile()
only owns the RECONCILER-scoped subset (crash/boot-prefix classification,
fired at "executor start ... before any new plan is admitted" per ORG-080
§Base engine mechanics). The remaining edges are operator-verb- or
live-executor-realized (park request, leg completion, resume, rollback,
verify outcomes, apply-retry) -- they are proven ADMISSIBLE here
(closed-graph facts, satisfying C2) without Tranche B implementing the
code that fires them; that code belongs to the CLI-verb handlers / live
executor, outside this tranche's scope. See T-3293 DECISIONS.

Deliberately NOT modeled: ROLLBACK_INCOMPLETE's "gated `remove`" recovery
path (ORG-080 §Mid-leg failure / Rollback failure). That path decommissions
the component and tombstones the record rather than transitioning
`record.state` to another enumerated value, so it sits outside this closed
state graph. PO-approved 2026-08-26 (T-3293 Phase 1 plan review, Open
Question #4).
"""

from __future__ import annotations

from dataclasses import dataclass

from .states import State


@dataclass(frozen=True)
class Transition:
    """One admissible edge in the closed state graph."""

    frm: State
    to: State
    trigger: str
    note: str


TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        State.PENDING,
        State.ACTIVE,
        "admission_flip",
        "ORG-080 Base engine mechanics step 4 'Optional flip' / "
        "admission-transaction reconciliation-matrix row 3 (step-3 "
        "succeeded, step-4 crashed)",
    ),
    Transition(
        State.ACTIVE,
        State.PENDING_PARK,
        "park_request",
        "Handle-to-record parking transaction step 2 (Prepare record)",
    ),
    Transition(
        State.ACTIVE,
        State.HALTED,
        "mid_leg_failure",
        "§Mid-leg failure: the failing operation writes a HALTED marker "
        "naming the failed operation",
    ),
    Transition(
        State.ACTIVE,
        State.HALTED,
        "reboot_interrupt",
        "boot_id mismatch at engine startup classifies a previously-ACTIVE "
        "run as HALTED(op_id='system-reboot', reason='interrupted-by-reboot')",
    ),
    Transition(
        State.ACTIVE,
        State.SATISFIED,
        "leg_complete",
        "Terminal-state release: normal successful leg completion",
    ),
    Transition(
        State.PENDING_PARK,
        State.PARKED,
        "parking_committed",
        "Parking transaction steps 3-5 complete (handle written, index "
        "committed, flip to PARKED)",
    ),
    Transition(
        State.PENDING_PARK,
        State.PARKING_FAILED,
        "reconciliation_unclassifiable",
        "'Reconciliation-failed halted escalation': prefix cannot be "
        "classified against the parking-transaction matrix",
    ),
    Transition(
        State.PENDING_PARK,
        State.ACTIVE,
        "rollback_step2_crash",
        "ORG-080 L403 reconciliation-matrix row: step-2 succeeded, step-3 "
        "crashed; roll back record to `_prior_state.json.prior_state` "
        "(ACTIVE -- PENDING_PARK's sole predecessor in this closed graph)",
    ),
    Transition(
        State.PENDING_PARK,
        State.ACTIVE,
        "rollback_step3_crash",
        "ORG-080 L404 reconciliation-matrix row: step-3 succeeded, step-4 "
        "crashed; unlink resume/handle.json (immutable-authority allows "
        "unlink on abandonment), roll back record to prior state",
    ),
    Transition(
        State.PARKED,
        State.VERIFYING,
        "resume_invoked",
        "`germinate resume <handle>` triggers the verify-check",
    ),
    Transition(
        State.PARKED,
        State.ROLLING_BACK,
        "rollback_of_parked_run",
        "§Abandonment with external state: rollback of a parked run runs "
        "each in-flight interaction's abandon_check",
    ),
    Transition(
        State.PARKED,
        State.ACTIVE,
        "reboot_recovery_reclaim",
        "C2(d): reconciler-internal deterministic reclaim; second hop of "
        "the reboot-recovery path",
    ),
    Transition(
        State.VERIFYING,
        State.ACTIVE,
        "verify_satisfied",
        "`satisfied` outcome: transition to the active run, continue from "
        "the next operation",
    ),
    Transition(
        State.VERIFYING,
        State.PARKED,
        "verify_unsatisfied_or_error",
        "`unsatisfied(reason)`/`check_error(reason)` outcomes both loop "
        "back to PARKED with an updated instruction",
    ),
    Transition(
        State.HALTED,
        State.ROLLING_BACK,
        "operator_rollback",
        "§Mid-leg failure operator option (a): `germinate rollback --run "
        "<run-id>`",
    ),
    Transition(
        State.HALTED,
        State.ACTIVE,
        "operator_apply_retry",
        "§Mid-leg failure operator option (b): `germinate apply <same "
        "scope>` re-run, plan-from-current-state",
    ),
    Transition(
        State.HALTED,
        State.PARKED,
        "reboot_recovery_park",
        "C2(d): reconciler-internal auto-transit toward reclaim; first hop "
        "of the reboot-recovery path",
    ),
    Transition(
        State.PARKING_FAILED,
        State.ROLLING_BACK,
        "operator_rollback",
        "C2(b): PARKING_FAILED's ONLY admissible outgoing transition",
    ),
    Transition(
        State.ROLLING_BACK,
        State.ROLLED_BACK,
        "rollback_complete",
        "Successful rollback completion",
    ),
    Transition(
        State.ROLLING_BACK,
        State.ROLLBACK_INCOMPLETE,
        "rollback_partial_failure",
        "§Rollback failure: `germinate rollback` itself fails partway",
    ),
    Transition(
        State.ROLLBACK_INCOMPLETE,
        State.ROLLING_BACK,
        "rollback_retry",
        "§Rollback failure: 'exact germinate rollback --run <run-id> may "
        "retry'",
    ),
)


def outgoing(state: State) -> tuple[Transition, ...]:
    """Every admissible transition OUT of `state`."""

    return tuple(t for t in TRANSITIONS if t.frm is state)


def incoming(state: State) -> tuple[Transition, ...]:
    """Every admissible transition INTO `state`."""

    return tuple(t for t in TRANSITIONS if t.to is state)
