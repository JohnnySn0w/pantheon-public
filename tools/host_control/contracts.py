"""Typed contracts for the harness-neutral host planning core."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from tools.persona_installer import InstallPlan, MutationRecord, OperationEvidence


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

OperationKind = Literal["apply", "rollback", "verify"]
OperationState = Literal[
    "prepared",
    "authorized",
    "dispatch_intent",
    "dispatched",
    "completed",
    "failed",
    "refused",
    "manual_recovery",
]
ReceiptPhase = Literal["staged", "complete", "failed"]

OPERATION_SCHEMA = "pantheon-host-operation/v1"
OPERATION_RECEIPT_SCHEMA = "pantheon-host-operation-receipt/v1"

# "verify" is accepted at the SCHEMA tier (closed enum + CHECK constraint) so
# that T-2917 can land the verify path without a v2->v3 journal migration.
# The service tier refuses it explicitly until then (directed relaxation,
# broker-po disposition on T-2909 open question 3).
OPERATION_KINDS = frozenset(("apply", "rollback", "verify"))

OPERATION_STATES = frozenset(
    (
        "prepared",
        "authorized",
        "dispatch_intent",
        "dispatched",
        "completed",
        "failed",
        "refused",
        "manual_recovery",
    )
)
TERMINAL_OPERATION_STATES = frozenset(
    ("completed", "failed", "refused", "manual_recovery")
)

# Closed transition table (ORG-052 sections 5 and 6). Terminals have no
# outgoing edges; manual_recovery is loud and non-reusable by construction.
# The completed/failed/manual_recovery edges out of dispatch_intent exist only
# for the recovery reconciliation path.
OPERATION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "prepared": frozenset(("authorized", "refused")),
    "authorized": frozenset(("dispatch_intent",)),
    "dispatch_intent": frozenset(
        ("dispatched", "completed", "failed", "manual_recovery")
    ),
    "dispatched": frozenset(("completed", "failed", "manual_recovery")),
    "completed": frozenset(),
    "failed": frozenset(),
    "refused": frozenset(),
    "manual_recovery": frozenset(),
}

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


class MigrationRequired(HostControlError):
    """The journal is a valid v1 database and needs an explicit migration."""


class InvalidTransition(HostControlError):
    """An operation state change is outside the closed transition table."""


class ApprovalUnavailable(HostControlError):
    """The approval authority is unreachable; retry later, fail closed."""


class RecoveryRefused(HostControlError):
    """Recovery refuses loudly; any bound grant remains consumed."""


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


def _string_mapping(value: object, field_name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise HostControlError(f"invalid {field_name}")
    return {key: item for key, item in value.items()}


def _mutation_from_dict(raw: object) -> MutationRecord:
    expected = {item.name for item in dataclasses.fields(MutationRecord)}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise HostControlError("invalid mutation record fields")
    after = raw["after_fingerprint"]
    if after is not None and not isinstance(after, str):
        raise HostControlError("invalid mutation fingerprint")
    if not isinstance(raw["changed"], bool):
        raise HostControlError("invalid mutation changed flag")
    return MutationRecord(
        operation_id=_closed_string(raw["operation_id"], "mutation id"),
        kind=_closed_string(raw["kind"], "mutation kind"),
        target=_closed_string(raw["target"], "mutation target", maximum=1024),
        owner=_closed_string(raw["owner"], "mutation owner"),
        changed=raw["changed"],
        before=_string_mapping(raw["before"], "mutation before"),
        after_fingerprint=after,
    )


def _evidence_from_dict(raw: object) -> OperationEvidence:
    expected = {item.name for item in dataclasses.fields(OperationEvidence)}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise HostControlError("invalid evidence record fields")
    layer = raw["layer"]
    if not isinstance(layer, int) or isinstance(layer, bool):
        raise HostControlError("invalid evidence layer")
    if not isinstance(raw["ok"], bool):
        raise HostControlError("invalid evidence ok flag")
    if not isinstance(raw["detail"], str):
        raise HostControlError("invalid evidence detail")
    return OperationEvidence(
        operation_id=_closed_string(raw["operation_id"], "evidence id"),
        layer=layer,
        kind=_closed_string(raw["kind"], "evidence kind"),
        ok=raw["ok"],
        detail=raw["detail"],
        evidence=_string_mapping(raw["evidence"], "evidence payload"),
    )


@dataclass(frozen=True)
class BindOutcome:
    """Typed result of the approval service's ValidateAndBind seam."""

    state: Literal["bound", "denied"]
    authority_record_id: str | None
    reason_code: str


@dataclass(frozen=True)
class ReconcileOutcome:
    """Typed result of live-state reconciliation for an unresolved dispatch."""

    outcome: Literal[
        "clean_prestate",
        "complete_poststate",
        "restored",
        "irreconcilable",
    ]
    mutations: tuple[MutationRecord, ...]
    evidence: tuple[OperationEvidence, ...]
    detail: str


@dataclass(frozen=True)
class RollbackIntent:
    """Write-ahead rollback intent, durably journaled before dispatch.

    Carries the plan operation's server-owned inverse specification rather
    than a live 'before' observation: at broker tier no host inspection has
    happened yet, so the intent is the exact inverse action the plan declares
    (ORG-049), not an observed state snapshot.
    """

    operation_handle: str
    seq: int
    operation_id: str
    kind: str
    target: str
    owner: str
    inverse: Mapping[str, object]
    created_at: str

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "RollbackIntent":
        expected = {item.name for item in dataclasses.fields(cls)}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise HostControlError("invalid rollback intent fields")
        seq = raw["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise HostControlError("invalid rollback intent seq")
        inverse = raw["inverse"]
        if not isinstance(inverse, Mapping):
            raise HostControlError("invalid rollback intent inverse")
        return cls(
            operation_handle=_closed_hex(
                raw["operation_handle"],
                "rollback intent handle",
            ),
            seq=seq,
            operation_id=_closed_string(raw["operation_id"], "intent id"),
            kind=_closed_string(raw["kind"], "intent kind"),
            target=_closed_string(raw["target"], "intent target", maximum=1024),
            owner=_closed_string(raw["owner"], "intent owner"),
            inverse={str(key): value for key, value in inverse.items()},
            created_at=str(raw["created_at"]),
        )


@dataclass(frozen=True)
class OperationRecord:
    """Durable broker-side operation truth (ORG-052 sections 5 and 6)."""

    schema: str
    operation_handle: str
    kind: OperationKind
    state: OperationState
    owner: SubjectContext
    target_seat_id: str
    plan_handle: str
    plan_digest: str
    receipt_handle: str | None
    receipt_digest: str | None
    operation_families: tuple[str, ...]
    approval_handle: str
    authority_record_id: str | None
    idempotency_key: str
    request_digest: str
    created_at: str
    updated_at: str
    digest: str

    def digest_payload(self) -> Mapping[str, object]:
        value = dict(self.to_dict())
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: object) -> "OperationRecord":
        candidate = cls(digest="", **kwargs)  # type: ignore[arg-type]
        digest = sha256_json(candidate.digest_payload())
        record = dataclasses.replace(candidate, digest=digest)
        record._validate_shape()
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "OperationRecord":
        expected = {item.name for item in dataclasses.fields(cls)}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise HostControlError("invalid operation record fields")
        owner_raw = raw["owner"]
        if not isinstance(owner_raw, Mapping):
            raise HostControlError("invalid operation owner")
        receipt_handle = raw["receipt_handle"]
        receipt_digest = raw["receipt_digest"]
        authority = raw["authority_record_id"]
        try:
            candidate = cls(
                schema=str(raw["schema"]),
                operation_handle=str(raw["operation_handle"]),
                kind=str(raw["kind"]),  # type: ignore[arg-type]
                state=str(raw["state"]),  # type: ignore[arg-type]
                owner=SubjectContext(**owner_raw),  # type: ignore[arg-type]
                target_seat_id=str(raw["target_seat_id"]),
                plan_handle=str(raw["plan_handle"]),
                plan_digest=str(raw["plan_digest"]),
                receipt_handle=(
                    None if receipt_handle is None else str(receipt_handle)
                ),
                receipt_digest=(
                    None if receipt_digest is None else str(receipt_digest)
                ),
                operation_families=tuple(
                    raw["operation_families"]  # type: ignore[arg-type]
                ),
                approval_handle=str(raw["approval_handle"]),
                authority_record_id=(
                    None if authority is None else str(authority)
                ),
                idempotency_key=str(raw["idempotency_key"]),
                request_digest=str(raw["request_digest"]),
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostControlError("invalid operation record") from exc
        candidate._validate_shape()
        return candidate

    def _validate_shape(self) -> None:
        if self.schema != OPERATION_SCHEMA:
            raise HostControlError("invalid operation schema")
        _closed_hex(self.operation_handle, "operation handle")
        if self.kind not in OPERATION_KINDS:
            raise HostControlError("invalid operation kind")
        if self.state not in OPERATION_STATES:
            raise HostControlError("invalid operation state")
        _closed_hex(self.plan_handle, "operation plan handle")
        _closed_hex(self.plan_digest, "operation plan digest")
        if self.kind == "rollback":
            _closed_hex(self.receipt_handle, "operation receipt handle")
            _closed_hex(self.receipt_digest, "operation receipt digest")
        elif self.receipt_handle is not None or self.receipt_digest is not None:
            raise HostControlError("receipt binding is rollback-only")
        if not self.operation_families:
            raise HostControlError("empty operation family set")
        for family in self.operation_families:
            _closed_string(family, "operation family")
        _closed_string(self.target_seat_id, "operation target")
        _closed_string(self.approval_handle, "approval handle")
        _closed_string(self.idempotency_key, "operation idempotency key")
        _closed_hex(self.request_digest, "operation request digest")
        if self.state in ("prepared", "refused"):
            if self.authority_record_id is not None:
                raise HostControlError(
                    "authority record forbidden before authorization"
                )
        else:
            _closed_string(self.authority_record_id, "authority record id")
        for stamp in (self.created_at, self.updated_at):
            if not isinstance(stamp, str) or not stamp:
                raise HostControlError("invalid operation timestamp")

    def validate_digest(self) -> bool:
        try:
            self._validate_shape()
        except HostControlError:
            return False
        return sha256_json(self.digest_payload()) == self.digest


@dataclass(frozen=True)
class OperationReceipt:
    """Broker-owned receipt envelope (staged before dispatch, terminal after).

    Wraps the reusable installer MutationRecord/OperationEvidence shapes and
    adds the ORG-052 section 6 bindings the installer receipt lacks:
    operation correlation, manifest/framework revision, and effect digest.
    """

    schema: str
    receipt_handle: str
    operation_handle: str
    phase: ReceiptPhase
    plan_handle: str
    plan_digest: str
    effect_digest: str
    subject: SubjectContext
    target_seat_id: str
    manifest_revision: str
    framework_revision: str
    mutations: tuple[MutationRecord, ...]
    evidence: tuple[OperationEvidence, ...]
    created_at: str
    digest: str

    def digest_payload(self) -> Mapping[str, object]:
        value = dict(self.to_dict())
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: object) -> "OperationReceipt":
        candidate = cls(digest="", **kwargs)  # type: ignore[arg-type]
        digest = sha256_json(candidate.digest_payload())
        receipt = dataclasses.replace(candidate, digest=digest)
        receipt._validate_shape()
        return receipt

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "OperationReceipt":
        expected = {item.name for item in dataclasses.fields(cls)}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise HostControlError("invalid operation receipt fields")
        subject_raw = raw["subject"]
        mutations_raw = raw["mutations"]
        evidence_raw = raw["evidence"]
        if (
            not isinstance(subject_raw, Mapping)
            or not isinstance(mutations_raw, list)
            or not isinstance(evidence_raw, list)
        ):
            raise HostControlError("invalid operation receipt nesting")
        try:
            candidate = cls(
                schema=str(raw["schema"]),
                receipt_handle=str(raw["receipt_handle"]),
                operation_handle=str(raw["operation_handle"]),
                phase=str(raw["phase"]),  # type: ignore[arg-type]
                plan_handle=str(raw["plan_handle"]),
                plan_digest=str(raw["plan_digest"]),
                effect_digest=str(raw["effect_digest"]),
                subject=SubjectContext(**subject_raw),  # type: ignore[arg-type]
                target_seat_id=str(raw["target_seat_id"]),
                manifest_revision=str(raw["manifest_revision"]),
                framework_revision=str(raw["framework_revision"]),
                mutations=tuple(
                    _mutation_from_dict(item) for item in mutations_raw
                ),
                evidence=tuple(
                    _evidence_from_dict(item) for item in evidence_raw
                ),
                created_at=str(raw["created_at"]),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostControlError("invalid operation receipt") from exc
        candidate._validate_shape()
        return candidate

    def _validate_shape(self) -> None:
        if self.schema != OPERATION_RECEIPT_SCHEMA:
            raise HostControlError("invalid operation receipt schema")
        _closed_hex(self.receipt_handle, "receipt handle")
        _closed_hex(self.operation_handle, "receipt operation handle")
        if self.phase not in ("staged", "complete", "failed"):
            raise HostControlError("invalid receipt phase")
        _closed_hex(self.plan_handle, "receipt plan handle")
        _closed_hex(self.plan_digest, "receipt plan digest")
        _closed_hex(self.effect_digest, "receipt effect digest")
        _closed_string(self.target_seat_id, "receipt target")
        _closed_string(self.manifest_revision, "receipt manifest revision")
        _closed_string(self.framework_revision, "receipt framework revision")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise HostControlError("invalid receipt timestamp")

    def validate_digest(self) -> bool:
        try:
            self._validate_shape()
        except HostControlError:
            return False
        return sha256_json(self.digest_payload()) == self.digest


def resolve_error_name(
    exc: BaseException,
    name_by_type: Mapping[type, str],
    fallback: str,
) -> str:
    """Nearest-ancestor resolution of an exception to a closed wire name.

    Walks the exception type's MRO so any subclass — including ones
    minted after a service's mapping was written — resolves via its
    closest mapped ancestor, and anything outside the mapped hierarchy
    resolves to the bounded ``fallback``. Shared by the broker's and the
    approval service's wire modules (T-2913 OQ2 disposition): a
    duplicated resolver that drifts would let one service's mapping
    silently stop being TOTAL over the live exception hierarchy.
    """

    for klass in type(exc).__mro__:
        name = name_by_type.get(klass)
        if name is not None:
            return name
    return fallback
