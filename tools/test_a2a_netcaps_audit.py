"""Tests for the ORG-071 D6 vocabulary in tools/a2a/netcaps.py + its composition
with passport_handshake's AbortReason / HandshakeResult (T-2857 PR1).

The property under test: the PEER sees one coarse refusal for every ABORT (a
bound breach is indistinguishable from an auth failure — the anti-oracle), while
the internal side keeps the precise AbortReason and emits a named ORG-070 audit
event for every bound breach.
"""

from __future__ import annotations

import logging
import unittest

from tools.a2a.netcaps import (
    PEER_ABORT,
    AuditEvent,
    AuditEventType,
    AuditSink,
    FakeClock,
    InMemoryAuditSink,
    LoggingAuditSink,
    MonotonicClock,
)
from tools.a2a.passport_handshake import (
    BOUND_BREACH_EVENTS,
    AbortReason,
    HandshakeResult,
    HandshakeState,
    Outcome,
    Scheme,
)
from tools.test_a2a_netcaps_timeouts import build_timed
from tools.test_a2a_passport_handshake import _build

# ORG-047 D1 auth-failure reasons (pre-ORG-071): these are NOT bound breaches.
AUTH_REASONS = {
    AbortReason.NO_TXT, AbortReason.DNSSEC_INVALID, AbortReason.CARD_FETCH_FAILED,
    AbortReason.CARD_HASH_MISMATCH, AbortReason.ENDPOINT_NOT_ALLOWLISTED,
    AbortReason.CARD_UNPARSEABLE, AbortReason.NO_COMMON_SCHEME,
    AbortReason.DOWNGRADE_DETECTED, AbortReason.PRESENTATION_FAILED,
}


def _aborted(reason: AbortReason) -> HandshakeResult:
    return HandshakeResult(outcome=Outcome.ABORTED,
                           state=HandshakeState.RESOLVE_ENDPOINT,
                           abort_reason=reason)


class TestAuditEventVocabulary(unittest.TestCase):
    def test_exactly_the_named_org070_event_types(self):
        self.assertEqual(
            {e.value for e in AuditEventType},
            {"rate_limit_rejected", "quota_exhausted", "resolver_timeout",
             "status_fetch_failed", "negative_cache_hit", "stale_denied",
             "circuit_breaker_open", "unknown_did_budget_exceeded",
             "did_resolution_failed", "downgrade_store_failed"})

    def test_every_non_auth_reason_is_a_bound_breach_with_an_event(self):
        breach = set(AbortReason) - AUTH_REASONS
        self.assertEqual(set(BOUND_BREACH_EVENTS), breach)
        for reason, event in BOUND_BREACH_EVENTS.items():
            self.assertIsInstance(event, AuditEventType, reason)

    def test_every_event_type_is_reachable_from_some_reason(self):
        self.assertEqual(set(BOUND_BREACH_EVENTS.values()), set(AuditEventType))

    def test_timeouts_emit_resolver_timeout(self):
        self.assertIs(BOUND_BREACH_EVENTS[AbortReason.STEP_TIMEOUT],
                      AuditEventType.RESOLVER_TIMEOUT)
        self.assertIs(BOUND_BREACH_EVENTS[AbortReason.TOTAL_DEADLINE_EXCEEDED],
                      AuditEventType.RESOLVER_TIMEOUT)


class TestCoarsePeerFacingAbort(unittest.TestCase):
    def test_peer_error_identical_for_every_abort_reason(self):
        for reason in AbortReason:
            with self.subTest(reason=reason):
                self.assertEqual(_aborted(reason).peer_error, PEER_ABORT)

    def test_peer_error_leaks_no_internal_reason(self):
        for reason in AbortReason:
            self.assertNotIn(reason.value, PEER_ABORT)
        for event in AuditEventType:
            self.assertNotIn(event.value, PEER_ABORT)

    def test_admitted_has_no_peer_error(self):
        hs, _, _ = _build()
        self.assertIsNone(hs.run("did:web:your-domain.example").peer_error)

    def test_live_auth_failure_and_bound_breach_look_the_same_to_the_peer(self):
        # Auth failure: adapter refuses the presentation.
        auth_fail, _, _ = _build(adapter_results={Scheme.MTLS: False})
        a = auth_fail.run("did:web:your-domain.example")
        # Bound breach: the DNS lookup blows its timeout.
        clock = FakeClock()
        b = build_timed(clock=clock, dns=lambda: clock.advance(10_000)).run(
            "did:web:your-domain.example")
        self.assertEqual(a.outcome, b.outcome)
        self.assertEqual(a.peer_error, b.peer_error)
        # ...while the INTERNAL reasons stay distinct.
        self.assertEqual(a.abort_reason, AbortReason.PRESENTATION_FAILED)
        self.assertEqual(b.abort_reason, AbortReason.STEP_TIMEOUT)


class TestAuditSinks(unittest.TestCase):
    def _event(self, t=AuditEventType.RATE_LIMIT_REJECTED):
        return AuditEvent(type=t, endpoint_id="did:web:x", reason="r",
                          detail="d", at=1.0)

    def test_in_memory_sink_records_in_order_and_filters(self):
        sink = InMemoryAuditSink()
        self.assertIsInstance(sink, AuditSink)
        sink.emit(self._event())
        sink.emit(self._event(AuditEventType.STALE_DENIED))
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.RATE_LIMIT_REJECTED,
                          AuditEventType.STALE_DENIED])
        self.assertEqual(len(sink.of(AuditEventType.STALE_DENIED)), 1)

    def test_logging_sink_logs_the_event_type(self):
        sink = LoggingAuditSink()
        self.assertIsInstance(sink, AuditSink)
        with self.assertLogs("pantheon.a2a.netcaps", level=logging.WARNING) as cm:
            sink.emit(self._event(AuditEventType.CIRCUIT_BREAKER_OPEN))
        self.assertIn("circuit_breaker_open", cm.output[0])

    def test_auth_failure_emits_no_netcaps_event(self):
        sink = InMemoryAuditSink()
        # No TXT record: an ORG-047 auth failure, not an ORG-071 bound breach.
        r = build_timed(clock=FakeClock(), sink=sink, present=False).run(
            "did:web:your-domain.example")
        self.assertEqual(r.abort_reason, AbortReason.NO_TXT)
        self.assertEqual(sink.events, [])


class TestClocks(unittest.TestCase):
    def test_fake_clock_advances_only_when_told(self):
        c = FakeClock(start=5.0)
        self.assertEqual(c.monotonic(), 5.0)
        c.advance(2.5)
        self.assertEqual(c.monotonic(), 7.5)

    def test_fake_clock_never_goes_backwards(self):
        with self.assertRaises(ValueError):
            FakeClock().advance(-1)

    def test_monotonic_clock_is_non_decreasing(self):
        c = MonotonicClock()
        a = c.monotonic()
        self.assertGreaterEqual(c.monotonic(), a)


if __name__ == "__main__":
    unittest.main()
