#!/usr/bin/env python3
"""lint_report_day_index.py — assert watch-report day-index READMEs match disk.

For each `knowledge/watch/reports/<date>/` directory, the README.md is a
hand-or-runner-authored index of that day's reports. It drifts from disk in two
directions (T-1121):
  - DEAD LINK   — README links a `*.md` that is not on disk (renamed/dropped
                  pre-delivery, mis-dated row, or a never-committed `ok` row).
  - UNINDEXED   — a `*.md` report is on disk but not linked from README (a manual
                  delivery that bypassed the cron watch_runner's index regen).

Root cause: manual report deliveries (operator/recovery dispatches outside the cron
watch_runner) bypass the runner's day-index regen.

The contract this lints, per directory:
    { relative `*.md` links in README.md }  ==  { `*.md` files on disk } \\ {README.md}

Failed-run rows legitimately have NO file, so they must appear as PLAIN TEXT
(no markdown link) — a `fail` row that still links to a nonexistent `.md` is a
dead link and is flagged. Only same-directory relative `.md` links are checked;
links with a path separator or a URL scheme are out of scope (cross-doc refs).

Exit codes:
    0 — all checked directories are consistent (or warn-only mode).
    1 — at least one drift found (mismatch mode, the default).
Use --warn to always exit 0 (surfacing drift without failing a pipeline).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Markdown inline link whose target ends in `.md`. Capture the target.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")


def _repo_root(start: Path) -> Path:
    """Walk up to the directory containing `knowledge/watch/reports`, else cwd."""
    cur = start.resolve()
    for cand in (cur, *cur.parents):
        if (cand / "knowledge" / "watch" / "reports").is_dir():
            return cand
    return cur


def linked_md_targets(readme_text: str) -> set[str]:
    """Same-directory relative `*.md` link targets in a day-index README.

    Excludes anything with a path separator or a URL scheme (those are cross-doc
    references, not day-index rows).
    """
    out: set[str] = set()
    for target in _LINK_RE.findall(readme_text):
        if "://" in target or "/" in target:
            continue
        out.add(target)
    return out


def disk_md_files(day_dir: Path) -> set[str]:
    """`*.md` report files in a day directory, excluding README.md and
    `_`-prefixed runner artifacts (e.g. `_topic-warnings.md`, which
    watch_runner writes with deliberately no README row)."""
    return {
        p.name for p in day_dir.glob("*.md")
        if p.name != "README.md" and not p.name.startswith("_")
    }


def check_day_dir(day_dir: Path) -> tuple[list[str], list[str]]:
    """Return (dead_links, unindexed) for one day directory.

    dead_links — linked from README but not on disk.
    unindexed  — on disk but not linked from README.
    A missing README.md with report files present surfaces every file as unindexed.
    """
    readme = day_dir / "README.md"
    linked = linked_md_targets(readme.read_text()) if readme.is_file() else set()
    on_disk = disk_md_files(day_dir)
    dead_links = sorted(linked - on_disk)
    unindexed = sorted(on_disk - linked)
    return dead_links, unindexed


def iter_day_dirs(reports_root: Path):
    """Yield day directories (those containing a README.md or any *.md), sorted."""
    if not reports_root.is_dir():
        return
    for child in sorted(reports_root.iterdir()):
        if child.is_dir() and list(child.glob("*.md")):
            yield child


def lint(reports_root: Path) -> dict[str, tuple[list[str], list[str]]]:
    """Map of day-dir name → (dead_links, unindexed) for dirs with any drift."""
    findings: dict[str, tuple[list[str], list[str]]] = {}
    for day_dir in iter_day_dirs(reports_root):
        dead, unindexed = check_day_dir(day_dir)
        if dead or unindexed:
            findings[day_dir.name] = (dead, unindexed)
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=None,
                    help="a single day dir, or the reports root, or omit to autodetect "
                         "knowledge/watch/reports from cwd")
    ap.add_argument("--warn", action="store_true",
                    help="always exit 0 (surface drift without failing)")
    args = ap.parse_args(argv)

    if args.path:
        target = Path(args.path)
        # A single day dir (has *.md but is not itself named 'reports')?
        if target.is_dir() and target.name != "reports" and list(target.glob("*.md")):
            reports_root = target.parent
            only = target.name
        else:
            reports_root = target
            only = None
    else:
        reports_root = _repo_root(Path.cwd()) / "knowledge" / "watch" / "reports"
        only = None

    findings = lint(reports_root)
    if only is not None:
        findings = {k: v for k, v in findings.items() if k == only}

    if not findings:
        print(f"[lint-day-index] OK — no drift under {reports_root}")
        return 0

    total = 0
    for day, (dead, unindexed) in sorted(findings.items()):
        for f in dead:
            print(f"[lint-day-index] DEAD LINK   {day}/README.md → {f} (not on disk)")
            total += 1
        for f in unindexed:
            print(f"[lint-day-index] UNINDEXED   {day}/{f} (on disk, not linked in README.md)")
            total += 1
    print(f"[lint-day-index] {total} drift item(s) across {len(findings)} dir(s)")
    return 0 if args.warn else 1


if __name__ == "__main__":
    sys.exit(main())
