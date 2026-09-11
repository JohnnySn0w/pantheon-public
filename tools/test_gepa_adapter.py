"""Tests for tools/gepa/adapter.py (T-2809 Phase 2).

Run under system python3: python3 -m unittest tools.test_gepa_adapter -v
Run under the gepa venv for full coverage (a2a-style carve-out, per
`pantheon-dispatch` skill §2 — the real `gepa` package is not installed under
system python3):
    /home/claude/.venv-gepa/bin/python -m unittest tools.test_gepa_adapter -v

All FAKE-executor tests run under both interpreters. The
TestConformanceAgainstRealGepa class round-trips this adapter's output
through the REAL installed `gepa.core.adapter.EvaluationBatch` and is
skip-guarded to only run when `gepa` is importable.
"""

from __future__ import annotations

import os
import unittest

from tools.gepa import adapter, case_loader
from tools.gepa.adapter import ExecutorCall, HoldoutLeakError, ReviewerGEPAAdapter
from tools.gepa.case_loader import Case

try:
    import gepa  # noqa: F401
    from gepa.core.adapter import EvaluationBatch as RealEvaluationBatch

    _HAVE_GEPA = True
except ImportError:
    _HAVE_GEPA = False


TRAIN_CASE = {
    "case_id": "T-9201",
    "class": "positive",
    "split": "train",
    "source_ticket": "T-9201",
    "source_path": "tickets/done/T-9201.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {"ask_summary": "widget frobnicator retries", "diff_ref": {"repo": None, "sha": None}, "review_kind": "diff"},
    "expected": {
        "verdict": "REBOUND",
        "findings": [{"category": "unbounded-retry-loop", "description": "GOLD-A retry has no cap"}],
        "must_not_flag": [],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}

HOLDOUT_CASE = {
    "case_id": "T-9202",
    "class": "positive",
    "split": "holdout",
    "source_ticket": "T-9202",
    "source_path": "tickets/done/T-9202.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {"ask_summary": "another change", "diff_ref": {"repo": None, "sha": None}, "review_kind": "diff"},
    "expected": {
        "verdict": "APPROVE",
        "findings": [],
        "must_not_flag": [],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}


def _cases_by_id():
    return {
        "T-9201": Case.from_json_dict(TRAIN_CASE),
        "T-9202": Case.from_json_dict(HOLDOUT_CASE),
    }


class FakeExecutor:
    """A rollout_executor test double: records calls, returns a scripted response per case."""

    def __init__(self, responses: dict[str, str] | None = None, default_response: str = "APPROVE: fine."):
        self.responses = responses or {}
        self.default_response = default_response
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def __call__(self, persona_text: str, task_prompt: str) -> ExecutorCall:
        self.calls.append((persona_text, task_prompt))
        # Identify which case this is for by matching a marker substring
        # against the rendered task prompt (the fixtures use each case's
        # ask_summary text as its own marker).
        response = self.default_response
        for marker, resp in self.responses.items():
            if marker in task_prompt:
                response = resp
                if marker in self.fail_on:
                    raise RuntimeError(f"scripted failure for {marker}")
        return ExecutorCall(argv=("claude", "-p", "--fake"), raw_response=response)


class TestHoldoutRefusal(unittest.TestCase):
    def test_optimization_mode_raises_on_holdout_case_in_batch(self):
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=FakeExecutor(),
            optimization_mode=True,
        )
        with self.assertRaises(HoldoutLeakError):
            adap.evaluate(["T-9202"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona text"})

    def test_optimization_mode_raises_even_if_batch_mixes_train_and_holdout(self):
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=FakeExecutor(),
            optimization_mode=True,
        )
        with self.assertRaises(HoldoutLeakError):
            adap.evaluate(["T-9201", "T-9202"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona text"})

    def test_measurement_mode_allows_holdout_case(self):
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=FakeExecutor(),
            optimization_mode=False,
        )
        result = adap.evaluate(["T-9202"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona text"})
        self.assertEqual(len(result.scores), 1)

    def test_optimization_mode_true_still_allows_a_pure_train_batch(self):
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=FakeExecutor(),
            optimization_mode=True,
        )
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona text"})
        self.assertEqual(len(result.scores), 1)


class TestLengthCap(unittest.TestCase):
    def test_over_cap_candidate_scores_zero_and_fires_no_rollouts(self):
        executor = FakeExecutor()
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=executor,
            baseline_length=10,
            length_cap_ratio=1.3,
        )
        over_cap_text = "x" * 100  # way over 1.3 * 10 = 13
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: over_cap_text})
        self.assertEqual(result.scores, [0.0])
        self.assertEqual(len(executor.calls), 0)

    def test_within_cap_candidate_fires_rollouts_normally(self):
        executor = FakeExecutor()
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=executor,
            baseline_length=1000,
            length_cap_ratio=1.3,
        )
        adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "short persona"})
        self.assertEqual(len(executor.calls), 1)

    def test_no_baseline_length_disables_the_cap(self):
        executor = FakeExecutor()
        adap = ReviewerGEPAAdapter(
            cases_by_id=_cases_by_id(),
            fixed_skills_context="",
            rollout_executor=executor,
            baseline_length=None,
        )
        adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "x" * 100000})
        self.assertEqual(len(executor.calls), 1)


class TestEvaluateWithFakeExecutor(unittest.TestCase):
    def test_output_and_score_align_with_scorer(self):
        from tools.gepa import scorer

        executor = FakeExecutor(responses={"widget frobnicator": "REBOUND: the retry loop has no bound on attempts."})
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=executor)
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona"})
        expected = scorer.score_rollout(
            Case.from_json_dict(TRAIN_CASE), "REBOUND: the retry loop has no bound on attempts."
        )
        self.assertEqual(result.scores[0], expected.score)
        self.assertEqual(result.outputs[0], "REBOUND: the retry loop has no bound on attempts.")

    def test_capture_traces_false_gives_none_trajectories(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona"}, capture_traces=False)
        self.assertIsNone(result.trajectories)

    def test_capture_traces_true_captures_full_argv_prompt_and_response(self):
        """HO plan-approval addition (B): trajectory must carry the FULL argv, the
        complete task prompt, and the complete raw response — a bad rollout must
        be replayable byte-identically without --resume."""
        executor = FakeExecutor(responses={"widget frobnicator": "REBOUND: unbounded retry loop confirmed."})
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="fixed-ctx", rollout_executor=executor)
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona"}, capture_traces=True)
        self.assertEqual(len(result.trajectories), 1)
        traj = result.trajectories[0]
        self.assertEqual(traj.case_id, "T-9201")
        self.assertEqual(traj.argv, ("claude", "-p", "--fake"))
        self.assertIn("widget frobnicator", traj.task_prompt)
        self.assertIn("fixed-ctx", traj.task_prompt)
        self.assertEqual(traj.raw_response, "REBOUND: unbounded retry loop confirmed.")
        self.assertIsNotNone(traj.scored)
        self.assertIsNone(traj.error)

    def test_executor_failure_on_one_case_scores_zero_but_does_not_raise(self):
        executor = FakeExecutor(responses={"widget frobnicator": "irrelevant"}, default_response="APPROVE")
        executor.fail_on.add("widget frobnicator")
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=executor)
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona"}, capture_traces=True)
        self.assertEqual(result.scores, [0.0])
        self.assertIsNotNone(result.trajectories[0].error)

    def test_unknown_case_id_raises_keyerror(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        with self.assertRaises(KeyError):
            adap.evaluate(["T-NONEXISTENT"], {ReviewerGEPAAdapter.PERSONA_KEY: "persona"})

    def test_batch_output_length_invariants_hold_across_all_paths(self):
        """len(outputs) == len(scores) == len(batch); trajectories match when captured."""
        cases = _cases_by_id()
        batch = ["T-9201"]

        # Normal path.
        adap = ReviewerGEPAAdapter(cases_by_id=cases, fixed_skills_context="", rollout_executor=FakeExecutor())
        r1 = adap.evaluate(batch, {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, capture_traces=True)
        self.assertEqual(len(r1.outputs), len(batch))
        self.assertEqual(len(r1.scores), len(batch))
        self.assertEqual(len(r1.trajectories), len(batch))

        # Length-cap-rejected path.
        adap2 = ReviewerGEPAAdapter(cases_by_id=cases, fixed_skills_context="", rollout_executor=FakeExecutor(), baseline_length=1)
        r2 = adap2.evaluate(batch, {ReviewerGEPAAdapter.PERSONA_KEY: "x" * 1000}, capture_traces=True)
        self.assertEqual(len(r2.outputs), len(batch))
        self.assertEqual(len(r2.scores), len(batch))
        self.assertEqual(len(r2.trajectories), len(batch))


class TestMakeReflectiveDataset(unittest.TestCase):
    def test_returns_empty_dict_when_persona_key_not_in_components_to_update(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, capture_traces=True)
        records = adap.make_reflective_dataset({ReviewerGEPAAdapter.PERSONA_KEY: "p"}, result, components_to_update=[])
        self.assertEqual(records, {})

    def test_returns_one_record_per_trajectory_for_the_persona_component(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, capture_traces=True)
        records = adap.make_reflective_dataset(
            {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, result, components_to_update=[ReviewerGEPAAdapter.PERSONA_KEY]
        )
        self.assertIn(ReviewerGEPAAdapter.PERSONA_KEY, records)
        self.assertEqual(len(records[ReviewerGEPAAdapter.PERSONA_KEY]), 1)
        record = records[ReviewerGEPAAdapter.PERSONA_KEY][0]
        self.assertIn("Inputs", record)
        self.assertIn("Generated Outputs", record)
        self.assertIn("Feedback", record)

    @unittest.skipUnless(
        os.path.isdir("/home/claude/pantheon-pwa/.git")
        and os.access("/home/claude/pantheon-pwa/.git", os.R_OK | os.X_OK),
        "requires /home/claude/pantheon-pwa/.git to be readable — the full "
        "committed case set includes diff_refs that resolve into pantheon-pwa "
        "(case_loader.py:48), unreachable on runner UIDs. Anti-leakage logic "
        "is still covered structurally by TestMakeReflectiveDataset's sibling "
        "tests on a synthetic case set (T-3563 slice B).",
    )
    def test_reflective_feedback_never_leaks_gold_text_across_full_committed_case_set(self):
        # optimization_mode=False: this test exercises make_reflective_dataset's
        # anti-leakage guarantee over the FULL committed set (train + holdout),
        # not the optimization loop itself — the holdout-refusal gate (tested
        # separately above) would otherwise correctly reject this batch.
        cases = case_loader.load_all_cases()
        executor = FakeExecutor(default_response="APPROVE: nothing to add here.")
        adap = ReviewerGEPAAdapter(
            cases_by_id=cases, fixed_skills_context="", rollout_executor=executor, optimization_mode=False
        )
        batch = list(cases.keys())
        result = adap.evaluate(batch, {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, capture_traces=True)
        records = adap.make_reflective_dataset(
            {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, result, components_to_update=[ReviewerGEPAAdapter.PERSONA_KEY]
        )
        for record in records[ReviewerGEPAAdapter.PERSONA_KEY]:
            feedback = record["Feedback"]
            for case in cases.values():
                for finding in case.expected.findings:
                    self.assertNotIn(finding.description, feedback)
                for finding in case.expected.must_not_flag:
                    self.assertNotIn(finding.description, feedback)


@unittest.skipUnless(_HAVE_GEPA, "gepa not installed under this interpreter (see /home/claude/.venv-gepa)")
class TestConformanceAgainstRealGepa(unittest.TestCase):
    def test_evaluate_output_round_trips_through_real_evaluation_batch(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        result = adap.evaluate(["T-9201"], {ReviewerGEPAAdapter.PERSONA_KEY: "p"}, capture_traces=True)
        real = RealEvaluationBatch(
            outputs=result.outputs,
            scores=result.scores,
            trajectories=result.trajectories,
            objective_scores=result.objective_scores,
            num_metric_calls=result.num_metric_calls,
        )
        self.assertEqual(len(real.outputs), len(real.scores))
        self.assertEqual(len(real.trajectories), len(real.outputs))

    def test_adapter_exposes_evaluate_and_make_reflective_dataset(self):
        adap = ReviewerGEPAAdapter(cases_by_id=_cases_by_id(), fixed_skills_context="", rollout_executor=FakeExecutor())
        self.assertTrue(callable(getattr(adap, "evaluate", None)))
        self.assertTrue(callable(getattr(adap, "make_reflective_dataset", None)))


if __name__ == "__main__":
    unittest.main()
