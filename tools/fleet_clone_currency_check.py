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

T-3425 (2026-09-05, from T-3009 5-obs threshold hit): per-account
dispositioned-mitigation skip-list. A seat can be listed at
fleet-clone-currency-skip-list.yaml with a disposition_ticket + expected_state
(inbox/active/done); while the ticket sits in that state, DRIFTED/UNKNOWN for
that seat is downgraded to SKIPPED and dropped from the alert. Fail-open at
EVERY step (missing file, parse error, missing/moved disposition ticket, state
mismatch, malformed entry — every failure yields "no skip applied", so a
broken skip-list can never suppress an alert). Skipped seats are still
visible in --dry-run/--json output and in a footer section on any real alert.
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

CURRENT, DRIFTED, NO_CLONE, UNKNOWN, SKIPPED = (
    "CURRENT", "DRIFTED", "NO_CLONE", "UNKNOWN", "SKIPPED",
)

# T-3425: per-account dispositioned-mitigation skip-list. Fail-open (missing
# file / parse error / unresolvable expiry-condition = zero skips applied, so
# a broken skip-list can never SUPPRESS an alert — only ever fail to add one).
SKIP_LIST_PATH_DEFAULT = REPO_ROOT / "fleet-clone-currency-skip-list.yaml"
SKIP_LIST_PATH = Path(os.environ.get("PANTHEON_FLEET_CLONE_SKIP_LIST", str(SKIP_LIST_PATH_DEFAULT)))
_TICKET_STATE_DIRS = ("inbox", "active", "done")

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
    tool exists to catch; NO_CLONE is never a problem; SKIPPED is a
    dispositioned-mitigation carve-out (T-3425) that was verified to still
    apply at check time (fail-open: expired disposition = seat re-alerts)."""
    return [r for r in results if r["verdict"] in (DRIFTED, UNKNOWN)]


def load_skip_list(path: Path | None = None) -> list[dict]:
    """Load the per-account dispositioned-mitigation skip-list (T-3425).

    Fail-open at every step: a missing file, an unreadable file, a parse
    error, or a malformed root shape all yield []. A skip-list that we
    cannot understand must never silently suppress alerts — the whole
    point of this check is to catch silent drift, so an unreadable
    skip-list defaults to "no skips" (alert normally), never "skip
    everything I can't parse" (suppression by malformation).
    """
    p = path or SKIP_LIST_PATH
    try:
        raw = p.read_text()
    except OSError:
        return []
    try:
        import yaml
        data = yaml.safe_load(raw) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("skips")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def is_skip_active(entry: dict, *, repo_root: Path = REPO_ROOT) -> tuple[bool, str]:
    """Check whether a skip entry's expiry-condition still holds.

    An entry names a disposition ticket + expected state; the entry is
    "active" iff the ticket file currently lives in the expected state
    directory (tickets/<state>/<id>.md). Any other outcome — missing
    ticket, missing state, mismatched state, malformed entry — is fail-open
    ("expired", so the seat re-alerts). The reason string is included
    verbatim in render_alert's SKIPPED footer for auditability.
    """
    account = entry.get("account")
    ticket_id = entry.get("disposition_ticket")
    expected = entry.get("expected_state")
    reason = entry.get("reason", "(no reason given)")
    if not (account and ticket_id and expected):
        return False, f"malformed skip entry (account/disposition_ticket/expected_state required); ignored"
    if expected not in _TICKET_STATE_DIRS:
        return False, f"skip for {account}: expected_state '{expected}' is not one of {_TICKET_STATE_DIRS}; ignored"
    actual = None
    for state in _TICKET_STATE_DIRS:
        if (repo_root / "tickets" / state / f"{ticket_id}.md").exists():
            actual = state
            break
    if actual is None:
        return False, f"skip for {account} EXPIRED: disposition ticket {ticket_id} not found in tickets/{{inbox,active,done}}/"
    if actual != expected:
        return False, f"skip for {account} EXPIRED: {ticket_id} now in tickets/{actual}/ (was expected in tickets/{expected}/)"
    return True, f"skip active per {ticket_id} (state={actual}): {reason}"


def apply_skip_list(results: list[dict], skip_list: list[dict],
                    *, repo_root: Path = REPO_ROOT) -> list[dict]:
    """Suppress DRIFTED/UNKNOWN verdicts to SKIPPED for accounts whose
    disposition-skip is currently active. Returns a NEW list; never
    mutates the input results. CURRENT and NO_CLONE are never suppressed
    (nothing to suppress). The SKIPPED result carries the original verdict
    under `original_verdict` and the reason under `skip_reason` for the
    dry-run/json output and the alert footer.
    """
    by_account: dict[str, dict] = {}
    for entry in skip_list:
        acct = entry.get("account")
        if isinstance(acct, str):
            by_account[acct] = entry
    out: list[dict] = []
    for r in results:
        entry = by_account.get(r.get("account", ""))
        if entry is None or r["verdict"] not in (DRIFTED, UNKNOWN):
            out.append(r)
            continue
        active, reason = is_skip_active(entry, repo_root=repo_root)
        if active:
            out.append({**r, "verdict": SKIPPED, "original_verdict": r["verdict"],
                        "skip_reason": reason,
                        "detail": f"{r.get('detail', '')} — SKIPPED: {reason}"})
        else:
            # Expired / malformed skip: keep the original verdict so the seat
            # still alerts. Annotate the detail so an operator can see WHY
            # the skip didn't apply on this cycle.
            out.append({**r, "detail": f"{r.get('detail', '')} — skip attempted but not applied: {reason}"})
    return out


def render_alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> str:
    lines = [f"Fleet clone-currency check: {len(flagged)} seat(s) need attention.", ""]
    for r in flagged:
        lines.append(f"- {r['persona']}/{r['account']}: {r['verdict']} — {r['detail']}")
    lines += [
        "",
        "Remedy: supervised ff-pull each seat (T-2743 pattern) — confirm the clone is "
        "clean, ff-only pull to origin/main, verify a clean consume fire.",
    ]
    if skipped:
        lines += [
            "",
            f"Skipped (dispositioned-mitigation carve-outs still active, T-3425): "
            f"{len(skipped)} seat(s).",
        ]
        for r in skipped:
            lines.append(
                f"- {r['persona']}/{r['account']}: "
                f"was {r.get('original_verdict', 'DRIFTED')} → SKIPPED. "
                f"{r.get('skip_reason', '')}"
            )
    return "\n".join(lines)


def alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> None:
    from tools import mailbox
    mailbox.send(
        ORCHESTRATOR_INBOX, render_alert(flagged, skipped=skipped), "fleet_clone_currency_check",
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
    p.add_argument("--skip-list", type=Path, default=SKIP_LIST_PATH,
                    help=f"per-account dispositioned-mitigation skip-list YAML "
                         f"(default {SKIP_LIST_PATH}; T-3425)")
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

    raw_results = sweep(seats, threshold=args.threshold)
    skip_list = load_skip_list(args.skip_list)
    results = apply_skip_list(raw_results, skip_list)
    flagged = needs_alert(results)
    skipped = [r for r in results if r["verdict"] == SKIPPED]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _log(f"[{r['verdict']:<8}] {r['persona']:10} {r['account']:12} {r['detail']}")

    if flagged and not args.dry_run:
        alert(flagged, skipped=skipped)
        _log(f"alerted orchestrator: {len(flagged)} seat(s) need attention "
             f"({len(skipped)} dispositioned-skipped)")
    elif flagged:
        _log(f"[dry-run] would alert: {len(flagged)} seat(s) need attention "
             f"({len(skipped)} dispositioned-skipped)")
    else:
        _log(f"all seats current; no alert ({len(skipped)} dispositioned-skipped)")
    # Exit 0 always: a monitor must not fail its own timer.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
