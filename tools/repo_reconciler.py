"""tools/repo_reconciler.py — T-2951: the declarative repository-lifecycle reconciler.

WHAT THIS OWNS (ORG-053 §6 cradle-to-grave; ORG-080 plan/apply/verify/rollback +
convergent bringup; ORG-046 logical identity; ORG-063/ORG-052 Action Broker;
ORG-064 declarative-spec reconciler safety). Given the declarative `checkouts/`
surface (`checkout_spec.py`), this module PLANS what each managed checkout needs
to converge to its desired state, and — behind a guarded-inert gate — APPLIES the
non-destructive subset, VERIFIES the result, and can ROLL BACK its own installs.
It is the real implementation behind the `RepositoryLifecyclePlanner` Protocol seam
that `assembly_resolver.py` (T-3182) already composes against.

SAFETY POSTURE — the non-negotiables (plan §"Reconciler algorithm", §DECISIONS):
  - `plan()` is READ-ONLY + privilege-free: it probes on-disk state and issues a
    read-only `git ls-remote` (remote-side query, no local mutation). It never
    fetches, merges, resets, cleans, or checks out. Divergence is read from the
    LOCAL objects/remote-tracking refs the checkout already holds; the freshness of
    those refs is the caller's fetch cadence's business, not plan()'s.
  - DIRTY / DIVERGED / FOREIGN / MISSING_REF / CREDENTIAL / HEALTH are REFUSALS on
    the shared `RefusalKind` surface — never a reset/clean/force/rewind. FOREIGN
    content (a non-managed repo, or non-repo content, at a target path) is REPORTED,
    NEVER reaped: hard refusal, not a flag (ORG-080 :103 + the Ask ownership
    boundary).
  - The disruptive restart is a guarded-inert brokered INTENT (ORG-052 op-family
    shape): emitted into the plan/receipt, NEVER dispatched in phase-1 (the broker
    D-Bus/MCP exposure is unbuilt, T-2652). Every real-apply path is gated on a
    FUNCTIONAL probe (the T-3629 guarded-inert-on-a-functional-probe lesson) so it
    is inert by default; only a fixture test that injects `functional_probe=lambda:
    True` exercises real (bubble-safe /tmp) mutation.
  - Rollback/removal reclaim ONLY reconciler-installed+recorded content; anything
    not in the receipt is unknown data and is never deleted.

COMPOSITION (T-3182, option a). `RepoReconciler.plan(sources: Sequence[LockedComponent])
-> LifecyclePlan` satisfies the authored Protocol signature exactly, so
`assembly_resolver.plan_assembly_update` composes unchanged: each `LockedComponent`
(what revision is wanted, keyed by `logical_repo`) is joined against the declarative
checkout-spec (where/how/policy), the plan algorithm runs, and the result collapses
to `LifecyclePlan(ok, stages, refusal)` at the boundary. The module's own richer
per-checkout view lives on `ReconcilePlan` (`reconcile_plan()`).

SEAMS (all injected; fixture-stubbed in tests):
  - `probe`          — read-only on-disk observer (`GitCheckoutProbe` is the real one).
  - `remote_resolver`— `logical_repo -> remote url`; the ORG-046 seam. Wrap
    `remote_resolve.resolve_remote` in production; a resolver failure is an
    `UNRESOLVED_SOURCE` refusal.
  - `health_probe`   — `(entry) -> bool`; runs a checkout's health_contract. Default:
    inert (returns True; health not run — the phase-1 plan-only posture).
  - `broker`         — the ORG-063 Action Broker (`available()` + `dispatch(intent)`).
    Default: absent → intents are emitted, never dispatched.
  - `functional_probe`— gates real git mutation in `apply`. Default `lambda: False`
    (inert). This is the guarded-inert gate.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tools.assembly_resolver import (
    ROLLBACK_STAGES,
    UPDATE_STAGES,
    CompatibilityRefusal,
    LifecyclePlan,
    LockedComponent,
    RefusalKind,
)
from tools.checkout_spec import (
    UPDATE_POLICY_AVAILABILITY_ONLY,
    UPDATE_POLICY_FF_ONLY,
    UPDATE_POLICY_NEVER,
    CheckoutEntry,
    CheckoutSpec,
    RepositoryLifecycleError,
)
from tools.component_catalogue import _json_bytes

# ---------------------------------------------------------------------------
# Plan states (the on-disk classification) + their shared-surface refusal kinds
# ---------------------------------------------------------------------------

STATE_ABSENT = "absent"
STATE_CURRENT = "current"
STATE_BEHIND = "behind"
STATE_DIRTY = "dirty"
STATE_DIVERGED = "diverged"
STATE_MISSING_REF = "missing-ref"
STATE_CREDENTIAL_FAIL = "credential-fail"
STATE_HEALTH_FAIL = "health-fail"
STATE_FOREIGN = "foreign"
STATE_UNRESOLVED = "unresolved-source"

# Every refusing plan-state maps to exactly one shared RefusalKind — one uniform
# failure surface across the assembly + reconciler planes (plan §DECISIONS).
_STATE_REFUSAL: dict[str, RefusalKind] = {
    STATE_DIRTY: RefusalKind.DIRTY_CHECKOUT,
    STATE_DIVERGED: RefusalKind.DIVERGED_CHECKOUT,
    STATE_FOREIGN: RefusalKind.FOREIGN_CHECKOUT,
    STATE_MISSING_REF: RefusalKind.MISSING_REF,
    STATE_CREDENTIAL_FAIL: RefusalKind.CREDENTIAL_FAILURE,
    STATE_HEALTH_FAIL: RefusalKind.HEALTH_FAILURE,
    STATE_UNRESOLVED: RefusalKind.UNRESOLVED_SOURCE,
}

ACTION_CLONE = "clone"
ACTION_FF_ADVANCE = "ff-advance"
ACTION_NOOP = "noop"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

RemoteResolver = Callable[[str], str]
HealthProbe = Callable[[CheckoutEntry], bool]


@dataclass(frozen=True)
class CheckoutObservation:
    """A read-only observation of one checkout's on-disk + remote-side state.

    Produced by a `CheckoutProbe`; consumed by the pure `_classify` function.
    """

    remote_reachable: bool
    desired_present: bool  # the desired ref/sha exists on the remote (or locally, pinned)
    exists: bool  # target path exists AND is non-empty
    is_managed: bool  # a git repo whose origin identity matches the resolved remote
    dirty: bool
    head_sha: str | None
    ahead: int  # local commits not reachable from the desired target
    behind: int  # desired-target commits not reachable from local HEAD (ff available)
    desired_resolvable: bool  # the desired target is present in the local object store


class CheckoutProbe(Protocol):
    """Read-only observer seam. `observe` NEVER mutates local state."""

    def observe(self, entry: CheckoutEntry, remote_url: str) -> CheckoutObservation: ...


@dataclass(frozen=True)
class BrokeredIntent:
    """A guarded-inert ORG-052 op-family intent. Emitted into the plan/receipt;
    `dispatched` is always False in phase-1 (the broker exposure is unbuilt)."""

    checkout_id: str
    operation_family: str
    target_unit: str
    dispatched: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "operation_family": self.operation_family,
            "target_unit": self.target_unit,
            "dispatched": self.dispatched,
        }


class ActionBroker(Protocol):
    """ORG-063 Action Broker seam. `available()` is the functional probe for the
    disruptive dispatch path; `dispatch` is NEVER reached in phase-1."""

    def available(self) -> bool: ...

    def dispatch(self, intent: BrokeredIntent) -> None: ...


# ---------------------------------------------------------------------------
# Plan / receipt data model (mirrors scheduler_reconcile PlanItem/PlanResult)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcilePlanItem:
    checkout_id: str
    logical_repo: str
    target_path: str
    state: str
    action: str
    detail: str = ""
    refusal: CompatibilityRefusal | None = None
    brokered_intent: BrokeredIntent | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "logical_repo": self.logical_repo,
            "target_path": self.target_path,
            "state": self.state,
            "action": self.action,
            "detail": self.detail,
            "refusal": self.refusal.as_text() if self.refusal is not None else None,
            "brokered_intent": (
                self.brokered_intent.to_dict() if self.brokered_intent is not None else None
            ),
        }


@dataclass(frozen=True)
class ReconcilePlan:
    items: tuple[ReconcilePlanItem, ...]

    def refusals(self) -> tuple[CompatibilityRefusal, ...]:
        return tuple(
            item.refusal for item in self.items if item.refusal is not None
        )

    @property
    def ok(self) -> bool:
        return not self.refusals()

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.state] = out.get(item.state, 0) + 1
        return out

    def to_report_bytes(self) -> bytes:
        """Deterministic report bytes — stable across checkout ordering (items are
        sorted by checkout_id before rendering)."""
        return _json_bytes(
            {
                "schema_version": 1,
                "items": [
                    item.to_dict()
                    for item in sorted(self.items, key=lambda i: i.checkout_id)
                ],
            }
        )


@dataclass
class ApplyReceipt:
    """What an `apply` did (or, when inert, WOULD do). Records reconciler-installed
    content so rollback/removal can reclaim exactly it and nothing else."""

    materialized: bool
    installs: list[str] = field(default_factory=list)  # checkout_ids cloned
    advances: list[str] = field(default_factory=list)  # checkout_ids ff-advanced
    intents: list[BrokeredIntent] = field(default_factory=list)
    dispatched: list[BrokeredIntent] = field(default_factory=list)
    refused: list[CompatibilityRefusal] = field(default_factory=list)
    install_paths: dict[str, str] = field(default_factory=dict)  # checkout_id -> path
    installed_sha: dict[str, str] = field(default_factory=dict)  # checkout_id -> HEAD sha recorded at install


# ---------------------------------------------------------------------------
# Real read-only git probe (used against bubble-safe /tmp fixtures in tests)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Thin read-only-friendly `git` wrapper; never raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _normalize_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


class GitCheckoutProbe:
    """The real read-only observer. Uses only non-mutating git commands:
    `ls-remote` (remote-side), `rev-parse`, `status --porcelain`, `cat-file -e`,
    `rev-list --count`, `remote get-url`. It NEVER fetches, merges, or writes."""

    def __init__(self, *, timeout: int = 30) -> None:
        self._timeout = timeout

    def _ls_remote(self, remote_url: str) -> dict[str, str] | None:
        res = _run_git(["ls-remote", remote_url], timeout=self._timeout)
        if res.returncode != 0:
            return None
        refs: dict[str, str] = {}
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return refs

    def observe(self, entry: CheckoutEntry, remote_url: str) -> CheckoutObservation:
        remote_refs = self._ls_remote(remote_url)
        remote_reachable = remote_refs is not None

        path = Path(entry.target_path)
        exists = path.is_dir() and any(path.iterdir()) if path.exists() else False

        is_managed = False
        head_sha: str | None = None
        dirty = False
        if exists:
            top = _run_git(["-C", str(path), "rev-parse", "--show-toplevel"], timeout=self._timeout)
            if top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve():
                origin = _run_git(
                    ["-C", str(path), "remote", "get-url", "origin"], timeout=self._timeout
                )
                if origin.returncode == 0 and _normalize_url(origin.stdout.strip()) == _normalize_url(
                    remote_url
                ):
                    is_managed = True

        # Desired target: the pin sha, or the tracking branch's remote tip.
        desired_present = False
        desired_target: str | None = None
        if entry.mode.is_pinned:
            desired_target = entry.mode.pinned
            if remote_refs is not None:
                # present if the sha is a remote tip, or resolvable in the local repo.
                if desired_target in remote_refs.values():
                    desired_present = True
                elif is_managed:
                    chk = _run_git(
                        ["-C", str(path), "cat-file", "-e", f"{desired_target}^{{commit}}"],
                        timeout=self._timeout,
                    )
                    desired_present = chk.returncode == 0
        else:
            branch = entry.mode.tracking
            if remote_refs is not None:
                tip = remote_refs.get(f"refs/heads/{branch}")
                if tip is not None:
                    desired_present = True
                    desired_target = tip

        ahead = 0
        behind = 0
        desired_resolvable = False
        if is_managed:
            head = _run_git(["-C", str(path), "rev-parse", "HEAD"], timeout=self._timeout)
            head_sha = head.stdout.strip() if head.returncode == 0 else None
            dirty = (
                _run_git(["-C", str(path), "status", "--porcelain"], timeout=self._timeout).stdout.strip()
                != ""
            )
            if desired_target is not None:
                resolvable = _run_git(
                    ["-C", str(path), "cat-file", "-e", f"{desired_target}^{{commit}}"],
                    timeout=self._timeout,
                )
                desired_resolvable = resolvable.returncode == 0
                if desired_resolvable and head_sha is not None:
                    ahead = _rev_count(path, f"{desired_target}..HEAD", self._timeout)
                    behind = _rev_count(path, f"HEAD..{desired_target}", self._timeout)

        return CheckoutObservation(
            remote_reachable=remote_reachable,
            desired_present=desired_present,
            exists=exists,
            is_managed=is_managed,
            dirty=dirty,
            head_sha=head_sha,
            ahead=ahead,
            behind=behind,
            desired_resolvable=desired_resolvable,
        )


def _rev_count(path: Path, spec: str, timeout: int) -> int:
    res = _run_git(["-C", str(path), "rev-list", "--count", spec], timeout=timeout)
    try:
        return int(res.stdout.strip()) if res.returncode == 0 else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


def _classify_state(entry: CheckoutEntry, obs: CheckoutObservation, health_ok: bool) -> tuple[str, str]:
    """Classify one checkout into (plan_state, detail). Pure; no I/O."""
    if not obs.remote_reachable:
        return STATE_CREDENTIAL_FAIL, "remote unreachable / auth failed"
    if not obs.desired_present:
        return STATE_MISSING_REF, f"desired ref/sha {entry.mode.desired!r} absent on remote"
    if not obs.exists:
        return STATE_ABSENT, "target path absent — clone needed"
    if not obs.is_managed:
        return STATE_FOREIGN, "target path holds non-managed content — refuse, never reap"
    if obs.dirty:
        return STATE_DIRTY, "working tree has uncommitted changes — refuse, never reset"
    if obs.desired_resolvable:
        if obs.ahead > 0:
            return STATE_DIVERGED, f"local is ahead/diverged ({obs.ahead} local commit(s)) — never rewind"
        if obs.behind > 0:
            state = STATE_BEHIND
        else:
            state = STATE_CURRENT
    else:
        # Desired target not yet in the local object store: an update is available,
        # but ff-vs-diverged cannot be proven locally. Classify BEHIND; a real
        # apply is ff-only, which refuses divergence safely.
        if obs.head_sha is not None and entry.mode.is_pinned and obs.head_sha == entry.mode.pinned:
            state = STATE_CURRENT
        else:
            state = STATE_BEHIND
    if state == STATE_CURRENT and not health_ok:
        return STATE_HEALTH_FAIL, "post-state health probe failed"
    return state, ""


def _action_for(entry: CheckoutEntry, state: str) -> str:
    if state == STATE_ABSENT:
        # never = touch nothing; ff-only / availability-only clone to establish presence.
        return ACTION_NOOP if entry.update_policy == UPDATE_POLICY_NEVER else ACTION_CLONE
    if state == STATE_BEHIND:
        # only ff-only advances; availability-only + never suppress the advance.
        return ACTION_FF_ADVANCE if entry.update_policy == UPDATE_POLICY_FF_ONLY else ACTION_NOOP
    if state == STATE_CURRENT:
        return ACTION_NOOP
    return f"refuse:{_STATE_REFUSAL[state].value}"


# ---------------------------------------------------------------------------
# The reconciler
# ---------------------------------------------------------------------------


class RepoReconciler:
    def __init__(
        self,
        spec: CheckoutSpec,
        *,
        probe: CheckoutProbe,
        remote_resolver: RemoteResolver,
        health_probe: HealthProbe | None = None,
        broker: ActionBroker | None = None,
        functional_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._spec = spec
        self._probe = probe
        self._remote_resolver = remote_resolver
        self._health_probe = health_probe or (lambda _entry: True)
        self._broker = broker
        self._functional_probe = functional_probe or (lambda: False)

    # -- planning ---------------------------------------------------------

    def _resolve(self, entry: CheckoutEntry) -> str | None:
        try:
            url = self._remote_resolver(entry.logical_repo)
        except Exception:  # noqa: BLE001 — any resolver failure is an unresolved source
            return None
        return url or None

    def _classify_entry(self, entry: CheckoutEntry) -> ReconcilePlanItem:
        remote_url = self._resolve(entry)
        if remote_url is None:
            refusal = CompatibilityRefusal(
                RefusalKind.UNRESOLVED_SOURCE,
                entry.checkout_id,
                f"logical repo {entry.logical_repo!r} did not resolve to a remote",
            )
            return ReconcilePlanItem(
                checkout_id=entry.checkout_id,
                logical_repo=entry.logical_repo,
                target_path=entry.target_path,
                state=STATE_UNRESOLVED,
                action=f"refuse:{RefusalKind.UNRESOLVED_SOURCE.value}",
                detail=refusal.detail,
                refusal=refusal,
            )

        obs = self._probe.observe(entry, remote_url)
        # Health is only meaningful for an otherwise-converged checkout; probe it
        # lazily so the health seam is never consulted for a refusing/absent state.
        health_ok = True
        if (
            obs.remote_reachable
            and obs.desired_present
            and obs.exists
            and obs.is_managed
            and not obs.dirty
            and obs.desired_resolvable
            and obs.ahead == 0
            and obs.behind == 0
        ):
            health_ok = bool(self._health_probe(entry))

        state, detail = _classify_state(entry, obs, health_ok)
        action = _action_for(entry, state)

        refusal = None
        if state in _STATE_REFUSAL:
            refusal = CompatibilityRefusal(_STATE_REFUSAL[state], entry.checkout_id, detail)

        brokered_intent = None
        if action in (ACTION_CLONE, ACTION_FF_ADVANCE) and entry.restart_contract is not None:
            brokered_intent = BrokeredIntent(
                checkout_id=entry.checkout_id,
                operation_family=entry.restart_contract.operation_family,
                target_unit=entry.restart_contract.target_unit,
                dispatched=False,
            )

        return ReconcilePlanItem(
            checkout_id=entry.checkout_id,
            logical_repo=entry.logical_repo,
            target_path=entry.target_path,
            state=state,
            action=action,
            detail=detail,
            refusal=refusal,
            brokered_intent=brokered_intent,
        )

    def reconcile_plan(self) -> ReconcilePlan:
        """The module's own rich, per-checkout plan over every declared checkout."""
        items = tuple(
            self._classify_entry(self._spec.checkouts[cid])
            for cid in sorted(self._spec.checkouts)
        )
        return ReconcilePlan(items=items)

    # -- T-3182 RepositoryLifecyclePlanner Protocol -----------------------

    def plan(self, sources: Sequence[LockedComponent]) -> LifecyclePlan:
        """Join each candidate `LockedComponent` (keyed by logical_repo) against the
        declarative checkout-spec, run the plan algorithm, and collapse to the
        Protocol's `LifecyclePlan`. Signature preserved so `plan_assembly_update`
        composes unchanged."""
        refusals: list[CompatibilityRefusal] = []
        for component in sources:
            for entry in self._spec.by_logical_repo(component.logical_repo):
                item = self._classify_entry(entry)
                if item.refusal is not None:
                    refusals.append(item.refusal)
        if refusals:
            refusals.sort(key=lambda r: (r.kind.value, r.subject_id, r.detail))
            return LifecyclePlan(ok=False, stages=ROLLBACK_STAGES, refusal=refusals[0])
        return LifecyclePlan(ok=True, stages=UPDATE_STAGES, refusal=None)

    # -- apply / verify / rollback ----------------------------------------

    def apply(self, plan: ReconcilePlan) -> ApplyReceipt:
        """Materialize ONLY the approved, non-destructive actions — and ONLY when the
        functional probe is live (guarded-inert by default). Refusals are skipped
        (never mutated). The disruptive restart is emitted as a brokered intent and
        dispatched ONLY if a broker reports itself available AND the functional probe
        is live — neither holds in phase-1, so it is NEVER dispatched.
        """
        materialize = bool(self._functional_probe())
        receipt = ApplyReceipt(materialized=materialize)

        for item in sorted(plan.items, key=lambda i: i.checkout_id):
            entry = self._spec.checkouts.get(item.checkout_id)
            if item.refusal is not None:
                receipt.refused.append(item.refusal)
                continue
            if item.action == ACTION_CLONE and entry is not None:
                if materialize:
                    self._do_clone(entry, receipt)
            elif item.action == ACTION_FF_ADVANCE and entry is not None:
                if materialize:
                    self._do_ff_advance(entry, receipt)
            # noop: nothing.

            if item.brokered_intent is not None:
                receipt.intents.append(item.brokered_intent)
                if (
                    self._broker is not None
                    and self._broker.available()
                    and materialize
                ):
                    # Unreachable in phase-1: no broker reports available (unbuilt,
                    # T-2652). Kept as the single dispatch site so phase-2 wiring is
                    # a broker that answers available()=True, nothing here.
                    self._broker.dispatch(item.brokered_intent)
                    receipt.dispatched.append(item.brokered_intent)

        return receipt

    def _do_clone(self, entry: CheckoutEntry, receipt: ApplyReceipt) -> None:
        remote_url = self._resolve(entry)
        if remote_url is None:
            return
        res = _run_git(["clone", remote_url, entry.target_path])
        if res.returncode == 0:
            # Advance a fresh clone to the pinned sha via ff-only (never a reset).
            if entry.mode.is_pinned:
                _run_git(
                    ["-C", entry.target_path, "merge", "--ff-only", entry.mode.pinned]
                )
            receipt.installs.append(entry.checkout_id)
            receipt.install_paths[entry.checkout_id] = entry.target_path
            # Record the HEAD we installed so rollback/removal can prove the clone
            # is still exactly what we put there (unmoved) before any rmtree.
            head = _run_git(["-C", entry.target_path, "rev-parse", "HEAD"])
            if head.returncode == 0:
                receipt.installed_sha[entry.checkout_id] = head.stdout.strip()

    def _do_ff_advance(self, entry: CheckoutEntry, receipt: ApplyReceipt) -> None:
        remote_url = self._resolve(entry)
        if remote_url is None:
            return
        path = entry.target_path
        # Fetch the SPECIFIC desired ref/object (never a bare `git fetch <url>`,
        # whose FETCH_HEAD is the remote's DEFAULT branch — advancing a
        # non-default `tracking:` checkout onto the wrong branch, and leaving a
        # pinned object un-fetched so `merge --ff-only <sha>` silently no-ops).
        # The apply target MUST equal the ref the plan computed as desired.
        # Fetch updates refs/objects only — never the working tree; `merge
        # --ff-only` inherently REFUSES divergence: no reset, no force, ever.
        if entry.mode.is_pinned:
            target = entry.mode.pinned
            # Fetch the exact pin. If the server refuses a bare-sha want, fall
            # back to fetching every branch tip (a ref that contains the pin);
            # the merge target stays the exact pin sha either way.
            fetch = _run_git(["-C", path, "fetch", remote_url, target])
            if fetch.returncode != 0:
                fetch = _run_git(
                    ["-C", path, "fetch", remote_url, "+refs/heads/*:refs/remotes/origin/*"]
                )
                if fetch.returncode != 0:
                    return
        else:
            # Fetch the desired branch by name; FETCH_HEAD is then THAT branch's
            # tip (the ref the plan computed), not the remote's default HEAD.
            target = "FETCH_HEAD"
            fetch = _run_git(["-C", path, "fetch", remote_url, f"refs/heads/{entry.mode.tracking}"])
            if fetch.returncode != 0:
                return
        res = _run_git(["-C", path, "merge", "--ff-only", target])
        if res.returncode == 0:
            receipt.advances.append(entry.checkout_id)

    def verify(self, plan: ReconcilePlan | None = None) -> ReconcilePlan:
        """Diagnose every checkout on one uniform failure surface. A re-plan IS the
        verification (convergent: a fully-applied plan re-plans to all CURRENT/noop).
        `plan` is accepted for symmetry; verify always re-observes."""
        return self.reconcile_plan()

    def rollback(self, receipt: ApplyReceipt) -> list[CompatibilityRefusal]:
        """Reclaim ONLY reconciler-installed content recorded in `receipt`; never
        delete unknown/foreign data. Returns refusals for any recorded install whose
        path is no longer the managed repo we installed (identity changed → FOREIGN,
        reported, never reaped)."""
        refusals: list[CompatibilityRefusal] = []
        for checkout_id in list(receipt.installs):
            entry = self._spec.checkouts.get(checkout_id)
            recorded_path = receipt.install_paths.get(checkout_id)
            if entry is None or recorded_path is None:
                continue
            remote_url = self._resolve(entry)
            recorded_sha = receipt.installed_sha.get(checkout_id)
            outcome = self._reclaim(entry, recorded_path, remote_url, recorded_sha)
            if outcome is not None:
                refusals.append(outcome)
        return refusals

    @staticmethod
    def _working_tree_state(path: Path) -> tuple[bool, bool]:
        """Return (tracked_dirty, has_untracked) for a checkout — read-only.

        `tracked_dirty` = uncommitted changes to tracked files; `has_untracked` =
        untracked (foreign) files present. Either means the working tree is not the
        pristine install we recorded, so it must never be reaped."""
        res = _run_git(["-C", str(path), "status", "--porcelain", "--untracked-files=all"])
        tracked_dirty = False
        has_untracked = False
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            if line.startswith("??"):
                has_untracked = True
            else:
                tracked_dirty = True
        return tracked_dirty, has_untracked

    def _reclaim(
        self,
        entry: CheckoutEntry,
        recorded_path: str,
        remote_url: str | None,
        recorded_sha: str | None,
    ) -> CompatibilityRefusal | None:
        """Remove one reconciler-installed checkout — but ONLY after re-confirming it
        is still EXACTLY the pristine managed clone we installed. Refuse (never
        delete) if identity changed, the working tree is dirty, untracked foreign
        files are present, or HEAD has moved off the recorded installed sha. A clone
        that carries un-reconciled human/foreign state is unknown data (ORG-080 :103
        + the Ask ownership boundary) — reported, never reaped.
        """
        if not self._functional_probe():
            return None  # inert: nothing removed
        path = Path(recorded_path)
        if not path.exists():
            return None
        if remote_url is None:
            return CompatibilityRefusal(
                RefusalKind.UNRESOLVED_SOURCE, entry.checkout_id, "cannot confirm identity before removal"
            )
        obs = self._probe.observe(entry, remote_url)
        if not obs.is_managed:
            return CompatibilityRefusal(
                RefusalKind.FOREIGN_CHECKOUT,
                entry.checkout_id,
                f"recorded path {recorded_path!r} is no longer the managed repo — refusing to reap",
            )
        # Re-observe the working tree: never reap a dirty edit, an untracked foreign
        # file, or a clone whose HEAD moved off the sha we installed (e.g. a user
        # local commit inside an identity-matched clone).
        tracked_dirty, has_untracked = self._working_tree_state(path)
        if tracked_dirty:
            return CompatibilityRefusal(
                RefusalKind.DIRTY_CHECKOUT,
                entry.checkout_id,
                f"recorded path {recorded_path!r} has uncommitted changes — refusing to reap",
            )
        if has_untracked:
            return CompatibilityRefusal(
                RefusalKind.FOREIGN_CHECKOUT,
                entry.checkout_id,
                f"recorded path {recorded_path!r} holds untracked foreign files — refusing to reap",
            )
        if recorded_sha is None or obs.head_sha != recorded_sha:
            return CompatibilityRefusal(
                RefusalKind.FOREIGN_CHECKOUT,
                entry.checkout_id,
                f"recorded path {recorded_path!r} HEAD {obs.head_sha!r} != recorded "
                f"install {recorded_sha!r} — refusing to reap (content moved)",
            )
        shutil.rmtree(path)
        return None

    def remove(self, checkout_id: str, receipt: ApplyReceipt) -> CompatibilityRefusal | None:
        """Remove one checkout by id — ONLY if it is recorded as reconciler-installed.
        An unrecorded path is unknown data: refuse, never delete."""
        entry = self._spec.checkouts.get(checkout_id)
        if entry is None:
            return CompatibilityRefusal(
                RefusalKind.UNRESOLVED_SOURCE, checkout_id, "no such checkout in spec"
            )
        if checkout_id not in receipt.installs:
            return CompatibilityRefusal(
                RefusalKind.FOREIGN_CHECKOUT,
                checkout_id,
                "path not reconciler-installed/recorded — refusing to reap unknown data",
            )
        remote_url = self._resolve(entry)
        return self._reclaim(
            entry,
            receipt.install_paths[checkout_id],
            remote_url,
            receipt.installed_sha.get(checkout_id),
        )


# ---------------------------------------------------------------------------
# CLI (report-only — the plan IS the output; no apply, no mutation)
# ---------------------------------------------------------------------------


def _inert_resolver(_logical_repo: str) -> str:
    # The production wiring (remote_resolve.resolve_remote under a verified
    # manifest) is phase-2; the CLI stays inert here so a bare `plan` run never
    # touches a real remote. Returns "" → every checkout reports UNRESOLVED_SOURCE.
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="T-2951 checkout-lifecycle reconciler (report-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="report the reconcile plan (read-only; no apply)")
    plan_cmd.add_argument("--checkouts-dir", type=Path, default=Path.cwd() / "checkouts")
    generate = sub.add_parser("generate-schema", help="write schemas/checkout-spec.schema.json")
    generate.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "generate-schema":
        from tools.checkout_spec import generate_checkout_spec_schema
        from tools.component_catalogue import _atomic_write

        _atomic_write(
            args.repo_root / "schemas" / "checkout-spec.schema.json",
            generate_checkout_spec_schema(),
        )
        return 0

    from tools.checkout_spec import load_checkout_spec

    try:
        spec = load_checkout_spec(args.checkouts_dir)
    except RepositoryLifecycleError as exc:
        print(f"checkout-spec-invalid: {exc}")
        return 1
    reconciler = RepoReconciler(
        spec, probe=GitCheckoutProbe(), remote_resolver=_inert_resolver
    )
    plan = reconciler.reconcile_plan()
    import sys

    sys.stdout.buffer.write(plan.to_report_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
