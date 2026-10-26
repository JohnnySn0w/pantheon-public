"""Tests for tools/a2a/netcaps_cache.py — the ORG-071 D2 resolution + status
cache (T-2857 PR3).

Hermetic: a FakeClock, a seeded random.Random, fake fetchers that count calls,
no network and no sleeps. The singleflight test uses real threads but
coordinates them with Events, never time.
"""

from __future__ import annotations

import gzip
import json
import random
import socket
import threading
import unittest

from tools.a2a.did_web import ParsedDidDocument
from tools.a2a.netcaps import AuditEventType, FakeClock, NetcapsParams
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_cache import (
    CacheBreach,
    FailureClass,
    FetchFailure,
    FetchResponse,
    ResolutionCache,
    bounded_gunzip,
    max_age_from_cache_control,
)
from tools.a2a.netcaps_failure_class import TransientAnchorError
from tools.a2a.netcaps_timeouts import StepTimeout, TimeoutPolicy, TimeoutStep
from tools.a2a.passport_handshake import AbortReason

DID_A = "did:web:a.example"
DID_B = "did:web:b.example"
URL_A = "https://a.example/.well-known/did.json"
STATUS_URL = "https://issuer.example/status/1"
ISSUER = "did:web:issuer.example"

# Jitter off unless a test is about jitter, so expiry instants are exact.
P = NetcapsParams(jitter_range_pct=0)


def did_doc(did: str) -> bytes:
    kid = f"{did}#k1"
    return json.dumps({
        "id": did,
        "verificationMethod": [{
            "id": kid, "type": "JsonWebKey2020", "controller": did,
            "publicKeyJwk": {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"},
        }],
        "authentication": [kid],
    }).encode()


class FakeFetcher:
    """Answers from a url -> (body | FetchFailure | callable) script; counts calls."""

    def __init__(self, responses=None, *, max_age=None):
        self.responses = dict(responses or {})
        self.max_age = max_age
        self.calls: list[str] = []
        self.max_bytes_seen: list[int] = []
        self.timeouts_seen: list[float] = []

    def __call__(self, url: str, *, max_bytes: int, timeout: float) -> FetchResponse:
        self.calls.append(url)
        self.max_bytes_seen.append(max_bytes)
        self.timeouts_seen.append(timeout)
        r = self.responses[url]
        if callable(r):
            r = r()
        if isinstance(r, FetchFailure):
            raise r
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, FetchResponse):
            return r
        return FetchResponse(body=r, max_age=self.max_age)


def make(params=P, *, seed=0, max_entries=1024):
    clock = FakeClock()
    cache = ResolutionCache(params, clock=clock, rng=random.Random(seed),
                            max_entries=max_entries)
    return cache, clock


def transient(detail="dns servfail"):
    return FetchFailure(FailureClass.TRANSIENT_INFRA, detail)


def absent(detail="404"):
    return FetchFailure(FailureClass.DEFINITIVE_ABSENT, detail)


class CacheControlParsing(unittest.TestCase):
    def test_max_age(self):
        self.assertEqual(max_age_from_cache_control("public, max-age=120"), 120)

    def test_no_store_and_no_cache_mean_zero(self):
        self.assertEqual(max_age_from_cache_control("no-store"), 0)
        self.assertEqual(max_age_from_cache_control("max-age=60, no-cache"), 0)

    def test_absent_or_malformed_is_none(self):
        self.assertIsNone(max_age_from_cache_control(None))
        self.assertIsNone(max_age_from_cache_control("public"))
        self.assertIsNone(max_age_from_cache_control("max-age=soon"))
        self.assertIsNone(max_age_from_cache_control("max-age=-5"))


class PositiveCaching(unittest.TestCase):
    def test_hit_within_ttl_no_refetch(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        resolve = cache.did_documents(f)
        doc = resolve(DID_A)
        self.assertIsInstance(doc, ParsedDidDocument)
        self.assertEqual(doc.did, DID_A)
        clock.advance(P.did_doc_positive_ttl_max - 1)
        self.assertIs(resolve(DID_A), doc)
        self.assertEqual(len(f.calls), 1)

    def test_local_max_is_the_ceiling(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        resolve = cache.did_documents(f)
        resolve(DID_A)
        clock.advance(P.did_doc_positive_ttl_max)
        resolve(DID_A)
        self.assertEqual(len(f.calls), 2)

    def test_cache_control_above_local_max_is_clamped(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)}, max_age=86400)
        resolve = cache.did_documents(f)
        resolve(DID_A)
        clock.advance(P.did_doc_positive_ttl_max)
        resolve(DID_A)
        self.assertEqual(len(f.calls), 2, "Cache-Control must never extend the TTL")

    def test_cache_control_below_local_max_shortens(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)}, max_age=30)
        resolve = cache.did_documents(f)
        resolve(DID_A)
        clock.advance(29)
        resolve(DID_A)
        self.assertEqual(len(f.calls), 1)
        clock.advance(1)
        resolve(DID_A)
        self.assertEqual(len(f.calls), 2)

    def test_max_age_zero_is_served_but_not_cached(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)}, max_age=0)
        resolve = cache.did_documents(f)
        resolve(DID_A)
        resolve(DID_A)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(len(cache), 0)

    def test_status_list_uses_its_own_ttl(self):
        params = NetcapsParams(jitter_range_pct=0, status_list_positive_ttl_max=100)
        cache, clock = make(params)
        f = FakeFetcher({STATUS_URL: b"list"})
        resolve = cache.status_lists(f, lambda body, issuer, url: body)
        self.assertEqual(resolve(ISSUER, STATUS_URL), b"list")
        clock.advance(99)
        resolve(ISSUER, STATUS_URL)
        self.assertEqual(len(f.calls), 1)
        clock.advance(1)
        resolve(ISSUER, STATUS_URL)
        self.assertEqual(len(f.calls), 2)

    def test_fetcher_is_told_the_size_cap(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: did_doc(DID_A), STATUS_URL: b"x"})
        cache.did_documents(f)(DID_A)
        cache.status_lists(f, lambda b, i, u: b)(ISSUER, STATUS_URL)
        self.assertEqual(f.max_bytes_seen,
                         [P.max_did_doc_bytes, P.max_status_list_bytes])


class StrictStaleOnError(unittest.TestCase):
    def test_expired_entry_never_served_on_refresh_failure(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        resolve = cache.did_documents(f)
        resolve(DID_A)
        clock.advance(P.did_doc_positive_ttl_max)
        f.responses[URL_A] = transient()
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A)
        self.assertEqual(cm.exception.reason, AbortReason.STALE_DENIED.value)
        self.assertEqual(cm.exception.event, AuditEventType.STALE_DENIED.value)

    def test_expired_status_list_on_failure_is_stale_denied(self):
        cache, clock = make()
        f = FakeFetcher({STATUS_URL: b"list"})
        resolve = cache.status_lists(f, lambda b, i, u: b)
        resolve(ISSUER, STATUS_URL)
        clock.advance(P.status_list_positive_ttl_max)
        f.responses[STATUS_URL] = transient()
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL)
        self.assertEqual(cm.exception.reason, AbortReason.STALE_DENIED.value)

    def test_status_list_first_fetch_failure_is_status_fetch_failed(self):
        cache, _ = make()
        f = FakeFetcher({STATUS_URL: transient()})
        with self.assertRaises(CacheBreach) as cm:
            cache.status_lists(f, lambda b, i, u: b)(ISSUER, STATUS_URL)
        self.assertEqual(cm.exception.reason, AbortReason.STATUS_FETCH_FAILED.value)
        self.assertEqual(cm.exception.event,
                         AuditEventType.STATUS_FETCH_FAILED.value)

    def test_did_doc_first_fetch_failure_aborts(self):
        cache, _ = make()
        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: transient()}))(DID_A)
        self.assertEqual(cm.exception.reason, AbortReason.DID_RESOLUTION_FAILED.value)
        self.assertEqual(cm.exception.event,
                         AuditEventType.DID_RESOLUTION_FAILED.value)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)

    def test_unresolvable_did_reports_did_resolution_failed(self):
        cache, _ = make()
        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({}))("not-a-did")
        self.assertEqual(cm.exception.reason, AbortReason.DID_RESOLUTION_FAILED.value)
        self.assertEqual(cm.exception.event,
                         AuditEventType.DID_RESOLUTION_FAILED.value)

    def test_oserror_and_anchor_throttle_are_transient(self):
        for exc in (OSError("boom"), TransientAnchorError("429")):
            cache, _ = make()
            with self.assertRaises(CacheBreach) as cm:
                cache.did_documents(FakeFetcher({URL_A: exc}))(DID_A)
            self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
            self.assertEqual(cm.exception.event,
                             AuditEventType.DID_RESOLUTION_FAILED.value)

    def test_unclassified_exception_aborts_and_caches_nothing(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: RuntimeError("fetcher bug")})
        resolve = cache.did_documents(f)
        for _ in range(2):
            with self.assertRaises(CacheBreach) as cm:
                resolve(DID_A)
            self.assertNotEqual(cm.exception.reason,
                                AbortReason.NEGATIVE_CACHE_HIT.value)
            self.assertIs(cm.exception.failure_class, FailureClass.UNKNOWN)
        self.assertEqual(len(f.calls), 2, "no negative entry, no suppression")
        self.assertEqual(len(cache), 0)

    def test_timeout_error_is_transient(self):
        cache, _ = make()
        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: TimeoutError()}))(DID_A)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)


class DefinitiveAbsentNegative(unittest.TestCase):
    def test_404_cached_negatively_for_class_ttl(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: absent()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach) as first:
            resolve(DID_A)
        self.assertIs(first.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)
        clock.advance(P.negative_ttl_definitive_absent - 1)
        with self.assertRaises(CacheBreach) as hit:
            resolve(DID_A)
        self.assertEqual(hit.exception.reason, AbortReason.NEGATIVE_CACHE_HIT.value)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(hit.exception.event, AuditEventType.NEGATIVE_CACHE_HIT.value)
        self.assertIsNone(hit.exception.failure_class)
        clock.advance(1)
        with self.assertRaises(CacheBreach) as again:
            resolve(DID_A)
        self.assertNotEqual(again.exception.reason, AbortReason.NEGATIVE_CACHE_HIT.value)
        self.assertEqual(len(f.calls), 2)

    def test_negative_hit_applies_to_every_source(self):
        cache, _ = make()
        resolve = cache.did_documents(FakeFetcher({URL_A: absent()}))
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="peer-1")
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A, source="peer-2")
        self.assertEqual(cm.exception.reason, AbortReason.NEGATIVE_CACHE_HIT.value)

    def test_negative_is_scoped_to_the_did(self):
        cache, _ = make()
        url_b = "https://b.example/.well-known/did.json"
        f = FakeFetcher({URL_A: absent(), url_b: did_doc(DID_B)})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A)
        self.assertEqual(resolve(DID_B).did, DID_B)

    def test_malformed_doc_is_definitive(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: b"{not json"})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A)
        self.assertIs(cm.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)
        with self.assertRaises(CacheBreach) as hit:
            resolve(DID_A)
        self.assertEqual(hit.exception.reason, AbortReason.NEGATIVE_CACHE_HIT.value)

    def test_status_validator_rejection_is_definitive(self):
        cache, _ = make()

        def bad_sig(body, issuer, url):
            raise ValueError("bad signature")

        resolve = cache.status_lists(FakeFetcher({STATUS_URL: b"x"}), bad_sig)
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL)
        self.assertIs(cm.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)
        self.assertEqual(cm.exception.reason, AbortReason.STATUS_FETCH_FAILED.value)

    def test_oversized_body_is_definitive_and_never_parsed(self):
        params = NetcapsParams(jitter_range_pct=0, max_did_doc_bytes=10)
        cache, _ = make(params)
        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: did_doc(DID_A)}))(DID_A)
        self.assertIs(cm.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)
        self.assertIn("max_did_doc_bytes", str(cm.exception))


class TransientInfraSuppression(unittest.TestCase):
    def test_suppresses_refetch_for_window_then_retries(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: transient()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="s1")
        clock.advance(P.negative_ttl_transient_infra - 1)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A, source="s1")
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
        self.assertEqual(len(f.calls), 1, "suppressed: no refetch inside the window")
        clock.advance(1)
        f.responses[URL_A] = did_doc(DID_A)
        self.assertEqual(resolve(DID_A, source="s1").did, DID_A)
        self.assertEqual(len(f.calls), 2)

    def test_never_reports_negative_cache_hit(self):
        cache, _ = make()
        resolve = cache.did_documents(FakeFetcher({URL_A: transient()}))
        for _ in range(3):
            with self.assertRaises(CacheBreach) as cm:
                resolve(DID_A, source="s1")
            self.assertNotEqual(cm.exception.event,
                                AuditEventType.NEGATIVE_CACHE_HIT.value)

    def test_keyed_by_source_not_shared_across_sources(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: transient()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="attacker")
        f.responses[URL_A] = did_doc(DID_A)
        self.assertEqual(resolve(DID_A, source="kin").did, DID_A)

    def test_keyed_by_anchor_host_not_by_counterparty(self):
        # Two DIDs on the same anchor host share the suppression (it is about
        # the host being down); a DID on another host is unaffected.
        cache, _ = make()
        did_a2 = "did:web:a.example:users:two"
        url_a2 = "https://a.example/users/two/did.json"
        url_b = "https://b.example/.well-known/did.json"
        f = FakeFetcher({URL_A: transient(), url_a2: did_doc(did_a2),
                         url_b: did_doc(DID_B)})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="s1")
        with self.assertRaises(CacheBreach):
            resolve(did_a2, source="s1")
        self.assertNotIn(url_a2, f.calls)
        self.assertEqual(resolve(DID_B, source="s1").did, DID_B)

    def test_fresh_positive_is_served_despite_suppression(self):
        # Suppression is advisory: it stops FETCHES, it never denies a handshake
        # that has fresh validated material.
        cache, clock = make()
        url_a2 = "https://a.example/users/two/did.json"
        did_a2 = "did:web:a.example:users:two"
        f = FakeFetcher({URL_A: did_doc(DID_A), url_a2: transient()})
        resolve = cache.did_documents(f)
        resolve(DID_A, source="s1")
        with self.assertRaises(CacheBreach):
            resolve(did_a2, source="s1")   # anchor a.example now suppressed for s1
        self.assertEqual(resolve(DID_A, source="s1").did, DID_A)


class NegativeNeverOverridesSuccess(unittest.TestCase):
    def test_success_clears_definitive_negative(self):
        cache, clock = make()
        f = FakeFetcher({URL_A: absent()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A)
        clock.advance(P.negative_ttl_definitive_absent)
        f.responses[URL_A] = did_doc(DID_A)
        resolve(DID_A)
        f.responses[URL_A] = absent()
        clock.advance(1)
        self.assertEqual(resolve(DID_A).did, DID_A)

    def test_success_by_other_source_served_to_suppressed_source(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: transient()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="s1")
        f.responses[URL_A] = did_doc(DID_A)
        resolve(DID_A, source="s2")
        self.assertEqual(resolve(DID_A, source="s1").did, DID_A)
        self.assertEqual(len(f.calls), 2)

    def test_success_clears_own_transient_suppression(self):
        cache, clock = make()
        did_a2 = "did:web:a.example:users:two"
        url_a2 = "https://a.example/users/two/did.json"
        f = FakeFetcher({URL_A: transient(), url_a2: did_doc(did_a2)})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A, source="s1")
        resolve(did_a2, source="s2")           # s2 not suppressed
        clock.advance(P.negative_ttl_transient_infra)
        resolve(did_a2, source="s1")           # window over, s1 retries ok
        f.responses[URL_A] = did_doc(DID_A)
        self.assertEqual(resolve(DID_A, source="s1").did, DID_A)


class AntiPoisoning(unittest.TestCase):
    def test_doc_for_another_did_is_rejected_and_not_cached(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: did_doc(DID_B)})   # anchor serves B's doc at A
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A)
        self.assertIs(cm.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)
        self.assertFalse(any(DID_B in k for k in cache.keys()))

    def test_status_list_bound_to_issuer(self):
        cache, _ = make()
        f = FakeFetcher({STATUS_URL: b"list"})
        seen = []

        def validate(body, issuer, url):
            seen.append(issuer)
            return (issuer, body)

        resolve = cache.status_lists(f, validate)
        self.assertEqual(resolve(ISSUER, STATUS_URL), (ISSUER, b"list"))
        other = "did:web:evil.example"
        self.assertEqual(resolve(other, STATUS_URL), (other, b"list"))
        self.assertEqual(seen, [ISSUER, other],
                         "same URL under another issuer must re-validate")
        self.assertEqual(len(f.calls), 2)

    def test_did_and_status_namespaces_are_separate(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        cache.did_documents(f)(DID_A)
        status = cache.status_lists(f, lambda b, i, u: "status")
        self.assertEqual(status(DID_A, URL_A), "status")
        self.assertEqual(len(f.calls), 2)

    def test_non_https_status_url_refused_without_fetch(self):
        cache, _ = make()
        f = FakeFetcher({})
        with self.assertRaises(CacheBreach) as cm:
            cache.status_lists(f, lambda b, i, u: b)(ISSUER, "http://x/s")
        self.assertEqual(cm.exception.reason, AbortReason.STATUS_FETCH_FAILED.value)
        self.assertEqual(f.calls, [])

    def test_invalid_did_refused_without_fetch(self):
        cache, _ = make()
        f = FakeFetcher({})
        with self.assertRaises(CacheBreach):
            cache.did_documents(f)("did:key:z6Mk")
        self.assertEqual(f.calls, [])


class Singleflight(unittest.TestCase):
    def test_concurrent_requests_one_fetch(self):
        cache, _ = make()
        release = threading.Event()
        entered = threading.Event()

        def slow():
            entered.set()
            release.wait()
            return did_doc(DID_A)

        f = FakeFetcher({URL_A: slow})
        resolve = cache.did_documents(f)
        results, errors = [], []

        def worker():
            try:
                results.append(resolve(DID_A))
            except Exception as exc:   # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        threads[0].start()
        entered.wait()
        for t in threads[1:]:
            t.start()
        # Every follower must be parked on the in-flight fetch before release.
        while cache.waiters(("did_doc", DID_A)) < 7:
            threading.Event().wait(0)    # yield, no sleep
        release.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r is results[0] for r in results))

    def test_followers_share_the_failure_and_each_audits(self):
        cache, _ = make()
        release = threading.Event()
        entered = threading.Event()

        def slow_fail():
            entered.set()
            release.wait()
            return transient()

        f = FakeFetcher({STATUS_URL: slow_fail})
        resolve = cache.status_lists(f, lambda b, i, u: b)
        reasons = []

        def worker():
            try:
                resolve(ISSUER, STATUS_URL)
            except CacheBreach as exc:
                reasons.append(exc.reason)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        threads[0].start()
        entered.wait()
        for t in threads[1:]:
            t.start()
        while cache.waiters(("status_list", ISSUER, STATUS_URL)) < 3:
            threading.Event().wait(0)
        release.set()
        for t in threads:
            t.join()
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(reasons, [AbortReason.STATUS_FETCH_FAILED.value] * 4)


class Jitter(unittest.TestCase):
    def test_expiry_within_jitter_band_and_never_beyond_max(self):
        params = NetcapsParams(jitter_range_pct=20)
        ttl = params.did_doc_positive_ttl_max
        lifetimes = set()
        for seed in range(40):
            cache, clock = make(params, seed=seed)
            f = FakeFetcher({URL_A: did_doc(DID_A)})
            resolve = cache.did_documents(f)
            resolve(DID_A)
            life = cache.expires_at(("did_doc", DID_A)) - clock.monotonic()
            self.assertLessEqual(life, ttl)
            self.assertGreaterEqual(life, ttl * 0.8)
            lifetimes.add(round(life, 6))
        self.assertGreater(len(lifetimes), 30, "jitter must actually spread expiry")

    def test_seeded_rng_is_deterministic(self):
        params = NetcapsParams(jitter_range_pct=20)
        exp = []
        for _ in range(2):
            cache, _ = make(params, seed=7)
            cache.did_documents(FakeFetcher({URL_A: did_doc(DID_A)}))(DID_A)
            exp.append(cache.expires_at(("did_doc", DID_A)))
        self.assertEqual(exp[0], exp[1])

    def test_zero_jitter_is_exact(self):
        cache, clock = make()
        cache.did_documents(FakeFetcher({URL_A: did_doc(DID_A)}))(DID_A)
        self.assertEqual(cache.expires_at(("did_doc", DID_A)),
                         P.did_doc_positive_ttl_max)


class BoundedMemory(unittest.TestCase):
    def test_flood_of_distinct_dids_is_capped(self):
        cache, _ = make(max_entries=16)
        f = FakeFetcher()
        for i in range(200):
            f.responses[f"https://h{i}.example/.well-known/did.json"] = (
                did_doc(f"did:web:h{i}.example") if i % 2 else absent())
        resolve = cache.did_documents(f)
        for i in range(200):
            try:
                resolve(f"did:web:h{i}.example", source=f"s{i}")
            except CacheBreach:
                pass
        self.assertLessEqual(len(cache), 16)

    def test_lru_evicts_least_recently_used(self):
        cache, _ = make(max_entries=2)
        f = FakeFetcher({f"https://h{i}.example/.well-known/did.json":
                         did_doc(f"did:web:h{i}.example") for i in range(3)})
        resolve = cache.did_documents(f)
        resolve("did:web:h0.example")
        resolve("did:web:h1.example")
        resolve("did:web:h0.example")        # h0 now most recent
        resolve("did:web:h2.example")        # evicts h1
        self.assertEqual(len(f.calls), 3)
        resolve("did:web:h0.example")
        self.assertEqual(len(f.calls), 3)
        resolve("did:web:h1.example")
        self.assertEqual(len(f.calls), 4)

    def test_max_entries_must_be_positive(self):
        with self.assertRaises(ValueError):
            ResolutionCache(P, max_entries=0)


class BoundedGunzip(unittest.TestCase):
    def test_roundtrip(self):
        raw = b"\x00" * 1000
        self.assertEqual(bounded_gunzip(gzip.compress(raw), P), raw)

    def test_decompressed_cap(self):
        params = NetcapsParams(max_status_list_bytes=1024,
                               max_status_list_decompressed_bytes=2048)
        with self.assertRaises(ValueError) as cm:
            bounded_gunzip(gzip.compress(b"\x00" * 4096), params)
        self.assertIn("max_status_list_decompressed_bytes", str(cm.exception))

    def test_compression_ratio_cap(self):
        params = NetcapsParams(max_compression_ratio=10)
        with self.assertRaises(ValueError) as cm:
            bounded_gunzip(gzip.compress(b"\x00" * 100000), params)
        self.assertIn("max_compression_ratio", str(cm.exception))

    def test_compressed_size_cap(self):
        params = NetcapsParams(max_status_list_bytes=16)
        with self.assertRaises(ValueError):
            bounded_gunzip(gzip.compress(bytes(range(256)) * 4), params)

    def test_garbage_is_value_error(self):
        with self.assertRaises(ValueError):
            bounded_gunzip(b"not gzip", P)

    def test_validator_rejection_via_gunzip_is_definitive(self):
        params = NetcapsParams(jitter_range_pct=0, max_compression_ratio=10)
        cache, _ = make(params)
        f = FakeFetcher({STATUS_URL: gzip.compress(b"\x00" * 100000)})
        resolve = cache.status_lists(
            f, lambda body, issuer, url: bounded_gunzip(body, params))
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL)
        self.assertIs(cm.exception.failure_class, FailureClass.DEFINITIVE_ABSENT)


class BreachShape(unittest.TestCase):
    """The cache raises NetcapsBreach (PR2's hook turns it into _breach(), which
    emits the one audit event); it never emits events itself."""

    def test_strings_match_pr1_vocabulary(self):
        from tools.a2a import netcaps_cache as nc
        reasons = {r.value for r in AbortReason}
        events = {e.value for e in AuditEventType}
        for r in (nc.R_NEGATIVE, nc.R_STALE, nc.R_STATUS, nc.R_STEP_TIMEOUT,
                  nc.R_TOTAL_TIMEOUT, nc.R_DID_RESOLUTION):
            self.assertIn(r, reasons)
        for e in (nc.E_NEGATIVE, nc.E_STALE, nc.E_STATUS, nc.E_TIMEOUT,
                  nc.E_DID_RESOLUTION):
            self.assertIn(e, events)

    def test_breach_is_a_netcaps_breach_with_detail(self):
        cache, _ = make()
        with self.assertRaises(NetcapsBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: absent("410 gone")}))(DID_A)
        self.assertIn("410 gone", cm.exception.detail)
        self.assertIn("definitive_absent", cm.exception.detail)

    def test_netcaps_breach_attributes(self):
        b = NetcapsBreach("r", "e", "d")
        self.assertEqual((b.reason, b.event, b.detail), ("r", "e", "d"))
        self.assertEqual(NetcapsBreach("r", "e").detail, "")


class Timeouts(unittest.TestCase):
    def test_fetch_budget_is_d3_fetch_steps_without_deadline(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        cache.did_documents(f)(DID_A)
        self.assertEqual(f.timeouts_seen,
                         [P.timeout_connect + P.timeout_tls + P.timeout_fetch_read])

    def test_leader_budget_is_capped_by_its_own_deadline(self):
        # r3 MED: the leader never waits on a fetch past its own deadline (D3).
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 2)
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        cache.did_documents(f)(DID_A, deadline=deadline)
        self.assertEqual(f.timeouts_seen, [2])

    def test_deadline_squeeze_cannot_suppress_the_anchor(self):
        # F1 repro: a fetcher that times out on any short budget. An attacker
        # handshake with 0.05 s left fails ITSELF; the victim anchor is untouched.
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 0.05)
        seen = []

        def fetcher(url, *, max_bytes, timeout):
            seen.append(timeout)
            if timeout < 1:
                raise TimeoutError("socket timed out")
            return FetchResponse(did_doc(DID_A))

        resolve = cache.did_documents(fetcher)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A, deadline=deadline)
        self.assertEqual(cm.exception.reason, AbortReason.TOTAL_DEADLINE_EXCEEDED.value)
        self.assertTrue(cm.exception.deadline_only)
        self.assertLessEqual(seen[0], 0.05 + 1e-9)
        self.assertFalse(any(k[0] == "supp" for k in cache.keys()))
        self.assertEqual(resolve(DID_A).did, DID_A)

    def test_late_on_cut_budget_is_neither_cached_nor_suppressed(self):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 1)

        def slow():
            clock.advance(2)            # past the leader's cut 1 s budget
            return did_doc(DID_A)

        f = FakeFetcher({URL_A: slow})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A, deadline=deadline)
        self.assertEqual(cm.exception.reason, AbortReason.TOTAL_DEADLINE_EXCEEDED.value)
        self.assertEqual(f.timeouts_seen, [1])
        self.assertEqual(len(cache), 0)
        self.assertEqual(resolve(DID_A).did, DID_A)
        self.assertEqual(len(f.calls), 2)

    def test_full_budget_timeout_is_shared_and_suppresses(self):
        cache, clock = make()
        entered, release = threading.Event(), threading.Event()
        full = P.timeout_connect + P.timeout_tls + P.timeout_fetch_read

        def slow():
            entered.set()
            release.wait()
            clock.advance(full + 1)     # a real anchor timeout on the full budget
            return did_doc(DID_A)

        f = FakeFetcher({URL_A: slow})
        resolve = cache.did_documents(f)
        out = {}

        def run(name, **kw):
            try:
                resolve(DID_A, **kw)
                out[name] = "ok"
            except CacheBreach as exc:
                out[name] = exc.reason

        t1 = threading.Thread(target=run, args=("lead",))
        t1.start()
        entered.wait()
        t2 = threading.Thread(target=run, args=("follow",), kwargs={"source": "s2"})
        t2.start()
        while cache.waiters(("did_doc", DID_A)) < 1:
            threading.Event().wait(0)
        release.set()
        t1.join()
        t2.join()
        self.assertEqual(out, {"lead": AbortReason.STEP_TIMEOUT.value,
                               "follow": AbortReason.STEP_TIMEOUT.value})
        self.assertEqual(len(f.calls), 1, "shared: the follower did not refetch")
        self.assertIn(("supp", "a.example", "local"), cache.keys())

    def test_spent_deadline_refuses_without_fetch(self):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake)
        f = FakeFetcher({URL_A: did_doc(DID_A)})
        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(f)(DID_A, deadline=deadline)
        self.assertEqual(cm.exception.reason, AbortReason.TOTAL_DEADLINE_EXCEEDED.value)
        self.assertEqual(cm.exception.event, AuditEventType.RESOLVER_TIMEOUT.value)
        self.assertEqual(f.calls, [])

    def test_late_result_is_discarded_and_not_cached(self):
        cache, clock = make()
        budget = P.timeout_connect + P.timeout_tls + P.timeout_fetch_read

        def late():
            clock.advance(budget + 1)
            return did_doc(DID_A)

        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: late}))(DID_A)
        self.assertEqual(cm.exception.reason, AbortReason.STEP_TIMEOUT.value)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
        self.assertEqual([k for k in cache.keys() if k[0] == "id"], [])

    def test_late_under_deadline_is_total_for_that_caller(self):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 1)

        def late():
            clock.advance(2)
            return did_doc(DID_A)

        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(FakeFetcher({URL_A: late}))(DID_A, deadline=deadline)
        self.assertEqual(cm.exception.reason, AbortReason.TOTAL_DEADLINE_EXCEEDED.value)

    def test_library_timeout_types_are_transient_resolver_timeout(self):
        class Timeout(Exception):          # stands in for dns.exception.Timeout
            pass
        Timeout.__module__ = "dns.exception"
        Timeout.__qualname__ = "Timeout"

        class LifetimeTimeout(Timeout):    # dnspython's subclass
            pass

        for exc in (TimeoutError("t"), socket.timeout("t"), Timeout(),
                    LifetimeTimeout(), StepTimeout(TimeoutStep.FETCH_READ)):
            cache, _ = make()
            with self.assertRaises(CacheBreach) as cm:
                cache.did_documents(FakeFetcher({URL_A: exc}))(DID_A)
            self.assertEqual(cm.exception.event, AuditEventType.RESOLVER_TIMEOUT.value,
                             type(exc).__name__)
            self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)

    def test_timeout_suppresses_like_transient(self):
        cache, _ = make()
        f = FakeFetcher({URL_A: TimeoutError()})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach):
            resolve(DID_A)
        with self.assertRaises(CacheBreach):
            resolve(DID_A)
        self.assertEqual(len(f.calls), 1)


class FollowerIsolation(unittest.TestCase):
    """F1/L5: a waiter's timeout and reason are its own."""

    def _park(self, cache, key, n=1):
        while cache.waiters(key) < n:
            threading.Event().wait(0)

    def test_leader_deadline_failure_not_shared_with_follower(self):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 0.05)
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait()
            clock.advance(1)            # past the leader's deadline only
            return did_doc(DID_A)

        resolve = cache.did_documents(FakeFetcher({URL_A: slow}))
        out = {}

        def lead():
            try:
                resolve(DID_A, source="attacker", deadline=deadline)
                out["lead"] = "ok"
            except CacheBreach as exc:
                out["lead"] = exc.reason

        def follow():
            try:
                out["follow"] = resolve(DID_A, source="legit").did
            except CacheBreach as exc:   # pragma: no cover - the bug
                out["follow"] = exc.reason

        t1 = threading.Thread(target=lead)
        t1.start()
        entered.wait()
        t2 = threading.Thread(target=follow)
        t2.start()
        self._park(cache, ("did_doc", DID_A))
        release.set()
        t1.join()
        t2.join()
        self.assertEqual(out["lead"], AbortReason.TOTAL_DEADLINE_EXCEEDED.value)
        self.assertEqual(out["follow"], DID_A)
        # The leader's cut fetch was discarded; the follower re-fetched on the
        # full budget as the new leader.
        self.assertEqual(len(cache.keys()), 1)

    def test_follower_times_out_on_its_own_budget(self):
        cache, _ = make()
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait()
            return did_doc(DID_A)

        resolve = cache.did_documents(FakeFetcher({URL_A: slow}))

        class Tiny:                      # 10 ms of real wait, then give up
            def remaining(self):
                return 0.01

        leader = threading.Thread(target=lambda: resolve(DID_A))
        leader.start()
        entered.wait()
        try:
            with self.assertRaises(CacheBreach) as cm:
                resolve(DID_A, source="s2", deadline=Tiny())
        finally:
            release.set()
            leader.join()
        self.assertEqual(cm.exception.reason, AbortReason.TOTAL_DEADLINE_EXCEEDED.value)
        self.assertEqual(cm.exception.event, AuditEventType.RESOLVER_TIMEOUT.value)
        self.assertFalse(any(k[0] == "supp" for k in cache.keys()))
        self.assertEqual(resolve(DID_A).did, DID_A)   # the leader's result cached

    def test_expired_material_while_anchor_suppressed_is_stale_denied(self):
        cache, clock = make()
        did_a2 = "did:web:a.example:users:two"
        url_a2 = "https://a.example/users/two/did.json"
        f = FakeFetcher({URL_A: did_doc(DID_A), url_a2: transient()})
        resolve = cache.did_documents(f)
        resolve(DID_A)
        clock.advance(P.did_doc_positive_ttl_max)
        with self.assertRaises(CacheBreach):
            resolve(did_a2)                  # suppresses a.example for "local"
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A)
        self.assertEqual(cm.exception.reason, AbortReason.STALE_DENIED.value)
        self.assertNotIn(URL_A, f.calls[1:], "suppressed: no refetch")


class ValidatorRouting(unittest.TestCase):
    """F2: only an explicit rejection is definitive."""

    def _status(self, exc_or_none):
        cache, clock = make()
        n = {"v": 0}

        def validator(body, issuer, url):
            n["v"] += 1
            if n["v"] == 1 and exc_or_none is not None:
                raise exc_or_none
            return body

        f = FakeFetcher({STATUS_URL: b"ok"})
        return cache, clock, f, cache.status_lists(f, validator)

    def test_validator_timeout_is_transient_not_negative(self):
        cache, clock, f, resolve = self._status(TimeoutError("issuer doc timed out"))
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
        self.assertEqual(cm.exception.event, AuditEventType.RESOLVER_TIMEOUT.value)
        self.assertEqual(resolve(ISSUER, STATUS_URL, source="other"), b"ok")

    def test_validator_oserror_and_transient_breach_are_transient(self):
        inner = CacheBreach("x", "y", failure_class=FailureClass.TRANSIENT_INFRA)
        for exc in (OSError("issuer anchor down"), inner):
            cache, clock, f, resolve = self._status(exc)
            with self.assertRaises(CacheBreach) as cm:
                resolve(ISSUER, STATUS_URL)
            self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
            self.assertEqual(resolve(ISSUER, STATUS_URL, source="other"), b"ok")

    def test_validator_definitive_breach_is_definitive(self):
        inner = CacheBreach("x", "y", failure_class=FailureClass.DEFINITIVE_ABSENT)
        cache, clock, f, resolve = self._status(inner)
        with self.assertRaises(CacheBreach):
            resolve(ISSUER, STATUS_URL)
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL, source="other")
        self.assertEqual(cm.exception.reason, AbortReason.NEGATIVE_CACHE_HIT.value)

    def test_validator_unclassified_caches_nothing(self):
        for exc in (RuntimeError("bug"),
                    CacheBreach("negative_cache_hit", "negative_cache_hit"),
                    CacheBreach("x", "y", failure_class=FailureClass.UNKNOWN)):
            cache, clock, f, resolve = self._status(exc)
            with self.assertRaises(CacheBreach) as cm:
                resolve(ISSUER, STATUS_URL)
            self.assertIs(cm.exception.failure_class, FailureClass.UNKNOWN)
            self.assertEqual(len(cache), 0)
            self.assertEqual(resolve(ISSUER, STATUS_URL), b"ok")


class LowFixes(unittest.TestCase):
    def test_ttl_anchored_at_fetch_start(self):
        cache, clock = make()

        def slow():
            clock.advance(3)
            return did_doc(DID_A)

        cache.did_documents(FakeFetcher({URL_A: slow}))(DID_A)
        self.assertEqual(cache.expires_at(("did_doc", DID_A)),
                         P.did_doc_positive_ttl_max)

    def test_slow_validator_counts_against_ttl(self):
        cache, clock = make()

        def validator(body, issuer, url):
            clock.advance(4)
            return body

        cache.status_lists(FakeFetcher({STATUS_URL: b"x"}), validator)(ISSUER, STATUS_URL)
        self.assertEqual(cache.expires_at(("status_list", ISSUER, STATUS_URL)),
                         P.status_list_positive_ttl_max)

    def test_repeated_max_age_takes_minimum(self):
        self.assertEqual(max_age_from_cache_control("max-age=0, max-age=900"), 0)
        self.assertEqual(max_age_from_cache_control("max-age=900, max-age=0"), 0)
        self.assertEqual(max_age_from_cache_control("max-age=60, max-age=30"), 30)

    def test_max_age_ascii_digits_only(self):
        self.assertIsNone(max_age_from_cache_control("max-age=\u00b2"))
        self.assertIsNone(max_age_from_cache_control("max-age=\u0663"))

    def test_gunzip_rejects_trailing_data_and_extra_members(self):
        one = gzip.compress(b"\x00" * 64)
        for data in (one + b"junk", one + one):
            with self.assertRaises(ValueError) as cm:
                bounded_gunzip(data, P)
            self.assertIn("trailing", str(cm.exception))


class R3Fixes(unittest.TestCase):
    def test_follower_retries_after_leader_deadline_only_failure(self):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - 0.05)
        entered, release = threading.Event(), threading.Event()
        full = P.timeout_connect + P.timeout_tls + P.timeout_fetch_read
        seen = []

        def fetcher(url, *, max_bytes, timeout):
            seen.append(timeout)
            if len(seen) == 1:
                entered.set()
                release.wait()
                raise TimeoutError("cut budget ran out")
            return FetchResponse(did_doc(DID_A))

        resolve = cache.did_documents(fetcher)
        out = {}

        def lead():
            try:
                resolve(DID_A, source="attacker", deadline=deadline)
            except CacheBreach as exc:
                out["lead"] = exc

        t1 = threading.Thread(target=lead)
        t1.start()
        entered.wait()
        t2 = threading.Thread(
            target=lambda: out.__setitem__("follow", resolve(DID_A, source="legit")))
        t2.start()
        while cache.waiters(("did_doc", DID_A)) < 1:
            threading.Event().wait(0)
        release.set()
        t1.join()
        t2.join()
        self.assertTrue(out["lead"].deadline_only)
        self.assertEqual(out["follow"].did, DID_A)
        self.assertEqual(len(seen), 2)
        self.assertLessEqual(seen[0], 0.05 + 1e-9)
        self.assertEqual(seen[1], full)
        self.assertFalse(any(k[0] == "supp" for k in cache.keys()))

    def test_declared_length_mismatch_is_transient(self):
        cache, _ = make()
        body = did_doc(DID_A)
        f = FakeFetcher({URL_A: FetchResponse(body[:20], declared_length=len(body))})
        resolve = cache.did_documents(f)
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
        self.assertIn("short body", cm.exception.detail)
        self.assertFalse(any(k[0] == "id" for k in cache.keys()), "never negative")

    def test_declared_length_match_is_fine(self):
        cache, _ = make()
        body = did_doc(DID_A)
        f = FakeFetcher({URL_A: FetchResponse(body, declared_length=len(body))})
        self.assertEqual(cache.did_documents(f)(DID_A).did, DID_A)

    def test_nested_transient_breach_suppresses_the_failing_host(self):
        cache, _ = make()
        inner = CacheBreach("x", "y", failure_class=FailureClass.TRANSIENT_INFRA,
                            anchor="issuer-doc.example")

        def validator(body, issuer, url):
            raise inner

        resolve = cache.status_lists(FakeFetcher({STATUS_URL: b"ok"}), validator)
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, STATUS_URL, source="s1")
        self.assertEqual(cm.exception.anchor, "issuer-doc.example")
        supp = [k for k in cache.keys() if k[0] == "supp"]
        self.assertEqual(supp, [("supp", "issuer-doc.example", "s1")])

    def test_nested_transient_breach_without_host_suppresses_nothing(self):
        for inner in (CacheBreach("x", "y", failure_class=FailureClass.TRANSIENT_INFRA),
                      OSError("issuer anchor down"), TimeoutError("t")):
            cache, _ = make()

            def validator(body, issuer, url, inner=inner):
                raise inner

            resolve = cache.status_lists(FakeFetcher({STATUS_URL: b"ok"}), validator)
            with self.assertRaises(CacheBreach) as cm:
                resolve(ISSUER, STATUS_URL)
            self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
            self.assertEqual(len(cache), 0, repr(inner))

    def test_real_nested_resolve_names_its_anchor(self):
        # A status-list validator that resolves the issuer DID doc through the
        # same cache: the issuer host is suppressed, not the status-list host.
        cache, _ = make()
        issuer_url = "https://issuer.example/.well-known/did.json"
        dids = cache.did_documents(FakeFetcher({issuer_url: transient()}))

        def validator(body, issuer, url):
            dids(issuer, source="s1")
            return body

        status_url = "https://lists.example/s/1"
        resolve = cache.status_lists(FakeFetcher({status_url: b"ok"}), validator)
        with self.assertRaises(CacheBreach) as cm:
            resolve(ISSUER, status_url, source="s1")
        self.assertEqual(cm.exception.anchor, "issuer.example")
        supp = {k for k in cache.keys() if k[0] == "supp"}
        self.assertEqual(supp, {("supp", "issuer.example", "s1")})


FULL = P.timeout_connect + P.timeout_tls + P.timeout_fetch_read


class _GatedLock:
    """Wraps the cache lock; holds one named thread's Nth..Mth acquisitions at
    a gate Event, so a test can order who registers a flight before whom."""

    def __init__(self, real, thread_name, gates):
        self._real, self._name, self._gates = real, thread_name, gates
        self._n = 0

    def __enter__(self):
        if threading.current_thread().name == self._name:
            self._n += 1
            gate = self._gates.get(self._n)
            if gate is not None:
                assert gate.wait(10), "gate never opened"
        self._real.acquire()

    def __exit__(self, *a):
        self._real.release()


class T5399cSingleflightCap(unittest.TestCase):
    def test_follower_with_budget_gets_a_solo_fetch_after_the_cap(self):
        """Three successive leaders die on their own cut deadlines while the
        follower (ample budget) is parked on each; it then does one solo fetch
        and gets the value instead of failing."""
        cache, clock = make()
        key = ("did_doc", DID_A)
        deadlines = [TimeoutPolicy(P, clock=clock).start() for _ in range(3)]
        clock.advance(P.timeout_total_handshake - 0.05)
        entered = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]
        seen = []

        def fetcher(url, *, max_bytes, timeout):
            i = len(seen)
            seen.append(timeout)
            if i < 3:
                entered[i].set()
                assert release[i].wait(10)
                raise TimeoutError("leader's cut budget ran out")
            return FetchResponse(did_doc(DID_A))

        resolve = cache.did_documents(fetcher)
        gates = {2: threading.Event(), 3: threading.Event()}
        cache._lock = _GatedLock(cache._lock, "F", gates)
        out = {}

        def lead(i):
            try:
                resolve(DID_A, source=f"s{i}", deadline=deadlines[i])
            except CacheBreach as exc:
                out[f"lead{i}"] = exc

        def follow():
            try:
                out["F"] = resolve(DID_A, source="legit")
            except CacheBreach as exc:
                out["F"] = exc

        threads = [threading.Thread(target=lead, args=(0,), name="L0")]
        threads[0].start()
        assert entered[0].wait(10)
        f = threading.Thread(target=follow, name="F")
        f.start()
        for i in range(3):
            while cache.waiters(key) < 1:
                threading.Event().wait(0)
            release[i].set()
            threads[-1].join(10)
            if i < 2:
                t = threading.Thread(target=lead, args=(i + 1,), name=f"L{i + 1}")
                threads.append(t)
                t.start()
                assert entered[i + 1].wait(10)
                gates[i + 2].set()
        f.join(10)
        self.assertFalse(f.is_alive())
        self.assertTrue(all(out[f"lead{i}"].deadline_only for i in range(3)))
        self.assertIsInstance(out["F"], ParsedDidDocument, out["F"])
        self.assertEqual(len(seen), 4, "exactly one extra, solo fetch")
        self.assertEqual(seen[3], FULL)


class T5399cMaterialCut(unittest.TestCase):
    def _run(self, remaining):
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - remaining)

        def fetcher(url, *, max_bytes, timeout):
            raise TimeoutError("budget ran out")

        with self.assertRaises(CacheBreach) as cm:
            cache.did_documents(fetcher)(DID_A, deadline=deadline)
        return cache, cm.exception

    def test_near_full_budget_timeout_is_a_real_anchor_timeout(self):
        cache, exc = self._run(0.95 * FULL)
        self.assertFalse(exc.deadline_only)
        self.assertEqual(exc.reason, "network_step_timeout")
        self.assertTrue(any(k[0] == "supp" for k in cache.keys()))

    def test_materially_cut_budget_stays_deadline_only(self):
        cache, exc = self._run(0.5 * FULL)
        self.assertTrue(exc.deadline_only)
        self.assertFalse(any(k[0] == "supp" for k in cache.keys()))

    def test_threshold_is_shared_by_the_follower_naming(self):
        from tools.a2a.netcaps_cache import MATERIAL_CUT_RATIO, is_material_cut
        self.assertFalse(is_material_cut(FULL, FULL))
        self.assertFalse(is_material_cut(MATERIAL_CUT_RATIO * FULL, FULL))
        self.assertTrue(is_material_cut(MATERIAL_CUT_RATIO * FULL - 1e-6, FULL))


class T5399cNestedDeadlineOnly(unittest.TestCase):
    def test_nested_deadline_only_survives_the_validator_and_suppresses_nothing(self):
        cache, _ = make()
        nested = CacheBreach("total_handshake_deadline_exceeded", "resolver_timeout",
                             "inner", failure_class=FailureClass.TRANSIENT_INFRA,
                             anchor="inner.example", deadline_only=True)

        def validator(body, issuer, url):
            raise nested

        f = FakeFetcher({STATUS_URL: b"x"})
        with self.assertRaises(CacheBreach) as cm:
            cache.status_lists(f, validator)(ISSUER, STATUS_URL)
        self.assertTrue(cm.exception.deadline_only)
        self.assertEqual(cm.exception.reason, "total_handshake_deadline_exceeded")
        self.assertFalse(any(k[0] == "supp" for k in cache.keys()))


class T5399cRawLengthContract(unittest.TestCase):
    """declared_length is the raw on-the-wire length; body is raw undecoded."""

    def test_raw_gzip_body_with_raw_length_reaches_the_validator_undecoded(self):
        cache, _ = make()
        raw = gzip.compress(b"bitstring" * 10)
        got = []

        def validator(body, issuer, url):
            got.append(body)
            return bounded_gunzip(body, P)

        f = FakeFetcher({STATUS_URL: FetchResponse(raw, declared_length=len(raw))})
        cache.status_lists(f, validator)(ISSUER, STATUS_URL)
        self.assertEqual(got, [raw])

    def test_transparently_decoded_body_is_a_transient_short_body(self):
        # A fetcher that content-decoded violates the contract; the length
        # mismatch classifies transient (see also test_declared_length_mismatch_is_transient).
        cache, _ = make()
        raw = gzip.compress(b"bitstring" * 10)
        decoded = gzip.decompress(raw)
        f = FakeFetcher({STATUS_URL: FetchResponse(decoded, declared_length=len(raw))})
        with self.assertRaises(CacheBreach) as cm:
            cache.status_lists(f, lambda b, i, u: b)(ISSUER, STATUS_URL)
        self.assertIs(cm.exception.failure_class, FailureClass.TRANSIENT_INFRA)
        self.assertFalse(any(k[0] == "id" for k in cache.keys()), "never negative")



class T5399cFollowerBinding(unittest.TestCase):
    """The follower's timeout reason names its own deadline only when that
    deadline MATERIALLY cut its wait (same threshold as the leader's cut)."""

    class _NeverDone:
        def __init__(self):
            self.timeouts = []

        def wait(self, timeout=None):
            self.timeouts.append(timeout)
            return False

        def set(self):
            pass

    def _follow(self, remaining):
        from tools.a2a import netcaps_cache as nc
        cache, clock = make()
        deadline = TimeoutPolicy(P, clock=clock).start()
        clock.advance(P.timeout_total_handshake - remaining)
        flight = nc._Flight()
        flight.done = self._NeverDone()
        cache._flights[("did_doc", DID_A)] = flight      # a leader already in flight
        resolve = cache.did_documents(FakeFetcher({}))
        with self.assertRaises(CacheBreach) as cm:
            resolve(DID_A, deadline=deadline)
        self.assertAlmostEqual(flight.done.timeouts[0], remaining, places=6)
        return cm.exception

    def test_near_full_wait_is_a_step_timeout(self):
        exc = self._follow(0.95 * FULL)
        self.assertEqual(exc.reason, "network_step_timeout")

    def test_materially_cut_wait_names_the_total_deadline(self):
        exc = self._follow(0.5 * FULL)
        self.assertEqual(exc.reason, "total_handshake_deadline_exceeded")



if __name__ == "__main__":
    unittest.main()
