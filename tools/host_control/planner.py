"""Server-owned plan construction for the host capability broker.

Import-closure exception (T-5516, ORG-052 §1/§3): this is the ONLY module in
``tools/host_control`` allowed to import ``tools.persona_installer``. The broker
plans in-process -- ``Manifest.from_bytes`` and ``PersonaInstaller.plan`` validate
and render the seat's Codex Telegram bridge block, so the broker process loads the
installer's agent-facing closure (Telegram bridge, mailbox client, harness
adapter). Every other host_control module takes the pure contract types from
``tools.persona_installer_contracts``. ``tools/test_host_control_import_closure.py``
pins this exception by name. T-5533 removes it (split the pure bridge
validate/render functions, or move planning into a sandboxed unprivileged
process) before T-2920 enables the broker.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Final, Iterable, Literal

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
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    OutOfEnvelopeKinds,
    require_executor_envelope,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.runtime_table import DaemonRuntime, load_daemon_runtime
from tools.host_control.self_amendment import default_protected_roots, refuse_self_amendment
# Sanctioned exception, see the module docstring (T-5516 / T-5533).
from tools.persona_installer import Manifest, PersonaInstaller
from tools.persona_installer_contracts import (
    InstallOperation,
    InstallPlan,
    OperatorAction,
)


_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")

# T-2917 5b planner partition (HO ruling T-4861, coordinator ruling B8).
SEAT_LOCAL_RESIDUE_LIMIT: Final = 32
SEAT_LOCAL_STATUS: Final = "pending-seat-local"
SEAT_LOCAL_ACTION_PREFIX: Final = "seat-local."
SEAT_LOCAL_CAPABILITY_PREFIX: Final = "seat-local:"


def partition_installer_plan(
    plan: InstallPlan,
) -> tuple[InstallPlan, tuple[InstallOperation, ...]]:
    """Split an installer plan into the BROKER plan (only kinds the
    executor envelope admits) and an explicit seat-local residue.

    Every excluded or unclassified kind is named in the residue, which is
    carried as ``OperatorAction`` entries on the broker plan so it is bound
    by both the plan digest and the effect digest and the approver sees it
    (nothing widens quietly). Fail closed, refusing the WHOLE plan, when
    the residue is over its bound or when an admitted operation is ordered
    after a residue operation: its ``precondition["after"]`` or
    ``desired["after"]`` (comma list, whitespace-tolerant) names a residue
    operation id or a residue kind (nothing partially applies out of order).
    """

    admitted = tuple(op for op in plan.operations if op.kind in EXECUTOR_KIND_CLASSES)
    residue = tuple(op for op in plan.operations if op.kind not in EXECUTOR_KIND_CLASSES)
    if not residue:
        return plan, ()
    if len(residue) > SEAT_LOCAL_RESIDUE_LIMIT:
        raise PolicyDenied("family_denied")
    residue_by_id = {op.operation_id: op for op in residue}
    residue_kinds = {op.kind for op in residue}
    blocked: set[str] = set()
    for operation in admitted:
        # ordering declarations live in precondition["after"] and, for
        # some kinds, desired["after"] (e.g. timer-observe names KINDS);
        # an entry may name an operation id or an operation kind
        for source in (operation.precondition, operation.desired):
            after = str(source.get("after", "") or "")
            for dependency in (item.strip() for item in after.split(",")):
                if not dependency:
                    continue
                if dependency in residue_by_id:
                    blocked.add(residue_by_id[dependency].kind)
                elif dependency in residue_kinds:
                    blocked.add(dependency)
    if blocked:
        raise OutOfEnvelopeKinds(blocked)
    actions = plan.operator_actions + tuple(
        OperatorAction(
            action_id=f"{SEAT_LOCAL_ACTION_PREFIX}{op.operation_id}",
            layer=op.layer,
            target=op.target,
            reason=f"{SEAT_LOCAL_STATUS}: {op.kind}",
            required_capability=f"{SEAT_LOCAL_CAPABILITY_PREFIX}{op.kind}",
        )
        for op in residue
    )
    values = {
        field.name: getattr(plan, field.name)
        for field in dataclasses.fields(plan)
        if field.name != "digest"
    }
    values.update(operations=admitted, operator_actions=actions)
    return InstallPlan.create(**values), residue


def seat_local_residue(plan: InstallPlan) -> list[dict[str, str]]:
    """The bounded residue list as the approver-facing summary shows it."""

    entries = []
    for action in plan.operator_actions:
        if not action.action_id.startswith(SEAT_LOCAL_ACTION_PREFIX):
            continue
        entries.append(
            {
                "operation_id": action.action_id[len(SEAT_LOCAL_ACTION_PREFIX):],
                "kind": action.required_capability[len(SEAT_LOCAL_CAPABILITY_PREFIX):],
                "status": SEAT_LOCAL_STATUS,
            }
        )
    if len(entries) > SEAT_LOCAL_RESIDUE_LIMIT:
        raise HostControlError("seat-local residue exceeds its bound")
    return entries


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
        runtime: DaemonRuntime | None = None,
        protected_roots: Iterable[str] = (),
    ) -> None:
        # gate 36: the static + loaded-package roots, plus any configured
        # trusted-config roots the daemon passes in.
        self._protected_roots = default_protected_roots(protected_roots)
        if plan_ttl <= timedelta(0):
            raise HostControlError("plan TTL must be positive")
        if not host_operation_families:
            raise HostControlError("host operation families must not be empty")
        # ORG-082 steps 3 + 5 (T-2918): constructing the plan composer IS the
        # daemon load; it captures the op-registry digest and asserts the
        # substrate-free dispatch table, or refuses (DaemonStartRefused).
        if runtime is None:
            runtime = load_daemon_runtime(host_operation_families=host_operation_families)
        elif runtime.host_operation_families != frozenset(host_operation_families):
            raise HostControlError("runtime table was loaded for different host operation families")
        self._runtime = runtime
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

    @property
    def runtime(self) -> DaemonRuntime:
        """The load-time runtime table (digest + dispatch table) this composer uses."""

        return self._runtime

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

        full_plan = self._installer.plan(
            frozen_manifest,
            target.persona,
            harness_override=target.harness,
            root=target.install_root,
        )
        if not full_plan.validate_digest():
            raise HostControlError("installer returned an invalid plan digest")
        # ORG-082 §2 (T-2918, F1): a substrate family is absent from the
        # daemon's dispatch table, so the composer refuses it HERE, before
        # partition can turn it into seat-local residue and before any
        # handle is minted or anything is journaled.
        self._runtime.refuse_outside_table(
            (operation.kind for operation in full_plan.operations),
            manifest_revision=manifest_revision,
        )
        # ORG-052 gate 36 (T-2918): a plan never amends the broker's own
        # authority surfaces (executables, units, bus rules, root registries,
        # approver policy, allowlists, op-enum definitions, journals).
        refuse_self_amendment(full_plan.operations, self._protected_roots)
        # T-2917 5b partition (HO ruling T-4861, superseding the 5a
        # whole-plan refusal of ruling i4): the broker plan holds ONLY
        # admitted kinds; everything else is the named seat-local residue.
        installer_plan, _residue = partition_installer_plan(full_plan)
        # Belt and braces: the broker still refuses, whole-plan, any
        # non-admitted kind INSIDE its own plan, before any handle is
        # minted or anything is journaled.
        require_executor_envelope(
            operation.kind for operation in installer_plan.operations
        )
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
        summary = self.plan_summary(stored)
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
        quota_check = manifest.installer.get("quota_check")
        if quota_check is None:
            quota_check = {}
        if not isinstance(quota_check, dict):
            raise HostControlError("frozen manifest quota_check section is invalid")
        actual = (
            systemd.get("service_template"),
            systemd.get("timer_template"),
            quota_check.get(
                "service_template",
                "tools/systemd/pantheon-persona-quota@.service",
            ),
            quota_check.get(
                "timer_template",
                "tools/systemd/pantheon-persona-quota@.timer",
            ),
        )
        if actual != template_ids:
            raise HostControlError("registered template binding mismatch")

    @staticmethod
    def plan_summary(plan: PlanEnvelope) -> str:
        """Bounded redacted plan summary — the single canonical
        redaction surface: both the public ``Plan`` result and the
        restricted ``ApprovalSource.PlanSummary`` return exactly these
        bytes (T-2912 OQ2 disposition: one surface, so the two
        disclosure paths cannot drift apart).
        """

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
            "seat_local_residue": seat_local_residue(plan.installer_plan),
            "expires_at": plan.expires_at,
        }
        encoded = canonical_json(summary)
        if len(encoded.encode("utf-8")) > 65536:
            raise HostControlError("plan summary exceeds size limit")
        return encoded
