#!/usr/bin/env python3
"""Retroactively scrub credentials from existing Chronicle companion archives (T-1683).

Formerly scrub_graveyard_companions.py; renamed 2026-08-23 (T-3039).

The T-1683 credential scrub applies to NEW pane-log companion archives at write
time. This one-off sweep applies the SAME scrub to companion .pane.log.gz files
already on disk (the 2026-07-11 finding: scrollback with an inline client
password was persisted before the write-time scrub landed).

Reuses chronicle.scrub_credentials so the retro pass and the write-time pass can
never diverge. Per-file atomic: decompress → scrub → recompress to a temp →
os.replace only if a redaction actually happened (untouched files are left
byte-identical, no churn). Best-effort + non-fatal per file: one unreadable/
corrupt archive must not abort the sweep.

Runs on the CURRENT account's Chronicle (each account owns its own; cross-account
is a separate sudo'd invocation). Default dry-run — pass --apply to rewrite.

Usage:
  python3 tools/scrub_chronicle_companions.py            # dry-run: report would-scrub
  python3 tools/scrub_chronicle_companions.py --apply    # rewrite redacted files
  python3 tools/scrub_chronicle_companions.py --root <dir> --apply
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.chronicle import CHRONICLE_ROOT, scrub_credentials  # noqa: E402


def _process_one(gz_path: Path, *, apply: bool, also_literals: list[str] | None = None) -> str:
    """Return 'clean' | 'scrubbed' | 'would-scrub' | 'error' for one archive."""
    try:
        with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as fh:
            original = fh.read()
    except (OSError, EOFError) as exc:
        print(f"  ERROR reading {gz_path.name}: {exc!r}", file=sys.stderr)
        return "error"
    scrubbed = scrub_credentials(original)
    # Belt-and-suspenders for a KNOWN leaked secret: exhaustively remove the
    # literal (post-ANSI-strip so pane-split forms collapse first). Pattern
    # matching can miss a novel form; a known string can be nuked exactly.
    for lit in also_literals or []:
        if lit:
            scrubbed = scrubbed.replace(lit, "<redacted-cred>")
    if scrubbed == original:
        return "clean"
    if not apply:
        print(f"  would-scrub: {gz_path}")
        return "would-scrub"
    try:
        tmp = gz_path.with_suffix(gz_path.suffix + ".scrubtmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as out:
            out.write(scrubbed)
        os.replace(tmp, gz_path)  # atomic same-dir swap
        print(f"  scrubbed: {gz_path}")
        return "scrubbed"
    except OSError as exc:
        print(f"  ERROR writing {gz_path.name}: {exc!r}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass
        return "error"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="chronicle root (default: this account's)")
    ap.add_argument("--apply", action="store_true", help="rewrite redacted files (default: dry-run)")
    ap.add_argument("--also-literal", action="append", default=[],
                    help="exhaustively redact this exact string too (repeatable; for a KNOWN leaked secret)")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else CHRONICLE_ROOT
    if not root.is_dir():
        print(f"no chronicle at {root} (nothing to sweep)")
        return 0

    counts = {"clean": 0, "scrubbed": 0, "would-scrub": 0, "error": 0}
    for gz in sorted(root.rglob("*.pane.log.gz")):
        counts[_process_one(gz, apply=args.apply, also_literals=args.also_literal)] += 1

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[scrub-chronicle {mode}] clean={counts['clean']} "
          f"scrubbed={counts['scrubbed']} would-scrub={counts['would-scrub']} "
          f"errors={counts['error']} (root={root})")
    # Non-zero only on a write error under --apply (a real failure to surface).
    return 1 if (args.apply and counts["error"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
