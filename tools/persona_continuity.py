#!/usr/bin/env python3
"""Portable, reference-only persona continuity substrate (T-3176).

This module deliberately stops at local resolution, durable checkpoints, and
read-only evidence.  Runtime selection and receiver transfer belong to T-3177.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

import yaml


BUNDLE_SCHEMA_ID = "pantheon-persona-continuity-bundle/v1"
VIEW_SCHEMA_ID = "pantheon-persona-continuity-view/v1"
CHECKPOINT_SCHEMA_ID = "pantheon-persona-continuity-checkpoint/v1"
VERIFICATION_SCHEMA_ID = "pantheon-persona-continuity-verification/v1"
SWITCH_SCHEMA_ID = "pantheon-persona-continuity-switch/v1"
SOURCE_POLICY_SCHEMA_ID = "pantheon-source-policy-catalog/v1"
LEGACY_EVIDENCE_SCHEMA_ID = "pantheon-legacy-session-evidence/v1"
SNAPSHOT_INVENTORY_SCHEMA_ID = "pantheon-continuity-snapshot-inventory/v1"

MAX_BUNDLE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
MAX_POLICY_BYTES = 256 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_CLI_OUTPUT_BYTES = 256 * 1024
MAX_INVENTORY_FILES = 512
MAX_REFS = 64
MAX_ITEMS = 128
MAX_STRING = 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?$")


class ContinuityError(ValueError):
    def __init__(self, code: str, detail: str, *, status: str = "refused") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.status = status
        self.detail = detail


class ContinuityRefKind(str, Enum):
    ROLE = "role"
    IDENTITY = "identity"
    GOAL = "goal"
    CAPABILITY = "capability"
    AUTHORITY = "authority"
    MEMORY = "memory"
    COMMITMENT = "commitment"
    RUNTIME = "runtime"


class ContinuityPurpose(str, Enum):
    HYDRATE_RUNTIME = "hydrate-runtime"
    CHECKPOINT_BOUNDARY = "checkpoint-boundary"
    VERIFY_CANDIDATE = "verify-candidate"
    HANDOFF_SUCCESSOR = "handoff-successor"
    OPERATOR_AUDIT = "operator-audit"
    ROLLBACK_VERIFY = "rollback-verify"


class SourceClassification(str, Enum):
    CANONICAL = "canonical"
    DERIVED = "derived"
    RUNTIME_EVIDENCE = "runtime-evidence"
    NATIVE_MEMORY = "native-memory"


class PrivacyClass(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    PRIVATE = "private"


VERIFICATION_ASSERTION_IDS = (
    "candidate-persona-organism",
    "role-authority-boundary",
    "commitments-next-action",
    "authority-unavailable-capabilities",
    "last-checkpoint-digest",
    "bundle-source-revisions",
    "candidate-communication-correlation",
    "candidate-actions-disabled",
    "old-embodiment-sole-authority",
    "behavioral-variance-separated",
)


@dataclass(frozen=True)
class ContinuityRef:
    kind: ContinuityRefKind
    uri: str
    expected_revision: str | None
    expected_sha256: str | None
    authority_class: str
    required: bool
    purpose_limits: tuple[ContinuityPurpose, ...]


@dataclass(frozen=True)
class CommitmentQuery:
    kind: Literal["commitment"]
    persona_id: str
    states: tuple[str, ...]


@dataclass(frozen=True)
class BundleProvenance:
    authored_by: str
    adopted_by: str
    source_revision: str
    migrations: tuple[str, ...]
    persona_primitive_ref: ContinuityRef


@dataclass(frozen=True)
class ContinuityBundleManifest:
    schema: str
    persona_id: str
    organism_id: str
    role_contract_ref: ContinuityRef
    identity_canon_ref: ContinuityRef
    values_and_goals_refs: tuple[ContinuityRef, ...]
    capability_profile_ref: ContinuityRef
    authority_profile_ref: ContinuityRef
    memory_root_refs: tuple[ContinuityRef, ...]
    commitment_query: CommitmentQuery
    runtime_binding_ref: ContinuityRef
    provenance: BundleProvenance
    descriptor_sha256: str

    def refs(self) -> tuple[ContinuityRef, ...]:
        return (
            self.role_contract_ref,
            self.identity_canon_ref,
            *self.values_and_goals_refs,
            self.capability_profile_ref,
            self.authority_profile_ref,
            *self.memory_root_refs,
            self.runtime_binding_ref,
            self.provenance.persona_primitive_ref,
        )


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    authority_refs: tuple[str, ...]
    purpose_authorizations: tuple[ContinuityPurpose, ...]


@dataclass(frozen=True)
class SourcePolicyEntry:
    uri: str
    source_revision: str
    authority_class: str
    privacy: PrivacyClass
    redacted: bool
    allowed_purposes: tuple[ContinuityPurpose, ...]
    expected_uid: int
    max_mode: int


@dataclass(frozen=True)
class SourcePolicyActor:
    actor_id: str
    authority_refs: tuple[str, ...]
    allowed_purposes: tuple[ContinuityPurpose, ...]


@dataclass(frozen=True)
class SourcePolicyCatalog:
    source_locator: str
    catalog_revision: str
    catalog_sha256: str
    entries: tuple[SourcePolicyEntry, ...]
    actors: tuple[SourcePolicyActor, ...]

    def for_uri(self, uri: str) -> SourcePolicyEntry:
        matches = [entry for entry in self.entries if entry.uri == uri]
        if not matches:
            raise ContinuityError("source-policy-missing", uri, status="unavailable")
        if len(matches) != 1:
            raise ContinuityError("source-policy-ambiguous", uri)
        return matches[0]

    def actor_context(self, actor_id: str, purpose: ContinuityPurpose) -> ActorContext:
        matches = [actor for actor in self.actors if actor.actor_id == actor_id]
        if not matches:
            raise ContinuityError("actor-policy-missing", actor_id)
        if len(matches) != 1:
            raise ContinuityError("actor-policy-ambiguous", actor_id)
        actor = matches[0]
        if purpose not in actor.allowed_purposes:
            raise ContinuityError("actor-purpose-refused", f"{actor_id}:{purpose.value}")
        return ActorContext(actor.actor_id, actor.authority_refs, actor.allowed_purposes)


@dataclass(frozen=True)
class ResolvedRefEvidence:
    kind: str
    uri: str
    source_revision: str
    source_sha256: str
    authority_class: str
    privacy: str
    redacted: bool
    size_bytes: int


@dataclass(frozen=True)
class OmissionEvidence:
    kind: str
    uri: str
    reason: str


@dataclass(frozen=True)
class ResolvedContinuityView:
    schema: str
    persona_id: str
    organism_id: str
    purpose: str
    actor_digest: str
    descriptor_sha256: str
    source_policy_locator: str
    source_policy_revision: str
    source_policy_sha256: str
    source_evidence: tuple[ResolvedRefEvidence, ...]
    omissions: tuple[OmissionEvidence, ...]
    behavioral_variance: tuple[str, ...]
    resolved_at: str
    view_sha256: str


@dataclass(frozen=True)
class RuntimeObservation:
    runtime_id: str
    harness_id: str
    account_ref: str
    responsive: bool
    self_identification: str | None
    role_identification: str | None
    authority_limits: tuple[str, ...]
    current_work_refs: tuple[str, ...]
    receiver_evidence_sha256: str | None
    external_actions_disabled: bool
    observed_at: str


@dataclass(frozen=True)
class CheckpointContent:
    active_ticket_refs: tuple[str, ...]
    promise_refs: tuple[str, ...]
    wait_refs: tuple[str, ...]
    escalation_refs: tuple[str, ...]
    expected_next_action: str
    decision_refs: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    memory_candidate_refs: tuple[str, ...]


@dataclass(frozen=True)
class CheckpointBoundary:
    boundary_id: str
    author: str
    correlation_id: str
    created_at: str
    supersedes_receipt_path: str | None = None
    supersedes_payload_sha256: str | None = None


@dataclass(frozen=True)
class CheckpointEnvelope:
    schema: str
    checkpoint_id: str
    persona_id: str
    organism_id: str
    view_sha256: str
    purpose: str
    actor_digest: str
    descriptor_sha256: str
    source_policy_locator: str
    source_policy_revision: str
    source_policy_sha256: str
    source_refs: tuple[ResolvedRefEvidence, ...]
    content: CheckpointContent
    runtime_observation: RuntimeObservation
    boundary: CheckpointBoundary
    producer_revision: str
    prior_checkpoint_sha256: str | None
    payload_sha256: str


@dataclass(frozen=True)
class CheckpointReceipt:
    checkpoint_id: str
    persona_id: str
    view_sha256: str
    payload_sha256: str
    path: str
    created_at: str
    receipt_sha256: str


@dataclass(frozen=True)
class VerificationAssertion:
    assertion_id: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class ContinuityVerificationReport:
    schema: str
    persona_id: str
    candidate_runtime: str
    checkpoint_sha256: str
    assertions: tuple[VerificationAssertion, ...]
    external_actions_disabled: bool
    behavioral_variance: tuple[str, ...]
    verified_at: str
    report_sha256: str


@dataclass(frozen=True)
class VerificationResult:
    status: Literal["ready", "degraded", "unavailable", "refused"]
    code: str
    report: ContinuityVerificationReport | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class CandidateSourceRevision:
    uri: str
    source_revision: str
    source_sha256: str


@dataclass(frozen=True)
class CandidateVerificationEvidence:
    runtime_id: str
    persona_id: str
    organism_id: str
    role_id: str
    role_authority_refs: tuple[str, ...]
    current_commitment_refs: tuple[str, ...]
    expected_next_action: str
    authority_limits: tuple[str, ...]
    unavailable_capabilities: tuple[str, ...]
    capability_evidence_complete: bool
    checkpoint_sha256: str
    descriptor_sha256: str
    source_revisions: tuple[CandidateSourceRevision, ...]
    communication_correlation_id: str
    communication_evidence_sha256: str
    communication_ok: bool
    external_actions_disabled: bool
    old_embodiment_sole_authority: bool
    behavioral_variance: tuple[str, ...]
    variance_evidence_sha256: str | None
    observed_at: str


@dataclass(frozen=True)
class LegacySessionEvidence:
    schema: str
    persona_id: str
    classification: SourceClassification
    cache_only: bool
    can_satisfy_required_ref: bool
    source_sha256: str
    source_locator: str
    session_id_sha256: str | None


@dataclass(frozen=True)
class LegacyMigrationReceipt:
    status: Literal["created", "idempotent"]
    output_path: str
    source_sha256: str
    output_sha256: str


@dataclass(frozen=True)
class SnapshotInventoryEntry:
    rel_path: str
    schema: str
    sha256: str
    size_bytes: int
    mode: str
    classification: SourceClassification


@dataclass(frozen=True)
class SnapshotExportInventory:
    schema: str
    root_sha256: str
    entries: tuple[SnapshotInventoryEntry, ...]


@dataclass(frozen=True)
class HarnessSwitchReceipt:
    schema: str
    receipt_id: str
    persona_id: str
    approved_plan_sha256: str
    actor_id: str
    reviewed_source_revision: str
    old_runtime_observation: RuntimeObservation
    candidate_runtime_observation: RuntimeObservation
    checkpoint_sha256: str
    verification_sha256: str
    compatibility_result: str
    old_receiver_isolation_sha256: str
    candidate_receiver_isolation_sha256: str
    outcome: Literal["committed", "rolled-back"]
    manifest_revision: str
    controller_revision: str
    prior_receipt_sha256: str | None
    receipt_sha256: str


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _without(value: Mapping[str, object], *keys: str) -> dict[str, object]:
    return {key: item for key, item in value.items() if key not in keys}


def _expect_keys(value: object, required: set[str], *, optional: set[str] = frozenset(), where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuityError("invalid-object", f"{where} must be an object")
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise ContinuityError("missing-field", f"{where}: {sorted(missing)}")
    if unknown:
        raise ContinuityError("unknown-field", f"{where}: {sorted(unknown)}")
    return value


def _string(value: object, where: str, *, stable_id: bool = False, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_STRING or (not allow_empty and not value):
        raise ContinuityError("invalid-string", where)
    if stable_id and not ID_RE.fullmatch(value):
        raise ContinuityError("invalid-id", f"{where}: {value!r}")
    return value


def _string_tuple(value: object, where: str, *, max_items: int = MAX_ITEMS) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ContinuityError("invalid-list", where)
    result = tuple(_string(item, f"{where}[]") for item in value)
    if len(set(result)) != len(result):
        raise ContinuityError("duplicate-item", where)
    return result


def _reference_tuple(value: object, where: str, *, max_items: int = MAX_ITEMS) -> tuple[str, ...]:
    result = _string_tuple(value, where, max_items=max_items)
    for item in result:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", item):
            raise ContinuityError("invalid-reference", f"{where}: {item!r}")
    return result


def _reference_value(value: object, where: str) -> str:
    return _reference_tuple([value], where, max_items=1)[0]


def _sha(value: object, where: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContinuityError("invalid-sha256", where)
    return value


def _timestamp(value: object, where: str) -> str:
    text = _string(value, where)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContinuityError("invalid-timestamp", where) from exc
    if parsed.tzinfo is None:
        raise ContinuityError("invalid-timestamp", f"{where} lacks timezone")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContinuityError("duplicate-field", key)
        result[key] = value
    return result


class _StrictYamlLoader(yaml.SafeLoader):
    def compose_node(self, parent: object, index: object) -> object:
        if self.check_event(yaml.AliasEvent):
            raise ContinuityError("yaml-alias-forbidden", "aliases are not allowed")
        return super().compose_node(parent, index)


def _yaml_mapping(loader: _StrictYamlLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ContinuityError("invalid-field-name", repr(key))
        if key in result:
            raise ContinuityError("duplicate-field", key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _read_regular_file(path: Path, *, max_bytes: int, expected_uid: int | None = None, exact_mode: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ContinuityError("source-unavailable", f"{path}: {exc}", status="unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ContinuityError("not-regular", str(path))
        if expected_uid is not None and info.st_uid != expected_uid:
            raise ContinuityError("owner-mismatch", str(path))
        mode = stat.S_IMODE(info.st_mode)
        if exact_mode is not None and mode != exact_mode:
            raise ContinuityError("mode-mismatch", f"{path}: {mode:04o}")
        if mode & 0o022:
            raise ContinuityError("unsafe-mode", f"{path}: {mode:04o}")
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise ContinuityError("source-size", f"{path}: {info.st_size}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ContinuityError("source-size", f"{path}: exceeds {max_bytes}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_directory_no_follow(
    path: Path,
    *,
    expected_uid: int | None = None,
    create: bool = False,
    exact_mode: int | None = None,
) -> int:
    """Open an absolute directory path without following any component."""
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(absolute.anchor or "/", flags)
    try:
        components = absolute.parts[1:]
        for index, part in enumerate(components):
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise ContinuityError("root-unavailable", str(absolute), status="unavailable")
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    code = "symlink-root-forbidden" if index == len(components) - 1 else "symlink-ancestor-forbidden"
                    raise ContinuityError(code, f"{absolute}: {exc}") from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    code = "symlink-root-forbidden" if index == len(components) - 1 else "symlink-ancestor-forbidden"
                else:
                    code = "root-unavailable"
                raise ContinuityError(code, f"{absolute}: {exc}", status="unavailable" if code == "root-unavailable" else "refused") from exc
            os.close(current_fd)
            current_fd = next_fd
        info = os.fstat(current_fd)
        uid = os.getuid() if expected_uid is None else expected_uid
        if not stat.S_ISDIR(info.st_mode):
            raise ContinuityError("root-not-directory", str(absolute))
        if info.st_uid != uid:
            raise ContinuityError("owner-mismatch", str(absolute))
        mode = stat.S_IMODE(info.st_mode)
        if exact_mode is not None and mode != exact_mode:
            raise ContinuityError("root-mode-mismatch", f"{absolute}: {mode:04o}")
        if mode & 0o022:
            raise ContinuityError("unsafe-mode", f"{absolute}: {mode:04o}")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validated_root(path: Path, *, expected_uid: int | None = None) -> Path:
    absolute = path.absolute()
    fd = _open_directory_no_follow(absolute, expected_uid=expected_uid)
    os.close(fd)
    return absolute


def _read_under_root(
    root: Path,
    rel: PurePosixPath,
    *,
    max_bytes: int,
    expected_uid: int | None = None,
    exact_mode: int | None = None,
) -> bytes:
    """Open every path component beneath *root* without following symlinks."""
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ContinuityError("path-traversal", rel.as_posix())
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    leaf_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = _open_directory_no_follow(root, expected_uid=expected_uid)
    try:
        uid = os.getuid() if expected_uid is None else expected_uid
        for part in rel.parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                code = "symlink-forbidden" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "source-unavailable"
                raise ContinuityError(code, f"{root / rel}: {exc}", status="unavailable" if code == "source-unavailable" else "refused") from exc
            os.close(current_fd)
            current_fd = next_fd
            directory_info = os.fstat(current_fd)
            if directory_info.st_uid != uid:
                raise ContinuityError("owner-mismatch", str(root / rel))
            if stat.S_IMODE(directory_info.st_mode) & 0o022:
                raise ContinuityError("unsafe-mode", f"{root / rel}: directory component")
        try:
            leaf_fd = os.open(rel.parts[-1], leaf_flags, dir_fd=current_fd)
        except OSError as exc:
            code = "symlink-forbidden" if exc.errno == errno.ELOOP else "source-unavailable"
            raise ContinuityError(code, f"{root / rel}: {exc}", status="unavailable" if code == "source-unavailable" else "refused") from exc
        try:
            info = os.fstat(leaf_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ContinuityError("not-regular", str(root / rel))
            if expected_uid is not None and info.st_uid != expected_uid:
                raise ContinuityError("owner-mismatch", str(root / rel))
            mode = stat.S_IMODE(info.st_mode)
            if exact_mode is not None and mode != exact_mode:
                raise ContinuityError("mode-mismatch", f"{root / rel}: {mode:04o}")
            if mode & 0o022:
                raise ContinuityError("unsafe-mode", f"{root / rel}: {mode:04o}")
            if info.st_size <= 0 or info.st_size > max_bytes:
                raise ContinuityError("source-size", f"{root / rel}: {info.st_size}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(leaf_fd, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ContinuityError("source-size", f"{root / rel}: exceeds {max_bytes}")
            return b"".join(chunks)
        finally:
            os.close(leaf_fd)
    finally:
        os.close(current_fd)


def _parse_json_bytes(raw: bytes, *, where: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContinuityError("invalid-json", f"{where}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContinuityError("invalid-object", where)
    return value


def _parse_document(raw: bytes, *, where: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContinuityError("invalid-utf8", where) from exc
    try:
        if text.lstrip().startswith(("{", "[")):
            return _parse_json_bytes(raw, where=where)
        value = yaml.load(text, Loader=_StrictYamlLoader)
    except yaml.YAMLError as exc:
        raise ContinuityError("invalid-yaml", f"{where}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContinuityError("invalid-object", where)
    return value


def _canonical_uri(uri: object) -> tuple[str, PurePosixPath]:
    text = _string(uri, "ref.uri")
    if ":" not in text:
        raise ContinuityError("invalid-uri", text)
    scheme, raw_rel = text.split(":", 1)
    if scheme not in {"repo", "state"} or not raw_rel or "\\" in raw_rel:
        raise ContinuityError("invalid-uri", text)
    rel = PurePosixPath(raw_rel)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ContinuityError("path-traversal", text)
    canonical = f"{scheme}:{rel.as_posix()}"
    if canonical != text:
        raise ContinuityError("noncanonical-uri", text)
    return scheme, rel


def _enum(cls: type[Enum], value: object, where: str) -> Any:
    try:
        return cls(value)
    except (ValueError, TypeError) as exc:
        raise ContinuityError("invalid-enum", f"{where}: {value!r}") from exc


def _ref_from_obj(value: object, where: str) -> ContinuityRef:
    obj = _expect_keys(
        value,
        {"kind", "uri", "expected_revision", "expected_sha256", "authority_class", "required", "purpose_limits"},
        where=where,
    )
    if type(obj["required"]) is not bool:
        raise ContinuityError("invalid-boolean", f"{where}.required")
    purposes = tuple(_enum(ContinuityPurpose, item, f"{where}.purpose_limits") for item in _string_tuple(obj["purpose_limits"], f"{where}.purpose_limits"))
    if not purposes:
        raise ContinuityError("invalid-list", f"{where}.purpose_limits empty")
    uri = _canonical_uri(obj["uri"])[0] + ":" + _canonical_uri(obj["uri"])[1].as_posix()
    expected_revision = None if obj["expected_revision"] is None else _string(obj["expected_revision"], f"{where}.expected_revision")
    expected_sha256 = _sha(obj["expected_sha256"], f"{where}.expected_sha256", optional=True)
    if obj["required"] and (expected_revision is None or expected_sha256 is None):
        raise ContinuityError("required-ref-unpinned", where)
    return ContinuityRef(
        kind=_enum(ContinuityRefKind, obj["kind"], f"{where}.kind"),
        uri=uri,
        expected_revision=expected_revision,
        expected_sha256=expected_sha256,
        authority_class=_string(obj["authority_class"], f"{where}.authority_class", stable_id=True),
        required=obj["required"],
        purpose_limits=purposes,
    )


def _reject_inline_authority(value: object, *, path: str = "bundle") -> None:
    forbidden = {"secret", "token", "password", "credential", "private_key", "payload", "prompt", "memory_text", "content"}
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in forbidden or any(part in lowered for part in ("secret", "password", "credential", "private_key")):
                raise ContinuityError("inline-authority-forbidden", f"{path}.{key}")
            _reject_inline_authority(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_inline_authority(item, path=f"{path}[{index}]")


def load_continuity_bundle(path: Path, *, descriptor_root: Path, max_bytes: int = MAX_BUNDLE_BYTES) -> ContinuityBundleManifest:
    root = _validated_root(descriptor_root)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise ContinuityError("path-traversal", str(path)) from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ContinuityError("path-traversal", str(path))
    raw = _read_under_root(root, PurePosixPath(*relative.parts), max_bytes=max_bytes, expected_uid=os.getuid())
    value = _parse_document(raw, where=str(path))
    _reject_inline_authority(value)
    required = {
        "schema", "persona_id", "organism_id", "role_contract_ref", "identity_canon_ref",
        "values_and_goals_refs", "capability_profile_ref", "authority_profile_ref", "memory_root_refs",
        "commitment_query", "runtime_binding_ref", "provenance",
    }
    obj = _expect_keys(value, required, where="bundle")
    if obj["schema"] != BUNDLE_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "bundle")
    persona_id = _string(obj["persona_id"], "persona_id", stable_id=True)
    goals_raw = obj["values_and_goals_refs"]
    memories_raw = obj["memory_root_refs"]
    if not isinstance(goals_raw, list) or len(goals_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "values_and_goals_refs")
    if not isinstance(memories_raw, list) or len(memories_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "memory_root_refs")
    commitment = _expect_keys(obj["commitment_query"], {"kind", "persona_id", "states"}, where="commitment_query")
    if commitment["kind"] != "commitment" or commitment["persona_id"] != persona_id:
        raise ContinuityError("commitment-query-mismatch", persona_id)
    provenance = _expect_keys(obj["provenance"], {"authored_by", "adopted_by", "source_revision", "migrations", "persona_primitive_ref"}, where="provenance")
    manifest = ContinuityBundleManifest(
        schema=BUNDLE_SCHEMA_ID,
        persona_id=persona_id,
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        role_contract_ref=_ref_from_obj(obj["role_contract_ref"], "role_contract_ref"),
        identity_canon_ref=_ref_from_obj(obj["identity_canon_ref"], "identity_canon_ref"),
        values_and_goals_refs=tuple(_ref_from_obj(item, "values_and_goals_refs[]") for item in goals_raw),
        capability_profile_ref=_ref_from_obj(obj["capability_profile_ref"], "capability_profile_ref"),
        authority_profile_ref=_ref_from_obj(obj["authority_profile_ref"], "authority_profile_ref"),
        memory_root_refs=tuple(_ref_from_obj(item, "memory_root_refs[]") for item in memories_raw),
        commitment_query=CommitmentQuery("commitment", persona_id, _string_tuple(commitment["states"], "commitment_query.states")),
        runtime_binding_ref=_ref_from_obj(obj["runtime_binding_ref"], "runtime_binding_ref"),
        provenance=BundleProvenance(
            authored_by=_string(provenance["authored_by"], "provenance.authored_by"),
            adopted_by=_string(provenance["adopted_by"], "provenance.adopted_by"),
            source_revision=_string(provenance["source_revision"], "provenance.source_revision"),
            migrations=_string_tuple(provenance["migrations"], "provenance.migrations"),
            persona_primitive_ref=_ref_from_obj(provenance["persona_primitive_ref"], "provenance.persona_primitive_ref"),
        ),
        descriptor_sha256=_sha256(raw),
    )
    expected_kinds = {
        "role_contract_ref": (manifest.role_contract_ref, ContinuityRefKind.ROLE),
        "identity_canon_ref": (manifest.identity_canon_ref, ContinuityRefKind.IDENTITY),
        "capability_profile_ref": (manifest.capability_profile_ref, ContinuityRefKind.CAPABILITY),
        "authority_profile_ref": (manifest.authority_profile_ref, ContinuityRefKind.AUTHORITY),
        "runtime_binding_ref": (manifest.runtime_binding_ref, ContinuityRefKind.RUNTIME),
        "persona_primitive_ref": (manifest.provenance.persona_primitive_ref, ContinuityRefKind.IDENTITY),
    }
    for where, (ref, kind) in expected_kinds.items():
        if ref.kind is not kind:
            raise ContinuityError("logical-ref-kind", where)
    if any(ref.kind is not ContinuityRefKind.GOAL for ref in manifest.values_and_goals_refs):
        raise ContinuityError("logical-ref-kind", "values_and_goals_refs")
    if any(ref.kind is not ContinuityRefKind.MEMORY for ref in manifest.memory_root_refs):
        raise ContinuityError("logical-ref-kind", "memory_root_refs")
    uris = [ref.uri for ref in manifest.refs()]
    if len(set(uris)) != len(uris):
        raise ContinuityError("duplicate-logical-ref", "bundle refs")
    return manifest


def _normalize_manifest(value: ContinuityBundleManifest) -> ContinuityBundleManifest:
    plain = _plain(value)
    if not isinstance(plain, dict):
        raise ContinuityError("invalid-object", "bundle")
    descriptor_sha256 = _sha(plain.pop("descriptor_sha256", None), "descriptor_sha256") or ""
    _reject_inline_authority(plain)
    required = {
        "schema", "persona_id", "organism_id", "role_contract_ref", "identity_canon_ref",
        "values_and_goals_refs", "capability_profile_ref", "authority_profile_ref", "memory_root_refs",
        "commitment_query", "runtime_binding_ref", "provenance",
    }
    obj = _expect_keys(plain, required, where="bundle")
    if obj["schema"] != BUNDLE_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "bundle")
    persona_id = _string(obj["persona_id"], "persona_id", stable_id=True)
    goals_raw = obj["values_and_goals_refs"]
    memories_raw = obj["memory_root_refs"]
    if not isinstance(goals_raw, list) or len(goals_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "values_and_goals_refs")
    if not isinstance(memories_raw, list) or len(memories_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "memory_root_refs")
    commitment = _expect_keys(obj["commitment_query"], {"kind", "persona_id", "states"}, where="commitment_query")
    if commitment["kind"] != "commitment" or commitment["persona_id"] != persona_id:
        raise ContinuityError("commitment-query-mismatch", persona_id)
    provenance = _expect_keys(obj["provenance"], {"authored_by", "adopted_by", "source_revision", "migrations", "persona_primitive_ref"}, where="provenance")
    manifest = ContinuityBundleManifest(
        schema=BUNDLE_SCHEMA_ID,
        persona_id=persona_id,
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        role_contract_ref=_ref_from_obj(obj["role_contract_ref"], "role_contract_ref"),
        identity_canon_ref=_ref_from_obj(obj["identity_canon_ref"], "identity_canon_ref"),
        values_and_goals_refs=tuple(_ref_from_obj(item, "values_and_goals_refs[]") for item in goals_raw),
        capability_profile_ref=_ref_from_obj(obj["capability_profile_ref"], "capability_profile_ref"),
        authority_profile_ref=_ref_from_obj(obj["authority_profile_ref"], "authority_profile_ref"),
        memory_root_refs=tuple(_ref_from_obj(item, "memory_root_refs[]") for item in memories_raw),
        commitment_query=CommitmentQuery("commitment", persona_id, _string_tuple(commitment["states"], "commitment_query.states")),
        runtime_binding_ref=_ref_from_obj(obj["runtime_binding_ref"], "runtime_binding_ref"),
        provenance=BundleProvenance(
            authored_by=_string(provenance["authored_by"], "provenance.authored_by"),
            adopted_by=_string(provenance["adopted_by"], "provenance.adopted_by"),
            source_revision=_string(provenance["source_revision"], "provenance.source_revision"),
            migrations=_string_tuple(provenance["migrations"], "provenance.migrations"),
            persona_primitive_ref=_ref_from_obj(provenance["persona_primitive_ref"], "provenance.persona_primitive_ref"),
        ),
        descriptor_sha256=descriptor_sha256,
    )
    expected_kinds = {
        "role_contract_ref": (manifest.role_contract_ref, ContinuityRefKind.ROLE),
        "identity_canon_ref": (manifest.identity_canon_ref, ContinuityRefKind.IDENTITY),
        "capability_profile_ref": (manifest.capability_profile_ref, ContinuityRefKind.CAPABILITY),
        "authority_profile_ref": (manifest.authority_profile_ref, ContinuityRefKind.AUTHORITY),
        "runtime_binding_ref": (manifest.runtime_binding_ref, ContinuityRefKind.RUNTIME),
        "persona_primitive_ref": (manifest.provenance.persona_primitive_ref, ContinuityRefKind.IDENTITY),
    }
    for where, (ref, kind) in expected_kinds.items():
        if ref.kind is not kind:
            raise ContinuityError("logical-ref-kind", where)
    if any(ref.kind is not ContinuityRefKind.GOAL for ref in manifest.values_and_goals_refs):
        raise ContinuityError("logical-ref-kind", "values_and_goals_refs")
    if any(ref.kind is not ContinuityRefKind.MEMORY for ref in manifest.memory_root_refs):
        raise ContinuityError("logical-ref-kind", "memory_root_refs")
    uris = [ref.uri for ref in manifest.refs()]
    if len(set(uris)) != len(uris):
        raise ContinuityError("duplicate-logical-ref", "bundle refs")
    return manifest


def load_source_policy_catalog(path: Path, *, max_bytes: int = MAX_POLICY_BYTES) -> SourcePolicyCatalog:
    root = _validated_root(path.parent)
    raw = _read_under_root(root, PurePosixPath(path.name), max_bytes=max_bytes, expected_uid=os.getuid())
    obj = _expect_keys(_parse_json_bytes(raw, where=str(path)), {"schema", "source_locator", "catalog_revision", "entries", "actors"}, where="source-policy")
    if obj["schema"] != SOURCE_POLICY_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "source-policy")
    entries_raw = obj["entries"]
    if not isinstance(entries_raw, list) or not entries_raw or len(entries_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "source-policy.entries")
    entries: list[SourcePolicyEntry] = []
    for index, raw_entry in enumerate(entries_raw):
        where = f"source-policy.entries[{index}]"
        entry = _expect_keys(raw_entry, {"uri", "source_revision", "authority_class", "privacy", "redacted", "allowed_purposes", "expected_uid", "max_mode"}, where=where)
        if type(entry["redacted"]) is not bool or type(entry["expected_uid"]) is not int:
            raise ContinuityError("invalid-policy-field", where)
        mode_text = _string(entry["max_mode"], f"{where}.max_mode")
        if not re.fullmatch(r"0?[0-7]{3}", mode_text):
            raise ContinuityError("invalid-mode", mode_text)
        uri = _canonical_uri(entry["uri"])[0] + ":" + _canonical_uri(entry["uri"])[1].as_posix()
        entries.append(SourcePolicyEntry(
            uri=uri,
            source_revision=_string(entry["source_revision"], f"{where}.source_revision"),
            authority_class=_string(entry["authority_class"], f"{where}.authority_class", stable_id=True),
            privacy=_enum(PrivacyClass, entry["privacy"], f"{where}.privacy"),
            redacted=entry["redacted"],
            allowed_purposes=tuple(_enum(ContinuityPurpose, item, f"{where}.allowed_purposes") for item in _string_tuple(entry["allowed_purposes"], f"{where}.allowed_purposes")),
            expected_uid=entry["expected_uid"],
            max_mode=int(mode_text, 8),
        ))
    if len({entry.uri for entry in entries}) != len(entries):
        raise ContinuityError("source-policy-ambiguous", "duplicate URI")
    actors_raw = obj["actors"]
    if not isinstance(actors_raw, list) or not actors_raw or len(actors_raw) > MAX_ITEMS:
        raise ContinuityError("invalid-list", "source-policy.actors")
    actors: list[SourcePolicyActor] = []
    for index, raw_actor in enumerate(actors_raw):
        where = f"source-policy.actors[{index}]"
        actor = _expect_keys(raw_actor, {"actor_id", "authority_refs", "allowed_purposes"}, where=where)
        actors.append(SourcePolicyActor(
            actor_id=_string(actor["actor_id"], f"{where}.actor_id", stable_id=True),
            authority_refs=_reference_tuple(actor["authority_refs"], f"{where}.authority_refs"),
            allowed_purposes=tuple(_enum(ContinuityPurpose, item, f"{where}.allowed_purposes") for item in _string_tuple(actor["allowed_purposes"], f"{where}.allowed_purposes")),
        ))
    if len({actor.actor_id for actor in actors}) != len(actors):
        raise ContinuityError("actor-policy-ambiguous", "duplicate actor")
    return SourcePolicyCatalog(
        source_locator=_string(obj["source_locator"], "source-policy.source_locator"),
        catalog_revision=_string(obj["catalog_revision"], "source-policy.catalog_revision"),
        catalog_sha256=_sha256(raw),
        entries=tuple(entries),
        actors=tuple(actors),
    )


class ContinuitySourceStore:
    def __init__(self, *, repo_root: Path, state_root: Path, source_policy: SourcePolicyCatalog, max_source_bytes: int = MAX_SOURCE_BYTES) -> None:
        self.repo_root = _validated_root(repo_root)
        self.state_root = _validated_root(state_root)
        self.source_policy = source_policy
        self.max_source_bytes = max_source_bytes

    def _path_for_uri(self, uri: str) -> Path:
        scheme, rel = _canonical_uri(uri)
        root = self.repo_root if scheme == "repo" else self.state_root
        current = root
        for part in rel.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError as exc:
                raise ContinuityError("source-unavailable", uri, status="unavailable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ContinuityError("symlink-forbidden", uri)
        return current

    def read_ref(self, ref: ContinuityRef, *, actor: ActorContext, purpose: ContinuityPurpose) -> ResolvedRefEvidence:
        if purpose not in ref.purpose_limits or purpose not in actor.purpose_authorizations:
            raise ContinuityError("purpose-not-authorized", ref.uri)
        policy = self.source_policy.for_uri(ref.uri)
        if purpose not in policy.allowed_purposes:
            raise ContinuityError("source-purpose-not-authorized", ref.uri)
        if ref.authority_class != policy.authority_class:
            raise ContinuityError("authority-conflict", ref.uri)
        if f"authority:{policy.authority_class}" not in actor.authority_refs:
            raise ContinuityError("actor-authority-missing", ref.uri)
        if ref.expected_revision is not None and ref.expected_revision != policy.source_revision:
            raise ContinuityError("revision-drift", ref.uri)
        scheme, rel = _canonical_uri(ref.uri)
        root = self.repo_root if scheme == "repo" else self.state_root
        raw = _read_under_root(root, rel, max_bytes=self.max_source_bytes, expected_uid=policy.expected_uid)
        # The no-follow read already rejects group/other-writable bytes.  A
        # catalog may narrow that further (for example, require 0600).
        path = root.joinpath(*rel.parts)
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        if mode & ~policy.max_mode:
            raise ContinuityError("mode-exceeds-policy", ref.uri)
        digest = _sha256(raw)
        if ref.expected_sha256 is not None and digest != ref.expected_sha256:
            raise ContinuityError("digest-drift", ref.uri)
        return ResolvedRefEvidence(
            kind=ref.kind.value,
            uri=ref.uri,
            source_revision=policy.source_revision,
            source_sha256=digest,
            authority_class=policy.authority_class,
            privacy=policy.privacy.value,
            redacted=policy.redacted,
            size_bytes=len(raw),
        )


def _actor_digest(actor: ActorContext, purpose: ContinuityPurpose) -> str:
    return _sha256(_json_bytes({
        "actor_id": actor.actor_id,
        "authority_refs": sorted(actor.authority_refs),
        "purpose": purpose.value,
    }))


def _normalize_actor_context(value: ActorContext) -> ActorContext:
    obj = _expect_keys(_plain(value), {"actor_id", "authority_refs", "purpose_authorizations"}, where="actor-context")
    purposes = tuple(
        _enum(ContinuityPurpose, item, "actor-context.purpose_authorizations")
        for item in _string_tuple(obj["purpose_authorizations"], "actor-context.purpose_authorizations")
    )
    if not purposes:
        raise ContinuityError("invalid-list", "actor-context.purpose_authorizations")
    return ActorContext(
        actor_id=_string(obj["actor_id"], "actor-context.actor_id", stable_id=True),
        authority_refs=_reference_tuple(obj["authority_refs"], "actor-context.authority_refs"),
        purpose_authorizations=purposes,
    )


def _validate_actor_context(actor: ActorContext, policy_actor: ActorContext, purpose: ContinuityPurpose) -> ActorContext:
    actor = _normalize_actor_context(actor)
    policy_actor = _normalize_actor_context(policy_actor)
    if (
        actor.actor_id != policy_actor.actor_id
        or set(actor.authority_refs) != set(policy_actor.authority_refs)
        or purpose not in actor.purpose_authorizations
        or not set(actor.purpose_authorizations).issubset(set(policy_actor.purpose_authorizations))
    ):
        raise ContinuityError("actor-context-conflict", actor.actor_id)
    return actor


class ContinuityResolver:
    def __init__(self, source_store: ContinuitySourceStore | None, *, checkpoint_root: Path, source_policy: SourcePolicyCatalog | None = None) -> None:
        self.source_store = source_store
        self.checkpoint_root = checkpoint_root
        self.source_policy = source_store.source_policy if source_store is not None else source_policy

    def resolve(self, manifest: ContinuityBundleManifest, *, purpose: str, actor: ActorContext, as_of: datetime) -> ResolvedContinuityView:
        if self.source_store is None:
            raise ContinuityError("resolver-unconfigured", "source store")
        manifest = _normalize_manifest(manifest)
        purpose_value = _enum(ContinuityPurpose, purpose, "purpose")
        policy_actor = self.source_store.source_policy.actor_context(actor.actor_id, purpose_value)
        actor = _validate_actor_context(actor, policy_actor, purpose_value)
        evidence: list[ResolvedRefEvidence] = []
        omissions: list[OmissionEvidence] = []
        for ref in manifest.refs():
            try:
                evidence.append(self.source_store.read_ref(ref, actor=actor, purpose=purpose_value))
            except ContinuityError as exc:
                if ref.required or exc.code not in {"source-unavailable", "source-policy-missing"}:
                    raise
                omissions.append(OmissionEvidence(ref.kind.value, ref.uri, exc.code))
        if not any(item.kind == ContinuityRefKind.IDENTITY.value for item in evidence):
            raise ContinuityError("required-identity-unavailable", manifest.persona_id, status="unavailable")
        base = {
            "schema": VIEW_SCHEMA_ID,
            "persona_id": manifest.persona_id,
            "organism_id": manifest.organism_id,
            "purpose": purpose_value.value,
            "actor_digest": _actor_digest(actor, purpose_value),
            "descriptor_sha256": manifest.descriptor_sha256,
            "source_policy_locator": self.source_store.source_policy.source_locator,
            "source_policy_revision": self.source_store.source_policy.catalog_revision,
            "source_policy_sha256": self.source_store.source_policy.catalog_sha256,
            "source_evidence": _plain(tuple(evidence)),
            "omissions": _plain(tuple(omissions)),
            "behavioral_variance": [],
            "resolved_at": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return ResolvedContinuityView(
            schema=VIEW_SCHEMA_ID,
            persona_id=manifest.persona_id,
            organism_id=manifest.organism_id,
            purpose=purpose_value.value,
            actor_digest=str(base["actor_digest"]),
            descriptor_sha256=manifest.descriptor_sha256,
            source_policy_locator=self.source_store.source_policy.source_locator,
            source_policy_revision=self.source_store.source_policy.catalog_revision,
            source_policy_sha256=self.source_store.source_policy.catalog_sha256,
            source_evidence=tuple(evidence),
            omissions=tuple(omissions),
            behavioral_variance=(),
            resolved_at=str(base["resolved_at"]),
            view_sha256=_sha256(_json_bytes(base)),
        )

    def checkpoint(
        self,
        view: ResolvedContinuityView,
        *,
        boundary_id: str,
        runtime_observation: RuntimeObservation,
        content: CheckpointContent,
        boundary: CheckpointBoundary,
        producer_revision: str,
        actor: ActorContext,
        purpose: str,
    ) -> CheckpointReceipt:
        view = _resolved_view_from_obj(_plain(view))
        if boundary_id != boundary.boundary_id:
            raise ContinuityError("boundary-mismatch", boundary_id)
        purpose_value = _enum(ContinuityPurpose, purpose, "purpose")
        if self.source_policy is None:
            raise ContinuityError("source-policy-required", "checkpoint")
        policy_actor = self.source_policy.actor_context(actor.actor_id, purpose_value)
        actor = _validate_actor_context(actor, policy_actor, purpose_value)
        if boundary.author != actor.actor_id:
            raise ContinuityError("checkpoint-actor-mismatch", boundary.author)
        if (
            view.purpose != purpose_value.value
            or view.actor_digest != _actor_digest(actor, purpose_value)
            or view.source_policy_sha256 != self.source_policy.catalog_sha256
            or view.source_policy_locator != self.source_policy.source_locator
            or view.source_policy_revision != self.source_policy.catalog_revision
        ):
            raise ContinuityError("view-replay-refused", f"{actor.actor_id}:{purpose}")
        return write_checkpoint(
            view,
            content=content,
            boundary=boundary,
            runtime_observation=runtime_observation,
            checkpoint_root=self.checkpoint_root,
            producer_revision=producer_revision,
        )

    def verify_checkpoint(
        self,
        receipt: CheckpointReceipt,
        manifest: ContinuityBundleManifest,
        *,
        candidate_evidence: CandidateVerificationEvidence,
        actor: ActorContext,
        purpose: str,
    ) -> VerificationResult:
        try:
            if self.source_store is None or self.source_policy is None:
                raise ContinuityError("source-policy-required", "verify")
            receipt = _receipt_from_obj(_plain(receipt))
            candidate_evidence = _candidate_evidence_from_obj(_plain(candidate_evidence))
            manifest = _normalize_manifest(manifest)
            purpose_value = _enum(ContinuityPurpose, purpose, "purpose")
            policy_actor = self.source_policy.actor_context(actor.actor_id, purpose_value)
            actor = _validate_actor_context(actor, policy_actor, purpose_value)
            envelope = load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.checkpoint_root)
            if envelope.persona_id != manifest.persona_id or envelope.payload_sha256 != receipt.payload_sha256:
                raise ContinuityError("checkpoint-manifest-mismatch", receipt.path)
            if envelope.descriptor_sha256 != manifest.descriptor_sha256:
                raise ContinuityError("descriptor-drift", receipt.path)
            if (
                envelope.purpose != purpose_value.value
                or envelope.actor_digest != _actor_digest(actor, purpose_value)
                or envelope.source_policy_sha256 != self.source_policy.catalog_sha256
                or envelope.source_policy_locator != self.source_policy.source_locator
                or envelope.source_policy_revision != self.source_policy.catalog_revision
            ):
                raise ContinuityError("verification-context-drift", receipt.path)
            current_evidence = tuple(
                self.source_store.read_ref(ref, actor=actor, purpose=purpose_value)
                for ref in manifest.refs()
            )
            if _plain(current_evidence) != _plain(envelope.source_refs):
                raise ContinuityError("checkpoint-source-drift", receipt.path)
            raw_receipt = _without(_plain(receipt), "receipt_sha256")
            if _sha256(_json_bytes(raw_receipt)) != receipt.receipt_sha256:
                raise ContinuityError("receipt-digest-drift", receipt.path)
            report = build_verification_report(envelope=envelope, candidate_evidence=candidate_evidence)
            passed = all(item.passed for item in report.assertions)
            return VerificationResult("ready" if passed else "degraded", "verified" if passed else "assertion-failed", report, ())
        except ContinuityError as exc:
            return VerificationResult("refused", exc.code, None, (exc.detail,))


def _validate_runtime_observation(value: RuntimeObservation) -> None:
    _string(value.runtime_id, "runtime_id", stable_id=True)
    _string(value.harness_id, "harness_id", stable_id=True)
    _string(value.account_ref, "account_ref")
    _timestamp(value.observed_at, "observed_at")
    if type(value.responsive) is not bool or type(value.external_actions_disabled) is not bool:
        raise ContinuityError("invalid-runtime-observation", "booleans")
    if not value.responsive or not (value.self_identification or "").strip() or not (value.role_identification or "").strip() or not value.current_work_refs:
        raise ContinuityError("runtime-not-ready", value.runtime_id, status="unavailable")
    if not value.external_actions_disabled:
        raise ContinuityError("runtime-not-isolated", value.runtime_id)
    if value.receiver_evidence_sha256 is not None:
        _sha(value.receiver_evidence_sha256, "receiver_evidence_sha256")


def assess_hydration_readiness(view: ResolvedContinuityView, runtime_observation: RuntimeObservation) -> VerificationResult:
    issues: list[str] = []
    try:
        view = _resolved_view_from_obj(_plain(view))
        runtime_observation = _runtime_from_obj(_plain(runtime_observation))
        _validate_runtime_observation(runtime_observation)
    except ContinuityError as exc:
        issues.append(exc.code)
    kinds = {item.kind for item in view.source_evidence}
    for required in ("identity", "role", "authority"):
        if required not in kinds:
            issues.append(f"missing-{required}")
    authority_evidence = {
        f"authority:{item.authority_class}"
        for item in view.source_evidence
        if item.kind == "authority"
    }
    if authority_evidence and not authority_evidence.issubset(set(runtime_observation.authority_limits)):
        issues.append("authority-mismatch")
    return VerificationResult("ready" if not issues else "degraded", "ready" if not issues else "hydration-degraded", None, tuple(issues))


def _content_from_obj(value: object) -> CheckpointContent:
    keys = {"active_ticket_refs", "promise_refs", "wait_refs", "escalation_refs", "expected_next_action", "decision_refs", "unresolved_questions", "memory_candidate_refs"}
    obj = _expect_keys(value, keys, where="checkpoint.content")
    return CheckpointContent(
        active_ticket_refs=_reference_tuple(obj["active_ticket_refs"], "active_ticket_refs"),
        promise_refs=_reference_tuple(obj["promise_refs"], "promise_refs"),
        wait_refs=_reference_tuple(obj["wait_refs"], "wait_refs"),
        escalation_refs=_reference_tuple(obj["escalation_refs"], "escalation_refs"),
        expected_next_action=_reference_value(obj["expected_next_action"], "expected_next_action"),
        decision_refs=_reference_tuple(obj["decision_refs"], "decision_refs"),
        unresolved_questions=_reference_tuple(obj["unresolved_questions"], "unresolved_questions"),
        memory_candidate_refs=_reference_tuple(obj["memory_candidate_refs"], "memory_candidate_refs"),
    )


def _boundary_from_obj(value: object) -> CheckpointBoundary:
    obj = _expect_keys(value, {"boundary_id", "author", "correlation_id", "created_at", "supersedes_receipt_path", "supersedes_payload_sha256"}, where="checkpoint.boundary")
    path = obj["supersedes_receipt_path"]
    digest = obj["supersedes_payload_sha256"]
    if (path is None) != (digest is None):
        raise ContinuityError("supersession-incomplete", "boundary")
    return CheckpointBoundary(
        boundary_id=_string(obj["boundary_id"], "boundary_id", stable_id=True),
        author=_string(obj["author"], "author"),
        correlation_id=_string(obj["correlation_id"], "correlation_id", stable_id=True),
        created_at=_timestamp(obj["created_at"], "created_at"),
        supersedes_receipt_path=None if path is None else _string(path, "supersedes_receipt_path"),
        supersedes_payload_sha256=_sha(digest, "supersedes_payload_sha256", optional=True),
    )


def _runtime_from_obj(value: object) -> RuntimeObservation:
    obj = _expect_keys(value, {"runtime_id", "harness_id", "account_ref", "responsive", "self_identification", "role_identification", "authority_limits", "current_work_refs", "receiver_evidence_sha256", "external_actions_disabled", "observed_at"}, where="runtime_observation")
    if type(obj["responsive"]) is not bool or type(obj["external_actions_disabled"]) is not bool:
        raise ContinuityError("invalid-runtime-observation", "booleans")
    result = RuntimeObservation(
        runtime_id=_string(obj["runtime_id"], "runtime_id", stable_id=True),
        harness_id=_string(obj["harness_id"], "harness_id", stable_id=True),
        account_ref=_string(obj["account_ref"], "account_ref"),
        responsive=obj["responsive"],
        self_identification=None if obj["self_identification"] is None else _string(obj["self_identification"], "self_identification", allow_empty=True),
        role_identification=None if obj["role_identification"] is None else _string(obj["role_identification"], "role_identification", allow_empty=True),
        authority_limits=_reference_tuple(obj["authority_limits"], "authority_limits"),
        current_work_refs=_reference_tuple(obj["current_work_refs"], "current_work_refs"),
        receiver_evidence_sha256=_sha(obj["receiver_evidence_sha256"], "receiver_evidence_sha256", optional=True),
        external_actions_disabled=obj["external_actions_disabled"],
        observed_at=_timestamp(obj["observed_at"], "observed_at"),
    )
    return result


def _candidate_evidence_from_obj(value: object) -> CandidateVerificationEvidence:
    keys = {
        "runtime_id", "persona_id", "organism_id", "role_id", "role_authority_refs",
        "current_commitment_refs", "expected_next_action", "authority_limits",
        "unavailable_capabilities", "capability_evidence_complete", "checkpoint_sha256",
        "descriptor_sha256", "source_revisions", "communication_correlation_id",
        "communication_evidence_sha256", "communication_ok", "external_actions_disabled",
        "old_embodiment_sole_authority", "behavioral_variance", "variance_evidence_sha256",
        "observed_at",
    }
    obj = _expect_keys(value, keys, where="candidate-evidence")
    for name in ("capability_evidence_complete", "communication_ok", "external_actions_disabled", "old_embodiment_sole_authority"):
        if type(obj[name]) is not bool:
            raise ContinuityError("invalid-boolean", f"candidate-evidence.{name}")
    revisions_raw = obj["source_revisions"]
    if not isinstance(revisions_raw, list) or not revisions_raw or len(revisions_raw) > MAX_REFS:
        raise ContinuityError("invalid-list", "candidate-evidence.source_revisions")
    revisions: list[CandidateSourceRevision] = []
    for item in revisions_raw:
        entry = _expect_keys(item, {"uri", "source_revision", "source_sha256"}, where="candidate-evidence.source_revision")
        uri = _canonical_uri(entry["uri"])[0] + ":" + _canonical_uri(entry["uri"])[1].as_posix()
        revisions.append(CandidateSourceRevision(uri, _string(entry["source_revision"], "source_revision"), _sha(entry["source_sha256"], "source_sha256") or ""))
    if len({item.uri for item in revisions}) != len(revisions):
        raise ContinuityError("duplicate-item", "candidate-evidence.source_revisions")
    return CandidateVerificationEvidence(
        runtime_id=_string(obj["runtime_id"], "runtime_id", stable_id=True),
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        role_id=_canonical_uri(obj["role_id"])[0] + ":" + _canonical_uri(obj["role_id"])[1].as_posix(),
        role_authority_refs=_reference_tuple(obj["role_authority_refs"], "role_authority_refs"),
        current_commitment_refs=_reference_tuple(obj["current_commitment_refs"], "current_commitment_refs"),
        expected_next_action=_string(obj["expected_next_action"], "expected_next_action"),
        authority_limits=_reference_tuple(obj["authority_limits"], "authority_limits"),
        unavailable_capabilities=_reference_tuple(obj["unavailable_capabilities"], "unavailable_capabilities"),
        capability_evidence_complete=obj["capability_evidence_complete"],
        checkpoint_sha256=_sha(obj["checkpoint_sha256"], "checkpoint_sha256") or "",
        descriptor_sha256=_sha(obj["descriptor_sha256"], "descriptor_sha256") or "",
        source_revisions=tuple(revisions),
        communication_correlation_id=_string(obj["communication_correlation_id"], "communication_correlation_id", stable_id=True),
        communication_evidence_sha256=_sha(obj["communication_evidence_sha256"], "communication_evidence_sha256") or "",
        communication_ok=obj["communication_ok"],
        external_actions_disabled=obj["external_actions_disabled"],
        old_embodiment_sole_authority=obj["old_embodiment_sole_authority"],
        behavioral_variance=_string_tuple(obj["behavioral_variance"], "behavioral_variance"),
        variance_evidence_sha256=_sha(obj["variance_evidence_sha256"], "variance_evidence_sha256", optional=True),
        observed_at=_timestamp(obj["observed_at"], "observed_at"),
    )


def _ensure_secure_directory(path: Path) -> None:
    fd = _open_directory_no_follow(path, expected_uid=os.getuid(), create=True, exact_mode=0o700)
    os.close(fd)


def _read_regular_fd(fd: int, *, max_bytes: int, exact_mode: int = 0o600) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ContinuityError("published-file-unsafe", "not an owned regular file")
    if stat.S_IMODE(info.st_mode) != exact_mode:
        raise ContinuityError("mode-mismatch", f"published file: {stat.S_IMODE(info.st_mode):04o}")
    if info.st_size <= 0 or info.st_size > max_bytes:
        raise ContinuityError("source-size", f"published file: {info.st_size}")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ContinuityError("source-size", f"published file exceeds {max_bytes}")
    return b"".join(chunks)


def _publish_immutable_file(
    directory: Path,
    filename: str,
    raw: bytes,
    *,
    max_bytes: int,
    exists_code: str,
    conflict_code: str | None = None,
) -> bool:
    """Durably publish bytes under *filename* relative to a trusted dir fd.

    Returns True when this call publishes the file and False only when an
    identical existing file is accepted through *conflict_code*.
    """
    if "/" in filename or filename in {"", ".", ".."}:
        raise ContinuityError("invalid-filename", filename)
    directory_fd = _open_directory_no_follow(directory, expected_uid=os.getuid(), create=True, exact_mode=0o700)
    temporary_name = f".{filename}.{os.urandom(8).hex()}.tmp"
    temporary_fd: int | None = None
    published = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        offset = 0
        while offset < len(raw):
            offset += os.write(temporary_fd, raw[offset:])
        os.fsync(temporary_fd)
        if _read_regular_fd(temporary_fd, max_bytes=max_bytes) != raw:
            raise ContinuityError("publication-readback-drift", filename)
        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            if conflict_code is None:
                raise ContinuityError(exists_code, str(directory / filename)) from exc
            existing_fd = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                if _read_regular_fd(existing_fd, max_bytes=max_bytes) != raw:
                    raise ContinuityError(conflict_code, str(directory / filename))
            finally:
                os.close(existing_fd)
            return False
        published = True
        final_fd = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            if _read_regular_fd(final_fd, max_bytes=max_bytes) != raw:
                raise ContinuityError("publication-readback-drift", filename)
        finally:
            os.close(final_fd)
        os.fsync(directory_fd)
        return True
    except ContinuityError:
        # Already-typed error (e.g. publication-readback-drift, conflict/exists
        # translation). Preserve as-is; cleanup + re-raise. T-3459 residual fix:
        # cleanup failures MUST NOT replace the primary typed error.
        if published:
            try:
                os.unlink(filename, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass  # cleanup best-effort; primary ContinuityError wins
        raise
    except OSError as exc:
        # T-3459: publication-boundary filesystem failure (temp-open / write /
        # fsync / link / final-open / directory-fsync / etc.) that ISN'T
        # FileExistsError (which has its own conflict-code translation above).
        # Translate into ContinuityError so the CLI's typed-refusal contract
        # holds; the CLI catches only ContinuityError, so a raw OSError would
        # emit a traceback instead of bounded JSON. Cleanup best-effort;
        # cleanup failures do NOT replace the primary translated error.
        if published:
            try:
                os.unlink(filename, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass  # cleanup best-effort; primary publication-failed wins
        errno_name = errno.errorcode.get(exc.errno, str(exc.errno or "unknown"))
        raise ContinuityError(
            "publication-failed",
            f"{directory / filename}: {errno_name}: {exc.strerror or exc}",
            status="unavailable",
        ) from exc
    finally:
        # Cleanup: close temp fd, unlink temp file, close dir fd. Each is
        # wrapped in a broad OSError catch (T-3459) so a cleanup failure
        # cannot replace the primary error propagating out of the except
        # blocks above.
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass  # cleanup best-effort
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except OSError:
            pass  # cleanup best-effort (was FileNotFoundError only pre-T-3459)
        try:
            os.close(directory_fd)
        except OSError:
            pass  # cleanup best-effort


def _read_prior_receipt(boundary: CheckpointBoundary, checkpoint_root: Path) -> str | None:
    if boundary.supersedes_receipt_path is None:
        return None
    receipt_path = Path(boundary.supersedes_receipt_path)
    root = _validated_root(checkpoint_root)
    try:
        relative = receipt_path.absolute().relative_to(root)
    except ValueError as exc:
        raise ContinuityError("supersession-path-invalid", str(receipt_path)) from exc
    raw = _read_under_root(root, PurePosixPath(*relative.parts), max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid(), exact_mode=0o600)
    envelope = _parse_json_bytes(raw, where=str(receipt_path))
    actual = envelope.get("payload_sha256")
    if actual != boundary.supersedes_payload_sha256:
        raise ContinuityError("supersession-mismatch", str(receipt_path))
    return str(actual)


def write_checkpoint(
    view: ResolvedContinuityView,
    *,
    content: CheckpointContent,
    boundary: CheckpointBoundary,
    runtime_observation: RuntimeObservation,
    checkpoint_root: Path,
    producer_revision: str,
) -> CheckpointReceipt:
    checkpoint_root = checkpoint_root.absolute()
    view = _resolved_view_from_obj(_plain(view))
    content = _content_from_obj(_plain(content))
    boundary = _boundary_from_obj(_plain(boundary))
    runtime_observation = _runtime_from_obj(_plain(runtime_observation))
    _validate_runtime_observation(runtime_observation)
    _string(producer_revision, "producer_revision")
    prior_digest = _read_prior_receipt(boundary, checkpoint_root) if boundary.supersedes_receipt_path else None
    checkpoint_id = f"{view.persona_id}-{boundary.boundary_id}-{view.view_sha256[:16]}"
    _string(checkpoint_id, "checkpoint_id", stable_id=True)
    base: dict[str, object] = {
        "schema": CHECKPOINT_SCHEMA_ID,
        "checkpoint_id": checkpoint_id,
        "persona_id": view.persona_id,
        "organism_id": view.organism_id,
        "view_sha256": view.view_sha256,
        "purpose": view.purpose,
        "actor_digest": view.actor_digest,
        "descriptor_sha256": view.descriptor_sha256,
        "source_policy_locator": view.source_policy_locator,
        "source_policy_revision": view.source_policy_revision,
        "source_policy_sha256": view.source_policy_sha256,
        "source_refs": _plain(view.source_evidence),
        "content": _plain(content),
        "runtime_observation": _plain(runtime_observation),
        "boundary": _plain(boundary),
        "producer_revision": producer_revision,
        "prior_checkpoint_sha256": prior_digest,
    }
    payload_sha = _sha256(_json_bytes(base))
    envelope = dict(base, payload_sha256=payload_sha)
    raw = _json_bytes(envelope)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ContinuityError("checkpoint-size", str(len(raw)))
    _ensure_secure_directory(checkpoint_root)
    persona_dir = checkpoint_root / view.persona_id
    _ensure_secure_directory(persona_dir)
    path = persona_dir / f"{checkpoint_id}.json"
    _publish_immutable_file(
        persona_dir,
        path.name,
        raw,
        max_bytes=MAX_ENVELOPE_BYTES,
        exists_code="checkpoint-exists",
    )
    receipt_base = {
        "checkpoint_id": checkpoint_id,
        "persona_id": view.persona_id,
        "view_sha256": view.view_sha256,
        "payload_sha256": payload_sha,
        "path": str(path),
        "created_at": boundary.created_at,
    }
    return CheckpointReceipt(**receipt_base, receipt_sha256=_sha256(_json_bytes(receipt_base)))


def _evidence_from_obj(value: object, where: str) -> ResolvedRefEvidence:
    obj = _expect_keys(value, {"kind", "uri", "source_revision", "source_sha256", "authority_class", "privacy", "redacted", "size_bytes"}, where=where)
    if type(obj["redacted"]) is not bool or type(obj["size_bytes"]) is not int or obj["size_bytes"] <= 0:
        raise ContinuityError("invalid-evidence", where)
    return ResolvedRefEvidence(
        kind=_enum(ContinuityRefKind, obj["kind"], f"{where}.kind").value,
        uri=_canonical_uri(obj["uri"])[0] + ":" + _canonical_uri(obj["uri"])[1].as_posix(),
        source_revision=_string(obj["source_revision"], f"{where}.source_revision"),
        source_sha256=_sha(obj["source_sha256"], f"{where}.source_sha256") or "",
        authority_class=_string(obj["authority_class"], f"{where}.authority_class", stable_id=True),
        privacy=_enum(PrivacyClass, obj["privacy"], f"{where}.privacy").value,
        redacted=obj["redacted"],
        size_bytes=obj["size_bytes"],
    )


def _resolved_view_from_obj(value: object) -> ResolvedContinuityView:
    required = {"schema", "persona_id", "organism_id", "purpose", "actor_digest", "descriptor_sha256", "source_policy_locator", "source_policy_revision", "source_policy_sha256", "source_evidence", "omissions", "behavioral_variance", "resolved_at", "view_sha256"}
    obj = _expect_keys(value, required, where="view")
    if obj["schema"] != VIEW_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "view")
    evidence_raw = obj["source_evidence"]
    omissions_raw = obj["omissions"]
    if not isinstance(evidence_raw, list) or not isinstance(omissions_raw, list):
        raise ContinuityError("invalid-list", "view evidence")
    omissions: list[OmissionEvidence] = []
    for item in omissions_raw:
        entry = _expect_keys(item, {"kind", "uri", "reason"}, where="view.omission")
        scheme, rel = _canonical_uri(entry["uri"])
        omissions.append(OmissionEvidence(_enum(ContinuityRefKind, entry["kind"], "kind").value, f"{scheme}:{rel.as_posix()}", _string(entry["reason"], "reason")))
    base = _without(obj, "view_sha256")
    if _sha256(_json_bytes(base)) != obj["view_sha256"]:
        raise ContinuityError("view-digest-drift", "view")
    return ResolvedContinuityView(
        schema=VIEW_SCHEMA_ID,
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        purpose=_enum(ContinuityPurpose, obj["purpose"], "purpose").value,
        actor_digest=_sha(obj["actor_digest"], "actor_digest") or "",
        descriptor_sha256=_sha(obj["descriptor_sha256"], "descriptor_sha256") or "",
        source_policy_locator=_string(obj["source_policy_locator"], "source_policy_locator"),
        source_policy_revision=_string(obj["source_policy_revision"], "source_policy_revision"),
        source_policy_sha256=_sha(obj["source_policy_sha256"], "source_policy_sha256") or "",
        source_evidence=tuple(_evidence_from_obj(item, "view.source_evidence[]") for item in evidence_raw),
        omissions=tuple(omissions),
        behavioral_variance=_string_tuple(obj["behavioral_variance"], "behavioral_variance"),
        resolved_at=_timestamp(obj["resolved_at"], "resolved_at"),
        view_sha256=_sha(obj["view_sha256"], "view_sha256") or "",
    )


def load_resolved_view(path: Path) -> ResolvedContinuityView:
    raw = _read_regular_file(path, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid())
    return _resolved_view_from_obj(_parse_json_bytes(raw, where=str(path)))


def load_checkpoint_envelope(path: Path, *, checkpoint_root: Path) -> CheckpointEnvelope:
    root = _validated_root(checkpoint_root)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ContinuityError("checkpoint-path-invalid", str(path)) from exc
    raw = _read_under_root(root, PurePosixPath(*relative.parts), max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid(), exact_mode=0o600)
    keys = {"schema", "checkpoint_id", "persona_id", "organism_id", "view_sha256", "purpose", "actor_digest", "descriptor_sha256", "source_policy_locator", "source_policy_revision", "source_policy_sha256", "source_refs", "content", "runtime_observation", "boundary", "producer_revision", "prior_checkpoint_sha256", "payload_sha256"}
    obj = _expect_keys(_parse_json_bytes(raw, where=str(path)), keys, where="checkpoint")
    if obj["schema"] != CHECKPOINT_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "checkpoint")
    base = _without(obj, "payload_sha256")
    if _sha256(_json_bytes(base)) != obj["payload_sha256"]:
        raise ContinuityError("checkpoint-digest-drift", str(path))
    source_refs = obj["source_refs"]
    if not isinstance(source_refs, list) or not source_refs:
        raise ContinuityError("invalid-list", "checkpoint.source_refs")
    return CheckpointEnvelope(
        schema=CHECKPOINT_SCHEMA_ID,
        checkpoint_id=_string(obj["checkpoint_id"], "checkpoint_id", stable_id=True),
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        view_sha256=_sha(obj["view_sha256"], "view_sha256") or "",
        purpose=_enum(ContinuityPurpose, obj["purpose"], "purpose").value,
        actor_digest=_sha(obj["actor_digest"], "actor_digest") or "",
        descriptor_sha256=_sha(obj["descriptor_sha256"], "descriptor_sha256") or "",
        source_policy_locator=_string(obj["source_policy_locator"], "source_policy_locator"),
        source_policy_revision=_string(obj["source_policy_revision"], "source_policy_revision"),
        source_policy_sha256=_sha(obj["source_policy_sha256"], "source_policy_sha256") or "",
        source_refs=tuple(_evidence_from_obj(item, "checkpoint.source_refs[]") for item in source_refs),
        content=_content_from_obj(obj["content"]),
        runtime_observation=_runtime_from_obj(obj["runtime_observation"]),
        boundary=_boundary_from_obj(obj["boundary"]),
        producer_revision=_string(obj["producer_revision"], "producer_revision"),
        prior_checkpoint_sha256=_sha(obj["prior_checkpoint_sha256"], "prior_checkpoint_sha256", optional=True),
        payload_sha256=_sha(obj["payload_sha256"], "payload_sha256") or "",
    )


def _normalize_checkpoint_envelope(value: CheckpointEnvelope) -> CheckpointEnvelope:
    keys = {"schema", "checkpoint_id", "persona_id", "organism_id", "view_sha256", "purpose", "actor_digest", "descriptor_sha256", "source_policy_locator", "source_policy_revision", "source_policy_sha256", "source_refs", "content", "runtime_observation", "boundary", "producer_revision", "prior_checkpoint_sha256", "payload_sha256"}
    obj = _expect_keys(_plain(value), keys, where="checkpoint")
    if obj["schema"] != CHECKPOINT_SCHEMA_ID:
        raise ContinuityError("schema-mismatch", "checkpoint")
    base = _without(obj, "payload_sha256")
    if _sha256(_json_bytes(base)) != obj["payload_sha256"]:
        raise ContinuityError("checkpoint-digest-drift", "checkpoint")
    source_refs = obj["source_refs"]
    if not isinstance(source_refs, list) or not source_refs:
        raise ContinuityError("invalid-list", "checkpoint.source_refs")
    return CheckpointEnvelope(
        schema=CHECKPOINT_SCHEMA_ID,
        checkpoint_id=_string(obj["checkpoint_id"], "checkpoint_id", stable_id=True),
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        organism_id=_string(obj["organism_id"], "organism_id", stable_id=True),
        view_sha256=_sha(obj["view_sha256"], "view_sha256") or "",
        purpose=_enum(ContinuityPurpose, obj["purpose"], "purpose").value,
        actor_digest=_sha(obj["actor_digest"], "actor_digest") or "",
        descriptor_sha256=_sha(obj["descriptor_sha256"], "descriptor_sha256") or "",
        source_policy_locator=_string(obj["source_policy_locator"], "source_policy_locator"),
        source_policy_revision=_string(obj["source_policy_revision"], "source_policy_revision"),
        source_policy_sha256=_sha(obj["source_policy_sha256"], "source_policy_sha256") or "",
        source_refs=tuple(_evidence_from_obj(item, "checkpoint.source_refs[]") for item in source_refs),
        content=_content_from_obj(obj["content"]),
        runtime_observation=_runtime_from_obj(obj["runtime_observation"]),
        boundary=_boundary_from_obj(obj["boundary"]),
        producer_revision=_string(obj["producer_revision"], "producer_revision"),
        prior_checkpoint_sha256=_sha(obj["prior_checkpoint_sha256"], "prior_checkpoint_sha256", optional=True),
        payload_sha256=_sha(obj["payload_sha256"], "payload_sha256") or "",
    )


def build_verification_report(*, envelope: CheckpointEnvelope, candidate_evidence: CandidateVerificationEvidence) -> ContinuityVerificationReport:
    envelope = _normalize_checkpoint_envelope(envelope)
    candidate_evidence = _candidate_evidence_from_obj(_plain(candidate_evidence))
    obs = envelope.runtime_observation
    expected_commitments = tuple(sorted((*envelope.content.active_ticket_refs, *envelope.content.promise_refs, *envelope.content.wait_refs, *envelope.content.escalation_refs)))
    expected_authority_refs = tuple(sorted({f"authority:{item.authority_class}" for item in envelope.source_refs if item.kind == "authority"}))
    role_refs = tuple(item for item in envelope.source_refs if item.kind == "role")
    expected_role_ref = role_refs[0].uri if len(role_refs) == 1 else None
    expected_revisions = tuple(sorted((item.uri, item.source_revision, item.source_sha256) for item in envelope.source_refs))
    candidate_revisions = tuple(sorted((item.uri, item.source_revision, item.source_sha256) for item in candidate_evidence.source_revisions))
    checks = {
        "candidate-persona-organism": candidate_evidence.persona_id == envelope.persona_id and candidate_evidence.organism_id == envelope.organism_id,
        "role-authority-boundary": candidate_evidence.role_id == expected_role_ref and tuple(sorted(candidate_evidence.role_authority_refs)) == expected_authority_refs,
        "commitments-next-action": tuple(sorted(candidate_evidence.current_commitment_refs)) == expected_commitments and candidate_evidence.expected_next_action == envelope.content.expected_next_action,
        "authority-unavailable-capabilities": tuple(sorted(candidate_evidence.authority_limits)) == tuple(sorted(obs.authority_limits)) and candidate_evidence.capability_evidence_complete,
        "last-checkpoint-digest": candidate_evidence.checkpoint_sha256 == envelope.payload_sha256,
        "bundle-source-revisions": candidate_evidence.descriptor_sha256 == envelope.descriptor_sha256 and candidate_revisions == expected_revisions,
        "candidate-communication-correlation": candidate_evidence.communication_ok and candidate_evidence.communication_correlation_id == envelope.boundary.correlation_id and bool(candidate_evidence.communication_evidence_sha256),
        "candidate-actions-disabled": candidate_evidence.external_actions_disabled,
        "old-embodiment-sole-authority": candidate_evidence.old_embodiment_sole_authority,
        "behavioral-variance-separated": bool(candidate_evidence.variance_evidence_sha256),
    }
    assertions = tuple(VerificationAssertion(item, checks[item], "present" if checks[item] else "missing") for item in VERIFICATION_ASSERTION_IDS)
    base: dict[str, object] = {
        "schema": VERIFICATION_SCHEMA_ID,
        "persona_id": envelope.persona_id,
        "candidate_runtime": candidate_evidence.runtime_id,
        "checkpoint_sha256": envelope.payload_sha256,
        "assertions": _plain(assertions),
        "external_actions_disabled": candidate_evidence.external_actions_disabled,
        "behavioral_variance": list(candidate_evidence.behavioral_variance),
        "verified_at": _now(),
    }
    return ContinuityVerificationReport(
        schema=VERIFICATION_SCHEMA_ID,
        persona_id=envelope.persona_id,
        candidate_runtime=candidate_evidence.runtime_id,
        checkpoint_sha256=envelope.payload_sha256,
        assertions=assertions,
        external_actions_disabled=candidate_evidence.external_actions_disabled,
        behavioral_variance=candidate_evidence.behavioral_variance,
        verified_at=str(base["verified_at"]),
        report_sha256=_sha256(_json_bytes(base)),
    )


def load_verification_report(path: Path) -> ContinuityVerificationReport:
    raw = _read_regular_file(path, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid())
    keys = {"schema", "persona_id", "candidate_runtime", "checkpoint_sha256", "assertions", "external_actions_disabled", "behavioral_variance", "verified_at", "report_sha256"}
    obj = _expect_keys(_parse_json_bytes(raw, where=str(path)), keys, where="verification")
    if obj["schema"] != VERIFICATION_SCHEMA_ID or type(obj["external_actions_disabled"]) is not bool:
        raise ContinuityError("verification-invalid", str(path))
    assertions_raw = obj["assertions"]
    if not isinstance(assertions_raw, list):
        raise ContinuityError("invalid-list", "verification.assertions")
    assertions: list[VerificationAssertion] = []
    for item in assertions_raw:
        entry = _expect_keys(item, {"assertion_id", "passed", "evidence"}, where="verification.assertion")
        if type(entry["passed"]) is not bool:
            raise ContinuityError("invalid-boolean", "verification.assertion.passed")
        assertions.append(VerificationAssertion(_string(entry["assertion_id"], "assertion_id"), entry["passed"], _string(entry["evidence"], "evidence")))
    if tuple(item.assertion_id for item in assertions) != VERIFICATION_ASSERTION_IDS:
        raise ContinuityError("verification-assertions", "must contain fixed ten in order")
    base = _without(obj, "report_sha256")
    if _sha256(_json_bytes(base)) != obj["report_sha256"]:
        raise ContinuityError("verification-digest-drift", str(path))
    return ContinuityVerificationReport(
        schema=VERIFICATION_SCHEMA_ID,
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        candidate_runtime=_string(obj["candidate_runtime"], "candidate_runtime", stable_id=True),
        checkpoint_sha256=_sha(obj["checkpoint_sha256"], "checkpoint_sha256") or "",
        assertions=tuple(assertions),
        external_actions_disabled=obj["external_actions_disabled"],
        behavioral_variance=_string_tuple(obj["behavioral_variance"], "behavioral_variance"),
        verified_at=_timestamp(obj["verified_at"], "verified_at"),
        report_sha256=_sha(obj["report_sha256"], "report_sha256") or "",
    )


def validate_switch_receipt_file(path: Path) -> HarnessSwitchReceipt:
    raw = _read_regular_file(path, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid())
    keys = {
        "schema", "receipt_id", "persona_id", "approved_plan_sha256", "actor_id",
        "reviewed_source_revision", "old_runtime_observation", "candidate_runtime_observation",
        "checkpoint_sha256", "verification_sha256", "compatibility_result",
        "old_receiver_isolation_sha256", "candidate_receiver_isolation_sha256", "outcome",
        "manifest_revision", "controller_revision", "prior_receipt_sha256", "receipt_sha256",
    }
    obj = _expect_keys(_parse_json_bytes(raw, where=str(path)), keys, where="switch-receipt")
    if obj["schema"] != SWITCH_SCHEMA_ID or obj["outcome"] not in {"committed", "rolled-back"}:
        raise ContinuityError("switch-receipt-invalid", str(path))
    base = _without(obj, "receipt_sha256")
    if _sha256(_json_bytes(base)) != obj["receipt_sha256"]:
        raise ContinuityError("switch-receipt-digest-drift", str(path))
    return HarnessSwitchReceipt(
        schema=SWITCH_SCHEMA_ID,
        receipt_id=_string(obj["receipt_id"], "receipt_id", stable_id=True),
        persona_id=_string(obj["persona_id"], "persona_id", stable_id=True),
        approved_plan_sha256=_sha(obj["approved_plan_sha256"], "approved_plan_sha256") or "",
        actor_id=_string(obj["actor_id"], "actor_id", stable_id=True),
        reviewed_source_revision=_string(obj["reviewed_source_revision"], "reviewed_source_revision"),
        old_runtime_observation=_runtime_from_obj(obj["old_runtime_observation"]),
        candidate_runtime_observation=_runtime_from_obj(obj["candidate_runtime_observation"]),
        checkpoint_sha256=_sha(obj["checkpoint_sha256"], "checkpoint_sha256") or "",
        verification_sha256=_sha(obj["verification_sha256"], "verification_sha256") or "",
        compatibility_result=_string(obj["compatibility_result"], "compatibility_result"),
        old_receiver_isolation_sha256=_sha(obj["old_receiver_isolation_sha256"], "old_receiver_isolation_sha256") or "",
        candidate_receiver_isolation_sha256=_sha(obj["candidate_receiver_isolation_sha256"], "candidate_receiver_isolation_sha256") or "",
        outcome=obj["outcome"],
        manifest_revision=_string(obj["manifest_revision"], "manifest_revision"),
        controller_revision=_string(obj["controller_revision"], "controller_revision"),
        prior_receipt_sha256=_sha(obj["prior_receipt_sha256"], "prior_receipt_sha256", optional=True),
        receipt_sha256=_sha(obj["receipt_sha256"], "receipt_sha256") or "",
    )


def classify_legacy_session_state(path: Path, *, persona_id: str, state_root: Path) -> LegacySessionEvidence:
    root = _validated_root(state_root)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ContinuityError("legacy-path-invalid", str(path)) from exc
    raw = _read_under_root(root, PurePosixPath(*relative.parts), max_bytes=MAX_BUNDLE_BYTES, expected_uid=os.getuid())
    obj = _parse_json_bytes(raw, where=str(path))
    allowed = {"persona_id", "session_id", "updated_at", "harness"}
    if set(obj) - allowed:
        raise ContinuityError("legacy-unknown-field", str(path))
    observed_persona = obj.get("persona_id", persona_id)
    if observed_persona != persona_id:
        raise ContinuityError("legacy-persona-mismatch", str(path))
    session = obj.get("session_id")
    if session is not None:
        session = _string(session, "legacy.session_id")
    return LegacySessionEvidence(
        schema=LEGACY_EVIDENCE_SCHEMA_ID,
        persona_id=persona_id,
        classification=SourceClassification.RUNTIME_EVIDENCE,
        cache_only=True,
        can_satisfy_required_ref=False,
        source_sha256=_sha256(raw),
        source_locator=str(root.joinpath(*relative.parts)),
        session_id_sha256=None if session is None else _sha256(session.encode("utf-8")),
    )


def migrate_legacy_session_state(path: Path, *, persona_id: str, state_root: Path, output_root: Path) -> LegacyMigrationReceipt:
    evidence = classify_legacy_session_state(path, persona_id=persona_id, state_root=state_root)
    _ensure_secure_directory(output_root)
    legacy_dir = output_root / "legacy-session-evidence"
    _ensure_secure_directory(legacy_dir)
    persona_dir = legacy_dir / persona_id
    _ensure_secure_directory(persona_dir)
    output = persona_dir / f"{evidence.source_sha256}.json"
    raw = _json_bytes(_plain(evidence))
    digest = _sha256(raw)
    created = _publish_immutable_file(
        persona_dir,
        output.name,
        raw,
        max_bytes=MAX_BUNDLE_BYTES,
        exists_code="legacy-migration-exists",
        conflict_code="legacy-migration-conflict",
    )
    return LegacyMigrationReceipt("created" if created else "idempotent", str(output), evidence.source_sha256, digest)


def _inventory_walk(root: Path, *, expected_uid: int, max_files: int, max_bytes: int) -> tuple[SnapshotInventoryEntry, ...]:
    entries: list[SnapshotInventoryEntry] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        info = current.lstat()
        if info.st_uid != expected_uid:
            raise ContinuityError("owner-mismatch", str(current))
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
            raise ContinuityError("inventory-directory-unsafe", str(current))
        for name in sorted(dirnames):
            child = current / name
            if child.is_symlink():
                raise ContinuityError("inventory-symlink", str(child))
        for name in sorted(filenames):
            path = current / name
            if path.is_symlink():
                raise ContinuityError("inventory-symlink", str(path))
            raw = _read_regular_file(path, max_bytes=max_bytes, expected_uid=expected_uid)
            total += len(raw)
            if total > max_bytes or len(entries) >= max_files:
                raise ContinuityError("inventory-bounds", str(root))
            try:
                parsed = _parse_json_bytes(raw, where=str(path))
            except ContinuityError:
                continue
            schema = parsed.get("schema")
            if schema not in {CHECKPOINT_SCHEMA_ID, VERIFICATION_SCHEMA_ID, LEGACY_EVIDENCE_SCHEMA_ID}:
                continue
            rel = path.relative_to(root).as_posix()
            entries.append(SnapshotInventoryEntry(
                rel_path=rel,
                schema=str(schema),
                sha256=_sha256(raw),
                size_bytes=len(raw),
                mode=f"{stat.S_IMODE(path.stat(follow_symlinks=False).st_mode):04o}",
                classification=SourceClassification.DERIVED,
            ))
    return tuple(sorted(entries, key=lambda item: item.rel_path))


def build_snapshot_export_inventory(continuity_root: Path, *, expected_uid: int | None = None, max_files: int = MAX_INVENTORY_FILES, max_bytes: int = MAX_SOURCE_BYTES) -> SnapshotExportInventory:
    root = _validated_root(continuity_root, expected_uid=os.getuid() if expected_uid is None else expected_uid)
    uid = os.getuid() if expected_uid is None else expected_uid
    entries = _inventory_walk(root, expected_uid=uid, max_files=max_files, max_bytes=max_bytes)
    base = {"schema": SNAPSHOT_INVENTORY_SCHEMA_ID, "entries": _plain(entries)}
    return SnapshotExportInventory(SNAPSHOT_INVENTORY_SCHEMA_ID, _sha256(_json_bytes(base)), entries)


def write_snapshot_export_inventory(inventory: SnapshotExportInventory, output_path: Path) -> None:
    parent = output_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = parent.lstat()
    # The snapshot repository may be intentionally group-writable for the
    # pantheon operators group.  The inventory file itself is always 0600;
    # reject only foreign ownership, symlinks, and world-writable parents.
    if stat.S_ISLNK(parent_info.st_mode) or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o002:
        raise ContinuityError("inventory-output-parent-unsafe", str(parent))
    if output_path.exists() or output_path.is_symlink():
        info = output_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ContinuityError("inventory-output-unsafe", str(output_path))
    raw = _json_bytes(_plain(inventory))
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=parent)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        if _read_regular_file(temporary, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid(), exact_mode=0o600) != raw:
            raise ContinuityError("inventory-readback-drift", str(temporary))
        os.replace(temporary, output_path)
        temporary = None
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _object_schema(required: Sequence[str], properties: Mapping[str, object]) -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "required": list(required), "properties": dict(properties)}


def _text_schema(*, enum: Sequence[str] | None = None, pattern: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "minLength": 1, "maxLength": MAX_STRING}
    if enum is not None:
        result["enum"] = list(enum)
    if pattern is not None:
        result["pattern"] = pattern
    return result


def _reference_schema() -> dict[str, object]:
    return _text_schema(pattern=r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _schema_document(schema_id: str, body: Mapping[str, object]) -> bytes:
    return _json_bytes({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": schema_id, **body})


def _ref_schema() -> dict[str, object]:
    fields = ["kind", "uri", "expected_revision", "expected_sha256", "authority_class", "required", "purpose_limits"]
    result = _object_schema(fields, {
        "kind": _text_schema(enum=[item.value for item in ContinuityRefKind]),
        "uri": _text_schema(pattern=r"^(repo|state):[^/].*$"),
        "expected_revision": {"type": ["string", "null"], "maxLength": MAX_STRING},
        "expected_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "authority_class": _text_schema(),
        "required": {"type": "boolean"},
        "purpose_limits": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True, "items": _text_schema(enum=[item.value for item in ContinuityPurpose])},
    })
    result["allOf"] = [{
        "if": {"properties": {"required": {"const": True}}, "required": ["required"]},
        "then": {"properties": {
            "expected_revision": _text_schema(),
            "expected_sha256": _text_schema(pattern="^[0-9a-f]{64}$"),
        }},
    }]
    return result


def _evidence_schema() -> dict[str, object]:
    required = ["kind", "uri", "source_revision", "source_sha256", "authority_class", "privacy", "redacted", "size_bytes"]
    return _object_schema(required, {
        "kind": _text_schema(enum=[item.value for item in ContinuityRefKind]),
        "uri": _text_schema(pattern=r"^(repo|state):[^/].*$"),
        "source_revision": _text_schema(),
        "source_sha256": _text_schema(pattern="^[0-9a-f]{64}$"),
        "authority_class": _text_schema(),
        "privacy": _text_schema(enum=[item.value for item in PrivacyClass]),
        "redacted": {"type": "boolean"},
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_SOURCE_BYTES},
    })


def _omission_schema() -> dict[str, object]:
    return _object_schema(["kind", "uri", "reason"], {
        "kind": _text_schema(enum=[item.value for item in ContinuityRefKind]),
        "uri": _text_schema(pattern=r"^(repo|state):[^/].*$"),
        "reason": _text_schema(),
    })


def _content_schema() -> dict[str, object]:
    names = ["active_ticket_refs", "promise_refs", "wait_refs", "escalation_refs", "expected_next_action", "decision_refs", "unresolved_questions", "memory_candidate_refs"]
    properties: dict[str, object] = {
        name: {"type": "array", "maxItems": MAX_ITEMS, "uniqueItems": True, "items": _reference_schema()}
        for name in names if name != "expected_next_action"
    }
    properties["expected_next_action"] = _reference_schema()
    return _object_schema(names, properties)


def _runtime_schema() -> dict[str, object]:
    names = ["runtime_id", "harness_id", "account_ref", "responsive", "self_identification", "role_identification", "authority_limits", "current_work_refs", "receiver_evidence_sha256", "external_actions_disabled", "observed_at"]
    return _object_schema(names, {
        "runtime_id": _text_schema(),
        "harness_id": _text_schema(),
        "account_ref": _text_schema(),
        "responsive": {"type": "boolean"},
        "self_identification": {"type": ["string", "null"], "maxLength": MAX_STRING},
        "role_identification": {"type": ["string", "null"], "maxLength": MAX_STRING},
        "authority_limits": {"type": "array", "maxItems": MAX_ITEMS, "uniqueItems": True, "items": _reference_schema()},
        "current_work_refs": {"type": "array", "maxItems": MAX_ITEMS, "uniqueItems": True, "items": _reference_schema()},
        "receiver_evidence_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "external_actions_disabled": {"type": "boolean"},
        "observed_at": _text_schema(),
    })


def _boundary_schema() -> dict[str, object]:
    names = ["boundary_id", "author", "correlation_id", "created_at", "supersedes_receipt_path", "supersedes_payload_sha256"]
    return _object_schema(names, {
        "boundary_id": _text_schema(),
        "author": _text_schema(),
        "correlation_id": _text_schema(),
        "created_at": _text_schema(),
        "supersedes_receipt_path": {"type": ["string", "null"], "maxLength": MAX_STRING},
        "supersedes_payload_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
    })


def generate_continuity_bundle_schema() -> bytes:
    required = ["schema", "persona_id", "organism_id", "role_contract_ref", "identity_canon_ref", "values_and_goals_refs", "capability_profile_ref", "authority_profile_ref", "memory_root_refs", "commitment_query", "runtime_binding_ref", "provenance"]
    ref = _ref_schema()
    props: dict[str, object] = {key: ref for key in ("role_contract_ref", "identity_canon_ref", "capability_profile_ref", "authority_profile_ref", "runtime_binding_ref")}
    props.update({
        "schema": {"const": BUNDLE_SCHEMA_ID},
        "persona_id": _text_schema(pattern="^[a-z0-9][a-z0-9._:-]*$"),
        "organism_id": _text_schema(pattern="^[a-z0-9][a-z0-9._:-]*$"),
        "values_and_goals_refs": {"type": "array", "maxItems": MAX_REFS, "items": ref},
        "memory_root_refs": {"type": "array", "maxItems": MAX_REFS, "items": ref},
        "commitment_query": _object_schema(["kind", "persona_id", "states"], {"kind": {"const": "commitment"}, "persona_id": _text_schema(), "states": {"type": "array", "maxItems": MAX_ITEMS, "uniqueItems": True, "items": _text_schema()}}),
        "provenance": _object_schema(["authored_by", "adopted_by", "source_revision", "migrations", "persona_primitive_ref"], {"authored_by": _text_schema(), "adopted_by": _text_schema(), "source_revision": _text_schema(), "migrations": {"type": "array", "maxItems": MAX_ITEMS, "uniqueItems": True, "items": _text_schema()}, "persona_primitive_ref": ref}),
    })
    return _schema_document(BUNDLE_SCHEMA_ID, _object_schema(required, props))


def generate_continuity_view_schema() -> bytes:
    required = ["schema", "persona_id", "organism_id", "purpose", "actor_digest", "descriptor_sha256", "source_policy_locator", "source_policy_revision", "source_policy_sha256", "source_evidence", "omissions", "behavioral_variance", "resolved_at", "view_sha256"]
    generic = {key: _text_schema() for key in required}
    generic["schema"] = {"const": VIEW_SCHEMA_ID}
    generic["source_evidence"] = {"type": "array", "maxItems": MAX_REFS, "items": _evidence_schema()}
    generic["omissions"] = {"type": "array", "maxItems": MAX_REFS, "items": _omission_schema()}
    generic["behavioral_variance"] = {"type": "array", "maxItems": MAX_ITEMS, "items": _text_schema()}
    generic["purpose"] = _text_schema(enum=[item.value for item in ContinuityPurpose])
    for key in ("actor_digest", "descriptor_sha256", "source_policy_sha256", "view_sha256"):
        generic[key] = _text_schema(pattern="^[0-9a-f]{64}$")
    return _schema_document(VIEW_SCHEMA_ID, _object_schema(required, generic))


def generate_continuity_checkpoint_schema() -> bytes:
    required = ["schema", "checkpoint_id", "persona_id", "organism_id", "view_sha256", "purpose", "actor_digest", "descriptor_sha256", "source_policy_locator", "source_policy_revision", "source_policy_sha256", "source_refs", "content", "runtime_observation", "boundary", "producer_revision", "prior_checkpoint_sha256", "payload_sha256"]
    props = {key: _text_schema() for key in required}
    props["schema"] = {"const": CHECKPOINT_SCHEMA_ID}
    props["source_refs"] = {"type": "array", "minItems": 1, "maxItems": MAX_REFS, "items": _evidence_schema()}
    props["content"] = _content_schema()
    props["runtime_observation"] = _runtime_schema()
    props["boundary"] = _boundary_schema()
    props["prior_checkpoint_sha256"] = {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
    props["purpose"] = _text_schema(enum=[item.value for item in ContinuityPurpose])
    for key in ("view_sha256", "actor_digest", "descriptor_sha256", "source_policy_sha256", "payload_sha256"):
        props[key] = _text_schema(pattern="^[0-9a-f]{64}$")
    return _schema_document(CHECKPOINT_SCHEMA_ID, _object_schema(required, props))


def generate_continuity_verification_schema() -> bytes:
    required = ["schema", "persona_id", "candidate_runtime", "checkpoint_sha256", "assertions", "external_actions_disabled", "behavioral_variance", "verified_at", "report_sha256"]
    props = {key: _text_schema() for key in required}
    props["schema"] = {"const": VERIFICATION_SCHEMA_ID}
    props["assertions"] = {"type": "array", "minItems": 10, "maxItems": 10, "items": _object_schema(["assertion_id", "passed", "evidence"], {"assertion_id": _text_schema(enum=VERIFICATION_ASSERTION_IDS), "passed": {"type": "boolean"}, "evidence": _text_schema()})}
    props["external_actions_disabled"] = {"type": "boolean"}
    props["behavioral_variance"] = {"type": "array", "maxItems": MAX_ITEMS, "items": _text_schema()}
    props["checkpoint_sha256"] = _text_schema(pattern="^[0-9a-f]{64}$")
    props["report_sha256"] = _text_schema(pattern="^[0-9a-f]{64}$")
    return _schema_document(VERIFICATION_SCHEMA_ID, _object_schema(required, props))


def generate_continuity_switch_schema() -> bytes:
    required = ["schema", "receipt_id", "persona_id", "approved_plan_sha256", "actor_id", "reviewed_source_revision", "old_runtime_observation", "candidate_runtime_observation", "checkpoint_sha256", "verification_sha256", "compatibility_result", "old_receiver_isolation_sha256", "candidate_receiver_isolation_sha256", "outcome", "manifest_revision", "controller_revision", "prior_receipt_sha256", "receipt_sha256"]
    sha = _text_schema(pattern="^[0-9a-f]{64}$")
    return _schema_document(SWITCH_SCHEMA_ID, _object_schema(required, {
        "schema": {"const": SWITCH_SCHEMA_ID},
        "receipt_id": _text_schema(),
        "persona_id": _text_schema(),
        "approved_plan_sha256": sha,
        "actor_id": _text_schema(),
        "reviewed_source_revision": _text_schema(),
        "old_runtime_observation": _runtime_schema(),
        "candidate_runtime_observation": _runtime_schema(),
        "checkpoint_sha256": sha,
        "verification_sha256": sha,
        "compatibility_result": _text_schema(),
        "old_receiver_isolation_sha256": sha,
        "candidate_receiver_isolation_sha256": sha,
        "outcome": _text_schema(enum=["committed", "rolled-back"]),
        "manifest_revision": _text_schema(),
        "controller_revision": _text_schema(),
        "prior_receipt_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "receipt_sha256": sha,
    }))


def generate_continuity_schemas() -> dict[str, bytes]:
    return {
        "persona-continuity-bundle.schema.json": generate_continuity_bundle_schema(),
        "persona-continuity-view.schema.json": generate_continuity_view_schema(),
        "persona-continuity-checkpoint.schema.json": generate_continuity_checkpoint_schema(),
        "persona-continuity-verification.schema.json": generate_continuity_verification_schema(),
        "persona-continuity-switch.schema.json": generate_continuity_switch_schema(),
    }


def write_continuity_schema_files(repo_root: Path) -> tuple[str, ...]:
    schema_dir = repo_root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, raw in generate_continuity_schemas().items():
        path = schema_dir / name
        path.write_bytes(raw)
        written.append(str(path))
    return tuple(written)


def _load_json_dataclass(path: Path, kind: str) -> object:
    raw = _read_regular_file(path, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid())
    obj = _parse_json_bytes(raw, where=str(path))
    if kind == "content":
        return _content_from_obj(obj)
    if kind == "boundary":
        return _boundary_from_obj(obj)
    if kind == "runtime":
        return _runtime_from_obj(obj)
    if kind == "candidate":
        return _candidate_evidence_from_obj(obj)
    raise AssertionError(kind)


def _receipt_from_obj(obj: object) -> CheckpointReceipt:
    value = _expect_keys(obj, {"checkpoint_id", "persona_id", "view_sha256", "payload_sha256", "path", "created_at", "receipt_sha256"}, where="receipt")
    return CheckpointReceipt(
        checkpoint_id=_string(value["checkpoint_id"], "checkpoint_id", stable_id=True),
        persona_id=_string(value["persona_id"], "persona_id", stable_id=True),
        view_sha256=_sha(value["view_sha256"], "view_sha256") or "",
        payload_sha256=_sha(value["payload_sha256"], "payload_sha256") or "",
        path=_string(value["path"], "path"),
        created_at=_timestamp(value["created_at"], "created_at"),
        receipt_sha256=_sha(value["receipt_sha256"], "receipt_sha256") or "",
    )


def _write_json_stdout(value: object) -> None:
    raw = _json_bytes(_plain(value))
    if len(raw) > MAX_CLI_OUTPUT_BYTES:
        raise ContinuityError("cli-output-too-large", str(len(raw)))
    sys.stdout.buffer.write(raw)


def _atomic_write_secure(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ContinuityError("output-path-unsafe", str(path))
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="persona_continuity.py")
    sub = parser.add_subparsers(dest="command", required=True)
    generated = sub.add_parser("generate-schemas")
    generated.add_argument("--repo-root", type=Path, required=True)
    _add_json_flag(generated)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--manifest", type=Path, required=True)
    resolve.add_argument("--descriptor-root", type=Path, required=True)
    resolve.add_argument("--repo-root", type=Path, required=True)
    resolve.add_argument("--state-root", type=Path, required=True)
    resolve.add_argument("--source-policy", type=Path, required=True)
    resolve.add_argument("--checkpoint-root", type=Path, required=True)
    resolve.add_argument("--actor-id", required=True)
    resolve.add_argument("--purpose", choices=[item.value for item in ContinuityPurpose], required=True)
    resolve.add_argument("--output", type=Path)
    _add_json_flag(resolve)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--view", type=Path, required=True)
    checkpoint.add_argument("--content", type=Path, required=True)
    checkpoint.add_argument("--boundary", type=Path, required=True)
    checkpoint.add_argument("--runtime-observation", type=Path, required=True)
    checkpoint.add_argument("--checkpoint-root", type=Path, required=True)
    checkpoint.add_argument("--source-policy", type=Path, required=True)
    checkpoint.add_argument("--actor-id", required=True)
    checkpoint.add_argument("--purpose", choices=[item.value for item in ContinuityPurpose], required=True)
    checkpoint.add_argument("--producer-revision", required=True)
    _add_json_flag(checkpoint)
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--descriptor-root", type=Path, required=True)
    verify.add_argument("--checkpoint-root", type=Path, required=True)
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--state-root", type=Path, required=True)
    verify.add_argument("--source-policy", type=Path, required=True)
    verify.add_argument("--candidate-evidence", type=Path, required=True)
    verify.add_argument("--actor-id", required=True)
    verify.add_argument("--purpose", choices=[item.value for item in ContinuityPurpose], required=True)
    _add_json_flag(verify)
    inventory = sub.add_parser("snapshot-inventory")
    inventory.add_argument("--continuity-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path)
    _add_json_flag(inventory)
    migration = sub.add_parser("migrate-legacy")
    migration.add_argument("--legacy-state", type=Path, required=True)
    migration.add_argument("--persona-id", required=True)
    migration.add_argument("--state-root", type=Path, required=True)
    migration.add_argument("--output-root", type=Path, required=True)
    _add_json_flag(migration)
    switch_plan = sub.add_parser("switch-plan")
    switch_plan.add_argument("--receipt", type=Path, required=True)
    _add_json_flag(switch_plan)
    for name in ("switch-apply", "switch-rollback"):
        child = sub.add_parser(name)
        _add_json_flag(child)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "generate-schemas":
            written = write_continuity_schema_files(args.repo_root)
            _write_json_stdout({"status": "ok", "written": list(written)})
            return 0
        if args.command == "resolve":
            manifest = load_continuity_bundle(args.manifest, descriptor_root=args.descriptor_root)
            policy = load_source_policy_catalog(args.source_policy)
            purpose = ContinuityPurpose(args.purpose)
            actor = policy.actor_context(args.actor_id, purpose)
            store = ContinuitySourceStore(repo_root=args.repo_root, state_root=args.state_root, source_policy=policy)
            view = ContinuityResolver(store, checkpoint_root=args.checkpoint_root).resolve(manifest, purpose=args.purpose, actor=actor, as_of=datetime.now(timezone.utc))
            if args.output:
                _atomic_write_secure(args.output, _json_bytes(_plain(view)))
            _write_json_stdout(view)
            return 0
        if args.command == "checkpoint":
            view = load_resolved_view(args.view)
            content = _load_json_dataclass(args.content, "content")
            boundary = _load_json_dataclass(args.boundary, "boundary")
            observation = _load_json_dataclass(args.runtime_observation, "runtime")
            policy = load_source_policy_catalog(args.source_policy)
            purpose = ContinuityPurpose(args.purpose)
            actor = policy.actor_context(args.actor_id, purpose)
            assert isinstance(content, CheckpointContent) and isinstance(boundary, CheckpointBoundary) and isinstance(observation, RuntimeObservation)
            receipt = ContinuityResolver(None, checkpoint_root=args.checkpoint_root, source_policy=policy).checkpoint(view, boundary_id=boundary.boundary_id, runtime_observation=observation, content=content, boundary=boundary, producer_revision=args.producer_revision, actor=actor, purpose=args.purpose)
            _write_json_stdout(receipt)
            return 0
        if args.command == "verify":
            receipt_obj = _parse_json_bytes(_read_regular_file(args.receipt, max_bytes=MAX_ENVELOPE_BYTES, expected_uid=os.getuid()), where=str(args.receipt))
            receipt = _receipt_from_obj(receipt_obj)
            manifest = load_continuity_bundle(args.manifest, descriptor_root=args.descriptor_root)
            policy = load_source_policy_catalog(args.source_policy)
            purpose = ContinuityPurpose(args.purpose)
            actor = policy.actor_context(args.actor_id, purpose)
            store = ContinuitySourceStore(repo_root=args.repo_root, state_root=args.state_root, source_policy=policy)
            candidate = _load_json_dataclass(args.candidate_evidence, "candidate")
            assert isinstance(candidate, CandidateVerificationEvidence)
            result = ContinuityResolver(store, checkpoint_root=args.checkpoint_root).verify_checkpoint(receipt, manifest, candidate_evidence=candidate, actor=actor, purpose=args.purpose)
            _write_json_stdout(result)
            return 0 if result.status == "ready" else 1
        if args.command == "snapshot-inventory":
            inventory = build_snapshot_export_inventory(args.continuity_root)
            if args.output:
                write_snapshot_export_inventory(inventory, args.output)
            _write_json_stdout(inventory)
            return 0
        if args.command == "migrate-legacy":
            receipt = migrate_legacy_session_state(args.legacy_state, persona_id=args.persona_id, state_root=args.state_root, output_root=args.output_root)
            _write_json_stdout(receipt)
            return 0
        if args.command == "switch-plan":
            receipt = validate_switch_receipt_file(args.receipt)
            _write_json_stdout({"status": "valid", "receipt_sha256": receipt.receipt_sha256, "outcome": receipt.outcome, "side_effects": False})
            return 0
        if args.command in {"switch-apply", "switch-rollback"}:
            _write_json_stdout({"status": "refused", "code": "delegated-to-t3177", "delegated_to": "T-3177", "side_effects": False})
            return 78
        raise ContinuityError("unknown-command", str(args.command))
    except ContinuityError as exc:
        _write_json_stdout({"status": exc.status, "code": exc.code, "detail": exc.detail})
        print(f"{exc.code}: {exc.detail}"[:4096], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
