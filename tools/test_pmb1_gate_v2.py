#!/usr/bin/env python3
"""PMB-1 gate hardening (T-2518 step 2b): pooling, intervals, held to the governed
substrate, a named embedder. Every new criterion has a sabotage proof naming it.

Two kinds of record: REAL (tiny v2 fixture x 3 seeds x 2 stub families, real
substrates) and SYNTHETIC (the same records with the per-probe values replaced
by seeded vectors, so an interval and a point estimate can be pulled apart on
purpose). The offline guard is on.
"""
from __future__ import annotations

import copy
import random
import sys
import unittest
from unittest import mock

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import fixtures_v2 as fx2
from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.gate import gate
from tools.pmb1.stats import paired_interval
from tools.pmb1.substrate import ROLE_CANDIDATE, VerbatimRAGBaseline

gatem = sys.modules["tools.pmb1.gate"]  # the package re-exports the FUNCTION under the module's name

SEEDS = (1, 2, 3)
FAMILIES = ("strict", "lenient")
SIZE = "tiny"
N = 120  # synthetic probes per seed (3 seeds => 360 pooled)
KEYS = ("A1.accuracy", "A-retrieve.recall", "E3.utility")
_GUARD = None
_REAL: dict[str, list[dict]] = {}


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class GoodCandidate(VerbatimRAGBaseline):
    """A well-behaved candidate: RAG under another name and role."""
    name = "candidate-good"
    role = ROLE_CANDIDATE


class ReturnsNothing(GoodCandidate):
    name = "candidate-nothing"

    def _retrieve(self, query, purpose, budget_tokens):
        return []


class IgnoresForget(GoodCandidate):
    name = "candidate-ignores-forget"

    def _forget(self, ref):
        return None


class IgnoresBudget(GoodCandidate):
    name = "candidate-ignores-budget"

    def _retrieve(self, query, purpose, budget_tokens):
        return super()._retrieve(query, purpose, 10**9)


class NothingOverGoverned(ReturnsNothing):
    name = "tier-nothing-over-governed"
    over = "governed"


def real(key: str, make) -> list[dict]:
    """Records for ``make()`` over every seed x family (cached)."""
    if key not in _REAL:
        out = []
        for seed in SEEDS:
            fx = fx2.generate_v2(seed, SIZE)
            for fam in FAMILIES:
                out.append(runner.run_one(make(fx), StubBackbone(fam), fx, gate=True, embedder_is_lexical_stub=True))
        _REAL[key] = out
    return _REAL[key]


def controls() -> list[dict]:
    out: list[dict] = []
    for n in ("rag", "random", "full", "amnesiac"):
        out += real(n, lambda fx, n=n: (lambda: runner.SUBSTRATES[n](fx)))
    return out


def governed() -> list[dict]:
    return real("governed", lambda fx: (lambda: runner.SUBSTRATES["governed"](fx)))


def candidate(cls) -> list[dict]:
    return real(cls.__name__, lambda fx: (lambda: cls(runner.HashingEmbedder())))


def failed(recs, **kw) -> set[str]:
    return set(gate(1, recs, **kw).failed)


def crit(v, name):
    return next(c for c in v.criteria if c.name == name)


# ---- synthetic records -----------------------------------------------------------------

def _bits(rng: random.Random, p: float) -> list[float]:
    return [1.0 if rng.random() < p else 0.0 for _ in range(N)]


def _put(recs: list[dict], name: str, key: str, vec_for_seed, *, family: str | None = None) -> None:
    """Overwrite ``key``'s per-probe values (and the metric mirroring them) for substrate ``name``."""
    track, metric = key.split(".")
    for r in recs:
        if r["substrate"]["name"] != name or (family and r["backbone"]["family"] != f"stub-{family}"):
            continue
        vec = vec_for_seed(r["fixture"]["seed"])
        r["samples"][key] = list(vec)
        r["tracks"][track]["metrics"][metric] = round(sum(vec) / len(vec), 6)


def governed_standin() -> list[dict]:
    """SYNTHETIC records wearing the governed store's name and role: clones of the RAG records (real shape, real
    A3 / budget numbers), whose per-probe values ``synthetic`` then replaces. The real governed store is run in
    ``TestOverGoverned.test_real_governed_*`` only: its tiny run alone costs ~2 minutes."""
    out = copy.deepcopy(real("rag", lambda fx: (lambda: runner.SUBSTRATES["rag"](fx))))
    for r in out:
        r["substrate"].update(name="governed-store", role=ROLE_CANDIDATE)
    return out


def synthetic() -> list[dict]:
    """controls + a governed stand-in + a candidate cloned from the real good one, all with seeded vectors."""
    recs = copy.deepcopy(controls() + governed_standin() + candidate(GoodCandidate))
    rag = {s: _bits(random.Random(f"rag/{s}"), 0.8) for s in SEEDS}
    gov = {}
    for s in SEEDS:  # governed = rag plus 12 recovered probes
        g, zeros = list(rag[s]), [i for i, b in enumerate(rag[s]) if b == 0.0]
        for i in zeros[:12]:
            g[i] = 1.0
        gov[s] = g
    e3 = lambda bits: [0.2 + 0.2 * b for b in bits]  # noqa: E731
    for key in ("A1.accuracy", "A-retrieve.recall"):
        _put(recs, "verbatim-rag", key, lambda s: rag[s])
        _put(recs, "candidate-good", key, lambda s: rag[s])
        _put(recs, "governed-store", key, lambda s: gov[s])
        _put(recs, "random-control", key, lambda s: _bits(random.Random(f"rnd/{key}/{s}"), 0.05))
        _put(recs, "full-context-ceiling", key, lambda s: [1.0] * N)
        _put(recs, "amnesiac-floor", key, lambda s: [0.0] * N)
    _put(recs, "verbatim-rag", "E3.utility", lambda s: e3(rag[s]))
    _put(recs, "candidate-good", "E3.utility", lambda s: e3(rag[s]))
    _put(recs, "governed-store", "E3.utility", lambda s: [x + 0.05 for x in e3(rag[s])])
    _put(recs, "random-control", "E3.utility", lambda s: [0.02 * b for b in _bits(random.Random(f"rnd3/{s}"), 0.05)])
    _put(recs, "full-context-ceiling", "E3.utility", lambda s: [0.0] * N)
    _put(recs, "amnesiac-floor", "E3.utility", lambda s: [0.0] * N)
    for r in recs:
        if r["substrate"]["name"] == "candidate-good":
            r["substrate"]["name"] = "tier-x"
        # N = 120 values per seed is exactly the medium fixture's probe count: the records claim that fixture
        r["fixture"].update(size="medium", sha256=fx2.generate_v2(r["fixture"]["seed"], "medium").sha256)
    return recs


def over_governed(recs: list[dict]) -> list[dict]:
    for r in recs:
        if r["substrate"]["name"] == "tier-x":
            r["substrate"]["over"] = "governed"
    return recs


def rag_bits(seed: int) -> list[float]:
    return _bits(random.Random(f"rag/{seed}"), 0.8)


def flipped(vec: list[float], ones_to_zero: int, zeros_to_one: int) -> list[float]:
    out = list(vec)
    ones = [i for i, b in enumerate(vec) if b == 1.0][:ones_to_zero]
    zeros = [i for i, b in enumerate(vec) if b == 0.0][:zeros_to_one]
    for i in ones:
        out[i] = 0.0
    for i in zeros:
        out[i] = 1.0
    return out


# ---- tests -----------------------------------------------------------------------------

class TestStatsHelper(unittest.TestCase):
    def test_interval_is_deterministic_and_brackets_the_mean_difference(self) -> None:
        c, b = [1.0, 0.0, 1.0, 1.0] * 30, [1.0, 1.0, 0.0, 1.0] * 30
        a, a2 = paired_interval(c, b, "x"), paired_interval(c, b, "x")
        self.assertEqual(a, a2)
        self.assertEqual(a.diff, 0.0)
        self.assertLess(a.lo, 0.0)
        self.assertGreater(a.hi, 0.0)
        self.assertNotEqual(paired_interval(c, b, "x").lo, paired_interval(c, b, "other-label").lo)

    def test_zero_variance_collapses_the_interval_and_unpaired_samples_are_refused(self) -> None:
        iv = paired_interval([0.5] * 50, [0.8] * 50, "x")
        self.assertEqual((iv.lo, iv.hi, iv.diff), (-0.3, -0.3, -0.3))
        with self.assertRaises(ValueError):
            paired_interval([1.0, 0.0], [1.0], "x")


class TestRealRecords(unittest.TestCase):
    """Real substrates, real v2 runs: the pooled gate passes a good candidate and each broken one fails by name."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.good = controls() + candidate(GoodCandidate)

    def test_the_records_carry_what_the_gate_needs(self) -> None:
        r = self.good[0]
        self.assertEqual(r["fixture"]["version"], 2)
        self.assertIs(r["reader_knows_lexicon"], True)  # a stub reader knows the paraphrase lexicon: say so
        self.assertIs(r["embedder_is_lexical_stub"], True)
        for k in KEYS:
            self.assertTrue(r["samples"][k])
        self.assertEqual(set(r["not_run"]) & {"A2", "A7", "E4"}, {"A2", "A7", "E4"})

    def test_a_good_candidate_passes_pooled_over_three_seeds(self) -> None:
        v = gate(1, self.good)
        self.assertTrue(v.passed, v.render())
        self.assertIn("[95% CI", crit(v, "A1-vs-rag").detail)
        self.assertEqual(crit(v, "pool-size").numbers["seeds"], [1, 2, 3])
        self.assertIn("over-governed-checks", [c.name for c in v.criteria])

    def test_the_verdict_is_deterministic(self) -> None:
        self.assertEqual(gate(1, self.good).to_dict(), gate(1, self.good).to_dict())

    def test_one_seed_is_refused_and_the_criterion_is_named(self) -> None:
        one = [r for r in self.good if r["fixture"]["seed"] == 1]
        v = gate(1, one)
        self.assertEqual(v.failed, ("pool-size",))
        self.assertIn(f">= {gatem.MIN_SEEDS} seeds", crit(v, "pool-size").detail)
        self.assertIn(f">= {gatem.MIN_PROBES} probes", crit(v, "pool-size").detail)
        self.assertFalse(any(c.name.endswith("-vs-rag") for c in v.criteria))  # refused before any comparison

    def test_one_seed_with_enough_probes_per_track_is_not_refused(self) -> None:
        one = [r for r in self.good if r["fixture"]["seed"] == 1]
        n = len(one[0]["samples"]["A1.accuracy"])
        with mock.patch.object(gatem, "MIN_PROBES", n):  # the probes-per-track alternative, at tiny scale
            v = gate(1, one)
        self.assertTrue(crit(v, "pool-size").passed)
        self.assertNotIn("pool-size", v.failed)

    def test_retrieval_returning_nothing_fails_the_comparisons_and_collapse(self) -> None:
        f = failed(controls() + candidate(ReturnsNothing))
        for want in ("A1-beats-random", "A-retrieve-beats-random", "E3-beats-random", "A1-vs-rag", "A-retrieve-vs-rag",
                     "E3-vs-rag", "A3-no-collapse"):
            self.assertIn(want, f)
        self.assertNotIn("A3-leak-at-or-under-floor", f)

    def test_ignoring_forget_fails_exactly_the_a3_leak_criterion(self) -> None:
        self.assertEqual(failed(controls() + candidate(IgnoresForget)), {"A3-leak-at-or-under-floor"})

    def test_ignoring_the_budget_fails_the_e3_budget_criterion(self) -> None:
        self.assertIn("E3-budget-respected", failed(controls() + candidate(IgnoresBudget)))

    def test_mixed_fixtures_within_a_seed_fail_same_fixture(self) -> None:
        recs = copy.deepcopy(self.good)
        recs[0]["fixture"]["sha256"] = "0" * 64
        self.assertIn("same-fixture", failed(recs))

    def test_a_missing_seed_for_a_control_fails_controls_present(self) -> None:
        recs = [r for r in self.good if not (r["substrate"]["role"] == "baseline-rag" and r["fixture"]["seed"] == 2)]
        self.assertIn("controls-present", failed(recs))

    def test_duplicate_records_fail(self) -> None:
        self.assertIn("no-duplicate-records", failed(self.good + [copy.deepcopy(self.good[0])]))

    def test_a_non_gate_record_and_one_family_fail_their_criteria(self) -> None:
        recs = copy.deepcopy(self.good)
        recs[0]["gate"] = False
        self.assertIn("records-are-gate-runs", failed(recs))
        self.assertIn("families>=2", failed([r for r in self.good if r["backbone"]["family"] == "stub-strict"]))

    def test_a_v2_record_without_samples_is_invalid(self) -> None:
        recs = copy.deepcopy(self.good)
        del recs[0]["samples"]
        self.assertEqual(gate(1, recs).failed, ("records-valid",))

    def test_a_truncated_candidate_seed_cannot_reach_the_comparisons(self) -> None:
        recs = copy.deepcopy(self.good)
        for r in recs:
            if r["substrate"]["name"] == "candidate-good" and r["fixture"]["seed"] == 1:
                r["samples"]["A1.accuracy"] = r["samples"]["A1.accuracy"][:-1]
        self.assertIn("fixture-matches-seed", failed(recs))


class TestIntervalsChangeTheVerdict(unittest.TestCase):
    def test_a_candidate_003_below_rag_with_a_wide_interval_fails_where_the_point_estimate_would_pass(self) -> None:
        recs = synthetic()
        _put(recs, "tier-x", "A1.accuracy", lambda s: flipped(rag_bits(s), 14, 10))
        v = gate(1, recs, candidate="tier-x")
        c = crit(v, "A1-vs-rag")
        diffs = [f["diff"] for f in c.numbers.values()]
        self.assertTrue(all(-0.05 <= d < -0.03 + 0.005 for d in diffs), diffs)  # the point estimate alone would pass
        self.assertTrue(all(f["lo"] < -gatem.RAG_TOLERANCE for f in c.numbers.values()), c.numbers)
        self.assertIn("A1-vs-rag", v.failed)
        self.assertEqual(failed(recs, candidate="tier-x"), {"A1-vs-rag"})

    def test_the_same_gap_with_a_tight_interval_passes(self) -> None:
        recs = synthetic()
        _put(recs, "tier-x", "E3.utility", lambda s: [x - 0.03 for x in [0.2 + 0.2 * b for b in rag_bits(s)]])
        v = gate(1, recs, candidate="tier-x")
        self.assertNotIn("E3-vs-rag", v.failed, v.render())
        self.assertAlmostEqual(next(iter(crit(v, "E3-vs-rag").numbers.values()))["diff"], -0.03, places=5)

    def test_beating_random_needs_the_lower_bound_above_zero(self) -> None:
        recs = synthetic()  # candidate = rag; make it indistinguishable from random
        _put(recs, "tier-x", "A1.accuracy", lambda s: _bits(random.Random(f"rnd/A1.accuracy/{s}"), 0.05))
        self.assertIn("A1-beats-random", failed(recs, candidate="tier-x"))

    def test_the_tolerance_lives_in_one_place(self) -> None:
        self.assertEqual(gatem.RAG_TOLERANCE, 0.05)
        src = open(gatem.__file__, encoding="utf-8").read()
        self.assertEqual(src.count("RAG_TOLERANCE ="), 1)
        self.assertIn("ORG-062:R1", src.split("RAG_TOLERANCE =")[0].rsplit("\n\n", 1)[-1])


def gov_vec(recs: list[dict], key: str):
    """The governed store's per-probe values for ``key`` (by seed): what 'equal to governed' means."""
    return lambda s: next(r for r in recs if r["substrate"]["name"] == "governed-store"
                          and r["fixture"]["seed"] == s)["samples"][key]


def like_governed(recs: list[dict], e3_bonus: float = 0.0) -> list[dict]:
    """tier-x declares ``over: governed`` and scores exactly like it (plus ``e3_bonus`` on E3)."""
    over_governed(recs)
    for k in ("A1.accuracy", "A-retrieve.recall"):
        _put(recs, "tier-x", k, gov_vec(recs, k))
    _put(recs, "tier-x", "E3.utility", lambda s: [x + e3_bonus for x in gov_vec(recs, "E3.utility")(s)])
    return recs


class TestSuperiorityUsesTheInterval(unittest.TestCase):
    """diff > 0 but the lower bound <= 0 must FAIL: superiority is judged on the interval, not the point estimate."""

    def test_e3_beats_governed_fails_when_only_the_point_estimate_is_positive(self) -> None:
        recs = like_governed(synthetic())
        _put(recs, "tier-x", "E3.utility",
             lambda s: [x + (0.2 if i % 2 == 0 else -0.18) for i, x in enumerate(gov_vec(recs, "E3.utility")(s))])
        v = gate(1, recs, candidate="tier-x")
        nums = crit(v, "E3-beats-governed").numbers
        self.assertTrue(all(f["diff"] > 0 and f["lo"] <= 0 for f in nums.values()), nums)
        self.assertIn("E3-beats-governed", v.failed)

    def test_beats_random_fails_when_only_the_point_estimate_is_positive(self) -> None:
        recs = synthetic()
        _put(recs, "tier-x", "A1.accuracy",
             lambda s: flipped(_bits(random.Random(f"rnd/A1.accuracy/{s}"), 0.05), 4, 6))
        v = gate(1, recs, candidate="tier-x")
        nums = crit(v, "A1-beats-random").numbers
        self.assertTrue(all(f["diff"] > 0 and f["lo"] <= 0 for f in nums.values()), nums)
        self.assertIn("A1-beats-random", v.failed)


class TestStaleOrMixedBatches(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.good = controls() + candidate(GoodCandidate)

    def test_a_seed_with_a_stale_sha_is_refused_by_name(self) -> None:
        recs = copy.deepcopy(self.good)
        for r in recs:
            if r["fixture"]["seed"] == 1:
                r["fixture"]["sha256"] = "f" * 64
        v = gate(1, recs)
        self.assertFalse(v.passed)
        self.assertIn("fixture-matches-seed", v.failed)
        self.assertIn("sha256 differs", crit(v, "fixture-matches-seed").detail)

    def test_a_seed_with_truncated_samples_is_refused_by_name(self) -> None:
        recs = copy.deepcopy(self.good)
        for r in recs:
            if r["fixture"]["seed"] == 1:
                for k in KEYS:
                    r["samples"][k] = r["samples"][k][:3]
        v = gate(1, recs)
        self.assertFalse(v.passed)
        self.assertIn("fixture-matches-seed", v.failed)
        self.assertIn("fixture has", crit(v, "fixture-matches-seed").detail)

    def test_candidate_records_must_agree_on_over(self) -> None:
        recs = copy.deepcopy(self.good)
        next(r for r in recs if r["substrate"]["name"] == "candidate-good")["substrate"]["over"] = "governed"
        v = gate(1, recs)
        self.assertIn("over-consistent", v.failed)

    def test_a_mixed_v1_v2_batch_is_refused_by_name(self) -> None:
        fx = fxm.generate(1, "tiny")
        v1 = runner.run_one(lambda: runner.SUBSTRATES["rag"](fx), StubBackbone("strict"), fx, gate=True)
        v = gate(1, self.good + [v1])
        self.assertEqual(v.failed, ("fixture-versions-consistent",))


class TestOverGoverned(unittest.TestCase):
    def test_a_tier_no_better_than_rag_fails_every_governed_comparison(self) -> None:
        recs = over_governed(synthetic())  # tier-x scores like RAG; governed recovers 12/120 probes more
        self.assertEqual(failed(recs, candidate="tier-x"), {"A1-vs-governed", "A-retrieve-vs-governed", "E3-beats-governed"})

    def test_equal_to_governed_on_a1_but_not_better_on_e3_fails_the_e3_superiority_criterion(self) -> None:
        recs = like_governed(synthetic())
        self.assertEqual(failed(recs, candidate="tier-x"), {"E3-beats-governed"})
        self.assertIn("must exceed 0", crit(gate(1, recs, candidate="tier-x"), "E3-beats-governed").detail)

    def test_strictly_better_on_e3_and_equal_elsewhere_passes_everything(self) -> None:
        v = gate(1, like_governed(synthetic(), e3_bonus=0.1), candidate="tier-x")
        self.assertTrue(v.passed, v.render())

    def test_worse_than_governed_on_a1_beyond_the_tolerance_fails_non_inferiority(self) -> None:
        recs = like_governed(synthetic(), e3_bonus=0.1)
        _put(recs, "tier-x", "A1.accuracy", lambda s: flipped(gov_vec(recs, "A1.accuracy")(s), 30, 0))  # 30/120 probes lost per seed
        self.assertIn("A1-vs-governed", failed(recs, candidate="tier-x"))

    def test_a_small_drop_with_a_wide_interval_also_fails_non_inferiority_vs_governed(self) -> None:
        recs = like_governed(synthetic(), e3_bonus=0.1)
        _put(recs, "tier-x", "A-retrieve.recall", lambda s: flipped(gov_vec(recs, "A-retrieve.recall")(s), 12, 0))
        v = gate(1, recs, candidate="tier-x")
        self.assertIn("A-retrieve-vs-governed", v.failed)
        self.assertNotIn("A-retrieve-vs-rag", v.failed)  # the same candidate is fine against RAG alone
        self.assertNotIn("A1-vs-governed", v.failed)

    def test_declaring_over_governed_without_the_governed_records_fails_controls_present(self) -> None:
        recs = [r for r in over_governed(synthetic()) if r["substrate"]["name"] != "governed-store"]
        v = gate(1, recs, candidate="tier-x")
        self.assertIn("controls-present", v.failed)
        self.assertIn("governed", crit(v, "controls-present").detail)

    def test_governed_as_the_candidate_skips_the_checks_and_says_why(self) -> None:
        recs = over_governed(synthetic())
        c = crit(gate(1, recs, candidate="governed-store"), "over-governed-checks")
        self.assertFalse(c.required)
        self.assertIn("governed store itself", c.detail)
        names = [x.name for x in gate(1, recs, candidate="governed-store").criteria]
        self.assertNotIn("E3-beats-governed", names)

    def test_a_candidate_that_does_not_declare_over_is_not_held_to_governed(self) -> None:
        c = crit(gate(1, synthetic(), candidate="tier-x"), "over-governed-checks")
        self.assertIn("does not declare over: governed", c.detail)

    def test_the_real_governed_store_as_candidate_and_as_the_comparator(self) -> None:
        """One test, one worker: the real governed store's tiny run costs ~2 minutes."""
        v = gate(1, controls() + governed(), candidate="governed-store")
        self.assertTrue(v.passed, v.render())
        self.assertIn("governed store itself", crit(v, "over-governed-checks").detail)
        # a deliberately broken tier over the REAL governed store fails every governed comparison by name
        f = failed(controls() + governed() + candidate(NothingOverGoverned), candidate="tier-nothing-over-governed")
        self.assertTrue({"A1-vs-governed", "A-retrieve-vs-governed", "E3-beats-governed"} <= f, f)


class TestNamedEmbedder(unittest.TestCase):
    def test_a_gate_run_with_hashing_and_no_stub_flag_is_refused(self) -> None:
        fx = fx2.generate_v2(1, "tiny")
        with self.assertRaises(runner.GateRunRefused) as cm:
            runner.run(["rag"], [StubBackbone("strict"), StubBackbone("lenient")], fx, gate=True)
        self.assertIn("lexical stub", str(cm.exception))

    def test_with_the_flag_the_run_goes_through_and_the_record_carries_it(self) -> None:
        fx = fx2.generate_v2(1, "tiny")
        recs, _ = runner.run(["rag"], [StubBackbone("strict"), StubBackbone("lenient")], fx, gate=True,
                             embedder_is_lexical_stub=True)
        self.assertTrue(all(r["embedder_is_lexical_stub"] is True for r in recs))

    def test_a_non_gate_run_may_use_hashing_unflagged(self) -> None:
        recs, _ = runner.run(["rag"], [StubBackbone("strict")], fx2.generate_v2(1, "tiny"), gate=False)
        self.assertIs(recs[0]["embedder_is_lexical_stub"], False)

    def test_the_verdict_marks_the_rag_comparisons_as_vs_a_lexical_stub(self) -> None:
        v = gate(1, controls() + candidate(GoodCandidate))
        self.assertIn("vs a lexical stub", crit(v, "A1-vs-rag").detail)
        self.assertTrue(any("vs a lexical stub" in n for n in v.notes))
        self.assertIn("vs a lexical stub", v.render())

    def test_a_hashing_record_that_does_not_declare_the_flag_fails_embedder_named(self) -> None:
        recs = copy.deepcopy(controls() + candidate(GoodCandidate))
        for r in recs:
            if r["substrate"]["role"] == "baseline-rag":
                r["embedder_is_lexical_stub"] = False
        v = gate(1, recs)
        self.assertEqual(v.failed, ("embedder-named",))
        self.assertIn("verbatim-rag", crit(v, "embedder-named").detail)


class TestStubFamilyNote(unittest.TestCase):
    def test_the_verdict_notes_that_stub_families_are_not_independent_families(self) -> None:
        v = gate(1, controls() + candidate(GoodCandidate))
        self.assertTrue(any("not independent model families" in n for n in v.notes), v.notes)
        self.assertIn("not independent model families", v.render())
        self.assertTrue(v.passed)  # informational, not a failure
        self.assertIn("not independent model families", " ".join(gate(1, controls() + candidate(GoodCandidate)).to_dict()["notes"]))


class TestLegacyV1Records(unittest.TestCase):
    def test_v1_records_keep_the_point_estimate_path_and_say_so(self) -> None:
        fx = fxm.generate(1, "tiny")
        recs = []
        for fam in FAMILIES:
            for make in [lambda: GoodCandidate(runner.HashingEmbedder())] + [
                    lambda n=n: runner.SUBSTRATES[n](fx) for n in ("rag", "random", "full", "amnesiac")]:
                recs.append(runner.run_one(make, StubBackbone(fam), fx, gate=True))
        v = gate(1, recs)
        self.assertTrue(v.passed, v.render())
        self.assertTrue(any("legacy point-estimate" in n for n in v.notes), v.notes)
        self.assertNotIn("pool-size", [c.name for c in v.criteria])


if __name__ == "__main__":
    unittest.main()
