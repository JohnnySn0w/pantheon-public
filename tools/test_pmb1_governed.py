#!/usr/bin/env python3
"""GovernedStoreSubstrate (T-2518): the ORG-058 store as the PMB-1 baseline.

Real store, real FTS5 projections, real ``correct`` / ``forget`` /
``evaluate_retrieval`` over a temp dir. Synthetic data only; no network."""
from __future__ import annotations

import os
import sys
import unittest

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import offline
from tools.pmb1 import runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.gate import gate
from tools.pmb1.governed import GovernedStoreSubstrate
from tools.pmb1.substrate import Item

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_policy_evaluator as ev  # noqa: E402
import test_memory_store as fxs  # noqa: E402

FX = fxm.generate(1, "tiny")

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()

Q = "What is Kabuto's beverage?"


def item(key: str, subject: str, text: str) -> Item:
    return Item(key, subject, text)


class TestGovernedAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.g = GovernedStoreSubstrate()
        self.addCleanup(self.g.close)

    def test_write_then_retrieve_returns_text_with_a_backlink_ref(self) -> None:
        ref = self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        self.g.write(item("b", "zed", "Zed's pet is bbbbbb."))
        got = self.g.retrieve(Q, "companionship", 60)
        self.assertEqual([(e.text, e.ref) for e in got], [("Kabuto's beverage is aaaaaa.", ref)])

    def test_supersede_goes_through_correct_and_the_old_version_is_not_returned(self) -> None:
        old = self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        new = self.g.supersede(old, item("a2", "kabuto", "Kabuto's beverage is cccccc."))
        self.assertNotEqual(old, new)
        got = self.g.retrieve(Q, "companionship", 60)
        self.assertEqual([e.ref for e in got], [new])
        self.assertIn("cccccc", got[0].text)
        self.assertEqual(self.g.describe()["lifecycle_outcomes"].keys(), {"correct:incomplete"})

    def test_forget_removes_the_item_from_every_retrieval_path(self) -> None:
        ref = self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        self.g.write(item("b", "zed", "Zed's pet is bbbbbb."))
        self.assertTrue(self.g.retrieve(Q, "companionship", 60))
        self.g.forget(ref)
        self.assertEqual(self.g.retrieve(Q, "companionship", 60), [])
        self.assertEqual(self.g.store.lookup_content("aaaaaa"), ())
        self.assertTrue(self.g.retrieve("What is Zed's pet?", "companionship", 60))  # others untouched

    def test_content_token_budget_is_applied_to_real_text(self) -> None:
        for i, name in enumerate(("kabuto", "kabuta", "kabutu")):
            self.g.write(item(f"k{i}", name, f"{name.capitalize()}'s beverage is xxxxx{i}."))
        full = self.g.retrieve("beverage", "companionship", 1000)
        one = self.g.retrieve("beverage", "companionship", 4)
        self.assertEqual(len(full), 3)
        self.assertEqual(len(one), 1)
        self.assertEqual(self.g.retrieve("beverage", "companionship", 3), [])

    def test_unknown_purpose_returns_nothing(self) -> None:
        self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        self.assertTrue(self.g.retrieve(Q, "companionship", 60))
        self.assertEqual(self.g.retrieve(Q, "no-such-purpose", 60), [])

    def test_item_above_the_privacy_ceiling_never_comes_back(self) -> None:
        g = GovernedStoreSubstrate(privacy_ceiling="internal")
        self.addCleanup(g.close)
        g.write(Item("a", "kabuto", "Kabuto's beverage is aaaaaa.", privacy_class="personal"))
        g.write(Item("b", "kabuto", "Kabuto's snack is bbbbbb.", privacy_class="internal"))
        got = g.retrieve("Kabuto's beverage snack", "companionship", 1000)
        self.assertEqual([e.text for e in got], ["Kabuto's snack is bbbbbb."])
        self.assertFalse([e for e in g.retrieve(Q, "companionship", 1000) if "aaaaaa" in e.text])
        wide = GovernedStoreSubstrate()  # same writes, default ceiling "personal": returned
        self.addCleanup(wide.close)
        wide.write(Item("a", "kabuto", "Kabuto's beverage is aaaaaa.", privacy_class="personal"))
        self.assertTrue([e for e in wide.retrieve(Q, "companionship", 1000) if "aaaaaa" in e.text])

    def test_use_disposition_travels_on_the_evidence(self) -> None:
        self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        self.assertEqual(self.g.retrieve(Q, "companionship", 60)[0].disposition, "allowed")
        # "assertion" needs a floor above the item's trust: returned, but QUARANTINED
        self.assertEqual(self.g.retrieve(Q, "assertion", 60)[0].disposition, "quarantined")

    def test_describe_reports_cost_counters(self) -> None:
        self.g.write(item("a", "kabuto", "Kabuto's beverage is aaaaaa."))
        self.g.retrieve(Q, "companionship", 60)
        d = self.g.describe()
        self.assertEqual((d["name"], d["role"]), ("governed-store", "candidate"))
        self.assertEqual(d["cost"]["write_calls"], 1)
        self.assertEqual(d["cost"]["retrieve_calls"], 1)
        self.assertEqual(d["cost"]["write_llm_calls"], 0)
        self.assertEqual(d["cost"]["write_dollars"], 0.0)
        self.assertGreater(d["cost"]["write_seconds"], 0)


class TestEvaluatorFindings(unittest.TestCase):
    """Observed behaviour of ``evaluate_retrieval`` that shapes the adapter. If
    the evaluator changes, these fail loudly and the adapter must be revisited."""

    def setUp(self) -> None:
        self.trust = fxs.trust_policy()

    def envs(self, ids):
        return [fxs.make_env(i, "v-1", "x", subjects=("s",)) for i in ids]

    def test_ranking_is_alphabetical_not_relevance(self) -> None:
        req = fxs.retrieval_request(subject_scope=(), item_budget=0, token_budget=0)
        view = ev.evaluate_retrieval(req, self.envs(["m-2", "m-10", "m-1"]), self.trust)
        self.assertEqual([i.memory_id for i in view.items], ["m-1", "m-10", "m-2"])

    def test_token_budget_meters_payload_ref_length_not_content(self) -> None:
        req = fxs.retrieval_request(subject_scope=(), item_budget=0, token_budget=0)
        view = ev.evaluate_retrieval(req, self.envs(["m-1", "m-2"]), self.trust)
        self.assertEqual(view.token_count, sum(len(i.bounded_content) for i in view.items))
        self.assertTrue(all(i.bounded_content.startswith("payload://") for i in view.items))

    def test_privacy_ceiling_denials_are_aggregate_only(self) -> None:
        req = fxs.retrieval_request(subject_scope=(), privacy_ceiling="internal")
        e = fxs.make_env("m-9", "v-1", "x", privacy_class="personal", subjects=("s",))
        view = ev.evaluate_retrieval(req, [e], self.trust)
        self.assertEqual((len(view.items), view.denied_count), (0, 1))


class TestGovernedAgainstTheFloorAndTheGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bbs = [StubBackbone("strict"), StubBackbone("lenient")]
        cls.recs, cls.stab = runner.run(
            ["governed", "rag", "random", "full", "amnesiac"], cls.bbs, FX, gate=True,
        )

    def rec(self, name: str, family: str = "stub-strict") -> dict:
        return next(r for r in self.recs if r["substrate"]["name"] == name and r["backbone"]["family"] == family)

    def test_a3_forget_really_removes_it_versus_the_amnesiac_floor(self) -> None:
        for fam in ("stub-strict", "stub-lenient"):
            g, floor = self.rec("governed-store", fam)["tracks"]["A3"]["metrics"], self.rec("amnesiac-floor", fam)["tracks"]["A3"]["metrics"]
            self.assertLessEqual(g["forgotten_leak_rate"], floor["forgotten_leak_rate"])
            self.assertEqual(g["forgotten_leak_rate"], 0.0)
            self.assertGreater(g["retained_recall"], floor["retained_recall"])  # not a collapse

    def test_e3_respects_the_budget_and_beats_the_dump(self) -> None:
        g = self.rec("governed-store")["tracks"]["E3"]["metrics"]
        full = self.rec("full-context-ceiling")["tracks"]["E3"]["metrics"]
        self.assertEqual(g["budget_violation_rate"], 0.0)
        self.assertLess(g["mean_tokens"], full["mean_tokens"])
        self.assertGreater(g["utility"], full["utility"])

    def test_a2_supersession_leaves_no_stale_returns(self) -> None:
        m = self.rec("governed-store")["tracks"]["A2"]["metrics"]
        self.assertEqual((m["new_value_accuracy"], m["stale_evidence_rate"], m["stale_answer_rate"]), (1.0, 0.0, 0.0))

    def test_records_carry_lifecycle_outcomes_and_cost(self) -> None:
        r = self.rec("governed-store")
        self.assertTrue(r["substrate"]["lifecycle_outcomes"])
        self.assertGreater(r["cost"]["write_seconds"], 0)

    def test_tier_1_gate_over_the_stub_run(self) -> None:
        v = gate(1, self.recs)
        self.assertTrue(v.passed, v.render())
        self.assertEqual(v.candidate, "governed-store")


if __name__ == "__main__":
    unittest.main()
