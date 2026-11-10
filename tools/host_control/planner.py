"""Server-owned plan composition for the host capability broker.

T-5533 (ORG-052 section 1/3, split protocol from privilege): the broker does NOT
compute installer plans. Registration (T-2920) runs the unprivileged
``tools.host_plan_producer`` from the root-owned framework copy and registers the
plan's bytes per (seat, manifest revision) in the seat registry (``plans``).
``HostPlanService.plan`` reads that plan through the root-owned loader, checks its
registered sha256, canonical form and digest, refuses it when the framework copy's
artifact digest no longer matches the registered ``framework_sha256``, and then
applies the broker's own checks: the op-registry dispatch table, the self-amendment
refusal, the seat-local partition, the executor envelope, and the effect digest
(target, framework revision and framework digest bindings). No module in
``tools/host_control`` imports ``tools.persona_installer``; only the pure contract
types in ``tools.persona_installer_contracts`` are used
(``tools/test_host_control_import_closure.py`` pins this, and executes ``plan()``).
"""

from __future__ import annotations

import dataclasses
import json
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
    LAUNCH_DEPENDENT_KINDS,
    LAUNCH_KINDS,
    MUST_RUN_BEFORE_LAUNCH_KINDS,
    LaunchBeforeContainment,
    OutOfEnvelopeKinds,
    launch_order_violation,
    require_executor_envelope,
    require_routed_launch_bound,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.runtime_table import DaemonRuntime, load_daemon_runtime
from tools.host_control.self_amendment import default_protected_roots, refuse_self_amendment
from tools.persona_installer_contracts import (
    InstallOperation,
    InstallPlan,
    OperatorAction,
)


_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")

# T-2917 5b planner partition (HO ruling T-4861, coordinator ruling B8).
SEAT_LOCAL_RESIDUE_LIMIT: Final = 32
# T-5961 (orchestrator ruling on B8, 2026-11-02): the enumerated, allowlisted seat periodic timer ops
# (seat-owned user units named pantheon-fleet-usage-*) are counted in their OWN bucket, capped
# at 16 ops = 4 timer pairs x 4 ops per pair (service unit-install, timer unit-install, enable,
# observe; was 12 = 3 pairs before T-6125), so the general residue cap above stays 32. A Claude
# seat uses 4 (prune), a codex seat 8 (prune + codex-export), recovery 12 (T-6125: + consultant's
# checkpoint and rotate pairs). Anything else of these kinds counts as general residue.
# T-6125 (orchestrator ruling 2026-11-04): +4 headroom for watchman's codex seat timers; a seat past 16 needs a fresh ruling, not an automatic bump.
SEAT_PERIODIC_TIMER_LIMIT: Final = 16
SEAT_PERIODIC_TIMER_KINDS: Final = frozenset(
    {"seat-periodic-unit-install", "seat-periodic-timer-enable", "seat-periodic-timer-observe"}
)
_SEAT_PERIODIC_TIMER_STEMS: Final = r"(?:pantheon-fleet-usage-prune|pantheon-tmp-session-prune|pantheon-chronicle-prune|pantheon-fleet-usage-codex-export|pantheon-codex-handoff-checkpoint|pantheon-codex-thread-rotate)"
# unit-install targets the TEMPLATE file (empty instance); enable/observe target an instance TIMER.
# T-6121: pantheon-echo-target-prune is host-global and NOT instanced; its exact unit names are
# allowlisted as a second form (no `@`), so the same bucket cap applies and nothing is raised.
_SEAT_PERIODIC_SINGLETON_STEMS: Final = r"pantheon-echo-target-prune"
SEAT_PERIODIC_TEMPLATE_RE: Final = re.compile(
    r"(?:" + _SEAT_PERIODIC_TIMER_STEMS + r"@\.(?:service|timer)|" + _SEAT_PERIODIC_SINGLETON_STEMS + r"\.(?:service|timer))\Z")
SEAT_PERIODIC_TIMER_INSTANCE_RE: Final = re.compile(
    r"(?:" + _SEAT_PERIODIC_TIMER_STEMS + r"@[a-z0-9][a-z0-9_-]{0,63}\.timer|" + _SEAT_PERIODIC_SINGLETON_STEMS + r"\.timer)\Z")
# a seat's user unit dir is (<root>)/home/<account>/.config/systemd/user (persona_installer)
SEAT_PERIODIC_USER_UNIT_DIR_TAIL: Final = (".config", "systemd", "user")
SEAT_LOCAL_STATUS: Final = "pending-seat-local"
SEAT_LOCAL_ACTION_PREFIX: Final = "seat-local."
SEAT_LOCAL_CAPABILITY_PREFIX: Final = "seat-local:"


def is_seat_periodic_timer_op(kind: str, target: str) -> bool:
    """T-5961: True for an op in the seat-periodic-timer bucket, i.e. exactly the shapes the
    installer emits: a seat-periodic-unit-install of an allowlisted TEMPLATE unit file at an
    absolute, normalized .../home/<account>/.config/systemd/user/ path, or a seat-periodic-timer-enable /
    -observe of a bare allowlisted instance .timer name. Anything else of these kinds is
    general residue (T-5962/T-5961 review LOW-2: a suffix match let look-alike paths in)."""
    if kind not in SEAT_PERIODIC_TIMER_KINDS or not isinstance(target, str) or "\n" in target:
        return False
    if kind == "seat-periodic-unit-install":
        parts = target.split("/")
        if (not target.startswith("/") or any(p in ("", ".", "..") for p in parts[1:])
                or len(parts) < 7 or parts[-6] != "home"
                or tuple(parts[-4:-1]) != SEAT_PERIODIC_USER_UNIT_DIR_TAIL):
            return False
        return SEAT_PERIODIC_TEMPLATE_RE.match(parts[-1]) is not None
    return "/" not in target and SEAT_PERIODIC_TIMER_INSTANCE_RE.match(target) is not None


def residue_bucket_counts(kind_targets: Iterable[tuple[str, str]]) -> tuple[int, int]:
    """(general, periodic-timer) counts of out-of-envelope residue (kind, target) pairs."""
    periodic = general = 0
    for kind, target in kind_targets:
        if is_seat_periodic_timer_op(kind, target):
            periodic += 1
        else:
            general += 1
    return general, periodic


def require_residue_bounds(kind_targets: Iterable[tuple[str, str]], error: Callable[[], Exception]) -> None:
    general, periodic = residue_bucket_counts(kind_targets)
    if general > SEAT_LOCAL_RESIDUE_LIMIT or periodic > SEAT_PERIODIC_TIMER_LIMIT:
        raise error()


def launch_routed_operation_ids(plan: InstallPlan) -> frozenset[str]:
    """T-5470 partition routing (HO ruling 2026-10-26): when the plan carries a
    must-run-before-launch op that is seat-local residue (never admitted), every
    launch op, and every admitted op that needs a launch's effect in the same
    run (``LAUNCH_DEPENDENT_KINDS``), is routed to the residue too, so the broker
    never launches ahead of containment. Routing only ever moves admitted ops to
    the residue, never the other way. Otherwise (e.g. a codex plan with no such
    op) nothing is routed and the fires stay broker-executed."""

    if not any(
        op.kind in MUST_RUN_BEFORE_LAUNCH_KINDS and op.kind not in EXECUTOR_KIND_CLASSES
        for op in plan.operations
    ):
        return frozenset()
    return frozenset(
        op.operation_id
        for op in plan.operations
        if op.kind in EXECUTOR_KIND_CLASSES and op.kind in (LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS)
    )


def partition_installer_plan(
    plan: InstallPlan,
) -> tuple[InstallPlan, tuple[InstallOperation, ...]]:
    """Split an installer plan into the BROKER plan (only kinds the
    executor envelope admits, less any T-5470 launch routing,
    ``launch_routed_operation_ids``) and an explicit seat-local residue.

    Every excluded or unclassified kind is named in the residue, which is
    carried as ``OperatorAction`` entries on the broker plan so it is bound
    by both the plan digest and the effect digest and the approver sees it
    (nothing widens quietly). Fail closed, refusing the WHOLE plan, when
    the residue is over its bound or when an admitted operation is ordered
    after a residue operation: its ``precondition["after"]`` or
    ``desired["after"]`` (comma list, whitespace-tolerant) names a residue
    operation id or a residue kind (nothing partially applies out of order).
    """

    routed = launch_routed_operation_ids(plan)
    admitted = tuple(
        op for op in plan.operations if op.kind in EXECUTOR_KIND_CLASSES and op.operation_id not in routed
    )
    residue = tuple(
        op for op in plan.operations if op.kind not in EXECUTOR_KIND_CLASSES or op.operation_id in routed
    )
    if not residue:
        return plan, ()
    # T-5470 partition routing (HO ruling 2026-10-26): routed launches have their
    # own bound; B8 cap unchanged. SEAT_LOCAL_RESIDUE_LIMIT bounds only the
    # out-of-envelope residue; the routed ops (fixed, enumerated, in-envelope
    # kinds) are bounded at one op per LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS kind.
    require_residue_bounds(
        ((op.kind, op.target) for op in residue if op.operation_id not in routed),
        lambda: PolicyDenied("family_denied"),
    )
    require_routed_launch_bound(op.kind for op in residue if op.operation_id in routed)
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


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostControlError("registered plan has a duplicate JSON key")
        result[key] = value
    return result


def parse_registered_plan(raw: bytes) -> InstallPlan:
    """Parse root-registered plan bytes (T-5533): strict JSON with no duplicate
    keys, exactly the canonical encoding of the plan it decodes to, and a valid
    installer-plan digest."""

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostControlError("registered plan is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HostControlError("registered plan is malformed")
    try:
        plan = InstallPlan.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise HostControlError("registered plan is malformed") from exc
    if canonical_json(plan.to_dict()).encode("utf-8") != raw:
        raise HostControlError("registered plan is not canonical")
    if not plan.validate_digest():
        raise HostControlError("registered plan has an invalid digest")
    return plan


def effective_broker_order(plan: InstallPlan) -> tuple[InstallOperation, ...]:
    """T-5470: the order the broker flow actually executes a plan in: the
    admitted broker operations, then the seat-local residue (both from
    ``partition_installer_plan`` itself, never a copy of its rule)."""

    broker_plan, residue = partition_installer_plan(plan)
    return broker_plan.operations + residue


def refuse_launch_before_containment(plan: InstallPlan) -> None:
    """T-5470 standing invariant, shared by the registration-time producer and
    ``HostPlanService.plan``: over the effective broker order no launch op runs
    before a must-run-before-launch op, and a claude plan that launches carries
    its shell-box containment op (``envelope.launch_order_violation``)."""

    reason = launch_order_violation(
        ((operation.operation_id, operation.kind) for operation in effective_broker_order(plan)),
        selected_harness=plan.selected_harness,
    )
    if reason is not None:
        raise LaunchBeforeContainment(reason)


def seat_local_residue(plan: InstallPlan) -> list[dict[str, str]]:
    """The bounded residue list as the approver-facing summary shows it."""

    entries = []
    targets = []
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
        targets.append(action.target)
    # T-5470 partition routing (HO ruling 2026-10-26): routed launches have their
    # own bound; B8 cap unchanged. A residue entry of an in-envelope kind can only
    # be a routed launch (or dependent); the rest is the out-of-envelope residue.
    routed = [entry for entry in entries if entry["kind"] in EXECUTOR_KIND_CLASSES]
    require_residue_bounds(
        ((e["kind"], t) for e, t in zip(entries, targets) if e["kind"] not in EXECUTOR_KIND_CLASSES),
        lambda: HostControlError("seat-local residue exceeds its bound"),
    )
    require_routed_launch_bound(entry["kind"] for entry in routed)
    return entries


class HostPlanService:
    def __init__(
        self,
        *,
        registry: SeatRegistry,
        policy: HostPolicy,
        journal: BrokerJournal,
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
        # The registered manifest must not have drifted since registration, and
        # (T-5533) the framework copy must still hash to the registered
        # framework_sha256: a plan registered against an older copy is stale.
        # (The frozen-manifest template binding moved to the producer:
        # tools.host_plan_producer.validate_frozen_manifest.)
        self._registry.read_manifest_bytes(manifest_record)
        self._registry.check_framework_digest(manifest_record)

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

        # T-5533: the root-registered plan for exactly this (seat, manifest
        # revision); computed at registration by tools.host_plan_producer.
        plan_record = self._registry.plan_record(target.seat_id, manifest_record.revision)
        full_plan = parse_registered_plan(self._registry.read_plan_bytes(plan_record))
        if full_plan.provenance.get("manifest") != str(manifest_record.path):
            raise HostControlError("registered plan is bound to another manifest")
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
        # T-5470: no launch ahead of containment in the order the broker flow
        # executes (admitted, then residue). After the target/framework bindings
        # (compute_effect_digest), still before any handle or journal write.
        refuse_launch_before_containment(full_plan)

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
