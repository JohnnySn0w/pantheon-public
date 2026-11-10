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

T-4575: it also FAILS when root's installed copy of the backup script
(INSTALLED_SCRIPT, which the system unit executes) is missing, differs from the
tracked tools/vps_state_backup.sh, or is not a root-owned, owner-only-writable
regular file.
Root never executes the claude-writable checkout; this is what keeps the root
copy from drifting silently after a merge. Fix with REINSTALL_HINT.

Usage: backup_staleness_check.py [--repo OWNER/NAME] [--max-age-hours N]
Defaults: example-org/pantheon-vps-backup, 48h (tolerates one missed daily
run + clock skew before alerting).
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_REPO = "example-org/pantheon-vps-backup"
DEFAULT_MAX_AGE_HOURS = 48.0
RETRIES = 3
RETRY_SLEEP_S = 30

INSTALLED_SCRIPT = Path("/usr/local/sbin/pantheon-vps-state-backup")
SOURCE_SCRIPT = Path(__file__).resolve().parent / "vps_state_backup.sh"
REINSTALL_HINT = (
    "sudo install -o root -g root -m 0755 "
    "/home/claude/pantheon-config/tools/vps_state_backup.sh " + str(INSTALLED_SCRIPT)
)


def installed_copy_drift(installed: Path = INSTALLED_SCRIPT, source: Path = SOURCE_SCRIPT,
                         owner_uid: int = 0) -> str | None:
    """Why root's installed backup script cannot be trusted, or None when it is a
    regular file (not a symlink), byte-identical to the tracked source, owned by
    `owner_uid`, and writable by its owner only."""
    # One descriptor for the type check and the read: O_NOFOLLOW refuses a symlink,
    # O_NONBLOCK keeps a FIFO from hanging the open, and nothing is read until fstat
    # has proven a regular file.
    try:
        fd = os.open(installed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return f"{installed} is a symlink, not a regular file"
        return f"cannot read the installed copy {installed} ({exc.__class__.__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return f"{installed} is not a regular file"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            body = fh.read()
    except OSError as exc:
        return f"cannot read the installed copy {installed} ({exc.__class__.__name__})"
    finally:
        os.close(fd)
    try:
        tracked = Path(source).read_bytes()
    except OSError as exc:
        return f"cannot read the tracked source {source} ({exc.__class__.__name__})"
    if st.st_uid != owner_uid:
        return f"{installed} is owned by uid {st.st_uid}, not {owner_uid}"
    if st.st_mode & 0o022:
        return f"{installed} is group- or world-writable (mode {st.st_mode & 0o777:o})"
    if body != tracked:
        return f"{installed} differs from the tracked {source}"
    return None


# T-5993: the ORG-101 mirror leg imports _git_utils (and through it remote_resolve +
# repos.yaml) from this root-owned lib copy, never from a claude-writable checkout.
INSTALLED_LIB = Path("/usr/local/lib/pantheon-vps-backup")
SOURCE_ROOT = Path(__file__).resolve().parent.parent
LIB_FILES = ("tools/_git_utils.py", "tools/remote_resolve.py", "repos.yaml")
LIB_REINSTALL_HINT = (
    "sudo install -d -o root -g root -m 0755 " + str(INSTALLED_LIB) + "/tools && "
    + " && ".join(
        f"sudo install -o root -g root -m 0644 /home/claude/pantheon-config/{rel} {INSTALLED_LIB}/{rel}"
        for rel in LIB_FILES)
    + " && re-pin root's repos.yaml (tools/manifest_pin.py)"
)


def installed_lib_drift(lib: Path = INSTALLED_LIB, source_root: Path = SOURCE_ROOT,
                        owner_uid: int = 0) -> str | None:
    """Why root's installed mirror-leg lib cannot be trusted, or None. An ABSENT lib is
    not drift: the leg is simply not installed and prints 'skipped' in the backup log.
    Once present, both dirs must be owned by `owner_uid` with no group/world write bit
    and not symlinks, and each LIB_FILES entry must pass installed_copy_drift against
    its tracked source."""
    lib = Path(lib)
    if not os.path.lexists(lib):
        return None
    for d in (lib, lib / "tools"):
        try:
            st = os.lstat(d)
        except OSError as exc:
            return f"cannot stat {d} ({exc.__class__.__name__})"
        if not stat.S_ISDIR(st.st_mode):
            return f"{d} is not a directory (a symlink is refused)"
        if st.st_uid != owner_uid:
            return f"{d} is owned by uid {st.st_uid}, not {owner_uid}"
        if st.st_mode & 0o022:
            return f"{d} is group- or world-writable (mode {st.st_mode & 0o777:o})"
    for rel in LIB_FILES:
        why = installed_copy_drift(lib / rel, Path(source_root) / rel, owner_uid=owner_uid)
        if why:
            return why
    return None


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

    drift = installed_copy_drift()
    if drift:
        print(f"DRIFT: {drift} — the root unit runs that copy, not the checkout. "
              f"Reinstall: {REINSTALL_HINT}", file=sys.stderr)
    lib_drift = installed_lib_drift()
    if lib_drift:
        print(f"DRIFT: {lib_drift} — the backup's ORG-101 mirror leg imports that lib "
              f"(T-5993). Reinstall: {LIB_REINSTALL_HINT}", file=sys.stderr)
    fresh = _freshness(args)
    return 1 if (drift or lib_drift) else fresh


def _freshness(args) -> int:
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
