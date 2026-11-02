#!/usr/bin/env python3
"""operator_decision_throughline.py (T-5015 phase 3) — weekly trigger for the throughline pass.

Serves "the operator involved but not necessary" (TG 11821). The decision register
(notes/operator-decision-register.md) records each ruling with the rule it implies and
whether that rule is delegable. This job counts register rows not yet "passed"; when
at least --min-rows have accumulated it files ONE target=role:engineering_orchestrator ticket asking the HO to
run the throughline pass (cluster by implied rule, draft one amendment per delegable
cluster for the operator to ratify). Clustering is the HO's judgment: no LLM call here.

Watermark: `<state-dir>/throughline.json` holds a set of row hashes keyed on
Date+Ticket+Ask (see row_key), so edits to an old row's prose do not re-trigger. It
advances only after ticket.py file succeeds. Below the threshold nothing is filed and
the watermark is untouched, so rows accumulate.

The register is committed, so Ticket and Rule cells go in the ticket body. (Contrast
phase 2, whose message text is private.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REGISTER = REPO / "notes" / "operator-decision-register.md"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon" / "decision-sweep"
COLUMNS = ("date", "ticket", "ask", "ruling", "category", "why", "rule", "delegable")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

PROCEDURE = (
    "Procedure: cluster by implied rule; for each cluster whose rows say Delegable yes or "
    "partial and which no existing trust-posture amendment already covers, draft ONE "
    "amendment to `rulebook/policies/trust-posture.md` (or the governing ADR) as a "
    "committed diff and file it `target=operator intent=ratify` with `ratifiable_files`; "
    "clusters below threshold wait; record the pass in the register's 'Open questions for "
    "the throughline pass' section; resolve."
)


def parse_register(text: str) -> list[dict]:
    """Rows of the decision table as dicts keyed by COLUMNS; header/separator skipped.

    Cells split on unescaped pipes only; an escaped pipe inside a cell is unescaped
    (phase 1's writer escapes pipes that way). A row is any table line with
    len(COLUMNS) cells whose first cell is an ISO date.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        s = s[1:]
        if s.endswith("|") and not s.endswith("\\|"):
            s = s[:-1]
        cells = [c.strip().replace("\\|", "|") for c in _UNESCAPED_PIPE.split(s)]
        if not _DATE.match(cells[0]):
            continue
        if len(cells) != len(COLUMNS):
            # A dated line with the wrong cell count is a real ruling the pass would
            # otherwise miss silently (e.g. an unescaped pipe in a hand-edited cell).
            print(f"decision-throughline: WARN skipped malformed register row "
                  f"({len(cells)} cells): {s[:80]}", file=sys.stderr)
            continue
        rows.append(dict(zip(COLUMNS, cells)))
    return rows


def row_key(row: dict) -> str:
    """Stable identity: hash of Date+Ticket+Ask, so prose edits to a passed row don't re-trigger."""
    ident = "\x1f".join(row[k] for k in ("date", "ticket", "ask"))
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


def load_watermark(path: Path) -> set[str]:
    try:
        d = json.loads(path.read_text())
        return set(map(str, d.get("seen", [])))
    except (OSError, ValueError, AttributeError):
        return set()


def save_watermark(path: Path, seen: set[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"seen": sorted(seen), "count": len(seen),
               "last_date": max((r["date"] for r in rows), default="")}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".throughline-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_body(new_rows: list[dict]) -> str:
    dates = sorted(r["date"] for r in new_rows)
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in new_rows:
        groups.setdefault(r["category"] or "(none)", []).append(r)
    out = [f"{len(new_rows)} new row(s) in notes/operator-decision-register.md since the last "
           f"throughline pass, dated {dates[0]} to {dates[-1]}.", "",
           "Grouped by Category (a starting hint only; cluster by implied rule):", ""]
    for cat, rs in groups.items():
        out.append(f"{cat}:")
        for r in rs:
            out.append(f"- {r['ticket']} — {r['rule']} (delegable: {r['delegable']})")
        out.append("")
    out.append(PROCEDURE)
    return "\n".join(out)


def file_ticket(new_rows: list[dict]) -> None:
    dates = sorted(r["date"] for r in new_rows)
    cmd = [sys.executable, str(REPO / "tools" / "ticket.py"), "file", "--automated-filer", "operator_decision_throughline", "--requester", "orchestrator",
           "--target", "role:engineering_orchestrator", "--title",
           f"decision throughline pass: {len(new_rows)} new register rows ({dates[0]} to {dates[-1]})",
           "--labels", "decision-register,throughline", "--stale-after", "14d",
           "--project-id", "null",
           "--nearest-intent-ref", "maintenance:decision-register",
           "--serves", "clustering the operator's rulings into ratifiable amendments so the next matching case stops reaching him (T-5015, TG 11821)",
           "--ask", build_body(new_rows)]
    res = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ticket.py file failed rc={res.returncode}: {res.stderr.strip()[:500]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--register", type=Path, default=DEFAULT_REGISTER)
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--min-rows", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="count only; file nothing, write nothing")
    args = ap.parse_args(argv)

    try:
        rows = parse_register(args.register.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"decision-throughline: error: {e}", file=sys.stderr)
        return 2
    wm_path = args.state_dir / "throughline.json"
    seen = load_watermark(wm_path)
    new_rows = [r for r in rows if row_key(r) not in seen]
    print(f"decision-throughline: rows={len(rows)} passed={len(rows) - len(new_rows)} "
          f"new={len(new_rows)} min-rows={args.min_rows}")
    if len(new_rows) < args.min_rows or args.dry_run:
        return 0
    try:
        file_ticket(new_rows)
    except (RuntimeError, OSError) as e:
        print(f"decision-throughline: error: {e}", file=sys.stderr)
        return 1
    save_watermark(wm_path, seen | {row_key(r) for r in rows}, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
