"""admission.coordinator -- the single-holder coordination primitive (C1).

Real-primitive mechanism: a filesystem-atomic claim. A claim's EXISTENCE
on disk is the mechanism-of-record -- no process needs to stay alive to
"hold" it, so it survives caller-process exit (including SIGKILL) by
construction. This is the ref-impl's answer to the Cairn v9 BLOCK-1
finding (a caller-held ``flock(2)`` releases automatically when the
holding process exits, which breaks C1's "across process lifecycle
boundaries" requirement). See T-3292 DECISIONS for the flock/broker
alternatives considered and rejected, with failure signatures.

Write-then-link protocol (NOT a bare ``os.rename()`` -- verified
empirically that ``os.rename()`` silently replaces an existing
destination and does not give "exactly one claimant wins" semantics):

  1. Write the full claim payload to a per-attempt temp file inside the
     leaf directory, ``fsync()`` it, close it.
  2. ``os.link(tmp_path, canonical_claim_path)`` -- atomic; raises
     ``FileExistsError`` if another claim already holds the canonical
     path. Exactly one contender's ``link()`` call succeeds.
  3. The loser reads the winner's claim (world-readable) to build a
     useful refusal reason, then unlinks its own temp file.

Leaf directory mode is ``0o1777`` (sticky), not the ADR's
flock-only-needs-a-world-readable-leaf scheme: a link-based claim needs
directory WRITE (to create a new name), unlike ``flock`` which only
needs the leaf FILE to be ``open()``-able. Sticky bit gives "anyone may
create, only the owner/root may unlink" -- the same property the ADR's
Base engine mechanics constraint (a) wants (no group-escalation unlink)
realized through a different, outcome-equivalent mechanism (README's
documentary-vs-outcome split, ORG-080 Architectural-tier scope carve).

The leaf directory itself is created once by ``bootstrap_component()``
(the root-equivalent "germinate install" step) -- NOT lazily on first
``claim()`` -- because the leaf's PARENT (``system_dir``, mode 0755)
does not grant "other" write, so a non-owning uid's lazy ``mkdir`` would
fail with ``PermissionError``, not the intended contention semantics.
This mirrors the ADR's own bootstrap-vs-runtime split for its tmpfs
lock file (Base engine mechanics L318-319: only root, only at
"germinate install").
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .exceptions import AdmissionRefused, PrimitiveUnavailable

#: Sticky + world-writable: any uid may create a claim attempt; only the
#: owner (or root) may unlink an existing one (matches /tmp's own model).
_LEAF_MODE = 0o1777

#: World-traversable + writable-by-owner: matches the ADR's own
#: ancestor-chain mode for the gate tree (Base engine mechanics
#: "Gate location").
_INSTANCE_DIR_MODE = 0o755

#: World-readable so a losing caller can read the winner's claim for a
#: diagnostic AdmissionRefused reason (mirrors the ADR's gate-leaf mode).
_CLAIM_FILE_MODE = 0o644

_CLAIM_LEAF_NAME = "_component_gate.claim"

#: Reference name used in every PrimitiveUnavailable this module raises
#: -- the PO's cross-tranche consistency ask (T-3292 DECISIONS).
PRIMITIVE_NAME = "coordination-lock"


def _leaf_dir(system_dir: Path, instance: str, component: str) -> Path:
    return system_dir / instance / component


def _claim_path(system_dir: Path, instance: str, component: str) -> Path:
    return _leaf_dir(system_dir, instance, component) / _CLAIM_LEAF_NAME


def bootstrap_component(system_dir: Path, instance: str, component: str) -> None:
    """Root-equivalent one-time setup: create the leaf dir at 0o1777.

    Idempotent -- safe to call more than once. Does NOT touch any
    existing claim (bootstrap and claim/release are independent
    concerns; re-bootstrapping a component with a live claim leaves the
    claim untouched).
    """

    instance_dir = system_dir / instance
    try:
        instance_dir.mkdir(parents=True, exist_ok=True)
        instance_dir.chmod(_INSTANCE_DIR_MODE)
        leaf = _leaf_dir(system_dir, instance, component)
        leaf.mkdir(exist_ok=True)
        leaf.chmod(_LEAF_MODE)
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"bootstrap failed at {system_dir}: {exc}"
        ) from exc


def read_claim(system_dir: Path, instance: str, component: str) -> dict[str, Any] | None:
    """Return the current claim payload, or ``None`` if unclaimed.

    Raises ``PrimitiveUnavailable`` if the claim path is unreachable in
    a way that is NOT simply "no claim yet" (e.g. the gate tree root was
    renamed out from under a live invocation -- the C4-1 scenario).
    """

    claim_path = _claim_path(system_dir, instance, component)
    try:
        raw = claim_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim unreadable at {claim_path}: {exc}"
        ) from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim malformed at {claim_path}: {exc}"
        ) from exc


def claim(
    system_dir: Path,
    instance: str,
    component: str,
    *,
    run_id: str,
    scope: str,
    uid: int,
    verb: str,
) -> None:
    """Attempt the atomic claim for ``(instance, component)``.

    Raises ``AdmissionRefused(scope, reason)`` if another run already
    holds the claim. Raises ``PrimitiveUnavailable(name, reason)`` if
    the coordination primitive itself is unreachable (bootstrap missing,
    gate tree renamed/removed, permission trouble unrelated to normal
    contention). Returns ``None`` on success -- the claim is now durably
    on disk; the caller holds admission until ``release()`` is called
    explicitly, by the run's own finalize step, NEVER automatically on
    process exit. This is the load-bearing property that survives the
    Cairn v9 BLOCK-1 witness.
    """

    leaf = _leaf_dir(system_dir, instance, component)
    canonical = _claim_path(system_dir, instance, component)

    if not leaf.is_dir():
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME,
            f"leaf directory missing at {leaf} -- bootstrap_component() "
            "was not called for this (instance, component)",
        )

    payload = {
        "run_id": run_id,
        "instance": instance,
        "component": component,
        "scope": scope,
        "uid": uid,
        "verb": verb,
        "claimed_at": time.time(),
    }
    tmp_path = leaf / f".claim-tmp-{uuid.uuid4().hex}"

    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _CLAIM_FILE_MODE)
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"could not open claim-attempt temp file at {leaf}: {exc}"
        ) from exc

    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp_path, canonical)
    except FileExistsError:
        holder = read_claim(system_dir, instance, component)
        _best_effort_unlink(tmp_path)
        reason = (
            f"(instance={instance}, component={component}) already held by "
            f"run_id={holder.get('run_id')!r} scope={holder.get('scope')!r} "
            f"uid={holder.get('uid')!r} verb={holder.get('verb')!r}"
            if holder is not None
            else f"(instance={instance}, component={component}) already held "
            "(holder claim unreadable)"
        )
        raise AdmissionRefused(scope, reason)
    except OSError as exc:
        _best_effort_unlink(tmp_path)
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim link failed at {canonical}: {exc}"
        ) from exc
    else:
        _best_effort_unlink(tmp_path)


def release(system_dir: Path, instance: str, component: str) -> None:
    """Explicitly release a held claim.

    Called only by the run's own finalize path -- never automatically
    on process exit (that automaticity is exactly what the rejected
    caller-held-flock design would have given us, and exactly what C1's
    caller-exit-persistence requirement forbids).
    """

    canonical = _claim_path(system_dir, instance, component)
    try:
        os.unlink(canonical)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"release failed at {canonical}: {exc}"
        ) from exc


def _best_effort_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
