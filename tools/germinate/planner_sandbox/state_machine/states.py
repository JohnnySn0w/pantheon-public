"""Closed enum of every persisted `record.state` value the parked-resume
state machine (ORG-080 §Base engine mechanics + §Parked-resume state
machine) can hold, as proven reachable/closed by T-3293 (Tranche B).

Reconciled against the ADR's literal 12-member schema-table enum
(ORG-080 L296) with ONE collapse: `PARKING` is not modeled as a distinct
member here -- the concrete 7-step parking transaction (ORG-080 L381-389,
"Handle-to-record parking transaction") never writes `state: PARKING`,
only `PENDING_PARK` then `PARKED`; the schema table's own parenthetical
step-citations are internally inconsistent (misattributes which
transaction step writes which value). Per ORG-080 L58-77
(§Architectural-tier scope carve -- "the reference-implementation is free
to demonstrate an equally-valid mechanism ... where documentary prose
conflicts with the reference-implementation, the reference-implementation
wins on mechanism"), this ref-impl treats PARKING as a documentary
duplicate of PENDING_PARK rather than inventing a synthetic checkpoint no
described write produces -- an invented checkpoint would itself violate
C2's own closure requirement (a state with no honest transition attached
to it). PO-approved 2026-08-26 (T-3293 Phase 1 plan review, Open
Question #1).
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """Every value `record.json`'s `state` field can hold (11 members)."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PENDING_PARK = "PENDING_PARK"
    PARKED = "PARKED"
    VERIFYING = "VERIFYING"
    PARKING_FAILED = "PARKING_FAILED"
    HALTED = "HALTED"
    SATISFIED = "SATISFIED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE"


#: The sole state with no admissible predecessor -- fresh admission write
#: (ORG-080 §Base engine mechanics "Gate + record admission transaction"
#: step 2). C2(a)'s "every non-initial persisted state has >=1 admissible
#: incoming" exempts exactly this one state.
INITIAL_STATE: State = State.PENDING

#: Genuinely terminal, absorbing states -- C2(c) verbatim: "SATISFIED,
#: ROLLED_BACK -- natural completions of the forward and rollback flows
#: respectively." No other state in the enum is terminal (PARKING_FAILED
#: and ROLLBACK_INCOMPLETE are explicitly non-terminal per C2(b) / §10).
TERMINAL_STATES: frozenset[State] = frozenset({State.SATISFIED, State.ROLLED_BACK})
