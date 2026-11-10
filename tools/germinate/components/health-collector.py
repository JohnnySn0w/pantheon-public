"""health-collector component planner (ORG-080 §2 / §6; ORG-076 §5.5; T-5102).

An ADAPTER over the running fleet health collector, which it does not change:
tools/health_collector.py (the poll leg, T-5395) and tools/health_watcher.py (the
PropertiesChanged subscribing leg, T-5409), run on Recovery's account from three
tools/systemd/ units. The planner reads only the executor-side observation
(``request["observed"]["health-collector"]``): unit facts against their repo
templates, the root-provisioned out dir, the invariant-list verdict over
fleet-health.json, and the collector's actual subscription set against the
registry's emitter declarations. It runs no tool, file or bus call.

* install: observe-and-attest (the T-5947 shape). A healthy collector the engine
  did not install gets ONE ``health-collector.adopt-attest`` op whose ``desired``
  is the observed evidence; a matching rerun plans nothing; healthy drift
  re-attests; an unhealthy collector is never attested. Greenfield (no unit
  present): one ``health-collector.unit-install`` op per absent unit.
  The out dir /run/pantheon-health is root tmpfiles (source tools/systemd/system/pantheon-health.tmpfiles.conf, T-5982; engine install T-6011): absent or wrong
  mode/group refuses, never installed here.
* configure: attest the subscription set. The set is DATA (T-5980): derived from the
  registry's ``emitter:`` declarations plus the roster (tools/health_subscriptions.py,
  with fleet_personas() over instance.yaml), so a newly declared emitter is attested,
  not refused. Only a declaration set that yields no usable subscription set (an
  unsubscribable emitter) refuses.
* update: converge every installed unit to its full spec (bytes, enabled,
  active; the watcher restarts), then re-attest only when the evidence predicted
  from those very ops is healthy (otherwise units only; install re-attests
  later). Code currency rides
  pantheon-config's own update leg.
* remove: retire the three units and the engine's attestations behind the
  operator gate ``health-collector-retire``. Never the out dir: kino and the
  recovery watcher read it.
* repair is not declared (the engine refuses leg-not-declared).
"""

from __future__ import annotations

import hashlib
import json

HANDLER = "health-collector"
ATTEST_TARGET = "attestation:health-collector"
SUBSCRIPTION_TARGET = "attestation:health-collector-subscription"
GATE = "health-collector-retire"
SUPERVISED = "Recovery's supervised hand install (the tools/systemd/pantheon-health-collector / pantheon-health-watcher unit headers)"
ROOT_STEP = "root provisions it by tmpfiles (tools/systemd/system/pantheon-health.tmpfiles.conf -> /etc/tmpfiles.d/pantheon-health.conf: d /run/pantheon-health 2750 claude-fallback pantheon-health; engine install is T-6011)"
EVIDENCE_KEYS = ("units_sha256", "out_dir", "aggregate_status", "verify_rc")
#: the invariants a unit op can change; every other failing invariant blocks an update's attest
UNIT_INVARIANTS = frozenset({"HC1-unit-templates", "HC2-timer-enabled", "HC3-watcher-active"})
RETIRE_ORDER = ("pantheon-health-watcher.service", "pantheon-health-collector.timer", "pantheon-health-collector.service")


class PlanRefused(RuntimeError):
    """The observed health collector cannot be planned for."""


def _observed(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or "health" not in observed:
        raise PlanRefused(f"request carries no {HANDLER} observation")
    return observed


def _require_out_dir(obs):
    want, got = obs["context"]["out_dir"], obs["out_dir"]
    if not got.get("present") or got.get("mode") != want["mode"] or got.get("group") != want["group"]:
        raise PlanRefused(
            f"out dir {want['path']} is absent or not {want['mode']} {want['group']} (observed {got}); "
            f"it is never installed through this component: {ROOT_STEP}"
        )


def _op(op_id, kind, target, owner, precondition, desired, inverse, provenance, gate=None):
    op = {
        "operation_id": op_id[:64],
        "layer": 1,
        "kind": kind,
        "target": target,
        "precondition": precondition,
        "desired": desired,
        "inverse": inverse,
        "privilege": "seat",
        "owner": owner,
        "provenance": dict(provenance, planner="tools.health_collector + tools.health_watcher (wrapped, unchanged)"),
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }
    if gate is not None:
        op["gate"] = gate
    return op


def _flag(value):
    return "true" if value else "false"


def _unit_state(obs, unit):
    cur = obs["units"].get(unit) or {}
    return {"sha256": cur.get("sha256") or "absent", "enabled": _flag(cur.get("enabled"))}


def _unit_install(obs, unit, basis):
    """One unit-install op; refuses first when the unit's repo template is missing (no target hash),
    the one gate every template-reading leg (install greenfield, update) goes through."""

    template = obs["context"]["templates"].get(unit)
    if not template:
        raise PlanRefused(f"template missing: {unit} (tools/systemd/{unit})")
    spec = obs["context"]["unit_spec"][unit]
    before = _unit_state(obs, unit)
    desired = {
        "unit": unit,
        "sha256": template,
        "enabled": _flag(spec["enabled"]),
        "restart": _flag(spec["active"]),
    }
    return _op(f"unit-install-{unit.replace('.', '-')}", "health-collector.unit-install", f"unit:{unit}", obs["context"]["account"],
               before, desired, before, {"basis": basis})


def _attest(obs, evidence):
    prior = obs["attestation"]
    pre = {"attested_units_sha256": (prior or {}).get("units_sha256", "none")}
    return _op("adopt-attest-health-collector", "health-collector.adopt-attest", ATTEST_TARGET, obs["context"]["account"],
               pre, {k: evidence[k] for k in EVIDENCE_KEYS}, pre,
               {"basis": "observed-legacy", "re_attest": _flag(prior is not None)})


def _failed(obs):
    return "; ".join(obs["health"]["failed"]) or "no failure detail"


def _engine_owned(obs):
    installed = set(obs["installed"])
    return all(f"unit:{u}" in installed for u in obs["context"]["units"])


def plan_install(request):
    obs = _observed(request)
    _require_out_dir(obs)
    healthy = obs["health"]["ok"]
    attested = obs["attestation"]
    if attested is not None:
        if not healthy:
            raise PlanRefused(
                f"the attested health collector is UNHEALTHY now ({_failed(obs)}); unhealthy evidence is never "
                f"re-attested: converge it first (update, or {SUPERVISED}), then re-plan"
            )
        if all(attested.get(k) == obs["evidence"][k] for k in EVIDENCE_KEYS):
            return []
        return [_attest(obs, obs["evidence"])]
    if healthy:
        return [] if _engine_owned(obs) else [_attest(obs, obs["evidence"])]
    units, installed = obs["context"]["units"], set(obs["installed"])
    present = [u for u in units if (obs["units"].get(u) or {}).get("sha256")]
    legacy = [u for u in present if f"unit:{u}" not in installed]
    if legacy:
        raise PlanRefused(f"no healthy legacy health collector ({_failed(obs)}); units {legacy} are hand-installed: {SUPERVISED}")
    absent = [u for u in units if u not in present]
    if not absent:
        raise PlanRefused(
            f"the engine-installed health collector is unhealthy ({_failed(obs)}); diagnose it "
            "(update converges unit drift; a stale aggregate is a runtime fault, not an install gap)"
        )
    return [_unit_install(obs, u, "greenfield") for u in absent]


def plan_configure(request):
    """Attest the derived subscription set; refuse only when the declarations are unsubscribable."""

    obs = _observed(request)
    sub = obs["subscription"]
    if sub["unsubscribable"]:
        raise PlanRefused(
            "the registry's emitter declarations do not yield a subscription set: "
            + "; ".join(sub["unsubscribable"])
        )
    desired = {"subscription_sha256": sub["sha256"], "entry_count": str(len(sub["entries"])),
               "declared": ",".join(sub["declared"]) or "none"}
    prior = obs["subscription_attestation"]
    if prior == desired:
        return []
    pre = {"attested_subscription_sha256": (prior or {}).get("subscription_sha256", "none")}
    return [_op("subscription-attest", "health-collector.subscription-attest", SUBSCRIPTION_TARGET,
                obs["context"]["account"], pre, desired, pre, {"basis": "health_collector.collect subscription set"})]


def _spec_unit(obs, unit):
    """The unit as its spec says it should be: template bytes, spec enabled / active."""

    spec = obs["context"]["unit_spec"][unit]
    return {"sha256": obs["context"]["templates"][unit], "enabled": spec["enabled"], "active": spec["active"]}


def apply_unit_ops(units, ops):
    """The ONE prediction of a plan's unit after-state: a copy of ``units`` with every planned
    ``health-collector.unit-install`` op applied exactly as the handler executes it
    (``HealthCollectorSim._result``: sha256 <- desired, enabled <- desired enabled, active <- desired
    restart). Non-unit ops leave units untouched."""

    after = {unit: dict(facts) for unit, facts in units.items()}
    for op in ops:
        if op["kind"] == "health-collector.unit-install":
            desired = op["desired"]
            after[desired["unit"]] = {"sha256": desired["sha256"], "enabled": desired["enabled"] == "true",
                                      "active": desired["restart"] == "true"}
    return after


def _units_sha256(units):
    """The handler's units_sha256 encoding (health-collector_handlers._sha over state["units"])."""

    return hashlib.sha256(json.dumps(units, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def predicted_evidence(obs, ops):
    """The attest evidence after ``ops`` run, or None when that after-state is not healthy.

    Healthy after the ops means: every unit present AND at its spec in ``apply_unit_ops``'s
    after-state (which implies HC-1..HC-3 pass), AND no invariant outside HC-1..HC-3 fails now
    (HC-4..HC-8 read the out dir and fleet-health.json, which unit ops never change). Anything
    else returns None, so update never plans an attest over unhealthy evidence (r1 F1, r2 F1)."""

    after = apply_unit_ops(obs["units"], ops)
    units = obs["context"]["units"]
    if any(after.get(u) != _spec_unit(obs, u) for u in units):
        return None
    if set(obs["health"]["failed_ids"]) - UNIT_INVARIANTS:
        return None
    return dict(obs["evidence"], units_sha256=_units_sha256(after), verify_rc="0")


def plan_update(request):
    """Converge every installed unit to its full spec (bytes, enabled, active), then re-attest
    only when the converged evidence is healthy.

    A unit is converged when ANY of its bytes, enabled or active differs from spec: a stopped
    watcher at current bytes is exactly why someone runs update. Absent units are install's job.
    The attest decision and its evidence come from ``predicted_evidence`` over these very ops, never
    from an independent "everything at spec" model, so the planned attest matches what apply will
    observe (r2 F1). When the after-state is unhealthy, update plans the unit ops ONLY; install
    re-attests once the collector is healthy again."""

    obs = _observed(request)
    _require_out_dir(obs)
    if obs["attestation"] is None and not _engine_owned(obs):
        raise PlanRefused("no engine-installed or attested health collector to update: run install first")
    ops = [
        _unit_install(obs, u, "update")
        for u in obs["context"]["units"]
        if (obs["units"].get(u) or {}).get("sha256") and obs["units"][u] != _spec_unit(obs, u)
    ]
    evidence = predicted_evidence(obs, ops) if ops and obs["attestation"] is not None else None
    if evidence is not None:
        ops.append(_attest(obs, evidence))
    return ops


def plan_remove(request):
    """Retire the units and the engine's attestations (gated); never the out dir."""

    obs = _observed(request)
    owner = obs["context"]["account"]
    ops = []
    for unit in RETIRE_ORDER:
        cur = obs["units"].get(unit) or {}
        if cur.get("sha256"):
            before = _unit_state(obs, unit)
            ops.append(_op(f"unit-retire-{unit.replace('.', '-')}", "health-collector.unit-retire", f"unit:{unit}", owner, before,
                           {"unit": unit, "sha256": "absent", "enabled": "false"}, before,
                           {"basis": "decommission"}, gate=GATE))
    for target, slot, key in ((ATTEST_TARGET, obs["attestation"], "units_sha256"),
                              (SUBSCRIPTION_TARGET, obs["subscription_attestation"], "subscription_sha256")):
        if slot is not None:
            pre = {"attested": slot.get(key, "none")}
            ops.append(_op(f"attest-retire-{target.split(':', 1)[1]}", "health-collector.attest-retire", target, owner,
                           pre, {"attested": "false"}, pre, {"basis": "engine attestation record only"}, gate=GATE))
    return ops
