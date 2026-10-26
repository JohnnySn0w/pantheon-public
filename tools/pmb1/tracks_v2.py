"""PMB-1 tracks over the v2 episodic fixture (T-2518 step 2a).

Implemented on v2: A1, A-retrieve, A3, E3. A2 / A7 / E4 are not implemented on
v2 and are reported "not run", never as passed. Every track also returns its
PER-PROBE values (``samples``) so the gate can put an interval on a comparison.
The first / middle / recent buckets are NOMINAL: A8 is not run (every episode is
loaded up front, so a bucket is position only, not elapsed time).
Budgets are derived from the fixture (``FixtureV2.retrieval_budget`` /
``e3_budgets``), never tuned.
"""
from __future__ import annotations

from typing import Callable

from .backbone import Backbone
from .fixtures import PURPOSE
from .fixtures_v2 import FixtureV2, Probe
from .substrate import Evidence, Item, Substrate
from .tokens import count_tokens, words
from .tracks import TrackResult, _mean

V2_NOT_RUN = ("A2", "A7", "E4")
BUCKETS = ("first", "middle", "recent")
MODES = ("canonical", "paraphrase")


def _load(sub: Substrate, fx: FixtureV2) -> dict[str, str]:
    return {k: sub.write(Item(k, s, t)) for k, s, t in fx.episodes}


def _ask(sub: Substrate, bb: Backbone, p: Probe, budget: int) -> tuple[str, list[Evidence]]:
    ev = sub.retrieve(p.query, PURPOSE, budget)
    return bb.generate(p.query, [e.text for e in ev]), ev


def sources(e: Evidence) -> tuple[str, ...]:
    """Every source ref a unit stands on: its own ref plus its ``support`` (T-2518 t2). Leak and stale checks
    test all of them, so a derived unit cannot hide a forgotten or unauthorized source behind a clean ref."""
    return (e.ref, *e.support)


def relevant_units(p: Probe, ev: list[Evidence], gold_ref: str) -> int:
    """Precision with sub-item evidence: a unit is relevant iff its backlink
    resolves to the gold source AND it contains the gold value."""
    return sum(1 for e in ev if e.ref == gold_ref and p.value in words(e.text))


def _by(probes: list[Probe], vals: list[float], attr: str, names: tuple[str, ...]) -> dict[str, float]:
    return {
        f"{n}": _mean([v for p, v in zip(probes, vals) if getattr(p, attr) == n]) for n in names
    }


def track_a1(sub: Substrate, bb: Backbone, fx: FixtureV2) -> TrackResult:
    """A1 Remember, over episodes; canonical vs paraphrase queries reported apart."""
    _load(sub, fx)
    budget = fx.retrieval_budget(count_tokens)
    ok = []
    for p in fx.probes:
        ans, _ = _ask(sub, bb, p, budget)
        ok.append(1.0 if bb.judge(p.query, ans, p.value) else 0.0)
    m = {"accuracy": _mean(ok)}
    m.update({f"accuracy_{k}": v for k, v in _by(fx.probes, ok, "mode", MODES).items()})
    return TrackResult("A1", len(ok), m, {"accuracy": ok})


def track_a_retrieve(sub: Substrate, bb: Backbone, fx: FixtureV2) -> TrackResult:
    """A-retrieve: recall by backlink to the gold episode; precision counts a
    unit relevant only if its backlink is gold AND it holds the gold value.
    Recall is also bucketed first / middle / recent (the A8-style curve)."""
    refs = _load(sub, fx)
    budget = fx.retrieval_budget(count_tokens)
    rec, prec, size = [], [], []
    for p in fx.probes:
        ev = sub.retrieve(p.query, PURPOSE, budget)
        rec.append(1.0 if refs[p.episode] in {e.ref for e in ev} else 0.0)
        prec.append(relevant_units(p, ev, refs[p.episode]) / len(ev) if ev else 0.0)
        size.append(float(len(ev)))
    m = {"precision": _mean(prec), "recall": _mean(rec), "mean_returned": _mean(size)}
    m.update({f"recall_{k}": v for k, v in _by(fx.probes, rec, "bucket", BUCKETS).items()})
    m.update({f"recall_{k}": v for k, v in _by(fx.probes, rec, "mode", MODES).items()})
    return TrackResult("A-retrieve", len(rec), m, {"recall": rec})


def track_a3(sub: Substrate, bb: Backbone, fx: FixtureV2) -> TrackResult:
    """A3 Forget on v2: the forget probes' episodes are forgotten, then queried.
    Each forgotten value lives only in its own episode, so any appearance of it
    (backlink, evidence text, answer) is a leak. ``retained_recall`` on the
    other probes catches a substrate that forgets by collapsing."""
    refs = _load(sub, fx)
    for p in fx.forgets:
        sub.forget(refs[p.episode])
    budget = fx.retrieval_budget(count_tokens)
    leak = []
    for p in fx.forgets:
        ans, ev = _ask(sub, bb, p, budget)
        leaked = (
            refs[p.episode] in {r for e in ev for r in sources(e)}
            or any(p.value in words(e.text) for e in ev)
            or p.value in words(ans)
        )
        leak.append(1.0 if leaked else 0.0)
    kept = []
    for p in fx.probes:
        got = {e.ref for e in sub.retrieve(p.query, PURPOSE, budget)}
        kept.append(1.0 if refs[p.episode] in got else 0.0)
    return TrackResult("A3", len(leak) + len(kept), {
        "forgotten_leak_rate": _mean(leak), "retained_recall": _mean(kept),
        "n_forgotten": float(len(leak)), "n_retained": float(len(kept)),
    })


def track_e3(sub: Substrate, bb: Backbone, fx: FixtureV2) -> TrackResult:
    """E3 on v2: utility = success * (1 - tokens/corpus) at three derived
    budgets. ``samples['utility']`` is one value per probe (mean over the
    budgets), so a resample over probes is a resample over independent units."""
    _load(sub, fx)
    corpus = max(1, fx.corpus_tokens(count_tokens))
    budgets = fx.e3_budgets(count_tokens)
    util, succ, toks, viol = [], [], [], []
    per_probe: list[list[float]] = [[] for _ in fx.probes]
    for budget in budgets:
        for i, p in enumerate(fx.probes):
            ans, ev = _ask(sub, bb, p, budget)
            used = sum(count_tokens(e.text) for e in ev)
            ok = 1.0 if bb.judge(p.query, ans, p.value) else 0.0
            u = ok * max(0.0, 1.0 - used / corpus)
            util.append(u)
            per_probe[i].append(u)
            succ.append(ok)
            toks.append(float(used))
            viol.append(1.0 if used > budget else 0.0)
    return TrackResult("E3", len(util), {
        "utility": _mean(util), "success": _mean(succ), "mean_tokens": _mean(toks),
        "budget_violation_rate": _mean(viol), "corpus_tokens": float(corpus),
    }, {"utility": [_mean(x) for x in per_probe]})


TRACKS_V2: dict[str, Callable[[Substrate, Backbone, FixtureV2], TrackResult]] = {
    "A1": track_a1, "A3": track_a3, "A-retrieve": track_a_retrieve, "E3": track_e3,
}
