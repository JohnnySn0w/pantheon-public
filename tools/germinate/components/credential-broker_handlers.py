"""credential-broker component: executor-side registration (ORG-051, ORG-080 §2; T-5092, tranche GG).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``credential-broker.attest`` class. The component
has NO kind that touches credential material: install / rotate / revoke are the
gated stubs of tools/credential_broker_cli.py (pending the T-2869 custody
decision, operator-reserved), so the only mutation here is the engine's own
attestation record.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier, nothing live):
``CredentialBrokerSim`` keeps reference METADATA only (presence, mode, parent
mode, expiry) per declared ``credential_refs`` id from this checkout's
instance.yaml. Observation WRAPS the broker unchanged: each reference becomes a
ref-spec in the CLI's own shape, parsed by ``credential_broker_cli._ref_from_spec``
and checked by ``HostFileCredentialBroker.verify`` over a throwaway file holding a
fixed placeholder (never a secret; verify opens it but never reads it).

Observe-and-attest install (T-5947): the ``credential-broker.adopt-attest`` op
carries the verify evidence (``verify_rc``, ``report_sha256``) in ``desired``,
so the approved plan digest binds it and the receipt records it; its only
mutation is the attestation entry in this handler's state, and its inverse
restores the prior entry. ``verify_operation`` compares the CURRENT evidence with
the attested evidence and names the drifted keys. Attestation does not make
dependency satisfaction fresh (``_satisfied_install`` counts any SATISFIED install
run, however old; residual T-5954).

On a real host this refuses today: the legacy install is a gated stub (T-2869)
and instance.yaml's credential_refs carry no location / uid / mode (gap under
T-2870), so there is no healthy legacy setup to attest. No live handler exists.

Simulated state::

    refs:        {credential_id: {present: bool, mode: "0600", parent_mode: "0700", expires_at: null|float}}
    attestation: null | {evidence key: str}
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from tools import credential_broker_cli as CLI
from tools.credential_broker import HostFileCredentialBroker
from tools.persona_installer import Manifest
from tools.persona_installer_contracts import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "credential-broker.adopt-attest": ("credential-broker.attest", "seat"),
    "credential-broker.attest-retire": ("credential-broker.attest", "seat"),
}

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "instance.yaml"
EVIDENCE_KEYS = ("verify_rc", "report_sha256", "refs")
PLACEHOLDER = b"germinate-sim placeholder, not a credential\n"


def _default_state():
    return {"refs": {}, "attestation": None}


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _canon(value):
    return json.dumps(value, sort_keys=True)


class CredentialBrokerSim:
    name = "credential-broker-refs"
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()
        self._declared = None

    def declared(self):
        """``[(persona, account, CredentialReference)]`` from this checkout's instance.yaml, in manifest order."""

        if self._declared is None:
            manifest = Manifest.load(MANIFEST, strict_flip_keys=False)
            out = []
            for raw in manifest.roles.values():
                persona = raw.get("persona") if isinstance(raw, dict) else None
                if not isinstance(persona, str) or not persona:
                    continue
                binding = manifest.binding(persona)
                out.extend((persona, binding.account, ref) for ref in binding.credential_refs)
            self._declared = out
        return self._declared

    def attestation_owner(self):
        """The account that owns the attestation op (review F5).

        Each declared reference names its ``owner`` in instance.yaml (the account
        the material belongs to; every live reference says ``claude`` today). The
        attestation runs as that one declared owner. References naming more than
        one owner yield None and the planner refuses: picking the first manifest
        persona would be an arbitrary choice, and no designated steward account
        exists (the contract steward ``codex`` is a persona, not the owner of this
        material).
        """

        owners = {ref.owner for _persona, _account, ref in self.declared()}
        return owners.pop() if len(owners) == 1 else None

    def legacy_verify(self):
        """``HostFileCredentialBroker.verify`` per declared reference, over a throwaway materialization."""

        report = []
        with tempfile.TemporaryDirectory(prefix="germinate-credentials-") as tmp:
            root = Path(tmp)
            for _persona, _account, decl in self.declared():
                sim = self.state["refs"].get(decl.reference_id) or {}
                parent = root / decl.reference_id.replace("/", "__")
                parent.mkdir(mode=0o700)
                os.chmod(parent, int(sim.get("parent_mode", "0700"), 8))
                location = parent / "material"
                if sim.get("present"):
                    location.write_bytes(PLACEHOLDER)
                    os.chmod(location, int(sim.get("mode", "0600"), 8))
                spec = {
                    "credential_id": decl.reference_id,
                    "kind": decl.kind,
                    "provider": "host-file",
                    "location": str(location),
                    "owner_uid": os.getuid(),
                    "owner_gid": os.getgid(),
                    "mode": "0600",
                    "host_file": {"parent_dir": str(parent), "parent_owner_uid": os.getuid(), "parent_owner_gid": os.getgid(), "parent_mode": "0700"},
                    "expires_at": sim.get("expires_at"),
                }
                try:
                    ref = CLI._ref_from_spec(spec)
                    status = HostFileCredentialBroker((ref,), audit_path=root / "audit.jsonl").verify(ref)
                    entry = {
                        "credential_id": status.credential_id,
                        "available": status.available,
                        "mode": None if status.mode is None else format(status.mode, "04o"),
                        "expires_at": status.expires_at,
                        "errors": [e.replace(tmp, "<sim>") for e in status.errors],
                    }
                except (CLI.CliError, ValueError) as exc:
                    entry = {"credential_id": decl.reference_id, "available": False, "mode": None, "expires_at": None,
                             "errors": [str(exc).replace(tmp, "<sim>")]}
                report.append(entry)
        return report

    def evidence(self, report=None):
        report = self.legacy_verify() if report is None else report
        return {
            "verify_rc": "0" if report and all(r["available"] for r in report) else "1",
            "report_sha256": _sha(report),
            "refs": ",".join(sorted(r["credential_id"] for r in report)),
        }

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        report = self.legacy_verify()
        return {
            "owner": self.attestation_owner(),
            "verify": {"rc": 0 if report and all(r["available"] for r in report) else 1, "report": report},
            "evidence": self.evidence(report),
            "attestation": copy.deepcopy(self.state["attestation"]),
            "gate_message": CLI.GATE_MESSAGE,
        }

    def _drift(self, attested):
        current = self.evidence()
        return current, sorted(k for k in EVIDENCE_KEYS if current.get(k) != (attested or {}).get(k))

    @staticmethod
    def _result(op):
        if op.kind == "credential-broker.adopt-attest":
            return {k: op.desired[k] for k in EVIDENCE_KEYS}
        return None

    def preflight(self, op):
        if op.kind == "credential-broker.adopt-attest":
            current, drifted = self._drift(op.desired)
            ok = not drifted and current["verify_rc"] == "0"
            return PreflightResult(ok, self.name, "verify healthy and matches the planned evidence" if ok else f"evidence moved since plan: {drifted or ['verify_rc']}")
        ok = self.state["attestation"] is not None
        return PreflightResult(ok, self.name, "attestation present" if ok else "no attestation to retire")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"slot": _canon(self.state["attestation"])}, _canon(self._result(op)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        self.state["attestation"] = self._result(op)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        stored = self.state["attestation"]
        if op.kind == "credential-broker.attest-retire":
            ok = stored is None
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "attestation retired" if ok else "attestation still present")
        if stored != self._result(op):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "the recorded attestation is not this op's evidence")
        current, drifted = self._drift(stored)
        if drifted:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift from the attested evidence: {', '.join(drifted)} (verify_rc now {current['verify_rc']})")
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, f"attested broker verify rc 0 over {stored['refs']}")

    def can_rollback(self, mutation):
        current = _canon(self.state["attestation"])
        if current == mutation.before["slot"]:
            return PreflightResult(True, "not-applied", "attestation still at its preimage")
        owned = current == mutation.after_fingerprint
        return PreflightResult(owned, "owned" if owned else "drifted", "this op's record" if owned else "attestation moved since the op")

    def rollback_mutation(self, mutation):
        self.state["attestation"] = json.loads(mutation.before["slot"])

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


HANDLERS = [CredentialBrokerSim()]
