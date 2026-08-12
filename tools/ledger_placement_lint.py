#!/usr/bin/env python3
"""Lint ledger entries for correct ISO-week shard placement (T-2077).

The ledger week-shard invariant: an entry lives in `ledger/<seg>/<seg>.jsonl`
where `seg = segment_for_ts(entry_ts(entry))`. It is *advisory* — nothing on
the append path enforces it — so boundary-spillover recurs: entries get
appended to the PREVIOUS week's shard by session habit near the Monday 00:00Z
ISO rollover (2026-07-27 audit found 107 such historical misplacements).

This is the forward-only guard (T-2077, option (b)). Modes:

  * (bare)             scan only the current + prior ISO-week shard (the
                        DEFAULT, T-2659) -- catches live mid-week-roll drift
                        without re-warning the ~90 frozen historical
                        misplacements sitting in older closed shards
  * ``--all``          scan every shard (full historical audit)
  * ``--range B T``    scan only the entries ADDED between two git revisions

The pre-push hook runs ``--range`` over just the push range so NEW
misplacements surface without re-warning the frozen historical ones (option
(c) — backfilling those — was declined: they stay, union-glob readers key off
``entry_ts`` so nothing drops). T-2659 applied the same "accept frozen
history, scope the live check" reasoning to the bare/author-invocation
default: a chronically-red lint from closed-week noise defeats its own
purpose, since a genuinely new misplacement wouldn't stand out.

Severity is COSMETIC: every reader unions ``ledger/*/*.jsonl`` via
``ledger_shard.iter_ledger_entry_files`` and keys off ``entry_ts``, so a
misplaced entry is still read and grouped into the right week. This guards
hygiene + drift, not correctness — which is why the hook WARNS, never blocks.

Exit code: 0 if clean, 1 if any misplacement (author/CI use). The pre-push
hook ignores the exit code and never blocks on findings.

A third, unrelated concern lives in this module too (T-2650): ``--staged``
validates that ledger lines newly ADDED in the staged diff are parseable
JSON objects at all — a missing-`}` line commits with zero guard otherwise
(T-2637 found six such entries already on origin/main). This is a
correctness guard, not a cosmetic one, so unlike ``--all``/``--range`` it is
wired to BLOCK the commit (see ``.githooks/pre-commit``). It shares this
module only because the underlying git-diff-scan shape (added-lines-only)
is the same primitive; ``check_entry`` (placement) and
``check_entry_validity`` (parseability) are independent functions with
different judgements.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from ledger_shard import (  # noqa: E402
    LEGACY_LEDGER_FILENAME,
    entry_ts,
    segment_for_ts,
)

# A shard segment id is `<ISO-year>-W<NN>` (e.g. 2026-W31).
_SEG_RE = re.compile(r"^\d{4}-W\d{2}$")


def shard_segment(path: str | Path) -> str | None:
    """Return the ISO-week segment a shard-file path encodes, or None.

    `ledger/<seg>/<seg>.jsonl` -> `<seg>`. Returns None for the legacy flat
    file or anything that is not a `<seg>/<seg>.jsonl` shard — i.e. paths with
    no authoritative segment to check placement against.
    """
    p = Path(path)
    if p.name == LEGACY_LEDGER_FILENAME:
        return None
    seg = p.parent.name
    if not _SEG_RE.match(seg):
        return None
    if p.name != f"{seg}.jsonl":
        return None
    return seg


def check_entry(seg: str, line: str) -> dict | None:
    """Return a misplacement dict for a raw jsonl line, or None.

    A line that is blank, not parseable JSON, not an object, or whose entry
    has no derivable ts is NOT judgeable here -> None. This linter judges
    placement only, and only for entries whose ts it can read.
    """
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(entry, dict):
        return None
    ts = entry_ts(entry)
    if not ts:
        return None
    try:
        want = segment_for_ts(ts)
    except ValueError:
        return None
    if want == seg:
        return None
    return {
        "shard": seg,
        "want": want,
        "ts": ts,
        "id": entry.get("id", "?"),
        "agent": entry.get("agent", "?"),
    }


def check_entry_validity(line: str) -> dict | None:
    """Return a validity-failure dict for a raw jsonl line, or None if valid.

    Unlike `check_entry` (placement judgement — silently skips anything it
    can't parse, since it only judges placement), this treats "not parseable
    JSON" or "not a JSON object" as the finding itself (T-2650): that is
    exactly the write-path corruption class the six 2026-08-05 missing-`}`
    entries were (T-2637). Blank lines are not entries -> None, not a
    failure.
    """
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except (ValueError, TypeError) as exc:
        return {"line": line, "error": f"invalid JSON: {exc}"}
    if not isinstance(entry, dict):
        return {"line": line, "error": "not a JSON object"}
    return None


def _is_ledger_jsonl_path(rel: str) -> bool:
    """True for any ledger/**/*.jsonl path (shard or legacy), false for
    everything else -- including anything under an archived filename (the
    `archive` marker `ledger_shard._ARCHIVE_MARKER` uses), since archived
    files are frozen and never legitimately re-written."""
    if not rel.startswith("ledger/") or not rel.endswith(".jsonl"):
        return False
    if "archive" in rel:
        return False
    return True


def lint_staged_validity(repo_root: str | Path | None = None) -> list[dict]:
    """Validate JSON-parseability of ledger lines ADDED in the STAGED diff.

    This is the pre-commit-hook scope (T-2650): it flags an unparseable or
    non-object line only if it is being newly added in THIS commit
    (`git diff --cached` vs HEAD) under a `ledger/**/*.jsonl` path -- a line
    already present in HEAD (any pre-existing/legacy/item-2-placement
    content) is structurally never inspected, so this can never block on
    frozen historical state. Mirrors `lint_range`'s added-lines-only diff
    scan, applied to the staged diff instead of a commit range.

    Fail-open: any `git` invocation failing (bad repo_root, non-git dir,
    detached/corrupt state) yields `[]` for that step rather than raising --
    a validity guard must never brick a commit on its own account.
    """
    if repo_root is None:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        repo_root = r.stdout.strip() or "."
    out: list[dict] = []
    names = _git(repo_root, "diff", "--cached", "--name-only",
                 "--diff-filter=ACM", "--", "ledger")
    if names.returncode != 0:
        return out
    for rel in names.stdout.splitlines():
        rel = rel.strip()
        if not rel or not _is_ledger_jsonl_path(rel):
            continue
        d = _git(repo_root, "diff", "--cached", "--unified=0", "--", rel)
        if d.returncode != 0:
            continue
        for dl in d.stdout.splitlines():
            # added content lines only (skip the "+++ b/..." file header)
            if dl.startswith("+") and not dl.startswith("+++"):
                v = check_entry_validity(dl[1:])
                if v:
                    v["path"] = rel
                    out.append(v)
    return out


def format_validity_lines(invalid: list[dict]) -> list[str]:
    lines = []
    for v in invalid:
        raw = v["line"]
        shown = raw if len(raw) <= 200 else raw[:200] + "...(truncated)"
        lines.append(f"{v['path']}: {v['error']} -- {shown!r}")
    return lines


def _scan_shard(shard: Path, seg: str) -> list[dict]:
    """Scan one shard file's lines for placement misplacements, given the
    segment id its path resolved to (`shard_segment` already validated it)."""
    out: list[dict] = []
    for line in shard.read_text().splitlines():
        m = check_entry(seg, line)
        if m:
            out.append(m)
    return out


def current_scope_segments(now: datetime | None = None) -> list[str]:
    """Return `[current, prior]` ISO-week segment ids for the DEFAULT scope.

    `now` is an injectable reference datetime (the determinism seam) --
    defaults to the real UTC clock at call time when omitted, but every test
    must pass a fixed `now` so results don't depend on wall-clock time. Uses
    ISO calendar (`isocalendar()`/`fromisocalendar()`, matching
    `ledger_shard.segment_for_ts`'s own semantics) so year-boundary weeks
    (e.g. a week 53 spanning into the next calendar year) resolve the same
    way the linter's own placement judgement does.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    current = f"{iso_year}-W{iso_week:02d}"
    monday = datetime.fromisocalendar(iso_year, iso_week, 1)
    prior_day = monday - timedelta(days=1)  # last day (Sun) of the prior ISO week
    prior_year, prior_week, _ = prior_day.isocalendar()
    prior = f"{prior_year}-W{prior_week:02d}"
    return [current, prior]


def lint_all(ledger_dir: str | Path = "ledger") -> list[dict]:
    """Scan every shard file under `ledger_dir`; return misplacements.

    Skips the legacy flat file (no shard segment to check against). Fail-soft:
    a missing `ledger_dir` yields `[]`.
    """
    ledger_dir = Path(ledger_dir)
    out: list[dict] = []
    if not ledger_dir.is_dir():
        return out
    for sub in sorted(ledger_dir.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        shard = sub / f"{sub.name}.jsonl"
        seg = shard_segment(shard)
        if seg is None or not shard.is_file():
            continue
        out.extend(_scan_shard(shard, seg))
    return out


def lint_default(ledger_dir: str | Path = "ledger", now: datetime | None = None) -> list[dict]:
    """Scan only the current + prior ISO-week shard; return misplacements.

    This is the bare-invocation DEFAULT scope (T-2659): it catches live
    mid-week-roll drift in either direction (a current-week entry stranded in
    the prior shard, or a stale-ts entry newly appended to the current shard)
    without re-warning the frozen historical misplacements sitting in older,
    closed shards -- `--all` remains the full-audit opt-in. Fail-soft: a
    missing `ledger_dir` or missing/non-shard segment file yields `[]` for
    that segment rather than raising.
    """
    ledger_dir = Path(ledger_dir)
    out: list[dict] = []
    if not ledger_dir.is_dir():
        return out
    for seg in current_scope_segments(now):
        shard = ledger_dir / seg / f"{seg}.jsonl"
        if shard_segment(shard) != seg or not shard.is_file():
            continue
        out.extend(_scan_shard(shard, seg))
    return out


def _git(repo_root: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=False,
    )


def lint_range(base: str, tip: str, repo_root: str | Path | None = None) -> list[dict]:
    """Scan only the ledger entries ADDED between `base` and `tip`.

    This is the pre-push scope: it flags NEW misplacements in a push without
    re-warning the frozen historical ones. Fail-open: an unreadable range (bad
    revs, etc.) yields `[]` rather than raising — a placement warn must never
    brick a push.
    """
    if repo_root is None:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        repo_root = r.stdout.strip() or "."
    out: list[dict] = []
    names = _git(repo_root, "diff", "--name-only", "--diff-filter=d",
                 f"{base}..{tip}", "--", "ledger")
    if names.returncode != 0:
        return out
    for rel in names.stdout.splitlines():
        rel = rel.strip()
        seg = shard_segment(rel) if rel else None
        if seg is None:
            continue
        d = _git(repo_root, "diff", "--unified=0", f"{base}..{tip}", "--", rel)
        if d.returncode != 0:
            continue
        for dl in d.stdout.splitlines():
            # added content lines only (skip the "+++ b/..." file header)
            if dl.startswith("+") and not dl.startswith("+++"):
                m = check_entry(seg, dl[1:])
                if m:
                    out.append(m)
    return out


def format_lines(misplaced: list[dict]) -> list[str]:
    return [
        f"{m['shard']} should be {m['want']}: {m['id']} "
        f"(agent={m['agent']}, ts={m['ts']})"
        for m in misplaced
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Lint ledger entries for correct ISO-week shard placement (T-2077) "
                     "and, via --staged, JSON-validity of staged additions (T-2650).",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true",
                   help="scan every shard file for placement (full historical audit; "
                        "bare invocation with no flags is the DEFAULT and scans only "
                        "the current + prior ISO-week shard, T-2659)")
    g.add_argument("--range", nargs=2, metavar=("BASE", "TIP"),
                   help="scan only entries ADDED between two git revisions for placement "
                        "(pre-push scope)")
    g.add_argument("--staged", action="store_true",
                   help="validate JSON-parseability of ledger lines ADDED in the staged "
                        "diff (pre-commit scope, T-2650) -- a different check than "
                        "--all/--range, which judge placement, not validity")
    ap.add_argument("--ledger-dir", default="ledger",
                    help="ledger root (default: ledger); ignored by --staged")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.staged:
        invalid = lint_staged_validity()
        if args.json:
            print(json.dumps(invalid, indent=2))
        else:
            for line in format_validity_lines(invalid):
                print(line)
        return 1 if invalid else 0

    if args.range:
        misplaced = lint_range(args.range[0], args.range[1])
    elif args.all:
        misplaced = lint_all(args.ledger_dir)
    else:
        misplaced = lint_default(args.ledger_dir)

    if args.json:
        print(json.dumps(misplaced, indent=2))
    else:
        for line in format_lines(misplaced):
            print(line)
    return 1 if misplaced else 0


if __name__ == "__main__":
    raise SystemExit(main())
