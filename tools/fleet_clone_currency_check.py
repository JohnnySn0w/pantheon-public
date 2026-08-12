#!/usr/bin/env python3
"""tools/fleet_clone_currency_check.py — fleet clone-currency check (T-2798, T-2794 option-b).

Consume controllers run tools/persona_bringup.py from each SEAT'S OWN
pantheon-config clone, so a stale clone runs stale tooling. T-2743 found
suborch1/metis 332 behind origin/main and suborch2/atlas 156 behind, with NO
auto-pull and NO discovery mechanism — the drift sat unnoticed for weeks. The
T-2794 design annotation picked option (b): a READ-ONLY currency CHECK with a
drift alert, not auto-pull and not pull-on-fire — remediation (a supervised
ff-pull, the T-2743 pattern) stays a human/HO act; this tool only closes the
DISCOVERY gap.

Per seat, read-only: resolve HEAD (own account direct; every other account via
`sudo -S -k -u <account>`, password piped from /home/claude/root.env — orchestrator
has no NOPASSWD cross-account sudo, unlike recovery's monitors, which is why this
port of the T-2743/persona_cutover.sh recon pattern needs the password leg at
all) and compute commits-behind against the RUNNER's OWN origin/main (never a
second command against the seat's clone). NEVER mutates a seat clone — the
only command that ever reaches one is `git rev-parse HEAD`.

Four verdicts, not a boolean (ci_fleet_account_reconcile / ci_audit_liveness
house style — a monitor whose broken state reads as healthy reproduces the
exact T-2743 discovery gap this tool exists to close):
  CURRENT  — within the drift threshold.
  DRIFTED  — behind-count exceeds the threshold (default 20).
  NO_CLONE — the account has no pantheon-config clone at all (expected for
             seats outside the clone-bearing set, e.g. the mailbox hub).
  UNKNOWN  — could not be verified (sudo/auth failure, timeout, unresolvable
             sha). Alerted exactly like DRIFTED — an unverifiable seat is the
             same discovery-gap risk as a confirmed-stale one.

Seats are DERIVED from agent_role_map.fleet_accounts() — every bound account,
not a hand-carried list — so a new seat is covered automatically the moment
it's bound in instance.yaml; an account with no clone simply reports NO_CLONE
and is never alerted on.

Alerts land in orchestrator's own ORG-026 mailbox (the supervised-pull remediator);
pulls stay supervised, per T-2794's option-(b) decision.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map as arm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# The bare-line sudo password for the claude account (T-2743/persona_cutover.sh
# pattern). Env-overridable so tests never touch the real file.
ROOT_ENV_PATH = Path(os.environ.get("PANTHEON_ROOT_ENV_PATH", "/home/claude/root.env"))

# Env-overridable so tests never depend on a live mailbox hub path.
ORCHESTRATOR_INBOX = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX", f"{arm.mailbox_hub()}/orchestrator")

# Drift threshold, commits-behind. "env/flag" per the ticket: env sets the
# default, --threshold overrides per-invocation.
DRIFT_THRESHOLD_DEFAULT = int(os.environ.get("PANTHEON_FLEET_CLONE_DRIFT_THRESHOLD", "20"))

CURRENT, DRIFTED, NO_CLONE, UNKNOWN = "CURRENT", "DRIFTED", "NO_CLONE", "UNKNOWN"

# git stderr shapes that mean "this account has no pantheon-config clone at
# all" (path absent, or present but not a repo) — distinct from every other
# failure, which is UNKNOWN (could not verify, not "nothing to verify").
_NO_CLONE_MARKERS = ("no such file or directory", "not a git repository", "cannot change to")


@dataclass(frozen=True)
class Seat:
    persona: str
    account: str
    clone_path: str


def discover_seats() -> list[Seat]:
    """Every fleet seat's (persona, account, clone_path) — derived from
    agent_role_map.fleet_accounts(), never hand-listed. A future account bound
    in instance.yaml is covered the next time this runs, with no code change;
    an account with no pantheon-config clone self-classifies NO_CLONE at
    check time rather than needing an exclusion list here."""
    seats = []
    for account in arm.fleet_accounts():
        persona = arm.account_persona(account) or account
        seats.append(Seat(persona=persona, account=account, clone_path=f"/home/{account}/pantheon-config"))
    return seats


def _read_sudo_password() -> str:
    """The claude account's sudo password, as a bare line (T-2743 convention:
    `tr -d '\\n' < /home/claude/root.env`). Never logged, never returned in an
    error message, and the only caller pipes it via subprocess `input=`."""
    return ROOT_ENV_PATH.read_text().strip()


def _seat_head(seat: Seat, *, run=subprocess.run) -> tuple[str, str | None]:
    """Read-only `git rev-parse HEAD` for one seat. Returns (verdict, head_sha):
    verdict is "" on success, else NO_CLONE or UNKNOWN (head_sha is None on
    failure). This is the ONLY command this tool ever sends to a seat clone —
    own account direct, every other account via `sudo -S -k`, password on
    stdin (never argv, never logged)."""
    me = getpass.getuser()
    argv = ["git", "-C", seat.clone_path, "rev-parse", "HEAD"]
    kwargs: dict = {}
    if seat.account != me:
        try:
            password = _read_sudo_password()
        except OSError:
            return UNKNOWN, None
        argv = ["sudo", "-S", "-k", "-u", seat.account] + argv
        kwargs["input"] = password + "\n"
    try:
        r = run(argv, capture_output=True, text=True, timeout=20, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, None
    if r.returncode == 0:
        head = (r.stdout or "").strip()
        return ("", head) if head else (UNKNOWN, None)
    stderr_lower = (r.stderr or "").lower()
    if any(marker in stderr_lower for marker in _NO_CLONE_MARKERS):
        return NO_CLONE, None
    return UNKNOWN, None


def _fetch_origin_main(*, repo_root: Path = REPO_ROOT, run=subprocess.run) -> bool:
    """Refresh the RUNNER's own origin/main remote-tracking ref. Read-only
    w.r.t. working state (a fetch never touches HEAD or the working tree) and
    only ever runs against `repo_root` (the runner's own clone) — never a
    seat's clone."""
    try:
        r = run(["git", "-C", str(repo_root), "fetch", "origin", "main"],
                capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _behind_count(head_sha: str, *, repo_root: Path = REPO_ROOT, run=subprocess.run) -> tuple[str, int | None]:
    """`git rev-list --count <head_sha>..origin/main`, run entirely in the
    RUNNER's own clone (repo_root) — the seat's HEAD is just a sha string
    handed to a local command; no second command ever touches the seat's
    clone. Returns (verdict, count): verdict is "" on success, else UNKNOWN."""
    try:
        r = run(["git", "-C", str(repo_root), "rev-list", "--count", f"{head_sha}..origin/main"],
                capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, None
    if r.returncode != 0:
        return UNKNOWN, None
    try:
        return "", int((r.stdout or "").strip())
    except ValueError:
        return UNKNOWN, None


def check_seat(seat: Seat, *, threshold: int, head_run=subprocess.run, count_run=subprocess.run) -> dict:
    """Classify one seat. Pure decision over the two read-only legs above."""
    verdict, head = _seat_head(seat, run=head_run)
    if verdict:
        return {"persona": seat.persona, "account": seat.account, "verdict": verdict,
                "detail": f"could not read HEAD ({verdict.lower()})"}
    verdict, behind = _behind_count(head, run=count_run)
    if verdict:
        return {"persona": seat.persona, "account": seat.account, "verdict": verdict,
                "detail": f"HEAD={head[:10]} but behind-count unavailable"}
    status = DRIFTED if behind > threshold else CURRENT
    return {"persona": seat.persona, "account": seat.account, "verdict": status,
            "head": head, "behind": behind,
            "detail": f"HEAD={head[:10]}, {behind} commit(s) behind origin/main"}


def sweep(seats: list[Seat], *, threshold: int, head_run=subprocess.run, count_run=subprocess.run) -> list[dict]:
    return [check_seat(s, threshold=threshold, head_run=head_run, count_run=count_run) for s in seats]


def needs_alert(results: list[dict]) -> list[dict]:
    """DRIFTED (over threshold) and UNKNOWN (could not verify) both need
    attention — an unverifiable seat is exactly the silent-drift risk this
    tool exists to catch; NO_CLONE is never a problem."""
    return [r for r in results if r["verdict"] in (DRIFTED, UNKNOWN)]


def render_alert(flagged: list[dict]) -> str:
    lines = [f"Fleet clone-currency check: {len(flagged)} seat(s) need attention.", ""]
    for r in flagged:
        lines.append(f"- {r['persona']}/{r['account']}: {r['verdict']} — {r['detail']}")
    lines += [
        "",
        "Remedy: supervised ff-pull each seat (T-2743 pattern) — confirm the clone is "
        "clean, ff-only pull to origin/main, verify a clean consume fire.",
    ]
    return "\n".join(lines)


def alert(flagged: list[dict]) -> None:
    from tools import mailbox
    mailbox.send(
        ORCHESTRATOR_INBOX, render_alert(flagged), "fleet_clone_currency_check",
        subject=f"fleet clone-currency: {len(flagged)} seat(s) need attention",
    )


def _log(msg: str) -> None:
    print(f"[fleet_clone_currency_check ts={int(time.time())}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="sweep the fleet + alert on drift (cron path)")
    p.add_argument("--dry-run", action="store_true", help="sweep + print, send no alert")
    p.add_argument("--threshold", type=int, default=DRIFT_THRESHOLD_DEFAULT,
                    help=f"commits-behind alert threshold (default {DRIFT_THRESHOLD_DEFAULT})")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (cron path) or "
            "--dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    seats = discover_seats()
    if not _fetch_origin_main():
        _log("could not fetch origin/main — skipping this cycle's sweep (stale ref would under-report drift)")
        if not args.dry_run:
            from tools import mailbox
            mailbox.send(
                ORCHESTRATOR_INBOX,
                "Fleet clone-currency check: could not fetch origin/main this cycle — "
                "the sweep was SKIPPED (a stale ref would under-report drift, which is "
                "worse than skipping). Check the runner's own clone/network.",
                "fleet_clone_currency_check",
                subject="fleet clone-currency: fetch failed, sweep skipped",
            )
        return 0

    results = sweep(seats, threshold=args.threshold)
    flagged = needs_alert(results)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _log(f"[{r['verdict']:<8}] {r['persona']:10} {r['account']:12} {r['detail']}")

    if flagged and not args.dry_run:
        alert(flagged)
        _log(f"alerted orchestrator: {len(flagged)} seat(s) need attention")
    elif flagged:
        _log(f"[dry-run] would alert: {len(flagged)} seat(s) need attention")
    else:
        _log("all seats current; no alert")
    # Exit 0 always: a monitor must not fail its own timer.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
