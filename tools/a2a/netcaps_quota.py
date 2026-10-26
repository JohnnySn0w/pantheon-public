"""ORG-071 D4 — the per-counterparty quota on the post-auth key (T-2857 PR4).

A grant (ORG-069 D2) carries a quota: the post-auth load dimension. `QuotaGate`
is an AdmissionGate (netcaps_admission) that acts only at POST_AUTH, keyed by
the verified DID (`ctx.principal`). Every earlier phase is a no-op for it, so
pre-auth load under a claimed DID (charged by the RateLimiter to the
(source, claimed DID) pre-auth budget) and any handshake that ABORTs before
POST_AUTH never reach a counterparty's quota.

The single charge: one `RateLimiter.charge()` per verified handshake, here, at
POST_AUTH, of Op.HANDSHAKE_START's weight. The limiter's own check() charges no
post-auth tier, so nothing double-counts. Accounting is the limiter's: same
windows, same atomic refusal (a refused charge consumes nothing), same
bounded-table rule (at most max_tracked_keys DIDs per tier; a new DID at a
full table is refused, fail-closed).

Tier per verified DID, from the injected `GrantLookup`:
  * allowlisted AND granted -> Tier.GRANTED, limit = the grant's quota;
  * otherwise              -> Tier.AUTHENTICATED_UNGRANTED,
                              limit = budget_authenticated_ungranted.

Exhaustion is a throttle (D4 soft-deny): the gate returns a NetcapsBreach with
reason/event `quota_exhausted`; the handshake ABORTs through _breach(), the
single audit emitter. The gate emits nothing and writes no grant or allowlist
state; the window rolls over and the counterparty is admitted again.

Fail-closed on a bad grant source: a lookup that raises, or a quota that is
negative, non-finite, non-numeric, a bool or too large to represent, is refused
as `quota_exhausted` (the detail names the exception type, never its message).

Accepted: a grant revoked mid-window starts a fresh ungranted window for that
DID (a different tier, hence a different counter). The most it can then spend is
`budget_authenticated_ungranted`, which NetcapsParams keeps <= `budget_granted`.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Protocol, runtime_checkable

from tools.a2a.netcaps import AuditEventType, NetcapsParams
from tools.a2a.netcaps_admission import AdmissionContext, AdmissionPhase
from tools.a2a.netcaps_breach import NetcapsBreach
from tools.a2a.netcaps_ratelimit import QUOTA_EXHAUSTED, Op, RateLimiter, Tier

ADMIT_OP = Op.HANDSHAKE_START  # the weight one verified handshake costs


@runtime_checkable
class GrantLookup(Protocol):
    """Where a verified DID's quota comes from (ORG-069's grant store later).
    Returns the grant's quota in D5 units per window, or None when the DID
    holds no grant."""

    def grant_quota(self, did: str) -> float | None: ...


class StaticGrants:
    """An in-memory GrantLookup: {verified DID: quota}."""

    def __init__(self, quotas: Mapping[str, float]) -> None:
        self._quotas = dict(quotas)

    @classmethod
    def granted(cls, dids: Iterable[str], params: NetcapsParams) -> "StaticGrants":
        """Each DID granted the default quota, `budget_granted`."""
        return cls({d: params.budget_granted for d in dids})

    def grant_quota(self, did: str) -> float | None:
        return self._quotas.get(did)


def _refuse(detail: str) -> NetcapsBreach:
    return NetcapsBreach(QUOTA_EXHAUSTED, AuditEventType.QUOTA_EXHAUSTED.value,
                         detail)


def _valid(quota) -> bool:
    """A usable quota: a real, finite, non-negative int/float. An int beyond
    float range (10**400) makes isfinite raise OverflowError: unrepresentable
    is invalid. Any quota that passes is safe inside RateLimiter.charge (plain
    comparison and a %g format, both fine up to the float maximum)."""
    if isinstance(quota, bool) or not isinstance(quota, (int, float)):
        return False
    try:
        return math.isfinite(quota) and quota >= 0
    except (OverflowError, ValueError):
        return False


class QuotaGate:
    """ORG-071 D4 quota enforcer, an AdmissionGate. Holds no state of its own:
    the windows live in the shared `limiter`."""

    def __init__(self, limiter: RateLimiter, grants: GrantLookup | None = None):
        self._limiter = limiter
        self._grants = grants or StaticGrants({})

    def check(self, ctx: AdmissionContext) -> NetcapsBreach | None:
        if ctx.phase is not AdmissionPhase.POST_AUTH:
            return None
        did = ctx.principal
        if not did:
            return _refuse("post-auth without a verified DID (fail-closed)")
        # `known` is always True for a verified handshake (the DID passed the
        # allowlist before POST_AUTH); the check stays as defence in depth
        # because the gate is public and may be used outside run().
        try:
            quota = self._grants.grant_quota(did) if ctx.known else None
        except Exception as exc:  # noqa: BLE001 - fail-closed on a bad lookup
            return _refuse(f"grant lookup raised {type(exc).__name__} "
                           "(fail-closed)")
        if quota is None:
            tier, limit = Tier.AUTHENTICATED_UNGRANTED, None
        elif _valid(quota):
            tier, limit = Tier.GRANTED, quota
        else:
            return _refuse(f"invalid grant quota {quota!r} (fail-closed)")
        breach = self._limiter.charge(tier, did, ADMIT_OP, limit=limit)
        # The limiter names an ungranted-tier overflow rate_limit_rejected; for
        # D4 every post-auth refusal is the counterparty's quota.
        return None if breach is None else _refuse(breach.detail)

    def release(self, ctx: AdmissionContext) -> None:
        return None  # no in-flight state
