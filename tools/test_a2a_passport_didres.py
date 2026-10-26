"""Tests for in-run DID-document resolution (T-5399 PR-b, ORG-071 D1/D3/D6).

PassportHandshake.run() resolves the counterparty DID document itself, under
the run's total deadline and the RESOLVE admission gate, for adapters that
declare `needs_did_doc`. Failures map to AbortReason.DID_RESOLUTION_FAILED with
exactly one audit event. Also pins the generic-exception -> PRESENTATION_FAILED
mapping for adapter.verify / verify_with_doc.

FakeClock throughout; no network, no sleeps.
"""

from __future__ import annotations

import json
import random
import unittest

from tools.a2a.did_web import DidResolutionError, ParsedDidDocument, parse_did_document
from tools.a2a.netcaps import (
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
    NetcapsParams,
)
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_cache import (
    CacheBreach,
    FailureClass,
    FetchFailure,
    FetchResponse,
    ResolutionCache,
)
from tools.a2a.netcaps_ratelimit import UNKNOWN_SOURCE, RateLimiter
from tools.a2a.netcaps_timeouts import HandshakeDeadline, TimeoutPolicy
from tools.a2a.passport_handshake import (
    BOUND_BREACH_EVENTS,
    AbortReason,
    AllowlistEntry,
    HandshakeState,
    Outcome,
    PassportHandshake,
    Scheme,
    TxtResolution,
    compute_card_hash,
)
from tools.test_a2a_passport_handshake import CARD_URL, ENDPOINT

try:
    import jwt  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import ec
    _HAVE = True
except ImportError:  # pragma: no cover
    _HAVE = False

if _HAVE:
    from tools.a2a.passport_bearer import LiveBearerAdapter
    from tools.test_a2a_passport_bearer import AUD, _jwk_from_ec_pub, _token

SRC = "10.0.0.1"
CARD = b'{"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}}'
CARD_HASH = compute_card_hash(CARD)
DID_REASON = AbortReason.DID_RESOLUTION_FAILED


def _doc(did: str = ENDPOINT) -> ParsedDidDocument:
    kid = f"{did}#k1"
    return parse_did_document({
        "id": did,
        "verificationMethod": [{
            "id": kid, "type": "JsonWebKey2020", "controller": did,
            "publicKeyJwk": {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}}],
        "authentication": [kid]}, did)


class _DocAdapter:
    """Declares it needs the run-resolved DID doc; records what it receives."""

    def __init__(self, *, needs=True, result=True, raises=None, on_verify=None):
        self.needs_did_doc = needs
        self.result = result
        self.raises = raises
        self.on_verify = on_verify
        self.docs: list = []
        self.plain_verifies = 0

    @property
    def scheme(self) -> Scheme:
        return Scheme.OIDC_BEARER

    def verify(self, endpoint_id, allowlist_entry) -> bool:
        self.plain_verifies += 1
        if self.raises is not None:
            raise self.raises
        return self.result

    def verify_with_doc(self, endpoint_id, allowlist_entry, did_doc) -> bool:
        self.docs.append(did_doc)
        if self.on_verify:
            self.on_verify()
        if self.raises is not None:
            raise self.raises
        return self.result


class _Resolver:
    """did_resolver stand-in: (did, *, source, deadline) -> doc; records calls."""

    def __init__(self, result=None, *, raises=None, on_call=None):
        self.result = result
        self.raises = raises
        self.on_call = on_call
        self.calls: list[tuple] = []

    def __call__(self, did, *, source, deadline):
        self.calls.append((did, source, deadline))
        if self.on_call:
            self.on_call()
        if self.raises is not None:
            raise self.raises
        return self.result if self.result is not None else _doc(did)


class _Gate:
    """Admission gate that records contexts and refuses chosen phases/resources."""

    def __init__(self, refuse=None):
        self.refuse = refuse
        self.seen: list[AdmissionContext] = []

    def check(self, ctx):
        self.seen.append(ctx)
        if self.refuse and self.refuse(ctx):
            return NetcapsBreach("rate_limit_rejected", "rate_limit_rejected",
                                 "gate refused")
        return None

    def release(self, ctx):
        pass


def build(adapter=None, resolver=None, *, params=None, clock=None, gates=(),
          sink=None, with_resolver=True):
    clock = clock or FakeClock()
    sink = sink if sink is not None else InMemoryAuditSink()
    adapter = adapter if adapter is not None else _DocAdapter()
    resolver = resolver if resolver is not None else _Resolver()

    def txt(endpoint_id):
        return TxtResolution(True, True, CARD_URL, CARD_HASH)

    kw = {"did_resolver": resolver} if with_resolver else {}
    hs = PassportHandshake(
        txt_resolver=txt, card_fetcher=lambda u: CARD,
        allowlist={ENDPOINT: AllowlistEntry(card_hash=CARD_HASH)},
        adapters={Scheme.OIDC_BEARER: adapter},
        timeout_policy=TimeoutPolicy(params or NetcapsParams(), clock=clock),
        audit_sink=sink, admission=list(gates), **kw)
    return hs, adapter, resolver, sink, clock


class TestResolveInsideRun(unittest.TestCase):
    def test_resolver_gets_run_deadline_and_source(self):
        hs, _, res, _, _ = build()
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ADMITTED)
        (did, source, deadline), = res.calls
        self.assertEqual((did, source), (ENDPOINT, SRC))
        self.assertIsInstance(deadline, HandshakeDeadline)

    def test_unknown_source_uses_the_shared_unknown_pool(self):
        hs, _, res, _, _ = build()
        hs.run(ENDPOINT)
        self.assertEqual(res.calls[0][1], UNKNOWN_SOURCE)

    def test_doc_reaches_the_adapter(self):
        doc = _doc()
        hs, ad, _, _, _ = build(resolver=_Resolver(doc))
        hs.run(ENDPOINT, source=SRC)
        self.assertEqual(ad.docs, [doc])
        self.assertEqual(ad.plain_verifies, 0)

    def test_adapter_without_the_need_never_triggers_the_resolver(self):
        hs, ad, res, _, _ = build(adapter=_DocAdapter(needs=False))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertTrue(r.admitted)
        self.assertEqual(res.calls, [])
        self.assertEqual(ad.plain_verifies, 1)

    def test_adapter_without_the_need_attribute_is_unchanged(self):
        class Plain:
            scheme = Scheme.OIDC_BEARER

            def verify(self, e, a):
                return True
        hs, _, res, _, _ = build(adapter=Plain())
        self.assertTrue(hs.run(ENDPOINT, source=SRC).admitted)
        self.assertEqual(res.calls, [])

    def test_no_did_resolver_means_plain_verify(self):
        hs, ad, _, _, _ = build(with_resolver=False)
        self.assertTrue(hs.run(ENDPOINT, source=SRC).admitted)
        self.assertEqual(ad.plain_verifies, 1)
        self.assertEqual(ad.docs, [])


class TestDeadline(unittest.TestCase):
    P = NetcapsParams(timeout_total_handshake=11)

    def test_spent_deadline_aborts_before_any_fetch(self):
        clock = FakeClock()
        sink = InMemoryAuditSink()
        # Burn the whole budget in the card fetch: 5s dns + 6s fetch = 11s.
        res = _Resolver()
        hs = PassportHandshake(
            txt_resolver=lambda e: (clock.advance(5) or
                                    TxtResolution(True, True, CARD_URL, CARD_HASH)),
            card_fetcher=lambda u: (clock.advance(6) or CARD),
            allowlist={ENDPOINT: AllowlistEntry(card_hash=CARD_HASH)},
            adapters={Scheme.OIDC_BEARER: _DocAdapter()},
            timeout_policy=TimeoutPolicy(self.P, clock=clock),
            audit_sink=sink, did_resolver=res)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, AbortReason.TOTAL_DEADLINE_EXCEEDED)
        self.assertEqual(res.calls, [])
        self.assertEqual(len(sink.events), 1)

    def test_slow_resolver_times_out_and_late_doc_is_not_used(self):
        clock = FakeClock()
        res = _Resolver(on_call=lambda: clock.advance(12))
        hs, ad, _, sink, _ = build(resolver=res, clock=clock)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertIn(r.abort_reason, (AbortReason.STEP_TIMEOUT,
                                       AbortReason.TOTAL_DEADLINE_EXCEEDED))
        self.assertEqual(ad.docs, [])
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.RESOLVER_TIMEOUT])

    def test_resolver_advancing_past_deadline_then_valid_doc_is_discarded(self):
        clock = FakeClock()
        res = _Resolver(_doc(), on_call=lambda: clock.advance(100))
        hs, ad, _, sink, _ = build(resolver=res, clock=clock)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertFalse(r.admitted)
        self.assertIn(r.abort_reason, (AbortReason.STEP_TIMEOUT,
                                       AbortReason.TOTAL_DEADLINE_EXCEEDED))
        self.assertEqual(ad.docs, [])
        self.assertEqual(len(sink.events), 1)


class TestAdmissionGate(unittest.TestCase):
    def test_resolve_gate_consulted_with_did_doc_resource(self):
        gate = _Gate()
        hs, _, _, _, _ = build(gates=[gate])
        hs.run(ENDPOINT, source=SRC)
        resolves = [c for c in gate.seen if c.phase is AdmissionPhase.RESOLVE]
        self.assertEqual([c.resource for c in resolves], ["txt", "did_doc"])
        self.assertTrue(all(c.known and c.source == SRC for c in resolves))

    def test_gate_refusal_aborts_audited_and_resolver_never_called(self):
        gate = _Gate(refuse=lambda c: c.phase is AdmissionPhase.RESOLVE
                     and c.resource == "did_doc")
        hs, ad, res, sink, _ = build(gates=[gate])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, AbortReason.RATE_LIMITED)
        self.assertEqual(r.state, HandshakeState.PRESENT_IDENTITY)
        self.assertEqual(res.calls, [])
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.RATE_LIMIT_REJECTED])

    def test_no_did_doc_resource_consulted_when_not_needed(self):
        gate = _Gate()
        hs, _, _, _, _ = build(adapter=_DocAdapter(needs=False), gates=[gate])
        hs.run(ENDPOINT, source=SRC)
        self.assertEqual([c.resource for c in gate.seen
                          if c.phase is AdmissionPhase.RESOLVE], ["txt"])


class TestFailureMapping(unittest.TestCase):
    def _assert_did_failed(self, res):
        hs, ad, _, sink, _ = build(resolver=res)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertEqual(r.state, HandshakeState.PRESENT_IDENTITY)
        self.assertEqual(ad.docs, [])
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.DID_RESOLUTION_FAILED])
        self.assertEqual(sink.events[0].reason, DID_REASON.value)
        return sink.events[0]

    def test_did_resolution_error(self):
        self._assert_did_failed(_Resolver(raises=DidResolutionError("bad doc")))

    def test_generic_exception_detail_has_type_only(self):
        ev = self._assert_did_failed(
            _Resolver(raises=RuntimeError("SECRET-counterparty-text")))
        self.assertIn("RuntimeError", ev.detail)
        self.assertNotIn("SECRET", ev.detail)

    def test_doc_for_a_different_did(self):
        self._assert_did_failed(_Resolver(_doc("did:web:evil.example")))

    def test_non_document_result(self):
        self._assert_did_failed(_Resolver({"id": ENDPOINT}))

    def test_cache_breach_uses_its_mapped_reason(self):
        hs, ad, _, sink, _ = build(resolver=_Resolver(raises=CacheBreach(
            "negative_cache_hit", "negative_cache_hit", "neg")))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, AbortReason.NEGATIVE_CACHE_HIT)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.NEGATIVE_CACHE_HIT])

    def test_did_resolution_failed_cache_breach_maps_to_new_reason(self):
        hs, _, _, sink, _ = build(resolver=_Resolver(raises=CacheBreach(
            "did_resolution_failed", "did_resolution_failed", "x")))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertEqual([e.type for e in sink.events],
                         [AuditEventType.DID_RESOLUTION_FAILED])

    def test_base_exception_is_not_swallowed(self):
        hs, _, _, _, _ = build(resolver=_Resolver(raises=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)


class TestAdapterGenericException(unittest.TestCase):
    """Added scope: a generic Exception from verify / verify_with_doc is a
    refused presentation (logged, type only), never an escaping exception."""

    def _run(self, adapter, **kw):
        kw.setdefault("with_resolver", adapter.needs_did_doc)
        hs, ad, _, sink, _ = build(adapter=adapter, **kw)
        with self.assertLogs("pantheon.a2a.passport_handshake", "WARNING") as lg:
            r = hs.run(ENDPOINT, source=SRC)
        return r, sink, "\n".join(lg.output)

    def test_verify_with_doc_generic_exception(self):
        r, sink, out = self._run(_DocAdapter(raises=ValueError("PEER-TEXT")))
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)
        self.assertIn("ValueError", out)
        self.assertNotIn("PEER-TEXT", out)
        self.assertNotIn("PEER-TEXT", "".join(r.log))

    def test_plain_verify_generic_exception(self):
        r, sink, out = self._run(_DocAdapter(needs=False, raises=KeyError("PEER-TEXT")))
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)
        self.assertIn("KeyError", out)
        self.assertNotIn("PEER-TEXT", out)

    def test_not_implemented_still_presentation_failed(self):
        r, _, _ = self._run(_DocAdapter(needs=False, raises=NotImplementedError()))
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)

    def test_base_exception_propagates(self):
        hs, _, _, _, _ = build(adapter=_DocAdapter(raises=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            hs.run(ENDPOINT, source=SRC)


class _RaisingSink:
    def emit(self, event):
        raise RuntimeError("sink down")


class _RecordingGate(_Gate):
    def __init__(self, refuse=None):
        super().__init__(refuse)
        self.released: list[AdmissionContext] = []

    def release(self, ctx):
        self.released.append(ctx)


class TestInteractionWithHardening(unittest.TestCase):
    def test_raising_sink_still_returns_did_resolution_failed_abort(self):
        hs, ad, _, _, _ = build(resolver=_Resolver(raises=DidResolutionError("x")),
                                sink=_RaisingSink())
        with self.assertLogs("pantheon.a2a.passport_handshake", "ERROR") as lg:
            r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertTrue(any("AUDIT SINK FAILED" in line for line in r.log))
        self.assertTrue(any("ERROR" in o for o in lg.output))
        self.assertEqual(ad.docs, [])

    def test_gate_released_once_on_did_resolution_abort(self):
        gate = _RecordingGate()
        hs, _, _, _, _ = build(resolver=_Resolver(raises=DidResolutionError("x")),
                               gates=[gate])
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertEqual(len(gate.released), 1)
        self.assertIs(gate.released[0].phase, AdmissionPhase.START)

    def test_gate_released_once_on_resolve_gate_refusal(self):
        gate = _RecordingGate(refuse=lambda c: c.resource == "did_doc")
        hs, _, res, _, _ = build(gates=[gate])
        hs.run(ENDPOINT, source=SRC)
        self.assertEqual(res.calls, [])
        self.assertEqual(len(gate.released), 1)


class TestMalformedAdapter(unittest.TestCase):
    """Reviewer LOW: attribute lookups on a malformed adapter must not escape
    run(); they are a refused presentation."""

    def _assert_presentation_failed(self, adapter):
        hs, _, _, sink, _ = build(adapter=adapter)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)

    def test_needs_did_doc_property_raises(self):
        class Bad:
            scheme = Scheme.OIDC_BEARER

            @property
            def needs_did_doc(self):
                raise RuntimeError("boom")

            def verify(self, e, a):
                return True
        self._assert_presentation_failed(Bad())

    def test_needs_did_doc_true_but_no_verify_with_doc(self):
        class NoMethod:
            scheme = Scheme.OIDC_BEARER
            needs_did_doc = True

            def verify(self, e, a):
                return True
        self._assert_presentation_failed(NoMethod())


class TestVocabularyProperties(unittest.TestCase):
    def test_new_reason_and_event_are_mapped(self):
        self.assertEqual(DID_REASON.value, "did_resolution_failed")
        self.assertIs(BOUND_BREACH_EVENTS[DID_REASON],
                      AuditEventType.DID_RESOLUTION_FAILED)

    def test_cache_constants_equal_enum_values(self):
        from tools.a2a import netcaps_cache as nc
        self.assertEqual(nc.R_DID_RESOLUTION, DID_REASON.value)
        self.assertEqual(nc.E_DID_RESOLUTION,
                         AuditEventType.DID_RESOLUTION_FAILED.value)


# ---------------------------------------------------------------------------
# Same-process integration: real cache + stub RawFetcher + real limiter +
# LiveBearerAdapter with NO pre-parsed doc.
# ---------------------------------------------------------------------------

DID_URL = "https://your-domain.example/.well-known/did.json"


class _Fetcher:
    def __init__(self, body=None, exc=None, clock=None, advance=0.0):
        self.body, self.exc, self.clock, self.advance = body, exc, clock, advance
        self.calls = 0

    def __call__(self, url, *, max_bytes, timeout):
        self.calls += 1
        if self.clock and self.advance:
            self.clock.advance(self.advance)
        if self.exc is not None:
            raise self.exc
        return FetchResponse(body=self.body)


def _doc_bytes(key) -> bytes:
    kid = f"{ENDPOINT}#k1"
    return json.dumps({
        "id": ENDPOINT,
        "verificationMethod": [{
            "id": kid, "type": "JsonWebKey2020", "controller": ENDPOINT,
            "publicKeyJwk": _jwk_from_ec_pub(key.public_key())}],
        "authentication": [kid]}).encode()


@unittest.skipUnless(_HAVE, "pyjwt/cryptography not available")
class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.clock = FakeClock()
        self.params = NetcapsParams(jitter_range_pct=0)
        self.cache = ResolutionCache(self.params, clock=self.clock,
                                     rng=random.Random(0))
        self.limiter = RateLimiter(self.params, clock=self.clock)
        self.sink = InMemoryAuditSink()

    def _hs(self, fetcher, token_jti="j1"):
        adapter = LiveBearerAdapter(_token(self.key, jti=token_jti),
                                    audience=AUD)
        return PassportHandshake(
            txt_resolver=lambda e: TxtResolution(True, True, CARD_URL, CARD_HASH),
            card_fetcher=lambda u: CARD,
            allowlist={ENDPOINT: AllowlistEntry(card_hash=CARD_HASH)},
            adapters={Scheme.OIDC_BEARER: adapter},
            timeout_policy=TimeoutPolicy(self.params, clock=self.clock),
            audit_sink=self.sink, admission=[self.limiter],
            did_resolver=self.cache.did_documents(fetcher)), adapter

    def test_end_to_end_admit_then_cache_hit(self):
        f = _Fetcher(_doc_bytes(self.key))
        hs, _ = self._hs(f)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.outcome, Outcome.ADMITTED, r.log)
        self.assertEqual(f.calls, 1)
        self.assertEqual(self.sink.events, [])
        hs2, _ = self._hs(f, token_jti="j2")
        self.assertTrue(hs2.run(ENDPOINT, source=SRC).admitted)
        self.assertEqual(f.calls, 1, "second run served from the cache")

    def test_nothing_held_fetch_failure_reports_did_resolution_failed(self):
        f = _Fetcher(exc=FetchFailure(FailureClass.TRANSIENT_INFRA, "dns"))
        hs, _ = self._hs(f)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertEqual([e.type for e in self.sink.events],
                         [AuditEventType.DID_RESOLUTION_FAILED])

    def test_plain_verify_without_a_doc_fails_closed(self):
        _, adapter = self._hs(_Fetcher(_doc_bytes(self.key)))
        self.assertFalse(adapter.verify(ENDPOINT, AllowlistEntry("x")))

    def test_garbled_doc(self):
        for body in (b"\xff\xfe{{{", b"[]", b'{"id": "did:web:other.example"}',
                     b"null"):
            with self.subTest(body=body):
                self.sink.events.clear()
                hs, _ = self._hs(_Fetcher(body))
                self.cache = ResolutionCache(self.params, clock=self.clock,
                                             rng=random.Random(0))
                hs, _ = self._hs(_Fetcher(body))
                r = hs.run(ENDPOINT, source=SRC)
                self.assertFalse(r.admitted)
                self.assertEqual(r.abort_reason, DID_REASON)
                self.assertEqual(len(self.sink.events), 1)
                self.clock.advance(1000)  # roll the limiter windows

    def test_oversized_body(self):
        big = b" " * (self.params.max_did_doc_bytes + 1)
        hs, _ = self._hs(_Fetcher(big))
        r = hs.run(ENDPOINT, source=SRC)
        self.assertEqual(r.abort_reason, DID_REASON)
        self.assertEqual(len(self.sink.events), 1)

    def test_clock_past_deadline_then_valid_doc_discarded(self):
        f = _Fetcher(_doc_bytes(self.key), clock=self.clock, advance=500)
        hs, _ = self._hs(f)
        r = hs.run(ENDPOINT, source=SRC)
        self.assertFalse(r.admitted)
        self.assertIn(r.abort_reason, (AbortReason.TOTAL_DEADLINE_EXCEEDED,
                                       AbortReason.STEP_TIMEOUT))
        self.assertEqual(len(self.sink.events), 1)


if __name__ == "__main__":
    unittest.main()
