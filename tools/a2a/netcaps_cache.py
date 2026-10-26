"""ORG-071 D2 — resolution + status caching: STRICT freshness, failure-classed
negative caching, singleflight (T-2857 PR3).

Ratified spec: architecture/adr/ORG-071-passport-network-capabilities.md D2
(+ D1 amplification bounds, D3 per-fetch timeouts); ORG-047 D5 (a rotation
propagates in minutes, so the positive TTL is short and authoritative).

Shape: a DECORATOR. `ResolutionCache.did_documents(raw_fetcher)` and
`.status_lists(raw_fetcher, validator)` wrap a network-touching `RawFetcher`
(the concretion lives outside Layer A, like TxtResolver/CardFetcher) and return
a resolver that yields VALIDATED material or raises `CacheBreach` (a
NetcapsBreach). passport_handshake.py is unchanged.

Failure classification happens at the fetcher boundary:

  * The fetcher raises `FetchFailure(FailureClass.DEFINITIVE_ABSENT, ...)` for
    404/410, and `FetchFailure(FailureClass.TRANSIENT_INFRA, ...)` for DNS/TLS
    failure, 429/5xx (the anchor rate-limited us), or a local resolver fault.
    Any other raised exception goes through PR5's `classify_fetch_failure`
    (netcaps_failure_class.py, shared byte-identical): OSError,
    TransientAnchorError and dnspython timeouts are TRANSIENT_INFRA.
  * A body over the size cap, or one the validator explicitly rejects
    (VALIDATION_REJECTIONS: a malformed doc, an id/issuer mismatch, a bad
    signature, a bounded_gunzip cap), is DEFINITIVE_ABSENT, because the anchor
    did answer and the material is bad. Other validator exceptions are routed
    like fetcher ones (see status_lists).
  * A timeout (TimeoutError incl. socket.timeout and PR1's StepTimeout,
    dnspython's dns.exception.Timeout, or a result that arrived after its
    budget) is TRANSIENT_INFRA, reported as resolver_timeout (D3).
  * FailureClass.UNKNOWN (an exception PR5's classifier cannot place) still
    aborts (STRICT), but the cache records NO negative state for it, so a bug
    can never become a DID-scoped denial window.

Breach strings raised (they equal PR1's AbortReason values / AuditEventType
values; a test pins them):

  | situation                                      | reason                            | event               |
  |------------------------------------------------|-----------------------------------|---------------------|
  | fresh definitive-absent entry hit              | negative_cache_hit                | negative_cache_hit  |
  | timeout / deadline spent / late result         | network_step_timeout or           | resolver_timeout    |
  |                                                | total_handshake_deadline_exceeded |                     |
  | other failure, an expired entry was held       | stale_material_denied             | stale_denied        |
  | other failure, status list, nothing held       | status_fetch_failed               | status_fetch_failed |
  | other failure, DID doc, nothing held           | did_resolution_failed             | did_resolution_failed |
  | unresolvable DID (not did:web)                 | did_resolution_failed             | did_resolution_failed |

The cache emits no audit event itself; PR2's hook maps NetcapsBreach to
PassportHandshake._breach(), which emits exactly one.

Deadline seam (D3): every resolve call takes an optional `deadline` (anything
with `remaining()`, e.g. PR1's HandshakeDeadline). No caller ever waits past
its own deadline: the leader hands the fetcher `timeout=` min(full D3 per-fetch
budget, its remaining), and a waiter waits min(full, its remaining). A timeout
on a budget the leader's deadline CUT is that caller's alone: no suppression,
not shared, and waiters with budget left retry as the new leader (at most
MAX_FLIGHT_ATTEMPTS, then ONE solo, unshared fetch on the caller's own budget:
at most one extra fetch per caller). "Cut" means a budget below
MATERIAL_CUT_RATIO * full. A timeout on a budget at or above it is a real anchor timeout:
suppressed and shared. A result that lands after a caller's deadline is
discarded for it alone (still cached for others).
PassportHandshake resolves the DID doc inside run() (`did_resolver=`, T-5399
PR-b) and passes the run's HandshakeDeadline here, so it is under the total
deadline.

Size limits: `max_did_doc_bytes` / `max_status_list_bytes` are passed to the
fetcher as `max_bytes=` (it must stop reading at max_bytes + 1, since only it
can avoid buffering a huge body) and re-checked here. The decompressed and
ratio caps apply where the bitstring is inflated, inside the status-list
validator (StatusList2021 parsing is Layer B, not built). `bounded_gunzip` is
the helper it must use.
"""

from __future__ import annotations

import json
import random
import threading
import urllib.parse
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from tools.a2a.did_web import (
    DidResolutionError,
    ParsedDidDocument,
    parse_did_document,
    resolve_did_web_url,
)
from tools.a2a.netcaps import Clock, MonotonicClock, NetcapsParams
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_failure_class import (  # shared with PR5, byte-identical
    FailureClass,
    TransientAnchorError,
    classify_fetch_failure,
)

DEFAULT_MAX_ENTRIES = 1024

# AbortReason values / AuditEventType values (strings: no passport_handshake import).
R_NEGATIVE, E_NEGATIVE = "negative_cache_hit", "negative_cache_hit"
R_STALE, E_STALE = "stale_material_denied", "stale_denied"
R_STATUS, E_STATUS = "status_fetch_failed", "status_fetch_failed"
R_DID_RESOLUTION, E_DID_RESOLUTION = "did_resolution_failed", "did_resolution_failed"
R_STEP_TIMEOUT = "network_step_timeout"
R_TOTAL_TIMEOUT = "total_handshake_deadline_exceeded"
E_TIMEOUT = "resolver_timeout"

# A validator REJECTS material by raising one of these (json errors and
# bounded_gunzip's caps are ValueErrors; did_web raises DidResolutionError).
VALIDATION_REJECTIONS = (ValueError, DidResolutionError)

# A follower whose leader failed only on the leader's own deadline retries;
# this bounds the retries so a run of short-deadline leaders cannot spin it.
MAX_FLIGHT_ATTEMPTS = 3

# A fetch budget below this fraction of the full D3 per-fetch budget is a
# "material cut": the timeout then measures the caller's deadline, not the
# anchor. At or above it the anchor had essentially the whole window, so a
# timeout is real evidence (suppressed + shared). Provisional tuning value
# (T-2857 first-fire to re-evaluate), not an ORG-071 taxonomy parameter.
MATERIAL_CUT_RATIO = 0.9


def is_material_cut(budget: float, full: float) -> bool:
    return budget < MATERIAL_CUT_RATIO * full

DID_DOC = "did_doc"
STATUS_LIST = "status_list"


class FetchFailure(Exception):
    """Raised by a RawFetcher to classify its failure (see module docstring)."""

    def __init__(self, failure_class: FailureClass, detail: str = "") -> None:
        super().__init__(f"{failure_class.value}: {detail}")
        self.failure_class = failure_class
        self.detail = detail


class CacheBreach(NetcapsBreach):
    """A NetcapsBreach that also carries the failure class (None for a hit),
    the anchor host a TRANSIENT failure was about (so a nested resolve inside
    a validator suppresses the right host), and `deadline_only` for a timeout
    caused purely by the fetching caller's own deadline (never shared)."""

    def __init__(self, reason: str, event: str, detail: str = "", *,
                 failure_class: FailureClass | None = None,
                 anchor: str | None = None, deadline_only: bool = False) -> None:
        super().__init__(reason, event, detail)
        self.failure_class = failure_class
        self.anchor = anchor
        self.deadline_only = deadline_only


@dataclass(frozen=True)
class FetchResponse:
    body: bytes
    max_age: float | None = None  # from Cache-Control; see max_age_from_cache_control
    declared_length: int | None = None  # raw (pre-decoding) Content-Length, if any


@runtime_checkable
class RawFetcher(Protocol):
    """Fetches one URL with no redirects (max_redirects = 0), reading at most
    max_bytes + 1 and bounding its sockets by `timeout` seconds.

    It MUST detect a short body (fewer bytes than Content-Length, or a chunked
    transfer without its terminating chunk) and raise OSError or
    TransientAnchorError, never return the truncated bytes: a cleanly
    truncated document would otherwise fail validation and be cached as
    DEFINITIVE_ABSENT. It should also report Content-Length as
    `declared_length`; the cache treats a mismatch as transient (a backstop).

    Content-coding contract: `declared_length` is the RAW on-the-wire entity
    length (Content-Length, before any content-decoding) and `body` is the raw
    undecoded bytes. The fetcher MUST send `Accept-Encoding: identity` and MUST
    NOT transparently content-decode (a gzip'd status list is inflated by
    `bounded_gunzip` inside the validator, under the decompression caps). A
    decoded body against a raw length reads as a short body (transient)."""

    def __call__(self, url: str, *, max_bytes: int, timeout: float) -> FetchResponse: ...


class Deadline(Protocol):
    def remaining(self) -> float: ...


def max_age_from_cache_control(value: str | None) -> float | None:
    """Cache-Control -> max-age seconds: 0 for no-store/no-cache, None when
    absent or malformed (the local max then applies)."""
    if not value:
        return None
    max_age = None
    for part in value.split(","):
        name, _, arg = part.strip().partition("=")
        name = name.strip().lower()
        if name in ("no-store", "no-cache"):
            return 0
        if name == "max-age":
            arg = arg.strip().strip('"')
            if arg.isascii() and arg.isdigit():
                v = int(arg)
                max_age = v if max_age is None else min(max_age, v)
    return max_age


def bounded_gunzip(data: bytes, params: NetcapsParams) -> bytes:
    """Inflate a gzip'd status-list bitstring under the Sizes caps; ValueError
    (definitive: the material is bad) on any breach or corrupt input."""
    if len(data) > params.max_status_list_bytes:
        raise ValueError("status list exceeds max_status_list_bytes")
    limit = min(params.max_status_list_decompressed_bytes,
                int(max(len(data), 1) * params.max_compression_ratio))
    d = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error as exc:
        raise ValueError(f"status list is not valid gzip: {exc}") from exc
    if len(out) > limit:
        cap = ("max_status_list_decompressed_bytes"
               if limit == params.max_status_list_decompressed_bytes
               else "max_compression_ratio")
        raise ValueError(f"decompressed status list exceeds {cap}")
    if not d.eof:
        raise ValueError("status list gzip stream is truncated")
    if d.unused_data:
        raise ValueError("status list has trailing data after the gzip member")
    return out


def _is_timeout(exc: BaseException) -> bool:
    """TimeoutError (socket.timeout, StepTimeout), or one of PR5's foreign
    resolver timeouts (dns.exception.Timeout): transient yet not an OSError."""
    if isinstance(exc, TimeoutError):
        return True
    return (classify_fetch_failure(exc) is FailureClass.TRANSIENT_INFRA
            and not isinstance(exc, (OSError, TransientAnchorError)))


@dataclass
class _Entry:
    kind: str            # "pos" | "neg" | "supp"
    value: Any
    expires_at: float
    detail: str = ""


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.waiters = 0
        self.value: Any = None
        self.error: CacheBreach | None = None


class ResolutionCache:
    """One cache shared by every resolver it decorates. Thread-safe."""

    def __init__(self, params: NetcapsParams | None = None, *,
                 clock: Clock | None = None, rng: random.Random | None = None,
                 max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.params = params or NetcapsParams()
        self.clock = clock or MonotonicClock()
        self._rng = rng or random.Random()
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, _Entry] = OrderedDict()
        self._flights: dict[tuple, _Flight] = {}

    # -- decorators ----------------------------------------------------------

    def did_documents(self, fetcher: RawFetcher) -> Callable[..., ParsedDidDocument]:
        p = self.params

        def resolve(did: str, *, source: str = "local",
                    deadline: Deadline | None = None) -> ParsedDidDocument:
            try:
                url = resolve_did_web_url(did)
            except DidResolutionError as exc:
                raise CacheBreach(R_DID_RESOLUTION, E_DID_RESOLUTION,
                                  f"unresolvable DID: {exc}",
                                  failure_class=FailureClass.DEFINITIVE_ABSENT) from exc

            def validate(body: bytes) -> ParsedDidDocument:
                return parse_did_document(json.loads(body), did)

            return self._get((DID_DOC, did), url, source, deadline, fetcher,
                             validate, p.max_did_doc_bytes,
                             p.did_doc_positive_ttl_max, "max_did_doc_bytes")
        return resolve

    def status_lists(self, fetcher: RawFetcher,
                     validator: Callable[[bytes, str, str], Any]) -> Callable[..., Any]:
        """`validator(body, issuer, url)` returns the validated status list.

        Contract (F2): to REJECT the material (bad signature, issuer mismatch,
        malformed, bounded_gunzip caps) raise ValueError or DidResolutionError;
        that is cached negatively for negative_ttl_definitive_absent. A
        timeout, OSError, TransientAnchorError or a transient CacheBreach (e.g.
        resolving the issuer's DID doc) only suppresses fetches briefly. Any
        other exception aborts and caches nothing."""
        p = self.params

        def resolve(issuer: str, url: str, *, source: str = "local",
                    deadline: Deadline | None = None) -> Any:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise CacheBreach(R_STATUS, E_STATUS, f"status URL not https: {url!r}",
                                  failure_class=FailureClass.DEFINITIVE_ABSENT)
            return self._get((STATUS_LIST, issuer, url), url, source, deadline,
                             fetcher, lambda body: validator(body, issuer, url),
                             p.max_status_list_bytes,
                             p.status_list_positive_ttl_max, "max_status_list_bytes")
        return resolve

    # -- introspection (tests / operators) ----------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[tuple]:
        with self._lock:
            return list(self._entries)

    def expires_at(self, key: tuple) -> float | None:
        with self._lock:
            e = self._entries.get(("id",) + key)
            return e.expires_at if e else None

    def waiters(self, key: tuple) -> int:
        with self._lock:
            f = self._flights.get(key)
            return f.waiters if f else 0

    # -- core ----------------------------------------------------------------

    def _get(self, key, url, source, deadline, fetcher, validate, max_bytes,
             ttl_max, size_param):
        anchor = urllib.parse.urlsplit(url).hostname or ""
        id_key = ("id",) + key                 # positive + definitive-negative
        supp_key = ("supp", anchor, source)    # transient: anchor + source only
        full = self._fetch_budget()
        # The last pass is a SOLO fetch (not a shared flight), reached only
        # after MAX_FLIGHT_ATTEMPTS deadline-only leader failures.
        for _attempt in range(MAX_FLIGHT_ATTEMPTS + 1):
            solo = _attempt == MAX_FLIGHT_ATTEMPTS
            if deadline is not None and deadline.remaining() <= 0:
                raise CacheBreach(R_TOTAL_TIMEOUT, E_TIMEOUT,
                                  "deadline spent before fetch",
                                  failure_class=FailureClass.TRANSIENT_INFRA)
            now = self.clock.monotonic()
            with self._lock:
                e = self._live(id_key, now)
                if e is not None and e.kind == "pos":
                    return e.value
                if e is not None:              # definitive-absent, any source
                    raise CacheBreach(R_NEGATIVE, E_NEGATIVE, e.detail)
                stale = self._entries.get(id_key)
                held = stale is not None and stale.kind == "pos"  # expired, never served
                if self._live(supp_key, now) is not None:
                    raise self._failure(key, held, FailureClass.TRANSIENT_INFRA,
                                        f"fetches to {anchor} suppressed for {source}",
                                        anchor=anchor)
                flight = None if solo else self._flights.get(key)
                leader = flight is None
                if leader and not solo:
                    flight = self._flights[key] = _Flight()
                elif not leader:
                    flight.waiters += 1
            if leader and solo:
                # One fetch on this caller's own budget; no flight, so no one
                # else's deadline can cut it and no one waits on it.
                budget = full if deadline is None else min(full, deadline.remaining())
                value = self._fetch(key, url, held, fetcher, validate,
                                    max_bytes, ttl_max, size_param,
                                    id_key, supp_key, budget, full, anchor)
                if deadline is not None and deadline.remaining() <= 0:
                    raise CacheBreach(R_TOTAL_TIMEOUT, E_TIMEOUT,
                                      "own deadline spent while the fetch ran; "
                                      "result discarded",
                                      failure_class=FailureClass.TRANSIENT_INFRA)
                return value
            if leader:
                # The leader's fetch never outlives its own deadline (D3).
                budget = full if deadline is None else min(full, deadline.remaining())
                try:
                    flight.value = self._fetch(key, url, held, fetcher, validate,
                                               max_bytes, ttl_max, size_param,
                                               id_key, supp_key, budget, full, anchor)
                except CacheBreach as exc:
                    flight.error = exc
                except BaseException as exc:
                    # An internal bug: followers must still fail closed, never get None.
                    flight.error = CacheBreach(R_STALE, E_STALE, f"internal error: {exc!r}")
                    raise
                finally:
                    with self._lock:
                        del self._flights[key]
                    flight.done.set()
            else:
                wait = full if deadline is None else min(full, deadline.remaining())
                if not flight.done.wait(max(wait, 0)):
                    # This caller's own timeout: nothing is shared or suppressed.
                    binding = deadline is not None and is_material_cut(wait, full)
                    raise CacheBreach(R_TOTAL_TIMEOUT if binding else R_STEP_TIMEOUT,
                                      E_TIMEOUT, f"waited {wait:.3f}s on in-flight fetch",
                                      failure_class=FailureClass.TRANSIENT_INFRA)
                if flight.error is not None and flight.error.deadline_only:
                    continue   # the leader ran out of ITS deadline: retry on ours
            if deadline is not None and deadline.remaining() <= 0:
                # Late for THIS caller only; a successful flight stays cached for others.
                raise CacheBreach(R_TOTAL_TIMEOUT, E_TIMEOUT,
                                  "own deadline spent while the fetch ran; result discarded",
                                  failure_class=FailureClass.TRANSIENT_INFRA)
            if flight.error is not None:
                raise flight.error
            return flight.value
        raise AssertionError("unreachable: the solo pass returns or raises")

    def _fetch_budget(self) -> float:
        p = self.params
        return p.timeout_connect + p.timeout_tls + p.timeout_fetch_read

    def _fetch(self, key, url, held, fetcher, validate, max_bytes, ttl_max,
               size_param, id_key, supp_key, budget, full, anchor):
        """One flight on `budget` = min(full D3 per-fetch budget, the leader's
        remaining deadline). A timeout on a CUT budget is the leader's own
        deadline, not evidence about the anchor: it neither suppresses nor is
        shared (deadline_only, followers retry). A timeout on the full budget
        is a real anchor timeout: suppressed and shared."""
        cut = is_material_cut(budget, full)
        t0 = self.clock.monotonic()
        try:
            resp = fetcher(url, max_bytes=max_bytes, timeout=budget)
        except FetchFailure as exc:
            raise self._fail(key, held, exc.failure_class, exc.detail,
                             id_key, supp_key, anchor) from exc
        except Exception as exc:  # noqa: BLE001 — classified, never swallowed
            if _is_timeout(exc):
                raise self._timeout(cut, supp_key, anchor,
                                    f"fetch timeout: {exc!r}") from exc
            # TRANSIENT_INFRA suppresses; UNKNOWN aborts with no negative state
            # (a fetcher bug must never become a DID-scoped denial window).
            raise self._fail(key, held, classify_fetch_failure(exc),
                             f"fetcher error: {exc!r}", id_key, supp_key, anchor) from exc
        if self.clock.monotonic() - t0 > budget:
            raise self._timeout(cut, supp_key, anchor,
                                f"result arrived after {budget:.3f}s budget, discarded")
        body = resp.body
        if len(body) > max_bytes:
            raise self._fail(key, held, FailureClass.DEFINITIVE_ABSENT,
                             f"body {len(body)}B exceeds {size_param}",
                             id_key, supp_key, anchor)
        if resp.declared_length is not None and resp.declared_length != len(body):
            # A truncated transfer is the path's fault, not bad material.
            raise self._fail(key, held, FailureClass.TRANSIENT_INFRA,
                             f"short body: {len(body)}B of declared "
                             f"{resp.declared_length}B", id_key, supp_key, anchor)
        try:
            value = validate(body)
        except Exception as exc:  # noqa: BLE001 — routed by class, never swallowed
            raise self._validator_failure(exc, key, held, id_key, supp_key) from exc
        ttl = ttl_max if resp.max_age is None else min(resp.max_age, ttl_max)
        ttl *= 1 - self._rng.uniform(0, self.params.jitter_range_pct / 100)
        with self._lock:
            # A validated success clears this source's suppression and replaces
            # any negative entry for the identity (never the other way round).
            self._entries.pop(supp_key, None)
            self._entries.pop(id_key, None)
            if ttl > 0:
                # TTL runs from the fetch START (L1): validation time counts.
                self._put(id_key, _Entry("pos", value, t0 + ttl))
        return value

    def _timeout(self, cut, supp_key, anchor, detail) -> CacheBreach:
        if cut:
            return CacheBreach(R_TOTAL_TIMEOUT, E_TIMEOUT,
                               f"{detail} (leader's own deadline)",
                               failure_class=FailureClass.TRANSIENT_INFRA,
                               deadline_only=True)
        self._suppress(supp_key)
        return CacheBreach(R_STEP_TIMEOUT, E_TIMEOUT, detail,
                           failure_class=FailureClass.TRANSIENT_INFRA, anchor=anchor)

    def _validator_failure(self, exc, key, held, id_key, supp_key) -> CacheBreach:
        """Route a validator exception (F2); returns the breach to raise.

        Only an explicit rejection (VALIDATION_REJECTIONS, or a CacheBreach
        marked DEFINITIVE_ABSENT) is definitive. A transient CacheBreach from a
        nested resolve suppresses the host that actually failed (its `anchor`),
        never this URL's host. A bare timeout / OSError names no host, so it
        aborts transient with nothing suppressed. Anything else is UNKNOWN:
        abort, no negative state."""
        detail = f"validation failed: {exc!r}"
        if isinstance(exc, CacheBreach) and exc.deadline_only:
            # A nested resolve hit its own caller's deadline: not evidence about
            # any host. Keep the flag and the timeout reason; suppress nothing.
            return CacheBreach(exc.reason, E_TIMEOUT, detail,
                               failure_class=FailureClass.TRANSIENT_INFRA,
                               deadline_only=True)
        if _is_timeout(exc):
            return CacheBreach(R_STEP_TIMEOUT, E_TIMEOUT, f"validator timeout: {exc!r}",
                               failure_class=FailureClass.TRANSIENT_INFRA)
        if isinstance(exc, CacheBreach):
            cls = exc.failure_class or FailureClass.UNKNOWN
            if cls is FailureClass.TRANSIENT_INFRA:
                if exc.anchor:
                    self._suppress(("supp", exc.anchor, supp_key[2]))
                return self._failure(key, held, cls, detail, anchor=exc.anchor)
        elif isinstance(exc, VALIDATION_REJECTIONS):
            cls = FailureClass.DEFINITIVE_ABSENT
        else:
            cls = classify_fetch_failure(exc)   # TRANSIENT_INFRA or UNKNOWN
            if cls is FailureClass.TRANSIENT_INFRA:
                return self._failure(key, held, cls, detail)
        return self._fail(key, held, cls, detail, id_key, supp_key, None)

    def _fail(self, key, held, cls, detail, id_key, supp_key, anchor) -> CacheBreach:
        """Record the negative state `cls` calls for; return the breach."""
        with self._lock:
            if cls is FailureClass.DEFINITIVE_ABSENT:
                self._put(id_key, _Entry(
                    "neg", None,
                    self.clock.monotonic() + self.params.negative_ttl_definitive_absent,
                    f"{cls.value}: {detail}"))
        if cls is FailureClass.TRANSIENT_INFRA:
            self._suppress(supp_key)
        return self._failure(key, held, cls, detail, anchor=anchor)

    def _suppress(self, supp_key) -> None:
        with self._lock:
            self._put(supp_key, _Entry(
                "supp", None,
                self.clock.monotonic() + self.params.negative_ttl_transient_infra))

    @staticmethod
    def _failure(key, held, cls, detail, *, anchor=None) -> CacheBreach:
        if held:
            reason, event = R_STALE, E_STALE
        elif key[0] == STATUS_LIST:
            reason, event = R_STATUS, E_STATUS
        else:
            reason, event = R_DID_RESOLUTION, E_DID_RESOLUTION
        what = "expired entry refused" if held else "no fresh material"
        return CacheBreach(reason, event, f"{what}; {cls.value}: {detail}",
                           failure_class=cls,
                           anchor=anchor if cls is FailureClass.TRANSIENT_INFRA else None)

    def _live(self, k, now) -> _Entry | None:
        """Entry if unexpired (marked most-recently-used). An expired positive
        entry is kept as the 'held' marker (never served); others are dropped."""
        e = self._entries.get(k)
        if e is None:
            return None
        if now >= e.expires_at:
            if e.kind != "pos":
                del self._entries[k]
            return None
        self._entries.move_to_end(k)
        return e

    def _put(self, k, e: _Entry) -> None:
        self._entries[k] = e
        self._entries.move_to_end(k)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
