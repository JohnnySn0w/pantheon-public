"""health-collector component: executor-side registration (ORG-080 §2 / §6; T-5102).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``health.collector`` class.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier, nothing live):
``HealthCollectorSim`` keeps the collector organ as JSON state inside the
simulated host. A live handler would act on Recovery's account (claude-fallback user
units), which no other seat may touch, and ORG-080's A-5 GO forbids live-host
actuation until S3 and C5 hold: the Recovery-run live handler is T-5981.

It WRAPS the running collector, unchanged:

* the subscription set is what ``health_collector.collect()`` actually reads,
  run over this checkout's roster (``health_export.fleet_personas()``) with a
  null reader (no bus, no sudo) and the set DERIVED from the registry's emitter
  declarations (tools/health_subscriptions.py, T-5980: contract ``emitter:``
  blocks plus the health-collector.subscriptions.yaml sidecar). The declared set
  is every component contract in this checkout that carries an ``emitter:``
  block; a declaration that yields no usable set is ``unsubscribable``;
* fleet-health.json is judged by ``tools/health_collector_check.py``, the ONE
  staleness rule the out-of-domain witness (T-5978) also runs;
* the unit facts are compared with the tools/systemd/ templates' sha256.

``VERIFY`` evaluates org.pantheon.health-collector/v1 (HC-1..HC-8, DRAFT, Recovery
signed DRAFT 2026-11-02) over the observation; ``observe()`` runs the same
evaluator, so install's "healthy" and the engine's verify cannot disagree.

Simulated state::

    out_dir:      {present: bool, mode: "2750", group: "pantheon-health"}
    units:        {unit: {sha256: str|None, enabled: bool, active: bool}}
    aggregate:    null | the fleet-health.json document (a real collector writes it)
    now:          int epoch (the simulated clock HC-6 reads)
    installed:    {target: true}                     # engine-installed units
    attestation:  null | {evidence key: str}
    subscription_attestation: null | {subscription_sha256, entry_count, declared}
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from tools import health_collector as HC
from tools import health_collector_check as CHECK
from tools import health_subscriptions as HS
from tools.health_export import fleet_personas
from tools.persona_installer_contracts import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "health-collector.unit-install": ("health.collector", "seat"),
    "health-collector.unit-retire": ("health.collector", "seat"),
    "health-collector.adopt-attest": ("health.collector", "seat"),
    "health-collector.subscription-attest": ("health.collector", "seat"),
    "health-collector.attest-retire": ("health.collector", "seat"),
}

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENTS = REPO_ROOT / "tools" / "germinate" / "components"
SYSTEMD = REPO_ROOT / "tools" / "systemd"
SYSTEMS_FILE = REPO_ROOT / "registry" / "systems.yaml"
LIST_FILE = COMPONENTS / "health-collector.invariants.yaml"
HANDLER = "health-collector"
ACCOUNT = "claude-fallback"          # the collector runs on Recovery's account (unit headers)
ATTEST_TARGET = "attestation:health-collector"
SUBSCRIPTION_TARGET = "attestation:health-collector-subscription"
OUT_DIR = {"path": str(HC.DEFAULT_OUT.parent), "mode": "2750", "group": "pantheon-health"}
#: what each unit looks like when converged: the oneshot is timer-driven (never enabled itself).
UNIT_SPEC = {
    "pantheon-health-collector.service": {"enabled": False, "active": False},
    "pantheon-health-collector.timer": {"enabled": True, "active": True},
    "pantheon-health-watcher.service": {"enabled": True, "active": True},
}
UNITS = tuple(UNIT_SPEC)
EVIDENCE_KEYS = ("units_sha256", "out_dir", "aggregate_status", "verify_rc")
INTERFACE_REGISTRY = {HC.PC_INTERFACE: "session", HC.HEALTH1_INTERFACE: "system"}
DEGRADED_ONLY = frozenset({"HC1-unit-templates", "HC8-reachability"})


def _default_state():
    return {
        "out_dir": {"present": False, "mode": None, "group": None},
        "units": {u: {"sha256": None, "enabled": False, "active": False} for u in UNITS},
        "aggregate": None,
        "now": 0,
        "installed": {},
        "attestation": None,
        "subscription_attestation": None,
    }


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _canon(value):
    return json.dumps(value, sort_keys=True)


def _unit(target):
    return target.split(":", 1)[1]


def template_sha256(unit):
    path = SYSTEMD / unit
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class _NullReader:
    """CollectorReader's two methods, reading nothing: collect() then reports the SET it reads."""

    def getall_system(self, bus_name, path, interface):
        return "unreachable", None

    def getall_session(self, account, persona, interface):
        return "unreachable", None


def registry_system_ids():
    """The system ids of registry/systems.yaml (the handler may read it; the runtime collector may not)."""

    doc = yaml.safe_load(SYSTEMS_FILE.read_text(encoding="utf-8")) or {}
    systems = doc.get("systems") if isinstance(doc, dict) else None
    if isinstance(systems, dict):
        return set(systems)
    return {s.get("id") for s in systems or [] if isinstance(s, dict)}


def collector_subscription(components_dir=None, registry_ids=None):
    """``(component_id, registry_component, bus_class)`` for every emitter health_collector reads.

    Raises ``HS.SubscriptionError`` when the registry's declarations yield no usable set."""

    subs = HS.derive(fleet_personas(), components_dir or COMPONENTS, registry_ids=registry_ids)
    doc = HC.collect(fleet_personas(), reader=_NullReader(), now=0, subscriptions=subs)
    return sorted(f"{e['component_id']}|{e['registry_component']}|{e['bus_class']}" for e in doc["entries"])


def declared_emitters():
    """``{component_id: bus}`` for every contract in this checkout that declares an ``emitter:`` block."""

    out = {}
    for cid, emitter in HS.declared_emitters(COMPONENTS).items():
        out[cid] = INTERFACE_REGISTRY.get(emitter.get("interface"), str(emitter.get("bus")))
    return out


# --- the ONE evaluator (VERIFY and observe) ---------------------------------------


def _list_entry(list_doc, iid):
    for entry in list_doc.get("invariants") or []:
        if isinstance(entry, dict) and entry.get("id") == iid:
            return entry
    return {}


def max_age_secs(list_doc):
    value = _list_entry(list_doc, "HC6-aggregate-fresh").get("max_age_secs")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else CHECK.DEFAULT_MAX_AGE_SECS


def expected_unreachable(list_doc):
    rows = _list_entry(list_doc, "HC8-reachability").get("expected_unreachable") or []
    return {r["component_id"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("component_id"), str)}


def _hc8(list_doc, obs):
    verdict = obs["aggregate"]["verdict"]
    if verdict["status"] not in ("fresh", "stale"):
        return f"could not ask: fleet-health.json is {verdict['status']}"
    expected = expected_unreachable(list_doc)
    reach = obs["aggregate"]["reachability"]
    unlisted = sorted(cid for cid, r in reach.items() if r != "ok" and cid not in expected)
    missing = sorted(cid for cid in expected if cid not in reach)
    # "exactly that set" (Recovery): a listed entry that is now reachable is a stale list entry (r1 F2)
    now_reachable = sorted(cid for cid in expected if reach.get(cid) == "ok")
    problems = [f"{cid} is reachable; remove it from expected_unreachable" for cid in now_reachable]
    if unlisted:
        problems.append(f"unreachable and not in expected_unreachable: {', '.join(f'{c} ({reach[c]})' for c in unlisted)}")
    if missing:
        problems.append(f"listed in expected_unreachable but missing from the subscription set: {', '.join(missing)}")
    return "; ".join(problems) or None


def _findings(list_doc, obs):
    """``{invariant id: failure detail or None}`` over the observation."""

    ctx, units, out_dir = obs["context"], obs["units"], obs["out_dir"]
    verdict = obs["aggregate"]["verdict"]
    drift = [u for u in ctx["units"] if not units[u]["sha256"] or units[u]["sha256"] != ctx["templates"][u]]
    timer, watcher = units["pantheon-health-collector.timer"], units["pantheon-health-watcher.service"]
    want = ctx["out_dir"]
    return {
        "HC1-unit-templates": f"not byte-equal to the tools/systemd template: {', '.join(drift)}" if drift else None,
        "HC2-timer-enabled": None if timer["enabled"] else "pantheon-health-collector.timer is not enabled",
        "HC3-watcher-active": None if watcher["enabled"] and watcher["active"] else "pantheon-health-watcher.service is not enabled and active",
        "HC4-out-dir": None if (out_dir.get("present") and out_dir.get("mode") == want["mode"] and out_dir.get("group") == want["group"])
        else f"{want['path']} is not present {want['mode']} {want['group']} (observed {out_dir})",
        "HC5-aggregate-shape": None if verdict["status"] in ("fresh", "stale") else f"fleet-health.json {verdict['status']}: {verdict['detail']}",
        "HC6-aggregate-fresh": None if verdict["status"] == "fresh" else f"fleet-health.json {verdict['status']}: {verdict['detail']}",
        "HC7-collector-mode": None if verdict.get("mode") in CHECK.MODES else f"collector.mode unreadable (fleet-health.json {verdict['status']})",
        "HC8-reachability": _hc8(list_doc, obs),
    }


def evaluate(list_doc, obs):
    findings = _findings(list_doc, obs)
    results, severities = [], set()
    for entry in list_doc.get("invariants") or []:
        iid = entry.get("id") if isinstance(entry, dict) else None
        if iid not in findings:
            results.append({"id": str(iid), "status": "alarm", "detail": "no evaluator for this invariant"})
            severities.add("unhealthy")
        elif findings[iid] is not None:
            results.append({"id": iid, "status": "fail", "detail": findings[iid]})
            severities.add("degraded" if iid in DEGRADED_ONLY else "unhealthy")
        else:
            results.append({"id": iid, "status": "pass", "detail": "holds"})
    state = "unhealthy" if "unhealthy" in severities else "degraded" if severities else "ready"
    return {"results": results, "state": state}


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5135 S1: evaluate org.pantheon.health-collector/v1 (HC-1..HC-8) over the observation."""

    obs = observed.get(HANDLER)
    if not isinstance(obs, dict) or "aggregate" not in obs:
        raise ValueError("no health-collector observation: could not ask")
    return evaluate(list_doc, obs)


# --- the simulated organ ----------------------------------------------------------


class HealthCollectorSim:
    name = HANDLER
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()
        self._ctx = None

    def context(self):
        if self._ctx is None:
            try:
                declared = declared_emitters()
                subscription = collector_subscription(registry_ids=registry_system_ids())
                unsubscribable = []
            except (HS.SubscriptionError, OSError, yaml.YAMLError) as exc:
                declared, subscription, unsubscribable = {}, [], [str(exc)]
            self._ctx = {
                "account": ACCOUNT,
                "units": list(UNITS),
                "unit_spec": copy.deepcopy(UNIT_SPEC),
                "templates": {u: template_sha256(u) for u in UNITS},
                "out_dir": dict(OUT_DIR),
                "subscription": {
                    "entries": subscription,
                    "sha256": _sha(subscription),
                    "declared": sorted(declared),
                    "unsubscribable": unsubscribable,
                },
            }
        return self._ctx

    @staticmethod
    def list_doc():
        return yaml.safe_load(LIST_FILE.read_text(encoding="utf-8"))

    def _facts(self, state):
        ctx, list_doc = self.context(), self.list_doc()
        doc = state["aggregate"]
        if doc is None:
            verdict = {"status": "missing", "detail": "no aggregate", "mode": None}
            reach = {}
        else:
            verdict = CHECK.check_doc(doc, max_age_secs(list_doc), state["now"], CHECK.DEFAULT_PATH)
            entries = doc.get("entries") if isinstance(doc, dict) and isinstance(doc.get("entries"), list) else []
            reach = {e.get("component_id"): e.get("reachability") for e in entries if isinstance(e, dict)}
        obs = {
            "context": {k: copy.deepcopy(v) for k, v in ctx.items() if k != "subscription"},
            "out_dir": dict(state["out_dir"]),
            "units": copy.deepcopy(state["units"]),
            "aggregate": {"verdict": {k: verdict.get(k) for k in ("status", "detail", "mode", "age_secs")}, "reachability": reach},
        }
        result = evaluate(list_doc, obs)
        failed = [f"{r['id']}: {r['detail']}" for r in result["results"] if r["status"] != "pass"]
        evidence = {
            "units_sha256": _sha(state["units"]),
            "out_dir": f"{state['out_dir'].get('mode')}:{state['out_dir'].get('group')}" if state["out_dir"].get("present") else "absent",
            "aggregate_status": verdict["status"],
            "verify_rc": "0" if not failed else "1",
        }
        failed_ids = sorted(r["id"] for r in result["results"] if r["status"] != "pass")
        return obs, {"ok": not failed, "failed": failed, "failed_ids": failed_ids, "state": result["state"]}, evidence

    def evidence(self, state=None):
        return self._facts(self.state if state is None else state)[2]

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        obs, health, evidence = self._facts(self.state)
        obs.update(
            health=health,
            evidence=evidence,
            subscription=copy.deepcopy(self.context()["subscription"]),
            attestation=copy.deepcopy(self.state["attestation"]),
            subscription_attestation=copy.deepcopy(self.state["subscription_attestation"]),
            installed=sorted(self.state["installed"]),
        )
        return obs

    def _slot(self, op):
        if op.kind in ("health-collector.unit-install", "health-collector.unit-retire"):
            return self.state["units"].get(_unit(op.target))
        if op.target == SUBSCRIPTION_TARGET:
            return self.state["subscription_attestation"]
        return self.state["attestation"]

    def _snapshot(self, op):
        return {"slot": _canon(self._slot(op)), "installed": "true" if op.target in self.state["installed"] else "false"}

    @staticmethod
    def _result(op):
        if op.kind == "health-collector.unit-install":
            return {"sha256": op.desired["sha256"], "enabled": op.desired["enabled"] == "true", "active": op.desired["restart"] == "true"}
        if op.kind == "health-collector.unit-retire":
            return {"sha256": None, "enabled": False, "active": False}
        if op.kind == "health-collector.adopt-attest":
            return {k: op.desired[k] for k in EVIDENCE_KEYS}
        if op.kind == "health-collector.subscription-attest":
            return dict(op.desired)
        return None

    def _subscription_now(self):
        sub = self.context()["subscription"]
        return {"subscription_sha256": sub["sha256"], "entry_count": str(len(sub["entries"])), "declared": ",".join(sub["declared"]) or "none"}

    def preflight(self, op):
        slot = self._slot(op) or {}
        if op.kind == "health-collector.unit-install":
            ok = (slot.get("sha256") or "absent") == op.precondition["sha256"]
            return PreflightResult(ok, self.name, "unit at its planned preimage" if ok else "the unit moved since plan")
        if op.kind == "health-collector.unit-retire":
            ok = bool(slot.get("sha256"))
            return PreflightResult(ok, self.name, "unit installed" if ok else "nothing to retire")
        if op.kind == "health-collector.adopt-attest":
            current = self.evidence()
            drifted = sorted(k for k in EVIDENCE_KEYS if current.get(k) != op.desired.get(k))
            ok = not drifted and current["verify_rc"] == "0"
            return PreflightResult(ok, self.name, "collector healthy and matches the planned evidence" if ok else f"evidence moved since plan: {drifted or ['verify_rc']}")
        if op.kind == "health-collector.subscription-attest":
            ok = self._subscription_now() == dict(op.desired) and not self.context()["subscription"]["unsubscribable"]
            return PreflightResult(ok, self.name, "subscription set matches the plan" if ok else "subscription set moved since plan")
        ok = self._slot(op) is not None
        return PreflightResult(ok, self.name, "attestation present" if ok else "no attestation to retire")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, self._snapshot(op), _canon(self._result(op)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        result = self._result(op)
        if op.kind in ("health-collector.unit-install", "health-collector.unit-retire"):
            self.state["units"][_unit(op.target)] = result
            if op.kind == "health-collector.unit-install":
                self.state["installed"][op.target] = True
            else:
                self.state["installed"].pop(op.target, None)
        elif op.target == SUBSCRIPTION_TARGET:
            self.state["subscription_attestation"] = result
        else:
            self.state["attestation"] = result
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        slot = self._slot(op)
        if op.kind == "health-collector.unit-install":
            ok = slot == self._result(op)
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "unit at its template bytes" if ok else f"unit moved: {slot}")
        if op.kind == "health-collector.unit-retire":
            ok = not (slot or {}).get("sha256")
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "unit retired" if ok else "unit still installed")
        if op.kind == "health-collector.attest-retire":
            ok = slot is None
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "attestation retired" if ok else "attestation still present")
        if op.kind == "health-collector.subscription-attest":
            ok = slot == self._result(op) and self._subscription_now() == slot
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "subscription set as attested" if ok else "subscription set drifted from the attestation")
        if slot != self._result(op):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "the recorded attestation is not this op's evidence")
        current = self.evidence()
        drifted = sorted(k for k in EVIDENCE_KEYS if current.get(k) != slot.get(k))
        if drifted:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift from the attested evidence: {', '.join(drifted)}")
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "attested health collector, verify rc 0")

    def can_rollback(self, mutation):
        current = _canon(self._slot_for(mutation))
        if current == mutation.before["slot"]:
            return PreflightResult(True, "not-applied", "slot still at its preimage")
        owned = current == mutation.after_fingerprint
        return PreflightResult(owned, "owned" if owned else "drifted", "this op's result" if owned else "slot moved since the op")

    def _slot_for(self, mutation):
        return self._slot(SimpleNamespace(kind=mutation.kind, target=mutation.target))

    def rollback_mutation(self, mutation):
        before = json.loads(mutation.before["slot"])
        if mutation.kind in ("health-collector.unit-install", "health-collector.unit-retire"):
            self.state["units"][_unit(mutation.target)] = before
            if mutation.before["installed"] == "true":
                self.state["installed"][mutation.target] = True
            else:
                self.state["installed"].pop(mutation.target, None)
        elif mutation.target == SUBSCRIPTION_TARGET:
            self.state["subscription_attestation"] = before
        else:
            self.state["attestation"] = before

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


HANDLERS = [HealthCollectorSim()]
