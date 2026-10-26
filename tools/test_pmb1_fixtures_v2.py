#!/usr/bin/env python3
"""PMB-1 fixture v2 (T-2518 step 2a): deterministic, spec-derived axes, derived budgets, v1 untouched."""
from __future__ import annotations

import re
import unittest

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import fixtures_v2 as v2
from tools.pmb1 import offline
from tools.pmb1.backbone import ABSTAIN, StubBackbone
from tools.pmb1.tokens import count_tokens, words

_GUARD = None

# Pinned from origin/main BEFORE step 2: v1 bytes must not move.
V1_SHA = {
    (1, "tiny"): "962ef15d4f75baad2969250ef06f9d28bc7bdb4554ecf24c1ba0cc6ec9ea093f",
    (1, "small"): "a71b9adfd6bd1e2446bd909d7051add22870fda9f8fdea77bc175e214fb38183",
    (7, "medium"): "daf246efba28eff30b674c47017f0f17c9b05fb34058f15071afd3a99c67d686",
}


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class TestV1Untouched(unittest.TestCase):
    def test_v1_fixture_sha_is_byte_identical_to_main(self) -> None:
        for (seed, size), sha in V1_SHA.items():
            self.assertEqual(fxm.generate(seed, size).sha256, sha)

    def test_default_spec_stays_v1_and_parse_spec_is_unchanged(self) -> None:
        self.assertEqual(fxm.parse_spec("seed=7,size=small"), (7, "small"))
        self.assertEqual(v2.parse_fixture_spec("seed=7,size=small"), (7, "small", 1))
        self.assertEqual(v2.generate_from_spec("seed=1,size=small").sha256, V1_SHA[(1, "small")])
        self.assertEqual(v2.parse_fixture_spec("seed=2,size=xlarge,version=2"), (2, "xlarge", 2))

    def test_unknown_version_or_size_for_the_version_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            v2.parse_fixture_spec("seed=1,size=small,version=3")
        with self.assertRaises(ValueError):
            v2.parse_fixture_spec("seed=1,size=xlarge")  # xlarge exists on v2 only


class TestV2Determinism(unittest.TestCase):
    def test_same_seed_same_sha_different_seed_different_sha(self) -> None:
        a, b, c = v2.generate_v2(1, "small"), v2.generate_v2(1, "small"), v2.generate_v2(2, "small")
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, c.sha256)
        self.assertNotEqual(a.sha256, v2.generate_v2(1, "tiny").sha256)
        self.assertNotEqual(a.sha256, fxm.generate(1, "small").sha256)

    def test_the_parameters_are_part_of_the_sha(self) -> None:
        f = v2.generate_v2(1, "tiny")
        before = f.sha256
        self.addCleanup(setattr, v2, "_PARAMS", v2._PARAMS)
        v2._PARAMS = {**v2._PARAMS, "p_paraphrase": 0.9}
        self.assertNotEqual(f.sha256, before)


class TestV2Axes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = v2.generate_v2(3, "medium")

    def test_episodes_are_dated_multi_sentence_records_in_time_order(self) -> None:
        for i, (key, _, text) in enumerate(self.fx.episodes):
            self.assertTrue(text.startswith(f"Day {i + 1}. "), text)
            self.assertEqual(key, f"ep{i:04d}")
            n = len(re.findall(r"\.(?: |$)", text)) - 1  # minus the "Day N." marker
            self.assertTrue(3 <= n <= 8 + 3 * 3, (n, text))  # fact + 2-5 filler + the planted distractors

    def test_every_value_lives_in_exactly_its_own_episode(self) -> None:
        by_ep = {k: t for k, _, t in self.fx.episodes}
        for p in self.fx.probes + self.fx.forgets:
            holders = [k for k, t in by_ep.items() if p.value in words(t)]
            self.assertEqual(holders, [p.episode])
            self.assertIn(p.fact_sentence, by_ep[p.episode])

    def test_tokens_are_fixed_length_distinct_and_never_a_substring_of_another(self) -> None:
        ps = self.fx.probes + self.fx.forgets
        toks = [p.value for p in ps] + [p.entity.lower() for p in ps]
        self.assertEqual({len(t) for t in toks}, {6})
        self.assertEqual(len({p.value for p in ps}), len(ps))
        self.assertFalse({p.value for p in ps} & {p.entity.lower() for p in ps})

    def test_about_half_the_queries_omit_the_attribute_word_via_the_fixed_lexicon(self) -> None:
        para = [p for p in self.fx.probes if p.mode == "paraphrase"]
        share = len(para) / len(self.fx.probes)
        self.assertTrue(0.35 < share < 0.65, share)
        for p in para:
            self.assertNotIn(p.attr, p.query.lower())
            self.assertEqual(p.query, v2.PARAPHRASE[p.attr].format(e=p.entity))
        for p in self.fx.probes:
            if p.mode == "canonical":
                self.assertIn(p.attr, p.query)

    def test_the_reader_knows_the_lexicon_and_the_record_says_so(self) -> None:
        bb = StubBackbone("strict")
        self.assertTrue(bb.knows_lexicon)
        p = next(p for p in self.fx.probes if p.mode == "paraphrase")
        text = dict((k, t) for k, _, t in self.fx.episodes)[p.episode]
        self.assertEqual(bb.generate(p.query, [text]), p.value)
        self.assertEqual(bb.generate(p.query, ["Nobody could find the talabu that evening."]), ABSTAIN)

    def test_distractors_name_the_same_entity_and_attr_without_a_value(self) -> None:
        by_ep = {k: t for k, _, t in self.fx.episodes}
        counts = []
        for p in self.fx.probes:
            marker = f"{p.entity}'s {p.attr} in passing."
            hits = [k for k, t in by_ep.items() if marker in t]
            counts.append(len(hits))
            for k in hits:
                sent = next(x for x in by_ep[k].split(". ") if marker[:-1] in x)
                self.assertNotIn(p.value, words(sent))  # no value in a distractor
        self.assertTrue(any(p.episode not in [k for k, t in by_ep.items() if f"{p.entity}'s {p.attr} in passing." in t]
                            and any(f"{p.entity}'s {p.attr} in passing." in t for t in by_ep.values())
                            for p in self.fx.probes))  # some distractors sit in OTHER episodes
        self.assertTrue(all(0 <= c <= v2.MAX_DISTRACTORS for c in counts), max(counts))
        self.assertGreater(len([c for c in counts if c > 0]), len(counts) // 3)

    def test_probes_are_bucketed_across_the_timeline(self) -> None:
        self.assertEqual({p.bucket for p in self.fx.probes}, {"first", "middle", "recent"})
        order = {k: i for i, (k, _, _) in enumerate(self.fx.episodes)}
        n = len(self.fx.episodes)
        for p in self.fx.probes:
            want = "first" if order[p.episode] < n / 3 else ("middle" if order[p.episode] < 2 * n / 3 else "recent")
            self.assertEqual(p.bucket, want)

    def test_one_forget_probe_per_entity_and_the_rest_are_retained_probes(self) -> None:
        n = v2.SIZES["medium"]
        self.assertEqual((len(self.fx.forgets), len(self.fx.probes)), (n, 4 * n))
        self.assertEqual({p.fact_key for p in self.fx.forgets} & {p.fact_key for p in self.fx.probes}, set())
        self.assertEqual(len({p.entity for p in self.fx.forgets}), n)

    def test_scale_reaches_xlarge(self) -> None:
        self.assertEqual(v2.SIZES["xlarge"], 300)
        self.assertEqual(len(v2.generate_v2(1, "xlarge").episodes), 5 * 300)


class TestV2DerivedBudgets(unittest.TestCase):
    """Budgets are pure functions of the fixture, pinned by RULE (not by value)."""

    def test_budgets_follow_the_rule_for_several_fixtures(self) -> None:
        for seed, size in ((1, "tiny"), (2, "small"), (3, "medium")):
            f = v2.generate_v2(seed, size)
            lens = sorted(count_tokens(t) for _, _, t in f.episodes)
            med = lens[len(lens) // 2]
            fact = sorted(count_tokens(p.fact_sentence) for p in f.probes + f.forgets)[(len(f.probes) + len(f.forgets)) // 2]
            self.assertEqual(f.retrieval_budget(count_tokens), 5 * med)
            self.assertEqual(f.e3_budgets(count_tokens), (fact, med, 5 * med))
            self.assertEqual(f.e3_budgets(count_tokens), v2.generate_v2(seed, size).e3_budgets(count_tokens))

    def test_the_budget_is_a_function_of_the_counter_not_a_constant(self) -> None:
        f = v2.generate_v2(1, "small")
        self.assertEqual(f.retrieval_budget(lambda t: 2 * count_tokens(t)), 2 * f.retrieval_budget(count_tokens))


if __name__ == "__main__":
    unittest.main()
