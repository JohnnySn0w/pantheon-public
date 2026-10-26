"""tools/assembly_transaction.py — T-3183: the bounded assembly update transaction.

WHAT THIS OWNS (plan §1/§2/§4). This is the update-transaction DRIVER that
composes the already-built resolve -> lock -> publish pipeline
(`assembly_resolver.py`) and the checkout-lifecycle seam (`repo_reconciler.py`)
into one fail-closed, crash-safe assembly update. It adds NO second
resolver/lock/publish. It contributes four new pieces the pipeline did not have:

  1. a distinct fail-closed STAGING primitive (`stage_exact_sources`, §2.A) that
     materialises each component at its EXACT pinned revision and asserts
     `HEAD == pin` — the BLOCKER-1 fix. The reconciler is deliberately NOT the
     staging engine: its `apply()`/`_do_clone` is ff-only for LIVE checkouts and
     cannot stage an exact non-tip revision (it ignores the `merge --ff-only`
     return code and never asserts HEAD == pin), and its `plan()` refuses any
     non-tip pin as MISSING_REF. The reconciler is used only for the read-only
     lifecycle gate (step-3 `plan_assembly_update`) and post-commit live
     convergence — its correct never-rewind ff-only role (D10).
  2. a RUN-TIME smoke/health gate (`ContractRunner`/`run_contract_gate`, §2.B)
     layered over the resolver's resolve-time PRESENCE gate.
  3. an evidence seam (`RunResultsEvidence`, §2.B / MAJOR-2 fix) that implements
     the resolver's `EvidenceResolver` Protocol backed by recorded run results,
     handed to BOTH the final render and `verify_generated_lock` so the lock's
     `ref@<digest>` suffixes are the RUN digests and byte-verify reproduces them.
  4. a content-addressed immutable RECEIPT sealer (`seal_receipt`, §2.C) chained
     new-lock -> receipt -> prior-lock (the receipt does NOT store the new-lock
     digest, breaking circularity).

SCOPE (phase-1, FIXTURE-ONLY). Every git actuation here runs against TEMPORARY
git repos built by `git_fixture.BubbleGit`. Nothing touches a real/live checkout;
the reconciler's production real-apply stays guarded-inert. Staging clones,
fetches, and detaches ONLY under the caller's `staging_root`.

ROLLBACK / COMMIT MODEL (§2.A, HO-ratified deviation from parent step-7).
Because staging is isolated, a pre-publish failure (stage/smoke/health) restores
the prior exact state trivially: the lock was never published (the `publish_lock`
CAS confirms the prior digest is still present — restore-prior-lock is a
construction-verified no-op), the staging tree is discarded, prior health is
rerun against the untouched prior live tree, and candidate diagnostics are
retained in an `outcome="rolled-back"` receipt. The atomic commit point is
`seal_receipt` then `publish_lock`. Converging the live checkout to the new
revisions is a normal post-commit reconciler reconcile driven by the published
lock (convergent/idempotent, ORG-049) — NOT part of the atomic commit. This
substitutes parent step-7's rewind-based restore with equivalent-safety forward
convergence, preserving T-2951's never-rewind invariant (D10). Residual gap
(recorded, phase-2/T-3936): a health failure manifesting only AFTER the live
checkout converges has no restore-prior path; within T-3183 there is no
production live checkout, so this is preserved-visibility, not present-tense risk.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tools import git_fixture
from tools.assembly_resolver import (
    ROLLBACK_STAGES,
    AssemblyLock,
    AssemblyManifest,
    AssemblyResolverError,
    CompatibilityRefusal,
    EvidencePresence,
    LockedComponent,
    LogicalRepoResolver,
    RefusalKind,
    RepositoryLifecyclePlanner,
    _lock_json,
    build_lock,
    lock_digest,
    parse_assembly_lock,
    plan_assembly_update,
    publish_lock,
    render_lock,
    resolve_assembly,
)
from tools.component_catalogue import (
    SCHEMA_VERSION,
    ComponentRecord,
    _atomic_write,
    _json_bytes,
)

GENERATOR_ID = "tools.assembly_transaction"
GENERATOR_VERSION = "1.0.0"

# The fault-injection boundary names (§4). `execute_assembly_update` raises
# FaultInjected immediately BEFORE the named step so a test can re-instantiate
# and assert the boundary's invariant.
FAULT_BOUNDARIES = (
    "validate-current",
    "resolve-candidate-lock",
    "lifecycle-plan",
    "stage-exact-sources",
    "run-smoke",
    "run-health",
    "seal-receipt",
    "publish-lock",
    "converge-live",
)


class FaultInjected(RuntimeError):
    """Raised by `execute_assembly_update(..., fault_at=<boundary>)` immediately
    before the named step, to prove the crash-safety invariant at that boundary."""


# ---------------------------------------------------------------------------
# Staging primitive (§2.A) — a DISTINCT fail-closed engine, NOT the reconciler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagedComponent:
    component_id: str
    logical_repo: str
    revision: str
    path: Path
    head: str  # asserted == revision by stage_exact_sources


@dataclass(frozen=True)
class StagingResult:
    ok: bool
    staged: tuple[StagedComponent, ...]
    refusal: CompatibilityRefusal | None


# `remote_resolver` is a logical_repo -> clone-url callable (distinct from the
# resolver's LogicalRepoResolver, which yields a ResolvedSource): staging needs a
# URL to clone the bubble remote from.


def stage_exact_sources(
    components: Sequence[LockedComponent],
    *,
    staging_root: Path,
    remote_resolver,
    git=git_fixture.git,
) -> StagingResult:
    """Materialise every component at its EXACT pinned revision, fail-closed.

    Per component (§2.A):
      1. `git clone <url> <staging_root>/<component_id>`.
      2. If `git cat-file -e <pin>^{commit}` is absent, `git fetch <url> <pin>`;
         on a server that refuses a bare-sha want, fall back to fetching every
         branch tip; still unresolvable -> typed MISSING_REF.
      3. `git checkout --detach <pin>` (goes to the EXACT sha, never a tip).
      4. ASSERT `git rev-parse HEAD == pin`; any mismatch -> PROVENANCE_MISMATCH.

    Nothing downstream runs unless EVERY component's HEAD equals its pin. Clones,
    fetches, and detaches happen only under `staging_root` (bubble-safe). The
    reconciler's never-rewind ff-only posture is correct for LIVE checkouts and
    deliberately not repurposed here — an ancestor pin under ff-only stays at the
    tip (silently staging LATEST); `checkout --detach <sha>` cannot.
    """
    root = Path(staging_root)
    staged: list[StagedComponent] = []
    for component in components:
        cid = component.component_id
        pin = component.revision
        url = remote_resolver(component.logical_repo)
        if not url:
            return StagingResult(
                ok=False,
                staged=(),
                refusal=CompatibilityRefusal(
                    RefusalKind.MISSING_REF,
                    cid,
                    f"logical repo {component.logical_repo!r} did not resolve to a remote",
                ),
            )
        dest = root / cid
        # Clear any prior scratch at this dest so staging is retry-safe: a clone
        # into a non-empty dir fails, and §4 requires a retry to re-stage.
        if dest.exists():
            shutil.rmtree(dest)
        clone = git("clone", url, str(dest), check=False)
        if clone.returncode != 0:
            return StagingResult(
                ok=False,
                staged=(),
                refusal=CompatibilityRefusal(
                    RefusalKind.MISSING_REF, cid, f"clone of {component.logical_repo!r} failed"
                ),
            )
        present = git("cat-file", "-e", f"{pin}^{{commit}}", cwd=dest, check=False)
        if present.returncode != 0:
            fetched = git("fetch", url, pin, cwd=dest, check=False)
            if fetched.returncode != 0:
                # Server refused a bare-sha want: fetch every branch tip (a ref
                # that may contain the pin). The detach target stays the exact pin.
                git("fetch", url, "+refs/heads/*:refs/remotes/origin/*", cwd=dest, check=False)
            recheck = git("cat-file", "-e", f"{pin}^{{commit}}", cwd=dest, check=False)
            if recheck.returncode != 0:
                return StagingResult(
                    ok=False,
                    staged=(),
                    refusal=CompatibilityRefusal(
                        RefusalKind.MISSING_REF,
                        cid,
                        f"revision {pin!r} unresolvable on {component.logical_repo!r}",
                    ),
                )
        checkout = git("checkout", "--detach", pin, cwd=dest, check=False)
        if checkout.returncode != 0:
            return StagingResult(
                ok=False,
                staged=(),
                refusal=CompatibilityRefusal(
                    RefusalKind.MISSING_REF, cid, f"detach checkout of {pin!r} failed"
                ),
            )
        head = git("rev-parse", "HEAD", cwd=dest, check=False).stdout.strip()
        if head != pin:
            # Defence-in-depth: `checkout --detach <full-sha>` lands on the exact
            # sha or fails, so this asserts the exact-revision honesty invariant.
            return StagingResult(
                ok=False,
                staged=(),
                refusal=CompatibilityRefusal(
                    RefusalKind.PROVENANCE_MISMATCH,
                    cid,
                    f"staged HEAD {head!r} != pinned revision {pin!r}",
                ),
            )
        staged.append(
            StagedComponent(
                component_id=cid,
                logical_repo=component.logical_repo,
                revision=pin,
                path=dest,
                head=head,
            )
        )
    return StagingResult(ok=True, staged=tuple(staged), refusal=None)


# ---------------------------------------------------------------------------
# Run-time contract gate + evidence seam (§2.B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractResult:
    contract_ref: str
    ok: bool
    diagnostics: str
    evidence_digest: str  # sha256 of the run's captured output


class ContractRunner(Protocol):
    """Runs one smoke/health contract against a staged tree, returning a result
    whose `evidence_digest` is the sha256 of the run's captured output."""

    def run(self, contract_ref: str, *, workdir: Path) -> ContractResult: ...


class RunResultsEvidence:
    """An `EvidenceResolver` (Protocol) backed by recorded ContractRunner results.

    `.resolve(ref).present` is True iff the contract ran AND was ok; `.digest` is
    that run's `evidence_digest`. Handed to BOTH the final resolve/render AND
    `verify_generated_lock`, so the published lock's `ref@<digest>` suffixes are
    the RUN digests and a byte-verify reproduces them (the MAJOR-2 fix: the happy
    path cannot self-refuse HAND_EDITED_LOCK).
    """

    def __init__(self) -> None:
        self._results: dict[str, ContractResult] = {}

    def record(self, result: ContractResult) -> None:
        self._results[result.contract_ref] = result

    def resolve(self, evidence_ref: str) -> EvidencePresence:
        result = self._results.get(evidence_ref)
        if result is None or not result.ok:
            return EvidencePresence(evidence_ref, present=False, digest="")
        return EvidencePresence(evidence_ref, present=True, digest=result.evidence_digest)


class _DeclaredPresentEvidence:
    """Step-2 provisional evidence (§2.B two-resolver split): every DECLARED
    smoke/health contract is treated as present so the provisional resolve fixes
    component revisions and runs the resolve-time compatibility gates WITHOUT
    prematurely refusing SMOKE_FAILURE/HEALTH_FAILURE before the run gate. The
    provisional lock is never published; these digests are provisional."""

    def __init__(self, declared: frozenset[str]) -> None:
        self._declared = declared

    def resolve(self, evidence_ref: str) -> EvidencePresence:
        present = evidence_ref in self._declared
        return EvidencePresence(
            evidence_ref, present=present, digest="provisional" if present else ""
        )


def run_contract_gate(
    runner: ContractRunner, refs: Sequence[str], *, workdir: Path, kind: str
) -> tuple[ContractResult, ...]:
    """Run every declared contract ref against `workdir`, in order. `kind`
    ("smoke"/"health") is the gate label for the caller; each result already
    carries its own `contract_ref`.

    MINOR-4 (phase-2 note): the driver passes `workdir=staging_root` — the PARENT
    dir holding every staged component tree — not a per-component tree. A real
    ContractRunner that must run a component-scoped contract would want the
    specific `StagedComponent.path`; the assembly-wide smoke/health here is content
    with the staging root."""
    return tuple(runner.run(ref, workdir=workdir) for ref in refs)


# ---------------------------------------------------------------------------
# Receipt (§2.C) — content-addressed, immutable, chained new-lock->receipt->prior
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateReceipt:
    schema_version: int
    assembly_id: str
    outcome: str  # "published" | "rolled-back"
    prior_lock_sha256: str | None
    staged_revisions: tuple[tuple[str, str], ...]
    smoke_results: tuple[ContractResult, ...]
    health_results: tuple[ContractResult, ...]
    restored_lock_sha256: str | None
    retained_diagnostics: tuple[str, ...]
    generator_id: str
    generator_version: str


@dataclass(frozen=True)
class UpdateOutcome:
    ok: bool
    published_lock_sha256: str | None
    receipt_ref: str | None  # "sha256:<digest>"
    smoke_results: tuple[ContractResult, ...]
    health_results: tuple[ContractResult, ...]
    refusal: CompatibilityRefusal | None
    rollback_trace: tuple[str, ...]


@dataclass(frozen=True)
class AssemblyUpdatePlan:
    """The inputs one update transaction resolves, stages, gates, and publishes.

    NOT a pre-resolved lock: the executor resolves TWICE — a provisional resolve
    at step-2 (fixes revisions + compat gates, evidence declared-present) and a
    final resolve at step-7 (evidence = run results) — so it carries the raw
    resolve inputs plus the prior-lock lineage and the lifecycle seam."""

    manifest: AssemblyManifest
    records: Mapping[str, ComponentRecord]
    repo_resolver: LogicalRepoResolver
    lifecycle: RepositoryLifecyclePlanner
    prior_lock: AssemblyLock | None = None
    prior_lock_bytes: bytes | None = None


def _contract_result_json(result: ContractResult) -> dict[str, object]:
    return {
        "contract_ref": result.contract_ref,
        "ok": result.ok,
        "diagnostics": result.diagnostics,
        "evidence_digest": result.evidence_digest,
    }


def _receipt_json(receipt: UpdateReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "assembly_id": receipt.assembly_id,
        "outcome": receipt.outcome,
        "prior_lock_sha256": receipt.prior_lock_sha256,
        "staged_revisions": [[cid, rev] for cid, rev in receipt.staged_revisions],
        "smoke_results": [_contract_result_json(r) for r in receipt.smoke_results],
        "health_results": [_contract_result_json(r) for r in receipt.health_results],
        "restored_lock_sha256": receipt.restored_lock_sha256,
        "retained_diagnostics": list(receipt.retained_diagnostics),
        "generator_id": receipt.generator_id,
        "generator_version": receipt.generator_version,
    }


def seal_receipt(receipt_root: Path, receipt: UpdateReceipt) -> str:
    """Content-addressed atomic write of an immutable receipt; returns
    "sha256:<digest>". Immutability is structural: the filename IS the digest, so
    a re-seal of identical bytes is idempotent and a differing receipt lands at a
    different path. Chain direction (§2.C): the new lock points at this receipt
    via `update_receipt_ref`; the receipt points back at `prior_lock_sha256`; the
    receipt does NOT store the new-lock digest, breaking circularity. A crash
    after seal but before the lock commits leaves a harmless GC-able orphan."""
    body = _json_bytes(_receipt_json(receipt))
    digest = hashlib.sha256(body).hexdigest()
    path = Path(receipt_root) / receipt.assembly_id / f"{digest}.receipt.json"
    _atomic_write(path, body)
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# The update transaction driver (§2.B ordering, §4 crash/CAS model)
# ---------------------------------------------------------------------------


def _current_digest(lock_path: Path) -> str | None:
    try:
        return lock_digest(Path(lock_path).read_bytes())
    except FileNotFoundError:
        return None


def _prior_lock_sha256(plan: AssemblyUpdatePlan) -> str | None:
    if plan.prior_lock is None:
        return None
    if plan.prior_lock_bytes is not None:
        return lock_digest(plan.prior_lock_bytes)
    return lock_digest(_json_bytes(_lock_json(plan.prior_lock)))


def _refusal_outcome(refusal: CompatibilityRefusal, lock_path: Path) -> UpdateOutcome:
    """A refusal BEFORE any staging (steps 2-3): no receipt, no mutation, the
    current published lock (if any) untouched."""
    return UpdateOutcome(
        ok=False,
        published_lock_sha256=_current_digest(lock_path),
        receipt_ref=None,
        smoke_results=(),
        health_results=(),
        refusal=refusal,
        rollback_trace=(),
    )


def _rerun_prior_health(
    plan: AssemblyUpdatePlan, health_runner: ContractRunner, prior_root: Path
) -> tuple[ContractResult, ...]:
    """Rerun the PRIOR lock's health contracts against the untouched prior live
    tree — a REAL run (§4). No prior lock -> nothing to rerun.

    MINOR-2 (phase-2 note, HO-ratified): `prior_root` is `lock_path.parent`, a
    stand-in for the prior LIVE checkout root. In the temp-fixture pilot there is
    no production live checkout, so the workdir is a fixture no-op that only lets a
    workdir-keyed fixture runner distinguish the candidate staged tree from the
    prior-health rerun. A production implementation would pass the real prior live
    checkout path here."""
    if plan.prior_lock is None:
        return ()
    refs = tuple(ref.rsplit("@", 1)[0] for ref in plan.prior_lock.health_evidence)
    return tuple(health_runner.run(ref, workdir=prior_root) for ref in refs)


def _rollback(
    plan: AssemblyUpdatePlan,
    *,
    triggering: CompatibilityRefusal,
    staged_revisions: tuple[tuple[str, str], ...],
    smoke_results: tuple[ContractResult, ...],
    retained_diagnostics: tuple[str, ...],
    lock_path: Path,
    receipt_root: Path,
    staging_root: Path,
    expected_prior_digest: str | None,
    health_runner: ContractRunner,
) -> UpdateOutcome:
    """A pre-publish failure (stage/smoke/health). Restore-prior-lock is a
    construction-verified no-op (the lock was never published); discard the
    isolated staging tree; rerun prior health against the untouched prior live
    tree; retain candidate diagnostics in an outcome="rolled-back" receipt. If
    the prior-health rerun FAILS -> ROLLBACK_FAILURE with the failing result in
    `health_results`; NEVER a false health-ok claim (§4)."""
    shutil.rmtree(staging_root, ignore_errors=True)  # discard the isolated scratch
    current_digest = _current_digest(lock_path)  # restore-prior-lock = verified no-op
    prior_health = _rerun_prior_health(plan, health_runner, Path(lock_path).parent)
    rollback_failed = any(not r.ok for r in prior_health)
    refusal = (
        CompatibilityRefusal(
            RefusalKind.ROLLBACK_FAILURE,
            plan.manifest.assembly_id,
            "rerun-prior-health failed during rollback; NOT claiming health-ok",
        )
        if rollback_failed
        else triggering
    )
    receipt = UpdateReceipt(
        schema_version=SCHEMA_VERSION,
        assembly_id=plan.manifest.assembly_id,
        outcome="rolled-back",
        prior_lock_sha256=_prior_lock_sha256(plan),
        staged_revisions=staged_revisions,
        smoke_results=smoke_results,
        health_results=prior_health,
        restored_lock_sha256=expected_prior_digest,
        retained_diagnostics=retained_diagnostics,
        generator_id=GENERATOR_ID,
        generator_version=GENERATOR_VERSION,
    )
    receipt_ref = seal_receipt(receipt_root, receipt)
    return UpdateOutcome(
        ok=False,
        published_lock_sha256=current_digest,
        receipt_ref=receipt_ref,
        smoke_results=smoke_results,
        health_results=prior_health,
        refusal=refusal,
        rollback_trace=ROLLBACK_STAGES,
    )


def execute_assembly_update(
    plan: AssemblyUpdatePlan,
    *,
    smoke_runner: ContractRunner,
    health_runner: ContractRunner,
    staging_root: Path,
    lock_path: Path,
    receipt_root: Path,
    remote_resolver,
    expected_prior_digest: str | None,
    fault_at: str | None = None,
) -> UpdateOutcome:
    """Drive one bounded assembly update through the §2.B ordering, reconciled
    with `assembly_resolver.UPDATE_STAGES`. `fault_at` raises `FaultInjected`
    immediately before the named boundary (§4). Refusals before staging return a
    no-mutation outcome; pre-publish failures roll back with a sealed
    rolled-back receipt; the atomic commit is `seal_receipt` then `publish_lock`."""
    lock_path = Path(lock_path)
    staging_root = Path(staging_root)
    receipt_root = Path(receipt_root)

    def fault(step: str) -> None:
        if fault_at == step:
            raise FaultInjected(f"injected fault before {step}")

    # 1. validate-current-manifest-catalogue-lock (read-only).
    fault("validate-current")
    if lock_path.exists():
        try:
            parse_assembly_lock(lock_path.read_bytes())
        except AssemblyResolverError:
            return _refusal_outcome(
                CompatibilityRefusal(
                    RefusalKind.STALE_LOCK_INPUT,
                    plan.manifest.assembly_id,
                    "on-disk current lock does not parse",
                ),
                lock_path,
            )

    # 2. resolve-candidate-lock (PROVISIONAL): fix revisions + resolve-time
    #    compatibility gates. Declared-present evidence so smoke/health do not
    #    prematurely refuse before the run gate (two-resolver split).
    fault("resolve-candidate-lock")
    declared = frozenset(plan.manifest.smoke_contracts) | frozenset(plan.manifest.health_contracts)
    provisional = resolve_assembly(
        plan.manifest, plan.records, plan.repo_resolver, _DeclaredPresentEvidence(declared)
    )
    if provisional.refusals:
        return _refusal_outcome(provisional.refusals[0], lock_path)
    staged_revisions = tuple(
        (component.component_id, component.revision) for component in provisional.locked_components
    )
    prior_sha = _prior_lock_sha256(plan)

    # 3. lifecycle-plan: prior-lock lineage CAS gate + single reconciler.plan().
    fault("lifecycle-plan")
    candidate_for_lifecycle = build_lock(
        dataclasses.replace(provisional, prior_lock_sha256=prior_sha, update_receipt_ref=None)
    )
    try:
        plan_assembly_update(
            plan.prior_lock,
            candidate_for_lifecycle,
            plan.lifecycle,
            prior_lock_bytes=plan.prior_lock_bytes,
        )
    except AssemblyResolverError as exc:
        return _refusal_outcome(exc.refusal, lock_path)

    # 4. stage-exact-sources (§2.A primitive; fail-closed HEAD == pin).
    fault("stage-exact-sources")
    staging = stage_exact_sources(
        provisional.locked_components,
        staging_root=staging_root,
        remote_resolver=remote_resolver,
    )
    if not staging.ok:
        return _rollback(
            plan,
            triggering=staging.refusal,
            staged_revisions=staged_revisions,
            smoke_results=(),
            retained_diagnostics=(staging.refusal.as_text(),),
            lock_path=lock_path,
            receipt_root=receipt_root,
            staging_root=staging_root,
            expected_prior_digest=expected_prior_digest,
            health_runner=health_runner,
        )

    # 5. run-smoke-contracts against the staged tree.
    fault("run-smoke")
    evidence = RunResultsEvidence()
    smoke_results = run_contract_gate(
        smoke_runner, plan.manifest.smoke_contracts, workdir=staging_root, kind="smoke"
    )
    for result in smoke_results:
        evidence.record(result)
    if not all(result.ok for result in smoke_results):
        return _rollback(
            plan,
            triggering=CompatibilityRefusal(
                RefusalKind.SMOKE_FAILURE, plan.manifest.assembly_id, "smoke contract(s) failed"
            ),
            staged_revisions=staged_revisions,
            smoke_results=smoke_results,
            retained_diagnostics=tuple(r.diagnostics for r in smoke_results if not r.ok),
            lock_path=lock_path,
            receipt_root=receipt_root,
            staging_root=staging_root,
            expected_prior_digest=expected_prior_digest,
            health_runner=health_runner,
        )

    # 6. run-health-contracts against the staged tree.
    fault("run-health")
    health_results = run_contract_gate(
        health_runner, plan.manifest.health_contracts, workdir=staging_root, kind="health"
    )
    for result in health_results:
        evidence.record(result)
    if not all(result.ok for result in health_results):
        return _rollback(
            plan,
            triggering=CompatibilityRefusal(
                RefusalKind.HEALTH_FAILURE, plan.manifest.assembly_id, "health contract(s) failed"
            ),
            staged_revisions=staged_revisions,
            smoke_results=smoke_results,
            retained_diagnostics=tuple(r.diagnostics for r in health_results if not r.ok),
            lock_path=lock_path,
            receipt_root=receipt_root,
            staging_root=staging_root,
            expected_prior_digest=expected_prior_digest,
            health_runner=health_runner,
        )

    # 7. publish-lock-and-receipt. Re-resolve the FINAL candidate with the
    #    run-results evidence (digests = run digests), carrying the SAME
    #    prior_lock_sha256 + the sealed receipt ref; seal receipt first (§2.C),
    #    then publish under the CAS.
    final = resolve_assembly(plan.manifest, plan.records, plan.repo_resolver, evidence)
    if final.refusals:
        # Defensive: a provisional-pass + runs-ok assembly re-resolves clean.
        return _rollback(
            plan,
            triggering=final.refusals[0],
            staged_revisions=staged_revisions,
            smoke_results=smoke_results,
            retained_diagnostics=tuple(r.as_text() for r in final.refusals),
            lock_path=lock_path,
            receipt_root=receipt_root,
            staging_root=staging_root,
            expected_prior_digest=expected_prior_digest,
            health_runner=health_runner,
        )
    # CAS pre-check (read-only): refuse a stale commit before sealing an orphan.
    if _current_digest(lock_path) != expected_prior_digest:
        return _refusal_outcome(
            CompatibilityRefusal(
                RefusalKind.STALE_LOCK_INPUT,
                plan.manifest.assembly_id,
                f"current lock digest {_current_digest(lock_path)!r} != expected "
                f"{expected_prior_digest!r}",
            ),
            lock_path,
        )
    receipt = UpdateReceipt(
        schema_version=SCHEMA_VERSION,
        assembly_id=plan.manifest.assembly_id,
        outcome="published",
        prior_lock_sha256=prior_sha,
        staged_revisions=staged_revisions,
        smoke_results=smoke_results,
        health_results=health_results,
        restored_lock_sha256=None,
        retained_diagnostics=(),
        generator_id=GENERATOR_ID,
        generator_version=GENERATOR_VERSION,
    )
    fault("seal-receipt")
    receipt_ref = seal_receipt(receipt_root, receipt)
    final_bytes = render_lock(
        dataclasses.replace(final, prior_lock_sha256=prior_sha, update_receipt_ref=receipt_ref)
    )
    fault("publish-lock")
    published = publish_lock(lock_path, final_bytes, expected_prior_digest=expected_prior_digest)

    # 8. converge-live (post-commit; NOT part of the atomic commit). A normal
    #    reconciler reconcile driven by the now-published lock — convergent/
    #    idempotent (ORG-049). In the temp-fixture pilot there is no production
    #    live checkout, so a crash here self-heals on re-run.
    fault("converge-live")
    return UpdateOutcome(
        ok=True,
        published_lock_sha256=published,
        receipt_ref=receipt_ref,
        smoke_results=smoke_results,
        health_results=health_results,
        refusal=None,
        rollback_trace=(),
    )
