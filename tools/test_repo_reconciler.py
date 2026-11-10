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

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class DirectScriptEntrypoint(unittest.TestCase):
    def test_literal_file_path_reaches_argument_parser_without_pythonpath(self):
        script = Path(rr.__file__).resolve()
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run(
                [sys.executable, "-I", str(script), "--help"],
                cwd=raw,
                env={**os.environ, "PYTHONPATH": ""},
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checkout-lifecycle reconciler", result.stdout)
        self.assertIn("authorize", result.stdout)


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

    def _authorized_apply(self, rec: RepoReconciler, plan: rr.ReconcilePlan):
        self.assertEqual(len(plan.items), 1)
        auth = rec.issue_authorization(plan, plan.items[0].checkout_id)
        return rec.apply(plan, auth)


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
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
        self.assertFalse(receipt.materialized)
        self.assertEqual(receipt.installs, [])
        self.assertFalse(Path(self._path("seat")).exists())

    def test_apply_clone_then_verify_current(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
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
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
        self.assertEqual(receipt.advances, ["c"])
        self.assertFalse(receipt.rollback_supported)
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.rollback(receipt)
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
        receipt = self._authorized_apply(rec, plan)
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
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
        self.assertEqual(receipt.installs, ["c"])
        seat_path = Path(self._path("seat"))
        self.assertTrue(seat_path.exists())
        # a foreign dir the reconciler never installed sits next door.
        foreign = Path(self._path("foreign"))
        foreign.mkdir()
        (foreign / "keep.txt").write_text("not ours", encoding="utf-8")
        outcome = rec.rollback(receipt)
        self.assertTrue(outcome.success)
        self.assertFalse(seat_path.exists())  # recorded install reclaimed
        self.assertTrue(Path(outcome.quarantine_path).joinpath("file.txt").exists())
        self.assertTrue((foreign / "keep.txt").exists())  # unknown data untouched

    def test_remove_unrecorded_is_refused(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        seat = self._clone_seat(remote, "seat")  # pre-existing, NOT reconciler-installed
        marker = Path(seat) / "file.txt"
        entry = self._entry("c", target_path=seat)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        empty_receipt = rr.ApplyReceipt(materialized=True)  # nothing recorded as installed
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.remove("c", empty_receipt)
        self.assertTrue(marker.exists())  # never deleted

    def test_rollback_refuses_identity_changed_install(self) -> None:
        remote = self._new_remote()
        self._seed(remote)
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
        # the installed checkout's identity is changed out from under us.
        other = self._new_remote("other")
        _git("remote", "set-url", "origin", other, cwd=Path(self._path("seat")))
        outcome = rec.rollback(receipt)
        self.assertEqual(len(outcome.refusals), 1)
        self.assertEqual(outcome.refusals[0].kind, RefusalKind.FOREIGN_CHECKOUT)
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
        receipt = self._authorized_apply(rec, plan)
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
        receipt = self._authorized_apply(rec, plan)
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
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
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
        plan = rec.reconcile_plan()
        receipt = self._authorized_apply(rec, plan)
        self.assertEqual(receipt.installs, ["c"])
        self.assertIn("c", receipt.installed_sha)  # FIX2: install records HEAD sha
        self.assertTrue(Path(seat).exists())
        return rec, receipt, Path(seat)

    # -- rollback refuses -------------------------------------------------

    def test_rollback_refuses_dirty_uncommitted_edit(self) -> None:
        rec, receipt, seat = self._install()
        edited = seat / "file.txt"
        edited.write_text("USER UNCOMMITTED WORK", encoding="utf-8")
        outcome = rec.rollback(receipt)
        self.assertEqual([r.kind for r in outcome.refusals], [RefusalKind.DIRTY_CHECKOUT])
        self.assertTrue(seat.exists())  # never reaped
        self.assertEqual(edited.read_text(encoding="utf-8"), "USER UNCOMMITTED WORK")

    def test_rollback_refuses_untracked_foreign_file(self) -> None:
        rec, receipt, seat = self._install()
        foreign = seat / "UNTRACKED_IMPORTANT.txt"
        foreign.write_text("someone's unsaved data", encoding="utf-8")
        outcome = rec.rollback(receipt)
        self.assertEqual([r.kind for r in outcome.refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertTrue(seat.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(foreign.read_text(encoding="utf-8"), "someone's unsaved data")

    def test_rollback_refuses_user_local_commit(self) -> None:
        rec, receipt, seat = self._install()
        (seat / "file.txt").write_text("local work", encoding="utf-8")
        _git("add", "file.txt", cwd=seat)
        _git("commit", "-m", "user local commit", cwd=seat)
        head_after_commit = _git("rev-parse", "HEAD", cwd=seat).stdout.strip()
        outcome = rec.rollback(receipt)
        self.assertEqual([r.kind for r in outcome.refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertTrue(seat.exists())  # user's commit survives
        self.assertEqual(_git("rev-parse", "HEAD", cwd=seat).stdout.strip(), head_after_commit)

    # -- remove refuses ---------------------------------------------------

    def test_remove_refuses_dirty_uncommitted_edit(self) -> None:
        rec, receipt, seat = self._install()
        edited = seat / "file.txt"
        edited.write_text("USER UNCOMMITTED WORK", encoding="utf-8")
        outcome = rec.remove("c", receipt)
        self.assertEqual(outcome.refusals[0].kind, RefusalKind.DIRTY_CHECKOUT)
        self.assertTrue(seat.exists())
        self.assertEqual(edited.read_text(encoding="utf-8"), "USER UNCOMMITTED WORK")

    def test_remove_refuses_untracked_foreign_file(self) -> None:
        rec, receipt, seat = self._install()
        foreign = seat / "UNTRACKED_IMPORTANT.txt"
        foreign.write_text("someone's unsaved data", encoding="utf-8")
        outcome = rec.remove("c", receipt)
        self.assertEqual(outcome.refusals[0].kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(seat.exists())
        self.assertTrue(foreign.exists())

    def test_remove_refuses_user_local_commit(self) -> None:
        rec, receipt, seat = self._install()
        (seat / "file.txt").write_text("local work", encoding="utf-8")
        _git("add", "file.txt", cwd=seat)
        _git("commit", "-m", "user local commit", cwd=seat)
        outcome = rec.remove("c", receipt)
        self.assertEqual(outcome.refusals[0].kind, RefusalKind.FOREIGN_CHECKOUT)
        self.assertTrue(seat.exists())

    # -- succeeds only on a pristine identity+sha-matched clone -----------

    def test_rollback_succeeds_on_pristine_clone(self) -> None:
        rec, receipt, seat = self._install()
        outcome = rec.rollback(receipt)
        self.assertTrue(outcome.success)
        self.assertFalse(seat.exists())  # pristine install reclaimed
        self.assertTrue(Path(outcome.quarantine_path).is_dir())

    def test_remove_succeeds_on_pristine_clone(self) -> None:
        rec, receipt, seat = self._install()
        outcome = rec.remove("c", receipt)
        self.assertTrue(outcome.success)
        self.assertFalse(seat.exists())


class TestScopedPhase2Cutover(_BubbleSafe):
    def test_default_reconciler_remains_inert_without_scoped_authorization(self) -> None:
        remote = self._new_remote(); self._seed(remote)
        target = self._path("seat")
        rec = self._reconciler([self._entry("c", target_path=target)], {"demo": remote}, functional=True)
        receipt = rec.apply(rec.reconcile_checkout("c"))
        self.assertFalse(receipt.materialized); self.assertFalse(Path(target).exists())

    def test_scoped_authorization_cannot_widen_or_select_another_checkout(self) -> None:
        remote = self._new_remote(); self._seed(remote)
        a = self._entry("a", target_path=self._path("a")); b = self._entry("b", target_path=self._path("b"))
        rec = self._reconciler([a, b], {"demo": remote}, functional=True)
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.issue_authorization(rec.reconcile_plan(), "a")
        plan = rec.reconcile_checkout("a"); auth = rec.issue_authorization(plan, "a")
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.apply(rec.reconcile_checkout("b"), auth)

    def test_apply_revalidates_and_refuses_dirty_diverged_identity_or_head_drift(self) -> None:
        remote = self._new_remote(); self._seed(remote)
        targets = {n: self._clone_seat(remote, n) for n in ("dirty", "diverged", "identity", "head")}
        self._advance_remote(remote)
        entries = [self._entry(n, target_path=p) for n, p in targets.items()]
        rec = self._reconciler(entries, {"demo": remote}, functional=True)
        plans = {n: rec.reconcile_checkout(n) for n in targets}
        auths = {n: rec.issue_authorization(plans[n], n) for n in targets}
        Path(targets["dirty"], "local.txt").write_text("dirty", encoding="utf-8")
        _git("commit", "--allow-empty", "-m", "local divergence", cwd=Path(targets["diverged"]))
        _git("remote", "set-url", "origin", self._new_remote("other"), cwd=Path(targets["identity"]))
        _git("commit", "--allow-empty", "-m", "head drift", cwd=Path(targets["head"]))
        for name in targets:
            with self.subTest(name=name), self.assertRaises(rr.ScopedAuthorizationError):
                rec.apply(plans[name], auths[name])

    def test_rehearsal_clone_apply_verify_rollback_restores_absent_state(self) -> None:
        remote = self._new_remote(); wanted = self._seed(remote); target = self._path("seat")
        rec = self._reconciler([self._entry("c", target_path=target)], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); receipt = rec.apply(plan, rec.issue_authorization(plan, "c"))
        self.assertTrue(receipt.rollback_supported); self.assertEqual(receipt.installed_sha, {"c": wanted})
        self.assertEqual(rec.verify().items[0].observed_head, wanted)
        outcome = rec.rollback(receipt)
        self.assertTrue(outcome.success); self.assertFalse(Path(target).exists())
        self.assertTrue(Path(outcome.quarantine_path).is_dir())

    def test_rollback_refuses_equivalent_replacement_checkout_identity(self) -> None:
        remote = self._new_remote(); wanted = self._seed(remote); target = self._path("seat")
        rec = self._reconciler([self._entry("c", target_path=target)], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); receipt = rec.apply(plan, rec.issue_authorization(plan, "c"))
        recorded_identity = receipt.installed_path_identity["c"]

        # Replace the reconciler-owned directory with a separately-created clean
        # clone of the same origin and HEAD. Content equivalence is not ownership.
        Path(target).rename(self._path("original-clone"))
        _git("clone", remote, target)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=Path(target)).stdout.strip(), wanted)
        replacement_identity = {"device": Path(target).stat().st_dev, "inode": Path(target).stat().st_ino}
        self.assertNotEqual(replacement_identity, recorded_identity)

        outcome = rec.rollback(receipt)
        self.assertEqual([refusal.kind for refusal in outcome.refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertIn("filesystem identity", outcome.refusals[0].detail)
        self.assertTrue(Path(target).exists())
        self.assertEqual(_git("rev-parse", "HEAD", cwd=Path(target)).stdout.strip(), wanted)

    def test_rehearsal_receipt_refuses_tamper_stale_scope_and_unknown_paths(self) -> None:
        remote = self._new_remote(); self._seed(remote); target = self._path("seat")
        rec = self._reconciler([self._entry("c", target_path=target)], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); receipt = rec.apply(plan, rec.issue_authorization(plan, "c"))
        tampered = json.loads(receipt.to_bytes()); tampered["target_path"] = self._path("elsewhere")
        with self.assertRaises(rr.ScopedAuthorizationError):
            rr.ApplyReceipt.from_bytes(json.dumps(tampered).encode())
        wrong = rr.ApplyReceipt.from_bytes(receipt.to_bytes()); wrong.target_path = self._path("elsewhere"); wrong.seal()
        with self.assertRaises(rr.ScopedAuthorizationError): rec.rollback(wrong)
        unknown = rr.ApplyReceipt.from_bytes(receipt.to_bytes()); unknown.install_paths["extra"] = self._path("unknown"); unknown.seal()
        with self.assertRaises(rr.ScopedAuthorizationError): rec.rollback(unknown)
        _git("commit", "--allow-empty", "-m", "receipt stale", cwd=Path(target))
        outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success); self.assertTrue(Path(target).exists())

    def test_restart_intent_never_dispatches_in_phase2_without_built_broker(self) -> None:
        remote = self._new_remote(); self._seed(remote); broker = _RecordingBroker(available=True)
        contract = RestartContract(operation_family="service.restart", target_unit="kino.service")
        entry = self._entry("c", target_path=self._path("seat"), restart_contract=contract)
        rec = self._reconciler([entry], {"demo": remote}, broker=broker, functional=True)
        plan = rec.reconcile_checkout("c"); receipt = rec.apply(plan, rec.issue_authorization(plan, "c"))
        self.assertEqual(len(receipt.intents), 1); self.assertEqual(receipt.dispatched, []); self.assertEqual(broker.dispatched, [])

    def test_failed_clone_race_leaves_foreign_content_and_preserves_receipt_evidence(self) -> None:
        remote = self._new_remote(); self._seed(remote)
        work = self._clone_seat(remote, "work")
        _git("checkout", "-b", "side", cwd=Path(work))
        _git("commit", "--allow-empty", "-m", "side", cwd=Path(work)); _git("push", "origin", "side", cwd=Path(work))
        side = _git("rev-parse", "HEAD", cwd=Path(work)).stdout.strip(); target = self._path("seat")
        entry = self._entry("c", target_path=target, tracking=None, pinned=side)
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); auth = rec.issue_authorization(plan, "c"); original = rr._run_git
        def raced(args, **kwargs):
            result = original(args, **kwargs)
            if args and args[0] == "clone" and result.returncode == 0:
                Path(target, "foreign.txt").write_text("preserve", encoding="utf-8")
            return result
        with patch.object(rr, "_run_git", side_effect=raced): receipt = rec.apply(plan, auth)
        self.assertEqual(receipt.installs, []); self.assertEqual(receipt.failed_clone_paths, {"c": target})
        self.assertTrue(receipt.refused); self.assertEqual(Path(target, "foreign.txt").read_text(), "preserve")
        loaded = rr.ApplyReceipt.from_bytes(receipt.to_bytes())
        self.assertEqual(loaded.failed_clone_paths, receipt.failed_clone_paths)
        self.assertEqual([r.as_text() for r in loaded.refused], [r.as_text() for r in receipt.refused])

    def test_receipt_strict_types_fail_closed_and_roundtrip_intents_refusals(self) -> None:
        refusal = CompatibilityRefusal(RefusalKind.DIVERGED_CHECKOUT, "c", "kept")
        intent = BrokeredIntent("c", "service.restart", "kino.service")
        identity = {"c": {"device": 11, "inode": 22}}
        receipt = rr.ApplyReceipt(
            materialized=True,
            installed_path_identity=identity,
            intents=[intent],
            refused=[refusal],
        ).seal()
        loaded = rr.ApplyReceipt.from_bytes(receipt.to_bytes())
        self.assertEqual(loaded.intents, [intent]); self.assertEqual(loaded.refused, [refusal])
        self.assertEqual(loaded.installed_path_identity, identity)

        tampered = json.loads(receipt.to_bytes())
        tampered["installed_path_identity"]["c"]["inode"] = 23
        with self.assertRaisesRegex(rr.ScopedAuthorizationError, "digest mismatch"):
            rr.ApplyReceipt.from_bytes(json.dumps(tampered).encode())

        wrong_identity_type = json.loads(receipt.to_bytes())
        wrong_identity_type["installed_path_identity"]["c"]["inode"] = "22"
        typed_payload = {k: v for k, v in wrong_identity_type.items() if k != "receipt_digest"}
        wrong_identity_type["receipt_digest"] = rr._sha256_bytes(rr._json_bytes(typed_payload))
        with self.assertRaisesRegex(rr.ScopedAuthorizationError, "identity"):
            rr.ApplyReceipt.from_bytes(json.dumps(wrong_identity_type).encode())

        raw = json.loads(receipt.to_bytes()); raw["materialized"] = 1
        payload = {k: v for k, v in raw.items() if k != "receipt_digest"}
        raw["receipt_digest"] = rr._sha256_bytes(rr._json_bytes(payload))
        with self.assertRaises(rr.ScopedAuthorizationError):
            rr.ApplyReceipt.from_bytes(json.dumps(raw).encode())

    def test_remote_and_path_option_injection_refused_and_git_argv_delimited(self) -> None:
        entry = self._entry("c", target_path=self._path("seat"))
        rec = self._reconciler([entry], {"demo": "--upload-pack=evil"})
        self.assertEqual(rec.reconcile_checkout("c").items[0].state, rr.STATE_UNRESOLVED)
        calls = []
        def fake_run(args, **_kwargs):
            calls.append(list(args)); return subprocess.CompletedProcess(args, 1, "", "")
        with patch.object(rr, "_run_git", side_effect=fake_run): GitCheckoutProbe()._ls_remote("/tmp/repo.git")
        self.assertEqual(calls[0], ["ls-remote", "--", "/tmp/repo.git"])
        from tools.germinate.components import pantheon_config_git as pg
        handler = object.__new__(pg.PantheonConfigRepoGit); handler._timeout = 1
        with patch.object(pg.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertIsNone(handler._remote_tips("--evil")); handler._remote_tips("/tmp/repo.git")
        self.assertEqual(run.call_args.args[0][:4], ["git", "ls-remote", "--", "/tmp/repo.git"])

    def _capture_apply_argv(self, entry, remote_map, method):
        """Run a private apply step with `_run_git` recorded (T-5502)."""
        rec = self._reconciler([entry], remote_map)
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch.object(rr, "_run_git", side_effect=fake_run):
            getattr(rec, method)(entry, "a" * 40, MagicMock())
        return calls

    def test_clone_and_fetch_argv_delimit_url_before_operands(self) -> None:
        seat = self._path("seat")
        url = "/tmp/repo.git"
        clone = self._capture_apply_argv(self._entry("c", target_path=seat), {"demo": url}, "_do_clone")
        self.assertEqual(clone[0], ["clone", "--branch", "main", "--", url, seat])
        fetch = self._capture_apply_argv(self._entry("c", target_path=seat), {"demo": url}, "_do_ff_advance")
        self.assertEqual(fetch[0], ["-C", seat, "fetch", "--", url, "refs/heads/main"])
        pin = "b" * 40
        pinned = self._capture_apply_argv(
            self._entry("c", target_path=seat, tracking=None, pinned=pin), {"demo": url}, "_do_ff_advance"
        )
        self.assertEqual(pinned[0][:5], ["-C", seat, "fetch", "--", url])

    def test_dash_operands_refused_and_git_never_invoked(self) -> None:
        seat = self._path("seat")
        cases = [
            (self._entry("c", target_path=seat), {"demo": "-evil"}),
            (self._entry("c", target_path=seat), {"demo": ""}),
            (self._entry("c", target_path="-seat"), {"demo": "/tmp/repo.git"}),
            (self._entry("c", target_path=seat, tracking="-evil"), {"demo": "/tmp/repo.git"}),
        ]
        for entry, remote_map in cases:
            for method in ("_do_clone", "_do_ff_advance"):
                with self.subTest(entry=entry.target_path, tracking=entry.mode.tracking, remote=remote_map["demo"], m=method):
                    self.assertEqual(self._capture_apply_argv(entry, remote_map, method), [])

    def test_pinned_wildcard_fallback_fetch_argv_delimits_url(self) -> None:
        """T-6151: the fallback fetch is reached only when the exact-pin fetch fails."""
        seat = self._path("seat"); url = "/tmp/repo.git"; pin = "b" * 40
        calls = self._capture_apply_argv(
            self._entry("c", target_path=seat, tracking=None, pinned=pin), {"demo": url}, "_do_ff_advance"
        )
        self.assertEqual(
            calls,
            [
                ["-C", seat, "fetch", "--", url, "a" * 40],
                ["-C", seat, "fetch", "--", url, "+refs/heads/*:refs/remotes/origin/*"],
            ],
        )

    def _apply_step_receipt(self, entry, remote_map, method):
        rec = self._reconciler([entry], remote_map)
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, "", "")

        receipt = rr.ApplyReceipt(materialized=True)
        with patch.object(rr, "_run_git", side_effect=fake_run):
            getattr(rec, method)(entry, "a" * 40, receipt)
        return receipt, calls

    def test_unsafe_operand_refusal_lands_in_receipt_naming_entry_and_operand(self) -> None:
        """T-6151: an apply-time operand refusal is typed, named, and secret-free."""
        seat = self._path("seat")
        cases = [
            (self._entry("c", target_path=seat), {"demo": "-evil-secret"}, "url", RefusalKind.UNRESOLVED_SOURCE),
            (self._entry("c", target_path=seat), {"demo": ""}, "url", RefusalKind.UNRESOLVED_SOURCE),
            (self._entry("c", target_path="-seat"), {"demo": "/tmp/repo.git"}, "target_path", RefusalKind.LIFECYCLE_REFUSED),
            (self._entry("c", target_path=seat, tracking="-evil"), {"demo": "/tmp/repo.git"}, "tracking", RefusalKind.LIFECYCLE_REFUSED),
        ]
        for entry, remote_map, operand, kind in cases:
            for method in ("_do_clone", "_do_ff_advance"):
                with self.subTest(operand=operand, url=remote_map["demo"], m=method):
                    receipt, calls = self._apply_step_receipt(entry, remote_map, method)
                    self.assertEqual(calls, [])
                    self.assertEqual((receipt.installs, receipt.advances), ([], []))
                    self.assertEqual(len(receipt.refused), 1)
                    refusal = receipt.refused[0]
                    self.assertEqual(refusal.kind, kind)
                    self.assertEqual(refusal.subject_id, "c")
                    self.assertIn(operand, refusal.detail)
                    self.assertNotIn("secret", refusal.detail)
                    self.assertNotIn("-evil", refusal.detail)

    def test_unsafe_operand_refusal_roundtrips_and_blocks_rollback(self) -> None:
        entry = self._entry("c", target_path="-seat")
        receipt, _calls = self._apply_step_receipt(entry, {"demo": "/tmp/repo.git"}, "_do_clone")
        receipt.checkout_id = "c"; receipt.target_path = "-seat"; receipt.action = rr.ACTION_CLONE
        sealed = receipt.seal()
        loaded = rr.ApplyReceipt.from_bytes(sealed.to_bytes())
        self.assertEqual([r.as_text() for r in loaded.refused], [r.as_text() for r in sealed.refused])
        self.assertEqual(loaded.receipt_digest, sealed.receipt_digest)
        rec = self._reconciler([entry], {"demo": "/tmp/repo.git"})
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.rollback(loaded)

    def test_apply_end_to_end_unsafe_target_path_reports_refusal(self) -> None:
        """T-6151 end to end through apply(): target_path is checked only at apply time."""
        remote = self._new_remote(); self._seed(remote)
        entry = self._entry("c", target_path="-seat")
        rec = self._reconciler([entry], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); auth = rec.issue_authorization(plan, "c")
        calls: list[list[str]] = []
        original = rr._run_git

        def spy(args, **kwargs):
            calls.append(list(args))
            return original(args, **kwargs)

        with patch.object(rr, "_run_git", side_effect=spy):
            receipt = rec.apply(plan, auth)
        self.assertEqual([c for c in calls if c[0] in ("clone", "fetch")], [])
        self.assertEqual((receipt.installs, receipt.advances), ([], []))
        self.assertEqual([(r.subject_id, r.kind) for r in receipt.refused], [("c", RefusalKind.LIFECYCLE_REFUSED)])
        self.assertIn("target_path", receipt.refused[0].detail)
        self.assertEqual(json.loads(receipt.to_bytes())["refused"][0]["subject_id"], "c")

    def test_authorized_rehearsal_cli_real_git_integration_isolated(self) -> None:
        real_root = Path(__file__).resolve().parent.parent
        real_head = _git("-C", str(real_root), "rev-parse", "HEAD").stdout.strip()
        remote = self._new_remote(); self._seed(remote); checkout_dir = Path(self._path("checkouts")); checkout_dir.mkdir()
        target = self._path("rehearsal")
        checkout_dir.joinpath("global.yaml").write_text(
            "schema_version: 1\ncheckouts:\n  - checkout_id: rehearsal\n    logical_repo: demo\n"
            "    remote_ref: manifest\n    target_account: fixture\n" + f"    target_path: {target}\n" +
            "    mode: {tracking: main}\n    update_policy: ff-only\n", encoding="utf-8")
        auth = Path(self._path("auth.json")); receipt = Path(self._path("receipt.json"))
        common = ["--checkouts-dir", str(checkout_dir), "--repo-root", str(self.root), "--pin-path", self._path("unused"), "--checkout-id", "rehearsal"]
        with patch.object(rr, "_production_resolver", return_value=_DictResolver({"demo": remote})):
            self.assertEqual(rr.main(["authorize", *common, "--authorization-out", str(auth)]), 0)
            self.assertEqual(rr.main(["apply", *common, "--authorization", str(auth), "--receipt-out", str(receipt)]), 0)
            self.assertEqual(rr.main(["verify", *common, "--receipt", str(receipt)]), 0)
            stdout = SimpleNamespace(buffer=io.BytesIO())
            with patch("sys.stdout", stdout):
                self.assertEqual(rr.main(["rollback", *common, "--receipt", str(receipt)]), 0)
        rollback_report = json.loads(stdout.buffer.getvalue())
        self.assertTrue(rollback_report["rolled_back"])
        self.assertFalse(rollback_report["resumed"])
        self.assertEqual(rollback_report["refusals"], [])
        self.assertTrue(Path(rollback_report["quarantine_path"]).is_dir())
        self.assertFalse(Path(target).exists())
        self.assertEqual(_git("-C", str(real_root), "rev-parse", "HEAD").stdout.strip(), real_head)


class TestQuarantineRollback(_BubbleSafe):
    def _install(self, suffix: str = ""):
        remote_name = f"remote{suffix}"; seed_name = f"seed{suffix}"; target_name = f"seat{suffix}"
        remote = self._new_remote(remote_name); wanted = self._seed(remote, name=seed_name); target = self._path(target_name)
        rec = self._reconciler([self._entry("c", target_path=target)], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c")
        receipt = rec.apply(plan, rec.issue_authorization(plan, "c"))
        return rec, receipt, Path(target), remote, wanted

    def test_forged_unsealed_receipt_refuses_without_moving_anything(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        receipt.receipt_digest = ""
        with self.assertRaisesRegex(rr.ScopedAuthorizationError, "sealed"):
            rec.rollback(receipt)
        self.assertTrue(target.is_dir())

    def test_recorded_path_different_from_spec_target_refuses(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        receipt.target_path = self._path("elsewhere")
        receipt.install_paths = {"c": receipt.target_path}
        receipt.seal()
        with self.assertRaisesRegex(rr.ScopedAuthorizationError, "spec target"):
            rec.rollback(receipt)
        self.assertTrue(target.is_dir())

    def test_semantic_exactness_rejects_ambiguous_clone_receipts(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        refusal = CompatibilityRefusal(RefusalKind.FOREIGN_CHECKOUT, "c", "ambiguous")
        mutations = {
            "wrong-action": lambda r: setattr(r, "action", ACTION_FF_ADVANCE),
            "rollback-disabled": lambda r: setattr(r, "rollback_supported", False),
            "missing-install": lambda r: r.installs.clear(),
            "advance-present": lambda r: r.advances.append("c"),
            "extra-path": lambda r: r.install_paths.__setitem__("extra", self._path("extra")),
            "sha-mismatch": lambda r: r.installed_sha.__setitem__("c", "f" * 40),
            "failed-clone": lambda r: r.failed_clone_paths.__setitem__("c", str(target)),
            "refusal-present": lambda r: r.refused.append(refusal),
        }
        for name, mutate in mutations.items():
            forged = rr.ApplyReceipt.from_bytes(receipt.to_bytes()); mutate(forged); forged.seal()
            with self.subTest(name=name), self.assertRaises(rr.ScopedAuthorizationError):
                rec.rollback(forged)
            self.assertTrue(target.is_dir())

    def test_target_symlink_refuses_without_following(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        owned = Path(self._path("owned")); target.rename(owned); target.symlink_to(owned, target_is_directory=True)
        outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success)
        self.assertEqual([r.kind for r in outcome.refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertTrue(target.is_symlink()); self.assertTrue(owned.is_dir())

    def test_precreated_quarantine_refuses_and_preserves_target(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        container, _payload = rr._quarantine_paths(receipt, target)
        container.mkdir(mode=0o700)
        outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success); self.assertTrue(target.is_dir())
        self.assertTrue(container.is_dir())

    def test_final_path_swap_is_moved_detected_and_restored_without_delete(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        owned_aside = Path(self._path("owned-aside")); original = rr._rename_noreplace
        calls = 0
        def swap_then_rename(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                source.rename(owned_aside)
                source.mkdir(); source.joinpath("foreign.txt").write_text("preserve", encoding="utf-8")
            original(source, destination)
        with patch.object(rr, "_rename_noreplace", side_effect=swap_then_rename):
            outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success)
        self.assertEqual([r.kind for r in outcome.refusals], [RefusalKind.FOREIGN_CHECKOUT])
        self.assertEqual(target.joinpath("foreign.txt").read_text(encoding="utf-8"), "preserve")
        self.assertTrue(owned_aside.is_dir())
        self.assertIsNone(outcome.quarantine_path)

    def test_final_path_swap_with_repopulated_target_preserves_both_paths(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        owned_aside = Path(self._path("owned-aside")); original = rr._rename_noreplace; calls = 0
        def swap_move_and_repopulate(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                source.rename(owned_aside)
                source.mkdir(); source.joinpath("moved-foreign.txt").write_text("moved", encoding="utf-8")
                original(source, destination)
                source.mkdir(); source.joinpath("new-foreign.txt").write_text("new", encoding="utf-8")
                return
            original(source, destination)
        with patch.object(rr, "_rename_noreplace", side_effect=swap_move_and_repopulate):
            outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success); self.assertIsNotNone(outcome.quarantine_path)
        self.assertEqual(target.joinpath("new-foreign.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual(Path(outcome.quarantine_path).joinpath("moved-foreign.txt").read_text(encoding="utf-8"), "moved")
        self.assertTrue(owned_aside.is_dir())

    def test_target_repopulated_after_payload_identity_check_refuses_and_preserves_both(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        _container, payload = rr._quarantine_paths(receipt, target)
        original_identity = rr._filesystem_identity

        def identity_then_repopulate(path: Path):
            identity = original_identity(path)
            if path == payload:
                target.mkdir()
                target.joinpath("foreign.txt").write_text("preserve", encoding="utf-8")
            return identity

        with patch.object(rr, "_filesystem_identity", side_effect=identity_then_repopulate):
            outcome = rec.rollback(receipt)

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.quarantine_path, str(payload))
        self.assertEqual(target.joinpath("foreign.txt").read_text(encoding="utf-8"), "preserve")
        self.assertTrue(payload.is_dir())
        self.assertIn(str(target), outcome.refusals[0].detail)
        self.assertIn(str(payload), outcome.refusals[0].detail)

    def test_final_target_absence_lookup_error_refuses_without_moving_quarantine(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        _container, payload = rr._quarantine_paths(receipt, target)
        original_lstat = rr.os.lstat

        def fail_final_target_lookup(path):
            if Path(path) == target and payload.exists() and not target.exists():
                raise PermissionError("indeterminate final target lookup")
            return original_lstat(path)

        with patch.object(rr.os, "lstat", side_effect=fail_final_target_lookup):
            outcome = rec.rollback(receipt)

        self.assertFalse(outcome.success)
        self.assertFalse(target.exists())
        self.assertTrue(payload.is_dir())
        self.assertEqual(outcome.quarantine_path, str(payload))
        self.assertIn(str(target), outcome.refusals[0].detail)
        self.assertIn(str(payload), outcome.refusals[0].detail)

    def test_success_quarantines_exact_clone_without_deleting_bytes(self) -> None:
        rec, receipt, target, _remote, wanted = self._install()
        recorded = receipt.installed_path_identity["c"]
        outcome = rec.rollback(receipt)
        self.assertTrue(outcome.success); self.assertFalse(outcome.resumed)
        self.assertFalse(target.exists()); self.assertIsNotNone(outcome.quarantine_path)
        payload = Path(outcome.quarantine_path)
        self.assertTrue(payload.is_dir())
        self.assertEqual(payload.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual({"device": payload.stat().st_dev, "inode": payload.stat().st_ino}, recorded)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=payload).stdout.strip(), wanted)

    def test_matching_quarantine_resume_is_idempotent_and_foreign_is_refused(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install()
        first = rec.rollback(receipt); second = rec.rollback(receipt)
        self.assertTrue(first.success); self.assertTrue(second.success); self.assertTrue(second.resumed)
        self.assertEqual(first.quarantine_path, second.quarantine_path)

        rec2, receipt2, target2, _remote2, _wanted2 = self._install("-foreign")
        container, payload = rr._quarantine_paths(receipt2, target2)
        target2.rename(self._path("owned-second")); container.mkdir(mode=0o700); payload.mkdir()
        payload.joinpath("foreign.txt").write_text("keep", encoding="utf-8")
        outcome = rec2.rollback(receipt2)
        self.assertFalse(outcome.success); self.assertTrue(payload.joinpath("foreign.txt").exists())

        rec3, receipt3, target3, _remote3, _wanted3 = self._install("-empty")
        empty_container, _empty_payload = rr._quarantine_paths(receipt3, target3)
        target3.rename(self._path("owned-third")); empty_container.mkdir(mode=0o700)
        empty = rec3.rollback(receipt3)
        self.assertFalse(empty.success); self.assertTrue(empty_container.is_dir())

        rec4, receipt4, _target4, _remote4, _wanted4 = self._install("-ambiguous")
        first4 = rec4.rollback(receipt4); payload4 = Path(first4.quarantine_path)
        payload4.parent.joinpath("foreign.txt").write_text("keep", encoding="utf-8")
        ambiguous = rec4.rollback(receipt4)
        self.assertFalse(ambiguous.success)
        self.assertTrue(payload4.is_dir())
        self.assertTrue(payload4.parent.joinpath("foreign.txt").is_file())

    def test_nonzero_git_status_refuses_as_indeterminate(self) -> None:
        rec, receipt, target, _remote, _wanted = self._install(); original = rr._run_git
        def fail_status(args, **kwargs):
            if "status" in args:
                return subprocess.CompletedProcess(args, 128, "", "indeterminate")
            return original(args, **kwargs)
        with patch.object(rr, "_run_git", side_effect=fail_status):
            outcome = rec.rollback(receipt)
        self.assertFalse(outcome.success); self.assertTrue(target.is_dir())

    def test_partial_failed_clone_evidence_roundtrips_and_rollback_refuses(self) -> None:
        remote = self._new_remote(); self._seed(remote); target = Path(self._path("partial"))
        rec = self._reconciler([self._entry("c", target_path=str(target))], {"demo": remote}, functional=True)
        plan = rec.reconcile_checkout("c"); auth = rec.issue_authorization(plan, "c"); original = rr._run_git
        def partial_clone(args, **kwargs):
            if args and args[0] == "clone":
                target.mkdir(); target.joinpath("foreign.partial").write_text("keep", encoding="utf-8")
                return subprocess.CompletedProcess(args, 1, "", "clone failed")
            return original(args, **kwargs)
        with patch.object(rr, "_run_git", side_effect=partial_clone):
            receipt = rec.apply(plan, auth)
        loaded = rr.ApplyReceipt.from_bytes(receipt.to_bytes())
        self.assertEqual(loaded.failed_clone_paths, {"c": str(target)})
        self.assertIn("c", loaded.failed_clone_identity); self.assertTrue(loaded.refused)
        with self.assertRaises(rr.ScopedAuthorizationError):
            rec.rollback(loaded)
        self.assertEqual(target.joinpath("foreign.partial").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
