"""tools/test_assembly_transaction.py — T-3183 §5 transaction matrix.

Real temp-git 3-repo pilot (A refuses / B publishes / C restores B), the parent
refusal matrix driven THROUGH the transaction, the crash/CAS boundary model
(one injected fault per boundary), the publish CAS, the rollback-failure honesty
guard, and the BLOCKER-1 staging regression guard. Every git repo is a real repo
under `git_fixture.BubbleGit`; functional git mutation happens only inside the
bubble.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import unittest
from pathlib import Path

from tools import assembly_resolver as ar
from tools.assembly_resolver import RefusalKind, ResolvedSource
from tools.assembly_transaction import (
    AssemblyUpdatePlan,
    ContractResult,
    FaultInjected,
    RunResultsEvidence,
    execute_assembly_update,
    stage_exact_sources,
)
from tools.checkout_spec import CheckoutEntry, CheckoutMode, CheckoutSpec
from tools.git_fixture import BubbleSafeCase, git as _git
from tools.repo_reconciler import GitCheckoutProbe, RepoReconciler
from tools.test_assembly_resolver import (
    FixtureLifecycle,
    FixtureRepoResolver,
    _load_records,
    _manifest as _manifest_dict,
    _manifest_component,
    _parse_manifest,
    _record,
)

_COMPONENTS = ("comp-a", "comp-b", "comp-c")


class _DictResolver:
    """logical_repo -> remote url; unknown (or empty) means unresolved."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def __call__(self, logical_repo: str) -> str:
        return self._mapping.get(logical_repo, "")


class _FixtureRunner:
    """A deterministic ContractRunner. `decide(ref, workdir) -> bool` picks ok.

    `evidence_digest` depends only on (ref, ok, diagnostics) — NOT on workdir —
    so a happy-path run is reproducible: a test can rebuild a RunResultsEvidence
    by re-running the same runner and hand it to `verify_generated_lock`. A
    workdir-sensitive decision (used for the health/rollback split) is fine
    because those paths never round-trip through verify."""

    def __init__(self, decide=None) -> None:
        self._decide = decide or (lambda ref, workdir: True)

    def run(self, contract_ref: str, *, workdir: Path) -> ContractResult:
        ok = bool(self._decide(contract_ref, workdir))
        diagnostics = "" if ok else f"contract {contract_ref} failed"
        digest = hashlib.sha256(f"{contract_ref}|{ok}|{diagnostics}".encode("utf-8")).hexdigest()
        return ContractResult(
            contract_ref=contract_ref, ok=ok, diagnostics=diagnostics, evidence_digest=digest
        )


class _PilotBase(BubbleSafeCase):
    """Three real bubble remotes (comp-a/b/c), each seeded to a real commit, plus
    the resolve/stage/manifest fixtures the bounded assembly pilot composes."""

    SMOKE = ("smoke:pilot",)
    HEALTH = ("health:pilot",)

    def setUp(self) -> None:
        super().setUp()
        self._ids = itertools.count()
        self.repos: dict[str, tuple[str, str]] = {}
        for cid in _COMPONENTS:
            remote = self.new_remote(cid)
            sha = self.seed(remote, content=f"{cid}-v1", name=cid)
            self.repos[f"{cid}-repo"] = (remote, sha)

    # -- fixtures ---------------------------------------------------------

    def _uniq(self, prefix: str) -> Path:
        return Path(self.path(prefix, str(next(self._ids))))

    def _records(self, *, revisions: dict[str, str] | None = None):
        revisions = revisions or {}
        recs = []
        for cid in _COMPONENTS:
            lr = f"{cid}-repo"
            recs.append(
                _record(
                    component_id=cid,
                    logical_repo=lr,
                    tested_revision=revisions.get(lr, self.repos[lr][1]),
                )
            )
        return _load_records(self._uniq("records"), recs)

    def _repo_resolver(self, *, revisions: dict[str, str] | None = None) -> FixtureRepoResolver:
        revisions = revisions or {}
        sources = {
            lr: ResolvedSource(
                logical_repo=lr,
                resolved_source=f"file://{remote}",
                revision=revisions.get(lr, sha),
                artifact_digest=f"sha256:{lr}",
            )
            for lr, (remote, sha) in self.repos.items()
        }
        return FixtureRepoResolver(sources)

    def _remote_resolver(self):
        mapping = {lr: remote for lr, (remote, _sha) in self.repos.items()}
        return _DictResolver(mapping)

    def _manifest(self, *, feature_conflict: bool = False, assembly_id: str = "pilot-assembly"):
        comps = [_manifest_component(cid) for cid in _COMPONENTS]
        if feature_conflict:
            comps[0] = _manifest_component("comp-a", required_features=["nonexistent-feature"])
        data = _manifest_dict(
            assembly_id=assembly_id,
            components=comps,
            smoke=list(self.SMOKE),
            health=list(self.HEALTH),
        )
        return _parse_manifest(self._uniq("manifests"), data)

    def _plan(self, *, lifecycle=None, prior=None, prior_bytes=None, revisions=None, feature_conflict=False):
        return AssemblyUpdatePlan(
            manifest=self._manifest(feature_conflict=feature_conflict),
            records=self._records(revisions=revisions),
            repo_resolver=self._repo_resolver(revisions=revisions),
            lifecycle=lifecycle or FixtureLifecycle(),
            prior_lock=prior,
            prior_lock_bytes=prior_bytes,
        )

    def _paths(self):
        return {
            "staging_root": Path(self.path("staging")),
            "lock_path": Path(self.path("locks", "pilot.lock.json")),
            "receipt_root": Path(self.path("receipts")),
        }

    def _run(self, plan, *, smoke=None, health=None, expected_prior_digest=None, fault_at=None, paths=None):
        paths = paths or self._paths()
        return execute_assembly_update(
            plan,
            smoke_runner=smoke or _FixtureRunner(),
            health_runner=health or _FixtureRunner(),
            staging_root=paths["staging_root"],
            lock_path=paths["lock_path"],
            receipt_root=paths["receipt_root"],
            remote_resolver=self._remote_resolver(),
            expected_prior_digest=expected_prior_digest,
            fault_at=fault_at,
        )

    def _publish_b(self, paths):
        """Publish the corrected assembly B; return (lock_bytes, digest)."""
        outcome = self._run(self._plan(), paths=paths, expected_prior_digest=None)
        self.assertTrue(outcome.ok, outcome.refusal)
        return paths["lock_path"].read_bytes(), outcome.published_lock_sha256

    def _evidence_for(self, manifest):
        evidence = RunResultsEvidence()
        runner = _FixtureRunner()
        for ref in list(manifest.smoke_contracts) + list(manifest.health_contracts):
            evidence.record(runner.run(ref, workdir=Path(self.path("staging"))))
        return evidence


# ---------------------------------------------------------------------------
# Three-repo pilot: A refuses / B publishes / C restores B
# ---------------------------------------------------------------------------


class TestThreeRepoPilot(_PilotBase):
    def test_candidate_a_incompatible_feature_refuses_before_mutation(self) -> None:
        paths = self._paths()
        outcome = self._run(self._plan(feature_conflict=True), paths=paths)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.FEATURE_CONFLICT)
        # staging_root empty, lock absent, no receipt.
        self.assertFalse(paths["staging_root"].exists() and any(paths["staging_root"].iterdir()))
        self.assertFalse(paths["lock_path"].exists())
        self.assertIsNone(outcome.receipt_ref)
        self.assertFalse(
            paths["receipt_root"].exists() and any(paths["receipt_root"].rglob("*.receipt.json"))
        )

    def test_corrected_b_stages_smoke_health_publishes_lock_and_receipt(self) -> None:
        paths = self._paths()
        plan = self._plan()
        outcome = self._run(plan, paths=paths, expected_prior_digest=None)
        self.assertTrue(outcome.ok, outcome.refusal)

        # staged HEAD == pin per component.
        for cid in _COMPONENTS:
            staged = Path(self.path("staging", cid))
            head = _git("rev-parse", "HEAD", cwd=staged).stdout.strip()
            self.assertEqual(head, self.repos[f"{cid}-repo"][1])

        published = paths["lock_path"].read_bytes()
        lock = ar.parse_assembly_lock(published)
        self.assertEqual(lock.update_receipt_ref, outcome.receipt_ref)
        self.assertEqual(outcome.published_lock_sha256, ar.lock_digest(published))

        # receipt at <digest>.receipt.json; prior_lock_sha256 linkage (None first).
        digest = outcome.receipt_ref.split(":", 1)[1]
        receipt_path = paths["receipt_root"] / "pilot-assembly" / f"{digest}.receipt.json"
        self.assertTrue(receipt_path.is_file())
        receipt_json = json.loads(receipt_path.read_bytes())
        self.assertEqual(receipt_json["outcome"], "published")
        self.assertIsNone(receipt_json["prior_lock_sha256"])
        self.assertEqual(lock.prior_lock_sha256, None)

        # render_lock deterministic (rendered twice byte-equal) + equals published;
        # verify_generated_lock passes on the published bytes (run-digest seam).
        evidence = self._evidence_for(plan.manifest)
        resolved = ar.resolve_assembly(plan.manifest, plan.records, plan.repo_resolver, evidence)
        rendered = ar.render_lock(
            dataclasses.replace(resolved, prior_lock_sha256=None, update_receipt_ref=outcome.receipt_ref)
        )
        self.assertEqual(
            rendered,
            ar.render_lock(
                dataclasses.replace(
                    resolved, prior_lock_sha256=None, update_receipt_ref=outcome.receipt_ref
                )
            ),
        )
        self.assertEqual(rendered, published)
        ar.verify_generated_lock(plan.manifest, published, plan.records, plan.repo_resolver, evidence)

    def test_candidate_c_poststage_smoke_failure_restores_b(self) -> None:
        # Restore is a construction-verified no-op (isolated staging + a pre-publish
        # failure), per the HO-ratified rollback semantics; no committed-lock
        # CAS-revert is exercised because T-2951 never rewinds a live checkout.
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)

        plan_c = self._plan(prior=b_lock, prior_bytes=b_bytes)
        outcome = self._run(
            plan_c,
            smoke=_FixtureRunner(decide=lambda ref, wd: False),  # C smoke fails post-stage
            paths=paths,
            expected_prior_digest=b_digest,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.SMOKE_FAILURE)
        # published lock still == B; prior revisions untouched; no C lock published.
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)
        self.assertEqual(outcome.published_lock_sha256, b_digest)
        # rerun-prior-health True.
        self.assertTrue(outcome.health_results and all(r.ok for r in outcome.health_results))
        # rolled-back receipt retains C diagnostics.
        digest = outcome.receipt_ref.split(":", 1)[1]
        receipt_json = json.loads(
            (paths["receipt_root"] / "pilot-assembly" / f"{digest}.receipt.json").read_bytes()
        )
        self.assertEqual(receipt_json["outcome"], "rolled-back")
        self.assertEqual(receipt_json["restored_lock_sha256"], b_digest)
        self.assertTrue(receipt_json["retained_diagnostics"])


# ---------------------------------------------------------------------------
# Parent refusal matrix, driven THROUGH the transaction
# ---------------------------------------------------------------------------


class TestLifecycleRefusalsThroughTransaction(_PilotBase):
    def _entry(self, logical_repo, target_path, *, tracking="main", pinned=None) -> CheckoutEntry:
        mode = CheckoutMode(pinned=pinned) if pinned is not None else CheckoutMode(tracking=tracking)
        return CheckoutEntry(
            checkout_id="lc",
            logical_repo=logical_repo,
            remote_ref="manifest",
            target_account="role:x",
            target_path=target_path,
            mode=mode,
            update_policy="ff-only",
        )

    def _reconciler(self, entry, remote_map) -> RepoReconciler:
        spec = CheckoutSpec(checkouts={entry.checkout_id: entry})
        return RepoReconciler(
            spec, probe=GitCheckoutProbe(), remote_resolver=_DictResolver(remote_map)
        )

    def _refuse_through(self, reconciler) -> ar.CompatibilityRefusal:
        outcome = self._run(self._plan(lifecycle=reconciler))
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.receipt_ref)
        self.assertFalse(Path(self.path("locks", "pilot.lock.json")).exists())
        return outcome.refusal

    def test_lifecycle_dirty_refuses_through_transaction(self) -> None:
        remote, _sha = self.repos["comp-a-repo"]
        seat = self.clone_seat(remote, "seat")
        (Path(seat) / "file.txt").write_text("uncommitted", encoding="utf-8")
        rec = self._reconciler(self._entry("comp-a-repo", seat), {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.DIRTY_CHECKOUT)

    def test_lifecycle_diverged_refuses_through_transaction(self) -> None:
        remote, _sha = self.repos["comp-a-repo"]
        seat = self.clone_seat(remote, "seat")
        (Path(seat) / "file.txt").write_text("local only", encoding="utf-8")
        _git("add", "file.txt", cwd=Path(seat))
        _git("commit", "-m", "local", cwd=Path(seat))
        rec = self._reconciler(self._entry("comp-a-repo", seat), {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.DIVERGED_CHECKOUT)

    def test_lifecycle_wrong_remote_refuses_through_transaction(self) -> None:
        remote, _sha = self.repos["comp-a-repo"]
        foreign = Path(self.path("seat"))
        foreign.mkdir(parents=True)
        (foreign / "IMPORTANT.txt").write_text("someone else's data", encoding="utf-8")
        rec = self._reconciler(self._entry("comp-a-repo", str(foreign)), {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.FOREIGN_CHECKOUT)

    def test_lifecycle_missing_ref_refuses_through_transaction(self) -> None:
        remote, _sha = self.repos["comp-a-repo"]
        entry = self._entry("comp-a-repo", self.path("seat"), tracking="nonexistent-branch")
        rec = self._reconciler(entry, {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.MISSING_REF)

    def test_rewound_surfaces_diverged_checkout(self) -> None:
        # Explicit fold: a remote force-rewound under an advanced seat classifies
        # DIVERGED_CHECKOUT (not a separate enum member).
        remote, _sha = self.repos["comp-a-repo"]
        seat = self.clone_seat(remote, "seat")
        (Path(seat) / "file.txt").write_text("seat line", encoding="utf-8")
        _git("add", "file.txt", cwd=Path(seat))
        _git("commit", "-m", "seat-line", cwd=Path(seat))
        rewind = Path(self.path("_rewind"))
        _git("init", "-b", "main", str(rewind))
        (rewind / "other.txt").write_text("rewound", encoding="utf-8")
        _git("add", "other.txt", cwd=rewind)
        _git("commit", "-m", "rewound", cwd=rewind)
        _git("push", "--force", remote, "main", cwd=rewind)
        _git("fetch", "origin", cwd=Path(seat))
        rec = self._reconciler(self._entry("comp-a-repo", seat), {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.DIVERGED_CHECKOUT)

    def test_wrong_identity_surfaces_foreign_checkout(self) -> None:
        # Explicit fold: a checkout whose origin identity != the resolved remote
        # classifies FOREIGN_CHECKOUT (not a separate enum member).
        remote, _sha = self.repos["comp-a-repo"]
        other, _osha = self.repos["comp-b-repo"]
        seat = self.clone_seat(remote, "seat")
        _git("remote", "set-url", "origin", other, cwd=Path(seat))
        rec = self._reconciler(self._entry("comp-a-repo", seat), {"comp-a-repo": remote})
        self.assertEqual(self._refuse_through(rec).kind, RefusalKind.FOREIGN_CHECKOUT)


# ---------------------------------------------------------------------------
# Stage / smoke / health failures preserve the prior published lock
# ---------------------------------------------------------------------------


class TestPrePublishFailuresPreservePrior(_PilotBase):
    def test_stage_failure_preserves_prior(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        # comp-c pinned to a real-shape but absent revision: resolve passes
        # (tested_revision == resolved revision) but staging cannot find it.
        bogus = "0" * 40
        plan = self._plan(prior=b_lock, prior_bytes=b_bytes, revisions={"comp-c-repo": bogus})
        outcome = self._run(plan, paths=paths, expected_prior_digest=b_digest)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.MISSING_REF)
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)

    def test_smoke_failure_preserves_prior(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        outcome = self._run(
            self._plan(prior=b_lock, prior_bytes=b_bytes),
            smoke=_FixtureRunner(decide=lambda ref, wd: False),
            paths=paths,
            expected_prior_digest=b_digest,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.SMOKE_FAILURE)
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)

    def test_health_failure_preserves_prior(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        staging_root = paths["staging_root"]
        # health fails for the CANDIDATE staged tree only; the prior-health rerun
        # (a different workdir) passes, so this stays HEALTH_FAILURE not ROLLBACK.
        health = _FixtureRunner(decide=lambda ref, wd: Path(wd) != staging_root)
        outcome = self._run(
            self._plan(prior=b_lock, prior_bytes=b_bytes),
            health=health,
            paths=paths,
            expected_prior_digest=b_digest,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.HEALTH_FAILURE)
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)


# ---------------------------------------------------------------------------
# Crash / CAS boundary model (one injected fault per boundary)
# ---------------------------------------------------------------------------


class TestCrashBoundaries(_PilotBase):
    def test_crash_at_stage_leaves_prior(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        with self.assertRaises(FaultInjected):
            self._run(
                self._plan(prior=b_lock, prior_bytes=b_bytes),
                paths=paths,
                expected_prior_digest=b_digest,
                fault_at="stage-exact-sources",
            )
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)

    def test_crash_at_seal_receipt_leaves_prior(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        receipts_before = sorted(paths["receipt_root"].rglob("*.receipt.json"))
        with self.assertRaises(FaultInjected):
            self._run(
                self._plan(prior=b_lock, prior_bytes=b_bytes),
                paths=paths,
                expected_prior_digest=b_digest,
                fault_at="seal-receipt",
            )
        # lock still B; no new receipt sealed for the crashed attempt.
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)
        self.assertEqual(sorted(paths["receipt_root"].rglob("*.receipt.json")), receipts_before)

    def test_crash_before_publish_lock_commit_leaves_prior(self) -> None:
        # A crash BEFORE the commit point (fault_at raises before publish_lock
        # runs): the receipt was sealed (orphan, harmless) but the lock is EXACTLY
        # the prior — the commit never began. (The DURING-commit atomicity — that a
        # mid-write crash leaves prior-or-new, never a partial, and leaks no temp —
        # is proven by test_publish_lock_commit_is_atomic below.)
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        with self.assertRaises(FaultInjected):
            self._run(
                self._plan(prior=b_lock, prior_bytes=b_bytes),
                paths=paths,
                expected_prior_digest=b_digest,
                fault_at="publish-lock",
            )
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)
        ar.parse_assembly_lock(paths["lock_path"].read_bytes())

    def test_publish_lock_commit_is_atomic(self) -> None:
        # Genuine commit-boundary coverage: inject an os.replace failure DURING the
        # publish (the _atomic_write commit point) and assert the on-disk lock is
        # EXACTLY the prior lock, still parses, and no temp file leaked; then a
        # clean publish yields EXACTLY the new lock.
        import tools.component_catalogue as cc

        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        lock_path = paths["lock_path"]
        new_bytes = b_bytes + b"\n"  # any different bytes; publish_lock is content-agnostic

        real_replace = cc.os.replace

        def boom(src, dst):
            raise OSError("injected os.replace failure during publish commit")

        cc.os.replace = boom
        try:
            with self.assertRaises(OSError):
                ar.publish_lock(lock_path, new_bytes, expected_prior_digest=b_digest)
        finally:
            cc.os.replace = real_replace

        # exactly the prior lock; parses; no temp file leaked in the lock dir.
        self.assertEqual(lock_path.read_bytes(), b_bytes)
        ar.parse_assembly_lock(lock_path.read_bytes())
        leaked = [p.name for p in lock_path.parent.iterdir() if p.name != lock_path.name]
        self.assertEqual(leaked, [], f"temp file leaked during a failed commit: {leaked}")

        # success path: a clean publish yields EXACTLY the new lock.
        digest = ar.publish_lock(lock_path, new_bytes, expected_prior_digest=b_digest)
        self.assertEqual(lock_path.read_bytes(), new_bytes)
        self.assertEqual(digest, ar.lock_digest(new_bytes))

    def test_crash_at_converge_live_reheals(self) -> None:
        # converge-live is post-commit: the crash lands AFTER publish_lock, so the
        # new lock is durably committed. Convergence self-heals on re-run (a no-op
        # in the temp-fixture pilot). The committed lock is a valid generated lock.
        paths = self._paths()
        plan = self._plan()
        with self.assertRaises(FaultInjected):
            self._run(plan, paths=paths, expected_prior_digest=None, fault_at="converge-live")
        committed = paths["lock_path"].read_bytes()
        self.assertTrue(committed)
        ar.verify_generated_lock(
            plan.manifest, committed, plan.records, plan.repo_resolver, self._evidence_for(plan.manifest)
        )

    def test_publish_lock_cas_mismatch_refuses_stale(self) -> None:
        paths = self._paths()
        b_bytes, _b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        stale = "sha256:" + "0" * 64  # a wrong expected-prior digest
        outcome = self._run(
            self._plan(prior=b_lock, prior_bytes=b_bytes),
            paths=paths,
            expected_prior_digest=stale,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.STALE_LOCK_INPUT)
        self.assertIsNone(outcome.receipt_ref)
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)


# ---------------------------------------------------------------------------
# Rollback-failure honesty + BLOCKER-1 staging regression guard
# ---------------------------------------------------------------------------


class TestRollbackHonestyAndStagingGuard(_PilotBase):
    def test_rollback_failure_typed_without_false_health_claim(self) -> None:
        paths = self._paths()
        b_bytes, b_digest = self._publish_b(paths)
        b_lock = ar.parse_assembly_lock(b_bytes)
        # Smoke triggers a rollback; the rerun-prior-health then ALSO fails.
        outcome = self._run(
            self._plan(prior=b_lock, prior_bytes=b_bytes),
            smoke=_FixtureRunner(decide=lambda ref, wd: False),
            health=_FixtureRunner(decide=lambda ref, wd: False),
            paths=paths,
            expected_prior_digest=b_digest,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.refusal.kind, RefusalKind.ROLLBACK_FAILURE)
        # never a health-ok claim: the failing rerun IS in health_results.
        self.assertTrue(outcome.health_results)
        self.assertTrue(any(not r.ok for r in outcome.health_results))
        self.assertEqual(paths["lock_path"].read_bytes(), b_bytes)

    def test_staging_refuses_non_tip_pin_mismatch(self) -> None:
        """BLOCKER-1 guard: staging an ANCESTOR pin lands on the EXACT ancestor
        (never silently on LATEST), and an unresolvable pin fails closed
        MISSING_REF — not a fall-back to the tip."""
        remote, base = self.repos["comp-a-repo"]  # `base` is the ancestor commit
        tip = self.advance_remote(remote, content="comp-a-v2", name="comp-a")
        self.assertNotEqual(base, tip)

        comp = ar.LockedComponent(
            component_id="comp-a",
            logical_repo="comp-a-repo",
            path=None,
            artifact=None,
            revision=base,  # a non-tip (ancestor) pin
            artifact_digest="sha256:x",
            contract_versions=(),
            schema_versions=(),
            enabled_features=(),
            requirement_closure=(),
            compatibility_evidence=(),
            submodules=(),
        )
        staged_root = Path(self.path("stage-ancestor"))
        result = stage_exact_sources(
            [comp], staging_root=staged_root, remote_resolver=self._remote_resolver()
        )
        self.assertTrue(result.ok, result.refusal)
        (one,) = result.staged
        self.assertEqual(one.head, base)  # exact ancestor, NOT the advanced tip
        self.assertNotEqual(one.head, tip)
        head = _git("rev-parse", "HEAD", cwd=one.path).stdout.strip()
        self.assertEqual(head, base)

        # An unresolvable pin fails closed (MISSING_REF), never a tip fallback.
        bogus = dataclasses.replace(comp, revision="0" * 40)
        refused = stage_exact_sources(
            [bogus], staging_root=Path(self.path("stage-bogus")), remote_resolver=self._remote_resolver()
        )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.refusal.kind, RefusalKind.MISSING_REF)


if __name__ == "__main__":
    unittest.main()
