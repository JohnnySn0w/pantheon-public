"""PMB-1 tracks. Each is a pure scorer over (substrate, backbone, fixture) that
returns a ``TrackResult``. The runner hands every track a FRESH substrate.

Implemented in v1: A1, A2, A3, A7, A-retrieve, E3, E4. Everything else in the
PMB-1 matrix is a named stub that raises ``NotImplementedError`` and is
reported as "not run", never as passed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .backbone import ABSTAIN, Backbone, normalize
from .fixtures import PURPOSE, Fact, Fixture
from .substrate import Evidence, Item, Substrate
from .tokens import count_tokens, words

DEFAULT_BUDGET = 60  # tokens; fits the substrate's top-k on the synthetic facts
E3_BUDGETS = (4, 12, 40)  # ~1 fact, ~3 facts, ~k facts on the synthetic grammar


@dataclass(frozen=True)
class TrackResult:
    name: str
    n: int
    metrics: dict[str, float]
    samples: dict[str, list[float]] = field(default_factory=dict)  # per-probe values (v2), for the gate's intervals


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 6) if xs else 0.0


def _item(f: Fact) -> Item:
    return Item(f.key, f.subject, f.text)


def _load(sub: Substrate, facts: list[Fact]) -> dict[str, str]:
    return {f.key: sub.write(_item(f)) for f in facts}


def _ask(sub: Substrate, bb: Backbone, q: str, budget: int = DEFAULT_BUDGET) -> tuple[str, list[Evidence]]:
    ev = sub.retrieve(q, PURPOSE, budget)
    return bb.generate(q, [e.text for e in ev]), ev


def _mentions(answer: str, value: str) -> bool:
    return value.lower() in words(answer)


def track_a1(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """A1 Remember: store, then query later."""
    _load(sub, fx.base_facts)
    ok = []
    for f in fx.stable:
        ans, _ = _ask(sub, bb, f.question)
        ok.append(1.0 if bb.judge(f.question, ans, f.value) else 0.0)
    return TrackResult("A1", len(ok), {"accuracy": _mean(ok)})


def track_a_retrieve(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """A-retrieve: precision/recall against gold evidence refs, BEFORE any
    generation (the backbone is deliberately never touched)."""
    refs = _load(sub, fx.base_facts)
    prec, rec, size = [], [], []
    for f in fx.stable:
        got = [e.ref for e in sub.retrieve(f.question, PURPOSE, DEFAULT_BUDGET)]
        hit = 1.0 if refs[f.key] in got else 0.0
        rec.append(hit)
        prec.append(hit / len(got) if got else 0.0)
        size.append(float(len(got)))
    return TrackResult("A-retrieve", len(rec), {
        "precision": _mean(prec), "recall": _mean(rec), "mean_returned": _mean(size),
    })


def track_a2(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """A2 Update: store, supersede, query. New-value correctness is scored
    apart from stale returns ("hallucination of the past")."""
    refs = _load(sub, fx.base_facts)
    old_ref = {u.old.key: refs[u.old.key] for u in fx.updates}
    for u in fx.updates:
        refs[u.new.key] = sub.supersede(refs[u.old.key], _item(u.new))
    new_ok, stale_ev, stale_ans = [], [], []
    for u in fx.updates:
        q = u.new.question
        ans, ev = _ask(sub, bb, q)
        new_ok.append(1.0 if bb.judge(q, ans, u.new.value) else 0.0)
        stale_ev.append(1.0 if old_ref[u.old.key] in {e.ref for e in ev} else 0.0)
        stale_ans.append(1.0 if _mentions(ans, u.old.value) else 0.0)
    return TrackResult("A2", len(new_ok), {
        "new_value_accuracy": _mean(new_ok),
        "stale_evidence_rate": _mean(stale_ev),
        "stale_answer_rate": _mean(stale_ans),
    })


def track_a3(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """A3 Forget: store, forget, query. ``forgotten_leak_rate`` is compared with
    the AMNESIAC FLOOR by the gate; ``retained_recall`` on unrelated items is
    how a substrate that "forgets" by collapsing is caught (it scores like the
    floor there)."""
    refs = _load(sub, fx.base_facts)
    for f in fx.forgets:
        sub.forget(refs[f.key])
    leak = []
    for f in fx.forgets:
        ans, ev = _ask(sub, bb, f.question)
        leaked = (
            refs[f.key] in {e.ref for e in ev}
            or any(_mentions(e.text, f.value) for e in ev)
            or _mentions(ans, f.value)
        )
        leak.append(1.0 if leaked else 0.0)
    kept = []
    for f in fx.stable:
        got = {e.ref for e in sub.retrieve(f.question, PURPOSE, DEFAULT_BUDGET)}
        kept.append(1.0 if refs[f.key] in got else 0.0)
    return TrackResult("A3", len(leak) + len(kept), {
        "forgotten_leak_rate": _mean(leak), "retained_recall": _mean(kept),
        "n_forgotten": float(len(leak)), "n_retained": float(len(kept)),
    })


def track_a7(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """A7 Pollution: an unrelated query; count stored-value leakage."""
    _load(sub, fx.base_facts)
    values = {v.lower() for v in fx.stored_values()}
    leak = []
    for u in fx.unseen:
        ans, _ = _ask(sub, bb, u.pollution)
        leak.append(1.0 if values & set(words(ans)) else 0.0)
    return TrackResult("A7", len(leak), {"leak_rate": _mean(leak)})


def track_e3(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """E3 Purpose-bounded retrieval: tokens returned vs task success under a
    purpose + budget. ``utility`` = success * (1 - tokens/corpus), so a dump of
    the archive scores worse than a small evidence-bearing view."""
    _load(sub, fx.base_facts)
    corpus = max(1, fx.corpus_tokens(count_tokens))
    util, succ, toks, viol = [], [], [], []
    for budget in E3_BUDGETS:
        for f in fx.stable:
            ans, ev = _ask(sub, bb, f.question, budget)
            used = sum(count_tokens(e.text) for e in ev)
            ok = 1.0 if bb.judge(f.question, ans, f.value) else 0.0
            succ.append(ok)
            toks.append(float(used))
            util.append(ok * max(0.0, 1.0 - used / corpus))
            viol.append(1.0 if used > budget else 0.0)
    return TrackResult("E3", len(util), {
        "utility": _mean(util), "success": _mean(succ), "mean_tokens": _mean(toks),
        "budget_violation_rate": _mean(viol), "corpus_tokens": float(corpus),
    })


def track_e4(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
    """E4 Abstention: a question about something never stored; correct = abstain."""
    _load(sub, fx.base_facts)
    ok = []
    for u in fx.unseen:
        ans, _ = _ask(sub, bb, u.question)
        ok.append(1.0 if normalize(ans) == normalize(ABSTAIN) else 0.0)
    return TrackResult("E4", len(ok), {"abstain_rate": _mean(ok)})


TRACKS: dict[str, Callable[[Substrate, Backbone, Fixture], TrackResult]] = {
    "A1": track_a1, "A2": track_a2, "A3": track_a3, "A7": track_a7,
    "A-retrieve": track_a_retrieve, "E3": track_e3, "E4": track_e4,
}

# Out of v1 (brief section 2.2): named, unimplemented, reported "not run".
STUB_TRACKS = ("A4", "A5", "A6", "A8", "A9", "B", "C", "D", "E1", "E2", "E5")


def _make_stub(name: str) -> Callable[[Substrate, Backbone, Fixture], TrackResult]:
    def stub(sub: Substrate, bb: Backbone, fx: Fixture) -> TrackResult:
        raise NotImplementedError(f"PMB-1 track {name} is not implemented in v1")
    stub.__name__ = f"track_{name.lower()}"
    return stub


STUBS: dict[str, Callable[[Substrate, Backbone, Fixture], TrackResult]] = {n: _make_stub(n) for n in STUB_TRACKS}
