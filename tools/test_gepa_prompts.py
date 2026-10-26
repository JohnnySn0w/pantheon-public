"""Tests for tools/gepa/prompts.py (T-2809 Phase 2).

Run: python3 -m unittest tools.test_gepa_prompts -v

No `gepa` import. The load-bearing assertion here is anti-leakage: the task
prompt shown to the EXECUTOR must never contain the case's own gold
`expected.findings[]` / `expected.must_not_flag[]` description text — that
would hand the executor the answer key and make the eval meaningless, not
just leak into the reflector (a separate contract enforced in
tools/test_gepa_scorer.py).
"""

from __future__ import annotations

import unittest

from tools.gepa import case_loader, prompts
from tools.gepa.case_loader import Case


SAMPLE_CASE_DICT = {
    "case_id": "T-9002",
    "class": "positive",
    "split": "train",
    "source_ticket": "T-9002",
    "source_path": "tickets/done/T-9002.md",
    "provenance": {"requester": "x", "target": "y", "labels": ["a"], "round": None},
    "review_material": {
        "ask_summary": "Implement a widget frobnicator with bounded retries.",
        "diff_ref": {"repo": "pantheon-config", "sha": None},
        "review_kind": "diff",
    },
    "expected": {
        "verdict": "REBOUND",
        "findings": [
            {
                "category": "unbounded-retry-loop",
                "description": "The retry loop has no maximum attempt count, so a persistently failing frobnicator spins forever, a very specific gold-only phrase XYZZY-GOLD-MARKER",
            }
        ],
        "must_not_flag": [
            {
                "category": "irrelevant-style-nit",
                "description": "A correct reviewer must not flag brace style, another gold-only phrase PLUGH-GOLD-MARKER",
            }
        ],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}


class TestBuildAgentsJson(unittest.TestCase):
    def test_embeds_candidate_text_verbatim(self):
        payload = prompts.build_agents_persona_payload("candidate persona text goes here")
        self.assertIn("candidate persona text goes here", payload)


class TestBuildCaseTaskPrompt(unittest.TestCase):
    def setUp(self):
        self.case = Case.from_json_dict(SAMPLE_CASE_DICT)

    def test_includes_ask_summary(self):
        text = prompts.build_case_task_prompt(self.case, diff_text=None, fixed_skills_context="")
        self.assertIn("widget frobnicator", text)

    def test_includes_diff_text_when_present(self):
        text = prompts.build_case_task_prompt(self.case, diff_text="--- a/foo\n+++ b/foo\n", fixed_skills_context="")
        self.assertIn("--- a/foo", text)

    def test_narrative_only_fallback_when_diff_is_none(self):
        text = prompts.build_case_task_prompt(self.case, diff_text=None, fixed_skills_context="")
        # Must still be a coherent task (the ask_summary), not an empty/broken prompt.
        self.assertIn("widget frobnicator", text)

    def test_includes_fixed_skills_context(self):
        text = prompts.build_case_task_prompt(self.case, diff_text=None, fixed_skills_context="SKILL-CONTEXT-MARKER-42")
        self.assertIn("SKILL-CONTEXT-MARKER-42", text)

    def test_never_contains_gold_finding_description_text(self):
        text = prompts.build_case_task_prompt(self.case, diff_text="some diff", fixed_skills_context="ctx")
        self.assertNotIn("XYZZY-GOLD-MARKER", text)

    def test_never_contains_gold_must_not_flag_description_text(self):
        text = prompts.build_case_task_prompt(self.case, diff_text="some diff", fixed_skills_context="ctx")
        self.assertNotIn("PLUGH-GOLD-MARKER", text)

    def test_never_contains_any_gold_text_across_the_full_committed_case_set(self):
        """Regression-proof against future eval-set cases too, not just the fixture."""
        for case in case_loader.load_all_cases().values():
            text = prompts.build_case_task_prompt(case, diff_text="diff placeholder", fixed_skills_context="ctx")
            for finding in case.expected.findings:
                self.assertNotIn(
                    finding.description, text,
                    f"{case.case_id}: gold finding description leaked into task prompt",
                )
            for finding in case.expected.must_not_flag:
                self.assertNotIn(
                    finding.description, text,
                    f"{case.case_id}: gold must_not_flag description leaked into task prompt",
                )

    def test_null_diff_narrative_path_never_leaks_gold_text_across_committed_set(self):
        """T-3837 v2: when diff_text is None the source_path narrative is read
        into the prompt. That path must remain leak-free — a gold description
        that happens to also appear in the ticket transition-log text would
        hand the executor the answer key by a different route."""
        for case in case_loader.load_all_cases().values():
            text = prompts.build_case_task_prompt(case, diff_text=None, fixed_skills_context="ctx")
            for finding in case.expected.findings:
                self.assertNotIn(
                    finding.description, text,
                    f"{case.case_id}: null-diff narrative leaked gold finding description",
                )
            for finding in case.expected.must_not_flag:
                self.assertNotIn(
                    finding.description, text,
                    f"{case.case_id}: null-diff narrative leaked gold must_not_flag description",
                )

    def test_prompt_requests_the_reviewer_output_contract(self):
        text = prompts.build_case_task_prompt(self.case, diff_text=None, fixed_skills_context="")
        self.assertIn("APPROVE", text)
        self.assertIn("REBOUND", text)


if __name__ == "__main__":
    unittest.main()
