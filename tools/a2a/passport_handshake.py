"""ORG-047 cross-seed passport — Layer A: the D1 handshake state machine + verifier.

Ratified spec: adr/ORG-047-cross-seed-passport-a2a-federated-identity.md,
decision D1 (lines 36-42, the ordered fail-closed handshake), the scheme
strength precedence (line 38: mTLS > OIDC/Bearer > API-key), D2 (line 48,
per-scheme algorithm allowlist enforced VERIFIER-SIDE), and D4 (lines 61-62,
a verified identity gates *who*, never *what*). The TG-869 card-hash + manual
allowlist primitive it composes is specified in
knowledge/episodic/2026-07-20-a2a-rung1-design/notes.md lines 168-173 and
knowledge/episodic/2026-07-24-cross-seed-passport-design/notes.md lines 29-35,
56, 111 — this module is its first implementation. Ticket: T-2830 (Layer A of
T-2018 / ORG-047).

Scope — Layer A is the reversible, exposure-free, value-free security core:

  * PURE-LOCAL. No network, no a2a-sdk, no clock, no filesystem in the core.
    Every side-effecting dependency (DNSSEC-validated TXT resolution, Agent
    Card fetch, the local allowlist, the per-endpoint downgrade memory, the
    scheme adapters) is INJECTED, so the whole state machine is hermetically
    testable and runs in the system smoke suite. ORG-047 line 89 confirms this
    code is additive/removable — nothing here is a one-way door.

  * FAIL-CLOSED BY CONSTRUCTION. Every precondition that is absent, invalid, or
    merely unproven ABORTs and logs the failing precondition; there is no
    partial-trust state (ADR line 42). Production cannot false-admit: step 3
    admits only through a registered scheme adapter whose verify() succeeds, and
    no real adapter ships in Layer A (the concrete mTLS X.509 / Bearer JWS
    crypto is the follow-up sub-ticket), so with the real (empty) adapter set
    step 2 finds no common scheme and the handshake refuses.

Explicitly NOT in Layer A (do not add here): the Layer B credential/key VALUES
(key-custody, seat-cred TTL, deny-list values, StatusList2021 revocation —
T-2803/T-2810/T-2811), the Layer C operator-gated public DNS/serving publish,
and the full scheme-adapter presentation crypto (mTLS cert<->DID-key binding,
Bearer iss/aud/replay/clock-skew validation). The presentation-time freshness /
clock-skew checks belong to those concrete adapters (step 3), which plug into
the SchemeAdapter interface defined below.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger("pantheon.a2a.passport_handshake")


# ---------------------------------------------------------------------------
# Schemes + strength precedence (ORG-047 D1, line 38).
# ---------------------------------------------------------------------------


class Scheme(IntEnum):
    """A2A security schemes in the ORG-047 D1 LINEAR strength precedence.

    Higher value = stronger. select_strongest_common() picks the highest.
    Line 38: "mTLS > OIDC/Bearer > API-key". Line 55: API-key is the weakest,
    associative-only (its DID association is a side-channel lookup, not a
    cryptographic binding).

    Verifiable-Credential (VC/VP) is DELIBERATELY absent from this linear
    precedence: it is offered only to capability-"kin" (D3, lines 54/57), a
    kin-predicate gate that is out of Layer A scope. Adding VC here would
    corrupt the linear ordering; it is a follow-up when a kin counterparty
    needs it.
    """

    API_KEY = 1
    OIDC_BEARER = 2
    MTLS = 3


# Mapping from A2A/OpenAPI `securityScheme` shapes to our precedence slots.
# The card's securitySchemes is a dict of name -> {type, ...}. ADR line 53
# splits Bearer into an HTTP-Bearer profile (self-contained JWT) and true
# OIDC/OAuth2; both occupy the single OIDC_BEARER precedence slot here (their
# DISTINCTION matters to the adapter that verifies the presentation, not to the
# strength ordering). Unknown/unsupported types are ignored — never guessed
# into a slot (fail-closed: a scheme we cannot name is a scheme we cannot pick).
def parse_advertised_schemes(card: dict) -> set[Scheme]:
    """Extract the set of Scheme values a counterparty Agent Card advertises.

    Defensive against malformed cards: a non-dict card, a missing/!dict
    `securitySchemes`, or a scheme entry without a recognised `type` simply
    contributes nothing. Returns the empty set rather than raising, so the
    caller's "no common scheme -> ABORT" path (never an exception) governs.
    """
    out: set[Scheme] = set()
    if not isinstance(card, dict):
        return out
    schemes = card.get("securitySchemes")
    if not isinstance(schemes, dict):
        return out
    for entry in schemes.values():
        if not isinstance(entry, dict):
            continue
        typ = str(entry.get("type", "")).strip().lower()
        if typ in ("mutualtls", "mtls"):
            out.add(Scheme.MTLS)
        elif typ in ("openidconnect", "oauth2"):
            out.add(Scheme.OIDC_BEARER)
        elif typ == "http":
            # http-basic is not a supported presentation scheme; only bearer
            # maps to the OIDC_BEARER slot (the self-contained-JWT profile).
            if str(entry.get("scheme", "")).strip().lower() == "bearer":
                out.add(Scheme.OIDC_BEARER)
        elif typ == "apikey":
            out.add(Scheme.API_KEY)
        # else: unknown type — ignored on purpose.
    return out


def select_strongest_common(
    counterparty: set[Scheme], supported: set[Scheme]
) -> Scheme | None:
    """Strongest scheme common to both sides, or None if the intersection is
    empty. The precedence is the IntEnum ordering (max)."""
    common = counterparty & supported
    return max(common) if common else None


# ---------------------------------------------------------------------------
# Card hash (TG-869): tamper-evident pin over the fetched Agent Card bytes.
# ---------------------------------------------------------------------------

_CARD_HASH_ALG = "sha256"


def compute_card_hash(card_bytes: bytes) -> str:
    """Return the algorithm-tagged hash of the EXACT bytes served for the Agent
    Card, e.g. "sha256:ab12…". The DNSSEC-signed TXT pins this same value over
    the same served bytes (TG-869), so the comparison is byte-exact — there is
    deliberately no JSON re-canonicalisation here: the pin is over what the wire
    delivered, and any producer/consumer canonicalisation must happen before
    the value is pinned, identically on both ends.
    """
    if not isinstance(card_bytes, (bytes, bytearray)):
        raise TypeError("card_bytes must be bytes")
    return f"{_CARD_HASH_ALG}:{hashlib.sha256(bytes(card_bytes)).hexdigest()}"


# ---------------------------------------------------------------------------
# Injected-dependency data + interfaces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TxtResolution:
    """The outcome of resolving an endpoint's DNSSEC-signed TXT (SRV-pointed).

    The resolver reports FACTS; this module's core enforces the fail-closed
    policy on them (defence in depth — the core never trusts the resolver to
    have pre-filtered). `present` is False when no TXT exists; `dnssec_validated`
    is True ONLY when the resolver cryptographically validated the DNSSEC chain
    (the core ABORTs on False and never falls back to unsigned DNS — ADR line
    37). `card_url` is the advertised Agent Card URL and `pinned_card_hash` the
    algorithm-tagged hash the TXT pins over the served card bytes.
    """

    present: bool
    dnssec_validated: bool
    card_url: str = ""
    pinned_card_hash: str = ""


@dataclass(frozen=True)
class AllowlistEntry:
    """A hand-managed known-seed entry (TG-869 manual allowlist). Holds the
    expected card hash (an INDEPENDENT pin from the TXT's — both must agree) and
    optionally a pinned mTLS client-cert fingerprint the adapter checks at
    presentation time."""

    card_hash: str
    pinned_mtls_cert_fingerprint: str | None = None


@runtime_checkable
class TxtResolver(Protocol):
    """Resolves endpoint_id (e.g. "did:web:snowis.me") -> TxtResolution. The
    concrete DNSSEC/SRV-walking resolver is network-touching and lives outside
    Layer A; the core only ever calls this interface."""

    def __call__(self, endpoint_id: str) -> TxtResolution: ...


@runtime_checkable
class CardFetcher(Protocol):
    """Fetches the Agent Card bytes at the advertised URL, or returns None on
    any fetch failure (the core treats None as ABORT). Network-touching
    concretion lives outside Layer A."""

    def __call__(self, card_url: str) -> bytes | None: ...


@runtime_checkable
class DowngradeStore(Protocol):
    """Per-endpoint memory of schemes advertised on prior handshakes, for the
    STATEFUL downgrade-rejection rule (ADR line 38)."""

    def seen(self, endpoint_id: str) -> set[Scheme]:
        """Schemes advertised on any prior handshake with this endpoint."""
        ...

    def record(self, endpoint_id: str, schemes: set[Scheme]) -> None:
        """Fold this handshake's advertised set into the endpoint's memory."""
        ...

    def reset(self, endpoint_id: str) -> None:
        """Clear an endpoint's downgrade memory — the deliberate operator escape
        hatch for a legitimately retired scheme. Contracted in the Protocol
        (T-2867 HANDSHAKE-2) so a persistent-store swap cannot silently drop it
        and leave a high-water mark permanently un-clearable, which (with
        HANDSHAKE-1) is the only way out of a poisoned/legit-downgrade state."""
        ...


class InMemoryDowngradeStore:
    """Default DowngradeStore: a high-water mark (union of every advertised set
    ever seen per endpoint). High-water rather than last-seen is the fail-closed
    choice — once a stronger scheme has been advertised, its later ABSENCE is
    always treated as a suspicious downgrade. A LEGITIMATE scheme retirement is
    therefore a deliberate operator action (clear/reset the endpoint's memory),
    never a silent step-down.

    Not persistent: a fresh process starts with empty memory (first handshake
    per endpoint cannot be a downgrade). A durable store is a later concern;
    the interface is injected so that swap is a one-liner.
    """

    def __init__(self) -> None:
        self._seen: dict[str, set[Scheme]] = {}

    def seen(self, endpoint_id: str) -> set[Scheme]:
        return set(self._seen.get(endpoint_id, set()))

    def record(self, endpoint_id: str, schemes: set[Scheme]) -> None:
        self._seen.setdefault(endpoint_id, set()).update(schemes)

    def reset(self, endpoint_id: str) -> None:
        """Deliberate operator action to clear an endpoint's downgrade memory
        (e.g. a legitimately retired scheme). Explicit, never automatic."""
        self._seen.pop(endpoint_id, None)


@runtime_checkable
class SchemeAdapter(Protocol):
    """Presents/verifies an identity in one scheme (ADR D3, step 3). Layer A
    defines the INTERFACE; the concrete crypto (mTLS cert<->DID-key X.509
    binding; Bearer JWS iss/aud/replay/clock-skew) is the follow-up sub-ticket.

    verify() returns True only on a fully-validated presentation. A Layer-A
    production deployment registers NO real adapter, so the supported set is
    empty and step 2 refuses before step 3 is ever reached — the core cannot
    false-admit. Tests inject a stub adapter to exercise the admit path.
    """

    @property
    def scheme(self) -> Scheme: ...

    def verify(
        self, endpoint_id: str, allowlist_entry: AllowlistEntry
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Result type.
# ---------------------------------------------------------------------------


class HandshakeState(Enum):
    """The step a handshake reached (ADR D1)."""

    RESOLVE_ENDPOINT = "resolve_endpoint"       # step 1 (card-auth)
    SELECT_SCHEME = "select_scheme"             # step 2
    PRESENT_IDENTITY = "present_identity"       # step 3 (presenter-auth)
    ADMIT_UNTRUSTED = "admit_untrusted"         # step 4 (terminal, D4)


class AbortReason(Enum):
    """The machine-readable failing precondition logged on every ABORT
    (ADR line 42: "each ABORT logs the failing precondition")."""

    NO_TXT = "no_dnssec_txt_record"
    DNSSEC_INVALID = "dnssec_chain_absent_or_invalid"
    CARD_FETCH_FAILED = "agent_card_fetch_failed"
    CARD_HASH_MISMATCH = "card_hash_ne_pinned_hash"
    ENDPOINT_NOT_ALLOWLISTED = "endpoint_not_in_local_allowlist"
    CARD_UNPARSEABLE = "agent_card_not_valid_json"
    NO_COMMON_SCHEME = "no_common_security_scheme"
    DOWNGRADE_DETECTED = "stronger_prior_scheme_now_absent"
    PRESENTATION_FAILED = "scheme_adapter_verify_failed"


class Outcome(Enum):
    ADMITTED = "admitted"
    ABORTED = "aborted"


@dataclass(frozen=True)
class HandshakeResult:
    """The terminal record of one handshake.

    On ADMITTED: `scheme` and `principal` (the endpoint_id) are set. On ABORTED:
    `state` is the step that failed and `abort_reason` the precondition.

    `content_trusted` is ALWAYS False — the D4 invariant (lines 61-62): a
    verified identity gates who may speak, never whether what they say is
    trusted. It is intentionally not settable to True anywhere in this module.

    Frozen (T-2867 HANDSHAKE-3): a caller cannot reassign `content_trusted = True`
    (or any field) post-construction, closing the D4 mutation-by-caller gap and
    matching the frozen TxtResolution / AllowlistEntry. Both construction sites
    (`_abort`, the admit path) build the whole record at once, so freezing needs
    no construction refactor. NB the `log` list itself stays mutable (frozen
    blocks attribute REASSIGNMENT, not deep-freeze); no invariant depends on log
    immutability. `log` is `compare=False` so it is excluded from the generated
    __eq__/__hash__ — the result's identity is its verdict (outcome/scheme/
    principal/reason), not the diagnostic narration, and this keeps the frozen
    record cleanly hashable rather than hashable-in-name-but-raising on the list.
    """

    outcome: Outcome
    state: HandshakeState
    scheme: Scheme | None = None
    principal: str | None = None
    abort_reason: AbortReason | None = None
    content_trusted: bool = False  # D4 invariant — never True.
    log: list[str] = field(default_factory=list, compare=False)

    @property
    def admitted(self) -> bool:
        return self.outcome is Outcome.ADMITTED


# ---------------------------------------------------------------------------
# The handshake state machine.
# ---------------------------------------------------------------------------


class PassportHandshake:
    """Runs the ORD-047 D1 ordered, fail-closed handshake for one initiating
    seed against a counterparty endpoint.

    All external effects are injected:

      txt_resolver    — endpoint_id -> TxtResolution (DNSSEC-validated TXT)
      card_fetcher    — card_url -> Agent Card bytes | None
      allowlist       — endpoint_id -> AllowlistEntry (the local manual allowlist)
      adapters        — {Scheme: SchemeAdapter} we support ("our supported adapters")
      downgrade_store — per-endpoint prior-advertised memory (defaults in-memory)

    `run(endpoint_id)` returns a HandshakeResult; it never raises for a policy
    failure (only for a programming error in an injected dependency the caller
    controls). Each ABORT is logged both to the module logger and into the
    result's `log`.
    """

    def __init__(
        self,
        *,
        txt_resolver: TxtResolver,
        card_fetcher: CardFetcher,
        allowlist: dict[str, AllowlistEntry],
        adapters: dict[Scheme, SchemeAdapter],
        downgrade_store: DowngradeStore | None = None,
    ) -> None:
        self._resolve = txt_resolver
        self._fetch = card_fetcher
        self._allowlist = allowlist
        # T-2867 HANDSHAKE-4: validate the adapter map at WIRING time. Step 3 does
        # `self._adapters[selected].verify(...)`, trusting that the adapter stored
        # under scheme key K actually presents scheme K. A mis-wired map (e.g.
        # {MTLS: BearerAdapter()}) would run the WRONG scheme's verifier against a
        # selection made for K. That is an operator PROGRAMMING error, so it fails
        # fast HERE (raising at construction, the one place run()'s "never raises
        # for a policy failure" contract allows) rather than as a bare `assert`
        # (stripped under -O) or a silent mis-verification at handshake time.
        for _key, _adapter in adapters.items():
            if _adapter.scheme != _key:
                raise ValueError(
                    f"adapter map mis-wired: key {_key.name} holds an adapter "
                    f"presenting {_adapter.scheme.name}; the key must equal "
                    "adapter.scheme (a selection for one scheme would otherwise "
                    "run another scheme's verifier)"
                )
        self._adapters = adapters
        self._downgrade = downgrade_store or InMemoryDowngradeStore()

    # -- helpers -----------------------------------------------------------

    def _abort(
        self,
        result_log: list[str],
        state: HandshakeState,
        reason: AbortReason,
        detail: str,
    ) -> HandshakeResult:
        msg = f"ABORT at {state.value}: {reason.value} — {detail}"
        result_log.append(msg)
        logger.warning("passport-handshake %s", msg)
        return HandshakeResult(
            outcome=Outcome.ABORTED,
            state=state,
            abort_reason=reason,
            log=result_log,
        )

    # -- the sequence ------------------------------------------------------

    def run(self, endpoint_id: str) -> HandshakeResult:
        log: list[str] = [f"handshake start: endpoint={endpoint_id!r}"]

        # === Step 1 — Resolve endpoint (card-auth). ADR lines 37. ==========
        st = HandshakeState.RESOLVE_ENDPOINT
        txt = self._resolve(endpoint_id)
        if not isinstance(txt, TxtResolution) or not txt.present:
            return self._abort(log, st, AbortReason.NO_TXT,
                               "no DNSSEC-signed TXT record for endpoint")
        # Fail-closed on the DNSSEC chain — NEVER fall back to unsigned DNS.
        if not txt.dnssec_validated:
            return self._abort(log, st, AbortReason.DNSSEC_INVALID,
                               "TXT present but DNSSEC chain absent/invalid")

        # Endpoint must be in the local manual allowlist (TG-869).
        entry = self._allowlist.get(endpoint_id)
        if entry is None:
            return self._abort(log, st, AbortReason.ENDPOINT_NOT_ALLOWLISTED,
                               "endpoint not present in the local allowlist")

        # Fetch the Agent Card and verify its hash against BOTH the TXT pin and
        # the allowlist pin (two independent pins; any disagreement -> ABORT).
        card_bytes = self._fetch(txt.card_url)
        if not card_bytes:
            return self._abort(log, st, AbortReason.CARD_FETCH_FAILED,
                               f"could not fetch Agent Card at {txt.card_url!r}")
        computed = compute_card_hash(card_bytes)
        if computed != txt.pinned_card_hash:
            return self._abort(log, st, AbortReason.CARD_HASH_MISMATCH,
                               "card hash != TXT-pinned hash")
        if computed != entry.card_hash:
            return self._abort(log, st, AbortReason.CARD_HASH_MISMATCH,
                               "card hash != allowlist-pinned hash")

        # Parse the (now hash-verified) card to read its advertised schemes.
        try:
            card = json.loads(card_bytes)
        except (json.JSONDecodeError, ValueError):
            return self._abort(log, st, AbortReason.CARD_UNPARSEABLE,
                               "hash-verified card is not valid JSON")
        log.append("step 1 ok: DNSSEC + allowlist + dual-pinned card hash verified")

        # === Step 2 — Select scheme (+ stateful downgrade rejection). ======
        st = HandshakeState.SELECT_SCHEME
        advertised = parse_advertised_schemes(card)
        supported = set(self._adapters.keys())
        selected = select_strongest_common(advertised, supported)
        if selected is None:
            # Record the advertised set BEFORE aborting is unnecessary here —
            # a no-common-scheme handshake did still observe the advertised set,
            # but recording it only matters for the downgrade check, which we
            # never reach without a selection. Keep the memory update on the
            # success path (below) so an ABORT cannot poison it.
            return self._abort(log, st, AbortReason.NO_COMMON_SCHEME,
                               f"no common scheme (advertised={_names(advertised)}, "
                               f"supported={_names(supported)})")

        # Downgrade rejection: a scheme advertised on a PRIOR handshake that is
        # STRONGER than the one we are about to select, now absent, is suspicious.
        prior = self._downgrade.seen(endpoint_id)
        stronger_now_absent = {
            s for s in prior if s > selected and s not in advertised
        }
        if stronger_now_absent:
            return self._abort(
                log, st, AbortReason.DOWNGRADE_DETECTED,
                f"stronger prior scheme(s) {_names(stronger_now_absent)} now "
                f"absent; selected={selected.name} — refusing to step down")
        log.append(f"step 2 ok: selected {selected.name} "
                   f"(advertised={_names(advertised)})")

        # === Step 3 — Present identity in the selected scheme. ADR line 39. =
        st = HandshakeState.PRESENT_IDENTITY
        adapter = self._adapters[selected]  # guaranteed present: selected ∈ keys
        try:
            verified = bool(adapter.verify(endpoint_id, entry))
        except NotImplementedError:
            # A declared-but-unimplemented adapter (e.g. Layer A's structural
            # mTLS skeleton) cannot admit — fail-closed, never a false pass.
            verified = False
        if not verified:
            return self._abort(log, st, AbortReason.PRESENTATION_FAILED,
                               f"{selected.name} adapter verify() did not pass")
        log.append(f"step 3 ok: {selected.name} presentation verified")

        # T-2867 HANDSHAKE-1: fold the advertised set into the endpoint's downgrade
        # memory ONLY after a fully-verified presentation. Recording it earlier (right
        # after the step-2 downgrade gate) let a step-3 PRESENTATION_FAILED abort poison
        # the high-water mark, so a failed handshake could later false-refuse a
        # legitimate one as a downgrade. An ABORT at ANY step now records nothing; the
        # union stays monotonic (stricter-only, never a false-admit). The downgrade
        # CHECK above already read `prior` before this record, so its logic is unchanged.
        self._downgrade.record(endpoint_id, advertised)

        # === Step 4 — Admit as UNTRUSTED content (D4, lines 61-62). ========
        st = HandshakeState.ADMIT_UNTRUSTED
        log.append(f"step 4: admitting {endpoint_id!r} as principal — "
                   "content remains UNTRUSTED (D4)")
        return HandshakeResult(
            outcome=Outcome.ADMITTED,
            state=st,
            scheme=selected,
            principal=endpoint_id,
            content_trusted=False,  # invariant.
            log=log,
        )


def _names(schemes: set[Scheme]) -> str:
    """Stable comma-joined scheme names for log lines (sorted by strength)."""
    return ",".join(s.name for s in sorted(schemes)) or "∅"


# ---------------------------------------------------------------------------
# mTLS adapter — the first declared scheme adapter (STRUCTURAL skeleton).
# ---------------------------------------------------------------------------


class MtlsAdapter:
    """The mTLS scheme adapter's Layer-A fail-closed SKELETON (ADR D3 line 52).

    ⚠️ SUPERSEDED for real use by `LiveMtlsAdapter` in `passport_mtls.py` (T-2832),
    which performs the concrete cert↔DID-key binding. This skeleton is retained as
    the fail-closed DEFAULT: registering it lets step 2 SELECT mTLS but step 3 can
    only ABORT (verify() raises NotImplementedError → refused presentation), so a
    deployment that has not yet wired the live TLS-session plumbing cannot
    false-admit. Use `LiveMtlsAdapter` (constructed with the presented cert DER +
    the parsed DID document) wherever a real presenter must actually be admitted;
    keep this only where the fail-closed placeholder is intended.
    """

    @property
    def scheme(self) -> Scheme:
        return Scheme.MTLS

    def verify(self, endpoint_id: str, allowlist_entry: AllowlistEntry) -> bool:
        raise NotImplementedError(
            "MtlsAdapter is the Layer-A fail-closed skeleton — use LiveMtlsAdapter "
            "(passport_mtls.py) for real cert↔DID-key binding. This one refuses."
        )
