"""Tests for the ORG-059 Child E typed delegation-route adapter (T-3222).

Covers: the closed-schema DelegationProposal / DelegationReceipt validators, the
5-stage round-trip over an INJECTED fake delivery channel (subject flips to the
receiving role at acceptance; receipts chain via prior_receipt_ref), the real
MailboxDelivery over the existing mailbox substrate, the navigator-engine
DelegationProposal emission wire-in, and an adversarial test per HARD REFUSAL
guard (credential/lookalike reject; role-not-persona; recipient re-plans under
its own subject and never inherits requester authority; delivery-ack is never
invocation evidence; duplicate / expired / tampered / rerouted / refused /
overloaded / lost-result each a typed outcome).
"""

from __future__ import annotations

import datetime as dt
import itertools
import tempfile
import unittest
from pathlib import Path

from tools import capability_navigator_contract as cn
from tools import capability_navigator_engine as engine
from tools import delegation_route as dr
from tools import mailbox
from tools.test_capability_navigator_contract import (
    _auth,
    _fixture_projection,
    _provision,
    _record,
    _request,
    _subject_visibility,
)


NOW = "2026-09-28T00:00:00Z"
LATER = "2026-09-28T02:00:00Z"
ENGINE_NOW = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=dt.timezone.utc)

ROLE_IDS = frozenset({"engineering_orchestrator", "role:owner"})
PERSONAS = frozenset({"mason", "artisan", "orchestrator"})


def _minter(clock_value: str = NOW) -> dr.ReceiptMinter:
    counter = itertools.count()
    return dr.ReceiptMinter(
        clock=lambda: clock_value,
        id_gen=lambda stage: f"receipt:{stage}:{next(counter)}",
    )


def _proposal(**overrides: object) -> cn.DelegationProposal:
    data: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": "proposal:1",
        "trace_id": "trace:1",
        "requester_subject_ref": "subject:requester",
        "requester_persona_ref": "persona:consultant",
        "requester_role_refs": ["role:worker"],
        "intent": "produce a bounded effect",
        "capability_id": "alpha-capability",
        "contract_revision": "registry:1",
        "constraints_ref": None,
        "eligible_owning_role": "engineering_orchestrator",
        "created_at": NOW,
    }
    data.update(overrides)
    return cn.parse_delegation_proposal(data, known_role_ids=ROLE_IDS, known_personas=PERSONAS)


def _proposal_real_map(**overrides: object) -> cn.DelegationProposal:
    """Build a proposal validated against the ACTUAL AGENT_ROLE_MAP-derived role
    and persona sets (not synthetic disjoint sets)."""
    data: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": "proposal:1",
        "trace_id": "trace:1",
        "requester_subject_ref": "subject:requester",
        "requester_persona_ref": "persona:consultant",
        "requester_role_refs": ["role:worker"],
        "intent": "produce a bounded effect",
        "capability_id": "alpha-capability",
        "contract_revision": "registry:1",
        "constraints_ref": None,
        "eligible_owning_role": "engineering_orchestrator",
        "created_at": NOW,
    }
    data.update(overrides)
    return cn.parse_delegation_proposal(
        data, known_role_ids=dr.KNOWN_ROLE_IDS, known_personas=dr.KNOWN_PERSONAS
    )


def _receipt(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "receipt:1",
        "proposal_id": "proposal:1",
        "trace_id": "trace:1",
        "stage": "request",
        "subject_ref": "subject:requester",
        "status": "proposed",
        "created_at": NOW,
        "redacted_summary": "a summary",
        "prior_receipt_ref": None,
        "provider_native_evidence_refs": [],
        "authority_provenance_ref": None,
    }
    data.update(overrides)
    return data


class _FakeChannel:
    """Injected DeliveryChannel double — records the envelope, reports a status."""

    def __init__(self, *, status: str = "delivered") -> None:
        self.status = status
        self.sent: list[dr.DeliveryEnvelope] = []

    def send_proposal(self, envelope: dr.DeliveryEnvelope) -> dr.DeliveryOutcome:
        self.sent.append(envelope)
        return dr.DeliveryOutcome(status=self.status, transport_ref="fake:1", redacted_summary="fake delivery")

    def poll(self, proposal_id: str) -> tuple:
        return ()


class _Resolver:
    """SubjectResolver double for the RECEIVING role's own AuthenticatedContext."""

    def __init__(self, *, subject_ref: str, role_refs: tuple[str, ...]) -> None:
        self._ctx = cn.AuthenticatedContext(
            subject_ref=subject_ref,
            uid=2000,
            persona_ref="persona:owner",
            role_refs=role_refs,
            instance_ref="instance:owner",
            trust_zone="test-zone",
            registry_revision="registry:1",
        )

    def resolve(self, transport: object) -> cn.AuthenticatedContext:
        return self._ctx


class _Planner:
    def __init__(self) -> None:
        self.seen_auth: list[cn.AuthenticatedContext] = []

    def plan(self, auth: cn.AuthenticatedContext, proposal: cn.DelegationProposal) -> object:
        self.seen_auth.append(auth)
        return {"planned_for": proposal.capability_id}


class _Invoker:
    def __init__(self, outcome: dr.NativeOperationOutcome | None = None) -> None:
        self._outcome = outcome

    def invoke(self, auth, proposal, plan) -> dr.NativeOperationOutcome:
        if self._outcome is not None:
            return self._outcome
        # Native authority provenance is the RECEIVING role's own subject.
        return dr.NativeOperationOutcome(
            status="invoked",
            provider_native_evidence_refs=(f"native:{auth.subject_ref}:evidence",),
            authority_provenance_ref=f"authority:{auth.subject_ref}",
            redacted_summary="native op completed",
        )


def _owner_resolver(role: str = "engineering_orchestrator") -> _Resolver:
    return _Resolver(subject_ref="subject:owner", role_refs=(role,))


# ---------------------------------------------------------------------------
# Contract validators — DelegationProposal
# ---------------------------------------------------------------------------


class TestDelegationProposalValidator(unittest.TestCase):
    def test_valid_proposal_parses(self) -> None:
        proposal = _proposal()
        self.assertEqual(proposal.eligible_owning_role, "engineering_orchestrator")
        self.assertEqual(proposal.requester_role_refs, ("role:worker",))

    def test_guard1_rejects_credential_and_secret_lookalikes(self) -> None:
        # A naive impl would ignore/strip an unknown field; the closed schema +
        # lookalike reject REFUSES it. Each smuggled credential/identity-authority
        # field must raise, not pass.
        for smuggled in (
            "credential",
            "credentials",
            "secret",
            "token",
            "grant",
            "approver",
            "policy_revision",
            "authority_source",
            "uid",
            "account",
            "subject_ref",
            "persona_ref",
            "role_refs",
        ):
            base = {
                "schema_version": 1,
                "proposal_id": "proposal:1",
                "trace_id": "trace:1",
                "requester_subject_ref": "subject:requester",
                "requester_persona_ref": "persona:consultant",
                "requester_role_refs": ["role:worker"],
                "intent": "x",
                "capability_id": "alpha-capability",
                "contract_revision": "registry:1",
                "constraints_ref": None,
                "eligible_owning_role": "engineering_orchestrator",
                "created_at": NOW,
                smuggled: "smuggled-value",
            }
            with self.assertRaises(cn.NavigatorContractError, msg=smuggled) as ctx:
                cn.parse_delegation_proposal(base, known_role_ids=ROLE_IDS, known_personas=PERSONAS)
            self.assertIn(
                ctx.exception.code,
                (cn.NavigatorErrorCode.IDENTITY_LOOKALIKE, cn.NavigatorErrorCode.SCHEMA_CLOSED_OBJECT),
                msg=f"{smuggled} -> {ctx.exception.code}",
            )

    def test_guard2_rejects_named_persona_as_owning_role(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            _proposal(eligible_owning_role="mason")
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE)

    def test_guard2_rejects_unknown_role(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            _proposal(eligible_owning_role="role:not-declared")
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE)

    def test_missing_required_field_rejects(self) -> None:
        base = cn.delegation_proposal_to_json(_proposal())
        del base["intent"]
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_proposal(base, known_role_ids=ROLE_IDS, known_personas=PERSONAS)
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.SCHEMA_REQUIRED_FIELD)

    def test_bad_schema_version_rejects(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            _proposal(schema_version=2)
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.UNSUPPORTED_VERSION)


# ---------------------------------------------------------------------------
# Contract validators — DelegationReceipt
# ---------------------------------------------------------------------------


class TestDelegationReceiptValidator(unittest.TestCase):
    def test_valid_stage_receipts_parse(self) -> None:
        self.assertEqual(cn.parse_delegation_receipt(_receipt()).stage, "request")
        delivery = cn.parse_delegation_receipt(
            _receipt(receipt_id="r2", stage="delivery", status="delivered", prior_receipt_ref="r1")
        )
        self.assertEqual(delivery.stage, "delivery")
        result = cn.parse_delegation_receipt(
            _receipt(
                receipt_id="r5",
                stage="result",
                status="completed",
                subject_ref="subject:owner",
                prior_receipt_ref="r4",
                provider_native_evidence_refs=["native:evidence"],
                authority_provenance_ref="authority:owner",
            )
        )
        self.assertEqual(result.provider_native_evidence_refs, ("native:evidence",))

    def test_guard4_delivery_ack_cannot_carry_native_evidence(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(
                _receipt(
                    stage="delivery",
                    status="delivered",
                    prior_receipt_ref="r1",
                    provider_native_evidence_refs=["native:sneaky"],
                )
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD)

    def test_guard4_delivery_ack_cannot_carry_authority_provenance(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(
                _receipt(
                    stage="delivery",
                    status="delivered",
                    prior_receipt_ref="r1",
                    authority_provenance_ref="authority:sneaky",
                )
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD)

    def test_guard4_result_requires_native_evidence(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(
                _receipt(
                    receipt_id="r5",
                    stage="result",
                    status="completed",
                    prior_receipt_ref="r4",
                    provider_native_evidence_refs=[],
                    authority_provenance_ref="authority:owner",
                )
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD)

    def test_result_requires_authority_provenance(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(
                _receipt(
                    receipt_id="r5",
                    stage="result",
                    status="completed",
                    prior_receipt_ref="r4",
                    provider_native_evidence_refs=["native:evidence"],
                    authority_provenance_ref=None,
                )
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD)

    def test_request_root_rejects_prior_receipt(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(_receipt(prior_receipt_ref="r0"))
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_STAGE_PAYLOAD)

    def test_non_root_requires_prior_receipt(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(
                _receipt(stage="delivery", status="delivered", prior_receipt_ref=None)
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.SCHEMA_REQUIRED_FIELD)

    def test_bad_stage_and_status_reject(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(_receipt(stage="teleport"))
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.SCHEMA_ENUM)
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(_receipt(status="totally-fine-trust-me"))
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.SCHEMA_ENUM)

    def test_secret_bearing_receipt_content_rejects(self) -> None:
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            cn.parse_delegation_receipt(_receipt(redacted_summary={"credential": "x"}))
        self.assertIn(
            ctx.exception.code,
            (cn.NavigatorErrorCode.FORBIDDEN_RESULT_CONTAINER, cn.NavigatorErrorCode.SCHEMA_TYPE),
        )


# ---------------------------------------------------------------------------
# Round-trip over an injected fake channel
# ---------------------------------------------------------------------------


class TestRoundTrip(unittest.TestCase):
    def _run(self):
        proposal = _proposal()
        channel = _FakeChannel()
        store = dr.ReceiptStore()
        router = dr.DelegationRouter(channel, minter=_minter(), store=store)
        route = router.route(proposal)
        planner = _Planner()
        recipient = dr.DelegationRecipient(
            _owner_resolver(), planner, _Invoker(), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(route.envelope, route.delivery)
        return route, planner, recipient, outcome

    def test_five_distinct_receipts_chain(self) -> None:
        route, planner, recipient, outcome = self._run()
        chain = [route.request, route.delivery, *outcome.receipts]
        stages = [r.stage for r in chain]
        self.assertEqual(
            stages, ["request", "delivery", "acceptance", "native_operation", "result"]
        )
        # prior_receipt_ref chains request -> delivery -> acceptance -> native -> result.
        self.assertIsNone(route.request.prior_receipt_ref)
        self.assertEqual(route.delivery.prior_receipt_ref, route.request.receipt_id)
        self.assertEqual(outcome.receipts[0].prior_receipt_ref, route.delivery.receipt_id)
        self.assertEqual(outcome.receipts[1].prior_receipt_ref, outcome.receipts[0].receipt_id)
        self.assertEqual(outcome.receipts[2].prior_receipt_ref, outcome.receipts[1].receipt_id)

    def test_subject_flips_to_receiving_role_at_acceptance(self) -> None:
        route, planner, recipient, outcome = self._run()
        # request/delivery carry the requester's subject...
        self.assertEqual(route.request.subject_ref, "subject:requester")
        self.assertEqual(route.delivery.subject_ref, "subject:requester")
        # ...acceptance/native/result carry the RECEIVING role's own subject.
        for r in outcome.receipts:
            self.assertEqual(r.subject_ref, "subject:owner")

    def test_recipient_replans_under_own_subject(self) -> None:
        route, planner, recipient, outcome = self._run()
        # Planning was re-run under the receiving role's own authenticated context.
        self.assertEqual(len(planner.seen_auth), 1)
        self.assertEqual(planner.seen_auth[0].subject_ref, "subject:owner")

    def test_result_completed_with_native_evidence_and_own_authority(self) -> None:
        route, planner, recipient, outcome = self._run()
        result = outcome.receipts[-1]
        self.assertEqual(outcome.terminal_status, "completed")
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.provider_native_evidence_refs)
        # Authority provenance is the RECEIVING role's own, never the requester's.
        self.assertEqual(result.authority_provenance_ref, "authority:subject:owner")


# ---------------------------------------------------------------------------
# Adversarial refusal guards (each red-fails a naive/missing implementation)
# ---------------------------------------------------------------------------


class TestRefusalGuards(unittest.TestCase):
    def _route(self, proposal, channel):
        router = dr.DelegationRouter(channel, minter=_minter(), store=dr.ReceiptStore())
        return router

    def test_guard3_recipient_refuses_without_native_authority(self) -> None:
        # The receiving role does NOT hold the eligible owning role -> it refuses;
        # it never inherits the requester's authority and never auto-executes.
        proposal = _proposal()
        channel = _FakeChannel()
        router = self._route(proposal, channel)
        route = router.route(proposal)
        planner = _Planner()
        recipient = dr.DelegationRecipient(
            _Resolver(subject_ref="subject:owner", role_refs=("role:something-else",)),
            planner,
            _Invoker(),
            minter=_minter(),
            store=dr.ReceiptStore(),
        )
        outcome = recipient.receive(route.envelope, route.delivery)
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.terminal_status, "refused")
        self.assertEqual(outcome.receipts[0].stage, "acceptance")
        self.assertEqual(outcome.receipts[0].status, "refused")
        # The requester's plan was NEVER executed on refusal.
        self.assertEqual(planner.seen_auth, [])

    def test_guard4_router_delivery_receipt_has_no_evidence(self) -> None:
        route = self._route(_proposal(), _FakeChannel()).route(_proposal())
        self.assertEqual(route.delivery.provider_native_evidence_refs, ())
        self.assertIsNone(route.delivery.authority_provenance_ref)

    def test_guard5_duplicate_at_sender(self) -> None:
        proposal = _proposal()
        router = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore())
        first = router.propose(proposal)
        second = router.route(proposal)
        self.assertEqual(first.status, "proposed")
        self.assertEqual(second.request.status, "duplicate")
        self.assertIsNone(second.delivery)  # a duplicate is never re-delivered

    def test_guard5_duplicate_at_recipient(self) -> None:
        proposal = _proposal()
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(proposal)
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(), minter=_minter(), store=dr.ReceiptStore()
        )
        recipient.receive(route.envelope, route.delivery)
        again = recipient.receive(route.envelope, route.delivery)
        self.assertEqual(again.terminal_status, "duplicate")
        self.assertEqual(again.receipts[0].status, "duplicate")

    def test_guard5_expired(self) -> None:
        proposal = _proposal()
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(proposal)
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(route.envelope, route.delivery, now=LATER)
        self.assertEqual(outcome.terminal_status, "expired")

    def test_guard5_tampered(self) -> None:
        proposal = _proposal()
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(proposal)
        # Mutate the proposal in flight while keeping the stale digest.
        mutated = dr.DeliveryEnvelope(
            proposal=_proposal(intent="tampered intent"),
            proposal_digest=route.envelope.proposal_digest,
            sent_at=route.envelope.sent_at,
            ttl_seconds=route.envelope.ttl_seconds,
        )
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(mutated, route.delivery)
        self.assertEqual(outcome.terminal_status, "tampered")

    def test_guard5_rerouted(self) -> None:
        route = dr.DelegationRouter(
            _FakeChannel(status="rerouted"), minter=_minter(), store=dr.ReceiptStore()
        ).route(_proposal())
        self.assertEqual(route.delivery.status, "rerouted")

    def test_guard5_overloaded_is_typed_not_silent(self) -> None:
        # ORG-059:R4 — delegation overload surfaces as a typed outcome, not policy.
        overloaded = dr.NativeOperationOutcome(
            status="overloaded",
            provider_native_evidence_refs=(),
            authority_provenance_ref=None,
            redacted_summary="owning role overloaded",
        )
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(_proposal())
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(overloaded), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(route.envelope, route.delivery)
        self.assertEqual(outcome.receipts[1].status, "overloaded")
        self.assertEqual(outcome.terminal_status, "lost-result")

    def test_guard5_lost_result_on_missing_native_evidence(self) -> None:
        # "invoked" but with NO native evidence cannot normalize as success.
        empty = dr.NativeOperationOutcome(
            status="invoked",
            provider_native_evidence_refs=(),
            authority_provenance_ref="authority:subject:owner",
            redacted_summary="no evidence produced",
        )
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(_proposal())
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(empty), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(route.envelope, route.delivery)
        self.assertEqual(outcome.terminal_status, "lost-result")
        self.assertEqual(outcome.receipts[-1].status, "lost-result")

    def test_guard5_tampered_native_evidence_cannot_normalize(self) -> None:
        # A control-character evidence ref is tampered/non-normalizable -> lost-result,
        # and it never reaches a stored receipt as raw refs.
        tampered = dr.NativeOperationOutcome(
            status="invoked",
            provider_native_evidence_refs=("native:\u0001forged",),
            authority_provenance_ref="authority:subject:owner",
            redacted_summary="forged evidence",
        )
        route = dr.DelegationRouter(_FakeChannel(), minter=_minter(), store=dr.ReceiptStore()).route(_proposal())
        recipient = dr.DelegationRecipient(
            _owner_resolver(), _Planner(), _Invoker(tampered), minter=_minter(), store=dr.ReceiptStore()
        )
        outcome = recipient.receive(route.envelope, route.delivery)
        self.assertEqual(outcome.terminal_status, "lost-result")
        self.assertEqual(outcome.receipts[1].provider_native_evidence_refs, ())


# ---------------------------------------------------------------------------
# Real MailboxDelivery over the existing mailbox substrate
# ---------------------------------------------------------------------------


class TestMailboxDelivery(unittest.TestCase):
    def test_send_and_poll_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            recipient_inbox = Path(td) / "recipient"
            return_inbox = Path(td) / "sender"
            mailbox.provision(recipient_inbox)
            mailbox.provision(return_inbox)
            channel = dr.MailboxDelivery(
                recipient_inbox=recipient_inbox,
                return_inbox=return_inbox,
                sender="mason",
                known_role_ids=ROLE_IDS,
            )
            proposal = _proposal()
            envelope = dr.DeliveryEnvelope(
                proposal=proposal,
                proposal_digest=dr.proposal_digest(proposal),
                sent_at=NOW,
                ttl_seconds=3600,
            )
            outcome = channel.send_proposal(envelope)
            self.assertEqual(outcome.status, "delivered")

            # The recipient seat reads the proposal off its inbox and round-trips it.
            received: list[dr.DeliveryEnvelope] = []

            def _read(msg: dict) -> None:
                raw = msg["payload"]["envelope"]
                received.append(
                    dr.parse_delivery_envelope(raw, known_role_ids=ROLE_IDS)
                )

            mailbox.consume(recipient_inbox, handler=_read, use_lease=False)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].proposal.proposal_id, "proposal:1")

            # The receiving role posts a stage receipt back; the sender polls it.
            receipt = cn.parse_delegation_receipt(
                _receipt(stage="acceptance", status="accepted", subject_ref="subject:owner", prior_receipt_ref="r1")
            )
            channel.post_receipt(receipt)
            polled = channel.poll("proposal:1")
            self.assertEqual(len(polled), 1)
            self.assertEqual(polled[0]["status"], "accepted")
            # A receipt for a different proposal is filtered out.
            self.assertEqual(channel.poll("proposal:other"), ())

    def test_poll_with_anchors_drops_forged_receipt(self) -> None:
        # GAP C: a well-formed forged receipt injected into the return inbox with
        # a bogus prior_receipt_ref is dropped when the sender polls with its own
        # minted anchor receipt ids; only the chain-authentic receipt survives.
        with tempfile.TemporaryDirectory() as td:
            return_inbox = Path(td) / "sender"
            recipient_inbox = Path(td) / "recipient"
            mailbox.provision(return_inbox)
            mailbox.provision(recipient_inbox)
            channel = dr.MailboxDelivery(
                recipient_inbox=recipient_inbox,
                return_inbox=return_inbox,
                sender="mason",
                known_role_ids=ROLE_IDS,
            )
            # The sender's minted delivery receipt id is the anchor the receiving
            # side's acceptance must chain to.
            anchor = "delivery:1"
            legit = cn.parse_delegation_receipt(
                _receipt(
                    receipt_id="acceptance:1", stage="acceptance", status="accepted",
                    subject_ref="subject:owner", prior_receipt_ref=anchor,
                )
            )
            forged = cn.parse_delegation_receipt(
                _receipt(
                    receipt_id="forged:1", stage="result", status="lost-result",
                    subject_ref="subject:owner", prior_receipt_ref="never-minted",
                )
            )
            channel.post_receipt(legit)
            channel.post_receipt(forged)
            verified = channel.poll("proposal:1", anchor_receipt_ids=frozenset({anchor}))
            ids = {r["receipt_id"] for r in verified}
            self.assertEqual(ids, {"acceptance:1"})

    def test_missing_recipient_inbox_is_typed_reroute(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            return_inbox = Path(td) / "sender"
            mailbox.provision(return_inbox)
            channel = dr.MailboxDelivery(
                recipient_inbox=Path(td) / "never-provisioned",
                return_inbox=return_inbox,
                sender="mason",
                known_role_ids=ROLE_IDS,
            )
            proposal = _proposal()
            envelope = dr.DeliveryEnvelope(
                proposal=proposal,
                proposal_digest=dr.proposal_digest(proposal),
                sent_at=NOW,
                ttl_seconds=3600,
            )
            outcome = channel.send_proposal(envelope)
            self.assertEqual(outcome.status, "rerouted")


# ---------------------------------------------------------------------------
# Navigator-engine DelegationProposal emission wire-in
# ---------------------------------------------------------------------------


def _owning_projection():
    # A capability whose catalogue subject_visibility declares an owning role
    # ("role:owner") that the default _auth() subject (role:worker) does not hold.
    provision = _provision(
        subject_visibility=_subject_visibility(role_refs=["role:owner", "role:worker"])
    )
    record = _record(provides=[provision])
    _, _, _, projection = _fixture_projection(records_data=[record])
    return projection


def _plan_request():
    raw = _request(
        "plan",
        {"capability_id": "alpha-capability", "typed_input": {}, "bounded_purpose": "produce effect"},
    )
    return cn.parse_request(raw, auth=_auth())


class TestEngineEmission(unittest.TestCase):
    def test_plan_without_emitter_is_bare_refusal(self) -> None:
        outcome = engine.handle_request(
            _auth(), _plan_request(), _owning_projection(), {}, None, ENGINE_NOW
        )
        self.assertIsInstance(outcome, engine.EngineRefusal)
        self.assertEqual(outcome.code, engine.EngineRefusalCode.MISSING_EFFECT_CONTRACT_AUTHORITY)

    def test_plan_with_emitter_returns_delegation_proposal(self) -> None:
        emitter = dr.make_delegation_emitter(minter=_minter())
        outcome = engine.handle_request(
            _auth(), _plan_request(), _owning_projection(), {}, None, ENGINE_NOW,
            delegation_emitter=emitter,
        )
        self.assertIsInstance(outcome, cn.DelegationProposal)
        # Requester identity is lifted from the AUTHENTICATED context, not caller input.
        self.assertEqual(outcome.requester_subject_ref, "subject:alpha")
        self.assertEqual(outcome.requester_persona_ref, "persona:consultant")
        self.assertEqual(outcome.requester_role_refs, ("role:worker",))
        self.assertEqual(outcome.capability_id, "alpha-capability")
        # The eligible owning role is the catalogue-declared role the subject lacks.
        self.assertEqual(outcome.eligible_owning_role, "role:owner")

    def test_emitter_rejects_persona_against_real_catalogue_set(self) -> None:
        # REBOUND BLOCKING-1: validation must be against the REAL catalogue role
        # namespace, not a self-singleton. A persona as eligible_owning_role is
        # rejected even though it is the candidate.
        emitter = dr.make_delegation_emitter(minter=_minter())
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            emitter(
                auth=_auth(),
                capability_id="alpha-capability",
                eligible_owning_role="mason",
                intent="x",
                trace_id="trace:1",
                known_role_ids=frozenset({"role:owner"}),
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE)

    def test_emitter_rejects_garbage_not_in_catalogue(self) -> None:
        # REBOUND BLOCKING-1: arbitrary garbage NOT declared by the catalogue is
        # rejected (the pre-fix self-singleton would have accepted it).
        emitter = dr.make_delegation_emitter(minter=_minter())
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            emitter(
                auth=_auth(),
                capability_id="alpha-capability",
                eligible_owning_role="totally_made_up_role",
                intent="x",
                trace_id="trace:1",
                known_role_ids=frozenset({"role:owner"}),
            )
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE)

    def test_emitter_accepts_real_catalogue_role(self) -> None:
        emitter = dr.make_delegation_emitter(minter=_minter())
        proposal = emitter(
            auth=_auth(),
            capability_id="alpha-capability",
            eligible_owning_role="role:owner",
            intent="x",
            trace_id="trace:1",
            known_role_ids=frozenset({"role:owner"}),
        )
        self.assertEqual(proposal.eligible_owning_role, "role:owner")

    def test_poisoned_catalogue_persona_role_falls_back_to_refusal(self) -> None:
        # REBOUND BLOCKING-1 end-to-end: a catalogue that declares a PERSONA seat
        # as its only owning role produces NO proposal — the engine fails closed
        # to the bare authority refusal, never emitting an invalid proposal.
        provision = _provision(subject_visibility=_subject_visibility(role_refs=["mason"]))
        _, _, _, projection = _fixture_projection(records_data=[_record(provides=[provision])])
        emitter = dr.make_delegation_emitter(minter=_minter())
        outcome = engine.handle_request(
            _auth(), _plan_request(), projection, {}, None, ENGINE_NOW, delegation_emitter=emitter
        )
        self.assertIsInstance(outcome, engine.EngineRefusal)
        self.assertEqual(outcome.code, engine.EngineRefusalCode.MISSING_EFFECT_CONTRACT_AUTHORITY)

    def test_real_map_peer_daemon_persona_rejected(self) -> None:
        # REBOUND BLOCKING-2: a persona seat is never an execution primitive. The
        # self-colliding case it was written for (the-ancestor, persona == role-id) left
        # the real map with the persona (T-3421); `codex` stands in as a peer-daemon seat.
        self.assertIn("codex", dr.KNOWN_PERSONAS)
        with self.assertRaises(cn.NavigatorContractError) as ctx:
            _proposal_real_map(eligible_owning_role="codex")
        self.assertEqual(ctx.exception.code, cn.NavigatorErrorCode.DELEGATION_ROLE_NOT_A_ROLE)

    def test_real_map_accepts_a_genuine_role_class(self) -> None:
        # Guard #2 must not over-reject: a genuine role-class id that is NOT a
        # persona seat is accepted under the real map.
        proposal = _proposal_real_map(eligible_owning_role="engineering_orchestrator")
        self.assertEqual(proposal.eligible_owning_role, "engineering_orchestrator")

    def test_no_emission_when_subject_already_holds_every_declared_role(self) -> None:
        # auth holds role:worker; a capability visible only to role:worker leaves
        # no eligible owning role -> the bare refusal is preserved even with an emitter.
        provision = _provision(subject_visibility=_subject_visibility(role_refs=["role:worker"]))
        _, _, _, projection = _fixture_projection(records_data=[_record(provides=[provision])])
        emitter = dr.make_delegation_emitter(minter=_minter())
        outcome = engine.handle_request(
            _auth(), _plan_request(), projection, {}, None, ENGINE_NOW, delegation_emitter=emitter
        )
        self.assertIsInstance(outcome, engine.EngineRefusal)


if __name__ == "__main__":
    unittest.main()
