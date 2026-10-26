"""Broker-side per-caller rate limiting (ORG-052 Unit 5a, T-2917 ruling i1).

The MCP specification says servers MUST rate-limit. The host MCP adapter's
token bucket is not a security control (a same-uid process can call D-Bus
directly), so the authoritative limit lives here, at the broker's transport
boundary, keyed ONLY by the bus-derived Unix uid (never a caller argument).

Ordering contract at every Manager method: identity first (an unregistered
uid gets the authorization refusal, never ``RateLimited``), then this check,
then the domain call. A refused call reads and writes nothing.

In-memory by design (TG 7236 "stateless"): a restart resets the buckets,
which is acceptable because this is availability control, not authority.
Memory is bounded: at most ``max_tracked`` uids are tracked; the least
recently seen bucket is evicted (a fresh bucket starts full, so eviction can
only ever be more permissive for an idle uid, never lock anyone out).

Pure module: no dbus import, deterministic under an injected clock.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Final, Literal

from tools.host_control.contracts import HostControlError, RateLimited

Budget = Literal["mutating", "read"]

# Manager method -> budget. Closed: an unknown method is a programming
# error and fails loudly rather than escaping the limiter.
METHOD_BUDGETS: Final[dict[str, Budget]] = {
    "Plan": "mutating",
    "Apply": "mutating",
    "Verify": "mutating",
    "Rollback": "mutating",
    "Capabilities": "read",
    "OperationGet": "read",
    "ApprovalGet": "read",
}


@dataclass(frozen=True)
class BucketSpec:
    capacity: float
    refill_per_second: float

    def __post_init__(self) -> None:
        if not self.capacity >= 1 or not self.refill_per_second > 0:
            raise HostControlError("invalid rate-limit bucket spec")


# Defaults sized for a lifecycle/recovery surface, not an everyday control
# plane (ORG-052 section 1): a burst of planning/apply work, then about one
# mutating request every two seconds; reads poll OperationGet more freely.
DEFAULT_MUTATING = BucketSpec(capacity=10, refill_per_second=0.5)
DEFAULT_READ = BucketSpec(capacity=30, refill_per_second=5.0)
DEFAULT_MAX_TRACKED = 1024


class TokenBucketLimiter:
    """Per-key token buckets with bounded, LRU-evicted memory."""

    def __init__(
        self,
        spec: BucketSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        if max_tracked < 1:
            raise HostControlError("invalid rate-limit tracking bound")
        self._spec = spec
        self._clock = clock
        self._max_tracked = max_tracked
        self._buckets: OrderedDict[int, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: int) -> bool:
        now = self._clock()
        with self._lock:
            tokens, stamp = self._buckets.pop(key, (self._spec.capacity, now))
            elapsed = max(0.0, now - stamp)
            tokens = min(
                self._spec.capacity,
                tokens + elapsed * self._spec.refill_per_second,
            )
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_tracked:
                self._buckets.popitem(last=False)
            return allowed

    def tracked(self) -> int:
        with self._lock:
            return len(self._buckets)


class BrokerRateLimits:
    """The broker's two independent per-uid budgets."""

    def __init__(
        self,
        *,
        mutating: BucketSpec = DEFAULT_MUTATING,
        read: BucketSpec = DEFAULT_READ,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        self._limiters: dict[Budget, TokenBucketLimiter] = {
            "mutating": TokenBucketLimiter(
                mutating, clock=clock, max_tracked=max_tracked
            ),
            "read": TokenBucketLimiter(read, clock=clock, max_tracked=max_tracked),
        }

    def check(self, authenticated_uid: int, method: str) -> None:
        """Raise ``RateLimited`` when ``authenticated_uid`` is over budget
        for ``method``'s budget class."""

        if not isinstance(authenticated_uid, int) or isinstance(
            authenticated_uid, bool
        ):
            raise HostControlError("invalid authenticated uid")
        try:
            budget = METHOD_BUDGETS[method]
        except KeyError as exc:
            raise HostControlError("unknown rate-limited method") from exc
        if not self._limiters[budget].allow(authenticated_uid):
            raise RateLimited(f"{budget} request budget exhausted; retry later")
