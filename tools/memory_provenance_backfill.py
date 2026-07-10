#!/usr/bin/env python3
"""tools/memory_provenance_backfill.py — add ORG-035 provenance fields to memory pins (T-1131).

memory_lint WARNs on pins missing the ORG-035 provenance/freshness fields
(owner, source, created, updated, verified, scope). This backfills the
DERIVABLE ones safely and idempotently:

  owner    — uniform (the agent that owns the store; --owner, default 'orchestrator')
  scope    — uniform (--scope, default 'ho')
  created  — file mtime date (YYYY-MM-DD). The memory dir is NOT git-tracked, so
             mtime is the best-available signal; this is an approximation, noted.
  updated  — file mtime date (same caveat)
  source   — written ONLY when encoded in the FILENAME (`..._msg1271.md`,
             `..._T-0444.md`) — an author-intentional provenance choice. Body /
             frontmatter scanning grabs incidental cross-references (e.g. a pin
             whose body mentions an unrelated ticket), so for pins without a
             filename-encoded id, `source` is LEFT for the per-pin audit. Writing
             a wrong source is worse than a missing one.

DELIBERATELY OMITS `verified` (and non-filename `source`). Stamping verified=<today> across the corpus
without re-reading + confirming each fact is a false-freshness claim (cf. the
health-claims-need-comprehensive-checks pin). `verified` is set by a separate,
genuine per-pin audit — NOT by this mechanical sweep. So memory_lint will still
WARN "missing provenance fields: verified" after this runs; that is correct and
honest. This tool shrinks each WARN, it does not pretend to zero the surface.

Fields are inserted as TOP-LEVEL frontmatter keys (memory_lint counts a field
present in either the top level or the nested `metadata:` map, and top-level is
uniform across both legacy pin shapes). Bodies are never touched. Idempotent:
a key already present (top-level) is left as-is. Reversible.

  python3 tools/memory_provenance_backfill.py <memory_dir>          # dry-run (default)
  python3 tools/memory_provenance_backfill.py <memory_dir> --apply  # write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Always-safe uniform/derivable fields. `source` is handled separately and only
# written when it can be read from the FILENAME (author-intentional) — body/ticket
# scanning grabs incidental cross-references and is unreliable as provenance, so it
# is left for the deliberate per-pin audit (alongside `verified`).
SAFE_FIELDS = ("owner", "scope", "created", "updated")
TICKET_RE = re.compile(r"T-\d{3,4}\b")
MSG_RE = re.compile(r"msg[_-]?(\d{3,5})\b", re.IGNORECASE)
SESSION_RE = re.compile(r"originSessionId:\s*([A-Za-z0-9-]{8,})")


def split_frontmatter_lines(text: str) -> tuple[list[str], list[str], str] | None:
    """Return (pre, fm_lines, rest) where fm_lines are the lines BETWEEN the --- fences.

    pre is everything up to and including the opening fence line; rest is the
    closing fence + body. None if no well-formed frontmatter.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return lines[:1], lines[1:i], "".join(lines[i:])
    return None


def top_level_keys(fm_lines: list[str]) -> set[str]:
    """Keys defined at the top level of the frontmatter (non-indented `key:`)."""
    keys: set[str] = set()
    for ln in fm_lines:
        if ln[:1] in (" ", "\t"):
            continue
        m = re.match(r"([A-Za-z0-9_-]+):", ln)
        if m:
            keys.add(m.group(1))
    return keys


def derive_source_from_filename(filename: str) -> str | None:
    """Source token IFF encoded in the filename (author-intentional). Else None.

    Filename-encoded ids (`..._msg1271.md`, `..._T-0444.md`) are a deliberate
    provenance choice by the pin author; body/frontmatter scanning grabs incidental
    cross-references, so it is NOT used for auto-write — that's the audit's job.
    """
    t = TICKET_RE.search(filename)
    if t:
        return t.group(0)
    m = MSG_RE.search(filename)
    if m:
        return f"msg{m.group(1)}"
    return None


def build_insert_lines(present: set[str], values: dict[str, str]) -> list[str]:
    """Render `key: value` lines for safe fields not already present (stable order)."""
    out = []
    for k in SAFE_FIELDS:
        if k not in present:
            out.append(f"{k}: {values[k]}\n")
    # source only if filename-derivable AND not already present.
    if "source" not in present and values.get("source") is not None:
        out.append(f"source: {values['source']}\n")
    return out


def backfill_text(text: str, *, mtime_date: str, owner: str, scope: str,
                  filename: str) -> str | None:
    """Return the new file text with derivable provenance inserted, or None if no-op.

    Pure (mtime_date passed in for testability). Inserts before the top-level
    `metadata:` line if present, else at the end of the frontmatter block.
    """
    parts = split_frontmatter_lines(text)
    if parts is None:
        return None
    pre, fm_lines, rest = parts
    present = top_level_keys(fm_lines)
    values = {
        "owner": owner,
        "scope": scope,
        "created": mtime_date,
        "updated": mtime_date,
        "source": derive_source_from_filename(filename),
    }
    insert = build_insert_lines(present, values)
    if not insert:
        return None

    # Insertion point: before the first top-level `metadata:` line, else at end.
    idx = len(fm_lines)
    for i, ln in enumerate(fm_lines):
        if ln[:1] not in (" ", "\t") and re.match(r"metadata:\s*$", ln):
            idx = i
            break
    new_fm = fm_lines[:idx] + insert + fm_lines[idx:]
    return "".join(pre) + "".join(new_fm) + rest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("memory_dir")
    ap.add_argument("--owner", default="orchestrator")
    ap.add_argument("--scope", default="ho")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default dry-run: report only)")
    args = ap.parse_args(argv)

    mem = Path(args.memory_dir)
    if not mem.is_dir():
        print(f"backfill: {mem} is not a directory", file=sys.stderr)
        return 2

    import time
    changed = skipped = 0
    for path in sorted(mem.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        text = path.read_text(encoding="utf-8")
        mtime_date = time.strftime("%Y-%m-%d", time.gmtime(path.stat().st_mtime))
        new = backfill_text(text, mtime_date=mtime_date, owner=args.owner,
                            scope=args.scope, filename=path.name)
        if new is None:
            skipped += 1
            continue
        changed += 1
        if args.apply:
            path.write_text(new, encoding="utf-8")
            print(f"backfilled {path.name}")
        else:
            src = derive_source_from_filename(path.name) or "(audit)"
            print(f"would backfill {path.name} (source={src}, dates={mtime_date})")

    verb = "backfilled" if args.apply else "would backfill"
    print(f"backfill: {verb} {changed}, skipped {skipped} (already complete) "
          f"[{'APPLIED' if args.apply else 'DRY-RUN'}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
