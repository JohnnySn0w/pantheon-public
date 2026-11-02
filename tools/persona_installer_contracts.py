"""Pure installer contract types (T-5516; ORG-052 \u00a71/\u00a73, T-2652).

The typed plan vocabulary -- ``InstallOperation``, ``InstallPlan``,
``OperatorAction``, ``PreflightResult``, ``MutationRecord``,
``OperationEvidence`` and the installer error bases -- moved verbatim out of
``tools/persona_installer.py`` so the ORG-052 privileged services (broker,
approval service, per-seat executor) can name them without importing the
installer, whose module-level imports pull in the agent-facing Telegram bridge,
the mailbox client and the harness adapter.

Closed module set: this module imports the standard library only (pinned by
``tools/test_host_control_import_closure.py``). ``tools.persona_installer``
re-exports every name, so both import paths yield the same objects.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


PrivilegeRequirement = Literal[
    "unprivileged",
    "privileged-helper",
    "operator-required",
]
ExecutionScope = Literal["all", "staged-cutover"]
Workflow = Literal["all-in-one", "stage-prefix", "staged-activate"]
OperationKind = Literal[
    "identity-check",
    "source-check",
    "harness-check",
    "brief-check",
    "policy-check",
    "mkdir",
    "atomic-write",
    "user-unit-install",
    "seat-periodic-unit-install",
    "seat-periodic-daemon-reload",
    "seat-periodic-timer-enable",
    "seat-periodic-timer-observe",
    "seat-service-enable-observe",
    "seat-service-decommission",
    "seat-claude-settings-env",
    "seat-runtime-residue-quarantine",
    "seat-claude-workspace-trust",
    "manager-check",
    "daemon-reload",
    "stale-unit-failure-reset",
    "direct-fire",
    "unit-fire",
    "mailbox-probe",
    "mailbox-writable-root-check",
    "timer-enablement-absence-check",
    "timer-enable",
    "timer-observe",
    "codex-telegram-source-check",
    "codex-telegram-disabled-first",
    "codex-telegram-mcp-check",
    "codex-standing-instructions-source-check",
    "codex-standing-instructions-install",
    "codex-standing-instructions-target-check",
    "codex-standing-instructions-override-check",
    "claude-mcp-server-check",
    "claude-mcp-server-registration",
]


class InstallerError(RuntimeError):
    """Base class for a bounded installer failure."""


class ManifestError(InstallerError):
    """The trusted manifest does not satisfy the installer schema."""


class OperationFailed(InstallerError):
    """A typed operation failed during execution."""

    def __init__(self, message: str, *, evidence: Mapping[str, str] | None = None):
        super().__init__(message)
        self.evidence = dict(evidence or {})


# ORG-050 "Required installer gates": target-path check. Every absolute operation
# target must be `..`-free and (unless the op only READS the installer's own
# framework checkout) sit under one of the plan's declared allowed roots. The
# roots are the persona account's home plus the mailbox hub (the only place
# existing plans write outside the home: <hub>/<persona>, today
# /home/post-office/mailbox); both are rooted under the plan root and recorded
# in provenance so the digest the operator approves covers them. Lexical check:
# symlinks are not resolved.
ALLOWED_TARGET_ROOTS_KEY = "allowed_target_roots"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _jsonable(value: Any) -> Any:
    operation_cls = globals().get("InstallOperation")
    if operation_cls is not None and isinstance(value, operation_cls):
        result = {
            item.name: _jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.name != "execution_scope"
        }
        if value.execution_scope != "all":
            result["execution_scope"] = value.execution_scope
        return result
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


@dataclass(frozen=True)
class InstallOperation:
    operation_id: str
    layer: int
    kind: OperationKind
    target: str
    precondition: Mapping[str, str]
    desired: Mapping[str, str]
    inverse: Mapping[str, str] | None
    privilege: PrivilegeRequirement
    owner: str
    provenance: Mapping[str, str]
    execution_scope: ExecutionScope = "all"


@dataclass(frozen=True)
class ExecutionContext:
    workflow: Workflow
    # T-4837: the plan's allowed target roots, pinned to the manifest by
    # _preflight_apply and handed to the backend so it can enforce realpath
    # containment before each write. Empty means "none supplied": a backend
    # that needs them (LocalHostBackend) fails closed.
    allowed_roots: tuple[str, ...] = ()


EXECUTION_CONTEXT_ALL_IN_ONE = ExecutionContext("all-in-one")


@dataclass(frozen=True)
class OperatorAction:
    action_id: str
    layer: int
    target: str
    reason: str
    required_capability: str


@dataclass(frozen=True)
class InstallPlan:
    schema: str
    manifest_version: int
    instance: str
    role: str
    persona: str
    account: str
    configured_harness: str
    selected_harness: str
    root: str
    operations: tuple[InstallOperation, ...]
    operator_actions: tuple[OperatorAction, ...]
    provenance: Mapping[str, str]
    digest: str

    def payload(self) -> Mapping[str, Any]:
        value = _jsonable(self)
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, Any]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: Any) -> "InstallPlan":
        candidate = cls(digest="", **kwargs)
        digest = _sha256_bytes(_canonical_json(candidate.payload()).encode("utf-8"))
        return dataclasses.replace(candidate, digest=digest)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InstallPlan":
        operations = tuple(InstallOperation(**item) for item in raw["operations"])
        actions = tuple(OperatorAction(**item) for item in raw["operator_actions"])
        return cls(
            schema=raw["schema"],
            manifest_version=raw["manifest_version"],
            instance=raw["instance"],
            role=raw["role"],
            persona=raw["persona"],
            account=raw["account"],
            configured_harness=raw["configured_harness"],
            selected_harness=raw["selected_harness"],
            root=raw["root"],
            operations=operations,
            operator_actions=actions,
            provenance=raw["provenance"],
            digest=raw["digest"],
        )

    def validate_digest(self) -> bool:
        expected = _sha256_bytes(_canonical_json(self.payload()).encode("utf-8"))
        return expected == self.digest


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    classification: str
    detail: str


@dataclass(frozen=True)
class MutationRecord:
    operation_id: str
    kind: str
    target: str
    owner: str
    changed: bool
    before: Mapping[str, str]
    after_fingerprint: str | None


@dataclass(frozen=True)
class OperationEvidence:
    operation_id: str
    layer: int
    kind: str
    ok: bool
    detail: str
    evidence: Mapping[str, str] = field(default_factory=dict)


# T-5516: the framework.source artifact set -- the installer's own code files, in
# plan order, ahead of the manifest's template ids. The installer digests these
# bytes (``source_artifact_sha256``), the broker binds the list to the registry
# (host_control.contracts.effect_digest_payload) and the executor re-hashes the
# root-owned framework copy, so the moved contract/activation code stays covered.
FRAMEWORK_CODE_ARTIFACTS = (
    "tools/persona_installer.py",
    "tools/persona_installer_contracts.py",
    "tools/persona_installer_activation.py",
    "tools/mcp_allowlist_registry.py",
    "tools/mcp_allowlist.yaml",
)
