"""Unit tests for tools/cycle_stats.py.

Run from repo root:
    python3 -m unittest tools.test_cycle_stats -v

Covers (per T-0203):
  - parse + filter close-entries with metrics
  - --shape filter
  - --last N filter
  - --similar T-NNNN lookup
  - graceful skip on entries without metrics field
  - action regex (`t<NNNN>_(resolved|landed|closed|done)_*`)
  - average computation across multiple entries
  - report shapes (summary / shape / similar)
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.cycle_stats as cs
from tools.cycle_stats import (
    average_metrics,
    extract_ticket_id,
    filter_by_shape,
    find_by_ticket,
    has_metrics,
    is_close_entry,
    load_entries,
    main,
)


def _entry(action: str, *, metrics: dict | None = None, **extra) -> dict:
    e = {
        "id": "2026-01-01T00:00:00Z-001",
        "ts": "2026-01-01T00:00:00Z",
        "agent": "recovery",
        "action": action,
        "rationale": "test",
        "justifies_via": ["P5"],
    }
    if metrics is not None:
        e["metrics"] = metrics
    e.update(extra)
    return e


def _metrics(shape="feature", outcome="clean", **fields) -> dict:
    base = {
        "wall_clock_min": 30,
        "plan_rebounds": 0,
        "code_rebounds": 0,
        "reviewer_rebounds": 0,
        "implementer_count": 1,
        "reviewer_count": 1,
        "scope_loc_delta": 100,
        "cycle_shape": shape,
        "outcome_class": outcome,
    }
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestIsCloseEntry(unittest.TestCase):
    def test_landed_action_matches(self):
        self.assertTrue(is_close_entry(_entry("t0184_landed_context_fill_monitor")))

    def test_resolved_action_matches(self):
        self.assertTrue(is_close_entry(_entry("t0001_resolved_initial_setup")))

    def test_closed_action_matches(self):
        self.assertTrue(is_close_entry(_entry("t0099_closed_with_no_action")))

    def test_done_action_matches(self):
        self.assertTrue(is_close_entry(_entry("t0050_done_authoring_phase")))

    def test_open_action_rejected(self):
        # Non-close action shape (e.g. ratification, dispatch).
        self.assertFalse(is_close_entry(_entry("ratify_principles_v0")))

    def test_non_close_t_prefix_rejected(self):
        # Action prefixed with t<NNNN> but verb is not a close-verb.
        self.assertFalse(is_close_entry(_entry("t0001_dispatched_to_recovery")))


class TestHasMetrics(unittest.TestCase):
    def test_with_metrics(self):
        self.assertTrue(has_metrics(_entry("t0001_landed_x", metrics=_metrics())))

    def test_without_metrics(self):
        self.assertFalse(has_metrics(_entry("t0001_landed_x")))

    def test_metrics_without_cycle_shape(self):
        self.assertFalse(has_metrics(_entry("t0001_landed_x", metrics={"wall_clock_min": 5})))


class TestExtractTicketId(unittest.TestCase):
    def test_4_digit(self):
        self.assertEqual(extract_ticket_id(_entry("t0184_landed_x")), "T-0184")

    def test_5_digit(self):
        self.assertEqual(extract_ticket_id(_entry("t10042_landed_x")), "T-10042")

    def test_uppercase(self):
        self.assertEqual(extract_ticket_id(_entry("T0001_landed_x")), "T-0001")

    def test_no_match(self):
        self.assertIsNone(extract_ticket_id(_entry("ratify_x")))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAverageMetrics(unittest.TestCase):
    def test_averages_numeric_fields(self):
        e1 = _entry("t0001_landed_x", metrics=_metrics(wall_clock_min=10, plan_rebounds=0))
        e2 = _entry("t0002_landed_x", metrics=_metrics(wall_clock_min=20, plan_rebounds=2))
        avgs = average_metrics([e1, e2])
        self.assertEqual(avgs["wall_clock_min"], 15)
        self.assertEqual(avgs["plan_rebounds"], 1)

    def test_skips_entries_without_metrics(self):
        e1 = _entry("t0001_landed_x", metrics=_metrics(wall_clock_min=10))
        e2 = _entry("t0002_landed_x")  # no metrics
        avgs = average_metrics([e1, e2])
        self.assertEqual(avgs["wall_clock_min"], 10)

    def test_empty_input(self):
        self.assertEqual(average_metrics([]), {})


class TestFilterByShape(unittest.TestCase):
    def test_filters_to_named_shape(self):
        e1 = _entry("t0001_landed_x", metrics=_metrics(shape="feature"))
        e2 = _entry("t0002_landed_x", metrics=_metrics(shape="migration"))
        e3 = _entry("t0003_landed_x", metrics=_metrics(shape="feature"))
        result = filter_by_shape([e1, e2, e3], "feature")
        self.assertEqual(len(result), 2)
        self.assertIn(e1, result)
        self.assertIn(e3, result)


class TestFindByTicket(unittest.TestCase):
    def test_finds_matching_entry(self):
        e1 = _entry("t0184_landed_context_fill_monitor", metrics=_metrics())
        e2 = _entry("t0189_landed_per_agent", metrics=_metrics())
        self.assertEqual(find_by_ticket([e1, e2], "T-0184"), e1)

    def test_missing_ticket_returns_none(self):
        self.assertIsNone(find_by_ticket([], "T-9999"))


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoadEntries(unittest.TestCase):
    def test_loads_jsonl_and_skips_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            shard = Path(tmp) / "2026.jsonl"
            shard.write_text(
                json.dumps(_entry("t0001_landed_x")) + "\n"
                "not json\n"
                + json.dumps(_entry("t0002_landed_x")) + "\n",
                encoding="utf-8",
            )
            entries = load_entries(Path(tmp))
        self.assertEqual(len(entries), 2)

    def test_missing_dir_returns_empty(self):
        self.assertEqual(load_entries(Path("/nonexistent/path/cs")), [])

    def test_picks_up_subfolder_shards_post_migration(self):
        # T-0148: the weekly-subfolder layout — load_entries must descend into
        # ledger/<seg>/<seg>.jsonl. The old flat glob("*.jsonl") missed these.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for seg in ("2026-W18", "2026-W19"):
                (d / seg).mkdir()
                (d / seg / f"{seg}.jsonl").write_text(
                    json.dumps(_entry(f"t0001_landed_{seg}")) + "\n", encoding="utf-8"
                )
            entries = load_entries(d)
        self.assertEqual(len(entries), 2)
        actions = {e["action"] for e in entries}
        self.assertEqual(actions, {"t0001_landed_2026-W18", "t0001_landed_2026-W19"})

    def test_pre_migration_flat_file_still_read(self):
        # Backward-compat: before the migration only 2026.jsonl exists.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "2026.jsonl").write_text(
                json.dumps(_entry("t0001_landed_flat")) + "\n", encoding="utf-8"
            )
            entries = load_entries(d)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "t0001_landed_flat")

    def test_excludes_archive_file(self):
        # The post-migration archived legacy file must not be double-counted.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "2026-W18").mkdir()
            (d / "2026-W18" / "2026-W18.jsonl").write_text(
                json.dumps(_entry("t0001_landed_shard")) + "\n", encoding="utf-8"
            )
            (d / "2026.jsonl.archive-pre-d10-shard").write_text(
                json.dumps(_entry("t0001_landed_archived")) + "\n", encoding="utf-8"
            )
            entries = load_entries(d)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "t0001_landed_shard")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        shard = self.tmp_path / "2026.jsonl"
        rows = [
            _entry("t0001_landed_x", metrics=_metrics(shape="feature", wall_clock_min=30)),
            _entry("t0002_landed_x", metrics=_metrics(shape="feature", wall_clock_min=60, outcome="one-rebound")),
            _entry("t0003_landed_x", metrics=_metrics(shape="migration", wall_clock_min=120)),
            _entry("t0004_resolved_x", metrics=_metrics(shape="bugfix", wall_clock_min=15)),
            _entry("ratify_no_metrics"),  # not a close-entry; ignored
            _entry("t0005_landed_x_no_metrics"),  # close-entry but no metrics; ignored
        ]
        shard.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["metrics", "--ledger-dir", str(self.tmp_path), *argv])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_summary_default(self):
        out = self._run()
        self.assertIn("close-entries with metrics: 4", out)
        self.assertIn("feature: 2 cycle(s)", out)
        self.assertIn("migration: 1 cycle(s)", out)
        self.assertIn("bugfix: 1 cycle(s)", out)

    def test_shape_filter(self):
        out = self._run("--shape", "feature")
        self.assertIn("cycle_shape=feature: 2 cycle(s)", out)
        # Average of 30 + 60 = 45
        self.assertIn("avg wall_clock_min: 45.00", out)

    def test_last_filter(self):
        out = self._run("--last", "2")
        # Only last 2 close-entries should be considered: t0003 + t0004.
        self.assertIn("close-entries with metrics: 2", out)
        self.assertIn("migration", out)
        self.assertIn("bugfix", out)
        # Older feature entries excluded
        self.assertNotIn("feature: 2 cycle(s)", out)

    def test_similar_lookup(self):
        out = self._run("--similar", "T-0001")
        # T-0001 is shape=feature; should find both feature entries.
        self.assertIn("cycle_shape=feature: 2 cycle(s)", out)

    def test_similar_unknown_ticket(self):
        out = self._run("--similar", "T-9999")
        self.assertIn("no ledger entry found for ticket T-9999", out)

    def test_no_ledger_dir(self):
        # main returns 1 if no entries.
        rc = main(["metrics", "--ledger-dir", "/nonexistent/ledger/cs"])
        self.assertEqual(rc, 1)

    def test_no_subcommand_returns_usage(self):
        # No subcommand → exit code 2 with usage hint to stderr.
        rc = main([])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
