#!/usr/bin/env python3
"""``SemanticTierSubstrate`` (T-2518 S4) in the PMB-1 harness: registration, the R6 declaration, the tier-2 tracks on
one tiny DEV seed through the REAL runner and tracks, and (``PMB1_V4_FULL=1``) the real ``gate(2)`` on v4 tiny DEV
seeds 1-3 with every track run for real. Seeds 28-36 are reserved for the real run and never generated here.
Synthetic data only; offline guard on."""
from __future__ import annotations

import os
import unittest

from tools.pmb1 import fixtures_v4 as fx4
from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.episodic import EpisodicTierSubstrate
from tools.pmb1.gate import gate
from tools.pmb1.semantic import SemanticTierSubstrate

FULL = os.environ.get("PMB1_V4_FULL") == "1"
DEV_SEEDS = (1, 2, 3)
T2_TRACKS = ("A2c", "A9", "C5", "GOV-derived")
_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class _FakeEmbedder:
    name = "fake-embedder"

    def embed_query(self, text):
        return [1.0, float(len(text) % 3)]

    def embed_document(self, text):
        return [1.0, float(len(text) % 3)]


class TestWiring(unittest.TestCase):
    def test_registered_as_an_embedding_substrate_over_episodic(self) -> None:
        self.assertIn("semantic", runner.SUBSTRATES)
        self.assertIn("semantic", runner.EMBEDDING_SUBSTRATES)
        f = fx4.generate_v4(1, "tiny")
        sub = runner.make_substrate("semantic", f)
        try:
            self.assertIsInstance(sub, SemanticTierSubstrate)
            self.assertIsInstance(sub, EpisodicTierSubstrate)
            self.assertEqual(sub.privacy_ceiling, f.privacy_ceiling)
            d = sub.describe()
            self.assertEqual((d["name"], d["over"], d["role"]), ("semantic-tier", "episodic", "candidate"))
            self.assertEqual(d["extractor"], "pattern-stub:possessive-copula")
            self.assertIs(d["extractor_is_pattern_stub"], True)
            self.assertEqual(sub.template_words, ())
            self.assertIs(sub.lineage_store(), sub.store)  # inherited: C5 checks against the store itself
        finally:
            sub.close()
        e = _FakeEmbedder()
        sub = runner.make_substrate("semantic", f, e)
        try:
            self.assertIs(sub.embedder, e)
            self.assertEqual(sub.embedding_model, "fake-embedder")
        finally:
            sub.close()

    def test_a_v4_gate_run_refuses_the_undeclared_pattern_stub(self) -> None:
        f = fx4.generate_v4(1, "tiny")
        with self.assertRaises(runner.GateRunRefused):
            runner.run(["semantic"], [StubBackbone("strict"), StubBackbone("lenient")], f, gate=True,
                       embedder_is_lexical_stub=True, extractor_is_pattern_stub=False)


def _record(name: str, seed: int, fam: str, tracks=None) -> dict:
    f = fx4.generate_v4(seed, "tiny")
    return runner.run_one(lambda: runner.SUBSTRATES[name](f), StubBackbone(fam), f, gate=True, tracks=tracks,
                          embedder_is_lexical_stub=True, extractor_is_pattern_stub=True)


class TestTier2TracksSeed1(unittest.TestCase):
    """The tier-2 tracks, real runner, DEV seed 1, strict reader. Expectations are the tier's DEFINITIONS (no stale
    value asserted as current, no false conflict, lineage on every unit, no derived leak, every word sourced), each
    paired with the episodic tier's number on the same probes so the probes are shown able to fail."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sem = _record("semantic", 1, "strict", T2_TRACKS)
        cls.epi = _record("episodic", 1, "strict", ("A2c", "A9"))

    def m(self, rec: dict, track: str) -> dict:
        return rec["tracks"][track]["metrics"]

    def test_record_is_valid_and_declares_the_stub(self) -> None:
        # (a partial-track record is not a valid full record; records-valid is the FULL gate run's criterion)
        self.assertIs(self.sem["extractor_is_pattern_stub"], True)
        self.assertEqual(self.sem["substrate"]["extractor"], "pattern-stub:possessive-copula")
        self.assertEqual(self.sem["template_words"], [])

    def test_a2c(self) -> None:
        self.assertGreater(self.m(self.epi, "A2c")["stale_as_current_rate"], 0.0)
        self.assertEqual(self.m(self.sem, "A2c")["stale_as_current_rate"], 0.0)
        self.assertGreater(self.m(self.sem, "A2c")["asof_accuracy"], self.m(self.epi, "A2c")["asof_accuracy"])
        self.assertGreater(self.m(self.sem, "A2c")["current_accuracy"], self.m(self.epi, "A2c")["current_accuracy"])

    def test_a9(self) -> None:
        self.assertGreater(self.m(self.epi, "A9")["false_conflict_change"], 0.0)
        self.assertEqual(self.m(self.sem, "A9")["false_conflict_rate"], 0.0)
        self.assertGreater(self.m(self.sem, "A9")["surfaced_rate"], self.m(self.epi, "A9")["surfaced_rate"])

    def test_c5_and_gov_derived(self) -> None:
        c5 = self.m(self.sem, "C5")
        self.assertEqual(c5["lineage_rate"], 1.0)
        self.assertGreater(c5["n_units"], 0)
        self.assertEqual(c5["n_store_checked"], c5["n_units"])
        g = self.m(self.sem, "GOV-derived")
        self.assertEqual((g["governance_leak_rate"], g["provenance_failure_rate"]), (0.0, 0.0))


@unittest.skipUnless(FULL, "PMB1_V4_FULL=1: the tier-2 gate on v4 tiny DEV seeds 1-3, every track real")
class TestTier2GateAllReal(unittest.TestCase):
    def test_gate2_tiny_dev_seeds(self) -> None:
        names = ("semantic", "episodic", "rag", "random", "full", "amnesiac")
        recs = [_record(n, s, fam) for n in names for s in DEV_SEEDS for fam in ("strict", "lenient")]
        v = gate(2, recs)
        self.assertEqual((v.tier, v.candidate), (2, "semantic-tier"))
        self.assertEqual(v.failed, (), v.render())
        self.assertTrue(any(n.startswith("R6:") and "semantic-tier" in n for n in v.notes))


if __name__ == "__main__":
    unittest.main()
