#!/usr/bin/env python3
"""PMB-1 controls, tracks and run records (T-2518). In-memory substrates only:
no store, no network, no Ollama, no GPU."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from tools.pmb1 import backbone as bbm
from tools.pmb1 import fixtures as fxm
from tools.pmb1 import offline, records, runner, tracks
from tools.pmb1.backbone import ABSTAIN, StubBackbone
from tools.pmb1.embedder import HashingEmbedder
from tools.pmb1.substrate import (
    AmnesiacFloor, FullContextCeiling, Item, RandomRetrievalControl, VerbatimRAGBaseline,
)

FX = fxm.generate(1, "small")
STRICT, LENIENT = StubBackbone("strict"), StubBackbone("lenient")

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()



def run(factory, bb=STRICT, **kw) -> dict:
    return runner.run_one(factory, bb, FX, gate=False, **kw)


class _ControlRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rag = run(lambda: VerbatimRAGBaseline(HashingEmbedder()))
        cls.rand = run(lambda: RandomRetrievalControl(seed=1))
        cls.full = run(lambda: FullContextCeiling())
        cls.amn = run(lambda: AmnesiacFloor())
        cls.rag_lenient = run(lambda: VerbatimRAGBaseline(HashingEmbedder()), LENIENT)

    @staticmethod
    def m(rec: dict, track: str, metric: str) -> float:
        return rec["tracks"][track]["metrics"][metric]


class TestControlsBehaveAsControls(_ControlRuns):
    def test_random_is_below_rag_on_a_retrieve_with_the_hashing_embedder(self) -> None:
        self.assertLess(self.m(self.rand, "A-retrieve", "recall"), self.m(self.rag, "A-retrieve", "recall"))
        self.assertEqual(self.rag["embedding_model"], HashingEmbedder().name)

    def test_amnesiac_has_zero_recall_and_passes_a3_trivially(self) -> None:
        self.assertEqual(self.m(self.amn, "A-retrieve", "recall"), 0.0)
        self.assertEqual(self.m(self.amn, "A1", "accuracy"), 0.0)
        self.assertEqual(self.m(self.amn, "A3", "forgotten_leak_rate"), 0.0)
        self.assertEqual(self.m(self.amn, "A3", "retained_recall"), 0.0)  # ...which is exactly why collapse is checked

    def test_full_context_uses_the_most_tokens_on_e3_and_ignores_the_budget(self) -> None:
        toks = {r["substrate"]["name"]: self.m(r, "E3", "mean_tokens") for r in (self.rag, self.rand, self.full, self.amn)}
        self.assertEqual(max(toks, key=toks.get), "full-context-ceiling")
        self.assertEqual(self.m(self.full, "E3", "mean_tokens"), self.m(self.full, "E3", "corpus_tokens"))
        self.assertGreater(self.m(self.full, "E3", "budget_violation_rate"), 0.0)
        self.assertEqual(self.m(self.full, "E3", "utility"), 0.0)  # a dump scores worse than a small view
        self.assertGreater(self.m(self.rag, "E3", "utility"), self.m(self.full, "E3", "utility"))

    def test_rag_respects_the_budget(self) -> None:
        self.assertEqual(self.m(self.rag, "E3", "budget_violation_rate"), 0.0)

    def test_lenient_reader_leaks_where_the_strict_reader_does_not(self) -> None:
        self.assertEqual(self.m(self.rag, "A7", "leak_rate"), 0.0)
        self.assertGreater(self.m(self.rag_lenient, "A7", "leak_rate"), 0.0)
        self.assertEqual(self.m(self.rag, "E4", "abstain_rate"), 1.0)
        self.assertLess(self.m(self.rag_lenient, "E4", "abstain_rate"), 1.0)

    def test_a2_scores_new_value_and_stale_returns_separately(self) -> None:
        m = self.rag["tracks"]["A2"]["metrics"]
        self.assertEqual(set(m), {"new_value_accuracy", "stale_evidence_rate", "stale_answer_rate"})
        self.assertEqual(m["new_value_accuracy"], 1.0)
        self.assertEqual(m["stale_evidence_rate"], 0.0)

    def test_a2_counts_a_substrate_that_keeps_the_past(self) -> None:
        class KeepsThePast(VerbatimRAGBaseline):
            def _supersede(self, ref, new_item):  # adds the new fact, never retires the old
                return self._write(new_item)
        rec = run(lambda: KeepsThePast(HashingEmbedder()))
        self.assertGreater(rec["tracks"]["A2"]["metrics"]["stale_evidence_rate"], 0.0)

    def test_a3_counts_a_substrate_that_ignores_forget(self) -> None:
        class IgnoresForget(VerbatimRAGBaseline):
            def _forget(self, ref):
                return None
        rec = run(lambda: IgnoresForget(HashingEmbedder()))
        self.assertGreater(rec["tracks"]["A3"]["metrics"]["forgotten_leak_rate"], 0.0)

    def test_a_retrieve_never_calls_the_backbone(self) -> None:
        bb = StubBackbone("strict")
        res = tracks.track_a_retrieve(VerbatimRAGBaseline(HashingEmbedder()), bb, FX)
        self.assertEqual(bb.calls, 0)
        self.assertEqual(res.name, "A-retrieve")


class TestStubsAndRecords(_ControlRuns):
    def test_unimplemented_tracks_raise_and_report_not_run(self) -> None:
        for name, stub in tracks.STUBS.items():
            with self.subTest(track=name), self.assertRaises(NotImplementedError):
                stub(AmnesiacFloor(), STRICT, FX)
        self.assertEqual(set(self.rag["not_run"]), set(tracks.STUB_TRACKS))
        self.assertFalse(set(self.rag["not_run"]) & set(self.rag["tracks"]))  # never reported as passed

    def test_a_record_validates_and_carries_the_required_fields(self) -> None:
        self.assertEqual(records.validate_record(self.rag), [])
        self.assertEqual(self.rag["schema"], "pmb1-run/1")
        self.assertEqual(self.rag["fixture"]["sha256"], FX.sha256)
        self.assertEqual(self.rag["backbone"], {"name": "stub", "family": "stub-strict"})
        for k in records.RUN_COST_FIELDS:
            self.assertIn(k, self.rag["cost"])
        self.assertEqual(self.rag["cost"]["write_llm_calls"], 0)
        self.assertGreater(self.rag["cost"]["backbone_calls"], 0)
        for t in self.rag["tracks"].values():
            self.assertGreater(t["n"], 0)

    def test_a_record_without_cost_fields_fails_validation(self) -> None:
        bad = copy.deepcopy(self.rag)
        del bad["cost"]
        self.assertTrue(any("cost" in e for e in records.validate_record(bad)))
        for field in records.RUN_COST_FIELDS:
            bad = copy.deepcopy(self.rag)
            del bad["cost"][field]
            self.assertTrue(any(field in e for e in records.validate_record(bad)), field)

    def test_other_malformed_records_fail_validation(self) -> None:
        for mutate in (
            lambda r: r.__setitem__("schema", "pmb1-run/0"),
            lambda r: r["backbone"].pop("family"),
            lambda r: r["fixture"].__setitem__("sha256", "x"),
            lambda r: r.__setitem__("tracks", {}),
            lambda r: r.pop("embedding_model"),
            lambda r: r.pop("not_run"),
        ):
            bad = copy.deepcopy(self.rag)
            mutate(bad)
            self.assertTrue(records.validate_record(bad))
        self.assertTrue(records.validate_record("not a record"))


class TestRunnerFamilies(unittest.TestCase):
    def test_gate_run_with_one_family_is_refused(self) -> None:
        with self.assertRaises(runner.GateRunRefused):
            runner.run(["rag"], [StubBackbone("strict")], fxm.generate(1, "tiny"), gate=True)
        with self.assertRaises(runner.GateRunRefused):  # two backbones, ONE family, still refused
            runner.run(["rag"], [StubBackbone("strict"), StubBackbone("strict")], fxm.generate(1, "tiny"), gate=True)

    def test_non_gate_single_family_run_is_allowed_and_reports_rank_stability_na(self) -> None:
        recs, stab = runner.run(["rag", "random"], [StubBackbone("strict")], fxm.generate(1, "tiny"), gate=False)
        self.assertEqual(len(recs), 2)
        self.assertIsNone(stab["stable"])

    def test_two_families_run_and_rank_order_is_reported(self) -> None:
        recs, stab = runner.run(
            ["rag", "random", "amnesiac"], [StubBackbone("strict"), StubBackbone("lenient")],
            fxm.generate(1, "tiny"), gate=True,
        )
        self.assertTrue(all(r["gate"] for r in recs))
        self.assertEqual(set(stab["families"]), {"stub-strict", "stub-lenient"})
        self.assertIsInstance(stab["stable"], bool)

    def test_unknown_substrate_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            runner.run(["nope"], [StubBackbone()], fxm.generate(1, "tiny"))


class TestBackbones(unittest.TestCase):
    def test_stub_is_deterministic_and_abstains_without_evidence(self) -> None:
        q = "What is Kabuto's beverage?"
        ev = ["Zed's pet is bbbbbb.", "Kabuto's beverage is aaaaaa."]
        self.assertEqual(StubBackbone().generate(q, ev), "aaaaaa")
        self.assertEqual(StubBackbone().generate(q, []), ABSTAIN)
        self.assertEqual(StubBackbone().generate(q, ["Zed's pet is bbbbbb."]), ABSTAIN)
        self.assertEqual(StubBackbone("lenient").generate(q, ["Zed's pet is bbbbbb."]), "bbbbbb")

    def test_stub_families_differ(self) -> None:
        self.assertNotEqual(StubBackbone("strict").family, StubBackbone("lenient").family)


if __name__ == "__main__":
    unittest.main()
