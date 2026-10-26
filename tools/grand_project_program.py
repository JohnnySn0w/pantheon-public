#!/usr/bin/env python3
"""ORG-061 stage evidence and owned re-evaluation obligation machinery.

This module owns strict loading, pure validation, and pure evaluation.  The
existing ``adr_reeval_walk`` module remains the weekly ticket-filing adapter.
T-2426 deliberately keeps architecture and stage mutation outside this module.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from tools import ticket


_MAX_INDEX_BYTES = 1_048_576
_OWNED_ADRS = frozenset(f"ORG-{number:03d}" for number in range(53, 62))
_SOURCE_ID_RE = re.compile(r"ORG-(?:05[3-9]|060|061):R[1-9][0-9]*\Z")
_TICKET_ID_RE = re.compile(r"T-[0-9]+\Z")
_RECEIPT_REF_RE = re.compile(r"ticket:(T-[0-9]+)#(.+)\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REEVAL_SECTION_RE = re.compile(
    r"^#{2,}\s*(?:Re-?evaluate|Revisit|Sunset)\b[^\n]*\n(?P<body>.*?)(?=^#{1,}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_ID_BULLET_RE = re.compile(
    r"^\s*[-*]\s*\*\*(?P<tid>[A-Z]{2,}-\d+:R\d+)\*\*\s*[—:-]?\s*(?P<text>.+?)\s*$",
    re.MULTILINE,
)
_TRIGGER_DIGEST_DOMAIN = "pantheon-grand-project-trigger-v1"
_STAGE_NAMES = {
    1: "Harness parity and control recovery",
    2: "Installation and seed viability",
    3: "Persona restoration",
    4: "Bridge establishment",
    5: "Echo conversational interface",
    6: "Exocortex and memory",
    7: "Federation and inter-system communication",
    8: "Senses and actuation",
    9: "Rich presence",
    10: "Adaptive developmental loop",
}
_STAGE_STATES = frozenset(
    {"shaping", "active", "evidence-pending", "passed", "reorienting", "paused", "retired"}
)
_OBLIGATION_STATES = frozenset({"armed", "due", "open", "satisfied", "superseded"})
_TRIGGER_KINDS = frozenset({"calendar", "milestone", "threshold", "proposal", "incident"})


def _finding(code: str, record_id: str, message: str) -> "Finding":
    return Finding(code=code, record_id=record_id, message=message)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ValueError(f"{where}:unknown-key:{','.join(unknown)}")
    if missing:
        raise ValueError(f"{where}:missing-key:{','.join(missing)}")


def _as_str(value: object, where: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{where}:expected-nonempty-string")
    return value


def _as_str_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}:expected-list")
    result: list[str] = []
    for index, item in enumerate(value):
        parsed = _as_str(item, f"{where}[{index}]")
        assert parsed is not None
        result.append(parsed)
    return tuple(result)


def _as_bool(value: object, where: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{where}:expected-bool")
    return value


def _as_int(value: object, where: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{where}:expected-int")
    return value


def _strict_yaml(path: Path) -> Mapping[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_INDEX_BYTES:
        raise ValueError(f"{path}:invalid-size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}:invalid-utf8") from exc
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - repository dependency
        raise RuntimeError("PyYAML is required") from exc

    for token_value in yaml.scan(text):
        if isinstance(token_value, yaml.tokens.AliasToken):
            raise ValueError(f"{path}:alias-not-allowed")
        if isinstance(token_value, yaml.tokens.AnchorToken):
            raise ValueError(f"{path}:alias-or-anchor-not-allowed")
        if isinstance(token_value, yaml.tokens.TagToken):
            raise ValueError(f"{path}:custom-tag-not-allowed")

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: StrictLoader, node: object, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise ValueError(f"{path}:expected-mapping-node")
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key == "<<":
                raise ValueError(f"{path}:merge-key-not-allowed")
            if not isinstance(key, str):
                raise ValueError(f"{path}:mapping-key-not-string")
            if key in result:
                raise ValueError(f"{path}:duplicate-key:{key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        data = yaml.load(text, Loader=StrictLoader)
    except ValueError:
        raise
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}:invalid-yaml:{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}:root-not-mapping")
    return data


def _ticket_paths(tickets_root: Path, ticket_id: str | None = None) -> list[Path]:
    paths: list[Path] = []
    for state in ("inbox", "active", "done", "blocked"):
        directory = tickets_root / state
        if not directory.is_dir():
            continue
        candidates = [directory / f"{ticket_id}.md"] if ticket_id else sorted(directory.glob("T-*.md"))
        paths.extend(path for path in candidates if path.is_file())
    return paths


def _ticket_fields(path: Path) -> Mapping[str, Any]:
    return ticket.parse_ticket(path).fields


_OPEN_STATES = frozenset({"inbox", "active"})


def _open_ticket_fields(tickets_root: Path) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """(ticket id, front-matter fields) for each OPEN ticket, tolerant of concurrent moves.

    T-4165: the tree is live. The inbox-consumer, any agent's ticket.py, or a parallel
    CI worker can move a ticket between the glob in _ticket_paths and the parse here,
    which crashed the scan with FileNotFoundError (main run 36457036198). A ticket
    that vanished is re-resolved by id once: parsed where it now lives if that is still
    inbox/active, skipped if it moved to done/blocked (it is no longer open) or is gone.
    Each id is yielded at most once, since a move between the per-state globs can list
    one ticket under both states."""
    seen: set[str] = set()
    for path in _ticket_paths(tickets_root):
        if path.parent.name not in _OPEN_STATES or path.stem in seen:
            continue
        try:
            fields = _ticket_fields(path)
        except FileNotFoundError:
            now = [p for p in _ticket_paths(tickets_root, path.stem) if p.parent.name in _OPEN_STATES]
            if not now:
                continue
            try:
                fields = _ticket_fields(now[0])
            except FileNotFoundError:
                continue
        seen.add(path.stem)
        yield path.stem, fields


def _ticket_record(tickets_root: Path, ticket_id: str | None) -> ticket.Ticket | None:
    if ticket_id is None:
        return None
    paths = _ticket_paths(tickets_root, ticket_id)
    if len(paths) != 1:
        return None
    return ticket.parse_ticket(paths[0])


def _receipt_parts(encoded: str) -> tuple[str, str, str] | None:
    parts = tuple(part.strip() for part in encoded.split("="))
    if len(parts) != 3 or any(not part for part in parts):
        return None
    return parts


_GATE_QUESTION_PHRASES = (
    "what the stage made possible",
    "what the concrete implementation revealed",
    "which tensions moved and which remain",
    "current developmental mode",
    "whether the next stage/order/contract still serves the root north star",
    "changes to catalogue implications and upkeep",
    "user ruling required at goal/relationship level",
)


@dataclasses.dataclass(frozen=True)
class EvidenceRefs:
    functional: tuple[str, ...]
    continuity: tuple[str, ...]
    trust: tuple[str, ...]
    developmental: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class StageRecord:
    stage_id: str
    ordinal: int
    name: str
    outcome_ref: str
    state: str
    steward_ref: str
    prerequisite_stage_ids: tuple[str, ...]
    research_probe_allowed: bool
    research_probe_boundary_ref: str
    evidence_refs: EvidenceRefs
    denial_failure_rollback_refs: tuple[str, ...]
    acceptance_ticket: str | None
    acceptance_authority_ref: str | None
    reorientation_gate_ticket: str | None
    reorientation_receipt: str | None
    next_production_tickets: tuple[str, ...]
    applicable_obligation_ids: tuple[str, ...]
    outstanding_risk_refs: tuple[str, ...]
    last_reorientation: str | None


@dataclasses.dataclass(frozen=True)
class ReevaluationObligation:
    obligation_id: str
    source_ref: str
    source_text_sha256: str
    owner_ref: str
    review_target: str
    trigger_kind: str
    operational_predicate: str
    observable_source: str
    state: str
    armed_ticket: str | None
    last_evaluated: str | None
    next_check: str | None
    evidence_refs: tuple[str, ...]
    outcome_ref: str | None


@dataclasses.dataclass(frozen=True)
class SourceTrigger:
    obligation_id: str
    source_ref: str
    canonical_text: str


@dataclasses.dataclass(frozen=True)
class EvaluationInputs:
    now: dt.datetime
    resolved_ticket_ids: frozenset[str]
    instrumented_metrics: Mapping[str, float]
    metric_evidence_refs: Mapping[str, str]
    weekly_activity_ref: str | None


@dataclasses.dataclass(frozen=True)
class Finding:
    code: str
    record_id: str
    message: str


@dataclasses.dataclass(frozen=True)
class ProgramIndexes:
    stages: tuple[StageRecord, ...]
    obligations: tuple[ReevaluationObligation, ...]


@dataclasses.dataclass(frozen=True)
class DueObligation:
    obligation: ReevaluationObligation
    evidence_ref: str
    prerequisite_ticket: str | None = None
    not_before: str | None = None


@dataclasses.dataclass(frozen=True)
class ReceiptResolution:
    ticket_id: str
    receipt_id: str
    encoded_receipt: str


@dataclasses.dataclass(frozen=True)
class EvaluationReport:
    findings: tuple[Finding, ...]
    due_obligation_ids: tuple[str, ...]
    surfaced_obligation_ids: tuple[str, ...]
    existing_ticket_ids: tuple[str, ...]
    proposed_ticket_args: tuple[tuple[str, ...], ...]


def load_stage_index(path: Path) -> tuple[StageRecord, ...]:
    data = _strict_yaml(path)
    _require_exact_keys(data, {"schema_version", "stages"}, str(path))
    if _as_int(data["schema_version"], f"{path}:schema_version") != 1:
        raise ValueError(f"{path}:unsupported-schema-version")
    raw_stages = data["stages"]
    if not isinstance(raw_stages, list):
        raise ValueError(f"{path}:stages-not-list")
    fields = {
        "stage_id", "ordinal", "name", "outcome_ref", "state", "steward_ref",
        "prerequisite_stage_ids", "research_probe_policy", "evidence_refs",
        "denial_failure_rollback_refs", "acceptance_ticket", "acceptance_authority_ref",
        "reorientation_gate_ticket", "reorientation_receipt", "next_production_tickets",
        "applicable_obligation_ids", "outstanding_risk_refs", "last_reorientation",
    }
    records: list[StageRecord] = []
    for index, value in enumerate(raw_stages):
        where = f"{path}:stages[{index}]"
        if not isinstance(value, dict):
            raise ValueError(f"{where}:not-mapping")
        _require_exact_keys(value, fields, where)
        policy = value["research_probe_policy"]
        if not isinstance(policy, dict):
            raise ValueError(f"{where}:research_probe_policy:not-mapping")
        _require_exact_keys(policy, {"allowed", "boundary_ref"}, f"{where}:research_probe_policy")
        evidence = value["evidence_refs"]
        if not isinstance(evidence, dict):
            raise ValueError(f"{where}:evidence_refs:not-mapping")
        _require_exact_keys(
            evidence, {"functional", "continuity", "trust", "developmental"},
            f"{where}:evidence_refs",
        )
        stage_id = _as_str(value["stage_id"], f"{where}:stage_id")
        ordinal = _as_int(value["ordinal"], f"{where}:ordinal")
        name = _as_str(value["name"], f"{where}:name")
        state = _as_str(value["state"], f"{where}:state")
        if not re.fullmatch(r"ORG-061:STAGE-(?:[1-9]|10)", stage_id or ""):
            raise ValueError(f"{where}:invalid-stage-id")
        if state not in _STAGE_STATES:
            raise ValueError(f"{where}:invalid-state")
        records.append(
            StageRecord(
                stage_id=stage_id or "",
                ordinal=ordinal,
                name=name or "",
                outcome_ref=_as_str(value["outcome_ref"], f"{where}:outcome_ref") or "",
                state=state or "",
                steward_ref=_as_str(value["steward_ref"], f"{where}:steward_ref") or "",
                prerequisite_stage_ids=_as_str_tuple(
                    value["prerequisite_stage_ids"], f"{where}:prerequisite_stage_ids"
                ),
                research_probe_allowed=_as_bool(
                    policy["allowed"], f"{where}:research_probe_policy:allowed"
                ),
                research_probe_boundary_ref=_as_str(
                    policy["boundary_ref"], f"{where}:research_probe_policy:boundary_ref"
                ) or "",
                evidence_refs=EvidenceRefs(
                    functional=_as_str_tuple(evidence["functional"], f"{where}:evidence_refs:functional"),
                    continuity=_as_str_tuple(evidence["continuity"], f"{where}:evidence_refs:continuity"),
                    trust=_as_str_tuple(evidence["trust"], f"{where}:evidence_refs:trust"),
                    developmental=_as_str_tuple(evidence["developmental"], f"{where}:evidence_refs:developmental"),
                ),
                denial_failure_rollback_refs=_as_str_tuple(
                    value["denial_failure_rollback_refs"], f"{where}:denial_failure_rollback_refs"
                ),
                acceptance_ticket=_as_str(value["acceptance_ticket"], f"{where}:acceptance_ticket", nullable=True),
                acceptance_authority_ref=_as_str(
                    value["acceptance_authority_ref"], f"{where}:acceptance_authority_ref", nullable=True
                ),
                reorientation_gate_ticket=_as_str(
                    value["reorientation_gate_ticket"], f"{where}:reorientation_gate_ticket", nullable=True
                ),
                reorientation_receipt=_as_str(
                    value["reorientation_receipt"], f"{where}:reorientation_receipt", nullable=True
                ),
                next_production_tickets=_as_str_tuple(
                    value["next_production_tickets"], f"{where}:next_production_tickets"
                ),
                applicable_obligation_ids=_as_str_tuple(
                    value["applicable_obligation_ids"], f"{where}:applicable_obligation_ids"
                ),
                outstanding_risk_refs=_as_str_tuple(
                    value["outstanding_risk_refs"], f"{where}:outstanding_risk_refs"
                ),
                last_reorientation=_as_str(
                    value["last_reorientation"], f"{where}:last_reorientation", nullable=True
                ),
            )
        )
    return tuple(records)


def load_obligation_index(path: Path) -> tuple[ReevaluationObligation, ...]:
    data = _strict_yaml(path)
    _require_exact_keys(data, {"schema_version", "obligations"}, str(path))
    if _as_int(data["schema_version"], f"{path}:schema_version") != 1:
        raise ValueError(f"{path}:unsupported-schema-version")
    values = data["obligations"]
    if not isinstance(values, list):
        raise ValueError(f"{path}:obligations-not-list")
    fields = {
        "obligation_id", "source_ref", "source_text_sha256", "owner_ref", "review_target",
        "trigger_kind", "operational_predicate", "observable_source", "state", "armed_ticket",
        "last_evaluated", "next_check", "evidence_refs", "outcome_ref",
    }
    records: list[ReevaluationObligation] = []
    for index, value in enumerate(values):
        where = f"{path}:obligations[{index}]"
        if not isinstance(value, dict):
            raise ValueError(f"{where}:not-mapping")
        _require_exact_keys(value, fields, where)
        obligation_id = _as_str(value["obligation_id"], f"{where}:obligation_id") or ""
        source_ref = _as_str(value["source_ref"], f"{where}:source_ref") or ""
        digest = _as_str(value["source_text_sha256"], f"{where}:source_text_sha256") or ""
        trigger_kind = _as_str(value["trigger_kind"], f"{where}:trigger_kind") or ""
        state = _as_str(value["state"], f"{where}:state") or ""
        armed_ticket = _as_str(value["armed_ticket"], f"{where}:armed_ticket", nullable=True)
        if not _SOURCE_ID_RE.fullmatch(obligation_id):
            raise ValueError(f"{where}:invalid-obligation-id")
        adr_id, suffix = obligation_id.split(":", 1)
        if source_ref != f"adr:{adr_id}#{suffix}":
            raise ValueError(f"{where}:source-ref-id-mismatch")
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"{where}:invalid-source-digest")
        if trigger_kind not in _TRIGGER_KINDS:
            raise ValueError(f"{where}:invalid-trigger-kind")
        if state not in _OBLIGATION_STATES:
            raise ValueError(f"{where}:invalid-state")
        if armed_ticket is not None and not _TICKET_ID_RE.fullmatch(armed_ticket):
            raise ValueError(f"{where}:invalid-armed-ticket")
        records.append(
            ReevaluationObligation(
                obligation_id=obligation_id,
                source_ref=source_ref,
                source_text_sha256=digest,
                owner_ref=_as_str(value["owner_ref"], f"{where}:owner_ref") or "",
                review_target=_as_str(value["review_target"], f"{where}:review_target") or "",
                trigger_kind=trigger_kind,
                operational_predicate=_as_str(
                    value["operational_predicate"], f"{where}:operational_predicate"
                ) or "",
                observable_source=_as_str(
                    value["observable_source"], f"{where}:observable_source"
                ) or "",
                state=state,
                armed_ticket=armed_ticket,
                last_evaluated=_as_str(
                    value["last_evaluated"], f"{where}:last_evaluated", nullable=True
                ),
                next_check=_as_str(value["next_check"], f"{where}:next_check", nullable=True),
                evidence_refs=_as_str_tuple(value["evidence_refs"], f"{where}:evidence_refs"),
                outcome_ref=_as_str(value["outcome_ref"], f"{where}:outcome_ref", nullable=True),
            )
        )
    return tuple(records)


def scan_source_obligations(adr_root: Path) -> tuple[SourceTrigger, ...]:
    all_triggers: list[SourceTrigger] = []
    seen_global: set[str] = set()
    for path in sorted(adr_root.glob("ORG-*.md")):
        match = re.match(r"(ORG-[0-9]{3})", path.name)
        if not match or match.group(1) not in _OWNED_ADRS:
            continue
        adr_id = match.group(1)
        text = path.read_text(encoding="utf-8")
        frontmatter = ""
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---", 4)
            if end >= 0:
                frontmatter, body = text[4:end], text[end + 4 :]
        structured: dict[str, str] = {}
        if frontmatter:
            try:
                import yaml
                metadata = yaml.safe_load(frontmatter) or {}
            except Exception as exc:
                raise ValueError(f"{path}:invalid-frontmatter") from exc
            entries = metadata.get("re_evaluate", []) if isinstance(metadata, dict) else []
            if entries is not None and not isinstance(entries, list):
                raise ValueError(f"{path}:re-evaluate-not-list")
            for index, entry in enumerate(entries or []):
                if not isinstance(entry, dict):
                    raise ValueError(f"{path}:re-evaluate[{index}]-not-mapping")
                raw_id = entry.get("id")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    raise ValueError(f"{path}:re-evaluate[{index}]-missing-id")
                trigger_id = raw_id if ":" in raw_id else f"{adr_id}:{raw_id}"
                canonical = entry.get("text") or entry.get("when")
                if not isinstance(canonical, str) or not canonical.strip():
                    raise ValueError(f"{path}:{trigger_id}-missing-text")
                if trigger_id in structured:
                    raise ValueError(f"{path}:duplicate-structured-trigger:{trigger_id}")
                structured[trigger_id] = canonical.strip()
        prose: dict[str, str] = {}
        for section in _REEVAL_SECTION_RE.finditer(body):
            for match_value in _ID_BULLET_RE.finditer(section.group("body")):
                trigger_id = match_value.group("tid")
                if trigger_id in prose:
                    raise ValueError(f"{path}:duplicate-prose-trigger:{trigger_id}")
                prose[trigger_id] = match_value.group("text").strip()
        selected = dict(prose)
        selected.update(structured)
        for trigger_id, canonical in selected.items():
            if not _SOURCE_ID_RE.fullmatch(trigger_id) or not trigger_id.startswith(adr_id + ":"):
                raise ValueError(f"{path}:invalid-trigger-id:{trigger_id}")
            if trigger_id in seen_global:
                raise ValueError(f"{path}:duplicate-cross-adr-trigger:{trigger_id}")
            seen_global.add(trigger_id)
            suffix = trigger_id.split(":", 1)[1]
            all_triggers.append(
                SourceTrigger(
                    obligation_id=trigger_id,
                    source_ref=f"adr:{adr_id}#{suffix}",
                    canonical_text=canonical,
                )
            )
    return tuple(sorted(all_triggers, key=lambda item: item.obligation_id))


def canonical_trigger_digest(trigger: SourceTrigger) -> str:
    canonical = " ".join(unicodedata.normalize("NFC", trigger.canonical_text).split())
    payload = (
        _TRIGGER_DIGEST_DOMAIN.encode("utf-8")
        + b"\0"
        + trigger.obligation_id.encode("ascii")
        + b"\0"
        + canonical.encode("utf-8")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def lint_obligation_completeness(
    source: Sequence[SourceTrigger],
    owned: Sequence[ReevaluationObligation],
    tickets_root: Path,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    source_counts = Counter(item.obligation_id for item in source)
    owned_counts = Counter(item.obligation_id for item in owned)
    source_by_id = {item.obligation_id: item for item in source}
    owned_by_id = {item.obligation_id: item for item in owned}
    for record_id, count in sorted(source_counts.items()):
        if count > 1:
            findings.append(_finding("obligation-source-duplicate-id", record_id, f"source count is {count}"))
    for record_id, count in sorted(owned_counts.items()):
        if count > 1:
            findings.append(_finding("obligation-owned-duplicate-id", record_id, f"owned count is {count}"))
    for record_id in sorted(source_counts.keys() - owned_counts.keys()):
        findings.append(_finding("obligation-source-missing-owned", record_id, "source has no owned record"))
    for record_id in sorted(owned_counts.keys() - source_counts.keys()):
        findings.append(_finding("obligation-owned-missing-source", record_id, "owned record has no source"))
    for record_id in sorted(source_counts.keys() & owned_counts.keys()):
        if source_counts[record_id] != 1 or owned_counts[record_id] != 1:
            continue
        source_record = source_by_id[record_id]
        owned_record = owned_by_id[record_id]
        if owned_record.source_ref != source_record.source_ref:
            findings.append(_finding("obligation-source-ref-drift", record_id, "source_ref does not match source"))
        if owned_record.source_text_sha256 != canonical_trigger_digest(source_record):
            findings.append(_finding("obligation-source-digest-drift", record_id, "source digest changed"))
    for obligation in sorted(owned, key=lambda item: item.obligation_id):
        matching = []
        for ticket_id, fields in _open_ticket_fields(tickets_root):
            labels = fields.get("labels", [])
            if isinstance(labels, list) and {
                "adr-reeval", f"reeval:{obligation.obligation_id}"
            }.issubset(set(labels)):
                matching.append(ticket_id)
        if obligation.state == "open":
            if len(matching) != 1:
                findings.append(
                    _finding(
                        "obligation-open-ticket-count", obligation.obligation_id,
                        f"expected one open review ticket, found {len(matching)}",
                    )
                )
            if obligation.armed_ticket is None or obligation.armed_ticket not in matching:
                findings.append(
                    _finding(
                        "obligation-open-ticket-stale", obligation.obligation_id,
                        "armed_ticket is absent or does not name the matching open ticket",
                    )
                )
        elif obligation.state in {"armed", "due"} and obligation.armed_ticket is not None:
            if matching != [obligation.armed_ticket]:
                findings.append(
                    _finding(
                        "obligation-armed-ticket-stale", obligation.obligation_id,
                        "armed_ticket is not the unique matching open review ticket",
                    )
                )
        if obligation.state == "superseded" and obligation.outcome_ref is None:
            findings.append(
                _finding("obligation-superseded-without-outcome", obligation.obligation_id, "missing outcome_ref")
            )
    return tuple(findings)


def lint_stage_evidence(
    stages: Sequence[StageRecord], tickets_root: Path
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    counts = Counter(stage.stage_id for stage in stages)
    by_id = {stage.stage_id: stage for stage in stages}
    for stage_id, count in sorted(counts.items()):
        if count > 1:
            findings.append(_finding("stage-duplicate-id", stage_id, f"stage count is {count}"))
    for ordinal, name in _STAGE_NAMES.items():
        stage_id = f"ORG-061:STAGE-{ordinal}"
        if counts[stage_id] == 0:
            findings.append(_finding("stage-missing-id", stage_id, "canonical stage is absent"))
            continue
        if counts[stage_id] != 1:
            continue
        stage = by_id[stage_id]
        if stage.ordinal != ordinal:
            findings.append(_finding("stage-ordinal-drift", stage_id, f"expected ordinal {ordinal}"))
        if stage.name != name:
            findings.append(_finding("stage-name-drift", stage_id, f"expected name {name!r}"))
        expected_prerequisites = () if ordinal == 1 else (f"ORG-061:STAGE-{ordinal - 1}",)
        if stage.prerequisite_stage_ids != expected_prerequisites:
            findings.append(_finding("stage-prerequisite-drift", stage_id, "prerequisite chain is not canonical"))

    def ticket_done(ticket_id: str | None) -> bool:
        record = _ticket_record(tickets_root, ticket_id)
        return record is not None and record.path.parent.name == "done"

    def ticket_receipts(ticket_id: str | None) -> tuple[str, ...]:
        record = _ticket_record(tickets_root, ticket_id)
        if record is None:
            return ()
        values = record.fields.get("receipts", [])
        return tuple(values) if isinstance(values, list) and all(isinstance(v, str) for v in values) else ()

    def direct_dependencies(ticket_id: str) -> tuple[str, ...]:
        record = _ticket_record(tickets_root, ticket_id)
        if record is None:
            return ()
        values = record.fields.get("depends_on", [])
        return tuple(values) if isinstance(values, list) and all(isinstance(v, str) for v in values) else ()

    evidence_classes = ("functional", "continuity", "trust", "developmental")
    for stage in sorted(stages, key=lambda item: (item.ordinal, item.stage_id)):
        if stage.state != "passed":
            continue
        for class_name in evidence_classes:
            refs = getattr(stage.evidence_refs, class_name)
            if not refs:
                findings.append(
                    _finding(
                        f"stage-passed-missing-{class_name}-evidence", stage.stage_id,
                        f"passed stage has no {class_name} evidence",
                    )
                )
            for ref in refs:
                if not ref.startswith("ticket:") or isinstance(
                    resolve_ticket_receipt(ref, tickets_root, require_done=True), Finding
                ):
                    findings.append(
                        _finding("stage-passed-unresolved-evidence", stage.stage_id, f"unresolved {ref}")
                    )
        if not stage.denial_failure_rollback_refs:
            findings.append(
                _finding(
                    "stage-passed-missing-denial-failure-rollback", stage.stage_id,
                    "passed stage has no denial/failure/rollback evidence",
                )
            )
        for ref in stage.denial_failure_rollback_refs:
            if not ref.startswith("ticket:") or isinstance(
                resolve_ticket_receipt(ref, tickets_root, require_done=True), Finding
            ):
                findings.append(
                    _finding("stage-passed-unresolved-denial-failure-rollback", stage.stage_id, f"unresolved {ref}")
                )
        acceptance_receipts = ticket_receipts(stage.acceptance_ticket)
        if not ticket_done(stage.acceptance_ticket) or not acceptance_receipts:
            findings.append(
                _finding("stage-passed-invalid-acceptance-ticket", stage.stage_id, "acceptance ticket is not done with receipts")
            )
        if not any(
            parts is not None and parts[2] == "reviewer"
            for parts in (_receipt_parts(encoded) for encoded in acceptance_receipts)
        ):
            findings.append(
                _finding(
                    "stage-passed-missing-independent-review", stage.stage_id,
                    "acceptance ticket has no reviewer-provenance receipt",
                )
            )
        authority_resolution: ReceiptResolution | Finding | None = None
        if stage.acceptance_authority_ref is not None:
            authority_resolution = resolve_ticket_receipt(
                stage.acceptance_authority_ref, tickets_root, require_done=True
            )
        if stage.acceptance_authority_ref is None or isinstance(authority_resolution, Finding):
            findings.append(
                _finding(
                    "stage-passed-missing-acceptance-authority", stage.stage_id,
                    "acceptance authority receipt is absent or unresolved",
                )
            )
        elif isinstance(authority_resolution, ReceiptResolution):
            authority_ticket = _ticket_record(tickets_root, authority_resolution.ticket_id)
            if authority_ticket is None or authority_ticket.fields.get("target") != "operator":
                findings.append(
                    _finding(
                        "stage-passed-wrong-acceptance-authority", stage.stage_id,
                        "acceptance authority receipt is not carried by an operator-targeted ticket",
                    )
                )
        if stage.reorientation_gate_ticket == stage.acceptance_ticket:
            findings.append(
                _finding("stage-passed-gate-aliases-acceptance", stage.stage_id, "gate and acceptance tickets must differ")
            )
        if not ticket_done(stage.reorientation_gate_ticket):
            findings.append(
                _finding("stage-passed-invalid-reorientation-gate", stage.stage_id, "reorientation gate is not done")
            )
        if stage.acceptance_ticket not in direct_dependencies(stage.reorientation_gate_ticket or ""):
            findings.append(
                _finding(
                    "stage-passed-gate-missing-acceptance-dependency", stage.stage_id,
                    "reorientation gate does not directly depend on acceptance",
                )
            )
        gate_record = _ticket_record(tickets_root, stage.reorientation_gate_ticket)
        gate_body = " ".join((gate_record.body if gate_record else "").lower().split())
        missing_questions = [
            str(index)
            for index, phrase in enumerate(_GATE_QUESTION_PHRASES, 1)
            if phrase not in gate_body
        ]
        if missing_questions:
            findings.append(
                _finding(
                    "stage-passed-gate-missing-seven-question-record", stage.stage_id,
                    "missing reorientation questions " + ",".join(missing_questions),
                )
            )
        if stage.reorientation_receipt is None:
            findings.append(
                _finding(
                    "stage-passed-without-reorientation-receipt", stage.stage_id,
                    "passed stage lacks its completed gate receipt",
                )
            )
        else:
            resolution = resolve_ticket_receipt(stage.reorientation_receipt, tickets_root, require_done=True)
            if isinstance(resolution, Finding):
                findings.append(
                    _finding("stage-passed-invalid-reorientation-receipt", stage.stage_id, resolution.message)
                )
            elif resolution.ticket_id != stage.reorientation_gate_ticket:
                findings.append(
                    _finding("stage-passed-reorientation-receipt-ticket-mismatch", stage.stage_id, "receipt belongs to another ticket")
                )
        if stage.last_reorientation != stage.reorientation_receipt:
            findings.append(
                _finding("stage-passed-last-reorientation-drift", stage.stage_id, "last_reorientation does not equal gate receipt")
            )
        for production_ticket in stage.next_production_tickets:
            if production_ticket in {stage.acceptance_ticket, stage.reorientation_gate_ticket}:
                findings.append(
                    _finding(
                        "stage-next-production-aliases-gate", stage.stage_id,
                        f"{production_ticket} aliases acceptance or gate",
                    )
                )
            if stage.reorientation_gate_ticket not in direct_dependencies(production_ticket):
                findings.append(
                    _finding("stage-next-production-missing-direct-gate", stage.stage_id, f"{production_ticket} lacks direct gate dependency")
                )
        if stage.outstanding_risk_refs:
            findings.append(
                _finding("stage-passed-with-outstanding-risk", stage.stage_id, "passed stage retains outstanding risks")
            )

    stage4 = by_id.get("ORG-061:STAGE-4")
    stage5 = by_id.get("ORG-061:STAGE-5")
    if stage4 and stage5 and stage5.next_production_tickets:
        if not stage4.reorientation_gate_ticket or any(
            stage4.reorientation_gate_ticket not in direct_dependencies(ticket_id)
            for ticket_id in stage5.next_production_tickets
        ):
            findings.append(
                _finding("stage-bridge-before-echo-bypass", stage5.stage_id, "Stage 5 production bypasses Stage 4 gate")
            )
    return tuple(findings)


def resolve_ticket_receipt(
    ref: str, tickets_root: Path, *, require_done: bool
) -> ReceiptResolution | Finding:
    match = _RECEIPT_REF_RE.fullmatch(ref)
    if not match:
        return _finding("receipt-ref-invalid", ref, "expected ticket:T-NNNN#<receipt-id>")
    ticket_id, receipt_id = match.groups()
    paths = _ticket_paths(tickets_root, ticket_id)
    if len(paths) != 1:
        return _finding("receipt-ticket-count", ticket_id, f"expected one ticket, found {len(paths)}")
    path = paths[0]
    if require_done and path.parent.name != "done":
        return _finding("receipt-ticket-not-done", ticket_id, "receipt ticket is not done")
    raw_receipts = _ticket_fields(path).get("receipts", [])
    if not isinstance(raw_receipts, list):
        return _finding("receipt-list-invalid", ticket_id, "receipts is not a list")
    matches: list[str] = []
    for encoded in raw_receipts:
        if not isinstance(encoded, str) or encoded.count("=") < 2:
            continue
        _clause, middle, _provenance = encoded.split("=", 2)
        if middle == receipt_id:
            matches.append(encoded)
    if len(matches) != 1:
        return _finding("receipt-id-count", ticket_id, f"expected one {receipt_id!r}, found {len(matches)}")
    return ReceiptResolution(ticket_id=ticket_id, receipt_id=receipt_id, encoded_receipt=matches[0])


def due_obligations(
    obligations: Sequence[ReevaluationObligation], evidence: EvaluationInputs
) -> tuple[DueObligation, ...]:
    due: list[DueObligation] = []
    threshold_re = re.compile(
        r"metric:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>>=|<=|>|<|==)\s*"
        r"(?P<number>-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\Z"
    )
    milestone_re = re.compile(r"ticket-done:(T-[0-9]+)\Z")
    for obligation in sorted(obligations, key=lambda item: item.obligation_id):
        if obligation.state in {"open", "superseded"}:
            continue
        candidate: DueObligation | None = None
        if obligation.trigger_kind == "milestone":
            match = milestone_re.fullmatch(obligation.operational_predicate)
            if match and match.group(1) in evidence.resolved_ticket_ids:
                evidence_ref = f"ticket-done:{match.group(1)}"
                candidate = DueObligation(
                    obligation=obligation,
                    evidence_ref=evidence_ref,
                    prerequisite_ticket=match.group(1),
                )
        elif obligation.trigger_kind == "calendar":
            if obligation.operational_predicate != "next_check" or obligation.next_check is None:
                continue
            try:
                check = dt.datetime.fromisoformat(obligation.next_check.replace("Z", "+00:00"))
                if check.tzinfo is None:
                    check = check.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if evidence.now >= check and evidence.weekly_activity_ref:
                evidence_ref = f"calendar:{obligation.next_check}@{evidence.weekly_activity_ref}"
                candidate = DueObligation(
                    obligation=obligation,
                    evidence_ref=evidence_ref,
                    not_before=obligation.next_check,
                )
        elif obligation.trigger_kind == "threshold":
            match = threshold_re.fullmatch(obligation.operational_predicate)
            if not match:
                continue
            name = match.group("name")
            value = evidence.instrumented_metrics.get(name)
            evidence_ref = evidence.metric_evidence_refs.get(name)
            if value is None or evidence_ref is None or isinstance(value, bool) or not math.isfinite(value):
                continue
            threshold = float(match.group("number"))
            operation = match.group("op")
            crossed = {
                ">=": value >= threshold,
                "<=": value <= threshold,
                ">": value > threshold,
                "<": value < threshold,
                "==": value == threshold,
            }[operation]
            if crossed:
                candidate = DueObligation(obligation=obligation, evidence_ref=evidence_ref)
        # proposal and incident predicates are deliberately surface-only.
        if candidate and candidate.evidence_ref not in obligation.evidence_refs:
            due.append(candidate)
    return tuple(due)


def existing_review_ticket(obligation_id: str, tickets_root: Path) -> str | None:
    wanted = {"adr-reeval", f"reeval:{obligation_id}"}
    matches: list[str] = []
    for ticket_id, fields in _open_ticket_fields(tickets_root):
        labels = fields.get("labels", [])
        if isinstance(labels, list) and wanted.issubset(set(labels)):
            matches.append(ticket_id)
    return matches[0] if len(matches) == 1 else None


def build_review_ticket_args(
    obligation: ReevaluationObligation, due: DueObligation
) -> tuple[str, ...]:
    if due.obligation.obligation_id != obligation.obligation_id:
        raise ValueError("due-obligation-mismatch")
    title = f"Re-evaluate {obligation.obligation_id}: owned trigger due"
    ask = (
        f"Review owned re-evaluation obligation {obligation.obligation_id} against its "
        f"canonical source {obligation.source_ref}. Evidence: {due.evidence_ref}. "
        "Record a real disposition and receipt; this evaluator does not amend architecture "
        "or change stage state."
    )
    args = [
        "file", "--requester", "recovery", "--target", obligation.review_target,
        "--title", title,
        "--labels", f"adr-reeval,reeval:{obligation.obligation_id},adr,process",
        "--project-id", "null",
        "--nearest-intent-ref", "maintenance:grand-project-program",
        "--ask", ask,
        "--context", f"Observable source: {obligation.observable_source}",
        "--serves", "Owned architecture re-evaluation obligations fire from evidence (ORG-061)",
    ]
    if due.prerequisite_ticket is not None:
        args.extend(("--depends-on", due.prerequisite_ticket))
    if due.not_before is not None:
        args.extend(
            (
                "--not-before", due.not_before,
                "--not-before-anchor", f"event:cadence:{due.not_before}",
            )
        )
    return tuple(args)


def evaluate_indexes(
    indexes: ProgramIndexes,
    evidence: EvaluationInputs,
    tickets_root: Path,
    *,
    max_new: int = 1,
) -> EvaluationReport:
    if type(max_new) is not int or max_new < 0:
        raise ValueError("max_new must be a nonnegative integer")
    due = due_obligations(indexes.obligations, evidence)
    findings: list[Finding] = []
    due_ids: list[str] = []
    existing: list[str] = []
    proposed: list[tuple[str, ...]] = []
    for candidate in due:
        obligation = candidate.obligation
        due_ids.append(obligation.obligation_id)
        duplicate = existing_review_ticket(obligation.obligation_id, tickets_root)
        if duplicate is not None:
            existing.append(duplicate)
            continue
        matching_count = 0
        wanted = {"adr-reeval", f"reeval:{obligation.obligation_id}"}
        for _ticket_id, fields in _open_ticket_fields(tickets_root):
            labels = fields.get("labels", [])
            if isinstance(labels, list) and wanted.issubset(set(labels)):
                matching_count += 1
        if matching_count > 1:
            findings.append(
                _finding(
                    "obligation-review-ticket-duplicate", obligation.obligation_id,
                    f"found {matching_count} matching open tickets",
                )
            )
            continue
        if len(proposed) < max_new:
            proposed.append(build_review_ticket_args(obligation, candidate))
    surfaced = tuple(
        obligation.obligation_id
        for obligation in sorted(indexes.obligations, key=lambda item: item.obligation_id)
        if obligation.state != "superseded" and obligation.trigger_kind in {"proposal", "incident"}
    )
    return EvaluationReport(
        findings=tuple(findings),
        due_obligation_ids=tuple(due_ids),
        surfaced_obligation_ids=surfaced,
        existing_ticket_ids=tuple(existing),
        proposed_ticket_args=tuple(proposed),
    )


def lint_repository_indexes(repo_root: Path) -> tuple[Finding, ...]:
    stage_path = repo_root / "tickets" / "grand-project-stage-evidence.yaml"
    obligation_path = repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
    stages = load_stage_index(stage_path)
    obligations = load_obligation_index(obligation_path)
    source = scan_source_obligations(repo_root / "architecture" / "adr")
    findings = list(
        lint_obligation_completeness(source, obligations, repo_root / "tickets")
        + lint_stage_evidence(stages, repo_root / "tickets")
    )
    obligations_by_id = {item.obligation_id: item for item in obligations}
    for stage in stages:
        if stage.state != "passed":
            continue
        for obligation_id in stage.applicable_obligation_ids:
            obligation = obligations_by_id.get(obligation_id)
            if obligation is None:
                findings.append(
                    _finding(
                        "stage-passed-applicable-obligation-missing", stage.stage_id,
                        f"unknown obligation {obligation_id}",
                    )
                )
            elif obligation.state in {"due", "open"}:
                findings.append(
                    _finding(
                        "stage-passed-with-due-obligation", stage.stage_id,
                        f"{obligation_id} is {obligation.state}",
                    )
                )
    return tuple(findings)
