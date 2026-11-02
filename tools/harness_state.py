"""Durable-state schema helpers for the T-2953 harness controller (T-2967 BT4).

Layout + schemas are per notes/2026-08-18-t2953-phase1-spec.md §Durable-state
layout — this module does NOT invent shapes; it implements exactly what's
documented there:

    ~/.local/state/pantheon/personas/<persona>/
    |-- active-runtime.json          # cache/shortcut, NOT authoritative
    |-- runtimes/<harness>--<profile>/last-attempt.json
    |-- overrides/holds.json
    `-- invocations/<yyyy-mm-dd>.jsonl   # controller-owned; this module does
                                          # not write it (BT4 is CLI-side only)

Deliberately independent of tools/runtime_controller.py: BT1 (T-2966) is
rewriting that module on a parallel branch, and coordination for T-2967 is at
the file-schema level, not the import level (see T-2967 ticket "Coordination
with BT1"). This module reads/writes the same JSON files by path + schema,
never by importing runtime_controller internals.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

SCHEMA_ACTIVE_RUNTIME = "pantheon-persona-active-runtime/v1"
SCHEMA_LAST_ATTEMPT = "pantheon-persona-runtime-last-attempt/v1"
SCHEMA_HOLDS = "pantheon-persona-runtime-holds/v1"
SCHEMA_OPERATOR_RESTART = "pantheon-operator-restart/v1"
SCHEMA_SEAT_STATE = "pantheon-seat-state/v1"  # ORG-106 L0 (T-5523)

# Persona names are directory-safe lowercase identifiers (orchestrator, specialist, recovery, ...).
PERSONA_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Runtime names are always <harness>/<profile> — exactly one slash, each side a
# directory-safe lowercase-leading identifier. This is the form used inside
# JSON payloads (holds.json keys, last-attempt.json "runtime" field, ledger
# links); the double-dash SLUG form below is only used for the on-disk
# runtimes/<slug>/ directory segment, since a literal "/" can't be a path
# component.
RUNTIME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$")


class HarnessStateError(RuntimeError):
    """Raised for invalid persona/runtime input or malformed on-disk state."""


def validate_persona(persona: str) -> str:
    if not isinstance(persona, str) or not PERSONA_RE.fullmatch(persona):
        raise HarnessStateError(f"invalid persona name: {persona!r}")
    return persona


def validate_runtime(runtime: str) -> str:
    if not isinstance(runtime, str) or not RUNTIME_RE.fullmatch(runtime):
        raise HarnessStateError(
            f"invalid runtime name: {runtime!r} (expected exactly '<harness>/<profile>')"
        )
    return runtime


def runtime_slug(runtime: str) -> str:
    """`<harness>/<profile>` -> `<harness>--<profile>` (double-dash survives the slash)."""
    validate_runtime(runtime)
    harness, profile = runtime.split("/", 1)
    return f"{harness}--{profile}"


def persona_root(persona: str, *, root: Path) -> Path:
    validate_persona(persona)
    return root / "personas" / persona


def active_runtime_path(persona: str, *, root: Path) -> Path:
    return persona_root(persona, root=root) / "active-runtime.json"


def runtimes_dir(persona: str, *, root: Path) -> Path:
    return persona_root(persona, root=root) / "runtimes"


def last_attempt_path(persona: str, runtime: str, *, root: Path) -> Path:
    slug = runtime_slug(runtime)
    return runtimes_dir(persona, root=root) / slug / "last-attempt.json"


def holds_path(persona: str, *, root: Path) -> Path:
    return persona_root(persona, root=root) / "overrides" / "holds.json"


def seat_state_path(persona: str, *, root: Path) -> Path:
    """ORG-106 L0 (T-5523): the seat-state record, a sibling of holds.json. Readers go
    through tools/seat_state.effective(); this module only owns the path + raw load."""
    return persona_root(persona, root=root) / "overrides" / "seat-state.json"


def load_seat_state(persona: str, *, root: Path) -> dict[str, Any] | None:
    """Raw seat-state record, or None when absent. Raises HarnessStateError on a
    corrupt file (the fail-open handling lives in seat_state.effective)."""
    return _read_json_or_none(seat_state_path(persona, root=root))


def seat_state_tombstones_path(persona: str, *, root: Path) -> Path:
    """ORG-106 L1 (T-5524): ended seat-state records, one JSON line each."""
    return persona_root(persona, root=root) / "overrides" / "seat-state.tombstones.jsonl"


def operator_restart_path(persona: str, *, root: Path) -> Path:
    """T-5483: durable marker an operator `harness restart` drops before the kill.
    The runtime controller (a separate process that may re-exec) consumes it so the
    deliberate kill is not recorded as a runtime failure. Same file-schema-level
    contract as the other state files: runtime_controller keeps its own path copy."""
    return persona_root(persona, root=root) / "overrides" / "operator-restart.json"


def write_operator_restart_marker(
    persona: str, *, root: Path, operator: str, runtime: str, tmux_session: str,
    pid: int | None, ts: str, correlation_id: str,
) -> None:
    _atomic_json(
        operator_restart_path(persona, root=root),
        {
            "schema": SCHEMA_OPERATOR_RESTART,
            "operator": operator,
            "ts": ts,
            "runtime": runtime,
            "tmux_session": tmux_session,
            "pid": pid,
            "correlation_id": correlation_id,
        },
    )


SCHEMA_SLEEP_MARKER = "pantheon-seat-sleep-marker/v1"  # ORG-106 L3b (T-5526)


def sleep_marker_path(persona: str, *, root: Path) -> Path:
    """ORG-106 L3b: durable marker the deep-sleep drain drops before it ends the session,
    so the controller's death classification does not record the end as process_death.
    Sibling of operator-restart.json; runtime_controller keeps its own path copy (pinned
    by a parity test)."""
    return persona_root(persona, root=root) / "overrides" / "sleep-marker.json"


def write_sleep_marker(
    persona: str, *, root: Path, runtime: str, drain_started_ts: str, ts: str,
    correlation_id: str,
) -> None:
    _atomic_json(
        sleep_marker_path(persona, root=root),
        {
            "schema": SCHEMA_SLEEP_MARKER,
            "ts": ts,
            "runtime": runtime,
            "drain_started_ts": drain_started_ts,
            "correlation_id": correlation_id,
        },
    )


def load_sleep_marker(persona: str, *, root: Path) -> dict[str, Any] | None:
    return _read_json_or_none(sleep_marker_path(persona, root=root))


def remove_sleep_marker(persona: str, *, root: Path) -> None:
    try:
        sleep_marker_path(persona, root=root).unlink()
    except FileNotFoundError:
        pass


def load_operator_restart_marker(persona: str, *, root: Path) -> dict[str, Any] | None:
    return _read_json_or_none(operator_restart_path(persona, root=root))


def remove_operator_restart_marker(persona: str, *, root: Path) -> None:
    try:
        operator_restart_path(persona, root=root).unlink()
    except FileNotFoundError:
        pass


def _atomic_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Write `payload` to `path` atomically (temp file + fsync + os.replace).

    Mirrors the established repo pattern (tools/claude_quota_probe.py
    `_atomic_json`) rather than importing it — that helper is module-private
    (leading underscore) in its own file, and tools/runtime_controller.py
    keeps an independent private copy too; a third private copy here is
    convention-consistent, not novel duplication (see DECISIONS).
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise HarnessStateError(f"refusing to write through a symlink: {path}")
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessStateError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HarnessStateError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessStateError(f"expected a JSON object in {path}, got {type(data).__name__}")
    return data


def load_active_runtime(persona: str, *, root: Path) -> dict[str, Any] | None:
    """Read the active-runtime.json cache. NOT authoritative — tmux+pid is."""
    return _read_json_or_none(active_runtime_path(persona, root=root))


def load_last_attempt(persona: str, runtime: str, *, root: Path) -> dict[str, Any] | None:
    return _read_json_or_none(last_attempt_path(persona, runtime, root=root))


def write_last_attempt(persona: str, runtime: str, doc: Mapping[str, Any], *, root: Path) -> None:
    _atomic_json(last_attempt_path(persona, runtime, root=root), doc)


def load_holds(persona: str, *, root: Path) -> dict[str, Any]:
    """Read overrides/holds.json, defaulting to the empty-holds shape if absent."""
    data = _read_json_or_none(holds_path(persona, root=root))
    if data is None:
        return {"schema": SCHEMA_HOLDS, "holds": {}}
    data.setdefault("holds", {})
    return data


def write_holds(persona: str, doc: Mapping[str, Any], *, root: Path) -> None:
    _atomic_json(holds_path(persona, root=root), doc)


def write_seat_state(persona: str, doc: Mapping[str, Any], *, root: Path) -> None:
    """ORG-106 L1 (T-5524): atomically write the seat-state record (0600). The record
    is validated first; an invalid one is refused and never written."""
    from tools import seat_state  # lazy: seat_state imports this module

    problems = seat_state.validate_record(dict(doc))
    if problems:
        raise HarnessStateError("refusing to write an invalid seat-state record: " + "; ".join(problems))
    _atomic_json(seat_state_path(persona, root=root), doc)


def clear_seat_state(persona: str, *, root: Path) -> bool:
    """Remove the seat-state record. True iff one existed."""
    path = seat_state_path(persona, root=root)
    if path.is_symlink():
        raise HarnessStateError(f"refusing to remove a symlink: {path}")
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


class SeatStateBusy(HarnessStateError):
    """The per-seat seat-state lock could not be taken in time."""


def seat_state_lock_path(persona: str, *, root: Path) -> Path:
    return seat_state_path(persona, root=root).with_name("seat-state.lock")


@contextlib.contextmanager
def seat_state_lock(persona: str, *, root: Path, blocking: bool = True,
                    timeout_s: float = 30.0) -> Iterator[None]:
    """ORG-106 L5 (T-5529): the one per-seat lock over seat-state read-modify-write.

    Shared by `harness pause`/`wake` (blocking, bounded) and the L5 janitor
    (non-blocking). It is a separate file, not a lock on the record, because
    `os.replace` swaps the record's inode. Raises SeatStateBusy if not taken:
    immediately when blocking=False, after timeout_s otherwise."""
    path = seat_state_lock_path(persona, root=root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise HarnessStateError(f"refusing to lock through a symlink: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (timeout_s if blocking else 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SeatStateBusy(f"seat-state busy, retry ({persona}; lock {path})") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


def append_seat_tombstone(persona: str, row: Mapping[str, Any], *, root: Path) -> None:
    """Append one ended-record line to seat-state.tombstones.jsonl (0600, flock-serialised)."""
    path = seat_state_tombstones_path(persona, root=root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise HarnessStateError(f"refusing to write through a symlink: {path}")
    line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def list_runtime_records(persona: str, *, root: Path) -> dict[str, dict[str, Any]]:
    """Discover every runtime with an existing last-attempt.json on disk.

    Returns {runtime (slash form): last-attempt doc}. Deliberately does NOT
    cross-reference instance.yaml's runtime_binding manifest — that key does
    not exist in instance.yaml yet (BT1/BT2-forward work); disk truth is what
    `status` can honestly report today (see DECISIONS).
    """
    base = runtimes_dir(persona, root=root)
    result: dict[str, dict[str, Any]] = {}
    if not base.is_dir():
        return result
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        record_path = child / "last-attempt.json"
        data = _read_json_or_none(record_path)
        if data is None:
            continue
        runtime = data.get("runtime")
        if isinstance(runtime, str) and runtime:
            result[runtime] = data
        else:
            # Malformed/missing "runtime" field — reconstruct from the slug
            # dirname rather than dropping the record silently.
            result[child.name.replace("--", "/", 1)] = data
    return result


def list_usability(persona: str, *, root: Path) -> dict[str, dict[str, Any]]:
    """T-4862: per-runtime live-usability verdicts (usability.json), keyed by runtime ref.
    Read-only; malformed files are skipped, never raised."""
    base = runtimes_dir(persona, root=root)
    result: dict[str, dict[str, Any]] = {}
    if not base.is_dir():
        return result
    for child in sorted(base.iterdir()):
        try:
            data = _read_json_or_none(child / "usability.json")
        except HarnessStateError:
            continue
        if data is None:
            continue
        runtime = data.get("runtime")
        result[runtime if isinstance(runtime, str) and runtime else child.name.replace("--", "/", 1)] = data
    return result


def set_hold(
    persona: str,
    runtime: str,
    *,
    reason: str,
    operator: str,
    root: Path,
    now_ts: str,
) -> dict[str, Any]:
    """Return the updated holds doc with `runtime` held. Caller writes it."""
    validate_runtime(runtime)
    doc = load_holds(persona, root=root)
    doc["schema"] = SCHEMA_HOLDS
    doc.setdefault("holds", {})
    doc["holds"][runtime] = {
        "set_ts": now_ts,
        "set_by": f"operator:{operator}",
        "reason": reason,
    }
    return doc


def clear_hold(persona: str, runtime: str, *, root: Path) -> tuple[dict[str, Any], bool]:
    """Return (updated holds doc, whether `runtime` was actually held)."""
    validate_runtime(runtime)
    doc = load_holds(persona, root=root)
    existed = runtime in doc.get("holds", {})
    if existed:
        del doc["holds"][runtime]
    return doc, existed


#: Failure classes with no cooldown timer: they block until `clear-cooldown`.
UNTIMED_BLOCKING_FAILURE_CLASSES = frozenset({"runtime_blocked", "authentication_loss"})


def clear_cooldown(
    persona: str,
    runtime: str,
    *,
    operator: str,
    now_ts: str,
    root: Path,
) -> tuple[dict[str, Any], bool]:
    """Return (updated last-attempt doc, whether it previously had an active cooldown).

    Nulls `cooldown_until` and re-stamps provenance to manual-override.
    Deliberately leaves `consecutive_failures` untouched — the operator is
    clearing the cooldown TIMER, not erasing failure history (see DECISIONS).
    T-5028 (d): a `runtime_blocked` record has no timer (it blocks until
    cleared), so clearing also resets its `failure_class` to null and counts
    as having had a cooldown. T-5791: `authentication_loss` is the same shape
    (no timer, blocks until cleared) and is cleared the same way -- this is the
    revocation DR rollback.
    If no record exists yet, a fresh placeholder is created (there was
    nothing to clear, but the CLI still needs a `runtime` field to write).
    """
    validate_runtime(runtime)
    existing = load_last_attempt(persona, runtime, root=root)
    blocked = bool(existing) and existing.get("failure_class") in UNTIMED_BLOCKING_FAILURE_CLASSES
    had_cooldown = bool(existing and existing.get("cooldown_until")) or blocked
    if existing is None:
        doc: dict[str, Any] = {
            "schema": SCHEMA_LAST_ATTEMPT,
            "runtime": runtime,
            "last_attempt_ts": None,
            "outcome": None,
            "failure_class": None,
            "cooldown_until": None,
            "consecutive_failures": 0,
            "provenance": {"trigger": "manual-override", "operator": operator},
        }
    else:
        doc = dict(existing)
        doc["cooldown_until"] = None
        if blocked:
            doc["failure_class"] = None
        doc["provenance"] = {"trigger": "manual-override", "operator": operator}
    return doc, had_cooldown


def state_root_from_env(default: Path | None = None) -> Path:
    """Resolve the state root (the parent of ``personas/``).

    T-5028 (b): ONE resolution with the runtime planes, so a non-default env can
    never split the holds path between this CLI and the planes that read it. The
    planes resolve the personas dir as PANTHEON_PERSONA_STATE_DIR, else
    ``$PANTHEON_STATE_DIR/personas``, else ``~/.local/state/pantheon/personas``
    (runtime_controller._state_root, session_chronicle.persona_state_root,
    proactive_swap_init._default_state_root); this returns that dir's parent.

    Raises HarnessStateError when PANTHEON_PERSONA_STATE_DIR is not named
    ``personas`` (this CLI's spool lives beside it, at ``<root>/harness-overrides``)
    or when both variables are set and disagree: refusing beats writing a hold
    the planes will not read. `default` is used only when neither is set."""
    persona_env = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    state_env = os.environ.get("PANTHEON_STATE_DIR")
    if persona_env:
        personas = Path(os.path.abspath(persona_env))
        if personas.name != "personas":
            raise HarnessStateError(
                f"PANTHEON_PERSONA_STATE_DIR={persona_env!r} is not a 'personas' directory; "
                "the harness CLI cannot place its state beside it"
            )
        if state_env and Path(os.path.abspath(state_env)) != personas.parent:
            raise HarnessStateError(
                f"PANTHEON_PERSONA_STATE_DIR={persona_env!r} and PANTHEON_STATE_DIR={state_env!r} "
                "disagree (expected PANTHEON_PERSONA_STATE_DIR = PANTHEON_STATE_DIR/personas); "
                "refusing so a hold cannot land where the runtime planes do not read"
            )
        return personas.parent
    if state_env:
        return Path(state_env)
    if default is not None:
        return default
    return Path.home() / ".local" / "state" / "pantheon"


#: T-5028 (f+): an override evidence row older than this with no matching
#: outcome row is flagged (the SIGKILL-mid-swap residual from the MR-2 review).
OVERRIDE_OUTCOME_GRACE_SECONDS = 900
OVERRIDE_EVENT = "operator_swap_hold_overridden"
OVERRIDE_OUTCOME_EVENT = "operator_swap_hold_override_outcome"


def _json_lines(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def open_quarantine_rows(lines: Iterable[str]) -> list[dict[str, Any]]:
    """T-5028 (f): quarantine.jsonl rows not yet resolved. A row is
    ``{"entry", "reason", "quarantined_at"}``; a later tombstone
    ``{"tombstone": <entry id>, "action": "landed"|"dropped", ...}`` resolves
    every row whose entry has that id. Unparseable lines are skipped. Pure, so
    fleet_health can reuse it on a cross-account read."""
    rows: list[dict[str, Any]] = []
    resolved: set[str] = set()
    for value in _json_lines(lines):
        if isinstance(value.get("tombstone"), str):
            resolved.add(value["tombstone"])
        elif isinstance(value.get("entry"), dict):
            rows.append(value)
    return [row for row in rows if str(row["entry"].get("id")) not in resolved]


def orphan_override_rows(
    lines: Iterable[str], *, now: datetime, grace_seconds: float = OVERRIDE_OUTCOME_GRACE_SECONDS
) -> list[dict[str, Any]]:
    """T-5028 (f+): override evidence rows with no outcome row for the same
    correlation_id after `grace_seconds`. Pure (lines of controller-evidence.jsonl)."""
    overrides: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    for value in _json_lines(lines):
        correlation = value.get("correlation_id")
        if not isinstance(correlation, str) or not correlation:
            continue
        if value.get("event") == OVERRIDE_EVENT:
            overrides[correlation] = value
        elif value.get("event") == OVERRIDE_OUTCOME_EVENT:
            finished.add(correlation)
    orphans = []
    for correlation, row in overrides.items():
        if correlation in finished:
            continue
        try:
            at = datetime.fromisoformat(str(row.get("recorded_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - at).total_seconds() >= grace_seconds:
            orphans.append(row)
    return orphans


def root_for_personas_dir(personas_dir: Path) -> Path:
    """The harness state root for a runtime plane's personas dir (T-5028 (a)):
    its parent, which must be named ``personas``."""
    personas = Path(os.path.abspath(personas_dir))
    if personas.name != "personas":
        raise HarnessStateError(f"{personas_dir} is not a 'personas' directory")
    return personas.parent
