"""The substrate adapter interface (the thing under test) + the controls.

Cost counters (rule 6.1.5) live in the base class so no adapter can forget
them: every ``write`` / ``supersede`` / ``forget`` is timed and counted, and
``write_llm_calls`` is always reported (0 for v1's adapters: none writes
through a model).
"""
from __future__ import annotations

import hashlib
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .embedder import Embedder, HashingEmbedder, cosine
from .tokens import count_tokens

CHUNK_WORDS = 64
DEFAULT_TOP_K = 5

# describe()["role"] values: how the gate tells candidate from controls.
ROLE_CANDIDATE = "candidate"
ROLE_RAG = "baseline-rag"
ROLE_RANDOM = "control-random"
ROLE_FULL = "ceiling-full-context"
ROLE_FLOOR = "floor-amnesiac"


@dataclass(frozen=True)
class Item:
    key: str
    subject: str
    text: str
    privacy_class: str = "internal"  # only substrates with a privacy lattice use it
    valid_from: str | None = None  # T-2518 t2: when the item became true in the world (None: the substrate's default)


@dataclass(frozen=True)
class Evidence:
    text: str
    ref: str  # backlink to the source write (opaque, substrate-issued)
    disposition: str = ""  # policy use-disposition when the substrate has one (e.g. "allowed", "quarantined")
    # T-2518 t2: every source ref this unit was derived from (a relation joined over two episodes names both).
    # The leak and stale checks test {ref} | support, so a derived unit cannot launder a bad source behind a good ref.
    support: tuple[str, ...] = ()


@dataclass
class Cost:
    write_calls: int = 0
    supersede_calls: int = 0
    forget_calls: int = 0
    retrieve_calls: int = 0
    write_llm_calls: int = 0
    write_dollars: float = 0.0  # 0.0 for stub/local backbones; rule 6.1.5 wants money, not just accuracy
    write_seconds: float = 0.0
    retrieve_seconds: float = 0.0
    tokens_returned: int = 0

    def to_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


class Substrate(ABC):
    name: str
    role: str
    version: str = "v1"
    embedding_model: str | None = None
    over: str | None = None  # a tier declares what it sits over (ORG-062 s1-2), e.g. "governed"
    # T-2518 t2 REBOUND 3: words a substrate's output template adds that no payload holds (e.g. a "Note:" label).
    # Declared, at most 8, never a fixture word (the tier-2 gate refuses otherwise), listed in the verdict's notes.
    template_words: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.cost = Cost()

    # --- adapter surface (counted + timed here, implemented in _x) ----------
    def write(self, item: Item) -> str:
        t = time.perf_counter()
        ref = self._write(item)
        self.cost.write_calls += 1
        self.cost.write_seconds += time.perf_counter() - t
        return ref

    def supersede(self, ref: str, new_item: Item) -> str:
        t = time.perf_counter()
        new = self._supersede(ref, new_item)
        self.cost.supersede_calls += 1
        self.cost.write_seconds += time.perf_counter() - t
        return new

    def forget(self, ref: str) -> None:
        t = time.perf_counter()
        self._forget(ref)
        self.cost.forget_calls += 1
        self.cost.write_seconds += time.perf_counter() - t

    def retrieve(self, query: str, purpose: str, budget_tokens: int, *, as_of: str | None = None) -> list[Evidence]:
        """``as_of`` (T-2518 t2, R7) is a STRUCTURED time: "what held at this instant". It is never text in the
        query. ``None`` (every v1-v3 track) is today's call, unchanged; a substrate with no notion of valid time
        inherits ``_retrieve_as_of``, which ignores it."""
        t = time.perf_counter()
        out = (self._retrieve(query, purpose, budget_tokens) if as_of is None
               else self._retrieve_as_of(query, purpose, budget_tokens, as_of))
        self.cost.retrieve_calls += 1
        self.cost.retrieve_seconds += time.perf_counter() - t
        self.cost.tokens_returned += sum(count_tokens(e.text) for e in out)
        return out

    @abstractmethod
    def _write(self, item: Item) -> str: ...
    @abstractmethod
    def _supersede(self, ref: str, new_item: Item) -> str: ...
    @abstractmethod
    def _forget(self, ref: str) -> None: ...
    @abstractmethod
    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]: ...

    def _retrieve_as_of(self, query: str, purpose: str, budget_tokens: int, as_of: str) -> list[Evidence]:
        """Default: no valid-time model, so the as-of view is simply the current one."""
        return self._retrieve(query, purpose, budget_tokens)

    def audit(self, ref: str) -> dict | None:
        """C5 lineage for one returned unit's ref (T-2518 t2): ids and times only, never payload text.
        ``None`` = this substrate keeps no audit trail (the default)."""
        return None

    def lineage_store(self):
        """The governed store this substrate's audit() claims resolve in (T-2518 t2 REBOUND 3), or ``None``. C5
        verifies every audited version against it; at tier 2 a candidate returning ``None`` fails C5 (closed)."""
        return None

    def describe(self) -> dict:
        return {
            "name": self.name, "role": self.role, "version": self.version,
            "embedding_model": self.embedding_model, "cost": self.cost.to_dict(),
            **({"over": self.over} if self.over else {}),
            **self._extra(),
        }

    def _extra(self) -> dict:
        return {}

    def close(self) -> None:  # tempdir-backed adapters override
        return None


class _ListSubstrate(Substrate):
    """Shared plumbing for the in-memory baselines/controls: refs are
    ``r<n>``; supersede replaces, forget removes."""

    def __init__(self) -> None:
        super().__init__()
        self._n = 0
        self._items: dict[str, Item] = {}

    def _write(self, item: Item) -> str:
        self._n += 1
        ref = f"r{self._n}"
        self._items[ref] = item
        self._on_add(ref, item)
        return ref

    def _supersede(self, ref: str, new_item: Item) -> str:
        self._forget(ref)
        return self._write(new_item)

    def _forget(self, ref: str) -> None:
        self._items.pop(ref, None)
        self._on_remove(ref)

    def _on_add(self, ref: str, item: Item) -> None: ...
    def _on_remove(self, ref: str) -> None: ...


def _within_budget(cands: list[Evidence], budget_tokens: int, top_k: int) -> list[Evidence]:
    out: list[Evidence] = []
    used = 0
    for e in cands:
        n = count_tokens(e.text)
        if used + n > budget_tokens:
            continue
        out.append(e)
        used += n
        if len(out) >= top_k:
            break
    return out


def chunk(text: str, size: int = CHUNK_WORDS) -> list[str]:
    w = text.split()
    return [" ".join(w[i:i + size]) for i in range(0, len(w), size)] or [""]


class VerbatimRAGBaseline(_ListSubstrate):
    """Rule 6.1.1: verbatim chunks + a NAMED embedder, cosine top-k. Lifecycle
    is honoured best-effort (a vector store can delete/upsert), so the
    baseline is the strongest simple thing a tier must match."""

    name = "verbatim-rag"
    role = ROLE_RAG

    def __init__(self, embedder: Embedder | None = None, top_k: int = DEFAULT_TOP_K) -> None:
        super().__init__()
        self.embedder = embedder or HashingEmbedder()
        self.embedding_model = self.embedder.name
        self.top_k = top_k
        self._chunks: dict[str, list[tuple[str, list[float]]]] = {}

    def _on_add(self, ref: str, item: Item) -> None:
        self._chunks[ref] = [(c, self.embedder.embed_document(c)) for c in chunk(item.text)]

    def _on_remove(self, ref: str) -> None:
        self._chunks.pop(ref, None)

    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        q = self.embedder.embed_query(query)
        scored = [
            (cosine(q, vec), order, Evidence(text, ref))
            for order, (ref, cs) in enumerate(self._chunks.items())
            for text, vec in cs
        ]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], s[1]))
        return _within_budget([s[2] for s in scored], budget_tokens, self.top_k)


class RandomRetrievalControl(_ListSubstrate):
    """Rule 6.1.2: k random stored items (seeded per query, so deterministic)."""

    name = "random-control"
    role = ROLE_RANDOM

    def __init__(self, seed: int = 0, top_k: int = DEFAULT_TOP_K) -> None:
        super().__init__()
        self.seed, self.top_k = seed, top_k

    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        h = hashlib.sha256(f"{self.seed}/{query}".encode()).digest()
        rng = random.Random(int.from_bytes(h[:8], "big"))
        refs = list(self._items)
        rng.shuffle(refs)
        cands = [Evidence(self._items[r].text, r) for r in refs]
        return _within_budget(cands, budget_tokens, self.top_k)


class FullContextCeiling(_ListSubstrate):
    """Rule 6.1.3: returns everything, budget ignored. Its token count is the
    cost of "just dump the archive"."""

    name = "full-context-ceiling"
    role = ROLE_FULL

    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        return [Evidence(i.text, r) for r, i in self._items.items()]


class AmnesiacFloor(Substrate):
    """Rule 6.1.7: stores nothing, retrieves nothing. Mandatory on every
    Forget-class metric. Hands out refs so the protocol runs unchanged."""

    name = "amnesiac-floor"
    role = ROLE_FLOOR

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    def _write(self, item: Item) -> str:
        self._n += 1
        return f"a{self._n}"

    def _supersede(self, ref: str, new_item: Item) -> str:
        return self._write(new_item)

    def _forget(self, ref: str) -> None:
        return None

    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        return []
