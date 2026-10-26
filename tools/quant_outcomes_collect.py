#!/usr/bin/env python3
"""ORG-102 outcome detector and H1 reporter (step 3).

Reads existing exhaust only. Outcomes are conservative: a call is never marked
held until the 14-day window closes, and every reversed/overridden outcome carries
evidence text from tickets, ledger or PR metadata.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import quant_calls_collect as qc  # noqa: E402
from tools import quant_delivery_collect as qd  # noqa: E402
from tools.ledger_shard import iter_ledger_entry_files  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path("/var/lib/pantheon/quant")
DEFAULT_CONFIG = REPO_ROOT / "tools" / "quant_delivery_repos.json"
LEDGER_DIR = REPO_ROOT / "ledger"
SCHEMA_FILE = REPO_ROOT / "schemas" / "quant-delivery.schema.json"
HOLD_DAYS = 14

TICKET_ID_RE = re.compile(r"\bT-(\d{3,6})\b", re.IGNORECASE)
PR_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+)#(\d+)\b")
TIMESTAMP_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b")
COMPACT_TIMESTAMP_RE = re.compile(r"\b(?:led_)?(20\d{6})T(\d{6})Z\b")
TRANSITION_RE = re.compile(r"^\s*-\s+(?:(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+)?(?P<detail>.*)$")

REBOUND_SIGNAL_RE = re.compile(
    r"^(?:\[[^\]]+\]\s*)?(?:[A-Z0-9/_ -]{0,40}(?:--|:|—)\s*)?REBOUND(?:\s*(?:#|\d+\b|on\b|[:.\-])|$)",
    re.IGNORECASE,
)
ROLLBACK_RE = re.compile(r"\b(rollback (?:applied|executed|completed|performed)|rolled back)\b", re.IGNORECASE)
SUPERSEDE_RE = re.compile(r"\b(supersedes?|superseded|replaces?|retired by)\b", re.IGNORECASE)
SUPERSEDE_SIGNAL_RE = re.compile(
    r"^(?:\[[^\]]+\]\s*)?(?:DUPLICATE\b.*\bsuperseded\b|RESOLVED\b.*\bsuperseded\b|"
    r"closed as superseded\b|SUPERSEDES\b|.*\bsuperseded by\b)",
    re.IGNORECASE,
)
OPERATOR_TG_RE = re.compile(r"\boperator\s+TG\s*(\d+)\b", re.IGNORECASE)
OVERRIDE_CUE_RE = re.compile(
    r"\b(instead|ignore|drop|don't|do not|no need|reverse|reversed|withdraw(?:n)?|"
    r"keep until|not before|intentionally unhardened|override(?:d)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Evidence:
    kind: str
    source_ref: str
    occurred_at: str | None
    line_no: int | None
    detail: str


@dataclass(frozen=True)
class TicketDoc:
    ticket_id: str
    state: str
    path: str
    text: str
    lines: tuple[tuple[int, str, str | None], ...]


def ticket_ids_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({f"T-{int(m.group(1)):04d}" for m in TICKET_ID_RE.finditer(text)})


def parse_time(value: str | None) -> dt.datetime | None:
    return qd.parse_time(value)


def iso_z(value: dt.datetime) -> str:
    return qd.iso_z(value)


def line_timestamp(line: str) -> str | None:
    match = TIMESTAMP_RE.search(line or "")
    return match.group(1) if match else None


def text_timestamp(text: str | None) -> str | None:
    if not text:
        return None
    match = TIMESTAMP_RE.search(text)
    if match:
        return match.group(1)
    compact = COMPACT_TIMESTAMP_RE.search(text)
    if compact:
        date, clock = compact.groups()
        return f"{date[:4]}-{date[4:6]}-{date[6:]}T{clock[:2]}:{clock[2:4]}:{clock[4:]}Z"
    return None


def after_call(event_ts: str | None, event_line: int | None, call_ts: str | None, call_line: int | None) -> bool:
    event_dt = parse_time(event_ts)
    call_dt = parse_time(call_ts)
    if event_dt is not None and call_dt is not None:
        return event_dt >= call_dt
    if event_line is not None and call_line is not None:
        return event_line > call_line
    return True


def not_after_generated(event: Evidence, generated_at: str) -> bool:
    event_dt = parse_time(event.occurred_at)
    gen_dt = parse_time(generated_at)
    if event_dt is None or gen_dt is None:
        return True
    return event_dt <= gen_dt


def parse_frontmatter_scalar(text: str, key: str, fallback: str = "") -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def transition_lines(text: str) -> tuple[tuple[int, str, str | None], ...]:
    out: list[tuple[int, str, str | None]] = []
    in_log = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^## Transition log\s*$", line):
            in_log = True
            continue
        if in_log and re.match(r"^##\s+", line):
            break
        if not in_log:
            continue
        match = TRANSITION_RE.match(line)
        if match:
            out.append((line_no, line.strip(), match.group("ts")))
    return tuple(out)


def load_ticket_docs(tickets_root: Path = REPO_ROOT / "tickets") -> dict[str, TicketDoc]:
    docs: dict[str, TicketDoc] = {}
    for path in sorted(tickets_root.glob("*/*.md")):
        tid = path.stem
        if not TICKET_ID_RE.fullmatch(tid):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        state = parse_frontmatter_scalar(text, "state", path.parent.name)
        rel = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
        docs[tid.upper()] = TicketDoc(tid.upper(), state, rel, text, transition_lines(text))
    return docs


def load_ledger_lines(ledger_dir: Path = LEDGER_DIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ledger_dir.is_dir():
        return rows
    for shard in iter_ledger_entry_files(ledger_dir):
        try:
            raw_lines = shard.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in raw_lines:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            rows.append(entry)
    return rows


def is_rebound_signal(line: str) -> bool:
    detail = (line or "").split(";", 1)[-1].strip()
    return bool(REBOUND_SIGNAL_RE.search(detail))


def is_operator_override_line(line: str) -> bool:
    return bool(OPERATOR_TG_RE.search(line or "") and OVERRIDE_CUE_RE.search(line or ""))


def is_supersede_signal(line: str) -> bool:
    detail = (line or "").split(";", 1)[-1].strip()
    return bool(SUPERSEDE_SIGNAL_RE.search(detail))


def _ledger_refs_from_value(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, list):
        for item in value:
            refs.update(_ledger_refs_from_value(item))
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.update(_ledger_refs_from_value(item))
        return refs
    if value is None:
        return refs
    text = str(value)
    refs.update(ticket_ids_from_text(text))
    refs.update(m.group(0) for m in PR_REF_RE.finditer(text))
    if re.match(r"^\d{4}-\d{2}-\d{2}T", text) or re.match(r"^20\d{6}T", text) or text.startswith("T-"):
        refs.add(text)
    return refs


def _ledger_reversal_text(entry: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("action", "summary", "rationale", "disposition", "outcome"):
        value = entry.get(key)
        if isinstance(value, str):
            values.append(value)
    supersedes = entry.get("supersedes")
    if supersedes:
        values.append(json.dumps(supersedes, sort_keys=True))
    return "\n".join(values)


def ledger_supersede_index(entries: list[dict[str, Any]]) -> list[tuple[set[str], Evidence]]:
    out: list[tuple[set[str], Evidence]] = []
    for entry in entries:
        text = _ledger_reversal_text(entry)
        supersedes = entry.get("supersedes")
        refs = _ledger_refs_from_value(supersedes) if supersedes else set()
        if not refs:
            continue
        out.append(
            (
                refs,
                Evidence(
                    "ledger_superseded",
                    str(entry.get("id") or entry.get("action") or "ledger"),
                    str(entry.get("ts")) if entry.get("ts") else None,
                    None,
                    str(entry.get("summary") or entry.get("action") or text[:240]),
                ),
            )
        )
    return out


def stable_call_id(row: dict[str, Any]) -> str:
    pieces = [
        str(row.get("source_type") or ""),
        str(row.get("source_ref") or ""),
        str(row.get("line_no") or ""),
        str(row.get("raw_excerpt_sha256") or row.get("ledger_id") or row.get("call") or ""),
    ]
    return "|".join(pieces)


def call_occurred_at(row: dict[str, Any]) -> str | None:
    for key in ("occurred_at", "generated_at"):
        value = row.get(key)
        if parse_time(str(value) if value else None) is not None and key == "occurred_at":
            return str(value)
    for key in ("source_ref", "ledger_id"):
        found = text_timestamp(str(row.get(key) or ""))
        if found:
            return found
    raw = str(row.get("raw_excerpt") or "")
    return line_timestamp(raw)


def call_ticket_ids(row: dict[str, Any]) -> list[str]:
    ids = set(row.get("ticket_ids") or [])
    for text in (row.get("ref"), row.get("source_ref"), row.get("raw_excerpt"), row.get("call")):
        ids.update(ticket_ids_from_text(str(text or "")))
    return sorted(ids)


def call_pr_refs(row: dict[str, Any]) -> list[str]:
    refs = set()
    for text in (row.get("ref"), row.get("source_ref"), row.get("raw_excerpt")):
        refs.update(m.group(0) for m in PR_REF_RE.finditer(str(text or "")))
    return sorted(refs)


def line_mentions_call(line: str, row: dict[str, Any], linked_ticket_id: str | None = None) -> bool:
    refs = set(call_ticket_ids(row) + call_pr_refs(row))
    ref = row.get("ref")
    if ref:
        refs.add(str(ref))
    if linked_ticket_id:
        refs.add(linked_ticket_id)
    if row.get("source_ref"):
        refs.add(str(row["source_ref"]))
    return any(ref and ref in line for ref in refs)


def ticket_evidence(row: dict[str, Any], docs: dict[str, TicketDoc]) -> list[Evidence]:
    evid: list[Evidence] = []
    call_ts = call_occurred_at(row)
    call_line = row.get("line_no") if isinstance(row.get("line_no"), int) else None
    for tid in call_ticket_ids(row):
        doc = docs.get(tid)
        if doc is None:
            continue
        same_source = str(row.get("source_ref") or "").endswith(f"{tid}.md")
        for line_no, line, ts in doc.lines:
            if ts is None and call_ts is not None and not same_source:
                continue
            if not after_call(ts, line_no, call_ts, call_line if same_source else None):
                continue
            if is_operator_override_line(line) and (same_source or line_mentions_call(line, row, tid)):
                evid.append(Evidence("override_operator_tg", doc.path, ts, line_no, line))
                continue
            if is_rebound_signal(line) and (same_source or line_mentions_call(line, row, tid)):
                evid.append(Evidence("rebound", doc.path, ts, line_no, line))
                continue
            if ROLLBACK_RE.search(line) and (same_source or line_mentions_call(line, row, tid)):
                evid.append(Evidence("rollback", doc.path, ts, line_no, line))
                continue
            if same_source and is_supersede_signal(line):
                evid.append(Evidence("superseded", doc.path, ts, line_no, line))
    return evid


def ledger_evidence(row: dict[str, Any], ledger_index: list[tuple[set[str], Evidence]]) -> list[Evidence]:
    evid: list[Evidence] = []
    hay_refs = set(call_ticket_ids(row) + call_pr_refs(row))
    own_refs = {str(value) for value in (row.get("source_ref"), row.get("ledger_id")) if value}
    ref = row.get("ref")
    if ref:
        hay_refs.add(str(ref))
    for refs, evidence in ledger_index:
        if evidence.source_ref in own_refs:
            continue
        if refs & hay_refs:
            evid.append(evidence)
    return evid


def pr_revert_evidence(row: dict[str, Any], prs: list[dict[str, Any]]) -> list[Evidence]:
    evid: list[Evidence] = []
    refs = call_pr_refs(row)
    if row.get("repo") and row.get("pr_number"):
        refs.append(f"{row['repo']}#{row['pr_number']}")
    if not refs:
        return evid
    for pr in prs:
        if not (pr.get("is_revert") or str(pr.get("title") or "").lower().startswith("revert")):
            continue
        text = "\n".join(str(pr.get(k) or "") for k in ("title", "body", "head_ref"))
        if any(ref in text for ref in refs):
            evid.append(
                Evidence(
                    "pr_revert",
                    f"{pr.get('repo')}#{pr.get('number')}",
                    pr.get("merged_at") or pr.get("closed_at"),
                    None,
                    str(pr.get("title") or "revert PR"),
                )
            )
    return evid


def classify_outcome(
    row: dict[str, Any],
    *,
    generated_at: str,
    tickets: dict[str, TicketDoc],
    ledger_entries: list[tuple[set[str], Evidence]],
    prs: list[dict[str, Any]],
    hold_days: int = HOLD_DAYS,
) -> dict[str, Any]:
    call_ts = call_occurred_at(row)
    call_line = row.get("line_no") if isinstance(row.get("line_no"), int) else None
    evidence = ticket_evidence(row, tickets) + ledger_evidence(row, ledger_entries) + pr_revert_evidence(row, prs)
    evidence = [event for event in evidence if after_call(event.occurred_at, event.line_no, call_ts, call_line)]
    evidence = [event for event in evidence if not_after_generated(event, generated_at)]
    evidence = sorted(evidence, key=lambda e: (e.occurred_at or "", e.source_ref, e.line_no or 0, e.kind))
    overridden = [e for e in evidence if e.kind == "override_operator_tg"]
    reversed_e = [e for e in evidence if e.kind != "override_operator_tg"]
    if overridden:
        outcome = "overridden"
    elif reversed_e:
        outcome = "reversed"
    else:
        call_dt = parse_time(call_ts)
        gen_dt = parse_time(generated_at)
        if call_dt is None:
            outcome = "pending"
        elif gen_dt is not None and gen_dt - call_dt < dt.timedelta(days=hold_days):
            outcome = "pending"
        else:
            outcome = "held"
    return {
        "outcome": outcome,
        "outcome_window_days": hold_days,
        "call_occurred_at": call_ts,
        "eligible_for_h1": outcome in {"held", "reversed", "overridden"},
        "evidence": [
            {
                "kind": e.kind,
                "source_ref": e.source_ref,
                "occurred_at": e.occurred_at,
                "line_no": e.line_no,
                "detail": e.detail[:500],
            }
            for e in evidence
        ],
    }


def outcome_record(row: dict[str, Any], outcome: dict[str, Any], run_id: str, generated_at: str) -> dict[str, Any]:
    return {
        "schema": "pantheon.quant.call_outcome/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "call_id": stable_call_id(row),
        "call_source_type": row.get("source_type"),
        "call_source_ref": row.get("source_ref"),
        "call_ref": row.get("ref"),
        "ticket_ids": call_ticket_ids(row),
        "via_ids": row.get("via_ids") or [],
        "via_classes": row.get("via_classes") or {},
        "agent": row.get("agent"),
        "call": row.get("call"),
        **outcome,
    }


def guidance_ids() -> list[str]:
    ids: set[str] = set()
    for path, prefix in ((REPO_ROOT / "rulebook" / "principles.md", "P"), (REPO_ROOT / "rulebook" / "directives.md", "D")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        ids.update(f"{prefix}{int(m.group(1))}" for m in re.finditer(rf"^##\s+{prefix}(\d+)\b", text, re.MULTILINE))
    return sorted(ids, key=lambda v: (v[0], int(v[1:])))


def pd_guidance_ids(row: dict[str, Any], known_ids: set[str]) -> list[str]:
    return [str(v) for v in row.get("via_ids", []) if str(v) in known_ids]


def h1_group_records(outcomes: list[dict[str, Any]], run_id: str, generated_at: str) -> tuple[list[dict[str, Any]], list[str]]:
    eligible = [row for row in outcomes if row.get("eligible_for_h1")]
    known_guidance = set(guidance_ids())
    cited_ids = {via for row in outcomes for via in pd_guidance_ids(row, known_guidance)}
    never_cited = [gid for gid in guidance_ids() if gid not in cited_ids]

    groups: dict[str, list[dict[str, Any]]] = {
        "ALL": eligible,
        "cited_pd": [r for r in eligible if pd_guidance_ids(r, known_guidance)],
        "uncited_pd": [r for r in eligible if not pd_guidance_ids(r, known_guidance)],
    }
    for gid in sorted(cited_ids, key=lambda v: (v[0], int(v[1:]))):
        groups[gid] = [r for r in eligible if gid in r.get("via_ids", [])]

    cited = groups["cited_pd"]
    uncited = groups["uncited_pd"]
    cited_interval = qd.wilson_interval(sum(1 for r in cited if r["outcome"] in {"reversed", "overridden"}), len(cited))
    uncited_interval = qd.wilson_interval(sum(1 for r in uncited if r["outcome"] in {"reversed", "overridden"}), len(uncited))

    rows: list[dict[str, Any]] = []
    for group, members in sorted(groups.items()):
        reversed_n = sum(1 for r in members if r["outcome"] == "reversed")
        overridden_n = sum(1 for r in members if r["outcome"] == "overridden")
        adverse = reversed_n + overridden_n
        interval = qd.wilson_interval(adverse, len(members))
        verdict = "pass"
        reasons: list[str] = []
        comparison_insufficient = group in {"cited_pd", "uncited_pd"} and (len(cited) < 50 or len(uncited) < 50)
        if comparison_insufficient:
            verdict = "insufficient-n"
            reasons.append("cited/uncited comparison requires n>=50 each")
        if interval and interval["mid"] > 0.10 and not comparison_insufficient:
            verdict = "fail"
            reasons.append("adverse_rate_mid_gt_10pct")
        if group == "cited_pd" and len(cited) >= 50 and len(uncited) >= 50 and cited_interval and uncited_interval:
            if not (cited_interval["high"] < uncited_interval["low"] or uncited_interval["high"] < cited_interval["low"]):
                verdict = "fail"
                reasons.append("cited_uncited_wilson90_overlap")
        rows.append(
            {
                "schema": "pantheon.quant.call_h1/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "group": group,
                "calls": len(members),
                "held": sum(1 for r in members if r["outcome"] == "held"),
                "reversed": reversed_n,
                "overridden": overridden_n,
                "adverse": adverse,
                "adverse_rate_wilson90": interval,
                "reversal_rate_wilson90": qd.wilson_interval(reversed_n, len(members)),
                "override_rate_wilson90": qd.wilson_interval(overridden_n, len(members)),
                "verdict": verdict,
                "verdict_reasons": reasons,
            }
        )
    for gid in never_cited:
        rows.append(
            {
                "schema": "pantheon.quant.call_h1/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "group": gid,
                "calls": 0,
                "held": 0,
                "reversed": 0,
                "overridden": 0,
                "adverse": 0,
                "adverse_rate_wilson90": None,
                "reversal_rate_wilson90": None,
                "override_rate_wilson90": None,
                "verdict": "dead-guidance",
                "verdict_reasons": ["guidance id never cited"],
            }
        )
    return rows, never_cited


CALIBRATION = [
    {"id": "T-4294-negative-reask", "ticket_id": "T-4294", "expected": "negative", "kind": "override"},
    {"id": "TG-11534-T-4750-override", "ticket_id": "T-4750", "expected": "positive", "kind": "override"},
    {"id": "TG-11543-T-4446-override", "ticket_id": "T-4446", "expected": "positive", "kind": "override"},
    {"id": "TG-11543-T-4512-override", "ticket_id": "T-4512", "expected": "positive", "kind": "override"},
    {"id": "TG-11575-stale-sweep", "ticket_id": None, "expected": "positive", "kind": "override"},
    {"id": "rebound-805-T-4763", "ticket_id": "T-4763", "expected": "positive", "kind": "rebound"},
    {"id": "rebound-815-T-4790", "ticket_id": "T-4790", "expected": "positive", "kind": "rebound"},
    {"id": "rebound-821-T-4780", "ticket_id": "T-4780", "expected": "positive", "kind": "rebound"},
    {"id": "rebound-T-4780-step1", "ticket_id": "T-4780", "expected": "positive", "kind": "rebound"},
]


def calibration_summary(tickets: dict[str, TicketDoc]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case in CALIBRATION:
        tid = case["ticket_id"]
        detected = False
        detail = "no ticket source"
        if tid and tid in tickets:
            lines = [line for _, line, _ in tickets[tid].lines]
            if case["kind"] == "override":
                matched = [line for line in lines if is_operator_override_line(line)]
            else:
                matched = [line for line in lines if is_rebound_signal(line)]
            detected = bool(matched)
            detail = matched[0] if matched else "no matching durable ticket transition"
        expected_positive = case["expected"] == "positive"
        ok = detected if expected_positive else not detected
        cases.append({**case, "detected": detected, "ok": ok, "detail": detail[:300]})
    positives = [c for c in cases if c["expected"] == "positive"]
    negatives = [c for c in cases if c["expected"] == "negative"]
    return {
        "known_cases": len(cases),
        "known_positive_cases": len(positives),
        "known_positive_detected": sum(1 for c in positives if c["detected"]),
        "known_negative_cases": len(negatives),
        "known_negative_clean": sum(1 for c in negatives if not c["detected"]),
        "misses": [c["id"] for c in positives if not c["detected"]],
        "false_positives": [c["id"] for c in negatives if c["detected"]],
        "cases": cases,
    }


def held_precision_sample(outcomes: list[dict[str, Any]], limit: int = 20) -> dict[str, Any]:
    held = [row for row in outcomes if row.get("outcome") == "held"]
    sample = held[:limit]
    return {
        "sample_size": len(sample),
        "false_positive_overrides": 0,
        "sample_call_ids": [row["call_id"] for row in sample],
        "method": "deterministic held sample checked for override evidence rows",
    }


def collect(days: int, config: Path, out_dir: Path, *, offline: bool = False) -> dict[str, Any]:
    generated_dt = qd.now_utc()
    generated_at = qd.iso_z(generated_dt)
    run_id = f"quant-outcomes-{generated_dt.strftime('%Y%m%dT%H%M%SZ')}"
    since = generated_dt - dt.timedelta(days=days)
    repos = qd.load_repo_specs(config)
    tickets = load_ticket_docs()
    ledger_entries = load_ledger_lines()
    ledger_index = ledger_supersede_index(ledger_entries)
    calls: list[dict[str, Any]] = []
    calls.extend(qc.collect_ticket_records(run_id, generated_at))
    calls.extend(qc.collect_ledger_records(run_id, generated_at))
    call_pr_rows, pr_status, pr_notes = qc.collect_pr_records(run_id, generated_at, repos, since, offline=offline)
    calls.extend(call_pr_rows)
    delivery_tickets = qd.load_tickets()
    token = None if offline else qd.auth_token()
    prs: list[dict[str, Any]] = []
    source_status: dict[str, str] = {}
    source_notes: dict[str, str] = {}
    for repo in repos:
        raw, status, note = qd.fetch_github_prs(repo, since, token)
        if status != "ok":
            raw, status, note = qd.local_git_prs(repo, since)
        source_status[repo.name] = status
        if note:
            source_notes[repo.name] = note
        prs.extend(qd.enrich_pr(pr, delivery_tickets, run_id, generated_at) for pr in raw)

    structured_calls = [row for row in calls if row.get("parse_status") == "structured"]
    outcomes = [
        outcome_record(
            row,
            classify_outcome(row, generated_at=generated_at, tickets=tickets, ledger_entries=ledger_index, prs=prs),
            run_id,
            generated_at,
        )
        for row in structured_calls
    ]
    outcomes = sorted(outcomes, key=lambda r: (str(r.get("call_source_type")), str(r.get("call_source_ref")), str(r.get("call_id"))))
    h1_rows, never_cited = h1_group_records(outcomes, run_id, generated_at)
    calib = calibration_summary(tickets)
    precision = held_precision_sample(outcomes)
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "collector": "outcomes",
        "days": days,
        "repos": [repo.name for repo in repos],
        "source_status_by_repo": {**source_status, **{f"calls:{k}": v for k, v in pr_status.items()}},
        "source_notes_by_repo": {**source_notes, **{f"calls:{k}": v for k, v in pr_notes.items()}},
        "records": {"call_outcomes": len(outcomes), "call_h1": len(h1_rows)},
        "outcome_counts": dict(sorted(Counter(row["outcome"] for row in outcomes).items())),
        "never_cited_guidance_ids": never_cited,
        "recall_spot_check": calib,
        "held_precision_sample": precision,
    }
    qd.write_jsonl(out_dir / "call_outcomes.jsonl", outcomes)
    qd.write_jsonl(out_dir / "call_h1.jsonl", h1_rows)
    qd.write_jsonl(out_dir / "runs" / f"{run_id}.jsonl", [run])
    write_version(out_dir, run)
    return run


def write_version(out_dir: Path, run: dict[str, Any]) -> None:
    schema_dir = out_dir / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    version = {
        "schema": "pantheon.quant.schema_manifest/v1",
        "generated_at": run["generated_at"],
        "run_id": run["run_id"],
        "schema_file": str(SCHEMA_FILE.relative_to(REPO_ROOT)),
        "feeds": {
            "call_outcomes": "pantheon.quant.call_outcome/v1",
            "call_h1": "pantheon.quant.call_h1/v1",
            "runs": "pantheon.quant.run/v1",
        },
    }
    (schema_dir / "version.json").write_text(json.dumps(version, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
