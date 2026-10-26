"""Tests for tools/a2a/netcaps_breaker.py (ORG-071 D1 per-anchor circuit
breaker) and tools/a2a/netcaps_failure_class.py (the D2 transient-vs-definitive
split at the fetcher boundary) — T-2857 PR5.

All timing runs on FakeClock; nothing sleeps and nothing touches the network.
"""

from __future__ import annotations

import socket
import ssl
import unittest

from tools.a2a.netcaps import (
    PEER_ABORT,
    AuditEventType,
    FakeClock,
    InMemoryAuditSink,
    NetcapsParams,
)
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_breaker import (
    CIRCUIT_OPEN_EVENT,
    CIRCUIT_OPEN_REASON,
    MAX_TRACKED_ANCHORS,
    AnchorCircuitBreaker,
    BreakerCardFetcher,
    BreakerState,
    BreakerTxtResolver,
    anchor_of_did,
    anchor_of_url,
    dns_key,
)
from tools.a2a.netcaps_failure_class import (
    FailureClass,
    TransientAnchorError,
    classify_fetch_failure,
)
from tools.a2a.passport_handshake import (
    BOUND_BREACH_EVENTS,
    AbortReason,
    HandshakeResult,
    HandshakeState,
    Outcome,
    TxtResolution,
)

P = NetcapsParams()
A = "your-domain.example"
B = "kin.example"
URL_A = f"https://{A}/.well-known/agent-card.json"
URL_B = f"https://{B}/.well-known/agent-card.json"


def _breaker(**kw):
    clock = FakeClock()
    sink = InMemoryAuditSink()
    br = AnchorCircuitBreaker(kw.pop("params", P), clock=clock, audit_sink=sink, **kw)
    return br, clock, sink


class _Fetcher:
    """Stub CardFetcher: raises `exc` while set, else returns `value`."""

    def __init__(self, value=b"card", exc=None):
        self.value, self.exc, self.calls = value, exc, 0

    def __call__(self, card_url):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.value


def _fail(fetcher, n, url=URL_A, exc=None):
    """Drive n transient failures through `fetcher` (a BreakerCardFetcher)."""
    for _ in range(n):
        try:
            fetcher(url)
        except (OSError, TransientAnchorError, NetcapsBreach):
            pass


class _OwnCard:
    """A BreakerCardFetcher whose card host is always the resolved DID's own
    anchor: it sets the context the paired BreakerTxtResolver would set."""

    def __init__(self, inner, br):
        self._f, self._br = BreakerCardFetcher(inner, br), br

    def __call__(self, card_url):
        self._br.set_endpoint_anchor(anchor_of_url(card_url))
        return self._f(card_url)


def _wrapped(br, **kw):
    inner = _Fetcher(**kw)
    return inner, _OwnCard(inner, br)


# ---------------------------------------------------------------------------


class TestClassification(unittest.TestCase):

    def test_network_errors_are_transient(self):
        for exc in (TimeoutError(), socket.timeout(), ConnectionRefusedError(),
                    ConnectionResetError(), socket.gaierror(-2, "nx"),
                    ssl.SSLError(), OSError("connect"), TransientAnchorError("429")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(classify_fetch_failure(exc), FailureClass.TRANSIENT_INFRA)

    def test_dnspython_timeout_matched_by_name(self):
        # dnspython is not installed here; a stand-in with its module/name (and a
        # subclass, as dns.resolver.LifetimeTimeout is) must classify transient.
        Timeout = type("Timeout", (Exception,), {"__module__": "dns.exception"})
        Lifetime = type("LifetimeTimeout", (Timeout,), {"__module__": "dns.resolver"})
        self.assertIs(classify_fetch_failure(Timeout()), FailureClass.TRANSIENT_INFRA)
        self.assertIs(classify_fetch_failure(Lifetime()), FailureClass.TRANSIENT_INFRA)
        Other = type("Timeout", (Exception,), {"__module__": "somewhere.else"})
        self.assertIs(classify_fetch_failure(Other()), FailureClass.UNKNOWN)

    def test_other_exceptions_are_unknown(self):
        for exc in (ValueError("bad doc"), KeyError("x"), RuntimeError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(classify_fetch_failure(exc), FailureClass.UNKNOWN)


class TestAnchorKey(unittest.TestCase):

    def test_url_and_did_share_the_host_key(self):
        self.assertEqual(anchor_of_url("https://your-domain.example/x"), A)
        self.assertEqual(anchor_of_did("did:web:your-domain.example"), A)
        self.assertEqual(anchor_of_did("did:web:your-domain.example:users:alice"), A)
        self.assertEqual(anchor_of_did("did:web:your-domain.example%3A8443"), "your-domain.example:8443")

    def test_key_normalisation(self):
        same = ("https://Kin.Example/x", "https://kin.example./x",
                "https://kin.example:443/x", "https://u@kin.example/x",
                "http://kin.example:80/x")
        for u in same:
            with self.subTest(u=u):
                self.assertEqual(anchor_of_url(u), "kin.example")
        self.assertEqual(anchor_of_url("https://kin.example:8443/"), "kin.example:8443")
        self.assertEqual(anchor_of_url("https://[::1]:8443/"), "[::1]:8443")
        self.assertEqual(anchor_of_url("https://[::1:8443]/"), "[::1:8443]")
        self.assertEqual(anchor_of_url("https://[0:0:0:0:0:0:0:1]/"), "[::1]")
        self.assertEqual(anchor_of_url("https://[::1]:443/"), "[::1]")
        self.assertEqual(anchor_of_url("https://192.0.2.1:443/"), "192.0.2.1")
        self.assertEqual(anchor_of_did("did:web:kin.example%3A443"), "kin.example")

    def test_unparseable_raises(self):
        for bad in ("", "not a url", "https://", "https://./x", "https://k..",
                    "https://%6Bin.example/x", "https://[fe80::1%25eth0]/"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                anchor_of_url(bad)
        with self.assertRaises(ValueError):
            anchor_of_did("did:key:z6Mk")


class TestBreachVocabulary(unittest.TestCase):
    """The NetcapsBreach strings must equal PR1's enums exactly (PR2's hook maps
    them into _breach(), which gives the peer only PEER_ABORT)."""

    def test_strings_match_pr1_enums(self):
        self.assertEqual(CIRCUIT_OPEN_REASON, AbortReason.CIRCUIT_OPEN.value)
        self.assertEqual(CIRCUIT_OPEN_EVENT, "circuit_breaker_open")
        reason = AbortReason(CIRCUIT_OPEN_REASON)
        self.assertEqual(BOUND_BREACH_EVENTS[reason].value, CIRCUIT_OPEN_EVENT)

    def test_breach_is_not_a_timeout(self):
        # The D3 deadline converts TimeoutError into StepTimeout; a breach must
        # not be mistaken for one, nor classify as transient itself.
        self.assertFalse(issubclass(NetcapsBreach, OSError))
        exc = NetcapsBreach(CIRCUIT_OPEN_REASON, CIRCUIT_OPEN_EVENT, "d")
        self.assertIs(classify_fetch_failure(exc), FailureClass.UNKNOWN)

    def test_peer_error_unchanged_for_circuit_open_abort(self):
        r = HandshakeResult(outcome=Outcome.ABORTED, state=HandshakeState.RESOLVE_ENDPOINT,
                            abort_reason=AbortReason.CIRCUIT_OPEN)
        self.assertEqual(r.peer_error, PEER_ABORT)


class TestStateMachine(unittest.TestCase):

    def test_threshold_trips(self):
        br, _, _ = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold - 1)
        self.assertIs(br.state(A), BreakerState.CLOSED)
        _fail(f, 1)
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertEqual(inner.calls, P.circuit_breaker_threshold)

    def test_open_rejects_without_calling_inner(self):
        br, clock, _ = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        inner.exc = None
        calls = inner.calls
        clock.advance(P.circuit_breaker_cooldown - 1)
        with self.assertRaises(NetcapsBreach) as cm:
            f(URL_A)
        self.assertEqual(inner.calls, calls)
        self.assertEqual(cm.exception.reason, CIRCUIT_OPEN_REASON)
        self.assertEqual(cm.exception.event, CIRCUIT_OPEN_EVENT)
        self.assertIn(A, cm.exception.detail)

    def test_success_resets_consecutive_count(self):
        br, _, _ = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold - 1)
        inner.exc = None
        self.assertEqual(f(URL_A), b"card")
        self.assertEqual(br.tracked(), 0)
        inner.exc = TimeoutError()
        _fail(f, P.circuit_breaker_threshold - 1)
        self.assertIs(br.state(A), BreakerState.CLOSED)

    def test_cooldown_then_half_open_probe_success_closes(self):
        br, clock, _ = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        inner.exc = None
        self.assertEqual(f(URL_A), b"card")  # the probe goes through
        self.assertIs(br.state(A), BreakerState.CLOSED)
        self.assertEqual(br.tracked(), 0)

    def test_single_probe_per_interval(self):
        br, clock, _ = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        br.acquire(A, URL_A)  # probe admitted (in flight)
        self.assertIs(br.state(A), BreakerState.HALF_OPEN)
        with self.assertRaises(NetcapsBreach):
            br.acquire(A, URL_A)  # a second request during the probe
        clock.advance(P.circuit_breaker_probe_interval - 1)
        with self.assertRaises(NetcapsBreach):
            br.acquire(A, URL_A)
        clock.advance(1)  # a probe that never reported: next slot opens
        br.acquire(A, URL_A)

    def test_probe_failure_reopens_until_next_interval(self):
        br, clock, sink = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        calls = inner.calls
        with self.assertRaises(TimeoutError):
            f(URL_A)  # the probe, failing transiently: re-raised unchanged
        self.assertEqual(inner.calls, calls + 1)
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertTrue(any("reopened" in e.detail for e in sink.events))
        clock.advance(P.circuit_breaker_probe_interval - 1)
        with self.assertRaises(NetcapsBreach):
            f(URL_A)
        self.assertEqual(inner.calls, calls + 1)
        clock.advance(1)
        inner.exc = None
        self.assertEqual(f(URL_A), b"card")
        self.assertIs(br.state(A), BreakerState.CLOSED)

    def test_straggler_failure_does_not_extend_open(self):
        # A request admitted before the trip that fails after it must neither
        # push the probe slot out nor emit a second open event.
        br, clock, sink = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        br.acquire(A, URL_A)  # in flight
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown - 1)
        br.record_failure(A, URL_A)  # the straggler reports
        self.assertEqual(len(sink.events), 1)
        clock.advance(1)
        br.acquire(A, URL_A)  # cooldown measured from the trip, not the straggler
        self.assertIs(br.state(A), BreakerState.HALF_OPEN)

    def test_definitive_results_never_count(self):
        br, _, _ = _breaker()
        # A None card (404 / fetch refusal) and a non-transient exception
        # (invalid doc) never trip, however many.
        inner, f = _wrapped(br, value=None)
        for _ in range(P.circuit_breaker_threshold * 3):
            self.assertIsNone(f(URL_A))
        inner.exc = ValueError("invalid doc")
        for _ in range(P.circuit_breaker_threshold * 3):
            with self.assertRaises(ValueError):
                f(URL_A)
        self.assertIs(br.state(A), BreakerState.CLOSED)
        self.assertEqual(br.tracked(), 0)

    def test_definitive_answer_during_probe_closes(self):
        br, clock, _ = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        inner.exc, inner.value = None, None  # the anchor answers 404
        self.assertIsNone(f(URL_A))
        self.assertIs(br.state(A), BreakerState.CLOSED)

    def test_per_anchor_isolation(self):
        br, _, _ = _breaker()
        inner, f = _wrapped(br, exc=ConnectionRefusedError())
        _fail(f, P.circuit_breaker_threshold, url=URL_A)
        inner.exc = None
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertIs(br.state(B), BreakerState.CLOSED)
        self.assertEqual(f(URL_B), b"card")

    def test_keyed_by_host_not_did(self):
        # Two DIDs on one anchor share its DNS-channel breaker.
        br, _, _ = _breaker()
        calls = []

        def resolver(endpoint_id):
            calls.append(endpoint_id)
            raise TimeoutError()

        r = BreakerTxtResolver(resolver, br)
        for i in range(P.circuit_breaker_threshold):
            with self.assertRaises(TimeoutError):
                r(f"did:web:{A}:users:u{i}")
        self.assertIs(br.state(dns_key(A)), BreakerState.OPEN)
        with self.assertRaises(NetcapsBreach):
            r("did:web:your-domain.example")
        self.assertEqual(len(calls), P.circuit_breaker_threshold)

    def test_dns_and_https_channels_are_separate(self):
        # A dead DNS channel does not gate the card fetch, and a working TXT
        # answer does not reset the count of a dead web server.
        br, _, _ = _breaker()
        r = BreakerTxtResolver(lambda e: (_ for _ in ()).throw(TimeoutError()), br)
        for _ in range(P.circuit_breaker_threshold):
            with self.assertRaises(TimeoutError):
                r(f"did:web:{A}")
        self.assertIs(br.state(A), BreakerState.CLOSED)
        self.assertEqual(BreakerCardFetcher(_Fetcher(), br)(URL_A), b"card")

        br, _, _ = _breaker()
        ok = BreakerTxtResolver(lambda e: TxtResolution(
            present=True, dnssec_validated=True, card_url=URL_A), br)
        fetch = BreakerCardFetcher(_Fetcher(exc=TimeoutError()), br)
        for _ in range(P.circuit_breaker_threshold):
            ok(f"did:web:{A}")
            with self.assertRaises(TimeoutError):
                fetch(URL_A)
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertEqual(br.state(dns_key(A)), BreakerState.CLOSED)

    def test_dns_key_cannot_collide_with_url_key(self):
        self.assertNotIn("/", anchor_of_url("https://dns:8443/x"))
        self.assertNotEqual(dns_key("8443"), anchor_of_url("https://dns:8443/x"))


class TestCrossKin(unittest.TestCase):
    """A kin whose TXT card_url names another kin's host cannot trip that
    host's breaker."""

    def _pair(self, br, card_exc):
        card = _Fetcher(exc=card_exc)
        txt = TxtResolution(present=True, dnssec_validated=True,
                            card_url=URL_B, pinned_card_hash="sha256:x")
        return card, BreakerTxtResolver(lambda e: txt, br), BreakerCardFetcher(card, br)

    def test_foreign_card_host_failures_not_counted(self):
        br, _, sink = _breaker()
        card, resolve, fetch = self._pair(br, TimeoutError())
        for _ in range(P.circuit_breaker_threshold * 3):
            t = resolve(f"did:web:{A}")  # hostile kin A points at B's host
            with self.assertRaises(TimeoutError):
                fetch(t.card_url)
        self.assertIs(br.state(B), BreakerState.CLOSED)
        self.assertEqual(br.tracked(), 0)
        self.assertEqual(sink.events, [])

    def test_own_card_host_failures_counted(self):
        br, _, _ = _breaker()
        card, _, fetch = self._pair(br, TimeoutError())
        resolve = BreakerTxtResolver(lambda e: None, br)
        for _ in range(P.circuit_breaker_threshold):  # TXT answers each time
            resolve(f"did:web:{B}")
            with self.assertRaises(TimeoutError):
                fetch(URL_B)
        self.assertIs(br.state(B), BreakerState.OPEN)

    def test_open_foreign_host_still_gates_the_fetch(self):
        br, _, _ = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold, url=URL_B)  # B genuinely down
        card, resolve, fetch = self._pair(br, None)
        resolve(f"did:web:{A}")
        with self.assertRaises(NetcapsBreach):
            fetch(URL_B)
        self.assertEqual(card.calls, 0)

    def test_unkeyable_resolve_clears_stale_context(self):
        br, _, _ = _breaker()
        resolve = BreakerTxtResolver(lambda e: None, br)
        fetch = BreakerCardFetcher(_Fetcher(exc=TimeoutError()), br)
        resolve(f"did:web:{A}")
        with self.assertRaises(NetcapsBreach):
            resolve("not-a-did")
        _fail(fetch, P.circuit_breaker_threshold * 2)
        self.assertEqual(br.tracked(), 0)

    def test_no_resolver_context_counts_nothing(self):
        br, _, _ = _breaker()
        fetch = BreakerCardFetcher(_Fetcher(exc=TimeoutError()), br)
        _fail(fetch, P.circuit_breaker_threshold * 2)
        self.assertEqual(br.tracked(), 0)


class TestConcurrency(unittest.TestCase):

    def test_fifty_threads_one_probe(self):
        # Reviewer repro: past cooldown, 50 concurrent callers -> exactly one
        # reaches the anchor. No sleeps: the probe blocks on an Event until the
        # other 49 have been refused (bounded waits so a bug fails, not hangs).
        import threading
        br, clock, _ = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        hits, refused, lock = [], [], threading.Lock()
        all_refused, release = threading.Event(), threading.Event()

        def inner():
            with lock:
                hits.append(1)
            release.wait(5)
            raise TimeoutError()

        def worker():
            try:
                br.call(A, URL_A, inner)
            except NetcapsBreach:
                with lock:
                    refused.append(1)
                    if len(refused) == 49:
                        all_refused.set()
            except TimeoutError:
                pass

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        self.assertTrue(all_refused.wait(5))
        self.assertEqual(len(hits), 1)
        release.set()
        for t in threads:
            t.join(5)
        self.assertIs(br.state(A), BreakerState.OPEN)  # the probe failed


class TestBoundedMemory(unittest.TestCase):

    def test_healthy_anchors_hold_no_entry(self):
        br, _, _ = _breaker(max_anchors=2)
        _, f = _wrapped(br)
        for i in range(50):
            f(f"https://h{i}.example/card")
        self.assertEqual(br.tracked(), 0)

    def test_closed_entries_evicted_before_open(self):
        br, _, _ = _breaker(max_anchors=3)
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold, url=URL_A)  # A open
        _fail(f, 1, url="https://c1.example/x")           # closed, 1 failure
        _fail(f, 1, url="https://c2.example/x")           # closed, 1 failure
        _fail(f, 1, url="https://c3.example/x")           # evicts c1 (oldest closed)
        self.assertEqual(br.tracked(), 3)
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertIs(br.state("c1.example"), BreakerState.CLOSED)
        for i in range(20):
            _fail(f, 1, url=f"https://n{i}.example/x")
        self.assertEqual(br.tracked(), 3)
        self.assertIs(br.state(A), BreakerState.OPEN)

    def test_table_full_of_open_admits_new_anchor(self):
        # A healthy anchor is never refused for lack of room: not after the
        # cooldowns pass, and not while every slot is still open.
        for advance in (0, P.circuit_breaker_cooldown, 10**6):
            with self.subTest(advance=advance):
                br, clock, sink = _breaker(max_anchors=2)
                inner, f = _wrapped(br, exc=TimeoutError())
                _fail(f, P.circuit_breaker_threshold, url=URL_A)
                _fail(f, P.circuit_breaker_threshold, url=URL_B)
                clock.advance(advance)
                inner.exc = None
                self.assertEqual(f("https://healthy-kin.example/card"), b"card")
                self.assertEqual(br.tracked(), 2)
                self.assertEqual(len(sink.events), 2)  # only the two opens

    def test_full_table_evicts_open_past_probe_slot_before_oldest(self):
        br, clock, _ = _breaker(max_anchors=2)
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold, url=URL_B)  # t=0, slot at 60
        clock.advance(50)
        _fail(f, P.circuit_breaker_threshold, url=URL_A)  # t=50, slot at 110
        _fail(f, 1, url=URL_B)  # refused, but touches B: A is now the oldest
        clock.advance(20)       # t=70: B's slot passed, A's not
        _fail(f, 1, url="https://new.example/x")
        self.assertIs(br.state(B), BreakerState.CLOSED)  # expired: evicted
        self.assertIs(br.state(A), BreakerState.OPEN)    # oldest, but kept

    def test_full_table_evicts_closed_before_expired_open(self):
        br, clock, _ = _breaker(max_anchors=2)
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold, url=URL_A)
        clock.advance(P.circuit_breaker_cooldown)  # A's slot passed, A oldest
        _fail(f, 1, url="https://c1.example/x")    # closed, newer
        _fail(f, 1, url="https://new.example/x")
        self.assertIs(br.state(A), BreakerState.OPEN)
        self.assertEqual(br.tracked(), 2)
        _fail(f, 1, url="https://c1.example/x")    # c1 was evicted: count restarts
        self.assertIs(br.state("c1.example"), BreakerState.CLOSED)

    def test_full_table_of_unexpired_open_evicts_oldest(self):
        br, _, _ = _breaker(max_anchors=2)
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold, url=URL_A)
        _fail(f, P.circuit_breaker_threshold, url=URL_B)
        _fail(f, 1, url="https://new.example/x")
        self.assertIs(br.state(A), BreakerState.CLOSED)  # oldest, evicted
        self.assertIs(br.state(B), BreakerState.OPEN)
        self.assertEqual(br.tracked(), 2)

    def test_max_anchors_validated(self):
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AnchorCircuitBreaker(max_anchors=bad)
        self.assertGreater(MAX_TRACKED_ANCHORS, 0)


class TestAudit(unittest.TestCase):

    def test_open_transition_emits_refusals_do_not(self):
        # One record per abort: the handshake hook (PR2 _breach) emits each
        # refusal; the breaker emits only its own open/reopen transitions.
        br, clock, sink = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        clock.advance(7)
        _fail(f, P.circuit_breaker_threshold)
        self.assertEqual(len(sink.events), 1)
        trip = sink.events[0]
        self.assertIs(trip.type, AuditEventType.CIRCUIT_BREAKER_OPEN)
        self.assertEqual(trip.reason, AbortReason.CIRCUIT_OPEN.value)
        self.assertEqual(trip.endpoint_id, URL_A)
        self.assertIn(f"anchor={A}", trip.detail)
        self.assertIn("opened", trip.detail)
        self.assertEqual(trip.at, 7)
        for _ in range(3):
            with self.assertRaises(NetcapsBreach):
                f(URL_A)
        self.assertEqual(len(sink.events), 1)

    def test_reopen_transition_emits_once(self):
        br, clock, sink = _breaker()
        _, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold)
        clock.advance(P.circuit_breaker_cooldown)
        _fail(f, 1)  # failed probe -> reopen
        _fail(f, 3)  # refusals
        self.assertEqual([("reopened" in e.detail) for e in sink.events],
                         [False, True])
        self.assertTrue(all(e.type is AuditEventType.CIRCUIT_BREAKER_OPEN
                            for e in sink.events))

    def test_closed_traffic_emits_nothing(self):
        br, _, sink = _breaker()
        inner, f = _wrapped(br, exc=TimeoutError())
        _fail(f, P.circuit_breaker_threshold - 1)
        inner.exc = None
        f(URL_A)
        self.assertEqual(sink.events, [])

    def test_unkeyable_request_refused_without_emit(self):
        br, _, sink = _breaker()
        calls = []
        r = BreakerTxtResolver(lambda e: calls.append(e) or TxtResolution(
            present=True, dnssec_validated=True), br)
        with self.assertRaises(NetcapsBreach) as cm:
            r("not-a-did")
        self.assertEqual(cm.exception.reason, CIRCUIT_OPEN_REASON)
        self.assertIn("no anchor host", cm.exception.detail)
        f = BreakerCardFetcher(_Fetcher(), br)
        with self.assertRaises(NetcapsBreach):
            f("not a url")
        self.assertEqual(calls, [])
        self.assertEqual(sink.events, [])


class TestTransparency(unittest.TestCase):

    def test_wrappers_return_and_raise_exactly_what_inner_did(self):
        br, _, _ = _breaker()
        txt = TxtResolution(present=True, dnssec_validated=True,
                            card_url=URL_A, pinned_card_hash="sha256:x")
        r = BreakerTxtResolver(lambda e: txt, br)
        self.assertIs(r("did:web:your-domain.example"), txt)
        exc = socket.gaierror(-2, "nx")
        _, f = _wrapped(br, exc=exc)
        with self.assertRaises(socket.gaierror) as cm:
            f(URL_A)
        self.assertIs(cm.exception, exc)

    def test_custom_params_honoured(self):
        params = NetcapsParams(circuit_breaker_threshold=1,
                               circuit_breaker_cooldown=10,
                               circuit_breaker_probe_interval=2)
        br, clock, _ = _breaker(params=params)
        _, f = _wrapped(br, exc=TransientAnchorError("429"))
        _fail(f, 1)
        self.assertIs(br.state(A), BreakerState.OPEN)
        clock.advance(10)
        _fail(f, 1)  # probe fails
        clock.advance(2)
        br.acquire(A, URL_A)
        self.assertIs(br.state(A), BreakerState.HALF_OPEN)


class T5399cContextVarAnchor(unittest.TestCase):
    def test_interleaved_tasks_keep_separate_anchors(self):
        import asyncio
        br, _, _ = _breaker()

        async def task(anchor, ready, go):
            br.set_endpoint_anchor(None)
            br.set_endpoint_anchor(anchor)
            ready.append(anchor)
            while len(ready) < 2:
                await asyncio.sleep(0)     # both have set before either reads
            await go.wait()
            return br.endpoint_anchor()

        async def main():
            ready, go = [], asyncio.Event()
            ts = [asyncio.ensure_future(task(a, ready, go)) for a in ("a.example", "b.example")]
            while len(ready) < 2:
                await asyncio.sleep(0)
            go.set()
            return await asyncio.gather(*ts)

        self.assertEqual(asyncio.run(main()), ["a.example", "b.example"])

    def test_copy_context_runs_are_isolated_and_default_none(self):
        import contextvars
        br, _, _ = _breaker()
        ctx = contextvars.copy_context()
        ctx.run(br.set_endpoint_anchor, "x.example")
        self.assertEqual(ctx.run(br.endpoint_anchor), "x.example")
        self.assertIsNone(br.endpoint_anchor())

    def test_thread_isolation_still_holds(self):
        import threading
        br, _, _ = _breaker()
        br.set_endpoint_anchor("main.example")
        seen = []
        t = threading.Thread(target=lambda: seen.append(br.endpoint_anchor()))
        t.start()
        t.join(5)
        self.assertEqual(seen, [None])
        self.assertEqual(br.endpoint_anchor(), "main.example")


class T5399cKeyVariants(unittest.TestCase):
    def k(self, host):
        return anchor_of_url(f"https://{host}/x")

    def test_unicode_host_and_a_label_key_identically(self):
        self.assertEqual(self.k("bücher.example"), "xn--bcher-kva.example")
        self.assertEqual(self.k("xn--bcher-kva.example"), "xn--bcher-kva.example")
        self.assertEqual(self.k("BÜCHER.Example"), "xn--bcher-kva.example")
        self.assertEqual(self.k("bücher.example."), "xn--bcher-kva.example")

    def test_hostile_hosts_refused(self):
        bad = [
            "xn--a-", "xn--zz", "xn--", "xn--bcher-kvb.example",
            "ß.example", "faß.de", "ＥＸＡＭＰＬＥ.com",          # IDNA2003 maps beyond case
            "１２７.０.０.１", "ａ.example",
            "a\u200db.example", "ς.example",                    # ZWJ, final sigma
            "a..b.example", "a" * 64 + ".example", "bü\u0000cher.example",
        ]
        for h in bad:
            with self.subTest(host=h), self.assertRaises(ValueError):
                self.k(h)

    def test_shorthand_and_legacy_ipv4_refused(self):
        for h in ("2130706433", "0x7f.0.0.1", "0x7f000001", "127.1", "127.0.1",
                  "0177.0.0.1", "0177.0.0.01", "1.2.3", "0x7f.1", "017700000001",
                  "01.2.3.4", "1.2.3.0x4", "4294967295", "0X7F.0.0.1"):
            with self.subTest(host=h), self.assertRaises(ValueError):
                self.k(h)

    def test_canonical_ipv4_and_numeric_looking_names_still_key(self):
        self.assertEqual(self.k("127.0.0.1"), "127.0.0.1")
        self.assertEqual(self.k("1.example"), "1.example")
        self.assertEqual(self.k("0x7f.example"), "0x7f.example")

    def test_ipv4_mapped_ipv6_keys_as_ipv4(self):
        self.assertEqual(self.k("[::ffff:1.2.3.4]"), "1.2.3.4")
        self.assertEqual(self.k("[::ffff:102:304]"), "1.2.3.4")
        self.assertEqual(self.k("[::FFFF:1.2.3.4]"), "1.2.3.4")
        self.assertEqual(anchor_of_url("https://[::ffff:1.2.3.4]:8443/"), "1.2.3.4:8443")
        self.assertEqual(self.k("[::1]"), "[::1]")

    def test_keys_are_idempotent_and_equivalent_spellings_agree(self):
        spellings = [("bücher.example", "xn--bcher-kva.example", "BÜCHER.EXAMPLE."),
                     ("[::ffff:1.2.3.4]", "1.2.3.4", "[0:0:0:0:0:ffff:102:304]"),
                     ("Kin.Example", "kin.example.", "kin.example")]
        for group in spellings:
            keys = {self.k(h) for h in group}
            with self.subTest(group=group):
                self.assertEqual(len(keys), 1)
                key = keys.pop()
                self.assertEqual(anchor_of_url(f"https://{key}/"), key)


class T5399cRaisingSink(unittest.TestCase):
    class _Boom:
        def emit(self, event):
            raise RuntimeError("sink down")

    def _br(self):
        clock = FakeClock()
        return AnchorCircuitBreaker(P, clock=clock, audit_sink=self._Boom()), clock

    def test_trip_survives_a_raising_sink(self):
        br, _ = self._br()
        f = BreakerCardFetcher(_Fetcher(exc=TimeoutError()), br)
        br.set_endpoint_anchor(A)
        with self.assertLogs("pantheon.a2a.netcaps.breaker", level="ERROR") as logs:
            for _ in range(P.circuit_breaker_threshold):
                with self.assertRaises(TimeoutError):   # the original, not RuntimeError
                    f(URL_A)
        self.assertTrue(any("audit" in m.lower() for m in logs.output))
        self.assertIs(br.state(A), BreakerState.OPEN)
        with self.assertRaises(NetcapsBreach) as cm:
            f(URL_A)
        self.assertEqual(cm.exception.reason, CIRCUIT_OPEN_REASON)

    def test_reopen_survives_a_raising_sink(self):
        br, clock = self._br()
        f = BreakerCardFetcher(_Fetcher(exc=TimeoutError()), br)
        br.set_endpoint_anchor(A)
        with self.assertLogs("pantheon.a2a.netcaps.breaker", level="ERROR"):
            for _ in range(P.circuit_breaker_threshold):
                with self.assertRaises(TimeoutError):
                    f(URL_A)
            clock.advance(P.circuit_breaker_cooldown)
            with self.assertRaises(TimeoutError):       # the probe fails
                f(URL_A)
        self.assertIs(br.state(A), BreakerState.OPEN)



if __name__ == "__main__":
    unittest.main()
