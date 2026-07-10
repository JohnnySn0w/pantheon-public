#!/usr/bin/env python3
"""session_reset_reconcile — daily cron that lands session-reset jsonl entries
into structured ledger session_reset action entries.

Per T-0257. The deferred half of T-0256: producers (ho_resumer,
handoff_and_restart, activity_monitor P1.d) cannot commit at fire-time because
they run pre-restart, mid-restart, or outside HO's git context. They write
fire-time records to <state>/session_resets/<class>.jsonl. THIS reconciler
runs daily, sweeps unconsumed entries, emits structured ledger session_reset
entries, and commits.

Also: journal-sweep for the 4th class (service_restart_unattributed) — scans
claude-telegram.service Started events past N days; entries with no matching
producer (cap_hit_auto_resume / handoff_restart) within ±10s get logged to
service_restart_unattributed.jsonl with trigger=journal_sweep_unmatched.

Cadence: daily at 00:30 UTC (after grooming at 00:00). systemd-user timer on
Recovery's account.

Watermark: per-class last-consumed ts at <state>/session_resets/.reconcile_watermark.json.
Survives crash/restart — re-running the same window is idempotent.

Exit codes:
  0 — clean run (with or without new entries).
  1 — fetch/merge failure OR ledger write OR commit failure.
  2 — argv error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools._git_utils import _git, _hermes_scan_file, _hermes_scan_message, pull_repo_or_warn
from tools._audit import append_ledger_entries
from tools._audit import build_ledger_entry as _audit_build_entry
from tools.ledger_shard import current_write_target
from tools.session_reset_log import (
    CLASSES,
    KNOWN_ACCOUNTS,
    _PANTHEON_STATE_DIR_DEFAULT,
    _SESSION_RESETS_SUBDIR,
    append_event,
    read_events_cross_account,
    session_resets_dir,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WATERMARK_NAME = ".reconcile_watermark.json"

_LEDGER_DIR = _REPO_ROOT / "ledger"

# Journal sweep — window past N days for unattributed-restart detection.
_DEFAULT_JOURNAL_LOOKBACK_DAYS = 1

# Time tolerance for matching a journal Start event to a producer entry.
_PRODUCER_MATCH_TOLERANCE_SECONDS = 10

# Which producers can attribute a service Started event.
_PRODUCER_CLASSES_FOR_ATTRIBUTION = ("cap_hit_auto_resume", "handoff_restart")

# Journal target (the claude bot's user service — same target as health probes).
_JOURNAL_USER_UNIT = "claude-telegram.service"
_JOURNAL_SUDO_USER = "claude"

# Subprocess timeouts.
_JOURNAL_TIMEOUT_SECONDS = 60
_GIT_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    ts = int(time.time())
    print(f"[session_reset_reconcile ts={ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


def _watermark_path() -> Path:
    """Watermark lives in the caller's own state dir (Recovery, for the cron)."""
    return session_resets_dir() / _WATERMARK_NAME


def load_watermark(path: Path | None = None) -> dict[str, int]:
    """Load per-class last-consumed ts. Missing/malformed → all-zero."""
    p = path if path is not None else _watermark_path()
    base = {c: 0 for c in CLASSES}
    if not p.exists():
        return base
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return base
    last = data.get("last_consumed_ts_by_class")
    if not isinstance(last, dict):
        return base
    for c in CLASSES:
        v = last.get(c)
        if isinstance(v, int):
            base[c] = v
    return base


def save_watermark(wm: dict[str, int], path: Path | None = None) -> None:
    p = path if path is not None else _watermark_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_consumed_ts_by_class": {c: int(wm.get(c, 0)) for c in CLASSES},
        "last_run_ts": int(time.time()),
    }
    p.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Ledger entry construction
# ---------------------------------------------------------------------------


def _entry_id_for(entry: dict) -> str:
    """Deterministic ledger entry id derived from the source entry.

    Format: <ts_iso>-session-reset-<class>-<short-hash>. The short-hash is the
    first 6 chars of sha1(class + ts + trigger + agent_account) — gives us
    collision-resistant uniqueness across the per-day batch.
    """
    cls = entry.get("class", "unknown")
    ts_iso = entry.get("ts_iso", "")
    trigger = entry.get("trigger", "")
    acct = entry.get("agent_account", "")
    h = hashlib.sha1(f"{cls}|{ts_iso}|{trigger}|{acct}".encode("utf-8")).hexdigest()[:6]
    return f"{ts_iso}-session-reset-{cls.replace('_', '-')}-{h}"


def _rationale_for(entry: dict) -> str:
    """Short structured rationale string."""
    parts = [
        f"session reset: class={entry.get('class', 'unknown')}",
        f"trigger={entry.get('trigger', 'unknown')}",
        f"agent_account={entry.get('agent_account', 'unknown')}",
    ]
    # Append known extras inline if present.
    for k in ("session_id", "resets_at", "service", "target_pane", "agent", "predicate", "note", "mode"):
        if k in entry and entry[k] is not None and k not in {"class", "trigger", "agent_account", "ts", "ts_iso"}:
            parts.append(f"{k}={entry[k]}")
    return "; ".join(parts) + "."


def build_ledger_entry(src: dict) -> dict:
    """Build a structured ledger entry from a session_reset_log source entry.

    T-1088: the canonical 10-field ORG-001 assembly lives in `_audit`; this
    function stays as the session-reset-specific projection (id/links shape).
    """
    return _audit_build_entry(
        entry_id=_entry_id_for(src),
        ts=src.get("ts_iso", ""),
        agent="recovery",
        action="session_reset",
        rationale=_rationale_for(src),
        justifies_via=["D1"],
        links=[
            {
                "type": "session_reset_log_entry",
                "ref": f"{src.get('class', 'unknown')}:{src.get('ts_iso', '')}",
            }
        ],
    )


# ---------------------------------------------------------------------------
# Ledger append
# ---------------------------------------------------------------------------


def _current_year_shard(ledger_dir: Path = _LEDGER_DIR) -> Path:
    # T-0148 Phase 2: resolve the write target dynamically — the legacy
    # ledger/2026.jsonl while it exists (behavior-preserving), else the
    # current ISO-week shard ledger/<seg>/<seg>.jsonl.
    return current_write_target(ledger_dir, datetime.now(timezone.utc))


def append_to_ledger(entries: list[dict], shard: Path | None = None, ledger_dir: Path = _LEDGER_DIR) -> Path:
    """Append a batch of structured entries to the current write target shard.

    The returned path (legacy file pre-migration, ISO-week shard post-) flows
    into commit_and_push's modified_paths, so the same-commit staging is on the
    dynamic path automatically.
    """
    # T-1088: the resolve→ensure→append kernel lives in the shared _audit
    # helper (sort_keys serialization preserved; returns the written path).
    return append_ledger_entries(entries, ledger_dir=ledger_dir, shard=shard)


# ---------------------------------------------------------------------------
# Journal sweep
# ---------------------------------------------------------------------------


def _normalize_iso(ts: str) -> str:
    """Make an ISO-8601 timestamp parseable by ``datetime.fromisoformat`` on the
    documented >=3.10 bring-up baseline (T-1383 / ORG-A4).

    Python 3.10's ``fromisoformat`` rejects a trailing ``Z`` and a compact
    ``+HHMM`` offset; 3.11+ accept both. journalctl ``short-iso`` emits the
    compact form (e.g. ``2026-05-11T13:30:00+0000``). Normalize ``Z`` → ``+00:00``
    and insert the offset colon so the parse is portable. Idempotent on the
    already-extended forms.
    """
    ts = ts.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", ts):  # compact +HHMM / -HHMM → +HH:MM
        ts = ts[:-2] + ":" + ts[-2:]
    return ts


def journal_started_events(
    *,
    since_days: int = _DEFAULT_JOURNAL_LOOKBACK_DAYS,
    sudo_user: str = _JOURNAL_SUDO_USER,
    user_unit: str = _JOURNAL_USER_UNIT,
) -> list[int]:
    """Return epoch timestamps of `Started <unit>` events past N days.

    journalctl is run as the unit's owning user via sudo + XDG_RUNTIME_DIR.
    Best-effort: any failure returns [].
    """
    cmd = [
        "sudo", "-nu", sudo_user,
        "bash", "-c",
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) journalctl --user-unit {user_unit} "
        f"--since '{since_days} days ago' --no-pager -o short-iso 2>/dev/null | grep -F Started || true",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_JOURNAL_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    out: list[int] = []
    for line in result.stdout.splitlines():
        # Format: "2026-05-11T13:30:00+0000 host systemd[NNN]: Started ..."
        token = line.split(None, 1)
        if not token:
            continue
        ts_str = token[0]
        try:
            dt = datetime.fromisoformat(_normalize_iso(ts_str))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append(int(dt.timestamp()))
    return out


def find_unattributed_restarts(
    started_ts_list: list[int],
    producer_entries: list[dict],
    tolerance: int = _PRODUCER_MATCH_TOLERANCE_SECONDS,
) -> list[int]:
    """Return epoch ts of Started events with no matching producer entry."""
    producer_ts = sorted(
        e["ts"] for e in producer_entries
        if isinstance(e.get("ts"), int) and e.get("class") in _PRODUCER_CLASSES_FOR_ATTRIBUTION
    )
    unattributed: list[int] = []
    for start_ts in started_ts_list:
        if not _has_match_within(start_ts, producer_ts, tolerance):
            unattributed.append(start_ts)
    return unattributed


def _has_match_within(target: int, sorted_list: list[int], tolerance: int) -> bool:
    """Linear scan — list is typically small (≤30/day)."""
    for ts in sorted_list:
        if abs(ts - target) <= tolerance:
            return True
    return False


# ---------------------------------------------------------------------------
# Commit + push
# ---------------------------------------------------------------------------


def commit_and_push(modified_paths: list[Path], message: str) -> tuple[bool, str]:
    """HERMES-scan the modified paths + commit message, then commit + push.

    Returns (ok, error_detail). On HERMES refusal, returns False with the
    refused-file detail; on git failure, returns False with stderr last-line.
    """
    for p in modified_paths:
        ok, detail = _hermes_scan_file(p)
        if not ok:
            return False, f"HERMES guard refused: {detail}"
    if not _hermes_scan_message(message):
        return False, "HERMES guard refused: commit message"
    # Stage.
    for p in modified_paths:
        rel = str(p.relative_to(_REPO_ROOT))
        r = _git("add", rel)
        if r.returncode != 0:
            return False, f"git add {rel} failed: {r.stderr.strip()}"
    # Commit.
    r = _git("commit", "-m", message)
    if r.returncode != 0:
        # Could be "nothing to commit" — treat as no-op success.
        if "nothing to commit" in r.stdout.lower() or "nothing to commit" in r.stderr.lower():
            return True, "nothing to commit"
        return False, f"git commit failed: {r.stderr.strip()}"
    # Push.
    r = _git("push", "origin", "main")
    if r.returncode != 0:
        return False, f"git push failed: {r.stderr.strip().splitlines()[-1] if r.stderr else 'unknown'}"
    return True, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily session-reset reconciliation cron. "
                    "Default-safe contract (T-0259): must pass --commit-prod, --dry-run, or --no-commit explicitly."
    )
    parser.add_argument(
        "--commit-prod", action="store_true",
        help="commit + push to production ledger. systemd cron sets this; smoke runs MUST NOT.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run sweep + emit logic, but skip ledger append + commit + push",
    )
    parser.add_argument(
        "--journal-lookback-days", type=int, default=_DEFAULT_JOURNAL_LOOKBACK_DAYS,
        help=f"journal sweep lookback window in days (default {_DEFAULT_JOURNAL_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--no-journal-sweep", action="store_true",
        help="skip the journal-sweep step (useful for unit testing)",
    )
    parser.add_argument(
        "--ledger-dir", type=Path, default=_LEDGER_DIR,
        help="ledger directory (default: <repo>/ledger). Override for smoke runs.",
    )
    parser.add_argument(
        "--no-commit", action="store_true",
        help="append to ledger + bump watermark, but skip commit + push. Pairs with --ledger-dir for smoke.",
    )
    return parser.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse to run if neither a prod nor a safe mode is explicitly chosen.

    Default-safe contract (T-0259): avoid silent prod-target. Caller must opt in
    via --commit-prod, OR opt out via --dry-run / --no-commit / --ledger-dir
    pointing at a tmp shard.

    Returns exit code (64) if the invocation should be rejected; None otherwise.
    """
    # Safe-mode-explicit indicators.
    safe_explicit = (
        args.dry_run
        or args.no_commit
        or (args.ledger_dir != _LEDGER_DIR)
    )
    if args.commit_prod and safe_explicit:
        # Ambiguous — refuse rather than guess.
        print(
            "ERROR: --commit-prod conflicts with safe-mode flag (--dry-run / --no-commit / --ledger-dir override).",
            file=sys.stderr,
        )
        return 64
    if not args.commit_prod and not safe_explicit:
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --commit-prod         commit + push to production (systemd cron path)\n"
            "  --dry-run             scan + report only; no side effects\n"
            "  --no-commit           append to ledger but skip commit/push (smoke path)\n"
            "  --ledger-dir <path>   redirect to a tmp ledger dir (smoke path)\n"
            "Per T-0259 default-safe contract.",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Default-safe gate per T-0259.
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    _log(f"start (commit_prod={args.commit_prod} dry_run={args.dry_run} no_commit={args.no_commit})")

    # 1. Sync repo first so the ledger shard we append to is current.
    if not args.dry_run:
        pull_repo_or_warn("session_reset_reconcile")

    # 2. Journal sweep for service_restart_unattributed (before reading producers
    #    so the synthesized entries get reconciled in the same pass).
    if not args.no_journal_sweep:
        producer_entries = read_events_cross_account(
            classes=list(_PRODUCER_CLASSES_FOR_ATTRIBUTION),
            since_epoch=int(time.time()) - args.journal_lookback_days * 86400,
        )
        started = journal_started_events(since_days=args.journal_lookback_days)
        unattributed = find_unattributed_restarts(started, producer_entries)
        if unattributed:
            _log(f"journal sweep: {len(unattributed)} unattributed Started events")
            for ts in unattributed:
                # De-dup against existing synthesized entries: check if we
                # already logged this ts.
                already = read_events_cross_account(
                    classes=["service_restart_unattributed"],
                    since_epoch=ts - _PRODUCER_MATCH_TOLERANCE_SECONDS,
                    until_epoch=ts + _PRODUCER_MATCH_TOLERANCE_SECONDS,
                )
                if any(e.get("ts") == ts for e in already):
                    continue
                if not args.dry_run:
                    append_event(
                        "service_restart_unattributed",
                        trigger="journal_sweep_unmatched",
                        ts=ts,
                        agent_account="recovery",
                        source_unit=_JOURNAL_USER_UNIT,
                    )
        else:
            _log("journal sweep: no unattributed Started events")

    # 3. Read all producer + synthesized entries newer than the watermark.
    wm = load_watermark()
    # Most-permissive since: 0 (sweep entire corpus), then per-class filter.
    all_entries = read_events_cross_account(since_epoch=0)
    unconsumed: list[dict] = []
    for entry in all_entries:
        cls = entry.get("class")
        ts = entry.get("ts")
        if cls not in CLASSES or not isinstance(ts, int):
            continue
        if ts <= wm.get(cls, 0):
            continue
        unconsumed.append(entry)

    if not unconsumed:
        _log("no new entries to reconcile; clean bail")
        return 0

    _log(f"{len(unconsumed)} new entries to reconcile")

    # 4. Build ledger entries.
    ledger_entries = [build_ledger_entry(e) for e in unconsumed]

    # 5. HERMES belt on the rationale strings (cheap defensive check before
    #    appending). The guard runs again at file-tier post-append, but failing
    #    early here saves a half-written shard.
    for le in ledger_entries:
        if not _hermes_scan_message(le["rationale"]):
            _log(f"HERMES belt refused rationale for entry id={le['id']}; bailing")
            return 1

    if args.dry_run:
        _log("dry-run: would append:")
        for le in ledger_entries:
            print(json.dumps(le, sort_keys=True))
        return 0

    # 6. Append to ledger.
    shard = append_to_ledger(ledger_entries, ledger_dir=args.ledger_dir)

    # 7. Bump watermark.
    new_wm = dict(wm)
    for e in unconsumed:
        cls = e["class"]
        ts = e["ts"]
        if ts > new_wm.get(cls, 0):
            new_wm[cls] = ts
    save_watermark(new_wm)

    # 8. Commit + push (skip if --no-commit, useful for smoke).
    if args.no_commit:
        _log(f"appended {len(ledger_entries)} entries to {shard}; skip commit per --no-commit")
        return 0

    msg_lines = [
        f"ledger: reconcile {len(ledger_entries)} session_reset entries",
        "",
        "Synced from per-class session_resets jsonl into structured ledger entries",
        "with action=session_reset. Per T-0257.",
        "",
        "Class counts:",
    ]
    counts_by_class: dict[str, int] = {}
    for le in ledger_entries:
        cls_human = le["rationale"].split("class=", 1)[1].split(";", 1)[0].strip()
        counts_by_class[cls_human] = counts_by_class.get(cls_human, 0) + 1
    for cls, n in sorted(counts_by_class.items()):
        msg_lines.append(f"  {cls}: {n}")
    msg = "\n".join(msg_lines) + "\n"

    ok, detail = commit_and_push([shard], msg)
    if not ok:
        _log(f"commit/push failed: {detail}")
        return 1
    if detail == "nothing to commit":
        _log("nothing to commit (shard unchanged?); clean")
        return 0
    _log(f"reconciled {len(ledger_entries)} entries, committed + pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
