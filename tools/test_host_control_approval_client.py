"""ORG-052 Unit 5a (T-2917): broker-side HostApproval1.Consumer client
(the T-2913 OQ6 deferral) and ApprovalGet subject binding.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.host_control import approval_client as client_module
from tools.host_control.approval import contracts as approval_contracts
from tools.host_control.approval import wire as approval_wire
from tools.host_control.approval_client import (
    ApprovalConsumerClient,
    ConsumerCallError,
    DBusConsumerTransport,
    bind_outcome_from_error,
    bind_outcome_from_reply,
    status_from_reply,
)
from tools.host_control.contracts import (
    ApprovalUnavailable,
    HostControlError,
    SubjectContext,
    UnknownHandle,
)
from tools import test_host_control_approval_client_driver as fake

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"
SUBJECT = SubjectContext(uid=1003, seat_id="alpha", persona="alpha", role="worker", instance="t")
OTHER = SubjectContext(uid=1004, seat_id="bravo", persona="bravo", role="worker", instance="t")
HANDLE = "a" * 64


def _status_json(handle: str, state: str = "issued") -> str:
    return json.dumps(
        {
            "schema": "pantheon-approval-status/v1",
            "approval_handle": handle,
            "state": state,
            "expires_at": "2026-10-10T01:00:00+00:00",
            "summary": "bounded",
        },
        sort_keys=True,
    )


class FakeTransport:
    def __init__(self, reply=None, error: ConsumerCallError | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def call(self, method, signature, args):
        self.calls.append((method, signature, args))
        if self.error is not None:
            raise self.error
        return self.reply


class FakeOwnership:
    def __init__(self, owned: set[tuple[int, str]]) -> None:
        self.owned = owned

    def subject_references_approval(self, *, subject, approval_handle):
        return (subject.uid, approval_handle) in self.owned


class TranscriptionPinTests(unittest.TestCase):
    """Gate 18: the client transcribes instead of importing the approval
    package; these pins make any drift fail loudly."""

    def test_transcribed_names_match_the_approval_package(self) -> None:
        self.assertEqual(client_module.APPROVAL_BUS_NAME, approval_wire.BUS_NAME)
        self.assertEqual(client_module.APPROVAL_OBJECT_PATH, approval_wire.OBJECT_PATH)
        self.assertEqual(client_module.CONSUMER_INTERFACE, approval_wire.CONSUMER_INTERFACE)
        self.assertEqual(client_module.APPROVAL_UNKNOWN_HANDLE_ERROR, approval_wire.UNKNOWN_HANDLE_ERROR)
        self.assertEqual(client_module.APPROVAL_BIND_DENIED_ERROR, approval_wire.BIND_DENIED_ERROR)
        self.assertEqual(
            client_module.APPROVAL_METADATA_UNAVAILABLE_ERROR,
            approval_wire.METADATA_UNAVAILABLE_ERROR,
        )
        self.assertEqual(client_module.APPROVAL_STATUS_SCHEMA, approval_wire.APPROVAL_STATUS_SCHEMA)
        self.assertEqual(client_module.GRANT_STATES, approval_contracts.GRANT_STATES)
        self.assertEqual(
            client_module.BIND_DENIAL_REASON_CODES,
            approval_contracts.BIND_DENIAL_REASON_CODES,
        )

    def test_client_module_imports_no_approval_package(self) -> None:
        tree = ast.parse((HOST_CONTROL / "approval_client.py").read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                if node.module == "tools.host_control":
                    names.update(f"tools.host_control.{alias.name}" for alias in node.names)
        self.assertFalse(
            any(n == "tools.host_control.approval" or n.startswith("tools.host_control.approval.") for n in names),
            names,
        )


class BindMappingTests(unittest.TestCase):
    def test_bound_reply_maps_to_bound_outcome(self) -> None:
        outcome = bind_outcome_from_reply(("bound", "e" * 64))
        self.assertEqual((outcome.state, outcome.authority_record_id), ("bound", "e" * 64))

    def test_malformed_bound_replies_never_grant(self) -> None:
        for reply in (("bound", "short"), ("denied", "e" * 64), ("bound",), "bound", ("bound", "E" * 64)):
            with self.subTest(reply=reply):
                with self.assertRaises(HostControlError):
                    bind_outcome_from_reply(reply)

    def test_every_closed_denial_reason_is_a_denied_outcome(self) -> None:
        for reason in sorted(approval_contracts.BIND_DENIAL_REASON_CODES):
            with self.subTest(reason=reason):
                outcome = bind_outcome_from_error(
                    ConsumerCallError(approval_wire.BIND_DENIED_ERROR, reason)
                )
                self.assertEqual((outcome.state, outcome.reason_code), ("denied", reason))
                self.assertIsNone(outcome.authority_record_id)

    def test_unknown_denial_reason_raises_never_denies_or_grants(self) -> None:
        with self.assertRaises(HostControlError) as caught:
            bind_outcome_from_error(ConsumerCallError(approval_wire.BIND_DENIED_ERROR, "made-up"))
        self.assertNotIsInstance(caught.exception, ApprovalUnavailable)

    def test_unknown_handle_raises_so_the_operation_stays_prepared(self) -> None:
        # T-2911 override 2: a vanished plan must not burn the operation.
        with self.assertRaises(UnknownHandle):
            bind_outcome_from_error(ConsumerCallError(approval_wire.UNKNOWN_HANDLE_ERROR, "x"))

    def test_outage_and_transport_faults_are_approval_unavailable(self) -> None:
        names = sorted(client_module.TRANSPORT_UNAVAILABLE_ERRORS) + [
            approval_wire.METADATA_UNAVAILABLE_ERROR
        ]
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ApprovalUnavailable):
                    bind_outcome_from_error(ConsumerCallError(name, "x"))

    def test_other_named_errors_are_bounded_failures(self) -> None:
        for name in (approval_wire.POLICY_DENIED_ERROR, approval_wire.INTERNAL_ERROR, "x.y.Z"):
            with self.subTest(name=name):
                with self.assertRaises(HostControlError) as caught:
                    bind_outcome_from_error(ConsumerCallError(name, "secret /etc/x"))
                self.assertNotIsInstance(caught.exception, (ApprovalUnavailable, UnknownHandle))
                self.assertNotIn("/etc", str(caught.exception))


class ClientTests(unittest.TestCase):
    def test_non_hex_approval_handle_is_denied_without_a_call(self) -> None:
        transport = FakeTransport(reply=("bound", "e" * 64))
        client = ApprovalConsumerClient(transport=transport, ownership=FakeOwnership(set()))
        for bad in ("approval-x", "A" * 64, "a" * 63, ""):
            with self.subTest(handle=bad):
                outcome = client.validate_and_bind(bad, "b" * 64, "c" * 64, "d" * 64)
                self.assertEqual((outcome.state, outcome.reason_code), ("denied", "unknown_approval"))
        self.assertEqual(transport.calls, [])

    def test_validate_and_bind_passes_the_exact_tuple(self) -> None:
        transport = FakeTransport(reply=("bound", "e" * 64))
        client = ApprovalConsumerClient(transport=transport, ownership=FakeOwnership(set()))
        outcome = client.validate_and_bind(HANDLE, "b" * 64, "c" * 64, "d" * 64)
        self.assertEqual(outcome.state, "bound")
        self.assertEqual(
            transport.calls,
            [("ValidateAndBind", "ssss", (HANDLE, "b" * 64, "c" * 64, "d" * 64))],
        )

    def test_approval_get_is_bound_to_the_calling_subject(self) -> None:
        transport = FakeTransport(reply=("issued", _status_json(HANDLE)))
        client = ApprovalConsumerClient(
            transport=transport, ownership=FakeOwnership({(SUBJECT.uid, HANDLE)})
        )
        self.assertEqual(client.approval_status(SUBJECT, HANDLE)[0], "issued")
        # gate 15: the same handle from another subject learns nothing and
        # the approval service is never even asked.
        with self.assertRaises(UnknownHandle):
            client.approval_status(OTHER, HANDLE)
        with self.assertRaises(UnknownHandle):
            client.approval_status(SUBJECT, "approval-x")
        self.assertEqual(transport.calls, [("ApprovalStatus", "s", (HANDLE,))])

    def test_approval_get_maps_errors_and_validates_payload(self) -> None:
        ownership = FakeOwnership({(SUBJECT.uid, HANDLE)})
        cases = {
            approval_wire.UNKNOWN_HANDLE_ERROR: UnknownHandle,
            "org.freedesktop.DBus.Error.ServiceUnknown": ApprovalUnavailable,
            approval_wire.FAILED_ERROR: HostControlError,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                client = ApprovalConsumerClient(
                    transport=FakeTransport(error=ConsumerCallError(name, "x")), ownership=ownership
                )
                with self.assertRaises(expected):
                    client.approval_status(SUBJECT, HANDLE)

    def test_status_payload_must_match_the_request(self) -> None:
        for reply in (
            ("granted", _status_json(HANDLE)),
            ("issued", _status_json("b" * 64)),
            ("issued", _status_json(HANDLE, state="bound")),
            ("issued", "not json"),
            ("issued", json.dumps({"schema": "other"})),
            ("issued", "x" * 70000),
        ):
            with self.subTest(reply=str(reply)[:60]):
                with self.assertRaises(HostControlError):
                    status_from_reply(HANDLE, reply)


class JournalOwnershipTests(unittest.TestCase):
    """The real read-only journal query behind ApprovalGet subject binding."""

    def test_subject_references_only_its_own_operation_handles(self) -> None:
        from tools.test_host_control_dbus_driver import MutableClock, build_domain, FIXED_NOW

        with tempfile.TemporaryDirectory() as temp:
            fixture = build_domain(Path(temp) / "domain", subject_uid=1003, clock=MutableClock(FIXED_NOW))
            try:
                plan = fixture.plan_service.plan(1003, "recovery", "manifest-r1", "own-plan")
                fixture.operation_service.prepare_apply(
                    1003, plan.plan_handle, plan.plan_digest, "own-apply", HANDLE
                )
                alpha = fixture.registry.subject_for_uid(1003)
                bravo = fixture.registry.subject_for_uid(60001)
                journal = fixture.journal
                self.assertTrue(journal.subject_references_approval(subject=alpha, approval_handle=HANDLE))
                self.assertFalse(journal.subject_references_approval(subject=bravo, approval_handle=HANDLE))
                self.assertFalse(journal.subject_references_approval(subject=alpha, approval_handle="b" * 64))
            finally:
                fixture.journal.close()
                fixture.files.close()


DBUS_DAEMON = shutil.which("dbus-daemon")
try:
    import dbus  # noqa: F401

    _DBUS = True
except ImportError:  # pragma: no cover
    _DBUS = False

_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@unittest.skipUnless(_DBUS and DBUS_DAEMON, "private-bus test needs dbus-python and dbus-daemon")
class ConsumerTransportPrivateBusTests(unittest.TestCase):
    """DBusConsumerTransport over a REAL private dbus-daemon against a fake
    Consumer object (never the system bus)."""

    @classmethod
    def setUpClass(cls) -> None:
        import tools._host_control_testbus as testbus

        cls.testbus = testbus
        cls.handle = testbus.spawn_daemon("t2917-", _BUS_CONFIG)
        cls.ready = os.path.join(cls.handle.bus_dir, "ready")
        cls.log = open(os.path.join(cls.handle.bus_dir, "driver.log"), "w", encoding="utf-8")  # noqa: SIM115
        cls.driver = subprocess.Popen(
            [sys.executable, "-m", "tools.test_host_control_approval_client_driver", cls.handle.bus_address, cls.ready],
            cwd=REPO_ROOT,
            stdout=cls.log,
            stderr=cls.log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        cls.handle.register_driver(cls.driver.pid)
        deadline = time.monotonic() + 30
        while not os.path.exists(cls.ready):
            if time.monotonic() > deadline or cls.driver.poll() is not None:
                cls.log.flush()
                log_text = Path(cls.log.name).read_text(encoding="utf-8")[-2000:]
                cls.tearDownClass()  # setUpClass failure skips tearDownClass
                raise AssertionError("fake consumer never became ready; log: " + log_text)
            time.sleep(0.05)
        import dbus.bus

        cls.connection = dbus.bus.BusConnection(cls.handle.bus_address)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.driver.poll() is None:
            cls.driver.terminate()
            try:
                cls.driver.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.driver.kill()
                cls.driver.wait(timeout=10)
        cls.log.close()
        cls.testbus.stop_daemon(cls.handle)

    def _client(self, owned=frozenset()) -> ApprovalConsumerClient:
        transport = DBusConsumerTransport(lambda: self.connection, timeout=5.0)
        return ApprovalConsumerClient(transport=transport, ownership=FakeOwnership(set(owned)))

    def test_bind_outcomes_over_the_bus(self) -> None:
        client = self._client()
        args = ("b" * 64, "c" * 64, "d" * 64)
        bound = client.validate_and_bind(fake.BOUND, *args)
        self.assertEqual((bound.state, bound.authority_record_id), ("bound", fake.AUTHORITY_RECORD))
        denied = client.validate_and_bind(fake.DENIED_EXPIRED, *args)
        self.assertEqual((denied.state, denied.reason_code), ("denied", "expired"))
        with self.assertRaises(UnknownHandle):
            client.validate_and_bind(fake.UNKNOWN, *args)
        with self.assertRaises(ApprovalUnavailable):
            client.validate_and_bind(fake.METADATA_DOWN, *args)
        with self.assertRaises(HostControlError):
            client.validate_and_bind(fake.DENIED_UNKNOWN_CODE, *args)

    def test_status_over_the_bus_is_subject_bound(self) -> None:
        client = self._client({(SUBJECT.uid, fake.BOUND)})
        state, payload = client.approval_status(SUBJECT, fake.BOUND)
        self.assertEqual(state, "bound")
        self.assertEqual(json.loads(payload)["approval_handle"], fake.BOUND)
        with self.assertRaises(UnknownHandle):
            client.approval_status(OTHER, fake.BOUND)

    def test_absent_authority_is_approval_unavailable(self) -> None:
        import dbus.bus

        transport = DBusConsumerTransport(lambda: self.connection, timeout=5.0)
        # a name nobody owns on this bus: same mapping as an absent service
        original = client_module.APPROVAL_BUS_NAME
        try:
            client_module.APPROVAL_BUS_NAME = "org.pantheon.HostApproval1.Absent"
            client = ApprovalConsumerClient(transport=transport, ownership=FakeOwnership(set()))
            with self.assertRaises(ApprovalUnavailable):
                client.validate_and_bind(fake.BOUND, "b" * 64, "c" * 64, "d" * 64)
        finally:
            client_module.APPROVAL_BUS_NAME = original
        self.assertIsInstance(self.connection, dbus.bus.BusConnection)


if __name__ == "__main__":
    unittest.main()
