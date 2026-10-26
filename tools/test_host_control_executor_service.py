"""T-2917 5b: the executor service core (caller gates, one-at-a-time
execution table, worker), its isolation from the broker journal, and the
staged executor artifacts. Transport-free; the D-Bus shell is covered by
tools/test_host_control_executor_dbus.py.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from tools import _host_control_executor_fixtures as fx
from tools.host_control import wire as broker_wire
from tools.host_control.contracts import canonical_json, sha256_json
from tools.host_control.executor import dbus_service as executor_service_module
from tools.host_control.executor.dbus_service import ExecutionTable, ExecutorService
from tools.host_control.executor.engine import EXECUTION_SPEC_SCHEMA
from tools.host_control.executor.wire import (
    ExecutorBusy,
    ExecutorDenied,
    InvalidSpec,
    UnknownExecution,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_PACKAGE = REPO_ROOT / "tools" / "host_control" / "executor"
DIST = REPO_ROOT / "tools" / "host_control" / "dist"
BROKER_UID = 5555
BROKER_UNIQUE = ":1.42"


class FakeBus:
    def __init__(self) -> None:
        self.users = {BROKER_UNIQUE: BROKER_UID, ":1.99": BROKER_UID, ":1.7": 1234}
        self.owner = BROKER_UNIQUE

    def unix_user(self, unique_name: str) -> int:
        return self.users[unique_name]

    def name_owner(self, well_known_name: str) -> str:
        assert well_known_name == "org.pantheon.HostBroker1"
        return self.owner


class FakeSpecSource:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []
        self.refuse: Exception | None = None

    def operation_spec(self, broker_unique_name, operation_handle, expected_digest):
        self.calls.append((broker_unique_name, operation_handle, expected_digest))
        if self.refuse is not None:
            raise self.refuse
        return (
            EXECUTION_SPEC_SCHEMA,
            self.payload["execution_kind"],
            sha256_json(self.payload),
            canonical_json(self.payload),
        )


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.target = self.fixture.seat_root / "made"
        self.payload = fx.spec_payload(
            self.fixture, (fx.op("dir", "mkdir", str(self.target), {"mode": "0755"}),)
        )
        self.digest = sha256_json(self.payload)
        self.bus = FakeBus()
        self.source = FakeSpecSource(self.payload)
        self.deferred: list = []
        self.service = ExecutorService(
            identity=self.fixture.identity,
            registry=self.fixture.registry,
            broker_uid=BROKER_UID,
            engine=self.fixture.engine(),
            spec_source=self.source,
            bus=self.bus,
            start_worker=self.deferred.append,
        )

    def run_deferred(self) -> None:
        while self.deferred:
            self.deferred.pop(0)()

    def test_a_non_owner_broker_connection_is_accepted_but_the_spec_comes_from_the_owner(self) -> None:
        handle = self.service.execute(":1.99", fx.OPERATION_HANDLE, self.digest)
        self.run_deferred()
        self.assertEqual(self.service.table.get(handle)[0], "succeeded")
        self.assertEqual(self.source.calls, [(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)])

    def test_execute_returns_immediately_and_work_runs_off_the_handler(self) -> None:
        handle = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        self.assertEqual(len(handle), 64)
        # nothing happened yet: the handler only queued the work
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.calls, [])
        self.assertEqual(self.service.execution_get(BROKER_UNIQUE, handle), ("running", ""))
        self.run_deferred()
        state, raw = self.service.execution_get(BROKER_UNIQUE, handle)
        self.assertEqual(state, "succeeded")
        result = json.loads(raw)
        self.assertEqual(result["operation_handle"], fx.OPERATION_HANDLE)
        self.assertEqual(result["spec_digest"], self.digest)
        self.assertTrue(self.target.is_dir())
        # the spec was fetched from the VERIFIED unique name, not the
        # well-known name
        self.assertEqual(self.source.calls, [(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)])

    def test_caller_must_be_the_broker_uid_and_name_owner(self) -> None:
        with self.assertRaises(ExecutorDenied):
            self.service.execute(":1.7", fx.OPERATION_HANDLE, self.digest)  # wrong uid
        # a second connection of the broker uid (the driver's outbound
        # connection) is accepted; the spec still comes from the OWNER
        self.bus.owner = ":1.7"  # broker name now held by another uid
        with self.assertRaises(ExecutorDenied):
            self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        self.bus.owner = BROKER_UNIQUE
        with self.assertRaises(ExecutorDenied):
            self.service.execute(None, fx.OPERATION_HANDLE, self.digest)
        with self.assertRaises(ExecutorDenied):
            self.service.execute(":1.404", fx.OPERATION_HANDLE, self.digest)  # unattested
        handle = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        with self.assertRaises(ExecutorDenied):
            self.service.execution_get(":1.7", handle)
        self.assertEqual(self.deferred.__len__(), 1)

    def test_duplicate_busy_and_digest_rebinding(self) -> None:
        first = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        self.assertEqual(self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest), first)
        self.assertEqual(len(self.deferred), 1)  # one worker, not two
        with self.assertRaises(ExecutorDenied):
            self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, "0" * 64)
        with self.assertRaises(ExecutorBusy):
            self.service.execute(BROKER_UNIQUE, "cd" * 32, self.digest)
        self.run_deferred()
        # finished: the duplicate still gets the same handle (retained)
        self.assertEqual(self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest), first)
        # and a different operation may now start
        other = self.service.execute(BROKER_UNIQUE, "cd" * 32, "ef" * 32)
        self.assertNotEqual(other, first)

    def test_malformed_inputs_and_unknown_execution(self) -> None:
        with self.assertRaises(InvalidSpec):
            self.service.execute(BROKER_UNIQUE, "x", self.digest)
        with self.assertRaises(UnknownExecution):
            self.service.execution_get(BROKER_UNIQUE, "0" * 64)
        with self.assertRaises(UnknownExecution):
            self.service.execution_get(BROKER_UNIQUE, "not-a-handle")

    def test_spec_for_another_seat_fails_with_nothing_executed(self) -> None:
        self.source.payload = fx.spec_payload(
            self.fixture,
            (fx.op("dir", "mkdir", str(self.target), {"mode": "0755"}),),
            target_seat_id="other",
        )
        digest = sha256_json(self.source.payload)
        handle = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, digest)
        self.run_deferred()
        state, raw = self.service.execution_get(BROKER_UNIQUE, handle)
        # T-2917 5d.1 (T-2917:D14): a pre-engine refusal is its own state
        self.assertEqual(state, "refused")
        value = json.loads(raw)
        self.assertEqual((value["mutations"], value["evidence"]), ([], []))
        self.assertIn("Denied", value["detail"])
        self.assertFalse(self.target.exists())

    def test_broker_identity_rechecked_before_the_spec_fetch(self) -> None:
        handle = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        self.bus.users[BROKER_UNIQUE] = 1234  # name re-owned by another uid
        self.run_deferred()
        self.assertEqual(self.service.table.get(handle)[0], "refused")  # T-2917:D14
        self.assertEqual(self.source.calls, [])
        self.assertFalse(self.target.exists())

    def test_spec_refused_by_the_broker_is_a_failure_with_no_mutation(self) -> None:
        # E30 executor half: OperationSpec answers only after DISPATCH_INTENT;
        # a refusal there means the executor touches nothing
        from tools.host_control.contracts import InvalidTransition

        self.source.refuse = InvalidTransition("operation is not dispatchable")
        handle = self.service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, self.digest)
        self.run_deferred()
        state, raw = self.service.table.get(handle)
        self.assertEqual(state, "refused")  # T-2917:D14: pre-engine refusal
        self.assertEqual(json.loads(raw)["mutations"], [])
        self.assertFalse(self.target.exists())

    def test_table_retention_is_bounded(self) -> None:
        counter = iter(range(1, 100))
        table = ExecutionTable(handle_factory=lambda: format(next(counter), "064x"), retain=2)
        handles = []
        for index in range(4):
            entry, _ = table.submit(format(index + 1, "064x"), "ab" * 32)
            table.finish(entry.execution_handle, "succeeded", "{}")
            handles.append(entry.execution_handle)
        with self.assertRaises(UnknownExecution):
            table.get(handles[0])
        self.assertEqual(table.get(handles[-1])[0], "succeeded")


class IsolationTests(unittest.TestCase):
    """ORG-052 section 6: the executor never imports or writes the broker
    journal; it returns typed records only."""

    FORBIDDEN = (
        "tools.host_control.journal",
        "tools.host_control.operations",
        "tools.host_control.execution_source",
        "tools.host_control.driver",
        "tools.host_control.executor_client",
        "tools.host_control.dbus_service",
        "sqlite3",
    )

    EXECUTOR_MODULES = [
        "__init__.py",
        "__main__.py",
        "config.py",
        "dbus_service.py",
        "engine.py",
        "identity.py",
        "mailbox_probe.py",  # T-2917 5d.3: the confined mailbox probes
        "seat_files.py",
        "user_bus.py",  # T-2917 5c.2: the user-bus DBusUserManager
        "user_manager.py",
        "wire.py",
    ]

    def test_executor_package_inventory_is_exact(self) -> None:
        # a new module must be added here deliberately, so it is also
        # covered by the import bans below (review S2)
        self.assertEqual(sorted(p.name for p in EXECUTOR_PACKAGE.glob("*.py")), self.EXECUTOR_MODULES)
        self.assertEqual([p.name for p in EXECUTOR_PACKAGE.iterdir() if p.is_dir() and p.name != "__pycache__"], [])

    def test_no_executor_module_imports_journal_machinery(self) -> None:
        sources = sorted(EXECUTOR_PACKAGE.glob("*.py"))
        self.assertEqual([p.name for p in sources], self.EXECUTOR_MODULES)
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: set[str] = set()
            # ast.walk visits imports nested in functions/classes too
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                    for alias in node.names:
                        with self.subTest(module=path.name, imported=alias.name):
                            self.assertNotEqual(alias.name, "tools.host_control", "parent-package import")
                elif isinstance(node, ast.ImportFrom):
                    with self.subTest(module=path.name, line=node.lineno):
                        # relative imports would evade the absolute-name ban
                        self.assertEqual(node.level, 0, f"{path.name}:{node.lineno} relative import")
                        # the parent package re-exports broker symbols
                        self.assertNotEqual(node.module, "tools.host_control", "parent-package import")
                        # review n1: `from tools import host_control` is the
                        # same parent-package import spelled differently
                        if node.module == "tools":
                            self.assertNotIn(
                                "host_control", [alias.name for alias in node.names], "parent-package import"
                            )
                    if node.module:
                        imported.add(node.module)
                        imported.update(f"{node.module}.{alias.name}" for alias in node.names)
                elif isinstance(node, ast.Call):
                    # review n1: dynamic imports naming a module by string
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in ("import_module", "__import__") and node.args:
                        first = node.args[0]
                        with self.subTest(module=path.name, line=node.lineno):
                            self.assertIsInstance(first, ast.Constant, "dynamic import of a computed name")
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            imported.add(first.value)
            with self.subTest(module=path.name):
                for forbidden in self.FORBIDDEN:
                    self.assertFalse(
                        any(name == forbidden or name.startswith(forbidden + ".") for name in imported),
                        f"{path.name} imports {forbidden}",
                    )

    def test_transitive_import_closure_excludes_the_journal(self) -> None:
        probe = (
            "import sys\n"
            "import tools.host_control.executor.__main__\n"
            "import tools.host_control.executor.dbus_service\n"
            f"bad = [m for m in {list(self.FORBIDDEN)!r} if m in sys.modules]\n"
            "print(','.join(bad))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_transcribed_broker_names_match(self) -> None:
        self.assertEqual(executor_service_module.EXECUTION_SOURCE_INTERFACE, broker_wire.EXECUTION_SOURCE_INTERFACE)
        self.assertEqual(executor_service_module.BROKER_BUS_NAME, broker_wire.BUS_NAME)
        self.assertEqual(executor_service_module.BROKER_OBJECT_PATH, broker_wire.OBJECT_PATH)


def _unit_values(raw: str) -> dict:
    values: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[", ";")):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key, []).append(value)
    return values


class StagedArtifactTests(unittest.TestCase):
    def test_executor_policy_is_default_deny_and_member_level(self) -> None:
        raw = (DIST / "org.pantheon.HostExecutor1.conf").read_text(encoding="utf-8")
        self.assertIn("STAGED ARTIFACT", raw)
        self.assertIn("PROVISIONAL", raw)
        tree = ElementTree.fromstring(raw)
        policies = {
            (node.get("context") or node.get("user")): node for node in tree.findall("policy")
        }
        self.assertEqual(set(policies), {"default", "pantheon-hostbroker"})
        default = policies["default"]
        self.assertEqual(default.findall("allow"), [])
        self.assertEqual(
            {tuple(sorted(node.attrib.items())) for node in default.findall("deny")},
            {
                (("own_prefix", "org.pantheon.HostExecutor1"),),
                (("send_destination_prefix", "org.pantheon.HostExecutor1"),),
            },
        )
        broker = policies["pantheon-hostbroker"]
        self.assertEqual(broker.findall("deny"), [])
        members = set()
        for allow in broker.findall("allow"):
            self.assertIsNone(allow.get("own"))
            self.assertIsNone(allow.get("own_prefix"))
            self.assertEqual(allow.get("send_destination_prefix"), "org.pantheon.HostExecutor1")
            self.assertEqual(allow.get("send_interface"), "org.pantheon.HostExecutor1.Executor")
            members.add(allow.get("send_member"))
        self.assertEqual(members, {"Execute", "ExecutionGet"})

    def _daemon_accepts(self, policy_path: Path) -> bool:
        """True when the daemon starts with the policy, False when the
        daemon REJECTS it (exits). A start timeout is neither: it fails the
        test loudly instead of masquerading as a rejection (review S5)."""

        from tools import _host_control_testbus as testbus

        template = (
            '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN" '
            '"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
            "<busconfig><type>session</type><listen>unix:path={socket}</listen>"
            "<auth>EXTERNAL</auth>"
            f"<include>{policy_path}</include></busconfig>\n"
        )
        try:
            handle = testbus.spawn_daemon("t2917x-", template, timeout=60.0)
        except AssertionError as exc:
            if "exited early" in str(exc):
                return False
            self.fail(f"dbus-daemon neither started nor exited within 60s: {exc}")
        testbus.stop_daemon(handle)
        return True

    def test_executor_policy_loads_in_a_real_dbus_daemon(self) -> None:
        # the prefix attributes must be ones this dbus-daemon accepts; the
        # negative control proves a policy error really stops the daemon
        from tools import _host_control_testbus as testbus

        if testbus.DBUS_DAEMON is None:
            self.skipTest("dbus-daemon not installed")
        self.assertTrue(self._daemon_accepts(DIST / "org.pantheon.HostExecutor1.conf"))
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.conf"
            broken.write_text(
                (DIST / "org.pantheon.HostExecutor1.conf")
                .read_text(encoding="utf-8")
                .replace("own_prefix=", "own_prefixx="),
                encoding="utf-8",
            )
            self.assertFalse(self._daemon_accepts(broken))

    def test_unit_template_is_hardened_and_never_uses_the_instance_name(self) -> None:
        raw = (DIST / "pantheon-host-executor@.service").read_text(encoding="utf-8")
        self.assertIn("STAGED TEMPLATE", raw)
        self.assertIn("PROVISIONAL", raw)
        self.assertIn("T-2920", raw)
        unit = _unit_values(raw)
        self.assertEqual(unit["Type"], ["dbus"])
        self.assertEqual(unit["BusName"], ["org.pantheon.HostExecutor1.U@SEAT_UID@"])
        self.assertEqual(unit["User"], ["@SEAT_ACCOUNT@"])
        self.assertEqual(unit["NoNewPrivileges"], ["yes"])
        self.assertEqual(unit["CapabilityBoundingSet"], [""])
        self.assertEqual(unit["ProtectSystem"], ["strict"])
        # T-2917 5d.3 (T-2917:D19; HO-accepted staged write surface,
        # verified at T-2920 install): the seat root plus the mailbox hub
        self.assertEqual(unit["ReadWritePaths"], ["@SEAT_ROOT@ @MAILBOX_HUB@"])
        self.assertIn("@MAILBOX_HUB@", raw.split("[Unit]")[0])  # the header lists the substitution
        self.assertEqual(unit["RestrictAddressFamilies"], ["AF_UNIX"])
        body = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("%i", body)
        self.assertNotIn("%I", body)
        exec_start = unit["ExecStart"][0]
        for metachar in ("|", ";", "&", "$", "`", " "):
            self.assertNotIn(metachar, exec_start)

    def test_activation_template_delegates_to_systemd(self) -> None:
        raw = (DIST / "org.pantheon.HostExecutor1.service.template").read_text(encoding="utf-8")
        values = _unit_values(raw)
        self.assertEqual(values["Name"], ["org.pantheon.HostExecutor1.U@SEAT_UID@"])
        self.assertEqual(values["Exec"], ["/bin/false"])
        self.assertEqual(values["User"], ["@SEAT_ACCOUNT@"])
        self.assertEqual(values["SystemdService"], ["pantheon-host-executor@@SEAT_ID@.service"])


if __name__ == "__main__":  # must stay LAST
    unittest.main()
