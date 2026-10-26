#!/usr/bin/env python3
"""The ORG-062 tier-1 episodic view (T-2518 step 2d): authorization before reading, backlinks that resolve,
forget / supersede, the budget, determinism, and no fixture knowledge. Real store, real FTS5 projections,
real ``correct`` / ``forget`` / ``evaluate_retrieval`` over a temp dir. Synthetic data only; no network."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.pmb1 import offline
from tools.pmb1.episodic import EpisodicTierSubstrate
from tools.pmb1.substrate import Item
from tools.pmb1.tokens import count_tokens as harness_count_tokens

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_tier_episodic as tier  # noqa: E402
import test_memory_store as fxs  # noqa: E402

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


EPISODE = "Day 1. Kabuto went to the market. Kabuto's beverage is aaaaaa. Zed waved at the crowd."
Q = "What is Kabuto's beverage?"


class _TierReads:
    """Records EVERY ``store.read_payload`` call, wherever and whenever it is made (before or after
    ``evaluate_retrieval``), EXCEPT the store's own internal reads inside ``store.retrievable_envelopes()`` (which
    verifies each payload; ``memory_store.py`` ``retrievable_envelopes``, also reached through ``lookup_*``). Also
    records the items ``evaluate_retrieval`` authorized. A read made BEFORE authorization is therefore seen (T-2518
    epx; semleg review finding 1: the old spy only recorded reads after ``evaluate_retrieval`` returned)."""

    _MISSING = object()

    def __init__(self, store) -> None:
        self.store, self.reads, self.items, self._inside = store, [], [], 0

    def __enter__(self):
        real_read, real_eval = self.store.read_payload, tier.ev.evaluate_retrieval
        # whatever is installed now (possibly a test's own mock.patch of retrievable_envelopes), restored on exit
        self._prior = {n: self.store.__dict__.get(n, self._MISSING) for n in ("read_payload", "retrievable_envelopes")}
        real_live = self.store.retrievable_envelopes

        def evaluate(request, envelopes, policy):
            view = real_eval(request, envelopes, policy)
            self.items = [(i.memory_id, i.version_id) for i in view.items]
            return view

        def live():
            self._inside += 1
            try:
                return real_live()
            finally:
                self._inside -= 1

        def read(mid, vid):
            if not self._inside:
                self.reads.append((mid, vid))
            return real_read(mid, vid)

        self._patch = mock.patch.object(tier.ev, "evaluate_retrieval", evaluate)
        self._patch.start()
        self.store.read_payload = read
        self.store.retrievable_envelopes = live
        return self

    def __exit__(self, *a) -> None:
        for n, v in self._prior.items():
            if v is self._MISSING:
                del self.store.__dict__[n]
            else:
                setattr(self.store, n, v)
        self._patch.stop()

    def assert_only_authorized_reads(self, case: unittest.TestCase) -> None:
        case.assertFalse([r for r in self.reads if r not in self.items], (self.reads, self.items))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.sub = EpisodicTierSubstrate()
        self.addCleanup(self.sub.close)

    def write(self, text: str, subject: str = "kabuto", privacy: str = "internal") -> str:
        ref = self.sub.write(Item(f"k{self.sub._n}", subject, text, privacy_class=privacy))
        self.sub.store.rebuild_projections()
        return ref

    def req(self, **kw):
        d = dict(request_id="t", purpose="companionship", subject_scope=(), item_budget=0, token_budget=0,
                 privacy_ceiling="personal")
        d.update(kw)
        return fxs.retrieval_request(**d)

    def extracts(self, query: str = Q, *, budget: int = 1000, top_k: int = 5, **kw):
        return tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(**kw), query,
                                      budget_tokens=budget, top_k=top_k)


class TestExtraction(_Case):
    def test_returns_the_matching_sentence_not_the_whole_episode(self) -> None:
        self.write(EPISODE)
        got = self.extracts()
        self.assertEqual(got[-1].text, "Kabuto's beverage is aaaaaa.")  # reading order: best score LAST
        self.assertEqual(got[-1].sentence_index, 2)
        self.assertNotIn("Zed waved at the crowd.", [x.text for x in got])  # no query term: not returned
        self.assertLess(count_tokens_of(got), harness_count_tokens(EPISODE))

    def test_split_sentences_are_verbatim_substrings(self) -> None:
        for s in tier.split_sentences(EPISODE):
            self.assertIn(s, EPISODE)

    def test_the_disposition_travels_with_the_extract(self) -> None:
        self.write(EPISODE)
        self.assertEqual(self.extracts()[0].use_disposition, "allowed")
        self.assertEqual(self.extracts(purpose="assertion")[0].use_disposition, "quarantined")

    def test_token_unit_equals_the_harness_unit(self) -> None:
        for t in ("", "one", "a b  c\td\n", EPISODE):
            self.assertEqual(tier.count_tokens(t), harness_count_tokens(t))


def count_tokens_of(extracts) -> int:
    return sum(tier.count_tokens(x.text) for x in extracts)


class TestAuthorizationBeforeReading(_Case):
    def test_above_the_privacy_ceiling_never_yields_an_extract_and_is_never_read(self) -> None:
        secret = self.write("Kabuto's beverage is secret.", privacy="personal")
        plain = self.write("Kabuto's snack is plain.", privacy="internal")
        with _TierReads(self.sub.store) as spy:
            got = self.extracts("Kabuto's beverage snack", privacy_ceiling="internal")
        self.assertEqual([x.text for x in got], ["Kabuto's snack is plain."])
        self.assertEqual(spy.reads, [tuple(plain.split("/"))])
        spy.assert_only_authorized_reads(self)
        self.assertNotIn(tuple(secret.split("/")), spy.reads)
        self.assertEqual(len(self.extracts("Kabuto's beverage snack", privacy_ceiling="personal")), 2)

    def test_the_ceiling_test_goes_red_when_authorization_is_removed(self) -> None:
        """Guard proof: with ``evaluate_retrieval`` replaced by a pass-through, the over-ceiling envelope IS read."""
        secret = self.write("Kabuto's beverage is secret.", privacy="personal")

        def passthrough(request, envelopes, policy):
            return mock.Mock(items=[mock.Mock(memory_id=e.memory_id, version_id=e.version_id,
                                              use_disposition=mock.Mock(value="allowed")) for e in envelopes])

        with mock.patch.object(tier.ev, "evaluate_retrieval", passthrough), _TierReads(self.sub.store) as spy:
            got = self.extracts("Kabuto's beverage", privacy_ceiling="internal")
        self.assertEqual(spy.reads, [tuple(secret.split("/"))])
        self.assertEqual([x.text for x in got], ["Kabuto's beverage is secret."])
        real_view = tier.ev.evaluate_retrieval(self.req(privacy_ceiling="internal"),
                                               list(self.sub.store.retrievable_envelopes()), self.sub._trust)
        self.assertNotIn(tuple(secret.split("/")), [(i.memory_id, i.version_id) for i in real_view.items])

    def test_an_unknown_purpose_yields_nothing_and_reads_nothing(self) -> None:
        self.write(EPISODE)
        with _TierReads(self.sub.store) as spy:
            self.assertEqual(self.extracts(purpose="no-such-purpose"), [])
        self.assertEqual(spy.reads, [])

    def test_a_malformed_request_yields_nothing_and_reads_nothing(self) -> None:
        self.write(EPISODE)
        with _TierReads(self.sub.store) as spy:
            got = tier.retrieve_extracts(self.sub.store, self.sub._trust, object(), Q, budget_tokens=100, top_k=5)
        self.assertEqual(got, [])
        self.assertEqual(spy.reads, [])

    def test_the_spy_does_see_a_tier_read_when_authorized(self) -> None:
        ref = self.write(EPISODE)
        with _TierReads(self.sub.store) as spy:
            self.extracts()
        self.assertEqual(spy.reads, [tuple(ref.split("/"))])


class TestSelection(_Case):
    def test_the_fact_sentence_survives_a_filler_sentence_sharing_a_query_word(self) -> None:
        """Reviewer repro (brittle.py): the filler shares 'evening' with the query, the fact shares 'kabuto'+'hobby'."""
        self.write("Kabuto's hobby is chess. Kabuto stayed home that evening.")
        got = self.extracts("What does Kabuto do in the evening for fun?")
        self.assertIn("Kabuto's hobby is chess.", [x.text for x in got])

    def test_the_fact_sentence_is_kept_when_only_the_entity_matches(self) -> None:
        self.write("Kabuto's hobby is chess. Zed waved.")
        self.assertEqual([x.text for x in self.extracts("What does Kabuto do for fun?")], ["Kabuto's hobby is chess."])

    def test_order_is_score_then_episode_rank_then_source_order_reversed_for_reading(self) -> None:
        """Ranking is score, then episode rank, then source order; the output is that ranking REVERSED (T-4937),
        so the best extract sits last, adjacent to the question in a reader's prompt."""
        self.write("Kabuto sat. Kabuto's beverage is tea.", subject="a")
        self.write("Kabuto's beverage is milk. Kabuto stood.", subject="b")
        got = [x.text for x in self.extracts("Kabuto beverage")]
        self.assertEqual(got, ["Kabuto stood.", "Kabuto sat.", "Kabuto's beverage is milk.", "Kabuto's beverage is tea."])

    def test_reading_order_changes_only_the_order_never_the_selection(self) -> None:
        """T-4937: the budget still keeps the BEST extracts; only their presentation order flips. A tight budget that
        fits two extracts must keep the two score-2 sentences, whatever order they come out in."""
        self.write("Kabuto sat. Kabuto's beverage is tea.", subject="a")
        self.write("Kabuto's beverage is milk. Kabuto stood.", subject="b")
        got = [x.text for x in self.extracts("Kabuto beverage", budget=8)]
        self.assertEqual(got, ["Kabuto's beverage is milk.", "Kabuto's beverage is tea."])

    def test_top_k_counts_episodes_that_return_something(self) -> None:
        self.write("Kabuto is a very long sentence " + "word " * 40 + "end.", subject="a")
        self.write("Kabuto's beverage is tea.", subject="b")
        self.write("Kabuto's beverage is milk.", subject="c")
        got = self.extracts("Kabuto beverage", budget=10, top_k=2)
        self.assertEqual(len({x.memory_id for x in got}), 2)  # the over-budget episode does not use up a slot

    def test_the_stoplist_is_the_reference_callers(self) -> None:
        from tools.pmb1 import governed
        self.assertEqual(tier._STOP, governed._STOP)


class TestBacklinks(_Case):
    def test_every_backlink_resolves_to_a_retrievable_version_holding_the_text_verbatim(self) -> None:
        self.write(EPISODE)
        self.write("Zed's beverage is bbbbbb. Zed sat down.", subject="zed")
        got = self.extracts("beverage Kabuto Zed")
        self.assertGreaterEqual(len(got), 2)
        live = {(e.memory_id, e.version_id) for e in self.sub.store.retrievable_envelopes()}
        for x in got:
            mid, vid, idx = x.backlink
            self.assertIn((mid, vid), live)
            payload = self.sub.store.read_payload(mid, vid)
            self.assertIn(x.text, payload)
            self.assertEqual(tier.split_sentences(payload)[idx], x.text)


class TestLifecycle(_Case):
    def test_forget_removes_every_extract_of_that_memory(self) -> None:
        ref = self.write(EPISODE)
        self.write("Zed's beverage is bbbbbb.", subject="zed")
        self.assertTrue(self.extracts("beverage Kabuto Zed"))
        self.sub.forget(ref)
        self.sub.store.rebuild_projections()
        got = self.extracts("beverage Kabuto Zed")
        self.assertEqual([x.text for x in got], ["Zed's beverage is bbbbbb."])
        self.assertFalse([x for x in got if x.memory_id == ref.split("/")[0]])
        self.assertEqual(self.extracts("Kabuto aaaaaa"), [])

    def test_supersede_leaves_no_extract_of_the_old_version(self) -> None:
        old = self.write(EPISODE)
        new = self.sub.supersede(old, Item("n", "kabuto", "Kabuto's beverage is cccccc. Kabuto left."))
        self.sub.store.rebuild_projections()
        got = self.extracts()
        self.assertEqual({(x.memory_id, x.version_id) for x in got}, {tuple(new.split("/"))})
        self.assertEqual(got[-1].text, "Kabuto's beverage is cccccc.")
        self.assertFalse([x for x in got if "aaaaaa" in x.text])


class TestBudget(_Case):
    def setUp(self) -> None:
        super().setUp()
        for i in range(4):
            self.write(f"Day {i}. Kabuto walked about. Kabuto's beverage is tea{i} and more words here.", subject=f"k{i}")

    def test_the_summed_content_never_exceeds_the_budget(self) -> None:
        for budget in (0, 1, 3, 4, 7, 8, 15, 23, 50, 230, 10_000):
            got = self.extracts("Kabuto's beverage", budget=budget)
            self.assertLessEqual(count_tokens_of(got), budget, budget)

    def test_budget_zero_returns_nothing(self) -> None:
        self.assertEqual(self.extracts(budget=0), [])
        self.assertEqual(self.extracts(budget=-5), [])

    def test_top_k_bounds_the_episodes(self) -> None:
        got = self.extracts("Kabuto's beverage", top_k=2)
        self.assertEqual(len({(x.memory_id, x.version_id) for x in got}), 2)


class TestDeterminism(_Case):
    def test_same_inputs_same_output(self) -> None:
        for i in range(3):
            self.write(f"Day {i}. Kabuto's beverage is t{i}. Kabuto sat.", subject=f"s{i}")
        a, b = self.extracts("Kabuto beverage"), self.extracts("Kabuto beverage")
        self.assertEqual(a, b)
        self.assertTrue(a)


class TestNoFixtureKnowledge(unittest.TestCase):
    def test_the_module_imports_nothing_from_the_fixtures_or_the_harness(self) -> None:
        src = Path(tier.__file__).read_text(encoding="utf-8")
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
        self.assertFalse([m for m in mods if "fixtures" in m or "pmb1" in m or m.startswith("test_")], mods)
        self.assertNotIn("PARAPHRASE", src)


if __name__ == "__main__":
    unittest.main()
