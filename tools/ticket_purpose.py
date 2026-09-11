"""Typed ORG-054 purpose links for Pantheon tickets.

The module is deliberately independent of :mod:`tools.ticket`: lifecycle
commands import it, while corpus lint and tests can use the same parser and
graph rules without creating a ticket <-> lint import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import ast
import re
from typing import AbstractSet, Mapping

from tools.project_catalogue import PROJECT_ID_RE, ProjectRecord


TICKET_ID_RE = re.compile(r"^T-\d{4,}$")
GOAL_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAINTENANCE_REF_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
SELF_REFERENCE_LABELS = frozenset({"project-formation", "exploratory-probe"})


class PurposeError(ValueError):
    """A ticket purpose value or graph cannot be accepted."""


class AppetiteError(PurposeError):
    """An ORG-089 appetite / substate frontmatter value cannot be accepted.

    Subclasses PurposeError so existing PurposeError catch sites (and the
    lifecycle commands that wrap purpose validation) surface it uniformly; the
    distinct type lets callers that care route it separately (T-3440 / ORG-089).
    """


# T-3440 / ORG-089 §Decision.3: appetite is a 1-10 integer (average of three
# positive sub-scores, per D-E). Bounds are inclusive.
APPETITE_MIN = 1
APPETITE_MAX = 10
# The two ORG-021-frontmatter substates this ADR adds. `None`/absent is the
# normal case; any other value is refused (fail-loud on an unknown substate so
# a typo cannot silently disable the enforcement gate).
VALID_SUBSTATES = frozenset({"appetite-overrun", "dropped"})
# §Decision.4e applicability: hygiene labels + the goal-neutral maintenance
# serves marker that route a ticket to the medium-default rather than a refusal.
HYGIENE_LABELS = frozenset({"hygiene", "chore", "cleanup", "noodle", "noop"})
GOAL_NEUTRAL_SERVES = "goal-neutral-maintenance"
# Medium band (§Decision.3: 4-6 medium) default stamped on hygiene/legacy bets.
APPETITE_MEDIUM_DEFAULT = 5


def normalize_appetite(raw: object) -> int | None:
    """Parse an appetite value to an int in [APPETITE_MIN, APPETITE_MAX].

    Accepts an int or a str-int (frontmatter scalars parse as str); returns the
    normalized int. `None` (absent) passes through as `None` — the CALLER
    decides whether absence is allowed (file-time applicability vs. a re-decision
    verb that always supplies one). Raises AppetiteError on a non-int, a
    non-integer numeric, or an out-of-range value. Pure; no I/O.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        # bool is an int subclass in Python; a truth value is never an appetite.
        raise AppetiteError(f"appetite must be an integer {APPETITE_MIN}-{APPETITE_MAX}, got bool {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not re.fullmatch(r"-?\d+", text):
            raise AppetiteError(
                f"appetite must be an integer {APPETITE_MIN}-{APPETITE_MAX}, got {raw!r}"
            )
        value = int(text)
    else:
        raise AppetiteError(
            f"appetite must be an integer {APPETITE_MIN}-{APPETITE_MAX}, got {type(raw).__name__} {raw!r}"
        )
    if value < APPETITE_MIN or value > APPETITE_MAX:
        raise AppetiteError(
            f"appetite {value} out of range {APPETITE_MIN}-{APPETITE_MAX}"
        )
    return value


def validate_appetite_fields(fields: Mapping[str, object]) -> None:
    """Shape-check the ORG-089 appetite/substate frontmatter block.

    Rules (T-3440 Scope A):
      - appetite: normalize_appetite when present (int 1-10; str-int coerced).
      - substate: must be None or one of VALID_SUBSTATES.
      - substate == 'dropped' REQUIRES a non-empty dropped_reason (ORG-021).
      - bet_ref / dropped_reason / dropped_from_state: non-empty str when present.
      - dropped_from_state, when present, names an open dir (inbox|active).
    Raises AppetiteError on any violation. Pure; no I/O.
    """
    normalize_appetite(fields.get("appetite"))

    substate = fields.get("substate")
    if substate is not None:
        if not isinstance(substate, str) or substate not in VALID_SUBSTATES:
            raise AppetiteError(
                f"substate must be one of {sorted(VALID_SUBSTATES)} or absent, got {substate!r}"
            )

    for key in ("bet_ref", "dropped_reason", "dropped_from_state"):
        value = fields.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise AppetiteError(f"{key} must be a non-empty string when present, got {value!r}")

    dropped_from = fields.get("dropped_from_state")
    if dropped_from is not None and dropped_from not in ("inbox", "active"):
        raise AppetiteError(
            f"dropped_from_state must be 'inbox' or 'active', got {dropped_from!r}"
        )

    if substate == "dropped":
        dropped_reason = fields.get("dropped_reason")
        if not (isinstance(dropped_reason, str) and dropped_reason.strip()):
            raise AppetiteError("substate: dropped requires a non-empty dropped_reason")


# ADR id shapes. ORG-NNN is always agent-authored (non-vault). ADR-NNN is
# vault (operator-authored) below the agent floor; the default grooming/file
# scan scope (pantheon-config + echo-prismatic) uses 200 (Q4-confirmed; the
# pantheon-pwa floor of 50 is out of default scan scope).
ADR_ID_RE = re.compile(r"\b((?:ORG|ADR)-\d+)\b")
_ADR_AGENT_FLOOR = 200


def is_vault_adr(adr_id: str) -> bool:
    """True iff `adr_id` names an operator-authored VAULT ADR (exempt from the
    ORG-089 RECORD-mode appetite fields). ORG-* is never vault; ADR-NNN is
    vault iff NNN < 200 (T-3440 / ORG-089 §Surface 4a, Q4-confirmed)."""
    m = re.fullmatch(r"(ORG|ADR)-(\d+)", adr_id.strip())
    if m is None:
        return False
    prefix, num = m.group(1), int(m.group(2))
    return prefix == "ADR" and num < _ADR_AGENT_FLOOR


class PurposeRefKind(str, Enum):
    TICKET = "ticket"
    PROJECT = "project"
    GOAL = "goal"
    MAINTENANCE = "maintenance"


class PurposeEnforcement(str, Enum):
    WARN = "warn"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class IntentRef:
    kind: PurposeRefKind
    value: str


@dataclass(frozen=True)
class TicketPurpose:
    ticket_id: str
    project_id: str | None
    project_id_present: bool
    nearest_intent_ref: str | None
    serves: str | None
    labels: frozenset[str]
    state: str


@dataclass(frozen=True)
class PurposeIssue:
    code: str
    ticket_id: str
    detail: str


AUTOMATED_FILER_EXPECTATIONS: Mapping[str, int] = {
    "tools/adr_reeval_walk.py": 1,
    "tools/grand_project_program.py": 1,
    "tools/knowledge_sweep.py": 1,
    "tools/mirror_drift_sweep.py": 1,
    "tools/s3star_audit_runner.py": 2,
    "tools/soak_filer.py": 1,
    "tools/watch_runner.py": 5,
    "tools/weekly_ticket_summary.py": 1,
}


def parse_intent_ref(raw: str) -> IntentRef:
    """Parse and shape-check ``<kind>:<value>`` without resolving it."""
    if not isinstance(raw, str) or raw != raw.strip() or ":" not in raw:
        raise PurposeError("nearest_intent_ref must be a trimmed '<kind>:<value>' string")
    prefix, value = raw.split(":", 1)
    try:
        kind = PurposeRefKind(prefix)
    except ValueError as exc:
        raise PurposeError(
            "nearest_intent_ref kind must be ticket, project, goal, or maintenance"
        ) from exc
    if not value:
        raise PurposeError(f"{kind.value}: reference value must be non-empty")
    if kind is PurposeRefKind.TICKET and not TICKET_ID_RE.fullmatch(value):
        raise PurposeError(f"ticket reference must be ticket:T-NNNN, got {raw!r}")
    if kind is PurposeRefKind.PROJECT and not PROJECT_ID_RE.fullmatch(value):
        raise PurposeError(f"invalid project reference {raw!r}")
    if kind is PurposeRefKind.GOAL and not GOAL_KEY_RE.fullmatch(value):
        raise PurposeError(f"invalid goal reference {raw!r}")
    if kind is PurposeRefKind.MAINTENANCE:
        path = PurePosixPath(value)
        if (
            not MAINTENANCE_REF_RE.fullmatch(value)
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise PurposeError(
                "maintenance reference must name a clean relative logical/path asset"
            )
    return IntentRef(kind=kind, value=value)


def _load_frontmatter(path: Path) -> Mapping[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise PurposeError(f"{path}: missing frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise PurposeError(f"{path}: missing closing frontmatter delimiter")
    fields: dict[str, object] = {}
    for raw_line in text[4:end].splitlines():
        if not raw_line.strip():
            continue
        match = re.match(r"^([a-z_][a-z0-9_]*):\s*(.*)$", raw_line.rstrip())
        if match is None:
            raise PurposeError(f"{path}: cannot parse frontmatter line {raw_line!r}")
        key, raw = match.group(1), match.group(2).strip()
        if key in fields:
            raise PurposeError(f"{path}: duplicate frontmatter key {key!r}")
        if raw == "" or raw.lower() == "null":
            value: object = None
        elif raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            value = [] if not inner else _split_flow_list(inner)
        else:
            # Ticket frontmatter intentionally permits plain scalars containing
            # colon-space; it is a constrained ticket format, not general YAML.
            value = raw
        fields[key] = value
    return fields


def _split_flow_list(inner: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in inner:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            item = "".join(current).strip()
            if not item:
                raise PurposeError("empty flow-list item")
            items.append(item)
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if quoted or escaped or not item:
        raise PurposeError("malformed flow list")
    items.append(item)
    return items


def _optional_scalar(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PurposeError(f"{key} must be null or a non-empty string")
    return value.strip()


def load_ticket_purpose_index(tickets_root: Path) -> dict[str, TicketPurpose]:
    """Load ticket purpose fields from inbox, active, and done tickets."""
    purposes: dict[str, TicketPurpose] = {}
    for state in ("inbox", "active", "done"):
        state_root = tickets_root / state
        if not state_root.is_dir():
            continue
        for path in sorted(state_root.glob("T-*.md")):
            fields = _load_frontmatter(path)
            ticket_id = _optional_scalar(fields, "id")
            if ticket_id is None or not TICKET_ID_RE.fullmatch(ticket_id):
                raise PurposeError(f"{path}: invalid ticket id")
            if ticket_id in purposes:
                raise PurposeError(f"duplicate ticket id: {ticket_id}")
            raw_labels = fields.get("labels") or []
            if not isinstance(raw_labels, list) or any(
                not isinstance(label, str) for label in raw_labels
            ):
                raise PurposeError(f"{path}: labels must be a string list")
            project_id = _optional_scalar(fields, "project_id")
            if project_id is not None and not PROJECT_ID_RE.fullmatch(project_id):
                raise PurposeError(f"{path}: invalid project_id {project_id!r}")
            purposes[ticket_id] = TicketPurpose(
                ticket_id=ticket_id,
                project_id=project_id,
                project_id_present="project_id" in fields,
                nearest_intent_ref=_optional_scalar(fields, "nearest_intent_ref"),
                serves=_optional_scalar(fields, "serves"),
                labels=frozenset(label.strip() for label in raw_labels if label.strip()),
                state=str(fields.get("state") or state),
            )
    return purposes


def _chain_issue(
    start: TicketPurpose,
    tickets: Mapping[str, TicketPurpose],
    records: Mapping[str, ProjectRecord],
    goal_keys: AbstractSet[str],
) -> PurposeIssue | None:
    seen: list[str] = []
    current = start
    while True:
        if current.ticket_id in seen:
            cycle = seen[seen.index(current.ticket_id) :] + [current.ticket_id]
            return PurposeIssue(
                "purpose-cycle", start.ticket_id, " -> ".join(cycle)
            )
        seen.append(current.ticket_id)
        raw = current.nearest_intent_ref
        if raw is None:
            if current.serves:
                return None
            return PurposeIssue(
                "missing-purpose-terminal",
                start.ticket_id,
                f"{current.ticket_id} has neither nearest_intent_ref nor serves",
            )
        try:
            ref = parse_intent_ref(raw)
        except PurposeError as exc:
            return PurposeIssue("malformed-intent-ref", start.ticket_id, str(exc))
        if ref.kind is PurposeRefKind.TICKET:
            target = tickets.get(ref.value)
            if target is None:
                return PurposeIssue(
                    "dangling-ticket-purpose", start.ticket_id, f"missing {ref.value}"
                )
            if target.ticket_id == current.ticket_id:
                legal = (
                    current.project_id_present
                    and current.project_id is None
                    and bool(current.labels & SELF_REFERENCE_LABELS)
                    and bool(current.serves)
                )
                if legal:
                    return None
                return PurposeIssue(
                    "invalid-self-purpose",
                    start.ticket_id,
                    "self reference requires project_id:null, serves, and a formation/probe label",
                )
            if (
                current.project_id_present
                and target.project_id_present
                and current.project_id != target.project_id
            ):
                return PurposeIssue(
                    "ticket-project-mismatch",
                    start.ticket_id,
                    f"{current.ticket_id} project_id differs from parent {target.ticket_id}",
                )
            current = target
            continue
        if ref.kind is PurposeRefKind.PROJECT and ref.value not in records:
            return PurposeIssue(
                "dangling-project-purpose", start.ticket_id, f"missing project:{ref.value}"
            )
        if ref.kind is PurposeRefKind.GOAL:
            if not goal_keys:
                return PurposeIssue(
                    "goal-registry-unavailable",
                    start.ticket_id,
                    "canonical machine-readable goal keys were not supplied",
                )
            if ref.value not in goal_keys:
                return PurposeIssue(
                    "dangling-goal-purpose", start.ticket_id, f"missing goal:{ref.value}"
                )
        return None


def validate_intent_link(
    purpose: TicketPurpose,
    tickets: Mapping[str, TicketPurpose],
    records: Mapping[str, ProjectRecord],
    goal_keys: AbstractSet[str],
) -> tuple[PurposeIssue, ...]:
    """Validate a ticket's local purpose fields and its full upward chain."""
    if (
        purpose.state == "done"
        and not purpose.project_id_present
        and purpose.nearest_intent_ref is None
    ):
        # Historic closure remains readable and quiet; migration never rewrites
        # the completed corpus merely to satisfy a newly introduced schema.
        return ()
    issues: list[PurposeIssue] = []
    if purpose.state != "done" and (
        not purpose.project_id_present or purpose.nearest_intent_ref is None
    ):
        issues.append(
            PurposeIssue(
                "missing-structured-purpose",
                purpose.ticket_id,
                "open ticket needs project_id (including explicit null) and nearest_intent_ref",
            )
        )
    if purpose.project_id is not None and purpose.project_id not in records:
        issues.append(
            PurposeIssue(
                "dangling-project-id",
                purpose.ticket_id,
                f"missing project record {purpose.project_id!r}",
            )
        )
    if not purpose.serves:
        issues.append(
            PurposeIssue("missing-serves", purpose.ticket_id, "serves must be non-empty")
        )
    if purpose.nearest_intent_ref is not None:
        try:
            direct_ref = parse_intent_ref(purpose.nearest_intent_ref)
        except PurposeError:
            direct_ref = None
        if direct_ref is not None and direct_ref.kind is PurposeRefKind.PROJECT:
            if purpose.project_id != direct_ref.value:
                issues.append(PurposeIssue(
                    "project-purpose-mismatch",
                    purpose.ticket_id,
                    "direct project reference must equal project_id",
                ))
        if direct_ref is not None and direct_ref.kind in {
            PurposeRefKind.GOAL,
            PurposeRefKind.MAINTENANCE,
        } and not (purpose.project_id_present and purpose.project_id is None):
            issues.append(PurposeIssue(
                "root-project-id-conflict",
                purpose.ticket_id,
                f"direct {direct_ref.kind.value} root requires explicit project_id:null",
            ))
    if purpose.nearest_intent_ref is not None or purpose.serves:
        chain_issue = _chain_issue(purpose, tickets, records, goal_keys)
        if chain_issue is not None:
            issues.append(chain_issue)
    return tuple(issues)


def resolve_ticket_purpose_chain(
    ticket_id: str,
    tickets: Mapping[str, TicketPurpose],
    records: Mapping[str, ProjectRecord],
    goal_keys: AbstractSet[str],
) -> tuple[str, ...]:
    """Resolve a ticket chain to its first non-ticket purpose reference."""
    if ticket_id not in tickets:
        raise PurposeError(f"unknown ticket: {ticket_id}")
    path: list[str] = []
    current = tickets[ticket_id]
    while True:
        if current.ticket_id in path:
            cycle = path[path.index(current.ticket_id) :] + [current.ticket_id]
            raise PurposeError(f"purpose cycle: {' -> '.join(cycle)}")
        path.append(current.ticket_id)
        raw = current.nearest_intent_ref
        if raw is None:
            if current.serves:
                return tuple(path)
            raise PurposeError(f"{current.ticket_id} has no purpose terminal")
        ref = parse_intent_ref(raw)
        if ref.kind is PurposeRefKind.TICKET:
            target = tickets.get(ref.value)
            if target is None:
                raise PurposeError(f"dangling ticket purpose: {ref.value}")
            if target.ticket_id == current.ticket_id and (
                current.project_id_present
                and current.project_id is None
                and current.labels & SELF_REFERENCE_LABELS
                and current.serves
            ):
                return tuple(path + [raw])
            current = target
            continue
        if ref.kind is PurposeRefKind.PROJECT and ref.value not in records:
            raise PurposeError(f"dangling project purpose: {ref.value}")
        if ref.kind is PurposeRefKind.GOAL:
            if not goal_keys:
                raise PurposeError("canonical goal-key registry unavailable")
            if ref.value not in goal_keys:
                raise PurposeError(f"dangling goal purpose: {ref.value}")
        return tuple(path + [raw])


def validate_new_ticket_purpose(
    purpose: TicketPurpose,
    tickets: Mapping[str, TicketPurpose],
    records: Mapping[str, ProjectRecord],
    goal_keys: AbstractSet[str],
    mode: PurposeEnforcement,
) -> tuple[PurposeIssue, ...]:
    """Validate a newly filed ticket under warn-first or enforcing policy."""
    issues = validate_intent_link(purpose, tickets, records, goal_keys)
    if mode is PurposeEnforcement.ENFORCE and issues:
        return issues
    return issues


def audit_automated_filers(repo_root: Path) -> tuple[PurposeIssue, ...]:
    """Static negative control for every known production ticket filer."""
    issues: list[PurposeIssue] = []
    discovered: dict[str, list[set[str]]] = {}
    for path in sorted((repo_root / "tools").glob("*.py")):
        if path.name.startswith("test_") or path.name == "ticket.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        commands: list[set[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            literals = {
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            if {"file", "--requester", "--target"}.issubset(literals):
                commands.append(literals)
        if commands:
            discovered[str(path.relative_to(repo_root))] = commands

    for relative in sorted(set(discovered) - set(AUTOMATED_FILER_EXPECTATIONS)):
        issues.append(PurposeIssue(
            "unregistered-automated-filer",
            relative,
            "ticket.py file command is not in AUTOMATED_FILER_EXPECTATIONS",
        ))
    for relative, minimum in AUTOMATED_FILER_EXPECTATIONS.items():
        path = repo_root / relative
        if not path.is_file():
            issues.append(PurposeIssue("missing-filer", relative, "file is absent"))
            continue
        commands = discovered.get(relative, [])
        if len(commands) != minimum:
            issues.append(PurposeIssue(
                "filer-command-count-changed",
                relative,
                f"found {len(commands)} command(s), expected exactly {minimum}",
            ))
        for number, literals in enumerate(commands, start=1):
            for flag in ("--project-id", "--nearest-intent-ref"):
                if flag in literals:
                    continue
                issues.append(
                    PurposeIssue(
                        "filer-purpose-flag-missing",
                        relative,
                        f"command {number} omits {flag}",
                    )
                )
    return tuple(issues)
