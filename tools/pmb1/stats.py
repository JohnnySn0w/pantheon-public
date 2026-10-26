"""Interval estimates for the gate's vs-baseline comparisons (T-2518 step 2b).

A paired percentile bootstrap over probes: the candidate and the baseline are
scored on the SAME probes, so the unit resampled is the per-probe difference.
Deterministic: the resampling RNG is seeded from a caller-supplied label, never
from the clock.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

N_BOOT = 2000
CI_LEVEL = 0.95


@dataclass(frozen=True)
class Interval:
    diff: float  # mean(candidate - baseline)
    lo: float
    hi: float
    n: int

    def fmt(self) -> str:
        return f"{self.diff:+.3f} [95% CI {self.lo:+.3f}, {self.hi:+.3f}] (n={self.n})"

    def to_dict(self) -> dict:
        return {"diff": self.diff, "lo": self.lo, "hi": self.hi, "n": self.n}


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def paired_interval(cand: list[float], base: list[float], label: str, *, n_boot: int = N_BOOT) -> Interval:
    """95% percentile-bootstrap interval for mean(cand - base) over paired probes."""
    if len(cand) != len(base) or not cand:
        raise ValueError(f"paired samples must be non-empty and the same length ({len(cand)} vs {len(base)})")
    d = [c - b for c, b in zip(cand, base)]
    n = len(d)
    rng = random.Random(f"pmb1-ci/{label}")
    means = sorted(sum(rng.choices(d, k=n)) / n for _ in range(n_boot))
    tail = (1.0 - CI_LEVEL) / 2.0
    lo, hi = means[int(tail * n_boot)], means[min(n_boot - 1, int((1.0 - tail) * n_boot))]
    return Interval(round(mean(d), 6), round(lo, 6), round(hi, 6), n)


def clustered_interval(cand: list[float], base: list[float], cluster: int, label: str, *,
                       n_boot: int = N_BOOT) -> Interval:
    """As ``paired_interval``, but resampling CLUSTERS of ``cluster`` consecutive probes together (T-2518 t2 REBOUND 1,
    F4): v4 asks each slot pair in both phrasings, so the two probes of a pair are not independent units. ``n`` in
    the result is the number of clusters."""
    if len(cand) != len(base) or not cand or cluster < 1 or len(cand) % cluster:
        raise ValueError(f"clustered samples must be non-empty, the same length and a multiple of {cluster} "
                         f"({len(cand)} vs {len(base)})")
    d = [c - b for c, b in zip(cand, base)]
    sums = [sum(d[i:i + cluster]) for i in range(0, len(d), cluster)]
    k = len(sums)
    rng = random.Random(f"pmb1-ci-cluster/{label}")
    means = sorted(sum(rng.choices(sums, k=k)) / (k * cluster) for _ in range(n_boot))
    tail = (1.0 - CI_LEVEL) / 2.0
    lo, hi = means[int(tail * n_boot)], means[min(n_boot - 1, int((1.0 - tail) * n_boot))]
    return Interval(round(mean(d), 6), round(lo, 6), round(hi, 6), k)
