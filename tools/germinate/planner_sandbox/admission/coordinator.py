"""admission.coordinator -- the single-holder coordination primitive (C1).

Real-primitive mechanism: a filesystem-atomic claim. A claim's EXISTENCE
on disk is the mechanism-of-record -- no process needs to stay alive to
"hold" it, so it survives caller-process exit (including SIGKILL) by
construction. This is the ref-impl's answer to the Consultant v9 BLOCK-1
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

import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from .exceptions import AdmissionRefused, ClaimHolderMismatch, PrimitiveUnavailable

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
    renamed out from under a live invocation -- the C4-1 scenario), or if
    the claim file exists but is not a JSON object (``null`` included,
    T-5826): a corrupt claim is never read as unclaimed.
    """

    claim_path = _claim_path(system_dir, instance, component)
    try:
        raw = claim_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim unreadable at {claim_path}: {exc}"
        ) from exc

    return _parse_claim(raw, claim_path)


def _parse_claim(raw: bytes, canonical: Path) -> dict[str, Any]:
    """The ONE claim parser (T-5826), shared by ``read_claim``, ``claim`` (via
    ``read_claim``) and ``release`` (via ``_read_claim_at``).

    Returns the claim object. Anything else -- not UTF-8, not JSON, or JSON
    that is not an object (``null`` included) -- raises ``PrimitiveUnavailable``
    (fail closed, C4). It never returns ``None``: ``None`` is reserved for an
    ABSENT claim file, decided by the callers before they reach this parser.
    """

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim malformed at {canonical}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME,
            f"claim malformed at {canonical}: expected a JSON object, got {type(payload).__name__}",
        )
    return payload


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
    Consultant v9 BLOCK-1 witness.
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
        _best_effort_unlink(tmp_path)  # before the read: a malformed claim raises below (T-5826)
        holder = read_claim(system_dir, instance, component)
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


def release(system_dir: Path, instance: str, component: str, *, expected_run_id: str) -> None:
    """Explicitly release the claim held by ``expected_run_id`` (T-5819).

    Called only by the run's own finalize path -- never automatically
    on process exit (that automaticity is exactly what the rejected
    caller-held-flock design would have given us, and exactly what C1's
    caller-exit-persistence requirement forbids).

    Compare-and-delete: deletes the canonical claim ONLY if it names
    ``expected_run_id``, so a releaser that checked the holder earlier
    can never delete a claim a different run took since.

    - claim absent -> no-op (idempotent release);
    - claim names another run -> ``ClaimHolderMismatch``, nothing deleted;
    - claim unreadable / malformed -> ``PrimitiveUnavailable``, nothing
      deleted (fail closed, C4; before T-5819 a malformed claim was
      unlinked unconditionally);
    - release lock busy past ``_RELEASE_LOCK_WAIT_S`` -> ``PrimitiveUnavailable``.

    Operator recovery for a malformed claim (C4 fail-closed is intended:
    release never guesses whose claim an unparseable file was): inspect
    the file named in the ``PrimitiveUnavailable`` reason; if it is
    confirmed stale (no live run owns the component), remove it by hand
    as its owner or root (the leaf is sticky). The next admission can
    then proceed.

    The read and the unlink both act relative to the locked leaf-directory
    fd, so the lock, the read and the delete all hit the SAME directory
    even if the gate tree is renamed or replaced mid-section (C4-1).

    Why a lock around release only is sufficient: the canonical name goes
    absent->present only through ``claim()``'s ``link()``, which fails if
    the name exists, and present->absent only here. With every removal
    serialized under the leaf-directory flock, a claim that is present
    when the holder reads it is the same claim when this unlinks it.
    ``claim()`` takes no lock, so admission semantics are unchanged. The
    flock is a microsecond critical section, NOT the claim: the claim file
    stays the mechanism-of-record and persists across process exit (C1).
    """

    leaf = _leaf_dir(system_dir, instance, component)
    canonical = _claim_path(system_dir, instance, component)
    try:
        lock_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return  # no leaf, so no claim: nothing to release
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"release lock unopenable at {leaf}: {exc}"
        ) from exc
    try:
        _lock_leaf(lock_fd, leaf)
        holder = _read_claim_at(lock_fd, canonical)  # raises PrimitiveUnavailable if malformed
        if holder is None:
            return
        _after_holder_read()
        actual = holder.get("run_id")
        if actual != expected_run_id:
            raise ClaimHolderMismatch(component, expected_run_id, actual)
        try:
            os.unlink(_CLAIM_LEAF_NAME, dir_fd=lock_fd)
        except OSError as exc:  # FileNotFoundError included: the lock forbids a concurrent removal
            raise PrimitiveUnavailable(
                PRIMITIVE_NAME, f"release failed at {canonical}: {exc}"
            ) from exc
    finally:
        os.close(lock_fd)  # closing the fd drops the flock


@contextlib.contextmanager
def claim_lock(system_dir: Path, instance: str, component: str) -> Iterator[None]:
    """Hold the component's leaf-directory flock: the ONE lock ``release`` and ``takeover`` take (T-5875).

    A caller that must make a check-and-transition mutually exclusive with a takeover
    (the engine's ``rollback``) does it inside this. Fails closed: a busy lock past
    ``_RELEASE_LOCK_WAIT_S``, or an unopenable leaf, is ``PrimitiveUnavailable``. Not reentrant
    (a second open of the leaf conflicts with the first, even in one process).
    """

    leaf = _leaf_dir(system_dir, instance, component)
    try:
        lock_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PrimitiveUnavailable(PRIMITIVE_NAME, f"claim lock unopenable at {leaf}: {exc}") from exc
    try:
        _lock_leaf(lock_fd, leaf)
        yield
    finally:
        os.close(lock_fd)  # closing the fd drops the flock


def takeover(
    system_dir: Path,
    instance: str,
    component: str,
    *,
    expected_run_id: str,
    run_id: str,
    scope: str,
    uid: int,
    verb: str,
    before_swap: Callable[[], None] | None = None,
    already_locked: bool = False,
) -> None:
    """Atomically hand the claim from ``expected_run_id`` to ``run_id`` (T-5875, ORG-080 D-Y).

    Compare-and-swap under the SAME leaf-directory flock ``release`` takes: the
    canonical claim is replaced in one ``rename`` only if it still names
    ``expected_run_id``, so it is never absent (no other admission can slip in
    between a release and a re-claim) and never swapped from under a different
    holder. ``claim()`` takes no lock, but it can only create the name when it is
    absent, and it never is during the swap, so admission semantics are unchanged.

    - claim absent or naming another run -> ``ClaimHolderMismatch``, nothing changed;
    - ``before_swap`` runs INSIDE the locked section, after the holder check and before the swap:
      the caller re-checks its own precondition there (and may write a fence). Whatever it
      raises propagates and nothing is swapped;
    - claim unreadable / malformed -> ``PrimitiveUnavailable`` (fail closed, C4);
    - lock busy past ``_RELEASE_LOCK_WAIT_S`` -> ``PrimitiveUnavailable``.

    The engine decides WHEN a takeover is admissible (a gated remove over a
    ROLLBACK_INCOMPLETE holder); this primitive only makes the swap atomic.
    """

    leaf = _leaf_dir(system_dir, instance, component)
    try:
        lock_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PrimitiveUnavailable(PRIMITIVE_NAME, f"takeover lock unopenable at {leaf}: {exc}") from exc
    tmp_name = f".claim-tmp-{uuid.uuid4().hex}"
    try:
        if not already_locked:  # the caller holds ``claim_lock`` (a second flock would block on the first)
            _lock_leaf(lock_fd, leaf)
        holder = _read_claim_at(lock_fd, leaf / _CLAIM_LEAF_NAME)
        actual = None if holder is None else holder.get("run_id")
        if actual != expected_run_id:
            raise ClaimHolderMismatch(component, expected_run_id, actual)
        if before_swap is not None:
            before_swap()
        payload = {
            "run_id": run_id,
            "instance": instance,
            "component": component,
            "scope": scope,
            "uid": uid,
            "verb": verb,
            "claimed_at": time.time(),
        }
        try:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _CLAIM_FILE_MODE, dir_fd=lock_fd)
            try:
                os.write(fd, json.dumps(payload).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_name, _CLAIM_LEAF_NAME, src_dir_fd=lock_fd, dst_dir_fd=lock_fd)
        except OSError as exc:
            try:
                os.unlink(tmp_name, dir_fd=lock_fd)
            except OSError:
                pass
            raise PrimitiveUnavailable(PRIMITIVE_NAME, f"takeover failed at {leaf}: {exc}") from exc
    finally:
        os.close(lock_fd)


def _read_claim_at(dir_fd: int, canonical: Path) -> dict[str, Any] | None:
    """Read the claim relative to the locked leaf fd (T-5819 L1).

    ``None`` if absent. ``PrimitiveUnavailable`` (fail closed, nothing
    deleted) if unreadable, a symlink (``O_NOFOLLOW``), not JSON, or JSON
    that is not an object. ``canonical`` is for messages only.
    """

    try:
        fd = os.open(_CLAIM_LEAF_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim unreadable at {canonical}: {exc}"
        ) from exc
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise PrimitiveUnavailable(
            PRIMITIVE_NAME, f"claim unreadable at {canonical}: {exc}"
        ) from exc
    finally:
        os.close(fd)
    return _parse_claim(b"".join(chunks), canonical)


#: Bound on waiting for the release critical section. The section is a
#: read + unlink; a holder past this bound is wedged or hostile, so release
#: fails closed (C4) rather than hanging.
_RELEASE_LOCK_WAIT_S = 2.0
_RELEASE_LOCK_RETRY_S = 0.01


def _lock_leaf(lock_fd: int, leaf: Path) -> None:
    deadline = time.monotonic() + _RELEASE_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise PrimitiveUnavailable(
                    PRIMITIVE_NAME,
                    f"release lock busy at {leaf} past {_RELEASE_LOCK_WAIT_S}s; nothing released",
                ) from None
            time.sleep(_RELEASE_LOCK_RETRY_S)
        except OSError as exc:
            raise PrimitiveUnavailable(
                PRIMITIVE_NAME, f"release lock failed at {leaf}: {exc}"
            ) from exc


def _after_holder_read() -> None:
    """Test injection point: runs inside the release critical section,
    after the holder read and before the compare-and-unlink. No-op in
    production."""


def _best_effort_unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
