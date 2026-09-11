"""require_or_refuse() -- the single universal C3 enforcement entry-point.

Every invocation surface (CLI, systemd unit ExecStart, library call from
another Pantheon component) MUST call this before any mutation-tier code
runs. C3's "universal across invocation surfaces" property holds
precisely because there is exactly one function that decides, and every
surface calls the identical one -- see
tools/germinate/planner_sandbox/tests/test_postboot_c3.py (C3-1/2/3 each
prove a distinct invocation transport routing through this same
function).
"""

from __future__ import annotations

from . import marker
from .exceptions import PostbootPreconditionUnmet, PrimitiveUnavailable


def require_or_refuse() -> None:
    """Raise unless postboot has completed on the current boot.

    Dispatch by ``marker.read_marker()``'s ``MarkerState``:

    - ``MarkerAbsent`` / stale ``MarkerPresent`` (boot_id mismatch) ->
      ``PostbootPreconditionUnmet`` (C3: precondition genuinely not yet
      met -- expected/normal, not a primitive malfunction).
    - ``MarkerMalformed`` / ``MarkerCompatWarning`` ->
      ``PrimitiveUnavailable`` (C4: the primitive itself is broken or
      reports an unsupported shape -- never degrade to "present, good
      enough").
    - ``MarkerPresent`` with matching boot_id -> returns ``None``
      (admitted).
    """

    state = marker.read_marker()

    if isinstance(state, marker.MarkerAbsent):
        raise PostbootPreconditionUnmet("postboot marker absent")

    if isinstance(state, marker.MarkerMalformed):
        raise PrimitiveUnavailable("postboot-marker", state.reason)

    if isinstance(state, marker.MarkerCompatWarning):
        raise PrimitiveUnavailable("postboot-marker", f"compat warning: {state.shape}")

    # isinstance(state, marker.MarkerPresent) -- the only remaining variant.
    current = marker.current_boot_id()
    if state.boot_id != current:
        raise PostbootPreconditionUnmet(
            f"postboot marker stale: marker_boot={state.boot_id} current_boot={current}"
        )

    return None
