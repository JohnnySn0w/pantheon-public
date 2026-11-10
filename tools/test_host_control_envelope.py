"""ORG-052 Unit 5a (T-2917): the closed per-seat executor envelope.

Covers T-2917 ruling i4 (out-of-section-8 installer kinds are refused at
plan time with family_denied, naming the refused kinds) and HO ruling
T-4861 (section 8a classification; the broker never applies harness
permission-posture changes).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import typing
import unittest
from pathlib import Path

from tools import persona_installer as _pim
from tools.host_control import envelope, wire
from tools.host_control.contracts import PolicyDenied
from tools.host_control.envelope import (
    EXECUTOR_CAPABILITY_CLASSES,
    EXECUTOR_KIND_CLASSES,
    ORG082_SUBSTRATE_FAMILIES,
    OUT_OF_ENVELOPE_KINDS,
    PERMISSION_POSTURE_KINDS,
    OutOfEnvelopeKinds,
    require_executor_envelope,
)
from tools import test_host_control_core as _core  # module import: never re-collect its TestCases
from tools._host_control_plan_fixtures import registered_plan_transform
from tools.host_control.envelope import LAUNCH_DEPENDENT_KINDS, LAUNCH_KINDS, MUST_RUN_BEFORE_LAUNCH_KINDS


REPO_ROOT = Path(__file__).resolve().parent.parent
ALL_INSTALLER_KINDS = frozenset(typing.get_args(_pim.OperationKind))
UNMAPPED_KINDS = (
    "codex-telegram-source-check",
    "codex-telegram-mcp-check",
    # T-2917 5c.2: codex-telegram-disabled-first is admitted (diagnostics.read
    # via the user-manager port, T-4861), so it is no longer unmapped
    "claude-mcp-server-registration",
    # T-5447: seat-core periodics are seat-local residue, never executor kinds
    "seat-periodic-unit-install",
    "seat-periodic-daemon-reload",
    "seat-periodic-timer-enable",
    "seat-periodic-timer-observe",
    # T-5448: controller bring-up + ho-resumer decommission, also seat-local residue
    "seat-service-enable-observe",
    "seat-service-decommission",
    # T-5454: shell-box containment wiring in the seat's own settings.json, seat-local too
    "seat-claude-settings-env",
    # T-5480: shell-box PreToolUse hook + trust in the seat's ~/.codex/config.toml, seat-local too
    "seat-codex-hook-config",
    # T-5474: first-launch workspace trust, seat-local too (T-5473's residue
    # quarantine is admitted as files.seat_root since T-5608)
    "seat-claude-workspace-trust",
)


class EnvelopeTableTests(unittest.TestCase):
    def test_table_is_a_single_frozen_constant(self) -> None:
        with self.assertRaises(TypeError):
            EXECUTOR_KIND_CLASSES["unit-fire"] = "x"  # type: ignore[index]
        self.assertEqual(
            EXECUTOR_CAPABILITY_CLASSES,
            frozenset(
                ("diagnostics.read", "files.seat_root", "user_systemd.activate", "mailbox.probe")
            ),
        )

    def test_every_installer_kind_is_admitted_or_explicitly_unmapped(self) -> None:
        admitted = frozenset(EXECUTOR_KIND_CLASSES)
        self.assertEqual(admitted & OUT_OF_ENVELOPE_KINDS, frozenset())
        # A new installer kind fails here until it is classified by effect.
        self.assertEqual(admitted | OUT_OF_ENVELOPE_KINDS, ALL_INSTALLER_KINDS)
        self.assertEqual(OUT_OF_ENVELOPE_KINDS, frozenset(UNMAPPED_KINDS))

    def test_t4861_section_8a_entries(self) -> None:
        expected = {
            "codex-standing-instructions-source-check": "diagnostics.read",
            "codex-standing-instructions-target-check": "diagnostics.read",
            "codex-standing-instructions-override-check": "diagnostics.read",
            "claude-mcp-server-check": "diagnostics.read",
            "codex-standing-instructions-install": "files.seat_root",
        }
        for kind, capability in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(EXECUTOR_KIND_CLASSES[kind], capability)

    def test_no_capability_class_or_kind_is_an_org082_substrate_family(self) -> None:
        self.assertEqual(EXECUTOR_CAPABILITY_CLASSES & ORG082_SUBSTRATE_FAMILIES, frozenset())
        self.assertEqual(frozenset(EXECUTOR_KIND_CLASSES) & ORG082_SUBSTRATE_FAMILIES, frozenset())

    def test_no_permission_posture_kind_is_admitted(self) -> None:
        self.assertTrue(PERMISSION_POSTURE_KINDS)
        self.assertEqual(frozenset(EXECUTOR_KIND_CLASSES) & PERMISSION_POSTURE_KINDS, frozenset())
        self.assertLessEqual(PERMISSION_POSTURE_KINDS, OUT_OF_ENVELOPE_KINDS)


class ResidueQuarantineAdmissionTests(unittest.TestCase):
    """T-5608 (HO ruling 2026-10-27): files.seat_root + declared inspection reads."""

    KIND = "seat-runtime-residue-quarantine"

    def test_quarantine_is_files_seat_root_and_not_out_of_envelope(self) -> None:
        self.assertEqual(EXECUTOR_KIND_CLASSES[self.KIND], "files.seat_root")
        self.assertNotIn(self.KIND, OUT_OF_ENVELOPE_KINDS)
        require_executor_envelope([self.KIND])

    def test_quarantine_is_not_permission_posture(self) -> None:
        # it moves runtime state; it changes no harness approval / MCP posture
        self.assertNotIn(self.KIND, PERMISSION_POSTURE_KINDS)

    def test_quarantine_still_must_run_before_every_launch(self) -> None:
        self.assertIn(self.KIND, MUST_RUN_BEFORE_LAUNCH_KINDS)

    def test_declared_reads_are_pinned_closed_and_read_only(self) -> None:
        from tools.host_control.envelope import (
            INSPECTION_PORT_METHODS,
            INSPECTION_UNIT_ROLES,
            KIND_INSPECTION_READS,
        )
        from tools.user_systemd import ReadPurpose

        with self.assertRaises(TypeError):
            KIND_INSPECTION_READS[self.KIND] = ()  # type: ignore[index]
        self.assertEqual(
            dict(KIND_INSPECTION_READS),
            {
                self.KIND: (
                    ("unit_file_enablement", "timer", ""),
                    ("read_unit", "controller", "service-observe"),
                    ("read_unit", "consume", "service-observe"),
                )
            },
        )
        self.assertEqual(INSPECTION_PORT_METHODS, {"unit_file_enablement", "read_unit"})
        self.assertEqual(INSPECTION_UNIT_ROLES, {"timer", "consume", "controller"})
        purposes = {purpose.value for purpose in ReadPurpose}
        for kind, reads in KIND_INSPECTION_READS.items():
            self.assertIn(kind, EXECUTOR_KIND_CLASSES)
            self.assertNotEqual(EXECUTOR_KIND_CLASSES[kind], "user_systemd.activate")
            hash(reads)
            for method, role, purpose in reads:
                self.assertIn(method, INSPECTION_PORT_METHODS)
                self.assertIn(role, INSPECTION_UNIT_ROLES)
                self.assertEqual(purpose == "", method == "unit_file_enablement")
                if purpose:
                    self.assertIn(purpose, purposes)

    def test_declared_reads_enter_the_op_registry_digest(self) -> None:
        from types import MappingProxyType
        from unittest import mock

        from tools.germinate import op_registry
        from tools.host_control.envelope import KIND_INSPECTION_READS

        loaded = op_registry.load_op_registry()
        self.assertEqual(
            loaded.entries[self.KIND].inspection_reads, KIND_INSPECTION_READS[self.KIND]
        )
        self.assertIn(b'"inspection_reads"', loaded.blob)
        for changed in (
            KIND_INSPECTION_READS[self.KIND][:-1],  # a read dropped
            KIND_INSPECTION_READS[self.KIND] + (("read_unit", "timer", "timer-elapse"),),  # added
        ):
            with self.subTest(reads=changed), mock.patch.object(
                op_registry, "KIND_INSPECTION_READS", MappingProxyType({self.KIND: changed})
            ):
                self.assertNotEqual(op_registry.load_op_registry().digest, loaded.digest)
        with mock.patch.object(
            op_registry, "KIND_INSPECTION_READS", MappingProxyType({"no-such-kind": ()})
        ), self.assertRaises(op_registry.OpRegistryError):
            op_registry.load_op_registry()

    def test_the_read_purpose_set_equals_user_systemd_read_purpose(self) -> None:
        # T-5635 (4): op_registry cannot import tools.user_systemd (it would widen the
        # pinned broker import closure), so the envelope carries the value set; pin it here
        from tools.host_control.envelope import INSPECTION_READ_PURPOSES
        from tools.user_systemd import ReadPurpose

        self.assertEqual(INSPECTION_READ_PURPOSES, {purpose.value for purpose in ReadPurpose} | {""})

    def test_a_malformed_declared_read_fails_the_registry_build(self) -> None:
        # T-5635 (4): method, role and purpose are validated at build time (P1)
        from types import MappingProxyType
        from unittest import mock

        from tools.germinate import op_registry

        good = ("read_unit", "controller", "service-observe")
        bad_reads = (
            ("bad method", (("write_unit", "controller", "service-observe"),)),
            ("bad role", (("read_unit", "operator", "service-observe"),)),
            ("bad purpose", (("read_unit", "controller", "no-such-purpose"),)),
            ("short triple", (("read_unit", "controller"),)),
            ("non-string", (("read_unit", "controller", None),)),
        )
        for label, reads in bad_reads:
            with self.subTest(label), mock.patch.object(
                op_registry, "KIND_INSPECTION_READS", MappingProxyType({self.KIND: (good,) + reads})
            ), self.assertRaises(op_registry.OpRegistryError):
                op_registry.load_op_registry()

    def test_kinds_without_declared_reads_keep_their_registry_bytes(self) -> None:
        from tools.germinate import op_registry

        for kind, entry in op_registry.load_op_registry().entries.items():
            if kind != self.KIND:
                self.assertNotIn("inspection_reads", entry.to_json_obj())


class HoResumerDecommissionKindTests(unittest.TestCase):
    """T-5633: the decommission is a must-run-before-launch kind and stays
    seat-local residue (never admitted)."""

    KIND = "seat-service-decommission"

    def test_decommission_must_run_before_every_launch(self) -> None:
        self.assertIn(self.KIND, MUST_RUN_BEFORE_LAUNCH_KINDS)

    def test_decommission_stays_out_of_the_envelope(self) -> None:
        self.assertIn(self.KIND, OUT_OF_ENVELOPE_KINDS)
        self.assertNotIn(self.KIND, EXECUTOR_KIND_CLASSES)
        with self.assertRaises(OutOfEnvelopeKinds):
            require_executor_envelope([self.KIND])


class EnvelopeRefusalTests(unittest.TestCase):
    def test_each_unmapped_kind_is_refused_naming_the_kind(self) -> None:
        for kind in UNMAPPED_KINDS:
            with self.subTest(kind=kind):
                with self.assertRaises(OutOfEnvelopeKinds) as caught:
                    require_executor_envelope(["mkdir", kind, "unit-fire"])
                exc = caught.exception
                self.assertIsInstance(exc, PolicyDenied)
                self.assertEqual(exc.reason_code, "family_denied")
                self.assertEqual(exc.refused_kinds, (kind,))
                self.assertIn(kind, exc.detail())
                self.assertEqual(wire.error_name_for(exc), wire.POLICY_DENIED_ERROR)
                self.assertEqual(wire.error_body_for(exc), exc.detail())

    def test_unknown_kind_fails_closed(self) -> None:
        with self.assertRaises(OutOfEnvelopeKinds) as caught:
            require_executor_envelope(["mkdir", "shell-exec"])
        self.assertEqual(caught.exception.refused_kinds, ("shell-exec",))

    def test_detail_is_bounded_and_sorted(self) -> None:
        many = [f"kind-{index:03d}-padding-padding" for index in range(50)]
        exc = OutOfEnvelopeKinds(reversed(many))
        self.assertEqual(list(exc.refused_kinds), sorted(many))
        self.assertLessEqual(len(exc.detail()), 200)
        self.assertLessEqual(len(wire.error_body_for(exc)), 200)
        self.assertTrue(exc.detail().startswith("family_denied: "))

    def test_admitted_only_plan_passes(self) -> None:
        require_executor_envelope(EXECUTOR_KIND_CLASSES)


class PlannerEnvelopeTests(unittest.TestCase):
    """The planner refuses before minting a handle or journaling anything."""

    setUp = _core.HostControlCase.setUp
    tearDown = _core.HostControlCase.tearDown
    build_service = _core.HostControlCase.build_service
    write_json = _core.HostControlCase.write_json
    registry_dict = _core.HostControlCase.registry_dict
    write_registry = _core.HostControlCase.write_registry
    policy_dict = _core.HostControlCase.policy_dict
    write_policy = _core.HostControlCase.write_policy

    def _plan_count(self) -> int:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    def test_fixture_plan_is_admitted(self) -> None:
        result = self.service.plan(1003, "recovery", "manifest-r1", "admitted")
        self.assertEqual(len(result.plan_handle), 64)
        self.assertEqual(self._plan_count(), 1)

    def test_each_unmapped_kind_is_partitioned_to_the_seat_local_residue(self) -> None:
        # T-2917 5b (HO ruling T-4861, superseding the 5a whole-plan
        # refusal): the broker plan holds only admitted kinds and each
        # non-admitted kind is NAMED in the digest-bound residue.
        # T-5533: inject into the REGISTERED plan the broker reads.
        for index, kind in enumerate(UNMAPPED_KINDS):
            with self.subTest(kind=kind):

                def injected(plan, _kind=kind):
                    extra = dataclasses.replace(
                        plan.operations[0],
                        operation_id=f"injected.{_kind}",
                        kind=_kind,
                    )
                    if _kind in MUST_RUN_BEFORE_LAUNCH_KINDS:
                        # T-5470: a must-run-before-launch kind is placed where a
                        # real plan carries it, ahead of the launches (appended
                        # last it is a launch-before-containment plan, refused).
                        return _core._plan_replace(plan, operations=(extra,) + plan.operations)
                    return _core._plan_replace(plan, operations=plan.operations + (extra,))

                with registered_plan_transform(injected):
                    result = self.service.plan(1003, "recovery", "manifest-r1", f"partitioned-{index}")
                summary = json.loads(result.summary_json)
                self.assertNotIn(kind, {op["kind"] for op in summary["operations"]})
                # T-5447: the baseline periodic residue is set aside; the injected
                # kind must still be the ONLY other residue entry. T-5470: a residue
                # must-run-before-launch kind also routes the plan's launches (and
                # their dependents) to the residue; those are set aside too.
                # (T-5473 puts the residue quarantine on every seat, so the fixture's
                # launches are routed whatever kind is injected.)
                # Only the in-envelope routed class is set aside, never an injected kind.
                routed = (LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS) & set(EXECUTOR_KIND_CLASSES)
                self.assertEqual(
                    [
                        r for r in summary["seat_local_residue"]
                        if not r["operation_id"].startswith(("periodic.", "service.", "harness.codex-shell-box"))  # T-5480: baseline op
                        and r["kind"] not in routed
                    ],
                    [{"operation_id": f"injected.{kind}", "kind": kind, "status": "pending-seat-local"}],
                )
                self.assertEqual(self._plan_count(), index + 1)


class PermissionPostureTests(unittest.TestCase):
    """HO ruling T-4861: no admitted kind's mutation can write a harness
    tool-approval table (Codex approval_mode / MCP server tables, Claude
    MCP registration)."""

    def _real_plans(self):
        manifest = _pim.Manifest.load(REPO_ROOT / "instance.yaml")
        installer = _pim.PersonaInstaller(REPO_ROOT)
        personas = sorted({role["persona"] for role in manifest.roles.values()})
        for persona in personas:
            for harness in (None, "claude", "codex"):
                try:
                    yield installer.plan(
                        manifest, persona, harness_override=harness, root=Path("/projection")
                    )
                except _pim.InstallerError:
                    continue

    @staticmethod
    def _writes_tool_approval(operation: _pim.InstallOperation) -> bool:
        desired = json.dumps(dict(operation.desired), sort_keys=True)
        name = Path(operation.target).name
        return (
            "approval_mode" in desired
            or "mcp_servers" in desired
            or name in ("config.toml", ".claude.json")
        )

    def test_no_admitted_mutation_targets_a_tool_approval_table(self) -> None:
        mutating = frozenset(("files.seat_root", "user_systemd.activate", "mailbox.probe"))
        admitted_seen: set[str] = set()
        readonly_touching: set[str] = set()
        posture_seen: set[str] = set()
        for plan in self._real_plans():
            for operation in plan.operations:
                capability = EXECUTOR_KIND_CLASSES.get(operation.kind)
                touches = self._writes_tool_approval(operation)
                if capability in mutating:
                    admitted_seen.add(operation.kind)
                    with self.subTest(op=operation.operation_id, kind=operation.kind):
                        self.assertFalse(touches)
                elif capability == "diagnostics.read":
                    if touches:
                        readonly_touching.add(operation.kind)
                elif touches:
                    posture_seen.add(operation.kind)
        # Anti-vacuity: the real plans exercise admitted file writers, and
        # the detector does fire on at least one real posture writer.
        self.assertIn("atomic-write", admitted_seen)
        self.assertIn("user-unit-install", admitted_seen)
        self.assertTrue(posture_seen)
        self.assertLessEqual(posture_seen, OUT_OF_ENVELOPE_KINDS)
        # The only admitted kind that so much as READS a tool-approval
        # table is the read-only Claude MCP audit; its no-write behavior is
        # proven against real bytes below.
        self.assertEqual(readonly_touching, {"claude-mcp-server-check"})

    def test_admitted_claude_mcp_check_never_writes_the_registration_file(self) -> None:
        plan = next(
            plan
            for plan in self._real_plans()
            if any(op.kind == "claude-mcp-server-check" for op in plan.operations)
        )
        operation = next(op for op in plan.operations if op.kind == "claude-mcp-server-check")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".claude.json"
            config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            before = (config.read_bytes(), config.stat().st_mtime_ns)
            desired = dict(operation.desired)
            for key, value in desired.items():
                if isinstance(value, str) and value.startswith("/"):
                    desired[key] = str(config) if value.endswith(".claude.json") else str(root)
            local = dataclasses.replace(operation, target=str(config), desired=desired)
            context = _pim.ExecutionContext("all-in-one", allowed_roots=(str(root),))
            mutation, evidence = _pim.LocalHostBackend().execute(local, context)
            self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), before)
            self.assertFalse(mutation.changed)
            self.assertIn("verdict", evidence.evidence)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [".claude.json"])


if __name__ == "__main__":
    unittest.main()
