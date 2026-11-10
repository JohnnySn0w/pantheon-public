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

# Marker: a DECISIONS/DECISION heading or label at line start (optionally after a bullet / markdown
# heading / bold), never the bare word inside prose (T-4819).
MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:#{1,6}[ \t]*)?(?:\*\*)?DECISIONS?\b(?:\*\*)?[ \t]*(?::|\(|$)",
    re.MULTILINE,
)
# Strict starts claim structure (line start or the transition-log `; DECISION:`): malformed ones warn.
# Inline starts (after sentence punctuation) are prose: they parse only when fully structured.
DECISION_PREFIX_RE = re.compile(
    r"(?:(?P<strict>^[ \t]*|;[ \t]*)|(?P<inline>(?<=[.!?])[ \t]+))(?:[-*][ \t]*)?DECISION[ \t]*:",
    re.MULTILINE,
)
DECISION_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?DECISION\s*:\s*(?P<call>[^|]+?)\s*"
    r"\|\s*serves\s*:\s*(?P<serves>[^|]+?)\s*"
    r"\|\s*via\s*:\s*(?P<via>[^|]+?)\s*"
    r"\|\s*ref\s*:\s*(?P<ref>[^|]+?)\s*$",
)
_BASE = r"(?:(P)-?(\d+)|(D)-?(\d+)|(ORG)-(\d{3,})|(ADR)-(\d+)|(T)-(\d+))"
VIA_TOKEN_RE = re.compile(rf"^{_BASE}$", re.IGNORECASE)
# base id fused with a section qualifier: ORG-052§3, ORG-052§3/§5, ORG-083-sec-3
VIA_QUALIFIED_RE = re.compile(rf"^{_BASE}(?P<qual>§[\w§/.\-]*|-sec-\d+(?:-\d+)*)$", re.IGNORECASE)
# standalone qualifier token, attaches to the preceding base id in the list
VIA_QUALIFIER_RE = re.compile(r"^(?:§[\w§/.\-]*|Amendment-[A-Za-z0-9]+)$")
RULING_TG_RE = re.compile(r"^operator:tg:(\d+)$", re.IGNORECASE)
RULING_MAIL_RE = re.compile(r"^ho:mail:([A-Za-z0-9_.-]+)$", re.IGNORECASE)
TICKET_REF_RE = re.compile(r"^T-(\d{3,6})$", re.IGNORECASE)
PR_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+#\d+$")
SHA_REF_RE = re.compile(r"^(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,40}$")
_REF_TOK = r"(?:T-\d{3,6}|[A-Za-z0-9_.-]+#\d+|[0-9a-fA-F]{7,40})"
REF_LIST_RE = re.compile(rf"^(?P<list>{_REF_TOK}(?:\s*,\s*{_REF_TOK})*)(?:\s+(?P<note>\S.*))?$", re.IGNORECASE)
REF_ERROR = "ref must be T-NNNN, repo#PR or commit sha (comma-separated)"


@dataclass(frozen=True)
class DecisionLine:
    call: str
    serves: str
    via_ids: tuple[str, ...]
    via_classes: dict[str, int]
    ref: str
    line_no: int
    raw: str
    via_qualifiers: tuple[tuple[str, str], ...] = ()
    refs: tuple[str, ...] = ()
    ref_note: str | None = None

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
    text = text or ""
    return bool(MARKER_RE.search(text)) or any(m.group("strict") is not None for m in DECISION_PREFIX_RE.finditer(text))


def _base_from_match(match: re.Match[str]) -> tuple[str, str]:
    if match.group(1):
        return f"P{int(match.group(2))}", "principle"
    if match.group(3):
        return f"D{int(match.group(4))}", "directive"
    if match.group(5):
        return f"ORG-{int(match.group(6)):03d}", "org"
    if match.group(7):
        return f"ADR-{int(match.group(8))}", "adr"
    return f"T-{int(match.group(10)):04d}", "ticket"


def parse_via_token(token: str) -> tuple[str, str, str | None] | None:
    """Return (id, class, qualifier|None) for a via token, or None if invalid."""
    token = token.strip()
    match = VIA_TOKEN_RE.match(token)
    if match:
        return (*_base_from_match(match), None)
    match = VIA_QUALIFIED_RE.match(token)
    if match:
        return (*_base_from_match(match), match.group("qual"))
    match = RULING_TG_RE.match(token)
    if match:
        return f"operator:tg:{match.group(1)}", "ruling", None
    match = RULING_MAIL_RE.match(token)
    if match:
        return f"ho:mail:{match.group(1)}", "ruling", None
    return None


def normalize_via_id(token: str) -> tuple[str, str] | None:
    parsed = parse_via_token(token)
    if parsed is None:
        return None
    return parsed[0], parsed[1]


def parse_refs(ref: str) -> tuple[tuple[str, ...], str | None] | None:
    """Parse a ref field: comma-separated T-NNNN | repo#PR | sha, optional trailing note."""
    ref = ref.strip().rstrip(".").strip()
    match = REF_LIST_RE.match(ref)
    if not match:
        return None
    refs: list[str] = []
    for tok in re.split(r"\s*,\s*", match.group("list")):
        ticket = TICKET_REF_RE.match(tok)
        if ticket:
            refs.append(f"T-{int(ticket.group(1)):04d}")
        elif PR_REF_RE.match(tok):
            refs.append(tok)
        elif SHA_REF_RE.match(tok):
            refs.append(tok.lower())
        else:
            return None
    note = match.group("note")
    return tuple(refs), (note.strip() if note else None)


def normalize_ref(ref: str) -> str | None:
    parsed = parse_refs(ref)
    if parsed is None:
        return None
    return ",".join(parsed[0])


def split_via_ids(
    raw: str,
) -> tuple[tuple[str, ...], dict[str, int], list[str], tuple[tuple[str, str], ...]]:
    tokens = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    ids: list[str] = []
    classes: dict[str, int] = {}
    errors: list[str] = []
    qualifiers: list[tuple[str, str]] = []
    last_base: str | None = None
    for token in tokens:
        parsed = parse_via_token(token)
        if parsed is None:
            if last_base is not None and VIA_QUALIFIER_RE.match(token):
                qualifiers.append((last_base, token))
                continue
            errors.append(f"invalid via id {token!r}")
            continue
        via_id, cls, qual = parsed
        ids.append(via_id)
        classes[cls] = classes.get(cls, 0) + 1
        last_base = via_id
        if qual:
            qualifiers.append((via_id, qual))
    if not ids and not errors:
        errors.append("via must contain at least one id")
    return tuple(ids), classes, errors, tuple(qualifiers)


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
    via_ids, via_classes, via_errors, via_quals = split_via_ids(via_raw)
    if via_errors:
        return None, DecisionProblem(line_no, "; ".join(via_errors), line)
    parsed_ref = parse_refs(ref_raw)
    if parsed_ref is None:
        return None, DecisionProblem(line_no, REF_ERROR, line)
    refs, ref_note = parsed_ref
    return (
        DecisionLine(call, serves, via_ids, via_classes, ",".join(refs), line_no, line, via_quals, refs, ref_note),
        None,
    )


def scan_decisions(text: str) -> DecisionScan:
    text = text or ""
    marker = has_decision_marker(text)
    decisions: list[DecisionLine] = []
    problems: list[DecisionProblem] = []
    decision_like = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        matches = list(DECISION_PREFIX_RE.finditer(line))
        for idx, prefix in enumerate(matches):
            start = prefix.start() + prefix.group(0).index("DECISION")
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            parsed, problem = parse_decision_line(line[start:end].rstrip(), line_no)
            if prefix.group("strict") is None:
                # Inline in prose: count it only when fully structured; otherwise it is just prose.
                if parsed is not None:
                    decision_like += 1
                    decisions.append(parsed)
                continue
            decision_like += 1
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
