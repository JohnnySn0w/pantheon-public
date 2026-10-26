"""The runner: substrates x backbones over one fixture -> run records.

A run marked ``gate=True`` REQUIRES backbones of at least two different
``family`` values (rule 6.1.4) and is refused otherwise. The runner also
reports whether the substrate rank order is stable across families.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .backbone import Backbone
from .embedder import Embedder, HashingEmbedder, is_lexical_stub
from .fixtures import Fixture
from .fixtures_v2 import FixtureV2
from .fixtures_v3 import FixtureV3
from .records import SCHEMA, validate_record, write_record
from .substrate import (
    AmnesiacFloor, FullContextCeiling, RandomRetrievalControl, Substrate, VerbatimRAGBaseline,
)
from .tracks import STUBS, TRACKS
from .tracks_v2 import TRACKS_V2, V2_NOT_RUN
from .tracks_v3 import TRACKS_V3, V3_NOT_RUN
from .tracks_v4 import TRACKS_V4, V4_NOT_RUN, is_pattern_stub_extractor

# Primary metric per track used for the composite rank (higher is better).
RANK_METRICS = (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility"))


class GateRunRefused(ValueError):
    """A gate run that does not satisfy the harness's own preconditions."""


def _store_kwargs(fx: Fixture) -> dict:
    """v3 declares the run's privacy ceiling (its restricted episodes sit above it); v1 / v2 keep the default.
    One helper for every store-backed substrate, so a tier over governed cannot drift from governed."""
    return {"privacy_ceiling": fx.privacy_ceiling} if hasattr(fx, "privacy_ceiling") else {}


def _governed(fx: Fixture) -> Substrate:
    from .governed import GovernedStoreSubstrate  # imports the store lazily
    return GovernedStoreSubstrate(**_store_kwargs(fx))


def _episodic(fx: Fixture, embedder: Embedder | None = None) -> Substrate:
    """The ORG-062 tier-1 episodic view. Default: lexical (unchanged); an embedder switches on its semantic leg."""
    from .episodic import EpisodicTierSubstrate  # imports the store lazily
    return EpisodicTierSubstrate(**_store_kwargs(fx), **({} if embedder is None else {"embedder": embedder}))


def _semantic(fx: Fixture, embedder: Embedder | None = None) -> Substrate:
    """The ORG-062 tier-2 semantic/relational slot view (T-2518 S4), over the episodic tier."""
    from .semantic import SemanticTierSubstrate  # imports the store lazily
    return SemanticTierSubstrate(**_store_kwargs(fx), **({} if embedder is None else {"embedder": embedder}))


def _rag(fx: Fixture, embedder: Embedder | None = None) -> Substrate:
    """The verbatim-RAG baseline. Default: the hashing stub (unchanged); the realrun driver passes a real
    (cached, replay-only) embedder through ``make_substrate``."""
    return VerbatimRAGBaseline(embedder if embedder is not None else HashingEmbedder())


SUBSTRATES: dict[str, Callable[..., Substrate]] = {
    "governed": _governed,
    "episodic": _episodic,
    "semantic": _semantic,
    "rag": _rag,
    "random": lambda fx: RandomRetrievalControl(seed=fx.seed),
    "full": lambda fx: FullContextCeiling(),
    "amnesiac": lambda fx: AmnesiacFloor(),
}

# The substrates that embed: only these take an ``embedder`` (every factory still takes ``fx`` alone).
EMBEDDING_SUBSTRATES = frozenset({"rag", "episodic", "semantic"})


def make_substrate(name: str, fx: Fixture, embedder: Embedder | None = None) -> Substrate:
    """``SUBSTRATES[name](fx)``, with ``embedder`` threaded to the substrates that embed (ignored by the rest)."""
    if embedder is not None and name in EMBEDDING_SUBSTRATES:
        return SUBSTRATES[name](fx, embedder)
    return SUBSTRATES[name](fx)


def _version(fixture: Fixture | FixtureV2 | FixtureV3) -> int:
    return getattr(fixture, "version", 1)


def _not_run(backbone: Backbone, fixture: Fixture | FixtureV2) -> list[str]:
    """Stub tracks are invoked for real: NotImplementedError is what puts a
    track in "not run". A stub that silently returned would fail loudly here."""
    out = []
    for name, stub in STUBS.items():
        if _version(fixture) >= 4 and name in TRACKS_V4:
            continue  # v4 runs A9 for real (T-2518 tier 2); v1-v3 keep their list unchanged
        try:
            stub(AmnesiacFloor(), backbone, fixture)
        except NotImplementedError:
            out.append(name)
        else:
            raise RuntimeError(f"stub track {name} returned instead of raising NotImplementedError")
    return out + {2: list(V2_NOT_RUN), 3: list(V3_NOT_RUN), 4: list(V4_NOT_RUN)}.get(_version(fixture), [])


def run_one(
    make_substrate: Callable[[], Substrate], backbone: Backbone, fixture: Fixture | FixtureV2 | FixtureV3, *, gate: bool,
    tracks: tuple[str, ...] | None = None, embedder_is_lexical_stub: bool = False,
    extractor_is_pattern_stub: bool = False,
) -> dict:
    """One record for one (substrate, backbone). Each track gets a fresh substrate.

    ``extractor_is_pattern_stub`` (v4, R6): the run declares that a substrate's relation extractor is a pattern
    stub (it knows the corpus grammar), the way ``embedder_is_lexical_stub`` declares the hashing embedder."""
    v2 = _version(fixture) >= 2  # v2 and v3 records carry per-probe samples
    table = {4: TRACKS_V4, 3: TRACKS_V3, 2: TRACKS_V2}.get(_version(fixture), TRACKS)
    names = tracks or tuple(table)
    scores: dict[str, dict] = {}
    samples: dict[str, list[float]] = {}
    cost: dict[str, float] = {}
    describe: dict | None = None
    template: list[str] = []
    merged: dict[str, dict[str, int]] = {}  # dict-of-int extras (e.g. lifecycle outcomes), summed over tracks
    bb_before = backbone.calls
    for name in names:
        sub = make_substrate()
        try:
            res = table[name](sub, backbone, fixture)
            d = sub.describe()
            template = template or list(sub.template_words)
        finally:
            sub.close()
        scores[name] = {"n": res.n, "metrics": res.metrics}
        samples.update({f"{name}.{k}": [round(x, 6) for x in v] for k, v in res.samples.items()})
        for k, v in d["cost"].items():
            cost[k] = cost.get(k, 0) + v
        describe = describe or d
        for k, v in d.items():
            if k != "cost" and isinstance(v, dict) and all(isinstance(x, int) for x in v.values()):
                for kk, x in v.items():
                    merged.setdefault(k, {})[kk] = merged.get(k, {}).get(kk, 0) + x
    assert describe is not None
    cost = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in cost.items()}
    cost["backbone_calls"] = backbone.calls - bb_before
    describe = {k: v for k, v in describe.items() if k != "cost"}
    describe.update({k: dict(sorted(v.items())) for k, v in merged.items()})
    not_run = _not_run(backbone, fixture)
    extra: dict = {}
    if v2:
        extra = {
            "samples": samples,
            "reader_knows_lexicon": bool(backbone.knows_lexicon),  # a stub reader may know the paraphrase lexicon
            "embedder_is_lexical_stub": bool(embedder_is_lexical_stub),
        }
        if _version(fixture) >= 4:
            extra["extractor_is_pattern_stub"] = bool(extractor_is_pattern_stub)
            extra["template_words"] = template  # REBOUND 3: declared output-template words (the gate validates)
    return {
        **extra,
        "schema": SCHEMA, "gate": gate, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "substrate": describe, "backbone": {"name": backbone.name, "family": backbone.family},
        "embedding_model": describe.get("embedding_model"),
        "fixture": {"id": fixture.id, "seed": fixture.seed, "size": fixture.size, "sha256": fixture.sha256,
                    **({"version": _version(fixture)} if v2 else {})},
        "tracks": scores, "not_run": not_run, "cost": cost,
    }


def composite(rec: dict) -> float:
    vals = [rec["tracks"][t]["metrics"][m] for t, m in RANK_METRICS if t in rec["tracks"]]
    return round(sum(vals) / len(vals), 6) if vals else 0.0


def rank_stability(records: list[dict]) -> dict:
    """Is the substrate rank order the same under every backbone family?"""
    fams: dict[str, dict[str, float]] = {}
    for r in records:
        fams.setdefault(r["backbone"]["family"], {})[r["substrate"]["name"]] = composite(r)
    orders = {f: [n for n, _ in sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))] for f, s in fams.items()}
    if len(fams) < 2:
        return {"stable": None, "families": sorted(fams), "orders": orders, "note": "n/a: fewer than 2 families"}
    first = next(iter(orders.values()))
    return {"stable": all(o == first for o in orders.values()), "families": sorted(fams), "orders": orders,
            "composite": fams}


def run(
    substrates: list[str], backbones: list[Backbone], fixture: Fixture | FixtureV2 | FixtureV3, *, gate: bool = False,
    out_dir: Path | None = None, embedder_is_lexical_stub: bool = False, extractor_is_pattern_stub: bool = False,
) -> tuple[list[dict], dict]:
    if gate and len({b.family for b in backbones}) < 2:
        raise GateRunRefused(
            "a gate run needs backbones of at least 2 different families (rule 6.1.4); "
            f"got {sorted({b.family for b in backbones})}"
        )
    unknown = [s for s in substrates if s not in SUBSTRATES]
    if unknown:
        raise ValueError(f"unknown substrate(s) {unknown}; one of {sorted(SUBSTRATES)}")
    if gate and _version(fixture) >= 2 and not embedder_is_lexical_stub:
        for s in substrates:
            probe = SUBSTRATES[s](fixture)
            try:
                if is_lexical_stub(probe.embedding_model):
                    raise GateRunRefused(
                        f"a v2 gate run refuses the lexical stub embedder ({probe.embedding_model}) unless the run "
                        "is declared embedder_is_lexical_stub (its RAG comparisons are then marked 'vs a lexical stub')"
                    )
            finally:
                probe.close()
    if gate and _version(fixture) >= 4 and not extractor_is_pattern_stub:
        for s in substrates:
            probe = SUBSTRATES[s](fixture)
            try:
                if is_pattern_stub_extractor(probe.describe().get("extractor")):
                    raise GateRunRefused(
                        f"a v4 gate run refuses a pattern-stub extractor ({probe.describe().get('extractor')}) unless "
                        "the run is declared extractor_is_pattern_stub (R6)"
                    )
            finally:
                probe.close()
    records = []
    for s in substrates:
        for b in backbones:
            rec = run_one(lambda s=s: SUBSTRATES[s](fixture), b, fixture, gate=gate,
                          embedder_is_lexical_stub=embedder_is_lexical_stub,
                          extractor_is_pattern_stub=extractor_is_pattern_stub)
            problems = validate_record(rec)
            if problems:
                raise RuntimeError(f"runner produced an invalid record: {problems}")
            records.append(rec)
            if out_dir is not None:
                write_record(rec, out_dir)
    stability = rank_stability(records)
    if out_dir is not None:
        (out_dir / "rank_stability.json").write_text(json.dumps(stability, indent=2, sort_keys=True) + "\n")
    return records, stability
