#!/usr/bin/env python3
"""Pantheon restart request — operator-facing CLI to trigger a graceful agent restart.

Writes a tombstone at ~/.local/state/pantheon/restart_requested_<agent>.json
with reason='operator'. The restart_drain.py timer (running on the target agent's
account) picks up the tombstone on its next cycle (~30s) and fires
handoff_and_restart.sh.

Usage:
    python3 tools/restart_request.py --agent <recovery|orchestrator|specialist> [--force]

Options:
    --agent AGENT       Target agent to restart (required). One of: recovery, orchestrator, specialist.
    --handoff-file PATH Override the HANDOFF.md path to check freshness against
                        (default: agent's canonical HANDOFF.md path from AGENT_CONFIG).
    --force             Skip HANDOFF.md freshness + existence check. Use when HANDOFF.md
                        is absent or stale but you want the restart anyway.

The HANDOFF.md freshness check:
    By default, refuses to write the tombstone if the agent's HANDOFF.md is:
      - Missing (doesn't exist) — suggests the agent may not be in a clean state
        to hand off from.
      - Stale (mtime > HANDOFF_MAX_AGE_SECONDS = 10 min) — same signal.
    Use --force to bypass both checks (e.g., for a cold restart after an unclean halt).

Tombstone shape:
    {
        "requested_at": <unix epoch int>,
        "agent": "<agent name>",
        "reason": "operator"
    }

The tombstone is read by restart_drain.py running on the agent's own account.
Operator tombstones bypass the freshness gate in the drain (only context_fill_threshold
tombstones are subject to a 5-min staleness check). Justifies-via: D7 (operator trust
differs structurally from monitor trust).

Environment overrides:
    PANTHEON_STATE_DIR  — override state dir (default ~/.local/state/pantheon).

Exit codes:
    0 — tombstone written successfully.
    1 — refused (HANDOFF.md missing or stale; use --force to override).
    2 — argv error (unknown agent, missing --agent).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# Staleness threshold for HANDOFF.md freshness gate (operator path).
# If HANDOFF.md mtime is older than this, the request is refused unless --force.
HANDOFF_MAX_AGE_SECONDS = 10 * 60  # 10 minutes

# ---------------------------------------------------------------------------
# Per-agent configuration (operator path)
# ---------------------------------------------------------------------------
#
# Default HANDOFF.md paths per agent. The operator can override via --handoff-file.

AGENT_CONFIG: dict[str, dict] = {
    "recovery": {
        "handoff_file": "/home/claude-fallback/HANDOFF.md",
    },
    "orchestrator": {
        "handoff_file": "/home/claude/HANDOFF.md",
    },
    "specialist": {
        "handoff_file": "/home/claude-research/HANDOFF.md",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Emit a structured log line to stdout."""
    ts = int(time.time())
    print(f"[restart_request ts={ts}] {msg}", flush=True)


def _state_dir() -> Path:
    """Return the state dir, honouring PANTHEON_STATE_DIR env override."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else _DEFAULT_STATE_DIR


def _handoff_is_fresh(handoff_path: Path) -> tuple[bool, str]:
    """Check whether HANDOFF.md exists and is recent enough.

    Returns (ok, reason). ok=True means the check passed (proceed).
    reason is a human-readable explanation if ok=False.
    """
    if not handoff_path.exists():
        return False, f"HANDOFF.md not found at {handoff_path}"
    try:
        mtime = handoff_path.stat().st_mtime
    except OSError as exc:
        return False, f"could not stat {handoff_path}: {exc}"
    age = time.time() - mtime
    if age > HANDOFF_MAX_AGE_SECONDS:
        return False, (
            f"HANDOFF.md is stale ({int(age)}s old; max={HANDOFF_MAX_AGE_SECONDS}s); "
            f"pass --force to override"
        )
    return True, ""


def _write_tombstone(state_dir: Path, agent: str) -> Path:
    """Write the operator tombstone and return its path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"restart_requested_{agent}.json"
    data = {
        "requested_at": int(time.time()),
        "agent": agent,
        "reason": "operator",
    }
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Request a graceful restart for a Pantheon agent via tombstone."
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_CONFIG.keys()),
        required=True,
        help="Target agent to restart. One of: " + ", ".join(sorted(AGENT_CONFIG.keys())),
    )
    parser.add_argument(
        "--handoff-file",
        default=None,
        help="Override the HANDOFF.md path to check. Defaults to the agent's canonical path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip HANDOFF.md freshness check. Use for cold restarts or when HANDOFF.md is absent.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Write the restart tombstone. Returns 0 on success, 1 on refusal, 2 on argv error."""

    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    agent = args.agent
    cfg = AGENT_CONFIG[agent]

    handoff_path = Path(args.handoff_file) if args.handoff_file else Path(cfg["handoff_file"])

    if not args.force:
        ok, reason = _handoff_is_fresh(handoff_path)
        if not ok:
            _log(f"refusing restart request for {agent}: {reason}")
            return 1

    state_dir = _state_dir()
    tombstone = _write_tombstone(state_dir, agent)
    _log(
        f"tombstone written for {agent}: {tombstone} "
        f"(drain timer will fire within ~30s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
