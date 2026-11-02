#!/usr/bin/env python3
"""ORG-102 delivery collector (T-4780 step 1).

Reads existing exhaust only: GitHub PR metadata when available, local git merge history when
GitHub is not available, ticket transition logs, and fleet-usage readings. It writes a rebuildable
JSONL feed under /var/lib/pantheon/quant by default.

DECISION: Delivery metrics come from exhaust and record missing sources explicitly | serves: D1 the operator's bandwidth + P4 traceability | via: ORG-102 | ref: T-4780
DECISION: GitHub PRs are the delivery unit and local git is only a metadata-poor fallback | serves: D1 the operator's bandwidth + H3/H4 delivery proof | via: ORG-102 | ref: T-4780
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "tools" / "quant_delivery_repos.json"
DEFAULT_OUT = Path("/var/lib/pantheon/quant")
DEFAULT_FLEET_USAGE_DIR = Path("/var/lib/pantheon/fleet-usage")
SCHEMA_FILE = REPO_ROOT / "schemas" / "quant-delivery.schema.json"

TICKET_RE = re.compile(r"\bT-(\d{3,6})\b", re.IGNORECASE)
TG_ID_RE = re.compile(r"\b(?:the operator\s+TG|TG)\s*(\d+)\b", re.IGNORECASE)
OPERATOR_ACTOR_RE = re.compile(r"\[operator\]", re.IGNORECASE)
REBOUND_RE = re.compile(r"\bREBOUND\b", re.IGNORECASE)
TRANSITION_ENTRY_RE = re.compile(r"^\s*-\s+\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+\[[^\]]+\].*", re.MULTILINE)
REVERT_SUBJECT_RE = re.compile(r"^Revert\b", re.IGNORECASE)
REVERT_TRAILER_RE = re.compile(r"\bThis reverts commit [0-9a-f]{7,40}\b", re.IGNORECASE)
HOTFIX_SUBJECT_RE = re.compile(r"\b(hotfix|fix-forward|emergency fix|urgent fix)\b", re.IGNORECASE)
HOTFIX_LABEL_RE = re.compile(r"\b(hotfix|urgent|fix-forward)\b", re.IGNORECASE)
MERGE_PR_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+)\b", re.IGNORECASE)
SQUASH_PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")
GH_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")


@dataclass(frozen=True)
class RepoSpec:
    name: str
    github: str | None
    local_path: Path | None


@dataclass(frozen=True)
class TicketInfo:
    ticket_id: str
    state: str
    created_at: str | None
    parent_ticket: str | None
    title: str
    path: str
    rebound_rounds: int
    operator_touches: int
    operator_touch_ids: tuple[str, ...]
    rollback_transitions: int


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def hours_between(start: str | None, end: str | None) -> float | None:
    a = parse_time(start)
    b = parse_time(end)
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() / 3600, 3)


def week_start(value: str | None) -> str | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    monday = (parsed.date() - dt.timedelta(days=parsed.weekday()))
    return monday.isoformat()


def quantile(values: list[float], q: float) -> float | None:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 3)
    pos = (len(clean) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(clean[lo], 3)
    weight = pos - lo
    return round(clean[lo] * (1 - weight) + clean[hi] * weight, 3)


def wilson_interval(successes: int, total: int, z: float = 1.6448536269514722) -> dict[str, float] | None:
    """Return the 90% Wilson interval by default. Empty samples are insufficient-source."""
    if total <= 0:
        return None
    successes = max(0, min(successes, total))
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total) / denom
    return {
        "low": round(max(0.0, center - margin), 4),
        "mid": round(phat, 4),
        "high": round(min(1.0, center + margin), 4),
    }


def ticket_ids_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({f"T-{int(m.group(1)):04d}" for m in TICKET_RE.finditer(text)})


def operator_touch_ids_from_text(text: str, ticket_id: str) -> tuple[str, ...]:
    ids = {f"TG-{int(m.group(1))}" for m in TG_ID_RE.finditer(text)}
    if OPERATOR_ACTOR_RE.search(text):
        ids.add(f"OPERATOR-ACTOR:{ticket_id}")
    return tuple(sorted(ids))


def pr_number_from_subject(subject: str) -> int | None:
    merge = MERGE_PR_SUBJECT_RE.search(subject)
    if merge:
        return int(merge.group(1))
    squash = SQUASH_PR_SUBJECT_RE.search(subject)
    if squash:
        return int(squash.group(1))
    return None


def transition_log_text(ticket_text: str) -> str:
    match = re.search(r"^## Transition log\s*$", ticket_text, re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", ticket_text[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(ticket_text)
    return ticket_text[start:end]


def is_explicit_rollback_transition(line: str) -> bool:
    detail = line.split(";", 1)[1] if ";" in line else line
    detail = re.sub(r"^\s*\[[^\]]+\]\s*", "", detail.strip())
    lower = detail.lower()
    if lower.startswith("characterization") or "would have rolled back" in lower:
        return False
    early = lower[:180]
    if re.search(r"\brollback (?:applied|executed|completed|performed)\b", early):
        return True
    if "rolled back" not in early:
        return False
    return bool(re.search(r"\b(attempt|fire|cutover|outcome|failed|failure|rollback)\b", early))


def explicit_rollback_transition_count(ticket_text: str) -> int:
    section = transition_log_text(ticket_text)
    return sum(1 for match in TRANSITION_ENTRY_RE.finditer(section) if is_explicit_rollback_transition(match.group(0)))


def load_repo_specs(path: Path) -> list[RepoSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    repos = raw.get("repos", [])
    out: list[RepoSpec] = []
    for item in repos:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        local = item.get("local_path")
        out.append(
            RepoSpec(
                name=str(item["name"]),
                github=str(item["github"]) if item.get("github") else None,
                local_path=Path(local).expanduser() if local else None,
            )
        )
    return out


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"null", "None"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip("'\"") for part in inner.split(",")]
    return value.strip("'\"")


def parse_ticket_file(path: Path) -> TicketInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    fields: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        # T-5689: an indented line is block-scalar content (`key: |` / `>`), never a key.
        if ":" not in line or line[:1] in (" ", "\t"):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = _parse_scalar(value)
    tid = fields.get("id")
    if not isinstance(tid, str):
        return None
    title_match = re.search(r"^#\s+(.+)$", text[end + 4 :], re.MULTILINE)
    operator_touch_ids = operator_touch_ids_from_text(text, tid)
    return TicketInfo(
        ticket_id=tid,
        state=str(fields.get("state") or path.parent.name),
        created_at=str(fields["created"]) if fields.get("created") else None,
        parent_ticket=str(fields["parent_ticket"]) if fields.get("parent_ticket") else None,
        title=title_match.group(1).strip() if title_match else "",
        path=str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path),
        rebound_rounds=len(REBOUND_RE.findall(text)),
        operator_touches=len(operator_touch_ids),
        operator_touch_ids=operator_touch_ids,
        rollback_transitions=explicit_rollback_transition_count(text),
    )


def load_tickets(root: Path = REPO_ROOT / "tickets") -> dict[str, TicketInfo]:
    out: dict[str, TicketInfo] = {}
    for subdir in ("inbox", "active", "done"):
        base = root / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.glob("T-*.md")):
            parsed = parse_ticket_file(path)
            if parsed:
                out[parsed.ticket_id] = parsed
    return out


def auth_token(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    for key in GH_TOKEN_ENV:
        token = env.get(key)
        if token:
            return token
    return None


def github_get_json(url: str, token: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pantheon-quant-delivery",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_github_prs(repo: RepoSpec, since: dt.datetime, token: str | None) -> tuple[list[dict], str, str | None]:
    if not repo.github or not token:
        return [], "insufficient-source", "missing GitHub token"
    owner_repo = repo.github
    out: list[dict] = []
    page = 1
    try:
        while True:
            url = (
                f"https://api.github.com/repos/{owner_repo}/pulls"
                f"?state=closed&sort=updated&direction=desc&per_page=100&page={page}"
            )
            batch = github_get_json(url, token)
            if not isinstance(batch, list) or not batch:
                break
            stop = False
            for pr in batch:
                merged_at = parse_time(pr.get("merged_at"))
                updated_at = parse_time(pr.get("updated_at"))
                if merged_at and merged_at >= since:
                    out.append(normalize_github_pr(repo.name, pr))
                if updated_at and updated_at < since and not merged_at:
                    stop = True
            if stop or len(batch) < 100:
                break
            page += 1
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return [], "insufficient-source", str(exc)
    return out, "ok", None


def normalize_github_pr(repo_name: str, pr: dict) -> dict:
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    user = pr.get("user") or {}
    return {
        "repo": repo_name,
        "number": pr.get("number"),
        "source": "github-rest",
        "source_status": "ok",
        "title": title,
        "body": body,
        "author": user.get("login"),
        "url": pr.get("html_url"),
        "created_at": pr.get("created_at"),
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at"),
        "head_ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "labels": sorted(
            str(label.get("name") or label)
            for label in pr.get("labels", [])
            if isinstance(label, (str, dict))
        ),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
    }


def local_git_prs(repo: RepoSpec, since: dt.datetime) -> tuple[list[dict], str, str | None]:
    if repo.local_path is None or not (repo.local_path / ".git").exists():
        return [], "insufficient-source", "local git checkout missing"
    cmd = [
        "git",
        "-C",
        str(repo.local_path),
        "log",
        "--first-parent",
        f"--since={iso_z(since)}",
        "--format=%H%x1f%cI%x1f%s%x1e",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], "insufficient-source", str(exc)
    if proc.returncode != 0:
        return [], "insufficient-source", proc.stderr.strip() or "git log failed"
    out: list[dict] = []
    for item in proc.stdout.strip("\x1e\n").split("\x1e"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("\x1f")
        if len(parts) != 3:
            continue
        sha, committed_at, subject = parts
        number = pr_number_from_subject(subject)
        if number is None:
            continue
        out.append(
            {
                "repo": repo.name,
                "number": number,
                "source": "local-git",
                "source_status": "insufficient-source",
                "title": subject,
                "body": "",
                "author": None,
                "url": None,
                "created_at": None,
                "merged_at": committed_at,
                "closed_at": committed_at,
                "head_ref": None,
                "base_ref": None,
                "merge_commit_sha": sha,
                "labels": [],
                "additions": None,
                "deletions": None,
                "changed_files": None,
                "source_note": "local first-parent PR-shape fallback; PR metadata fields insufficient-source",
            }
        )
    return out, ("ok" if out else "insufficient-source"), None if out else "no recent first-parent PR-shaped commits"


def is_automation_delivery(pr: dict) -> tuple[bool, str | None]:
    hay = "\n".join(str(pr.get(k) or "") for k in ("title", "body", "author", "head_ref"))
    title = str(pr.get("title") or "")
    author = str(pr.get("author") or "")
    head = str(pr.get("head_ref") or "")
    if author.endswith("[bot]") or author in {"github-actions", "dependabot"}:
        return True, "bot author"
    patterns = [
        (r"^tickets?:", "ticket.py ticket-state commit"),
        (r"\bticket\.py\b", "ticket.py ticket-state commit"),
        (r"\bledger\b.*\b(spine|groom|append)\b|\bspine groom\b", "ledger/spine groom commit"),
        (r"\bresearch\(watch\)|\bwatch auto\b|\bauto-commit\b", "research/watch auto commit"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, title, re.IGNORECASE) or re.search(pattern, hay, re.IGNORECASE):
            return True, reason
    if re.search(r"(spine-groom|ledger|watch)", head, re.IGNORECASE):
        return True, "automation branch"
    return False, None


def is_revert_pr(pr: dict) -> bool:
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    if REVERT_SUBJECT_RE.search(title):
        return True
    if REVERT_TRAILER_RE.search(body):
        return True
    return False


def is_hotfix_pr(pr: dict) -> bool:
    title = str(pr.get("title") or "")
    labels = [str(label) for label in pr.get("labels", [])]
    return bool(HOTFIX_SUBJECT_RE.search(title) or any(HOTFIX_LABEL_RE.search(label) for label in labels))


def enrich_pr(pr: dict, tickets: dict[str, TicketInfo], run_id: str, generated_at: str) -> dict:
    text = "\n".join(str(pr.get(k) or "") for k in ("title", "body", "head_ref"))
    ticket_ids = ticket_ids_from_text(text)
    linked = [tickets[tid] for tid in ticket_ids if tid in tickets]
    parent_ids = sorted({t.parent_ticket for t in linked if t.parent_ticket})
    if parent_ids:
        arc_id = parent_ids[0]
    elif ticket_ids:
        arc_id = ticket_ids[0]
    else:
        arc_id = f"{pr.get('repo')}#{pr.get('number') or pr.get('merge_commit_sha') or 'unknown'}"
    excluded, reason = is_automation_delivery(pr)
    rebound_rounds = sum(t.rebound_rounds for t in linked)
    operator_touch_ids = sorted({touch_id for ticket in linked for touch_id in ticket.operator_touch_ids})
    first_created = min((t.created_at for t in linked if t.created_at), default=None)
    out = {
        "schema": "pantheon.quant.delivery_pr/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        **pr,
        "ticket_ids": ticket_ids,
        "arc_id": arc_id,
        "delivery_excluded": excluded,
        "delivery_exclusion_reason": reason,
        "lead_time_pr_hours": hours_between(pr.get("created_at"), pr.get("merged_at")),
        "lead_time_ticket_hours": hours_between(first_created, pr.get("merged_at")),
        "rebound_rounds": rebound_rounds,
        "ticket_rollback_transitions": sum(t.rollback_transitions for t in linked),
        "operator_touch_ids": operator_touch_ids,
        "operator_touches": len(operator_touch_ids),
        "is_revert": is_revert_pr(pr),
        "is_hotfix": is_hotfix_pr(pr),
    }
    if pr.get("source_status") == "insufficient-source":
        for field in ("created_at", "author", "head_ref", "base_ref", "additions", "deletions", "changed_files"):
            if out.get(field) is None:
                out[f"{field}_status"] = "insufficient-source"
    return out


def change_failure_breakdown(prs: list[dict]) -> dict[str, int]:
    """ORG-102 H4 change failures: reverts, hotfix PRs, and rollbacks only.

    REBOUND/review churn before merge is deliberately excluded; it is the review
    system working, not a shipped-change failure.
    """
    return {
        "revert_prs": sum(1 for p in prs if p.get("is_revert")),
        "hotfix_prs": sum(1 for p in prs if p.get("is_hotfix")),
        "rollback_prs": sum(1 for p in prs if int(p.get("ticket_rollback_transitions") or 0) > 0),
    }


def is_change_failure_pr(pr: dict) -> bool:
    return bool(
        pr.get("is_revert")
        or pr.get("is_hotfix")
        or int(pr.get("ticket_rollback_transitions") or 0) > 0
    )


def ticket_records(tickets: dict[str, TicketInfo], run_id: str, generated_at: str) -> list[dict]:
    rows = []
    for ticket in tickets.values():
        rows.append(
            {
                "schema": "pantheon.quant.delivery_ticket/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "ticket_id": ticket.ticket_id,
                "state": ticket.state,
                "created_at": ticket.created_at,
                "parent_ticket": ticket.parent_ticket,
                "title": ticket.title,
                "path": ticket.path,
                "rebound_rounds": ticket.rebound_rounds,
                "operator_touch_ids": list(ticket.operator_touch_ids),
                "operator_touches": ticket.operator_touches,
                "rollback_transitions": ticket.rollback_transitions,
            }
        )
    return sorted(rows, key=lambda r: r["ticket_id"])


def arc_records(prs: list[dict], run_id: str, generated_at: str) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pr in prs:
        if pr.get("delivery_excluded"):
            continue
        groups[(str(pr.get("repo")), str(pr.get("arc_id")))].append(pr)
    rows = []
    for (repo, arc_id), group in sorted(groups.items()):
        merged = [p for p in group if p.get("merged_at")]
        lead_times = [p["lead_time_ticket_hours"] for p in group if p.get("lead_time_ticket_hours") is not None]
        failures = [p for p in group if is_change_failure_pr(p)]
        breakdown = change_failure_breakdown(group)
        operator_touch_ids = {touch_id for p in group for touch_id in p.get("operator_touch_ids", [])}
        rows.append(
            {
                "schema": "pantheon.quant.delivery_arc/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "repo": repo,
                "arc_id": arc_id,
                "ticket_ids": sorted({tid for p in group for tid in p.get("ticket_ids", [])}),
                "operator_touch_ids": sorted(operator_touch_ids),
                "pr_count": len(group),
                "merged_prs": len(merged),
                "change_failures": len(failures),
                "change_failure_breakdown": breakdown,
                "failed_pr_refs": [
                    f"{p.get('repo')}#{p.get('number')}" for p in failures if p.get("repo") and p.get("number")
                ],
                "operator_touches": len(operator_touch_ids),
                "lead_time_arc_hours": max(lead_times) if lead_times else None,
                "first_pr_created_at": min((p.get("created_at") for p in group if p.get("created_at")), default=None),
                "last_pr_merged_at": max((p.get("merged_at") for p in group if p.get("merged_at")), default=None),
            }
        )
    return rows


def latest_fleet_usage(usage_dir: Path = DEFAULT_FLEET_USAGE_DIR) -> dict[str, Any]:
    if not usage_dir.is_dir():
        return {"status": "insufficient-source", "used_percentage": None, "readings": 0}
    readings = []
    for path in sorted(usage_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seven = data.get("seven_day") if isinstance(data, dict) else None
        if isinstance(seven, dict) and isinstance(seven.get("used_percentage"), (int, float)):
            readings.append({"seat": data.get("seat") or path.stem, "used_percentage": seven["used_percentage"]})
    if not readings:
        return {"status": "insufficient-source", "used_percentage": None, "readings": 0}
    return {
        "status": "ok",
        "used_percentage": round(sum(r["used_percentage"] for r in readings) / len(readings), 3),
        "readings": len(readings),
    }


def weekly_records(prs: list[dict], run_id: str, generated_at: str, usage: dict[str, Any]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pr in prs:
        wk = week_start(pr.get("merged_at"))
        if wk:
            groups[(wk, str(pr.get("repo")))].append(pr)
            groups[(wk, "ALL")].append(pr)
    rows = []
    for (wk, repo), group in sorted(groups.items()):
        delivery = [p for p in group if not p.get("delivery_excluded")]
        failures = [p for p in delivery if is_change_failure_pr(p)]
        breakdown = change_failure_breakdown(delivery)
        lead_pr = [p["lead_time_pr_hours"] for p in delivery if p.get("lead_time_pr_hours") is not None]
        lead_ticket = [p["lead_time_ticket_hours"] for p in delivery if p.get("lead_time_ticket_hours") is not None]
        arc_ids = {p.get("arc_id") for p in delivery}
        operator_touch_ids = {touch_id for p in delivery for touch_id in p.get("operator_touch_ids", [])}
        rows.append(
            {
                "schema": "pantheon.quant.delivery_weekly/v1",
                "run_id": run_id,
                "generated_at": generated_at,
                "week_start": wk,
                "repo": repo,
                "merged_prs": len(group),
                "delivery_prs": len(delivery),
                "excluded_prs": len(group) - len(delivery),
                "change_failures": len(failures),
                "change_failure_breakdown": breakdown,
                "failure_rate_wilson90": wilson_interval(len(failures), len(delivery)),
                "lead_time_pr_hours_p50": quantile(lead_pr, 0.5),
                "lead_time_pr_hours_p90": quantile(lead_pr, 0.9),
                "lead_time_ticket_hours_p50": quantile(lead_ticket, 0.5),
                "lead_time_ticket_hours_p90": quantile(lead_ticket, 0.9),
                "arc_count": len(arc_ids),
                "operator_touch_ids": sorted(operator_touch_ids),
                "operator_touches": len(operator_touch_ids),
                "operator_touches_per_arc": round(len(operator_touch_ids) / len(arc_ids), 3) if arc_ids else None,
                "quota_source_status": usage["status"],
                "quota_used_percentage": usage.get("used_percentage"),
                "quota_readings": usage.get("readings", 0),
                "quota_percent_per_pr": round(usage["used_percentage"] / len(delivery), 3)
                if usage.get("used_percentage") is not None and delivery
                else None,
            }
        )
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def write_version(out_dir: Path, run: dict) -> None:
    schema_dir = out_dir / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    version = {
        "schema": "pantheon.quant.schema_manifest/v1",
        "generated_at": run["generated_at"],
        "run_id": run["run_id"],
        "schema_file": str(SCHEMA_FILE.relative_to(REPO_ROOT)),
        "feeds": {
            "delivery_prs": "pantheon.quant.delivery_pr/v1",
            "delivery_tickets": "pantheon.quant.delivery_ticket/v1",
            "delivery_arcs": "pantheon.quant.delivery_arc/v1",
            "delivery_weekly": "pantheon.quant.delivery_weekly/v1",
            "runs": "pantheon.quant.run/v1",
        },
    }
    (schema_dir / "version.json").write_text(json.dumps(version, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect(days: int, config: Path, out_dir: Path, offline: bool = False) -> dict[str, Any]:
    generated_dt = now_utc()
    generated_at = iso_z(generated_dt)
    run_id = f"quant-delivery-{generated_dt.strftime('%Y%m%dT%H%M%SZ')}"
    since = generated_dt - dt.timedelta(days=days)
    repos = load_repo_specs(config)
    tickets = load_tickets()
    token = None if offline else auth_token()
    prs: list[dict] = []
    source_status: dict[str, str] = {}
    source_notes: dict[str, str] = {}

    for repo in repos:
        raw, status, note = fetch_github_prs(repo, since, token)
        if status != "ok":
            raw, status, note = local_git_prs(repo, since)
        source_status[repo.name] = status
        if note:
            source_notes[repo.name] = note
        prs.extend(enrich_pr(pr, tickets, run_id, generated_at) for pr in raw)

    prs = sorted(prs, key=lambda r: (str(r.get("repo")), str(r.get("merged_at") or ""), int(r.get("number") or 0)))
    tickets_rows = ticket_records(tickets, run_id, generated_at)
    arcs = arc_records(prs, run_id, generated_at)
    usage = latest_fleet_usage()
    weekly = weekly_records(prs, run_id, generated_at, usage)
    run = {
        "schema": "pantheon.quant.run/v1",
        "run_id": run_id,
        "generated_at": generated_at,
        "days": days,
        "repos": [r.name for r in repos],
        "source_status_by_repo": source_status,
        "source_notes_by_repo": source_notes,
        "records": {
            "delivery_prs": len(prs),
            "delivery_tickets": len(tickets_rows),
            "delivery_arcs": len(arcs),
            "delivery_weekly": len(weekly),
        },
    }

    write_jsonl(out_dir / "delivery_prs.jsonl", prs)
    write_jsonl(out_dir / "delivery_tickets.jsonl", tickets_rows)
    write_jsonl(out_dir / "delivery_arcs.jsonl", arcs)
    write_jsonl(out_dir / "delivery_weekly.jsonl", weekly)
    write_jsonl(out_dir / "runs" / f"{run_id}.jsonl", [run])
    write_version(out_dir, run)
    return run


def check_github(config: Path) -> int:
    repos = load_repo_specs(config)
    token = auth_token()
    if not token:
        for repo in repos:
            print(f"{repo.name}: insufficient-source (missing GitHub token)")
        return 1
    failed = False
    since = now_utc() - dt.timedelta(days=1)
    for repo in repos:
        _, status, note = fetch_github_prs(repo, since, token)
        if status == "ok":
            print(f"{repo.name}: ok")
        else:
            failed = True
            print(f"{repo.name}: insufficient-source ({note})")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "collect"):
        p = sub.add_parser(name, help=f"{name} delivery quantification feed")
        p.add_argument("--days", type=int, default=60 if name == "backfill" else 1)
        p.add_argument("--out", type=Path, default=DEFAULT_OUT)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--offline", action="store_true", help="skip GitHub REST and use local git fallback only")
    p = sub.add_parser("check-github", help="check GitHub PR read access for configured repos")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "check-github":
        return check_github(args.config)
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2
    run = collect(args.days, args.config, args.out, offline=args.offline)
    print(json.dumps(run, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
