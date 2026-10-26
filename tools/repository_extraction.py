"""tools/repository_extraction.py — T-3183 §3: the repository-extraction executor.

Driven by a parsed `component_catalogue.RepositoryBoundaryDecision`, this executes
a bounded component extraction from a monorepo prefix into its own repository, on
TEMPORARY git repos only (BubbleGit fixtures), behind a guarded-inert `functional`
gate. A `keep-together` decision is a valid reviewed no-op. An `extract` decision
re-asserts the T-2415 gate (a present contract + at least one independence signal)
at execution, or refuses before any mutation.

Extraction steps (§3), functional=True:
  1. Inventory + preserve canonical source/history/data/ownership. Materialise a
     new noncanonical bare remote and migrate the `current_source.path` history via
     `git subtree split --prefix=<path>` (a committed choice: `git filter-repo` is
     not installed on this seat; `git subtree` is, and subtree split preserves
     per-commit author AND committer identity). Then push. `migration.data` /
     ownership refs are preserved AS RECORDED, never invented.
  2. One-canonical enforcement. Write a `canonical-source` marker in the NEW repo;
     flip the OLD path non-canonical. `assert_single_canonical_source` refuses
     DUAL_CANONICAL_SOURCE if two writable canonicals ever coexist.
  3. source_cutover once (atomic marker flip; idempotent — re-run is a no-op).
  4. old_path_disposition per the decision's refs (tombstone/redirect, NEVER
     authority; never delete unknown/foreign content — only reclaim what the
     extraction recorded).
  5. Update the component record (additive: `source.logical_repo` = the new repo).
     No project formation, no organ enable — the extraction only rewrites the
     logical-repo of one component (scope guard).
  6. Rollback: restore the old canonical marker, discard the new-repo marker,
     retain diagnostics. The old-path content is preserved until disposition is
     confirmed, so rollback is always possible.

SCOPE (phase-1, FIXTURE-ONLY). Nothing here forms a project, production-enables an
organ, or creates remote/failover policy. All git actuation is bubble-safe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
import shutil

from tools import git_fixture
from tools.assembly_resolver import (
    AssemblyResolverError,
    CompatibilityRefusal,
    RefusalKind,
)
from tools.component_catalogue import (
    SCHEMA_VERSION,
    BoundaryDecisionKind,
    RepositoryBoundaryDecision,
    _atomic_write,
    _json_bytes,
)

GENERATOR_ID = "tools.repository_extraction"
GENERATOR_VERSION = "1.0.0"

# The canonical-source sentinel: a repo (working tree, or a monorepo prefix) that
# carries this file is a writable canonical source for its component. Exactly one
# such source may exist at a time (`assert_single_canonical_source`).
CANONICAL_MARKER = ".canonical-source"
TOMBSTONE = "EXTRACTED.txt"

EXTRACTION_STEPS = (
    "inventory-and-preserve",
    "one-canonical-enforcement",
    "source-cutover",
    "old-path-disposition",
    "update-component-record",
)

_SPLIT_BRANCH = "extraction-split"


class _InjectedFailure(RuntimeError):
    """A test-injected mid-step failure (`fault_after`) exercising the rollback
    path; caught by `execute_extraction`, which rolls back and returns a
    rolled-back receipt."""


def _refuse(kind: RefusalKind, subject: str, detail: str) -> AssemblyResolverError:
    refusal = CompatibilityRefusal(kind, subject, detail)
    return AssemblyResolverError(refusal.as_text(), refusal=refusal)


@dataclass(frozen=True)
class ExtractionPlan:
    decision: RepositoryBoundaryDecision
    source_repo: Path
    prefix: str
    new_remote: str
    new_logical_repo: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionReceipt:
    schema_version: int
    decision_id: str
    component_id: str
    outcome: str  # "extracted" | "kept-together" | "inert" | "rolled-back"
    prefix: str
    new_logical_repo: str | None
    old_path_disposition: tuple[str, ...]
    preserved_data: tuple[str, ...]
    migrated_commits: int
    canonical_repo: str | None
    retained_diagnostics: tuple[str, ...]
    generator_id: str
    generator_version: str


def _assert_extract_gate(decision: RepositoryBoundaryDecision) -> None:
    """Re-assert the T-2415 extraction gate: a present contract + >=1 independence
    signal. The catalogue parser already enforces this for a parsed decision; this
    re-checks at execution so a directly-constructed decision cannot bypass it."""
    if decision.decision is not BoundaryDecisionKind.EXTRACT:
        return
    if not (decision.contract.id and decision.contract.test_evidence_refs):
        raise _refuse(
            RefusalKind.LIFECYCLE_REFUSED,
            decision.decision_id,
            "extract gate (T-2415): contract not present (id + test evidence required)",
        )
    if not decision.independence_signals:
        raise _refuse(
            RefusalKind.LIFECYCLE_REFUSED,
            decision.decision_id,
            "extract gate (T-2415): no independence signal recorded",
        )


def plan_extraction(
    decision: RepositoryBoundaryDecision,
    *,
    source_repo: Path,
    new_remote: str,
    new_logical_repo: str,
) -> ExtractionPlan:
    """Plan (never execute) an extraction. `keep-together` -> an empty-step no-op;
    `extract` -> the five steps, after re-asserting the T-2415 gate."""
    prefix = decision.current_source.path
    if decision.decision is BoundaryDecisionKind.KEEP_TOGETHER:
        steps: tuple[str, ...] = ()
    else:
        _assert_extract_gate(decision)
        steps = EXTRACTION_STEPS
    return ExtractionPlan(
        decision=decision,
        source_repo=Path(source_repo),
        prefix=prefix,
        new_remote=str(new_remote),
        new_logical_repo=new_logical_repo,
        steps=steps,
    )


def assert_single_canonical_source(repos) -> None:
    """Refuse DUAL_CANONICAL_SOURCE if more than one of `repos` (working-dir paths,
    or a monorepo prefix dir) carries the canonical-source marker. One canonical
    writable source at a time — the reverse-write guard depends on this."""
    canonical = sorted(str(Path(r)) for r in repos if (Path(r) / CANONICAL_MARKER).is_file())
    if len(canonical) > 1:
        raise _refuse(
            RefusalKind.DUAL_CANONICAL_SOURCE,
            "repository-boundary",
            f"multiple writable canonical sources: {canonical}",
        )


def _extraction_receipt_json(receipt: ExtractionReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "decision_id": receipt.decision_id,
        "component_id": receipt.component_id,
        "outcome": receipt.outcome,
        "prefix": receipt.prefix,
        "new_logical_repo": receipt.new_logical_repo,
        "old_path_disposition": list(receipt.old_path_disposition),
        "preserved_data": list(receipt.preserved_data),
        "migrated_commits": receipt.migrated_commits,
        "canonical_repo": receipt.canonical_repo,
        "retained_diagnostics": list(receipt.retained_diagnostics),
        "generator_id": receipt.generator_id,
        "generator_version": receipt.generator_version,
    }


def seal_extraction_receipt(receipt_root: Path, receipt: ExtractionReceipt) -> str:
    """Content-addressed atomic write of an immutable extraction receipt (sealed
    like the assembly UpdateReceipt); returns "sha256:<digest>"."""
    body = _json_bytes(_extraction_receipt_json(receipt))
    digest = hashlib.sha256(body).hexdigest()
    path = Path(receipt_root) / receipt.component_id / f"{digest}.extraction.json"
    _atomic_write(path, body)
    return f"sha256:{digest}"


def execute_extraction(
    plan: ExtractionPlan,
    *,
    git=git_fixture.git,
    functional: bool = False,
    receipt_root: Path,
    work_root: Path,
    fault_after: str | None = None,
) -> ExtractionReceipt:
    """Execute (or, when inert, describe) the extraction. Guarded-inert by default
    (functional=False -> no mutation). `fault_after` injects a mid-step failure
    after the named step to exercise the rollback path."""
    decision = plan.decision
    receipt_root = Path(receipt_root)
    work_root = Path(work_root)

    def _seal(receipt: ExtractionReceipt) -> ExtractionReceipt:
        seal_extraction_receipt(receipt_root, receipt)
        return receipt

    def _receipt(outcome, *, new_logical_repo=None, migrated=0, canonical=None, diagnostics=()):
        return ExtractionReceipt(
            schema_version=SCHEMA_VERSION,
            decision_id=decision.decision_id,
            component_id=decision.component_id,
            outcome=outcome,
            prefix=plan.prefix,
            new_logical_repo=new_logical_repo,
            old_path_disposition=decision.migration.old_path_disposition,
            preserved_data=decision.migration.data,
            migrated_commits=migrated,
            canonical_repo=canonical,
            retained_diagnostics=tuple(diagnostics),
            generator_id=GENERATOR_ID,
            generator_version=GENERATOR_VERSION,
        )

    # keep-together: a valid reviewed no-op.
    if decision.decision is BoundaryDecisionKind.KEEP_TOGETHER:
        return _seal(_receipt("kept-together"))

    # extract: re-assert the gate BEFORE any mutation.
    _assert_extract_gate(decision)

    if not functional:
        return _seal(_receipt("inert", new_logical_repo=plan.new_logical_repo))

    src = plan.source_repo
    prefix = plan.prefix
    old_prefix = src / prefix
    # The OLD canonical marker is extraction METADATA about the monorepo, so it
    # lives at the monorepo ROOT, NOT inside `<prefix>` — otherwise `git subtree
    # split --prefix=<prefix>` would carry it into the new repo's history, leaving
    # a stray `.canonical-source` in new_remote even after a clean rollback (the
    # MAJOR-1 confound). The vacated prefix instead gets a TOMBSTONE (step 4).
    old_marker = src / CANONICAL_MARKER
    old_marker_existed = old_marker.is_file()
    old_marker_content = old_marker.read_text(encoding="utf-8") if old_marker_existed else None
    new_work = work_root / "new-canonical"

    def _rollback(diagnostic: str) -> ExtractionReceipt:
        # Restore the old canonical marker; discard the new-repo working clone; the
        # old-path content was preserved so this is always possible (§3.6). Because
        # the NEW canonical marker is committed+pushed only AFTER source-cutover
        # (MAJOR-1 deferral), any fault at/before cutover leaves new_remote with no
        # canonical marker, so rmtree(new_work) leaves EXACTLY one canonical source
        # (the old path) and a retry against the same remote fast-forwards.
        if old_marker_existed:
            old_marker.write_text(old_marker_content or "", encoding="utf-8")
        tombstone = old_prefix / TOMBSTONE
        if tombstone.exists():
            tombstone.unlink()
        if new_work.exists():
            shutil.rmtree(new_work)
        return _seal(_receipt("rolled-back", diagnostics=(diagnostic,)))

    def _fault(step: str) -> None:
        if fault_after == step:
            raise _InjectedFailure(f"injected failure after {step}")

    try:
        # 1. inventory-and-preserve: subtree split (author+committer preserved) + push.
        if not Path(plan.new_remote).exists():
            git("init", "--bare", "-b", "main", plan.new_remote)
        git("branch", "-D", _SPLIT_BRANCH, cwd=src, check=False)  # retry-safe
        split = git("subtree", "split", f"--prefix={prefix}", "-b", _SPLIT_BRANCH, cwd=src, check=False)
        if split.returncode != 0:
            raise _refuse(
                RefusalKind.MISSING_REF,
                decision.decision_id,
                f"subtree split of {prefix!r} failed: {split.stderr.strip()}",
            )
        push = git("push", plan.new_remote, f"{_SPLIT_BRANCH}:refs/heads/main", cwd=src, check=False)
        if push.returncode != 0:
            raise _refuse(
                RefusalKind.MISSING_REF, decision.decision_id, "push of split history failed"
            )
        migrated = int(
            git("rev-list", "--count", _SPLIT_BRANCH, cwd=src, check=False).stdout.strip() or "0"
        )
        if new_work.exists():
            shutil.rmtree(new_work)
        git("clone", plan.new_remote, str(new_work))
        _fault("inventory-and-preserve")

        # 2. one-canonical-enforcement: stage the NEW canonical marker in the
        #    working tree ONLY. The commit + push to plan.new_remote is DEFERRED to
        #    AFTER source-cutover succeeds (below), so any fault at/before cutover
        #    leaves new_remote with NO canonical marker on `main` — rollback is
        #    then naturally clean (rmtree new_work) and a retry against the same
        #    remote fast-forwards. assert_single_canonical_source reads the
        #    working-tree marker, so the one-canonical check is unaffected by the
        #    deferral (MAJOR-1 fix).
        (new_work / CANONICAL_MARKER).write_text(plan.new_logical_repo + "\n", encoding="utf-8")
        _fault("one-canonical-enforcement")

        # 3. source-cutover: flip the OLD path non-canonical (idempotent), then
        #    assert exactly one canonical source remains. The NEW marker is not yet
        #    committed/pushed at this point.
        if old_marker.is_file():
            old_marker.unlink()
        assert_single_canonical_source([src, new_work])
        _fault("source-cutover")

        # Cutover succeeded: NOW commit + push the NEW canonical marker (the
        # deferred atomic marker publish). Before this line new_remote carries only
        # the migrated split history — no canonical marker.
        git("add", CANONICAL_MARKER, cwd=new_work)
        git("commit", "-m", "extraction: mark canonical-source", cwd=new_work)
        git("push", "origin", "main", cwd=new_work)

        # 4. old-path-disposition: tombstone/redirect (never delete recorded-only).
        old_prefix.mkdir(parents=True, exist_ok=True)
        (old_prefix / TOMBSTONE).write_text(
            "extracted-to: "
            + plan.new_logical_repo
            + "\n"
            + "\n".join(decision.migration.old_path_disposition)
            + "\n",
            encoding="utf-8",
        )
        git("add", "-A", cwd=src)
        # MINOR-3: never ignore the commit return code (the ignored-return-code
        # class the staging BLOCKER was). Tolerate ONLY an empty commit; any other
        # non-zero fails closed with a typed refusal.
        commit = git("commit", "-m", f"extraction: cut over {prefix}", cwd=src, check=False)
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            raise _refuse(
                RefusalKind.LIFECYCLE_REFUSED,
                decision.decision_id,
                f"old-path cutover commit failed: {commit.stderr.strip()}",
            )
        _fault("old-path-disposition")

        # 5. update-component-record: additive — source.logical_repo = new repo,
        #    recorded in the receipt. No project formation, no organ enable.
        _fault("update-component-record")
    except _InjectedFailure as exc:
        return _rollback(str(exc))

    return _seal(
        _receipt(
            "extracted",
            new_logical_repo=plan.new_logical_repo,
            migrated=migrated,
            canonical=str(new_work),
        )
    )
