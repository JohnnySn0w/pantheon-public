#!/usr/bin/env python3
"""Manifest-driven, fail-closed Pantheon persona installer.

The installer is deliberately separate from the runtime harness adapter.  It
builds a typed, digest-addressed plan and applies it through a backend that
implements known operation kinds.  There is no generic shell operation.

Production activation is intentionally conservative: the local backend only
operates on the current Unix account and requires a reachable user-systemd
manager.  Cross-account provisioning remains an explicit operator action.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_SCHEMA = "pantheon-persona-install-plan/v1"
RECEIPT_SCHEMA = "pantheon-persona-install-receipt/v1"
MANAGED_MARKER = "Managed by pantheon persona installer"
LAYER_NAMES = {
    0: "host-substrate",
    1: "connective-substrate",
    2: "harness-runtime",
    3: "persona-overlay",
    4: "policy-governance",
}

PrivilegeRequirement = Literal[
    "unprivileged",
    "privileged-helper",
    "operator-required",
]
OperationKind = Literal[
    "identity-check",
    "source-check",
    "harness-check",
    "brief-check",
    "policy-check",
    "mkdir",
    "atomic-write",
    "user-unit-install",
    "manager-check",
    "daemon-reload",
    "direct-fire",
    "unit-fire",
    "mailbox-probe",
    "timer-enable",
    "timer-observe",
]


class InstallerError(RuntimeError):
    """Base class for a bounded installer failure."""


class ManifestError(InstallerError):
    """The trusted manifest does not satisfy the installer schema."""


class PlanRefused(InstallerError):
    """Apply or rollback was refused before an unsafe mutation."""


class OperationFailed(InstallerError):
    """A typed operation failed during execution."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _normalize_harness(value: str) -> str:
    aliases = {
        "claude": "claude",
        "claude-code": "claude",
        "codex": "codex",
    }
    try:
        return aliases[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ManifestError(f"unsupported harness {value!r}") from exc


def _manifest_relative_path(value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{field_name} must stay within its declared root")
    return path


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo_root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "unversioned"
    return result.stdout.strip()


def _artifact_digest(repo_root: Path, artifacts: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(artifacts, key=str):
        path = (repo_root / relative).resolve()
        if not path.is_relative_to(repo_root.resolve()) or not path.is_file():
            raise ManifestError(f"framework artifact missing or escaped root: {relative}")
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ActorContext:
    uid: int
    persona: str
    role: str
    instance: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class CredentialReference:
    reference_id: str
    kind: str
    provider: str
    owner: str
    scope: tuple[str, ...]
    rotation_strategy: str
    revocation_plan: str
    location: str | None = None
    direct_use_justification: str | None = None


@dataclass(frozen=True)
class PersonaBinding:
    role: str
    persona: str
    account: str
    configured_harness: str
    credential_refs: tuple[CredentialReference, ...] = ()


@dataclass(frozen=True)
class Manifest:
    path: Path
    version: int
    instance_id: str
    installer: Mapping[str, Any]
    roles: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_bytes(cls, path: Path, raw: bytes) -> "Manifest":
        try:
            document = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ManifestError(f"{path}: cannot load manifest: {exc}") from exc
        if not isinstance(document, dict):
            raise ManifestError(f"{path}: root must be a mapping")
        version = document.get("manifest_version")
        if not isinstance(version, int) or version < 2:
            raise ManifestError(f"{path}: manifest_version must be an integer >= 2")
        instance = document.get("instance")
        roles = document.get("roles")
        installer = document.get("persona_installer")
        if not isinstance(instance, dict) or not isinstance(instance.get("id"), str):
            raise ManifestError(f"{path}: instance.id missing")
        if not isinstance(roles, dict) or not roles:
            raise ManifestError(f"{path}: roles mapping missing")
        if not isinstance(installer, dict):
            raise ManifestError(f"{path}: persona_installer mapping missing")
        if installer.get("schema") != "pantheon-persona-install/v1":
            raise ManifestError(f"{path}: unsupported persona_installer.schema")
        security = installer.get("security")
        if not isinstance(security, dict):
            raise ManifestError(f"{path}: persona_installer.security missing")
        if security.get("credential_mode") != "references-only":
            raise ManifestError(f"{path}: credential mode must be references-only")
        if security.get("prohibit_auth_copy") is not True:
            raise ManifestError(f"{path}: prohibit_auth_copy must be true")
        return cls(
            path=path,
            version=version,
            instance_id=instance["id"],
            installer=installer,
            roles=roles,
        )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ManifestError(f"{path}: cannot load manifest: {exc}") from exc
        return cls.from_bytes(path, raw)

    def binding(self, persona: str) -> PersonaBinding:
        for role, raw in self.roles.items():
            if not isinstance(raw, dict) or raw.get("persona") != persona:
                continue
            account = raw.get("account")
            harness = raw.get("harness")
            if not isinstance(account, str) or not account:
                raise ManifestError(f"{self.path}: {role}.account missing")
            if not isinstance(harness, str) or not harness:
                raise ManifestError(f"{self.path}: {role}.harness missing")
            refs = _parse_credential_refs(raw.get("credential_refs", ()), self.path, role)
            return PersonaBinding(
                role=str(role),
                persona=persona,
                account=account,
                configured_harness=_normalize_harness(harness),
                credential_refs=refs,
            )
        raise ManifestError(f"{self.path}: persona {persona!r} is not bound")


_FORBIDDEN_CREDENTIAL_KEYS = {
    "value",
    "secret",
    "token",
    "password",
    "private_key",
    "contents",
    "auth_file",
    "copy_from",
    "template",
    "shell",
    "command",
}


def _parse_credential_refs(
    raw: Any,
    path: Path,
    role: str,
) -> tuple[CredentialReference, ...]:
    if raw in (None, ()):
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{path}: {role}.credential_refs must be a list")
    result: list[CredentialReference] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{path}: {role}.credential_refs[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where} must be a mapping")
        forbidden = sorted(_FORBIDDEN_CREDENTIAL_KEYS.intersection(item))
        if forbidden:
            raise ManifestError(f"{where} contains forbidden value/copy keys: {forbidden}")
        required = ("id", "kind", "provider", "owner", "scope", "rotation")
        missing = [key for key in required if key not in item]
        if missing:
            raise ManifestError(f"{where} missing {missing}")
        scope = item["scope"]
        rotation = item["rotation"]
        if not isinstance(scope, list) or not scope or not all(
            isinstance(entry, str) and entry for entry in scope
        ):
            raise ManifestError(f"{where}.scope must be a non-empty string list")
        if not isinstance(rotation, dict):
            raise ManifestError(f"{where}.rotation must be a mapping")
        strategy = rotation.get("strategy")
        revoke = rotation.get("revoke")
        if not isinstance(strategy, str) or not strategy:
            raise ManifestError(f"{where}.rotation.strategy missing")
        if not isinstance(revoke, str) or not revoke:
            raise ManifestError(f"{where}.rotation.revoke missing")
        provider = item["provider"]
        justification = item.get("direct_use_justification")
        if provider != "broker" and not (
            isinstance(justification, str) and justification.strip()
        ):
            raise ManifestError(
                f"{where}: direct provider requires direct_use_justification"
            )
        string_fields = ("id", "kind", "provider", "owner")
        if not all(isinstance(item[key], str) and item[key] for key in string_fields):
            raise ManifestError(f"{where}: id/kind/provider/owner must be non-empty strings")
        if item["id"] in seen_ids:
            raise ManifestError(f"{where}: duplicate credential reference id")
        seen_ids.add(item["id"])
        location = item.get("location")
        if location is not None and not isinstance(location, str):
            raise ManifestError(f"{where}.location must be a string")
        result.append(
            CredentialReference(
                reference_id=item["id"],
                kind=item["kind"],
                provider=provider,
                owner=item["owner"],
                scope=tuple(scope),
                rotation_strategy=strategy,
                revocation_plan=revoke,
                location=location,
                direct_use_justification=justification,
            )
        )
    return tuple(result)


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


@dataclass(frozen=True)
class ApplyReceipt:
    schema: str
    receipt_id: str
    phase: Literal["staged", "complete", "failed"]
    plan_digest: str
    instance: str
    persona: str
    role: str
    account: str
    actor: Mapping[str, str]
    mutations: tuple[MutationRecord, ...]
    evidence: tuple[OperationEvidence, ...]
    created_at: str

    def to_dict(self) -> Mapping[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplyReceipt":
        return cls(
            schema=raw["schema"],
            receipt_id=raw["receipt_id"],
            phase=raw.get("phase", "complete"),
            plan_digest=raw["plan_digest"],
            instance=raw["instance"],
            persona=raw["persona"],
            role=raw["role"],
            account=raw["account"],
            actor=raw["actor"],
            mutations=tuple(MutationRecord(**item) for item in raw["mutations"]),
            evidence=tuple(OperationEvidence(**item) for item in raw["evidence"]),
            created_at=raw["created_at"],
        )


@dataclass(frozen=True)
class ApplyReport:
    status: Literal["applied", "noop", "refused", "failed"]
    changed: int
    receipt: ApplyReceipt | None
    errors: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyLayer:
    layer: int
    name: str
    ok: bool
    checks: tuple[OperationEvidence, ...]


@dataclass(frozen=True)
class VerifyReport:
    status: Literal["verified", "failed"]
    layers: tuple[VerifyLayer, ...]
    provenance: Mapping[str, str]
    evidence_scope: str
    live_acceptance: bool


@dataclass(frozen=True)
class RollbackReport:
    status: Literal["rolled-back", "noop", "refused", "failed"]
    restored: int
    errors: tuple[str, ...] = ()


class HostBackend(Protocol):
    evidence_scope: str

    def preflight(self, operation: InstallOperation) -> PreflightResult: ...

    def execute(
        self,
        operation: InstallOperation,
    ) -> tuple[MutationRecord, OperationEvidence]: ...

    def verify_operation(self, operation: InstallOperation) -> OperationEvidence: ...

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult: ...

    def rollback_mutation(self, mutation: MutationRecord) -> None: ...

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None: ...

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None: ...


def _provenance(
    manifest: Manifest,
    binding: PersonaBinding,
    selected_harness: str,
    capability: str = "",
) -> dict[str, str]:
    result = {
        "manifest": str(manifest.path),
        "manifest_version": str(manifest.version),
        "instance": manifest.instance_id,
        "role": binding.role,
        "persona": binding.persona,
        "account": binding.account,
        "configured_harness": binding.configured_harness,
        "selected_harness": selected_harness,
        "source_revision": str(
            manifest.installer.get("framework", {}).get("revision", "unknown")
        ),
    }
    if capability:
        result["required_capability"] = capability
    return result


def _rooted(root: Path, absolute: Path) -> Path:
    if not absolute.is_absolute():
        raise ManifestError(f"installer target must be absolute: {absolute}")
    if root == Path("/"):
        return absolute
    return root / absolute.relative_to("/")


def _content_operation(
    *,
    operation_id: str,
    layer: int,
    kind: OperationKind,
    target: Path,
    content: str,
    owner: str,
    privilege: PrivilegeRequirement,
    provenance: Mapping[str, str],
) -> InstallOperation:
    return InstallOperation(
        operation_id=operation_id,
        layer=layer,
        kind=kind,
        target=str(target),
        precondition={"state": "absent-matching-or-managed"},
        desired={
            "content": content,
            "sha256": _sha256_bytes(content.encode("utf-8")),
            "mode": "0644",
        },
        inverse={"state": "receipt-snapshot"},
        privilege=privilege,
        owner=owner,
        provenance=provenance,
    )


class PersonaInstaller:
    def __init__(self, repo_root: Path = REPO_ROOT):
        self.repo_root = repo_root.resolve()

    def plan(
        self,
        manifest: Manifest,
        persona: str,
        harness_override: str | None = None,
        root: Path = Path("/"),
    ) -> InstallPlan:
        binding = manifest.binding(persona)
        selected_harness = _normalize_harness(
            harness_override or binding.configured_harness
        )
        root = root.resolve()
        config = manifest.installer
        paths = config.get("paths")
        mailbox = config.get("mailbox")
        systemd = config.get("systemd")
        policy = config.get("policy")
        lifecycle = config.get("lifecycle")
        if not all(isinstance(item, dict) for item in (paths, mailbox, systemd, policy, lifecycle)):
            raise ManifestError(f"{manifest.path}: installer sections missing")

        home = _rooted(root, Path("/home") / binding.account)
        repo_relative = _manifest_relative_path(
            paths.get("repo"),
            "persona_installer.paths.repo",
        )
        unit_relative = _manifest_relative_path(
            paths.get("unit_dir"),
            "persona_installer.paths.unit_dir",
        )
        state_relative = _manifest_relative_path(
            paths.get("state_dir"),
            "persona_installer.paths.state_dir",
        )
        unit_dir = home / unit_relative
        state_dir = home / state_relative
        mailbox_path = mailbox.get("hub")
        if not isinstance(mailbox_path, str) or not Path(mailbox_path).is_absolute():
            raise ManifestError("persona_installer.mailbox.hub must be absolute")
        mailbox_hub = _rooted(root, Path(mailbox_path))
        probe_peer = str(mailbox.get("probe_peer", "orchestrator"))
        if probe_peer == binding.persona:
            probe_peer = "recovery"
        service_relative = _manifest_relative_path(
            systemd.get("service_template"),
            "persona_installer.systemd.service_template",
        )
        timer_relative = _manifest_relative_path(
            systemd.get("timer_template"),
            "persona_installer.systemd.timer_template",
        )
        service_template = self.repo_root / service_relative
        timer_template = self.repo_root / timer_relative
        for source in (service_template, timer_template):
            if not source.is_file():
                raise ManifestError(f"framework template missing: {source}")
        policy_bundle = tuple(
            _manifest_relative_path(item, "persona_installer.policy.bundle[]")
            for item in policy.get("bundle", ())
        )
        if not policy_bundle:
            raise ManifestError("persona_installer.policy.bundle must not be empty")

        base_provenance = _provenance(manifest, binding, selected_harness)
        artifacts = (
            Path("tools/persona_installer.py"),
            service_relative,
            timer_relative,
        )
        base_provenance = dict(base_provenance) | {
            "declared_revision": base_provenance["source_revision"],
            "source_revision": _git_head(self.repo_root),
            "source_artifact_sha256": _artifact_digest(
                self.repo_root,
                artifacts,
            ),
        }
        unit_banner = (
            f"# {MANAGED_MARKER}\n"
            f"# instance={manifest.instance_id} role={binding.role} "
            f"persona={binding.persona} harness={selected_harness} "
            f"revision={base_provenance['source_revision']}\n"
        )
        service_content = unit_banner + service_template.read_text(encoding="utf-8")
        timer_content = unit_banner + timer_template.read_text(encoding="utf-8")
        binding_content = json.dumps(
            {
                "managed_by": "pantheon-persona-installer",
                "schema": "pantheon-persona-binding/v1",
                "instance": manifest.instance_id,
                "role": binding.role,
                "persona": binding.persona,
                "account": binding.account,
                "configured_harness": binding.configured_harness,
                "selected_harness": selected_harness,
                "credential_refs": [
                    {
                        "id": ref.reference_id,
                        "kind": ref.kind,
                        "provider": ref.provider,
                        "owner": ref.owner,
                        "scope": list(ref.scope),
                        "location": ref.location,
                        "rotation": {
                            "strategy": ref.rotation_strategy,
                            "revoke": ref.revocation_plan,
                        },
                        "direct_use_justification": ref.direct_use_justification,
                    }
                    for ref in binding.credential_refs
                ],
                "provenance": base_provenance,
            },
            sort_keys=True,
            indent=2,
        ) + "\n"

        identity_cap = f"host.identity:{binding.account}"
        fs_cap = f"host.files:{binding.account}"
        manager_cap = f"systemd.user:{binding.account}"
        mailbox_cap = f"mailbox.probe:{binding.account}"

        def op(
            operation_id: str,
            layer: int,
            kind: OperationKind,
            target: str,
            desired: Mapping[str, str],
            privilege: PrivilegeRequirement,
            capability: str = "",
            inverse: Mapping[str, str] | None = None,
        ) -> InstallOperation:
            return InstallOperation(
                operation_id=operation_id,
                layer=layer,
                kind=kind,
                target=target,
                precondition={"state": "must-pass"},
                desired=desired,
                inverse=inverse,
                privilege=privilege,
                owner=binding.account,
                provenance=_provenance(
                    manifest,
                    binding,
                    selected_harness,
                    capability,
                ),
            )

        operations: list[InstallOperation] = [
            op(
                "host.identity",
                0,
                "identity-check",
                binding.account,
                {
                    "account": binding.account,
                    "mailbox_group": str(mailbox.get("group")),
                    "linger": "enabled",
                    "user_manager": "active",
                },
                "operator-required",
                identity_cap,
            ),
            op(
                "framework.source",
                1,
                "source-check",
                str(self.repo_root),
                {
                    "declared_path": str(repo_relative),
                    "revision": base_provenance["source_revision"],
                    "artifact_sha256": base_provenance[
                        "source_artifact_sha256"
                    ],
                    "artifacts": ",".join(str(item) for item in artifacts),
                },
                "unprivileged",
            ),
            op(
                "harness.command",
                2,
                "harness-check",
                selected_harness,
                {"harness": selected_harness},
                "unprivileged",
            ),
            op(
                "persona.brief",
                3,
                "brief-check",
                str(self.repo_root / "pinned-rules" / f"{binding.persona}.md"),
                {"persona": binding.persona},
                "unprivileged",
            ),
            op(
                "policy.bundle",
                4,
                "policy-check",
                str(self.repo_root),
                {
                    "bundle": ",".join(
                        str(item) for item in policy_bundle
                    )
                },
                "unprivileged",
            ),
        ]

        directory_specs = (
            ("connective.config-dir", home / ".config", "0755"),
            ("connective.systemd-dir", home / ".config" / "systemd", "0755"),
            ("connective.unit-dir", unit_dir, "0755"),
            ("connective.local-dir", home / ".local", "0755"),
            ("connective.local-state-dir", home / ".local" / "state", "0755"),
            (
                "connective.pantheon-state-dir",
                home / ".local" / "state" / "pantheon",
                "0700",
            ),
            ("connective.installer-state-dir", state_dir, "0700"),
            ("connective.state-dir", state_dir / binding.persona, "0700"),
            (
                "connective.receipt-dir",
                state_dir / binding.persona / "receipts",
                "0700",
            ),
        )
        seen_directories: set[str] = set()
        for operation_id, directory, mode in directory_specs:
            target = str(directory)
            if target in seen_directories:
                continue
            seen_directories.add(target)
            operations.append(
                op(
                    operation_id,
                    1,
                    "mkdir",
                    target,
                    {"mode": mode},
                    "privileged-helper",
                    fs_cap,
                    {"state": "remove-if-created-and-empty"},
                )
            )

        operations.extend(
            [
                _content_operation(
                    operation_id="persona.binding",
                    layer=3,
                    kind="atomic-write",
                    target=state_dir / binding.persona / "binding.json",
                    content=binding_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="connective.service-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-consume@.service",
                    content=service_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="connective.timer-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-consume@.timer",
                    content=timer_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                op(
                    "host.user-manager",
                    0,
                    "manager-check",
                    binding.account,
                    {"manager_uid": binding.account},
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.daemon-reload",
                    1,
                    "daemon-reload",
                    binding.account,
                    {"timer_state": "disabled"},
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "harness.direct-fire",
                    2,
                    "direct-fire",
                    binding.persona,
                    {
                        "harness": selected_harness,
                        "manifest": str(manifest.path),
                        "repo": str(self.repo_root),
                    },
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.unit-fire",
                    1,
                    "unit-fire",
                    f"pantheon-persona-consume@{binding.persona}.service",
                    {
                        "timer": f"pantheon-persona-consume@{binding.persona}.timer",
                        "timer_state": "disabled",
                        "result": "success",
                        "exec_main_status": "0",
                    },
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.mailbox-probe",
                    1,
                    "mailbox-probe",
                    str(mailbox_hub / binding.persona),
                    {
                        "receive_tmp": str(mailbox_hub / binding.persona / "tmp"),
                        "send_tmp": str(mailbox_hub / probe_peer / "tmp"),
                        "receive": "required",
                        "send": "required",
                        "durable_round_trip": "required",
                    },
                    "operator-required",
                    mailbox_cap,
                ),
                op(
                    "connective.timer-enable",
                    1,
                    "timer-enable",
                    f"pantheon-persona-consume@{binding.persona}.timer",
                    {"after": "direct-fire,unit-fire,mailbox-probe"},
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.timer-observe",
                    1,
                    "timer-observe",
                    f"pantheon-persona-consume@{binding.persona}.timer",
                    {
                        "service": (
                            f"pantheon-persona-consume@{binding.persona}.service"
                        ),
                        "next_elapse": "finite-after-inactive",
                        "timed_fire": "observed",
                        "mailbox_effect": "correlated",
                    },
                    "operator-required",
                    manager_cap,
                ),
            ]
        )
        operations = [
            dataclasses.replace(
                operation,
                provenance=(
                    dict(operation.provenance)
                    | base_provenance
                    | {"install_root": str(root)}
                ),
            )
            for operation in operations
        ]

        actions = [
            OperatorAction(
                action_id="operator.host-identity",
                layer=0,
                target=binding.account,
                reason=(
                    "create/verify the Unix account, linger, user manager, and "
                    "pantheon-mailbox membership without a generic root shell"
                ),
                required_capability=identity_cap,
            ),
            OperatorAction(
                action_id="operator.user-systemd",
                layer=1,
                target=binding.account,
                reason=(
                    "enter the target login/user-manager context and supervise "
                    "disabled first fire before timer activation"
                ),
                required_capability=manager_cap,
            ),
            OperatorAction(
                action_id="operator.mailbox-acl",
                layer=1,
                target=str(mailbox_hub),
                reason="grant and verify both receive and sibling-send ACL directions",
                required_capability=mailbox_cap,
            ),
        ]
        for ref in binding.credential_refs:
            actions.append(
                OperatorAction(
                    action_id=f"operator.credential.{ref.reference_id}",
                    layer=2,
                    target=ref.reference_id,
                    reason=(
                        "resolve provider metadata and verify scope/rotation "
                        "without reading or copying a secret value"
                    ),
                    required_capability=f"credential.check:{ref.reference_id}",
                )
            )

        return InstallPlan.create(
            schema=PLAN_SCHEMA,
            manifest_version=manifest.version,
            instance=manifest.instance_id,
            role=binding.role,
            persona=binding.persona,
            account=binding.account,
            configured_harness=binding.configured_harness,
            selected_harness=selected_harness,
            root=str(root),
            operations=tuple(operations),
            operator_actions=tuple(actions),
            provenance=base_provenance
            | {
                "installer": "tools/persona_installer.py",
                "receipt_dir": str(state_dir / binding.persona / "receipts"),
            },
        )

    def apply(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
    ) -> ApplyReport:
        try:
            self._preflight_apply(plan, approved_digest, backend, actor)
        except PlanRefused as exc:
            return ApplyReport("refused", 0, None, (str(exc),))

        mutations: list[MutationRecord] = []
        evidence: list[OperationEvidence] = []
        activation_kinds = {
            "daemon-reload",
            "direct-fire",
            "unit-fire",
            "mailbox-probe",
            "timer-enable",
            "timer-observe",
        }
        receipt_id = (
            f"{int(time.time())}-{plan.digest[:16]}-{uuid.uuid4().hex[:8]}"
        )
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        staged = False
        pending_intents: tuple[MutationRecord, ...] = ()

        def make_receipt(
            phase: Literal["staged", "complete", "failed"],
            receipt_mutations: Sequence[MutationRecord],
            receipt_evidence: Sequence[OperationEvidence],
        ) -> ApplyReceipt:
            return ApplyReceipt(
                schema=RECEIPT_SCHEMA,
                receipt_id=receipt_id,
                phase=phase,
                plan_digest=plan.digest,
                instance=plan.instance,
                persona=plan.persona,
                role=plan.role,
                account=plan.account,
                actor={
                    "uid": str(actor.uid),
                    "persona": actor.persona,
                    "role": actor.role,
                    "instance": actor.instance,
                    "capabilities": ",".join(sorted(actor.capabilities)),
                },
                mutations=tuple(receipt_mutations),
                evidence=tuple(receipt_evidence),
                created_at=created_at,
            )

        try:
            for operation in plan.operations:
                if not staged and operation.kind in activation_kinds:
                    pending_intents = tuple(
                        intent
                        for planned in plan.operations
                        if planned.kind in activation_kinds
                        for intent in (backend.rollback_intent(planned),)
                        if intent is not None
                    )
                    staged_receipt = make_receipt(
                        "staged",
                        tuple(mutations) + pending_intents,
                        evidence,
                    )
                    backend.store_receipt(plan, staged_receipt)
                    staged = True
                mutation, observed = backend.execute(operation)
                mutations.append(mutation)
                evidence.append(observed)
                if not observed.ok:
                    raise OperationFailed(
                        f"{operation.operation_id}: {observed.detail}"
                    )
            receipt = make_receipt("complete", mutations, evidence)
            backend.store_receipt(plan, receipt)
        except Exception as exc:
            returned_ids = {item.operation_id for item in mutations}
            effective_mutations = tuple(mutations) + tuple(
                item
                for item in pending_intents
                if item.operation_id not in returned_ids
            )
            rollback_notes: list[str] = []
            for mutation in reversed(
                [item for item in effective_mutations if item.changed]
            ):
                try:
                    check = backend.can_rollback(mutation)
                    if not check.ok:
                        rollback_notes.append(
                            f"{mutation.operation_id}: rollback refused: {check.detail}"
                        )
                        continue
                    backend.rollback_mutation(mutation)
                    rollback_notes.append(f"{mutation.operation_id}: restored")
                except Exception as rollback_exc:
                    rollback_notes.append(
                        f"{mutation.operation_id}: rollback failed: {rollback_exc}"
                    )
            failed_receipt = (
                make_receipt("failed", effective_mutations, evidence)
                if staged
                else None
            )
            if failed_receipt is not None:
                try:
                    backend.store_receipt(plan, failed_receipt)
                except Exception as receipt_exc:
                    rollback_notes.append(
                        f"failed receipt persistence: {receipt_exc}"
                    )
            return ApplyReport(
                "failed",
                sum(item.changed for item in effective_mutations),
                failed_receipt,
                (f"{type(exc).__name__}: {exc}",),
                tuple(rollback_notes),
            )
        changed = sum(item.changed for item in mutations)
        return ApplyReport("applied" if changed else "noop", changed, receipt)

    def _preflight_apply(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
    ) -> None:
        if plan.schema != PLAN_SCHEMA:
            raise PlanRefused(f"unsupported plan schema {plan.schema!r}")
        if not plan.validate_digest():
            raise PlanRefused("plan payload does not match its embedded digest")
        if approved_digest != plan.digest:
            raise PlanRefused("approved digest does not match the plan")
        if actor.instance != plan.instance:
            raise PlanRefused("actor instance does not match plan instance")
        for action in plan.operator_actions:
            if action.required_capability not in actor.capabilities:
                raise PlanRefused(
                    f"{action.action_id}: operator action is incomplete or actor "
                    f"lacks capability {action.required_capability!r}"
                )
        for operation in plan.operations:
            capability = operation.provenance.get("required_capability", "")
            if operation.privilege != "unprivileged" and capability not in actor.capabilities:
                raise PlanRefused(
                    f"{operation.operation_id}: actor lacks capability {capability!r}"
                )
            result = backend.preflight(operation)
            if not result.ok:
                raise PlanRefused(
                    f"{operation.operation_id}: {result.classification}: {result.detail}"
                )

    def verify(
        self,
        plan: InstallPlan,
        backend: HostBackend,
    ) -> VerifyReport:
        checks = tuple(backend.verify_operation(item) for item in plan.operations)
        layers = tuple(
            VerifyLayer(
                layer=layer,
                name=LAYER_NAMES[layer],
                ok=all(item.ok for item in checks if item.layer == layer),
                checks=tuple(item for item in checks if item.layer == layer),
            )
            for layer in sorted(LAYER_NAMES)
        )
        all_ok = all(item.ok for item in checks)
        by_id = {item.operation_id: item for item in checks}
        unit_evidence = by_id.get("connective.unit-fire")
        mailbox_evidence = by_id.get("connective.mailbox-probe")
        timer_evidence = by_id.get("connective.timer-observe")
        acceptance_evidence = (
            unit_evidence is not None
            and unit_evidence.evidence.get("result") == "success"
            and unit_evidence.evidence.get("exec_main_status") == "0"
            and mailbox_evidence is not None
            and mailbox_evidence.evidence.get("receive") == "true"
            and mailbox_evidence.evidence.get("send") == "true"
            and mailbox_evidence.evidence.get("durable_round_trip") == "true"
            and timer_evidence is not None
            and timer_evidence.evidence.get("next_elapse_finite") == "true"
            and timer_evidence.evidence.get("timed_fire_observed") == "true"
            and timer_evidence.evidence.get("mailbox_effect_correlated") == "true"
            and bool(timer_evidence.evidence.get("mailbox_probe_sha256"))
        )
        live_acceptance = (
            all_ok
            and backend.evidence_scope == "live"
            and acceptance_evidence
        )
        return VerifyReport(
            status="verified" if all_ok else "failed",
            layers=layers,
            provenance=plan.provenance,
            evidence_scope=backend.evidence_scope,
            live_acceptance=live_acceptance,
        )

    def rollback(
        self,
        receipt: ApplyReceipt,
        backend: HostBackend,
        actor: ActorContext,
    ) -> RollbackReport:
        if receipt.schema != RECEIPT_SCHEMA:
            return RollbackReport("refused", 0, ("unsupported receipt schema",))
        if actor.instance != receipt.instance:
            return RollbackReport("refused", 0, ("actor instance mismatch",))
        capability = f"host.files:{receipt.account}"
        if capability not in actor.capabilities:
            return RollbackReport(
                "refused",
                0,
                (f"actor lacks capability {capability!r}",),
            )
        changed = tuple(item for item in receipt.mutations if item.changed)
        failures = []
        for mutation in changed:
            result = backend.can_rollback(mutation)
            if not result.ok:
                failures.append(
                    f"{mutation.operation_id}: {result.classification}: {result.detail}"
                )
        if failures:
            return RollbackReport("refused", 0, tuple(failures))
        restored = 0
        try:
            for mutation in reversed(changed):
                backend.rollback_mutation(mutation)
                restored += 1
        except Exception as exc:
            return RollbackReport("failed", restored, (str(exc),))
        return RollbackReport("rolled-back" if restored else "noop", restored)


def _snapshot(path: Path) -> dict[str, str]:
    if not path.exists() and not path.is_symlink():
        return {"state": "absent"}
    if path.is_dir():
        return {"state": "directory", "mode": f"{path.stat().st_mode & 0o7777:04o}"}
    if path.is_file():
        content = path.read_bytes()
        return {
            "state": "file",
            "content_b64": base64.b64encode(content).decode("ascii"),
            "fingerprint": _sha256_bytes(content),
            "mode": f"{path.stat().st_mode & 0o7777:04o}",
        }
    return {"state": "other"}


def _snapshot_fingerprint(snapshot: Mapping[str, str]) -> str | None:
    state = snapshot.get("state")
    if state == "absent":
        return None
    if state == "directory":
        return "directory"
    return snapshot.get("fingerprint") or state


def _desired_content(operation: InstallOperation) -> bytes:
    value = operation.desired.get("content")
    if value is None:
        raise OperationFailed(f"{operation.operation_id}: content missing")
    return value.encode("utf-8")


def _finite_next_elapse_basis(values: Mapping[str, str]) -> str | None:
    """Return the systemd clock basis with a finite scheduled next elapse."""

    invalid = {"", "0", "n/a", "infinity"}
    for basis, key in (
        ("realtime", "NextElapseUSecRealtime"),
        ("monotonic", "NextElapseUSecMonotonic"),
    ):
        if values.get(key, "").strip().lower() not in invalid:
            return basis
    return None


def _is_installer_managed(content: str) -> bool:
    lines = content.splitlines()
    if lines and lines[0] == f"# {MANAGED_MARKER}":
        return True
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(raw, dict)
        and raw.get("managed_by") == "pantheon-persona-installer"
    )


class SimulatedHostBackend:
    """Deterministic same-process backend for ordering and rollback proofs."""

    def __init__(
        self,
        *,
        evidence_scope: str = "fixture",
        fail_operation: str | None = None,
        unavailable_kinds: Sequence[str] = (),
        mailbox_send: bool = True,
        mailbox_receive: bool = True,
        finite_next_elapse: bool = True,
        observed_timed_fire: bool = True,
    ):
        self.evidence_scope = evidence_scope
        self.fail_operation = fail_operation
        self.unavailable_kinds = frozenset(unavailable_kinds)
        self.mailbox_send = mailbox_send
        self.mailbox_receive = mailbox_receive
        self.finite_next_elapse = finite_next_elapse
        self.observed_timed_fire = observed_timed_fire
        self.entries: dict[str, dict[str, str]] = {}
        self.events: list[str] = []
        self.receipts: dict[str, Mapping[str, Any]] = {}
        self.receipt_events: list[str] = []
        self.timer_enabled = False
        self.converged = False

    def seed_file(self, target: str, content: str, mode: str = "0644") -> None:
        raw = content.encode("utf-8")
        self.entries[target] = {
            "state": "file",
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "fingerprint": _sha256_bytes(raw),
            "mode": mode,
        }

    def drift(self, target: str, content: str) -> None:
        self.seed_file(target, content)

    def _snapshot(self, target: str) -> dict[str, str]:
        return dict(self.entries.get(target, {"state": "absent"}))

    def preflight(self, operation: InstallOperation) -> PreflightResult:
        if operation.kind in self.unavailable_kinds:
            return PreflightResult(False, "unavailable", "backend kind unavailable")
        if operation.kind == "mkdir":
            state = self._snapshot(operation.target)
            if state["state"] in {"absent", "directory"}:
                return PreflightResult(True, state["state"], "safe directory state")
            return PreflightResult(False, "conflicting", "non-directory exists")
        if operation.kind in {"atomic-write", "user-unit-install"}:
            state = self._snapshot(operation.target)
            if state["state"] == "absent":
                return PreflightResult(True, "absent", "will create managed file")
            if state["state"] != "file":
                return PreflightResult(False, "conflicting", "target is not a regular file")
            desired = operation.desired["sha256"]
            if state.get("fingerprint") == desired:
                return PreflightResult(True, "matching", "desired content already installed")
            content = base64.b64decode(state["content_b64"]).decode("utf-8", "replace")
            if _is_installer_managed(content):
                return PreflightResult(True, "managed", "installer-owned file may converge")
            return PreflightResult(False, "conflicting", "unknown regular file")
        return PreflightResult(True, "check", "typed operation available")

    def execute(
        self,
        operation: InstallOperation,
    ) -> tuple[MutationRecord, OperationEvidence]:
        if operation.operation_id == self.fail_operation:
            raise OperationFailed("injected operation failure")
        if operation.operation_id == "host.identity" and self.events:
            self.events.clear()
        before = self._snapshot(operation.target)
        changed = False
        if operation.kind == "mkdir":
            if before["state"] == "absent":
                self.entries[operation.target] = {
                    "state": "directory",
                    "mode": operation.desired.get("mode", "0755"),
                }
                changed = True
        elif operation.kind in {"atomic-write", "user-unit-install"}:
            raw = _desired_content(operation)
            if before.get("fingerprint") != _sha256_bytes(raw):
                self.entries[operation.target] = {
                    "state": "file",
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "fingerprint": _sha256_bytes(raw),
                    "mode": operation.desired.get("mode", "0644"),
                }
                changed = True
        if operation.kind == "timer-enable":
            before = {
                "state": "enabled" if self.timer_enabled else "disabled",
            }
            changed = not self.timer_enabled
            self.timer_enabled = True
        self.events.append(operation.operation_id)
        ok = True
        detail = "typed operation passed"
        observed: dict[str, str] = {}
        if operation.kind == "unit-fire":
            ok = self.converged or "connective.timer-enable" not in self.events
            detail = "oneshot inactive with Result=success and ExecMainStatus=0"
            observed = {
                "active_state": "inactive",
                "result": "success",
                "exec_main_status": "0",
                "timer_enabled": "false",
            }
        elif operation.kind == "mailbox-probe":
            ok = self.mailbox_send and self.mailbox_receive
            detail = (
                "mailbox receive/send durable round trip passed"
                if ok
                else "mailbox ACL direction failed"
            )
            observed = {
                "receive": str(self.mailbox_receive).lower(),
                "send": str(self.mailbox_send).lower(),
                "durable_round_trip": str(ok).lower(),
                "sha256": _sha256_bytes(b"simulated-mailbox-round-trip"),
            }
        elif operation.kind == "timer-enable":
            prerequisites = {
                "harness.direct-fire",
                "connective.unit-fire",
                "connective.mailbox-probe",
            }
            ok = prerequisites.issubset(self.events)
            detail = (
                "timer enabled after all first-fire gates"
                if ok
                else "timer ordering gate failed"
            )
        elif operation.kind == "timer-observe":
            ok = self.finite_next_elapse and self.observed_timed_fire
            mailbox_probe = next(
                (
                    item
                    for item in reversed(self.events)
                    if item == "connective.mailbox-probe"
                ),
                "",
            )
            detail = (
                "finite next elapse and correlated timed fire observed"
                if ok
                else "timer observation incomplete"
            )
            observed = {
                "next_elapse_finite": str(self.finite_next_elapse).lower(),
                "timed_fire_observed": str(self.observed_timed_fire).lower(),
                "mailbox_effect_correlated": str(
                    self.observed_timed_fire and bool(mailbox_probe)
                ).lower(),
                "mailbox_probe_sha256": _sha256_bytes(
                    b"simulated-mailbox-round-trip"
                ),
            }
            if ok:
                self.converged = True
        after = self._snapshot(operation.target)
        after_fingerprint = _snapshot_fingerprint(after)
        if operation.kind == "timer-enable":
            after_fingerprint = "timer-enabled"
        return (
            MutationRecord(
                operation_id=operation.operation_id,
                kind=operation.kind,
                target=operation.target,
                owner=operation.owner,
                changed=changed,
                before=before,
                after_fingerprint=after_fingerprint,
            ),
            OperationEvidence(
                operation_id=operation.operation_id,
                layer=operation.layer,
                kind=operation.kind,
                ok=ok,
                detail=detail,
                evidence=observed,
            ),
        )

    def verify_operation(self, operation: InstallOperation) -> OperationEvidence:
        ok = operation.operation_id in self.events
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install"}:
            result = self.preflight(operation)
            ok = result.ok and result.classification in {"directory", "matching"}
        return OperationEvidence(
            operation_id=operation.operation_id,
            layer=operation.layer,
            kind=operation.kind,
            ok=ok,
            detail=(
                "verified from deterministic backend state"
                if ok
                else "operation evidence absent"
            ),
        )

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.kind == "timer-enable":
            before_enabled = mutation.before.get("state") == "enabled"
            if self.timer_enabled == before_enabled:
                return PreflightResult(
                    True,
                    "not-applied",
                    "timer still matches the write-ahead state",
                )
            if not self.timer_enabled:
                return PreflightResult(False, "drifted", "timer is no longer enabled")
            return PreflightResult(True, "owned", "timer enable state matches receipt")
        current = _snapshot_fingerprint(self._snapshot(mutation.target))
        before = _snapshot_fingerprint(mutation.before)
        if current == before:
            return PreflightResult(
                True,
                "not-applied",
                "target already matches the pre-apply receipt state",
            )
        if current != mutation.after_fingerprint:
            return PreflightResult(False, "drifted", "post-apply fingerprint changed")
        return PreflightResult(True, "owned", "post-apply fingerprint matches receipt")

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if mutation.kind == "timer-enable":
            self.timer_enabled = mutation.before.get("state") == "enabled"
            self.converged = False
            self.events.append(f"rollback:{mutation.operation_id}")
            return
        before = dict(mutation.before)
        if before.get("state") == "absent":
            self.entries.pop(mutation.target, None)
        else:
            self.entries[mutation.target] = before
        self.events.append(f"rollback:{mutation.operation_id}")

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable":
            return None
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=not self.timer_enabled,
            before={"state": "enabled" if self.timer_enabled else "disabled"},
            after_fingerprint="timer-enabled",
        )

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        self.receipts[receipt.receipt_id] = receipt.to_dict()
        self.receipt_events.append(f"receipt:{receipt.phase}")
        self.events.append(f"receipt:{receipt.phase}")


class LocalHostBackend:
    """Fixed-argv backend for the current account's real user manager.

    This backend never changes Unix users, groups, ACLs, credentials, or policy.
    Those remain typed operator actions.  It refuses a target account other than
    the current uid and refuses an unreachable user bus before any mutation.
    """

    evidence_scope = "live"

    def __init__(self, *, command_timeout: int = 30, timed_fire_timeout: int = 180):
        self.command_timeout = command_timeout
        self.timed_fire_timeout = timed_fire_timeout
        self.events: dict[str, OperationEvidence] = {}
        self._baseline_invocation = ""

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout or self.command_timeout,
        )

    def _current_account_only(self, account: str) -> PreflightResult:
        try:
            target = pwd.getpwnam(account)
        except KeyError:
            return PreflightResult(False, "missing", "Unix account does not exist")
        if target.pw_uid != os.geteuid():
            return PreflightResult(
                False,
                "operator-required",
                "local backend must run inside the target account",
            )
        return PreflightResult(True, "current-account", f"uid={target.pw_uid}")

    def _identity_ready(self, operation: InstallOperation) -> PreflightResult:
        account = self._current_account_only(operation.owner)
        if not account.ok:
            return account
        user = pwd.getpwnam(operation.owner)
        group_name = operation.desired["mailbox_group"]
        try:
            group = grp.getgrnam(group_name)
        except KeyError:
            return PreflightResult(
                False,
                "missing",
                f"mailbox group {group_name!r} does not exist",
            )
        group_ids = os.getgrouplist(operation.owner, user.pw_gid)
        if group.gr_gid not in group_ids:
            return PreflightResult(
                False,
                "missing",
                f"{operation.owner} is not a member of {group_name}",
            )
        linger = self._run(
            ("loginctl", "show-user", operation.owner, "-p", "Linger", "--value")
        )
        if linger.returncode != 0 or linger.stdout.strip().lower() != "yes":
            return PreflightResult(False, "inactive", "login linger is not enabled")
        manager = self._run(("systemctl", "is-active", f"user@{user.pw_uid}.service"))
        if manager.returncode != 0 or manager.stdout.strip() != "active":
            return PreflightResult(
                False,
                "inactive",
                f"user@{user.pw_uid}.service is not active",
            )
        return PreflightResult(
            True,
            "identity",
            f"uid={user.pw_uid} group={group_name} linger=yes user-manager=active",
        )

    def _manager_ready(self) -> PreflightResult:
        result = self._run(("systemctl", "--user", "show-environment"))
        if result.returncode != 0:
            return PreflightResult(
                False,
                "unavailable",
                (result.stderr or result.stdout).strip() or "user manager unavailable",
            )
        return PreflightResult(True, "active", "target user manager reachable")

    def preflight(self, operation: InstallOperation) -> PreflightResult:
        if operation.provenance.get("install_root") != "/":
            return PreflightResult(
                False,
                "operator-required",
                "local backend activates only a root=/ plan",
            )
        if operation.kind == "mkdir":
            path = Path(operation.target)
            if not path.exists():
                return PreflightResult(True, "absent", "will create directory")
            if path.is_dir():
                return PreflightResult(True, "directory", "directory already exists")
            return PreflightResult(False, "conflicting", "non-directory exists")
        if operation.kind in {"atomic-write", "user-unit-install"}:
            path = Path(operation.target)
            snap = _snapshot(path)
            if snap["state"] == "absent":
                return PreflightResult(True, "absent", "will create managed file")
            if snap["state"] != "file":
                return PreflightResult(False, "conflicting", "target is not a regular file")
            if snap.get("fingerprint") == operation.desired["sha256"]:
                return PreflightResult(True, "matching", "desired content already installed")
            content = base64.b64decode(snap["content_b64"]).decode("utf-8", "replace")
            if _is_installer_managed(content):
                return PreflightResult(True, "managed", "installer-owned file may converge")
            return PreflightResult(False, "conflicting", "unknown regular file")
        if operation.kind == "identity-check":
            return self._identity_ready(operation)
        if operation.kind == "source-check":
            source = Path(operation.target)
            artifact_paths = tuple(
                Path(item)
                for item in operation.desired.get("artifacts", "").split(",")
                if item
            )
            try:
                ok = (
                    source.is_dir()
                    and _git_head(source) == operation.desired.get("revision")
                    and _artifact_digest(source, artifact_paths)
                    == operation.desired.get("artifact_sha256")
                )
            except ManifestError:
                ok = False
            return PreflightResult(
                ok,
                "source",
                (
                    "framework revision and artifact digest match"
                    if ok
                    else "framework source drifted from reviewed plan"
                ),
            )
        if operation.kind == "brief-check":
            ok = Path(operation.target).is_file()
            return PreflightResult(
                ok,
                "persona",
                "persona brief present" if ok else "persona brief missing",
            )
        if operation.kind == "policy-check":
            bundle = [entry for entry in operation.desired.get("bundle", "").split(",") if entry]
            missing = [entry for entry in bundle if not (Path(operation.target) / entry).exists()]
            return PreflightResult(
                not missing,
                "policy",
                "policy bundle present" if not missing else f"missing {missing}",
            )
        if operation.kind == "harness-check":
            command = "claude" if operation.target == "claude" else "codex"
            ok = shutil.which(command) is not None
            return PreflightResult(
                ok,
                "harness",
                f"{command} found" if ok else f"{command} missing",
            )
        account = self._current_account_only(operation.owner)
        if not account.ok:
            return account
        manager = self._manager_ready()
        if not manager.ok:
            return manager
        if operation.kind == "unit-fire":
            enabled = self._run(
                ("systemctl", "--user", "is-enabled", operation.desired["timer"])
            )
            if enabled.returncode == 0 and "connective.timer-observe" not in self.events:
                return PreflightResult(
                    False,
                    "conflicting",
                    "timer is already enabled without this backend's convergence evidence",
                )
        return manager

    def _write_file(self, operation: InstallOperation) -> MutationRecord:
        path = Path(operation.target)
        before = _snapshot(path)
        raw = _desired_content(operation)
        changed = before.get("fingerprint") != _sha256_bytes(raw)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, int(operation.desired.get("mode", "0644"), 8))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        after = _snapshot(path)
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=changed,
            before=before,
            after_fingerprint=_snapshot_fingerprint(after),
        )

    def _simple_mutation(self, operation: InstallOperation) -> MutationRecord:
        path = Path(operation.target)
        before = _snapshot(path)
        changed = False
        if operation.kind == "mkdir" and before["state"] == "absent":
            path.mkdir()
            os.chmod(path, int(operation.desired.get("mode", "0755"), 8))
            changed = True
        after = _snapshot(path)
        return MutationRecord(
            operation.operation_id,
            operation.kind,
            operation.target,
            operation.owner,
            changed,
            before,
            _snapshot_fingerprint(after),
        )

    def _systemd_show(self, unit: str, properties: Sequence[str]) -> dict[str, str]:
        argv = ["systemctl", "--user", "show", unit]
        for item in properties:
            argv.extend(("-p", item))
        result = self._run(argv)
        if result.returncode != 0:
            raise OperationFailed((result.stderr or result.stdout).strip())
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def _mailbox_probe(self, operation: InstallOperation) -> Mapping[str, str]:
        receive = Path(operation.desired["receive_tmp"])
        send = Path(operation.desired["send_tmp"])
        payload = (
            f"pantheon-installer-probe:{operation.provenance['instance']}:"
            f"{operation.provenance['persona']}:{uuid.uuid4()}"
        ).encode("utf-8")
        digest = _sha256_bytes(payload)
        written: list[Path] = []
        try:
            for directory, direction in ((receive, "receive"), (send, "send")):
                name = f".installer-probe-{uuid.uuid4().hex}"
                path = directory / name
                with path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                written.append(path)
                if _sha256_bytes(path.read_bytes()) != digest:
                    raise OperationFailed(f"mailbox {direction} round-trip hash mismatch")
            return {
                "receive": "true",
                "send": "true",
                "durable_round_trip": "true",
                "sha256": digest,
            }
        finally:
            for path in written:
                path.unlink(missing_ok=True)

    def execute(
        self,
        operation: InstallOperation,
    ) -> tuple[MutationRecord, OperationEvidence]:
        mutation = MutationRecord(
            operation.operation_id,
            operation.kind,
            operation.target,
            operation.owner,
            False,
            {"state": "not-applicable"},
            None,
        )
        observed: dict[str, str] = {}
        if operation.kind == "mkdir":
            mutation = self._simple_mutation(operation)
        elif operation.kind in {"atomic-write", "user-unit-install"}:
            mutation = self._write_file(operation)
        elif operation.kind == "daemon-reload":
            result = self._run(("systemctl", "--user", "daemon-reload"))
            if result.returncode != 0:
                raise OperationFailed((result.stderr or result.stdout).strip())
        elif operation.kind == "direct-fire":
            result = self._run(
                (
                    "python3",
                    str(Path(operation.desired["repo"]) / "tools" / "persona_bringup.py"),
                    "diagnose",
                    "--manifest",
                    operation.desired["manifest"],
                    "--persona",
                    operation.target,
                    "--harness",
                    operation.desired["harness"],
                )
            )
            if result.returncode != 0:
                raise OperationFailed(
                    (result.stderr or result.stdout).strip() or "direct boot smoke failed"
                )
        elif operation.kind == "unit-fire":
            timer = operation.desired["timer"]
            enabled = self._run(("systemctl", "--user", "is-enabled", timer))
            if enabled.returncode == 0 and "connective.timer-observe" in self.events:
                observed = dict(self.events["connective.unit-fire"].evidence)
            elif enabled.returncode == 0:
                raise OperationFailed("timer was enabled before disabled first fire")
            else:
                start = self._run(("systemctl", "--user", "start", operation.target))
                if start.returncode != 0:
                    raise OperationFailed((start.stderr or start.stdout).strip())
                values = self._systemd_show(
                    operation.target,
                    ("ActiveState", "Result", "ExecMainStatus"),
                )
                observed = {
                    "active_state": values.get("ActiveState", ""),
                    "result": values.get("Result", ""),
                    "exec_main_status": values.get("ExecMainStatus", ""),
                    "timer_enabled": "false",
                }
                if observed != {
                    "active_state": "inactive",
                    "result": "success",
                    "exec_main_status": "0",
                    "timer_enabled": "false",
                }:
                    raise OperationFailed(f"disabled first fire failed: {observed}")
        elif operation.kind == "mailbox-probe":
            observed = dict(self._mailbox_probe(operation))
        elif operation.kind == "timer-enable":
            before_enabled = (
                self._run(("systemctl", "--user", "is-enabled", operation.target)).returncode
                == 0
            )
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                not before_enabled,
                {"state": "enabled" if before_enabled else "disabled"},
                "timer-enabled",
            )
            service = operation.target.removesuffix(".timer") + ".service"
            self._baseline_invocation = self._systemd_show(
                service, ("InvocationID",)
            ).get("InvocationID", "")
            if not before_enabled:
                result = self._run(
                    ("systemctl", "--user", "enable", "--now", operation.target)
                )
                if result.returncode != 0:
                    raise OperationFailed((result.stderr or result.stdout).strip())
        elif operation.kind == "timer-observe":
            if "connective.timer-observe" in self.events:
                observed = dict(self.events["connective.timer-observe"].evidence)
                evidence = OperationEvidence(
                    operation.operation_id,
                    operation.layer,
                    operation.kind,
                    True,
                    "existing live convergence evidence retained",
                    observed,
                )
                self.events[operation.operation_id] = evidence
                return mutation, evidence
            service = operation.desired["service"]
            deadline = time.monotonic() + self.timed_fire_timeout
            current = self._baseline_invocation
            while time.monotonic() < deadline:
                values = self._systemd_show(
                    service,
                    ("InvocationID", "ActiveState", "Result", "ExecMainStatus"),
                )
                current = values.get("InvocationID", "")
                if (
                    current
                    and current != self._baseline_invocation
                    and values.get("ActiveState") == "inactive"
                    and values.get("Result") == "success"
                    and values.get("ExecMainStatus") == "0"
                ):
                    break
                time.sleep(1)
            else:
                raise OperationFailed("no successful timed fire observed before timeout")
            timer = self._systemd_show(
                operation.target,
                ("NextElapseUSecRealtime", "NextElapseUSecMonotonic"),
            )
            next_elapse_basis = _finite_next_elapse_basis(timer)
            if next_elapse_basis is None:
                raise OperationFailed("next timer elapse is not finite after inactivity")
            mailbox_probe = self.events.get("connective.mailbox-probe")
            probe_sha = (
                mailbox_probe.evidence.get("sha256", "") if mailbox_probe else ""
            )
            if not probe_sha:
                raise OperationFailed(
                    "timed fire lacks correlated durable mailbox-probe evidence"
                )
            observed = {
                "next_elapse_basis": next_elapse_basis,
                "next_elapse_finite": "true",
                "timed_fire_observed": "true",
                "mailbox_effect_correlated": "true",
                "mailbox_probe_sha256": probe_sha,
                "invocation_id": current,
            }
        detail = "typed local operation passed"
        evidence = OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            True,
            detail,
            observed,
        )
        self.events[operation.operation_id] = evidence
        return mutation, evidence

    def verify_operation(self, operation: InstallOperation) -> OperationEvidence:
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install"}:
            result = self.preflight(operation)
            ok = result.ok and result.classification in {"directory", "matching"}
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                result.detail,
            )
        if operation.operation_id in self.events:
            return self.events[operation.operation_id]
        if operation.kind in {
            "daemon-reload",
            "direct-fire",
            "unit-fire",
            "mailbox-probe",
            "timer-enable",
            "timer-observe",
        }:
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                False,
                "live execution evidence absent",
            )
        result = self.preflight(operation)
        return OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            result.ok,
            result.detail,
        )

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.kind == "timer-enable":
            enabled = (
                self._run(("systemctl", "--user", "is-enabled", mutation.target)).returncode
                == 0
            )
            before_enabled = mutation.before.get("state") == "enabled"
            if enabled == before_enabled:
                return PreflightResult(
                    True,
                    "not-applied",
                    "timer still matches the write-ahead state",
                )
            if not enabled:
                return PreflightResult(False, "drifted", "timer is no longer enabled")
            return PreflightResult(True, "owned", "timer enable state matches receipt")
        current = _snapshot_fingerprint(_snapshot(Path(mutation.target)))
        before = _snapshot_fingerprint(mutation.before)
        if current == before:
            return PreflightResult(
                True,
                "not-applied",
                "target already matches the pre-apply receipt state",
            )
        if current != mutation.after_fingerprint:
            return PreflightResult(False, "drifted", "post-apply fingerprint changed")
        return PreflightResult(True, "owned", "post-apply fingerprint matches receipt")

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if mutation.kind == "timer-enable":
            desired_enabled = mutation.before.get("state") == "enabled"
            argv = (
                ("systemctl", "--user", "enable", "--now", mutation.target)
                if desired_enabled
                else ("systemctl", "--user", "disable", "--now", mutation.target)
            )
            result = self._run(argv)
            if result.returncode != 0:
                raise OperationFailed((result.stderr or result.stdout).strip())
            return
        path = Path(mutation.target)
        before = mutation.before
        if before.get("state") == "absent":
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    # Durable receipts or user-created state are preserved. A
                    # rollback never recursively deletes a non-empty directory.
                    return
            else:
                path.unlink(missing_ok=True)
            return
        if before.get("state") == "directory":
            os.chmod(path, int(before.get("mode", "0755"), 8))
            return
        if before.get("state") == "file":
            raw = base64.b64decode(before["content_b64"])
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, int(before.get("mode", "0644"), 8))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable":
            return None
        enabled = (
            self._run(("systemctl", "--user", "is-enabled", operation.target)).returncode
            == 0
        )
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=not enabled,
            before={"state": "enabled" if enabled else "disabled"},
            after_fingerprint="timer-enabled",
        )

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        directory = Path(plan.provenance["receipt_dir"])
        if not directory.is_dir():
            raise OperationFailed("planned receipt directory is missing")
        path = directory / f"{receipt.receipt_id}.json"
        content = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".receipt.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _all_plan_capabilities(plan: InstallPlan) -> frozenset[str]:
    operation_capabilities = {
        operation.provenance["required_capability"]
        for operation in plan.operations
        if operation.provenance.get("required_capability")
    }
    action_capabilities = {
        action.required_capability for action in plan.operator_actions
    }
    return frozenset(operation_capabilities | action_capabilities)


def _print_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "simulate"):
        child = subparsers.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--persona", required=True)
        child.add_argument("--harness")
        child.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args(argv)
    try:
        manifest = Manifest.load(args.manifest)
        installer = PersonaInstaller()
        plan = installer.plan(
            manifest,
            args.persona,
            harness_override=args.harness,
            root=args.root,
        )
        if args.command == "plan":
            _print_json(plan)
            return 0
        backend = SimulatedHostBackend()
        actor = ActorContext(
            uid=os.geteuid(),
            persona=args.persona,
            role=plan.role,
            instance=plan.instance,
            capabilities=_all_plan_capabilities(plan),
        )
        applied = installer.apply(plan, plan.digest, backend, actor)
        verified = installer.verify(plan, backend)
        _print_json({"plan": plan, "apply": applied, "verify": verified})
        return 0 if applied.status in {"applied", "noop"} and verified.status == "verified" else 1
    except InstallerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
