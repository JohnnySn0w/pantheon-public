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

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA_ACTIVE_RUNTIME = "pantheon-persona-active-runtime/v1"
SCHEMA_LAST_ATTEMPT = "pantheon-persona-runtime-last-attempt/v1"
SCHEMA_HOLDS = "pantheon-persona-runtime-holds/v1"

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
    If no record exists yet, a fresh placeholder is created (there was
    nothing to clear, but the CLI still needs a `runtime` field to write).
    """
    validate_runtime(runtime)
    existing = load_last_attempt(persona, runtime, root=root)
    had_cooldown = bool(existing and existing.get("cooldown_until"))
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
        doc["provenance"] = {"trigger": "manual-override", "operator": operator}
    return doc, had_cooldown


def state_root_from_env(default: Path | None = None) -> Path:
    """Resolve the state root, honouring PANTHEON_STATE_DIR (matches
    tools/restart_request.py / tools/restart_drain.py convention)."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    if env:
        return Path(env)
    if default is not None:
        return default
    return Path.home() / ".local" / "state" / "pantheon"
