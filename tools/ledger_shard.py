#!/usr/bin/env python3
"""Shared ledger-shard helpers for the weekly-subfolder layout (T-0148).

Per T-0148 (the operator msg 2125) the decision ledger moves from a single
`ledger/2026.jsonl` to per-ISO-week subfolders, each carrying its own
entries shard plus a dedicated per-segment spine:

    ledger/
      INDEX.md                 ← top-level global spine (groomer-maintained)
      2026-W18/
        2026-W18.jsonl          ← that ISO-week's entries
        SPINE.md                ← dedicated per-segment spine
      2026-W19/ … 2026-W22/
      2026.jsonl.archive-pre-d10-shard   ← (post-migration; flat at ledger/ root)

Segment id = `<ISO-year>-W<NN>` computed from `datetime.isocalendar()` —
ISO year (NOT calendar year) for year-boundary correctness; uppercase W,
two-digit zero-padded week.

This module is the backward-compatibility seam. `iter_ledger_entry_files`
returns the union of subfolder shards PLUS the legacy `2026.jsonl` if it
still exists, so every reader is correct pre-migration (just `[2026.jsonl]`),
during transition (both), and post-migration (just the shards). It is the
key Phase-1 primitive: readers route through it and need no further change
when the migration eventually runs.

Phase 1 shipped read-only backward-compat code (readers route through
`iter_ledger_entry_files`). Phase 2 adds `current_write_target` — the
atomic-flip primitive every WRITER routes through: it returns the legacy
`2026.jsonl` while that file exists (a runtime no-op vs today) and the
current ISO-week shard once Phase 3 archives the legacy file. No writer
flips at deploy time; the flip is driven solely by the on-disk presence of
`2026.jsonl` (that is Phase 3).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# The legacy single-file shard name. ORG-001 §Format chose `YYYY.jsonl`;
# 2026 is the only year-file in existence. Kept explicit (not derived from
# now().year) so a year-boundary fire still finds the legacy file during the
# transition window.
LEGACY_LEDGER_FILENAME = "2026.jsonl"

# Substring that marks an archived legacy file (e.g.
# `2026.jsonl.archive-pre-d10-shard`). Any path containing this is excluded
# from the reader union.
_ARCHIVE_MARKER = "archive"

# Timestamp format used throughout the ledger (ORG-001 §Per-entry schema:
# "ISO 8601 UTC", concretely `%Y-%m-%dT%H:%M:%SZ`).
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def segment_for_ts(ts_str: str) -> str:
    """Return the `<ISO-year>-W<NN>` segment id for an ISO-8601 UTC timestamp.

    Uses ISO calendar (`datetime.isocalendar()`) so entries in the first/last
    days of a calendar year land in the correct ISO-week segment — e.g.
    2027-01-01 belongs to ISO week 53 of ISO-year 2026.

    Raises ValueError if ts_str is not in the expected `%Y-%m-%dT%H:%M:%SZ`
    form (callers that must not silently mis-shard rely on this).
    """
    dt = datetime.strptime(ts_str, TS_FORMAT)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def current_segment_shard(ledger_dir: Path, now: datetime) -> Path:
    """Return the shard path for the segment containing `now` — pure path.

    `ledger_dir / <segment> / f"{<segment>}.jsonl"`. Does NOT create any
    directories; callers that intend to write are responsible for mkdir.
    `now` is taken as already being in the desired (UTC) frame; only its
    isocalendar() is consulted.
    """
    iso_year, iso_week, _ = now.isocalendar()
    segment = f"{iso_year}-W{iso_week:02d}"
    return ledger_dir / segment / f"{segment}.jsonl"


def current_write_target(ledger_dir: Path, now: datetime) -> Path:
    """Return the path a writer should append the NEXT ledger entry to.

    This is the Phase-2 atomic-flip primitive (T-0148):

      * If the legacy `ledger_dir/2026.jsonl` still EXISTS → return it.
        Pre-migration, every writer keeps appending to the single legacy
        file exactly as before — ZERO behavior change while 2026.jsonl is
        present in the repo.
      * Otherwise → return `current_segment_shard(ledger_dir, now)`, the
        current ISO-week subfolder shard. The moment the Phase-3 migration
        archives 2026.jsonl (renames it to `2026.jsonl.archive-pre-d10-shard`),
        this branch takes over and every writer switches to the week-shard on
        its NEXT write — no code change, no redeploy.

    The flip is therefore driven purely by the on-disk existence of the legacy
    file, so deploying Phase-2 (routing all writers through this helper) is a
    runtime no-op until Phase-3 runs. `current_segment_shard` returns a pure
    path; callers that intend to write are responsible for creating the parent
    segment dir first (see `ensure_segment_dir`).
    """
    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    if legacy.is_file():
        return legacy
    return current_segment_shard(ledger_dir, now)


def ensure_segment_dir(target: Path) -> None:
    """Create the parent directory of a write target if it does not exist.

    `current_write_target` may return a post-migration week-shard path whose
    `ledger/<seg>/` parent does not yet exist (a brand-new ISO week). Callers
    invoke this immediately before opening the target for append. Idempotent;
    also covers the legacy-file case (its parent `ledger/` already exists).
    """
    target.parent.mkdir(parents=True, exist_ok=True)


def iter_ledger_entry_files(ledger_dir: Path) -> list[Path]:
    """Return the union of all ledger entry files every reader should consume.

    The union is:
      * every subfolder shard `ledger_dir/<seg>/<seg>.jsonl` (sorted by
        segment id), PLUS
      * the legacy `ledger_dir/2026.jsonl` if it still exists,
    EXCLUDING any path whose name contains the archive marker (e.g.
    `2026.jsonl.archive-pre-d10-shard`).

    Pre-migration this returns `[2026.jsonl]`; post-migration just the
    shards; during the transition window, both. This is the backward-compat
    primitive — readers route through it and stay correct across the flip.

    Fail-soft: a missing `ledger_dir` yields `[]`.
    """
    if not ledger_dir.is_dir():
        return []

    shards: list[Path] = []
    for sub in sorted(ledger_dir.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        shard = sub / f"{sub.name}.jsonl"
        if shard.is_file() and _ARCHIVE_MARKER not in shard.name:
            shards.append(shard)

    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    if legacy.is_file() and _ARCHIVE_MARKER not in legacy.name:
        shards.append(legacy)

    return shards
