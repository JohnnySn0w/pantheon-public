#!/usr/bin/env python3
"""ORG-062 tier-1 EPISODIC (warm) view over the governed memory store (T-2518 step 2d).

A purpose-bounded view that returns SENTENCE EXTRACTS with backlinks instead of
whole episodes, so a caller gets the same evidence for fewer tokens (ORG-062 s2.1:
each compressed memory keeps a backward link to its source; ORG-058 s5:
purpose-bounded, context-frugal retrieval). It is built OVER the store: it uses the
store's own lookups, ``evaluate_retrieval`` and ``read_payload``, and re-implements
none of them.

Pipeline (a pure function of store, trust policy, request, query, budget, top_k):

1. hot candidates: the store's ``lookup_content`` / ``lookup_subject`` hits, scored
   per distinct query term. Both lookups re-filter against the retrievable set, so
   a forgotten or superseded version can never be a candidate.
2. AUTHORIZE the candidate envelopes with ``evaluate_retrieval`` (INV-6). Only its
   ``view.items`` go on; no payload of an unauthorized envelope is ever read.
3. rank the authorized episodes by candidate score.
4. extract: ``read_payload`` per authorized episode in rank order, split into
   sentences, score each sentence by how many distinct query terms it contains. Every
   sentence scoring >= 1 is a candidate extract. Model-free, deterministic, and it
   knows nothing about any test vocabulary.
5. budget: candidates fill ``budget_tokens`` of CONTENT ordered by score descending,
   then episode rank, then source order; an extract that would overshoot is skipped,
   so the total never exceeds it. ``top_k`` counts episodes that return an extract.
6. output: each ``Extract`` carries a backlink (memory_id, version_id, sentence
   index) and the source's ``use_disposition``. The list is in READING order: the
   ranking of step 5 REVERSED, so the best extract comes LAST, adjacent to the
   question in a reader's prompt. Selection (what survives the budget) is unchanged.
   Why (T-4937): on PMB-1 v3, qwen2.5:14b answered UNKNOWN on canonical probes whose
   gold sentence led a 12-22 extract list; best-last fixed it on held-out dev seeds
   with paraphrase accuracy unchanged. A caller that wants best-first reverses it.

OPTIONAL SEMANTIC LEG (T-2518 semleg). ``embedder=None`` (the default) is the lexical pipeline above,
byte-identical. With an injected embedder (any object with ``embed_query`` / ``embed_document``; see
``Embedder``; this module never imports one):

S1. AUTHORIZE the FULL ``retrievable_envelopes()`` set with ``evaluate_retrieval`` FIRST (INV-6; superseded and
    forgotten versions are not in it). Only then ``read_payload`` the AUTHORIZED items and embed their sentences.
    An unauthorized, superseded or forgotten payload is never read and never embedded. A view that the request's
    item/token budget truncated would silently shrink the semantic candidate set, so it raises instead.
S2. episode ranking = TRUE TOP-N reciprocal-rank fusion (``RRF_K`` = 60, fixed) of two lists over the authorized
    items: the lexical list (step 1's score, items scoring >= 1) and the semantic list (every authorized item with
    a payload, by the max cosine of its sentences to the query) TRUNCATED to its top ``sem_k``. That one truncated
    list feeds BOTH the candidate set and the fused score: the candidates are the lexical items UNION the semantic
    top ``sem_k``, and an item's fused score is the sum, over the lists it appears in, of 1 / (60 + rank), rank
    1-based. A lexical item outside the semantic top ``sem_k`` gets its one lexical term only; a semantic-only item
    at semantic rank b gets 1 / (60 + b). Ties break on (memory_id, version_id). Why (T-2518 epx): full-list
    fusion gave every lexical item a second term from its tail semantic rank, burying a deep-lexical /
    top-semantic gold episode below the ``top_k`` break (17 of 17 DEV paraphrase misses), so ``sem_k`` was inert.
    With sem_k <= top_k, an item in both lists that is in the semantic top sem_k is guaranteed a fused rank
    <= sem_k, so it is visited before the top_k break (only the at most sem_k items holding two terms can outrank
    it, and two terms beat any single 1/61 term while the lexical rank stays below ~930 at sem_k 5). DEFAULT_SEM_K assumes
    top_k = 5 (sem_k >= ~2x top_k reverts to full-list burial).
S3. a sentence is a candidate when its distinct-term overlap is >= 1 OR its cosine is >= ``tau``; candidates are
    ordered by (-(overlap + cosine), fused episode rank, source order). Steps 5 and 6 (budget fill, ``top_k``
    episode counting, best-LAST reading order) are unchanged.

``DEFAULT_TAU`` / ``DEFAULT_SEM_K`` are chosen on DEV seeds only (PMB-1 v3 small seeds 1-9), never on the gate
seeds 10-27.

DEFERRED (not built): a precomputed extract projection in the store. Query-time
extraction needs no store change. Re-evaluate trigger: the tier's retrieve
wall-clock in the PMB-1 cost counters exceeds governed's by more than 3x on
the medium fixture.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol, Sequence

import memory_policy_evaluator as ev

_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Identical to the reference caller's stoplist (tools/pmb1/governed.py ``_STOP``): step 1 scores candidates "as
# the reference caller does" (T-2518 s2d brief 2.2.1). A stronger stoplist would improve governed too, so it is a
# separate decision; a test pins the two sets equal.
_STOP = frozenset({"what", "is", "s", "the", "a", "an", "of", "tell", "me", "about", "who", "which", "does", "do"})

# Semantic leg (T-2518 semleg, epx). RRF_K is fixed, not tuned. DEFAULT_TAU / DEFAULT_SEM_K were chosen by the
# pre-registered T-2518 epx phase-C DEV sweep (tools/pmb1/sweeps/epx_dev_sweep.py; PMB-1 v3 small seeds 1-9, stub
# backbone, nomic-embed-text@0a109f422b47 replayed from cache; never the gate seeds 10-27) over true top-N RRF:
# tau in {0.60..0.80} x sem_k in {3,5,10,20}. No cell leaked or served stale evidence and all kept canonical recall
# 1.000; paraphrase recall 1.000 only at sem_k 5 (sem_k 3: 0.994; sem_k 10/20: 0.901, the pre-epx level); lowest E3
# mean_tokens at tau 0.75 and 0.80 (tied), then the higher tau. tau 0.80 sits on the grid's upper edge (reported,
# not extended). They never affect the default lexical path (embedder=None). With sem_k <= top_k, an item in both
# lists that is in the semantic top sem_k is guaranteed a fused rank <= sem_k, so it is visited before the top_k
# break; DEFAULT_SEM_K assumes top_k = 5 (sem_k >= ~2x top_k reverts to full-list burial; see S2).
RRF_K = 60
DEFAULT_TAU = 0.80
DEFAULT_SEM_K = 5
# evaluate_retrieval's budget-cut counters: a semantic view must never carry one (S1).
_BUDGET_CUTS = ("item_budget_truncated", "token_budget_truncated")


class Embedder(Protocol):
    """What the semantic leg needs from an embedder (duck-typed; nothing is imported)."""

    def embed_query(self, text: str) -> list[float]: ...

    def embed_document(self, text: str) -> list[float]: ...


class SemanticViewTruncated(ValueError):
    """The authorization view of the full retrievable set was cut by the request's item/token budget, which would
    silently shrink the semantic candidate set. With an embedder, pass item_budget=0, token_budget=0 (unbounded)."""


def count_tokens(text: str) -> int:
    """Whitespace-delimited words: the same unit the PMB-1 harness meters in
    (pinned equal to ``tools.pmb1.tokens.count_tokens`` by a test)."""
    return len(text.split())


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def query_terms(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for t in words(query) if t not in _STOP))


def split_sentences(payload: str) -> list[str]:
    """Non-empty sentences, each a verbatim substring of ``payload``."""
    return [s for s in (p.strip() for p in _SENTENCE_END.split(payload)) if s]


@dataclass(frozen=True)
class Extract:
    text: str
    memory_id: str
    version_id: str
    sentence_index: int
    use_disposition: str

    @property
    def backlink(self) -> tuple[str, str, int]:
        return (self.memory_id, self.version_id, self.sentence_index)


def _sentence_scores(sentences: list[str], terms: tuple[str, ...]) -> list[int]:
    return [len(set(words(s)) & set(terms)) for s in sentences]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 when either vector is zero."""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def retrieve_extracts(
    store: Any, trust_policy: Any, request: Any, query: str, *, budget_tokens: int, top_k: int,
    embedder: Embedder | None = None, tau: float | None = None, sem_k: int | None = None,
) -> list[Extract]:
    if embedder is not None:
        return _retrieve_semantic(
            store, trust_policy, request, query, budget_tokens=budget_tokens, top_k=top_k, embedder=embedder,
            tau=DEFAULT_TAU if tau is None else tau, sem_k=DEFAULT_SEM_K if sem_k is None else sem_k,
        )
    terms = query_terms(query)
    if not terms or budget_tokens <= 0 or top_k <= 0:
        return []
    score: Counter = Counter()
    for tok in terms:
        for hit in store.lookup_content(tok):
            score[hit] += 1
        for hit in store.lookup_subject(tok):
            score[hit] += 1
    if not score:
        return []
    live = {(e.memory_id, e.version_id): e for e in store.retrievable_envelopes()}
    cands = [live[h] for h in score if h in live]
    view = ev.evaluate_retrieval(request, cands, trust_policy)  # authorization BEFORE ranking and BEFORE any read
    ranked = sorted(view.items, key=lambda i: (-score[(i.memory_id, i.version_id)], i.memory_id, i.version_id))
    # (-sentence score, episode rank, source order) -> extract; filled in that order under the content budget.
    pool: list[tuple[int, int, int, Extract]] = []
    returned: list[Extract] = []
    for rank, it in enumerate(ranked):
        payload = store.read_payload(it.memory_id, it.version_id)
        if payload is None:
            continue
        sentences = split_sentences(payload)
        for idx, (text, sc) in enumerate(zip(sentences, _sentence_scores(sentences, terms))):
            if sc >= 1:
                pool.append((-sc, rank, idx, Extract(text, it.memory_id, it.version_id, idx, it.use_disposition.value)))
        returned = _fill(pool, budget_tokens)
        # top_k counts episodes that actually RETURN something, as the reference caller counts returned items
        if len({x.backlink[:2] for x in returned}) >= top_k:
            break
    return returned[::-1]  # reading order: best LAST (docstring step 6, T-4937)


def rrf_scores(*lists: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Fraction]:
    """Reciprocal-rank fusion: sum over the lists an item appears in of 1 / (RRF_K + rank), rank 1-based. Exact
    arithmetic, so a tie is a real tie and never a float-rounding artefact."""
    fused: dict[tuple[str, str], Fraction] = {}
    for lst in lists:
        for r, k in enumerate(lst, start=1):
            fused[k] = fused.get(k, Fraction(0)) + Fraction(1, RRF_K + r)
    return fused


def fuse_ranks(lex_list: Sequence[tuple[str, str]], sem_list: Sequence[tuple[str, str]], sem_k: int) -> list[tuple[str, str]]:
    """S2, true top-N RRF: ``sem_list`` is truncated to its top ``sem_k`` BEFORE fusion, and that truncated list feeds
    both the candidate set (``lex_list`` UNION it) and the fused score, sorted by fused score descending, ties on
    (memory_id, version_id). A lexical item outside the semantic top ``sem_k`` gets its lexical term only. (Fusing
    the FULL semantic list gave every lexical item a second term from its tail semantic rank, burying a
    deep-lexical / top-semantic gold episode; T-2518 epx.)"""
    top = list(sem_list[:max(sem_k, 0)])
    fused = rrf_scores(lex_list, top)
    return sorted(set(lex_list) | set(top), key=lambda k: (-fused[k], k))


def _retrieve_semantic(
    store: Any, trust_policy: Any, request: Any, query: str, *, budget_tokens: int, top_k: int,
    embedder: Embedder, tau: float, sem_k: int,
) -> list[Extract]:
    """Docstring S1-S3. Authorization of the FULL retrievable set precedes every read and every embed."""
    if not query.strip() or budget_tokens <= 0 or top_k <= 0:
        return []
    terms = query_terms(query)
    score: Counter = Counter()
    for tok in terms:  # step 1, unchanged: the lexical list's score
        for hit in store.lookup_content(tok):
            score[hit] += 1
        for hit in store.lookup_subject(tok):
            score[hit] += 1
    live = sorted(store.retrievable_envelopes(), key=lambda e: (e.memory_id, e.version_id))
    view = ev.evaluate_retrieval(request, live, trust_policy)  # S1: authorization BEFORE any read or embed (INV-6)
    cut = {k: view.omitted_reason_counts[k] for k in _BUDGET_CUTS if view.omitted_reason_counts.get(k)}
    if cut:
        raise SemanticViewTruncated(f"the authorization view was budget-truncated {cut}; the semantic leg needs "
                                    "an unbounded view (item_budget=0, token_budget=0)")
    if not view.items:
        return []
    qv = embedder.embed_query(query)
    # Only AUTHORIZED items are read and embedded, each once per retrieve: key -> (item, sentences, overlap, cosine).
    sents: dict[tuple[str, str], tuple[Any, list[str], list[int], list[float]]] = {}
    for it in view.items:
        payload = store.read_payload(it.memory_id, it.version_id)
        if payload is None:
            continue
        ss = split_sentences(payload)
        if ss:
            sents[(it.memory_id, it.version_id)] = (
                it, ss, _sentence_scores(ss, terms), [cosine(qv, embedder.embed_document(x)) for x in ss])
    lex_list = sorted((k for k in sents if score[k] >= 1), key=lambda k: (-score[k], k))
    sem_list = sorted(sents, key=lambda k: (-max(sents[k][3]), k))
    ranked = fuse_ranks(lex_list, sem_list, sem_k)
    pool: list[tuple[float, int, int, Extract]] = []
    returned: list[Extract] = []
    for rank, k in enumerate(ranked):
        it, ss, lex, cos = sents[k]
        for idx, (text, sc, c) in enumerate(zip(ss, lex, cos)):
            if sc >= 1 or c >= tau:
                pool.append((-(sc + c), rank, idx, Extract(text, it.memory_id, it.version_id, idx, it.use_disposition.value)))
        returned = _fill(pool, budget_tokens)
        if len({x.backlink[:2] for x in returned}) >= top_k:  # top_k counts episodes that return something
            break
    return returned[::-1]  # reading order: best LAST (docstring step 6, T-4937)


def _fill(pool: list[tuple[Any, int, int, Extract]], budget_tokens: int) -> list[Extract]:
    out: list[Extract] = []
    used = 0
    for _, _, _, x in sorted(pool, key=lambda t: t[:3]):
        n = count_tokens(x.text)
        if used + n > budget_tokens:
            continue
        out.append(x)
        used += n
    return out
