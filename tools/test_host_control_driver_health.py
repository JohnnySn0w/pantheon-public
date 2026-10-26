"""T-2917 5d.4: the broker's Health1 legs from the operation driver (ruling
T-2917:D21; HO ruling: ``operation_driver`` joins the CLOSED
``pantheon-health/v1`` broker roster, PROVISIONAL name): the driver's
lock-guarded health cache, the ``operation_driver`` / ``executor_reachability``
/ ``approval_reachability`` probes, the bus reachability source, and a
private-bus cell where Health1 flips when the driver dies.

Fixture evidence: fake journals and transports in-process; the bus cell
runs a private dbus-daemon (``skipUnless``; CI skips it, T-4931).
"""

from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace

from tools.host_control.driver import (
    HEALTH_REVISION,
    DriverContext,
    OperationDriver,
    approval_reachability_probe,
    executor_reachability_probe,
    operation_driver_probe,
)
from tools.host_control.executor import wire as executor_wire
from tools.host_control.executor_client import APPROVAL_BUS_NAME, BusReachability, ExecutorCallError
from tools.host_control.health import (
    BROKER_ROSTER,
    HealthProbeResult,
    make_broker_health_provider,
    serialize_health,
)

WAIT = 30.0


def _ready(revision: str):
    return lambda: HealthProbeResult("ready", "", revision)


class _Journal:
    def __init__(self, pending=lambda: ()) -> None:
        self._pending = pending
        self.closed = False

    def pending_operations(self):
        return self._pending()

    def close(self) -> None:
        self.closed = True


def _context(journal: _Journal, reachability=None) -> DriverContext:
    return DriverContext(
        service=SimpleNamespace(),  # type: ignore[arg-type]
        journal=journal,  # type: ignore[arg-type]
        port=SimpleNamespace(),  # type: ignore[arg-type]
        close=journal.close,
        reachability=reachability,
    )


class _Source:
    """A reachability source that counts calls and can block or fire an
    event on a given call."""

    def __init__(self, executor=None, approval=None, *, block: threading.Event | None = None) -> None:
        self.executor = executor or HealthProbeResult("ready", "", "x")
        self.approval = approval or HealthProbeResult("ready", "", "x")
        self.block = block
        self.calls = 0
        self.entered = threading.Event()
        self.nth = threading.Event()
        self.target = 0

    def executor_reachability(self) -> HealthProbeResult:
        self.calls += 1
        self.entered.set()
        if self.block is not None:
            self.block.wait(WAIT)
        if self.target and self.calls >= self.target:
            self.nth.set()
        return self.executor

    def approval_reachability(self) -> HealthProbeResult:
        return self.approval


class _Transport:
    def __init__(self, owners: dict[str, tuple[str, int]], activatable=()) -> None:
        self.owners = owners
        self.activatable = tuple(activatable)

    def name_owner(self, name: str) -> str:
        if name not in self.owners:
            raise ExecutorCallError("org.freedesktop.DBus.Error.NameHasNoOwner", "none")
        return self.owners[name][0]

    def unix_user(self, unique: str) -> int:
        for owner, uid in self.owners.values():
            if owner == unique:
                return uid
        raise ExecutorCallError("org.freedesktop.DBus.Error.NameHasNoOwner", "gone")

    def list_activatable_names(self):
        return self.activatable


class _Registry:
    def __init__(self, seats: dict[str, int]) -> None:
        self.seats = seats

    def seat_ids(self):
        return tuple(sorted(self.seats))

    def seat(self, seat_id: str):
        return SimpleNamespace(seat_id=seat_id, uid=self.seats[seat_id])


def _start(driver: OperationDriver) -> OperationDriver:
    driver.start()
    return driver


class BrokerHealthLegTests(unittest.TestCase):
    def tearDown(self) -> None:
        for driver in getattr(self, "drivers", ()):
            driver.stop(timeout=WAIT)

    def driver(self, factory, **kwargs) -> OperationDriver:
        driver = OperationDriver(context_factory=factory, sleeper=lambda seconds: None, **kwargs)
        self.drivers = [*getattr(self, "drivers", ()), driver]
        return driver

    def test_driver_setup_failure_surfaces_unhealthy_with_bounded_reason(self) -> None:
        def factory():
            raise RuntimeError("SECRET-SENTINEL " + "x" * 500)

        driver = _start(self.driver(factory))
        self.assertTrue(driver.recovered.wait(WAIT))
        result = operation_driver_probe(driver)()
        self.assertEqual(result.status, "unhealthy")
        self.assertEqual(result.reason, "driver setup failed: RuntimeError")
        self.assertNotIn("SECRET", result.reason)
        provider = make_broker_health_provider(
            journal_probe=_ready("j"),
            registry_probe=_ready("r"),
            policy_probe=_ready("p"),
            approval_reachability_probe=approval_reachability_probe(driver),
            executor_reachability_probe=executor_reachability_probe(driver),
            operation_driver_probe=operation_driver_probe(driver),
        )
        payload = serialize_health(provider.snapshot())
        self.assertEqual(payload["Status"], "unhealthy")
        self.assertEqual(payload["Subcomponents"]["operation_driver"], "unhealthy")
        self.assertLessEqual(len(payload["Reasons"]["operation_driver"]), 200)
        # the reachability legs never claim ready from a dead driver
        self.assertNotEqual(payload["Subcomponents"]["executor_reachability"], "ready")
        # an over-long failure text is bounded and printable
        driver.failure = "driver thread died: " + "é" * 400
        long = operation_driver_probe(driver)()
        self.assertLessEqual(len(long.reason), 200)
        self.assertTrue(all(32 <= ord(ch) <= 126 for ch in long.reason))

    def test_dead_driver_thread_surfaces_unhealthy(self) -> None:
        def pending():
            raise RuntimeError("journal gone")

        driver = _start(self.driver(lambda: _context(_Journal(pending))))
        self.assertTrue(driver.recovered.wait(WAIT))
        driver._thread.join(WAIT)  # type: ignore[union-attr]
        result = operation_driver_probe(driver)()
        self.assertEqual((result.status, result.reason), ("unhealthy", "driver thread died: RuntimeError"))
        # a thread that exited WITHOUT a recorded failure (and without stop)
        quiet = self.driver(lambda: _context(_Journal()))
        quiet._thread = threading.Thread(target=lambda: None)
        quiet._thread.start()
        quiet._thread.join(WAIT)
        self.assertEqual(operation_driver_probe(quiet)().status, "unhealthy")
        self.assertEqual(operation_driver_probe(quiet)().reason, "driver thread is not running")
        # an orderly stop is not a death
        stopped = _start(self.driver(lambda: _context(_Journal())))
        self.assertTrue(stopped.recovered.wait(WAIT))
        stopped.stop(timeout=WAIT)
        self.assertEqual(operation_driver_probe(stopped)().status, "ready")

    def test_recovering_driver_is_degraded(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        def pending():
            entered.set()
            release.wait(WAIT)
            return ()

        driver = self.driver(lambda: _context(_Journal(pending)))
        self.assertEqual(operation_driver_probe(driver)().status, "degraded")  # not started
        _start(driver)
        self.assertTrue(entered.wait(WAIT))
        result = operation_driver_probe(driver)()
        self.assertEqual((result.status, result.reason), ("degraded", "recovering pending operations"))
        release.set()
        self.assertTrue(driver.recovered.wait(WAIT))
        self.assertEqual(operation_driver_probe(driver)(), HealthProbeResult("ready", "", HEALTH_REVISION))

    def test_executor_reachability_ready_when_owner_uid_matches_or_activatable(self) -> None:
        registry = _Registry({"alpha": 5001, "bravo": 5002})
        names = {seat: executor_wire.bus_name_for_uid(uid) for seat, uid in registry.seats.items()}
        transport = _Transport({names["alpha"]: (":1.10", 5001)}, activatable=(names["bravo"],))
        reach = BusReachability(registry=registry, transport=transport, approval_uid=7)  # type: ignore[arg-type]
        self.assertEqual(reach.executor_reachability().status, "ready")
        transport.owners[names["bravo"]] = (":1.11", 5002)
        transport.activatable = ()
        self.assertEqual(reach.executor_reachability().status, "ready")

    def test_executor_identity_mismatch_is_unhealthy(self) -> None:
        registry = _Registry({"alpha": 5001, "bravo": 5002})
        names = {seat: executor_wire.bus_name_for_uid(uid) for seat, uid in registry.seats.items()}
        transport = _Transport({names["alpha"]: (":1.10", 5001), names["bravo"]: (":1.11", 6666)})
        result = BusReachability(registry=registry, transport=transport, approval_uid=7).executor_reachability()  # type: ignore[arg-type]
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("bravo", result.reason)
        self.assertNotIn("alpha", result.reason)

    def test_absent_executor_is_degraded_not_unhealthy(self) -> None:
        registry = _Registry({"alpha": 5001, "bravo": 5002})
        names = {seat: executor_wire.bus_name_for_uid(uid) for seat, uid in registry.seats.items()}
        transport = _Transport({names["alpha"]: (":1.10", 5001)})
        result = BusReachability(registry=registry, transport=transport, approval_uid=7).executor_reachability()  # type: ignore[arg-type]
        self.assertEqual(result.status, "degraded")
        self.assertIn("bravo", result.reason)

    def test_approval_reachability_legs(self) -> None:
        registry = _Registry({})
        for owners, expected in (
            ({APPROVAL_BUS_NAME: (":1.20", 7)}, "ready"),
            ({}, "degraded"),
            ({APPROVAL_BUS_NAME: (":1.20", 8)}, "unhealthy"),
        ):
            with self.subTest(expected=expected):
                reach = BusReachability(registry=registry, transport=_Transport(owners), approval_uid=7)  # type: ignore[arg-type]
                self.assertEqual(reach.approval_reachability().status, expected)
        from tools.host_control import approval_client

        self.assertEqual(APPROVAL_BUS_NAME, approval_client.APPROVAL_BUS_NAME)

    def test_reachability_legs_come_from_the_driver_cache(self) -> None:
        # unwired: not_configured, machine-legible
        unwired = _start(self.driver(lambda: _context(_Journal())))
        self.assertTrue(unwired.recovered.wait(WAIT))
        self.assertEqual(executor_reachability_probe(unwired)().status, "not_configured")
        self.assertEqual(approval_reachability_probe(unwired)().status, "not_configured")
        # wired: the source's verdict, refreshed while idle
        source = _Source(
            executor=HealthProbeResult("degraded", "executor absent and not activatable for seats: bravo", "x"),
            approval=HealthProbeResult("unhealthy", "approval service name owned by an unexpected uid", "x"),
        )
        source.target = 3
        # a frozen clock: the cache never ages here (the age bound has its own test)
        wired = _start(self.driver(lambda: _context(_Journal(), source), health_refresh=0.01, clock=lambda: 0.0))
        self.assertTrue(source.nth.wait(WAIT))  # the idle refresh keeps running
        self.assertEqual(executor_reachability_probe(wired)().status, "degraded")
        self.assertEqual(approval_reachability_probe(wired)().status, "unhealthy")

        class Raising(_Source):
            def approval_reachability(self):
                raise ExecutorCallError("org.freedesktop.DBus.Error.Timeout", "SECRET")

        raising = Raising()
        failing = _start(self.driver(lambda: _context(_Journal(), raising)))
        self.assertTrue(failing.recovered.wait(WAIT))
        result = approval_reachability_probe(failing)()
        self.assertEqual((result.status, result.reason), ("degraded", "approval reachability probe failed: ExecutorCallError"))

    def test_stale_reachability_cache_is_degraded_never_the_old_ready(self) -> None:
        now = [100.0]
        source = _Source()
        driver = _start(self.driver(lambda: _context(_Journal(), source), clock=lambda: now[0]))
        self.assertTrue(driver.recovered.wait(WAIT))  # one refresh happened at now=100
        self.assertEqual(executor_reachability_probe(driver)().status, "ready")
        bound = 3 * driver.health_refresh
        now[0] = 100.0 + bound  # exactly at the bound: still fresh
        self.assertEqual(approval_reachability_probe(driver)().status, "ready")
        now[0] = 100.0 + bound + 0.001
        for probe in (executor_reachability_probe(driver), approval_reachability_probe(driver)):
            result = probe()
            self.assertEqual(result.status, "degraded")
            self.assertIn("stale", result.reason)

    def test_health_get_never_blocks_on_the_bus(self) -> None:
        block = threading.Event()
        source = _Source(block=block)
        driver = _start(self.driver(lambda: _context(_Journal(), source)))
        self.assertTrue(source.entered.wait(WAIT))  # the driver thread is inside a bus call
        try:
            provider = make_broker_health_provider(
                journal_probe=_ready("j"),
                registry_probe=_ready("r"),
                policy_probe=_ready("p"),
                approval_reachability_probe=approval_reachability_probe(driver),
                executor_reachability_probe=executor_reachability_probe(driver),
                operation_driver_probe=operation_driver_probe(driver),
            )
            done = threading.Event()
            snapshots = []

            def get() -> None:
                snapshots.append(serialize_health(provider.snapshot()))
                done.set()

            threading.Thread(target=get, daemon=True).start()
            self.assertTrue(done.wait(5.0), "a Health1 snapshot blocked behind the driver's bus call")
            payload = snapshots[0]
            self.assertEqual(payload["Subcomponents"]["executor_reachability"], "degraded")
            self.assertEqual(payload["Reasons"]["executor_reachability"], "executor reachability not yet observed")
            self.assertEqual(payload["Subcomponents"]["operation_driver"], "degraded")  # still recovering
            self.assertEqual(source.calls, 1)  # only the driver thread ever called the source
        finally:
            block.set()

    def test_roster_appends_operation_driver_and_keeps_the_pre_5d_prefix(self) -> None:
        # consumers (e.g. the B7 recovery seat, T-4884) key by leg name:
        # legs are only appended, never renamed or reordered
        self.assertEqual(
            BROKER_ROSTER[:5],
            ("journal", "registry", "policy", "approval_reachability", "executor_reachability"),
        )
        self.assertEqual(BROKER_ROSTER[5:], ("operation_driver",))
        provider = make_broker_health_provider(journal_probe=_ready("j"), registry_probe=_ready("r"), policy_probe=_ready("p"))
        payload = serialize_health(provider.snapshot())
        self.assertEqual(payload["Subcomponents"]["operation_driver"], "not_configured")
        self.assertIn("T-2917", payload["Reasons"]["operation_driver"])


# ---------------------------------------------------------------------------
# bus tier: Health1 over a private bus flips when the driver dies
# ---------------------------------------------------------------------------

try:  # pragma: no cover - environment probe
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    from gi.repository import GLib

    from tools import _host_control_testbus as testbus

    DBUS = testbus.DBUS_DAEMON is not None
except ImportError:  # pragma: no cover
    DBUS = False

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


@unittest.skipUnless(DBUS, "dbus-python, PyGObject and dbus-daemon are required")
class Health1DriverBusTests(unittest.TestCase):
    def test_health1_flips_when_the_driver_dies(self) -> None:
        from tools.host_control import health_dbus
        from tools.host_control.health import HEALTH_INTERFACE

        handle = testbus.spawn_daemon("t2917h-", _BUS_CONFIG)
        self.addCleanup(testbus.stop_daemon, handle)
        dbus.mainloop.glib.threads_init()
        # dbus-python's GLib integration dispatches on the DEFAULT context;
        # this loop runs it in one helper thread for this cell only
        loop = GLib.MainLoop()
        release = threading.Event()
        entered = threading.Event()

        def pending():
            entered.set()
            release.wait(WAIT)
            raise RuntimeError("journal lost")

        driver = OperationDriver(context_factory=lambda: _context(_Journal(pending)), sleeper=lambda seconds: None)
        provider = make_broker_health_provider(
            journal_probe=_ready("j"),
            registry_probe=_ready("r"),
            policy_probe=_ready("p"),
            approval_reachability_probe=approval_reachability_probe(driver),
            executor_reachability_probe=executor_reachability_probe(driver),
            operation_driver_probe=operation_driver_probe(driver),
        )
        ready = threading.Event()
        state: dict[str, object] = {}

        connection = dbus.bus.BusConnection(handle.bus_address, mainloop=dbus.mainloop.glib.DBusGMainLoop())
        state["object"] = health_dbus.Health1Object(
            connection,
            object_path=health_dbus.broker_health_path(),
            provider=provider,
            monitor_uids=frozenset({os.getuid()}),
        )
        state["name"] = connection.get_unique_name()

        def serve() -> None:
            ready.set()
            loop.run()

        server = threading.Thread(target=serve, name="health1-bus", daemon=True)
        server.start()
        self.assertTrue(ready.wait(WAIT))
        self.addCleanup(server.join, WAIT)
        self.addCleanup(lambda: GLib.idle_add(loop.quit))
        client = dbus.bus.BusConnection(handle.bus_address, mainloop=dbus.mainloop.NULL_MAIN_LOOP)

        def get(name: str):
            return client.call_blocking(
                state["name"], health_dbus.broker_health_path(), health_dbus.PROPERTIES_INTERFACE,
                "Get", "ss", (HEALTH_INTERFACE, name), timeout=10.0,
            )

        driver.start()
        self.addCleanup(driver.stop, WAIT)
        self.assertTrue(entered.wait(WAIT))
        self.assertEqual(str(get("Status")), "degraded")
        self.assertEqual(str(get("Subcomponents")["operation_driver"]), "degraded")
        release.set()  # the driver thread dies now
        self.assertTrue(driver.recovered.wait(WAIT))
        driver._thread.join(WAIT)  # type: ignore[union-attr]
        self.assertEqual(str(get("Status")), "unhealthy")
        self.assertEqual(str(get("Subcomponents")["operation_driver"]), "unhealthy")
        self.assertEqual(str(get("Reasons")["operation_driver"]), "driver thread died: RuntimeError")


if __name__ == "__main__":  # must stay LAST
    unittest.main()
