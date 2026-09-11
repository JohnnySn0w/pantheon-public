"""admission.gate -- the admission-tier refusal path (C1).

Every admission-eligible engine verb calls ``admission.gate.acquire(...)``
as a context manager. On success it yields the run's ``run_id``; on
refusal it raises ``AdmissionRefused``/``PrimitiveUnavailable`` from
*inside* ``__enter__`` -- before the ``with`` block's body executes at
all. There is no window in which a refused caller could reach
mutation-tier code (Python cannot enter a ``with`` block's body after
its context manager's ``__enter__`` raises).

``acquire()`` is a thin wrapper, not a re-catching translator:
``coordinator.claim()`` already raises the two typed exceptions this
module re-exports. ``gate.py`` adds (a) admission-eligible-verb
closed-set validation and (b) context-manager framing (claim on enter,
release on exit) -- it deliberately does NOT catch-and-rewrap
coordinator's exceptions. An extra catch/re-raise layer is itself a
place a future edit could accidentally swallow an exception, which C4
forbids outright.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import coordinator
from .exceptions import AdmissionRefused, PrimitiveUnavailable

__all__ = ["acquire", "ADMISSION_ELIGIBLE_VERBS", "AdmissionRefused", "PrimitiveUnavailable"]

#: The closed set of admission-eligible engine verbs named by C1
#: (ORG-080 Architectural-tier scope carve). Any other value is a
#: programming error, not a normal refusal -- fail loud (ValueError),
#: matching C5's closed-enum philosophy and Tranche B's state-machine
#: closure discipline (PO-confirmed, T-3292 plan approval).
ADMISSION_ELIGIBLE_VERBS = frozenset({"plan", "apply", "resume", "rollback"})


@contextlib.contextmanager
def acquire(
    system_dir: Path,
    instance: str,
    component: str,
    verb: str,
    *,
    scope: str,
    uid: int,
    run_id: str | None = None,
) -> Iterator[str]:
    """Admission-tier context manager. Yields the run_id on success.

    Raises ``AdmissionRefused(scope, reason)`` if another run already
    holds admission for ``(instance, component)``. Raises
    ``PrimitiveUnavailable(name, reason)`` if the coordination primitive
    is unreachable. Raises ``ValueError`` if ``verb`` is outside
    ``ADMISSION_ELIGIBLE_VERBS``.
    """

    if verb not in ADMISSION_ELIGIBLE_VERBS:
        raise ValueError(
            f"verb={verb!r} is not admission-eligible; expected one of "
            f"{sorted(ADMISSION_ELIGIBLE_VERBS)}"
        )

    resolved_run_id = run_id if run_id is not None else uuid.uuid4().hex

    coordinator.claim(
        system_dir,
        instance,
        component,
        run_id=resolved_run_id,
        scope=scope,
        uid=uid,
        verb=verb,
    )
    try:
        yield resolved_run_id
    finally:
        coordinator.release(system_dir, instance, component)
