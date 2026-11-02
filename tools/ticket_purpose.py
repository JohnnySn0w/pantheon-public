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

from tools import _frontmatter_block
from tools.project_catalogue import PROJECT_ID_RE, ProjectRecord


TICKET_ID_RE = re.compile(r"^T-\d{4,}$")
GOAL_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAINTENANCE_REF_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
SELF_REFERENCE_LABELS = frozenset({"project-formation", "exploratory-probe"})
# T-3332 (b), ratified by orchestrator 2026-10-27: a ticket carrying this label is a
# designated non-goal ROOT (e.g. T-5677, operator-directed one-offs). It is a
# purpose terminal in its own right: a child whose nearest_intent_ref chain
# reaches it is rooted, even though the root itself has no serves/intent ref.
# Single source of truth, also imported by tools/ticket_lineage.py.
OPERATIONAL_ROOT_LABEL = "operational-root"


def is_purpose_terminal(purpose: "TicketPurpose") -> bool:
    """True iff a chain walk may stop at `purpose` with no further intent ref:
    it carries a `serves` arc-intent, or it is a labelled operational root."""
    return bool(purpose.serves) or OPERATIONAL_ROOT_LABEL in purpose.labels


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
# The frontmatter substates registered so far: `appetite-overrun` and `dropped`
# from this ORG-021/ORG-089 ADR, plus `awaiting-merge` (T-4119 — a work-complete
# ticket parked awaiting the HO-exclusive merge; see sop/standard/ticket-flow.md).
# `None`/absent is the normal case; any other value is refused (fail-loud on an
# unknown substate so a typo cannot silently disable the enforcement gate).
VALID_SUBSTATES = frozenset({"appetite-overrun", "dropped", "awaiting-merge"})
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


# The canonical `role:<RoleId>` target shape. RETAINED after the ORG-088 D-4
# work_domain/delegation routing-validation was removed (rev-2 filter-at-pull-time,
# T-3789): rev-2 KEEPS role:<R> targets, so this shape is still the single source
# of truth for the role-target syntax. Lazily imported at runtime by ticket_lint
# (role-target detection) and mirrored byte-for-byte by ticket._ROLE_TARGET_RE.
ROLE_TARGET_RE = re.compile(r"^role:[a-z][a-z0-9_]*$")


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
    "tools/episode_store.py": 1,  # T-4707 / ORG-076 BT1: runtime repair-ticket emission
    "tools/grand_project_program.py": 1,
    "tools/knowledge_sweep.py": 1,
    "tools/operator_decision_sweep.py": 1,  # T-5015 phase 2
    "tools/operator_decision_throughline.py": 1,  # T-5015 phase 3
    "tools/mirror_drift_sweep.py": 1,
    "tools/parity_audit_runner.py": 1,
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
    return _parse_frontmatter_text(path.read_text(encoding="utf-8"), str(path))


def _parse_frontmatter_text(text: str, label: str) -> Mapping[str, object]:
    """Parse ticket frontmatter from TEXT; `label` names the source in errors.

    Split out of `_load_frontmatter` (T-4402) so a caller that holds the text
    but no file (a staged blob in the pre-commit content gate) runs the SAME
    parser the live-corpus loader does."""
    if not text.startswith("---\n"):
        raise PurposeError(f"{label}: missing frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise PurposeError(f"{label}: missing closing frontmatter delimiter")
    fields: dict[str, object] = {}
    # T-5694: LF is the ticket format's only source-line delimiter.  The other
    # characters consumed by str.splitlines() are scalar value bytes.
    fm_lines = text[4:end].split("\n")
    idx = 0
    while idx < len(fm_lines):
        raw_line = fm_lines[idx]
        idx += 1
        if not raw_line.strip():
            continue
        match = re.match(r"^([a-z_][a-z0-9_]*):\s*(.*)$", raw_line.rstrip())
        if match is None:
            raise PurposeError(f"{label}: cannot parse frontmatter line {raw_line!r}")
        key, raw = match.group(1), match.group(2).strip()
        if key in fields:
            raise PurposeError(f"{label}: duplicate frontmatter key {key!r}")
        if _frontmatter_block.parse_block_header(raw) is not None:
            # T-4707 / T-5689: a YAML `|` / `>` block scalar on ANY key (ORG-076
            # evidence_snapshot, T-5678 threat_path, ...). Same shared reader as
            # ticket.parse_front_matter, so the two parsers agree on the value.
            # No indented content: fall through, the plain scalar (e.g. "|").
            block, idx = _frontmatter_block.consume_block(fm_lines, idx, raw, key=key)
            if block is not None:
                fields[key] = block
                continue
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
            purpose = _purpose_from_fields(_load_frontmatter(path), str(path), state)
            if purpose.ticket_id in purposes:
                raise PurposeError(f"duplicate ticket id: {purpose.ticket_id}")
            purposes[purpose.ticket_id] = purpose
    return purposes


def _purpose_from_fields(
    fields: Mapping[str, object], label: str, default_state: str
) -> TicketPurpose:
    ticket_id = _optional_scalar(fields, "id")
    if ticket_id is None or not TICKET_ID_RE.fullmatch(ticket_id):
        raise PurposeError(f"{label}: invalid ticket id")
    raw_labels = fields.get("labels") or []
    if not isinstance(raw_labels, list) or any(
        not isinstance(item, str) for item in raw_labels
    ):
        raise PurposeError(f"{label}: labels must be a string list")
    project_id = _optional_scalar(fields, "project_id")
    if project_id is not None and not PROJECT_ID_RE.fullmatch(project_id):
        raise PurposeError(f"{label}: invalid project_id {project_id!r}")
    return TicketPurpose(
        ticket_id=ticket_id,
        project_id=project_id,
        project_id_present="project_id" in fields,
        nearest_intent_ref=_optional_scalar(fields, "nearest_intent_ref"),
        serves=_optional_scalar(fields, "serves"),
        labels=frozenset(item.strip() for item in raw_labels if item.strip()),
        state=str(fields.get("state") or default_state),
    )


def purpose_from_text(text: str, *, source: str, default_state: str) -> TicketPurpose:
    """Parse ONE ticket file's text into a `TicketPurpose`, with the same parser
    and field rules as `load_ticket_purpose_index` (T-4402).

    For callers that hold ticket text but no file: the pre-commit content gate
    reads a staged blob (and the HEAD blob) rather than the working tree.
    `source` only labels errors; `default_state` applies when the frontmatter
    carries no `state` (the loader passes the directory name).
    """
    return _purpose_from_fields(_parse_frontmatter_text(text, source), source, default_state)


def project_id_justified(
    purpose: TicketPurpose, index: Mapping[str, TicketPurpose]
) -> bool:
    """True iff `purpose.project_id` is None, appears in `purpose.labels`
    (exact match), or a `ticket:`-chain walk via `nearest_intent_ref` reaches
    an ancestor ticket carrying the same `project_id`.

    This is the single source of truth for T-4080/T-4090's project_id
    justification rule: shared by the live-corpus test
    (test_initial_backfill_never_infers_project_from_repo_or_persona) and by
    ticket.py's file-time / purpose-set / label-remove enforcement, so the
    two paths never drift into disagreement the way they did in T-4080 (a
    filing landed project_id without either justification path holding, and
    only the test caught it -- after main was already red).

    Pure; no I/O. `index` is caller-supplied so a not-yet-persisted
    candidate ticket can be checked against the live on-disk corpus.
    """
    if purpose.project_id is None:
        return True
    if purpose.project_id in purpose.labels:
        return True
    seen: set[str] = set()
    current = purpose
    while current.nearest_intent_ref and current.nearest_intent_ref.startswith(
        "ticket:"
    ):
        parent_id = current.nearest_intent_ref.split(":", 1)[1]
        if parent_id in seen or parent_id not in index:
            return False
        seen.add(parent_id)
        current = index[parent_id]
        if current.project_id == purpose.project_id:
            return True
    return False


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
            if is_purpose_terminal(current):
                return None
            return PurposeIssue(
                "missing-purpose-terminal",
                start.ticket_id,
                f"{current.ticket_id} has neither nearest_intent_ref nor serves "
                f"nor the {OPERATIONAL_ROOT_LABEL} label",
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
            if is_purpose_terminal(current):
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


def with_goal_registry_reason(
    issues: tuple[PurposeIssue, ...], reason: str | None
) -> tuple[PurposeIssue, ...]:
    """T-5679: name WHY the goal registry is unavailable on the issue itself.

    ``reason`` comes from ``goal_registry.registry_status``. The validator's
    fixed detail only says no keys were supplied; this swaps in the concrete
    cause (missing file, malformed marker, duplicate key) so the warning or
    refusal is honest about it. Issues of other codes pass through untouched.
    """
    if not reason:
        return issues
    return tuple(
        PurposeIssue(issue.code, issue.ticket_id, f"goal registry unavailable: {reason}")
        if issue.code == "goal-registry-unavailable"
        else issue
        for issue in issues
    )


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
