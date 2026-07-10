#!/usr/bin/env python3
"""Hermetic tests for tools/_audit.py — the canonical ORG-001 ledger
build + append helper (T-1088 §Auditable, part a)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools._audit import ORG001_FIELDS, append_ledger_entries, build_ledger_entry


class TestBuildLedgerEntry(unittest.TestCase):
    def test_full_ten_field_schema_present(self) -> None:
        entry = build_ledger_entry(
            entry_id="2026-06-12T00:00:00Z-x",
            ts="2026-06-12T00:00:00Z",
            agent="recovery",
            action="unit_test",
            rationale="because",
            justifies_via=["P1"],
        )
        # All 10 ORG-001 keys present, even the optional provenance trio.
        self.assertEqual(set(entry.keys()), set(ORG001_FIELDS))
        self.assertEqual(len(ORG001_FIELDS), 10)
        # Optional fields default to their canonical empties.
        self.assertEqual(entry["links"], [])
        self.assertIsNone(entry["cost_usd"])
        self.assertIsNone(entry["parent_id"])
        self.assertIsNone(entry["supersedes"])

    def test_values_passed_through(self) -> None:
        links = [{"type": "ticket", "ref": "T-0001"}]
        entry = build_ledger_entry(
            entry_id="id1",
            ts="2026-06-12T01:02:03Z",
            agent="specialist",
            action="actuation_2026-06-12",
            rationale="filed 2 tickets",
            justifies_via=["P1", "D1", "D3"],
            links=links,
            cost_usd=1.5,
            parent_id="T-0000",
            supersedes="id0",
        )
        self.assertEqual(entry["id"], "id1")
        self.assertEqual(entry["agent"], "specialist")
        self.assertEqual(entry["justifies_via"], ["P1", "D1", "D3"])
        self.assertEqual(entry["links"], links)
        self.assertEqual(entry["cost_usd"], 1.5)
        self.assertEqual(entry["parent_id"], "T-0000")
        self.assertEqual(entry["supersedes"], "id0")

    def test_iterables_materialized_not_aliased(self) -> None:
        jv = ["P1"]
        links = [{"type": "file", "ref": "x"}]
        entry = build_ledger_entry(
            entry_id="i", ts="t", agent="a", action="ac",
            rationale="r", justifies_via=jv, links=links,
        )
        # Mutating the caller's lists must not affect the built entry.
        jv.append("MUTATED")
        links.append({"type": "MUTATED", "ref": "z"})
        self.assertEqual(entry["justifies_via"], ["P1"])
        self.assertEqual(entry["links"], [{"type": "file", "ref": "x"}])


class TestAppendLedgerEntries(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_dir = Path(self._tmp.name) / "ledger"
        self.ledger_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, n: int) -> dict:
        return build_ledger_entry(
            entry_id=f"id{n}", ts=f"2026-06-12T00:00:0{n}Z", agent="recovery",
            action=f"act{n}", rationale="r", justifies_via=["P1"],
        )

    def test_explicit_shard_append_and_return(self) -> None:
        shard = self.ledger_dir / "custom.jsonl"
        returned = append_ledger_entries([self._entry(1)], ledger_dir=self.ledger_dir, shard=shard)
        self.assertEqual(returned, shard)
        lines = shard.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], "id1")

    def test_serialization_is_sort_keys(self) -> None:
        shard = self.ledger_dir / "s.jsonl"
        append_ledger_entries([self._entry(1)], ledger_dir=self.ledger_dir, shard=shard)
        line = shard.read_text(encoding="utf-8").splitlines()[0]
        # Keys appear in sorted order on disk (deterministic).
        self.assertEqual(line, json.dumps(json.loads(line), sort_keys=True))
        # 'action' precedes 'agent' precedes 'id' precedes 'ts' alphabetically.
        self.assertLess(line.index('"action"'), line.index('"agent"'))
        self.assertLess(line.index('"agent"'), line.index('"id"'))

    def test_batch_append_one_open(self) -> None:
        shard = self.ledger_dir / "b.jsonl"
        append_ledger_entries(
            [self._entry(1), self._entry(2), self._entry(3)],
            ledger_dir=self.ledger_dir, shard=shard,
        )
        lines = shard.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], ["id1", "id2", "id3"])

    def test_append_is_additive(self) -> None:
        shard = self.ledger_dir / "a.jsonl"
        append_ledger_entries([self._entry(1)], ledger_dir=self.ledger_dir, shard=shard)
        append_ledger_entries([self._entry(2)], ledger_dir=self.ledger_dir, shard=shard)
        lines = shard.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], ["id1", "id2"])

    def test_default_target_is_legacy_when_present(self) -> None:
        # Legacy 2026.jsonl present → current_write_target returns it.
        legacy = self.ledger_dir / "2026.jsonl"
        legacy.write_text("", encoding="utf-8")
        returned = append_ledger_entries(
            [self._entry(1)], ledger_dir=self.ledger_dir,
            now=datetime(2026, 6, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(returned, legacy)
        self.assertEqual(len(legacy.read_text().splitlines()), 1)

    def test_default_target_is_week_shard_when_no_legacy(self) -> None:
        # No legacy file → current ISO-week shard, and ensure_segment_dir
        # creates its parent dir.
        returned = append_ledger_entries(
            [self._entry(1)], ledger_dir=self.ledger_dir,
            now=datetime(2026, 6, 12, tzinfo=timezone.utc),
        )
        self.assertTrue(returned.exists())
        self.assertTrue(returned.parent.is_dir())
        # 2026-06-12 is ISO week 24.
        self.assertIn("2026-W24", str(returned))


if __name__ == "__main__":
    unittest.main()
