#!/usr/bin/env python3
"""continuity_counter.py — Pantheon system-continuity day-count tool.

Emits the current Pantheon continuity streak: how many consecutive days the
system has been continuously operational since the most recent break (or since
genesis if no breaks have occurred).

Continuity definition (T-0435 + T-0445):
  - Measures SYSTEM-LEVEL state continuity, not process uptime.
  - Graceful HO handoff via handoff_and_restart.sh and cap-hit auto-resume
    do NOT count as breaks — the architecture is designed for state-handoff
    across restarts; counting restarts would mismeasure the system.
  - Only a full Pantheon outage (multi-day lost state, ledger discontinuity,
    repo loss) breaks continuity.

Genesis: pantheon-config's first commit date (2026-04-28), read from git log.
Justifies via P4 (traceability) — git first-commit is canonical + verifiable.

Break-tracking: knowledge/state/continuity_breaks.jsonl
  Append-only JSONL; each line is a JSON object: {start_iso, end_iso, cause}.
  Missing file treated as empty (no breaks). Malformed lines silently skipped
  (ledger-style robustness — partial corruption must not block the tool).
  Overlapping breaks are not guarded; that's an operator data error; tool picks
  the break with the latest end_iso as the current streak-start.

Day-counting: floor((now_epoch - streak_start_epoch) / 86400)
  Integer-day grain matches public-claim semantics ("X days continuous") and
  avoids partial-day noise.  Justifies via P5 (caller-determined contract).

Usage:
  continuity_counter.py [--format text|json|jsonl]

Exit codes:
  0 — successful run.
  1 — genesis resolution failed (git log subprocess error).
  2 — argv error.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# (sys.path[0]=tools/) or `python3 -m tools.foo` (sys.path[0]=repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BREAKS_PATH = REPO_ROOT / "knowledge" / "state" / "continuity_breaks.jsonl"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _get_genesis_iso(repo_root: Path) -> str:
    """Return the ISO 8601 timestamp of the first commit in the repo.

    Runs: git -C <repo_root> log --reverse --format=%cI
    Takes the first output line (oldest commit).
    Raises subprocess.CalledProcessError on git failure.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "log", "--reverse", "--format=%cI"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    if not lines:
        raise ValueError("git log returned no commits — empty repo?")
    return lines[0].strip()


def _load_breaks(breaks_path: Path) -> list[dict]:
    """Read continuity_breaks.jsonl; return list of decoded dicts.

    - Missing file → [] (semantically identical to no breaks; initial state).
    - Malformed JSONL line → silently skipped (ledger-style robustness).
    """
    if not breaks_path.exists():
        return []
    entries: list[dict] = []
    for line in breaks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            # Silently skip malformed lines — per ledger-style robustness.
            pass
    return entries


# ---------------------------------------------------------------------------
# Pure computation core (no I/O — testable with synthetic inputs)
# ---------------------------------------------------------------------------

def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO 8601 string (with offset) to a UTC Unix epoch float."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _epoch_to_iso(epoch: float) -> str:
    """Format a Unix epoch float as ISO 8601 UTC string (Z suffix)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _compute(now_ts: float, genesis_iso: str, breaks: list[dict]) -> dict:
    """Compute the full continuity output dict.

    Parameters
    ----------
    now_ts:
        Current time as Unix epoch float.
    genesis_iso:
        ISO 8601 string of the first commit timestamp.
    breaks:
        Decoded list from continuity_breaks.jsonl (may be empty).

    Returns a dict with keys:
        genesis_iso, now_iso, current_streak_start_iso,
        current_streak_days, total_days_since_genesis, breaks, schema_version.

    Design notes:
    - Streak start = latest break end_iso across all breaks (or genesis if none).
    - If the latest break's end_iso is in the future relative to now_ts,
      streak_days is clamped to 0 — a streak cannot be negative.
    - A wholly-future break (start_iso > now_ts) is treated the same as any
      other: if its end_iso is the latest, it becomes streak start, and the
      resulting negative raw_streak_days is clamped to 0. This correctly
      signals "break ongoing / not yet resolved" with zero current streak.
    - Overlapping breaks: not guarded; last end_iso wins (operator controls file).
    - Day-counting: floor((now_ts - streak_start_epoch) / 86400) per P5.
    """
    genesis_epoch = _iso_to_epoch(genesis_iso)
    total_days = int(math.floor((now_ts - genesis_epoch) / 86400))

    # Find streak start: latest break end_iso, or genesis if no breaks.
    streak_start_epoch = genesis_epoch
    streak_start_iso_override: str | None = None

    if breaks:
        # Find the break with the maximum end_iso epoch.
        latest_break_epoch: float = float("-inf")
        latest_break_iso: str = ""
        for b in breaks:
            try:
                end_epoch = _iso_to_epoch(b["end_iso"])
            except (KeyError, ValueError):
                continue
            if end_epoch > latest_break_epoch:
                latest_break_epoch = end_epoch
                latest_break_iso = b["end_iso"]
        if latest_break_iso:
            streak_start_epoch = latest_break_epoch
            streak_start_iso_override = latest_break_iso

    # Compute streak days; clamp to 0 if break end is in the future.
    raw_streak_days = (now_ts - streak_start_epoch) / 86400
    current_streak_days = max(0, int(math.floor(raw_streak_days)))

    # Canonical ISO representations for output (always UTC Z).
    genesis_out = _epoch_to_iso(genesis_epoch)
    now_out = _epoch_to_iso(now_ts)
    streak_start_out = _epoch_to_iso(streak_start_epoch)

    return {
        "genesis_iso": genesis_out,
        "now_iso": now_out,
        "current_streak_start_iso": streak_start_out,
        "current_streak_days": current_streak_days,
        "total_days_since_genesis": total_days,
        "breaks": breaks,
        "schema_version": 1,
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_json(data: dict) -> str:
    """Return pretty-printed JSON string (indent=2) with trailing newline."""
    return json.dumps(data, indent=2) + "\n"


def _format_jsonl(data: dict) -> str:
    """Return single-line JSON string with trailing newline."""
    return json.dumps(data) + "\n"


def _format_text(data: dict) -> str:
    """Return human-readable summary lines with trailing newline."""
    lines = [
        f"continuity: {data['current_streak_days']} days current streak",
        f"  genesis:      {data['genesis_iso']}",
        f"  streak start: {data['current_streak_start_iso']}",
        f"  now:          {data['now_iso']}",
        f"  total days since genesis: {data['total_days_since_genesis']}",
        f"  break events: {len(data['breaks'])}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="continuity_counter.py",
        description="Emit Pantheon continuity streak count.",
    )
    p.add_argument(
        "--format",
        dest="fmt",
        choices=("text", "json", "jsonl"),
        default="text",
        help="output format (text=human summary, json=pretty JSON, jsonl=single-line JSON)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Resolve genesis from git log.
    try:
        genesis_iso = _get_genesis_iso(REPO_ROOT)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: failed to resolve genesis from git log: {exc}", file=sys.stderr)
        return 1

    breaks = _load_breaks(BREAKS_PATH)
    now_ts = time.time()
    data = _compute(now_ts, genesis_iso, breaks)

    if args.fmt == "json":
        sys.stdout.write(_format_json(data))
    elif args.fmt == "jsonl":
        sys.stdout.write(_format_jsonl(data))
    else:
        sys.stdout.write(_format_text(data))

    return 0


if __name__ == "__main__":
    sys.exit(main())
