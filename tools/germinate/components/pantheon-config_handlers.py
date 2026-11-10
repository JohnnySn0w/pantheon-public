"""pantheon-config component: executor-side registration (ORG-080 §2; T-5081).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``repo.checkout`` class.

``HANDLERS`` holds TWO handlers, and both are SIMULATED (CI tier, nothing live):
``PantheonConfigRepoSim`` (the three repo kinds) and ``PantheonConfigLocalSim``
(the install leg's ``repo.hooks-activate`` and ``pantheon-config.adopt-attest``, and
the configure leg's ``pantheon-config.manifest-attest``; simulated tier; the live
handler for those kinds is T-6098).
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
    commit_ts: {sha: epoch}  # optional: commit timestamps; absent reads as unknown
    clock:     epoch         # optional: the observation clock; absent reads as unknown. NO real time anywhere.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import posixpath
from pathlib import Path

from tools.checkout_spec import CheckoutMode, CheckoutSpec, load_checkout_spec
from tools.persona_installer import MutationRecord, OperationEvidence, OperationFailed, PreflightResult
from tools.repo_reconciler import ACTION_NOOP, STATE_CURRENT, CheckoutObservation, RepoReconciler

OP_KINDS = {
    "repo.clone": ("repo.checkout", "seat"),
    "repo.ff-advance": ("repo.checkout", "seat"),
    "repo.reclaim": ("repo.checkout", "seat"),
    "repo.hooks-activate": ("repo.checkout", "seat"),
    "pantheon-config.adopt-attest": ("repo.checkout", "seat"),
    "pantheon-config.manifest-attest": ("repo.checkout", "seat"),
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
    kinds = frozenset({"repo.clone", "repo.ff-advance", "repo.reclaim"})

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
        branch = (ck.get("branch") or entry.mode.tracking) if managed else None  # default: on the tracking branch; "HEAD" = detached
        ahead = behind = 0
        resolvable = False
        oldest = None
        if managed and target is not None:
            resolvable = bool(self._ancestors(target, local_parents))
            if resolvable:
                mine, theirs = set(self._ancestors(head, local_parents)), set(self._ancestors(target, local_parents))
                ahead, behind = len(mine - theirs), len(theirs - mine)
                stamps = [self.state.get("commit_ts", {}).get(sha) for sha in mine - theirs]
                if ahead and all(isinstance(t, int) and not isinstance(t, bool) for t in stamps):
                    oldest = min(stamps)  # None when nothing is unpushed or any unpushed commit has no timestamp
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
            "tracking": entry.mode.tracking,
            "branch": branch,
            "oldest_unpushed_commit_ts": oldest,  # extra facts for VERIFY's PC-4; the probes select by _OBS_FIELDS and ignore them
            "observed_at": self.state.get("clock"),  # the sim clock (epoch seconds); absent reads as unknown, never real time
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


# --- the install leg's local kinds (simulated tier; live handler is T-6098) -----------

HOOKS_PATH = ".githooks"  # tools/install_hooks.sh's exact value; the value a NEW activation writes
HOOK_NAMES = ("pre-commit", "commit-msg", "pre-push")
BASIS_LEGACY = "observed-legacy"


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def hooks_path_is_own(value, toplevel, realpath, isdir):
    """``(own, reason)``: does ``core.hooksPath`` ``value`` resolve to THAT clone's own ``.githooks``?

    One rule for every caller: a relative value is taken from the clone's top level, both sides are
    resolved with the injected ``realpath`` (symlinks and ``..`` followed), and the value is own only
    when it lands exactly on ``realpath(toplevel)/.githooks`` and that is an existing directory. The
    expected side is joined, not resolved further: a repo ``.githooks`` that is itself a link to
    elsewhere does NOT make the outside directory own (hooks would run from outside the tracked tree).
    Another clone's ``.githooks`` therefore never passes, whatever its spelling. ``realpath`` and
    ``isdir`` are injected so the simulated tier resolves over simulated state; the live handler
    (T-6098) passes ``os.path.realpath`` and ``os.path.isdir``. An unset (None) value is not
    classified here: callers handle it."""

    if not isinstance(value, str) or not value:
        return False, "not a path"
    path = value if posixpath.isabs(value) else posixpath.join(toplevel, value)
    real = realpath(path)
    expected = posixpath.join(realpath(toplevel), HOOKS_PATH)
    if real != expected:
        return False, f"resolves to {real}, not {expected}"
    if not isdir(real):
        return False, "not an existing directory"
    return True, "own"


_SYMLINK_DEPTH_CAP = 40


def _sim_realpath(symlinks, path):
    """A pure realpath over the simulated ``fs.symlinks`` map ({abs_path: abs_target}): ``.`` and ``..``
    are folded and links followed at every component. A loop or an over-deep chain returns a marker path
    that no clone's ``.githooks`` can equal."""

    pending = [c for c in path.split("/") if c]
    resolved, follows = [], 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = "/" + "/".join(resolved + [part])
        if candidate in symlinks:
            follows += 1
            if follows > _SYMLINK_DEPTH_CAP:
                return "/!symlink-loop" + candidate
            target = symlinks[candidate]
            pending = [c for c in target.split("/") if c] + pending
            if target.startswith("/"):
                resolved = []
            continue
        resolved.append(part)
    return "/" + "/".join(resolved)


def _default_local_state():
    return {"hooks": {}, "attestations": {}, "manifests": {}, "manifest_attestations": {}, "fs": {"symlinks": {}, "dirs": [], "absent": []}}


def _default_hooks():
    return {"hooks_path": None, "modes": {name: "noexec" for name in HOOK_NAMES}, "engine": False}


def _manifest_validity(text):
    """(valid, detail) for the instance.yaml manifest bytes. Valid means the fail-closed
    tools.agent_role_map._load_membership_projection returns: it covers the parse, the
    mapping document and the role_membership_projection block. The roles: stanza is
    NOT checked. The bytes go to a private tmp file; the loader's parse-cache entry for
    that path is dropped so the check leaves no residue. The lazy import reads the
    worktree's tracked instance.yaml read-only, as every importer of that module does."""

    import os
    import tempfile

    from tools import agent_role_map as role_map

    with tempfile.TemporaryDirectory(prefix="germinate-manifest-") as tmp:
        path = Path(tmp) / "instance.yaml"
        path.write_bytes(text.encode("utf-8"))
        try:
            role_map._load_membership_projection(path)
        except role_map.MembershipSourceMissing as exc:
            return False, str(exc).replace(str(path), "instance.yaml")
        finally:
            role_map._MANIFEST_CACHE.pop(os.path.abspath(path), None)
    return True, ""


def _no_view(_cid):
    return {"exists": False, "installed_sha": None}


class PantheonConfigLocalSim:
    """Simulated ``repo.hooks-activate`` / ``pantheon-config.adopt-attest`` handler.

    It owns only hook and attestation state; whether a checkout exists, its HEAD,
    origin and cleanliness come from an injected ``view(checkout_id)`` (the repo
    handler's observation), so it stays a sibling of the repo handler and never
    reaches into its state. Each entry is keyed to the checkout identity
    (``installed_sha``) it was written against: an entry for a checkout that is
    gone, or was re-cloned under another identity, reads as the default entry, so
    a rolled-back clone never leaves hooks state for a later clone to inherit.

    ``hooks_path`` is healthy iff it resolves to THAT clone's own ``.githooks``
    (``hooks_path_is_own``: the literal ``.githooks``, the clone's absolute
    ``.githooks`` as hand-installed seats have it, or a link resolving into it). The
    simulated filesystem (``fs``) is the resolver's whole world; a new activation
    still writes only the literal ``.githooks``.

    State::

        hooks:        {checkout_id: {hooks_path, modes{hook: exec|noexec|missing}, engine, identity}}
        attestations: {checkout_id: {checkout_id, origin, hooks_path, hook_modes, observed_head, basis, identity}}
        manifests:    {checkout_id: {text, identity}}              # instance.yaml bytes; the engine NEVER writes this
        manifest_attestations: {checkout_id: {digest, identity}}   # sha256 of the attested manifest bytes
        fs:           {symlinks: {abs_path: abs_target}, dirs: [abs_path], absent: [abs_path]}  # default empty
                      # implicit dirs: each existing checkout's target_path and target_path/.githooks, minus absent
    """

    name = "pantheon-config-local"
    kinds = frozenset({"repo.hooks-activate", "pantheon-config.adopt-attest", "pantheon-config.manifest-attest"})
    STABLE = ("checkout_id", "origin", "hooks_path", "hook_modes")

    def __init__(self, view=None):
        self._view = view or _no_view
        self.state = _default_local_state()

    def bind_view(self, view):
        self._view = view

    def bind_observation(self, observe_repo):
        """Point the view at a repo handler's ``observe()`` output (any repo handler)."""

        def view(cid):
            obs = observe_repo()
            raw = (obs.get("checkouts") or {}).get(cid) or {}
            return {
                "exists": bool(raw.get("exists")),
                "installed_sha": raw.get("installed_sha"),
                "head": raw.get("head_sha"),
                "dirty": bool(raw.get("dirty")),
                "origin": _norm(obs.get("remote_url")) if raw.get("is_managed") else None,
            }

        self._view = view

    # --- views --------------------------------------------------------------

    def _spec_ids(self):
        spec = CheckoutSpec({e.checkout_id: e for e in load_checkout_spec(CHECKOUTS_DIR).by_logical_repo(LOGICAL_REPO)})
        return sorted(spec.checkouts)

    def _v(self, cid):
        v = dict(self._view(cid))
        v.setdefault("installed_sha", None)
        v["exists"] = bool(v.get("exists"))
        return v

    def _fresh(self, entry, v):
        return entry is not None and v["exists"] and entry.get("identity") == v["installed_sha"]

    def _hooks(self, cid):
        entry = self.state["hooks"].get(cid)
        if not self._fresh(entry, self._v(cid)):
            return _default_hooks()
        # only the named hooks are hook state: a stray sim entry (__pycache__, *.pyc) is neither a hook, a missing hook, a mode failure nor attestation evidence
        modes = {n: entry["modes"][n] for n in HOOK_NAMES if n in entry["modes"]}
        return {"hooks_path": entry["hooks_path"], "modes": modes, "engine": bool(entry["engine"])}

    def _attestation(self, cid):
        entry = self.state["attestations"].get(cid)
        if not self._fresh(entry, self._v(cid)):
            return None
        return {k: entry[k] for k in entry if k != "identity"}

    def _manifest(self, cid):
        entry = self.state["manifests"].get(cid)
        if not self._fresh(entry, self._v(cid)):
            return None
        raw = entry["text"].encode("utf-8")
        valid, detail = _manifest_validity(entry["text"])
        return {"digest": hashlib.sha256(raw).hexdigest(), "valid": valid, "detail": detail}

    def _manifest_attestation(self, cid):
        entry = self.state["manifest_attestations"].get(cid)
        if not self._fresh(entry, self._v(cid)):
            return None
        return {"digest": entry["digest"]}

    def _toplevels(self):
        spec = CheckoutSpec({e.checkout_id: e for e in load_checkout_spec(CHECKOUTS_DIR).by_logical_repo(LOGICAL_REPO)})
        return {cid: entry.target_path for cid, entry in spec.checkouts.items()}

    def _own(self, cid, value):
        """``(own, reason)`` for ``value`` against checkout ``cid``'s simulated top level (never a real path)."""

        top = self._toplevels().get(cid)
        if top is None:
            return False, "checkout not in spec"
        fs = self.state.get("fs") or {}
        symlinks, absent = dict(fs.get("symlinks") or {}), set(fs.get("absent") or ())
        implicit = {top, posixpath.join(top, HOOKS_PATH)} if self._v(cid)["exists"] else set()
        known = (set(fs.get("dirs") or ()) | implicit) - absent
        return hooks_path_is_own(value, top, lambda p: _sim_realpath(symlinks, p), lambda p: p in known)

    def _active(self, cid, h):
        own = self._own(cid, h["hooks_path"])[0] if h["hooks_path"] is not None else False
        return own is True and all(h["modes"].get(n) == "exec" for n in HOOK_NAMES)

    def _stable(self, cid):
        v, h = self._v(cid), self._hooks(cid)
        return {"checkout_id": cid, "origin": v.get("origin"), "hooks_path": h["hooks_path"], "hook_modes": h["modes"]}

    def observe(self, component, scope):
        out = {}
        for cid in self._spec_ids():
            v = self._v(cid)
            if not v["exists"]:
                continue
            h = self._hooks(cid)
            own, detail = self._own(cid, h["hooks_path"]) if h["hooks_path"] is not None else (None, "hooksPath unset")
            out[cid] = {
                "hooks_path": h["hooks_path"],
                "hooks_path_own": own,
                "hooks_path_detail": detail,
                "hook_modes": h["modes"],
                "hooks_active": self._active(cid, h),
                "engine_hooks": h["engine"],
                "attestation": self._attestation(cid),
                "manifest": self._manifest(cid),
                "manifest_attestation": self._manifest_attestation(cid),
                "evidence": dict(self._stable(cid), observed_head=v.get("head")),
            }
        return {"checkouts": out}

    # --- EffectHandler ---------------------------------------------------------

    def preflight(self, op):
        cid, v, h = op.target, self._v(op.target), self._hooks(op.target)
        if not v["exists"]:
            return PreflightResult(False, self.name, "checkout absent")
        if v.get("dirty"):
            return PreflightResult(False, self.name, "checkout is dirty")
        if op.kind == "repo.hooks-activate":
            if h["hooks_path"] not in (None, HOOKS_PATH):
                if self._own(cid, h["hooks_path"])[0]:  # healthy as it stands: a stale plan must not rewrite it
                    return PreflightResult(False, self.name, f"hooksPath already active as {h['hooks_path']}; plan is stale")
                return PreflightResult(False, self.name, f"hooksPath set to {h['hooks_path']}; refusing to overwrite")
            return PreflightResult(True, self.name, "hooksPath unset or already .githooks")
        if op.kind == "pantheon-config.manifest-attest":
            manifest = self._manifest(cid)
            if manifest is None:
                return PreflightResult(False, self.name, "no manifest observed; refusing to attest")
            if not manifest["valid"]:
                return PreflightResult(False, self.name, f"manifest is invalid: {manifest['detail']}")
            if manifest["digest"] != op.desired["digest"]:
                return PreflightResult(False, self.name, "manifest changed since the plan")
            return PreflightResult(True, self.name, "manifest valid and unchanged, checkout clean")
        want = json.loads(op.desired["evidence"])
        if self._stable(cid) != want:
            return PreflightResult(False, self.name, "evidence changed since the plan")
        if not self._active(cid, h):
            return PreflightResult(False, self.name, "hooks are not active; refusing to attest")
        if not v.get("origin"):
            return PreflightResult(False, self.name, "not the managed repo; refusing to attest")
        return PreflightResult(True, self.name, "evidence matches, hooks active, managed, clean")

    def _after_hooks(self, h):
        return {"hooks_path": HOOKS_PATH, "modes": {n: ("missing" if h["modes"].get(n) == "missing" else "exec") for n in HOOK_NAMES}}

    def rollback_intent(self, op):
        cid = op.target
        if op.kind == "pantheon-config.manifest-attest":
            prior = self._manifest_attestation(cid)
            before = {"attestation": prior["digest"] if prior else "none"}
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, op.desired["digest"])
        if op.kind == "repo.hooks-activate":
            h = self._hooks(cid)
            before = {
                "hooks_path": h["hooks_path"] or "unset",
                "modes": _canon(h["modes"]),
                "engine": "true" if h["engine"] else "false",
                "identity": self._v(cid)["installed_sha"] or "",
            }
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, _canon(self._after_hooks(h)))
        prior = self._attestation(cid)
        before = {"attestation": _canon(prior) if prior else "none"}
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, op.desired["evidence"])

    def execute(self, op):
        mutation = self.rollback_intent(op)
        cid, v = op.target, self._v(op.target)
        if op.kind == "pantheon-config.manifest-attest":
            self.state["manifest_attestations"][cid] = {"digest": op.desired["digest"], "identity": v["installed_sha"]}
            return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")
        if op.kind == "repo.hooks-activate":
            h = self._hooks(cid)
            after = self._after_hooks(h)
            self.state["hooks"][cid] = {
                "hooks_path": after["hooks_path"],
                "modes": after["modes"],
                "engine": h["engine"] or h["hooks_path"] is None,
                "identity": v["installed_sha"],
            }
        else:
            record = dict(json.loads(op.desired["evidence"]), observed_head=op.desired.get("observed_head"), basis=op.desired.get("basis"))
            self.state["attestations"][cid] = dict(record, identity=v["installed_sha"])
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        cid, h = op.target, self._hooks(op.target)
        if op.kind == "pantheon-config.manifest-attest":
            recorded, manifest = self._manifest_attestation(cid), self._manifest(cid)
            if recorded is None or recorded["digest"] != op.desired["digest"]:
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, "manifest attestation not recorded as planned")
            ok = manifest is not None and manifest["valid"] and manifest["digest"] == op.desired["digest"]
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "attested manifest holds" if ok else "manifest drifted since the attestation")
        if op.kind == "repo.hooks-activate":
            bad = [n for n in HOOK_NAMES if h["modes"].get(n) in ("noexec", "missing")]
            own, why = self._own(cid, h["hooks_path"]) if h["hooks_path"] is not None else (False, "unset")
            ok = own is True and not bad
            detail = f"hooksPath {h['hooks_path']} is the clone's own .githooks, hooks executable" if ok else f"hooks not active: path {h['hooks_path']} ({why}), not executable: {bad}"
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, detail)
        want = json.loads(op.desired["evidence"])
        rec = self._attestation(cid)
        if rec is None or {k: rec.get(k) for k in self.STABLE} != want:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "attestation not recorded as planned")
        ok = self._stable(cid) == want
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "attested evidence holds" if ok else "evidence drifted since the attestation")

    def can_rollback(self, mutation):
        cid = mutation.target
        if mutation.kind == "pantheon-config.manifest-attest":
            cur = self._manifest_attestation(cid)
            current, prior = cur["digest"] if cur else "none", mutation.before["attestation"]
            if current == prior:
                return PreflightResult(True, "not-applied", "manifest attestation at preimage")
            owned = current == mutation.after_fingerprint
            return PreflightResult(owned, "owned" if owned else "drifted", "own manifest attestation" if owned else "manifest attestation changed")
        if mutation.kind == "repo.hooks-activate":
            h = self._hooks(cid)
            before = {"hooks_path": None if mutation.before["hooks_path"] == "unset" else mutation.before["hooks_path"], "modes": json.loads(mutation.before["modes"])}
            if h["hooks_path"] == before["hooks_path"] and h["modes"] == before["modes"]:
                return PreflightResult(True, "not-applied", "hooks at preimage")
            after = json.loads(mutation.after_fingerprint)
            owned = h["hooks_path"] == after["hooks_path"] and h["modes"] == after["modes"] and before["hooks_path"] is None and h["engine"]
            return PreflightResult(owned, "owned" if owned else "drifted", "own hooks activation" if owned else "hooks changed since the activation, or not ours")
        cur, prior = self._attestation(cid), mutation.before["attestation"]
        if (_canon(cur) if cur else "none") == prior:
            return PreflightResult(True, "not-applied", "attestation at preimage")
        owned = cur is not None and _canon({k: cur.get(k) for k in self.STABLE}) == mutation.after_fingerprint
        return PreflightResult(owned, "owned" if owned else "drifted", "own attestation" if owned else "attestation changed")

    def rollback_mutation(self, mutation):
        verdict = self.can_rollback(mutation)
        if verdict.classification == "drifted":
            raise OperationFailed(f"{mutation.kind} rollback refused (drifted): {verdict.detail}")
        cid = mutation.target
        if mutation.kind == "pantheon-config.manifest-attest":
            if mutation.before["attestation"] == "none":
                self.state["manifest_attestations"].pop(cid, None)
            else:
                self.state["manifest_attestations"][cid] = {"digest": mutation.before["attestation"], "identity": self._v(cid)["installed_sha"]}
            return
        if mutation.kind == "repo.hooks-activate":
            restored = {
                "hooks_path": None if mutation.before["hooks_path"] == "unset" else mutation.before["hooks_path"],
                "modes": json.loads(mutation.before["modes"]),
                "engine": mutation.before["engine"] == "true",
            }
            if restored == _default_hooks():
                self.state["hooks"].pop(cid, None)  # back to the default entry: leave nothing to inherit
            else:
                self.state["hooks"][cid] = dict(restored, identity=mutation.before["identity"] or None)
        elif mutation.before["attestation"] == "none":
            self.state["attestations"].pop(cid, None)
        else:
            self.state["attestations"][cid] = dict(json.loads(mutation.before["attestation"]), identity=self._v(cid)["installed_sha"])

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_local_state(), **copy.deepcopy(dict(state)))


_REPO = PantheonConfigRepoSim()


def _repo_view(cid):
    ck = _REPO.state["checkouts"].get(cid)
    remote = _REPO.state["remote"]
    managed = ck is not None and _norm(ck.get("origin")) == _norm(remote["url"])
    return {
        "exists": ck is not None,
        "installed_sha": (ck or {}).get("installed_sha"),
        "head": ck["head"] if managed else None,
        "dirty": bool(managed and ck.get("dirty")),
        "origin": _norm(remote["url"]) if managed else None,
    }


_LOCAL = PantheonConfigLocalSim(_repo_view)
HANDLERS = [_REPO, _LOCAL]


# --- T-5107 PR3: the org.pantheon.pantheon-config/v1 invariant list (PC-1..PC-6), evaluated by VERIFY -----------------

#: Mirrors of tools/fleet_clone_currency_check.py's verdict names and threshold; pinned by tests, never imported.
CURRENT, DRIFTED, OFF_MAIN = "CURRENT", "DRIFTED", "OFF_MAIN"
DRIFT_THRESHOLD = 20
PENDING_PUSH_MAX_AGE_S = 86400  # 24h: ahead of the target is a breach only when the oldest unpushed commit is older than this
_FAILED, _DEGRADED = "failed", "degraded"
_STABLE = ("checkout_id", "origin", "hooks_path", "hook_modes")
_PC1, _PC2, _PC3, _PC4, _PC5, _PC6 = (
    "PC-1-checkout-managed", "PC-2-tree-clean", "PC-3-hooks-active", "PC-4-currency", "PC-5-manifest-valid", "PC-6-attestations-current",
)
_PASS_DETAIL = {
    _PC1: "every observed checkout exists and is managed",
    _PC2: "every managed working tree is clean",
    _PC3: f"core.hooksPath resolves to each clone's own {HOOKS_PATH} and {', '.join(HOOK_NAMES)} are executable on every checkout",
    _PC4: f"every managed checkout is current ({CURRENT}: at most {DRIFT_THRESHOLD} commits behind, on its tracking branch, no unpushed commit older than {PENDING_PUSH_MAX_AGE_S // 3600}h)",
    _PC5: "every checkout has a valid instance.yaml manifest with its role_membership_projection block",
    _PC6: "recorded attestations match the current evidence (or none is recorded)",
}
_CURRENCY_FACTS = ("remote_reachable", "head_sha", "desired_target", "behind", "ahead", "desired_resolvable")


def _str(value):
    return isinstance(value, str) and bool(value)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _currency_finding(cid, raw):
    """PC-4 for a managed checkout, over the repo handler's raw observation: at most one finding."""

    bad = [
        k for k in _CURRENCY_FACTS
        if k not in raw
        or (k == "remote_reachable" or k == "desired_resolvable") and not isinstance(raw[k], bool)
        or k == "head_sha" and not _str(raw[k])
        or k == "desired_target" and raw[k] is not None and not isinstance(raw[k], str)
        or k in ("behind", "ahead") and not _count(raw[k])
    ]
    if bad:
        return (_PC4, _FAILED, f"{cid}: currency facts missing or mistyped: {', '.join(bad)}")
    if raw["remote_reachable"] is not True:
        return (_PC4, _DEGRADED, f"{cid}: remote unreachable (could not evaluate)")
    target = raw["desired_target"]
    if not target:
        return (_PC4, _DEGRADED, f"{cid}: no target resolved (could not evaluate)")
    if "tracking" not in raw or "branch" not in raw:
        return (_PC4, _DEGRADED, f"{cid}: branch not observed (could not evaluate)")
    tracking, branch = raw["tracking"], raw["branch"]
    if tracking is not None and not _str(tracking):
        return (_PC4, _DEGRADED, f"{cid}: tracking not observed (could not evaluate)")
    if tracking is not None:  # tracking mode; tracking None is pinned mode, which has no branch rule
        if not _str(branch):
            return (_PC4, _DEGRADED, f"{cid}: branch not observed (could not evaluate)")
        if branch == "HEAD":
            return (_PC4, _DEGRADED, f"{cid}: {OFF_MAIN}: detached HEAD, expected {tracking}")
        if branch != tracking:
            return (_PC4, _DEGRADED, f"{cid}: {OFF_MAIN}: on branch {branch} not {tracking}")
    at_target = raw["head_sha"] == target
    if tracking is None:  # pinned mode: current only at the pin
        return None if at_target else (_PC4, _DEGRADED, f"{cid}: head is not the pinned target")
    if not at_target and raw["desired_resolvable"] is not True:
        return (_PC4, _DEGRADED, f"{cid}: ancestry to the target not resolvable (could not evaluate)")
    if raw["behind"] > DRIFT_THRESHOLD:
        return (_PC4, _DEGRADED, f"{cid}: {DRIFTED}: {raw['behind']} commits behind (threshold {DRIFT_THRESHOLD})")
    if raw["ahead"] > 0:  # tracking mode: unpushed work is fine until it is old (the monitor's PENDING_PUSH)
        ts, now = raw.get("oldest_unpushed_commit_ts"), raw.get("observed_at")
        if not (_count(ts) and _count(now) and now >= ts):
            return (_PC4, _DEGRADED, f"{cid}: PENDING_PUSH age not observed (could not evaluate)")
        age = now - ts
        if age > PENDING_PUSH_MAX_AGE_S:
            return (_PC4, _DEGRADED, f"{cid}: PENDING_PUSH: oldest unpushed commit {age // 3600}h old (threshold {PENDING_PUSH_MAX_AGE_S // 3600}h)")
    return None


def _hooks_findings(cid, local):
    path, modes, flag, own = local.get("hooks_path"), local.get("hook_modes"), local.get("hooks_active"), local.get("hooks_path_own")
    out = []
    if not _str(path):
        out.append(f"core.hooksPath is {path!r}, expected this clone's own {HOOKS_PATH}")
    elif own is False:  # the handler resolved it (realpath); the evaluator trusts that, it has no lexical rule
        out.append(f"core.hooksPath {path} is not this clone's own {HOOKS_PATH} ({local.get('hooks_path_detail')})")
    elif own is not True:
        out.append(f"core.hooksPath {path} ownership not observed (expected this clone's own {HOOKS_PATH})")
    bad = [n for n in HOOK_NAMES if not isinstance(modes, dict) or modes.get(n) != "exec"]
    if bad:
        out.append(f"hooks not executable or not observed: {', '.join(bad)}")
    computed = _str(path) and own is True and not bad
    if flag is not computed:  # `is`: a non-bool flag is a lie, never a pass
        out.append(f"hooks_active is {flag!r} but path and modes say {computed}")
    return [(_PC3, _FAILED, f"{cid}: {m}") for m in out]


def _manifest_finding(cid, local):
    if "manifest" not in local:
        return (_PC5, _FAILED, f"{cid}: manifest not observed (could not evaluate)")
    manifest = local["manifest"]
    if manifest is None:
        return (_PC5, _FAILED, f"{cid}: instance.yaml manifest absent")
    if not isinstance(manifest, dict) or manifest.get("valid") is not True:
        detail = manifest.get("detail") if isinstance(manifest, dict) else "malformed manifest observation"
        return (_PC5, _FAILED, f"{cid}: instance.yaml manifest invalid: {detail}")
    return None


def _attestation_findings(cid, local):
    out = []
    missing = [k for k in ("attestation", "manifest_attestation") if k not in local]
    if missing:
        return [(_PC6, _DEGRADED, f"{cid}: attestation facts not observed (could not evaluate): {', '.join(missing)}")]  # unknown is not no
    attested, recorded = local["attestation"], local["manifest_attestation"]
    evidence = local.get("evidence")
    if attested is not None:
        if not isinstance(attested, dict):
            out.append((_PC6, _FAILED, f"{cid}: recorded adopt attestation is not an object"))
        elif not isinstance(evidence, dict):
            out.append((_PC6, _FAILED, f"{cid}: current evidence is not an object while an attestation is recorded"))
        else:
            drifted = [k for k in _STABLE if k not in attested or attested[k] != evidence.get(k)]
            if drifted:
                out.append((_PC6, _DEGRADED, f"{cid}: drift from the attested evidence: {', '.join(drifted)}"))
    if recorded is not None:
        manifest = local.get("manifest")
        if not isinstance(recorded, dict) or not _str(recorded.get("digest")):
            out.append((_PC6, _FAILED, f"{cid}: recorded manifest attestation is not an object with a digest"))
        elif not isinstance(manifest, dict) or manifest.get("digest") != recorded["digest"]:
            out.append((_PC6, _DEGRADED, f"{cid}: drift from the attested manifest: manifest.digest"))
    return out


def _findings(repo, local):
    """(invariant id, severity, message) per breach, per observed checkout, over the observation only."""

    out = []
    if not repo:
        out.append((_PC1, _FAILED, "no checkout observed"))
    for cid in sorted(repo):
        raw = repo[cid]
        if not isinstance(raw, dict):
            out.append((_PC1, _FAILED, f"{cid}: malformed checkout observation"))
            continue
        if raw.get("exists") is not True:
            out.append((_PC1, _FAILED, f"{cid}: checkout absent"))
            continue
        managed = raw.get("is_managed") is True
        if not managed:
            out.append((_PC1, _FAILED, f"{cid}: origin is not the managed remote"))
        else:
            if raw.get("dirty") is not False:
                out.append((_PC2, _FAILED, f"{cid}: working tree is dirty" if raw.get("dirty") is True else f"{cid}: dirty fact is not a bool"))
            finding = _currency_finding(cid, raw)
            if finding:
                out.append(finding)
        entry = local.get(cid)
        if not isinstance(entry, dict):
            note = f"{cid}: no local observation (could not evaluate)"
            out.extend([(_PC3, _DEGRADED, note), (_PC5, _DEGRADED, note), (_PC6, _DEGRADED, note)])  # unknown is not no
            continue
        out.extend(_hooks_findings(cid, entry))
        finding = _manifest_finding(cid, entry)
        if finding:
            out.append(finding)
        out.extend(_attestation_findings(cid, entry))
    return out


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """Evaluate org.pantheon.pantheon-config/v1 (PC-1..PC-6) over the observed state of both handlers.

    Any FAILED finding makes the state unhealthy; DEGRADED-only findings make it degraded. An observation
    that cannot be asked raises (the engine turns that into an alarm, never a pass)."""

    repo = observed.get(PantheonConfigRepoSim.name) if isinstance(observed, dict) else None
    local = observed.get(PantheonConfigLocalSim.name) if isinstance(observed, dict) else None
    repo_cks = repo.get("checkouts") if isinstance(repo, dict) else None
    if not isinstance(repo_cks, dict):
        raise ValueError("no pantheon-config observation: could not ask")
    local_cks = local.get("checkouts") if isinstance(local, dict) else None
    findings = _findings(repo_cks, local_cks if isinstance(local_cks, dict) else {})
    results, severities = [], set()
    for entry in list_doc.get("invariants") or []:
        iid = entry.get("id") if isinstance(entry, dict) else None
        if iid not in _PASS_DETAIL:
            results.append({"id": str(iid), "status": "alarm", "detail": "no evaluator for this invariant"})
            severities.add(_FAILED)
            continue
        mine = [(sev, msg) for fid, sev, msg in findings if fid == iid]
        severities.update(sev for sev, _m in mine)
        if mine:
            results.append({"id": iid, "status": "fail", "detail": "; ".join(m for _s, m in mine)})
        else:
            results.append({"id": iid, "status": "pass", "detail": _PASS_DETAIL[iid]})
    state = "unhealthy" if _FAILED in severities else "degraded" if severities else "ready"
    return {"results": results, "state": state}
