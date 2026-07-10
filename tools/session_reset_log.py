#!/usr/bin/env python3
"""session_reset_log — structured event log for session-reset class events.

Per T-0256. Producers (ho_resumer.py, handoff_and_restart.sh, activity_monitor
P1.d) append one entry per fire to a per-cause-class jsonl. The reader
(cycle_stats.py session-resets subcommand) aggregates across cause-classes +
across accounts for stats surfaces (text table for ops, JSON for kino-pwa).

Cause-classes:
  cap_hit_auto_resume          — ho_resumer fires on 5h cap StopFailure hook.
  handoff_restart              — handoff_and_restart.sh fires (operator-initiated).
  rate_limit_retry_inject      — activity_monitor P1.d retry-inject fires.
  service_restart_unattributed — systemd start without matching producer entry
                                 (filled in by reconciliation; out of scope for v0).

Storage:
  Per-account state dir: <home>/.local/state/pantheon/session_resets/
  Per-class file:        <class>.jsonl (append-only)

Entry shape (one per line, machine-readable):
  {
    "ts":            <epoch int>,
    "ts_iso":        "YYYY-MM-DDTHH:MM:SSZ",
    "class":         "<one of CLASSES>",
    "trigger":       "<short string identifying the firing cause>",
    "agent_account": "claude" | "claude-fallback" | "claude-research",
    ...extras...
  }

CLI (for shell-script invocation, e.g. handoff_and_restart.sh):
  python3 -m tools.session_reset_log emit --class <c> --trigger <t> [--note <s>]

Environment overrides:
  PANTHEON_STATE_DIR — override state dir root (default ~/.local/state/pantheon).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLASSES = (
    "cap_hit_auto_resume",
    "handoff_restart",
    "rate_limit_retry_inject",
    "service_restart_unattributed",
)

# Schema served by --schema flag in the reader; consumed by the kino-pwa.
SCHEMA = {
    "version": 1,
    "classes": list(CLASSES),
    "entry_fields": {
        "ts": "epoch int (seconds since 1970-01-01 UTC)",
        "ts_iso": "ISO-8601 UTC timestamp with Z suffix, no fractional seconds",
        "class": "one of CLASSES",
        "trigger": "short identifier of the firing cause (free-form, producer-defined)",
        "agent_account": "linux account where the fire was recorded",
    },
    "extras_keys_observed": [
        # Documented producers' extras; not exhaustive.
        "session_id",       # ho_resumer
        "resets_at",        # ho_resumer
        "service",          # handoff_and_restart
        "target_pane",      # handoff_and_restart
        "agent",            # activity_monitor (which agent's pane was injected)
        "predicate",        # activity_monitor
        "note",             # free-form
    ],
}

# Accounts to sweep when doing a cross-account read.
KNOWN_ACCOUNTS = ("claude", "claude-fallback", "claude-research")

_PANTHEON_STATE_DIR_DEFAULT = ".local/state/pantheon"
_SESSION_RESETS_SUBDIR = "session_resets"

_SUDO_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _state_dir_root() -> Path:
    """Return the configured state-dir root for the current account."""
    override = os.environ.get("PANTHEON_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / _PANTHEON_STATE_DIR_DEFAULT


def session_resets_dir(state_dir_root: Path | None = None) -> Path:
    """Return <state_dir_root>/session_resets/ (created on demand)."""
    root = state_dir_root if state_dir_root is not None else _state_dir_root()
    return root / _SESSION_RESETS_SUBDIR


def class_log_path(cls: str, state_dir_root: Path | None = None) -> Path:
    if cls not in CLASSES:
        raise ValueError(f"unknown class: {cls!r}")
    return session_resets_dir(state_dir_root) / f"{cls}.jsonl"


def _current_account() -> str:
    # PANTHEON_ACCOUNT is an explicit override mirroring PANTHEON_STATE_DIR:
    # it lets integration tests pin the caller to a KNOWN_ACCOUNTS value so the
    # own-account read branch (which honors PANTHEON_STATE_DIR) fires regardless
    # of the host login name. On a clean CI runner getpass.getuser() is e.g.
    # "runner" — not a known account — which would otherwise route every read
    # through the cross-account sudo path and miss the tmp state dir entirely.
    override = os.environ.get("PANTHEON_ACCOUNT")
    if override:
        return override
    try:
        return getpass.getuser()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def append_event(
    cls: str,
    trigger: str,
    *,
    state_dir_root: Path | None = None,
    agent_account: str | None = None,
    ts: int | None = None,
    **extras,
) -> Path:
    """Append a structured event entry to the per-class jsonl. Returns path.

    Caller may pass arbitrary extras; they're serialized into the entry verbatim
    (provided they're JSON-serializable). Standard fields can be overridden
    (ts, agent_account) for test purposes.
    """
    if cls not in CLASSES:
        raise ValueError(f"unknown class: {cls!r}; expected one of {CLASSES}")
    epoch = int(ts) if ts is not None else int(time.time())
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "ts": epoch,
        "ts_iso": iso,
        "class": cls,
        "trigger": trigger,
        "agent_account": agent_account if agent_account is not None else _current_account(),
    }
    for k, v in extras.items():
        if v is None:
            continue
        entry[k] = v
    path = class_log_path(cls, state_dir_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------------------
# Read — same-account
# ---------------------------------------------------------------------------


def _read_jsonl_file(path: Path) -> list[dict]:
    """Read entries from a single jsonl file (skip malformed lines)."""
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


def read_events(
    *,
    state_dir_root: Path | None = None,
    classes: list[str] | None = None,
    since_epoch: int | None = None,
    until_epoch: int | None = None,
) -> list[dict]:
    """Read entries from this account's session_resets/ tree."""
    target_classes = classes if classes is not None else list(CLASSES)
    out: list[dict] = []
    for cls in target_classes:
        if cls not in CLASSES:
            continue
        for entry in _read_jsonl_file(class_log_path(cls, state_dir_root)):
            ts = entry.get("ts")
            if not isinstance(ts, int):
                continue
            if since_epoch is not None and ts < since_epoch:
                continue
            if until_epoch is not None and ts > until_epoch:
                continue
            out.append(entry)
    out.sort(key=lambda e: e.get("ts", 0))
    return out


# ---------------------------------------------------------------------------
# Read — cross-account
# ---------------------------------------------------------------------------


def _account_state_dir(account: str) -> Path:
    return Path(f"/home/{account}") / _PANTHEON_STATE_DIR_DEFAULT


def read_events_cross_account(
    *,
    accounts: list[str] | None = None,
    classes: list[str] | None = None,
    since_epoch: int | None = None,
    until_epoch: int | None = None,
) -> list[dict]:
    """Read entries from all known accounts, merging into a sorted list.

    Cross-account reads use sudo cat to bypass the 700-mode home dirs that the
    caller (typically Recovery, claude-fallback) cannot traverse directly. For the
    caller's own account, reads happen in-process (no sudo needed).

    Failures on a per-account basis are silently elided — best-effort merge.
    """
    target_accounts = accounts if accounts is not None else list(KNOWN_ACCOUNTS)
    target_classes = classes if classes is not None else list(CLASSES)
    caller = _current_account()
    out: list[dict] = []
    for acct in target_accounts:
        for cls in target_classes:
            if cls not in CLASSES:
                continue
            if acct == caller:
                # For the caller's own account, honor PANTHEON_STATE_DIR
                # (lets integration tests redirect to a tmp state dir).
                path = session_resets_dir() / f"{cls}.jsonl"
                entries = _read_jsonl_file(path)
            else:
                path = _account_state_dir(acct) / _SESSION_RESETS_SUBDIR / f"{cls}.jsonl"
                entries = _read_jsonl_via_sudo(path, acct)
            for entry in entries:
                ts = entry.get("ts")
                if not isinstance(ts, int):
                    continue
                if since_epoch is not None and ts < since_epoch:
                    continue
                if until_epoch is not None and ts > until_epoch:
                    continue
                out.append(entry)
    out.sort(key=lambda e: e.get("ts", 0))
    return out


def _read_jsonl_via_sudo(path: Path, account: str) -> list[dict]:
    """Cat the file via sudo as `account`. Returns empty list on any failure.

    Invokes `/bin/cat` directly rather than via `bash -c`. The wrapped-shell
    form (`bash -c "cat <path> 2>/dev/null || true"`) cannot be matched by a
    sudoers entry tightly: shell argument quoting + the inline 2>/dev/null
    pipeline make the full command string opaque to sudo's path-pattern check.
    Direct `/bin/cat <path>` is a stable command shape that sudoers entries
    can match per-account + per-path-pattern (T-0443, 2026-05-18).

    Missing-file (cat exits 1) and permission-denied (sudo refuses) both
    surface as non-zero returncode; the existing returncode check + return
    []` handles them uniformly. Dropping the shell-side `|| true` does not
    change behavior — the Python guard is the source of truth.
    """
    cmd = ["sudo", "-nu", account, "/bin/cat", str(path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_SUDO_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    out: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def count_by_class(entries: list[dict]) -> dict[str, int]:
    """Return {class: count} across the given entries."""
    counts: dict[str, int] = {c: 0 for c in CLASSES}
    for entry in entries:
        cls = entry.get("class")
        if cls in counts:
            counts[cls] += 1
    return counts


def count_by_class_and_trigger(entries: list[dict]) -> dict[tuple[str, str], int]:
    """Return {(class, trigger): count} — for break-down views."""
    out: dict[tuple[str, str], int] = {}
    for entry in entries:
        cls = entry.get("class")
        trig = entry.get("trigger", "")
        if cls not in CLASSES:
            continue
        key = (cls, trig)
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# CLI (emit subcommand for shell-script invocation)
# ---------------------------------------------------------------------------


def _cli_emit(args: argparse.Namespace) -> int:
    extras: dict = {}
    if args.note:
        extras["note"] = args.note
    if args.extras:
        # --extras k1=v1 k2=v2 …
        for token in args.extras:
            if "=" not in token:
                print(f"skipping malformed extras token: {token!r}", file=sys.stderr)
                continue
            k, v = token.split("=", 1)
            extras[k] = v
    path = append_event(args.cls, args.trigger, **extras)
    print(f"appended {args.cls} → {path}")
    return 0


def _cli_dump(args: argparse.Namespace) -> int:
    entries = read_events_cross_account()
    for e in entries:
        print(json.dumps(e, sort_keys=True))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="session-reset event log producer/dumper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_emit = sub.add_parser("emit", help="append one event")
    p_emit.add_argument("--class", dest="cls", choices=CLASSES, required=True)
    p_emit.add_argument("--trigger", required=True, help="short cause identifier")
    p_emit.add_argument("--note", help="optional free-form note")
    p_emit.add_argument(
        "--extras", nargs="*", default=[], help="extra key=value pairs"
    )
    p_emit.set_defaults(func=_cli_emit)
    p_dump = sub.add_parser("dump", help="dump cross-account event log to stdout")
    p_dump.set_defaults(func=_cli_dump)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
