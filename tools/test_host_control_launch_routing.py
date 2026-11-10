"""T-5470 partition routing (HO ruling 2026-10-26) and the effective-order
containment invariant.

Intent: no installer execution path launches a seat before its containment and
pre-launch ops have run. The envelope kind sets are unchanged (direct-fire,
unit-fire and timer-enable stay admitted kinds); ``partition_installer_plan``
routes a plan's launch ops, and the admitted ops that need a launch's effect,
to the seat-local residue when the plan carries a residue must-run-before-launch
op. The shared predicate (``planner.refuse_launch_before_containment`` over
``envelope.launch_order_violation``) then holds at registration (the producer)
and in ``HostPlanService.plan``. Pinned here:

(a) the real CLAUDE fixture plan routes its launches behind containment and
    passes, at the producer and through the live Plan() path;
(b) T-5608: the REAL codex plan (its residue quarantine admitted as
    files.seat_root) routes nothing: direct-fire, unit-fire, timer-enable and
    timer-observe stay ADMITTED, behind the broker-run quarantine;
(c) a codex plan with a residue must-run-before-launch op (synthetic) still
    routes its launches to the residue and passes; every real plan is within
    both bounds;
(d) a launch before containment, and a claude launch without containment, are
    refused at the producer AND by Plan();
(f) routing only ever moves admitted ops to the residue, never the reverse.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import host_plan_producer as HPP
from tools import persona_installer as PI
from tools import test_host_control_core as _core  # module import: never re-collect its TestCases
from tools._host_control_plan_fixtures import register_plan
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    LAUNCH_DEPENDENT_KINDS,
    LAUNCH_KINDS,
    MUST_RUN_BEFORE_LAUNCH_KINDS,
    LaunchBeforeContainment,
    launch_order_violation,
)
from tools.host_control.planner import (
    effective_broker_order,
    launch_routed_operation_ids,
    partition_installer_plan,
    refuse_launch_before_containment,
    residue_bucket_counts,
)
from tools.host_control.registry import SeatRegistry
from tools.test_host_control_core import REPO_ROOT, _manifest_dict, _plan_replace

ROUTED_KINDS = ("direct-fire", "unit-fire", "timer-enable", "timer-observe")
QUARANTINE = "seat-runtime-residue-quarantine"


def _plan(harness: str) -> PI.InstallPlan:
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "instance.yaml"
        path.write_text(yaml.safe_dump(_manifest_dict(), sort_keys=False), encoding="utf-8")
        return PI.PersonaInstaller(REPO_ROOT).plan(
            PI.Manifest.load(path), "recovery", harness_override=harness, root=Path("/projection")
        )


def _kinds(operations) -> list[str]:
    return [op.kind for op in operations]


def _with_residue_must_run(plan: PI.InstallPlan) -> PI.InstallPlan:
    """A synthetic residue must-run-before-launch op (a workspace-trust kind,
    out of the envelope), placed before the first fire."""
    template = next(op for op in plan.operations if op.kind == "atomic-write")
    extra = dataclasses.replace(
        template, operation_id="synthetic.workspace-trust", kind="seat-claude-workspace-trust"
    )
    index = next(i for i, op in enumerate(plan.operations) if op.kind == "direct-fire")
    operations = plan.operations[:index] + (extra,) + plan.operations[index:]
    return _plan_replace(plan, operations=operations)


def _move_after(plan: PI.InstallPlan, kind: str, anchor_id: str) -> PI.InstallPlan:
    ops = list(plan.operations)
    moved = next(op for op in ops if op.kind == kind)
    ops.remove(moved)
    ops.insert(next(i for i, op in enumerate(ops) if op.operation_id == anchor_id) + 1, moved)
    return _plan_replace(plan, operations=tuple(ops))


def _without(plan: PI.InstallPlan, kind: str) -> PI.InstallPlan:
    return _plan_replace(plan, operations=tuple(op for op in plan.operations if op.kind != kind))


class PartitionRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.claude = _plan("claude")
        cls.codex = _plan("codex")

    def test_routed_kind_set_is_pinned(self) -> None:
        # growing the routed class is a visible edit (HO ruling 2026-10-26)
        self.assertEqual(
            LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS,
            {"direct-fire", "unit-fire", "timer-enable", "seat-service-enable-observe", "timer-observe"},
        )

    def test_kind_sets_are_the_ruled_ones(self) -> None:
        self.assertEqual(LAUNCH_KINDS, {"direct-fire", "unit-fire", "timer-enable", "seat-service-enable-observe"})
        self.assertEqual(
            MUST_RUN_BEFORE_LAUNCH_KINDS,
            {
                "seat-claude-settings-env",
                "seat-runtime-residue-quarantine",
                "seat-claude-workspace-trust",
                "seat-service-decommission",  # T-5633
            },
        )
        self.assertEqual(LAUNCH_DEPENDENT_KINDS, {"timer-observe"})
        # the envelope itself is unchanged: the fires stay admitted kinds
        for kind in ("direct-fire", "unit-fire", "timer-enable", "timer-observe"):
            self.assertIn(kind, EXECUTOR_KIND_CLASSES)

    def test_a_real_claude_plan_routes_its_launches_behind_containment(self) -> None:
        broker, residue = partition_installer_plan(self.claude)
        for kind in ROUTED_KINDS:
            self.assertNotIn(kind, _kinds(broker.operations))
            self.assertIn(kind, _kinds(residue))
        # T-5608: exactly the 4 launch-class ops route (settings-env and
        # workspace-trust stay residue); the quarantine is broker-run, ahead
        # of every routed launch in the effective order
        self.assertEqual(
            launch_routed_operation_ids(self.claude),
            {"harness.direct-fire", "connective.unit-fire", "connective.timer-enable", "connective.timer-observe"},
        )
        self.assertIn(QUARANTINE, _kinds(broker.operations))
        self.assertNotIn(QUARANTINE, _kinds(residue))
        order = _kinds(effective_broker_order(self.claude))
        self.assertLess(order.index(QUARANTINE), min(order.index(kind) for kind in LAUNCH_KINDS if kind in order))
        containment = order.index("seat-claude-settings-env")
        self.assertLess(containment, min(order.index(kind) for kind in LAUNCH_KINDS if kind in order))
        self.assertIsNone(
            launch_order_violation(
                [(op.operation_id, op.kind) for op in effective_broker_order(self.claude)],
                selected_harness="claude",
            )
        )
        refuse_launch_before_containment(self.claude)
        # routed launches keep raw plan order within the residue
        raw = [op.operation_id for op in self.claude.operations if op.operation_id in {o.operation_id for o in residue}]
        self.assertEqual([op.operation_id for op in residue], raw)
        # the routed launches are named as seat-local operator actions
        actions = {action.action_id for action in broker.operator_actions}
        self.assertIn("seat-local.harness.direct-fire", actions)
        self.assertIn("seat-local.connective.unit-fire", actions)

    def test_the_real_codex_plan_routes_nothing(self) -> None:
        # T-5608: with the residue quarantine admitted (files.seat_root), the
        # REAL codex plan has no residue must-run-before-launch op, so its fires
        # stay broker-executed (E40 reachable) and the quarantine runs in the
        # broker plan ahead of them.
        self.assertIn(QUARANTINE, _kinds(self.codex.operations))
        self.assertEqual(launch_routed_operation_ids(self.codex), frozenset())
        broker, residue = partition_installer_plan(self.codex)
        admitted = _kinds(broker.operations)
        self.assertIn(QUARANTINE, admitted)
        self.assertNotIn(QUARANTINE, _kinds(residue))
        for kind in ROUTED_KINDS:
            self.assertIn(kind, admitted)
            self.assertNotIn(kind, _kinds(residue))
        self.assertLess(admitted.index(QUARANTINE), admitted.index("direct-fire"))
        self.assertLess(admitted.index(QUARANTINE), admitted.index("unit-fire"))
        refuse_launch_before_containment(self.codex)

    def test_a_codex_plan_with_a_residue_must_run_op_routes_its_launches(self) -> None:
        # the T-5470 rule is unchanged: a residue must-run-before-launch op on
        # a codex plan still routes its launches behind it (synthetic op)
        plan = _with_residue_must_run(self.codex)
        broker, residue = partition_installer_plan(plan)
        for kind in ROUTED_KINDS:
            self.assertNotIn(kind, _kinds(broker.operations))
            self.assertIn(kind, _kinds(residue))
        order = _kinds(effective_broker_order(plan))
        self.assertLess(order.index("seat-claude-workspace-trust"), order.index("direct-fire"))
        self.assertLess(order.index(QUARANTINE), order.index("direct-fire"))
        refuse_launch_before_containment(plan)

    def test_every_real_plan_is_within_both_bounds(self) -> None:
        from tools.host_control.envelope import require_routed_launch_bound
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT

        for name, plan in (("claude", self.claude), ("codex", self.codex)):
            with self.subTest(plan=name):
                broker, residue = partition_installer_plan(plan)
                routed = launch_routed_operation_ids(plan)
                self.assertLessEqual(
                    # T-5961 (orchestrator ruling on B8): the general bucket; periodic-timer ops are separate
                    residue_bucket_counts((op.kind, op.target) for op in residue if op.operation_id not in routed)[0],
                    SEAT_LOCAL_RESIDUE_LIMIT,
                )
                require_routed_launch_bound(op.kind for op in residue if op.operation_id in routed)

    def test_unknown_residue_ops_are_ignored(self) -> None:
        template = next(op for op in self.claude.operations if op.kind == "atomic-write")
        extra = dataclasses.replace(template, operation_id="future.thing", kind="seat-future-thing")
        plan = _plan_replace(self.claude, operations=self.claude.operations + (extra,))
        refuse_launch_before_containment(plan)

    def test_launch_before_containment_is_refused(self) -> None:
        plan = _move_after(self.claude, "seat-claude-settings-env", "connective.unit-fire")
        with self.assertRaisesRegex(LaunchBeforeContainment, "harness.claude-shell-box"):
            refuse_launch_before_containment(plan)

    def test_a_claude_launch_without_containment_is_refused(self) -> None:
        plan = _without(self.claude, "seat-claude-settings-env")
        with self.assertRaisesRegex(LaunchBeforeContainment, "claude seat launches without"):
            refuse_launch_before_containment(plan)

    def test_a_codex_plan_without_containment_passes(self) -> None:
        self.assertNotIn("seat-claude-settings-env", _kinds(self.codex.operations))
        refuse_launch_before_containment(self.codex)

    def test_routing_never_moves_an_op_to_admitted(self) -> None:
        plans = {
            "claude": self.claude,
            "codex": self.codex,
            "codex+residue-must-run": _with_residue_must_run(self.codex),
            "claude-no-containment": _without(self.claude, "seat-claude-settings-env"),
        }
        for name, plan in plans.items():
            with self.subTest(plan=name):
                broker, residue = partition_installer_plan(plan)
                for op in broker.operations:
                    self.assertIn(op.kind, EXECUTOR_KIND_CLASSES)
                envelope_out = {op.operation_id for op in plan.operations if op.kind not in EXECUTOR_KIND_CLASSES}
                self.assertLessEqual(envelope_out, {op.operation_id for op in residue})
                routed = launch_routed_operation_ids(plan)
                self.assertTrue(all(
                    op.kind in EXECUTOR_KIND_CLASSES for op in plan.operations if op.operation_id in routed
                ))
                self.assertEqual(
                    {op.operation_id for op in broker.operations},
                    {op.operation_id for op in plan.operations
                     if op.kind in EXECUTOR_KIND_CLASSES and op.operation_id not in routed},
                )


DECOMMISSION_ID = "service.ho-resumer.decommission"
DECOMMISSION = "seat-service-decommission"
ROUTED_LAUNCH_IDS = frozenset(
    {"harness.direct-fire", "connective.unit-fire", "connective.timer-enable", "connective.timer-observe"}
)


class RealInstancePlansTest(unittest.TestCase):
    """Routing and the effective-order invariant over EVERY real instance plan.

    T-5633's ho-resumer decommission is Orchestrator-only. T-5748 adds an equivalent
    pre-launch migration on Consultant so its bespoke keep-going timer cannot overlap
    the shared installer-owned timer. Each declared legacy migration routes that
    plan's launch behind the seat-local containment step; other Codex plans keep
    their launch operations broker-admitted.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = PI.Manifest.load(REPO_ROOT / "instance.yaml")
        installer = PI.PersonaInstaller(REPO_ROOT)
        personas = sorted(
            {raw["persona"] for raw in cls.manifest.roles.values() if isinstance(raw, dict) and raw.get("persona")}
        )
        cls._tmp = tempfile.TemporaryDirectory()
        cls.plans = {p: installer.plan(cls.manifest, p, root=Path(cls._tmp.name) / p) for p in personas}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_fleet_has_both_harnesses(self) -> None:
        harnesses = {plan.selected_harness for plan in self.plans.values()}
        self.assertEqual(harnesses, {"claude", "codex"})

    def test_only_plans_with_prelaunch_residue_route_the_four_launch_operations(self) -> None:
        for persona, plan in self.plans.items():
            with self.subTest(persona=persona, harness=plan.selected_harness):
                routed = launch_routed_operation_ids(plan)
                if plan.selected_harness == "claude" or persona == "codex":
                    self.assertEqual(routed, ROUTED_LAUNCH_IDS)
                else:
                    self.assertEqual(routed, frozenset())
                    broker, _residue = partition_installer_plan(plan)
                    for kind in ROUTED_KINDS:
                        self.assertIn(kind, _kinds(broker.operations))

    def test_every_real_plan_is_within_both_bounds_and_passes_the_invariant(self) -> None:
        from tools.host_control.envelope import require_routed_launch_bound
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT

        for persona, plan in self.plans.items():
            with self.subTest(persona=persona):
                _broker, residue = partition_installer_plan(plan)
                routed = launch_routed_operation_ids(plan)
                self.assertLessEqual(
                    # T-5961 (orchestrator ruling on B8): the general bucket; periodic-timer ops are separate
                    residue_bucket_counts((op.kind, op.target) for op in residue if op.operation_id not in routed)[0],
                    SEAT_LOCAL_RESIDUE_LIMIT,
                )
                require_routed_launch_bound(op.kind for op in residue if op.operation_id in routed)
                self.assertIsNone(
                    launch_order_violation(
                        [(op.operation_id, op.kind) for op in effective_broker_order(plan)],
                        selected_harness=plan.selected_harness,
                    )
                )
                refuse_launch_before_containment(plan)

    def test_declared_legacy_migrations_precede_every_launch(self) -> None:
        expected = {
            "orchestrator": "service.ho-resumer.decommission",
            "codex": "service.codex-keep-going.decommission",
        }
        for persona, plan in self.plans.items():
            with self.subTest(persona=persona):
                kinds = _kinds(plan.operations)
                if persona not in expected:
                    self.assertNotIn(DECOMMISSION, kinds)
                    continue
                self.assertEqual(kinds.count(DECOMMISSION), 1)
                _broker, residue = partition_installer_plan(plan)
                self.assertIn(expected[persona], {op.operation_id for op in residue})
                order = _kinds(effective_broker_order(plan))
                self.assertLess(
                    order.index(DECOMMISSION), min(order.index(kind) for kind in LAUNCH_KINDS if kind in order)
                )


class DecommissionOrderRefusalTest(unittest.TestCase):
    """T-5633: the decommission is a must-run-before-launch kind, so a plan with
    it after a launch is refused (synthetic placements)."""

    @classmethod
    def setUpClass(cls) -> None:
        manifest = PI.Manifest.load(REPO_ROOT / "instance.yaml")
        with tempfile.TemporaryDirectory() as work:
            cls.orchestrator = PI.PersonaInstaller(REPO_ROOT).plan(manifest, "orchestrator", root=Path(work) / "orchestrator")
        cls.codex = _plan("codex")

    def test_orchestrator_with_the_decommission_after_a_launch_is_refused(self) -> None:
        refuse_launch_before_containment(self.orchestrator)
        plan = _move_after(self.orchestrator, DECOMMISSION, "connective.unit-fire")
        with self.assertRaisesRegex(LaunchBeforeContainment, DECOMMISSION_ID):
            refuse_launch_before_containment(plan)

    def test_a_codex_plan_with_a_decommission_after_its_fire_is_refused(self) -> None:
        # the T-5633 hazard shape: residue decommission behind an admitted fire
        template = next(op for op in self.codex.operations if op.kind == "atomic-write")
        extra = dataclasses.replace(template, operation_id=DECOMMISSION_ID, kind=DECOMMISSION)
        index = next(i for i, op in enumerate(self.codex.operations) if op.kind == "direct-fire")
        plan = _plan_replace(
            self.codex, operations=self.codex.operations[: index + 1] + (extra,) + self.codex.operations[index + 1:]
        )
        with self.assertRaisesRegex(LaunchBeforeContainment, DECOMMISSION_ID):
            refuse_launch_before_containment(plan)

    def test_a_codex_plan_with_a_decommission_before_its_fire_routes_and_passes(self) -> None:
        template = next(op for op in self.codex.operations if op.kind == "atomic-write")
        extra = dataclasses.replace(template, operation_id=DECOMMISSION_ID, kind=DECOMMISSION)
        index = next(i for i, op in enumerate(self.codex.operations) if op.kind == "direct-fire")
        plan = _plan_replace(
            self.codex, operations=self.codex.operations[:index] + (extra,) + self.codex.operations[index:]
        )
        self.assertTrue(launch_routed_operation_ids(plan))
        refuse_launch_before_containment(plan)


class ProducerLaunchOrderTest(unittest.TestCase):
    """The same invariant at registration time."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self.manifest_path = root / "instance.yaml"
        self.manifest_path.write_text(yaml.safe_dump(_manifest_dict(), sort_keys=False), encoding="utf-8")
        templates = (
            "tools/systemd/pantheon-persona-consume@.service",
            "tools/systemd/pantheon-persona-consume@.timer",
            "tools/systemd/pantheon-persona-quota@.service",
            "tools/systemd/pantheon-persona-quota@.timer",
        )
        self.manifest = {
            "revision": "manifest-r1",
            "path": str(self.manifest_path),
            "sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
            "framework_revision": "test-rev",
            "framework_sha256": HPP.framework_artifact_digest(templates),
            "template_ids": list(templates),
        }
        self.seat = {
            "seat_id": "recovery", "uid": 1002, "persona": "recovery", "role": "recovery",
            "instance": "test-instance", "account": "claude-fallback", "harness": "claude",
            "install_root": "/projection",
        }

    def produce(self, transform=None) -> bytes:
        if transform is None:
            return HPP.produce_plan(framework_root=REPO_ROOT, seat_record=self.seat, manifest_record=self.manifest)
        original = PI.PersonaInstaller.plan

        def crafted(installer, *args, **kwargs):
            return transform(original(installer, *args, **kwargs))

        with mock.patch.object(PI.PersonaInstaller, "plan", crafted):
            return HPP.produce_plan(framework_root=REPO_ROOT, seat_record=self.seat, manifest_record=self.manifest)

    def test_the_real_claude_plan_is_produced(self) -> None:
        plan = PI.InstallPlan.from_dict(json.loads(self.produce()))
        self.assertEqual(plan.selected_harness, "claude")

    def test_a_launch_before_containment_is_refused_at_registration(self) -> None:
        with self.assertRaisesRegex(HPP.PlanProducerError, "launch before containment"):
            self.produce(lambda plan: _move_after(plan, "seat-claude-settings-env", "connective.unit-fire"))

    def test_a_claude_launch_without_containment_is_refused_at_registration(self) -> None:
        with self.assertRaisesRegex(HPP.PlanProducerError, "claude seat launches without"):
            self.produce(lambda plan: _without(plan, "seat-claude-settings-env"))


class BrokerLaunchOrderTest(unittest.TestCase):
    """The same invariant through the live HostPlanService.plan() path, for a
    registered claude seat (the core fixture with the recovery seat on claude)."""

    setUp = _core.HostControlCase.setUp
    tearDown = _core.HostControlCase.tearDown
    build_service = _core.HostControlCase.build_service
    write_json = _core.HostControlCase.write_json
    registry_dict = _core.HostControlCase.registry_dict
    write_registry = _core.HostControlCase.write_registry
    policy_dict = _core.HostControlCase.policy_dict
    write_policy = _core.HostControlCase.write_policy
    row_count = _core.HostControlCase.row_count

    def claude_service(self, transform=lambda plan: plan):
        plan = PI.PersonaInstaller(REPO_ROOT).plan(
            PI.Manifest.load(self.manifest_path), "recovery", harness_override="claude", root=Path("/projection")
        )
        value = self.registry_dict()
        for seat in value["seats"]:
            if seat["seat_id"] == "recovery":
                seat["harness"] = "claude"
        value["plans"] = []
        register_plan(value, self.root, transform(plan), seat_id="recovery", manifest_revision="manifest-r1")
        self.write_registry(value)
        return self.build_service(
            registry=SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework)
        )

    def test_the_real_claude_plan_passes_and_the_broker_plan_holds_no_launch(self) -> None:
        service = self.claude_service()
        result = service.plan(1003, "recovery", "manifest-r1", "claude")
        summary = json.loads(result.summary_json)
        broker_kinds = {op["kind"] for op in summary["operations"]}
        for kind in ROUTED_KINDS:
            self.assertNotIn(kind, broker_kinds)
        residue_kinds = [item["kind"] for item in summary["seat_local_residue"]]
        self.assertLess(residue_kinds.index("seat-claude-settings-env"), residue_kinds.index("direct-fire"))

    def test_a_launch_before_containment_is_refused_by_plan(self) -> None:
        service = self.claude_service(
            lambda plan: _move_after(plan, "seat-claude-settings-env", "connective.unit-fire")
        )
        with self.assertRaisesRegex(LaunchBeforeContainment, "launch before containment"):
            service.plan(1003, "recovery", "manifest-r1", "refused")
        self.assertEqual(self.row_count(), 0)

    def test_a_claude_launch_without_containment_is_refused_by_plan(self) -> None:
        service = self.claude_service(lambda plan: _without(plan, "seat-claude-settings-env"))
        with self.assertRaisesRegex(LaunchBeforeContainment, "claude seat launches without"):
            service.plan(1003, "recovery", "manifest-r1", "refused")
        self.assertEqual(self.row_count(), 0)


if __name__ == "__main__":
    unittest.main()
