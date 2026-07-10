#!/usr/bin/env python3
"""Weekly promotion-pipeline runner (T-0649 inc 4).

Cron entrypoint for the lessons→hooks promotion pipeline. Per fire:
  1. Scan the corpora + file new (unledgered) high-confidence candidates to the
     `promotion-candidates/` review surface (`promote_lessons.file_candidates`).
  2. Auto-commit + push the surface (candidate files + the ledger), scoped
     STRICTLY to `promotion-candidates/`, HERMES-guarded — the same discipline as
     `ticket_grooming.py`.
  3. Send a plain-text proof-of-life digest to Telegram (pending count + how many
     new this run), regardless of count — a silent week is still visible.

Recovery-unattended. It NEVER auto-commits a hook — only the review SURFACE; landing
a guard stays behind the recovery review gate (`--land`/`--decline`). No force, no
`--no-verify`. Mirrors grooming's posture: a HERMES-guard refusal or push failure
exits non-zero and is reported, never silently swallowed.

    python3 tools/promote_lessons_cron.py               # file + commit + push + digest
    python3 tools/promote_lessons_cron.py --no-digest   # skip Telegram
    python3 tools/promote_lessons_cron.py --no-commit   # file to the surface only
    python3 tools/promote_lessons_cron.py --dry-run     # report what WOULD file; touch nothing
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `from tools... import` work as a script and as a module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import promote_lessons as pl  # noqa: E402
from tools._git_utils import commit_scoped  # noqa: E402
from tools.watch_digest import read_telegram_token, send_telegram  # noqa: E402

SURFACE_REL = "promotion-candidates"
DEFAULT_LEDGER = pl.DEFAULT_CANDIDATES_DIR / "ledger.json"
DEFAULT_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "456474197")  # the operator


def _default_telegram_env() -> Path:
    return Path.home() / ".claude" / "channels" / "telegram" / ".env"


# --------------------------------------------------------------------------- #
# Digest                                                                       #
# --------------------------------------------------------------------------- #

def build_digest(n_pending: int, n_new: int, pending: list[dict],
                 *, committed: bool, commit_note: str = "") -> str:
    """Plain-text proof-of-life. First words are sender-identifiable (watch_digest
    convention) so the operator can pattern-match it. Always emitted, even at 0."""
    head = (f"promote_lessons (weekly): {n_pending} promotion candidate(s) "
            f"awaiting recovery review — {n_new} new this run.")
    lines = [head]
    if n_new and not committed:
        lines.append(f"⚠ filed but NOT committed: {commit_note}")
    if pending:
        lines.append("")
        for fm in pending[:10]:
            lines.append(f"• [{fm.get('confidence','?')}] {fm.get('fingerprint','?')}")
        if len(pending) > 10:
            lines.append(f"  …and {len(pending) - 10} more.")
        lines.append("")
        lines.append("Review: tools/promote_lessons.py --list-pending "
                     "| --land FP | --decline FP")
    else:
        lines.append("Nothing to review. (Nothing is auto-promoted; the gate is recovery.)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commit the surface (scoped, HERMES-guarded)                                  #
# --------------------------------------------------------------------------- #

def commit_surface(written: list[Path], commit_message: str,
                   cwd: Path = _REPO_ROOT) -> tuple[bool, str]:
    """Stage + commit + push ONLY `promotion-candidates/`. Returns (ok, detail).

    Delegates to the shared `commit_scoped` primitive (T-1088 convergence):
    HERMES message + staged-file guard, sync, dir-scoped add of SURFACE_REL,
    leak-guard, commit, push_with_retry. `written` is retained for signature
    compat (run() passes it); the HERMES file-guard now scans the actually-staged
    files inside commit_scoped. Operator-facing return strings are preserved."""
    del written  # HERMES file-guard moved into commit_scoped (scans staged files)
    ok, detail = commit_scoped(
        [SURFACE_REL], commit_message, cwd=cwd, push=True, allow_prefix=True
    )
    if not ok:
        return False, detail
    if detail == "no changes":
        return True, "nothing staged (surface unchanged)"
    return True, "committed + pushed"


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def run(args, *, do_commit=commit_surface, do_send=None) -> int:
    surface, ledger = args.candidates_dir, args.ledger
    cands = pl.scan_all(args.lessons_dir, args.bugs_dir, args.memory_dir)
    threshold = pl.CONFIDENCE_ORDER[args.min_confidence]
    cands = [c for c in cands if pl.CONFIDENCE_ORDER[c.confidence] >= threshold]

    if args.dry_run:
        fresh = pl.filter_unledgered(cands, pl.load_ledger(ledger))
        print(f"[dry-run] would file {len(fresh)} new candidate(s); "
              f"{len(pl.list_pending(surface))} already pending. Nothing touched.")
        for c in fresh:
            print(f"  + [{c.confidence}] {c.fingerprint}")
        return 0

    written = pl.file_candidates(cands, surface, ledger)
    pending = pl.list_pending(surface)

    committed, commit_note = True, ""
    rc = 0
    if written and not args.no_commit:
        msg = (f"promote(candidates): file {len(written)} new candidate(s)\n\n"
               "Auto-committed by promote_lessons_cron.py (Recovery unattended).\n"
               f"Scope: {SURFACE_REL}/ only. Review gate is recovery "
               "(--land/--decline); no hook is auto-committed.\n")
        if args.no_push:
            # commit locally only — exercise the path without pushing.
            committed, commit_note = False, "push skipped (--no-push)"
        else:
            committed, commit_note = do_commit(written, msg)
            if not committed:
                print(f"[promote_cron] {commit_note}", file=sys.stderr)
                rc = 8

    digest = build_digest(len(pending), len(written), pending,
                          committed=committed, commit_note=commit_note)
    if args.no_digest:
        print(digest)
    else:
        token = read_telegram_token(args.telegram_env)
        if not token:
            print(f"[promote_cron] no telegram token at {args.telegram_env}; "
                  f"digest not sent:\n{digest}", file=sys.stderr)
            return rc or 0
        sender = do_send or send_telegram
        if not sender(token, args.chat_id, digest):
            print("[promote_cron] telegram send failed", file=sys.stderr)
    return rc


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Default-safe contract (T-0259 / T-0825): refuse a flagless invocation.

    The prod action is commit + push to origin/main. The caller must explicitly
    opt in via --commit-prod, OR opt into a reduced mode (--dry-run / --no-commit
    / --no-push). A flagless run refuses (exit 64) rather than silently writing
    prod — the no-prod-default invariant. The systemd unit passes --commit-prod
    explicitly. Returns the exit code (64) to reject the invocation, else None.
    """
    safe_explicit = args.dry_run or args.no_commit or args.no_push
    if args.commit_prod and safe_explicit:
        print(
            "ERROR: --commit-prod conflicts with a safe-mode flag "
            "(--dry-run / --no-commit / --no-push).",
            file=sys.stderr,
        )
        return 64
    if not (args.commit_prod or safe_explicit):
        print(
            "ERROR: refusing to run with no mode flag. Pass --commit-prod (the cron "
            "path) or a safe mode (--dry-run / --no-commit / --no-push).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly lessons→hooks promotion runner (T-0649 inc 4).")
    ap.add_argument("--lessons-dir", type=Path, default=pl.DEFAULT_LESSONS_DIR)
    ap.add_argument("--bugs-dir", type=Path, default=pl.DEFAULT_BUGS_DIR)
    ap.add_argument("--memory-dir", type=Path, default=pl.DEFAULT_MEMORY_DIR)
    ap.add_argument("--candidates-dir", type=Path, default=pl.DEFAULT_CANDIDATES_DIR)
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--min-confidence", choices=("medium", "high"), default="high")
    ap.add_argument("--commit-prod", action="store_true",
                    help="real run: file + commit + push to prod (the cron path; required to write prod)")
    ap.add_argument("--no-commit", action="store_true", help="file to the surface but do not git")
    ap.add_argument("--no-push", action="store_true", help="commit locally but do not push")
    ap.add_argument("--no-digest", action="store_true", help="print the digest instead of Telegram")
    ap.add_argument("--dry-run", action="store_true", help="report what would file; touch nothing")
    ap.add_argument("--telegram-env", type=Path, default=_default_telegram_env())
    ap.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    args = ap.parse_args(argv)
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
