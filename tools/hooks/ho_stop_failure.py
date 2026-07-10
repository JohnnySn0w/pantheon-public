#!/usr/bin/env python3
"""StopFailure hook handler — writes limit-hit state file for auto-resumer.

Claude Code fires this hook when a session ends due to an API error.
This script:
  1. Reads the hook event JSON from stdin.
  2. Filters for error_type == "rate_limit" (ignores all other error types).
  3. Parses resets_at from the human-readable error_message (timezone-aware,
     DST-safe). Falls back to written_at + 5h if parsing fails.
  4. Writes a JSON state file at $PANTHEON_STATE_DIR/ho_limit_hit.json
     (default: ~/.local/state/pantheon/ho_limit_hit.json).

State file shape (read by T-0045 resumer):
  {
    "error_type": "rate_limit",
    "error_message": "<full text from hook event>",
    "transcript_path": "<path>",
    "session_id": "<id>",
    "resets_at": <Unix epoch seconds, int>,
    "written_at": <Unix epoch seconds, int>
  }

Environment:
  PANTHEON_STATE_DIR           — override default state dir (tests use tempdir)
  PANTHEON_WRITTEN_AT_OVERRIDE — override written_at epoch (tests use fixed anchor)
  PANTHEON_TMP_MIRROR_OVERRIDE — override /tmp mirror path (tests redirect to tempfile)

Exit codes:
  0 — always (hook must not block Claude Code; errors are logged to stderr)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_FILE_NAME = "ho_limit_hit.json"
_FALLBACK_OFFSET_SECONDS = 5 * 3600  # 5-hour window default

# /tmp mirror path — cross-user signal for Specialist/claude-research (T-0072).
# Overridable via PANTHEON_TMP_MIRROR_OVERRIDE for tests (subprocess-level tests
# cannot use mock.patch; env-var seam mirrors the PANTHEON_STATE_DIR pattern).
_TMP_MIRROR_PATH = Path("/tmp/pantheon_ho_limit_hit.json")

# Day-of-week abbreviations used in error messages
_DOW_MAP = {
    "sun": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
}

# Matches patterns like:
#   "resets 3:45pm"
#   "resets 12:00am"
#   "resets Mon 12:00am"
#   "resets 3pm"           (no minutes)
#   "resets Mon 3pm"
#
# Groups: (dow_abbr_or_None, hour, minutes_or_None, ampm)
_RESETS_RE = re.compile(
    r"resets\s+(?:(mon|tue|wed|thu|fri|sat|sun)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# resets_at parser
# ---------------------------------------------------------------------------


def _parse_resets_at(error_message: str, written_at: int) -> int:
    """Extract reset epoch from a human-readable error message.

    Handles:
      - "resets H:MMpm" / "resets H:MMam"
      - "resets Mon H:MMam" (day-of-week prefix)
      - Past-time detection: if parsed local time is <= written_at, advance 24h
        (or to the next occurrence of the named weekday).

    Falls back to written_at + 5h on any parse failure.
    """
    m = _RESETS_RE.search(error_message)
    if not m:
        return written_at + _FALLBACK_OFFSET_SECONDS

    dow_str, hour_str, min_str, ampm = m.group(1), m.group(2), m.group(3), m.group(4)

    hour = int(hour_str)
    minute = int(min_str) if min_str else 0
    ampm = ampm.lower()

    # Convert to 24-hour
    if ampm == "am":
        if hour == 12:
            hour = 0
    else:  # pm
        if hour != 12:
            hour += 12

    # Resolve the local timezone from the running OS environment.
    # datetime.now().astimezone().tzinfo gives us the OS local tz without
    # requiring the TZ env var to be set — robust on servers where TZ may
    # be absent or set to UTC.
    local_tz = datetime.now().astimezone().tzinfo

    # Build candidate datetime: today's date in local time at the parsed H:MM
    written_dt_local = datetime.fromtimestamp(written_at, tz=local_tz)
    candidate = written_dt_local.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )

    if dow_str:
        # Advance to the next occurrence of the named weekday.
        target_dow = _DOW_MAP[dow_str.lower()]
        current_dow = candidate.weekday()  # Monday=0, Sunday=6
        days_ahead = (target_dow - current_dow) % 7
        if days_ahead == 0:
            # Same weekday: if time is in the past, go to next week
            if candidate.timestamp() <= written_at:
                days_ahead = 7
        candidate = candidate + timedelta(days=days_ahead)
    else:
        # No weekday: if the time is in the past, advance by 24h
        if candidate.timestamp() <= written_at:
            candidate = candidate + timedelta(days=1)

    return int(candidate.timestamp())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # Read and parse hook event from stdin; errors are non-fatal (exit 0).
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ho_stop_failure: could not parse hook event JSON: {exc}", file=sys.stderr)
        return

    if not isinstance(event, dict):
        print("ho_stop_failure: hook event is not a JSON object; ignoring", file=sys.stderr)
        return

    error_type = event.get("error_type")
    if error_type != "rate_limit":
        # Not a rate-limit halt; nothing to do.
        return

    # Written-at timestamp (may be overridden for deterministic tests).
    _override = os.environ.get("PANTHEON_WRITTEN_AT_OVERRIDE")
    written_at: int = int(_override) if _override else int(time.time())

    error_message = event.get("error_message", "")
    session_id = event.get("session_id", "")
    transcript_path = event.get("transcript_path", "")

    resets_at = _parse_resets_at(error_message, written_at)

    state: dict = {
        "error_type": error_type,
        "error_message": error_message,
        "transcript_path": transcript_path,
        "session_id": session_id,
        "resets_at": resets_at,
        "written_at": written_at,
    }

    # Resolve state dir; honour env override (tests point at a tempdir).
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / _STATE_FILE_NAME
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"ho_stop_failure: failed to write state file: {exc}", file=sys.stderr)
        return

    # Mirror to /tmp for cross-user signal (Specialist/claude-research, T-0072).
    # Failure here must not block canonical write or the exit-0 contract.
    # Path is overridable for tests via PANTHEON_TMP_MIRROR_OVERRIDE.
    _mirror_override = os.environ.get("PANTHEON_TMP_MIRROR_OVERRIDE")
    mirror_path = Path(_mirror_override) if _mirror_override else _TMP_MIRROR_PATH
    try:
        mirror_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        mirror_path.chmod(0o644)
    except (OSError, PermissionError) as exc:
        print(
            f"ho_stop_failure: mirror write to {mirror_path} failed (non-fatal): {exc}",
            file=sys.stderr,
        )

    print(
        f"ho_stop_failure: rate_limit halt written → {state_path} "
        f"(resets_at={resets_at}, session={session_id})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — last-resort catch; hook must not crash
        print(f"ho_stop_failure: unexpected error: {exc}", file=sys.stderr)
    sys.exit(0)
