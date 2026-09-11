"""Typed exceptions for the planner_sandbox harness.

Fixtures and helpers raise HarnessUnavailable (never silently degrade or
swallow) when a real-host primitive the harness depends on is missing,
malformed, or reports an unexpected state. This mirrors the ADR's own
PrimitiveUnavailable(name, reason) shape for C4 (fail-closed on primitive
unavailability, adr/ORG-080-germination-engine.md) at the harness's own
meta-level -- see T-3290 DECISIONS.
"""

from __future__ import annotations


class HarnessUnavailable(Exception):
    """Raised when a real-host primitive the harness needs is unavailable.

    Attributes:
        primitive: short identifier for the missing/malformed primitive
            (e.g. "systemd-user", "boot_id").
        reason: human-readable, concrete description of what was observed
            (never a generic "not available") -- callers building typed
            pytest SKIP reasons rely on this being informative.
    """

    def __init__(self, primitive: str, reason: str) -> None:
        self.primitive = primitive
        self.reason = reason
        super().__init__(f"{primitive} unavailable: {reason}")
