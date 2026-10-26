"""Tests for tools/a2a/passport_mtls.py — ORG-047 mTLS cert<->DID-key binding (T-2832).

The REJECT paths are the point: a binding verifier that accepts a cert whose key
is NOT the DID-authenticated key is an auth bypass, so every mismatch case below
is load-bearing. Uses `cryptography` (system-available); skip-guarded so a runner
without it doesn't show a false failure.
"""

from __future__ import annotations

import base64
import datetime
import unittest

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.x509.oid import NameOID
    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAVE_CRYPTO = False

if _HAVE_CRYPTO:
    from tools.a2a.did_web import parse_did_document
    from tools.a2a.passport_handshake import AllowlistEntry, Scheme
    from tools.a2a.passport_mtls import (
        LiveMtlsAdapter,
        cert_sha256_fingerprint,
        verify_mtls_binding,
    )

DID = "did:web:your-domain.example"


def _jwk_from_ec_pub(pub, crv="P-256", alg="ES256"):
    nums = pub.public_numbers()
    size = (pub.curve.key_size + 7) // 8
    enc = lambda n: base64.urlsafe_b64encode(n.to_bytes(size, "big")).rstrip(b"=").decode()
    return {"kty": "EC", "crv": crv, "alg": alg, "x": enc(nums.x), "y": enc(nums.y)}


def _self_signed_der(key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pantheon-test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2026, 1, 1))
            .not_valid_after(datetime.datetime(2030, 1, 1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


def _did_doc(jwk):
    return {
        "id": DID,
        "verificationMethod": [{"id": f"{DID}#k1", "type": "JsonWebKey2020",
                                "controller": DID, "publicKeyJwk": jwk}],
        "authentication": [f"{DID}#k1"],
    }


@unittest.skipUnless(_HAVE_CRYPTO, "cryptography not available")
class TestVerifyMtlsBinding(unittest.TestCase):
    def setUp(self):
        # The legitimate presenter: one EC key, its cert, and the DID doc that
        # authenticates exactly that key.
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cert_der = _self_signed_der(self.key)
        self.jwk = _jwk_from_ec_pub(self.key.public_key())
        self.doc = parse_did_document(_did_doc(self.jwk), DID)
        self.entry = AllowlistEntry(card_hash="sha256:x")

    def test_accepts_bound_cert(self):
        self.assertTrue(verify_mtls_binding(self.cert_der, self.doc, self.entry))

    def test_mtls3_expired_cert_with_right_key_still_binds(self):
        # T-2867 MTLS-3: the trust anchor is DID-key POSSESSION proven by the TLS
        # layer, NOT a PKI — so cert validity dates are DELIBERATELY unchecked. A
        # long-expired cert that carries the DID-authenticated key still binds.
        # This LOCKS that decision so a future reader does not add an expiry check
        # assuming it was merely forgotten (see the module's MTLS-3 comment).
        expired = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired")]))
                   .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired")]))
                   .public_key(self.key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.datetime(2000, 1, 1))
                   .not_valid_after(datetime.datetime(2001, 1, 1))  # long expired
                   .sign(self.key, hashes.SHA256()))
        expired_der = expired.public_bytes(serialization.Encoding.DER)
        self.assertTrue(
            verify_mtls_binding(expired_der, self.doc, self.entry),
            "expired-but-right-key cert must still bind (trust = key possession, not PKI validity)")

    def test_rejects_wrong_key_cert(self):
        # THE bypass test: a cert for a DIFFERENT key must be refused even though
        # it is a perfectly valid cert.
        other = ec.generate_private_key(ec.SECP256R1())
        other_der = _self_signed_der(other)
        self.assertFalse(verify_mtls_binding(other_der, self.doc, self.entry))

    def test_rejects_fingerprint_mismatch(self):
        pinned = AllowlistEntry(card_hash="sha256:x",
                                pinned_mtls_cert_fingerprint="sha256:deadbeef")
        self.assertFalse(verify_mtls_binding(self.cert_der, self.doc, pinned))

    def test_accepts_fingerprint_match(self):
        fp = cert_sha256_fingerprint(self.cert_der)
        pinned = AllowlistEntry(card_hash="sha256:x", pinned_mtls_cert_fingerprint=fp)
        self.assertTrue(verify_mtls_binding(self.cert_der, self.doc, pinned))

    def test_rejects_unparseable_cert(self):
        self.assertFalse(verify_mtls_binding(b"not a cert", self.doc, self.entry))

    def test_rejects_alg_mismatch(self):
        # DID key is published as ES384 but the adapter pins ES256 → no key → refuse.
        jwk384 = dict(self.jwk, alg="ES384")
        doc = parse_did_document(_did_doc(jwk384), DID)
        self.assertFalse(verify_mtls_binding(self.cert_der, doc, self.entry,
                                             required_algorithm="ES256"))

    def test_rejects_rsa_cert(self):
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rsa_der = _self_signed_der(rsa_key)
        # DID authenticates our EC key; an RSA cert can't match it.
        self.assertFalse(verify_mtls_binding(rsa_der, self.doc, self.entry))

    def test_rejects_malformed_base64_jwk_coordinate_no_raise(self):
        # T-2867 MTLS-1: a counterparty-controlled JWK with non-base64 x makes
        # _b64url_to_int raise binascii.Error. It MUST be refused (return False),
        # not escape verify_mtls_binding → PassportHandshake.run() (contract
        # violation + per-handshake DoS). assertFalse also proves it does not raise.
        bad = dict(self.jwk, x="@@@not-base64@@@")
        doc = parse_did_document(_did_doc(bad), DID)
        self.assertFalse(verify_mtls_binding(self.cert_der, doc, self.entry))

    def test_rejects_off_curve_jwk_point_no_raise(self):
        # T-2867 MTLS-1: an off-curve point (x=1,y=1 on P-256) makes public_key()
        # raise ValueError; it must fail closed (False), not escape run().
        offcurve = dict(self.jwk, x="AQ", y="AQ")  # base64url of int 1 / int 1
        doc = parse_did_document(_did_doc(offcurve), DID)
        self.assertFalse(verify_mtls_binding(self.cert_der, doc, self.entry))


@unittest.skipUnless(_HAVE_CRYPTO, "cryptography not available")
class TestLiveMtlsAdapter(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cert_der = _self_signed_der(self.key)
        self.jwk = _jwk_from_ec_pub(self.key.public_key())
        self.doc = parse_did_document(_did_doc(self.jwk), DID)
        self.entry = AllowlistEntry(card_hash="sha256:x")

    def test_scheme_is_mtls(self):
        a = LiveMtlsAdapter(self.cert_der, self.doc)
        self.assertEqual(a.scheme, Scheme.MTLS)

    def test_verify_happy(self):
        a = LiveMtlsAdapter(self.cert_der, self.doc)
        self.assertTrue(a.verify(DID, self.entry))

    def test_verify_rejects_did_mismatch(self):
        # The adapter's parsed doc is for DID, but the handshake passes a
        # different endpoint — refuse (don't verify against the wrong identity).
        a = LiveMtlsAdapter(self.cert_der, self.doc)
        self.assertFalse(a.verify("did:web:evil.com", self.entry))

    def test_verify_composes_into_handshake_admit(self):
        # End-to-end: the LiveMtlsAdapter admits through the real D1 handshake.
        from tools.a2a.passport_handshake import (
            PassportHandshake, TxtResolution, compute_card_hash, Outcome)
        card = b'{"securitySchemes": {"m": {"type": "mutualTLS"}}}'
        h = compute_card_hash(card)
        hs = PassportHandshake(
            txt_resolver=lambda e: TxtResolution(True, True, "https://x/card", h),
            card_fetcher=lambda u: card,
            allowlist={DID: AllowlistEntry(card_hash=h)},
            adapters={Scheme.MTLS: LiveMtlsAdapter(self.cert_der, self.doc)},
        )
        r = hs.run(DID)
        self.assertEqual(r.outcome, Outcome.ADMITTED)
        self.assertEqual(r.scheme, Scheme.MTLS)
        self.assertFalse(r.content_trusted)  # D4 invariant


if __name__ == "__main__":
    unittest.main()
