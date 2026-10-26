"""T-4964: automatic broker compensation of a failed apply (ORG-052 section 5
"may"; the T-2917:D15 follow-up).

A FAILED apply whose receipt holds restorable mutations gets a broker-created
rollback operation, created atomically with the failed receipt, that inherits
the apply's authority record (no second grant) and is driven through the
T-2917 5d.2 rollback machinery unchanged: the D6 restorable set, the D16
content elision, the engine's whole-set drift refusal and reverse order, and
the reconciler's MAX_SPEC_REARMS cap. A lost result parked in manual_recovery
is never compensated. Off by default (``DriverContext.compensate``), so it is
inert until the T-2920 production entrypoint opts in.

Fixture evidence only: the real broker domain (journal, planner, operation
service, execution source) in a SYSTEM temp dir, with a fake executor port.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import test_host_control_driver as _driver_tests
from tools.test_host_control_driver import SUBJECT_UID, FakePort, _ok
from tools.test_host_control_hardening import _refused
from tools.test_host_control_reconciler import ServingPort, _ReconcileCase
from tools.host_control import wire
from tools.host_control.contracts import (
    ReconcileOutcome,
    COMPENSATION_APPROVAL_SENTINEL,
    COMPENSATION_KEY_PREFIX,
    OPERATION_SCHEMA,
    HostControlError,
    OperationRecord,
    canonical_json,
)
from tools.host_control.driver import DriverContext, DriverCore, OperationDriver
from tools.host_control.execution_source import SPEC_JSON_LIMIT, BrokerExecutionSource
from tools.host_control.executor import wire as executor_wire
from tools.host_control.executor_client import CollectedResult, ExecutorUnknownExecution
from tools.host_control.journal import MAX_SPEC_REARMS, BrokerJournal
from tools.host_control.operations import (
    OperationService,
    RESTORE_CLEANUP_AUTHORITY,
    RESTORE_INVERSE_ACTION,
    restorable_mutations,
)
from tools.persona_installer import MutationRecord

HOST_CONTROL = Path(__file__).resolve().parent / "host_control"


def _failed(mutations=(), detail: str = "apply failed") -> CollectedResult:
    return CollectedResult(
        ok=False,
        result=executor_wire.ExecutionResult(ok=False, mutations=tuple(mutations), evidence=(), detail=detail),
    )


class _CompensationCase(_ReconcileCase):
    def setUp(self) -> None:
        # the plan ruling: a SYSTEM temp dir, never one inside the repo
        with mock.patch.object(_driver_tests, "REPO_ROOT", Path(tempfile.gettempdir())):
            super().setUp()
        self.assertFalse(
            str(self.fixture.root).startswith(str(Path(__file__).resolve().parent.parent)),
            "fixture must live outside the repository",
        )

    def make_core(self, port, *, compensate: bool = True) -> DriverCore:
        return DriverCore(
            DriverContext(
                self.service, self.fixture.journal, port, lambda: None, reconcile=True, compensate=compensate
            ),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )

    def plan_op(self, kind: str):
        plan = self.fixture.journal.load_plan_unbound(self.plan.plan_handle)
        return next(op for op in plan.installer_plan.operations if op.kind == kind and op.inverse)

    def made(self, kind: str = "mkdir", before=None) -> MutationRecord:
        op = self.plan_op(kind)
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, dict(before or {"state": "absent"}), "directory")

    def receipt_for(self, handle: str, phase: str) -> str:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase=?",
                (handle, phase),
            ).fetchone()[0]

    def sentinel_count(self) -> int:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM operations WHERE approval_handle=?", (COMPENSATION_APPROVAL_SENTINEL,)
            ).fetchone()[0]

    def failed_apply(self, key: str, mutations=None, *, port=None, compensate: bool = True):
        """Drive one apply to a FAILED collected result; returns
        (apply handle, compensation record or None, core)."""

        handle = self.apply(key)
        port = port or FakePort(self.fixture.journal)
        port.collect_script = [_failed((self.made(),) if mutations is None else mutations)]
        core = self.make_core(port, compensate=compensate)
        self.assertEqual(core.drive(handle), "failed")
        return handle, self.fixture.journal.compensation_for(handle), core


class CompensationCreationTests(_CompensationCase):
    def test_failed_apply_with_restorable_mutations_creates_exactly_one_compensation_atomically(self) -> None:
        handle, compensation, core = self.failed_apply("c-one")
        self.assertIsNotNone(compensation)
        self.assertEqual((compensation.kind, compensation.state), ("rollback", "prepared"))
        self.assertEqual(compensation.approval_handle, COMPENSATION_APPROVAL_SENTINEL)
        self.assertEqual(compensation.idempotency_key, COMPENSATION_KEY_PREFIX + handle)
        self.assertEqual(compensation.receipt_handle, self.receipt_for(handle, "failed"))
        self.assertIsNone(compensation.authority_record_id)
        (result,) = self.events(handle, "result")
        self.assertEqual(result["compensation_operation_handle"], compensation.operation_handle)
        # born in the SAME transaction: the terminal and the prepared record
        # carry one timestamp
        source = self.fixture.journal.load_operation_unbound(handle)
        self.assertEqual(compensation.created_at, source.updated_at)
        self.assertEqual(core.followups, [compensation.operation_handle])
        self.assertEqual(self.sentinel_count(), 1)

    def test_compensation_inherits_apply_authority_and_never_calls_the_binder(self) -> None:
        handle, compensation, core = self.failed_apply("c-auth")
        binds = len(self.binder.calls)
        restored = dataclasses.replace(self.made(), before={"state": "directory", "mode": "0755"}, after_fingerprint=None)
        core._ctx.port.collect_script = [_ok((restored.operation_id,), (restored,))]
        self.assertEqual(core.drive(compensation.operation_handle), "completed")
        self.assertEqual(len(self.binder.calls), binds)  # no second grant
        source = self.fixture.journal.load_operation_unbound(handle)
        final = self.fixture.journal.load_operation_unbound(compensation.operation_handle)
        self.assertEqual(final.authority_record_id, source.authority_record_id)
        (authorized,) = self.events(compensation.operation_handle, "compensation_authorized")
        self.assertEqual(
            authorized, {"source_operation_handle": handle, "authority_record_id": source.authority_record_id}
        )

    def test_compensation_restores_only_intents_intersect_changed_records(self) -> None:
        made = self.made()
        op = self.plan_op("mkdir")
        noise = (
            dataclasses.replace(made, changed=False, operation_id=self.plan_op("atomic-write").operation_id,
                                kind="atomic-write", target=self.plan_op("atomic-write").target),
            MutationRecord("diag-no-intent", "harness-check", "codex", op.owner, True, {"state": "x"}, None),
            dataclasses.replace(made, owner="intruder"),
        )
        handle, compensation, _core = self.failed_apply("c-set", (made,) + noise)
        receipt = self.fixture.journal.get_receipt(subject=compensation.owner, receipt_handle=compensation.receipt_handle)
        expected = restorable_mutations(receipt, self.fixture.journal.intents_for(handle))
        self.assertEqual(expected, (made,))
        self.service.authorize(compensation.operation_handle)
        self.service.dispatch(compensation.operation_handle, FakePort(self.fixture.journal))
        (intent,) = self.fixture.journal.intents_for(compensation.operation_handle)
        self.assertEqual((intent.operation_id, intent.target), (made.operation_id, made.target))
        self.assertEqual(
            dict(intent.inverse),
            {
                "action": RESTORE_INVERSE_ACTION,
                "source_receipt_handle": compensation.receipt_handle,
                "cleanup_authority": RESTORE_CLEANUP_AUTHORITY,
            },
        )

    def test_no_restorable_mutation_means_no_compensation(self) -> None:
        _handle, compensation, core = self.failed_apply("c-none", ())
        self.assertIsNone(compensation)
        self.assertEqual(core.followups, [])
        self.assertEqual(self.sentinel_count(), 0)


class CompensationSpecTests(_CompensationCase):
    def test_compensation_spec_matches_a_grant_bound_rollback_and_elides_content(self) -> None:
        """D16 elision, the restorable set and its plan order are the SAME
        spec a grant-bound Rollback of that receipt gets, so the engine's
        whole-set drift refusal and reverse-order restore (pinned in
        test_host_control_rollback) apply unchanged."""

        big = "QUJD" * 40000
        made = self.made("atomic-write", before={"state": "file", "content_b64": big, "fingerprint": "f" * 64, "mode": "0644"})
        handle, compensation, core = self.failed_apply("c-spec", (made, self.made()))
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        self.service.authorize(compensation.operation_handle)
        dispatched = self.service.dispatch(compensation.operation_handle, FakePort(self.fixture.journal))
        spec = source.spec_for(dispatched)
        self.assertLess(len(canonical_json(spec).encode("utf-8")), SPEC_JSON_LIMIT)
        self.assertTrue(all("content" not in op["desired"] for op in spec["operations"]))
        # the compensation's whole-set drift refusal: the engine reports
        # failed with no mutations; the compensation ends failed
        self.service.append_result(
            compensation.operation_handle, ok=False, mutations=(), evidence=(), detail="drifted: refused before any change"
        )
        # ... which (the ruling) admits a grant-bound rollback of the receipt
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, compensation.receipt_handle, "c-spec-rb", "cd" * 32)
        self.service.authorize(rollback.operation_handle)
        manual = source.spec_for(self.service.dispatch(rollback.operation_handle, FakePort(self.fixture.journal)))
        for key in ("execution_kind", "operations", "rollback_mutations", "receipt_handle", "receipt_digest"):
            self.assertEqual(spec.get(key), manual.get(key), key)
        self.assertEqual(
            [m["operation_id"] for m in spec["rollback_mutations"]], [made.operation_id, self.made().operation_id]
        )


class CompensationRecoveryTests(_CompensationCase):
    def test_compensation_served_and_lost_rearms_then_parks_at_rearm_cap(self) -> None:
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        _handle, compensation, core = self.failed_apply("c-lost", port=port)
        comp = compensation.operation_handle
        outcomes = []
        for _ in range(MAX_SPEC_REARMS + 1):
            port.collect_script = [ExecutorUnknownExecution("gone"), ExecutorUnknownExecution("gone")]
            outcomes.append(self.make_core(port).drive(comp))
        self.assertEqual(outcomes, ["reconcile_pending"] * MAX_SPEC_REARMS + ["manual_recovery"])
        self.assertEqual(len(self.events(comp, "spec_rearmed")), MAX_SPEC_REARMS)
        (parked,) = self.events(comp, "irreconcilable")
        self.assertEqual(parked["detail"], "rearm-cap")
        self.assertEqual(self.probe.calls, [])

    def test_manual_recovery_park_never_compensates(self) -> None:
        handle, port = self.served_and_lost("c-park")
        self.set_observation("true")  # first mutating target at desired
        port.collect_script = [ExecutorUnknownExecution("gone")]
        core = self.make_core(port)
        self.assertEqual(core.drive(handle), "manual_recovery")
        self.assertIsNone(self.fixture.journal.compensation_for(handle))
        self.assertEqual(core.followups, [])
        self.assertEqual(self.sentinel_count(), 0)

    def test_no_compensation_for_other_failure_paths(self) -> None:
        # a completed apply
        handle = self.apply("c-ok")
        port = FakePort(self.fixture.journal)
        port.collect_script = [_ok((self.made().operation_id,), (self.made(),))]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        # a pre-engine refusal (nothing ran)
        refused = self.apply("c-refused")
        port.collect_script = [_refused()]
        self.assertEqual(self.make_core(port).drive(refused), "failed")
        # a failed verify
        verify = self.verify("c-verify")
        port.collect_script = [_failed((self.made(),))]
        self.assertEqual(self.make_core(port).drive(verify), "failed")
        # the reconciler's real ``restored`` outcome, driven through the
        # driver's lost-result path (service.recover -> _reconcile), with
        # compensation ON: it appends WITHOUT the flag
        made = self.made()

        class RestoringReconciler:
            def reconcile(self, operation, staged_receipt, unresolved_intents):
                return ReconcileOutcome("restored", (made,), (), "restored")

        restoring = OperationService(
            registry=self.fixture.registry,
            journal=self.fixture.journal,
            binder=self.binder,
            reconciler=RestoringReconciler(),
            clock=self.fixture.clock,
            # a disjoint handle space from self.service's factory
            handle_factory=iter(format((1 << 52) + n, "064x") for n in range(1, 1000)).__next__,
        )
        restored = self.apply("c-restored")
        lost = FakePort(self.fixture.journal)
        lost.collect_script = [ExecutorUnknownExecution("gone")]
        restored_core = DriverCore(
            DriverContext(restoring, self.fixture.journal, lost, lambda: None, reconcile=True, compensate=True),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )
        self.assertEqual(restored_core.drive(restored), "failed")
        self.assertEqual(self.events(restored, "result")[0]["detail"], "restored")
        self.assertEqual(restored_core.followups, [])
        # compensation off (the default context): the 5d behaviour
        _h, off, off_core = self.failed_apply("c-off", compensate=False)
        self.assertIsNone(off)
        self.assertEqual(off_core.followups, [])
        self.assertEqual(self.sentinel_count(), 0)

    def test_a_failed_compensation_is_never_compensated(self) -> None:
        _handle, compensation, core = self.failed_apply("c-chain")
        core._ctx.port.collect_script = [_failed((self.made(),))]
        core.followups.clear()
        self.assertEqual(core.drive(compensation.operation_handle), "failed")
        self.assertEqual(core.followups, [])
        self.assertEqual(self.sentinel_count(), 1)

    def test_compensation_insert_failure_rolls_back_the_terminal_too(self) -> None:
        handle = self.apply("c-atomic")
        self.service.authorize(handle)
        self.service.dispatch(handle, FakePort(self.fixture.journal))
        with mock.patch.object(BrokerJournal, "_insert_operation", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.service.append_result(
                    handle, ok=False, mutations=(self.made(),), evidence=(), detail="x", compensate=True
                )
        self.assertEqual(self.state(handle), "dispatched")
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            phases = [row[0] for row in connection.execute(
                "SELECT phase FROM operation_receipts WHERE operation_handle=?", (handle,)
            )]
        self.assertEqual(phases, ["staged"])
        self.assertEqual(self.sentinel_count(), 0)

    def test_prepared_compensation_is_recovered_at_driver_start(self) -> None:
        _handle, compensation, _core = self.failed_apply("c-restart")
        self.assertIn(compensation.operation_handle, self.fixture.journal.pending_operations())
        port = FakePort(self.fixture.journal)  # a fresh driver after a restart
        port.collect_script = [_ok()]
        self.assertEqual(self.make_core(port).drive(compensation.operation_handle), "completed")

    def test_driver_drives_the_compensation_after_the_failed_apply(self) -> None:
        handle = self.apply("c-driver")
        port = FakePort(self.fixture.journal)
        port.collect_script = [_failed((self.made(),)), _ok()]
        outcomes: list[tuple[str, str]] = []
        driver = OperationDriver(
            context_factory=lambda: None, sleeper=lambda seconds: None, on_outcome=lambda h, o: outcomes.append((h, o))
        )
        driver._drive_with_retries(self.make_core(port), handle)
        compensation = self.fixture.journal.compensation_for(handle)
        self.assertEqual(outcomes, [(handle, "failed"), (compensation.operation_handle, "completed")])

    def test_compensate_requires_reconcile(self) -> None:
        with self.assertRaises(HostControlError):
            DriverContext(self.service, self.fixture.journal, FakePort(self.fixture.journal), lambda: None, compensate=True)


class CompensationAuthorityTests(_CompensationCase):
    def test_compensation_sentinel_and_key_are_unforgeable(self) -> None:
        receipt_handle = self.receipt_for(self.failed_apply("c-forge")[0], "failed")
        with self.assertRaises(HostControlError):  # callers pass 64-hex only
            self.service.prepare_rollback(SUBJECT_UID, receipt_handle, "k", COMPENSATION_APPROVAL_SENTINEL)
        with self.assertRaises(HostControlError):  # the reserved key has a space
            self.service.prepare_apply(
                SUBJECT_UID, self.plan.plan_handle, self.plan.plan_digest, COMPENSATION_KEY_PREFIX + "x", "ab" * 32
            )
        base = self.fixture.journal.load_operation_unbound(self.apply("c-shape")).to_dict()
        base = {k: v for k, v in base.items() if k != "digest"}
        base["owner"] = self.fixture.journal.load_operation_unbound(self.apply("c-shape2")).owner
        for changes in (
            {"approval_handle": COMPENSATION_APPROVAL_SENTINEL, "idempotency_key": COMPENSATION_KEY_PREFIX + "a" * 64},
            {"idempotency_key": COMPENSATION_KEY_PREFIX + "a" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(HostControlError):
                OperationRecord.create(**{**base, "operation_families": tuple(base["operation_families"]), **changes})

    def test_authorize_refuses_a_compensation_whose_source_does_not_match(self) -> None:
        handle, compensation, _core = self.failed_apply("c-mismatch")
        other, _ignored, _c = self.failed_apply("c-other", ())  # a failed apply, never compensated
        forged = OperationRecord.create(
            **{
                **{k: v for k, v in compensation.to_dict().items() if k not in ("digest", "owner")},
                "owner": compensation.owner,
                "operation_families": tuple(compensation.operation_families),
                "operation_handle": "9" * 64,
                "idempotency_key": COMPENSATION_KEY_PREFIX + other,  # names the WRONG source
            }
        )
        self.fixture.journal.put_operation_or_replay(record=forged)
        binds = len(self.binder.calls)
        self.assertEqual(self.service.authorize(forged.operation_handle).state, "refused")
        self.assertEqual(len(self.binder.calls), binds)
        (refused,) = self.events(forged.operation_handle, "compensation_refused")
        self.assertEqual(refused["reason_code"], "compensation-source-mismatch")

    def test_grant_bound_rollback_refused_while_compensation_live_completed_or_parked(self) -> None:
        handle, compensation, core = self.failed_apply("c-policy")
        receipt = compensation.receipt_handle
        with self.assertRaisesRegex(HostControlError, "compensated by the broker"):  # prepared
            self.service.prepare_rollback(SUBJECT_UID, receipt, "p1", "cd" * 32)
        core._ctx.port.collect_script = [_ok()]
        self.assertEqual(core.drive(compensation.operation_handle), "completed")
        with self.assertRaisesRegex(HostControlError, "compensated by the broker"):  # completed
            self.service.prepare_rollback(SUBJECT_UID, receipt, "p2", "cd" * 32)
        parked_apply, parked, _c = self.failed_apply("c-policy-park")
        self.fixture.journal.mark_manual_recovery(parked.operation_handle, "operator", marked_at=self.service.now_iso())
        with self.assertRaisesRegex(HostControlError, "compensated by the broker"):  # manual_recovery
            self.service.prepare_rollback(SUBJECT_UID, parked.receipt_handle, "p3", "cd" * 32)
        # the parked compensation's json is untouched on purpose (P4); the
        # source apply's OperationGet still names it
        source = self.service.get_operation(SUBJECT_UID, parked_apply)
        self.assertEqual(self.service.compensation_handle(source), parked.operation_handle)

    def test_grant_bound_rollback_admitted_after_a_refused_compensation(self) -> None:
        _handle, compensation, _core = self.failed_apply("c-refused-comp")
        # the source cannot be re-derived at authorize: the compensation ends
        # ``refused`` without running anything
        with mock.patch.object(self.fixture.journal, "get_operation", side_effect=HostControlError("unreadable")):
            self.assertEqual(self.service.authorize(compensation.operation_handle).state, "refused")
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, compensation.receipt_handle, "after-refused", "cd" * 32)
        self.assertEqual((rollback.kind, rollback.state), ("rollback", "prepared"))

    def test_a_malformed_compensation_is_never_written(self) -> None:
        other = "e" * 64
        real = self.service._compensation_for_failure

        def malformed(changes):
            def build(record, receipt, *, created_at):
                good = real(record, receipt, created_at=created_at)
                values = {k: v for k, v in good.to_dict().items() if k not in ("digest", "owner")}
                values.update({"owner": good.owner, "operation_families": tuple(good.operation_families), **changes})
                return OperationRecord.create(**values)

            return build

        for name, changes in (
            ("key names another source", {"idempotency_key": COMPENSATION_KEY_PREFIX + other}),
            ("families differ", {"operation_families": ("some.other.family",)}),
        ):
            with self.subTest(name):
                handle = self.apply("c-malformed-" + name.split()[0])
                self.service.authorize(handle)
                self.service.dispatch(handle, FakePort(self.fixture.journal))
                with mock.patch.object(self.service, "_compensation_for_failure", side_effect=malformed(changes)):
                    with self.assertRaisesRegex(HostControlError, "invalid compensation"):
                        self.service.append_result(
                            handle, ok=False, mutations=(self.made(),), evidence=(), detail="x", compensate=True
                        )
                self.assertEqual(self.state(handle), "dispatched")
                self.assertEqual(self.sentinel_count(), 0)
        # an authority record is refused at insert as well (a prepared record
        # can never carry one; the digest/shape gate is the first line)
        handle = self.apply("c-malformed-authority")
        self.service.authorize(handle)
        self.service.dispatch(handle, FakePort(self.fixture.journal))

        def with_authority(record, receipt, *, created_at):
            return dataclasses.replace(real(record, receipt, created_at=created_at), authority_record_id="b" * 64)

        with mock.patch.object(self.service, "_compensation_for_failure", side_effect=with_authority):
            with self.assertRaisesRegex(HostControlError, "invalid compensation"):
                self.service.append_result(handle, ok=False, mutations=(self.made(),), evidence=(), detail="x", compensate=True)
        self.assertEqual(self.sentinel_count(), 0)

    def test_a_compensation_that_cannot_be_built_never_blocks_the_failed_result(self) -> None:
        handle = self.apply("c-build-fails")
        self.service.authorize(handle)
        self.service.dispatch(handle, FakePort(self.fixture.journal))
        with mock.patch.object(self.fixture.journal, "get_plan", side_effect=HostControlError("plan unreadable")):
            final = self.service.append_result(
                handle, ok=False, mutations=(self.made(),), evidence=(), detail="x", compensate=True
            )
        self.assertEqual(final.state, "failed")
        (result,) = self.events(handle, "result")
        self.assertEqual(result["compensation_error"], "HostControlError")
        self.assertNotIn("compensation_operation_handle", result)
        self.assertEqual(self.sentinel_count(), 0)

    def test_no_dbus_handler_can_create_or_authorize_a_compensation(self) -> None:
        tree = ast.parse((HOST_CONTROL / "dbus_service.py").read_text(encoding="utf-8"))
        forbidden = {"append_result", "_compensation_for_failure", "record_terminal", "_authorize_compensation",
                     "_insert_operation", "COMPENSATION_APPROVAL_SENTINEL", "COMPENSATION_KEY_PREFIX"}
        seen = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        seen |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertEqual(seen & forbidden, set())


class OperationGetFieldTests(_CompensationCase):
    def test_operation_get_json_names_the_compensation_only_when_present(self) -> None:
        handle, compensation, _core = self.failed_apply("c-get")
        source = self.service.get_operation(SUBJECT_UID, handle)
        payload = json.loads(wire.operation_json(source, compensation_handle=self.service.compensation_handle(source)))
        self.assertEqual(payload.pop(wire.COMPENSATION_FIELD), compensation.operation_handle)
        self.assertEqual(payload, json.loads(canonical_json(source.to_dict())))  # not in the record or digest
        # absent on the compensation itself and on an uncompensated apply
        comp = self.service.get_operation(SUBJECT_UID, compensation.operation_handle)
        self.assertIsNone(self.service.compensation_handle(comp))
        plain, _none, _c = self.failed_apply("c-get-plain", ())
        plain_record = self.service.get_operation(SUBJECT_UID, plain)
        self.assertIsNone(self.service.compensation_handle(plain_record))
        self.assertNotIn(wire.COMPENSATION_FIELD, json.loads(wire.operation_json(plain_record)))
        with self.assertRaises(HostControlError):  # only an apply carries it
            wire.operation_json(comp, compensation_handle="a" * 64)

    def test_operation_get_method_carries_the_field(self) -> None:
        from tools.host_control import dbus_service

        if not dbus_service.DBUS_AVAILABLE:
            self.skipTest("dbus-python is required")
        from tools.host_control.ratelimit import BrokerRateLimits

        handle, compensation, _core = self.failed_apply("c-get-dbus")
        instance = object.__new__(dbus_service.HostBroker1Object)
        instance._registry = self.fixture.registry
        instance._operation_service = self.service
        instance._rate_limits = BrokerRateLimits()
        instance._driver = mock.Mock(failure=None)
        instance._get_unix_user = lambda sender: SUBJECT_UID
        state, body = dbus_service.HostBroker1Object.OperationGet(instance, handle, sender=":1.5")
        self.assertEqual(state, "failed")
        self.assertEqual(json.loads(body)[wire.COMPENSATION_FIELD], compensation.operation_handle)


if __name__ == "__main__":
    unittest.main()
