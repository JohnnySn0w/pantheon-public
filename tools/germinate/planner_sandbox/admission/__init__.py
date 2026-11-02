"""planner_sandbox.admission -- Tranche A of the T-3274 ref-impl build.

Delivers the single-holder coordination primitive (C1) + admission-tier
refusal path + fail-closed on primitive unavailability (C4, coordination-
primitive dimension). See ``tools/germinate/planner_sandbox/README.md``
(Tranche A) and ``adr/ORG-080-germination-engine.md`` (Architectural-tier
scope carve).
"""

from __future__ import annotations

from .coordinator import bootstrap_component
from .exceptions import AdmissionRefused, ClaimHolderMismatch, PrimitiveUnavailable
from .gate import acquire

__all__ = [
    "acquire",
    "bootstrap_component",
    "AdmissionRefused",
    "ClaimHolderMismatch",
    "PrimitiveUnavailable",
]
