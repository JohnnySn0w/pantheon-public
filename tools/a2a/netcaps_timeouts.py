"""ORG-071 D3 — timeouts everywhere; a timeout is an ABORT (T-2857 PR1).

Named per-step timeouts (DNS-lookup, TCP-connect, TLS-handshake, per-fetch
read, per-handshake round-trip) plus a total handshake deadline that caps the
whole exchange, so a slow counterparty cannot walk the per-step timeouts
indefinitely. Values come from NetcapsParams (provisional; T-2857).

How it bounds a synchronous injected call without threads: `HandshakeDeadline.
call()` refuses to start a step once the total deadline is spent, and DISCARDS
any result that arrives after the step's budget (min of the step timeouts and
what is left of the total) — the late value is never used, the step raises
StepTimeout and the handshake ABORTs. Pre-empting a socket mid-read is the job
of the network-touching concretion outside Layer A: it reads `budget()` for its
socket timeouts and may raise StepTimeout(<sub-step>) (or any TimeoutError) to
say exactly which sub-step blew. Either way the outcome is fail-closed.

Only TimeoutError (and so StepTimeout) is translated into a timeout ABORT. A
network concretion must therefore map its client library's own timeout class
(e.g. a library `Timeout` that is not a TimeoutError subclass) to TimeoutError
or StepTimeout itself; an unmapped library timeout is just an exception from the
injected dependency, not a D3 timeout.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, TypeVar

from tools.a2a.netcaps import Clock, MonotonicClock, NetcapsParams

T = TypeVar("T")


class TimeoutStep(Enum):
    """The ORG-071 D3 named step categories."""

    DNS = "dns_lookup"
    CONNECT = "tcp_connect"
    TLS = "tls_handshake"
    FETCH_READ = "fetch_read"
    ROUNDTRIP = "handshake_roundtrip"


_STEP_PARAM = {
    TimeoutStep.DNS: "timeout_dns",
    TimeoutStep.CONNECT: "timeout_connect",
    TimeoutStep.TLS: "timeout_tls",
    TimeoutStep.FETCH_READ: "timeout_fetch_read",
    TimeoutStep.ROUNDTRIP: "timeout_handshake_roundtrip",
}

# One HTTPS fetch (Agent Card, DID doc, status list) = connect + TLS + read.
FETCH_STEPS = (TimeoutStep.CONNECT, TimeoutStep.TLS, TimeoutStep.FETCH_READ)


class StepTimeout(TimeoutError):
    """A step overran its budget. `steps` names the step(s) that were running
    (empty for a deadline spent before the step began); `total` is True when the
    total handshake deadline, not the steps' own timeouts, was the bound that
    bit. Injected dependencies may raise it themselves to name a sub-step."""

    def __init__(self, *steps: TimeoutStep, total: bool = False,
                 detail: str = "") -> None:
        self.steps = steps
        self.total = total
        super().__init__(f"{self.label} timeout{': ' + detail if detail else ''}")

    @property
    def step(self) -> TimeoutStep | None:
        """The single step that blew, or None (composite / total)."""
        return self.steps[0] if len(self.steps) == 1 else None

    @property
    def label(self) -> str:
        """Stable name for logs/audit: 'total', or the step value(s)."""
        if self.total:
            return "total"
        return "+".join(s.value for s in self.steps) or "unknown"


class TimeoutPolicy:
    """Holds the D3 parameters + clock; start() opens one handshake's deadline."""

    def __init__(self, params: NetcapsParams | None = None, *,
                 clock: Clock | None = None) -> None:
        self.params = params or NetcapsParams()
        self.clock = clock or MonotonicClock()

    def start(self) -> HandshakeDeadline:
        return HandshakeDeadline(self.params, self.clock)


class HandshakeDeadline:
    """One handshake's total deadline, from the moment start() was called."""

    def __init__(self, params: NetcapsParams, clock: Clock) -> None:
        self._params = params
        self._clock = clock
        self._ends_at = clock.monotonic() + params.timeout_total_handshake

    def remaining(self) -> float:
        return max(0.0, self._ends_at - self._clock.monotonic())

    def _own(self, steps: tuple[TimeoutStep, ...]) -> float:
        return sum(getattr(self._params, _STEP_PARAM[s]) for s in steps)

    def budget(self, steps: tuple[TimeoutStep, ...]) -> float:
        """Seconds allowed for `steps` run back to back: the sum of their named
        timeouts, capped by what is left of the total deadline."""
        return min(self._own(steps), self.remaining())

    def call(self, steps: tuple[TimeoutStep, ...], fn: Callable[..., T],
             *args) -> T:
        """Run fn(*args) as `steps`; return its value only if it finished in
        budget, else raise StepTimeout (the value is discarded, never used)."""
        if self.remaining() <= 0:
            raise StepTimeout(total=True, detail="deadline spent before step start")
        own = self._own(steps)
        budget = self.budget(steps)
        t0 = self._clock.monotonic()
        try:
            value = fn(*args)
        except StepTimeout:
            raise
        except TimeoutError as exc:
            raise StepTimeout(*steps, detail=f"dependency timeout: {exc}") from exc
        elapsed = self._clock.monotonic() - t0
        if elapsed > budget:
            names = "+".join(s.value for s in steps)
            # budget < own means the total deadline, not the step, was binding.
            raise StepTimeout(*steps, total=budget < own, detail=(
                f"{names} took {elapsed:.3f}s > budget {budget:.3f}s"))
        return value
