"""host-substrate component: executor-side registration (ORG-080 §2; T-5084, tranche GE).

``OP_KINDS`` is empty: the root vocabulary is the engine's own ORG-082 substrate
families (A-8.5: a component may not declare root kinds). ``HANDLERS`` serves
exactly those families through ``RootHelper`` over the SIMULATED port, pinned to
the ``bootstrap-in-process-root`` context. Nothing here actuates a live host;
the host port is dark (``host_substrate.ports.LIVE_ACTUATION_ENABLED``).

Engine seam gaps (T-5135): S6 -- the engine passes no invocation context, so
the context is pinned here and ``staged-cutover`` maps to the stage phase;
S7 -- the engine never reads ``privilege`` at actuation.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate.helper import ACTIVATE, BOOTSTRAP, STAGE, HelperRefusal, RootHelper
from tools.germinate.host_substrate.ports import SimulatedSubstratePort
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {}

MANIFEST = Path(__file__).with_name("host-substrate.manifest.yaml")
REPO_ROOT = Path(__file__).resolve().parents[3]


def _manifest():
    return F.load_manifest(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")), repo_root=REPO_ROOT)


class HostSubstrateSim:
    name = "host-substrate"
    kinds = frozenset(F.FAMILIES)

    def __init__(self):
        self.port = SimulatedSubstratePort()

    def _helper(self):
        return RootHelper(self.port, _manifest(), BOOTSTRAP)

    def _step(self, op):
        phase = STAGE if getattr(op, "execution_scope", "all") == "staged-cutover" else ACTIVATE
        helper = self._helper()
        step = helper.admit(op.kind, dict(op.desired), phase)
        if step.subject != op.target:
            raise HelperRefusal(f"{op.operation_id}: target {op.target!r} != resolved subject {step.subject!r}")
        if op.provenance.get("manifest_sha256") != helper.manifest.sha256:
            raise HelperRefusal(f"{op.operation_id}: the manifest changed since plan")
        return helper, step

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        return {"subjects": self.port.snapshot()}

    def preflight(self, op):
        try:
            helper, step = self._step(op)
        except (HelperRefusal, F.ManifestError) as exc:
            return PreflightResult(False, "refused", str(exc))
        if F.sha256_text(F.canonical(helper.before(step))) != op.precondition.get("observed_sha256"):
            return PreflightResult(False, "drifted", f"{step.subject} changed since plan")
        evidence = helper.preflight(step)
        return PreflightResult(evidence.ok, self.name if evidence.ok else "refused", evidence.detail)

    def rollback_intent(self, op):
        helper, step = self._step(op)
        before = helper.before(step)
        return MutationRecord(op.operation_id, op.kind, step.subject, op.owner, True,
                              {"snapshot": F.canonical(before)}, F.canonical(step.resolved.after))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        helper, step = self._step(op)
        helper.apply(step)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind} ({step.phase})")

    def verify_operation(self, op):
        try:
            helper, step = self._step(op)
        except (HelperRefusal, F.ManifestError) as exc:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, str(exc))
        ev = helper.verify(step)
        return OperationEvidence(op.operation_id, op.layer, op.kind, ev.ok, ev.detail, dict(ev.data))

    def can_rollback(self, mutation):
        current = F.canonical(self.port.get(mutation.target))
        if current == mutation.before["snapshot"]:
            return PreflightResult(True, "not-applied", "subject still at its preimage")
        if current == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "subject at this op's value")
        return PreflightResult(False, "drifted", f"{mutation.target} moved past this op")

    def rollback_mutation(self, mutation):
        self.port.put(mutation.target, json.loads(mutation.before["snapshot"]))

    def dump_state(self):
        return self.port.dump()

    def load_state(self, state):
        self.port.load(state)


HANDLERS = [HostSubstrateSim()]


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5135 S1: evaluate the HS invariant list over the simulated port's subjects."""

    from tools.germinate.host_substrate import invariants as I

    subjects = (observed.get(HostSubstrateSim.name) or {}).get("subjects")
    if not isinstance(subjects, dict):
        raise ValueError("no subjects observed: could not ask")
    I.check_list(list_doc)
    results = [
        {"id": r["id"], "status": "pass" if r["ok"] else "fail", "detail": "ok" if r["ok"] else f"drift: {r['drift']}"}
        for r in I.evaluate(_manifest(), subjects)
    ]
    return {"results": results, "state": "ready" if all(r["status"] == "pass" for r in results) else "unhealthy"}
