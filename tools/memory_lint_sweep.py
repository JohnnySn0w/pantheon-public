#!/usr/bin/env python3
"""memory_lint_sweep.py — T-2888 (ORG-068 memory Health leg): lint EVERY pin store.

memory_lint.py checks one memory dir per invocation; a seat can carry several
(one per project slug — e.g. `-home-claude` and `-home-claude-echo-prismatic`).
This sweep finds every `~/.claude/projects/*/memory` dir and lints each, so the
scheduled health check covers the whole account rather than the canonical dir
only. Exit 1 if ANY store has ERRORs (structurally broken pins) — the daily
timer unit routes that through `OnFailure=pantheon-unit-failure-alert@%n` to
Recovery. WARNs stay advisory (same posture as memory_lint's default).

Usage: memory_lint_sweep.py [--projects-root ~/.claude/projects] [--strict]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import memory_lint  # noqa: E402


def sweep(projects_root: Path, stale_days: int, today_ord: int, strict: bool) -> int:
    stores = sorted(p for p in projects_root.glob("*/memory") if p.is_dir())
    if not stores:
        # A seat with no pin store at all is a provisioning gap, not a clean pass.
        print(f"memory_lint_sweep: no */memory dirs under {projects_root}", file=sys.stderr)
        return 1
    total_err = total_warn = 0
    for store in stores:
        n_err = n_warn = 0
        for name, errs, warns in memory_lint.lint_dir(store, stale_days, today_ord):
            for e in errs:
                print(f"ERROR  {store.parent.name}/{name}: {e}")
                n_err += 1
            for w in warns:
                print(f"WARN   {store.parent.name}/{name}: {w}")
                n_warn += 1
        print(f"memory_lint_sweep: {store.parent.name}: {n_err} error(s), {n_warn} warning(s)",
              file=sys.stderr)
        total_err += n_err
        total_warn += n_warn
    print(f"memory_lint_sweep: TOTAL {total_err} error(s), {total_warn} warning(s) "
          f"over {len(stores)} store(s)", file=sys.stderr)
    return 1 if (total_err or (strict and total_warn)) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects-root", type=Path,
                    default=Path.home() / ".claude" / "projects")
    ap.add_argument("--strict", action="store_true", help="warnings fail too")
    ap.add_argument("--stale-days", type=int, default=memory_lint.DEFAULT_STALE_DAYS)
    ap.add_argument("--today-ord", type=int, default=None)
    args = ap.parse_args(argv)

    today_ord = args.today_ord
    if today_ord is None:
        lt = time.gmtime()
        today_ord = lt.tm_year * 372 + lt.tm_mon * 31 + lt.tm_mday

    if not args.projects_root.is_dir():
        print(f"memory_lint_sweep: {args.projects_root} is not a directory", file=sys.stderr)
        return 2
    return sweep(args.projects_root, args.stale_days, today_ord, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
