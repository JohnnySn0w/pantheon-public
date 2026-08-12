"""Tests for tools.bringup_acceptance.keep_going_kicker (T-2556 phase 3 continuation / T-2578).

Disposable fixtures only for the module-level declare/prove/restore/report
plumbing (temp evidence root, temp disposable mailbox hub) -- matching the
fixture convention set by test_bringup_acceptance_mailbox_roundtrip.py /
test_bringup_acceptance_producer_fires.py. The mailbox race-dedupe leg
exercises the REAL tools.mailbox + tools.keep_going.send_mailbox against a
throwaway hub; the rate-limit and race-gap legs exercise the REAL
tools.keep_going.decide_nudge -- neither is reimplemented here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import keep_going
from tools.bringup_acceptance import keep_going_kicker as kgk
from tools.bringup_acceptance.types import HardStop, InventoryPathEntry, RunContext, TargetInventory

PERSONA = "testpersona"


def _run_context(evidence_root: Path, hub_path: Path, state_path: Path,
                  *, run_id: str = "20260805T062000Z") -> RunContext:
    return RunContext(
        run_id=run_id, persona=PERSONA, account="test-account", harness="claude",
        profile="interactive", hub_path=hub_path, state_path=state_path,
        evidence_root=evidence_root, baseline_ref="deadbeef" * 5,
    )


class OracleCoverageTest(unittest.TestCase):
    def test_every_rule_id_present_and_sourced(self) -> None:
        details = kgk.capture_keep_going_oracle()
        rule_ids = {d.rule_id for d in details}
        self.assertEqual(
            rule_ids,
            {
                "idle-gate", "adaptive-backoff", "queue-aware-floor",
                "session-reset", "mailbox-delivery-race-dedupe",
                "send-keys-no-transport-dedupe",
            },
        )
        for detail in details:
            self.assertTrue(detail.source, f"{detail.rule_id} has no source pointer")
            self.assertIn("keep_going.py", detail.source)


class InterpretFiringTest(unittest.TestCase):
    def test_active_with_trigger_is_confirmed_firing(self) -> None:
        raw = kgk.RawFiringObservation(
            agent="x", timer_active_state="active", timer_unit_file_state="enabled",
            timer_last_trigger="Wed 2026-08-05 06:11:30 UTC",
            state_file_present=True, state_file={"last_nudge_ts": 1},
        )
        self.assertEqual(kgk.interpret_firing(raw).verdict, "confirmed-firing")

    def test_inactive_is_not_firing_observed(self) -> None:
        raw = kgk.RawFiringObservation(
            agent="x", timer_active_state="inactive", timer_unit_file_state="disabled",
            timer_last_trigger=None, state_file_present=False, state_file=None,
        )
        self.assertEqual(kgk.interpret_firing(raw).verdict, "not-firing-observed")

    def test_unqueryable_timer_is_unknown_never_guessed(self) -> None:
        raw = kgk.RawFiringObservation(
            agent="x", timer_active_state=None, timer_unit_file_state=None,
            timer_last_trigger=None, state_file_present=False, state_file=None,
        )
        self.assertEqual(kgk.interpret_firing(raw).verdict, "unknown")

    def test_active_but_no_trigger_yet_is_not_firing_observed(self) -> None:
        raw = kgk.RawFiringObservation(
            agent="x", timer_active_state="active", timer_unit_file_state="enabled",
            timer_last_trigger=None, state_file_present=False, state_file=None,
        )
        self.assertEqual(kgk.interpret_firing(raw).verdict, "not-firing-observed")


class FleetFiringSnapshotTest(unittest.TestCase):
    def test_covers_every_agent_pane_seat_with_framing(self) -> None:
        snapshot = kgk.capture_fleet_firing_snapshot()
        self.assertEqual({e.agent for e in snapshot}, set(keep_going.AGENT_PANE))
        for entry in snapshot:
            self.assertTrue(entry.framing)

    def test_orchestrator_and_specialist_confirmed_firing(self) -> None:
        snapshot = {e.agent: e for e in kgk.capture_fleet_firing_snapshot()}
        self.assertEqual(snapshot["orchestrator"].observation.verdict, "confirmed-firing")
        self.assertEqual(snapshot["specialist"].observation.verdict, "confirmed-firing")

    def test_recovery_metis_atlas_not_firing_but_framed_as_non_defect(self) -> None:
        snapshot = {e.agent: e for e in kgk.capture_fleet_firing_snapshot()}
        for agent in ("recovery", "metis", "atlas"):
            self.assertEqual(snapshot[agent].observation.verdict, "not-firing-observed")
            self.assertNotIn("is broken", snapshot[agent].framing.lower())
        self.assertIn("quiet", snapshot["recovery"].framing.lower())
        self.assertIn("not a regression", snapshot["recovery"].framing.lower())
        self.assertIn("on-demand", snapshot["metis"].framing.lower())
        self.assertIn("not asserted broken", snapshot["metis"].framing.lower())
        self.assertIn("on-demand", snapshot["atlas"].framing.lower())
        self.assertIn("not asserted broken", snapshot["atlas"].framing.lower())

    def test_snapshot_is_dated_not_asserted_live(self) -> None:
        self.assertTrue(kgk.FLEET_FIRING_SNAPSHOT_OBSERVED_AT.startswith("2026-08-05"))


class MailboxRaceDedupeTest(unittest.TestCase):
    def test_two_racing_identical_nudges_collapse_to_one_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            result = kgk.prove_mailbox_race_dedupe(hub_path=hub, agent="race-agent")
            self.assertEqual(len(result.pending_names), 1)
            self.assertEqual(result.first_id, result.second_id)

    def test_refuses_the_real_hub(self) -> None:
        with self.assertRaises(HardStop):
            kgk.prove_mailbox_race_dedupe(hub_path=kgk._REAL_HUB_DEFAULT, agent="race-agent")


class RateLimitSequencingTest(unittest.TestCase):
    def test_never_double_submits_on_ineffective_signal(self) -> None:
        result = kgk.prove_rate_limit_sequencing(cycles=10, cur_work_ts=None)
        nudge_cycles = [i for i, fired in enumerate(result.fired_per_cycle) if fired]
        self.assertTrue(nudge_cycles)
        # Bootstrap exception (traced, documented in the adaptive-backoff oracle
        # detail): cycle 0 is the first-ever nudge, which cannot yet be judged
        # effective/ineffective; cycle 1 is the tick that captures that verdict,
        # so it is still due at threshold=1. That is the ONE allowed adjacent
        # pair -- every pair after it must show a strictly growing gap.
        self.assertEqual(nudge_cycles[:2], [0, 1])
        for a, b in zip(nudge_cycles[1:], nudge_cycles[2:]):
            self.assertGreater(b - a, 1, "two nudges fired on adjacent cycles after bootstrap")

    def test_effective_signal_keeps_base_cadence(self) -> None:
        # Continually ADVANCING work (not a constant timestamp) is what
        # decide_nudge's own contract reads as "effective" -- see
        # prove_rate_limit_sequencing's docstring.
        result = kgk.prove_rate_limit_sequencing(
            cycles=6, cur_work_ts=None, work_ts_for_cycle=lambda i: 1_700_000_000 + i * 100,
        )
        self.assertTrue(all(result.fired_per_cycle))

    def test_queue_floor_caps_backoff(self) -> None:
        floored = kgk.prove_rate_limit_sequencing(cycles=8, cur_work_ts=None, floor_exp=1)
        unfloored = kgk.prove_rate_limit_sequencing(cycles=8, cur_work_ts=None)
        self.assertGreaterEqual(
            sum(floored.fired_per_cycle), sum(unfloored.fired_per_cycle),
        )


class RaceGapFindingTest(unittest.TestCase):
    def test_two_concurrent_readers_of_same_state_both_decide_true(self) -> None:
        finding = kgk.prove_same_state_race_gap()
        self.assertTrue(finding.first_should)
        self.assertTrue(finding.second_should)
        self.assertIn("no file lock", finding.detail.lower())
        self.assertIn("narrow", finding.detail.lower())


class KeepGoingKickerModuleTest(unittest.TestCase):
    def test_declare_applies_when_inventory_has_entry(self) -> None:
        module = kgk.KeepGoingKickerModule()
        target = TargetInventory(
            persona=PERSONA,
            entries=(
                InventoryPathEntry(
                    path=Path(f"pantheon-keep-going@{PERSONA}.timer"), tier="core",
                    status="applies", module_id=kgk.MODULE_ID,
                ),
            ),
        )
        self.assertTrue(module.declare(target).applies)

    def test_declare_absent_when_no_entry(self) -> None:
        module = kgk.KeepGoingKickerModule()
        target = TargetInventory(persona=PERSONA, entries=())
        self.assertFalse(module.declare(target).applies)

    def test_prove_restore_report_roundtrip(self) -> None:
        module = kgk.KeepGoingKickerModule()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root / "evidence", root / "hub", root / "state")
            evidence = module.prove(ctx)
            self.assertEqual(evidence.status, "proven")
            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertIn("recovery", row.summary.lower())


if __name__ == "__main__":
    unittest.main()
