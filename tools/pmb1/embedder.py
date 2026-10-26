"""Embedder interface behind the verbatim-RAG baseline (rule 6.1.1).

v1 ships a deterministic hashing embedder for tests; a real run records the
NAMED model it used, so the record says which embedder produced the numbers.

Step 2g-2 (T-2518) adds the real local embedder, ``OllamaEmbedder`` (nomic-embed-text on VM210), and the
on-disk ``CachedEmbedder`` the realrun driver replays from: the embedder never loads inside a backbone lock
session. Its texts are embedded in a precompute phase of their own (``precompute``), then the backbone units
run against the cache in replay-only mode (a miss raises ``EmbeddingCacheMiss``: fail closed).

Ollama ``POST /api/embed`` (captured 2026-10-13, Ollama 0.34.2, nomic-embed-text digest 0a109f422b47):
``{"model", "input": str | [str], "keep_alive", "truncate"}`` -> 200 ``{"model", "embeddings": [[768 floats]...],
"total_duration", "load_duration", "prompt_eval_count"}``, one vector per input, already L2-normalised. With
``"truncate": false`` an input over the context gives HTTP 400 ``{"error": "the input length exceeds the context
length"}``; the default (true) truncates SILENTLY, so ``truncate: false`` is always sent.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Sequence

from .tokens import words

DOCUMENT, QUERY = "document", "query"
KINDS = (DOCUMENT, QUERY)
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "
# Captured output dimension per model (the cache header's dim is checked against it when known).
KNOWN_DIMS = {"nomic-embed-text": 768}
EMBED_UNLOAD_TIMEOUT_S = 10.0
PS_TIMEOUT_S = 10.0
# A model in KNOWN_DIMS returns L2-normalised vectors (captured: norm 1.0); anything further off is degenerate.
NORM_TOLERANCE = 1e-3
_DIGEST = re.compile(r"^[0-9a-f]{12}$")


class Embedder(ABC):
    name: str

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_document(self, text: str) -> list[float]:
        """A stored text (a chunk). Default: ``embed``; a model with task prefixes overrides it."""
        return self.embed(text)

    def embed_query(self, text: str) -> list[float]:
        """A retrieval query. Default: ``embed``; a model with task prefixes overrides it."""
        return self.embed(text)

    def embed_many(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        """One vector per text, of ``kind`` (``document`` | ``query``). Default: one call each."""
        fn = {DOCUMENT: self.embed_document, QUERY: self.embed_query}[kind]
        return [fn(t) for t in texts]


class HashingEmbedder(Embedder):
    """Signed feature hashing of lowercase word tokens, L2-normalised."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}-v1"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in words(text):
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            n = int.from_bytes(h, "big")
            vec[n % self.dim] += 1.0 if (n >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def is_lexical_stub(embedding_model: str | None) -> bool:
    """True for the hashing stub: lexical overlap only, not a semantic embedder."""
    return bool(embedding_model) and embedding_model.startswith("hashing-")


# ---------------------------------------------------------------------------------------------------- errors

class EmbedderError(RuntimeError):
    """A real embedder call failed or answered something unusable. Never a zero vector, never a fallback."""


class EmbeddingContextExceeded(EmbedderError):
    """Ollama refused an input over the model's context (sent with ``truncate: false``)."""


class EmbeddingDimMismatch(EmbedderError):
    """A vector whose dimension differs from the one this run (or this cache) already has."""


class EmbeddingDigestMismatch(EmbedderError):
    """The weights Ollama has loaded are not the ones the embedder's name (and so every record) claims."""


class EmbeddingCacheMiss(EmbedderError):
    """Replay-only: a text that the precompute did not embed. Fail closed."""


class EmbeddingCacheMismatch(EmbedderError):
    """A cache made under another embedder name or dimension, or a corrupt cache."""


# ---------------------------------------------------------------------------------------------------- Ollama

def parse_embedder_spec(spec: str) -> tuple[str, str]:
    """``ollama:<model>@<digest>`` -> (model, digest). The digest is Ollama's 12-hex-char tag digest."""
    if not spec.startswith("ollama:") or "@" not in spec:
        raise ValueError(f"embedder spec is ollama:<model>@<12-hex digest>, got {spec!r}")
    model, _, digest = spec[len("ollama:"):].rpartition("@")
    if not model or not _DIGEST.match(digest):
        raise ValueError(f"embedder spec is ollama:<model>@<12-hex digest>, got {spec!r}")
    return model, digest


def _base_model(model: str) -> str:
    return model.split(":", 1)[0]


class OllamaEmbedder(Embedder):
    """HTTP ``/api/embed`` against a local Ollama (VM210). Base URL from env ``PMB1_OLLAMA_URL`` (or ``base_url``).
    ``digest`` (the 12-char tag digest) is required and is part of ``name`` (``ollama:<model>@<digest>``), so a
    record names the exact weights; it is not looked up here. ``truncate: false`` is sent on every request, so
    an oversize input is an error, not a silently cut vector. Every vector of one embedder must share one
    dimension (``dim``: the expected one when given, else the first seen). ``timeout_cap`` (set by the realrun
    driver to what is left of a lock session) caps each request's HTTP timeout."""

    def __init__(
        self, model: str, digest: str, *, base_url: str | None = None,
        document_prefix: str = NOMIC_DOCUMENT_PREFIX, query_prefix: str = NOMIC_QUERY_PREFIX,
        timeout: float = 120.0, keep_alive: str | int = "2m", dim: int | None = None,
    ) -> None:
        url = base_url or os.environ.get("PMB1_OLLAMA_URL", "")
        if not url:
            raise ValueError("OllamaEmbedder needs PMB1_OLLAMA_URL (or base_url)")
        if not _DIGEST.match(digest or ""):
            raise ValueError(f"OllamaEmbedder needs the 12-hex-char tag digest, got {digest!r}")
        self.base_url = url.rstrip("/")
        self.model, self.digest = model, digest
        self.name = f"ollama:{model}@{digest}"
        self.document_prefix, self.query_prefix = document_prefix, query_prefix
        self.timeout, self.keep_alive = timeout, keep_alive
        self.dim = dim if dim is not None else KNOWN_DIMS.get(_base_model(model))
        self.timeout_cap: float | None = None
        self.requests = 0

    def request_timeout(self) -> float:
        return self.timeout if self.timeout_cap is None else min(self.timeout, self.timeout_cap)

    def _post(self, path: str, payload: dict | None, timeout: float) -> dict:
        """POST ``payload`` (or GET when it is None) and return the JSON object; typed errors on anything else."""
        req = urllib.request.Request(
            self.base_url + path, data=None if payload is None else json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-set local URL)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp is not None else b""
            try:
                msg = json.loads(body).get("error", "")
            except (ValueError, AttributeError):
                msg = ""
            if e.code == 400 and "exceeds the context length" in str(msg):
                raise EmbeddingContextExceeded(f"{self.name}: {msg}") from None
            raise EmbedderError(f"{self.name}: HTTP {e.code} from {path}: {msg or body[:200]!r}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise EmbedderError(f"{self.name}: {path} answered a body that is not JSON: {raw[:200]!r}") from None
        if not isinstance(data, dict):
            raise EmbedderError(f"{self.name}: {path} answered {type(data).__name__}, not an object")
        return data

    def _raw_many(self, inputs: list[str]) -> list[list[float]]:
        """POST the (already prefixed) inputs as one list; one validated vector each."""
        if not inputs:
            raise ValueError("never send an empty input list to /api/embed")
        self.requests += 1
        data = self._post("/api/embed", {"model": self.model, "input": inputs, "keep_alive": self.keep_alive,
                                         "truncate": False}, self.request_timeout())
        return self.parse_embeddings(data, len(inputs))

    def parse_embeddings(self, data: dict, n: int) -> list[list[float]]:
        if "error" in data:
            raise EmbedderError(f"{self.name}: /api/embed error: {data['error']}")
        embs = data.get("embeddings")
        if not isinstance(embs, list) or len(embs) != n:
            got = len(embs) if isinstance(embs, list) else type(embs).__name__
            raise EmbedderError(f"{self.name}: /api/embed answered {got} embeddings for {n} inputs")
        out: list[list[float]] = []
        for i, v in enumerate(embs):
            if not isinstance(v, list) or not v or not all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v):
                raise EmbedderError(f"{self.name}: embedding {i} is not a non-empty list of finite numbers")
            if self.dim is None:
                self.dim = len(v)
            elif len(v) != self.dim:
                raise EmbeddingDimMismatch(f"{self.name}: embedding {i} has dim {len(v)}, this run has {self.dim}")
            if _base_model(self.model) in KNOWN_DIMS:   # a known model normalises: anything else is degenerate
                norm = math.sqrt(sum(float(x) * float(x) for x in v))
                if abs(norm - 1.0) >= NORM_TOLERANCE:
                    raise EmbedderError(f"{self.name}: embedding {i} has L2 norm {norm:.6f}, expected 1 "
                                        f"(+/- {NORM_TOLERANCE}): a degenerate vector")
            out.append([float(x) for x in v])
        return out

    def embed(self, text: str) -> list[float]:
        """The bare text, no task prefix (the RAG baseline uses ``embed_document`` / ``embed_query``)."""
        return self._raw_many([text])[0]

    def prefixed(self, text: str, kind: str) -> str:
        return {DOCUMENT: self.document_prefix, QUERY: self.query_prefix}[kind] + text

    def embed_document(self, text: str) -> list[float]:
        return self.embed_many([text], DOCUMENT)[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_many([text], QUERY)[0]

    def embed_many(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        return self._raw_many([self.prefixed(t, kind) for t in texts])

    def _ps_matches(self, entry: dict) -> bool:
        want = {self.model} | ({self.model + ":latest"} if ":" not in self.model else set())
        return entry.get("model") in want or entry.get("name") in want

    def verify_digest(self) -> str:
        """GET /api/ps and require this model's loaded ``digest`` to start with ``self.digest`` (Ollama lists the
        full sha256; ``name`` / ``model`` carry an implicit ``:latest`` tag). A mismatch or a missing entry raises
        ``EmbeddingDigestMismatch``: the record would otherwise name weights that did not make its vectors.
        Returns the full digest."""
        data = self._post("/api/ps", None, PS_TIMEOUT_S)
        models = data.get("models")
        if not isinstance(models, list):
            raise EmbeddingDigestMismatch(f"{self.name}: /api/ps answered no models list")
        hits = [m for m in models if isinstance(m, dict) and self._ps_matches(m)]
        if not hits:
            raise EmbeddingDigestMismatch(f"{self.name}: /api/ps lists no loaded entry for {self.model!r} "
                                          f"(loaded: {[m.get('name') for m in models if isinstance(m, dict)]})")
        for m in hits:
            d = m.get("digest")
            if not isinstance(d, str) or not d.startswith(self.digest):
                raise EmbeddingDigestMismatch(f"{self.name}: Ollama has {self.model!r} loaded with digest {d!r}, "
                                              f"the embedder claims {self.digest!r}")
        return str(hits[0]["digest"])

    def unload(self) -> None:
        """Evict the model now: POST /api/generate {model, keep_alive: 0}, as ``OllamaBackbone.unload`` does.
        VERIFIED for the embedding model on Ollama 0.34.2, 2026-10-13 (nomic-embed-text): HTTP 200
        ``{"done_reason": "unload"}`` and ``/api/ps`` empty afterwards (~/archivist-work/t2518-2g2-captures/
        unload-capture.txt). A non-200 raises, so a refusal is loud (``UnloadFailed`` in the driver)."""
        self._post("/api/generate", {"model": self.model, "keep_alive": 0}, EMBED_UNLOAD_TIMEOUT_S)


# ---------------------------------------------------------------------------------------------------- cache

CACHE_FORMAT = "pmb1-embed-cache/1"
HEADER = "header.json"


def cache_key(embedder_name: str, kind: str, text: str) -> str:
    """Content address: sha256 of (embedder name, kind, text)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return hashlib.sha256(json.dumps([embedder_name, kind, text], ensure_ascii=False).encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class EmbeddingCache:
    """A directory: ``header.json`` ({format, embedder, dim}) plus ``shard-NNNNNN.json`` files, each one batch
    of {key: vector}, each written atomically (tmp + fsync + rename), so a kill leaves whole shards only. A
    header for another embedder name or dim, a vector of another dim, or a corrupt shard raises
    ``EmbeddingCacheMismatch`` on load."""

    def __init__(self, path: Path, embedder_name: str, dim: int | None = None) -> None:
        self.path, self.embedder_name, self.dim = Path(path), embedder_name, dim
        self.vectors: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        h = self.path / HEADER
        if not h.exists():
            if self.path.exists() and any(self.path.glob("shard-*.json")):
                raise EmbeddingCacheMismatch(f"{self.path}: shards without a {HEADER}")
            return
        try:
            head = json.loads(h.read_text(encoding="utf-8"))
        except ValueError:
            raise EmbeddingCacheMismatch(f"{self.path / HEADER} is corrupt") from None
        if not isinstance(head, dict) or head.get("format") != CACHE_FORMAT:
            raise EmbeddingCacheMismatch(f"{self.path}: not a {CACHE_FORMAT} cache")
        if head.get("embedder") != self.embedder_name:
            raise EmbeddingCacheMismatch(f"{self.path}: cache was made by embedder {head.get('embedder')!r}, "
                                         f"this run uses {self.embedder_name!r}")
        if self.dim is not None and head.get("dim") != self.dim:
            raise EmbeddingCacheMismatch(f"{self.path}: cache has dim {head.get('dim')!r}, this run expects {self.dim}")
        self.dim = head.get("dim")
        for p in sorted(self.path.glob("shard-*.json")):
            try:
                shard = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                raise EmbeddingCacheMismatch(f"{p} is corrupt") from None
            for k, v in shard.items():
                if not isinstance(v, list) or len(v) != self.dim:
                    raise EmbeddingCacheMismatch(f"{p}: vector {k} has dim {len(v) if isinstance(v, list) else '?'}, "
                                                 f"the header says {self.dim}")
                self.vectors[k] = v

    def __contains__(self, key: str) -> bool:
        return key in self.vectors

    def __len__(self) -> int:
        return len(self.vectors)

    def add(self, batch: dict[str, list[float]]) -> None:
        """Write one shard atomically (the header first, once)."""
        if not batch:
            return
        dims = {len(v) for v in batch.values()}
        if len(dims) != 1 or (self.dim is not None and dims != {self.dim}):
            raise EmbeddingDimMismatch(f"{self.path}: batch dims {sorted(dims)}, cache dim {self.dim}")
        if self.dim is None:
            self.dim = dims.pop()
        if not (self.path / HEADER).exists():
            _atomic_write_text(self.path / HEADER, json.dumps(
                {"format": CACHE_FORMAT, "embedder": self.embedder_name, "dim": self.dim}, sort_keys=True) + "\n")
        idx = [int(p.name[len("shard-"):-len(".json")]) for p in self.path.glob("shard-*.json")
               if p.name[len("shard-"):-len(".json")].isdigit()]
        n = max(idx, default=-1) + 1   # never the file count: a gap would overwrite the last shard
        _atomic_write_text(self.path / f"shard-{n:06d}.json", json.dumps(batch, sort_keys=True) + "\n")
        self.vectors.update(batch)


class CachedEmbedder(Embedder):
    """Replay-only: serves vectors from an ``EmbeddingCache`` made by the embedder named ``inner_name``; a miss
    raises ``EmbeddingCacheMiss`` naming the key. Its ``name`` is the inner embedder's, so the record names the
    real model, never the cache."""

    def __init__(self, inner_name: str, path: Path, dim: int | None = None) -> None:
        self.name = inner_name
        self.cache = EmbeddingCache(path, inner_name, dim)
        self.misses = 0

    def _get(self, kind: str, text: str) -> list[float]:
        k = cache_key(self.name, kind, text)
        v = self.cache.vectors.get(k)
        if v is None:
            self.misses += 1
            raise EmbeddingCacheMiss(f"embedding cache {self.cache.path} has no {kind} vector for key {k} "
                                     f"(text {text[:60]!r}); run the precompute for this fixture first")
        return v

    def embed(self, text: str) -> list[float]:
        raise EmbedderError("CachedEmbedder serves embed_document / embed_query only (the cache is keyed by kind)")

    def embed_document(self, text: str) -> list[float]:
        return self._get(DOCUMENT, text)

    def embed_query(self, text: str) -> list[float]:
        return self._get(QUERY, text)


class RecordingEmbedder(Embedder):
    """Wraps an embedder and records every (kind, text) it is asked to embed. The enumeration instrument."""

    def __init__(self, inner: Embedder | None = None) -> None:
        self.inner = inner or HashingEmbedder()
        self.name = self.inner.name
        self.seen: set[tuple[str, str]] = set()

    def embed(self, text: str) -> list[float]:
        raise EmbedderError("RecordingEmbedder records embed_document / embed_query only")

    def embed_document(self, text: str) -> list[float]:
        self.seen.add((DOCUMENT, text))
        return self.inner.embed_document(text)

    def embed_query(self, text: str) -> list[float]:
        self.seen.add((QUERY, text))
        return self.inner.embed_query(text)


def missing(cache: EmbeddingCache, texts: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (kind, text) pairs the cache has no vector for, in a stable order."""
    return sorted(t for t in set(texts) if cache_key(cache.embedder_name, t[0], t[1]) not in cache)
