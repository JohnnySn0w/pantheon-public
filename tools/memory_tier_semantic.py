#!/usr/bin/env python3
"""ORG-062 tier-2 SEMANTIC / RELATIONAL view over the governed memory store (T-2518 step S4).

ORG-062:116-119: "bi-temporal validity edges ... never overwrite; close-and-supersede with (valid-from, valid-to) +
ingestion time". This module is that tier as a PURE QUERY-TIME PROJECTION: a function of (store, trust policy,
request, query, as_of). Nothing derived is persisted: no new store, no projection, no cache that outlives one call.
The store's envelopes already carry the valid time (``valid_from`` / ``valid_to``) and the event log carries the
ingestion time, so "never overwrite" is the store's own append-only discipline; this view only READS it.

Pipeline (one call):

1. AUTHORIZE FIRST (INV-6, L2). ``evaluate_retrieval`` over the FULL ``retrievable_envelopes()`` set, before any
   ``read_payload``. Superseded and forgotten versions are not in that set; an above-ceiling or out-of-scope envelope
   is not in the view. Only ``view.items`` are ever read. A budget-truncated view raises (it would silently shrink the
   slot table), as the tier-1 semantic leg does.
2. SLOT TABLE. Every authorized payload is split into sentences (tier 1's ``split_sentences``: verbatim substrings)
   and each sentence goes through the pluggable ``Extractor``, which yields ``Claim(entity, predicate, value)``. A
   slot is (entity, predicate), case-insensitive. Each claim carries its envelope's ``valid_from`` / ``valid_to``.
3. VALIDITY at t (t = ``as_of``, a STRUCTURED parameter, never parsed from the query (R7); ``None`` means the
   request's own ``request_time``, i.e. "now"). Among a slot's claims with ``valid_from <= t``, the LATEST valid_from
   wins; a later valid_from closes every earlier interval (history, not current). The values at that instant whose
   envelope is not closed by its own ``valid_to <= t`` are the slot's value at t: one distinct value is current, two
   or more distinct values are a CONFLICT and every one is surfaced (never silently picked).
4. UNITS. Start from tier 1's sentence extracts (``memory_tier_episodic.retrieve_extracts``, same request). DROP an
   extract that asserts any value not valid at t. ADD, for every slot the query touches (a slot an extract asserts, or
   whose entity and predicate both appear among the query terms) any valid value the kept units do not already
   assert, as the VERBATIM source sentence (never synthesized text, so the output template adds no word:
   ``template_words = ()``). A unit's ``ref`` is its source version; its ``support`` is every OTHER version the
   claim depends on: for a conflict, every source of a different value valid at the same instant. SELECTION
   dependencies are NOT listed in ``support``: an added unit selected because some extract touched its slot does not
   name that extract's source. MEET DISCIPLINE: a unit is emitted only if every (memory_id, version_id) it names is in
   the authorized view. This is DEFENCE IN DEPTH: unreachable on the real path, because the slot table is built only
   from ``view.items``; it guards a future table or extractor that over-reports.
5. BUDGET + ORDER. Tier 1's reading order (best LAST) is kept; added slot units go LAST (they are the slot's answer).
   If the total overshoots ``budget_tokens``, NON-SLOT units (asserting no claim) are trimmed first, from the FRONT
   (the weakest). Only then are SLOT units trimmed, and only as WHOLE GROUPS: every unit asserting a value of one slot
   (units sharing any slot are merged into one group) is kept or dropped together, frontmost group first. Dropping a
   slot entirely (no answer) is acceptable; serving one half of a conflict (a wrong single answer) never is.

TIME AXES. The view is VALID-time only: "what held at t". Transaction (ingestion) time is exposed only as lineage
through the adapter's ``audit()`` (``retained_at``); there is no "as known at" axis, so a backfilled record changes
past answers retroactively. A malformed (zone-less or unparseable) ``valid_from`` / ``valid_to`` on ANY authorized
envelope raises ``ValueError`` and fails the WHOLE retrieve closed: one bad record is an outage of the view (noted for
S7: per-record quarantine instead).

DEFERRED (not built): a model-backed extractor (S7) and a persisted slot index. Re-evaluate trigger for the index:
this tier's retrieve wall-clock exceeds the episodic tier's by more than 3x on the PMB-1 medium fixture (the same
trigger memory_tier_episodic.py names).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import memory_policy_evaluator as ev
import memory_store as ms
import memory_tier_episodic as tier1

_BUDGET_CUTS = ("item_budget_truncated", "token_budget_truncated")


# --------------------------------------------------------------------------- extractors
@dataclass(frozen=True)
class Claim:
    entity: str
    predicate: str
    value: str

    @property
    def slot(self) -> tuple[str, str]:
        return (self.entity.casefold(), self.predicate.casefold())

    @property
    def norm_value(self) -> str:
        return self.value.casefold()


class Extractor(Protocol):
    """What the tier needs from a claim extractor (L3). ``is_pattern_stub`` declares that it knows the evaluation
    corpus's grammar (R6): its records must say so, and its scores show what the gate can measure, not what a real
    extractor would score. ``template_words``: words its units add that no payload holds (empty for verbatim units)."""

    name: str
    is_pattern_stub: bool
    template_words: tuple[str, ...]

    def claims(self, sentence: str) -> list[Claim]: ...


class PossessiveCopulaExtractor:
    """English "<X>'s <p> is <v>." (one-word X, p, v; the sentence ends at the value). General grammar, but EXACTLY
    the PMB-1 synthetic corpus's fact grammar, so it is declared a pattern stub (R6)."""

    name = "pattern-stub:possessive-copula"
    is_pattern_stub = True
    template_words: tuple[str, ...] = ()
    _RX = re.compile(r"\b(\w+)'s (\w+) is (\w+)\.")

    def claims(self, sentence: str) -> list[Claim]:
        return [Claim(m[1], m[2], m[3]) for m in self._RX.finditer(sentence)]


DEFAULT_EXTRACTOR = PossessiveCopulaExtractor()


# --------------------------------------------------------------------------- records + units
@dataclass(frozen=True)
class ClaimRecord:
    """One claim in one authorized payload sentence, with its envelope's valid interval."""
    claim: Claim
    memory_id: str
    version_id: str
    sentence_index: int
    text: str  # the source sentence, verbatim
    valid_from: datetime
    valid_to: datetime | None
    use_disposition: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.memory_id, self.version_id)


@dataclass(frozen=True)
class Unit:
    text: str
    memory_id: str
    version_id: str
    sentence_index: int
    use_disposition: str
    support: tuple[tuple[str, str], ...] = ()  # every OTHER version this unit's claim depends on
    added: bool = False  # True: a slot unit the tier added (not one of tier 1's extracts)

    @property
    def backlink(self) -> tuple[str, str, int]:
        return (self.memory_id, self.version_id, self.sentence_index)

    @property
    def sources(self) -> tuple[tuple[str, str], ...]:
        return ((self.memory_id, self.version_id),) + self.support


class SlotViewTruncated(ValueError):
    """The authorization view of the full retrievable set was cut by the request's item/token budget: the slot table
    would silently shrink. Pass item_budget=0, token_budget=0 (unbounded)."""


def _instant(value: str, what: str) -> datetime:
    t = ms.parse_zoned_time(value)
    if t is None:
        raise ValueError(f"{what} must be an ISO-8601 time WITH a zone, got {value!r}")
    return t


def valid_at(records: list[ClaimRecord], t: datetime) -> list[ClaimRecord]:
    """L4: the records of ONE slot that hold at ``t``: those at the latest valid_from <= t, minus any whose own
    valid_to has closed by t. A later valid_from closes every earlier interval."""
    live = [r for r in records if r.valid_from <= t]
    if not live:
        return []
    top = max(r.valid_from for r in live)
    return [r for r in live if r.valid_from == top and (r.valid_to is None or t < r.valid_to)]


def slot_table(store: Any, items: list, extractor: Extractor) -> dict[tuple[str, str], list[ClaimRecord]]:
    """(entity, predicate) -> every claim in the given AUTHORIZED view items. Reads only those items."""
    table: dict[tuple[str, str], list[ClaimRecord]] = {}
    for it in items:
        payload = store.read_payload(it.memory_id, it.version_id)
        if payload is None:
            continue
        env = store.stored_envelope(it.memory_id, it.version_id)
        vf = _instant(env.valid_from, f"{it.memory_id}/{it.version_id} valid_from")
        vt = None if env.valid_to is None else _instant(env.valid_to, f"{it.memory_id}/{it.version_id} valid_to")
        for idx, sent in enumerate(tier1.split_sentences(payload)):
            for c in extractor.claims(sent):
                table.setdefault(c.slot, []).append(ClaimRecord(
                    c, it.memory_id, it.version_id, idx, sent, vf, vt, it.use_disposition.value))
    return table


def retrieve_units(
    store: Any, trust_policy: Any, request: Any, query: str, *, budget_tokens: int, top_k: int,
    as_of: str | None = None, extractor: Extractor = DEFAULT_EXTRACTOR, embedder: Any = None,
    tau: float | None = None, sem_k: int | None = None,
) -> list[Unit]:
    """Module docstring steps 1-5. ``as_of``: a structured ISO-8601 instant (R7); ``None`` = ``request.request_time``.
    ``embedder`` / ``tau`` / ``sem_k`` pass through to tier 1 unchanged (its optional semantic leg; None: lexical)."""
    t = _instant(as_of if as_of is not None else request.request_time, "as_of" if as_of is not None else "request_time")
    if budget_tokens <= 0 or top_k <= 0:
        return []
    live = sorted(store.retrievable_envelopes(), key=lambda e: (e.memory_id, e.version_id))
    view = ev.evaluate_retrieval(request, live, trust_policy)  # step 1: authorization BEFORE any read (INV-6, L2)
    cut = {k: view.omitted_reason_counts[k] for k in _BUDGET_CUTS if view.omitted_reason_counts.get(k)}
    if cut:
        raise SlotViewTruncated(f"the authorization view was budget-truncated {cut}; the slot tier needs an "
                                "unbounded view (item_budget=0, token_budget=0)")
    authorized = {(i.memory_id, i.version_id) for i in view.items}
    if not authorized:
        return []
    table = slot_table(store, list(view.items), extractor)  # step 2 (per-call: nothing outlives this call)
    valid = {k: valid_at(v, t) for k, v in table.items()}  # step 3

    def unit_for(r: ClaimRecord, added: bool) -> Unit | None:
        """A unit for record ``r``'s sentence (support: ``conflict_support``). Meet discipline: refused unless every
        version it names is authorized."""
        u = Unit(r.text, r.memory_id, r.version_id, r.sentence_index, r.use_disposition,
                 conflict_support(extractor.claims(r.text), r.key), added)
        return u if all(s in authorized for s in u.sources) else None

    def conflict_support(cs: list[Claim], own: tuple[str, str]) -> tuple[tuple[str, str], ...]:
        """Every OTHER source holding a DIFFERENT value valid at t for a slot these claims assert: the conflict the
        unit is part of. An agreeing second source is not a dependency, and a closed (earlier) value is history."""
        out: dict[tuple[str, str], None] = {}
        for c in cs:
            out.update(dict.fromkeys(r.key for r in valid.get(c.slot, [])
                                     if r.key != own and r.claim.norm_value != c.norm_value))
        return tuple(out)

    def holds(c: Claim) -> bool:
        return c.norm_value in {r.claim.norm_value for r in valid.get(c.slot, [])}

    # step 4: tier 1's extracts (same request: it authorizes its own candidates again, a subset of this view)
    extracts = tier1.retrieve_extracts(
        store, trust_policy, request, query, budget_tokens=budget_tokens, top_k=top_k,
        **({} if embedder is None else {"embedder": embedder, "tau": tau, "sem_k": sem_k}))
    touched: dict[tuple[str, str], None] = {}
    kept: list[Unit] = []
    for x in extracts:
        if (x.memory_id, x.version_id) not in authorized:
            continue  # cannot happen (same request, a subset); kept as the meet check for tier 1's own units
        cs = extractor.claims(x.text)
        touched.update(dict.fromkeys(c.slot for c in cs))
        if not all(holds(c) for c in cs):
            continue  # DROP: it asserts a value not valid at t
        u = Unit(x.text, x.memory_id, x.version_id, x.sentence_index, x.use_disposition,
                 conflict_support(cs, (x.memory_id, x.version_id)))
        if all(s in authorized for s in u.sources):
            kept.append(u)
    terms = set(tier1.query_terms(query))
    for k in valid:
        if k[0] in terms and k[1] in terms:
            touched.setdefault(k, None)
    have = {(c.slot, c.norm_value) for u in kept for c in extractor.claims(u.text)}
    added: list[Unit] = []
    for k in touched:
        for r in sorted(valid.get(k, []), key=lambda r: (r.memory_id, r.version_id, r.sentence_index)):
            if (k, r.claim.norm_value) in have:
                continue
            if not all(holds(c) for c in extractor.claims(r.text)):
                continue  # the sentence also asserts a value not valid at t: never re-add it
            u = unit_for(r, True)
            if u is not None:
                added.append(u)
                have.update((c.slot, c.norm_value) for c in extractor.claims(r.text))
    return _fit(kept + added, budget_tokens, extractor)


def _fit(units: list[Unit], budget_tokens: int, extractor: Extractor) -> list[Unit]:
    """Step 5: keep reading order; trim non-slot units from the front first, then whole slot groups (frontmost first)
    until the content budget holds. A conflict is never halved by the budget."""
    n = len(units)
    slots = [{c.slot for c in extractor.claims(u.text)} for u in units]
    group = list(range(n))  # union-find over "shares a slot"

    def root(i: int) -> int:
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    first: dict[tuple[str, str], int] = {}
    for i, ss in enumerate(slots):
        for k in ss:
            if k in first:
                group[root(i)] = root(first[k])
            else:
                first[k] = i
    alive = [True] * n
    used = sum(tier1.count_tokens(u.text) for u in units)
    for i in range(n):  # non-slot units first, weakest (front) first
        if used <= budget_tokens:
            break
        if not slots[i]:
            alive[i] = False
            used -= tier1.count_tokens(units[i].text)
    for i in range(n):  # then whole slot groups, frontmost first
        if used <= budget_tokens:
            break
        if alive[i]:
            g = root(i)
            for j in range(n):
                if alive[j] and root(j) == g:
                    alive[j] = False
                    used -= tier1.count_tokens(units[j].text)
    return [u for u, a in zip(units, alive) if a]
