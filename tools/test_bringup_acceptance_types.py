"""Tests for tools.bringup_acceptance.types (T-2558 phase 1)."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from tools.bringup_acceptance.types import (
    AcceptanceModule,
    HardStop,
    InventoryPathEntry,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
)


def _run_context() -> RunContext:
    return RunContext(
        run_id="20260805T000000Z",
        persona="recovery",
        account="claude-fallback",
        harness="claude",
        profile="interactive",
        hub_path=Path("/tmp/hub"),
        state_path=Path("/tmp/state"),
        evidence_root=Path("/tmp/evidence"),
        baseline_ref="deadbeef" * 5,
    )


class DataclassShapeTest(unittest.TestCase):
    def test_run_context_is_frozen_with_expected_fields(self) -> None:
        ctx = _run_context()
        self.assertEqual(
            {field.name for field in dataclasses.fields(ctx)},
            {
                "run_id",
                "persona",
                "account",
                "harness",
                "profile",
                "hub_path",
                "state_path",
                "evidence_root",
                "baseline_ref",
            },
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.run_id = "other"  # type: ignore[misc]

    def test_target_inventory_holds_tagged_path_entries(self) -> None:
        entry = InventoryPathEntry(
            path=Path("/etc/systemd/user/pantheon-runtime.service"),
            tier="core",
            status="applies",
            module_id="timer-cron",
        )
        inventory = TargetInventory(persona="recovery", entries=(entry,))
        self.assertEqual(inventory.entries[0].tier, "core")
        self.assertEqual(inventory.entries[0].status, "applies")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.tier = "optional"  # type: ignore[misc]

    def test_module_declaration_evidence_restore_matrix_row_construct(self) -> None:
        declaration = ModuleDeclaration(
            module_id="mailbox-roundtrip", tier="core", applies=True, reason="mailbox present"
        )
        evidence = ModuleEvidence(
            module_id="mailbox-roundtrip",
            status="proven",
            trace_path=Path("/tmp/evidence/run/modules/mailbox-roundtrip/trace.jsonl"),
            evidence_paths=(Path("/tmp/evidence/run/modules/mailbox-roundtrip/evidence/ack.json"),),
            detail={"nonce": "abc123"},
        )
        restore = RestoreEvidence(
            module_id="mailbox-roundtrip", restored=True, detail={"cleaned": True}
        )
        row = MatrixRow(
            module_id="mailbox-roundtrip",
            tier="core",
            status="proven",
            summary="mailbox nonce round-tripped",
            evidence_refs=evidence.evidence_paths,
        )
        self.assertTrue(declaration.applies)
        self.assertEqual(evidence.status, "proven")
        self.assertTrue(restore.restored)
        self.assertEqual(row.status, "proven")


class FixtureModule:
    """Minimal fixture satisfying the AcceptanceModule protocol."""

    id = "fixture"
    tier = "core"

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=True, reason="fixture")

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        return ModuleEvidence(
            module_id=self.id,
            status="proven",
            trace_path=ctx.evidence_root,
            evidence_paths=(),
            detail={},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        return RestoreEvidence(module_id=self.id, restored=True, detail={})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        return MatrixRow(
            module_id=self.id,
            tier=self.tier,
            status="proven",
            summary="fixture proven",
            evidence_refs=(),
        )


class IncompleteModule:
    """Missing prove()/restore() -- must NOT satisfy the protocol."""

    id = "incomplete"
    tier = "optional"

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=False, reason="n/a")


class AcceptanceModuleProtocolTest(unittest.TestCase):
    def test_fixture_module_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(FixtureModule(), AcceptanceModule)

    def test_incomplete_module_does_not_satisfy_the_protocol(self) -> None:
        self.assertNotIsInstance(IncompleteModule(), AcceptanceModule)

    def test_fixture_module_round_trip_declare_prove_restore_report(self) -> None:
        module = FixtureModule()
        ctx = _run_context()
        declaration = module.declare(TargetInventory(persona=ctx.persona, entries=()))
        self.assertTrue(declaration.applies)
        evidence = module.prove(ctx)
        self.assertEqual(evidence.status, "proven")
        restore = module.restore(ctx)
        self.assertTrue(restore.restored)
        row = module.report(evidence)
        self.assertEqual(row.status, "proven")


class HardStopTest(unittest.TestCase):
    def test_hard_stop_is_a_runtime_error(self) -> None:
        with self.assertRaises(HardStop):
            raise HardStop("anomaly")
        self.assertTrue(issubclass(HardStop, RuntimeError))


if __name__ == "__main__":
    unittest.main()
