"""Approval-side review/grant lifecycle service (ORG-052 §5 — T-2910).

Pure state transitions over frozen records; the approval journal owns
persistence. The broker-facing consumer surface implements the landed
``ApprovalBinder`` contract via ``ApprovalService.as_binder`` — the broker
knows only that Protocol and the shared ``BindOutcome``; it can never name,
construct, or edit a grant record.

Authentication vs authorization boundary in this unit (PO mandate, OQ5):
the reviewer-surface ``approver_subject`` parameter is a CALLER-ASSERTED
label. Identity verification is the caller's responsibility and MUST be
replaced by a bus-derived subject at the Unit-3 D-Bus boundary; the shipped
DENY-ALL default authorizer is what keeps the caller-asserted label inert
until then. (T-2911's ``PolicyApproverAuthorizer`` gates what a SUBJECT may
do — roster, separation of duty, class routing — but the binding of caller
to subject stays Unit 3's; policy narrows the label's power, it does not
authenticate it.) The consumer-surface ``authenticated_uid`` is likewise taken as
given-authentic; uid-to-bus-sender resolution is Unit 3's. The
``registered_consumer_uids`` allowlist (default: empty, deny) mirrors
ORG-052 §5's requirement that each service rechecks the live bus-derived
uid — defense in depth, not Unit-3 scope pulled forward.

Public boundary discipline (PO mandate (b), gate 21): no public method
returns a ``GrantRecord``, the single-use ``nonce``, or — outside the
``BindOutcome`` of a durable BOUND commit — an ``authority_record_id``.
Decide returns state plus the opaque approval handle only.
"""

from __future__ import annotations

import dataclasses
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from tools.host_control.approval.contracts import (
    BIND_DENIAL_REASON_CODES,
    DECIDE_REASON_CODES,
    GRANT_SCHEMA,
    GRANT_STATES,
    GRANT_TRANSITIONS,
    MAX_PLAN_SUMMARY_LENGTH,
    REVIEW_SCHEMA,
    REVIEW_STATES,
    REVIEW_TRANSITIONS,
    REVOKE_REASON_CODES,
    ApprovalCounts,
    ApprovalStatus,
    ApproverDenied,
    DecisionResult,
    ExpirySweepStatus,
    GrantRecord,
    HandoffLink,
    HandoffMismatch,
    OperationConsumed,
    PlanMetadata,
    PolicyVerdict,
    ReviewRecord,
    ReviewResult,
    StaleAuthorityState,
)
from tools.host_control.approval.human import (
    HUMAN_AUDIT_ANCHOR,
    MAX_CARD_SUMMARY_LENGTH,
    MAX_PROVIDER_MESSAGE_REF_LENGTH,
    MAX_SOURCE_EVENT_LENGTH,
    AttestationRecord,
    HumanDecisionRefused,
    HumanDecisionResult,
    HumanIngressRegistration,
    HumanRequestRecord,
    HumanRequestResult,
)
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.presentation import (
    AsyncHumanCardPresenter,
    DeliveryScheduler,
    HumanCardDelivery,
    PresentCall,
    PresentReply,
    Settled,
)
from tools.host_control.contracts import (
    BindOutcome,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    PolicyDenied,
    UnknownHandle,
    _closed_hex,
    _closed_string,
    canonical_json,
)


class PlanMetadataSource(Protocol):
    """Value-returning seam for ``HostBroker1.ApprovalSource`` (both
    methods: ``PlanMetadata`` and ``PlanSummary``).

    Same shape discipline as the broker's ``ExecutorPort``: it is handed no
    journal, no connection, and no callback — the real implementation lands
    in Unit 3 (T-2912/T-2913) as a D-Bus client and fakes supply it in
    tests.

    ``plan_summary`` (T-2911, acceptance gate 12) mirrors the wire method
    ``PlanSummary(plan_handle, expected_digest) -> "ss"``: returns the
    recomputed digest and a bounded redacted summary. CALL-ORDER CONTRACT:
    the service calls it ONLY after the approver has been authorized
    against minimal metadata AND the handoff link (if any) has validated —
    metadata-first disclosure; an unauthorized reviewer must trigger ZERO
    summary fetches (the spy tests assert call count and order).

    Raises ``UnknownHandle`` for a plan the broker does not know and
    ``MetadataUnavailable`` on transport/outage failure. On unavailability
    the caller must refuse WITHOUT writing: fail closed, grant untouched.
    """

    def plan_metadata(self, plan_handle: str) -> PlanMetadata: ...

    def plan_summary(
        self,
        plan_handle: str,
        expected_digest: str,
    ) -> tuple[str, str]: ...


class ApproverAuthorizer(Protocol):
    """Fail-closed approver/administrator authorization seam.

    Re-cut in T-2911 (pre-authorized by the AB-2 OQ3 disposition): every
    method returns a closed ``PolicyVerdict`` so denial reason codes
    survive to the enforcement boundary, and the handoff stage gets its
    own method. The real implementation is
    ``tools.host_control.approval.policy.PolicyApproverAuthorizer`` over a
    root-owned policy file; the DENY-ALL module default below remains the
    shipped default — the real authorizer is opt-in by construction.

    All verdicts still gate a CALLER-ASSERTED subject label in this unit
    (see module docstring); the Unit-3 D-Bus boundary replaces it with the
    bus-derived subject.

    ``chain_participants`` is the server-derived set of subjects already
    participating in the same request-plan handoff chain (the service
    queries its own journal); empty for direct reviews.
    """

    def may_review(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
    ) -> PolicyVerdict: ...

    def may_handoff(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
        request_metadata: PlanMetadata,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict: ...

    def may_decide(
        self,
        approver_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict: ...

    def may_revoke(self, subject: str, grant: GrantRecord) -> PolicyVerdict: ...

    def may_decide_human(
        self,
        human_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict: ...

    def may_withdraw(self, subject: str) -> PolicyVerdict: ...


class HumanCardPresenter(Protocol):
    """Seam for ``HostApprovalTelegram1.Ingress.Present`` (ORG-052 §5).

    The real implementation is the D-Bus client in
    ``tools.host_control.approval.telegram_presenter``; fakes supply it
    in tests. Returns ``(state, provider_message_ref)``; any exception
    means "not delivered" and the record stays ``pending`` (a
    RequestHuman replay retries; the ingress dedupes by review handle so
    exactly one card exists per record).

    This SYNCHRONOUS seam is for in-process composition only. Over D-Bus
    the service uses the non-blocking ``AsyncHumanCardPresenter`` plus a
    ``DeliveryScheduler`` (``card_presenter=`` / ``delivery_scheduler=``):
    the service itself then retries an undelivered card with bounded
    backoff until it is presented, expired or withdrawn (T-5518)."""

    def present(
        self,
        review_handle: str,
        expected_digest: str,
        summary_json: str,
        callback_nonce: str,
        expires_at: str,
    ) -> tuple[str, str]: ...


@dataclass(frozen=True)
class ReviewExtras:
    """Record-derived ReviewGet additions (T-2919 Q3): the opaque approval
    handle once granted (never bearer authority, ORG-052 §5) and the human
    request's handle/state/expiry. Never the nonce."""

    approval_handle: str | None
    human_request_handle: str | None
    human_request_state: str | None
    human_request_expires_at: str | None


_TRUNCATION_MARKER = " [truncated]"


class _DenyAllAuthorizer:
    """Shipped default: nobody may review, decide, or revoke (L9)."""

    def may_review(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
    ) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")

    def may_handoff(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
        request_metadata: PlanMetadata,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")

    def may_decide(
        self,
        approver_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")

    def may_revoke(self, subject: str, grant: GrantRecord) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")

    def may_decide_human(
        self,
        human_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")

    def may_withdraw(self, subject: str) -> PolicyVerdict:
        return PolicyVerdict(False, "deny_all_default")


DENY_ALL_AUTHORIZER: ApproverAuthorizer = _DenyAllAuthorizer()


def _require_allowed(verdict: PolicyVerdict, message: str) -> PolicyVerdict:
    """Raise ApproverDenied carrying the verdict's closed reason code."""

    if not verdict.allowed:
        raise ApproverDenied(message, reason_code=verdict.reason_code)
    return verdict


def _nonce_matches(stored: str, presented: str) -> bool:
    """Constant-time nonce comparison (single-use callback nonce)."""

    return hmac.compare_digest(stored.encode("ascii"), presented.encode("ascii"))


def default_handle_factory() -> str:
    """Random opaque 64-hex handle/nonce material (L11)."""

    return secrets.token_hex(32)


def transition_review_record(
    record: ReviewRecord,
    to_state: str,
    *,
    decided_at: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    approver_subject: str | None = None,
    authority_source: str | None = None,
    audit_anchor: str | None = None,
    decide_idempotency_key: str | None = None,
    source_event: str | None = None,
) -> ReviewRecord:
    """Pure review transition; raises InvalidTransition outside the table."""

    if record.state not in REVIEW_STATES or to_state not in REVIEW_STATES:
        raise InvalidTransition("unknown review state")
    if to_state not in REVIEW_TRANSITIONS[record.state]:
        raise InvalidTransition(
            f"illegal review transition {record.state} -> {to_state}"
        )
    values = {
        field.name: getattr(record, field.name)
        for field in dataclasses.fields(record)
        if field.name != "digest"
    }
    values.update(
        state=to_state,
        decision=decision,
        reason_code=reason_code,
        approver_subject=approver_subject,
        authority_source=authority_source,
        audit_anchor=audit_anchor,
        decide_idempotency_key=decide_idempotency_key,
        decided_at=decided_at,
        source_event=source_event,
    )
    return ReviewRecord.create(**values)


def transition_grant_record(
    record: GrantRecord,
    to_state: str,
    *,
    updated_at: str,
    consuming_operation_handle: str | None = None,
    authority_record_id: str | None = None,
) -> GrantRecord:
    """Pure grant transition; raises InvalidTransition outside the table."""

    if record.state not in GRANT_STATES or to_state not in GRANT_STATES:
        raise InvalidTransition("unknown grant state")
    if to_state not in GRANT_TRANSITIONS[record.state]:
        raise InvalidTransition(
            f"illegal grant transition {record.state} -> {to_state}"
        )
    values = {
        field.name: getattr(record, field.name)
        for field in dataclasses.fields(record)
        if field.name != "digest"
    }
    values.update(
        state=to_state,
        updated_at=updated_at,
        consuming_operation_handle=consuming_operation_handle,
        authority_record_id=authority_record_id,
    )
    return GrantRecord.create(**values)


class ConsumerBinder:
    """Adapter binding one authenticated consumer uid to the service.

    Structurally satisfies the broker's landed ``ApprovalBinder`` Protocol
    (four strings in, ``BindOutcome`` out) so the broker's
    ``OperationService`` can consume the real approval service without ever
    holding the approval journal.
    """

    def __init__(self, service: "ApprovalService", authenticated_uid: int) -> None:
        self._service = service
        self._authenticated_uid = authenticated_uid

    def validate_and_bind(
        self,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome:
        return self._service.validate_and_bind(
            self._authenticated_uid,
            approval_handle,
            plan_handle,
            expected_digest,
            operation_handle,
        )


class ApprovalService:
    def __init__(
        self,
        *,
        journal: ApprovalJournal,
        metadata_source: PlanMetadataSource,
        clock: Callable[[], datetime],
        authorizer: ApproverAuthorizer = DENY_ALL_AUTHORIZER,
        handle_factory: Callable[[], str] = default_handle_factory,
        registered_consumer_uids: frozenset[int] = frozenset(),
        review_ttl: timedelta = timedelta(hours=1),
        grant_ttl: timedelta = timedelta(hours=1),
        human_ingress: HumanIngressRegistration | None = None,
        presenter: HumanCardPresenter | None = None,
        human_card_ttl: timedelta = timedelta(minutes=15),
        card_presenter: AsyncHumanCardPresenter | None = None,
        delivery_scheduler: DeliveryScheduler | None = None,
    ) -> None:
        for uid in registered_consumer_uids:
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                raise HostControlError("invalid registered consumer uid")
        if review_ttl <= timedelta(0) or grant_ttl <= timedelta(0):
            raise HostControlError("invalid approval ttl")
        if human_card_ttl <= timedelta(0):
            raise HostControlError("invalid human card ttl")
        if human_ingress is not None:
            if not isinstance(human_ingress, HumanIngressRegistration):
                raise HostControlError("invalid human ingress registration")
            # construction-time cross-check (T-2919 §6): the registered
            # human subject's policy trust domain IS the ingress uid, when
            # the authorizer can answer (the real policy authorizer can)
            subject_uid = getattr(authorizer, "subject_uid", None)
            if subject_uid is not None and (
                subject_uid(human_ingress.human_subject) != human_ingress.uid
            ):
                raise HostControlError(
                    "human subject trust domain is not the ingress uid"
                )
            agent_on_uid = getattr(authorizer, "agent_approvers_on_uid", None)
            if agent_on_uid is not None and agent_on_uid(human_ingress.uid):
                # review finding 7: the network-facing ingress uid can
                # never carry Reviewer standing
                raise HostControlError(
                    "an agent-source approver shares the ingress uid"
                )
            if human_ingress.uid in registered_consumer_uids:
                raise HostControlError(
                    "ingress uid must not be a registered consumer"
                )
        if (card_presenter is None) != (delivery_scheduler is None):
            raise HostControlError(
                "an async card presenter needs a delivery scheduler"
            )
        if card_presenter is not None and presenter is not None:
            raise HostControlError("configure one card presenter, not two")
        self._human_ingress = human_ingress
        self._presenter = presenter
        self._delivery = (
            None
            if card_presenter is None
            else HumanCardDelivery(
                presenter=card_presenter,
                scheduler=delivery_scheduler,  # type: ignore[arg-type]
                load=self._delivery_load,
                complete=self._delivery_complete,
            )
        )
        self._human_card_ttl = human_card_ttl
        self._journal = journal
        self._metadata_source = metadata_source
        self._clock = clock
        self._authorizer = authorizer
        self._handle_factory = handle_factory
        self._consumer_uids = frozenset(registered_consumer_uids)
        self._review_ttl = review_ttl
        self._grant_ttl = grant_ttl
        # expiry-sweep contention signal (T-4350): in-memory, single-writer
        # (the journal connection is thread-affine, so is this service)
        self._busy_sweeps_total = 0
        self._consecutive_busy_sweeps = 0

    # ------------------------------------------------------------------
    # reviewer surface (HostApproval1.Reviewer shape; D-Bus wiring Unit 3)
    # ------------------------------------------------------------------

    def review(
        self,
        approver_subject: str,
        plan_handle: str,
        expected_digest: str,
        request_plan_handle: str | None,
        *,
        handoff_anchor: str | None = None,
    ) -> ReviewResult:
        """Open (or replay) a review; return record + bounded summary.

        Disclosure order (gate 12): minimal metadata -> authorization ->
        handoff-link validation -> ONLY THEN the redacted plan summary.
        An unauthorized reviewer triggers zero summary fetches and zero
        request-plan fetches.

        ``handoff_anchor`` (gate 23) is the bounded dispatch/ticket
        anchor: caller-supplied AUDIT CONTEXT, stored on the link and
        bound into any resulting grant — never authority, never an input
        to identity derivation or a policy verdict. One hop only: deeper
        chains are unsupported BY CONSTRUCTION — no field on the link,
        review, or grant can express a parent handoff or chain depth, and
        this surface accepts exactly one optional request-plan handle.
        """

        approver = _closed_string(approver_subject, "approver subject")
        plan_handle = _closed_hex(plan_handle, "plan handle")
        expected_digest = _closed_hex(expected_digest, "plan digest")
        if request_plan_handle is not None:
            request_plan_handle = _closed_hex(
                request_plan_handle, "request plan handle"
            )
        if handoff_anchor is not None:
            if request_plan_handle is None:
                raise HostControlError(
                    "handoff anchor requires a request-plan handoff"
                )
            handoff_anchor = _closed_string(
                handoff_anchor, "handoff anchor", maximum=256
            )
        metadata = self._metadata_source.plan_metadata(plan_handle)
        _require_allowed(
            self._authorizer.may_review(approver, metadata),
            "approver may not review this plan",
        )
        if metadata.plan_digest != expected_digest:
            raise HostControlError("plan digest does not match expectation")
        handoff: HandoffLink | None = None
        handoff_kind = "direct_no_handoff"
        if request_plan_handle is not None:
            if request_plan_handle == plan_handle:
                # refused BEFORE any second fetch
                raise HandoffMismatch(
                    "handoff request plan is the performer plan",
                    reason_code="handoff_self_reference",
                )
            request_metadata = self._metadata_source.plan_metadata(
                request_plan_handle
            )
            # policy separation FIRST (an ineligible approver learns
            # nothing about how the tuples relate), then link validation
            _require_allowed(
                self._authorizer.may_handoff(
                    approver,
                    metadata,
                    request_metadata,
                    self._journal.handoff_chain_subjects(request_plan_handle),
                ),
                "approver may not review this handoff",
            )
            # requester identity comes ONLY from the request plan's owner
            # (server-derived); a caller argument, role name, mailbox
            # label, or ticket anchor is never identity evidence
            requester = request_metadata.owner
            if requester in (metadata.owner, metadata.performer):
                raise HandoffMismatch(
                    "handoff requester is the performer",
                    reason_code="handoff_requester_is_performer",
                )
            # full-tuple match (gate 23): target, manifest revision,
            # operation families, effect digest — each its own code
            if request_metadata.target_seat_id != metadata.target_seat_id:
                raise HandoffMismatch(
                    "handoff target does not match",
                    reason_code="handoff_target_mismatch",
                )
            if request_metadata.manifest_revision != metadata.manifest_revision:
                raise HandoffMismatch(
                    "handoff manifest revision does not match",
                    reason_code="handoff_manifest_mismatch",
                )
            if tuple(request_metadata.operation_families) != tuple(
                metadata.operation_families
            ):
                raise HandoffMismatch(
                    "handoff operation families do not match",
                    reason_code="handoff_family_mismatch",
                )
            if request_metadata.effect_digest != metadata.effect_digest:
                raise HandoffMismatch(
                    "handoff effect digest does not match",
                    reason_code="handoff_effect_mismatch",
                )
            handoff = HandoffLink(
                request_plan_handle=request_plan_handle,
                request_plan_digest=request_metadata.plan_digest,
                request_plan_owner=requester,
                shared_effect_digest=request_metadata.effect_digest,
                handoff_anchor=handoff_anchor,
            )
            handoff_kind = "request_plan"
        # metadata-first disclosure (gate 12): the summary is fetched LAST,
        # only after authorization and link validation both passed
        recomputed_digest, plan_summary = self._metadata_source.plan_summary(
            plan_handle, expected_digest
        )
        if recomputed_digest != metadata.plan_digest:
            raise HostControlError(
                "plan summary digest does not match plan metadata"
            )
        plan_summary = _closed_string(
            plan_summary,
            "plan summary",
            maximum=MAX_PLAN_SUMMARY_LENGTH,
        )
        now = self._now()
        record = ReviewRecord.create(
            schema=REVIEW_SCHEMA,
            review_handle=self._new_handle(),
            plan_handle=plan_handle,
            plan_digest=metadata.plan_digest,
            plan_owner=metadata.owner,
            performer=metadata.performer,
            target_seat_id=metadata.target_seat_id,
            operation_families=tuple(metadata.operation_families),
            effect_digest=metadata.effect_digest,
            manifest_revision=metadata.manifest_revision,
            policy_revision=metadata.policy_revision,
            handoff_kind=handoff_kind,
            handoff=handoff,
            requested_by=approver,
            state="open",
            decision=None,
            reason_code=None,
            approver_subject=None,
            authority_source=None,
            audit_anchor=None,
            source_event=None,
            decide_idempotency_key=None,
            created_at=now.isoformat(),
            expires_at=(now + self._review_ttl).isoformat(),
            decided_at=None,
        )
        stored, replayed = self._journal.put_review_or_replay(record=record)
        if replayed:
            live = self._lazy_expire_review(stored)
            if live.state == "expired":
                # the stale open review just expired durably; the slot is
                # free again and the fresh record takes it
                fresh, fresh_replayed = self._journal.put_review_or_replay(
                    record=record
                )
                return ReviewResult(fresh, fresh_replayed, plan_summary)
            # the journal's replay tuple deliberately excludes the anchor
            # (it is context, not identity) — but a replay that would have
            # recorded DIFFERENT context is a conflict, not an idempotent
            # repeat (the decide-replay audit_anchor rule, same shape)
            stored_anchor = (
                None if live.handoff is None else live.handoff.handoff_anchor
            )
            if handoff_anchor != stored_anchor:
                raise IdempotencyConflict(
                    "review replay with a different handoff anchor"
                )
            return ReviewResult(live, True, plan_summary)
        return ReviewResult(stored, False, plan_summary)

    def review_get(
        self,
        approver_subject: str,
        review_handle: str,
    ) -> ReviewRecord:
        approver = _closed_string(approver_subject, "approver subject")
        review_handle = _closed_hex(review_handle, "review handle")
        record = self._journal.get_review(review_handle)
        # reconstruct the minimal metadata view from the stored record; the
        # seam decides, this unit implements no policy (L9)
        view = PlanMetadata(
            owner=record.plan_owner,
            performer=record.performer,
            target_seat_id=record.target_seat_id,
            operation_families=record.operation_families,
            plan_digest=record.plan_digest,
            effect_digest=record.effect_digest,
            manifest_revision=record.manifest_revision,
            policy_revision=record.policy_revision,
        )
        _require_allowed(
            self._authorizer.may_review(approver, view),
            "approver may not read this review",
        )
        return self._lazy_expire_review(record)

    def decide(
        self,
        approver_subject: str,
        review_handle: str,
        expected_digest: str,
        decision: str,
        reason_code: str,
        audit_anchor: str,
        idempotency_key: str,
    ) -> DecisionResult:
        approver = _closed_string(approver_subject, "approver subject")
        review_handle = _closed_hex(review_handle, "review handle")
        expected_digest = _closed_hex(expected_digest, "plan digest")
        if decision not in ("granted", "denied"):
            raise HostControlError("invalid decision")
        if reason_code not in DECIDE_REASON_CODES:
            raise HostControlError("invalid decide reason code")
        if (decision == "granted") != (reason_code == "approved"):
            raise HostControlError("decision and reason code do not pair")
        audit_anchor = _closed_string(
            audit_anchor, "audit anchor", maximum=256
        )
        idempotency_key = _closed_string(
            idempotency_key, "decide idempotency key"
        )
        record = self._journal.get_review(review_handle)
        if record.state in ("granted", "denied"):
            if (
                record.decide_idempotency_key == idempotency_key
                and record.approver_subject == approver
            ):
                if (
                    record.decision == decision
                    and record.reason_code == reason_code
                    and record.plan_digest == expected_digest
                    # AB-2 review gap 3: a same-key replay carrying a
                    # DIFFERENT audit anchor is a conflict, not a replay —
                    # the anchor is context, but silently swallowing a
                    # differing one would misrecord the audit trail
                    and record.audit_anchor == audit_anchor
                ):
                    grant = (
                        self._journal.get_grant_for_review(review_handle)
                        if record.state == "granted"
                        else None
                    )
                    if record.state == "granted" and grant is None:
                        raise HostControlError(
                            "granted review has no grant record"
                        )
                    return DecisionResult(
                        review_handle=review_handle,
                        state=record.state,
                        decision=record.decision,
                        reason_code=record.reason_code,
                        approval_handle=(
                            None if grant is None else grant.approval_handle
                        ),
                    )
                raise IdempotencyConflict("decide idempotency conflict")
            raise HostControlError("review is already decided")
        existing = self._journal.find_decided_by_key(
            approver, idempotency_key
        )
        if existing is not None and existing.review_handle != review_handle:
            raise IdempotencyConflict(
                "decide idempotency key reused for a different review"
            )
        record = self._lazy_expire_review(record)
        if record.state == "expired":
            raise HostControlError("review is expired")
        if record.state != "open":
            raise HostControlError("review is not open")
        if expected_digest != record.plan_digest:
            raise HostControlError("plan digest does not match expectation")
        # chain participants re-queried at decide time — the chain may
        # have grown since review-open; decide is the authoritative gate
        chain_participants = (
            frozenset()
            if record.handoff is None
            else self._journal.handoff_chain_subjects(
                record.handoff.request_plan_handle
            )
        )
        verdict = _require_allowed(
            self._authorizer.may_decide(approver, record, chain_participants),
            "approver may not decide this review",
        )
        # ORG-052 §5 maximum TTL: the grant gets the TIGHTER of the
        # policy ceiling and the service's configured TTL
        grant_ttl = self._grant_ttl
        if verdict.max_ttl is not None:
            grant_ttl = min(grant_ttl, verdict.max_ttl)
        now = self._now()
        new = transition_review_record(
            record,
            decision,
            decided_at=now.isoformat(),
            decision=decision,
            reason_code=reason_code,
            approver_subject=approver,
            # every decision this unit can mint is agent-sourced; the
            # provider-authenticated human path is T-2919's HumanDecision
            authority_source="agent",
            audit_anchor=audit_anchor,
            decide_idempotency_key=idempotency_key,
        )
        grant: GrantRecord | None = None
        if decision == "granted":
            grant = GrantRecord.create(
                schema=GRANT_SCHEMA,
                approval_handle=self._new_handle(),
                review_handle=review_handle,
                plan_handle=record.plan_handle,
                plan_digest=record.plan_digest,
                plan_owner=record.plan_owner,
                performer=record.performer,
                target_seat_id=record.target_seat_id,
                operation_families=record.operation_families,
                effect_digest=record.effect_digest,
                manifest_revision=record.manifest_revision,
                policy_revision=record.policy_revision,
                handoff_kind=record.handoff_kind,
                handoff=record.handoff,
                approver_subject=approver,
                authority_source="agent",
                audit_anchor=audit_anchor,
                source_event=None,
                decision="granted",
                reason_code=reason_code,
                issued_at=now.isoformat(),
                expires_at=(now + grant_ttl).isoformat(),
                nonce=self._new_handle(),
                idempotency_key=idempotency_key,
                state="issued",
                consuming_operation_handle=None,
                authority_record_id=None,
                updated_at=now.isoformat(),
            )
        self._journal.record_decision(
            current=record,
            new=new,
            grant=grant,
            payload={
                "decision": decision,
                "reason_code": reason_code,
                "approver_subject": approver,
                # P4: which APPROVER-policy version authorized this
                # decision (None for verdict sources that carry no
                # revision, e.g. test permissives). Distinct from the
                # broker policy_revision on the record itself.
                "approver_policy_revision": verdict.policy_revision,
            },
        )
        return DecisionResult(
            review_handle=review_handle,
            state=new.state,
            decision=decision,
            reason_code=reason_code,
            approval_handle=None if grant is None else grant.approval_handle,
        )

    # ------------------------------------------------------------------
    # human authority ingress (T-2919; ORG-052 §5 "Human authority
    # ingress")
    # ------------------------------------------------------------------

    def request_human(
        self,
        approver_subject: str,
        review_handle: str,
        expected_digest: str,
        idempotency_key: str,
    ) -> HumanRequestResult:
        """Create (or replay) the ONE pending human-review card for a
        review, then ask the registered ingress to present it.

        Mailbox/ticket-triggered calls can REQUEST review this way
        (gate 22) but nothing here mints authority: the record is inert
        until the provider-authenticated ingress consumes its nonce.
        Disclosure order (gate 12) is preserved: the caller is authorized
        against the stored review's minimal metadata before the redacted
        summary is fetched."""

        approver = _closed_string(approver_subject, "approver subject")
        review_handle = _closed_hex(review_handle, "review handle")
        expected_digest = _closed_hex(expected_digest, "plan digest")
        idempotency_key = _closed_string(
            idempotency_key, "human request idempotency key"
        )
        registration = self._human_ingress
        if registration is None:
            raise HumanDecisionRefused(reason_code="human_ingress_unconfigured")
        review = self._journal.get_review(review_handle)
        _require_allowed(
            self._authorizer.may_review(approver, self._review_view(review)),
            "approver may not request a human card for this review",
        )
        existing = self._journal.get_human_request_for_review(review_handle)
        if existing is not None:
            if expected_digest != existing.plan_digest:
                raise HumanDecisionRefused(reason_code="digest_mismatch")
            live = self._present(self._lazy_expire_human(existing))
            return HumanRequestResult(live.request_handle, live.state, True)
        review = self._lazy_expire_review(review)
        if review.state == "expired":
            raise HumanDecisionRefused(reason_code="review_expired")
        if review.state != "open":
            raise HumanDecisionRefused(reason_code="review_not_open")
        if expected_digest != review.plan_digest:
            raise HumanDecisionRefused(reason_code="digest_mismatch")
        if not self._authorizer.may_decide_human(
            registration.human_subject,
            review,
            self._chain_participants(review),
        ).allowed:
            raise HumanDecisionRefused(reason_code="no_human_approver")
        recomputed, summary = self._metadata_source.plan_summary(
            review.plan_handle, review.plan_digest
        )
        if recomputed != review.plan_digest:
            raise HostControlError(
                "plan summary digest does not match plan metadata"
            )
        summary = _closed_string(
            summary, "plan summary", maximum=MAX_PLAN_SUMMARY_LENGTH
        )
        if len(summary) > MAX_CARD_SUMMARY_LENGTH:
            summary = (
                summary[: MAX_CARD_SUMMARY_LENGTH - len(_TRUNCATION_MARKER)]
                + _TRUNCATION_MARKER
            )
        now = self._now()
        expires = min(
            datetime.fromisoformat(review.expires_at),
            now + self._human_card_ttl,
        )
        record = HumanRequestRecord(
            request_handle=self._new_handle(),
            review_handle=review_handle,
            requested_by=approver,
            request_idempotency_key=idempotency_key,
            plan_digest=review.plan_digest,
            target_seat_id=review.target_seat_id,
            operation_families=tuple(review.operation_families),
            redacted_summary=summary,
            nonce=self._new_handle(),
            state="pending",
            provider_message_ref=None,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
            presented_at=None,
            updated_at=now.isoformat(),
        )
        stored, replayed = self._journal.put_human_request_or_replay(
            record=record
        )
        live = self._present(stored)
        return HumanRequestResult(live.request_handle, live.state, replayed)

    def human_decision(
        self,
        authenticated_uid: int,
        review_handle: str,
        expected_digest: str,
        decision: str,
        source_event_handle: str,
        callback_nonce: str,
        idempotency_key: str,
    ) -> HumanDecisionResult:
        """Submit ONE provider-verified human decision (HumanIngress).

        The uid gate is FIRST and is the only source of identity: the
        bus-attested uid must equal the registered ingress uid, and the
        provider + human subject come from the root-owned registration —
        never from any argument (ORG-052 §5). Single-use: the nonce is
        consumed atomically with the decision in one durable transaction;
        a same-key identical replay returns the original outcome, any
        other reuse is refused."""

        registration = self._require_ingress_uid(authenticated_uid)
        review_handle = _closed_hex(review_handle, "review handle")
        expected_digest = _closed_hex(expected_digest, "plan digest")
        if decision not in ("granted", "denied"):
            raise HostControlError("invalid decision")
        source_event_handle = _closed_string(
            source_event_handle,
            "source event handle",
            maximum=MAX_SOURCE_EVENT_LENGTH,
        )
        callback_nonce = _closed_hex(callback_nonce, "callback nonce")
        idempotency_key = _closed_string(
            idempotency_key, "human decision idempotency key"
        )
        subject = registration.human_subject
        replay = self._human_replay(
            subject,
            idempotency_key,
            review_handle,
            expected_digest,
            decision,
            source_event_handle,
            callback_nonce,
        )
        if replay is not None:
            return replay
        request = self._journal.get_human_request_for_review(review_handle)
        if request is None:
            raise HumanDecisionRefused(reason_code="no_human_request")
        request = self._lazy_expire_human(request)
        if request.state == "pending":
            raise HumanDecisionRefused(reason_code="human_request_not_presented")
        if request.state == "expired":
            raise HumanDecisionRefused(reason_code="human_request_expired")
        if request.state != "presented":
            raise HumanDecisionRefused(reason_code="human_request_closed")
        if not _nonce_matches(request.nonce, callback_nonce):
            raise HumanDecisionRefused(reason_code="human_nonce_mismatch")
        review = self._journal.get_review(review_handle)
        if not (
            expected_digest == request.plan_digest == review.plan_digest
        ):
            raise HumanDecisionRefused(reason_code="digest_mismatch")
        review = self._lazy_expire_review(review)
        if review.state == "expired":
            raise HumanDecisionRefused(reason_code="review_expired")
        if review.state != "open":
            raise HumanDecisionRefused(reason_code="review_not_open")
        verdict = _require_allowed(
            self._authorizer.may_decide_human(
                subject, review, self._chain_participants(review)
            ),
            "human subject may not decide this review",
        )
        grant_ttl = self._grant_ttl
        if verdict.max_ttl is not None:
            grant_ttl = min(grant_ttl, verdict.max_ttl)
        now = self._now()
        attestation_handle = self._new_handle()
        reason_code = "approved" if decision == "granted" else "rejected"
        new_review = transition_review_record(
            review,
            decision,
            decided_at=now.isoformat(),
            decision=decision,
            reason_code=reason_code,
            approver_subject=subject,
            authority_source="human",
            audit_anchor=HUMAN_AUDIT_ANCHOR,
            decide_idempotency_key=idempotency_key,
            source_event=attestation_handle,
        )
        grant: GrantRecord | None = None
        if decision == "granted":
            grant = GrantRecord.create(
                schema=GRANT_SCHEMA,
                approval_handle=self._new_handle(),
                review_handle=review_handle,
                plan_handle=review.plan_handle,
                plan_digest=review.plan_digest,
                plan_owner=review.plan_owner,
                performer=review.performer,
                target_seat_id=review.target_seat_id,
                operation_families=review.operation_families,
                effect_digest=review.effect_digest,
                manifest_revision=review.manifest_revision,
                policy_revision=review.policy_revision,
                handoff_kind=review.handoff_kind,
                handoff=review.handoff,
                approver_subject=subject,
                authority_source="human",
                audit_anchor=HUMAN_AUDIT_ANCHOR,
                source_event=attestation_handle,
                decision="granted",
                reason_code=reason_code,
                issued_at=now.isoformat(),
                expires_at=(now + grant_ttl).isoformat(),
                nonce=self._new_handle(),
                idempotency_key=idempotency_key,
                state="issued",
                consuming_operation_handle=None,
                authority_record_id=None,
                updated_at=now.isoformat(),
            )
        attestation = AttestationRecord(
            attestation_handle=attestation_handle,
            review_handle=review_handle,
            request_handle=request.request_handle,
            provider=registration.provider,
            provider_user_id=registration.provider_user_id,
            provider_chat_id=registration.provider_chat_id,
            provider_message_id=request.provider_message_ref,
            callback_nonce=callback_nonce,  # the PRESENTED nonce: the journal keys consumption on it
            source_event=source_event_handle,
            decision=decision,
            human_subject=subject,
            idempotency_key=idempotency_key,
            plan_digest=request.plan_digest,
            created_at=now.isoformat(),
        )
        try:
            self._journal.consume_human_nonce_and_decide(
                request=request,
                review_current=review,
                review_new=new_review,
                grant=grant,
                attestation=attestation,
                payload={
                    "decision": decision,
                    "reason_code": reason_code,
                    "approver_subject": subject,
                    "authority_source": "human",
                    "attestation_handle": attestation_handle,
                    "approver_policy_revision": verdict.policy_revision,
                },
            )
        except StaleAuthorityState:
            # lost a serialized race: an identical concurrent submission
            # may have won (same key) — otherwise the nonce is spent
            replay = self._human_replay(
                subject,
                idempotency_key,
                review_handle,
                expected_digest,
                decision,
                source_event_handle,
                callback_nonce,
            )
            if replay is not None:
                return replay
            raise HumanDecisionRefused(reason_code="human_request_closed") from None
        return HumanDecisionResult(
            review_handle=review_handle,
            state=new_review.state,
            attestation_handle=attestation_handle,
            replayed=False,
        )

    def deliver_human_request(
        self,
        request_handle: str,
        on_settled: Settled | None = None,
    ) -> bool:
        """Start (or join) the asynchronous ``Present`` of a pending human
        request (T-5518). Returns ``True`` when ``on_settled`` WILL be
        called later with ``(request_handle, state)`` (or an error), on
        the loop thread; ``False`` when no async presenter is configured
        (the synchronous seam already ran inside ``request_human``).

        A failed or timed-out attempt is retried by the service itself
        with bounded backoff until the request is presented, expired or
        withdrawn: recovery never depends on a RequestHuman replay."""

        request_handle = _closed_hex(request_handle, "human request handle")
        if self._delivery is None:
            return False
        self._delivery.deliver(request_handle, on_settled)
        return True

    def resume_human_deliveries(self, *, limit: int = 100) -> int:
        """Re-arm delivery for every still-pending, unexpired human
        request (service start, after a restart). Idempotent: a request
        with an attempt in flight or a retry armed is not doubled.
        Returns how many pending requests were found."""

        if self._delivery is None:
            return 0
        handles = self._journal.pending_human_request_handles(
            self._now().isoformat(), limit=limit
        )
        for handle in handles:
            self._delivery.resume(handle)
        return len(handles)

    def resume_all_human_deliveries(self, *, page: int = 100) -> int:
        """Re-arm delivery for EVERY still-pending, unexpired human request
        (T-2920 U2, the T-5518 obligation at daemon start). Unlike
        ``resume_human_deliveries`` (one fixed first page: re-arming does
        not change a request's state until its async reply is processed, so
        repeating that call returns the same page forever), this walks the
        journal by a strictly advancing ``(created_at, request_handle)``
        keyset cursor and stops on the first short page, so it terminates by
        construction. Idempotent per request (``HumanCardDelivery.resume``).
        Returns how many pending requests were visited."""

        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise HostControlError("invalid resume page size")
        if self._delivery is None:
            return 0
        now_iso = self._now().isoformat()
        cursor: tuple[str, str] | None = None
        visited = 0
        while True:
            rows = self._journal.pending_human_request_page(
                now_iso, limit=page, after=cursor
            )
            for _created_at, handle in rows:
                self._delivery.resume(handle)
            visited += len(rows)
            if len(rows) < page:
                return visited
            cursor = rows[-1]

    def withdraw_human_request(
        self,
        subject: str,
        request_handle: str,
    ) -> HumanRequestResult:
        """In-process administrative withdrawal of a pending/presented
        card (T-2919 Q6; no D-Bus method — gate 2's inventory is locked).
        Gated by the policy's revokers list. A later callback for it is
        refused (``human_request_closed``) and the nonce is never
        consumable again."""

        subject = _closed_string(subject, "withdrawing subject")
        request_handle = _closed_hex(request_handle, "human request handle")
        _require_allowed(
            self._authorizer.may_withdraw(subject),
            "subject may not withdraw human requests",
        )
        request = self._lazy_expire_human(
            self._journal.get_human_request(request_handle)
        )
        if request.state not in ("pending", "presented"):
            raise HumanDecisionRefused(reason_code="human_request_closed")
        new = dataclasses.replace(
            request, state="withdrawn", updated_at=self._now().isoformat()
        )
        try:
            self._journal.transition_human_request(current=request, new=new)
        except StaleAuthorityState:
            raise HumanDecisionRefused(reason_code="human_request_closed") from None
        return HumanRequestResult(new.request_handle, new.state, False)

    def review_extras(self, review_handle: str) -> ReviewExtras:
        """Record-derived ReviewGet additions (T-2919 Q3). Callers MUST
        have authorized the reviewer first (``review_get`` does)."""

        review_handle = _closed_hex(review_handle, "review handle")
        grant = self._journal.get_grant_for_review(review_handle)
        request = self._journal.get_human_request_for_review(review_handle)
        if request is not None:
            request = self._lazy_expire_human(request)
        return ReviewExtras(
            approval_handle=None if grant is None else grant.approval_handle,
            human_request_handle=(
                None if request is None else request.request_handle
            ),
            human_request_state=None if request is None else request.state,
            human_request_expires_at=(
                None if request is None else request.expires_at
            ),
        )

    def _require_ingress_uid(
        self,
        authenticated_uid: object,
    ) -> HumanIngressRegistration:
        """The HumanIngress uid gate (domain tier; the transport gate in
        dbus_service is the other, deliberately duplicated, half)."""

        registration = self._human_ingress
        if (
            registration is None
            or not isinstance(authenticated_uid, int)
            or isinstance(authenticated_uid, bool)
            or authenticated_uid != registration.uid
        ):
            raise PolicyDenied("subject_denied")
        return registration

    def _human_replay(
        self,
        subject: str,
        idempotency_key: str,
        review_handle: str,
        expected_digest: str,
        decision: str,
        source_event_handle: str,
        callback_nonce: str,
    ) -> HumanDecisionResult | None:
        attestation = self._journal.find_attestation_by_key(
            subject, idempotency_key
        )
        if attestation is None:
            return None
        if not (
            attestation.review_handle == review_handle
            and attestation.decision == decision
            and attestation.source_event == source_event_handle
            and attestation.plan_digest == expected_digest
            and _nonce_matches(attestation.callback_nonce, callback_nonce)
        ):
            raise IdempotencyConflict("human decision idempotency conflict")
        review = self._journal.get_review(review_handle)
        return HumanDecisionResult(
            review_handle=review_handle,
            state=review.state,
            attestation_handle=attestation.attestation_handle,
            replayed=True,
        )

    def _present(self, request: HumanRequestRecord) -> HumanRequestRecord:
        """Deliver a still-pending card through the presenter seam.

        Failure of ANY kind leaves the record pending (fail closed: an
        undelivered card can never be decided)."""

        if request.state != "pending" or self._presenter is None:
            return request
        try:
            state, message_ref = self._presenter.present(
                *self._present_call(request).args()
            )
        except Exception:
            return request
        return self._record_presented(request, state, message_ref)

    @staticmethod
    def _present_call(request: HumanRequestRecord) -> PresentCall:
        return PresentCall(
            request.review_handle,
            request.plan_digest,
            canonical_json(request.card_summary()),
            request.nonce,
            request.expires_at,
        )

    def _delivery_load(self, request_handle: str) -> tuple[str, PresentCall | None]:
        request = self._lazy_expire_human(
            self._journal.get_human_request(request_handle)
        )
        if request.state != "pending":
            return (request.state, None)
        return (request.state, self._present_call(request))

    def _delivery_complete(
        self,
        request_handle: str,
        reply: PresentReply,
    ) -> tuple[str, str]:
        request = self._lazy_expire_human(
            self._journal.get_human_request(request_handle)
        )
        if reply.state:
            request = self._record_presented(
                request, reply.state, reply.message_ref
            )
        return (request.request_handle, request.state)

    def _record_presented(
        self,
        request: HumanRequestRecord,
        state: object,
        message_ref: object,
    ) -> HumanRequestRecord:
        """The guarded pending -> presented transition for one reported
        delivery; anything malformed (or a request no longer pending)
        leaves the record as it is (fail closed)."""

        if request.state != "pending":
            return request
        if (
            state != "presented"
            or not isinstance(message_ref, str)
            or not 1 <= len(message_ref) <= MAX_PROVIDER_MESSAGE_REF_LENGTH
            or not message_ref.isdigit()
        ):
            return request
        now = self._now().isoformat()
        new = dataclasses.replace(
            request,
            state="presented",
            provider_message_ref=message_ref,
            presented_at=now,
            updated_at=now,
        )
        try:
            self._journal.transition_human_request(current=request, new=new)
        except StaleAuthorityState:
            return self._journal.get_human_request(request.request_handle)
        return new

    def _lazy_expire_human(
        self,
        request: HumanRequestRecord,
    ) -> HumanRequestRecord:
        if request.state not in ("pending", "presented"):
            return request
        now = self._now()
        if not self._is_past(request.expires_at, now):
            return request
        new = dataclasses.replace(
            request, state="expired", updated_at=now.isoformat()
        )
        try:
            self._journal.transition_human_request(current=request, new=new)
        except StaleAuthorityState:
            return self._journal.get_human_request(request.request_handle)
        return new

    def _chain_participants(self, review: ReviewRecord) -> frozenset[str]:
        if review.handoff is None:
            return frozenset()
        return self._journal.handoff_chain_subjects(
            review.handoff.request_plan_handle
        )

    @staticmethod
    def _review_view(record: ReviewRecord) -> PlanMetadata:
        return PlanMetadata(
            owner=record.plan_owner,
            performer=record.performer,
            target_seat_id=record.target_seat_id,
            operation_families=record.operation_families,
            plan_digest=record.plan_digest,
            effect_digest=record.effect_digest,
            manifest_revision=record.manifest_revision,
            policy_revision=record.policy_revision,
        )

    # ------------------------------------------------------------------
    # consumer surface (HostApproval1.Consumer shape; broker uid only)
    # ------------------------------------------------------------------

    def validate_and_bind(
        self,
        authenticated_uid: int,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome:
        self._require_consumer(authenticated_uid)
        approval_handle = _closed_hex(approval_handle, "approval handle")
        plan_handle = _closed_hex(plan_handle, "plan handle")
        expected_digest = _closed_hex(expected_digest, "plan digest")
        operation_handle = _closed_hex(operation_handle, "operation handle")
        try:
            grant = self._journal.get_grant(approval_handle)
        except UnknownHandle:
            # fail-closed denial, not an exception: an unknown handle is a
            # terminal refusal for the requesting operation
            return self._deny("unknown_approval")
        return self._bind_or_classify(
            grant,
            plan_handle,
            expected_digest,
            operation_handle,
            self._now(),
        )

    def approval_status(
        self,
        authenticated_uid: int,
        approval_handle: str,
    ) -> ApprovalStatus:
        self._require_consumer(authenticated_uid)
        approval_handle = _closed_hex(approval_handle, "approval handle")
        grant = self._journal.get_grant(approval_handle)
        now = self._now()
        if grant.state == "issued" and self._is_past(grant.expires_at, now):
            grant = self._expire_grant(grant, now)
        return self._status_view(grant)

    def as_binder(self, authenticated_uid: int) -> ConsumerBinder:
        if (
            not isinstance(authenticated_uid, int)
            or isinstance(authenticated_uid, bool)
            or authenticated_uid < 0
        ):
            raise HostControlError("invalid consumer uid")
        return ConsumerBinder(self, authenticated_uid)

    # ------------------------------------------------------------------
    # administrative / maintenance
    # ------------------------------------------------------------------

    def revoke(
        self,
        subject: str,
        approval_handle: str,
        reason_code: str,
    ) -> ApprovalStatus:
        subject = _closed_string(subject, "revoking subject")
        approval_handle = _closed_hex(approval_handle, "approval handle")
        if reason_code not in REVOKE_REASON_CODES:
            raise HostControlError("invalid revoke reason code")
        grant = self._journal.get_grant(approval_handle)
        _require_allowed(
            self._authorizer.may_revoke(subject, grant),
            "subject may not revoke this grant",
        )
        if grant.state != "issued":
            # a bound grant is never "unconsumed": stopping the operation
            # is the broker's cancellation/rollback policy (ORG-052 §5)
            raise HostControlError(
                f"grant is {grant.state}; revocation applies only to "
                "issued grants"
            )
        now = self._now()
        if self._is_past(grant.expires_at, now):
            fresh = self._expire_grant(grant, now)
            raise HostControlError(
                f"grant is {fresh.state}; revocation applies only to "
                "issued grants"
            )
        new = transition_grant_record(
            grant, "revoked", updated_at=now.isoformat()
        )
        try:
            self._journal.transition_grant(
                current=grant,
                new=new,
                event_kind="revoked",
                payload={"subject": subject, "reason_code": reason_code},
            )
        except StaleAuthorityState as exc:
            fresh = self._journal.get_grant(approval_handle)
            raise HostControlError(
                f"grant is {fresh.state}; revocation lost the serialized race"
            ) from exc
        return self._status_view(new)

    def expire_due(self, *, limit: int = 100) -> tuple[str, ...]:
        """Bounded expiry sweep hook (L8).

        Nothing calls this yet: no daemon and no timer runs it. Expiry is
        enforced LAZILY at bind/decide/revoke/status time (an expired
        grant or review is refused there regardless of what state the
        journal still says), so a sweep only keeps ``pending_counts``
        accurate; it is not a safety mechanism. Returns the handles this
        sweep transitioned to ``expired``.

        Races with concurrent bind/revoke lose cleanly through the same
        state-guarded UPDATE (``StaleAuthorityState``) and that item is
        skipped; the sweep continues.

        Write-lock contention is different (T-4350): the sqlite write lock
        covers the whole database, so a ``JournalBusy`` on one item means
        every later item would wait out the same busy timeout, and a
        per-item skip would cost up to ``limit`` x that timeout. The FIRST
        ``JournalBusy`` therefore stops the sweep. It does not raise: the
        handles already transitioned are returned, so partial progress is
        never lost. Nothing was written for the item that hit busy, so it
        and every later due item stay due and are retried by the next
        sweep. Contention is not silent: ``expiry_sweep_status`` counts busy
        sweeps, so a sweeper that is permanently contended (and expires
        nothing) shows a growing ``consecutive_busy_sweeps``.

        The return value alone CANNOT show contention: a busy sweep returns
        a normal tuple, indistinguishable from "nothing was due". Whoever
        wires a sweeper MUST expose ``consecutive_busy_sweeps`` (via
        Health1) or the contention signal is lost.

        Only ``JournalBusy`` stops the sweep quietly. Any other error (for
        example a corrupted journal row) propagates: it is neither a clean
        nor a busy sweep, and the counters are left untouched.
        """

        now = self._now()
        due = self._journal.due_for_expiry(now.isoformat(), limit=limit)
        transitioned: list[str] = []
        busy = False
        for kind, handle in due:
            try:
                if kind == "review":
                    record = self._journal.get_review(handle)
                    if record.state != "open":
                        continue
                    self._journal.transition_review(
                        current=record,
                        new=transition_review_record(record, "expired"),
                        event_kind="expired",
                        payload={},
                        created_at=now.isoformat(),
                    )
                else:
                    grant = self._journal.get_grant(handle)
                    if grant.state != "issued":
                        continue
                    self._journal.transition_grant(
                        current=grant,
                        new=transition_grant_record(
                            grant, "expired", updated_at=now.isoformat()
                        ),
                        event_kind="expired",
                        payload={},
                    )
            except StaleAuthorityState:
                continue
            except JournalBusy:
                busy = True
                break
            transitioned.append(handle)
        if busy:
            self._busy_sweeps_total += 1
            self._consecutive_busy_sweeps += 1
        else:
            self._consecutive_busy_sweeps = 0
        return tuple(transitioned)

    def expiry_sweep_status(self) -> ExpirySweepStatus:
        """Bounded read-only expiry-sweep contention counters (T-4350).

        Provide-the-read, defer-the-wiring, like ``pending_counts``.
        Nothing calls ``expire_due`` or reads this yet (T-2914/T-2923 are
        done): whoever wires a sweeper MUST expose
        ``consecutive_busy_sweeps`` via Health1, because ``expire_due``'s
        return value cannot show contention. Expiry itself is enforced
        lazily at bind, so a stuck sweeper degrades count accuracy, not
        safety. In-memory for the life of this service object; a durable
        write is impossible in exactly the contended case.
        """

        return ExpirySweepStatus(
            busy_sweeps_total=self._busy_sweeps_total,
            consecutive_busy_sweeps=self._consecutive_busy_sweeps,
        )

    def pending_counts(self) -> ApprovalCounts:
        """Bounded read-only counters (ORG-068 monitor leg).

        Provide-the-read, defer-the-wiring: the AB-1 ``pending_operations``
        precedent. Nothing calls it yet (T-2914/T-2923 are done); whoever
        wires the monitor leg must expose it via Health1.
        """

        return self._journal.counts()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _bind_or_classify(
        self,
        grant: GrantRecord,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
        now: datetime,
        *,
        depth: int = 0,
    ) -> BindOutcome:
        if grant.state == "bound":
            if (
                grant.plan_handle == plan_handle
                and grant.plan_digest == expected_digest
                and grant.consuming_operation_handle == operation_handle
            ):
                if self._is_past(grant.expires_at, now):
                    # T-5233 (gate 16 lists expiry among the refusing
                    # fields): the identical-tuple replay honours the
                    # grant's expiry, so a deterministic operation handle
                    # (germination) cannot re-use a bound grant
                    # indefinitely. Zero writes: BOUND is terminal and
                    # stays BOUND. Consequence on the broker path:
                    # OperationService.recover() re-authorizes a PREPARED
                    # operation through this method, so a PREPARED op
                    # recovered after its grant's expiry is REFUSED with
                    # nothing dispatched (fail closed; a new review is
                    # needed) instead of resuming on the old binding.
                    return self._deny("expired")
                # identical tuple: return the ORIGINAL binding, zero writes
                return BindOutcome(
                    "bound", grant.authority_record_id, "bound_replay"
                )
            return self._deny("already_bound")
        if grant.state == "revoked":
            return self._deny("revoked")
        if grant.state == "expired":
            return self._deny("expired")
        # state == issued: args against the durable record first
        if grant.plan_handle != plan_handle:
            return self._deny_issued(grant, "plan_mismatch", now)
        if grant.plan_digest != expected_digest:
            return self._deny_issued(grant, "digest_mismatch", now)
        if self._is_past(grant.expires_at, now):
            expired = self._expire_grant(grant, now)
            if expired.state != "expired":
                # the expiry transition lost a serialized race; classify
                # against the committed winner instead of guessing
                if depth >= 2:
                    raise HostControlError("grant state is thrashing")
                return self._bind_or_classify(
                    expired,
                    plan_handle,
                    expected_digest,
                    operation_handle,
                    now,
                    depth=depth + 1,
                )
            return self._deny("expired")
        # re-fetch live plan metadata through the seam and compare EVERY
        # stored dimension (L5). MetadataUnavailable/UnknownHandle propagate:
        # fail closed without writing; the grant stays issued and retryable.
        metadata = self._metadata_source.plan_metadata(plan_handle)
        checks: tuple[tuple[str, bool], ...] = (
            # DERIVED-REDUNDANT diagnostics given plan_digest equality: a
            # mismatch here signals broker-side metadata inconsistency, not
            # a forging attacker (OQ1 disposition)
            (
                "metadata_digest_mismatch",
                metadata.plan_digest == grant.plan_digest,
            ),
            ("owner_mismatch", metadata.owner == grant.plan_owner),
            ("performer_mismatch", metadata.performer == grant.performer),
            (
                "target_mismatch",
                metadata.target_seat_id == grant.target_seat_id,
            ),
            (
                "family_mismatch",
                tuple(metadata.operation_families)
                == tuple(grant.operation_families),
            ),
            (
                "effect_mismatch",
                metadata.effect_digest == grant.effect_digest,
            ),
            (
                "manifest_revision_mismatch",
                metadata.manifest_revision == grant.manifest_revision,
            ),
            (
                # stored-revision vs live-revision COMPARISON only; policy
                # EVALUATION lives in approval.policy at review/decide time
                "policy_revision_mismatch",
                metadata.policy_revision == grant.policy_revision,
            ),
        )
        for code, matches in checks:
            if not matches:
                return self._deny_issued(grant, code, now)
        if grant.handoff_kind == "request_plan":
            # gate 23: the grant binds the request-plan tuple; compare the
            # stored link against a LIVE re-fetch. The handle comes from
            # the GRANT record, not the caller — an unknown/unreadable
            # request plan RAISES (UnknownHandle / MetadataUnavailable
            # propagate: broker-side inconsistency or outage must be loud
            # and leave the operation PREPARED-retryable; a denial would
            # terminally refuse it for an infrastructure fault). The
            # stored handoff_anchor has no live source and is deliberately
            # not compared here — it is bound context, not authority.
            link = grant.handoff
            request_live = self._metadata_source.plan_metadata(
                link.request_plan_handle
            )
            if (
                request_live.plan_digest != link.request_plan_digest
                or request_live.owner != link.request_plan_owner
                or request_live.effect_digest != link.shared_effect_digest
            ):
                return self._deny_issued(grant, "handoff_mismatch", now)
        authority_record_id = self._new_handle()
        new = transition_grant_record(
            grant,
            "bound",
            updated_at=now.isoformat(),
            consuming_operation_handle=operation_handle,
            authority_record_id=authority_record_id,
        )
        try:
            self._journal.transition_grant(
                current=grant,
                new=new,
                event_kind="bound",
                payload={
                    "operation_handle": operation_handle,
                    "authority_record_id": authority_record_id,
                },
            )
        except StaleAuthorityState:
            # lost the serialized race (L7): re-read the committed winner
            # and classify against it
            if depth >= 2:
                raise HostControlError("grant state is thrashing")
            fresh = self._journal.get_grant(grant.approval_handle)
            return self._bind_or_classify(
                fresh,
                plan_handle,
                expected_digest,
                operation_handle,
                now,
                depth=depth + 1,
            )
        except OperationConsumed:
            # PO mandate (c): one operation can never be authorized by two
            # grants — UNIQUE(consuming_operation_handle) fired
            return self._deny("operation_consumed")
        return BindOutcome("bound", authority_record_id, "bound")

    def _deny(self, reason_code: str) -> BindOutcome:
        if reason_code not in BIND_DENIAL_REASON_CODES:
            raise HostControlError("unknown bind denial reason code")
        return BindOutcome("denied", None, reason_code)

    def _deny_issued(
        self,
        grant: GrantRecord,
        reason_code: str,
        now: datetime,
    ) -> BindOutcome:
        """Denial on a still-issued grant: audit event, grant NOT consumed."""

        outcome = self._deny(reason_code)
        try:
            self._journal.append_grant_event(
                current=grant,
                event_kind="bind_denied",
                payload={"reason_code": reason_code},
                created_at=now.isoformat(),
            )
        except StaleAuthorityState:
            # the row moved concurrently; the denial stands, the audit
            # event is best-effort
            pass
        return outcome

    def _lazy_expire_review(self, record: ReviewRecord) -> ReviewRecord:
        if record.state != "open":
            return record
        now = self._now()
        if not self._is_past(record.expires_at, now):
            return record
        new = transition_review_record(record, "expired")
        try:
            self._journal.transition_review(
                current=record,
                new=new,
                event_kind="expired",
                payload={},
                created_at=now.isoformat(),
            )
        except StaleAuthorityState:
            return self._journal.get_review(record.review_handle)
        return new

    def _expire_grant(self, grant: GrantRecord, now: datetime) -> GrantRecord:
        new = transition_grant_record(
            grant, "expired", updated_at=now.isoformat()
        )
        try:
            self._journal.transition_grant(
                current=grant,
                new=new,
                event_kind="expired",
                payload={},
            )
        except StaleAuthorityState:
            return self._journal.get_grant(grant.approval_handle)
        return new

    def _status_view(self, grant: GrantRecord) -> ApprovalStatus:
        summary = (
            f"grant {grant.state}; target={grant.target_seat_id}; "
            f"families={','.join(grant.operation_families)}"
        )[:256]
        return ApprovalStatus(
            approval_handle=grant.approval_handle,
            state=grant.state,
            expires_at=grant.expires_at,
            summary=summary,
            plan_handle=grant.plan_handle,
            plan_owner=grant.plan_owner,
        )

    def _require_consumer(self, authenticated_uid: int) -> None:
        # ORG-052 §5: each service rechecks the live bus-derived uid. The
        # default-empty allowlist denies every caller (L9 fail-closed).
        if authenticated_uid not in self._consumer_uids:
            raise HostControlError("unregistered consumer uid")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise HostControlError(
                "approval clock must return aware datetime"
            )
        return now.astimezone(timezone.utc)

    def _new_handle(self) -> str:
        handle = self._handle_factory()
        try:
            return _closed_hex(handle, "generated approval handle")
        except HostControlError:
            raise HostControlError("invalid generated approval handle")

    @staticmethod
    def _is_past(stamp: str, now: datetime) -> bool:
        try:
            expires = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise HostControlError("invalid expiry timestamp") from exc
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise HostControlError("invalid expiry timestamp")
        return expires <= now
