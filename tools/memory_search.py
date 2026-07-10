#!/usr/bin/env python3
"""tools/memory_search.py — rg-backed search over an agent's memory pins (ORG-035 / T-1129).

The built-in harness recall reads pins on-demand guided by MEMORY.md descriptions;
there is no query path for non-harness consumers (a tool or a sibling agent asking
"what do we know about X"). This is that path (ORG-035 portability checklist P3):
a thin, deterministic ripgrep wrapper over the pin corpus — matching the mechanism's
grain (recall is literally "read/grep the files").

  python3 tools/memory_search.py "telegram"  <memory_dir>
  python3 tools/memory_search.py "sudo" <memory_dir> --names-only

Cross-agent search (one agent reading another's corpus) is possible by pointing
--dir at the other store IF readable; cross-user perms currently block that
(reference: the per-agent stores are uid-siloed until ORG-035's canonical repo).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def build_rg_cmd(query: str, memory_dir: str, *, names_only: bool,
                 ignore_case: bool) -> list[str]:
    """Construct the ripgrep argv (pure — testable without running rg)."""
    cmd = ["rg", "--color=never"]
    if ignore_case:
        cmd.append("-i")
    if names_only:
        cmd.append("--files-with-matches")
    else:
        cmd += ["--no-heading", "--line-number"]
    # Only search markdown pins; never recurse outside the given dir.
    cmd += ["--glob", "*.md", "--", query, memory_dir]
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("memory_dir", help="the pin directory to search")
    ap.add_argument("--names-only", action="store_true", help="list matching pin files only")
    ap.add_argument("--case-sensitive", action="store_true")
    args = ap.parse_args(argv)

    if shutil.which("rg") is None:
        print("memory_search: ripgrep (rg) not found on PATH", file=sys.stderr)
        return 2
    d = Path(args.memory_dir)
    if not d.is_dir():
        print(f"memory_search: {d} is not a directory", file=sys.stderr)
        return 2

    cmd = build_rg_cmd(args.query, str(d), names_only=args.names_only,
                       ignore_case=not args.case_sensitive)
    # rg exit: 0 = matches, 1 = no matches (not an error here), 2 = real error.
    proc = subprocess.run(cmd, text=True)
    if proc.returncode == 1:
        print("memory_search: no matches", file=sys.stderr)
        return 0
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
