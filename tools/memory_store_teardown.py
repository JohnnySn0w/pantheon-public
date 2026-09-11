#!/usr/bin/env python3
"""memory_store_teardown.py — T-2888 (ORG-068 memory Remove leg): archive-then-delete a pin store.

The memory baseline had install/lint but no teardown; decommissioning a seat or
retiring a project slug left its pin store as orphaned disk state. This is the
scripted Remove leg: tar the store to a dated archive, THEN delete the dir —
audit-preserving (P4), and the archive is additionally covered off-box by the
T-2178 backup on its next run.

Safety rails (destructive op):
  - refuses unless the target actually looks like a pin store (contains
    MEMORY.md or at least one `<type>_<slug>.md` pin) — a wrong path fails
    instead of deleting arbitrary trees;
  - refuses without an explicit `--yes`;
  - archives BEFORE deleting; a failed archive aborts the delete.

Usage: memory_store_teardown.py <memory_dir> --yes [--archive-root DIR]
Default archive root: ~/.claude/memory-archives/
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sys
import tarfile
from pathlib import Path

PIN_RE = re.compile(r"^(user|feedback|project|reference)_[a-z0-9_]+\.md$")


def looks_like_pin_store(d: Path) -> bool:
    if (d / "MEMORY.md").is_file():
        return True
    return any(PIN_RE.match(p.name) for p in d.iterdir() if p.is_file())


def teardown(memory_dir: Path, archive_root: Path) -> Path:
    """Archive `memory_dir` under `archive_root`, then remove it. Returns the archive path."""
    slug = memory_dir.parent.name or "root"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = archive_root / f"{slug}-memory-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(memory_dir, arcname=f"{slug}/memory")
    if not archive.is_file() or archive.stat().st_size == 0:
        raise RuntimeError(f"archive failed at {archive}; NOT deleting {memory_dir}")
    shutil.rmtree(memory_dir)
    return archive


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("memory_dir", type=Path)
    ap.add_argument("--yes", action="store_true",
                    help="required — this deletes the store after archiving")
    ap.add_argument("--archive-root", type=Path,
                    default=Path.home() / ".claude" / "memory-archives")
    args = ap.parse_args(argv)

    d = args.memory_dir
    if not d.is_dir():
        print(f"teardown: {d} is not a directory", file=sys.stderr)
        return 2
    if not looks_like_pin_store(d):
        print(f"teardown: {d} does not look like a pin store (no MEMORY.md, no "
              f"<type>_<slug>.md pins) — refusing", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"teardown: would archive+delete {d} — re-run with --yes to proceed",
              file=sys.stderr)
        return 2

    archive = teardown(d, args.archive_root)
    print(f"teardown: archived to {archive}; removed {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
