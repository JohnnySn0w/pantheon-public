#!/usr/bin/env python3
"""PMB-1 step 2g-2 (T-2518): the real local embedder for the RAG baseline, offline.

``OllamaEmbedder`` is tested against CAPTURED /api/embed bodies (tools/testdata/pmb1_ollama_embed_*.json, verbatim
from Ollama 0.34.2 on gpu-ml, nomic-embed-text digest 0a109f422b47, 2026-10-13) with urlopen mocked; the HTTP 400
body is the captured ``truncate: false`` context-length refusal. No network, no Ollama."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools.pmb1 import cli, offline, realrun as rr
from tools.pmb1 import embedder as em
from tools.pmb1.backbone import Backbone, StubBackbone
from tools.pmb1.embedphase import enumerate_texts, precompute
from tools.pmb1.fixtures_v3 import generate_by_version
from tools.pmb1.runner import SUBSTRATES, make_substrate, run_one
from tools.pmb1.substrate import VerbatimRAGBaseline
from tools.test_pmb1_realrun import FakeClock, FakeLock, SlowStub, cfg_for, strip

_GUARD = None
TESTDATA = Path(__file__).resolve().parent / "testdata"
LIST2 = json.loads((TESTDATA / "pmb1_ollama_embed_list2.json").read_text())   # 2-item list input
SINGLE = json.loads((TESTDATA / "pmb1_ollama_embed_single.json").read_text())  # a single string input
CTX_400 = b'{"error":"the input length exceeds the context length"}'   # captured, truncate:false + an oversize input
# GET /api/ps with nomic-embed-text loaded: captured on Ollama 0.34.2, 2026-10-13 (relayed by head_memory_orchestrator in
# T-2518 2g-2 REBOUND 1; ~/archivist-work/t2518-2g2-captures/unload-capture.txt "ps-before"). "details" and
# "expires_at" were elided in the relay and are not read.
PS_LOADED = {"models": [{"name": "nomic-embed-text:latest", "model": "nomic-embed-text:latest", "size": 323150151,
                         "digest": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f", "details": {},
                         "expires_at": "...", "size_vram": 323150151, "context_length": 2048}]}
PS_EMPTY = {"models": []}   # captured after the unload (same file, "ps-after-generate")
DIGEST = "0a109f422b47"
NOMIC = f"ollama:nomic-embed-text@{DIGEST}"
GATE_SEEDS = tuple(range(10, 19))   # the realrun gate seeds (v3 small)


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install(allow_ollama=True)


def tearDownModule() -> None:
    _GUARD.close()


class Transport:
    """A fake urlopen: records each request's URL, JSON body and timeout, answers from a queue of
    (status, body bytes)."""

    def __init__(self, *answers: tuple[int, bytes]) -> None:
        self.answers = list(answers)
        self.seen: list[dict] = []

    def __call__(self, req, timeout):
        self.seen.append({"url": req.full_url, "method": req.get_method(),
                          "body": None if req.data is None else json.loads(req.data), "timeout": timeout})
        status, body = self.answers.pop(0)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return body
        return Resp()


def ollama(**kw) -> em.OllamaEmbedder:
    return em.OllamaEmbedder("nomic-embed-text", DIGEST, base_url="http://ollama.invalid:11434/", **kw)


def body(d: dict) -> bytes:
    return json.dumps(d).encode()


class TestOllamaEmbedderCaptured(unittest.TestCase):
    def test_parse_both_captured_shapes(self) -> None:
        e = ollama()
        two = e.parse_embeddings(LIST2, 2)
        one = e.parse_embeddings(SINGLE, 1)
        self.assertEqual([len(v) for v in two + one], [768, 768, 768])
        for v in two + one:   # the capture: already L2-normalised
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=4)
        self.assertEqual(two[0][0], LIST2["embeddings"][0][0])
        self.assertEqual(e.dim, 768)

    def test_the_request_sends_truncate_false_the_prefixes_and_a_list(self) -> None:
        t = Transport((200, body(LIST2)), (200, body(SINGLE)))
        e = ollama(keep_alive="2m", timeout=30.0)
        with mock.patch("urllib.request.urlopen", t):
            vs = e.embed_many(["The kettle was descaled on Tuesday.", "x"], em.DOCUMENT)
            q = e.embed_query("when was the kettle descaled?")
        self.assertEqual(len(vs), 2)
        self.assertEqual(len(q), 768)
        b0, b1 = t.seen[0]["body"], t.seen[1]["body"]
        self.assertEqual(t.seen[0]["url"], "http://ollama.invalid:11434/api/embed")
        self.assertIs(b0["truncate"], False)
        self.assertIs(b1["truncate"], False)
        self.assertEqual(b0["model"], "nomic-embed-text")
        self.assertEqual(b0["keep_alive"], "2m")
        self.assertEqual(b0["input"], ["search_document: The kettle was descaled on Tuesday.", "search_document: x"])
        self.assertEqual(b1["input"], ["search_query: when was the kettle descaled?"])
        self.assertEqual(t.seen[0]["timeout"], 30.0)

    def test_the_timeout_is_capped_by_the_driver(self) -> None:
        t = Transport((200, body(SINGLE)), (200, body(SINGLE)))
        e = ollama(timeout=120.0)
        with mock.patch("urllib.request.urlopen", t):
            e.embed_document("a")
            e.timeout_cap = 7.0
            e.embed_document("a")
        self.assertEqual([s["timeout"] for s in t.seen], [120.0, 7.0])

    def test_name_digest_and_url(self) -> None:
        e = ollama()
        self.assertEqual(e.name, NOMIC)
        self.assertFalse(em.is_lexical_stub(e.name))
        with self.assertRaises(ValueError):
            em.OllamaEmbedder("nomic-embed-text", "", base_url="http://x.invalid")
        with self.assertRaises(ValueError):
            em.OllamaEmbedder("nomic-embed-text", "latest", base_url="http://x.invalid")
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            em.OllamaEmbedder("nomic-embed-text", DIGEST)
        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://env.invalid/"}):
            self.assertEqual(em.OllamaEmbedder("nomic-embed-text", DIGEST).base_url, "http://env.invalid")
        self.assertEqual(em.parse_embedder_spec(NOMIC), ("nomic-embed-text", DIGEST))
        for bad in ("nomic-embed-text@0a109f422b47", "ollama:nomic-embed-text", "ollama:@0a109f422b47",
                    "ollama:nomic-embed-text@0A109F422B47", "ollama:nomic-embed-text@0a109f422b4"):
            with self.subTest(bad), self.assertRaises(ValueError):
                em.parse_embedder_spec(bad)

    def test_the_context_length_400_is_a_typed_error(self) -> None:
        t = Transport((400, CTX_400))
        with mock.patch("urllib.request.urlopen", t), self.assertRaises(em.EmbeddingContextExceeded) as cm:
            ollama().embed_document("word " * 5000)
        self.assertIn("exceeds the context length", str(cm.exception))

    def test_other_http_errors_raise(self) -> None:
        for status, b in ((500, b'{"error":"boom"}'), (404, b"not json")):
            with self.subTest(status), mock.patch("urllib.request.urlopen", Transport((status, b))), \
                    self.assertRaises(em.EmbedderError) as cm:
                ollama().embed_document("a")
            self.assertNotIsInstance(cm.exception, em.EmbeddingContextExceeded)

    def test_a_malformed_body_raises_never_a_zero_vector(self) -> None:
        v = SINGLE["embeddings"][0]
        bad = [
            ("not json", b"{nope"),
            ("not an object", b"[1, 2]"),
            ("no embeddings", body({"model": "nomic-embed-text"})),
            ("too few", body({"embeddings": []})),
            ("too many", body({"embeddings": [v, v]})),
            ("empty vector", body({"embeddings": [[]]})),
            ("a string", body({"embeddings": [["0.1"] * 768]})),
            ("a bool", body({"embeddings": [[True] * 768]})),
            ("an error field", body({"error": "model not found", "embeddings": [v]})),
        ]
        for label, b in bad:
            with self.subTest(label), mock.patch("urllib.request.urlopen", Transport((200, b))), \
                    self.assertRaises(em.EmbedderError):
                ollama().embed_document("a")
        with self.assertRaises(em.EmbedderError):   # NaN never parses into a vector
            ollama().parse_embeddings({"embeddings": [[float("nan")] * 768]}, 1)

    def test_a_dim_change_inside_one_run_raises(self) -> None:
        short = {"embeddings": [SINGLE["embeddings"][0][:767]]}   # the capture, one value cut
        e = em.OllamaEmbedder("some-unknown-model", DIGEST, base_url="http://x.invalid")
        self.assertIsNone(e.dim)
        with mock.patch("urllib.request.urlopen", Transport((200, body(SINGLE)), (200, body(short)))):
            e.embed_document("a")
            with self.assertRaises(em.EmbeddingDimMismatch):
                e.embed_document("b")
        # a known model's dim is expected from the first call
        with mock.patch("urllib.request.urlopen", Transport((200, body(short)))), self.assertRaises(em.EmbeddingDimMismatch):
            ollama().embed_document("a")

    def test_never_sends_an_empty_list(self) -> None:
        with mock.patch("urllib.request.urlopen", Transport()), self.assertRaises(ValueError):
            ollama().embed_many([], em.DOCUMENT)

    def test_unload_posts_keep_alive_zero(self) -> None:
        t = Transport((200, b'{"done": true}'))
        with mock.patch("urllib.request.urlopen", t):
            ollama().unload()
        self.assertEqual(t.seen[0]["url"], "http://ollama.invalid:11434/api/generate")
        self.assertEqual(t.seen[0]["body"], {"model": "nomic-embed-text", "keep_alive": 0})
        self.assertEqual(t.seen[0]["timeout"], em.EMBED_UNLOAD_TIMEOUT_S)


class TestDigestAndNorm(unittest.TestCase):
    """REBOUND 1: the loaded weights must be the ones the name claims; a known model's vectors must be unit-norm."""

    def test_the_captured_ps_matches_through_the_implicit_latest_tag(self) -> None:
        t = Transport((200, body(PS_LOADED)))
        with mock.patch("urllib.request.urlopen", t):
            full = ollama().verify_digest()
        self.assertTrue(full.startswith(DIGEST))
        self.assertEqual((t.seen[0]["url"], t.seen[0]["method"], t.seen[0]["body"]),
                         ("http://ollama.invalid:11434/api/ps", "GET", None))
        self.assertEqual(t.seen[0]["timeout"], em.PS_TIMEOUT_S)
        e = em.OllamaEmbedder("nomic-embed-text:latest", DIGEST, base_url="http://x.invalid")   # an explicit tag
        with mock.patch("urllib.request.urlopen", Transport((200, body(PS_LOADED)))):
            e.verify_digest()

    def test_a_digest_mismatch_raises_naming_both(self) -> None:
        e = em.OllamaEmbedder("nomic-embed-text", "ffffffffffff", base_url="http://x.invalid")
        with mock.patch("urllib.request.urlopen", Transport((200, body(PS_LOADED)))), \
                self.assertRaises(em.EmbeddingDigestMismatch) as cm:
            e.verify_digest()
        self.assertIn("ffffffffffff", str(cm.exception))
        self.assertIn(PS_LOADED["models"][0]["digest"], str(cm.exception))

    def test_a_missing_entry_raises(self) -> None:
        other = {"models": [dict(PS_LOADED["models"][0], name="llama3.1:8b", model="llama3.1:8b")]}
        for label, ps in (("empty", PS_EMPTY), ("another model", other), ("no list", {"error": "x"}),
                          ("tag differs", {"models": [dict(PS_LOADED["models"][0], name="nomic-embed-text:v1.5",
                                                           model="nomic-embed-text:v1.5")]})):
            with self.subTest(label), mock.patch("urllib.request.urlopen", Transport((200, body(ps)))), \
                    self.assertRaises(em.EmbeddingDigestMismatch):
                ollama().verify_digest()

    def test_a_degenerate_vector_raises(self) -> None:
        v = SINGLE["embeddings"][0]
        for label, bad in (("zero", [0.0] * 768), ("half", [x * 0.5 for x in v]), ("just off", [x * 1.0011 for x in v])):
            with self.subTest(label), self.assertRaises(em.EmbedderError) as cm:
                ollama().parse_embeddings({"embeddings": [bad]}, 1)
            self.assertIn("norm", str(cm.exception))
        ollama().parse_embeddings({"embeddings": [[x * 1.0009 for x in v]]}, 1)   # inside the tolerance
        # an unknown model has no normalisation contract: its norm is not policed
        em.OllamaEmbedder("other", DIGEST, base_url="http://x.invalid").parse_embeddings({"embeddings": [[0.0] * 8]}, 1)

    def test_a_real_embedder_with_the_wrong_weights_caches_nothing(self) -> None:
        """The driver's hook on the real OllamaEmbedder: embed (captured body), then /api/ps disagrees."""
        e = em.OllamaEmbedder("nomic-embed-text", "ffffffffffff", base_url="http://x.invalid")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("urllib.request.urlopen", Transport((200, body(SINGLE)), (200, body(PS_LOADED)))), \
                    self.assertRaises(em.EmbeddingDigestMismatch):
                precompute(e, em.EmbeddingCache(Path(d), e.name), {(em.DOCUMENT, "a")},
                           after_embed=lambda i: rr._verify_digest(e, i, lambda s: None))
            self.assertEqual(list(Path(d).glob("shard-*.json")), [])
        with tempfile.TemporaryDirectory() as d, mock.patch(
                "urllib.request.urlopen", Transport((200, body(SINGLE)), (200, body(PS_LOADED)))):
            e = ollama()
            r = precompute(e, em.EmbeddingCache(Path(d), e.name), {(em.DOCUMENT, "a")},
                           after_embed=lambda i: rr._verify_digest(e, i, lambda s: None))
            self.assertEqual(r["embedded"], 1)
            self.assertEqual(len(list(Path(d).glob("shard-*.json"))), 1)


class FakeEmbedder(em.Embedder):
    """A hashing-backed embedder named like a real one; counts what it is asked; optional fake clock cost."""

    def __init__(self, name: str = NOMIC, dim: int = 768, clock: FakeClock | None = None, per_item: float = 0.0,
                 events: list | None = None) -> None:
        self.name, self.inner = name, em.HashingEmbedder(dim)
        self.asked: list[tuple[str, str]] = []
        self.clock, self.per_item, self.events = clock, per_item, events
        self.timeout_cap: float | None = None
        self.unloads = 0

    def embed(self, text: str) -> list[float]:
        return self.inner.embed(text)

    def embed_document(self, text: str) -> list[float]:
        return self.embed_many([text], em.DOCUMENT)[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_many([text], em.QUERY)[0]

    def embed_many(self, texts, kind):
        if self.events is not None:
            self.events.append(("embed", len(texts)))
        if self.clock is not None:
            self.clock.t += self.per_item * len(texts)
        self.asked += [(kind, t) for t in texts]
        return [self.inner.embed(f"{kind}|{t}") for t in texts]

    loaded_digest = PS_LOADED["models"][0]["digest"]   # what a fake /api/ps reports as loaded

    def verify_digest(self) -> str:
        if self.events is not None:
            self.events.append(("verify",))
        if not self.loaded_digest.startswith(self.name.rpartition("@")[2]):
            raise em.EmbeddingDigestMismatch(f"loaded {self.loaded_digest}, claimed {self.name}")
        return self.loaded_digest

    def unload(self) -> None:
        self.unloads += 1
        if self.events is not None:
            self.events.append(("embed-unload",))


TEXTS = {(em.DOCUMENT, "alpha beta"), (em.DOCUMENT, "gamma"), (em.QUERY, "alpha?"), (em.QUERY, "gamma?"),
         (em.DOCUMENT, "delta epsilon"), (em.QUERY, "zeta")}


class TestCachedEmbedder(unittest.TestCase):
    def fill(self, d: str, **kw) -> tuple[FakeEmbedder, dict]:
        f = FakeEmbedder(**kw)
        r = precompute(f, em.EmbeddingCache(Path(d), f.name), TEXTS, batch_size=2)
        return f, r

    def test_hit_returns_the_inner_vector_and_names_the_real_model(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f, r = self.fill(d)
            self.assertEqual((r["embedded"], r["skipped"], r["remaining"]), (6, 0, 0))
            c = em.CachedEmbedder(NOMIC, Path(d), 768)
            self.assertEqual(c.name, NOMIC)
            self.assertEqual(c.embed_document("alpha beta"), f.inner.embed("document|alpha beta"))
            self.assertEqual(c.embed_query("alpha?"), f.inner.embed("query|alpha?"))
            self.assertEqual(c.misses, 0)
            self.assertEqual([p.name for p in Path(d).iterdir() if p.name.endswith(".tmp")], [])

    def test_a_miss_fails_closed_naming_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.fill(d)
            c = em.CachedEmbedder(NOMIC, Path(d))
            with self.assertRaises(em.EmbeddingCacheMiss) as cm:
                c.embed_document("never embedded")
            self.assertIn(em.cache_key(NOMIC, em.DOCUMENT, "never embedded"), str(cm.exception))
            with self.assertRaises(em.EmbeddingCacheMiss):   # the kind is part of the key
                c.embed_query("alpha beta")
            with self.assertRaises(em.EmbedderError):
                c.embed("alpha beta")

    def test_a_header_mismatch_raises_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.fill(d)
            with self.assertRaises(em.EmbeddingCacheMismatch) as cm:
                em.CachedEmbedder("ollama:nomic-embed-text@ffffffffffff", Path(d))
            self.assertIn("made by embedder", str(cm.exception))
            with self.assertRaises(em.EmbeddingCacheMismatch):
                em.CachedEmbedder(NOMIC, Path(d), 1024)
            with self.assertRaises(em.EmbeddingCacheMismatch):   # the fill side refuses too
                em.EmbeddingCache(Path(d), "ollama:other@ffffffffffff")

    def test_a_corrupt_cache_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.fill(d)
            shard = sorted(Path(d).glob("shard-*.json"))[0]
            data = json.loads(shard.read_text())
            k = next(iter(data))
            data[k] = data[k][:10]
            shard.write_text(json.dumps(data))
            with self.assertRaises(em.EmbeddingCacheMismatch):
                em.CachedEmbedder(NOMIC, Path(d))
            shard.write_text("{trunc")
            with self.assertRaises(em.EmbeddingCacheMismatch):
                em.CachedEmbedder(NOMIC, Path(d))
            (Path(d) / em.HEADER).unlink()
            with self.assertRaises(em.EmbeddingCacheMismatch):
                em.CachedEmbedder(NOMIC, Path(d))

    def test_a_shard_gap_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            c = em.EmbeddingCache(Path(d), NOMIC)
            for k in "abc":
                c.add({k * 64: [1.0, 0.0]})
            (Path(d) / "shard-000001.json").unlink()   # a gap: two files left, highest index 2
            c = em.EmbeddingCache(Path(d), NOMIC)
            c.add({"d" * 64: [0.0, 1.0]})
            self.assertEqual(sorted(p.name for p in Path(d).glob("shard-*.json")),
                             ["shard-000000.json", "shard-000002.json", "shard-000003.json"])
            self.assertEqual(set(em.EmbeddingCache(Path(d), NOMIC).vectors), {"a" * 64, "c" * 64, "d" * 64})

    def test_resume_skips_existing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            cache = em.EmbeddingCache(Path(d), f.name)
            r = precompute(f, cache, TEXTS, batch_size=2, before_batch=lambda i, n: i < 1)   # stopped after one batch
            self.assertEqual((r["embedded"], r["remaining"]), (2, 4))
            g = FakeEmbedder()
            r = precompute(g, em.EmbeddingCache(Path(d), g.name), TEXTS, batch_size=2)
            self.assertEqual((r["embedded"], r["skipped"], r["remaining"]), (4, 2, 0))
            self.assertEqual(set(f.asked) | set(g.asked), TEXTS)
            self.assertEqual(set(f.asked) & set(g.asked), set())
            h = FakeEmbedder()
            r = precompute(h, em.EmbeddingCache(Path(d), h.name), TEXTS, batch_size=2)
            self.assertEqual((r["embedded"], r["skipped"], h.asked), (0, 6, []))

    def test_a_batch_never_mixes_kinds_and_precompute_checks_the_name(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            calls = []
            orig = f.embed_many
            f.embed_many = lambda texts, kind: calls.append((kind, list(texts))) or orig(texts, kind)
            precompute(f, em.EmbeddingCache(Path(d), f.name), TEXTS, batch_size=10)
            self.assertEqual(sorted(k for k, _ in calls), [em.DOCUMENT, em.QUERY])
            with self.assertRaises(ValueError):
                precompute(FakeEmbedder(name="ollama:x@ffffffffffff"), em.EmbeddingCache(Path(d), f.name), TEXTS)


class Contrary(Backbone):
    """Answers differently from every stub: a reversed, junk reply; judges everything wrong."""
    name, family, knows_lexicon = "contrary", "contrary", False

    def generate(self, question, evidence):
        self.calls += 1
        return "zzz " + question[::-1]

    def judge(self, question, answer, gold):
        self.calls += 1
        return False


class TestEnumerationIsBackboneIndependent(unittest.TestCase):
    """LOCKED design point 3: the precompute is sound only if what the RAG substrate embeds does not depend on the
    backbone's replies."""

    def test_two_differently_answering_backbones_embed_the_same_texts_across_the_gate_seeds(self) -> None:
        for seed in GATE_SEEDS:
            fx = generate_by_version(seed, "small", 3)
            a = enumerate_texts(fx, backbone_factory=lambda: StubBackbone("strict"))
            b = enumerate_texts(fx, backbone_factory=Contrary)
            with self.subTest(seed=seed):
                self.assertEqual(a, b, f"backbone-dependent texts: {sorted((a ^ b))[:3]}")
                self.assertEqual({k for k, _ in a}, {em.DOCUMENT, em.QUERY})

    def test_and_the_inner_embedders_ranking_does_not_change_them_either(self) -> None:
        fx = generate_by_version(10, "small", 3)
        self.assertEqual(enumerate_texts(fx), enumerate_texts(fx, inner=em.HashingEmbedder(16)))

    def test_a_precompute_covers_a_replay_only_run_with_zero_misses(self) -> None:
        fx = generate_by_version(10, "small", 3)
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            precompute(f, em.EmbeddingCache(Path(d), f.name), enumerate_texts(fx), batch_size=64)
            for bb in (StubBackbone("strict"), Contrary()):
                c = em.CachedEmbedder(NOMIC, Path(d), 768)
                with self.subTest(bb.name):
                    rec = run_one(lambda: make_substrate("rag", fx, c), bb, fx, gate=True)
                    self.assertEqual(c.misses, 0)
                    self.assertEqual(rec["embedding_model"], NOMIC)
                    self.assertIs(rec["embedder_is_lexical_stub"], False)

    def test_an_uncovered_text_fails_the_run_closed(self) -> None:
        fx = generate_by_version(10, "small", 3)
        with tempfile.TemporaryDirectory() as d:
            f = FakeEmbedder()
            texts = enumerate_texts(fx)
            texts.discard(sorted(t for t in texts if t[0] == em.QUERY)[0])
            precompute(f, em.EmbeddingCache(Path(d), f.name), texts, batch_size=64)
            with self.assertRaises(em.EmbeddingCacheMiss):
                run_one(lambda: make_substrate("rag", fx, em.CachedEmbedder(NOMIC, Path(d))), StubBackbone(), fx, gate=True)


class TestHashingByteIdentity(unittest.TestCase):
    """LOCKED design point 1: the hashing stub's records are byte-identical to the golden produced BEFORE the
    embed_document / embed_query change (commit 91a2cef9b, generated at 52ba40e7c)."""

    def test_records_match_the_pre_change_golden(self) -> None:
        golden = json.loads((TESTDATA / "pmb1_rag_hashing_golden.json").read_text())
        self.assertEqual(len(golden), 6)
        for key, want in golden.items():
            v, size, seed, mode = key.split("/")
            v, seed = int(v[1:]), int(seed.split("-")[1])
            fx = generate_by_version(seed, size, v)
            got = run_one(lambda: SUBSTRATES["rag"](fx), StubBackbone(mode), fx, gate=(v >= 2), embedder_is_lexical_stub=(v >= 2))
            got.pop("created")
            for k in list(got["cost"]):
                if k.endswith("_seconds"):
                    got["cost"][k] = 0
            with self.subTest(key):
                self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(want, sort_keys=True))

    def test_hashing_document_and_query_are_embed(self) -> None:
        h = em.HashingEmbedder()
        for t in ("Ava's colour is teal.", "what is ava's colour?", ""):
            self.assertEqual(h.embed_document(t), h.embed(t))
            self.assertEqual(h.embed_query(t), h.embed(t))
        self.assertEqual(VerbatimRAGBaseline().embedding_model, "hashing-512-v1")
        self.assertEqual(SUBSTRATES["rag"](generate_by_version(1, "tiny", 3)).embedding_model, "hashing-512-v1")


# ---------------------------------------------------------------------------------------------------- realrun

class EventStub(SlowStub):
    def _tick(self) -> None:
        if self.events is not None:
            self.events.append(("bb", self.name))
        super()._tick()


def emb_cfg(out: str, **kw) -> rr.RunConfig:
    d = dict(substrates=["rag", "amnesiac"], embedder_is_lexical_stub=False, embedder=NOMIC, max_lock_s=1e9, embed_batch=16)
    d.update(kw)
    return cfg_for(out, **d)


def sessions_of(events: list) -> list[list]:
    out, cur = [], None
    for e in events:
        if e == ("enter",):
            cur = []
        elif e == ("exit",):
            out.append(cur)
            cur = None
        else:
            assert cur is not None, f"{e} outside any lock session"
            cur.append(e)
    return out


class TestRealrunEmbedPhase(unittest.TestCase):
    def go(self, out: str, clock: FakeClock, events: list, cfg: rr.RunConfig | None = None, per_item: float = 0.0,
           bb_cost: float = 0.0):
        made: list[FakeEmbedder] = []
        lock = FakeLock(clock, events)

        def emb_factory(spec: str):
            made.append(FakeEmbedder(clock=clock, per_item=per_item, events=events))
            return made[-1]

        s = rr.realrun(cfg or emb_cfg(out), lambda spec, n: EventStub(clock, bb_cost, "strict" if spec == "stub" else "lenient",
                                                                      events=events),
                       lock, clock=clock, embedder_factory=emb_factory)
        return s, lock, made

    def test_the_embed_phase_runs_first_in_its_own_sessions_and_never_with_a_backbone(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock(), []
            s, lock, made = self.go(out, clock, events, emb_cfg(out, max_lock_s=60.0, gap_s=45.0, embed_batch=8), per_item=2.0)
            sess = sessions_of(events)
            kinds = [{e[0] for e in x} for x in sess]
            n_embed = sum(1 for k in kinds if "embed" in k)
            self.assertGreater(n_embed, 1, "a 60 s budget splits the precompute into several sessions")
            self.assertEqual(n_embed, s["embed"]["sessions"])
            for k in kinds[:n_embed]:   # the first sessions: the embedder only, unloaded before release
                self.assertEqual(k, {"embed", "verify", "embed-unload"})
            for x in sess[:n_embed]:
                self.assertEqual(x[-1], ("embed-unload",))
                # the digest is verified once per session, right after its FIRST batch
                self.assertEqual([e[0] for e in x[:2]], ["embed", "verify"])
                self.assertEqual(x.count(("verify",)), 1)
            for k in kinds[n_embed:]:   # every later session: no embedder at all
                self.assertNotIn("embed", k)
                self.assertNotIn("embed-unload", k)
            self.assertTrue(any("bb" in k for k in kinds[n_embed:]))
            for x in lock.sessions[:n_embed]:
                self.assertLessEqual(x["end"] - x["start"], 60.0)
            for a, b in zip(lock.sessions, lock.sessions[1:]):   # the gap holds across the phase change too
                self.assertGreaterEqual(b["start"] - a["end"], 45.0)
            self.assertEqual(len(lock.sessions), s["sessions"])
            self.assertEqual(sum(f.unloads for f in made), n_embed)
            rec = json.loads((Path(out) / "seed-1" / "stub-strict" / "rag.json").read_text())
            self.assertEqual(rec["embedding_model"], NOMIC)
            self.assertIs(rec["embedder_is_lexical_stub"], False)
            self.assertEqual(rec["substrate"]["embedding_model"], NOMIC)
            amn = json.loads((Path(out) / "seed-1" / "stub-strict" / "amnesiac.json").read_text())
            self.assertIsNone(amn["embedding_model"])

    def test_the_replay_records_equal_a_direct_run_over_the_same_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            self.go(out, clock, [])
            fx = generate_by_version(1, "tiny", 3)
            direct = run_one(lambda: make_substrate("rag", fx, FakeEmbedder()), StubBackbone("strict"), fx, gate=True)
            rec = json.loads((Path(out) / "seed-1" / "stub-strict" / "rag.json").read_text())
            self.assertEqual(strip(rec), strip(direct))

    def test_resume_reembeds_nothing_and_a_hashing_record_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            # a hashing run first: its RAG records must not count as complete under --embedder
            rr.realrun(cfg_for(out, substrates=["rag", "amnesiac"], max_lock_s=1e9),
                       lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)
            for p in (Path(out) / ".journal").rglob("*.jsonl"):
                p.unlink()   # (journals are per run config; see the header test below)
            s, _, made = self.go(out, clock, [])
            self.assertEqual((s["skipped"], s["ran"]), (2, 2))   # the two amnesiac units were complete
            self.assertGreater(len(made[0].asked), 0)
            s, _, made = self.go(out, clock, [])
            self.assertEqual((s["ran"], s["sessions"], made), (0, 0, []))
            (Path(out) / "seed-1" / "stub-strict" / "rag.json").unlink()
            events: list = []
            s, _, made = self.go(out, clock, events)
            self.assertEqual(made, [])   # the cache already covers it: no embed session
            self.assertEqual((s["ran"], s["embed"]["sessions"], s["embed"]["cached_before"]), (1, 0, s["embed"]["texts"]))
            self.assertNotIn(("embed-unload",), events)

    def test_a_cache_from_another_dim_refuses_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = emb_cfg(out)
            em.EmbeddingCache(rr.embed_cache_path(cfg), NOMIC).add({"k" * 64: [0.0] * 384})
            with self.assertRaises(em.EmbeddingCacheMismatch):
                self.go(out, clock, [], cfg)

    def test_a_hashing_journal_does_not_resume_under_the_real_embedder(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg_for(out, substrates=["rag"], max_lock_s=1e9),
                           lambda spec, n: SlowStub(clock, 0.0, die_after=5), FakeLock(clock), clock=clock)
            with self.assertRaises(rr.JournalMismatch) as cm:
                self.go(out, clock, [], emb_cfg(out, substrates=["rag"]))
            self.assertIn("embedding_model", str(cm.exception))
            self.assertIn("this run has", str(cm.exception))
        with tempfile.TemporaryDirectory() as out:   # and the reverse: a real-embedder journal under the stub
            clock = FakeClock()
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(emb_cfg(out, substrates=["rag"]), lambda spec, n: SlowStub(clock, 0.0, die_after=5),
                           FakeLock(clock), clock=clock, embedder_factory=lambda spec: FakeEmbedder())
            with self.assertRaises(rr.JournalMismatch) as cm:
                rr.realrun(cfg_for(out, substrates=["rag"], max_lock_s=1e9), lambda spec, n: SlowStub(clock, 0.0),
                           FakeLock(clock), clock=clock)
            self.assertIn("this run has none", str(cm.exception))
            hdr = json.loads(next((Path(out) / ".journal").rglob("rag.jsonl")).read_text().splitlines()[0])["header"]
            self.assertEqual(hdr["embedding_model"], NOMIC)

    def test_no_embed_session_starts_in_the_ingest_window(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock("2026-10-10T04:56:00"), []
            with self.assertRaises(rr.InWindow):
                self.go(out, clock, events)
            self.assertEqual(events, [])
            self.assertFalse(rr.embed_cache_path(emb_cfg(out)).exists())

    def test_a_digest_mismatch_stops_the_run_before_anything_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock(), []

            class Wrong(FakeEmbedder):
                loaded_digest = "ffffffffffff" + "0" * 52

            with self.assertRaises(em.EmbeddingDigestMismatch):
                rr.realrun(emb_cfg(out), lambda spec, n: self.fail("no backbone"), FakeLock(clock, events), clock=clock,
                           embedder_factory=lambda spec: Wrong(events=events))
            self.assertEqual([e[0] for e in events], ["enter", "embed", "verify", "embed-unload", "exit"])
            self.assertEqual(list(rr.embed_cache_path(emb_cfg(out)).glob("shard-*.json")), [])

    def test_an_embed_error_releases_and_unloads(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock(), []
            lock = FakeLock(clock, events)

            class Boom(FakeEmbedder):
                def embed_many(self, texts, kind):
                    super().embed_many(texts, kind)
                    raise em.EmbeddingContextExceeded("too long")

            with self.assertRaises(em.EmbeddingContextExceeded):
                rr.realrun(emb_cfg(out), lambda spec, n: self.fail("no backbone"), lock, clock=clock,
                           embedder_factory=lambda spec: Boom(events=events))
            self.assertEqual(events[-2:], [("embed-unload",), ("exit",)])

    def test_the_embedder_and_the_stub_flag_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(ValueError):
                rr.realrun(emb_cfg(out, embedder_is_lexical_stub=True), lambda s, n: None, FakeLock(FakeClock()),
                           clock=FakeClock(), embedder_factory=lambda spec: FakeEmbedder())
            with self.assertRaises(ValueError):   # a factory is required
                rr.realrun(emb_cfg(out), lambda s, n: None, FakeLock(FakeClock()), clock=FakeClock())
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                cli.main(["realrun", "--backbones", "stub,stub-lenient", "--fixture", "size=tiny,version=3", "--out", out,
                          "--lexical-stub-embedder", "--embedder", NOMIC, "--dry-run"])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not allowed with argument", err.getvalue())

    def test_the_stub_flag_alone_still_refuses_rag_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(rr.GateRunRefused):
                rr.realrun(cfg_for(out, substrates=["rag"], embedder_is_lexical_stub=False), lambda s, n: None,
                           FakeLock(FakeClock()), clock=FakeClock())
            with self.assertRaises(ValueError):
                rr.realrun(emb_cfg(out, embedder="ollama:nomic-embed-text"), lambda s, n: None, FakeLock(FakeClock()),
                           clock=FakeClock(), embedder_factory=lambda spec: FakeEmbedder())


class TestCliDryRunEmbed(unittest.TestCase):
    def test_dry_run_reports_the_precompute_and_calls_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as out, mock.patch("urllib.request.urlopen", side_effect=AssertionError("called")), \
                mock.patch.object(em.OllamaEmbedder, "__init__", side_effect=AssertionError("constructed")):
            out_s = io.StringIO()
            with contextlib.redirect_stdout(out_s):
                rc = cli.main(["realrun", "--backbones", "ollama:qwen2.5:14b:qwen2,ollama:llama3.1:8b:llama",
                               "--substrate", "rag,amnesiac", "--fixture", "size=tiny,version=3", "--seeds", "1,2",
                               "--embedder", NOMIC, "--embed-batch", "8", "--out", out, "--dry-run"])
            self.assertEqual(rc, 0)
            fx = [generate_by_version(s, "tiny", 3) for s in (1, 2)]
            n = len(enumerate_texts(fx[0]) | enumerate_texts(fx[1]))
            self.assertIn(f"embed precompute {NOMIC}: {n} texts (0 cached, {n} to embed)", out_s.getvalue())
            self.assertRegex(out_s.getvalue(), r"estimated embed sessions: \d+")
            self.assertEqual(os.listdir(out), [])

    def test_the_cli_builds_the_ollama_embedder_from_the_spec(self) -> None:
        seen = {}

        def fake_realrun(cfg, build, lock, log, embedder_factory):
            seen["cfg"], seen["emb"] = cfg, embedder_factory(cfg.embedder)
            return {}

        with tempfile.TemporaryDirectory() as out, mock.patch.object(cli, "realrun", fake_realrun), \
                mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["realrun", "--backbones", "stub,stub-lenient", "--fixture", "size=tiny,version=3", "--out", out,
                           "--embedder", NOMIC, "--call-timeout", "33"])
        self.assertEqual(rc, 0)
        self.assertEqual((seen["emb"].name, seen["emb"].timeout, seen["cfg"].embedder), (NOMIC, 33.0, NOMIC))

    def test_an_embedder_error_is_a_clean_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as out, mock.patch.object(cli, "realrun", side_effect=em.EmbeddingCacheMiss("miss k")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["realrun", "--backbones", "stub,stub-lenient", "--fixture", "size=tiny,version=3",
                               "--out", out, "--embedder", NOMIC])
        self.assertEqual(rc, 2)
        self.assertIn("refused: miss k", err.getvalue())


if __name__ == "__main__":
    unittest.main()
