"""tools/test_repo_reconciler.py — T-2951 fixture-git test matrix.

BUBBLE-SAFE DISCIPLINE (phase-1 hard constraint). Every git repo these tests touch
is a REAL git repo created under a per-test `tempfile.TemporaryDirectory()` — the
`_BubbleSafe` base class holds `self.root`, and EVERY path any test constructs
derives from `self.root`. No test names, reads, or mutates any path outside its
tempdir; no real/live checkout is ever operated on. The refusal tests additionally
assert the on-disk working-tree bytes are UNCHANGED after a plan+apply (never a
reset/clean/reap of dirty/diverged/foreign state).

A quick audit hook (satisfies the plan's "grep your test for the tempdir
discipline"): every `target_path` passed to a checkout entry flows through
`self._path(...)`, which asserts the result is under `self.root`.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools import assembly_resolver as ar
from tools.assembly_resolver import CompatibilityRefusal, LifecyclePlan, LockedComponent, RefusalKind
from tools.checkout_spec import (
    CheckoutEntry,
    CheckoutMode,
    CheckoutSpec,
    RestartContract,
    generate_checkout_spec_schema,
    load_checkout_spec,
    parse_checkout_file,
)
from tools.git_fixture import BubbleSafeCase, git as _git
from tools import repo_reconciler as rr
from tools.repo_reconciler import (
    ACTION_CLONE,
    ACTION_FF_ADVANCE,
    ACTION_NOOP,
    BrokeredIntent,
    CheckoutObservation,
    GitCheckoutProbe,
    RepoReconciler,
)


class _DictResolver:
    """logical_repo -> remote url; an unknown repo (or an empty url) means unresolved."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def __call__(self, logical_repo: str) -> str:
        return self._mapping.get(logical_repo, "")


class _RecordingBroker:
    """An ORG-063 broker that is NOT available (phase-1: unbuilt) and records every
    dispatch so a test can assert zero dispatches occurred."""

    def __init__(self, available: bool = False):
        self._available = available
        self.dispatched: list[BrokeredIntent] = []

    def available(self) -> bool:
        return self._available

    def dispatch(self, intent: BrokeredIntent) -> None:
        self.dispatched.append(intent)


class _StubProbe:
    """Returns a fixed observation regardless of entry — for the ordering-determinism
    test (no real git needed)."""

    def __init__(self, obs: CheckoutObservation):
        self._obs = obs

    def observe(self, entry: CheckoutEntry, remote_url: str) -> CheckoutObservation:
        return self._obs


class _BubbleSafe(BubbleSafeCase):
    """Thin subclass over the shared `git_fixture.BubbleSafeCase` (T-3183 §1). The
    tempdir lifecycle + bubble-safe git builders now live in `git_fixture`; the
    underscore-named methods below preserve this module's long-standing call
    style (`self._path`, `self._new_remote`, ...) so every existing test body is
    unchanged. Reconciler-specific fixtures (`_entry`/`_reconciler`/`_one`) stay
    here — they are not shared harness."""

    # -- bubble-safe path discipline + git builders (delegated) -----------

    def _path(self, *parts: str) -> str:
        return self.path(*parts)

    def _new_remote(self, name: str = "remote") -> str:
        return self.new_remote(name)

    def _seed(self, remote: str, *, content: str = "v1", name: str = "seed") -> str:
        return self.seed(remote, content=content, name=name)

    def _advance_remote(self, remote: str, *, content: str = "v2", name: str = "seed") -> str:
        return self.advance_remote(remote, content=content, name=name)

    def _clone_seat(self, remote: str, name: str) -> str:
        return self.clone_seat(remote, name)

    def _entry(
        self,
        checkout_id: str,
        *,
        logical_repo: str = "demo",
        target_path: str,
        tracking: str | None = "main",
        pinned: str | None = None,
        update_policy: str = "ff-only",
        health_contract: tuple[str, ...] = (),
        restart_contract: RestartContract | None = None,
    ) -> CheckoutEntry:
        mode = CheckoutMode(pinned=pinned) if pinned is not None else CheckoutMode(tracking=tracking)
        return CheckoutEntry(
            checkout_id=checkout_id,
            logical_repo=logical_repo,
            remote_ref="manifest",
            target_account="role:engineering-orchestrator-2",
            target_path=target_path,
            mode=mode,
            update_policy=update_policy,
            health_contract=health_contract,
            restart_contract=restart_contract,
        )

    def _reconciler(
        self,
        entries: list[CheckoutEntry],
        remote_map: dict[str, str],
        *,
        health_probe=None,
        broker=None,
        functional: bool = False,
        probe=None,
    ) -> RepoReconciler:
        spec = CheckoutSpec(checkouts={e.checkout_id: e for e in entries})
        return RepoReconciler(
            spec,
            probe=probe or GitCheckoutProbe(),
            remote_resolver=_DictResolver(remote_map),
            health_probe=health_probe,
            broker=broker,
            functional_probe=(lambda: functional),
        )

    def _one(self, plan: rr.ReconcilePlan, checkout_id: str) -> rr.ReconcilePlanItem:
        for item in plan.items:
            if item.checkout_id == checkout_id:
                return item
        self.fail(f"no plan item for {checkout_id}")


# ---------------------------------------------------------------------------
# Spec loader / schema
# ---------------------------------------------------------------------------


class TestCheckoutSpecLoader(_BubbleSafe):
    def _write(self, text: str, name: str = "global.yaml") -> Path:
        d = Path(self._path("checkouts"))
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")
        return d

    _VALID = """schema_version: 1
checkouts:
  - checkout_id: demo-one
    logical_repo: demo-repo
    remote_ref: manifest
    target_account: role:engineering-orchestrator-2
    target_path: /home/suborch2/demo
    mode:
      tracking: main
    update_policy: ff-only
    health_contract: []
"""

    def test_loads_valid_spec(self) -> None:
        d = self._write(self._VALID)
        spec = load_checkout_spec(d)
        self.assertIn("demo-one", spec.checkouts)
        self.assertEqual(spec.checkouts["demo-one"].mode.desired, "main")

    def test_refuses_raw_url_logical_repo(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "git@github.com:org/demo.git",
                    "target_account": "role:x",
                    "target_path": "/home/x/demo",
                    "mode": {"tracking": "main"},
                    "update_policy": "ff-only",
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml")

    def test_refuses_both_modes(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "/home/x/demo",
                    "mode": {"tracking": "main", "pinned": "abc1234"},
                    "update_policy": "ff-only",
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml")

    def test_refuses_neither_mode(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "/home/x/demo",
                    "mode": {},
                    "update_policy": "ff-only",
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml")

    def test_refuses_unknown_key(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "/home/x/demo",
                    "mode": {"tracking": "main"},
                    "update_policy": "ff-only",
                    "surprise": True,
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml")

    def test_refuses_relative_target_path(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "relative/path",
                    "mode": {"tracking": "main"},
                    "update_policy": "ff-only",
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml")

    def test_known_repos_cross_reference(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "not-registered",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "/home/x/demo",
                    "mode": {"tracking": "main"},
                    "update_policy": "ff-only",
                }
            ],
        }
        with self.assertRaises(Exception):
            parse_checkout_file(data, source_file="t.yaml", known_repos=frozenset({"other"}))

    def test_duplicate_checkout_id_across_files(self) -> None:
        d = self._write(self._VALID)
        (d / "extra.yaml").write_text(self._VALID, encoding="utf-8")
        with self.assertRaises(Exception):
            load_checkout_spec(d)

    def test_restart_contract_parsed(self) -> None:
        data = {
            "schema_version": 1,
            "checkouts": [
                {
                    "checkout_id": "x",
                    "logical_repo": "demo",
                    "remote_ref": "manifest",
                    "target_account": "acct",
                    "target_path": "/home/x/demo",
                    "mode": {"tracking": "main"},
                    "update_policy": "ff-only",
                    "restart_contract": {
                        "operation_family": "service.restart",
                        "target_unit": "kino.service",
                    },
                }
            ],
        }
        (entry,) = parse_checkout_file(data, source_file="t.yaml")
        self.assertEqual(entry.restart_contract.operation_family, "service.restart")
        self.assertEqual(entry.restart_contract.target_unit, "kino.service")


class TestSchemaByteStable(_BubbleSafe):
    def test_generated_schema_byte_identical_to_committed(self) -> None:
        committed = Path(__file__).resolve().parents[1] / "schemas" / "checkout-spec.schema.json"
        self.assertTrue(committed.is_file(), "schemas/checkout-spec.schema.json must exist")
        self.assertEqual(committed.read_bytes(), generate_checkout_spec_schema())


# ---------------------------------------------------------------------------
# Plan-state matrix (real fixture git)
# ---------------------------------------------------------------------------


class TestPlanStates(_BubbleSafe):
    def test_absent_plans_clone(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_ABSENT)
        self.assertEqual(item.action, ACTION_CLONE)

    def test_current_is_noop_and_convergent(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        first = rec.reconcile_plan()
        item = self._one(first, "c")
        self.assertEqual(item.state, rr.STATE_CURRENT)
        self.assertEqual(item.action, ACTION_NOOP)
        # Convergent: a 2nd run yields byte-identical report.
        self.assertEqual(first.to_report_bytes(), rec.reconcile_plan().to_report_bytes())

    def test_behind_plans_ff_advance(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        self._advance_remote(remote)  # remote now ahead of seat
        _git("fetch", "origin", cwd=Path(seat))  # seat's fetch cadence populated objects
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_BEHIND)
        self.assertEqual(item.action, ACTION_FF_ADVANCE)

    def test_dirty_refuses_and_leaves_files_untouched(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        dirty_file = Path(seat) / "file.txt"
        dirty_file.write_text("LOCAL UNCOMMITTED EDIT", encoding="utf-8")
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_DIRTY)
        self.assertEqual(item.refusal.kind, RefusalKind.DIRTY_CHECKOUT)
        # never reset: the uncommitted edit survives plan AND an apply attempt.
        rec2 = self._reconciler([entry], {"demo": remote}, functional=True)
        rec2.apply(rec2.reconcile_plan())
        self.assertEqual(dirty_file.read_text(encoding="utf-8"), "LOCAL UNCOMMITTED EDIT")

    def test_local_ahead_diverges_no_rewrite(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        # local commit not on remote → HEAD ahead of the remote tip.
        (Path(seat) / "file.txt").write_text("local-only commit", encoding="utf-8")
        _git("add", "file.txt", cwd=Path(seat))
        _git("commit", "-m", "local", cwd=Path(seat))
        head_before = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_DIVERGED)
        self.assertEqual(item.refusal.kind, RefusalKind.DIVERGED_CHECKOUT)
        rec.apply(rec.reconcile_plan())  # must not rewrite
        self.assertEqual(_git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip(), head_before)

    def test_remote_rewind_diverges(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        # seat advances its own line…
        (Path(seat) / "file.txt").write_text("seat line", encoding="utf-8")
        _git("add", "file.txt", cwd=Path(seat))
        _git("commit", "-m", "seat-line", cwd=Path(seat))
        # …meanwhile the remote is force-rewound to a brand-new unrelated history.
        rewind = self._path("_rewind")
        _git("init", "-b", "main", rewind)
        (Path(rewind) / "other.txt").write_text("rewound", encoding="utf-8")
        _git("add", "other.txt", cwd=Path(rewind))
        _git("commit", "-m", "rewound", cwd=Path(rewind))
        _git("push", "--force", remote, "main", cwd=Path(rewind))
        _git("fetch", "origin", cwd=Path(seat))  # observe the rewound tip locally
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_DIVERGED)

    def test_pinned_mismatch_ahead_diverges(self) -> None:
        remote = self._new_remote()
        base = self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        # seat advances beyond the pin (pin = the base commit, HEAD is ahead of it).
        (Path(seat) / "file.txt").write_text("beyond pin", encoding="utf-8")
        _git("add", "file.txt", cwd=Path(seat))
        _git("commit", "-m", "beyond", cwd=Path(seat))
        entry = self._entry("c", target_path=seat, tracking=None, pinned=base)
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_DIVERGED)

    def test_missing_tracking_ref(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        entry = self._entry("c", target_path=seat, tracking="nonexistent-branch")
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_MISSING_REF)
        self.assertEqual(item.refusal.kind, RefusalKind.MISSING_REF)

    def test_missing_pinned_sha(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry(
            "c", target_path=self._path("seat"), tracking=None, pinned="deadbeefdead"
        )
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_MISSING_REF)

    def test_credential_failure_unreachable_remote(self) -> None:
        entry = self._entry("c", target_path=self._path("seat"))
        # remote points at a non-existent bare repo → ls-remote errors.
        rec = self._reconciler([entry], {"demo": self._path("gone.git")})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_CREDENTIAL_FAIL)
        self.assertEqual(item.refusal.kind, RefusalKind.CREDENTIAL_FAILURE)

    def test_health_failure_on_otherwise_current(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        entry = self._entry("c", target_path=seat, health_contract=("probe:x",))
        rec = self._reconciler([entry], {"demo": remote}, health_probe=lambda e: False)
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_HEALTH_FAIL)
        self.assertEqual(item.refusal.kind, RefusalKind.HEALTH_FAILURE)

    def test_foreign_content_refused_never_reaped(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        foreign_path = Path(self._path("seat"))
        foreign_path.mkdir(parents=True)
        marker = foreign_path / "IMPORTANT.txt"
        marker.write_text("someone else's data", encoding="utf-8")
        entry = self._entry("c", target_path=str(foreign_path))
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_FOREIGN)
        self.assertEqual(item.refusal.kind, RefusalKind.FOREIGN_CHECKOUT)
        # never reaped: apply must not touch foreign content.
        rec.apply(rec.reconcile_plan())
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "someone else's data")

    def test_unresolved_source(self) -> None:
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {})  # resolver returns "" → unresolved
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_UNRESOLVED)
        self.assertEqual(item.refusal.kind, RefusalKind.UNRESOLVED_SOURCE)


# ---------------------------------------------------------------------------
# update_policy semantics
# ---------------------------------------------------------------------------


class TestUpdatePolicy(_BubbleSafe):
    def test_availability_only_suppresses_advance(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        self._advance_remote(remote)
        _git("fetch", "origin", cwd=Path(seat))
        entry = self._entry("c", target_path=seat, update_policy="availability-only")
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_BEHIND)
        self.assertEqual(item.action, ACTION_NOOP)

    def test_never_suppresses_clone(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"), update_policy="never")
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")
        self.assertEqual(item.state, rr.STATE_ABSENT)
        self.assertEqual(item.action, ACTION_NOOP)


# ---------------------------------------------------------------------------
# apply / verify (fixture-only mutation, guarded-inert real path)
# ---------------------------------------------------------------------------


class TestApplyVerify(_BubbleSafe):
    def test_apply_inert_by_default_does_nothing(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote})  # functional=False (default)
        receipt = rec.apply(rec.reconcile_plan())
        self.assertFalse(receipt.materialized)
        self.assertEqual(receipt.installs, [])
        self.assertFalse(Path(self._path("seat")).exists())

    def test_apply_clone_then_verify_current(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        self.assertEqual(receipt.installs, ["c"])
        self.assertTrue((Path(self._path("seat")) / "file.txt").exists())
        # verify (re-plan) is convergent → CURRENT/noop.
        item = self._one(rec.verify(), "c")
        self.assertEqual(item.state, rr.STATE_CURRENT)

    def test_apply_ff_advance_then_verify_current(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        tip = self._advance_remote(remote)
        _git("fetch", "origin", cwd=Path(seat))
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        self.assertEqual(receipt.advances, ["c"])
        self.assertEqual(_git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip(), tip)
        self.assertEqual(self._one(rec.verify(), "c").state, rr.STATE_CURRENT)


# ---------------------------------------------------------------------------
# restart contract — guarded-inert brokered intent, NEVER dispatched
# ---------------------------------------------------------------------------


class TestRestartContractInert(_BubbleSafe):
    def test_intent_emitted_never_dispatched(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        rc = RestartContract(operation_family="service.restart", target_unit="kino.service")
        entry = self._entry("c", target_path=self._path("seat"), restart_contract=rc)
        broker = _RecordingBroker(available=False)
        rec = self._reconciler([entry], {"demo": remote}, broker=broker, functional=True)
        plan = rec.reconcile_plan()
        item = self._one(plan, "c")
        # the clone action carries a brokered intent, marked not-dispatched.
        self.assertIsNotNone(item.brokered_intent)
        self.assertFalse(item.brokered_intent.dispatched)
        self.assertEqual(item.brokered_intent.target_unit, "kino.service")
        # apply emits it into the receipt but dispatches NOTHING (broker unavailable).
        receipt = rec.apply(plan)
        self.assertEqual(len(receipt.intents), 1)
        self.assertEqual(receipt.dispatched, [])
        self.assertEqual(broker.dispatched, [])

    def test_no_intent_when_action_is_noop(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        rc = RestartContract(operation_family="service.restart", target_unit="kino.service")
        entry = self._entry("c", target_path=seat, restart_contract=rc)
        rec = self._reconciler([entry], {"demo": remote})
        item = self._one(rec.reconcile_plan(), "c")  # CURRENT → noop
        self.assertEqual(item.action, ACTION_NOOP)
        self.assertIsNone(item.brokered_intent)


# ---------------------------------------------------------------------------
# removal / rollback ownership — reclaim recorded-only, never delete unknown
# ---------------------------------------------------------------------------


class TestRemovalRollback(_BubbleSafe):
    def test_rollback_reclaims_recorded_only(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        installed = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([installed], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        self.assertEqual(receipt.installs, ["c"])
        seat_path = Path(self._path("seat"))
        self.assertTrue(seat_path.exists())
        # a foreign dir the reconciler never installed sits next door.
        foreign = Path(self._path("foreign"))
        foreign.mkdir()
        (foreign / "keep.txt").write_text("not ours", encoding="utf-8")
        refusals = rec.rollback(receipt)
        self.assertEqual(refusals, [])
        self.assertFalse(seat_path.exists())  # recorded install reclaimed
        self.assertTrue((foreign / "keep.txt").exists())  # unknown data untouched

    def test_remove_unrecorded_is_refused(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")  # pre-existing, NOT reconciler-installed
        marker = Path(seat) / "file.txt"
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        empty_receipt = rr.ApplyReceipt(materialized=True)  # nothing recorded as installed
        refusal = rec.remove("c", empty_receipt)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(marker.exists())  # never deleted

    def test_rollback_refuses_identity_changed_install(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        # the installed checkout's identity is changed out from under us.
        other = self._new_remote("other")
        _git("remote", "set-url", "origin", other, cwd=Path(self._path("seat")))
        refusals = rec.rollback(receipt)
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(Path(self._path("seat")).exists())  # not reaped


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism(_BubbleSafe):
    def _obs_current(self) -> CheckoutObservation:
        return CheckoutObservation(
            remote_reachable=True,
            desired_present=True,
            exists=True,
            is_managed=True,
            dirty=False,
            head_sha="a" * 40,
            ahead=0,
            behind=0,
            desired_resolvable=True,
        )

    def test_report_bytes_stable_across_ordering(self) -> None:
        e1 = self._entry("alpha", target_path=self._path("a"))
        e2 = self._entry("bravo", target_path=self._path("b"))
        probe = _StubProbe(self._obs_current())
        spec_a = CheckoutSpec(checkouts={"alpha": e1, "bravo": e2})
        spec_b = CheckoutSpec(checkouts={"bravo": e2, "alpha": e1})
        rec_a = RepoReconciler(spec_a, probe=probe, remote_resolver=_DictResolver({"demo": "x"}))
        rec_b = RepoReconciler(spec_b, probe=probe, remote_resolver=_DictResolver({"demo": "x"}))
        self.assertEqual(
            rec_a.reconcile_plan().to_report_bytes(),
            rec_b.reconcile_plan().to_report_bytes(),
        )


# ---------------------------------------------------------------------------
# T-3182 Protocol composition into assembly_resolver.plan_assembly_update
# ---------------------------------------------------------------------------


class TestProtocolComposition(_BubbleSafe):
    def _locked_component(self, logical_repo: str) -> LockedComponent:
        return LockedComponent(
            component_id="demo-component",
            logical_repo=logical_repo,
            path=None,
            artifact=None,
            revision="r1",
            artifact_digest="sha256:abc",
            contract_versions=(),
            schema_versions=(),
            enabled_features=(),
            requirement_closure=(),
            compatibility_evidence=(),
            submodules=(),
        )

    def _candidate_lock(self, component: LockedComponent) -> ar.AssemblyLock:
        return ar.AssemblyLock(
            schema_version=ar.SCHEMA_VERSION,
            assembly_id="demo-assembly",
            manifest_sha256="sha256:" + "0" * 64,
            catalogue_sha256="sha256:" + "0" * 64,
            generator_id="t",
            generator_version="1.0.0",
            resolution_order=ar.RESOLUTION_ORDER,
            components=(component,),
            acknowledged_implications=(),
            owned_obligations=(),
            smoke_evidence=(),
            health_evidence=(),
            prior_lock_sha256=None,
            update_receipt_ref=None,
        )

    def test_protocol_plan_ok_on_current(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        self._clone_seat(remote, "seat")
        entry = self._entry("c", logical_repo="demo", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote})
        result = rec.plan([self._locked_component("demo")])
        self.assertIsInstance(result, LifecyclePlan)
        self.assertTrue(result.ok)
        self.assertEqual(result.stages, ar.UPDATE_STAGES)

    def test_protocol_plan_refuses_on_dirty(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        (Path(seat) / "file.txt").write_text("dirty", encoding="utf-8")
        entry = self._entry("c", logical_repo="demo", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        result = rec.plan([self._locked_component("demo")])
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal.kind, RefusalKind.DIRTY_CHECKOUT)

    def test_composes_into_plan_assembly_update(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        self._clone_seat(remote, "seat")
        entry = self._entry("c", logical_repo="demo", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote})
        component = self._locked_component("demo")
        candidate = self._candidate_lock(component)
        update = ar.plan_assembly_update(None, candidate, rec)
        self.assertEqual(update.stages, ar.UPDATE_STAGES)
        self.assertTrue(update.lifecycle_plan.ok)

    def test_plan_assembly_update_surfaces_reconciler_refusal(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")
        (Path(seat) / "file.txt").write_text("dirty", encoding="utf-8")
        entry = self._entry("c", logical_repo="demo", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote})
        candidate = self._candidate_lock(self._locked_component("demo"))
        with self.assertRaises(ar.AssemblyResolverError) as ctx:
            ar.plan_assembly_update(None, candidate, rec)
        self.assertEqual(ctx.exception.refusal.kind, RefusalKind.DIRTY_CHECKOUT)


# ---------------------------------------------------------------------------
# FIX 1 (regression) — _do_ff_advance must fetch + advance to the DESIRED ref,
# never the remote's default branch (FETCH_HEAD). Reviewer probes: probe_track3
# (tracking a non-default branch advanced onto main), probe_track (silent no-op).
# ---------------------------------------------------------------------------


class TestFfAdvanceFetchesCorrectRef(_BubbleSafe):
    def _remote_with_two_branches(self):
        """Build a bare remote whose `main` and `release` both descend from a
        common ancestor A onto separate tips, and a seat cloned tracking release
        at A. Returns (remote, seat, seat_start_A, release_tip_B, main_tip_C)."""
        remote = self._new_remote()
        work = self._path("_w")
        _git("clone", remote, work)
        wp = Path(work)
        (wp / "f.txt").write_text("seed", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "A", cwd=wp)
        _git("push", "origin", "main", cwd=wp)
        _git("branch", "release", cwd=wp)
        _git("push", "origin", "release", cwd=wp)
        # seat tracks release, checked out at the common ancestor A.
        seat = self._path("seat")
        _git("clone", "-b", "release", remote, seat)
        seat_start = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        # advance main: A -> C (linear, separate content)
        (wp / "f.txt").write_text("MAIN-CONTENT-C", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "C", cwd=wp)
        _git("push", "origin", "main", cwd=wp)
        main_c = _git("rev-parse", "HEAD", cwd=wp).stdout.strip()
        # advance release: A -> B (separate line)
        _git("checkout", "release", cwd=wp)
        (wp / "g.txt").write_text("RELEASE-CONTENT-B", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "B", cwd=wp)
        _git("push", "origin", "release", cwd=wp)
        rel_b = _git("rev-parse", "HEAD", cwd=wp).stdout.strip()
        return remote, seat, seat_start, rel_b, main_c

    def test_tracking_advances_to_desired_branch_not_default(self) -> None:
        """probe_track3: a `tracking: release` checkout must ff to release's tip B,
        NOT the remote's default (main) tip C."""
        remote, seat, seat_start, rel_b, main_c = self._remote_with_two_branches()
        self.assertNotEqual(rel_b, main_c)
        entry = self._entry("c", target_path=seat, tracking="release")
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_plan()
        item = self._one(plan, "c")
        self.assertEqual(item.state, rr.STATE_BEHIND)
        self.assertEqual(item.action, ACTION_FF_ADVANCE)
        receipt = rec.apply(plan)
        self.assertEqual(receipt.advances, ["c"])
        seat_after = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        self.assertEqual(seat_after, rel_b, "must advance to release tip B")
        self.assertNotEqual(seat_after, main_c, "must NOT advance to main tip C (wrong branch)")
        # release's B-only file is present; the seat converged on release content.
        self.assertTrue((Path(seat) / "g.txt").exists())

    def test_pinned_advances_to_exact_pin_sha(self) -> None:
        """Pinned mode fetches the pin object (a non-default-branch tip) and ff's to
        THAT exact sha — the old bare `git fetch` left the pin un-fetched so the
        merge silently no-op'd at the default branch."""
        remote = self._new_remote()
        work = self._path("_w")
        _git("clone", remote, work)
        wp = Path(work)
        (wp / "f.txt").write_text("A", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "A", cwd=wp)
        _git("push", "origin", "main", cwd=wp)
        # seat clones at A (on default main).
        seat = self._clone_seat(remote, "seat")
        seat_start = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        # release advances A -> B (a non-default tip); main STAYS at A.
        _git("checkout", "-b", "release", cwd=wp)
        (wp / "f.txt").write_text("B", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "B", cwd=wp)
        _git("push", "origin", "release", cwd=wp)
        pin_b = _git("rev-parse", "HEAD", cwd=wp).stdout.strip()
        self.assertNotEqual(pin_b, seat_start)
        entry = self._entry("c", target_path=seat, tracking=None, pinned=pin_b)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_plan()
        item = self._one(plan, "c")
        self.assertEqual(item.state, rr.STATE_BEHIND)
        self.assertEqual(item.action, ACTION_FF_ADVANCE)
        receipt = rec.apply(plan)
        self.assertEqual(receipt.advances, ["c"])
        seat_after = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        self.assertEqual(seat_after, pin_b, "must advance to the exact pin sha")

    def test_tracking_non_default_converges_not_silent_noop(self) -> None:
        """probe_track: tracking a non-default branch whose remote diverges from
        main must CONVERGE (ff to the release tip), never silently no-op success at
        the stale tip (the old bare fetch fetched main, whose objects don't ff)."""
        remote = self._new_remote()
        work = self._path("_w")
        _git("clone", remote, work)
        wp = Path(work)
        (wp / "f.txt").write_text("main-v1", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "seed", cwd=wp)
        _git("push", "origin", "main", cwd=wp)
        _git("checkout", "-b", "release", cwd=wp)
        (wp / "f.txt").write_text("release-v1", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "rel", cwd=wp)
        _git("push", "origin", "release", cwd=wp)
        seat = self._path("seat")
        _git("clone", "-b", "release", remote, seat)
        seat_start = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        # advance BOTH branches; main diverges from release below the seat's tip.
        _git("checkout", "main", cwd=wp)
        (wp / "f.txt").write_text("main-v2", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "main2", cwd=wp)
        _git("push", "origin", "main", cwd=wp)
        main_tip = _git("rev-parse", "HEAD", cwd=wp).stdout.strip()
        _git("checkout", "release", cwd=wp)
        (wp / "f.txt").write_text("release-v2", encoding="utf-8")
        _git("add", ".", cwd=wp)
        _git("commit", "-m", "rel2", cwd=wp)
        _git("push", "origin", "release", cwd=wp)
        release_tip = _git("rev-parse", "HEAD", cwd=wp).stdout.strip()
        entry = self._entry("c", target_path=seat, tracking="release")
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        seat_after = _git("rev-parse", "HEAD", cwd=Path(seat)).stdout.strip()
        # honest convergence to the desired branch — not a stale-tip no-op, not main.
        self.assertEqual(receipt.advances, ["c"])
        self.assertEqual(seat_after, release_tip)
        self.assertNotEqual(seat_after, seat_start, "must not silently stay at the stale tip")
        self.assertNotEqual(seat_after, main_tip, "must not advance onto main")
        self.assertEqual((Path(seat) / "f.txt").read_text(encoding="utf-8"), "release-v2")


# ---------------------------------------------------------------------------
# FIX 2 (regression) — _reclaim (rollback + remove) must REFUSE, never delete, a
# clone that is dirty, holds untracked foreign files, or whose HEAD moved off the
# recorded installed sha. Reviewer probe: probe_reclaim.
# ---------------------------------------------------------------------------


class TestReclaimRefusesUnreconciledContent(_BubbleSafe):
    def _install(self):
        """Reconciler-install a fresh clone (functional apply) and return
        (rec, receipt, seat_path). The receipt records the installed HEAD sha."""
        remote = self._new_remote()
        self._seed(remote)
        seat = self._path("seat")
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_plan())
        self.assertEqual(receipt.installs, ["c"])
        self.assertIn("c", receipt.installed_sha)  # FIX2: install records HEAD sha
        self.assertTrue(Path(seat).exists())
        return rec, receipt, Path(seat)

    # -- rollback refuses -------------------------------------------------

    def test_rollback_refuses_dirty_uncommitted_edit(self) -> None:
        rec, receipt, seat = self._install()
        edited = seat / "file.txt"
        edited.write_text("USER UNCOMMITTED WORK", encoding="utf-8")
        refusals = rec.rollback(receipt)
        self.assertEqual([r.kind for r in refusals], [RefusalKind.DIRTY_CHECKOUT])
        self.assertTrue(seat.exists())  # never reaped
        self.assertEqual(edited.read_text(encoding="utf-8"), "USER UNCOMMITTED WORK")

    def test_rollback_refuses_untracked_foreign_file(self) -> None:
        rec, receipt, seat = self._install()
        foreign = seat / "UNTRACKED_IMPORTANT.txt"
        foreign.write_text("someone's unsaved data", encoding="utf-8")
        refusals = rec.rollback(receipt)
        self.assertEqual([r.kind for r in refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertTrue(seat.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "someone's unsaved data")

    def test_rollback_refuses_user_local_commit(self) -> None:
        rec, receipt, seat = self._install()
        (seat / "file.txt").write_text("local work", encoding="utf-8")
        _git("add", "file.txt", cwd=seat)
        _git("commit", "-m", "user local commit", cwd=seat)
        head_after_commit = _git("rev-parse", "HEAD", cwd=seat).stdout.strip()
        refusals = rec.rollback(receipt)
        self.assertEqual([r.kind for r in refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertTrue(seat.exists())  # user's commit survives
        self.assertEqual(_git("rev-parse", "HEAD", cwd=seat).stdout.strip(), head_after_commit)

    # -- remove refuses ---------------------------------------------------

    def test_remove_refuses_dirty_uncommitted_edit(self) -> None:
        rec, receipt, seat = self._install()
        edited = seat / "file.txt"
        edited.write_text("USER UNCOMMITTED WORK", encoding="utf-8")
        refusal = rec.remove("c", receipt)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.kind, RefusalKind.DIRTY_CHECKOUT)
        self.assertTrue(seat.exists())
        self.assertEqual(edited.read_text(encoding="utf-8"), "USER UNCOMMITTED WORK")

    def test_remove_refuses_untracked_foreign_file(self) -> None:
        rec, receipt, seat = self._install()
        foreign = seat / "UNTRACKED_IMPORTANT.txt"
        foreign.write_text("someone's unsaved data", encoding="utf-8")
        refusal = rec.remove("c", receipt)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(seat.exists())
        self.assertTrue(foreign.exists())

    def test_remove_refuses_user_local_commit(self) -> None:
        rec, receipt, seat = self._install()
        (seat / "file.txt").write_text("local work", encoding="utf-8")
        _git("add", "file.txt", cwd=seat)
        _git("commit", "-m", "user local commit", cwd=seat)
        refusal = rec.remove("c", receipt)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(seat.exists())

    # -- succeeds only on a pristine identity+sha-matched clone -----------

    def test_rollback_succeeds_on_pristine_clone(self) -> None:
        rec, receipt, seat = self._install()
        refusals = rec.rollback(receipt)
        self.assertEqual(refusals, [])
        self.assertFalse(seat.exists())  # pristine install reclaimed

    def test_remove_succeeds_on_pristine_clone(self) -> None:
        rec, receipt, seat = self._install()
        refusal = rec.remove("c", receipt)
        self.assertIsNone(refusal)
        self.assertFalse(seat.exists())


if __name__ == "__main__":
    unittest.main()
