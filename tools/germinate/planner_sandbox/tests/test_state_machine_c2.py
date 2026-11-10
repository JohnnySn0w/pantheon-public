"""C2 (parked-resume state-machine reachability + closure) red-before-green
tests for T-3293 (Tranche B). All record I/O is real filesystem I/O against
`temp_state_store()` -- no mocks -- matching the ADR's own atomic
temp-file + rename write pattern (Base engine mechanics, "Atomic
replacement rule").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.harness.fixtures import StateStore, temp_state_store
from tools.germinate.planner_sandbox.state_machine.reconciler import (
    Halted,
    NextState,
    RefuseMalformed,
    reconcile,
    trace_to_fixed_point,
)
from tools.germinate.planner_sandbox.state_machine.states import (
    INITIAL_STATE,
    TERMINAL_STATES,
    State,
)
from tools.germinate.planner_sandbox.state_machine.transitions import (
    TRANSITIONS,
    incoming,
    outgoing,
)

_INSTANCE = "test-instance"
_COMPONENT = "test-component"
_SCOPE = "system"
_LEG = "install"
_SCHEMA = "pantheon-run-state/v1"
_BOOT_A = "4a5b6c7d-89ab-cdef-0123-456789abcdef"
_BOOT_B = "00000000-0000-0000-0000-000000000000"


def _write_record(store: StateStore, run_id: str, payload: dict) -> Path:
    """Real atomic temp-file + rename write, matching ORG-080's own
    pattern and `test_harness_selftest.py`'s existing convention."""

    record_path = store.record_path(
        instance=_INSTANCE, component=_COMPONENT, scope=_SCOPE, leg=_LEG, run_id=run_id
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = record_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.rename(tmp_path, record_path)
    return record_path


def _base_record(state: State, **extra: object) -> dict:
    return {
        "schema": _SCHEMA,
        "tuple": {
            "instance": _INSTANCE,
            "component": _COMPONENT,
            "scope": _SCOPE,
            "leg": _LEG,
            "run_id": "01JATESTRUNID00000000000",
        },
        "state": state.value,
        **extra,
    }


# ---------------------------------------------------------------------------
# Test 1: Coverage exhaustion.
# ---------------------------------------------------------------------------


def test_coverage_exhaustion_refuses_incomplete_closure() -> None:
    """Every non-terminal state has >=1 admissible outgoing transition;
    every non-initial state has >=1 admissible incoming transition. Pure
    structural check over the closed transition table (ORG-080 C2(a)/(b));
    red-before-green until TRANSITIONS is fully populated."""

    non_terminal = set(State) - TERMINAL_STATES
    covered_outgoing = {t.frm for t in TRANSITIONS}
    missing_outgoing = non_terminal - covered_outgoing
    assert not missing_outgoing, f"states with zero outgoing transitions: {missing_outgoing}"

    non_initial = set(State) - {INITIAL_STATE}
    covered_incoming = {t.to for t in TRANSITIONS}
    missing_incoming = non_initial - covered_incoming
    assert not missing_incoming, f"states with zero incoming transitions: {missing_incoming}"

    for terminal in TERMINAL_STATES:
        assert outgoing(terminal) == (), f"terminal state {terminal} has an outgoing transition"
        assert incoming(terminal), f"terminal state {terminal} has no defined entry condition"


# Reconciler-scoped edges (the subset reconcile() itself fires -- see
# reconciler.py's module docstring for the scope boundary). Each case is
# (test id, state written to the fixture record, extra fixture fields,
# current_boot_id, expected result).
_RECONCILER_CASES = [
    (
        "from_pending_admits_active_via_admission_flip",
        State.PENDING,
        {},
        _BOOT_A,
        NextState(
            State.ACTIVE,
            "admission-transaction reconciliation: complete a crashed flip "
            "(step-3 succeeded, step-4 crashed)",
        ),
    ),
    (
        "from_active_admits_halted_via_reboot_interrupt",
        State.ACTIVE,
        {"boot_id": _BOOT_B},
        _BOOT_A,
        Halted(op_id="system-reboot", reason="interrupted-by-reboot"),
    ),
    (
        "from_active_admits_active_stable_via_no_reconciliation",
        State.ACTIVE,
        {"boot_id": _BOOT_A},
        _BOOT_A,
        NextState(State.ACTIVE, "no reconciliation; normal in-flight run"),
    ),
    (
        "from_halted_admits_parked_via_reboot_recovery_park",
        State.HALTED,
        {"halted": {"op_id": "system-reboot", "reason": "interrupted-by-reboot"}},
        _BOOT_A,
        NextState(
            State.PARKED,
            "reboot-recovery auto-transit toward reclaim (C2(d) first hop)",
        ),
    ),
    (
        "from_halted_admits_halted_stable_via_no_reconciliation",
        State.HALTED,
        {"halted": {"op_id": "op-42", "reason": "some-operation-failed"}},
        _BOOT_A,
        NextState(
            State.HALTED,
            "no reconciliation; awaiting operator verb (rollback or apply-retry)",
        ),
    ),
    (
        "from_parked_admits_active_via_reboot_recovery_reclaim",
        State.PARKED,
        {"parking": None},
        _BOOT_A,
        NextState(
            State.ACTIVE,
            "reboot-recovery reclaim: transit PARKED carried no "
            "operator-facing parking payload (C2(d) second hop)",
        ),
    ),
    (
        "from_parked_admits_parked_stable_via_no_reconciliation",
        State.PARKED,
        {
            "parking": {
                "resume_handle": "01JAHANDLE00000000000000",
                "check_id": "operator-confirm",
            }
        },
        _BOOT_A,
        NextState(
            State.PARKED,
            "no reconciliation; normal parked state, awaiting germinate resume",
        ),
    ),
    (
        "from_pending_park_admits_parked_via_parking_committed",
        State.PENDING_PARK,
        {
            "_prior_state": State.ACTIVE.value,
            "_handle_written": True,
            "_index_written": True,
        },
        _BOOT_A,
        NextState(
            State.PARKED,
            "parking transaction step-4 succeeded, step-5 crashed; "
            "complete step-5 (idempotent completion)",
        ),
    ),
    (
        # ORG-080 run-state note (notes/2026-11-02-org-080-run-state-relocated.md) §Reconciliation for every prefix state, step-2 row; reconciler.py's rollback_step2_crash path.
        "from_pending_park_admits_active_via_rollback_step2_crash",
        State.PENDING_PARK,
        {"_prior_state": State.ACTIVE.value, "_handle_written": False, "_index_written": False},
        _BOOT_A,
        NextState(
            State.ACTIVE,
            "parking transaction step-2 succeeded, step-3 crashed; roll "
            "back to prior state",
        ),
    ),
    (
        # ORG-080 run-state note (notes/2026-11-02-org-080-run-state-relocated.md) §Reconciliation for every prefix state, step-3 row; reconciler.py's rollback_step3_crash path --
        # mirror-shape of the step2 case above, distinguished only by
        # _handle_written: True (REBOUND r2 fix: this edge was declared in
        # transitions.py per r1 but not previously driven by any fixture).
        "from_pending_park_admits_active_via_rollback_step3_crash",
        State.PENDING_PARK,
        {"_prior_state": State.ACTIVE.value, "_handle_written": True, "_index_written": False},
        _BOOT_A,
        NextState(
            State.ACTIVE,
            "parking transaction step-3 succeeded, step-4 crashed; unlink "
            "handle (immutable-authority allows unlink on abandonment), "
            "roll back to prior state",
        ),
    ),
    (
        "from_pending_park_admits_parking_failed_via_reconciliation_unclassifiable",
        State.PENDING_PARK,
        {"_prior_state": None, "_handle_written": False, "_index_written": False},
        _BOOT_A,
        NextState(
            State.PARKING_FAILED,
            "reconciliation-failed halted escalation: prefix unclassifiable",
        ),
    ),
]

# Non-reconciler-edge cases: fixture inputs that should refuse rather than
# land on a declared TRANSITIONS edge. Handled by a separate, smaller
# parametrized test below (not _RECONCILER_CASES / the subset-invariant
# assertion, since RefuseMalformed carries no target state to check against
# the closed graph).
_MALFORMED_PRIOR_STATE_CASES = [
    (
        # REBOUND r2 optional tightening: the try/except ValueError ->
        # RefuseMalformed wrapper added in r1 around
        # State(data["_prior_state"]) was itself untested for an
        # unenumerated _prior_state value.
        "from_pending_park_refuses_via_bogus_prior_state_step2",
        {"_prior_state": "BOGUS_STATE", "_handle_written": False, "_index_written": False},
    ),
    (
        "from_pending_park_refuses_via_bogus_prior_state_step3",
        {"_prior_state": "BOGUS_STATE", "_handle_written": True, "_index_written": False},
    ),
]


@pytest.mark.parametrize(
    "case_id,state,extra,current_boot_id,expected",
    _RECONCILER_CASES,
    ids=[c[0] for c in _RECONCILER_CASES],
)
def test_transition_from_state_admits_successor(
    case_id: str, state: State, extra: dict, current_boot_id: str, expected: object
) -> None:
    with temp_state_store() as store:
        record_path = _write_record(store, "01JATESTRUNID00000000000", _base_record(state, **extra))
        record_text = record_path.read_text(encoding="utf-8")
        result = reconcile(record_text, current_boot_id=current_boot_id)
        assert result == expected, f"{case_id}: got {result!r}, expected {expected!r}"

        # Reconciler-vs-declared-graph subset invariant (REBOUND r1 fix):
        # every non-self-loop edge reconcile() actually fires must be named
        # in transitions.TRANSITIONS -- reconcile()'s own module docstring
        # claims it is a subset of that closed graph; this assertion makes
        # the claim machine-checked instead of merely documented. Self-loop
        # "stable, no reconciliation" results are deliberately NOT edges in
        # TRANSITIONS, so they're excluded here.
        target = State.HALTED if isinstance(result, Halted) else result.state
        if target is not state:
            declared_pairs = {(t.frm, t.to) for t in TRANSITIONS}
            assert (state, target) in declared_pairs, (
                f"{case_id}: reconcile() fires {state.value}->{target.value} "
                "but no such edge is declared in transitions.TRANSITIONS"
            )


@pytest.mark.parametrize(
    "case_id,extra",
    _MALFORMED_PRIOR_STATE_CASES,
    ids=[c[0] for c in _MALFORMED_PRIOR_STATE_CASES],
)
def test_pending_park_refuses_on_unenumerated_prior_state(case_id: str, extra: dict) -> None:
    """REBOUND r2 optional tightening: the r1 try/except ValueError ->
    RefuseMalformed wrapper around `State(data["_prior_state"])` is itself
    exercised here for both PENDING_PARK rollback branches (step-2-crash
    and step-3-crash), so an unenumerated `_prior_state` fixture value
    refuses instead of raising -- was previously untested."""

    with temp_state_store() as store:
        record_path = _write_record(
            store, "01JABOGUSPRIORID00000000", _base_record(State.PENDING_PARK, **extra)
        )
        record_text = record_path.read_text(encoding="utf-8")
        result = reconcile(record_text, current_boot_id=_BOOT_A)

    assert isinstance(result, RefuseMalformed), f"{case_id}: got {result!r}"
    assert "BOGUS_STATE" in result.reason


# ---------------------------------------------------------------------------
# Test 2: Reboot-recovery trace (Consultant v9 BLOCK-2 witness).
# ---------------------------------------------------------------------------


def test_reboot_recovery_traces_halted_parked_active_in_order() -> None:
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if boot_id_path.exists():
        current_boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    else:
        current_boot_id = _BOOT_A  # non-Linux fallback; still a real string comparison

    stale_boot_id = "00000000-0000-0000-0000-000000000000"
    assert stale_boot_id != current_boot_id

    with temp_state_store() as store:
        record = _base_record(State.ACTIVE, boot_id=stale_boot_id)
        record_path = _write_record(store, "01JAREBOOTRUNID000000000", record)

        trace = trace_to_fixed_point(record, current_boot_id=current_boot_id)

        # Persist the final in-memory record state to disk too, proving the
        # trace and the on-disk artifact agree (real I/O round trip).
        record_path.write_text(json.dumps(record), encoding="utf-8")

    assert len(trace) == 3, f"expected 3 logged transitions, got {len(trace)}: {trace}"

    assert isinstance(trace[0], Halted)
    assert trace[0].op_id == "system-reboot"
    assert trace[0].reason == "interrupted-by-reboot"

    assert isinstance(trace[1], NextState)
    assert trace[1].state is State.PARKED

    assert isinstance(trace[2], NextState)
    assert trace[2].state is State.ACTIVE

    assert record["state"] == State.ACTIVE.value
    assert record["boot_id"] == current_boot_id


# ---------------------------------------------------------------------------
# Test 3: PARKING_FAILED escape.
# ---------------------------------------------------------------------------


def test_parking_failed_only_admits_rollback() -> None:
    # Structural: the closed graph admits exactly one outgoing edge from
    # PARKING_FAILED, and it targets ROLLING_BACK.
    edges = outgoing(State.PARKING_FAILED)
    assert len(edges) == 1, f"PARKING_FAILED has {len(edges)} outgoing edges, expected 1"
    assert edges[0].to is State.ROLLING_BACK
    assert edges[0].trigger == "operator_rollback"

    # Real I/O: a genuine on-disk PARKING_FAILED record is never
    # auto-advanced by reconcile() -- it stays put, awaiting the operator
    # verb (the state-machine gate refuses every OTHER verb; reconcile()
    # never invents a way out on its own).
    with temp_state_store() as store:
        record_path = _write_record(
            store, "01JAPARKFAILRUNID0000000", _base_record(State.PARKING_FAILED)
        )
        record_text = record_path.read_text(encoding="utf-8")
        result = reconcile(record_text, current_boot_id=_BOOT_A)

    assert isinstance(result, NextState)
    assert result.state is State.PARKING_FAILED


# ---------------------------------------------------------------------------
# Test 4: Malformed-record refusal.
# ---------------------------------------------------------------------------


def test_malformed_record_refuses_without_inventing_state() -> None:
    with temp_state_store() as store:
        record_path = _write_record(
            store, "01JAMALFORMEDRUNID000000", _base_record(State.ACTIVE)
        )
        # Corrupt the persisted `state` value after the fact -- an
        # unenumerated value the closed enum does not name.
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        payload["state"] = "NOT_A_REAL_STATE"
        record_path.write_text(json.dumps(payload), encoding="utf-8")

        before = record_path.read_text(encoding="utf-8")
        result = reconcile(before, current_boot_id=_BOOT_A)
        after = record_path.read_text(encoding="utf-8")

    assert isinstance(result, RefuseMalformed)
    assert "NOT_A_REAL_STATE" in result.reason
    assert before == after  # reconcile() never writes; record is untouched
