"""ORG-071 D1 + D5 — the rate limiter: tiered budgets, handshake rates and
concurrency, outbound amplification bounds, weighted cost (T-2857 PR2).

One `RateLimiter` is an AdmissionGate (netcaps_admission) for PassportHandshake
and can also be charged directly (`charge`) by a component doing metered work
outside the handshake, e.g. a VC verify or a status-list decode.

Accounting: fixed windows of ACCOUNTING_WINDOW_S on the injected clock, one
window per key, started at the key's first use. A "per min" rate becomes
rate * window / 60 per window. Budgets are D5 cost units per window.

Keys (ORG-071 D1(b), D4 pre-auth key):
  * A claim on an ALLOWLISTED DID from a real source is keyed per source for
    the per-source rate, concurrency and amplification bounds, and per
    (source, claimed DID) for the pre-auth budget AND the per-counterparty rate
    and concurrency bounds. Kin DIDs are public, so anything keyed on the
    claimed DID alone would let any source lock a counterparty out before it
    proves possession (D4: pre-auth load counts only against the source +
    claimed DID). The per-anchor resolution rate is likewise per (source,
    anchor) for these claims. Bounds on the VERIFIED DID belong to POST_AUTH
    (PR4). The global caps stay shared: a distributed attacker can still fill
    the global resolution rate or concurrency (accepted trade-off; reserved
    headroom for known claims is a first-fire tuning item).
  * Everything else (an unallowlisted DID, or source=None) is the single
    UNKNOWN_SOURCE pool: one key in every per-source / per-counterparty table and
    the shared `budget_unknown_source`. Unknown parties therefore hold, all
    together, at most one source's share of every global bound (e.g. per-source concurrency of the
    global slots, per-source resolution rate of the global rate) and cannot
    starve known counterparties. They add no table keys of their own (every
    table uses the one shared pool key); the only per-party state is the
    bounded distinct-DID / distinct-anchor sets under it, capped per window.
  * The authenticated-ungranted, granted and known-degraded tiers are keyed by
    the verified DID. The limiter's own check() never charges them; the D4
    quota gate (netcaps_quota.QuotaGate, PR4) charges them through `charge()`,
    once per verified handshake at POST_AUTH.

Accepted properties (ORG-071 T-2857 reviews):
  * Fixed windows can admit up to 2x a limit across a window boundary (a full
    budget at the end of one window and again at the start of the next).
  * Per-anchor resolution for a known claim is in effect per (source, anchor)
    plus the global rate (the HO-accepted reading on T-2857).
  * Failures are charged to the source, never to the claimed DID (D1/D4).

Excess is REJECTED, never degraded: a check returns a NetcapsBreach (internal
reason + audit event value), the handshake turns it into an ABORT through
_breach(), which emits the one audit event. Every check is atomic: a refused
check consumes nothing.

Bounded state: every keyed table holds at most `max_tracked_keys` keys. A new
key arriving at a full table first evicts expired windows; if the table is
still full the new key is REJECTED (fail-closed), never tracked. Concurrency
tables drop a key when its count returns to zero, so they are bounded by the
global concurrency cap.

`resolution_rate_per_source_unresolved` counts every resolution the handshake
attempts, cached or not: the cache (PR3) sits inside the TxtResolver, out of
the limiter's sight. Counting cache hits too is the stricter direction.
"""

from __future__ import annotations

import threading
import urllib.parse
from enum import Enum
from typing import Hashable, Mapping

from tools.a2a.did_web import DidResolutionError, resolve_did_web_url
from tools.a2a.netcaps import (
    ACCOUNTING_WINDOW_S,
    AuditEventType,
    Clock,
    MonotonicClock,
    NetcapsParams,
)
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_breach import NetcapsBreach

# Internal reasons: the AbortReason VALUES (passport_handshake), kept as strings
# so this module never imports the handshake (it imports us via the seam).
RATE_LIMITED = "rate_limit_rejected"
QUOTA_EXHAUSTED = "quota_exhausted"
UNKNOWN_DID_BUDGET_EXCEEDED = "unknown_did_budget_exceeded"

_EVENT = {
    RATE_LIMITED: AuditEventType.RATE_LIMIT_REJECTED.value,
    QUOTA_EXHAUSTED: AuditEventType.QUOTA_EXHAUSTED.value,
    UNKNOWN_DID_BUDGET_EXCEEDED: AuditEventType.UNKNOWN_DID_BUDGET_EXCEEDED.value,
}

UNKNOWN_SOURCE = "<unknown-source>"
_GLOBAL = "<global>"
DEFAULT_MAX_TRACKED_KEYS = 1024


class Tier(Enum):
    """ORG-071 D1(b) budget tiers, keyed by verification state."""

    PREAUTH = "budget_preauth"
    AUTHENTICATED_UNGRANTED = "budget_authenticated_ungranted"
    GRANTED = "budget_granted"
    KNOWN_DEGRADED = "budget_known_degraded"
    UNKNOWN_SOURCE = "budget_unknown_source"


_TIER_REASON = {
    Tier.GRANTED: QUOTA_EXHAUSTED,
    Tier.UNKNOWN_SOURCE: UNKNOWN_DID_BUDGET_EXCEEDED,
}


class Op(Enum):
    """Metered operations (ORG-071 D5 weighted cost)."""

    HANDSHAKE_START = "handshake_start"
    TOKEN_CHECK = "token_check"
    DID_RESOLVE = "did_resolve"
    SIGNATURE_VERIFY = "signature_verify"
    STATUS_LIST_DECODE = "status_list_decode"
    VC_VERIFY = "vc_verify"


# Provisional first-fire weights (1 unit = one cheap token check), the uniform
# MINIMUM: an adapter or deployment may only raise them (stricter_costs).
DEFAULT_COSTS: Mapping[Op, float] = {
    Op.HANDSHAKE_START: 1,
    Op.TOKEN_CHECK: 1,
    Op.DID_RESOLVE: 1,
    Op.SIGNATURE_VERIFY: 2,
    Op.STATUS_LIST_DECODE: 4,
    Op.VC_VERIFY: 8,
}

# The PRESENT-phase op per Scheme NAME. A scheme not named here is charged as
# the dearest op (fail-closed: unknown cost is assumed high).
SCHEME_OPS: Mapping[str, Op] = {
    "API_KEY": Op.TOKEN_CHECK,
    "OIDC_BEARER": Op.SIGNATURE_VERIFY,
    "MTLS": Op.SIGNATURE_VERIFY,
}


def stricter_costs(overrides: Mapping[Op, float] | None = None) -> dict[Op, float]:
    """DEFAULT_COSTS with `overrides` applied; raises ValueError if any override
    is below the default (looser than the uniform minimum, ORG-071 D5)."""
    costs = dict(DEFAULT_COSTS)
    for op, cost in (overrides or {}).items():
        if not isinstance(op, Op):
            raise ValueError(f"unknown op {op!r}")
        if not cost >= DEFAULT_COSTS[op]:
            raise ValueError(f"{op.value} cost {cost!r} is looser than the "
                             f"uniform minimum {DEFAULT_COSTS[op]}")
        costs[op] = cost
    return costs


def anchor_of(endpoint_id: str) -> str:
    """The anchor host a resolution of `endpoint_id` reaches: the did:web host
    (lower-cased, port dropped), else the identifier itself."""
    try:
        host = urllib.parse.urlsplit(resolve_did_web_url(endpoint_id)).hostname
    except (DidResolutionError, ValueError):
        host = None
    return host or endpoint_id


def _breach(reason: str, detail: str) -> NetcapsBreach:
    return NetcapsBreach(reason, _EVENT[reason], detail)


# ---------------------------------------------------------------------------
# Bounded windowed tables.
# ---------------------------------------------------------------------------

_FULL = "full"


class _Table:
    """key -> [window_start, state] for one window length, at most max_keys
    keys. Subclasses define the fresh state and the fit/commit rules."""

    def __init__(self, window: float, max_keys: int) -> None:
        self._window = window
        self._max = max_keys
        self._entries: dict[Hashable, list] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _live(self, key, now):
        e = self._entries.get(key)
        if e is not None and now - e[0] >= self._window:
            return None
        return e

    def _room(self, key, now) -> bool:
        if key in self._entries or len(self._entries) < self._max:
            return True
        cutoff = now - self._window
        for k in [k for k, e in self._entries.items() if e[0] <= cutoff]:
            del self._entries[k]
        return len(self._entries) < self._max

    def _entry(self, key, now) -> list:
        e = self._live(key, now)
        if e is None:
            e = self._entries[key] = [now, self._fresh()]
        return e


class _Counter(_Table):
    """Units used per key per window (rates count 1, budgets count cost)."""

    def _fresh(self):
        return 0.0

    def fits(self, key, amount: float, limit: float, now: float) -> str | None:
        if not self._room(key, now):
            return _FULL
        e = self._live(key, now)
        used = e[1] if e is not None else 0.0
        return None if used + amount <= limit else "over limit"

    def commit(self, key, amount: float, now: float) -> None:
        self._entry(key, now)[1] += amount


class _Distinct(_Table):
    """Distinct items seen per key per window (DIDs / anchors per source)."""

    def _fresh(self):
        return set()

    def fits(self, key, item, limit: float, now: float) -> str | None:
        if not self._room(key, now):
            return _FULL
        e = self._live(key, now)
        seen = e[1] if e is not None else set()
        return None if item in seen or len(seen) < limit else "over limit"

    def commit(self, key, item, now: float) -> None:
        self._entry(key, now)[1].add(item)


# ---------------------------------------------------------------------------
# The limiter.
# ---------------------------------------------------------------------------


class RateLimiter:
    """ORG-071 central load policy (D1 + D5), an AdmissionGate. Thread-safe."""

    TABLES = 12  # bounded keyed tables (for the state-bound assertion)

    def __init__(self, params: NetcapsParams | None = None, *,
                 clock: Clock | None = None,
                 costs: Mapping[Op, float] | None = None,
                 max_tracked_keys: int = DEFAULT_MAX_TRACKED_KEYS) -> None:
        if max_tracked_keys < 1:
            raise ValueError("max_tracked_keys must be >= 1")
        self.params = params or NetcapsParams()
        self.clock = clock or MonotonicClock()
        self.costs = stricter_costs(costs)
        self._lock = threading.Lock()
        w, n = ACCOUNTING_WINDOW_S, max_tracked_keys
        self._budget = {t: _Counter(w, n) for t in Tier}             # 5
        self._rate_source = _Counter(w, n)
        self._rate_cp = _Counter(w, n)
        self._res_global = _Counter(w, n)
        self._res_source = _Counter(w, n)
        self._res_anchor = _Counter(w, n)
        self._dids = _Distinct(w, n)
        self._anchors = _Distinct(w, n)       # = 12 bounded tables
        self._conc_global = 0
        self._conc_source: dict[str, int] = {}
        self._conc_cp: dict[str, int] = {}

    # -- introspection (tests, ops) ------------------------------------------

    def tracked_keys(self) -> int:
        with self._lock:
            return (sum(len(t) for t in self._budget.values())
                    + len(self._rate_source) + len(self._rate_cp)
                    + len(self._res_global) + len(self._res_source)
                    + len(self._res_anchor) + len(self._dids) + len(self._anchors)
                    + len(self._conc_source) + len(self._conc_cp))

    def in_flight(self) -> int:
        with self._lock:
            return self._conc_global

    # -- direct metering -----------------------------------------------------

    def charge(self, tier: Tier, key: Hashable, op: Op, *,
               limit: float | None = None) -> NetcapsBreach | None:
        """Consume `op`'s weighted cost from `tier`'s budget for `key`.
        `limit` replaces the tier's NetcapsParams budget for this key (a
        per-grant quota, ORG-071 D4); None means the tier's budget."""
        with self._lock:
            return self._apply([self._budget_item(tier, key, op, limit)])

    # -- AdmissionGate -------------------------------------------------------

    def check(self, ctx: AdmissionContext) -> NetcapsBreach | None:
        tier, source, cp, bkey = self._keys(ctx)
        p, per_window = self.params, ACCOUNTING_WINDOW_S / 60
        with self._lock:
            if ctx.phase is AdmissionPhase.START:
                breach = self._concurrency_fits(source, cp)
                if breach is not None:
                    return breach
                breach = self._apply([
                    (self._rate_source, source, 1,
                     p.rate_handshake_per_source * per_window, RATE_LIMITED,
                     "handshake rate per source"),
                    (self._rate_cp, cp, 1,
                     p.rate_handshake_per_counterparty * per_window, RATE_LIMITED,
                     "handshake rate per counterparty"),
                    self._budget_item(tier, bkey, Op.HANDSHAKE_START),
                ])
                if breach is None:
                    self._conc_global += 1
                    self._conc_source[source] = self._conc_source.get(source, 0) + 1
                    self._conc_cp[cp] = self._conc_cp.get(cp, 0) + 1
                return breach
            if ctx.phase is AdmissionPhase.RESOLVE:
                anchor = anchor_of(ctx.endpoint_id)
                # Known claims: per (source, anchor), so sources claiming a
                # public kin DID cannot lock its anchor out; a distributed
                # attacker reaches a kin anchor only up to the global rate.
                # Unknown claims stay keyed on the anchor alone.
                anchor_key = (source, anchor) if tier is Tier.PREAUTH else anchor
                if ctx.resource == "did_doc":
                    # The DID-doc fetch of a run whose TXT RESOLVE already
                    # recorded this source, DID and anchor (T-5399 PR-b): charge
                    # the true outbound load (global, per-anchor, budget) but not
                    # the per-source unresolved rate or distinct sets, which would
                    # double-count one claim. Per-source doc fetches stay bounded
                    # by the TXT RESOLVE every run passes first. Any other
                    # resource value takes the full charge below (stricter).
                    return self._apply([
                        (self._res_global, _GLOBAL, 1,
                         p.resolution_rate_global * per_window, RATE_LIMITED,
                         "global resolution rate (DID doc)"),
                        (self._res_anchor, anchor_key, 1,
                         p.resolution_rate_per_anchor * per_window, RATE_LIMITED,
                         "resolution rate per anchor (DID doc)"),
                        self._budget_item(tier, bkey, Op.DID_RESOLVE),
                    ])
                return self._apply([
                    (self._res_global, _GLOBAL, 1,
                     p.resolution_rate_global * per_window, RATE_LIMITED,
                     "global resolution rate"),
                    (self._res_source, source, 1,
                     p.resolution_rate_per_source_unresolved * per_window,
                     UNKNOWN_DID_BUDGET_EXCEEDED, "unresolved-DID rate per source"),
                    (self._res_anchor, anchor_key, 1,
                     p.resolution_rate_per_anchor * per_window, RATE_LIMITED,
                     "resolution rate per anchor"),
                    (self._dids, source, ctx.endpoint_id,
                     p.max_distinct_dids_per_source_window,
                     UNKNOWN_DID_BUDGET_EXCEEDED, "distinct DIDs per source"),
                    (self._anchors, source, anchor,
                     p.max_distinct_anchors_per_source_window, RATE_LIMITED,
                     "distinct anchors per source"),
                    self._budget_item(tier, bkey, Op.DID_RESOLVE),
                ])
            if ctx.phase is AdmissionPhase.PRESENT:
                op = SCHEME_OPS.get(ctx.scheme or "", None)
                if op is None:
                    op = max(Op, key=lambda o: self.costs[o])
                return self._apply([self._budget_item(tier, bkey, op)])
            return None  # POST_AUTH: netcaps_quota.QuotaGate's phase (D4)

    def release(self, ctx: AdmissionContext) -> None:
        _, source, cp, _ = self._keys(ctx)
        with self._lock:
            if self._conc_global == 0:
                return
            self._conc_global -= 1
            for table, key in ((self._conc_source, source), (self._conc_cp, cp)):
                n = table.get(key, 0) - 1
                if n > 0:
                    table[key] = n
                else:
                    table.pop(key, None)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _keys(ctx: AdmissionContext):
        if ctx.known and ctx.source is not None:
            pair = (ctx.source, ctx.endpoint_id)
            return Tier.PREAUTH, ctx.source, pair, pair
        return Tier.UNKNOWN_SOURCE, UNKNOWN_SOURCE, UNKNOWN_SOURCE, UNKNOWN_SOURCE

    def _budget_item(self, tier: Tier, key, op: Op, limit: float | None = None):
        return (self._budget[tier], key, self.costs[op],
                getattr(self.params, tier.value) if limit is None else limit,
                _TIER_REASON.get(tier, RATE_LIMITED),
                f"{tier.value} ({op.value} costs {self.costs[op]})")

    def _concurrency_fits(self, source: str, cp: str) -> NetcapsBreach | None:
        p = self.params
        for used, cap, what in (
            (self._conc_global, p.max_concurrent_handshakes_global, "global"),
            (self._conc_source.get(source, 0),
             p.max_concurrent_handshakes_per_source, "per source"),
            (self._conc_cp.get(cp, 0),
             p.max_concurrent_handshakes_per_counterparty, "per counterparty"),
        ):
            if used >= cap:
                return _breach(RATE_LIMITED, f"concurrent handshakes {what} "
                                             f"at cap {cap}")
        return None

    def _apply(self, items) -> NetcapsBreach | None:
        """Check every item, then commit all of them, or none (atomic)."""
        now = self.clock.monotonic()
        for table, key, x, limit, reason, what in items:
            why = table.fits(key, x, limit, now)
            if why == _FULL:
                return _breach(reason, f"{what}: tracked-key table full, "
                                       "new key refused (fail-closed)")
            if why is not None:
                return _breach(reason, f"{what}: {why} {limit:g}/window")
        for table, key, x, _limit, _reason, _what in items:
            table.commit(key, x, now)
        return None
