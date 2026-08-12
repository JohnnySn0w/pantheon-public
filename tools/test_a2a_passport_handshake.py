"""Tests for tools/a2a/passport_handshake.py — ORG-047 Layer A (T-2830).

Pure-local: the module has no a2a-sdk / network / clock dependency, so these
run under system python3 in the whole-suite smoke
(`python3 -m unittest discover -s tools -p 'test_*.py'`) with no skip guard.

Every fail-closed ABORT precondition from ADR D1 (lines 37-42) gets a test, plus
the D4 untrusted-content invariant, the scheme strength precedence, and the
stateful downgrade rejection.
"""

from __future__ import annotations

import json
import unittest

from tools.a2a.passport_handshake import (
    AbortReason,
    AllowlistEntry,
    HandshakeState,
    InMemoryDowngradeStore,
    MtlsAdapter,
    Outcome,
    PassportHandshake,
    Scheme,
    TxtResolution,
    compute_card_hash,
    parse_advertised_schemes,
    select_strongest_common,
)

ENDPOINT = "did:web:snowis.me"
CARD_URL = "https://snowis.me/.well-known/agent-card.json"


def _card_bytes(scheme_types: list[dict]) -> bytes:
    """Serialise a minimal Agent Card advertising the given securityScheme
    entries. Bytes are what get hashed + pinned (TG-869)."""
    card = {
        "name": "snowis-seed",
        "url": "https://snowis.me/a2a",
        "securitySchemes": {f"s{i}": e for i, e in enumerate(scheme_types)},
    }
    return json.dumps(card).encode("utf-8")


class _StubAdapter:
    """Test scheme adapter — verify() returns a fixed bool (or raises if told)."""

    def __init__(self, scheme: Scheme, result: bool = True, raises: bool = False):
        self._scheme = scheme
        self._result = result
        self._raises = raises

    @property
    def scheme(self) -> Scheme:
        return self._scheme

    def verify(self, endpoint_id: str, allowlist_entry: AllowlistEntry) -> bool:
        if self._raises:
            raise NotImplementedError("stub")
        return self._result


def _resolver(
    present: bool = True,
    dnssec: bool = True,
    card_url: str = CARD_URL,
    pinned_hash: str = "",
):
    def _r(endpoint_id: str) -> TxtResolution:
        return TxtResolution(
            present=present,
            dnssec_validated=dnssec,
            card_url=card_url,
            pinned_card_hash=pinned_hash,
        )
    return _r


def _fetcher(card_bytes: bytes | None):
    def _f(card_url: str) -> bytes | None:
        return card_bytes
    return _f


def _build(
    *,
    advertised=(("mutualTLS", Scheme.MTLS), ("apiKey", Scheme.API_KEY)),
    supported=(Scheme.MTLS, Scheme.API_KEY),
    present=True,
    dnssec=True,
    in_allowlist=True,
    txt_pin_override=None,
    allowlist_pin_override=None,
    card_bytes_override=None,
    fetch_none=False,
    adapter_results=None,
    downgrade_store=None,
):
    """Construct a PassportHandshake wired for the happy path, with single-knob
    overrides for each ABORT case. Returns (handshake, card_bytes, true_hash)."""
    scheme_entries = []
    for typ, _ in advertised:
        if typ == "http":
            scheme_entries.append({"type": "http", "scheme": "bearer"})
        else:
            scheme_entries.append({"type": typ})
    card_bytes = card_bytes_override if card_bytes_override is not None else _card_bytes(scheme_entries)
    true_hash = compute_card_hash(card_bytes)

    txt_pin = txt_pin_override if txt_pin_override is not None else true_hash
    allow_pin = allowlist_pin_override if allowlist_pin_override is not None else true_hash

    allowlist = {}
    if in_allowlist:
        allowlist[ENDPOINT] = AllowlistEntry(card_hash=allow_pin)

    adapters = {}
    for s in supported:
        res = True if adapter_results is None else adapter_results.get(s, True)
        if res == "raise":
            adapters[s] = _StubAdapter(s, raises=True)
        else:
            adapters[s] = _StubAdapter(s, result=bool(res))

    hs = PassportHandshake(
        txt_resolver=_resolver(present=present, dnssec=dnssec, pinned_hash=txt_pin),
        card_fetcher=_fetcher(None if fetch_none else card_bytes),
        allowlist=allowlist,
        adapters=adapters,
        downgrade_store=downgrade_store,
    )
    return hs, card_bytes, true_hash


# ---------------------------------------------------------------------------
# Happy path + D4 invariant.
# ---------------------------------------------------------------------------


class TestHappyPath(unittest.TestCase):
    def test_admits_strongest_common_scheme(self):
        hs, _, _ = _build()
        r = hs.run(ENDPOINT)
        self.assertEqual(r.outcome, Outcome.ADMITTED)
        self.assertTrue(r.admitted)
        self.assertEqual(r.state, HandshakeState.ADMIT_UNTRUSTED)
        # mTLS beats API-key (precedence).
        self.assertEqual(r.scheme, Scheme.MTLS)
        self.assertEqual(r.principal, ENDPOINT)

    def test_admitted_content_is_never_trusted(self):
        """D4 invariant (lines 61-62): even a fully-authenticated admit keeps
        content_trusted False."""
        hs, _, _ = _build()
        r = hs.run(ENDPOINT)
        self.assertTrue(r.admitted)
        self.assertFalse(r.content_trusted)

    def test_no_partial_trust_abort_has_no_scheme_or_principal(self):
        hs, _, _ = _build(present=False)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertIsNone(r.scheme)
        self.assertIsNone(r.principal)
        self.assertFalse(r.content_trusted)


# ---------------------------------------------------------------------------
# Step 1 — resolve endpoint (card-auth) ABORTs.
# ---------------------------------------------------------------------------


class TestStep1Aborts(unittest.TestCase):
    def test_no_txt_aborts(self):
        hs, _, _ = _build(present=False)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.NO_TXT)
        self.assertEqual(r.state, HandshakeState.RESOLVE_ENDPOINT)

    def test_invalid_dnssec_aborts_no_unsigned_fallback(self):
        hs, _, _ = _build(dnssec=False)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.DNSSEC_INVALID)

    def test_endpoint_not_allowlisted_aborts(self):
        hs, _, _ = _build(in_allowlist=False)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.ENDPOINT_NOT_ALLOWLISTED)

    def test_card_fetch_failure_aborts(self):
        hs, _, _ = _build(fetch_none=True)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.CARD_FETCH_FAILED)

    def test_card_hash_ne_txt_pin_aborts(self):
        hs, _, _ = _build(txt_pin_override="sha256:deadbeef")
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.CARD_HASH_MISMATCH)

    def test_card_hash_ne_allowlist_pin_aborts(self):
        # TXT pin matches the served card, but the allowlist independently pins
        # a DIFFERENT hash — dual-pin disagreement must ABORT.
        hs, _, _ = _build(allowlist_pin_override="sha256:cafe")
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.CARD_HASH_MISMATCH)

    def test_unparseable_card_aborts_after_hash_verify(self):
        raw = b"this is hash-verified but not json"
        h = compute_card_hash(raw)
        hs, _, _ = _build(card_bytes_override=raw, txt_pin_override=h,
                          allowlist_pin_override=h)
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.CARD_UNPARSEABLE)


# ---------------------------------------------------------------------------
# Step 2 — select scheme + downgrade rejection.
# ---------------------------------------------------------------------------


class TestStep2SchemeSelection(unittest.TestCase):
    def test_no_common_scheme_aborts(self):
        # Counterparty advertises only API-key; we support only mTLS.
        hs, _, _ = _build(
            advertised=(("apiKey", Scheme.API_KEY),),
            supported=(Scheme.MTLS,),
        )
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.NO_COMMON_SCHEME)
        self.assertEqual(r.state, HandshakeState.SELECT_SCHEME)

    def test_empty_supported_set_cannot_admit(self):
        """A Layer-A production posture (no real adapter registered) refuses at
        step 2 — the fail-closed-by-construction guarantee."""
        hs, _, _ = _build(supported=())
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.NO_COMMON_SCHEME)

    def test_precedence_prefers_mtls_over_bearer_over_apikey(self):
        hs, _, _ = _build(
            advertised=(("apiKey", Scheme.API_KEY), ("http", Scheme.OIDC_BEARER),
                        ("mutualTLS", Scheme.MTLS)),
            supported=(Scheme.API_KEY, Scheme.OIDC_BEARER, Scheme.MTLS),
        )
        r = hs.run(ENDPOINT)
        self.assertEqual(r.scheme, Scheme.MTLS)


class TestDowngradeRejection(unittest.TestCase):
    def test_stronger_prior_scheme_now_absent_aborts(self):
        store = InMemoryDowngradeStore()
        store.record(ENDPOINT, {Scheme.MTLS, Scheme.API_KEY})  # prior saw mTLS
        # Now the counterparty advertises only API-key — mTLS dropped.
        hs, _, _ = _build(
            advertised=(("apiKey", Scheme.API_KEY),),
            supported=(Scheme.API_KEY,),
            downgrade_store=store,
        )
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.DOWNGRADE_DETECTED)

    def test_dropping_weaker_scheme_while_keeping_strongest_is_ok(self):
        store = InMemoryDowngradeStore()
        store.record(ENDPOINT, {Scheme.MTLS, Scheme.API_KEY})
        # Now advertises only mTLS (dropped the WEAKER api-key) — not a downgrade.
        hs, _, _ = _build(
            advertised=(("mutualTLS", Scheme.MTLS),),
            supported=(Scheme.MTLS,),
            downgrade_store=store,
        )
        r = hs.run(ENDPOINT)
        self.assertTrue(r.admitted)
        self.assertEqual(r.scheme, Scheme.MTLS)

    def test_first_handshake_is_never_a_downgrade(self):
        store = InMemoryDowngradeStore()
        hs, _, _ = _build(
            advertised=(("apiKey", Scheme.API_KEY),),
            supported=(Scheme.API_KEY,),
            downgrade_store=store,
        )
        r = hs.run(ENDPOINT)
        self.assertTrue(r.admitted)

    def test_downgrade_store_records_on_success_not_on_abort(self):
        store = InMemoryDowngradeStore()
        # An ABORT (unparseable card) after seeing advertised schemes must NOT
        # poison the downgrade memory — the abort happens in step 1, before the
        # record. Use a step-2-reaching abort instead: no-common-scheme records
        # nothing either (abort precedes record). Verify memory stays empty.
        hs, _, _ = _build(
            advertised=(("apiKey", Scheme.API_KEY),),
            supported=(Scheme.MTLS,),  # no common -> ABORT at step 2
            downgrade_store=store,
        )
        hs.run(ENDPOINT)
        self.assertEqual(store.seen(ENDPOINT), set())

    def test_downgrade_store_records_advertised_on_admit(self):
        store = InMemoryDowngradeStore()
        hs, _, _ = _build(
            advertised=(("mutualTLS", Scheme.MTLS), ("apiKey", Scheme.API_KEY)),
            supported=(Scheme.MTLS, Scheme.API_KEY),
            downgrade_store=store,
        )
        hs.run(ENDPOINT)
        self.assertEqual(store.seen(ENDPOINT), {Scheme.MTLS, Scheme.API_KEY})

    def test_step3_abort_does_not_record(self):
        # T-2867 HANDSHAKE-1: a PRESENTATION_FAILED abort at step 3 must NOT fold the
        # advertised set into the downgrade memory. record() now runs only after a
        # verified presentation, so a failed handshake cannot poison the high-water
        # mark (which would later false-refuse a legitimate handshake as a downgrade).
        store = InMemoryDowngradeStore()
        hs, _, _ = _build(
            advertised=(("mutualTLS", Scheme.MTLS), ("apiKey", Scheme.API_KEY)),
            supported=(Scheme.MTLS, Scheme.API_KEY),
            adapter_results={Scheme.MTLS: False},  # step 2 selects mTLS; step 3 fails
            downgrade_store=store,
        )
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)
        self.assertEqual(store.seen(ENDPOINT), set())


# ---------------------------------------------------------------------------
# Step 3 — presentation, and the fail-closed adapter posture.
# ---------------------------------------------------------------------------


class TestStep3Presentation(unittest.TestCase):
    def test_adapter_verify_false_aborts(self):
        hs, _, _ = _build(adapter_results={Scheme.MTLS: False})
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)
        self.assertEqual(r.state, HandshakeState.PRESENT_IDENTITY)

    def test_adapter_not_implemented_aborts_not_raises(self):
        hs, _, _ = _build(adapter_results={Scheme.MTLS: "raise"})
        r = hs.run(ENDPOINT)
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)

    def test_real_mtls_adapter_skeleton_cannot_admit(self):
        """Registering the shipped structural MtlsAdapter lets step 2 SELECT
        mTLS but step 3 can only ABORT (fail-closed until the crypto lands)."""
        _, card_bytes, true_hash = _build()  # reuse the happy card
        hs = PassportHandshake(
            txt_resolver=_resolver(pinned_hash=true_hash),
            card_fetcher=_fetcher(card_bytes),
            allowlist={ENDPOINT: AllowlistEntry(card_hash=true_hash)},
            adapters={Scheme.MTLS: MtlsAdapter()},
        )
        r = hs.run(ENDPOINT)
        self.assertEqual(r.outcome, Outcome.ABORTED)
        self.assertEqual(r.abort_reason, AbortReason.PRESENTATION_FAILED)


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    def test_compute_card_hash_is_tagged_and_byte_exact(self):
        self.assertTrue(compute_card_hash(b"abc").startswith("sha256:"))
        self.assertEqual(compute_card_hash(b"abc"), compute_card_hash(b"abc"))
        self.assertNotEqual(compute_card_hash(b"abc"), compute_card_hash(b"abd"))

    def test_compute_card_hash_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            compute_card_hash("not-bytes")  # type: ignore[arg-type]

    def test_parse_advertised_schemes_mapping(self):
        card = {
            "securitySchemes": {
                "a": {"type": "mutualTLS"},
                "b": {"type": "http", "scheme": "bearer"},
                "c": {"type": "http", "scheme": "basic"},   # not supported
                "d": {"type": "oauth2"},
                "e": {"type": "openIdConnect"},
                "f": {"type": "apiKey"},
                "g": {"type": "somethingWeird"},            # ignored
                "h": "not-a-dict",                          # ignored
            }
        }
        self.assertEqual(
            parse_advertised_schemes(card),
            {Scheme.MTLS, Scheme.OIDC_BEARER, Scheme.API_KEY},
        )

    def test_parse_advertised_schemes_defensive(self):
        self.assertEqual(parse_advertised_schemes({}), set())
        self.assertEqual(parse_advertised_schemes({"securitySchemes": 5}), set())
        self.assertEqual(parse_advertised_schemes("nope"), set())  # type: ignore[arg-type]

    def test_select_strongest_common(self):
        self.assertEqual(
            select_strongest_common({Scheme.MTLS, Scheme.API_KEY},
                                    {Scheme.API_KEY, Scheme.MTLS}),
            Scheme.MTLS,
        )
        self.assertEqual(
            select_strongest_common({Scheme.API_KEY}, {Scheme.API_KEY}),
            Scheme.API_KEY,
        )
        self.assertIsNone(
            select_strongest_common({Scheme.MTLS}, {Scheme.API_KEY}))

    def test_scheme_precedence_ordering(self):
        self.assertGreater(Scheme.MTLS, Scheme.OIDC_BEARER)
        self.assertGreater(Scheme.OIDC_BEARER, Scheme.API_KEY)


if __name__ == "__main__":
    unittest.main()
