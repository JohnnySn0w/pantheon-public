"""Pure ORG-059 catalogue-to-navigator contract (T-3218).

This module owns only the strict, revision-bound projection and closed wire
schema validators for the capability navigator. T-3181 ComponentRecord objects
and T-3182 AssemblyLock bytes remain canonical; no search, selection, planning,
invoke, fallback, health evaluation, persistence, adapter, or authority behavior
is implemented here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import re
from enum import Enum
from typing import Protocol

from tools import assembly_resolver as ar
from tools import component_catalogue as cc


SCHEMA_VERSION = 1

PROJECTION_SCHEMA_ID = (
    "https://pantheon.local/schemas/capability-navigator-projection.schema.json"
)
REQUEST_SCHEMA_ID = "https://pantheon.local/schemas/capability-navigator-request.schema.json"
RESULT_SCHEMA_ID = "https://pantheon.local/schemas/capability-navigator-result.schema.json"
RECEIPT_SCHEMA_ID = "https://pantheon.local/schemas/capability-navigator-receipt.schema.json"

NAVIGATOR_METHODS = ("search", "describe", "plan", "invoke", "status", "cancel")

MAX_METADATA_STRING_BYTES = 4096
MAX_REQUEST_BYTES = 8192
MAX_TYPED_INPUT_BYTES = 2048
MAX_RESULT_SUMMARY_BYTES = 256

IDENTITY_LOOKALIKE_KEYS = frozenset(
    {
        "uid",
        "user_id",
        "account",
        "account_id",
        "persona",
        "persona_ref",
        "role",
        "roles",
        "role_refs",
        "instance",
        "instance_ref",
        "grant",
        "grants",
        "credential",
        "credentials",
        "secret",
        "token",
        "approver",
        "policy_revision",
        "authority_source",
        "auth",
        "authenticated_context",
        "subject",
        "subject_ref",
        "trust_zone",
        "registry_revision",
    }
)

FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "api_key",
        "private_key",
        "raw_log",
        "raw_logs",
        "provider_log",
        "provider_logs",
        "stdout",
        "stderr",
        "binary",
        "bytes",
        "blob",
        "base64",
        "raw_result",
        "unredacted_content",
    }
)

CONTROL_CODEPOINTS = frozenset(
    tuple(range(0x00, 0x20))
    + tuple(range(0x7F, 0xA0))
    + tuple(range(0x202A, 0x202F))
    + tuple(range(0x2066, 0x206A))
    + (0x200B, 0x200C, 0x200D, 0xFEFF)
)

STABLE_ID_PATTERN = "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
VERSION_PATTERN = "^[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
SHA256_PATTERN = "^sha256:[0-9a-f]{64}$"


class NavigatorErrorCode(str, Enum):
    UNSUPPORTED_VERSION = "unsupported-version"
    CATALOGUE_DIGEST_MISMATCH = "catalogue-digest-mismatch"
    NONCANONICAL_LOCK_BYTES = "noncanonical-lock-bytes"
    PROJECTION_DIGEST_MISMATCH = "projection-digest-mismatch"
    RESOLUTION_ORDER_MISMATCH = "resolution-order-mismatch"
    MEMBERSHIP_MISMATCH = "membership-mismatch"
    DUPLICATE_COMPONENT = "duplicate-component"
    SOURCE_IDENTITY_MISMATCH = "source-identity-mismatch"
    PROVIDER_DECLARATION_DUPLICATE = "provider-declaration-duplicate"
    PROVIDER_DECLARATION_INCONSISTENT = "provider-declaration-inconsistent"
    MISSING_PROVIDER_DATA = "missing-provider-data"
    OVERSIZED_METADATA = "oversized-metadata"
    BINARY_OR_NON_UTF8_METADATA = "binary-or-non-utf8-metadata"
    HOSTILE_CONTROL_METADATA = "hostile-control-metadata"
    IDENTITY_LOOKALIKE = "identity-lookalike"
    BUDGET_BELOW_MINIMUM = "budget-below-minimum"
    FORBIDDEN_RESULT_CONTAINER = "forbidden-result-container"
    BYTE_BOUND_EXCEEDED = "byte-bound-exceeded"
    SCHEMA_CLOSED_OBJECT = "schema-closed-object"
    SCHEMA_REQUIRED_FIELD = "schema-required-field"
    SCHEMA_TYPE = "schema-type"
    SCHEMA_ENUM = "schema-enum"
    # T-3222 (ORG-059 Child E) — typed delegation round-trip refusals.
    DELEGATION_ROLE_NOT_A_ROLE = "delegation-role-not-a-role"
    DELEGATION_STAGE_PAYLOAD = "delegation-stage-payload"


class NavigatorContractError(ValueError):
    """Typed refusal for navigator contract construction/validation."""

    def __init__(self, code: NavigatorErrorCode, path: str, detail: str) -> None:
        super().__init__(f"{code.value}: {path}: {detail}")
        self.code = code
        self.path = path
        self.detail = detail


@dataclass(frozen=True)
class AuthenticatedContext:
    subject_ref: str
    uid: int
    persona_ref: str
    role_refs: tuple[str, ...]
    instance_ref: str
    trust_zone: str
    registry_revision: str


class SubjectResolver(Protocol):
    """Server-owned seam; public wire requests never supply this context."""

    def resolve(self, transport: object) -> AuthenticatedContext: ...


@dataclass(frozen=True)
class NavigatorBudget:
    max_candidates: int
    max_summary_bytes: int
    max_contract_bytes: int
    max_total_bytes: int
    max_provider_evidence_refs: int


SERVER_CEILINGS = NavigatorBudget(
    max_candidates=20,
    max_summary_bytes=256,
    max_contract_bytes=2048,
    max_total_bytes=8192,
    max_provider_evidence_refs=8,
)
MINIMUM_BUDGET = NavigatorBudget(
    max_candidates=1,
    max_summary_bytes=1,
    max_contract_bytes=1,
    max_total_bytes=512,
    max_provider_evidence_refs=1,
)
_BUDGET_FIELDS = tuple(SERVER_CEILINGS.__dataclass_fields__)


@dataclass(frozen=True)
class SourceIdentity:
    logical_repo: str
    path: str | None
    artifact: str | None
    record_sha256: str
    tested_revision: str
    locked_revision: str
    artifact_digest: str


@dataclass(frozen=True)
class NavigatorImplication:
    kind: str
    implication_id: str
    scope: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class NavigatorRecurringCost:
    cost_id: str
    category: str
    quantity: float
    unit: str
    cadence: str
    measurement_source: str
    owner: str
    alternative_ref: str | None
    last_measured_at: str
    review_due_at: str


@dataclass(frozen=True)
class NavigatorUpkeep:
    steward: str
    health_evidence_refs: tuple[str, ...]
    monitoring_refs: tuple[str, ...]
    upgrade_rollback_refs: tuple[str, ...]
    credential_rotation_refs: tuple[str, ...]
    storage_retention_refs: tuple[str, ...]
    costs: tuple[NavigatorRecurringCost, ...]
    review_obligations: tuple[str, ...]


@dataclass(frozen=True)
class NavigatorSubjectVisibilityPolicy:
    subject_refs: tuple[str, ...]
    role_refs: tuple[str, ...]
    instance_refs: tuple[str, ...]
    trust_zones: tuple[str, ...]
    policy_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class NavigatorCapabilityProvider:
    capability_id: str
    provider_component_id: str
    contract: str
    schema: str
    version: str
    enabled_features: tuple[str, ...]
    requirement_closure: tuple[str, ...]
    equivalence_class: str | None
    trust_class: str | None
    deployment_trust_zone: str | None
    subject_visibility: NavigatorSubjectVisibilityPolicy | None
    navigator_provider_selection: cc.NavigatorProviderSelection | None
    source_identity: SourceIdentity
    compatibility_evidence: tuple[str, ...]
    implications: tuple[NavigatorImplication, ...]
    upkeep: NavigatorUpkeep


@dataclass(frozen=True)
class NavigatorProjection:
    schema_version: int
    assembly_id: str
    manifest_sha256: str
    catalogue_sha256: str
    raw_lock_sha256: str
    lock_sha256: str
    generator_id: str
    generator_version: str
    resolution_order: tuple[str, ...]
    member_component_ids: tuple[str, ...]
    acknowledged_implications: tuple[str, ...]
    owned_obligations: tuple[str, ...]
    smoke_evidence: tuple[str, ...]
    health_evidence: tuple[str, ...]
    capabilities: tuple[NavigatorCapabilityProvider, ...]
    projection_sha256: str


@dataclass(frozen=True)
class NavigatorRequest:
    schema_version: int
    method: str
    request_id: str
    payload: Mapping[str, object]
    context_budget: NavigatorBudget


@dataclass(frozen=True)
class NavigatorResult:
    schema_version: int
    method: str
    request_id: str
    status: str
    subject_ref: str
    projection_sha256: str
    catalogue_sha256: str
    lock_sha256: str
    raw_lock_sha256: str
    created_at: str
    payload: Mapping[str, object]
    provider_evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class NavigatorReceipt:
    schema_version: int
    receipt_id: str
    method: str
    status: str
    subject_ref: str
    capability_id: str
    provider_component_id: str
    contract: str
    schema: str
    version: str
    projection_sha256: str
    catalogue_sha256: str
    lock_sha256: str
    raw_lock_sha256: str
    effect_digest: str | None
    redacted_summary: str
    provider_native_evidence_refs: tuple[str, ...]
    authority_provenance_ref: str | None
    created_at: str
    updated_at: str | None
    prior_handle_ref: str | None
    prior_receipt_ref: str | None


def canonical_json_bytes(value: object) -> bytes:
    _validate_json_value(value, path="$")
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _catalogue_digest(records: Mapping[str, cc.ComponentRecord]) -> str:
    semantic = {
        component_id: records[component_id].source_sha256
        for component_id in sorted(records)
    }
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_lock_bytes(lock: ar.AssemblyLock) -> bytes:
    return ar._json_bytes(ar._lock_json(lock))


def _fail(code: NavigatorErrorCode, path: str, detail: str) -> None:
    raise NavigatorContractError(code, path, detail)


def _validate_text(value: str, *, path: str, max_bytes: int = MAX_METADATA_STRING_BYTES) -> None:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise NavigatorContractError(
            NavigatorErrorCode.BINARY_OR_NON_UTF8_METADATA,
            path,
            "string is not encodable as UTF-8",
        ) from exc
    if len(encoded) > max_bytes:
        _fail(NavigatorErrorCode.OVERSIZED_METADATA, path, "string exceeds byte ceiling")
    for char in value:
        if ord(char) in CONTROL_CODEPOINTS:
            _fail(
                NavigatorErrorCode.HOSTILE_CONTROL_METADATA,
                path,
                f"control character U+{ord(char):04X} is forbidden",
            )


def _validate_json_value(value: object, *, path: str) -> None:
    if isinstance(value, (bytes, bytearray)):
        _fail(NavigatorErrorCode.BINARY_OR_NON_UTF8_METADATA, path, "binary value is forbidden")
    if isinstance(value, str):
        _validate_text(value, path=path)
    elif isinstance(value, bool) or value is None or type(value) is int:
        return
    elif type(value) is float:
        if not math.isfinite(value):
            _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "non-finite number is forbidden")
        return
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "object keys must be strings")
            _validate_text(key, path=f"{path}.{key}")
            _validate_json_value(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
    else:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, f"unsupported JSON value {type(value).__name__}")


def _reject_identity_lookalikes(value: object, *, path: str, in_typed_input: bool = False) -> None:
    if in_typed_input:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "object keys must be strings")
            if key in IDENTITY_LOOKALIKE_KEYS:
                _fail(NavigatorErrorCode.IDENTITY_LOOKALIKE, f"{path}.{key}", "identity/control key is forbidden")
            _reject_identity_lookalikes(
                item,
                path=f"{path}.{key}",
                in_typed_input=(key == "typed_input"),
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_identity_lookalikes(item, path=f"{path}[{index}]")


def _reject_forbidden_result_content(value: object, *, path: str) -> None:
    if isinstance(value, (bytes, bytearray)):
        _fail(NavigatorErrorCode.FORBIDDEN_RESULT_CONTAINER, path, "binary container is forbidden")
    if isinstance(value, str):
        _validate_text(value, path=path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "object keys must be strings")
            if key in FORBIDDEN_RESULT_KEYS:
                _fail(NavigatorErrorCode.FORBIDDEN_RESULT_CONTAINER, f"{path}.{key}", "forbidden raw/secret key")
            _reject_forbidden_result_content(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_forbidden_result_content(item, path=f"{path}[{index}]")


def _require_mapping(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "object keys must be strings")
        result[key] = item
    return result


def _require_closed(value: Mapping[str, object], *, keys: Sequence[str], path: str) -> None:
    expected = set(keys)
    actual = set(value)
    missing = sorted(expected - actual)
    if missing:
        _fail(NavigatorErrorCode.SCHEMA_REQUIRED_FIELD, path, f"missing {missing[0]!r}")
    extra = sorted(actual - expected)
    if extra:
        _fail(NavigatorErrorCode.SCHEMA_CLOSED_OBJECT, f"{path}.{extra[0]}", "unexpected field")


def _require_closed_compatible(
    value: Mapping[str, object],
    *,
    required_keys: Sequence[str],
    allowed_keys: Sequence[str],
    path: str,
) -> None:
    required = set(required_keys)
    allowed = set(allowed_keys)
    actual = set(value)
    missing = sorted(required - actual)
    if missing:
        _fail(NavigatorErrorCode.SCHEMA_REQUIRED_FIELD, path, f"missing {missing[0]!r}")
    extra = sorted(actual - allowed)
    if extra:
        _fail(NavigatorErrorCode.SCHEMA_CLOSED_OBJECT, f"{path}.{extra[0]}", "unexpected field")


def _require_string(value: object, *, path: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected nonempty string")
    _validate_text(value, path=path)
    return value


def _require_int(value: object, *, path: str) -> int:
    if type(value) is not int:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected integer")
    return value


def _require_bool(value: object, *, path: str) -> bool:
    if type(value) is not bool:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected boolean")
    return value


def _require_number(value: object, *, path: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected number")
    result = float(value)
    if not math.isfinite(result):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "non-finite number is forbidden")
    return result


def _require_sha(value: object, *, path: str, nullable: bool = False) -> str | None:
    result = _require_string(value, path=path, nullable=nullable)
    if result is None:
        return None
    if (
        len(result) != 71
        or not result.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in result[7:])
    ):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected sha256 digest")
    return result


def _require_pattern(
    value: object,
    *,
    path: str,
    pattern: str,
    label: str,
    nullable: bool = False,
) -> str | None:
    result = _require_string(value, path=path, nullable=nullable)
    if result is None:
        return None
    if re.fullmatch(pattern, result) is None:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, f"expected {label}")
    return result


def _require_stable_id(value: object, *, path: str, nullable: bool = False) -> str | None:
    return _require_pattern(
        value,
        path=path,
        pattern=STABLE_ID_PATTERN,
        label="stable id",
        nullable=nullable,
    )


def _require_semver(value: object, *, path: str, nullable: bool = False) -> str | None:
    return _require_pattern(
        value,
        path=path,
        pattern=VERSION_PATTERN,
        label="semver",
        nullable=nullable,
    )


def _require_summary(value: object, *, path: str) -> str:
    result = _require_string(value, path=path) or ""
    if len(result.encode("utf-8")) > MAX_RESULT_SUMMARY_BYTES:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, path, "summary exceeds UTF-8 byte ceiling")
    return result


def _string_tuple(values: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected array")
    result: list[str] = []
    for index, value in enumerate(values):
        result.append(_require_string(value, path=f"{path}[{index}]") or "")
    if len(set(result)) != len(result):
        _fail(NavigatorErrorCode.PROVIDER_DECLARATION_DUPLICATE, path, "array values must be unique")
    return tuple(result)


def _subject_visibility_tuple(
    values: object, *, path: str, require_nonempty: bool = False
) -> tuple[str, ...]:
    result = _string_tuple(values, path=path)
    if require_nonempty and not result:
        _fail(NavigatorErrorCode.SCHEMA_REQUIRED_FIELD, path, "array must not be empty")
    forbidden = sorted(
        token for token in result if token in cc.FORBIDDEN_SUBJECT_VISIBILITY_TOKENS
    )
    if forbidden:
        _fail(
            NavigatorErrorCode.SCHEMA_ENUM,
            path,
            f"wildcard subject visibility tokens are forbidden: {forbidden!r}",
        )
    return result


def _stable_tuple(values: object, *, path: str) -> tuple[str, ...]:
    result = _string_tuple(values, path=path)
    for index, value in enumerate(result):
        _require_stable_id(value, path=f"{path}[{index}]")
    return result


def _semver_tuple(values: object, *, path: str) -> tuple[str, ...]:
    result = _string_tuple(values, path=path)
    for index, value in enumerate(result):
        _require_semver(value, path=f"{path}[{index}]")
    return result


def intersect_budget(requested: Mapping[str, object] | None) -> NavigatorBudget:
    if requested is None:
        return SERVER_CEILINGS
    data = _require_mapping(requested, path="context_budget")
    _require_closed(data, keys=_BUDGET_FIELDS, path="context_budget")
    values: dict[str, int] = {}
    for field in _BUDGET_FIELDS:
        raw = _require_int(data[field], path=f"context_budget.{field}")
        minimum = getattr(MINIMUM_BUDGET, field)
        ceiling = getattr(SERVER_CEILINGS, field)
        if raw < minimum:
            _fail(NavigatorErrorCode.BUDGET_BELOW_MINIMUM, f"context_budget.{field}", "below semantic minimum")
        values[field] = min(raw, ceiling)
    return NavigatorBudget(**values)


def _budget_json(budget: NavigatorBudget) -> dict[str, object]:
    return {field: getattr(budget, field) for field in _BUDGET_FIELDS}


def _implications(record: cc.ComponentRecord) -> tuple[NavigatorImplication, ...]:
    result: list[NavigatorImplication] = []
    groups = (
        ("authority", record.implications.authority),
        ("privacy_data", record.implications.privacy_data),
        ("hardware_network", record.implications.hardware_network),
        ("operational", record.implications.operational),
    )
    for kind, items in groups:
        for item in items:
            result.append(
                NavigatorImplication(
                    kind=kind,
                    implication_id=item.implication_id,
                    scope=item.scope,
                    evidence_refs=tuple(item.evidence_refs),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.kind, item.implication_id, item.scope)))


def _upkeep(record: cc.ComponentRecord) -> NavigatorUpkeep:
    return NavigatorUpkeep(
        steward=record.upkeep.steward,
        health_evidence_refs=tuple(record.upkeep.health_evidence),
        monitoring_refs=tuple(record.upkeep.monitoring),
        upgrade_rollback_refs=tuple(record.upkeep.upgrade_rollback),
        credential_rotation_refs=tuple(record.upkeep.credential_rotation),
        storage_retention_refs=tuple(record.upkeep.storage_retention),
        costs=tuple(
            NavigatorRecurringCost(
                cost_id=cost.cost_id,
                category=cost.category.value,
                quantity=cost.quantity,
                unit=cost.unit,
                cadence=cost.cadence.value,
                measurement_source=cost.measurement_source,
                owner=cost.owner,
                alternative_ref=cost.alternative_ref,
                last_measured_at=cost.last_measured_at,
                review_due_at=cost.review_due_at,
            )
            for cost in sorted(record.upkeep.cost, key=lambda item: item.cost_id)
        ),
        review_obligations=tuple(record.upkeep.review_obligations),
    )


def _navigator_subject_visibility(
    policy: cc.SubjectVisibilityPolicy | None,
) -> NavigatorSubjectVisibilityPolicy | None:
    if policy is None:
        return None
    return NavigatorSubjectVisibilityPolicy(
        subject_refs=tuple(policy.subject_refs),
        role_refs=tuple(policy.role_refs),
        instance_refs=tuple(policy.instance_refs),
        trust_zones=tuple(policy.trust_zones),
        policy_refs=tuple(policy.policy_refs),
        evidence_refs=tuple(policy.evidence_refs),
    )


def _navigator_provider_selection_json(
    selection: cc.NavigatorProviderSelection | None,
) -> dict[str, object] | None:
    return cc.navigator_provider_selection_to_json(selection)


def _prefixed_ref(value: object, *, path: str, prefix: str) -> str:
    result = _require_string(value, path=path) or ""
    pattern = (
        cc.ORGAN_REF_PATTERN
        if prefix == cc.ORGAN_REF_PREFIX
        else cc.DEPENDENCY_REF_PATTERN
        if prefix == cc.DEPENDENCY_REF_PREFIX
        else rf"^{re.escape(prefix)}.+$"
    )
    if re.fullmatch(pattern, result) is None:
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, f"expected {prefix} reference")
    return result


def _parse_navigator_provider_selection(
    value: object, *, path: str
) -> cc.NavigatorProviderSelection | None:
    if value is None:
        return None
    data = _require_mapping(value, path=path)
    _require_closed(data, keys=cc.NAVIGATOR_PROVIDER_SELECTION_KEYS, path=path)
    raw_role = _require_string(data["role"], path=f"{path}.role") or ""
    try:
        role = cc.NavigatorProviderRole(raw_role)
    except ValueError:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, f"{path}.role", "unsupported provider selection role")
    primary_provider_component_id: str | None
    fallback_rank: int | None
    if role is cc.NavigatorProviderRole.PRIMARY:
        if data["primary_provider_component_id"] is not None:
            _fail(
                NavigatorErrorCode.SCHEMA_TYPE,
                f"{path}.primary_provider_component_id",
                "primary provider selection requires null target",
            )
        if data["fallback_rank"] is not None:
            _fail(
                NavigatorErrorCode.SCHEMA_TYPE,
                f"{path}.fallback_rank",
                "primary provider selection requires null rank",
            )
        primary_provider_component_id = None
        fallback_rank = None
    else:
        primary_provider_component_id = _require_stable_id(
            data["primary_provider_component_id"],
            path=f"{path}.primary_provider_component_id",
        )
        raw_rank = _require_int(data["fallback_rank"], path=f"{path}.fallback_rank")
        if raw_rank < 1:
            _fail(
                NavigatorErrorCode.SCHEMA_TYPE,
                f"{path}.fallback_rank",
                "fallback rank must be positive",
            )
        fallback_rank = raw_rank
    return cc.NavigatorProviderSelection(
        role=role,
        primary_provider_component_id=primary_provider_component_id,
        fallback_rank=fallback_rank,
        provider_organ_id=_prefixed_ref(
            data["provider_organ_id"],
            path=f"{path}.provider_organ_id",
            prefix=cc.ORGAN_REF_PREFIX,
        ),
        health_dependency_id=_prefixed_ref(
            data["health_dependency_id"],
            path=f"{path}.health_dependency_id",
            prefix=cc.DEPENDENCY_REF_PREFIX,
        ),
    )


def _selection_group_key(
    provider: NavigatorCapabilityProvider,
) -> tuple[str, str, str, str, str | None, str | None]:
    return (
        provider.capability_id,
        provider.contract,
        provider.schema,
        provider.version,
        provider.equivalence_class,
        provider.trust_class,
    )


def _validate_navigator_provider_selections(
    providers: Sequence[NavigatorCapabilityProvider], *, path: str
) -> None:
    groups: dict[
        tuple[str, str, str, str, str | None, str | None],
        list[NavigatorCapabilityProvider],
    ] = {}
    for index, provider in enumerate(providers):
        selection = provider.navigator_provider_selection
        if selection is None:
            continue
        if provider.equivalence_class is None or provider.trust_class is None:
            _fail(
                NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                f"{path}[{index}].navigator_provider_selection",
                "selected providers require equivalence_class and trust_class",
            )
        groups.setdefault(_selection_group_key(provider), []).append(provider)

    join_pairs: dict[tuple[str, str], str] = {}
    for provider in (item for group in groups.values() for item in group):
        selection = provider.navigator_provider_selection
        if selection is None:
            continue
        pair = (selection.provider_organ_id, selection.health_dependency_id)
        prior_component = join_pairs.get(pair)
        if prior_component is not None and prior_component != provider.provider_component_id:
            _fail(
                NavigatorErrorCode.PROVIDER_DECLARATION_DUPLICATE,
                f"{path}.{provider.provider_component_id}.navigator_provider_selection",
                f"duplicate provider health join pair also used by {prior_component}",
            )
        join_pairs[pair] = provider.provider_component_id

    for group_key, selected in groups.items():
        group_path = f"{path}.{group_key[0]}.{group_key[1]}.{group_key[2]}.{group_key[3]}"
        primaries = [
            provider
            for provider in selected
            if provider.navigator_provider_selection is not None
            and provider.navigator_provider_selection.role is cc.NavigatorProviderRole.PRIMARY
        ]
        if len(primaries) > 1:
            _fail(
                NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                group_path,
                "multiple primary provider selections",
            )
        selected_by_component = {provider.provider_component_id: provider for provider in selected}
        if len(selected_by_component) != len(selected):
            _fail(
                NavigatorErrorCode.PROVIDER_DECLARATION_DUPLICATE,
                group_path,
                "duplicate selected provider component",
            )
        ranks: dict[int, str] = {}
        for provider in selected:
            selection = provider.navigator_provider_selection
            if selection is None or selection.role is cc.NavigatorProviderRole.PRIMARY:
                continue
            rank = selection.fallback_rank
            if rank is None:
                _fail(
                    NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                    f"{group_path}.{provider.provider_component_id}",
                    "fallback provider selection missing rank",
                )
            prior = ranks.get(rank)
            if prior is not None:
                _fail(
                    NavigatorErrorCode.PROVIDER_DECLARATION_DUPLICATE,
                    f"{group_path}.{provider.provider_component_id}.fallback_rank",
                    f"duplicate fallback rank also used by {prior}",
                )
            ranks[rank] = provider.provider_component_id
            target = selection.primary_provider_component_id
            if target is None:
                _fail(
                    NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                    f"{group_path}.{provider.provider_component_id}",
                    "fallback provider selection missing primary target",
                )
            if target == provider.provider_component_id:
                _fail(
                    NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                    f"{group_path}.{provider.provider_component_id}",
                    "fallback provider selection targets itself",
                )
            target_provider = selected_by_component.get(target)
            if target_provider is None:
                _fail(
                    NavigatorErrorCode.MISSING_PROVIDER_DATA,
                    f"{group_path}.{provider.provider_component_id}",
                    "fallback provider selection target is not a selected primary in the same group",
                )
            target_selection = target_provider.navigator_provider_selection
            if (
                target_selection is None
                or target_selection.role is not cc.NavigatorProviderRole.PRIMARY
            ):
                _fail(
                    NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT,
                    f"{group_path}.{provider.provider_component_id}",
                    "fallback provider selection target is not primary",
                )


def _metadata_values(record: cc.ComponentRecord) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = [
        ("component_id", record.component_id),
        ("project_id", record.project_id),
        ("logical_repo", record.source.logical_repo),
        ("tested_revision", record.compatibility.tested_revision),
        ("trust_zone", record.deployment.trust_zone),
        ("steward", record.upkeep.steward),
    ]
    for provision in record.provides:
        values.extend(
            (
                ("capability", provision.capability),
                ("contract", provision.contract),
                ("schema", provision.schema),
                ("version", provision.version),
            )
        )
        if provision.equivalence_class is not None:
            values.append(("equivalence_class", provision.equivalence_class))
        if provision.trust_class is not None:
            values.append(("trust_class", provision.trust_class))
        values.extend(("feature", feature) for feature in provision.features)
        if provision.subject_visibility is not None:
            values.extend(
                ("subject_visibility_subject_ref", ref)
                for ref in provision.subject_visibility.subject_refs
            )
            values.extend(
                ("subject_visibility_role_ref", ref)
                for ref in provision.subject_visibility.role_refs
            )
            values.extend(
                ("subject_visibility_instance_ref", ref)
                for ref in provision.subject_visibility.instance_refs
            )
            values.extend(
                ("subject_visibility_trust_zone", ref)
                for ref in provision.subject_visibility.trust_zones
            )
            values.extend(
                ("subject_visibility_policy_ref", ref)
                for ref in provision.subject_visibility.policy_refs
            )
            values.extend(
                ("subject_visibility_evidence_ref", ref)
                for ref in provision.subject_visibility.evidence_refs
            )
    for implication in _implications(record):
        values.extend(
            (
                ("implication_id", implication.implication_id),
                ("implication_scope", implication.scope),
            )
        )
        values.extend(("implication_evidence", ref) for ref in implication.evidence_refs)
    upkeep = _upkeep(record)
    values.extend(("health_evidence", ref) for ref in upkeep.health_evidence_refs)
    values.extend(("monitoring", ref) for ref in upkeep.monitoring_refs)
    values.extend(("review_obligation", ref) for ref in upkeep.review_obligations)
    for cost in upkeep.costs:
        values.extend(
            (
                ("cost_id", cost.cost_id),
                ("cost_measurement_source", cost.measurement_source),
                ("cost_owner", cost.owner),
                ("cost_review_due_at", cost.review_due_at),
            )
        )
    return tuple(values)


def _validate_record_metadata(record: cc.ComponentRecord) -> None:
    for label, value in _metadata_values(record):
        _validate_text(value, path=f"records.{record.component_id}.{label}")


def _version_map(values: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name, version in values:
        result.setdefault(name, set()).add(version)
    return result


def _projection_preimage(projection: NavigatorProjection) -> dict[str, object]:
    return projection_to_json(projection, include_digest=False)


def build_navigator_projection(
    records: Mapping[str, cc.ComponentRecord],
    raw_lock_bytes: bytes,
    *,
    assembly_id: str | None = None,
) -> NavigatorProjection:
    if not isinstance(raw_lock_bytes, bytes):
        _fail(NavigatorErrorCode.BINARY_OR_NON_UTF8_METADATA, "raw_lock_bytes", "lock boundary must be bytes")
    raw_lock_sha256 = _sha256_bytes(raw_lock_bytes)
    try:
        lock = ar.parse_assembly_lock(raw_lock_bytes)
    except Exception as exc:
        raise NavigatorContractError(
            NavigatorErrorCode.BINARY_OR_NON_UTF8_METADATA,
            "raw_lock_bytes",
            str(exc),
        ) from exc
    canonical_lock = _canonical_lock_bytes(lock)
    if raw_lock_bytes != canonical_lock:
        _fail(NavigatorErrorCode.NONCANONICAL_LOCK_BYTES, "raw_lock_bytes", "lock bytes differ from canonical resolver bytes")
    if assembly_id is not None and lock.assembly_id != assembly_id:
        _fail(NavigatorErrorCode.MEMBERSHIP_MISMATCH, "assembly_id", "assembly id does not match lock")
    computed_catalogue = _catalogue_digest(records)
    if lock.catalogue_sha256 != f"sha256:{computed_catalogue}":
        _fail(NavigatorErrorCode.CATALOGUE_DIGEST_MISMATCH, "catalogue_sha256", "lock catalogue digest does not match records")
    if tuple(lock.resolution_order) != tuple(ar.RESOLUTION_ORDER):
        _fail(NavigatorErrorCode.RESOLUTION_ORDER_MISMATCH, "resolution_order", "lock resolver pipeline mismatch")
    for mapping_key, record in records.items():
        if mapping_key != record.component_id:
            _fail(NavigatorErrorCode.SOURCE_IDENTITY_MISMATCH, f"records.{mapping_key}", "record key does not match component_id")

    locked_by_id: dict[str, ar.LockedComponent] = {}
    for locked in lock.components:
        if locked.component_id in locked_by_id:
            _fail(NavigatorErrorCode.DUPLICATE_COMPONENT, "components", f"duplicate {locked.component_id}")
        locked_by_id[locked.component_id] = locked
    if not locked_by_id:
        _fail(NavigatorErrorCode.MEMBERSHIP_MISMATCH, "components", "assembly lock has no members")

    providers: list[NavigatorCapabilityProvider] = []
    declaration_index: dict[tuple[str, str, str, str, str], NavigatorCapabilityProvider] = {}
    for component_id in sorted(locked_by_id):
        locked = locked_by_id[component_id]
        record = records.get(component_id)
        if record is None:
            _fail(NavigatorErrorCode.MEMBERSHIP_MISMATCH, f"components.{component_id}", "locked member has no record")
        _validate_record_metadata(record)
        if (
            record.source.logical_repo != locked.logical_repo
            or record.source.path != locked.path
            or record.source.artifact != locked.artifact
        ):
            _fail(NavigatorErrorCode.SOURCE_IDENTITY_MISMATCH, f"components.{component_id}.source", "record source does not match lock")
        if record.compatibility.tested_revision != locked.revision:
            _fail(NavigatorErrorCode.SOURCE_IDENTITY_MISMATCH, f"components.{component_id}.revision", "record tested_revision does not match lock revision")

        record_contract_versions = {
            (provision.contract, provision.version) for provision in record.provides
        }
        lock_contract_versions = set(locked.contract_versions)
        if record_contract_versions != lock_contract_versions:
            _fail(NavigatorErrorCode.MISSING_PROVIDER_DATA, f"components.{component_id}.contract_versions", "record contract/version set does not match lock")
        record_schema_versions = {
            (provision.schema, provision.version) for provision in record.provides
        }
        lock_schema_versions = set(locked.schema_versions)
        if record_schema_versions != lock_schema_versions:
            _fail(NavigatorErrorCode.MISSING_PROVIDER_DATA, f"components.{component_id}.schema_versions", "record schema/version set does not match lock")
        locked_features = set(locked.enabled_features)
        declared_features = {
            feature for provision in record.provides for feature in provision.features
        }
        if not locked_features.issubset(declared_features):
            _fail(NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT, f"components.{component_id}.features", "locked feature missing from provider declarations")
        source_identity = SourceIdentity(
            logical_repo=locked.logical_repo,
            path=locked.path,
            artifact=locked.artifact,
            record_sha256=f"sha256:{record.source_sha256}",
            tested_revision=record.compatibility.tested_revision,
            locked_revision=locked.revision,
            artifact_digest=locked.artifact_digest,
        )
        for provision in sorted(
            record.provides,
            key=lambda item: (item.capability, item.contract, item.schema, item.version),
        ):
            provider = NavigatorCapabilityProvider(
                capability_id=provision.capability,
                provider_component_id=component_id,
                contract=provision.contract,
                schema=provision.schema,
                version=provision.version,
                enabled_features=tuple(sorted(set(provision.features) & locked_features)),
                requirement_closure=tuple(locked.requirement_closure),
                equivalence_class=provision.equivalence_class,
                trust_class=provision.trust_class,
                deployment_trust_zone=record.deployment.trust_zone,
                subject_visibility=_navigator_subject_visibility(
                    provision.subject_visibility
                ),
                navigator_provider_selection=provision.navigator_provider_selection,
                source_identity=source_identity,
                compatibility_evidence=tuple(locked.compatibility_evidence),
                implications=_implications(record),
                upkeep=_upkeep(record),
            )
            key = (
                provider.provider_component_id,
                provider.capability_id,
                provider.contract,
                provider.schema,
                provider.version,
            )
            prior = declaration_index.get(key)
            if prior == provider:
                _fail(NavigatorErrorCode.PROVIDER_DECLARATION_DUPLICATE, f"components.{component_id}.provides", "duplicate provider declaration")
            if prior is not None and prior != provider:
                _fail(NavigatorErrorCode.PROVIDER_DECLARATION_INCONSISTENT, f"components.{component_id}.provides", "inconsistent provider declaration")
            declaration_index[key] = provider
            providers.append(provider)
    if not providers:
        _fail(NavigatorErrorCode.MISSING_PROVIDER_DATA, "capabilities", "projection has no providers")
    _validate_navigator_provider_selections(providers, path="capabilities")

    base = NavigatorProjection(
        schema_version=SCHEMA_VERSION,
        assembly_id=lock.assembly_id,
        manifest_sha256=lock.manifest_sha256,
        catalogue_sha256=lock.catalogue_sha256,
        raw_lock_sha256=f"sha256:{raw_lock_sha256}",
        lock_sha256=f"sha256:{_sha256_bytes(canonical_lock)}",
        generator_id=lock.generator_id,
        generator_version=lock.generator_version,
        resolution_order=tuple(lock.resolution_order),
        member_component_ids=tuple(sorted(locked_by_id)),
        acknowledged_implications=tuple(lock.acknowledged_implications),
        owned_obligations=tuple(lock.owned_obligations),
        smoke_evidence=tuple(lock.smoke_evidence),
        health_evidence=tuple(lock.health_evidence),
        capabilities=tuple(
            sorted(
                providers,
                key=lambda item: (
                    item.capability_id,
                    item.provider_component_id,
                    item.contract,
                    item.schema,
                    item.version,
                ),
            )
        ),
        projection_sha256="",
    )
    return NavigatorProjection(
        **{
            **base.__dict__,
            "projection_sha256": f"sha256:{_sha256_json(_projection_preimage(base))}",
        }
    )


def _source_identity_json(source: SourceIdentity) -> dict[str, object]:
    return {
        "logical_repo": source.logical_repo,
        "path": source.path,
        "artifact": source.artifact,
        "record_sha256": source.record_sha256,
        "tested_revision": source.tested_revision,
        "locked_revision": source.locked_revision,
        "artifact_digest": source.artifact_digest,
    }


def _implication_json(implication: NavigatorImplication) -> dict[str, object]:
    return {
        "kind": implication.kind,
        "implication_id": implication.implication_id,
        "scope": implication.scope,
        "evidence_refs": list(implication.evidence_refs),
    }


def _cost_json(cost: NavigatorRecurringCost) -> dict[str, object]:
    return {
        "cost_id": cost.cost_id,
        "category": cost.category,
        "quantity": cost.quantity,
        "unit": cost.unit,
        "cadence": cost.cadence,
        "measurement_source": cost.measurement_source,
        "owner": cost.owner,
        "alternative_ref": cost.alternative_ref,
        "last_measured_at": cost.last_measured_at,
        "review_due_at": cost.review_due_at,
    }


def _upkeep_json(upkeep: NavigatorUpkeep) -> dict[str, object]:
    return {
        "steward": upkeep.steward,
        "health_evidence_refs": list(upkeep.health_evidence_refs),
        "monitoring_refs": list(upkeep.monitoring_refs),
        "upgrade_rollback_refs": list(upkeep.upgrade_rollback_refs),
        "credential_rotation_refs": list(upkeep.credential_rotation_refs),
        "storage_retention_refs": list(upkeep.storage_retention_refs),
        "costs": [_cost_json(cost) for cost in upkeep.costs],
        "review_obligations": list(upkeep.review_obligations),
    }


def _subject_visibility_json(
    policy: NavigatorSubjectVisibilityPolicy | None,
) -> dict[str, object] | None:
    if policy is None:
        return None
    return {
        "subject_refs": list(policy.subject_refs),
        "role_refs": list(policy.role_refs),
        "instance_refs": list(policy.instance_refs),
        "trust_zones": list(policy.trust_zones),
        "policy_refs": list(policy.policy_refs),
        "evidence_refs": list(policy.evidence_refs),
    }


def _provider_json(provider: NavigatorCapabilityProvider) -> dict[str, object]:
    return {
        "capability_id": provider.capability_id,
        "provider_component_id": provider.provider_component_id,
        "contract": provider.contract,
        "schema": provider.schema,
        "version": provider.version,
        "enabled_features": list(provider.enabled_features),
        "requirement_closure": list(provider.requirement_closure),
        "equivalence_class": provider.equivalence_class,
        "trust_class": provider.trust_class,
        "deployment_trust_zone": provider.deployment_trust_zone,
        "subject_visibility": _subject_visibility_json(provider.subject_visibility),
        "navigator_provider_selection": _navigator_provider_selection_json(
            provider.navigator_provider_selection
        ),
        "source_identity": _source_identity_json(provider.source_identity),
        "compatibility_evidence": list(provider.compatibility_evidence),
        "implications": [_implication_json(item) for item in provider.implications],
        "upkeep": _upkeep_json(provider.upkeep),
    }


def projection_to_json(
    projection: NavigatorProjection, *, include_digest: bool = True
) -> dict[str, object]:
    return {
        "schema_version": projection.schema_version,
        "assembly_id": projection.assembly_id,
        "manifest_sha256": projection.manifest_sha256,
        "catalogue_sha256": projection.catalogue_sha256,
        "raw_lock_sha256": projection.raw_lock_sha256,
        "lock_sha256": projection.lock_sha256,
        "generator_id": projection.generator_id,
        "generator_version": projection.generator_version,
        "resolution_order": list(projection.resolution_order),
        "member_component_ids": list(projection.member_component_ids),
        "acknowledged_implications": list(projection.acknowledged_implications),
        "owned_obligations": list(projection.owned_obligations),
        "smoke_evidence": list(projection.smoke_evidence),
        "health_evidence": list(projection.health_evidence),
        "capabilities": [_provider_json(provider) for provider in projection.capabilities],
        "projection_sha256": projection.projection_sha256 if include_digest else None,
    }


def parse_projection(value: object) -> NavigatorProjection:
    data = _require_mapping(value, path="projection")
    _require_closed(
        data,
        keys=(
            "schema_version",
            "assembly_id",
            "manifest_sha256",
            "catalogue_sha256",
            "raw_lock_sha256",
            "lock_sha256",
            "generator_id",
            "generator_version",
            "resolution_order",
            "member_component_ids",
            "acknowledged_implications",
            "owned_obligations",
            "smoke_evidence",
            "health_evidence",
            "capabilities",
            "projection_sha256",
        ),
        path="projection",
    )
    if _require_int(data["schema_version"], path="projection.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "projection.schema_version", "unsupported schema version")
    incoming = _require_sha(data["projection_sha256"], path="projection.projection_sha256")
    preimage = dict(data)
    preimage["projection_sha256"] = None
    expected = f"sha256:{_sha256_json(preimage)}"
    if incoming != expected:
        _fail(NavigatorErrorCode.PROJECTION_DIGEST_MISMATCH, "projection.projection_sha256", "projection digest mismatch")
    capabilities = tuple(_parse_provider(item, path=f"projection.capabilities[{idx}]") for idx, item in enumerate(_require_sequence(data.get("capabilities"), path="projection.capabilities")))
    if not capabilities:
        _fail(NavigatorErrorCode.MISSING_PROVIDER_DATA, "projection.capabilities", "projection has no providers")
    _validate_navigator_provider_selections(capabilities, path="projection.capabilities")
    return NavigatorProjection(
        schema_version=SCHEMA_VERSION,
        assembly_id=_require_stable_id(data.get("assembly_id"), path="projection.assembly_id") or "",
        manifest_sha256=_require_sha(data.get("manifest_sha256"), path="projection.manifest_sha256") or "",
        catalogue_sha256=_require_sha(data.get("catalogue_sha256"), path="projection.catalogue_sha256") or "",
        raw_lock_sha256=_require_sha(data.get("raw_lock_sha256"), path="projection.raw_lock_sha256") or "",
        lock_sha256=_require_sha(data.get("lock_sha256"), path="projection.lock_sha256") or "",
        generator_id=_require_string(data.get("generator_id"), path="projection.generator_id") or "",
        generator_version=_require_string(data.get("generator_version"), path="projection.generator_version") or "",
        resolution_order=_string_tuple(data.get("resolution_order"), path="projection.resolution_order"),
        member_component_ids=_stable_tuple(data.get("member_component_ids"), path="projection.member_component_ids"),
        acknowledged_implications=_string_tuple(data.get("acknowledged_implications"), path="projection.acknowledged_implications"),
        owned_obligations=_string_tuple(data.get("owned_obligations"), path="projection.owned_obligations"),
        smoke_evidence=_string_tuple(data.get("smoke_evidence"), path="projection.smoke_evidence"),
        health_evidence=_string_tuple(data.get("health_evidence"), path="projection.health_evidence"),
        capabilities=capabilities,
        projection_sha256=incoming or "",
    )


def _require_sequence(value: object, *, path: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, path, "expected array")
    return value


def _parse_provider(value: object, *, path: str) -> NavigatorCapabilityProvider:
    data = _require_mapping(value, path=path)
    provider_required_keys = (
        "capability_id",
        "provider_component_id",
        "contract",
        "schema",
        "version",
        "enabled_features",
        "requirement_closure",
        "equivalence_class",
        "trust_class",
        "source_identity",
        "compatibility_evidence",
        "implications",
        "upkeep",
    )
    _require_closed_compatible(
        data,
        required_keys=provider_required_keys,
        allowed_keys=provider_required_keys
        + ("deployment_trust_zone", "subject_visibility", "navigator_provider_selection"),
        path=path,
    )
    return NavigatorCapabilityProvider(
        capability_id=_require_stable_id(data.get("capability_id"), path=f"{path}.capability_id") or "",
        provider_component_id=_require_stable_id(data.get("provider_component_id"), path=f"{path}.provider_component_id") or "",
        contract=_require_stable_id(data.get("contract"), path=f"{path}.contract") or "",
        schema=_require_stable_id(data.get("schema"), path=f"{path}.schema") or "",
        version=_require_semver(data.get("version"), path=f"{path}.version") or "",
        enabled_features=_stable_tuple(data.get("enabled_features"), path=f"{path}.enabled_features"),
        requirement_closure=_stable_tuple(data.get("requirement_closure"), path=f"{path}.requirement_closure"),
        equivalence_class=_require_string(data.get("equivalence_class"), path=f"{path}.equivalence_class", nullable=True),
        trust_class=_require_string(data.get("trust_class"), path=f"{path}.trust_class", nullable=True),
        deployment_trust_zone=_require_string(
            data.get("deployment_trust_zone"),
            path=f"{path}.deployment_trust_zone",
            nullable=True,
        ),
        subject_visibility=_parse_subject_visibility(
            data.get("subject_visibility"), path=f"{path}.subject_visibility"
        ),
        navigator_provider_selection=_parse_navigator_provider_selection(
            data.get("navigator_provider_selection"),
            path=f"{path}.navigator_provider_selection",
        ),
        source_identity=_parse_source_identity(data.get("source_identity"), path=f"{path}.source_identity"),
        compatibility_evidence=_string_tuple(data.get("compatibility_evidence"), path=f"{path}.compatibility_evidence"),
        implications=tuple(
            _parse_implication(item, path=f"{path}.implications[{idx}]")
            for idx, item in enumerate(_require_sequence(data.get("implications"), path=f"{path}.implications"))
        ),
        upkeep=_parse_upkeep(data.get("upkeep"), path=f"{path}.upkeep"),
    )


def _parse_subject_visibility(
    value: object, *, path: str
) -> NavigatorSubjectVisibilityPolicy | None:
    if value is None:
        return None
    data = _require_mapping(value, path=path)
    _require_closed(data, keys=cc.SUBJECT_VISIBILITY_KEYS, path=path)
    dimensions = {
        key: _subject_visibility_tuple(data.get(key), path=f"{path}.{key}")
        for key in ("subject_refs", "role_refs", "instance_refs", "trust_zones")
    }
    if not any(dimensions.values()):
        _fail(
            NavigatorErrorCode.SCHEMA_REQUIRED_FIELD,
            path,
            "requires at least one subject, role, instance, or trust zone",
        )
    return NavigatorSubjectVisibilityPolicy(
        subject_refs=dimensions["subject_refs"],
        role_refs=dimensions["role_refs"],
        instance_refs=dimensions["instance_refs"],
        trust_zones=dimensions["trust_zones"],
        policy_refs=_subject_visibility_tuple(
            data.get("policy_refs"),
            path=f"{path}.policy_refs",
            require_nonempty=True,
        ),
        evidence_refs=_subject_visibility_tuple(
            data.get("evidence_refs"),
            path=f"{path}.evidence_refs",
            require_nonempty=True,
        ),
    )


def _parse_source_identity(value: object, *, path: str) -> SourceIdentity:
    data = _require_mapping(value, path=path)
    _require_closed(
        data,
        keys=("logical_repo", "path", "artifact", "record_sha256", "tested_revision", "locked_revision", "artifact_digest"),
        path=path,
    )
    return SourceIdentity(
        logical_repo=_require_stable_id(data.get("logical_repo"), path=f"{path}.logical_repo") or "",
        path=_require_string(data.get("path"), path=f"{path}.path", nullable=True),
        artifact=_require_string(data.get("artifact"), path=f"{path}.artifact", nullable=True),
        record_sha256=_require_sha(data.get("record_sha256"), path=f"{path}.record_sha256") or "",
        tested_revision=_require_string(data.get("tested_revision"), path=f"{path}.tested_revision") or "",
        locked_revision=_require_string(data.get("locked_revision"), path=f"{path}.locked_revision") or "",
        artifact_digest=_require_string(data.get("artifact_digest"), path=f"{path}.artifact_digest") or "",
    )


def _parse_implication(value: object, *, path: str) -> NavigatorImplication:
    data = _require_mapping(value, path=path)
    _require_closed(data, keys=("kind", "implication_id", "scope", "evidence_refs"), path=path)
    return NavigatorImplication(
        kind=_require_string(data.get("kind"), path=f"{path}.kind") or "",
        implication_id=_require_stable_id(data.get("implication_id"), path=f"{path}.implication_id") or "",
        scope=_require_string(data.get("scope"), path=f"{path}.scope") or "",
        evidence_refs=_string_tuple(data.get("evidence_refs"), path=f"{path}.evidence_refs"),
    )


def _parse_cost(value: object, *, path: str) -> NavigatorRecurringCost:
    data = _require_mapping(value, path=path)
    _require_closed(
        data,
        keys=("cost_id", "category", "quantity", "unit", "cadence", "measurement_source", "owner", "alternative_ref", "last_measured_at", "review_due_at"),
        path=path,
    )
    return NavigatorRecurringCost(
        cost_id=_require_stable_id(data.get("cost_id"), path=f"{path}.cost_id") or "",
        category=_require_string(data.get("category"), path=f"{path}.category") or "",
        quantity=_require_number(data.get("quantity"), path=f"{path}.quantity"),
        unit=_require_string(data.get("unit"), path=f"{path}.unit") or "",
        cadence=_require_string(data.get("cadence"), path=f"{path}.cadence") or "",
        measurement_source=_require_string(data.get("measurement_source"), path=f"{path}.measurement_source") or "",
        owner=_require_string(data.get("owner"), path=f"{path}.owner") or "",
        alternative_ref=_require_string(data.get("alternative_ref"), path=f"{path}.alternative_ref", nullable=True),
        last_measured_at=_require_string(data.get("last_measured_at"), path=f"{path}.last_measured_at") or "",
        review_due_at=_require_string(data.get("review_due_at"), path=f"{path}.review_due_at") or "",
    )


def _parse_upkeep(value: object, *, path: str) -> NavigatorUpkeep:
    data = _require_mapping(value, path=path)
    _require_closed(
        data,
        keys=("steward", "health_evidence_refs", "monitoring_refs", "upgrade_rollback_refs", "credential_rotation_refs", "storage_retention_refs", "costs", "review_obligations"),
        path=path,
    )
    return NavigatorUpkeep(
        steward=_require_string(data.get("steward"), path=f"{path}.steward") or "",
        health_evidence_refs=_string_tuple(data.get("health_evidence_refs"), path=f"{path}.health_evidence_refs"),
        monitoring_refs=_string_tuple(data.get("monitoring_refs"), path=f"{path}.monitoring_refs"),
        upgrade_rollback_refs=_string_tuple(data.get("upgrade_rollback_refs"), path=f"{path}.upgrade_rollback_refs"),
        credential_rotation_refs=_string_tuple(data.get("credential_rotation_refs"), path=f"{path}.credential_rotation_refs"),
        storage_retention_refs=_string_tuple(data.get("storage_retention_refs"), path=f"{path}.storage_retention_refs"),
        costs=tuple(
            _parse_cost(item, path=f"{path}.costs[{idx}]")
            for idx, item in enumerate(_require_sequence(data.get("costs"), path=f"{path}.costs"))
        ),
        review_obligations=_string_tuple(data.get("review_obligations"), path=f"{path}.review_obligations"),
    )


def request_to_json(request: NavigatorRequest) -> dict[str, object]:
    return {
        "schema_version": request.schema_version,
        "method": request.method,
        "request_id": request.request_id,
        "context_budget": _budget_json(request.context_budget),
        "payload": dict(request.payload),
    }


def parse_request(value: object, *, auth: AuthenticatedContext) -> NavigatorRequest:
    if not isinstance(auth, AuthenticatedContext):
        _fail(NavigatorErrorCode.SCHEMA_TYPE, "auth", "AuthenticatedContext is required")
    _validate_json_value(value, path="request")
    _reject_identity_lookalikes(value, path="request")
    data = _require_mapping(value, path="request")
    _require_closed(data, keys=("schema_version", "method", "request_id", "context_budget", "payload"), path="request")
    if _require_int(data["schema_version"], path="request.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "request.schema_version", "unsupported schema version")
    method = _require_string(data["method"], path="request.method") or ""
    if method not in NAVIGATOR_METHODS:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "request.method", "unsupported method")
    budget = intersect_budget(_require_mapping(data["context_budget"], path="request.context_budget"))
    payload = dict(_require_mapping(data["payload"], path="request.payload"))
    _validate_request_payload(method, payload)
    request = NavigatorRequest(
        schema_version=SCHEMA_VERSION,
        method=method,
        request_id=_require_string(data["request_id"], path="request.request_id") or "",
        payload=payload,
        context_budget=budget,
    )
    if len(canonical_json_bytes(request_to_json(request))) > budget.max_total_bytes:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "request", "request exceeds max_total_bytes")
    return request


def _validate_request_payload(method: str, payload: Mapping[str, object]) -> None:
    if method == "search":
        _require_closed(payload, keys=("query", "constraints"), path="request.payload")
        query = _require_mapping(payload["query"], path="request.payload.query")
        _require_closed(query, keys=("text",), path="request.payload.query")
        _require_string(query["text"], path="request.payload.query.text")
        constraints = _require_mapping(payload["constraints"], path="request.payload.constraints")
        _require_closed(
            constraints,
            keys=(
                "capability_ids",
                "provider_component_ids",
                "contracts",
                "schemas",
                "versions",
                "features",
                "equivalence_classes",
                "trust_classes",
                "implication_scopes",
                "require_declared_equivalence",
            ),
            path="request.payload.constraints",
        )
        for key in (
            "capability_ids",
            "provider_component_ids",
            "contracts",
            "schemas",
            "features",
        ):
            _stable_tuple(constraints[key], path=f"request.payload.constraints.{key}")
        _semver_tuple(constraints["versions"], path="request.payload.constraints.versions")
        for key in (
            "equivalence_classes",
            "trust_classes",
            "implication_scopes",
        ):
            _string_tuple(constraints[key], path=f"request.payload.constraints.{key}")
        _require_bool(
            constraints["require_declared_equivalence"],
            path="request.payload.constraints.require_declared_equivalence",
        )
    elif method == "describe":
        _require_closed(payload, keys=("capability_id", "requested_detail"), path="request.payload")
        _require_stable_id(payload["capability_id"], path="request.payload.capability_id")
        if payload["requested_detail"] not in ("summary", "contract", "evidence", "full"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "request.payload.requested_detail", "unsupported detail")
    elif method == "plan":
        _require_closed(payload, keys=("capability_id", "typed_input", "bounded_purpose"), path="request.payload")
        _require_stable_id(payload["capability_id"], path="request.payload.capability_id")
        _require_string(payload["bounded_purpose"], path="request.payload.bounded_purpose")
        typed_bytes = canonical_json_bytes(payload["typed_input"])
        if len(typed_bytes) > MAX_TYPED_INPUT_BYTES:
            _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "request.payload.typed_input", "typed_input too large")
    elif method == "invoke":
        _require_closed(payload, keys=("plan_or_handle",), path="request.payload")
        handle = _require_mapping(payload["plan_or_handle"], path="request.payload.plan_or_handle")
        _require_closed(handle, keys=("kind", "handle_ref", "plan_digest"), path="request.payload.plan_or_handle")
        if handle["kind"] not in ("effect-plan", "invocation-handle"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "request.payload.plan_or_handle.kind", "unsupported handle kind")
        _require_string(handle["handle_ref"], path="request.payload.plan_or_handle.handle_ref", nullable=True)
        _require_sha(handle["plan_digest"], path="request.payload.plan_or_handle.plan_digest")
    elif method == "status":
        _require_closed(payload, keys=("handle",), path="request.payload")
        handle = _require_mapping(payload["handle"], path="request.payload.handle")
        _require_closed(handle, keys=("handle_ref", "handle_digest"), path="request.payload.handle")
        _require_string(handle["handle_ref"], path="request.payload.handle.handle_ref")
        _require_sha(handle["handle_digest"], path="request.payload.handle.handle_digest")
    elif method == "cancel":
        _require_closed(payload, keys=("handle", "bounded_reason"), path="request.payload")
        handle = _require_mapping(payload["handle"], path="request.payload.handle")
        _require_closed(handle, keys=("handle_ref", "handle_digest"), path="request.payload.handle")
        _require_string(handle["handle_ref"], path="request.payload.handle.handle_ref")
        _require_sha(handle["handle_digest"], path="request.payload.handle.handle_digest")
        _require_string(payload["bounded_reason"], path="request.payload.bounded_reason", nullable=True)


def result_to_json(result: NavigatorResult) -> dict[str, object]:
    return {
        "schema_version": result.schema_version,
        "method": result.method,
        "request_id": result.request_id,
        "status": result.status,
        "subject_ref": result.subject_ref,
        "projection_sha256": result.projection_sha256,
        "catalogue_sha256": result.catalogue_sha256,
        "lock_sha256": result.lock_sha256,
        "raw_lock_sha256": result.raw_lock_sha256,
        "created_at": result.created_at,
        "provider_evidence_refs": list(result.provider_evidence_refs),
        "payload": dict(result.payload),
    }


def parse_result(value: object) -> NavigatorResult:
    _reject_forbidden_result_content(value, path="result")
    data = _require_mapping(value, path="result")
    _require_closed(
        data,
        keys=(
            "schema_version",
            "method",
            "request_id",
            "status",
            "subject_ref",
            "projection_sha256",
            "catalogue_sha256",
            "lock_sha256",
            "raw_lock_sha256",
            "created_at",
            "provider_evidence_refs",
            "payload",
        ),
        path="result",
    )
    method = _require_string(data["method"], path="result.method") or ""
    if method not in NAVIGATOR_METHODS:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.method", "unsupported method")
    if _require_int(data["schema_version"], path="result.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "result.schema_version", "unsupported schema version")
    if data["status"] not in ("ok", "refused", "pending", "failed"):
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.status", "unsupported status")
    payload = _require_mapping(data["payload"], path="result.payload")
    _validate_result_payload(method, payload)
    refs = _bounded_result_evidence_refs(data["provider_evidence_refs"], path="result.provider_evidence_refs")
    if len(canonical_json_bytes(value)) > SERVER_CEILINGS.max_total_bytes:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "result", "result exceeds max_total_bytes")
    return NavigatorResult(
        schema_version=SCHEMA_VERSION,
        method=method,
        request_id=_require_string(data["request_id"], path="result.request_id") or "",
        status=_require_string(data["status"], path="result.status") or "",
        subject_ref=_require_string(data["subject_ref"], path="result.subject_ref") or "",
        projection_sha256=_require_sha(data["projection_sha256"], path="result.projection_sha256") or "",
        catalogue_sha256=_require_sha(data["catalogue_sha256"], path="result.catalogue_sha256") or "",
        lock_sha256=_require_sha(data["lock_sha256"], path="result.lock_sha256") or "",
        raw_lock_sha256=_require_sha(data["raw_lock_sha256"], path="result.raw_lock_sha256") or "",
        created_at=_require_string(data["created_at"], path="result.created_at") or "",
        payload=dict(payload),
        provider_evidence_refs=refs,
    )


def _validate_result_payload(method: str, payload: Mapping[str, object]) -> None:
    if method == "search":
        _require_closed(payload, keys=("candidates", "truncated"), path="result.payload")
        candidates = _require_sequence(payload["candidates"], path="result.payload.candidates")
        if len(candidates) > SERVER_CEILINGS.max_candidates:
            _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "result.payload.candidates", "too many candidates")
        for idx, candidate in enumerate(candidates):
            item = _require_mapping(candidate, path=f"result.payload.candidates[{idx}]")
            _require_closed(
                item,
                keys=("capability_id", "provider_component_id", "contract", "schema", "version", "redacted_summary", "match_refs", "projection_ref"),
                path=f"result.payload.candidates[{idx}]",
            )
            _require_result_identity(item, path=f"result.payload.candidates[{idx}]")
            _require_summary(item["redacted_summary"], path=f"result.payload.candidates[{idx}].redacted_summary")
            _string_tuple(item["match_refs"], path=f"result.payload.candidates[{idx}].match_refs")
            _require_sha(item["projection_ref"], path=f"result.payload.candidates[{idx}].projection_ref")
        _require_bool(payload["truncated"], path="result.payload.truncated")
    elif method == "describe":
        _require_closed(payload, keys=("capability_id", "provider_component_id", "contract", "schema", "version", "detail_level", "contract_slice"), path="result.payload")
        _require_result_identity(payload, path="result.payload")
        if payload["detail_level"] not in ("summary", "contract", "evidence", "full"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.payload.detail_level", "unsupported detail")
        contract_slice = _require_mapping(payload["contract_slice"], path="result.payload.contract_slice")
        _require_closed(contract_slice, keys=("redacted_summary", "contract_digest", "evidence_refs"), path="result.payload.contract_slice")
        _require_summary(contract_slice["redacted_summary"], path="result.payload.contract_slice.redacted_summary")
        _require_sha(contract_slice["contract_digest"], path="result.payload.contract_slice.contract_digest")
        _bounded_result_evidence_refs(contract_slice["evidence_refs"], path="result.payload.contract_slice.evidence_refs")
    elif method == "plan":
        _require_closed(payload, keys=("capability_id", "provider_component_id", "contract", "schema", "version", "effect_digest", "redacted_effect_summary", "handle_ref", "receipt_ref"), path="result.payload")
        _require_result_identity(payload, path="result.payload")
        _require_sha(payload["effect_digest"], path="result.payload.effect_digest")
        _require_summary(payload["redacted_effect_summary"], path="result.payload.redacted_effect_summary")
        _require_string(payload["handle_ref"], path="result.payload.handle_ref", nullable=True)
        _require_string(payload["receipt_ref"], path="result.payload.receipt_ref", nullable=True)
    elif method == "invoke":
        _require_closed(payload, keys=("capability_id", "provider_component_id", "contract", "schema", "version", "result_kind", "effect_digest", "handle_ref", "receipt_ref", "redacted_summary"), path="result.payload")
        _require_result_identity(payload, path="result.payload")
        if payload["result_kind"] not in ("receipt", "handle", "refusal"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.payload.result_kind", "unsupported result kind")
        _require_sha(payload["effect_digest"], path="result.payload.effect_digest", nullable=True)
        _require_string(payload["handle_ref"], path="result.payload.handle_ref", nullable=True)
        _require_string(payload["receipt_ref"], path="result.payload.receipt_ref", nullable=True)
        _require_summary(payload["redacted_summary"], path="result.payload.redacted_summary")
    elif method == "status":
        _require_closed(payload, keys=("capability_id", "provider_component_id", "contract", "schema", "version", "handle_ref", "lifecycle_status", "effect_digest", "receipt_ref", "updated_at", "redacted_summary"), path="result.payload")
        _require_result_identity(payload, path="result.payload")
        _require_string(payload["handle_ref"], path="result.payload.handle_ref")
        if payload["lifecycle_status"] not in ("pending", "running", "succeeded", "failed", "unknown", "provider-unavailable"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.payload.lifecycle_status", "unsupported lifecycle status")
        _require_sha(payload["effect_digest"], path="result.payload.effect_digest", nullable=True)
        _require_string(payload["receipt_ref"], path="result.payload.receipt_ref", nullable=True)
        _require_string(payload["updated_at"], path="result.payload.updated_at", nullable=True)
        _require_summary(payload["redacted_summary"], path="result.payload.redacted_summary")
    elif method == "cancel":
        _require_closed(payload, keys=("capability_id", "provider_component_id", "contract", "schema", "version", "handle_ref", "cancellation_status", "effect_digest", "receipt_ref", "updated_at", "redacted_summary"), path="result.payload")
        _require_result_identity(payload, path="result.payload")
        _require_string(payload["handle_ref"], path="result.payload.handle_ref")
        if payload["cancellation_status"] not in ("accepted", "refused", "already-terminal", "provider-unavailable"):
            _fail(NavigatorErrorCode.SCHEMA_ENUM, "result.payload.cancellation_status", "unsupported cancellation status")
        _require_sha(payload["effect_digest"], path="result.payload.effect_digest", nullable=True)
        _require_string(payload["receipt_ref"], path="result.payload.receipt_ref", nullable=True)
        _require_string(payload["updated_at"], path="result.payload.updated_at", nullable=True)
        _require_summary(payload["redacted_summary"], path="result.payload.redacted_summary")


def _require_result_identity(value: Mapping[str, object], *, path: str) -> None:
    _require_stable_id(value["capability_id"], path=f"{path}.capability_id")
    _require_stable_id(value["provider_component_id"], path=f"{path}.provider_component_id")
    _require_stable_id(value["contract"], path=f"{path}.contract")
    _require_stable_id(value["schema"], path=f"{path}.schema")
    _require_semver(value["version"], path=f"{path}.version")


def _bounded_result_evidence_refs(value: object, *, path: str) -> tuple[str, ...]:
    refs = _string_tuple(value, path=path)
    if len(refs) > SERVER_CEILINGS.max_provider_evidence_refs:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, path, "too many evidence refs")
    return refs


def _receipt_evidence_refs(value: object, *, path: str) -> tuple[str, ...]:
    refs = _bounded_result_evidence_refs(value, path=path)
    if not refs:
        _fail(NavigatorErrorCode.SCHEMA_REQUIRED_FIELD, path, "evidence refs required")
    return refs


def receipt_to_json(receipt: NavigatorReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "method": receipt.method,
        "status": receipt.status,
        "subject_ref": receipt.subject_ref,
        "capability_id": receipt.capability_id,
        "provider_component_id": receipt.provider_component_id,
        "contract": receipt.contract,
        "schema": receipt.schema,
        "version": receipt.version,
        "projection_sha256": receipt.projection_sha256,
        "catalogue_sha256": receipt.catalogue_sha256,
        "lock_sha256": receipt.lock_sha256,
        "raw_lock_sha256": receipt.raw_lock_sha256,
        "effect_digest": receipt.effect_digest,
        "redacted_summary": receipt.redacted_summary,
        "provider_native_evidence_refs": list(receipt.provider_native_evidence_refs),
        "authority_provenance_ref": receipt.authority_provenance_ref,
        "created_at": receipt.created_at,
        "updated_at": receipt.updated_at,
        "prior_handle_ref": receipt.prior_handle_ref,
        "prior_receipt_ref": receipt.prior_receipt_ref,
    }


def parse_receipt(value: object) -> NavigatorReceipt:
    _reject_forbidden_result_content(value, path="receipt")
    if len(canonical_json_bytes(value)) > SERVER_CEILINGS.max_total_bytes:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "receipt", "receipt exceeds max_total_bytes")
    data = _require_mapping(value, path="receipt")
    keys = (
        "schema_version",
        "receipt_id",
        "method",
        "status",
        "subject_ref",
        "capability_id",
        "provider_component_id",
        "contract",
        "schema",
        "version",
        "projection_sha256",
        "catalogue_sha256",
        "lock_sha256",
        "raw_lock_sha256",
        "effect_digest",
        "redacted_summary",
        "provider_native_evidence_refs",
        "authority_provenance_ref",
        "created_at",
        "updated_at",
        "prior_handle_ref",
        "prior_receipt_ref",
    )
    _require_closed(data, keys=keys, path="receipt")
    method = _require_string(data["method"], path="receipt.method") or ""
    if method not in NAVIGATOR_METHODS:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "receipt.method", "unsupported method")
    if _require_int(data["schema_version"], path="receipt.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "receipt.schema_version", "unsupported schema version")
    if data["status"] not in ("pending", "running", "succeeded", "failed", "unknown", "provider-unavailable", "accepted", "refused", "already-terminal"):
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "receipt.status", "unsupported status")
    refs = _receipt_evidence_refs(data["provider_native_evidence_refs"], path="receipt.provider_native_evidence_refs")
    return NavigatorReceipt(
        schema_version=SCHEMA_VERSION,
        receipt_id=_require_string(data["receipt_id"], path="receipt.receipt_id") or "",
        method=method,
        status=_require_string(data["status"], path="receipt.status") or "",
        subject_ref=_require_string(data["subject_ref"], path="receipt.subject_ref") or "",
        capability_id=_require_stable_id(data["capability_id"], path="receipt.capability_id") or "",
        provider_component_id=_require_stable_id(data["provider_component_id"], path="receipt.provider_component_id") or "",
        contract=_require_stable_id(data["contract"], path="receipt.contract") or "",
        schema=_require_stable_id(data["schema"], path="receipt.schema") or "",
        version=_require_semver(data["version"], path="receipt.version") or "",
        projection_sha256=_require_sha(data["projection_sha256"], path="receipt.projection_sha256") or "",
        catalogue_sha256=_require_sha(data["catalogue_sha256"], path="receipt.catalogue_sha256") or "",
        lock_sha256=_require_sha(data["lock_sha256"], path="receipt.lock_sha256") or "",
        raw_lock_sha256=_require_sha(data["raw_lock_sha256"], path="receipt.raw_lock_sha256") or "",
        effect_digest=_require_sha(data["effect_digest"], path="receipt.effect_digest", nullable=True),
        redacted_summary=_require_summary(data["redacted_summary"], path="receipt.redacted_summary"),
        provider_native_evidence_refs=refs,
        authority_provenance_ref=_require_string(data["authority_provenance_ref"], path="receipt.authority_provenance_ref", nullable=True),
        created_at=_require_string(data["created_at"], path="receipt.created_at") or "",
        updated_at=_require_string(data["updated_at"], path="receipt.updated_at", nullable=True),
        prior_handle_ref=_require_string(data["prior_handle_ref"], path="receipt.prior_handle_ref", nullable=True),
        prior_receipt_ref=_require_string(data["prior_receipt_ref"], path="receipt.prior_receipt_ref", nullable=True),
    )


def _schema_object(required: Sequence[str], properties: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _array(items: object, *, min_items: int = 0, max_items: int | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "array", "items": items}
    if min_items:
        result["minItems"] = min_items
    if max_items is not None:
        result["maxItems"] = max_items
    return result


def _nullable(schema: object) -> dict[str, object]:
    return {"anyOf": [{"type": "null"}, schema]}


def _subject_visibility_array(items: object, *, min_items: int = 0) -> dict[str, object]:
    result = _array(
        {
            **dict(items),  # type: ignore[arg-type]
            "not": {"enum": sorted(cc.FORBIDDEN_SUBJECT_VISIBILITY_TOKENS)},
        },
        min_items=min_items,
    )
    result["uniqueItems"] = True
    return result


def _subject_visibility_schema(text: object) -> dict[str, object]:
    subject_visibility = _schema_object(
        cc.SUBJECT_VISIBILITY_KEYS,
        {
            "subject_refs": _subject_visibility_array(text),
            "role_refs": _subject_visibility_array(text),
            "instance_refs": _subject_visibility_array(text),
            "trust_zones": _subject_visibility_array(text),
            "policy_refs": _subject_visibility_array(text, min_items=1),
            "evidence_refs": _subject_visibility_array(text, min_items=1),
        },
    )
    subject_visibility["anyOf"] = [
        {"properties": {key: {"minItems": 1}}}
        for key in ("subject_refs", "role_refs", "instance_refs", "trust_zones")
    ]
    return _nullable(subject_visibility)


def _navigator_provider_selection_schema(text: object, stable: object) -> dict[str, object]:
    selection = _schema_object(
        cc.NAVIGATOR_PROVIDER_SELECTION_KEYS,
        {
            "role": {"enum": [item.value for item in cc.NavigatorProviderRole]},
            "primary_provider_component_id": _nullable(stable),
            "fallback_rank": _nullable({"type": "integer", "minimum": 1}),
            "provider_organ_id": {**dict(text), "pattern": cc.ORGAN_REF_PATTERN},  # type: ignore[arg-type]
            "health_dependency_id": {**dict(text), "pattern": cc.DEPENDENCY_REF_PATTERN},  # type: ignore[arg-type]
        },
    )
    selection["allOf"] = [
        {
            "if": {"properties": {"role": {"const": cc.NavigatorProviderRole.PRIMARY.value}}},
            "then": {
                "properties": {
                    "primary_provider_component_id": {"type": "null"},
                    "fallback_rank": {"type": "null"},
                }
            },
        },
        {
            "if": {"properties": {"role": {"const": cc.NavigatorProviderRole.FALLBACK.value}}},
            "then": {
                "properties": {
                    "primary_provider_component_id": stable,
                    "fallback_rank": {"type": "integer", "minimum": 1},
                }
            },
        },
    ]
    return _nullable(selection)


def _defs() -> dict[str, object]:
    text = {"type": "string", "minLength": 1, "maxLength": MAX_METADATA_STRING_BYTES}
    summary = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_RESULT_SUMMARY_BYTES,
        "description": "Necessary character bound; runtime validators enforce the exact 256 UTF-8-byte ceiling.",
    }
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    semver = {"type": "string", "pattern": VERSION_PATTERN}
    sha = {"type": "string", "pattern": SHA256_PATTERN}
    budget = _schema_object(
        _BUDGET_FIELDS,
        {
            "max_candidates": {"type": "integer", "minimum": 1},
            "max_summary_bytes": {"type": "integer", "minimum": 1},
            "max_contract_bytes": {"type": "integer", "minimum": 1},
            "max_total_bytes": {"type": "integer", "minimum": 512},
            "max_provider_evidence_refs": {"type": "integer", "minimum": 1},
        },
    )
    return {"text": text, "summary": summary, "stable": stable, "semver": semver, "sha": sha, "budget": budget}


def _schema_base(schema_id: str, properties: Mapping[str, object], required: Sequence[str], *, defs: Mapping[str, object] | None = None, extra: Mapping[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "$id": schema_id,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
    }
    if properties or required:
        result["additionalProperties"] = False
        result["required"] = list(required)
        result["properties"] = dict(properties)
    if defs:
        result["$defs"] = dict(defs)
    if extra:
        result.update(extra)
    return result


def _projection_schema() -> dict[str, object]:
    defs = _defs()
    text = {"$ref": "#/$defs/text"}
    sha = {"$ref": "#/$defs/sha"}
    source = _schema_object(
        ("logical_repo", "path", "artifact", "record_sha256", "tested_revision", "locked_revision", "artifact_digest"),
        {
            "logical_repo": {"$ref": "#/$defs/stable"},
            "path": _nullable(text),
            "artifact": _nullable(text),
            "record_sha256": sha,
            "tested_revision": text,
            "locked_revision": text,
            "artifact_digest": text,
        },
    )
    implication = _schema_object(
        ("kind", "implication_id", "scope", "evidence_refs"),
        {"kind": text, "implication_id": {"$ref": "#/$defs/stable"}, "scope": text, "evidence_refs": _array(text)},
    )
    cost = _schema_object(
        ("cost_id", "category", "quantity", "unit", "cadence", "measurement_source", "owner", "alternative_ref", "last_measured_at", "review_due_at"),
        {
            "cost_id": {"$ref": "#/$defs/stable"},
            "category": text,
            "quantity": {"type": "number"},
            "unit": text,
            "cadence": text,
            "measurement_source": text,
            "owner": text,
            "alternative_ref": _nullable(text),
            "last_measured_at": text,
            "review_due_at": text,
        },
    )
    upkeep = _schema_object(
        ("steward", "health_evidence_refs", "monitoring_refs", "upgrade_rollback_refs", "credential_rotation_refs", "storage_retention_refs", "costs", "review_obligations"),
        {
            "steward": text,
            "health_evidence_refs": _array(text),
            "monitoring_refs": _array(text),
            "upgrade_rollback_refs": _array(text),
            "credential_rotation_refs": _array(text),
            "storage_retention_refs": _array(text),
            "costs": _array(cost),
            "review_obligations": _array(text),
        },
    )
    capability = _schema_object(
        ("capability_id", "provider_component_id", "contract", "schema", "version", "enabled_features", "requirement_closure", "equivalence_class", "trust_class", "source_identity", "compatibility_evidence", "implications", "upkeep"),
        {
            "capability_id": {"$ref": "#/$defs/stable"},
            "provider_component_id": {"$ref": "#/$defs/stable"},
            "contract": {"$ref": "#/$defs/stable"},
            "schema": {"$ref": "#/$defs/stable"},
            "version": {"$ref": "#/$defs/semver"},
            "enabled_features": _array({"$ref": "#/$defs/stable"}),
            "requirement_closure": _array({"$ref": "#/$defs/stable"}),
            "equivalence_class": _nullable(text),
            "trust_class": _nullable(text),
            "deployment_trust_zone": _nullable(text),
            "subject_visibility": _subject_visibility_schema(text),
            "navigator_provider_selection": _navigator_provider_selection_schema(
                text,
                {"$ref": "#/$defs/stable"},
            ),
            "source_identity": source,
            "compatibility_evidence": _array(text),
            "implications": _array(implication),
            "upkeep": upkeep,
        },
    )
    return _schema_base(
        PROJECTION_SCHEMA_ID,
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "assembly_id": {"$ref": "#/$defs/stable"},
            "manifest_sha256": sha,
            "catalogue_sha256": sha,
            "raw_lock_sha256": sha,
            "lock_sha256": sha,
            "generator_id": text,
            "generator_version": text,
            "resolution_order": _array(text, min_items=len(ar.RESOLUTION_ORDER)),
            "member_component_ids": _array({"$ref": "#/$defs/stable"}, min_items=1),
            "acknowledged_implications": _array(text),
            "owned_obligations": _array(text),
            "smoke_evidence": _array(text),
            "health_evidence": _array(text),
            "capabilities": _array(capability, min_items=1),
            "projection_sha256": sha,
        },
        ("schema_version", "assembly_id", "manifest_sha256", "catalogue_sha256", "raw_lock_sha256", "lock_sha256", "generator_id", "generator_version", "resolution_order", "member_component_ids", "acknowledged_implications", "owned_obligations", "smoke_evidence", "health_evidence", "capabilities", "projection_sha256"),
        defs=defs,
    )


def _request_schema() -> dict[str, object]:
    defs = _defs()
    text = {"$ref": "#/$defs/text"}
    stable = {"$ref": "#/$defs/stable"}
    json_value: dict[str, object] = {"description": "Opaque canonical JSON value"}
    common = {
        "schema_version": {"const": SCHEMA_VERSION},
        "request_id": text,
        "context_budget": {"$ref": "#/$defs/budget"},
    }
    branches = []
    for method in NAVIGATOR_METHODS:
        payload: dict[str, object]
        required: tuple[str, ...]
        if method == "search":
            payload = {
                "query": _schema_object(("text",), {"text": text}),
                "constraints": _schema_object(
                    ("capability_ids", "provider_component_ids", "contracts", "schemas", "versions", "features", "equivalence_classes", "trust_classes", "implication_scopes", "require_declared_equivalence"),
                    {
                        "capability_ids": _array(stable),
                        "provider_component_ids": _array(stable),
                        "contracts": _array(stable),
                        "schemas": _array(stable),
                        "versions": _array({"$ref": "#/$defs/semver"}),
                        "features": _array(stable),
                        "equivalence_classes": _array(text),
                        "trust_classes": _array(text),
                        "implication_scopes": _array(text),
                        "require_declared_equivalence": {"type": "boolean"},
                    },
                ),
            }
            required = ("query", "constraints")
        elif method == "describe":
            payload = {"capability_id": stable, "requested_detail": {"enum": ["summary", "contract", "evidence", "full"]}}
            required = ("capability_id", "requested_detail")
        elif method == "plan":
            payload = {"capability_id": stable, "typed_input": json_value, "bounded_purpose": text}
            required = ("capability_id", "typed_input", "bounded_purpose")
        elif method == "invoke":
            payload = {
                "plan_or_handle": _schema_object(
                    ("kind", "handle_ref", "plan_digest"),
                    {"kind": {"enum": ["effect-plan", "invocation-handle"]}, "handle_ref": _nullable(text), "plan_digest": {"$ref": "#/$defs/sha"}},
                )
            }
            required = ("plan_or_handle",)
        elif method == "status":
            payload = {"handle": _schema_object(("handle_ref", "handle_digest"), {"handle_ref": text, "handle_digest": {"$ref": "#/$defs/sha"}})}
            required = ("handle",)
        else:
            payload = {
                "handle": _schema_object(("handle_ref", "handle_digest"), {"handle_ref": text, "handle_digest": {"$ref": "#/$defs/sha"}}),
                "bounded_reason": _nullable(text),
            }
            required = ("handle", "bounded_reason")
        branches.append(
            _schema_object(
                ("schema_version", "method", "request_id", "context_budget", "payload"),
                {
                    **common,
                    "method": {"const": method},
                    "payload": _schema_object(required, payload),
                },
            )
        )
    return _schema_base(
        REQUEST_SCHEMA_ID,
        {},
        (),
        defs=defs,
        extra={"oneOf": branches},
    )


def _result_schema() -> dict[str, object]:
    defs = _defs()
    text = {"$ref": "#/$defs/text"}
    summary = {"$ref": "#/$defs/summary"}
    stable = {"$ref": "#/$defs/stable"}
    semver = {"$ref": "#/$defs/semver"}
    sha = {"$ref": "#/$defs/sha"}
    evidence_refs = _array(text, max_items=SERVER_CEILINGS.max_provider_evidence_refs)
    identity = {
        "capability_id": stable,
        "provider_component_id": stable,
        "contract": stable,
        "schema": stable,
        "version": semver,
    }
    common = {
        "schema_version": {"const": SCHEMA_VERSION},
        "request_id": text,
        "status": {"enum": ["ok", "refused", "pending", "failed"]},
        "subject_ref": text,
        "projection_sha256": sha,
        "catalogue_sha256": sha,
        "lock_sha256": sha,
        "raw_lock_sha256": sha,
        "created_at": text,
        "provider_evidence_refs": evidence_refs,
    }
    payloads = {
        "search": _schema_object(("candidates", "truncated"), {"candidates": _array(_schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "redacted_summary", "match_refs", "projection_ref"), {**identity, "redacted_summary": summary, "match_refs": _array(text), "projection_ref": sha}), max_items=SERVER_CEILINGS.max_candidates), "truncated": {"type": "boolean"}}),
        "describe": _schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "detail_level", "contract_slice"), {**identity, "detail_level": {"enum": ["summary", "contract", "evidence", "full"]}, "contract_slice": _schema_object(("redacted_summary", "contract_digest", "evidence_refs"), {"redacted_summary": summary, "contract_digest": sha, "evidence_refs": evidence_refs})}),
        "plan": _schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "effect_digest", "redacted_effect_summary", "handle_ref", "receipt_ref"), {**identity, "effect_digest": sha, "redacted_effect_summary": summary, "handle_ref": _nullable(text), "receipt_ref": _nullable(text)}),
        "invoke": _schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "result_kind", "effect_digest", "handle_ref", "receipt_ref", "redacted_summary"), {**identity, "result_kind": {"enum": ["receipt", "handle", "refusal"]}, "effect_digest": _nullable(sha), "handle_ref": _nullable(text), "receipt_ref": _nullable(text), "redacted_summary": summary}),
        "status": _schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "handle_ref", "lifecycle_status", "effect_digest", "receipt_ref", "updated_at", "redacted_summary"), {**identity, "handle_ref": text, "lifecycle_status": {"enum": ["pending", "running", "succeeded", "failed", "unknown", "provider-unavailable"]}, "effect_digest": _nullable(sha), "receipt_ref": _nullable(text), "updated_at": _nullable(text), "redacted_summary": summary}),
        "cancel": _schema_object(("capability_id", "provider_component_id", "contract", "schema", "version", "handle_ref", "cancellation_status", "effect_digest", "receipt_ref", "updated_at", "redacted_summary"), {**identity, "handle_ref": text, "cancellation_status": {"enum": ["accepted", "refused", "already-terminal", "provider-unavailable"]}, "effect_digest": _nullable(sha), "receipt_ref": _nullable(text), "updated_at": _nullable(text), "redacted_summary": summary}),
    }
    branches = [
        _schema_object(
            ("schema_version", "method", "request_id", "status", "subject_ref", "projection_sha256", "catalogue_sha256", "lock_sha256", "raw_lock_sha256", "created_at", "provider_evidence_refs", "payload"),
            {**common, "method": {"const": method}, "payload": payloads[method]},
        )
        for method in NAVIGATOR_METHODS
    ]
    return _schema_base(RESULT_SCHEMA_ID, {}, (), defs=defs, extra={"oneOf": branches})


def _receipt_schema() -> dict[str, object]:
    defs = _defs()
    text = {"$ref": "#/$defs/text"}
    summary = {"$ref": "#/$defs/summary"}
    sha = {"$ref": "#/$defs/sha"}
    return _schema_base(
        RECEIPT_SCHEMA_ID,
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "receipt_id": text,
            "method": {"enum": list(NAVIGATOR_METHODS)},
            "status": {"enum": ["pending", "running", "succeeded", "failed", "unknown", "provider-unavailable", "accepted", "refused", "already-terminal"]},
            "subject_ref": text,
            "capability_id": {"$ref": "#/$defs/stable"},
            "provider_component_id": {"$ref": "#/$defs/stable"},
            "contract": {"$ref": "#/$defs/stable"},
            "schema": {"$ref": "#/$defs/stable"},
            "version": {"$ref": "#/$defs/semver"},
            "projection_sha256": sha,
            "catalogue_sha256": sha,
            "lock_sha256": sha,
            "raw_lock_sha256": sha,
            "effect_digest": _nullable(sha),
            "redacted_summary": summary,
            "provider_native_evidence_refs": _array(text, min_items=1, max_items=SERVER_CEILINGS.max_provider_evidence_refs),
            "authority_provenance_ref": _nullable(text),
            "created_at": text,
            "updated_at": _nullable(text),
            "prior_handle_ref": _nullable(text),
            "prior_receipt_ref": _nullable(text),
        },
        ("schema_version", "receipt_id", "method", "status", "subject_ref", "capability_id", "provider_component_id", "contract", "schema", "version", "projection_sha256", "catalogue_sha256", "lock_sha256", "raw_lock_sha256", "effect_digest", "redacted_summary", "provider_native_evidence_refs", "authority_provenance_ref", "created_at", "updated_at", "prior_handle_ref", "prior_receipt_ref"),
        defs=defs,
    )


def generate_projection_schema() -> bytes:
    return canonical_json_bytes(_projection_schema())


def generate_request_schema() -> bytes:
    return canonical_json_bytes(_request_schema())


def generate_result_schema() -> bytes:
    return canonical_json_bytes(_result_schema())


def generate_receipt_schema() -> bytes:
    return canonical_json_bytes(_receipt_schema())


def generate_schemas() -> Mapping[str, bytes]:
    return {
        "capability-navigator-projection.schema.json": generate_projection_schema(),
        "capability-navigator-request.schema.json": generate_request_schema(),
        "capability-navigator-result.schema.json": generate_result_schema(),
        "capability-navigator-receipt.schema.json": generate_receipt_schema(),
    }


# ---------------------------------------------------------------------------
# T-3222 (ORG-059 Child E) — typed delegation round-trip.
#
# A capability delegation is a typed, auditable round-trip: a role PROPOSES
# (DelegationProposal), the receiving role INDEPENDENTLY authenticates the
# delivery, accepts or refuses, re-plans under ITS OWN subject, invokes through
# ITS OWN native authority, and owns the operation — with a DISTINCT receipt
# (DelegationReceipt) per stage and no credential passing. These are pure typed
# records: the closed-schema validators here reuse the same _require_* /
# _require_closed / _reject_identity_lookalikes machinery as the navigator wire
# types, so the request-identity invariant is REFERENCED, not forked. All
# authority behaviour (delivery, re-planning, native invocation, receipt
# persistence) lives in tools/delegation_route.py — this module owns only the
# shapes and their fail-closed validators.
# ---------------------------------------------------------------------------

DELEGATION_STAGES = ("request", "delivery", "acceptance", "native_operation", "result")
# The two stages that describe an actually-invoked native operation. Native
# evidence refs and the receiving role's authority provenance are permitted
# ONLY here — a delivery/acceptance acknowledgment can never carry invocation
# evidence (guard: a delivery-ack is not an invocation receipt).
DELEGATION_EVIDENCE_STAGES = frozenset({"native_operation", "result"})
# Stages whose subject_ref is the RECEIVING role's own subject (the proposal's
# requester identity is provenance-only from acceptance onward — never inherited
# authority). Enforced by the adapter; recorded here for reference.
DELEGATION_RECEIVING_STAGES = frozenset({"acceptance", "native_operation", "result"})
# The single success sentinel at the result stage; only a completed result may
# carry native evidence + authority provenance.
DELEGATION_RESULT_SUCCESS = "completed"

DELEGATION_PROPOSAL_KEYS = (
    "schema_version",
    "proposal_id",
    "trace_id",
    "requester_subject_ref",
    "requester_persona_ref",
    "requester_role_refs",
    "intent",
    "capability_id",
    "contract_revision",
    "constraints_ref",
    "eligible_owning_role",
    "created_at",
)

DELEGATION_RECEIPT_KEYS = (
    "schema_version",
    "receipt_id",
    "proposal_id",
    "trace_id",
    "stage",
    "subject_ref",
    "status",
    "created_at",
    "redacted_summary",
    "prior_receipt_ref",
    "provider_native_evidence_refs",
    "authority_provenance_ref",
)

# Closed status vocabulary. The normal chain uses proposed/delivered/accepted/
# invoked/completed; the remainder are the typed, auditable non-happy outcomes
# the design's refusal matrix requires each be representable WITHOUT a silent
# accept (duplicate / expired / tampered / rerouted / refused / overloaded /
# lost-result). ORG-059:R4 (delegation dominates / overloads heads) surfaces as
# the `overloaded` outcome — represented, not actioned (no escalation policy).
DELEGATION_STATUSES = frozenset(
    {
        "proposed",
        "delivered",
        "accepted",
        "refused",
        "invoked",
        "native-failed",
        "completed",
        "duplicate",
        "expired",
        "tampered",
        "rerouted",
        "overloaded",
        "lost-result",
    }
)


@dataclass(frozen=True)
class DelegationProposal:
    """A typed capability-delegation proposal (proposing is NOT invoking).

    The requester identity fields are lifted from the server-built
    AuthenticatedContext — they are AUTHENTICATED provenance, never
    caller-supplied. The proposal carries NO credential, secret, grant, or
    persona-as-execution-primitive; `constraints_ref` is a reference, never
    inline secret values; `eligible_owning_role` is a ROLE id (validated
    against known role ids), never a named persona.
    """

    schema_version: int
    proposal_id: str
    trace_id: str
    requester_subject_ref: str
    requester_persona_ref: str
    requester_role_refs: tuple[str, ...]
    intent: str
    capability_id: str
    contract_revision: str
    constraints_ref: str | None
    eligible_owning_role: str
    created_at: str


@dataclass(frozen=True)
class DelegationReceipt:
    """One distinct receipt for one delegation stage (5 stages via `stage`).

    subject_ref is the requester's subject for the request/delivery stages and
    the RECEIVING role's OWN subject for acceptance/native_operation/result.
    provider_native_evidence_refs + authority_provenance_ref are permitted only
    on the native_operation/result stages; a delivery/acceptance ack can never
    present as invocation evidence. A result stage REQUIRES non-empty native
    evidence and the receiving role's own authority provenance.
    """

    schema_version: int
    receipt_id: str
    proposal_id: str
    trace_id: str
    stage: str
    subject_ref: str
    status: str
    created_at: str
    redacted_summary: str
    prior_receipt_ref: str | None
    provider_native_evidence_refs: tuple[str, ...]
    authority_provenance_ref: str | None


def delegation_proposal_to_json(proposal: DelegationProposal) -> dict[str, object]:
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "trace_id": proposal.trace_id,
        "requester_subject_ref": proposal.requester_subject_ref,
        "requester_persona_ref": proposal.requester_persona_ref,
        "requester_role_refs": list(proposal.requester_role_refs),
        "intent": proposal.intent,
        "capability_id": proposal.capability_id,
        "contract_revision": proposal.contract_revision,
        "constraints_ref": proposal.constraints_ref,
        "eligible_owning_role": proposal.eligible_owning_role,
        "created_at": proposal.created_at,
    }


def _require_delegation_role(
    value: str,
    *,
    known_role_ids: frozenset[str],
    known_personas: frozenset[str],
    path: str,
) -> None:
    # A named persona SEAT as an execution primitive is REJECTED
    # UNCONDITIONALLY — the owning capability is a ROLE the catalogue declares,
    # never a specific seat. The reject is unconditional (not gated on
    # `value not in known_role_ids`) precisely because a seat name can also
    # self-collide as an audit role-id: a persona that is BOTH a persona key
    # AND its own identity-passthrough role-id in AGENT_ROLE_MAP (no
    # distinct role-class). Such a value is ambiguous between seat and role, so it
    # is refused fail-closed rather than silently accepted as a delegation target.
    if value in known_personas:
        _fail(
            NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE,
            path,
            "eligible_owning_role is a named persona seat, not a role id",
        )
    if value not in known_role_ids:
        _fail(
            NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE,
            path,
            "eligible_owning_role is not a known role id",
        )


def parse_delegation_proposal(
    value: object,
    *,
    known_role_ids: frozenset[str],
    known_personas: frozenset[str] = frozenset(),
) -> DelegationProposal:
    """Parse + fail-closed validate a delegation proposal.

    Rejects (never strips) any credential/secret/identity-authority lookalike
    field via _reject_identity_lookalikes + the closed-schema check, and
    validates eligible_owning_role as a role id (not a persona). `known_role_ids`
    is REQUIRED — role validation is fail-closed and never optional.
    """
    _validate_json_value(value, path="delegation_proposal")
    _reject_identity_lookalikes(value, path="delegation_proposal")
    data = _require_mapping(value, path="delegation_proposal")
    _require_closed(data, keys=DELEGATION_PROPOSAL_KEYS, path="delegation_proposal")
    if _require_int(data["schema_version"], path="delegation_proposal.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "delegation_proposal.schema_version", "unsupported schema version")
    eligible_owning_role = _require_string(data["eligible_owning_role"], path="delegation_proposal.eligible_owning_role") or ""
    _require_delegation_role(
        eligible_owning_role,
        known_role_ids=known_role_ids,
        known_personas=known_personas,
        path="delegation_proposal.eligible_owning_role",
    )
    return DelegationProposal(
        schema_version=SCHEMA_VERSION,
        proposal_id=_require_string(data["proposal_id"], path="delegation_proposal.proposal_id") or "",
        trace_id=_require_string(data["trace_id"], path="delegation_proposal.trace_id") or "",
        requester_subject_ref=_require_string(data["requester_subject_ref"], path="delegation_proposal.requester_subject_ref") or "",
        requester_persona_ref=_require_string(data["requester_persona_ref"], path="delegation_proposal.requester_persona_ref") or "",
        requester_role_refs=_string_tuple(data["requester_role_refs"], path="delegation_proposal.requester_role_refs"),
        intent=_require_string(data["intent"], path="delegation_proposal.intent") or "",
        capability_id=_require_stable_id(data["capability_id"], path="delegation_proposal.capability_id") or "",
        contract_revision=_require_string(data["contract_revision"], path="delegation_proposal.contract_revision") or "",
        constraints_ref=_require_string(data["constraints_ref"], path="delegation_proposal.constraints_ref", nullable=True),
        eligible_owning_role=eligible_owning_role,
        created_at=_require_string(data["created_at"], path="delegation_proposal.created_at") or "",
    )


def delegation_receipt_to_json(receipt: DelegationReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "proposal_id": receipt.proposal_id,
        "trace_id": receipt.trace_id,
        "stage": receipt.stage,
        "subject_ref": receipt.subject_ref,
        "status": receipt.status,
        "created_at": receipt.created_at,
        "redacted_summary": receipt.redacted_summary,
        "prior_receipt_ref": receipt.prior_receipt_ref,
        "provider_native_evidence_refs": list(receipt.provider_native_evidence_refs),
        "authority_provenance_ref": receipt.authority_provenance_ref,
    }


def parse_delegation_receipt(value: object) -> DelegationReceipt:
    """Parse + fail-closed validate one delegation-stage receipt.

    Enforces the stage/payload distinctness guard: native evidence and the
    receiving role's authority provenance are permitted only on the
    native_operation/result stages, a result requires non-empty native
    evidence + provenance, and the request stage is the chain root (no
    prior_receipt_ref) while every later stage requires one.
    """
    _reject_forbidden_result_content(value, path="delegation_receipt")
    if len(canonical_json_bytes(value)) > SERVER_CEILINGS.max_total_bytes:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "delegation_receipt", "receipt exceeds max_total_bytes")
    data = _require_mapping(value, path="delegation_receipt")
    _require_closed(data, keys=DELEGATION_RECEIPT_KEYS, path="delegation_receipt")
    if _require_int(data["schema_version"], path="delegation_receipt.schema_version") != SCHEMA_VERSION:
        _fail(NavigatorErrorCode.UNSUPPORTED_VERSION, "delegation_receipt.schema_version", "unsupported schema version")
    stage = _require_string(data["stage"], path="delegation_receipt.stage") or ""
    if stage not in DELEGATION_STAGES:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "delegation_receipt.stage", "unsupported delegation stage")
    status = _require_string(data["status"], path="delegation_receipt.status") or ""
    if status not in DELEGATION_STATUSES:
        _fail(NavigatorErrorCode.SCHEMA_ENUM, "delegation_receipt.status", "unsupported delegation status")
    evidence_refs = _string_tuple(
        data["provider_native_evidence_refs"], path="delegation_receipt.provider_native_evidence_refs"
    )
    if len(evidence_refs) > SERVER_CEILINGS.max_provider_evidence_refs:
        _fail(NavigatorErrorCode.BYTE_BOUND_EXCEEDED, "delegation_receipt.provider_native_evidence_refs", "too many evidence refs")
    authority_provenance_ref = _require_string(
        data["authority_provenance_ref"], path="delegation_receipt.authority_provenance_ref", nullable=True
    )
    prior_receipt_ref = _require_string(
        data["prior_receipt_ref"], path="delegation_receipt.prior_receipt_ref", nullable=True
    )
    evidence_allowed = stage in DELEGATION_EVIDENCE_STAGES
    if evidence_refs and not evidence_allowed:
        _fail(
            NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
            "delegation_receipt.provider_native_evidence_refs",
            "native evidence permitted only on native_operation/result stages",
        )
    if authority_provenance_ref is not None and not evidence_allowed:
        _fail(
            NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
            "delegation_receipt.authority_provenance_ref",
            "authority provenance permitted only on native_operation/result stages",
        )
    if stage == "result":
        # A COMPLETED result is the only success sentinel: it REQUIRES non-empty
        # normalized native evidence + the receiving role's own authority
        # provenance. Any other result status (e.g. lost-result) must NOT carry
        # evidence/authority — tampered/missing native evidence can never present
        # as a completed result.
        if status == DELEGATION_RESULT_SUCCESS:
            if not evidence_refs:
                _fail(
                    NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
                    "delegation_receipt.provider_native_evidence_refs",
                    "completed result requires non-empty native evidence",
                )
            if authority_provenance_ref is None:
                _fail(
                    NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
                    "delegation_receipt.authority_provenance_ref",
                    "completed result requires the receiving role's authority provenance",
                )
        else:
            if evidence_refs:
                _fail(
                    NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
                    "delegation_receipt.provider_native_evidence_refs",
                    "a non-completed result must not present native evidence as success",
                )
            if authority_provenance_ref is not None:
                _fail(
                    NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
                    "delegation_receipt.authority_provenance_ref",
                    "a non-completed result must not present authority provenance",
                )
    if stage == "request":
        if prior_receipt_ref is not None:
            _fail(
                NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD,
                "delegation_receipt.prior_receipt_ref",
                "request stage is the chain root and carries no prior_receipt_ref",
            )
    elif prior_receipt_ref is None:
        _fail(
            NavigatorErrorCode.SCHEMA_REQUIRED_FIELD,
            "delegation_receipt.prior_receipt_ref",
            "non-root stage requires prior_receipt_ref",
        )
    return DelegationReceipt(
        schema_version=SCHEMA_VERSION,
        receipt_id=_require_string(data["receipt_id"], path="delegation_receipt.receipt_id") or "",
        proposal_id=_require_string(data["proposal_id"], path="delegation_receipt.proposal_id") or "",
        trace_id=_require_string(data["trace_id"], path="delegation_receipt.trace_id") or "",
        stage=stage,
        subject_ref=_require_string(data["subject_ref"], path="delegation_receipt.subject_ref") or "",
        status=status,
        created_at=_require_string(data["created_at"], path="delegation_receipt.created_at") or "",
        redacted_summary=_require_summary(data["redacted_summary"], path="delegation_receipt.redacted_summary"),
        prior_receipt_ref=prior_receipt_ref,
        provider_native_evidence_refs=evidence_refs,
        authority_provenance_ref=authority_provenance_ref,
    )
