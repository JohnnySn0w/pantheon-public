#!/usr/bin/env python3
"""Pantheon suite-health soak filer (T-0991 option b / T-0992).

Monthly timer entrypoint that files ONE per-account suite-health soak ticket to
each Pantheon agent (specialist, orchestrator, recovery). Each filed ticket instructs that
agent's OWN session to run its tools-suite leg with the guarded invocation and
resolve with the green count — decentralizing execution so no cross-account sudo
is needed (T-0991 item-2 decision: option (b), per-account self-run; the
all-accounts soak grew out of the T-0884 / ORG-025 watch-dispatch cutover).

This file is the due-detector + ticket-filer ONLY: it runs NO test suite itself.
The receiving agent's in-session run + resolve is the execution leg.

Design notes (from the T-0992 pre-implementation review):
  - Files via subprocess to `tools/ticket.py file` (reusing watch_runner's
    transient-retry kernel `_invoke_ticket_file`) — NEVER re-implements ID
    allocation, commit, push, or the HERMES byte-sequence guard; those all live
    inside `ticket.py file` and run for us. One file-invocation = one commit+push.
  - Idempotency scans inbox/ AND active/ (NOT done/; `blocked/` was retired per
    ORG-019). A claimed-but-unresolved soak ticket lives in active/, so an
    inbox-only scan would refile a duplicate every cycle — the failure this
    guards against. Match on the frontmatter LABEL (not a free-text substring).
  - No `--intent` (it is target=operator-specific; ticket.py hard-errors otherwise).

CLI:
    python3 tools/soak_filer.py --file-soak-tickets   # monthly timer entrypoint
    python3 tools/soak_filer.py --dry-run             # print decisions; no side effects
    python3 tools/soak_filer.py --force               # bypass idempotency (supervised first-fire)
    python3 tools/soak_filer.py --repo-root <path>    # tmp-target for integration smoke

Default-safe contract (T-0261, sop/standard/auto-commit-cron-default-safe-contract.md):
refuses to run without an explicit mode.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse the proven ticket-file invoker (transient-retry kernel) + the frontmatter
# parser rather than re-implementing them — one source of truth.
from tools.ticket import parse_ticket  # noqa: E402
from tools.watch_runner import _invoke_ticket_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

# The three always-on agent accounts each run their own suite leg.
SOAK_AGENTS = ("specialist", "orchestrator", "recovery")

# Idempotency marker: a frontmatter label unique to auto-filed soak legs. The
# scan matches on label membership (robust), NOT a free-text body substring (a
# comment mentioning the token would falsely suppress filing).
SOAK_LABEL = "suite-health-soak-auto"
SOAK_LABELS_CSV = "suite-health,soak,suite-health-soak-auto,infra"

# The guarded suite invocation the receiving agent runs. The env flag is
# MANDATORY: without it the full suite leaks alerts via the send-keys-to-pane
# path (the leak is now also fixed in inbox_consumer._send_alert_via_recovery, which
# honors _live_telegram_suppressed, but the flag stays belt-and-suspenders).
SOAK_INVOCATION = "PANTHEON_DISABLE_LIVE_TELEGRAM=1 python3 -m unittest discover -s tools -t ."

# States whose presence means a prior soak leg is still UNRESOLVED. done/ is
# excluded (resolved); blocked/ does not exist (ORG-019 retired it).
UNRESOLVED_STATES = ("inbox", "active")


# ---------------------------------------------------------------------------
# Idempotency scan.
# ---------------------------------------------------------------------------


def unresolved_soak_for(agent: str, repo_root: Path | None = None) -> str | None:
    """Return the filename of an unresolved auto-soak ticket targeting `agent`,
    else None. "Unresolved" = present in tickets/inbox/ or tickets/active/ (NOT
    done/). Matches on frontmatter target == agent AND SOAK_LABEL in labels.

    Mirrors inbox_consumer._list_auto_delivered_unresolved_for_agent's
    inbox+active scan model — deliberately NOT knowledge_sweep's inbox-only
    pending check, which would miss a claimed (active/) soak leg and refile a
    duplicate. Per-agent: filing for specialist is not suppressed by recovery's unresolved
    leg. Fail-open: an unparseable ticket file is skipped, not fatal.
    """
    root = repo_root or REPO_ROOT
    tickets = root / "tickets"
    for state in UNRESOLVED_STATES:
        d = tickets / state
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                t = parse_ticket(p)
            except Exception:
                continue
            if t.fields.get("target") == agent and SOAK_LABEL in (t.fields.get("labels") or []):
                return p.name
    return None


# ---------------------------------------------------------------------------
# Per-account ticket body.
# ---------------------------------------------------------------------------


def _build_ask(agent: str) -> str:
    return (
        f"Suite-health soak leg for the `{agent}` account — standing all-accounts "
        f"soak (T-0991/T-0992 option b: each account runs its OWN leg, no "
        f"cross-account sudo).\n\n"
        f"Run the full tools suite with the guarded invocation (the env flag is "
        f"MANDATORY — without it the full suite leaks alerts via the "
        f"send-keys-to-pane path):\n"
        f"    {SOAK_INVOCATION}\n\n"
        f"Route the output to a file (do NOT trust inline terminal output — test "
        f"stdout garbles it), then verify exit 0 AND the `Ran N tests` / `OK` "
        f"summary line. Resolve THIS ticket with the green count, e.g. "
        f"`python3 tools/ticket.py resolve <id> --with \"NNNN green, exit 0\"`.\n\n"
        f"If RED: a SINGLE red run is NOT conclusive — the suite has a rare flake "
        f"(T-0993, ~1 in 30+ runs, test unidentified). Re-run up to 2 more times. "
        f"A genuine account-dependent regression fails DETERMINISTICALLY (every "
        f"run); only treat it as real if it is RED on a majority of runs. If "
        f"consistently RED: do NOT resolve — investigate the regression (exactly "
        f"what this soak exists to catch) and surface it. If it was a one-off "
        f"flake: resolve noting the flake AND the failing test name (so T-0993 can "
        f"finally pin it). Prior manual cycle (2026-06-09) was 1945 green on all "
        f"three accounts; green is expected — the point is confirmation, not assumption."
    )


def _build_context(agent: str, today: date) -> str:
    return (
        f"suite-health-soak-auto: {agent} {today.isoformat()}\n\n"
        f"Filed by soak_filer.py --file-soak-tickets (T-0992), the monthly standing "
        f"per-account suite-health soak (T-0991 item-2 decision: option b). The "
        f"filer runs NO suite itself; each account runs its own leg in-session. "
        f"Parent soak: T-0884 (ORG-025 watch-dispatch cutover)."
    )


# ---------------------------------------------------------------------------
# Timer entrypoint.
# ---------------------------------------------------------------------------


def file_soak_tickets(
    today: date,
    *,
    agents: tuple[str, ...] = SOAK_AGENTS,
    force: bool = False,
    ticket_bin: Path | None = None,
    env: dict | None = None,
    repo_root: Path | None = None,
) -> int:
    """Monthly entrypoint: file ONE soak ticket per agent, idempotently.

    Per agent: skip if an unresolved auto-soak ticket already targets it (unless
    --force). Each filing is an independent `ticket.py file` subprocess (its own
    id/commit/push). Processes all agents even if one fails (the next run retries
    the failed one while skipping the already-filed). Returns 0 if every agent
    was filed-or-skipped, 1 if any agent errored.
    """
    root = repo_root or REPO_ROOT
    tb = ticket_bin or TICKET_BIN
    subprocess_env = env if env is not None else (os.environ.copy() | {"PANTHEON_ACTOR": "recovery"})
    rc = 0
    for agent in agents:
        if not force:
            existing = unresolved_soak_for(agent, root)
            if existing:
                print(f"[soak_filer] {agent}: unresolved soak ticket exists ({existing}); skipping")
                continue
        title = f"Suite-health soak leg — {agent} ({today.isoformat()})"
        argv = [
            sys.executable,
            str(tb),
            "file",
            "--requester", "recovery",
            "--target", agent,
            # NB: no --intent — ticket.py rejects it for non-operator targets.
            "--title", title,
            "--labels", SOAK_LABELS_CSV,
            "--project-id", "null",
            "--nearest-intent-ref", "maintenance:test-suite-soak",
            "--ask", _build_ask(agent),
            "--context", _build_context(agent, today),
            # T-3328: connect via the goal/arc-intent edge rather than filing
            # an orphan — the monthly soak leg is infra maintenance.
            "--serves", "the monthly suite-health soak leg keeps running per "
                        "agent (no missed regression window)",
        ]
        outcome, tid, detail = _invoke_ticket_file(
            argv, f"soak_filer {agent} {today.isoformat()}", subprocess_env
        )
        if outcome == "filed":
            print(f"[soak_filer] {agent}: filed {tid or '(id?)'}")
        else:
            print(f"[soak_filer] {agent}: FAILED to file: {detail}", file=sys.stderr)
            rc = 1
    return rc


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _dry_run(today: date) -> int:
    """Print the would-file / would-skip decision per agent; no side effects."""
    for agent in SOAK_AGENTS:
        existing = unresolved_soak_for(agent)
        if existing:
            print(f"[soak_filer] DRY-RUN {agent}: would SKIP (unresolved soak {existing})")
        else:
            print(f"[soak_filer] DRY-RUN {agent}: would FILE soak ticket")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pantheon suite-health soak filer (T-0992). Default-safe "
                    "contract (T-0261): must pass --file-soak-tickets or --dry-run.",
    )
    p.add_argument(
        "--file-soak-tickets", action="store_true",
        help="MONTHLY TIMER ENTRYPOINT: file one per-account suite-health soak "
             "ticket to specialist/orchestrator/recovery, skipping any account with an unresolved "
             "prior leg. Files NO suite itself. Prod-mutating mode.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the per-agent would-file/would-skip decision; file nothing. Safe-mode.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="bypass the idempotency skip; file for every agent regardless (for "
             "the supervised first-fire). Modifier for --file-soak-tickets.",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="override REPO_ROOT (tmp pantheon-config equivalent) for integration smoke. Modifier.",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse to run without an explicit action mode (T-0261). --file-soak-tickets
    (prod) and --dry-run (safe) are mutually exclusive. Returns 64 to reject."""
    if not (args.file_soak_tickets or args.dry_run):
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --file-soak-tickets  file per-account soak tickets (monthly timer)\n"
            "  --dry-run            print per-agent file/skip decisions; no side effects\n"
            "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0261).",
            file=sys.stderr,
        )
        return 64
    if args.file_soak_tickets and args.dry_run:
        print(
            "ERROR: --file-soak-tickets and --dry-run are mutually exclusive.",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    global REPO_ROOT, TICKET_BIN
    args = parse_args(argv)

    gate = _enforce_default_safe(args)
    if gate is not None:
        return gate

    if args.repo_root is not None:
        REPO_ROOT = args.repo_root.resolve()
        TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

    if args.dry_run:
        return _dry_run(_today())

    if args.file_soak_tickets:
        return file_soak_tickets(_today(), force=args.force)

    return 0


def _today() -> date:
    """Today's date. Isolated so tests can monkeypatch without touching stdlib."""
    import datetime as _dt

    return _dt.date.today()


if __name__ == "__main__":
    raise SystemExit(main())
