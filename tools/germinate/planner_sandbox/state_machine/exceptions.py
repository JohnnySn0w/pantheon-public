"""Typed exceptions for the state-machine subsystem (T-3293, Tranche B).

`PrimitiveUnavailable` mirrors `harness.exceptions.HarnessUnavailable`
field-for-field -- the cross-tranche invariant the project-orchestrator
owns (consistent exception shape + error-text convention across every
tranche's fail-closed-on-primitive-unavailability mechanism, ORG-080 C4).
"""

from __future__ import annotations


class PrimitiveUnavailable(Exception):
    """Raised when a real-host primitive this module needs is unreachable.

    Attributes:
        name: short identifier for the missing/malformed primitive (e.g.
            "state-store").
        reason: human-readable, concrete description of what was observed
            (never a generic "not available") -- callers building typed
            pytest SKIP reasons rely on this being informative.
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")
