"""Tests for the ORG-068 Health1 value contract (T-2914)."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from pathlib import Path

from tools.host_control.contracts import HostControlError
from tools.host_control.health import (
    APPROVAL_ROSTER,
    BROKER_ROSTER,
    CONTRACT_REVISION,
    HEALTH_PROPERTIES,
    HealthProbeResult,
    HealthProvider,
    HealthSnapshot,
    SubcomponentHealth,
    constant_probe,
    make_approval_health_provider,
    make_broker_health_provider,
    serialize_health,
    serialized_changed_properties,
)
from tools.host_control import health_dbus

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"
APPROVAL_PACKAGE = HOST_CONTROL / "approval"


def _ready_probe(revision: str = "rev-ready"):
    return constant_probe(status="ready", revision=revision)


def _provider(status: str, reason: str = "not ready") -> HealthProvider:
    return make_broker_health_provider(
        journal_probe=constant_probe(
            status=status, reason="" if status == "ready" else reason,
            revision="journal-r1",
        ),
        registry_probe=_ready_probe("registry-r1"),
        policy_probe=_ready_probe("policy-r1"),
        approval_reachability_probe=_ready_probe("approval-r1"),
        executor_reachability_probe=_ready_probe("executor-r1"),
    )


class ProviderRosterTests(unittest.TestCase):
    def test_provider_requires_exact_closed_roster_for_each_service(self) -> None:
        broker = {name: _ready_probe(name + "-r1") for name in BROKER_ROSTER}
        approval = {name: _ready_probe(name + "-r1") for name in APPROVAL_ROSTER}
        self.assertEqual(HealthProvider(service="broker", probes=broker).roster, BROKER_ROSTER)
        self.assertEqual(
            HealthProvider(service="approval", probes=approval).roster,
            APPROVAL_ROSTER,
        )
        for service, probes in (("broker", broker), ("approval", approval)):
            missing = dict(probes)
            missing.pop(next(iter(missing)))
            with self.subTest(service=service, shape="missing"):
                with self.assertRaises(HostControlError):
                    HealthProvider(service=service, probes=missing)
            extra = dict(probes)
            extra["extra"] = _ready_probe("extra-r1")
            with self.subTest(service=service, shape="extra"):
                with self.assertRaises(HostControlError):
                    HealthProvider(service=service, probes=extra)


class OverallDerivationTests(unittest.TestCase):
    def test_not_configured_remains_machine_legible_and_degrades_overall(self) -> None:
        provider = make_broker_health_provider(
            journal_probe=_ready_probe("journal-r1"),
            registry_probe=_ready_probe("registry-r1"),
            policy_probe=_ready_probe("policy-r1"),
        )
        snapshot = provider.snapshot()
        payload = serialize_health(snapshot)
        self.assertEqual(snapshot.status, "degraded")
        self.assertEqual(
            payload["Subcomponents"]["approval_reachability"],
            "not_configured",
        )
        self.assertEqual(
            payload["Subcomponents"]["executor_reachability"],
            "not_configured",
        )
        self.assertIn("T-2917", payload["Reasons"]["approval_reachability"])


class CapabilitiesDownMapTests(unittest.TestCase):
    def test_unhealthy_health1_downmaps_to_degraded_capabilities(self) -> None:
        provider = _provider("unhealthy", "journal failed")
        self.assertEqual(provider.snapshot().status, "unhealthy")
        self.assertEqual(provider.capabilities_health(), "degraded")
        self.assertEqual(_provider("degraded", "journal slow").capabilities_health(), "degraded")
        self.assertEqual(_provider("ready").capabilities_health(), "ready")


class ProbeFailureTests(unittest.TestCase):
    def test_raising_probe_returns_unhealthy_without_exception_leakage(self) -> None:
        def raising_probe() -> HealthProbeResult:
            raise RuntimeError("SECRET-SENTINEL /root/token")

        provider = make_broker_health_provider(
            journal_probe=raising_probe,
            registry_probe=_ready_probe("registry-r1"),
            policy_probe=_ready_probe("policy-r1"),
            approval_reachability_probe=_ready_probe("approval-r1"),
            executor_reachability_probe=_ready_probe("executor-r1"),
        )
        payload = serialize_health(provider.snapshot())
        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(provider.snapshot().status, "unhealthy")
        self.assertEqual(payload["Subcomponents"]["journal"], "unhealthy")
        self.assertEqual(payload["Reasons"]["journal"], "probe failed")
        self.assertEqual(provider.capabilities_health(), "degraded")
        self.assertNotIn("SECRET-SENTINEL", encoded)
        self.assertNotIn("/root/token", encoded)


class SnapshotBoundsTests(unittest.TestCase):
    def test_reasons_and_revisions_are_bounded(self) -> None:
        ready = SubcomponentHealth("journal", "ready", "", "journal-r1")
        serialize_health(
            HealthSnapshot(
                "approval",
                CONTRACT_REVISION,
                "degraded",
                (
                    ready,
                    SubcomponentHealth(
                        "approver_policy", "ready", "", "policy-r1"
                    ),
                    SubcomponentHealth(
                        "broker_metadata_reachability",
                        "degraded",
                        "slow",
                        "broker-r1",
                    ),
                ),
            )
        )

        def approval_snapshot(component: SubcomponentHealth) -> HealthSnapshot:
            return HealthSnapshot(
                "approval",
                CONTRACT_REVISION,
                "degraded",
                (
                    component,
                    SubcomponentHealth(
                        "approver_policy", "ready", "", "policy-r1"
                    ),
                    SubcomponentHealth(
                        "broker_metadata_reachability",
                        "ready",
                        "",
                        "broker-r1",
                    ),
                ),
            )

        invalid = (
            SubcomponentHealth("journal", "ready", "not empty", "journal-r1"),
            SubcomponentHealth("journal", "degraded", "", "journal-r1"),
            SubcomponentHealth("journal", "degraded", "x" * 201, "journal-r1"),
            SubcomponentHealth("journal", "degraded", "bad\n", "journal-r1"),
            SubcomponentHealth("journal", "degraded", "slow", ""),
            SubcomponentHealth("journal", "degraded", "slow", "x" * 129),
        )
        for component in invalid:
            with self.subTest(component=component):
                with self.assertRaises(HostControlError):
                    serialize_health(approval_snapshot(component))
        with self.assertRaises(HostControlError):
            serialize_health(
                HealthSnapshot(
                    "approval",
                    CONTRACT_REVISION,
                    "ready",
                    (
                        SubcomponentHealth("journal", "ready", "", "journal-r1"),
                        SubcomponentHealth("extra", "ready", "", "extra-r1"),
                        SubcomponentHealth(
                            "broker_metadata_reachability",
                            "ready",
                            "",
                            "broker-r1",
                        ),
                    ),
                )
            )


class StatelessProviderTests(unittest.TestCase):
    def test_provider_and_emitter_retain_no_session_snapshot(self) -> None:
        state = {"status": "ready", "reason": ""}

        def probe() -> HealthProbeResult:
            return HealthProbeResult(
                status=state["status"],
                reason=state["reason"],
                revision="journal-r1",
            )

        provider = make_broker_health_provider(
            journal_probe=probe,
            registry_probe=_ready_probe("registry-r1"),
            policy_probe=_ready_probe("policy-r1"),
            approval_reachability_probe=_ready_probe("approval-r1"),
            executor_reachability_probe=_ready_probe("executor-r1"),
        )
        first = provider.snapshot()
        state.update(status="unhealthy", reason="journal failed")
        second = provider.snapshot()
        changed = serialized_changed_properties(second, first)
        self.assertEqual(first.status, "ready")
        self.assertEqual(second.status, "unhealthy")
        self.assertIn("Status", changed)
        self.assertNotIn("snapshot", provider.__dict__)
        if health_dbus.DBUS_AVAILABLE:
            source = inspect.getsource(health_dbus.Health1Object.__init__)
            self.assertNotIn("_previous", source)
            self.assertNotIn("_snapshot", source)


class SerializationShapeTests(unittest.TestCase):
    def test_serializer_emits_exact_five_health_properties(self) -> None:
        payload = serialize_health(_provider("ready").snapshot())
        self.assertEqual(tuple(payload), HEALTH_PROPERTIES)
        self.assertIsInstance(payload["ContractRevision"], str)
        self.assertIsInstance(payload["Status"], str)
        for name in ("Subcomponents", "Reasons", "Revisions"):
            self.assertIsInstance(payload[name], dict)
            self.assertTrue(all(isinstance(k, str) for k in payload[name]))
            self.assertTrue(all(isinstance(v, str) for v in payload[name].values()))
        forged = HealthSnapshot(
            "approval",
            CONTRACT_REVISION,
            "ready",
            (
                SubcomponentHealth("journal", "ready", "", "journal-r1"),
                SubcomponentHealth("extra", "ready", "", "extra-r1"),
                SubcomponentHealth(
                    "broker_metadata_reachability",
                    "ready",
                    "",
                    "broker-r1",
                ),
            ),
        )
        with self.assertRaises(HostControlError):
            serialize_health(forged)


class BrokerPackageInventoryTests(unittest.TestCase):
    def test_broker_package_inventory_is_exact_sorted_glob(self) -> None:
        # Protects the T-2911/T-2912 approval-package separation hole:
        # root broker modules are allow-by-omission, so package growth is
        # pinned here without re-architecting the separation logic.
        observed = sorted(path.name for path in HOST_CONTROL.glob("*.py"))
        self.assertEqual(
            observed,
            [
                "__init__.py",
                "approval_source.py",
                "contracts.py",
                "dbus_service.py",
                "health.py",
                "health_dbus.py",
                "journal.py",
                "operations.py",
                "planner.py",
                "policy.py",
                "registry.py",
                "trusted_files.py",
                "wire.py",
            ],
        )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class HealthModuleStructureTests(unittest.TestCase):
    def test_health_modules_preserve_service_isolation(self) -> None:
        for path in (HOST_CONTROL / "health.py", HOST_CONTROL / "health_dbus.py"):
            with self.subTest(module=path.name):
                imports = _imports(path)
                self.assertFalse(
                    any(name.startswith("tools.host_control.approval") for name in imports)
                )
        for path in sorted(APPROVAL_PACKAGE.glob("*.py")):
            with self.subTest(module=path.name):
                imports = _imports(path)
                self.assertNotIn("tools.host_control.health", imports)
                self.assertNotIn("tools.host_control.health_dbus", imports)


class HonestUnavailableBusTests(unittest.TestCase):
    def test_unconfigured_legs_report_not_configured_not_ready(self) -> None:
        broker = make_broker_health_provider(
            journal_probe=_ready_probe("journal-r1"),
            registry_probe=_ready_probe("registry-r1"),
            policy_probe=_ready_probe("policy-r1"),
        )
        payload = serialize_health(broker.snapshot())
        self.assertEqual(payload["Status"], "degraded")
        self.assertEqual(
            payload["Subcomponents"]["approval_reachability"],
            "not_configured",
        )
        self.assertEqual(
            payload["Subcomponents"]["executor_reachability"],
            "not_configured",
        )
        self.assertNotEqual(payload["Status"], "ready")

        approval = make_approval_health_provider(
            journal_probe=_ready_probe("approval-journal-r1"),
            approver_policy_probe=constant_probe(
                status="not_configured",
                reason="approver policy is not configured until T-2920",
                revision="T-2920",
            ),
            broker_metadata_reachability_probe=_ready_probe("broker-r1"),
        )
        approval_payload = serialize_health(approval.snapshot())
        self.assertEqual(approval_payload["Status"], "degraded")
        self.assertEqual(
            approval_payload["Subcomponents"]["approver_policy"],
            "not_configured",
        )
