"""Strict ORG-054 project-record catalogue and generated index.

Canonical project records are hand-curated YAML.  The JSON schema and lookup
index are generated artifacts and are accepted only when their bytes exactly
match this module's typed contract.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import AbstractSet, Any

import yaml


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 256 * 1024
PROJECT_ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
PARENT_REF_PATTERN = (
    r"^(?:goal:[a-z][a-z0-9]*(?:-[a-z0-9]+)*|"
    r"project:[a-z][a-z0-9]*(?:-[a-z0-9]+)*)$"
)
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)
PARENT_REF_RE = re.compile(PARENT_REF_PATTERN)

PROJECT_RECORD_KEYS = (
    "schema_version",
    "project_id",
    "telos",
    "parent_goal_ref",
    "classification",
    "boundary",
    "interfaces",
    "stewardship",
    "lifecycle",
    "evidence",
    "tensions",
    "operational_contract",
)
BOUNDARY_KEYS = ("inside", "outside", "not_owned")
INTERFACE_KEYS = ("provides", "requires")
STEWARDSHIP_KEYS = ("role", "authority_surface")
PROJECT_LIFECYCLE_KEYS = ("developmental_mode", "formed_at", "transition_refs")
EVIDENCE_KEYS = ("health", "user_value", "safety", "developmental")
TENSION_KEYS = ("pole_a", "pole_b", "rebalance_trigger")
OPERATIONAL_CONTRACT_KEYS = ("obligations", "lifecycle")
OPERATIONAL_OBLIGATION_KEYS = (
    "deployment",
    "data",
    "security",
    "cost",
    "maintenance",
    "recovery",
)
LIFECYCLE_COVERAGE_KEYS = (
    "install",
    "configure",
    "health_monitor",
    "update",
    "remove",
)


class ProjectCatalogueError(ValueError):
    """A project record or generated catalogue cannot be accepted."""


class DevelopmentalMode(str, Enum):
    SHAPING = "shaping"
    INFOLDING = "infolding"
    INFILLING = "infilling"
    OPERATING = "operating"
    REORIENTING = "reorienting"
    RETIRING = "retiring"
    SPLITTING = "splitting"
    MERGING = "merging"


class ProjectClassification(str, Enum):
    ORGAN = "organ"
    SUPPORTING_INFRASTRUCTURE = "supporting-infrastructure"
    EXTERNAL_SERVED = "external-served"
    INDEPENDENT = "independent"


@dataclass(frozen=True)
class ProjectIssue:
    code: str
    project_id: str
    detail: str


@dataclass(frozen=True)
class ProjectTension:
    pole_a: str
    pole_b: str
    rebalance_trigger: str


@dataclass(frozen=True)
class ProjectBoundary:
    inside: tuple[str, ...]
    outside: tuple[str, ...]
    not_owned: tuple[str, ...]


@dataclass(frozen=True)
class ProjectInterfaces:
    provides: tuple[str, ...]
    requires: tuple[str, ...]


@dataclass(frozen=True)
class ProjectStewardship:
    role: str
    authority_surface: str


@dataclass(frozen=True)
class ProjectLifecycle:
    developmental_mode: DevelopmentalMode
    formed_at: str
    transition_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectEvidence:
    health: tuple[str, ...]
    user_value: tuple[str, ...]
    safety: tuple[str, ...]
    developmental: tuple[str, ...]


@dataclass(frozen=True)
class OperationalObligations:
    deployment: tuple[str, ...]
    data: tuple[str, ...]
    security: tuple[str, ...]
    cost: tuple[str, ...]
    maintenance: tuple[str, ...]
    recovery: tuple[str, ...]

    @property
    def refs(self) -> frozenset[str]:
        return frozenset(
            self.deployment
            + self.data
            + self.security
            + self.cost
            + self.maintenance
            + self.recovery
        )


@dataclass(frozen=True)
class LifecycleCoverage:
    install: tuple[str, ...]
    configure: tuple[str, ...]
    health_monitor: tuple[str, ...]
    update: tuple[str, ...]
    remove: tuple[str, ...]

    @property
    def gaps(self) -> tuple[str, ...]:
        """Return lifecycle stages with no evidence; emptiness never means done."""
        return tuple(key for key in LIFECYCLE_COVERAGE_KEYS if not getattr(self, key))


@dataclass(frozen=True)
class OperationalContract:
    obligations: OperationalObligations
    lifecycle: LifecycleCoverage


@dataclass(frozen=True)
class ProjectRecord:
    schema_version: int
    project_id: str
    telos: str
    parent_goal_ref: str | None
    classification: ProjectClassification
    boundary: ProjectBoundary
    interfaces: ProjectInterfaces
    stewardship: ProjectStewardship
    lifecycle: ProjectLifecycle
    evidence: ProjectEvidence
    tensions: tuple[ProjectTension, ...]
    operational_contract: OperationalContract


class _StrictLoader(yaml.SafeLoader):
    pass


# Keep RFC3339 timestamps as strings.  The JSON Schema and typed parser both
# define formed_at as a string, so YAML's convenience conversion is ambiguity.
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
        raise ProjectCatalogueError("YAML mapping required")
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ProjectCatalogueError("YAML mapping keys must be strings")
        if key == "<<":
            raise ProjectCatalogueError("YAML merge keys are forbidden")
        if key in seen:
            raise ProjectCatalogueError(f"duplicate YAML key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_construct_mapping
)


def _exact_mapping(value: object, *, path: str, keys: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProjectCatalogueError(f"{path} must be an object")
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ProjectCatalogueError(
            f"{path} keys differ: missing={missing!r} unknown={unknown!r}"
        )
    if any(not isinstance(key, str) for key in value):
        raise ProjectCatalogueError(f"{path} keys must be strings")
    return value


def _string(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProjectCatalogueError(f"{path} must be a non-empty trimmed string")
    return value


def _string_tuple(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProjectCatalogueError(f"{path} must be a list")
    items = tuple(_string(item, path=f"{path}[]") for item in value)
    if len(set(items)) != len(items):
        raise ProjectCatalogueError(f"{path} must not contain duplicate refs")
    return items


def _enum(enum_type: type[Enum], value: object, *, path: str) -> Any:
    if not isinstance(value, str):
        raise ProjectCatalogueError(f"{path} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ProjectCatalogueError(f"{path} has unsupported value: {value!r}") from exc


def _formed_at(value: object) -> str:
    text = _string(value, path="lifecycle.formed_at")
    if RFC3339_PATTERN.fullmatch(text) is None:
        raise ProjectCatalogueError("lifecycle.formed_at must be RFC3339")
    try:
        parsed = datetime.fromisoformat(text.removesuffix("Z") + ("+00:00" if text.endswith("Z") else ""))
    except ValueError as exc:
        raise ProjectCatalogueError("lifecycle.formed_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ProjectCatalogueError("lifecycle.formed_at must include a timezone")
    return text


def _parse_record(data: object, *, filename_stem: str) -> ProjectRecord:
    record = _exact_mapping(data, path="project record", keys=PROJECT_RECORD_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ProjectCatalogueError(f"schema_version must equal {SCHEMA_VERSION}")
    project_id = _string(record["project_id"], path="project_id")
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ProjectCatalogueError("project_id must be stable lowercase kebab form")
    if project_id != filename_stem:
        raise ProjectCatalogueError(
            f"record filename {filename_stem!r} does not match project_id {project_id!r}"
        )
    parent = record["parent_goal_ref"]
    if parent is not None and not isinstance(parent, str):
        raise ProjectCatalogueError("parent_goal_ref must be null or a typed string ref")

    boundary = _exact_mapping(record["boundary"], path="boundary", keys=BOUNDARY_KEYS)
    interfaces = _exact_mapping(record["interfaces"], path="interfaces", keys=INTERFACE_KEYS)
    stewardship = _exact_mapping(
        record["stewardship"], path="stewardship", keys=STEWARDSHIP_KEYS
    )
    lifecycle = _exact_mapping(
        record["lifecycle"], path="lifecycle", keys=PROJECT_LIFECYCLE_KEYS
    )
    evidence = _exact_mapping(record["evidence"], path="evidence", keys=EVIDENCE_KEYS)
    tensions_value = record["tensions"]
    if not isinstance(tensions_value, list):
        raise ProjectCatalogueError("tensions must be a list")
    tensions: list[ProjectTension] = []
    for number, value in enumerate(tensions_value):
        item = _exact_mapping(value, path=f"tensions[{number}]", keys=TENSION_KEYS)
        tensions.append(
            ProjectTension(
                pole_a=_string(item["pole_a"], path=f"tensions[{number}].pole_a"),
                pole_b=_string(item["pole_b"], path=f"tensions[{number}].pole_b"),
                rebalance_trigger=_string(
                    item["rebalance_trigger"],
                    path=f"tensions[{number}].rebalance_trigger",
                ),
            )
        )

    contract = _exact_mapping(
        record["operational_contract"],
        path="operational_contract",
        keys=OPERATIONAL_CONTRACT_KEYS,
    )
    obligations = _exact_mapping(
        contract["obligations"],
        path="operational_contract.obligations",
        keys=OPERATIONAL_OBLIGATION_KEYS,
    )
    coverage = _exact_mapping(
        contract["lifecycle"],
        path="operational_contract.lifecycle",
        keys=LIFECYCLE_COVERAGE_KEYS,
    )
    return ProjectRecord(
        schema_version=SCHEMA_VERSION,
        project_id=project_id,
        telos=_string(record["telos"], path="telos"),
        parent_goal_ref=parent,
        classification=_enum(
            ProjectClassification, record["classification"], path="classification"
        ),
        boundary=ProjectBoundary(
            inside=_string_tuple(boundary["inside"], path="boundary.inside"),
            outside=_string_tuple(boundary["outside"], path="boundary.outside"),
            not_owned=_string_tuple(boundary["not_owned"], path="boundary.not_owned"),
        ),
        interfaces=ProjectInterfaces(
            provides=_string_tuple(interfaces["provides"], path="interfaces.provides"),
            requires=_string_tuple(interfaces["requires"], path="interfaces.requires"),
        ),
        stewardship=ProjectStewardship(
            role=_string(stewardship["role"], path="stewardship.role"),
            authority_surface=_string(
                stewardship["authority_surface"], path="stewardship.authority_surface"
            ),
        ),
        lifecycle=ProjectLifecycle(
            developmental_mode=_enum(
                DevelopmentalMode,
                lifecycle["developmental_mode"],
                path="lifecycle.developmental_mode",
            ),
            formed_at=_formed_at(lifecycle["formed_at"]),
            transition_refs=_string_tuple(
                lifecycle["transition_refs"], path="lifecycle.transition_refs"
            ),
        ),
        evidence=ProjectEvidence(
            health=_string_tuple(evidence["health"], path="evidence.health"),
            user_value=_string_tuple(evidence["user_value"], path="evidence.user_value"),
            safety=_string_tuple(evidence["safety"], path="evidence.safety"),
            developmental=_string_tuple(
                evidence["developmental"], path="evidence.developmental"
            ),
        ),
        tensions=tuple(tensions),
        operational_contract=OperationalContract(
            obligations=OperationalObligations(
                **{
                    key: _string_tuple(
                        obligations[key], path=f"operational_contract.obligations.{key}"
                    )
                    for key in OPERATIONAL_OBLIGATION_KEYS
                }
            ),
            lifecycle=LifecycleCoverage(
                **{
                    key: _string_tuple(
                        coverage[key], path=f"operational_contract.lifecycle.{key}"
                    )
                    for key in LIFECYCLE_COVERAGE_KEYS
                }
            ),
        ),
    )


def _load_yaml(path: Path) -> object:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProjectCatalogueError(f"cannot stat record {path.name}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ProjectCatalogueError(f"record must not be a symlink: {path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise ProjectCatalogueError(f"record must be a regular file: {path.name}")
    if info.st_size > MAX_RECORD_BYTES:
        raise ProjectCatalogueError(f"record exceeds {MAX_RECORD_BYTES} bytes: {path.name}")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectCatalogueError(f"cannot read UTF-8 record {path.name}: {exc}") from exc
    try:
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise ProjectCatalogueError(
                    f"YAML anchors, aliases, and explicit tags are forbidden: {path.name}"
                )
        return yaml.load(text, Loader=_StrictLoader)
    except ProjectCatalogueError:
        raise
    except yaml.YAMLError as exc:
        raise ProjectCatalogueError(f"invalid YAML in {path.name}: {exc}") from exc


def load_project_records(records_root: Path) -> dict[str, ProjectRecord]:
    """Load all canonical record files beneath *records_root*."""
    if not records_root.exists():
        return {}
    if records_root.is_symlink() or not records_root.is_dir():
        raise ProjectCatalogueError("projects/records must be a real directory")
    records: dict[str, ProjectRecord] = {}
    for path in sorted(records_root.glob("*.yaml"), key=lambda item: item.name):
        parsed = _parse_record(_load_yaml(path), filename_stem=path.stem)
        if parsed.project_id in records:
            raise ProjectCatalogueError(f"duplicate project_id: {parsed.project_id}")
        records[parsed.project_id] = parsed
    return records


def _parent_issue(record: ProjectRecord, records: Mapping[str, ProjectRecord], goal_keys: AbstractSet[str]) -> ProjectIssue | None:
    parent = record.parent_goal_ref
    if parent is None:
        return None
    if PARENT_REF_RE.fullmatch(parent) is None:
        return ProjectIssue("invalid-parent-ref", record.project_id, f"unsupported parent ref {parent!r}")
    kind, value = parent.split(":", 1)
    if kind == "goal" and value not in goal_keys:
        return ProjectIssue("dangling-goal", record.project_id, f"goal key is not registered: {value}")
    if kind == "project" and value == record.project_id:
        return ProjectIssue("self-parent", record.project_id, "project cannot parent itself")
    if kind == "project" and value not in records:
        return ProjectIssue("dangling-parent", record.project_id, f"project parent does not exist: {value}")
    return None


def validate_project_graph(
    records: Mapping[str, ProjectRecord], goal_keys: AbstractSet[str]
) -> tuple[ProjectIssue, ...]:
    """Validate parentage and operational evidence references deterministically."""
    issues: list[ProjectIssue] = []
    for project_id in sorted(records):
        record = records[project_id]
        if project_id != record.project_id:
            issues.append(
                ProjectIssue("identity-key-mismatch", project_id, f"record carries {record.project_id!r}")
            )
        parent_issue = _parent_issue(record, records, goal_keys)
        if parent_issue is not None:
            issues.append(parent_issue)
        obligations = record.operational_contract.obligations.refs
        for stage in LIFECYCLE_COVERAGE_KEYS:
            for ref in getattr(record.operational_contract.lifecycle, stage):
                if ref not in obligations:
                    issues.append(
                        ProjectIssue(
                            "unmapped-lifecycle-ref",
                            project_id,
                            f"{stage} ref is not present in ORG-054 obligations: {ref}",
                        )
                    )

    cycle_members: set[str] = set()
    complete: set[str] = set()
    for start in sorted(records):
        if start in complete:
            continue
        ordered: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in records and current not in complete:
            if current in positions:
                cycle_members.update(ordered[positions[current] :])
                break
            positions[current] = len(ordered)
            ordered.append(current)
            parent = records[current].parent_goal_ref
            if parent is None or not parent.startswith("project:"):
                break
            current = parent.split(":", 1)[1]
        complete.update(ordered)
    for project_id in sorted(cycle_members):
        issues.append(ProjectIssue("parent-cycle", project_id, "project parentage contains a cycle"))
    return tuple(sorted(issues, key=lambda issue: (issue.project_id, issue.code, issue.detail)))


def resolve_upward_chain(
    project_id: str,
    records: Mapping[str, ProjectRecord],
    goal_keys: AbstractSet[str],
) -> tuple[str, ...]:
    issues = validate_project_graph(records, goal_keys)
    if issues:
        raise ProjectCatalogueError(f"invalid project graph: {issues!r}")
    if project_id not in records:
        raise ProjectCatalogueError(f"unknown project_id: {project_id}")
    chain: list[str] = []
    current = project_id
    while True:
        chain.append(f"project:{current}")
        parent = records[current].parent_goal_ref
        if parent is None:
            break
        if parent.startswith("goal:"):
            chain.append(parent)
            break
        current = parent.split(":", 1)[1]
    return tuple(chain)


def _array_schema() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}


def _object_schema(keys: Sequence[str], properties: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "required": list(keys),
        "additionalProperties": False,
        "properties": dict(properties),
    }


def _project_record_schema() -> dict[str, object]:
    string_array = _array_schema
    boundary = _object_schema(BOUNDARY_KEYS, {key: string_array() for key in BOUNDARY_KEYS})
    interfaces = _object_schema(INTERFACE_KEYS, {key: string_array() for key in INTERFACE_KEYS})
    stewardship = _object_schema(
        STEWARDSHIP_KEYS,
        {key: {"type": "string", "minLength": 1} for key in STEWARDSHIP_KEYS},
    )
    lifecycle = _object_schema(
        PROJECT_LIFECYCLE_KEYS,
        {
            "developmental_mode": {"type": "string", "enum": [item.value for item in DevelopmentalMode]},
            "formed_at": {"type": "string", "format": "date-time"},
            "transition_refs": string_array(),
        },
    )
    evidence = _object_schema(EVIDENCE_KEYS, {key: string_array() for key in EVIDENCE_KEYS})
    tension = _object_schema(
        TENSION_KEYS,
        {key: {"type": "string", "minLength": 1} for key in TENSION_KEYS},
    )
    obligations = _object_schema(
        OPERATIONAL_OBLIGATION_KEYS,
        {key: string_array() for key in OPERATIONAL_OBLIGATION_KEYS},
    )
    coverage = _object_schema(
        LIFECYCLE_COVERAGE_KEYS,
        {key: string_array() for key in LIFECYCLE_COVERAGE_KEYS},
    )
    contract = _object_schema(
        OPERATIONAL_CONTRACT_KEYS,
        {"obligations": obligations, "lifecycle": coverage},
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/project-record.schema.json",
        "title": "Pantheon ORG-054 project record",
        "type": "object",
        "required": list(PROJECT_RECORD_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "project_id": {"type": "string", "pattern": PROJECT_ID_PATTERN},
            "telos": {"type": "string", "minLength": 1},
            "parent_goal_ref": {
                "anyOf": [{"type": "null"}, {"type": "string", "pattern": PARENT_REF_PATTERN}]
            },
            "classification": {"type": "string", "enum": [item.value for item in ProjectClassification]},
            "boundary": boundary,
            "interfaces": interfaces,
            "stewardship": stewardship,
            "lifecycle": lifecycle,
            "evidence": evidence,
            "tensions": {"type": "array", "items": tension},
            "operational_contract": contract,
        },
    }


def generate_project_record_schema() -> bytes:
    return (json.dumps(_project_record_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_record(record: ProjectRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "project_id": record.project_id,
        "telos": record.telos,
        "parent_goal_ref": record.parent_goal_ref,
        "classification": record.classification.value,
        "boundary": {key: list(getattr(record.boundary, key)) for key in BOUNDARY_KEYS},
        "interfaces": {key: list(getattr(record.interfaces, key)) for key in INTERFACE_KEYS},
        "stewardship": {key: getattr(record.stewardship, key) for key in STEWARDSHIP_KEYS},
        "lifecycle": {
            "developmental_mode": record.lifecycle.developmental_mode.value,
            "formed_at": record.lifecycle.formed_at,
            "transition_refs": list(record.lifecycle.transition_refs),
        },
        "evidence": {key: list(getattr(record.evidence, key)) for key in EVIDENCE_KEYS},
        "tensions": [
            {key: getattr(tension, key) for key in TENSION_KEYS}
            for tension in record.tensions
        ],
        "operational_contract": {
            "obligations": {
                key: list(getattr(record.operational_contract.obligations, key))
                for key in OPERATIONAL_OBLIGATION_KEYS
            },
            "lifecycle": {
                key: list(getattr(record.operational_contract.lifecycle, key))
                for key in LIFECYCLE_COVERAGE_KEYS
            },
        },
    }


def generate_project_index(
    records: Mapping[str, ProjectRecord], goal_keys: AbstractSet[str]
) -> bytes:
    issues = validate_project_graph(records, goal_keys)
    if issues:
        raise ProjectCatalogueError(f"invalid project graph: {issues!r}")
    semantic = {project_id: _canonical_record(records[project_id]) for project_id in sorted(records)}
    canonical = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_from_digest": f"sha256:{digest}",
        "projects": {
            project_id: {
                "record": f"projects/records/{project_id}.yaml",
                "parent_goal_ref": records[project_id].parent_goal_ref,
                "classification": records[project_id].classification.value,
                "developmental_mode": records[project_id].lifecycle.developmental_mode.value,
            }
            for project_id in sorted(records)
        },
    }
    return (json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def check_project_catalogue(
    repo_root: Path, goal_keys: AbstractSet[str]
) -> tuple[ProjectIssue, ...]:
    try:
        records = load_project_records(repo_root / "projects" / "records")
    except ProjectCatalogueError as exc:
        return (ProjectIssue("catalogue-invalid", "*", str(exc)),)
    issues = list(validate_project_graph(records, goal_keys))
    expected_schema = generate_project_record_schema()
    schema_path = repo_root / "schemas" / "project-record.schema.json"
    try:
        actual_schema = schema_path.read_bytes()
    except OSError:
        actual_schema = None
    if actual_schema != expected_schema:
        issues.append(ProjectIssue("schema-stale", "*", "generated schema bytes differ"))
    if not issues:
        expected_index = generate_project_index(records, goal_keys)
        index_path = repo_root / "projects" / "index.json"
        try:
            actual_index = index_path.read_bytes()
        except OSError:
            actual_index = None
        if actual_index != expected_index:
            issues.append(ProjectIssue("index-stale", "*", "generated index bytes differ"))
    return tuple(sorted(issues, key=lambda issue: (issue.project_id, issue.code, issue.detail)))


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
        child.add_argument("--goal-key", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.goal_key:
        # Explicit flags override the registry (tests, ad-hoc checks).
        goal_keys = frozenset(args.goal_key)
    else:
        # T-5679: default to the canonical goals.md markers. A failing
        # registry is surfaced, then validation proceeds with no keys, so
        # only a record that actually names goal:<key> is affected.
        try:
            from tools.goal_registry import registry_status
        except ImportError:  # run as `python3 tools/project_catalogue.py`
            from goal_registry import registry_status

        goal_keys, registry_error = registry_status(args.repo_root)
        if registry_error is not None:
            print(f"goal-registry-unavailable: {registry_error}", file=sys.stderr)
    if any(PROJECT_ID_RE.fullmatch(key) is None for key in goal_keys):
        print("goal keys must use stable lowercase kebab form", file=sys.stderr)
        return 2
    try:
        if args.command == "generate":
            records = load_project_records(args.repo_root / "projects" / "records")
            index = generate_project_index(records, goal_keys)
            _atomic_write(
                args.repo_root / "schemas" / "project-record.schema.json",
                generate_project_record_schema(),
            )
            _atomic_write(args.repo_root / "projects" / "index.json", index)
            return 0
        issues = check_project_catalogue(args.repo_root, goal_keys)
    except ProjectCatalogueError as exc:
        print(f"project-catalogue-invalid: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        print(f"{issue.code}: {issue.project_id}: {issue.detail}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
