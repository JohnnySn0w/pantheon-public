"""Unit tests for tools/retro_metrics.py (T-0204).

Run from repo root:
    python3 -m unittest tools.test_retro_metrics -v

Covers:
  - wall-clock parser variants (min / hour / Hh+Mmin / reversed)
  - rebound count parser (per-tier, cycle-wide, zero-shortcut)
  - implementer/reviewer count parsers + their noun-suffix discipline
  - LoC parser including multi-match sum
  - outcome_class derivation from rebound counts
  - extract() composes all extractors + flag list
  - load_close_entries_without_metrics() respects metrics field presence
  - sidecar shape (review-state, ledger-sha, schema-ref)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.retro_metrics as rm


def _close_entry(action: str, rationale: str, *, metrics: dict | None = None) -> dict:
    e = {
        "id": "2026-01-01T00:00:00Z-001",
        "ts": "2026-01-01T00:00:00Z",
        "agent": "recovery",
        "action": action,
        "rationale": rationale,
        "justifies_via": ["P5"],
    }
    if metrics is not None:
        e["metrics"] = metrics
    return e


class TestExtractWallClock(unittest.TestCase):
    def test_minutes_before_wallclock(self):
        self.assertEqual(rm.extract_wall_clock_min("resolved ~50min wall-clock; clean"), 50)

    def test_minutes_no_tilde(self):
        self.assertEqual(rm.extract_wall_clock_min("30min wall-clock from claim"), 30)

    def test_hours_and_minutes(self):
        self.assertEqual(rm.extract_wall_clock_min("~1h45min wall-clock"), 105)

    def test_hours_only(self):
        self.assertEqual(rm.extract_wall_clock_min("~2hr wall-clock"), 120)

    def test_reversed_form(self):
        self.assertEqual(rm.extract_wall_clock_min("wall-clock: ~30min from open to close"), 30)

    def test_no_match(self):
        self.assertIsNone(rm.extract_wall_clock_min("a clean cycle landed today"))


class TestExtractRebounds(unittest.TestCase):
    def test_per_tier_counts(self):
        prose = "0/3 plan REBOUNDs + 1/3 code REBOUNDs + 0 reviewer REBOUNDs across all tiers"
        out = rm.extract_rebound_counts(prose)
        self.assertEqual(out, {"plan_rebounds": 0, "code_rebounds": 1, "reviewer_rebounds": 0})

    def test_cycle_wide_zero_any_tier(self):
        out = rm.extract_rebound_counts("~40min wall-clock; 0/3 REBOUNDs at any tier")
        self.assertEqual(out, {"plan_rebounds": 0, "code_rebounds": 0, "reviewer_rebounds": 0})

    def test_zero_rebounds_keyword(self):
        out = rm.extract_rebound_counts("cycle: zero REBOUNDs needed; landed first-pass")
        self.assertEqual(out, {"plan_rebounds": 0, "code_rebounds": 0, "reviewer_rebounds": 0})

    def test_no_match_all_none(self):
        out = rm.extract_rebound_counts("clean implementation; no surprises")
        self.assertEqual(out, {"plan_rebounds": None, "code_rebounds": None, "reviewer_rebounds": None})


class TestExtractCounts(unittest.TestCase):
    def test_implementer_subagents(self):
        self.assertEqual(rm.extract_implementer_count("dispatched 2 implementer subagents"), 2)

    def test_implementer_one_shots(self):
        self.assertEqual(rm.extract_implementer_count("3 implementer one-shots later"), 3)

    def test_implementer_rejects_modifier_phrase(self):
        # "3 implementer-added subprocess-fixture" is NOT a count of implementers.
        self.assertIsNone(rm.extract_implementer_count("3 implementer-added subprocess-fixture real path"))

    def test_implementer_rejects_b_cycle_collision(self):
        # "b30 implementer model" should NOT match (b30 is a cycle ref).
        self.assertIsNone(rm.extract_implementer_count("Claude Sonnet 4.6 per actual b30 implementer model"))

    def test_reviewer_one_shots(self):
        self.assertEqual(rm.extract_reviewer_count("2 reviewer one-shots; within HO budget"), 2)

    def test_reviewer_rejects_tier_rebound_phrase(self):
        # "0 reviewer REBOUNDs" is REBOUND count, not reviewer count.
        self.assertIsNone(rm.extract_reviewer_count("0 reviewer REBOUNDs across all tiers"))


class TestExtractLoc(unittest.TestCase):
    def test_single_match(self):
        self.assertEqual(rm.extract_loc("script ~120 LoC across the helper module"), 120)

    def test_multi_match_sums(self):
        # When prose mentions multiple LoC counts (script + tests), sum.
        self.assertEqual(
            rm.extract_loc("tools/ticket_state_sweep.py (~480 LoC) + tests (~480 LoC)"),
            960,
        )

    def test_lines_of_code_phrasing(self):
        self.assertEqual(rm.extract_loc("~150 lines of code across two files"), 150)

    def test_no_match(self):
        self.assertIsNone(rm.extract_loc("authoring-only cycle; no code"))


class TestDeriveOutcomeClass(unittest.TestCase):
    def test_clean_when_all_zero(self):
        self.assertEqual(
            rm.derive_outcome_class({"plan_rebounds": 0, "code_rebounds": 0, "reviewer_rebounds": 0}),
            "clean",
        )

    def test_one_rebound(self):
        self.assertEqual(
            rm.derive_outcome_class({"plan_rebounds": 0, "code_rebounds": 1, "reviewer_rebounds": 0}),
            "one-rebound",
        )

    def test_multi_rebound(self):
        self.assertEqual(
            rm.derive_outcome_class({"plan_rebounds": 1, "code_rebounds": 1, "reviewer_rebounds": 0}),
            "multi-rebound",
        )

    def test_escalated_at_three(self):
        self.assertEqual(
            rm.derive_outcome_class({"plan_rebounds": 2, "code_rebounds": 1, "reviewer_rebounds": 0}),
            "escalated",
        )

    def test_none_when_unknown(self):
        # Any None field → can't compute; flag for manual.
        self.assertIsNone(
            rm.derive_outcome_class({"plan_rebounds": 0, "code_rebounds": None, "reviewer_rebounds": 0})
        )


class TestExtractComposes(unittest.TestCase):
    def test_high_signal_prose(self):
        entry = _close_entry(
            "t0184_landed_x",
            "Cycle: ~40min wall-clock; 0/3 REBOUNDs at any tier. ~120 LoC across two files.",
        )
        extracted, flagged = rm.extract(entry)
        self.assertEqual(extracted["wall_clock_min"], 40)
        self.assertEqual(extracted["plan_rebounds"], 0)
        self.assertEqual(extracted["code_rebounds"], 0)
        self.assertEqual(extracted["reviewer_rebounds"], 0)
        self.assertEqual(extracted["scope_loc_delta"], 120)
        self.assertEqual(extracted["outcome_class"], "clean")
        # cycle_shape always flagged (judgment-laden); implementer/reviewer counts
        # absent here so also flagged.
        self.assertIn("cycle_shape", flagged)
        self.assertIn("implementer_count", flagged)
        self.assertIn("reviewer_count", flagged)

    def test_low_signal_prose(self):
        entry = _close_entry(
            "t0001_resolved_x",
            "Closed as superseded by T-0002. No code change.",
        )
        extracted, flagged = rm.extract(entry)
        self.assertEqual(extracted, {})
        # Every field flagged.
        self.assertEqual(set(flagged), set(rm.ALL_METRIC_FIELDS))


class TestLoadCloseEntries(unittest.TestCase):
    def test_filters_non_close_actions(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "test.jsonl"
            entries = [
                _close_entry("t0001_resolved_x", "first close"),
                _close_entry("ratify_principles_v0", "not a close"),
                _close_entry("t0002_landed_y", "second close"),
            ]
            ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            loaded = rm.load_close_entries_without_metrics(ledger)
        self.assertEqual([e["action"] for e in loaded], ["t0001_resolved_x", "t0002_landed_y"])

    def test_filters_entries_with_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "test.jsonl"
            entries = [
                _close_entry("t0001_resolved_x", "no metrics"),
                _close_entry(
                    "t0002_resolved_y",
                    "has metrics",
                    metrics={"wall_clock_min": 10, "cycle_shape": "feature"},
                ),
            ]
            ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            loaded = rm.load_close_entries_without_metrics(ledger)
        self.assertEqual([e["action"] for e in loaded], ["t0001_resolved_x"])

    def test_missing_ledger_returns_empty(self):
        self.assertEqual(rm.load_close_entries_without_metrics(Path("/nonexistent.jsonl")), [])

    def test_dir_path_reads_union_of_legacy_and_subfolder_shards(self):
        # T-0148 (review test-gap): passing the ledger DIR reads the union via
        # iter_ledger_entry_files — legacy flat file + per-week subfolder shards.
        with tempfile.TemporaryDirectory() as td:
            ledger_dir = Path(td)
            # legacy flat file
            (ledger_dir / "2026.jsonl").write_text(
                json.dumps(_close_entry("t0001_resolved_legacy", "x")) + "\n"
            )
            # a subfolder shard
            seg = ledger_dir / "2026-W20"
            seg.mkdir()
            (seg / "2026-W20.jsonl").write_text(
                json.dumps(_close_entry("t0002_resolved_shard", "y")) + "\n"
            )
            # an archive backup must be EXCLUDED
            (ledger_dir / "2026.jsonl.archive-pre-d10-shard").write_text(
                json.dumps(_close_entry("t0003_resolved_archive", "z")) + "\n"
            )
            loaded = rm.load_close_entries_without_metrics(ledger_dir)
        actions = {e["action"] for e in loaded}
        self.assertEqual(actions, {"t0001_resolved_legacy", "t0002_resolved_shard"})


class TestSidecarShape(unittest.TestCase):
    def test_sidecar_carries_review_state_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            entries = [
                _close_entry("t0001_resolved_x", "~30min wall-clock; clean cycle. 0/3 REBOUNDs at any tier."),
            ]
            ledger.write_text(json.dumps(entries[0]) + "\n")
            sidecar = rm.build_sidecar(ledger, entries)
        self.assertEqual(sidecar["review_state"], "unreviewed")
        self.assertEqual(len(sidecar["ledger_sha256_at_extract"]), 64)
        self.assertIn("schema_ref", sidecar)
        row = sidecar["entries"]["2026-01-01T00:00:00Z-001"]
        self.assertEqual(row["extracted"]["wall_clock_min"], 30)
        self.assertEqual(row["extracted"]["outcome_class"], "clean")
        self.assertIn("cycle_shape", row["flagged_fields"])


if __name__ == "__main__":
    unittest.main()
