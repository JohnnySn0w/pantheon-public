"""T-5644: ApprovalGet is a deferred-reply handler; its status query never
blocks the broker's GLib main loop.

Handler-level tests: the decorated method is called directly on a bare
instance with a scripted async transport, so every completion path (ok,
error, timeout, exception in finish, shutdown, vanished caller, duplicate
completion) and every phase-2 re-check is deterministic and event-driven.
The bus-level stall reproduction lives in test_host_control_dbus.py
(ApprovalGetDoesNotBlockTheLoopBusTests)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tools import test_host_control_dbus as dbus_tests
from tools.host_control import dbus_service, wire
from tools.host_control.approval_client import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    ApprovalConsumerClient,
    ConsumerCallError,
    DBusConsumerTransport,
)
from tools.host_control.contracts import (
    ApprovalUnavailable,
    RegistryError,
    SubjectContext,
    UnknownHandle,
)

try:
    import dbus  # noqa: F401

    _DBUS = True
except ImportError:
    _DBUS = False

HANDLE = "a" * 64
PLAN = "e" * 64
SUBJECT = SubjectContext(uid=1003, seat_id="alpha", persona="alpha", role="worker", instance="t")
JOURNAL_PY = Path(__file__).resolve().parent / "host_control" / "journal.py"
UNKNOWN_HANDLE_NAME = wire.error_name_for(UnknownHandle("x"))
UNAVAILABLE_NAME = wire.error_name_for(ApprovalUnavailable("x"))


def _status(handle: str = HANDLE) -> tuple[str, str]:
    return (
        "issued",
        json.dumps(
            {
                "schema": "pantheon-approval-status/v2",
                "approval_handle": handle,
                "state": "issued",
                "expires_at": "2026-10-10T01:00:00+00:00",
                "summary": "bounded",
                "plan_handle": PLAN,
                "plan_owner": "alpha",
            },
            sort_keys=True,
        ),
    )


class AsyncTransport:
    """Holds every ApprovalStatus request until the test completes it."""

    def __init__(self) -> None:
        self.pending: list[tuple] = []

    def call(self, method, signature, args):  # pragma: no cover - must stay unused
        raise AssertionError("the blocking call must not be used")

    def call_async(self, method, signature, args, reply_handler, error_handler):
        self.pending.append((args, reply_handler, error_handler))


class Ownership:
    def __init__(self) -> None:
        self.rows = {(SUBJECT.uid, HANDLE, PLAN)}
        self.reads = 0

    def subject_references_approval(self, *, subject, approval_handle):
        self.reads += 1
        return any(u == subject.uid and h == approval_handle for u, h, _ in self.rows)

    def subject_operation_binds_plan(self, *, subject, approval_handle, plan_handle):
        self.reads += 1
        return (subject.uid, approval_handle, plan_handle) in self.rows


class Registry:
    def __init__(self) -> None:
        self.subject: object = SUBJECT
        self.fail = False

    def subject_for_uid(self, uid):
        if self.fail:
            raise RegistryError("authenticated uid is not registered")
        return self.subject


class Harness:
    def __init__(self) -> None:
        self.transport = AsyncTransport()
        self.ownership = Ownership()
        self.registry = Registry()
        client = ApprovalConsumerClient(transport=self.transport, ownership=self.ownership)
        self.service = dbus_tests._bare_service(_registry=self.registry, _approval_status=client)
        self.service._get_unix_user = lambda sender: int(sender)
        self.replies: list[tuple] = []
        self.errors: list[BaseException] = []

    def call(self, sender: str = str(SUBJECT.uid), handle: str = HANDLE) -> None:
        dbus_service.HostBroker1Object.ApprovalGet(
            self.service,
            handle,
            sender,
            lambda *values: self.replies.append(values),
            self.errors.append,
        )

    def inflight(self) -> tuple[int, dict]:
        state = self.service._approval_get_state()
        return state["total"], dict(state["by_uid"])

    def settled(self) -> bool:
        return self.inflight() == (0, {})


@unittest.skipUnless(dbus_service.DBUS_AVAILABLE, "needs dbus-python")
class CompletionPathTests(unittest.TestCase):
    def test_reply_ok_replies_once_and_frees_the_slot(self) -> None:
        h = Harness()
        h.call()
        self.assertEqual(h.replies, [])
        self.assertEqual(h.inflight(), (1, {SUBJECT.uid: 1}))
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [_status()])
        self.assertEqual(h.errors, [])
        self.assertTrue(h.settled())

    def test_transport_error_keeps_wire_name(self) -> None:
        for name, expected in (
            ("org.pantheon.HostApproval1.Error.UnknownHandle", UNKNOWN_HANDLE_NAME),
            ("org.freedesktop.DBus.Error.NoReply", UNAVAILABLE_NAME),
        ):
            with self.subTest(name=name):
                h = Harness()
                h.call()
                h.transport.pending[0][2](ConsumerCallError(name, "body"))
                self.assertEqual(h.replies, [])
                self.assertEqual(len(h.errors), 1)
                self.assertEqual(h.errors[0].get_dbus_name(), expected)
                self.assertTrue(h.settled())

    def test_timeout_error_maps_to_approval_unavailable_and_frees_the_slot(self) -> None:
        h = Harness()
        h.call()
        h.transport.pending[0][2](ConsumerCallError("org.freedesktop.DBus.Error.NoReply", ""))
        self.assertEqual(h.errors[0].get_dbus_name(), UNAVAILABLE_NAME)
        self.assertEqual(h.errors[0].get_dbus_message(), "approval authority is not available")
        self.assertTrue(h.settled())

    def test_exception_in_finish_is_an_error_reply_and_frees_the_slot(self) -> None:
        h = Harness()
        h.call()
        h.transport.pending[0][1](("not-a-state", "{}"))  # malformed reply
        self.assertEqual(h.replies, [])
        self.assertEqual(len(h.errors), 1)
        self.assertTrue(h.settled())

    def test_synchronous_precheck_refusal_raises_and_frees_the_slot(self) -> None:
        h = Harness()
        h.ownership.rows.clear()
        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            h.call()
        self.assertEqual(caught.exception.get_dbus_name(), UNKNOWN_HANDLE_NAME)
        self.assertEqual(h.transport.pending, [])
        self.assertTrue(h.settled())

    def test_duplicate_completion_replies_and_releases_once(self) -> None:
        h = Harness()
        h.call()
        _args, reply_handler, error_handler = h.transport.pending[0]
        reply_handler(_status())
        reply_handler(_status())
        error_handler(ConsumerCallError("org.freedesktop.DBus.Error.NoReply", ""))
        self.assertEqual(len(h.replies), 1)
        self.assertEqual(h.errors, [])
        self.assertTrue(h.settled())

    def test_vanished_caller_does_not_leak_the_slot(self) -> None:
        h = Harness()

        def gone(*values):
            raise RuntimeError("peer disconnected")

        dbus_service.HostBroker1Object.ApprovalGet(
            h.service, HANDLE, str(SUBJECT.uid), gone, h.errors.append
        )
        with self.assertRaises(RuntimeError):
            h.transport.pending[0][1](_status())
        self.assertTrue(h.settled())

    def test_close_drops_a_late_reply_without_touching_the_journal(self) -> None:
        h = Harness()
        h.call()
        reads_before = h.ownership.reads
        h.service.close()
        h.transport.pending[0][1](_status())
        h.transport.pending[0][2](ConsumerCallError("org.freedesktop.DBus.Error.NoReply", ""))
        self.assertEqual((h.replies, h.errors), ([], []))
        self.assertEqual(h.ownership.reads, reads_before)
        self.assertTrue(h.settled())


@unittest.skipUnless(dbus_service.DBUS_AVAILABLE, "needs dbus-python")
class InflightBoundTests(unittest.TestCase):
    def test_pinned_constants(self) -> None:
        self.assertEqual(dbus_service.MAX_APPROVAL_GET_INFLIGHT_PER_UID, 4)
        self.assertEqual(dbus_service.MAX_APPROVAL_GET_INFLIGHT_TOTAL, 16)

    def test_per_uid_cap_refuses_with_the_existing_rate_limited_error(self) -> None:
        h = Harness()
        for _ in range(dbus_service.MAX_APPROVAL_GET_INFLIGHT_PER_UID):
            h.call()
        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            h.call()
        self.assertEqual(caught.exception.get_dbus_name(), wire.RATE_LIMITED_ERROR)
        self.assertEqual(caught.exception.get_dbus_message(), "read request budget exhausted; retry later")
        self.assertEqual(h.inflight(), (4, {SUBJECT.uid: 4}))
        h.transport.pending[0][1](_status())
        h.call()  # a freed slot is reusable
        self.assertEqual(h.inflight()[0], 4)

    def test_global_cap_spans_uids(self) -> None:
        h = Harness()
        h.registry.subject_for_uid = lambda uid: SubjectContext(
            uid=uid, seat_id="alpha", persona="alpha", role="worker", instance="t"
        )
        h.ownership.subject_references_approval = lambda **kw: True
        per_uid = dbus_service.MAX_APPROVAL_GET_INFLIGHT_PER_UID
        uids = dbus_service.MAX_APPROVAL_GET_INFLIGHT_TOTAL // per_uid
        for uid in range(2000, 2000 + uids):
            for _ in range(per_uid):
                h.call(sender=str(uid))
        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            h.call(sender="3000")
        self.assertEqual(caught.exception.get_dbus_name(), wire.RATE_LIMITED_ERROR)
        self.assertEqual(h.inflight()[0], dbus_service.MAX_APPROVAL_GET_INFLIGHT_TOTAL)


@unittest.skipUnless(dbus_service.DBUS_AVAILABLE, "needs dbus-python")
class MutationBetweenPhasesTests(unittest.TestCase):
    """The state a concurrent handler can change between begin and finish."""

    def test_unrelated_journal_activity_does_not_change_the_answer(self) -> None:
        h = Harness()
        h.call()
        # a concurrent Apply inserts another row; cancel/complete only change
        # operation state, which the binding predicate never reads
        h.ownership.rows.add((SUBJECT.uid, "b" * 64, "f" * 64))
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [_status()])

    def test_binding_vanishing_fails_closed_as_unknown_handle(self) -> None:
        h = Harness()
        h.call()
        h.ownership.rows.clear()
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [])
        self.assertEqual(h.errors[0].get_dbus_name(), UNKNOWN_HANDLE_NAME)
        self.assertEqual(h.errors[0].get_dbus_message(), "unknown approval handle")
        self.assertTrue(h.settled())

    def test_binding_to_a_different_plan_fails_closed(self) -> None:
        h = Harness()
        h.call()
        h.ownership.rows = {(SUBJECT.uid, HANDLE, "f" * 64)}
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [])
        self.assertEqual(h.errors[0].get_dbus_message(), "unknown approval handle")

    def test_subject_changed_by_a_registry_reload_is_policy_denied(self) -> None:
        h = Harness()
        h.call()
        h.registry.subject = SubjectContext(
            uid=SUBJECT.uid, seat_id="bravo", persona="b", role="w", instance="t"
        )
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [])
        self.assertEqual(h.errors[0].get_dbus_name(), wire.POLICY_DENIED_ERROR)
        self.assertEqual(h.errors[0].get_dbus_message(), "subject_denied")
        self.assertTrue(h.settled())

    def test_subject_removed_by_a_registry_reload_is_policy_denied(self) -> None:
        h = Harness()
        h.call()
        h.registry.fail = True
        h.transport.pending[0][1](_status())
        self.assertEqual(h.replies, [])
        self.assertEqual(h.errors[0].get_dbus_name(), wire.POLICY_DENIED_ERROR)
        self.assertTrue(h.settled())


class JournalImmutabilityPinTests(unittest.TestCase):
    """The monotone-predicate argument in ApprovalGet's comment rests on no
    host_control module re-keying or deleting an operations row."""

    BINDING_COLUMNS = ("subject_uid", "subject_seat_id", "approval_handle", "plan_handle")

    def _sources(self):
        return sorted(JOURNAL_PY.parent.rglob("*.py"))

    def test_no_module_updates_binding_columns_or_deletes_operations(self) -> None:
        updates = 0
        for path in self._sources():
            source = re.sub(r'"\s*\n\s*f?"', "", path.read_text(encoding="utf-8"))
            for statement in re.findall(r"UPDATE\s+operations\s+SET\s+(.*?)WHERE", source, re.S | re.I):
                updates += 1
                for column in self.BINDING_COLUMNS:
                    self.assertNotIn(column, statement, str(path))
            self.assertNotRegex(source, r"(?i)DELETE\s+FROM\s+operations", str(path))
            self.assertNotRegex(source, r"(?i)(INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+operations", str(path))
            self.assertNotRegex(source, r"(?i)DROP\s+TABLE\s+(IF\s+EXISTS\s+)?operations", str(path))
        self.assertGreaterEqual(updates, 2)


@unittest.skipUnless(dbus_service.DBUS_AVAILABLE, "needs dbus-python")
class WireContractPinTests(unittest.TestCase):
    def test_decorator_keeps_signature_and_names_the_async_callbacks(self) -> None:
        method = dbus_service.HostBroker1Object.ApprovalGet
        self.assertEqual(method._dbus_interface, wire.MANAGER_INTERFACE)
        self.assertEqual(method._dbus_in_signature, "s")
        self.assertEqual(method._dbus_out_signature, "ss")
        self.assertEqual(method._dbus_async_callbacks, ("reply_cb", "error_cb"))

    def test_client_timeout_is_still_ten_seconds(self) -> None:
        self.assertEqual(DEFAULT_CALL_TIMEOUT_SECONDS, 10.0)
        self.assertEqual(DBusConsumerTransport(lambda: None)._timeout, 10.0)


@unittest.skipUnless(_DBUS, "needs dbus-python")
class TransportCallAsyncTests(unittest.TestCase):
    def test_call_async_refuses_a_connection_not_attached_to_a_main_loop(self) -> None:
        # defence in depth (T-5644): a blocking-only connection never fires
        # call_async handlers or its timeout, which would leak the slot
        class Connection:
            def call_async(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("must not send")

        transport = DBusConsumerTransport(lambda: Connection())
        with self.assertRaises(ConsumerCallError) as caught:
            transport.call_async("ApprovalStatus", "s", (HANDLE,), lambda r: None, lambda e: None)
        self.assertEqual(caught.exception.name, "main-loop-required")

    def test_handler_refuses_loudly_and_frees_the_slot_on_a_misconfigured_wiring(self) -> None:
        h = Harness()
        h.service._approval_status = ApprovalConsumerClient(
            transport=DBusConsumerTransport(lambda: object()), ownership=h.ownership
        )
        with self.assertRaises(dbus.exceptions.DBusException):
            h.call()
        self.assertTrue(h.settled())

    def test_call_async_sends_with_the_client_timeout_and_maps_handlers(self) -> None:
        sent: dict = {}

        class Connection:
            def call_async(self, *args, **kwargs):
                sent["args"], sent["kwargs"] = args, kwargs

        transport = DBusConsumerTransport(lambda: Connection(), main_loop_attached=True)
        replies, errors = [], []
        transport.call_async("ApprovalStatus", "s", (HANDLE,), replies.append, errors.append)
        self.assertEqual(sent["kwargs"], {"timeout": 10.0})
        self.assertEqual(sent["args"][3:6], ("ApprovalStatus", "s", (HANDLE,)))
        on_reply, on_error = sent["args"][6], sent["args"][7]
        on_reply("issued", "{}")
        on_reply("only-one")
        on_error(dbus.exceptions.DBusException("m", name="org.freedesktop.DBus.Error.NoReply"))
        self.assertEqual(replies, [("issued", "{}")])
        self.assertEqual(
            [(e.name, e.body) for e in errors],
            [("malformed", "malformed reply"), ("org.freedesktop.DBus.Error.NoReply", "m")],
        )


def _have_private_bus() -> bool:
    import shutil

    return _DBUS and shutil.which("dbus-daemon") is not None


@unittest.skipUnless(_have_private_bus(), "needs dbus-python and dbus-daemon")
class ProductionWiringTests(unittest.TestCase):
    """T-5644 r1 BLOCKER: the status connection the broker builds must be
    attached to the GLib loop, or call_async never replies (every ApprovalGet
    then hangs and leaks its slot). This builds the status client through
    ``__main__.build_status_client`` and ``__main__.open_bus``, the exact code
    ``run`` uses, over a private bus against the fake Consumer, then pumps the
    default GLib context. The wait is bounded, so a hang is a failure."""

    DEADLINE_SECONDS = 8.0

    @classmethod
    def setUpClass(cls) -> None:
        import os
        import subprocess
        import sys
        import time

        import tools._host_control_testbus as testbus
        from tools import test_host_control_approval_client as client_tests

        cls.testbus = testbus
        cls.handle = testbus.spawn_daemon("t5644-", client_tests._BUS_CONFIG)
        ready = os.path.join(cls.handle.bus_dir, "ready")
        cls.log = open(os.path.join(cls.handle.bus_dir, "driver.log"), "w", encoding="utf-8")  # noqa: SIM115
        cls.driver = subprocess.Popen(
            [sys.executable, "-m", "tools.test_host_control_approval_client_driver", cls.handle.bus_address, ready],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=cls.log,
            stderr=cls.log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        cls.handle.register_driver(cls.driver.pid)
        deadline = time.monotonic() + 30
        while not os.path.exists(ready):
            if time.monotonic() > deadline or cls.driver.poll() is not None:
                cls.tearDownClass()
                raise AssertionError("fake consumer never became ready")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.driver.poll() is None:
            cls.driver.terminate()
            try:
                cls.driver.wait(timeout=10)
            except Exception:
                cls.driver.kill()
                cls.driver.wait(timeout=10)
        cls.log.close()
        cls.testbus.stop_daemon(cls.handle)

    def _connect(self, main_loop: bool):
        """``__main__.system_bus`` with the private test bus as the target:
        the main-loop decision is the production ``open_bus``."""

        import dbus.bus
        from dbus.mainloop.glib import DBusGMainLoop

        from tools.host_control import __main__ as entry

        address = self.handle.bus_address

        def constructor(*, private: bool, mainloop=None):
            return dbus.bus.BusConnection(address, mainloop=mainloop or DBusGMainLoop())

        return entry.open_bus(constructor, main_loop)

    def _pump(self, done) -> None:
        from gi.repository import GLib

        context = GLib.MainContext.default()
        expired = []
        source = GLib.timeout_add(int(self.DEADLINE_SECONDS * 1000), lambda: expired.append(1) or False)
        try:
            while not done() and not expired:
                context.iteration(True)
        finally:
            if not expired:
                GLib.source_remove(source)

    def _service(self):
        from tools import test_host_control_approval_client_driver as fake
        from tools.host_control import __main__ as entry

        ownership = Ownership()
        ownership.rows = {(SUBJECT.uid, fake.BOUND, fake.PLAN)}
        client, status_connection = entry.build_status_client(self._connect, ownership)
        self.addCleanup(status_connection.close)
        harness = Harness()
        harness.ownership = ownership
        harness.service._approval_status = client
        return harness, fake

    def test_approval_get_replies_through_the_production_status_wiring(self) -> None:
        harness, fake = self._service()
        harness.call(handle=fake.BOUND)
        self.assertEqual(harness.inflight(), (1, {SUBJECT.uid: 1}))
        self._pump(lambda: harness.replies or harness.errors)
        self.assertEqual(harness.errors, [], "ApprovalGet errored")
        self.assertEqual(len(harness.replies), 1, "no reply: the status connection has no main loop")
        self.assertEqual(harness.replies[0][0], "bound")
        self.assertTrue(harness.settled())

    def test_the_production_status_connection_is_main_loop_attached(self) -> None:
        from tools.host_control import __main__ as entry

        seen: list[bool] = []

        def spy(main_loop: bool):
            seen.append(main_loop)
            return self._connect(main_loop)

        _client, status_connection = entry.build_status_client(spy, Ownership())
        self.addCleanup(status_connection.close)
        status_connection()
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
