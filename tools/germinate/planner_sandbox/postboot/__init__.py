"""T-3294 postboot subsystem -- C3 (universal postboot precondition) +
partial C4 (fail-closed on postboot-indicator unavailability).

See tools/germinate/planner_sandbox/README.md (§C3, §C4) and
adr/ORG-080-germination-engine.md (Architectural-tier scope carve) for
what this proves and why.
"""

from .enforcement import require_or_refuse
from .exceptions import PostbootPreconditionUnmet, PrimitiveUnavailable
from .marker import (
    CURRENT_MARKER_VERSION,
    MarkerAbsent,
    MarkerCompatWarning,
    MarkerMalformed,
    MarkerPresent,
    MarkerState,
    current_boot_id,
    read_marker,
    write_marker,
)
from .substrate_ordering import gate_prelude_source, source_is_gated

__all__ = [
    "CURRENT_MARKER_VERSION",
    "MarkerAbsent",
    "MarkerCompatWarning",
    "MarkerMalformed",
    "MarkerPresent",
    "MarkerState",
    "PostbootPreconditionUnmet",
    "PrimitiveUnavailable",
    "current_boot_id",
    "gate_prelude_source",
    "read_marker",
    "require_or_refuse",
    "source_is_gated",
    "write_marker",
]
