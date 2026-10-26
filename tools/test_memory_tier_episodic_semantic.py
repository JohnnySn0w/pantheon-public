#!/usr/bin/env python3
"""The tier-1 episodic view's optional SEMANTIC LEG (T-2518 semleg): a paraphrase with zero term overlap is found
through an injected embedder; governance holds (an item above the ceiling, under a refused purpose, superseded or
forgotten is never returned, never ``read_payload``'d and never embedded), each with a RED proof that removes the
guard; RRF ranking and its tie-break; ``top_k`` / budget / best-LAST unchanged. Real store, real
``evaluate_retrieval``; the embedder is a table-driven fake (fake cosines). Synthetic data only; no network."""
from __future__ import annotations

import os
import sys
import unittest
from fractions import Fraction
from unittest import mock

from tools.pmb1 import offline
from tools.pmb1.substrate import Item
from tools.test_memory_tier_episodic import _Case, _TierReads, count_tokens_of

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_tier_episodic as tier  # noqa: E402

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


Q = "Which drink suits the turtle?"          # no term shared with any episode below
GOLD = "Kabuto's beverage is aaaaaa."
EPISODE = f"Day 1. Kabuto went to the market. {GOLD} Zed waved at the crowd."
QV, HIT, MISS = [1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]
GOLD_ZED = "Zed's beverage is aaaaaa."    # shares no term with any Kabuto query below


class FakeEmb:
    """Table-driven fake: a text in ``table`` gets that vector, any other text ``MISS`` (cosine 0 with ``QV``).
    Records every document and query it is asked to embed."""

    name = "fake-semantic"

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = dict(table or {})
        self.docs: list[str] = []
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.table.get(text, QV)

    def embed_document(self, text: str) -> list[float]:
        self.docs.append(text)
        return self.table.get(text, MISS)


def passthrough(request, envelopes, policy):
    """The guard removed: every envelope handed in comes back as an allowed item."""
    return mock.Mock(items=[mock.Mock(memory_id=e.memory_id, version_id=e.version_id,
                                      use_disposition=mock.Mock(value="allowed")) for e in envelopes],
                     omitted_reason_counts={})


class _SemCase(_Case):
    def sem(self, query: str = Q, emb: FakeEmb | None = None, *, budget: int = 1000, top_k: int = 5,
            tau: float = 0.5, sem_k: int = 10, **kw):
        return tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(**kw), query, budget_tokens=budget,
                                      top_k=top_k, embedder=emb or FakeEmb(), tau=tau, sem_k=sem_k)

    def all_versions(self):
        """Every stored version, superseded and forgotten ones included: what the store must NOT hand the tier."""
        s = self.sub.store
        return tuple(e for m in s.memory_ids() for v in s.version_ids(m) for e in (s.stored_envelope(m, v),) if e)


class TestParaphrase(_SemCase):
    def test_zero_overlap_high_cosine_retrieves_the_gold_sentence_only_with_the_embedder(self) -> None:
        self.write(EPISODE)
        self.assertEqual(self.sub.store.lookup_content("drink"), ())
        self.assertEqual(self.extracts(Q), [])                       # lexical: nothing
        got = self.sem(Q, FakeEmb({GOLD: HIT}))
        self.assertEqual([x.text for x in got], [GOLD])            # only the sentence over tau
        self.assertEqual(got[-1].sentence_index, 2)

    def test_the_query_is_embedded_as_a_query_and_sentences_as_documents(self) -> None:
        self.write(EPISODE)
        emb = FakeEmb({GOLD: HIT})
        self.sem(Q, emb)
        self.assertEqual(emb.queries, [Q])
        self.assertEqual(sorted(emb.docs), sorted(tier.split_sentences(EPISODE)))

    def test_below_tau_and_no_overlap_is_not_a_candidate(self) -> None:
        self.write(EPISODE)
        near = [0.55, 0.0, (1 - 0.55 ** 2) ** 0.5]                  # cosine 0.55 with QV
        self.assertEqual(self.sem(Q, FakeEmb({GOLD: near}), tau=0.6), [])
        self.assertEqual([x.text for x in self.sem(Q, FakeEmb({GOLD: near}), tau=0.55)], [GOLD])

    def test_overlap_plus_cosine_orders_sentences(self) -> None:
        """Order key -(overlap + cosine): an overlap-1 sentence (score 1.0) beats a cosine-0.9 one; best LAST."""
        self.write("Kabuto sat. Kabuto's beverage is tea.")
        v09 = [0.9, (1 - 0.81) ** 0.5, 0.0]
        got = self.sem("Kabuto drink", FakeEmb({"Kabuto's beverage is tea.": v09}), tau=0.5)
        # "Kabuto sat." has overlap 1 + cos 0; the beverage sentence overlap 1 + cos 0.9 -> best (LAST)
        self.assertEqual([x.text for x in got], ["Kabuto sat.", "Kabuto's beverage is tea."])
        self.write("Zed sat.", subject="zed")
        got = self.sem("Zed drink", FakeEmb({"Kabuto's beverage is tea.": v09}), tau=0.5)
        self.assertEqual([x.text for x in got], ["Kabuto's beverage is tea.", "Zed sat."])  # 1.0 > 0.9: Zed's LAST


class TestGovernance(_SemCase):
    """A semantic-only match is never returned, read or embedded when it is above the ceiling, under a refused
    purpose, superseded or forgotten. Each has a RED twin that removes exactly that guard."""

    SECRET = "Kabuto's beverage is secret."

    def _ceiling_setup(self):
        secret = self.write(self.SECRET, privacy="personal")
        plain = self.write("Zed sat down.", subject="zed", privacy="internal")
        return tuple(secret.split("/")), tuple(plain.split("/"))

    def test_above_the_privacy_ceiling(self) -> None:
        secret, plain = self._ceiling_setup()
        emb = FakeEmb({self.SECRET: HIT})
        with _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb, privacy_ceiling="internal")
        self.assertEqual(got, [])
        self.assertEqual(spy.reads, [plain])
        spy.assert_only_authorized_reads(self)
        self.assertNotIn(self.SECRET, emb.docs)
        self.assertEqual([x.text for x in self.sem(Q, FakeEmb({self.SECRET: HIT}), privacy_ceiling="personal")],
                         [self.SECRET])

    def test_red_above_the_privacy_ceiling_without_authorization(self) -> None:
        secret, _ = self._ceiling_setup()
        emb = FakeEmb({self.SECRET: HIT})
        with mock.patch.object(tier.ev, "evaluate_retrieval", passthrough), _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb, privacy_ceiling="internal")
        self.assertIn(secret, spy.reads)
        self.assertIn(self.SECRET, emb.docs)
        self.assertEqual([x.text for x in got], [self.SECRET])

    def test_a_refused_purpose(self) -> None:
        self.write(EPISODE)
        emb = FakeEmb({GOLD: HIT})
        with _TierReads(self.sub.store) as spy:
            self.assertEqual(self.sem(Q, emb, purpose="no-such-purpose"), [])
        self.assertEqual((spy.reads, emb.docs, emb.queries), ([], [], []))

    def test_red_a_refused_purpose_without_authorization(self) -> None:
        ref = self.write(EPISODE)
        emb = FakeEmb({GOLD: HIT})
        with mock.patch.object(tier.ev, "evaluate_retrieval", passthrough), _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb, purpose="no-such-purpose")
        self.assertEqual(spy.reads, [tuple(ref.split("/"))])
        self.assertIn(GOLD, emb.docs)
        self.assertEqual([x.text for x in got], [GOLD])

    def _supersede_setup(self):
        old = self.write(EPISODE)
        new = self.sub.supersede(old, Item("n", "kabuto", "Kabuto left town."))
        self.sub.store.rebuild_projections()
        return tuple(old.split("/")), tuple(new.split("/"))

    def test_a_superseded_version(self) -> None:
        old, new = self._supersede_setup()
        emb = FakeEmb({GOLD: HIT})
        with _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb)
        self.assertEqual(got, [])
        self.assertEqual(spy.reads, [new])
        self.assertEqual(emb.docs, ["Kabuto left town."])

    def test_red_a_superseded_version_when_the_store_filter_is_bypassed(self) -> None:
        old, _ = self._supersede_setup()
        emb = FakeEmb({GOLD: HIT})
        with mock.patch.object(self.sub.store, "retrievable_envelopes", self.all_versions), \
                _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb)
        self.assertIn(old, spy.reads)
        self.assertIn(GOLD, emb.docs)
        self.assertEqual([x.text for x in got], [GOLD])

    def _forget_setup(self):
        ref = self.write(EPISODE)
        self.write("Zed sat down.", subject="zed")
        self.sub.forget(ref)
        self.sub.store.rebuild_projections()
        return tuple(ref.split("/"))

    def test_a_forgotten_memory(self) -> None:
        gone = self._forget_setup()
        emb = FakeEmb({GOLD: HIT})
        with _TierReads(self.sub.store) as spy:
            got = self.sem(Q, emb)
        self.assertEqual(got, [])
        self.assertNotIn(gone, spy.reads)
        self.assertFalse([d for d in emb.docs if d in tier.split_sentences(EPISODE)])

    def test_red_a_forgotten_memory_when_the_store_filter_is_bypassed(self) -> None:
        gone = self._forget_setup()
        self.assertIn(gone, [(e.memory_id, e.version_id) for e in self.all_versions()])
        emb = FakeEmb({GOLD: HIT})
        with mock.patch.object(self.sub.store, "retrievable_envelopes", self.all_versions), \
                _TierReads(self.sub.store) as spy:
            self.sem(Q, emb)
        self.assertIn(gone, spy.reads)  # the read is attempted: the only thing between the tier and the payload


class TestRRF(unittest.TestCase):
    A, B, C = ("m-1", "v-1"), ("m-2", "v-1"), ("m-3", "v-1")

    def test_the_formula_with_k_60_and_1_based_ranks(self) -> None:
        self.assertEqual(tier.RRF_K, 60)
        s = tier.rrf_scores([self.A, self.B], [self.B, self.C])
        self.assertEqual(s, {self.A: Fraction(1, 61), self.B: Fraction(1, 62) + Fraction(1, 61), self.C: Fraction(1, 62)})

    def test_a_tie_breaks_on_memory_then_version_id(self) -> None:
        self.assertEqual(tier.fuse_ranks([self.B, self.A], [self.A, self.B], 10), [self.A, self.B])
        self.assertEqual(tier.fuse_ranks([self.A, self.B], [self.B, self.A], 10), [self.A, self.B])
        v2 = ("m-1", "v-2")   # equal fused score (both 1/61), same memory: the version id decides
        self.assertEqual(tier.fuse_ranks([v2], [self.A], 10), [self.A, v2])

    def test_candidates_are_lexical_union_top_sem_k(self) -> None:
        # true top-N: C is outside the semantic top 1, so it keeps its lexical 1/61 only; A ties it and wins on id
        self.assertEqual(tier.fuse_ranks([self.C], [self.A, self.B, self.C], 1), [self.A, self.C])
        self.assertEqual(tier.fuse_ranks([self.C], [self.A, self.B, self.C], 0), [self.C])
        self.assertEqual(tier.fuse_ranks([], [self.A, self.B, self.C], 2), [self.A, self.B])

    def test_the_fused_score_uses_only_the_semantic_top_sem_k(self) -> None:
        """T-2518 epx: the truncated semantic list feeds the fused score, not only the candidate set."""
        lex, sem = [self.A, self.B], [self.C, self.A, self.B]
        self.assertEqual(tier.fuse_ranks(lex, sem, 1), [self.A, self.C, self.B])   # B: lexical 1/62 only
        self.assertEqual(tier.fuse_ranks(lex, sem, 3), [self.A, self.B, self.C])   # B: 1/62 + 1/63 > C: 1/61

    def test_a_semantic_rank_1_item_outranks_a_lexical_item_outside_the_semantic_top_sem_k(self) -> None:
        """A semantic-only item at semantic rank 1 (1/61) beats a lexical item at lexical rank >= 2 outside the
        semantic top sem_k (1/62 at best). Under full-list fusion that item's tail semantic rank gave it a second
        term and buried the semantic item."""
        lex = [("m-%02d" % i, "v-1") for i in range(1, 7)]
        gold = ("m-99", "v-1")
        sem = [gold] + lex                       # the lexical items sit at semantic ranks 2-7
        got = tier.fuse_ranks(lex, sem, 1)
        self.assertEqual(got.index(gold), 1)     # behind lexical rank 1 (tie at 1/61, id m-01 < m-99), ahead of 2-6
        for i in range(1, 6):
            self.assertLess(got.index(gold), got.index(lex[i]))
        full = tier.rrf_scores(lex, sem)         # the old full-list fusion: every lexical item outranks gold
        self.assertTrue(all(full[k] > full[gold] for k in lex))

    def test_deterministic(self) -> None:
        a = tier.fuse_ranks([self.A, self.C], [self.C, self.B, self.A], 2)
        self.assertEqual(a, tier.fuse_ranks([self.A, self.C], [self.C, self.B, self.A], 2))
        self.assertEqual(a, [self.C, self.A, self.B])


class TestEpisodeExpansion(_SemCase):
    """T-2518 epx: episode expansion is real. A top-semantic gold episode is visited before the ``top_k`` break, and
    ``sem_k`` changes which episodes are visited. Each test is RED on the full-list fusion of pre-epx main (shown in
    the T-2518 epx report)."""

    def _lexical_crowd(self, n: int, text: str = "Kabuto had a drink.") -> list[tuple[str, str]]:
        return [tuple(self.write(text).split("/")) for _ in range(n)]

    def test_a_zero_overlap_gold_episode_is_retrieved_past_five_lexical_episodes(self) -> None:
        crowd = self._lexical_crowd(6)
        gold = tuple(self.write(GOLD_ZED, subject="zed").split("/"))
        q = "What does Kabuto like to drink?"
        self.assertEqual(tier._sentence_scores([GOLD_ZED], tier.query_terms(q)), [0])   # zero term overlap
        lexical = self.extracts(q)
        self.assertEqual(len({x.backlink[:2] for x in lexical}), 5)                      # 5+ lexical episodes return
        self.assertNotIn(gold, {x.backlink[:2] for x in lexical})
        got = tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(), q, budget_tokens=1000, top_k=5,
                                     embedder=FakeEmb({GOLD_ZED: HIT}))                 # the defaults: tau 0.80, sem_k 5
        self.assertIn(gold, {x.backlink[:2] for x in got})
        self.assertIn(GOLD_ZED, [x.text for x in got])                                   # admitted on cos 1.0 >= tau
        self.assertEqual(len({x.backlink[:2] for x in got}), 5)
        self.assertTrue(crowd)

    def test_a_deep_lexical_top_semantic_gold_episode_is_retrieved(self) -> None:
        """The DEV anatomy shape (17 of 17 misses): gold has overlap 1 (the entity), sits deep in the lexical list
        (rank 13 here; >= 6) and at semantic rank 1. Full-list fusion ranked it 6th, behind the top_k break."""
        self._lexical_crowd(12)                                                          # score 3: content x2 + subject
        gold = tuple(self.write(GOLD).split("/"))                                         # score 2: content + subject
        q = "Kabuto drink"
        emb = FakeEmb({GOLD: HIT})
        self.assertEqual(tier._sentence_scores([GOLD], tier.query_terms(q)), [1])        # overlap 1
        got = tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(), q, budget_tokens=1000, top_k=5,
                                     embedder=emb)
        self.assertIn(gold, {x.backlink[:2] for x in got})
        self.assertIn(GOLD, [x.text for x in got])
        # the rank pins: lexical rank 13, semantic rank 1, and top-N fused rank 5 (inside the top_k)
        live = sorted(self.sub.store.retrievable_envelopes(), key=lambda e: (e.memory_id, e.version_id))
        keys = [(e.memory_id, e.version_id) for e in live]
        lex = sorted(keys, key=lambda k: (-(2 if k == gold else 3), k))
        sem = [gold] + sorted(k for k in keys if k != gold)
        self.assertEqual((lex.index(gold) + 1, tier.fuse_ranks(lex, sem, 5).index(gold) + 1), (13, 5))
        full = tier.rrf_scores(lex, sem)
        self.assertEqual(sorted(keys, key=lambda k: (-full[k], k)).index(gold) + 1, 6)   # pre-epx: 6th, not visited

    def test_sem_k_changes_the_visited_episodes(self) -> None:
        crowd = sorted(self._lexical_crowd(6, "Kabuto sat."))
        s1 = tuple(self.write("Zed rode a boat.", subject="zed").split("/"))
        s2 = tuple(self.write("Yuu swam far.", subject="yuu").split("/"))
        v09, v08 = [0.9, (1 - 0.81) ** 0.5, 0.0], [0.8, 0.6, 0.0]
        table = {"Zed rode a boat.": v09, "Yuu swam far.": v08}

        def visited(sem_k: int) -> set:
            got = self.sem("Kabuto", FakeEmb(table), top_k=3, tau=0.7, sem_k=sem_k)
            return {x.backlink[:2] for x in got}

        # sem_k 1: S1 (1/61) ties lexical rank 1 and beats lexical rank 2 (1/62). sem_k 5: the semantic top 5 also
        # holds lexical ranks 1-3, whose two terms each beat S1's one.
        self.assertEqual(visited(1), {crowd[0], s1, crowd[1]})
        self.assertEqual(visited(5), {crowd[0], crowd[1], crowd[2]})
        self.assertNotIn(s2, visited(1) | visited(5))


class TestPreAuthorizationReadIsSeen(_SemCase):
    """Semleg review finding 1: a mutant that ``read_payload``s every live envelope BEFORE ``evaluate_retrieval``
    must be caught by the spy (the old spy recorded reads only after authorization, so the mutant survived)."""

    @staticmethod
    def _mutant(name: str):
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(getattr(tier, name)))
        anchor = "    view = ev.evaluate_retrieval("
        assert src.count(anchor) == 1, name
        inject = ("    for _e in (list(live.values()) if isinstance(live, dict) else live):\n"
                  "        store.read_payload(_e.memory_id, _e.version_id)\n")
        ns = dict(vars(tier))
        exec(compile(src.replace(anchor, inject + anchor), f"<pre-auth-read mutant of {name}>", "exec"), ns)
        return ns[name]

    def _secret(self):
        secret = tuple(self.write("Kabuto's beverage is secret.", privacy="personal").split("/"))
        self.write("Kabuto's snack is plain.", privacy="internal")
        return secret

    def test_red_semantic_leg(self) -> None:
        secret = self._secret()
        with mock.patch.object(tier, "_retrieve_semantic", self._mutant("_retrieve_semantic")), \
                _TierReads(self.sub.store) as spy:
            got = self.sem("Kabuto's beverage snack", FakeEmb(), privacy_ceiling="internal")
        self.assertNotIn(secret, {x.backlink[:2] for x in got})   # the output alone cannot see this leak
        self.assertIn(secret, spy.reads)
        with self.assertRaises(AssertionError):
            spy.assert_only_authorized_reads(self)

    def test_red_lexical_path(self) -> None:
        secret = self._secret()
        with mock.patch.object(tier, "retrieve_extracts", self._mutant("retrieve_extracts")), \
                _TierReads(self.sub.store) as spy:
            got = tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(privacy_ceiling="internal"),
                                         "Kabuto's beverage snack", budget_tokens=1000, top_k=5)
        self.assertNotIn(secret, {x.backlink[:2] for x in got})
        self.assertIn(secret, spy.reads)
        with self.assertRaises(AssertionError):
            spy.assert_only_authorized_reads(self)

    def test_the_unmutated_tier_passes_the_same_check(self) -> None:
        secret = self._secret()
        with _TierReads(self.sub.store) as spy:
            self.sem("Kabuto's beverage snack", FakeEmb(), privacy_ceiling="internal")
        self.assertNotIn(secret, spy.reads)
        self.assertTrue(spy.reads)
        spy.assert_only_authorized_reads(self)

    def test_the_stores_own_verification_reads_are_not_counted(self) -> None:
        self._secret()
        before = dict(self.sub.store.__dict__)       # the substrate's own memo wrapper included
        raw: list = []
        real = self.sub.store.read_payload
        with mock.patch.object(self.sub.store, "read_payload", lambda m, v: raw.append((m, v)) or real(m, v)):
            inner = dict(self.sub.store.__dict__)
            with _TierReads(self.sub.store) as spy:
                self.sub.store.retrievable_envelopes()
                self.sub.store.lookup_content("kabuto")
                self.sub.store.lookup_subject("kabuto")
            self.assertEqual(self.sub.store.__dict__, inner)   # restored on exit, exactly
        self.assertTrue(raw)                 # the store DID read internally (the exclusion is not vacuous) ...
        self.assertEqual(spy.reads, [])      # ... and none of those reads is counted against the tier
        self.assertEqual(self.sub.store.__dict__, before)


class TestRankingEndToEnd(_SemCase):
    def test_an_end_to_end_tie_breaks_on_memory_id_best_last(self) -> None:
        self.write("Alpha one.", subject="a")
        self.write("Beta two.", subject="b")
        v = [0.9, (1 - 0.81) ** 0.5, 0.0]
        got = [x.text for x in self.sem(Q, FakeEmb({"Alpha one.": v, "Beta two.": v}))]
        self.assertEqual(got, ["Beta two.", "Alpha one."])  # m-1 ranks first, so it comes LAST

    def test_independent_of_the_stores_envelope_order(self) -> None:
        for i in range(4):
            self.write(f"Item {i} fact. Kabuto note {i}.", subject=f"s{i}")
        table = {f"Item {i} fact.": [0.5 + 0.1 * i, (1 - (0.5 + 0.1 * i) ** 2) ** 0.5, 0.0] for i in range(4)}
        a = self.sem("Kabuto", FakeEmb(table), tau=0.55, top_k=3)
        real = self.sub.store.retrievable_envelopes
        with mock.patch.object(self.sub.store, "retrievable_envelopes", lambda: tuple(reversed(real()))):
            b = self.sem("Kabuto", FakeEmb(table), tau=0.55, top_k=3)
        self.assertEqual(a, b)
        self.assertTrue(a)
        self.assertEqual(a, self.sem("Kabuto", FakeEmb(table), tau=0.55, top_k=3))


class TestTopKCounting(_SemCase):
    def test_top_k_counts_episodes_that_return_something(self) -> None:
        long = "Kabuto is a very long sentence " + "word " * 40 + "end."
        self.write(long, subject="a")                                  # m-1: ranks first, never fits the budget
        self.write("Kabuto's beverage is tea.", subject="b")
        self.write("Kabuto's beverage is milk.", subject="c")
        table = {long.strip(): HIT, "Kabuto's beverage is tea.": HIT, "Kabuto's beverage is milk.": HIT}
        got = self.sem(Q, FakeEmb(table), budget=10, top_k=2)
        self.assertEqual(len({x.memory_id for x in got}), 2)  # the over-budget episode does not use up a slot


class TestInvariantsUnchanged(_SemCase):
    def setUp(self) -> None:
        super().setUp()
        for i in range(4):
            self.write(f"Day {i}. Kabuto walked about. Kabuto's beverage is tea{i} and more words here.", subject=f"k{i}")
        self.table = {f"Kabuto's beverage is tea{i} and more words here.": HIT for i in range(4)}

    def test_the_summed_content_never_exceeds_the_budget(self) -> None:
        for budget in (1, 3, 4, 7, 8, 15, 23, 50, 230, 10_000):
            for q in (Q, "Kabuto's beverage"):
                got = self.sem(q, FakeEmb(self.table), budget=budget)
                self.assertLessEqual(count_tokens_of(got), budget, (q, budget))

    def test_budget_or_top_k_zero_returns_nothing_and_embeds_nothing(self) -> None:
        for kw in ({"budget": 0}, {"budget": -5}, {"top_k": 0}):
            emb = FakeEmb(self.table)
            self.assertEqual(self.sem(Q, emb, **kw), [])
            self.assertEqual((emb.docs, emb.queries), ([], []))

    def test_top_k_bounds_the_episodes(self) -> None:
        got = self.sem(Q, FakeEmb(self.table), top_k=2)
        self.assertEqual(len({(x.memory_id, x.version_id) for x in got}), 2)

    def test_best_last_reading_order(self) -> None:
        got = self.sem("Kabuto's beverage", FakeEmb(self.table))
        keys = [-(len(set(tier.words(x.text)) & {"kabuto", "beverage"}) + (1.0 if x.text in self.table else 0.0))
                for x in got]
        self.assertEqual(keys, sorted(keys, reverse=True))  # ascending quality: best LAST

    def test_a_budget_truncated_view_raises_instead_of_shrinking_the_candidates(self) -> None:
        with self.assertRaises(tier.SemanticViewTruncated):
            self.sem(Q, FakeEmb(self.table), item_budget=1)
        with self.assertRaises(tier.SemanticViewTruncated):
            self.sem(Q, FakeEmb(self.table), token_budget=5)
        self.assertTrue(self.sem(Q, FakeEmb(self.table), item_budget=100))  # a budget that cuts nothing is fine

    def test_the_defaults_apply_when_tau_and_sem_k_are_not_given(self) -> None:
        emb = FakeEmb(self.table)
        got = tier.retrieve_extracts(self.sub.store, self.sub._trust, self.req(), Q, budget_tokens=1000, top_k=5,
                                     embedder=emb)
        self.assertEqual(len({x.memory_id for x in got}), 4)
        self.assertEqual((tier.DEFAULT_TAU, tier.DEFAULT_SEM_K, tier.RRF_K), (0.80, 5, 60))


if __name__ == "__main__":
    unittest.main()
