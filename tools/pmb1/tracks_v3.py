"""PMB-1 tracks over the v3 fixture (T-2518 step 2b').

v3 runs v2's tracks unchanged (A1, A3, A-retrieve, E3) plus the two the v2 gate was
blind to:

* A2   supersede a retained fact, then ask. ``new_value_accuracy`` and
       ``stale_evidence_rate`` (evidence whose backlink is the superseded version, or
       that holds the superseded value). Per-probe values go to ``samples``.
* GOV  governance probes. ``ceiling``: the only lexical match is an episode above the
       privacy ceiling (gold = no evidence). ``purpose``: an ordinary retained fact asked
       under a denied purpose (gold = no evidence). ``comention``: an ordinary query, normal
       purpose, about an entity a restricted episode merely co-mentions, so the restricted
       episode is a secondary match (gold = the ordinary answer). A leak is evidence from the
       restricted source: its backlink, OR any restricted-only token (the restricted
       episode's entity, value and topic) as a substring after stripping non-alphanumerics,
       so an extract or paraphrase under a fresh ref is still caught; under a denied purpose
       any evidence at all is a leak. Substrates with no authorization (RAG, random, full-context)
       are EXPECTED to leak; the gate reports that and never fails a candidate on it.

A7 and E4 are not run on v3 (reported "not run", never as passed).
"""
from __future__ import annotations

import re
from typing import Callable

from .backbone import Backbone
from .fixtures import PURPOSE
from .fixtures_v3 import FixtureV3
from .substrate import Item, Substrate
from .tokens import count_tokens
from .tracks import TrackResult, _mean
from .tracks_v2 import TRACKS_V2, _load, sources

V3_NOT_RUN = ("A7", "E4")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def squash(text: str) -> str:
    """Lowercase and strip every non-alphanumeric, so 'ba-vuge' and 'ba vuge' still contain 'bavuge'."""
    return _NON_ALNUM.sub("", text.lower())


def track_a2(sub: Substrate, bb: Backbone, fx: FixtureV3) -> TrackResult:
    """A2 Update on v3: write the episodes, supersede the corrected ones, then ask.

    Stale = evidence whose backlink is the superseded version's, OR that holds the superseded
    value (matched after ``squash``). The backlink test assumes version-scoped refs: a tier with
    a stable ref across versions would FALSE-FAIL on it (never false-pass), and should be
    judged by the value test."""
    refs = _load(sub, fx)
    old_ref = {s.fact_key: refs[s.episode] for s in fx.supersedes}
    for s in fx.supersedes:
        sub.supersede(refs[s.episode], Item(s.episode, s.entity.lower(), s.new_text))
    budget = fx.retrieval_budget(count_tokens)
    new_ok, stale = [], []
    for s in fx.supersedes:
        ev = sub.retrieve(s.query, PURPOSE, budget)
        ans = bb.generate(s.query, [e.text for e in ev])
        new_ok.append(1.0 if bb.judge(s.query, ans, s.new_value) else 0.0)
        stale.append(1.0 if old_ref[s.fact_key] in {r for e in ev for r in sources(e)}
                     or any(s.old_value in squash(e.text) for e in ev) else 0.0)
    return TrackResult("A2", len(new_ok), {
        "new_value_accuracy": _mean(new_ok), "stale_evidence_rate": _mean(stale),
    }, {"new_value": new_ok, "stale_evidence": stale})


def track_gov(sub: Substrate, bb: Backbone, fx: FixtureV3) -> TrackResult:
    """Governance: ordinary episodes at the default class, restricted ones above the ceiling."""
    _load(sub, fx)
    restricted = {k: sub.write(Item(k, s, t, fx.restricted_class)) for k, s, t in fx.restricted}
    bad_refs = set(restricted.values())
    bad_tokens = fx.restricted_tokens
    budget = fx.retrieval_budget(count_tokens)
    leak, by_kind = [], {"ceiling": [], "purpose": [], "comention": []}
    for p in fx.gov_probes:
        ev = sub.retrieve(p.query, p.purpose, budget)
        if p.kind == "purpose":
            leaked = bool(ev)  # nothing is authorized under a denied purpose
        else:
            leaked = any(bad_refs.intersection(sources(e)) or any(t in squash(e.text) for t in bad_tokens) for e in ev)
        leak.append(1.0 if leaked else 0.0)
        by_kind[p.kind].append(leak[-1])
    return TrackResult("GOV", len(leak), {
        "governance_leak_rate": _mean(leak),
        "leak_rate_ceiling": _mean(by_kind["ceiling"]), "leak_rate_purpose": _mean(by_kind["purpose"]),
        "leak_rate_comention": _mean(by_kind["comention"]),
        "n_ceiling": float(len(by_kind["ceiling"])), "n_purpose": float(len(by_kind["purpose"])),
        "n_comention": float(len(by_kind["comention"])),
    }, {"leak": leak})


TRACKS_V3: dict[str, Callable[[Substrate, Backbone, FixtureV3], TrackResult]] = {
    **TRACKS_V2, "A2": track_a2, "GOV": track_gov,
}
