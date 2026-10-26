"""ORG-059 Child E — typed capability-delegation route adapter (T-3222).

Capability delegation is a typed, auditable ROUND-TRIP, never a credential
hand-off:

    request  -> delivery  -> acceptance  -> native_operation  -> result

A role PROPOSES a delegation (``DelegationProposal``); the receiving role
INDEPENDENTLY authenticates the delivery under ITS OWN subject, accepts or
refuses, RE-RUNS planning under its own native authority, invokes through that
authority, and owns the operation. Every stage emits a DISTINCT
``DelegationReceipt`` chained via ``prior_receipt_ref``; the subject flips to the
receiving role at acceptance. No credential, secret, or persona-as-execution
primitive ever crosses the wire — the closed-schema validators in
``capability_navigator_contract`` reject those, not strip them.

This module owns the ADAPTER: the injectable delivery interface
(``DeliveryChannel`` with a real ``MailboxDelivery`` over the existing mailbox
substrate), the sender-side ``DelegationRouter``, the receiving-side
``DelegationRecipient`` (which re-derives its own ``AuthenticatedContext`` and
re-plans), and the receipt store. It reuses — never forks — the navigator
contract's typed model and the request-identity invariant. The pure typed
shapes + fail-closed validators live in ``capability_navigator_contract``; no
new transport is invented (mailbox is reused; ticket/A2A are sibling stubs).

The navigator engine emits a ``DelegationProposal`` as a new outcome branch off
its authority-unavailable refusals (see ``eligible_owning_role_for`` /
``make_delegation_emitter``): when the current subject cannot plan/invoke but the
catalogue declares an eligible OWNING role the subject does not hold.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
import hashlib
import uuid
from typing import Callable, Protocol

from tools import agent_role_map
from tools import capability_navigator_contract as cn
from tools import mailbox


# The persona seats (never valid as an execution primitive) and the audit
# role-ids, sourced from the single-source-of-truth map. `known_personas` is
# always the seat names; `known_role_ids` is the caller's role namespace (the
# engine-emission path supplies the catalogue's declared owning role refs — see
# eligible_owning_role_for). A named persona as eligible_owning_role is REJECTED.
KNOWN_PERSONAS: frozenset[str] = frozenset(agent_role_map.AGENT_ROLE_MAP.keys())
KNOWN_ROLE_IDS: frozenset[str] = frozenset(agent_role_map.AGENT_ROLE_MAP.values())

DEFAULT_TTL_SECONDS = 3600

# Transport-status vocabulary a DeliveryChannel may report; each maps onto a
# typed, auditable delivery-stage receipt status (never a silent accept).
_DELIVERY_STATUSES = frozenset({"delivered", "rerouted", "overloaded", "lost-result"})


# ---------------------------------------------------------------------------
# Transport envelope + digest
# ---------------------------------------------------------------------------


def proposal_digest(proposal: cn.DelegationProposal) -> str:
    """Canonical digest of a proposal — the tamper anchor across a delivery."""
    return "sha256:" + hashlib.sha256(
        cn.canonical_json_bytes(cn.delegation_proposal_to_json(proposal))
    ).hexdigest()


@dataclass(frozen=True)
class DeliveryEnvelope:
    """What crosses a delivery channel: the proposal plus its tamper digest and
    a delivery TTL. Carries NO credential — the proposal already forbids them."""

    proposal: cn.DelegationProposal
    proposal_digest: str
    sent_at: str
    ttl_seconds: int


def delivery_envelope_to_json(envelope: DeliveryEnvelope) -> dict[str, object]:
    return {
        "proposal": cn.delegation_proposal_to_json(envelope.proposal),
        "proposal_digest": envelope.proposal_digest,
        "sent_at": envelope.sent_at,
        "ttl_seconds": envelope.ttl_seconds,
    }


def parse_delivery_envelope(
    value: object,
    *,
    known_role_ids: frozenset[str],
    known_personas: frozenset[str] = KNOWN_PERSONAS,
) -> DeliveryEnvelope:
    data = cn._require_mapping(value, path="delivery_envelope")
    cn._require_closed(
        data, keys=("proposal", "proposal_digest", "sent_at", "ttl_seconds"), path="delivery_envelope"
    )
    proposal = cn.parse_delegation_proposal(
        data["proposal"], known_role_ids=known_role_ids, known_personas=known_personas
    )
    return DeliveryEnvelope(
        proposal=proposal,
        proposal_digest=cn._require_string(data["proposal_digest"], path="delivery_envelope.proposal_digest") or "",
        sent_at=cn._require_string(data["sent_at"], path="delivery_envelope.sent_at") or "",
        ttl_seconds=cn._require_int(data["ttl_seconds"], path="delivery_envelope.ttl_seconds"),
    )


@dataclass(frozen=True)
class DeliveryOutcome:
    """A delivery channel's report of transporting one envelope. `status` is a
    delivery-stage status (delivered / rerouted / overloaded / lost-result)."""

    status: str
    transport_ref: str
    redacted_summary: str


@dataclass(frozen=True)
class NativeOperationOutcome:
    """The receiving role's native invocation result. Evidence refs and the
    authority provenance are the RECEIVING role's own — never the requester's."""

    status: str  # "invoked" (evidence follows) or "native-failed" / "overloaded"
    provider_native_evidence_refs: tuple[str, ...]
    authority_provenance_ref: str | None
    redacted_summary: str


# ---------------------------------------------------------------------------
# Injectable seams (Protocols) — mailbox is the real reference channel
# ---------------------------------------------------------------------------


class DeliveryChannel(Protocol):
    """A durable, typed delivery substrate. Reuses an EXISTING transport; no new
    transport is invented. Implemented by MailboxDelivery (real); TicketDelivery
    / A2ADelivery are sibling adapters over the same interface."""

    def send_proposal(self, envelope: DeliveryEnvelope) -> DeliveryOutcome: ...

    def poll(self, proposal_id: str) -> tuple[Mapping[str, object], ...]: ...


class NativePlanner(Protocol):
    """Re-runs planning under the RECEIVING role's own authenticated context."""

    def plan(self, auth: cn.AuthenticatedContext, proposal: cn.DelegationProposal) -> object: ...


class NativeInvoker(Protocol):
    """Invokes through the receiving role's own native authority and returns the
    native evidence — the requester's identity is never inherited authority."""

    def invoke(
        self, auth: cn.AuthenticatedContext, proposal: cn.DelegationProposal, plan: object
    ) -> NativeOperationOutcome: ...


# ---------------------------------------------------------------------------
# Receipt minting + store (the adapter owns its records; the engine does not
# persist receipts — reuse the NavigatorReceipt shape + prior_receipt_ref chain)
# ---------------------------------------------------------------------------


def _default_clock() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _default_id_gen(stage: str) -> str:
    return f"delegation-receipt:{stage}:{uuid.uuid4().hex}"


class ReceiptMinter:
    """Deterministic-injectable clock + id source for receipts."""

    def __init__(
        self,
        *,
        clock: Callable[[], str] | None = None,
        id_gen: Callable[[str], str] | None = None,
    ) -> None:
        self._clock = clock or _default_clock
        self._id_gen = id_gen or _default_id_gen

    def now(self) -> str:
        return self._clock()

    def mint(
        self,
        proposal: cn.DelegationProposal,
        *,
        stage: str,
        status: str,
        subject_ref: str,
        prior_receipt_ref: str | None,
        redacted_summary: str,
        provider_native_evidence_refs: Sequence[str] = (),
        authority_provenance_ref: str | None = None,
    ) -> cn.DelegationReceipt:
        return cn.DelegationReceipt(
            schema_version=cn.SCHEMA_VERSION,
            receipt_id=self._id_gen(stage),
            proposal_id=proposal.proposal_id,
            trace_id=proposal.trace_id,
            stage=stage,
            subject_ref=subject_ref,
            status=status,
            created_at=self.now(),
            redacted_summary=redacted_summary,
            prior_receipt_ref=prior_receipt_ref,
            provider_native_evidence_refs=tuple(provider_native_evidence_refs),
            authority_provenance_ref=authority_provenance_ref,
        )


class ReceiptStore:
    """Append-only audit store. Every append is re-validated through the closed
    contract validator, so a stored receipt is always contract-valid — including
    the stage/payload distinctness guard."""

    def __init__(self) -> None:
        self._order: list[cn.DelegationReceipt] = []
        self._by_id: dict[str, cn.DelegationReceipt] = {}

    def append(self, receipt: cn.DelegationReceipt) -> cn.DelegationReceipt:
        validated = cn.parse_delegation_receipt(cn.delegation_receipt_to_json(receipt))
        if validated.receipt_id in self._by_id:
            raise ValueError(f"duplicate receipt_id {validated.receipt_id!r}")
        self._order.append(validated)
        self._by_id[validated.receipt_id] = validated
        return validated

    def get(self, receipt_id: str) -> cn.DelegationReceipt | None:
        return self._by_id.get(receipt_id)

    def all(self) -> tuple[cn.DelegationReceipt, ...]:
        return tuple(self._order)

    def chain(self, trace_id: str) -> tuple[cn.DelegationReceipt, ...]:
        return tuple(r for r in self._order if r.trace_id == trace_id)


# ---------------------------------------------------------------------------
# Sender side — DelegationRouter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationRoute:
    request: cn.DelegationReceipt
    delivery: cn.DelegationReceipt | None
    envelope: DeliveryEnvelope | None


class DelegationRouter:
    """Sender side: emits the request receipt, then hands the envelope to the
    injected delivery channel and emits the delivery receipt. Both stages carry
    the REQUESTER's subject and never any native evidence."""

    def __init__(
        self,
        channel: DeliveryChannel,
        *,
        minter: ReceiptMinter | None = None,
        store: ReceiptStore | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._channel = channel
        self._minter = minter or ReceiptMinter()
        self._store = store or ReceiptStore()
        self._ttl_seconds = ttl_seconds
        self._seen_proposal_ids: set[str] = set()

    @property
    def store(self) -> ReceiptStore:
        return self._store

    def propose(self, proposal: cn.DelegationProposal) -> cn.DelegationReceipt:
        # Duplicate proposal_id -> a typed, auditable outcome, never a silent
        # re-send/accept.
        if proposal.proposal_id in self._seen_proposal_ids:
            return self._store.append(
                self._minter.mint(
                    proposal,
                    stage="request",
                    status="duplicate",
                    subject_ref=proposal.requester_subject_ref,
                    prior_receipt_ref=None,
                    redacted_summary="duplicate proposal_id at sender",
                )
            )
        self._seen_proposal_ids.add(proposal.proposal_id)
        return self._store.append(
            self._minter.mint(
                proposal,
                stage="request",
                status="proposed",
                subject_ref=proposal.requester_subject_ref,
                prior_receipt_ref=None,
                redacted_summary=f"proposed {proposal.capability_id} to {proposal.eligible_owning_role}",
            )
        )

    def deliver(
        self, proposal: cn.DelegationProposal, request_receipt: cn.DelegationReceipt
    ) -> tuple[DeliveryEnvelope, cn.DelegationReceipt]:
        envelope = DeliveryEnvelope(
            proposal=proposal,
            proposal_digest=proposal_digest(proposal),
            sent_at=self._minter.now(),
            ttl_seconds=self._ttl_seconds,
        )
        outcome = self._channel.send_proposal(envelope)
        status = outcome.status if outcome.status in _DELIVERY_STATUSES else "rerouted"
        # A delivery-ack can NEVER carry native evidence — enforced structurally
        # (empty refs, no authority provenance) and by the contract validator.
        delivery = self._store.append(
            self._minter.mint(
                proposal,
                stage="delivery",
                status=status,
                subject_ref=proposal.requester_subject_ref,
                prior_receipt_ref=request_receipt.receipt_id,
                redacted_summary=outcome.redacted_summary,
            )
        )
        return envelope, delivery

    def route(self, proposal: cn.DelegationProposal) -> DelegationRoute:
        request = self.propose(proposal)
        if request.status == "duplicate":
            return DelegationRoute(request=request, delivery=None, envelope=None)
        envelope, delivery = self.deliver(proposal, request)
        return DelegationRoute(request=request, delivery=delivery, envelope=envelope)


# ---------------------------------------------------------------------------
# Receiving side — DelegationRecipient
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecipientOutcome:
    accepted: bool
    terminal_status: str
    receipts: tuple[cn.DelegationReceipt, ...]


def _parse_ts(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _expired(envelope: DeliveryEnvelope, now: str) -> bool:
    # ttl_seconds <= 0 is an explicit "no expiry" sentinel (a non-expiring
    # delegation); a positive ttl bounds the delivery window.
    if envelope.ttl_seconds <= 0:
        return False
    sent = _parse_ts(envelope.sent_at)
    current = _parse_ts(now)
    if sent is None or current is None:
        # Fail closed: an unparseable clock is treated as expired.
        return True
    return (current - sent).total_seconds() > envelope.ttl_seconds


def _normalize_native_evidence(refs: Sequence[str]) -> tuple[str, ...] | None:
    """Native evidence normalizes only if non-empty and every ref is a clean,
    bounded string. Missing/tampered evidence cannot normalize as success."""
    if not refs:
        return None
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            return None
        try:
            cn._validate_text(ref, path="native_evidence")
        except cn.NavigatorContractError:
            return None
    return tuple(refs)


class DelegationRecipient:
    """Receiving side: RE-DERIVES its own AuthenticatedContext, independently
    validates the delivery (duplicate / expiry / tamper), decides accept/refuse,
    RE-RUNS planning under its own subject + native authority, invokes, and
    owns the result. The proposal's requester identity is provenance-only — it
    is NEVER inherited as authority, and the recipient never auto-executes the
    requester's plan."""

    def __init__(
        self,
        subject_resolver: cn.SubjectResolver,
        planner: NativePlanner,
        invoker: NativeInvoker,
        *,
        minter: ReceiptMinter | None = None,
        store: ReceiptStore | None = None,
    ) -> None:
        self._subject_resolver = subject_resolver
        self._planner = planner
        self._invoker = invoker
        self._minter = minter or ReceiptMinter()
        self._store = store or ReceiptStore()
        self._seen_proposal_ids: set[str] = set()

    @property
    def store(self) -> ReceiptStore:
        return self._store

    def _single(
        self,
        proposal: cn.DelegationProposal,
        *,
        status: str,
        subject_ref: str,
        prior_receipt_ref: str,
        redacted_summary: str,
    ) -> RecipientOutcome:
        acceptance = self._store.append(
            self._minter.mint(
                proposal,
                stage="acceptance",
                status=status,
                subject_ref=subject_ref,
                prior_receipt_ref=prior_receipt_ref,
                redacted_summary=redacted_summary,
            )
        )
        return RecipientOutcome(accepted=False, terminal_status=status, receipts=(acceptance,))

    def receive(
        self,
        envelope: DeliveryEnvelope,
        delivery_receipt: cn.DelegationReceipt,
        *,
        transport: object = None,
        now: str | None = None,
    ) -> RecipientOutcome:
        proposal = envelope.proposal
        prior = delivery_receipt.receipt_id
        # 1. INDEPENDENTLY authenticate the delivery under the receiving role's
        #    OWN subject. The requester identity in the proposal is provenance.
        auth = self._subject_resolver.resolve(transport)
        subject = auth.subject_ref
        now = now or self._minter.now()

        # 2. Typed handling of the adversarial delivery conditions — each its own
        #    auditable acceptance-stage receipt, never a silent accept.
        if proposal.proposal_id in self._seen_proposal_ids:
            return self._single(
                proposal, status="duplicate", subject_ref=subject, prior_receipt_ref=prior,
                redacted_summary="duplicate delivery at recipient",
            )
        self._seen_proposal_ids.add(proposal.proposal_id)
        if _expired(envelope, now):
            return self._single(
                proposal, status="expired", subject_ref=subject, prior_receipt_ref=prior,
                redacted_summary="proposal ttl expired",
            )
        if proposal_digest(proposal) != envelope.proposal_digest:
            return self._single(
                proposal, status="tampered", subject_ref=subject, prior_receipt_ref=prior,
                redacted_summary="proposal digest mismatch",
            )

        # 3. Accept/refuse under NATIVE authority: the receiving role accepts only
        #    if it actually HOLDS the eligible owning role — it never inherits the
        #    requester's authority to reach it.
        if proposal.eligible_owning_role not in set(auth.role_refs):
            return self._single(
                proposal, status="refused", subject_ref=subject, prior_receipt_ref=prior,
                redacted_summary="receiving role lacks native owning authority",
            )
        acceptance = self._store.append(
            self._minter.mint(
                proposal,
                stage="acceptance",
                status="accepted",
                subject_ref=subject,
                prior_receipt_ref=prior,
                redacted_summary=f"accepted under {proposal.eligible_owning_role}; re-planning under own subject",
            )
        )

        # 4. RE-RUN planning under the receiving role's own subject, then invoke
        #    through its own native authority.
        try:
            plan = self._planner.plan(auth, proposal)
            native = self._invoker.invoke(auth, proposal, plan)
        except Exception:  # noqa: BLE001 — any native failure is a typed outcome
            native = NativeOperationOutcome(
                status="native-failed",
                provider_native_evidence_refs=(),
                authority_provenance_ref=None,
                redacted_summary="native re-planning/invocation failed",
            )

        native_status = native.status if native.status in cn.DELEGATION_STATUSES else "native-failed"
        # Only normalized, non-empty native evidence + the receiving role's own
        # authority provenance can present as success. Missing or tampered native
        # evidence normalizes to nothing, so it never reaches a receipt as raw
        # refs and can never carry an invocation forward.
        normalized = _normalize_native_evidence(native.provider_native_evidence_refs)
        success = (
            native_status == "invoked"
            and normalized is not None
            and native.authority_provenance_ref is not None
        )
        native_receipt = self._store.append(
            self._minter.mint(
                proposal,
                stage="native_operation",
                status=native_status,
                subject_ref=subject,
                prior_receipt_ref=acceptance.receipt_id,
                redacted_summary=native.redacted_summary,
                provider_native_evidence_refs=normalized if success else (),
                authority_provenance_ref=native.authority_provenance_ref if success else None,
            )
        )

        # 5. Result: a completed result REQUIRES the normalized native evidence +
        #    the receiving role's own authority provenance; anything else is a
        #    typed lost-result, never a silent success.
        if not success:
            result = self._store.append(
                self._minter.mint(
                    proposal,
                    stage="result",
                    status="lost-result",
                    subject_ref=subject,
                    prior_receipt_ref=native_receipt.receipt_id,
                    redacted_summary="native evidence did not normalize as success",
                )
            )
            return RecipientOutcome(
                accepted=True, terminal_status="lost-result",
                receipts=(acceptance, native_receipt, result),
            )
        result = self._store.append(
            self._minter.mint(
                proposal,
                stage="result",
                status="completed",
                subject_ref=subject,
                prior_receipt_ref=native_receipt.receipt_id,
                redacted_summary=native.redacted_summary,
                provider_native_evidence_refs=normalized,
                authority_provenance_ref=native.authority_provenance_ref,
            )
        )
        return RecipientOutcome(
            accepted=True, terminal_status="completed",
            receipts=(acceptance, native_receipt, result),
        )


# ---------------------------------------------------------------------------
# Real delivery channel — MailboxDelivery over the existing mailbox substrate
# ---------------------------------------------------------------------------


class MailboxDelivery:
    """Reference DeliveryChannel over tools/mailbox.py (ORG-026). Serializes the
    envelope to a mailbox message on the recipient's inbox; polls the sender's
    return inbox for receipts the receiving role posts back. No new transport."""

    SUBJECT_ENVELOPE = "delegation-proposal"
    SUBJECT_RECEIPT = "delegation-receipt"

    def __init__(
        self,
        *,
        recipient_inbox,
        return_inbox,
        sender: str,
        known_role_ids: frozenset[str] = KNOWN_ROLE_IDS,
        known_personas: frozenset[str] = KNOWN_PERSONAS,
    ) -> None:
        self._recipient_inbox = recipient_inbox
        self._return_inbox = return_inbox
        self._sender = sender
        self._known_role_ids = known_role_ids
        self._known_personas = known_personas

    def send_proposal(self, envelope: DeliveryEnvelope) -> DeliveryOutcome:
        payload = {"envelope": delivery_envelope_to_json(envelope)}
        try:
            message_id = mailbox.send(
                self._recipient_inbox, payload, self._sender, subject=self.SUBJECT_ENVELOPE
            )
        except mailbox.MailboxNotFound:
            # A vanished/mistyped hub -> a typed, auditable rerouted outcome.
            return DeliveryOutcome(
                status="rerouted", transport_ref="",
                redacted_summary="recipient inbox not provisioned",
            )
        return DeliveryOutcome(
            status="delivered", transport_ref=message_id,
            redacted_summary=f"delivered to {self._sender_safe()}",
        )

    def _sender_safe(self) -> str:
        return str(self._recipient_inbox)

    def post_receipt(self, receipt: cn.DelegationReceipt) -> str:
        """Receiving role posts a stage receipt back to the sender's return inbox."""
        payload = {"receipt": cn.delegation_receipt_to_json(receipt)}
        return mailbox.send(
            self._return_inbox, payload, self._sender, subject=self.SUBJECT_RECEIPT
        )

    def poll(
        self,
        proposal_id: str,
        *,
        anchor_receipt_ids: frozenset[str] | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Collect the receipts posted back for `proposal_id`.

        When `anchor_receipt_ids` is supplied (the sender's own minted request/
        delivery receipt ids), each collected receipt is kept only if it chains —
        via prior_receipt_ref — back to an anchor or to an already-accepted
        receipt in this batch, AND the receiving-stage receipts are
        subject-consistent (one receiving subject). This is a cheap
        wire-authenticity guard so a forged well-formed receipt injected into the
        return inbox is dropped rather than trusted (guard #4, GAP C). It uses
        only material the adapter already holds — no receipt signing (the
        receipt-MAC question is an ORG-059 design decision, out of scope here;
        transport integrity is bounded by ORG-026). Without anchors, receipts are
        returned unverified — callers that need the guard MUST pass anchors."""
        parsed: list[cn.DelegationReceipt] = []

        def _handler(msg: dict) -> None:
            payload = msg.get("payload") or {}
            raw = payload.get("receipt")
            if not isinstance(raw, Mapping):
                return
            receipt = cn.parse_delegation_receipt(raw)
            if receipt.proposal_id == proposal_id:
                parsed.append(receipt)

        mailbox.consume(self._return_inbox, handler=_handler, use_lease=False)
        if anchor_receipt_ids is None:
            return tuple(cn.delegation_receipt_to_json(r) for r in parsed)

        accepted_ids = set(anchor_receipt_ids)
        receiving_subjects: set[str] = set()
        verified: list[cn.DelegationReceipt] = []
        # Chain forward in the mailbox arrival order; a receipt is authentic only
        # if its predecessor is already trusted.
        for receipt in parsed:
            if receipt.prior_receipt_ref not in accepted_ids:
                continue
            if receipt.stage in cn.DELEGATION_RECEIVING_STAGES:
                receiving_subjects.add(receipt.subject_ref)
                if len(receiving_subjects) > 1:
                    # A second, inconsistent receiving subject is not this
                    # round-trip's owner — drop it.
                    receiving_subjects.discard(receipt.subject_ref)
                    continue
            accepted_ids.add(receipt.receipt_id)
            verified.append(receipt)
        return tuple(cn.delegation_receipt_to_json(r) for r in verified)


class TicketDelivery:
    """Sibling adapter over the ticket substrate. Stub (design: mailbox first;
    ticket/A2A are siblings). Reuses the ticket transport when implemented — no
    new transport is invented."""

    def send_proposal(self, envelope: DeliveryEnvelope) -> DeliveryOutcome:
        raise NotImplementedError("TicketDelivery is a sibling adapter stub (T-3222)")

    def poll(self, proposal_id: str) -> tuple[Mapping[str, object], ...]:
        raise NotImplementedError("TicketDelivery is a sibling adapter stub (T-3222)")


class A2ADelivery:
    """Sibling adapter over the A2A substrate. Stub (design: mailbox first;
    ticket/A2A are siblings). Reuses the A2A transport when implemented — no
    new transport is invented."""

    def send_proposal(self, envelope: DeliveryEnvelope) -> DeliveryOutcome:
        raise NotImplementedError("A2ADelivery is a sibling adapter stub (T-3222)")

    def poll(self, proposal_id: str) -> tuple[Mapping[str, object], ...]:
        raise NotImplementedError("A2ADelivery is a sibling adapter stub (T-3222)")


# ---------------------------------------------------------------------------
# Engine emission wire-in — DelegationProposal off an authority-unavailable
# refusal, when the catalogue declares an eligible OWNING role the subject lacks
# ---------------------------------------------------------------------------


def eligible_owning_role_for(
    projection: cn.NavigatorProjection,
    auth: cn.AuthenticatedContext,
    capability_id: str | None,
) -> str | None:
    """The lowest (deterministic) catalogue-declared owning role for
    `capability_id` that the current subject does NOT already hold, or None. This
    is the catalogue's answer to 'who could own this that I cannot'."""
    if capability_id is None:
        return None
    held = set(auth.role_refs)
    candidates: set[str] = set()
    for provider in projection.capabilities:
        if provider.capability_id != capability_id:
            continue
        visibility = provider.subject_visibility
        if visibility is None:
            continue
        for role in visibility.role_refs:
            if role not in held:
                candidates.add(role)
    if not candidates:
        return None
    return sorted(candidates)[0]


def make_delegation_emitter(
    *,
    minter: ReceiptMinter | None = None,
    known_personas: frozenset[str] = KNOWN_PERSONAS,
) -> Callable[..., cn.DelegationProposal]:
    """Build the emitter the navigator engine calls at an authority-unavailable
    refusal boundary. It constructs a validated DelegationProposal from the
    AUTHENTICATED requester context (never caller-supplied) and the
    catalogue-declared eligible owning role.

    The caller (the engine) MUST pass `known_role_ids` — the REAL set of roles
    the catalogue declares for the capability, never a self-singleton of the
    candidate. Validating against the candidate alone is vacuous (the candidate
    is always a member), which would let a persona or arbitrary garbage emit
    unrejected. With the real catalogue set, `known_personas` is a genuine,
    disjoint blacklist and _require_delegation_role rejects a persona seat
    (including a self-colliding one) and any value the
    catalogue never declared as a role."""
    local_minter = minter or ReceiptMinter()

    def _emit(
        *,
        auth: cn.AuthenticatedContext,
        capability_id: str,
        eligible_owning_role: str,
        intent: str,
        trace_id: str,
        known_role_ids: frozenset[str],
        contract_revision: str | None = None,
        constraints_ref: str | None = None,
    ) -> cn.DelegationProposal:
        proposal = cn.DelegationProposal(
            schema_version=cn.SCHEMA_VERSION,
            proposal_id=f"delegation-proposal:{uuid.uuid4().hex}",
            trace_id=trace_id,
            requester_subject_ref=auth.subject_ref,
            requester_persona_ref=auth.persona_ref,
            requester_role_refs=tuple(auth.role_refs),
            intent=intent,
            capability_id=capability_id,
            contract_revision=contract_revision or auth.registry_revision,
            constraints_ref=constraints_ref,
            eligible_owning_role=eligible_owning_role,
            created_at=local_minter.now(),
        )
        # Re-validate through the closed schema against the REAL catalogue role
        # namespace: the eligible owning role must be a role the catalogue
        # declares AND must not be a persona seat.
        return cn.parse_delegation_proposal(
            cn.delegation_proposal_to_json(proposal),
            known_role_ids=known_role_ids,
            known_personas=known_personas,
        )

    return _emit


__all__ = [
    "A2ADelivery",
    "DEFAULT_TTL_SECONDS",
    "DeliveryChannel",
    "DeliveryEnvelope",
    "DeliveryOutcome",
    "DelegationRecipient",
    "DelegationRoute",
    "DelegationRouter",
    "KNOWN_PERSONAS",
    "KNOWN_ROLE_IDS",
    "MailboxDelivery",
    "NativeInvoker",
    "NativeOperationOutcome",
    "NativePlanner",
    "ReceiptMinter",
    "ReceiptStore",
    "RecipientOutcome",
    "TicketDelivery",
    "delivery_envelope_to_json",
    "eligible_owning_role_for",
    "make_delegation_emitter",
    "parse_delivery_envelope",
    "proposal_digest",
]
