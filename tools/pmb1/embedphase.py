"""The RAG embed precompute (T-2518 step 2g-2): enumerate every text the RAG substrate will embed for a fixture,
then fill an ``EmbeddingCache`` through the real embedder in batches, OUTSIDE any backbone session.

Enumeration runs every track over the RAG substrate with a ``RecordingEmbedder`` and a stub backbone. That is
sound only because the embedded texts do not depend on the backbone's replies (the tracks write fixture text
and query with fixture queries); ``test_pmb1_embedder`` proves it under two differently-answering backbones
across the gate seeds, and a replay-only run fails closed (``EmbeddingCacheMiss``) if that ever stops holding.
"""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

from .backbone import StubBackbone
from .embedder import Embedder, EmbeddingCache, HashingEmbedder, RecordingEmbedder, cache_key, missing
from .runner import make_substrate, run_one

DEFAULT_BATCH = 32


def enumerate_texts(fx: object, *, backbone_factory: Callable[[], object] | None = None,
                    inner: Embedder | None = None, substrate: str = "rag") -> set[tuple[str, str]]:
    """Every (kind, text) the ``substrate`` embeds over all of the fixture's tracks."""
    rec = RecordingEmbedder(inner or HashingEmbedder())
    bb = backbone_factory() if backbone_factory else StubBackbone("strict")
    run_one(lambda: make_substrate(substrate, fx, rec), bb, fx, gate=False,  # type: ignore[arg-type]
            embedder_is_lexical_stub=True)
    return rec.seen


def enumerate_many(fixtures: Iterable[object]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for fx in fixtures:
        out |= enumerate_texts(fx)
    return out


def batches(todo: Sequence[tuple[str, str]], size: int) -> list[tuple[str, list[str]]]:
    """Group by kind (one request carries one prefix), then cut into batches of at most ``size``."""
    if size < 1:
        raise ValueError("batch size must be >= 1")
    out: list[tuple[str, list[str]]] = []
    for kind in sorted({k for k, _ in todo}):
        texts = [t for k, t in todo if k == kind]
        out += [(kind, texts[i:i + size]) for i in range(0, len(texts), size)]
    return out


def precompute(inner: Embedder, cache: EmbeddingCache, texts: Iterable[tuple[str, str]], *, batch_size: int = DEFAULT_BATCH,
               before_batch: Callable[[int, int], bool] | None = None,
               after_embed: Callable[[int], None] | None = None) -> dict:
    """Fill ``cache`` with every missing (kind, text) through ``inner``, one atomic shard per batch. Resumable:
    keys already cached are skipped. ``before_batch(i, n)`` (batch index, batch size) returning False stops early (the session budget); the
    rest stays missing for the next call. ``after_embed(i)`` runs after batch i is embedded and BEFORE it is
    written (the driver verifies the loaded digest there), so a raise leaves that batch out of the cache. Returns {embedded, skipped, remaining, requests}."""
    if cache.embedder_name != inner.name:
        raise ValueError(f"cache is for {cache.embedder_name!r}, the embedder is {inner.name!r}")
    texts = set(texts)
    todo = missing(cache, texts)
    skipped = len(texts) - len(todo)
    done = requests = 0
    for i, (kind, chunk) in enumerate(batches(todo, batch_size)):
        if before_batch is not None and not before_batch(i, len(chunk)):
            break
        vecs = inner.embed_many(chunk, kind)
        requests += 1
        if len(vecs) != len(chunk):
            raise RuntimeError(f"{inner.name} returned {len(vecs)} vectors for {len(chunk)} texts")
        if after_embed is not None:
            after_embed(i)
        cache.add({cache_key(inner.name, kind, t): v for t, v in zip(chunk, vecs)})
        done += len(chunk)
    return {"embedded": done, "skipped": skipped, "remaining": len(todo) - done, "requests": requests}
