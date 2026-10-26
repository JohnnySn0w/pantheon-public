#!/usr/bin/env python3
"""PMB-1 wiring of the episodic tier's semantic leg (T-2518 semleg), offline.

* LOCKED point 1: with no embedder the tier's records are byte-identical to the golden generated at the BASE
  commit before any code change (``tools/testdata/pmb1_episodic_lexical_golden.json``, v3 small seeds 10 + 11,
  strict + lenient stubs).
* LOCKED point 6: ``"episodic"`` is an embedding substrate; what it embeds does not depend on the backbone's
  replies (two differently-answering backbones, seeds 1-18) nor on the inner embedder's ranking; a precompute
  covers a replay-only run with zero misses; a miss fails closed; realrun precomputes and replays it; the records
  carry ``embedding_model``.
No network, no ssh, no Ollama: the "real" embedder is ``FakeEmbedder`` (hashing-backed, named like nomic)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.pmb1 import cli, offline, realrun as rr
from tools.pmb1 import embedder as em
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.embedphase import enumerate_texts, precompute
from tools.pmb1.episodic import EpisodicTierSubstrate
from tools.pmb1.fixtures_v3 import generate_by_version
from tools.pmb1.runner import EMBEDDING_SUBSTRATES, SUBSTRATES, make_substrate, run_one
from tools.test_pmb1_embedder import NOMIC, Contrary, FakeEmbedder, emb_cfg
from tools.test_pmb1_realrun import FakeClock, FakeLock, SlowStub, strip

import memory_tier_episodic as tier  # noqa: E402  (tools/ is on sys.path via tools.pmb1)

TESTDATA = Path(__file__).resolve().parent / "testdata"
GOLDEN = TESTDATA / "pmb1_episodic_lexical_golden.json"
_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install(allow_ollama=True)


def tearDownModule() -> None:
    _GUARD.close()


class Unit1(em.Embedder):
    """The cheapest embedder: a constant 1-d vector. What the semantic leg EMBEDS never depends on the vectors
    (every authorized sentence is embedded), so the 18-seed enumeration test uses it for speed; the hashing
    inner is checked to give the same set on one seed."""
    name = "unit-1"

    def embed(self, text: str) -> list[float]:
        return [1.0]


def canonical(rec: dict) -> str:
    rec = json.loads(json.dumps(rec))
    rec.pop("created")
    for k in list(rec["cost"]):
        if k.endswith("_seconds"):
            rec["cost"][k] = 0
    return json.dumps(rec, sort_keys=True)


class TestLexicalByteIdentity(unittest.TestCase):
    """embedder=None (the default) is today's lexical tier, byte-identical to the pre-change golden."""

    def check(self, key: str) -> None:
        want = json.loads(GOLDEN.read_text())[key]
        _, size, seed, mode = key.split("/")
        fx = generate_by_version(int(seed.split("-")[1]), size, 3)
        got = run_one(lambda: SUBSTRATES["episodic"](fx), StubBackbone(mode), fx, gate=True, embedder_is_lexical_stub=True)
        self.assertEqual(canonical(got), json.dumps(want, sort_keys=True))
        self.assertIsNone(got["embedding_model"])

    def test_golden_has_the_four_records(self) -> None:
        self.assertEqual(sorted(json.loads(GOLDEN.read_text())), [
            "v3/small/seed-10/lenient", "v3/small/seed-10/strict", "v3/small/seed-11/lenient", "v3/small/seed-11/strict"])

    def test_seed_10_strict(self) -> None:
        self.check("v3/small/seed-10/strict")

    def test_seed_10_lenient(self) -> None:
        self.check("v3/small/seed-10/lenient")

    def test_seed_11_strict(self) -> None:
        self.check("v3/small/seed-11/strict")

    def test_seed_11_lenient(self) -> None:
        self.check("v3/small/seed-11/lenient")

    def test_make_substrate_without_an_embedder_is_lexical(self) -> None:
        sub = make_substrate("episodic", generate_by_version(1, "tiny", 3))
        try:
            self.assertIsNone(sub.embedder)
            self.assertIsNone(sub.embedding_model)
            self.assertNotIn("semantic_tau", sub.describe())
        finally:
            sub.close()


class TestWiring(unittest.TestCase):
    def test_episodic_is_an_embedding_substrate_and_takes_the_embedder(self) -> None:
        self.assertEqual(EMBEDDING_SUBSTRATES, frozenset({"rag", "episodic", "semantic"}))  # T-2518 S4: the tier-2 view
        fx = generate_by_version(1, "tiny", 3)
        e = FakeEmbedder()
        sub = make_substrate("episodic", fx, e)
        try:
            self.assertIsInstance(sub, EpisodicTierSubstrate)
            self.assertIs(sub.embedder, e)
            self.assertEqual(sub.privacy_ceiling, fx.privacy_ceiling)
            d = sub.describe()
            self.assertEqual(d["embedding_model"], NOMIC)
            self.assertEqual((d["semantic_tau"], d["semantic_sem_k"]), (tier.DEFAULT_TAU, tier.DEFAULT_SEM_K))
            self.assertIn("SEMANTIC LEG", d["selection_method"])
        finally:
            sub.close()

    def test_the_adapter_passes_the_embedder_and_parameters_through(self) -> None:
        seen = {}
        real = tier.retrieve_extracts

        def spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)

        sub = EpisodicTierSubstrate(embedder=FakeEmbedder(), tau=0.42, sem_k=7)
        try:
            with mock.patch.object(tier, "retrieve_extracts", spy):
                sub.retrieve("anything at all", "companionship", 100)
        finally:
            sub.close()
        self.assertEqual((seen["tau"], seen["sem_k"]), (0.42, 7))
        self.assertIsInstance(seen["embedder"], FakeEmbedder)

    def test_the_record_carries_the_embedding_model(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        rec = run_one(lambda: make_substrate("episodic", fx, FakeEmbedder()), StubBackbone("strict"), fx, gate=True)
        self.assertEqual(rec["embedding_model"], NOMIC)
        self.assertEqual(rec["substrate"]["name"], "episodic-tier")


def _independence(case: unittest.TestCase, seed: int) -> None:
    fx = generate_by_version(seed, "small", 3)
    a = enumerate_texts(fx, backbone_factory=lambda: StubBackbone("strict"), inner=Unit1(), substrate="episodic")
    b = enumerate_texts(fx, backbone_factory=Contrary, inner=Unit1(), substrate="episodic")
    case.assertEqual(a, b, f"seed {seed}: backbone-dependent texts: {sorted(a ^ b)[:3]}")
    case.assertEqual({k for k, _ in a}, {em.DOCUMENT, em.QUERY})


class TestEpisodicEnumerationIsBackboneIndependent(unittest.TestCase):
    """LOCKED point 6: if the episodic embedded-text set depended on the backbone's replies, the precompute would
    be unsound (bubble-up). Two differently-answering backbones, every v3 small seed 1-18 (one test per seed)."""

    def test_the_inner_embedders_ranking_does_not_change_them(self) -> None:
        fx = generate_by_version(10, "small", 3)
        self.assertEqual(enumerate_texts(fx, substrate="episodic"),
                         enumerate_texts(fx, inner=Unit1(), substrate="episodic"))

    def test_the_episodic_texts_are_sentences_and_queries_not_rag_chunks(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        epi = enumerate_texts(fx, inner=Unit1(), substrate="episodic")
        docs = {t for k, t in epi if k == em.DOCUMENT}
        self.assertTrue(docs)
        self.assertTrue(all(tier.split_sentences(t) == [t] for t in docs))
        rag_q = {t for k, t in enumerate_texts(fx) if k == em.QUERY}
        self.assertEqual({t for k, t in epi if k == em.QUERY}, rag_q)  # same fixture queries


for _sd in range(1, 19):
    setattr(TestEpisodicEnumerationIsBackboneIndependent, f"test_seed_{_sd:02d}",
            lambda self, _sd=_sd: _independence(self, _sd))


class TestReplay(unittest.TestCase):
    def test_a_precompute_covers_a_replay_only_run_with_zero_misses(self) -> None:
        fx = generate_by_version(10, "small", 3)
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            precompute(f, em.EmbeddingCache(Path(d), f.name), enumerate_texts(fx, substrate="episodic"), batch_size=256)
            for bb in (StubBackbone("strict"), Contrary()):
                c = em.CachedEmbedder(NOMIC, Path(d), 768)
                with self.subTest(bb.name):
                    rec = run_one(lambda: make_substrate("episodic", fx, c), bb, fx, gate=True)
                    self.assertEqual(c.misses, 0)
                    self.assertEqual(rec["embedding_model"], NOMIC)

    def test_the_replay_equals_a_direct_run_over_the_same_vectors(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            precompute(f, em.EmbeddingCache(Path(d), f.name), enumerate_texts(fx, substrate="episodic"), batch_size=256)
            replay = run_one(lambda: make_substrate("episodic", fx, em.CachedEmbedder(NOMIC, Path(d))),
                             StubBackbone("strict"), fx, gate=True)
        direct = run_one(lambda: make_substrate("episodic", fx, FakeEmbedder()), StubBackbone("strict"), fx, gate=True)
        self.assertEqual(canonical(replay), canonical(direct))

    def test_an_uncovered_document_or_query_fails_closed(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        texts = enumerate_texts(fx, substrate="episodic")
        for kind in (em.DOCUMENT, em.QUERY):
            with self.subTest(kind), tempfile.TemporaryDirectory() as d:
                t = set(texts)
                t.discard(sorted(x for x in t if x[0] == kind)[0])
                f = FakeEmbedder()
                precompute(f, em.EmbeddingCache(Path(d), f.name), t, batch_size=256)
                with self.assertRaises(em.EmbeddingCacheMiss):
                    run_one(lambda: make_substrate("episodic", fx, em.CachedEmbedder(NOMIC, Path(d))),
                            StubBackbone(), fx, gate=True)

    def test_a_rag_only_precompute_does_not_cover_the_episodic_tier(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            precompute(f, em.EmbeddingCache(Path(d), f.name), enumerate_texts(fx), batch_size=256)
            with self.assertRaises(em.EmbeddingCacheMiss):
                run_one(lambda: make_substrate("episodic", fx, em.CachedEmbedder(NOMIC, Path(d))),
                        StubBackbone(), fx, gate=True)


class TestRealrunEpisodic(unittest.TestCase):
    def go(self, out: str, clock: FakeClock, cfg: rr.RunConfig):
        made: list[FakeEmbedder] = []

        def emb_factory(spec: str):
            made.append(FakeEmbedder(clock=clock))
            return made[-1]

        s = rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"),
                       FakeLock(clock), clock=clock, embedder_factory=emb_factory)
        return s, made

    def test_embed_texts_for_enumerates_each_embedding_substrate(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            cfg = emb_cfg(out, substrates=["rag", "episodic", "amnesiac"])
            fxs = {1: generate_by_version(1, "tiny", 3)}
            units = rr.plan_units([1], cfg.backbones, cfg.substrates)
            want = enumerate_texts(fxs[1]) | enumerate_texts(fxs[1], substrate="episodic")
            self.assertEqual(rr.embed_texts_for(cfg, fxs, units), want)
            only_epi = [u for u in units if u.substrate == "episodic"]
            self.assertEqual(rr.embed_texts_for(cfg, fxs, only_epi), enumerate_texts(fxs[1], substrate="episodic"))
            self.assertEqual(rr.embed_texts_for(cfg, fxs, [u for u in units if u.substrate == "amnesiac"]), set())

    def test_realrun_precomputes_and_replays_the_episodic_units(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = emb_cfg(out, substrates=["episodic", "amnesiac"])
            s, made = self.go(out, clock, cfg)
            fx = generate_by_version(1, "tiny", 3)
            self.assertEqual(s["embed"]["texts"], len(enumerate_texts(fx, substrate="episodic")))
            self.assertEqual(set(made[0].asked), enumerate_texts(fx, substrate="episodic"))
            rec = json.loads((Path(out) / "seed-1" / "stub-strict" / "episodic.json").read_text())
            self.assertEqual(rec["embedding_model"], NOMIC)
            direct = run_one(lambda: make_substrate("episodic", fx, FakeEmbedder()), StubBackbone("strict"), fx, gate=True)
            self.assertEqual(strip(rec), strip(direct))
            hdr = json.loads((Path(out) / ".journal" / "seed-1" / "stub-strict" / "episodic.jsonl").read_text().splitlines()[0])
            self.assertEqual(hdr["header"].get("embedding_model"), NOMIC)
            # resume: complete, nothing re-embedded, nothing rerun
            s2, made2 = self.go(out, clock, cfg)
            self.assertEqual((s2["ran"], made2), (0, []))

    def test_a_lexical_episodic_record_is_not_complete_under_the_embedder(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            from tools.test_pmb1_realrun import cfg_for
            rr.realrun(cfg_for(out, substrates=["episodic"], max_lock_s=1e9),
                       lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock),
                       clock=clock)
            for p in (Path(out) / ".journal").rglob("*.jsonl"):
                p.unlink()
            fx = generate_by_version(1, "tiny", 3)
            u = rr.plan_units([1], ["stub"], ["episodic"])[0]
            self.assertTrue(rr.unit_complete(Path(out) / u.rel(), fx, u, None))
            self.assertFalse(rr.unit_complete(Path(out) / u.rel(), fx, u, NOMIC))
            s, _ = self.go(out, clock, emb_cfg(out, substrates=["episodic"]))
            self.assertEqual((s["skipped"], s["ran"]), (0, 2))


class TestCliDryRunEpisodic(unittest.TestCase):
    def test_dry_run_counts_the_episodic_texts_and_calls_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as out, mock.patch("urllib.request.urlopen", side_effect=AssertionError("called")), \
                mock.patch.object(em.OllamaEmbedder, "__init__", side_effect=AssertionError("constructed")):
            out_s = io.StringIO()
            with contextlib.redirect_stdout(out_s):
                rc = cli.main(["realrun", "--backbones", "ollama:qwen2.5:14b:qwen2,ollama:llama3.1:8b:llama",
                               "--substrate", "episodic,amnesiac", "--fixture", "size=tiny,version=3", "--seeds", "1,2",
                               "--embedder", NOMIC, "--out", out, "--dry-run"])
            self.assertEqual(rc, 0)
            n = len(enumerate_texts(generate_by_version(1, "tiny", 3), substrate="episodic")
                    | enumerate_texts(generate_by_version(2, "tiny", 3), substrate="episodic"))
            self.assertIn(f"embed precompute {NOMIC}: {n} texts (0 cached, {n} to embed)", out_s.getvalue())
            self.assertEqual(os.listdir(out), [])


if __name__ == "__main__":
    unittest.main()
