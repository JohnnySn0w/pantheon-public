"""ORG-071 D2 failure classes at the fetcher boundary (T-2857 PR5).

ORG-071 D2 splits a failed outbound resolution into two classes that must be
handled differently:

  * TRANSIENT_INFRA — the ANCHOR (or the path to it) is unwell: a timeout, a
    DNS / TCP-connect / TLS error, or the anchor rate-limiting us (HTTP 429, or
    a 5xx). Advisory backoff only, never handshake-denial state; the per-anchor
    circuit breaker (netcaps_breaker.py) governs repeated ones.
  * DEFINITIVE_ABSENT — the anchor ANSWERED and the counterparty's material is
    bad or missing: 404/410, an invalid/malformed document, no TXT record, a
    failed DNSSEC chain. That is the counterparty's problem, not anchor
    ill-health, so it never counts toward a breaker.

How the boundary signals the class (the contract for network concretions):

  * A RETURNED value — any value, including a `None` card or a
    `TxtResolution(present=False)` — means the anchor answered: DEFINITIVE.
  * A RAISED exception is classified by `classify_fetch_failure`. TRANSIENT:
    `OSError` (covers TimeoutError, socket.timeout, ConnectionError,
    socket.gaierror, ssl.SSLError), `TransientAnchorError` (raise it for
    429 / 5xx), and resolver-library timeouts that do not subclass
    TimeoutError (dnspython's `dns.exception.Timeout` and its subclasses,
    matched by name so dnspython is not a dependency).
  * A RAISED exception that matches none of those is UNKNOWN: evidence of
    neither anchor ill-health nor bad counterparty material. The breaker
    treats it as neutral (it neither counts nor resets), so an unknown bug
    cannot trip a breaker.

PR3 (the D2 resolution cache) needs the same split for its negative-TTL
classes; the two are reconciled when the PRs land.
"""

from __future__ import annotations

from enum import Enum


class FailureClass(Enum):
    TRANSIENT_INFRA = "transient_infra"
    DEFINITIVE_ABSENT = "definitive_absent"
    UNKNOWN = "unknown"


class TransientAnchorError(Exception):
    """Raised by a network concretion when the anchor answered but is unwell
    or throttling us (HTTP 429, 5xx). Builtin network errors (OSError and its
    subclasses) need no wrapping — they classify as transient already."""


# (module, class name) of third-party timeout types that are not OSErrors.
_FOREIGN_TIMEOUTS = frozenset({("dns.exception", "Timeout")})


def classify_fetch_failure(exc: BaseException) -> FailureClass:
    """The ORG-071 D2 class of an exception raised at the fetcher boundary."""
    if isinstance(exc, (OSError, TransientAnchorError)):
        return FailureClass.TRANSIENT_INFRA
    for cls in type(exc).__mro__:
        if (cls.__module__, cls.__qualname__) in _FOREIGN_TIMEOUTS:
            return FailureClass.TRANSIENT_INFRA
    return FailureClass.UNKNOWN
