"""Approval-owned typed contracts (ORG-052 §5 — T-2910 / AB-2).

This module is the approval authority's OWN record layer. It deliberately
lives outside the broker's shared ``tools/host_control/contracts.py`` so the
broker tree never even names a grant-record type, let alone constructs one
(the broker knows only the value-level ``ApprovalBinder`` Protocol and its
``BindOutcome`` return, which stay in the shared module because they genuinely
cross the boundary).

Separation claim, labeled precisely (do not over-read it):

- SOURCE-DECLARATION level: no module under ``approval/`` imports the broker's
  ``journal`` / ``operations`` / ``planner`` / ``policy`` / ``registry`` /
  ``trusted_files`` modules. (Python import mechanics still execute the
  parent-package ``__init__`` — ``sys.modules`` reachability is conceded, not
  claimed.)
- CAPABILITY level: no constructor parameter, attribute, or method signature
  in this package accepts a broker journal handle or broker DB path.
- DATA level: the approval journal and the broker journal carry distinct
  SQLite ``application_id`` markers and each ``open()`` loudly refuses the
  other's database file.
- DEPLOYMENT level (separate Unix uids, root-owned units, file ownership) is
  NOT proven by this unit; it is owned by T-2913 + T-2920.

The pure helpers (``canonical_json`` / ``sha256_json`` / the bounded-string
and hex validators) are imported from the shared contracts module rather than
re-implemented: a duplicated validator that drifts is worse than an
intra-package private import.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Mapping

from tools.host_control.contracts import (
    HostControlError,
    _closed_hex,
    _closed_string,
    _jsonable,
    sha256_json,
)


ReviewState = Literal["open", "granted", "denied", "expired"]
GrantState = Literal["issued", "bound", "revoked", "expired"]
ReviewDecision = Literal["granted", "denied"]
# Schema slot only in this unit: every decision this unit can mint is
# 'agent'-sourced; 'human' is populated by T-2919 and 'operator' by the
# operator-CLI leg. Directed relaxation — a slot today costs a word, a
# migration later costs a unit.
AuthoritySource = Literal["agent", "human", "operator"]
HandoffKind = Literal["direct_no_handoff", "request_plan"]

REVIEW_SCHEMA = "pantheon-approval-review/v1"
GRANT_SCHEMA = "pantheon-approval-grant/v1"

REVIEW_STATES = frozenset(("open", "granted", "denied", "expired"))
GRANT_STATES = frozenset(("issued", "bound", "revoked", "expired"))

# Closed transition tables (ORG-052 §5). Terminals have no outgoing edges.
# BOUND is the single-use authorization commit and never becomes reusable;
# denied / revoked / expired never become reusable — any later attempt
# requires a NEW review.
REVIEW_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "open": frozenset(("granted", "denied", "expired")),
    "granted": frozenset(),
    "denied": frozenset(),
    "expired": frozenset(),
}
GRANT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "issued": frozenset(("bound", "revoked", "expired")),
    "bound": frozenset(),
    "revoked": frozenset(),
    "expired": frozenset(),
}
TERMINAL_REVIEW_STATES = frozenset(("granted", "denied", "expired"))
TERMINAL_GRANT_STATES = frozenset(("bound", "revoked", "expired"))

# Closed reason-code sets are SERVICE-tier (Python), while the small ratified
# state enums are SQL CHECK constraints: widening a reason code must never
# require a schema migration, but a state can never drift even under direct
# SQL (directed relaxation — the AB-1 'verify' enum-slot logic).
DECIDE_REASON_CODES = frozenset(("approved", "rejected"))
REVOKE_REASON_CODES = frozenset(("revoked_by_admin",))
BIND_SUCCESS_REASON_CODES = frozenset(("bound", "bound_replay"))
BIND_DENIAL_REASON_CODES = frozenset(
    (
        "unknown_approval",
        "plan_mismatch",
        "digest_mismatch",
        "metadata_digest_mismatch",
        "owner_mismatch",
        "performer_mismatch",
        "target_mismatch",
        "family_mismatch",
        "effect_mismatch",
        "manifest_revision_mismatch",
        "policy_revision_mismatch",
        "handoff_mismatch",
        "expired",
        "revoked",
        "already_bound",
        "operation_consumed",
    )
)
# Approver-policy denial codes (T-2911, ORG-052 §5 "Approver policy and
# separation of duty"). Closed at the PolicyVerdict boundary: a verdict
# carrying any other code refuses to construct. Service-tier (Python) per
# the directed-relaxation pattern — widening never needs a migration.
POLICY_DENIAL_REASON_CODES = frozenset(
    (
        # generic denials
        "approver_denied",
        "deny_all_default",
        # roster / routing (fail-closed: absence is a DENIAL, never permit)
        "unknown_approver",
        "unknown_subject",
        "unknown_operation_family",
        "unknown_target_seat",
        "family_not_granted",
        "seat_class_not_granted",
        "human_decision_required",
        "source_not_permitted",
        # separation of duty (gate 13)
        "approver_is_plan_owner",
        "approver_is_performer",
        "approver_is_request_plan_owner",
        "approver_shares_trust_domain",
        "approver_is_handoff_participant",
        # administrative
        "revoke_not_permitted",
    )
)
# Review-time cross-persona link refusals (T-2911, gate 23): the request
# plan and the performer plan must MATCH on target, manifest revision,
# operation families, and effect digest before review proceeds, and the
# link shape itself must be sane. Raised via HandoffMismatch BEFORE any
# summary disclosure.
HANDOFF_LINK_REASON_CODES = frozenset(
    (
        "handoff_self_reference",
        "handoff_requester_is_performer",
        "handoff_target_mismatch",
        "handoff_manifest_mismatch",
        "handoff_family_mismatch",
        "handoff_effect_mismatch",
    )
)
APPROVAL_REASON_CODES = (
    DECIDE_REASON_CODES
    | REVOKE_REASON_CODES
    | BIND_SUCCESS_REASON_CODES
    | BIND_DENIAL_REASON_CODES
    | POLICY_DENIAL_REASON_CODES
    | HANDOFF_LINK_REASON_CODES
)

# Gate-16 completeness contract: every GrantRecord field is EITHER a denial
# dimension (mutating exactly that element at bind time yields exactly that
# reason code) OR an explicitly documented exclusion. The matrix test derives
# its case list from dataclasses.fields(GrantRecord) against these two maps,
# so adding a field later FAILS the test instead of silently escaping
# coverage.
#
# Honesty notes (gate 16, kept in shipped source per PO mandate (g)):
# - 'nonce' is SERVER-side and is not a ValidateAndBind argument; its denial
#   surface is identical to gate 16's "operation" dimension — a consumed
#   (BOUND) grant refuses any different operation handle. There is no nonce
#   parameter to mutate; fabricating one would make the matrix look
#   symmetric while proving nothing.
# - 'policy_revision' is a stored-revision vs live-revision COMPARISON, not
#   policy evaluation. Policy evaluation is the approval-side approver
#   policy (tools.host_control.approval.policy), enforced at review/decide
#   time — the bind comparison here stays a revision equality check.
# - 'effect_digest' and 'manifest_revision' are DERIVED-REDUNDANT given
#   plan_digest equality (both are functions of the pinned server-owned
#   plan): a mismatch signals broker-side metadata inconsistency, not a
#   forging attacker. They keep distinct diagnostic reason codes precisely
#   because they distinguish that failure class.
# - 'handoff' compares the stored link tuple (request plan digest, owner,
#   shared effect digest) against a live re-fetch of the request plan
#   through the metadata seam. The handle comes from the GRANT record, not
#   the caller, so an UNKNOWN/unreadable request plan RAISES (broker-side
#   inconsistency/outage; a denial would terminally refuse the broker's
#   operation for an infrastructure fault — the AB-2 decision-22/23 shape).
#   'handoff_anchor' inside the link is issuance-bound audit context with
#   NO live source; it is deliberately not a live comparison — its
#   non-authority is proven by the verdict-independence test.
BIND_DENIAL_DIMENSIONS: Mapping[str, str] = {
    "approval_handle": "unknown_approval",
    "plan_handle": "plan_mismatch",
    "plan_digest": "digest_mismatch",
    "plan_owner": "owner_mismatch",
    "performer": "performer_mismatch",
    "target_seat_id": "target_mismatch",
    "operation_families": "family_mismatch",
    "effect_digest": "effect_mismatch",
    "manifest_revision": "manifest_revision_mismatch",
    "policy_revision": "policy_revision_mismatch",
    "handoff": "handoff_mismatch",
    "expires_at": "expired",
    "state": "revoked",
    "nonce": "already_bound",
}
BIND_DIMENSION_EXCLUSIONS: Mapping[str, str] = {
    "schema": "record mechanics; tamper surface is the corrupt-record tests",
    "review_handle": (
        "provenance linkage to the review; not a bind-time comparison"
    ),
    "handoff_kind": (
        "discriminator selecting whether the stored link is compared at "
        "bind time; it has no live counterpart of its own — the link "
        "CONTENTS are the compared dimension (see 'handoff'); its tamper "
        "surface is the corrupt-record tests and the paired-nullability "
        "SQL CHECK"
    ),
    "approver_subject": (
        "decision provenance fixed at issuance; approver policy is T-2911"
    ),
    "authority_source": (
        "decision provenance fixed at issuance; human/operator sources are "
        "T-2919 / operator-CLI legs"
    ),
    "audit_anchor": "bounded audit context, not authority (ORG-052 §5)",
    "source_event": (
        "provider-authenticated event pointer, populated only by T-2919; "
        "structurally NULL for agent decisions and never caller-supplied"
    ),
    "decision": (
        "structurally 'granted' by SQL CHECK; a grants row cannot encode a "
        "denial"
    ),
    "reason_code": "issuance provenance, not a bind input",
    "issued_at": "issuance timestamp, not a bind comparison",
    "idempotency_key": "decide-idempotency surface; covered by gate-24 tests",
    "consuming_operation_handle": (
        "bind OUTPUT; single-use surface is the nonce dimension plus the "
        "operation_consumed cross-grant test"
    ),
    "authority_record_id": (
        "bind OUTPUT; identical-tuple replay returns the stored id"
    ),
    "updated_at": "record mechanics",
    "digest": "record mechanics; tamper surface is the corrupt-record tests",
}


class ApproverDenied(HostControlError):
    """The fail-closed authorizer seam denied the approver/administrator.

    Distinct from a VALID 'denied' decision: this is the approver lacking
    authority to act at all (the shipped default authorizer denies
    everything), not the approver validly rejecting a plan.

    Carries the closed policy reason code so every denial in the system is
    attributable (T-2911); constructing one with an unregistered code
    refuses loudly.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "approver_denied",
    ) -> None:
        if reason_code not in POLICY_DENIAL_REASON_CODES:
            raise HostControlError("unknown policy denial reason code")
        super().__init__(message)
        self.reason_code = reason_code


class HandoffMismatch(HostControlError):
    """Cross-persona handoff linkage failed validation (ORG-052 §5, gate 23).

    Raised at review time BEFORE any plan-summary disclosure. Carries one
    closed HANDOFF_LINK_REASON_CODES member; each tuple dimension (target,
    manifest revision, operation families, effect digest) and each link
    shape fault (self-reference, requester-is-performer) has its own code.
    """

    def __init__(self, message: str, *, reason_code: str) -> None:
        if reason_code not in HANDOFF_LINK_REASON_CODES:
            raise HostControlError("unknown handoff mismatch reason code")
        super().__init__(message)
        self.reason_code = reason_code


class MetadataUnavailable(HostControlError):
    """The broker-side plan-metadata source is unreachable.

    Deliberately distinct from the shared ``ApprovalUnavailable`` (which
    names the APPROVAL authority being down, from the broker's view). On
    this error the bind refuses without writing anything: the grant stays
    ISSUED and retryable — fail closed, never fail open.
    """


class StaleAuthorityState(HostControlError):
    """A state-guarded conditional UPDATE moved zero rows.

    The caller lost a serialized revoke/bind/expire race; re-read the
    committed winner and classify.
    """


class OperationConsumed(HostControlError):
    """The UNIQUE(consuming_operation_handle) constraint fired.

    A second grant attempted to bind an operation handle already consumed
    by a first grant; one operation can never be authorized by two grants.
    """


@dataclass(frozen=True)
class PlanMetadata:
    """Value mirror of ``HostBroker1.ApprovalSource.PlanMetadata`` (ORG-052
    §5, wire signature ``"s" -> "ssssssss"``).

    ``operation_families`` is a tuple at value tier for consistency with the
    shared ``OperationRecord``; flattening to the single scalar "family"
    wire slot is the Unit-3 D-Bus adapter's concern.
    """

    owner: str
    performer: str
    target_seat_id: str
    operation_families: tuple[str, ...]
    plan_digest: str
    effect_digest: str
    manifest_revision: str
    policy_revision: str


@dataclass(frozen=True)
class HandoffLink:
    """Request-plan linkage for a cross-persona handoff (ORG-052 §5).

    Every identity field is SERVER-derived at review time: requester
    identity comes only from the request plan's owner as returned by the
    broker metadata seam — never from a caller argument, role name,
    mailbox label, or ticket anchor. ``handoff_anchor`` is the bounded
    dispatch/ticket anchor: caller-supplied AUDIT CONTEXT in the same
    class as ``audit_anchor`` — bound into the grant for traceability,
    never authority, never an input to identity derivation or any policy
    verdict (T-2911; the verdict-independence test proves it).
    """

    request_plan_handle: str
    request_plan_digest: str
    request_plan_owner: str
    shared_effect_digest: str
    handoff_anchor: str | None


def _validate_handoff_pair(kind: str, handoff: HandoffLink | None) -> None:
    if kind not in ("direct_no_handoff", "request_plan"):
        raise HostControlError("invalid handoff kind")
    if (kind == "request_plan") != (handoff is not None):
        raise HostControlError("handoff link does not match handoff kind")
    if handoff is not None:
        _closed_hex(handoff.request_plan_handle, "request plan handle")
        _closed_hex(handoff.request_plan_digest, "request plan digest")
        _closed_string(handoff.request_plan_owner, "request plan owner")
        _closed_hex(handoff.shared_effect_digest, "shared effect digest")
        if handoff.handoff_anchor is not None:
            _closed_string(
                handoff.handoff_anchor,
                "handoff anchor",
                maximum=256,
            )


def _validate_families(families: tuple[str, ...]) -> None:
    if not families:
        raise HostControlError("empty operation family set")
    for family in families:
        _closed_string(family, "operation family")


def _validate_stamp(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise HostControlError(f"invalid {field_name}")


def _handoff_from_raw(raw: object) -> HandoffLink | None:
    if raw is None:
        return None
    expected = {item.name for item in dataclasses.fields(HandoffLink)}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise HostControlError("invalid handoff link fields")
    anchor = raw["handoff_anchor"]
    return HandoffLink(
        request_plan_handle=str(raw["request_plan_handle"]),
        request_plan_digest=str(raw["request_plan_digest"]),
        request_plan_owner=str(raw["request_plan_owner"]),
        shared_effect_digest=str(raw["shared_effect_digest"]),
        handoff_anchor=None if anchor is None else str(anchor),
    )


@dataclass(frozen=True)
class ReviewRecord:
    """Durable review truth (``OPEN -> GRANTED | DENIED | EXPIRED``).

    ``requested_by`` is the approver subject who OPENED the review;
    ``approver_subject`` is the one who DECIDED it (usually the same, but
    the record keeps them distinct — the field set drives the gate-16
    completeness map and separation-of-duty checks land in T-2911).

    ``audit_anchor`` is bounded audit context, NOT authority.
    ``source_event`` is the provider-authenticated event pointer that only
    T-2919's human ingress may populate; it is structurally NULL for agent
    decisions and there is no service parameter that can set it.
    """

    schema: str
    review_handle: str
    plan_handle: str
    plan_digest: str
    plan_owner: str
    performer: str
    target_seat_id: str
    operation_families: tuple[str, ...]
    effect_digest: str
    manifest_revision: str
    policy_revision: str
    handoff_kind: HandoffKind
    handoff: HandoffLink | None
    requested_by: str
    state: ReviewState
    decision: ReviewDecision | None
    reason_code: str | None
    approver_subject: str | None
    authority_source: AuthoritySource | None
    audit_anchor: str | None
    source_event: str | None
    decide_idempotency_key: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    digest: str

    def digest_payload(self) -> Mapping[str, object]:
        value = dict(self.to_dict())
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: object) -> "ReviewRecord":
        candidate = cls(digest="", **kwargs)  # type: ignore[arg-type]
        digest = sha256_json(candidate.digest_payload())
        record = dataclasses.replace(candidate, digest=digest)
        record._validate_shape()
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ReviewRecord":
        expected = {item.name for item in dataclasses.fields(cls)}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise HostControlError("invalid review record fields")
        try:
            candidate = cls(
                schema=str(raw["schema"]),
                review_handle=str(raw["review_handle"]),
                plan_handle=str(raw["plan_handle"]),
                plan_digest=str(raw["plan_digest"]),
                plan_owner=str(raw["plan_owner"]),
                performer=str(raw["performer"]),
                target_seat_id=str(raw["target_seat_id"]),
                operation_families=tuple(
                    raw["operation_families"]  # type: ignore[arg-type]
                ),
                effect_digest=str(raw["effect_digest"]),
                manifest_revision=str(raw["manifest_revision"]),
                policy_revision=str(raw["policy_revision"]),
                handoff_kind=str(raw["handoff_kind"]),  # type: ignore[arg-type]
                handoff=_handoff_from_raw(raw["handoff"]),
                requested_by=str(raw["requested_by"]),
                state=str(raw["state"]),  # type: ignore[arg-type]
                decision=_optional_str(raw["decision"]),  # type: ignore[arg-type]
                reason_code=_optional_str(raw["reason_code"]),
                approver_subject=_optional_str(raw["approver_subject"]),
                authority_source=_optional_str(  # type: ignore[arg-type]
                    raw["authority_source"]
                ),
                audit_anchor=_optional_str(raw["audit_anchor"]),
                source_event=_optional_str(raw["source_event"]),
                decide_idempotency_key=_optional_str(
                    raw["decide_idempotency_key"]
                ),
                created_at=str(raw["created_at"]),
                expires_at=str(raw["expires_at"]),
                decided_at=_optional_str(raw["decided_at"]),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostControlError("invalid review record") from exc
        candidate._validate_shape()
        return candidate

    def _validate_shape(self) -> None:
        if self.schema != REVIEW_SCHEMA:
            raise HostControlError("invalid review schema")
        _closed_hex(self.review_handle, "review handle")
        _closed_hex(self.plan_handle, "review plan handle")
        _closed_hex(self.plan_digest, "review plan digest")
        _closed_hex(self.effect_digest, "review effect digest")
        for name in (
            self.plan_owner,
            self.performer,
            self.target_seat_id,
            self.manifest_revision,
            self.policy_revision,
            self.requested_by,
        ):
            _closed_string(name, "review identifier")
        _validate_families(self.operation_families)
        _validate_handoff_pair(self.handoff_kind, self.handoff)
        if self.state not in REVIEW_STATES:
            raise HostControlError("invalid review state")
        decided = self.state in ("granted", "denied")
        if decided != (self.decision is not None):
            raise HostControlError("review decision does not match state")
        if decided:
            if self.decision != self.state:
                raise HostControlError("review decision does not match state")
            if self.reason_code not in DECIDE_REASON_CODES:
                raise HostControlError("invalid review reason code")
            _closed_string(self.approver_subject, "review approver subject")
            if self.authority_source not in ("agent", "human", "operator"):
                raise HostControlError("invalid review authority source")
            _closed_string(self.audit_anchor, "review audit anchor", maximum=256)
            _closed_string(
                self.decide_idempotency_key,
                "review decide idempotency key",
            )
            _validate_stamp(self.decided_at, "review decided_at")
        else:
            for value in (
                self.reason_code,
                self.approver_subject,
                self.authority_source,
                self.audit_anchor,
                self.source_event,
                self.decide_idempotency_key,
                self.decided_at,
            ):
                if value is not None:
                    raise HostControlError(
                        "undecided review carries decision fields"
                    )
        if self.authority_source == "agent" and self.source_event is not None:
            raise HostControlError(
                "agent decisions never carry a provider source event"
            )
        if self.source_event is not None:
            _closed_string(self.source_event, "review source event", maximum=256)
        _validate_stamp(self.created_at, "review created_at")
        _validate_stamp(self.expires_at, "review expires_at")

    def validate_digest(self) -> bool:
        try:
            self._validate_shape()
        except HostControlError:
            return False
        return sha256_json(self.digest_payload()) == self.digest


@dataclass(frozen=True)
class GrantRecord:
    """Durable grant truth (``ISSUED -> BOUND | REVOKED | EXPIRED``).

    Every element of the ORG-052 §5 grant record, plus ``effect_digest`` and
    ``manifest_revision`` as first-class bind-compared dimensions (L5
    disposition, OQ1-confirmed): both are DERIVED-REDUNDANT given
    plan_digest equality, so their mismatch codes diagnose broker-side
    metadata inconsistency rather than defend against a forging attacker.

    ``nonce`` is the server-side single-use nonce: it never crosses a public
    service boundary (gate 21) and its consumption IS the BOUND commit.
    ``authority_record_id`` is minted exactly once at BOUND and is what
    ``BindOutcome`` returns; identical-tuple replay returns the stored id.
    """

    schema: str
    approval_handle: str
    review_handle: str
    plan_handle: str
    plan_digest: str
    plan_owner: str
    performer: str
    target_seat_id: str
    operation_families: tuple[str, ...]
    effect_digest: str
    manifest_revision: str
    policy_revision: str
    handoff_kind: HandoffKind
    handoff: HandoffLink | None
    approver_subject: str
    authority_source: AuthoritySource
    audit_anchor: str
    source_event: str | None
    decision: str
    reason_code: str
    issued_at: str
    expires_at: str
    nonce: str
    idempotency_key: str
    state: GrantState
    consuming_operation_handle: str | None
    authority_record_id: str | None
    updated_at: str
    digest: str

    def digest_payload(self) -> Mapping[str, object]:
        value = dict(self.to_dict())
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: object) -> "GrantRecord":
        candidate = cls(digest="", **kwargs)  # type: ignore[arg-type]
        digest = sha256_json(candidate.digest_payload())
        record = dataclasses.replace(candidate, digest=digest)
        record._validate_shape()
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "GrantRecord":
        expected = {item.name for item in dataclasses.fields(cls)}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise HostControlError("invalid grant record fields")
        try:
            candidate = cls(
                schema=str(raw["schema"]),
                approval_handle=str(raw["approval_handle"]),
                review_handle=str(raw["review_handle"]),
                plan_handle=str(raw["plan_handle"]),
                plan_digest=str(raw["plan_digest"]),
                plan_owner=str(raw["plan_owner"]),
                performer=str(raw["performer"]),
                target_seat_id=str(raw["target_seat_id"]),
                operation_families=tuple(
                    raw["operation_families"]  # type: ignore[arg-type]
                ),
                effect_digest=str(raw["effect_digest"]),
                manifest_revision=str(raw["manifest_revision"]),
                policy_revision=str(raw["policy_revision"]),
                handoff_kind=str(raw["handoff_kind"]),  # type: ignore[arg-type]
                handoff=_handoff_from_raw(raw["handoff"]),
                approver_subject=str(raw["approver_subject"]),
                authority_source=str(  # type: ignore[arg-type]
                    raw["authority_source"]
                ),
                audit_anchor=str(raw["audit_anchor"]),
                source_event=_optional_str(raw["source_event"]),
                decision=str(raw["decision"]),
                reason_code=str(raw["reason_code"]),
                issued_at=str(raw["issued_at"]),
                expires_at=str(raw["expires_at"]),
                nonce=str(raw["nonce"]),
                idempotency_key=str(raw["idempotency_key"]),
                state=str(raw["state"]),  # type: ignore[arg-type]
                consuming_operation_handle=_optional_str(
                    raw["consuming_operation_handle"]
                ),
                authority_record_id=_optional_str(raw["authority_record_id"]),
                updated_at=str(raw["updated_at"]),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HostControlError("invalid grant record") from exc
        candidate._validate_shape()
        return candidate

    def _validate_shape(self) -> None:
        if self.schema != GRANT_SCHEMA:
            raise HostControlError("invalid grant schema")
        _closed_hex(self.approval_handle, "approval handle")
        _closed_hex(self.review_handle, "grant review handle")
        _closed_hex(self.plan_handle, "grant plan handle")
        _closed_hex(self.plan_digest, "grant plan digest")
        _closed_hex(self.effect_digest, "grant effect digest")
        _closed_hex(self.nonce, "grant nonce")
        for name in (
            self.plan_owner,
            self.performer,
            self.target_seat_id,
            self.manifest_revision,
            self.policy_revision,
            self.approver_subject,
            self.idempotency_key,
        ):
            _closed_string(name, "grant identifier")
        _validate_families(self.operation_families)
        _validate_handoff_pair(self.handoff_kind, self.handoff)
        if self.authority_source not in ("agent", "human", "operator"):
            raise HostControlError("invalid grant authority source")
        _closed_string(self.audit_anchor, "grant audit anchor", maximum=256)
        if self.authority_source == "agent" and self.source_event is not None:
            raise HostControlError(
                "agent decisions never carry a provider source event"
            )
        if self.source_event is not None:
            _closed_string(self.source_event, "grant source event", maximum=256)
        if self.decision != "granted":
            raise HostControlError("a grant record cannot encode a denial")
        if self.reason_code not in DECIDE_REASON_CODES:
            raise HostControlError("invalid grant reason code")
        if self.state not in GRANT_STATES:
            raise HostControlError("invalid grant state")
        bound = self.state == "bound"
        if bound != (self.consuming_operation_handle is not None):
            raise HostControlError(
                "consuming operation handle does not match grant state"
            )
        if bound != (self.authority_record_id is not None):
            raise HostControlError(
                "authority record id does not match grant state"
            )
        if bound:
            _closed_hex(
                self.consuming_operation_handle,
                "grant consuming operation handle",
            )
            _closed_hex(self.authority_record_id, "grant authority record id")
        _validate_stamp(self.issued_at, "grant issued_at")
        _validate_stamp(self.expires_at, "grant expires_at")
        _validate_stamp(self.updated_at, "grant updated_at")

    def validate_digest(self) -> bool:
        try:
            self._validate_shape()
        except HostControlError:
            return False
        return sha256_json(self.digest_payload()) == self.digest


# Bounded redacted plan summaries (gate 12): the seam's summary is capped
# at this length at the service boundary. ALIGNED with the wire-tier JSON
# limit (65536) that both services' wire modules already enforce on every
# JSON payload: the summary this service consumes is PRODUCED under that
# wire contract, so a domain bound tighter than the wire it consumes from
# was a latent production defect — the original empirical 1024 refused
# the real broker plan summary (measured 3134 bytes; T-2913 cross-service
# integration finding). Any future divergence from the wire limit must be
# deliberate: the alignment is test-pinned (T-2913).
MAX_PLAN_SUMMARY_LENGTH = 65536


@dataclass(frozen=True)
class PolicyVerdict:
    """Closed authorization verdict from the approver-policy seam (T-2911).

    ``reason_code`` is ``"authorized"`` on allow and a
    POLICY_DENIAL_REASON_CODES member on deny — closed structurally at
    construction, so no authorizer can smuggle an unregistered code.
    ``max_ttl`` is populated only on allowed DECIDE verdicts: the policy's
    TTL ceiling for the grant (ORG-052 §5 "maximum TTL"); the service
    issues the TIGHTER of this and its configured grant TTL.

    ``policy_revision`` is the APPROVER-policy revision that produced the
    verdict (P4: the decision event records which policy version
    authorized it). Distinct from the broker policy revision carried on
    plan metadata and grant records.
    """

    allowed: bool
    reason_code: str
    max_ttl: timedelta | None = None
    policy_revision: str | None = None

    def __post_init__(self) -> None:
        if self.allowed:
            if self.reason_code != "authorized":
                raise HostControlError(
                    "allowed verdict must carry reason code 'authorized'"
                )
        else:
            if self.reason_code not in POLICY_DENIAL_REASON_CODES:
                raise HostControlError("unknown policy denial reason code")
            if self.max_ttl is not None:
                raise HostControlError("denied verdict cannot carry a ttl")
        if self.max_ttl is not None and self.max_ttl <= timedelta(0):
            raise HostControlError("verdict ttl must be positive")


@dataclass(frozen=True)
class ReviewResult:
    """Bounded public result of Review (ORG-052 §5 wire contract: review
    handle, state, recomputed digest, bounded redacted plan summary — the
    record carries the first three).

    ``plan_summary`` is fetched ONLY after the approver has been authorized
    against minimal metadata and the handoff link (if any) has validated —
    metadata-first disclosure, gate 12. A frozen view rather than a bare
    tuple, consistent with DecisionResult/ApprovalStatus (PO override 3).
    """

    record: ReviewRecord
    replayed: bool
    plan_summary: str


@dataclass(frozen=True)
class DecisionResult:
    """Bounded public view of a decision (PO mandate (b), gate 21).

    Mirrors the ADR Decide contract: state plus an opaque approval handle
    only when granted. Never the grant record, never the nonce, never the
    authority record id.
    """

    review_handle: str
    state: ReviewState
    decision: ReviewDecision
    reason_code: str
    approval_handle: str | None


@dataclass(frozen=True)
class ApprovalStatus:
    """Bounded state, NEVER authority (ORG-052 §5 ApprovalStatus).

    By construction this type carries no authority record id and no nonce:
    the broker cannot substitute a status read for binding.

    ``plan_handle`` and ``plan_owner`` are the grant's plan binding (T-4875,
    ORG-052 §4 "handle bound to subject"): they let the broker refuse a
    status read whose grant is not bound to the caller's own plan. Both are
    already-persisted ``GrantRecord`` fields; neither is authority.
    """

    approval_handle: str
    state: GrantState
    expires_at: str
    summary: str
    plan_handle: str
    plan_owner: str


@dataclass(frozen=True)
class ApprovalCounts:
    """Bounded read-only counters for the monitor leg (ORG-068).

    Provide-the-read, defer-the-wiring: the AB-1 ``pending_operations()``
    precedent. Wiring is owned by T-2914 + T-2923.
    """

    open_reviews: int
    issued_grants: int
    bound_grants: int


@dataclass(frozen=True)
class ExpirySweepStatus:
    """Bounded read-only contention signal for the expiry sweeper (T-4350).

    In-memory, per service instance (the long-lived daemon object): a
    durable write is impossible in exactly the contended case, and a
    restart merely re-accumulates. Provide-the-read, defer-the-wiring, like
    ``ApprovalCounts``: whoever wires a sweeper must expose
    ``consecutive_busy_sweeps`` via Health1.
    """

    busy_sweeps_total: int
    consecutive_busy_sweeps: int


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
