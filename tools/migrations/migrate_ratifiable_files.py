#!/usr/bin/env python3
"""One-shot migration: ratifiable_path → ratifiable_files (T-0291).

Converts 13 ratification tickets (T-0266..T-0278) from the singular
`ratifiable_path: <pantheon-config-rel-path>` field to the plural
`ratifiable_files: [<repo>:<path>]` list field.

Idempotent: skips tickets that already have `ratifiable_files:` and
no longer have `ratifiable_path:`.

Run from repo root (or worktree root):
    python3 tools/migrations/migrate_ratifiable_files.py [--dry-run]

Writes files in-place. Does NOT commit or push — caller commits separately.

Mapping (repo:path values per T-0291 brief):
    T-0266 → ratifiable_files: [pantheon-config:sop/remediation/locked-answer-deviation-doubled-down.md]
    T-0267 → ratifiable_files: [pantheon-config:sop/remediation/orchestrator-tier-skip-on-approval.md]
    T-0268 → ratifiable_files: [pantheon-config:sop/remediation/routing-mis-routing-to-team-lead.md]
    T-0269 → ratifiable_files: [pantheon-config:sop/remediation/jumps-ahead-without-brief.md]
    T-0270 → ratifiable_files: [pantheon-config:sop/standard/handoff-restart-runbook.md]
    T-0271 → ratifiable_files: [pantheon-config:sop/standard/plan-first-dispatch.md]
    T-0272 → ratifiable_files: [pantheon-config:sop/standard/cross-account-ops-discipline.md]
    T-0273 → ratifiable_files: [pantheon-config:sop/standard/team-orchestrator-dispatch-checklist.md]
    T-0274 → ratifiable_files: [pantheon-config:sop/standard/auto-file-side-observations.md]
    T-0275 → ratifiable_files: [pantheon-config:adr/ORG-012-information-inflow-bubble-up-methodology.md]
    T-0276 → ratifiable_files: [pantheon-config:lessons/orchestrator-tier-substrate-verification.md]
    T-0277 → ratifiable_files: [revenue-project:adr/MX-001-token-cost-asymmetry.md]
    T-0278 → ratifiable_files: [pantheon-config:rulebook/directives.md, pantheon-config:rulebook/policies/trust-posture.md]

HERMES byte-sequence discipline: this script's source must not contain
the contiguous unescaped trigger sequence. Any reference to it uses
the concatenated form "HERME" + "S.md".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"
LANES = ("inbox", "active", "done")

# Mapping from ticket ID to list of repo:path entries.
# T-0277 + T-0278 had no ratifiable_path; field is being added fresh.
MIGRATION_MAP: dict[str, list[str]] = {
    "T-0266": ["pantheon-config:sop/remediation/locked-answer-deviation-doubled-down.md"],
    "T-0267": ["pantheon-config:sop/remediation/orchestrator-tier-skip-on-approval.md"],
    "T-0268": ["pantheon-config:sop/remediation/routing-mis-routing-to-team-lead.md"],
    "T-0269": ["pantheon-config:sop/remediation/jumps-ahead-without-brief.md"],
    "T-0270": ["pantheon-config:sop/standard/handoff-restart-runbook.md"],
    "T-0271": ["pantheon-config:sop/standard/plan-first-dispatch.md"],
    "T-0272": ["pantheon-config:sop/standard/cross-account-ops-discipline.md"],
    "T-0273": ["pantheon-config:sop/standard/team-orchestrator-dispatch-checklist.md"],
    "T-0274": ["pantheon-config:sop/standard/auto-file-side-observations.md"],
    "T-0275": ["pantheon-config:adr/ORG-012-information-inflow-bubble-up-methodology.md"],
    "T-0276": ["pantheon-config:lessons/orchestrator-tier-substrate-verification.md"],
    "T-0277": ["revenue-project:adr/MX-001-token-cost-asymmetry.md"],
    "T-0278": [
        "pantheon-config:rulebook/directives.md",
        "pantheon-config:rulebook/policies/trust-posture.md",
    ],
}

# Fields that precede ratifiable_files in FRONT_MATTER_FIELD_ORDER.
# Used to find the right insertion point for tickets that have neither field.
_INSERT_AFTER = "not_before"


def _render_ratifiable_files(entries: list[str]) -> str:
    return "ratifiable_files: [" + ", ".join(entries) + "]"


def _find_ticket(tid: str) -> Path | None:
    for lane in LANES:
        p = TICKETS_ROOT / lane / f"{tid}.md"
        if p.exists():
            return p
    return None


def _migrate_text(tid: str, text: str, entries: list[str]) -> tuple[str, str]:
    """Return (new_text, action) where action is 'swap', 'insert', or 'skip'."""
    lines = text.splitlines(keepends=True)
    new_field_line = _render_ratifiable_files(entries) + "\n"

    # Check idempotency: already migrated?
    has_new = any(l.startswith("ratifiable_files:") for l in lines)
    has_old = any(l.startswith("ratifiable_path:") for l in lines)
    if has_new and not has_old:
        return text, "skip"

    if has_old:
        # Swap ratifiable_path line → ratifiable_files line
        new_lines = []
        for line in lines:
            if line.startswith("ratifiable_path:"):
                new_lines.append(new_field_line)
            else:
                new_lines.append(line)
        return "".join(new_lines), "swap"

    # T-0277 / T-0278: no existing field — insert after _INSERT_AFTER line
    # (or after the closing --- if not found, which shouldn't happen)
    new_lines = []
    inserted = False
    in_front_matter = False
    fm_open = False
    for line in lines:
        new_lines.append(line)
        stripped = line.rstrip("\n")
        if not fm_open and stripped == "---":
            fm_open = True
            in_front_matter = True
            continue
        if in_front_matter and stripped == "---":
            # Closing delimiter — insert before it if not inserted yet
            if not inserted:
                new_lines.insert(-1, new_field_line)
                inserted = True
            in_front_matter = False
            continue
        if in_front_matter and stripped.startswith(_INSERT_AFTER + ":") and not inserted:
            new_lines.append(new_field_line)
            inserted = True

    return "".join(new_lines), "insert"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate ratifiable_path → ratifiable_files")
    parser.add_argument("--dry-run", action="store_true", help="print diffs without writing")
    args = parser.parse_args()

    results: list[tuple[str, str, str]] = []  # (tid, path, action)
    errors: list[str] = []

    for tid, entries in MIGRATION_MAP.items():
        path = _find_ticket(tid)
        if path is None:
            errors.append(f"  {tid}: NOT FOUND in any lane")
            continue
        text = path.read_text(encoding="utf-8")
        new_text, action = _migrate_text(tid, text, entries)
        if action == "skip":
            results.append((tid, str(path.relative_to(REPO_ROOT)), "skip (already migrated)"))
            continue
        if args.dry_run:
            results.append((tid, str(path.relative_to(REPO_ROOT)), f"DRY-RUN:{action}"))
            if action != "skip":
                # Show the ratifiable_files line that would be written
                for line in new_text.splitlines():
                    if "ratifiable" in line:
                        print(f"  {tid}: would write → {line.strip()}")
        else:
            path.write_text(new_text, encoding="utf-8")
            results.append((tid, str(path.relative_to(REPO_ROOT)), action))

    print("\nMigration results:")
    for tid, rel, action in results:
        print(f"  {tid:7s}  {action:30s}  {rel}")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(e)
        return 1

    not_skipped = [r for r in results if "skip" not in r[2] and "DRY-RUN" not in r[2]]
    dry_run_count = sum(1 for r in results if "DRY-RUN" in r[2])
    if args.dry_run:
        print(f"\nDry run: {dry_run_count} ticket(s) would be modified.")
    else:
        print(f"\nDone: {len(not_skipped)} ticket(s) modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
