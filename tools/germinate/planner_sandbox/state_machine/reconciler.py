"""Reconciliation function for the parked-resume state machine (T-3293).

`reconcile()` walks ONE real run-record's raw content and returns exactly
one of {NextState, Halted, RefuseMalformed} -- the reconciler-scoped
subset of `transitions.TRANSITIONS` (see that module's docstring for the
scope boundary against operator-verb-realized edges). Pure function: no
I/O, no wall-clock reads -- callers own reading the record text off disk
and writing the result back (see `tests/test_state_machine_c2.py` for the
real-`temp_state_store()` round-trip this drives).

Fixture-only reconciliation-context fields (T-3293 DECISIONS, PO-approved
Open Question #3): `boot_id`, `_prior_state`, `_handle_written`,
`_index_written` are NOT part of ORG-080's literal `record.json` schema
(L290-302) -- production reconciliation assembles this context by joining
the run-record with sibling on-disk artifacts (the gate's `boot_id`, the
parking transaction's `_prior_state.json` / `resume/handle.json` /
`schema-v1/handles/<handle>.json`, per ORG-080 L342 and L398-409). Tranche
B pre-flattens that joined context onto `record` so the state-transition
LOGIC is testable standalone, without a live Tranche-A gate-tree join.
These fields are test-only fixture fields; the production record shape
stays the ADR-literal `record.json` schema -- the live join is integration
wiring for a separate follow-up arc (P5: roles don't hop).

Scope note: only the matrix rows Tranche B's tests exercise are
implemented (not the ADR's full reconciliation matrix -- e.g. the
tampering-signal rows for an index-only orphan, or a PARKED record whose
index vanished, are out of scope; see T-3293 report's out-of-scope
discoveries).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .states import State

_SCHEMA = "pantheon-run-state/v1"


@dataclass(frozen=True)
class NextState:
    """Reconcile the record forward to `state` (may equal the current
    state -- a self-loop meaning "stable, no reconciliation needed")."""

    state: State
    reason: str


@dataclass(frozen=True)
class Halted:
    """Reconcile the record to HALTED, carrying the schema's `halted`
    payload shape (`{op_id, reason}` -- `at` is a write-time stamp the
    caller adds; reconcile() stays a pure function, no wall-clock read)."""

    op_id: str
    reason: str


@dataclass(frozen=True)
class RefuseMalformed:
    """The record's content cannot be classified -- refuse; never invent
    a next state (ORG-080 'Reconciliation policy')."""

    reason: str


ReconcileResult = NextState | Halted | RefuseMalformed


def reconcile(record: str, *, current_boot_id: str) -> ReconcileResult:
    """Classify one real run-record's raw JSON text into a ReconcileResult.

    Owns JSON parsing itself so a corrupt/unparseable record folds into
    RefuseMalformed rather than raising from outside this function's
    control (T-3293 DECISIONS) -- this is what lets a multi-record sweep
    refuse ONE bad record "without progressing others" (ORG-080 C4 test 2)
    using a plain per-record loop with no per-item try/except needed.
    """

    try:
        data = json.loads(record)
    except json.JSONDecodeError as exc:
        return RefuseMalformed(reason=f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return RefuseMalformed(reason=f"record is not a JSON object: {type(data).__name__}")

    schema = data.get("schema")
    if schema != _SCHEMA:
        return RefuseMalformed(reason=f"unknown schema: {schema!r}")

    raw_state = data.get("state")
    try:
        state = State(raw_state)
    except ValueError:
        return RefuseMalformed(reason=f"unenumerated state: {raw_state!r}")

    if state is State.PENDING:
        return NextState(
            State.ACTIVE,
            "admission-transaction reconciliation: complete a crashed "
            "flip (step-3 succeeded, step-4 crashed)",
        )

    if state is State.ACTIVE:
        record_boot_id = data.get("boot_id")
        if record_boot_id != current_boot_id:
            return Halted(op_id="system-reboot", reason="interrupted-by-reboot")
        return NextState(State.ACTIVE, "no reconciliation; normal in-flight run")

    if state is State.HALTED:
        halted = data.get("halted") or {}
        if halted.get("op_id") == "system-reboot":
            return NextState(
                State.PARKED,
                "reboot-recovery auto-transit toward reclaim (C2(d) first hop)",
            )
        return NextState(
            State.HALTED,
            "no reconciliation; awaiting operator verb (rollback or apply-retry)",
        )

    if state is State.PARKED:
        if data.get("parking") is None:
            return NextState(
                State.ACTIVE,
                "reboot-recovery reclaim: transit PARKED carried no "
                "operator-facing parking payload (C2(d) second hop)",
            )
        return NextState(
            State.PARKED,
            "no reconciliation; normal parked state, awaiting germinate resume",
        )

    if state is State.PENDING_PARK:
        has_snapshot = bool(data.get("_prior_state"))
        has_handle = bool(data.get("_handle_written"))
        has_index = bool(data.get("_index_written"))

        if has_snapshot and has_handle and has_index:
            return NextState(
                State.PARKED,
                "parking transaction step-4 succeeded, step-5 crashed; "
                "complete step-5 (idempotent completion)",
            )
        if has_snapshot and has_handle and not has_index:
            try:
                prior_state = State(data["_prior_state"])
            except ValueError:
                return RefuseMalformed(
                    reason=f"PENDING_PARK _prior_state unenumerated: {data['_prior_state']!r}"
                )
            return NextState(
                prior_state,
                "parking transaction step-3 succeeded, step-4 crashed; "
                "unlink handle (immutable-authority allows unlink on "
                "abandonment), roll back to prior state",
            )
        if has_snapshot and not has_handle and not has_index:
            try:
                prior_state = State(data["_prior_state"])
            except ValueError:
                return RefuseMalformed(
                    reason=f"PENDING_PARK _prior_state unenumerated: {data['_prior_state']!r}"
                )
            return NextState(
                prior_state,
                "parking transaction step-2 succeeded, step-3 crashed; "
                "roll back to prior state",
            )
        # Snapshot absent, or an ordering the real transaction's write
        # sequence cannot produce (e.g. index present without handle) --
        # unclassifiable.
        return NextState(
            State.PARKING_FAILED,
            "reconciliation-failed halted escalation: prefix unclassifiable",
        )

    # Every other enumerated state has no reconciler-owned transition in
    # this tranche's scope (operator-verb- or live-executor-realized only,
    # per this module's docstring) -- stable self-loop.
    return NextState(state, f"no reconciliation defined for {state.value}; stable")


def trace_to_fixed_point(
    record: dict, *, current_boot_id: str, max_steps: int = 8
) -> list[ReconcileResult]:
    """Repeatedly reconcile `record` (mutated in place between steps),
    returning the ordered, non-stable results -- the "transitions logged
    in order" acceptance signal ORG-080 C2 test 2 requires.

    reconcile() itself stays single-step and pure; this loop is the only
    place that applies a result back onto `record` between calls, mirroring
    production's real write-then-re-reconcile cycle. Stops at the first
    NextState result that targets the state the record already holds (a
    stable fixed point) -- that final idempotent call is NOT appended to
    the returned trace.
    """

    trace: list[ReconcileResult] = []
    for _ in range(max_steps):
        result = reconcile(json.dumps(record), current_boot_id=current_boot_id)

        if isinstance(result, NextState) and result.state.value == record.get("state"):
            break  # stable fixed point; nothing new to log

        trace.append(result)

        if isinstance(result, RefuseMalformed):
            break

        if isinstance(result, Halted):
            record["state"] = State.HALTED.value
            record["halted"] = {"op_id": result.op_id, "reason": result.reason}
            continue

        record["state"] = result.state.value
        if result.state is State.ACTIVE:
            record["boot_id"] = current_boot_id

    return trace
