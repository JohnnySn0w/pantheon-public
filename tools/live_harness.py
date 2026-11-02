"""Resolve a seat's LIVE harness for pane injection (T-4792).

Pane injectors (inbox_consumer, mailbox_wake) used a static per-agent harness,
so a Claude-configured seat that the runtime controller had failed over to codex
got the claude-code submit-verify profile. That profile's "[Pasted text" sentinel
is always absent on codex's TUI, so it false-passes: the paste sits unsubmitted in
the composer (the T-1106 class). The operator DMs to specialist waited ~1h on 2026-10-13.

The controller records the active runtime in
``<state_root>/<agent>/runtime-controller/runtime.json`` (schema
pantheon-runtime-state/v1, key ``harness``: "claude" | "codex" | "hermes"). This module maps
that to a send_keys_submit profile, falling back to the caller's static default on
any missing, malformed or unknown state. It never raises.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# runtime.json harness value -> send_keys_submit.sh / submit_verify.py profile
_PROFILE = {"claude": "claude-code", "codex": "codex", "hermes": "hermes"}  # hermes: T-5011 PR-B


def default_state_root(home: str | Path | None = None) -> Path:
    """``<home>/.local/state/pantheon/personas`` (home defaults to the caller's)."""
    base = Path(home) if home is not None else Path.home()
    return base / ".local" / "state" / "pantheon" / "personas"


def profile_for(harness: object) -> str | None:
    """The submit profile for a runtime harness name ("claude" | "codex" | "hermes"), else None."""
    return _PROFILE.get(harness) if isinstance(harness, str) else None


def runtime_profile(state: object) -> str | None:
    """The submit profile recorded in a parsed runtime.json, or None when the record is
    not a pantheon-runtime-state/v1 dict or names no known harness. Pure (T-5793)."""
    if not isinstance(state, dict) or state.get("schema") != "pantheon-runtime-state/v1":
        return None
    return profile_for(state.get("harness"))


def runtime_started_at(state: object) -> float | None:
    """Epoch seconds of a parsed runtime.json's ``started_at`` (ISO-8601, tz-aware), or None."""
    raw = state.get("started_at") if isinstance(state, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def resolve(agent: str, default: str, *, state_root: Path | None = None) -> str:
    """The submit-verify profile for ``agent``'s live runtime, else ``default``."""
    root = state_root if state_root is not None else default_state_root()
    try:
        state = json.loads((root / agent / "runtime-controller" / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return runtime_profile(state) or default
