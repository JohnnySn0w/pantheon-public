"""Seeded, deterministic synthetic corpus generator (D11: spec-derived).

Derived from the PMB-1 track definitions (report section 6.2 / 6.6): entities
with facts, some superseded (A2), some forgotten (A3), queries with GOLD
answers and gold evidence keys, never-stored subjects for abstention (E4) and
unrelated pollution probes (A7). Generated, never hand-written per test.

All tokens are fixed-length and distinct, so no value is a substring of
another: leakage checks by substring cannot misfire.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

_CONS = "bdfgklmnprstvz"
_VOW = "aeiou"
ATTRS = ("beverage", "vehicle", "instrument", "snack", "hobby", "pet")
SIZES = {"tiny": 4, "small": 10, "medium": 30, "large": 100}  # entities
# Per entity: ATTRS[:5] get roles in this multiset (shuffled per entity).
_ROLES = ("stable", "stable", "stable", "update", "forget")
PURPOSE = "companionship"


def question(entity: str, attr: str) -> str:
    return f"What is {entity}'s {attr}?"


@dataclass(frozen=True)
class Fact:
    key: str
    entity: str
    attr: str
    value: str

    @property
    def text(self) -> str:
        return f"{self.entity}'s {self.attr} is {self.value}."

    @property
    def subject(self) -> str:
        return self.entity.lower()

    @property
    def question(self) -> str:
        return question(self.entity, self.attr)


@dataclass(frozen=True)
class Update:
    old: Fact
    new: Fact


@dataclass(frozen=True)
class Unseen:
    entity: str
    attr: str

    @property
    def question(self) -> str:
        return question(self.entity, self.attr)

    @property
    def pollution(self) -> str:
        return f"Tell me about {self.entity}'s weekend plans."


@dataclass
class Fixture:
    id: str
    seed: int
    size: str
    stable: list[Fact] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    forgets: list[Fact] = field(default_factory=list)
    unseen: list[Unseen] = field(default_factory=list)

    @property
    def base_facts(self) -> list[Fact]:
        """Everything written first, in a stable order."""
        return self.stable + [u.old for u in self.updates] + self.forgets

    def stored_values(self) -> set[str]:
        return {f.value for f in self.base_facts} | {u.new.value for u in self.updates}

    def corpus_tokens(self, count) -> int:
        return sum(count(f.text) for f in self.base_facts)

    def to_dict(self) -> dict:
        def f(x: Fact) -> list[str]:
            return [x.key, x.entity, x.attr, x.value]
        return {
            "seed": self.seed, "size": self.size,
            "stable": [f(x) for x in self.stable],
            "updates": [[f(u.old), f(u.new)] for u in self.updates],
            "forgets": [f(x) for x in self.forgets],
            "unseen": [[u.entity, u.attr] for u in self.unseen],
        }

    @property
    def sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_spec(spec: str) -> tuple[int, str]:
    """``seed=1,size=small`` -> (1, "small")."""
    kv = dict(p.split("=", 1) for p in spec.split(",") if p)
    seed, size = int(kv.get("seed", "1")), kv.get("size", "small")
    if size not in SIZES:
        raise ValueError(f"unknown fixture size {size!r}; one of {sorted(SIZES)}")
    return seed, size


def generate(seed: int = 1, size: str = "small") -> Fixture:
    if size not in SIZES:
        raise ValueError(f"unknown fixture size {size!r}; one of {sorted(SIZES)}")
    rng = random.Random(f"pmb1/{seed}/{size}")
    n_ent = SIZES[size]
    n_unseen = max(5, n_ent // 2)
    need = n_ent + n_unseen + n_ent * 5 + n_ent  # entities, unseen, values, update values
    seen: set[str] = set()
    pool: list[str] = []
    while len(pool) < need:
        t = "".join(rng.choice(_CONS) + rng.choice(_VOW) for _ in range(3))
        if t not in seen:
            seen.add(t)
            pool.append(t)
    take = iter(pool)
    entities = [next(take).capitalize() for _ in range(n_ent)]
    unseen_entities = [next(take).capitalize() for _ in range(n_unseen)]
    fx = Fixture(id=f"synthetic-seed{seed}-{size}", seed=seed, size=size)
    for ei, ent in enumerate(entities):
        roles = list(_ROLES)
        rng.shuffle(roles)
        for ai, (attr, role) in enumerate(zip(ATTRS[:5], roles)):
            key = f"e{ei:03d}-{ai}"
            fact = Fact(key, ent, attr, next(take))
            if role == "stable":
                fx.stable.append(fact)
            elif role == "update":
                fx.updates.append(Update(fact, Fact(key + "-u", ent, attr, next(take))))
            else:
                fx.forgets.append(fact)
    for ent in unseen_entities:
        fx.unseen.append(Unseen(ent, rng.choice(ATTRS)))
    return fx
