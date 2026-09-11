"""Broker-side provider for the restricted ``HostBroker1.ApprovalSource``
interface (ORG-052 §5, T-2912).

Value-level domain provider: the D-Bus tier (``dbus_service``) restricts
the caller to the approval-service uid and marshals; this module owns
the reads. It returns plain wire tuples rather than the approval
package's ``PlanMetadata`` dataclass — the broker imports NOTHING from
the approval package (gate 18 broker half, structurally asserted), and
T-2913's D-Bus client owns the dataclass conversion.

Disclosure posture — DELIBERATELY NON-UNIFORM (C1 disposition, T-2912):

- An EXPIRED plan is reported as ``UnknownHandle``, byte-for-byte
  indistinguishable from a plan that never existed. The ratified 8-field
  wire contract has no staleness or expiry slot, so an error is the only
  fail-closed channel, and a stale plan must never be reviewable as if
  live.
- The public ``Manager`` interface, by contrast, keeps
  ``SubjectMismatch`` distinct from ``UnknownHandle``: handles are
  256-bit random (unguessable, so existence disclosure buys an attacker
  nothing) and the distinction aids the legitimate owner. This asymmetry
  is a named tension (P6), not an accident — do not "unify" it in either
  direction without revisiting the C1 record on T-2912.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from tools.host_control.contracts import (
    HostControlError,
    PlanEnvelope,
    UnknownHandle,
    sha256_json,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.planner import HostPlanService
from tools.host_control.wire import flatten_families

_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")

# One message for never-existed, malformed, and expired: the boundary is
# deliberately unable to express the difference.
_UNKNOWN = "unknown plan handle"


class BrokerApprovalSource:
    """Reads for the approval service's metadata-first review flow."""

    def __init__(
        self,
        *,
        journal: BrokerJournal,
        clock: Callable[[], datetime],
    ) -> None:
        self._journal = journal
        self._clock = clock

    def plan_metadata_wire(
        self,
        plan_handle: str,
    ) -> tuple[str, str, str, str, str, str, str, str]:
        """The eight ``PlanMetadata`` wire slots (ORG-052 §5,
        ``"s" -> "ssssssss"``): owner, performer, target, family,
        plan digest, effect digest, manifest revision, policy revision.

        owner == performer == the plan owner's ``seat_id``: a broker
        plan's performer IS its owner — distinct performers exist only
        as two linked plans in a handoff, each owned by its own subject
        (ORG-052 §5). The seat_id-as-subject-label convention is
        provisional and flagged to T-2913.
        """

        plan = self._load_live_plan(plan_handle)
        return (
            plan.owner.seat_id,
            plan.owner.seat_id,
            plan.target_seat_id,
            flatten_families(plan.operation_families),
            plan.digest,
            plan.effect_digest,
            plan.manifest_revision,
            plan.policy_revision,
        )

    def plan_summary(
        self,
        plan_handle: str,
        expected_digest: str,
    ) -> tuple[str, str]:
        """``PlanSummary`` (``"ss" -> "ss"``): honestly recomputed digest
        plus the bounded redacted summary — exactly the planner's
        canonical summary bytes, so the two disclosure surfaces cannot
        drift (OQ2 disposition)."""

        plan = self._load_live_plan(plan_handle)
        recomputed = sha256_json(plan.digest_payload())
        if not isinstance(expected_digest, str) or (
            expected_digest != recomputed
        ):
            raise HostControlError("plan digest does not match expectation")
        return recomputed, HostPlanService.plan_summary(plan)

    def _load_live_plan(self, plan_handle: str) -> PlanEnvelope:
        if (
            not isinstance(plan_handle, str)
            or _HEX_256.fullmatch(plan_handle) is None
        ):
            raise UnknownHandle(_UNKNOWN)
        plan = self._journal.load_plan_unbound(plan_handle)
        now = self._now()
        try:
            expires = datetime.fromisoformat(plan.expires_at)
        except ValueError as exc:
            raise HostControlError("invalid plan expiry timestamp") from exc
        if expires.tzinfo is None:
            raise HostControlError("invalid plan expiry timestamp")
        if expires <= now:
            raise UnknownHandle(_UNKNOWN)
        return plan

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise HostControlError(
                "approval source clock must return aware datetime"
            )
        return now.astimezone(timezone.utc)
