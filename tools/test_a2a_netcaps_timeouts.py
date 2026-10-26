"""Tests for tools/a2a/netcaps_timeouts.py — the ORG-071 D3 timeout policy — and
its wiring into PassportHandshake (T-2857 PR1).

Every timeout is driven by a FakeClock that the injected stub dependencies
advance, so nothing sleeps. Each named step timeout and the total handshake
deadline must ABORT fail-closed, log an internal reason, and emit
`resolver_timeout`; a result that arrives late is discarded, never used.
"""

from __future__ import annotations

import unittest

from tools.a2a.netcaps import (
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
    NetcapsParams,
)
from tools.a2a.netcaps_timeouts import StepTimeout, TimeoutPolicy, TimeoutStep
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

P = NetcapsParams()


class _Adapter:
    def __init__(self, on_verify=None):
        self._on_verify = on_verify

    @property
    def scheme(self) -> Scheme:
        return Scheme.MTLS

    def verify(self, endpoint_id: str, allowlist_entry: AllowlistEntry) -> bool:
        if self._on_verify:
            self._on_verify()
        return True


def build_timed(*, clock, dns=None, fetch=None, verify=None, params=P,
                sink=None, downgrade_store=None, present=True):
    """A happy-path handshake whose three network seams run `dns` / `fetch` /
    `verify` side-effects (typically clock.advance) before answering."""
    card = _card_bytes([{"type": "mutualTLS"}])
    h = compute_card_hash(card)

    def resolver(endpoint_id):
        if dns:
            dns()
        return TxtResolution(present=present, dnssec_validated=True,
                             card_url=CARD_URL, pinned_card_hash=h)

    def fetcher(card_url):
        if fetch:
            fetch()
        return card

    return PassportHandshake(
        txt_resolver=resolver,
        card_fetcher=fetcher,
        allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
        adapters={Scheme.MTLS: _Adapter(verify)},
        downgrade_store=downgrade_store,
        timeout_policy=TimeoutPolicy(params, clock=clock),
        audit_sink=sink,
    )


def _advance(clock, s):
    return lambda: clock.advance(s)


class TestDeadlineUnit(unittest.TestCase):
    """HandshakeDeadline.call against each named step, in isolation."""

    def test_within_budget_returns_value(self):
        c = FakeClock()
        dl = TimeoutPolicy(P, clock=c).start()
        self.assertEqual(dl.call((TimeoutStep.DNS,), lambda: "ok"), "ok")

    def test_each_named_step_times_out_past_its_own_budget(self):
        for step, budget in (
            (TimeoutStep.DNS, P.timeout_dns),
            (TimeoutStep.CONNECT, P.timeout_connect),
            (TimeoutStep.TLS, P.timeout_tls),
            (TimeoutStep.FETCH_READ, P.timeout_fetch_read),
            (TimeoutStep.ROUNDTRIP, P.timeout_handshake_roundtrip),
        ):
            with self.subTest(step=step):
                c = FakeClock()
                dl = TimeoutPolicy(P, clock=c).start()
                # Exactly at budget is fine; just past it is a timeout.
                dl.call((step,), lambda: c.advance(budget))
                c2 = FakeClock()
                dl2 = TimeoutPolicy(P, clock=c2).start()
                with self.assertRaises(StepTimeout) as cm:
                    dl2.call((step,), lambda: c2.advance(budget + 0.001))
                self.assertIs(cm.exception.step, step)
                self.assertFalse(cm.exception.total)

    def test_composite_step_budget_is_the_sum(self):
        c = FakeClock()
        dl = TimeoutPolicy(P, clock=c).start()
        steps = (TimeoutStep.CONNECT, TimeoutStep.TLS, TimeoutStep.FETCH_READ)
        self.assertEqual(dl.budget(steps), P.timeout_connect + P.timeout_tls
                         + P.timeout_fetch_read)

    def test_budget_capped_by_remaining_total(self):
        c = FakeClock()
        dl = TimeoutPolicy(P, clock=c).start()
        c.advance(P.timeout_total_handshake - 0.5)
        self.assertEqual(dl.budget((TimeoutStep.ROUNDTRIP,)), 0.5)
        with self.assertRaises(StepTimeout) as cm:
            dl.call((TimeoutStep.ROUNDTRIP,), lambda: c.advance(1))
        self.assertTrue(cm.exception.total)

    def test_exhausted_deadline_refuses_before_calling(self):
        c = FakeClock()
        dl = TimeoutPolicy(P, clock=c).start()
        c.advance(P.timeout_total_handshake)
        called = []
        with self.assertRaises(StepTimeout) as cm:
            dl.call((TimeoutStep.DNS,), lambda: called.append(1))
        self.assertTrue(cm.exception.total)
        self.assertEqual(called, [])

    def test_dependency_timeout_error_becomes_step_timeout(self):
        dl = TimeoutPolicy(P, clock=FakeClock()).start()

        def boom():
            raise TimeoutError("socket timed out")

        with self.assertRaises(StepTimeout) as cm:
            dl.call((TimeoutStep.DNS,), boom)
        self.assertIs(cm.exception.step, TimeoutStep.DNS)

    def test_dependency_can_name_the_sub_step(self):
        # A fetcher knows whether connect, TLS or read blew; it raises that.
        dl = TimeoutPolicy(P, clock=FakeClock()).start()

        def tls_blew():
            raise StepTimeout(TimeoutStep.TLS)

        with self.assertRaises(StepTimeout) as cm:
            dl.call((TimeoutStep.CONNECT, TimeoutStep.TLS,
                     TimeoutStep.FETCH_READ), tls_blew)
        self.assertIs(cm.exception.step, TimeoutStep.TLS)


class TestHandshakeTimeouts(unittest.TestCase):
    def _assert_timeout_abort(self, r, sink, state, reason, step_value):
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertIsNone(r.scheme)
        self.assertIsNone(r.principal)
        self.assertEqual(r.state, state)
        self.assertEqual(r.abort_reason, reason)
        self.assertTrue(any(reason.value in line for line in r.log))
        [ev] = sink.events
        self.assertIs(ev.type, AuditEventType.RESOLVER_TIMEOUT)
        self.assertEqual(ev.endpoint_id, ENDPOINT)
        self.assertEqual(ev.reason, reason.value)
        self.assertIn(step_value, ev.detail)

    def test_fast_handshake_admits_and_emits_nothing(self):
        c, sink = FakeClock(), InMemoryAuditSink()
        r = build_timed(clock=c, sink=sink).run(ENDPOINT)
        self.assertTrue(r.admitted)
        self.assertEqual(sink.events, [])

    def test_dns_timeout_aborts_at_step1(self):
        c, sink = FakeClock(), InMemoryAuditSink()
        r = build_timed(clock=c, sink=sink,
                        dns=_advance(c, P.timeout_dns + 1)).run(ENDPOINT)
        self._assert_timeout_abort(r, sink, HandshakeState.RESOLVE_ENDPOINT,
                                   AbortReason.STEP_TIMEOUT, "dns_lookup")

    def test_card_fetch_past_connect_tls_read_budget_aborts(self):
        c, sink = FakeClock(), InMemoryAuditSink()
        slow = P.timeout_connect + P.timeout_tls + P.timeout_fetch_read + 0.1
        r = build_timed(clock=c, sink=sink, fetch=_advance(c, slow)).run(ENDPOINT)
        self._assert_timeout_abort(r, sink, HandshakeState.RESOLVE_ENDPOINT,
                                   AbortReason.STEP_TIMEOUT, "fetch_read")

    def test_fetcher_reported_connect_and_tls_timeouts_abort(self):
        for step in (TimeoutStep.CONNECT, TimeoutStep.TLS):
            with self.subTest(step=step):
                c, sink = FakeClock(), InMemoryAuditSink()

                def blew(step=step):
                    raise StepTimeout(step)

                r = build_timed(clock=c, sink=sink, fetch=blew).run(ENDPOINT)
                self._assert_timeout_abort(
                    r, sink, HandshakeState.RESOLVE_ENDPOINT,
                    AbortReason.STEP_TIMEOUT, step.value)

    def test_presentation_roundtrip_timeout_aborts_at_step3(self):
        c, sink = FakeClock(), InMemoryAuditSink()
        r = build_timed(clock=c, sink=sink, verify=_advance(
            c, P.timeout_handshake_roundtrip + 1)).run(ENDPOINT)
        self._assert_timeout_abort(r, sink, HandshakeState.PRESENT_IDENTITY,
                                   AbortReason.STEP_TIMEOUT, "handshake_roundtrip")

    def test_total_deadline_caps_steps_that_each_fit(self):
        # Each step stays inside its OWN timeout, but together they walk past
        # the total deadline — the slow-counterparty case D3 names.
        params = NetcapsParams(timeout_dns=4, timeout_connect=4, timeout_tls=4,
                               timeout_fetch_read=4, timeout_handshake_roundtrip=4,
                               timeout_total_handshake=6)
        c, sink = FakeClock(), InMemoryAuditSink()
        r = build_timed(clock=c, sink=sink, params=params,
                        dns=_advance(c, 3), fetch=_advance(c, 3.5)).run(ENDPOINT)
        self._assert_timeout_abort(r, sink, HandshakeState.RESOLVE_ENDPOINT,
                                   AbortReason.TOTAL_DEADLINE_EXCEEDED, "total")

    def test_timeout_abort_does_not_record_downgrade_memory(self):
        c, store = FakeClock(), InMemoryDowngradeStore()
        build_timed(clock=c, downgrade_store=store, verify=_advance(
            c, P.timeout_handshake_roundtrip + 1)).run(ENDPOINT)
        self.assertEqual(store.seen(ENDPOINT), set())

    def test_each_run_gets_a_fresh_deadline(self):
        c = FakeClock()
        hs = build_timed(clock=c)
        c.advance(P.timeout_total_handshake * 10)  # time passes between runs
        self.assertTrue(hs.run(ENDPOINT).admitted)


if __name__ == "__main__":
    unittest.main()
