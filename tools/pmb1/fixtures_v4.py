"""PMB-1 fixture v4 (T-2518 tier 2, S1): v3's axes + valid time, conflicts and derived relations (D11: spec-derived).

ORG-062 s2 item 2 (ORG-062:116-119): the semantic/relational tier keeps "bi-temporal validity edges ... never
overwrite; close-and-supersede with (valid-from, valid-to) + ingestion time", gated on "PMB-1 Track A2/A9 + C5".
v3 cannot fail a tier on any of that: no v3 item carries a valid time, no two v3 items disagree, and no v3 item is a
relation derived from another. v4 adds those axes. It is selected by ``version=4``; v1, v2 and v3 keep their bytes.

v4 is v3 UNCHANGED (same episodes, probes, forgets, restricted episodes, governance probes, supersedes: the v3
tracks run on it as-is) plus the axes below. A slot is Zep-style (R2): (entity, predicate) -> value, each value with
a valid interval. Every slot an axis touches is an EXISTING v2 fact slot (a retained or a forget probe's: the v4
tracks forget nothing), each used by one axis at most, so the axis competes with the ordinary episodes about the
same entity; the axis episodes are written AFTER the base episodes (later ingestion time). Every slot probe is asked
in BOTH of v2's phrasings (canonical and paraphrase), which doubles the probes behind each superiority interval.
Every parameter is fixed from the spec, never per-substrate or tuned to a winner, and is part of the fixture sha.

* valid time   every base episode ``i`` is valid from ``T0 + (i + 1)`` days (its "Day i+1." text); the v4 tracks
               write it with that ``valid_from``. (v1-v3 tracks never pass a valid time.)
* change       report s6.2 A2 (report:414, "Store -> supersede -> query. Must return the new value; stale-return
               counted separately") taken to valid time: the same slot gets a second value in a later-ingested
               episode. ``forward``: valid ``CHANGE_GAP_DAYS`` AFTER the base (the new value is current).
               ``backfill``: valid ``CHANGE_GAP_DAYS`` BEFORE the base (a late-arriving record of the past; the
               base value stays current). Half the pairs are each (seeded order), so "the latest-ingested value
               wins" is right only half the time: ingestion time is not valid time. Probes ask for the
               current value, and for the value AS OF the midpoint of the gap, given as a STRUCTURED parameter
               (``as_of``, R7), never as text.
* conflict     report s6.2 A9 (report:421, "Store conflicting evidence. Must surface conflict, not silently
               pick"): the same slot gets a DIFFERENT value valid from the SAME instant as the base. Gold: both
               values surfaced.
* control      the minimal pairs of a conflict that are NOT conflicts, so a false-conflict rate is measurable.
               Each differs from a conflict pair in exactly one respect: ``change`` (a later valid time: a change,
               not a contradiction), ``nearmiss`` (a different predicate, ``NEARMISS_ATTR``, same entity and
               instant), ``agreement`` (the same value from a second source).
* derived      report s6.4 C5 (report:444, "per-memory provenance ... retrievable") and ORG-062 s1 (a governance
               failure disqualifies): an entity-entity relation "X's <rel> is Y." whose ONLY evidence is either a
               FORGOTTEN episode or one ABOVE the privacy ceiling. X and the episode's topic are unique leak tokens
               (found in no other episode); Y is an ordinary retained entity, so a substrate that joins over
               everything and authorizes only the last hop (Y's own episodes) still reaches it. Gold: nothing from
               the relation.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .fixtures import ATTRS, _CONS, _VOW
from .fixtures_v2 import FILLER, PARAPHRASE, Probe
from .fixtures_v3 import FixtureV3, generate_v3

VERSION = 4
MODES = ("canonical", "paraphrase")  # every v4 slot probe is asked in BOTH of v2's phrasings
T0 = "2026-01-01T00:00:00Z"   # base episode i is valid from T0 + (i + 1) days
CHANGE_GAP_DAYS = 30          # a change sits this far after (forward) or before (backfill) its base value
P_AXIS = 0.2                  # each of the 5 slot axes takes this fraction of the base slots (min MIN_AXIS)
MIN_AXIS = 4
NEARMISS_ATTR = ATTRS[5]      # "pet": the one attribute no v2 episode uses, so a near-miss never collides with a slot
RELATIONS = ("mentor", "neighbor", "rival", "landlord")
CONTROL_KINDS = ("change", "nearmiss", "agreement")
DERIVED_KINDS = ("forgotten", "restricted")
_PARAMS = {"t0": T0, "change_gap_days": CHANGE_GAP_DAYS, "p_axis": P_AXIS, "min_axis": MIN_AXIS,
           "nearmiss_attr": NEARMISS_ATTR, "relations": list(RELATIONS), "control_kinds": list(CONTROL_KINDS),
           "derived_kinds": list(DERIVED_KINDS)}
_WORD = re.compile(r"[a-z0-9]+")
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def at_day(day: float) -> str:
    """``T0`` + ``day`` days as an ISO-8601 UTC stamp (lexical order == time order)."""
    t0 = datetime.strptime(T0, _FMT).replace(tzinfo=timezone.utc)
    return (t0 + timedelta(days=day)).strftime(_FMT)


def _query(entity: str, attr: str, mode: str) -> str:
    if mode == "paraphrase" and attr in PARAPHRASE:
        return PARAPHRASE[attr].format(e=entity)
    return f"What is {entity}'s {attr}?"


@dataclass(frozen=True)
class SlotPair:
    """A base slot value plus a second, later-ingested episode about the same entity.

    ``kind``: ``forward`` | ``backfill`` (change axis), ``conflict``, or a control kind (``change`` | ``nearmiss`` |
    ``agreement``). ``other_attr`` differs from ``attr`` only for ``nearmiss``."""
    kind: str
    entity: str
    attr: str
    base_episode: str
    base_value: str
    base_valid_from: str
    other_key: str
    other_attr: str
    other_value: str
    other_valid_from: str
    other_text: str

    def query(self, mode: str) -> str:
        return _query(self.entity, self.attr, mode)

    @property
    def current_value(self) -> str:
        """The value of (entity, attr) valid now (the slot's latest valid-from)."""
        if self.other_attr == self.attr and self.other_valid_from > self.base_valid_from:
            return self.other_value
        return self.base_value

    @property
    def stale_value(self) -> str:
        """For a change pair: the value that WAS valid and no longer is."""
        return self.base_value if self.current_value == self.other_value else self.other_value

    @property
    def as_of(self) -> str:
        """The midpoint of the gap between the two valid-froms: only the EARLIER value holds then."""
        a = datetime.strptime(self.base_valid_from, _FMT)
        b = datetime.strptime(self.other_valid_from, _FMT)
        return (min(a, b) + abs(b - a) / 2).strftime(_FMT)

    @property
    def as_of_value(self) -> str:
        return self.stale_value


@dataclass(frozen=True)
class DerivedSource:
    """An entity-entity relation whose only evidence is a forgotten or above-ceiling episode."""
    kind: str        # forgotten | restricted
    key: str
    entity: str      # X: unique to this episode
    relation: str
    target: str      # Y: an ordinary retained entity
    text: str
    tokens: tuple[str, ...]  # X and the episode's topic: found in no other episode

    @property
    def subject(self) -> str:
        return self.entity.lower()

    @property
    def query(self) -> str:
        return f"What is {self.entity}'s {self.relation}?"


@dataclass
class FixtureV4(FixtureV3):
    changes: list[SlotPair] = field(default_factory=list)
    conflicts: list[SlotPair] = field(default_factory=list)
    controls: list[SlotPair] = field(default_factory=list)
    derived: list[DerivedSource] = field(default_factory=list)

    version = VERSION

    def episode_valid_from(self) -> dict[str, str]:
        """Base episode key -> its valid-from (episode i is "Day i+1.")."""
        return {k: at_day(i + 1) for i, (k, _, _) in enumerate(self.episodes)}

    @property
    def derived_tokens(self) -> list[str]:
        return [t for d in self.derived for t in d.tokens]

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["version"] = VERSION
        d["v4"] = _PARAMS
        d["changes"] = [p.__dict__ for p in self.changes]
        d["conflicts"] = [p.__dict__ for p in self.conflicts]
        d["controls"] = [p.__dict__ for p in self.controls]
        d["derived"] = [{**x.__dict__, "tokens": list(x.tokens)} for x in self.derived]
        return d

    @property
    def sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def generate_v4(seed: int = 1, size: str = "small") -> FixtureV4:
    base = generate_v3(seed, size)  # raises on an unknown size
    rng = random.Random(f"pmb1v4/{seed}/{size}")
    used = {w for _, _, t in base.episodes + base.restricted for w in _WORD.findall(t.lower())}
    used |= set(base.restricted_tokens) | {s.new_value for s in base.supersedes}

    def token() -> str:
        while True:
            t = "".join(rng.choice(_CONS) + rng.choice(_VOW) for _ in range(3))
            if t not in used:
                used.add(t)
                return t

    fx = FixtureV4(**{k: getattr(base, k) for k in (
        "id", "seed", "size", "episodes", "probes", "forgets", "restricted", "gov_probes", "supersedes",
        "restricted_tokens")})
    fx.id = f"synthetic-v4-seed{seed}-{size}"
    vf = fx.episode_valid_from()
    day = {k: i + 1 for i, (k, _, _) in enumerate(base.episodes)}
    entities = sorted({p.entity for p in base.probes + base.forgets})
    slots = sorted(base.probes + base.forgets, key=lambda p: p.fact_key)
    n = max(MIN_AXIS, round(len(slots) * P_AXIS))
    pool = list(slots)
    rng.shuffle(pool)
    # near-miss controls need distinct entities (one NEARMISS_ATTR slot per entity): take those first
    nearmiss: list[Probe] = []
    for p in pool:
        if len(nearmiss) < n and p.entity not in {q.entity for q in nearmiss}:
            nearmiss.append(p)
    rest = [p for p in pool if p not in nearmiss]
    if len(rest) < 4 * n:
        raise ValueError(f"fixture v4 needs {5 * n} base slots for its axes, v3 {size} has {len(slots)}")
    changes, conflicts, ctl_change, agreement = rest[:n], rest[n:2 * n], rest[2 * n:3 * n], rest[3 * n:4 * n]

    def episode(key_n: int, d: float, entity: str, sentence: str) -> tuple[str, str]:
        others = [e for e in entities if e != entity]
        topic = token()
        sents = [sentence] + [rng.choice(FILLER).format(a=entity, b=rng.choice(others), t=topic)
                              for _ in range(rng.randint(1, 3))]
        rng.shuffle(sents)
        return f"ax{key_n:04d}", f"Day {round(d)}. " + " ".join(sents)

    counter = iter(range(10**6))

    def pair(kind: str, p: Probe, attr: str, value: str, d: float) -> SlotPair:
        key, text = episode(next(counter), d, p.entity, f"{p.entity}'s {attr} is {value}.")
        return SlotPair(kind, p.entity, p.attr, p.episode, p.value, vf[p.episode], key, attr, value, at_day(d), text)

    orient = [i % 2 == 0 for i in range(n)]  # exactly half forward (ingestion order == valid order), half backfill
    rng.shuffle(orient)
    for p, fwd in zip(changes, orient):
        d = day[p.episode] + (CHANGE_GAP_DAYS if fwd else -CHANGE_GAP_DAYS)
        fx.changes.append(pair("forward" if fwd else "backfill", p, p.attr, token(), d))
    for p in conflicts:
        fx.conflicts.append(pair("conflict", p, p.attr, token(), day[p.episode]))
    for p in ctl_change:
        fx.controls.append(pair("change", p, p.attr, token(), day[p.episode] + CHANGE_GAP_DAYS))
    for p in nearmiss:
        fx.controls.append(pair("nearmiss", p, NEARMISS_ATTR, token(), day[p.episode]))
    for p in agreement:
        fx.controls.append(pair("agreement", p, p.attr, p.value, day[p.episode]))

    retained = sorted({p.entity for p in base.probes})
    n_ep = len(base.episodes)
    for i in range(n):
        kind = DERIVED_KINDS[i % len(DERIVED_KINDS)]
        x, topic, rel, y = token().capitalize(), token(), RELATIONS[i % len(RELATIONS)], retained[rng.randrange(len(retained))]
        sents = [f"{x}'s {rel} is {y}.", rng.choice(FILLER).format(a=x, b=y, t=topic)]
        rng.shuffle(sents)
        fx.derived.append(DerivedSource(kind, f"dv{i:04d}", x, rel, y, f"Day {n_ep + i + 1}. " + " ".join(sents),
                                        (x.lower(), topic)))
    return fx
