#!/usr/bin/env python3
"""retro_metrics.py — one-time best-effort retro-extraction of the cycle-stats
`metrics` field from existing ledger close-entry rationale prose (T-0204).

Per T-0193 ratification (the operator msg_1214 #4): prose traceability survives in
the original entries, so existing 2026.jsonl close-entries can be re-digested
into the structured metrics field via a one-time pass. This tool runs that
pass and emits a *sidecar* JSON for review. Apply path is deliberately NOT in
this tool — the ledger is append-only (see `ledger/README.md`), so the
decision between in-place mutation vs. `metrics_backfill_t<NNNN>` append
entries is a follow-up call once the sidecar data is in hand.

Run:

    python3 tools/retro_metrics.py [--ledger ledger/2026.jsonl]
                                   [--out ledger/2026.metrics-augment.sidecar.json]

The tool reads close-entries lacking a `metrics` field (action shape
`t<NNNN>_(resolved|landed|closed|done)_*` matching cycle_stats.py's
CLOSE_ACTION_RE), runs a deliberately conservative regex set over each
rationale, and emits:

  - extracted fields with high-confidence matches
  - flagged_fields for everything the regex set couldn't surface cleanly
    (HO does a manual pass on those, per the ticket Ask)

Pattern hit-rates are intentionally low — most ledger close-entries don't
embed metrics numerically in prose. Flagged is the default; extracted is the
exception.

Exit codes:
  0 — successful extraction + sidecar written.
  1 — ledger file missing or contains no close-entries without metrics.
  2 — argv error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make tools.X imports resolve whether invoked as `python3 tools/retro_metrics.py`
# or `python3 -m tools.retro_metrics`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.ledger_shard import iter_ledger_entry_files  # noqa: E402

# Mirror cycle_stats.py's close-entry filter so the two tools agree on scope.
CLOSE_ACTION_RE = re.compile(r"^t\d{4,5}_(resolved|landed|closed|done)_", re.IGNORECASE)

DEFAULT_LEDGER = _REPO_ROOT / "ledger" / "2026.jsonl"
DEFAULT_OUT = _REPO_ROOT / "ledger" / "2026.metrics-augment.sidecar.json"

# Per the ratified schema in sop/standard/cycle-stats-augmentation.md.
ALL_METRIC_FIELDS = (
    "wall_clock_min",
    "plan_rebounds",
    "code_rebounds",
    "reviewer_rebounds",
    "implementer_count",
    "reviewer_count",
    "scope_loc_delta",
    "cycle_shape",
    "outcome_class",
)

# ---------------------------------------------------------------------------
# Extractors. Each returns either a value or None (= not confidently
# extractable from prose; flag for manual). Each is deliberately conservative:
# false-flag (HO does the manual pass) is cheap; false-extract (silently wrong
# number lands in the ledger) is expensive.
# ---------------------------------------------------------------------------


def extract_wall_clock_min(prose: str) -> int | None:
    """Pull wall-clock duration in minutes from prose.

    Recognised shapes (case-insensitive):
      - "~50min wall-clock", "30min wall-clock"
      - "~1h45min wall-clock", "1hr30min wall-clock"
      - "~1 hour wall-clock", "2h wall-clock"
      - "wall-clock: ~30min" / "wall-clock ~30min"
    """
    # Combined hours + minutes: "1h45min wall-clock" / "1hr30min wall-clock"
    m = re.search(
        r"~?\s*(\d+)\s*(?:h|hr|hour)s?\s*(\d+)\s*(?:min|minute)s?\s+wall[- ]?clock",
        prose,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # Pure hours preceding "wall-clock"
    m = re.search(
        r"~?\s*(\d+)\s*(?:h|hr|hour)s?\s+wall[- ]?clock",
        prose,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1)) * 60

    # Pure minutes preceding "wall-clock"
    m = re.search(
        r"~?\s*(\d+)\s*(?:min|minute)s?\s+wall[- ]?clock",
        prose,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1))

    # Reversed: "wall-clock: ~30min" / "wall-clock ~1h"
    m = re.search(
        r"wall[- ]?clock\s*[:~]?\s*~?\s*(\d+)\s*(min|minute|h|hr|hour)s?",
        prose,
        re.IGNORECASE,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return n if unit.startswith("min") else n * 60

    return None


def extract_rebound_counts(prose: str) -> dict[str, int | None]:
    """Parse REBOUND counts from prose. Returns dict with three keys
    (plan/code/reviewer rebounds). Values are int when matched, None when
    prose doesn't surface them.

    Recognised shapes:
      - "0/3 plan REBOUNDs + 0/3 code REBOUNDs + 0 reviewer REBOUNDs"
      - "0/3 REBOUNDs at any tier" → all three set to 0
      - "zero rebounds" / "0 REBOUNDs" → all three set to 0 (cycle-wide)
      - "clean" alone is NOT enough (too ambiguous; flag for HO)
    """
    out: dict[str, int | None] = {
        "plan_rebounds": None,
        "code_rebounds": None,
        "reviewer_rebounds": None,
    }

    # Tier-specific: "<n>/3 <tier> REBOUND" or "<n> <tier> REBOUND".
    for tier_word, key in (
        ("plan", "plan_rebounds"),
        ("code", "code_rebounds"),
        ("reviewer", "reviewer_rebounds"),
    ):
        m = re.search(
            rf"(\d+)(?:/\d+)?\s+{tier_word}\s+rebound",
            prose,
            re.IGNORECASE,
        )
        if m:
            out[key] = int(m.group(1))

    # Cycle-wide zero: "0/3 REBOUNDs at any tier" or "zero REBOUNDs"
    # Only apply if no tier-specific values were extracted.
    if all(v is None for v in out.values()):
        if re.search(r"\b0/3\s*REBOUND.*\bany\s+tier\b", prose, re.IGNORECASE):
            return {"plan_rebounds": 0, "code_rebounds": 0, "reviewer_rebounds": 0}
        if re.search(r"\b(?:zero|0)\s+REBOUND", prose, re.IGNORECASE):
            return {"plan_rebounds": 0, "code_rebounds": 0, "reviewer_rebounds": 0}

    return out


def extract_implementer_count(prose: str) -> int | None:
    """Pull implementer subagent count. Requires a noun-suffix that confirms
    we're counting agents, not modifiers (avoids 'b30 implementer model',
    '3 implementer-added subprocess-fixture', etc.).
    """
    m = re.search(
        r"(\d+)\s+implementer(?:s|\s+subagents?|\s+one-shots?|\s+sub-?cycles?|\s+passes?|\s+dispatch(?:es)?)\b",
        prose,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1))
    return None


def extract_reviewer_count(prose: str) -> int | None:
    """Pull reviewer subagent count with the same noun-suffix discipline. The
    "reviewer-tier REBOUND" phrasing is excluded because that's a REBOUND
    count, not a reviewer count.
    """
    m = re.search(
        r"(\d+)\s+reviewer(?:s|\s+subagents?|\s+one-shots?|\s+sub-?cycles?|\s+passes?|\s+dispatch(?:es)?)\b",
        prose,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1))
    return None


def extract_loc(prose: str) -> int | None:
    """Pull LoC from "~480 LoC" / "120 LoC" / "~150 lines of code" patterns.

    If multiple LoC numbers appear (e.g., script + tests), sum them — the
    ratified scope_loc_delta is insertions+deletions across the cycle's
    deliverable paths, so a "script 200 LoC + tests 300 LoC" prose maps to
    500.
    """
    matches = re.findall(
        r"~?\s*(\d+)\s*(?:LoC|lines?\s+of\s+code)\b",
        prose,
        re.IGNORECASE,
    )
    if not matches:
        return None
    return sum(int(n) for n in matches)


def derive_outcome_class(rebounds: dict[str, int | None]) -> str | None:
    """Map rebound counts → outcome_class per the ratified vocab.

    Returns None when any rebound field is None (can't compute total).
    Aborted cycles aren't derivable from rebound counts — they need prose
    classification; flag for manual instead.
    """
    if any(v is None for v in rebounds.values()):
        return None
    total = sum(rebounds.values())  # type: ignore[arg-type]
    if total == 0:
        return "clean"
    if total == 1:
        return "one-rebound"
    if total == 2:
        return "multi-rebound"
    return "escalated"  # 3+ REBOUNDs total = budget exhausted per D7


def extract(entry: dict) -> tuple[dict, list[str]]:
    """Run all extractors over one close-entry. Returns (extracted_metrics,
    flagged_fields_for_manual_pass).
    """
    rationale = entry.get("rationale", "")
    extracted: dict = {}

    wall = extract_wall_clock_min(rationale)
    if wall is not None:
        extracted["wall_clock_min"] = wall

    rebounds = extract_rebound_counts(rationale)
    for k, v in rebounds.items():
        if v is not None:
            extracted[k] = v

    impl_n = extract_implementer_count(rationale)
    if impl_n is not None:
        extracted["implementer_count"] = impl_n

    rev_n = extract_reviewer_count(rationale)
    if rev_n is not None:
        extracted["reviewer_count"] = rev_n

    loc = extract_loc(rationale)
    if loc is not None:
        extracted["scope_loc_delta"] = loc

    outcome = derive_outcome_class(rebounds)
    if outcome is not None:
        extracted["outcome_class"] = outcome

    # cycle_shape is intentionally NOT regex-extracted — the SOP says
    # "pick the *intent* category" which is judgment-laden classification;
    # always flag for HO's manual pass.
    flagged = [f for f in ALL_METRIC_FIELDS if f not in extracted]
    return extracted, flagged


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_close_entries_without_metrics(ledger_path: Path) -> list[dict]:
    """Read the ledger, return close-entries lacking a metrics field.

    `ledger_path` may be a single JSONL file (legacy / explicit --ledger
    override — read directly) OR the ledger directory, in which case the
    union of entry files is read via ledger_shard.iter_ledger_entry_files
    (T-0148: both the legacy flat 2026.jsonl pre-migration and the per-week
    subfolder shards post-migration, archive backups excluded).
    """
    if ledger_path.is_dir():
        files = iter_ledger_entry_files(ledger_path)
    elif ledger_path.is_file():
        files = [ledger_path]
    else:
        files = []

    out: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not CLOSE_ACTION_RE.match(entry.get("action", "")):
                continue
            if "metrics" in entry and isinstance(entry["metrics"], dict):
                continue
            out.append(entry)
    return out


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_sidecar(ledger_path: Path, entries: list[dict]) -> dict:
    rows: dict[str, dict] = {}
    for e in entries:
        extracted, flagged = extract(e)
        rows[e["id"]] = {
            "action": e["action"],
            "ts": e["ts"],
            "extracted": extracted,
            "flagged_fields": flagged,
        }
    try:
        ledger_path_str = str(ledger_path.relative_to(_REPO_ROOT))
    except ValueError:
        ledger_path_str = str(ledger_path)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "tools/retro_metrics.py (T-0204)",
        "ledger_path": ledger_path_str,
        # The single-file sha pins the source for a single-file source. When
        # reading the directory union (post-migration, T-0148) there is no one
        # file to hash; record null rather than crash on a directory path.
        "ledger_sha256_at_extract": (
            sha256_of_file(ledger_path) if ledger_path.is_file() else None
        ),
        "schema_ref": "sop/standard/cycle-stats-augmentation.md",
        "review_state": "unreviewed",
        "apply_path": "TBD — append-only ledger forbids in-place mutation; the apply mechanism (metrics_backfill_* entries vs. one-time schema migration) is a follow-up decision once this sidecar is reviewed.",
        "entries": rows,
    }


def summarize(sidecar: dict) -> str:
    rows = sidecar["entries"]
    n = len(rows)
    field_hits = {f: 0 for f in ALL_METRIC_FIELDS}
    for row in rows.values():
        for f in row["extracted"]:
            field_hits[f] += 1
    lines = [
        f"retro_metrics: scanned {n} close-entries lacking metrics field",
        "  field hit-rates (extracted / flagged-for-manual):",
    ]
    for f in ALL_METRIC_FIELDS:
        hits = field_hits[f]
        lines.append(f"    {f:<22s} {hits:>3d} / {n - hits:>3d}")
    return "\n".join(lines) + "\n"


def write_sidecar(sidecar: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retro-extract cycle-stats metrics field from existing ledger close-entry prose (T-0204).",
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    ledger_source = args.ledger
    # Post-migration fallback: if the default legacy 2026.jsonl no longer
    # exists (it has been archived to *.archive-pre-d10-shard), read the
    # ledger directory union instead (T-0148). An explicit --ledger override
    # to a present file is honored as-is.
    if ledger_source == DEFAULT_LEDGER and not ledger_source.is_file():
        ledger_source = DEFAULT_LEDGER.parent

    entries = load_close_entries_without_metrics(ledger_source)
    if not entries:
        print(f"retro_metrics: no close-entries without metrics in {ledger_source}", file=sys.stderr)
        return 1

    sidecar = build_sidecar(ledger_source, entries)
    write_sidecar(sidecar, args.out)
    print(summarize(sidecar))
    print(f"sidecar written: {args.out.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
