#!/usr/bin/env python3
"""Local byte-sequence discipline audit — origin-side coverage without a runner (T-2392).

WHY THIS EXISTS. The origin-side guard is `.github/workflows/hermes-audit.yml`,
pinned `runs-on: [self-hosted, linux, x64, pve2]`. While pve2 is off the tailnet
that workflow cannot run: jobs queue against a registered-but-offline runner and
expire at 24h (605 deep on 2026-08-03). `protect-main` declares no required
status checks, so nothing renders the absence as broken. This script reproduces
the workflow's scan locally, on a timer, so the window is covered by something
rather than by nothing.

It is NOT a replacement for the workflow. It runs on one seat's clone against
`origin/main`; it cannot gate a push. It answers exactly one question: *has the
trigger sequence entered history between the last verified point and
origin/main?*

## Four design decisions, each bought with a failure

1. **`git log -p`, never `git diff BASE...HEAD`.** A cumulative three-dot diff
   shows NET EFFECT: a line added in one commit and removed in a later one leaves
   no trace in it while remaining in history forever. Measured on the 2026-08-02
   window, the cumulative diff was **51%** of the per-commit surface (2,190,513 vs
   4,293,141 bytes; independently reproduced by recovery from a second clone). A
   discipline guard exists precisely to catch content that ENTERED history, so
   net-effect is the wrong surface — and the missing half is exactly the class of
   thing the guard is for.

2. **Refuse a base that is not an ancestor of `origin/main`.** The obvious base —
   "the last successful audit run" — can be a PULL_REQUEST head sha, which is not
   on main's history. Diffing from it yields phantom reversals and a clean result
   that means nothing. This cost one `merge-base --is-ancestor` call to catch and
   would otherwise have shipped as a green.

3. **Validate the instrument on every run, before believing any zero.** A clean
   scan from an unproven scanner is worth nothing. Each run fires a known-POSITIVE
   (must match) and a known-NEGATIVE (must not) through the same pattern object
   the real scan uses. If either behaves wrongly the run FAILS rather than
   reporting clean — a null instrument that reports clean is worse than no
   instrument, because it certifies the window.

4. **A violation does NOT advance the watermark.** On a hit the state file is left
   untouched, so the next run re-scans the same window and keeps failing until a
   human resolves it. Advancing on violation would audit the problem away.

## Source cleanliness

This file must never contain the unescaped trigger as contiguous bytes. The
self-test probe is assembled at RUNTIME by concatenation, so the sequence exists
only in memory. Verification: `grep -cF` for the unescaped form against this file
must return 0.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from _git_utils import _HERMES_PATTERN  # noqa: E402  single source of truth for the trigger

DEFAULT_STATE = Path.home() / ".local" / "state" / "pantheon" / "hermes_local_audit.json"

# Last GREEN push-to-main run of the origin-side workflow (2026-08-02T00:40:00Z).
# Only used when no state file exists yet; every later run reads the watermark.
BOOTSTRAP_BASE = "24749ccfcdd111b582dbabd1da24df2370a4edfd"

REMOTE_REF = "origin/main"


class AuditError(RuntimeError):
    """Fatal: the audit could not be performed. Never conflated with 'clean'."""


def _git(*args: str, binary: bool = False, cwd: Path | None = None):
    """Run git, raising AuditError on failure. Never returns a partial result."""
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=not binary,
    )
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode("utf-8", "replace")
        raise AuditError(f"git {' '.join(args)} failed ({r.returncode}): {err.strip()[:400]}")
    return r.stdout


def validate_instrument() -> None:
    """Fire a known-positive and a known-negative through the real pattern.

    Raises AuditError unless the positive matches AND the negative does not. This
    runs BEFORE the real scan on every invocation; see design decision 3.
    """
    positive = b"see " + b"HERME" + b"S.md here"   # assembled at runtime, never in source
    negative = b"see hermes.md here"               # documented-safe lowercase form
    if not _HERMES_PATTERN.search(positive):
        raise AuditError("instrument invalid: known-POSITIVE did not match; a clean scan would be meaningless")
    if _HERMES_PATTERN.search(negative):
        raise AuditError("instrument invalid: known-NEGATIVE matched; the pattern is over-broad")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def resolve_base(state: dict, override: str | None) -> str:
    base = override or state.get("last_clean_sha") or BOOTSTRAP_BASE
    # Design decision 2: a base off main's history produces a meaningless green.
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, REMOTE_REF],
        cwd=str(REPO_ROOT), capture_output=True,
    )
    if r.returncode != 0:
        raise AuditError(
            f"base {base} is NOT an ancestor of {REMOTE_REF} — refusing to scan. "
            "A base off main's history yields phantom reversals and a clean result that means nothing. "
            "(Common cause: a pull_request head sha.)"
        )
    return base


def scan_window(base: str, head_sha: str) -> tuple[int, bytes]:
    """Return (violation_count, matched_sample) over the per-commit surface."""
    # Design decision 1: log -p, not diff A...B.
    surface = _git("log", "-p", "--no-color", f"{base}..{head_sha}", binary=True)
    hits = _HERMES_PATTERN.findall(surface)
    return len(hits), surface


def audit(state_path: Path, base_override: str | None, dry_run: bool) -> dict:
    validate_instrument()
    _git("fetch", "--quiet", "origin")

    state = load_state(state_path)
    base = resolve_base(state, base_override)
    head_sha = _git("rev-parse", REMOTE_REF).strip()

    if base == head_sha:
        return {"result": "noop", "base": base, "head": head_sha, "commits": 0, "violations": 0}

    commits = int(_git("rev-list", "--count", f"{base}..{head_sha}").strip())
    violations, surface = scan_window(base, head_sha)

    out = {
        "result": "violation" if violations else "clean",
        "base": base,
        "head": head_sha,
        "commits": commits,
        "surface_bytes": len(surface),
        "violations": violations,
    }

    # Design decision 4: only a CLEAN result advances the watermark.
    if not violations and not dry_run:
        state.update({"last_clean_sha": head_sha, "last_result": "clean"})
        save_state(state_path, state)
    elif violations and not dry_run:
        state["last_result"] = "violation"
        save_state(state_path, state)

    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", help="override the watermark (must be an ancestor of origin/main)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--dry-run", action="store_true", help="scan but never write state")
    ap.add_argument("--json", action="store_true", help="emit the result dict as JSON")
    args = ap.parse_args(argv)

    try:
        out = audit(args.state, args.base, args.dry_run)
    except AuditError as exc:
        print(f"[hermes_local_audit] AUDIT COULD NOT RUN: {exc}", file=sys.stderr)
        return 2  # distinct from 1 (violation): "did not ask" vs "asked and found"

    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        if out["result"] == "noop":
            print(f"[hermes_local_audit] noop — {REMOTE_REF} unchanged at {out['head'][:12]}")
        elif out["result"] == "clean":
            print(
                f"[hermes_local_audit] CLEAN — {out['commits']} commits, "
                f"{out['surface_bytes']} bytes scanned, watermark -> {out['head'][:12]}"
            )
        else:
            print(
                f"[hermes_local_audit] VIOLATION — {out['violations']} hit(s) in "
                f"{out['commits']} commits ({out['base'][:12]}..{out['head'][:12]}). "
                "Watermark NOT advanced; re-run will re-scan this window.",
                file=sys.stderr,
            )

    return 1 if out["result"] == "violation" else 0


if __name__ == "__main__":
    sys.exit(main())
