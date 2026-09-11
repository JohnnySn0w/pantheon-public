"""Postboot marker -- read/write side of the C3 runtime completion signal.

Mechanism PICK (T-3294 DECISIONS): a runtime marker checked at
library-call tier, per adr/ORG-080-germination-engine.md L343-347
(``[Documentary -- informs C3]``, but the ADR's own v8->v9 fold already
found cross-manager systemd-target ordering structurally broken and
converged on exactly this shape -- "the gate therefore lives at the
germinate-command tier via a runtime completion marker"). Production
path is ``/run/pantheon/germinate/postboot-complete.marker``
(root-owned, mode 0644, tmpfs); non-root test/dev callers override
``MARKER_PATH`` at the module level (mirrors the harness fixture
pattern of substituting a temp root for a real ``/var``/``/run`` path --
see harness/fixtures.py).

Marker body is versioned JSON, NOT the ADR's plain-boot_id-only
documentary shape (``{"marker_version": 1, "boot_id": "<36-char>"}``).
Deviation licensed by ORG-080 L60 ("documentary specifics ... the
reference-implementation is free to demonstrate a different one").
Necessary because C4-3 (postboot-indicator compat warning) requires a
"valid-but-warning body shape (e.g. reserved-for-future-use marker
version)" -- the plain-boot_id shape has no field to express that.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import PrimitiveUnavailable

#: Production marker path (ORG-080 L345). Non-root callers (tests, dev)
#: override this module attribute directly -- see module docstring.
MARKER_PATH: Path = Path("/run/pantheon/germinate/postboot-complete.marker")

#: Real host boot-id source (ORG-080's own reboot-recovery mechanism).
BOOT_ID_PATH: Path = Path("/proc/sys/kernel/random/boot_id")

#: The only marker_version this reader accepts as fully valid. Any other
#: value that otherwise parses is a COMPAT_WARNING (C4-3), not a hard
#: MALFORMED -- the shape is recognized, the version isn't supported.
CURRENT_MARKER_VERSION = 1


@dataclass(frozen=True)
class MarkerPresent:
    """Marker exists, parses, version supported. ``boot_id`` is its body."""

    boot_id: str


@dataclass(frozen=True)
class MarkerAbsent:
    """Marker file does not exist."""


@dataclass(frozen=True)
class MarkerMalformed:
    """Marker exists but its body doesn't parse as the expected shape."""

    reason: str


@dataclass(frozen=True)
class MarkerCompatWarning:
    """Marker parses but reports a marker_version this reader doesn't support."""

    shape: str


#: MarkerState is a closed sum type over the four postboot-marker read
#: outcomes named by T-3294's ask (mirrors the ticket's literal
#: ``{PRESENT(boot_id), ABSENT, MALFORMED(reason), COMPAT_WARNING(shape)}``).
MarkerState = MarkerPresent | MarkerAbsent | MarkerMalformed | MarkerCompatWarning


def current_boot_id() -> str:
    """Read the real host's current boot id from /proc.

    Fail-closed (C4): a read failure raises typed ``PrimitiveUnavailable``
    rather than falling back to any default.
    """

    try:
        raw = BOOT_ID_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrimitiveUnavailable("boot-id", f"read {BOOT_ID_PATH} failed: {exc}") from exc

    boot_id = raw.strip()
    if not boot_id:
        raise PrimitiveUnavailable("boot-id", f"{BOOT_ID_PATH} read as empty")
    return boot_id


def write_marker(boot_id: str) -> None:
    """Atomically write the postboot marker with the given boot_id.

    Real temp-file + rename (matches ADR's own atomic-write pattern and
    T-3290's ``test_temp_state_store_round_trips_a_real_record``
    precedent). Body is versioned JSON at ``CURRENT_MARKER_VERSION``.
    Creates ``MARKER_PATH.parent`` if it doesn't exist (non-root test
    roots aren't pre-created the way production's tmpfs mount is).
    """

    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"marker_version": CURRENT_MARKER_VERSION, "boot_id": boot_id})

    tmp_path = MARKER_PATH.with_suffix(MARKER_PATH.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    tmp_path.chmod(0o644)
    os.rename(tmp_path, MARKER_PATH)


def read_marker() -> MarkerState:
    """Read MARKER_PATH and classify it into one of the four MarkerState variants."""

    if not MARKER_PATH.exists():
        return MarkerAbsent()

    try:
        raw = MARKER_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return MarkerMalformed(f"read {MARKER_PATH} failed: {exc}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return MarkerMalformed(f"body is not valid JSON: {exc}")

    if not isinstance(payload, dict) or "marker_version" not in payload:
        return MarkerMalformed(f"body missing required 'marker_version' field: {raw!r}")

    version = payload["marker_version"]
    if not isinstance(version, int):
        return MarkerMalformed(f"'marker_version' is not an int: {version!r}")

    if version != CURRENT_MARKER_VERSION:
        return MarkerCompatWarning(
            f"marker_version={version} unsupported (this reader supports "
            f"marker_version={CURRENT_MARKER_VERSION})"
        )

    boot_id = payload.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        return MarkerMalformed(f"'boot_id' missing or not a string: {boot_id!r}")

    return MarkerPresent(boot_id=boot_id)
