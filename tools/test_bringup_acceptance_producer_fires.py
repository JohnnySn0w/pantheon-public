"""Tests for tools.bringup_acceptance.producer_fires (T-2556 phase 5 / T-2564).

Disposable fixtures only: a fake FireHarness (in-memory installed/marker-file
state) stands in for real systemd (no live systemd/sleep touched), matching
the fixture convention set by test_bringup_acceptance_restore.py's fake
CommandRunner. The one place this module touches real state (the Q4 alert
leg's cooldown file) is exercised against a real temp file, through
restore.py's own byte-snapshot/verify primitive.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

from tools.bringup_acceptance import producer_fires
from tools.bringup_acceptance.evidence import RunPaths
from tools.bringup_acceptance.producer_fires import (
    PRODUCER_CATALOG,
    TIMER_CRON_ID,
    UNIT_FAILURE_ALERT_ID,
    WATCH_PRODUCER_ID,
    FireHarness,
    FireOutcome,
    TimerCronModule,
    UnitFailureAlertModule,
    UnitPlan,
    WatchProducerModule,
    _catalog_entries_for,
    nonce_prove_unit,
    reconcile_wake_trigger,
    resolve_wake_family,
)
from tools.bringup_acceptance.types import HardStop, InventoryPathEntry, RunContext, TargetInventory


def _entry(module_id: str, unit_name: str, *, tier: str = "core", status: str = "applies") -> InventoryPathEntry:
    return InventoryPathEntry(path=Path(unit_name), tier=tier, status=status, module_id=module_id)


PERSONA = "testpersona"


def _full_target(*, mailbox_wake_timer: bool = True, mailbox_wake_path: bool = False) -> TargetInventory:
    entries = [
        _entry(TIMER_CRON_ID, f"pantheon-persona-consume@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-context-fill-monitor@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-activity-monitor@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-inbox-consumer@{PERSONA}.timer"),
    ]
    if mailbox_wake_timer:
        entries.append(_entry(TIMER_CRON_ID, f"pantheon-mailbox-wake@{PERSONA}.timer"))
    if mailbox_wake_path:
        entries.append(_entry(WATCH_PRODUCER_ID, f"pantheon-mailbox-wake@{PERSONA}.path"))
    return TargetInventory(persona=PERSONA, entries=tuple(entries))


def _run_context(evidence_root: Path, *, run_id: str = "20260805T000000Z") -> RunContext:
    return RunContext(
        run_id=run_id,
        persona=PERSONA,
        account="test-account",
        harness="claude",
        profile="interactive",
        hub_path=Path("/tmp/hub"),
        state_path=Path("/tmp/state"),
        evidence_root=evidence_root,
        baseline_ref="deadbeef" * 5,
    )


def _make_fake_harness(
    *, wont_fire: frozenset[str] = frozenset(), teardown_fails: frozenset[str] = frozenset()
) -> tuple[FireHarness, list[str], list[str]]:
    """In-memory harness: install() writes the nonce marker unless the real
    unit is named in ``wont_fire``; teardown() removes it, unless named in
    ``teardown_fails`` (to exercise the residue-is-a-HardStop path)."""

    installed: list[str] = []
    torn_down: list[str] = []

    def _install(plan: UnitPlan) -> None:
        installed.append(plan.disposable_unit)
        if plan.unit not in wont_fire:
            plan.marker_path.parent.mkdir(parents=True, exist_ok=True)
            plan.marker_path.write_text(plan.nonce)

    def _await_fire(plan: UnitPlan) -> FireOutcome:
        if plan.marker_path.is_file() and plan.marker_path.read_text() == plan.nonce:
            return FireOutcome(fired=True, detail={"observed_marker": True})
        return FireOutcome(fired=False, detail={"observed_marker": False})

    def _teardown(plan: UnitPlan) -> None:
        if plan.unit in teardown_fails:
            raise RuntimeError("simulated teardown failure")
        if plan.marker_path.exists():
            plan.marker_path.unlink()
        torn_down.append(plan.disposable_unit)

    return FireHarness(install=_install, await_fire=_await_fire, teardown=_teardown), installed, torn_down


class ProducerCatalogTest(unittest.TestCase):
    def test_catalog_covers_every_ticket_named_producer_minus_keep_going(self) -> None:
        names = {spec.name for spec in PRODUCER_CATALOG}
        self.assertEqual(
            names,
            {
                "persona-consume",
                "mailbox-wake-timer",
                "mailbox-wake-path",
                "context-fill-monitor",
                "activity-monitor",
                "inbox-consumer",
            },
        )
        self.assertNotIn("keep-going", names)
        for spec in PRODUCER_CATALOG:
            self.assertFalse("keep-going" in spec.unit_base)

    def test_wake_trigger_family_members_split_across_the_two_module_ids(self) -> None:
        wake_specs = {spec.name: spec for spec in PRODUCER_CATALOG if spec.wake_trigger_family}
        self.assertEqual(wake_specs["mailbox-wake-timer"].module_id, TIMER_CRON_ID)
        self.assertEqual(wake_specs["mailbox-wake-path"].module_id, WATCH_PRODUCER_ID)


class CatalogEntryMatchTest(unittest.TestCase):
    def test_matches_only_the_right_module_id_and_kind(self) -> None:
        target = _full_target(mailbox_wake_path=True)
        timer_spec = next(s for s in PRODUCER_CATALOG if s.name == "mailbox-wake-timer")
        path_spec = next(s for s in PRODUCER_CATALOG if s.name == "mailbox-wake-path")
        self.assertEqual(len(_catalog_entries_for(timer_spec, target)), 1)
        self.assertEqual(len(_catalog_entries_for(path_spec, target)), 1)
        self.assertEqual(
            str(_catalog_entries_for(timer_spec, target)[0].path), f"pantheon-mailbox-wake@{PERSONA}.timer"
        )

    def test_resolve_wake_family_sees_only_installed_members(self) -> None:
        status = resolve_wake_family(_full_target(mailbox_wake_timer=True, mailbox_wake_path=False))
        self.assertIn("mailbox-wake-timer", status.installed)
        self.assertNotIn("mailbox-wake-path", status.installed)


class NonceProveUnitTest(unittest.TestCase):
    def test_fire_within_budget_is_proven(self) -> None:
        harness, installed, torn_down = _make_fake_harness()
        with tempfile.TemporaryDirectory() as tmp:
            plan = UnitPlan(
                unit="pantheon-persona-consume@recovery.timer",
                disposable_unit="pantheon-bringup-acceptance-run-1-x.timer",
                kind="timer",
                nonce="abc123",
                marker_path=Path(tmp) / "marker.nonce",
            )
            outcome = nonce_prove_unit(plan, harness=harness)
        self.assertTrue(outcome.fired)
        self.assertEqual(installed, [plan.disposable_unit])
        self.assertEqual(torn_down, [plan.disposable_unit])

    def test_timeout_is_a_gap_outcome_not_a_hard_stop(self) -> None:
        harness, _, torn_down = _make_fake_harness(wont_fire=frozenset({"never-fires.timer"}))
        with tempfile.TemporaryDirectory() as tmp:
            plan = UnitPlan(
                unit="never-fires.timer",
                disposable_unit="pantheon-bringup-acceptance-run-1-y.timer",
                kind="timer",
                nonce="abc123",
                marker_path=Path(tmp) / "marker.nonce",
            )
            outcome = nonce_prove_unit(plan, harness=harness)  # must not raise
        self.assertFalse(outcome.fired)
        self.assertEqual(torn_down, [plan.disposable_unit])  # teardown still ran

    def test_teardown_failure_is_a_hard_stop_naming_the_disposable_unit(self) -> None:
        harness, _, _ = _make_fake_harness(teardown_fails=frozenset({"flaky.timer"}))
        with tempfile.TemporaryDirectory() as tmp:
            plan = UnitPlan(
                unit="flaky.timer",
                disposable_unit="pantheon-bringup-acceptance-run-1-z.timer",
                kind="timer",
                nonce="abc123",
                marker_path=Path(tmp) / "marker.nonce",
            )
            with self.assertRaisesRegex(HardStop, "pantheon-bringup-acceptance-run-1-z.timer"):
                nonce_prove_unit(plan, harness=harness)


class TimerCronModuleTest(unittest.TestCase):
    def test_all_installed_all_fire_is_proven(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=False)
        declaration = module.declare(target)
        self.assertTrue(declaration.applies)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
            self.assertEqual(evidence.status, "proven")
            self.assertEqual(evidence.detail["gaps"], [])
            restore = module.restore(ctx)
            self.assertTrue(restore.restored)
            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertEqual(row.module_id, TIMER_CRON_ID)

    def test_a_missing_catalog_producer_is_a_named_gap(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = TargetInventory(
            persona=PERSONA,
            entries=(
                _entry(TIMER_CRON_ID, f"pantheon-mailbox-wake@{PERSONA}.timer"),
                _entry(TIMER_CRON_ID, f"pantheon-context-fill-monitor@{PERSONA}.timer"),
                _entry(TIMER_CRON_ID, f"pantheon-activity-monitor@{PERSONA}.timer"),
                # inbox-consumer deliberately absent
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        self.assertEqual(evidence.status, "gap")
        self.assertTrue(any("inbox-consumer" in gap for gap in evidence.detail["gaps"]))

    def test_a_unit_that_never_fires_is_a_named_gap_not_a_hard_stop(self) -> None:
        failing_unit = f"pantheon-activity-monitor@{PERSONA}.timer"
        harness, _, _ = _make_fake_harness(wont_fire=frozenset({failing_unit}))
        module = TimerCronModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=False)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        self.assertEqual(evidence.status, "gap")
        self.assertTrue(any(failing_unit in gap for gap in evidence.detail["gaps"]))

    def test_wake_trigger_absent_with_sibling_installed_is_not_penalized(self) -> None:
        # T-1015 reality reversed: .path installed, .timer absent -- timer-cron
        # must NOT be gapped for the absent .timer half.
        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = _full_target(mailbox_wake_timer=False, mailbox_wake_path=True)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        self.assertEqual(evidence.status, "proven")
        self.assertEqual(
            evidence.detail["producers"]["mailbox-wake-timer"]["note"],
            "not installed on this target; satisfied via sibling module_id for the mailbox-wake family",
        )

    def test_reload_from_disk_when_target_not_passed_directly(self) -> None:
        from tools.bringup_acceptance.inventory import build_inventory  # noqa: F401 -- unused, sanity only
        from tools.bringup_acceptance.evidence import write_inventory

        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=False)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
            write_inventory(paths, target)
            evidence = module.prove(ctx)  # no target= kwarg -- must re-derive from disk
        self.assertEqual(evidence.status, "proven")


class WatchProducerModuleTest(unittest.TestCase):
    def test_wake_trigger_absent_with_sibling_installed_is_not_penalized(self) -> None:
        # T-1015 reality: .timer installed, .path absent -- watch-producer has
        # no other catalog member, so it must end up trivially proven.
        harness, _, _ = _make_fake_harness()
        module = WatchProducerModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=False)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        self.assertEqual(evidence.status, "proven")
        self.assertEqual(evidence.detail["gaps"], [])
        note = evidence.detail["producers"]["mailbox-wake-path"]["note"]
        self.assertIn("satisfied via sibling", note)

    def test_installed_path_unit_is_nonce_fired(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = WatchProducerModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=True)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        self.assertEqual(evidence.status, "proven")
        self.assertTrue(evidence.detail["producers"]["mailbox-wake-path"]["fired"])


class WakeTriggerNeitherInstalledTest(unittest.TestCase):
    """T-2564 condition 3: neither installed anywhere -> exactly-once gap,
    attributed to timer-cron, never dropped, never doubled."""

    def _target_without_any_wake_unit(self) -> TargetInventory:
        return _full_target(mailbox_wake_timer=False, mailbox_wake_path=False)

    def test_timer_cron_carries_the_gap_exactly_once(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = self._target_without_any_wake_unit()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        wake_gaps = [g for g in evidence.detail["gaps"] if "wake-trigger" in g]
        self.assertEqual(len(wake_gaps), 1)
        self.assertEqual(evidence.status, "gap")

    def test_watch_producer_does_not_also_report_the_gap(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = WatchProducerModule(harness=harness)
        target = self._target_without_any_wake_unit()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            evidence = module.prove(ctx, target=target)
        wake_gaps = [g for g in evidence.detail["gaps"] if "wake-trigger" in g]
        self.assertEqual(wake_gaps, [])
        # watch-producer's only catalog member is the absent wake-trigger path
        # unit, so with no gap attributed here it is vacuously proven.
        self.assertEqual(evidence.status, "proven")


class RestoreResidueTest(unittest.TestCase):
    def test_leftover_nonce_marker_after_prove_is_a_hard_stop(self) -> None:
        harness, _, _ = _make_fake_harness()
        module = TimerCronModule(harness=harness)
        target = _full_target(mailbox_wake_timer=True, mailbox_wake_path=False)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            module.prove(ctx, target=target)
            paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
            stray = paths.module_evidence_dir(TIMER_CRON_ID) / "stray-leftover.nonce"
            stray.write_text("residue")
            with self.assertRaisesRegex(HardStop, "stray-leftover.nonce"):
                module.restore(ctx)


class UnitFailureAlertModuleTest(unittest.TestCase):
    def test_declared_absent_when_target_has_no_entries(self) -> None:
        harness, _, _ = _make_fake_harness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = UnitFailureAlertModule(
                harness=harness, state_path=root / "state.json", backup_root=root / "backups",
            )
            target = _full_target()
            declaration = module.declare(target)
            self.assertFalse(declaration.applies)
            ctx = _run_context(root)
            evidence = module.prove(ctx, target=target)
            self.assertEqual(evidence.status, "proven")
            self.assertTrue(evidence.detail["declared_absent"])
            row = module.report(evidence)
            self.assertEqual(row.status, "declared_absent")

    def test_present_entry_nonce_fires_and_reaches_disposable_mailbox(self) -> None:
        harness, _, _ = _make_fake_harness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "unit_failure_alert_state.json"
            original = b'{"pantheon-mailbox-wake@recovery.service": 123}'
            state_path.write_bytes(original)
            module = UnitFailureAlertModule(
                harness=harness, state_path=state_path, backup_root=root / "backups", account="host-account",
            )
            target = TargetInventory(
                persona=PERSONA,
                entries=(_entry(UNIT_FAILURE_ALERT_ID, "some-host-hygiene.timer", tier="optional"),),
            )
            declaration = module.declare(target)
            self.assertTrue(declaration.applies)
            ctx = _run_context(root)
            evidence = module.prove(ctx, target=target)
            self.assertEqual(evidence.status, "proven")
            self.assertTrue(evidence.detail["producers"]["some-host-hygiene.timer"]["fired"])
            restore = module.restore(ctx)
            self.assertTrue(restore.restored)
            self.assertEqual(state_path.read_bytes(), original)  # byte-identical after restore
            row = module.report(evidence)
            self.assertEqual(row.status, "proven")

    def test_corrupted_revert_of_the_real_cooldown_file_hard_stops_naming_it(self) -> None:
        """T-2564 condition 1: adversarial real-state-touch coverage -- if the
        revert doesn't actually take (simulated here the same way
        test_bringup_acceptance_restore.py does: force restore_to to be a
        no-op so the file is left mutated), restore() must HardStop naming
        the file, never silently report restored=True."""

        harness, _, _ = _make_fake_harness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "unit_failure_alert_state.json"
            state_path.write_bytes(b"original-cooldown-bytes")
            module = UnitFailureAlertModule(
                harness=harness, state_path=state_path, backup_root=root / "backups",
            )
            target = TargetInventory(
                persona=PERSONA,
                entries=(_entry(UNIT_FAILURE_ALERT_ID, "some-host-hygiene.timer", tier="optional"),),
            )
            ctx = _run_context(root)
            module.prove(ctx, target=target)
            # Simulate the alert leg's real OnFailure invocation mutating the
            # cooldown file, and a broken restore that fails to undo it.
            state_path.write_bytes(b"mutated-by-real-alert-invocation")
            with patch.object(producer_fires, "restore_to", lambda baseline: None):
                with self.assertRaisesRegex(HardStop, "unit_failure_alert_state.json"):
                    module.restore(ctx)


class ReconcileWakeTriggerTest(unittest.TestCase):
    def _detail(self, *, installed: bool, fired: bool | None) -> Mapping[str, object]:
        return {"producers": {"mailbox-wake-timer": {"wake_trigger": {"installed": installed, "fired": fired}}}}

    def _empty_detail(self) -> Mapping[str, object]:
        return {"producers": {"mailbox-wake-path": {"wake_trigger": {"installed": False, "fired": None}}}}

    def test_neither_installed_is_a_gap(self) -> None:
        verdict = reconcile_wake_trigger(
            timer_detail=self._detail(installed=False, fired=None), watch_detail=self._empty_detail()
        )
        self.assertFalse(verdict.satisfied)
        self.assertIn("neither", verdict.gap)

    def test_one_installed_and_fired_is_satisfied(self) -> None:
        verdict = reconcile_wake_trigger(
            timer_detail=self._detail(installed=True, fired=True), watch_detail=self._empty_detail()
        )
        self.assertTrue(verdict.satisfied)
        self.assertIsNone(verdict.gap)

    def test_both_installed_one_fails_but_sibling_succeeds_is_satisfied(self) -> None:
        timer_detail = self._detail(installed=True, fired=False)
        watch_detail = {"producers": {"mailbox-wake-path": {"wake_trigger": {"installed": True, "fired": True}}}}
        verdict = reconcile_wake_trigger(timer_detail=timer_detail, watch_detail=watch_detail)
        self.assertTrue(verdict.satisfied)

    def test_all_installed_candidates_fail_is_a_gap(self) -> None:
        timer_detail = self._detail(installed=True, fired=False)
        watch_detail = {"producers": {"mailbox-wake-path": {"wake_trigger": {"installed": True, "fired": False}}}}
        verdict = reconcile_wake_trigger(timer_detail=timer_detail, watch_detail=watch_detail)
        self.assertFalse(verdict.satisfied)
        self.assertIn("none fired", verdict.gap)


if __name__ == "__main__":
    unittest.main()
