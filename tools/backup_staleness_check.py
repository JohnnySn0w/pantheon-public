#!/usr/bin/env python3
"""backup_staleness_check.py — T-2886: alert when the off-box VPS backup goes stale.

The T-2178 backup runs as a SYSTEM/root unit (it must read every account), and
both fleet failure-alert mechanisms are user-scoped — so its unit failures are
structurally invisible. The honest health signal for a backup is COMMIT
FRESHNESS anyway: every failure mode (unit failure, script hang, push failure,
auth break, box-down) shows up as a missing daily commit in the backup repo.

This check runs as a USER unit (claude) on a daily timer and FAILS (exit 1)
when the backup repo's latest commit is older than the threshold — the unit's
`OnFailure=pantheon-unit-failure-alert@%n.service` then routes the alert to
Recovery's mailbox via the standard fleet path (Telegram fallback included). No
new alert plumbing; the watchdog just fails loudly on the existing rails.

Fail-closed: if the GitHub API cannot be reached after retries, that is ALSO a
failure — a watchdog that shrugs at errors is blind exactly when the box's
network/auth is broken, which is a state the operator should hear about.

Usage: backup_staleness_check.py [--repo OWNER/NAME] [--max-age-hours N]
Defaults: example-org/pantheon-vps-backup, 48h (tolerates one missed daily
run + clock skew before alerting).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time

DEFAULT_REPO = "example-org/pantheon-vps-backup"
DEFAULT_MAX_AGE_HOURS = 48.0
RETRIES = 3
RETRY_SLEEP_S = 30


def age_hours(iso_ts: str, now: dt.datetime) -> float:
    """Hours elapsed from an ISO-8601 UTC timestamp (e.g. 2026-08-12T03:30:03Z) to `now`."""
    then = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    return (now - then).total_seconds() / 3600.0


def latest_commit_date(repo: str) -> str:
    """ISO date of the repo's newest commit, via the gh CLI (authed as the seat)."""
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits?per_page=1"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api failed: {out.stderr.strip()[:200]}")
    commits = json.loads(out.stdout)
    if not commits:
        raise RuntimeError(f"{repo} has no commits")
    return commits[0]["commit"]["committer"]["date"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    args = ap.parse_args(argv)

    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            iso = latest_commit_date(args.repo)
            hours = age_hours(iso, dt.datetime.now(dt.timezone.utc))
            if hours > args.max_age_hours:
                print(
                    f"STALE: {args.repo} latest commit {iso} is {hours:.1f}h old "
                    f"(threshold {args.max_age_hours:.0f}h) — the daily backup has "
                    f"missed at least one run; check pantheon-vps-state-backup.service "
                    f"(system unit) and its push path.", file=sys.stderr,
                )
                return 1
            print(f"fresh: {args.repo} latest commit {iso} ({hours:.1f}h old, threshold {args.max_age_hours:.0f}h)")
            return 0
        except Exception as exc:  # noqa: BLE001 — every failure class means "cannot prove freshness"
            last_err = exc
            if attempt < RETRIES:
                time.sleep(RETRY_SLEEP_S)
    print(
        f"UNVERIFIABLE: could not read {args.repo} after {RETRIES} attempts "
        f"({last_err}) — freshness unproven, treating as failure (fail-closed).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
