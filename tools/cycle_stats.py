#!/usr/bin/env python3
"""cycle_stats.py — read tool for the ledger-augmented metrics field.

Per T-0193 ratification: each ledger close-entry gains a structured
`metrics` field with shape:

  metrics: {
    wall_clock_min:   int,
    plan_rebounds:    int,
    code_rebounds:    int,
    reviewer_rebounds:int,
    implementer_count:int,
    reviewer_count:   int,
    scope_loc_delta:  int,
    cycle_shape:      one of feature/refactor/migration/authoring/drift-cleanup/bugfix,
    outcome_class:    one of clean/one-rebound/multi-rebound/escalated/aborted,
  }

This tool reads ledger/<YYYY>.jsonl files, filters for close-entries
(action shape `t<NNNN>_(resolved|landed|closed)_*`) that carry a
`metrics` field, and surfaces:

  - rolling N-cycle averages by cycle_shape + outcome_class;
  - recurrence-pattern queries (which cycle_shape consistently REBOUNDs);
  - similar-cycle-shape lookup (so HO can grep before estimating).

Usage:
  cycle_stats.py metrics                        # summary across all close-entries with metrics
  cycle_stats.py metrics --shape SHAPE          # filter to one cycle_shape
  cycle_stats.py metrics --last N               # only the most recent N matching entries
  cycle_stats.py metrics --similar T-NNNN       # entries with same cycle_shape as T-NNNN
  cycle_stats.py session-resets [--days N]      # session-reset events (T-0256), cross-account
  cycle_stats.py session-resets --format json   # machine-readable output for kino-pwa
  cycle_stats.py session-resets --schema        # dump the event schema and exit

Exit codes:
  0 — successful run.
  1 — no matching ledger files OR no entries with metrics found.
  2 — argv error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# (sys.path[0]=tools/) or `python3 -m tools.foo` (sys.path[0]=repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.ledger_shard import iter_ledger_entry_files  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = REPO_ROOT / "ledger"

# Close-entry action regex: t<NNNN> followed by a verb (resolved | landed | closed | done).
CLOSE_ACTION_RE = re.compile(r"^t\d{4,5}_(resolved|landed|closed|done)_", re.IGNORECASE)

CYCLE_SHAPES = ("feature", "refactor", "migration", "authoring", "drift-cleanup", "bugfix")
OUTCOME_CLASSES = ("clean", "one-rebound", "multi-rebound", "escalated", "aborted")

# Numeric metrics fields we average across entries.
NUMERIC_FIELDS = (
    "wall_clock_min",
    "plan_rebounds",
    "code_rebounds",
    "reviewer_rebounds",
    "implementer_count",
    "reviewer_count",
    "scope_loc_delta",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_entries(ledger_dir: Path) -> list[dict]:
    """Read every ledger entry file in dir, return list of decoded entries.

    Routes through ledger_shard.iter_ledger_entry_files (T-0148) so it sees
    both the legacy flat `2026.jsonl` (pre-migration) and the per-week
    subfolder shards `ledger/<seg>/<seg>.jsonl` (post-migration), with the
    `*.archive*` legacy backup excluded. The previous flat `glob("*.jsonl")`
    did NOT descend into subfolders, so it would silently miss every shard.

    Skips malformed lines silently — the ledger is append-only and any
    write race could leave a partial line; tool stays robust.
    """
    entries: list[dict] = []
    if not ledger_dir.is_dir():
        return entries
    for shard in iter_ledger_entry_files(ledger_dir):
        try:
            for line in shard.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        except OSError:
            continue
    return entries


def is_close_entry(entry: dict) -> bool:
    """Return True if entry's action looks like a close-entry."""
    action = entry.get("action")
    if not isinstance(action, str):
        return False
    return bool(CLOSE_ACTION_RE.match(action))


def has_metrics(entry: dict) -> bool:
    """Return True if entry has a structured metrics field."""
    metrics = entry.get("metrics")
    return isinstance(metrics, dict) and "cycle_shape" in metrics


def extract_ticket_id(entry: dict) -> str | None:
    """Pull T-NNNN from the action prefix.

    Action shape: t<NNNN>_<verb>_..., e.g. t0184_landed_context_fill_monitor
    """
    action = entry.get("action")
    if not isinstance(action, str):
        return None
    m = re.match(r"^t(\d{4,5})_", action, re.IGNORECASE)
    if not m:
        return None
    return f"T-{m.group(1).zfill(4)}"


def average_metrics(entries: list[dict]) -> dict:
    """Return per-field averages across entries' metrics. Empty input → {}."""
    if not entries:
        return {}
    out: dict = {}
    for field in NUMERIC_FIELDS:
        values = [
            entry["metrics"].get(field)
            for entry in entries
            if has_metrics(entry) and isinstance(entry["metrics"].get(field), (int, float))
        ]
        if values:
            out[field] = sum(values) / len(values)
    return out


def filter_by_shape(entries: list[dict], shape: str) -> list[dict]:
    return [e for e in entries if has_metrics(e) and e["metrics"].get("cycle_shape") == shape]


def find_by_ticket(entries: list[dict], ticket_id: str) -> dict | None:
    """Return the entry whose action starts with the ticket id (case-insensitive)."""
    target = ticket_id.upper()
    for e in entries:
        ext = extract_ticket_id(e)
        if ext and ext.upper() == target:
            return e
    return None


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def report_summary(entries: list[dict]) -> str:
    """Top-level summary: count + averages by shape, count by outcome_class."""
    if not entries:
        return "no close-entries with metrics found.\n"
    lines = [f"close-entries with metrics: {len(entries)}", ""]

    # Per-shape breakdown.
    shape_counts: Counter = Counter(
        e["metrics"]["cycle_shape"] for e in entries if has_metrics(e)
    )
    lines.append("by cycle_shape:")
    for shape in sorted(shape_counts, key=lambda s: -shape_counts[s]):
        sub = filter_by_shape(entries, shape)
        avgs = average_metrics(sub)
        lines.append(f"  {shape}: {shape_counts[shape]} cycle(s)")
        for field in NUMERIC_FIELDS:
            if field in avgs:
                lines.append(f"    avg {field}: {avgs[field]:.2f}")

    # Per-outcome breakdown.
    outcome_counts: Counter = Counter(
        e["metrics"]["outcome_class"]
        for e in entries
        if has_metrics(e) and "outcome_class" in e["metrics"]
    )
    if outcome_counts:
        lines.append("")
        lines.append("by outcome_class:")
        for outcome in sorted(outcome_counts, key=lambda o: -outcome_counts[o]):
            lines.append(f"  {outcome}: {outcome_counts[outcome]} cycle(s)")

    return "\n".join(lines) + "\n"


def report_shape(entries: list[dict], shape: str) -> str:
    """Per-shape report: list of entries + averages + recurrence-pattern flag."""
    sub = filter_by_shape(entries, shape)
    if not sub:
        return f"no entries with cycle_shape={shape!r} found.\n"
    lines = [f"cycle_shape={shape}: {len(sub)} cycle(s)"]
    avgs = average_metrics(sub)
    for field in NUMERIC_FIELDS:
        if field in avgs:
            lines.append(f"  avg {field}: {avgs[field]:.2f}")

    # Outcome distribution → recurrence-pattern flag.
    outcomes = Counter(
        e["metrics"].get("outcome_class") for e in sub if "outcome_class" in e.get("metrics", {})
    )
    if outcomes:
        lines.append("  outcome distribution:")
        for outcome, n in outcomes.most_common():
            lines.append(f"    {outcome}: {n}")
        rebound_share = (
            outcomes.get("one-rebound", 0) + outcomes.get("multi-rebound", 0)
        ) / sum(outcomes.values())
        if rebound_share > 0.5:
            lines.append(f"  ⚠ recurrence flag: {rebound_share:.0%} of {shape} cycles REBOUND.")

    lines.append("")
    lines.append("recent entries:")
    for e in sub[-10:]:
        ticket = extract_ticket_id(e) or "?"
        outcome = e["metrics"].get("outcome_class", "?")
        wc = e["metrics"].get("wall_clock_min", "?")
        lines.append(f"  {ticket}  outcome={outcome}  wall_clock_min={wc}")

    return "\n".join(lines) + "\n"


def report_similar(entries: list[dict], ticket_id: str) -> str:
    """Find ticket_id's cycle_shape, return all entries with same shape."""
    target = find_by_ticket(entries, ticket_id)
    if target is None:
        return f"no ledger entry found for ticket {ticket_id}.\n"
    if not has_metrics(target):
        return f"ledger entry for {ticket_id} has no metrics field; cannot find similar.\n"
    shape = target["metrics"].get("cycle_shape")
    if not shape:
        return f"ledger entry for {ticket_id} has no cycle_shape.\n"
    return report_shape(entries, shape)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read tool for ledger metrics (T-0193) + session-reset stats (T-0256)."
    )
    sub = parser.add_subparsers(dest="cmd")

    # metrics — original T-0193 surface.
    p_metrics = sub.add_parser("metrics", help="ledger metrics field aggregation (T-0193)")
    p_metrics.add_argument("--shape", choices=CYCLE_SHAPES, help="filter to one cycle_shape")
    p_metrics.add_argument("--last", type=int, help="only the most recent N matching entries")
    p_metrics.add_argument("--similar", metavar="T-NNNN", help="entries sharing the named ticket's cycle_shape")
    p_metrics.add_argument(
        "--ledger-dir",
        type=Path,
        default=LEDGER_DIR,
        help="ledger directory (default: <repo>/ledger)",
    )

    # session-resets — T-0256.
    p_sr = sub.add_parser("session-resets", help="session-reset event stats (T-0256)")
    p_sr.add_argument("--days", type=int, default=30, help="window in days back from now (default 30)")
    p_sr.add_argument("--since", help="explicit window start, YYYY-MM-DD (overrides --days)")
    p_sr.add_argument("--until", help="explicit window end, YYYY-MM-DD")
    p_sr.add_argument("--class", dest="cls", help="filter to a single class")
    p_sr.add_argument(
        "--format", dest="fmt", choices=("text", "json", "jsonl"), default="text",
        help="output format (text=human table, json=summary obj, jsonl=raw entries)"
    )
    p_sr.add_argument("--schema", action="store_true", help="dump the event schema as JSON and exit")

    return parser.parse_args(argv)


def _run_metrics(args: argparse.Namespace) -> int:
    raw = load_entries(args.ledger_dir)
    if not raw:
        print(f"no ledger entries found in {args.ledger_dir}", file=sys.stderr)
        return 1

    closes = [e for e in raw if is_close_entry(e) and has_metrics(e)]

    if args.last and args.last > 0:
        closes = closes[-args.last :]

    if args.similar:
        sys.stdout.write(report_similar(closes, args.similar))
        return 0
    if args.shape:
        sys.stdout.write(report_shape(closes, args.shape))
        return 0
    sys.stdout.write(report_summary(closes))
    return 0


def _run_session_resets(args: argparse.Namespace) -> int:
    # Lazy-import so unrelated metrics invocations don't require the module.
    from tools.session_reset_log import (
        CLASSES,
        SCHEMA,
        count_by_class,
        count_by_class_and_trigger,
        read_events_cross_account,
    )
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    if args.schema:
        sys.stdout.write(json.dumps(SCHEMA, sort_keys=True, indent=2) + "\n")
        return 0

    # Window resolution: --since/--until take precedence; else --days back from now.
    until_epoch: int | None = None
    since_epoch: int | None = None
    if args.until:
        try:
            until_epoch = int(_dt.strptime(args.until, "%Y-%m-%d").replace(tzinfo=_tz.utc).timestamp())
        except ValueError:
            print(f"bad --until format (expect YYYY-MM-DD): {args.until!r}", file=sys.stderr)
            return 2
    if args.since:
        try:
            since_epoch = int(_dt.strptime(args.since, "%Y-%m-%d").replace(tzinfo=_tz.utc).timestamp())
        except ValueError:
            print(f"bad --since format (expect YYYY-MM-DD): {args.since!r}", file=sys.stderr)
            return 2
    if since_epoch is None and args.days and args.days > 0:
        since_epoch = int(_time.time()) - args.days * 86400

    classes_filter = [args.cls] if args.cls else None
    if classes_filter and args.cls not in CLASSES:
        print(f"unknown class: {args.cls!r}; expected one of {CLASSES}", file=sys.stderr)
        return 2

    entries = read_events_cross_account(
        classes=classes_filter,
        since_epoch=since_epoch,
        until_epoch=until_epoch,
    )

    if args.fmt == "jsonl":
        for e in entries:
            sys.stdout.write(json.dumps(e, sort_keys=True) + "\n")
        return 0

    counts = count_by_class(entries)
    breakdown = count_by_class_and_trigger(entries)
    if args.fmt == "json":
        out = {
            "window": {
                "since_epoch": since_epoch,
                "until_epoch": until_epoch,
                "days": args.days if since_epoch and not args.since else None,
            },
            "total": len(entries),
            "counts_by_class": counts,
            "counts_by_class_and_trigger": [
                {"class": k[0], "trigger": k[1], "count": v}
                for k, v in sorted(breakdown.items())
            ],
            "schema_version": SCHEMA["version"],
        }
        sys.stdout.write(json.dumps(out, sort_keys=True, indent=2) + "\n")
        return 0

    # text format — human table
    lines = []
    lines.append(f"session-resets — window: ", )
    if args.since or args.until:
        win = f"since={args.since or '-'} until={args.until or 'now'}"
    else:
        win = f"last {args.days}d"
    lines[-1] += win
    lines.append(f"total: {len(entries)}")
    lines.append("")
    lines.append("by class:")
    for cls in CLASSES:
        if classes_filter and cls != args.cls:
            continue
        lines.append(f"  {cls:<32s} {counts[cls]}")
    if breakdown:
        lines.append("")
        lines.append("by class + trigger:")
        for (cls, trig), n in sorted(breakdown.items()):
            if classes_filter and cls != args.cls:
                continue
            lines.append(f"  {cls:<32s} {trig:<32s} {n}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.cmd == "metrics":
        return _run_metrics(args)
    if args.cmd == "session-resets":
        return _run_session_resets(args)
    print(
        "usage: cycle_stats.py {metrics|session-resets} [...]\n"
        "  metrics        — ledger metrics aggregation (T-0193)\n"
        "  session-resets — session-reset event stats (T-0256)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
