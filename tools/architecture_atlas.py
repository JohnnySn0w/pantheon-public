"""Architecture atlas derivation and validation contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AbstractSet, TypeAlias

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import component_catalogue, grand_project_program, organism_topology, project_catalogue
from tools.organism_topology import EvidenceSnapshot


JSONScalar: TypeAlias = str | int | float | bool | None
JSONObject: TypeAlias = Mapping[str, "JSONValue"]
JSONArray: TypeAlias = Sequence["JSONValue"]
JSONValue: TypeAlias = JSONScalar | JSONObject | JSONArray
JSONProjectionValue: TypeAlias = JSONValue | tuple[JSONValue, ...]


MAX_ATLAS_REQUEST_BYTES = 256 * 1024
MAX_ATLAS_STYLE_BYTES = 64 * 1024
MAX_ATLAS_AUTHORITY_BYTES = 1024 * 1024
ATLAS_STABLE_ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
ATLAS_STABLE_ID_RE = re.compile(ATLAS_STABLE_ID_PATTERN)
ATLAS_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ATLAS_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
ATLAS_BUNDLE_PATH_PATTERN = r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*//)[A-Za-z0-9._/-]+$"
ATLAS_BUNDLE_FILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
ATLAS_BUNDLE_FILE_RE = re.compile(ATLAS_BUNDLE_FILE_PATTERN)
ATLAS_BUNDLE_MANIFEST = "fixture-bundle.json"
ATLAS_IDENTITY_TO_AUTHORITY = {
    "project": "project",
    "organ": "organ",
    "component": "component",
    "surface": "surface",
}

PROJECT_SELECTOR_ALLOWLIST = frozenset(
    (
        "project_id",
        "telos",
        "parent_goal_ref",
        "classification",
        "boundary.inside",
        "boundary.outside",
        "boundary.not_owned",
        "interfaces.provides",
        "interfaces.requires",
        "stewardship.role",
        "stewardship.authority_surface",
        "lifecycle.developmental_mode",
        "lifecycle.formed_at",
        "lifecycle.transition_refs",
        "evidence.health",
        "evidence.user_value",
        "evidence.safety",
        "evidence.developmental",
        "tensions.pole_a",
        "tensions.pole_b",
        "tensions.rebalance_trigger",
        "operational_contract.obligations.deployment",
        "operational_contract.obligations.data",
        "operational_contract.obligations.security",
        "operational_contract.obligations.cost",
        "operational_contract.obligations.maintenance",
        "operational_contract.obligations.recovery",
        "operational_contract.lifecycle.install",
        "operational_contract.lifecycle.configure",
        "operational_contract.lifecycle.health_monitor",
        "operational_contract.lifecycle.update",
        "operational_contract.lifecycle.remove",
    )
)

COMPONENT_SELECTOR_ALLOWLIST = frozenset(
    (
        "component_id",
        "project_id",
        "kind",
        "source.logical_repo",
        "source.artifact",
        "provides.capability",
        "provides.contract",
        "provides.schema",
        "provides.version",
        "provides.features",
        "requires.hard.capability",
        "requires.hard.contract",
        "requires.hard.schema",
        "requires.hard.version",
        "requires.hard.feature",
        "requires.hard.enabled",
        "requires.hard.degradation_ref",
        "requires.optional.capability",
        "requires.optional.contract",
        "requires.optional.schema",
        "requires.optional.version",
        "requires.optional.feature",
        "requires.optional.enabled",
        "requires.optional.degradation_ref",
        "compatibility.tested_revision",
        "deployment.loci",
        "deployment.trust_zone",
        "implications.authority.evidence_refs",
        "implications.privacy_data.evidence_refs",
        "implications.hardware_network.evidence_refs",
        "implications.operational.evidence_refs",
        "upkeep.steward",
        "upkeep.health_evidence",
        "upkeep.monitoring",
        "upkeep.upgrade_rollback",
        "upkeep.credential_rotation",
        "upkeep.storage_retention",
        "upkeep.review_obligations",
        "maturity.developmental_mode",
        "maturity.replaceability",
        "tensions.tension_id",
        "tensions.pole_a",
        "tensions.pole_b",
        "tensions.rebalance_trigger",
    )
)

ORGAN_SELECTOR_ALLOWLIST = frozenset(
    (
        "organ_id",
        "responsibility",
        "must_not_become",
        "ports.inputs.port_id",
        "ports.inputs.contract_ref",
        "ports.inputs.schema_ref",
        "ports.inputs.version_range",
        "ports.inputs.authority_ref",
        "ports.inputs.confidentiality",
        "ports.inputs.freshness_bound",
        "ports.outputs.port_id",
        "ports.outputs.contract_ref",
        "ports.outputs.schema_ref",
        "ports.outputs.version_range",
        "ports.outputs.authority_ref",
        "ports.outputs.confidentiality",
        "ports.outputs.freshness_bound",
        "authoritative_inputs",
        "authoritative_outputs",
        "boundaries.identity",
        "boundaries.data",
        "boundaries.trust",
        "boundaries.authority",
        "expectations.latency",
        "expectations.availability",
        "health.required_facets",
        "health.evidence_requirements",
        "failure_modes",
        "degradation.unavailable",
        "degradation.stale",
        "degradation.partitioned",
        "degradation.inconsistent",
        "degradation.unknown",
        "recovery.owner_ref",
        "recovery.escalation_ref",
        "recovery.verification_requirements",
        "replacement.continuity_requirements",
        "replacement.provenance_requirements",
        "replacement.rollback_requirements",
        "owners",
        "project_refs",
        "component_refs",
        "obligation_refs",
        "re_evaluate_when",
    )
)

BINDING_SELECTOR_ALLOWLIST = frozenset(
    (
        "binding_id",
        "organ_id",
        "implementation_ref.kind",
        "implementation_ref.id",
        "seed_ref",
        "locus",
        "ports",
        "lifecycle",
        "authority_owner_ref",
        "evidence_refs",
        "verified_at",
        "fresh_until",
        "health.facets.reachability",
        "health.facets.liveness",
        "health.facets.readiness",
        "health.facets.correctness",
        "health.facets.freshness",
        "health.facets.authority",
        "health.facets.continuity",
    )
)

DEPENDENCY_SELECTOR_ALLOWLIST = frozenset(
    (
        "dependency_id",
        "consumer.organ_id",
        "consumer.port_id",
        "provider.organ_id",
        "provider.port_id",
        "provider.capability_ref",
        "relationship",
        "contract_ref",
        "schema_ref",
        "version_range",
        "freshness_bound",
        "failure_propagation",
        "degraded_behavior",
        "owner_ref",
        "evidence_refs",
        "selection.authority_ref",
        "selection.rule",
        "selection.priority",
        "selection.evidence_refs",
        "feedback.bootstrap",
        "feedback.bound",
        "feedback.break_condition",
    )
)

SURFACE_SELECTOR_ALLOWLIST = frozenset(
    (
        "surface_id",
        "attached_port.organ_id",
        "attached_port.port_id",
        "transport",
        "audience",
        "boundaries.identity",
        "boundaries.consent",
        "boundaries.trust",
        "boundaries.authority",
        "owner_ref",
        "degradation",
        "evidence_refs",
    )
)

STAGE_SELECTOR_ALLOWLIST = frozenset(
    (
        "stage_id",
        "ordinal",
        "name",
        "outcome_ref",
        "state",
        "steward_ref",
        "prerequisite_stage_ids",
        "research_probe_allowed",
        "research_probe_boundary_ref",
        "evidence_refs.functional",
        "evidence_refs.continuity",
        "evidence_refs.trust",
        "evidence_refs.developmental",
        "denial_failure_rollback_refs",
        "acceptance_ticket",
        "acceptance_authority_ref",
        "reorientation_gate_ticket",
        "reorientation_receipt",
        "next_production_tickets",
        "applicable_obligation_ids",
        "outstanding_risk_refs",
        "last_reorientation",
    )
)

OBLIGATION_SELECTOR_ALLOWLIST = frozenset(
    (
        "obligation_id",
        "source_ref",
        "owner_ref",
        "review_target",
        "trigger_kind",
        "operational_predicate",
        "observable_source",
        "state",
        "armed_ticket",
        "last_evaluated",
        "next_check",
        "evidence_refs",
        "outcome_ref",
    )
)

COMPONENT_SELECTOR_ALIASES = {
    "requires.hard": "hard_requirements",
    "requires.optional": "optional_requirements",
}


class AtlasError(RuntimeError):
    """Raised when atlas tooling cannot complete a requested operation."""


class AtlasViewKind(str, Enum):
    BOUNDARY_PURPOSE = "boundary-purpose"
    INTERFACES_TRUST = "interfaces-trust"
    DEGRADATION_RECOVERY = "degradation-recovery"
    EVIDENCE_REORIENTATION = "evidence-reorientation"


class AtlasIdentityKind(str, Enum):
    PROJECT = "project"
    ORGAN = "organ"
    COMPONENT = "component"
    SURFACE = "surface"


class AtlasAuthorityKind(str, Enum):
    PROJECT = "project"
    COMPONENT = "component"
    ORGAN = "organ"
    SURFACE = "surface"
    BINDING = "binding"
    DEPENDENCY = "dependency"
    STAGE = "stage"
    OBLIGATION = "obligation"


class AtlasSourceClass(str, Enum):
    REPO_DOCUMENT = "repo-document"
    EVIDENCE_SNAPSHOT = "evidence-snapshot"


class AtlasContentRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class AtlasRelationKind(str, Enum):
    JUSTIFIES_UPWARD = "justifies-upward"
    BOUNDARY = "boundary"
    PROVIDES = "provides"
    REQUIRES = "requires"
    TRUST_AUTHORITY_SEAM = "trust-authority-seam"
    DEGRADES_TO = "degrades-to"
    RECOVERS_BY = "recovers-by"
    EVIDENCED_BY = "evidenced-by"
    REORIENTS_ON = "reorients-on"


class AtlasProjectionStatus(str, Enum):
    DERIVED = "derived"
    DECLARED = "declared"
    EVIDENCE_PROVEN = "evidence-proven"
    UNKNOWN = "unknown"


ATLAS_SELECTOR_ALLOWLISTS = {
    AtlasAuthorityKind.PROJECT: PROJECT_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.COMPONENT: COMPONENT_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.ORGAN: ORGAN_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.BINDING: BINDING_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.DEPENDENCY: DEPENDENCY_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.SURFACE: SURFACE_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.STAGE: STAGE_SELECTOR_ALLOWLIST,
    AtlasAuthorityKind.OBLIGATION: OBLIGATION_SELECTOR_ALLOWLIST,
}


@dataclass(frozen=True)
class AtlasIssue:
    code: str
    record_id: str
    detail: str


@dataclass(frozen=True)
class AtlasIdentityRef:
    kind: AtlasIdentityKind
    id: str


@dataclass(frozen=True)
class AtlasAuthorityRef:
    kind: AtlasAuthorityKind
    id: str


@dataclass(frozen=True)
class AtlasFieldSelector:
    authority_ref: AtlasAuthorityRef
    field_path: str


@dataclass(frozen=True)
class SourcePin:
    source_class: AtlasSourceClass
    canonical_ref: str
    authority_path: str | None
    document_sha256: str
    authority_digest: str
    field_path: str


@dataclass(frozen=True)
class AtlasNodeRequest:
    node_id: str
    identity_ref: AtlasIdentityRef
    role: AtlasContentRole
    selectors: tuple[AtlasFieldSelector, ...]
    rank: int
    order: int
    necessary_for: str | None


@dataclass(frozen=True)
class AtlasEdgeRequest:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: AtlasRelationKind
    role: AtlasContentRole
    source_selectors: tuple[AtlasFieldSelector, ...]
    necessary_for: str | None


@dataclass(frozen=True)
class AtlasStyle:
    schema_version: int
    canvas_width: int
    canvas_height: int
    background_color: str
    primary_fill_color: str
    secondary_fill_color: str
    stroke_color: str
    text_color: str
    font_family: int
    font_size: int
    stroke_width: int
    rank_gap: int
    order_gap: int
    node_width: int
    node_height: int


@dataclass(frozen=True)
class AtlasViewRequest:
    schema_version: int
    view_id: str
    view_kind: AtlasViewKind
    primary_question: str
    exclusions: tuple[str, ...]
    subject_refs: tuple[AtlasIdentityRef, ...]
    supporting_refs: tuple[AtlasAuthorityRef, ...]
    nodes: tuple[AtlasNodeRequest, ...]
    edges: tuple[AtlasEdgeRequest, ...]


@dataclass(frozen=True)
class AtlasProjection:
    selector: AtlasFieldSelector
    value: JSONProjectionValue
    source_pins: tuple[SourcePin, ...]
    status: AtlasProjectionStatus


@dataclass(frozen=True)
class AtlasNode:
    node_id: str
    identity_ref: AtlasIdentityRef
    role: AtlasContentRole
    selectors: tuple[AtlasFieldSelector, ...]
    rank: int
    order: int
    necessary_for: str | None
    projections: tuple[AtlasProjection, ...]


@dataclass(frozen=True)
class AtlasEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: AtlasRelationKind
    role: AtlasContentRole
    source_selectors: tuple[AtlasFieldSelector, ...]
    necessary_for: str | None
    projections: tuple[AtlasProjection, ...]


@dataclass(frozen=True)
class AtlasViewSpec:
    schema_version: int
    request_sha256: str
    view_id: str
    view_kind: AtlasViewKind
    primary_question: str
    exclusions: tuple[str, ...]
    subject_refs: tuple[AtlasIdentityRef, ...]
    supporting_refs: tuple[AtlasAuthorityRef, ...]
    nodes: tuple[AtlasNode, ...]
    edges: tuple[AtlasEdge, ...]
    source_pins: tuple[SourcePin, ...]
    visible_text_sha256: str


@dataclass(frozen=True)
class AtlasBundleEntry:
    view_id: str
    request_path: str
    spec_path: str
    scene_path: str
    request_sha256: str
    spec_sha256: str
    scene_sha256: str


@dataclass(frozen=True)
class AtlasBundleManifest:
    schema_version: int
    bundle_id: str
    style_path: str
    style_sha256: str
    entries: tuple[AtlasBundleEntry, ...]


@dataclass(frozen=True)
class AtlasAuthorities:
    records: Mapping[AtlasAuthorityRef, object]
    record_paths: Mapping[AtlasAuthorityRef, str]
    source_bytes: Mapping[str, bytes]
    source_sha256: Mapping[str, str]
    generated_index_bytes: Mapping[str, bytes]
    generated_index_sha256: Mapping[str, str]
    authority_digests: Mapping[AtlasAuthorityKind, str]
    evidence: EvidenceSnapshot
    now: datetime


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AtlasError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_object(path: Path, *, max_bytes: int) -> Mapping[str, object]:
    if max_bytes <= 0:
        raise AtlasError("max_bytes must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AtlasError(f"cannot open JSON input: {path}: {exc.strerror}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise AtlasError(f"JSON input must be a regular file: {path}")
        raw = os.read(fd, max_bytes + 1)
        if len(raw) > max_bytes:
            raise AtlasError(f"JSON input exceeds {max_bytes} bytes: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AtlasError(f"JSON input is not UTF-8: {path}") from exc
        try:
            parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except AtlasError:
            raise
        except json.JSONDecodeError as exc:
            raise AtlasError(f"invalid JSON object: {path}: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise AtlasError(f"JSON input must be an object: {path}")
        return parsed
    finally:
        os.close(fd)


def _load_regular_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise AtlasError("max_bytes must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AtlasError(f"cannot open authority source: {path}: {exc.strerror}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise AtlasError(f"authority source must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise AtlasError(f"authority source exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise AtlasError(f"authority source exceeds {max_bytes} bytes: {path}")
        return raw
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise AtlasError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise AtlasError(f"authority path escapes repo root: {path}") from exc


def _read_source(
    repo_root: Path,
    rel_path: str,
    source_bytes: dict[str, bytes],
    source_sha256: dict[str, str],
) -> None:
    raw = _load_regular_file_bytes(
        repo_root / rel_path, max_bytes=MAX_ATLAS_AUTHORITY_BYTES
    )
    source_bytes[rel_path] = raw
    source_sha256[rel_path] = _sha256(raw)


def _ref(kind: AtlasAuthorityKind, record_id: str) -> AtlasAuthorityRef:
    return AtlasAuthorityRef(kind=kind, id=record_id)


def _issue_detail(issue: object) -> str:
    code = getattr(issue, "code", type(issue).__name__)
    record_id = getattr(issue, "record_id", getattr(issue, "project_id", getattr(issue, "component_id", "*")))
    detail = getattr(issue, "detail", getattr(issue, "message", repr(issue)))
    return f"{code}:{record_id}:{detail}"


def _exact_mapping(
    value: object, *, path: str, keys: Sequence[str]
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AtlasError(f"{path} must be an object")
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise AtlasError(f"{path} keys differ: missing={missing!r} unknown={unknown!r}")
    return value


def _nonempty_str(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise AtlasError(f"{path} must be a nonempty string")
    return value


def _optional_nonempty_str(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    return _nonempty_str(value, path=path)


def _stable_id(value: object, *, path: str) -> str:
    parsed = _nonempty_str(value, path=path)
    if ATLAS_STABLE_ID_RE.fullmatch(parsed) is None:
        raise AtlasError(f"{path} must match {ATLAS_STABLE_ID_PATTERN}")
    return parsed


def _int_0_5(value: object, *, path: str) -> int:
    if type(value) is not int:
        raise AtlasError(f"{path} must be an integer")
    if value < 0 or value > 5:
        raise AtlasError(f"{path} must be between 0 and 5")
    return value


def _bounded_int(value: object, *, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise AtlasError(f"{path} must be an integer")
    if value < minimum or value > maximum:
        raise AtlasError(f"{path} must be between {minimum} and {maximum}")
    return value


def _enum_value(enum_type: type[Enum], value: object, *, path: str) -> Enum:
    parsed = _nonempty_str(value, path=path)
    try:
        return enum_type(parsed)
    except ValueError as exc:
        raise AtlasError(f"{path} has unsupported enum value: {parsed}") from exc


def _list(value: object, *, path: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise AtlasError(f"{path} must be a list")
    return value


def _unique_tuple(values: Sequence[object], *, path: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        parsed = _nonempty_str(item, path=f"{path}[{index}]")
        if parsed in seen:
            raise AtlasError(f"{path} contains duplicate value: {parsed}")
        seen.add(parsed)
        result.append(parsed)
    return tuple(result)


def _parse_identity_ref(value: object, *, path: str) -> AtlasIdentityRef:
    data = _exact_mapping(value, path=path, keys=("kind", "id"))
    kind = _enum_value(AtlasIdentityKind, data["kind"], path=f"{path}.kind")
    assert isinstance(kind, AtlasIdentityKind)
    return AtlasIdentityRef(kind=kind, id=_nonempty_str(data["id"], path=f"{path}.id"))


def _parse_authority_ref(value: object, *, path: str) -> AtlasAuthorityRef:
    data = _exact_mapping(value, path=path, keys=("kind", "id"))
    kind = _enum_value(AtlasAuthorityKind, data["kind"], path=f"{path}.kind")
    assert isinstance(kind, AtlasAuthorityKind)
    return AtlasAuthorityRef(
        kind=kind,
        id=_nonempty_str(data["id"], path=f"{path}.id"),
    )


def _parse_field_selector(value: object, *, path: str) -> AtlasFieldSelector:
    data = _exact_mapping(value, path=path, keys=("authority_ref", "field_path"))
    return AtlasFieldSelector(
        authority_ref=_parse_authority_ref(
            data["authority_ref"], path=f"{path}.authority_ref"
        ),
        field_path=_nonempty_str(data["field_path"], path=f"{path}.field_path"),
    )


def _parse_selectors(
    value: object, *, path: str, require_nonempty: bool
) -> tuple[AtlasFieldSelector, ...]:
    raw = _list(value, path=path)
    if require_nonempty and not raw:
        raise AtlasError(f"{path} must be nonempty")
    if len(raw) > 6:
        raise AtlasError(f"{path} cannot contain more than 6 selectors")
    return tuple(
        _parse_field_selector(item, path=f"{path}[{index}]")
        for index, item in enumerate(raw)
    )


def _parse_node_request(value: object, *, path: str) -> AtlasNodeRequest:
    data = _exact_mapping(
        value,
        path=path,
        keys=(
            "node_id",
            "identity_ref",
            "role",
            "selectors",
            "rank",
            "order",
            "necessary_for",
        ),
    )
    role = _enum_value(AtlasContentRole, data["role"], path=f"{path}.role")
    assert isinstance(role, AtlasContentRole)
    necessary_for = _optional_nonempty_str(
        data["necessary_for"], path=f"{path}.necessary_for"
    )
    if role is AtlasContentRole.PRIMARY and necessary_for is not None:
        raise AtlasError(f"{path}.necessary_for must be null for primary content")
    if role is AtlasContentRole.SECONDARY and necessary_for is None:
        raise AtlasError(f"{path}.necessary_for must be nonempty for secondary content")
    return AtlasNodeRequest(
        node_id=_stable_id(data["node_id"], path=f"{path}.node_id"),
        identity_ref=_parse_identity_ref(data["identity_ref"], path=f"{path}.identity_ref"),
        role=role,
        selectors=_parse_selectors(data["selectors"], path=f"{path}.selectors", require_nonempty=True),
        rank=_int_0_5(data["rank"], path=f"{path}.rank"),
        order=_int_0_5(data["order"], path=f"{path}.order"),
        necessary_for=necessary_for,
    )


def _parse_edge_request(value: object, *, path: str) -> AtlasEdgeRequest:
    data = _exact_mapping(
        value,
        path=path,
        keys=(
            "edge_id",
            "source_node_id",
            "target_node_id",
            "relation",
            "role",
            "source_selectors",
            "necessary_for",
        ),
    )
    role = _enum_value(AtlasContentRole, data["role"], path=f"{path}.role")
    relation = _enum_value(AtlasRelationKind, data["relation"], path=f"{path}.relation")
    assert isinstance(role, AtlasContentRole)
    assert isinstance(relation, AtlasRelationKind)
    necessary_for = _optional_nonempty_str(
        data["necessary_for"], path=f"{path}.necessary_for"
    )
    if role is AtlasContentRole.PRIMARY and necessary_for is not None:
        raise AtlasError(f"{path}.necessary_for must be null for primary content")
    if role is AtlasContentRole.SECONDARY and necessary_for is None:
        raise AtlasError(f"{path}.necessary_for must be nonempty for secondary content")
    return AtlasEdgeRequest(
        edge_id=_stable_id(data["edge_id"], path=f"{path}.edge_id"),
        source_node_id=_stable_id(
            data["source_node_id"], path=f"{path}.source_node_id"
        ),
        target_node_id=_stable_id(
            data["target_node_id"], path=f"{path}.target_node_id"
        ),
        relation=relation,
        role=role,
        source_selectors=_parse_selectors(
            data["source_selectors"], path=f"{path}.source_selectors", require_nonempty=False
        ),
        necessary_for=necessary_for,
    )


def _question(value: object) -> str:
    parsed = _nonempty_str(value, path="primary_question")
    if len(parsed.encode("utf-8")) > 240:
        raise AtlasError("primary_question must be at most 240 UTF-8 bytes")
    if parsed.count("?") != 1:
        raise AtlasError("primary_question must contain exactly one question mark")
    return parsed


def _color(value: object, *, path: str) -> str:
    parsed = _nonempty_str(value, path=path)
    if ATLAS_COLOR_RE.fullmatch(parsed) is None:
        raise AtlasError(f"{path} must be a #RRGGBB color")
    return parsed


def _font_family(value: object, *, path: str) -> int:
    parsed = _bounded_int(value, path=path, minimum=1, maximum=3)
    if parsed not in {1, 2, 3}:
        raise AtlasError(f"{path} must be one of 1, 2, 3")
    return parsed


def _subject_authority_refs(
    subject_refs: Sequence[AtlasIdentityRef],
) -> set[tuple[AtlasAuthorityKind, str]]:
    result: set[tuple[AtlasAuthorityKind, str]] = set()
    for ref in subject_refs:
        kind = AtlasAuthorityKind(ATLAS_IDENTITY_TO_AUTHORITY[ref.kind.value])
        result.add((kind, ref.id))
    return result


def _authority_key(ref: AtlasAuthorityRef) -> tuple[AtlasAuthorityKind, str]:
    return (ref.kind, ref.id)


def _validate_request_graph(request: AtlasViewRequest) -> None:
    subject_pairs = {(ref.kind, ref.id) for ref in request.subject_refs}
    if len(subject_pairs) != len(request.subject_refs):
        raise AtlasError("subject_refs must be unique")
    supporting_keys = {_authority_key(ref) for ref in request.supporting_refs}
    if len(supporting_keys) != len(request.supporting_refs):
        raise AtlasError("supporting_refs must be unique")
    subject_authorities = _subject_authority_refs(request.subject_refs)

    primary_nodes = [node for node in request.nodes if node.role is AtlasContentRole.PRIMARY]
    secondary_nodes = [
        node for node in request.nodes if node.role is AtlasContentRole.SECONDARY
    ]
    if len(primary_nodes) > 12:
        raise AtlasError("request cannot contain more than 12 primary nodes")
    if len(secondary_nodes) > 6:
        raise AtlasError("request cannot contain more than 6 secondary nodes")

    node_ids = [node.node_id for node in request.nodes]
    if len(set(node_ids)) != len(node_ids):
        raise AtlasError("node_id values must be unique")
    slots = [(node.rank, node.order) for node in request.nodes]
    if len(set(slots)) != len(slots):
        raise AtlasError("node rank/order slots must be unique")

    for node in request.nodes:
        if (node.identity_ref.kind, node.identity_ref.id) not in subject_pairs:
            raise AtlasError(f"node identity is not a subject ref: {node.node_id}")
        for selector in node.selectors:
            _validate_selector_authority(selector, subject_authorities, supporting_keys)

    if len(request.edges) > 24:
        raise AtlasError("request cannot contain more than 24 edges")
    edge_ids = [edge.edge_id for edge in request.edges]
    if len(set(edge_ids)) != len(edge_ids):
        raise AtlasError("edge_id values must be unique")
    node_id_set = set(node_ids)
    for edge in request.edges:
        if edge.source_node_id not in node_id_set:
            raise AtlasError(f"edge source node does not exist: {edge.edge_id}")
        if edge.target_node_id not in node_id_set:
            raise AtlasError(f"edge target node does not exist: {edge.edge_id}")
        for selector in edge.source_selectors:
            _validate_selector_authority(selector, subject_authorities, supporting_keys)


def _validate_selector_authority(
    selector: AtlasFieldSelector,
    subject_authorities: set[tuple[AtlasAuthorityKind, str]],
    supporting_keys: set[tuple[AtlasAuthorityKind, str]],
) -> None:
    authority = selector.authority_ref
    if _authority_key(authority) in subject_authorities:
        return
    if _authority_key(authority) in supporting_keys:
        return
    raise AtlasError(
        f"selector authority is not a subject or supporting ref: {authority.kind.value}:{authority.id}"
    )


def _translated_selector_parts(kind: AtlasAuthorityKind, field_path: str) -> tuple[str, ...]:
    allowlist = ATLAS_SELECTOR_ALLOWLISTS[kind]
    if field_path not in allowlist:
        raise AtlasError(f"selector field is not allowed for {kind.value}: {field_path}")
    if kind is AtlasAuthorityKind.BINDING and field_path.startswith("health.facets."):
        raise AtlasError("binding health facet selectors require health projection")
    translated = field_path
    if kind is AtlasAuthorityKind.COMPONENT:
        for prefix, replacement in COMPONENT_SELECTOR_ALIASES.items():
            if field_path.startswith(prefix + "."):
                translated = replacement + field_path[len(prefix) :]
                break
    return tuple(translated.split("."))


def _dataclass_field(value: object, name: str, *, field_path: str) -> object:
    if not is_dataclass(value) or isinstance(value, type):
        raise AtlasError(f"{field_path} cannot traverse non-dataclass value")
    if name not in {field.name for field in fields(value)}:
        raise AtlasError(f"{field_path} is not a canonical field")
    return getattr(value, name)


def _json_scalar(value: object, *, field_path: str) -> JSONScalar:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) in (int, float):
        return value
    raise AtlasError(f"{field_path} is not a JSON scalar")


def _json_leaf(value: object, *, field_path: str) -> JSONProjectionValue:
    if isinstance(value, (bytes, bytearray)):
        raise AtlasError(f"{field_path} cannot expose raw bytes")
    if isinstance(value, Mapping):
        raise AtlasError(f"{field_path} cannot expose nested mappings")
    if is_dataclass(value) and not isinstance(value, type):
        raise AtlasError(f"{field_path} cannot expose nested dataclasses")
    if isinstance(value, (tuple, list)):
        return tuple(_json_scalar(item, field_path=field_path) for item in value)
    return _json_scalar(value, field_path=field_path)


def _extract_selector_value(value: object, parts: tuple[str, ...], *, field_path: str) -> JSONProjectionValue:
    if not parts:
        return _json_leaf(value, field_path=field_path)
    if isinstance(value, (tuple, list)):
        flattened: list[JSONValue] = []
        for item in value:
            extracted = _extract_selector_value(item, parts, field_path=field_path)
            if isinstance(extracted, tuple):
                flattened.extend(extracted)
            else:
                flattened.append(extracted)
        return tuple(flattened)
    if isinstance(value, Mapping):
        raise AtlasError(f"{field_path} cannot traverse nested mappings")
    if isinstance(value, (bytes, bytearray)):
        raise AtlasError(f"{field_path} cannot traverse raw bytes")
    child = _dataclass_field(value, parts[0], field_path=field_path)
    return _extract_selector_value(child, parts[1:], field_path=field_path)


def _canonical_ref(ref: AtlasAuthorityRef, field_path: str) -> str:
    prefix = f"{ref.kind.value}:"
    record_id = ref.id if ref.id.startswith(prefix) else prefix + ref.id
    return f"{record_id}#{field_path}"


def _projection_status(kind: AtlasAuthorityKind, field_path: str) -> AtlasProjectionStatus:
    if field_path.startswith("lifecycle.") or field_path.startswith("maturity."):
        return AtlasProjectionStatus.DECLARED
    if kind is AtlasAuthorityKind.BINDING and field_path == "lifecycle":
        return AtlasProjectionStatus.DECLARED
    if field_path == "state":
        return AtlasProjectionStatus.DECLARED
    return AtlasProjectionStatus.DERIVED


def _build_ordinary_projection(
    selector: AtlasFieldSelector, authorities: AtlasAuthorities
) -> AtlasProjection:
    ref = selector.authority_ref
    if ref not in authorities.records:
        raise AtlasError(f"selector authority record is missing: {ref.kind.value}:{ref.id}")
    parts = _translated_selector_parts(ref.kind, selector.field_path)
    value = _extract_selector_value(
        authorities.records[ref], parts, field_path=selector.field_path
    )
    authority_path = authorities.record_paths.get(ref)
    if authority_path is None:
        raise AtlasError(f"selector authority path is missing: {ref.kind.value}:{ref.id}")
    document_sha256 = authorities.source_sha256.get(authority_path)
    if document_sha256 is None:
        raise AtlasError(f"selector authority source digest is missing: {authority_path}")
    authority_digest = authorities.authority_digests.get(ref.kind)
    if authority_digest is None:
        raise AtlasError(f"selector authority family digest is missing: {ref.kind.value}")
    return AtlasProjection(
        selector=selector,
        value=value,
        source_pins=(
            SourcePin(
                source_class=AtlasSourceClass.REPO_DOCUMENT,
                canonical_ref=_canonical_ref(ref, selector.field_path),
                authority_path=authority_path,
                document_sha256=document_sha256,
                authority_digest=authority_digest,
                field_path=selector.field_path,
            ),
        ),
        status=_projection_status(ref.kind, selector.field_path),
    )


def _health_facet(field_path: str) -> organism_topology.HealthFacet:
    prefix = "health.facets."
    if not field_path.startswith(prefix):
        raise AtlasError("health selector must use health.facets.<facet>")
    try:
        return organism_topology.HealthFacet(field_path[len(prefix) :])
    except ValueError as exc:
        raise AtlasError(f"unsupported health facet selector: {field_path}") from exc


def _repo_pin(
    ref: AtlasAuthorityRef,
    authorities: AtlasAuthorities,
    *,
    field_path: str,
) -> SourcePin:
    authority_path = authorities.record_paths.get(ref)
    if authority_path is None:
        raise AtlasError(f"authority path is missing: {ref.kind.value}:{ref.id}")
    document_sha256 = authorities.source_sha256.get(authority_path)
    if document_sha256 is None:
        raise AtlasError(f"authority source digest is missing: {authority_path}")
    authority_digest = authorities.authority_digests.get(ref.kind)
    if authority_digest is None:
        raise AtlasError(f"authority family digest is missing: {ref.kind.value}")
    return SourcePin(
        source_class=AtlasSourceClass.REPO_DOCUMENT,
        canonical_ref=_canonical_ref(ref, field_path),
        authority_path=authority_path,
        document_sha256=document_sha256,
        authority_digest=authority_digest,
        field_path=field_path,
    )


def _evidence_semantic(record: organism_topology.EvidenceRef) -> Mapping[str, object]:
    return {
        "schema_version": record.schema_version,
        "evidence_id": record.evidence_id,
        "subject_ref": {
            "kind": record.subject_ref.kind.value,
            "id": record.subject_ref.id,
        },
        "facet": record.facet.value,
        "evidence_kind": record.evidence_kind.value,
        "canonical_ref": record.canonical_ref,
        "sha256": record.sha256,
        "observer_ref": record.observer_ref,
        "observed_at": record.observed_at,
        "fresh_until": record.fresh_until,
        "result": record.result.value,
    }


def _evidence_authority_digest(record: organism_topology.EvidenceRef) -> str:
    raw = json.dumps(
        _evidence_semantic(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(raw)


def _evidence_pin(
    evidence_id: str,
    record: organism_topology.EvidenceRef,
    facet: organism_topology.HealthFacet,
) -> SourcePin:
    return SourcePin(
        source_class=AtlasSourceClass.EVIDENCE_SNAPSHOT,
        canonical_ref=record.canonical_ref,
        authority_path=None,
        document_sha256=record.sha256,
        authority_digest=_evidence_authority_digest(record),
        field_path=f"evidence:{evidence_id}/{facet.value}",
    )


def _considered_evidence(
    binding: organism_topology.OrganBinding,
    organ: organism_topology.OrganContract,
    facet: organism_topology.HealthFacet,
    evidence: EvidenceSnapshot,
) -> tuple[tuple[str, organism_topology.EvidenceRef], ...]:
    selected = set(binding.evidence_refs)
    return tuple(
        (evidence_id, record)
        for evidence_id, record in sorted(evidence.records.items())
        if evidence_id in selected
        and record.subject_ref.id in {binding.binding_id, organ.organ_id}
        and record.facet is facet
    )


def _build_health_projection(
    selector: AtlasFieldSelector, authorities: AtlasAuthorities
) -> AtlasProjection:
    binding_ref = selector.authority_ref
    if binding_ref.kind is not AtlasAuthorityKind.BINDING:
        raise AtlasError("health projection selector authority must be binding")
    facet = _health_facet(selector.field_path)
    binding = authorities.records.get(binding_ref)
    if not isinstance(binding, organism_topology.OrganBinding):
        raise AtlasError(f"binding authority record is missing: {binding_ref.id}")
    organ_ref = AtlasAuthorityRef(AtlasAuthorityKind.ORGAN, binding.organ_id)
    organ = authorities.records.get(organ_ref)
    if not isinstance(organ, organism_topology.OrganContract):
        raise AtlasError(f"organ authority record is missing: {binding.organ_id}")
    try:
        health = organism_topology.evaluate_organ_health(
            organ,
            binding,
            authorities.evidence,
            now=authorities.now,
        )
    except Exception as exc:
        raise AtlasError(f"health evaluation failed: {exc}") from exc
    state = health.facets.get(facet, organism_topology.FacetState.UNKNOWN)
    evidence_pins = tuple(
        _evidence_pin(evidence_id, record, facet)
        for evidence_id, record in _considered_evidence(
            binding, organ, facet, authorities.evidence
        )
    )
    pins = (
        _repo_pin(binding_ref, authorities, field_path="evidence_refs"),
        _repo_pin(organ_ref, authorities, field_path="health.required_facets"),
        *evidence_pins,
    )
    return AtlasProjection(
        selector=selector,
        value=state.value,
        source_pins=pins,
        status=(
            AtlasProjectionStatus.UNKNOWN
            if state is organism_topology.FacetState.UNKNOWN
            else AtlasProjectionStatus.EVIDENCE_PROVEN
        ),
    )


def _build_projection(
    selector: AtlasFieldSelector, authorities: AtlasAuthorities
) -> AtlasProjection:
    if (
        selector.authority_ref.kind is AtlasAuthorityKind.BINDING
        and selector.field_path.startswith("health.facets.")
    ):
        return _build_health_projection(selector, authorities)
    return _build_ordinary_projection(selector, authorities)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _semantic_sha256(value: object) -> str:
    return _sha256(_json_bytes(value))


def _enum_values(enum_type: type[Enum]) -> list[str]:
    return sorted(item.value for item in enum_type)


def _schema_object(properties: Mapping[str, object], required: Sequence[str]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _schema_array(
    items: object,
    *,
    min_items: int | None = None,
    max_items: int | None = None,
    unique_items: bool | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"type": "array", "items": items}
    if min_items is not None:
        result["minItems"] = min_items
    if max_items is not None:
        result["maxItems"] = max_items
    if unique_items is not None:
        result["uniqueItems"] = unique_items
    return result


def _schema_defs() -> dict[str, object]:
    nonempty_string = {"type": "string", "minLength": 1}
    nullable_nonempty_string = {
        "anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]
    }
    stable_id = {"type": "string", "pattern": ATLAS_STABLE_ID_PATTERN}
    rank_order = {"type": "integer", "minimum": 0, "maximum": 5}
    sha256 = {"type": "string", "pattern": ATLAS_SHA256_PATTERN}
    path_ref = {"type": "string", "minLength": 1, "pattern": ATLAS_BUNDLE_PATH_PATTERN}
    identity_ref = _schema_object(
        {
            "kind": {"type": "string", "enum": _enum_values(AtlasIdentityKind)},
            "id": nonempty_string,
        },
        ("kind", "id"),
    )
    authority_ref = _schema_object(
        {
            "kind": {"type": "string", "enum": _enum_values(AtlasAuthorityKind)},
            "id": nonempty_string,
        },
        ("kind", "id"),
    )
    field_selector = _schema_object(
        {
            "authority_ref": {"$ref": "#/$defs/authorityRef"},
            "field_path": nonempty_string,
        },
        ("authority_ref", "field_path"),
    )
    source_pin = _schema_object(
        {
            "source_class": {"type": "string", "enum": _enum_values(AtlasSourceClass)},
            "canonical_ref": nonempty_string,
            "authority_path": {
                "anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]
            },
            "document_sha256": {"$ref": "#/$defs/sha256"},
            "authority_digest": {"$ref": "#/$defs/sha256"},
            "field_path": nonempty_string,
        },
        (
            "source_class",
            "canonical_ref",
            "authority_path",
            "document_sha256",
            "authority_digest",
            "field_path",
        ),
    )
    source_pin["allOf"] = [
        {
            "if": {"properties": {"source_class": {"const": AtlasSourceClass.REPO_DOCUMENT.value}}},
            "then": {"properties": {"authority_path": {"type": "string", "minLength": 1}}},
        },
        {
            "if": {
                "properties": {
                    "source_class": {"const": AtlasSourceClass.EVIDENCE_SNAPSHOT.value}
                }
            },
            "then": {"properties": {"authority_path": {"type": "null"}}},
        },
    ]
    return {
        "stableId": stable_id,
        "bundlePath": path_ref,
        "sha256": sha256,
        "nonemptyString": nonempty_string,
        "nullableNonemptyString": nullable_nonempty_string,
        "rankOrder": rank_order,
        "identityRef": identity_ref,
        "authorityRef": authority_ref,
        "fieldSelector": field_selector,
        "sourcePin": source_pin,
        "projectionValue": {
            "anyOf": [
                {"type": "null"},
                {"type": "boolean"},
                {"type": "integer"},
                {"type": "number"},
                {"type": "string"},
                {"type": "array", "items": {"$ref": "#/$defs/projectionValue"}},
            ]
        },
    }


def _node_request_schema() -> dict[str, object]:
    return _schema_object(
        {
            "node_id": {"$ref": "#/$defs/stableId"},
            "identity_ref": {"$ref": "#/$defs/identityRef"},
            "role": {"type": "string", "enum": _enum_values(AtlasContentRole)},
            "selectors": _schema_array(
                {"$ref": "#/$defs/fieldSelector"}, min_items=1, max_items=6
            ),
            "rank": {"$ref": "#/$defs/rankOrder"},
            "order": {"$ref": "#/$defs/rankOrder"},
            "necessary_for": {"$ref": "#/$defs/nullableNonemptyString"},
        },
        ("node_id", "identity_ref", "role", "selectors", "rank", "order", "necessary_for"),
    )


def _edge_request_schema() -> dict[str, object]:
    return _schema_object(
        {
            "edge_id": {"$ref": "#/$defs/stableId"},
            "source_node_id": {"$ref": "#/$defs/stableId"},
            "target_node_id": {"$ref": "#/$defs/stableId"},
            "relation": {"type": "string", "enum": _enum_values(AtlasRelationKind)},
            "role": {"type": "string", "enum": _enum_values(AtlasContentRole)},
            "source_selectors": _schema_array(
                {"$ref": "#/$defs/fieldSelector"}, max_items=6
            ),
            "necessary_for": {"$ref": "#/$defs/nullableNonemptyString"},
        },
        (
            "edge_id",
            "source_node_id",
            "target_node_id",
            "relation",
            "role",
            "source_selectors",
            "necessary_for",
        ),
    )


def _projection_schema() -> dict[str, object]:
    return _schema_object(
        {
            "selector": {"$ref": "#/$defs/fieldSelector"},
            "value": {"$ref": "#/$defs/projectionValue"},
            "source_pins": _schema_array({"$ref": "#/$defs/sourcePin"}, min_items=1),
            "status": {"type": "string", "enum": _enum_values(AtlasProjectionStatus)},
        },
        ("selector", "value", "source_pins", "status"),
    )


def _node_spec_schema() -> dict[str, object]:
    schema = _node_request_schema()
    properties = dict(schema["properties"])
    properties["projections"] = _schema_array({"$ref": "#/$defs/projection"}, min_items=1)
    return _schema_object(
        properties,
        (
            "node_id",
            "identity_ref",
            "role",
            "selectors",
            "rank",
            "order",
            "necessary_for",
            "projections",
        ),
    )


def _edge_spec_schema() -> dict[str, object]:
    schema = _edge_request_schema()
    properties = dict(schema["properties"])
    properties["projections"] = _schema_array({"$ref": "#/$defs/projection"})
    return _schema_object(
        properties,
        (
            "edge_id",
            "source_node_id",
            "target_node_id",
            "relation",
            "role",
            "source_selectors",
            "necessary_for",
            "projections",
        ),
    )


def _view_node_count_constraints() -> list[object]:
    return [
        {
            "properties": {
                "nodes": {
                    "contains": {
                        "type": "object",
                        "required": ["role"],
                        "properties": {"role": {"const": AtlasContentRole.PRIMARY.value}},
                    },
                    "minContains": 0,
                    "maxContains": 12,
                }
            }
        },
        {
            "properties": {
                "nodes": {
                    "contains": {
                        "type": "object",
                        "required": ["role"],
                        "properties": {"role": {"const": AtlasContentRole.SECONDARY.value}},
                    },
                    "minContains": 0,
                    "maxContains": 6,
                }
            }
        },
    ]


def _request_schema_map() -> dict[str, object]:
    defs = _schema_defs()
    defs["nodeRequest"] = _node_request_schema()
    defs["edgeRequest"] = _edge_request_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/architecture-atlas-request.schema.json",
        "title": "Architecture Atlas View Request",
        **_schema_object(
            {
                "schema_version": {"const": 1},
                "view_id": {"$ref": "#/$defs/stableId"},
                "view_kind": {"type": "string", "enum": _enum_values(AtlasViewKind)},
                "primary_question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                    "pattern": r"^[^?]*\?[^?]*$",
                },
                "exclusions": _schema_array(
                    {"$ref": "#/$defs/nonemptyString"},
                    min_items=1,
                    max_items=12,
                    unique_items=True,
                ),
                "subject_refs": _schema_array(
                    {"$ref": "#/$defs/identityRef"},
                    min_items=1,
                    max_items=12,
                    unique_items=True,
                ),
                "supporting_refs": _schema_array(
                    {"$ref": "#/$defs/authorityRef"},
                    max_items=24,
                    unique_items=True,
                ),
                "nodes": _schema_array({"$ref": "#/$defs/nodeRequest"}, max_items=18),
                "edges": _schema_array({"$ref": "#/$defs/edgeRequest"}, max_items=24),
            },
            (
                "schema_version",
                "view_id",
                "view_kind",
                "primary_question",
                "exclusions",
                "subject_refs",
                "supporting_refs",
                "nodes",
                "edges",
            ),
        ),
        "$defs": defs,
        "allOf": _view_node_count_constraints(),
    }


def _view_schema_map() -> dict[str, object]:
    defs = _schema_defs()
    defs["projection"] = _projection_schema()
    defs["nodeSpec"] = _node_spec_schema()
    defs["edgeSpec"] = _edge_spec_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/architecture-atlas-view.schema.json",
        "title": "Architecture Atlas View Spec",
        **_schema_object(
            {
                "schema_version": {"const": 1},
                "request_sha256": {"$ref": "#/$defs/sha256"},
                "view_id": {"$ref": "#/$defs/stableId"},
                "view_kind": {"type": "string", "enum": _enum_values(AtlasViewKind)},
                "primary_question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                    "pattern": r"^[^?]*\?[^?]*$",
                },
                "exclusions": _schema_array(
                    {"$ref": "#/$defs/nonemptyString"},
                    min_items=1,
                    max_items=12,
                    unique_items=True,
                ),
                "subject_refs": _schema_array(
                    {"$ref": "#/$defs/identityRef"},
                    min_items=1,
                    max_items=12,
                    unique_items=True,
                ),
                "supporting_refs": _schema_array(
                    {"$ref": "#/$defs/authorityRef"},
                    max_items=24,
                    unique_items=True,
                ),
                "nodes": _schema_array({"$ref": "#/$defs/nodeSpec"}, max_items=18),
                "edges": _schema_array({"$ref": "#/$defs/edgeSpec"}, max_items=24),
                "source_pins": _schema_array({"$ref": "#/$defs/sourcePin"}),
                "visible_text_sha256": {"$ref": "#/$defs/sha256"},
            },
            (
                "schema_version",
                "request_sha256",
                "view_id",
                "view_kind",
                "primary_question",
                "exclusions",
                "subject_refs",
                "supporting_refs",
                "nodes",
                "edges",
                "source_pins",
                "visible_text_sha256",
            ),
        ),
        "$defs": defs,
        "allOf": _view_node_count_constraints(),
    }


def _bundle_schema_map() -> dict[str, object]:
    bundle_entry = _schema_object(
        {
            "view_id": {"$ref": "#/$defs/stableId"},
            "request_path": {"$ref": "#/$defs/bundlePath"},
            "spec_path": {"$ref": "#/$defs/bundlePath"},
            "scene_path": {"$ref": "#/$defs/bundlePath"},
            "request_sha256": {"$ref": "#/$defs/sha256"},
            "spec_sha256": {"$ref": "#/$defs/sha256"},
            "scene_sha256": {"$ref": "#/$defs/sha256"},
        },
        (
            "view_id",
            "request_path",
            "spec_path",
            "scene_path",
            "request_sha256",
            "spec_sha256",
            "scene_sha256",
        ),
    )
    defs = _schema_defs()
    defs["bundleEntry"] = bundle_entry
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/architecture-atlas-bundle.schema.json",
        "title": "Architecture Atlas Bundle Manifest",
        **_schema_object(
            {
                "schema_version": {"const": 1},
                "bundle_id": {"$ref": "#/$defs/stableId"},
                "style_path": {"$ref": "#/$defs/bundlePath"},
                "style_sha256": {"$ref": "#/$defs/sha256"},
                "entries": _schema_array({"$ref": "#/$defs/bundleEntry"}, unique_items=True),
            },
            ("schema_version", "bundle_id", "style_path", "style_sha256", "entries"),
        ),
        "$defs": defs,
    }


def _serialize_identity_ref(ref: AtlasIdentityRef) -> Mapping[str, object]:
    return {"kind": ref.kind.value, "id": ref.id}


def _serialize_authority_ref(ref: AtlasAuthorityRef) -> Mapping[str, object]:
    return {"kind": ref.kind.value, "id": ref.id}


def _serialize_selector(selector: AtlasFieldSelector) -> Mapping[str, object]:
    return {
        "authority_ref": _serialize_authority_ref(selector.authority_ref),
        "field_path": selector.field_path,
    }


def _serialize_pin(pin: SourcePin) -> Mapping[str, object]:
    return {
        "source_class": pin.source_class.value,
        "canonical_ref": pin.canonical_ref,
        "authority_path": pin.authority_path,
        "document_sha256": pin.document_sha256,
        "authority_digest": pin.authority_digest,
        "field_path": pin.field_path,
    }


def _serialize_projection_value(value: JSONProjectionValue) -> object:
    if isinstance(value, tuple):
        return [_serialize_projection_value(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) in (int, float):
        return value
    raise AtlasError("projection value is not serializable")


def _serialize_projection(projection: AtlasProjection) -> Mapping[str, object]:
    if not projection.source_pins:
        raise AtlasError("projection must carry at least one source pin")
    return {
        "selector": _serialize_selector(projection.selector),
        "value": _serialize_projection_value(projection.value),
        "source_pins": [_serialize_pin(pin) for pin in projection.source_pins],
        "status": projection.status.value,
    }


def _serialize_node_request(node: AtlasNodeRequest) -> Mapping[str, object]:
    return {
        "node_id": node.node_id,
        "identity_ref": _serialize_identity_ref(node.identity_ref),
        "role": node.role.value,
        "selectors": [_serialize_selector(selector) for selector in node.selectors],
        "rank": node.rank,
        "order": node.order,
        "necessary_for": node.necessary_for,
    }


def _serialize_edge_request(edge: AtlasEdgeRequest) -> Mapping[str, object]:
    return {
        "edge_id": edge.edge_id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "relation": edge.relation.value,
        "role": edge.role.value,
        "source_selectors": [
            _serialize_selector(selector) for selector in edge.source_selectors
        ],
        "necessary_for": edge.necessary_for,
    }


def _serialize_request(request: AtlasViewRequest) -> Mapping[str, object]:
    return {
        "schema_version": request.schema_version,
        "view_id": request.view_id,
        "view_kind": request.view_kind.value,
        "primary_question": request.primary_question,
        "exclusions": list(request.exclusions),
        "subject_refs": [_serialize_identity_ref(ref) for ref in request.subject_refs],
        "supporting_refs": [
            _serialize_authority_ref(ref) for ref in request.supporting_refs
        ],
        "nodes": [_serialize_node_request(node) for node in request.nodes],
        "edges": [_serialize_edge_request(edge) for edge in request.edges],
    }


def _serialize_node(node: AtlasNode) -> Mapping[str, object]:
    return {
        "node_id": node.node_id,
        "identity_ref": _serialize_identity_ref(node.identity_ref),
        "role": node.role.value,
        "selectors": [_serialize_selector(selector) for selector in node.selectors],
        "rank": node.rank,
        "order": node.order,
        "necessary_for": node.necessary_for,
        "projections": [
            _serialize_projection(projection) for projection in node.projections
        ],
    }


def _serialize_edge(edge: AtlasEdge) -> Mapping[str, object]:
    return {
        "edge_id": edge.edge_id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "relation": edge.relation.value,
        "role": edge.role.value,
        "source_selectors": [
            _serialize_selector(selector) for selector in edge.source_selectors
        ],
        "necessary_for": edge.necessary_for,
        "projections": [
            _serialize_projection(projection) for projection in edge.projections
        ],
    }


def _serialize_spec(spec: AtlasViewSpec) -> Mapping[str, object]:
    return {
        "schema_version": spec.schema_version,
        "request_sha256": spec.request_sha256,
        "view_id": spec.view_id,
        "view_kind": spec.view_kind.value,
        "primary_question": spec.primary_question,
        "exclusions": list(spec.exclusions),
        "subject_refs": [_serialize_identity_ref(ref) for ref in spec.subject_refs],
        "supporting_refs": [
            _serialize_authority_ref(ref) for ref in spec.supporting_refs
        ],
        "nodes": [_serialize_node(node) for node in spec.nodes],
        "edges": [_serialize_edge(edge) for edge in spec.edges],
        "source_pins": [_serialize_pin(pin) for pin in spec.source_pins],
        "visible_text_sha256": spec.visible_text_sha256,
    }


def _serialize_bundle_entry(entry: AtlasBundleEntry) -> Mapping[str, object]:
    return {
        "view_id": entry.view_id,
        "request_path": entry.request_path,
        "spec_path": entry.spec_path,
        "scene_path": entry.scene_path,
        "request_sha256": entry.request_sha256,
        "spec_sha256": entry.spec_sha256,
        "scene_sha256": entry.scene_sha256,
    }


def _serialize_bundle_manifest(manifest: AtlasBundleManifest) -> Mapping[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "bundle_id": manifest.bundle_id,
        "style_path": manifest.style_path,
        "style_sha256": manifest.style_sha256,
        "entries": [_serialize_bundle_entry(entry) for entry in manifest.entries],
    }


def _sha256_value(value: object, *, path: str) -> str:
    parsed = _nonempty_str(value, path=path)
    if re.fullmatch(ATLAS_SHA256_PATTERN, parsed) is None:
        raise AtlasError(f"{path} must be a sha256:<64 lowercase hex> digest")
    return parsed


def _safe_bundle_file_name(value: object, *, path: str) -> str:
    parsed = _nonempty_str(value, path=path)
    if (
        "/" in parsed
        or "\\" in parsed
        or parsed in {".", ".."}
        or ATLAS_BUNDLE_FILE_RE.fullmatch(parsed) is None
    ):
        raise AtlasError(f"{path} must be a safe bundle file basename")
    return parsed


def _safe_payload_key(value: object, *, path: str) -> str:
    parsed = _safe_bundle_file_name(value, path=path)
    if parsed == ATLAS_BUNDLE_MANIFEST:
        return parsed
    if parsed.endswith((".request.json", ".view.json", ".excalidraw", ".json")):
        return parsed
    raise AtlasError(f"{path} has unsupported bundle file suffix")


def _parse_bundle_entry(value: object, *, path: str) -> AtlasBundleEntry:
    data = _exact_mapping(
        value,
        path=path,
        keys=(
            "view_id",
            "request_path",
            "spec_path",
            "scene_path",
            "request_sha256",
            "spec_sha256",
            "scene_sha256",
        ),
    )
    view_id = _stable_id(data["view_id"], path=f"{path}.view_id")
    return AtlasBundleEntry(
        view_id=view_id,
        request_path=_safe_bundle_file_name(data["request_path"], path=f"{path}.request_path"),
        spec_path=_safe_bundle_file_name(data["spec_path"], path=f"{path}.spec_path"),
        scene_path=_safe_bundle_file_name(data["scene_path"], path=f"{path}.scene_path"),
        request_sha256=_sha256_value(data["request_sha256"], path=f"{path}.request_sha256"),
        spec_sha256=_sha256_value(data["spec_sha256"], path=f"{path}.spec_sha256"),
        scene_sha256=_sha256_value(data["scene_sha256"], path=f"{path}.scene_sha256"),
    )


def _parse_bundle_manifest(value: object) -> AtlasBundleManifest:
    data = _exact_mapping(
        value,
        path="manifest",
        keys=("schema_version", "bundle_id", "style_path", "style_sha256", "entries"),
    )
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise AtlasError("manifest.schema_version must be 1")
    raw_entries = _list(data["entries"], path="manifest.entries")
    if not raw_entries:
        raise AtlasError("manifest.entries must be nonempty")
    entries = tuple(
        _parse_bundle_entry(item, path=f"manifest.entries[{index}]")
        for index, item in enumerate(raw_entries)
    )
    view_ids = [entry.view_id for entry in entries]
    if len(set(view_ids)) != len(view_ids):
        raise AtlasError("manifest view_id values must be unique")
    entry_paths = [
        path_name
        for entry in entries
        for path_name in (entry.request_path, entry.spec_path, entry.scene_path)
    ]
    if len(set(entry_paths)) != len(entry_paths):
        raise AtlasError("manifest entry paths must be unique")
    return AtlasBundleManifest(
        schema_version=1,
        bundle_id=_stable_id(data["bundle_id"], path="manifest.bundle_id"),
        style_path=_safe_bundle_file_name(data["style_path"], path="manifest.style_path"),
        style_sha256=_sha256_value(data["style_sha256"], path="manifest.style_sha256"),
        entries=tuple(sorted(entries, key=lambda entry: entry.view_id)),
    )


def _request_output_name(view_id: str) -> str:
    return f"{view_id}.request.json"


def _spec_output_name(view_id: str) -> str:
    return f"{view_id}.view.json"


def _scene_output_name(view_id: str) -> str:
    return f"{view_id}.excalidraw"


def _bundle_issue(code: str, detail: str, record_id: str = "*") -> AtlasIssue:
    return AtlasIssue(code=code, record_id=record_id, detail=detail)


def _bundle_file(root: Path, name: str) -> Path:
    safe_name = _safe_bundle_file_name(name, path="bundle path")
    return root / safe_name


def _read_bundle_file(root: Path, name: str, *, max_bytes: int) -> bytes:
    path = _bundle_file(root, name)
    return _load_regular_file_bytes(path, max_bytes=max_bytes)


def _load_bundle_manifest(bundle_root: Path) -> tuple[AtlasBundleManifest, bytes]:
    manifest_path = bundle_root / ATLAS_BUNDLE_MANIFEST
    raw = _load_regular_file_bytes(manifest_path, max_bytes=MAX_ATLAS_REQUEST_BYTES)
    parsed = _load_json_object(manifest_path, max_bytes=MAX_ATLAS_REQUEST_BYTES)
    return _parse_bundle_manifest(parsed), raw


def _require_bundle_inventory(bundle_root: Path, manifest: AtlasBundleManifest) -> None:
    expected = {ATLAS_BUNDLE_MANIFEST, manifest.style_path}
    for entry in manifest.entries:
        expected.update((entry.request_path, entry.spec_path, entry.scene_path))
    actual: set[str] = set()
    for child in bundle_root.iterdir():
        if child.is_symlink():
            raise AtlasError(f"bundle contains symlink: {child.name}")
        if not child.is_file():
            raise AtlasError(f"bundle contains non-file entry: {child.name}")
        _safe_bundle_file_name(child.name, path="bundle inventory")
        actual.add(child.name)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AtlasError(f"bundle inventory mismatch: missing={missing} extra={extra}")


def _pin_key(pin: SourcePin) -> tuple[str, str, str, str, str, str]:
    return (
        pin.source_class.value,
        pin.canonical_ref,
        pin.authority_path or "",
        pin.document_sha256,
        pin.authority_digest,
        pin.field_path,
    )


def _source_pin_union(
    nodes: Sequence[AtlasNode], edges: Sequence[AtlasEdge]
) -> tuple[SourcePin, ...]:
    pins: dict[tuple[str, str, str, str, str, str], SourcePin] = {}
    for projection in tuple(
        projection
        for node in nodes
        for projection in node.projections
    ) + tuple(projection for edge in edges for projection in edge.projections):
        if not projection.source_pins:
            raise AtlasError("projection must carry at least one source pin")
        for pin in projection.source_pins:
            pins[_pin_key(pin)] = pin
    return tuple(pins[key] for key in sorted(pins))


def _request_authority_refs(request: AtlasViewRequest) -> tuple[AtlasAuthorityRef, ...]:
    subject_authorities = tuple(
        AtlasAuthorityRef(
            AtlasAuthorityKind(ATLAS_IDENTITY_TO_AUTHORITY[ref.kind.value]), ref.id
        )
        for ref in request.subject_refs
    )
    return subject_authorities + request.supporting_refs


def _validate_direct_request(request: AtlasViewRequest, authorities: AtlasAuthorities) -> None:
    if not isinstance(request, AtlasViewRequest):
        raise AtlasError("request must be AtlasViewRequest")
    if request.schema_version != 1 or type(request.schema_version) is not int:
        raise AtlasError("request.schema_version must be 1")
    _stable_id(request.view_id, path="view_id")
    if not isinstance(request.view_kind, AtlasViewKind):
        raise AtlasError("request.view_kind must be AtlasViewKind")
    _question(request.primary_question)
    if not isinstance(request.exclusions, tuple):
        raise AtlasError("request.exclusions must be a tuple")
    if not 1 <= len(request.exclusions) <= 12:
        raise AtlasError("exclusions must contain 1..12 values")
    _unique_tuple(request.exclusions, path="exclusions")
    if not isinstance(request.subject_refs, tuple):
        raise AtlasError("request.subject_refs must be a tuple")
    if not 1 <= len(request.subject_refs) <= 12:
        raise AtlasError("subject_refs must contain 1..12 values")
    if not all(isinstance(ref, AtlasIdentityRef) for ref in request.subject_refs):
        raise AtlasError("subject_refs must contain AtlasIdentityRef values")
    if not isinstance(request.supporting_refs, tuple):
        raise AtlasError("request.supporting_refs must be a tuple")
    if len(request.supporting_refs) > 24:
        raise AtlasError("supporting_refs cannot contain more than 24 values")
    if not all(isinstance(ref, AtlasAuthorityRef) for ref in request.supporting_refs):
        raise AtlasError("supporting_refs must contain AtlasAuthorityRef values")
    if not isinstance(request.nodes, tuple):
        raise AtlasError("request.nodes must be a tuple")
    if not isinstance(request.edges, tuple):
        raise AtlasError("request.edges must be a tuple")
    for node in request.nodes:
        _validate_node_dataclass(node)
    for edge in request.edges:
        _validate_edge_dataclass(edge)
    _validate_request_graph(request)
    for ref in _request_authority_refs(request):
        if ref not in authorities.records:
            raise AtlasError(f"request authority does not exist: {ref.kind.value}:{ref.id}")


def _validate_node_dataclass(node: object) -> None:
    if not isinstance(node, AtlasNodeRequest):
        raise AtlasError("nodes must contain AtlasNodeRequest values")
    _stable_id(node.node_id, path="node_id")
    if not isinstance(node.identity_ref, AtlasIdentityRef):
        raise AtlasError("node.identity_ref must be AtlasIdentityRef")
    if not isinstance(node.role, AtlasContentRole):
        raise AtlasError("node.role must be AtlasContentRole")
    if not isinstance(node.selectors, tuple) or not node.selectors or len(node.selectors) > 6:
        raise AtlasError("node.selectors must be a nonempty tuple of at most 6")
    if not all(isinstance(selector, AtlasFieldSelector) for selector in node.selectors):
        raise AtlasError("node.selectors must contain AtlasFieldSelector values")
    _int_0_5(node.rank, path="node.rank")
    _int_0_5(node.order, path="node.order")
    if node.role is AtlasContentRole.PRIMARY and node.necessary_for is not None:
        raise AtlasError("primary node necessary_for must be None")
    if node.role is AtlasContentRole.SECONDARY and not node.necessary_for:
        raise AtlasError("secondary node necessary_for must be nonempty")


def _validate_edge_dataclass(edge: object) -> None:
    if not isinstance(edge, AtlasEdgeRequest):
        raise AtlasError("edges must contain AtlasEdgeRequest values")
    _stable_id(edge.edge_id, path="edge_id")
    _stable_id(edge.source_node_id, path="edge.source_node_id")
    _stable_id(edge.target_node_id, path="edge.target_node_id")
    if not isinstance(edge.relation, AtlasRelationKind):
        raise AtlasError("edge.relation must be AtlasRelationKind")
    if not isinstance(edge.role, AtlasContentRole):
        raise AtlasError("edge.role must be AtlasContentRole")
    if not isinstance(edge.source_selectors, tuple) or len(edge.source_selectors) > 6:
        raise AtlasError("edge.source_selectors must be a tuple of at most 6")
    if not all(isinstance(selector, AtlasFieldSelector) for selector in edge.source_selectors):
        raise AtlasError("edge.source_selectors must contain AtlasFieldSelector values")
    if edge.role is AtlasContentRole.PRIMARY and edge.necessary_for is not None:
        raise AtlasError("primary edge necessary_for must be None")
    if edge.role is AtlasContentRole.SECONDARY and not edge.necessary_for:
        raise AtlasError("secondary edge necessary_for must be nonempty")


def _visible_value_text(value: JSONProjectionValue) -> str:
    if isinstance(value, tuple):
        return "[" + ",".join(_visible_value_text(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, (str, bool)):
        return str(value)
    if type(value) in (int, float):
        return str(value)
    raise AtlasError("projection value is hidden or nested")


def _visible_transcript(spec: AtlasViewSpec) -> str:
    lines: list[str] = [
        f"question:{spec.primary_question}",
        "exclusions:" + "|".join(spec.exclusions),
    ]
    for ref in spec.subject_refs:
        lines.append(f"subject:{ref.kind.value}:{ref.id}")
    for ref in spec.supporting_refs:
        lines.append(f"supporting:{ref.kind.value}:{ref.id}")
    for node in spec.nodes:
        lines.append(
            f"node:{node.rank}:{node.order}:{node.node_id}:{node.identity_ref.kind.value}:{node.identity_ref.id}:{node.role.value}:{node.necessary_for or ''}"
        )
        for projection in node.projections:
            lines.append(
                f"projection:{projection.selector.authority_ref.kind.value}:{projection.selector.authority_ref.id}:{projection.selector.field_path}:{_visible_value_text(projection.value)}"
            )
    for edge in spec.edges:
        lines.append(
            f"edge:{edge.edge_id}:{edge.source_node_id}:{edge.target_node_id}:{edge.relation.value}:{edge.role.value}:{edge.necessary_for or ''}"
        )
        for projection in edge.projections:
            lines.append(
                f"edge-projection:{projection.selector.authority_ref.kind.value}:{projection.selector.authority_ref.id}:{projection.selector.field_path}:{_visible_value_text(projection.value)}"
            )
    transcript = "\n".join(lines)
    if len(transcript) > 4000:
        raise AtlasError("visible text exceeds 4000 Unicode characters")
    return transcript


def _visible_text_sha256(spec: AtlasViewSpec) -> str:
    return _sha256((_visible_transcript(spec) + "\n").encode("utf-8"))


def _spec_field_differences(actual: AtlasViewSpec, expected: AtlasViewSpec) -> tuple[AtlasIssue, ...]:
    issues: list[AtlasIssue] = []
    actual_map = _serialize_spec(actual)
    expected_map = _serialize_spec(expected)
    for key in sorted(expected_map):
        if actual_map.get(key) != expected_map[key]:
            issues.append(
                AtlasIssue(
                    code="view-mismatch",
                    record_id=actual.view_id,
                    detail=f"{key} differs",
                )
            )
    for key in sorted(set(actual_map) - set(expected_map)):
        issues.append(
            AtlasIssue(
                code="view-mismatch",
                record_id=actual.view_id,
                detail=f"unexpected field {key}",
            )
        )
    return tuple(issues)


def _stable_int(*parts: object) -> int:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") & 0x7FFFFFFF


def _stable_element_id(view_id: str, role: str, ref: str) -> str:
    raw = f"atlas-element-v1|{view_id}|{role}|{ref}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _element_base(spec: AtlasViewSpec, role: str, ref: str) -> dict[str, object]:
    seed = _stable_int("seed", spec.view_id, role, ref)
    nonce = _stable_int("nonce", spec.view_id, role, ref)
    return {
        "id": _stable_element_id(spec.view_id, role, ref),
        "version": 1,
        "versionNonce": nonce,
        "seed": seed,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "boundElements": None,
        "isDeleted": False,
    }


def _node_text(node: AtlasNode) -> str:
    lines = [
        node.node_id,
        f"{node.identity_ref.kind.value}:{node.identity_ref.id}",
    ]
    if node.necessary_for:
        lines.append(node.necessary_for)
    for projection in node.projections:
        lines.append(
            f"{projection.selector.field_path}: {_visible_value_text(projection.value)}"
        )
    return "\n".join(lines)


def _edge_text(edge: AtlasEdge) -> str:
    parts = [edge.relation.value]
    if edge.necessary_for:
        parts.append(edge.necessary_for)
    for projection in edge.projections:
        parts.append(
            f"{projection.selector.field_path}: {_visible_value_text(projection.value)}"
        )
    return "\n".join(parts)


def _fits_text(text: str, *, width: float, height: float, font_size: int) -> bool:
    if not text:
        return True
    line_height = font_size * 1.25
    lines = text.splitlines() or [text]
    if len(lines) * line_height > height:
        return False
    average_char_width = font_size * 0.62
    return all(len(line) * average_char_width <= width for line in lines)


def _node_position(node: AtlasNode, style: AtlasStyle) -> tuple[float, float]:
    grid_width = (6 * style.node_width) + (5 * style.rank_gap)
    grid_height = (6 * style.node_height) + (5 * style.order_gap)
    origin_x = (style.canvas_width - grid_width) / 2
    origin_y = (style.canvas_height - grid_height) / 2
    return (
        origin_x + node.rank * (style.node_width + style.rank_gap),
        origin_y + node.order * (style.node_height + style.order_gap),
    )


def _rectangles_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    return lx < rx + rw and lx + lw > rx and ly < ry + rh and ly + lh > ry


def _require_scene_safe(spec: AtlasViewSpec, style: AtlasStyle) -> None:
    if _source_pin_union(spec.nodes, spec.edges) != spec.source_pins:
        raise AtlasError("spec source pin union does not match projections")
    if _visible_text_sha256(spec) != spec.visible_text_sha256:
        raise AtlasError("spec visible text hash does not match content")
    rectangles: list[tuple[str, tuple[float, float, float, float]]] = []
    for node in spec.nodes:
        x, y = _node_position(node, style)
        rect = (x, y, float(style.node_width), float(style.node_height))
        if x < 0 or y < 0 or x + style.node_width > style.canvas_width or y + style.node_height > style.canvas_height:
            raise AtlasError(f"node is outside canvas: {node.node_id}")
        for other_id, other in rectangles:
            if _rectangles_overlap(rect, other):
                raise AtlasError(f"node collision: {other_id}/{node.node_id}")
        rectangles.append((node.node_id, rect))
        if not _fits_text(
            _node_text(node),
            width=style.node_width - 24,
            height=style.node_height - 24,
            font_size=style.font_size,
        ):
            raise AtlasError(f"node text does not fit: {node.node_id}")
    node_ids = {node.node_id for node in spec.nodes}
    for edge in spec.edges:
        if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
            raise AtlasError(f"edge references missing node: {edge.edge_id}")
        if not _fits_text(
            _edge_text(edge),
            width=320,
            height=80,
            font_size=style.font_size,
        ):
            raise AtlasError(f"edge label text does not fit: {edge.edge_id}")


def _rectangle_element(
    spec: AtlasViewSpec,
    node: AtlasNode,
    style: AtlasStyle,
    *,
    text_id: str,
    arrow_ids: Sequence[str],
) -> Mapping[str, object]:
    x, y = _node_position(node, style)
    element = _element_base(spec, "node-rect", node.node_id)
    element.update(
        {
            "type": "rectangle",
            "x": x,
            "y": y,
            "width": style.node_width,
            "height": style.node_height,
            "angle": 0,
            "strokeColor": style.stroke_color,
            "backgroundColor": (
                style.primary_fill_color
                if node.role is AtlasContentRole.PRIMARY
                else style.secondary_fill_color
            ),
            "fillStyle": "solid",
            "strokeWidth": style.stroke_width,
            "strokeStyle": "solid",
            "roughness": 1,
            "opacity": 100,
            "boundElements": [{"type": "text", "id": text_id}]
            + [{"type": "arrow", "id": arrow_id} for arrow_id in sorted(arrow_ids)],
        }
    )
    return element


def _text_element(
    spec: AtlasViewSpec,
    *,
    role: str,
    ref: str,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    style: AtlasStyle,
    container_id: str,
) -> Mapping[str, object]:
    element = _element_base(spec, role, ref)
    element.update(
        {
            "type": "text",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "angle": 0,
            "strokeColor": style.text_color,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 1,
            "strokeStyle": "solid",
            "roughness": 0,
            "opacity": 100,
            "fontSize": style.font_size,
            "fontFamily": style.font_family,
            "text": text,
            "originalText": text,
            "textAlign": "center",
            "verticalAlign": "middle",
            "containerId": container_id,
            "lineHeight": 1.25,
        }
    )
    return element


def _arrow_element(
    spec: AtlasViewSpec,
    edge: AtlasEdge,
    style: AtlasStyle,
    *,
    source_rect: Mapping[str, object],
    target_rect: Mapping[str, object],
    label_id: str,
) -> Mapping[str, object]:
    sx = float(source_rect["x"]) + float(source_rect["width"]) / 2
    sy = float(source_rect["y"]) + float(source_rect["height"]) / 2
    tx = float(target_rect["x"]) + float(target_rect["width"]) / 2
    ty = float(target_rect["y"]) + float(target_rect["height"]) / 2
    element = _element_base(spec, "edge-arrow", edge.edge_id)
    element.update(
        {
            "type": "arrow",
            "x": sx,
            "y": sy,
            "width": tx - sx,
            "height": ty - sy,
            "angle": 0,
            "strokeColor": style.stroke_color,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": style.stroke_width,
            "strokeStyle": "solid",
            "roughness": 1,
            "opacity": 100,
            "points": [[0, 0], [tx - sx, ty - sy]],
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "startBinding": {
                "elementId": source_rect["id"],
                "focus": 0,
                "gap": 8,
            },
            "endBinding": {
                "elementId": target_rect["id"],
                "focus": 0,
                "gap": 8,
            },
            "boundElements": [{"type": "text", "id": label_id}],
        }
    )
    return element


def _check_reciprocal_bindings(elements: Sequence[Mapping[str, object]]) -> None:
    by_id = {element["id"]: element for element in elements}
    for element in elements:
        element_type = element["type"]
        if element_type == "text":
            container_id = element.get("containerId")
            if container_id not in by_id:
                raise AtlasError("text container binding is missing")
            container = by_id[container_id]
            bound = container.get("boundElements") or []
            if not any(item.get("id") == element["id"] and item.get("type") == "text" for item in bound):
                raise AtlasError("text binding is not reciprocal")
        if element_type == "arrow":
            for binding_name in ("startBinding", "endBinding"):
                binding = element.get(binding_name)
                if not isinstance(binding, dict) or binding.get("elementId") not in by_id:
                    raise AtlasError("arrow endpoint binding is missing")
                target = by_id[binding["elementId"]]
                bound = target.get("boundElements") or []
                if not any(item.get("id") == element["id"] and item.get("type") == "arrow" for item in bound):
                    raise AtlasError("arrow binding is not reciprocal")


def load_atlas_authorities(
    repo_root: Path,
    goal_keys: AbstractSet[str],
    *,
    evidence: EvidenceSnapshot | None = None,
    now: datetime | None = None,
) -> AtlasAuthorities:
    loaded_now = _utc_now(now)
    loaded_evidence = evidence or EvidenceSnapshot.empty()

    project_issues = project_catalogue.check_project_catalogue(repo_root, goal_keys)
    component_issues = component_catalogue.check_component_catalogue(repo_root)
    topology_issues = organism_topology.check_organism_topology(
        repo_root, evidence=loaded_evidence, now=loaded_now
    )
    program_findings = grand_project_program.lint_repository_indexes(repo_root)
    issues = (
        tuple(project_issues)
        + tuple(component_issues)
        + tuple(topology_issues)
        + tuple(program_findings)
    )
    if issues:
        raise AtlasError(
            "authority validation failed: "
            + "; ".join(_issue_detail(issue) for issue in issues)
        )

    try:
        project_records = project_catalogue.load_project_records(
            repo_root / "projects" / "records"
        )
        component_records = component_catalogue.load_component_records(
            repo_root / "components" / "records"
        )
        topology = organism_topology.load_organism_topology(repo_root)
        organism_topology.build_reference_indexes(
            repo_root,
            canonical_ref_sha256=loaded_evidence.canonical_ref_sha256,
        )
        stages = grand_project_program.load_stage_index(
            repo_root / "tickets" / "grand-project-stage-evidence.yaml"
        )
        obligations = grand_project_program.load_obligation_index(
            repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
        )
    except Exception as exc:
        raise AtlasError(f"authority load failed: {exc}") from exc

    records: dict[AtlasAuthorityRef, object] = {}
    record_paths: dict[AtlasAuthorityRef, str] = {}
    source_bytes: dict[str, bytes] = {}
    source_sha256: dict[str, str] = {}
    generated_index_bytes: dict[str, bytes] = {}
    generated_index_sha256: dict[str, str] = {}

    for rel_path in ("projects/index.json", "components/index.json", "organism/index.json"):
        raw = _load_regular_file_bytes(
            repo_root / rel_path, max_bytes=MAX_ATLAS_AUTHORITY_BYTES
        )
        generated_index_bytes[rel_path] = raw
        generated_index_sha256[rel_path] = _sha256(raw)

    stage_index_path = "tickets/grand-project-stage-evidence.yaml"
    obligation_index_path = "tickets/grand-project-reevaluation-obligations.yaml"
    _read_source(repo_root, stage_index_path, source_bytes, source_sha256)
    _read_source(repo_root, obligation_index_path, source_bytes, source_sha256)

    for project_id, record in sorted(project_records.items()):
        ref = _ref(AtlasAuthorityKind.PROJECT, project_id)
        rel_path = f"projects/records/{project_id}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for component_id, record in sorted(component_records.items()):
        ref = _ref(AtlasAuthorityKind.COMPONENT, component_id)
        rel_path = f"components/records/{component_id}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for organ_id, record in sorted(topology.organs.items()):
        ref = _ref(AtlasAuthorityKind.ORGAN, organ_id)
        rel_path = f"organism/organs/{organ_id.split(':', 1)[1]}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for record in sorted(topology.bindings, key=lambda item: item.binding_id):
        ref = _ref(AtlasAuthorityKind.BINDING, record.binding_id)
        rel_path = f"organism/bindings/{record.binding_id.split(':', 1)[1]}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for record in sorted(topology.dependencies, key=lambda item: item.dependency_id):
        ref = _ref(AtlasAuthorityKind.DEPENDENCY, record.dependency_id)
        rel_path = f"organism/dependencies/{record.dependency_id.split(':', 1)[1]}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for record in sorted(topology.surfaces, key=lambda item: item.surface_id):
        ref = _ref(AtlasAuthorityKind.SURFACE, record.surface_id)
        rel_path = f"organism/surfaces/{record.surface_id.split(':', 1)[1]}.yaml"
        records[ref] = record
        record_paths[ref] = rel_path
        _read_source(repo_root, rel_path, source_bytes, source_sha256)

    for record in sorted(stages, key=lambda item: item.stage_id):
        ref = _ref(AtlasAuthorityKind.STAGE, record.stage_id)
        records[ref] = record
        record_paths[ref] = stage_index_path

    for record in sorted(obligations, key=lambda item: item.obligation_id):
        ref = _ref(AtlasAuthorityKind.OBLIGATION, record.obligation_id)
        records[ref] = record
        record_paths[ref] = obligation_index_path

    authority_digests = {
        AtlasAuthorityKind.PROJECT: generated_index_sha256["projects/index.json"],
        AtlasAuthorityKind.COMPONENT: generated_index_sha256["components/index.json"],
        AtlasAuthorityKind.ORGAN: generated_index_sha256["organism/index.json"],
        AtlasAuthorityKind.BINDING: generated_index_sha256["organism/index.json"],
        AtlasAuthorityKind.DEPENDENCY: generated_index_sha256["organism/index.json"],
        AtlasAuthorityKind.SURFACE: generated_index_sha256["organism/index.json"],
        AtlasAuthorityKind.STAGE: source_sha256[stage_index_path],
        AtlasAuthorityKind.OBLIGATION: source_sha256[obligation_index_path],
    }

    return AtlasAuthorities(
        records=records,
        record_paths=record_paths,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        generated_index_bytes=generated_index_bytes,
        generated_index_sha256=generated_index_sha256,
        authority_digests=authority_digests,
        evidence=loaded_evidence,
        now=loaded_now,
    )


def load_atlas_view_request(path: Path) -> AtlasViewRequest:
    data = _load_json_object(path, max_bytes=MAX_ATLAS_REQUEST_BYTES)
    parsed = _exact_mapping(
        data,
        path="request",
        keys=(
            "schema_version",
            "view_id",
            "view_kind",
            "primary_question",
            "exclusions",
            "subject_refs",
            "supporting_refs",
            "nodes",
            "edges",
        ),
    )
    if parsed["schema_version"] != 1 or type(parsed["schema_version"]) is not int:
        raise AtlasError("request.schema_version must be 1")
    view_kind = _enum_value(AtlasViewKind, parsed["view_kind"], path="view_kind")
    assert isinstance(view_kind, AtlasViewKind)
    exclusions = _unique_tuple(
        _list(parsed["exclusions"], path="exclusions"), path="exclusions"
    )
    if not 1 <= len(exclusions) <= 12:
        raise AtlasError("exclusions must contain 1..12 values")
    raw_subjects = _list(parsed["subject_refs"], path="subject_refs")
    if not 1 <= len(raw_subjects) <= 12:
        raise AtlasError("subject_refs must contain 1..12 values")
    raw_supporting = _list(parsed["supporting_refs"], path="supporting_refs")
    if len(raw_supporting) > 24:
        raise AtlasError("supporting_refs cannot contain more than 24 values")
    raw_nodes = _list(parsed["nodes"], path="nodes")
    raw_edges = _list(parsed["edges"], path="edges")
    request = AtlasViewRequest(
        schema_version=parsed["schema_version"],
        view_id=_stable_id(parsed["view_id"], path="view_id"),
        view_kind=view_kind,
        primary_question=_question(parsed["primary_question"]),
        exclusions=exclusions,
        subject_refs=tuple(
            _parse_identity_ref(item, path=f"subject_refs[{index}]")
            for index, item in enumerate(raw_subjects)
        ),
        supporting_refs=tuple(
            _parse_authority_ref(item, path=f"supporting_refs[{index}]")
            for index, item in enumerate(raw_supporting)
        ),
        nodes=tuple(
            _parse_node_request(item, path=f"nodes[{index}]")
            for index, item in enumerate(raw_nodes)
        ),
        edges=tuple(
            _parse_edge_request(item, path=f"edges[{index}]")
            for index, item in enumerate(raw_edges)
        ),
    )
    _validate_request_graph(request)
    return request


def load_atlas_style(path: Path) -> AtlasStyle:
    data = _load_json_object(path, max_bytes=MAX_ATLAS_STYLE_BYTES)
    parsed = _exact_mapping(
        data,
        path="style",
        keys=(
            "schema_version",
            "canvas_width",
            "canvas_height",
            "background_color",
            "primary_fill_color",
            "secondary_fill_color",
            "stroke_color",
            "text_color",
            "font_family",
            "font_size",
            "stroke_width",
            "rank_gap",
            "order_gap",
            "node_width",
            "node_height",
        ),
    )
    if parsed["schema_version"] != 1 or type(parsed["schema_version"]) is not int:
        raise AtlasError("style.schema_version must be 1")
    canvas_width = _bounded_int(
        parsed["canvas_width"], path="style.canvas_width", minimum=3600, maximum=3600
    )
    canvas_height = _bounded_int(
        parsed["canvas_height"], path="style.canvas_height", minimum=2400, maximum=2400
    )
    style = AtlasStyle(
        schema_version=1,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        background_color=_color(
            parsed["background_color"], path="style.background_color"
        ),
        primary_fill_color=_color(
            parsed["primary_fill_color"], path="style.primary_fill_color"
        ),
        secondary_fill_color=_color(
            parsed["secondary_fill_color"], path="style.secondary_fill_color"
        ),
        stroke_color=_color(parsed["stroke_color"], path="style.stroke_color"),
        text_color=_color(parsed["text_color"], path="style.text_color"),
        font_family=_font_family(parsed["font_family"], path="style.font_family"),
        font_size=_bounded_int(
            parsed["font_size"], path="style.font_size", minimum=8, maximum=64
        ),
        stroke_width=_bounded_int(
            parsed["stroke_width"], path="style.stroke_width", minimum=1, maximum=8
        ),
        rank_gap=_bounded_int(
            parsed["rank_gap"], path="style.rank_gap", minimum=20, maximum=1000
        ),
        order_gap=_bounded_int(
            parsed["order_gap"], path="style.order_gap", minimum=20, maximum=1000
        ),
        node_width=_bounded_int(
            parsed["node_width"], path="style.node_width", minimum=120, maximum=1200
        ),
        node_height=_bounded_int(
            parsed["node_height"], path="style.node_height", minimum=60, maximum=800
        ),
    )
    grid_width = (6 * style.node_width) + (5 * style.rank_gap)
    grid_height = (6 * style.node_height) + (5 * style.order_gap)
    if grid_width > style.canvas_width:
        raise AtlasError("style rank grid exceeds canvas width")
    if grid_height > style.canvas_height:
        raise AtlasError("style order grid exceeds canvas height")
    return style


def derive_atlas_view(
    request: AtlasViewRequest, authorities: AtlasAuthorities
) -> AtlasViewSpec:
    _validate_direct_request(request, authorities)
    request_sha256 = _semantic_sha256(_serialize_request(request))
    nodes = tuple(
        AtlasNode(
            node_id=node.node_id,
            identity_ref=node.identity_ref,
            role=node.role,
            selectors=node.selectors,
            rank=node.rank,
            order=node.order,
            necessary_for=node.necessary_for,
            projections=tuple(
                _build_projection(selector, authorities) for selector in node.selectors
            ),
        )
        for node in sorted(request.nodes, key=lambda item: (item.rank, item.order, item.node_id))
    )
    edges = tuple(
        AtlasEdge(
            edge_id=edge.edge_id,
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            relation=edge.relation,
            role=edge.role,
            source_selectors=edge.source_selectors,
            necessary_for=edge.necessary_for,
            projections=tuple(
                _build_projection(selector, authorities)
                for selector in edge.source_selectors
            ),
        )
        for edge in sorted(request.edges, key=lambda item: item.edge_id)
    )
    source_pins = _source_pin_union(nodes, edges)
    spec = AtlasViewSpec(
        schema_version=1,
        request_sha256=request_sha256,
        view_id=request.view_id,
        view_kind=request.view_kind,
        primary_question=request.primary_question,
        exclusions=request.exclusions,
        subject_refs=request.subject_refs,
        supporting_refs=request.supporting_refs,
        nodes=nodes,
        edges=edges,
        source_pins=source_pins,
        visible_text_sha256="",
    )
    return AtlasViewSpec(
        schema_version=spec.schema_version,
        request_sha256=spec.request_sha256,
        view_id=spec.view_id,
        view_kind=spec.view_kind,
        primary_question=spec.primary_question,
        exclusions=spec.exclusions,
        subject_refs=spec.subject_refs,
        supporting_refs=spec.supporting_refs,
        nodes=spec.nodes,
        edges=spec.edges,
        source_pins=spec.source_pins,
        visible_text_sha256=_visible_text_sha256(spec),
    )


def verify_atlas_view(
    spec: AtlasViewSpec, request: AtlasViewRequest, authorities: AtlasAuthorities
) -> tuple[AtlasIssue, ...]:
    try:
        expected = derive_atlas_view(request, authorities)
    except AtlasError as exc:
        record_id = getattr(request, "view_id", "*")
        if not isinstance(record_id, str):
            record_id = "*"
        return (AtlasIssue("view-derivation-failed", record_id, str(exc)),)
    try:
        return _spec_field_differences(spec, expected)
    except AtlasError as exc:
        return (AtlasIssue("view-mismatch", spec.view_id, str(exc)),)


def render_excalidraw(spec: AtlasViewSpec, style: AtlasStyle) -> bytes:
    _require_scene_safe(spec, style)
    node_text_ids = {
        node.node_id: _stable_element_id(spec.view_id, "node-text", node.node_id)
        for node in spec.nodes
    }
    edge_arrow_ids = {
        edge.edge_id: _stable_element_id(spec.view_id, "edge-arrow", edge.edge_id)
        for edge in spec.edges
    }
    edge_label_ids = {
        edge.edge_id: _stable_element_id(spec.view_id, "edge-label", edge.edge_id)
        for edge in spec.edges
    }
    arrows_by_node: dict[str, list[str]] = {node.node_id: [] for node in spec.nodes}
    for edge in spec.edges:
        arrows_by_node[edge.source_node_id].append(edge_arrow_ids[edge.edge_id])
        arrows_by_node[edge.target_node_id].append(edge_arrow_ids[edge.edge_id])

    rectangles: dict[str, Mapping[str, object]] = {}
    elements: list[Mapping[str, object]] = []
    for node in spec.nodes:
        rect = _rectangle_element(
            spec,
            node,
            style,
            text_id=node_text_ids[node.node_id],
            arrow_ids=arrows_by_node[node.node_id],
        )
        rectangles[node.node_id] = rect
        elements.append(rect)
        x = float(rect["x"]) + 12
        y = float(rect["y"]) + 12
        elements.append(
            _text_element(
                spec,
                role="node-text",
                ref=node.node_id,
                text=_node_text(node),
                x=x,
                y=y,
                width=style.node_width - 24,
                height=style.node_height - 24,
                style=style,
                container_id=str(rect["id"]),
            )
        )

    for edge in spec.edges:
        arrow = _arrow_element(
            spec,
            edge,
            style,
            source_rect=rectangles[edge.source_node_id],
            target_rect=rectangles[edge.target_node_id],
            label_id=edge_label_ids[edge.edge_id],
        )
        elements.append(arrow)
        label_text = _edge_text(edge)
        elements.append(
            _text_element(
                spec,
                role="edge-label",
                ref=edge.edge_id,
                text=label_text,
                x=float(arrow["x"]) + float(arrow["width"]) / 2 - 160,
                y=float(arrow["y"]) + float(arrow["height"]) / 2 - 40,
                width=320,
                height=80,
                style=style,
                container_id=str(arrow["id"]),
            )
        )

    elements = sorted(elements, key=lambda element: str(element["id"]))
    _check_reciprocal_bindings(elements)
    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {
            "viewBackgroundColor": style.background_color,
            "gridSize": None,
        },
        "files": {},
    }
    return _json_bytes(scene)


def generate_atlas_request_schema() -> bytes:
    return _json_bytes(_request_schema_map())


def generate_atlas_view_schema() -> bytes:
    return _json_bytes(_view_schema_map())


def generate_atlas_bundle_schema() -> bytes:
    return _json_bytes(_bundle_schema_map())


def _schema_payloads() -> dict[str, bytes]:
    return {
        "schemas/architecture-atlas-request.schema.json": generate_atlas_request_schema(),
        "schemas/architecture-atlas-view.schema.json": generate_atlas_view_schema(),
        "schemas/architecture-atlas-bundle.schema.json": generate_atlas_bundle_schema(),
    }


def _write_file_replace_atomic(path: Path, raw: bytes) -> None:
    parent = path.parent
    parent_stat = parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise AtlasError(f"schema parent must be a non-symlink directory: {parent}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(parent)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_schema_files_atomic(repo_root: Path) -> tuple[str, ...]:
    payloads = _schema_payloads()
    schema_root = repo_root / "schemas"
    schema_root.mkdir(exist_ok=True)
    written: list[str] = []
    for rel_path, raw in sorted(payloads.items()):
        path = repo_root / rel_path
        _write_file_replace_atomic(path, raw)
        written.append(rel_path)
    return tuple(written)


def _issue_map(issue: AtlasIssue) -> Mapping[str, object]:
    return {
        "code": issue.code,
        "record_id": issue.record_id,
        "detail": issue.detail,
    }


def _cli_bytes(value: object) -> bytes:
    return _json_bytes(value)


def _write_stdout_json(value: object) -> None:
    raw = _cli_bytes(value)
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
    else:
        sys.stdout.write(raw.decode("utf-8"))


def build_atlas_bundle(
    bundle_id: str,
    request_paths: Sequence[Path],
    style_path: Path,
    authorities: AtlasAuthorities,
) -> Mapping[str, bytes]:
    parsed_bundle_id = _stable_id(bundle_id, path="bundle_id")
    if not request_paths:
        raise AtlasError("request_paths must be nonempty")
    style_name = _safe_bundle_file_name(Path(style_path).name, path="style_path")
    style = load_atlas_style(Path(style_path))
    style_bytes = _load_regular_file_bytes(Path(style_path), max_bytes=MAX_ATLAS_STYLE_BYTES)

    payloads: dict[str, bytes] = {}
    entries: list[AtlasBundleEntry] = []
    seen_view_ids: set[str] = set()

    def add_payload(name: str, raw: bytes) -> None:
        safe_name = _safe_payload_key(name, path="bundle payload path")
        if safe_name in payloads:
            raise AtlasError(f"duplicate bundle payload path: {safe_name}")
        payloads[safe_name] = raw

    add_payload(style_name, style_bytes)

    for request_path in request_paths:
        request = load_atlas_view_request(Path(request_path))
        if request.view_id in seen_view_ids:
            raise AtlasError(f"duplicate request view_id: {request.view_id}")
        seen_view_ids.add(request.view_id)
        request_name = _request_output_name(request.view_id)
        if Path(request_path).name != request_name:
            raise AtlasError(
                f"request basename must be {request_name}: {Path(request_path).name}"
            )
        request_bytes = _load_regular_file_bytes(
            Path(request_path), max_bytes=MAX_ATLAS_REQUEST_BYTES
        )
        spec = derive_atlas_view(request, authorities)
        spec_bytes = _json_bytes(_serialize_spec(spec))
        scene_bytes = render_excalidraw(spec, style)
        spec_name = _spec_output_name(request.view_id)
        scene_name = _scene_output_name(request.view_id)
        add_payload(request_name, request_bytes)
        add_payload(spec_name, spec_bytes)
        add_payload(scene_name, scene_bytes)
        entries.append(
            AtlasBundleEntry(
                view_id=request.view_id,
                request_path=request_name,
                spec_path=spec_name,
                scene_path=scene_name,
                request_sha256=_sha256(request_bytes),
                spec_sha256=_sha256(spec_bytes),
                scene_sha256=_sha256(scene_bytes),
            )
        )

    manifest = AtlasBundleManifest(
        schema_version=1,
        bundle_id=parsed_bundle_id,
        style_path=style_name,
        style_sha256=_sha256(style_bytes),
        entries=tuple(sorted(entries, key=lambda entry: entry.view_id)),
    )
    add_payload(ATLAS_BUNDLE_MANIFEST, _json_bytes(_serialize_bundle_manifest(manifest)))
    return dict(sorted(payloads.items()))


def verify_atlas_bundle(
    bundle_root: Path,
    repo_root: Path,
    goal_keys: AbstractSet[str],
    *,
    evidence: EvidenceSnapshot | None = None,
    now: datetime | None = None,
) -> tuple[AtlasIssue, ...]:
    issues: list[AtlasIssue] = []
    try:
        root_stat = bundle_root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise AtlasError("bundle_root must be a non-symlink directory")
        manifest, manifest_bytes = _load_bundle_manifest(bundle_root)
        _require_bundle_inventory(bundle_root, manifest)
    except Exception as exc:
        return (_bundle_issue("bundle-invalid", str(exc)),)

    try:
        canonical_manifest = _json_bytes(_serialize_bundle_manifest(manifest))
        if manifest_bytes != canonical_manifest:
            issues.append(
                _bundle_issue(
                    "bundle-manifest-mismatch",
                    "fixture-bundle.json is not canonical manifest bytes",
                    manifest.bundle_id,
                )
            )
    except AtlasError as exc:
        issues.append(_bundle_issue("bundle-manifest-mismatch", str(exc), manifest.bundle_id))

    try:
        style_bytes = _read_bundle_file(
            bundle_root, manifest.style_path, max_bytes=MAX_ATLAS_STYLE_BYTES
        )
        if _sha256(style_bytes) != manifest.style_sha256:
            issues.append(
                _bundle_issue("bundle-hash-mismatch", "style_sha256 differs", manifest.bundle_id)
            )
        style = load_atlas_style(bundle_root / manifest.style_path)
    except Exception as exc:
        return tuple(
            issues
            + [_bundle_issue("bundle-invalid", f"style load failed: {exc}", manifest.bundle_id)]
        )

    try:
        authorities = load_atlas_authorities(
            repo_root, goal_keys, evidence=evidence, now=now
        )
    except Exception as exc:
        return tuple(
            issues
            + [_bundle_issue("bundle-authorities-invalid", str(exc), manifest.bundle_id)]
        )

    for entry in manifest.entries:
        try:
            if entry.request_path != _request_output_name(entry.view_id):
                raise AtlasError("request path does not match view_id")
            if entry.spec_path != _spec_output_name(entry.view_id):
                raise AtlasError("spec path does not match view_id")
            if entry.scene_path != _scene_output_name(entry.view_id):
                raise AtlasError("scene path does not match view_id")
            request_bytes = _read_bundle_file(
                bundle_root, entry.request_path, max_bytes=MAX_ATLAS_REQUEST_BYTES
            )
            spec_bytes = _read_bundle_file(
                bundle_root, entry.spec_path, max_bytes=MAX_ATLAS_REQUEST_BYTES
            )
            scene_bytes = _read_bundle_file(
                bundle_root, entry.scene_path, max_bytes=MAX_ATLAS_REQUEST_BYTES
            )
            if _sha256(request_bytes) != entry.request_sha256:
                issues.append(
                    _bundle_issue("bundle-hash-mismatch", "request_sha256 differs", entry.view_id)
                )
            if _sha256(spec_bytes) != entry.spec_sha256:
                issues.append(
                    _bundle_issue("bundle-hash-mismatch", "spec_sha256 differs", entry.view_id)
                )
            if _sha256(scene_bytes) != entry.scene_sha256:
                issues.append(
                    _bundle_issue("bundle-hash-mismatch", "scene_sha256 differs", entry.view_id)
                )
            request = load_atlas_view_request(bundle_root / entry.request_path)
            if request.view_id != entry.view_id:
                raise AtlasError("request view_id does not match manifest entry")
            spec = derive_atlas_view(request, authorities)
            expected_spec_bytes = _json_bytes(_serialize_spec(spec))
            expected_scene_bytes = render_excalidraw(spec, style)
            if spec_bytes != expected_spec_bytes:
                issues.append(
                    _bundle_issue("bundle-spec-mismatch", "view spec bytes differ", entry.view_id)
                )
            if scene_bytes != expected_scene_bytes:
                issues.append(
                    _bundle_issue("bundle-scene-mismatch", "scene bytes differ", entry.view_id)
                )
            expected_entry = AtlasBundleEntry(
                view_id=entry.view_id,
                request_path=entry.request_path,
                spec_path=entry.spec_path,
                scene_path=entry.scene_path,
                request_sha256=_sha256(request_bytes),
                spec_sha256=_sha256(expected_spec_bytes),
                scene_sha256=_sha256(expected_scene_bytes),
            )
            if _serialize_bundle_entry(entry) != _serialize_bundle_entry(expected_entry):
                issues.append(
                    _bundle_issue("bundle-manifest-mismatch", "entry hashes differ", entry.view_id)
                )
        except Exception as exc:
            issues.append(_bundle_issue("bundle-entry-invalid", str(exc), entry.view_id))
    return tuple(issues)


def write_bundle_atomic(output_root: Path, payloads: Mapping[str, bytes]) -> None:
    if not payloads:
        raise AtlasError("bundle payloads must be nonempty")
    output_root = Path(output_root)
    parent = output_root.parent if output_root.parent != Path("") else Path(".")
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise AtlasError(f"bundle output parent is unavailable: {parent}: {exc}") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise AtlasError("bundle output parent must be a non-symlink directory")
    if output_root.exists() or output_root.is_symlink():
        raise AtlasError(f"bundle output target already exists: {output_root}")

    checked_payloads: dict[str, bytes] = {}
    for key, raw in payloads.items():
        safe_key = _safe_payload_key(key, path="bundle payload path")
        if safe_key in checked_payloads:
            raise AtlasError(f"duplicate bundle payload path: {safe_key}")
        if not isinstance(raw, bytes):
            raise AtlasError(f"bundle payload must be bytes: {safe_key}")
        checked_payloads[safe_key] = raw
    if ATLAS_BUNDLE_MANIFEST not in checked_payloads:
        raise AtlasError("bundle payloads must include fixture-bundle.json")

    temp_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.tmp-", dir=str(parent))
    )
    try:
        for name, raw in sorted(checked_payloads.items()):
            path = temp_root / name
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(path, flags, 0o600)
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(temp_root)
        os.rename(temp_root, output_root)
        _fsync_dir(parent)
    except Exception as exc:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        raise AtlasError(f"atomic bundle write failed: {exc}") from exc


def check_atlas_tooling(
    repo_root: Path,
    goal_keys: AbstractSet[str],
    *,
    evidence: EvidenceSnapshot | None = None,
    now: datetime | None = None,
) -> tuple[AtlasIssue, ...]:
    issues: list[AtlasIssue] = []
    for rel_path, expected in sorted(_schema_payloads().items()):
        path = repo_root / rel_path
        try:
            actual = _load_regular_file_bytes(path, max_bytes=MAX_ATLAS_REQUEST_BYTES)
        except Exception as exc:
            issues.append(
                AtlasIssue("atlas-schema-missing", rel_path, f"schema read failed: {exc}")
            )
            continue
        if actual != expected:
            issues.append(
                AtlasIssue("atlas-schema-mismatch", rel_path, "schema bytes differ")
            )

    fixture_root = repo_root / "tools" / "fixtures" / "architecture_atlas"
    try:
        root_stat = fixture_root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise AtlasError("fixture bundle must be a non-symlink directory")
    except Exception as exc:
        issues.append(
            AtlasIssue(
                "atlas-fixture-missing",
                "tools/fixtures/architecture_atlas",
                f"fixture bundle unavailable: {exc}",
            )
        )
        return tuple(sorted(issues, key=lambda issue: (issue.code, issue.record_id, issue.detail)))

    issues.extend(
        verify_atlas_bundle(
            fixture_root,
            repo_root,
            goal_keys,
            evidence=evidence,
            now=now,
        )
    )
    return tuple(sorted(issues, key=lambda issue: (issue.code, issue.record_id, issue.detail)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="architecture_atlas")
    subparsers = parser.add_subparsers(dest="verb", required=True)

    generate = subparsers.add_parser("generate-schemas")
    generate.add_argument("--repo-root", required=True, type=Path)

    derive = subparsers.add_parser("derive-bundle")
    derive.add_argument("--repo-root", required=True, type=Path)
    derive.add_argument("--bundle-id", required=True)
    derive.add_argument("--request", required=True, action="append", type=Path)
    derive.add_argument("--style", required=True, type=Path)
    derive.add_argument("--output", required=True, type=Path)
    derive.add_argument("--goal-key", action="append", default=[])

    verify = subparsers.add_parser("verify-bundle")
    verify.add_argument("--repo-root", required=True, type=Path)
    verify.add_argument("--bundle-root", required=True, type=Path)
    verify.add_argument("--goal-key", action="append", default=[])

    check = subparsers.add_parser("check-tooling")
    check.add_argument("--repo-root", required=True, type=Path)
    check.add_argument("--goal-key", action="append", default=[])

    try:
        args = parser.parse_args(argv)
        if args.verb == "generate-schemas":
            written = _write_schema_files_atomic(args.repo_root)
            _write_stdout_json({"issues": [], "paths": list(written)})
            return 0
        if args.verb == "derive-bundle":
            authorities = load_atlas_authorities(
                args.repo_root,
                frozenset(args.goal_key),
                evidence=EvidenceSnapshot.empty(),
            )
            payloads = build_atlas_bundle(
                args.bundle_id, tuple(args.request), args.style, authorities
            )
            write_bundle_atomic(args.output, payloads)
            _write_stdout_json({"issues": [], "output": str(args.output)})
            return 0
        if args.verb == "verify-bundle":
            issues = verify_atlas_bundle(
                args.bundle_root,
                args.repo_root,
                frozenset(args.goal_key),
                evidence=EvidenceSnapshot.empty(),
            )
            _write_stdout_json({"issues": [_issue_map(issue) for issue in issues]})
            return 1 if issues else 0
        if args.verb == "check-tooling":
            issues = check_atlas_tooling(
                args.repo_root,
                frozenset(args.goal_key),
                evidence=EvidenceSnapshot.empty(),
            )
            _write_stdout_json({"issues": [_issue_map(issue) for issue in issues]})
            return 1 if issues else 0
        raise AtlasError(f"unsupported verb: {args.verb}")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            _write_stdout_json(
                {
                    "issues": [
                        _issue_map(AtlasIssue("atlas-cli-usage", "*", "invalid arguments"))
                    ]
                }
            )
        return code
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        _write_stdout_json(
            {
                "issues": [
                    _issue_map(AtlasIssue("atlas-cli-error", "*", str(exc)))
                ]
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
