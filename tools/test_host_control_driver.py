"""T-2917 5b: the broker-internal OperationDriver, the read-only verify path
(rulings B2/B3), the apply revision gate (B4) and recovery (B11), plus the
broker-side executor client. Real broker domain (journal, planner,
operation service); the executor is a fake port here and real in
tools/test_host_control_executor_dbus.py.
"""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools import test_host_control_core as _core
from tools import test_host_control_dbus_driver as _driver
from tools._host_control_plan_fixtures import registered_plan_transform
from tools.host_control.approval_client import ApprovalConsumerClient
from tools.host_control.contracts import (
    OPERATION_SCHEMA,
    VERIFY_APPROVAL_SENTINEL,
    VERIFY_AUTHORITY_SENTINEL,
    BindOutcome,
    HostControlError,
    OperationRecord,
    PolicyDenied,
    UnknownHandle,
    Unsupported,
)
from tools.host_control.driver import DriverContext, DriverCore, OperationDriver
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OutOfEnvelopeKinds
from tools.host_control.execution_source import BrokerExecutionSource
from tools.host_control.executor import wire as executor_wire
from tools.host_control.executor_client import (
    CollectedResult,
    DBusExecutorPort,
    ExecutorAmbiguous,
    ExecutorCallError,
    ExecutorIdentityMismatch,
    ExecutorRefused,
    ExecutorUnavailable,
    ExecutorUnknownExecution,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import OperationService
from tools.persona_installer import MutationRecord, OperationEvidence

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBJECT_UID = 1003
RECOVERY_UID = 60002
IMPLEMENTED = frozenset(("diagnostics.read", "files.seat_root"))


def pre_flip_table():
    """T-2917 5d.3: the B4 gate is still tested after the mailbox.probe
    flip, against the PRE-flip implemented table (mailbox.probe absent for
    apply/verify/reconcile), so a plan holding an unimplemented class is
    still provably never authorized."""

    from types import MappingProxyType

    from tools.host_control import envelope

    table = {
        kind: (classes - {"mailbox.probe"} if kind != "rollback" else classes)
        for kind, classes in envelope.IMPLEMENTED_EXECUTION_CLASSES.items()
    }
    return mock.patch.object(envelope, "IMPLEMENTED_EXECUTION_CLASSES", MappingProxyType(table))


class CountingBinder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def validate_and_bind(self, approval_handle, plan_handle, expected_digest, operation_handle):
        self.calls.append((approval_handle, plan_handle, expected_digest, operation_handle))
        return BindOutcome(state="bound", authority_record_id="b" * 64, reason_code="bound")


class FailingReconciler:
    def reconcile(self, *args):
        raise AssertionError("the 5b driver never calls the reconciler")


class FakePort:
    """ExecutorPort + collect; observes durable state at the call point."""

    def __init__(self, journal: BrokerJournal) -> None:
        self.journal = journal
        self.executions: list[tuple[str, str, str, int]] = []
        self.execute_errors: list[Exception] = []
        self.collect_script: list[object] = []
        self.ready_value = True

    def ready(self, operation) -> bool:
        return self.ready_value

    def execute(self, operation, staged_receipt, intents) -> str:
        durable = self.journal.load_operation_unbound(operation.operation_handle)
        self.executions.append((operation.operation_handle, durable.state, staged_receipt.phase, len(intents)))
        if self.execute_errors:
            raise self.execute_errors.pop(0)
        return format(len(self.executions), "064x")

    def collect(self, operation, execution_ref):
        if not self.collect_script:
            return None
        item = self.collect_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(operation_ids=(), mutations=()) -> CollectedResult:
    evidence = tuple(OperationEvidence(op_id, 1, "harness-check", True, "ok") for op_id in operation_ids)
    return CollectedResult(
        ok=True,
        result=executor_wire.ExecutionResult(ok=True, mutations=tuple(mutations), evidence=evidence, detail="done"),
    )


class _DomainCase(unittest.TestCase):
    narrow = True

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = _driver.build_domain(
            Path(temp.name) / "domain",
            subject_uid=SUBJECT_UID,
            clock=_driver.MutableClock(_driver.FIXED_NOW),
        )
        self.addCleanup(self.fixture.close)
        if self.narrow:
            # T-5533: narrow the REGISTERED plan the broker reads (was: wrap
            # installer.plan, which the broker no longer calls).
            def narrowed(plan):
                keep = tuple(
                    op for op in plan.operations if EXECUTOR_KIND_CLASSES.get(op.kind) in IMPLEMENTED
                )
                return _core._plan_replace(plan, operations=keep)

            patcher = registered_plan_transform(narrowed)
            patcher.__enter__()
            self.addCleanup(patcher.__exit__, None, None, None)
        self.binder = CountingBinder()
        self.service = OperationService(
            registry=self.fixture.registry,
            journal=self.fixture.journal,
            binder=self.binder,
            reconciler=FailingReconciler(),
            clock=self.fixture.clock,
            handle_factory=_driver._handle_factory_offset(1 << 40),
        )
        self.port = FakePort(self.fixture.journal)
        self.core = DriverCore(
            DriverContext(self.service, self.fixture.journal, self.port, lambda: None),
            sleeper=lambda seconds: None,
        )
        self.plan = self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "drv-plan")

    def state(self, handle: str) -> str:
        return self.fixture.journal.load_operation_unbound(handle).state

    def apply(self, key: str = "drv-apply") -> str:
        record, _ = self.service.prepare_apply(
            SUBJECT_UID, self.plan.plan_handle, self.plan.plan_digest, key, "ab" * 32
        )
        return record.operation_handle

    def verify(self, key: str = "drv-verify") -> str:
        record, _ = self.service.prepare_verify(SUBJECT_UID, "recovery", self.plan.plan_handle, key)
        return record.operation_handle


class VerifyPathTests(_DomainCase):
    def test_verify_is_policy_authorized_read_only_and_completes(self) -> None:
        handle = self.verify()
        record = self.fixture.journal.load_operation_unbound(handle)
        self.assertEqual(record.kind, "verify")
        self.assertEqual(record.approval_handle, VERIFY_APPROVAL_SENTINEL)
        self.port.collect_script = [_ok(("harness.command",))]
        self.assertEqual(self.core.drive(handle), "completed")
        self.assertEqual(self.binder.calls, [])  # B2: no ValidateAndBind
        final = self.fixture.journal.load_operation_unbound(handle)
        self.assertEqual(final.authority_record_id, VERIFY_AUTHORITY_SENTINEL)
        # dispatched with an EMPTY write-ahead intent set, staged receipt first
        self.assertEqual(self.port.executions, [(handle, "dispatch_intent", "staged", 0)])
        self.assertEqual(self.fixture.journal.unresolved_intents(handle), ())

    def test_verify_requires_own_plan_and_matching_seat(self) -> None:
        with self.assertRaises(PolicyDenied):
            self.service.prepare_verify(SUBJECT_UID, "bravo", self.plan.plan_handle, "v-wrong-seat")
        with self.assertRaises(HostControlError):
            self.service.prepare_verify(60001, "recovery", self.plan.plan_handle, "v-thief")

    def test_verify_idempotency(self) -> None:
        first = self.verify("v-same")
        self.assertEqual(self.verify("v-same"), first)

    def test_rollback_of_a_verify_receipt_is_refused(self) -> None:
        handle = self.verify()
        self.port.collect_script = [_ok()]
        self.core.drive(handle)
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            receipt = connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase='complete'",
                (handle,),
            ).fetchone()[0]
        with self.assertRaises(HostControlError) as caught:
            self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-verify", "cd" * 32)
        self.assertIn("apply receipt", str(caught.exception))


class SentinelPinTests(_DomainCase):
    """Coordinator ruling B2 pins (a), (b), (c)."""

    def test_a_approval_get_refuses_the_sentinel_without_contacting_the_authority(self) -> None:
        transport = mock.Mock()
        ownership = mock.Mock(return_value=True)
        client = ApprovalConsumerClient(transport=transport, ownership=mock.Mock(subject_references_approval=ownership))
        subject = self.fixture.registry.subject_for_uid(SUBJECT_UID)
        self.verify()  # a real operation now carries the sentinel
        with self.assertRaises(UnknownHandle):
            client.approval_status(subject, VERIFY_APPROVAL_SENTINEL)
        transport.call.assert_not_called()
        # ValidateAndBind on it is a closed denial, never a transport call
        outcome = client.validate_and_bind(VERIFY_APPROVAL_SENTINEL, "a" * 64, "b" * 64, "c" * 64)
        self.assertEqual(outcome.state, "denied")
        transport.call.assert_not_called()

    def test_b_apply_and_rollback_can_never_carry_the_sentinel(self) -> None:
        with self.assertRaises(HostControlError):
            self.service.prepare_apply(
                SUBJECT_UID, self.plan.plan_handle, self.plan.plan_digest, "k", VERIFY_APPROVAL_SENTINEL
            )
        with self.assertRaises(HostControlError):
            self.service.prepare_rollback(SUBJECT_UID, "a" * 64, "k", VERIFY_APPROVAL_SENTINEL)
        template = self.fixture.journal.load_operation_unbound(self.apply())
        values = {f.name: getattr(template, f.name) for f in dataclasses.fields(template) if f.name != "digest"}
        for kind in ("apply", "rollback"):
            for field, value in (
                ("approval_handle", VERIFY_APPROVAL_SENTINEL),
                ("authority_record_id", VERIFY_AUTHORITY_SENTINEL),
            ):
                with self.subTest(kind=kind, field=field):
                    changed = dict(values, kind=kind, **{field: value})
                    if kind == "rollback":
                        changed.update(receipt_handle="d" * 64, receipt_digest="e" * 64)
                    if field == "authority_record_id":
                        changed["state"] = "authorized"
                    with self.assertRaises(HostControlError):
                        OperationRecord.create(**changed)

    def test_c_the_sentinel_cannot_be_minted_from_caller_input(self) -> None:
        # Verify takes no approval argument at any tier
        self.assertNotIn("approval_handle", inspect.signature(OperationService.prepare_verify).parameters)
        from tools.host_control import dbus_service

        if dbus_service.DBUS_AVAILABLE:
            verify = dbus_service.HostBroker1Object.Verify
            self.assertNotIn("approval_handle", inspect.signature(verify).parameters)
        # a verify record is only valid with the server constants
        template = self.fixture.journal.load_operation_unbound(self.verify())
        values = {f.name: getattr(template, f.name) for f in dataclasses.fields(template) if f.name != "digest"}
        for approval in ("ab" * 32, "none", "caller-chosen"):
            with self.subTest(approval=approval), self.assertRaises(HostControlError):
                OperationRecord.create(**dict(values, approval_handle=approval))
        with self.assertRaises(HostControlError):
            OperationRecord.create(**dict(values, state="authorized", authority_record_id="b" * 64))
        # and caller-chosen strings (even the sentinel text) only ever land
        # in the idempotency key, never the approval field
        record, _ = self.service.prepare_verify(SUBJECT_UID, "recovery", self.plan.plan_handle, "none:x")
        self.assertEqual(record.approval_handle, VERIFY_APPROVAL_SENTINEL)
        self.assertEqual(record.schema, OPERATION_SCHEMA)


class ApplyPathTests(_DomainCase):
    def test_apply_authorize_dispatch_collect_append(self) -> None:
        handle = self.apply()
        mutation = MutationRecord("connective.config-dir", "mkdir", "/x", "claude-fallback", True, {"state": "absent"}, "directory")
        self.port.collect_script = [None, None, _ok(("connective.config-dir",), (mutation,))]
        self.assertEqual(self.core.drive(handle), "completed")
        self.assertEqual(len(self.binder.calls), 1)
        # E30: the executor was called only once the staged receipt, the
        # write-ahead intents and DISPATCH_INTENT were durable
        (_, durable_state, phase, intent_count), = self.port.executions
        self.assertEqual((durable_state, phase), ("dispatch_intent", "staged"))
        self.assertGreater(intent_count, 0)
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            phases = sorted(
                row[0] for row in connection.execute(
                    "SELECT phase FROM operation_receipts WHERE operation_handle=?", (handle,)
                )
            )
        self.assertEqual(phases, ["complete", "staged"])

    def test_unimplemented_class_is_never_authorized(self) -> None:
        # B4 at the driver: a prepared apply of a plan holding activation
        # kinds (e.g. recovered from a pre-5b journal) is left untouched
        # T-2917 5d.3: re-pointed at the pre-flip table (pre_flip_table) so
        # the B4 mechanism stays tested after real plans become executable
        with registered_plan_transform(lambda plan: plan, compose=False):
            full = self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "full-plan")
        record, _ = self.service.prepare_apply(SUBJECT_UID, full.plan_handle, full.plan_digest, "full-apply", "ab" * 32)
        with pre_flip_table():
            with self.assertRaises(Unsupported):
                self.service.require_apply_implemented(SUBJECT_UID, full.plan_handle, full.plan_digest)
            self.assertEqual(self.core.drive(record.operation_handle), "unsupported_pending")
        self.assertEqual(self.binder.calls, [])
        self.assertEqual(self.state(record.operation_handle), "prepared")

    def test_real_persona_plan_is_authorized_after_the_flip(self) -> None:
        # T-2917 5d.3 (T-2917:D22): the FULL real plan is implemented now
        with registered_plan_transform(lambda plan: plan, compose=False):
            full = self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "full-plan-flip")
        kinds = {op.kind for op in self.fixture.journal.load_plan_unbound(full.plan_handle).installer_plan.operations}
        self.assertIn("mailbox-probe", kinds)
        self.service.require_apply_implemented(SUBJECT_UID, full.plan_handle, full.plan_digest)
        record, _ = self.service.prepare_apply(SUBJECT_UID, full.plan_handle, full.plan_digest, "full-apply-flip", "ab" * 32)
        self.port.collect_script = [_ok()]
        self.assertEqual(self.core.drive(record.operation_handle), "completed")
        self.assertEqual(len(self.binder.calls), 1)

    def test_definitive_refusal_then_safe_resume(self) -> None:
        handle = self.apply()
        self.port.execute_errors = [ExecutorRefused("busy", name=executor_wire.BUSY_ERROR)]
        self.assertEqual(self.core.drive(handle), "dispatch_refused")
        self.assertEqual(self.state(handle), "dispatch_intent")
        self.port.collect_script = [_ok()]
        self.assertEqual(self.core.drive(handle), "completed")
        self.assertEqual(len(self.port.executions), 2)

    def test_executor_unavailable_before_dispatch_writes_nothing(self) -> None:
        handle = self.apply()
        self.port.ready_value = False
        self.assertEqual(self.core.drive(handle), "executor_unavailable")
        self.assertEqual(self.state(handle), "authorized")
        self.assertEqual(self.port.executions, [])

    def test_ambiguous_dispatch_is_left_for_the_reconciler(self) -> None:
        handle = self.apply()
        self.port.execute_errors = [ExecutorAmbiguous("timeout")]
        self.assertEqual(self.core.drive(handle), "awaiting_reconciler")
        for _ in range(3):
            self.assertEqual(self.core.drive(handle), "awaiting_reconciler")
        self.assertEqual(len(self.port.executions), 1)
        self.assertEqual(self.state(handle), "dispatch_intent")

    def test_b11_dispatched_apply_with_unknown_execution_stays_visible_and_is_never_redispatched(self) -> None:
        handle = self.apply()
        self.port.collect_script = [ExecutorUnknownExecution("restarted")]
        self.assertEqual(self.core.drive(handle), "awaiting_reconciler")
        for _ in range(3):
            self.port.collect_script = [ExecutorUnknownExecution("restarted")]
            self.assertEqual(self.core.drive(handle), "awaiting_reconciler")
        self.assertEqual(len(self.port.executions), 1)  # never re-dispatched
        # visible to the caller as dispatched (not completed/failed)
        record = self.fixture.operation_service.get_operation(SUBJECT_UID, handle)
        self.assertEqual(record.state, "dispatched")
        with self.assertRaises(HostControlError):
            self.service.resume_dispatch(handle, self.port)

    def test_collect_timeout_leaves_the_operation_dispatched(self) -> None:
        handle = self.apply()
        ticks = iter(range(0, 100000, 100))
        core = DriverCore(
            DriverContext(self.service, self.fixture.journal, self.port, lambda: None),
            sleeper=lambda seconds: None,
            clock=lambda: float(next(ticks)),
            collect_timeout=500.0,
        )
        self.assertEqual(core.drive(handle), "collect_pending")
        self.assertEqual(self.state(handle), "dispatched")

    def test_rollback_is_driven_since_5d2(self) -> None:
        # T-2917 5d.2 (T-2917:D15) replaces the 5b "never driven" pin: a
        # rollback of a restorable receipt is bound, dispatched, collected
        handle = self.apply()
        plan = self.fixture.journal.load_plan_unbound(self.plan.plan_handle)
        intent = next(op for op in plan.installer_plan.operations if op.kind == "mkdir" and op.inverse)
        made = MutationRecord(intent.operation_id, intent.kind, intent.target, intent.owner, True, {"state": "absent"}, "directory")
        self.port.collect_script = [_ok((intent.operation_id,), (made,))]
        self.assertEqual(self.core.drive(handle), "completed")
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            receipt = connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase='complete'",
                (handle,),
            ).fetchone()[0]
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-1", "cd" * 32)
        binds = len(self.binder.calls)
        restored = MutationRecord(intent.operation_id, intent.kind, intent.target, intent.owner, True, {"state": "directory", "mode": "0755"}, None)
        self.port.collect_script = [_ok((intent.operation_id,), (restored,))]
        self.assertEqual(self.core.drive(rollback.operation_handle), "completed")
        self.assertEqual(len(self.binder.calls), binds + 1)
        self.assertEqual(self.port.executions[-1][0], rollback.operation_handle)


class TransportGateTests(_DomainCase):
    """Rulings B3/B4 at the Manager transport with a driver wired: a plan
    holding a class this revision cannot execute is Unsupported BEFORE any
    record is written or any grant can bind, and nothing is submitted."""

    def setUp(self) -> None:
        super().setUp()
        from tools.host_control import dbus_service

        if not dbus_service.DBUS_AVAILABLE:
            self.skipTest("dbus-python is required")
        self.dbus_service = dbus_service
        from tools.host_control.ratelimit import BrokerRateLimits

        self.submitted: list[str] = []
        instance = object.__new__(dbus_service.HostBroker1Object)
        instance._registry = self.fixture.registry
        instance._operation_service = self.service
        instance._rate_limits = BrokerRateLimits()
        instance._driver = mock.Mock(submit=self.submitted.append, failure=None)
        instance._get_unix_user = lambda sender: SUBJECT_UID
        self.instance = instance
        with registered_plan_transform(lambda plan: plan, compose=False):
            self.full = self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "full-plan-gate")

    def operation_count(self) -> int:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

    def test_apply_and_verify_refused_unsupported_before_any_record(self) -> None:
        import dbus

        from tools.host_control import wire

        # T-2917 5d.3: re-pointed at the pre-flip table (the B4 transport
        # gate stays tested); the positive real-plan case follows
        before = self.operation_count()
        with pre_flip_table():
            with self.assertRaises(dbus.exceptions.DBusException) as caught:
                self.dbus_service.HostBroker1Object.Apply(
                    self.instance, self.full.plan_handle, self.full.plan_digest, "gate-apply", "ab" * 32, sender=":1.5"
                )
            self.assertEqual(caught.exception.get_dbus_name(), wire.UNSUPPORTED_ERROR)
            with self.assertRaises(dbus.exceptions.DBusException) as caught:
                self.dbus_service.HostBroker1Object.Verify(
                    self.instance, "recovery", self.full.plan_handle, "gate-verify", sender=":1.5"
                )
            self.assertEqual(caught.exception.get_dbus_name(), wire.UNSUPPORTED_ERROR)
        self.assertEqual(self.operation_count(), before)
        self.assertEqual(self.binder.calls, [])
        self.assertEqual(self.submitted, [])

    def test_real_plan_apply_and_verify_are_admitted_after_the_flip(self) -> None:
        before = self.operation_count()
        applied = self.dbus_service.HostBroker1Object.Apply(
            self.instance, self.full.plan_handle, self.full.plan_digest, "flip-apply", "ab" * 32, sender=":1.5"
        )
        verified = self.dbus_service.HostBroker1Object.Verify(
            self.instance, "recovery", self.full.plan_handle, "flip-verify", sender=":1.5"
        )
        self.assertEqual(self.operation_count(), before + 2)
        self.assertEqual(self.submitted, [str(applied), str(verified)])

    def test_n2_dead_driver_refuses_before_prepare(self) -> None:
        import dbus

        from tools.host_control import wire

        self.instance._driver.failure = "driver setup failed: RuntimeError"
        before = self.operation_count()
        for call in (
            lambda: self.dbus_service.HostBroker1Object.Apply(
                self.instance, self.plan.plan_handle, self.plan.plan_digest, "dead-a", "ab" * 32, sender=":1.5"
            ),
            lambda: self.dbus_service.HostBroker1Object.Verify(
                self.instance, "recovery", self.plan.plan_handle, "dead-v", sender=":1.5"
            ),
        ):
            with self.assertRaises(dbus.exceptions.DBusException) as caught:
                call()
            self.assertEqual(caught.exception.get_dbus_name(), wire.FAILED_ERROR)
        self.assertEqual(self.operation_count(), before)
        self.assertEqual(self.submitted, [])

    def test_implemented_plan_is_prepared_and_submitted(self) -> None:
        handle = self.dbus_service.HostBroker1Object.Apply(
            self.instance, self.plan.plan_handle, self.plan.plan_digest, "gate-ok", "ab" * 32, sender=":1.5"
        )
        self.assertEqual(self.submitted, [handle])
        self.assertEqual(self.state(handle), "prepared")
        self.assertEqual(self.binder.calls, [])  # binding happens on the driver thread
        verify = self.dbus_service.HostBroker1Object.Verify(
            self.instance, "recovery", self.plan.plan_handle, "gate-ok-v", sender=":1.5"
        )
        self.assertEqual(self.submitted, [handle, verify])


class VerifyRecoveryTests(_DomainCase):
    def test_verify_with_unknown_execution_is_redispatched(self) -> None:
        handle = self.verify()
        self.port.collect_script = [ExecutorUnknownExecution("restarted"), _ok()]
        self.assertEqual(self.core.drive(handle), "completed")
        self.assertEqual(len(self.port.executions), 2)
        self.assertEqual(self.fixture.journal.dispatch_execution_ref(handle), format(2, "064x"))


class ReviewFixDriverTests(_DomainCase):
    def test_m3_verify_redispatch_is_bounded_and_backs_off(self) -> None:
        handle = self.verify("m3")
        sleeps: list[float] = []

        class AlwaysUnknown(FakePort):
            def collect(self, operation, execution_ref):
                raise ExecutorUnknownExecution("gone")

        port = AlwaysUnknown(self.fixture.journal)
        core = DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None),
            sleeper=sleeps.append,
            max_verify_redispatches=3,
        )
        self.assertEqual(core.drive(handle), "collect_pending")
        # first dispatch + at most 3 re-dispatches, each after a back-off
        self.assertEqual(len(port.executions), 4)
        self.assertEqual(len(sleeps), 3)
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            redispatched = connection.execute(
                "SELECT COUNT(*) FROM operation_events WHERE operation_handle=? AND event_kind='redispatched'",
                (handle,),
            ).fetchone()[0]
        self.assertEqual(redispatched, 3)

    def test_m3_verify_redispatch_respects_the_deadline(self) -> None:
        handle = self.verify("m3-deadline")

        class AlwaysUnknown(FakePort):
            def collect(self, operation, execution_ref):
                raise ExecutorUnknownExecution("gone")

        port = AlwaysUnknown(self.fixture.journal)
        ticks = iter(range(0, 10**6, 1000))
        core = DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None),
            sleeper=lambda seconds: None,
            clock=lambda: float(next(ticks)),
            collect_timeout=1500.0,
            max_verify_redispatches=100,
        )
        self.assertEqual(core.drive(handle), "collect_pending")
        self.assertLessEqual(len(port.executions), 3)

    def test_m4_one_execute_per_apply_when_the_post_execute_write_fails(self) -> None:
        handle = self.apply("m4")
        self.port.execute_errors = [ExecutorRefused("busy", name=executor_wire.BUSY_ERROR)]
        self.assertEqual(self.core.drive(handle), "dispatch_refused")
        refused_attempts = len(self.port.executions)
        real = self.fixture.journal.transition_operation
        failures = {"n": 0}

        def flaky(**kwargs):
            if kwargs["new"].state == "dispatched" and failures["n"] == 0:
                failures["n"] += 1
                raise HostControlError("injected journal failure after Execute")
            return real(**kwargs)

        with mock.patch.object(self.fixture.journal, "transition_operation", side_effect=flaky):
            with self.assertRaises(HostControlError):
                self.core.drive(handle)
        self.assertEqual(len(self.port.executions), refused_attempts + 1)
        # the accepted Execute may have started work: never again
        for _ in range(3):
            self.assertEqual(self.core.drive(handle), "awaiting_reconciler")
        self.assertEqual(len(self.port.executions), refused_attempts + 1)
        self.assertEqual(self.state(handle), "dispatch_intent")

    def test_s3_setup_failure_is_visible_not_a_silent_dead_thread(self) -> None:
        def broken() -> DriverContext:
            raise RuntimeError("cannot open journal")

        driver = OperationDriver(context_factory=broken, sleeper=lambda seconds: None)
        with self.assertLogs("tools.host_control.driver", level="ERROR"):
            driver.start()
            self.assertTrue(driver.recovered.wait(30))
            driver._thread.join(30)
        self.assertEqual(driver.failure, "driver setup failed: RuntimeError")
        with self.assertRaises(HostControlError):
            driver.submit("ab" * 32)

    def test_s4_mutating_spec_is_served_once_verify_may_refetch(self) -> None:
        from tools.host_control.contracts import InvalidTransition

        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        handles = {}
        for kind, handle in (("apply", self.apply("s4-a")), ("verify", self.verify("s4-v"))):
            self.service.authorize(handle)
            self.port.execute_errors = [ExecutorAmbiguous("stop at dispatch_intent")]
            try:
                self.service.dispatch(handle, self.port)
            except ExecutorAmbiguous:
                pass
            handles[kind] = handle
        apply_record = self.fixture.journal.load_operation_unbound(handles["apply"])
        digest = source.expected_digest(apply_record)
        source.operation_spec(RECOVERY_UID, handles["apply"], digest)
        with self.assertRaises(InvalidTransition):
            source.operation_spec(RECOVERY_UID, handles["apply"], digest)
        verify_record = self.fixture.journal.load_operation_unbound(handles["verify"])
        verify_digest = source.expected_digest(verify_record)
        for _ in range(3):
            source.operation_spec(RECOVERY_UID, handles["verify"], verify_digest)


class InsideBrokerPlanRefusalTests(_DomainCase):
    """Coordinator: a non-admitted kind INSIDE a broker plan (a hand-built
    plan bypassing the partition) is refused at prepare and again at
    OperationSpec."""

    def _bypassed_plan(self):
        def with_unmapped(plan):
            extra = dataclasses.replace(plan.operations[1], operation_id="injected", kind="claude-mcp-server-registration")
            return _core._plan_replace(plan, operations=plan.operations + (extra,))

        from tools.host_control import planner as planner_module

        with registered_plan_transform(with_unmapped), \
                mock.patch.object(planner_module, "partition_installer_plan", side_effect=lambda plan: (plan, ())), \
                mock.patch.object(planner_module, "require_executor_envelope"):
            return self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "bypass")

    def test_planner_belt_refuses_without_the_bypass(self) -> None:
        def with_unmapped(plan):
            extra = dataclasses.replace(plan.operations[1], operation_id="injected", kind="claude-mcp-server-registration")
            return _core._plan_replace(plan, operations=plan.operations + (extra,))

        from tools.host_control import planner as planner_module

        with registered_plan_transform(with_unmapped), \
                mock.patch.object(planner_module, "partition_installer_plan", side_effect=lambda plan: (plan, ())):
            with self.assertRaises(OutOfEnvelopeKinds):
                self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "belt")

    def test_refused_at_prepare_and_again_at_operation_spec(self) -> None:
        plan = self._bypassed_plan()
        with self.assertRaises(OutOfEnvelopeKinds):
            self.service.prepare_apply(SUBJECT_UID, plan.plan_handle, plan.plan_digest, "in-1", "ab" * 32)
        with self.assertRaises(OutOfEnvelopeKinds):
            self.service.prepare_verify(SUBJECT_UID, "recovery", plan.plan_handle, "in-2")
        # bypass prepare's check too, walk it to DISPATCH_INTENT, and the
        # execution point still refuses (5a re-check)
        from tools.host_control import operations as operations_module

        with mock.patch.object(operations_module, "require_executor_envelope"), \
                mock.patch.object(operations_module, "require_revision_implemented"):
            record, _ = self.service.prepare_apply(SUBJECT_UID, plan.plan_handle, plan.plan_digest, "in-3", "ab" * 32)
            self.service.authorize(record.operation_handle)
            self.port.execute_errors = [ExecutorAmbiguous("stop here")]
            try:
                self.service.dispatch(record.operation_handle, self.port)
            except ExecutorAmbiguous:
                pass
        dispatched = self.fixture.journal.load_operation_unbound(record.operation_handle)
        self.assertEqual(dispatched.state, "dispatch_intent")
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        with self.assertRaises(OutOfEnvelopeKinds):
            source.operation_spec(RECOVERY_UID, record.operation_handle, "0" * 64)


class DriverThreadTests(_DomainCase):
    def test_recovery_at_start_runs_on_the_driver_thread_with_its_own_journal(self) -> None:
        verify_handle = self.verify("thread-verify")
        apply_handle = self.apply("thread-apply")
        seen_threads: set[str] = set()
        journal_path = self.fixture.root / "broker.sqlite3"
        outcomes: dict[str, str] = {}

        def factory() -> DriverContext:
            seen_threads.add(threading.current_thread().name)
            journal = BrokerJournal.open(journal_path)
            service = OperationService(
                registry=self.fixture.registry,
                journal=journal,
                binder=self.binder,
                reconciler=FailingReconciler(),
                clock=self.fixture.clock,
                handle_factory=_driver._handle_factory_offset(1 << 41),
            )
            port = FakePort(journal)
            port.collect_script = [_ok(), _ok()]
            return DriverContext(service, journal, port, journal.close)

        driver = OperationDriver(
            context_factory=factory,
            sleeper=lambda seconds: None,
            on_outcome=lambda handle, outcome: outcomes.__setitem__(handle, outcome),
        )
        driver.start()
        self.addCleanup(driver.stop)
        self.assertTrue(driver.recovered.wait(30))
        self.assertEqual(seen_threads, {"host-broker-driver"})
        self.assertEqual(outcomes, {verify_handle: "completed", apply_handle: "completed"})
        self.assertEqual(self.state(verify_handle), "completed")
        self.assertEqual(self.state(apply_handle), "completed")


class ExecutorClientTests(_DomainCase):
    class Transport:
        def __init__(self, owner_uid: int) -> None:
            self.owner_uid = owner_uid
            self.calls: list[tuple] = []
            self.owner: str | None = ":1.77"
            self.reply: object = ("e" * 64,)
            self.error: ExecutorCallError | None = None

        def name_owner(self, name: str) -> str:
            self.names = getattr(self, "names", []) + [name]
            if self.owner is None:
                raise ExecutorCallError("org.freedesktop.DBus.Error.NameHasNoOwner", "none")
            return self.owner

        def unix_user(self, unique: str) -> int:
            return self.owner_uid

        def call(self, unique, method, signature, args):
            self.calls.append((unique, method, signature, args))
            if self.error is not None:
                raise self.error
            return self.reply

    def make_port(self, transport) -> DBusExecutorPort:
        return DBusExecutorPort(
            registry=self.fixture.registry,
            execution_source=BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry),
            transport=transport,
        )

    def dispatched_verify(self) -> OperationRecord:
        handle = self.verify("client-verify")
        self.service.authorize(handle)
        self.port.execute_errors = [ExecutorAmbiguous("stop")]
        try:
            self.service.dispatch(handle, self.port)
        except ExecutorAmbiguous:
            pass
        return self.fixture.journal.load_operation_unbound(handle)

    def test_execute_targets_the_verified_unique_name_of_the_registry_uid(self) -> None:
        record = self.dispatched_verify()
        transport = self.Transport(RECOVERY_UID)
        port = self.make_port(transport)
        handle = port.execute(record, None, ())
        self.assertEqual(handle, "e" * 64)
        self.assertEqual(transport.names, [f"org.pantheon.HostExecutor1.U{RECOVERY_UID}"])
        unique, method, signature, args = transport.calls[0]
        self.assertEqual((unique, method, signature), (":1.77", "Execute", "ss"))
        self.assertEqual(args[0], record.operation_handle)
        expected = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry).expected_digest(record)
        self.assertEqual(args[1], expected)

    def test_name_owned_by_another_uid_is_refused_before_any_call(self) -> None:
        record = self.dispatched_verify()
        transport = self.Transport(RECOVERY_UID + 1)
        with self.assertRaises(ExecutorIdentityMismatch):
            self.make_port(transport).execute(record, None, ())
        with self.assertRaises(ExecutorIdentityMismatch):
            self.make_port(transport).collect(record, "e" * 64)
        self.assertEqual(transport.calls, [])

    def test_error_classes(self) -> None:
        record = self.dispatched_verify()
        transport = self.Transport(RECOVERY_UID)
        transport.owner = None
        with self.assertRaises(ExecutorUnavailable):
            self.make_port(transport).execute(record, None, ())
        transport.owner = ":1.77"
        for name, error in (
            (executor_wire.BUSY_ERROR, ExecutorRefused),
            (executor_wire.DENIED_ERROR, ExecutorRefused),
            (executor_wire.UNKNOWN_EXECUTION_ERROR, ExecutorUnknownExecution),
            ("org.freedesktop.DBus.Error.ServiceUnknown", ExecutorUnavailable),
            ("org.freedesktop.DBus.Error.NoReply", ExecutorAmbiguous),
        ):
            with self.subTest(name=name):
                transport.error = ExecutorCallError(name, "x")
                with self.assertRaises(error):
                    self.make_port(transport).execute(record, None, ())
        transport.error = None
        transport.reply = ("not-a-handle",)
        with self.assertRaises(ExecutorAmbiguous):
            self.make_port(transport).execute(record, None, ())

    def test_collect_validates_the_result_against_the_spec(self) -> None:
        record = self.dispatched_verify()
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        digest = source.expected_digest(record)
        transport = self.Transport(RECOVERY_UID)
        port = self.make_port(transport)
        transport.reply = ("running", "")
        self.assertIsNone(port.collect(record, "e" * 64))
        good = executor_wire.encode_result(
            operation_handle=record.operation_handle,
            spec_digest=digest,
            result=executor_wire.ExecutionResult(
                ok=True, mutations=(), evidence=(OperationEvidence("harness.command", 2, "harness-check", True, "ok"),), detail="d"
            ),
        )
        transport.reply = ("succeeded", good)
        collected = port.collect(record, "e" * 64)
        self.assertTrue(collected.ok)
        with_mutation = executor_wire.encode_result(
            operation_handle=record.operation_handle,
            spec_digest=digest,
            result=executor_wire.ExecutionResult(
                ok=True,
                mutations=(MutationRecord("harness.command", "harness-check", "codex", "claude-fallback", False, {}, None),),
                evidence=(),
                detail="d",
            ),
        )
        transport.reply = ("succeeded", with_mutation)
        with self.assertRaises(HostControlError):
            port.collect(record, "e" * 64)  # verify never returns mutations
        transport.reply = ("succeeded", good.replace(record.operation_handle, "0" * 64))
        with self.assertRaises(HostControlError):
            port.collect(record, "e" * 64)
        transport.reply = ("exploded", "")
        with self.assertRaises(ExecutorAmbiguous):
            port.collect(record, "e" * 64)


if __name__ == "__main__":  # must stay LAST
    unittest.main()
