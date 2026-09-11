"""Typed exceptions for the T-3292 admission subsystem.

Mirrors ``harness.exceptions.HarnessUnavailable``'s shape exactly (two
required positional strings, stored as same-named attributes, formatted
``__str__``) so ``PrimitiveUnavailable`` becomes the reference shape
tranches B/C/D's own primitives (state-store, postboot indicator) mirror
for cross-tranche C4 consistency (T-3292 DECISIONS -- the PO's explicit
ask, ORG-080 Architectural-tier scope carve C4).

Never caught-and-degraded: every call site in this subsystem either lets
these propagate or turns them into a typed refusal. No silent-swallow
path exists anywhere in this module tree.
"""

from __future__ import annotations


class PrimitiveUnavailable(Exception):
    """The coordination primitive is unreachable, malformed, or otherwise
    fails closed per C4.

    Attributes:
        name: short identifier for the unavailable primitive (e.g.
            "coordination-lock").
        reason: concrete, human-readable description of what was
            observed -- never a generic "not available".
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")


class AdmissionRefused(Exception):
    """A second admission attempt found the coordination primitive
    already held. Raised BEFORE any mutation-tier code runs (C1).

    Attributes:
        scope: the calling scope's identifier (e.g. "system",
            "seat-1000") -- who was refused.
        reason: concrete, human-readable description, including (where
            known) the current holder's claim payload for diagnosis.
    """

    def __init__(self, scope: str, reason: str) -> None:
        self.scope = scope
        self.reason = reason
        super().__init__(f"admission refused (scope={scope}): {reason}")
