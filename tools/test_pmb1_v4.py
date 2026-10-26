#!/usr/bin/env python3
"""PMB-1 tier-2 gate (T-2518 tier 2, S0-S3): fixture v4, tracks A2c / A9 / C5 / GOV-derived, ``gate(2, ...)``.

ARC: ORG-062 tier 2 (the semantic/relational bi-temporal tier) must pass a PMB-1 gate that CAN FAIL before it
ships. This file builds no tier. It holds the gate's test doubles, all synthetic and all HERE, never in production
code:

* ``RefTier`` is a small validity-aware slot layer over the REAL episodic tier (Zep-style (entity, predicate) ->
  value slots with a valid interval, R2), with a pattern-stub extractor (R6: it knows the corpus grammar, and its
  records say so). It exists to show the gate CAN pass, so each adversary below can be shown to fail ONE criterion.
  It is not the tier and proves nothing about one.
* the thirteen adversaries are eleven ``RefTier`` variants with one defect each, HiddenShifted (a wrapper around
  one), and the episodic tier as-is (NoValidity). Six were written by the reviewer: SilentJoin, FakeLineage,
  IgnoresAsOf, ParaphraseJoin, ShiftedIds, HiddenShifted. HonestDense is the one honest variant: it must PASS (declared template words).
  Each is RED through the real gate on the criterion that names it.

The gate path is real: real substrates over the real governed store, the real runner and tracks, real records,
tiny x 3 seeds x 2 stub families, offline guard on. To keep the default suite cheap (REBOUND 1, F5):
* the tracks that never call the backbone (``READER_FREE``) run once per seed and are shared by both families;
  ``_both`` asserts that shared run made zero backbone calls;
* RefTier's record copies the tracks where its slot layer is inert from the episodic tier's, and an adversary's
  record re-runs for real only the tracks its defect can touch and copies the rest from RefTier's.
  ``TestAdversaryCopiesAreSound`` proves both copies sound (seed 1 strict; the adversaries' half under FULL);
* StaleGraph, ServesHistory and LatestWins run under ``PMB1_V4_FULL=1`` only.
``PMB1_V4_FULL=1`` also runs every record on every track for real with no copies and no sharing
(``TestTier2GateAllTracksReal``), the small S0 golden matrix, the tier-1 verdict goldens, and governed on v4.

ANY NEW ADVERSARY MUST JOIN ``ADVERSARIES`` AND ``TestTier2GateAllTracksReal`` (its
``test_every_adversary_is_in_this_matrix`` enforces the second): the default suite's copied tracks are only as sound
as that all-real matrix makes them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
from collections import defaultdict
from unittest import mock

from tools.pmb1 import fixtures as fx1
from tools.pmb1 import fixtures_v2 as fx2
from tools.pmb1 import fixtures_v3 as fx3
from tools.pmb1 import fixtures_v4 as fx4
from tools.pmb1 import governed as govmod
from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.episodic import EpisodicTierSubstrate
from tools.pmb1.gate import gate
from tools.pmb1.governed import GovernedStoreSubstrate, fx
from tools.pmb1.records import validate_record
from tools.pmb1.substrate import ROLE_CANDIDATE, Evidence, Item, Substrate
from tools.pmb1.tokens import count_tokens

gatem = sys.modules["tools.pmb1.gate"]
ev = govmod.ev

SEEDS = (1, 2, 3)
FAMILIES = ("strict", "lenient")
SIZE = "tiny"
FULL = os.environ.get("PMB1_V4_FULL") == "1"
_GUARD = None
_REAL: dict[str, list[dict]] = {}
_FACT = re.compile(r"\b(\w+)'s (\w+) is (\w+)\.")
EXTRACTOR = "pattern-stub:fact-grammar"

# Pinned: S0-S3 must not move a byte of fixtures v1-v3 (same pins as test_pmb1_v3.py, plus v3 itself).
V1_SHAS = {1: "962ef15d4f75baad", 2: "c9cdceb0b45c4fba", 3: "295df924333fba66"}
V2_SHAS = {1: "24cb378d9921c32f", 2: "292e9470ef9f4eba", 3: "16376fad118e3f2d"}


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


# --------------------------------------------------------------------------- the reference slot tier (test-only)
class RefTier(EpisodicTierSubstrate):
    """The REAL episodic tier's extracts, then a slot layer computed at query time over the AUTHORIZED retrievable
    set only: every "<E>'s <p> is <v>." in an authorized payload is a slot record with its envelope's valid-from.
    The value of a slot at time t is the set of values recorded at its latest valid-from <= t (two values there =
    a conflict, surfaced). An extract asserting a value that is not valid at t is dropped; a valid value the
    extracts miss for a slot they touch is added as a unit backed by its source (ref + support). Superseded,
    forgotten and unauthorized versions are never in the authorized set, so nothing here serves them (R8)."""

    name = "t2-reference"
    over = "episodic"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._t: str | None = None
        self._slot_cache: dict[str, dict] = {}

    def _extra(self) -> dict:
        return {**super()._extra(), "extractor": EXTRACTOR}

    def _mutated(self) -> None:
        super()._mutated()
        self._slot_cache = {}

    def _retrieve_as_of(self, query, purpose, budget_tokens, as_of):
        self._t = as_of
        try:
            return self._retrieve(query, purpose, budget_tokens)
        finally:
            self._t = None

    # --- slot records -------------------------------------------------------
    def _authorized(self, purpose: str) -> list:
        req = fx.retrieval_request(request_id="pmb1", purpose=purpose, subject_scope=(), item_budget=0,
                                   token_budget=0, privacy_ceiling=self.privacy_ceiling)
        live = self.store.retrievable_envelopes()
        by = {(e.memory_id, e.version_id): e for e in live}
        return [by[(i.memory_id, i.version_id)] for i in ev.evaluate_retrieval(req, list(live), self._trust).items]

    def _records(self, purpose: str) -> dict:
        """(entity, predicate) -> [(valid_from, ingest_order, value, Entity, ref)] over the authorized set."""
        if purpose not in self._slot_cache:
            slots: dict = defaultdict(list)
            for env in self._authorized(purpose):
                text = self.store.read_payload(env.memory_id, env.version_id) or ""
                for m in _FACT.finditer(text):
                    slots[(m[1].lower(), m[2].lower())].append(
                        (env.valid_from, int(env.memory_id.split("-")[1]), m[3].lower(), m[1],
                         f"{env.memory_id}/{env.version_id}"))
            self._slot_cache[purpose] = dict(slots)
        return self._slot_cache[purpose]

    def valid(self, recs: list, key: tuple, all_recs: dict) -> list:
        """The records valid at ``self._t`` (None: now): those at the latest valid-from <= t."""
        live = [r for r in recs if self._t is None or r[0] <= self._t]
        if not live:
            return []
        top = max(r[0] for r in live)
        return [r for r in live if r[0] == top]

    # --- retrieval ----------------------------------------------------------
    def _retrieve_memoised(self, query, purpose, budget_tokens):
        ext = super()._retrieve_memoised(query, purpose, budget_tokens)
        recs = self._records(purpose)
        valid = {k: self.valid(v, k, recs) for k, v in recs.items()}
        out, touched = [], []
        for e in ext:
            keep = True
            for m in _FACT.finditer(e.text):
                k = (m[1].lower(), m[2].lower())
                if k in valid:
                    touched.append(k)
                    if m[3].lower() not in {r[2] for r in valid[k]}:
                        keep = False
            if keep:
                out.append(e)
        used = sum(count_tokens(e.text) for e in out)
        have = {(m[1].lower(), m[2].lower(), m[3].lower()) for e in out for m in _FACT.finditer(e.text)}
        for k in dict.fromkeys(touched):
            for r in valid[k]:
                if (k[0], k[1], r[2]) in have:
                    continue
                unit = Evidence(f"{r[3]}'s {k[1]} is {r[2]}.", r[4], "allowed", support=(r[4],))
                n = count_tokens(unit.text)
                if used + n <= budget_tokens:
                    out.append(unit)
                    used += n
                    have.add((k[0], k[1], r[2]))
        return out + self._extra_units(query, purpose, budget_tokens - used, recs)

    def _extra_units(self, query, purpose, budget_left, recs) -> list[Evidence]:
        return []


# --------------------------------------------------------------------------- the thirteen adversaries (test-only)
class StaleGraph(RefTier):
    """1. Facts frozen at WRITE time: each written or corrected version's facts are indexed once, with that version's
    valid-from, and never invalidated. A correction adds the successor's fact but leaves its predecessor's in place,
    and the index authorizes by memory, so a corrected memory still serves its superseded fact (as a "conflict")."""
    name = "adv-stale-graph"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._frozen: list[tuple[str, str, str, str, str, str]] = []  # (mid, valid_from, entity, pred, value, ref)

    def _freeze(self, ref: str, text: str) -> None:
        env = self.store.stored_envelope(*self._split(ref))
        self._frozen += [(env.memory_id, env.valid_from, m[1], m[2].lower(), m[3].lower(), ref) for m in _FACT.finditer(text)]

    def _write(self, item):
        ref = super()._write(item)
        self._freeze(ref, item.text)
        return ref

    def _supersede(self, ref, new_item):
        new = super()._supersede(ref, new_item)
        self._freeze(new, new_item.text)
        return new

    def _records(self, purpose):
        mids = {e.memory_id for e in self._authorized(purpose)}
        slots: dict = defaultdict(list)
        for i, (mid, vf, ent, pred, val, ref) in enumerate(self._frozen):
            if mid in mids:
                slots[(ent.lower(), pred)].append((vf, i, val, ent, ref))
        return dict(slots)


class ServesHistory(RefTier):
    """2. Also reads the SUPERSEDED versions of every memory it returns, and serves them as history."""
    name = "adv-serves-history"

    def _retrieve_memoised(self, query, purpose, budget_tokens):
        out = super()._retrieve_memoised(query, purpose, budget_tokens)
        extra = []
        for mid in dict.fromkeys(e.ref.split("/")[0] for e in out):
            for vid in self.store.version_ids(mid):
                if f"{mid}/{vid}" not in {e.ref for e in out} and vid not in self.store.retrievable_version_ids(mid):
                    text = self.store.read_payload(mid, vid)
                    if text:
                        extra.append(Evidence("History: " + text, f"{mid}/{vid}", "allowed"))
        return out + extra


class JoinThenAuthorize(RefTier):
    """3. Joins entity-entity edges over EVERYTHING ever written, and authorizes only the last hop (the target's
    own facts). The joined unit's ref is the authorized last hop; its support names both hops."""
    name = "adv-join-then-authorize"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._edges: list[tuple[str, str, str, str]] = []  # (X, rel, Y, ref) from every write
        self._subjects: set[str] = set()

    def _write(self, item):
        ref = super()._write(item)
        self._subjects.add(item.subject)
        self._edges += [(m[1], m[2], m[3], ref) for m in _FACT.finditer(item.text)]
        return ref

    def _extra_units(self, query, purpose, budget_left, recs):
        words = {w.lower() for w in re.findall(r"\w+", query)}
        out = []
        for x, rel, y, ref in self._edges:
            if x.lower() in words and y.lower() in self._subjects:
                hop = next((r for (ent, _), rs in sorted(recs.items()) if ent == y.lower() for r in rs), None)
                if hop is None:
                    continue
                text = f"{x}'s {rel} is {y}. {y}'s {[k for k, rs in recs.items() if hop in rs][0][1]} is {hop[2]}."
                if count_tokens(text) <= budget_left:
                    out.append(Evidence(text, hop[4], "allowed", support=(ref, hop[4])))
                    budget_left -= count_tokens(text)
        return out


class LatestWins(RefTier):
    """4. Resolves a conflict silently: of the values at a slot's latest valid-from, the latest INGESTED wins."""
    name = "adv-latest-wins"

    def valid(self, recs, key, all_recs):
        live = super().valid(recs, key, all_recs)
        return [max(live, key=lambda r: r[1])] if live else []


class AlwaysConflict(RefTier):
    """5. Predicate-blind conflict detection: any two facts about one entity valid from the same instant are a
    conflict, so the other predicate's value is surfaced as a competing value of the asked slot."""
    name = "adv-always-conflict"

    def valid(self, recs, key, all_recs):
        live = super().valid(recs, key, all_recs)
        if not live:
            return live
        top = live[0][0]
        rivals = [r for (ent, pred), rs in sorted(all_recs.items()) if ent == key[0] and pred != key[1]
                  for r in rs if r[0] == top]
        return live + rivals


class NoLineage(RefTier):
    """7. Returns the same units as RefTier but keeps no audit trail."""
    name = "adv-no-lineage"

    def audit(self, ref):
        return None


class SilentJoin(JoinThenAuthorize):
    """8. (reviewer, REBOUND 1) Joins like JoinThenAuthorize, but the unit drops X's name and the forbidden hop: "The
    <rel> is Y." under Y's own clean ref, with no support. Only the harness's content-provenance check sees it."""
    name = "adv-silent-join"

    def _extra_units(self, query, purpose, budget_left, recs):
        words = {w.lower() for w in re.findall(r"\w+", query)}
        out = []
        for x, rel, y, _ in self._edges:
            if x.lower() in words and y.lower() in self._subjects:
                hop = next((r for (ent, _), rs in sorted(recs.items()) if ent == y.lower() for r in rs), None)
                text = f"The {rel} is {y}."
                if hop is not None and count_tokens(text) <= budget_left:
                    out.append(Evidence(text, hop[4], "allowed"))
                    budget_left -= count_tokens(text)
        return out


class FakeLineage(RefTier):
    """9. (reviewer, REBOUND 1) Keeps no audit trail, but answers audit() with one plausible record for ANY ref."""
    name = "adv-fake-lineage"

    def audit(self, ref):
        return {"memory_id": "m-1", "version_id": "v-1", "retrievable": True, "state": "retained",
                "observed_at": "2026-01-01T00:00:00Z", "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None, "retained_at": "2026-01-01T00:00:00Z"}


class IgnoresAsOf(RefTier):
    """10. (reviewer, REBOUND 1) Current-only: answers every as-of query with today's view."""
    name = "adv-ignores-asof"

    def _retrieve_as_of(self, query, purpose, budget_tokens, as_of):
        return self._retrieve(query, purpose, budget_tokens)


class ParaphraseJoin(JoinThenAuthorize):
    """11. (reviewer, REBOUND 2) Joins over everything and answers in a PARAPHRASE under the target's clean ref: no
    fact sentence, no relation word, no X. Only word-level provenance sees it."""
    name = "adv-paraphrase-join"

    def _extra_units(self, query, purpose, budget_left, recs):
        words = {w.lower() for w in re.findall(r"\w+", query)}
        out = []
        for x, rel, y, _ in self._edges:
            if x.lower() in words and y.lower() in self._subjects:
                hop = next((r for (ent, _), rs in sorted(recs.items()) if ent == y.lower() for r in rs), None)
                text = f"{y} fills that role."
                if hop is not None and count_tokens(text) <= budget_left:
                    out.append(Evidence(text, hop[4], "allowed"))
                    budget_left -= count_tokens(text)
        return out


class ShiftedIds(RefTier):
    """12. (reviewer, REBOUND 2) Replays the REAL audit, but maps every ref onto the NEXT memory's version: stable,
    injective, real ids, real digest. Only checking the claimed version against the store sees it."""
    name = "adv-shifted-ids"

    def audit(self, ref):
        a = super().audit(ref)
        if a is None:
            return None
        return {**a, "memory_id": f"m-{int(a['memory_id'].split('-')[1]) + 1}"}


class HiddenShifted(Substrate):
    """13. (reviewer, REBOUND 3) ShiftedIds, wrapped: the store lives under a private name and the wrapper declares no
    lineage_store(), so a store check by attribute name never ran. C5 now requires the declared hook."""
    name = "adv-hidden-shifted"
    role = ROLE_CANDIDATE
    over = "episodic"

    def __init__(self, privacy_ceiling: str = "personal") -> None:
        super().__init__()
        self._inner = ShiftedIds(privacy_ceiling=privacy_ceiling)

    def _write(self, item):
        return self._inner.write(item)

    def _supersede(self, ref, new_item):
        return self._inner.supersede(ref, new_item)

    def _forget(self, ref):
        return self._inner.forget(ref)

    def _retrieve(self, query, purpose, budget_tokens):
        return self._inner.retrieve(query, purpose, budget_tokens)

    def _retrieve_as_of(self, query, purpose, budget_tokens, as_of):
        return self._inner.retrieve(query, purpose, budget_tokens, as_of=as_of)

    def audit(self, ref):
        return self._inner.audit(ref)

    def _extra(self) -> dict:
        return {"extractor": EXTRACTOR}

    def close(self) -> None:
        self._inner.close()


class HonestDense(RefTier):
    """NOT an adversary (reviewer, REBOUND 3): honest and dense-retriever-like, it never returns empty. It labels
    every unit "Note: " and pads with AUTHORIZED current facts under their true refs. It joins nothing and serves
    nothing forbidden, so with "note" DECLARED as a template word it must pass; undeclared, "note" is unsourced."""
    name = "t2-honest-dense"
    template_words = ("note",)

    def _retrieve_memoised(self, q, p, b):
        out = [Evidence("Note: " + e.text, e.ref, e.disposition, e.support) for e in super()._retrieve_memoised(q, p, b)]
        used = sum(count_tokens(e.text) for e in out)
        for k, rs in sorted(self._records(p).items()):
            for r in self.valid(rs, k, None):
                u = Evidence(f"Note: {r[3]}'s {k[1]} is {r[2]}.", r[4], "allowed")
                if used + count_tokens(u.text) > b:
                    return out
                out.append(u)
                used += count_tokens(u.text)
        return out


class HonestDenseUndeclared(HonestDense):
    name = "t2-honest-dense-undeclared"
    template_words = ()


# 6. NoValidity is the episodic tier as-is (no valid time at all), submitted as a tier over itself.
# EVERY adversary added here must also join the all-tracks-real matrix (TestTier2GateAllTracksReal, PMB1_V4_FULL=1):
# the default suite's copied tracks are only as sound as that matrix and the copy-soundness test make them.
ADVERSARIES = {  # class -> (the criteria it must fail, the tracks its defect can touch)
    StaleGraph: (("A2-stale-evidence",), ("A2",)),
    ServesHistory: (("A2-stale-evidence",), ("A2",)),
    JoinThenAuthorize: (("governance-leak",), ("GOV", "GOV-derived")),
    LatestWins: (("A9-surfaced-vs-baseline", "A9-no-silent-pick"), ("A9", "C5")),  # T-5043: it picks silently
    # T-5043: more false conflicts than the baseline, so it is not superior on discrimination either
    AlwaysConflict: (("A9-false-conflict", "A9-false_conflict-beats-baseline"), ("A9", "A2c", "E3", "A-retrieve", "C5")),
    NoLineage: (("C5-lineage",), ("C5",)),
    SilentJoin: (("governance-leak",), ("GOV", "GOV-derived")),
    FakeLineage: (("C5-lineage",), ("C5",)),
    IgnoresAsOf: (("A2c-asof-beats-baseline",), ("A2c", "C5")),
    ParaphraseJoin: (("governance-leak",), ("GOV", "GOV-derived")),
    ShiftedIds: (("C5-lineage",), ("C5",)),
    HiddenShifted: (("C5-lineage",), ("C5",)),
}
FULL_ONLY = (StaleGraph, ServesHistory, LatestWins)  # REBOUND 1, F5: off the default suite


# RefTier's slot layer is inert where every slot has ONE authorized value (the v1-v3 worlds): those tracks are copied
# from the episodic tier's records in the default suite (TestAdversaryCopiesAreSound proves it), the rest run for real.
INERT = ("A1", "A-retrieve", "A3", "E3", "A2", "GOV")
REF_TRACKS = ("A2c", "A9", "C5", "GOV-derived")
# Tracks that never call the backbone: run once per seed and shared by both reader families. _both() ASSERTS the
# shared run made zero backbone calls, so the sharing cannot hide a reader effect.
READER_FREE = ("A-retrieve", "GOV", "A9", "C5", "GOV-derived")
# Record-level fields that do not depend on the reader family (every other field is rebuilt per family in _both).
FAMILY_FREE_FIELDS = frozenset({"schema", "gate", "created", "substrate", "embedding_model", "fixture", "tracks",
                                "not_run", "cost", "samples", "embedder_is_lexical_stub", "extractor_is_pattern_stub",
                                "template_words"})


# --------------------------------------------------------------------------- record plumbing (real runs, cached)
def _run(make, seed: int, fam: str, tracks=None) -> dict:
    f = fx4.generate_v4(seed, SIZE)
    return runner.run_one(lambda: make(f), StubBackbone(fam), f, gate=True, tracks=tracks,
                          embedder_is_lexical_stub=True, extractor_is_pattern_stub=True)


def _both(make, seed: int, tracks=None) -> list[dict]:
    """One record per family for ``seed``: reader-free tracks run ONCE (zero backbone calls, asserted) and shared,
    reader tracks run per family."""
    names = tuple(tracks or runner.TRACKS_V4)
    free = tuple(t for t in names if t in READER_FREE)
    read = tuple(t for t in names if t not in READER_FREE)
    shared = _run(make, seed, "strict", free) if free else None
    assert shared is None or shared["cost"]["backbone_calls"] == 0, "a READER_FREE track called the backbone"
    out = []
    for fam in FAMILIES:
        bb = StubBackbone(fam)
        if not read:
            # REBOUND 2, N3: no reader track ran for this family, so rebuild the record's family-dependent fields from
            # THIS family's backbone. The allowlist fails closed: a new record field must be classified here first.
            assert set(shared) == FAMILY_FREE_FIELDS | {"backbone", "reader_knows_lexicon"}, sorted(shared)
            out.append({**shared, "backbone": {"name": bb.name, "family": bb.family},
                        "reader_knows_lexicon": bool(bb.knows_lexicon)})
            continue
        part = _run(make, seed, fam, read)
        if shared is not None:
            part = {**part, "tracks": {**part["tracks"], **shared["tracks"]},
                    "samples": {**part["samples"], **shared["samples"]},
                    "cost": {k: round(v + shared["cost"][k], 6) for k, v in part["cost"].items()}}
        out.append(part)
    return out


def _grid(key: str, build) -> list[dict]:
    """``build(seed)`` -> one record per family, cached for the module (per xdist worker)."""
    if key not in _REAL:
        _REAL[key] = [r for seed in SEEDS for r in build(seed)]
    return _REAL[key]


def of(name: str, real: bool = False) -> list[dict]:
    """A registry substrate (episodic, governed, the controls) on v4, every track run for real."""
    make = lambda x: runner.SUBSTRATES[name](x)  # noqa: E731
    if real:
        return _grid(f"{name}/real", lambda s: [_run(make, s, f) for f in FAMILIES])
    return _grid(name, lambda s: _both(make, s))


def controls() -> list[dict]:
    return [r for n in ("rag", "random", "full", "amnesiac") for r in of(n)]


def merged(base: dict, part: dict) -> dict:
    """``base`` with ``part``'s substrate and tracks (a real run of only the tracks ``part`` ran)."""
    return {**base, "substrate": part["substrate"], "embedding_model": part["embedding_model"],
            "template_words": part["template_words"],  # a declaration of the candidate's, never of the base
            "tracks": {**base["tracks"], **part["tracks"]}, "samples": {**base["samples"], **part["samples"]}}


def _make(cls):
    return lambda x: cls(privacy_ceiling=x.privacy_ceiling)


def _key(r: dict) -> tuple:
    return r["fixture"]["seed"], r["backbone"]["family"]


def ref(real: bool = False) -> list[dict]:
    """RefTier's records. Default: INERT tracks copied from the episodic tier's (TestAdversaryCopiesAreSound);
    ``real``: every track run for real."""
    if real:
        return _grid("ref/real", lambda s: [_run(_make(RefTier), s, f) for f in FAMILIES])
    epi = {_key(r): r for r in of("episodic")}
    return _grid("ref", lambda s: [merged(epi[_key(p)], p) for p in _both(_make(RefTier), s, REF_TRACKS)])


def adversary(cls, real: bool = False) -> list[dict]:
    """An adversary's records. Default: only the tracks its defect can touch re-run, the rest copied from RefTier's;
    ``real``: every track run for real."""
    if real:
        return _grid(f"{cls.__name__}/real", lambda s: [_run(_make(cls), s, f) for f in FAMILIES])
    base = {_key(r): r for r in ref()}
    return _grid(cls.__name__, lambda s: [merged(base[_key(p)], p) for p in _both(_make(cls), s, ADVERSARIES[cls][1])])


def variant(cls, touched: tuple[str, ...]) -> list[dict]:
    """A non-adversary RefTier variant, built like adversary() with ``touched`` re-run for real."""
    base = {_key(r): r for r in ref()}
    return _grid(cls.__name__, lambda s: [merged(base[_key(p)], p) for p in _both(_make(cls), s, touched)])


def relabel(recs: list[dict], name: str, over: str | None) -> list[dict]:
    out = []
    for r in recs:
        sub = {k: v for k, v in r["substrate"].items() if k != "over"}
        out.append({**r, "substrate": {**sub, "name": name, **({"over": over} if over else {})}})
    return out


def no_validity() -> list[dict]:
    """6. The episodic tier as-is (no valid time), submitted as a tier over the episodic tier."""
    return relabel(of("episodic"), "adv-no-validity", "episodic")


def batch(cand: list[dict], real: bool = False) -> list[dict]:
    """The candidate plus its over=episodic baseline and the four controls."""
    return cand + of("episodic", real) + [r for n in ("rag", "random", "full", "amnesiac") for r in of(n, real)]


def crit(v, name):
    return next(c for c in v.criteria if c.name == name)


def norm_digest(rec: dict) -> str:
    rec = {k: v for k, v in rec.items() if k != "created"}
    rec["cost"] = {k: v for k, v in rec["cost"].items() if not k.endswith("_seconds")}
    return hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- S0 golden: v1-v3 records unchanged
# Digests of v1 / v2 / v3 run records (``created`` and wall-clock seconds removed), computed on the tree BEFORE S0
# (dc6223b8f): seed 1, strict stub reader. Tiny for the default suite; the whole small matrix under PMB1_V4_FULL=1.
GOLDEN_TINY = {"v1/governed": "5154e14c5709d624", "v1/rag": "517de599daf4645b", "v2/rag": "ca4589653fa1434c",
               "v3/governed": "5ef1c7198950cb68", "v3/rag": "c3b1227e5df961c2"}  # v2 governed: in GOLDEN_SMALL (FULL)
GOLDEN_SMALL = {
    "v1/amnesiac": "910cee7bdd911fbd", "v1/episodic": "4fb2157b8bc4379f", "v1/full": "db688ae936c06927",
    "v1/governed": "2091291ade181d6c", "v1/rag": "7608baf1ae6332ae", "v1/random": "00b2210de07700b9",
    "v2/amnesiac": "a00f18e8c2b9d2e3", "v2/episodic": "0c93a9cacb8c0807", "v2/full": "8c0f2dbdd84aba8e",
    "v2/governed": "e00c2d96f3f64a99", "v2/rag": "ba4df82c165b846b", "v2/random": "92f3541c994cdc38",
    "v3/amnesiac": "cd1d82de8ec42311", "v3/episodic": "9f59ec8d35004d1b", "v3/full": "898481ec4950e328",
    "v3/governed": "789b4a079659d199", "v3/rag": "5f710c4d908c8458", "v3/random": "60c6bbd70d84f290",
}
# Tier-1 verdicts (Verdict.to_dict() digest) on tiny records BEFORE S0-S3: v1 seed 1, v3 seeds 1-3, both families.
GOLDEN_VERDICTS = {"v1/episodic-tier": "4eb256c4919b963d", "v1/governed-store": "966e0f1b39b833e3",
                   "v3/episodic-tier": "a40663c13da1e289", "v3/governed-store": "1475ff2c228e4beb"}


def _golden_record(version: int, size: str, name: str, seed: int = 1, fam: str = "strict") -> dict:
    f = fx3.generate_by_version(seed, size, version)
    return runner.run_one(lambda: runner.SUBSTRATES[name](f), StubBackbone(fam), f, gate=True,
                          embedder_is_lexical_stub=True)


class TestS0RecordsUnchanged(unittest.TestCase):
    def test_v1_v2_v3_records_are_byte_identical_to_before(self) -> None:
        for key, want in GOLDEN_TINY.items():
            v, name = key.split("/")
            self.assertEqual(norm_digest(_golden_record(int(v[1]), "tiny", name)), want, key)


class TestS0Interfaces(unittest.TestCase):
    def test_defaults_keep_todays_shapes(self) -> None:
        self.assertIsNone(Item("k", "s", "t").valid_from)
        self.assertEqual(Evidence("t", "r").support, ())
        self.assertIsNone(runner.SUBSTRATES["rag"](fx4.generate_v4(1, SIZE)).audit("r1"))

    def test_governed_audit_is_ids_and_times_only_and_tracks_retrievability(self) -> None:
        g = GovernedStoreSubstrate()
        try:
            text = "Zed's pet is fuzozo. Zed and Bo talked."
            ref = g.write(Item("k", "zed", text, valid_from="2026-03-01T00:00:00Z"))
            a = g.audit(ref)
            self.assertEqual((a["memory_id"], a["version_id"], a["valid_from"], a["retrievable"]),
                             ("m-1", "v-1", "2026-03-01T00:00:00Z", True))
            self.assertTrue(a["retained_at"] and a["observed_at"])
            self.assertEqual(a["payload_digest"], "sha256:" + hashlib.sha256(text.encode()).hexdigest())  # a hash
            self.assertFalse(any(isinstance(v, str) and "fuzozo" in v for v in a.values()))  # no payload text
            new = g.supersede(ref, Item("k", "zed", "Zed's pet is gakulo."))
            self.assertFalse(g.audit(ref)["retrievable"])  # the superseded version no longer resolves as retrievable
            self.assertEqual(g.audit(new)["valid_from"], "2026-03-01T00:00:00Z")  # None keeps the prior's
            self.assertIsNone(g.audit("not-a-ref"))
            self.assertIsNone(g.audit("m-9/v-1"))
        finally:
            g.close()

    def test_a_substrate_without_valid_time_answers_as_of_with_its_current_view(self) -> None:
        g = GovernedStoreSubstrate()
        try:
            g.write(Item("k", "zed", "Zed's pet is fuzozo."))
            q = "What is Zed's pet?"
            self.assertEqual(g.retrieve(q, "companionship", 60), g.retrieve(q, "companionship", 60, as_of="2020-01-01T00:00:00Z"))
        finally:
            g.close()


# --------------------------------------------------------------------------- S1 fixture v4
class TestFixtureV4(unittest.TestCase):
    def test_v1_v2_v3_bytes_are_untouched(self) -> None:
        v3 = {1: "53da631d0b0e06a3", 2: "ba040fa26a77e32b", 3: "0ebf361fc6c39218"}  # computed before S1 (dc6223b8f)
        for s in SEEDS:
            self.assertEqual(fx1.generate(s, SIZE).sha256[:16], V1_SHAS[s])
            self.assertEqual(fx2.generate_v2(s, SIZE).sha256[:16], V2_SHAS[s])
            self.assertEqual(fx3.generate_v3(s, SIZE).sha256[:16], v3[s])

    def test_seeded_sha_and_probe_count_are_pinned(self) -> None:
        pins = {1: "a0388d58bf96bb59", 2: "11d4a0bb6e69484e", 3: "682cb6c8a43c2ae2"}
        for s in SEEDS:
            a = fx4.generate_v4(s, SIZE)
            self.assertEqual(a.sha256, fx4.generate_v4(s, SIZE).sha256)
            self.assertEqual(a.sha256[:16], pins[s])
            self.assertEqual(fx3.generate_from_spec(f"seed={s},size=tiny,version=4").sha256, a.sha256)
            self.assertEqual((len(a.changes), len(a.conflicts), len(a.controls), len(a.derived)), (4, 4, 12, 4))
        self.assertEqual(fx3.parse_fixture_spec("seed=1,size=tiny,version=4"), (1, "tiny", 4))
        with self.assertRaises(ValueError):
            fx3.parse_fixture_spec("seed=1,size=tiny,version=5")

    def test_v4_is_v3_plus_the_new_axes(self) -> None:
        v3, v4 = fx3.generate_v3(1, SIZE), fx4.generate_v4(1, SIZE)
        for k in ("episodes", "probes", "forgets", "restricted", "gov_probes", "supersedes", "restricted_tokens"):
            self.assertEqual(getattr(v4, k), getattr(v3, k), k)
        self.assertEqual(v4.version, 4)

    def test_axes_are_well_formed(self) -> None:
        for s in SEEDS:
            f = fx4.generate_v4(s, SIZE)
            vf = f.episode_valid_from()
            slots = [(p.entity, p.attr) for p in f.changes + f.conflicts + f.controls]
            self.assertEqual(len(slots), len(set(slots)))  # one axis per slot
            self.assertEqual(sorted(c.kind for c in f.changes).count("forward"), len(f.changes) // 2)
            for c in f.changes:
                self.assertEqual(c.base_valid_from, vf[c.base_episode])
                self.assertNotEqual(c.other_value, c.base_value)
                lo, hi = sorted((c.base_valid_from, c.other_valid_from))
                self.assertTrue(lo < c.as_of < hi)  # only the earlier value holds at as_of
                earlier = c.base_value if c.base_valid_from == lo else c.other_value
                later = c.other_value if earlier == c.base_value else c.base_value
                self.assertEqual((c.as_of_value, c.current_value), (earlier, later))
            for c in f.conflicts:
                self.assertEqual(c.other_valid_from, c.base_valid_from)
                self.assertNotEqual(c.other_value, c.base_value)
            kinds = {k: [c for c in f.controls if c.kind == k] for k in fx4.CONTROL_KINDS}
            self.assertTrue(all(c.other_valid_from > c.base_valid_from for c in kinds["change"]))
            self.assertTrue(all(c.other_attr == fx4.NEARMISS_ATTR and c.other_valid_from == c.base_valid_from
                                for c in kinds["nearmiss"]))
            self.assertEqual(len({c.entity for c in kinds["nearmiss"]}), len(kinds["nearmiss"]))
            self.assertTrue(all(c.other_value == c.base_value for c in kinds["agreement"]))
            for c in f.changes + f.conflicts + f.controls:
                self.assertIn(f"{c.entity}'s {c.other_attr} is {c.other_value}.", c.other_text)
            # a derived relation's leak tokens occur in its own episode only
            others = " ".join(t.lower() for _, _, t in f.episodes + f.restricted) + " ".join(
                c.other_text.lower() for c in f.changes + f.conflicts + f.controls)
            for d in f.derived:
                self.assertIn(d.target, {p.entity for p in f.probes})
                for t in d.tokens:
                    self.assertNotIn(t, others)
                    self.assertEqual(sum(t in x.text.lower() for x in f.derived), 1)
            self.assertEqual({d.kind for d in f.derived}, {"forgotten", "restricted"})

    def test_records_validate_and_carry_the_v4_samples(self) -> None:
        rec = _run(lambda x: runner.SUBSTRATES["amnesiac"](x), 1, "strict")
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["fixture"]["version"], 4)
        self.assertIn("A9", rec["tracks"])
        self.assertNotIn("A9", rec["not_run"])
        self.assertIn("C5.purpose-use-history", rec["not_run"])  # R3
        self.assertIn("A4", rec["not_run"])  # R4
        for k in ("A2c.current", "A9.surfaced", "C5.lineage", "GOV-derived.leak"):
            self.assertTrue(rec["samples"][k])
        self.assertTrue(any("extractor_is_pattern_stub" in e for e in validate_record(
            {k: v for k, v in rec.items() if k != "extractor_is_pattern_stub"})))
        self.assertTrue(any("GOV-derived.leak" in e for e in validate_record(
            {**rec, "samples": {k: v for k, v in rec["samples"].items() if k != "GOV-derived.leak"}})))


# --------------------------------------------------------------------------- S2 tracks: answerable, and discriminating
class TestTracksV4(unittest.TestCase):
    def test_asserted_reads_only_present_tense_facts_of_the_slot(self) -> None:
        from tools.pmb1.tracks_v4 import asserted
        ev = [Evidence("Zed's pet is fuzozo. Zed's pet was kalobo. Bo's pet is mezide. Zed's hobby is rulavo.", "r")]
        self.assertEqual(asserted(ev, "Zed", "pet"), {"fuzozo"})

    def test_unsourced_is_word_level_against_the_units_own_sources(self) -> None:
        """Word-level provenance (REBOUND 2, N1) alone catches SilentJoin's and ParaphraseJoin's units."""
        from tools.pmb1.tracks_v4 import PROVENANCE_GLUE, WriteLog, unsourced

        class _Sub:
            n = 0

            def write(self, item):
                self.n += 1
                return f"r{self.n}"

        log = WriteLog(_Sub())
        r = log.write(Item("k", "bo", "Day 1. Bo's pet is fuzozo. Bo and Zed talked about the kalo."))
        self.assertEqual(PROVENANCE_GLUE, frozenset())
        self.assertEqual(unsourced(Evidence("Bo's pet is fuzozo.", r), log), [])
        self.assertEqual(unsourced(Evidence("Zed talked about the kalo.", r), log), [])
        self.assertEqual(unsourced(Evidence("The mentor is Bo.", r), log), ["mentor"])
        self.assertEqual(unsourced(Evidence("Bo fills that role.", r), log), ["fills", "that", "role"])
        self.assertEqual(unsourced(Evidence("Bo's pet is gakulo.", r), log), ["gakulo"])
        self.assertEqual(unsourced(Evidence("Bo's pet is fuzozo.", "r-never-written"), log), ["bo", "s", "pet", "is", "fuzozo"])
        self.assertEqual(unsourced(Evidence("Bo's pet is fuzozo.", "r-x", support=(r,)), log), [])

    def test_the_derived_probes_are_answerable(self) -> None:
        """POSITIVE controls: an unguarded substrate returns the restricted relations, and one that joins over
        everything returns the forgotten ones too; the governed store returns neither."""
        from tools.pmb1.tracks_v4 import track_gov_derived
        f = fx4.generate_v4(1, SIZE)
        m = {}
        for name, make in (("full", lambda: runner.SUBSTRATES["full"](f)), ("governed", lambda: runner.SUBSTRATES["governed"](f)),
                           ("join", lambda: JoinThenAuthorize(privacy_ceiling=f.privacy_ceiling))):
            sub = make()
            try:
                m[name] = track_gov_derived(sub, StubBackbone("strict"), f).metrics
            finally:
                sub.close()
        self.assertEqual(m["full"]["leak_rate_restricted"], 1.0)
        self.assertEqual(m["join"]["leak_rate_forgotten"], 1.0)
        self.assertEqual(m["join"]["leak_rate_restricted"], 1.0)
        self.assertEqual(m["governed"]["governance_leak_rate"], 0.0)


class TestAdversaryCopiesAreSound(unittest.TestCase):
    """The default suite copies RefTier's INERT tracks from the episodic tier's records. That is sound only if the
    slot layer IS inert there: run them for real and compare (seed 1, strict)."""

    def test_reftier_scores_identically_to_episodic_on_the_inert_tracks(self) -> None:
        real = _run(_make(RefTier), 1, "strict", INERT)
        epi = next(r for r in of("episodic") if _key(r) == (1, "stub-strict"))  # the record ref() copies from
        for t in INERT:
            self.assertEqual(real["tracks"][t], epi["tracks"][t], t)
        for k, v in real["samples"].items():
            self.assertEqual(v, epi["samples"][k], k)

# --------------------------------------------------------------------------- S3 gate(2)
class TestTier2Gate(unittest.TestCase):
    """The default tier-2 matrix, one test per candidate so pytest-xdist can spread them; the records are cached per
    module (per worker). StaleGraph, ServesHistory and LatestWins run under PMB1_V4_FULL=1 only (REBOUND 1, F5)."""

    def check(self, cls) -> None:
        v = gate(2, batch(adversary(cls)))
        self.assertEqual(set(v.failed), set(ADVERSARIES[cls][0]), v.render())

    def test_the_reference_slot_tier_passes_and_only_because_of_what_it_is_held_to(self) -> None:
        epi, rt = of("episodic")[0], ref()[0]  # seed 1, strict: the probes separate a validity model from none
        self.assertGreater(epi["tracks"]["A2c"]["metrics"]["stale_as_current_rate"], 0.0)
        self.assertEqual(rt["tracks"]["A2c"]["metrics"]["stale_as_current_rate"], 0.0)
        self.assertGreater(rt["tracks"]["A2c"]["metrics"]["asof_accuracy"], epi["tracks"]["A2c"]["metrics"]["asof_accuracy"])
        self.assertEqual(rt["tracks"]["A9"]["metrics"]["false_conflict_rate"], 0.0)
        self.assertGreater(epi["tracks"]["A9"]["metrics"]["false_conflict_change"], 0.0)  # a change read as a conflict
        v = gate(2, batch(ref()))
        self.assertEqual((v.tier, v.candidate), (2, RefTier.name))
        self.assertEqual(v.failed, (), v.render())
        names = {c.name for c in v.criteria if c.required}
        for c in ("governance-leak", "GOV-derived-provenance", "A2-stale-evidence", "A2c-stale-as-current",
                  "A3-leak-at-or-under-floor", "C5-lineage", "A1-accuracy-vs-baseline", "A-retrieve-recall-vs-baseline",
                  "E3-utility-vs-baseline", "A2-new_value-vs-baseline", "A2c-current-beats-baseline",
                  "A2c-asof-beats-baseline", "A9-surfaced-vs-baseline", "A9-no-silent-pick",
                  "A9-false_conflict-beats-baseline",
                  "A9-false-conflict", "over-declared",
                  "extractor-named"):
            self.assertIn(c, names)
        for c in ("A2c-asof-wrong-time", "A9-false-conflict-absolute"):
            self.assertFalse(crit(v, c).required)  # informational (R9, D-b)
        self.assertEqual(crit(v, "A2c-asof-beats-baseline").numbers["stub-strict"]["n"], 12)  # clusters: 4 pairs x 3 seeds
        self.assertTrue(any(n.startswith("R6:") and RefTier.name in n for n in v.notes))
        self.assertEqual(crit(v, "governance-leak").numbers["stub-strict"]["by_samples"],
                         {"GOV.leak": 0, "GOV-derived.leak": 0})

    def test_join_then_authorize_fails_and_the_gate_sabotage_is_caught(self) -> None:
        self.check(JoinThenAuthorize)
        with mock.patch.object(gatem, "GOV_LEAK_KEYS_V4", ("GOV.leak",)):  # SABOTAGE: GOV-derived dropped
            v = gate(2, batch(adversary(JoinThenAuthorize)))
            self.assertEqual(v.failed, (), v.render())
            with self.assertRaises(AssertionError):  # ... and the adversary's own check catches the sabotage
                self.check(JoinThenAuthorize)

    def test_silent_join(self) -> None:
        self.check(SilentJoin)
        recs = adversary(SilentJoin)  # caught by the harness's content check, not by a ref or a leak token
        self.assertTrue(all(r["tracks"]["GOV-derived"]["metrics"]["governance_leak_rate"] > 0 for r in recs))

    def test_always_conflict(self) -> None:
        self.check(AlwaysConflict)

    def test_no_lineage(self) -> None:
        self.check(NoLineage)

    def test_fake_lineage(self) -> None:
        self.check(FakeLineage)

    def test_ignores_asof(self) -> None:
        self.check(IgnoresAsOf)

    def test_paraphrase_join(self) -> None:
        self.check(ParaphraseJoin)

    def test_shifted_ids(self) -> None:
        self.check(ShiftedIds)

    def test_hidden_shifted(self) -> None:
        self.check(HiddenShifted)
        self.assertTrue(all(r["tracks"]["C5"]["metrics"]["n_store_checked"] == 0 for r in adversary(HiddenShifted)))

    def test_declared_template_words(self) -> None:
        """REBOUND 3: honest wording passes when its template words are declared; undeclared, the same unit is a
        leak (it names a derived target with an unsourced word); declaring a fixture word, or more than 8, is
        refused. GOV-derived (the only track the declaration changes) runs for real; the rest are RefTier's."""
        dense = variant(HonestDense, ("GOV-derived",))
        v = gate(2, batch(dense))
        self.assertEqual(v.failed, (), v.render())
        self.assertTrue(any("declared template words" in n and "note" in n for n in v.notes), v.notes)
        self.assertEqual(dense[0]["template_words"], ["note"])
        # undeclared, "note" is an unsourced word: a leak on units naming a derived target, a provenance failure on the rest
        self.assertEqual(set(gate(2, batch(variant(HonestDenseUndeclared, ("GOV-derived",)))).failed),
                         {"governance-leak", "GOV-derived-provenance"})
        for bad in (["note", "is"], [f"zq{i}" for i in range(9)]):
            with self.subTest(declared=bad):
                v = gate(2, batch([{**r, "template_words": bad} for r in dense]))
                self.assertIn("template-words-valid", v.failed, v.render())

    def test_no_validity_the_episodic_tier_as_is_over_itself(self) -> None:
        v = gate(2, batch(no_validity()))
        # Not isolatable: against ITSELF every superiority interval is exactly 0, and having no valid time it
        # asserts stale values as current. (T-5043: A9's superiority is discrimination; the surfaced floor ties.)
        self.assertEqual(set(v.failed), {"A2c-current-beats-baseline", "A2c-asof-beats-baseline",
                                         "A9-false_conflict-beats-baseline", "A9-no-silent-pick",
                                         "A2c-stale-as-current"}, v.render())  # T-5043: 8 of 24 picks

    def test_gate_guards(self) -> None:
        with self.subTest("a single derived leak fails"):
            recs = [dict(r) for r in ref()]
            recs[0]["samples"] = {**recs[0]["samples"], "GOV-derived.leak": [1.0] + recs[0]["samples"]["GOV-derived.leak"][1:]}
            self.assertEqual(gate(2, batch(recs)).failed, ("governance-leak",))
        with self.subTest("a single provenance failure fails"):
            recs = [dict(r) for r in ref()]
            recs[0]["samples"] = {**recs[0]["samples"],
                                  "GOV-derived.provenance": [1.0] + recs[0]["samples"]["GOV-derived.provenance"][1:]}
            self.assertEqual(gate(2, batch(recs)).failed, ("GOV-derived-provenance",))
        with self.subTest("an unflagged pattern-stub extractor fails extractor-named (R6)"):
            recs = [{**r, "extractor_is_pattern_stub": False} for r in ref()]
            self.assertIn("extractor-named", gate(2, batch(recs)).failed)
        with self.subTest("tier 2 refuses a candidate that declares no over, or over itself"):
            self.assertIn("over-declared", gate(2, batch(relabel(ref(), "t2-no-over", None))).failed)
            itself = relabel(of("episodic"), "episodic-tier", "episodic")
            self.assertIn("over-declared", gate(2, itself + controls(), candidate="episodic-tier").failed)
        with self.subTest("a stale v4 sample vector is caught"):
            recs = [dict(r) for r in ref()]
            recs[0]["samples"] = {**recs[0]["samples"], "A9.false_conflict": recs[0]["samples"]["A9.false_conflict"][:-1]}
            self.assertIn("fixture-matches-seed", gate(2, batch(recs)).failed)
        with self.subTest("tier 1 on the same v4 batch still answers tier 1"):
            v = gate(1, batch(ref()))
            self.assertEqual(v.tier, 1)
            self.assertNotIn("A2c-current-beats-baseline", {c.name for c in v.criteria})

    def test_tier2_refuses_records_below_v4(self) -> None:
        f = fx3.generate_v3(1, SIZE)
        recs = [runner.run_one(lambda: runner.SUBSTRATES["amnesiac"](f), StubBackbone("strict"), f, gate=True,
                               embedder_is_lexical_stub=True)]
        v = gate(2, recs)
        self.assertEqual((v.tier, v.failed), (2, ("fixture-version-for-tier",)))
        self.assertEqual(gate(2, []).failed, ("fixture-version-for-tier",))
        with self.assertRaises(NotImplementedError):
            gate(3, recs)


def with_a9(recs: list[dict], surfaced, false_conflict, silent_pick=0.0) -> list[dict]:
    """Copies of ``recs`` with synthetic A9 samples: each argument is a value for every probe, or a function of the
    probe count returning the vector (one record per seed and family, so the vector is per record). ``silent_pick``
    defaults to none: a probe neither surfaced nor picked is a retrieval miss."""
    def vec(spec, n):
        return spec(n) if callable(spec) else [float(spec)] * n
    out = []
    for r in recs:
        s = r["samples"]
        out.append({**r, "samples": {**s, "A9.surfaced": vec(surfaced, len(s["A9.surfaced"])),
                                     "A9.false_conflict": vec(false_conflict, len(s["A9.false_conflict"])),
                                     "A9.silent_pick": vec(silent_pick, len(s["A9.surfaced"]))}})
    return out


def _sixty(n: int) -> list[float]:
    """~0.6 of n probes 1.0, the rest 0.0."""
    k = round(0.6 * n)
    return [1.0] * k + [0.0] * (n - k)


class TestTier2A9Discrimination(unittest.TestCase):
    """T-5043: A9's superiority is DISCRIMINATION (fewer false conflicts, pooled over every control kind); the
    surfaced rate is a FLOOR. A ceiling-tied surfaced rate cannot show superiority for ANY tier, so it must neither
    be required to (case 1) nor be able to earn it (case 4). Synthetic A9 samples on RefTier (candidate) and the
    episodic tier (its over=episodic baseline); every other criterion is RefTier's real, passing one."""

    def verdict(self, cand_surf, base_surf, cand_fc, base_fc, cand_pick=0.0, base_pick=0.0):
        recs = (with_a9(ref(), cand_surf, cand_fc, cand_pick) + with_a9(of("episodic"), base_surf, base_fc, base_pick)
                + controls())
        return gate(2, recs)

    def test_surfaced_tied_at_the_ceiling_with_fewer_false_conflicts_passes(self) -> None:
        v = self.verdict(1.0, 1.0, 0.0, lambda n: [0.0] + [1.0] * (n - 1))  # ~0.99 baseline false-conflict rate
        self.assertEqual(v.failed, (), v.render())
        self.assertTrue(crit(v, "A9-surfaced-vs-baseline").passed)
        self.assertTrue(crit(v, "A9-no-silent-pick").passed)
        self.assertTrue(crit(v, "A9-false_conflict-beats-baseline").passed)
        self.assertNotIn("A9-surfaced-beats-baseline", {c.name for c in v.criteria})

    def test_both_at_the_ceiling_and_both_without_false_conflicts_fails(self) -> None:
        v = self.verdict(1.0, 1.0, 0.0, 0.0)
        self.assertEqual(v.failed, ("A9-false_conflict-beats-baseline",), v.render())

    def test_surfaced_far_below_the_baseline_fails_the_floor_even_with_fewer_false_conflicts(self) -> None:
        v = self.verdict(lambda n: [1.0, 1.0, 0.0, 0.0] * (n // 4) + [1.0] * (n % 4), 1.0, 0.0, 1.0)
        self.assertEqual(v.failed, ("A9-surfaced-vs-baseline",), v.render())  # the misses are retrieval misses

    def test_surfaced_superior_with_equal_false_conflicts_fails(self) -> None:
        v = self.verdict(1.0, 0.0, 0.0, 0.0)
        self.assertTrue(crit(v, "A9-surfaced-vs-baseline").passed)
        self.assertEqual(v.failed, ("A9-false_conflict-beats-baseline",), v.render())

    # REBOUND 1-2 (F1): the relative floor alone rewards silent picking over a picking baseline, because a tier that
    # asserts one value per slot drives false conflicts to 0 BY picking. No-silent-pick (report:421) closes it.
    def test_picking_every_conflict_against_a_picking_baseline_fails_no_silent_pick(self) -> None:
        v = self.verdict(0.0, 0.0, 0.0, lambda n: [0.0] * (n // 25) + [1.0] * (n - n // 25), 1.0, 1.0)  # fc 0 vs ~0.96
        self.assertTrue(crit(v, "A9-surfaced-vs-baseline").passed)
        self.assertTrue(crit(v, "A9-false_conflict-beats-baseline").passed)
        self.assertEqual(v.failed, ("A9-no-silent-pick",), v.render())

    def test_a_sixty_percent_picker_against_a_picking_baseline_fails_no_silent_pick(self) -> None:
        def picks(n):
            return [1.0 - x for x in _sixty(n)]
        v = self.verdict(_sixty, _sixty, 0.0, 1.0, picks, picks)
        self.assertTrue(crit(v, "A9-surfaced-vs-baseline").passed)
        self.assertEqual(v.failed, ("A9-no-silent-pick",), v.render())

    def test_retrieval_misses_are_not_picks(self) -> None:
        """RefTier's REAL A9 samples: it asserts NO value on some conflict probes (retrieval misses) and picks none."""
        recs = ref()
        self.assertTrue(all(sum(r["samples"]["A9.silent_pick"]) == 0 for r in recs))
        self.assertTrue(any(min(r["samples"]["A9.surfaced"]) == 0.0 for r in recs))  # it does miss some
        v = gate(2, batch(recs))
        self.assertTrue(crit(v, "A9-no-silent-pick").passed, v.render())
        self.assertEqual(v.failed, (), v.render())

    def test_a_record_without_the_silent_pick_samples_fails_closed(self) -> None:
        recs = [{**r, "samples": {k: x for k, x in r["samples"].items() if k != "A9.silent_pick"}} for r in ref()]
        v = gate(2, batch(recs))
        self.assertEqual(v.failed, ("A9-no-silent-pick",), v.render())
        self.assertIn("written before T-5043", crit(v, "A9-no-silent-pick").detail)

    def test_a_surfaced_deficit_within_the_tolerance_passes_the_floor(self) -> None:
        # A uniform 0.03 deficit on every probe: the interval is exactly -0.03, inside the floor's tolerance (a
        # 0-tolerance floor fails it). Fractional per-probe values because tiny pools only 12 clusters per family:
        # any 0/1 miss there puts the bootstrap's lower bound below -0.05.
        v = self.verdict(0.97, 1.0, 0.0, 1.0)
        iv = crit(v, "A9-surfaced-vs-baseline").numbers["stub-strict"]
        self.assertAlmostEqual(iv["lo"], -0.03, places=6)
        self.assertEqual(v.failed, (), v.render())


class TestClusteredInterval(unittest.TestCase):
    def test_pairs_resample_together_and_widen_the_interval(self) -> None:
        from tools.pmb1.stats import clustered_interval, paired_interval
        cand = [1.0, 1.0, 0.0, 0.0] * 6  # each pair's two phrasings agree: 12 units of evidence, not 24
        base = [0.0] * 24
        iid, cl = paired_interval(cand, base, "t"), clustered_interval(cand, base, 2, "t")
        self.assertEqual((iid.diff, cl.diff, iid.n, cl.n), (0.5, 0.5, 24, 12))
        self.assertLess(cl.lo, iid.lo)
        with self.assertRaises(ValueError):
            clustered_interval([1.0] * 3, [0.0] * 3, 2, "t")


# --------------------------------------------------------------------------- PMB1_V4_FULL=1 (opt-in, heavy)
# Defined only when opted in, so the default suite neither runs nor counts them as skips (CI's outcome floor).
if FULL:
    class TestFullGoldensAndCopies(unittest.TestCase):
        """The S0 small golden matrix, the tier-1 verdict goldens, and the adversaries' copy soundness."""

        def test_the_small_matrix_is_byte_identical_to_before(self) -> None:
            for key, want in GOLDEN_SMALL.items():
                v, name = key.split("/")
                self.assertEqual(norm_digest(_golden_record(int(v[1]), "small", name)), want, key)

        def test_tier1_verdicts_are_byte_identical_to_before(self) -> None:
            for version, seeds in ((1, (1,)), (3, SEEDS)):
                recs = [_golden_record(version, "tiny", n, s, f) for s in seeds for f in FAMILIES
                        for n in ("governed", "episodic", "rag", "random", "full", "amnesiac")]
                for cand in ("governed-store", "episodic-tier"):
                    v = gate(1, recs, candidate=cand)
                    got = hashlib.sha256(json.dumps(v.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
                    self.assertEqual(got, GOLDEN_VERDICTS[f"v{version}/{cand}"], v.render())

        def test_each_adversary_scores_identically_to_reftier_where_its_records_copy_it(self) -> None:
            base = _run(_make(RefTier), 1, "strict")
            diff = []
            for cls, (_, touched) in ADVERSARIES.items():
                rest = tuple(t for t in base["tracks"] if t not in touched)
                real = _run(_make(cls), 1, "strict", rest)
                diff += [f"{cls.__name__}/{t}" for t in rest if real["tracks"][t] != base["tracks"][t]]
                diff += [f"{cls.__name__}/{k}" for k, v in real["samples"].items() if v != base["samples"][k]]
            self.assertEqual(diff, [])

        def test_the_full_only_adversaries_on_the_default_copy_path(self) -> None:
            for cls in FULL_ONLY:
                with self.subTest(cls.__name__):
                    v = gate(2, batch(adversary(cls)))
                    self.assertEqual(set(v.failed), set(ADVERSARIES[cls][0]), v.render())

    class TestTier2GateAllTracksReal(unittest.TestCase):
        """Every record on every track for real, both families run separately (no copies, no sharing)."""

        def check(self, cls) -> None:
            v = gate(2, batch(adversary(cls, real=True), real=True))
            self.assertEqual(set(v.failed), set(ADVERSARIES[cls][0]), v.render())

        def test_reference(self) -> None:
            v = gate(2, batch(ref(real=True), real=True))
            self.assertEqual(v.failed, (), v.render())

        def test_stale_graph(self) -> None:
            self.check(StaleGraph)

        def test_serves_history(self) -> None:
            self.check(ServesHistory)

        def test_join_then_authorize(self) -> None:
            self.check(JoinThenAuthorize)

        def test_latest_wins(self) -> None:
            self.check(LatestWins)

        def test_always_conflict(self) -> None:
            self.check(AlwaysConflict)

        def test_no_lineage(self) -> None:
            self.check(NoLineage)

        def test_silent_join(self) -> None:
            self.check(SilentJoin)

        def test_fake_lineage(self) -> None:
            self.check(FakeLineage)

        def test_ignores_as_of(self) -> None:
            self.check(IgnoresAsOf)

        def test_paraphrase_join(self) -> None:
            self.check(ParaphraseJoin)

        def test_shifted_ids(self) -> None:
            self.check(ShiftedIds)

        def test_hidden_shifted(self) -> None:
            self.check(HiddenShifted)

        def test_every_adversary_is_in_this_matrix(self) -> None:
            tested = {m.removeprefix("test_") for m in dir(self) if m.startswith("test_")}
            snake = {re.sub(r"(?<!^)(?=[A-Z])", "_", c.__name__).lower() for c in ADVERSARIES}
            self.assertEqual(snake - tested, set())

    class TestBaselinesFailTier2(unittest.TestCase):
        def test_governed_and_episodic_each_fail_tier2_on_superiority(self) -> None:
            epi = gate(2, of("episodic") + of("governed") + controls(), candidate="episodic-tier")  # as-is: over governed
            gov = gate(2, relabel(of("governed"), "governed-as-tier2", "episodic") + of("episodic") + controls())
            for v in (epi, gov):
                print("\n" + v.render())
                self.assertIn("A2c-current-beats-baseline", v.failed, v.render())


if __name__ == "__main__":
    unittest.main()
