"""T-2918: ORG-052 privileged-slice gates at the broker, adapter and helper tiers.

* F1 / ORG-082 §2: ``Plan`` refuses a substrate family with
  ``SubstrateFamilyNotExposed`` BEFORE partition (it never becomes residue),
  before any handle is minted or anything is journaled.
* E17: no entry point (broker D-Bus methods, host MCP adapter, root helper)
  accepts shell, argv, executable, path, content, env, sudo target, uid,
  group, unit/policy/sudoers text, helper op or credential value.
* E19 (structure): the broker unit is unprivileged; the root helper package
  imports neither journal.
* E36: plans targeting broker self-amendment surfaces are refused at ``Plan``.

Runs everywhere (CI included): no real host path is read for writing; the
disposable-host side of these gates is test_gate_acceptance_sandbox.py.
"""

from __future__ import annotations

import ast
import configparser
import dataclasses
import unittest
from pathlib import Path

import pytest

from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.host_substrate import ports as P
from tools.host_control import wire
from tools.host_control.contracts import SubstrateFamilyNotExposed
from tools.host_control.envelope import ORG082_SUBSTRATE_FAMILIES
from tools.host_control.self_amendment import (
    STATIC_PROTECTED_ROOTS,
    SelfAmendmentRefused,
    default_protected_roots,
    refuse_self_amendment,
)
from tools.host_mcp_adapter import contract as adapter
from tools._host_control_plan_fixtures import registered_plan_transform
from tools.test_host_control_core import HostControlCase, _plan_replace

REPO = Path(__file__).resolve().parent.parent

#: Every value class ORG-052 gate 17 says no caller may supply, as the field
#: names a caller would try.
FORBIDDEN_FIELDS = (
    "shell", "command", "cmd", "argv", "args", "executable", "exe", "path", "target",
    "content", "body", "data", "env", "environment", "sudo_target", "runas", "user",
    "uid", "gid", "group", "unit", "unit_text", "policy", "policy_text", "sudoers",
    "sudoers_text", "operation", "op", "helper_op", "family", "credential", "token",
    "password", "secret",
)

#: The handle/id names the broker's public surface is allowed to take.
CLOSED_HANDLE_PARAMS = frozenset(
    (
        "seat_id", "manifest_revision", "idempotency_key", "plan_handle", "plan_digest",
        "approval_handle", "receipt_handle", "operation_handle", "expected_digest",
        "expected_operation_digest",
    )
)


def _injecting(extra):
    """Appends crafted operations to the REAL registered plan, so the plan
    composer sees what a hostile or buggy installer revision would emit (T-5533:
    the broker reads the root-registered plan; was a wrapped installer)."""

    def inject(plan):
        template = next(op for op in plan.operations if op.kind == "atomic-write")
        added = tuple(
            dataclasses.replace(template, operation_id=f"t2918-{index}", kind=kind, target=target)
            for index, (kind, target) in enumerate(extra)
        )
        return _plan_replace(plan, operations=plan.operations + added)

    return inject


class PlanComposerGateTests(HostControlCase):
    def build_service(self, *, protected_roots=(), **kwargs):
        from datetime import timedelta

        from tools.host_control.planner import HostPlanService
        from tools.test_host_control_core import FIXED_NOW

        if not protected_roots:
            return super().build_service(**kwargs)
        return HostPlanService(
            registry=self.registry, policy=self.policy, journal=self.journal,
            clock=lambda: FIXED_NOW, handle_factory=lambda: next(self.handles), plan_ttl=timedelta(hours=1),
            contract_version="org.pantheon.HostBroker1/v1", broker_revision="unit-test",
            host_operation_families=frozenset(("persona.install", "persona.verify")),
            health_check=lambda: "ready", protected_roots=protected_roots,
        )

    def plan_with(self, extra, **service_kwargs):
        service = self.build_service(**service_kwargs)
        with registered_plan_transform(_injecting(extra)):
            return service.plan(1003, "recovery", "manifest-r1", "t2918-request")

    def test_baseline_plan_still_composes(self) -> None:
        self.assertTrue(self.plan().plan_handle)

    def test_every_substrate_family_is_refused_at_plan_before_partition(self) -> None:
        for family in sorted(ORG082_SUBSTRATE_FAMILIES):
            with self.subTest(family=family):
                before = self.row_count()
                with self.assertRaises(SubstrateFamilyNotExposed) as info:
                    self.plan_with([(family, "/projection/home/claude-fallback/x")])
                self.assertEqual(info.exception.family, family)
                self.assertEqual(info.exception.manifest_revision, "manifest-r1")
                self.assertEqual(wire.error_name_for(info.exception), wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR)
                wire.validate_org082_error(wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR, wire.error_body_for(info.exception))
                self.assertEqual(self.row_count(), before)  # nothing journaled

    def test_a_non_substrate_out_of_envelope_kind_is_still_seat_local_residue(self) -> None:
        result = self.plan_with([("seat-periodic-unit-install", "/projection/home/claude-fallback/.config/x")])
        self.assertIn("seat-periodic-unit-install", result.summary_json)

    def test_self_amendment_targets_are_refused_at_plan(self) -> None:
        surfaces = {
            "broker executable": "/usr/local/libexec/pantheon-host-broker",
            "approval executable": "/usr/local/libexec/pantheon-host-approval",
            "helper executable": "/usr/local/libexec/pantheon-host-substrate-helper",
            "broker unit": "/etc/systemd/system/pantheon-host-broker.service",
            "approval unit drop-in": "/etc/systemd/system/pantheon-host-approval.service.d/x.conf",
            "bus ownership rules": "/etc/dbus-1/system.d/org.pantheon.HostBroker1.conf",
            "root registry": "/etc/pantheon/host-broker/registry.json",
            "approver policy": "/etc/pantheon/host-approval/approver-policy.json",
            "human allowlist": "/etc/pantheon/host-approval/human-allowlist.json",
            "sudoers": "/etc/sudoers.d/pantheon-anything",
            "broker journal": "/var/lib/pantheon-host-broker/broker.sqlite3",
            "approval journal": "/var/lib/pantheon-host-approval/approval.sqlite3",
            "op enum (envelope)": str(REPO / "tools/host_control/envelope.py"),
            "op enum (registry)": str(REPO / "tools/germinate/op_registry.py"),
            # T-2920 U2 (E36): the ingress state, the framework install prefix
            # and the installer operation-enum files
            "ingress state": "/var/lib/pantheon-host-approval-telegram/offset.json",
            "framework copy": "/usr/local/lib/pantheon/framework/r1/tools/persona_installer.py",
            "framework active link": "/usr/local/lib/pantheon/framework/active",
            "op enum (installer contracts)": str(REPO / "tools/persona_installer_contracts.py"),
            "op enum (installer)": str(REPO / "tools/persona_installer.py"),
            "dot-dot escape": "/projection/home/claude-fallback/../../../etc/pantheon/x",
            "double-slash spelling": "//etc/pantheon/x",
            # review N4: non-normalized spellings must normalize INTO the protected root
            "inner double slash": "/etc//pantheon/x",
            "dot segment": "/etc/./pantheon/x",
            "trailing-slash dir": "/usr/local/libexec//pantheon-host-broker",
            "dot-slash unit": "/etc/systemd/./system/pantheon-host-broker.service",
            "triple slash": "///var/lib/pantheon-host-broker/broker.sqlite3",
        }
        for kind in ("atomic-write", "mkdir", "user-unit-install", "codex-standing-instructions-install"):
            for label, target in surfaces.items():
                with self.subTest(kind=kind, surface=label):
                    before = self.row_count()
                    with self.assertRaises(SelfAmendmentRefused) as info:
                        self.plan_with([(kind, target)])
                    self.assertEqual(wire.error_body_for(info.exception), "family_denied")  # no path leak
                    self.assertEqual(self.row_count(), before)

    def test_configured_trusted_roots_are_protected_too(self) -> None:
        """The registry/policy this broker loaded live under its trusted root."""

        with self.assertRaises(SelfAmendmentRefused):
            self.plan_with([("atomic-write", str(self.registry_path))], protected_roots=(str(self.root),))

    def test_read_only_diagnostics_may_name_protected_paths(self) -> None:
        refuse_self_amendment(
            [dataclasses.make_dataclass("Op", ["operation_id", "kind", "target"])("d", "source-check", "/etc/pantheon")],
            default_protected_roots(),
        )


def test_static_roots_cover_every_dist_unit_execstart_and_state_directory():
    """The protected set tracks the staged deployment artifacts."""

    roots = default_protected_roots()
    for unit in sorted((REPO / "tools/host_control/dist").glob("*.service")):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read_string("\n".join(line for line in unit.read_text().splitlines() if not line.startswith("#")))
        service = parser["Service"] if parser.has_section("Service") else {}
        execstart = service.get("ExecStart", "").split()[0] if service.get("ExecStart") else ""
        if execstart:
            assert any(execstart == r or execstart.startswith(r + "/") for r in roots), (unit.name, execstart)
        state = service.get("StateDirectory")
        if state and unit.name in (
            "pantheon-host-broker.service",
            "pantheon-host-approval.service",
            # T-2920 U2 (E36): the Telegram ingress state too
            "pantheon-host-approval-telegram-ingress.service",
        ):
            assert f"/var/lib/{state}" in STATIC_PROTECTED_ROOTS, (unit.name, state)


# --- E17: broker D-Bus surface ------------------------------------------------------


def _manager_methods():
    from tools.host_control import dbus_service

    if not dbus_service.DBUS_AVAILABLE:  # pragma: no cover - canonical host has dbus-python
        pytest.skip("dbus-python unavailable")
    methods = {}
    for name, member in vars(dbus_service.HostBroker1Object).items():
        if getattr(member, "_dbus_is_method", False):
            methods[name] = member
    return methods


def test_e17_the_broker_method_table_takes_only_closed_handles_and_ids():
    methods = _manager_methods()
    manager = {n: m for n, m in methods.items() if m._dbus_interface == wire.MANAGER_INTERFACE}
    assert {n: m._dbus_in_signature for n, m in manager.items()} == {
        "Capabilities": "", "Plan": "sss", "Apply": "ssss", "Verify": "sss",
        "Rollback": "sss", "OperationGet": "s", "ApprovalGet": "s",
    }
    for name, member in methods.items():
        params = set(member._dbus_args)
        assert params <= CLOSED_HANDLE_PARAMS, (name, params - CLOSED_HANDLE_PARAMS)
        assert not params & set(FORBIDDEN_FIELDS), name
        assert set(member._dbus_in_signature) <= {"s"}, name  # scalars only: no arrays, dicts, variants


class PlanArgumentShapeTests(HostControlCase):
    def test_e17_shell_and_path_shaped_plan_arguments_are_refused(self) -> None:
        from tools.host_control.contracts import HostControlError

        for seat_id, revision in (
            ("/bin/sh -c id", "manifest-r1"),
            ("recovery", "../../etc/sudoers"),
            ("recovery", "manifest-r1; rm"),
            ("recovery\x00", "manifest-r1"),
            ("recovery", "ALL=(ALL) NOPASSWD: ALL"),
        ):
            with self.subTest(seat_id=seat_id, revision=revision):
                with self.assertRaises(HostControlError):
                    self.service.plan(1003, seat_id, revision, "k-e17")
        self.assertEqual(self.row_count(), 0)


# --- E17: host MCP adapter ------------------------------------------------------------


@pytest.mark.parametrize("tool", adapter.TOOLS, ids=lambda t: t.name)
def test_e17_adapter_schemas_are_closed_handle_shapes(tool):
    schema = adapter.input_schema(tool)
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) <= CLOSED_HANDLE_PARAMS
    for prop in schema["properties"].values():
        assert prop["type"] == "string" and "pattern" in prop


@pytest.mark.parametrize("tool", adapter.TOOLS, ids=lambda t: t.name)
@pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
def test_e17_adapter_refuses_every_forbidden_field(tool, field):
    good = {name: ("a" * 64 if kind == "hex64" else "manifest-r1") for name, kind in tool.params}
    with pytest.raises(adapter.AdapterError) as info:
        adapter.validate_arguments(tool, {**good, field: "/bin/sh"})
    assert info.value.code == "invalid_arguments"


# --- E17: root helper -------------------------------------------------------------------

_M = {
    "schema": F.MANIFEST_SCHEMA,
    "groups": {"pmail": {"gid": 3001}},
    "accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "groups": ["pmail"]}},
    "installer_roots": ["/srv/pantheon"],
    "paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750"}},
    "mailbox_dirs": {"alice": {"path": "/srv/pantheon/mbox-alice", "owner": "alice", "group": "pmail", "mode": "2770"}},
    "units": {}, "dbus_policies": {},
    "sudoers_templates": {"alice-probe": {"id": "alice-probe", "path": "/etc/sudoers.d/pantheon-alice-probe",
                                          "grantee": "alice", "runas": "root",
                                          "commands": ["/usr/local/sbin/pantheon-probe status"],
                                          "ticket": "T-2918", "expiry": "2026-12-31"}},
}


def _valid_params(family: str) -> dict[str, str]:
    m = F.load_manifest(_M)
    t = m.section("sudoers_templates")["alice-probe"]
    sha = F.sha256_text(F.sudoers_text(t))
    return {
        "account": "alice", "group": "pmail", "state": {"group.member": "present", "unit.system.install": "present",
                                                         "unit.system.enable": "enabled"}.get(family, "present"),
        "path": "/srv/pantheon/shared", "template": "alice-probe", "sha256": sha, "unit": "x", "policy": "p",
        "mailbox": "alice",
    }


@pytest.mark.parametrize("family", sorted(F.FAMILIES))
@pytest.mark.parametrize("field", FORBIDDEN_FIELDS)
def test_e17_the_root_helper_refuses_every_forbidden_field_before_touching_the_port(family, field):
    port = P.SimulatedSubstratePort()
    helper = Hp.RootHelper(port, F.load_manifest(_M), Hp.BOOTSTRAP)
    params = {k: v for k, v in _valid_params(family).items() if k in F.PARAMS[family]}
    params[field] = "/bin/sh -c id"
    before = port.dump()
    with pytest.raises(Hp.HelperRefusal):
        helper.admit(family, params, Hp.ACTIVATE)
    assert port.dump() == before


# --- E19 (structure) ----------------------------------------------------------------------


def test_e19_the_root_helper_package_imports_neither_journal():
    pkg = REPO / "tools/germinate/host_substrate"
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                assert "journal" not in name and "approval" not in name, (path.name, name)


def test_e19_broker_and_approval_units_are_unprivileged_and_off_the_mailbox_group():
    for unit in ("pantheon-host-broker.service", "pantheon-host-approval.service"):
        text = (REPO / "tools/host_control/dist" / unit).read_text()
        lines = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
        user = next(line.split("=", 1)[1] for line in lines if line.startswith("User="))
        assert user not in ("root", "0") and user.startswith("pantheon-")
        assert "NoNewPrivileges=yes" in lines and "CapabilityBoundingSet=" in lines
        assert not any(line.startswith("SupplementaryGroups=") and "mailbox" in line for line in lines)
        assert not any(line.startswith("User=root") for line in lines)


# --- ORG-082 §2 at the host MCP adapter boundary ----------------------------------------


def test_the_adapter_maps_a_broker_substrate_refusal_with_its_payload():
    exc = SubstrateFamilyNotExposed("account.ensure", manifest_revision="manifest-r1")
    err = adapter.error_for_dbus_name(wire.error_name_for(exc), (wire.error_body_for(exc),))
    assert err.code == "substrate_family_not_exposed"
    import json

    assert json.loads(err.detail) == exc.payload()


@pytest.mark.parametrize(
    "body",
    [
        (),
        ("not json",),
        ('{"code":"SUBSTRATE_FAMILY_NOT_EXPOSED","amendment":"ORG-082","retry_context":"bootstrap-in-process-root"}',),
        ('{"code":"REGISTRY_DIGEST_MISMATCH","family":"x","amendment":"ORG-082","retry_context":"not-retryable"}',),
    ],
)
def test_the_adapter_refuses_a_malformed_org082_reply(body):
    assert adapter.error_for_dbus_name(wire.SUBSTRATE_FAMILY_NOT_EXPOSED_ERROR, body).code == "broker_contract_mismatch"
