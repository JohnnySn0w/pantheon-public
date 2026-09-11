"""ORG-047 passport — Bearer (HTTP-Bearer-profile JWT) scheme adapter (T-2832).

The concrete Bearer presenter-auth for ADR D3 line 53 case (a): a SELF-CONTAINED
JWT signed under a DID `authentication` key, verified by resolving the did:web
document — explicitly "an HTTP Bearer scheme, NOT OIDC". (True OIDC/OAuth2 — case
(b), issuer-metadata discovery + external IdP — is a separate follow-up.) Composes
did_web.py (increment 1): resolve DID -> authentication key -> verify the JWS.

Trust model mirrors the mTLS adapter: the trust anchor is the DID document
(DNSSEC-pinned via TG-869 in D1 step 1), so the JWT is trusted iff it is signed by
the key the DID `authentication` relationship publishes (selected by the token's
`kid`, with the D2 verifier-side alg pin), and its claims validate — iss == the
counterparty DID, aud == us, exp/nbf within a clock-skew leeway, and a fresh `jti`
(replay defence — the freshness/replay checks the T-2018 triage routed here).

PURE-LOCAL + fail-closed: the token is passed as a string (the injected
presentation context); EVERY failure — bad signature, wrong/unknown kid, alg
mismatch, expired, wrong aud/iss, replayed jti, malformed — returns False, never
True-on-error. Uses PyJWT (system-available), skip-guarded tests.

NOTE: clock-skew leeway is a validation PARAMETER (default 60s), not a deferred
T-2803 credential value. Seat-credential TTL / issuance / revocation stay out of
scope here (T-2803/T-2810/T-2811). NEEDS the same independent adversarial review
as the Layer A core before gating real traffic.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import jwt
from jwt import InvalidTokenError, PyJWK

from tools.a2a.did_web import DidResolutionError, ParsedDidDocument
from tools.a2a.passport_handshake import AllowlistEntry, Scheme

logger = logging.getLogger("pantheon.a2a.passport_bearer")

DEFAULT_BEARER_ALGORITHM = "ES256"
DEFAULT_LEEWAY_SECONDS = 60


@runtime_checkable
class ReplayStore(Protocol):
    """Tracks consumed JWT `jti`s so a captured token cannot be replayed."""

    def seen(self, jti: str) -> bool: ...

    def record(self, jti: str) -> None: ...


class InMemoryReplayStore:
    """Default ReplayStore. Not persistent (a process restart forgets consumed
    jtis); combined with the exp window that is an acceptable v1 — a durable
    store is a later concern. Interface injected so the swap is a one-liner."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, jti: str) -> bool:
        return jti in self._seen

    def record(self, jti: str) -> None:
        self._seen.add(jti)


def verify_bearer_jwt(
    token: str,
    parsed_doc: ParsedDidDocument,
    *,
    audience: str,
    replay_store: ReplayStore,
    required_algorithm: str = DEFAULT_BEARER_ALGORITHM,
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
) -> bool:
    """True iff `token` is a valid self-contained JWT bound to the counterparty
    DID's authentication key and its claims validate (iss/aud/exp/nbf/jti).
    Fail-closed: returns False on ANY failure; never raises for a verification
    failure."""
    if not isinstance(token, str) or not token:
        return False
    # Unverified header first, to find the kid and pin the alg BEFORE trusting
    # the signature (standard JWS verify order).
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        logger.warning("bearer: unparseable JWT header: %s", exc)
        return False

    alg = header.get("alg")
    kid = header.get("kid")
    if alg != required_algorithm:
        logger.warning("bearer: token alg %r != pinned %r", alg, required_algorithm)
        return False
    if not isinstance(kid, str) or not kid:
        logger.warning("bearer: token has no kid")
        return False

    # The kid MUST name a key in the DID authentication relationship, and that
    # key's declared alg MUST match the pin (D2 verifier-side).
    vm = next((m for m in parsed_doc.authentication if m.id == kid), None)
    if vm is None:
        logger.warning("bearer: kid %r is not a DID authentication key", kid)
        return False
    if vm.algorithm != required_algorithm:
        logger.warning("bearer: DID key %r alg %r != pinned %r",
                       kid, vm.algorithm, required_algorithm)
        return False
    if not vm.public_key_jwk:
        logger.warning("bearer: DID key %r has no JWK", kid)
        return False

    try:
        pyjwk = PyJWK.from_dict(vm.public_key_jwk)
    except (InvalidTokenError, DidResolutionError, Exception) as exc:  # noqa: BLE001
        # T-2867 BEARER-4: the bare `Exception` is LOAD-BEARING, not lazy. PyJWK
        # .from_dict raises a zoo of types on a counterparty-controlled JWK —
        # PyJWKError / InvalidKeyError, plus KeyError/ValueError/TypeError from
        # malformed dict shapes — and this key material comes from the (trusted-
        # but-still-parsed) DID doc. Any of them must FAIL CLOSED (return False),
        # never escape and violate verify()'s no-raise contract. The named
        # types are listed first for intent; `Exception` is the fail-closed net.
        logger.warning("bearer: cannot build key from JWK: %r", exc)
        return False

    # Full verify: signature + iss(==DID) + aud(==us) + exp/nbf (with leeway).
    # require= forces the security-relevant claims to be PRESENT, not just valid
    # if present — a token missing exp/iss/aud/jti is refused.
    try:
        claims = jwt.decode(
            token,
            pyjwk.key,
            algorithms=[required_algorithm],
            audience=audience,
            issuer=parsed_doc.did,
            leeway=leeway_seconds,
            options={"require": ["exp", "iss", "aud", "jti"]},
        )
    except InvalidTokenError as exc:
        logger.warning("bearer: JWT verification failed: %s", exc)
        return False

    # Replay defence: a fresh jti, consumed exactly once.
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        return False
    if replay_store.seen(jti):
        logger.warning("bearer: jti %r already consumed — replay refused", jti)
        return False
    replay_store.record(jti)
    return True


class LiveBearerAdapter:
    """SchemeAdapter that admits a Bearer presenter via verify_bearer_jwt.

    Constructed with its presentation context — the presented token, the parsed
    counterparty DID document, our audience, and the (shared) replay store —
    since the pure handshake core carries no live request. Occupies the
    OIDC_BEARER precedence slot (the HTTP-Bearer profile; true OIDC is separate).
    """

    def __init__(
        self,
        token: str,
        parsed_doc: ParsedDidDocument,
        *,
        audience: str,
        replay_store: ReplayStore | None = None,
        required_algorithm: str = DEFAULT_BEARER_ALGORITHM,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    ) -> None:
        self._token = token
        self._doc = parsed_doc
        self._audience = audience
        self._replay = replay_store or InMemoryReplayStore()
        self._alg = required_algorithm
        self._leeway = leeway_seconds

    @property
    def scheme(self) -> Scheme:
        return Scheme.OIDC_BEARER

    def verify(self, endpoint_id: str, allowlist_entry: AllowlistEntry) -> bool:
        if self._doc.did != endpoint_id:
            logger.warning("bearer: adapter DID %r != handshake endpoint %r",
                           self._doc.did, endpoint_id)
            return False
        return verify_bearer_jwt(
            self._token, self._doc,
            audience=self._audience, replay_store=self._replay,
            required_algorithm=self._alg, leeway_seconds=self._leeway)
