#!/usr/bin/env python3
"""The ORG-062 tier-2 semantic/relational view (T-2518 S4): slot / validity / conflict logic on hand-built synthetic
sentences, then REAL-PATH governance through the real store and the real lifecycle ops (``correct`` / ``forget``),
never injected state. Synthetic data only; no network.

Expectations follow the DEFINITIONS (brief L4), written by hand, never read back from the code's output:
* a slot is (entity, predicate), case-insensitive;
* the value at t is the set of values whose claim has the LATEST valid_from <= t;
* one distinct value = current; two or more = a conflict, all surfaced;
* a later valid_from closes the earlier interval.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

from tools.pmb1 import offline
from tools.pmb1.semantic import SemanticTierSubstrate
from tools.pmb1.substrate import Item

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_lifecycle_ops as ops  # noqa: E402
import memory_tier_semantic as t2  # noqa: E402
import test_memory_store as fxs  # noqa: E402

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


D = {n: f"2026-0{n}-01T00:00:00Z" for n in range(1, 8)}  # D[1] < D[2] < ... (request_time NOW is 2026-09-28)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rec(value: str, vf: str, mid: str = "m", vt: str | None = None, entity: str = "Kabuto", pred: str = "beverage"):
    return t2.ClaimRecord(t2.Claim(entity, pred, value), mid, "v-1", 0, f"{entity}'s {pred} is {value}.",
                          at(vf), None if vt is None else at(vt), "allowed")


def values(rs) -> set[str]:
    return {r.claim.norm_value for r in rs}


# --------------------------------------------------------------------------- pure logic (no store)
class TestExtractor(unittest.TestCase):
    def test_possessive_copula_claims_and_case_insensitive_slot(self) -> None:
        x = t2.PossessiveCopulaExtractor()
        self.assertEqual(x.claims("Kabuto's beverage is tea."), [t2.Claim("Kabuto", "beverage", "tea")])
        self.assertEqual(t2.Claim("KABUTO", "Beverage", "Tea").slot, ("kabuto", "beverage"))
        self.assertEqual(t2.Claim("KABUTO", "Beverage", "Tea").norm_value, "tea")
        for s in ("Zed mentioned Kabuto's beverage in passing.", "Kabuto went to the market.", "Day 3."):
            self.assertEqual(x.claims(s), [], s)

    def test_it_is_declared_a_pattern_stub(self) -> None:
        x = t2.DEFAULT_EXTRACTOR
        self.assertTrue(x.is_pattern_stub)
        self.assertTrue(x.name.startswith("pattern-stub"))  # tracks_v4.PATTERN_STUB_PREFIX: the R6 flag keys on it
        self.assertEqual(x.template_words, ())


class TestValidity(unittest.TestCase):
    def test_single_value_is_current_from_its_valid_from(self) -> None:
        rs = [rec("tea", D[2])]
        self.assertEqual(values(t2.valid_at(rs, at(D[3]))), {"tea"})
        self.assertEqual(values(t2.valid_at(rs, at(D[2]))), {"tea"})  # valid_from <= t: inclusive
        self.assertEqual(t2.valid_at(rs, at(D[1])), [])  # not yet true

    def test_a_later_valid_from_closes_the_earlier_interval(self) -> None:
        rs = [rec("tea", D[2], "a"), rec("gin", D[4], "b")]
        self.assertEqual(values(t2.valid_at(rs, at(D[5]))), {"gin"})
        self.assertEqual(values(t2.valid_at(rs, at(D[3]))), {"tea"})  # as of between: the earlier value
        self.assertEqual(t2.valid_at(rs, at(D[1])), [])

    def test_ingestion_order_is_not_valid_order(self) -> None:
        """Backfill: the later-INGESTED record is valid EARLIER; the base value stays current."""
        rs = [rec("tea", D[4], "base"), rec("gin", D[2], "late-arriving")]
        self.assertEqual(values(t2.valid_at(rs, at(D[5]))), {"tea"})
        self.assertEqual(values(t2.valid_at(rs, at(D[3]))), {"gin"})
        self.assertEqual(values(t2.valid_at(list(reversed(rs)), at(D[5]))), {"tea"})

    def test_two_values_at_the_same_instant_are_a_conflict(self) -> None:
        rs = [rec("tea", D[2], "a"), rec("gin", D[2], "b")]
        self.assertEqual(values(t2.valid_at(rs, at(D[3]))), {"tea", "gin"})

    def test_agreement_is_one_value(self) -> None:
        rs = [rec("tea", D[2], "a"), rec("TEA", D[2], "b")]
        got = t2.valid_at(rs, at(D[3]))
        self.assertEqual(len(got), 2)
        self.assertEqual(values(got), {"tea"})  # two sources, one distinct value: not a conflict

    def test_an_explicit_valid_to_closes_without_reviving_history(self) -> None:
        rs = [rec("tea", D[2], "a"), rec("gin", D[4], "b", vt=D[6])]
        self.assertEqual(values(t2.valid_at(rs, at(D[5]))), {"gin"})
        self.assertEqual(t2.valid_at(rs, at(D[6])), [])  # gin closed at D6; tea was closed by gin at D4
        self.assertEqual(t2.valid_at(rs, at(D[7])), [])


# --------------------------------------------------------------------------- the real store
class _Spy:
    """Every ``store.read_payload`` OUTSIDE the store's own ``retrievable_envelopes`` verification pass, and every
    ``evaluate_retrieval`` call, in ONE ordered event list; ``authorized`` is the union of every view's items."""

    def __init__(self, store) -> None:
        self.store, self.events, self.authorized, self._inside = store, [], set(), 0

    def __enter__(self):
        real_read, real_live, real_eval = self.store.read_payload, self.store.retrievable_envelopes, t2.ev.evaluate_retrieval

        def evaluate(request, envelopes, policy):
            view = real_eval(request, envelopes, policy)
            self.events.append(("eval", len(list(envelopes))))
            self.authorized |= {(i.memory_id, i.version_id) for i in view.items}
            return view

        def live():
            self._inside += 1
            try:
                return real_live()
            finally:
                self._inside -= 1

        def read(mid, vid):
            if not self._inside:
                self.events.append(("read", (mid, vid)))
            return real_read(mid, vid)

        self._p = [mock.patch.object(t2.ev, "evaluate_retrieval", evaluate),
                   mock.patch.object(self.store, "read_payload", read),
                   mock.patch.object(self.store, "retrievable_envelopes", live)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a) -> None:
        for p in reversed(self._p):
            p.stop()

    @property
    def reads(self) -> list:
        return [k for e, k in self.events if e == "read"]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.sub = SemanticTierSubstrate()
        self.addCleanup(self.sub.close)

    def write(self, text: str, vf: str, subject: str = "kabuto", privacy: str = "internal") -> tuple[str, str]:
        ref = self.sub.write(Item(f"k{self.sub._n}", subject, text, privacy_class=privacy, valid_from=vf))
        self.sub.store.rebuild_projections()
        return tuple(ref.split("/"))

    def req(self, **kw):
        d = dict(request_id="t", purpose="companionship", subject_scope=(), item_budget=0, token_budget=0,
                 privacy_ceiling="personal")
        d.update(kw)
        return fxs.retrieval_request(**d)

    def units(self, query: str = "What is Kabuto's beverage?", *, as_of: str | None = None, budget: int = 1000,
              top_k: int = 5, **kw) -> list:
        return t2.retrieve_units(self.sub.store, self.sub._trust, self.req(**kw), query, budget_tokens=budget,
                                 top_k=top_k, as_of=as_of)

    def asserted(self, us) -> set[str]:
        return {c.norm_value for u in us for c in t2.DEFAULT_EXTRACTOR.claims(u.text) if c.slot == ("kabuto", "beverage")}


class TestSlotView(_Case):
    def test_forward_change_serves_the_new_value_now_and_the_old_one_as_of(self) -> None:
        self.write("Day 1. Kabuto's beverage is tea. Kabuto sat.", D[2])
        self.write("Day 2. Kabuto's beverage is gin. Zed waved.", D[4], subject="zed")
        self.assertEqual(self.asserted(self.units()), {"gin"})
        self.assertEqual(self.asserted(self.units(as_of=D[3])), {"tea"})
        self.assertEqual(self.asserted(self.units(as_of=D[1])), set())  # nothing held yet

    def test_backfill_keeps_the_base_value_current(self) -> None:
        self.write("Kabuto's beverage is tea.", D[4])
        self.write("Kabuto's beverage is gin.", D[2])  # ingested later, valid earlier
        self.assertEqual(self.asserted(self.units()), {"tea"})
        self.assertEqual(self.asserted(self.units(as_of=D[3])), {"gin"})

    def test_a_conflict_surfaces_both_values_each_supported_by_the_other(self) -> None:
        a = self.write("Kabuto's beverage is tea.", D[2])
        b = self.write("Kabuto's beverage is gin.", D[2])
        got = {u.text: u for u in self.units()}
        self.assertEqual(self.asserted(got.values()), {"tea", "gin"})
        self.assertEqual(got["Kabuto's beverage is tea."].support, (b,))
        self.assertEqual(got["Kabuto's beverage is gin."].support, (a,))

    def test_an_agreement_is_not_a_conflict_and_has_no_support(self) -> None:
        self.write("Kabuto's beverage is tea.", D[2])
        self.write("Kabuto's beverage is tea.", D[2])
        us = self.units()
        self.assertEqual(self.asserted(us), {"tea"})
        self.assertTrue(all(u.support == () for u in us))

    def test_a_valid_value_the_extracts_missed_is_added_verbatim(self) -> None:
        """top_k=1: tier 1 returns ONE episode, so the other half of the conflict must be ADDED."""
        self.write("Kabuto's beverage is tea. Kabuto beverage Kabuto beverage.", D[2])
        self.write("Kabuto's beverage is gin.", D[2])
        us = self.units(top_k=1)
        self.assertEqual(self.asserted(us), {"tea", "gin"})
        self.assertTrue(any(u.added for u in us))
        self.assertTrue(self.asserted(us[-1:]))  # added slot units go LAST (reading order: best last)

    def test_every_unit_is_a_verbatim_source_sentence(self) -> None:
        self.write("Day 1. Kabuto's beverage is tea. Kabuto sat.", D[2])
        self.write("Day 2. Kabuto's beverage is gin. Kabuto stood.", D[4])
        self.write("Kabuto's beverage is rum.", D[4])
        for u in self.units() + self.units(as_of=D[3]):
            self.assertEqual(t2.tier1.split_sentences(self.sub.store.read_payload(u.memory_id, u.version_id))
                             [u.sentence_index], u.text)

    def test_now_is_the_request_time_and_as_of_is_never_read_from_the_query(self) -> None:
        self.write("Kabuto's beverage is tea.", D[2])
        self.write("Kabuto's beverage is gin.", "2027-01-01T00:00:00Z")  # after the request_time (NOW)
        self.assertEqual(self.asserted(self.units()), {"tea"})
        self.assertEqual(self.asserted(self.units("What was Kabuto's beverage on 2027-02-01?")), {"tea"})  # R7
        self.assertEqual(self.asserted(self.units(as_of="2027-02-01T00:00:00Z")), {"gin"})
        with self.assertRaises(ValueError):
            self.units(as_of="2027-02-01T00:00:00")  # naive time: no zone, refused rather than guessed

    def test_the_budget_holds_and_trims_the_weakest_first(self) -> None:
        self.write("Kabuto sat down near the beverage stand. Kabuto's beverage is tea.", D[2])
        us = self.units(budget=4)
        self.assertLessEqual(sum(t2.tier1.count_tokens(u.text) for u in us), 4)
        self.assertEqual([u.text for u in us], ["Kabuto's beverage is tea."])

    def test_a_budget_truncated_authorization_view_raises(self) -> None:
        self.write("Kabuto's beverage is tea.", D[2])
        self.write("Kabuto's snack is nut.", D[2])
        with self.assertRaises(t2.SlotViewTruncated):
            self.units(item_budget=1)

    def test_the_adapter_threads_as_of_structurally(self) -> None:
        self.write("Kabuto's beverage is tea.", D[2])
        self.write("Kabuto's beverage is gin.", D[4])
        now = self.sub.retrieve("What is Kabuto's beverage?", "companionship", 1000)
        then = self.sub.retrieve("What is Kabuto's beverage?", "companionship", 1000, as_of=D[3])
        self.assertEqual([e.text for e in now], ["Kabuto's beverage is gin."])
        self.assertEqual([e.text for e in then], ["Kabuto's beverage is tea."])
        self.assertIsNone(self.sub._as_of)


class TestGovernanceRealPath(_Case):
    """Real lifecycle ops on the real store: nothing is injected."""

    def forget(self, key: tuple[str, str]) -> None:
        r = ops.forget(self.sub.store, operation_id=f"op-f-{key[0]}", mode=ops.LifecycleMode.REMOVE_DERIVED,
                       target_ids=[key[0]], authority_ref="auth:pmb1", lifecycle_policies=self.sub._policies,
                       now=fxs.NOW)
        self.assertIsNone(r.refusal, r)
        self.sub.store.rebuild_projections()

    def test_forget_of_a_source_removes_its_fact_and_its_part_in_a_conflict(self) -> None:
        a = self.write("Kabuto's beverage is tea.", D[2])
        b = self.write("Kabuto's beverage is gin.", D[2])
        self.assertEqual(self.asserted(self.units()), {"tea", "gin"})
        self.forget(b)
        with _Spy(self.sub.store) as spy:
            us = self.units()
        self.assertEqual(self.asserted(us), {"tea"})
        self.assertFalse([u for u in us if b in u.sources])  # no unit, and no support, names the forgotten source
        self.assertEqual([u.support for u in us], [()])
        self.assertNotIn(b, spy.reads)
        self.forget(a)
        self.assertEqual(self.units(), [])  # the sole remaining source forgotten: the fact is gone

    def test_forget_of_the_newer_value_re_exposes_the_older_as_current(self) -> None:
        """Forget is not a supersede: with the later claim gone, nothing closes the earlier interval any more."""
        self.write("Kabuto's beverage is tea.", D[2])
        b = self.write("Kabuto's beverage is gin.", D[4])
        self.forget(b)
        self.assertEqual(self.asserted(self.units()), {"tea"})

    def test_a_correction_serves_only_the_new_value_and_never_reads_the_old(self) -> None:
        a = self.write("Kabuto's beverage is tea. Kabuto sat.", D[2])
        new = self.sub.supersede("/".join(a), Item("n", "kabuto", "Kabuto's beverage is gin. Kabuto sat."))  # ops.correct
        self.sub.store.rebuild_projections()
        with _Spy(self.sub.store) as spy:
            us = self.units()
            us_then = self.units(as_of=D[3])
        self.assertEqual(self.asserted(us), {"gin"})
        self.assertEqual(self.asserted(us_then), {"gin"})  # the superseded version is not history, it is gone (R8)
        self.assertEqual({u.sources for u in us + us_then} - {(tuple(new.split("/")),)}, set())
        self.assertNotIn(a, spy.reads)
        self.assertNotIn(a, spy.authorized)

    def test_above_ceiling_is_never_read_and_its_conflict_never_emitted(self) -> None:
        a = self.write("Kabuto's beverage is tea.", D[2])
        secret = self.write("Kabuto's beverage is gin.", D[2], privacy="personal")
        self.assertEqual(self.asserted(self.units()), {"tea", "gin"})  # under the personal ceiling: a conflict
        with _Spy(self.sub.store) as spy:
            us = self.units(privacy_ceiling="internal")
        self.assertEqual(self.asserted(us), {"tea"})
        self.assertEqual([(u.sources, u.support) for u in us], [((a,), ())])  # no conflict, no support naming it
        self.assertNotIn(secret, spy.reads)
        self.assertNotIn(secret, spy.authorized)
        self.assertEqual(spy.events[0][0], "eval")  # authorization of the FULL set precedes every read (L2)
        self.assertEqual(spy.events[0][1], 2)
        self.assertFalse([r for r in spy.reads if r not in spy.authorized])

    def test_the_ceiling_test_goes_red_without_authorization(self) -> None:
        """Guard proof: with evaluate_retrieval a pass-through, the above-ceiling payload IS read and its value IS
        served, so the test above can fail."""
        self.write("Kabuto's beverage is tea.", D[2])
        secret = self.write("Kabuto's beverage is gin.", D[2], privacy="personal")

        def passthrough(request, envelopes, policy):
            return mock.Mock(items=[mock.Mock(memory_id=e.memory_id, version_id=e.version_id,
                                              use_disposition=mock.Mock(value="allowed")) for e in envelopes],
                             omitted_reason_counts={})

        with mock.patch.object(t2.ev, "evaluate_retrieval", passthrough), _Spy(self.sub.store) as spy:
            us = self.units(privacy_ceiling="internal")
        self.assertIn(secret, spy.reads)
        self.assertIn("gin", self.asserted(us))

    def test_meet_discipline_refuses_a_unit_whose_support_is_unauthorized(self) -> None:
        """Unit test of the meet guard (state injected on purpose, NOT a governance path): a slot table that reports a
        claim from a version OUTSIDE the authorized view makes a conflict whose every unit depends on it, so no unit
        of that slot may be emitted."""
        a = self.write("Kabuto's beverage is tea.", D[2])
        real_table = t2.slot_table
        ghost = t2.ClaimRecord(t2.Claim("Kabuto", "beverage", "rum"), "m-ghost", "v-9", 0, "Kabuto's beverage is rum.",
                               at(D[2]), None, "allowed")

        def leaky_table(store, items, extractor):
            out = real_table(store, items, extractor)
            out.setdefault(("kabuto", "beverage"), []).append(ghost)
            return out

        with mock.patch.object(t2, "slot_table", leaky_table):
            us = self.units()
        self.assertFalse([u for u in us if ("m-ghost", "v-9") in u.sources])
        self.assertEqual(self.asserted(us), set())  # tea depends on the ghost conflict: refused too
        self.assertEqual(self.asserted(self.units()), {"tea"})
        self.assertEqual([u.sources for u in self.units()], [(a,)])


class TestBudgetNeverHalvesAConflict(_Case):
    """REBOUND 1 (review MAJOR): a budget trim must never resolve a conflict silently. tea and gin both hold at D2;
    top_k=1 so tier 1 returns only the tea episode's sentences and gin is ADDED. Definition (locked ruling): non-slot
    units go first; one slot's units are kept or dropped TOGETHER. So at every budget the slot is answered with BOTH
    values or not at all, and filler never survives while the slot is dropped."""

    Q = "What is Kabuto's beverage at the noon stall?"

    def setUp(self) -> None:
        super().setUp()
        self.write("Kabuto's beverage is tea. Kabuto drank a beverage at noon. Kabuto liked that beverage stall.", D[2])
        self.write("Kabuto's beverage is gin.", D[2], subject="zed")

    def test_both_values_or_none_at_every_budget(self) -> None:
        for budget in range(1, 25):
            us = self.units(self.Q, budget=budget, top_k=1)
            vals = self.asserted(us)
            self.assertIn(vals, ({"tea", "gin"}, set()), (budget, [u.text for u in us]))
            self.assertLessEqual(sum(t2.tier1.count_tokens(u.text) for u in us), budget)
            if not vals:  # the slot was dropped: only because the two slot sentences (8 words) cannot fit
                self.assertLess(budget, 8, budget)
                self.assertEqual(us, [], budget)  # non-slot units were trimmed first

    def test_filler_is_trimmed_before_the_slot(self) -> None:
        for budget in (8, 10, 12, 16, 18):  # 10 / 16 / 18 served only {gin} before the fix
            us = self.units(self.Q, budget=budget, top_k=1)
            self.assertEqual(self.asserted(us), {"tea", "gin"}, budget)
        self.assertEqual([u.text for u in self.units(self.Q, budget=8, top_k=1)],
                         ["Kabuto's beverage is tea.", "Kabuto's beverage is gin."])
        self.assertEqual(len(self.units(self.Q, budget=100, top_k=1)), 4)  # everything fits: nothing trimmed


class TestReviewerBiTemporal(_Case):
    """The reviewer's hand-derived tests (L4 + F2), adopted in REBOUND 1. ``write_vt`` admits a version with an
    explicit valid_to through the real ``store.append_version`` (the harness Item has no valid_to)."""

    def by_val(self, us):
        out = {}
        for u in us:
            for c in t2.DEFAULT_EXTRACTOR.claims(u.text):
                if c.slot == ("kabuto", "beverage"):
                    out[c.norm_value] = u
        return out

    def write_vt(self, text, vf, vt):
        self.sub._n += 1
        mid = f"m-{self.sub._n}"
        env = fxs.make_env(mid, "v-1", text, subjects=("kabuto",), privacy_class="internal",
                           canonical_source_ref=fxs.source_ref(object_id=f"{mid}.md"), valid_from=vf, valid_to=vt)
        r = self.sub.store.append_version(env, text, lifecycle_policy=self.sub._policy,
                                          trust_policy=self.sub._trust, now=fxs.NOW)
        self.assertIsNone(r.refusal, r)
        self.sub.store.rebuild_projections()
        return (mid, "v-1")

    def test_equal_valid_from_conflict_then_closed_by_a_later_one(self):
        a = self.write("Kabuto's beverage is tea.", D[2])
        b = self.write("Kabuto's beverage is gin.", D[2])
        self.write("Kabuto's beverage is rum.", D[4])
        now = self.by_val(self.units())
        self.assertEqual(set(now), {"rum"})
        self.assertEqual(now["rum"].support, ())
        for t in (D[2], D[3]):  # D2 exactly: inclusive
            got = self.by_val(self.units(as_of=t))
            self.assertEqual(set(got), {"tea", "gin"}, t)
            self.assertEqual(got["tea"].support, (b,))
            self.assertEqual(got["gin"].support, (a,))
        self.assertEqual(set(self.by_val(self.units(as_of=D[4]))), {"rum"})  # D4 exactly: the conflict is closed
        self.assertEqual(self.units(as_of=D[1]), [])

    def test_backfill_chain_and_as_of_before_any_claim(self):
        self.write("Kabuto sat. Kabuto's beverage is tea.", D[4])
        self.write("Kabuto's beverage is gin.", D[2])  # backfill: ingested later, valid earlier
        self.write("Kabuto's beverage is rum.", D[6])
        self.assertEqual(set(self.by_val(self.units())), {"rum"})
        self.assertEqual(set(self.by_val(self.units(as_of=D[5]))), {"tea"})
        self.assertEqual(set(self.by_val(self.units(as_of=D[3]))), {"gin"})
        early = self.units(as_of=D[1])
        self.assertEqual(self.by_val(early), {})  # no claim sentence at all before any valid_from
        self.assertEqual([u.text for u in early], ["Kabuto sat."])  # the non-claim extract is kept

    def test_backfill_into_the_same_instant_is_a_conflict(self):
        a = self.write("Kabuto's beverage is tea.", D[4])
        b = self.write("Kabuto's beverage is gin.", D[4])
        got = self.by_val(self.units())
        self.assertEqual(set(got), {"tea", "gin"})
        self.assertEqual((got["tea"].support, got["gin"].support), ((b,), (a,)))

    def test_f2_valid_to_half_open_and_no_revival(self):
        self.write("Kabuto's beverage is tea.", D[2])
        self.write_vt("Kabuto's beverage is gin.", D[4], D[6])
        self.assertEqual(set(self.by_val(self.units(as_of=D[5]))), {"gin"})
        self.assertEqual(self.by_val(self.units(as_of=D[6])), {})  # valid_to is exclusive; tea stays closed
        self.assertEqual(self.by_val(self.units()), {})
        self.assertEqual(set(self.by_val(self.units(as_of=D[3]))), {"tea"})

    def test_f2_sole_claim_closed_by_valid_to(self):
        self.write_vt("Kabuto's beverage is tea.", D[2], D[3])
        self.assertEqual(set(self.by_val(self.units(as_of=D[2]))), {"tea"})
        self.assertEqual(self.by_val(self.units(as_of=D[3])), {})

    def test_selection_dependency_is_not_in_support(self):
        """Documented limit (docstring step 4): an ADDED unit selected because a DROPPED extract touched its slot does
        not name that extract's source in its support."""
        self.write("Kabuto's beverage is tea.", D[2])
        self.write("Kabuto's beverage is gin.", D[4], subject="zed")
        got = self.by_val(self.units("tea"))  # the query names no predicate: only the tea extract touches the slot
        self.assertEqual(set(got), {"gin"})
        self.assertTrue(got["gin"].added)
        self.assertEqual(got["gin"].support, ())


if __name__ == "__main__":
    unittest.main()
