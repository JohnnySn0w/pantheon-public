"""action-broker component: executor-side registration (ORG-080 §2; T-5948).

``OP_KINDS`` is literal data (read with ast.literal_eval): one seat-privilege kind,
``action-broker.adopt-attest``, in the component's own non-reserved
``action-broker.adoption`` class.

The install leg is OBSERVE-AND-ATTEST. The engine never installs the broker: the
live install is the head role's supervised root run (T-2920 / T-2925, through
``tools/host_broker_install_apply.py``). This handler reports that legacy install's
evidence (the ``pantheon-host-broker-install-receipt/v1`` receipt and the legacy
``verify()`` result) and its one mutation is an attestation entry in its OWN state
(inverse: remove it). Nothing on the host changes.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier): the legacy evidence
lives as JSON state inside the simulated host, seeded by tests from real
fixture-mode ``apply`` / ``verify`` output. It accepts ``fixture`` receipts only
(``TIER``); a ``rehearsal-nspawn`` receipt never serves (Recovery (c)). There is no
live evidence reader here (T-5948 D5): with nothing seeded the observation carries
no receipt and the planner refuses, which is the honest answer today (the broker
is not installed on the host).

Simulated state::

    install:      {receipt: <receipt doc> | None,
                   verify: {rc: int, plan_sha256: str, report: [<legacy verify rows>]} | None}
    attestations: {target: {<the op's desired evidence digests>}}
"""

from __future__ import annotations

import copy
import hashlib
import json

from tools import host_broker_install_apply as A
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "action-broker.adopt-attest": ("action-broker.adoption", "seat"),
}

KIND = "action-broker.adopt-attest"
TARGET = "action-broker:legacy-install"
#: the receipt mode this (simulated) handler's evidence tier serves (Recovery (c))
TIER = A.FIXTURE_MODE
ABSENT = "absent"


def _default_state():
    return {"install": {"receipt": None, "verify": None}, "attestations": {}}


def _sha(value):
    return hashlib.sha256(A.canonical(value).encode("ascii")).hexdigest()


def evidence(receipt, verify):
    """The attested evidence digests, re-derived from the legacy shapes (or None if
    unusable). The planner (action-broker.py) derives the same fields and refuses with
    reasons; this is the handler's own re-check, so a drift between plan and apply
    fails preflight / verify."""

    if not isinstance(receipt, dict) or not isinstance(verify, dict):
        return None
    report = verify.get("report")
    if not isinstance(report, list):
        return None
    return {
        "receipt_sha256": str(receipt.get("receipt_sha256")),
        "plan_sha256": str(receipt.get("plan_sha256")),
        "framework_rev": str(receipt.get("framework_rev")),
        "mode": str(receipt.get("mode")),
        "status": str(receipt.get("status")),
        "verify_report_sha256": _sha(report),
    }


def _healthy(receipt, verify):
    """The same refusal set the planner applies, as a boolean."""

    try:
        if receipt.get("schema") != A.RECEIPT_SCHEMA or receipt.get("receipt_sha256") != A.receipt_digest(receipt):
            return False
        if receipt.get("mode") == A.REHEARSAL_MODE:
            return False
        A.require_receipt_mode(receipt, TIER)
    except (A.ApplyRefused, AttributeError, TypeError, ValueError):
        return False
    report = verify.get("report") or []
    return (
        receipt.get("status") in ("complete", "noop")
        and verify.get("rc") == 0
        and bool(report)
        and all(isinstance(r, dict) and r.get("ok") is True for r in report)
        and verify.get("plan_sha256") == receipt.get("plan_sha256")
    )


class ActionBrokerInstallSim:
    name = "action-broker-install"
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()

    def _current(self):
        install = self.state["install"]
        receipt, verify = install.get("receipt"), install.get("verify")
        if not isinstance(receipt, dict) or not isinstance(verify, dict) or not _healthy(receipt, verify):
            return None
        return evidence(receipt, verify)

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        install = self.state["install"]
        return {
            "tier": TIER,
            "receipt": copy.deepcopy(install.get("receipt")),
            "verify": copy.deepcopy(install.get("verify")),
            "attestation": copy.deepcopy(self.state["attestations"].get(TARGET)),
        }

    def _attestation_sha(self, target):
        att = self.state["attestations"].get(target)
        return ABSENT if att is None else _sha(att)

    def preflight(self, op):
        if op.target != TARGET:
            return PreflightResult(False, "refused", f"unknown target {op.target!r}")
        if self._current() != dict(op.desired):
            return PreflightResult(False, "drifted", "the legacy install evidence changed since plan (or is no longer healthy)")
        if self._attestation_sha(op.target) != op.precondition.get("attestation_sha256"):
            return PreflightResult(False, "drifted", "the attestation changed since plan")
        return PreflightResult(True, self.name, "legacy install healthy; evidence matches the plan")

    def rollback_intent(self, op):
        prior = self.state["attestations"].get(op.target)
        before = {"attestation": ABSENT if prior is None else json.dumps(prior, sort_keys=True)}
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, _sha(dict(op.desired)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        self.state["attestations"][op.target] = dict(op.desired)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, "simulated attestation of the legacy T-2920 install")

    def verify_operation(self, op):
        """The engine's verify (drift check) and the apply-time postcondition: the CURRENT
        observed legacy evidence against the attested evidence (T-5948 / T-5092 shape)."""

        attested = dict(op.desired)
        att = self.state["attestations"].get(op.target)
        if att != attested:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "drift: the attestation is absent or not this op's")
        install = self.state["install"]
        receipt, verify = install.get("receipt"), install.get("verify")
        if not isinstance(receipt, dict) or not isinstance(verify, dict) or not _healthy(receipt, verify):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "drift: the legacy install is no longer healthy")
        current = evidence(receipt, verify)
        changed = sorted(k for k in attested if current.get(k) != attested[k])
        if changed:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift: legacy evidence changed since attestation: {changed}")
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, "attested; legacy evidence still matches")

    def can_rollback(self, mutation):
        att = self.state["attestations"].get(mutation.target)
        before = mutation.before["attestation"]
        current = ABSENT if att is None else json.dumps(att, sort_keys=True)
        if current == before:
            return PreflightResult(True, "not-applied", "attestation still at its preimage")
        if att is not None and _sha(att) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "this op's attestation")
        return PreflightResult(False, "drifted", "the attestation moved past this op")

    def rollback_mutation(self, mutation):
        before = mutation.before["attestation"]
        if before == ABSENT:
            self.state["attestations"].pop(mutation.target, None)
        else:
            self.state["attestations"][mutation.target] = json.loads(before)

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


HANDLERS = [ActionBrokerInstallSim()]
