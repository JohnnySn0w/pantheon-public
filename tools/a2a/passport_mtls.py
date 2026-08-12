"""ORG-047 passport — mTLS scheme adapter: client-cert <-> DID-key binding (T-2832).

The concrete mTLS presenter-auth (ADR D3, line 52: a client cert bound to a DID
`authentication` key), composing the did:web resolution from did_web.py. This is
the FIRST scheme adapter (D6 build order) — it lets step 3 of the D1 handshake
actually admit an mTLS presenter.

THE BINDING, and why it is the trust: the counterparty presents a client cert
during the TLS handshake. We do NOT trust it via a PKI CA chain — the trust
anchor is the DID document (itself DNSSEC-pinned via the TG-869 card hash in step
1). So the check is: the presented cert's public key MUST EQUAL the public key of
the counterparty DID's `authentication` verificationMethod (for the pinned alg),
AND the cert's fingerprint MUST match the allowlist's pinned_mtls_cert_fingerprint
when one is set. A cert whose key is not the DID-authenticated key is refused no
matter how it chains.

D2 line 48 (verifier-side alg pin): the adapter pins the algorithm it requires
(ES256 by default) and selects the DID authentication key for exactly that alg,
so a key published for a different scheme/alg cannot be presented here.

PURE-LOCAL + fail-closed: the presented cert is passed as DER bytes (the adapter's
injected presentation context), the DID document is the already-parsed
ParsedDidDocument, and EVERY failure path — unparseable cert, unsupported key
type, key mismatch, fingerprint mismatch, alg with no/ambiguous key — returns
False (never True-on-error). Uses `cryptography` (system-available), so it runs
in the system smoke.

SCOPE: EC keys (ES256/384/512 — the ADR's likely mTLS alg). RSA and the live
TLS-session plumbing that hands us the peer cert are follow-ups. NEEDS the same
independent adversarial review as the Layer A core before it gates real traffic.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tools.a2a.did_web import (
    DidResolutionError,
    ParsedDidDocument,
    VerificationMethod,
    select_authentication_key,
)
from tools.a2a.passport_handshake import AllowlistEntry, Scheme

logger = logging.getLogger("pantheon.a2a.passport_mtls")

# Default alg the mTLS adapter pins. EC P-256 / ES256 is the conventional choice
# for a client-cert key; overridable per deployment.
DEFAULT_MTLS_ALGORITHM = "ES256"

# JWK `crv` -> cryptography curve, for the EC algs we support.
_CRV_TO_CURVE = {
    "P-256": ec.SECP256R1,
    "P-384": ec.SECP384R1,
    "P-521": ec.SECP521R1,
}


def _b64url_to_int(val: str) -> int:
    """Decode a base64url (unpadded) JWK coordinate to an int, fail-closed."""
    if not isinstance(val, str) or not val:
        raise DidResolutionError("JWK coordinate is not a non-empty string")
    pad = "=" * (-len(val) % 4)
    raw = base64.urlsafe_b64decode(val + pad)
    return int.from_bytes(raw, "big")


def _ec_public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    """Reconstruct an EC public key from a JWK, fail-closed on anything odd."""
    if not isinstance(jwk, dict):
        raise DidResolutionError("verification method has no JWK for mTLS binding")
    if jwk.get("kty") != "EC":
        raise DidResolutionError(f"mTLS binding supports EC keys only, got kty={jwk.get('kty')!r}")
    crv = jwk.get("crv")
    curve_cls = _CRV_TO_CURVE.get(crv)
    if curve_cls is None:
        raise DidResolutionError(f"unsupported/absent EC curve for mTLS: {crv!r}")
    # T-2867 MTLS-1: the x/y decode + point construction operate on
    # counterparty-controlled did.json. _b64url_to_int raises binascii.Error on
    # malformed base64, and public_key() raises ValueError on an off-curve point
    # (e.g. x=1,y=1 on P-256) — neither a DidResolutionError. Left to escape, they
    # break this helper's "fail-closed on anything odd" contract, surface out of
    # verify_mtls_binding (which the caller expects to only ever return False), and
    # ultimately out of PassportHandshake.run() (a "never raises" contract violation
    # + per-handshake DoS by an already-enrolled counterparty). Convert any such
    # failure to DidResolutionError so the caller's existing except fails it closed.
    try:
        x = _b64url_to_int(jwk.get("x"))
        y = _b64url_to_int(jwk.get("y"))
        return ec.EllipticCurvePublicNumbers(x, y, curve_cls()).public_key()
    except DidResolutionError:
        raise
    except Exception as exc:  # malformed base64 x/y, off-curve point, etc.
        raise DidResolutionError(f"unusable EC key material in JWK: {exc!r}") from exc


def _spki(pubkey) -> bytes:
    """Canonical SubjectPublicKeyInfo DER for byte-exact public-key comparison."""
    return pubkey.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def cert_sha256_fingerprint(cert_der: bytes) -> str:
    """Algorithm-tagged sha256 fingerprint over the cert DER (matches the
    allowlist's pinned_mtls_cert_fingerprint format)."""
    return f"sha256:{hashlib.sha256(cert_der).hexdigest()}"


def verify_mtls_binding(
    presented_cert_der: bytes,
    parsed_doc: ParsedDidDocument,
    allowlist_entry: AllowlistEntry,
    *,
    required_algorithm: str = DEFAULT_MTLS_ALGORITHM,
) -> bool:
    """True iff the presented client cert is bound to the counterparty DID's
    authentication key AND (when pinned) matches the allowlist cert fingerprint.

    Fail-closed: returns False on ANY failure (unparseable cert, unsupported key,
    key mismatch, fingerprint mismatch, missing/ambiguous alg key). Never raises
    for a verification failure — the caller (adapter/handshake) treats False as
    a refused presentation.
    """
    try:
        cert = x509.load_der_x509_certificate(presented_cert_der)
    except Exception as exc:  # malformed cert bytes
        logger.warning("mtls: unparseable presented cert: %r", exc)
        return False

    # Pick the DID authentication key for the pinned alg (fail-closed on
    # no-match / ambiguous — select_authentication_key raises, caught below).
    try:
        vm: VerificationMethod = select_authentication_key(
            parsed_doc, required_algorithm=required_algorithm)
        did_pubkey = _ec_public_key_from_jwk(vm.public_key_jwk)
    except DidResolutionError as exc:
        logger.warning("mtls: no usable DID authentication key: %s", exc)
        return False

    # THE BINDING: the cert's public key must be exactly the DID-authenticated key.
    cert_pubkey = cert.public_key()
    if not isinstance(cert_pubkey, ec.EllipticCurvePublicKey):
        logger.warning("mtls: presented cert is not an EC key")
        return False
    if _spki(cert_pubkey) != _spki(did_pubkey):
        logger.warning("mtls: presented cert key != DID authentication key — refused")
        return False

    # Fingerprint pin (when the allowlist sets one): the cert must be the exact
    # pinned cert, not merely a cert carrying the right key.
    pin = allowlist_entry.pinned_mtls_cert_fingerprint
    if pin:
        if cert_sha256_fingerprint(presented_cert_der) != pin:
            logger.warning("mtls: presented cert fingerprint != allowlist pin — refused")
            return False

    return True


class LiveMtlsAdapter:
    """SchemeAdapter that admits an mTLS presenter via verify_mtls_binding.

    Constructed with its presentation context — the presented client cert (DER)
    and the already-parsed counterparty DID document — since the pure handshake
    core has no live TLS session. verify() consults the allowlist entry the
    handshake passes. Replaces the Layer-A structural MtlsAdapter skeleton once
    the TLS-session plumbing that supplies `presented_cert_der` lands.
    """

    def __init__(
        self,
        presented_cert_der: bytes,
        parsed_doc: ParsedDidDocument,
        *,
        required_algorithm: str = DEFAULT_MTLS_ALGORITHM,
    ) -> None:
        self._cert_der = presented_cert_der
        self._doc = parsed_doc
        self._alg = required_algorithm

    @property
    def scheme(self) -> Scheme:
        return Scheme.MTLS

    def verify(self, endpoint_id: str, allowlist_entry: AllowlistEntry) -> bool:
        # endpoint_id is the DID we resolved; the parsed_doc was resolved for it.
        if self._doc.did != endpoint_id:
            logger.warning("mtls: adapter DID %r != handshake endpoint %r",
                           self._doc.did, endpoint_id)
            return False
        return verify_mtls_binding(
            self._cert_der, self._doc, allowlist_entry,
            required_algorithm=self._alg)
