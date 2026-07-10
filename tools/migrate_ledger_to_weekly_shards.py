#!/usr/bin/env python3
"""One-shot migration: flat ledger/2026.jsonl → weekly subfolder shards (T-0148).

Per the locked architecture (the operator msg 2125), the decision ledger moves
from a single `ledger/2026.jsonl` to per-ISO-week subfolders, each with a
dedicated entries shard plus a per-segment spine:

    ledger/
      INDEX.md                 ← top-level global spine (untouched here)
      2026-W18/
        2026-W18.jsonl          ← that ISO-week's entries (RAW lines, verbatim)
        SPINE.md                ← per-segment spine (generated)
      2026-W19/ … 2026-W22/
      2026.jsonl.archive-pre-d10-shard   ← legacy file renamed on success

Logic:
  1. Read every line of `ledger/2026.jsonl`. ABORT on any malformed line
     (no silent skip — a mis-parse would mis-shard or drop an entry).
  2. Group lines by `ledger_shard.segment_for_ts(entry["ts"])` (ISO-year/week).
  3. For each segment: create `ledger/<seg>/`, write `<seg>.jsonl` with the
     ORIGINAL RAW LINES verbatim in source order (no re-json.dumps drift),
     and generate `<seg>/SPINE.md`.
  4. If a target shard already exists (partial prior run), MERGE: append only
     the ids not already present, in source/ts order — never clobber.
  5. HARD verification gate: total line-count equality (shards == source) AND
     id-set equality AND every shard's entries map to that shard's segment.
  6. On success: rename `2026.jsonl` → `2026.jsonl.archive-pre-d10-shard`.

Idempotent: if `2026.jsonl` is absent and shards already exist, no-op.

Run from repo root (or worktree root):
    python3 tools/migrate_ledger_to_weekly_shards.py [--dry-run] [--ledger-dir DIR]

Writes files in-place. Does NOT commit or push — caller commits separately.
Does NOT run automatically; Phase-3 of T-0148 fires it in an Orchestrator-paused
window. Phase-1 code (the readers + ledger_shard helper) is backward-compatible
and does not require this migration to have run.

HERMES byte-sequence discipline: this script's source must not contain the
contiguous unescaped trigger sequence; any reference uses the spine-groom's
generator (it does not emit the trigger). No commit-message construction here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ledger_shard import (  # noqa: E402
    LEGACY_LEDGER_FILENAME,
    segment_for_ts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_SUFFIX = ".archive-pre-d10-shard"


class MigrationError(RuntimeError):
    """Raised on any abort condition — caller surfaces, does not auto-recover."""


def _parse_raw_lines(text: str) -> list[tuple[str, dict]]:
    """Return [(raw_line_without_trailing_newline, parsed_entry), ...].

    ABORTS (raises MigrationError) on any non-blank line that is not valid
    JSON or that lacks a usable `ts` — no silent skip.
    """
    out: list[tuple[str, dict]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"malformed JSON at line {lineno}: {exc}")
        if not isinstance(entry, dict):
            raise MigrationError(f"line {lineno}: not a JSON object")
        ts = entry.get("ts")
        if not isinstance(ts, str):
            raise MigrationError(f"line {lineno}: missing/invalid 'ts'")
        try:
            segment_for_ts(ts)
        except ValueError as exc:
            raise MigrationError(f"line {lineno}: unparseable ts {ts!r}: {exc}")
        eid = entry.get("id")
        if not isinstance(eid, str):
            raise MigrationError(f"line {lineno}: missing/invalid 'id'")
        out.append((raw, entry))
    return out


def _build_spine(segment: str, entries: list[dict]) -> str:
    """Generate per-segment SPINE.md (reuses spine_index_groom's row style).

    Imported lazily so the migration has no hard dependency cycle at module
    import time; spine_index_groom imports _git_utils (git subprocess shims)
    which the migration doesn't otherwise need.
    """
    from tools.spine_index_groom import _build_segment_spine

    return _build_segment_spine(segment, entries)


def _existing_id_counts(shard_path: Path) -> "Counter":
    """Multiset of ids already present in an existing shard (partial prior run).

    Count-based (not a set) so legitimately-duplicated ids survive a merge —
    the ledger is append-only and may carry the same id twice.
    """
    counts: Counter = Counter()
    if not shard_path.exists():
        return counts
    for raw in shard_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            counts[json.loads(raw)["id"]] += 1
        except (json.JSONDecodeError, KeyError, TypeError):
            raise MigrationError(f"existing shard {shard_path} has a malformed line")
    return counts


def _verify(
    ledger_dir: Path,
    source_pairs: list[tuple[str, dict]],
) -> None:
    """HARD gate. Raises MigrationError unless every invariant holds.

    The ledger is append-only and may legitimately carry DUPLICATE ids (e.g.
    sync-alert entries written twice with the same id). The migration is a
    byte-preserving regrouping, so the correct invariants are:
      * line-count equality (every source line lands in exactly one shard),
      * id-MULTISET equality (no entry dropped or invented — duplicates kept),
      * per-line segment-correctness (each shard's lines map to its segment).
    Id-SET equality would wrongly reject a valid ledger that contains dups.
    """
    source_id_multiset = Counter(e["id"] for _, e in source_pairs)
    shard_id_multiset: Counter = Counter()
    shard_line_total = 0

    # Walk the segment subfolders that should now exist.
    segments = sorted({segment_for_ts(e["ts"]) for _, e in source_pairs})
    for seg in segments:
        shard = ledger_dir / seg / f"{seg}.jsonl"
        if not shard.is_file():
            raise MigrationError(f"verify: expected shard missing: {shard}")
        for raw in shard.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            entry = json.loads(raw)
            shard_line_total += 1
            shard_id_multiset[entry["id"]] += 1
            # Each entry must map to the segment of the shard it lives in.
            mapped = segment_for_ts(entry["ts"])
            if mapped != seg:
                raise MigrationError(
                    f"verify: entry {entry['id']} (ts {entry['ts']}) is in shard "
                    f"{seg} but maps to {mapped}"
                )

    if shard_line_total != len(source_pairs):
        raise MigrationError(
            f"verify: line-count mismatch — source {len(source_pairs)}, "
            f"shards {shard_line_total}"
        )
    if shard_id_multiset != source_id_multiset:
        diff = (source_id_multiset - shard_id_multiset) + (shard_id_multiset - source_id_multiset)
        raise MigrationError(
            f"verify: id-multiset mismatch — differing ids {sorted(diff)[:5]}"
        )


def migrate(ledger_dir: Path, *, dry_run: bool = False) -> dict:
    """Perform (or simulate) the migration. Returns a summary dict.

    Idempotent: legacy file absent → no-op (assumes already migrated).
    """
    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    archive = ledger_dir / (LEGACY_LEDGER_FILENAME + ARCHIVE_SUFFIX)

    summary: dict = {
        "dry_run": dry_run,
        "noop": False,
        "segments": {},
        "total_entries": 0,
        "archived": False,
    }

    if not legacy.is_file():
        # Already migrated (or nothing to do).
        summary["noop"] = True
        return summary

    pairs = _parse_raw_lines(legacy.read_text(encoding="utf-8"))
    summary["total_entries"] = len(pairs)

    # Group raw lines by segment, preserving source order.
    by_segment: dict[str, list[tuple[str, dict]]] = {}
    for raw, entry in pairs:
        seg = segment_for_ts(entry["ts"])
        by_segment.setdefault(seg, []).append((raw, entry))

    for seg in sorted(by_segment):
        seg_pairs = by_segment[seg]
        summary["segments"][seg] = len(seg_pairs)
        if dry_run:
            continue

        seg_dir = ledger_dir / seg
        seg_dir.mkdir(parents=True, exist_ok=True)
        shard = seg_dir / f"{seg}.jsonl"

        if shard.exists():
            # MERGE-safe (partial prior run): append only the per-id COPIES not
            # yet present, in source order. Count-based so legitimately
            # duplicated ids (append-only ledger) are neither dropped nor
            # double-counted. Never clobber existing lines.
            present = _existing_id_counts(shard)
            remaining = Counter(present)
            to_append: list[str] = []
            for raw, e in seg_pairs:
                eid = e["id"]
                if remaining.get(eid, 0) > 0:
                    remaining[eid] -= 1  # this copy is already in the shard
                else:
                    to_append.append(raw)
            existing_text = shard.read_text(encoding="utf-8")
            # Normalize to ensure a trailing newline before appending.
            if existing_text and not existing_text.endswith("\n"):
                existing_text += "\n"
            new_text = existing_text + ("\n".join(to_append) + ("\n" if to_append else ""))
            shard.write_text(new_text, encoding="utf-8")
        else:
            # Fresh write: all raw lines verbatim, in source order (dups kept).
            shard.write_text("\n".join(raw for raw, _ in seg_pairs) + "\n", encoding="utf-8")

        # Regenerate SPINE.md from the shard's full (possibly merged) entries.
        entries_now = [
            json.loads(r)
            for r in shard.read_text(encoding="utf-8").splitlines()
            if r.strip()
        ]
        (seg_dir / "SPINE.md").write_text(_build_spine(seg, entries_now), encoding="utf-8")

    if dry_run:
        return summary

    # HARD verification gate before the irreversible rename.
    _verify(ledger_dir, pairs)

    legacy.rename(archive)
    summary["archived"] = True
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate ledger/2026.jsonl → weekly subfolder shards (T-0148)."
    )
    parser.add_argument("--dry-run", action="store_true", help="report plan, write nothing")
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=REPO_ROOT / "ledger",
        help="ledger directory (default: <repo>/ledger)",
    )
    args = parser.parse_args(argv)

    try:
        summary = migrate(args.ledger_dir, dry_run=args.dry_run)
    except MigrationError as exc:
        print(f"migrate_ledger_to_weekly_shards: ABORT — {exc}", file=sys.stderr)
        return 1

    if summary["noop"]:
        print("migrate_ledger_to_weekly_shards: no-op (legacy 2026.jsonl absent; already migrated).")
        return 0

    mode = "DRY-RUN — would write" if summary["dry_run"] else "migrated"
    print(f"migrate_ledger_to_weekly_shards: {mode} {summary['total_entries']} entries into "
          f"{len(summary['segments'])} segment(s):")
    for seg in sorted(summary["segments"]):
        print(f"  {seg}: {summary['segments'][seg]} entries")
    if summary["archived"]:
        print(f"  archived legacy → 2026.jsonl{ARCHIVE_SUFFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
