"""Tests for tools.bringup_acceptance.evidence (T-2558 phase 1)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.bringup_acceptance.closure import build_closure
from tools.bringup_acceptance.evidence import (
    RunPaths,
    read_closure,
    read_inventory,
    read_restore,
    read_run_manifest,
    write_closure,
    write_inventory,
    write_module_evidence,
    write_restore,
    write_run_manifest,
)
from tools.bringup_acceptance.types import HardStop, InventoryPathEntry, MatrixRow, RunContext, TargetInventory


def _run_context(run_id: str, evidence_root: Path) -> RunContext:
    return RunContext(
        run_id=run_id,
        persona="recovery",
        account="claude-fallback",
        harness="claude",
        profile="interactive",
        hub_path=Path("/tmp/hub"),
        state_path=Path("/tmp/state"),
        evidence_root=evidence_root,
        baseline_ref="deadbeef" * 5,
    )


class RunPathsLayoutTest(unittest.TestCase):
    def test_layout_matches_the_ratified_schema(self) -> None:
        root = Path("/tmp/evidence")
        paths = RunPaths(root=root, run_id="run-1")
        self.assertEqual(paths.run_dir, root / "run-1")
        self.assertEqual(paths.run_json, root / "run-1" / "run.json")
        self.assertEqual(paths.inventory_json, root / "run-1" / "inventory.json")
        self.assertEqual(paths.modules_dir, root / "run-1" / "modules")
        self.assertEqual(
            paths.module_trace_path("mailbox-roundtrip"),
            root / "run-1" / "modules" / "mailbox-roundtrip" / "trace.jsonl",
        )
        self.assertEqual(
            paths.module_evidence_dir("mailbox-roundtrip"),
            root / "run-1" / "modules" / "mailbox-roundtrip" / "evidence",
        )
        self.assertEqual(paths.closure_json, root / "run-1" / "closure.json")
        self.assertEqual(paths.closure_md, root / "run-1" / "closure.md")
        self.assertEqual(paths.restore_json, root / "run-1" / "restore.json")

    def test_bad_run_id_is_rejected(self) -> None:
        with self.assertRaises(HardStop):
            RunPaths(root=Path("/tmp/evidence"), run_id="../escape")


class WriteReadRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = RunPaths(root=self.root, run_id="run-1")

    def test_run_manifest_round_trips(self) -> None:
        ctx = _run_context("run-1", self.root)
        write_run_manifest(self.paths, ctx)
        manifest = read_run_manifest(self.paths)
        self.assertEqual(manifest["run_id"], "run-1")
        self.assertEqual(manifest["persona"], "recovery")
        self.assertEqual(manifest["harness"], "claude")
        self.assertEqual(manifest["baseline_ref"], "deadbeef" * 5)

    def test_inventory_round_trips(self) -> None:
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("/etc/systemd/user/pantheon-runtime.service"),
                    tier="core",
                    status="applies",
                    module_id="timer-cron",
                ),
                InventoryPathEntry(
                    path=Path("/home/claude-fallback/.local/bin/self-restart.sh"),
                    tier="optional",
                    status="absent",
                    module_id="recovery-recovery",
                ),
            ),
        )
        write_inventory(self.paths, inventory)
        observed = read_inventory(self.paths)
        self.assertEqual(len(observed["entries"]), 2)
        self.assertEqual(observed["entries"][0]["tier"], "core")
        self.assertEqual(observed["entries"][1]["status"], "absent")

    def test_module_evidence_is_written_under_the_module_evidence_dir(self) -> None:
        target = write_module_evidence(
            self.paths, "mailbox-roundtrip", "ack", {"message_id": "m1"}
        )
        self.assertEqual(
            target, self.paths.module_evidence_dir("mailbox-roundtrip") / "ack.json"
        )
        self.assertTrue(target.is_file())

    def test_closure_round_trips_json_and_renders_markdown(self) -> None:
        rows = (
            MatrixRow(
                module_id="mailbox-roundtrip",
                tier="core",
                status="proven",
                summary="ok",
                evidence_refs=(),
            ),
        )
        closure = build_closure("run-1", rows)
        write_closure(self.paths, closure)
        observed = read_closure(self.paths)
        self.assertEqual(observed["status"], "GREEN")
        self.assertEqual(observed["gaps"], [])
        markdown = self.paths.closure_md.read_text(encoding="utf-8")
        self.assertIn("mailbox-roundtrip", markdown)
        self.assertIn("**Status:** GREEN", markdown)

    def test_closure_composite_rows_round_trip(self) -> None:
        rows = (
            MatrixRow(
                module_id="mailbox-roundtrip",
                tier="core",
                status="proven",
                summary="ok",
                evidence_refs=(),
            ),
        )
        closure = build_closure(
            "run-1", rows, composites={"mailbox-fully-functional": ("mailbox-roundtrip",)}
        )
        write_closure(self.paths, closure)
        observed = read_closure(self.paths)
        self.assertEqual(len(observed["composite_rows"]), 1)
        self.assertEqual(observed["composite_rows"][0]["capability_id"], "mailbox-fully-functional")
        self.assertEqual(observed["composite_rows"][0]["status"], "GREEN")
        self.assertEqual(observed["composite_rows"][0]["missing_module_ids"], [])

    def test_restore_round_trips(self) -> None:
        write_restore(self.paths, {"mailbox-roundtrip": {"restored": True}})
        observed = read_restore(self.paths)
        self.assertEqual(observed["modules"]["mailbox-roundtrip"]["restored"], True)


if __name__ == "__main__":
    unittest.main()
