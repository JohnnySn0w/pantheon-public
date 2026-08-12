"""Unit tests for tools/ledger_shard.py (T-0148 Phase 1).

Covers the three backward-compat primitives:
  - segment_for_ts: ISO-year/ISO-week derivation incl. year-boundary.
  - current_segment_shard: pure-path subfolder shard (no mkdir).
  - iter_ledger_entry_files: the reader union (legacy-only / shards-only /
    both during transition / archive-exclusion / sort order).
  - entry_ts: ts-field-or-derive-from-id fallback (T-1879).

Run from repo root:
    python3 -m unittest tools.test_ledger_shard -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.ledger_shard as ls


class TestSegmentForTs(unittest.TestCase):
    def test_mid_year_week(self):
        # 2026-04-30 is ISO week 18 of ISO-year 2026.
        self.assertEqual(ls.segment_for_ts("2026-04-30T12:00:00Z"), "2026-W18")

    def test_two_digit_zero_padded_week(self):
        # Early-year entry → single-digit week must zero-pad.
        self.assertEqual(ls.segment_for_ts("2026-01-05T00:00:00Z"), "2026-W02")
        self.assertTrue(ls.segment_for_ts("2026-01-05T00:00:00Z").endswith("W02"))

    def test_uppercase_w(self):
        self.assertIn("-W", ls.segment_for_ts("2026-05-29T00:00:00Z"))

    def test_iso_year_differs_from_calendar_year_boundary(self):
        # 2027-01-01 (calendar year 2027) is ISO week 53 of ISO-YEAR 2026.
        # The segment must use the ISO year, not the calendar year.
        self.assertEqual(ls.segment_for_ts("2027-01-01T00:00:00Z"), "2026-W53")

    def test_iso_year_early_january_belongs_to_prior_iso_year(self):
        # 2026-01-01 falls in ISO week 1 of ISO-year 2026 (Thu) — sanity that
        # the common case isn't accidentally shifted.
        self.assertEqual(ls.segment_for_ts("2026-01-01T00:00:00Z"), "2026-W01")

    def test_malformed_ts_raises(self):
        with self.assertRaises(ValueError):
            ls.segment_for_ts("not-a-timestamp")

    def test_real_ledger_head_and_tail_shape(self):
        # Captured-real-shape ts values from ledger/2026.jsonl.
        self.assertEqual(ls.segment_for_ts("2026-04-28T00:40:20Z"), "2026-W18")
        self.assertEqual(ls.segment_for_ts("2026-05-29T21:48:00Z"), "2026-W22")


class TestCurrentSegmentShard(unittest.TestCase):
    def test_returns_subfolder_shard_path(self):
        ledger_dir = Path("/tmp/ledger")
        now = datetime(2026, 5, 29, 21, 0, 0)  # ISO week 22 of 2026
        shard = ls.current_segment_shard(ledger_dir, now)
        self.assertEqual(shard, ledger_dir / "2026-W22" / "2026-W22.jsonl")

    def test_does_not_create_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp) / "ledger"
            now = datetime(2026, 5, 29, 21, 0, 0)
            shard = ls.current_segment_shard(ledger_dir, now)
            # Pure path: neither the ledger dir nor the segment dir created.
            self.assertFalse(ledger_dir.exists())
            self.assertFalse(shard.parent.exists())

    def test_year_boundary_uses_iso_year(self):
        ledger_dir = Path("/tmp/ledger")
        now = datetime(2027, 1, 1, 0, 0, 0)  # ISO week 53 of ISO-year 2026
        shard = ls.current_segment_shard(ledger_dir, now)
        self.assertEqual(shard, ledger_dir / "2026-W53" / "2026-W53.jsonl")


class TestCurrentWriteTarget(unittest.TestCase):
    def test_legacy_exists_returns_legacy(self):
        # Pre-migration: 2026.jsonl present → writer keeps appending to it
        # regardless of `now`. Zero behavior change.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            legacy = d / "2026.jsonl"
            legacy.write_text("{}\n")
            now = datetime(2026, 5, 29, 21, 0, 0)  # ISO week 22
            self.assertEqual(ls.current_write_target(d, now), legacy)

    def test_legacy_absent_returns_current_week_shard(self):
        # Post-migration: 2026.jsonl gone → writer targets the ISO-week shard.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            now = datetime(2026, 5, 29, 21, 0, 0)  # ISO week 22 of 2026
            target = ls.current_write_target(d, now)
            self.assertEqual(target, d / "2026-W22" / "2026-W22.jsonl")

    def test_legacy_absent_does_not_create_dirs(self):
        # current_write_target is a pure path resolver; the segment dir is the
        # caller's job (via ensure_segment_dir).
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            now = datetime(2026, 5, 29, 21, 0, 0)
            target = ls.current_write_target(d, now)
            self.assertFalse(target.parent.exists())

    def test_archived_legacy_does_not_count_as_legacy(self):
        # An archived 2026.jsonl.archive-pre-d10-shard is NOT the live legacy
        # file → the flip takes effect (we get the week shard).
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            (d / "2026.jsonl.archive-pre-d10-shard").write_text("{}\n")
            now = datetime(2026, 5, 29, 21, 0, 0)
            target = ls.current_write_target(d, now)
            self.assertEqual(target, d / "2026-W22" / "2026-W22.jsonl")


class TestEnsureSegmentDir(unittest.TestCase):
    def test_creates_missing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            target = d / "2026-W22" / "2026-W22.jsonl"
            self.assertFalse(target.parent.exists())
            ls.ensure_segment_dir(target)
            self.assertTrue(target.parent.is_dir())

    def test_idempotent_when_parent_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "ledger"
            d.mkdir()
            target = d / "2026.jsonl"  # parent (ledger/) already exists
            ls.ensure_segment_dir(target)  # must not raise
            self.assertTrue(target.parent.is_dir())


class TestIterLedgerEntryFiles(unittest.TestCase):
    def _mk(self, root: str) -> Path:
        d = Path(root) / "ledger"
        d.mkdir()
        return d

    def test_missing_dir_returns_empty(self):
        self.assertEqual(ls.iter_ledger_entry_files(Path("/nonexistent/ledger")), [])

    def test_pre_migration_returns_only_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            legacy = d / "2026.jsonl"
            legacy.write_text("{}\n")
            self.assertEqual(ls.iter_ledger_entry_files(d), [legacy])

    def test_post_migration_returns_only_shards_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            for seg in ("2026-W20", "2026-W18", "2026-W19"):
                (d / seg).mkdir()
                (d / seg / f"{seg}.jsonl").write_text("{}\n")
            files = ls.iter_ledger_entry_files(d)
            names = [f.parent.name for f in files]
            self.assertEqual(names, ["2026-W18", "2026-W19", "2026-W20"])

    def test_transition_returns_both_shards_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            (d / "2026-W18").mkdir()
            (d / "2026-W18" / "2026-W18.jsonl").write_text("{}\n")
            legacy = d / "2026.jsonl"
            legacy.write_text("{}\n")
            files = ls.iter_ledger_entry_files(d)
            # Shards sort first (by segment name), legacy appended last.
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0].parent.name, "2026-W18")
            self.assertEqual(files[-1], legacy)

    def test_excludes_archive_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            (d / "2026-W18").mkdir()
            (d / "2026-W18" / "2026-W18.jsonl").write_text("{}\n")
            # The post-migration archived legacy file must NOT be in the union.
            (d / "2026.jsonl.archive-pre-d10-shard").write_text("{}\n")
            files = ls.iter_ledger_entry_files(d)
            self.assertEqual([f.parent.name for f in files], ["2026-W18"])
            self.assertFalse(any("archive" in f.name for f in files))

    def test_ignores_non_matching_files_in_segment_dir(self):
        # A subfolder whose .jsonl name doesn't match the folder name is not
        # the segment shard and must be ignored (e.g. a stray SPINE-less dir).
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            (d / "2026-W18").mkdir()
            (d / "2026-W18" / "SPINE.md").write_text("# spine\n")
            (d / "2026-W18" / "stray.jsonl").write_text("{}\n")
            self.assertEqual(ls.iter_ledger_entry_files(d), [])

    def test_ignores_loose_files_at_ledger_root(self):
        # INDEX.md, sidecar json, README.md at root must not be treated as
        # entry files.
        with tempfile.TemporaryDirectory() as tmp:
            d = self._mk(tmp)
            (d / "INDEX.md").write_text("# index\n")
            (d / "2026.metrics-augment.sidecar.json").write_text("{}\n")
            legacy = d / "2026.jsonl"
            legacy.write_text("{}\n")
            self.assertEqual(ls.iter_ledger_entry_files(d), [legacy])


class TestEntryTs(unittest.TestCase):
    def test_ts_field_present_returned_as_is(self):
        entry = {"id": "2026-04-30T00:00:00Z-001", "ts": "2026-04-30T12:34:56Z"}
        self.assertEqual(ls.entry_ts(entry), "2026-04-30T12:34:56Z")

    def test_ts_field_wins_over_garbage_id(self):
        # A str ts field takes priority even when the id would not parse.
        entry = {"id": "not-a-timestamp-suffix", "ts": "2026-04-30T12:34:56Z"}
        self.assertEqual(ls.entry_ts(entry), "2026-04-30T12:34:56Z")

    def test_ts_absent_derives_from_valid_id_prefix(self):
        entry = {"id": "2026-07-20T00:10:23Z-ho-t1818-exocortex-phase1"}
        self.assertEqual(ls.entry_ts(entry), "2026-07-20T00:10:23Z")

    def test_non_str_ts_falls_back_to_id_derivation(self):
        entry = {"id": "2026-07-20T00:10:23Z-suffix", "ts": 12345}
        self.assertEqual(ls.entry_ts(entry), "2026-07-20T00:10:23Z")

    def test_garbage_id_returns_none(self):
        entry = {"id": "not-a-timestamp-suffix"}
        self.assertIsNone(ls.entry_ts(entry))

    def test_missing_id_and_ts_returns_none(self):
        self.assertIsNone(ls.entry_ts({"action": "x"}))

    def test_non_str_id_returns_none(self):
        entry = {"id": 12345}
        self.assertIsNone(ls.entry_ts(entry))

    def test_id_shorter_than_prefix_returns_none(self):
        entry = {"id": "2026-07-20"}
        self.assertIsNone(ls.entry_ts(entry))


class TestAppendRefusesDuplicateIds(unittest.TestCase):
    """T-2407: an append-only chain whose ids are not unique cannot address its
    own records — lookup by id returns two rows and a `supersedes` naming that
    id corrects both or neither.

    Nine such pairs exist historically (2026-05-14..05-20 sync-alerts). They are
    frozen rather than repaired: rewriting an append-only chain to fix an
    addressing defect forfeits the property the addressing exists to serve.
    """

    def _ledger(self, tmp: str) -> Path:
        d = Path(tmp) / "ledger"
        d.mkdir()
        return d

    def _entry(self, eid: str) -> dict:
        return {"id": eid, "ts": "2026-08-02T00:00:00Z", "agent": "orchestrator"}

    def test_duplicate_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._ledger(tmp)
            ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            with self.assertRaises(ValueError) as ctx:
                ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            self.assertIn("already exists", str(ctx.exception))

    def test_refusal_leaves_the_ledger_byte_identical(self) -> None:
        """The re-test criterion states this exactly: rejected WITHOUT modifying
        the ledger. A guard that refuses after opening the file in append mode
        would still touch it."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._ledger(tmp)
            target = ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            self.assertEqual(target.read_bytes(), before)

    def test_a_distinct_id_still_appends(self) -> None:
        """The guard must not be the kind that also blocks the legitimate case."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._ledger(tmp)
            ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            target = ls.append_entry(d, self._entry("20260802T000001Z-orchestrator-y"))
            self.assertEqual(len(target.read_text().strip().splitlines()), 2)

    def test_collision_is_caught_across_shards_not_just_within_one(self) -> None:
        """A duplicate must not be able to hide by living in a different shard
        from its twin."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._ledger(tmp)
            early = {"id": "dup", "ts": "2026-05-14T21:44:52Z"}
            late = {"id": "dup", "ts": "2026-08-02T00:00:00Z"}
            first = ls.append_entry(d, early)
            with self.assertRaises(ValueError):
                ls.append_entry(d, late)
            # ...and they really would have been different shards.
            self.assertNotEqual(
                ls.segment_for_ts(ls.entry_ts(early)), ls.segment_for_ts(ls.entry_ts(late))
            )
            self.assertEqual(len(first.read_text().strip().splitlines()), 1)

    def test_a_malformed_existing_line_does_not_break_appends(self) -> None:
        """Fail-open on unparseable history: a guard that crashes on a
        pre-existing bad line turns an unrelated defect into a total append
        outage, and a line that will not parse cannot collide anyway."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._ledger(tmp)
            target = ls.append_entry(d, self._entry("20260802T000000Z-orchestrator-x"))
            with target.open("a") as fh:
                fh.write("{not json at all\n")
            ls.append_entry(d, self._entry("20260802T000002Z-orchestrator-z"))
            self.assertIn("20260802T000002Z-orchestrator-z", target.read_text())

    def test_live_ledger_has_exactly_the_nine_known_pairs(self) -> None:
        """The frozen historical set, pinned so it cannot GROW unnoticed.

        Deliberately an equality assertion, not a ceiling: a tenth duplicate
        means the writer-side guard was bypassed (raw-git hand-append), which is
        the one path this refusal cannot cover and the thing worth hearing about.
        """
        import collections
        import json as _json

        repo_ledger = Path(__file__).resolve().parent.parent / "ledger"
        counts: collections.Counter = collections.Counter()
        for path in ls.iter_ledger_entry_files(repo_ledger):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    eid = _json.loads(line).get("id")
                except _json.JSONDecodeError:
                    continue
                if isinstance(eid, str):
                    counts[eid] += 1
        dupes = sorted(k for k, n in counts.items() if n > 1)
        self.assertEqual(
            len(dupes), 9,
            f"expected the 9 frozen 2026-05 sync-alert pairs, found {len(dupes)}: {dupes}",
        )
        self.assertTrue(
            all(k.startswith("2026-05-1") or k.startswith("2026-05-2") for k in dupes),
            f"a duplicate outside the frozen 2026-05 window: {dupes}",
        )


if __name__ == "__main__":
    unittest.main()
