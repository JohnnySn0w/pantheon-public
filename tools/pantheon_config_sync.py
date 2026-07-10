#!/usr/bin/env python3
"""pantheon-config bidirectional sync — T-0211 Phase B.

Replaces T-0208's pantheon-config-pull (pull-only) with a single timer that
handles BOTH push and pull symmetrically. Single timer keeps the sync
semantics atomic; two timers (push + pull) opens race windows on the same
cadence cycle.

Per-fire logic:
  1. git fetch origin.
  2. Determine local-ahead / remote-ahead state.
  3. Both ahead   -> DIVERGENT (log + future: surface to Recovery alert plumbing).
  4. Local ahead  -> git push origin main.
  5. Remote ahead -> git pull --ff-only origin main.
  6. Both clean   -> no-op.

Alerting: journalctl logs every action + every divergence/failure. Future
hookup to Recovery's alert plumbing (post-T-0201 generalization) is the
follow-up; for now, log-only is the gap surface.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# T-0471 opt-out: pantheon_config_sync is pull-only (fetch + ff-merge or
# push of already-committed local-ahead state). It never commits new work
# product — there is nothing to isolate in a worktree. The scope-leak race
# class (dirty shared working tree contaminating a narrow git-add commit's
# staged set) does not apply here because there is no `git add` step.
# Wrapping this script in `with_worktree` would invert its semantics: a
# worktree starts at the origin/main HEAD and lacks the local-ahead commits
# this script was designed to push. justifies-via P1 (structural fix targets
# the actual race class; wrapping a non-committing sync tool does not fix a
# race that doesn't exist here), D8 (don't add complexity without value).

REPO = Path("/home/claude/pantheon-config")

# T-0213: alert-state file shape for non-clean exits.
# NOTE (T-1036): Recovery's inbox_consumer used to read this each cycle and surface
# it as a sync_alert, but that reader was retired (the T-0365 pantheon-config-sync
# timer is gone; kino's syncGate drives this tool on-demand now). The state file
# is still written below as a local forensic artifact; nothing consumes it.
# Schema: {alert_class, exit_code, stderr_excerpt, first_seen_at}.
_ALERT_STATE_PATH = (
    Path.home() / ".local" / "state" / "pantheon" / "pantheon_config_sync_alert.json"
)

_STDERR_EXCERPT_MAX = 500


class SyncError(Exception):
    """Raised when a sync sub-step fails."""


def _write_alert_state(alert_class: str, exit_code: int, stderr_excerpt: str) -> None:
    """Write the per-non-clean-exit state file for Recovery's plumbing.

    Idempotent on `first_seen_at`: if state file already exists with the same
    `alert_class`, preserve the original `first_seen_at` so >15-min staleness
    semantics work across multiple sync fires that hit the same failure.
    """
    now = int(time.time())
    first_seen_at = now
    if _ALERT_STATE_PATH.exists():
        try:
            prior = json.loads(_ALERT_STATE_PATH.read_text(encoding="utf-8"))
            if prior.get("alert_class") == alert_class and isinstance(
                prior.get("first_seen_at"), int
            ):
                first_seen_at = prior["first_seen_at"]
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    payload = {
        "alert_class": alert_class,
        "exit_code": exit_code,
        "stderr_excerpt": (stderr_excerpt or "")[:_STDERR_EXCERPT_MAX],
        "first_seen_at": first_seen_at,
    }
    _ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ALERT_STATE_PATH.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _clear_alert_state() -> None:
    """Rotate the alert state file to .consumed.<epoch> on a clean sync.

    Mirrors ho_resumer.py's state-rotation pattern: keeps forensic record of
    past failures + removes the active alert so Recovery's consumer stops surfacing
    it on the next cycle.
    """
    if not _ALERT_STATE_PATH.exists():
        return
    try:
        _ALERT_STATE_PATH.rename(
            _ALERT_STATE_PATH.with_suffix(f".json.consumed.{int(time.time())}")
        )
    except OSError:
        # Best-effort; if rename fails (e.g., race), leave it and next clean
        # sync will retry.
        pass


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the repo. capture_output keeps it out of journal noise."""
    result = subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SyncError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result


def determine_state() -> tuple[bool, bool]:
    """Return (local_ahead, remote_ahead) for local main vs origin/main.

    Per T-0214: use explicit `main` refspec, NOT HEAD. When this sync runs
    while HEAD is on a feature branch (common during b9-pattern team cycles),
    comparing HEAD vs origin/main gave wrong signal — feature-branch commits
    not on main were reported as local-ahead.
    """
    run(["git", "fetch", "origin", "main"])
    local_ahead_raw = run(["git", "rev-list", "--count", "origin/main..main"]).stdout.strip()
    remote_ahead_raw = run(["git", "rev-list", "--count", "main..origin/main"]).stdout.strip()
    return int(local_ahead_raw) > 0, int(remote_ahead_raw) > 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pantheon_config_sync.py",
        description=(
            "pantheon-config bidirectional sync. Default-safe contract (T-0262 "
            "ADDITIVE variant per sop/standard/auto-commit-cron-default-safe-contract.md, "
            "the operator msg_1412): --commit-prod is no-op additive (default still commits + "
            "pushes), --dry-run skips push/pull side effects. Set "
            "PANTHEON_SYNC_STRICT_DEFAULT_SAFE=1 to upgrade to strict default-safe "
            "(refuses without explicit mode) — escape hatch for full rollout."
        ),
    )
    p.add_argument(
        "--commit-prod", action="store_true",
        help="additive no-op flag (default behavior is already prod-targeting). Pass for SOP-alignment / explicit intent.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="run fetch + determine_state but skip the push/pull side effects + alert-state writes.",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="override REPO target (tmp pantheon-config equivalent). Tmp-target for integration smoke.",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """T-0262 additive variant: refuse ONLY on ambiguity (--commit-prod + safe-mode).

    Default behavior is preserved (commits + pushes per existing semantics).
    Strict default-safe (refuse without explicit mode) only fires when the env
    var PANTHEON_SYNC_STRICT_DEFAULT_SAFE=1 is set — escape hatch for full
    rollout to standard-contract once coordinated systemd-unit updates land.

    Returns exit code (64) if the invocation should be rejected; None otherwise.
    """
    safe_explicit = args.dry_run or (args.repo_root is not None)
    if args.commit_prod and safe_explicit:
        print(
            "ERROR: --commit-prod conflicts with safe-mode flag (--dry-run / --repo-root override).",
            file=sys.stderr,
        )
        return 64
    if os.environ.get("PANTHEON_SYNC_STRICT_DEFAULT_SAFE") == "1":
        if not args.commit_prod and not safe_explicit:
            print(
                "ERROR: refusing to run without explicit mode (PANTHEON_SYNC_STRICT_DEFAULT_SAFE=1).\n"
                "Pass one of: --commit-prod / --dry-run / --repo-root <path>.\n"
                "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0262 additive variant).",
                file=sys.stderr,
            )
            return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    # --repo-root override (tmp-target for integration smoke).
    if args.repo_root is not None:
        global REPO
        REPO = args.repo_root.resolve()

    if args.dry_run:
        try:
            local_ahead, remote_ahead = determine_state()
        except SyncError as e:
            print(f"sync: (dry-run) fetch/state failed: {e}", file=sys.stderr)
            return 2
        verdict = (
            "DIVERGENT (both ahead)" if (local_ahead and remote_ahead) else
            "LOCAL AHEAD (would push)" if local_ahead else
            "REMOTE AHEAD (would pull)" if remote_ahead else
            "CLEAN"
        )
        print(f"sync: (dry-run) {verdict}")
        return 0

    try:
        local_ahead, remote_ahead = determine_state()
    except SyncError as e:
        print(f"sync: fetch/state failed: {e}", file=sys.stderr)
        _write_alert_state("fetch_failed", 2, str(e))
        return 2

    if local_ahead and remote_ahead:
        msg = "sync: DIVERGENT — local and remote both ahead; manual resolution needed"
        print(msg, file=sys.stderr)
        _write_alert_state("divergent", 3, msg)
        return 3
    if local_ahead:
        try:
            run(["git", "push", "origin", "main"])
            print("sync: push ok")
            _clear_alert_state()
            return 0
        except SyncError as e:
            print(f"sync: push failed: {e}", file=sys.stderr)
            _write_alert_state("push_failed", 4, str(e))
            return 4
    if remote_ahead:
        try:
            # T-0214: HEAD-on-feature-branch case — `git pull --ff-only origin main`
            # would advance the feature branch, not local main. Use refspec-fetch
            # which updates local main without checkout. When HEAD IS on main,
            # the refspec form is rejected (would update checked-out ref), so fall
            # back to pull.
            current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            if current == "main":
                run(["git", "pull", "--ff-only", "origin", "main"])
            else:
                # T-0222: refspec-fetch refuses when ANY worktree has main
                # checked out (e.g., a transient /tmp HO worktree). Try the
                # refspec form; on the specific "refusing to fetch into branch"
                # error, fall back to plain fetch — leaves local main stale this
                # cycle but doesn't fail; next cycle when HEAD lands on main, the
                # normal pull path will complete.
                try:
                    run(["git", "fetch", "origin", "main:main"])
                except SyncError as fetch_err:
                    if "refusing to fetch into branch" in str(fetch_err):
                        run(["git", "fetch", "origin", "main"])
                        print("sync: pull ok (refspec-fetch deferred — another worktree has main)")
                        _clear_alert_state()
                        return 0
                    raise
            print("sync: pull ok")
            _clear_alert_state()
            return 0
        except SyncError as e:
            print(f"sync: pull failed: {e}", file=sys.stderr)
            _write_alert_state("pull_failed", 5, str(e))
            return 5
    print("sync: clean")
    _clear_alert_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())  # argv parsed inside main()
