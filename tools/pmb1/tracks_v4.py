"""PMB-1 tracks over the v4 fixture (T-2518 tier 2, S2).

v4 runs v3's tracks unchanged (A1, A3, A-retrieve, E3, A2, GOV) plus the four the tier-2 gate needs
(ORG-062:116-119, "Gate: PMB-1 Track A2/A9 + C5"). Each loads the base episodes WITH their valid-from, then the
axis episodes (later ingestion), and returns one sample per probe in fixture order.

* A2c          change pairs (``fx.changes``). ``current_accuracy``: the reader's answer to the plain query is the
               current value. ``asof_accuracy``: the same query with the STRUCTURED ``as_of`` (R7) answers the value
               valid then. ``stale_as_current_rate``: the evidence ASSERTS the no-longer-valid value as a present
               fact ("<E>'s <attr> is <v>.", the corpus grammar) for the plain query. History a tier labels as
               history (any other phrasing) is not an assertion.
* A9           ``surfaced_rate`` over the conflict pairs: BOTH conflicting values are asserted for the slot in the
               evidence (report:421, "surface conflict, not silently pick"). ``false_conflict_rate`` over the
               non-conflict controls: the evidence asserts two or more DIFFERENT values for the asked slot. Rates
               are also reported per control kind (change / nearmiss / agreement); the gate checks each kind.
* C5           lineage (report:444; R3: the purpose/use-history clause is NOT run). For every unit returned to any
               v4 slot probe, ``audit(ref)`` resolves to a RETRIEVABLE (memory_id, version_id) carrying its
               observation, valid-from and retain times, and every ref in the unit's ``support`` does too. The
               audit is CROSS-CHECKED against the harness's own write log (REBOUND 1, F2): the ref was issued by a
               write, its valid-from is the one written, and ref -> (memory_id, version_id) is stable and
               injective. One sample per probe (1.0 iff every unit it returned has lineage; ``n_units`` counts the units checked,
               and the gate refuses a vacuous 1.0).
* GOV-derived  the derived-relation probes. A relation whose only evidence is a forgotten or above-ceiling episode
               must not come back: a leak is any unit whose ``{ref} | support`` names a derived source, whose
               text holds a derived leak token, or the probe's relation word AND its target (all after
               ``squash``). The harness also checks CONTENT PROVENANCE, word by word (REBOUND 2, N1): every
               word of a unit (lowercased, split on non-alphanumerics) must occur in a payload the harness wrote
               under one of the unit's own sources (``{ref} | support``), apart from ``PROVENANCE_GLUE`` (empty:
               the reference tier's units are payload sentences verbatim). A unit with such a word is a leak when it
               names a derived target, and a provenance failure (``provenance`` samples, gated at exactly 0)
               otherwise. The gate pools the leak samples with GOV's.

A4 (multi-hop, R4) is not built: reported "not run" with the other v1 stubs.

Lineage is checked against the store ONLY through the declared ``lineage_store()`` hook, which is REQUIRED at tier 2
(a candidate returning ``None`` fails C5). Declared ``template_words`` (at most 8, never a fixture word, listed in
the verdict's notes) are exempt from provenance: a declaration is trusted as stated, which is why it is surfaced.

THE CEILING (read this before trusting a tier-2 pass). C5 and GOV-derived judge CONSISTENCY: a substrate's units and
its audit() against what the HARNESS wrote (the write log: refs issued, valid-from, payload digests) and, where the
substrate is or wraps the governed store, against the store itself (``stored_envelope`` and its payload digest).
They are not proof. A tier that lies consistently, inventing nothing a source does not hold and attributing every
unit to a source that holds it, can only be caught by checking against the store or by a base rate. A tier-2 pass is
NOT a guarantee of lineage or of no leaks.

KNOWN LIMIT (not built, REBOUND 2): leakage through SELECTION. A tier that joins over a forbidden relation and then
returns a VERBATIM, authorized episode it selected because of that join (say, the target's own episode, word for
word, under its true ref) passes every check here: each word is sourced, each ref clean. Remedy: decoy derived probes
(the same query shape over relations that do not exist), so the rate at which the target's episodes come back for a
REAL forbidden relation can be held to the decoy base rate.
"""
from __future__ import annotations

import hashlib
import re
from typing import Callable

from .backbone import Backbone
from .fixtures import PURPOSE
from .fixtures_v4 import CONTROL_KINDS, MODES, FixtureV4, SlotPair
from .substrate import Evidence, Item, Substrate
from .tokens import count_tokens
from .tracks import TrackResult, _mean
from .tracks_v2 import sources
from .tracks_v3 import TRACKS_V3, squash

V4_NOT_RUN = ("A7", "E4", "C5.purpose-use-history")
PATTERN_STUB_PREFIX = "pattern-stub"  # describe()["extractor"] of a relation extractor that knows the corpus grammar


def is_pattern_stub_extractor(name: object) -> bool:
    """R6: a pattern-stub extractor is allowed in a gate run only when the record declares it."""
    return isinstance(name, str) and name.startswith(PATTERN_STUB_PREFIX)
AUDIT_FIELDS = ("memory_id", "version_id", "observed_at", "valid_from", "retained_at")


def asserted(ev: list[Evidence], entity: str, attr: str) -> set[str]:
    """Values the evidence asserts as PRESENT facts of (entity, attr), in the corpus grammar."""
    rx = re.compile(rf"\b{re.escape(entity)}'s {re.escape(attr)} is (\w+)\.", re.IGNORECASE)
    return {m.group(1).lower() for e in ev for m in rx.finditer(e.text)}


class WriteLog:
    """What the HARNESS wrote, keyed by the ref the substrate issued for it (T-2518 t2 REBOUND 1, F1/F2).

    The C5 and GOV-derived checks judge a unit against this log, never against what the substrate says about
    itself: a substrate's own ``support`` and ``audit()`` are claims; the log is what happened."""

    def __init__(self, sub: Substrate) -> None:
        self.sub = sub
        self.items: dict[str, Item] = {}

    def write(self, item: Item) -> str:
        ref = self.sub.write(item)
        self.items[ref] = item
        return ref

    def texts(self, refs) -> list[str]:
        return [self.items[r].text for r in refs if r in self.items]


def load_v4(sub: Substrate, fx: FixtureV4, pairs: list[SlotPair] = (), log: WriteLog | None = None) -> dict[str, str]:
    """Base episodes with their valid-from, then each pair's second episode (later ingestion)."""
    w = (log or WriteLog(sub)).write
    vf = fx.episode_valid_from()
    refs = {k: w(Item(k, s, t, valid_from=vf[k])) for k, s, t in fx.episodes}
    for p in pairs:
        refs[p.other_key] = w(Item(p.other_key, p.entity.lower(), p.other_text, valid_from=p.other_valid_from))
    return refs


_WORDS = re.compile(r"[a-z0-9]+")
# Words a unit may carry that no source payload holds: the fixed tokens of the reference tier's own output template.
# EMPTY: RefTier's units are payload sentences verbatim ("<E>'s <p> is <v>." splits to words its source holds).
PROVENANCE_GLUE: frozenset[str] = frozenset()


def words_of(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def unsourced(e: Evidence, log: WriteLog, template: frozenset[str] = frozenset()) -> list[str]:
    """Words of ``e`` that NO payload among its own sources (``{ref} | support``, as written by the harness) holds,
    in order, deduplicated (REBOUND 2, N1: word-level, so a paraphrase cannot pass). Empty = every word traceable.
    ``template``: the substrate's DECLARED template words (REBOUND 3), exempt; the gate validates the declaration."""
    held = {w for t in log.texts(sources(e)) for w in words_of(t)} | PROVENANCE_GLUE | template
    return list(dict.fromkeys(w for w in words_of(e.text) if w not in held))


def track_a2c(sub: Substrate, bb: Backbone, fx: FixtureV4) -> TrackResult:
    load_v4(sub, fx, fx.changes)
    budget = fx.retrieval_budget(count_tokens)
    cur, asof, stale, wrong, kinds = [], [], [], [], []
    for c, mode in probes(fx.changes):
        q = c.query(mode)
        ev = sub.retrieve(q, PURPOSE, budget)
        cur.append(1.0 if bb.judge(q, bb.generate(q, [e.text for e in ev]), c.current_value) else 0.0)
        stale.append(1.0 if c.stale_value in asserted(ev, c.entity, c.attr) else 0.0)
        ev = sub.retrieve(q, PURPOSE, budget, as_of=c.as_of)
        asof.append(1.0 if bb.judge(q, bb.generate(q, [e.text for e in ev]), c.as_of_value) else 0.0)
        wrong.append(1.0 if c.current_value in asserted(ev, c.entity, c.attr) else 0.0)
        kinds.append(c.kind)
    m = {"current_accuracy": _mean(cur), "asof_accuracy": _mean(asof), "stale_as_current_rate": _mean(stale),
         "asof_wrong_time_rate": _mean(wrong)}
    for kind in ("forward", "backfill"):
        m[f"current_accuracy_{kind}"] = _mean([x for k, x in zip(kinds, cur) if k == kind])
        m[f"n_{kind}"] = float(kinds.count(kind))
    return TrackResult("A2c", len(cur), m, {"current": cur, "asof": asof, "stale_as_current": stale})


def track_a9(sub: Substrate, bb: Backbone, fx: FixtureV4) -> TrackResult:
    load_v4(sub, fx, fx.conflicts + fx.controls)
    budget = fx.retrieval_budget(count_tokens)
    surf, fc, pick = [], [], []
    for c, mode in probes(fx.conflicts):
        vals = asserted(sub.retrieve(c.query(mode), PURPOSE, budget), c.entity, c.attr)
        surf.append(1.0 if {c.base_value, c.other_value} <= vals else 0.0)
        # T-5043: a SILENT PICK asserts exactly one value for the conflicted slot (report:421). Asserting none is a
        # retrieval miss (A-retrieve's business), asserting two or more is surfacing.
        pick.append(1.0 if len(vals) == 1 else 0.0)
    ctl = probes(fx.controls)
    for c, mode in ctl:
        fc.append(1.0 if len(asserted(sub.retrieve(c.query(mode), PURPOSE, budget), c.entity, c.attr)) >= 2 else 0.0)
    m = {"surfaced_rate": _mean(surf), "false_conflict_rate": _mean(fc)}
    for kind in CONTROL_KINDS:
        m[f"false_conflict_{kind}"] = _mean([x for (c, _), x in zip(ctl, fc) if c.kind == kind])
        m[f"n_{kind}"] = float(sum(c.kind == kind for c, _ in ctl))
    return TrackResult("A9", len(surf) + len(fc), m, {"surfaced": surf, "false_conflict": fc, "silent_pick": pick})


class LineageCheck:
    """C5, cross-checked against the harness's write log (REBOUND 1, F2; REBOUND 2, N2): ``audit(ref)`` must be a
    retrievable record with its times and a ``payload_digest``, AND the ref must be one write() issued, its
    ``valid_from`` must equal the Item's, its ``payload_digest`` must equal the sha256 of the Item's text, and the
    ref -> (memory_id, version_id) map must be stable (same ref, same pair) and injective (distinct refs, distinct
    pairs). Where the substrate is or wraps the governed store (it has ``store.stored_envelope``), the claimed
    (memory_id, version_id) must also exist IN THE STORE with that same payload digest: the substrate's word is not
    taken for it. A plausible constant fails here; so do real ids shifted onto other real versions."""

    def __init__(self, sub: Substrate, log: WriteLog) -> None:
        self.sub, self.log = sub, log
        self.store = sub.lineage_store()  # the DECLARED hook (REBOUND 3); no attribute sniffing
        self.pair_of: dict[str, tuple] = {}
        self.ref_of: dict[tuple, str] = {}

    def ok(self, ref: str) -> bool:
        a = self.sub.audit(ref)
        if not (isinstance(a, dict) and a.get("retrievable") is True and all(a.get(k) for k in AUDIT_FIELDS)):
            return False
        item = self.log.items.get(ref)
        if item is None or (item.valid_from is not None and a["valid_from"] != item.valid_from):
            return False
        want = digest(item.text)
        if a.get("payload_digest") != want:
            return False
        if self.store is not None:
            env = self.store.stored_envelope(a["memory_id"], a["version_id"])
            if env is None or env.payload_digest != want:
                return False
        pair = (a["memory_id"], a["version_id"])
        if self.pair_of.setdefault(ref, pair) != pair or self.ref_of.setdefault(pair, ref) != ref:
            return False
        return True

    def unit(self, e: Evidence) -> bool:
        return all(self.ok(r) for r in sources(e))


def digest(text: str) -> str:
    """The payload digest the harness expects for what it wrote (the store's format: ``sha256:<hex>``)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def has_lineage(sub: Substrate, e: Evidence, check: LineageCheck | None = None) -> bool:
    """Self-attested lineage only (no write log): kept for callers outside a track. The track uses LineageCheck."""
    if check is not None:
        return check.unit(e)

    def ok(ref: str) -> bool:
        a = sub.audit(ref)
        return isinstance(a, dict) and a.get("retrievable") is True and all(a.get(k) for k in AUDIT_FIELDS)
    return all(ok(r) for r in sources(e))


def probes(pairs: list[SlotPair]) -> list[tuple[SlotPair, str]]:
    """(pair, mode) in fixture order: each pair asked in both phrasings. ONE definition, so the tracks' sample
    order and the gate's per-probe expectations cannot drift apart."""
    return [(c, mode) for c in pairs for mode in MODES]


def c5_probes(fx: FixtureV4) -> list[tuple[str, str | None]]:
    """(query, as_of) for every v4 slot probe, in fixture order."""
    ch = probes(fx.changes)
    return ([(c.query(m), None) for c, m in ch] + [(c.query(m), c.as_of) for c, m in ch]
            + [(c.query(m), None) for c, m in probes(fx.conflicts + fx.controls)])


def track_c5(sub: Substrate, bb: Backbone, fx: FixtureV4) -> TrackResult:
    log = WriteLog(sub)
    load_v4(sub, fx, fx.changes + fx.conflicts + fx.controls, log)
    check = LineageCheck(sub, log)
    budget = fx.retrieval_budget(count_tokens)
    per, units, bad, stored = [], 0, 0, 0
    for q, t in c5_probes(fx):
        ev = sub.retrieve(q, PURPOSE, budget, as_of=t)
        miss = sum(1 for e in ev if not check.unit(e))
        units += len(ev)
        bad += miss
        stored += 0 if check.store is None else len(ev) - miss  # every source ref verified IN the store
        per.append(0.0 if miss else 1.0)
    return TrackResult("C5", len(per), {"lineage_rate": _mean(per), "n_units": float(units),
                                        "units_without_lineage": float(bad), "n_store_checked": float(stored)},
                       {"lineage": per})


def track_gov_derived(sub: Substrate, bb: Backbone, fx: FixtureV4) -> TrackResult:
    log = WriteLog(sub)
    load_v4(sub, fx, log=log)
    bad: set[str] = set()
    for d in fx.derived:
        if d.kind == "restricted":
            bad.add(log.write(Item(d.key, d.subject, d.text, fx.restricted_class)))
        else:
            ref = log.write(Item(d.key, d.subject, d.text))
            sub.forget(ref)
            bad.add(ref)
    tokens = fx.derived_tokens
    targets = {d.target.lower() for d in fx.derived}
    template = frozenset(w.lower() for w in sub.template_words)
    budget = fx.retrieval_budget(count_tokens)
    leak, prov, by_kind = [], [], {k: [] for k in ("forgotten", "restricted")}
    for d in fx.derived:
        ev = sub.retrieve(d.query, PURPOSE, budget)
        leaked, unsound = False, False
        for e in ev:
            sq = squash(e.text)
            if (bad.intersection(sources(e)) or any(t in sq for t in tokens)
                    or (squash(d.relation) in sq and squash(d.target) in sq)):  # the relation and its target together
                leaked = True
            elif unsourced(e, log, template):  # a word no source of the unit holds (REBOUND 2, N1)
                if targets.intersection(words_of(e.text)):
                    leaked = True  # ... and it names a derived target: the relation got out
                else:
                    unsound = True
        leak.append(1.0 if leaked else 0.0)
        prov.append(1.0 if unsound else 0.0)
        by_kind[d.kind].append(leak[-1])
    return TrackResult("GOV-derived", len(leak), {
        "governance_leak_rate": _mean(leak), "leak_rate_forgotten": _mean(by_kind["forgotten"]),
        "leak_rate_restricted": _mean(by_kind["restricted"]), "provenance_failure_rate": _mean(prov),
        "n_forgotten": float(len(by_kind["forgotten"])), "n_restricted": float(len(by_kind["restricted"])),
    }, {"leak": leak, "provenance": prov})


TRACKS_V4: dict[str, Callable[[Substrate, Backbone, FixtureV4], TrackResult]] = {
    **TRACKS_V3, "A2c": track_a2c, "A9": track_a9, "C5": track_c5, "GOV-derived": track_gov_derived,
}
