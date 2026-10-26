"""pantheon-config component: the REAL git-backed EffectHandler (ORG-080 §2; T-5133).

DARK. Nothing selects this handler: ``pantheon-config_handlers.py`` ``HANDLERS``
holds only the simulated ``PantheonConfigRepoSim``, and no engine module imports
this file. The only way in is explicit construction plus
``SimulatedHostBackend(root, handlers=(handler,))``, which today is test-only.
Two locks keep it off every real checkout even when constructed:

1. ``LIVE_ACTUATION_ENABLED`` is the literal ``False`` (no env var, no flag, the
   T-5084 ``host_substrate.ports`` shape). Without a ``sandbox_root`` the
   constructor raises ``LiveActuationDisabled``. Live cutover is T-3936; fleet
   pull fan-out is T-3442.
2. With a ``sandbox_root``, that root is vetted by the backend's own
   ``guard_sim_root`` (refuses production prefixes, seat-scope state trees, any
   path inside a git checkout, foreign ownership, wrong mode, non-empty without
   the sentinel, and root), and every checkout ``target_path`` must resolve
   strictly under it, checked at construction and again before every mutation.
   The reconciler's ``functional_probe`` is that check.

Import trust (the T-5084 reviewer note) is CARRIED FORWARD as T-5477: handler
modules are not digest-pinned (``contract_digest`` covers only the contract
YAML). Because this module is never in ``HANDLERS`` and never imported at
discover time, it adds no unpinned code to the discover path; T-5477 must land
before any real handler is registered (it informs T-3936).

Delegation (``tools/repo_reconciler.py`` is used UNCHANGED):

* ``observe()`` wraps ``GitCheckoutProbe`` through ``_TipRecordingProbe``, which
  keeps the refs of the probe's own single ``ls-remote`` so the desired-tip sha
  comes from the same remote snapshot as the observation.
* ``execute()`` builds a one-entry spec whose entry is pinned to the op's target
  sha and calls ``RepoReconciler(...).reconcile_plan()`` then ``.apply(plan)``.
  ``apply`` signals failure by omission (no ``installs`` / ``advances`` entry);
  this handler turns that into ``OperationFailed``.
* ``repo.reclaim`` calls ``RepoReconciler.remove`` with a receipt rebuilt from
  this handler's persisted install record (identity, dirty, untracked and HEAD
  re-checks are the reconciler's own).

Rollback:

* ff-advance: the engine rewinds ONLY its own receipted advance, and only while
  HEAD equals the receipted sha on the receipted symbolic ref (branch, or
  detached), the tree is clean (untracked files count as dirty) and the
  preimage is an ancestor of HEAD. After re-checking those conditions the
  rewind is a compare-and-swap ``update-ref`` (old value = the receipted sha)
  followed by a two-tree ``read-tree -m -u`` that refuses on local changes.
  Anything else is refused as drifted. (The reconciler has no rewind; its
  "never rewind" rule is about divergence and foreign state, which stays intact.)

Repo redirection: any repo-redirecting git variable in the environment
(``REPO_REDIRECT_ENV``: GIT_DIR, GIT_WORK_TREE, ...) closes the dark gate, because
the reconciler's git subprocesses inherit it and ``git -C`` would follow it to
another repo. The handler's own git calls also strip those variables.
* clone: ``RepoReconciler.rollback`` over the install record.
* reclaim: NOT generally reversible. Rollback is best-effort: it re-clones only
  while the remote still advertises the receipted sha as a tip; otherwise it is
  refused as drifted (ROLLBACK_INCOMPLETE), never a wrong-commit restore.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from tools.checkout_spec import CheckoutEntry, CheckoutMode, CheckoutSpec
from tools.germinate.engine.backend import guard_sim_root
from tools.persona_installer import MutationRecord, OperationEvidence, OperationFailed, PreflightResult
from tools.repo_reconciler import (
    ACTION_CLONE,
    ACTION_FF_ADVANCE,
    ACTION_NOOP,
    STATE_CURRENT,
    ApplyReceipt,
    CheckoutObservation,
    GitCheckoutProbe,
    RepoReconciler,
)

LIVE_ACTUATION_ENABLED: Final = False

HANDLER_NAME = "pantheon-config-repo"  # the planner keys observations by this name
LOGICAL_REPO = "pantheon-config"
KINDS = frozenset({"repo.clone", "repo.ff-advance", "repo.reclaim"})
_OBS_FIELDS = tuple(f.name for f in dataclasses.fields(CheckoutObservation))

RemoteResolver = Callable[[str], str]
HealthProbe = Callable[[CheckoutEntry], bool]


#: Environment variables that make git act on a repository other than the one
#: ``git -C <path>`` names (or rewrite its config, objects or refs). This is git's
#: own ``git rev-parse --local-env-vars`` list (the set git itself clears before
#: entering another repo), plus GIT_NAMESPACE (ref namespacing). A test pins it as
#: a superset of the installed git's list. GIT_CEILING_DIRECTORIES is NOT here: it
#: only bounds upward discovery, and every call names the checkout root.
REPO_REDIRECT_ENV: Final = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


def redirecting_env() -> list[str]:
    """The repo-redirecting git variables set in this process's environment."""

    return sorted(k for k in REPO_REDIRECT_ENV if k in os.environ)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in REPO_REDIRECT_ENV}


class LiveActuationDisabled(RuntimeError):
    """The real pantheon-config handler is dark in this tranche (T-5133; cutover T-3936)."""


class SandboxRefused(LiveActuationDisabled):
    """A checkout target lies outside the vetted sandbox root, or the git
    environment could redirect git away from it. A dark-gate refusal."""


def _norm(url: str | None) -> str:
    return (url or "").rstrip("/").removesuffix(".git")


class _TipRecordingProbe(GitCheckoutProbe):
    """GitCheckoutProbe that keeps its own ``ls-remote`` refs, so the desired-tip
    sha is derived from the same remote snapshot the observation used."""

    def __init__(self, *, timeout: int = 30) -> None:
        super().__init__(timeout=timeout)
        self.last_refs: dict[str, str] | None = None

    def _ls_remote(self, remote_url: str) -> dict[str, str] | None:
        refs = super()._ls_remote(remote_url)
        self.last_refs = refs
        return refs

    def observe_with_tip(self, entry: CheckoutEntry, remote_url: str) -> tuple[CheckoutObservation, str | None]:
        self.last_refs = None
        obs = self.observe(entry, remote_url)
        if entry.mode.is_pinned:
            return obs, entry.mode.pinned
        refs = self.last_refs
        return obs, (refs.get(f"refs/heads/{entry.mode.tracking}") if refs is not None else None)


class PantheonConfigRepoGit:
    """Real git EffectHandler for repo.clone / repo.ff-advance / repo.reclaim (dark)."""

    name = HANDLER_NAME
    kinds = KINDS

    def __init__(
        self,
        spec: CheckoutSpec,
        *,
        remote_resolver: RemoteResolver,
        sandbox_root: Path | None = None,
        health_probe: HealthProbe | None = None,
        timeout: int = 30,
    ) -> None:
        if sandbox_root is None:
            if LIVE_ACTUATION_ENABLED is not True:
                raise LiveActuationDisabled(
                    "the real pantheon-config handler is dark (T-5133); it runs only inside a vetted "
                    "sandbox_root. Live cutover is T-3936 and needs handler import trust (T-5477) first"
                )
            raise LiveActuationDisabled("live mode is not built in this tranche (T-3936)")  # pragma: no cover
        self._sandbox = guard_sim_root(Path(sandbox_root))
        self._spec = CheckoutSpec({e.checkout_id: e for e in spec.by_logical_repo(LOGICAL_REPO)})
        for entry in self._spec.checkouts.values():
            self._require_sandboxed(entry)
        self._resolver = remote_resolver
        self._health = health_probe or (lambda _entry: True)
        self._timeout = timeout
        self.installs: dict[str, dict[str, str]] = {}  # checkout_id -> {path, installed_sha}

    # --- the dark gate ------------------------------------------------------

    def _sandboxed(self, entry: CheckoutEntry) -> bool:
        """The dark gate (also the reconciler's functional_probe). False when the
        target resolves outside the sandbox, OR when a repo-redirecting git
        variable is set: the reconciler's own git subprocesses inherit this
        environment, so ``git -C <sandboxed path>`` could act on another repo."""

        if redirecting_env():
            return False
        resolved = os.path.realpath(entry.target_path)
        return resolved.startswith(str(self._sandbox) + os.sep)

    def _require_sandboxed(self, entry: CheckoutEntry) -> None:
        redirect = redirecting_env()
        if redirect:
            raise SandboxRefused(f"{entry.checkout_id}: repo-redirecting git environment set ({', '.join(redirect)}); refusing")
        if not self._sandboxed(entry):
            raise SandboxRefused(f"{entry.checkout_id}: target {entry.target_path!r} is outside sandbox {self._sandbox}")

    def _gate(self, entry: CheckoutEntry) -> Callable[[], bool]:
        return lambda: self._sandboxed(entry)

    # --- helpers --------------------------------------------------------------

    def _entry(self, checkout_id: str) -> CheckoutEntry:
        entry = self._spec.checkouts.get(checkout_id)
        if entry is None:
            raise OperationFailed(f"{checkout_id}: not a {LOGICAL_REPO} checkout in the spec")
        self._require_sandboxed(entry)
        return entry

    def _url(self, entry: CheckoutEntry) -> str:
        try:
            return self._resolver(entry.logical_repo) or ""
        except Exception:  # noqa: BLE001 -- mirrors the reconciler: an unresolved source is ""
            return ""

    def _git(self, path: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, check=False, timeout=self._timeout, env=_clean_env()
        )

    def _head(self, path: str) -> str | None:
        res = self._git(path, "rev-parse", "HEAD")
        return res.stdout.strip() if res.returncode == 0 else None

    def _symref(self, path: str) -> str:
        """The branch HEAD points at (``refs/heads/<b>``), or ``""`` when detached."""

        res = self._git(path, "symbolic-ref", "-q", "HEAD")
        return res.stdout.strip() if res.returncode == 0 else ""

    def _clean(self, path: str) -> bool:
        res = self._git(path, "status", "--porcelain", "--untracked-files=all")
        return res.returncode == 0 and res.stdout.strip() == ""

    def _is_ancestor(self, path: str, older: str, newer: str) -> bool:
        return self._git(path, "merge-base", "--is-ancestor", older, newer).returncode == 0

    def _remote_tips(self, url: str) -> set[str] | None:
        res = subprocess.run(
            ["git", "ls-remote", url], capture_output=True, text=True, check=False, timeout=self._timeout, env=_clean_env()
        )
        if res.returncode != 0:
            return None
        return {line.split("\t")[0] for line in res.stdout.splitlines() if "\t" in line}

    def _raw(self, entry: CheckoutEntry) -> dict[str, Any]:
        url = self._url(entry)
        probe = _TipRecordingProbe(timeout=self._timeout)
        obs, target = probe.observe_with_tip(entry, url)
        raw: dict[str, Any] = {name: getattr(obs, name) for name in _OBS_FIELDS}
        record = self.installs.get(entry.checkout_id)
        raw["desired_target"] = target
        raw["health_ok"] = bool(self._health(entry)) if obs.head_sha else False
        raw["installed_sha"] = record["installed_sha"] if record else None
        return raw

    def _reconciler(self, entry: CheckoutEntry) -> RepoReconciler:
        return RepoReconciler(
            CheckoutSpec({entry.checkout_id: entry}),
            probe=GitCheckoutProbe(timeout=self._timeout),
            remote_resolver=lambda _r: self._url(entry),
            health_probe=self._health,
            functional_probe=self._gate(entry),
        )

    @staticmethod
    def _pinned(entry: CheckoutEntry, sha: str) -> CheckoutEntry:
        return dataclasses.replace(entry, mode=CheckoutMode(pinned=sha))

    def _receipt_for(self, entry: CheckoutEntry, installed_sha: str) -> ApplyReceipt:
        return ApplyReceipt(
            materialized=True,
            installs=[entry.checkout_id],
            install_paths={entry.checkout_id: entry.target_path},
            installed_sha={entry.checkout_id: installed_sha},
        )

    def _apply_one(self, entry: CheckoutEntry, sha: str, expected_action: str) -> tuple[CheckoutEntry, ApplyReceipt]:
        """reconcile_plan() + apply() over a one-entry spec pinned to ``sha``."""

        if not self._sandboxed(entry):
            raise LiveActuationDisabled(f"{entry.checkout_id}: refusing to actuate outside the sandbox")
        pinned = self._pinned(entry, sha)
        reconciler = self._reconciler(pinned)
        plan = reconciler.reconcile_plan()
        [item] = plan.items
        if item.refusal is not None or item.action != expected_action:
            raise OperationFailed(
                f"{entry.checkout_id}: repo_reconciler plans {item.action} ({item.state} {item.detail}), not {expected_action}".strip()
            )
        return pinned, reconciler.apply(plan)

    # --- EffectHandler: observe ------------------------------------------------

    def observe(self, component: str, scope: str) -> Mapping[str, Any]:
        entries = [self._spec.checkouts[cid] for cid in sorted(self._spec.checkouts)]
        for entry in entries:  # the reconciler probe inherits the environment: never observe a decoy
            self._require_sandboxed(entry)
        return {
            "remote_url": self._url(entries[0]) if entries else "",
            "checkouts": {e.checkout_id: self._raw(e) for e in entries},
        }

    # --- preflight (read-only) ---------------------------------------------------

    def preflight(self, op) -> PreflightResult:
        try:
            entry = self._entry(op.target)
        except (OperationFailed, SandboxRefused) as exc:
            return PreflightResult(False, self.name, str(exc))
        raw = self._raw(entry)
        want = op.desired.get("head")
        if not raw["remote_reachable"]:
            return PreflightResult(False, self.name, "remote unreachable")
        if op.kind == "repo.clone":
            ok = not raw["exists"] and raw["desired_target"] == want
            return PreflightResult(ok, self.name, "absent, remote tip is the target" if ok else "target present or remote tip is not the target")
        if not raw["is_managed"] or not self._clean(entry.target_path):
            return PreflightResult(False, self.name, "checkout absent, foreign or dirty")
        if op.kind == "repo.ff-advance":
            if raw["head_sha"] != op.precondition.get("head"):
                return PreflightResult(False, self.name, "HEAD moved off the precondition")
            if raw["desired_resolvable"] and raw["ahead"] > 0:
                return PreflightResult(False, self.name, "not a fast-forward (local commits not in the target)")
            if self._git(entry.target_path, "cat-file", "-e", f"{want}^{{commit}}").returncode == 0 and not self._is_ancestor(
                entry.target_path, raw["head_sha"], want
            ):
                return PreflightResult(False, self.name, "not a fast-forward")
            return PreflightResult(True, self.name, "fast-forward available or deferred to merge --ff-only")
        record = self.installs.get(op.target)
        ok = record is not None and record["installed_sha"] == op.precondition.get("head") == raw["head_sha"]
        return PreflightResult(ok, self.name, "pristine engine-installed clone" if ok else "not an engine-installed pristine clone")

    # --- write-ahead inverse ------------------------------------------------------

    def rollback_intent(self, op) -> MutationRecord:
        entry = self._entry(op.target)
        if op.kind == "repo.clone":
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"exists": "false"}, op.desired["head"])
        if op.kind == "repo.ff-advance":
            head = self._head(entry.target_path) or ""
            before = {"head": head, "ref": self._symref(entry.target_path)}
            return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, op.desired["head"])
        record = self.installs.get(op.target) or {}
        before = {"head": record.get("installed_sha", ""), "path": entry.target_path}
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, "absent")

    # --- execute -----------------------------------------------------------------

    def execute(self, op) -> tuple[MutationRecord, OperationEvidence]:
        mutation = self.rollback_intent(op)
        entry = self._entry(op.target)
        if op.kind == "repo.clone":
            self._clone(entry, op.desired["head"])
        elif op.kind == "repo.ff-advance":
            _pinned, receipt = self._apply_one(entry, op.desired["head"], ACTION_FF_ADVANCE)
            if entry.checkout_id not in receipt.advances:
                raise OperationFailed(f"{entry.checkout_id}: fetch or merge --ff-only refused; HEAD unchanged")
        else:
            record = self.installs.get(op.target)
            if record is None:
                raise OperationFailed(f"{op.target}: not engine-installed; refusing to reap unknown data")
            refusal = self._reconciler(entry).remove(entry.checkout_id, self._receipt_for(entry, record["installed_sha"]))
            if refusal is not None:
                raise OperationFailed(f"repo_reconciler remove refused: {refusal.as_text()}")
            if Path(entry.target_path).exists():
                raise OperationFailed(f"{op.target}: still present after remove")
            del self.installs[op.target]
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"git {op.kind} via repo_reconciler")

    def _clone(self, entry: CheckoutEntry, sha: str) -> None:
        pinned, receipt = self._apply_one(entry, sha, ACTION_CLONE)
        if entry.checkout_id not in receipt.installs:
            raise OperationFailed(f"{entry.checkout_id}: clone failed")
        if receipt.installed_sha.get(entry.checkout_id) != sha:
            # The reconciler's clone reaches a pin only when it is the default-branch tip.
            # Reclaim our own pristine clone at once so the failed op leaves nothing behind.
            refusals = self._reconciler(pinned).rollback(receipt)
            raise OperationFailed(
                f"{entry.checkout_id}: clone landed on {receipt.installed_sha.get(entry.checkout_id)}, not {sha}; "
                + ("reclaimed" if not refusals else "reclaim refused: " + "; ".join(r.as_text() for r in refusals))
            )
        self.installs[entry.checkout_id] = {"path": entry.target_path, "installed_sha": sha}

    # --- verify ------------------------------------------------------------------

    def verify_operation(self, op) -> OperationEvidence:
        entry = self._spec.checkouts.get(op.target)
        if entry is None:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "checkout not in spec")
        if not self._sandboxed(entry):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "refused: outside the sandbox or git environment redirected")
        if op.kind == "repo.reclaim":
            ok = not Path(entry.target_path).exists()
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "reclaimed" if ok else "still present")
        [item] = self._reconciler(self._pinned(entry, op.desired["head"])).verify().items
        ok = item.state == STATE_CURRENT and item.action == ACTION_NOOP and item.refusal is None
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, f"repo_reconciler verify: {item.state} {item.detail}".strip())

    # --- rollback ------------------------------------------------------------------

    def _ff_owned(self, entry: CheckoutEntry, mutation: MutationRecord) -> tuple[bool, str]:
        path = entry.target_path
        url = self._url(entry)
        origin = self._git(path, "remote", "get-url", "origin") if Path(path).is_dir() else None
        if origin is None or origin.returncode != 0 or _norm(origin.stdout.strip()) != _norm(url):
            return False, "checkout vanished or is no longer the managed repo"
        head = self._head(path)
        if head != mutation.after_fingerprint:
            return False, f"HEAD {head} is not the receipted {mutation.after_fingerprint}"
        ref = self._symref(path)
        if "ref" not in mutation.before or ref != mutation.before["ref"]:
            return False, f"HEAD now points at {ref or 'a detached HEAD'!r}, not the receipted {mutation.before.get('ref')!r}"
        if not self._clean(path):
            return False, "working tree is dirty or holds untracked files"
        if not self._is_ancestor(path, mutation.before["head"], head):
            return False, "preimage is not an ancestor of HEAD"
        return True, "own receipted, clean advance"

    def _rewind(self, path: str, mutation: MutationRecord) -> None:
        """Compare-and-swap the receipted ref back to the preimage, then make the
        index and worktree follow with a two-tree ``read-tree -m -u``.

        * ``update-ref <ref> <before> <receipted>`` moves the ref only if it still
          holds the receipted sha, atomically: a commit that landed after the
          ownership check makes the rewind refuse instead of dropping that commit.
          A detached HEAD is swapped with ``--no-deref HEAD``.
        * ``read-tree -m -u <receipted> <before>`` carries index and worktree from
          the receipted tree to the preimage and refuses on any path with local
          changes (the ``reset --keep`` contract). ``reset --keep`` cannot be used
          after the swap: HEAD already equals the preimage, so it would update
          nothing. If the follow step refuses, the ref is swapped back.
        """

        before, receipted, ref = mutation.before["head"], mutation.after_fingerprint, mutation.before["ref"]
        target = ref or "HEAD"
        cas = ["update-ref", "-m", "germinate: roll back own receipted ff-advance", *([] if ref else ["--no-deref"])]
        swap = self._git(path, *cas, target, before, receipted)
        if swap.returncode != 0:
            raise OperationFailed(f"rewind refused: {target} is no longer the receipted {receipted}: {swap.stderr.strip()}")
        self._git(path, "update-index", "-q", "--refresh")
        follow = self._git(path, "read-tree", "-m", "-u", receipted, before)
        if follow.returncode != 0:
            back = self._git(path, *cas, target, receipted, before)
            restored = "ref restored" if back.returncode == 0 else f"REF RESTORE FAILED: {back.stderr.strip()}"
            raise OperationFailed(f"rewind refused: worktree has local changes ({follow.stderr.strip()}); {restored}")
        if self._head(path) != before or not self._clean(path):
            raise OperationFailed(f"rewind to {before} did not converge")

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        entry = self._spec.checkouts.get(mutation.target)
        if entry is None:
            return PreflightResult(False, "drifted", "checkout not in spec")
        if not self._sandboxed(entry):
            return PreflightResult(False, "drifted", "refused: outside the sandbox or git environment redirected")
        path = entry.target_path
        present = Path(path).exists()
        if mutation.kind == "repo.clone":
            if not present:
                return PreflightResult(True, "not-applied", "never cloned")
            owned = self._head(path) == mutation.after_fingerprint and self._clean(path)
            return PreflightResult(owned, "owned" if owned else "drifted", "pristine clone" if owned else "clone moved or dirty")
        if mutation.kind == "repo.reclaim":
            if present:
                same = self._head(path) == mutation.before.get("head") and self._clean(path)
                return PreflightResult(same, "not-applied" if same else "drifted", "never reclaimed" if same else "path changed")
            tips = self._remote_tips(self._url(entry))
            ok = tips is not None and mutation.before.get("head") in tips
            return PreflightResult(
                ok, "owned" if ok else "drifted", "remote still advertises the receipted sha" if ok else "receipted sha is no longer a remote tip"
            )
        if present and self._head(path) == mutation.before.get("head") and self._clean(path):
            # Clean is required: a kill between the rewind's ref swap and its
            # read-tree leaves HEAD at the preimage with the receipted tree checked
            # out. That falls through to _ff_owned and reads as drifted, never as a
            # false not-applied (T-5133 r2).
            return PreflightResult(True, "not-applied", "HEAD still at preimage, tree clean")
        owned, detail = self._ff_owned(entry, mutation)
        return PreflightResult(owned, "owned" if owned else "drifted", detail)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        entry = self._entry(mutation.target)
        if not self._sandboxed(entry):  # pragma: no cover - _entry already refuses
            raise LiveActuationDisabled("refusing to roll back outside the sandbox")
        if mutation.kind == "repo.clone":
            refusals = self._reconciler(self._pinned(entry, mutation.after_fingerprint)).rollback(
                self._receipt_for(entry, mutation.after_fingerprint)
            )
            if refusals or Path(entry.target_path).exists():
                raise OperationFailed("clone rollback refused: " + "; ".join(r.as_text() for r in refusals))
            self.installs.pop(entry.checkout_id, None)
        elif mutation.kind == "repo.ff-advance":
            owned, detail = self._ff_owned(entry, mutation)  # re-check right before the rewind
            if not owned:
                raise OperationFailed(f"ff-advance rollback refused (drifted): {detail}")
            self._rewind(entry.target_path, mutation)
        else:
            sha = mutation.before["head"]
            self._clone(entry, sha)  # raises (and self-reclaims) on a wrong-commit clone

    # --- persistence (install records only) -----------------------------------------

    def dump_state(self) -> Mapping[str, Any]:
        return {"installs": copy.deepcopy(self.installs)}

    def load_state(self, state: Mapping[str, Any]) -> None:
        self.installs = {k: dict(v) for k, v in (state.get("installs") or {}).items()}
