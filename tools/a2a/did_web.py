"""ORG-047 passport — did:web resolution + authentication-key extraction.

The foundation both concrete scheme adapters (T-2832) need: given a counterparty
`did:web:...` identity, produce the URL of its DID document, parse that document,
and extract the public keys in the `authentication` verification relationship
(ADR ORG-047 D2, line 48; D3 lines 52-53 — the mTLS client cert AND the Bearer
JWT both bind to a DID `authentication` key).

PURE-LOCAL. No network: `resolve_did_web_url` is a pure string transform, and
parsing takes the already-fetched document bytes/dict (the fetch is injected by
the adapter). So this module runs in the system smoke suite with no SDK/network.

D2 enforcement is VERIFIER-SIDE (ADR line 48): the DID document carries only the
DID-Core-standard parts (the verification-relationship partition + a distinct
`kid`/`id` per method); the per-scheme ALGORITHM allowlist is a local annotation
the verifier applies — so this module surfaces each method's declared algorithm
and the caller (the adapter) pins the expected alg for its scheme, rejecting a
cross-protocol presentation regardless of what the document declares.

Fail-closed throughout: a malformed document, an id/controller mismatch, an
unresolvable authentication reference, or a key with no usable material raises
DidResolutionError rather than yielding a partial/guessed result.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


class DidResolutionError(Exception):
    """A did:web identity or document that cannot be resolved/parsed fail-closed."""


# ---------------------------------------------------------------------------
# did:web -> DID-document URL (pure transform, per the did:web method spec).
# ---------------------------------------------------------------------------

_DID_WEB_PREFIX = "did:web:"


def resolve_did_web_url(did: str) -> str:
    """Map a `did:web:...` identifier to the HTTPS URL of its DID document.

    Per the did:web method spec:
      - did:web:example.com            -> https://example.com/.well-known/did.json
      - did:web:example.com:a:b        -> https://example.com/a/b/did.json
      - a percent-encoded ':' in the first segment is a PORT
        (did:web:example.com%3A3000   -> https://example.com:3000/.well-known/did.json)
    Path segments are percent-decoded per-segment. HTTPS only (the method
    mandates it) — there is no http fallback (mirrors D1's no-unsigned-DNS
    fail-closed posture).
    """
    if not isinstance(did, str) or not did.startswith(_DID_WEB_PREFIX):
        raise DidResolutionError(f"not a did:web identifier: {did!r}")
    method_specific = did[len(_DID_WEB_PREFIX):]
    if not method_specific:
        raise DidResolutionError("did:web with empty method-specific id")
    segments = method_specific.split(":")
    # First segment = host (with an optional percent-encoded :port).
    host = urllib.parse.unquote(segments[0])
    if not host or "/" in host:
        raise DidResolutionError(f"did:web invalid host segment: {segments[0]!r}")
    # A bare host must look like a domain[:port] — reject obviously-malformed.
    if not re.fullmatch(r"[A-Za-z0-9.\-]+(:[0-9]+)?", host):
        raise DidResolutionError(f"did:web host is not a valid domain[:port]: {host!r}")
    rest = segments[1:]
    if rest:
        decoded = [urllib.parse.unquote(seg) for seg in rest]
        for raw, seg in zip(rest, decoded):
            # T-2867 DIDWEB-3: a decoded segment that is empty, "." / ".." , or
            # carries a slash (e.g. a percent-encoded %2F) is a path-traversal / URL
            # injection vector. Non-exploitable today (the fetched doc's id == did is
            # checked twice downstream) but guarded here rather than relying on that.
            if seg in ("", ".", "..") or "/" in seg:
                raise DidResolutionError(
                    f"did:web has an unsafe path segment {raw!r} (decodes to {seg!r})")
        path = "/".join(decoded)
        return f"https://{host}/{path}/did.json"
    return f"https://{host}/.well-known/did.json"


# ---------------------------------------------------------------------------
# Verification methods + the authentication relationship.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationMethod:
    """One DID-document verification method (a key), plus its declared algorithm.

    `algorithm` is best-effort from the key material (a JWK's `alg`, else derived
    from `crv`/`kty`), else the method `type`. It is ADVISORY here — the verifier
    (adapter) pins the alg it REQUIRES for its scheme and rejects a mismatch;
    this field just surfaces what the document declares (D2 line 48).
    """

    id: str                       # the `kid` — MUST be distinct per method (D2)
    type: str                     # e.g. "JsonWebKey2020", "Multikey"
    controller: str
    public_key_jwk: dict | None = None
    public_key_multibase: str | None = None
    algorithm: str | None = None


def _derive_algorithm(vm: dict) -> str | None:
    jwk = vm.get("publicKeyJwk")
    if isinstance(jwk, dict):
        if isinstance(jwk.get("alg"), str):
            return jwk["alg"]
        # No explicit alg — derive a stable label from curve/key-type so the
        # verifier still has something to pin against.
        crv = jwk.get("crv")
        kty = jwk.get("kty")
        if isinstance(crv, str):
            return f"{kty}:{crv}" if isinstance(kty, str) else crv
        if isinstance(kty, str):
            return kty
    typ = vm.get("type")
    return typ if isinstance(typ, str) else None


def _parse_vm(vm: dict) -> VerificationMethod:
    if not isinstance(vm, dict):
        raise DidResolutionError("verificationMethod entry is not an object")
    vid = vm.get("id")
    vtype = vm.get("type")
    controller = vm.get("controller")
    if not isinstance(vid, str) or not vid:
        raise DidResolutionError("verificationMethod missing a string id (kid)")
    if not isinstance(vtype, str) or not vtype:
        raise DidResolutionError(f"verificationMethod {vid!r} missing type")
    if not isinstance(controller, str) or not controller:
        raise DidResolutionError(f"verificationMethod {vid!r} missing controller")
    jwk = vm.get("publicKeyJwk")
    multibase = vm.get("publicKeyMultibase")
    jwk = jwk if isinstance(jwk, dict) else None
    multibase = multibase if isinstance(multibase, str) and multibase else None
    if jwk is None and multibase is None:
        raise DidResolutionError(
            f"verificationMethod {vid!r} has no usable key material "
            "(publicKeyJwk / publicKeyMultibase)")
    return VerificationMethod(
        id=vid, type=vtype, controller=controller,
        public_key_jwk=jwk, public_key_multibase=multibase,
        algorithm=_derive_algorithm(vm),
    )


@dataclass(frozen=True)
class ParsedDidDocument:
    did: str
    verification_methods: tuple[VerificationMethod, ...]
    authentication: tuple[VerificationMethod, ...]


def parse_did_document(doc: dict, expected_did: str) -> ParsedDidDocument:
    """Parse + validate a DID document, resolving the `authentication`
    relationship into concrete VerificationMethods. Fail-closed.

    Enforces the D2-relevant structural rules verifier-side:
      - document `id` MUST equal expected_did (the identity we resolved);
      - every verificationMethod `id` (kid) MUST be DISTINCT (D2: distinct kid);
      - each `authentication` entry is either an embedded method or a string
        DID-URL reference that MUST resolve to a top-level verificationMethod
        (an unresolvable reference is fail-closed, never skipped);
      - authentication MUST be non-empty (there is no presenter-auth without it).
    """
    if not isinstance(doc, dict):
        raise DidResolutionError("DID document is not a JSON object")
    doc_id = doc.get("id")
    if doc_id != expected_did:
        raise DidResolutionError(
            f"DID document id {doc_id!r} != resolved identity {expected_did!r}")

    raw_vms = doc.get("verificationMethod", [])
    if not isinstance(raw_vms, list):
        raise DidResolutionError("verificationMethod is not a list")
    methods: list[VerificationMethod] = [_parse_vm(vm) for vm in raw_vms]

    seen: set[str] = set()
    for m in methods:
        if m.id in seen:
            raise DidResolutionError(f"duplicate verificationMethod id (kid): {m.id!r}")
        seen.add(m.id)
    by_id = {m.id: m for m in methods}

    raw_auth = doc.get("authentication", [])
    if not isinstance(raw_auth, list):
        raise DidResolutionError("authentication is not a list")
    auth: list[VerificationMethod] = []
    for entry in raw_auth:
        if isinstance(entry, str):
            ref = by_id.get(entry)
            if ref is None:
                raise DidResolutionError(
                    f"authentication references unknown verificationMethod {entry!r}")
            auth.append(ref)
        elif isinstance(entry, dict):
            m = _parse_vm(entry)
            # T-2867 DIDWEB-1: the distinct-kid invariant (D2) must hold across the
            # top-level verificationMethod pass AND embedded authentication methods —
            # otherwise an embedded auth entry can reuse a kid already bound to a
            # different key, and a first-match-wins consumer (bearer) silently trusts
            # a shadowed/stale key. Fail closed on the collision (same `seen` set).
            if m.id in seen:
                raise DidResolutionError(
                    f"duplicate verificationMethod id (kid) in authentication: {m.id!r}")
            seen.add(m.id)
            auth.append(m)
        else:
            raise DidResolutionError("authentication entry is neither a ref nor a method")
    if not auth:
        raise DidResolutionError(
            "DID document has no authentication key — no presenter-auth possible")

    return ParsedDidDocument(
        did=expected_did,
        verification_methods=tuple(methods),
        authentication=tuple(auth),
    )


def select_authentication_key(
    parsed: ParsedDidDocument, *, required_algorithm: str
) -> VerificationMethod:
    """Return the single authentication key whose declared algorithm matches
    `required_algorithm` (the alg the adapter pins for its scheme — D2 line 48,
    verifier-side enforcement). Fail-closed: no match, or MORE than one match
    (ambiguous — the presenter must be unambiguous), raises."""
    matches = [m for m in parsed.authentication if m.algorithm == required_algorithm]
    # T-2867 DIDWEB-2: the same key can legitimately appear more than once in
    # `authentication` (a string reference listed twice, or a ref plus its embedded
    # form) — that is not ambiguity. Dedup by kid before the ambiguity count;
    # distinct-kid across the doc is guaranteed at parse time (DIDWEB-1), so same id
    # ⇒ same key, while two genuinely DIFFERENT keys matching the alg still raise.
    by_id = {m.id: m for m in matches}
    if not by_id:
        raise DidResolutionError(
            f"no authentication key with algorithm {required_algorithm!r} "
            f"(have: {[m.algorithm for m in parsed.authentication]})")
    if len(by_id) > 1:
        raise DidResolutionError(
            f"ambiguous: {len(by_id)} authentication keys with algorithm "
            f"{required_algorithm!r} — presenter identity must be unambiguous")
    return next(iter(by_id.values()))
