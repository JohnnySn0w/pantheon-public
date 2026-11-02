"""T-2917 5b planner partition (HO ruling T-4861; coordinator ruling B8).

The broker plan holds ONLY executor-envelope kinds; every excluded or
unclassified kind is named in a bounded, digest-bound seat-local residue
that the approver sees in the plan summary. Nothing partially applies out
of order and nothing widens quietly.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from tools import persona_installer as _pim
from tools import test_host_control_core as _core
from tools import test_host_control_dbus_driver as _driver
from tools._host_control_plan_fixtures import registered_plan_transform
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.contracts import HostControlError, PolicyDenied
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    LAUNCH_DEPENDENT_KINDS,
    LAUNCH_KINDS,
    OUT_OF_ENVELOPE_KINDS,
    PERMISSION_POSTURE_KINDS,
    OutOfEnvelopeKinds,
    RoutedLaunchBoundExceeded,
)
from tools.host_control.planner import (
    SEAT_LOCAL_ACTION_PREFIX,
    SEAT_LOCAL_CAPABILITY_PREFIX,
    SEAT_LOCAL_RESIDUE_LIMIT,
    launch_routed_operation_ids,
    partition_installer_plan,
    seat_local_residue,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBJECT_UID = 1003
#: T-5470 partition routing: the in-envelope launch class routed to the residue
ROUTED_KINDS = (LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS) & set(EXECUTOR_KIND_CLASSES)


class PartitionFunctionTests(unittest.TestCase):
    def _real_plans(self):
        manifest = _pim.Manifest.load(REPO_ROOT / "instance.yaml")
        installer = _pim.PersonaInstaller(REPO_ROOT)
        for persona in sorted({role["persona"] for role in manifest.roles.values()}):
            for harness in (None, "claude", "codex"):
                try:
                    yield installer.plan(manifest, persona, harness_override=harness, root=Path("/projection"))
                except _pim.InstallerError:
                    continue

    def _codex_plan(self):
        # T-5470: the synthetic dependency cases need a plan with no launch
        # routing, so an admitted kind like unit-fire stays admitted there.
        # T-5608: the residue quarantine is admitted, so the REAL codex plan
        # routes nothing (no strip needed any more).
        plan = next(plan for plan in self._real_plans() if plan.selected_harness == "codex")
        self.assertIn("seat-runtime-residue-quarantine", {op.kind for op in plan.operations})
        return plan

    def test_every_real_plan_partitions_into_admitted_plus_named_residue(self) -> None:
        residue_kinds: set[str] = set()
        routed_kinds: set[str] = set()
        count = 0
        for plan in self._real_plans():
            count += 1
            with self.subTest(persona=plan.persona, harness=plan.selected_harness):
                broker, residue = partition_installer_plan(plan)
                self.assertTrue(broker.validate_digest())
                self.assertTrue(all(op.kind in EXECUTOR_KIND_CLASSES for op in broker.operations))
                self.assertEqual(len(broker.operations) + len(residue), len(plan.operations))
                listed = seat_local_residue(broker)
                self.assertEqual([entry["operation_id"] for entry in listed], [op.operation_id for op in residue])
                routed = launch_routed_operation_ids(plan)
                # T-5470 (HO ruling 2026-10-26): routed launches have their own
                # bound; the B8 cap covers only the out-of-envelope residue
                out_of_envelope = [op for op in residue if op.operation_id not in routed]
                self.assertLessEqual(len(out_of_envelope), SEAT_LOCAL_RESIDUE_LIMIT)
                residue_kinds.update(op.kind for op in out_of_envelope)
                routed_kinds.update(op.kind for op in residue if op.operation_id in routed)
        self.assertGreater(count, 0)
        # anti-vacuity: the real plans exercise the T-4861 kinds
        self.assertEqual(residue_kinds, set(OUT_OF_ENVELOPE_KINDS))
        # and the claude seats exercise every routed in-envelope kind
        self.assertEqual(routed_kinds, (LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS) & set(EXECUTOR_KIND_CLASSES))

    def test_routed_launches_do_not_count_against_the_b8_cap(self) -> None:
        orchestrator = next(
            plan for plan in self._real_plans() if plan.persona == "orchestrator" and plan.selected_harness == "claude"
        )
        routed = launch_routed_operation_ids(orchestrator)
        residue_total = sum(op.kind not in EXECUTOR_KIND_CLASSES for op in orchestrator.operations) + len(routed)
        self.assertGreater(residue_total, SEAT_LOCAL_RESIDUE_LIMIT)  # it would overflow if counted
        partition_installer_plan(orchestrator)

    def test_two_ops_of_one_routed_kind_are_refused_by_the_routing_bound(self) -> None:
        plan = next(plan for plan in self._real_plans() if plan.selected_harness == "claude")
        fire = next(op for op in plan.operations if op.kind == "unit-fire")
        second = dataclasses.replace(fire, operation_id="connective.unit-fire-2")
        with self.assertRaises(RoutedLaunchBoundExceeded) as caught:
            partition_installer_plan(_core._plan_replace(plan, operations=plan.operations + (second,)))
        self.assertEqual(caught.exception.kind, "unit-fire")
        self.assertIn("more than one routed unit-fire op", caught.exception.detail())

    def test_permission_posture_and_unclassified_kinds_are_always_residue(self) -> None:
        plan = next(self._real_plans())
        base = plan.operations[1]
        for kind in sorted(PERMISSION_POSTURE_KINDS | {"future-unclassified-kind"}):
            with self.subTest(kind=kind):
                extra = dataclasses.replace(base, operation_id=f"x.{kind}", kind=kind)
                broker, residue = partition_installer_plan(
                    _core._plan_replace(plan, operations=plan.operations + (extra,))
                )
                self.assertIn(f"x.{kind}", [op.operation_id for op in residue])
                self.assertNotIn(kind, {op.kind for op in broker.operations})

    def test_ordering_dependency_on_residue_refuses_the_whole_plan(self) -> None:
        plan = self._codex_plan()
        residue_op = dataclasses.replace(plan.operations[1], operation_id="seat.only", kind="claude-mcp-server-registration")
        dependent = dataclasses.replace(
            plan.operations[2], operation_id="after.residue", precondition={"after": "x,seat.only"}
        )
        with self.assertRaises(OutOfEnvelopeKinds) as caught:
            partition_installer_plan(
                _core._plan_replace(plan, operations=plan.operations + (residue_op, dependent))
            )
        self.assertEqual(caught.exception.refused_kinds, ("claude-mcp-server-registration",))

    def test_dependency_guard_reads_desired_after_kinds_and_whitespace(self) -> None:
        # review S1: precondition AND desired "after", whitespace-tolerant,
        # naming either a residue operation id or a residue kind
        plan = self._codex_plan()
        residue_op = dataclasses.replace(plan.operations[1], operation_id="seat.only", kind="codex-telegram-mcp-check")
        cases = {
            "precondition id with spaces": {"precondition": {"after": " x , seat.only "}},
            "desired id": {"desired": {"after": "seat.only"}},
            "desired kind": {"desired": {"after": "unit-fire, codex-telegram-mcp-check"}},
            "precondition kind": {"precondition": {"after": "codex-telegram-mcp-check"}},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                dependent = dataclasses.replace(plan.operations[2], operation_id="dep", **change)
                with self.assertRaises(OutOfEnvelopeKinds) as caught:
                    partition_installer_plan(
                        _core._plan_replace(plan, operations=plan.operations + (residue_op, dependent))
                    )
                self.assertEqual(caught.exception.refused_kinds, ("codex-telegram-mcp-check",))
        # an "after" naming only admitted kinds or ids is fine
        dependent = dataclasses.replace(plan.operations[2], operation_id="dep", desired={"after": "unit-fire, mkdir"})
        broker, residue = partition_installer_plan(
            _core._plan_replace(plan, operations=plan.operations + (residue_op, dependent))
        )
        self.assertIn("seat.only", [op.operation_id for op in residue])
        self.assertIn("dep", [op.operation_id for op in broker.operations])

    def test_residue_is_bounded(self) -> None:
        plan = next(self._real_plans())
        extras = tuple(
            dataclasses.replace(plan.operations[1], operation_id=f"r{index}", kind="codex-telegram-mcp-check")
            for index in range(SEAT_LOCAL_RESIDUE_LIMIT + 1)
        )
        with self.assertRaises(PolicyDenied):
            partition_installer_plan(_core._plan_replace(plan, operations=plan.operations + extras))

    # T-5617: the summary-side re-check of both bounds in seat_local_residue
    # (defence in depth: the partition enforces them first, so these feed the
    # summary a broker plan the partition would never emit).
    def _claude_broker_plan(self):
        plan = next(plan for plan in self._real_plans() if plan.selected_harness == "claude")
        broker, _residue = partition_installer_plan(plan)
        return broker

    def test_summary_refuses_two_seat_local_actions_of_one_routed_kind(self) -> None:
        broker = self._claude_broker_plan()
        fire = next(
            action for action in broker.operator_actions
            if action.required_capability == f"{SEAT_LOCAL_CAPABILITY_PREFIX}unit-fire"
        )
        self.assertIn("unit-fire", ROUTED_KINDS)
        seat_local_residue(broker)  # control: one routed unit-fire is within the bound
        second = dataclasses.replace(fire, action_id=f"{SEAT_LOCAL_ACTION_PREFIX}connective.unit-fire-2")
        doubled = _core._plan_replace(broker, operator_actions=broker.operator_actions + (second,))
        with self.assertRaises(RoutedLaunchBoundExceeded) as caught:
            seat_local_residue(doubled)
        self.assertEqual(caught.exception.kind, "unit-fire")

    def test_summary_refuses_out_of_envelope_residue_over_its_bound(self) -> None:
        broker = self._claude_broker_plan()
        template = next(
            action for action in broker.operator_actions if action.action_id.startswith(SEAT_LOCAL_ACTION_PREFIX)
        )
        kept = tuple(
            action for action in broker.operator_actions if not action.action_id.startswith(SEAT_LOCAL_ACTION_PREFIX)
        )

        def with_residue(count: int):
            entries = tuple(
                dataclasses.replace(
                    template,
                    action_id=f"{SEAT_LOCAL_ACTION_PREFIX}extra.residue-{index}",
                    required_capability=f"{SEAT_LOCAL_CAPABILITY_PREFIX}codex-telegram-mcp-check",
                )
                for index in range(count)
            )
            return _core._plan_replace(broker, operator_actions=kept + entries)

        self.assertNotIn("codex-telegram-mcp-check", EXECUTOR_KIND_CLASSES)
        self.assertEqual(len(seat_local_residue(with_residue(SEAT_LOCAL_RESIDUE_LIMIT))), SEAT_LOCAL_RESIDUE_LIMIT)
        with self.assertRaisesRegex(HostControlError, "seat-local residue exceeds its bound"):
            seat_local_residue(with_residue(SEAT_LOCAL_RESIDUE_LIMIT + 1))

    def test_plan_without_residue_is_unchanged(self) -> None:
        plan = next(self._real_plans())
        clean = _core._plan_replace(
            plan, operations=tuple(op for op in plan.operations if op.kind in EXECUTOR_KIND_CLASSES)
        )
        broker, residue = partition_installer_plan(clean)
        self.assertIs(broker, clean)
        self.assertEqual(residue, ())


class PlannerPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = _driver.build_domain(
            Path(temp.name) / "domain",
            subject_uid=SUBJECT_UID,
            clock=_driver.MutableClock(_driver.FIXED_NOW),
        )
        self.addCleanup(self.fixture.close)

    def plan_with(self, *kinds: str, key: str):
        # T-5533: inject into the REGISTERED plan the broker reads (was: wrap
        # installer.plan, which the broker no longer calls).
        def injected(plan):
            extras = tuple(
                dataclasses.replace(plan.operations[1], operation_id=f"injected.{index}", kind=kind)
                for index, kind in enumerate(kinds)
            )
            return _core._plan_replace(plan, operations=plan.operations + extras)

        with registered_plan_transform(injected):
            return self.fixture.plan_service.plan(SUBJECT_UID, "recovery", "manifest-r1", key)

    def test_approver_facing_summary_shows_the_residue(self) -> None:
        # B8: a human approving through HostApproval1 reads EXACTLY the
        # PlanSummary bytes; they must name what stays seat-local
        result = self.plan_with("codex-telegram-mcp-check", "claude-mcp-server-registration", key="b8")
        source = BrokerApprovalSource(journal=self.fixture.journal, clock=self.fixture.clock)
        _, summary_bytes = source.plan_summary(result.plan_handle, result.plan_digest)
        self.assertEqual(summary_bytes, result.summary_json)
        summary = json.loads(summary_bytes)
        # T-5447: every real plan now also carries the seat-core periodic ops as
        # seat-local residue; they are asserted present, then set aside so the
        # injected residue is still compared exactly.
        periodic = [r for r in summary["seat_local_residue"] if r["operation_id"].startswith("periodic.")]
        self.assertTrue(periodic)
        self.assertTrue(all(r["status"] == "pending-seat-local" for r in periodic))
        # T-5470/T-5473: the routed launches (behind the residue quarantine on
        # every seat) are seat-local residue too; set aside like the periodics.
        self.assertEqual(
            [
                r for r in summary["seat_local_residue"]
                if not r["operation_id"].startswith(("periodic.", "service.")) and r["kind"] not in ROUTED_KINDS
            ],
            [
                {"operation_id": "injected.0", "kind": "codex-telegram-mcp-check", "status": "pending-seat-local"},
                {"operation_id": "injected.1", "kind": "claude-mcp-server-registration", "status": "pending-seat-local"},
            ],
        )
        self.assertNotIn("injected.0", {op["operation_id"] for op in summary["operations"]})

    def test_residue_is_bound_into_the_plan_and_effect_digests(self) -> None:
        plain = self.plan_with(key="none")
        one = self.plan_with("codex-telegram-mcp-check", key="one")
        other = self.plan_with("claude-mcp-server-registration", key="other")
        # T-5447: the only baseline residue is the seat-core periodic set
        # (T-5470/T-5473: plus the routed launches, set aside here)
        self.assertEqual(
            [
                r
                for r in json.loads(plain.summary_json)["seat_local_residue"]
                if not r["operation_id"].startswith(("periodic.", "service.")) and r["kind"] not in ROUTED_KINDS
            ],
            [],
        )
        effects = {plain.effect_digest, one.effect_digest, other.effect_digest}
        self.assertEqual(len(effects), 3)
        stored = self.fixture.journal.load_plan_unbound(one.plan_handle)
        self.assertIn(
            "seat-local.injected.0",
            [action.action_id for action in stored.installer_plan.operator_actions],
        )


if __name__ == "__main__":  # must stay LAST
    unittest.main()
