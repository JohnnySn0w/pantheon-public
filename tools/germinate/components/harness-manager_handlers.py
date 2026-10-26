"""harness-manager component: executor-side registration (ORG-080 §2; T-5090, GF1).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``harness.runtime`` class.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier, nothing live; the
actuation boundary of the HO ruling, T-3296/T-5106). ``HarnessSeatSim`` keeps,
per seat account, JSON stand-ins for what the existing swap writes:

* ``runtime``    -- runtime-controller/runtime.json's selection
                    (runtime_controller.py:7255, ``_replace_runtime``);
* ``receiver``   -- which harness holds the Telegram receiver
                    (the bridge handoff, runtime_controller.py:7794);
* ``transition`` -- runtime-controller/transition.json's record
                    (runtime_controller.py:7963-7999 / :8048);
* ``readiness``  -- the simulated result of ``prove_runtime_readiness`` per runtime;
* plus read-only plan inputs (role block, holds, communications) and the verify
  ``facts`` block harness_manager_verify evaluates.

Every op rewrites exactly one slot (``transition`` | ``receiver`` | ``runtime``).
The write-ahead inverse is the slot's JSON before the op; ``can_rollback`` tells
not-applied (slot == before) / owned (slot == the op's own result) / drifted.
"""

from __future__ import annotations

import copy
import hashlib
import json

from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "harness.transition-stage": ("harness.runtime", "seat"),
    "harness.receiver-handoff": ("harness.runtime", "seat"),
    "harness.runtime-replace": ("harness.runtime", "seat"),
    "harness.transition-commit": ("harness.runtime", "seat"),
}

_SLOT_OF = {
    "harness.transition-stage": "transition",
    "harness.transition-commit": "transition",
    "harness.receiver-handoff": "receiver",
    "harness.runtime-replace": "runtime",
}
_SEAT_PREFIX = "seat-"


def _canon(value):
    return json.dumps(value, sort_keys=True)


def _fingerprint(value):
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


class HarnessSeatSim:
    name = "harness-manager-seat"
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = {"seats": {}}

    # --- slots -----------------------------------------------------------------

    @staticmethod
    def _split(target):
        account, _, slot = target.partition(":")
        return account, slot

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated seat {account!r}")
        return seat

    def _slot(self, op_or_mutation):
        account, slot = self._split(op_or_mutation.target)
        if slot != _SLOT_OF[op_or_mutation.kind]:
            raise KeyError(f"{op_or_mutation.kind} cannot write slot {slot!r}")
        return self._seat(account), slot

    @staticmethod
    def _result(op):
        """The slot value the op leaves (a pure function of the op)."""

        if op.kind == "harness.runtime-replace":
            return {"selected": op.desired["selected"]}
        if op.kind == "harness.receiver-handoff":
            holder = op.desired["holder"]
            return {"holder": None if holder == "none" else holder, "last_handoff": f"{op.desired['direction']}:{op.desired['phase']}"}
        return {k: op.desired[k] for k in ("transition_id", "outgoing", "incoming", "phase")}

    @staticmethod
    def _matches(slot, value, precondition):
        if slot == "transition":
            if precondition.get("phase") == "none":
                return value is None
            return isinstance(value, dict) and all(value.get(k) == v for k, v in precondition.items())
        if slot == "receiver":
            holder = (value or {}).get("holder") or "none"
            return holder == precondition.get("holder")
        return (value or {}).get("selected") == precondition.get("selected")

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "seat": None}
        account = scope[len(_SEAT_PREFIX):]
        seat = self.state["seats"].get(account)
        return {"account": account, "seat": copy.deepcopy(seat)}

    def preflight(self, op):
        try:
            seat, slot = self._slot(op)
        except KeyError as exc:
            return PreflightResult(False, self.name, str(exc))
        ok = self._matches(slot, seat.get(slot), op.precondition)
        return PreflightResult(ok, self.name, f"{slot} at its precondition" if ok else f"{slot} moved from its precondition")

    def rollback_intent(self, op):
        seat, slot = self._slot(op)
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"slot": _canon(seat.get(slot))}, _fingerprint(self._result(op)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        seat, slot = self._slot(op)
        seat[slot] = self._result(op)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def _postcondition(self, op, value):
        """The op's postcondition as a predicate that stays true once later ops of
        the same swap advance the slot (engine verify re-checks every receipted op):
        a staged record holds for its transition at or past staging; the "pre"
        handoff holds while the source no longer holds the receiver (the
        probe-<source>-absent action)."""

        if op.kind == "harness.transition-stage":
            ok = isinstance(value, dict) and all(value.get(k) == op.desired[k] for k in ("transition_id", "outgoing", "incoming"))
            return ok and value.get("phase") in ("preparing", "awaiting-successor-read")
        if op.kind == "harness.receiver-handoff" and op.desired["phase"] == "pre":
            return ((value or {}).get("holder") or "none") != op.precondition["holder"]
        return value == self._result(op)

    def verify_operation(self, op):
        seat, slot = self._slot(op)
        if not self._postcondition(op, seat.get(slot)):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"{slot} does not meet the op's postcondition")
        if op.kind == "harness.runtime-replace":
            # Stand-in for integration_for(h).prove_runtime_readiness (runtime_controller.py:8028).
            readiness = seat.get("readiness")
            ready = readiness.get(op.desired["selected"]) if isinstance(readiness, dict) else None
            if ready is not True:
                detail = (
                    f"{op.desired['selected']} not input-ready"
                    if ready is False
                    else f"{op.desired['selected']} readiness proof is missing or not true"
                )
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, detail)
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, f"{slot} verified")

    def can_rollback(self, mutation) -> PreflightResult:
        try:
            seat, slot = self._slot(mutation)
        except KeyError as exc:
            return PreflightResult(False, "drifted", str(exc))
        current = seat.get(slot)
        if _canon(current) == mutation.before["slot"]:
            return PreflightResult(True, "not-applied", f"{slot} still at its preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", f"{slot} holds this op's result")
        return PreflightResult(False, "drifted", f"{slot} moved since this op")

    def rollback_mutation(self, mutation):
        seat, slot = self._slot(mutation)
        seat[slot] = json.loads(mutation.before["slot"])

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


HANDLERS = [HarnessSeatSim()]


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5135 S1: evaluate the ONE invariant list over the seat's simulated observation.

    The observation (``context`` / ``facts`` / ``carve_outs`` / ``suppressed``,
    harness_manager_verify's shape) is read from the seat sim's
    ``verify_observation`` slot. A seat with none yields no context, so every
    invariant alarms (could not ask) and the rollup is ``not_configured``.
    """

    from tools.germinate.components import harness_manager_verify as V

    seat = (observed.get(HarnessSeatSim.name) or {}).get("seat") or {}
    observation = seat.get("verify_observation") or {}
    results = V.evaluate(list_doc, observation)
    return {"results": results, "state": V.rollup(results, observation)["state"]}
