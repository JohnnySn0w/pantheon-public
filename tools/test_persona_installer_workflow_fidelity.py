"""T-5472: the dry run predicts the staged-activate boundary refusals execute hits.

Intent: what a digest-approved dry run says under each workflow (all-in-one /
stage-prefix / staged-activate) is what execute does (T-5446 digest-approve
contract). T-5471 left one documented gap: ``preflight`` could not see the
workflow, so the staged-activate-only timer-enable refusal was not predicted.

Tests only: SimulatedHostBackend, and LocalHostBackend with a stubbed timer
state and a recording ``_run`` (nothing here touches a real manager or home).
"""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from tools import persona_installer as pim
from tools import persona_installer_apply as pia
from tools.persona_installer import (
    EXECUTION_CONTEXT_ALL_IN_ONE,
    EXECUTION_CONTEXT_STAGE_PREFIX,
    EXECUTION_CONTEXT_STAGED_ACTIVATE,
    InstallOperation,
    OperationFailed,
    PreflightResult,
    SimulatedHostBackend,
)
from tools.persona_installer_activation import TimerUnitState
from tools.test_persona_installer_converge import ConvergeCase
from tools.test_persona_installer_systemctl_port import RecordingBackend

CONTEXTS = {
    "all-in-one": EXECUTION_CONTEXT_ALL_IN_ONE,
    "stage-prefix": EXECUTION_CONTEXT_STAGE_PREFIX,
    "staged-activate": EXECUTION_CONTEXT_STAGED_ACTIVATE,
}
REFUSAL = "timer already enabled at staged activation boundary"


class TimerStateBackend(RecordingBackend):
    """Local backend whose timer unit state is injected; every other read is the real code."""

    timer_state = "disabled"

    def _timer_unit_state(self, unit: str) -> TimerUnitState:  # type: ignore[override]
        enabled = self.timer_state == "enabled"
        return TimerUnitState(self.timer_state, "loaded", "enabled" if enabled else "disabled", "active", "waiting")


class PredicateTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        f = pim._staged_activate_enabled_timer_refusal
        self.assertEqual(f("staged-activate", True, False), REFUSAL)
        self.assertIsNone(f("staged-activate", True, True))  # legacy-converged evidence seen
        self.assertIsNone(f("staged-activate", False, False))
        for workflow in ("all-in-one", "stage-prefix"):
            self.assertIsNone(f(workflow, True, False))

    def test_workflow_scopes(self) -> None:
        self.assertEqual(pim.workflow_scopes("all-in-one"), frozenset({"all"}))
        for workflow in ("stage-prefix", "staged-activate"):
            self.assertEqual(pim.workflow_scopes(workflow), frozenset({"all", "staged-cutover"}))


class PreflightInContextTests(unittest.TestCase):
    def test_context_is_passed_to_a_backend_that_accepts_it(self) -> None:
        seen = []

        class New:
            def preflight(self, operation, context=None):
                seen.append(context)
                return PreflightResult(True, "x", "y")

        pim.preflight_in_context(New(), None, EXECUTION_CONTEXT_STAGED_ACTIVATE)  # type: ignore[arg-type]
        self.assertEqual(seen, [EXECUTION_CONTEXT_STAGED_ACTIVATE])

    def test_one_arg_override_still_works_without_context(self) -> None:
        class Old:
            def preflight(self, operation):
                return PreflightResult(True, "old", "y")

        result = pim.preflight_in_context(Old(), None, EXECUTION_CONTEXT_STAGED_ACTIVATE)  # type: ignore[arg-type]
        self.assertEqual(result.classification, "old")

    def test_none_context_calls_the_plain_form(self) -> None:
        class Old:
            def preflight(self, operation):
                return PreflightResult(True, "old", "y")

        self.assertTrue(pim.preflight_in_context(Old(), None, None).ok)  # type: ignore[arg-type]


class SimFidelityTests(ConvergeCase):
    def test_sim_preflight_agrees_with_execute_per_workflow_and_timer_state(self) -> None:
        enable = self.op(self.base_plan, "connective.timer-enable")
        for workflow, ctx in CONTEXTS.items():
            for enabled in (False, True):
                with self.subTest(workflow=workflow, timer_enabled=enabled):
                    dry = SimulatedHostBackend()
                    dry.timer_enabled = enabled
                    verdict = dry.preflight(enable, ctx)
                    live = SimulatedHostBackend()
                    live.timer_enabled = enabled
                    try:
                        live.execute(enable, dataclasses.replace(ctx, allowed_roots=(str(self.root),)))
                        ran = True
                    except OperationFailed:
                        ran = False
                    self.assertEqual(verdict.ok, ran, verdict)
                    expect_refused = workflow == "staged-activate" and enabled
                    self.assertEqual(verdict.ok, not expect_refused)
                    if expect_refused:
                        self.assertIn(REFUSAL, verdict.detail)

    def test_sim_preflight_without_context_keeps_todays_behaviour(self) -> None:
        enable = self.op(self.base_plan, "connective.timer-enable")
        backend = SimulatedHostBackend()
        backend.timer_enabled = True
        self.assertTrue(backend.preflight(enable).ok)


class LocalFidelityTests(ConvergeCase):
    def local(self, state: str, *, observed: bool = False) -> TimerStateBackend:
        backend = TimerStateBackend([])
        backend.timer_state = state
        if observed:
            backend.events["connective.timer-observe"] = pim.OperationEvidence(
                "connective.timer-observe", 1, "timer-observe", True, "seeded", {}
            )
        return backend

    def enable_op(self) -> InstallOperation:
        return self.rooted(self.op(self.base_plan, "connective.timer-enable"))

    def prepare_link(self, op: InstallOperation, enabled: bool) -> None:
        """An enabled timer carries its canonical enablement link; a disabled one has none."""
        link = Path(op.desired["enablement_link_path"])
        canonical = Path(op.desired["canonical_template_path"])
        if enabled:
            link.parent.mkdir(parents=True, exist_ok=True)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text("[Timer]\n", encoding="utf-8")
            if not link.is_symlink():
                link.symlink_to(str(canonical))

    def execute_ok(self, backend: TimerStateBackend, op: InstallOperation, ctx) -> bool:
        link = Path(op.desired["enablement_link_path"])
        canonical = Path(op.desired["canonical_template_path"])
        if backend.timer_state == "disabled":
            # the fake `enable --now` materialises the canonical link, like systemd
            def enable(command):
                if command[:3] == ("systemctl", "--user", "enable"):
                    link.parent.mkdir(parents=True, exist_ok=True)
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                    canonical.write_text("[Timer]\n", encoding="utf-8")
                    if not link.is_symlink():
                        link.symlink_to(str(canonical))
                return None

            backend.handlers.append(enable)
        try:
            backend.execute(op, dataclasses.replace(ctx, allowed_roots=(str(self.root),)))
        except OperationFailed:
            return False
        return True

    def test_local_preflight_agrees_with_execute_per_workflow_and_timer_state(self) -> None:
        for workflow, ctx in CONTEXTS.items():
            for state in ("disabled", "enabled"):
                with self.subTest(workflow=workflow, timer=state):
                    op = self.enable_op()
                    link = Path(op.desired["enablement_link_path"])
                    if link.is_symlink() or link.exists():
                        link.unlink()
                    self.prepare_link(op, state == "enabled")
                    verdict = self.local(state).preflight(op, ctx)
                    live = self.local(state)
                    ran = self.execute_ok(live, op, ctx)
                    self.assertEqual(verdict.ok, ran, verdict)
                    if workflow == "staged-activate" and state == "enabled":
                        self.assertFalse(verdict.ok)
                        self.assertIn(REFUSAL, verdict.detail)
                    else:
                        self.assertTrue(verdict.ok, verdict)

    def test_legacy_converged_evidence_lets_staged_activate_through_in_both(self) -> None:
        op = self.enable_op()
        self.prepare_link(op, True)
        ctx = EXECUTION_CONTEXT_STAGED_ACTIVATE
        self.assertTrue(self.local("enabled", observed=True).preflight(op, ctx).ok)
        self.assertTrue(self.execute_ok(self.local("enabled", observed=True), op, ctx))

    def test_local_preflight_without_context_keeps_todays_behaviour(self) -> None:
        op = self.enable_op()
        self.prepare_link(op, True)
        self.assertTrue(self.local("enabled").preflight(op).ok)


class DryRunWorkflowTests(ConvergeCase):
    def verdicts(self, workflow: str, backend) -> dict[str, dict]:
        plan = self.base_plan
        actor = pia.actor_for_plan(plan)
        return {
            v["operation_id"]: v
            for v in pia.preflight_verdicts(plan, backend, actor, dry_run=True, workflow=workflow)
        }

    def test_staged_activate_dry_run_reports_the_boundary_refusal_only_there(self) -> None:
        for workflow in pia.WORKFLOWS:
            with self.subTest(workflow=workflow):
                backend = SimulatedHostBackend()
                backend.timer_enabled = True
                verdict = self.verdicts(workflow, backend)["connective.timer-enable"]
                self.assertEqual(verdict["state"], "refused" if workflow == "staged-activate" else "pass", verdict)

    def test_disabled_timer_passes_under_every_workflow(self) -> None:
        for workflow in pia.WORKFLOWS:
            with self.subTest(workflow=workflow):
                verdict = self.verdicts(workflow, SimulatedHostBackend())["connective.timer-enable"]
                self.assertEqual(verdict["state"], "pass", verdict)

    def test_scope_filter_matches_what_each_workflow_preflights(self) -> None:
        absent = "connective.timer-enablement-absent"
        self.assertNotIn(absent, self.verdicts("all-in-one", SimulatedHostBackend()))
        for workflow in ("stage-prefix", "staged-activate"):
            self.assertIn(absent, self.verdicts(workflow, SimulatedHostBackend()))

    def test_default_workflow_is_all_in_one(self) -> None:
        actor = pia.actor_for_plan(self.base_plan)
        default = pia.preflight_verdicts(self.base_plan, SimulatedHostBackend(), actor)
        explicit = pia.preflight_verdicts(self.base_plan, SimulatedHostBackend(), actor, workflow="all-in-one")
        self.assertEqual(default, explicit)

    def test_unknown_workflow_is_rejected(self) -> None:
        actor = pia.actor_for_plan(self.base_plan)
        with self.assertRaises(pia.ApplyRefused):
            pia.preflight_verdicts(self.base_plan, SimulatedHostBackend(), actor, workflow="bogus")


if __name__ == "__main__":
    unittest.main()
