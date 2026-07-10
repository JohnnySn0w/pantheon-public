"""Unit tests for tools/migrate_ledger_to_weekly_shards.py (T-0148).

The migration itself is WRITTEN BUT NOT RUN in Phase 1 — these tests exercise
it against captured-real-shape ledger slices (D11) in tmp dirs only, never the
live ledger.

Coverage: count/id/order/segment-correctness, year-boundary, idempotency,
archive-exclusion, merge-existing (partial prior run), malformed-abort, and
that the raw lines are written VERBATIM (no json.dumps drift, mixed spacing
preserved).

Run from repo root:
    python3 -m unittest tools.test_migrate_ledger_to_weekly_shards -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.migrate_ledger_to_weekly_shards as mig
from tools.ledger_shard import LEGACY_LEDGER_FILENAME

# Captured-real-shape slice of ledger/2026.jsonl (D11): the first line is the
# real compact-form head entry; the second is a real spaced-form actuation
# entry; the third is a synthetic-but-real-shape YEAR-BOUNDARY entry whose ts
# (2027-01-01) maps by ISO calendar to ISO-year 2026, week 53 (segment
# 2026-W53) — proves the migration uses ISO year, not calendar year.
REAL_HEAD = (
    '{"id":"2026-04-28T00:40:20Z-001","ts":"2026-04-28T00:40:20Z","agent":"human:operator",'
    '"action":"ratify_principles_v0","rationale":"Reviewed the v0 principles draft.",'
    '"justifies_via":["P3","P5"],"links":[{"type":"telegram","ref":"msg_64"}],'
    '"cost_usd":null,"parent_id":null,"supersedes":null}'
)
REAL_SPACED = (
    '{"id": "2026-05-11T08:34:59Z-actuation", "ts": "2026-05-11T08:34:59Z", "agent": "specialist", '
    '"action": "actuation_2026-05-11", "rationale": "Auto-filed 1 ticket(s).", '
    '"justifies_via": ["P1", "D1", "D3"], "links": [{"type": "ticket", "ref": "T-0215"}]}'
)
YEAR_BOUNDARY = (
    '{"id":"2027-01-01T00:00:00Z-001","ts":"2027-01-01T00:00:00Z","agent":"recovery",'
    '"action":"year_boundary_probe","rationale":"ISO week 53 of ISO-year 2026.",'
    '"justifies_via":["P5"]}'
)

# Expected segments: head → 2026-W18, actuation → 2026-W20, boundary → 2026-W53.
SLICE_LINES = [REAL_HEAD, REAL_SPACED, YEAR_BOUNDARY]


def _make_ledger(tmp: str, lines: list[str] | None = None) -> Path:
    ledger = Path(tmp) / "ledger"
    ledger.mkdir()
    body = "\n".join(SLICE_LINES if lines is None else lines) + "\n"
    (ledger / LEGACY_LEDGER_FILENAME).write_text(body, encoding="utf-8")
    return ledger


class TestMigrateHappyPath(unittest.TestCase):
    def test_segments_created_with_correct_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            summary = mig.migrate(ledger, dry_run=False)
            self.assertEqual(summary["total_entries"], 3)
            self.assertEqual(
                summary["segments"], {"2026-W18": 1, "2026-W20": 1, "2026-W53": 1}
            )

    def test_year_boundary_entry_lands_in_iso_year_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            # 2027-01-01 → ISO-year 2026, week 53 (NOT a 2027 segment).
            self.assertTrue((ledger / "2026-W53" / "2026-W53.jsonl").is_file())
            self.assertFalse((ledger / "2027-W01").exists())

    def test_raw_lines_written_verbatim(self):
        # No re-json.dumps drift: the spaced-form actuation entry must keep its
        # exact original bytes in the shard.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            shard_text = (ledger / "2026-W20" / "2026-W20.jsonl").read_text(encoding="utf-8")
            self.assertIn(REAL_SPACED, shard_text)

    def test_all_ids_preserved_and_segment_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            from tools.ledger_shard import iter_ledger_entry_files
            ids = set()
            for f in iter_ledger_entry_files(ledger):
                for raw in f.read_text().splitlines():
                    if raw.strip():
                        ids.add(json.loads(raw)["id"])
            self.assertEqual(
                ids,
                {
                    "2026-04-28T00:40:20Z-001",
                    "2026-05-11T08:34:59Z-actuation",
                    "2027-01-01T00:00:00Z-001",
                },
            )

    def test_legacy_renamed_to_archive_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            summary = mig.migrate(ledger, dry_run=False)
            self.assertTrue(summary["archived"])
            self.assertFalse((ledger / LEGACY_LEDGER_FILENAME).exists())
            self.assertTrue(
                (ledger / (LEGACY_LEDGER_FILENAME + mig.ARCHIVE_SUFFIX)).is_file()
            )

    def test_spine_md_generated_per_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            for seg in ("2026-W18", "2026-W20", "2026-W53"):
                spine = (ledger / seg / "SPINE.md").read_text()
                self.assertIn(f"Ledger segment {seg}", spine)

    def test_archive_excluded_from_reader_union_after_migration(self):
        from tools.ledger_shard import iter_ledger_entry_files
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            files = iter_ledger_entry_files(ledger)
            self.assertFalse(any("archive" in f.name for f in files))
            self.assertEqual(len(files), 3)


class TestMigrateDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            summary = mig.migrate(ledger, dry_run=True)
            self.assertEqual(summary["total_entries"], 3)
            self.assertFalse(summary["archived"])
            # No subfolders, legacy intact.
            self.assertTrue((ledger / LEGACY_LEDGER_FILENAME).is_file())
            self.assertEqual(list(ledger.glob("2026-W*")), [])


class TestMigrateIdempotency(unittest.TestCase):
    def test_second_run_is_noop_once_legacy_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            mig.migrate(ledger, dry_run=False)
            summary2 = mig.migrate(ledger, dry_run=False)
            self.assertTrue(summary2["noop"])

    def test_noop_when_no_legacy_and_no_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            summary = mig.migrate(ledger, dry_run=False)
            self.assertTrue(summary["noop"])


class TestMigrateMergeExisting(unittest.TestCase):
    def test_partial_prior_run_merges_missing_ids_no_clobber(self):
        # Simulate a partial prior run: 2026-W18 shard already has the head
        # entry. Re-running must NOT duplicate it and must keep it.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            seg_dir = ledger / "2026-W18"
            seg_dir.mkdir()
            (seg_dir / "2026-W18.jsonl").write_text(REAL_HEAD + "\n", encoding="utf-8")

            mig.migrate(ledger, dry_run=False)

            shard_lines = [
                l for l in (seg_dir / "2026-W18.jsonl").read_text().splitlines() if l.strip()
            ]
            # Head entry present exactly once (no clobber, no dup).
            ids = [json.loads(l)["id"] for l in shard_lines]
            self.assertEqual(ids.count("2026-04-28T00:40:20Z-001"), 1)


class TestMigrateDuplicateIds(unittest.TestCase):
    """The live ledger legitimately contains duplicate ids (append-only
    sync-alert entries). The migration must PRESERVE both copies, not drop or
    reject them (id-MULTISET, not id-set, equality)."""

    DUP = (
        '{"id":"2026-05-14T21:44:52Z-sync-divergent","ts":"2026-05-14T21:44:52Z",'
        '"agent":"recovery","action":"sync_alert_divergent_recorded","rationale":"dup.",'
        '"justifies_via":["D1"]}'
    )

    def test_duplicate_id_preserved_both_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp, lines=[self.DUP, self.DUP])
            summary = mig.migrate(ledger, dry_run=False)
            self.assertTrue(summary["archived"])
            # 2026-05-14 → ISO week 20.
            shard_lines = [
                l for l in (ledger / "2026-W20" / "2026-W20.jsonl").read_text().splitlines()
                if l.strip()
            ]
            self.assertEqual(len(shard_lines), 2)

    def test_merge_preserves_both_copies_of_duplicate_id(self):
        # Partial prior run already wrote ONE copy; merge must add the second.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp, lines=[self.DUP, self.DUP])
            seg = ledger / "2026-W20"
            seg.mkdir()
            (seg / "2026-W20.jsonl").write_text(self.DUP + "\n", encoding="utf-8")
            mig.migrate(ledger, dry_run=False)
            shard_lines = [
                l for l in (seg / "2026-W20.jsonl").read_text().splitlines() if l.strip()
            ]
            self.assertEqual(len(shard_lines), 2)


class TestMigrateAbort(unittest.TestCase):
    def test_malformed_line_aborts_no_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp, lines=[REAL_HEAD, "this is not json", REAL_SPACED])
            with self.assertRaises(mig.MigrationError):
                mig.migrate(ledger, dry_run=False)
            # Legacy must NOT have been archived on abort.
            self.assertTrue((ledger / LEGACY_LEDGER_FILENAME).is_file())

    def test_missing_ts_aborts(self):
        bad = '{"id":"x","agent":"recovery","action":"y","justifies_via":["P5"]}'
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp, lines=[REAL_HEAD, bad])
            with self.assertRaises(mig.MigrationError):
                mig.migrate(ledger, dry_run=False)


class TestMigrateVerifyGate(unittest.TestCase):
    def test_cli_dry_run_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp)
            rc = mig.main(["--dry-run", "--ledger-dir", str(ledger)])
            self.assertEqual(rc, 0)
            self.assertTrue((ledger / LEGACY_LEDGER_FILENAME).is_file())

    def test_cli_abort_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _make_ledger(tmp, lines=["garbage"])
            rc = mig.main(["--ledger-dir", str(ledger)])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
