#!/usr/bin/env python3
"""HO auto-resumer — reads the limit-hit state file and fires claude --resume.

Designed to run as a systemd user timer on the `claude` user (every 5 minutes).
On each fire it:

  1. Loads `~/.local/state/pantheon/ho_limit_hit.json` (written by the
     StopFailure hook when HO hits a Max-window cap).
  2. Bails clean if the file is missing, has a stale mtime (>24h), or has a
     resets_at that is already in the past before the 90s margin passed.
  3. Bails if `resets_at + 90s` is still in the future (not yet time — the
     next timer fire 5 minutes later will re-check).
  4. Bails if HO is already running (`pgrep -f 'claude.*--resume'` non-empty).
  5. Fires `claude --resume <session_id> -p "<locked-prompt>"`.
  6. Rotates the state file to `ho_limit_hit.json.consumed.<epoch>` so
     subsequent timer fires don't re-trigger the same resume.

Bail-and-poll design: the script always exits quickly. No long sleep inside the
process. Worst-case resume latency = one 5-minute poll interval.

Environment overrides:
  PANTHEON_STATE_DIR    — override state dir path (default ~/.local/state/pantheon)
  PANTHEON_CLAUDE_BIN   — override claude binary path (default: which-discovered
                          or ~/.npm-global/bin/claude per VPS convention)

Exit codes:
  0 — clean bail (nothing to do, or not-yet-time) OR successful resume
  1 — resume was attempted but claude --resume exited non-zero
"""

from __future__ import annotations

import json
import os
import subprocess
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
_DEFAULT_STATE_FILE_NAME = "ho_limit_hit.json"

# 90-second margin after the reset window to avoid the post-reset parallel-burst
# issue documented in the source research.
_RESUME_MARGIN_SECONDS = 90

# State file mtime ceiling: files older than 24h are considered stale.
_STALE_MTIME_SECONDS = 24 * 3600

# claude binary: PANTHEON_CLAUDE_BIN env override, then VPS convention path.
CLAUDE_BIN = os.environ.get(
    "PANTHEON_CLAUDE_BIN",
    str(Path.home() / ".npm-global" / "bin" / "claude"),
)

# Locked resumer prompt — verbatim per B8 / the operator msg_398. Do NOT edit.
RESUMER_PROMPT = (
    "Continue from where you left off. "
    "The previous halt was a Max-window cap-hit; "
    "the auto-resumer fired this session. "
    "If you were mid-dispatch when halted, "
    "evaluate whether to resume the worker or re-dispatch fresh. "
    "Confirm state via the latest ledger entry before proceeding."
)

# pgrep pattern to detect an already-running HO resume session.
_PGREP_PATTERN = "claude.*--resume"

# T-0909: one bounded in-poll retry on a non-zero resume exit before rotating
# the cap-hit state. Covers the transient claude-code auto-update window where
# the npm `claude` wrapper execs fine but its native-binary target is momentarily
# absent → "native binary not installed", non-zero exit (NOT a FileNotFoundError,
# so the launch-exception guard misses it). The sleep outlasts a typical
# seconds-long postinstall window; we still rotate after the retry, so the
# deliberate ≤1-re-fire anti-hammer guarantee holds. Chose a general retry over
# matching the stderr string for robustness — a brittle message match would
# silently rot if claude-code ever changes the wording.
RESUME_RETRY_SLEEP_SECS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Emit a structured log line to stdout (captured by systemd journal)."""
    ts = int(time.time())
    print(f"[ho_resumer ts={ts}] {msg}", flush=True)


def _resolve_state_path() -> Path:
    """Return the full path to the state file, honouring PANTHEON_STATE_DIR."""
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / _DEFAULT_STATE_FILE_NAME


def _load_state(path: Path) -> dict | None:
    """Load and JSON-parse the state file. Returns None on any failure."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        _log(f"could not parse state file: {exc}")
        return None


def _is_stale_mtime(path: Path) -> bool:
    """Return True if the state file's mtime is older than _STALE_MTIME_SECONDS."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    age = time.time() - mtime
    return age > _STALE_MTIME_SECONDS


def _ho_already_running() -> bool:
    """Return True if a `claude --resume` process is already running FOR THIS
    ACCOUNT.

    T-1956: scoped by uid. Unscoped `pgrep -f` on a box with nine agent
    accounts, each running `claude`, would match ANY sibling account's
    `claude ... --resume` — a false positive in the exact tool whose job is
    recovering the HO. This script only ever runs as the HO account (it's
    invoked by that account's own systemd user unit), so `os.getuid()` here
    IS the HO account's uid — no separate config needed. A same-account
    interactive HO session started via `claude --resume` in tmux still
    matches — that's a true positive (HO genuinely running -> correct
    decline).
    """
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-f", _PGREP_PATTERN],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _rotate_state_file(path: Path) -> Path:
    """Rename state file to .consumed.<epoch>. Returns the new path."""
    consumed = path.parent / f"{path.name}.consumed.{int(time.time())}"
    path.rename(consumed)
    return consumed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run one poll cycle. Returns 0 on clean bail or success; 1 on fire failure."""

    # Re-read PANTHEON_CLAUDE_BIN at runtime so test env overrides take effect.
    claude_bin = os.environ.get("PANTHEON_CLAUDE_BIN", CLAUDE_BIN)

    state_path = _resolve_state_path()

    # 1. Missing file → nothing to do.
    if not state_path.exists():
        _log("state file absent; nothing to do")
        return 0

    # 2. Stale mtime → old file from a prior cycle; don't retry indefinitely.
    if _is_stale_mtime(state_path):
        _log(f"state file mtime is stale (>24h); ignoring: {state_path}")
        return 0

    # 3. Load and validate.
    state = _load_state(state_path)
    if state is None:
        _log(f"state file unreadable or malformed; bailing: {state_path}")
        return 0

    session_id = state.get("session_id", "")
    resets_at = state.get("resets_at")

    if not session_id:
        _log("state file missing session_id; bailing")
        return 0

    if not isinstance(resets_at, (int, float)):
        _log(f"state file has non-numeric resets_at ({resets_at!r}); bailing")
        return 0

    resets_at = int(resets_at)
    now = int(time.time())
    fire_at = resets_at + _RESUME_MARGIN_SECONDS

    # 4. resets_at is in the past but fire_at (resets_at + 90s) hasn't arrived yet
    #    — edge: if resets_at itself is in the past but within the margin, we still
    #    wait for the full margin to pass.
    if fire_at > now:
        _log(
            f"not yet time: fire_at={fire_at} now={now} "
            f"({fire_at - now}s remaining); next poll will retry"
        )
        return 0

    # 5. Check if resets_at is so far in the past it's clearly a stale cap-hit
    #    (mtime check handles most staleness; this catches the edge where mtime
    #    was touched recently but resets_at is ancient).
    if resets_at < now - _STALE_MTIME_SECONDS:
        _log(
            f"resets_at={resets_at} is >24h in the past; stale cap-hit; bailing"
        )
        return 0

    # 6. Pre-fire health check: HO already running?
    if _ho_already_running():
        _log("HO resume already running (pgrep match); skipping double-start")
        return 0

    # 7. Fire.
    _log(
        f"firing: claude --resume {session_id} (resets_at={resets_at}, "
        f"fire_at={fire_at}, now={now})"
    )
    # T-0256: append session-reset stats entry BEFORE subprocess.run so the
    # attempt is recorded even if claude --resume blocks/exits early. Failure
    # of the append itself must not block the resume — best-effort.
    try:
        from tools.session_reset_log import append_event as _sr_append
        _sr_append(
            "cap_hit_auto_resume",
            trigger="ho_limit_hit_state_consumed",
            session_id=session_id,
            resets_at=resets_at,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort logging side-effect
        _log(f"session_reset_log append failed (non-fatal): {exc}")
    cmd = [claude_bin, "--resume", session_id, "-p", RESUMER_PROMPT]

    def _fire_resume():
        """Run `claude --resume` once. Returns the CompletedProcess, or None if
        the binary is genuinely missing/unlaunchable (the missing-PATH case —
        distinct from the transient binary-absent-at-runtime case the retry
        below handles). Caller rotates + bails on None."""
        try:
            return subprocess.run(
                cmd,
                capture_output=False,   # let stdout/stderr flow to journal
                check=False,
            )
        except FileNotFoundError:
            _log(f"claude binary not found: {claude_bin}")
            return None
        except OSError as exc:
            _log(f"failed to launch claude: {exc}")
            return None

    result = _fire_resume()
    if result is None:
        # Missing/unlaunchable binary path — rotate so we don't hammer every 5 min.
        _rotate_state_file(state_path)
        return 1

    # T-0909: a non-zero exit can be the transient claude-code auto-update window
    # (the npm wrapper execs, but its native-binary target is momentarily absent
    # → non-zero exit) — which the FileNotFoundError guard above does NOT catch,
    # so the state would be consumed below with no next-poll retry, leaving HO
    # unresumed until a human notices. Retry ONCE in-poll after a short sleep
    # (outlasts a typical postinstall window), then fall through to the
    # unconditional rotate — so re-fire is still capped at 1 (anti-hammer).
    #
    # Retry on ANY non-zero (not just a stderr "native binary not installed"
    # match) for robustness — a brittle string match would silently rot if the
    # wording changes. The cost is one wasted retry + sleep on a genuinely
    # permanent error (e.g. a dead session); accepted as low-frequency + harmless.
    #
    # The _ho_already_running() guard skips the retry if a `claude --resume` is
    # CONCURRENTLY live (e.g. an overlapping manual/poll resume) — don't pile a
    # second resume on top. Note: under `-p` print mode the first attempt has
    # already exited by here, so this guard does NOT detect "attempt 1 succeeded"
    # — that case is already handled by the `returncode == 0` short-circuit above.
    if result.returncode != 0 and not _ho_already_running():
        _log(
            f"resume exited {result.returncode} and HO not running; one bounded "
            f"retry in {RESUME_RETRY_SLEEP_SECS}s (covers transient auto-update "
            f"binary-absent window, T-0909)"
        )
        time.sleep(RESUME_RETRY_SLEEP_SECS)
        retry = _fire_resume()
        if retry is None:
            _rotate_state_file(state_path)
            return 1
        result = retry

    # 8. Rotate regardless of claude's exit code — the resume was attempted;
    #    don't re-fire the same session on next poll.
    consumed = _rotate_state_file(state_path)
    _log(f"state file rotated → {consumed.name}")

    if result.returncode != 0:
        _log(f"claude --resume exited {result.returncode}; see journal for output")
        return 1

    _log(f"resume fired successfully (session={session_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
