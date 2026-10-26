"""The gate evaluator: ``gate(tier, records) -> Verdict``.

Tier 1 (episodic retrieval) is A1 + A-retrieve + E3 and must: beat the
RandomRetrievalControl, not fall below the VerbatimRAGBaseline beyond a
tolerance, and sit at or under the amnesiac floor for forgotten items (A3).
The Verdict lists EVERY criterion with pass/fail and the numbers. It is never
a bare boolean.

Two regimes, chosen by the records' fixture version:

* v2 records (per-probe ``samples``) are POOLED over seeds: the gate refuses
  fewer than ``MIN_SEEDS`` seeds (or ``MIN_PROBES`` probes per track), every
  vs-baseline comparison carries a 95% paired-bootstrap interval, and a
  non-inferiority criterion passes iff the interval's lower bound is
  >= -RAG_TOLERANCE. A candidate declaring ``over: governed`` is also held to
  the governed store (non-inferior on A1 / A-retrieve, superior on E3).
* v3 records (fixture version 3 = v2 + governance + supersede) are pooled the same
  way and ADD two required criteria, which v2 could not fail (a tier with
  authorization skipped, or one serving a superseded version, passed v2):
  ``governance-leak`` (returned evidence from an unauthorized item must be exactly
  0, not a tolerance) and ``A2-stale-evidence`` (stale-evidence rate <= governed's
  for a tier, exactly 0 for any candidate claiming governance). Controls with no
  authorization (RAG / random / full-context) are expected to leak: their rates
  are reported informationally and never fail a candidate. v3 is the version tier-1
  gates should use from now on; a v2 record set still gates as before.
* v1 records carry no per-probe values, so they keep the point-estimate
  comparisons. The Verdict says so: such a verdict is not statistically stable.

Tier 2 (T-2518 tier 2; ORG-062:116-119, the semantic/relational bi-temporal tier, "Gate: PMB-1 Track
A2/A9 + C5") gates v4 records only, pooled over seeds with the same paired bootstrap. It keeps every
tier-1 pooled criterion and adds, against the substrate the candidate declares it sits ``over``
(``episodic`` or ``governed``, R1): REQUIRED exactly-zero governance leak (GOV and GOV-derived samples
pooled), exactly-zero stale evidence (A2) and stale-as-current (A2c), C5 lineage exactly 1.0, A3 at
or under the floor, zero derived-relation provenance failures; non-inferiority on A1 / A-retrieve /
E3 / A2 new-value; superiority on A2c current-value AND as-of accuracy (R9). A9 (T-5043) has a
RELATIVE surfacing floor, an exact-zero NO-SILENT-PICK rule and ONE superiority test. Surfacing is a
floor, never superiority: any tier that surfaces every conflict sits at the 1.0 ceiling, and a
ceiling-tied rate cannot show superiority for ANY tier. The relative floor: non-inferior, lower bound of
(candidate - baseline) >= -RAG_TOLERANCE. No silent pick (report:421, "must surface conflict, not silently
pick"): a conflict probe whose evidence asserts EXACTLY ONE value for the conflicted slot is a silent pick,
and the pooled count must be exactly 0 in every family (as A2c stale-as-current). A tier that asserts one
value per slot drives false conflicts to 0 BY picking, so the relative floor alone, over a picking
baseline, would reward it. Asserting no value is a retrieval miss (A-retrieve covers it), not a pick; a
record without ``A9.silent_pick`` (written before T-5043) FAILS the rule, never passes by absence. Known
residual, recorded and not gated: a tier that WITHHOLDS on a conflicted slot (asserts 0 values) counts as a
retrieval miss, not a silent pick, so only the relative surfacing floor and the pooled A-retrieve guard
against it; doing so requires detecting the conflict first, which makes it contrived. The
superiority test is DISCRIMINATION: the false-conflict rate pooled over every
control kind, lower is better (lower bound of (baseline - candidate) > 0). One superiority test on A9,
so no Bonferroni split is needed. A2c and both A9 comparisons
use a CLUSTER bootstrap that resamples a slot pair's two phrasings together (the controls are asked in
both phrasings too). The per-kind guard stays: the A9 false-conflict rate must be no worse than the
baseline's + ``FALSE_CONFLICT_TOLERANCE`` in EVERY control kind (the absolute rates and the as-of
wrong-time rate are reported, informational). Tier 1's verdicts are unchanged.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .embedder import is_lexical_stub
from .fixtures_v3 import generate_by_version
from .records import V2_SAMPLE_KEYS, validate_record
from .runner import rank_stability
from .stats import Interval, clustered_interval, mean, paired_interval
from .substrate import ROLE_CANDIDATE, ROLE_FLOOR, ROLE_FULL, ROLE_RAG, ROLE_RANDOM

# PROVISIONAL: how far below the verbatim-RAG baseline a tier-1 candidate may
# fall on A1 / A-retrieve / E3 and still pass. The value is a placeholder
# pending PMB-1's first real run (ORG-062:R1); it is the ONLY place it lives.
RAG_TOLERANCE = 0.05
# PROVISIONAL (T-2518 tier 2, ORG-062:R1 / R5): how far a tier-2 candidate's A9 false-conflict rate may sit
# ABOVE its ``over`` baseline's, per control kind. Tier 2's non-inferiority reuses RAG_TOLERANCE; its
# superiority means the interval's lower bound is > 0.
FALSE_CONFLICT_TOLERANCE = 0.05
TEMPLATE_WORDS_MAX = 8  # REBOUND 3: the most output-template words a record may declare

_CONTROL_ROLES = (ROLE_RANDOM, ROLE_RAG, ROLE_FULL, ROLE_FLOOR)  # rules 6.1.1-3, 6.1.7
GOVERNED_NAME = "governed-store"  # GovernedStoreSubstrate.name: what a tier declaring ``over: governed`` sits over
MIN_SEEDS = 3    # a pooled verdict needs at least this many seeds ...
MIN_PROBES = 150  # ... or at least this many probes in every track
_STAT_TRACKS = (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility"))
TIER2_MIN_VERSION = 4
TIER2_OVER = {"episodic": "episodic-tier", "governed": GOVERNED_NAME}  # ``over`` -> the baseline substrate's name (R1)
GOV_LEAK_KEYS = ("GOV.leak",)                        # the samples governance-leak pools: v3 ...
GOV_LEAK_KEYS_V4 = ("GOV.leak", "GOV-derived.leak")  # ... and v4, where derived relations join them
_T2_NONINFERIOR = (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility"), ("A2", "new_value"))
_T2_SUPERIOR = (("A2c", "current"), ("A2c", "asof"))  # as-of: R9 (REBOUND 1, F3)
# T-5043: A9 surfacing is a FLOOR (a ceiling-tied rate cannot show superiority); discrimination, the false-conflict
# rate pooled over every control kind, LOWER is better, is A9's one superiority test (so no Bonferroni split).
_T2_FLOOR = (("A9", "surfaced"),)
_T2_LOWER_SUPERIOR = (("A9", "false_conflict"),)


@dataclass(frozen=True)
class Criterion:
    name: str
    passed: bool
    detail: str
    numbers: dict = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class Verdict:
    tier: int
    candidate: str | None
    criteria: tuple[Criterion, ...]
    notes: tuple[str, ...] = ()  # informational caveats a reader must see with the verdict

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria if c.required)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.criteria if c.required and not c.passed)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier, "candidate": self.candidate, "passed": self.passed, "failed": list(self.failed),
            "notes": list(self.notes),
            "criteria": [
                {"name": c.name, "passed": c.passed, "required": c.required, "detail": c.detail, "numbers": c.numbers}
                for c in self.criteria
            ],
        }

    def render(self) -> str:
        lines = [f"PMB-1 tier {self.tier} gate: {'PASS' if self.passed else 'FAIL'}  candidate={self.candidate}"]
        for c in self.criteria:
            mark = "pass" if c.passed else "FAIL"
            req = "" if c.required else " (informational)"
            lines.append(f"  [{mark}] {c.name}{req}: {c.detail}")
        if self.failed:
            lines.append("failed: " + ", ".join(self.failed))
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def _m(rec: dict, track: str, metric: str) -> float | None:
    t = rec.get("tracks", {}).get(track)
    return None if t is None else t["metrics"].get(metric)


def _is_v2(r: object) -> bool:
    return isinstance(r, dict) and isinstance(r.get("fixture"), dict) and r["fixture"].get("version", 1) >= 2


def _version_of(r: object) -> int:
    return r["fixture"].get("version", 1) if _is_v2(r) else 1


def gate(tier: int, records: list[dict], *, candidate: str | None = None) -> Verdict:
    if tier not in (1, 2):
        raise NotImplementedError(f"PMB-1 gates tiers 1 and 2 only (asked for tier {tier})")
    notes: list[str] = []
    if any(str((r.get("backbone") or {}).get("family", "")).startswith("stub-") for r in records if isinstance(r, dict)):
        notes.append("the stub backbone families are not independent model families (rule 6.1.4 is satisfied "
                     "formally only; informational)")
    if tier == 2:
        low = sorted({_version_of(r) for r in records if _version_of(r) < TIER2_MIN_VERSION})
        if not records or low:
            v = Verdict(2, candidate, (Criterion(
                "fixture-version-for-tier", False,
                f"tier 2 gates fixture v{TIER2_MIN_VERSION} records only (valid time, conflicts, lineage, derived "
                "relations); got " + (f"version(s) {low}" if records else "no records")),))
        else:
            v = _gate_pooled(records, candidate, notes, tier=2)
        return dataclasses.replace(v, notes=tuple(notes))
    if records and all(_is_v2(r) for r in records):
        v = _gate_pooled(records, candidate, notes)
    elif any(_is_v2(r) for r in records):
        v = Verdict(1, candidate, (Criterion(
            "fixture-versions-consistent", False,
            "a batch mixes v1 and v2 records: refused (v1 records cannot be pooled with v2 per-probe values)"),))
    else:
        if records and not any(_is_v2(r) for r in records):
            notes.append("legacy point-estimate comparisons (v1 records carry no per-probe values): this verdict "
                         "is not statistically stable; gate on v2 records pooled over seeds")
        v = _gate_point(records, candidate)
    return dataclasses.replace(v, notes=tuple(notes))


def _gate_point(records: list[dict], candidate: str | None) -> Verdict:
    crit: list[Criterion] = []

    def add(name: str, passed: bool, detail: str, numbers: dict | None = None, required: bool = True) -> None:
        crit.append(Criterion(name, passed, detail, numbers or {}, required))

    bad = {i: p for i, r in enumerate(records) if (p := validate_record(r))}
    add("records-valid", not bad and bool(records),
        "all records valid" if not bad and records else f"invalid/empty: {bad or 'no records'}")
    if bad or not records:
        return Verdict(1, candidate, tuple(crit))

    add("records-are-gate-runs", all(r["gate"] for r in records),
        "every record is gate=True" if all(r["gate"] for r in records) else "a record came from a non-gate run")
    shas = {r["fixture"]["sha256"] for r in records}
    add("same-fixture", len(shas) == 1, f"{len(shas)} distinct fixture sha256", {"sha256": sorted(shas)})

    names = sorted({r["substrate"]["name"] for r in records if r["substrate"]["role"] == ROLE_CANDIDATE})
    if candidate is None and len(names) == 1:
        candidate = names[0]
    if candidate is None or candidate not in names:
        add("candidate-present", False, f"no unambiguous candidate (candidates in records: {names}; asked: {candidate})")
        return Verdict(1, candidate, tuple(crit))
    add("candidate-present", True, f"candidate = {candidate}")

    by_fam: dict[str, dict[str, dict]] = {}
    dupes = []
    for r in records:
        role = r["substrate"]["role"]
        key = candidate if r["substrate"]["name"] == candidate else role
        if role == ROLE_CANDIDATE and r["substrate"]["name"] != candidate:
            continue  # another candidate in the same batch
        slot = by_fam.setdefault(r["backbone"]["family"], {})
        if key in slot:
            dupes.append((r["backbone"]["family"], key))
        slot[key] = r
    add("no-duplicate-records", not dupes, "one record per (substrate, family)" if not dupes else f"duplicates: {dupes}")

    fams = sorted(f for f, s in by_fam.items() if candidate in s)
    add("families>=2", len(fams) >= 2,
        f"candidate has records under {len(fams)} backbone family(ies): {fams} (rule 6.1.4)", {"families": fams})

    missing = [(f, role) for f in fams for role in _CONTROL_ROLES if role not in by_fam[f]]
    add("controls-present", not missing,
        "random, RAG, full-context and amnesiac controls present for every family" if not missing else f"missing controls: {missing}")
    if missing:
        return Verdict(1, candidate, tuple(crit))

    def compare(name: str, track: str, metric: str, control_role: str, tol: float, desc: str) -> None:
        nums: dict = {}
        ok = True
        for f in fams:
            c, k = _m(by_fam[f][candidate], track, metric), _m(by_fam[f][control_role], track, metric)
            if c is None or k is None:
                nums[f] = {"candidate": c, "control": k}
                ok = False
                continue
            nums[f] = {"candidate": c, "control": k}
            ok = ok and (c > k if tol == 0 else c >= k - tol)
        add(name, ok, f"{track}.{metric} {desc}: " + "; ".join(
            f"{f}: {v['candidate']} vs {v['control']}" for f, v in nums.items()), nums)

    for track, metric in (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility")):
        compare(f"{track}-beats-random", track, metric, ROLE_RANDOM, 0, "must exceed the random control")
    for track, metric in (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility")):
        compare(f"{track}-vs-rag", track, metric, ROLE_RAG, RAG_TOLERANCE,
                f"must be >= verbatim RAG - {RAG_TOLERANCE} (provisional tolerance, ORG-062:R1)")

    nums = {}
    ok_leak = ok_collapse = True
    for f in fams:
        c, fl = by_fam[f][candidate], by_fam[f][ROLE_FLOOR]
        cl, fll = _m(c, "A3", "forgotten_leak_rate"), _m(fl, "A3", "forgotten_leak_rate")
        cr, fr = _m(c, "A3", "retained_recall"), _m(fl, "A3", "retained_recall")
        nums[f] = {"leak": cl, "floor_leak": fll, "retained_recall": cr, "floor_retained_recall": fr}
        ok_leak = ok_leak and cl is not None and fll is not None and cl <= fll
        ok_collapse = ok_collapse and cr is not None and fr is not None and cr > fr
    add("A3-leak-at-or-under-floor", ok_leak,
        "forgotten-item leak must be <= the amnesiac floor: " + "; ".join(
            f"{f}: {v['leak']} vs floor {v['floor_leak']}" for f, v in nums.items()), nums)
    add("A3-no-collapse", ok_collapse,
        "retained-item recall must exceed the amnesiac floor (a substrate scoring like the floor on unrelated "
        "items failed by collapse): " + "; ".join(
            f"{f}: {v['retained_recall']} vs floor {v['floor_retained_recall']}" for f, v in nums.items()), nums)

    viol = {f: _m(by_fam[f][candidate], "E3", "budget_violation_rate") for f in fams}
    add("E3-budget-respected", all(v == 0 for v in viol.values()),
        "E3 budget_violation_rate must be 0: " + "; ".join(f"{f}: {v}" for f, v in viol.items()), viol)

    gap: dict = {}
    for f in fams:
        c, full = by_fam[f][candidate], by_fam[f][ROLE_FULL]
        gap[f] = {
            f"{t}.{m}": {"candidate": _m(c, t, m), "full_context": _m(full, t, m)}
            for t, m in (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility"), ("E3", "mean_tokens"))
        }
    add("ceiling-gap", True,
        "candidate vs full-context ceiling (rule 6.1.3; informational): " + "; ".join(
            f"{f}: " + ", ".join(f"{k} {v['candidate']} vs {v['full_context']}" for k, v in d.items())
            for f, d in gap.items()), gap, required=False)

    stab = rank_stability([r for f in fams for r in by_fam[f].values()])
    add("rank-order-stable", stab["stable"] is True,
        f"substrate rank order across families: {stab['stable']} (rule 6.1.4 reports it; not a pass criterion)",
        stab["orders"], required=False)
    return Verdict(1, candidate, tuple(crit))


def _gate_pooled(records: list[dict], candidate: str | None, notes: list[str], tier: int = 1) -> Verdict:
    """v2 records: pool over seeds, put an interval on every vs-baseline comparison. ``tier=2``: v4 records,
    plus the tier-2 criteria (``_tier2_criteria``)."""
    crit: list[Criterion] = []

    def add(name: str, passed: bool, detail: str, numbers: dict | None = None, required: bool = True) -> None:
        crit.append(Criterion(name, passed, detail, numbers or {}, required))

    def done() -> Verdict:
        return Verdict(tier, candidate, tuple(crit))

    bad = {i: p for i, r in enumerate(records) if (p := validate_record(r))}
    add("records-valid", not bad, "all records valid" if not bad else f"invalid: {bad}")
    if bad:
        return done()
    add("records-are-gate-runs", all(r["gate"] for r in records),
        "every record is gate=True" if all(r["gate"] for r in records) else "a record came from a non-gate run")
    shas: dict[int, set[str]] = {}
    for r in records:
        shas.setdefault(r["fixture"]["seed"], set()).add(r["fixture"]["sha256"])
    sizes = {r["fixture"]["size"] for r in records}
    versions = {r["fixture"]["version"] for r in records}
    add("fixture-versions-consistent", len(versions) == 1,
        f"fixture version(s) {sorted(versions)}" + ("" if len(versions) == 1 else ": a batch mixes fixture versions, refused"))
    if len(versions) != 1:
        return done()
    version = next(iter(versions))
    add("same-fixture", all(len(v) == 1 for v in shas.values()) and len(sizes) == 1,
        f"{len(shas)} seed(s), one fixture sha256 per seed, size(s) {sorted(sizes)}", {"sha256_by_seed": {str(k): sorted(v) for k, v in shas.items()}})
    seeds = sorted(shas)

    # The batch agreeing with itself is not enough: regenerate each seed's fixture and hold the records to it,
    # so a stale or truncated seed cannot ride into a pooled verdict (MIN_SEEDS would be defeated).
    stale: list[str] = []
    size = next(iter(sizes)) if len(sizes) == 1 else None
    fixtures: dict = {}
    for sd in seeds:
        try:
            fx = generate_by_version(sd, size, version)
        except ValueError as e:
            stale.append(f"seed {sd}: cannot regenerate fixture ({e})")
            continue
        fixtures[sd] = fx
        for r in (x for x in records if x["fixture"]["seed"] == sd):
            name = f"{r['substrate']['name']}/{r['backbone']['family']}"
            if r["fixture"]["sha256"] != fx.sha256:
                stale.append(f"seed {sd} {name}: sha256 differs from the regenerated fixture")
            want = {k: len(fx.probes) for k in V2_SAMPLE_KEYS}
            if version >= 3:
                want.update({"GOV.leak": len(fx.gov_probes), "A2.stale_evidence": len(fx.supersedes),
                             "A2.new_value": len(fx.supersedes)})
            if version >= 4:
                want.update(_v4_sample_counts(fx))
            for k, n_want in want.items():
                if len(r["samples"][k]) != n_want:
                    stale.append(f"seed {sd} {name}: samples.{k} has {len(r['samples'][k])} values, fixture has {n_want}")
    add("fixture-matches-seed", not stale,
        "every record's sha256 equals the regenerated fixture for its seed and carries one value per probe" if not stale
        else "stale or truncated records: " + "; ".join(stale[:6]))
    if stale:
        return done()

    names = sorted({r["substrate"]["name"] for r in records if r["substrate"]["role"] == ROLE_CANDIDATE})
    # tier 2: the baselines a tier sits over (R1) ride in the batch as candidates too; never pick one implicitly
    auto = [n for n in names if n not in TIER2_OVER.values()] if tier == 2 else names
    if candidate is None and len(auto) == 1:
        candidate = auto[0]
    if candidate is None or candidate not in names:
        add("candidate-present", False, f"no unambiguous candidate (candidates in records: {names}; asked: {candidate})")
        return done()
    add("candidate-present", True, f"candidate = {candidate}")

    # by_fam[family][key][seed]: key is "candidate", "governed" (the governed store when it is not the candidate) or a control role
    by_fam: dict[str, dict[str, dict[int, dict]]] = {}
    dupes = []
    for r in records:
        role, name, fam, seed = r["substrate"]["role"], r["substrate"]["name"], r["backbone"]["family"], r["fixture"]["seed"]
        if name == candidate:
            key = "candidate"
        elif name == GOVERNED_NAME:
            key = "governed"
        elif tier == 2 and name == TIER2_OVER["episodic"]:
            key = "episodic"  # tier 2's usual baseline (R1); tier 1 keeps skipping other candidates
        elif role == ROLE_CANDIDATE:
            continue  # another candidate in the same batch
        else:
            key = role
        slot = by_fam.setdefault(fam, {}).setdefault(key, {})
        if seed in slot:
            dupes.append((fam, key, seed))
        slot[seed] = r
    add("no-duplicate-records", not dupes,
        "one record per (substrate, family, seed)" if not dupes else f"duplicates: {dupes}")

    fams = sorted(f for f, s in by_fam.items() if "candidate" in s)
    add("families>=2", len(fams) >= 2,
        f"candidate has records under {len(fams)} backbone family(ies): {fams} (rule 6.1.4)", {"families": fams})

    cand_recs = [by_fam[f]["candidate"][sd] for f in fams for sd in by_fam[f]["candidate"]]
    overs = {r["substrate"].get("over") for r in cand_recs}
    add("over-consistent", len(overs) == 1, f"candidate records declare over = {sorted(map(str, overs))} (all must agree)")
    if len(overs) != 1:
        return done()
    over = next(iter(overs))
    if tier == 2:
        add("over-declared", over in TIER2_OVER and TIER2_OVER[over] != candidate,
            f"candidate declares over = {over!r}; a tier-2 candidate must sit over one of {sorted(TIER2_OVER)} "
            "(R1), other than itself, and is held to it")
        if not crit[-1].passed:
            return done()
    probes = {f: min(sum(len(by_fam[f]["candidate"][sd]["samples"][k]) for sd in by_fam[f]["candidate"])
                     for k in V2_SAMPLE_KEYS) for f in fams}
    n_seeds = len(seeds)
    min_probes = min(probes.values(), default=0)
    add("pool-size", n_seeds >= MIN_SEEDS or min_probes >= MIN_PROBES,
        f"pooled {n_seeds} seed(s) and >= {min_probes} probes per track; a tier gate verdict needs "
        f">= {MIN_SEEDS} seeds or >= {MIN_PROBES} probes per track (statistical stability, T-2518 2b)",
        {"seeds": seeds, "min_probes_per_track": probes})
    if not crit[-1].passed:
        return done()

    needed = list(_CONTROL_ROLES) + (["governed"] if over == "governed" and candidate != GOVERNED_NAME else [])
    if tier == 2 and over not in needed:
        needed.append(over)
    missing = [(f, k, sd) for f in fams for k in ["candidate"] + needed for sd in seeds if sd not in by_fam[f].get(k, {})]
    add("controls-present", not missing,
        "random, RAG, full-context and amnesiac controls" + (" and the governed store" if "governed" in needed else "")
        + (" and the episodic tier" if "episodic" in needed else "")
        + " present for every family and seed" if not missing else f"missing (family, substrate, seed): {missing}")
    if missing:
        return done()

    lex = [r for f in fams for sd in seeds for r in (by_fam[f][ROLE_RAG][sd],) if is_lexical_stub(r["embedding_model"])]
    unflagged = [r["substrate"]["name"] for f in fams for k in by_fam[f] for r in by_fam[f][k].values()
                 if is_lexical_stub(r["embedding_model"]) and not r["embedder_is_lexical_stub"]]
    add("embedder-named", not unflagged,
        "no record used the lexical stub embedder, or every such record declares embedder_is_lexical_stub" if not unflagged
        else f"records used the hashing stub embedder without declaring embedder_is_lexical_stub: {sorted(set(unflagged))}")
    stub_tag = " (vs a lexical stub)" if lex else ""
    if lex:
        notes.append("the RAG baseline used the hashing stub embedder (lexical overlap only): the RAG comparisons are "
                     "vs a lexical stub, not a semantic embedder; the real embedder is a later step (2g)")

    def pooled(f: str, key: str, skey: str) -> list[float]:
        return [x for sd in seeds for x in by_fam[f][key][sd]["samples"][skey]]

    def compare(name: str, track: str, metric: str, base: str, kind: str, desc: str, cluster: int = 1,
                lower_better: bool = False) -> None:
        # lower_better (T-5043): the interval is of (baseline - candidate), so the pass rule below reads "higher is
        # better" unchanged; the samples are SWAPPED, never transformed, so the reported means stay the raw rates.
        nums: dict = {}
        ok = True
        parts = []
        for f in fams:
            c, b = pooled(f, "candidate", f"{track}.{metric}"), pooled(f, base, f"{track}.{metric}")
            if len(c) != len(b):
                nums[f], ok = {"error": f"unpaired samples ({len(c)} vs {len(b)})"}, False
                parts.append(f"{f}: unpaired samples")
                continue
            hi, lo = (b, c) if lower_better else (c, b)
            iv: Interval = (paired_interval(hi, lo, f"{name}/{f}") if cluster == 1
                            else clustered_interval(hi, lo, cluster, f"{name}/{f}"))
            passed = iv.lo > 0 if kind == "superior" else iv.lo >= -RAG_TOLERANCE
            ok = ok and passed
            nums[f] = {**iv.to_dict(), "candidate": round(mean(c), 6), "baseline": round(mean(b), 6)}
            parts.append(f"{f}: {nums[f]['candidate']} vs {nums[f]['baseline']}, "
                         f"diff{' (base-cand)' if lower_better else ''} {iv.fmt()}")
        add(name, ok, f"{track}.{metric} {desc}: " + "; ".join(parts), nums)

    for track, metric in _STAT_TRACKS:
        compare(f"{track}-beats-random", track, metric, ROLE_RANDOM, "superior",
                "lower bound of (candidate - random) must exceed 0")
    for track, metric in _STAT_TRACKS:
        compare(f"{track}-vs-rag", track, metric, ROLE_RAG, "noninferior",
                f"lower bound of (candidate - RAG) must be >= -{RAG_TOLERANCE} (provisional tolerance, ORG-062:R1){stub_tag}")
    if tier == 2:
        pass  # tier 2 holds the candidate to its declared ``over`` baseline in _tier2_criteria
    elif candidate == GOVERNED_NAME:
        add("over-governed-checks", True, "skipped: the candidate is the governed store itself", required=False)
    elif over != "governed":
        add("over-governed-checks", True, "skipped: the candidate does not declare over: governed", required=False)
    else:
        for track, metric in (("A1", "accuracy"), ("A-retrieve", "recall")):
            compare(f"{track}-vs-governed", track, metric, "governed", "noninferior",
                    f"a tier over the governed store: lower bound of (candidate - governed) must be >= -{RAG_TOLERANCE} (ORG-062:R1)")
        compare("E3-beats-governed", "E3", "utility", "governed", "superior",
                "a tier over the governed store must be SUPERIOR on E3 (fewer tokens for the same success): "
                "lower bound of (candidate - governed) must exceed 0")

    def wavg(f: str, key: str, track: str, metric: str, weight: str) -> float:
        rs = [by_fam[f][key][sd]["tracks"][track]["metrics"] for sd in seeds]
        tot = sum(m.get(weight, 1.0) for m in rs)
        return sum(m[metric] * m.get(weight, 1.0) for m in rs) / tot if tot else 0.0

    nums = {}
    for f in fams:
        nums[f] = {
            "leak": round(wavg(f, "candidate", "A3", "forgotten_leak_rate", "n_forgotten"), 6),
            "floor_leak": round(wavg(f, ROLE_FLOOR, "A3", "forgotten_leak_rate", "n_forgotten"), 6),
            "retained_recall": round(wavg(f, "candidate", "A3", "retained_recall", "n_retained"), 6),
            "floor_retained_recall": round(wavg(f, ROLE_FLOOR, "A3", "retained_recall", "n_retained"), 6),
        }
    add("A3-leak-at-or-under-floor", all(v["leak"] <= v["floor_leak"] for v in nums.values()),
        "forgotten-item leak (pooled over seeds) must be <= the amnesiac floor: " + "; ".join(
            f"{f}: {v['leak']} vs floor {v['floor_leak']}" for f, v in nums.items()), nums)
    add("A3-no-collapse", all(v["retained_recall"] > v["floor_retained_recall"] for v in nums.values()),
        "retained-item recall (pooled over seeds) must exceed the amnesiac floor: " + "; ".join(
            f"{f}: {v['retained_recall']} vs floor {v['floor_retained_recall']}" for f, v in nums.items()), nums)

    viol = {f: max(by_fam[f]["candidate"][sd]["tracks"]["E3"]["metrics"]["budget_violation_rate"] for sd in seeds) for f in fams}
    add("E3-budget-respected", all(v == 0 for v in viol.values()),
        "E3 budget_violation_rate must be 0 in every seed: " + "; ".join(f"{f}: {v}" for f, v in viol.items()), viol)

    if version >= 3:
        _governance_criteria(add, notes, by_fam, fams, seeds, candidate, over, pooled,
                             leak_keys=GOV_LEAK_KEYS_V4 if version >= 4 else GOV_LEAK_KEYS, zero_stale=tier == 2)
    if tier == 2:
        _tier2_criteria(add, notes, by_fam, fams, seeds, over, pooled, compare, fixtures)

    def smean(f: str, key: str, track: str, metric: str) -> float:
        return round(mean([by_fam[f][key][sd]["tracks"][track]["metrics"][metric] for sd in seeds]), 6)

    gap = {
        f: {f"{t}.{m}": {"candidate": smean(f, "candidate", t, m), "full_context": smean(f, ROLE_FULL, t, m)}
            for t, m in (("A1", "accuracy"), ("A-retrieve", "recall"), ("E3", "utility"), ("E3", "mean_tokens"))}
        for f in fams
    }
    add("ceiling-gap", True,
        "candidate vs full-context ceiling (rule 6.1.3; informational, mean over seeds): " + "; ".join(
            f"{f}: " + ", ".join(f"{k} {v['candidate']} vs {v['full_context']}" for k, v in d.items())
            for f, d in gap.items()), gap, required=False)

    composite_recs = [
        {"backbone": {"family": f}, "substrate": {"name": by_fam[f][k][seeds[0]]["substrate"]["name"]},
         "tracks": {t: {"metrics": {m: smean(f, k, t, m)}} for t, m in _STAT_TRACKS}}
        for f in fams for k in by_fam[f] if all(sd in by_fam[f][k] for sd in seeds)  # a partial set of records has no composite
    ]
    stab = rank_stability(composite_recs)
    add("rank-order-stable", stab["stable"] is True,
        f"substrate rank order across families: {stab['stable']} (rule 6.1.4 reports it; not a pass criterion)",
        stab["orders"], required=False)
    return done()


def _governance_criteria(add, notes: list[str], by_fam: dict, fams: list[str], seeds: list[int],
                         candidate: str, over: str | None, pooled, *, leak_keys: tuple[str, ...] = GOV_LEAK_KEYS,
                         zero_stale: bool = False) -> None:
    """The two v3 criteria (T-2518 2b') plus the controls' leak rates, informational.

    ``governance-leak`` is exact: ONE returned item from an unauthorized source fails
    the candidate, in every family. ``A2-stale-evidence``: exactly 0 for a candidate
    claiming governance (``over: governed``, or the governed store itself), else <= the
    governed store's rate when its records are in the batch, else exactly 0. A control
    has no authorization and is expected to leak; that is a fact about the control.
    """
    def rate(f: str, key: str, skey: str) -> float:
        v = pooled(f, key, skey)
        return round(sum(v) / len(v), 6) if v else 0.0

    def gov(f: str) -> list[float]:
        return [x for k in leak_keys for x in pooled(f, "candidate", k)]

    leaks = {f: {"rate": round(sum(gov(f)) / len(gov(f)), 6) if gov(f) else 0.0, "n": len(gov(f)),
                 "leaks": int(sum(gov(f)))} for f in fams}
    if len(leak_keys) > 1:
        for f in fams:
            leaks[f]["by_samples"] = {k: int(sum(pooled(f, "candidate", k))) for k in leak_keys}
    add("governance-leak", all(v["leaks"] == 0 and v["n"] > 0 for v in leaks.values()),
        "returned evidence from an unauthorized item (above the privacy ceiling, or under a denied purpose) "
        + ("or a unit derived from a forgotten or unauthorized source " if len(leak_keys) > 1 else "")
        + "must be EXACTLY 0, not a tolerance (ORG-062 s1, INV-6): " + "; ".join(
            f"{f}: {v['leaks']} of {v['n']} governance probes leaked" for f, v in leaks.items()), leaks)

    claiming = candidate == GOVERNED_NAME or over == "governed" or zero_stale
    stale: dict = {}
    for f in fams:
        whole = all(sd in by_fam[f].get("governed", {}) for sd in seeds)  # governed's bound only if it has EVERY seed
        gov = rate(f, "governed", "A2.stale_evidence") if whole else None
        bound = 0.0 if claiming or gov is None else gov
        n = len(pooled(f, "candidate", "A2.stale_evidence"))
        stale[f] = {"rate": rate(f, "candidate", "A2.stale_evidence"), "n": n, "bound": bound, "governed": gov}
    add("A2-stale-evidence", all(v["rate"] <= v["bound"] and v["n"] > 0 for v in stale.values()),
        "stale-evidence rate (evidence from a superseded version) must be <= " + (
            ("0 (tier 2)" if zero_stale else "0 (the candidate claims governance)") if claiming
            else "the governed store's, or 0 with no governed records")
        + ": " + "; ".join(f"{f}: {v['rate']} vs bound {v['bound']}" for f, v in stale.items()), stale)

    ctl = {}
    for f in fams:
        for key in sorted(by_fam[f]):
            if key == "candidate" or any(sd not in by_fam[f][key] for sd in seeds):
                continue
            name = by_fam[f][key][seeds[0]]["substrate"]["name"]
            ctl.setdefault(f, {})[name] = {"governance_leak": rate(f, key, "GOV.leak"),
                                           "stale_evidence": rate(f, key, "A2.stale_evidence")}
    add("governance-controls", True,
        "controls' governance-leak / stale-evidence rates (informational: RAG, random and full-context have NO "
        "authorization and are expected to leak; this never fails a candidate): " + "; ".join(
            f"{f}: " + ", ".join(f"{n} {v['governance_leak']}/{v['stale_evidence']}" for n, v in d.items())
            for f, d in ctl.items()), ctl, required=False)
    notes.append("governance (v3): control substrates without authorization (verbatim-rag, random, full-context) "
                 "leak by construction; their rates are a control fact, not a candidate failure")


def _fixture_vocabulary(fx) -> set[str]:
    """Every word of every payload a v4 fixture writes (episodes, restricted, corrections, axis pairs, derived)."""
    from .tracks_v4 import words_of
    texts = [t for _, _, t in fx.episodes + fx.restricted] + [s.new_text for s in fx.supersedes]
    texts += [p.other_text for p in fx.changes + fx.conflicts + fx.controls] + [d.text for d in fx.derived]
    return {w for t in texts for w in words_of(t)}


def _v4_sample_counts(fx) -> dict[str, int]:
    """Per-probe sample counts a v4 record must carry, from the regenerated fixture (one definition: the tracks')."""
    from .tracks_v4 import c5_probes, probes
    n_change = len(probes(fx.changes))
    return {"A2c.current": n_change, "A2c.asof": n_change, "A2c.stale_as_current": n_change,
            "A9.surfaced": len(probes(fx.conflicts)), "A9.false_conflict": len(probes(fx.controls)),
            "C5.lineage": len(c5_probes(fx)), "GOV-derived.leak": len(fx.derived),
            "GOV-derived.provenance": len(fx.derived)}


def _tier2_criteria(add, notes: list[str], by_fam: dict, fams: list[str], seeds: list[int], over: str,
                    pooled, compare, fixtures: dict) -> None:
    """Tier 2 against the declared ``over`` baseline (R1): non-inferiority, superiority, exact-zero staleness,
    C5 lineage, per-kind false conflicts, and the R6 extractor flag."""
    from .fixtures_v4 import MODES
    from .tracks_v4 import is_pattern_stub_extractor, probes
    base = over
    for track, metric in _T2_NONINFERIOR:
        compare(f"{track}-{metric}-vs-baseline", track, metric, base, "noninferior",
                f"a tier-2 candidate is non-inferior to its over={over} baseline: lower bound of (candidate - "
                f"{over}) must be >= -{RAG_TOLERANCE} (provisional, ORG-062:R1)")
    for track, metric in _T2_SUPERIOR:
        compare(f"{track}-{metric}-beats-baseline", track, metric, base, "superior",
                f"a tier-2 candidate must be SUPERIOR to its over={over} baseline here: lower bound of (candidate - "
                f"{over}) must exceed 0 (cluster bootstrap: a slot pair's {len(MODES)} phrasings resample together)",
                cluster=len(MODES))
    for track, metric in _T2_FLOOR:
        compare(f"{track}-{metric}-vs-baseline", track, metric, base, "noninferior",
                f"a FLOOR, not a superiority test (T-5043: a rate tied at the 1.0 ceiling cannot show superiority): "
                f"lower bound of (candidate - {over}) must be >= -{RAG_TOLERANCE} (provisional, ORG-062:R1; cluster "
                f"bootstrap: a slot pair's {len(MODES)} phrasings resample together)", cluster=len(MODES))
    picks: dict = {}
    for f in fams:
        lacking = [sd for sd in seeds if "A9.silent_pick" not in by_fam[f]["candidate"][sd]["samples"]]
        if lacking:
            picks[f] = {"error": f"records lack samples.A9.silent_pick (seeds {lacking}; written before T-5043)"}
            continue
        xs, n_surf = pooled(f, "candidate", "A9.silent_pick"), len(pooled(f, "candidate", "A9.surfaced"))
        picks[f] = {"picks": int(sum(1 for x in xs if x)), "n": len(xs), "n_conflict_probes": n_surf}
    add("A9-no-silent-pick", all("error" not in v and v["picks"] == 0 and v["n"] > 0 and v["n"] == v["n_conflict_probes"]
                                 for v in picks.values()),
        "a conflict probe whose evidence asserts EXACTLY ONE value for the conflicted slot is a silent pick; the count "
        "must be EXACTLY 0 (T-5043; report:421, A9 'must surface conflict, not silently pick'; asserting none is a "
        "retrieval miss, not a pick): " + "; ".join(
            f"{f}: {v['error']}" if "error" in v else f"{f}: {v['picks']} of {v['n']} conflict probes"
            for f, v in picks.items()), picks)
    for track, metric in _T2_LOWER_SUPERIOR:
        compare(f"{track}-{metric}-beats-baseline", track, metric, base, "superior",
                f"pooled over every control kind, LOWER is better (T-5043: discrimination is A9's one superiority "
                f"test, so no Bonferroni split): lower bound of ({over} - candidate) must exceed 0 (cluster "
                f"bootstrap: a slot pair's {len(MODES)} phrasings resample together)", cluster=len(MODES),
                lower_better=True)

    stale = {f: {"stale": int(sum(pooled(f, "candidate", "A2c.stale_as_current"))),
                 "n": len(pooled(f, "candidate", "A2c.stale_as_current"))} for f in fams}
    add("A2c-stale-as-current", all(v["stale"] == 0 and v["n"] > 0 for v in stale.values()),
        "evidence asserting a no-longer-valid value as a present fact must be EXACTLY 0 (ORG-062:116-119, "
        "close-and-supersede): " + "; ".join(
            f"{f}: {v['stale']} of {v['n']} current-value probes" for f, v in stale.items()), stale)

    lineage = {}
    for f in fams:
        xs = pooled(f, "candidate", "C5.lineage")
        c5 = [by_fam[f]["candidate"][sd]["tracks"]["C5"]["metrics"] for sd in seeds]
        units, stored = (int(sum(m.get(k, 0) for m in c5)) for k in ("n_units", "n_store_checked"))
        lineage[f] = {"rate": round(sum(xs) / len(xs), 6) if xs else 0.0, "n": len(xs), "units": units,
                      "store_checked": stored}
    add("C5-lineage", all(v["rate"] == 1.0 and v["n"] > 0 and v["units"] > 0 and v["store_checked"] == v["units"]
                          for v in lineage.values()),
        "every returned unit must resolve, through the substrate's audit(), to a retrievable (memory_id, version_id) "
        "with its observation, valid-from and retain times, as must every ref in its support, CROSS-CHECKED against "
        "the harness's write log (issued ref, the valid-from and payload digest written, a stable and injective "
        "ref -> version map) and, for a store-backed substrate, the store itself (REBOUND 1 F2, REBOUND 2 N2). This is "
        "CONSISTENCY, not proof (tracks_v4 docstring) (report:444; R3: the purpose/use-history clause is not run): " + "; ".join(
            f"{f}: rate {v['rate']} over {v['n']} probes, {v['units']} units, {v['store_checked']} verified in the "
            "store through lineage_store() (required: all; REBOUND 3)" for f, v in lineage.items()), lineage)

    kinds = [c.kind for sd in seeds for c, _ in probes(fixtures[sd].controls)]
    fc: dict = {}
    for f in fams:
        cand, bl = pooled(f, "candidate", "A9.false_conflict"), pooled(f, base, "A9.false_conflict")
        fc[f] = {}
        for k in dict.fromkeys(kinds):
            c = [x for x, kk in zip(cand, kinds) if kk == k]
            b = [x for x, kk in zip(bl, kinds) if kk == k]
            fc[f][k] = {"candidate": round(sum(c) / len(c), 6) if c else 0.0,
                        "baseline": round(sum(b) / len(b), 6) if b else 0.0, "n": len(c)}
    ok = bool(kinds) and all(len(pooled(f, "candidate", "A9.false_conflict")) == len(kinds) for f in fams) and all(
        v["n"] > 0 and v["candidate"] <= v["baseline"] + FALSE_CONFLICT_TOLERANCE + 1e-9
        for d in fc.values() for v in d.values())
    add("A9-false-conflict", ok,
        f"false-conflict rate must be <= the over={over} baseline's + {FALSE_CONFLICT_TOLERANCE} (provisional, "
        "ORG-062:R1) in EVERY control kind (a change, a near-miss, an agreement is not a conflict): " + "; ".join(
            f"{f}: " + ", ".join(f"{k} {v['candidate']} vs {v['baseline']}" for k, v in d.items())
            for f, d in fc.items()), fc)

    def tmean(f: str, key: str, track: str, metric: str) -> float:
        return round(mean([by_fam[f][key][sd]["tracks"][track]["metrics"][metric] for sd in seeds]), 6)

    wrong = {f: {"candidate": tmean(f, "candidate", "A2c", "asof_wrong_time_rate"),
                 "baseline": tmean(f, base, "A2c", "asof_wrong_time_rate")} for f in fams}
    add("A2c-asof-wrong-time", True, "as-of probes whose evidence asserts the value valid NOW rather than then "
        "(informational, R9: never == 0, which real LLM readers would make brittle): " + "; ".join(
            f"{f}: {v['candidate']} vs {v['baseline']}" for f, v in wrong.items()), wrong, required=False)
    add("A9-false-conflict-absolute", True, "absolute false-conflict rate per control kind (informational; R5 stays "
        "relative to the baseline until the first real run): " + "; ".join(
            f"{f}: " + ", ".join(f"{k} {v['candidate']}" for k, v in d.items()) for f, d in fc.items()),
        {f: {k: v["candidate"] for k, v in d.items()} for f, d in fc.items()}, required=False)

    prov = {f: {"failures": int(sum(pooled(f, "candidate", "GOV-derived.provenance"))),
                "n": len(pooled(f, "candidate", "GOV-derived.provenance"))} for f in fams}
    add("GOV-derived-provenance", all(v["failures"] == 0 and v["n"] > 0 for v in prov.values()),
        "on the derived-relation probes, every WORD of a returned unit must occur in a payload the HARNESS wrote "
        "under one of the unit's own sources (REBOUND 2, N1; a unit that fails this and names a derived target is "
        "already a governance leak; selection leaks are a documented limit, tracks_v4): " + "; ".join(
            f"{f}: {v['failures']} of {v['n']} probes" for f, v in prov.items()), prov)

    recs = [r for f in fams for d in by_fam[f].values() for r in d.values()]
    vocab = {w for fx in fixtures.values() for w in _fixture_vocabulary(fx)}
    bad_tw: dict[str, list] = {}
    declared: dict[str, list] = {}
    for r in recs:
        tw = r.get("template_words", [])
        name = r["substrate"]["name"]
        ok_shape = isinstance(tw, list) and len(tw) <= TEMPLATE_WORDS_MAX and all(
            isinstance(w, str) and w and w == w.lower() and w.isalnum() for w in tw)
        if not ok_shape:
            bad_tw[name] = [f"malformed declaration {tw!r} (a list of at most {TEMPLATE_WORDS_MAX} lowercase "
                            "alphanumeric words)"]
            continue
        hit = sorted(set(tw) & vocab)
        if hit:
            bad_tw[name] = hit
        if tw:
            declared[name] = sorted(set(tw))
    add("template-words-valid", not bad_tw,
        "declared template words (R6-style; exempt from provenance) must be at most "
        f"{TEMPLATE_WORDS_MAX} and occur in NO payload the fixture writes (REBOUND 3): "
        + ("valid" if not bad_tw else "refused: " + "; ".join(f"{n}: {w}" for n, w in sorted(bad_tw.items()))),
        {"refused": bad_tw, "declared": declared})
    if declared:
        notes.append("declared template words (exempt from the provenance check; trusted as stated): " + "; ".join(
            f"{n}: {', '.join(w)}" for n, w in sorted(declared.items())))
    unflagged = sorted({r["substrate"]["name"] for r in recs if is_pattern_stub_extractor(r["substrate"].get("extractor"))
                        and not r.get("extractor_is_pattern_stub")})
    add("extractor-named", not unflagged,
        "no record used a pattern-stub relation extractor, or every such record declares extractor_is_pattern_stub "
        "(R6)" if not unflagged else f"records used a pattern-stub extractor without declaring it: {unflagged}")
    flagged = sorted({r["substrate"]["name"] for r in recs if r.get("extractor_is_pattern_stub")
                      and is_pattern_stub_extractor(r["substrate"].get("extractor"))})
    if flagged:
        notes.append(f"R6: {', '.join(flagged)} used a PATTERN-STUB relation extractor (it knows the synthetic corpus "
                     "grammar): its A2c / A9 / C5 numbers show what the gate can measure, not what a real extractor "
                     "would score")
