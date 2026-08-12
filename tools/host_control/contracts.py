"""Typed contracts for the harness-neutral host planning core."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from tools.persona_installer import InstallPlan


ApprovalClass = Literal["none", "agent", "human"]
HostAction = Literal["plan", "apply", "verify", "rollback"]
PolicyReasonCode = Literal[
    "allowed",
    "subject_denied",
    "target_denied",
    "manifest_denied",
    "family_denied",
    "approval_class_denied",
]

_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_APPROVAL_CLASSES = frozenset(("none", "agent", "human"))


class HostControlError(RuntimeError):
    """Base class for bounded host-control failures."""


class RegistryError(HostControlError):
    """Trusted registry or file input is invalid."""


class PolicyDenied(HostControlError):
    """The closed host policy denied an operation."""

    reason_code: PolicyReasonCode

    def __init__(self, reason_code: PolicyReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class IdempotencyConflict(HostControlError):
    """An idempotency tuple was reused for a different request."""


class UnknownHandle(HostControlError):
    """A plan handle does not exist."""


class SubjectMismatch(HostControlError):
    """A plan exists but belongs to another authenticated subject."""


def canonical_json(value: object) -> str:
    """Return the one canonical JSON representation used by this package."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _closed_hex(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise HostControlError(f"invalid {field_name}")
    return value


def _closed_string(value: object, field_name: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise HostControlError(f"invalid {field_name}")
    return value


@dataclass(frozen=True)
class SubjectContext:
    uid: int
    seat_id: str
    persona: str
    role: str
    instance: str


@dataclass(frozen=True)
class SeatRecord:
    seat_id: str
    uid: int
    persona: str
    role: str
    instance: str
    account: str
    harness: Literal["claude", "codex"]
    install_root: Path


@dataclass(frozen=True)
class ManifestRecord:
    revision: str
    path: Path
    sha256: str
    framework_revision: str
    framework_sha256: str
    template_ids: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityGrant:
    action: HostAction
    target_seat_id: str
    operation_families: tuple[str, ...]
    approval_class: ApprovalClass


@dataclass(frozen=True)
class CapabilitiesResult:
    contract_version: str
    broker_revision: str
    policy_revision: str
    health: Literal["ready", "degraded"]
    host_operation_families: tuple[str, ...]
    grants: tuple[CapabilityGrant, ...]


@dataclass(frozen=True)
class PlanEnvelope:
    schema: str
    plan_handle: str
    owner: SubjectContext
    target_seat_id: str
    manifest_revision: str
    manifest_sha256: str
    framework_revision: str
    framework_sha256: str
    template_ids: tuple[str, ...]
    policy_revision: str
    operation_families: tuple[str, ...]
    approval_class: ApprovalClass
    created_at: str
    expires_at: str
    installer_plan: InstallPlan
    effect_digest: str
    digest: str

    def digest_payload(self) -> Mapping[str, object]:
        value = dict(self.to_dict())
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: object) -> "PlanEnvelope":
        candidate = cls(digest="", **kwargs)  # type: ignore[arg-type]
        digest = sha256_json(candidate.digest_payload())
        return dataclasses.replace(candidate, digest=digest)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PlanEnvelope":
        expected = {item.name for item in dataclasses.fields(cls)}
        if set(raw) != expected:
            raise HostControlError("invalid plan envelope fields")
        owner_raw = raw["owner"]
        inner_raw = raw["installer_plan"]
        if not isinstance(owner_raw, Mapping) or not isinstance(inner_raw, Mapping):
            raise HostControlError("invalid nested plan fields")
        try:
            owner = SubjectContext(**owner_raw)  # type: ignore[arg-type]
            installer_plan = InstallPlan.from_dict(inner_raw)
            candidate = cls(
                schema=str(raw["schema"]),
                plan_handle=str(raw["plan_handle"]),
                owner=owner,
                target_seat_id=str(raw["target_seat_id"]),
                manifest_revision=str(raw["manifest_revision"]),
                manifest_sha256=str(raw["manifest_sha256"]),
                framework_revision=str(raw["framework_revision"]),
                framework_sha256=str(raw["framework_sha256"]),
                template_ids=tuple(raw["template_ids"]),  # type: ignore[arg-type]
                policy_revision=str(raw["policy_revision"]),
                operation_families=tuple(raw["operation_families"]),  # type: ignore[arg-type]
                approval_class=str(raw["approval_class"]),  # type: ignore[arg-type]
                created_at=str(raw["created_at"]),
                expires_at=str(raw["expires_at"]),
                installer_plan=installer_plan,
                effect_digest=str(raw["effect_digest"]),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostControlError("invalid plan envelope") from exc
        candidate._validate_shape()
        return candidate

    def _validate_shape(self) -> None:
        if self.schema != "pantheon-host-plan-envelope/v1":
            raise HostControlError("invalid plan envelope schema")
        _closed_hex(self.plan_handle, "plan handle")
        _closed_hex(self.manifest_sha256, "manifest digest")
        _closed_hex(self.framework_sha256, "framework digest")
        _closed_hex(self.effect_digest, "effect digest")
        _closed_hex(self.digest, "plan digest")
        if self.approval_class not in _APPROVAL_CLASSES:
            raise HostControlError("invalid approval class")
        if not self.installer_plan.validate_digest():
            raise HostControlError("invalid installer plan digest")
        if not self.operation_families:
            raise HostControlError("empty operation family set")
        for name in (
            self.target_seat_id,
            self.manifest_revision,
            self.framework_revision,
            self.policy_revision,
        ):
            _closed_string(name, "plan identifier")

    def validate_digest(self) -> bool:
        try:
            self._validate_shape()
        except HostControlError:
            return False
        return sha256_json(self.digest_payload()) == self.digest


@dataclass(frozen=True)
class PlanResult:
    plan_handle: str
    plan_digest: str
    effect_digest: str
    summary_json: str
    replayed: bool


def effect_digest_payload(
    *,
    target: SeatRecord,
    manifest: ManifestRecord,
    installer_plan: InstallPlan,
) -> Mapping[str, object]:
    """Project an installer plan onto the closed execution-effect contract."""

    if not installer_plan.validate_digest():
        raise HostControlError("invalid installer plan digest")
    expected = {
        "instance": target.instance,
        "role": target.role,
        "persona": target.persona,
        "account": target.account,
        "selected_harness": target.harness,
        "root": str(target.install_root),
    }
    for field_name, wanted in expected.items():
        if getattr(installer_plan, field_name) != wanted:
            raise HostControlError(f"target mismatch: {field_name}")
    if installer_plan.provenance.get("declared_revision") != manifest.framework_revision:
        raise HostControlError("framework revision mismatch")
    if (
        installer_plan.provenance.get("source_artifact_sha256")
        != manifest.framework_sha256
    ):
        raise HostControlError("framework digest mismatch")
    _closed_hex(manifest.sha256, "registered manifest digest")
    _closed_hex(manifest.framework_sha256, "registered framework digest")
    if not manifest.template_ids:
        raise HostControlError("registered template ids missing")
    source_operations = tuple(
        operation
        for operation in installer_plan.operations
        if operation.operation_id == "framework.source"
    )
    if len(source_operations) != 1:
        raise HostControlError("framework source operation missing")
    registered_artifacts = (
        "tools/persona_installer.py",
        *manifest.template_ids,
    )
    if (
        tuple(source_operations[0].desired.get("artifacts", "").split(","))
        != registered_artifacts
    ):
        raise HostControlError("registered template binding mismatch")

    operations = [
        {
            "operation_id": item.operation_id,
            "layer": item.layer,
            "kind": item.kind,
            "target": item.target,
            "precondition": _jsonable(item.precondition),
            "desired": _jsonable(item.desired),
            "inverse": _jsonable(item.inverse),
            "privilege": item.privilege,
            "owner": item.owner,
        }
        for item in installer_plan.operations
    ]
    actions = [
        {
            "action_id": item.action_id,
            "layer": item.layer,
            "target": item.target,
            "reason": item.reason,
            "required_capability": item.required_capability,
        }
        for item in installer_plan.operator_actions
    ]
    return {
        "schema": "pantheon-host-effect/v1",
        "target": {
            "seat_id": target.seat_id,
            "instance": target.instance,
            "role": target.role,
            "persona": target.persona,
            "account": target.account,
        },
        "manifest": {
            "revision": manifest.revision,
            "sha256": manifest.sha256,
            "framework_revision": manifest.framework_revision,
            "framework_sha256": manifest.framework_sha256,
            "template_ids": list(manifest.template_ids),
            "manifest_schema_version": installer_plan.manifest_version,
        },
        "harness": {
            "configured": installer_plan.configured_harness,
            "selected": installer_plan.selected_harness,
            "install_root": installer_plan.root,
        },
        "operations": operations,
        "operator_actions": actions,
    }


def compute_effect_digest(
    *,
    target: SeatRecord,
    manifest: ManifestRecord,
    installer_plan: InstallPlan,
) -> str:
    return sha256_json(
        effect_digest_payload(
            target=target,
            manifest=manifest,
            installer_plan=installer_plan,
        )
    )
