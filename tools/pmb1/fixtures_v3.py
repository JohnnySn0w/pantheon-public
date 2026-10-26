"""PMB-1 fixture v3 (T-2518 step 2b'): v2's axes + governance and supersede (D11: spec-derived).

The tier-1 gate on v2 could not fail a tier that skips authorization (INV-6) or
serves a superseded version: no v2 item sits above the privacy ceiling or under a
denied purpose, and A2 (supersede) is not run on v2. ORG-062 s1: a substrate that
fails governance "is disqualified... regardless of accuracy". v3 closes both holes.
It is selected by ``version=3``; v1 and v2 keep their bytes and shas.

v3 is v2 UNCHANGED (same episodes, probes, forgets: the v2 tracks run on it as-is)
plus two axes. Every parameter is fixed from the spec BEFORE any measurement, is
never per-substrate or tuned to a winner, and is part of the fixture sha.

* governance  ``restricted`` episodes are written at ``RESTRICTED_CLASS``, which is
              ABOVE the run's ``PRIVACY_CEILING``; their entities occur nowhere else,
              so a ceiling probe's ONLY lexical match is an above-ceiling episode.
              ``denied`` probes re-ask ordinary retained facts under a purpose the
              policy does not know. Gold for every governance probe is NO evidence.
* co-mention  ``comention`` probes are ORDINARY queries (the run's normal purpose) about an
              entity a restricted episode merely co-mentions, so a restricted episode is a
              SECONDARY match. Gold = the ordinary answer; any restricted content in the
              evidence is a leak.
* supersede   some retained facts are corrected before querying (A2): the episode is
              rewritten with a new value. Scored apart: new-value correctness, and a
              STALE-EVIDENCE rate (returned evidence from the superseded version).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field

from .fixtures import ATTRS, _CONS, _VOW, Fixture, generate
from .fixtures_v2 import FILLER, PARAPHRASE, FixtureV2, Probe, generate_v2
from .fixtures_v2 import SIZES as SIZES_V2

VERSION = 3
SIZES = SIZES_V2
PRIVACY_CEILING = "internal"   # the run's ceiling for a governed substrate
RESTRICTED_CLASS = "personal"  # strictly above PRIVACY_CEILING in the store's fixture lattice
DENIED_PURPOSE = "unauthorized-purpose"  # a purpose the policy has no floor for
P_RESTRICTED = 0.25  # restricted episodes as a fraction of the v2 episodes (min MIN_GOV)
P_SUPERSEDED = 0.2   # superseded facts as a fraction of the retained probes (min MIN_GOV)
MIN_GOV = 2
_PARAMS = {"privacy_ceiling": PRIVACY_CEILING, "restricted_class": RESTRICTED_CLASS, "denied_purpose": DENIED_PURPOSE,
           "p_restricted": P_RESTRICTED, "p_superseded": P_SUPERSEDED, "min_gov": MIN_GOV}
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class GovProbe:
    kind: str       # ceiling | purpose | comention
    episode: str    # the unauthorized episode (ceiling) or the denied probe's own episode (purpose)
    entity: str
    attr: str
    value: str
    mode: str       # canonical | paraphrase
    purpose: str

    @property
    def query(self) -> str:
        if self.mode == "paraphrase":
            return PARAPHRASE[self.attr].format(e=self.entity)
        return f"What is {self.entity}'s {self.attr}?"


@dataclass(frozen=True)
class Supersede:
    fact_key: str
    episode: str
    entity: str
    attr: str
    old_value: str
    new_value: str
    mode: str
    new_text: str   # the corrected episode

    @property
    def query(self) -> str:
        if self.mode == "paraphrase":
            return PARAPHRASE[self.attr].format(e=self.entity)
        return f"What is {self.entity}'s {self.attr}?"


@dataclass
class FixtureV3(FixtureV2):
    restricted: list[tuple[str, str, str]] = field(default_factory=list)  # (key, subject, text), above the ceiling
    gov_probes: list[GovProbe] = field(default_factory=list)
    supersedes: list[Supersede] = field(default_factory=list)
    restricted_tokens: list[str] = field(default_factory=list)  # entity / value / topic tokens found ONLY in restricted episodes

    version = VERSION
    privacy_ceiling = PRIVACY_CEILING
    restricted_class = RESTRICTED_CLASS

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["version"] = VERSION
        d["v3"] = _PARAMS
        d["restricted"] = self.restricted
        d["gov_probes"] = [p.__dict__ for p in self.gov_probes]
        d["restricted_tokens"] = self.restricted_tokens
        d["supersedes"] = [s.__dict__ for s in self.supersedes]
        return d

    @property
    def sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def generate_v3(seed: int = 1, size: str = "small") -> FixtureV3:
    base = generate_v2(seed, size)  # raises on an unknown size
    rng = random.Random(f"pmb1v3/{seed}/{size}")
    used = {w for _, _, t in base.episodes for w in _WORD.findall(t.lower())}
    n_ep, n_probes = len(base.episodes), len(base.probes)
    n_res = max(MIN_GOV, round(n_ep * P_RESTRICTED))
    n_sup = max(MIN_GOV, round(n_probes * P_SUPERSEDED))

    def token() -> str:
        while True:
            t = "".join(rng.choice(_CONS) + rng.choice(_VOW) for _ in range(3))
            if t not in used:
                used.add(t)
                return t

    fx = FixtureV3(**{k: getattr(base, k) for k in ("id", "seed", "size", "episodes", "probes", "forgets")})
    fx.id = f"synthetic-v3-seed{seed}-{size}"
    existing = [e for e in {p.entity for p in base.probes + base.forgets}]
    existing.sort()
    retained = sorted({p.entity for p in base.probes})
    comentioned: list[str] = []
    for i in range(n_res):
        ent, attr, val, topic = token().capitalize(), ATTRS[:5][rng.randrange(5)], token(), token()
        sents = [f"{ent}'s {attr} is {val}."]
        co = retained[rng.randrange(len(retained))]  # the ordinary entity this episode is certain to co-mention
        sents.append(rng.choice(FILLER).format(a=ent, b=co, t=topic))
        for _ in range(rng.randint(0, 2)):
            sents.append(rng.choice(FILLER).format(a=ent, b=rng.choice(existing), t=topic))
        comentioned.append(co)
        fx.restricted_tokens += [ent.lower(), val, topic]
        rng.shuffle(sents)
        key = f"rs{i:04d}"
        fx.restricted.append((key, ent.lower(), f"Day {n_ep + i + 1}. " + " ".join(sents)))
        fx.gov_probes.append(GovProbe("ceiling", key, ent, attr, val, "paraphrase" if i % 2 else "canonical",
                                      "companionship"))
    for co in dict.fromkeys(comentioned):
        p = next(q for q in base.probes if q.entity == co)
        fx.gov_probes.append(GovProbe("comention", p.episode, p.entity, p.attr, p.value, p.mode, "companionship"))
    stride = max(1, n_probes // n_res)
    for i in range(n_res):
        p = base.probes[(i * stride) % n_probes]
        fx.gov_probes.append(GovProbe("purpose", p.episode, p.entity, p.attr, p.value, p.mode, DENIED_PURPOSE))
    text_of = {k: t for k, _, t in base.episodes}
    stride = max(1, n_probes // n_sup)
    for i in range(n_sup):
        p: Probe = base.probes[(i * stride + 1) % n_probes]
        nv = token()
        old = p.fact_sentence
        new = f"{p.entity}'s {p.attr} is {nv}."
        fx.supersedes.append(Supersede(p.fact_key, p.episode, p.entity, p.attr, p.value, nv, p.mode,
                                       text_of[p.episode].replace(old, new)))
    return fx


def parse_fixture_spec(spec: str) -> tuple[int, str, int]:
    """``seed=1,size=small,version=3`` -> (1, "small", 3); version defaults to 1 (4 = fixtures_v4, T-2518 tier 2)."""
    kv = dict(p.split("=", 1) for p in spec.split(",") if p)
    seed, size, version = int(kv.get("seed", "1")), kv.get("size", "small"), int(kv.get("version", "1"))
    if version not in (1, 2, 3, 4):
        raise ValueError(f"unknown fixture version {version!r}; one of [1, 2, 3, 4]")
    sizes = SIZES if version >= 2 else {"tiny", "small", "medium", "large"}
    if size not in sizes:
        raise ValueError(f"unknown fixture size {size!r} for version {version}; one of {sorted(sizes)}")
    return seed, size, version


def generate_by_version(seed: int, size: str, version: int) -> Fixture | FixtureV2 | FixtureV3:
    if version == 4:
        from .fixtures_v4 import generate_v4  # v4 imports this module; import it lazily
        return generate_v4(seed, size)
    if version == 3:
        return generate_v3(seed, size)
    return generate_v2(seed, size) if version == 2 else generate(seed, size)


def generate_from_spec(spec: str) -> Fixture | FixtureV2 | FixtureV3:
    seed, size, version = parse_fixture_spec(spec)
    return generate_by_version(seed, size, version)
