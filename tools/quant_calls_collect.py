#!/usr/bin/env python3
"""ORG-102 calls collector (step 2).

Collects structured DECISION calls from exhaust only:
  * tickets: structured DECISION lines and legacy DECISIONS markers;
  * PR bodies: GitHub REST when available;
  * ledger: existing justifies_via entries.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import decision_lines as dl  # noqa: E402
from tools import quant_delivery_collect as qd  # noqa: E402
from tools.ledger_shard import iter_ledger_entry_files  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("/var/lib/pantheon/quant")
DEFAULT_CONFIG = REPO_ROOT / "tools" / "quant_delivery_repos.json"
LEDGER_DIR = REPO_ROOT / "ledger"
SCHEMA_FILE = REPO_ROOT / "schemas" / "quant-delivery.schema.json"
TICKET_ID_RE = re.compile(r"\bT-(\d{3,6})\b", re.IGNORECASE)
PR_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+)#(\d+)\b")
TICKET_STEM_RE = re.compile(r"^T-\d{3,6}$", re.IGNORECASE)
TRANSITION_ACTOR_RE = re.compile(r"^\s*-\s+(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+)?\[([^\]]+)\]")


def ticket_ids_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({f"T-{int(m.group(1)):04d}" for m in TICKET_ID_RE.finditer(text)})


def via_classes(via_ids: list[str] | tuple[str, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for via_id in via_ids:
        parsed = dl.normalize_via_id(via_id)
        if parsed is not None:
            counts[parsed[1]] += 1
    return dict(sorted(counts.items()))


def principle_only(via_ids: list[str] | tuple[str, ...]) -> bool:
    classes = via_classes(via_ids)
    return bool(classes) and set(classes) == {"principle"}


def transition_actor(line: str | None) -> str | None:
    if not line:
        return None
    match = TRANSITION_ACTOR_RE.match(line)
    if not match:
        return None
    actor = match.group(1).strip()
    return actor or None


def base_record(run_id: str, generated_at: str, source_type: str, source_ref: str) -> dict[str, Any]:
    return {
        "schema": "pantheon.quant.call/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "source_type": source_type,
        "source_ref": source_ref,
        "source_status": "ok",
    }


def record_from_decision(
    decision: dl.DecisionLine,
    *,
    run_id: str,
    generated_at: str,
    source_type: str,
    source_ref: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = base_record(run_id, generated_at, source_type, source_ref)
    row.update(
        {
            "parse_status": "structured",
            "call": decision.call,
            "serves": decision.serves,
            "via_ids": list(decision.via_ids),
            "via_classes": dict(sorted(decision.via_classes.items())),
            "principle_only": decision.principle_only,
            "ref": decision.ref,
            "line_no": decision.line_no,
            "problems": [],
            "raw_excerpt": decision.raw[:240],
            "raw_excerpt_sha256": dl.excerpt_hash(decision.raw)[1],
        }
    )
    row["ticket_ids"] = sorted(set(ticket_ids_from_text(decision.raw) + ticket_ids_from_text(decision.ref)))
    if extra:
        row.update(extra)
    return row


def record_from_problem(
    problem: dl.DecisionProblem,
    *,
    run_id: str,
    generated_at: str,
    source_type: str,
    source_ref: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    excerpt, digest = dl.excerpt_hash(problem.raw)
    row = base_record(run_id, generated_at, source_type, source_ref)
    row.update(
        {
            "parse_status": status,
            "call": None,
            "serves": None,
            "via_ids": [],
            "via_classes": {},
            "principle_only": False,
            "ref": None,
            "line_no": problem.line_no,
            "problems": [problem.message],
            "raw_excerpt": excerpt,
            "raw_excerpt_sha256": digest,
            "ticket_ids": ticket_ids_from_text(problem.raw),
        }
    )
    if extra:
        row.update(extra)
    return row


def records_from_text(
    text: str,
    *,
    run_id: str,
    generated_at: str,
    source_type: str,
    source_ref: str,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    scan = dl.scan_decisions(text)
    rows = [
        record_from_decision(
            decision,
            run_id=run_id,
            generated_at=generated_at,
            source_type=source_type,
            source_ref=source_ref,
            extra=extra,
        )
        for decision in scan.decisions
    ]
    for problem in scan.problems:
        status = "unstructured" if problem.line_no is None else "malformed"
        rows.append(
            record_from_problem(
                problem,
                run_id=run_id,
                generated_at=generated_at,
                source_type=source_type,
                source_ref=source_ref,
                status=status,
                extra=extra,
            )
        )
    return rows


def collect_ticket_records(run_id: str, generated_at: str, tickets_root: Path = REPO_ROOT / "tickets") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(tickets_root.glob("*/*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        tid = path.stem
        normalized_tid = ticket_ids_from_text(tid)
        extra: dict[str, Any] = {"ticket_ids": normalized_tid, "ticket_state": path.parent.name}
        if TICKET_STEM_RE.match(tid):
            extra["ticket_id"] = normalized_tid[0] if normalized_tid else tid
        source_ref = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
        lines = text.splitlines()
        for row in records_from_text(text, run_id=run_id, generated_at=generated_at, source_type="ticket", source_ref=source_ref, extra=extra):
            row["ticket_ids"] = sorted(set(row.get("ticket_ids", []) + normalized_tid))
            line_no = row.get("line_no")
            if isinstance(line_no, int) and 1 <= line_no <= len(lines):
                row["agent"] = transition_actor(lines[line_no - 1])
            rows.append(row)
    return rows


def _ledger_links(entry: dict) -> list[dict[str, Any]]:
    links = entry.get("links")
    if not isinstance(links, list):
        return []
    return [link for link in links if isinstance(link, dict)]


def _normalize_ledger_via(raw_via: list[Any]) -> tuple[list[str], list[str]]:
    via: list[str] = []
    problems: list[str] = []
    for item in raw_via:
        parsed = dl.normalize_via_id(str(item))
        if parsed is None:
            problems.append(f"invalid ledger justifies_via id {item!r}")
        else:
            via.append(parsed[0])
    return via, problems


def collect_ledger_records(run_id: str, generated_at: str, ledger_dir: Path = LEDGER_DIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ledger_dir.is_dir():
        return rows
    for shard in iter_ledger_entry_files(ledger_dir):
        try:
            lines = shard.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            try:
                entry = json.loads(raw_line)
            except ValueError:
                continue
            raw_via = entry.get("justifies_via")
            if not isinstance(raw_via, list) or not raw_via:
                continue
            via_ids, problems = _normalize_ledger_via(raw_via)
            source_ref = str(entry.get("id") or entry.get("action") or shard.name)
            call = str(entry.get("summary") or entry.get("action") or "")
            links = _ledger_links(entry)
            link_refs = [str(link.get("ref")) for link in links if link.get("ref")]
            row = base_record(run_id, generated_at, "ledger", source_ref)
            row.update(
                {
                    "parse_status": "malformed" if problems else "structured",
                    "call": call,
                    "serves": str(entry.get("rationale") or "")[:500] or None,
                    "via_ids": via_ids,
                    "via_classes": via_classes(via_ids),
                    "principle_only": principle_only(via_ids),
                    "ref": link_refs[0] if link_refs else None,
                    "ledger_id": entry.get("id"),
                    "agent": entry.get("agent"),
                    "occurred_at": entry.get("ts"),
                    "links": links,
                    "ticket_ids": sorted({tid for text in [call, *link_refs] for tid in ticket_ids_from_text(text)}),
                    "problems": problems,
                    "raw_excerpt": None,
                    "raw_excerpt_sha256": None,
                }
            )
            rows.append(row)
    return rows


def collect_pr_records(
    run_id: str,
    generated_at: str,
    repos: list[qd.RepoSpec],
    since: dt.datetime,
    *,
    offline: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    notes: dict[str, str] = {}
    token = None if offline else qd.auth_token()
    for repo in repos:
        prs, status, note = qd.fetch_github_prs(repo, since, token)
        statuses[repo.name] = status
        if note:
            notes[repo.name] = note
        if status != "ok":
            continue
        for pr in prs:
            body = pr.get("body") or ""
            source_ref = f"{repo.name}#{pr.get('number')}"
            extra = {
                "repo": repo.name,
                "pr_number": pr.get("number"),
                "ticket_ids": ticket_ids_from_text("\n".join(str(pr.get(k) or "") for k in ("title", "body", "head_ref"))),
                "occurred_at": pr.get("merged_at") or pr.get("closed_at") or pr.get("created_at"),
                "agent": pr.get("author"),
            }
            rows.extend(records_from_text(body, run_id=run_id, generated_at=generated_at, source_type="pr", source_ref=source_ref, extra=extra))
    return rows, statuses, notes


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter[str]] = {}
    for row in rows:
        source = str(row.get("source_type"))
        out.setdefault(source, Counter())[str(row.get("parse_status"))] += 1
    return {source: dict(sorted(counter.items())) for source, counter in sorted(out.items())}


def write_version(out_dir: Path, run: dict[str, Any]) -> None:
    schema_dir = out_dir / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    version = {
        "schema": "pantheon.quant.schema_manifest/v1",
        "generated_at": run["generated_at"],
        "run_id": run["run_id"],
        "schema_file": str(SCHEMA_FILE.relative_to(REPO_ROOT)),
        "feeds": {
            "calls": "pantheon.quant.call/v1",
            "runs": "pantheon.quant.run/v1",
        },
    }
    (schema_dir / "version.json").write_text(json.dumps(version, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect(days: int, config: Path, out_dir: Path, *, offline: bool = False) -> dict[str, Any]:
    generated_dt = qd.now_utc()
    generated_at = qd.iso_z(generated_dt)
    run_id = f"quant-calls-{generated_dt.strftime('%Y%m%dT%H%M%SZ')}"
    since = generated_dt - dt.timedelta(days=days)
    repos = qd.load_repo_specs(config)
    rows: list[dict[str, Any]] = []
    rows.extend(collect_ticket_records(run_id, generated_at))
    rows.extend(collect_ledger_records(run_id, generated_at))
    pr_rows, pr_status, pr_notes = collect_pr_records(run_id, generated_at, repos, since, offline=offline)
    rows.extend(pr_rows)
    rows = sorted(rows, key=lambda r: (str(r.get("source_type")), str(r.get("source_ref")), int(r.get("line_no") or 0), str(r.get("parse_status"))))
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "collector": "calls",
        "days": days,
        "repos": [repo.name for repo in repos],
        "source_status_by_repo": pr_status,
        "source_notes_by_repo": pr_notes,
        "records": {"calls": len(rows)},
        "call_counts_by_source": summarize(rows),
    }
    qd.write_jsonl(out_dir / "calls.jsonl", rows)
    qd.write_jsonl(out_dir / "runs" / f"{run_id}.jsonl", [run])
    write_version(out_dir, run)
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=60 if name == "backfill" else 1)
        p.add_argument("--out", type=Path, default=DEFAULT_OUT)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--offline", action="store_true", help="skip GitHub PR-body collection")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2
    run = collect(args.days, args.config, args.out, offline=args.offline)
    print(json.dumps(run, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
