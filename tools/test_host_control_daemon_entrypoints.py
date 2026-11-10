"""T-2920 U2: the production broker and approval daemon entrypoints, their
closed root-config schemas, and the keyset resume walk (in-process tier; no
bus).

Intent (T-2920 / T-2652; ORG-052 sections 3-6, gates 19 and 36): the installer
needs installable daemons. The broker must start with reconcile ON, the
health legs wired, its registry bound to the framework copy and its runtime
digest logged; the approval service must re-arm EVERY pending human card
(T-5518), not only the first page; both refuse euid 0 and refuse any config
fault BEFORE touching a bus. The private-bus boots (zero executors reachable,
the full resume) are in tools/test_host_control_daemon_entrypoints_dbus.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tools import test_host_control_daemon_entrypoints_driver as fx
from tools.host_control.approval.config import (
    APPROVAL_CONFIG_KEYS,
    APPROVAL_CONFIG_SCHEMA,
    DEFAULT_APPROVAL_CONFIG_PATH,
    HUMAN_INGRESS_KEYS,
    parse_approval_config,
)
from tools.host_control.broker_config import (
    BROKER_CONFIG_OPTIONAL_KEYS,
    BROKER_CONFIG_REQUIRED_KEYS,
    BROKER_CONFIG_SCHEMA,
    DEFAULT_BROKER_CONFIG_PATH,
    parse_broker_config,
)
from tools.host_control.contracts import HostControlError, RegistryError
from tools.host_control.trusted_files import RootOwnedFileLoader

UID = os.getuid()


def _loader(roots):
    return RootOwnedFileLoader._open_for_test(tuple(roots), expected_owner_uid=UID)


class _Spy:
    def __init__(self, inner=None) -> None:
        self.calls: list[tuple] = []
        self.inner = inner

    def __call__(self, *args):
        self.calls.append(args)
        if self.inner is None:
            raise AssertionError("must not be called")
        return self.inner(*args)


def _broker_raw(**overrides) -> dict:
    value = {
        "schema": BROKER_CONFIG_SCHEMA,
        "trusted_roots": ["/etc/pantheon/host-broker", "/usr/local/lib/pantheon/framework/r1"],
        "registry_path": "/etc/pantheon/host-broker/seat-registry.json",
        "policy_path": "/etc/pantheon/host-broker/policy.json",
        "framework_root": "/usr/local/lib/pantheon/framework/r1",
        "journal_path": "/var/lib/pantheon-host-broker/broker.sqlite3",
        "approval_uid": 991,
        "monitor_uids": [993],
        "host_operation_families": ["persona.install", "persona.verify"],
        "plan_ttl_seconds": 3600,
        "broker_revision": "framework-r1",
    }
    value.update(overrides)
    return value


def _approval_raw(**overrides) -> dict:
    value = {
        "schema": APPROVAL_CONFIG_SCHEMA,
        "trusted_roots": ["/etc/pantheon/host-approval"],
        "approver_policy_path": "/etc/pantheon/host-approval/approver-policy.json",
        "journal_path": "/var/lib/pantheon-host-approval/approval.sqlite3",
        "broker_uid": 990,
        "monitor_uids": [993],
        "human_ingress": {
            "ingress_uid": 992,
            "human_subject": "operator",
            "provider_user_id": "424242",
            "provider_chat_id": "424242",
        },
    }
    value.update(overrides)
    return value


class BrokerConfigSchemaTests(unittest.TestCase):
    def test_key_set_and_default_path_are_pinned(self) -> None:
        self.assertEqual(
            BROKER_CONFIG_REQUIRED_KEYS,
            {
                "schema", "trusted_roots", "registry_path", "policy_path", "framework_root",
                "journal_path", "approval_uid", "monitor_uids", "host_operation_families",
                "plan_ttl_seconds", "broker_revision",
            },
        )
        self.assertEqual(BROKER_CONFIG_OPTIONAL_KEYS, {"compensate"})
        self.assertEqual(DEFAULT_BROKER_CONFIG_PATH, Path("/etc/pantheon/host-broker/config.json"))
        self.assertEqual(BROKER_CONFIG_SCHEMA, "pantheon-host-broker-config/v1")

    def test_valid_config_parses_with_compensate_off_by_default(self) -> None:
        config = parse_broker_config(_broker_raw())
        self.assertFalse(config.compensate)
        self.assertEqual(config.framework_root, Path("/usr/local/lib/pantheon/framework/r1"))
        self.assertEqual(config.host_operation_families, frozenset(("persona.install", "persona.verify")))
        self.assertTrue(parse_broker_config(_broker_raw(compensate=True)).compensate)
        self.assertFalse(parse_broker_config(_broker_raw(compensate=False)).compensate)

    def test_refusals(self) -> None:
        missing = _broker_raw()
        del missing["approval_uid"]
        cases = {
            "unknown key": _broker_raw(bot_token="x"),
            "missing key": missing,
            "not an object": ["schema"],
            "wrong schema": _broker_raw(schema="pantheon-host-broker-config/v2"),
            "relative path": _broker_raw(registry_path="etc/registry.json"),
            "double slash": _broker_raw(registry_path="//etc/pantheon/host-broker/seat-registry.json"),
            "dot dot": _broker_raw(policy_path="/etc/pantheon/host-broker/../x.json"),
            "registry outside roots": _broker_raw(registry_path="/etc/other/registry.json"),
            "registry equals a root": _broker_raw(registry_path="/etc/pantheon/host-broker"),
            "framework outside roots": _broker_raw(framework_root="/opt/framework"),
            "framework is the active link": _broker_raw(
                trusted_roots=["/etc/pantheon/host-broker", "/usr/local/lib/pantheon/framework"],
                framework_root="/usr/local/lib/pantheon/framework/active",
            ),
            "journal inside a trusted root": _broker_raw(journal_path="/etc/pantheon/host-broker/b.sqlite3"),
            "journal inside the framework": _broker_raw(
                trusted_roots=["/etc/pantheon/host-broker", "/usr/local/lib/pantheon"],
                journal_path="/usr/local/lib/pantheon/framework/r1/b.sqlite3",
            ),
            "bool uid": _broker_raw(approval_uid=True),
            "negative uid": _broker_raw(approval_uid=-1),
            "root approval uid": _broker_raw(approval_uid=0),
            "root monitor uid": _broker_raw(monitor_uids=[993, 0]),
            "no monitors": _broker_raw(monitor_uids=[]),
            "duplicate monitor": _broker_raw(monitor_uids=[1, 1]),
            "no families": _broker_raw(host_operation_families=[]),
            "duplicate family": _broker_raw(host_operation_families=["a", "a"]),
            "spaced family": _broker_raw(host_operation_families=["a b"]),
            "ttl too short": _broker_raw(plan_ttl_seconds=59),
            "ttl too long": _broker_raw(plan_ttl_seconds=86401),
            "bool ttl": _broker_raw(plan_ttl_seconds=True),
            "compensate not a bool": _broker_raw(compensate="yes"),
            "compensate int": _broker_raw(compensate=1),
            "duplicate root": _broker_raw(trusted_roots=["/etc/pantheon/host-broker"] * 2),
            "no roots": _broker_raw(trusted_roots=[]),
            "empty revision": _broker_raw(broker_revision=""),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RegistryError):
                    parse_broker_config(raw)


class ApprovalConfigSchemaTests(unittest.TestCase):
    def test_key_set_and_default_path_are_pinned(self) -> None:
        self.assertEqual(
            APPROVAL_CONFIG_KEYS,
            {"schema", "trusted_roots", "approver_policy_path", "journal_path", "broker_uid", "monitor_uids", "human_ingress"},
        )
        self.assertEqual(HUMAN_INGRESS_KEYS, {"ingress_uid", "human_subject", "provider_user_id", "provider_chat_id"})
        self.assertEqual(DEFAULT_APPROVAL_CONFIG_PATH, Path("/etc/pantheon/host-approval/config.json"))
        self.assertEqual(APPROVAL_CONFIG_SCHEMA, "pantheon-host-approval-config/v1")

    def test_valid_config_builds_the_ingress_registration(self) -> None:
        config = parse_approval_config(_approval_raw())
        registration = config.human_ingress
        self.assertEqual(
            (registration.uid, registration.provider, registration.human_subject),
            (992, "telegram", "operator"),
        )
        self.assertEqual(registration.provider_chat_id, registration.provider_user_id)
        self.assertEqual(config.broker_uid, 990)

    def test_refusals(self) -> None:
        def ingress(**overrides):
            value = dict(_approval_raw()["human_ingress"])
            value.update(overrides)
            return _approval_raw(human_ingress=value)

        missing = _approval_raw()
        del missing["human_ingress"]
        no_subject = dict(_approval_raw()["human_ingress"])
        del no_subject["human_subject"]
        cases = {
            "unknown key": _approval_raw(bot_token="x"),
            "missing key": missing,
            "wrong schema": _approval_raw(schema="pantheon-host-approval-config/v0"),
            "policy outside roots": _approval_raw(approver_policy_path="/etc/other/p.json"),
            "journal inside a trusted root": _approval_raw(journal_path="/etc/pantheon/host-approval/a.sqlite3"),
            "unknown ingress key": ingress(provider="telegram"),
            "missing ingress key": _approval_raw(human_ingress=no_subject),
            "group chat": ingress(provider_chat_id="-100123"),
            "chat differs from user": ingress(provider_chat_id="424243"),
            "numeric id not a string": ingress(provider_user_id=424242),
            "leading zero id": ingress(provider_user_id="0424242", provider_chat_id="0424242"),
            "ingress is the broker": ingress(ingress_uid=990),
            "bool ingress uid": ingress(ingress_uid=False),
            "root ingress uid": ingress(ingress_uid=0),
            "root broker uid": _approval_raw(broker_uid=0),
            "root monitor uid": _approval_raw(monitor_uids=[0]),
            "no monitors": _approval_raw(monitor_uids=[]),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RegistryError):
                    parse_approval_config(raw)


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t2920u2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class BrokerEntrypointTests(_TreeCase):
    def _tree(self, **kwargs) -> dict:
        return fx.broker_tree(
            self.tmp / "broker", subject_uid=UID, approval_uid=UID + 54321, monitor_uids=[UID], **kwargs
        )

    def _build(self, tree: dict, logs: list[str] | None = None):
        from tools.host_control import __main__ as entry
        from tools.host_control.broker_config import load_broker_config

        config_path = Path(tree["config_path"])
        with _loader((config_path.parent,)) as bootstrap:
            config = load_broker_config(config_path, files=bootstrap)
        files = _loader(config.trusted_roots)
        stack = entry.build_broker(
            config,
            files,
            connect=_Spy(),
            log=(logs.append if logs is not None else (lambda _m: None)),
        )
        self.addCleanup(stack.close)
        return stack

    def _run_with_fake_bus(self, *, status_close=None):
        """Drive ``entry.run`` (run_loop=False) past start-up with the bus
        objects faked, recording how the T-5644 status client is wired and the
        order of the shutdown calls. Returns (record, connect, rc)."""

        from unittest import mock

        from tools.host_control import __main__ as entry
        from tools.host_control import dbus_service, health_dbus

        record: dict = {"order": [], "built": [], "service_kwargs": None}
        client = object()

        class _StatusConnection:
            def close(self) -> None:
                record["order"].append("status_connection.close")
                if status_close is not None:
                    raise status_close

        real_build_broker = entry.build_broker

        def build_broker(*args, **kwargs):
            stack = real_build_broker(*args, **kwargs)
            record["stack"] = stack
            return stack

        def build_status_client(connect_arg, ownership):
            record["built"].append((connect_arg, ownership))
            return client, _StatusConnection()

        class _Service:
            def __init__(self, connection, **kwargs) -> None:
                record["service_kwargs"] = kwargs

            def close(self) -> None:
                record["order"].append("service_object.close")

        class _Health:
            def __init__(self, *args, **kwargs) -> None:
                pass

        real_stack_close = entry.BrokerStack.close

        def stack_close(stack) -> None:
            record["order"].append("stack.close")
            real_stack_close(stack)

        # run() imports dbus / gi at function level, and the service classes
        # exist only when dbus imports. Fake all of it (modules and classes) so
        # this wiring test runs on a runner with NO dbus-python too; the real
        # service modules are imported BEFORE the fakes go in.
        fake_modules = {}
        for name in (
            "dbus",
            "dbus.exceptions",
            "dbus.mainloop",
            "dbus.mainloop.glib",
            "dbus.service",
            "gi",
            "gi.repository",
        ):
            fake_modules[name] = types.ModuleType(name)
        fake_modules["dbus"].exceptions = fake_modules["dbus.exceptions"]
        fake_modules["dbus"].mainloop = fake_modules["dbus.mainloop"]
        fake_modules["dbus"].service = fake_modules["dbus.service"]
        fake_modules["dbus.mainloop"].glib = fake_modules["dbus.mainloop.glib"]
        fake_modules["dbus.exceptions"].DBusException = type("DBusException", (Exception,), {})
        fake_modules["dbus.mainloop.glib"].DBusGMainLoop = lambda **kwargs: None
        fake_modules["dbus.mainloop.glib"].threads_init = lambda: None
        fake_modules["dbus.service"].BusName = lambda *args, **kwargs: object()
        fake_modules["gi.repository"].GLib = object()
        fake_modules["gi"].repository = fake_modules["gi.repository"]

        tree = self._tree()
        connect = _Spy(inner=lambda main_loop: object())
        with mock.patch.object(entry, "build_broker", build_broker), mock.patch.object(
            entry, "build_status_client", build_status_client
        ), mock.patch.object(entry.BrokerStack, "close", stack_close), mock.patch.object(
            dbus_service, "HostBroker1Object", _Service, create=True
        ), mock.patch.object(health_dbus, "Health1Object", _Health, create=True), mock.patch.dict(
            sys.modules, fake_modules
        ):
            try:
                code = entry.run(
                    config_path=Path(tree["config_path"]),
                    open_loader=_loader,
                    connect=connect,
                    run_loop=False,
                    log=lambda _m: None,
                )
            except RuntimeError as exc:
                code = exc
        record["client"] = client
        return record, connect, code

    def test_run_wires_the_status_client_and_closes_in_order(self) -> None:
        """T-5644 r2: pin ``run``'s wiring, where the r1 bug lived (a
        blocking-only status connection never fires call_async replies)."""

        record, connect, code = self._run_with_fake_bus()
        self.assertEqual(code, 0)
        # (a) the real connect (identity, no wrapper) and the live journal
        self.assertEqual(len(record["built"]), 1)
        built_connect, built_ownership = record["built"][0]
        self.assertIs(built_connect, connect)
        self.assertIs(built_ownership, record["stack"].journal)
        # (b) exactly the object the status builder returned is what the
        # service gets as its approval_status
        self.assertIs(record["service_kwargs"]["approval_status"], record["client"])
        # (c) shutdown order: service, then the status connection, then stack
        self.assertEqual(record["order"], ["service_object.close", "status_connection.close", "stack.close"])

    def test_a_failing_status_connection_close_still_closes_the_stack(self) -> None:
        record, _connect, code = self._run_with_fake_bus(status_close=RuntimeError("close failed"))
        self.assertIsInstance(code, RuntimeError)
        self.assertEqual(record["order"], ["service_object.close", "status_connection.close", "stack.close"])

    def test_euid_zero_is_refused_before_any_file_is_read(self) -> None:
        from tools.host_control import __main__ as entry

        loader, connect, logs = _Spy(), _Spy(), []
        code = entry.run(
            config_path=Path("/nonexistent/config.json"),
            open_loader=loader,
            geteuid=lambda: 0,
            connect=connect,
            log=logs.append,
        )
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(loader.calls, [])
        self.assertEqual(connect.calls, [])
        self.assertIn("euid 0", logs[0])

    def test_a_config_fault_is_refused_before_any_bus_connection(self) -> None:
        from tools.host_control import __main__ as entry

        tree = self._tree()
        raw = json.loads(Path(tree["config_path"]).read_text(encoding="utf-8"))
        raw["bot_token"] = "inline-secret"
        fx._write_json(Path(tree["config_path"]), raw)
        connect, logs = _Spy(), []
        code = entry.run(
            config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append
        )
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs, ["start refused: RegistryError"])

    def test_main_takes_no_arguments(self) -> None:
        from tools.host_control import __main__ as entry

        self.assertEqual(entry.main(["--config", "/x"]), 2)

    def test_a_corrupt_journal_is_refused_with_exit_78(self) -> None:
        from tools.host_control import __main__ as entry

        tree = self._tree()
        Path(tree["journal"]).write_bytes(b"not a sqlite database " * 64)
        connect, logs = _Spy(), []
        code = entry.run(config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append)
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs[-1], "start refused: HostControlError")

    def test_a_raw_sqlite_error_at_journal_open_is_exit_78(self) -> None:
        """LOW-5: the exit contract holds even if a sqlite3.Error escapes
        unwrapped (today BrokerJournal.open wraps it)."""

        import sqlite3
        from unittest import mock

        from tools.host_control import __main__ as entry

        tree = self._tree()
        connect, logs = _Spy(), []
        with mock.patch.object(entry.BrokerJournal, "open", side_effect=sqlite3.OperationalError("locked")):
            code = entry.run(
                config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append
            )
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs[-1], "start refused: OperationalError")

    def test_registry_is_bound_to_the_framework_copy_and_plan_works(self) -> None:
        tree = self._tree()
        stack = self._build(tree)
        self.assertEqual(stack.registry._framework_root, Path(tree["framework"]))
        result = stack.plan_service.plan(UID, "recovery", "manifest-r1", "t2920-u2-plan")
        self.assertTrue(result.plan_handle)

    def test_startup_record_is_logged_and_digests_the_framework_copy(self) -> None:
        from tools.host_control.runtime_table import load_checkout_op_registry

        logs: list[str] = []
        tree = self._tree()
        stack = self._build(tree, logs)
        record_lines = [line for line in logs if line.startswith("startup-record ")]
        self.assertEqual(record_lines, [f"startup-record {stack.runtime.startup_record()}"])
        record = json.loads(record_lines[0].split(" ", 1)[1])
        self.assertEqual(record["schema"], "pantheon-broker-runtime-load/v1")
        self.assertEqual(
            record["op_registry_digest"], load_checkout_op_registry(Path(tree["framework"])).digest
        )
        self.assertIs(stack.plan_service.runtime, stack.runtime)

    def _context(self, stack):
        class FakeConnection:
            closed = False

            def close(self) -> None:
                self.closed = True

        outbound = FakeConnection()
        from tools.host_control import __main__ as entry

        factory = entry.driver_context_factory(
            config=stack.config, registry=stack.registry, connect=lambda main_loop: outbound
        )
        context = factory()
        self.addCleanup(context.close)
        return context, outbound

    def test_driver_context_reconciles_through_the_executor_probe(self) -> None:
        from tools.host_control.approval_client import ApprovalConsumerClient
        from tools.host_control.executor_client import BusReachability, DBusExecutorPort, ExecutorReconcileProbe
        from tools.host_control.reconciler import BrokerLiveStateReconciler

        stack = self._build(self._tree())
        context, outbound = self._context(stack)
        self.assertTrue(context.reconcile)
        self.assertFalse(context.compensate)
        self.assertIsInstance(context.port, DBusExecutorPort)
        reconciler = context.service._reconciler
        self.assertIsInstance(reconciler, BrokerLiveStateReconciler)
        self.assertIsInstance(reconciler._probe, ExecutorReconcileProbe)
        self.assertIs(reconciler._probe._port, context.port)
        self.assertIsInstance(context.service._binder, ApprovalConsumerClient)
        self.assertIsInstance(context.reachability, BusReachability)
        self.assertEqual(context.reachability._approval_uid, stack.config.approval_uid)
        self.assertIsNot(context.journal, stack.journal)  # the driver thread's own handle
        context.close()
        self.assertTrue(outbound.closed)

    def test_compensate_follows_the_config(self) -> None:
        stack = self._build(self._tree(compensate=True))
        context, _ = self._context(stack)
        self.assertTrue(context.compensate)
        self.assertTrue(context.reconcile)

    def test_main_loop_service_never_binds_or_reconciles(self) -> None:
        from tools.host_control.contracts import ApprovalUnavailable, RecoveryRefused

        stack = self._build(self._tree())
        with self.assertRaises(ApprovalUnavailable):
            stack.operation_service._binder.validate_and_bind("a", "b", "c", "d")
        with self.assertRaises(RecoveryRefused):
            stack.operation_service._reconciler.reconcile(None, None, ())

    def test_health_legs_are_wired_to_the_driver(self) -> None:
        stack = self._build(self._tree())
        snapshot = stack.health.snapshot()
        legs = {item.name: item for item in snapshot.subcomponents}
        for name, item in legs.items():
            self.assertNotIn("not wired", item.reason, name)
        # built, not started: the driver leg says so (degraded, never a crash)
        self.assertEqual(legs["operation_driver"].status, "degraded")
        self.assertEqual(legs["operation_driver"].reason, "operation driver not started")
        self.assertEqual(legs["executor_reachability"].status, "degraded")
        self.assertEqual(legs["journal"].status, "ready")
        self.assertEqual(legs["policy"].revision, "policy-r1")
        self.assertEqual(stack.plan_service.capabilities(UID).health, "degraded")

    def test_gate36_protects_the_configured_trusted_and_framework_roots(self) -> None:
        tree = self._tree()
        stack = self._build(tree)
        roots = stack.plan_service._protected_roots
        self.assertIn(tree["trusted"], roots)
        self.assertIn(tree["framework"], roots)
        self.assertIn("/usr/local/lib/pantheon", roots)
        self.assertIn("/var/lib/pantheon-host-approval-telegram", roots)


APPROVAL_PACKAGE = "tools.host_control.approval"


def approval_package_imports(source: str, package: str = "tools.host_control") -> set[str]:
    """Every spelling in ``source`` (a module of ``package``) that binds the
    approval package or a submodule of it: ``import tools.host_control.approval``,
    ``from tools.host_control.approval[.x] import ...``, ``from
    tools.host_control import approval`` (an alias of the parent package, the
    T-2920 U2 review LOW-1 gap), and the relative forms ``from . import
    approval`` / ``from .approval import ...``. ``approval_client`` (the
    broker-side client module) is a different NAME and is never flagged."""

    import ast

    def in_package(name: str) -> bool:
        return name == APPROVAL_PACKAGE or name.startswith(APPROVAL_PACKAGE + ".")

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if in_package(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if in_package(module):
                found.add(module)
                continue
            for alias in node.names:
                if in_package(f"{module}.{alias.name}"):
                    found.add(f"{module}.{alias.name}")
    return found


class BrokerEntrypointSeparationTests(unittest.TestCase):
    def test_broker_entrypoint_never_imports_the_approval_package(self) -> None:
        """Gate 18 broker half: the broker entrypoint reaches HostApproval1
        only through the broker-side ``approval_client`` (a module NAME that
        merely starts with "approval"), never the approval package. Checked
        here by package boundary, because the shared separation test's
        string-prefix check cannot tell ``approval_client`` from
        ``approval.*``."""

        path = Path(__file__).resolve().parent / "host_control" / "__main__.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("from tools.host_control.approval_client import", source)
        self.assertEqual(approval_package_imports(source), set())

    def test_the_check_flags_every_approval_package_spelling(self) -> None:
        flagged = {
            "from tools.host_control import approval\n": {"tools.host_control.approval"},
            "from tools.host_control import approval as a\n": {"tools.host_control.approval"},
            "from tools.host_control import wire, approval\n": {"tools.host_control.approval"},
            "import tools.host_control.approval\n": {"tools.host_control.approval"},
            "import tools.host_control.approval.service as s\n": {"tools.host_control.approval.service"},
            "from tools.host_control.approval import wire\n": {"tools.host_control.approval"},
            "from tools.host_control.approval.journal import ApprovalJournal\n": {
                "tools.host_control.approval.journal"
            },
            "from . import approval\n": {"tools.host_control.approval"},
            "from .approval import service\n": {"tools.host_control.approval"},
            "def f():\n    from tools.host_control import approval\n": {"tools.host_control.approval"},
        }
        for source, expected in flagged.items():
            with self.subTest(source=source):
                self.assertEqual(approval_package_imports(source), expected)
        for clean in (
            "from tools.host_control.approval_client import ApprovalConsumerClient\n",
            "from tools.host_control import approval_client\n",
            "import tools.host_control.approval_source\n",
            "from . import approval_client\n",
        ):
            with self.subTest(clean=clean):
                self.assertEqual(approval_package_imports(clean), set())


class Gate36InstallerEnumRootsTests(unittest.TestCase):
    def test_installer_enum_files_are_dynamic_protected_roots(self) -> None:
        from tools.host_control.self_amendment import default_protected_roots

        repo = Path(__file__).resolve().parent.parent
        roots = default_protected_roots()
        self.assertIn(str(repo / "tools" / "persona_installer_contracts.py"), roots)
        self.assertIn(str(repo / "tools" / "persona_installer.py"), roots)


class ApprovalEntrypointTests(_TreeCase):
    def test_euid_zero_is_refused_before_any_file_is_read(self) -> None:
        from tools.host_control.approval import __main__ as entry

        loader, connect = _Spy(), _Spy()
        code = entry.run(
            config_path=Path("/nonexistent/config.json"),
            open_loader=loader,
            geteuid=lambda: 0,
            connect=connect,
            log=lambda _m: None,
        )
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual((loader.calls, connect.calls), ([], []))

    def test_a_policy_that_does_not_map_the_human_to_the_ingress_uid_is_refused(self) -> None:
        from tools.host_control.approval import __main__ as entry

        policy = fx.approval_policy_value(UID)
        policy["subjects"]["operator"] = UID + 7777  # not the configured ingress uid
        tree = fx.approval_tree(self.tmp / "approval", caller_uid=UID, policy=policy)
        connect, logs = _Spy(), []
        code = entry.run(config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append)
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs, ["start refused: HostControlError"])

    def test_build_wires_the_registration_and_an_async_presenter(self) -> None:
        from tools.host_control.approval import __main__ as entry
        from tools.host_control.approval.config import load_approval_config

        tree = fx.approval_tree(self.tmp / "approval", caller_uid=UID)
        config_path = Path(tree["config_path"])
        with _loader((config_path.parent,)) as bootstrap:
            config = load_approval_config(config_path, files=bootstrap)
        stack = entry.build_approval(
            config,
            _loader(config.trusted_roots),
            metadata_source=_Spy(),
            card_presenter=fx._RecordingOnlyPresenter(),
            delivery_scheduler=fx._NoTimers(),
        )
        self.addCleanup(stack.close)
        self.assertIs(stack.service._human_ingress, config.human_ingress)
        self.assertIsNotNone(stack.service._delivery)
        self.assertEqual(stack.service._consumer_uids, frozenset((UID + fx.BROKER_OFFSET,)))
        legs = {item.name: item.status for item in stack.health.snapshot().subcomponents}
        self.assertEqual(
            legs, {"journal": "ready", "approver_policy": "ready", "broker_metadata_reachability": "not_configured"}
        )

    def test_main_takes_no_arguments(self) -> None:
        from tools.host_control.approval import __main__ as entry

        self.assertEqual(entry.main(["x"]), 2)

    def test_a_corrupt_journal_is_refused_with_exit_78(self) -> None:
        from tools.host_control.approval import __main__ as entry

        tree = fx.approval_tree(self.tmp / "approval", caller_uid=UID)
        Path(tree["journal"]).write_bytes(b"not a sqlite database " * 64)
        connect, logs = _Spy(), []
        code = entry.run(config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append)
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs, ["start refused: HostControlError"])

    def test_a_raw_sqlite_error_at_journal_open_is_exit_78(self) -> None:
        import sqlite3
        from unittest import mock

        from tools.host_control.approval import __main__ as entry

        tree = fx.approval_tree(self.tmp / "approval", caller_uid=UID)
        connect, logs = _Spy(), []
        with mock.patch.object(entry.ApprovalJournal, "open", side_effect=sqlite3.DatabaseError("malformed")):
            code = entry.run(
                config_path=Path(tree["config_path"]), open_loader=_loader, connect=connect, log=logs.append
            )
        self.assertEqual(code, entry.EXIT_START_REFUSED)
        self.assertEqual(connect.calls, [])
        self.assertEqual(logs, ["start refused: DatabaseError"])


class _RecordingPresenter:
    """Async presenter that holds every attempt (in flight, no reply) and
    lets a test act at the moment a request is presented."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_present = None

    def begin_present(self, review_handle, *args, on_done) -> None:
        self.calls.append(review_handle)
        if self.on_present is not None:
            self.on_present(review_handle)


class ResumeAllHumanDeliveriesTests(unittest.TestCase):
    """The T-5518 obligation at daemon start: EVERY pending human request is
    re-armed. ``resume_human_deliveries`` re-reads one fixed first page (the
    state stays pending until an async reply is processed), so a loop over it
    never terminates with a full page; ``resume_all_human_deliveries`` walks a
    strictly advancing keyset cursor and stops on a short page."""

    COUNT = 7

    def setUp(self) -> None:
        from tools.test_host_control_telegram_ingress_fixtures import build_human_stack, human_plan

        self._tmp = tempfile.mkdtemp(prefix="t2920u2-resume-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.stack = build_human_stack(Path(self._tmp) / "stack", async_delivery=True)
        self.addCleanup(self.stack.close)
        self.presenter = _RecordingPresenter()
        self.stack.service._delivery._presenter = self.presenter
        self.requests: dict[str, str] = {}  # review handle -> request handle
        self.order: list[str] = []
        for index in range(self.COUNT):
            plan_handle = format(0x7100 + index, "064x")
            digest = format(0x9100 + index, "064x")
            self.stack.metadata.plans[plan_handle] = human_plan(digest)
            self.stack.metadata.summaries[plan_handle] = '{"effects":["r%d"]}' % index
            review = self.stack.open_review(plan_handle, digest)
            result = self.stack.service.request_human("specialist", review.review_handle, digest, f"k{index}")
            self.assertEqual(result.state, "pending")
            self.requests[review.review_handle] = result.request_handle
            # ties on created_at for pairs, distinct across pairs: the keyset
            # must order on (created_at, request_handle), not created_at alone
            if index % 2:
                self.stack.clock.advance(seconds=1)
        self.order = [
            handle
            for _created, handle in self.stack.journal.pending_human_request_page(
                self.stack.clock().isoformat(), limit=100
            )
        ]
        self.assertEqual(len(self.order), self.COUNT)

    def test_the_first_page_alone_repeats_forever(self) -> None:
        # regression anchor for the U2 finding: the same page every call
        self.assertEqual(self.stack.service.resume_human_deliveries(limit=3), 3)
        self.assertEqual(self.stack.service.resume_human_deliveries(limit=3), 3)
        self.assertEqual(len(self.presenter.calls), 3)

    def test_every_pending_request_is_re_armed_exactly_once(self) -> None:
        self.assertEqual(self.stack.service.resume_all_human_deliveries(page=3), self.COUNT)
        self.assertEqual(sorted(self.presenter.calls), sorted(self.requests))
        self.assertEqual(len(self.presenter.calls), self.COUNT)
        # idempotent: everything is in flight, nothing is doubled
        self.assertEqual(self.stack.service.resume_all_human_deliveries(page=3), self.COUNT)
        self.assertEqual(len(self.presenter.calls), self.COUNT)

    def test_an_exact_multiple_of_the_page_terminates(self) -> None:
        self.assertEqual(self.stack.service.resume_all_human_deliveries(page=self.COUNT), self.COUNT)
        self.assertEqual(self.stack.service.resume_all_human_deliveries(page=1), self.COUNT)
        self.assertEqual(len(self.presenter.calls), self.COUNT)

    def test_requests_resolved_mid_paging_are_neither_skipped_nor_doubled(self) -> None:
        class Allow:
            allowed = True

        service = self.stack.service
        service._authorizer.may_withdraw = lambda subject: Allow()
        by_request = {request: review for review, request in self.requests.items()}
        far_future = self.order[-2]  # on the last page
        withdrawn: list[str] = []

        def on_present(review_handle: str) -> None:
            # resolve the request being visited (shrinks every earlier page:
            # an OFFSET walk would now skip rows) and, once, a later one
            request = self.requests[review_handle]
            service.withdraw_human_request("admin", request)
            withdrawn.append(request)
            if far_future not in withdrawn:
                service.withdraw_human_request("admin", far_future)
                withdrawn.append(far_future)

        self.presenter.on_present = on_present
        visited = service.resume_all_human_deliveries(page=2)
        presented = [self.requests[review] for review in self.presenter.calls]
        self.assertEqual(visited, self.COUNT - 1)
        self.assertNotIn(far_future, presented)
        self.assertEqual(sorted(presented), sorted(set(self.order) - {far_future}))
        self.assertEqual(len(presented), len(set(presented)))
        self.assertEqual(presented, [handle for handle in self.order if handle != far_future])
        self.assertIn(by_request[far_future], self.requests)

    def test_page_size_must_be_positive(self) -> None:
        for page in (0, -1, True, "2"):
            with self.subTest(page=page), self.assertRaises(HostControlError):
                self.stack.service.resume_all_human_deliveries(page=page)

    def test_expired_requests_are_not_visited(self) -> None:
        self.stack.clock.advance(minutes=16)  # past the 15-minute card ttl
        self.assertEqual(self.stack.service.resume_all_human_deliveries(page=2), 0)
        self.assertEqual(self.presenter.calls, [])

    def test_journal_page_rejects_a_malformed_cursor(self) -> None:
        now = self.stack.clock().isoformat()
        for cursor in (("a",), ["a", "b"], ("a", 1)):
            with self.subTest(cursor=cursor), self.assertRaises(HostControlError):
                self.stack.journal.pending_human_request_page(now, limit=2, after=cursor)


if __name__ == "__main__":
    unittest.main()
