"""pantheon-config component: executor-side registration (ORG-080 §2; T-5081).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``repo.checkout`` class.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier, nothing live):
``PantheonConfigRepoSim`` keeps a simulated remote and checkouts as JSON state
inside the simulated host. Its ``observe()`` yields ``CheckoutObservation``-shaped
data per checkout (what ``repo_reconciler.GitCheckoutProbe`` reports for a real
checkout), and ``verify_operation`` runs the reconciler's own ``verify()`` over the
post-state. The real git-backed handler (apply delegating to
``RepoReconciler.apply``) is ``pantheon_config_git.PantheonConfigRepoGit``
(T-5133). It is deliberately NOT listed here and not imported: it lands dark
(literal-False ``LIVE_ACTUATION_ENABLED``, sandbox-only), and handler import
trust (T-5477) must land before any real handler is registered (cutover T-3936).

Simulated state::

    remote:    {url, reachable, refs: {branch: sha}, parents: {sha: parent|None}}
    checkouts: {checkout_id: {head, origin, dirty, installed_sha, local_parents}}
    health:    {sha: bool}   # the post-state health probe result per HEAD (default True)
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

from tools.checkout_spec import CheckoutMode, CheckoutSpec, load_checkout_spec
from tools.persona_installer import MutationRecord, OperationEvidence, PreflightResult
from tools.repo_reconciler import ACTION_NOOP, STATE_CURRENT, CheckoutObservation, RepoReconciler

OP_KINDS = {
    "repo.clone": ("repo.checkout", "seat"),
    "repo.ff-advance": ("repo.checkout", "seat"),
    "repo.reclaim": ("repo.checkout", "seat"),
}

LOGICAL_REPO = "pantheon-config"
SIM_URL = "sim://pantheon-config"
CHECKOUTS_DIR = Path(__file__).resolve().parents[3] / "checkouts"
_OBS_FIELDS = tuple(f.name for f in dataclasses.fields(CheckoutObservation))


def _default_state():
    return {"remote": {"url": SIM_URL, "reachable": True, "refs": {}, "parents": {}}, "checkouts": {}, "health": {}}


def _norm(url):
    return (url or "").rstrip("/").removesuffix(".git")


class _SimProbe:
    """CheckoutProbe over the simulated state (the reconciler's observer seam)."""

    def __init__(self, handler):
        self._h = handler

    def observe(self, entry, remote_url):
        raw = self._h.raw_observation(entry)
        return CheckoutObservation(**{name: raw[name] for name in _OBS_FIELDS})


class PantheonConfigRepoSim:
    name = "pantheon-config-repo"
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()

    # --- simulated git ------------------------------------------------------

    def _spec(self):
        return CheckoutSpec({e.checkout_id: e for e in load_checkout_spec(CHECKOUTS_DIR).by_logical_repo(LOGICAL_REPO)})

    def _ancestors(self, sha, local_parents=None):
        parents = dict(self.state["remote"]["parents"], **(local_parents or {}))
        seen = []
        while sha is not None and sha not in seen and sha in parents:
            seen.append(sha)
            sha = parents[sha]
        return seen

    def raw_observation(self, entry):
        """GitCheckoutProbe.observe's fields, computed over simulated state, plus
        the desired-target sha and health the planner needs."""

        remote = self.state["remote"]
        ck = self.state["checkouts"].get(entry.checkout_id)
        exists = ck is not None
        managed = exists and _norm(ck.get("origin")) == _norm(remote["url"])
        local_parents = (ck or {}).get("local_parents") or {}
        refs = remote["refs"] if remote["reachable"] else None
        if entry.mode.is_pinned:
            target = entry.mode.pinned
            present = refs is not None and (target in refs.values() or (managed and target in self._ancestors(target, local_parents)))
        else:
            target = refs.get(entry.mode.tracking) if refs is not None else None
            present = target is not None
        head = ck["head"] if managed else None
        ahead = behind = 0
        resolvable = False
        if managed and target is not None:
            resolvable = bool(self._ancestors(target, local_parents))
            if resolvable:
                mine, theirs = set(self._ancestors(head, local_parents)), set(self._ancestors(target, local_parents))
                ahead, behind = len(mine - theirs), len(theirs - mine)
        return {
            "remote_reachable": remote["reachable"],
            "desired_present": present,
            "exists": exists,
            "is_managed": managed,
            "dirty": bool(managed and ck.get("dirty")),
            "head_sha": head,
            "ahead": ahead,
            "behind": behind,
            "desired_resolvable": resolvable,
            "desired_target": target,
            "health_ok": bool(self.state["health"].get(head, True)) if head else False,
            "installed_sha": (ck or {}).get("installed_sha"),
        }

    def _reconciler(self, spec):
        return RepoReconciler(
            spec,
            probe=_SimProbe(self),
            remote_resolver=lambda _r: self.state["remote"]["url"],
            health_probe=lambda entry: self.raw_observation(entry)["health_ok"],
        )

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        spec = self._spec()
        return {
            "remote_url": self.state["remote"]["url"],
            "checkouts": {cid: self.raw_observation(entry) for cid, entry in sorted(spec.checkouts.items())},
        }

    def _ck(self, op):
        return self.state["checkouts"].get(op.target)

    def preflight(self, op):
        ck = self._ck(op)
        want = op.desired.get("head")
        if op.kind == "repo.clone":
            ok = ck is None and self.state["remote"]["reachable"] and bool(self._ancestors(want))
            return PreflightResult(ok, self.name, "absent, remote holds the target" if ok else "target present or remote lacks it")
        if ck is None or ck.get("dirty"):
            return PreflightResult(False, self.name, "checkout absent or dirty")
        if op.kind == "repo.ff-advance":
            ok = ck["head"] == op.precondition["head"] and ck["head"] in self._ancestors(want, ck.get("local_parents"))
            return PreflightResult(ok, self.name, "fast-forward available" if ok else "HEAD moved or not a fast-forward")
        ok = ck["head"] == op.precondition["head"] == ck.get("installed_sha")
        return PreflightResult(ok, self.name, "pristine engine-installed clone" if ok else "not pristine")

    def rollback_intent(self, op):
        ck = self._ck(op)
        if op.kind == "repo.clone":
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"exists": "false"}, op.desired["head"])
        if op.kind == "repo.ff-advance":
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"head": ck["head"]}, op.desired["head"])
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"snapshot": json.dumps(ck, sort_keys=True)}, "absent")

    def execute(self, op):
        mutation = self.rollback_intent(op)
        if op.kind == "repo.clone":
            want = op.desired["head"]
            self.state["checkouts"][op.target] = {"head": want, "origin": self.state["remote"]["url"], "dirty": False, "installed_sha": want}
        elif op.kind == "repo.ff-advance":
            self.state["checkouts"][op.target]["head"] = op.desired["head"]
        else:
            del self.state["checkouts"][op.target]
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        if op.kind == "repo.reclaim":
            ok = self._ck(op) is None
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "reclaimed" if ok else "still present")
        spec = self._spec()
        entry = spec.checkouts.get(op.target)
        if entry is None:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "checkout not in spec")
        pinned = dataclasses.replace(entry, mode=CheckoutMode(pinned=op.desired["head"]))
        [item] = self._reconciler(CheckoutSpec({entry.checkout_id: pinned})).verify().items
        ok = item.state == STATE_CURRENT and item.action == ACTION_NOOP and item.refusal is None
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, f"repo_reconciler verify: {item.state} {item.detail}".strip())

    def can_rollback(self, mutation):
        ck = self.state["checkouts"].get(mutation.target)
        if mutation.kind == "repo.reclaim":
            if ck is None:
                return PreflightResult(True, "owned", "reclaimed by this op")
            same = json.dumps(ck, sort_keys=True) == mutation.before["snapshot"]
            return PreflightResult(same, "not-applied" if same else "drifted", "")
        if mutation.kind == "repo.clone":
            if ck is None:
                return PreflightResult(True, "not-applied", "never cloned")
            owned = ck["head"] == mutation.after_fingerprint == ck.get("installed_sha") and not ck.get("dirty")
            return PreflightResult(owned, "owned" if owned else "drifted", "pristine clone" if owned else "clone moved or dirty")
        if ck is None:
            return PreflightResult(False, "drifted", "checkout vanished")
        if ck["head"] == mutation.before["head"]:
            return PreflightResult(True, "not-applied", "HEAD still at preimage")
        owned = ck["head"] == mutation.after_fingerprint and not ck.get("dirty")
        return PreflightResult(owned, "owned" if owned else "drifted", "own advance" if owned else "HEAD moved or dirty")

    def rollback_mutation(self, mutation):
        if mutation.kind == "repo.clone":
            del self.state["checkouts"][mutation.target]
        elif mutation.kind == "repo.ff-advance":
            # The engine's rollback of ITS OWN receipted, clean advance (ORG-080 §10).
            self.state["checkouts"][mutation.target]["head"] = mutation.before["head"]
        else:
            self.state["checkouts"][mutation.target] = json.loads(mutation.before["snapshot"])

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


HANDLERS = [PantheonConfigRepoSim()]
