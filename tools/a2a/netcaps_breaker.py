"""ORG-071 D1 per-anchor circuit breaker with audit + recovery probe (T-2857 PR5).

One breaker per ANCHOR HOST (the host actually contacted), never per
counterparty DID: a hostile party can only trip the breaker of a host it can
make fail, and it cannot reach a breaker keyed on someone else's identity.

States, all timed on the injected Clock:

  * CLOSED    — requests pass; consecutive TRANSIENT_INFRA failures are counted
                (netcaps_failure_class). At `circuit_breaker_threshold` it opens.
  * OPEN      — every request is refused fail-closed WITHOUT touching the
                network, with `NetcapsBreach` (reason CIRCUIT_OPEN_REASON), for
                `circuit_breaker_cooldown`.
  * HALF_OPEN — after the cooldown, ONE request is let through as the recovery
                probe, and at most one per `circuit_breaker_probe_interval`.
                The anchor answering closes the breaker; a transient failure
                reopens it until the next probe slot.

The anchor answering at all — even a 404 / invalid document (DEFINITIVE_ABSENT)
— is anchor health, so it resets the count. An UNKNOWN exception is neutral: it
neither counts nor resets.

Memory is bounded: a healthy anchor holds no entry at all, and the table holds
at most `max_anchors`. A new anchor is never refused for lack of room. When the
table is full, the victim is the least-recently-touched CLOSED entry, else the
oldest OPEN / HALF_OPEN entry whose probe slot has passed, else the oldest
entry of any state. Evicting an open breaker fails toward "no breaker for X",
which is safe: X stays bounded by the D3 timeouts and PR2's per-anchor
resolution rate, whereas refusing healthy anchors would let an attacker with
wildcard DNS fill the table and deny the whole federation (and a transient
failure is never handshake-denial state, ORG-071 D2).

Two channels per host: the TXT lookup (DNS) and the card fetch (HTTPS) keep
separate entries, `dns_key(host)` and `host`, because they are different
infrastructure. Shared, a working DNS answer would reset the count of a dead
web server on every handshake, and that breaker could never trip.

Cross-kin tripping: a card fetch counts a failure only against the anchor of
the DID being resolved. If a kin's TXT points its `card_url` at some other
host, a failure there is not recorded against that host (an open breaker on it
still gates the fetch). The resolver wrapper records the endpoint's anchor in a
per-context (ContextVar) value that the card fetcher reads; the handshake calls
both in one thread / task, resolver first.

Known limitation: a kin whose Agent Card is hosted off its DID host never
counts HTTPS failures toward any breaker (the cross-kin guard above), so only
an already-open breaker on that host can gate it. Accepted for now.

Wiring: `BreakerTxtResolver` / `BreakerCardFetcher` wrap the
passport_handshake protocols, so the handshake is unchanged. A refusal raises
`netcaps_breach.NetcapsBreach(reason="anchor_circuit_breaker_open",
event="circuit_breaker_open")`; PR2's handshake hook maps it into `_breach()`,
which ABORTs with AbortReason.CIRCUIT_OPEN and the peer sees only PEER_ABORT
(D6). That hook emits the refusal's audit event, so each abort has one
record. The breaker emits only its own state changes: one `circuit_breaker_open`
event on each open and reopen. NetcapsBreach is not an OSError/TimeoutError, so
the D3 deadline never mistakes it for a timeout.

Every other outcome is transparent: the wrapper returns or re-raises exactly
what the inner callable did. A slow answer that the D3 deadline later discards
still counts as the anchor answering; the network concretion should take its
socket timeouts from the deadline budget and raise, so a timeout is seen here.
"""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import re
import threading
import unicodedata
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeVar

from tools.a2a.did_web import DidResolutionError, resolve_did_web_url
from tools.a2a.netcaps import (
    AuditEvent,
    AuditEventType,
    AuditSink,
    Clock,
    LoggingAuditSink,
    MonotonicClock,
    NetcapsParams,
)
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_failure_class import FailureClass, classify_fetch_failure

logger = logging.getLogger("pantheon.a2a.netcaps.breaker")

T = TypeVar("T")

# Cap on tracked (non-healthy) anchors. Not an ORG-071 taxonomy parameter: a
# memory guard sized well above a small hand-allowlisted federation.
MAX_TRACKED_ANCHORS = 1024


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# The NetcapsBreach strings: passport_handshake.AbortReason.CIRCUIT_OPEN's value
# and the D6 event name. Plain strings so this module does not import the
# handshake (which imports netcaps); a test pins them to the enums.
CIRCUIT_OPEN_REASON = "anchor_circuit_breaker_open"
CIRCUIT_OPEN_EVENT = AuditEventType.CIRCUIT_BREAKER_OPEN.value


_DEFAULT_PORTS = {"https": 443, "http": 80}


# inet_aton reads a final label of decimal digits or 0x-hex as (part of) an IPv4.
_NUMERIC_LABEL = re.compile(r"^(\d+|0[xX][0-9a-fA-F]*)$")


def _canonical_dns_name(host: str, url: str) -> str:
    """The ASCII (A-label) form of a DNS name, or ValueError (fail closed)."""
    if any(c <= " " or unicodedata.category(c) in ("Cc", "Cf") for c in host):
        raise ValueError(f"control character in anchor host of {url!r}")
    try:
        if host.isascii():
            ascii_host = host.encode("idna").decode("ascii")
        else:
            wanted = unicodedata.normalize("NFC", host).lower()
            ascii_host = host.encode("idna").decode("ascii")
            if ascii_host.encode("ascii").decode("idna") != wanted:
                raise ValueError("codec maps beyond case-folding")
        # Every A-label must be valid punycode and canonical.
        if ascii_host.encode("ascii").decode("idna").encode("idna") \
                != ascii_host.encode("ascii"):
            raise ValueError("non-canonical A-label")
    except UnicodeError as exc:                  # incl. bad punycode / empty label
        raise ValueError(f"invalid IDNA anchor host in {url!r}") from exc
    except ValueError as exc:
        raise ValueError(f"unsafe IDNA anchor host in {url!r}: {exc}") from exc
    return ascii_host.lower()


def anchor_of_url(url: str) -> str:
    """The canonical anchor key of a URL: the lower-cased host without a
    trailing dot (an IP in compressed form, IPv6 bracketed), plus `:port` only
    for a non-default port. Userinfo is ignored. ValueError (refused) if there
    is no host, the host is percent-encoded, or it is a spelling two resolvers
    could read as different hosts:

      * IDNA: a Unicode host keys as its xn-- A-label, so both spellings share
        one key. Stdlib codec (IDNA 2003), and a non-ASCII host is accepted
        only if the A-label decodes back to exactly its NFC lower-cased form;
        anything the codec maps beyond case-folding (ß -> ss, fullwidth forms,
        ZWJ, final sigma) is refused, because an IDNA 2008 resolver would go to
        a different host and one key would merge two hosts. A bad or
        non-canonical xn-- label is refused too.
      * Non-dotted / shorthand / octal / hex IPv4 (2130706433, 0x7f.0.0.1,
        127.1, 0177.0.0.1): inet_aton-style resolvers read them as IPs but
        ipaddress does not, so they would key as distinct hostnames. Refused;
        did:web has no use for them.
      * An IPv4-mapped IPv6 literal keys as its IPv4 form."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"no anchor host in {url!r}") from exc
    if host and host.endswith("."):
        host = host[:-1]
    if not host or host.endswith("."):
        raise ValueError(f"no anchor host in {url!r}")
    if "%" in host:
        raise ValueError(f"percent-encoded anchor host refused in {url!r}")
    if ":" not in host:                      # not an IPv6 literal
        host = _canonical_dns_name(host, url)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None and _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]):
        raise ValueError(f"non-canonical numeric anchor host refused in {url!r}")
    if ip is not None:
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    if port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        port = None
    return host if port is None else f"{host}:{port}"


def dns_key(anchor: str) -> str:
    """The breaker key of an anchor's DNS/TXT channel. '/' never occurs in a
    URL host, so it cannot collide with an HTTPS anchor key."""
    return f"dns/{anchor}"


def anchor_of_did(did: str) -> str:
    """The anchor key of a did:web DID (the host its document lives on)."""
    try:
        return anchor_of_url(resolve_did_web_url(did))
    except DidResolutionError as exc:
        raise ValueError(str(exc)) from exc


@dataclass
class _Entry:
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    next_probe_at: float = 0.0


class AnchorCircuitBreaker:
    """The per-anchor breaker table. Thread-safe; spawns nothing."""

    def __init__(self, params: NetcapsParams | None = None, *,
                 clock: Clock | None = None,
                 audit_sink: AuditSink | None = None,
                 max_anchors: int = MAX_TRACKED_ANCHORS) -> None:
        if isinstance(max_anchors, bool) or not isinstance(max_anchors, int) \
                or max_anchors < 1:
            raise ValueError(f"max_anchors must be an int >= 1, got {max_anchors!r}")
        self.params = params or NetcapsParams()
        self.clock = clock or MonotonicClock()
        self._audit = audit_sink or LoggingAuditSink()
        self._max = max_anchors
        self._table: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        # A ContextVar (not threading.local) so the cross-kin guard also holds
        # when the handshake runs as asyncio tasks. One var per breaker.
        self._anchor_var: contextvars.ContextVar[str | None] = \
            contextvars.ContextVar("netcaps_breaker_endpoint_anchor", default=None)

    # -- per-context handshake context (cross-kin guard) --------------------

    def set_endpoint_anchor(self, anchor: str | None) -> None:
        """Record the anchor of the DID the current thread / task / context is
        resolving. A new thread or an unpropagated executor job sees None (the
        card fetch then does not count failures: fail toward not tripping)."""
        self._anchor_var.set(anchor)

    def endpoint_anchor(self) -> str | None:
        return self._anchor_var.get()

    # -- introspection -----------------------------------------------------

    def state(self, anchor: str) -> BreakerState:
        with self._lock:
            entry = self._table.get(anchor)
            return entry.state if entry else BreakerState.CLOSED

    def tracked(self) -> int:
        with self._lock:
            return len(self._table)

    # -- the gate ----------------------------------------------------------

    def acquire(self, anchor: str, endpoint_id: str) -> None:
        """Admit one request to `anchor` or raise NetcapsBreach. `endpoint_id`
        is not recorded here: the handshake hook that maps the breach emits the
        refusal's audit event (one record per abort)."""
        with self._lock:
            now = self.clock.monotonic()
            entry = self._table.get(anchor)
            if entry is None:
                return  # untracked = healthy; never refused for lack of room
            self._table.move_to_end(anchor)
            if entry.state is BreakerState.CLOSED:
                return
            if now >= entry.next_probe_at:
                # The recovery probe: one request per probe slot.
                entry.state = BreakerState.HALF_OPEN
                entry.next_probe_at = now + self.params.circuit_breaker_probe_interval
                logger.info("netcaps breaker anchor=%r half-open: recovery probe",
                            anchor)
                return
            self._refuse(anchor, f"{entry.state.value}; next probe in "
                         f"{entry.next_probe_at - now:.1f}s")

    def record_success(self, anchor: str) -> None:
        """The anchor answered (any returned value): it is healthy again.

        So an anchor that answers intermittently (a 404 between timeouts) never
        trips. Accepted: such an anchor is usually the counterparty's own host,
        which only hurts itself, and D3 timeouts plus PR2's per-anchor
        resolution rate bound what its failures cost us."""
        with self._lock:
            entry = self._table.pop(anchor, None)
        if entry is not None and entry.state is not BreakerState.CLOSED:
            logger.info("netcaps breaker anchor=%r closed: anchor answered", anchor)

    def record_failure(self, anchor: str, endpoint_id: str) -> None:
        """One TRANSIENT_INFRA failure against `anchor`."""
        with self._lock:
            now = self.clock.monotonic()
            entry = self._table.get(anchor)
            if entry is None:
                if len(self._table) >= self._max:
                    self._evict_one(now)
                entry = self._table[anchor] = _Entry()
            self._table.move_to_end(anchor)
            if entry.state is BreakerState.OPEN:
                return  # a straggler admitted before the trip; already open
            if entry.state is BreakerState.HALF_OPEN:
                entry.state = BreakerState.OPEN
                entry.next_probe_at = now + self.params.circuit_breaker_probe_interval
                self._emit(anchor, endpoint_id, now,
                           "recovery probe failed (transient); reopened")
                return
            entry.failures += 1
            if entry.failures >= self.params.circuit_breaker_threshold:
                entry.state = BreakerState.OPEN
                entry.next_probe_at = now + self.params.circuit_breaker_cooldown
                self._emit(anchor, endpoint_id, now,
                           f"opened after {entry.failures} consecutive "
                           "transient failures")

    def call(self, anchor: str, endpoint_id: str,
             fn: Callable[..., T], *args, count_failures: bool = True) -> T:
        """acquire, run fn(*args), record the outcome by failure class, and
        return / re-raise exactly what fn did. With count_failures=False a
        transient failure is not recorded (the cross-kin guard)."""
        self.acquire(anchor, endpoint_id)
        try:
            value = fn(*args)
        except Exception as exc:
            if count_failures and (classify_fetch_failure(exc)
                                   is FailureClass.TRANSIENT_INFRA):
                self.record_failure(anchor, endpoint_id)
            raise
        self.record_success(anchor)
        return value

    # -- internals (caller holds the lock) ---------------------------------

    def _evict_one(self, now: float) -> None:
        # Oldest first: a closed entry, else an open one past its probe slot,
        # else the oldest of any state (see the module docstring for why
        # evicting an open breaker is the safe direction).
        items = self._table.items()
        victim = (next((k for k, e in items if e.state is BreakerState.CLOSED), None)
                  or next((k for k, e in items if now >= e.next_probe_at), None)
                  or next(iter(self._table)))
        entry = self._table.pop(victim)
        if entry.state is not BreakerState.CLOSED:
            logger.warning("netcaps breaker: table full, evicted %s breaker for "
                           "anchor=%r", entry.state.value, victim)

    def _emit(self, anchor: str, endpoint_id: str, now: float, detail: str) -> None:
        # Fail closed: breaker state is already updated, so a raising sink must
        # not replace the caller's outcome with a foreign, unaudited exception.
        try:
            self._audit.emit(AuditEvent(
                type=AuditEventType.CIRCUIT_BREAKER_OPEN, endpoint_id=endpoint_id,
                reason=CIRCUIT_OPEN_REASON,
                detail=f"anchor={anchor}: {detail}", at=now))
        except Exception:  # noqa: BLE001 — audit-sink faults are contained
            logger.error("netcaps breaker: audit sink raised emitting the open "
                         "event for anchor=%r (state updated, event lost)",
                         anchor, exc_info=True)

    @staticmethod
    def _refuse(anchor: str, detail: str):
        # No audit emit: the handshake hook mapping this breach into _breach()
        # records the refusal. The breaker emits only its own state changes.
        raise NetcapsBreach(CIRCUIT_OPEN_REASON, CIRCUIT_OPEN_EVENT,
                            f"anchor={anchor}: {detail}")

    def refuse_unkeyable(self, endpoint_id: str, detail: str):
        """Fail closed on a request whose anchor host cannot be derived: it
        cannot be keyed, so it may not bypass the breaker."""
        logger.warning("netcaps breaker: no anchor host in %r; refused",
                       endpoint_id)
        self._refuse("?", f"no anchor host ({detail})")


# ---------------------------------------------------------------------------
# Protocol wrappers (passport_handshake stays unchanged).
# ---------------------------------------------------------------------------


class BreakerTxtResolver:
    """A passport_handshake.TxtResolver behind the breaker, keyed by
    `dns_key(<the DID's anchor host>)`. Raises NetcapsBreach instead of
    resolving while open."""

    def __init__(self, inner: Callable[[str], T], breaker: AnchorCircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def __call__(self, endpoint_id: str):
        self._breaker.set_endpoint_anchor(None)
        try:
            anchor = anchor_of_did(endpoint_id)
        except ValueError as exc:
            self._breaker.refuse_unkeyable(endpoint_id, str(exc))
        self._breaker.set_endpoint_anchor(anchor)
        return self._breaker.call(dns_key(anchor), endpoint_id, self._inner,
                                  endpoint_id)


class BreakerCardFetcher:
    """A passport_handshake.CardFetcher behind the breaker, keyed by the card
    URL's host. Raises NetcapsBreach instead of fetching while open. Counts a
    failure only when that host is the anchor of the DID the paired
    BreakerTxtResolver (same breaker) just resolved on this thread."""

    def __init__(self, inner: Callable[[str], T], breaker: AnchorCircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def __call__(self, card_url: str):
        try:
            anchor = anchor_of_url(card_url)
        except ValueError as exc:
            self._breaker.refuse_unkeyable(card_url, str(exc))
        own = anchor == self._breaker.endpoint_anchor()
        return self._breaker.call(anchor, card_url, self._inner, card_url,
                                  count_failures=own)
