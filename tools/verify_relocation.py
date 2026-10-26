#!/usr/bin/env python3
"""Check that an ADR reshape lost no line (T-4730 / T-4745).

Usage (from the repo root, paths relative to it):
    python3 tools/verify_relocation.py <adr-path> <base-ref> [companion-path ...]

Every non-blank line of <adr-path> at <base-ref> must appear, stripped and
verbatim, as a whole line in the working-tree ADR or in one of the companion
files (the notes it was relocated to), once per occurrence. Prints each missing line with its old line
number and a preserved/total count; exit 1 if any line is missing, 2 on a
usage or git error.

The ADR altitude and clutter rules (sop/standard/ratifiable-adr-shape.md)
relocate build-tier detail and review-cycle content VERBATIM; this is the
check that nothing was dropped on the way (P4).
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path


def missing_lines(root: Path, adr: str, base: str, companions: list[str]) -> tuple[list[tuple[int, str]], int]:
    """Return ([(old line number, line)] missing, total non-blank lines)."""
    old = subprocess.run(
        ["git", "show", f"{base}:{adr}"], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    # Whole-line, count-aware match (T-4745 review): every occurrence of a line
    # in the old ADR needs its own occurrence in the ADR + notes, so a short or
    # generic line ("---", a code fence) cannot be satisfied by an unrelated
    # match elsewhere, and a duplicated line cannot be half-dropped.
    available: Counter[str] = Counter()
    for path in [adr, *companions]:
        available.update(
            line.strip() for line in (root / path).read_text(encoding="utf-8").splitlines() if line.strip()
        )
    missing = []
    for i, line in enumerate(old):
        key = line.strip()
        if not key:
            continue
        if available[key] > 0:
            available[key] -= 1
        else:
            missing.append((i + 1, line))
    return missing, sum(1 for line in old if line.strip())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("\n".join(__doc__.strip().splitlines()[2:4]), file=sys.stderr)
        return 2
    adr, base, *companions = args
    try:
        missing, total = missing_lines(Path.cwd(), adr, base, companions)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"verify_relocation: {exc}", file=sys.stderr)
        return 2
    for n, line in missing:
        print(f"MISSING L{n}: {line[:160]}")
    print(f"{total - len(missing)}/{total} non-blank lines preserved")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
