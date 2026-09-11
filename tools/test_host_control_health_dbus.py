"""Private-bus tests for ``org.pantheon.Health1`` (T-2914)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import tools._host_control_testbus as testbus
from tools.host_control import health_dbus
from tools.host_control import wire as broker_wire
from tools.host_control.approval import wire as approval_wire
from tools.host_control.health import (
    HEALTH_INTERFACE,
    HEALTH_PROPERTIES,
)
from tools.test_host_control_health_dbus_driver import _default_probe_state

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "tools" / "host_control" / "dist"
DBUS_DAEMON = shutil.which("dbus-daemon")

requires_private_bus = unittest.skipUnless(
    health_dbus.DBUS_AVAILABLE and DBUS_DAEMON,
    "private-bus integration needs dbus-python and dbus-daemon",
)

_PERMISSIVE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow own="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""

_DENY_SIGNAL_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow own="*"/>
    <allow receive_sender="*"/>
    <deny receive_sender="org.pantheon.HostBroker1"
          receive_path="/org/pantheon/HostBroker1/Health"
          receive_interface="org.freedesktop.DBus.Properties"
          receive_member="PropertiesChanged"
          receive_type="signal"/>
  </policy>
</busconfig>
"""


class HealthBusHarness:
    def __init__(
        self,
        case: unittest.TestCase,
        *,
        monitor_is_caller: bool = True,
        bus_config: str = _PERMISSIVE_BUS_CONFIG,
    ) -> None:
        self.case = case
        self.monitor_is_caller = monitor_is_caller
        self.bus_config_template = bus_config
        self.bus_dir = ""
        self.work: tempfile.TemporaryDirectory | None = None
        self._bus_handle = None
        self.daemon = None
        self.driver = None
        self.daemon_log = None
        self.driver_log = None

    def __enter__(self) -> "HealthBusHarness":
        import dbus
        import dbus.bus
        import dbus.mainloop.glib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus_handle = testbus.spawn_daemon("t2914-", self.bus_config_template)
        self.bus_dir = self._bus_handle.bus_dir
        self.daemon_log = self._bus_handle.log
        self.daemon = self._bus_handle.process
        self.bus_address = self._bus_handle.bus_address
        self.work = tempfile.TemporaryDirectory(dir="/tmp")
        self.connection = dbus.bus.BusConnection(self.bus_address)
        self.root = Path(self.work.name) / "domain"
        self.probe_path = Path(self.work.name) / "probes.json"
        self.emit_path = Path(self.work.name) / "emit.txt"
        self.touch_path = Path(self.work.name) / "touch.log"
        self.touch_gate_path = Path(self.work.name) / "touch.enabled"
        self.probe_path.write_text(
            json.dumps(_default_probe_state()),
            encoding="utf-8",
        )
        self._spawn_driver()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop_driver()
        if self.driver_log is not None:
            self.driver_log.close()
        if self.work is not None:
            self.work.cleanup()
        if self._bus_handle is not None:
            testbus.stop_daemon(self._bus_handle)

    def _spawn_driver(self) -> None:
        config = {
            "bus_address": self.bus_address,
            "root": str(self.root),
            "probe_path": str(self.probe_path),
            "emit_path": str(self.emit_path),
            "touch_path": str(self.touch_path),
            "touch_gate_path": str(self.touch_gate_path),
            "subject_uid": os.getuid(),
            "monitor_uids": (
                [os.getuid()]
                if self.monitor_is_caller
                else [os.getuid() + 50000]
            ),
            "broker_uid": os.getuid() + 41000,
            "approval_uid": os.getuid() + 42000,
            "ingress_uid": os.getuid() + 43000,
        }
        driver_config = os.path.join(self.bus_dir, "driver.json")
        Path(driver_config).write_text(json.dumps(config), encoding="utf-8")
        self.driver_log = open(
            os.path.join(self.bus_dir, f"driver-{time.monotonic_ns()}.log"),
            "w",
            encoding="utf-8",
        )
        self.driver = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tools.test_host_control_health_dbus_driver",
                driver_config,
            ],
            cwd=REPO_ROOT,
            stdout=self.driver_log,
            stderr=self.driver_log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        if self._bus_handle is not None:
            self._bus_handle.register_driver(self.driver.pid)
        deadline = time.monotonic() + 30
        while not self.connection.name_has_owner(broker_wire.BUS_NAME):
            if time.monotonic() > deadline:
                raise AssertionError(self.driver_log_tail())
            if self.driver.poll() is not None:
                raise AssertionError(self.driver_log_tail())
            time.sleep(0.05)
        while not self.connection.name_has_owner(approval_wire.BUS_NAME):
            if time.monotonic() > deadline:
                raise AssertionError(self.driver_log_tail())
            if self.driver.poll() is not None:
                raise AssertionError(self.driver_log_tail())
            time.sleep(0.05)
        self.state_path = self.root / "state.json"
        while True:
            if time.monotonic() > deadline:
                raise AssertionError("driver never wrote state")
            if self.state_path.exists():
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if self.state.get("baseline", {}).get("baseline_ready") is True:
                    break
            time.sleep(0.05)

    def stop_driver(self) -> None:
        if self.driver is not None and self.driver.poll() is None:
            self.driver.terminate()
            try:
                self.driver.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.driver.kill()
                self.driver.wait(timeout=10)

    def restart_driver(self) -> None:
        self.stop_driver()
        if self.driver_log is not None:
            self.driver_log.close()
            self.driver_log = None
        self._spawn_driver()

    def driver_log_tail(self) -> str:
        if self.driver_log is None:
            return ""
        return Path(self.driver_log.name).read_text(encoding="utf-8")[-2000:]

    def probe_state(self) -> dict:
        return json.loads(self.probe_path.read_text(encoding="utf-8"))

    def initial_baseline(self) -> dict:
        return dict(self.state["baseline"])

    def probe_positive_controls(self) -> dict:
        return dict(self.state["probe_positive_controls"])

    def enable_probe_touch_log(self) -> None:
        self.touch_gate_path.write_text("enabled", encoding="utf-8")

    def write_probe_state(self, data: dict) -> None:
        self.probe_path.write_text(json.dumps(data), encoding="utf-8")

    def get_all(self, bus_name: str, path: str) -> dict:
        return self.connection.call_blocking(
            bus_name,
            path,
            health_dbus.PROPERTIES_INTERFACE,
            "GetAll",
            "s",
            (HEALTH_INTERFACE,),
        )

    def get(self, bus_name: str, path: str, prop: str):
        return self.connection.call_blocking(
            bus_name,
            path,
            health_dbus.PROPERTIES_INTERFACE,
            "Get",
            "ss",
            (HEALTH_INTERFACE, prop),
        )

    def call_set(self, bus_name: str, path: str) -> None:
        self.connection.call_blocking(
            bus_name,
            path,
            health_dbus.PROPERTIES_INTERFACE,
            "Set",
            "ssv",
            (HEALTH_INTERFACE, "Status", "ready"),
        )

    def introspect(self, bus_name: str, path: str) -> str:
        return self.connection.call_blocking(
            bus_name,
            path,
            "org.freedesktop.DBus.Introspectable",
            "Introspect",
            "",
            (),
        )

    def capabilities_health(self) -> str:
        _version, payload = self.connection.call_blocking(
            broker_wire.BUS_NAME,
            broker_wire.OBJECT_PATH,
            broker_wire.MANAGER_INTERFACE,
            "Capabilities",
            "",
            (),
        )
        return json.loads(str(payload))["health"]

    def collect_signal(self, *, denied: bool = False) -> list:
        from gi.repository import GLib

        received: list = []

        def receiver(interface_name, changed, invalidated):
            received.append((interface_name, dict(changed), list(invalidated)))

        self.connection.add_signal_receiver(
            receiver,
            signal_name=health_dbus.PROPERTIES_CHANGED_SIGNAL,
            dbus_interface=health_dbus.PROPERTIES_INTERFACE,
            bus_name=broker_wire.BUS_NAME,
            path=health_dbus.broker_health_path(),
        )
        self.emit_path.write_text(
            f"broker:{time.monotonic_ns()}",
            encoding="utf-8",
        )
        loop = GLib.MainLoop()
        GLib.timeout_add(800 if denied else 3000, loop.quit)
        deadline = time.monotonic() + (0.8 if denied else 3.0)
        while time.monotonic() < deadline and (denied or not received):
            loop.get_context().iteration(True)
            if received and not denied:
                break
        return received


@requires_private_bus
class HealthIntrospectionBusTests(unittest.TestCase):
    def test_children_introspect_properties_and_parent_interfaces_unchanged(self) -> None:
        with HealthBusHarness(self) as harness:
            broker_parent = harness.introspect(
                broker_wire.BUS_NAME, broker_wire.OBJECT_PATH
            )
            approval_parent = harness.introspect(
                approval_wire.BUS_NAME, approval_wire.OBJECT_PATH
            )
            self.assertNotIn(HEALTH_INTERFACE, broker_parent)
            self.assertNotIn(HEALTH_INTERFACE, approval_parent)
            self.assertIn('<node name="Health"/>', broker_parent)
            self.assertIn('<node name="Health"/>', approval_parent)
            child = harness.introspect(
                broker_wire.BUS_NAME,
                health_dbus.broker_health_path(),
            )
            base = harness.state["broker_base_introspection"]
            wrapped = harness.state["broker_wrapped_introspection"]
            self.assertEqual(child, wrapped)
            self.assertEqual(wrapped, health_dbus.add_health_introspection(base))
            for name in HEALTH_PROPERTIES:
                self.assertIn(f'<property name="{name}"', child)


@requires_private_bus
class HealthPullBusTests(unittest.TestCase):
    def test_get_and_getall_return_same_current_snapshot(self) -> None:
        with HealthBusHarness(self) as harness:
            path = health_dbus.broker_health_path()
            self.assertEqual(harness.get(broker_wire.BUS_NAME, path, "Status"), "degraded")
            payload = harness.get_all(broker_wire.BUS_NAME, path)
            self.assertEqual(payload["Status"], "degraded")
            state = harness.probe_state()
            state["broker"]["approval_reachability"] = {
                "status": "ready",
                "reason": "",
                "revision": "approval-r2",
            }
            state["broker"]["executor_reachability"] = {
                "status": "ready",
                "reason": "",
                "revision": "executor-r2",
            }
            harness.write_probe_state(state)
            self.assertEqual(harness.get(broker_wire.BUS_NAME, path, "Status"), "ready")
            self.assertEqual(harness.get_all(broker_wire.BUS_NAME, path)["Status"], "ready")

    def test_set_is_absent_and_cannot_mutate_health(self) -> None:
        with HealthBusHarness(self) as harness:
            path = health_dbus.broker_health_path()
            before = harness.get_all(broker_wire.BUS_NAME, path)
            import dbus

            with self.assertRaises(dbus.exceptions.DBusException):
                harness.call_set(broker_wire.BUS_NAME, path)
            self.assertEqual(harness.get_all(broker_wire.BUS_NAME, path), before)


@requires_private_bus
class MonitorGateBusTests(unittest.TestCase):
    def test_only_monitor_uid_can_read_and_subscribe_on_both_services(self) -> None:
        with HealthBusHarness(self, monitor_is_caller=True) as monitor:
            self.assertIn(
                "Status",
                monitor.get_all(broker_wire.BUS_NAME, health_dbus.broker_health_path()),
            )
            self.assertIn(
                "Status",
                monitor.get_all(
                    approval_wire.BUS_NAME,
                    health_dbus.approval_health_path(),
                ),
            )
            self.assertTrue(monitor.collect_signal())
        with HealthBusHarness(self, monitor_is_caller=False) as denied:
            import dbus

            denied.enable_probe_touch_log()
            with self.assertRaises(dbus.exceptions.DBusException) as caught:
                denied.get(
                    broker_wire.BUS_NAME,
                    health_dbus.broker_health_path(),
                    "Status",
                )
            self.assertEqual(caught.exception.get_dbus_name(), health_dbus.ACCESS_DENIED_ERROR)
            self.assertFalse(denied.touch_path.exists())


@requires_private_bus
class HealthErrorSurfaceBusTests(unittest.TestCase):
    def test_invalid_uid_get_uses_only_standard_access_denied(self) -> None:
        with HealthBusHarness(self, monitor_is_caller=False) as harness:
            import dbus

            for bus_name, path in (
                (broker_wire.BUS_NAME, health_dbus.broker_health_path()),
                (approval_wire.BUS_NAME, health_dbus.approval_health_path()),
            ):
                with self.subTest(bus_name=bus_name):
                    with self.assertRaises(dbus.exceptions.DBusException) as caught:
                        harness.get(bus_name, path, "Status")
                    self.assertEqual(
                        caught.exception.get_dbus_name(),
                        health_dbus.ACCESS_DENIED_ERROR,
                    )
                    self.assertEqual(
                        caught.exception.get_dbus_message(),
                        health_dbus.ACCESS_DENIED_BODY,
                    )


@requires_private_bus
class DisclosureBoundaryBusTests(unittest.TestCase):
    def test_excluded_sentinels_present_then_absent_from_pull_and_push(self) -> None:
        with HealthBusHarness(self) as harness:
            controls = harness.probe_positive_controls()
            self.assertEqual(set(controls), {"broker.registry", "broker.journal", "approval.journal"})
            self.assertEqual(
                controls["broker.registry"],
                {
                    "label": "plan",
                    "surface": "broker plans.idempotency_key",
                    "content": harness.state["sentinels"]["plan"],
                },
            )
            self.assertEqual(
                {
                    "label": controls["broker.journal"]["label"],
                    "surface": controls["broker.journal"]["surface"],
                },
                {
                    "label": "journal",
                    "surface": "broker operation_events.payload_json",
                },
            )
            self.assertIn(
                harness.state["sentinels"]["journal"],
                controls["broker.journal"]["content"],
            )
            self.assertEqual(
                controls["approval.journal"],
                {
                    "label": "grant",
                    "surface": "approval grants.audit_anchor",
                    "content": harness.state["sentinels"]["grant"],
                },
            )
            pull = json.dumps(
                dict(harness.get_all(broker_wire.BUS_NAME, health_dbus.broker_health_path())),
                sort_keys=True,
                default=str,
            )
            signal = json.dumps(harness.collect_signal(), sort_keys=True, default=str)
            for sentinel in harness.state["sentinels"].values():
                self.assertNotIn(sentinel, pull)
                self.assertNotIn(sentinel, signal)


@requires_private_bus
class PropertiesChangedBusTests(unittest.TestCase):
    def test_properties_changed_emits_serialized_changed_fields(self) -> None:
        with HealthBusHarness(self) as harness:
            baseline = harness.initial_baseline()
            self.assertIs(baseline["baseline_ready"], True)
            self.assertEqual(baseline["initial_status"]["broker"], "degraded")
            self.assertEqual(baseline["initial_status"]["approval"], "degraded")
            self.assertEqual(
                baseline["initial_components"]["broker"]["journal"],
                "ready",
            )
            self.assertEqual(
                baseline["initial_components"]["approval"]["journal"],
                "ready",
            )
            state = harness.probe_state()
            state["broker"]["journal"] = {
                "status": "unhealthy",
                "reason": "journal failed",
                "revision": "journal-r2",
            }
            harness.write_probe_state(state)
            received = harness.collect_signal()
            self.assertEqual(len(received), 1)
            interface_name, changed, invalidated = received[0]
            self.assertEqual(interface_name, HEALTH_INTERFACE)
            self.assertEqual(invalidated, [])
            self.assertEqual(changed["Status"], "unhealthy")
            self.assertEqual(changed["Subcomponents"]["journal"], "unhealthy")
            self.assertNotIn("ContractRevision", changed)


@requires_private_bus
class TwoSurfaceAgreementBusTests(unittest.TestCase):
    def test_health1_and_capabilities_flip_and_recover_together(self) -> None:
        with HealthBusHarness(self) as harness:
            path = health_dbus.broker_health_path()
            state = harness.probe_state()
            state["broker"]["approval_reachability"] = {
                "status": "ready",
                "reason": "",
                "revision": "approval-r2",
            }
            state["broker"]["executor_reachability"] = {
                "status": "ready",
                "reason": "",
                "revision": "executor-r2",
            }
            harness.write_probe_state(state)
            self.assertEqual(harness.get(broker_wire.BUS_NAME, path, "Status"), "ready")
            self.assertEqual(harness.capabilities_health(), "ready")
            state["broker"]["journal"] = {
                "status": "unhealthy",
                "reason": "journal failed",
                "revision": "journal-r2",
            }
            harness.write_probe_state(state)
            self.assertEqual(harness.get(broker_wire.BUS_NAME, path, "Status"), "unhealthy")
            self.assertEqual(harness.capabilities_health(), "degraded")
            state["broker"]["journal"] = {
                "status": "ready",
                "reason": "",
                "revision": "journal-r3",
            }
            harness.write_probe_state(state)
            self.assertEqual(harness.get(broker_wire.BUS_NAME, path, "Status"), "ready")
            self.assertEqual(harness.capabilities_health(), "ready")


@requires_private_bus
class RestartFixableBusTests(unittest.TestCase):
    def test_restart_reget_reads_current_truth_for_both_services(self) -> None:
        with HealthBusHarness(self) as harness:
            harness.stop_driver()
            state = harness.probe_state()
            state["broker"]["journal"] = {
                "status": "unhealthy",
                "reason": "journal failed",
                "revision": "journal-r2",
            }
            state["approval"]["broker_metadata_reachability"] = {
                "status": "unhealthy",
                "reason": "broker metadata failed",
                "revision": "broker-r2",
            }
            harness.write_probe_state(state)
            harness.restart_driver()
            self.assertEqual(
                harness.get(
                    broker_wire.BUS_NAME,
                    health_dbus.broker_health_path(),
                    "Status",
                ),
                "unhealthy",
            )
            self.assertEqual(
                harness.get(
                    approval_wire.BUS_NAME,
                    health_dbus.approval_health_path(),
                    "Status",
                ),
                "unhealthy",
            )


@requires_private_bus
class ReceivePolicyMechanismBusTests(unittest.TestCase):
    def test_allow_delivers_and_deny_suppresses_signal_while_get_works(self) -> None:
        with HealthBusHarness(self, bus_config=_PERMISSIVE_BUS_CONFIG) as allowed:
            self.assertTrue(allowed.collect_signal())
        with HealthBusHarness(self, bus_config=_DENY_SIGNAL_BUS_CONFIG) as denied:
            self.assertEqual(
                denied.get(broker_wire.BUS_NAME, health_dbus.broker_health_path(), "Status"),
                "degraded",
            )
            self.assertEqual(denied.collect_signal(denied=True), [])


class MonitorPolicyArtifactTests(unittest.TestCase):
    def test_monitor_policy_is_exact_member_path_scoped_and_removable(self) -> None:
        for filename, service, path in (
            (
                "org.pantheon.HostBroker1.conf",
                "org.pantheon.HostBroker1",
                "/org/pantheon/HostBroker1/Health",
            ),
            (
                "org.pantheon.HostApproval1.conf",
                "org.pantheon.HostApproval1",
                "/org/pantheon/HostApproval1/Health",
            ),
        ):
            with self.subTest(filename=filename):
                raw = (DIST / filename).read_text(encoding="utf-8")
                self.assertIn("BEGIN T-2914 REMOVABLE MONITOR HEALTH SECTION", raw)
                self.assertIn("END T-2914 REMOVABLE MONITOR HEALTH SECTION", raw)
                self.assertIn("STAGED ARTIFACT", raw)
                self.assertIn("PROVISIONAL", raw)
                self.assertNotIn('="*"', raw)
                self.assertNotIn("eavesdrop", raw)
                tree = ElementTree.parse(DIST / filename)
                policies = {
                    node.get("user") or node.get("context"): node
                    for node in tree.getroot().findall("policy")
                }
                default = policies["default"]
                self.assertIn(
                    {
                        "receive_sender": service,
                        "receive_type": "signal",
                    },
                    [deny.attrib for deny in default.findall("deny")],
                )
                monitor = policies["pantheon-hostmonitor"]
                self.assertEqual(
                    [allow.attrib for allow in monitor.findall("allow")],
                    [
                        {
                            "send_destination": service,
                            "send_path": path,
                            "send_interface": health_dbus.PROPERTIES_INTERFACE,
                            "send_member": "Get",
                        },
                        {
                            "send_destination": service,
                            "send_path": path,
                            "send_interface": health_dbus.PROPERTIES_INTERFACE,
                            "send_member": "GetAll",
                        },
                        {
                            "receive_sender": service,
                            "receive_path": path,
                            "receive_interface": health_dbus.PROPERTIES_INTERFACE,
                            "receive_member": "PropertiesChanged",
                            "receive_type": "signal",
                        },
                    ],
                )
