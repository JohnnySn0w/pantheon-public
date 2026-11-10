"""T-5448: the installer brings up the per-persona runtime controller and
decommissions the legacy ho-resumer.

Simulated backend / tmp roots / mocks only. Nothing here touches a real home, a
real user manager, a live seat, tmux, claude or codex.
"""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_installer as _pim
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OUT_OF_ENVELOPE_KINDS
from tools.persona_installer import (
    HO_RESUMER_SERVICE_UNIT,
    HO_RESUMER_TIMER_UNIT,
    EXECUTION_CONTEXT_ALL_IN_ONE,
    LocalHostBackend,
    MutationRecord,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_persona_installer import REPO_ROOT, _manifest_dict
from tools.test_persona_installer_periodics import PeriodicCase, _strip_banner

SYSTEMD = REPO_ROOT / "tools" / "systemd"
CONTROLLER_ID = "service.runtime-controller.enable-observe"
UNIT_ID = "service.runtime-controller.unit"
RESUMER_ID = "service.ho-resumer.decommission"
SERVICE_KINDS = ("seat-service-enable-observe", "seat-service-decommission")


def legacy_ho_resumer_manifest() -> dict:
    """T-5633: the periodic fixture with recovery's binding declaring the legacy
    ho-resumer unit set, so recovery's plan carries the decommission op."""

    data = _manifest_dict(second_persona=True)
    data["roles"]["recovery"]["legacy_units"] = ["ho-resumer"]
    return data


class LegacyHoResumerCase(PeriodicCase):
    """PeriodicCase whose recovery binding declares legacy_units: [ho-resumer]."""

    def setUp(self) -> None:
        super().setUp()
        self.set_manifest(legacy_ho_resumer_manifest())


class UndeclaredFixtureTests(PeriodicCase):
    def test_the_undeclared_fixture_plans_no_decommission(self) -> None:
        # T-5633: without legacy_units the op is absent for every fixture seat
        for persona in ("recovery", "consultant"):
            with self.subTest(persona=persona):
                self.assertNotIn(RESUMER_ID, self.ids(self.plan(persona)))
                self.assertNotIn("seat-service-decommission", {o.kind for o in self.plan(persona).operations})


class ServicePlanShapeTests(LegacyHoResumerCase):
    def test_controller_unit_is_banner_plus_verbatim_template_through_the_periodic_install_kind(self) -> None:
        operation = self.op(self.plan(), UNIT_ID)
        self.assertEqual(operation.kind, "seat-periodic-unit-install")
        self.assertTrue(operation.target.endswith("/pantheon-runtime-controller@.service"))
        self.assertEqual(
            _strip_banner(operation.desired["content"]),
            (SYSTEMD / "pantheon-runtime-controller@.service").read_text(encoding="utf-8"),
        )

    def test_enable_observe_desired_carries_the_controller_tmux_target(self) -> None:
        # T-5449: the pre-start pane pid is read from <session>:<window>.<pane>
        operation = self.op(self.plan(), CONTROLLER_ID)
        self.assertEqual(operation.desired["tmux_target"], "pantheon-recovery:harness.0")

    def test_enable_observe_desired_carries_window_ref_and_provenance_flag(self) -> None:
        plan = self.plan()
        operation = self.op(plan, CONTROLLER_ID)
        self.assertEqual(operation.kind, "seat-service-enable-observe")
        self.assertEqual(operation.target, "pantheon-runtime-controller@recovery.service")
        desired = operation.desired
        self.assertEqual(desired["observe_window_seconds"], "20")
        self.assertEqual(desired["observe_interval_seconds"], "2")
        self.assertEqual(desired["require_source_provenance"], "true")
        self.assertEqual(desired["after"], "connective.unit-fire")
        # primary runtime ref of the role: "<harness>/<profile>"
        self.assertRegex(desired["expect_selected"], r"^[a-z0-9-]+/[a-z0-9-]+$")
        self.assertTrue(desired["enablement_link_path"].endswith(
            "/default.target.wants/pantheon-runtime-controller@recovery.service"))
        self.assertTrue(desired["canonical_template_path"].endswith(
            "/.config/systemd/user/pantheon-runtime-controller@.service"))

    def test_order_is_decommission_then_fire_then_controller_then_consume_timer_then_periodics(self) -> None:
        ids = self.ids(self.plan())
        at = ids.index
        self.assertLess(at(UNIT_ID), at(CONTROLLER_ID))
        self.assertLess(at(RESUMER_ID), at("harness.direct-fire"))
        self.assertLess(at(RESUMER_ID), at("connective.unit-fire"))
        self.assertLess(at("connective.unit-fire"), at(CONTROLLER_ID))
        self.assertLess(at(CONTROLLER_ID), at("connective.mailbox-probe"))
        self.assertLess(at(CONTROLLER_ID), at("connective.timer-enable"))
        self.assertLess(at("connective.timer-enable"), at("periodic.quota.timer-enable"))

    def test_periodics_wait_on_the_controller(self) -> None:
        plan = self.plan()
        for operation in plan.operations:
            if operation.kind == "seat-periodic-timer-enable":
                with self.subTest(op=operation.operation_id):
                    self.assertIn(CONTROLLER_ID, operation.desired["after"].split(","))

    def test_decommission_is_a_closed_list_with_no_ordering_dependency(self) -> None:
        operation = self.op(self.plan(), RESUMER_ID)
        self.assertEqual(operation.kind, "seat-service-decommission")
        self.assertEqual(operation.desired["units"], f"{HO_RESUMER_TIMER_UNIT},{HO_RESUMER_SERVICE_UNIT}")
        self.assertNotIn("after", operation.desired)
        self.assertNotIn("after", operation.precondition)
        self.assertTrue(operation.desired["quarantine_dir"].endswith("/quarantine/ho-resumer"))

    def test_no_admitted_op_references_a_service_residue_op_or_kind(self) -> None:
        plan = self.plan()
        residue_ids = {o.operation_id for o in plan.operations if o.kind not in EXECUTOR_KIND_CLASSES}
        residue_kinds = {o.kind for o in plan.operations if o.kind not in EXECUTOR_KIND_CLASSES}
        for operation in plan.operations:
            if operation.kind not in EXECUTOR_KIND_CLASSES:
                continue
            for source in (operation.precondition, operation.desired):
                for dep in str(source.get("after", "")).split(","):
                    dep = dep.strip()
                    with self.subTest(op=operation.operation_id, dep=dep):
                        self.assertNotIn(dep, residue_ids)
                        self.assertNotIn(dep, residue_kinds)

    def test_service_kinds_are_out_of_envelope_residue_and_partition_cleanly(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        for kind in SERVICE_KINDS:
            self.assertIn(kind, OUT_OF_ENVELOPE_KINDS)
            self.assertNotIn(kind, EXECUTOR_KIND_CLASSES)
            self.assertIn(kind, _pim.ACTIVATION_OPERATION_KINDS)
        plan = self.plan()
        broker, residue = partition_installer_plan(plan)
        residue_ids = {o.operation_id for o in residue}
        self.assertTrue({UNIT_ID, CONTROLLER_ID, RESUMER_ID} <= residue_ids)
        self.assertFalse({o.operation_id for o in broker.operations} & residue_ids)
        # T-5470 (HO ruling 2026-10-26): routed launches have their own bound; B8 counts the rest
        # T-5961 (orchestrator ruling on B8): the fleet-usage periodic-timer ops are a separate bucket
        from tools.host_control.planner import residue_bucket_counts

        self.assertLessEqual(residue_bucket_counts(
            (o.kind, o.target) for o in residue if o.kind not in EXECUTOR_KIND_CLASSES)[0],
            SEAT_LOCAL_RESIDUE_LIMIT)

    def test_controller_template_is_not_modified_by_the_installer_source(self) -> None:
        text = (SYSTEMD / "pantheon-runtime-controller@.service").read_text(encoding="utf-8")
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=always", text)


class ResidueKindsTests(LegacyHoResumerCase):
    def test_residue_kinds_constant_is_exactly_what_partition_leaves_and_no_executor_kind(self) -> None:
        from tools.host_control.planner import partition_installer_plan

        self.assertFalse(_pim.SEAT_LOCAL_RESIDUE_KINDS & set(EXECUTOR_KIND_CLASSES))
        seen: set[str] = set()
        for persona in ("recovery", "consultant"):
            # T-5470: routed launches (in-envelope kinds) are set aside; the
            # constant names the out-of-envelope residue kinds.
            seen |= {
                op.kind for op in partition_installer_plan(self.plan(persona))[1]
                if op.kind not in EXECUTOR_KIND_CLASSES
            }
        self.assertEqual(seen, set(_pim.SEAT_LOCAL_RESIDUE_KINDS))


class ParseAndJudgeTests(unittest.TestCase):
    GOOD = "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=0\n"

    @staticmethod
    def _parse(stdout: str) -> dict:
        # T-5517: the typed strict read is the single parser
        try:
            snap = _pim._systemctl_snapshot_from_stdout(
                "c.service", _pim.ReadPurpose.SERVICE_BRINGUP_OBSERVE, stdout)
        except _pim._us.UserManagerProtocolError as exc:
            raise OperationFailed(str(exc)) from None
        return _pim._controller_values_from_snapshot(snap)

    def test_parse_accepts_the_four_canonical_properties(self) -> None:
        self.assertEqual(
            self._parse(self.GOOD),
            {"ActiveState": "active", "SubState": "running", "MainPID": "4242", "NRestarts": "0"},
        )

    def test_parse_is_fail_closed(self) -> None:
        for bad in (
            "",
            "ActiveState=active\nSubState=running\nMainPID=4242\n",  # missing NRestarts
            self.GOOD + "NRestarts=0\n",  # duplicate
            self.GOOD + "Extra=1\n",  # unexpected key
            "ActiveState=active\nSubState=running\nMainPID=abc\nNRestarts=0\n",
            "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=\n",
            "ActiveState=active\nSubState=running\nMainPID=04242\nNRestarts=0\n",
            "garbled line\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(OperationFailed):
                self._parse(bad)

    def test_judge(self) -> None:
        ok = {"ActiveState": "active", "SubState": "running", "MainPID": "7", "NRestarts": "0"}
        _pim._judge_controller_samples([ok, dict(ok)])
        for bad in (
            [],
            [ok, {**ok, "MainPID": "8"}],
            [ok, {**ok, "MainPID": "0"}],
            [{**ok, "MainPID": "0"}],
            [ok, {**ok, "NRestarts": "1"}],
            [ok, {**ok, "ActiveState": "activating", "SubState": "auto-restart"}],
            [ok, {**ok, "SubState": "exited"}],
        ):
            with self.subTest(bad=bad), self.assertRaises(OperationFailed):
                _pim._judge_controller_samples(bad)

    def test_judge_adopted_controller_against_its_first_sample(self) -> None:
        # T-5478: artisan's controller, up since 10-21 with NRestarts=2, failed the observe
        hist = {"ActiveState": "active", "SubState": "running", "MainPID": "7", "NRestarts": "2"}
        _pim._judge_controller_samples([hist, dict(hist)], adopted=True)
        with self.assertRaises(OperationFailed):
            _pim._judge_controller_samples([hist, {**hist, "NRestarts": "3"}], adopted=True)
        with self.assertRaises(OperationFailed):
            _pim._judge_controller_samples([hist, {**hist, "MainPID": "8"}], adopted=True)
        with self.assertRaises(OperationFailed):  # a controller this op started must be at 0
            _pim._judge_controller_samples([hist, dict(hist)])


class ServiceSimulatedApplyTests(LegacyHoResumerCase):
    CONTROLLER = "pantheon-runtime-controller@recovery.service"

    def apply(self, backend=None):
        plan = self.plan()
        backend = backend or SimulatedHostBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        return plan, backend, report

    def test_apply_brings_up_the_controller_in_order_then_rolls_back(self) -> None:
        plan, backend, report = self.apply()
        self.assertEqual(report.status, "applied", report.errors)
        self.assertIn(self.CONTROLLER, backend.enabled_services)
        events = backend.events
        self.assertLess(events.index("connective.unit-fire"), events.index(CONTROLLER_ID))
        self.assertLess(events.index(CONTROLLER_ID), events.index("connective.timer-enable"))
        self.assertLess(events.index("connective.timer-enable"), events.index("periodic.quota.timer-enable"))
        self.assertLess(events.index(RESUMER_ID), events.index("connective.unit-fire"))
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        self.assertNotIn(self.CONTROLLER, backend.enabled_services)
        self.assert_host_restored(backend)

    def test_preenabled_controller_is_tolerated_and_not_rolled_back(self) -> None:
        plan = self.plan()
        operation = self.op(plan, CONTROLLER_ID)
        backend = SimulatedHostBackend()
        backend.enabled_services.add(self.CONTROLLER)
        backend.entries[operation.desired["enablement_link_path"]] = {
            "state": "symlink",
            "target": operation.desired["canonical_template_path"],
            "fingerprint": "x",
        }
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        mutation = next(m for m in report.receipt.mutations if m.operation_id == CONTROLLER_ID)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        self.assertIn(self.CONTROLLER, backend.enabled_services)

    def test_observe_failure_after_enable_leaves_the_controller_disabled_and_receipt_shows_recovery(self) -> None:
        for fault in ("inactive", "pid-change", "restart"):
            with self.subTest(fault=fault):
                backend = SimulatedHostBackend()
                backend.controller_fault = fault
                plan, backend, report = self.apply(backend)
                self.assertEqual(report.status, "failed")
                self.assertNotIn(self.CONTROLLER, backend.enabled_services)
                self.assertIn(f"{CONTROLLER_ID}: restored", report.rollback)
                self.assertNotIn("connective.timer-enable", backend.events)
                self.assertNotIn("periodic.quota.timer-enable", backend.events)

    def test_viability_mismatch_fails_closed_and_recovers(self) -> None:
        backend = SimulatedHostBackend()
        backend.controller_viability_ok = False
        plan, backend, report = self.apply(backend)
        self.assertEqual(report.status, "failed")
        self.assertIn("initial-viability", " ".join(report.errors))
        self.assertNotIn(self.CONTROLLER, backend.enabled_services)
        self.assertIn(f"{CONTROLLER_ID}: restored", report.rollback)

    def test_adopts_a_byte_identical_hand_installed_controller_unit(self) -> None:
        plan = self.plan()
        operation = self.op(plan, UNIT_ID)
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, _strip_banner(operation.desired["content"]), mode="0644")
        result = backend.preflight(operation)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.classification, "adoptable")


class DecommissionSimulatedTests(LegacyHoResumerCase):
    def seed(self, backend, operation, *, enabled=True):
        unit_dir = operation.desired["unit_dir"]
        backend.seed_file(f"{unit_dir}/{HO_RESUMER_TIMER_UNIT}", "[Timer]\nOnCalendar=x\n")
        backend.seed_file(f"{unit_dir}/{HO_RESUMER_SERVICE_UNIT}", "[Service]\nExecStart=/bin/true\n")
        if enabled:
            backend.seed_enabled_timer(
                HO_RESUMER_TIMER_UNIT,
                f"{unit_dir}/timers.target.wants/{HO_RESUMER_TIMER_UNIT}",
                f"{unit_dir}/{HO_RESUMER_TIMER_UNIT}",
            )

    def test_present_resumer_is_disabled_quarantined_and_inverse_restores_it(self) -> None:
        plan = self.plan()
        operation = self.op(plan, RESUMER_ID)
        backend = SimulatedHostBackend()
        self.seed(backend, operation)
        unit_dir, q = operation.desired["unit_dir"], operation.desired["quarantine_dir"]
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(evidence.ok)
        self.assertTrue(mutation.changed)
        self.assertNotIn(HO_RESUMER_TIMER_UNIT, backend.enabled_timers)
        for name in (HO_RESUMER_TIMER_UNIT, HO_RESUMER_SERVICE_UNIT):
            self.assertNotIn(f"{unit_dir}/{name}", backend.entries)
            self.assertIn(f"{q}/{name}", backend.entries)  # moved, never deleted
        self.assertTrue(backend.can_rollback(mutation).ok)
        backend.rollback_mutation(mutation)
        for name in (HO_RESUMER_TIMER_UNIT, HO_RESUMER_SERVICE_UNIT):
            self.assertIn(f"{unit_dir}/{name}", backend.entries)
            self.assertNotIn(f"{q}/{name}", backend.entries)
        self.assertIn(HO_RESUMER_TIMER_UNIT, backend.enabled_timers)

    def test_absent_resumer_is_a_noop(self) -> None:
        operation = self.op(self.plan(), RESUMER_ID)
        backend = SimulatedHostBackend()
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(evidence.ok)
        self.assertFalse(mutation.changed)
        self.assertTrue(backend.can_rollback(mutation).ok)

    def test_only_the_closed_unit_list_is_accepted(self) -> None:
        operation = self.op(self.plan(), RESUMER_ID)
        evil = dataclasses.replace(
            operation, desired={**operation.desired, "units": "pantheon-inbox-consumer@x.timer"}
        )
        self.assertFalse(SimulatedHostBackend().preflight(evil).ok)

    def test_quarantine_collision_is_refused(self) -> None:
        operation = self.op(self.plan(), RESUMER_ID)
        backend = SimulatedHostBackend()
        self.seed(backend, operation)
        backend.seed_file(f"{operation.desired['quarantine_dir']}/{HO_RESUMER_TIMER_UNIT}", "old\n")
        self.assertFalse(backend.preflight(operation).ok)

    def test_full_apply_with_a_present_resumer_then_rollback_restores_it(self) -> None:
        plan = self.plan()
        operation = self.op(plan, RESUMER_ID)
        backend = SimulatedHostBackend()
        self.seed(backend, operation)
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        self.assertNotIn(f"{operation.desired['unit_dir']}/{HO_RESUMER_TIMER_UNIT}", backend.entries)
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        self.assertIn(f"{operation.desired['unit_dir']}/{HO_RESUMER_TIMER_UNIT}", backend.entries)


class LocalServiceTests(LegacyHoResumerCase):
    """LocalHostBackend code paths against tmp roots and a mocked manager/_run only."""

    GOOD = "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=0\n"

    def rooted_enable_op(self):
        plan = self.plan()
        home = self.root / "acct"
        unit = "pantheon-runtime-controller@recovery.service"
        operation = self.op(plan, CONTROLLER_ID)
        return dataclasses.replace(
            operation,
            desired={
                **operation.desired,
                "enablement_link_path": str(_pim._controller_link_path(home, unit)),
                "canonical_template_path": str(_pim._controller_template_path(home)),
                "persona_state_root": str(home / ".local/state/pantheon/personas"),
            },
            provenance={**operation.provenance, "install_root": str(self.root)},
        ), home

    def backend(self, *, unit_state="disabled", shows=None, viability="ok"):
        backend = LocalHostBackend.__new__(LocalHostBackend)
        manager = mock.Mock()
        ok = mock.Mock(transport_evidence=subprocess.CompletedProcess([], 0, "", ""))
        for name in ("enable_now", "disable_now", "reload", "stop_unit"):
            getattr(manager, name).return_value = ok
        backend.__dict__["_user_manager"] = manager
        timer_state = _pim.TimerUnitState(unit_state, "loaded", unit_state, "inactive", "dead")
        backend._timer_unit_state = lambda unit: timer_state
        backend._stop_unit_or_converged_not_loaded = lambda unit: timer_state
        backend._observe_sleep = lambda seconds: None
        outputs = list(shows) if shows is not None else None
        calls = []

        def run(argv, *, timeout=None):
            calls.append(tuple(argv))
            if argv[0] == "tmux":  # no pane by default (T-5449); never consumes a show output
                return subprocess.CompletedProcess(argv, 1, "", "can't find session")
            if outputs is None:
                return subprocess.CompletedProcess(argv, 0, self.GOOD, "")
            return subprocess.CompletedProcess(argv, 0, outputs.pop(0) if len(outputs) > 1 else outputs[0], "")

        backend._run = run
        # T-5452: the observe reads through the typed port; the real text transport
        # (SystemctlUserManager) is driven by the same recorded _run seam.
        text_port = _pim.SystemctlUserManager(lambda argv, **kw: backend._run(argv, **kw))
        manager.read_unit.side_effect = text_port.read_unit
        if viability == "ok":
            backend._controller_viability = lambda op: {"initial_viability_ok": "true"}
        else:
            def fail(op):
                raise OperationFailed("initial-viability is not selected=claude/x")

            backend._controller_viability = fail
        return backend, manager, calls

    def test_enable_observe_passes_and_rollback_disables_only_the_owned_enable(self) -> None:
        operation, home = self.rooted_enable_op()
        backend, manager, calls = self.backend()
        mutation, observed = backend._execute_service_enable_observe(operation)
        link = Path(operation.desired["enablement_link_path"])
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before["owned"], "true")
        self.assertTrue(link.is_symlink())
        manager.enable_now.assert_called_once_with(operation.target)
        self.assertEqual(observed["controller_samples"], "11")  # 20s window / 2s + 1
        self.assertEqual(observed["controller_main_pid"], "4242")
        # every state read is the read-only four-property show, plus the one
        # read-only tmux pane-pid capture taken BEFORE the controller start (T-5449)
        for call in calls:
            if call[0] == "tmux":
                self.assertEqual(call[1], "display-message")
                continue
            self.assertEqual(call[:4], ("systemctl", "--user", "show", operation.target))
            self.assertNotIn(call[1:3], {("--user", "start")})
        self.assertTrue(backend.can_rollback(mutation).ok)
        backend.rollback_mutation(mutation, allowed_roots=(str(home),))
        manager.disable_now.assert_called_once_with(operation.target)
        self.assertFalse(link.is_symlink())

    def test_pre_start_pane_pid_is_captured_before_the_start_and_recorded(self) -> None:
        operation, _home = self.rooted_enable_op()
        backend, manager, _calls = self.backend()
        order = []
        started = mock.Mock(transport_evidence=subprocess.CompletedProcess([], 0, "", ""))
        manager.enable_now.side_effect = lambda unit: order.append("start") or started
        inner = backend._run

        def run(argv, *, timeout=None):
            if argv[0] == "tmux":
                order.append("tmux")
                self.assertEqual(
                    tuple(argv[:5]), ("tmux", "display-message", "-p", "-t", "pantheon-recovery:harness.0"))
                return subprocess.CompletedProcess(argv, 0, "pantheon-recovery\tharness\t0\t777\n", "")
            return inner(argv, timeout=timeout)

        backend._run = run
        _mutation, observed = backend._execute_service_enable_observe(operation)
        self.assertEqual(observed["controller_pre_start_pane_pid"], "777")
        self.assertEqual(order[:2], ["tmux", "start"])

    def test_pre_start_pane_pid_is_absent_without_a_pane_or_on_identity_mismatch(self) -> None:
        for stdout, rc in (("", 1), ("pantheon-other\tharness\t0\t9\n", 0), ("garbage\n", 0)):
            with self.subTest(stdout=stdout, rc=rc):
                operation, _home = self.rooted_enable_op()
                Path(operation.desired["enablement_link_path"]).unlink(missing_ok=True)
                backend, _manager, _calls = self.backend()
                inner = backend._run

                def run(argv, *, timeout=None, _inner=inner, _out=stdout, _rc=rc):
                    if argv[0] == "tmux":
                        return subprocess.CompletedProcess(argv, _rc, _out, "can't find pane")
                    return _inner(argv, timeout=timeout)

                backend._run = run
                _m, observed = backend._execute_service_enable_observe(operation)
                self.assertEqual(observed["controller_pre_start_pane_pid"], "absent")

    def test_tolerated_already_enabled_controller_is_observed_but_never_disabled(self) -> None:
        operation, home = self.rooted_enable_op()
        backend, manager, _ = self.backend(unit_state="enabled")
        mutation, _ = backend._execute_service_enable_observe(operation)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        manager.enable_now.assert_not_called()
        backend.rollback_mutation(mutation, allowed_roots=(str(home),))
        manager.disable_now.assert_not_called()

    def test_already_enabled_controller_with_earlier_restarts_is_adopted(self) -> None:
        # T-5478: historical NRestarts on an adopted controller is not a failure ...
        operation, _ = self.rooted_enable_op()
        hist = self.GOOD.replace("NRestarts=0", "NRestarts=2")
        backend, _, _ = self.backend(unit_state="enabled", shows=[hist])
        mutation, evidence = backend._execute_service_enable_observe(operation)
        self.assertFalse(mutation.changed)
        self.assertEqual(evidence["controller_nrestarts"], "2")
        # ... but a restart inside the window still is
        backend, _, _ = self.backend(unit_state="enabled", shows=[hist, hist.replace("NRestarts=2", "NRestarts=3")])
        with self.assertRaises(OperationFailed):
            backend._execute_service_enable_observe(operation)

    def test_restart_pid_change_or_garbled_output_fails_with_enable_evidence(self) -> None:
        stable = self.GOOD
        for name, outputs in (
            ("restart", [stable, stable.replace("NRestarts=0", "NRestarts=1")]),
            ("pid-change", [stable, stable.replace("4242", "4343")]),
            ("inactive", [stable, stable.replace("running", "auto-restart").replace("=active", "=activating")]),
            ("garbled", [stable, "ActiveState=active\n"]),
        ):
            with self.subTest(case=name):
                operation, _ = self.rooted_enable_op()
                backend, manager, _ = self.backend(shows=outputs)
                with self.assertRaises(OperationFailed) as ctx:
                    backend._execute_service_enable_observe(operation)
                # enable HAS run: the engine needs this to build the recovery mutation
                self.assertEqual(ctx.exception.evidence.get("enable_command_issued"), "true")
                recovery = backend.issued_timer_enable_recovery_mutation(operation, ctx.exception.evidence)
                self.assertIsNotNone(recovery)
                self.assertTrue(recovery.changed)
                self.assertEqual(recovery.before["owned"], "true")
                backend.rollback_mutation(recovery, allowed_roots=(str(self.root),))
                manager.disable_now.assert_called_once_with(operation.target)

    def test_every_fail_closed_observe_case_still_refuses_through_the_port(self) -> None:
        # T-5452: the typed port must refuse exactly what the private parse refused.
        good = self.GOOD
        bad_outputs = {
            "missing-property": "ActiveState=active\nSubState=running\nMainPID=4242\n",
            "duplicate-property": good + "NRestarts=0\n",
            "unexpected-property": good + "Result=success\n",
            "garbage-mainpid": good.replace("MainPID=4242", "MainPID=abc"),
            "empty-nrestarts": good.replace("NRestarts=0", "NRestarts="),
            "leading-zero-pid": good.replace("4242", "04242"),
            "trailing-space-nrestarts": good.replace("NRestarts=0", "NRestarts=0 "),
            "empty-output": "",
        }
        for name, text in bad_outputs.items():
            with self.subTest(case=name):
                operation, _ = self.rooted_enable_op()
                Path(operation.desired["enablement_link_path"]).unlink(missing_ok=True)
                backend, _manager, _calls = self.backend(shows=[text])
                with self.assertRaises(OperationFailed) as ctx:
                    backend._execute_service_enable_observe(operation)
                self.assertIn("controller", str(ctx.exception))
                self.assertEqual(ctx.exception.evidence.get("enable_command_issued"), "true")
        # a fresh start with NRestarts != 0 is refused on the first sample
        operation, _ = self.rooted_enable_op()
        Path(operation.desired["enablement_link_path"]).unlink(missing_ok=True)
        backend, _manager, _calls = self.backend(shows=[good.replace("NRestarts=0", "NRestarts=1")])
        with self.assertRaises(OperationFailed):
            backend._execute_service_enable_observe(operation)
        # a nonzero manager exit refuses with the bare stderr text
        operation, _ = self.rooted_enable_op()
        Path(operation.desired["enablement_link_path"]).unlink(missing_ok=True)
        backend, _manager, _calls = self.backend()
        backend._run = lambda argv, *, timeout=None: subprocess.CompletedProcess(argv, 1, "", "Unit gone")
        with self.assertRaises(OperationFailed) as ctx:
            backend._execute_service_enable_observe(operation)
        self.assertIn("Unit gone", str(ctx.exception))

    def test_viability_failure_fails_with_enable_evidence(self) -> None:
        operation, _ = self.rooted_enable_op()
        backend, manager, _ = self.backend(viability="bad")
        with self.assertRaises(OperationFailed) as ctx:
            backend._execute_service_enable_observe(operation)
        self.assertEqual(ctx.exception.evidence.get("enable_command_issued"), "true")

    def test_enable_command_failure_carries_evidence_and_unloaded_unit_fails_early(self) -> None:
        operation, _ = self.rooted_enable_op()
        backend, manager, _ = self.backend()
        manager.enable_now.return_value = mock.Mock(
            transport_evidence=subprocess.CompletedProcess([], 1, "", "boom")
        )
        with self.assertRaises(OperationFailed):
            backend._execute_service_enable_observe(operation)
        backend, manager, _ = self.backend(unit_state="not-found")
        with self.assertRaises(OperationFailed):
            backend._execute_service_enable_observe(operation)
        manager.enable_now.assert_not_called()

    def test_paths_must_be_account_derived_and_persona_matched(self) -> None:
        operation, _ = self.rooted_enable_op()
        backend, _, _ = self.backend()
        for bad in (
            dataclasses.replace(operation, desired={**operation.desired, "enablement_link_path": "/etc/x"}),
            dataclasses.replace(operation, desired={**operation.desired, "canonical_template_path": "/etc/y"}),
            dataclasses.replace(operation, target="pantheon-runtime-controller@other.service"),
        ):
            with self.assertRaises(OperationFailed):
                backend._execute_service_enable_observe(bad)

    def test_real_viability_check_reads_a_tmp_state_root_and_fails_closed(self) -> None:
        operation, home = self.rooted_enable_op()
        backend, _, _ = self.backend()
        del backend._controller_viability  # use the real in-process reader
        with self.assertRaises(OperationFailed):
            backend._controller_viability(operation)  # no record in the tmp root

    def decommission_op(self):
        operation = self.op(self.plan(), RESUMER_ID)
        unit_dir = self.root / "acct" / ".config/systemd/user"
        unit_dir.mkdir(parents=True)
        return dataclasses.replace(
            operation,
            desired={
                **operation.desired,
                "unit_dir": str(unit_dir),
                "quarantine_dir": str(self.root / "state/recovery/quarantine/ho-resumer"),
            },
        ), unit_dir

    def test_local_decommission_moves_files_disables_timer_and_inverse_restores(self) -> None:
        operation, unit_dir = self.decommission_op()
        (unit_dir / HO_RESUMER_TIMER_UNIT).write_text("T\n")
        (unit_dir / HO_RESUMER_SERVICE_UNIT).write_text("S\n")
        backend, manager, _ = self.backend(unit_state="enabled")
        mutation, observed = backend._execute_service_decommission(operation)
        q = Path(operation.desired["quarantine_dir"])
        self.assertTrue(mutation.changed)
        manager.disable_now.assert_called_once_with(HO_RESUMER_TIMER_UNIT)
        self.assertFalse((unit_dir / HO_RESUMER_TIMER_UNIT).exists())
        self.assertEqual((q / HO_RESUMER_TIMER_UNIT).read_text(), "T\n")
        self.assertEqual((q / HO_RESUMER_SERVICE_UNIT).read_text(), "S\n")
        self.assertTrue(backend.can_rollback(mutation).ok)
        backend.rollback_mutation(mutation, allowed_roots=(str(self.root),))
        self.assertEqual((unit_dir / HO_RESUMER_TIMER_UNIT).read_text(), "T\n")
        self.assertFalse((q / HO_RESUMER_TIMER_UNIT).exists())
        manager.enable_now.assert_called_once_with(HO_RESUMER_TIMER_UNIT)

    def test_local_decommission_absent_is_a_noop_and_collision_is_refused(self) -> None:
        operation, unit_dir = self.decommission_op()
        backend, manager, _ = self.backend()
        mutation, _ = backend._execute_service_decommission(operation)
        self.assertFalse(mutation.changed)
        manager.disable_now.assert_not_called()
        (unit_dir / HO_RESUMER_TIMER_UNIT).write_text("T\n")
        q = Path(operation.desired["quarantine_dir"])
        q.mkdir(parents=True)
        (q / HO_RESUMER_TIMER_UNIT).write_text("old\n")
        with self.assertRaises(OperationFailed):
            backend._execute_service_decommission(operation)
        self.assertTrue((unit_dir / HO_RESUMER_TIMER_UNIT).exists())

    def test_local_decommission_rollback_refuses_on_drift(self) -> None:
        operation, unit_dir = self.decommission_op()
        (unit_dir / HO_RESUMER_TIMER_UNIT).write_text("T\n")
        backend, _, _ = self.backend(unit_state="enabled")
        mutation, _ = backend._execute_service_decommission(operation)
        (unit_dir / HO_RESUMER_TIMER_UNIT).write_text("re-created\n")
        self.assertFalse(backend.can_rollback(mutation).ok)
        forged = dataclasses.replace(
            mutation, before={**mutation.before, "moved": "pantheon-inbox-consumer@x.timer"}
        )
        self.assertFalse(backend.can_rollback(forged).ok)

    def test_local_decommission_refuses_a_non_closed_unit_list(self) -> None:
        operation, _ = self.decommission_op()
        evil = dataclasses.replace(operation, desired={**operation.desired, "units": "other.timer"})
        backend, _, _ = self.backend()
        with self.assertRaises(OperationFailed):
            backend._execute_service_decommission(evil)


class ResidueBudgetTests(PeriodicCase):
    def test_every_real_instance_persona_plans_and_stays_within_the_residue_limit(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan
        from tools.persona_installer import Manifest
        from tools.test_persona_installer import ManifestPinnedInstaller

        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        installer = ManifestPinnedInstaller(REPO_ROOT)
        counts = {}
        for raw in manifest.roles.values():
            persona = raw.get("persona") if isinstance(raw, dict) else None
            if persona:
                plan = installer.plan(manifest, persona, root=self.root / f"real-{persona}")
                # T-5470 partition routing (HO ruling 2026-10-26): routed launches have their own
                # bound; B8 cap unchanged. Count only the out-of-envelope residue (a residue op of
                # an in-envelope kind is a routed launch or dependent).
                from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
                from tools.host_control.planner import residue_bucket_counts

                # T-5961 (orchestrator ruling on B8): the general bucket; the fleet-usage
                # periodic-timer ops have their own cap (test_persona_installer_periodics)
                counts[persona] = residue_bucket_counts(
                    (op.kind, op.target)
                    for op in partition_installer_plan(plan)[1]
                    if op.kind not in EXECUTOR_KIND_CLASSES
                )[0]
        self.assertTrue(counts)
        self.assertLessEqual(max(counts.values()), SEAT_LOCAL_RESIDUE_LIMIT, counts)
        # PR-1 left orchestrator at 25; PR-2 adds exactly 3 (unit, enable-observe, decommission);
        # T-5454 adds the shell-box settings op to every claude seat (orchestrator 29 of 32)
        # T-5473/T-5474 add the residue quarantine and the workspace-trust op (orchestrator 31 of 32)
        # T-5608 admits the residue quarantine (files.seat_root): it leaves the residue (30 of 32)
        # T-5633: orchestrator's binding declares legacy_units: [ho-resumer], so it keeps the decommission (30)
        # T-5480: the codex shell-box hook op is residue on every seat with a codex runtime (orchestrator 31 of 32)
        self.assertEqual(counts["orchestrator"], 31, counts)


if __name__ == "__main__":
    unittest.main()
