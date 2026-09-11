"""Tests for tools/gepa/report.py (T-2809 Phase 2).

Run: python3 -m unittest tools.test_gepa_report -v

Pure data-in/data-out — no `gepa` import, no LM calls, no case_loader
dependency (report.py takes already-scored results).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.gepa import report
from tools.gepa.scorer import ScoredRollout


def _scored(score: float, verdict_correct: bool = True, caught=(), missed=(), wrongly_flagged=()) -> ScoredRollout:
    return ScoredRollout(
        score=score,
        verdict_found="REBOUND",
        verdict_correct=verdict_correct,
        caught_categories=caught,
        missed_categories=missed,
        wrongly_flagged_categories=wrongly_flagged,
        feedback="fixture feedback",
    )


class TestUnifiedDiff(unittest.TestCase):
    def test_shows_added_and_removed_lines(self):
        diff = report.unified_diff("line one\nline two\n", "line one\nline THREE\n")
        self.assertIn("-line two", diff)
        self.assertIn("+line THREE", diff)

    def test_identical_text_produces_empty_diff(self):
        diff = report.unified_diff("same\n", "same\n")
        self.assertEqual(diff.strip(), "")


class TestBuildReport(unittest.TestCase):
    def test_computes_mean_before_and_after_scores(self):
        before = {"T-1": _scored(0.2), "T-2": _scored(0.4)}
        after = {"T-1": _scored(0.8), "T-2": _scored(0.9)}
        result = report.build_report(
            seed_persona_text="seed text",
            best_persona_text="best text",
            before_scored=before,
            after_scored=after,
            total_metric_calls=42,
            run_dir="/tmp/gepa_phase2_run",
        )
        self.assertAlmostEqual(result["holdout"]["mean_before"], 0.3)
        self.assertAlmostEqual(result["holdout"]["mean_after"], 0.85)
        self.assertAlmostEqual(result["holdout"]["mean_delta"], 0.55)

    def test_per_case_breakdown_present_for_every_case(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        self.assertIn("T-1", result["holdout"]["per_case"])
        self.assertEqual(result["holdout"]["per_case"]["T-1"]["before"], 0.2)
        self.assertEqual(result["holdout"]["per_case"]["T-1"]["after"], 0.8)

    def test_mismatched_case_id_sets_raises_value_error(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-2": _scored(0.8)}
        with self.assertRaises(ValueError):
            report.build_report(
                seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
                total_metric_calls=1, run_dir=None,
            )

    def test_includes_persona_unified_diff(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="line one\n", best_persona_text="line ONE\n",
            before_scored=before, after_scored=after, total_metric_calls=1, run_dir=None,
        )
        self.assertIn("persona_diff", result)
        self.assertIn("-line one", result["persona_diff"])

    def test_never_auto_applies_flag_present_and_true(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        self.assertTrue(result["nothing_auto_applies"])


class TestRenderMarkdown(unittest.TestCase):
    def test_markdown_mentions_before_after_numbers(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=7, run_dir="/tmp/run",
        )
        md = report.render_markdown(result)
        self.assertIn("0.2", md)
        self.assertIn("0.8", md)
        self.assertIn("ratify", md.lower())

    def test_markdown_never_claims_auto_applied(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        md = report.render_markdown(result)
        self.assertIn("Nothing has been applied automatically", md)


class TestWriteReport(unittest.TestCase):
    def test_writes_json_and_markdown_under_tmp(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        md = report.render_markdown(result)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "gepa_phase2_report"
            json_path, md_path = report.write_report(result, md, out_dir)
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())
            written = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(written["holdout"]["mean_before"], 0.2)

    def test_refuses_a_repo_internal_out_dir_with_explicit_repo_root(self):
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        md = report.render_markdown(result)
        fake_repo_root = Path("/home/claude/pantheon-config")
        with self.assertRaises(ValueError):
            report.write_report(result, md, fake_repo_root / "knowledge" / "some_report_dir", repo_root=fake_repo_root)

    def test_refuses_a_repo_internal_out_dir_using_the_real_default_repo_root(self):
        # Regression test for the actual T-2809 defect: this test process's real
        # REPO_ROOT is itself a /tmp-staged worktree (this org's normal dispatch
        # shape). An out_dir nested inside it (e.g. knowledge/some_report_dir) must
        # still be refused via the DEFAULT repo_root — no override needed — because
        # a prior version of _refuse_if_repo_internal checked "is this under /tmp?"
        # before checking repo-membership, so a /tmp-staged repo_root silently
        # defeated the refusal for exactly this shape. That defect is what produced
        # the real knowledge/some_report_dir/{report.json,report.md} debris in this
        # worktree during Phase-2 development.
        before = {"T-1": _scored(0.2)}
        after = {"T-1": _scored(0.8)}
        result = report.build_report(
            seed_persona_text="s", best_persona_text="b", before_scored=before, after_scored=after,
            total_metric_calls=1, run_dir=None,
        )
        md = report.render_markdown(result)
        with self.assertRaises(ValueError):
            report.write_report(result, md, report.REPO_ROOT / "knowledge" / "some_report_dir")


if __name__ == "__main__":
    unittest.main()
