#!/usr/bin/env python3
"""Pantheon ticket-state ↔ ledger consistency sweep.

Detects drift between ledger entries and ticket-file state on disk.
Auto-fixes forward-sweep matches with unambiguous resolution evidence;
surfaces reverse / target-operator candidates to operator.

Three sweeps:

1. Forward — ledger says resolved (action matches resolution-tag regex)
   but ticket file is in inbox/ / active/. Auto-fix:
   subprocess `tools/ticket.py claim + resolve` chain with
   `[sweep] sync to ledger entry <id>` rationale. Ambiguous matches
   (multiple ledger entries OR contested) surface to stderr.

2. Reverse — ticket file in done/ but no ledger entry references it
   with a resolution-shape action. Surface only; do NOT auto-author
   ledger entries (P4 append-only audit-chain integrity).

3. Target-operator moot — ticket with target=operator in inbox/
   alongside a recent (≤30d window) dispositional ledger entry that
   may have made it moot. Surface only; the operator ratifies the close.

Per T-0149. Filed per the operator directive 2026-05-09 msg_776.
Amended 2026-05-19 per ORG-019: blocked/ state retired; stale-blocked
sweep removed.

CLI:
    python3 tools/ticket_state_sweep.py            # normal fire
    python3 tools/ticket_state_sweep.py --dry-run  # scan + print, no auto-fix

Exit codes:
    0 = clean (no surfaces, no errors)
    1 = surfaces present (forward-ambiguous OR reverse OR moot) OR errors
    2 = transient errors only (no surfaces, but auto-fix subprocess failures)

If both surfaces and errors occur: exit 1 (surfaces take precedence).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ledger_shard import entry_ts, iter_ledger_entry_files  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"
LEDGER_DIR = REPO_ROOT / "ledger"

# Recency window for target-operator moot scan only. Forward / reverse
# sweeps scan the full ledger.
OPERATOR_MOOT_RECENCY_DAYS = 30

# Resolution-tag regex — case-insensitive, matched against the JSON
# `action` field ONLY (not rationale, not links). Five patterns per
# T-0149 §Auto-fix-policy locked-answer.
_RESOLUTION_RE = re.compile(
    r"(?:_resolved_|complete_t\d{4}_|t\d{4}_resolved_|resolve_t\d{4}_|t\d{4}_complete_)",
    re.IGNORECASE,
)

# Ticket-id pattern — case-insensitive; first-match-wins; 2+ distinct
# normalized ids in a single action = ambiguous.
_TICKET_ID_RE = re.compile(r"t(\d{4})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ForwardMismatch:
    """Ticket whose ledger entry says resolved but file is not in done/."""

    ticket_id: str  # e.g. "T-0141"
    current_lane: str  # "inbox" | "active"
    ledger_entry_id: str  # e.g. "2026-05-09T15:10:00Z-001"


@dataclass
class SweepReport:
    forward_mismatches: list[ForwardMismatch] = field(default_factory=list)
    reverse_gaps: list[str] = field(default_factory=list)  # ticket-ids
    target_operator_candidates: list[str] = field(default_factory=list)  # ticket-ids


# ---------------------------------------------------------------------------
# Loaders + parsers
# ---------------------------------------------------------------------------


def _parse_ticket_front_matter(path: Path) -> dict:
    """Parse YAML-ish front-matter from a ticket .md file.

    Minimal parser: handles single-line key: value and `key: [a, b, c]`
    list values. Front-matter is bracketed by `---` lines at top of file.
    Fail-open on malformed: returns whatever parsed cleanly.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    front: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Parse `[a, b, c]` lists naively
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                front[key] = []
            else:
                front[key] = [item.strip() for item in inner.split(",")]
        else:
            front[key] = value
    return front


def _load_tickets(repo_root: Path) -> dict[str, dict]:
    """Walk tickets/{inbox,active,done}/ and return id → metadata.

    Each value carries: lane, target, depends_on, requester, created,
    resolved (optional), path. Fail-open on per-ticket errors.
    """
    tickets_root = repo_root / "tickets"
    tickets: dict[str, dict] = {}
    for lane in ("inbox", "active", "done"):
        lane_dir = tickets_root / lane
        if not lane_dir.is_dir():
            continue
        for ticket_path in lane_dir.glob("T-*.md"):
            try:
                fm = _parse_ticket_front_matter(ticket_path)
                ticket_id = fm.get("id", "").strip()
                if not ticket_id:
                    continue
                tickets[ticket_id] = {
                    "lane": lane,
                    "target": fm.get("target", "").strip(),
                    "depends_on": fm.get("depends_on", []) or [],
                    "requester": fm.get("requester", "").strip(),
                    "created": fm.get("created", "").strip(),
                    "resolved": fm.get("resolved", "").strip(),
                    "path": ticket_path,
                }
            except Exception as exc:  # fail-open per-ticket
                print(
                    f"ticket_state_sweep: WARNING — failed to parse {ticket_path}: {exc}",
                    file=sys.stderr,
                )
    return tickets


def _load_ledger_entries(repo_root: Path) -> list[dict]:
    """Load all ledger entries from the ledger entry-file union.

    Routes through ledger_shard.iter_ledger_entry_files (T-0148) so it sees
    both the legacy flat `2026.jsonl` (pre-migration) and the per-week
    subfolder shards `ledger/<seg>/<seg>.jsonl` (post-migration), with the
    `*.archive*` backup excluded. The previous flat `glob("*.jsonl")` did NOT
    descend into subfolders, so it would silently miss every shard.

    Fail-open on missing or malformed files.
    """
    ledger_dir = repo_root / "ledger"
    if not ledger_dir.is_dir():
        return []
    entries: list[dict] = []
    for jsonl_path in iter_ledger_entry_files(ledger_dir):
        try:
            text = jsonl_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"ticket_state_sweep: WARNING — failed to read {jsonl_path}: {exc}",
                file=sys.stderr,
            )
            continue
        for line_num, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(
                    f"ticket_state_sweep: WARNING — bad JSONL at "
                    f"{jsonl_path}:{line_num}: {exc}",
                    file=sys.stderr,
                )
    return entries


# ---------------------------------------------------------------------------
# Action / id parsing
# ---------------------------------------------------------------------------


def _is_resolution_action(action: str) -> bool:
    """True iff action field matches the resolution-tag regex."""
    if not action:
        return False
    return bool(_RESOLUTION_RE.search(action))


def _extract_ticket_id_from_action(action: str) -> tuple[str | None, bool]:
    """Extract ticket-id from action field.

    Returns (id_or_None, ambiguous):
      - (T-NNNN, False): single distinct ticket-id found
      - (None, False): no ticket-id in action
      - (None, True): 2+ distinct ticket-ids found (ambiguous)

    Ids are case-insensitive in source, normalized to T-{int:04d}.
    """
    if not action:
        return (None, False)
    matches = _TICKET_ID_RE.findall(action)
    if not matches:
        return (None, False)
    # Normalize each match to T-NNNN (uppercase, zero-padded)
    distinct = {f"T-{int(num):04d}" for num in matches}
    if len(distinct) > 1:
        return (None, True)
    return (next(iter(distinct)), False)


# ---------------------------------------------------------------------------
# Scans (4)
# ---------------------------------------------------------------------------


def _scan_forward(
    tickets_by_id: dict[str, dict],
    ledger_entries: list[dict],
) -> tuple[list[ForwardMismatch], list[str]]:
    """Forward sweep: ledger says resolved but ticket file not in done/.

    Returns (mismatches, ambiguous_messages).
    `ambiguous_messages` are pre-formatted strings for stderr surfacing
    when a ledger entry has 2+ distinct ticket-ids (can't auto-resolve).
    """
    # First pass: collect ledger resolution entries grouped by ticket-id.
    # Multiple entries for same id → also ambiguous (per T-0149 §Auto-fix
    # "Forward sweep ambiguous (multiple ledger entries...)").
    by_ticket: dict[str, list[dict]] = {}
    ambiguous_messages: list[str] = []
    for entry in ledger_entries:
        action = entry.get("action", "") or ""
        if not _is_resolution_action(action):
            continue
        ticket_id, is_ambiguous = _extract_ticket_id_from_action(action)
        if is_ambiguous:
            entry_id = entry.get("id", "<unknown>")
            ambiguous_messages.append(
                f"forward-sweep: ledger entry {entry_id} has multiple ticket-ids "
                f"in action; no auto-fix attempted (action={action!r})"
            )
            continue
        if ticket_id is None:
            continue
        by_ticket.setdefault(ticket_id, []).append(entry)

    # Second pass: for each ticket-id with resolution entries, check
    # current lane. If not in done/, build mismatch (or surface multi-entry
    # ambiguity).
    mismatches: list[ForwardMismatch] = []
    for ticket_id, entries in by_ticket.items():
        ticket = tickets_by_id.get(ticket_id)
        if ticket is None:
            # Ticket file doesn't exist on disk; nothing to fix
            continue
        if ticket["lane"] == "done":
            continue
        if len(entries) > 1:
            entry_ids = ", ".join(e.get("id", "?") for e in entries)
            ambiguous_messages.append(
                f"forward-sweep: {ticket_id} has multiple resolution-shape "
                f"ledger entries ({entry_ids}); no auto-fix attempted"
            )
            continue
        entry_id = entries[0].get("id", "<unknown>")
        mismatches.append(
            ForwardMismatch(
                ticket_id=ticket_id,
                current_lane=ticket["lane"],
                ledger_entry_id=entry_id,
            )
        )
    return (mismatches, ambiguous_messages)


def _scan_reverse(
    tickets_by_id: dict[str, dict],
    ledger_entries: list[dict],
) -> list[str]:
    """Reverse sweep: ticket in done/ but no resolution-shape ledger entry.

    Surface only — does NOT auto-author ledger entries (P4 append-only +
    audit-chain integrity).
    """
    # Build set of ticket-ids referenced in resolution-shape ledger actions.
    resolved_via_ledger: set[str] = set()
    for entry in ledger_entries:
        action = entry.get("action", "") or ""
        if not _is_resolution_action(action):
            continue
        ticket_id, is_ambiguous = _extract_ticket_id_from_action(action)
        if is_ambiguous:
            # Ambiguous entries reference multiple ids — for reverse-sweep
            # purposes, treat all matched ids as covered (don't false-positive).
            for raw in _TICKET_ID_RE.findall(action):
                resolved_via_ledger.add(f"T-{int(raw):04d}")
            continue
        if ticket_id is not None:
            resolved_via_ledger.add(ticket_id)

    gaps: list[str] = []
    for ticket_id, ticket in tickets_by_id.items():
        if ticket["lane"] != "done":
            continue
        if ticket_id not in resolved_via_ledger:
            gaps.append(ticket_id)
    return sorted(gaps)


def _scan_target_operator(
    tickets_by_id: dict[str, dict],
    ledger_entries: list[dict],
    now: datetime | None = None,
) -> list[str]:
    """Target-operator moot sweep: open operator-target tickets with recent
    (≤OPERATOR_MOOT_RECENCY_DAYS) dispositional ledger entries that may
    have made them moot.

    Recency window applied to TARGET-OPERATOR ONLY (forward / reverse
    are full-ledger). Surface only; the operator ratifies the close.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=OPERATOR_MOOT_RECENCY_DAYS)

    # Build set of ticket-ids referenced in ANY recent ledger action
    # (not just resolution-shape — disposition entries may use other
    # action shapes like `ratify_*` or `close_*`).
    recent_referenced: set[str] = set()
    for entry in ledger_entries:
        ts_str = entry_ts(entry) or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        action = entry.get("action", "") or ""
        for raw in _TICKET_ID_RE.findall(action):
            recent_referenced.add(f"T-{int(raw):04d}")
        # Also check links field for ticket references
        links = entry.get("links") or []
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict):
                    link_id = link.get("id") or link.get("ref") or ""
                    if isinstance(link_id, str):
                        for raw in _TICKET_ID_RE.findall(link_id):
                            recent_referenced.add(f"T-{int(raw):04d}")

    candidates: list[str] = []
    for ticket_id, ticket in tickets_by_id.items():
        if ticket["lane"] not in ("inbox", "active"):
            continue
        if ticket.get("target", "").lower() != "operator":
            continue
        if ticket_id in recent_referenced:
            candidates.append(ticket_id)
    return sorted(candidates)


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


def _auto_fix_forward_mismatch(
    mismatch: ForwardMismatch,
    repo_root: Path,
    dry_run: bool,
) -> bool:
    """Auto-fix a forward-sweep mismatch via subprocess ticket.py.

    State-prereq chain:
      inbox  → ticket.py claim → ticket.py resolve
      active → ticket.py resolve only

    Returns True on success. Raises on subprocess non-zero exit.
    """
    rationale = f"[sweep] sync to ledger entry {mismatch.ledger_entry_id}"

    if dry_run:
        print(
            f"[dry-run] would auto-fix {mismatch.ticket_id} "
            f"(lane={mismatch.current_lane}): claim + resolve",
            file=sys.stderr,
        )
        return True

    if mismatch.current_lane == "inbox":
        result = subprocess.run(
            ["python3", "tools/ticket.py", "claim", mismatch.ticket_id],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claim failed: {result.stderr.strip()}")

    # Now in active state; resolve.
    result = subprocess.run(
        ["python3", "tools/ticket.py", "resolve",
         mismatch.ticket_id, "--with", rationale],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"resolve failed: {result.stderr.strip()}")
    return True


# ---------------------------------------------------------------------------
# Public sweep entry
# ---------------------------------------------------------------------------


def sweep(
    repo_root: Path,
    dry_run: bool = False,
) -> tuple[SweepReport, list[str], int]:
    """Run all four sweeps; auto-fix forward; surface others.

    Returns (report, ambiguous_messages, error_count).

    error_count = number of forward auto-fix subprocess failures
    (transient; failed individually but didn't halt the loop).
    """
    tickets_by_id = _load_tickets(repo_root)
    ledger_entries = _load_ledger_entries(repo_root)

    forward_mismatches, ambiguous_messages = _scan_forward(
        tickets_by_id, ledger_entries
    )
    reverse_gaps = _scan_reverse(tickets_by_id, ledger_entries)
    target_operator_candidates = _scan_target_operator(
        tickets_by_id, ledger_entries
    )

    # Auto-fix forward mismatches (per locked-answer-3); fail-open per
    # locked-answer-16: broad except continues to next ticket.
    error_count = 0
    fixed: list[ForwardMismatch] = []
    for mismatch in forward_mismatches:
        try:
            _auto_fix_forward_mismatch(mismatch, repo_root, dry_run)
            fixed.append(mismatch)
        except Exception as exc:  # fail-open per T-0118/T-0120 pattern
            print(
                f"{mismatch.ticket_id}: auto-fix failed "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            error_count += 1

    report = SweepReport(
        forward_mismatches=fixed,
        reverse_gaps=reverse_gaps,
        target_operator_candidates=target_operator_candidates,
    )
    return (report, ambiguous_messages, error_count)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_report(report: SweepReport, dry_run: bool = False) -> None:
    """Print a structured summary to stdout."""
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}ticket_state_sweep summary:")
    print(f"  forward auto-fixed: {len(report.forward_mismatches)}")
    for m in report.forward_mismatches:
        print(f"    - {m.ticket_id} (lane={m.current_lane}, "
              f"ledger={m.ledger_entry_id})")
    print(f"  reverse gaps (done without resolution-ledger entry): "
          f"{len(report.reverse_gaps)}")
    for tid in report.reverse_gaps:
        print(f"    - {tid}")
    print(f"  target-operator moot candidates (≤{OPERATOR_MOOT_RECENCY_DAYS}d "
          f"window): {len(report.target_operator_candidates)}")
    for tid in report.target_operator_candidates:
        print(f"    - {tid}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pantheon ticket-state ↔ ledger consistency sweep."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run; do not invoke ticket.py auto-fix.",
    )
    args = parser.parse_args(argv)

    report, ambiguous_messages, error_count = sweep(
        REPO_ROOT, dry_run=args.dry_run
    )

    # Surface ambiguous messages to stderr.
    for msg in ambiguous_messages:
        print(msg, file=sys.stderr)

    # Print report to stdout.
    _print_report(report, dry_run=args.dry_run)

    # Exit code:
    # 0 = clean (no surfaces, no errors)
    # 1 = surfaces present (any) OR mixed surfaces+errors
    # 2 = transient errors only (no surfaces)
    has_surfaces = (
        bool(ambiguous_messages)
        or bool(report.reverse_gaps)
        or bool(report.target_operator_candidates)
    )
    if has_surfaces:
        return 1
    if error_count > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
