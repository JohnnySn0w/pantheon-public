"""ORG-052 Unit 5a (T-2917): broker-only ExecutionSource.OperationSpec.

The per-seat executor obtains its operation ONLY through this interface;
only the registry uid of the operation's TARGET seat may read it, only
after the broker durably wrote the staged receipt and intents, and only
with the exact recomputed digest.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.host_control import execution_source as source_module
from tools.host_control import wire
from tools.host_control.contracts import (
    HostControlError,
    InvalidTransition,
    OperationRecord,
    PolicyDenied,
    UnknownHandle,
    canonical_json,
    sha256_json,
)
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
from tools.host_control.execution_source import (
    EXECUTION_SPEC_SCHEMA,
    BrokerExecutionSource,
    spec_digest,
)
from tools import test_host_control_dbus as _bus_tests
from tools import test_host_control_dbus_driver as _driver

SUBJECT_UID = 1003
SEAT_B_UID = 60001
RECOVERY_UID = 60002  # the fixture registry's uid for target seat "recovery"


def _record_with_state(record: OperationRecord, state: str, updated_at: str) -> OperationRecord:
    values = {
        field.name: getattr(record, field.name)
        for field in dataclasses.fields(record)
        if field.name != "digest"
    }
    values.update(state=state, updated_at=updated_at)
    return OperationRecord.create(**values)


class ExecutionSourceDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = _driver.build_domain(
            Path(self.temp.name) / "domain",
            subject_uid=SUBJECT_UID,
            clock=_driver.MutableClock(_driver.FIXED_NOW),
        )
        self.addCleanup(self.fixture.close)
        self.seeded = _driver.seed_other_subject(self.fixture)
        self.source = BrokerExecutionSource(
            journal=self.fixture.journal,
            registry=self.fixture.registry,
        )
        self.handle = self.seeded["seat_b_operation_handle"]
        self.record = self.fixture.journal.load_operation_unbound(self.handle)
        self.digest = self.source.expected_digest(self.record)

    def test_target_seat_executor_reads_the_exact_spec(self) -> None:
        self.assertEqual(self.record.state, "dispatched")
        schema, kind, digest, spec_json = self.source.operation_spec(
            RECOVERY_UID, self.handle, self.digest
        )
        self.assertEqual((schema, kind, digest), (EXECUTION_SPEC_SCHEMA, "apply", self.digest))
        spec = json.loads(spec_json)
        self.assertEqual(spec_json, canonical_json(spec))
        self.assertEqual(sha256_json(spec), digest)
        self.assertEqual(spec["operation_handle"], self.handle)
        self.assertEqual(spec["target_seat_id"], "recovery")
        self.assertEqual(spec["plan_handle"], self.seeded["seat_b_plan_handle"])
        self.assertTrue(spec["operations"])
        self.assertTrue(all(op["kind"] in EXECUTOR_KIND_CLASSES for op in spec["operations"]))
        # deterministic: no timestamps anywhere in the digested spec
        self.assertNotIn("created_at", spec_json)
        self.assertNotIn("updated_at", spec_json)
        self.assertNotIn("expires_at", spec_json)

    def test_only_the_target_seat_uid_may_read_it(self) -> None:
        for uid in (SEAT_B_UID, SUBJECT_UID, 0, 99999):
            with self.subTest(uid=uid):
                with self.assertRaises(PolicyDenied) as caught:
                    self.source.operation_spec(uid, self.handle, self.digest)
                self.assertEqual(caught.exception.reason_code, "subject_denied")

    def test_digest_must_match_and_inputs_are_closed(self) -> None:
        with self.assertRaises(HostControlError):
            self.source.operation_spec(RECOVERY_UID, self.handle, "f" * 64)
        for handle, digest in (("x", self.digest), (self.handle, "short"), ("A" * 64, self.digest)):
            with self.subTest(handle=handle[:8], digest=digest[:8]):
                with self.assertRaises(HostControlError):
                    self.source.operation_spec(RECOVERY_UID, handle, digest)
        with self.assertRaises(UnknownHandle):
            self.source.operation_spec(RECOVERY_UID, "9" * 64, self.digest)

    def test_spec_unreadable_before_dispatch_intent(self) -> None:
        plan = self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", "spec-prepared")
        record, _ = self.fixture.operation_service.prepare_apply(
            SUBJECT_UID, plan.plan_handle, plan.plan_digest, "spec-prepared-apply", "c" * 64
        )
        self.assertEqual(record.state, "prepared")
        with self.assertRaises(InvalidTransition):
            self.source.operation_spec(
                RECOVERY_UID, record.operation_handle, self.source.expected_digest(record)
            )

    def test_digest_is_stable_across_state_transitions(self) -> None:
        intent = _record_with_state(self.record, "dispatch_intent", "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(intent.digest, self.record.digest)
        self.assertEqual(self.source.expected_digest(intent), self.digest)

    def test_envelope_is_rechecked_at_the_execution_point(self) -> None:
        seen: list[tuple[str, ...]] = []
        real = source_module.require_executor_envelope

        def recording(kinds):
            kinds = tuple(kinds)
            seen.append(kinds)
            return real(kinds)

        with mock.patch.object(source_module, "require_executor_envelope", side_effect=recording):
            self.source.operation_spec(RECOVERY_UID, self.handle, self.digest)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0])

    def test_rollback_spec_carries_the_receipt_mutations(self) -> None:
        state = _driver.seed_subject_complete_receipt(self.fixture, subject_uid=SUBJECT_UID)
        service = _driver._seed_service(self.fixture, 1 << 34)
        record, _ = service.prepare_rollback(
            SUBJECT_UID, state["subject_complete_receipt_handle"], "spec-rollback", "d" * 64
        )
        service.authorize(record.operation_handle)
        dispatched = service.dispatch(record.operation_handle, _driver._SeedExecutor())
        digest = self.source.expected_digest(dispatched)
        _schema, kind, _digest, spec_json = self.source.operation_spec(
            RECOVERY_UID, dispatched.operation_handle, digest
        )
        spec = json.loads(spec_json)
        self.assertEqual(kind, "rollback")
        self.assertEqual(spec["receipt_handle"], state["subject_complete_receipt_handle"])
        self.assertEqual(spec["receipt_digest"], dispatched.receipt_digest)
        # T-2917 5d.2: the RESTORABLE mutations of the bound receipt (the
        # seed receipt holds one changed, intent-backed mkdir record) ...
        self.assertEqual(len(spec["rollback_mutations"]), 1)
        self.assertTrue(spec["rollback_mutations"][0]["changed"])
        self.assertEqual(spec["rollback_mutations"][0]["kind"], "mkdir")
        # ... and desired.content elided from every operation (T-2917:D16)
        self.assertTrue(all("content" not in op["desired"] for op in spec["operations"]))
        self.assertTrue(any("sha256" in op["desired"] for op in spec["operations"]))

    def test_spec_digest_helper_matches_the_wire_digest(self) -> None:
        payload = self.source.spec_for(self.record)
        self.assertEqual(spec_digest(payload), self.digest)


@_bus_tests.requires_dbus
class OperationSpecHandlerTests(unittest.TestCase):
    def test_unregistered_uid_is_refused_before_the_domain(self) -> None:
        import dbus as dbus_module

        sentinel = mock.Mock()
        instance = _bus_tests._bare_service(
            _registry=_bus_tests.FakeRegistryUnregistered(),
            _execution_source=sentinel,
        )
        instance._get_unix_user = lambda sender: 4242
        with self.assertRaises(dbus_module.DBusException) as caught:
            _bus_tests.dbus_service.HostBroker1Object.OperationSpec(
                instance, "a" * 64, "b" * 64, sender=":1.9"
            )
        self.assertEqual(caught.exception.get_dbus_name(), wire.POLICY_DENIED_ERROR)
        sentinel.operation_spec.assert_not_called()

    def test_unconfigured_source_is_unsupported(self) -> None:
        import dbus as dbus_module

        instance = _bus_tests._bare_service(
            _registry=_bus_tests.FakeRegistryResolves(),
            _execution_source=None,
        )
        instance._get_unix_user = lambda sender: 1003
        with self.assertRaises(dbus_module.DBusException) as caught:
            _bus_tests.dbus_service.HostBroker1Object.OperationSpec(
                instance, "a" * 64, "b" * 64, sender=":1.9"
            )
        self.assertEqual(caught.exception.get_dbus_name(), wire.UNSUPPORTED_ERROR)


@_bus_tests.requires_private_bus
class OperationSpecBusTests(_bus_tests.PrivateBusCase):
    """Over a real private bus: the registered REQUESTER (this process's
    uid, seat alpha) cannot read the target seat's execution spec."""

    def test_requester_cannot_read_another_seats_spec(self) -> None:
        exc = self.assert_wire_error(
            wire.POLICY_DENIED_ERROR,
            "OperationSpec",
            "ss",
            (self.state["seat_b_operation_handle"], "b" * 64),
            interface=wire.EXECUTION_SOURCE_INTERFACE,
        )
        self.assertEqual(exc.get_dbus_message(), "subject_denied")


if __name__ == "__main__":
    unittest.main()
