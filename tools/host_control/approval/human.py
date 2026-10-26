"""Human authority ingress contracts (ORG-052 §5 "Human authority
ingress", T-2919).

Approval-owned record and value types for the provider-authenticated
human path: the pending human-review card record, the root-owned
ingress registration (ingress uid -> one provider human), the closed
refusal codes, and the bounded public result views.

Gate 21 discipline: the single-use callback ``nonce`` lives on
``HumanRequestRecord`` only. No public result type carries it; it
leaves the approval service solely inside the private ``Present`` call
to the registered ingress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tools.host_control.contracts import (
    HostControlError,
    _closed_hex,
    _closed_string,
)

HUMAN_REQUEST_STATES = frozenset(
    ("pending", "presented", "decided", "expired", "withdrawn")
)
# pending -> presented once the ingress acknowledged delivery of the ONE
# card; presented -> decided is the single nonce consumption. expired and
# withdrawn close the card for good: a later attempt needs a NEW review
# (ORG-052 §5 "Denial and revocation require a new review").
HUMAN_REQUEST_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset(("presented", "expired", "withdrawn")),
    "presented": frozenset(("decided", "expired", "withdrawn")),
    "decided": frozenset(),
    "expired": frozenset(),
    "withdrawn": frozenset(),
}
HUMAN_REFUSAL_REASON_CODES = frozenset(
    (
        "ingress_uid_denied",
        "human_ingress_unconfigured",
        "no_human_request",
        "human_request_not_presented",
        "human_request_closed",
        "human_request_expired",
        "human_nonce_mismatch",
        "digest_mismatch",
        "review_not_open",
        "review_expired",
        "no_human_approver",
        "withdraw_not_permitted",
    )
)
MAX_CARD_SUMMARY_LENGTH = 4096
MAX_PROVIDER_MESSAGE_REF_LENGTH = 32
MAX_SOURCE_EVENT_LENGTH = 256
HUMAN_AUDIT_ANCHOR = "human-ingress:telegram"
HUMAN_CARD_SCHEMA = "pantheon-approval-human-card/v1"
HUMAN_CARD_KEYS = frozenset(
    (
        "schema",
        "review_handle",
        "plan_digest",
        "target_seat_id",
        "operation_families",
        "redacted_effects",
        "expires_at",
    )
)


class HumanDecisionRefused(HostControlError):
    """A RequestHuman / HumanDecision refusal carrying one closed code.

    The message IS the closed code: non-disclosing on the wire (nearest
    mapped ancestor ``HostControlError`` -> ``Failed``, bounded body)."""

    def __init__(self, message: str = "", *, reason_code: str) -> None:
        # ``message`` is accepted for the ApproverDenied-shaped signature
        # and deliberately discarded: the wire body is the closed code
        del message
        if reason_code not in HUMAN_REFUSAL_REASON_CODES:
            raise HostControlError("unknown human refusal reason code")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _provider_id(value: object, label: str) -> str:
    """Telegram ids as canonical decimal strings (chat ids may be
    negative; up to 52 significant bits — never floats)."""

    if not isinstance(value, str) or not 1 <= len(value) <= 24:
        raise HostControlError(f"invalid {label}")
    digits = value[1:] if value.startswith("-") else value
    if not digits.isdigit() or not digits.isascii() or (
        len(digits) > 1 and digits.startswith("0")
    ):
        raise HostControlError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class HumanIngressRegistration:
    """Root-owned mapping of ONE ingress Unix uid to ONE provider human.

    ORG-052 §5: "The approval service maps that uid to the configured
    provider and human subject; model-supplied Telegram ids or prose
    cannot do so." One allowlisted human per ingress identity in this
    revision (T-2919 Q5 ruling)."""

    uid: int
    provider: str
    human_subject: str
    provider_user_id: str
    provider_chat_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.uid, int)
            or isinstance(self.uid, bool)
            or self.uid < 0
        ):
            raise HostControlError("invalid human ingress uid")
        if self.provider != "telegram":
            raise HostControlError("unsupported human ingress provider")
        _closed_string(self.human_subject, "human subject")
        _provider_id(self.provider_user_id, "provider user id")
        _provider_id(self.provider_chat_id, "provider chat id")
        if self.provider_chat_id != self.provider_user_id:
            # a private chat with the one human only (review finding 6)
            raise HostControlError("provider chat must be the user's private chat")


@dataclass(frozen=True)
class HumanRequestRecord:
    """One pending human-review card record. Authoritative copy: the
    approval journal ``human_requests`` table. The card is rendered from
    THIS record and nothing else (``card_summary``)."""

    request_handle: str
    review_handle: str
    requested_by: str
    request_idempotency_key: str
    plan_digest: str
    target_seat_id: str
    operation_families: tuple[str, ...]
    redacted_summary: str
    nonce: str
    state: str
    provider_message_ref: str | None
    created_at: str
    expires_at: str
    presented_at: str | None
    updated_at: str

    def __post_init__(self) -> None:
        _closed_hex(self.request_handle, "human request handle")
        _closed_hex(self.review_handle, "review handle")
        _closed_string(self.requested_by, "human request requester")
        _closed_string(
            self.request_idempotency_key, "human request idempotency key"
        )
        _closed_hex(self.plan_digest, "plan digest")
        _closed_string(self.target_seat_id, "target seat id")
        if not isinstance(self.operation_families, tuple) or not (
            self.operation_families
        ):
            raise HostControlError("human request needs operation families")
        for family in self.operation_families:
            _closed_string(family, "operation family")
        _closed_string(
            self.redacted_summary,
            "card summary",
            maximum=MAX_CARD_SUMMARY_LENGTH,
        )
        _closed_hex(self.nonce, "callback nonce")
        if self.state not in HUMAN_REQUEST_STATES:
            raise HostControlError("invalid human request state")
        if self.provider_message_ref is not None:
            _closed_string(
                self.provider_message_ref,
                "provider message ref",
                maximum=MAX_PROVIDER_MESSAGE_REF_LENGTH,
            )
        if (self.provider_message_ref is None) != (self.presented_at is None):
            raise HostControlError("presented fields must pair")
        if self.state in ("presented", "decided") and (
            self.provider_message_ref is None
        ):
            raise HostControlError("presented human request needs a ref")
        if self.state == "pending" and self.provider_message_ref is not None:
            raise HostControlError("pending human request cannot carry a ref")

    def card_summary(self) -> dict[str, object]:
        """The bounded, closed-key card payload (``Present`` slot 3)."""

        return {
            "schema": HUMAN_CARD_SCHEMA,
            "review_handle": self.review_handle,
            "plan_digest": self.plan_digest,
            "target_seat_id": self.target_seat_id,
            "operation_families": list(self.operation_families),
            "redacted_effects": self.redacted_summary,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class HumanRequestResult:
    """Public RequestHuman view: handle + state only (never the nonce)."""

    request_handle: str
    state: str
    replayed: bool


@dataclass(frozen=True)
class HumanDecisionResult:
    """Public HumanDecision view: review state + attestation handle.

    The approval handle is deliberately NOT returned to the ingress; the
    requester learns it through ``ReviewGet`` (T-2919 Q3)."""

    review_handle: str
    state: str
    attestation_handle: str
    replayed: bool


@dataclass(frozen=True)
class AttestationRecord:
    """One provider attestation row (journal ``provider_attestations``)."""

    attestation_handle: str
    review_handle: str
    request_handle: str
    provider: str
    provider_user_id: str
    provider_chat_id: str
    provider_message_id: str
    callback_nonce: str
    source_event: str
    decision: str
    human_subject: str
    idempotency_key: str
    plan_digest: str
    created_at: str
