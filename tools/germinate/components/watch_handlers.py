"""watch component: executor-side registration (ORG-080 §2; T-5964).

``OP_KINDS`` is literal data (read with ast.literal_eval): two seat-privilege kinds
in the component's own non-reserved ``watch.adoption`` class.

Every leg is OBSERVE-AND-ATTEST (see ``watch.py``). The handler reports the
repo-side evidence of the legacy watch install and an observed decommission
report; its only mutations are attestation entries in its OWN state
(``watch.adopt-attest`` writes one, ``watch.attest-retire`` removes them all; each
inverse restores the preimage). Nothing on the host changes.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier): the evidence lives
as JSON state inside the simulated host, seeded by tests from committed captures
of real repo state through ``watch_evidence`` (T-5964 D3). There is no live reader
(T-5972): with nothing seeded the observation carries no evidence and every leg
refuses, which is the honest answer from a seat that cannot read claude-research.

``VERIFY`` evaluates the ``org.pantheon.watch/v1`` invariant list (W1-W6) over the
same observation (T-5135 S1).

Simulated state::

    evidence:     <watch_evidence.collect output> | None
    decommission: <watch_evidence.decommission_report output> | None
    attestations: {target: {<the op's desired>}}
"""

from __future__ import annotations

import copy
import json

from tools.germinate.components import watch as P
from tools.germinate.components import watch_evidence as E
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "watch.adopt-attest": ("watch.adoption", "seat"),
    "watch.attest-retire": ("watch.adoption", "seat"),
}

TIER = "simulated"


def _default_state():
    return {"evidence": None, "decommission": None, "attestations": {}}


def _blob(value):
    return P.ABSENT if value is None else json.dumps(value, sort_keys=True)


class WatchSim:
    name = P.HANDLER
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()

    def seed(self, evidence=None, decommission=None):
        """Load observed evidence; a legacy ``DecommissionReport`` is converted as-is."""

        if decommission is not None and not isinstance(decommission, dict):
            decommission = E.decommission_report(decommission)
        self.state["evidence"] = copy.deepcopy(evidence)
        self.state["decommission"] = copy.deepcopy(decommission)

    # --- current evidence per target ------------------------------------------

    def _install_now(self):
        evidence = self.state["evidence"]
        if P.install_problems(evidence):
            return None
        return P.install_desired(evidence)

    def _current(self, op):
        """The desired this op would carry now, or None if the evidence is not healthy."""

        install = self._install_now()
        if install is None:
            return None
        if op.target == P.INSTALL_TARGET:
            return install
        evidence = self.state["evidence"]
        if op.target == P.TOPICS_TARGET:
            return None if P.topics_problems(evidence) else P.topics_desired(evidence)
        if op.target == P.UPDATE_TARGET:
            revs = {key: op.desired.get(key) for key in P.UPDATE_REV_KEYS}
            if evidence.get("repo_head") != revs["source_rev_to"]:
                return None
            return dict(install, **revs)
        return None

    def _health(self, target):
        """Problems with the evidence an attestation on ``target`` rests on ([] = healthy)."""

        evidence = self.state["evidence"]
        problems = P.install_problems(evidence)
        if target == P.TOPICS_TARGET and not problems:
            problems = P.topics_problems(evidence)
        return problems

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        return {
            "tier": TIER,
            "evidence": copy.deepcopy(self.state["evidence"]),
            "decommission": copy.deepcopy(self.state["decommission"]),
            "attestations": copy.deepcopy(self.state["attestations"]),
        }

    def preflight(self, op):
        attestations = self.state["attestations"]
        if op.kind == P.RETIRE_KIND:
            if op.target != P.RETIRE_TARGET:
                return PreflightResult(False, "refused", f"unknown target {op.target!r}")
            report = self.state["decommission"]
            if P.decommission_problems(report) or P.sha(report) != op.desired.get("decommission_report_sha256"):
                return PreflightResult(False, "drifted", "the decommission report changed since plan (or no longer shows a teardown)")
            if P.sha(attestations) != op.precondition.get("attestations_sha256"):
                return PreflightResult(False, "drifted", "the attestations changed since plan")
            return PreflightResult(True, self.name, "decommission observed; attestations match the plan")
        if op.target not in P.ATTEST_TARGETS:
            return PreflightResult(False, "refused", f"unknown target {op.target!r}")
        if self._current(op) != dict(op.desired):
            return PreflightResult(False, "drifted", "the legacy watch evidence changed since plan (or is no longer healthy)")
        prior = attestations.get(op.target)
        if (P.ABSENT if prior is None else P.sha(prior)) != op.precondition.get("attestation_sha256"):
            return PreflightResult(False, "drifted", "the attestation changed since plan")
        return PreflightResult(True, self.name, "legacy watch evidence healthy; it matches the plan")

    def rollback_intent(self, op):
        attestations = self.state["attestations"]
        if op.kind == P.RETIRE_KIND:
            before = {"attestations": json.dumps(attestations, sort_keys=True)}
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, P.sha({}))
        before = {"attestation": _blob(attestations.get(op.target))}
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, P.sha(dict(op.desired)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        if op.kind == P.RETIRE_KIND:
            self.state["attestations"] = {}
            detail = "simulated retirement of every watch attestation"
        else:
            self.state["attestations"][op.target] = dict(op.desired)
            detail = f"simulated attestation of the legacy watch install ({op.target})"
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, detail)

    def verify_operation(self, op):
        """The engine's verify (drift check) and the apply-time postcondition: the CURRENT
        evidence against the attested evidence; drifted keys are named (T-5948 shape)."""

        if op.kind == P.RETIRE_KIND:
            if self.state["attestations"]:
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, "drift: attestations re-appeared after the retire")
            return OperationEvidence(op.operation_id, op.layer, op.kind, True, "retired; no watch attestation remains")
        attested = dict(op.desired)
        if self.state["attestations"].get(op.target) != attested:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "drift: the attestation is absent or not this op's")
        problems = self._health(op.target)
        if problems:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift: the legacy watch install is no longer healthy: {problems}")
        evidence = self.state["evidence"]
        if op.target == P.TOPICS_TARGET:
            current = P.topics_desired(evidence)
        else:
            current = P.install_desired(evidence)
            attested = {k: v for k, v in attested.items() if k not in P.UPDATE_REV_KEYS}
        moved = P.changed_keys(attested, current)
        if moved:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift: legacy evidence changed since attestation: {moved}")
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "attested; legacy evidence still matches")

    def can_rollback(self, mutation):
        attestations = self.state["attestations"]
        if mutation.kind == P.RETIRE_KIND:
            if json.dumps(attestations, sort_keys=True) == mutation.before["attestations"]:
                return PreflightResult(True, "not-applied", "attestations still at their preimage")
            if P.sha(attestations) == mutation.after_fingerprint:
                return PreflightResult(True, "owned", "this op's retirement")
            return PreflightResult(False, "drifted", "the attestations moved past this op")
        current = attestations.get(mutation.target)
        if _blob(current) == mutation.before["attestation"]:
            return PreflightResult(True, "not-applied", "attestation still at its preimage")
        if current is not None and P.sha(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "this op's attestation")
        return PreflightResult(False, "drifted", "the attestation moved past this op")

    def rollback_mutation(self, mutation):
        if mutation.kind == P.RETIRE_KIND:
            self.state["attestations"] = json.loads(mutation.before["attestations"])
            return
        before = mutation.before["attestation"]
        if before == P.ABSENT:
            self.state["attestations"].pop(mutation.target, None)
        else:
            self.state["attestations"][mutation.target] = json.loads(before)

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


#: what a passing invariant reports
_PASS_DETAIL = {
    "W1-templates-present": "all six unit templates present with well-formed digests",
    "W2-landed-run-fresh": f"newest landed run within {P.FRESH_DEGRADED_DAYS} days",
    "W3-landed-by-pipeline-account": f"landed by {P.PIPELINE_ACCOUNT} with an ok row",
    "W4-topics-parse": "topics parse cleanly with an active topic",
    "W5-latest-day-no-fail": "every row in the newest day index is ok",
    "W6-reports-byte-sequence-clean": "no forbidden byte sequence under knowledge/watch/reports/",
}


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5135 S1: evaluate org.pantheon.watch/v1 (W1-W6) over the observed evidence.

    Each finding carries a severity: any FAILED finding makes the state unhealthy;
    DEGRADED-only findings (W2 past FRESH_DEGRADED_DAYS, W5) make it degraded."""

    evidence = (observed.get(P.HANDLER) or {}).get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("no watch evidence observed: could not ask")
    findings = list(P.install_findings(evidence))
    findings += [("W4-topics-parse", P.FAILED, message) for message in P.topics_problems(evidence)]
    if P.landed_failures(evidence):
        findings.append(("W5-latest-day-no-fail", P.DEGRADED, f"non-ok rows in the newest day index: {P.landed_failures(evidence)}"))
    results, severities = [], set()
    for entry in list_doc.get("invariants") or []:
        iid = entry.get("id") if isinstance(entry, dict) else None
        if iid not in _PASS_DETAIL:
            results.append({"id": str(iid), "status": "alarm", "detail": "no evaluator for this invariant"})
            severities.add(P.FAILED)
            continue
        mine = [(severity, message) for fid, severity, message in findings if fid == iid]
        severities.update(severity for severity, _message in mine)
        if mine:
            results.append({"id": iid, "status": "fail", "detail": "; ".join(message for _severity, message in mine)})
        else:
            results.append({"id": iid, "status": "pass", "detail": _PASS_DETAIL[iid]})
    state = "unhealthy" if P.FAILED in severities else "degraded" if severities else "ready"
    return {"results": results, "state": state}


HANDLERS = [WatchSim()]
