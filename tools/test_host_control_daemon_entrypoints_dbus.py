"""T-2920 U2 tier B: the REAL broker and approval daemon entrypoints booted
over a PRIVATE dbus-daemon (never the system bus), each in its own subprocess.

Riskiest assumption (the U2 plan): the broker boots with ZERO executors
reachable. Seats are registered (an empty registry is refused by design,
registry.py), but no executor owns or can activate any seat's name and no
approval service is up. The broker must own its name, answer Capabilities and
report Health1 DEGRADED, not crash. A second case brings the approval
entrypoint up beside it: approval reachability turns ready while the executors
keep the broker degraded.

The approval case proves the T-5518 re-arm at start end to end: a journal
seeded with more pending human requests than one resume page, and a recording
stand-in for the Telegram ingress on the bus, which must see a Present for
EVERY pending request.

Fixture boundaries: one uid plays every role, permissive private bus (gate 10:
every refusal is in code), test-mode trusted loader. See
tools/test_host_control_daemon_entrypoints_driver.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import _host_control_testbus as testbus
from tools import test_host_control_daemon_entrypoints_driver as fx
from tools.host_control import wire as broker_wire
from tools.host_control.approval import wire as approval_wire
from tools.host_control.health import HEALTH_INTERFACE
from tools.host_control.health_dbus import approval_health_path, broker_health_path

REPO_ROOT = Path(__file__).resolve().parent.parent
UID = os.getuid()
PROPERTIES = "org.freedesktop.DBus.Properties"

try:
    import dbus  # noqa: F401
    import dbus.bus

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


class _BusCase(unittest.TestCase):
    PREFIX = "t2920u2-"

    def setUp(self) -> None:
        self.bus = testbus.spawn_daemon(self.PREFIX, _BUS_CONFIG)
        self.addCleanup(testbus.stop_daemon, self.bus)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.processes: list[tuple[subprocess.Popen, Path]] = []
        self.addCleanup(self._stop_all)
        self.connection = dbus.bus.BusConnection(self.bus.bus_address)
        self.addCleanup(self.connection.close)

    def _stop_all(self) -> None:
        for process, _log in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    def spawn(self, role: str, config: dict) -> subprocess.Popen:
        config = dict(config, bus_address=self.bus.bus_address)
        config_path = self.root / f"{role}-{len(self.processes)}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        log_path = Path(self.bus.bus_dir) / f"{role}-{len(self.processes)}.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "tools.test_host_control_daemon_entrypoints_driver", role, str(config_path)],
                cwd=REPO_ROOT,
                stdout=log,
                stderr=log,
                preexec_fn=testbus.pdeathsig_preexec_fn(),
            )
        self.bus.register_driver(process.pid)
        self.processes.append((process, log_path))
        return process

    def logs(self) -> str:
        return " | ".join(path.read_text(encoding="utf-8")[-2000:] for _p, path in self.processes)

    def wait_owned(self, name: str, process: subprocess.Popen, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.connection.name_has_owner(name):
            if process.poll() is not None:
                self.fail(f"exited ({process.returncode}) before owning {name}; logs: {self.logs()}")
            if time.monotonic() > deadline:
                self.fail(f"{name} not owned within {timeout:.0f}s; logs: {self.logs()}")
            time.sleep(0.05)

    def health(self, bus_name: str, path: str) -> dict:
        return self.connection.call_blocking(bus_name, path, PROPERTIES, "GetAll", "s", (HEALTH_INTERFACE,))

    def wait_for(self, predicate, what: str, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"{what} not reached within {timeout:.0f}s; logs: {self.logs()}")
            time.sleep(0.1)


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class BrokerZeroExecutorBootTests(_BusCase):
    def _broker(self, *, approval_uid: int) -> tuple[subprocess.Popen, dict]:
        tree = fx.broker_tree(self.root / "broker", subject_uid=UID, approval_uid=approval_uid, monitor_uids=[UID])
        process = self.spawn("broker", {"config_path": tree["config_path"]})
        self.wait_owned(broker_wire.BUS_NAME, process)
        return process, tree

    def _driver_recovered(self):
        snapshot = self.health(broker_wire.BUS_NAME, broker_health_path())
        return snapshot if str(snapshot["Subcomponents"]["operation_driver"]) == "ready" else None

    def test_broker_boots_degraded_with_zero_executors_and_no_approval(self) -> None:
        process, tree = self._broker(approval_uid=UID + 54321)
        snapshot = self.wait_for(self._driver_recovered, "operation driver recovery")
        components = {str(k): str(v) for k, v in snapshot["Subcomponents"].items()}
        reasons = {str(k): str(v) for k, v in snapshot["Reasons"].items()}
        self.assertEqual(str(snapshot["Status"]), "degraded")
        self.assertEqual(components["executor_reachability"], "degraded")
        self.assertIn("executor absent and not activatable", reasons["executor_reachability"])
        for seat in ("alpha", "bravo", "recovery"):
            self.assertIn(seat, reasons["executor_reachability"])
        self.assertEqual(components["approval_reachability"], "degraded")
        self.assertEqual(reasons["approval_reachability"], "approval service has no owner")
        self.assertEqual(
            (components["journal"], components["registry"], components["policy"]), ("ready", "ready", "ready")
        )
        # the public surface answers, honestly degraded
        version, payload = self.connection.call_blocking(
            broker_wire.BUS_NAME, broker_wire.OBJECT_PATH, broker_wire.MANAGER_INTERFACE, "Capabilities", "", ()
        )
        self.assertEqual(str(version), "org.pantheon.HostBroker1/v1")
        self.assertEqual(json.loads(str(payload))["health"], "degraded")
        self.assertIsNone(process.poll())
        # the startup record is on the log for the gate-32a comparison
        from tools.host_control.runtime_table import load_checkout_op_registry

        lines = [line for line in self.logs().split("\n") if "host-broker: startup-record " in line]
        self.assertEqual(len(lines), 1, self.logs())
        record = json.loads(lines[0].split("startup-record ", 1)[1])
        self.assertEqual(record["op_registry_digest"], load_checkout_op_registry(Path(tree["framework"])).digest)
        # SIGTERM is a clean stop
        process.terminate()
        self.assertEqual(process.wait(timeout=30), 0, self.logs())

    def test_a_second_broker_cannot_claim_the_name(self) -> None:
        self._broker(approval_uid=UID + 54321)
        second = self.spawn("broker", {"config_path": str(self.root / "broker" / "etc" / "config.json")})
        self.assertEqual(second.wait(timeout=90), 69, self.logs())

    def test_approval_reachability_turns_ready_while_executors_keep_it_degraded(self) -> None:
        approval = fx.approval_tree(self.root / "approval", caller_uid=UID)
        approval_process = self.spawn("approval", {"config_path": approval["config_path"]})
        self.wait_owned(approval_wire.BUS_NAME, approval_process)
        self._broker(approval_uid=UID)

        def approval_ready():
            snapshot = self._driver_recovered()
            if snapshot and str(snapshot["Subcomponents"]["approval_reachability"]) == "ready":
                return snapshot
            return None

        snapshot = self.wait_for(approval_ready, "approval reachability ready")
        self.assertEqual(str(snapshot["Status"]), "degraded")
        self.assertEqual(str(snapshot["Subcomponents"]["executor_reachability"]), "degraded")


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class ApprovalBootTests(_BusCase):
    PENDING = 5
    PAGE = 2

    def test_every_pending_human_request_is_presented_after_a_restart(self) -> None:
        tree = fx.approval_tree(self.root / "approval", caller_uid=UID)
        reviews = fx.seed_pending_human_requests(tree, self.PENDING)
        present_log = self.root / "presents.jsonl"
        ingress = self.spawn("ingress", {"present_log": str(present_log)})
        self.wait_owned(approval_wire.TELEGRAM_INGRESS_BUS_NAME, ingress)
        approval = self.spawn("approval", {"config_path": tree["config_path"], "resume_page": self.PAGE})
        self.wait_owned(approval_wire.BUS_NAME, approval)

        def presented():
            if not present_log.exists():
                return None
            seen = {
                json.loads(line)["review_handle"]
                for line in present_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            return seen if set(reviews) <= seen else None

        seen = self.wait_for(presented, "a Present for every pending request")
        self.assertEqual(seen, set(reviews))
        self.assertIn(f"resumed {self.PENDING} pending human requests", self.logs())
        snapshot = self.health(approval_wire.BUS_NAME, approval_health_path())
        self.assertEqual(str(snapshot["Subcomponents"]["broker_metadata_reachability"]), "not_configured")
        self.assertEqual(str(snapshot["Subcomponents"]["approver_policy"]), "ready")
        approval.terminate()
        self.assertEqual(approval.wait(timeout=30), 0, self.logs())

    def test_a_second_approval_service_cannot_claim_the_name(self) -> None:
        tree = fx.approval_tree(self.root / "approval", caller_uid=UID)
        first = self.spawn("approval", {"config_path": tree["config_path"]})
        self.wait_owned(approval_wire.BUS_NAME, first)
        second = self.spawn("approval", {"config_path": tree["config_path"]})
        self.assertEqual(second.wait(timeout=90), 69, self.logs())


if __name__ == "__main__":
    unittest.main()
