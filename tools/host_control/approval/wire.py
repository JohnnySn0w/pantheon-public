"""Wire-tier contract for the ``org.pantheon.HostApproval1`` D-Bus
service (ORG-052 §5, T-2913).

This module deliberately imports NO dbus binding (the AB-4/T-2912
split): the closed error namespace, its total exception mapping, the
ValidateAndBind outcome mapping, and the bounded JSON serializations
must all be testable on a runner without dbus-python. The one approval
module that needs dbus is ``tools.host_control.approval.dbus_service``.

Error names are PROVISIONAL-PENDING-RATIFICATION: ORG-052 §4 ratifies a
closed, versioned error namespace but does not name the strings; the
twelve names below were minted in T-2913 (mirroring the T-2912 broker
mint) and surfaced upward for ratification visibility. This service
NEVER emits a broker (``HostBroker1.Error``) name — the namespaces are
disjoint and a test pins that.

Deliberately UNMAPPED classes (nearest-ancestor resolves them to
``Failed``; each is pinned by test so the choice stays tested, never
accidental):

- ``StaleAuthorityState`` / ``OperationConsumed`` — internal
  serialization races the service absorbs as ``BindOutcome`` values;
  never expected on the wire.
- ``InvalidApproverPolicy`` — a config-load fault that precedes any
  call (policy is parsed at service construction; wiring is T-2920's).
- ``SubjectMismatch`` — broker-domain; no approval path raises it.
"""

from __future__ import annotations

from tools.host_control.approval.contracts import (
    BIND_DENIAL_REASON_CODES,
    BIND_SUCCESS_REASON_CODES,
    ApprovalStatus,
    ApproverDenied,
    HandoffMismatch,
    MetadataUnavailable,
    ReviewRecord,
)
from tools.host_control.contracts import (
    BindOutcome,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    PolicyDenied,
    RegistryError,
    UnknownHandle,
    _closed_hex,
    canonical_json,
    resolve_error_name,
)
from typing import Mapping

BUS_NAME = "org.pantheon.HostApproval1"
OBJECT_PATH = "/org/pantheon/HostApproval1"
REVIEWER_INTERFACE = "org.pantheon.HostApproval1.Reviewer"
CONSUMER_INTERFACE = "org.pantheon.HostApproval1.Consumer"
HUMAN_INGRESS_INTERFACE = "org.pantheon.HostApproval1.HumanIngress"

# The separately supervised Telegram ingress's restricted internal
# interface (ORG-052 §5 table: ``HostApprovalTelegram1.Ingress`` /
# approval uid / ``Present`` "sssss" -> "ss"). Exported BY the ingress,
# called ONLY by the approval-service uid (T-2919).
TELEGRAM_INGRESS_BUS_NAME = "org.pantheon.HostApprovalTelegram1"
TELEGRAM_INGRESS_OBJECT_PATH = "/org/pantheon/HostApprovalTelegram1"
TELEGRAM_INGRESS_INTERFACE = "org.pantheon.HostApprovalTelegram1.Ingress"
TELEGRAM_INGRESS_ERROR_NAMESPACE = "org.pantheon.HostApprovalTelegram1.Error"

ERROR_NAMESPACE = "org.pantheon.HostApproval1.Error"

APPROVER_DENIED_ERROR = f"{ERROR_NAMESPACE}.ApproverDenied"
HANDOFF_MISMATCH_ERROR = f"{ERROR_NAMESPACE}.HandoffMismatch"
POLICY_DENIED_ERROR = f"{ERROR_NAMESPACE}.PolicyDenied"
UNKNOWN_HANDLE_ERROR = f"{ERROR_NAMESPACE}.UnknownHandle"
IDEMPOTENCY_CONFLICT_ERROR = f"{ERROR_NAMESPACE}.IdempotencyConflict"
INVALID_TRANSITION_ERROR = f"{ERROR_NAMESPACE}.InvalidTransition"
METADATA_UNAVAILABLE_ERROR = f"{ERROR_NAMESPACE}.MetadataUnavailable"
REGISTRY_DENIED_ERROR = f"{ERROR_NAMESPACE}.RegistryDenied"
BIND_DENIED_ERROR = f"{ERROR_NAMESPACE}.BindDenied"
FAILED_ERROR = f"{ERROR_NAMESPACE}.Failed"
UNSUPPORTED_ERROR = f"{ERROR_NAMESPACE}.Unsupported"
INTERNAL_ERROR = f"{ERROR_NAMESPACE}.Internal"

CLOSED_ERROR_NAMES = frozenset(
    (
        APPROVER_DENIED_ERROR,
        HANDOFF_MISMATCH_ERROR,
        POLICY_DENIED_ERROR,
        UNKNOWN_HANDLE_ERROR,
        IDEMPOTENCY_CONFLICT_ERROR,
        INVALID_TRANSITION_ERROR,
        METADATA_UNAVAILABLE_ERROR,
        REGISTRY_DENIED_ERROR,
        BIND_DENIED_ERROR,
        FAILED_ERROR,
        UNSUPPORTED_ERROR,
        INTERNAL_ERROR,
    )
)

# Nearest-ancestor resolution over this table (shared resolver, T-2913
# OQ2) makes the mapping TOTAL across the live exception hierarchy: any
# HostControlError subclass resolves via its closest mapped ancestor,
# and anything outside the hierarchy is a bounded Internal.
_ERROR_NAME_BY_TYPE: Mapping[type, str] = {
    ApproverDenied: APPROVER_DENIED_ERROR,
    HandoffMismatch: HANDOFF_MISMATCH_ERROR,
    PolicyDenied: POLICY_DENIED_ERROR,
    UnknownHandle: UNKNOWN_HANDLE_ERROR,
    IdempotencyConflict: IDEMPOTENCY_CONFLICT_ERROR,
    InvalidTransition: INVALID_TRANSITION_ERROR,
    MetadataUnavailable: METADATA_UNAVAILABLE_ERROR,
    RegistryError: REGISTRY_DENIED_ERROR,
    HostControlError: FAILED_ERROR,
}

_INTERNAL_BODY = "internal error"
# RegistryError bodies can name trusted-file mode/path/owner facts —
# server configuration a caller must not learn (the T-2912 PO-required
# posture, kept identical here).
_REGISTRY_DENIED_BODY = "registry refused the request"
_MESSAGE_LIMIT = 200
_JSON_LIMIT = 65536

REVIEW_SUMMARY_SCHEMA = "pantheon-approval-review-summary/v1"
APPROVAL_STATUS_SCHEMA = "pantheon-approval-status/v1"


def error_name_for(exc: BaseException) -> str:
    """Resolve any exception to exactly one closed error name."""

    return resolve_error_name(exc, _ERROR_NAME_BY_TYPE, INTERNAL_ERROR)


def error_body_for(exc: BaseException) -> str:
    """Bounded, non-disclosing error body for the wire.

    Reason-code-bearing denials (``PolicyDenied`` at the transport uid
    gates, ``ApproverDenied`` from approver policy, ``HandoffMismatch``
    from link validation) put ONLY the closed reason code on the wire —
    the human-authored message stays server-side. Everything else
    follows the broker discipline: bounded server-authored phrases for
    domain errors, constants for registry faults and internals.
    """

    if isinstance(exc, PolicyDenied):
        return str(exc.reason_code)[:_MESSAGE_LIMIT]
    if isinstance(exc, (ApproverDenied, HandoffMismatch)):
        return str(exc.reason_code)[:_MESSAGE_LIMIT]
    if isinstance(exc, RegistryError):
        return _REGISTRY_DENIED_BODY
    if isinstance(exc, HostControlError):
        return str(exc)[:_MESSAGE_LIMIT] or "failed"
    return _INTERNAL_BODY


def bind_success_slots(outcome: BindOutcome) -> tuple[str, str]:
    """The two ratified ``ValidateAndBind`` out slots (``"ss"``: bound
    state and authority-record id) for a successful bind.

    ``bound`` and ``bound_replay`` DELIBERATELY produce byte-identical
    slots carrying the ORIGINAL authority-record id: gate 16's
    "identical retry returns the original binding" means a caller
    cannot — and must not — distinguish first-bind from replay on the
    wire (T-2913 OQ3(b) disposition).
    """

    if outcome.state != "bound":
        raise HostControlError("outcome is not a successful bind")
    if outcome.reason_code not in BIND_SUCCESS_REASON_CODES:
        raise HostControlError("unknown bind success reason code")
    record_id = _closed_hex(
        outcome.authority_record_id, "authority record id"
    )
    return ("bound", record_id)


def bind_denial_reason(outcome: BindOutcome) -> str:
    """The closed denial reason code a ``BindDenied`` wire error carries.

    The DOMAIN deliberately returns denial as a VALUE (``BindOutcome``,
    fail-closed classification the in-process consumer records), while
    the TRANSPORT converts it to the closed ``BindDenied`` error: the
    ratified ``"ss"`` out signature has slots only for the success case
    (bound state + authority-record id), and overloading slot 2 with a
    reason code would type-pun the authority-record-id slot — a caller
    could mistake a reason code for an authority record. ADR §4 routes
    policy/validation failures through the closed error namespace. Do
    NOT "fix" either tier to match the other (T-2913 OQ3(c) disposition).
    """

    if outcome.state != "denied":
        raise HostControlError("outcome is not a denial")
    if outcome.reason_code not in BIND_DENIAL_REASON_CODES:
        raise HostControlError("unknown bind denial reason code")
    return outcome.reason_code


def _bounded(encoded: str, context: str) -> str:
    if len(encoded.encode("utf-8")) > _JSON_LIMIT:
        raise HostControlError(f"{context} exceeds size limit")
    return encoded


def review_summary_json(record: ReviewRecord, extras: object = None) -> str:
    """Canonical bounded summary payload for ``ReviewGet`` (``"ss"``
    slot 2). PROVISIONAL-PENDING-RATIFICATION: ORG-052 §5 ratifies
    "review state and bounded non-secret summary" without naming the
    body; this schema was minted in T-2913 and surfaced upward.

    RECORD-DERIVED ONLY: this summary never re-discloses plan effects —
    the redacted plan summary is disclosed exactly once, by ``Review``,
    after the metadata-first authorization order (gate 12). Explicit
    allowlist construction, never ``to_dict`` minus a blocklist, so a
    field added to ``ReviewRecord`` later can never leak silently.

    Excluded by design: ``decide_idempotency_key`` (another caller's
    idempotency material), ``source_event`` (T-2919's provider-event
    pointer; its disclosure semantics are that unit's to define), and
    the record-integrity ``digest`` (internal tamper-evidence, not
    contract data).
    """

    handoff = None
    if record.handoff is not None:
        handoff = {
            "request_plan_handle": record.handoff.request_plan_handle,
            "request_plan_digest": record.handoff.request_plan_digest,
            "request_plan_owner": record.handoff.request_plan_owner,
            "shared_effect_digest": record.handoff.shared_effect_digest,
            "handoff_anchor": record.handoff.handoff_anchor,
        }
    payload = {
        "schema": REVIEW_SUMMARY_SCHEMA,
        "review_handle": record.review_handle,
        "plan_handle": record.plan_handle,
        "plan_digest": record.plan_digest,
        "plan_owner": record.plan_owner,
        "performer": record.performer,
        "target_seat_id": record.target_seat_id,
        "operation_families": list(record.operation_families),
        "effect_digest": record.effect_digest,
        "manifest_revision": record.manifest_revision,
        "policy_revision": record.policy_revision,
        "handoff_kind": record.handoff_kind,
        "handoff": handoff,
        "requested_by": record.requested_by,
        "state": record.state,
        "decision": record.decision,
        "reason_code": record.reason_code,
        "approver_subject": record.approver_subject,
        "authority_source": record.authority_source,
        "audit_anchor": record.audit_anchor,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "decided_at": record.decided_at,
    }
    # T-2919 Q3 (additive, provisional v1): the opaque approval handle once
    # granted — not bearer authority (ORG-052 §5), and the requester's only
    # way to learn it after a HUMAN grant — plus the human card's handle,
    # state and expiry. Never the nonce, never the attestation.
    payload["approval_handle"] = getattr(extras, "approval_handle", None)
    request_handle = getattr(extras, "human_request_handle", None)
    payload["human_request"] = (
        None
        if request_handle is None
        else {
            "request_handle": request_handle,
            "state": getattr(extras, "human_request_state"),
            "expires_at": getattr(extras, "human_request_expires_at"),
        }
    )
    return _bounded(canonical_json(payload), "review summary payload")


def approval_status_json(status: ApprovalStatus) -> str:
    """Canonical bounded status payload for ``ApprovalStatus`` (``"ss"``
    slot 2). PROVISIONAL-PENDING-RATIFICATION, minted in T-2913.

    The input dataclass carries no nonce and no authority-record id BY
    CONSTRUCTION (gate 21): a status read can never substitute for
    binding, so serializing all four fields is the whole story.
    """

    payload = {
        "schema": APPROVAL_STATUS_SCHEMA,
        "approval_handle": status.approval_handle,
        "state": status.state,
        "expires_at": status.expires_at,
        "summary": status.summary,
    }
    return _bounded(canonical_json(payload), "approval status payload")
