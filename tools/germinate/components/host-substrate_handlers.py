"""host-substrate component: executor-side registration (ORG-080 §2; T-5084, tranche GE).

``OP_KINDS`` is empty: the root vocabulary is the engine's own ORG-082 substrate
families (A-8.5: a component may not declare root kinds). ``HANDLERS`` serves
exactly those families through ``RootHelper`` over the SIMULATED port, pinned to
the ``bootstrap-in-process-root`` context. Nothing here actuates a live host;
the host port is dark (``host_substrate.ports.LIVE_ACTUATION_ENABLED``).

Engine seam gaps (T-5135): S6 -- the engine passes no invocation context, so
the context is pinned here and ``staged-cutover`` maps to the stage phase;
S7 -- the engine never reads ``privilege`` at actuation.

Seat fragments (T-5827, T-5091 U2; T-5829 ruling (1) Option A): with no explicit
``manifest_doc`` the manifest is the base merged with the managed seats' fragments
from this checkout's instance.yaml (``harness_manager_root.seat_merge``), and an op
whose recorded ``fragment_sha256:<role>`` set differs from the recomputation refuses (ruling
(1)(f)). An explicit ``manifest_doc`` (the T-2920 U3a host-broker seam) is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tools.germinate.components import harness_manager_root as HR
from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate.helper import ACTIVATE, BOOTSTRAP, STAGE, HelperRefusal, RootHelper
from tools.germinate.host_substrate.ports import SimulatedSubstratePort
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {}

MANIFEST = Path(__file__).with_name("host-substrate.manifest.yaml")
REPO_ROOT = Path(__file__).resolve().parents[3]


class SeatIdentityRefused(F.ManifestError):
    """The seat-fragment merge refused (an identity block, a fragment or the merge)."""


def _base_manifest(repo_root=None):
    """The in-repo base manifest alone (the R1-2 rollback fallback)."""
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    return F.load_manifest(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")), repo_root=root)


def _seat_manifest(doc=None, repo_root=None, subjects=None):
    """(validated manifest, recomputed fragment digests). An explicit ``doc`` is
    loaded as given and carries no seat fragments (``{}``)."""
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    if doc is not None:
        return F.load_manifest(doc, repo_root=root), {}
    base_doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    try:
        seats = HR.seat_merge(base_doc, HR.load_instance(root / "instance.yaml"),
                              subjects or (lambda: None), plan_time=False)
    except HR.IdentityRefused as exc:
        raise SeatIdentityRefused(f"seat identity refused: {exc}") from None
    return F.load_manifest(seats.doc, repo_root=root), dict(seats.fragment_sha256)


def _manifest(doc=None, repo_root=None, subjects=None):
    """The validated manifest. Defaults: the in-repo CURRENT manifest merged with the
    managed seats' fragments (T-5827), and this checkout. T-2920 U3a (render-staging
    seam, HO Q1): the host-broker installer passes its rendered fragment and the
    staging dir instead (additive only)."""
    return _seat_manifest(doc, repo_root, subjects)[0]


class HostSubstrateSim:
    name = "host-substrate"
    kinds = frozenset(F.FAMILIES)

    def __init__(self, *, manifest_doc=None, repo_root=None):
        self.port = SimulatedSubstratePort()
        self._manifest_doc = manifest_doc
        self._repo_root = repo_root
        self._fragments = {}

    def _helper(self):
        manifest, self._fragments = _seat_manifest(self._manifest_doc, self._repo_root, self.port.snapshot)
        return RootHelper(self.port, manifest, BOOTSTRAP)

    def _step(self, op):
        phase = STAGE if getattr(op, "execution_scope", "all") == "staged-cutover" else ACTIVATE
        helper = self._helper()
        refusal = HR.provenance_refusal(HR.recorded_fragments(op.provenance), self._fragments)
        if refusal is not None:  # T-5829 (1)(f): before any resolution against the merged manifest
            raise HelperRefusal(f"{op.operation_id}: {refusal}")
        step = helper.admit(op.kind, dict(op.desired), phase)
        if step.subject != op.target:
            raise HelperRefusal(f"{op.operation_id}: target {op.target!r} != resolved subject {step.subject!r}")
        # base_manifest_sha256 is AUDIT-ONLY (R1-6): any base change already moves the
        # merged manifest_sha256, which this check compares.
        if op.provenance.get("manifest_sha256") != helper.manifest.sha256:
            raise HelperRefusal(f"{op.operation_id}: the manifest changed since plan")
        return helper, step

    def _membership_guard(self, op, step):
        """T-6167 Q3 (b): a group.member (present) needs its group AND its account already on the
        substrate. Runs in preflight AND execute; a membership never creates either. (a), the seat's
        own account only, is enforced upstream: check_seat_scope, and _step's fragment-digest check."""
        if step.resolved.family != "group.member" or step.resolved.after is not True or step.phase != ACTIVATE:
            return  # a removal, or the staged-cutover phase (writes only staged: subjects, no live group needed)
        group, account = step.resolved.subject.split(":")[1:]
        snapshot = self.port.snapshot()
        if f"group:{group}" not in snapshot:
            raise HelperRefusal(f"{op.operation_id}: group {group!r} does not exist; a membership never creates a group")
        if f"passwd:{account}" not in snapshot:
            raise HelperRefusal(f"{op.operation_id}: account {account!r} does not exist; a membership never creates one")

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        # Shape divergence (T-2920 U3b review): the SIMULATED snapshot carries ``locked`` on
        # passwd:*, which VERIFY's HS1 compares; HostSubstratePort.snapshot has no ``locked``
        # (no shadow enumeration, Recovery D15). Wiring a host port here needs per-account get().
        return {"subjects": self.port.snapshot()}

    def preflight(self, op):
        try:
            helper, step = self._step(op)
        except (HelperRefusal, F.ManifestError) as exc:
            return PreflightResult(False, "refused", str(exc))
        try:
            self._membership_guard(op, step)
        except HelperRefusal as exc:
            return PreflightResult(False, "refused", str(exc))
        if F.sha256_text(F.canonical(helper.before(step))) != op.precondition.get("observed_sha256"):
            return PreflightResult(False, "drifted", f"{step.subject} changed since plan")
        evidence = helper.preflight(step)
        return PreflightResult(evidence.ok, self.name if evidence.ok else "refused", evidence.detail)

    def rollback_intent(self, op):
        helper, step = self._step(op)
        before = helper.before(step)
        record = {"snapshot": F.canonical(before)}
        fragments = HR.recorded_fragments(op.provenance)
        if fragments:  # R1-2: rollback must know the op resolved against seat fragments
            record["fragment_sha256"] = F.canonical(fragments)
        return MutationRecord(op.operation_id, op.kind, step.subject, op.owner, True,
                              record, F.canonical(step.resolved.after))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        helper, step = self._step(op)
        self._membership_guard(op, step)
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
        # T-5653: a rollback is path-taking actuation too; the symlink guard applies.
        try:
            helper = self._helper()
        except SeatIdentityRefused:
            # R1-2: a broken identity block must not strand an unrelated in-flight run
            # (ORG-080 §10: an unreversed op is never dropped). Only an op that resolved
            # against the BASE alone may roll back against it; a fragment op cannot be
            # resolved against fragments that can no longer be rebuilt, so it refuses.
            if "fragment_sha256" in mutation.before or self._manifest_doc is not None:
                raise
            helper = RootHelper(self.port, _base_manifest(self._repo_root), BOOTSTRAP)
        before = json.loads(mutation.before["snapshot"])
        if mutation.target.startswith("member:") and before is True and json.loads(mutation.after_fingerprint) is True:
            return  # T-6167 Q3 (d): an ADD onto a membership that pre-existed added nothing to remove (a removal still restores it)
        helper.guarded_put(mutation.target, before)

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
        for r in I.evaluate(_manifest(subjects=lambda: subjects), subjects)
    ]
    return {"results": results, "state": "ready" if all(r["status"] == "pass" for r in results) else "unhealthy"}
