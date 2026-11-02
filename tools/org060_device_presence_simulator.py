"""Deterministic, fixture-only ORG-060 device and presence simulator.

The module is a pure conformance fixture.  It has no filesystem, network,
subprocess, service, credential, device, ambient-clock, or random boundary.
Callers supply canonical durable bytes and explicit logical time.  Child B and
Child C remain the authorities for observation, actuation, and presence
semantics; these adapters implement their production-shaped Protocol seams.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, runtime_checkable

from tools import capability_navigator_contract as cn
from tools import org060_presence_contract as c
from tools import org060_sensory_actuation_contract as b


SCHEMA_VERSION: Final[str] = "org060-device-presence-simulator/v1"
FIXTURE_EVIDENCE_CLASS: Final[str] = "org060-fixture/non-production/v1"
MAX_DURABLE_BYTES: Final[int] = 1 << 20
LIFECYCLE_LEGS: Final[tuple[str, ...]] = (
    "install",
    "configure",
    "health",
    "update",
    "remove",
)
LIFECYCLE_OUTCOMES: Final[Mapping[str, frozenset[str]]] = {
    "install": frozenset({"verified"}),
    "configure": frozenset({"verified"}),
    "health": frozenset({"healthy", "degraded", "unhealthy", "unknown"}),
    "update": frozenset({"verified", "interrupted", "rolled_back"}),
    "remove": frozenset({"rehearsed", "executed"}),
}
_ANCHOR_SEAL: Final[object] = object()
_KERNEL_TRANSITION_SEAL: Final[object] = object()


@dataclass(frozen=True)
class SimulatorRefusal:
    reason: str
    detail: str


@dataclass(frozen=True)
class DeviceBinding:
    adapter_id: str
    device_id: str
    device_revision: str
    calibration_revision: str
    calibration_device_id: str
    location_ref: str
    calibration_valid_through_tick: int


def _is_authentic_anchor(value: object, expected_type: type[object]) -> bool:
    return (
        type(value) is expected_type
        and getattr(value, "_seal", None) is _ANCHOR_SEAL
    )


@dataclass(frozen=True, init=False)
class _ActuatorAuthorizationAnchor:
    """Server-owned nominal authorization; never parsed from public/fixture data."""

    adapter_id: str
    target_ref: str
    capability_id: str
    provider_component_id: str
    provider_locked_revision: str
    provider_binding_digest: str
    projection_sha256: str
    catalogue_sha256: str
    raw_lock_sha256: str
    lock_sha256: str
    approved_plan_digest: str
    request_digest: str
    target_state_digest: str
    authenticated_context_digest: str
    native_authority_ref: str
    approval_evidence_ref: str
    _seal: object = field(init=False, repr=False, compare=False)


def _bind_server_actuator_authorization(
    *,
    adapter_id: str,
    approved_plan: b.ActuationPlan,
    context: b.AuthenticatedActuationContext,
    provider: cn.NavigatorCapabilityProvider,
    projection: cn.NavigatorProjection,
) -> _ActuatorAuthorizationAnchor | SimulatorRefusal:
    """Bind independently trusted owner objects at the simulated server boundary."""
    if not _nonblank(adapter_id):
        return SimulatorRefusal("actuator-binding-invalid", "adapter_id")
    if type(approved_plan) is not b.ActuationPlan:
        return SimulatorRefusal("actuation-plan-invalid", "owner type")
    if type(context) is not b.AuthenticatedActuationContext:
        return SimulatorRefusal("actuation-context-invalid", "owner type")
    if type(provider) is not cn.NavigatorCapabilityProvider:
        return SimulatorRefusal("provider-binding-invalid", "owner type")
    if type(projection) is not cn.NavigatorProjection:
        return SimulatorRefusal("provider-binding-invalid", "projection owner type")
    try:
        reparsed_projection = cn.parse_projection(cn.projection_to_json(projection))
    except (cn.NavigatorContractError, TypeError, ValueError):
        return SimulatorRefusal("provider-binding-invalid", "projection invalid")
    if reparsed_projection != projection or provider not in reparsed_projection.capabilities:
        return SimulatorRefusal("provider-binding-invalid", "provider absent from projection")
    parsed_plan = b.parse_actuation_plan(dict(approved_plan.data))
    if isinstance(parsed_plan, b.TypedRefusal) or parsed_plan != approved_plan:
        return SimulatorRefusal("actuation-plan-invalid", "canonical approved plan required")
    auth_digest = c.presence_auth_context_digest(context.authenticated_context)
    if isinstance(auth_digest, c.PresenceRefusal):
        return SimulatorRefusal("actuation-context-invalid", auth_digest.reason)
    binding_digest = b.provider_binding_digest(provider, projection)
    exact = (
        (parsed_plan.data.get("capability_id"), provider.capability_id, "capability_id"),
        (
            parsed_plan.data.get("capability_binding_digest"),
            binding_digest,
            "provider_binding_digest",
        ),
        (
            parsed_plan.data.get("projection_sha256"),
            projection.projection_sha256,
            "projection_sha256",
        ),
        (
            parsed_plan.data.get("catalogue_sha256"),
            projection.catalogue_sha256,
            "catalogue_sha256",
        ),
        (parsed_plan.data.get("lock_sha256"), projection.lock_sha256, "lock_sha256"),
    )
    for actual, expected, key in exact:
        if actual != expected:
            return SimulatorRefusal("provider-binding-invalid", key)
    for value, key in (
        (provider.provider_component_id, "provider_component_id"),
        (provider.source_identity.locked_revision, "provider_locked_revision"),
        (context.native_authority_ref, "native_authority_ref"),
        (context.approval_evidence_ref, "approval_evidence_ref"),
    ):
        if not _nonblank(value):
            return SimulatorRefusal("actuator-binding-invalid", key)
    anchor = object.__new__(_ActuatorAuthorizationAnchor)
    object.__setattr__(anchor, "adapter_id", adapter_id)
    object.__setattr__(anchor, "target_ref", str(parsed_plan.data["target_ref"]))
    object.__setattr__(anchor, "capability_id", provider.capability_id)
    object.__setattr__(
        anchor, "provider_component_id", provider.provider_component_id
    )
    object.__setattr__(
        anchor,
        "provider_locked_revision",
        provider.source_identity.locked_revision,
    )
    object.__setattr__(anchor, "provider_binding_digest", binding_digest)
    object.__setattr__(anchor, "projection_sha256", projection.projection_sha256)
    object.__setattr__(anchor, "catalogue_sha256", projection.catalogue_sha256)
    object.__setattr__(anchor, "raw_lock_sha256", projection.raw_lock_sha256)
    object.__setattr__(anchor, "lock_sha256", projection.lock_sha256)
    object.__setattr__(anchor, "approved_plan_digest", parsed_plan.digest)
    object.__setattr__(
        anchor, "request_digest", str(parsed_plan.data["request_digest"])
    )
    object.__setattr__(
        anchor, "target_state_digest", str(parsed_plan.data["target_state_digest"])
    )
    object.__setattr__(anchor, "authenticated_context_digest", auth_digest)
    object.__setattr__(anchor, "native_authority_ref", context.native_authority_ref)
    object.__setattr__(
        anchor, "approval_evidence_ref", context.approval_evidence_ref
    )
    object.__setattr__(anchor, "_seal", _ANCHOR_SEAL)
    return anchor


@dataclass(frozen=True)
class SensorReadPlan:
    event_id: str
    logical_tick: int
    expected_device_id: str
    expected_device_revision: str
    expected_calibration_revision: str
    expected_location_ref: str
    purpose: str
    subject_scope: str


@dataclass(frozen=True)
class FixtureReceipt:
    receipt_id: str
    adapter_id: str
    logical_tick: int
    operation: str
    owner_receipt_digest: str | None
    payload_digest: str
    evidence_class: Literal["org060-fixture/non-production/v1"] = field(
        init=False, default=FIXTURE_EVIDENCE_CLASS
    )


@dataclass(frozen=True)
class SensorReadResult:
    observation: Mapping[str, object]
    receipt: FixtureReceipt


@runtime_checkable
class NativeSensorAdapter(Protocol):
    fixture_only: Literal[True]

    def read(
        self,
        plan: SensorReadPlan,
        *,
        durable_state: bytes,
    ) -> SensorReadResult | SimulatorRefusal: ...


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    adapter_id: str
    logical_tick: int
    leg: Literal["install", "configure", "health", "update", "remove"]
    outcome: str
    evidence_ref: str
    target_revision: str | None = None


@dataclass(frozen=True)
class LifecycleReadiness:
    adapter_id: str
    state_digest: str
    evaluated_at_tick: int
    health_observed_at_tick: int
    lifecycle_epoch: int
    lifecycle_revision: str


@dataclass(frozen=True)
class SimulationEvent:
    event_id: str
    logical_tick: int
    adapter_id: str
    kind: str
    payload: Mapping[str, object]
    crash_point: str | None = None


@dataclass(frozen=True, init=False)
class KernelTransition:
    durable_state: bytes
    transcript: bytes
    receipts: tuple[FixtureReceipt, ...]
    crashed_at: str | None
    _seal: object = field(init=False, repr=False, compare=False)


@dataclass(frozen=True, init=False)
class _KernelRecoveryAnchor:
    """Out-of-band durable authority; never represented inside a checkpoint."""

    genesis_bytes: bytes
    genesis_digest: str
    checkpoint_bytes: bytes
    checkpoint_digest: str
    _seal: object = field(init=False, repr=False, compare=False)


@dataclass(frozen=True)
class CameraDerivedResult:
    raw_media: b.RawMedia
    derived: b.DerivedPayload
    egress: b.EgressPolicy
    receipt: FixtureReceipt


@dataclass(frozen=True)
class DroneMotionRequest:
    event_id: str
    logical_tick: int
    location_observed_tick: int
    target_position: tuple[int, int, int]
    max_distance: int
    energy_per_unit: int
    native_authority_allowed: bool


@dataclass(frozen=True)
class DeviceStateTransition:
    durable_state: bytes
    receipt: FixtureReceipt


@dataclass(frozen=True)
class FixtureNativeDispatchResult(b.NativeDispatchResult):
    fixture_receipt: FixtureReceipt


@dataclass(frozen=True)
class FixtureNativeCancellationResult(b.NativeCancellationResult):
    fixture_receipt: FixtureReceipt


@dataclass(frozen=True)
class FixtureNativeRollbackResult(b.NativeRollbackResult):
    fixture_receipt: FixtureReceipt


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _event_receipt_preimage(event: SimulationEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "logical_tick": event.logical_tick,
        "adapter_id": event.adapter_id,
        "kind": event.kind,
        "payload": dict(event.payload),
        "evidence_class": FIXTURE_EVIDENCE_CLASS,
    }


def _decode(raw: object) -> dict[str, object] | SimulatorRefusal:
    if type(raw) is not bytes:
        return SimulatorRefusal("durable-bytes-required", "exact bytes required")
    if len(raw) > MAX_DURABLE_BYTES:
        return SimulatorRefusal("byte-bound-exceeded", "durable state")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SimulatorRefusal("malformed-durable-state", "canonical JSON required")
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        return SimulatorRefusal("noncanonical-durable-state", "canonical object required")
    return value


DEVICE_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "adapter_id",
        "device_id",
        "device_revision",
        "calibration_revision",
        "calibration_device_id",
        "location_ref",
        "calibration_valid_through_tick",
        "network",
        "provider",
        "verification",
        "position",
        "battery",
        "connected",
        "occluded",
        "ambiguous_subject",
    }
)
ACTUATOR_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "state_kind",
        "adapter_id",
        "target_ref",
        "capability_id",
        "provider_component_id",
        "provider_revision",
        "target_state_digest",
        "current_preconditions",
        "evaluated_at",
        "network",
        "provider",
        "device_available",
    }
)


def _exact_int(value: object, *, nonnegative: bool = False) -> bool:
    return type(value) is int and (not nonnegative or value >= 0)


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _decode_device_state(raw: object) -> dict[str, object] | SimulatorRefusal:
    state = _decode(raw)
    if isinstance(state, SimulatorRefusal):
        return state
    if set(state) != DEVICE_STATE_KEYS or state.get("schema_version") != SCHEMA_VERSION:
        return SimulatorRefusal("device-state-schema-invalid", "keys or schema_version")
    for key in (
        "adapter_id",
        "device_id",
        "device_revision",
        "calibration_revision",
        "calibration_device_id",
        "location_ref",
    ):
        if not _nonblank(state.get(key)):
            return SimulatorRefusal("device-state-schema-invalid", key)
    if not _exact_int(state.get("calibration_valid_through_tick"), nonnegative=True):
        return SimulatorRefusal("device-state-schema-invalid", "calibration_valid_through_tick")
    for key in ("network", "provider", "verification"):
        if state.get(key) not in ("available", "outage"):
            return SimulatorRefusal("device-state-schema-invalid", key)
    for key in ("connected", "occluded", "ambiguous_subject"):
        if type(state.get(key)) is not bool:
            return SimulatorRefusal("device-state-schema-invalid", key)
    position = state.get("position")
    if (
        not isinstance(position, list)
        or len(position) != 3
        or not all(_exact_int(value) for value in position)
    ):
        return SimulatorRefusal("device-state-schema-invalid", "position")
    if not _exact_int(state.get("battery"), nonnegative=True):
        return SimulatorRefusal("device-state-schema-invalid", "battery")
    return state


def actuator_state_bytes(
    authority: _ActuatorAuthorizationAnchor,
    *,
    target_state_digest: str,
    current_preconditions: Sequence[Mapping[str, object]],
    evaluated_at: str,
    **changes: object,
) -> bytes:
    if not _is_authentic_anchor(authority, _ActuatorAuthorizationAnchor):
        raise ValueError("actuator-authorization-anchor-required")
    state: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "state_kind": "actuator",
        "adapter_id": authority.adapter_id,
        "target_ref": authority.target_ref,
        "capability_id": authority.capability_id,
        "provider_component_id": authority.provider_component_id,
        "provider_revision": authority.provider_locked_revision,
        "target_state_digest": target_state_digest,
        "current_preconditions": [dict(item) for item in current_preconditions],
        "evaluated_at": evaluated_at,
        "network": "available",
        "provider": "available",
        "device_available": True,
    }
    state.update(changes)
    return canonical_bytes(state)


def _decode_actuator_state(raw: object) -> dict[str, object] | SimulatorRefusal:
    state = _decode(raw)
    if isinstance(state, SimulatorRefusal):
        return state
    if (
        set(state) != ACTUATOR_STATE_KEYS
        or state.get("schema_version") != SCHEMA_VERSION
        or state.get("state_kind") != "actuator"
    ):
        return SimulatorRefusal("actuator-state-schema-invalid", "keys or schema")
    for key in (
        "adapter_id",
        "target_ref",
        "capability_id",
        "provider_component_id",
        "provider_revision",
        "target_state_digest",
        "evaluated_at",
    ):
        if not _nonblank(state.get(key)):
            return SimulatorRefusal("actuator-state-schema-invalid", key)
    if not str(state["evaluated_at"]).endswith("Z"):
        return SimulatorRefusal("actuator-state-schema-invalid", "evaluated_at")
    preconditions = state.get("current_preconditions")
    if not isinstance(preconditions, list):
        return SimulatorRefusal("actuator-state-schema-invalid", "current_preconditions")
    for item in preconditions:
        if (
            not isinstance(item, dict)
            or set(item) != {"state"}
            or not _nonblank(item.get("state"))
        ):
            return SimulatorRefusal("actuator-state-schema-invalid", "current_preconditions")
    if state.get("network") not in ("available", "outage"):
        return SimulatorRefusal("actuator-state-schema-invalid", "network")
    if state.get("provider") not in ("available", "outage"):
        return SimulatorRefusal("actuator-state-schema-invalid", "provider")
    if type(state.get("device_available")) is not bool:
        return SimulatorRefusal("actuator-state-schema-invalid", "device_available")
    return state


def _fixture_receipt(
    *,
    receipt_id: str,
    adapter_id: str,
    tick: int,
    operation: str,
    payload: object,
    owner_digest: str | None = None,
) -> FixtureReceipt:
    return FixtureReceipt(
        receipt_id,
        adapter_id,
        tick,
        operation,
        owner_digest,
        _sha(payload),
    )


def device_state_bytes(binding: DeviceBinding, **changes: object) -> bytes:
    state: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "adapter_id": binding.adapter_id,
        "device_id": binding.device_id,
        "device_revision": binding.device_revision,
        "calibration_revision": binding.calibration_revision,
        "calibration_device_id": binding.calibration_device_id,
        "location_ref": binding.location_ref,
        "calibration_valid_through_tick": binding.calibration_valid_through_tick,
        "network": "available",
        "provider": "available",
        "verification": "available",
        "position": [0, 0, 0],
        "battery": 100,
        "connected": True,
        "occluded": False,
        "ambiguous_subject": False,
    }
    state.update(changes)
    return canonical_bytes(state)


def _binding_check(
    binding: DeviceBinding,
    plan: SensorReadPlan,
    durable_state: bytes,
) -> dict[str, object] | SimulatorRefusal:
    state = _decode_device_state(durable_state)
    if isinstance(state, SimulatorRefusal):
        return state
    checked = _device_binding_check(binding, state, logical_tick=plan.logical_tick)
    if isinstance(checked, SimulatorRefusal):
        return checked
    state = checked
    requested = (
        (plan.expected_device_id, binding.device_id, "device_id"),
        (plan.expected_device_revision, binding.device_revision, "device_revision"),
        (
            plan.expected_calibration_revision,
            binding.calibration_revision,
            "calibration_revision",
        ),
        (plan.expected_location_ref, binding.location_ref, "location_ref"),
    )
    for actual, expected, key in requested:
        if actual != expected:
            return SimulatorRefusal("device-binding-mismatch", key)
    return state


def _device_binding_check(
    binding: DeviceBinding,
    state: dict[str, object],
    *,
    logical_tick: object,
) -> dict[str, object] | SimulatorRefusal:
    binding_refused = _validate_device_binding(binding)
    if isinstance(binding_refused, SimulatorRefusal):
        return binding_refused
    if not _exact_int(logical_tick, nonnegative=True):
        return SimulatorRefusal("logical-time-invalid", "logical_tick")
    exact = (
        ("adapter_id", binding.adapter_id),
        ("device_id", binding.device_id),
        ("device_revision", binding.device_revision),
        ("calibration_revision", binding.calibration_revision),
        ("calibration_device_id", binding.calibration_device_id),
        ("location_ref", binding.location_ref),
    )
    for key, expected in exact:
        if state.get(key) != expected:
            return SimulatorRefusal("device-binding-mismatch", key)
    valid_through = state.get("calibration_valid_through_tick")
    if not _exact_int(valid_through, nonnegative=True) or logical_tick > valid_through:
        return SimulatorRefusal("stale-calibration", "calibration validity")
    return state


def _observation(
    binding: DeviceBinding,
    plan: SensorReadPlan,
    state: Mapping[str, object],
    *,
    capability_id: str,
    contract: str,
    schema: str,
    timestamp: str,
    raw_media: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "envelope_id": f"observation:{plan.event_id}",
        "component_id": binding.adapter_id,
        "capability_id": capability_id,
        "contract": contract,
        "schema": schema,
        "version": "1.0.0",
        "device_revision": binding.device_revision,
        "calibration": {
            "calibration_ref": f"calibration:{binding.calibration_revision}",
            "calibrated_at": timestamp,
        },
        "source_observed_at": timestamp,
        "source_digest": _sha({"event": plan.event_id, "state": state}),
        "quality": {"score": 1.0},
        "uncertainty": {"class": "bounded"},
        "subject_scope": plan.subject_scope,
        "purpose": plan.purpose,
        "capture_stage": "capture",
        "consent": {"refs": [f"consent:{plan.event_id}"]},
        "raw_media": dict(raw_media or {"class": "none"}),
        "locality": "local_only",
        "egress": "none",
        "redaction": {"refs": ["redaction:fixture"], "digest": _sha({"redacted": True})},
        "retention": {"shape": "derived"},
        "access": {"audience": "owner"},
        "captured_at": timestamp,
        "received_at": timestamp,
    }


def _read_sensor(
    binding: DeviceBinding,
    plan: SensorReadPlan,
    durable_state: bytes,
    *,
    capability_id: str,
    contract: str,
    schema: str,
    timestamp: str,
    camera: bool = False,
) -> SensorReadResult | SimulatorRefusal:
    state = _binding_check(binding, plan, durable_state)
    if isinstance(state, SimulatorRefusal):
        return state
    if state.get("network") != "available":
        return SimulatorRefusal("network-outage", binding.adapter_id)
    if state.get("provider") != "available":
        return SimulatorRefusal("provider-outage", binding.adapter_id)
    if state.get("connected") is not True:
        return SimulatorRefusal("device-unavailable", binding.adapter_id)
    if camera and state.get("occluded") is True:
        return SimulatorRefusal("camera-occluded", binding.adapter_id)
    if camera and state.get("ambiguous_subject") is True:
        return SimulatorRefusal("ambiguous-subject", binding.adapter_id)
    raw = (
        {"class": "local", "local_ref": f"local://{binding.adapter_id}/{plan.event_id}"}
        if camera
        else None
    )
    observation = _observation(
        binding,
        plan,
        state,
        capability_id=capability_id,
        contract=contract,
        schema=schema,
        timestamp=timestamp,
        raw_media=raw,
    )
    receipt = _fixture_receipt(
        receipt_id=f"receipt:{plan.event_id}",
        adapter_id=binding.adapter_id,
        tick=plan.logical_tick,
        operation="observe",
        payload={
            "device_id": binding.device_id,
            "device_revision": binding.device_revision,
            "calibration_revision": binding.calibration_revision,
            "location_ref": binding.location_ref,
            "observation": observation,
        },
    )
    return SensorReadResult(observation, receipt)


@dataclass(frozen=True)
class FixedMicrophoneAdapter(NativeSensorAdapter):
    binding: DeviceBinding
    observed_at: str
    fixture_only: Literal[True] = field(init=False, default=True)

    def read(
        self, plan: SensorReadPlan, *, durable_state: bytes
    ) -> SensorReadResult | SimulatorRefusal:
        return _read_sensor(
            self.binding,
            plan,
            durable_state,
            capability_id="fixed-microphone-observe",
            contract="org060-fixed-microphone",
            schema="org060-fixed-microphone/v1",
            timestamp=self.observed_at,
        )


@dataclass(frozen=True)
class LocalCameraAdapter(NativeSensorAdapter):
    binding: DeviceBinding
    observed_at: str
    fixture_only: Literal[True] = field(init=False, default=True)

    def read(
        self, plan: SensorReadPlan, *, durable_state: bytes
    ) -> SensorReadResult | SimulatorRefusal:
        return _read_sensor(
            self.binding,
            plan,
            durable_state,
            capability_id="camera-observe",
            contract="org060-local-camera",
            schema="org060-local-camera/v1",
            timestamp=self.observed_at,
            camera=True,
        )

    def derive_permitted_event(
        self,
        envelope: b.SensoryEnvelope,
        *,
        raw_media_ref: str,
        raw_media_digest: str,
        payload_ref: str,
        payload_digest: str,
        provenance: b.DerivationProvenance,
        redaction: b.RedactionEvidence,
        egress_evidence_refs: Sequence[str],
    ) -> CameraDerivedResult | b.TypedRefusal | SimulatorRefusal:
        raw = b.raw_media_ref(local_ref=raw_media_ref, media_digest=raw_media_digest)
        if isinstance(raw, b.TypedRefusal):
            return raw
        derived = b.derivation(
            envelope,
            payload_ref=payload_ref,
            payload_digest=payload_digest,
            provenance=provenance,
        )
        if isinstance(derived, b.TypedRefusal):
            return derived
        egress = b.egress_ref(
            purpose=str(envelope.data["purpose"]),
            evidence_refs=egress_evidence_refs,
            redaction=redaction,
        )
        if isinstance(egress, b.TypedRefusal):
            return egress
        receipt = _fixture_receipt(
            receipt_id=f"receipt:derived:{self.binding.adapter_id}",
            adapter_id=self.binding.adapter_id,
            tick=0,
            operation="derive-redacted-event",
            payload={"raw": raw.data, "derived": derived.data, "egress": egress.data},
            owner_digest=envelope.digest,
        )
        return CameraDerivedResult(raw, derived, egress, receipt)


def _script(raw: bytes, operation: str) -> dict[str, object]:
    value = _decode(raw)
    if isinstance(value, SimulatorRefusal):
        return {"state": "provider_denied", "evidence_refs": ("fixture:invalid-script",)}
    selected = value.get(operation)
    if not isinstance(selected, dict):
        return {"state": "provider_denied", "evidence_refs": ("fixture:missing-script",)}
    return selected


def outcome_script_bytes(
    *,
    dispatch: str = "provider_accepted",
    cancel: str = "provider_accepted",
    rollback: str = "provider_accepted",
) -> bytes:
    return canonical_bytes(
        {
            "dispatch": {
                "state": dispatch,
                "native_receipt_ref": "fixture:native-dispatch",
                "evidence_refs": ["fixture:dispatch"],
            },
            "cancel": {"state": cancel, "evidence_refs": ["fixture:cancel"]},
            "rollback": {
                "state": rollback,
                "native_receipt_ref": "fixture:native-rollback",
                "evidence_refs": ["fixture:rollback"],
            },
        }
    )


def _native_dispatch_denied(adapter_id: str, reason: str) -> FixtureNativeDispatchResult:
    return FixtureNativeDispatchResult(
        "provider_denied",
        None,
        (f"fixture:refusal:{reason}",),
        _fixture_receipt(
            receipt_id=f"receipt:{adapter_id}:dispatch-refused",
            adapter_id=adapter_id,
            tick=0,
            operation="dispatch-refused",
            payload={"reason": reason},
        ),
    )


def _validate_actuator_context(
    context: object,
    authority: _ActuatorAuthorizationAnchor,
    current_state: bytes,
) -> dict[str, object] | SimulatorRefusal:
    if not _is_authentic_anchor(authority, _ActuatorAuthorizationAnchor):
        return SimulatorRefusal("actuator-binding-invalid", "server anchor required")
    if type(context) is not b.AuthenticatedActuationContext:
        return SimulatorRefusal("actuation-context-invalid", "owner type")
    if not all(
        _nonblank(value)
        for value in (
            authority.adapter_id,
            authority.target_ref,
            authority.capability_id,
            authority.provider_component_id,
            authority.provider_locked_revision,
            authority.provider_binding_digest,
            authority.projection_sha256,
            authority.catalogue_sha256,
            authority.raw_lock_sha256,
            authority.lock_sha256,
            authority.approved_plan_digest,
            authority.request_digest,
            authority.target_state_digest,
            authority.authenticated_context_digest,
            authority.native_authority_ref,
            authority.approval_evidence_ref,
        )
    ):
        return SimulatorRefusal("actuator-binding-invalid", "blank authority binding")
    if (
        not _nonblank(context.native_authority_ref)
        or context.native_authority_ref != authority.native_authority_ref
    ):
        return SimulatorRefusal("native-authority-denied", "native authority")
    if type(context.authenticated_context) is not cn.AuthenticatedContext:
        return SimulatorRefusal("actuation-context-invalid", "authenticated context")
    auth_digest = c.presence_auth_context_digest(context.authenticated_context)
    if (
        isinstance(auth_digest, c.PresenceRefusal)
        or auth_digest != authority.authenticated_context_digest
    ):
        return SimulatorRefusal("actuation-context-invalid", "authenticated context binding")
    if (
        not _nonblank(context.approval_evidence_ref)
        or context.approval_evidence_ref != authority.approval_evidence_ref
    ):
        return SimulatorRefusal("actuation-context-invalid", "approval binding")
    state = _decode_actuator_state(current_state)
    if isinstance(state, SimulatorRefusal):
        return state
    exact = (
        ("adapter_id", authority.adapter_id),
        ("target_ref", authority.target_ref),
        ("capability_id", authority.capability_id),
        ("provider_component_id", authority.provider_component_id),
        ("provider_revision", authority.provider_locked_revision),
    )
    for key, expected in exact:
        if state.get(key) != expected:
            return SimulatorRefusal("actuator-binding-mismatch", key)
    if state.get("network") != "available":
        return SimulatorRefusal("network-outage", authority.adapter_id)
    if state.get("provider") != "available":
        return SimulatorRefusal("provider-outage", authority.adapter_id)
    if state.get("device_available") is not True:
        return SimulatorRefusal("device-unavailable", authority.adapter_id)
    return state


def _validate_actuation_dispatch(
    plan: object,
    context: object,
    authority: _ActuatorAuthorizationAnchor,
    current_state: bytes,
) -> None | SimulatorRefusal:
    if type(plan) is not b.ActuationPlan:
        return SimulatorRefusal("actuation-plan-invalid", "owner type")
    state = _validate_actuator_context(context, authority, current_state)
    if isinstance(state, SimulatorRefusal):
        return state
    parsed = b.parse_actuation_plan(dict(plan.data))
    if isinstance(parsed, b.TypedRefusal):
        return SimulatorRefusal("actuation-plan-invalid", parsed.reason)
    if parsed.digest != authority.approved_plan_digest:
        return SimulatorRefusal("actuation-plan-binding-mismatch", "approved plan digest")
    exact = (
        (plan.data.get("target_ref"), authority.target_ref, "target_ref"),
        (plan.data.get("capability_id"), authority.capability_id, "capability_id"),
        (
            plan.data.get("capability_binding_digest"),
            authority.provider_binding_digest,
            "capability_binding_digest",
        ),
        (plan.data.get("request_digest"), authority.request_digest, "request_digest"),
        (
            plan.data.get("projection_sha256"),
            authority.projection_sha256,
            "projection_sha256",
        ),
        (
            plan.data.get("catalogue_sha256"),
            authority.catalogue_sha256,
            "catalogue_sha256",
        ),
        (plan.data.get("lock_sha256"), authority.lock_sha256, "lock_sha256"),
        (
            plan.data.get("target_state_digest"),
            state.get("target_state_digest"),
            "target_state_digest",
        ),
    )
    for actual, expected, key in exact:
        if actual != expected:
            return SimulatorRefusal("actuation-plan-binding-mismatch", key)
    if canonical_bytes(plan.data.get("preconditions")) != canonical_bytes(
        state.get("current_preconditions")
    ):
        return SimulatorRefusal("actuation-precondition-stale", "current_preconditions")
    expires_at = plan.data.get("expires_at")
    evaluated_at = state.get("evaluated_at")
    if not _nonblank(expires_at) or not _nonblank(evaluated_at) or expires_at <= evaluated_at:
        return SimulatorRefusal("actuation-plan-expired", "expires_at")
    return None


def _dispatch(
    raw: bytes,
    plan: object,
    context: object,
    authority: _ActuatorAuthorizationAnchor,
    current_state: bytes,
) -> b.NativeDispatchResult:
    refused = _validate_actuation_dispatch(plan, context, authority, current_state)
    if isinstance(refused, SimulatorRefusal):
        return _native_dispatch_denied(
            getattr(authority, "adapter_id", "uninitialized-anchor"),
            refused.reason,
        )
    adapter_id = authority.adapter_id
    if not isinstance(context, b.AuthenticatedActuationContext):
        return FixtureNativeDispatchResult(
            "provider_denied",
            None,
            ("fixture:context-refused",),
            _fixture_receipt(
                receipt_id=f"receipt:{adapter_id}:dispatch-refused",
                adapter_id=adapter_id,
                tick=0,
                operation="dispatch-refused",
                payload={"reason": "context-type"},
            ),
        )
    value = _script(raw, "dispatch")
    return FixtureNativeDispatchResult(
        str(value.get("state")),
        value.get("native_receipt_ref") if isinstance(value.get("native_receipt_ref"), str) else None,
        tuple(str(item) for item in value.get("evidence_refs", ())),
        _fixture_receipt(
            receipt_id=f"receipt:{adapter_id}:dispatch",
            adapter_id=adapter_id,
            tick=0,
            operation="dispatch",
            payload=value,
        ),
    )


def _validate_native_receipt(
    receipt: object,
    authority: _ActuatorAuthorizationAnchor,
) -> b.ActuationReceipt | SimulatorRefusal:
    if not _is_authentic_anchor(authority, _ActuatorAuthorizationAnchor):
        return SimulatorRefusal("actuator-binding-invalid", "server anchor required")
    if not isinstance(receipt, Mapping):
        return SimulatorRefusal("actuation-receipt-invalid", "mapping required")
    parsed = b.parse_actuation_receipt(receipt)
    if isinstance(parsed, b.TypedRefusal):
        return SimulatorRefusal("actuation-receipt-invalid", parsed.reason)
    exact = (
        (parsed.data.get("plan_digest"), authority.approved_plan_digest, "plan_digest"),
        (parsed.data.get("request_digest"), authority.request_digest, "request_digest"),
        (parsed.data.get("capability_id"), authority.capability_id, "capability_id"),
        (parsed.data.get("target_ref"), authority.target_ref, "target_ref"),
        (
            parsed.data.get("target_state_digest"),
            authority.target_state_digest,
            "target_state_digest",
        ),
    )
    for actual, expected, key in exact:
        if actual != expected:
            return SimulatorRefusal("actuation-receipt-binding-mismatch", key)
    return parsed


def _cancel(
    raw: bytes,
    receipt: object,
    context: object,
    authority: _ActuatorAuthorizationAnchor,
    current_state: bytes,
) -> b.NativeCancellationResult:
    refused = _validate_actuator_context(context, authority, current_state)
    adapter_id = getattr(authority, "adapter_id", "uninitialized-anchor")
    if not isinstance(refused, SimulatorRefusal):
        refused = _validate_native_receipt(receipt, authority)
    if isinstance(refused, SimulatorRefusal):
        return FixtureNativeCancellationResult(
            "denied",
            ("fixture:context-refused",),
            _fixture_receipt(
                receipt_id=f"receipt:{adapter_id}:cancel-refused",
                adapter_id=adapter_id,
                tick=0,
                operation="cancel-refused",
                payload={"reason": refused.reason},
            ),
        )
    value = _script(raw, "cancel")
    return FixtureNativeCancellationResult(
        str(value.get("state")),
        tuple(str(item) for item in value.get("evidence_refs", ())),
        _fixture_receipt(
            receipt_id=f"receipt:{adapter_id}:cancel",
            adapter_id=adapter_id,
            tick=0,
            operation="cancel",
            payload=value,
        ),
    )


def _rollback(
    raw: bytes,
    receipt: object,
    context: object,
    authority: _ActuatorAuthorizationAnchor,
    current_state: bytes,
) -> b.NativeRollbackResult:
    refused = _validate_actuator_context(context, authority, current_state)
    adapter_id = getattr(authority, "adapter_id", "uninitialized-anchor")
    if not isinstance(refused, SimulatorRefusal):
        refused = _validate_native_receipt(receipt, authority)
    if isinstance(refused, SimulatorRefusal):
        return FixtureNativeRollbackResult(
            "denied",
            None,
            ("fixture:context-refused",),
            _fixture_receipt(
                receipt_id=f"receipt:{adapter_id}:rollback-refused",
                adapter_id=adapter_id,
                tick=0,
                operation="rollback-refused",
                payload={"reason": refused.reason},
            ),
        )
    value = _script(raw, "rollback")
    return FixtureNativeRollbackResult(
        str(value.get("state")),
        value.get("native_receipt_ref") if isinstance(value.get("native_receipt_ref"), str) else None,
        tuple(str(item) for item in value.get("evidence_refs", ())),
        _fixture_receipt(
            receipt_id=f"receipt:{adapter_id}:rollback",
            adapter_id=adapter_id,
            tick=0,
            operation="rollback",
            payload=value,
        ),
    )


@dataclass(frozen=True)
class MobileDroneAdapter(NativeSensorAdapter, b.NativeActuatorAdapter):
    binding: DeviceBinding
    observed_at: str
    outcome_script: bytes
    actuator_binding: _ActuatorAuthorizationAnchor
    actuator_state: bytes
    fixture_only: Literal[True] = field(init=False, default=True)

    def __post_init__(self) -> None:
        if not _is_authentic_anchor(
            self.actuator_binding, _ActuatorAuthorizationAnchor
        ):
            raise ValueError("actuator authorization anchor required")
        if self.actuator_binding.adapter_id != self.binding.adapter_id:
            raise ValueError("actuator binding adapter mismatch")

    def read(
        self, plan: SensorReadPlan, *, durable_state: bytes
    ) -> SensorReadResult | SimulatorRefusal:
        return _read_sensor(
            self.binding,
            plan,
            durable_state,
            capability_id="drone-observe",
            contract="org060-mobile-drone",
            schema="org060-mobile-drone/v1",
            timestamp=self.observed_at,
        )

    def move(
        self, request: DroneMotionRequest, *, durable_state: bytes
    ) -> DeviceStateTransition | SimulatorRefusal:
        state = _decode_device_state(durable_state)
        if isinstance(state, SimulatorRefusal):
            return state
        checked = _device_binding_check(
            self.binding, state, logical_tick=request.logical_tick
        )
        if isinstance(checked, SimulatorRefusal):
            return checked
        state = checked
        if not _nonblank(request.event_id):
            return SimulatorRefusal("motion-request-invalid", "event_id")
        if not _exact_int(request.logical_tick, nonnegative=True) or not _exact_int(
            request.location_observed_tick, nonnegative=True
        ):
            return SimulatorRefusal("logical-time-invalid", request.event_id)
        age = request.logical_tick - request.location_observed_tick
        if age < 0:
            return SimulatorRefusal("logical-time-inconsistent", request.event_id)
        if age > 5:
            return SimulatorRefusal("stale-location", request.event_id)
        if request.native_authority_allowed is not True:
            return SimulatorRefusal("native-authority-denied", request.event_id)
        if state.get("network") != "available":
            return SimulatorRefusal("network-outage", request.event_id)
        if state.get("provider") != "available":
            return SimulatorRefusal("provider-outage", request.event_id)
        if state.get("connected") is not True:
            return SimulatorRefusal("device-unavailable", request.event_id)
        if (
            not isinstance(request.target_position, tuple)
            or len(request.target_position) != 3
            or not all(_exact_int(value) for value in request.target_position)
            or not _exact_int(request.max_distance, nonnegative=True)
            or not _exact_int(request.energy_per_unit, nonnegative=True)
        ):
            return SimulatorRefusal("invalid-motion-number", request.event_id)
        position = state.get("position")
        if not isinstance(position, list):
            return SimulatorRefusal("invalid-position", request.event_id)
        distance = sum(
            abs(a - target)
            for a, target in zip(position, request.target_position, strict=True)
        )
        if distance > request.max_distance:
            return SimulatorRefusal("unsafe-motion", "distance")
        energy = distance * request.energy_per_unit
        battery = state.get("battery")
        if not _exact_int(battery, nonnegative=True) or energy > battery:
            return SimulatorRefusal("unsafe-motion", "energy")
        updated = dict(state)
        updated["position"] = list(request.target_position)
        updated["battery"] = battery - energy
        if updated["battery"] > battery:
            return SimulatorRefusal("unsafe-motion", "battery increase")
        receipt = _fixture_receipt(
            receipt_id=f"receipt:{request.event_id}",
            adapter_id=self.binding.adapter_id,
            tick=request.logical_tick,
            operation="bounded-motion",
            payload={"distance": distance, "energy": energy, "position": updated["position"]},
        )
        return DeviceStateTransition(canonical_bytes(updated), receipt)

    def dispatch(
        self, plan: b.ActuationPlan, *, context: b.AuthenticatedActuationContext
    ) -> b.NativeDispatchResult:
        return _dispatch(
            self.outcome_script,
            plan,
            context,
            self.actuator_binding,
            self.actuator_state,
        )

    def cancel(
        self, receipt: Mapping[str, object], *, context: b.AuthenticatedActuationContext
    ) -> b.NativeCancellationResult:
        return _cancel(
            self.outcome_script,
            receipt,
            context,
            self.actuator_binding,
            self.actuator_state,
        )

    def rollback(
        self, receipt: Mapping[str, object], *, context: b.AuthenticatedActuationContext
    ) -> b.NativeRollbackResult:
        return _rollback(
            self.outcome_script,
            receipt,
            context,
            self.actuator_binding,
            self.actuator_state,
        )


@dataclass(frozen=True)
class IoTSensorAdapter(NativeSensorAdapter):
    binding: DeviceBinding
    observed_at: str
    fixture_only: Literal[True] = field(init=False, default=True)

    def read(
        self, plan: SensorReadPlan, *, durable_state: bytes
    ) -> SensorReadResult | SimulatorRefusal:
        return _read_sensor(
            self.binding,
            plan,
            durable_state,
            capability_id="iot-sensor-observe",
            contract="org060-iot-sensor",
            schema="org060-iot-sensor/v1",
            timestamp=self.observed_at,
        )


def normalize_sensor_read(
    adapter: NativeSensorAdapter,
    plan: SensorReadPlan,
    *,
    durable_state: bytes,
    projection: cn.NavigatorProjection,
    now: object,
    freshness: b.FreshnessWindow,
) -> b.SensoryEnvelope | b.TypedRefusal | SimulatorRefusal:
    """Run device-local preconditions before the canonical Child-B normalizer."""
    if not isinstance(projection, cn.NavigatorProjection):
        return SimulatorRefusal("owner-type-required", "NavigatorProjection")
    result = adapter.read(plan, durable_state=durable_state)
    if isinstance(result, SimulatorRefusal):
        return result
    return b.normalize_observation(
        projection,
        result.observation,
        now=now,  # type: ignore[arg-type]
        freshness=freshness,
    )


@dataclass(frozen=True)
class ReversibleIoTActuatorAdapter(b.NativeActuatorAdapter):
    binding: DeviceBinding
    outcome_script: bytes
    actuator_binding: _ActuatorAuthorizationAnchor
    actuator_state: bytes
    fixture_only: Literal[True] = field(init=False, default=True)

    def __post_init__(self) -> None:
        if not _is_authentic_anchor(
            self.actuator_binding, _ActuatorAuthorizationAnchor
        ):
            raise ValueError("actuator authorization anchor required")
        if self.actuator_binding.adapter_id != self.binding.adapter_id:
            raise ValueError("actuator binding adapter mismatch")

    def dispatch(
        self, plan: b.ActuationPlan, *, context: b.AuthenticatedActuationContext
    ) -> b.NativeDispatchResult:
        return _dispatch(
            self.outcome_script,
            plan,
            context,
            self.actuator_binding,
            self.actuator_state,
        )

    def cancel(
        self, receipt: Mapping[str, object], *, context: b.AuthenticatedActuationContext
    ) -> b.NativeCancellationResult:
        return _cancel(
            self.outcome_script,
            receipt,
            context,
            self.actuator_binding,
            self.actuator_state,
        )

    def rollback(
        self, receipt: Mapping[str, object], *, context: b.AuthenticatedActuationContext
    ) -> b.NativeRollbackResult:
        return _rollback(
            self.outcome_script,
            receipt,
            context,
            self.actuator_binding,
            self.actuator_state,
        )


@dataclass(frozen=True)
class _PresenceAdapter:
    adapter_id: str
    clock: str
    interaction_bytes: bytes = b"{}\n"
    fixture_only: Literal[True] = field(init=False, default=True)

    def render(
        self,
        projection_json: Mapping[str, object],
        *,
        surface_id: str,
        indicators: Mapping[str, str],
    ) -> Mapping[str, object]:
        return {
            "surface_id": surface_id,
            "projection_id": projection_json.get("projection_id"),
            "projection_revision": projection_json.get("projection_revision"),
            "projection_digest": projection_json.get("projection_digest"),
            "displayed": dict(indicators),
            "rendered_at": self.clock,
        }

    def read_interaction(self, *, surface_id: str) -> Mapping[str, object]:
        value = _decode(self.interaction_bytes)
        if isinstance(value, SimulatorRefusal):
            return {}
        if value.get("surface_id", surface_id) != surface_id:
            return {}
        return value


@dataclass(frozen=True)
class FixtureDisplayAdapter(_PresenceAdapter, c.BridgePresenceAdapter):
    pass


@dataclass(frozen=True)
class FixtureWearableAdapter(_PresenceAdapter, c.BridgePresenceAdapter):
    pass


@dataclass(frozen=True)
class FixtureDesktopPresenceAdapter(_PresenceAdapter, c.BridgePresenceAdapter):
    pass


@dataclass(frozen=True)
class FixtureBadgePresenceAdapter(_PresenceAdapter, c.BridgePresenceAdapter):
    pass


def _binding_json(binding: DeviceBinding) -> dict[str, object]:
    return dataclasses.asdict(binding)


def _initial_lifecycle() -> dict[str, object]:
    return {"phase": "new", "revision": None, "epoch": 0, "legs": {}}


def _validate_target_revision(
    leg: object,
    outcome: object,
    target_revision: object,
) -> None | SimulatorRefusal:
    if leg == "install":
        if target_revision is None or _nonblank(target_revision):
            return None
    elif leg == "update" and outcome == "verified":
        if _nonblank(target_revision):
            return None
    elif target_revision is None:
        return None
    return SimulatorRefusal("event-schema-invalid", "target_revision")


def _validate_event(event: object) -> None | SimulatorRefusal:
    if not isinstance(event, SimulationEvent):
        return SimulatorRefusal("event-schema-invalid", "owner type")
    if not _nonblank(event.event_id) or not _nonblank(event.adapter_id):
        return SimulatorRefusal("event-schema-invalid", "identity")
    if not _exact_int(event.logical_tick, nonnegative=True):
        return SimulatorRefusal("event-schema-invalid", "logical_tick")
    if event.crash_point not in (None, "before", "after"):
        return SimulatorRefusal("event-schema-invalid", "crash_point")
    if not isinstance(event.payload, Mapping) or not all(
        isinstance(key, str) for key in event.payload
    ):
        return SimulatorRefusal("event-schema-invalid", "payload")
    keys = set(event.payload)
    if event.kind == "lifecycle":
        if keys != {"leg", "outcome", "evidence_ref", "target_revision"}:
            return SimulatorRefusal("event-schema-invalid", "lifecycle keys")
        if event.payload.get("leg") not in LIFECYCLE_LEGS:
            return SimulatorRefusal("event-schema-invalid", "lifecycle leg")
        if event.payload.get("outcome") not in LIFECYCLE_OUTCOMES[str(event.payload["leg"])] or not _nonblank(
            event.payload.get("evidence_ref")
        ):
            return SimulatorRefusal("event-schema-invalid", "lifecycle values")
        target_revision = event.payload.get("target_revision")
        revision_refused = _validate_target_revision(
            event.payload.get("leg"),
            event.payload.get("outcome"),
            target_revision,
        )
        if isinstance(revision_refused, SimulatorRefusal):
            return revision_refused
        return None
    if event.kind in ("outage", "recovery"):
        if keys != {"branch"} or event.payload.get("branch") not in (
            "network",
            "provider",
            "verification",
        ):
            return SimulatorRefusal("event-schema-invalid", "outage payload")
        return None
    if event.kind == "record":
        if keys:
            return SimulatorRefusal("event-schema-invalid", "record payload must be empty")
        return None
    return SimulatorRefusal("event-schema-invalid", "kind")


def _decode_kernel_state(raw: object) -> dict[str, object] | SimulatorRefusal:
    state = _decode(raw)
    if isinstance(state, SimulatorRefusal):
        return state
    if set(state) != {
        "schema_version",
        "logical_tick",
        "adapters",
        "applied_event_ids",
        "transcript",
    } or state.get("schema_version") != SCHEMA_VERSION:
        return SimulatorRefusal("kernel-state-schema-invalid", "top-level")
    if not _exact_int(state.get("logical_tick"), nonnegative=True):
        return SimulatorRefusal("kernel-state-schema-invalid", "logical_tick")
    applied = state.get("applied_event_ids")
    if (
        not isinstance(applied, list)
        or not all(_nonblank(item) for item in applied)
        or len(applied) != len(set(applied))
    ):
        return SimulatorRefusal("kernel-state-schema-invalid", "applied_event_ids")
    adapters = state.get("adapters")
    if not isinstance(adapters, dict) or not all(_nonblank(key) for key in adapters):
        return SimulatorRefusal("kernel-state-schema-invalid", "adapters")
    binding_keys = set(DeviceBinding.__dataclass_fields__)
    for adapter_id, adapter in adapters.items():
        if not isinstance(adapter, dict) or set(adapter) not in (
            {"binding", "lifecycle", "availability"},
            {"binding", "lifecycle", "availability", "last_payload"},
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", f"adapter:{adapter_id}")
        binding = adapter.get("binding")
        if not isinstance(binding, dict) or set(binding) != binding_keys:
            return SimulatorRefusal("kernel-state-schema-invalid", f"binding:{adapter_id}")
        if binding.get("adapter_id") != adapter_id:
            return SimulatorRefusal("kernel-state-schema-invalid", f"binding-id:{adapter_id}")
        if binding.get("calibration_device_id") != binding.get("device_id"):
            return SimulatorRefusal("kernel-state-schema-invalid", "binding calibration device")
        for key in binding_keys - {"calibration_valid_through_tick"}:
            if not _nonblank(binding.get(key)):
                return SimulatorRefusal("kernel-state-schema-invalid", f"binding:{key}")
        if not _exact_int(
            binding.get("calibration_valid_through_tick"), nonnegative=True
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", "binding calibration")
        availability = adapter.get("availability")
        if (
            not isinstance(availability, dict)
            or set(availability) != {"network", "provider", "verification"}
            or not all(type(value) is bool for value in availability.values())
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", "availability")
        lifecycle = adapter.get("lifecycle")
        if not isinstance(lifecycle, dict) or set(lifecycle) != {
            "phase",
            "revision",
            "epoch",
            "legs",
        }:
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle")
        if lifecycle.get("phase") not in (
            "new",
            "present",
            "update_interrupted",
            "removed",
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle phase")
        revision = lifecycle.get("revision")
        if revision is not None and not _nonblank(revision):
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle revision")
        epoch = lifecycle.get("epoch")
        if not _exact_int(epoch, nonnegative=True):
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle epoch")
        legs = lifecycle.get("legs")
        if not isinstance(legs, dict) or not set(legs).issubset(LIFECYCLE_LEGS):
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle legs")
        for leg, record in legs.items():
            if not isinstance(record, dict) or set(record) != {
                "outcome",
                "evidence_ref",
                "logical_tick",
                "target_revision",
                "epoch",
                "revision",
            }:
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg:{leg}")
            if record.get("outcome") not in LIFECYCLE_OUTCOMES[str(leg)] or not _nonblank(
                record.get("evidence_ref")
            ):
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg:{leg}")
            if not _exact_int(record.get("logical_tick"), nonnegative=True):
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg-time:{leg}")
            target_revision = record.get("target_revision")
            revision_refused = _validate_target_revision(
                leg,
                record.get("outcome"),
                target_revision,
            )
            if isinstance(revision_refused, SimulatorRefusal):
                return SimulatorRefusal(
                    "kernel-state-schema-invalid", f"leg-revision:{leg}"
                )
            if record["logical_tick"] > state["logical_tick"]:
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg-future:{leg}")
            if not _exact_int(record.get("epoch"), nonnegative=True):
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg-epoch:{leg}")
            if not _nonblank(record.get("revision")):
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg-state-revision:{leg}")
            if record["epoch"] > epoch:
                return SimulatorRefusal("kernel-state-schema-invalid", f"leg-epoch-future:{leg}")
        phase = lifecycle["phase"]
        update_outcome = legs.get("update", {}).get("outcome")
        remove_outcome = legs.get("remove", {}).get("outcome")
        if phase == "new" and (legs or revision is not None or epoch != 0):
            return SimulatorRefusal("kernel-state-schema-invalid", "new lifecycle")
        if phase != "new" and (not _nonblank(revision) or epoch < 1):
            return SimulatorRefusal("kernel-state-schema-invalid", "active lifecycle")
        if phase == "update_interrupted" and update_outcome != "interrupted":
            return SimulatorRefusal("kernel-state-schema-invalid", "interrupted lifecycle")
        if phase == "removed" and remove_outcome != "executed":
            return SimulatorRefusal("kernel-state-schema-invalid", "removed lifecycle")
        if phase == "present" and (
            update_outcome == "interrupted" or remove_outcome == "executed"
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", "present lifecycle")
        leg_names = set(legs)
        permitted_shapes = {
            frozenset(),
            frozenset({"install"}),
            frozenset({"install", "configure"}),
            frozenset({"install", "configure", "health"}),
            frozenset({"install", "configure", "update"}),
            frozenset({"install", "configure", "health", "update"}),
            frozenset({"install", "configure", "update", "remove"}),
            frozenset(LIFECYCLE_LEGS),
        }
        if frozenset(leg_names) not in permitted_shapes:
            return SimulatorRefusal("kernel-state-schema-invalid", "lifecycle order")
        if phase != "new" and "install" not in legs:
            return SimulatorRefusal("kernel-state-schema-invalid", "install missing")
        if "configure" in legs and legs["configure"]["logical_tick"] < legs["install"]["logical_tick"]:
            return SimulatorRefusal("kernel-state-schema-invalid", "configure time order")
        if "update" in legs:
            if "configure" not in legs or legs["update"]["logical_tick"] < legs["configure"]["logical_tick"]:
                return SimulatorRefusal("kernel-state-schema-invalid", "update time order")
            if legs["update"]["epoch"] != epoch or legs["update"]["revision"] != revision:
                return SimulatorRefusal("kernel-state-schema-invalid", "update epoch binding")
        if "health" in legs:
            health = legs["health"]
            if health["epoch"] != epoch or health["revision"] != revision:
                return SimulatorRefusal("kernel-state-schema-invalid", "health epoch binding")
            predecessor = legs.get("update", legs.get("configure"))
            if not isinstance(predecessor, dict) or health["logical_tick"] < predecessor["logical_tick"]:
                return SimulatorRefusal("kernel-state-schema-invalid", "health time order")
        if phase == "update_interrupted" and "health" in legs:
            return SimulatorRefusal("kernel-state-schema-invalid", "interrupted health")
        if "remove" in legs:
            remove = legs["remove"]
            if remove["epoch"] != epoch or remove["revision"] != revision:
                return SimulatorRefusal("kernel-state-schema-invalid", "remove epoch binding")
            predecessor = legs.get("health", legs.get("update"))
            if not isinstance(predecessor, dict) or remove["logical_tick"] < predecessor["logical_tick"]:
                return SimulatorRefusal("kernel-state-schema-invalid", "remove time order")
        if "last_payload" in adapter and adapter["last_payload"] != {}:
            return SimulatorRefusal("kernel-state-schema-invalid", "last_payload")
    transcript = state.get("transcript")
    if not isinstance(transcript, list):
        return SimulatorRefusal("kernel-state-schema-invalid", "transcript")
    transcript_ids: list[str] = []
    for row in transcript:
        if not isinstance(row, dict) or set(row) != {
            "event_id",
            "logical_tick",
            "adapter_id",
            "kind",
            "payload",
            "receipt_digest",
            "evidence_class",
        }:
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript row")
        if row.get("evidence_class") != FIXTURE_EVIDENCE_CLASS:
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript evidence")
        if not _nonblank(row.get("event_id")) or not _nonblank(row.get("adapter_id")):
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript identity")
        if not _exact_int(row.get("logical_tick"), nonnegative=True):
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript time")
        if row["logical_tick"] > state["logical_tick"]:
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript future")
        if not isinstance(row.get("payload"), dict) or not _nonblank(
            row.get("receipt_digest")
        ):
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript payload")
        if row.get("adapter_id") not in adapters:
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript adapter")
        event_preimage = {
            "event_id": row["event_id"],
            "logical_tick": row["logical_tick"],
            "adapter_id": row["adapter_id"],
            "kind": row["kind"],
            "payload": row["payload"],
            "evidence_class": row["evidence_class"],
        }
        if row.get("receipt_digest") != _sha(event_preimage):
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript digest")
        transcript_ids.append(str(row["event_id"]))
        event_check = _validate_event(
            SimulationEvent(
                event_id=str(row["event_id"]),
                logical_tick=int(row["logical_tick"]),
                adapter_id=str(row["adapter_id"]),
                kind=row.get("kind"),  # type: ignore[arg-type]
                payload=row["payload"],
            )
        )
        if isinstance(event_check, SimulatorRefusal):
            return SimulatorRefusal("kernel-state-schema-invalid", "transcript event")
    if transcript_ids != applied:
        return SimulatorRefusal("kernel-state-schema-invalid", "transcript event ids")
    return state


def _validate_device_binding(binding: object) -> None | SimulatorRefusal:
    if not isinstance(binding, DeviceBinding):
        return SimulatorRefusal("device-binding-invalid", "owner type")
    for key in (
        "adapter_id",
        "device_id",
        "device_revision",
        "calibration_revision",
        "calibration_device_id",
        "location_ref",
    ):
        if not _nonblank(getattr(binding, key)):
            return SimulatorRefusal("device-binding-invalid", key)
    if binding.calibration_device_id != binding.device_id:
        return SimulatorRefusal("device-binding-invalid", "calibration_device_id")
    if not _exact_int(binding.calibration_valid_through_tick, nonnegative=True):
        return SimulatorRefusal("device-binding-invalid", "calibration_valid_through_tick")
    return None


@dataclass(frozen=True)
class DeterministicKernel:
    durable_state: bytes

    @classmethod
    def create(
        cls,
        *,
        adapter_bindings: Sequence[DeviceBinding],
        initial_tick: int,
    ) -> "DeterministicKernel":
        if not _exact_int(initial_tick, nonnegative=True):
            raise ValueError("initial tick invalid")
        adapter_ids: list[str] = []
        for binding in adapter_bindings:
            refused = _validate_device_binding(binding)
            if isinstance(refused, SimulatorRefusal):
                raise ValueError(refused.reason)
            adapter_ids.append(binding.adapter_id)
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("duplicate adapter binding")
        adapters = {
            item.adapter_id: {
                "binding": _binding_json(item),
                "lifecycle": _initial_lifecycle(),
                "availability": {
                    "network": True,
                    "provider": True,
                    "verification": True,
                },
            }
            for item in sorted(adapter_bindings, key=lambda value: value.adapter_id)
        }
        return cls(
            canonical_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "logical_tick": initial_tick,
                    "adapters": adapters,
                    "applied_event_ids": [],
                    "transcript": [],
                }
            )
        )

    @classmethod
    def recover(
        cls,
        durable_state: bytes,
        *,
        anchor: _KernelRecoveryAnchor,
    ) -> "DeterministicKernel":
        if not _is_authentic_anchor(anchor, _KernelRecoveryAnchor):
            raise ValueError("recovery-anchor-required")
        if (
            _sha_bytes(anchor.genesis_bytes) != anchor.genesis_digest
            or _sha_bytes(anchor.checkpoint_bytes) != anchor.checkpoint_digest
        ):
            raise ValueError("recovery-anchor-invalid")
        if type(durable_state) is not bytes or durable_state != anchor.checkpoint_bytes:
            raise ValueError("recovery-checkpoint-mismatch")
        refused = _verify_recovery_checkpoint(anchor.genesis_bytes, durable_state)
        if isinstance(refused, SimulatorRefusal):
            raise ValueError(refused.reason)
        return cls(bytes(durable_state))

    def checkpoint_bytes(self) -> bytes:
        return bytes(self.durable_state)

    def apply(
        self, events: Sequence[SimulationEvent]
    ) -> KernelTransition | SimulatorRefusal:
        current = _decode_kernel_state(self.durable_state)
        if isinstance(current, SimulatorRefusal):
            return current

        def emit_transition(
            durable_state: bytes,
            transcript: bytes,
            transition_receipts: tuple[FixtureReceipt, ...],
            crashed_at: str | None,
        ) -> KernelTransition:
            transition = object.__new__(KernelTransition)
            object.__setattr__(transition, "durable_state", durable_state)
            object.__setattr__(transition, "transcript", transcript)
            object.__setattr__(transition, "receipts", transition_receipts)
            object.__setattr__(transition, "crashed_at", crashed_at)
            object.__setattr__(transition, "_seal", _KERNEL_TRANSITION_SEAL)
            return transition

        receipts: list[FixtureReceipt] = []
        validated_events: list[SimulationEvent] = []
        for event in events:
            refused = _validate_event(event)
            if isinstance(refused, SimulatorRefusal):
                return refused
            validated_events.append(event)
        ordered = sorted(
            validated_events, key=lambda value: (value.logical_tick, value.event_id)
        )
        for event in ordered:
            applied = current.get("applied_event_ids")
            if not isinstance(applied, list):
                return SimulatorRefusal("malformed-durable-state", "applied_event_ids")
            if event.event_id in applied:
                continue
            adapters = current.get("adapters")
            if not isinstance(adapters, dict) or event.adapter_id not in adapters:
                return SimulatorRefusal("unknown-adapter", event.adapter_id)
            if event.logical_tick < int(current.get("logical_tick", -1)):
                return SimulatorRefusal("logical-time-regression", event.event_id)
            if event.crash_point == "before":
                return emit_transition(
                    canonical_bytes(current),
                    canonical_bytes(current.get("transcript", [])),
                    tuple(receipts),
                    f"before:{event.event_id}",
                )
            adapter = adapters[event.adapter_id]
            if not isinstance(adapter, dict):
                return SimulatorRefusal("malformed-durable-state", event.adapter_id)
            refused = self._apply_one(adapter, event)
            if isinstance(refused, SimulatorRefusal):
                return refused
            receipt = _fixture_receipt(
                receipt_id=f"receipt:{event.event_id}",
                adapter_id=event.adapter_id,
                tick=event.logical_tick,
                operation=event.kind,
                payload=_event_receipt_preimage(event),
            )
            receipts.append(receipt)
            applied.append(event.event_id)
            current["logical_tick"] = event.logical_tick
            transcript = current.get("transcript")
            if not isinstance(transcript, list):
                return SimulatorRefusal("malformed-durable-state", "transcript")
            transcript.append(
                {
                    "event_id": event.event_id,
                    "logical_tick": event.logical_tick,
                    "adapter_id": event.adapter_id,
                    "kind": event.kind,
                    "payload": dict(event.payload),
                    "receipt_digest": receipt.payload_digest,
                    "evidence_class": FIXTURE_EVIDENCE_CLASS,
                }
            )
            if event.crash_point == "after":
                return emit_transition(
                    canonical_bytes(current),
                    canonical_bytes(transcript),
                    tuple(receipts),
                    f"after:{event.event_id}",
                )
        return emit_transition(
            canonical_bytes(current),
            canonical_bytes(current.get("transcript", [])),
            tuple(receipts),
            None,
        )

    @staticmethod
    def _apply_one(
        adapter: dict[str, object], event: SimulationEvent
    ) -> None | SimulatorRefusal:
        if event.kind == "lifecycle":
            try:
                lifecycle_event = LifecycleEvent(
                    event_id=event.event_id,
                    adapter_id=event.adapter_id,
                    logical_tick=event.logical_tick,
                    leg=event.payload["leg"],  # type: ignore[arg-type]
                    outcome=str(event.payload["outcome"]),
                    evidence_ref=str(event.payload["evidence_ref"]),
                    target_revision=event.payload.get("target_revision"),  # type: ignore[arg-type]
                )
            except (KeyError, TypeError):
                return SimulatorRefusal("malformed-lifecycle-event", event.event_id)
            return _apply_lifecycle(adapter, lifecycle_event)
        if event.kind in ("outage", "recovery"):
            branch = event.payload.get("branch")
            if branch not in ("network", "provider", "verification"):
                return SimulatorRefusal("unknown-outage-branch", event.event_id)
            availability = adapter.get("availability")
            if not isinstance(availability, dict):
                return SimulatorRefusal("malformed-durable-state", "availability")
            availability[str(branch)] = event.kind == "recovery"
            return None
        adapter["last_payload"] = dict(event.payload)
        return None

    def evaluate_lifecycle(
        self,
        adapter_id: str,
        *,
        now_tick: int,
        health_freshness_ticks: int,
    ) -> LifecycleReadiness | SimulatorRefusal:
        state = _decode_kernel_state(self.durable_state)
        if isinstance(state, SimulatorRefusal):
            return state
        if not _exact_int(now_tick, nonnegative=True) or not _exact_int(
            health_freshness_ticks, nonnegative=True
        ):
            return SimulatorRefusal("logical-time-invalid", adapter_id)
        if now_tick < state["logical_tick"]:
            return SimulatorRefusal("logical-time-regression", adapter_id)
        adapters = state.get("adapters")
        if not isinstance(adapters, dict) or not isinstance(adapters.get(adapter_id), dict):
            return SimulatorRefusal("unknown-adapter", adapter_id)
        adapter = adapters[adapter_id]
        lifecycle = adapter.get("lifecycle")
        if not isinstance(lifecycle, dict):
            return SimulatorRefusal("malformed-durable-state", "lifecycle")
        legs = lifecycle.get("legs")
        if not isinstance(legs, dict):
            return SimulatorRefusal("malformed-durable-state", "legs")
        update = legs.get("update")
        if isinstance(update, dict) and update.get("outcome") == "interrupted":
            return SimulatorRefusal("update-interrupted", adapter_id)
        if lifecycle.get("phase") != "present":
            return SimulatorRefusal("lifecycle-not-present", str(lifecycle.get("phase")))
        health = legs.get("health")
        if not isinstance(health, dict) or health.get("outcome") in (None, "unknown"):
            return SimulatorRefusal("health-unknown", adapter_id)
        if health.get("outcome") != "healthy":
            return SimulatorRefusal("health-unhealthy", adapter_id)
        missing = [leg for leg in LIFECYCLE_LEGS if leg not in legs]
        if missing:
            return SimulatorRefusal("lifecycle-leg-missing", missing[0])
        if not isinstance(update, dict):
            return SimulatorRefusal("lifecycle-leg-missing", "update")
        observed = health.get("logical_tick")
        if not _exact_int(observed, nonnegative=True) or now_tick < observed:
            return SimulatorRefusal("logical-time-regression", adapter_id)
        if now_tick - observed > health_freshness_ticks:
            return SimulatorRefusal("health-stale", adapter_id)
        availability = adapter.get("availability")
        if not isinstance(availability, dict) or not all(
            availability.get(branch) is True
            for branch in ("network", "provider", "verification")
        ):
            return SimulatorRefusal("operational-outage", adapter_id)
        return LifecycleReadiness(
            adapter_id,
            _sha(lifecycle),
            now_tick,
            observed,
            int(lifecycle["epoch"]),
            str(lifecycle["revision"]),
        )


def _validate_trusted_genesis(raw: bytes) -> dict[str, object] | SimulatorRefusal:
    state = _decode_kernel_state(raw)
    if isinstance(state, SimulatorRefusal):
        return state
    if state["applied_event_ids"] != [] or state["transcript"] != []:
        return SimulatorRefusal("recovery-genesis-invalid", "genesis must be closed and empty")
    adapters = state["adapters"]
    assert isinstance(adapters, dict)
    for adapter in adapters.values():
        if not isinstance(adapter, dict):
            return SimulatorRefusal("recovery-genesis-invalid", "adapter")
        if set(adapter) != {"binding", "lifecycle", "availability"}:
            return SimulatorRefusal("recovery-genesis-invalid", "adapter fields")
        if adapter["lifecycle"] != _initial_lifecycle():
            return SimulatorRefusal("recovery-genesis-invalid", "lifecycle")
        if adapter["availability"] != {
            "network": True,
            "provider": True,
            "verification": True,
        }:
            return SimulatorRefusal("recovery-genesis-invalid", "availability")
    return state


def _verify_recovery_checkpoint(
    genesis_bytes: bytes,
    checkpoint_bytes: bytes,
) -> None | SimulatorRefusal:
    genesis = _validate_trusted_genesis(genesis_bytes)
    if isinstance(genesis, SimulatorRefusal):
        return genesis
    checkpoint = _decode_kernel_state(checkpoint_bytes)
    if isinstance(checkpoint, SimulatorRefusal):
        return checkpoint
    if checkpoint["adapters"].keys() != genesis["adapters"].keys():  # type: ignore[union-attr]
        return SimulatorRefusal("recovery-replay-mismatch", "adapter set")
    kernel = DeterministicKernel(bytes(genesis_bytes))
    transcript = checkpoint["transcript"]
    assert isinstance(transcript, list)
    for row in transcript:
        assert isinstance(row, dict)
        event = SimulationEvent(
            event_id=str(row["event_id"]),
            logical_tick=int(row["logical_tick"]),
            adapter_id=str(row["adapter_id"]),
            kind=str(row["kind"]),
            payload=row["payload"],  # type: ignore[arg-type]
        )
        transition = kernel.apply((event,))
        if isinstance(transition, SimulatorRefusal):
            return SimulatorRefusal("recovery-replay-mismatch", transition.reason)
        if len(transition.receipts) != 1:
            return SimulatorRefusal("recovery-replay-mismatch", "receipt count")
        if transition.receipts[0].payload_digest != row["receipt_digest"]:
            return SimulatorRefusal("recovery-replay-mismatch", "receipt digest")
        kernel = DeterministicKernel(transition.durable_state)
    if kernel.durable_state != checkpoint_bytes:
        return SimulatorRefusal("recovery-replay-mismatch", "checkpoint bytes")
    if canonical_bytes(transcript) != canonical_bytes(
        _decode_kernel_state(kernel.durable_state)["transcript"]  # type: ignore[index]
    ):
        return SimulatorRefusal("recovery-replay-mismatch", "transcript bytes")
    return None


def _bind_server_recovery_anchor(
    *,
    trusted_genesis: DeterministicKernel,
    trusted_checkpoint: KernelTransition,
) -> _KernelRecoveryAnchor | SimulatorRefusal:
    """Mint an anchor only after proving the checkpoint derives from genesis."""
    if type(trusted_genesis) is not DeterministicKernel:
        return SimulatorRefusal("recovery-genesis-invalid", "owner type")
    if (
        type(trusted_checkpoint) is not KernelTransition
        or getattr(trusted_checkpoint, "_seal", None) is not _KERNEL_TRANSITION_SEAL
    ):
        return SimulatorRefusal("recovery-checkpoint-invalid", "owner type")
    checkpoint = _decode_kernel_state(trusted_checkpoint.durable_state)
    if isinstance(checkpoint, SimulatorRefusal):
        return checkpoint
    if trusted_checkpoint.transcript != canonical_bytes(checkpoint["transcript"]):
        return SimulatorRefusal("recovery-checkpoint-invalid", "transcript bytes")
    if type(trusted_checkpoint.receipts) is not tuple:
        return SimulatorRefusal("recovery-checkpoint-invalid", "receipt container")
    transcript_by_event = {
        row["event_id"]: row for row in checkpoint["transcript"]  # type: ignore[index]
    }
    receipt_ids: set[str] = set()
    for receipt in trusted_checkpoint.receipts:
        if type(receipt) is not FixtureReceipt:
            return SimulatorRefusal("recovery-checkpoint-invalid", "receipt owner type")
        row = transcript_by_event.get(receipt.receipt_id.removeprefix("receipt:"))
        if (
            not isinstance(row, dict)
            or receipt.receipt_id in receipt_ids
            or receipt.adapter_id != row.get("adapter_id")
            or receipt.logical_tick != row.get("logical_tick")
            or receipt.operation != row.get("kind")
            or receipt.owner_receipt_digest is not None
            or receipt.payload_digest != row.get("receipt_digest")
            or receipt.evidence_class != FIXTURE_EVIDENCE_CLASS
        ):
            return SimulatorRefusal("recovery-checkpoint-invalid", "receipt binding")
        receipt_ids.add(receipt.receipt_id)
    refused = _verify_recovery_checkpoint(
        trusted_genesis.durable_state,
        trusted_checkpoint.durable_state,
    )
    if isinstance(refused, SimulatorRefusal):
        return refused
    anchor = object.__new__(_KernelRecoveryAnchor)
    object.__setattr__(anchor, "genesis_bytes", bytes(trusted_genesis.durable_state))
    object.__setattr__(
        anchor, "genesis_digest", _sha_bytes(trusted_genesis.durable_state)
    )
    object.__setattr__(
        anchor, "checkpoint_bytes", bytes(trusted_checkpoint.durable_state)
    )
    object.__setattr__(
        anchor, "checkpoint_digest", _sha_bytes(trusted_checkpoint.durable_state)
    )
    object.__setattr__(anchor, "_seal", _ANCHOR_SEAL)
    return anchor


def _apply_lifecycle(
    adapter: dict[str, object], event: LifecycleEvent
) -> None | SimulatorRefusal:
    if event.leg not in LIFECYCLE_LEGS:
        return SimulatorRefusal("invalid-lifecycle-leg", event.leg)
    revision_refused = _validate_target_revision(
        event.leg,
        event.outcome,
        event.target_revision,
    )
    if isinstance(revision_refused, SimulatorRefusal):
        return revision_refused
    lifecycle = adapter.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return SimulatorRefusal("malformed-durable-state", "lifecycle")
    legs = lifecycle.get("legs")
    if not isinstance(legs, dict):
        return SimulatorRefusal("malformed-durable-state", "legs")
    phase = lifecycle.get("phase")
    if event.leg == "install":
        if event.outcome != "verified" or phase not in ("new", "removed"):
            return SimulatorRefusal("lifecycle-transition-invalid", "install")
        lifecycle["phase"] = "present"
        lifecycle["epoch"] = int(lifecycle["epoch"]) + 1
        lifecycle["revision"] = event.target_revision or "fixture-revision-1"
        lifecycle["legs"] = {}
        legs = lifecycle["legs"]
    elif event.leg == "configure":
        if (
            phase != "present"
            or "install" not in legs
            or "configure" in legs
            or event.outcome != "verified"
        ):
            return SimulatorRefusal("lifecycle-transition-invalid", "configure")
    elif event.leg == "health":
        if (
            phase != "present"
            or "configure" not in legs
            or event.outcome
            not in ("healthy", "degraded", "unhealthy", "unknown")
        ):
            return SimulatorRefusal("lifecycle-transition-invalid", "health")
    elif event.leg == "update":
        recovering = (
            phase == "update_interrupted"
            and isinstance(legs.get("update"), dict)
            and legs["update"].get("outcome") == "interrupted"  # type: ignore[union-attr]
            and event.outcome == "rolled_back"
        )
        starting = (
            phase == "present"
            and all(leg in legs for leg in ("install", "configure", "health"))
            and "remove" not in legs
            and event.outcome in ("verified", "interrupted")
        )
        if not (starting or recovering):
            return SimulatorRefusal("lifecycle-transition-invalid", "update")
        if event.outcome == "verified" and not _nonblank(event.target_revision):
            return SimulatorRefusal("lifecycle-transition-invalid", "update revision")
        lifecycle["epoch"] = int(lifecycle["epoch"]) + 1
        lifecycle["phase"] = (
            "update_interrupted" if event.outcome == "interrupted" else "present"
        )
        if event.outcome == "verified":
            lifecycle["revision"] = event.target_revision
        legs.pop("health", None)
        legs.pop("remove", None)
    elif event.leg == "remove":
        rehearsing = (
            phase == "present"
            and all(leg in legs for leg in ("install", "configure", "health", "update"))
            and "remove" not in legs
            and event.outcome == "rehearsed"
        )
        executing = (
            (
                phase == "update_interrupted"
                or (
                    phase == "present"
                    and isinstance(legs.get("remove"), dict)
                    and legs["remove"].get("outcome") == "rehearsed"  # type: ignore[union-attr]
                )
            )
            and event.outcome == "executed"
        )
        if not (rehearsing or executing):
            return SimulatorRefusal("lifecycle-transition-invalid", "remove")
        if event.outcome == "executed":
            lifecycle["phase"] = "removed"
    legs[event.leg] = {
        "outcome": event.outcome,
        "evidence_ref": event.evidence_ref,
        "logical_tick": event.logical_tick,
        "target_revision": event.target_revision,
        "epoch": lifecycle["epoch"],
        "revision": lifecycle["revision"],
    }
    return None


CONFORMANCE_MANIFEST: Final[Mapping[str, tuple[str, str]]] = {
    "test_simulator_clock_and_replay_are_byte_deterministic": ("threat:ambient-order-or-session-replay", "_threat_01"),
    "test_fixed_microphone_binds_identity_geometry_calibration_location_and_consent": ("threat:microphone-location-swap", "_threat_02"),
    "test_microphone_refuses_bystander_revocation_and_stale_or_swapped_calibration": ("threat:stale-calibration-before-normalize", "_threat_03"),
    "test_camera_keeps_raw_local_and_emits_only_permitted_derived_event": ("threat:occluded-camera", "_threat_04"),
    "test_camera_refuses_occluded_ambiguous_or_forbidden_egress": ("threat:malformed-camera-state", "_threat_05"),
    "test_mobile_drone_tracks_position_battery_connectivity_and_bounded_motion": ("threat:unbounded-drone-motion", "_threat_06"),
    "test_drone_refuses_stale_location_authority_denial_and_unsafe_motion": ("threat:drone-authority-denial", "_threat_07"),
    "test_iot_sensor_observation_consumes_real_child_b_normalization_and_policy": ("threat:fake-owner-projection", "_threat_08"),
    "test_reversible_actuator_consumes_real_child_b_plan_invoke_verify_cancel_and_rollback": ("threat:logical-time-regression", "_threat_09"),
    "test_actuator_distinguishes_dispatch_verification_cancellation_and_residual_rollback_risk": ("threat:missing-lifecycle-state", "_threat_10"),
    "test_display_and_wearable_expose_truthful_privacy_recording_action_and_availability": ("threat:forged-live-evidence", "_threat_11"),
    "test_two_presence_surfaces_share_one_checkpoint_and_replace_without_continuity_fork": ("threat:caller-authority-lookalike", "_threat_12"),
    "test_presence_outage_degradation_restart_and_durable_recovery_use_real_child_c_contracts": ("threat:session-object-recovery", "_threat_13"),
    "test_all_fixture_adapters_require_five_lifecycle_legs_and_externally_legible_health": ("threat:missing-remove-leg", "_threat_14"),
    "test_interrupted_update_remove_reprovision_and_crash_points_recover_deterministically": ("threat:crash-time-regression", "_threat_15"),
    "test_provider_network_and_verification_outages_fail_closed_then_recover": ("threat:provider-outage-promoted-ready", "_threat_16"),
    "test_real_t3219_discovery_and_planning_choose_only_declared_fixture_provider": ("threat:undeclared-provider", "_threat_17"),
    "test_fixture_only_marker_cannot_be_bypassed_or_claim_live_org061_evidence": ("threat:live-stage-claim", "_threat_18"),
    "test_no_simulator_path_performs_io_or_accepts_caller_identity_authority_lookalikes": ("threat:shape-compatible-owner-fake", "_threat_19"),
}
