#!/usr/bin/env python3
"""GEPA reviewer-eval candidate extractor (T-2809 Phase 1).

Read-only scan of `tickets/done/*.md`: selects tickets whose transition log
mentions REBOUND, and emits one JSON object per candidate ticket to a
caller-supplied `--out` file (JSONL, one record per line). This tool
surfaces faithfully; it does not curate — the curated eval set is written
separately by the orchestrator.

Usage:
    python3 tools/gepa_eval_extract.py --out <path.jsonl> [--limit N]

Record shape (JSONL, one object per line):
    {
        "ticket_id": str,
        "title": str,
        "labels": list[str],
        "requester": str,
        "target": str,
        "rebound_notes": list[str],       # verbatim note-bodies containing REBOUND
        "sha_annotations": list[str],     # @<sha> anchors found within rebound_notes
        "resolved_note": str | None,      # verbatim note-body of the last "resolved" bullet
        "approve_mentions": int,          # count of notes (full log) containing uppercase APPROVE
        "source_path": str,               # repo-root-relative path, e.g. "tickets/done/T-0130.md"
    }

Transition-log note-body extraction is verbatim (no whitespace mangling):
a bullet's note-body is the text following `[actor] ` through the end of
that bullet's span, where the span continues across wrapped/blank lines
until either the next bullet-start line or the next markdown header line
(`^#...`), whichever comes first. This intentionally stops a bullet's note
from swallowing an unrelated section appended later in the file (a real
pattern in this corpus — see DECISIONS in the T-2809 report).

REBOUND / APPROVE matching is case-sensitive (uppercase literal only) —
both are formal verdict keywords in this org's convention; lowercase
mentions in this corpus are generic prose about the concepts, not real
verdict events.

--out path safety: refused if the resolved path is inside the repo
(tracked working tree) UNLESS it is also under /tmp — /tmp is always
permitted regardless of where the repo itself is checked out (worktrees
staged under /tmp are a normal deployment shape for this org).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ticket  # noqa: E402 — after sys.path bootstrap.

TRANSITION_LOG_RE = re.compile(r"^## Transition log\s*$", re.MULTILINE)
BULLET_START_RE = re.compile(r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z \[[^\]]+\] ")
HEADER_RE = re.compile(r"^#")
SHA_ANNOTATION_RE = re.compile(r"@[0-9a-f]{7,40}\b")


def split_transition_log_bullets(body: str) -> list[str]:
    """Return the verbatim note-body text of each transition-log bullet, in file order.

    Locates the LAST line-anchored `## Transition log` header in *body*
    (tickets have exactly one; defensive against a stray mention elsewhere)
    and scans from there to end-of-text. Each bullet is a line matching
    `- TIMESTAMP [actor] `; its note-body is everything after that prefix,
    continuing across subsequent lines until the next bullet-start line or
    the next markdown header line, whichever comes first. Trailing blank
    lines in the span are stripped; embedded newlines / blank-line
    paragraph breaks within the span are preserved verbatim. Returns []
    if no `## Transition log` header is found.
    """
    matches = list(TRANSITION_LOG_RE.finditer(body))
    if not matches:
        return []
    lines = body[matches[-1].end():].split("\n")

    notes: list[str] = []
    current: list[str] | None = None

    for line in lines:
        m = BULLET_START_RE.match(line)
        if m:
            if current is not None:
                notes.append(_finalize_note(current))
            current = [line[m.end():]]
            continue
        if current is not None and HEADER_RE.match(line):
            notes.append(_finalize_note(current))
            current = None
            continue
        if current is not None:
            current.append(line)

    if current is not None:
        notes.append(_finalize_note(current))

    return notes


def _finalize_note(lines: list[str]) -> str:
    """Join a bullet's collected lines and strip only trailing blank lines."""
    return "\n".join(lines).rstrip("\n")


def extract_rebound_notes(notes: list[str]) -> list[str]:
    """Return the subset of *notes* containing the literal uppercase substring REBOUND."""
    return [n for n in notes if "REBOUND" in n]


def extract_sha_annotations(rebound_notes: list[str]) -> list[str]:
    """Return all `@<sha>` anchors found within *rebound_notes*, in order, no dedup.

    An anchor is `@` followed by 7-40 lowercase hex characters.
    """
    out: list[str] = []
    for note in rebound_notes:
        out.extend(SHA_ANNOTATION_RE.findall(note))
    return out


def extract_resolved_note(notes: list[str]) -> str | None:
    """Return the note-body of the LAST bullet in *notes* whose text starts with "resolved".

    *notes* is the full bullet list (not just rebound_notes) — a ticket can
    be reopened and re-resolved, and the last resolution is canonical for
    a tickets/done file. Returns None if no such bullet exists.
    """
    resolved = [n for n in notes if n.startswith("resolved")]
    return resolved[-1] if resolved else None


def extract_approve_mentions(notes: list[str]) -> int:
    """Return the count of *notes* (full log, not just rebound_notes) containing uppercase APPROVE.

    Counts matching notes, not raw substring occurrences — a note with two
    APPROVE mentions still counts once.
    """
    return sum(1 for n in notes if "APPROVE" in n)


def is_refused_out_path(out_path: Path, repo_root: Path) -> bool:
    """Return True if *out_path* must be refused as a --out target.

    Refused iff the resolved path is inside *repo_root* AND not under
    /tmp. /tmp is an unconditional carve-out regardless of where
    *repo_root* itself is checked out (a repo worktree staged under /tmp
    must still accept /tmp-rooted --out targets).
    """
    resolved = out_path.resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        return False
    resolved_root = repo_root.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def scan_ticket_file(path: Path, repo_root: Path) -> dict | None:
    """Parse one tickets/done/*.md file and return a candidate record, or None.

    Returns None when the file is not a REBOUND candidate (no REBOUND
    mention in its transition log), or when the file's frontmatter cannot
    be parsed (malformed/empty/missing-required-field) — the latter is
    tolerated: caught, a WARN line is printed to stderr, and the file is
    skipped rather than raising.
    """
    try:
        text = path.read_text(encoding="utf-8")
        fields, body = ticket.parse_front_matter(text)
    except ticket.TicketError as exc:
        print(f"[gepa_eval_extract] WARN: skipping {path.name}: {exc}", file=sys.stderr)
        return None

    notes = split_transition_log_bullets(body)
    rebound_notes = extract_rebound_notes(notes)
    if not rebound_notes:
        return None

    title = ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    return {
        "ticket_id": fields["id"],
        "title": title,
        "labels": fields.get("labels") or [],
        "requester": fields.get("requester"),
        "target": fields.get("target"),
        "rebound_notes": rebound_notes,
        "sha_annotations": extract_sha_annotations(rebound_notes),
        "resolved_note": extract_resolved_note(notes),
        "approve_mentions": extract_approve_mentions(notes),
        "source_path": str(path.resolve().relative_to(repo_root.resolve())),
    }


def scan_tickets_done(tickets_done_dir: Path, repo_root: Path, limit: int | None) -> list[dict]:
    """Scan *tickets_done_dir* for candidate records, filename-sorted.

    *limit*, when not None, caps the number of MATCHED candidate records
    returned (not the number of files scanned) — non-matching and
    tolerated-malformed files do not consume the limit budget.
    """
    records: list[dict] = []
    for path in sorted(tickets_done_dir.glob("*.md")):
        record = scan_ticket_file(path, repo_root)
        if record is None:
            continue
        records.append(record)
        if limit is not None and len(records) >= limit:
            break
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract REBOUND-mention ticket candidates from tickets/done/ "
            "for GEPA reviewer-eval curation (T-2809 Phase 1)."
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="JSONL output path")
    parser.add_argument("--limit", type=int, default=None, help="cap on matched candidates emitted")
    args = parser.parse_args(argv)

    tickets_done_dir = ticket.TICKETS_ROOT / "done"
    repo_root = ticket.TICKETS_ROOT.parent

    if is_refused_out_path(args.out, repo_root):
        print(
            f"[gepa_eval_extract] REFUSED: --out {args.out} resolves inside the repo "
            f"({repo_root}) and is not under /tmp. The curated eval set is written by "
            "the orchestrator, not this extraction tool.",
            file=sys.stderr,
        )
        return 2

    total_scanned = sorted(tickets_done_dir.glob("*.md"))
    records = scan_tickets_done(tickets_done_dir, repo_root, args.limit)
    matched_ids = {r["ticket_id"] for r in records}
    skipped = 0
    for path in total_scanned:
        try:
            ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        except ticket.TicketError:
            skipped += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")

    print(
        f"[gepa_eval_extract] wrote {len(records)} candidate(s) to {args.out} "
        f"({skipped} malformed file(s) skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
