"""Strict ORG-055 component catalogue, boundary decisions, and generated index.

Component records are hand-authored canonical inputs.  Their schemas and the
compact lookup index are generated artifacts whose exact bytes are checked.
This module validates compatibility declarations; it does not resolve assembly
ranges, mutate checkouts, or deploy components.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import AbstractSet, Any

import yaml

from tools import grand_project_program
from tools import project_catalogue


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 256 * 1024
STABLE_ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
DECISION_ID_PATTERN = r"^repository-boundary:[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
STABLE_ID_RE = re.compile(STABLE_ID_PATTERN)
DECISION_ID_RE = re.compile(DECISION_ID_PATTERN)
VERSION_RE = re.compile(VERSION_PATTERN)
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

COMPONENT_RECORD_KEYS = (
    "schema_version",
    "component_id",
    "project_id",
    "kind",
    "source",
    "provides",
    "requires",
    "compatibility",
    "deployment",
    "implications",
    "upkeep",
    "maturity",
    "tensions",
)
SOURCE_KEYS = ("logical_repo", "path", "artifact")
PROVISION_KEYS = ("capability", "contract", "schema", "version", "features")
REQUIREMENT_KEYS = (
    "capability",
    "contract",
    "schema",
    "version",
    "feature",
    "enabled",
    "degradation_ref",
)
REQUIRES_KEYS = ("hard", "optional")
ACCEPTS_KEYS = ("capability", "contract", "schema", "version")
CONFLICT_KEYS = ("kind", "target", "reason_ref")
COMPATIBILITY_KEYS = ("accepts", "conflicts", "tested_revision")
DEPLOYMENT_KEYS = ("loci", "trust_zone")
IMPLICATION_CATEGORY_KEYS = (
    "authority",
    "privacy_data",
    "hardware_network",
    "operational",
)
IMPLICATION_KEYS = ("implication_id", "scope", "evidence_refs")
UPKEEP_KEYS = (
    "steward",
    "health_evidence",
    "monitoring",
    "upgrade_rollback",
    "credential_rotation",
    "storage_retention",
    "cost",
    "review_obligations",
)
COST_KEYS = (
    "cost_id",
    "category",
    "quantity",
    "unit",
    "cadence",
    "measurement_source",
    "owner",
    "alternative_ref",
    "last_measured_at",
    "review_due_at",
)
MATURITY_KEYS = ("developmental_mode", "replaceability")
TENSION_KEYS = ("tension_id", "pole_a", "pole_b", "rebalance_trigger")

BOUNDARY_DECISION_KEYS = (
    "schema_version",
    "decision_id",
    "project_id",
    "component_id",
    "current_source",
    "contract",
    "independence_signals",
    "keep_together_reasons",
    "decision",
    "migration",
    "operational_contract",
    "compatibility_impact",
    "implication_acknowledgements",
    "upkeep_estimate_refs",
    "authority",
    "re_evaluate_when",
)
BOUNDARY_SOURCE_KEYS = ("logical_repo", "path")
BOUNDARY_CONTRACT_KEYS = ("id", "version", "schema_refs", "test_evidence_refs")
MIGRATION_KEYS = ("history", "data", "source_cutover", "old_path_disposition", "rollback")
BOUNDARY_OPERATION_KEYS = ("install", "update", "configure", "monitor", "remove")
COMPATIBILITY_IMPACT_KEYS = ("affected_assemblies", "catalogue_changes")
AUTHORITY_KEYS = ("decision_ref", "reviewer_evidence_refs")


class ComponentCatalogueError(ValueError):
    """A component record, decision, schema, or index cannot be accepted."""


class ComponentKind(str, Enum):
    SERVICE = "service"
    LIBRARY = "library"
    CONFIG = "config"
    MODEL = "model"
    DEVICE_ADAPTER = "device-adapter"
    DATA_STORE = "data-store"
    INTERFACE = "interface"


class DevelopmentalMode(str, Enum):
    SHAPING = "shaping"
    INFOLDING = "infolding"
    INFILLING = "infilling"
    OPERATING = "operating"
    REORIENTING = "reorienting"
    RETIRING = "retiring"


class Replaceability(str, Enum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    FIXED_WITH_REASON = "fixed-with-reason"


class ConflictKind(str, Enum):
    COMPONENT = "component"
    CAPABILITY = "capability"
    FEATURE = "feature"


class CostCategory(str, Enum):
    COMPUTE = "compute"
    STORAGE = "storage"
    NETWORK = "network"
    TOKEN = "token"
    SUBSCRIPTION = "subscription"
    OPERATOR_ATTENTION = "operator-attention"
    OTHER = "other"


class CostCadence(str, Enum):
    PER_USE = "per-use"
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    ANNUAL = "annual"
    ONE_TIME = "one-time"


class BoundaryDecisionKind(str, Enum):
    EXTRACT = "extract"
    KEEP_TOGETHER = "keep-together"


class IndependenceSignal(str, Enum):
    INDEPENDENT_CADENCE = "independent-cadence"
    DISTINCT_AUTHORITY = "distinct-authority"
    DISTINCT_STEWARDSHIP = "distinct-stewardship"
    REUSED_OR_SUBSTITUTABLE = "reused-or-substitutable"
    DIFFERENT_RUNTIME = "different-runtime"
    MATERIAL_WEIGHT = "material-weight"


class KeepTogetherReason(str, Enum):
    RAPID_INTERFACE_CHURN = "rapid-interface-churn"
    ATOMIC_CHANGE_COUPLING = "atomic-change-coupling"
    SHARED_RELEASE_IDENTITY = "shared-release-identity"
    COORDINATION_ONLY_EXTRACTION = "coordination-only-extraction"


@dataclass(frozen=True)
class ComponentIssue:
    code: str
    component_id: str
    detail: str


@dataclass(frozen=True)
class ComponentSource:
    logical_repo: str
    path: str | None
    artifact: str | None


@dataclass(frozen=True)
class CapabilityProvision:
    capability: str
    contract: str
    schema: str
    version: str
    features: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityRequirement:
    capability: str
    contract: str
    schema: str
    version: str
    feature: str | None
    enabled: bool
    degradation_ref: str | None


@dataclass(frozen=True)
class CompatibilityAcceptance:
    capability: str
    contract: str
    schema: str
    version: str


@dataclass(frozen=True)
class CompatibilityPredicate:
    kind: ConflictKind
    target: str
    reason_ref: str


@dataclass(frozen=True)
class ComponentCompatibility:
    accepts: tuple[CompatibilityAcceptance, ...]
    conflicts: tuple[CompatibilityPredicate, ...]
    tested_revision: str


@dataclass(frozen=True)
class ComponentDeployment:
    loci: tuple[str, ...]
    trust_zone: str


@dataclass(frozen=True)
class ComponentImplication:
    implication_id: str
    scope: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ComponentImplications:
    authority: tuple[ComponentImplication, ...]
    privacy_data: tuple[ComponentImplication, ...]
    hardware_network: tuple[ComponentImplication, ...]
    operational: tuple[ComponentImplication, ...]


@dataclass(frozen=True)
class RecurringCost:
    cost_id: str
    category: CostCategory
    quantity: float
    unit: str
    cadence: CostCadence
    measurement_source: str
    owner: str
    alternative_ref: str | None
    last_measured_at: str
    review_due_at: str


@dataclass(frozen=True)
class ComponentUpkeep:
    steward: str
    health_evidence: tuple[str, ...]
    monitoring: tuple[str, ...]
    upgrade_rollback: tuple[str, ...]
    credential_rotation: tuple[str, ...]
    storage_retention: tuple[str, ...]
    cost: tuple[RecurringCost, ...]
    review_obligations: tuple[str, ...]


@dataclass(frozen=True)
class ComponentMaturity:
    developmental_mode: DevelopmentalMode
    replaceability: Replaceability


@dataclass(frozen=True)
class ComponentTension:
    tension_id: str
    pole_a: str
    pole_b: str
    rebalance_trigger: str


@dataclass(frozen=True)
class ComponentRecord:
    schema_version: int
    component_id: str
    project_id: str
    kind: ComponentKind
    source: ComponentSource
    provides: tuple[CapabilityProvision, ...]
    hard_requirements: tuple[CapabilityRequirement, ...]
    optional_requirements: tuple[CapabilityRequirement, ...]
    compatibility: ComponentCompatibility
    deployment: ComponentDeployment
    implications: ComponentImplications
    upkeep: ComponentUpkeep
    maturity: ComponentMaturity
    tensions: tuple[ComponentTension, ...]
    source_sha256: str


@dataclass(frozen=True)
class BoundarySource:
    logical_repo: str
    path: str


@dataclass(frozen=True)
class BoundaryContract:
    id: str
    version: str
    schema_refs: tuple[str, ...]
    test_evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryMigration:
    history: tuple[str, ...]
    data: tuple[str, ...]
    source_cutover: tuple[str, ...]
    old_path_disposition: tuple[str, ...]
    rollback: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryOperationalContract:
    install: tuple[str, ...]
    update: tuple[str, ...]
    configure: tuple[str, ...]
    monitor: tuple[str, ...]
    remove: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryCompatibilityImpact:
    affected_assemblies: tuple[str, ...]
    catalogue_changes: tuple[str, ...]


@dataclass(frozen=True)
class BoundaryAuthority:
    decision_ref: str
    reviewer_evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryBoundaryDecision:
    schema_version: int
    decision_id: str
    project_id: str
    component_id: str
    current_source: BoundarySource
    contract: BoundaryContract
    independence_signals: tuple[IndependenceSignal, ...]
    keep_together_reasons: tuple[KeepTogetherReason, ...]
    decision: BoundaryDecisionKind
    migration: BoundaryMigration
    operational_contract: BoundaryOperationalContract
    compatibility_impact: BoundaryCompatibilityImpact
    implication_acknowledgements: tuple[str, ...]
    upkeep_estimate_refs: tuple[str, ...]
    authority: BoundaryAuthority
    re_evaluate_when: tuple[str, ...]


class _StrictLoader(yaml.SafeLoader):
    pass


_StrictLoader.yaml_implicit_resolvers = {
    key: [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _strict_construct_mapping(
    loader: _StrictLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[str, Any]:
    if not isinstance(node, yaml.nodes.MappingNode):
        raise ComponentCatalogueError("YAML mapping required")
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ComponentCatalogueError("YAML mapping keys must be strings")
        if key == "<<":
            raise ComponentCatalogueError("YAML merge keys are forbidden")
        if key in seen:
            raise ComponentCatalogueError(f"duplicate YAML key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_construct_mapping
)


def _exact_mapping(
    value: object, *, path: str, keys: Sequence[str]
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ComponentCatalogueError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ComponentCatalogueError(f"{path} keys must be strings")
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        raise ComponentCatalogueError(
            f"{path} keys differ: missing={sorted(expected - actual)!r} "
            f"unknown={sorted(actual - expected)!r}"
        )
    return value


def _string(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ComponentCatalogueError(f"{path} must be a non-empty trimmed string")
    return value


def _nullable_string(value: object, *, path: str) -> str | None:
    return None if value is None else _string(value, path=path)


def _string_tuple(
    value: object, *, path: str, require_nonempty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ComponentCatalogueError(f"{path} must be a list")
    result = tuple(_string(item, path=f"{path}[]") for item in value)
    if require_nonempty and not result:
        raise ComponentCatalogueError(f"{path} must not be empty")
    if len(set(result)) != len(result):
        raise ComponentCatalogueError(f"{path} must not contain duplicates")
    return result


def _stable_id(value: object, *, path: str) -> str:
    text = _string(value, path=path)
    if STABLE_ID_RE.fullmatch(text) is None:
        raise ComponentCatalogueError(f"{path} must be a stable lowercase identifier")
    return text


def _decision_id(value: object) -> str:
    text = _string(value, path="decision_id")
    if DECISION_ID_RE.fullmatch(text) is None:
        raise ComponentCatalogueError(
            "decision_id must use repository-boundary:<stable-id> form"
        )
    return text


def _version(value: object, *, path: str) -> str:
    text = _string(value, path=path)
    if VERSION_RE.fullmatch(text) is None:
        raise ComponentCatalogueError(f"{path} must be an exact semantic version")
    return text


def _enum(enum_type: type[Enum], value: object, *, path: str) -> Any:
    if not isinstance(value, str):
        raise ComponentCatalogueError(f"{path} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ComponentCatalogueError(f"{path} has unsupported value: {value!r}") from exc


def _enum_tuple(enum_type: type[Enum], value: object, *, path: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise ComponentCatalogueError(f"{path} must be a list")
    result = tuple(_enum(enum_type, item, path=f"{path}[]") for item in value)
    if len(set(result)) != len(result):
        raise ComponentCatalogueError(f"{path} must not contain duplicates")
    return result


def _relative_path(value: object, *, path: str, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    text = _string(value, path=path)
    if (
        text.startswith("/")
        or "\\" in text
        or "//" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise ComponentCatalogueError(f"{path} must be a normalized relative POSIX path")
    return text


def _logical_repo(value: object, *, path: str) -> str:
    text = _stable_id(value, path=path)
    if "://" in text or "@" in text or ":" in text or "/" in text:
        raise ComponentCatalogueError(f"{path} must be a logical identity, not a remote")
    return text


def _rfc3339(value: object, *, path: str) -> str:
    text = _string(value, path=path)
    if RFC3339_RE.fullmatch(text) is None:
        raise ComponentCatalogueError(f"{path} must be RFC3339 with timezone")
    try:
        parsed = datetime.fromisoformat(
            text.removesuffix("Z") + ("+00:00" if text.endswith("Z") else "")
        )
    except ValueError as exc:
        raise ComponentCatalogueError(f"{path} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None:
        raise ComponentCatalogueError(f"{path} must include a timezone")
    return text


def _quantity(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComponentCatalogueError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ComponentCatalogueError(f"{path} must be finite and nonnegative")
    return result


def _load_yaml(path: Path) -> tuple[object, bytes]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ComponentCatalogueError(f"cannot stat record {path.name}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ComponentCatalogueError(f"record must not be a symlink: {path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise ComponentCatalogueError(f"record must be a regular file: {path.name}")
    if info.st_size > MAX_RECORD_BYTES:
        raise ComponentCatalogueError(
            f"record exceeds {MAX_RECORD_BYTES} bytes: {path.name}"
        )
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ComponentCatalogueError(f"cannot read UTF-8 record {path.name}: {exc}") from exc
    try:
        for token in yaml.scan(text):
            if isinstance(
                token,
                (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken),
            ):
                raise ComponentCatalogueError(
                    f"YAML anchors, aliases, and explicit tags are forbidden: {path.name}"
                )
        return yaml.load(text, Loader=_StrictLoader), raw
    except ComponentCatalogueError:
        raise
    except yaml.YAMLError as exc:
        raise ComponentCatalogueError(f"invalid YAML in {path.name}: {exc}") from exc


def _parse_provision(value: object, *, path: str) -> CapabilityProvision:
    item = _exact_mapping(value, path=path, keys=PROVISION_KEYS)
    return CapabilityProvision(
        capability=_stable_id(item["capability"], path=f"{path}.capability"),
        contract=_stable_id(item["contract"], path=f"{path}.contract"),
        schema=_stable_id(item["schema"], path=f"{path}.schema"),
        version=_version(item["version"], path=f"{path}.version"),
        features=tuple(
            _stable_id(feature, path=f"{path}.features[]")
            for feature in _string_tuple(item["features"], path=f"{path}.features")
        ),
    )


def _parse_requirement(
    value: object, *, path: str, optional: bool
) -> CapabilityRequirement:
    item = _exact_mapping(value, path=path, keys=REQUIREMENT_KEYS)
    enabled = item["enabled"]
    if type(enabled) is not bool:
        raise ComponentCatalogueError(f"{path}.enabled must be a boolean")
    feature = item["feature"]
    feature_id = None if feature is None else _stable_id(feature, path=f"{path}.feature")
    degradation = _nullable_string(item["degradation_ref"], path=f"{path}.degradation_ref")
    if not optional and (not enabled or degradation is not None):
        raise ComponentCatalogueError(
            f"{path} hard requirements must be enabled with null degradation_ref"
        )
    if optional and enabled and degradation is not None:
        raise ComponentCatalogueError(
            f"{path} enabled optional requirements must have null degradation_ref"
        )
    if optional and not enabled and degradation is None:
        raise ComponentCatalogueError(
            f"{path} disabled optional requirements require degradation_ref"
        )
    return CapabilityRequirement(
        capability=_stable_id(item["capability"], path=f"{path}.capability"),
        contract=_stable_id(item["contract"], path=f"{path}.contract"),
        schema=_stable_id(item["schema"], path=f"{path}.schema"),
        version=_version(item["version"], path=f"{path}.version"),
        feature=feature_id,
        enabled=enabled,
        degradation_ref=degradation,
    )


def _parse_acceptance(value: object, *, path: str) -> CompatibilityAcceptance:
    item = _exact_mapping(value, path=path, keys=ACCEPTS_KEYS)
    return CompatibilityAcceptance(
        capability=_stable_id(item["capability"], path=f"{path}.capability"),
        contract=_stable_id(item["contract"], path=f"{path}.contract"),
        schema=_stable_id(item["schema"], path=f"{path}.schema"),
        version=_version(item["version"], path=f"{path}.version"),
    )


def _parse_conflict(value: object, *, path: str) -> CompatibilityPredicate:
    item = _exact_mapping(value, path=path, keys=CONFLICT_KEYS)
    return CompatibilityPredicate(
        kind=_enum(ConflictKind, item["kind"], path=f"{path}.kind"),
        target=_stable_id(item["target"], path=f"{path}.target"),
        reason_ref=_string(item["reason_ref"], path=f"{path}.reason_ref"),
    )


def _parse_implications(value: object) -> ComponentImplications:
    mapping = _exact_mapping(value, path="implications", keys=IMPLICATION_CATEGORY_KEYS)
    result: dict[str, tuple[ComponentImplication, ...]] = {}
    seen: set[str] = set()
    for category in IMPLICATION_CATEGORY_KEYS:
        raw_items = mapping[category]
        if not isinstance(raw_items, list):
            raise ComponentCatalogueError(f"implications.{category} must be a list")
        parsed: list[ComponentImplication] = []
        for number, raw in enumerate(raw_items):
            path = f"implications.{category}[{number}]"
            item = _exact_mapping(raw, path=path, keys=IMPLICATION_KEYS)
            implication_id = _stable_id(
                item["implication_id"], path=f"{path}.implication_id"
            )
            if implication_id in seen:
                raise ComponentCatalogueError(
                    f"duplicate implication_id: {implication_id}"
                )
            seen.add(implication_id)
            parsed.append(
                ComponentImplication(
                    implication_id=implication_id,
                    scope=_string(item["scope"], path=f"{path}.scope"),
                    evidence_refs=_string_tuple(
                        item["evidence_refs"],
                        path=f"{path}.evidence_refs",
                        require_nonempty=True,
                    ),
                )
            )
        result[category] = tuple(parsed)
    return ComponentImplications(**result)


def _parse_cost(value: object, *, path: str) -> RecurringCost:
    item = _exact_mapping(value, path=path, keys=COST_KEYS)
    return RecurringCost(
        cost_id=_stable_id(item["cost_id"], path=f"{path}.cost_id"),
        category=_enum(CostCategory, item["category"], path=f"{path}.category"),
        quantity=_quantity(item["quantity"], path=f"{path}.quantity"),
        unit=_string(item["unit"], path=f"{path}.unit"),
        cadence=_enum(CostCadence, item["cadence"], path=f"{path}.cadence"),
        measurement_source=_string(
            item["measurement_source"], path=f"{path}.measurement_source"
        ),
        owner=_string(item["owner"], path=f"{path}.owner"),
        alternative_ref=_nullable_string(
            item["alternative_ref"], path=f"{path}.alternative_ref"
        ),
        last_measured_at=_rfc3339(
            item["last_measured_at"], path=f"{path}.last_measured_at"
        ),
        review_due_at=_rfc3339(
            item["review_due_at"], path=f"{path}.review_due_at"
        ),
    )


def _parse_component(data: object, *, filename_stem: str, raw: bytes) -> ComponentRecord:
    record = _exact_mapping(data, path="component record", keys=COMPONENT_RECORD_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ComponentCatalogueError(f"schema_version must equal {SCHEMA_VERSION}")
    component_id = _stable_id(record["component_id"], path="component_id")
    if component_id != filename_stem:
        raise ComponentCatalogueError(
            f"record filename {filename_stem!r} does not match component_id {component_id!r}"
        )
    source = _exact_mapping(record["source"], path="source", keys=SOURCE_KEYS)
    provides_raw = record["provides"]
    if not isinstance(provides_raw, list):
        raise ComponentCatalogueError("provides must be a list")
    provides = tuple(
        _parse_provision(value, path=f"provides[{number}]")
        for number, value in enumerate(provides_raw)
    )
    if len(set(provides)) != len(provides):
        raise ComponentCatalogueError("provides must not contain duplicates")
    requires = _exact_mapping(record["requires"], path="requires", keys=REQUIRES_KEYS)
    requirement_groups: dict[str, tuple[CapabilityRequirement, ...]] = {}
    for group in REQUIRES_KEYS:
        raw_group = requires[group]
        if not isinstance(raw_group, list):
            raise ComponentCatalogueError(f"requires.{group} must be a list")
        parsed = tuple(
            _parse_requirement(
                value,
                path=f"requires.{group}[{number}]",
                optional=group == "optional",
            )
            for number, value in enumerate(raw_group)
        )
        if len(set(parsed)) != len(parsed):
            raise ComponentCatalogueError(f"requires.{group} must not contain duplicates")
        requirement_groups[group] = parsed
    compatibility = _exact_mapping(
        record["compatibility"], path="compatibility", keys=COMPATIBILITY_KEYS
    )
    accepts_raw = compatibility["accepts"]
    conflicts_raw = compatibility["conflicts"]
    if not isinstance(accepts_raw, list) or not isinstance(conflicts_raw, list):
        raise ComponentCatalogueError("compatibility accepts/conflicts must be lists")
    accepts = tuple(
        _parse_acceptance(value, path=f"compatibility.accepts[{number}]")
        for number, value in enumerate(accepts_raw)
    )
    conflicts = tuple(
        _parse_conflict(value, path=f"compatibility.conflicts[{number}]")
        for number, value in enumerate(conflicts_raw)
    )
    if len(set(accepts)) != len(accepts) or len(set(conflicts)) != len(conflicts):
        raise ComponentCatalogueError("compatibility predicates must not contain duplicates")
    deployment = _exact_mapping(
        record["deployment"], path="deployment", keys=DEPLOYMENT_KEYS
    )
    upkeep = _exact_mapping(record["upkeep"], path="upkeep", keys=UPKEEP_KEYS)
    costs_raw = upkeep["cost"]
    if not isinstance(costs_raw, list):
        raise ComponentCatalogueError("upkeep.cost must be a list")
    maturity = _exact_mapping(record["maturity"], path="maturity", keys=MATURITY_KEYS)
    tensions_raw = record["tensions"]
    if not isinstance(tensions_raw, list):
        raise ComponentCatalogueError("tensions must be a list")
    tensions: list[ComponentTension] = []
    for number, raw_tension in enumerate(tensions_raw):
        path = f"tensions[{number}]"
        tension = _exact_mapping(raw_tension, path=path, keys=TENSION_KEYS)
        tensions.append(
            ComponentTension(
                tension_id=_stable_id(tension["tension_id"], path=f"{path}.tension_id"),
                pole_a=_string(tension["pole_a"], path=f"{path}.pole_a"),
                pole_b=_string(tension["pole_b"], path=f"{path}.pole_b"),
                rebalance_trigger=_string(
                    tension["rebalance_trigger"], path=f"{path}.rebalance_trigger"
                ),
            )
        )
    if len({item.tension_id for item in tensions}) != len(tensions):
        raise ComponentCatalogueError("tensions must have unique tension_id values")
    return ComponentRecord(
        schema_version=SCHEMA_VERSION,
        component_id=component_id,
        project_id=_stable_id(record["project_id"], path="project_id"),
        kind=_enum(ComponentKind, record["kind"], path="kind"),
        source=ComponentSource(
            logical_repo=_logical_repo(source["logical_repo"], path="source.logical_repo"),
            path=_relative_path(source["path"], path="source.path", nullable=True),
            artifact=_nullable_string(source["artifact"], path="source.artifact"),
        ),
        provides=provides,
        hard_requirements=requirement_groups["hard"],
        optional_requirements=requirement_groups["optional"],
        compatibility=ComponentCompatibility(
            accepts=accepts,
            conflicts=conflicts,
            tested_revision=_string(
                compatibility["tested_revision"], path="compatibility.tested_revision"
            ),
        ),
        deployment=ComponentDeployment(
            loci=_string_tuple(deployment["loci"], path="deployment.loci"),
            trust_zone=_string(deployment["trust_zone"], path="deployment.trust_zone"),
        ),
        implications=_parse_implications(record["implications"]),
        upkeep=ComponentUpkeep(
            steward=_string(upkeep["steward"], path="upkeep.steward"),
            health_evidence=_string_tuple(
                upkeep["health_evidence"], path="upkeep.health_evidence"
            ),
            monitoring=_string_tuple(upkeep["monitoring"], path="upkeep.monitoring"),
            upgrade_rollback=_string_tuple(
                upkeep["upgrade_rollback"], path="upkeep.upgrade_rollback"
            ),
            credential_rotation=_string_tuple(
                upkeep["credential_rotation"], path="upkeep.credential_rotation"
            ),
            storage_retention=_string_tuple(
                upkeep["storage_retention"], path="upkeep.storage_retention"
            ),
            cost=tuple(
                _parse_cost(value, path=f"upkeep.cost[{number}]")
                for number, value in enumerate(costs_raw)
            ),
            review_obligations=_string_tuple(
                upkeep["review_obligations"], path="upkeep.review_obligations"
            ),
        ),
        maturity=ComponentMaturity(
            developmental_mode=_enum(
                DevelopmentalMode,
                maturity["developmental_mode"],
                path="maturity.developmental_mode",
            ),
            replaceability=_enum(
                Replaceability,
                maturity["replaceability"],
                path="maturity.replaceability",
            ),
        ),
        tensions=tuple(tensions),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_component_records(records_root: Path) -> dict[str, ComponentRecord]:
    """Load strict canonical component records from *records_root*."""
    if not records_root.exists():
        return {}
    if records_root.is_symlink() or not records_root.is_dir():
        raise ComponentCatalogueError("components/records must be a real directory")
    records: dict[str, ComponentRecord] = {}
    for path in sorted(records_root.glob("*.yaml"), key=lambda item: item.name):
        data, raw = _load_yaml(path)
        parsed = _parse_component(data, filename_stem=path.stem, raw=raw)
        if parsed.component_id in records:
            raise ComponentCatalogueError(f"duplicate component_id: {parsed.component_id}")
        records[parsed.component_id] = parsed
    return records


def _matching_providers(
    requirement: CapabilityRequirement, records: Mapping[str, ComponentRecord]
) -> tuple[str, ...]:
    matches: set[str] = set()
    for component_id, record in records.items():
        for provision in record.provides:
            if (
                provision.capability == requirement.capability
                and provision.contract == requirement.contract
                and provision.schema == requirement.schema
                and provision.version == requirement.version
                and (
                    requirement.feature is None
                    or requirement.feature in provision.features
                )
            ):
                matches.add(component_id)
    return tuple(sorted(matches))


def validate_component_graph(
    records: Mapping[str, ComponentRecord],
    project_ids: AbstractSet[str],
    obligation_ids: AbstractSet[str],
) -> tuple[ComponentIssue, ...]:
    """Validate identities, exact dependencies, conflicts, and owned upkeep."""
    issues: list[ComponentIssue] = []
    edges: dict[str, set[str]] = {component_id: set() for component_id in records}
    all_capabilities = {
        provision.capability for record in records.values() for provision in record.provides
    }
    all_features = {
        feature
        for record in records.values()
        for provision in record.provides
        for feature in provision.features
    }
    all_features.update(
        requirement.feature
        for record in records.values()
        for requirement in record.hard_requirements + record.optional_requirements
        if requirement.enabled and requirement.feature is not None
    )
    for component_id in sorted(records):
        record = records[component_id]
        if component_id != record.component_id:
            issues.append(
                ComponentIssue(
                    "identity-key-mismatch",
                    component_id,
                    f"mapping key carries record {record.component_id!r}",
                )
            )
        if record.project_id not in project_ids:
            issues.append(
                ComponentIssue(
                    "unknown-project", component_id, f"unknown project_id {record.project_id!r}"
                )
            )
        for requirement in record.hard_requirements + record.optional_requirements:
            if not requirement.enabled:
                continue
            providers = _matching_providers(requirement, records)
            detail = (
                f"{requirement.capability}/{requirement.contract}/"
                f"{requirement.schema}/{requirement.version} providers={providers!r}"
            )
            if not providers:
                issues.append(ComponentIssue("unknown-provider", component_id, detail))
            elif len(providers) != 1:
                issues.append(ComponentIssue("ambiguous-provider", component_id, detail))
            elif providers[0] == component_id:
                issues.append(ComponentIssue("self-requirement", component_id, detail))
            else:
                edges.setdefault(component_id, set()).add(providers[0])
        for conflict in record.compatibility.conflicts:
            present = (
                conflict.kind is ConflictKind.COMPONENT and conflict.target in records
            ) or (
                conflict.kind is ConflictKind.CAPABILITY
                and conflict.target in all_capabilities
            ) or (
                conflict.kind is ConflictKind.FEATURE and conflict.target in all_features
            )
            if present:
                issues.append(
                    ComponentIssue(
                        "active-conflict",
                        component_id,
                        f"{conflict.kind.value} {conflict.target!r}: {conflict.reason_ref}",
                    )
                )
        seen_costs: set[str] = set()
        for cost in record.upkeep.cost:
            if cost.cost_id in seen_costs:
                issues.append(
                    ComponentIssue(
                        "duplicate-cost-id", component_id, f"duplicate {cost.cost_id!r}"
                    )
                )
            seen_costs.add(cost.cost_id)
        for obligation_id in record.upkeep.review_obligations:
            if obligation_id not in obligation_ids:
                issues.append(
                    ComponentIssue(
                        "unknown-obligation",
                        component_id,
                        f"unknown ORG-061 obligation {obligation_id!r}",
                    )
                )

    cycle_members: set[str] = set()

    def visit(node: str, path: list[str], positions: dict[str, int]) -> None:
        if node in positions:
            cycle_members.update(path[positions[node] :])
            return
        positions[node] = len(path)
        path.append(node)
        for target in sorted(edges.get(node, ())):
            visit(target, path, positions)
        path.pop()
        positions.pop(node, None)

    for component_id in sorted(records):
        visit(component_id, [], {})
    for component_id in sorted(cycle_members):
        issues.append(
            ComponentIssue(
                "dependency-cycle", component_id, "active component dependencies contain a cycle"
            )
        )
    return tuple(
        sorted(issues, key=lambda issue: (issue.component_id, issue.code, issue.detail))
    )


def _parse_boundary(data: object) -> RepositoryBoundaryDecision:
    decision = _exact_mapping(
        data, path="repository boundary decision", keys=BOUNDARY_DECISION_KEYS
    )
    if type(decision["schema_version"]) is not int or decision["schema_version"] != SCHEMA_VERSION:
        raise ComponentCatalogueError(f"schema_version must equal {SCHEMA_VERSION}")
    source = _exact_mapping(
        decision["current_source"], path="current_source", keys=BOUNDARY_SOURCE_KEYS
    )
    contract = _exact_mapping(
        decision["contract"], path="contract", keys=BOUNDARY_CONTRACT_KEYS
    )
    migration = _exact_mapping(
        decision["migration"], path="migration", keys=MIGRATION_KEYS
    )
    operation = _exact_mapping(
        decision["operational_contract"],
        path="operational_contract",
        keys=BOUNDARY_OPERATION_KEYS,
    )
    impact = _exact_mapping(
        decision["compatibility_impact"],
        path="compatibility_impact",
        keys=COMPATIBILITY_IMPACT_KEYS,
    )
    authority = _exact_mapping(
        decision["authority"], path="authority", keys=AUTHORITY_KEYS
    )
    result = RepositoryBoundaryDecision(
        schema_version=SCHEMA_VERSION,
        decision_id=_decision_id(decision["decision_id"]),
        project_id=_stable_id(decision["project_id"], path="project_id"),
        component_id=_stable_id(decision["component_id"], path="component_id"),
        current_source=BoundarySource(
            logical_repo=_logical_repo(
                source["logical_repo"], path="current_source.logical_repo"
            ),
            path=_relative_path(
                source["path"], path="current_source.path", nullable=False
            )
            or "",
        ),
        contract=BoundaryContract(
            id=_stable_id(contract["id"], path="contract.id"),
            version=_version(contract["version"], path="contract.version"),
            schema_refs=_string_tuple(contract["schema_refs"], path="contract.schema_refs"),
            test_evidence_refs=_string_tuple(
                contract["test_evidence_refs"], path="contract.test_evidence_refs"
            ),
        ),
        independence_signals=_enum_tuple(
            IndependenceSignal,
            decision["independence_signals"],
            path="independence_signals",
        ),
        keep_together_reasons=_enum_tuple(
            KeepTogetherReason,
            decision["keep_together_reasons"],
            path="keep_together_reasons",
        ),
        decision=_enum(BoundaryDecisionKind, decision["decision"], path="decision"),
        migration=BoundaryMigration(
            **{
                key: _string_tuple(migration[key], path=f"migration.{key}")
                for key in MIGRATION_KEYS
            }
        ),
        operational_contract=BoundaryOperationalContract(
            **{
                key: _string_tuple(operation[key], path=f"operational_contract.{key}")
                for key in BOUNDARY_OPERATION_KEYS
            }
        ),
        compatibility_impact=BoundaryCompatibilityImpact(
            affected_assemblies=_string_tuple(
                impact["affected_assemblies"],
                path="compatibility_impact.affected_assemblies",
            ),
            catalogue_changes=_string_tuple(
                impact["catalogue_changes"], path="compatibility_impact.catalogue_changes"
            ),
        ),
        implication_acknowledgements=_string_tuple(
            decision["implication_acknowledgements"], path="implication_acknowledgements"
        ),
        upkeep_estimate_refs=_string_tuple(
            decision["upkeep_estimate_refs"], path="upkeep_estimate_refs"
        ),
        authority=BoundaryAuthority(
            decision_ref=_string(authority["decision_ref"], path="authority.decision_ref"),
            reviewer_evidence_refs=_string_tuple(
                authority["reviewer_evidence_refs"],
                path="authority.reviewer_evidence_refs",
            ),
        ),
        re_evaluate_when=_string_tuple(
            decision["re_evaluate_when"], path="re_evaluate_when"
        ),
    )
    if result.decision is BoundaryDecisionKind.EXTRACT:
        required_nonempty: tuple[tuple[str, Sequence[object]], ...] = (
            ("contract.schema_refs", result.contract.schema_refs),
            ("contract.test_evidence_refs", result.contract.test_evidence_refs),
            ("independence_signals", result.independence_signals),
            *((f"migration.{key}", getattr(result.migration, key)) for key in MIGRATION_KEYS),
            *((f"operational_contract.{key}", getattr(result.operational_contract, key)) for key in BOUNDARY_OPERATION_KEYS),
            ("compatibility_impact.affected_assemblies", result.compatibility_impact.affected_assemblies),
            ("compatibility_impact.catalogue_changes", result.compatibility_impact.catalogue_changes),
            ("implication_acknowledgements", result.implication_acknowledgements),
            ("upkeep_estimate_refs", result.upkeep_estimate_refs),
            ("authority.reviewer_evidence_refs", result.authority.reviewer_evidence_refs),
            ("re_evaluate_when", result.re_evaluate_when),
        )
        for path, values in required_nonempty:
            if not values:
                raise ComponentCatalogueError(f"extract requires nonempty {path}")
    elif not result.keep_together_reasons:
        raise ComponentCatalogueError("keep-together requires a keep_together_reason")
    return result


def parse_repository_boundary_decision(
    path: Path,
    *,
    project_ids: AbstractSet[str],
    component_ids: AbstractSet[str],
) -> RepositoryBoundaryDecision:
    """Parse one extraction/keep-together artifact without creating a registry."""
    data, _raw = _load_yaml(path)
    result = _parse_boundary(data)
    if result.project_id not in project_ids:
        raise ComponentCatalogueError(
            f"repository boundary decision has unknown project_id {result.project_id!r}"
        )
    if result.component_id not in component_ids:
        raise ComponentCatalogueError(
            f"repository boundary decision has unknown component_id {result.component_id!r}"
        )
    return result


def _array_schema(item: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "array",
        "items": dict(item or {"type": "string", "minLength": 1}),
        "uniqueItems": True,
    }


def _object_schema(
    keys: Sequence[str], properties: Mapping[str, object]
) -> dict[str, object]:
    return {
        "type": "object",
        "required": list(keys),
        "additionalProperties": False,
        "properties": dict(properties),
    }


def _nullable_string_schema() -> dict[str, object]:
    return {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]}


def _component_record_schema() -> dict[str, object]:
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    text = {"type": "string", "minLength": 1}
    version = {"type": "string", "pattern": VERSION_PATTERN}
    provision = _object_schema(
        PROVISION_KEYS,
        {
            "capability": stable,
            "contract": stable,
            "schema": stable,
            "version": version,
            "features": _array_schema(stable),
        },
    )
    requirement = _object_schema(
        REQUIREMENT_KEYS,
        {
            "capability": stable,
            "contract": stable,
            "schema": stable,
            "version": version,
            "feature": {"anyOf": [{"type": "null"}, stable]},
            "enabled": {"type": "boolean"},
            "degradation_ref": _nullable_string_schema(),
        },
    )
    acceptance = _object_schema(
        ACCEPTS_KEYS,
        {"capability": stable, "contract": stable, "schema": stable, "version": version},
    )
    conflict = _object_schema(
        CONFLICT_KEYS,
        {
            "kind": {"type": "string", "enum": [item.value for item in ConflictKind]},
            "target": stable,
            "reason_ref": text,
        },
    )
    implication = _object_schema(
        IMPLICATION_KEYS,
        {"implication_id": stable, "scope": text, "evidence_refs": _array_schema()},
    )
    cost = _object_schema(
        COST_KEYS,
        {
            "cost_id": stable,
            "category": {"type": "string", "enum": [item.value for item in CostCategory]},
            "quantity": {"type": "number", "minimum": 0},
            "unit": text,
            "cadence": {"type": "string", "enum": [item.value for item in CostCadence]},
            "measurement_source": text,
            "owner": text,
            "alternative_ref": _nullable_string_schema(),
            "last_measured_at": {"type": "string", "format": "date-time"},
            "review_due_at": {"type": "string", "format": "date-time"},
        },
    )
    tension = _object_schema(
        TENSION_KEYS,
        {"tension_id": stable, "pole_a": text, "pole_b": text, "rebalance_trigger": text},
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/component-record.schema.json",
        "title": "Pantheon ORG-055 component record",
        "type": "object",
        "required": list(COMPONENT_RECORD_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "component_id": stable,
            "project_id": stable,
            "kind": {"type": "string", "enum": [item.value for item in ComponentKind]},
            "source": _object_schema(
                SOURCE_KEYS,
                {
                    "logical_repo": stable,
                    "path": _nullable_string_schema(),
                    "artifact": _nullable_string_schema(),
                },
            ),
            "provides": _array_schema(provision),
            "requires": _object_schema(
                REQUIRES_KEYS,
                {"hard": _array_schema(requirement), "optional": _array_schema(requirement)},
            ),
            "compatibility": _object_schema(
                COMPATIBILITY_KEYS,
                {
                    "accepts": _array_schema(acceptance),
                    "conflicts": _array_schema(conflict),
                    "tested_revision": text,
                },
            ),
            "deployment": _object_schema(
                DEPLOYMENT_KEYS, {"loci": _array_schema(), "trust_zone": text}
            ),
            "implications": _object_schema(
                IMPLICATION_CATEGORY_KEYS,
                {key: _array_schema(implication) for key in IMPLICATION_CATEGORY_KEYS},
            ),
            "upkeep": _object_schema(
                UPKEEP_KEYS,
                {
                    "steward": text,
                    "health_evidence": _array_schema(),
                    "monitoring": _array_schema(),
                    "upgrade_rollback": _array_schema(),
                    "credential_rotation": _array_schema(),
                    "storage_retention": _array_schema(),
                    "cost": _array_schema(cost),
                    "review_obligations": _array_schema(),
                },
            ),
            "maturity": _object_schema(
                MATURITY_KEYS,
                {
                    "developmental_mode": {
                        "type": "string",
                        "enum": [item.value for item in DevelopmentalMode],
                    },
                    "replaceability": {
                        "type": "string",
                        "enum": [item.value for item in Replaceability],
                    },
                },
            ),
            "tensions": _array_schema(tension),
        },
    }


def _boundary_decision_schema() -> dict[str, object]:
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    text = {"type": "string", "minLength": 1}
    string_array = _array_schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/repository-boundary-decision.schema.json",
        "title": "Pantheon ORG-055 repository boundary decision",
        "type": "object",
        "required": list(BOUNDARY_DECISION_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "decision_id": {"type": "string", "pattern": DECISION_ID_PATTERN},
            "project_id": stable,
            "component_id": stable,
            "current_source": _object_schema(
                BOUNDARY_SOURCE_KEYS, {"logical_repo": stable, "path": text}
            ),
            "contract": _object_schema(
                BOUNDARY_CONTRACT_KEYS,
                {
                    "id": stable,
                    "version": {"type": "string", "pattern": VERSION_PATTERN},
                    "schema_refs": string_array(),
                    "test_evidence_refs": string_array(),
                },
            ),
            "independence_signals": string_array(
                {"type": "string", "enum": [item.value for item in IndependenceSignal]}
            ),
            "keep_together_reasons": string_array(
                {"type": "string", "enum": [item.value for item in KeepTogetherReason]}
            ),
            "decision": {
                "type": "string",
                "enum": [item.value for item in BoundaryDecisionKind],
            },
            "migration": _object_schema(
                MIGRATION_KEYS, {key: string_array() for key in MIGRATION_KEYS}
            ),
            "operational_contract": _object_schema(
                BOUNDARY_OPERATION_KEYS,
                {key: string_array() for key in BOUNDARY_OPERATION_KEYS},
            ),
            "compatibility_impact": _object_schema(
                COMPATIBILITY_IMPACT_KEYS,
                {key: string_array() for key in COMPATIBILITY_IMPACT_KEYS},
            ),
            "implication_acknowledgements": string_array(),
            "upkeep_estimate_refs": string_array(),
            "authority": _object_schema(
                AUTHORITY_KEYS,
                {"decision_ref": text, "reviewer_evidence_refs": string_array()},
            ),
            "re_evaluate_when": string_array(),
        },
    }


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def generate_component_record_schema() -> bytes:
    return _json_bytes(_component_record_schema())


def generate_repository_boundary_decision_schema() -> bytes:
    return _json_bytes(_boundary_decision_schema())


def generate_component_index(records: Mapping[str, ComponentRecord]) -> bytes:
    semantic = {
        component_id: records[component_id].source_sha256
        for component_id in sorted(records)
    }
    digest = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_from_digest": f"sha256:{digest}",
            "components": {
                component_id: {
                    "record": f"components/records/{component_id}.yaml",
                    "project_ref": f"project:{records[component_id].project_id}",
                    "kind": records[component_id].kind.value,
                    "logical_repo": records[component_id].source.logical_repo,
                    "provided_capabilities": sorted(
                        {item.capability for item in records[component_id].provides}
                    ),
                    "record_sha256": f"sha256:{records[component_id].source_sha256}",
                }
                for component_id in sorted(records)
            },
        }
    )


def _authority_ids(repo_root: Path) -> tuple[frozenset[str], frozenset[str]]:
    projects = project_catalogue.load_project_records(repo_root / "projects" / "records")
    obligation_path = repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
    obligations = (
        grand_project_program.load_obligation_index(obligation_path)
        if obligation_path.is_file()
        else ()
    )
    return frozenset(projects), frozenset(item.obligation_id for item in obligations)


def check_component_catalogue(repo_root: Path) -> tuple[ComponentIssue, ...]:
    try:
        records = load_component_records(repo_root / "components" / "records")
        project_ids, obligation_ids = _authority_ids(repo_root)
    except (ComponentCatalogueError, project_catalogue.ProjectCatalogueError, ValueError) as exc:
        return (ComponentIssue("catalogue-invalid", "*", str(exc)),)
    issues = list(validate_component_graph(records, project_ids, obligation_ids))
    expected_schemas = {
        repo_root / "schemas" / "component-record.schema.json": generate_component_record_schema(),
        repo_root
        / "schemas"
        / "repository-boundary-decision.schema.json": generate_repository_boundary_decision_schema(),
    }
    for path, expected in expected_schemas.items():
        try:
            actual = path.read_bytes()
        except OSError:
            actual = None
        if actual != expected:
            issues.append(
                ComponentIssue("schema-stale", "*", f"generated schema differs: {path.name}")
            )
    if not issues:
        expected_index = generate_component_index(records)
        try:
            actual_index = (repo_root / "components" / "index.json").read_bytes()
        except OSError:
            actual_index = None
        if actual_index != expected_index:
            issues.append(
                ComponentIssue("index-stale", "*", "generated component index bytes differ")
            )
    return tuple(
        sorted(issues, key=lambda issue: (issue.component_id, issue.code, issue.detail))
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "generate"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            records = load_component_records(args.repo_root / "components" / "records")
            project_ids, obligation_ids = _authority_ids(args.repo_root)
            issues = validate_component_graph(records, project_ids, obligation_ids)
            if issues:
                raise ComponentCatalogueError(f"invalid component graph: {issues!r}")
            _atomic_write(
                args.repo_root / "schemas" / "component-record.schema.json",
                generate_component_record_schema(),
            )
            _atomic_write(
                args.repo_root / "schemas" / "repository-boundary-decision.schema.json",
                generate_repository_boundary_decision_schema(),
            )
            _atomic_write(
                args.repo_root / "components" / "index.json",
                generate_component_index(records),
            )
            return 0
        issues = check_component_catalogue(args.repo_root)
    except (ComponentCatalogueError, project_catalogue.ProjectCatalogueError, ValueError) as exc:
        print(f"component-catalogue-invalid: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        print(
            f"{issue.code}: {issue.component_id}: {issue.detail}",
            file=sys.stderr,
        )
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
