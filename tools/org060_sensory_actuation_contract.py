"""ORG-060 sensory/actuation typed contract (T-3226).

This module is deliberately dark: it creates closed, canonical wire records
and typed internal decisions, but it performs no live device/platform action.
The public surface names capabilities, targets, evidence and digests; provider
identity, native authority and approval evidence stay inside server-owned
contexts and opaque binding digests.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from tools import capability_navigator_contract as cn

import memory_envelope as me  # noqa: E402
import memory_lifecycle_ops as ops  # noqa: E402
import memory_policy_evaluator as ev  # noqa: E402

SCHEMA_VERSION = "org060-sensory-actuation/v1"
WIRE_LIMIT = 8192
NESTED_LIMIT = 2048
SUMMARY_LIMIT = 256
TEXT_LIMIT = 4096
LOCAL_REF_LIMIT = 512
EVIDENCE_REFS_LIMIT = 8
MAX_RETRIES = 2

AVAILABILITY = ("available", "quiet", "off", "away")
SUBJECT_SCOPE = ("known_subject", "known_guest", "bystander", "unknown")
LOCALITY = ("local_only", "local_with_redacted_egress")
EGRESS = ("none", "redacted_derived")
IDEMPOTENCY_CLASS = (
    "non_idempotent",
    "idempotent_same_plan",
    "idempotent_until_observed",
)
DISPATCH_STATE = (
    "not_dispatched",
    "provider_accepted",
    "provider_denied",
    "provider_timeout",
    "unknown_outcome",
)
VERIFY_STATE = ("not_verified", "pending_observation", "verified", "mismatch", "expired")
CANCEL_STATE = (
    "not_requested",
    "requested",
    "provider_accepted",
    "observed_stopped",
    "already_terminal",
    "denied",
)
ROLLBACK_STATE = (
    "not_applicable",
    "not_requested",
    "planned",
    "provider_accepted",
    "verified",
    "partial",
    "denied",
)
CONSENT_STAGES = (
    "capture",
    "retention",
    "inference_transformation",
    "retrieval_use",
    "observation_dependent_actuation",
    "presence_recording_interaction",
)
LIFECYCLE_KIND = ("correction", "forget")

REFUSAL_CODES = frozenset(
    {
        "malformed-json",
        "noncanonical-json",
        "duplicate-key",
        "byte-bound-exceeded",
        "closed-object",
        "missing-field",
        "wrong-type",
        "invalid-enum",
        "invalid-pattern",
        "hostile-control",
        "forbidden-public-field",
        "identity-authority-lookalike",
        "stale-source",
        "tampered-source-binding",
        "catalogue-binding-mismatch",
        "projection-binding-mismatch",
        "stale-projection",
        "no-matching-capability",
        "ambiguous-capability",
        "missing-calibration",
        "stale-calibration",
        "missing-consent",
        "expired-consent",
        "revoked-consent",
        "replayed-consent",
        "contradictory-consent",
        "purpose-drift",
        "availability-override",
        "raw-media-inline-forbidden",
        "raw-media-remote-forbidden",
        "egress-evidence-missing",
        "redaction-evidence-missing",
        "derivation-source-missing",
        "derivation-source-stale",
        "derivation-provenance-missing",
        "trust-raise-forbidden",
        "capture-required-before-retention",
        "policy-evidence-missing",
        "retention-denied",
        "lifecycle-receipt-invalid",
        "global-erasure-claim-forbidden",
        "authenticated-context-required",
        "native-authority-missing",
        "approval-evidence-missing",
        "target-state-stale",
        "target-state-mismatch",
        "unsafe-precondition",
        "incompatible-precondition",
        "ambiguous-target",
        "plan-expired",
        "plan-digest-mismatch",
        "provider-denied",
        "provider-fallback-forbidden",
        "dispatch-not-completion",
        "verification-observation-required",
        "verification-mismatch",
        "non-idempotent-retry",
        "retry-digest-mismatch",
        "retry-exhausted",
        "retry-attempt-invalid",
        "cancellation-context-required",
        "cancellation-terminal",
        "cancellation-observation-required",
        "rollback-approval-missing",
        "rollback-precondition-failed",
        "rollback-denied",
        "residual-risk-required",
    }
)

FORBIDDEN_PUBLIC_PARTS = (
    "persona",
    "uid",
    "account",
    "role",
    "instance",
    "provider",
    "credential",
    "grant",
    "native-authority",
    "native_authority",
    "approver",
    "policy-revision",
    "policy_revision",
    "caller",
    "authority",
    "approval",
)

SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CONTROL_CODEPOINTS = frozenset(
    tuple(range(0x00, 0x20))
    + tuple(range(0x7F, 0xA0))
    + tuple(range(0x202A, 0x202F))
    + tuple(range(0x2066, 0x206A))
    + (0x200B, 0x200C, 0x200D, 0xFEFF)
)


@dataclass(frozen=True)
class TypedRefusal:
    reason: str
    detail: str

    def __post_init__(self) -> None:
        if self.reason not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal reason: {self.reason}")


@dataclass(frozen=True)
class FreshnessWindow:
    max_age_seconds: int


@dataclass(frozen=True)
class ConsentGrant:
    stage: str
    purpose: str
    subject_scope: str
    consent_ref: str
    consent_digest: str
    consented_at: str
    expires_at: str
    revoked: bool = False


@dataclass
class ConsentReplayCache:
    seen: set[str] = dataclasses.field(default_factory=set)

    def mark(self, consent_ref: str) -> bool:
        if consent_ref in self.seen:
            return False
        self.seen.add(consent_ref)
        return True


@dataclass(frozen=True)
class CapturePolicyDecision:
    allowed: bool
    stage: str
    purpose: str
    authority_ref: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class RedactionEvidence:
    refs: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class EgressPolicy:
    data: Mapping[str, object]


@dataclass(frozen=True)
class RawMedia:
    data: Mapping[str, object]


@dataclass(frozen=True)
class DerivationProvenance:
    source_digest: str
    transformation_ids: tuple[str, ...]
    model_refs: tuple[Mapping[str, object], ...]
    produced_at: str


@dataclass(frozen=True)
class DerivedPayload:
    data: Mapping[str, object]


@dataclass(frozen=True)
class RetentionDispositionRef:
    disposition_ref: str
    evaluator_reason: str
    policy_ref: str
    policy_revision: str
    policy_digest: str


@dataclass(frozen=True)
class LifecycleReceiptEvidence:
    kind: str
    receipt_ref: str
    receipt_digest: str
    outcome: str
    global_erasure_verified: bool


@dataclass(frozen=True)
class SensoryEnvelope:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["envelope_digest"])


@dataclass(frozen=True)
class ActuationRequest:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["request_digest"])


@dataclass(frozen=True)
class TargetState:
    target_ref: str
    capability_id: str
    projection_sha256: str
    catalogue_sha256: str
    lock_sha256: str
    capability_binding_digest: str
    observed_at: str
    state_digest: str
    current_preconditions: tuple[Mapping[str, object], ...]
    provider: cn.NavigatorCapabilityProvider


@dataclass(frozen=True)
class AuthenticatedActuationContext:
    authenticated_context: cn.AuthenticatedContext
    native_authority_ref: str
    approval_evidence_ref: str


@dataclass(frozen=True)
class ActuationPlan:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["plan_digest"])


@dataclass(frozen=True)
class ActuationReceipt:
    data: Mapping[str, object]

    @property
    def digest(self) -> str:
        return str(self.data["receipt_digest"])


@dataclass(frozen=True)
class NativeDispatchResult:
    state: str
    native_receipt_ref: str | None
    status_evidence: tuple[str, ...]


@dataclass(frozen=True)
class NativeCancellationResult:
    state: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class NativeRollbackResult:
    state: str
    native_receipt_ref: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class TypedEffectObservation:
    observed_ref: str
    observed_digest: str
    observed_at: str
    matches: bool


@dataclass(frozen=True)
class TypedCancellationObservation:
    observed_ref: str
    observed_digest: str
    observed_at: str
    stopped: bool


@dataclass(frozen=True)
class ResidualRisk:
    risk_class: str
    detail: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class TypedRollbackObservation:
    observed_ref: str
    observed_digest: str
    observed_at: str
    verified: bool


class NativeActuatorAdapter(Protocol):
    def dispatch(
        self, plan: ActuationPlan, *, context: AuthenticatedActuationContext
    ) -> NativeDispatchResult: ...

    def cancel(
        self, receipt: Mapping[str, object], *, context: AuthenticatedActuationContext
    ) -> NativeCancellationResult: ...

    def rollback(
        self, receipt: Mapping[str, object], *, context: AuthenticatedActuationContext
    ) -> NativeRollbackResult: ...


def _refuse(reason: str, detail: str) -> TypedRefusal:
    return TypedRefusal(reason, detail)


def canonical_json_bytes(value: Mapping[str, object] | Sequence[object]) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    ) + b"\n"


def _sha_json(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest_record(data: Mapping[str, object], digest_field: str) -> str:
    preimage = dict(data)
    preimage[digest_field] = None
    return _sha_json(preimage)


def _loads_canonical(raw: bytes | str, *, limit: int = WIRE_LIMIT) -> Mapping[str, object] | TypedRefusal:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        raw_bytes = raw
    else:
        return _refuse("wrong-type", "expected bytes or str")
    if len(raw_bytes) > limit:
        return _refuse("byte-bound-exceeded", "wire object exceeds byte bound")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _refuse("malformed-json", "non-UTF8 JSON")

    duplicate = False

    def pairs_hook(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        seen: set[str] = set()
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in seen:
                duplicate = True
            seen.add(key)
            out[key] = value
        return out

    try:
        parsed = json.loads(text, object_pairs_hook=pairs_hook)
    except (json.JSONDecodeError, ValueError):
        return _refuse("malformed-json", "malformed JSON")
    if duplicate:
        return _refuse("duplicate-key", "duplicate object key")
    if not isinstance(parsed, Mapping):
        return _refuse("wrong-type", "root must be object")
    if canonical_json_bytes(parsed) != raw_bytes:
        return _refuse("noncanonical-json", "JSON is not canonical")
    return parsed


def load_canonical_json_mapping(
    raw: bytes | str | Mapping[str, object], *, limit: int = WIRE_LIMIT
) -> Mapping[str, object] | TypedRefusal:
    if isinstance(raw, Mapping):
        try:
            raw_bytes = canonical_json_bytes(raw)
        except (TypeError, ValueError):
            return _refuse("noncanonical-json", "mapping is not canonical-equivalent")
        if len(raw_bytes) > limit:
            return _refuse("byte-bound-exceeded", "wire object exceeds byte bound")
        loaded = _loads_canonical(raw_bytes, limit=limit)
        if isinstance(loaded, TypedRefusal):
            return loaded
        return loaded
    return _loads_canonical(raw, limit=limit)


def parse_canonical_public(
    raw: bytes | str | Mapping[str, object],
    *,
    allowed_keys: Sequence[str],
    required_keys: Sequence[str],
    digest_field: str | None = None,
    schema: Mapping[str, object] | None = None,
) -> Mapping[str, object] | TypedRefusal:
    parsed = load_canonical_json_mapping(raw)
    if isinstance(parsed, TypedRefusal):
        return parsed
    closed = _closed(parsed, allowed_keys)
    if isinstance(closed, TypedRefusal):
        return closed
    for key in required_keys:
        if key not in parsed:
            return _refuse("missing-field", key)
    lookalike = _reject_public_lookalikes(parsed)
    if isinstance(lookalike, TypedRefusal):
        return lookalike
    hostile = _reject_hostile_control(parsed)
    if isinstance(hostile, TypedRefusal):
        return hostile
    if schema is not None:
        schema_result = _validate_json_schema_subset(parsed, schema)
        if isinstance(schema_result, TypedRefusal):
            return schema_result
    if digest_field is not None and parsed.get(digest_field) != _digest_record(parsed, digest_field):
        return _refuse("tampered-source-binding", f"{digest_field} mismatch")
    return parsed


def _schema_from_bytes(raw_schema: bytes) -> Mapping[str, object]:
    loaded = json.loads(raw_schema.decode("utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError("generated schema root must be mapping")
    return loaded


def _validate_json_schema_subset(
    value: object,
    schema: Mapping[str, object],
    *,
    root: Mapping[str, object] | None = None,
    path: str = "$",
) -> None | TypedRefusal:
    root = schema if root is None else root
    ref = schema.get("$ref")
    if isinstance(ref, str):
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            return _refuse("invalid-pattern", path)
        defs = root.get("$defs")
        if not isinstance(defs, Mapping):
            return _refuse("missing-field", "$defs")
        target = defs.get(ref[len(prefix) :])
        if not isinstance(target, Mapping):
            return _refuse("missing-field", ref)
        return _validate_json_schema_subset(value, target, root=root, path=path)

    branches = schema.get("oneOf", schema.get("anyOf"))
    if isinstance(branches, Sequence) and not isinstance(branches, (str, bytes, bytearray)):
        first_refusal: TypedRefusal | None = None
        for branch in branches:
            if not isinstance(branch, Mapping):
                continue
            result = _validate_json_schema_subset(value, branch, root=root, path=path)
            if not isinstance(result, TypedRefusal):
                return None
            if first_refusal is None or result.reason == "closed-object":
                first_refusal = result
        return first_refusal or _refuse("wrong-type", path)

    enum_values = schema.get("enum")
    if enum_values is not None:
        if not isinstance(enum_values, Sequence) or isinstance(enum_values, (str, bytes, bytearray)):
            return _refuse("invalid-enum", path)
        if value not in enum_values:
            return _refuse("invalid-enum", path)

    type_spec = schema.get("type")
    if type_spec is not None:
        allowed_types = list(type_spec) if isinstance(type_spec, list) else [type_spec]
        if not _matches_schema_type(value, allowed_types):
            return _refuse("wrong-type", path)
        if value is None:
            return None

    if isinstance(value, str):
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            return _refuse("byte-bound-exceeded", path)
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            return _refuse("invalid-pattern", path)

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            return _refuse("invalid-enum", path)
        if isinstance(maximum, int) and value > maximum:
            return _refuse("invalid-enum", path)

    if isinstance(value, Mapping):
        properties = schema.get("properties")
        if properties is not None:
            if not isinstance(properties, Mapping):
                return _refuse("wrong-type", f"{path}.properties")
            required = schema.get("required", ())
            if not isinstance(required, Sequence) or isinstance(required, (str, bytes, bytearray)):
                return _refuse("wrong-type", f"{path}.required")
            for key in required:
                if key not in value:
                    return _refuse("missing-field", f"{path}.{key}")
            if schema.get("additionalProperties") is False:
                extra = set(value) - {str(key) for key in properties}
                if extra:
                    return _refuse("closed-object", f"{path}.{sorted(extra)[0]}")
            for key, child_schema in properties.items():
                if key in value:
                    if not isinstance(child_schema, Mapping):
                        return _refuse("wrong-type", f"{path}.{key}")
                    result = _validate_json_schema_subset(value[key], child_schema, root=root, path=f"{path}.{key}")
                    if isinstance(result, TypedRefusal):
                        return result

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return _refuse("byte-bound-exceeded", path)
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, Mapping):
                return _refuse("wrong-type", f"{path}.items")
            for idx, item in enumerate(value):
                result = _validate_json_schema_subset(item, item_schema, root=root, path=f"{path}[{idx}]")
                if isinstance(result, TypedRefusal):
                    return result
    return None


def _matches_schema_type(value: object, allowed_types: Sequence[object]) -> bool:
    for item in allowed_types:
        if item == "null" and value is None:
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "object" and isinstance(value, Mapping):
            return True
        if item == "array" and isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return True
    return False


def _closed(value: Mapping[str, object], keys: Sequence[str]) -> None | TypedRefusal:
    extra = set(value) - set(keys)
    if extra:
        return _refuse("closed-object", f"unexpected field: {sorted(extra)[0]}")
    return None


def _reject_public_lookalikes(value: object, *, path: str = "$") -> None | TypedRefusal:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lower = key.lower()
            if any(part in lower for part in FORBIDDEN_PUBLIC_PARTS):
                return _refuse("identity-authority-lookalike", f"{path}.{key}")
            refused = _reject_public_lookalikes(child, path=f"{path}.{key}")
            if isinstance(refused, TypedRefusal):
                return refused
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for idx, child in enumerate(value):
            refused = _reject_public_lookalikes(child, path=f"{path}[{idx}]")
            if isinstance(refused, TypedRefusal):
                return refused
    return None


def _reject_hostile_control(value: object, *, path: str = "$") -> None | TypedRefusal:
    if isinstance(value, str):
        if any(ord(ch) in CONTROL_CODEPOINTS for ch in value):
            return _refuse("hostile-control", path)
        return None
    if isinstance(value, Mapping):
        for key, child in value.items():
            refused = _reject_hostile_control(str(key), path=f"{path}.{key}<key>")
            if isinstance(refused, TypedRefusal):
                return refused
            refused = _reject_hostile_control(child, path=f"{path}.{key}")
            if isinstance(refused, TypedRefusal):
                return refused
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for idx, child in enumerate(value):
            refused = _reject_hostile_control(child, path=f"{path}[{idx}]")
            if isinstance(refused, TypedRefusal):
                return refused
    return None


def _require_sha(value: object, field: str) -> str | TypedRefusal:
    if not isinstance(value, str):
        return _refuse("wrong-type", field)
    if SHA_RE.fullmatch(value) is None:
        return _refuse("invalid-pattern", field)
    return value


def _require_z(value: object, field: str) -> str | TypedRefusal:
    if not isinstance(value, str):
        return _refuse("wrong-type", field)
    if Z_RE.fullmatch(value) is None:
        return _refuse("invalid-pattern", field)
    return value


def _is_fresh(observed_at: str, now: datetime, freshness: FreshnessWindow) -> bool:
    try:
        seen = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return 0 <= (now - seen).total_seconds() <= freshness.max_age_seconds


def _provider_binding_preimage(provider: cn.NavigatorCapabilityProvider, projection: cn.NavigatorProjection) -> dict[str, object]:
    return {
        "provider_component_id": provider.provider_component_id,
        "capability_id": provider.capability_id,
        "contract": provider.contract,
        "schema": provider.schema,
        "version": provider.version,
        "source_record_sha256": provider.source_identity.record_sha256,
        "source_tested_revision": provider.source_identity.tested_revision,
        "source_locked_revision": provider.source_identity.locked_revision,
        "projection_sha256": projection.projection_sha256,
        "catalogue_sha256": projection.catalogue_sha256,
        "lock_sha256": projection.lock_sha256,
    }


def provider_binding_digest(provider: cn.NavigatorCapabilityProvider, projection: cn.NavigatorProjection) -> str:
    return _sha_json(_provider_binding_preimage(provider, projection))


def normalize_observation(
    projection: cn.NavigatorProjection,
    observation: Mapping[str, object],
    *,
    now: datetime,
    freshness: FreshnessWindow,
) -> SensoryEnvelope | TypedRefusal:
    if not isinstance(projection, cn.NavigatorProjection):
        return _refuse("projection-binding-mismatch", "real NavigatorProjection required")
    if not isinstance(observation, Mapping):
        return _refuse("wrong-type", "observation")
    lookalike = _reject_public_lookalikes(observation)
    if isinstance(lookalike, TypedRefusal):
        return lookalike
    hostile = _reject_hostile_control(observation)
    if isinstance(hostile, TypedRefusal):
        return hostile
    required = (
        "envelope_id",
        "component_id",
        "capability_id",
        "contract",
        "schema",
        "version",
        "device_revision",
        "calibration",
        "source_observed_at",
        "source_digest",
        "quality",
        "uncertainty",
        "subject_scope",
        "purpose",
        "capture_stage",
        "consent",
        "raw_media",
        "locality",
        "egress",
        "redaction",
        "retention",
        "access",
        "captured_at",
        "received_at",
    )
    for key in required:
        if key not in observation:
            return _refuse("missing-field", key)
    source_observed_at = _require_z(observation.get("source_observed_at"), "source_observed_at")
    if isinstance(source_observed_at, TypedRefusal):
        return source_observed_at
    if not _is_fresh(source_observed_at, now, freshness):
        return _refuse("stale-source", "source observation outside freshness window")
    matches = [
        p
        for p in projection.capabilities
        if p.provider_component_id == observation["component_id"]
        and p.capability_id == observation["capability_id"]
        and p.contract == observation["contract"]
        and p.schema == observation["schema"]
        and p.version == observation["version"]
    ]
    if not matches:
        return _refuse("no-matching-capability", "projection has no exact provider")
    if len(matches) > 1:
        return _refuse("ambiguous-capability", "projection has multiple exact providers")
    provider = matches[0]
    if observation.get("projection_sha256", projection.projection_sha256) != projection.projection_sha256:
        return _refuse("projection-binding-mismatch", "projection digest mismatch")
    if observation.get("catalogue_sha256", projection.catalogue_sha256) != projection.catalogue_sha256:
        return _refuse("catalogue-binding-mismatch", "catalogue digest mismatch")
    if observation.get("lock_sha256", projection.lock_sha256) != projection.lock_sha256:
        return _refuse("tampered-source-binding", "lock digest mismatch")
    calibration = observation["calibration"]
    if not isinstance(calibration, Mapping) or not calibration.get("calibration_ref"):
        return _refuse("missing-calibration", "calibration missing")
    if not calibration.get("calibrated_at"):
        return _refuse("stale-calibration", "calibrated_at missing")
    source = {
        "component_id": provider.provider_component_id,
        "capability_id": provider.capability_id,
        "contract": provider.contract,
        "schema": provider.schema,
        "version": provider.version,
        "device_revision": observation["device_revision"],
        "calibration": dict(calibration),
        "projection_sha256": projection.projection_sha256,
        "catalogue_sha256": projection.catalogue_sha256,
        "lock_sha256": projection.lock_sha256,
        "source_observed_at": source_observed_at,
        "source_digest": observation["source_digest"],
        "quality": observation["quality"],
        "uncertainty": observation["uncertainty"],
    }
    data = {
        "schema_version": SCHEMA_VERSION,
        "envelope_id": observation["envelope_id"],
        "captured_at": observation["captured_at"],
        "received_at": observation["received_at"],
        "source": source,
        "subject_scope": observation["subject_scope"],
        "purpose": observation["purpose"],
        "capture_stage": observation["capture_stage"],
        "consent": observation["consent"],
        "raw_media": observation["raw_media"],
        "derived": observation.get("derived", []),
        "locality": observation["locality"],
        "egress": observation["egress"],
        "redaction": observation["redaction"],
        "retention": observation["retention"],
        "access": observation["access"],
        "envelope_digest": None,
    }
    data["envelope_digest"] = _digest_record(data, "envelope_digest")
    refused = _reject_public_lookalikes(data)
    if isinstance(refused, TypedRefusal):
        return refused
    hostile = _reject_hostile_control(data)
    if isinstance(hostile, TypedRefusal):
        return hostile
    return SensoryEnvelope(data)


def evaluate_capture_policy(
    envelope: SensoryEnvelope,
    *,
    stage: str,
    consent_evidence: Sequence[ConsentGrant],
    grants: Sequence[ConsentGrant],
    purpose: str,
    availability: str,
    replay_cache: ConsentReplayCache,
    now: datetime,
) -> CapturePolicyDecision | TypedRefusal:
    if stage not in CONSENT_STAGES:
        return _refuse("invalid-enum", "stage")
    if availability in ("quiet", "off", "away"):
        return _refuse("availability-override", availability)
    if availability not in AVAILABILITY:
        return _refuse("invalid-enum", "availability")
    if envelope.data["subject_scope"] in ("bystander", "unknown"):
        return _refuse("missing-consent", "bystander/unknown subjects refuse")
    if len(purpose.encode("utf-8")) > SUMMARY_LIMIT:
        return _refuse("byte-bound-exceeded", "purpose")
    candidates = [g for g in list(consent_evidence) + list(grants) if g.stage == stage]
    if not candidates:
        return _refuse("missing-consent", stage)
    live: list[ConsentGrant] = []
    for grant in candidates:
        if grant.purpose != purpose:
            return _refuse("purpose-drift", grant.consent_ref)
        if grant.revoked:
            return _refuse("revoked-consent", grant.consent_ref)
        if not replay_cache.mark(grant.consent_ref):
            return _refuse("replayed-consent", grant.consent_ref)
        try:
            expiry = datetime.strptime(grant.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return _refuse("invalid-pattern", "expires_at")
        if expiry <= now:
            return _refuse("expired-consent", grant.consent_ref)
        live.append(grant)
    scopes = {grant.subject_scope for grant in live}
    if len(scopes) > 1:
        return _refuse("contradictory-consent", "multiple subject scopes")
    return CapturePolicyDecision(
        True,
        stage,
        purpose,
        "auth:org060-capture",
        tuple(grant.consent_ref for grant in live),
    )


def raw_media_ref(*, local_ref: str, media_digest: str) -> RawMedia | TypedRefusal:
    if "://" in local_ref and not local_ref.startswith("local://"):
        return _refuse("raw-media-remote-forbidden", local_ref)
    if len(local_ref.encode("utf-8")) > LOCAL_REF_LIMIT:
        return _refuse("byte-bound-exceeded", "local_ref")
    if not local_ref.startswith("local://"):
        return _refuse("raw-media-inline-forbidden", "raw media must be local ref")
    digest = _require_sha(media_digest, "media_digest")
    if isinstance(digest, TypedRefusal):
        return digest
    return RawMedia({"local_ref": local_ref, "media_digest": media_digest})


def egress_ref(
    *,
    purpose: str,
    evidence_refs: Sequence[str],
    redaction: RedactionEvidence,
) -> EgressPolicy | TypedRefusal:
    if not evidence_refs or len(evidence_refs) > EVIDENCE_REFS_LIMIT:
        return _refuse("egress-evidence-missing", "bounded evidence required")
    if not redaction.refs or not SHA_RE.fullmatch(redaction.digest):
        return _refuse("redaction-evidence-missing", "redaction evidence required")
    return EgressPolicy(
        {
            "class": "redacted_derived",
            "purpose": purpose,
            "evidence_refs": list(evidence_refs),
            "redaction": {"refs": list(redaction.refs), "digest": redaction.digest},
        }
    )


def derivation(
    envelope: SensoryEnvelope,
    *,
    payload_ref: str,
    payload_digest: str,
    provenance: DerivationProvenance,
) -> DerivedPayload | TypedRefusal:
    if provenance.source_digest != envelope.digest:
        return _refuse("derivation-source-stale", "source digest not current")
    if not provenance.transformation_ids:
        return _refuse("derivation-provenance-missing", "transformation missing")
    digest = _require_sha(payload_digest, "payload_digest")
    if isinstance(digest, TypedRefusal):
        return digest
    for model in provenance.model_refs:
        if "trust" in model:
            return _refuse("trust-raise-forbidden", "model cannot raise source trust")
    return DerivedPayload(
        {
            "payload_ref": payload_ref,
            "payload_digest": payload_digest,
            "confidence": 0.75,
            "provenance": {
                "source_digest": provenance.source_digest,
                "transformation_ids": list(provenance.transformation_ids),
                "model_refs": [dict(item) for item in provenance.model_refs],
                "produced_at": provenance.produced_at,
            },
        }
    )


def build_retention_proposal(
    envelope: SensoryEnvelope,
    capture_decision: CapturePolicyDecision,
    required_authority: str,
) -> me.RetentionProposal | TypedRefusal:
    if not capture_decision.allowed:
        return _refuse("capture-required-before-retention", "capture decision not allowed")
    return me.RetentionProposal(
        candidate_id=str(envelope.data["envelope_id"]),
        proposed_class=me.MemoryClass.SEMANTIC,
        canonical_source_ref=me.SourceRef(
            "org060",
            str(envelope.data["envelope_id"]),
            "rev:1",
            envelope.digest,
            str(envelope.data["captured_at"]),
            "org060",
        ),
        retention_shape=me.RetentionShape.DERIVED,
        owner="org060",
        subjects=("subject:org060",),
        audience="self",
        privacy_class="internal",
        confidence_kind=me.ConfidenceKind.INFERRED,
        retention_until=None,
        correction_policy="supersede",
        forgetting_policy="remove-derived",
        required_authority=required_authority,
        rationale="org060 capture retention proposal",
    )


def propose_retention(
    proposal: me.RetentionProposal,
    *,
    capture_decision: CapturePolicyDecision,
    authority: str | None,
    policy: me.TrustPolicy | None,
    required_authority: str,
) -> RetentionDispositionRef | TypedRefusal:
    if not capture_decision.allowed:
        return _refuse("capture-required-before-retention", "capture decision not allowed")
    if policy is None:
        return _refuse("policy-evidence-missing", "TrustPolicy evidence is required")
    if required_authority != proposal.required_authority:
        return _refuse("retention-denied", "required authority mismatch")
    decision = ev.evaluate_retention(proposal, authority, policy)
    if not decision.allowed:
        return _refuse("retention-denied", decision.detail)
    return RetentionDispositionRef(
        f"retention-disposition:{proposal.candidate_id}",
        decision.detail,
        policy.policy_ref,
        policy.revision,
        policy.digest,
    )


def consume_lifecycle_receipt(
    raw: bytes | str | Mapping[str, object],
    kind: Literal["correction", "forget"],
) -> LifecycleReceiptEvidence | TypedRefusal:
    if kind not in LIFECYCLE_KIND:
        return _refuse("invalid-enum", "kind")
    mapping = load_canonical_json_mapping(raw)
    if isinstance(mapping, TypedRefusal):
        return mapping
    try:
        if kind == "correction":
            receipt = ops.correction_receipt_from_json(mapping)
            decision = ops.validate_correction_receipt(receipt)
            digest = receipt.receipt_digest
            outcome = receipt.outcome.value
            receipt_ref = f"correction:{receipt.operation_id}"
        else:
            receipt = ops.forget_receipt_from_json(mapping)
            decision = ops.validate_forget_receipt(receipt)
            digest = receipt.receipt_digest
            outcome = receipt.outcome.value
            receipt_ref = f"forget:{receipt.operation_id}"
    except Exception as exc:
        return _refuse("lifecycle-receipt-invalid", exc.__class__.__name__)
    if not decision.allowed:
        return _refuse("lifecycle-receipt-invalid", decision.detail)
    text = json.dumps(mapping, sort_keys=True)
    if "global_erasure_verified" in text or "global-erasure" in text:
        return _refuse("global-erasure-claim-forbidden", "global erasure claim")
    return LifecycleReceiptEvidence(kind, receipt_ref, digest, outcome, False)


def make_authenticated_actuation_context(
    authenticated_context: cn.AuthenticatedContext,
    *,
    native_authority_ref: str,
    approval_evidence_ref: str,
) -> AuthenticatedActuationContext | TypedRefusal:
    if not isinstance(authenticated_context, cn.AuthenticatedContext):
        return _refuse("authenticated-context-required", "real AuthenticatedContext required")
    if not native_authority_ref:
        return _refuse("native-authority-missing", "native authority missing")
    if not approval_evidence_ref:
        return _refuse("approval-evidence-missing", "approval evidence missing")
    return AuthenticatedActuationContext(
        authenticated_context, native_authority_ref, approval_evidence_ref
    )


def make_actuation_request(data: Mapping[str, object]) -> ActuationRequest | TypedRefusal:
    allowed = (
        "schema_version",
        "request_id",
        "capability_id",
        "target_ref",
        "effect",
        "purpose",
        "preconditions",
        "expected_result",
        "deadline",
        "idempotency_key",
        "cancellation_ref",
        "request_digest",
    )
    closed = _closed(data, allowed)
    if isinstance(closed, TypedRefusal):
        return closed
    refused = _reject_public_lookalikes(data)
    if isinstance(refused, TypedRefusal):
        return refused
    hostile = _reject_hostile_control(data)
    if isinstance(hostile, TypedRefusal):
        return hostile
    body = dict(data)
    body.setdefault("request_digest", None)
    body["request_digest"] = _digest_record(body, "request_digest")
    normalized = load_canonical_json_mapping(body)
    if isinstance(normalized, TypedRefusal):
        return normalized
    schema_result = _validate_json_schema_subset(normalized, _schema_from_bytes(generate_actuation_request_schema()))
    if isinstance(schema_result, TypedRefusal):
        return schema_result
    return ActuationRequest(normalized)


def parse_actuation_request(raw: bytes | str | Mapping[str, object]) -> ActuationRequest | TypedRefusal:
    allowed = (
        "schema_version",
        "request_id",
        "capability_id",
        "target_ref",
        "effect",
        "purpose",
        "preconditions",
        "expected_result",
        "deadline",
        "idempotency_key",
        "cancellation_ref",
        "request_digest",
    )
    parsed = parse_canonical_public(
        raw,
        allowed_keys=allowed,
        required_keys=allowed,
        digest_field="request_digest",
        schema=_schema_from_bytes(generate_actuation_request_schema()),
    )
    if isinstance(parsed, TypedRefusal):
        return parsed
    if parsed["schema_version"] != SCHEMA_VERSION:
        return _refuse("invalid-enum", "schema_version")
    for field in ("effect", "expected_result"):
        if not isinstance(parsed[field], Mapping):
            return _refuse("wrong-type", field)
    if not isinstance(parsed["preconditions"], Sequence) or isinstance(
        parsed["preconditions"], (str, bytes)
    ):
        return _refuse("wrong-type", "preconditions")
    return ActuationRequest(parsed)


def actuation_request_to_json(request: ActuationRequest) -> dict[str, object]:
    return dict(request.data)


def make_target_state(
    *,
    target_ref: str,
    provider: cn.NavigatorCapabilityProvider,
    projection: cn.NavigatorProjection,
    observed_at: str,
    state_digest: str,
    current_preconditions: Sequence[Mapping[str, object]],
) -> TargetState:
    return TargetState(
        target_ref,
        provider.capability_id,
        projection.projection_sha256,
        projection.catalogue_sha256,
        projection.lock_sha256,
        provider_binding_digest(provider, projection),
        observed_at,
        state_digest,
        tuple(current_preconditions),
        provider,
    )


def _target_matches_request(
    request: ActuationRequest, projection: cn.NavigatorProjection, target: TargetState
) -> bool:
    return (
        target.target_ref == request.data["target_ref"]
        and target.capability_id == request.data["capability_id"]
        and target.projection_sha256 == projection.projection_sha256
        and target.catalogue_sha256 == projection.catalogue_sha256
        and target.lock_sha256 == projection.lock_sha256
    )


def plan_actuation(
    request: ActuationRequest,
    *,
    projection: cn.NavigatorProjection,
    target_states: Sequence[TargetState],
    context: AuthenticatedActuationContext,
    now: datetime,
    target_freshness: FreshnessWindow = FreshnessWindow(60),
) -> ActuationPlan | TypedRefusal:
    if not isinstance(context, AuthenticatedActuationContext):
        return _refuse("authenticated-context-required", "context required")
    deadline = _require_z(request.data.get("deadline"), "deadline")
    if isinstance(deadline, TypedRefusal):
        return deadline
    if deadline <= now.strftime("%Y-%m-%dT%H:%M:%SZ"):
        return _refuse("plan-expired", "request deadline expired")
    matches = [target for target in target_states if _target_matches_request(request, projection, target)]
    if not matches:
        return _refuse("no-matching-capability", "no exact target")
    if len(matches) > 1:
        return _refuse("ambiguous-target", "multiple matching targets")
    target = matches[0]
    if target.provider not in projection.capabilities:
        return _refuse("target-state-mismatch", "target provider absent from projection")
    if target.capability_binding_digest != provider_binding_digest(target.provider, projection):
        return _refuse("target-state-mismatch", "binding digest mismatch")
    if not _is_fresh(target.observed_at, now, target_freshness):
        return _refuse("target-state-stale", "target state outside freshness window")
    request_preconditions = request.data["preconditions"]
    if not isinstance(request_preconditions, Sequence) or isinstance(request_preconditions, (str, bytes)):
        return _refuse("wrong-type", "preconditions")
    if len(request_preconditions) > len(target.current_preconditions):
        return _refuse("unsafe-precondition", "precondition not satisfied")
    evidence_refs = ["evidence:plan-binding"]
    body = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": f"plan:{request.data['request_id']}",
        "request_digest": request.digest,
        "capability_id": request.data["capability_id"],
        "target_ref": request.data["target_ref"],
        "target_state_digest": target.state_digest,
        "effect": request.data["effect"],
        "preconditions": request.data["preconditions"],
        "expected_result": request.data["expected_result"],
        "projection_sha256": projection.projection_sha256,
        "catalogue_sha256": projection.catalogue_sha256,
        "lock_sha256": projection.lock_sha256,
        "capability_binding_digest": target.capability_binding_digest,
        "implications": [
            {
                "kind": item.kind,
                "id": item.implication_id,
                "scope": item.scope,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in target.provider.implications
        ],
        "idempotency": {
            "key": request.data["idempotency_key"],
            "class": "idempotent_same_plan",
            "max_attempts": MAX_RETRIES,
        },
        "verification_steps": [
            {
                "step_id": "verify:1",
                "observation_capability_id": request.data["capability_id"],
                "expected_result": request.data["expected_result"],
                "evidence_refs": evidence_refs,
            }
        ],
        "rollback_steps": [
            {
                "step_id": "rollback:1",
                "effect": request.data["effect"],
                "preconditions": request.data["preconditions"],
                "expected_result": request.data["expected_result"],
                "evidence_refs": evidence_refs,
            }
        ],
        "expires_at": request.data["deadline"],
        "evidence_refs": evidence_refs,
        "plan_digest": None,
    }
    body["plan_digest"] = _digest_record(body, "plan_digest")
    return ActuationPlan(body)


def parse_actuation_plan(raw: bytes | str | Mapping[str, object]) -> ActuationPlan | TypedRefusal:
    allowed = (
        "schema_version",
        "plan_id",
        "request_digest",
        "capability_id",
        "target_ref",
        "target_state_digest",
        "effect",
        "preconditions",
        "expected_result",
        "projection_sha256",
        "catalogue_sha256",
        "lock_sha256",
        "capability_binding_digest",
        "implications",
        "idempotency",
        "verification_steps",
        "rollback_steps",
        "expires_at",
        "evidence_refs",
        "plan_digest",
    )
    parsed = parse_canonical_public(
        raw,
        allowed_keys=allowed,
        required_keys=allowed,
        digest_field="plan_digest",
        schema=_schema_from_bytes(generate_actuation_plan_schema()),
    )
    if isinstance(parsed, TypedRefusal):
        return parsed
    if parsed["schema_version"] != SCHEMA_VERSION:
        return _refuse("invalid-enum", "schema_version")
    return ActuationPlan(parsed)


def actuation_plan_to_json(plan: ActuationPlan) -> dict[str, object]:
    return dict(plan.data)


def invoke(
    plan: ActuationPlan,
    *,
    context: AuthenticatedActuationContext,
    adapter: NativeActuatorAdapter | None = None,
    provider_result: NativeDispatchResult | None = None,
    prior: ActuationReceipt | Mapping[str, object] | None = None,
    attempt: int = 1,
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    if not isinstance(context, AuthenticatedActuationContext):
        return _refuse("authenticated-context-required", "context required")
    prior_data = _receipt_data(prior) if prior is not None else None
    if not context.approval_evidence_ref:
        return _refuse("approval-evidence-missing", "approval required")
    if plan.data.get("plan_digest") != _digest_record(plan.data, "plan_digest"):
        return _refuse("plan-digest-mismatch", "plan digest mismatch")
    if str(plan.data["expires_at"]) <= now.strftime("%Y-%m-%dT%H:%M:%SZ"):
        return _refuse("plan-expired", "plan expired")
    if attempt < 1 or attempt > MAX_RETRIES:
        return _refuse("retry-attempt-invalid", "attempt out of range")
    if prior_data is not None:
        if (
            prior_data.get("verification_state") == "verified"
            or prior_data.get("dispatch_state") == "provider_denied"
        ):
            return _refuse("provider-denied", "terminal receipt cannot retry")
        if prior_data.get("plan_digest") != plan.digest:
            return _refuse("retry-digest-mismatch", "prior receipt plan mismatch")
        idem = plan.data["idempotency"]
        if isinstance(idem, Mapping) and idem.get("class") == "non_idempotent":
            return _refuse("non-idempotent-retry", "non-idempotent retry")
        if attempt > int(prior_data.get("attempt_number", 0)) + 1:
            return _refuse("retry-attempt-invalid", "attempt must increment by one")
    result = provider_result if provider_result is not None else None
    if result is None and adapter is not None:
        result = adapter.dispatch(plan, context=context)
    if result is None:
        return _refuse("provider-fallback-forbidden", "provider result required")
    if result.state == "provider_denied":
        return _refuse("provider-denied", "provider denial is final")
    if result.state not in DISPATCH_STATE:
        return _refuse("invalid-enum", "dispatch_state")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": f"receipt:{plan.data['plan_id']}:{attempt}",
        "plan_digest": plan.digest,
        "request_digest": plan.data["request_digest"],
        "capability_id": plan.data["capability_id"],
        "target_ref": plan.data["target_ref"],
        "target_state_digest": plan.data["target_state_digest"],
        "attempt_number": attempt,
        "native_receipt_ref": result.native_receipt_ref,
        "dispatch_state": result.state,
        "verification_state": "pending_observation",
        "cancellation_state": "not_requested",
        "rollback_state": "not_requested",
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": None,
        "observed_postcondition": None,
        "residual_risk": None,
        "evidence_refs": list(result.status_evidence),
        "receipt_digest": None,
    }
    receipt["receipt_digest"] = _digest_record(receipt, "receipt_digest")
    return ActuationReceipt(receipt)


def _receipt_data(receipt: ActuationReceipt | Mapping[str, object]) -> Mapping[str, object]:
    return receipt.data if isinstance(receipt, ActuationReceipt) else receipt


def _resign_receipt(receipt: Mapping[str, object]) -> ActuationReceipt:
    updated = dict(receipt)
    updated["receipt_digest"] = _digest_record(updated, "receipt_digest")
    return ActuationReceipt(updated)


def observe_effect(
    receipt: ActuationReceipt | Mapping[str, object],
    observation: TypedEffectObservation,
    *,
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    receipt = _receipt_data(receipt)
    if receipt.get("dispatch_state") != "provider_accepted":
        return _refuse("dispatch-not-completion", "dispatch is not completion")
    updated = dict(receipt)
    updated["observed_postcondition"] = dataclasses.asdict(observation)
    updated["verification_state"] = "verified" if observation.matches else "mismatch"
    updated["completed_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not observation.matches:
        return _refuse("verification-mismatch", "observation mismatch")
    return _resign_receipt(updated)


def request_cancellation(
    receipt: ActuationReceipt | Mapping[str, object],
    context: AuthenticatedActuationContext,
    *,
    adapter: NativeActuatorAdapter | None = None,
    provider_result: NativeCancellationResult | None = None,
    evidence_refs: Sequence[str],
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    receipt = _receipt_data(receipt)
    if not isinstance(context, AuthenticatedActuationContext):
        return _refuse("cancellation-context-required", "context required")
    if receipt.get("verification_state") == "verified":
        return _refuse("cancellation-terminal", "already terminal")
    result = provider_result if provider_result is not None else None
    if result is None and adapter is not None:
        result = adapter.cancel(receipt, context=context)
    if result is None:
        updated = dict(receipt)
        updated["cancellation_state"] = "requested"
        return _resign_receipt(updated)
    if result.state not in ("provider_accepted", "denied", "already_terminal"):
        return _refuse("invalid-enum", "cancellation")
    updated = dict(receipt)
    updated["cancellation_state"] = result.state
    updated["evidence_refs"] = list(evidence_refs) + list(result.evidence_refs)
    return _resign_receipt(updated)


def observe_cancellation(
    receipt: ActuationReceipt | Mapping[str, object],
    observation: TypedCancellationObservation,
    *,
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    receipt = _receipt_data(receipt)
    if receipt.get("cancellation_state") != "provider_accepted":
        return _refuse("cancellation-observation-required", "accepted cancellation requires observation")
    updated = dict(receipt)
    updated["observed_postcondition"] = dataclasses.asdict(observation)
    updated["cancellation_state"] = "observed_stopped" if observation.stopped else "denied"
    updated["completed_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _resign_receipt(updated)


def rollback_effect(
    receipt: ActuationReceipt | Mapping[str, object],
    *,
    context: AuthenticatedActuationContext,
    adapter: NativeActuatorAdapter | None = None,
    approval_evidence_ref: str,
    provider_result: NativeRollbackResult | None = None,
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    receipt = _receipt_data(receipt)
    if not isinstance(context, AuthenticatedActuationContext):
        return _refuse("authenticated-context-required", "context required")
    if not approval_evidence_ref:
        return _refuse("rollback-approval-missing", "separate rollback approval required")
    if receipt.get("verification_state") not in ("mismatch", "verified"):
        return _refuse("rollback-precondition-failed", "rollback requires observed effect")
    result = provider_result if provider_result is not None else None
    if result is None and adapter is not None:
        result = adapter.rollback(receipt, context=context)
    if result is None:
        updated = dict(receipt)
        updated["rollback_state"] = "planned"
        return _resign_receipt(updated)
    if result.state == "denied":
        return _refuse("rollback-denied", "provider denied rollback")
    if result.state not in ("provider_accepted", "denied"):
        return _refuse("invalid-enum", "rollback")
    updated = dict(receipt)
    updated["rollback_state"] = result.state
    updated["evidence_refs"] = list(updated.get("evidence_refs", ())) + list(result.evidence_refs)
    return _resign_receipt(updated)


def observe_rollback(
    receipt: ActuationReceipt | Mapping[str, object],
    observation: TypedRollbackObservation,
    residual_risk: ResidualRisk | None,
    *,
    now: datetime,
) -> ActuationReceipt | TypedRefusal:
    receipt = _receipt_data(receipt)
    if receipt.get("rollback_state") != "provider_accepted":
        return _refuse("rollback-precondition-failed", "rollback was not accepted")
    updated = dict(receipt)
    updated["observed_postcondition"] = dataclasses.asdict(observation)
    if observation.verified:
        updated["rollback_state"] = "verified"
    else:
        if residual_risk is None:
            return _refuse("residual-risk-required", "partial rollback needs residual risk")
        updated["rollback_state"] = "partial"
        updated["residual_risk"] = {
            "class": residual_risk.risk_class,
            "detail": residual_risk.detail,
            "evidence_refs": list(residual_risk.evidence_refs),
        }
    updated["completed_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _resign_receipt(updated)


def parse_sensory_envelope(raw: bytes | str | Mapping[str, object]) -> SensoryEnvelope | TypedRefusal:
    allowed = (
        "schema_version",
        "envelope_id",
        "captured_at",
        "received_at",
        "source",
        "subject_scope",
        "purpose",
        "capture_stage",
        "consent",
        "raw_media",
        "derived",
        "locality",
        "egress",
        "redaction",
        "retention",
        "access",
        "envelope_digest",
    )
    parsed = parse_canonical_public(
        raw,
        allowed_keys=allowed,
        required_keys=allowed,
        digest_field="envelope_digest",
        schema=_schema_from_bytes(generate_sensory_envelope_schema()),
    )
    if isinstance(parsed, TypedRefusal):
        return parsed
    if parsed["schema_version"] != SCHEMA_VERSION:
        return _refuse("invalid-enum", "schema_version")
    return SensoryEnvelope(parsed)


def sensory_envelope_to_json(envelope: SensoryEnvelope) -> dict[str, object]:
    return dict(envelope.data)


def parse_actuation_receipt(raw: bytes | str | Mapping[str, object]) -> ActuationReceipt | TypedRefusal:
    allowed = (
        "schema_version",
        "receipt_id",
        "plan_digest",
        "request_digest",
        "capability_id",
        "target_ref",
        "target_state_digest",
        "attempt_number",
        "native_receipt_ref",
        "dispatch_state",
        "verification_state",
        "cancellation_state",
        "rollback_state",
        "started_at",
        "completed_at",
        "observed_postcondition",
        "residual_risk",
        "evidence_refs",
        "receipt_digest",
    )
    parsed = parse_canonical_public(
        raw,
        allowed_keys=allowed,
        required_keys=allowed,
        digest_field="receipt_digest",
        schema=_schema_from_bytes(generate_actuation_receipt_schema()),
    )
    if isinstance(parsed, TypedRefusal):
        return parsed
    if parsed["schema_version"] != SCHEMA_VERSION:
        return _refuse("invalid-enum", "schema_version")
    if parsed["dispatch_state"] not in DISPATCH_STATE:
        return _refuse("invalid-enum", "dispatch_state")
    if parsed["verification_state"] not in VERIFY_STATE:
        return _refuse("invalid-enum", "verification_state")
    if parsed["cancellation_state"] not in CANCEL_STATE:
        return _refuse("invalid-enum", "cancellation_state")
    if parsed["rollback_state"] not in ROLLBACK_STATE:
        return _refuse("invalid-enum", "rollback_state")
    return ActuationReceipt(parsed)


def actuation_receipt_to_json(receipt: ActuationReceipt) -> dict[str, object]:
    return dict(receipt.data)


def _defs() -> dict[str, object]:
    evidence_refs = {
        "type": "array",
        "items": {"$ref": "#/$defs/text"},
        "maxItems": EVIDENCE_REFS_LIMIT,
    }
    state_condition = _schema_object(("state",), {"state": {"$ref": "#/$defs/summary"}})
    return {
        "sha": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "ztime": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"},
        "text": {"type": "string", "maxLength": TEXT_LIMIT},
        "summary": {"type": "string", "maxLength": SUMMARY_LIMIT},
        "evidence_refs": evidence_refs,
        "calibration": _schema_object(
            ("calibration_ref", "calibrated_at"),
            {"calibration_ref": {"$ref": "#/$defs/text"}, "calibrated_at": {"$ref": "#/$defs/ztime"}},
        ),
        "quality": _schema_object(("score",), {"score": {"type": "number"}}),
        "uncertainty": _schema_object(("class",), {"class": {"$ref": "#/$defs/summary"}}),
        "source": _schema_object(
            (
                "component_id",
                "capability_id",
                "contract",
                "schema",
                "version",
                "device_revision",
                "calibration",
                "projection_sha256",
                "catalogue_sha256",
                "lock_sha256",
                "source_observed_at",
                "source_digest",
                "quality",
                "uncertainty",
            ),
            {
                "component_id": {"$ref": "#/$defs/text"},
                "capability_id": {"$ref": "#/$defs/text"},
                "contract": {"$ref": "#/$defs/text"},
                "schema": {"$ref": "#/$defs/text"},
                "version": {"$ref": "#/$defs/text"},
                "device_revision": {"$ref": "#/$defs/text"},
                "calibration": {"$ref": "#/$defs/calibration"},
                "projection_sha256": {"$ref": "#/$defs/sha"},
                "catalogue_sha256": {"$ref": "#/$defs/sha"},
                "lock_sha256": {"$ref": "#/$defs/sha"},
                "source_observed_at": {"$ref": "#/$defs/ztime"},
                "source_digest": {"$ref": "#/$defs/sha"},
                "quality": {"$ref": "#/$defs/quality"},
                "uncertainty": {"$ref": "#/$defs/uncertainty"},
            },
        ),
        "consent": _schema_object(
            ("refs",),
            {"refs": {"type": "array", "items": {"$ref": "#/$defs/text"}, "maxItems": EVIDENCE_REFS_LIMIT}},
        ),
        "raw_media": _schema_object(("class",), {"class": {"$ref": "#/$defs/summary"}}),
        "redaction": _schema_object(
            ("refs", "digest"),
            {"refs": evidence_refs, "digest": {"$ref": "#/$defs/sha"}},
        ),
        "retention": _schema_object(("shape",), {"shape": {"enum": ["reference", "excerpt", "derived"]}}),
        "access": _schema_object(("audience",), {"audience": {"$ref": "#/$defs/summary"}}),
        "model_ref": _schema_object(("model_ref",), {"model_ref": {"$ref": "#/$defs/text"}}),
        "provenance": _schema_object(
            ("source_digest", "transformation_ids", "model_refs", "produced_at"),
            {
                "source_digest": {"$ref": "#/$defs/sha"},
                "transformation_ids": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/text"},
                    "maxItems": EVIDENCE_REFS_LIMIT,
                },
                "model_refs": {"type": "array", "items": {"$ref": "#/$defs/model_ref"}, "maxItems": EVIDENCE_REFS_LIMIT},
                "produced_at": {"$ref": "#/$defs/ztime"},
            },
        ),
        "derived_payload": _schema_object(
            ("payload_ref", "payload_digest", "confidence", "provenance"),
            {
                "payload_ref": {"$ref": "#/$defs/text"},
                "payload_digest": {"$ref": "#/$defs/sha"},
                "confidence": {"type": "number"},
                "provenance": {"$ref": "#/$defs/provenance"},
            },
        ),
        "state_condition": state_condition,
        "state_conditions": {"type": "array", "items": {"$ref": "#/$defs/state_condition"}, "maxItems": EVIDENCE_REFS_LIMIT},
        "effect": _schema_object(("set",), {"set": {"$ref": "#/$defs/summary"}}),
        "idempotency": _schema_object(
            ("key", "class", "max_attempts"),
            {
                "key": {"$ref": "#/$defs/text"},
                "class": {"enum": list(IDEMPOTENCY_CLASS)},
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": MAX_RETRIES},
            },
        ),
        "implication": _schema_object(
            ("kind", "id", "scope", "evidence_refs"),
            {
                "kind": {"$ref": "#/$defs/summary"},
                "id": {"$ref": "#/$defs/text"},
                "scope": {"$ref": "#/$defs/summary"},
                "evidence_refs": evidence_refs,
            },
        ),
        "verification_step": _schema_object(
            ("step_id", "observation_capability_id", "expected_result", "evidence_refs"),
            {
                "step_id": {"$ref": "#/$defs/text"},
                "observation_capability_id": {"$ref": "#/$defs/text"},
                "expected_result": {"$ref": "#/$defs/state_condition"},
                "evidence_refs": evidence_refs,
            },
        ),
        "rollback_step": _schema_object(
            ("step_id", "effect", "preconditions", "expected_result", "evidence_refs"),
            {
                "step_id": {"$ref": "#/$defs/text"},
                "effect": {"$ref": "#/$defs/effect"},
                "preconditions": {"$ref": "#/$defs/state_conditions"},
                "expected_result": {"$ref": "#/$defs/state_condition"},
                "evidence_refs": evidence_refs,
            },
        ),
        "effect_observation": _schema_object(
            ("observed_ref", "observed_digest", "observed_at", "matches"),
            {
                "observed_ref": {"$ref": "#/$defs/text"},
                "observed_digest": {"$ref": "#/$defs/sha"},
                "observed_at": {"$ref": "#/$defs/ztime"},
                "matches": {"type": "boolean"},
            },
        ),
        "cancellation_observation": _schema_object(
            ("observed_ref", "observed_digest", "observed_at", "stopped"),
            {
                "observed_ref": {"$ref": "#/$defs/text"},
                "observed_digest": {"$ref": "#/$defs/sha"},
                "observed_at": {"$ref": "#/$defs/ztime"},
                "stopped": {"type": "boolean"},
            },
        ),
        "rollback_observation": _schema_object(
            ("observed_ref", "observed_digest", "observed_at", "verified"),
            {
                "observed_ref": {"$ref": "#/$defs/text"},
                "observed_digest": {"$ref": "#/$defs/sha"},
                "observed_at": {"$ref": "#/$defs/ztime"},
                "verified": {"type": "boolean"},
            },
        ),
        "residual_risk": _schema_object(
            ("class", "detail", "evidence_refs"),
            {
                "class": {"$ref": "#/$defs/summary"},
                "detail": {"$ref": "#/$defs/text"},
                "evidence_refs": evidence_refs,
            },
        ),
    }


def _schema_object(required: Sequence[str], properties: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _schema_base(schema_id: str, required: Sequence[str], properties: Mapping[str, object]) -> bytes:
    body = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "$defs": _defs(),
        **_schema_object(required, properties),
    }
    return canonical_json_bytes(body)


def generate_sensory_envelope_schema() -> bytes:
    props = {
        key: {"$ref": "#/$defs/text"}
        for key in (
            "schema_version",
            "envelope_id",
            "captured_at",
            "received_at",
            "subject_scope",
            "purpose",
            "capture_stage",
            "locality",
            "egress",
        )
    }
    props.update(
        {
            "captured_at": {"$ref": "#/$defs/ztime"},
            "received_at": {"$ref": "#/$defs/ztime"},
            "source": {"$ref": "#/$defs/source"},
            "consent": {"$ref": "#/$defs/consent"},
            "raw_media": {"$ref": "#/$defs/raw_media"},
            "derived": {"type": "array", "items": {"$ref": "#/$defs/derived_payload"}, "maxItems": EVIDENCE_REFS_LIMIT},
            "redaction": {"$ref": "#/$defs/redaction"},
            "retention": {"$ref": "#/$defs/retention"},
            "access": {"$ref": "#/$defs/access"},
            "envelope_digest": {"$ref": "#/$defs/sha"},
        }
    )
    return _schema_base(
        "https://pantheon.local/schemas/org060-sensory-envelope.schema.json",
        tuple(props),
        props,
    )


def generate_actuation_request_schema() -> bytes:
    props = {
        "schema_version": {"$ref": "#/$defs/text"},
        "request_id": {"$ref": "#/$defs/text"},
        "capability_id": {"$ref": "#/$defs/text"},
        "target_ref": {"$ref": "#/$defs/text"},
        "effect": {"$ref": "#/$defs/effect"},
        "purpose": {"$ref": "#/$defs/summary"},
        "preconditions": {"$ref": "#/$defs/state_conditions"},
        "expected_result": {"$ref": "#/$defs/state_condition"},
        "deadline": {"$ref": "#/$defs/ztime"},
        "idempotency_key": {"$ref": "#/$defs/text"},
        "cancellation_ref": {"$ref": "#/$defs/text"},
        "request_digest": {"$ref": "#/$defs/sha"},
    }
    return _schema_base(
        "https://pantheon.local/schemas/org060-actuation-request.schema.json",
        tuple(props),
        props,
    )


def generate_actuation_plan_schema() -> bytes:
    props = {
        key: {"$ref": "#/$defs/text"}
        for key in (
            "schema_version",
            "plan_id",
            "capability_id",
            "target_ref",
            "expires_at",
        )
    }
    props.update(
        {
            "expires_at": {"$ref": "#/$defs/ztime"},
            "request_digest": {"$ref": "#/$defs/sha"},
            "target_state_digest": {"$ref": "#/$defs/sha"},
            "effect": {"$ref": "#/$defs/effect"},
            "preconditions": {"$ref": "#/$defs/state_conditions"},
            "expected_result": {"$ref": "#/$defs/state_condition"},
            "projection_sha256": {"$ref": "#/$defs/sha"},
            "catalogue_sha256": {"$ref": "#/$defs/sha"},
            "lock_sha256": {"$ref": "#/$defs/sha"},
            "capability_binding_digest": {"$ref": "#/$defs/sha"},
            "implications": {"type": "array", "items": {"$ref": "#/$defs/implication"}, "maxItems": EVIDENCE_REFS_LIMIT},
            "idempotency": {"$ref": "#/$defs/idempotency"},
            "verification_steps": {"type": "array", "items": {"$ref": "#/$defs/verification_step"}, "maxItems": EVIDENCE_REFS_LIMIT},
            "rollback_steps": {"type": "array", "items": {"$ref": "#/$defs/rollback_step"}, "maxItems": EVIDENCE_REFS_LIMIT},
            "evidence_refs": {"$ref": "#/$defs/evidence_refs"},
            "plan_digest": {"$ref": "#/$defs/sha"},
        }
    )
    return _schema_base(
        "https://pantheon.local/schemas/org060-actuation-plan.schema.json",
        tuple(props),
        props,
    )


def generate_actuation_receipt_schema() -> bytes:
    props = {
        "schema_version": {"$ref": "#/$defs/text"},
        "receipt_id": {"$ref": "#/$defs/text"},
        "plan_digest": {"$ref": "#/$defs/sha"},
        "request_digest": {"$ref": "#/$defs/sha"},
        "capability_id": {"$ref": "#/$defs/text"},
        "target_ref": {"$ref": "#/$defs/text"},
        "target_state_digest": {"$ref": "#/$defs/sha"},
        "attempt_number": {"type": "integer", "minimum": 1, "maximum": MAX_RETRIES},
        "native_receipt_ref": {"type": ["string", "null"]},
        "dispatch_state": {"enum": list(DISPATCH_STATE)},
        "verification_state": {"enum": list(VERIFY_STATE)},
        "cancellation_state": {"enum": list(CANCEL_STATE)},
        "rollback_state": {"enum": list(ROLLBACK_STATE)},
        "started_at": {"$ref": "#/$defs/ztime"},
        "completed_at": {"oneOf": [{"$ref": "#/$defs/ztime"}, {"type": "null"}]},
        "observed_postcondition": {
            "oneOf": [
                {"type": "null"},
                {"$ref": "#/$defs/effect_observation"},
                {"$ref": "#/$defs/cancellation_observation"},
                {"$ref": "#/$defs/rollback_observation"},
            ]
        },
        "residual_risk": {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/residual_risk"}]},
        "evidence_refs": {"$ref": "#/$defs/evidence_refs"},
        "receipt_digest": {"$ref": "#/$defs/sha"},
    }
    return _schema_base(
        "https://pantheon.local/schemas/org060-actuation-receipt.schema.json",
        tuple(props),
        props,
    )


def sensory_envelope_schema() -> bytes:
    return generate_sensory_envelope_schema()


def actuation_request_schema() -> bytes:
    return generate_actuation_request_schema()


def actuation_plan_schema() -> bytes:
    return generate_actuation_plan_schema()


def actuation_receipt_schema() -> bytes:
    return generate_actuation_receipt_schema()


def generate_schemas() -> Mapping[str, bytes]:
    return {
        "org060-sensory-envelope.schema.json": generate_sensory_envelope_schema(),
        "org060-actuation-request.schema.json": generate_actuation_request_schema(),
        "org060-actuation-plan.schema.json": generate_actuation_plan_schema(),
        "org060-actuation-receipt.schema.json": generate_actuation_receipt_schema(),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-schemas"]:
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name, content in generate_schemas().items():
            (root / name).write_bytes(content)
    else:
        raise SystemExit("usage: org060_sensory_actuation_contract.py --write-schemas")
