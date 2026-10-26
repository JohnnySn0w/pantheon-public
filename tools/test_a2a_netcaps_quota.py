"""Tests for tools/a2a/netcaps_quota.py — the ORG-071 D4 per-counterparty quota
on the post-auth key (the verified DID), plugged into PassportHandshake at
POST_AUTH (T-2857 PR4).

FakeClock throughout: windows roll by advancing the clock, nothing sleeps, no
network. The D4 invariants proved here:
  (a) pre-auth load under a claimed DID never touches that DID's quota;
  (b) a failed handshake (ABORT before POST_AUTH) consumes nothing from it;
  (c) exhaustion is a throttle: one audited refusal per refused handshake, the
      next window admits again, grant and allowlist state untouched;
  (d) quota keys are bounded, fail-closed on overflow (the limiter's rule).
"""

from __future__ import annotations

import unittest

from tools.a2a.netcaps import (
    ACCOUNTING_WINDOW_S,
    PEER_ABORT,
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
)
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_quota import GrantLookup, QuotaGate, StaticGrants
from tools.a2a.netcaps_ratelimit import RateLimiter
from tools.a2a.netcaps_timeouts import TimeoutPolicy
from tools.a2a.passport_handshake import (
    AbortReason,
    AllowlistEntry,
    HandshakeState,
    Outcome,
    PassportHandshake,
    Scheme,
    TxtResolution,
    compute_card_hash,
)
from tools.test_a2a_netcaps_ratelimit import roomy
from tools.test_a2a_passport_handshake import CARD_URL, _card_bytes

W = ACCOUNTING_WINDOW_S
QUOTA = AbortReason.QUOTA_EXHAUSTED.value
VICTIM = "did:web:victim.example"
OTHER = "did:web:other.example"
THIRD = "did:web:third.example"
VICTIM_SRC = "10.0.0.1"
ATTACKER_SRC = "6.6.6.6"
DIDS = (VICTIM, OTHER, THIRD)


class _Adapter:
    """mTLS adapter whose verify() passes only while `ok` is True: an attacker
    claiming a DID it holds no key for is ok=False."""

    def __init__(self):
        self.ok = True

    @property
    def scheme(self) -> Scheme:
        return Scheme.MTLS

    def verify(self, endpoint_id, allowlist_entry) -> bool:
        return self.ok


class _Rig:
    def __init__(self, quotas, *, params=None, max_keys=1024, grants=None):
        self.clock = FakeClock()
        self.sink = InMemoryAuditSink()
        self.limiter = RateLimiter(params or roomy(), clock=self.clock,
                                   max_tracked_keys=max_keys)
        self.grants = grants if grants is not None else StaticGrants(quotas)
        self.quota = QuotaGate(self.limiter, self.grants)
        self.adapter = _Adapter()
        self.fetch_ok = True
        card = _card_bytes([{"type": "mutualTLS"}])
        h = compute_card_hash(card)
        self.allowlist = {d: AllowlistEntry(card_hash=h) for d in DIDS}
        self.handshake = PassportHandshake(
            txt_resolver=lambda eid: TxtResolution(
                present=True, dnssec_validated=True, card_url=CARD_URL,
                pinned_card_hash=h),
            card_fetcher=lambda url: card if self.fetch_ok else None,
            allowlist=self.allowlist,
            adapters={Scheme.MTLS: self.adapter},
            timeout_policy=TimeoutPolicy(clock=self.clock),
            audit_sink=self.sink,
            admission=[self.limiter, self.quota],
        )

    def run(self, did=VICTIM, source=VICTIM_SRC):
        return self.handshake.run(did, source=source)

    def admits(self, did=VICTIM, source=VICTIM_SRC, limit=50) -> int:
        """Admitted handshakes for `did` until the first refusal."""
        n = 0
        while n < limit and self.run(did, source).outcome is Outcome.ADMITTED:
            n += 1
        return n

    def quota_events(self):
        return [e for e in self.sink.events
                if e.type is AuditEventType.QUOTA_EXHAUSTED]


def post_auth(principal=VICTIM, known=True):
    return AdmissionContext(phase=AdmissionPhase.POST_AUTH, endpoint_id=VICTIM,
                            source=VICTIM_SRC, known=known, principal=principal)


# ---------------------------------------------------------------------------
# Quota source: the injected grant lookup.
# ---------------------------------------------------------------------------


class GrantLookupTests(unittest.TestCase):
    def test_static_grants_is_a_grant_lookup(self):
        self.assertIsInstance(StaticGrants({}), GrantLookup)

    def test_granted_uses_the_grant_quota(self):
        rig = _Rig({VICTIM: 3})
        self.assertEqual(rig.admits(), 3)

    def test_granted_default_is_budget_granted(self):
        params = roomy(budget_granted=1000)
        grants = StaticGrants.granted([VICTIM], params)
        self.assertEqual(grants.grant_quota(VICTIM), 1000)
        self.assertIsNone(grants.grant_quota(OTHER))

    def test_ungranted_gets_budget_authenticated_ungranted(self):
        params = roomy(budget_preauth=1, budget_authenticated_ungranted=2)
        rig = _Rig({}, params=params)
        # (NetcapsParams orders preauth <= ungranted, so a tight ungranted tier
        # forces a tight preauth one; the gate is driven directly here.)
        for _ in range(2):
            self.assertIsNone(rig.quota.check(post_auth()))
        breach = rig.quota.check(post_auth())
        self.assertEqual((breach.reason, breach.event), (QUOTA, QUOTA))

    def test_grant_on_an_unallowlisted_did_is_not_honoured(self):
        params = roomy(budget_preauth=1, budget_authenticated_ungranted=1)
        rig = _Rig({VICTIM: 5}, params=params)
        self.assertIsNone(rig.quota.check(post_auth(known=False)))
        self.assertIsNotNone(rig.quota.check(post_auth(known=False)))

    def test_invalid_grant_quota_fails_closed(self):
        for bad in (-1, float("nan"), float("inf"), True, "10"):
            with self.subTest(bad=bad):
                rig = _Rig({VICTIM: bad})
                breach = rig.quota.check(post_auth())
                self.assertEqual(breach.reason, QUOTA)
                self.assertIn("invalid grant quota", breach.detail)

    def test_zero_quota_admits_nothing(self):
        rig = _Rig({VICTIM: 0})
        self.assertEqual(rig.admits(), 0)

    def test_missing_principal_fails_closed(self):
        rig = _Rig({VICTIM: 3})
        self.assertEqual(rig.quota.check(post_auth(principal=None)).reason, QUOTA)

    def test_gate_ignores_every_pre_auth_phase(self):
        rig = _Rig({VICTIM: 1})
        for phase in (AdmissionPhase.START, AdmissionPhase.RESOLVE,
                      AdmissionPhase.PRESENT):
            ctx = AdmissionContext(phase=phase, endpoint_id=VICTIM,
                                   source=ATTACKER_SRC, known=True,
                                   scheme="MTLS", principal=VICTIM)
            for _ in range(20):
                self.assertIsNone(rig.quota.check(ctx))
        self.assertIsNone(rig.quota.check(post_auth()))  # quota 1 still full

    def test_release_is_a_noop(self):
        rig = _Rig({VICTIM: 1})
        rig.quota.release(post_auth())
        self.assertEqual(rig.admits(), 1)


# ---------------------------------------------------------------------------
# D4 (a) + (b): pre-auth and failed handshakes never touch the victim's quota.
# ---------------------------------------------------------------------------


class PreAuthIsolationTests(unittest.TestCase):
    def test_a_claimed_did_never_consumes_the_victims_quota(self):
        rig = _Rig({VICTIM: 3})
        rig.adapter.ok = False  # the attacker holds no key for VICTIM
        for _ in range(40):
            res = rig.run(VICTIM, ATTACKER_SRC)
            self.assertIs(res.outcome, Outcome.ABORTED)
            self.assertEqual(res.abort_reason, AbortReason.PRESENTATION_FAILED)
        rig.adapter.ok = True
        self.assertEqual(rig.admits(VICTIM, VICTIM_SRC, limit=3), 3)
        self.assertEqual(rig.quota_events(), [])

    def test_claims_from_many_sources_leave_the_quota_full(self):
        rig = _Rig({VICTIM: 3})
        rig.adapter.ok = False
        for i in range(200):
            res = rig.run(VICTIM, f"6.6.{i // 256}.{i % 256}")
            self.assertEqual(res.abort_reason, AbortReason.PRESENTATION_FAILED)
        rig.adapter.ok = True
        # Exactly the full quota is still there: 3 admits, the 4th refused.
        self.assertEqual(rig.admits(VICTIM, VICTIM_SRC), 3)
        self.assertEqual(len(rig.quota_events()), 1)
        rig.clock.advance(W)
        self.assertIs(rig.run(VICTIM, VICTIM_SRC).outcome, Outcome.ADMITTED)

    def test_attacker_exhausting_its_preauth_budget_leaves_the_quota_full(self):
        # 4 pre-auth units per handshake (start 1 + resolve 1 + mTLS verify 2).
        rig = _Rig({VICTIM: 3}, params=roomy(budget_preauth=8))
        rig.adapter.ok = False
        reasons = [rig.run(VICTIM, ATTACKER_SRC).abort_reason for _ in range(30)]
        self.assertEqual(reasons[:2], [AbortReason.PRESENTATION_FAILED] * 2)
        self.assertEqual(set(reasons[2:]), {AbortReason.RATE_LIMITED})
        rig.adapter.ok = True
        # The attacker's refusals were charged to (ATTACKER_SRC, VICTIM) pre-auth,
        # never to VICTIM's post-auth quota; the victim's own source is clean.
        self.assertEqual(rig.admits(VICTIM, VICTIM_SRC, limit=2), 2)
        self.assertEqual(rig.quota_events(), [])

    def test_a_failed_handshake_consumes_nothing(self):
        rig = _Rig({VICTIM: 2})
        rig.fetch_ok = False                       # ABORT in step 1
        for _ in range(10):
            self.assertEqual(rig.run().abort_reason,
                             AbortReason.CARD_FETCH_FAILED)
        rig.fetch_ok = True
        rig.adapter.ok = False                     # ABORT in step 3
        for _ in range(10):
            self.assertEqual(rig.run().abort_reason,
                             AbortReason.PRESENTATION_FAILED)
        rig.adapter.ok = True
        self.assertEqual(rig.admits(), 2)

    def test_one_charge_per_verified_handshake(self):
        # quota N admits exactly N: POST_AUTH is the only charge, 1 unit each.
        for n in (1, 2, 5):
            with self.subTest(n=n):
                self.assertEqual(_Rig({VICTIM: n}).admits(), n)

    def test_quotas_are_per_verified_did(self):
        rig = _Rig({VICTIM: 1, OTHER: 2})
        self.assertEqual(rig.admits(VICTIM), 1)
        self.assertEqual(rig.admits(OTHER), 2)


# ---------------------------------------------------------------------------
# D4 (c): exhaustion is a throttle + one audited event per refusal.
# ---------------------------------------------------------------------------


class ExhaustionTests(unittest.TestCase):
    def test_exhaustion_aborts_with_quota_exhausted_and_one_event(self):
        rig = _Rig({VICTIM: 2})
        self.assertEqual(rig.admits(limit=2), 2)
        res = rig.run()
        self.assertIs(res.outcome, Outcome.ABORTED)
        self.assertEqual(res.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual(res.state, HandshakeState.ADMIT_UNTRUSTED)
        self.assertEqual(res.peer_error, PEER_ABORT)
        events = rig.quota_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].endpoint_id, VICTIM)
        self.assertEqual(events[0].source, VICTIM_SRC)
        self.assertEqual(len(rig.sink.events), 1)  # no other emission

    def test_each_refusal_audits_once(self):
        rig = _Rig({VICTIM: 1})
        rig.admits(limit=1)
        for i in range(1, 4):
            self.assertEqual(rig.run().abort_reason, AbortReason.QUOTA_EXHAUSTED)
            self.assertEqual(len(rig.quota_events()), i)

    def test_next_window_admits_again(self):
        rig = _Rig({VICTIM: 2})
        self.assertEqual(rig.admits(), 2)
        rig.clock.advance(W - 1)
        self.assertEqual(rig.run().abort_reason, AbortReason.QUOTA_EXHAUSTED)
        rig.clock.advance(1)
        self.assertEqual(rig.admits(), 2)

    def test_throttle_touches_no_grant_or_allowlist_state(self):
        rig = _Rig({VICTIM: 1})
        allowlist = dict(rig.allowlist)
        rig.admits()
        for _ in range(3):
            rig.run()
        self.assertEqual(rig.allowlist, allowlist)
        self.assertEqual(rig.grants.grant_quota(VICTIM), 1)
        self.assertEqual(rig.limiter.in_flight(), 0)

    def test_exhausting_one_did_does_not_throttle_another(self):
        rig = _Rig({VICTIM: 1, OTHER: 1})
        rig.admits(VICTIM)
        self.assertIs(rig.run(OTHER).outcome, Outcome.ADMITTED)


# ---------------------------------------------------------------------------
# D4 (d): bounded keys, fail-closed overflow.
# ---------------------------------------------------------------------------


class BoundedKeysTests(unittest.TestCase):
    def test_full_quota_table_refuses_a_new_did(self):
        rig = _Rig({d: 5 for d in DIDS}, max_keys=2)
        self.assertIsNone(rig.quota.check(post_auth(VICTIM)))
        self.assertIsNone(rig.quota.check(post_auth(OTHER)))
        breach = rig.quota.check(post_auth(THIRD))
        self.assertEqual(breach.reason, QUOTA)
        self.assertIn("table full", breach.detail)
        self.assertIsNone(rig.quota.check(post_auth(VICTIM)))  # tracked: fine
        rig.clock.advance(W)  # expired windows are evicted on demand
        self.assertIsNone(rig.quota.check(post_auth(THIRD)))

    def test_tracked_keys_bounded_by_verified_parties(self):
        rig = _Rig({d: 5 for d in DIDS})
        for _ in range(3):
            for d in DIDS:
                rig.quota.check(post_auth(d))
        self.assertEqual(rig.limiter.tracked_keys(), len(DIDS))


if __name__ == "__main__":
    unittest.main()
