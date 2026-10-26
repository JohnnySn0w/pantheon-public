"""T-2917 5b end to end over a PRIVATE bus: the real broker service (with
the OperationDriver wired) and the real per-seat executor service, each in
its own subprocess, Verify and Apply through Manager -> driver ->
HostExecutor1 -> ExecutionSource.OperationSpec -> seat root -> typed
records -> broker receipt.

Fixture evidence (labelled, never relabelled as live): one uid plays every
role (real cross-uid is T-2924), approval binding is faked, the user
manager is a fake (5c), the plan is narrowed to the classes this revision
executes. See tools/test_host_control_executor_dbus_driver.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import _host_control_testbus as testbus
from tools import test_host_control_executor_dbus_driver as host
from tools.host_control import wire
from tools.host_control.contracts import VERIFY_APPROVAL_SENTINEL, VERIFY_AUTHORITY_SENTINEL
from tools.host_control.executor import wire as executor_wire

REPO_ROOT = Path(__file__).resolve().parent.parent

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


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class BrokerExecutorEndToEndTests(unittest.TestCase):
    """The permissive bus config is deliberate (gate 10): every refusal
    here is the services' own in-code check, never bus policy."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._bus = testbus.spawn_daemon("t2917b-", _BUS_CONFIG)
        cls.work = tempfile.TemporaryDirectory()
        cls.tree = host.prepare_tree(Path(cls.work.name) / "e2e")
        config = dict(cls.tree, bus_address=cls._bus.bus_address)
        config_path = Path(cls._bus.bus_dir) / "e2e.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        cls.processes = []
        cls.logs = []
        for role in ("executor", "broker"):
            log = open(Path(cls._bus.bus_dir) / f"{role}.log", "w", encoding="utf-8")  # noqa: SIM115
            cls.logs.append(log)
            process = subprocess.Popen(
                [sys.executable, "-m", "tools.test_host_control_executor_dbus_driver", role, str(config_path)],
                cwd=REPO_ROOT,
                stdout=log,
                stderr=log,
                preexec_fn=testbus.pdeathsig_preexec_fn(),
            )
            cls._bus.register_driver(process.pid)
            cls.processes.append(process)
        cls.connection = dbus.bus.BusConnection(cls._bus.bus_address)
        cls.executor_name = executor_wire.bus_name_for_uid(os.getuid())
        for name in (cls.executor_name, wire.BUS_NAME):
            deadline = time.monotonic() + 90
            while not cls.connection.name_has_owner(name):
                if any(p.poll() is not None for p in cls.processes):
                    raise AssertionError(f"a service exited before owning {name}; logs: {cls._logs()}")
                if time.monotonic() > deadline:
                    raise AssertionError(f"{name} not owned within 90s; logs: {cls._logs()}")
                time.sleep(0.05)

    @classmethod
    def _logs(cls) -> str:
        outcomes = Path(cls.tree["root"]) / "driver-outcomes.jsonl"
        driver = outcomes.read_text(encoding="utf-8")[-1500:] if outcomes.exists() else ""
        return " | ".join(
            [driver] + [Path(log.name).read_text(encoding="utf-8")[-1500:] for log in cls.logs]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        for process in getattr(cls, "processes", []):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in getattr(cls, "logs", []):
            log.close()
        if getattr(cls, "work", None) is not None:
            cls.work.cleanup()
        if getattr(cls, "_bus", None) is not None:
            testbus.stop_daemon(cls._bus)
        super().tearDownClass()

    def manager(self, method: str, signature: str, args: tuple):
        return self.connection.call_blocking(
            wire.BUS_NAME, wire.OBJECT_PATH, wire.MANAGER_INTERFACE, method, signature, args
        )

    def wait_terminal(self, handle: str, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            state, payload = self.manager("OperationGet", "s", (handle,))
            if state in ("completed", "failed", "refused", "manual_recovery"):
                return json.loads(payload)
            if time.monotonic() > deadline:
                self.fail(f"operation still {state} after {timeout:.0f}s; logs: {self._logs()}")
            time.sleep(0.05)

    def _operation_events(self, operation_handle: str) -> list:
        with sqlite3.connect(f"file:{self.tree['journal']}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT seq, from_state, to_state, event_kind, payload_json FROM operation_events "
                "WHERE operation_handle=? ORDER BY seq",
                (operation_handle,),
            ).fetchall()

    def receipt(self, operation_handle: str) -> dict:
        with sqlite3.connect(f"file:{self.tree['journal']}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase!='staged'",
                (operation_handle,),
            ).fetchone()
        return json.loads(row[0])

    def test_1_executor_owns_its_uid_derived_name(self) -> None:
        owner = self.connection.get_name_owner(self.executor_name)
        self.assertEqual(int(self.connection.get_unix_user(owner)), os.getuid())

    def test_2_verify_apply_verify_end_to_end(self) -> None:
        plan_handle, plan_digest, summary = self.manager("Plan", "sss", ("recovery", "manifest-r1", "e2e-plan"))
        seat_root = Path(self.tree["seat_root"])
        self.assertEqual(sorted(seat_root.iterdir()), [])

        # Verify before apply: completes as an operation, reports drift
        before = str(self.manager("Verify", "sss", ("recovery", plan_handle, "e2e-verify-1")))
        record = self.wait_terminal(before)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["kind"], "verify")
        self.assertEqual(record["approval_handle"], VERIFY_APPROVAL_SENTINEL)
        self.assertEqual(record["authority_record_id"], VERIFY_AUTHORITY_SENTINEL)
        receipt = self.receipt(before)
        self.assertEqual(receipt["mutations"], [])
        self.assertTrue(receipt["evidence"])
        self.assertEqual(sorted(seat_root.iterdir()), [])  # read-only

        # Apply: the executor materializes the seat-root files
        applied = str(self.manager("Apply", "ssss", (plan_handle, plan_digest, "e2e-apply", "ab" * 32)))
        record = self.wait_terminal(applied)
        self.assertEqual(record["state"], "completed", (self.receipt(applied).get("evidence"), self._operation_events(applied)))
        receipt = self.receipt(applied)
        self.assertEqual(receipt["phase"], "complete")
        # review S4: the mutating spec was served exactly once
        served = [e for e in self._operation_events(applied) if e[3] == "spec_served"]
        self.assertEqual(len(served), 1)
        self.assertTrue(any(m["changed"] for m in receipt["mutations"]))
        self.assertTrue((seat_root / ".config" / "systemd" / "user").is_dir())
        units = sorted(p.name for p in (seat_root / ".config" / "systemd" / "user").iterdir())
        self.assertIn("pantheon-persona-consume@.service", units)

        # Verify after apply: everything converged
        after = str(self.manager("Verify", "sss", ("recovery", plan_handle, "e2e-verify-2")))
        record = self.wait_terminal(after)
        self.assertEqual(record["state"], "completed", self.receipt(after))
        self.assertTrue(all(item["ok"] for item in self.receipt(after)["evidence"]))

        # the executor never touched the journal: every receipt row was
        # written by the broker process, and nothing journal-like landed in
        # the seat root
        found = [p.name for p in seat_root.rglob("*") if p.suffix in (".sqlite3", ".db", ".wal")]
        self.assertEqual(found, [])

    def test_3_execute_cannot_act_without_the_brokers_spec(self) -> None:
        # One uid plays every role on this fixture bus, so the test client
        # passes the uid gate (cross-uid refusal is unit-tested and T-2924's
        # live proof). It still cannot make the executor act: the work is
        # defined ONLY by the broker's ExecutionSource spec, which refuses
        # an operation the broker never dispatched.
        def call(method, signature, args):
            return self.connection.call_blocking(
                self.executor_name, executor_wire.OBJECT_PATH, executor_wire.EXECUTOR_INTERFACE,
                method, signature, args,
            )

        execution = str(call("Execute", "ss", ("ab" * 32, "cd" * 32)))
        deadline = time.monotonic() + 120
        while True:
            state, raw = call("ExecutionGet", "s", (execution,))
            if state != "running":
                break
            if time.monotonic() > deadline:
                self.fail(f"execution still running after 120s; logs: {self._logs()}")
            time.sleep(0.05)
        self.assertEqual(state, "refused")  # T-2917 5d.1 (T-2917:D14): refused before the engine ran
        result = json.loads(raw)
        self.assertEqual((result["mutations"], result["evidence"]), ([], []))
        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            call("ExecutionGet", "s", ("0" * 64,))
        self.assertEqual(caught.exception.get_dbus_name(), executor_wire.UNKNOWN_EXECUTION_ERROR)


class EntrypointStartRefusalTests(unittest.TestCase):
    def test_unregistered_uid_refuses_before_any_bus_connection(self) -> None:
        from tools.host_control.executor import __main__ as entry
        from tools.host_control.executor.config import EXECUTOR_CONFIG_SCHEMA
        from tools.host_control.trusted_files import RootOwnedFileLoader

        with tempfile.TemporaryDirectory() as temp:
            tree = host.prepare_tree(Path(temp) / "t")
            etc = Path(temp) / "etc"
            etc.mkdir()
            etc.chmod(0o755)
            config_path = etc / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema": EXECUTOR_CONFIG_SCHEMA,
                        "registry_path": str(Path(tree["trusted"]) / "registry.json"),
                        "trusted_roots": [tree["trusted"], tree["framework"]],
                        "broker_uid": os.getuid(),
                        "framework_root": tree["framework"],
                        # T-2917 5d.3 (T-2917:D19): the v1 config's hub key
                        "mailbox_hub": tree["mailbox_hub"],
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o644)

            def loader(roots):
                return RootOwnedFileLoader._open_for_test(tuple(roots), expected_owner_uid=os.getuid())

            def never_connect():
                raise AssertionError("must not connect before identity is resolved")

            import contextlib
            import io

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = entry.run(
                    config_path=config_path,
                    open_loader=loader,
                    getuid=lambda: os.getuid() + 1,
                    connection_factory=never_connect,
                )
            self.assertEqual(code, entry.EXIT_START_REFUSED)
            # D30: the UID refusal, not a config/registry RegistryError
            # (same exit code 78)
            self.assertIn("start refused: ExecutorStartRefused", stderr.getvalue())
            self.assertEqual(entry.main(["--seat", "other"]), 2)


if __name__ == "__main__":  # must stay LAST
    unittest.main()
