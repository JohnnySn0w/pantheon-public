"""harness-manager component planner (ORG-080 §2, §5; T-5090, germination tranche GF1).

``plan_configure`` is configure sub-op (b), the harness-runtime swap
(``germinate configure --system harness-manager --seat <account> --harness codex``).
It is an ADAPTER over the existing swap (``persona_bringup.operator_target_swap``
-> ``runtime_controller._operator_target_swap_resolved``), which it does not change
and does not import (ruling: the planner never imports runtime_controller). It
uses the leaf modules' pure functions only:

* ``harness_adapter.integration``: ``normalize_harness``,
  ``integration_for(h).receiver_direction`` and ``receiver_handoff_actions``
  (the exact action tuple the bridge handoff would run, recorded as data);
* ``harness_state.validate_runtime`` for runtime references.

Op order follows the existing code (T-5090 DECISION 6), not the ORG-057 §3 text:
stage the transition record -> receiver handoff "pre" (the authority-transfer
gate) -> replace the runtime (postcondition: readiness) -> receiver handoff
"post" -> mark the transition ``awaiting-successor-read``. The successor-read
prompt is send-keys into a model pane, which is off the engine path (D-N).

RE-STATEMENTS over observation data (the A-5 edge; each mirrors one check of
the existing code, which is fused with I/O there):
  R1 compensation-failed refusal        persona_bringup.py:612
  R2 already-active is a validated no-op persona_bringup.py:622-655 /
                                        runtime_controller.py:7733-7762
  R3 held destination refuses           persona_bringup.py:473-500 (no --override-hold: desired is closed)
  R4 destination in primary+fallbacks   persona_bringup.py:744-748
  R5 in-flight transition refuses       persona_bringup.py:657 (phase preparing/awaiting-successor-read)
  R6 receiver handoff only for telegram runtime_controller.py:7311 receiver_handoff_required
  R7 receiver must sit on the source     runtime_controller.py:7954-7961 (preflight _prove_receiver)

Returns typed operation data only.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from tools import harness_state
from tools.harness_adapter import integration

HANDLER = "harness-manager-seat"
GATE = "authority-transfer"
IN_FLIGHT = frozenset({"preparing", "awaiting-successor-read"})


class PlanRefused(RuntimeError):
    """The swap is refused at plan (a precondition of the existing code fails)."""


def _seat(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or not isinstance(observed.get("seat"), dict):
        raise PlanRefused(f"request carries no {HANDLER} seat observation for scope {request.get('scope')!r}")
    return observed["account"], observed["seat"]


def _op(op_id, kind, target, account, precondition, desired, *, crash_contract, provenance, gate=None):
    op = {
        "operation_id": op_id[:64],
        "layer": 2,
        "kind": kind,
        "target": target,
        "precondition": precondition,
        "desired": desired,
        "inverse": dict(precondition),
        "privilege": "seat",
        "owner": account,
        "provenance": provenance,
        "crash_contract": crash_contract,
        "side_effect_free": False,
    }
    if gate is not None:
        op["gate"] = gate
    return op


def _validate_active_noop(account, seat, destination, target_harness):
    runtime = seat.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("selected") != destination:
        raise PlanRefused(f"{account}: recorded active runtime does not match requested destination")
    communications = seat.get("communications")
    if isinstance(communications, dict) and communications.get("kind") == "telegram":
        receiver = seat.get("receiver")
        holder = receiver.get("holder") if isinstance(receiver, dict) else None
        if holder != target_harness:
            raise PlanRefused(
                f"{account}: active runtime receiver is held by {holder!r}, not target harness {target_harness!r}"
            )
    readiness = seat.get("readiness")
    if not isinstance(readiness, dict) or readiness.get(destination) is not True:
        raise PlanRefused(f"{account}: active runtime liveness was not positively proven")


def plan_configure(request):
    if request.get("scope") == "system":
        raise PlanRefused("configure (b) requires a seat scope")
    harness = (request.get("desired") or {}).get("harness")
    if not harness:
        raise PlanRefused("configure (b) needs --harness")
    account, seat = _seat(request)
    role = seat.get("harness_runtime_role") or {}
    primary, fallbacks, profiles = role.get("primary"), list(role.get("fallbacks") or []), role.get("profiles") or {}
    current = (seat.get("runtime") or {}).get("selected")
    transition = seat.get("transition") or {}
    phase = transition.get("phase", "none")
    target_harness = integration.normalize_harness(harness)
    if target_harness not in profiles:
        raise PlanRefused(f"{account}: the role declares no {target_harness} profile")
    destination = harness_state.validate_runtime(f"{target_harness}/{profiles[target_harness]}")
    harness_state.validate_runtime(current or "")
    if phase == "compensation-failed":  # R1
        raise PlanRefused(f"{account}: runtime transition compensation failed; manual recovery required")
    if destination == current:  # R2
        _validate_active_noop(account, seat, destination, target_harness)
        return []
    if destination not in [primary, *fallbacks]:  # R4
        raise PlanRefused(f"{account}: destination {destination} is not the primary or a configured fallback")
    if destination in (seat.get("holds") or {}):  # R3
        raise PlanRefused(f"{account}: {destination} is operator-held; refusing the swap")
    if phase in IN_FLIGHT:  # R5
        raise PlanRefused(f"{account}: a runtime transition is already {phase}")

    source_harness = current.split("/", 1)[0]
    direction = integration.integration_for(target_harness).receiver_direction(
        SimpleNamespace(harness=source_harness), SimpleNamespace(harness=target_harness)
    ) or "none"
    communications = seat.get("communications")
    telegram = isinstance(communications, dict) and communications.get("kind") == "telegram"  # R6
    binding = SimpleNamespace(communications=communications)
    seed = json.dumps({"seat": seat, "destination": destination, "account": account}, sort_keys=True)
    transition_id = "t-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    provenance = {
        "adapter_of": "tools.persona_bringup.operator_target_swap -> tools.runtime_controller._operator_target_swap_resolved",
        "pure_functions": "tools.harness_adapter.integration.receiver_direction/receiver_handoff_actions; tools.harness_state.validate_runtime",
        "transition_id": transition_id,
        "source_runtime": current,
        "destination": destination,
    }
    record = {"transition_id": transition_id, "outgoing": current, "incoming": destination}
    ops = [
        _op(
            f"stage-{account}", "harness.transition-stage", f"{account}:transition", account,
            {"phase": phase}, dict(record, phase="preparing"),
            crash_contract="stageable", provenance=provenance,
        )
    ]
    holder = (seat.get("receiver") or {}).get("holder") or "none"
    if telegram and direction != "none" and holder != source_harness:  # R7
        raise PlanRefused(f"{account}: the Telegram receiver is held by {holder!r}, not the source runtime {source_harness!r}")
    if telegram and direction != "none":
        pre = ",".join(integration.receiver_handoff_actions(binding, direction, "pre"))
        ops.append(
            _op(
                f"receiver-pre-{account}", "harness.receiver-handoff", f"{account}:receiver", account,
                {"holder": holder}, {"holder": "none", "direction": direction, "phase": "pre", "actions": pre},
                crash_contract="write-ahead-inverse-durable", provenance=provenance, gate=GATE,
            )
        )
    ops.append(
        _op(
            f"runtime-{account}", "harness.runtime-replace", f"{account}:runtime", account,
            {"selected": current}, {"selected": destination},
            crash_contract="write-ahead-inverse-durable", provenance=provenance,
            gate=None if telegram and direction != "none" else GATE,
        )
    )
    if telegram and direction != "none":
        post = ",".join(integration.receiver_handoff_actions(binding, direction, "post"))
        ops.append(
            _op(
                f"receiver-post-{account}", "harness.receiver-handoff", f"{account}:receiver", account,
                {"holder": "none"}, {"holder": target_harness, "direction": direction, "phase": "post", "actions": post},
                crash_contract="write-ahead-inverse-durable", provenance=provenance,
            )
        )
    ops.append(
        _op(
            f"commit-{account}", "harness.transition-commit", f"{account}:transition", account,
            dict(record, phase="preparing"), dict(record, phase="awaiting-successor-read"),
            crash_contract="stageable", provenance=provenance,
        )
    )
    return ops
