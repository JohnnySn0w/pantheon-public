"""Tests for tools/a2a/netcaps_ratelimit.py — ORG-071 D1 tiered budgets,
handshake rates + concurrency, outbound amplification bounds and D5 weighted
cost — and the admission seam it plugs into on PassportHandshake (T-2857 PR2).

FakeClock throughout: windows roll by advancing the clock, nothing sleeps, no
network. Excess is always an ABORT (never a degraded admit) whose peer-facing
error is the single coarse PEER_ABORT, with one named audit event internally.
"""

from __future__ import annotations

import unittest

from tools.a2a.netcaps import (
    ACCOUNTING_WINDOW_S,
    PEER_ABORT,
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
    NetcapsParams,
)
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_ratelimit import (
    DEFAULT_COSTS,
    SCHEME_OPS,
    UNKNOWN_SOURCE,
    Op,
    RateLimiter,
    Tier,
    anchor_of,
    stricter_costs,
)
from tools.a2a.netcaps_timeouts import TimeoutPolicy
from tools.a2a.passport_handshake import (
    AbortReason,
    AllowlistEntry,
    HandshakeState,
    InMemoryDowngradeStore,
    Outcome,
    PassportHandshake,
    Scheme,
    TxtResolution,
    compute_card_hash,
)
from tools.test_a2a_passport_handshake import CARD_URL, ENDPOINT, _card_bytes

W = ACCOUNTING_WINDOW_S
RATE = AbortReason.RATE_LIMITED.value
UNKNOWN_DID = AbortReason.UNKNOWN_DID_BUDGET_EXCEEDED.value
QUOTA = AbortReason.QUOTA_EXHAUSTED.value

# Everything roomy, so a test can tighten exactly the one bound it is about.
ROOMY = dict(
    max_concurrent_handshakes_global=1000,
    max_concurrent_handshakes_per_source=1000,
    max_concurrent_handshakes_per_counterparty=1000,
    max_conns_global=1000, max_conns_per_source=1000,
    rate_handshake_per_source=1000, rate_handshake_per_counterparty=1000,
    budget_preauth=1000, budget_authenticated_ungranted=1000,
    budget_granted=1000, budget_unknown_source=1000, budget_known_degraded=1000,
    resolution_rate_global=1000, resolution_rate_per_source_unresolved=1000,
    resolution_rate_per_anchor=1000,
    max_distinct_dids_per_source_window=1000,
    max_distinct_anchors_per_source_window=1000,
)


def roomy(**tight) -> NetcapsParams:
    return NetcapsParams(**{**ROOMY, **tight})


def ctx(phase, endpoint=ENDPOINT, source="10.0.0.1", known=True, **kw):
    return AdmissionContext(phase=phase, endpoint_id=endpoint, source=source,
                            known=known, **kw)


START, RESOLVE, PRESENT, POST_AUTH = (
    AdmissionPhase.START, AdmissionPhase.RESOLVE, AdmissionPhase.PRESENT,
    AdmissionPhase.POST_AUTH)


class _Adapter:
    def __init__(self, scheme=Scheme.MTLS, on_verify=None):
        self._scheme = scheme
        self._on_verify = on_verify

    @property
    def scheme(self) -> Scheme:
        return self._scheme

    def verify(self, endpoint_id, allowlist_entry) -> bool:
        if self._on_verify:
            self._on_verify()
        return True


def build(*, admission, clock, sink=None, known=(ENDPOINT,), fetch_ok=True,
          on_verify=None, downgrade_store=None):
    """A handshake that admits every allowlisted endpoint; any other endpoint
    resolves (so it costs an outbound lookup) and then ABORTs at the allowlist."""
    card = _card_bytes([{"type": "mutualTLS"}])
    h = compute_card_hash(card)
    return PassportHandshake(
        txt_resolver=lambda eid: TxtResolution(
            present=True, dnssec_validated=True, card_url=CARD_URL,
            pinned_card_hash=h),
        card_fetcher=lambda url: card if fetch_ok else None,
        allowlist={e: AllowlistEntry(card_hash=h) for e in known},
        adapters={Scheme.MTLS: _Adapter(on_verify=on_verify)},
        downgrade_store=downgrade_store,
        timeout_policy=TimeoutPolicy(clock=clock),
        audit_sink=sink,
        admission=admission,
    )


# ---------------------------------------------------------------------------
# D1(b) budget tiers, keyed by verification state.
# ---------------------------------------------------------------------------


class TestBudgetTiers(unittest.TestCase):
    TIER_PARAM = {
        Tier.PREAUTH: "budget_preauth",
        Tier.AUTHENTICATED_UNGRANTED: "budget_authenticated_ungranted",
        Tier.GRANTED: "budget_granted",
        Tier.KNOWN_DEGRADED: "budget_known_degraded",
        Tier.UNKNOWN_SOURCE: "budget_unknown_source",
    }
    TIER_REASON = {
        Tier.PREAUTH: RATE, Tier.AUTHENTICATED_UNGRANTED: RATE,
        Tier.KNOWN_DEGRADED: RATE, Tier.GRANTED: QUOTA,
        Tier.UNKNOWN_SOURCE: UNKNOWN_DID,
    }

    def test_each_tier_admits_exactly_its_budget_then_rejects(self):
        p = NetcapsParams()
        for tier, name in self.TIER_PARAM.items():
            with self.subTest(tier=tier):
                clock = FakeClock()
                lim = RateLimiter(p, clock=clock)
                budget = int(getattr(p, name))
                for _ in range(budget):
                    self.assertIsNone(lim.charge(tier, "k", Op.TOKEN_CHECK))
                breach = lim.charge(tier, "k", Op.TOKEN_CHECK)
                self.assertIsInstance(breach, NetcapsBreach)
                self.assertEqual(breach.reason, self.TIER_REASON[tier])
                clock.advance(W)  # the window rolls: the budget refills
                self.assertIsNone(lim.charge(tier, "k", Op.TOKEN_CHECK))

    def test_tiers_and_keys_are_isolated(self):
        p = NetcapsParams()
        lim = RateLimiter(p, clock=FakeClock())
        for _ in range(int(p.budget_unknown_source)):
            lim.charge(Tier.UNKNOWN_SOURCE, UNKNOWN_SOURCE, Op.TOKEN_CHECK)
        self.assertIsNotNone(
            lim.charge(Tier.UNKNOWN_SOURCE, UNKNOWN_SOURCE, Op.TOKEN_CHECK))
        for _ in range(int(p.budget_preauth)):
            lim.charge(Tier.PREAUTH, "a", Op.TOKEN_CHECK)
        self.assertIsNotNone(lim.charge(Tier.PREAUTH, "a", Op.TOKEN_CHECK))
        # Neither exhausted bucket touches another key or another tier.
        self.assertIsNone(lim.charge(Tier.PREAUTH, "b", Op.TOKEN_CHECK))
        self.assertIsNone(lim.charge(Tier.GRANTED, "a", Op.TOKEN_CHECK))
        self.assertIsNone(lim.charge(Tier.KNOWN_DEGRADED, "a", Op.TOKEN_CHECK))

    def test_unknown_flood_cannot_starve_a_known_counterparty(self):
        # The headline D1(b) property, end to end through the handshake: many
        # sources, each claiming a fresh unallowlisted DID, plus sourceless calls.
        clock, sink = FakeClock(), InMemoryAuditSink()
        lim = RateLimiter(NetcapsParams(), clock=clock)
        hs = build(admission=[lim], clock=clock, sink=sink)
        for i in range(500):
            r = hs.run(f"did:web:evil{i}.example", source=f"203.0.113.{i % 250}:{i}")
            self.assertFalse(r.admitted)
            self.assertFalse(hs.run(f"did:web:anon{i}.example").admitted)
        self.assertTrue(sink.of(AuditEventType.UNKNOWN_DID_BUDGET_EXCEEDED)
                        or sink.of(AuditEventType.RATE_LIMIT_REJECTED))
        kin = hs.run(ENDPOINT, source="198.51.100.7")
        self.assertTrue(kin.admitted, kin.log)

    def test_unknown_source_pool_is_one_shared_key(self):
        # source=None and every unallowlisted claim share one pool (PR1's
        # reading of budget_unknown_source, confirmed).
        lim = RateLimiter(roomy(budget_unknown_source=4, budget_preauth=4,
                                budget_authenticated_ungranted=4,
                                budget_granted=4), clock=FakeClock())
        # START costs 1 unit; four different "unknown" parties use the pool up.
        for c in (ctx(START, source=None),
                  ctx(START, endpoint="did:web:x.example", known=False),
                  ctx(START, endpoint="did:web:y.example", source="1.2.3.4",
                      known=False),
                  ctx(START, source=None, endpoint="did:web:z.example",
                      known=False)):
            self.assertIsNone(lim.check(c))
        b = lim.check(ctx(START, source="9.9.9.9", endpoint="did:web:q.example",
                          known=False))
        self.assertEqual(b.reason, UNKNOWN_DID)
        # A known claim from a real source is on its own pre-auth key.
        self.assertIsNone(lim.check(ctx(START, source="9.9.9.9")))


# ---------------------------------------------------------------------------
# D5 weighted cost accounting.
# ---------------------------------------------------------------------------


class TestWeightedCosts(unittest.TestCase):
    def test_expensive_operations_cost_more_than_a_token_check(self):
        tok = DEFAULT_COSTS[Op.TOKEN_CHECK]
        self.assertGreater(DEFAULT_COSTS[Op.VC_VERIFY], tok)
        self.assertGreater(DEFAULT_COSTS[Op.STATUS_LIST_DECODE], tok)
        self.assertGreater(DEFAULT_COSTS[Op.SIGNATURE_VERIFY], tok)
        self.assertEqual(set(DEFAULT_COSTS), set(Op))

    def test_a_charge_consumes_its_weight(self):
        p = NetcapsParams()
        lim = RateLimiter(p, clock=FakeClock())
        cost = DEFAULT_COSTS[Op.VC_VERIFY]
        n = int(p.budget_granted // cost)
        for _ in range(n):
            self.assertIsNone(lim.charge(Tier.GRANTED, "d", Op.VC_VERIFY))
        self.assertIsNotNone(lim.charge(Tier.GRANTED, "d", Op.VC_VERIFY))
        # A cheaper op may still fit in the remainder; the remainder is exact.
        rest = p.budget_granted - n * cost
        for _ in range(int(rest // DEFAULT_COSTS[Op.TOKEN_CHECK])):
            self.assertIsNone(lim.charge(Tier.GRANTED, "d", Op.TOKEN_CHECK))
        self.assertIsNotNone(lim.charge(Tier.GRANTED, "d", Op.TOKEN_CHECK))

    def test_stricter_costs_only(self):
        tighter = stricter_costs({Op.VC_VERIFY: DEFAULT_COSTS[Op.VC_VERIFY] * 2})
        self.assertEqual(tighter[Op.VC_VERIFY], DEFAULT_COSTS[Op.VC_VERIFY] * 2)
        self.assertEqual(tighter[Op.TOKEN_CHECK], DEFAULT_COSTS[Op.TOKEN_CHECK])
        with self.assertRaises(ValueError):  # looser than the uniform minimum
            stricter_costs({Op.VC_VERIFY: DEFAULT_COSTS[Op.VC_VERIFY] - 1})
        with self.assertRaises(ValueError):
            stricter_costs({Op.TOKEN_CHECK: 0})
        with self.assertRaises(ValueError):
            RateLimiter(costs={Op.SIGNATURE_VERIFY: 0.5}, clock=FakeClock())

    def test_stricter_limiter_costs_take_effect(self):
        p = roomy(budget_preauth=10)
        lim = RateLimiter(p, clock=FakeClock(),
                          costs={Op.TOKEN_CHECK: 5})
        self.assertIsNone(lim.charge(Tier.PREAUTH, "k", Op.TOKEN_CHECK))
        self.assertIsNone(lim.charge(Tier.PREAUTH, "k", Op.TOKEN_CHECK))
        self.assertIsNotNone(lim.charge(Tier.PREAUTH, "k", Op.TOKEN_CHECK))

    def test_present_phase_charges_the_schemes_verify_cost(self):
        budget = 12
        uses = {}
        for scheme in ("API_KEY", "MTLS", "SOMETHING_NEW"):
            lim = RateLimiter(roomy(budget_preauth=budget), clock=FakeClock())
            n = 0
            while lim.check(ctx(PRESENT, scheme=scheme)) is None:
                n += 1
            uses[scheme] = n
        op = SCHEME_OPS["MTLS"]
        self.assertEqual(uses["MTLS"], budget // DEFAULT_COSTS[op])
        self.assertGreater(uses["API_KEY"], uses["MTLS"])
        # A scheme the table does not name is charged as the dearest op.
        self.assertEqual(uses["SOMETHING_NEW"],
                         budget // max(DEFAULT_COSTS.values()))


# ---------------------------------------------------------------------------
# D1(a) handshake rates + concurrency, and the release contract.
# ---------------------------------------------------------------------------


class TestHandshakeRates(unittest.TestCase):
    def test_rate_per_source(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(rate_handshake_per_source=2), clock=clock)
        known = [f"did:web:k{i}.example" for i in range(3)]
        hs = build(admission=[lim], clock=clock, known=known)
        self.assertTrue(hs.run(known[0], source="s").admitted)
        self.assertTrue(hs.run(known[1], source="s").admitted)
        r = hs.run(known[2], source="s")
        self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertTrue(hs.run(known[2], source="other").admitted)
        clock.advance(W)
        self.assertTrue(hs.run(known[2], source="s").admitted)

    def test_rate_per_counterparty_is_keyed_per_source_and_claimed_did(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(rate_handshake_per_counterparty=2), clock=clock)
        hs = build(admission=[lim], clock=clock)
        self.assertTrue(hs.run(ENDPOINT, source="a").admitted)
        self.assertTrue(hs.run(ENDPOINT, source="a").admitted)
        self.assertEqual(hs.run(ENDPOINT, source="a").abort_reason,
                         AbortReason.RATE_LIMITED)
        # Another source claiming the same DID has its own pre-auth key.
        self.assertTrue(hs.run(ENDPOINT, source="b").admitted)

    def test_attackers_claiming_a_known_did_cannot_rate_lock_it_out(self):
        # Review r1 HIGH: kin DIDs are public. Failing handshakes claiming X
        # from attacker sources must not spend the real X's pre-auth bounds.
        clock, p = FakeClock(), NetcapsParams()
        lim = RateLimiter(p, clock=clock)
        card = _card_bytes([{"type": "mutualTLS"}])
        h = compute_card_hash(card)

        class Liar(_Adapter):
            def verify(self, endpoint_id, allowlist_entry):
                return False

        attacker = PassportHandshake(
            txt_resolver=lambda e: TxtResolution(True, True, CARD_URL, h),
            card_fetcher=lambda u: card,
            allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
            adapters={Scheme.MTLS: Liar()},
            timeout_policy=TimeoutPolicy(clock=clock), admission=[lim])
        # Three times the per-counterparty rate (30 >= the per-anchor rate), from
        # distinct sources: neither the per-counterparty nor the per-anchor
        # bound is spent for the real X. Only the global caps are shared.
        n = int(max(p.rate_handshake_per_counterparty * 3,
                    p.resolution_rate_per_anchor))
        for i in range(n):
            self.assertFalse(attacker.run(ENDPOINT, source=f"198.18.0.{i}").admitted)
        legit = build(admission=[lim], clock=clock)
        r = legit.run(ENDPOINT, source="192.0.2.10")
        self.assertTrue(r.admitted, r.log)

    def test_a_refused_start_consumes_nothing(self):
        # Atomic: the per-counterparty refusal must not eat the source's rate.
        clock = FakeClock()
        lim = RateLimiter(roomy(rate_handshake_per_counterparty=1,
                                rate_handshake_per_source=2), clock=clock)
        self.assertIsNone(lim.check(ctx(START, source="s")))
        lim.release(ctx(START, source="s"))
        for _ in range(5):
            self.assertIsNotNone(lim.check(ctx(START, source="s")))
        self.assertIsNone(lim.check(ctx(START, source="s",
                                        endpoint="did:web:other.example")))


class TestConcurrency(unittest.TestCase):
    def _fill(self, lim, contexts):
        for c in contexts:
            self.assertIsNone(lim.check(c))

    def test_global_cap_and_release(self):
        lim = RateLimiter(roomy(max_concurrent_handshakes_global=2,
                                max_concurrent_handshakes_per_source=2,
                                max_concurrent_handshakes_per_counterparty=2),
                          clock=FakeClock())
        a, b = ctx(START, source="a"), ctx(START, source="b")
        self._fill(lim, [a, b])
        self.assertEqual(lim.check(ctx(START, source="c")).reason, RATE)
        lim.release(a)
        self.assertIsNone(lim.check(ctx(START, source="c")))
        self.assertEqual(lim.in_flight(), 2)

    def test_per_source_cap(self):
        lim = RateLimiter(roomy(max_concurrent_handshakes_per_source=1),
                          clock=FakeClock())
        self._fill(lim, [ctx(START, source="s")])
        self.assertIsNotNone(lim.check(ctx(START, source="s",
                                           endpoint="did:web:o.example")))
        self.assertIsNone(lim.check(ctx(START, source="t",
                                        endpoint="did:web:o.example")))

    def test_per_counterparty_cap_is_per_source_and_claimed_did(self):
        lim = RateLimiter(roomy(max_concurrent_handshakes_per_counterparty=1),
                          clock=FakeClock())
        self._fill(lim, [ctx(START, source="s")])
        self.assertIsNotNone(lim.check(ctx(START, source="s")))
        self.assertIsNone(lim.check(ctx(START, source="t")))  # own key
        lim.release(ctx(START, source="s"))
        self.assertIsNone(lim.check(ctx(START, source="s")))

    def test_attacker_holding_start_slots_cannot_lock_out_a_known_did(self):
        # Review r1 HIGH: one attacker source holding its START slots for
        # claimed DID X leaves the real X free to start from its own source.
        clock, p = FakeClock(), NetcapsParams()
        lim = RateLimiter(p, clock=clock)
        held = 0
        while lim.check(ctx(START, source="198.18.0.1")) is None:
            held += 1
        # One source, one claimed DID: the tighter of the per-source and the
        # per-(source, DID) counterparty concurrency caps binds.
        self.assertEqual(held, min(p.max_concurrent_handshakes_per_source,
                                   p.max_concurrent_handshakes_per_counterparty))
        r = build(admission=[lim], clock=clock).run(ENDPOINT, source="192.0.2.10")
        self.assertTrue(r.admitted, r.log)
        self.assertEqual(lim.in_flight(), held)

    def test_handshake_releases_on_admit_abort_and_nesting(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(max_concurrent_handshakes_per_source=1),
                          clock=clock)
        inner = {}

        def nested():
            # A second handshake from the same source while the first is in
            # flight: over the per-source cap, so refused.
            inner["r"] = hs.run(ENDPOINT, source="s")

        hs = build(admission=[lim], clock=clock, on_verify=nested)
        outer = hs.run(ENDPOINT, source="s")
        self.assertTrue(outer.admitted)
        self.assertEqual(inner["r"].abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual(lim.in_flight(), 0)
        # Released after a non-gate ABORT too.
        failing = build(admission=[lim], clock=clock, fetch_ok=False)
        self.assertEqual(failing.run(ENDPOINT, source="s").abort_reason,
                         AbortReason.CARD_FETCH_FAILED)
        self.assertEqual(lim.in_flight(), 0)

    def test_every_gate_is_released_even_if_one_release_raises(self):
        clock = FakeClock()
        first, last = RateLimiter(roomy(), clock=clock), RateLimiter(roomy(), clock=clock)

        class BadRelease:
            def check(self, c):
                return None

            def release(self, c):
                raise RuntimeError("release bug")

        hs = build(admission=[first, BadRelease(), last], clock=clock)
        # T-5411: a raising release is an audited ABORT, not an exception.
        r = hs.run(ENDPOINT, source="s")
        self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual((first.in_flight(), last.in_flight()), (0, 0))

    def test_released_when_a_later_gate_refuses_or_raises(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(), clock=clock)

        class Refuse:
            def check(self, c):
                return NetcapsBreach(RATE, AuditEventType.RATE_LIMIT_REJECTED.value,
                                     "stub")

            def release(self, c):
                raise AssertionError("a gate that refused START is not released")

        class Boom:
            def check(self, c):
                if c.phase is RESOLVE:
                    raise RuntimeError("gate bug")
                return None

            def release(self, c):
                pass

        r = build(admission=[lim, Refuse()], clock=clock).run(ENDPOINT, source="s")
        self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual(lim.in_flight(), 0)
        # T-5399: a gate whose check raises is an audited ABORT (RATE_LIMITED),
        # not an exception out of run(); started gates are still released.
        r = build(admission=[lim, Boom()], clock=clock).run(ENDPOINT, source="s")
        self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual(lim.in_flight(), 0)


# ---------------------------------------------------------------------------
# D1 outbound amplification bounds (the RESOLVE phase).
# ---------------------------------------------------------------------------


class TestAmplification(unittest.TestCase):
    def _until_breach(self, lim, contexts):
        for i, c in enumerate(contexts):
            b = lim.check(c)
            if b is not None:
                return i, b
        return len(contexts), None

    def test_global_resolution_rate(self):
        lim = RateLimiter(roomy(resolution_rate_global=3,
                                resolution_rate_per_source_unresolved=3,
                                resolution_rate_per_anchor=3), clock=FakeClock())
        n, b = self._until_breach(lim, [
            ctx(RESOLVE, endpoint=f"did:web:h{i}.example", source=f"s{i}")
            for i in range(10)])
        self.assertEqual((n, b.reason), (3, RATE))

    def test_did_doc_resource_skips_source_rate_and_distinct_sets(self):
        # The TXT RESOLVE already recorded this source/DID/anchor; the DID-doc
        # fetch must not double-charge the per-source unresolved rate or the
        # distinct sets, but still charges global, per-(source, anchor), budget.
        lim = RateLimiter(roomy(resolution_rate_per_source_unresolved=1,
                                max_distinct_dids_per_source_window=1,
                                max_distinct_anchors_per_source_window=1),
                          clock=FakeClock())
        self.assertIsNone(lim.check(ctx(RESOLVE)))
        self.assertIsNone(lim.check(ctx(RESOLVE, resource="did_doc")))
        self.assertIsNone(lim.check(ctx(RESOLVE, resource="did_doc")))

    def test_did_doc_resource_still_charges_global_anchor_and_budget(self):
        for tight, reason in (
                (dict(resolution_rate_global=2, resolution_rate_per_source_unresolved=2,
                      resolution_rate_per_anchor=2), RATE),
                (dict(resolution_rate_per_anchor=2), RATE),
                (dict(budget_preauth=2), RATE)):
            with self.subTest(tight=tight):
                lim = RateLimiter(roomy(**tight), clock=FakeClock())
                self.assertIsNone(lim.check(ctx(RESOLVE)))
                self.assertIsNone(lim.check(ctx(RESOLVE, resource="did_doc")))
                b = lim.check(ctx(RESOLVE, resource="did_doc"))
                self.assertIsNotNone(b)
                self.assertEqual(b.reason, reason)

    def test_did_doc_charge_keyed_on_source_not_claimed_did_quota(self):
        lim = RateLimiter(roomy(resolution_rate_per_anchor=1), clock=FakeClock())
        self.assertIsNone(lim.check(ctx(RESOLVE, resource="did_doc", source="s")))
        # Another source has its own (source, anchor) key (D4).
        self.assertIsNone(lim.check(ctx(RESOLVE, resource="did_doc", source="t")))

    def test_unknown_resource_value_takes_the_full_strict_charge(self):
        lim = RateLimiter(roomy(resolution_rate_per_source_unresolved=1),
                          clock=FakeClock())
        self.assertIsNone(lim.check(ctx(RESOLVE)))
        b = lim.check(ctx(RESOLVE, resource="mystery"))
        self.assertIsNotNone(b)
        self.assertEqual(b.reason, UNKNOWN_DID)

    def test_per_source_unresolved_did_rate(self):
        lim = RateLimiter(roomy(resolution_rate_per_source_unresolved=2),
                          clock=FakeClock())
        n, b = self._until_breach(lim, [ctx(RESOLVE, source="s") for _ in range(5)])
        self.assertEqual((n, b.reason), (2, UNKNOWN_DID))
        self.assertIsNone(lim.check(ctx(RESOLVE, source="t")))

    def test_per_anchor_rate_known_claims_per_source_and_anchor(self):
        lim = RateLimiter(roomy(resolution_rate_per_anchor=2), clock=FakeClock())
        same_host = [ctx(RESOLVE, endpoint=f"did:web:a.example:u{i}",
                         source="s") for i in range(5)]
        n, b = self._until_breach(lim, same_host)
        self.assertEqual((n, b.reason), (2, RATE))
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint="did:web:b.example",
                                        source="s")))
        # Another source resolving the same anchor has its own key.
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint="did:web:a.example:u0",
                                        source="t")))

    def test_unknown_claim_flood_is_still_bounded_per_anchor(self):
        lim = RateLimiter(roomy(resolution_rate_per_anchor=2), clock=FakeClock())
        flood = [ctx(RESOLVE, endpoint=f"did:web:a.example:u{i}",
                     source=f"203.0.113.{i}", known=False) for i in range(50)]
        n, b = self._until_breach(lim, flood)
        self.assertEqual((n, b.reason), (2, RATE))
        self.assertIsNotNone(lim.check(ctx(RESOLVE, endpoint="did:web:a.example:x",
                                           source=None, known=True)))
        # A known claim from a real source keeps its own per-anchor key.
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint="did:web:a.example",
                                        source="192.0.2.10")))

    def test_distinct_dids_per_source(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(max_distinct_dids_per_source_window=2), clock=clock)
        dids = [f"did:web:a.example:u{i}" for i in range(3)]
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=dids[0])))
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=dids[1])))
        self.assertEqual(lim.check(ctx(RESOLVE, endpoint=dids[2])).reason,
                         UNKNOWN_DID)
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=dids[0])))  # seen
        clock.advance(W)
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=dids[2])))

    def test_distinct_anchors_per_source(self):
        lim = RateLimiter(roomy(max_distinct_anchors_per_source_window=2),
                          clock=FakeClock())
        hosts = [f"did:web:h{i}.example" for i in range(3)]
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=hosts[0])))
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=hosts[1])))
        self.assertEqual(lim.check(ctx(RESOLVE, endpoint=hosts[2])).reason, RATE)
        self.assertIsNone(lim.check(ctx(RESOLVE, endpoint=hosts[2], source="t")))

    def test_anchor_of(self):
        self.assertEqual(anchor_of("did:web:your-domain.example"), "your-domain.example")
        self.assertEqual(anchor_of("did:web:A.Example:u:1"), "a.example")
        self.assertEqual(anchor_of("did:web:a.example%3A8443"), "a.example")
        self.assertEqual(anchor_of("did:key:z6Mk"), "did:key:z6Mk")

    def test_resolve_refusal_happens_before_the_outbound_lookup(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(resolution_rate_global=1,
                                resolution_rate_per_source_unresolved=1,
                                resolution_rate_per_anchor=1), clock=clock)
        lookups = []
        card = _card_bytes([{"type": "mutualTLS"}])
        h = compute_card_hash(card)

        def resolver(eid):
            lookups.append(eid)
            return TxtResolution(True, True, CARD_URL, h)

        hs = PassportHandshake(
            txt_resolver=resolver, card_fetcher=lambda u: card,
            allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
            adapters={Scheme.MTLS: _Adapter()},
            timeout_policy=TimeoutPolicy(clock=clock), admission=[lim])
        self.assertTrue(hs.run(ENDPOINT, source="a").admitted)
        self.assertFalse(hs.run(ENDPOINT, source="b").admitted)
        self.assertEqual(lookups, [ENDPOINT])  # no second outbound lookup


# ---------------------------------------------------------------------------
# Bounded state: many distinct sources / DIDs cannot grow the limiter.
# ---------------------------------------------------------------------------


class TestBoundedState(unittest.TestCase):
    def test_unknown_flood_leaves_state_constant(self):
        clock = FakeClock()
        lim = RateLimiter(NetcapsParams(), clock=clock)
        hs = build(admission=[lim], clock=clock)
        for i in range(1000):
            hs.run(f"did:web:e{i}.example", source=f"2001:db8::{i:x}")
        self.assertLessEqual(lim.tracked_keys(), 16)

    def test_overflow_is_fail_closed_then_evicts(self):
        clock = FakeClock()
        lim = RateLimiter(NetcapsParams(), clock=clock, max_tracked_keys=3)
        for k in ("k0", "k1", "k2"):
            self.assertIsNone(lim.charge(Tier.PREAUTH, k, Op.TOKEN_CHECK))
        b = lim.charge(Tier.PREAUTH, "k3", Op.TOKEN_CHECK)
        self.assertEqual(b.reason, RATE)
        self.assertIn("full", b.detail)
        self.assertIsNone(lim.charge(Tier.PREAUTH, "k0", Op.TOKEN_CHECK))
        clock.advance(W)  # expired windows are evicted to make room
        self.assertIsNone(lim.charge(Tier.PREAUTH, "k3", Op.TOKEN_CHECK))
        self.assertLessEqual(lim.tracked_keys(), 3)

    def test_known_claim_source_flood_is_capped_and_fail_closed(self):
        clock = FakeClock()
        lim = RateLimiter(roomy(), clock=clock, max_tracked_keys=8)
        refused = 0
        for i in range(500):
            if lim.check(ctx(START, source=f"s{i}")) is not None:
                refused += 1
            else:
                lim.release(ctx(START, source=f"s{i}"))
        self.assertGreater(refused, 0)
        self.assertLessEqual(lim.tracked_keys(), 8 * lim.TABLES)


# ---------------------------------------------------------------------------
# D6: audit events, coarse peer error, the PR4 seam, no-gate parity.
# ---------------------------------------------------------------------------


class TestAuditAndSeam(unittest.TestCase):
    def test_breach_emits_one_mapped_event_and_coarse_peer_error(self):
        clock, sink = FakeClock(), InMemoryAuditSink()
        lim = RateLimiter(roomy(rate_handshake_per_counterparty=1), clock=clock)
        hs = build(admission=[lim], clock=clock, sink=sink)
        self.assertTrue(hs.run(ENDPOINT, source="b").admitted)
        self.assertEqual(sink.events, [])
        r = hs.run(ENDPOINT, source="b")
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertEqual(r.state, HandshakeState.RESOLVE_ENDPOINT)
        self.assertEqual(r.peer_error, PEER_ABORT)
        self.assertEqual(len(sink.events), 1)
        ev = sink.events[0]
        self.assertIs(ev.type, AuditEventType.RATE_LIMIT_REJECTED)
        self.assertEqual((ev.endpoint_id, ev.source, ev.reason),
                         (ENDPOINT, "b", RATE))

    def test_unknown_budget_breach_event(self):
        clock, sink = FakeClock(), InMemoryAuditSink()
        lim = RateLimiter(roomy(budget_unknown_source=1, budget_preauth=1,
                                budget_authenticated_ungranted=1,
                                budget_granted=1), clock=clock)
        hs = build(admission=[lim], clock=clock, sink=sink)
        r = hs.run("did:web:stranger.example")
        self.assertEqual(r.abort_reason, AbortReason.UNKNOWN_DID_BUDGET_EXCEEDED)
        self.assertEqual(r.peer_error, PEER_ABORT)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.UNKNOWN_DID_BUDGET_EXCEEDED])

    def test_post_auth_seam_for_pr4(self):
        clock, sink = FakeClock(), InMemoryAuditSink()
        store = InMemoryDowngradeStore()
        seen = []

        class Quota:
            def check(self, c):
                seen.append((c.phase, c.scheme, c.principal))
                if c.phase is POST_AUTH:
                    return NetcapsBreach(QUOTA, AuditEventType.QUOTA_EXHAUSTED.value,
                                         "grant quota spent")
                return None

            def release(self, c):
                seen.append(("release", c.phase))

        lim = RateLimiter(roomy(), clock=clock)
        hs = build(admission=[lim, Quota()], clock=clock, sink=sink,
                   downgrade_store=store)
        r = hs.run(ENDPOINT, source="s")
        self.assertEqual(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual(r.state, HandshakeState.ADMIT_UNTRUSTED)
        self.assertEqual(r.peer_error, PEER_ABORT)
        self.assertEqual(seen, [(START, None, None), (RESOLVE, None, None),
                                (PRESENT, "MTLS", None),
                                (POST_AUTH, "MTLS", ENDPOINT),
                                ("release", START)])
        self.assertEqual(store.seen(ENDPOINT), set())  # an ABORT records nothing
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.QUOTA_EXHAUSTED])
        self.assertIsNone(lim.check(ctx(POST_AUTH, principal=ENDPOINT)))
        self.assertEqual(lim.in_flight(), 0)

    def test_unmapped_breach_fails_closed_with_one_generic_event(self):
        clock = FakeClock()

        def gate(reason, event):
            class G:
                def check(self, c):
                    return NetcapsBreach(reason, event, "x")

                def release(self, c):
                    pass
            return G()

        for reason, event in (
                (AbortReason.NO_TXT.value, "rate_limit_rejected"),
                ("made_up", "made_up"),
                (RATE, AuditEventType.QUOTA_EXHAUSTED.value)):
            with self.subTest(reason=reason, event=event):
                sink = InMemoryAuditSink()
                r = build(admission=[gate(reason, event)], clock=clock,
                          sink=sink).run(ENDPOINT)
                self.assertFalse(r.admitted)
                self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
                self.assertEqual(r.peer_error, PEER_ABORT)
                self.assertEqual([e.type for e in sink.events],
                                 [AuditEventType.RATE_LIMIT_REJECTED])

    def test_breach_raised_inside_adapter_verify(self):
        # Review r1 LOW: the Bearer adapter resolves the DID doc inside
        # verify(), through the PR3 cache, which raises NetcapsBreach.
        clock, sink = FakeClock(), InMemoryAuditSink()
        lim = RateLimiter(roomy(), clock=clock)
        breach = NetcapsBreach(AbortReason.STALE_DENIED.value,
                               AuditEventType.STALE_DENIED.value, "expired doc")

        def raise_breach():
            raise breach

        r = build(admission=[lim], clock=clock, sink=sink,
                  on_verify=raise_breach).run(ENDPOINT, source="s")
        self.assertEqual(r.abort_reason, AbortReason.STALE_DENIED)
        self.assertEqual(r.state, HandshakeState.PRESENT_IDENTITY)
        self.assertEqual(r.peer_error, PEER_ABORT)
        self.assertEqual([(e.type, e.source) for e in sink.events],
                         [(AuditEventType.STALE_DENIED, "s")])
        self.assertEqual(lim.in_flight(), 0)

    def test_breach_raised_by_a_wrapped_resolver_or_fetcher(self):
        # PR3 (cache) / PR5 (breaker) wrap TxtResolver / CardFetcher and RAISE
        # a NetcapsBreach; the handshake is the single emitter of its event.
        card = _card_bytes([{"type": "mutualTLS"}])
        h = compute_card_hash(card)
        circuit = NetcapsBreach(AbortReason.CIRCUIT_OPEN.value,
                                AuditEventType.CIRCUIT_BREAKER_OPEN.value, "open")
        negative = NetcapsBreach(AbortReason.NEGATIVE_CACHE_HIT.value,
                                 AuditEventType.NEGATIVE_CACHE_HIT.value, "404")

        def raiser(exc):
            def f(_arg):
                raise exc
            return f

        ok_txt = lambda e: TxtResolution(True, True, CARD_URL, h)  # noqa: E731
        for resolver, fetcher, reason, event in (
                (raiser(circuit), lambda u: card, AbortReason.CIRCUIT_OPEN,
                 AuditEventType.CIRCUIT_BREAKER_OPEN),
                (ok_txt, raiser(negative), AbortReason.NEGATIVE_CACHE_HIT,
                 AuditEventType.NEGATIVE_CACHE_HIT)):
            with self.subTest(reason=reason):
                clock, sink = FakeClock(), InMemoryAuditSink()
                lim = RateLimiter(roomy(), clock=clock)
                hs = PassportHandshake(
                    txt_resolver=resolver, card_fetcher=fetcher,
                    allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
                    adapters={Scheme.MTLS: _Adapter()},
                    timeout_policy=TimeoutPolicy(clock=clock), audit_sink=sink,
                    admission=[lim])
                r = hs.run(ENDPOINT, source="s")
                self.assertEqual(r.abort_reason, reason)
                self.assertEqual(r.peer_error, PEER_ABORT)
                self.assertEqual([(e.type, e.source) for e in sink.events],
                                 [(event, "s")])
                self.assertEqual(lim.in_flight(), 0)
                # Without any gate the breach is still caught and mapped.
                bare = PassportHandshake(
                    txt_resolver=resolver, card_fetcher=fetcher,
                    allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
                    adapters={Scheme.MTLS: _Adapter()},
                    timeout_policy=TimeoutPolicy(clock=clock), audit_sink=sink)
                self.assertEqual(bare.run(ENDPOINT).abort_reason, reason)

    def test_no_gates_behaves_as_before(self):
        clock = FakeClock()
        for admission in ((), None):
            kwargs = {} if admission is None else {"admission": admission}
            card = _card_bytes([{"type": "mutualTLS"}])
            h = compute_card_hash(card)
            hs = PassportHandshake(
                txt_resolver=lambda e: TxtResolution(True, True, CARD_URL, h),
                card_fetcher=lambda u: card,
                allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
                adapters={Scheme.MTLS: _Adapter()},
                timeout_policy=TimeoutPolicy(clock=clock), **kwargs)
            for _ in range(100):  # far past every default bound
                self.assertTrue(hs.run(ENDPOINT).admitted)


if __name__ == "__main__":
    unittest.main()
