"""T-5471: dry-run fidelity and convergence of an already-installed consume seat.

Intent: one installer run converges a seat that is already on the consume plane
(T-5446), and what the dry run approves is what execute does (the digest-approve
contract). Mason, the first real install, failed at execute on
``connective.stale-unit-failure-reset`` after a dry run that said "ok".

Tests only: a fake ``systemctl`` behind ``LocalHostBackend._run``, tmp roots,
``SimulatedHostBackend``. Nothing here touches a real home, manager or seat.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import persona_installer as pim
from tools import persona_installer_apply as pia
from tools import persona_installer_verify_seat as vs
from tools import user_systemd as us
from tools.persona_installer import (
    InstallOperation,
    LocalHostBackend,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_persona_installer_periodics import PeriodicCase
from tools.test_persona_installer_systemctl_port import RecordingBackend, context, cp

INVOCATION = "0123456789abcdef0123456789abcdef"
OLD_INVOCATION = "fedcba9876543210fedcba9876543210"
NEW_INVOCATION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_INVOCATION = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
MUTATING = {"start", "stop", "enable", "disable", "restart", "daemon-reload", "reset-failed", "kill"}

CLEAN = {
    "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
    "ExecMainStatus": "0", "InvocationID": "", "Job": "", "SuccessExitStatus": "",
}
STALE_FAILED = {
    **CLEAN, "ActiveState": "failed", "SubState": "failed", "Result": "exit-code",
    "ExecMainStatus": "1", "InvocationID": INVOCATION,
}
LOADED_FAILED = {**STALE_FAILED, "LoadState": "loaded"}
# mason, read-only evidence from the 2026-10-25 19:00Z receipt:
# loaded / inactive / dead / result=success / exit 5 (policy-skip) / invocation present
MASON = {
    **CLEAN, "LoadState": "loaded", "ExecMainStatus": "5", "InvocationID": OLD_INVOCATION,
    "SuccessExitStatus": "3 4 5",
}
LOADED_ACTIVE = {**MASON, "ActiveState": "activating", "SubState": "start", "Job": "7"}
LOADED_QUEUED = {**MASON, "Job": "9"}
LOADED_OUT_OF_POLICY = {**MASON, "ExecMainStatus": "1"}
LOADED_NO_POLICY = {**MASON, "SuccessExitStatus": ""}  # exit 5 but 5 is not a success code
LOADED_BAD_ID = {**MASON, "InvocationID": "NOT-A-VALID-ID"}
LOADED_NEVER_RAN = {**CLEAN, "LoadState": "loaded"}

# (state, does the stale-reset gate proceed?)
STALE_CASES = {
    "clean-not-found": (CLEAN, True),
    "stale-failed-not-found": (STALE_FAILED, True),
    "mason-loaded-converged": (MASON, True),
    "loaded-never-ran": (LOADED_NEVER_RAN, True),
    "loaded-failed": (LOADED_FAILED, False),
    "loaded-active": (LOADED_ACTIVE, False),
    "loaded-queued-job": (LOADED_QUEUED, False),
    "loaded-out-of-policy-exit": (LOADED_OUT_OF_POLICY, False),
    "loaded-exit-5-without-policy": (LOADED_NO_POLICY, False),
    "loaded-malformed-invocation": (LOADED_BAD_ID, False),
}


class FakeSystemd:
    """A tiny stateful ``systemctl --user`` for the unit-state reads the installer makes.

    ``poll_script`` (optional) answers the unit-fire poll reads in order, last entry sticky."""

    def __init__(self, units: dict[str, dict[str, str]], *, timer_enabled: bool = False,
                 poll_script: list[dict[str, str]] | None = None) -> None:
        self.units = {name: dict(props) for name, props in units.items()}
        self.timer_enabled = timer_enabled
        self.poll_script = poll_script
        self.polls = 0
        self.log: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> Any:
        self.log.append(command)
        if command[:3] == ("systemctl", "--user", "show") and len(command) > 3 and command[3] != "-p":
            unit = command[3]
            wanted = [command[i + 1] for i, arg in enumerate(command) if arg == "-p"]
            state = self.units.get(unit, CLEAN)
            if self.poll_script is not None and "SubState" in wanted and "InvocationID" in wanted:
                state = self.poll_script[min(self.polls, len(self.poll_script) - 1)]
                self.polls += 1
            return cp(command, 0, "".join(f"{name}={state.get(name, '')}\n" for name in wanted))
        if command[:3] == ("systemctl", "--user", "is-enabled"):
            enabled = self.timer_enabled and command[3].endswith(".timer")
            return cp(command, 0 if enabled else 1, "enabled\n" if enabled else "disabled\n")
        if command[:3] == ("systemctl", "--user", "reset-failed"):
            unit = command[3]
            if self.units.get(unit, {}).get("LoadState") == "not-found":
                self.units[unit] = dict(CLEAN)
            return cp(command)
        return cp(command)

    def mutations(self) -> list[tuple[str, ...]]:
        return [c for c in self.log if len(c) > 2 and c[2] in MUTATING]


class GateStubBackend(RecordingBackend):
    """RecordingBackend with the bootstrap gates neutral (RecordingBackend stubs them)."""


class GatesBackend(RecordingBackend):
    """RecordingBackend with the REAL bootstrap gates and an injectable maintenance flag."""

    maintenance = False
    _unit_fire_bootstrap_blocker = LocalHostBackend._unit_fire_bootstrap_blocker

    def _seat_in_maintenance(self, persona: str) -> bool:  # type: ignore[override]
        return self.maintenance


def seeded_sim(state: dict[str, str]) -> SimulatedHostBackend:
    backend = SimulatedHostBackend()
    backend.seed_consume_service(
        load_state=state["LoadState"], active_state=state["ActiveState"],
        sub_state=state["SubState"], result=state["Result"],
        exec_main_status=state["ExecMainStatus"], invocation_id=state["InvocationID"],
        job=state["Job"], success_exit_status=state["SuccessExitStatus"],
    )
    return backend


class ConvergeCase(PeriodicCase):
    SERVICE = "pantheon-persona-consume@recovery.service"

    def setUp(self) -> None:
        super().setUp()
        self.base_plan = self.plan("recovery")

    def rooted(self, operation: InstallOperation) -> InstallOperation:
        return dataclasses.replace(
            operation, provenance={**operation.provenance, "install_root": "/"}, owner="probe"
        )

    def stale_op(self) -> InstallOperation:
        return self.rooted(self.op(self.base_plan, "connective.stale-unit-failure-reset"))

    def fire_op(self) -> InstallOperation:
        return self.rooted(self.op(self.base_plan, "connective.unit-fire"))

    def backend(self, state: dict[str, str], *, timer_enabled: bool = False, cls=GateStubBackend,
                poll_script=None) -> tuple[RecordingBackend, FakeSystemd]:
        fake = FakeSystemd({self.SERVICE: state}, timer_enabled=timer_enabled, poll_script=poll_script)
        return cls([fake]), fake

    def execute(self, backend: RecordingBackend, operation: InstallOperation) -> bool:
        try:
            backend.execute(operation, context(str(self.root)))
        except OperationFailed:
            return False
        return True


class StaleClassifierTests(unittest.TestCase):
    """The one installer classifier. The broker's classify_stale_reset is NOT touched."""

    def classify(self, state: dict[str, str]) -> str:
        view = pim.StaleUnitFailureResetSnapshot(
            load_state=state["LoadState"], active_state=state["ActiveState"], sub_state=state["SubState"],
            result=state["Result"], exec_main_status=state["ExecMainStatus"],
            invocation_id=state["InvocationID"], job=state["Job"], stdout_length=0, stdout_sha256="",
        )
        return pim._classify_stale_reset_for_install(
            view, lambda: us.exit_policy_from_text(state["SuccessExitStatus"])
        )

    def test_mason_shape_is_adopted(self) -> None:
        self.assertEqual(self.classify(MASON), pim.STALE_ADOPTED)

    def test_fresh_and_stale_failed_shapes_keep_their_historical_verdicts(self) -> None:
        self.assertEqual(self.classify(CLEAN), "clean-noop")
        self.assertEqual(self.classify(STALE_FAILED), "reset-required")

    def test_non_converged_loaded_shapes_refuse_with_a_reason_and_the_historical_prefix(self) -> None:
        for name in ("loaded-failed", "loaded-active", "loaded-queued-job", "loaded-out-of-policy-exit",
                     "loaded-exit-5-without-policy", "loaded-malformed-invocation"):
            with self.subTest(state=name):
                with self.assertRaises(OperationFailed) as raised:
                    self.classify(STALE_CASES[name][0])
                self.assertIn("pre-existing loaded service", str(raised.exception))
                self.assertIn("not adoptable", str(raised.exception))

    def test_the_broker_classifier_still_refuses_every_loaded_service(self) -> None:
        snapshot = pim._stale_unit_failure_reset_typed(
            pim.StaleUnitFailureResetSnapshot(
                load_state="loaded", active_state="inactive", sub_state="dead", result="success",
                exec_main_status="5", invocation_id=OLD_INVOCATION, job="", stdout_length=0, stdout_sha256="",
            )
        )
        with self.assertRaisesRegex(us.UnitStateRefused, "^pre-existing loaded service$"):
            us.classify_stale_reset(snapshot)


class StalePreflightFidelityTests(ConvergeCase):
    """GAP 1: the dry-run verdict for the stale reset equals the execute outcome."""

    def test_preflight_reads_live_state_and_matches_execute_per_state(self) -> None:
        for name, (state, proceeds) in STALE_CASES.items():
            with self.subTest(state=name):
                dry, dry_fake = self.backend(state)
                verdict = dry.preflight(self.stale_op())
                self.assertEqual(verdict.ok, proceeds, verdict)
                self.assertEqual(dry_fake.mutations(), [])  # the dry run is read-only
                live, _ = self.backend(state)
                self.assertEqual(self.execute(live, self.stale_op()), proceeds)

    def test_a_loaded_failed_service_is_refused_by_preflight_with_the_execute_reason(self) -> None:
        backend, _ = self.backend(LOADED_FAILED)
        verdict = backend.preflight(self.stale_op())
        self.assertFalse(verdict.ok)
        self.assertIn("pre-existing loaded service", verdict.detail)

    def test_an_unreadable_unit_is_a_refusal_not_a_pass(self) -> None:
        backend = RecordingBackend(
            [lambda c: cp(c, 1, "", "boom") if c[:3] == ("systemctl", "--user", "show") and c[3] != "-p" else None]
        )
        self.assertFalse(backend.preflight(self.stale_op()).ok)


class AdoptionExecuteTests(ConvergeCase):
    def test_adopted_service_is_not_reset_and_not_receipt_owned(self) -> None:
        backend, fake = self.backend(MASON)
        mutation, evidence = backend.execute(self.stale_op(), context(str(self.root)))
        self.assertEqual(fake.mutations(), [])  # no reset-failed
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before, {"state": pim.STALE_ADOPTED})
        self.assertEqual(mutation.after_fingerprint, "evidence-only")
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["classification"], "adopted")
        self.assertEqual(evidence.evidence["pre_exec_main_status"], "5")

    def test_a_reset_failed_service_is_still_reset(self) -> None:
        backend, fake = self.backend(STALE_FAILED)
        mutation, evidence = backend.execute(self.stale_op(), context(str(self.root)))
        self.assertEqual(evidence.evidence["classification"], "reset")
        self.assertEqual(len(fake.mutations()), 1)


class UnitFirePreflightFidelityTests(ConvergeCase):
    """GAP 1+2: the unit-fire preflight predicts the pre-enqueue idle proof execute runs."""

    def execute_outcome(self, state: dict[str, str]) -> bool:
        """What execute does with a disabled timer: the stale gate, then the idle proof."""
        backend, _ = self.backend(state)
        if not self.execute(backend, self.stale_op()):
            return False
        try:
            backend._prove_unit_fire_pre_enqueue_idle(self.SERVICE, timeout=1.0)
        except OperationFailed:
            return False
        return True

    def test_disabled_timer_verdict_equals_execute_outcome_per_state(self) -> None:
        for name, (state, _proceeds) in STALE_CASES.items():
            with self.subTest(state=name):
                backend, fake = self.backend(state)
                verdict = backend.preflight(self.fire_op())
                self.assertEqual(verdict.ok, self.execute_outcome(state), verdict)
                self.assertEqual(fake.mutations(), [])

    def test_failed_service_with_a_prior_invocation_is_never_a_baseline(self) -> None:
        backend, _ = self.backend(LOADED_FAILED)
        self.assertFalse(backend.preflight(self.fire_op()).ok)


def _poll(active: str, invocation: str, *, sub: str = "", result: str = "success", exit_status: str = "0",
          job: str = "", success_exit: str = "3 4 5") -> dict[str, str]:
    sub = sub or {"inactive": "dead", "activating": "start", "active": "running", "failed": "failed"}[active]
    return {
        "ActiveState": active, "SubState": sub, "InvocationID": invocation, "Result": result,
        "ExecMainStatus": exit_status, "Job": job, "SuccessExitStatus": success_exit,
    }


class BaselineAwareFireTests(ConvergeCase):
    """Execute's unit-fire against a service that already has a PRIOR InvocationID."""

    RECEIPT = {
        "selected_runtime": "claude/interactive-subscription", "selected_harness": "claude",
        "selected_profile": "interactive-subscription", "provider_completion_status": "provider-completed",
        "invocation_receipt_sha256": "r" * 64,
    }

    def fire(self, script: list[dict[str, str]], *, receipt: Any = None):
        backend, fake = self.backend(MASON, poll_script=script)
        patched = mock.patch(
            "tools.runtime_controller.validate_initial_viability_invocation_receipt",
            side_effect=receipt if isinstance(receipt, BaseException) else None,
            return_value=receipt if not isinstance(receipt, BaseException) else None,
        )
        with patched:
            try:
                _mutation, evidence = backend.execute(self.fire_op(), context(str(self.root)))
            except OperationFailed as exc:
                return None, exc, fake
        return evidence, None, fake

    def test_prior_invocation_is_ignored_until_a_new_active_one_is_captured(self) -> None:
        evidence, error, fake = self.fire(
            [
                _poll("inactive", OLD_INVOCATION, exit_status="5"),            # pre-enqueue baseline
                _poll("inactive", OLD_INVOCATION, exit_status="5", job="11"),   # start queued, not begun
                _poll("inactive", OLD_INVOCATION, exit_status="5"),             # still the prior run
                _poll("activating", NEW_INVOCATION),                            # captured
                _poll("inactive", NEW_INVOCATION, exit_status="5"),             # completion, in policy
            ],
            receipt=self.RECEIPT,
        )
        self.assertIsNone(error)
        self.assertEqual(evidence.evidence["invocation_id"], NEW_INVOCATION)
        self.assertEqual(sum(1 for c in fake.log if c[2:3] == ("start",)), 1)

    def test_only_the_exact_pre_enqueue_id_is_ignored(self) -> None:
        # a DIFFERENT id appearing on an inactive unit is the historical capture failure
        evidence, error, _ = self.fire(
            [
                _poll("inactive", OLD_INVOCATION, exit_status="5"),
                _poll("inactive", OTHER_INVOCATION, exit_status="5"),
            ],
            receipt=self.RECEIPT,
        )
        self.assertIsNone(evidence)
        self.assertIn("not captured while active", str(error))

    def test_failed_service_with_a_prior_invocation_still_refuses_before_any_start(self) -> None:
        evidence, error, fake = self.fire([_poll("failed", OLD_INVOCATION, result="exit-code", exit_status="1")])
        self.assertIsNone(evidence)
        self.assertIn("not idle", str(error))
        self.assertEqual([c for c in fake.log if c[2:3] == ("start",)], [])

    def test_out_of_policy_prior_exit_refuses_before_any_start(self) -> None:
        evidence, error, fake = self.fire([_poll("inactive", OLD_INVOCATION, exit_status="1")])
        self.assertIsNone(evidence)
        self.assertEqual([c for c in fake.log if c[2:3] == ("start",)], [])

    def test_a_fire_that_exits_in_policy_without_launching_is_not_a_success(self) -> None:
        # armed maintenance / held runtime: consume exits in policy and never writes the
        # initial-viability invocation receipt, so the receipt validation fails the fire.
        evidence, error, _ = self.fire(
            [
                _poll("inactive", OLD_INVOCATION, exit_status="5"),
                _poll("activating", NEW_INVOCATION),
                _poll("inactive", NEW_INVOCATION, exit_status="5"),
            ],
            receipt=OperationFailed("no invocation receipt"),
        )
        self.assertIsNone(evidence)
        self.assertIsNotNone(error)


class BootstrapGateTests(ConvergeCase):
    """Maintenance, workspace trust and control-plane residue: dry run == execute."""

    def setUp(self) -> None:
        super().setUp()
        # T-5473/T-5474: the planned fire op carries cleared-by keys (the dry run then predicts the
        # earlier clearing ops). These tests pin the REAL gates, so they use the standalone-fire
        # shape without them; the cleared-by prediction is covered in test_persona_installer_phase_q.
        fire = self.fire_op()
        self.op_ = dataclasses.replace(
            fire,
            desired={k: v for k, v in fire.desired.items() if k not in ("residue_cleared_by", "trust_set_by")},
        )
        self.viability = pim._parse_unit_fire_initial_viability_desired(self.op_)
        self.home = self.viability.account_home
        self.state_root = self.viability.persona_state_root
        self.home.mkdir(parents=True, exist_ok=True)
        self.trust()

    def trust(self, value: bool = True) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"projects": {str(self.home / "pantheon-config"): {"hasTrustDialogAccepted": value}}}),
            encoding="utf-8",
        )

    def gates(self, **attrs) -> tuple[GatesBackend, FakeSystemd]:
        backend, fake = self.backend(CLEAN, cls=GatesBackend)
        for key, value in attrs.items():
            setattr(backend, key, value)
        return backend, fake

    def test_ready_host_passes_the_gates(self) -> None:
        backend, _ = self.gates()
        self.assertIsNone(backend._unit_fire_bootstrap_blocker(self.op_))

    def test_armed_maintenance_refuses_in_dry_run_and_in_execute_before_any_start(self) -> None:
        backend, fake = self.gates(maintenance=True)
        verdict = backend.preflight(self.op_)
        self.assertFalse(verdict.ok)
        self.assertIn("seat maintenance", verdict.detail)
        with self.assertRaisesRegex(OperationFailed, "seat maintenance"):
            backend.execute(self.op_, context(str(self.root)))
        self.assertEqual([c for c in fake.log if c[2:3] == ("start",)], [])

    def test_missing_workspace_trust_refuses(self) -> None:
        for label, writer in {
            "false": lambda: self.trust(False),
            "absent-file": lambda: (self.home / ".claude.json").unlink(),
            "garbage": lambda: (self.home / ".claude.json").write_text("{not json", encoding="utf-8"),
        }.items():
            with self.subTest(trust=label):
                self.trust()
                writer()
                backend, _ = self.gates()
                verdict = backend.preflight(self.op_)
                self.assertFalse(verdict.ok)
                self.assertIn("workspace trust missing", verdict.detail)
                with self.assertRaisesRegex(OperationFailed, "workspace trust missing"):
                    backend.execute(self.op_, context(str(self.root)))

    def test_residue_refuses_and_names_it_but_never_the_operator_holds(self) -> None:
        persona = self.op_.provenance["persona"]
        controller = self.state_root / persona / "runtime-controller"
        controller.mkdir(parents=True)
        (self.state_root / persona / "overrides").mkdir(parents=True)
        (self.state_root / persona / "overrides" / "holds.json").write_text("{}", encoding="utf-8")
        (controller / "controller-evidence.jsonl").write_text("", encoding="utf-8")  # kept in place
        (controller / "runtime-operation.lock").write_text("", encoding="utf-8")  # kept in place
        backend, _ = self.gates()
        self.assertIsNone(backend._unit_fire_bootstrap_blocker(self.op_))  # holds + kept files are not residue
        (controller / "runtime.json").write_text("{}", encoding="utf-8")
        (self.state_root / persona / "active-runtime.json").write_text("{}", encoding="utf-8")
        (self.state_root / f"{persona}.policyskip.json").write_text("{}", encoding="utf-8")
        verdict = backend.preflight(self.op_)
        self.assertFalse(verdict.ok)
        for needle in ("runtime-controller/runtime.json", "active-runtime.json", f"{persona}.policyskip.json"):
            self.assertIn(needle, verdict.detail)
        listed = verdict.detail.split("): ", 1)[1]  # the residue list, not the guidance text
        self.assertNotIn("overrides", listed)
        self.assertNotIn("controller-evidence", listed)
        self.assertNotIn("runtime-operation.lock", listed)
        with self.assertRaisesRegex(OperationFailed, "control-plane residue"):
            backend.execute(self.op_, context(str(self.root)))

    def test_gates_apply_only_when_the_installer_will_fire(self) -> None:
        backend, fake = self.backend(CLEAN, cls=GatesBackend, timer_enabled=True)
        backend.maintenance = True
        backend.events["connective.timer-observe"] = pim.OperationEvidence(
            "connective.timer-observe", 1, "timer-observe", True, "seeded", {}
        )
        self.assertTrue(backend.preflight(self.op_).ok)  # enabled timer: no installer fire, no gate


class SimulatedBackendAgreesTests(ConvergeCase):
    """No hardcoded simulated path may disagree with the local backend."""

    seeded = staticmethod(seeded_sim)

    def sim_outcome(self, backend: SimulatedHostBackend, operation: InstallOperation) -> bool:
        try:
            backend.execute(operation, context(str(self.root)))
        except OperationFailed:
            return False
        return True

    def test_sim_preflight_and_execute_follow_the_shared_classifier_like_local(self) -> None:
        op = self.op(self.base_plan, "connective.stale-unit-failure-reset")
        for name, (state, proceeds) in STALE_CASES.items():
            with self.subTest(state=name):
                backend = self.seeded(state)
                self.assertEqual(backend.preflight(op).ok, proceeds)
                self.assertEqual(self.sim_outcome(self.seeded(state), op), proceeds)

    def test_sim_unit_fire_preflight_and_execute_agree_on_idle_and_blockers(self) -> None:
        op = self.op(self.base_plan, "connective.unit-fire")
        stale = self.op(self.base_plan, "connective.stale-unit-failure-reset")
        for name, (state, _proceeds) in STALE_CASES.items():
            with self.subTest(state=name):
                pre = self.seeded(state).preflight(op).ok
                live = self.seeded(state)
                ran = self.sim_outcome(live, stale) and self.sim_outcome(live, op)
                self.assertEqual(pre, ran)
        blocked = self.seeded(MASON)
        blocked.bootstrap_blocker = "mw is in seat maintenance"
        self.assertFalse(blocked.preflight(op).ok)
        self.assertFalse(self.sim_outcome(blocked, op))

    def test_unseeded_sim_keeps_the_fresh_install_behaviour(self) -> None:
        backend = SimulatedHostBackend()
        op = self.op(self.base_plan, "connective.stale-unit-failure-reset")
        self.assertTrue(backend.preflight(op).ok)
        self.assertTrue(self.sim_outcome(backend, op))

    def test_dry_run_through_the_apply_verb_reports_the_refusal_on_a_failed_loaded_seat(self) -> None:
        code, report = pia.dry_run(
            self.manifest_path, "recovery", backend=self.seeded(LOADED_FAILED), repo_root=pia.REPO_ROOT,
            root=self.root / "projection", uid_of=lambda a: 1, euid=1,
        )
        self.assertEqual(code, 1)
        self.assertTrue(
            any("connective.stale-unit-failure-reset" in p for p in report["problems"]), report["problems"]
        )


class MasonShapedPlanTests(ConvergeCase):
    """A fixture shaped like real mason: converged consume service (loaded, inactive/dead,
    result=success, exit 5 inside SuccessExitStatus), byte-identical hand-installed units,
    every periodic already in place. (a) dry-run verdict per op, (b) execute in the simulated
    backend, (c) the two agree."""

    def mason_backend(self, state=MASON) -> SimulatedHostBackend:
        backend = seeded_sim(state)
        plan = self.base_plan
        for operation in plan.operations:
            if operation.kind in {"user-unit-install", "seat-periodic-unit-install"}:
                wanted = pim._strip_unit_banner(pim._desired_content(operation).decode("utf-8"))
                backend.seed_file(operation.target, wanted)  # hand-installed: no banner
        return backend

    def dry_run(self, backend):
        return pia.dry_run(
            self.manifest_path, "recovery", backend=backend, repo_root=pia.REPO_ROOT,
            root=self.root / "projection", uid_of=lambda a: 1, euid=1,
        )

    def test_dry_run_passes_per_op_and_execute_converges_with_adoption(self) -> None:
        backend = self.mason_backend()
        code, report = self.dry_run(backend)
        self.assertEqual(code, 0, report["problems"])
        verdicts = {v["operation_id"]: v for v in report["operations"]}
        self.assertEqual(verdicts["connective.stale-unit-failure-reset"]["classification"], pim.STALE_ADOPTED)
        self.assertEqual(verdicts["connective.service-unit"]["classification"], "adoptable")
        self.assertTrue(all(v["state"] == "pass" or v["state"] == "deferred" for v in report["operations"]))

        plan = self.base_plan
        self.assertEqual(plan.digest, report["plan_digest"])
        applied = self.installer.apply(plan, plan.digest, backend, self.actor(plan), manifest=self.manifest)
        self.assertEqual(applied.status, "applied", applied.errors)
        # (c) every op the dry run passed was executed, none refused
        executed = {m.operation_id for m in applied.receipt.mutations}
        self.assertTrue(set(verdicts) >= executed)
        stale = next(m for m in applied.receipt.mutations if m.operation_id == "connective.stale-unit-failure-reset")
        self.assertFalse(stale.changed)
        self.assertEqual(stale.before["state"], pim.STALE_ADOPTED)
        # adopted units gained the managed banner
        for operation in plan.operations:
            if operation.kind in {"user-unit-install", "seat-periodic-unit-install"}:
                text = backend.entries[operation.target]
                import base64
                self.assertTrue(pim._is_installer_managed(base64.b64decode(text["content_b64"]).decode()))

    def test_a_diverged_unit_or_failed_service_is_refused_in_the_dry_run_with_a_reason(self) -> None:
        backend = self.mason_backend()
        unit = next(o for o in self.base_plan.operations if o.operation_id == "connective.service-unit")
        backend.seed_file(unit.target, "[Service]\nExecStart=/bin/false\n")
        code, report = self.dry_run(backend)
        self.assertEqual(code, 1)
        self.assertTrue(any("connective.service-unit" in p and "diff-digest" in p for p in report["problems"]))
        code, report = self.dry_run(self.mason_backend(LOADED_FAILED))
        self.assertEqual(code, 1)
        self.assertTrue(any("connective.stale-unit-failure-reset" in p for p in report["problems"]))

    def test_dry_run_and_execute_agree_when_refused(self) -> None:
        backend = self.mason_backend(LOADED_FAILED)
        code, report = self.dry_run(backend)
        self.assertEqual(code, 1)
        plan = self.base_plan
        applied = self.installer.apply(plan, plan.digest, backend, self.actor(plan), manifest=self.manifest)
        self.assertNotEqual(applied.status, "applied")
        refused = [v["operation_id"] for v in report["operations"] if v["state"] == "refused"]
        self.assertTrue(any(op in " ".join(applied.errors) for op in refused), (refused, applied.errors))


class AdoptedUnitsVerifySeatTests(ConvergeCase):
    """verify-seat's banner check after adoption (Local write over a tmp root)."""

    def test_adopted_units_carry_the_banner_and_pass_unit_checks(self) -> None:
        backend = RecordingBackend()
        for operation in self.base_plan.operations:
            if operation.kind not in {"user-unit-install", "seat-periodic-unit-install"}:
                continue
            path = Path(operation.target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pim._strip_unit_banner(pim._desired_content(operation).decode("utf-8")), encoding="utf-8")
        before = [c for c in vs.unit_checks(self.base_plan) if not c["ok"]]
        self.assertTrue(before)  # hand-installed: banner absent
        rooted = [self.rooted(o) for o in self.base_plan.operations]
        for operation in rooted:
            if operation.kind in {"user-unit-install", "seat-periodic-unit-install"}:
                self.assertEqual(backend.preflight(operation).classification, "adoptable")
                backend.execute(operation, context(str(self.root)))
        self.assertEqual([c for c in vs.unit_checks(self.base_plan) if not c["ok"]], [])


if __name__ == "__main__":  # keep last (main-guard lint)
    unittest.main()
