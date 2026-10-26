#!/usr/bin/env python3
"""PMB-1 fixture v2 DISCRIMINATES (T-2518 step 2a): on v1 governed = RAG = 1.0.

Asserts the ORDERING on medium x 3 seeds, never exact values. Real substrates
and the real governed store; synthetic data; the offline guard is on.
"""
from __future__ import annotations

import unittest

from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.fixtures_v2 import generate_v2
from tools.pmb1.governed import GovernedStoreSubstrate
from tools.pmb1.substrate import AmnesiacFloor, Evidence, VerbatimRAGBaseline
from tools.pmb1.tracks_v2 import relevant_units, track_a1, track_a3, track_a_retrieve

_GUARD = None
TRACKS = ("A1", "A-retrieve", "E3")


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


def score(name: str, fx) -> dict:
    rec = runner.run_one(lambda: runner.SUBSTRATES[name](fx), StubBackbone("strict"), fx, gate=False, tracks=TRACKS)
    return {t: rec["tracks"][t]["metrics"] for t in TRACKS}


class _Medium:
    seed = 0

    def test_does_not_saturate_and_orders_the_controls(self) -> None:
        fx = generate_v2(self.seed, "medium")
        rnd, rag, full = (score(n, fx) for n in ("random", "rag", "full"))
        a1 = lambda s: s["A1"]["accuracy"]  # noqa: E731
        self.assertLess(a1(rnd), a1(rag))
        self.assertLess(a1(rag), a1(full))
        self.assertLess(a1(rag), 1.0)
        # the full-context dump pays the whole corpus: no utility, and it breaks every budget
        self.assertEqual(full["E3"]["utility"], 0.0)
        self.assertEqual(full["E3"]["budget_violation_rate"], 1.0)
        self.assertGreater(rag["E3"]["utility"], rnd["E3"]["utility"])
        # governed (real store, A1 only: its full run is minutes) is below the ceiling too
        sub = GovernedStoreSubstrate()
        try:
            gov = track_a1(sub, StubBackbone("strict"), fx).metrics["accuracy"]
        finally:
            sub.close()
        self.assertLess(gov, 1.0)
        self.assertGreater(gov, a1(rnd))


class TestMediumSeed1(_Medium, unittest.TestCase):
    seed = 1


class TestMediumSeed2(_Medium, unittest.TestCase):
    seed = 2


class TestMediumSeed3(_Medium, unittest.TestCase):
    seed = 3


class TestParaphraseAndTimelineAreMeasured(unittest.TestCase):
    def test_a1_and_a_retrieve_report_the_paraphrase_and_timeline_breakdowns(self) -> None:
        fx = generate_v2(1, "small")
        sub = VerbatimRAGBaseline()
        a1 = track_a1(sub, StubBackbone("strict"), fx).metrics
        ar = track_a_retrieve(VerbatimRAGBaseline(), StubBackbone("strict"), fx).metrics
        for k in ("accuracy_canonical", "accuracy_paraphrase"):
            self.assertIn(k, a1)
        for k in ("recall_first", "recall_middle", "recall_recent", "recall_canonical", "recall_paraphrase"):
            self.assertIn(k, ar)
        # dropping the attribute word hurts a lexical retriever: paraphrase is the harder half
        self.assertLess(a1["accuracy_paraphrase"], a1["accuracy_canonical"])


class TestPrecisionWithSubItemEvidence(unittest.TestCase):
    def test_a_unit_counts_only_if_its_backlink_is_gold_and_it_holds_the_gold_value(self) -> None:
        fx = generate_v2(1, "tiny")
        p = fx.probes[0]
        gold = "m-1/v-1"
        text = dict((k, t) for k, _, t in fx.episodes)[p.episode]
        units = [
            Evidence(p.fact_sentence, gold),          # gold backlink + value: relevant
            Evidence("Nobody could find it.", gold),  # gold backlink, no value: not relevant
            Evidence(p.fact_sentence, "m-2/v-1"),     # the value, wrong source: not relevant
            Evidence(text, gold),                     # the whole gold episode holds the value: relevant
        ]
        self.assertEqual(relevant_units(p, units, gold), 2)
        self.assertEqual(relevant_units(p, [], gold), 0)

    def test_precision_is_relevant_units_over_returned_units(self) -> None:
        fx = generate_v2(1, "small")
        m = track_a_retrieve(VerbatimRAGBaseline(), StubBackbone("strict"), fx).metrics
        self.assertGreater(m["precision"], 0.0)
        self.assertLessEqual(m["precision"], m["recall"])  # a relevant unit needs a gold backlink, so precision <= recall


class TestForgetOnV2(unittest.TestCase):
    """An A3 run on v2 exercises forget: the forget probes' episodes are forgotten and queried."""

    def test_governed_leaks_no_more_than_the_amnesiac_floor_and_still_remembers(self) -> None:
        fx = generate_v2(1, "tiny")
        sub = GovernedStoreSubstrate()
        try:
            gov = track_a3(sub, StubBackbone("strict"), fx)
            floor = track_a3(AmnesiacFloor(), StubBackbone("strict"), fx)
            outcomes = dict(sub.outcomes)
        finally:
            sub.close()
        self.assertEqual(gov.metrics["n_forgotten"], float(len(fx.forgets)))
        self.assertLessEqual(gov.metrics["forgotten_leak_rate"], floor.metrics["forgotten_leak_rate"])
        self.assertGreater(gov.metrics["retained_recall"], floor.metrics["retained_recall"])
        self.assertEqual(sum(v for k, v in outcomes.items() if k.startswith("forget:")), len(fx.forgets))

    def test_a_forget_ignoring_substrate_leaks(self) -> None:
        class IgnoresForget(VerbatimRAGBaseline):
            def _forget(self, ref):
                return None

        fx = generate_v2(1, "small")
        floor = track_a3(AmnesiacFloor(), StubBackbone("strict"), fx).metrics["forgotten_leak_rate"]
        honest = track_a3(VerbatimRAGBaseline(), StubBackbone("strict"), fx).metrics["forgotten_leak_rate"]
        leaky = track_a3(IgnoresForget(), StubBackbone("strict"), fx).metrics["forgotten_leak_rate"]
        self.assertEqual((floor, honest), (0.0, 0.0))
        self.assertGreater(leaky, floor)


if __name__ == "__main__":
    unittest.main()
