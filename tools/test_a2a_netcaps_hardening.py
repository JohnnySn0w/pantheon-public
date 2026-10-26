"""Regression tests for T-5399 PR-a: every path that used to ESCAPE
PassportHandshake.run() unaudited is now an ABORT, and the audit sink failing
never turns an ABORT into an exception or an admit.

Each test drives the real PassportHandshake.run() end to end and asserts the
result (ABORTED, no exception), the internal abort_reason, the audit events
and the admission release contract.
"""

from __future__ import annotations

import logging
import unittest

from tools.a2a.netcaps import (
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
)
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_quota import QuotaGate, StaticGrants
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
from tools.test_a2a_passport_handshake import CARD_URL, ENDPOINT, _card_bytes

SRC = "10.0.0.1"
START, RESOLVE, PRESENT, POST_AUTH = (
    AdmissionPhase.START, AdmissionPhase.RESOLVE, AdmissionPhase.PRESENT,
    AdmissionPhase.POST_AUTH)


class _Adapter:
    scheme = Scheme.MTLS

    def verify(self, endpoint_id, allowlist_entry) -> bool:
        return True


class _RaisingGrants:
    def __init__(self, exc):
        self.exc = exc

    def grant_quota(self, did):
        raise self.exc


class _Quota:
    def __init__(self, q):
        self.q = q

    def grant_quota(self, did):
        return self.q


class _RaisingSink:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def emit(self, event):
        self.calls += 1
        raise self.exc


class _Probe:
    """A gate that records releases and raises at `boom_phase` (or never)."""

    def __init__(self, boom_phase=None, exc=None):
        self.boom_phase, self.exc = boom_phase, exc or RuntimeError("gate bug")
        self.released = 0

    def check(self, ctx):
        if ctx.phase is self.boom_phase:
            raise self.exc
        return None

    def release(self, ctx):
        self.released += 1


def rig(*, admission=(), sink=None, resolver=None, fetcher=None, adapter=None):
    clock = FakeClock()
    card = _card_bytes([{"type": "mutualTLS"}])
    h = compute_card_hash(card)
    sink = sink if sink is not None else InMemoryAuditSink()
    hs = PassportHandshake(
        txt_resolver=resolver or (lambda eid: TxtResolution(
            present=True, dnssec_validated=True, card_url=CARD_URL,
            pinned_card_hash=h)),
        card_fetcher=fetcher or (lambda url: card),
        allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
        adapters={Scheme.MTLS: adapter or _Adapter()},
        timeout_policy=TimeoutPolicy(clock=clock),
        audit_sink=sink,
        admission=list(admission),
    )
    return hs, sink, clock


def with_quota(grants, **tight):
    clock = FakeClock()
    lim = RateLimiter(roomy(**tight), clock=clock)
    return lim, QuotaGate(lim, grants)


class QuotaFailOpenTests(unittest.TestCase):
    def _assert_quota_abort(self, r, sink, lim):
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIs(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertIs(r.state, HandshakeState.ADMIT_UNTRUSTED)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.QUOTA_EXHAUSTED])
        self.assertEqual(lim.in_flight(), 0)

    def test_grant_lookup_raises(self):
        lim, quota = with_quota(_RaisingGrants(RuntimeError("secret store down")))
        hs, sink, _ = rig(admission=[lim, quota])
        r = hs.run(ENDPOINT, source=SRC)
        self._assert_quota_abort(r, sink, lim)
        self.assertIn("RuntimeError", sink.events[0].detail)
        self.assertNotIn("secret store down", sink.events[0].detail)

    def test_quota_hostile_values(self):
        for bad in (10**400, -(10**400), 10**309, float("nan"), float("inf"),
                    float("-inf"), True, False, -1, "5", []):
            with self.subTest(quota=repr(bad)[:20]):
                lim, quota = with_quota(_Quota(bad))
                hs, sink, _ = rig(admission=[lim, quota])
                self._assert_quota_abort(hs.run(ENDPOINT, source=SRC), sink, lim)

    def test_quota_huge_valid_values_admit_without_raising(self):
        for ok in (1.7e308, 10**308, 1e300):
            with self.subTest(quota=ok):
                lim, quota = with_quota(_Quota(ok))
                hs, sink, _ = rig(admission=[lim, quota])
                r = hs.run(ENDPOINT, source=SRC)
                self.assertIs(r.outcome, Outcome.ADMITTED)
                self.assertEqual(sink.events, [])

    def test_lookup_baseexception_propagates_and_releases(self):
        for exc in (KeyboardInterrupt(), SystemExit(1)):
            lim, quota = with_quota(_RaisingGrants(exc))
            hs, sink, _ = rig(admission=[lim, quota])
            with self.assertRaises(type(exc)):
                hs.run(ENDPOINT, source=SRC)
            self.assertEqual(lim.in_flight(), 0)

    def test_known_false_direct_uses_ungranted_tier_without_lookup(self):
        lim, _ = with_quota(None)
        grants = _RaisingGrants(AssertionError("lookup must not be called"))
        gate = QuotaGate(lim, grants)
        ctx = AdmissionContext(phase=POST_AUTH, endpoint_id=ENDPOINT,
                               source=SRC, known=False, principal=ENDPOINT)
        self.assertIsNone(gate.check(ctx))

    def test_integration_real_limiter_and_quota(self):
        grants = StaticGrants({ENDPOINT: 2})
        lim, quota = with_quota(grants)
        hs, sink, _ = rig(admission=[lim, quota])
        self.assertIs(hs.run(ENDPOINT, source=SRC).outcome, Outcome.ADMITTED)
        self.assertIs(hs.run(ENDPOINT, source=SRC).outcome, Outcome.ADMITTED)
        r = hs.run(ENDPOINT, source=SRC)
        self._assert_quota_abort(r, sink, lim)
        # A broken lookup swaps in mid-life: still an audited ABORT.
        quota._grants = _RaisingGrants(OSError("down"))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual(len(sink.events), 2)
        self.assertEqual(lim.in_flight(), 0)


class AuditSinkFailureTests(unittest.TestCase):
    def test_raising_sink_still_aborts_and_logs_error(self):
        lim, quota = with_quota(_Quota(-1))
        sink = _RaisingSink(RuntimeError("sink down"))
        hs, _, _ = rig(admission=[lim, quota], sink=sink)
        with self.assertLogs("pantheon.a2a.passport_handshake",
                             level=logging.ERROR) as cm:
            r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIs(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual(sink.calls, 1)
        self.assertTrue(any("RuntimeError" in m for m in cm.output))
        self.assertTrue(any("audit sink failed" in line.lower() for line in r.log))
        self.assertEqual(lim.in_flight(), 0)

    def test_raising_sink_on_timeout_breach(self):
        def slow(eid):
            raise TimeoutError("dns")
        sink = _RaisingSink(OSError("disk full"))
        hs, _, _ = rig(sink=sink, resolver=slow)
        with self.assertLogs("pantheon.a2a.passport_handshake",
                             level=logging.ERROR):
            r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.abort_reason, AbortReason.STEP_TIMEOUT)

    def test_sink_baseexception_propagates_and_releases(self):
        for exc in (KeyboardInterrupt(), SystemExit(2)):
            lim, quota = with_quota(_Quota(-1))
            hs, _, _ = rig(admission=[lim, quota], sink=_RaisingSink(exc))
            with self.assertRaises(type(exc)):
                hs.run(ENDPOINT, source=SRC)
            self.assertEqual(lim.in_flight(), 0)


class GateRaisesTests(unittest.TestCase):
    def test_gate_check_raises_at_each_phase(self):
        for phase in (START, RESOLVE, PRESENT, POST_AUTH):
            with self.subTest(phase=phase.value):
                lim = RateLimiter(roomy(), clock=FakeClock())
                before, boom = _Probe(), _Probe(phase)
                hs, sink, _ = rig(admission=[lim, before, boom])
                r = hs.run(ENDPOINT, source=SRC)
                self.assertIs(r.outcome, Outcome.ABORTED)
                self.assertIs(r.abort_reason, AbortReason.RATE_LIMITED)
                self.assertEqual([e.type for e in sink.events],
                                 [AuditEventType.RATE_LIMIT_REJECTED])
                d = sink.events[0].detail
                self.assertIn("_Probe", d)
                self.assertIn("RuntimeError", d)
                self.assertEqual(lim.in_flight(), 0)
                self.assertEqual(before.released, 1)
                # A gate whose START check raised was never started.
                self.assertEqual(boom.released, 0 if phase is START else 1)

    def test_gate_raising_netcapsbreach_maps_through_gate_abort(self):
        from tools.a2a.netcaps_breach import NetcapsBreach
        exc = NetcapsBreach(AbortReason.QUOTA_EXHAUSTED.value,
                            AuditEventType.QUOTA_EXHAUSTED.value, "x")
        hs, sink, _ = rig(admission=[_Probe(RESOLVE, exc)])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual(len(sink.events), 1)

    def test_gate_baseexception_propagates_and_releases(self):
        for exc in (KeyboardInterrupt(), SystemExit(3)):
            first, boom = _Probe(), _Probe(RESOLVE, exc)
            hs, sink, _ = rig(admission=[first, boom])
            with self.assertRaises(type(exc)):
                hs.run(ENDPOINT, source=SRC)
            self.assertEqual((first.released, boom.released), (1, 1))
            self.assertEqual(sink.events, [])


class SeamExceptionTests(unittest.TestCase):
    """Injected resolver / fetcher misbehaviour -> the ORG-047 reason, logged
    ABORT, no bound-breach audit event."""

    def _check(self, r, sink, reason):
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIs(r.abort_reason, reason)
        self.assertEqual(sink.events, [])

    def test_resolver_raises(self):
        for exc in (ValueError("bad"), OSError("net"), KeyError("k")):
            def res(eid, exc=exc):
                raise exc
            hs, sink, _ = rig(resolver=res)
            self._check(hs.run(ENDPOINT, source=SRC), sink, AbortReason.NO_TXT)

    def test_fetcher_raises(self):
        def fetch(url):
            raise ConnectionResetError("reset")
        hs, sink, _ = rig(fetcher=fetch)
        self._check(hs.run(ENDPOINT, source=SRC), sink,
                    AbortReason.CARD_FETCH_FAILED)

    def test_fetcher_returns_non_bytes(self):
        hs, sink, _ = rig(fetcher=lambda url: "not bytes")
        self._check(hs.run(ENDPOINT, source=SRC), sink,
                    AbortReason.CARD_FETCH_FAILED)

    def test_deeply_nested_card(self):
        card = b"[" * 100000 + b"]" * 100000
        h = compute_card_hash(card)
        hs = PassportHandshake(
            txt_resolver=lambda eid: TxtResolution(
                present=True, dnssec_validated=True, card_url=CARD_URL,
                pinned_card_hash=h),
            card_fetcher=lambda url: card,
            allowlist={ENDPOINT: AllowlistEntry(card_hash=h)},
            adapters={Scheme.MTLS: _Adapter()},
            audit_sink=InMemoryAuditSink())
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.abort_reason, AbortReason.CARD_UNPARSEABLE)

    def test_seam_baseexception_propagates(self):
        def res(eid):
            raise KeyboardInterrupt()
        hs, _, _ = rig(resolver=res)
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)



# ---------------------------------------------------------------------------
# T-5411: raising downgrade_store, raising gate.release, unhashable endpoint_id.
# ---------------------------------------------------------------------------


class _BadStore:
    """A DowngradeStore whose `seen` and/or `record` raises."""

    def __init__(self, seen_exc=None, record_exc=None):
        self.seen_exc, self.record_exc = seen_exc, record_exc
        self.recorded = []

    def seen(self, endpoint_id):
        if self.seen_exc is not None:
            raise self.seen_exc
        return set()

    def record(self, endpoint_id, schemes):
        if self.record_exc is not None:
            raise self.record_exc
        self.recorded.append((endpoint_id, schemes))

    def reset(self, endpoint_id):
        pass


def _rig_store(store, *, admission=()):
    hs, sink, clock = rig(admission=admission)
    hs._downgrade = store
    return hs, sink


class DowngradeStoreRaisesTests(unittest.TestCase):
    def test_seen_raises_is_audited_abort(self):
        probe = _Probe()
        hs, sink = _rig_store(_BadStore(seen_exc=RuntimeError("secret db down")),
                              admission=[probe])
        with self.assertLogs("pantheon.a2a.passport_handshake", "WARNING") as cm:
            r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIs(r.abort_reason, AbortReason.DOWNGRADE_STORE_FAILED)
        self.assertIs(r.state, HandshakeState.SELECT_SCHEME)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.DOWNGRADE_STORE_FAILED])
        self.assertEqual(sink.events[0].reason, "downgrade_store_failed")
        self.assertIn("RuntimeError", sink.events[0].detail)
        self.assertNotIn("secret db down", sink.events[0].detail)
        self.assertNotIn("secret db down", "\n".join(cm.output))
        self.assertEqual(probe.released, 1)

    def test_record_raises_is_audited_abort_not_admit(self):
        probe = _Probe()
        hs, sink = _rig_store(_BadStore(record_exc=OSError("disk full")),
                              admission=[probe])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIsNone(r.principal)
        self.assertIs(r.abort_reason, AbortReason.DOWNGRADE_STORE_FAILED)
        self.assertIs(r.state, HandshakeState.ADMIT_UNTRUSTED)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.DOWNGRADE_STORE_FAILED])
        self.assertNotIn("disk full", sink.events[0].detail)
        self.assertEqual(probe.released, 1)

    def test_store_baseexception_propagates_and_releases(self):
        for kw in ({"seen_exc": KeyboardInterrupt()},
                   {"record_exc": SystemExit(2)}):
            probe = _Probe()
            hs, _ = _rig_store(_BadStore(**kw), admission=[probe])
            with self.assertRaises((KeyboardInterrupt, SystemExit)):
                hs.run(ENDPOINT, source=SRC)
            self.assertEqual(probe.released, 1)


class _BadRelease(_Probe):
    def release(self, ctx):
        super().release(ctx)
        raise RuntimeError("release bug secret")


class _RefuseAtResolve(_Probe):
    def check(self, ctx):
        if ctx.phase is RESOLVE:
            return NetcapsBreach(AbortReason.QUOTA_EXHAUSTED.value,
                                 AuditEventType.QUOTA_EXHAUSTED.value, "spent")
        return None


class GateReleaseRaisesTests(unittest.TestCase):
    def test_release_raises_after_admit_becomes_audited_abort(self):
        first, bad, last = _Probe(), _BadRelease(), _Probe()
        hs, sink, _ = rig(admission=[first, bad, last])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.outcome, Outcome.ABORTED)
        self.assertIsNone(r.principal)
        self.assertIs(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.RATE_LIMIT_REJECTED])
        self.assertIn("gate release failed", sink.events[0].detail)
        self.assertIn("RuntimeError", sink.events[0].detail)
        self.assertNotIn("release bug secret", sink.events[0].detail)
        self.assertEqual((first.released, bad.released, last.released), (1, 1, 1))

    def test_release_raises_after_abort_keeps_original_reason(self):
        bad = _BadRelease()
        hs, sink, _ = rig(admission=[_RefuseAtResolve(), bad])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.abort_reason, AbortReason.QUOTA_EXHAUSTED)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.QUOTA_EXHAUSTED,
                          AuditEventType.RATE_LIMIT_REJECTED])
        self.assertIn("gate release failed", sink.events[1].detail)
        self.assertEqual(bad.released, 1)

    def test_release_raises_with_failing_sink_still_returns(self):
        hs, _, _ = rig(admission=[_BadRelease()], sink=_RaisingSink(OSError("x")))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertIs(r.outcome, Outcome.ABORTED)

    def test_release_baseexception_propagates(self):
        class Bad(_Probe):
            def release(self, ctx):
                raise KeyboardInterrupt()
        hs, _, _ = rig(admission=[Bad()])
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)


class UnhashableEndpointTests(unittest.TestCase):
    def test_unhashable_endpoint_is_refused_before_any_gate(self):
        probe = _Probe()
        hs, sink, _ = rig(admission=[probe])
        for bad in ([ENDPOINT], {"a": 1}, {1, 2}):
            with self.subTest(t=type(bad).__name__):
                with self.assertLogs("pantheon.a2a.passport_handshake",
                                     "WARNING") as cm:
                    r = hs.run(bad, source=SRC)
                self.assertIs(r.outcome, Outcome.ABORTED)
                self.assertIs(r.abort_reason, AbortReason.ENDPOINT_NOT_ALLOWLISTED)
                self.assertIs(r.state, HandshakeState.RESOLVE_ENDPOINT)
                self.assertIn(type(bad).__name__, "\n".join(cm.output))
                self.assertNotIn(ENDPOINT, "\n".join(cm.output))
        self.assertEqual(sink.events, [])
        self.assertEqual(probe.released, 0)



class _ReleaseRaises(_Probe):
    def __init__(self, exc):
        super().__init__()
        self.rel_exc = exc

    def release(self, ctx):
        super().release(ctx)
        raise self.rel_exc


class ReleaseBaseExceptionPrecedenceTests(unittest.TestCase):
    """T-5411 REBOUND 1: a BaseException from any release is never dropped in
    favour of an Exception from another; the in-flight BaseException is never
    replaced by a release Exception."""

    def test_baseexception_from_later_release_beats_earlier_exception(self):
        # releases run in reverse: `exc_gate` (last in list) releases FIRST.
        ki_gate = _ReleaseRaises(KeyboardInterrupt())
        mid = _Probe()
        exc_gate = _ReleaseRaises(RuntimeError("bug"))
        hs, sink, _ = rig(admission=[ki_gate, mid, exc_gate])
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)
        self.assertEqual((ki_gate.released, mid.released, exc_gate.released),
                         (1, 1, 1))

    def test_first_baseexception_wins_among_several(self):
        a = _ReleaseRaises(SystemExit(7))
        b = _ReleaseRaises(KeyboardInterrupt())
        hs, _, _ = rig(admission=[a, b])
        with self.assertRaises(KeyboardInterrupt):  # b releases first
            hs.run(ENDPOINT, source=SRC)
        self.assertEqual((a.released, b.released), (1, 1))

    def test_inflight_baseexception_survives_release_exception(self):
        def res(eid):
            raise KeyboardInterrupt()
        bad = _ReleaseRaises(RuntimeError("release bug"))
        hs, sink, _ = rig(admission=[bad], resolver=res)
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)
        self.assertEqual(bad.released, 1)

    def test_baseexception_from_release_replaces_inflight(self):
        def res(eid):
            raise KeyboardInterrupt()
        bad = _ReleaseRaises(SystemExit(4))
        hs, _, _ = rig(admission=[bad], resolver=res)
        with self.assertRaises(SystemExit):
            hs.run(ENDPOINT, source=SRC)


if __name__ == "__main__":
    unittest.main()
