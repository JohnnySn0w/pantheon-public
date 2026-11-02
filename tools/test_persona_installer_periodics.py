"""T-5447: the installer installs every seat-core periodic timer.

Simulated backend / temp roots only. Nothing here touches a real home, a real
user manager, or a live seat.
"""

from __future__ import annotations

import base64
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import persona_installer as _pim
from tools.persona_installer import (
    EXECUTION_CONTEXT_ALL_IN_ONE,
    LocalHostBackend,
    Manifest,
    MutationRecord,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_persona_installer import (
    REPO_ROOT,
    ManifestPinnedInstaller,
    _manifest_dict,
    _with_roots,
)

SYSTEMD = REPO_ROOT / "tools" / "systemd"
# (op-name, timer unit stem for recovery, template file stems)
PERIODIC_TIMERS = {
    "quota": "pantheon-persona-quota@recovery.timer",
    "inbox-consumer": "pantheon-inbox-consumer@recovery.timer",
    "keep-going": "pantheon-keep-going@recovery.timer",
    "restart-drain": "pantheon-restart-drain.timer",
}
HERMES_SENTINEL = "HERME" + "S.md"


def _strip_banner(content: str) -> str:
    lines = content.splitlines(keepends=True)
    assert lines[0].startswith("# " + _pim.MANAGED_MARKER)
    assert lines[1].startswith("# instance=")
    return "".join(lines[2:])


class PeriodicCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.set_manifest(_manifest_dict(second_persona=True))
        self.installer = ManifestPinnedInstaller(REPO_ROOT)

    def set_manifest(self, data: dict) -> None:
        self.manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.manifest = Manifest.load(self.manifest_path)

    def plan(self, persona: str = "recovery"):
        return self.installer.plan(self.manifest, persona, root=self.root / "projection")

    def actor(self, plan):
        capabilities = frozenset(
            o.provenance["required_capability"]
            for o in plan.operations
            if o.provenance.get("required_capability")
        ) | frozenset(a.required_capability for a in plan.operator_actions)
        return _pim.ActorContext(
            uid=1000, persona="consultant", role="installer", instance=plan.instance,
            capabilities=capabilities,
        )

    def op(self, plan, operation_id):
        return next(o for o in plan.operations if o.operation_id == operation_id)

    def assert_host_restored(self, backend) -> None:
        from tools.test_persona_installer import assert_sim_host_restored

        assert_sim_host_restored(self, backend)

    def ids(self, plan):
        return [o.operation_id for o in plan.operations]


class PlanShapeTests(PeriodicCase):
    def test_live_reconcile_workflow_contains_only_keep_going_and_legacy_migration(self) -> None:
        from tools import persona_installer_apply as apply_cli

        data = _manifest_dict(second_persona=True)
        data["roles"]["peer-daemon-consultant"]["legacy_units"] = ["codex-keep-going"]
        self.set_manifest(data)
        _installer, plan, _manifest = apply_cli.build_plan(
            self.manifest_path,
            "consultant",
            repo_root=REPO_ROOT,
            root=self.root / "projection",
            workflow="reconcile-keep-going",
        )
        self.assertEqual(
            [operation.operation_id for operation in plan.operations],
            [
                "connective.config-dir",
                "connective.systemd-dir",
                "connective.unit-dir",
                "connective.local-dir",
                "connective.local-state-dir",
                "connective.pantheon-state-dir",
                "connective.installer-state-dir",
                "connective.state-dir",
                "connective.receipt-dir",
                "periodic.keep-going.service-unit",
                "periodic.keep-going.timer-unit",
                "service.codex-keep-going.decommission",
                "periodic.daemon-reload",
                "periodic.keep-going.timer-enable",
                "periodic.keep-going.timer-observe",
            ],
        )
        self.assertNotIn("harness.direct-fire", {op.operation_id for op in plan.operations})
        enable = next(op for op in plan.operations if op.operation_id == "periodic.keep-going.timer-enable")
        self.assertEqual(enable.desired["after"], "periodic.daemon-reload")

        backend = SimulatedHostBackend()
        decommission = next(op for op in plan.operations if op.kind == "seat-service-decommission")
        unit_dir = decommission.desired["unit_dir"].rstrip("/")
        for name in ("codex-keep-going.timer", "codex-keep-going.service"):
            backend.seed_file(f"{unit_dir}/{name}", "legacy\n")
        backend.enabled_timers.add("codex-keep-going.timer")
        report = _installer.apply(plan, plan.digest, backend, self.actor(plan), manifest=_manifest)
        self.assertEqual(report.status, "applied", report.errors)
        self.assertIn("pantheon-keep-going@consultant.timer", backend.enabled_timers)
        self.assertNotIn("codex-keep-going.timer", backend.enabled_timers)
        self.assertIsNotNone(report.receipt)

    def test_seat_core_unit_installs_are_banner_plus_verbatim_template(self) -> None:
        plan = self.plan()
        expected = {
            "periodic.inbox-consumer.service-unit": "pantheon-inbox-consumer@.service",
            "periodic.inbox-consumer.timer-unit": "pantheon-inbox-consumer@.timer",
            "periodic.keep-going.service-unit": "pantheon-keep-going@.service",
            "periodic.keep-going.timer-unit": "pantheon-keep-going@.timer",
            "periodic.restart-drain.service-unit": "pantheon-restart-drain.service",
            "periodic.restart-drain.timer-unit": "pantheon-restart-drain.timer",
            "periodic.unit-failure-alert.service-unit": "pantheon-unit-failure-alert@.service",
        }
        for operation_id, template in expected.items():
            with self.subTest(op=operation_id):
                operation = self.op(plan, operation_id)
                self.assertEqual(operation.kind, "seat-periodic-unit-install")
                self.assertTrue(operation.target.endswith("/" + template))
                content = operation.desired["content"]
                self.assertEqual(_strip_banner(content), (SYSTEMD / template).read_text(encoding="utf-8"))

    def test_no_new_direct_or_unit_fire_and_no_unit_failure_alert_enable(self) -> None:
        plan = self.plan()
        kinds = [o.kind for o in plan.operations]
        self.assertEqual(kinds.count("direct-fire"), 1)
        self.assertEqual(kinds.count("unit-fire"), 1)
        enabled = [o.target for o in plan.operations if o.kind in {"timer-enable", "seat-periodic-timer-enable"}]
        self.assertFalse([t for t in enabled if "unit-failure-alert" in t])

    def test_every_periodic_timer_is_enabled_and_observed_after_consume(self) -> None:
        plan = self.plan()
        ids = self.ids(plan)
        consume_observe = ids.index("connective.timer-observe")
        for name, timer in PERIODIC_TIMERS.items():
            with self.subTest(timer=name):
                enable = self.op(plan, f"periodic.{name}.timer-enable")
                observe = self.op(plan, f"periodic.{name}.timer-observe")
                self.assertEqual((enable.kind, enable.target), ("seat-periodic-timer-enable", timer))
                self.assertEqual((observe.kind, observe.target), ("seat-periodic-timer-observe", timer))
                self.assertEqual(observe.desired["next_elapse"], "finite")
                self.assertGreater(ids.index(enable.operation_id), consume_observe)
                self.assertLess(ids.index(enable.operation_id), ids.index(observe.operation_id))
        self.assertLess(ids.index("periodic.daemon-reload"), ids.index("periodic.quota.timer-enable"))

    def test_quota_timer_enabled_for_a_persona_outside_the_quota_allowlist(self) -> None:
        # the fixture manifest carries no quota_check.personas allowlist at all
        self.assertNotIn("personas", self.manifest.installer.get("quota_check", {}))
        plan = self.plan("recovery")
        self.op(plan, "periodic.quota.timer-enable")  # StopIteration if gated away

    def test_codex_personas_receive_keep_going_from_the_same_installer_bundle(self) -> None:
        # The installer must not silently omit the anti-idle leg merely because the
        # selected/default harness is Codex. keep_going owns the harness-specific
        # submit profile at delivery time.
        for persona in ("codex", "watchman"):
            with self.subTest(persona=persona):
                self.assertTrue(_pim._keep_going_planned(persona))
                binding = dataclasses.replace(self.manifest.binding("recovery"), persona=persona)
                periodics = _pim._seat_core_periodics(
                    REPO_ROOT, self.manifest, binding, "# managed\n", "test-revision")
                names = {name for name, _timer, _service in periodics.timers}
                self.assertIn("keep-going", names)

    def test_codex_legacy_cadence_is_retired_before_shared_timer_can_start(self) -> None:
        data = _manifest_dict(second_persona=True)
        data["roles"]["peer-daemon-consultant"]["legacy_units"] = ["codex-keep-going"]
        self.set_manifest(data)
        plan = self.plan("consultant")
        operation = self.op(plan, "service.codex-keep-going.decommission")
        self.assertEqual(
            operation.desired["units"],
            "codex-keep-going.timer,codex-keep-going.service",
        )
        ids = self.ids(plan)
        self.assertLess(ids.index(operation.operation_id), ids.index("harness.direct-fire"))
        self.assertLess(
            ids.index(operation.operation_id),
            ids.index("periodic.keep-going.timer-enable"),
        )

    def test_live_codex_binding_declares_legacy_cadence_migration(self) -> None:
        live = Manifest.load(REPO_ROOT / "instance.yaml")
        self.assertIn("codex-keep-going", live.binding("codex").legacy_units)

    def test_restart_drain_dropin_matches_live_content(self) -> None:
        plan = self.plan()
        operation = self.op(plan, "periodic.restart-drain.dropin")
        self.assertTrue(operation.target.endswith("pantheon-restart-drain.service.d/enable.conf"))
        body = _strip_banner(operation.desired["content"])
        self.assertIn("[Service]\nEnvironment=PANTHEON_RESTART_DRAIN_ENABLED=1\n", body)
        self.assertEqual(operation.kind, "seat-periodic-unit-install")

    def test_mailbox_wake_units_follow_the_wake_lists_and_match_onboard_bytes(self) -> None:
        data = _manifest_dict(second_persona=True)
        data["persona_installer"]["mailbox"]["wake_timer_personas"] = ["recovery"]
        data["persona_installer"]["mailbox"]["wake_timer_secondary_personas"] = ["consultant"]
        self.set_manifest(data)
        plan = self.plan("recovery")
        from tools import mailbox_onboard as mo

        banner = mo._unit_banner(plan.instance, "recovery", plan.provenance["source_revision"])
        for suffix in ("service", "timer"):
            operation = self.op(plan, f"periodic.mailbox-wake.{suffix}-unit")
            template = SYSTEMD / f"pantheon-mailbox-wake@.{suffix}"
            self.assertEqual(operation.desired["content"].encode(), mo._rendered_unit(template, banner))
        self.op(plan, "periodic.mailbox-wake.timer-enable")
        # the deprecated .path unit is never installed (mirrors mailbox_onboard)
        self.assertFalse([o for o in plan.operations if o.target.endswith("mailbox-wake@.path")])
        # secondary seats get it too; an unlisted persona gets none
        self.assertIn("periodic.mailbox-wake.timer-enable", self.ids(self.plan("consultant")))
        data["persona_installer"]["mailbox"]["wake_timer_secondary_personas"] = []
        self.set_manifest(data)
        self.assertNotIn("periodic.mailbox-wake.timer-enable", self.ids(self.plan("consultant")))

    def test_live_manifest_declares_every_controller_codex_seat_wakeable(self) -> None:
        live = Manifest.load(REPO_ROOT / "instance.yaml")
        mailbox = live.installer["mailbox"]
        covered = set(mailbox["wake_timer_personas"]) | set(
            mailbox["wake_timer_secondary_personas"])
        self.assertTrue({"orchestrator", "specialist", "recovery", "codex", "watchman"} <= covered)

    def test_mailbox_onboard_is_imported_lazily(self) -> None:
        source = (REPO_ROOT / "tools" / "persona_installer.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            if "mailbox_onboard" in line and line.lstrip().startswith(("import ", "from ")):
                self.assertTrue(line.startswith((" ", "\t")), f"module-level import: {line!r}")

    def test_source_has_no_forbidden_byte_sequence(self) -> None:
        for path in (REPO_ROOT / "tools" / "persona_installer.py", Path(__file__)):
            self.assertNotIn(HERMES_SENTINEL, path.read_text(encoding="utf-8").replace(
                '"HERME" + "S.md"', ""))


class AllowlistTests(unittest.TestCase):
    def test_allowlist_is_closed(self) -> None:
        home = Path("/h/acct")
        for unit, template in (
            ("pantheon-persona-consume@recovery.timer", "pantheon-persona-consume@.timer"),
            ("pantheon-persona-quota@recovery.timer", "pantheon-persona-quota@.timer"),
            ("pantheon-inbox-consumer@recovery.timer", "pantheon-inbox-consumer@.timer"),
            ("pantheon-keep-going@recovery.timer", "pantheon-keep-going@.timer"),
            ("pantheon-mailbox-wake@recovery.timer", "pantheon-mailbox-wake@.timer"),
            ("pantheon-restart-drain.timer", "pantheon-restart-drain.timer"),
        ):
            with self.subTest(unit=unit):
                self.assertEqual(_pim._seat_timer_template_unit(unit), template)
                self.assertEqual(
                    _pim._seat_timer_link_path(home, unit),
                    home / ".config/systemd/user/timers.target.wants" / unit,
                )
        for bad in (
            "pantheon-activity-monitor@recovery.timer",
            "pantheon-unit-failure-alert@x.timer",
            "pantheon-keep-going@recovery.service",
            "pantheon-keep-going@..timer",
            "pantheon-keep-going@a/b.timer",
            "pantheon-restart-drain@recovery.timer",
            "evil.timer",
        ):
            with self.subTest(bad=bad), self.assertRaises(OperationFailed):
                _pim._seat_timer_template_unit(bad)


class SimulatedApplyTests(PeriodicCase):
    def apply(self, backend=None, persona="recovery"):
        plan = self.plan(persona)
        backend = backend or SimulatedHostBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        return plan, backend, report

    def test_apply_enables_and_observes_every_periodic_then_rolls_back(self) -> None:
        plan, backend, report = self.apply()
        self.assertEqual(report.status, "applied", report.errors)
        for name, timer in PERIODIC_TIMERS.items():
            self.assertIn(timer, backend.enabled_timers)
            self.assertIn(f"periodic.{name}.timer-observe", backend.events)
        self.assertNotIn("periodic.unit-failure-alert.timer-enable", backend.events)
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        self.assert_host_restored(backend)
        for timer in PERIODIC_TIMERS.values():
            self.assertNotIn(timer, backend.enabled_timers)

    def test_consume_failure_leaves_periodics_unenabled(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(fail_operation="connective.unit-fire")
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertNotEqual(report.status, "applied")
        self.assertEqual(
            {t for t in backend.enabled_timers if "consume" not in t}, set()
        )

    def test_unfinite_next_elapse_fails_periodic_observe(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        observe = self.op(plan, "periodic.keep-going.timer-observe")
        backend.enabled_timers.add(observe.target)
        backend.finite_next_elapse = False
        _, evidence = backend.execute(observe, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertFalse(evidence.ok)

    def test_preexisting_enabled_periodic_is_tolerated_and_not_rolled_back(self) -> None:
        plan = self.plan()
        enable = self.op(plan, "periodic.inbox-consumer.timer-enable")
        backend = SimulatedHostBackend()
        backend.seed_enabled_timer(
            enable.target,
            enable.desired["enablement_link_path"],
            enable.desired["canonical_template_path"],
        )
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        mutation = next(
            m for m in report.receipt.mutations
            if m.operation_id == "periodic.inbox-consumer.timer-enable"
        )
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        rolled = self.installer.rollback(
            report.receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest
        )
        self.assertEqual(rolled.status, "rolled-back", rolled)
        self.assertIn(enable.target, backend.enabled_timers)
        self.assertEqual(
            backend.entries[enable.desired["enablement_link_path"]]["state"], "symlink"
        )

    def test_consume_absence_check_semantics_unchanged(self) -> None:
        plan = self.plan()
        absence = self.op(plan, "connective.timer-enablement-absent")
        backend = SimulatedHostBackend()
        backend.seed_enabled_timer(
            "pantheon-persona-consume@recovery.timer", absence.target, absence.desired["canonical_template_path"]
        )
        self.assertFalse(backend.preflight(absence).ok)

    def test_codex_legacy_timer_is_disabled_and_quarantined_reversibly(self) -> None:
        data = _manifest_dict(second_persona=True)
        data["roles"]["peer-daemon-consultant"]["legacy_units"] = ["codex-keep-going"]
        self.set_manifest(data)
        plan = self.plan("consultant")
        operation = self.op(plan, "service.codex-keep-going.decommission")
        backend = SimulatedHostBackend()
        unit_dir = operation.desired["unit_dir"].rstrip("/")
        for name in ("codex-keep-going.timer", "codex-keep-going.service"):
            backend.seed_file(f"{unit_dir}/{name}", "legacy\n")
        backend.enabled_timers.add("codex-keep-going.timer")
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(evidence.ok)
        self.assertNotIn("codex-keep-going.timer", backend.enabled_timers)
        self.assertEqual(mutation.before["timer_was_enabled"], "true")
        self.assertEqual(
            backend.entries[
                f"{operation.desired['quarantine_dir']}/codex-keep-going.timer"
            ]["state"],
            "file",
        )
        backend.rollback_mutation(mutation)
        self.assertIn("codex-keep-going.timer", backend.enabled_timers)
        self.assertIn(f"{unit_dir}/codex-keep-going.timer", backend.entries)


class AdoptionTests(PeriodicCase):
    def unit_op(self, plan):
        return self.op(plan, "periodic.keep-going.service-unit")

    def test_banner_less_identical_file_is_adopted_and_inverse_restores_bytes_and_mode(self) -> None:
        plan = self.plan()
        operation = self.unit_op(plan)
        original = _strip_banner(operation.desired["content"])
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, original, mode="0600")
        result = backend.preflight(operation)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.classification, "adoptable")
        mutation, evidence = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(mutation.changed)
        self.assertTrue(_pim._is_installer_managed(
            base64.b64decode(backend.entries[operation.target]["content_b64"]).decode()))
        backend.rollback_mutation(mutation)
        restored = backend.entries[operation.target]
        self.assertEqual(base64.b64decode(restored["content_b64"]).decode(), original)
        self.assertEqual(restored["mode"], "0600")

    def test_diverged_file_is_refused_with_bounded_diff_digest(self) -> None:
        plan = self.plan()
        operation = self.unit_op(plan)
        diverged = _strip_banner(operation.desired["content"]).replace("90s", "91s") + "x" * 400 + "\n" * 3
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, diverged)
        result = backend.preflight(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertIn("unknown regular file", result.detail)
        self.assertIn("diff-digest", result.detail)
        self.assertLess(len(result.detail), 1600)

    def test_non_unit_atomic_write_is_still_refused_without_adoption(self) -> None:
        plan = self.plan()
        operation = self.op(plan, "persona.binding")
        backend = SimulatedHostBackend()
        backend.seed_file(operation.target, "hand written\n")
        self.assertFalse(backend.preflight(operation).ok)

    def test_local_backend_preflight_adopts_and_refuses(self) -> None:
        plan = self.plan()
        operation = self.unit_op(plan)
        target = self.root / "local-home" / "unit.service"
        target.parent.mkdir()
        local_op = dataclasses.replace(
            operation,
            target=str(target),
            provenance={**operation.provenance, "install_root": "/"},
        )
        backend = LocalHostBackend.__new__(LocalHostBackend)
        if True:
            target.write_text(_strip_banner(operation.desired["content"]), encoding="utf-8")
            target.chmod(0o640)
            adopt = backend.preflight(local_op)
            self.assertTrue(adopt.ok, adopt)
            self.assertEqual(adopt.classification, "adoptable")
            mutation = backend._write_file(local_op)
            self.assertTrue(mutation.changed)
            backend.rollback_mutation(mutation, allowed_roots=(str(target.parent),))
            self.assertEqual(target.read_text(encoding="utf-8"), _strip_banner(operation.desired["content"]))
            self.assertEqual(target.stat().st_mode & 0o7777, 0o640)
            target.write_text("different\n", encoding="utf-8")
            refuse = backend.preflight(local_op)
            self.assertFalse(refuse.ok)
            self.assertIn("diff-digest", refuse.detail)


class LocalPeriodicRollbackTests(PeriodicCase):
    def test_local_rollback_of_unowned_periodic_enable_is_a_noop(self) -> None:
        plan = self.plan()
        enable = self.op(plan, "periodic.keep-going.timer-enable")
        mutation = MutationRecord(
            enable.operation_id, "seat-periodic-timer-enable", enable.target, enable.owner, False,
            {"state": "enabled", "owned": "false"}, "timer-enabled",
        )
        backend = LocalHostBackend.__new__(LocalHostBackend)
        backend.__dict__["_user_manager"] = mock.Mock()
        result = backend.can_rollback(mutation)
        self.assertTrue(result.ok)
        self.assertEqual(result.classification, "not-applied")
        backend.rollback_mutation(mutation, allowed_roots=(str(self.root),))
        backend.__dict__["_user_manager"].disable_now.assert_not_called()

    def _local_with_manager(self, state: str):
        import subprocess

        backend = LocalHostBackend.__new__(LocalHostBackend)
        manager = mock.Mock()
        ok = mock.Mock(transport_evidence=subprocess.CompletedProcess([], 0, "", ""))
        for name in ("enable_now", "disable_now", "reload", "stop_unit", "reset_failed_unit"):
            getattr(manager, name).return_value = ok
        backend.__dict__["_user_manager"] = manager
        timer_state = _pim.TimerUnitState(state, "loaded", state, "inactive", "dead")
        backend._timer_unit_state = lambda unit: timer_state
        backend._stop_unit_or_converged_not_loaded = lambda unit: timer_state
        return backend, manager

    def _rooted_enable_op(self):
        plan = self.plan()
        home = self.root / "acct"
        timer = "pantheon-keep-going@recovery.timer"
        operation = self.op(plan, "periodic.keep-going.timer-enable")
        return dataclasses.replace(
            operation,
            desired={
                **operation.desired,
                "enablement_link_path": str(_pim._seat_timer_link_path(home, timer)),
                "canonical_template_path": str(_pim._seat_timer_template_path(home, timer)),
            },
            provenance={**operation.provenance, "install_root": str(self.root)},
        ), home

    def test_local_enable_creates_owned_link_and_rollback_removes_only_it(self) -> None:
        operation, home = self._rooted_enable_op()
        backend, manager = self._local_with_manager("disabled")
        mutation, observed = backend._execute_periodic_enable(operation)
        link = Path(operation.desired["enablement_link_path"])
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before["owned"], "true")
        self.assertTrue(link.is_symlink())
        manager.enable_now.assert_called_once_with(operation.target)
        self.assertTrue(backend.can_rollback(mutation).ok)
        backend.rollback_mutation(mutation, allowed_roots=(str(home),))
        self.assertFalse(link.is_symlink())
        manager.disable_now.assert_called_once_with(operation.target)

    def test_local_enable_tolerates_already_enabled_timer_without_ownership(self) -> None:
        operation, home = self._rooted_enable_op()
        backend, manager = self._local_with_manager("enabled")
        mutation, _ = backend._execute_periodic_enable(operation)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        manager.enable_now.assert_not_called()
        backend.rollback_mutation(mutation, allowed_roots=(str(home),))
        manager.disable_now.assert_not_called()

    def test_local_refuses_non_allowlisted_timer_paths(self) -> None:
        operation, _ = self._rooted_enable_op()
        bad = dataclasses.replace(operation, target="pantheon-activity-monitor@recovery.timer")
        backend, _ = self._local_with_manager("disabled")
        with self.assertRaises(OperationFailed):
            backend._execute_periodic_enable(bad)


class BrokerPartitionTests(PeriodicCase):
    """The periodic kinds are seat-local residue: the broker plan is unchanged."""

    def test_real_plan_partitions_without_raising_and_periodics_are_residue(self) -> None:
        from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        for persona in ("recovery", "consultant"):
            with self.subTest(persona=persona):
                plan = self.plan(persona)
                broker_plan, residue = partition_installer_plan(plan)
                residue_ids = {o.operation_id for o in residue}
                periodic_ids = {o.operation_id for o in plan.operations if o.operation_id.startswith("periodic.")}
                self.assertTrue(periodic_ids)
                self.assertTrue(periodic_ids <= residue_ids)
                # T-5470 (HO ruling 2026-10-26): routed launches have their own bound; B8 counts the rest
                from tools.host_control.envelope import EXECUTOR_KIND_CLASSES

                self.assertLessEqual(len([o for o in residue if o.kind not in EXECUTOR_KIND_CLASSES]), SEAT_LOCAL_RESIDUE_LIMIT)
                self.assertFalse(
                    {o.kind for o in broker_plan.operations if o.operation_id.startswith("periodic.")}
                )
                for operation in plan.operations:
                    if operation.operation_id.startswith("periodic."):
                        self.assertNotIn(operation.kind, EXECUTOR_KIND_CLASSES)

    def test_real_instance_yaml_residue_stays_under_the_limit(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        installer = ManifestPinnedInstaller(REPO_ROOT)
        counts = {}
        for raw in manifest.roles.values():
            persona = raw.get("persona") if isinstance(raw, dict) else None
            if not persona:
                continue
            # T-5448 (PR-1 reviewer nit a): no swallowed planning errors. Every
            # persona in the real instance.yaml plans today; one that stops
            # planning must fail this test loudly, not drop out of the count.
            plan = installer.plan(manifest, persona, root=self.root / f"real-{persona}")
            # T-5470 partition routing (HO ruling 2026-10-26): routed launches have their own
            # bound; B8 cap unchanged. Count only the out-of-envelope residue (a residue op of
            # an in-envelope kind is a routed launch or dependent).
            from tools.host_control.envelope import EXECUTOR_KIND_CLASSES

            counts[persona] = len(
                [op for op in partition_installer_plan(plan)[1] if op.kind not in EXECUTOR_KIND_CLASSES]
            )
        self.assertTrue(counts)
        self.assertLessEqual(max(counts.values()), SEAT_LOCAL_RESIDUE_LIMIT, counts)

    def test_partition_refuses_an_admitted_op_ordered_after_a_residue_op_or_kind(self) -> None:
        # T-5448 (PR-1 reviewer nit b)
        from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OutOfEnvelopeKinds
        from tools.host_control.planner import partition_installer_plan

        plan = self.plan()
        residue_id = "periodic.daemon-reload"
        # T-5470: on a claude seat the consume timer-enable is routed to the residue
        # now, so the victim is an op that stays ADMITTED (the mailbox probe).
        victim = self.op(plan, "connective.mailbox-probe")
        self.assertIn(victim.kind, EXECUTOR_KIND_CLASSES)
        self.assertIn(victim.operation_id, {o.operation_id for o in partition_installer_plan(plan)[0].operations})
        for dependency, field in (
            (residue_id, "desired"),
            ("seat-periodic-daemon-reload", "desired"),  # by residue KIND
            (residue_id, "precondition"),
            (" service.runtime-controller.enable-observe ", "desired"),  # whitespace-tolerant
        ):
            with self.subTest(dependency=dependency, field=field):
                source = dict(getattr(victim, field))
                source["after"] = (source.get("after", "") + "," + dependency).strip(",")
                bad = dataclasses.replace(victim, **{field: source})
                tampered = dataclasses.replace(
                    plan,
                    operations=tuple(bad if o.operation_id == victim.operation_id else o for o in plan.operations),
                )
                with self.assertRaises(OutOfEnvelopeKinds):
                    partition_installer_plan(tampered)

    def test_real_plans_never_order_an_admitted_op_after_residue(self) -> None:
        from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
        from tools.host_control.planner import partition_installer_plan

        for persona in ("recovery", "consultant"):
            plan = self.plan(persona)
            partition_installer_plan(plan)  # would raise OutOfEnvelopeKinds
            residue_ids = {o.operation_id for o in plan.operations if o.kind not in EXECUTOR_KIND_CLASSES}
            residue_kinds = {o.kind for o in plan.operations if o.kind not in EXECUTOR_KIND_CLASSES}
            for operation in plan.operations:
                if operation.kind not in EXECUTOR_KIND_CLASSES:
                    continue
                for source in (operation.precondition, operation.desired):
                    for dep in (d.strip() for d in str(source.get("after", "")).split(",")):
                        self.assertNotIn(dep, residue_ids | residue_kinds, (persona, operation.operation_id))


if __name__ == "__main__":
    unittest.main()
