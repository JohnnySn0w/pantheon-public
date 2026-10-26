#!/usr/bin/env python3
"""ORG-102 structured DECISION line parser.

Canonical line:
  DECISION: <call> | serves: <intent> | via: <ids> | ref: <T-NNNN or repo#PR>

The parser is intentionally small and dependency-free so ticket.py, CI checks and collectors can
share one grammar. It never attempts to convert legacy prose into structure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

MARKER_RE = re.compile(r"\bDECISIONS?\b")
DECISION_PREFIX_RE = re.compile(r"(?:^\s*|;\s*)(?:[-*]\s*)?DECISION\s*:")
DECISION_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?DECISION\s*:\s*(?P<call>[^|]+?)\s*"
    r"\|\s*serves\s*:\s*(?P<serves>[^|]+?)\s*"
    r"\|\s*via\s*:\s*(?P<via>[^|]+?)\s*"
    r"\|\s*ref\s*:\s*(?P<ref>[^|]+?)\s*$",
)
VIA_TOKEN_RE = re.compile(
    r"^(?:(P)-?(\d+)|(D)-?(\d+)|(ORG)-(\d{3,})|(ADR)-(\d+)|(T)-(\d+))$",
    re.IGNORECASE,
)
TICKET_REF_RE = re.compile(r"^T-(\d{3,6})$", re.IGNORECASE)
PR_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+#\d+$")


@dataclass(frozen=True)
class DecisionLine:
    call: str
    serves: str
    via_ids: tuple[str, ...]
    via_classes: dict[str, int]
    ref: str
    line_no: int
    raw: str

    @property
    def principle_only(self) -> bool:
        return bool(self.via_ids) and set(self.via_classes) == {"principle"}


@dataclass(frozen=True)
class DecisionProblem:
    line_no: int | None
    message: str
    raw: str


@dataclass(frozen=True)
class DecisionScan:
    decisions: tuple[DecisionLine, ...]
    problems: tuple[DecisionProblem, ...]
    marker_present: bool


def has_decision_marker(text: str) -> bool:
    return bool(MARKER_RE.search(text or ""))


def normalize_via_id(token: str) -> tuple[str, str] | None:
    token = token.strip()
    match = VIA_TOKEN_RE.match(token)
    if not match:
        return None
    if match.group(1):
        return f"P{int(match.group(2))}", "principle"
    if match.group(3):
        return f"D{int(match.group(4))}", "directive"
    if match.group(5):
        return f"ORG-{int(match.group(6)):03d}", "org"
    if match.group(7):
        return f"ADR-{int(match.group(8))}", "adr"
    return f"T-{int(match.group(10)):04d}", "ticket"


def normalize_ref(ref: str) -> str | None:
    ref = ref.strip()
    ticket = TICKET_REF_RE.match(ref)
    if ticket:
        return f"T-{int(ticket.group(1)):04d}"
    if PR_REF_RE.match(ref):
        return ref
    return None


def split_via_ids(raw: str) -> tuple[tuple[str, ...], dict[str, int], list[str]]:
    tokens = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    ids: list[str] = []
    classes: dict[str, int] = {}
    errors: list[str] = []
    for token in tokens:
        parsed = normalize_via_id(token)
        if parsed is None:
            errors.append(f"invalid via id {token!r}")
            continue
        via_id, cls = parsed
        ids.append(via_id)
        classes[cls] = classes.get(cls, 0) + 1
    if not ids and not errors:
        errors.append("via must contain at least one id")
    return tuple(ids), classes, errors


def parse_decision_line(line: str, line_no: int) -> tuple[DecisionLine | None, DecisionProblem | None]:
    match = DECISION_LINE_RE.match(line)
    if not match:
        return None, DecisionProblem(line_no, "malformed DECISION line; expected DECISION: <call> | serves: <intent> | via: <ids> | ref: <ref>", line)
    call = match.group("call").strip()
    serves = match.group("serves").strip()
    via_raw = match.group("via").strip()
    ref_raw = match.group("ref").strip()
    if not call:
        return None, DecisionProblem(line_no, "call must be non-empty", line)
    if not serves:
        return None, DecisionProblem(line_no, "serves must be non-empty", line)
    via_ids, via_classes, via_errors = split_via_ids(via_raw)
    if via_errors:
        return None, DecisionProblem(line_no, "; ".join(via_errors), line)
    ref = normalize_ref(ref_raw)
    if ref is None:
        return None, DecisionProblem(line_no, "ref must be T-NNNN or repo#PR", line)
    return DecisionLine(call, serves, via_ids, via_classes, ref, line_no, line), None


def scan_decisions(text: str) -> DecisionScan:
    text = text or ""
    marker = has_decision_marker(text)
    decisions: list[DecisionLine] = []
    problems: list[DecisionProblem] = []
    decision_like = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        prefix = DECISION_PREFIX_RE.search(line)
        if not prefix:
            continue
        decision_like += 1
        decision_start = prefix.start() + prefix.group(0).index("DECISION")
        parsed, problem = parse_decision_line(line[decision_start:], line_no)
        if parsed is not None:
            decisions.append(parsed)
        if problem is not None:
            problems.append(problem)
    if marker and decision_like == 0:
        problems.append(
            DecisionProblem(
                None,
                "DECISIONS marker present but no structured DECISION line found",
                _excerpt(text),
            )
        )
    return DecisionScan(tuple(decisions), tuple(problems), marker)


def _excerpt(text: str, limit: int = 240) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def excerpt_hash(text: str) -> tuple[str, str]:
    excerpt = _excerpt(text)
    return excerpt, hashlib.sha256((text or "").encode("utf-8")).hexdigest()
