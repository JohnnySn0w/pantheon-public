"""Typed exceptions for the T-3294 postboot subsystem.

Two exceptions, split by refusal cause (see T-3294 DECISIONS):

- ``PostbootPreconditionUnmet(reason)`` -- the postboot precondition
  genuinely has not been satisfied on this boot yet (marker absent, or
  present but stale for a prior boot). This is C3's refusal shape --
  expected/normal until ``germinate-postboot.service``-equivalent
  completes on the current boot.
- ``PrimitiveUnavailable(name, reason)`` -- the postboot-indicator
  primitive itself is broken, unreadable, or reports an incompatible
  shape (malformed marker body, unsupported ``marker_version``). This is
  C4's fail-closed refusal shape (adr/ORG-080-germination-engine.md
  Architectural-tier scope carve, C4: "unreachable, malformed, or
  reports a compatibility warning"). Mirrors
  ``harness.exceptions.HarnessUnavailable``'s ``(name, reason)``
  constructor shape at this tranche's own production-code tier (T-3290
  cross-tranche invariant) -- a distinct class, not a re-export, since
  harness exceptions are test-harness-only.
"""

from __future__ import annotations


class PostbootPreconditionUnmet(Exception):
    """Raised when postboot has not completed on the current boot.

    Attributes:
        reason: human-readable, concrete description (e.g. "marker
            absent", "marker stale: marker_boot=<x> current_boot=<y>")
            -- never a generic "not ready".
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"postboot precondition unmet: {reason}")


class PrimitiveUnavailable(Exception):
    """Raised when the postboot-indicator primitive itself is broken.

    Attributes:
        name: short identifier for the primitive (e.g.
            "postboot-marker", "boot-id").
        reason: human-readable, concrete description of what was
            observed -- never a generic "not available".
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")
