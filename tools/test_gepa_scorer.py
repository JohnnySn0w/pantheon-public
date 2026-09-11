"""Tests for tools/gepa/scorer.py (T-2809 Phase 2).

Run: python3 -m unittest tools.test_gepa_scorer -v

No `gepa` import, no live LM calls — `judge_fn` is always a fake callable
here. The load-bearing assertions are: (1) must_not_flag hits are penalized;
(2) the constructed `feedback` string never contains gold `description` text
anywhere in the committed case set (the anti-leakage contract for whatever
eventually flows into `make_reflective_dataset`).
"""

from __future__ import annotations

import unittest

from tools.gepa import case_loader, scorer
from tools.gepa.case_loader import Case


POSITIVE_CASE = {
    "case_id": "T-9101",
    "class": "positive",
    "split": "train",
    "source_ticket": "T-9101",
    "source_path": "tickets/done/T-9101.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {"ask_summary": "s", "diff_ref": {"repo": None, "sha": None}, "review_kind": "diff"},
    "expected": {
        "verdict": "REBOUND",
        "findings": [{"category": "unbounded-retry-loop", "description": "GOLD-DESC-ONE the retry has no cap"}],
        "must_not_flag": [],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}

MIXED_CASE = {
    "case_id": "T-9102",
    "class": "positive",
    "split": "train",
    "source_ticket": "T-9102",
    "source_path": "tickets/done/T-9102.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {"ask_summary": "s", "diff_ref": {"repo": None, "sha": None}, "review_kind": "diff"},
    "expected": {
        "verdict": "REBOUND",
        "findings": [{"category": "unbounded-retry-loop", "description": "GOLD-DESC-TWO retry has no cap"}],
        "must_not_flag": [{"category": "irrelevant-style-nit", "description": "GOLD-DESC-THREE brace style should not be a blocker"}],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}

NEGATIVE_CASE = {
    "case_id": "T-9103",
    "class": "negative_invalid_rebound",
    "split": "train",
    "source_ticket": "T-9103",
    "source_path": "tickets/done/T-9103.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {"ask_summary": "s", "diff_ref": {"repo": None, "sha": None}, "review_kind": "diff"},
    "expected": {
        "verdict": "APPROVE",
        "findings": [],
        "must_not_flag": [{"category": "report-boundary-vs-commit-body-overreach", "description": "GOLD-DESC-FOUR commit body must carry DECISIONS is wrong"}],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}


def _case(d):
    return Case.from_json_dict(d)


class TestParseVerdict(unittest.TestCase):
    def test_approve_only(self):
        self.assertEqual(scorer.parse_verdict("Looks good. APPROVE."), "APPROVE")

    def test_rebound_only(self):
        self.assertEqual(scorer.parse_verdict("REBOUND: fix the retry loop."), "REBOUND")

    def test_neither_present_is_none(self):
        self.assertIsNone(scorer.parse_verdict("This code looks fine to me."))

    def test_both_present_is_none_ambiguous(self):
        self.assertIsNone(scorer.parse_verdict("APPROVE with a note, though normally I'd REBOUND this."))

    def test_lowercase_mentions_do_not_count(self):
        self.assertIsNone(scorer.parse_verdict("i would approve or rebound depending on context"))


class TestDeterministicCategoryHit(unittest.TestCase):
    def test_hits_when_majority_of_keyword_tokens_present(self):
        self.assertTrue(scorer.deterministic_category_hit("unbounded-retry-loop", "the retry loop never bounds its attempts"))

    def test_misses_when_no_tokens_present(self):
        self.assertFalse(scorer.deterministic_category_hit("unbounded-retry-loop", "the widget color is wrong"))


class TestScoreRolloutPositiveCase(unittest.TestCase):
    def setUp(self):
        self.case = _case(POSITIVE_CASE)

    def test_correct_verdict_and_caught_finding_scores_high(self):
        result = scorer.score_rollout(self.case, "REBOUND: the retry loop has no bound on attempts.")
        self.assertEqual(result.verdict_found, "REBOUND")
        self.assertTrue(result.verdict_correct)
        self.assertIn("unbounded-retry-loop", result.caught_categories)
        self.assertEqual(result.missed_categories, ())
        self.assertGreater(result.score, 0.9)

    def test_wrong_verdict_and_missed_finding_scores_low(self):
        result = scorer.score_rollout(self.case, "APPROVE: looks fine to me, nothing to add.")
        self.assertFalse(result.verdict_correct)
        self.assertIn("unbounded-retry-loop", result.missed_categories)
        self.assertLess(result.score, 0.3)

    def test_judge_fn_can_rescue_a_deterministic_miss(self):
        judge_fn = lambda prompt: "YES, this concern is substantively raised."
        result = scorer.score_rollout(self.case, "REBOUND: this fails to terminate under persistent failure.", judge_fn=judge_fn)
        self.assertIn("unbounded-retry-loop", result.caught_categories)

    def test_judge_fn_returning_no_still_counts_as_missed(self):
        judge_fn = lambda prompt: "NO"
        result = scorer.score_rollout(self.case, "REBOUND: something entirely unrelated.", judge_fn=judge_fn)
        self.assertIn("unbounded-retry-loop", result.missed_categories)


class TestScoreRolloutMixedCase(unittest.TestCase):
    def setUp(self):
        self.case = _case(MIXED_CASE)

    def test_catches_valid_finding_but_also_wrongly_flags_must_not_flag(self):
        review = (
            "REBOUND: the retry loop has no bound on attempts. Also, this brace style nit "
            "is irrelevant but I'm blocking on it anyway."
        )
        result = scorer.score_rollout(self.case, review)
        self.assertIn("unbounded-retry-loop", result.caught_categories)
        self.assertIn("irrelevant-style-nit", result.wrongly_flagged_categories)
        # Penalized relative to a clean catch.
        clean_result = scorer.score_rollout(self.case, "REBOUND: the retry loop has no bound on attempts.")
        self.assertLess(result.score, clean_result.score)


class TestScoreRolloutNegativeCase(unittest.TestCase):
    def setUp(self):
        self.case = _case(NEGATIVE_CASE)

    def test_correct_approve_with_no_false_finding_scores_high(self):
        result = scorer.score_rollout(self.case, "APPROVE: no issues found, commit body format is fine either way.")
        self.assertTrue(result.verdict_correct)
        self.assertEqual(result.wrongly_flagged_categories, ())
        self.assertGreater(result.score, 0.9)

    def test_reproducing_the_invalid_rebound_is_penalized(self):
        review = (
            "REBOUND: this is a report-boundary overreach — the commit body must carry a "
            "DECISIONS section, amend the commit."
        )
        result = scorer.score_rollout(self.case, review)
        self.assertIn("report-boundary-vs-commit-body-overreach", result.wrongly_flagged_categories)
        clean_result = scorer.score_rollout(self.case, "APPROVE: fine as-is.")
        self.assertLess(result.score, clean_result.score)


class TestFeedbackNeverLeaksGoldText(unittest.TestCase):
    def test_feedback_excludes_gold_description_text_for_fixtures(self):
        for case_dict in (POSITIVE_CASE, MIXED_CASE, NEGATIVE_CASE):
            case = _case(case_dict)
            # Deliberately score a WRONG rollout so missed/wrongly-flagged
            # categories are populated (the risky path for leakage).
            wrong_review = "APPROVE: nothing else to say." if case.expected.verdict == "REBOUND" else "REBOUND: something is wrong here, commit body needs it."
            result = scorer.score_rollout(case, wrong_review)
            for finding in case.expected.findings:
                self.assertNotIn(finding.description, result.feedback)
            for finding in case.expected.must_not_flag:
                self.assertNotIn(finding.description, result.feedback)

    def test_feedback_excludes_gold_description_text_across_full_committed_case_set(self):
        for case in case_loader.load_all_cases().values():
            wrong_review = "APPROVE: nothing else to say." if case.expected.verdict == "REBOUND" else "REBOUND: something is wrong here."
            result = scorer.score_rollout(case, wrong_review)
            for finding in case.expected.findings:
                self.assertNotIn(finding.description, result.feedback, f"{case.case_id}: leaked finding description")
            for finding in case.expected.must_not_flag:
                self.assertNotIn(finding.description, result.feedback, f"{case.case_id}: leaked must_not_flag description")

    def test_feedback_mentions_category_slugs_not_raw_judge_text(self):
        judge_fn = lambda prompt: "YES because of some elaborate raw reasoning text that should never surface verbatim"
        case = _case(POSITIVE_CASE)
        result = scorer.score_rollout(case, "REBOUND: this fails under persistent failure conditions.", judge_fn=judge_fn)
        self.assertNotIn("elaborate raw reasoning", result.feedback)


class TestScoreIsClippedToUnitInterval(unittest.TestCase):
    def test_score_never_below_zero(self):
        case = _case(MIXED_CASE)
        result = scorer.score_rollout(case, "APPROVE: brace style nit, commit body must carry DECISIONS.")
        self.assertGreaterEqual(result.score, 0.0)

    def test_score_never_above_one(self):
        case = _case(POSITIVE_CASE)
        result = scorer.score_rollout(case, "REBOUND: the retry loop has no bound on attempts, unbounded retry loop confirmed.")
        self.assertLessEqual(result.score, 1.0)


if __name__ == "__main__":
    unittest.main()
