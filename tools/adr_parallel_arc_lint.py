#!/usr/bin/env python3
"""Detect open tickets whose surface parallels an ADR file being pushed.

**Failure class this guards** (incidents/2026-09-11-t2944-preempts-t3302-disposition):
a session resumes an authoring arc on `architecture/adr/ORG-NNN-*.md` and pushes
without noticing that `tickets/inbox/` or `tickets/active/` holds parallel work
on the SAME ORG-NNN — an operator-desk disposition, an in-flight agent-branch
arc, an open re-audit request. The reverify-state discipline pin fires as an
advisory hook but does not gate on the touched-file surface, so preempts land.

**What it does:** given a range of commits, list ADR files (`architecture/adr/
ORG-NNN-*.md`) touched in the range, then for each ORG-NNN grep
`tickets/{inbox,active}/` for open tickets whose body or frontmatter mentions
that id. Emit one line per (adr_id, open_ticket) pair — the pre-push hook
prints these as a warning (fail-open; the operator judges).

**Severity is a WARN, not a BLOCK.** Many legitimate cross-references exist
(routine `implements: ORG-NNN`, `relates: ORG-NNN`, a normal build/exec child
ticket) — false-positives on a warn are cheap, false-negatives on a block would
be catastrophic. The point is operator awareness, not machine gating.

**Fail posture:** every failure path is fail-open (returns "" and exits 0). A
missing tickets/ dir, unreadable file, glob crash, must never brick a push.

Invoked by ``.githooks/pre-push`` over the push range; standalone-runnable for
audit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


# Match ADR path shape: architecture/adr/ORG-NNN[-optional-suffix].md
ADR_PATH_RE = re.compile(r"architecture/adr/(ORG-\d{3})[-.]")
# Match any ORG-NNN mention (case-sensitive, word-bounded — hyphen counts as word char in \b).
ORG_ID_RE = re.compile(r"\bORG-\d{3}\b")


def _repo_root() -> Path:
    """Best-effort repo root — the working directory the hook runs from."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return Path(out.stdout.strip())
    except Exception:
        return Path.cwd()


def _changed_files_in_range(base_sha: str, tip_sha: str) -> list[str]:
    """Files changed between base_sha (exclusive) and tip_sha (inclusive)."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_sha}..{tip_sha}"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return [line for line in out.stdout.splitlines() if line]
    except Exception:
        return []


def _adr_ids_from_paths(paths: list[str]) -> list[str]:
    """Extract ORG-NNN ids from ADR file paths."""
    ids: list[str] = []
    for p in paths:
        m = ADR_PATH_RE.search(p)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _open_tickets_mentioning(repo: Path, adr_id: str, exclude_tids: set[str]) -> list[tuple[str, str]]:
    """Return [(ticket_id, subject_line)] for open tickets referencing adr_id.

    Scans tickets/inbox/ and tickets/active/. Excludes tickets whose ids are in
    exclude_tids — used to elide tickets being resolved by the same push, so
    resolving-work doesn't self-flag.
    """
    hits: list[tuple[str, str]] = []
    for state_dir in ("inbox", "active"):
        d = repo / "tickets" / state_dir
        if not d.is_dir():
            continue
        for f in sorted(d.glob("T-*.md")):
            tid = f.stem
            if tid in exclude_tids:
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if adr_id not in body:
                continue
            # First `# ` heading line is the subject.
            subject = ""
            for line in body.splitlines():
                if line.startswith("# ") and not line.startswith("# ---"):
                    subject = line[2:].strip()
                    break
            hits.append((tid, subject))
    return hits


def _tids_resolved_in_range(base_sha: str, tip_sha: str) -> set[str]:
    """Tickets moved TO tickets/done/ in the push range — exclude from open-ticket scan.

    A resolve moves a file from tickets/{inbox,active}/ to tickets/done/, so its
    id would still match by-name if a prior commit in the range added it AND a
    later commit resolved it; this returns those ids.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--name-status", "--pretty=format:", f"{base_sha}..{tip_sha}", "--", "tickets/"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except Exception:
        return set()
    tids: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        # A rename (R) into done/, or an add (A) into done/, both surface here.
        for p in parts[1:]:
            if p.startswith("tickets/done/") and p.endswith(".md"):
                stem = Path(p).stem
                if stem.startswith("T-"):
                    tids.add(stem)
    return tids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--range",
        nargs=2,
        metavar=("BASE", "TIP"),
        required=True,
        help="git revision range: BASE..TIP (exclusive..inclusive)",
    )
    args = ap.parse_args()
    base_sha, tip_sha = args.range

    try:
        repo = _repo_root()
        changed = _changed_files_in_range(base_sha, tip_sha)
        adr_ids = _adr_ids_from_paths(changed)
        if not adr_ids:
            return 0
        exclude_tids = _tids_resolved_in_range(base_sha, tip_sha)
        lines: list[str] = []
        for adr_id in adr_ids:
            hits = _open_tickets_mentioning(repo, adr_id, exclude_tids)
            if hits:
                lines.append(f"{adr_id}: {len(hits)} open ticket(s) reference this ADR:")
                for tid, subject in hits:
                    truncated = subject[:100] + ("…" if len(subject) > 100 else "")
                    lines.append(f"    {tid} — {truncated}")
        if lines:
            print("\n".join(lines))
    except Exception:
        # Fail-open on any anomaly. A guard that bricks pushes defeats itself.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
