"""Server-owned plan construction for the host capability broker."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from tools.host_control.contracts import (
    CapabilitiesResult,
    HostControlError,
    PlanEnvelope,
    PlanResult,
    PolicyDenied,
    canonical_json,
    compute_effect_digest,
    sha256_json,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.persona_installer import Manifest, PersonaInstaller


_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")


class HostPlanService:
    def __init__(
        self,
        *,
        registry: SeatRegistry,
        policy: HostPolicy,
        journal: BrokerJournal,
        installer: PersonaInstaller,
        clock: Callable[[], datetime],
        handle_factory: Callable[[], str],
        plan_ttl: timedelta,
        contract_version: str,
        broker_revision: str,
        host_operation_families: frozenset[str],
        health_check: Callable[[], Literal["ready", "degraded"]],
    ) -> None:
        if plan_ttl <= timedelta(0):
            raise HostControlError("plan TTL must be positive")
        if not host_operation_families:
            raise HostControlError("host operation families must not be empty")
        self._registry = registry
        self._policy = policy
        self._journal = journal
        self._installer = installer
        self._clock = clock
        self._handle_factory = handle_factory
        self._plan_ttl = plan_ttl
        self._contract_version = self._bounded_scalar(
            contract_version,
            "contract version",
        )
        self._broker_revision = self._bounded_scalar(
            broker_revision,
            "broker revision",
        )
        self._host_operation_families = frozenset(
            self._bounded_scalar(value, "operation family")
            for value in host_operation_families
        )
        self._health_check = health_check

    @staticmethod
    def _bounded_scalar(value: object, field_name: str) -> str:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise HostControlError(f"invalid {field_name}")
        return value

    def capabilities(self, authenticated_uid: int) -> CapabilitiesResult:
        subject = self._registry.subject_for_uid(authenticated_uid)
        health = self._health_check()
        if health not in ("ready", "degraded"):
            raise HostControlError("invalid broker health state")
        return CapabilitiesResult(
            contract_version=self._contract_version,
            broker_revision=self._broker_revision,
            policy_revision=self._policy.revision,
            health=health,
            host_operation_families=tuple(
                sorted(self._host_operation_families)
            ),
            grants=self._policy.capability_grants(
                subject,
                self._host_operation_families,
            ),
        )

    def plan(
        self,
        authenticated_uid: int,
        seat_id: str,
        manifest_revision: str,
        idempotency_key: str,
    ) -> PlanResult:
        seat_id = self._bounded_scalar(seat_id, "seat id")
        manifest_revision = self._bounded_scalar(
            manifest_revision,
            "manifest revision",
        )
        idempotency_key = self._bounded_scalar(
            idempotency_key,
            "idempotency key",
        )
        request_digest = sha256_json(
            {
                "schema": "pantheon-host-plan-request/v1",
                "seat_id": seat_id,
                "manifest_revision": manifest_revision,
            }
        )
        subject = self._registry.subject_for_uid(authenticated_uid)
        target = self._registry.seat(seat_id)
        manifest_record = self._registry.manifest(manifest_revision)
        manifest_bytes = self._registry.read_manifest_bytes(manifest_record)
        frozen_manifest = Manifest.from_bytes(
            manifest_record.path,
            manifest_bytes,
        )
        self._validate_frozen_manifest(frozen_manifest, manifest_record.template_ids)

        decision = self._policy.authorize_plan(
            subject,
            target,
            manifest_record,
        )
        allowed_families = tuple(
            family
            for family in decision.operation_families
            if family in self._host_operation_families
        )
        if not decision.allowed:
            raise PolicyDenied(decision.reason_code)
        if not allowed_families:
            raise PolicyDenied("family_denied")

        installer_plan = self._installer.plan(
            frozen_manifest,
            target.persona,
            harness_override=target.harness,
            root=target.install_root,
        )
        if not installer_plan.validate_digest():
            raise HostControlError("installer returned an invalid plan digest")
        effect_digest = compute_effect_digest(
            target=target,
            manifest=manifest_record,
            installer_plan=installer_plan,
        )

        handle = self._handle_factory()
        if not isinstance(handle, str) or _HEX_256.fullmatch(handle) is None:
            raise HostControlError("invalid generated plan handle")
        created = self._clock()
        if created.tzinfo is None or created.utcoffset() is None:
            raise HostControlError("plan clock must return an aware datetime")
        created = created.astimezone(timezone.utc)
        expires = created + self._plan_ttl
        envelope = PlanEnvelope.create(
            schema="pantheon-host-plan-envelope/v1",
            plan_handle=handle,
            owner=subject,
            target_seat_id=target.seat_id,
            manifest_revision=manifest_record.revision,
            manifest_sha256=manifest_record.sha256,
            framework_revision=manifest_record.framework_revision,
            framework_sha256=manifest_record.framework_sha256,
            template_ids=manifest_record.template_ids,
            policy_revision=self._policy.revision,
            operation_families=allowed_families,
            approval_class=decision.approval_class,
            created_at=created.isoformat(),
            expires_at=expires.isoformat(),
            installer_plan=installer_plan,
            effect_digest=effect_digest,
        )
        stored, replayed = self._journal.put_plan_or_replay(
            subject=subject,
            target_seat_id=target.seat_id,
            manifest_revision=manifest_record.revision,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            plan=envelope,
        )
        summary = self._summary(stored)
        return PlanResult(
            plan_handle=stored.plan_handle,
            plan_digest=stored.digest,
            effect_digest=stored.effect_digest,
            summary_json=summary,
            replayed=replayed,
        )

    @staticmethod
    def _validate_frozen_manifest(
        manifest: Manifest,
        template_ids: tuple[str, ...],
    ) -> None:
        systemd = manifest.installer.get("systemd")
        if not isinstance(systemd, dict):
            raise HostControlError("frozen manifest systemd section is invalid")
        actual = (
            systemd.get("service_template"),
            systemd.get("timer_template"),
        )
        if actual != template_ids:
            raise HostControlError("registered template binding mismatch")

    @staticmethod
    def _summary(plan: PlanEnvelope) -> str:
        summary = {
            "schema": "pantheon-host-plan-summary/v1",
            "plan_handle": plan.plan_handle,
            "plan_digest": plan.digest,
            "effect_digest": plan.effect_digest,
            "target_seat_id": plan.target_seat_id,
            "manifest_revision": plan.manifest_revision,
            "manifest_sha256": plan.manifest_sha256,
            "framework_revision": plan.framework_revision,
            "framework_sha256": plan.framework_sha256,
            "policy_revision": plan.policy_revision,
            "approval_class": plan.approval_class,
            "operation_families": list(plan.operation_families),
            "operations": [
                {
                    "operation_id": operation.operation_id,
                    "kind": operation.kind,
                    "layer": operation.layer,
                    "privilege": operation.privilege,
                }
                for operation in plan.installer_plan.operations
            ],
            "expires_at": plan.expires_at,
        }
        encoded = canonical_json(summary)
        if len(encoded.encode("utf-8")) > 65536:
            raise HostControlError("plan summary exceeds size limit")
        return encoded
