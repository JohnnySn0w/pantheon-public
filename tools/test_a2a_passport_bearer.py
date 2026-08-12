"""Tests for tools/a2a/passport_bearer.py — ORG-047 Bearer-JWT adapter (T-2832).

The reject paths carry the security weight: a Bearer verifier that accepts a
token with a bad signature / wrong issuer-audience / expired / replayed is an auth
bypass. Uses PyJWT + cryptography (system-available); skip-guarded.
"""

from __future__ import annotations

import base64
import datetime
import unittest

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    _HAVE = True
except ImportError:  # pragma: no cover
    _HAVE = False

if _HAVE:
    from tools.a2a.did_web import parse_did_document
    from tools.a2a.passport_handshake import AllowlistEntry, Scheme
    from tools.a2a.passport_bearer import (
        InMemoryReplayStore,
        LiveBearerAdapter,
        verify_bearer_jwt,
    )

DID = "did:web:snowis.me"
AUD = "did:web:us.example"
KID = f"{DID}#k1"


def _jwk_from_ec_pub(pub, crv="P-256", alg="ES256", kid=KID):
    nums = pub.public_numbers()
    size = (pub.curve.key_size + 7) // 8
    enc = lambda n: base64.urlsafe_b64encode(n.to_bytes(size, "big")).rstrip(b"=").decode()
    return {"kty": "EC", "crv": crv, "alg": alg, "x": enc(nums.x), "y": enc(nums.y), "kid": kid}


def _doc(jwk, kid=KID, extra_vms=None):
    vms = [{"id": kid, "type": "JsonWebKey2020", "controller": DID, "publicKeyJwk": jwk}]
    if extra_vms:
        vms.extend(extra_vms)
    return parse_did_document(
        {"id": DID, "verificationMethod": vms, "authentication": [kid]}, DID)


def _token(signing_key, kid=KID, *, iss=DID, aud=AUD, jti="j1",
           exp_delta=3600, nbf_delta=0, alg="ES256"):
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {"iss": iss, "aud": aud, "jti": jti, "iat": now,
              "nbf": now + datetime.timedelta(seconds=nbf_delta),
              "exp": now + datetime.timedelta(seconds=exp_delta)}
    return jwt.encode(claims, signing_key, algorithm=alg, headers={"kid": kid})


@unittest.skipUnless(_HAVE, "pyjwt/cryptography not available")
class TestVerifyBearerJwt(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.doc = _doc(_jwk_from_ec_pub(self.key.public_key()))
        self.store = InMemoryReplayStore()

    def _verify(self, token, **kw):
        kw.setdefault("audience", AUD)
        kw.setdefault("replay_store", self.store)
        return verify_bearer_jwt(token, self.doc, **kw)

    def test_accepts_valid_token(self):
        self.assertTrue(self._verify(_token(self.key)))

    def test_rejects_wrong_signing_key(self):
        # THE bypass test: signed by a key the DID does NOT authenticate.
        other = ec.generate_private_key(ec.SECP256R1())
        self.assertFalse(self._verify(_token(other)))

    def test_rejects_expired(self):
        self.assertFalse(self._verify(_token(self.key, exp_delta=-3600)))

    def test_rejects_not_yet_valid(self):
        self.assertFalse(self._verify(_token(self.key, nbf_delta=3600)))

    def test_rejects_wrong_audience(self):
        self.assertFalse(self._verify(_token(self.key, aud="did:web:someone-else")))

    def test_rejects_wrong_issuer(self):
        self.assertFalse(self._verify(_token(self.key, iss="did:web:evil.com")))

    def test_rejects_replayed_jti(self):
        t = _token(self.key, jti="once")
        self.assertTrue(self._verify(t))          # first use consumes it
        # A fresh token with the SAME jti is a replay.
        t2 = _token(self.key, jti="once")
        self.assertFalse(self._verify(t2))

    def test_rejects_alg_pin_mismatch(self):
        # token is ES256 but the verifier pins ES384.
        self.assertFalse(self._verify(_token(self.key), required_algorithm="ES384"))

    def test_rejects_kid_not_in_authentication(self):
        # A key present in verificationMethod but NOT in authentication cannot
        # sign a presenter token.
        nonauth = ec.generate_private_key(ec.SECP256R1())
        nonauth_jwk = _jwk_from_ec_pub(nonauth.public_key(), kid=f"{DID}#k2")
        doc = _doc(_jwk_from_ec_pub(self.key.public_key()),
                   extra_vms=[{"id": f"{DID}#k2", "type": "JsonWebKey2020",
                               "controller": DID, "publicKeyJwk": nonauth_jwk}])
        self.assertFalse(verify_bearer_jwt(
            _token(nonauth, kid=f"{DID}#k2"), doc,
            audience=AUD, replay_store=self.store))

    def test_rejects_missing_required_claim(self):
        # Hand-build a token with no jti (require=[...] must refuse it).
        now = datetime.datetime.now(datetime.timezone.utc)
        t = jwt.encode({"iss": DID, "aud": AUD, "iat": now,
                        "exp": now + datetime.timedelta(hours=1)},
                       self.key, algorithm="ES256", headers={"kid": KID})
        self.assertFalse(self._verify(t))

    def test_rejects_garbage(self):
        self.assertFalse(self._verify("not.a.jwt"))

    def test_rejects_alg_none(self):
        # T-2867 BEARER-2: an unsigned (alg=none) token must be refused by the D2
        # alg pin BEFORE any key is consulted — the classic "strip the signature"
        # downgrade. (Live-enforced; this regression-locks it.)
        now = datetime.datetime.now(datetime.timezone.utc)
        claims = {"iss": DID, "aud": AUD, "jti": "jn", "iat": now,
                  "nbf": now, "exp": now + datetime.timedelta(hours=1)}
        token = jwt.encode(claims, key="", algorithm="none", headers={"kid": KID})
        self.assertFalse(self._verify(token))

    def test_rejects_hs256_algorithm_confusion(self):
        # T-2867 BEARER-2: algorithm-confusion forgery — sign HS256 using the DID's
        # EC PUBLIC key bytes as the HMAC secret, the attack that lands if a verifier
        # feeds the public key to a symmetric verify. The alg pin (HS256 != ES256)
        # must refuse before the key is ever used.
        from cryptography.hazmat.primitives import serialization
        # Raw uncompressed EC point (no PEM markers) so it passes as an HMAC secret —
        # PyJWT's encode blocks a PEM key here, but a raw-bytes secret is exactly the
        # attacker's move; the verifier's alg pin is the real defense under test.
        raw_pub = self.key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        now = datetime.datetime.now(datetime.timezone.utc)
        claims = {"iss": DID, "aud": AUD, "jti": "jc", "iat": now,
                  "nbf": now, "exp": now + datetime.timedelta(hours=1)}
        forged = jwt.encode(claims, key=raw_pub, algorithm="HS256", headers={"kid": KID})
        self.assertFalse(self._verify(forged))

    def test_rejects_each_required_claim_missing(self):
        # T-2867 BEARER-3: iss/aud/exp/jti each individually absent must refuse
        # (only jti-missing was covered before). Live-enforced; regression-locks each.
        now = datetime.datetime.now(datetime.timezone.utc)
        full = {"iss": DID, "aud": AUD, "jti": "jm", "iat": now,
                "nbf": now, "exp": now + datetime.timedelta(hours=1)}
        for drop in ("iss", "aud", "exp", "jti"):
            claims = {k: v for k, v in full.items() if k != drop}
            token = jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": KID})
            with self.subTest(missing=drop):
                self.assertFalse(self._verify(token))


@unittest.skipUnless(_HAVE, "pyjwt/cryptography not available")
class TestLiveBearerAdapter(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.doc = _doc(_jwk_from_ec_pub(self.key.public_key()))
        self.entry = AllowlistEntry(card_hash="sha256:x")

    def test_scheme_is_oidc_bearer(self):
        a = LiveBearerAdapter(_token(self.key), self.doc, audience=AUD)
        self.assertEqual(a.scheme, Scheme.OIDC_BEARER)

    def test_verify_happy(self):
        a = LiveBearerAdapter(_token(self.key), self.doc, audience=AUD)
        self.assertTrue(a.verify(DID, self.entry))

    def test_verify_rejects_did_mismatch(self):
        a = LiveBearerAdapter(_token(self.key), self.doc, audience=AUD)
        self.assertFalse(a.verify("did:web:evil.com", self.entry))

    def test_composes_into_handshake_admit(self):
        from tools.a2a.passport_handshake import (
            PassportHandshake, TxtResolution, compute_card_hash, Outcome)
        # Card advertises ONLY http-bearer, so step 2 selects OIDC_BEARER.
        card = b'{"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}}'
        h = compute_card_hash(card)
        hs = PassportHandshake(
            txt_resolver=lambda e: TxtResolution(True, True, "https://x/card", h),
            card_fetcher=lambda u: card,
            allowlist={DID: AllowlistEntry(card_hash=h)},
            adapters={Scheme.OIDC_BEARER: LiveBearerAdapter(
                _token(self.key), self.doc, audience=AUD)},
        )
        r = hs.run(DID)
        self.assertEqual(r.outcome, Outcome.ADMITTED)
        self.assertEqual(r.scheme, Scheme.OIDC_BEARER)
        self.assertFalse(r.content_trusted)


if __name__ == "__main__":
    unittest.main()
