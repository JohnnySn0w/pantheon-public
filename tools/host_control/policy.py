"""Strict, exact-match policy for host-control planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tools.host_control.contracts import (
    ApprovalClass,
    CapabilitiesResult,
    CapabilityGrant,
    HostAction,
    ManifestRecord,
    PolicyReasonCode,
    RegistryError,
    SeatRecord,
    SubjectContext,
)
from tools.host_control.trusted_files import RootOwnedFileLoader


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOP_KEYS = frozenset(("schema", "revision", "grants"))
_GRANT_KEYS = frozenset(
    (
        "subject_seat_id",
        "action",
        "target_seat_id",
        "manifest_revisions",
        "operation_families",
        "approval_class",
    )
)
_ACTIONS = frozenset(("plan", "apply", "verify", "rollback"))
_APPROVAL_CLASSES = frozenset(("none", "agent", "human"))


def _exact_object(
    value: object,
    keys: frozenset[str],
    context: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RegistryError(f"invalid {context} fields")
    return value


def _identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RegistryError(f"invalid {context}")
    return value


def _identifier_list(value: object, context: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, str) and _IDENTIFIER.fullmatch(item) is not None
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise RegistryError(f"invalid {context}")
    return tuple(value)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    approval_class: ApprovalClass
    operation_families: tuple[str, ...]
    reason_code: PolicyReasonCode


@dataclass(frozen=True)
class _PolicyGrant:
    subject_seat_id: str
    action: HostAction
    target_seat_id: str
    manifest_revisions: tuple[str, ...]
    operation_families: tuple[str, ...]
    approval_class: ApprovalClass


@dataclass(frozen=True)
class HostPolicy:
    _revision: str
    _grants: tuple[_PolicyGrant, ...]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        files: RootOwnedFileLoader,
    ) -> "HostPolicy":
        raw = files.read_json(path, max_bytes=1024 * 1024)
        _exact_object(raw, _TOP_KEYS, "policy")
        if raw["schema"] != "pantheon-host-policy/v1":
            raise RegistryError("unsupported policy schema")
        revision = _identifier(raw["revision"], "policy revision")
        grants_raw = raw["grants"]
        if not isinstance(grants_raw, list):
            raise RegistryError("policy grants must be a list")
        grants: list[_PolicyGrant] = []
        seen: set[tuple[object, ...]] = set()
        for index, item in enumerate(grants_raw):
            value = _exact_object(item, _GRANT_KEYS, f"grant {index}")
            action = value["action"]
            approval_class = value["approval_class"]
            if action not in _ACTIONS:
                raise RegistryError("invalid policy action")
            if approval_class not in _APPROVAL_CLASSES:
                raise RegistryError("invalid approval class")
            grant = _PolicyGrant(
                subject_seat_id=_identifier(
                    value["subject_seat_id"],
                    "grant subject",
                ),
                action=action,
                target_seat_id=_identifier(
                    value["target_seat_id"],
                    "grant target",
                ),
                manifest_revisions=_identifier_list(
                    value["manifest_revisions"],
                    "grant manifests",
                ),
                operation_families=_identifier_list(
                    value["operation_families"],
                    "grant families",
                ),
                approval_class=approval_class,
            )
            identity = (
                grant.subject_seat_id,
                grant.action,
                grant.target_seat_id,
                grant.manifest_revisions,
                grant.operation_families,
                grant.approval_class,
            )
            if identity in seen:
                raise RegistryError("duplicate policy grant")
            seen.add(identity)
            grants.append(grant)
        return cls(revision, tuple(grants))

    @property
    def revision(self) -> str:
        return self._revision

    def capability_grants(
        self,
        subject: SubjectContext,
        host_operation_families: frozenset[str],
    ) -> tuple[CapabilityGrant, ...]:
        result: list[CapabilityGrant] = []
        for grant in self._grants:
            if grant.subject_seat_id != subject.seat_id:
                continue
            families = tuple(
                family
                for family in grant.operation_families
                if family in host_operation_families
            )
            if not families:
                continue
            result.append(
                CapabilityGrant(
                    action=grant.action,
                    target_seat_id=grant.target_seat_id,
                    operation_families=families,
                    approval_class=grant.approval_class,
                )
            )
        return tuple(result)

    def authorize_plan(
        self,
        subject: SubjectContext,
        target: SeatRecord,
        manifest: ManifestRecord,
    ) -> AuthorizationDecision:
        subject_grants = tuple(
            grant
            for grant in self._grants
            if grant.subject_seat_id == subject.seat_id and grant.action == "plan"
        )
        if not subject_grants:
            return AuthorizationDecision(False, "none", (), "subject_denied")
        target_grants = tuple(
            grant
            for grant in subject_grants
            if grant.target_seat_id == target.seat_id
        )
        if not target_grants:
            return AuthorizationDecision(False, "none", (), "target_denied")
        manifest_grants = tuple(
            grant
            for grant in target_grants
            if manifest.revision in grant.manifest_revisions
        )
        if not manifest_grants:
            return AuthorizationDecision(False, "none", (), "manifest_denied")
        grant = manifest_grants[0]
        if not grant.operation_families:
            return AuthorizationDecision(False, "none", (), "family_denied")
        if grant.approval_class not in _APPROVAL_CLASSES:
            return AuthorizationDecision(
                False,
                "none",
                (),
                "approval_class_denied",
            )
        return AuthorizationDecision(
            True,
            grant.approval_class,
            grant.operation_families,
            "allowed",
        )
