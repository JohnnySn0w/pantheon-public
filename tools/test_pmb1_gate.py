#!/usr/bin/env python3
"""PMB-1 tier-1 gate evaluator (T-2518): the gate CAN FAIL, one test per
criterion, with the right criterion named. In-memory substrates only."""
from __future__ import annotations

import copy
import sys
import unittest

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import offline
from tools.pmb1 import runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.embedder import HashingEmbedder
from tools.pmb1.gate import gate

gatem = sys.modules["tools.pmb1.gate"]  # the package re-exports the FUNCTION under the module's name
from tools.pmb1.substrate import ROLE_CANDIDATE, Evidence, VerbatimRAGBaseline

FX = fxm.generate(1, "small")
BBS = [StubBackbone("strict"), StubBackbone("lenient")]

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()



class GoodCandidate(VerbatimRAGBaseline):
    """A well-behaved candidate (RAG under another name and role)."""
    name = "candidate-good"
    role = ROLE_CANDIDATE


class ReturnsNothing(GoodCandidate):
    name = "candidate-nothing"

    def _retrieve(self, query, purpose, budget_tokens):
        return []


class IgnoresForget(GoodCandidate):
    name = "candidate-ignores-forget"

    def _forget(self, ref):
        return None


class IgnoresBudget(GoodCandidate):
    name = "candidate-ignores-budget"

    def _retrieve(self, query, purpose, budget_tokens):
        return super()._retrieve(query, purpose, 10**9)


def build(candidate_cls) -> list[dict]:
    recs = []
    for bb in BBS:
        makers = [lambda: candidate_cls(HashingEmbedder())]
        makers += [lambda s=s: runner.SUBSTRATES[s](FX) for s in ("rag", "random", "full", "amnesiac")]
        recs += [runner.run_one(make, bb, FX, gate=True) for make in makers]
    return recs


class _Base(unittest.TestCase):
    good: list[dict]

    @classmethod
    def setUpClass(cls) -> None:
        cls.good = build(GoodCandidate)

    def edit(self, substrate: str, family: str, track: str, metric: str, value: float) -> list[dict]:
        recs = copy.deepcopy(self.good)
        hit = [r for r in recs if r["substrate"]["name"] == substrate and r["backbone"]["family"] == family]
        self.assertEqual(len(hit), 1)
        hit[0]["tracks"][track]["metrics"][metric] = value
        return recs

    def crit(self, v, name):
        return next(c for c in v.criteria if c.name == name)


class TestGatePasses(_Base):
    def test_a_good_candidate_passes_and_every_criterion_is_listed_with_numbers(self) -> None:
        v = gate(1, self.good)
        self.assertTrue(v.passed, v.render())
        names = [c.name for c in v.criteria]
        for want in ("records-valid", "families>=2", "controls-present", "A1-beats-random", "A-retrieve-beats-random",
                     "E3-beats-random", "A1-vs-rag", "A-retrieve-vs-rag", "E3-vs-rag",
                     "A3-leak-at-or-under-floor", "A3-no-collapse", "E3-budget-respected"):
            self.assertIn(want, names)
        self.assertIn(" vs ", self.crit(v, "A1-beats-random").detail)  # the numbers, not a bare boolean
        self.assertIn("PASS", v.render())

    def test_only_tiers_1_and_2_exist_and_tier_2_refuses_v1_records(self) -> None:
        with self.assertRaises(NotImplementedError):
            gate(3, self.good)
        self.assertEqual(gate(2, self.good).failed, ("fixture-version-for-tier",))  # tier 2 gates v4 records only


class TestGateCanFail(_Base):
    """One test per criterion, driven by a real broken substrate where one exists."""

    def failed(self, recs) -> set[str]:
        return set(gate(1, recs).failed)

    def test_retrieve_returns_nothing(self) -> None:
        f = self.failed(build(ReturnsNothing))
        for want in ("A1-beats-random", "A-retrieve-beats-random", "E3-beats-random",
                     "A1-vs-rag", "A-retrieve-vs-rag", "E3-vs-rag", "A3-no-collapse"):
            self.assertIn(want, f)
        self.assertNotIn("A3-leak-at-or-under-floor", f)  # a mute store leaks nothing; collapse is what catches it

    def test_ignores_forget_fails_exactly_the_a3_leak_criterion(self) -> None:
        self.assertEqual(self.failed(build(IgnoresForget)), {"A3-leak-at-or-under-floor"})

    def test_ignores_budget_fails_the_e3_budget_criterion(self) -> None:
        self.assertIn("E3-budget-respected", self.failed(build(IgnoresBudget)))

    def test_a1_beats_random(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "A1", "accuracy", 0.1)
        self.assertIn("A1-beats-random", self.failed(recs))

    def test_a_retrieve_beats_random(self) -> None:
        recs = self.edit("candidate-good", "stub-lenient", "A-retrieve", "recall", 0.0)
        self.assertIn("A-retrieve-beats-random", self.failed(recs))

    def test_e3_beats_random(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "E3", "utility", 0.0)
        self.assertIn("E3-beats-random", self.failed(recs))

    def test_a1_vs_rag_uses_the_tolerance_constant(self) -> None:
        rag = next(r for r in self.good if r["substrate"]["name"] == "verbatim-rag" and r["backbone"]["family"] == "stub-strict")
        rag_a1 = rag["tracks"]["A1"]["metrics"]["accuracy"]
        edge = self.edit("candidate-good", "stub-strict", "A1", "accuracy", round(rag_a1 - gatem.RAG_TOLERANCE, 6))
        self.assertNotIn("A1-vs-rag", self.failed(edge))
        under = self.edit("candidate-good", "stub-strict", "A1", "accuracy", round(rag_a1 - gatem.RAG_TOLERANCE - 0.01, 6))
        self.assertIn("A1-vs-rag", self.failed(under))

    def test_a_retrieve_vs_rag(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "A-retrieve", "recall", 0.3)
        self.assertIn("A-retrieve-vs-rag", self.failed(recs))

    def test_e3_vs_rag(self) -> None:
        recs = self.edit("candidate-good", "stub-lenient", "E3", "utility", 0.2)
        self.assertIn("E3-vs-rag", self.failed(recs))

    def test_a3_leak_must_sit_at_or_under_the_amnesiac_floor(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "A3", "forgotten_leak_rate", 0.25)
        self.assertEqual(self.failed(recs), {"A3-leak-at-or-under-floor"})

    def test_a3_collapse_to_the_floor_fails(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "A3", "retained_recall", 0.0)
        self.assertEqual(self.failed(recs), {"A3-no-collapse"})

    def test_e3_budget_violation(self) -> None:
        recs = self.edit("candidate-good", "stub-strict", "E3", "budget_violation_rate", 0.1)
        self.assertEqual(self.failed(recs), {"E3-budget-respected"})

    def test_one_family_fails_the_families_criterion(self) -> None:
        recs = [r for r in self.good if r["backbone"]["family"] == "stub-strict"]
        self.assertIn("families>=2", self.failed(recs))

    def test_a_missing_control_fails_controls_present(self) -> None:
        for role in ("control-random", "baseline-rag", "floor-amnesiac", "ceiling-full-context"):
            with self.subTest(role=role):
                recs = [r for r in self.good if r["substrate"]["role"] != role]
                self.assertIn("controls-present", self.failed(recs))

    def test_ceiling_gap_is_reported_with_numbers_and_is_informational(self) -> None:
        c = self.crit(gate(1, self.good), "ceiling-gap")
        self.assertFalse(c.required)
        self.assertIn("full-context", c.detail)
        row = c.numbers["stub-strict"]["E3.mean_tokens"]
        self.assertLess(row["candidate"], row["full_context"])
        self.assertIn("ceiling-gap", [d["name"] for d in gate(1, self.good).to_dict()["criteria"]])

    def test_a_record_without_cost_fails_records_valid(self) -> None:
        recs = copy.deepcopy(self.good)
        del recs[0]["cost"]
        v = gate(1, recs)
        self.assertEqual(v.failed, ("records-valid",))
        self.assertIn("cost", v.criteria[0].detail)

    def test_mixed_fixtures_fail_same_fixture(self) -> None:
        recs = copy.deepcopy(self.good)
        recs[0]["fixture"]["sha256"] = "0" * 64
        self.assertIn("same-fixture", self.failed(recs))

    def test_a_non_gate_run_fails_records_are_gate_runs(self) -> None:
        recs = copy.deepcopy(self.good)
        recs[0]["gate"] = False
        self.assertIn("records-are-gate-runs", self.failed(recs))

    def test_empty_records_fail(self) -> None:
        self.assertEqual(gate(1, []).failed, ("records-valid",))

    def test_rank_stability_is_reported_but_is_not_a_pass_criterion(self) -> None:
        c = self.crit(gate(1, self.good), "rank-order-stable")
        self.assertFalse(c.required)


class TestEvidenceType(unittest.TestCase):
    def test_evidence_carries_a_backlink_ref(self) -> None:
        e = Evidence("text", "r1")
        self.assertEqual((e.text, e.ref), ("text", "r1"))


if __name__ == "__main__":
    unittest.main()
