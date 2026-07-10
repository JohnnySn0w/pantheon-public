#!/usr/bin/env python3
"""Pantheon weekly ticket-summary generator.

Per the shape ratified at `reports/2026-W21.md` and the agent brief at
`sop/standard/weekly-ticket-summary-generation.md`:

1. Determine target ISO week (default: prior week; --week YYYY-Wnn> to override).
2. Walk `tickets/done/` for tickets resolved within the week range
   (Mon 00:00:00 → Sun 23:59:59 UTC). Resolution timestamp is the last
   resolve / close-action / close-noodle entry in the transition log.
3. Build an input bundle (markdown-structured) summarizing every ticket.

Two-phase flow (post-2026-06-13 billing change; headless `claude -p` is API-billed):

  Phase 1 — cron (--file-request, the default):
    Write the full generation prompt to `reports/.pending/<YYYY>-W<NN>-prompt.md`
    and file a generation-request ticket (target=orchestrator). No headless dispatch.

  Phase 2 — in-session fulfillment (--finalize <week>):
    Assert the report file exists (written in-session by Orchestrator), run
    `refresh_readme_index()`, then `commit_and_push()` via the shared
    `_git_utils` helpers (HERMES guard + scope assertion).

CLI:
    python3 tools/weekly_ticket_summary.py [--file-request]    # cron: file request ticket
    python3 tools/weekly_ticket_summary.py --file-request --dry-run
    python3 tools/weekly_ticket_summary.py --finalize 2026-W23 # post-gen: commit report
    python3 tools/weekly_ticket_summary.py --finalize 2026-W23 --dry-run
    python3 tools/weekly_ticket_summary.py --finalize 2026-W23 --no-push
    python3 tools/weekly_ticket_summary.py --week 2026-W21 --file-request

Designed for weekly cron at Monday 09:00 UTC on `claude-fallback` (Recovery) via
`pantheon-weekly-ticket-summary.{service,timer}`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical filer account for the --file-request cron (T-1191 dedup). The cron
# must file from EXACTLY ONE account, or two accounts race to file an identical
# request ticket (the 2026-06-15 duplicate-T-NNNN incident). Recovery
# (claude-fallback) is canonical: the dedicated maintenance-cron account (it
# already owns ticket_grooming + the keep-going injectors). Encoding the owner
# here makes the dedup hold regardless of which account's systemd timer fires —
# a stray fire on any other account no-ops instead of filing a duplicate. The
# in-session FULFILLER (--finalize, run by Orchestrator) is intentionally NOT gated.
CANONICAL_FILER_ACCOUNT = "claude-fallback"
REPORTS_DIR = REPO_ROOT / "reports"
DONE_DIR = REPO_ROOT / "tickets" / "done"
SOP_PATH = REPO_ROOT / "sop" / "standard" / "weekly-ticket-summary-generation.md"
EXEMPLAR_PATH = REPORTS_DIR / "2026-W21.md"
README_PATH = REPORTS_DIR / "README.md"
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

SENTINEL_START = "<!-- AUTOGEN-START spine-index -->"
SENTINEL_END = "<!-- AUTOGEN-END spine-index -->"


# ---------------------------------------------------------------------------
# ISO week + date helpers
# ---------------------------------------------------------------------------


def iso_week_range(year: int, week: int) -> tuple[dt.date, dt.date]:
    """Return (Monday, Sunday) dates for the given ISO year + week."""
    monday = dt.date.fromisocalendar(year, week, 1)
    sunday = monday + dt.timedelta(days=6)
    return monday, sunday


def parse_week_arg(s: str) -> tuple[int, int]:
    """Parse YYYY-Wnn> → (year, week). Case-insensitive on the W."""
    m = re.fullmatch(r"(\d{4})-[Ww](\d{1,2})", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid --week format: {s!r} (expected YYYY-Wnn>)")
    year, week = int(m.group(1)), int(m.group(2))
    if not (1 <= week <= 53):
        raise argparse.ArgumentTypeError(f"week out of range: {week}")
    return year, week


def prior_iso_week(today: dt.date | None = None) -> tuple[int, int]:
    """Return (year, week) for the ISO week ENDING before today."""
    today = today or dt.date.today()
    last_sunday = today - dt.timedelta(days=today.isoweekday() % 7 or 7)
    iso = last_sunday.isocalendar()
    return iso.year, iso.week


def week_label(year: int, week: int) -> str:
    return f"{year}-W{week:02d}"


# ---------------------------------------------------------------------------
# Ticket walking
# ---------------------------------------------------------------------------


RESOLVE_LOG_RE = re.compile(
    r"^- (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+\[(?P<actor>[^\]]+)\]\s+"
    r"(?P<action>resolved|resolve|close-action|close-noodle)\b",
    re.MULTILINE,
)
FRONTMATTER_FIELD_RE = re.compile(r"^(\w+):\s*(.*)$")
COMMIT_HASH_RE = re.compile(r"\b([0-9a-f]{7,40})\b")


@dataclass
class TicketRecord:
    id: str
    title: str
    target: str
    requester: str
    labels: list[str] = field(default_factory=list)
    resolved_at: dt.datetime | None = None
    resolve_actor: str = ""
    resolve_action: str = ""
    transition_log: str = ""
    commit_refs: list[str] = field(default_factory=list)
    intent: str = ""
    parent_ticket: str = ""

    def to_bundle_block(self) -> str:
        lines = [f"### {self.id} — {self.title}"]
        meta = [
            f"- target: {self.target}",
            f"- requester: {self.requester}",
            f"- labels: {', '.join(self.labels) if self.labels else '(none)'}",
            f"- resolved: {self.resolved_at.isoformat() if self.resolved_at else '?'}"
            f" via {self.resolve_action} by {self.resolve_actor}",
        ]
        if self.intent:
            meta.append(f"- intent: {self.intent}")
        if self.parent_ticket:
            meta.append(f"- parent_ticket: {self.parent_ticket}")
        if self.commit_refs:
            meta.append(f"- commit refs: {', '.join(self.commit_refs[:8])}")
        lines.extend(meta)
        if self.transition_log.strip():
            lines.append("- transition log:")
            for log_line in self.transition_log.strip().splitlines():
                lines.append(f"    {log_line}")
        return "\n".join(lines)


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        m = FRONTMATTER_FIELD_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _parse_labels(fm: dict[str, str]) -> list[str]:
    raw = fm.get("labels", "").strip()
    if not raw or raw == "[]":
        return []
    raw = raw.strip("[]").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _extract_transition_log(text: str) -> str:
    """Return everything after the `## Transition log` heading."""
    m = re.search(r"^##\s+Transition log\s*\n", text, re.MULTILINE)
    if not m:
        return ""
    return text[m.end():]


def _extract_commit_refs(transition_log: str) -> list[str]:
    """Pull plausible 7-40 hex commit hashes from the transition log."""
    seen: list[str] = []
    for m in COMMIT_HASH_RE.finditer(transition_log):
        h = m.group(1)
        if h not in seen:
            seen.append(h)
    return seen


def parse_ticket_file(path: Path) -> TicketRecord | None:
    text = path.read_text(errors="replace")
    fm = _parse_frontmatter(text)
    matches = list(RESOLVE_LOG_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "(no title)"
    transition = _extract_transition_log(text)
    return TicketRecord(
        id=path.stem,
        title=title,
        target=fm.get("target", "?"),
        requester=fm.get("requester", "?"),
        labels=_parse_labels(fm),
        resolved_at=dt.datetime.fromisoformat(last.group("ts").replace("Z", "+00:00")),
        resolve_actor=last.group("actor"),
        resolve_action=last.group("action"),
        transition_log=transition,
        commit_refs=_extract_commit_refs(transition),
        intent=fm.get("intent", ""),
        parent_ticket=fm.get("parent_ticket", ""),
    )


def walk_done_tickets(monday: dt.date, sunday: dt.date) -> list[TicketRecord]:
    """Return all done tickets resolved within [monday 00:00, sunday+1 00:00) UTC."""
    start = dt.datetime.combine(monday, dt.time.min, tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(sunday + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc)
    out: list[TicketRecord] = []
    for f in sorted(DONE_DIR.glob("T-*.md")):
        rec = parse_ticket_file(f)
        if rec and rec.resolved_at and start <= rec.resolved_at < end:
            out.append(rec)
    out.sort(key=lambda r: r.resolved_at)
    return out


# ---------------------------------------------------------------------------
# Bundle + prompt
# ---------------------------------------------------------------------------


def build_input_bundle(tickets: Iterable[TicketRecord], monday: dt.date, sunday: dt.date) -> str:
    tickets = list(tickets)
    header = (
        f"# Input bundle — week of {monday.isoformat()} to {sunday.isoformat()}\n\n"
        f"Total tickets resolved: {len(tickets)}\n"
    )
    blocks = [t.to_bundle_block() for t in tickets]
    return header + "\n\n" + "\n\n".join(blocks) + "\n"


def build_prompt(
    sop_text: str,
    exemplar_text: str,
    bundle: str,
    target_path_rel: str,
    week_label_str: str,
    monday: dt.date,
    sunday: dt.date,
    ticket_count: int,
) -> str:
    return (
        "You are the weekly ticket-summary generator for Pantheon. "
        "Read the SOP/brief below, study the approved exemplar, then write "
        "the executive-summary report for the target week using the input bundle.\n\n"
        "Output the report markdown ONLY — no preface, no explanation, no fences "
        "around it. Begin with the YAML frontmatter `---` and end after the trailing "
        "italic pointer line. Status frontmatter MUST be `GENERATED` (not `DRAFT-SAMPLE`).\n\n"
        f"Target file path: `{target_path_rel}`\n"
        f"Week label: `{week_label_str}`\n"
        f"Date range: {monday.isoformat()} to {sunday.isoformat()}\n"
        f"Tickets resolved this week: {ticket_count}\n\n"
        "==================== SOP / AGENT BRIEF ====================\n"
        f"{sop_text}\n"
        "==================== APPROVED EXEMPLAR (reports/2026-W21.md) ====================\n"
        f"{exemplar_text}\n"
        "==================== INPUT BUNDLE ====================\n"
        f"{bundle}\n"
        "==================== END ====================\n\n"
        "Produce the report now."
    )


# ---------------------------------------------------------------------------
# Phase 1: --file-request mode (cron path, subscription-safe)
# ---------------------------------------------------------------------------


def file_request(
    label: str,
    monday: dt.date,
    sunday: dt.date,
    tickets: list[TicketRecord],
    *,
    dry_run: bool = False,
    ticket_bin: Path = TICKET_BIN,
) -> int:
    """File the generation-request ticket for in-session fulfillment.

    This is the cron-safe path: it writes no report, calls no headless subprocess.
    The generation prompt is NOT written to disk — it is DETERMINISTICALLY
    regenerable from the (now-closed) target week's done-tickets, so the fulfiller
    reconstructs it on its own account via `--print-prompt <week>`. (Previously the
    prompt was written to reports/.pending/ but only the TICKET was committed, so
    the prompt never crossed accounts to the Phase-2 fulfiller — an orphan file;
    T-1191 follow-up flagged by Orchestrator.)

    Returns 0 on success, 1 on filing failure.
    """
    target_path_rel = f"reports/{label}.md"

    if dry_run:
        print(f"[weekly_ticket_summary] --dry-run: would file ticket for in-session generation of {target_path_rel}")
        return 0

    ask = (
        f"Generate the weekly ticket summary for {label} in-session. The generation "
        f"prompt is regenerated on-demand (it is deterministic from the week's "
        f"done-tickets) — no file to read. Steps to fulfill:\n"
        f"1. Run `python3 tools/weekly_ticket_summary.py --print-prompt {label}` to "
        f"obtain the full generation prompt (regenerates it on your account).\n"
        f"2. Generate the report in-session from that prompt.\n"
        f"3. Write the result to `{target_path_rel}`.\n"
        f"4. Run `python3 tools/weekly_ticket_summary.py --finalize {label}` "
        f"to refresh the README index and commit+push the report.\n"
        f"5. Resolve this ticket."
    )
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "orchestrator",
        "--target", "orchestrator",
        # NB: no --intent — it is a target=operator-specific surface (ticket.py
        # rejects it for target=orchestrator, T-0465/T-0852). An orchestrator-targeted action
        # ticket carries no intent field. (Passing it failed the 09:00Z service
        # run every week: prompt staged but the request ticket never filed.)
        "--title", f"Generate weekly ticket summary {label}",
        "--labels", "weekly-summary,in-session-gen",
        "--ask", ask,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(
            f"[weekly_ticket_summary] ERROR: ticket.py file failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:300]}",
            file=sys.stderr,
        )
        return 1
    tid_match = re.search(r"filed (T-\d+)", result.stdout)
    tid = tid_match.group(1) if tid_match else "(id?)"
    print(f"[weekly_ticket_summary] filed generation-request ticket {tid} for {label}")
    return 0


# ---------------------------------------------------------------------------
# Phase 2: --finalize mode (in-session fulfiller commits the written report)
# ---------------------------------------------------------------------------


def finalize_report(label: str, *, dry_run: bool = False, push: bool = True) -> int:
    """Assert the report exists, refresh the README index, commit+push.

    Called by the in-session Orchestrator after writing `reports/<label>.md`.
    Returns 0 on success, 1 on error.
    """
    target_path = REPORTS_DIR / f"{label}.md"
    if not target_path.exists():
        print(
            f"[weekly_ticket_summary] ERROR: --finalize: report not found: {target_path}",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(f"[weekly_ticket_summary] --dry-run: would refresh README index and commit {target_path.name}")
        return 0

    changed_readme = refresh_readme_index()
    paths_to_commit = [target_path]
    if changed_readme:
        paths_to_commit.append(README_PATH)
        print(f"[weekly_ticket_summary] refreshed {README_PATH}")

    commit_and_push(paths_to_commit, week_label_str=label, push=push)
    print(f"[weekly_ticket_summary] committed {label}" + (" + pushed" if push else " (no push)"))
    return 0


# ---------------------------------------------------------------------------
# Index refresh
# ---------------------------------------------------------------------------


REPORT_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _extract_report_heading(text: str) -> str:
    """Pull the H1 heading text from a report markdown body."""
    m = REPORT_HEADING_RE.search(text)
    return m.group(1).strip() if m else "(no heading)"


def _extract_report_status(text: str) -> str:
    fm = _parse_frontmatter(text)
    return fm.get("status", "?")


def refresh_readme_index() -> bool:
    """Regenerate the autogen spine-index region in reports/README.md.

    Returns True if the file changed, False if it was already current.
    """
    readme = README_PATH.read_text(errors="replace")
    rows = []
    for f in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        if f.name == "README.md":
            continue
        text = f.read_text(errors="replace")
        heading = _extract_report_heading(text)
        status = _extract_report_status(text)
        rows.append(f"| `{f.name}` | {heading} | {status} |")
    new_block = "\n".join(rows)
    pattern = re.compile(
        re.escape(SENTINEL_START) + r"\n(.*?)\n" + re.escape(SENTINEL_END),
        re.DOTALL,
    )
    replacement = f"{SENTINEL_START}\n{new_block}\n{SENTINEL_END}"
    new_text, n = pattern.subn(replacement, readme)
    if n == 0:
        raise RuntimeError(f"sentinel block not found in {README_PATH}")
    if new_text == readme:
        return False
    README_PATH.write_text(new_text)
    return True


# ---------------------------------------------------------------------------
# Git commit + push (via _git_utils)
# ---------------------------------------------------------------------------


def commit_and_push(paths: list[Path], week_label_str: str, push: bool) -> None:
    """Stage the given paths, HERMES-scan, commit, push.

    Uses the shared `_git_utils` helpers per the cron contract.
    """
    # Mirror the ticket_grooming.py bootstrap so `from tools._git_utils` works
    # under script-mode invocation (per cron).
    if __package__ in (None, ""):
        sys.path.insert(0, str(REPO_ROOT))
    from tools._git_utils import commit_scoped  # type: ignore

    rels = [str(p.relative_to(REPO_ROOT)) for p in paths]
    msg = (
        f"reports: weekly summary for {week_label_str}\n\n"
        f"Generated by tools/weekly_ticket_summary.py per the brief at "
        f"sop/standard/weekly-ticket-summary-generation.md."
    )
    # Delegates to the shared commit_scoped primitive (T-1088 convergence): HERMES
    # message + staged-file guard → sync (fetch+ff-merge, +rebase-fallback) →
    # narrow add of exactly `rels` → leak-guard → commit → push_with_retry. Raises
    # RuntimeError on failure (this caller's contract), preserving the prior behavior.
    ok, detail = commit_scoped(rels, msg, cwd=REPO_ROOT, push=push)
    if not ok:
        raise RuntimeError(detail)


# ---------------------------------------------------------------------------
# Argument parsing + main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--week", type=parse_week_arg, help="target ISO week for --file-request, e.g. 2026-W21")
    p.add_argument("--dry-run", action="store_true", help="print intent, no writes or commits")
    p.add_argument("--force", action="store_true", help="(reserved; no current effect)")
    p.add_argument("--no-push", action="store_true", help="commit but don't push (for --finalize)")

    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--file-request", action="store_true",
        help=(
            "cron path (subscription-safe, default): file a generation-request ticket "
            "for in-session fulfillment (the prompt is regenerated via --print-prompt)."
        ),
    )
    mode_group.add_argument(
        "--finalize", metavar="WEEK",
        help=(
            "in-session fulfillment path: assert reports/<WEEK>.md exists, "
            "refresh README index, commit+push. Run after writing the report in-session."
        ),
    )
    mode_group.add_argument(
        "--print-prompt", metavar="WEEK", dest="print_prompt",
        help=(
            "read-only: regenerate + print the generation prompt for <WEEK> to "
            "stdout (deterministic from that week's done-tickets). The Phase-2 "
            "fulfiller calls this on its own account — no cross-account file needed."
        ),
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    # --finalize: assert report exists, refresh index, commit+push.
    if args.finalize:
        return finalize_report(
            label=args.finalize,
            dry_run=args.dry_run,
            push=not args.no_push,
        )

    # --print-prompt: read-only regenerate the prompt for <WEEK> to stdout. The
    # Phase-2 fulfiller runs this on its own account (no orphan .pending file).
    if args.print_prompt:
        year, week = parse_week_arg(args.print_prompt)
        label = week_label(year, week)
        monday, sunday = iso_week_range(year, week)
        tickets = walk_done_tickets(monday, sunday)
        sop_text = SOP_PATH.read_text(errors="replace")
        exemplar_text = EXEMPLAR_PATH.read_text(errors="replace") if EXEMPLAR_PATH.exists() else ""
        prompt = build_prompt(
            sop_text=sop_text,
            exemplar_text=exemplar_text,
            bundle=build_input_bundle(tickets, monday, sunday),
            target_path_rel=f"reports/{label}.md",
            week_label_str=label,
            monday=monday,
            sunday=sunday,
            ticket_count=len(tickets),
        )
        print(prompt)
        return 0

    # --file-request (explicit or default bare invocation): build prompt + file ticket.
    # Canonical-filer guard (T-1191 dedup): only the canonical account actually
    # files. A stray --file-request on any other account (e.g. a re-enabled timer
    # preset) no-ops cleanly instead of racing to file a duplicate request ticket.
    # --dry-run bypasses (it writes nothing, so a preview on any account is safe).
    current_account = getpass.getuser()
    if current_account != CANONICAL_FILER_ACCOUNT and not args.dry_run:
        print(
            f"[weekly_ticket_summary] not the canonical filer account "
            f"(running as {current_account!r}; canonical={CANONICAL_FILER_ACCOUNT!r}); "
            f"skipping --file-request to avoid a duplicate filing (T-1191). No-op.",
            file=sys.stderr,
        )
        return 0

    if args.week:
        year, week = args.week
    else:
        year, week = prior_iso_week()
    label = week_label(year, week)
    monday, sunday = iso_week_range(year, week)

    print(f"[weekly_ticket_summary] --file-request for {label} ({monday} → {sunday})")
    tickets = walk_done_tickets(monday, sunday)
    print(f"[weekly_ticket_summary] tickets resolved this week: {len(tickets)}")
    return file_request(
        label=label,
        monday=monday,
        sunday=sunday,
        tickets=tickets,
        dry_run=args.dry_run,
    )


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Default-safe contract (T-0259 / T-0825): refuse a flagless invocation.

    A bare run would silently fall through to --file-request and file a ticket (a
    write). The no-prod-default invariant requires an explicit mode: --file-request
    (the cron path), --finalize <week> (commit the report), or --dry-run (preview).
    Returns the exit code (64) to reject the invocation, else None.
    """
    if not (args.file_request or args.finalize or args.print_prompt or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --file-request (cron path), "
            "--finalize <week> (commit the report), or --print-prompt <week> (regenerate "
            "the prompt); add --dry-run to preview.",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
