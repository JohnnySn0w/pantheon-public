#!/usr/bin/env python3
"""context_fill_hold — explicit, TTL-bounded hold on context_fill_monitor's
ABSOLUTE-THRESHOLD restart (T-2615).

WHY THIS EXISTS. context_fill_monitor.py's absolute-threshold fire path
(reason="context_fill_threshold") has no pane/liveness check at all -- it
fires purely on fill_pct >= threshold once cooldown clears. That is CORRECT
for a normal healthy-but-full session (the whole point of the feature is to
preserve state before the hard ceiling). It is WRONG for a session an
operator (or the agent itself) is DELIBERATELY holding past the restart
point -- and "deliberate" is actor intent, which cannot be read off a pane.
T-2615's investigation ruled out both candidate passive signals (pane-text
"manual mode on" is an omnipresent default-permission-mode label, uncorrelated
with intent -- see bugs/telegram-inbound-injects-input-but-turn-doesnt-fire.md;
mere pane-advancing liveness can't discriminate a held session from a normal
one either, since both are alive). So a hold must be ASSERTED, not inferred.

SHAPE: a small per-agent JSON state file, bounded by a TTL so an assertion
can't silently mask remediation forever (see context_fill_monitor.py's call
site for the full rationale). Two properties, both load-bearing:
  - context_fill_monitor consults this ONLY in the absolute-threshold path,
    never in the wedge/staleness path -- a hold must never be able to mask a
    real wedge.
  - The default/max TTLs are hours, not minutes, because a deliberate hold is
    a human-timescale decision, not an in-flight automated step.

Usage:
    python3 tools/context_fill_hold.py --agent <orchestrator|specialist|recovery> [--hours N] [--reason TEXT] [--set-by WHO]
    python3 tools/context_fill_hold.py --agent <agent> --status
    python3 tools/context_fill_hold.py --agent <agent> --clear

A lapsed hold letting the absolute-threshold restart re-engage is CORRECT
behavior, not a regression -- re-run this before the TTL expires to extend.

Environment overrides:
    PANTHEON_STATE_DIR  — override state dir (default ~/.local/state/pantheon).

Exit codes:
    0 — set / cleared / status printed (status is informational regardless of
        whether a hold is active).
    2 — argv error (unknown agent, non-positive --hours).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Independent per-agent list, deliberately not imported from
# context_fill_monitor.AGENT_CONFIG (context_fill_monitor imports THIS
# module; importing back would risk a cycle). Mirrors restart_request.py's
# same loose-coupling choice.
KNOWN_AGENTS = ("orchestrator", "specialist", "recovery")

HOLD_TTL_DEFAULT_SECONDS = 2 * 60 * 60  # 2h — T-2615 design decision #3
HOLD_TTL_MAX_SECONDS = 8 * 60 * 60      # 8h hard cap — re-assertion required past this


# --------------------------------------------------------------------- state
def _state_dir() -> Path:
    """Mirror context_fill_monitor's state-dir resolution so the hold guard
    lives alongside the rest of the monitor's per-agent state (and so tests
    can redirect via PANTHEON_STATE_DIR)."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "pantheon"


def hold_path_for_agent(agent: str) -> Path:
    return _state_dir() / f"context_fill_{agent}_hold.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------- library
def set_hold(
    agent: str,
    *,
    hours: float | None = None,
    reason: str | None = None,
    set_by: str | None = None,
    now: float | None = None,
) -> dict:
    """Write (or refresh) the hold for `agent`. `hours` defaults to
    HOLD_TTL_DEFAULT_SECONDS and is clamped to HOLD_TTL_MAX_SECONDS -- a
    request above the cap is honoured up to the cap, never rejected outright,
    so a well-meaning long ask degrades to "re-assert sooner" rather than
    "nothing happened". Returns the written record (ttl_seconds reflects any
    clamp, so callers can tell the operator).

    Raises ValueError if hours <= 0 -- a non-positive duration is not a
    request for anything, it is malformed input.
    """
    now = time.time() if now is None else now
    if hours is None:
        hours = HOLD_TTL_DEFAULT_SECONDS / 3600.0
    if hours <= 0:
        raise ValueError(f"hours must be > 0 (got {hours})")
    ttl_seconds = min(int(round(hours * 3600)), HOLD_TTL_MAX_SECONDS)
    data = {
        "agent": agent,
        "ts": int(now),
        "ttl_seconds": ttl_seconds,
        "reason": reason,
        "set_by": set_by,
    }
    _write_json(hold_path_for_agent(agent), data)
    return data


def clear_hold(agent: str) -> bool:
    """Remove the hold for `agent`. Returns True iff one was present."""
    path = hold_path_for_agent(agent)
    existed = path.exists()
    try:
        path.unlink()
    except OSError:
        pass  # already gone — harmless, a concurrent clear raced us here
    return existed


def hold_status(agent: str, *, now: float | None = None) -> dict:
    """Tri-outcome status read, fail-safe toward "not active" on any doubt
    -- a monitor guard must never silently hold on ambiguous state:
      - no file / malformed / missing fields -> {"active": False}
      - present but past its ttl              -> {"active": False, "expired": True, ...}
      - present and fresh                     -> {"active": True, "remaining_seconds": ..., ...}
    """
    now = time.time() if now is None else now
    data = _read_json(hold_path_for_agent(agent))
    ts = data.get("ts")
    ttl = data.get("ttl_seconds")
    if not isinstance(ts, (int, float)) or not isinstance(ttl, (int, float)):
        return {"active": False}
    expires_at = ts + ttl
    remaining = expires_at - now
    if remaining <= 0:
        return {"active": False, "expired": True, "ts": ts, "ttl_seconds": ttl, "expires_at": expires_at}
    return {
        "active": True,
        "ts": ts,
        "ttl_seconds": ttl,
        "expires_at": expires_at,
        "remaining_seconds": remaining,
        "reason": data.get("reason"),
        "set_by": data.get("set_by"),
    }


def hold_active(agent: str, *, now: float | None = None) -> bool:
    """Convenience boolean wrapper around hold_status — the predicate
    context_fill_monitor's fire-path gate actually consults."""
    return bool(hold_status(agent, now=now).get("active"))


# ----------------------------------------------------------------------- CLI
def _log(msg: str) -> None:
    ts = int(time.time())
    print(f"[context_fill_hold ts={ts}] {msg}", flush=True)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set/clear/inspect a TTL-bounded hold on an agent's "
                     "context-fill absolute-threshold restart (T-2615)."
    )
    parser.add_argument(
        "--agent", choices=KNOWN_AGENTS, required=True,
        help="Target agent. One of: " + ", ".join(KNOWN_AGENTS),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Print the current hold status and exit.")
    mode.add_argument("--clear", action="store_true", help="Clear an existing hold and exit.")
    parser.add_argument(
        "--hours", type=float, default=None,
        help=f"Hold duration in hours (default {HOLD_TTL_DEFAULT_SECONDS / 3600:g}h, "
             f"hard-capped at {HOLD_TTL_MAX_SECONDS / 3600:g}h — a lapsed hold letting the "
             f"safety restart re-engage is correct behavior, not a bug; re-run to extend).",
    )
    parser.add_argument("--reason", default=None, help="Free-text reason, surfaced in the monitor log.")
    parser.add_argument("--set-by", default=None, help="Who/what asserted the hold, surfaced in the monitor log.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    agent = args.agent

    if args.status:
        status = hold_status(agent)
        if status.get("active"):
            _log(
                f"{agent}: HOLD ACTIVE — reason={status.get('reason') or 'none'} "
                f"set_by={status.get('set_by') or 'unknown'} "
                f"expires in {int(status['remaining_seconds'])}s"
            )
        else:
            _log(f"{agent}: no active hold")
        return 0

    if args.clear:
        existed = clear_hold(agent)
        _log(f"{agent}: hold cleared" if existed else f"{agent}: no hold was set")
        return 0

    if args.hours is not None and args.hours <= 0:
        _log(f"refusing: --hours must be > 0 (got {args.hours})")
        return 2

    requested_hours = args.hours if args.hours is not None else HOLD_TTL_DEFAULT_SECONDS / 3600.0
    data = set_hold(agent, hours=requested_hours, reason=args.reason, set_by=args.set_by)
    clamp_note = ""
    if requested_hours * 3600 > HOLD_TTL_MAX_SECONDS:
        clamp_note = f" (clamped from requested {requested_hours:g}h to the {HOLD_TTL_MAX_SECONDS // 3600}h hard cap)"
    _log(
        f"{agent}: HOLD set for {data['ttl_seconds'] // 3600}h{clamp_note} — suppresses the "
        f"context-fill ABSOLUTE-THRESHOLD restart only; wedge/staleness recovery is NOT "
        f"affected. Re-run before it lapses to extend."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
