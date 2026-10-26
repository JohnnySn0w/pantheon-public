"""PMB-1 fixture v2 (T-2518 step 2a): episodic and discriminating (D11: spec-derived).

v1 saturates (governed = RAG = 1.0), so no tier could show it is better or
worse. v2 keeps v1's token discipline (fixed-length, distinct, never a
substring of another) but embeds each fact in a multi-sentence dated episode
and adds the axes below. Selected by ``version=2`` in the fixture spec; the
default stays v1, whose bytes and sha are untouched.

Every parameter below is fixed from the spec BEFORE any measurement and is
never per-substrate or tuned to a winner. Each is part of the fixture sha.

Axes (the spec line each one is derived from):

* episodes     facts live inside 3-8-sentence dated records (ORG-062 s2.1
               "tickets, reports, ledger"; report s6.6 E3 "small view vs dump").
* paraphrase   about half the probe queries omit the attribute word, via the
               fixed ``PARAPHRASE`` lexicon (report s6.6 "workloads that resemble
               real conversations rather than only synthetic QA").
* distractors  0-3 sentences per probed fact name the same entity+attr with no
               value, spread over the episodes by position (report s6.2 A5
               "overlapping schemas": cross-entity attribution).
* timeline     episodes are written in time order; probes are bucketed
               first / middle / recent (report s6.2 A8 "reports a curve").
               NOMINAL: every episode is loaded up front, so the bucket is
               position only; A8 proper (a curve over elapsed time) is not run.
* scale        sizes up to xlarge (report s6.1.3 full-context cost; E3).
* forget       one fact per entity is a forget probe (report s6.2 A3), so an
               A3 run on v2 exercises forget.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

from .fixtures import ATTRS, _CONS, _VOW, Fixture, generate

VERSION = 2
SIZES = {"tiny": 4, "small": 10, "medium": 30, "large": 100, "xlarge": 300}  # entities
PARAPHRASE = {  # query templates with NO attribute word; the reader's lexicon maps them back
    "beverage": "What does {e} like to drink?",
    "vehicle": "How does {e} usually get around?",
    "instrument": "What does {e} play for music?",
    "snack": "What does {e} munch on between meals?",
    "hobby": "What does {e} do for fun?",
    "pet": "What animal lives with {e}?",
}
FILLER = (
    "{a} and {b} talked about the {t} for a while.",
    "Later {a} went over to the {t} with {b}.",
    "{a} asked {b} how the {t} went.",
    "Nobody could find the {t} that evening.",
)
DISTRACTOR = "{a} mentioned {e}'s {attr} in passing."
P_PARAPHRASE = 0.5
MAX_DISTRACTORS = 3    # per probed fact, uniform 0..MAX
FILLER_RANGE = (2, 5)  # filler sentences per episode
_PARAMS = {"p_paraphrase": P_PARAPHRASE, "max_distractors": MAX_DISTRACTORS, "filler_range": list(FILLER_RANGE),
           "attrs": list(ATTRS[:5]), "paraphrase": PARAPHRASE, "filler": list(FILLER), "distractor": DISTRACTOR}
RETRIEVAL_TOP_K_EPISODES = 5  # v1's own rule: the A1 / A-retrieve budget "fits top-k" (here, top-k median episodes)


@dataclass(frozen=True)
class Probe:
    fact_key: str
    episode: str
    entity: str
    attr: str
    value: str
    mode: str      # canonical | paraphrase
    bucket: str    # first | middle | recent

    @property
    def query(self) -> str:
        if self.mode == "paraphrase":
            return PARAPHRASE[self.attr].format(e=self.entity)
        return f"What is {self.entity}'s {self.attr}?"

    @property
    def fact_sentence(self) -> str:
        return f"{self.entity}'s {self.attr} is {self.value}."


@dataclass
class FixtureV2:
    id: str
    seed: int
    size: str
    episodes: list[tuple[str, str, str]] = field(default_factory=list)  # (key, subject, text) in time order
    probes: list[Probe] = field(default_factory=list)        # retained facts: A1 / A-retrieve / E3 / A3 retained
    forgets: list[Probe] = field(default_factory=list)       # forget probes: A3

    version = VERSION

    def corpus_tokens(self, count) -> int:
        return sum(count(t) for _, _, t in self.episodes)

    def _median_episode(self, count) -> int:
        lens = sorted(count(t) for _, _, t in self.episodes)
        return lens[len(lens) // 2]

    def retrieval_budget(self, count) -> int:
        """A1 / A-retrieve budget, DERIVED: 5 x the median episode (v1's rule
        "fits top-k", with the episode as the retrieved unit). A pure function of
        the fixture, pinned by rule and not by value."""
        return RETRIEVAL_TOP_K_EPISODES * self._median_episode(count)

    def e3_budgets(self, count) -> tuple[int, int, int]:
        """E3 budgets, DERIVED: (one median fact sentence, one median episode,
        5 x median episode). Pure functions of the fixture, never tuned."""
        facts = sorted(count(p.fact_sentence) for p in self.probes + self.forgets)
        med = self._median_episode(count)
        return (facts[len(facts) // 2], med, RETRIEVAL_TOP_K_EPISODES * med)

    def to_dict(self) -> dict:
        return {"version": VERSION, "seed": self.seed, "size": self.size, "params": _PARAMS,
                "episodes": self.episodes,
                "probes": [p.__dict__ for p in self.probes], "forgets": [p.__dict__ for p in self.forgets]}

    @property
    def sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def generate_v2(seed: int = 1, size: str = "small") -> FixtureV2:
    if size not in SIZES:
        raise ValueError(f"unknown fixture size {size!r}; one of {sorted(SIZES)}")
    rng = random.Random(f"pmb1v2/{seed}/{size}")
    n = SIZES[size]
    seen: set[str] = set()
    pool: list[str] = []
    while len(pool) < n * 8 + 40:
        t = "".join(rng.choice(_CONS) + rng.choice(_VOW) for _ in range(3))
        if t not in seen:
            seen.add(t)
            pool.append(t)
    take = iter(pool)
    ents = [next(take).capitalize() for _ in range(n)]
    topics = [next(take) for _ in range(max(8, n // 2))]
    forget_at = {e: rng.randrange(5) for e in ents}  # which attr of each entity is the forget probe
    facts = []  # (key, entity, attr, value, role)
    for ei, e in enumerate(ents):
        for ai, attr in enumerate(ATTRS[:5]):
            facts.append((f"e{ei:03d}-{ai}", e, attr, next(take), "forget" if forget_at[e] == ai else "keep"))
    rng.shuffle(facts)  # the timeline
    plant = []  # distractor sentences, one batch per probed fact
    for _, e, attr, _, _ in facts:
        for _ in range(rng.randint(0, MAX_DISTRACTORS)):
            other = rng.choice([x for x in ents if x != e])
            plant.append(DISTRACTOR.format(a=other, e=e, attr=attr))
    rng.shuffle(plant)
    per_ep = -(-len(plant) // len(facts))
    fx = FixtureV2(id=f"synthetic-v2-seed{seed}-{size}", seed=seed, size=size)
    ep_of = {}
    for i, (key, e, attr, v, _) in enumerate(facts):
        others = [x for x in ents if x != e]
        sents = [f"{e}'s {attr} is {v}."]
        for _ in range(rng.randint(*FILLER_RANGE)):
            sents.append(rng.choice(FILLER).format(a=e, b=rng.choice(others), t=rng.choice(topics)))
        sents += plant[i * per_ep:(i + 1) * per_ep]
        rng.shuffle(sents)
        ek = f"ep{i:04d}"
        ep_of[key] = (ek, i)
        fx.episodes.append((ek, e.lower(), f"Day {i + 1}. " + " ".join(sents)))
    third = len(facts) / 3
    for key, e, attr, v, role in sorted(facts):
        ek, i = ep_of[key]
        mode = "paraphrase" if rng.random() < P_PARAPHRASE else "canonical"
        bucket = "first" if i < third else ("middle" if i < 2 * third else "recent")
        (fx.forgets if role == "forget" else fx.probes).append(Probe(key, ek, e, attr, v, mode, bucket))
    return fx


def parse_fixture_spec(spec: str) -> tuple[int, str, int]:
    """``seed=1,size=small,version=2`` -> (1, "small", 2); version defaults to 1."""
    kv = dict(p.split("=", 1) for p in spec.split(",") if p)
    seed, size, version = int(kv.get("seed", "1")), kv.get("size", "small"), int(kv.get("version", "1"))
    if version not in (1, 2):
        raise ValueError(f"unknown fixture version {version!r}; one of [1, 2]")
    sizes = SIZES if version == 2 else {"tiny", "small", "medium", "large"}
    if size not in sizes:
        raise ValueError(f"unknown fixture size {size!r} for version {version}; one of {sorted(sizes)}")
    return seed, size, version


def generate_from_spec(spec: str) -> Fixture | FixtureV2:
    seed, size, version = parse_fixture_spec(spec)
    return generate_v2(seed, size) if version == 2 else generate(seed, size)
