#!/usr/bin/env python3
"""Strict ORG-056 organism topology contracts, validation, and generation.

This module owns pure catalogue mechanics only.  It does not poll runtime state,
choose action authority, deploy, recover, or project capability-path health.
Evidence remains caller-supplied through :class:`EvidenceSnapshot`.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Any, AbstractSet, Mapping, Sequence

import yaml

from tools import component_catalogue, grand_project_program, project_catalogue


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 1_048_576
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAMESPACED_ID_RE = re.compile(r"([a-z][a-z0-9-]*):([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\Z")
STABLE_REF_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*:[^\s]+\Z")

CANONICAL_ORGAN_IDS = (
    "organ:operator-relationship",
    "organ:bridge",
    "organ:pantheon-executive",
    "organ:exocortex",
    "organ:echo-prismatic",
    "organ:intelligence-research",
    "organ:recovery-homeostasis",
    "organ:harness-adapters",
    "organ:capability-navigator",
    "organ:federation-passport",
    "organ:senses-actuators",
    "organ:presence-surfaces",
)


class OrganismTopologyError(ValueError):
    pass


class IdentityKind(str, Enum):
    ORGANISM = "organism"
    SEED = "seed"
    PROJECT = "project"
    ORGAN = "organ"
    PERSONA = "persona"
    ROLE = "role"
    COMPONENT = "component"
    SERVICE = "service"
    DEVICE = "device"
    SURFACE = "surface"


class EvidenceSubjectKind(str, Enum):
    ORGAN = "organ"
    BINDING = "binding"
    DEPENDENCY = "dependency"
    SURFACE = "surface"


class BindingLifecycle(str, Enum):
    PLANNED = "planned"
    PARTIAL = "partial"
    LIVE = "live"
    RETIRING = "retiring"
    RETIRED = "retired"


class DependencyRelationship(str, Enum):
    HARD = "hard"
    OPTIONAL = "optional"
    FALLBACK = "fallback"
    INFORMATIONAL = "informational"
    FEEDBACK = "feedback"


class SelectionRule(str, Enum):
    SOLE = "sole"
    PRIORITY = "priority"
    EXPLICIT = "explicit"


class HealthFacet(str, Enum):
    REACHABILITY = "reachability"
    LIVENESS = "liveness"
    READINESS = "readiness"
    CORRECTNESS = "correctness"
    FRESHNESS = "freshness"
    AUTHORITY = "authority"
    CONTINUITY = "continuity"


class EvidenceKind(str, Enum):
    TEST = "test"
    PROBE = "probe"
    RECEIPT = "receipt"
    REVIEW = "review"
    OBSERVATION = "observation"
    DECLARATION = "declaration"


class EvidenceResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    AMBIGUOUS = "ambiguous"


class FacetState(str, Enum):
    HEALTHY = "healthy"
    FAILED = "failed"
    STALE = "stale"
    INCONSISTENT = "inconsistent"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class TypedIdentityRef:
    kind: IdentityKind
    id: str


@dataclasses.dataclass(frozen=True)
class EvidenceSubjectRef:
    kind: EvidenceSubjectKind
    id: str


@dataclasses.dataclass(frozen=True)
class PortContract:
    port_id: str
    contract_ref: str
    schema_ref: str
    version_range: str
    authority_ref: str
    confidentiality: str
    freshness_bound: str | None


@dataclasses.dataclass(frozen=True)
class OrganPorts:
    inputs: tuple[PortContract, ...]
    outputs: tuple[PortContract, ...]


@dataclasses.dataclass(frozen=True)
class OrganBoundaries:
    identity: tuple[str, ...]
    data: tuple[str, ...]
    trust: tuple[str, ...]
    authority: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganExpectations:
    latency: tuple[str, ...]
    availability: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganHealthContract:
    required_facets: tuple[HealthFacet, ...]
    evidence_requirements: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganDegradation:
    unavailable: tuple[str, ...]
    stale: tuple[str, ...]
    partitioned: tuple[str, ...]
    inconsistent: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganRecovery:
    owner_ref: str
    escalation_ref: str
    verification_requirements: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganReplacement:
    continuity_requirements: tuple[str, ...]
    provenance_requirements: tuple[str, ...]
    rollback_requirements: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganismRecord:
    schema_version: int
    organism_id: str
    organ_ids: tuple[str, ...]
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class OrganContract:
    schema_version: int
    organ_id: str
    responsibility: str
    must_not_become: tuple[str, ...]
    ports: OrganPorts
    authoritative_inputs: tuple[str, ...]
    authoritative_outputs: tuple[str, ...]
    boundaries: OrganBoundaries
    expectations: OrganExpectations
    health: OrganHealthContract
    failure_modes: tuple[str, ...]
    degradation: OrganDegradation
    recovery: OrganRecovery
    replacement: OrganReplacement
    owners: tuple[str, ...]
    project_refs: tuple[str, ...]
    component_refs: tuple[str, ...]
    obligation_refs: tuple[str, ...]
    re_evaluate_when: tuple[str, ...]
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class OrganBinding:
    schema_version: int
    binding_id: str
    organ_id: str
    implementation_ref: TypedIdentityRef
    seed_ref: str
    locus: str
    ports: tuple[str, ...]
    lifecycle: BindingLifecycle
    authority_owner_ref: str
    evidence_refs: tuple[str, ...]
    verified_at: str | None
    fresh_until: str | None
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class DependencyConsumer:
    organ_id: str
    port_id: str


@dataclasses.dataclass(frozen=True)
class DependencyProvider:
    organ_id: str
    port_id: str
    capability_ref: str


@dataclasses.dataclass(frozen=True)
class ProviderSelection:
    authority_ref: str | None
    rule: SelectionRule
    priority: int | None
    evidence_refs: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class FeedbackContract:
    bootstrap: tuple[str, ...]
    bound: str | None
    break_condition: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class OrganDependency:
    schema_version: int
    dependency_id: str
    consumer: DependencyConsumer
    provider: DependencyProvider
    relationship: DependencyRelationship
    contract_ref: str
    schema_ref: str
    version_range: str
    freshness_bound: str
    failure_propagation: tuple[str, ...]
    degraded_behavior: tuple[str, ...]
    owner_ref: str
    evidence_refs: tuple[str, ...]
    selection: ProviderSelection
    feedback: FeedbackContract
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class EvidenceRef:
    schema_version: int
    evidence_id: str
    subject_ref: EvidenceSubjectRef
    facet: HealthFacet
    evidence_kind: EvidenceKind
    canonical_ref: str
    sha256: str
    observer_ref: str
    observed_at: str
    fresh_until: str | None
    result: EvidenceResult


@dataclasses.dataclass(frozen=True)
class SurfacePort:
    organ_id: str
    port_id: str


@dataclasses.dataclass(frozen=True)
class SurfaceBoundaries:
    identity: tuple[str, ...]
    consent: tuple[str, ...]
    trust: tuple[str, ...]
    authority: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ExternalSurface:
    schema_version: int
    surface_id: str
    attached_port: SurfacePort
    transport: str
    audience: tuple[str, ...]
    boundaries: SurfaceBoundaries
    owner_ref: str
    degradation: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class OrganismTopology:
    organism: OrganismRecord
    organs: Mapping[str, OrganContract]
    bindings: tuple[OrganBinding, ...]
    dependencies: tuple[OrganDependency, ...]
    surfaces: tuple[ExternalSurface, ...]


@dataclasses.dataclass(frozen=True)
class ReferenceIndexes:
    project_ids: frozenset[str]
    component_ids: frozenset[str]
    capability_ids: frozenset[str]
    obligation_ids: frozenset[str]
    canonical_ref_sha256: Mapping[str, str]

    @classmethod
    def empty(
        cls,
        *,
        project_ids: AbstractSet[str] = frozenset(),
        component_ids: AbstractSet[str] = frozenset(),
        capability_ids: AbstractSet[str] = frozenset(),
        obligation_ids: AbstractSet[str] = frozenset(),
        canonical_ref_sha256: Mapping[str, str] | None = None,
    ) -> "ReferenceIndexes":
        return cls(
            frozenset(project_ids),
            frozenset(component_ids),
            frozenset(capability_ids),
            frozenset(obligation_ids),
            dict(canonical_ref_sha256 or {}),
        )


@dataclasses.dataclass(frozen=True)
class EvidenceSnapshot:
    records: Mapping[str, EvidenceRef]
    canonical_ref_sha256: Mapping[str, str]

    @classmethod
    def empty(cls) -> "EvidenceSnapshot":
        return cls({}, {})


@dataclasses.dataclass(frozen=True)
class OrganHealth:
    organ_id: str
    binding_id: str
    facets: Mapping[HealthFacet, FacetState]


@dataclasses.dataclass(frozen=True, order=True)
class TopologyIssue:
    code: str
    record_id: str
    detail: str


class _StrictLoader(yaml.SafeLoader):
    pass


_StrictLoader.yaml_implicit_resolvers = {
    key: [item for item in value if item[0] != "tag:yaml.org,2002:timestamp"]
    for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, object]:
    if not isinstance(node, yaml.MappingNode):
        raise OrganismTopologyError("expected mapping node")
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise OrganismTopologyError("mapping keys must be strings")
        if key == "<<":
            raise OrganismTopologyError("YAML merge keys are not allowed")
        if key in result:
            raise OrganismTopologyError(f"duplicate mapping key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _semantic_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_yaml(path: Path) -> tuple[object, bytes]:
    if path.is_symlink() or not path.is_file():
        raise OrganismTopologyError(f"record must be a regular non-symlink file: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_RECORD_BYTES:
        raise OrganismTopologyError(f"record has invalid size: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OrganismTopologyError(f"record is not UTF-8: {path}") from exc
    try:
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                raise OrganismTopologyError(f"YAML aliases, anchors, and tags are forbidden: {path}")
        value = yaml.load(text, Loader=_StrictLoader)
    except OrganismTopologyError:
        raise
    except yaml.YAMLError as exc:
        raise OrganismTopologyError(f"invalid YAML: {path}: {type(exc).__name__}") from exc
    return value, raw


def _exact(value: object, *, path: str, keys: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise OrganismTopologyError(f"{path} must be a mapping")
    unknown = sorted(set(value) - set(keys))
    missing = sorted(set(keys) - set(value))
    if unknown or missing:
        raise OrganismTopologyError(f"{path} keys mismatch: unknown={unknown} missing={missing}")
    return value


def _string(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OrganismTopologyError(f"{path} must be a nonempty trimmed string")
    return value


def _nullable_string(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path=path)


def _strings(value: object, *, path: str, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OrganismTopologyError(f"{path} must be a list")
    parsed = tuple(_string(item, path=f"{path}[{index}]") for index, item in enumerate(value))
    if unique and len(set(parsed)) != len(parsed):
        raise OrganismTopologyError(f"{path} must contain unique items")
    return parsed


def _enum(enum_type: type[Enum], value: object, *, path: str) -> Any:
    parsed = _string(value, path=path)
    try:
        return enum_type(parsed)
    except ValueError as exc:
        raise OrganismTopologyError(f"{path} has unsupported value {parsed!r}") from exc


def _namespaced(value: object, expected: str, *, path: str) -> str:
    parsed = _string(value, path=path)
    match = NAMESPACED_ID_RE.fullmatch(parsed)
    if match is None or match.group(1) != expected:
        raise OrganismTopologyError(f"{path} must use {expected}: namespace")
    return parsed


def _stable_ref(value: object, *, path: str) -> str:
    parsed = _string(value, path=path)
    if STABLE_REF_RE.fullmatch(parsed) is None:
        raise OrganismTopologyError(f"{path} must be a stable namespaced ref")
    return parsed


def _timestamp(value: object, *, path: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    parsed = _string(value, path=path)
    try:
        instant = dt.datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrganismTopologyError(f"{path} must be RFC3339") from exc
    if instant.tzinfo is None:
        raise OrganismTopologyError(f"{path} must include a timezone")
    return parsed


def _instant(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _source_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_identity_ref(ref: TypedIdentityRef) -> None:
    _namespaced(ref.id, ref.kind.value, path="identity.id")


def _typed_ref(value: object, *, path: str) -> TypedIdentityRef:
    record = _exact(value, path=path, keys=("kind", "id"))
    kind = _enum(IdentityKind, record["kind"], path=f"{path}.kind")
    ref = TypedIdentityRef(kind=kind, id=_string(record["id"], path=f"{path}.id"))
    validate_identity_ref(ref)
    return ref


def _port(value: object, *, path: str) -> PortContract:
    record = _exact(
        value,
        path=path,
        keys=(
            "port_id",
            "contract_ref",
            "schema_ref",
            "version_range",
            "authority_ref",
            "confidentiality",
            "freshness_bound",
        ),
    )
    return PortContract(
        port_id=_namespaced(record["port_id"], "port", path=f"{path}.port_id"),
        contract_ref=_stable_ref(record["contract_ref"], path=f"{path}.contract_ref"),
        schema_ref=_stable_ref(record["schema_ref"], path=f"{path}.schema_ref"),
        version_range=_string(record["version_range"], path=f"{path}.version_range"),
        authority_ref=_namespaced(record["authority_ref"], "role", path=f"{path}.authority_ref"),
        confidentiality=_string(record["confidentiality"], path=f"{path}.confidentiality"),
        freshness_bound=_nullable_string(record["freshness_bound"], path=f"{path}.freshness_bound"),
    )


def _ports(value: object) -> OrganPorts:
    record = _exact(value, path="ports", keys=("inputs", "outputs"))
    if not isinstance(record["inputs"], list) or not isinstance(record["outputs"], list):
        raise OrganismTopologyError("ports inputs/outputs must be lists")
    inputs = tuple(_port(item, path=f"ports.inputs[{i}]") for i, item in enumerate(record["inputs"]))
    outputs = tuple(_port(item, path=f"ports.outputs[{i}]") for i, item in enumerate(record["outputs"]))
    identifiers = [item.port_id for item in inputs + outputs]
    if len(identifiers) != len(set(identifiers)):
        raise OrganismTopologyError("port_id values must be unique across one organ contract")
    return OrganPorts(inputs=inputs, outputs=outputs)


def _four_string_groups(value: object, *, path: str, keys: Sequence[str]) -> dict[str, tuple[str, ...]]:
    record = _exact(value, path=path, keys=keys)
    return {key: _strings(record[key], path=f"{path}.{key}") for key in keys}


_CONTRACT_KEYS = (
    "schema_version",
    "organ_id",
    "responsibility",
    "must_not_become",
    "ports",
    "authoritative_inputs",
    "authoritative_outputs",
    "boundaries",
    "expectations",
    "health",
    "failure_modes",
    "degradation",
    "recovery",
    "replacement",
    "owners",
    "project_refs",
    "component_refs",
    "obligation_refs",
    "re_evaluate_when",
)


def parse_organ_contract(value: object, *, source: str) -> OrganContract:
    record = _exact(value, path=source, keys=_CONTRACT_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"{source}.schema_version must equal {SCHEMA_VERSION}")
    ports = _ports(record["ports"])
    boundaries = _four_string_groups(
        record["boundaries"], path="boundaries", keys=("identity", "data", "trust", "authority")
    )
    expectations = _four_string_groups(
        record["expectations"], path="expectations", keys=("latency", "availability")
    )
    health_record = _exact(
        record["health"], path="health", keys=("required_facets", "evidence_requirements")
    )
    required_raw = health_record["required_facets"]
    if not isinstance(required_raw, list):
        raise OrganismTopologyError("health.required_facets must be a list")
    required_facets = tuple(
        _enum(HealthFacet, item, path=f"health.required_facets[{i}]")
        for i, item in enumerate(required_raw)
    )
    if len(required_facets) != len(set(required_facets)):
        raise OrganismTopologyError("health.required_facets must be unique")
    degradation = _four_string_groups(
        record["degradation"],
        path="degradation",
        keys=("unavailable", "stale", "partitioned", "inconsistent", "unknown"),
    )
    recovery_record = _exact(
        record["recovery"],
        path="recovery",
        keys=("owner_ref", "escalation_ref", "verification_requirements"),
    )
    replacement_record = _exact(
        record["replacement"],
        path="replacement",
        keys=("continuity_requirements", "provenance_requirements", "rollback_requirements"),
    )
    parsed = OrganContract(
        schema_version=SCHEMA_VERSION,
        organ_id=_namespaced(record["organ_id"], "organ", path=f"{source}.organ_id"),
        responsibility=_string(record["responsibility"], path=f"{source}.responsibility"),
        must_not_become=_strings(record["must_not_become"], path=f"{source}.must_not_become"),
        ports=ports,
        authoritative_inputs=_strings(record["authoritative_inputs"], path="authoritative_inputs"),
        authoritative_outputs=_strings(record["authoritative_outputs"], path="authoritative_outputs"),
        boundaries=OrganBoundaries(**boundaries),
        expectations=OrganExpectations(**expectations),
        health=OrganHealthContract(
            required_facets=required_facets,
            evidence_requirements=_strings(
                health_record["evidence_requirements"], path="health.evidence_requirements"
            ),
        ),
        failure_modes=_strings(record["failure_modes"], path="failure_modes"),
        degradation=OrganDegradation(**degradation),
        recovery=OrganRecovery(
            owner_ref=_namespaced(recovery_record["owner_ref"], "role", path="recovery.owner_ref"),
            escalation_ref=_namespaced(
                recovery_record["escalation_ref"], "role", path="recovery.escalation_ref"
            ),
            verification_requirements=_strings(
                recovery_record["verification_requirements"], path="recovery.verification_requirements"
            ),
        ),
        replacement=OrganReplacement(
            continuity_requirements=_strings(
                replacement_record["continuity_requirements"], path="replacement.continuity_requirements"
            ),
            provenance_requirements=_strings(
                replacement_record["provenance_requirements"], path="replacement.provenance_requirements"
            ),
            rollback_requirements=_strings(
                replacement_record["rollback_requirements"], path="replacement.rollback_requirements"
            ),
        ),
        owners=tuple(_namespaced(item, "role", path="owners[]") for item in _strings(record["owners"], path="owners")),
        project_refs=tuple(
            _namespaced(item, "project", path="project_refs[]")
            for item in _strings(record["project_refs"], path="project_refs")
        ),
        component_refs=tuple(
            _namespaced(item, "component", path="component_refs[]")
            for item in _strings(record["component_refs"], path="component_refs")
        ),
        obligation_refs=_strings(record["obligation_refs"], path="obligation_refs"),
        re_evaluate_when=_strings(record["re_evaluate_when"], path="re_evaluate_when"),
        source_sha256=_semantic_sha(value),
    )
    input_ids = {item.port_id for item in ports.inputs}
    output_ids = {item.port_id for item in ports.outputs}
    if not set(parsed.authoritative_inputs) <= input_ids:
        raise OrganismTopologyError("authoritative_inputs must name declared input ports")
    if not set(parsed.authoritative_outputs) <= output_ids:
        raise OrganismTopologyError("authoritative_outputs must name declared output ports")
    return parsed


_BINDING_KEYS = (
    "schema_version", "binding_id", "organ_id", "implementation_ref", "seed_ref",
    "locus", "ports", "lifecycle", "authority_owner_ref", "evidence_refs",
    "verified_at", "fresh_until",
)


def parse_organ_binding(value: object, *, source: str) -> OrganBinding:
    record = _exact(value, path=source, keys=_BINDING_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"{source}.schema_version must equal {SCHEMA_VERSION}")
    implementation = _typed_ref(record["implementation_ref"], path="implementation_ref")
    if implementation.kind not in {
        IdentityKind.PERSONA, IdentityKind.ROLE, IdentityKind.COMPONENT,
        IdentityKind.SERVICE, IdentityKind.DEVICE,
    }:
        raise OrganismTopologyError("implementation_ref kind cannot be organism, seed, project, organ, or surface")
    return OrganBinding(
        schema_version=SCHEMA_VERSION,
        binding_id=_namespaced(record["binding_id"], "binding", path="binding_id"),
        organ_id=_namespaced(record["organ_id"], "organ", path="organ_id"),
        implementation_ref=implementation,
        seed_ref=_namespaced(record["seed_ref"], "seed", path="seed_ref"),
        locus=_string(record["locus"], path="locus"),
        ports=tuple(_namespaced(item, "port", path="ports[]") for item in _strings(record["ports"], path="ports")),
        lifecycle=_enum(BindingLifecycle, record["lifecycle"], path="lifecycle"),
        authority_owner_ref=_namespaced(
            record["authority_owner_ref"], "role", path="authority_owner_ref"
        ),
        evidence_refs=tuple(
            _namespaced(item, "evidence", path="evidence_refs[]")
            for item in _strings(record["evidence_refs"], path="evidence_refs")
        ),
        verified_at=_timestamp(record["verified_at"], path="verified_at", nullable=True),
        fresh_until=_timestamp(record["fresh_until"], path="fresh_until", nullable=True),
        source_sha256=_semantic_sha(value),
    )


_DEPENDENCY_KEYS = (
    "schema_version", "dependency_id", "consumer", "provider", "relationship",
    "contract_ref", "schema_ref", "version_range", "freshness_bound",
    "failure_propagation", "degraded_behavior", "owner_ref", "evidence_refs",
    "selection", "feedback",
)


def parse_organ_dependency(value: object, *, source: str) -> OrganDependency:
    record = _exact(value, path=source, keys=_DEPENDENCY_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"{source}.schema_version must equal {SCHEMA_VERSION}")
    consumer = _exact(record["consumer"], path="consumer", keys=("organ_id", "port_id"))
    provider = _exact(
        record["provider"], path="provider", keys=("organ_id", "port_id", "capability_ref")
    )
    selection = _exact(
        record["selection"], path="selection",
        keys=("authority_ref", "rule", "priority", "evidence_refs"),
    )
    feedback = _exact(
        record["feedback"], path="feedback", keys=("bootstrap", "bound", "break_condition")
    )
    authority = selection["authority_ref"]
    parsed_authority = None if authority is None else _namespaced(authority, "role", path="selection.authority_ref")
    priority = selection["priority"]
    if priority is not None and type(priority) is not int:
        raise OrganismTopologyError("selection.priority must be an integer or null")
    return OrganDependency(
        schema_version=SCHEMA_VERSION,
        dependency_id=_namespaced(record["dependency_id"], "dependency", path="dependency_id"),
        consumer=DependencyConsumer(
            organ_id=_namespaced(consumer["organ_id"], "organ", path="consumer.organ_id"),
            port_id=_namespaced(consumer["port_id"], "port", path="consumer.port_id"),
        ),
        provider=DependencyProvider(
            organ_id=_namespaced(provider["organ_id"], "organ", path="provider.organ_id"),
            port_id=_namespaced(provider["port_id"], "port", path="provider.port_id"),
            capability_ref=_namespaced(
                provider["capability_ref"], "capability", path="provider.capability_ref"
            ),
        ),
        relationship=_enum(DependencyRelationship, record["relationship"], path="relationship"),
        contract_ref=_stable_ref(record["contract_ref"], path="contract_ref"),
        schema_ref=_stable_ref(record["schema_ref"], path="schema_ref"),
        version_range=_string(record["version_range"], path="version_range"),
        freshness_bound=_string(record["freshness_bound"], path="freshness_bound"),
        failure_propagation=_strings(record["failure_propagation"], path="failure_propagation"),
        degraded_behavior=_strings(record["degraded_behavior"], path="degraded_behavior"),
        owner_ref=_namespaced(record["owner_ref"], "role", path="owner_ref"),
        evidence_refs=tuple(
            _namespaced(item, "evidence", path="evidence_refs[]")
            for item in _strings(record["evidence_refs"], path="evidence_refs")
        ),
        selection=ProviderSelection(
            authority_ref=parsed_authority,
            rule=_enum(SelectionRule, selection["rule"], path="selection.rule"),
            priority=priority,
            evidence_refs=tuple(
                _namespaced(item, "evidence", path="selection.evidence_refs[]")
                for item in _strings(selection["evidence_refs"], path="selection.evidence_refs")
            ),
        ),
        feedback=FeedbackContract(
            bootstrap=_strings(feedback["bootstrap"], path="feedback.bootstrap"),
            bound=_nullable_string(feedback["bound"], path="feedback.bound"),
            break_condition=_strings(feedback["break_condition"], path="feedback.break_condition"),
        ),
        source_sha256=_semantic_sha(value),
    )


_EVIDENCE_KEYS = (
    "schema_version", "evidence_id", "subject_ref", "facet", "evidence_kind",
    "canonical_ref", "sha256", "observer_ref", "observed_at", "fresh_until", "result",
)


def parse_evidence_ref(value: object, *, source: str) -> EvidenceRef:
    record = _exact(value, path=source, keys=_EVIDENCE_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"{source}.schema_version must equal {SCHEMA_VERSION}")
    digest = _string(record["sha256"], path="sha256")
    if DIGEST_RE.fullmatch(digest) is None:
        raise OrganismTopologyError("sha256 must be canonical lowercase digest")
    subject_record = _exact(record["subject_ref"], path="subject_ref", keys=("kind", "id"))
    subject_kind = _enum(EvidenceSubjectKind, subject_record["kind"], path="subject_ref.kind")
    subject = EvidenceSubjectRef(
        subject_kind,
        _namespaced(subject_record["id"], subject_kind.value, path="subject_ref.id"),
    )
    return EvidenceRef(
        schema_version=SCHEMA_VERSION,
        evidence_id=_namespaced(record["evidence_id"], "evidence", path="evidence_id"),
        subject_ref=subject,
        facet=_enum(HealthFacet, record["facet"], path="facet"),
        evidence_kind=_enum(EvidenceKind, record["evidence_kind"], path="evidence_kind"),
        canonical_ref=_stable_ref(record["canonical_ref"], path="canonical_ref"),
        sha256=digest,
        observer_ref=_namespaced(record["observer_ref"], "role", path="observer_ref"),
        observed_at=_timestamp(record["observed_at"], path="observed_at") or "",
        fresh_until=_timestamp(record["fresh_until"], path="fresh_until", nullable=True),
        result=_enum(EvidenceResult, record["result"], path="result"),
    )


_SURFACE_KEYS = (
    "schema_version", "surface_id", "attached_port", "transport", "audience",
    "boundaries", "owner_ref", "degradation", "evidence_refs",
)


def parse_external_surface(value: object, *, source: str) -> ExternalSurface:
    record = _exact(value, path=source, keys=_SURFACE_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"{source}.schema_version must equal {SCHEMA_VERSION}")
    attached = _exact(record["attached_port"], path="attached_port", keys=("organ_id", "port_id"))
    boundaries = _four_string_groups(
        record["boundaries"], path="boundaries", keys=("identity", "consent", "trust", "authority")
    )
    return ExternalSurface(
        schema_version=SCHEMA_VERSION,
        surface_id=_namespaced(record["surface_id"], "surface", path="surface_id"),
        attached_port=SurfacePort(
            organ_id=_namespaced(attached["organ_id"], "organ", path="attached_port.organ_id"),
            port_id=_namespaced(attached["port_id"], "port", path="attached_port.port_id"),
        ),
        transport=_string(record["transport"], path="transport"),
        audience=_strings(record["audience"], path="audience"),
        boundaries=SurfaceBoundaries(**boundaries),
        owner_ref=_namespaced(record["owner_ref"], "role", path="owner_ref"),
        degradation=_strings(record["degradation"], path="degradation"),
        evidence_refs=tuple(
            _namespaced(item, "evidence", path="evidence_refs[]")
            for item in _strings(record["evidence_refs"], path="evidence_refs")
        ),
        source_sha256=_semantic_sha(value),
    )


def _load_collection(root: Path, parser: Any, identity_attr: str) -> tuple[Any, ...]:
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise OrganismTopologyError(f"collection root must be a real directory: {root}")
    seen: set[str] = set()
    result: list[Any] = []
    for path in sorted(root.glob("*.yaml"), key=lambda item: item.name):
        value, raw = _load_yaml(path)
        parsed = parser(value, source=str(path))
        identity = getattr(parsed, identity_attr)
        suffix = identity.split(":", 1)[1]
        if path.stem != suffix:
            raise OrganismTopologyError(f"filename {path.name!r} does not match identity {identity!r}")
        if identity in seen:
            raise OrganismTopologyError(f"duplicate identity: {identity}")
        seen.add(identity)
        result.append(dataclasses.replace(parsed, source_sha256=_source_sha(raw)))
    return tuple(result)


def load_organism_record(path: Path) -> OrganismRecord:
    value, raw = _load_yaml(path)
    record = _exact(value, path=str(path), keys=("schema_version", "organism_id", "organ_ids"))
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise OrganismTopologyError(f"schema_version must equal {SCHEMA_VERSION}")
    organ_ids = tuple(
        _namespaced(item, "organ", path="organ_ids[]")
        for item in _strings(record["organ_ids"], path="organ_ids")
    )
    return OrganismRecord(
        schema_version=SCHEMA_VERSION,
        organism_id=_namespaced(record["organism_id"], "organism", path="organism_id"),
        organ_ids=organ_ids,
        source_sha256=_source_sha(raw),
    )


def load_organ_contracts(root: Path) -> dict[str, OrganContract]:
    items = _load_collection(root, parse_organ_contract, "organ_id")
    return {item.organ_id: item for item in items}


def load_organ_bindings(root: Path) -> tuple[OrganBinding, ...]:
    return _load_collection(root, parse_organ_binding, "binding_id")


def load_organ_dependencies(root: Path) -> tuple[OrganDependency, ...]:
    return _load_collection(root, parse_organ_dependency, "dependency_id")


def load_external_surfaces(root: Path) -> tuple[ExternalSurface, ...]:
    return _load_collection(root, parse_external_surface, "surface_id")


def load_organism_topology(repo_root: Path) -> OrganismTopology:
    base = repo_root / "organism"
    for directory in (
        base,
        base / "records",
        base / "organs",
        base / "bindings",
        base / "dependencies",
        base / "surfaces",
    ):
        if directory.is_symlink() or not directory.is_dir():
            raise OrganismTopologyError(
                f"topology directory must be a real non-symlink directory: {directory}"
            )
    return OrganismTopology(
        organism=load_organism_record(base / "records" / "organism.yaml"),
        organs=load_organ_contracts(base / "organs"),
        bindings=load_organ_bindings(base / "bindings"),
        dependencies=load_organ_dependencies(base / "dependencies"),
        surfaces=load_external_surfaces(base / "surfaces"),
    )


def build_reference_indexes(
    repo_root: Path,
    *,
    canonical_ref_sha256: Mapping[str, str] | None = None,
) -> ReferenceIndexes:
    projects = project_catalogue.load_project_records(repo_root / "projects" / "records")
    components = component_catalogue.load_component_records(repo_root / "components" / "records")
    capabilities = {
        provision.capability for record in components.values() for provision in record.provides
    }
    obligation_path = repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
    obligations = (
        grand_project_program.load_obligation_index(obligation_path)
        if obligation_path.is_file()
        else ()
    )
    digests = dict(canonical_ref_sha256 or {})
    for key, value in digests.items():
        if (
            not isinstance(key, str)
            or STABLE_REF_RE.fullmatch(key) is None
            or not isinstance(value, str)
            or DIGEST_RE.fullmatch(value) is None
        ):
            raise OrganismTopologyError("canonical evidence digest index is malformed")
    return ReferenceIndexes(
        project_ids=frozenset(projects),
        component_ids=frozenset(components),
        capability_ids=frozenset(capabilities),
        obligation_ids=frozenset(item.obligation_id for item in obligations),
        canonical_ref_sha256=digests,
    )


def _suffix(reference: str) -> str:
    return reference.split(":", 1)[1]


def _evidence_state(record: EvidenceRef, snapshot: EvidenceSnapshot, now: dt.datetime) -> FacetState:
    expected = snapshot.canonical_ref_sha256.get(record.canonical_ref)
    if expected is None or expected != record.sha256:
        return FacetState.UNKNOWN
    if _instant(record.observed_at) > now.astimezone(dt.timezone.utc):
        return FacetState.UNKNOWN
    if record.fresh_until is not None and _instant(record.fresh_until) < now.astimezone(dt.timezone.utc):
        return FacetState.STALE
    if record.evidence_kind is EvidenceKind.DECLARATION:
        return FacetState.UNKNOWN
    if record.result is EvidenceResult.AMBIGUOUS:
        return FacetState.UNKNOWN
    return FacetState.HEALTHY if record.result is EvidenceResult.PASS else FacetState.FAILED


def evaluate_organ_health(
    contract: OrganContract,
    binding: OrganBinding,
    evidence: EvidenceSnapshot,
    *,
    now: dt.datetime,
) -> OrganHealth:
    if now.tzinfo is None:
        raise OrganismTopologyError("now must be timezone-aware")
    facets: dict[HealthFacet, FacetState] = {}
    selected_evidence = set(binding.evidence_refs)
    for facet in contract.health.required_facets:
        states = [
            _evidence_state(record, evidence, now)
            for evidence_id, record in evidence.records.items()
            if evidence_id in selected_evidence
            and record.subject_ref.id in {binding.binding_id, contract.organ_id}
            and record.facet is facet
        ]
        current = set(states)
        if FacetState.HEALTHY in current and FacetState.FAILED in current:
            state = FacetState.INCONSISTENT
        elif FacetState.FAILED in current:
            state = FacetState.FAILED
        elif FacetState.HEALTHY in current:
            state = FacetState.HEALTHY
        elif FacetState.STALE in current:
            state = FacetState.STALE
        else:
            state = FacetState.UNKNOWN
        facets[facet] = state
    return OrganHealth(contract.organ_id, binding.binding_id, facets)


def _issue(code: str, record_id: str, detail: str) -> TopologyIssue:
    return TopologyIssue(code, record_id, detail)


def _current_passing_authority(evidence_id: str, snapshot: EvidenceSnapshot, now: dt.datetime) -> bool:
    record = snapshot.records.get(evidence_id)
    return (
        record is not None
        and record.facet is HealthFacet.AUTHORITY
        and _evidence_state(record, snapshot, now) is FacetState.HEALTHY
    )


def validate_topology(
    topology: OrganismTopology,
    refs: ReferenceIndexes,
    evidence: EvidenceSnapshot,
    *,
    now: dt.datetime,
) -> tuple[TopologyIssue, ...]:
    if now.tzinfo is None:
        raise OrganismTopologyError("now must be timezone-aware")
    issues: list[TopologyIssue] = []
    organ_ids = set(topology.organs)
    if set(topology.organism.organ_ids) != organ_ids:
        issues.append(_issue("organ-membership-mismatch", topology.organism.organism_id,
                             "organism organ_ids differ from loaded contracts"))

    for organ_id in sorted(topology.organs):
        contract = topology.organs[organ_id]
        if organ_id != contract.organ_id:
            issues.append(_issue("identity-key-mismatch", organ_id, contract.organ_id))
        for ref in contract.project_refs:
            if _suffix(ref) not in refs.project_ids:
                issues.append(_issue("unknown-project", organ_id, ref))
        for ref in contract.component_refs:
            if _suffix(ref) not in refs.component_ids:
                issues.append(_issue("unknown-component", organ_id, ref))
        for ref in contract.obligation_refs + contract.re_evaluate_when:
            if ref not in refs.obligation_ids:
                issues.append(_issue("unknown-obligation", organ_id, ref))

    binding_ids: set[str] = set()
    for binding in topology.bindings:
        if binding.binding_id in binding_ids:
            issues.append(_issue("duplicate-binding", binding.binding_id, "duplicate binding id"))
        binding_ids.add(binding.binding_id)
        contract = topology.organs.get(binding.organ_id)
        if contract is None:
            issues.append(_issue("unknown-binding-organ", binding.binding_id, binding.organ_id))
            continue
        all_ports = {item.port_id for item in contract.ports.inputs + contract.ports.outputs}
        for port_id in binding.ports:
            if port_id not in all_ports:
                issues.append(_issue("unknown-binding-port", binding.binding_id, port_id))
        for evidence_id in binding.evidence_refs:
            if evidence_id not in evidence.records:
                issues.append(_issue("unknown-evidence", binding.binding_id, evidence_id))
        if binding.lifecycle is BindingLifecycle.LIVE:
            timestamps_current = (
                binding.verified_at is not None
                and binding.fresh_until is not None
                and _instant(binding.verified_at) <= now.astimezone(dt.timezone.utc)
                and _instant(binding.fresh_until) >= now.astimezone(dt.timezone.utc)
            )
            health = evaluate_organ_health(contract, binding, evidence, now=now)
            if not timestamps_current or any(
                state is not FacetState.HEALTHY for state in health.facets.values()
            ):
                issues.append(_issue("unsupported-live-binding", binding.binding_id,
                                     "live requires current passing evidence for every required facet"))

    dependency_ids: set[str] = set()
    adjacency: dict[str, list[tuple[str, OrganDependency]]] = defaultdict(list)
    groups: dict[tuple[str, str], list[OrganDependency]] = defaultdict(list)
    for dependency in topology.dependencies:
        if dependency.dependency_id in dependency_ids:
            issues.append(_issue("duplicate-dependency", dependency.dependency_id, "duplicate id"))
        dependency_ids.add(dependency.dependency_id)
        consumer = topology.organs.get(dependency.consumer.organ_id)
        provider = topology.organs.get(dependency.provider.organ_id)
        if consumer is None:
            issues.append(_issue("unknown-consumer-organ", dependency.dependency_id,
                                 dependency.consumer.organ_id))
        else:
            inputs = {item.port_id: item for item in consumer.ports.inputs}
            port = inputs.get(dependency.consumer.port_id)
            if port is None:
                issues.append(_issue("unknown-consumer-port", dependency.dependency_id,
                                     dependency.consumer.port_id))
            elif (
                port.contract_ref != dependency.contract_ref
                or port.schema_ref != dependency.schema_ref
                or port.version_range != dependency.version_range
            ):
                issues.append(_issue("consumer-contract-mismatch", dependency.dependency_id,
                                     "contract/schema/version mismatch"))
        if provider is None:
            issues.append(_issue("unknown-provider-organ", dependency.dependency_id,
                                 dependency.provider.organ_id))
        else:
            outputs = {item.port_id: item for item in provider.ports.outputs}
            port = outputs.get(dependency.provider.port_id)
            if port is None:
                issues.append(_issue("unknown-provider-port", dependency.dependency_id,
                                     dependency.provider.port_id))
            elif (
                port.contract_ref != dependency.contract_ref
                or port.schema_ref != dependency.schema_ref
                or port.version_range != dependency.version_range
            ):
                issues.append(_issue("provider-contract-mismatch", dependency.dependency_id,
                                     "contract/schema/version mismatch"))
        if _suffix(dependency.provider.capability_ref) not in refs.capability_ids:
            issues.append(_issue("unknown-capability", dependency.dependency_id,
                                 dependency.provider.capability_ref))
        for evidence_id in dependency.evidence_refs:
            if evidence_id not in evidence.records:
                issues.append(_issue("unknown-evidence", dependency.dependency_id, evidence_id))
        is_feedback = dependency.relationship is DependencyRelationship.FEEDBACK
        feedback_complete = bool(
            dependency.feedback.bootstrap
            and dependency.feedback.bound
            and dependency.feedback.break_condition
        )
        feedback_empty = (
            not dependency.feedback.bootstrap
            and dependency.feedback.bound is None
            and not dependency.feedback.break_condition
        )
        if (is_feedback and not feedback_complete) or (not is_feedback and not feedback_empty):
            issues.append(_issue("feedback-shape-invalid", dependency.dependency_id,
                                 "feedback semantics disagree with relationship"))
        adjacency[dependency.consumer.organ_id].append((dependency.provider.organ_id, dependency))
        groups[(dependency.consumer.organ_id, dependency.consumer.port_id)].append(dependency)

    for group_key, group in sorted(groups.items()):
        if len(group) == 1:
            selection = group[0].selection
            if (
                selection.rule is not SelectionRule.SOLE
                or selection.priority is not None
                or selection.evidence_refs
            ):
                issues.append(_issue("sole-selection-invalid", group[0].dependency_id, str(group_key)))
            continue
        authorities = {item.selection.authority_ref for item in group}
        rules = {item.selection.rule for item in group}
        if None in authorities or len(authorities) != 1:
            issues.append(_issue("selection-authority-mismatch", str(group_key), repr(authorities)))
        if len(rules) != 1 or SelectionRule.SOLE in rules:
            issues.append(_issue("selection-rule-mismatch", str(group_key), repr(rules)))
            continue
        rule = next(iter(rules))
        if rule is SelectionRule.PRIORITY:
            priorities = [item.selection.priority for item in group]
            if any(value is None for value in priorities):
                issues.append(_issue("selection-priority-missing", str(group_key), repr(priorities)))
            elif len(set(priorities)) != len(priorities):
                issues.append(_issue("selection-priority-duplicate", str(group_key), repr(priorities)))
        elif rule is SelectionRule.EXPLICIT:
            if any(
                not item.selection.evidence_refs
                or not all(
                    _current_passing_authority(evidence_id, evidence, now)
                    and evidence.records[evidence_id].subject_ref.id == item.dependency_id
                    for evidence_id in item.selection.evidence_refs
                )
                for item in group
            ):
                issues.append(_issue("selection-evidence-invalid", str(group_key),
                                     "explicit selection needs current authority evidence"))

    cycle_dependencies: set[str] = set()

    def visit(node: str, stack: list[str], edge_stack: list[OrganDependency], positions: dict[str, int]) -> None:
        if node in positions:
            start = positions[node]
            for edge in edge_stack[start:]:
                cycle_dependencies.add(edge.dependency_id)
            return
        positions[node] = len(stack)
        stack.append(node)
        for target, edge in adjacency.get(node, ()):  # deterministic below
            edge_stack.append(edge)
            visit(target, stack, edge_stack, positions)
            edge_stack.pop()
        stack.pop()
        positions.pop(node, None)

    for node in sorted(organ_ids):
        visit(node, [], [], {})
    dependency_by_id = {item.dependency_id: item for item in topology.dependencies}
    if cycle_dependencies:
        cycle_edges = [dependency_by_id[item] for item in sorted(cycle_dependencies)]
        if any(
            edge.relationship is not DependencyRelationship.FEEDBACK
            or not edge.feedback.bootstrap
            or edge.feedback.bound is None
            or not edge.feedback.break_condition
            for edge in cycle_edges
        ):
            issues.append(_issue("dependency-cycle", "*", repr(sorted(cycle_dependencies))))

    surface_ids: set[str] = set()
    for surface in topology.surfaces:
        if surface.surface_id in surface_ids:
            issues.append(_issue("duplicate-surface", surface.surface_id, "duplicate id"))
        surface_ids.add(surface.surface_id)
        contract = topology.organs.get(surface.attached_port.organ_id)
        if contract is None:
            issues.append(_issue("unknown-surface-organ", surface.surface_id,
                                 surface.attached_port.organ_id))
        elif surface.attached_port.port_id not in {
            item.port_id for item in contract.ports.inputs + contract.ports.outputs
        }:
            issues.append(_issue("unknown-surface-port", surface.surface_id,
                                 surface.attached_port.port_id))
        for evidence_id in surface.evidence_refs:
            if evidence_id not in evidence.records:
                issues.append(_issue("unknown-evidence", surface.surface_id, evidence_id))

    valid_subjects = organ_ids | binding_ids | dependency_ids | surface_ids
    for evidence_id, record in evidence.records.items():
        if evidence_id != record.evidence_id:
            issues.append(_issue("evidence-key-mismatch", evidence_id, record.evidence_id))
        if record.subject_ref.id not in valid_subjects:
            issues.append(_issue("unknown-evidence-subject", evidence_id, record.subject_ref.id))
        expected = evidence.canonical_ref_sha256.get(record.canonical_ref)
        if expected is None:
            issues.append(_issue("unresolved-evidence-source", evidence_id, record.canonical_ref))
        elif expected != record.sha256:
            issues.append(_issue("evidence-digest-mismatch", evidence_id, record.canonical_ref))

    return tuple(sorted(set(issues)))


def _array(item: Mapping[str, object], *, unique: bool = True) -> dict[str, object]:
    result: dict[str, object] = {"type": "array", "items": item}
    if unique:
        result["uniqueItems"] = True
    return result


def _object(properties: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(properties),
    }


def _string_schema(pattern: str | None = None, enum: Sequence[str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "minLength": 1}
    if pattern is not None:
        result["pattern"] = pattern
    if enum is not None:
        result["enum"] = list(enum)
    return result


def _stable_ref_schema() -> dict[str, object]:
    return _string_schema(r"^[A-Za-z][A-Za-z0-9._-]*:[^\s]+$")


def _nullable(schema: Mapping[str, object]) -> dict[str, object]:
    return {"anyOf": [{"type": "null"}, dict(schema)]}


def _id_schema(kind: str) -> dict[str, object]:
    return _string_schema(rf"^{kind}:[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _typed_ref_schema(allowed: Sequence[IdentityKind] | None = None) -> dict[str, object]:
    kinds = tuple(allowed or IdentityKind)
    variants = []
    for kind in kinds:
        variants.append(
            _object({"kind": {"const": kind.value}, "id": _id_schema(kind.value)})
        )
    return {"oneOf": variants}


def _port_schema() -> dict[str, object]:
    return _object({
        "port_id": _id_schema("port"),
        "contract_ref": _stable_ref_schema(),
        "schema_ref": _stable_ref_schema(),
        "version_range": _string_schema(),
        "authority_ref": _id_schema("role"),
        "confidentiality": _string_schema(),
        "freshness_bound": _nullable(_string_schema()),
    })


def generate_organism_record_schema() -> bytes:
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 organism record",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "organism_id": _id_schema("organism"),
            "organ_ids": _array(_id_schema("organ")),
        }),
    })


def generate_organ_contract_schema() -> bytes:
    strings = _array(_string_schema())
    facets = _array(_string_schema(enum=[item.value for item in HealthFacet]))
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 organ contract",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "organ_id": _id_schema("organ"),
            "responsibility": _string_schema(),
            "must_not_become": strings,
            "ports": _object({"inputs": _array(_port_schema()), "outputs": _array(_port_schema())}),
            "authoritative_inputs": _array(_id_schema("port")),
            "authoritative_outputs": _array(_id_schema("port")),
            "boundaries": _object({key: strings for key in ("identity", "data", "trust", "authority")}),
            "expectations": _object({"latency": strings, "availability": strings}),
            "health": _object({"required_facets": facets, "evidence_requirements": strings}),
            "failure_modes": strings,
            "degradation": _object({key: strings for key in ("unavailable", "stale", "partitioned", "inconsistent", "unknown")}),
            "recovery": _object({
                "owner_ref": _id_schema("role"),
                "escalation_ref": _id_schema("role"),
                "verification_requirements": strings,
            }),
            "replacement": _object({
                "continuity_requirements": strings,
                "provenance_requirements": strings,
                "rollback_requirements": strings,
            }),
            "owners": _array(_id_schema("role")),
            "project_refs": _array(_id_schema("project")),
            "component_refs": _array(_id_schema("component")),
            "obligation_refs": strings,
            "re_evaluate_when": strings,
        }),
    })


def generate_organ_binding_schema() -> bytes:
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 organ binding",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "binding_id": _id_schema("binding"),
            "organ_id": _id_schema("organ"),
            "implementation_ref": _typed_ref_schema((IdentityKind.PERSONA, IdentityKind.ROLE, IdentityKind.COMPONENT, IdentityKind.SERVICE, IdentityKind.DEVICE)),
            "seed_ref": _id_schema("seed"),
            "locus": _string_schema(),
            "ports": _array(_id_schema("port")),
            "lifecycle": _string_schema(enum=[item.value for item in BindingLifecycle]),
            "authority_owner_ref": _id_schema("role"),
            "evidence_refs": _array(_id_schema("evidence")),
            "verified_at": _nullable({"type": "string", "format": "date-time"}),
            "fresh_until": _nullable({"type": "string", "format": "date-time"}),
        }),
    })


def generate_organ_dependency_schema() -> bytes:
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 organ dependency",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "dependency_id": _id_schema("dependency"),
            "consumer": _object({"organ_id": _id_schema("organ"), "port_id": _id_schema("port")}),
            "provider": _object({"organ_id": _id_schema("organ"), "port_id": _id_schema("port"), "capability_ref": _id_schema("capability")}),
            "relationship": _string_schema(enum=[item.value for item in DependencyRelationship]),
            "contract_ref": _stable_ref_schema(),
            "schema_ref": _stable_ref_schema(),
            "version_range": _string_schema(),
            "freshness_bound": _string_schema(),
            "failure_propagation": _array(_string_schema()),
            "degraded_behavior": _array(_string_schema()),
            "owner_ref": _id_schema("role"),
            "evidence_refs": _array(_id_schema("evidence")),
            "selection": _object({
                "authority_ref": _nullable(_id_schema("role")),
                "rule": _string_schema(enum=[item.value for item in SelectionRule]),
                "priority": _nullable({"type": "integer"}),
                "evidence_refs": _array(_id_schema("evidence")),
            }),
            "feedback": _object({
                "bootstrap": _array(_string_schema()),
                "bound": _nullable(_string_schema()),
                "break_condition": _array(_string_schema()),
            }),
        }),
    })


def generate_organ_evidence_ref_schema() -> bytes:
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 organ evidence reference",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "evidence_id": _id_schema("evidence"),
            "subject_ref": {"oneOf": [
                _object({"kind": {"const": "organ"}, "id": _id_schema("organ")}),
                _object({"kind": {"const": "binding"}, "id": _id_schema("binding")}),
                _object({"kind": {"const": "dependency"}, "id": _id_schema("dependency")}),
                _object({"kind": {"const": "surface"}, "id": _id_schema("surface")}),
            ]},
            "facet": _string_schema(enum=[item.value for item in HealthFacet]),
            "evidence_kind": _string_schema(enum=[item.value for item in EvidenceKind]),
            "canonical_ref": _stable_ref_schema(),
            "sha256": _string_schema(r"^sha256:[0-9a-f]{64}$"),
            "observer_ref": _id_schema("role"),
            "observed_at": {"type": "string", "format": "date-time"},
            "fresh_until": _nullable({"type": "string", "format": "date-time"}),
            "result": _string_schema(enum=[item.value for item in EvidenceResult]),
        }),
    })


def generate_external_surface_schema() -> bytes:
    strings = _array(_string_schema())
    return _json_bytes({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Pantheon ORG-056 external surface",
        **_object({
            "schema_version": {"const": SCHEMA_VERSION},
            "surface_id": _id_schema("surface"),
            "attached_port": _object({"organ_id": _id_schema("organ"), "port_id": _id_schema("port")}),
            "transport": _string_schema(),
            "audience": strings,
            "boundaries": _object({key: strings for key in ("identity", "consent", "trust", "authority")}),
            "owner_ref": _id_schema("role"),
            "degradation": strings,
            "evidence_refs": _array(_id_schema("evidence")),
        }),
    })


def generate_topology_schemas() -> dict[str, bytes]:
    return {
        "organism-record.schema.json": generate_organism_record_schema(),
        "organ-contract.schema.json": generate_organ_contract_schema(),
        "organ-binding.schema.json": generate_organ_binding_schema(),
        "organ-dependency.schema.json": generate_organ_dependency_schema(),
        "organ-evidence-ref.schema.json": generate_organ_evidence_ref_schema(),
        "external-surface.schema.json": generate_external_surface_schema(),
    }


def generate_topology_index(topology: OrganismTopology) -> bytes:
    semantic = {
        "organism": topology.organism.source_sha256,
        "organs": {key: topology.organs[key].source_sha256 for key in sorted(topology.organs)},
        "bindings": {item.binding_id: item.source_sha256 for item in sorted(topology.bindings, key=lambda x: x.binding_id)},
        "dependencies": {item.dependency_id: item.source_sha256 for item in sorted(topology.dependencies, key=lambda x: x.dependency_id)},
        "surfaces": {item.surface_id: item.source_sha256 for item in sorted(topology.surfaces, key=lambda x: x.surface_id)},
    }
    generated_from = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    def entries(items: Sequence[Any], identity: str, directory: str) -> dict[str, object]:
        return {
            getattr(item, identity): {
                "record": f"organism/{directory}/{getattr(item, identity).split(':', 1)[1]}.yaml",
                "record_sha256": f"sha256:{item.source_sha256}",
            }
            for item in sorted(items, key=lambda value: getattr(value, identity))
        }

    return _json_bytes({
        "schema_version": SCHEMA_VERSION,
        "generated_from_digest": f"sha256:{generated_from}",
        "organism": {
            "organism_id": topology.organism.organism_id,
            "record": "organism/records/organism.yaml",
            "record_sha256": f"sha256:{topology.organism.source_sha256}",
        },
        "organs": {
            key: {
                "record": f"organism/organs/{key.split(':', 1)[1]}.yaml",
                "record_sha256": f"sha256:{topology.organs[key].source_sha256}",
            }
            for key in sorted(topology.organs)
        },
        "bindings": entries(topology.bindings, "binding_id", "bindings"),
        "dependencies": entries(topology.dependencies, "dependency_id", "dependencies"),
        "surfaces": entries(topology.surfaces, "surface_id", "surfaces"),
    })


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OrganismTopologyError(
            f"generated artifact parent must be a real non-symlink directory: {path.parent}"
        )
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise OrganismTopologyError(
            f"generated artifact target must be a regular non-symlink file: {path}"
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_generated_artifact(path: Path) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def write_generated_artifacts(repo_root: Path) -> None:
    topology = load_organism_topology(repo_root)
    refs = build_reference_indexes(repo_root)
    issues = validate_topology(topology, refs, EvidenceSnapshot.empty(), now=dt.datetime.now(dt.timezone.utc))
    if issues:
        raise OrganismTopologyError(f"topology invalid: {issues!r}")
    for filename, payload in generate_topology_schemas().items():
        _atomic_write(repo_root / "schemas" / filename, payload)
    _atomic_write(repo_root / "organism" / "index.json", generate_topology_index(topology))


def check_organism_topology(
    repo_root: Path,
    *,
    now: dt.datetime | None = None,
    evidence: EvidenceSnapshot | None = None,
    canonical_ref_sha256: Mapping[str, str] | None = None,
) -> tuple[TopologyIssue, ...]:
    snapshot = evidence or EvidenceSnapshot.empty()
    if canonical_ref_sha256 is not None:
        snapshot = EvidenceSnapshot(snapshot.records, dict(canonical_ref_sha256))
    try:
        topology = load_organism_topology(repo_root)
        refs = build_reference_indexes(
            repo_root, canonical_ref_sha256=snapshot.canonical_ref_sha256
        )
    except (
        OrganismTopologyError,
        project_catalogue.ProjectCatalogueError,
        component_catalogue.ComponentCatalogueError,
        ValueError,
        OSError,
    ) as exc:
        return (_issue("topology-invalid", "*", str(exc)),)
    issues = list(
        validate_topology(
            topology,
            refs,
            snapshot,
            now=now or dt.datetime.now(dt.timezone.utc),
        )
    )
    if topology.organism.organism_id == "organism:grand-project" and (
        tuple(topology.organism.organ_ids) != CANONICAL_ORGAN_IDS
        or set(topology.organs) != set(CANONICAL_ORGAN_IDS)
    ):
        issues.append(_issue("canonical-organ-set-mismatch", topology.organism.organism_id,
                             "live topology must contain the exact twelve ORG-056 contracts"))
    for filename, expected in generate_topology_schemas().items():
        path = repo_root / "schemas" / filename
        actual = _read_generated_artifact(path)
        if actual != expected:
            issues.append(_issue("schema-stale", "*", filename))
    if not [issue for issue in issues if issue.code not in {"schema-stale"}]:
        expected_index = generate_topology_index(topology)
        actual_index = _read_generated_artifact(repo_root / "organism" / "index.json")
        if actual_index != expected_index:
            issues.append(_issue("index-stale", "*", "organism/index.json"))
    return tuple(sorted(set(issues)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    children = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "generate"):
        child = children.add_parser(command)
        child.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            write_generated_artifacts(args.repo_root)
            return 0
        issues = check_organism_topology(args.repo_root)
    except (OrganismTopologyError, ValueError, OSError) as exc:
        print(f"organism-topology-invalid: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        print(f"{issue.code}: {issue.record_id}: {issue.detail}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
