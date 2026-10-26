"""T-2919 (ORG-052 Unit 7): Telegram human-approval ingress — service +
ingress + loopback fake Bot API. Stdlib only, NO dbus: runs in the main
CI tools suite without skipping. The private-bus tier is
tools/test_host_control_telegram_ingress_dbus.py.

Gates: 14 (spoof matrix, red before green; credential + cursor
isolation), 21 (leak canaries), 22 (mailbox/ticket content can request
review but never mint authority), plus single-use / expiry / uid gate /
crash recovery and the mutation checks of every key guard.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tools.host_control.approval import human as approval_human
from tools.host_control.approval import service as service_mod
from tools.host_control.approval import wire as approval_wire
from tools.host_control.approval.contracts import ApproverDenied
from tools.host_control.approval.human import (
    HumanDecisionRefused,
    HumanIngressRegistration,
)
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    PolicyApproverAuthorizer,
    parse_approver_policy,
)
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import (
    HostControlError,
    IdempotencyConflict,
    PolicyDenied,
)
from tools.host_control.telegram_ingress import bot_api as bot_api_mod
from tools.host_control.telegram_ingress import card as card_mod
from tools.host_control.telegram_ingress import core as core_mod
from tools.host_control.telegram_ingress import wire as ingress_wire
from tools.host_control.telegram_ingress.bot_api import BotApiClient, BotApiError
from tools.host_control.telegram_ingress.config import (
    IngressConfigError,
    load_ingress_config,
    parse_ingress_config,
)
from tools.host_control.telegram_ingress.core import (
    CALLBACK_CHECKS,
    DecisionUnavailable,
    IngressCore,
    IngressLog,
    PresentRefused,
)
from tools.host_control.telegram_ingress.main import (
    StartupRefused,
    build_core,
    learn_bot_user_id,
)
from tools.host_control.telegram_ingress.state import IngressState
from tools.test_host_control_telegram_ingress_fixtures import (
    BOT_ID,
    CANARY_DIGEST,
    CANARY_TOKEN,
    CANARY_TOKEN_TEXT,
    CONSUMER_UID,
    EFFECT_CANARY,
    HUMAN_CHAT,
    HUMAN_USER,
    INGRESS_UID,
    NONCE_MARK,
    OTHER_CHAT,
    OTHER_DIGEST,
    OTHER_PLAN_HANDLE,
    OTHER_USER,
    PLAN_HANDLE,
    FakeBotApi,
    InProcessDecider,
    RecordingDecider,
    build_human_stack,
    human_plan,
    human_policy_raw,
    registration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "tools" / "host_control" / "dist"
INGRESS_PKG = REPO_ROOT / "tools" / "host_control" / "telegram_ingress"
# no leading \b: a token right after "/bot" must still match (review
# finding 2)
TOKEN_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_TOKEN_SECRET = CANARY_TOKEN_TEXT.split(":", 1)[1]
TOKEN_FRAGMENTS = frozenset(
    _TOKEN_SECRET[i:i + 12] for i in range(len(_TOKEN_SECRET) - 11)
)


def leaks_token(blob: str) -> bool:
    """True if ANY 12-char substring of the token's secret part appears
    (catches truncated / partial disclosure, e.g. a 64-char log detail)."""

    return TOKEN_SHAPE.search(blob) is not None or any(
        fragment in blob for fragment in TOKEN_FRAGMENTS
    )


class _StackCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stack = build_human_stack(self.root / "s")

    def tearDown(self) -> None:
        self.stack.close()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------

    def presented_card(self, key: str = "rh-1"):
        review = self.stack.open_review()
        result = self.stack.request(review, key)
        self.assertEqual(result.state, "presented")
        return review, self.stack.fake.last_card()

    def request_record(self, review):
        return self.stack.journal.get_human_request_for_review(review.review_handle)

    def process_next(self) -> str:
        """Fetch exactly the queued updates and return the last verdict."""

        updates = self.stack.core.api.get_updates(
            offset=self.stack.state.cursor(), timeout=0
        )
        verdict = "none"
        for update in updates:
            verdict = self.stack.core.process_update(update)
        return verdict

    def assert_no_authority(self, review) -> None:
        live = self.stack.journal.get_review(review.review_handle)
        self.assertEqual(live.state, "open")
        self.assertIsNone(self.stack.journal.get_attestation_for_review(review.review_handle))
        self.assertIsNone(self.stack.journal.get_grant_for_review(review.review_handle))


# ---------------------------------------------------------------------------
# RequestHuman: one record, one nonce, one card
# ---------------------------------------------------------------------------


class RequestHumanTests(_StackCase):
    def test_one_record_one_nonce_one_card_and_replays_never_resend(self) -> None:
        review = self.stack.open_review()
        first = self.stack.request(review, "rh-1")
        self.assertEqual(first.state, "presented")
        self.assertFalse(first.replayed)
        replay = self.stack.request(review, "rh-1")
        other_key = self.stack.request(review, "rh-2")
        self.assertEqual({replay.request_handle, other_key.request_handle}, {first.request_handle})
        self.assertTrue(replay.replayed and other_key.replayed)
        self.assertEqual(len(self.stack.fake.messages), 1)
        self.assertEqual(len(self.stack.presenter.calls), 1)
        record = self.request_record(review)
        self.assertTrue(record.nonce.startswith(NONCE_MARK))
        self.assertEqual(record.state, "presented")
        # the card is rendered from the record: digest + effects present,
        # the nonce never
        text = self.stack.fake.last_card()["text"]
        self.assertIn(CANARY_DIGEST, text)
        self.assertIn(EFFECT_CANARY, text)
        self.assertNotIn(record.nonce, text)
        # the presenter received exactly the record's card summary
        _, digest, summary_json, nonce, expires = self.stack.presenter.calls[0]
        self.assertEqual(json.loads(summary_json), record.card_summary())
        self.assertEqual((digest, nonce, expires), (record.plan_digest, record.nonce, record.expires_at))

    def test_refusals_never_create_a_record(self) -> None:
        review = self.stack.open_review()
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.request_human("specialist", review.review_handle, "0" * 64, "k")
        self.assertEqual(caught.exception.reason_code, "digest_mismatch")
        with self.assertRaises(ApproverDenied):
            self.stack.service.request_human("alpha", review.review_handle, review.plan_digest, "k")
        # no human approver standing for this family
        self.stack.metadata.plans["6" * 64] = human_plan("7" * 64, family="persona.install")
        other = self.stack.service.review("specialist", "6" * 64, "7" * 64, None).record
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.request_human("specialist", other.review_handle, other.plan_digest, "k")
        self.assertEqual(caught.exception.reason_code, "no_human_approver")
        # an expired review cannot get a card
        self.stack.clock.advance(hours=2)
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.request_human("specialist", review.review_handle, review.plan_digest, "k")
        self.assertEqual(caught.exception.reason_code, "review_expired")
        self.assertEqual(self.stack.fake.messages, {})
        self.assertIsNone(self.request_record(review))

    def test_unconfigured_ingress_refuses(self) -> None:
        journal = ApprovalJournal.open(self.root / "plain.sqlite3")
        try:
            service = ApprovalService(
                journal=journal,
                metadata_source=self.stack.metadata,
                clock=self.stack.clock,
                authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
            )
            review = service.review("specialist", PLAN_HANDLE, CANARY_DIGEST, None).record
            with self.assertRaises(HumanDecisionRefused) as caught:
                service.request_human("specialist", review.review_handle, review.plan_digest, "k")
            self.assertEqual(caught.exception.reason_code, "human_ingress_unconfigured")
            with self.assertRaises(PolicyDenied):
                service.human_decision(INGRESS_UID, review.review_handle, CANARY_DIGEST,
                                       "granted", "s", "c" * 64, "k")
        finally:
            journal.close()

    def test_construction_cross_checks_the_ingress_trust_domain(self) -> None:
        with self.assertRaises(HostControlError):
            ApprovalService(
                journal=self.stack.journal,
                metadata_source=self.stack.metadata,
                clock=self.stack.clock,
                authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
                human_ingress=registration(uid=INGRESS_UID + 1),
            )
        with self.assertRaises(HostControlError):
            ApprovalService(
                journal=self.stack.journal,
                metadata_source=self.stack.metadata,
                clock=self.stack.clock,
                authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
                human_ingress=registration(),
                registered_consumer_uids=frozenset((INGRESS_UID,)),
            )

    def test_undelivered_card_stays_pending_cannot_be_decided_then_replay_presents(self) -> None:
        self.stack.fake.faults["sendMessage"] = "500"
        review = self.stack.open_review()
        first = self.stack.request(review)
        self.assertEqual(first.state, "pending")
        record = self.request_record(review)
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.human_decision(
                INGRESS_UID, review.review_handle, review.plan_digest, "granted",
                "src", record.nonce, "k1")
        self.assertEqual(caught.exception.reason_code, "human_request_not_presented")
        del self.stack.fake.faults["sendMessage"]
        again = self.stack.request(review)
        self.assertEqual((again.request_handle, again.state), (first.request_handle, "presented"))
        self.assertEqual(len(self.stack.fake.messages), 1)

    def test_present_contract_refusals_send_nothing(self) -> None:
        review, _card = self.presented_card()
        record = self.request_record(review)
        sent = len(self.stack.fake.messages)
        summary = json.dumps(record.card_summary())
        cases = {
            "conflicting nonce": (record.review_handle, record.plan_digest, summary, "e" * 64, record.expires_at),
            "extra key": (record.review_handle, record.plan_digest,
                          json.dumps({**record.card_summary(), "nonce": record.nonce}), record.nonce, record.expires_at),
            "digest mismatch": (record.review_handle, "0" * 64, summary, record.nonce, record.expires_at),
            "not hex": (record.review_handle, record.plan_digest, summary, "zz", record.expires_at),
        }
        for label, args in cases.items():
            with self.subTest(label):
                with self.assertRaises(PresentRefused):
                    self.stack.core.present(*args)
        # expiry beyond the ingress's policy ceiling
        far = (self.stack.clock.now + timedelta(days=2)).isoformat()
        far_summary = json.dumps({**record.card_summary(), "expires_at": far, "review_handle": "9" * 64})
        with self.assertRaises(PresentRefused):
            self.stack.core.present("9" * 64, record.plan_digest, far_summary, "8" * 64, far)
        self.assertEqual(len(self.stack.fake.messages), sent)
        # an identical replay returns the SAME delivery, no second card
        self.assertEqual(
            self.stack.core.present(record.review_handle, record.plan_digest, summary, record.nonce, record.expires_at),
            ("presented", record.provider_message_ref),
        )
        self.assertEqual(len(self.stack.fake.messages), sent)


class ReviewFindingTests(_StackCase):
    """Fold-ins from the T-2919 independent review (findings 1, 5, 7, 8)."""

    def test_present_timeout_after_send_then_tap_then_replay_ends_decided_once(self) -> None:
        """Finding 1: the approval side's Present call times out AFTER the
        ingress already sent the card; the human taps; the RequestHuman
        replay reaches 'presented'; recovery completes the decision —
        exactly once."""

        stack = self.stack

        class TimeoutAfterDelivery:
            def __init__(self, core):
                self.core = core

            def present(self, *args):
                self.core.present(*args)  # the ingress serviced it late...
                raise TimeoutError("NoReply")  # ...after the caller gave up

        stack.service._presenter = TimeoutAfterDelivery(stack.core)
        review = stack.open_review()
        self.assertEqual(stack.request(review).state, "pending")
        card = stack.fake.last_card()
        stack.fake.callback(message=card, data=stack.fake.button_data(card, 0))
        self.assertEqual(stack.core.poll_once(timeout=0), 1)
        # retryable, NOT refused: intent pending; the card is closed locally
        # (decision_pending) and only the Telegram message is left untouched
        card_row = stack.state.card_for_review(review.review_handle)
        self.assertEqual((card_row.state, card_row.closed_reason), ("closed", "decision_pending"))
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        self.assertIn("reply_markup", stack.fake.card_message(card["chat"]["id"], card["message_id"]))
        self.assertEqual(stack.journal.get_review(review.review_handle).state, "open")
        self.assertIn("detail=human_request_not_presented", stack.log_stream.getvalue())
        # recovery before the service knows the delivery: still pending
        self.assertEqual(stack.core.recover(), 0)
        stack.service._presenter = stack.presenter  # healthy again
        replay = stack.request(review, "rh-2")
        self.assertEqual(replay.state, "presented")
        self.assertEqual(len(stack.fake.messages), 1)
        self.assertEqual(stack.core.recover(), 1)
        live = stack.journal.get_review(review.review_handle)
        self.assertEqual((live.state, live.authority_source), ("granted", "human"))
        self.assertEqual(stack.journal.counts().issued_grants, 1)
        self.assertEqual(self.request_record(review).state, "decided")
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "granted")])
        self.assertEqual(stack.core.recover(), 0)
        self.assertEqual(stack.request(review, "rh-3").state, "decided")
        with sqlite3.connect(stack.root / "approval.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM provider_attestations").fetchone()[0], 1)

    def _tap_and_decide(self, effects: str) -> tuple[dict, str]:
        self.stack.metadata.summaries[PLAN_HANDLE] = effects
        review, card = self.presented_card()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        verdict = self.process_next()
        return card, verdict

    def test_edited_card_digest_uses_the_text_telegram_returned(self) -> None:
        """Finding 5: the fake (like Telegram) trims surrounding
        whitespace; the card digest is taken from the RETURNED text, so a
        genuine tap on a card whose effects end in whitespace is accepted."""

        sent_before = len(self.stack.fake.requests)
        card, verdict = self._tap_and_decide("create account svc-demo   \n\t  ")
        self.assertEqual(verdict, "accepted")
        sent = [r for r in self.stack.fake.requests[sent_before:] if r.method == "sendMessage"][0]
        rendered = json.loads(sent.body)["text"]
        self.assertNotEqual(rendered, card["text"])  # Telegram normalized it
        self.assertEqual(rendered.strip(), card["text"])

    def test_emoji_heavy_card_is_bounded_in_utf16_units_and_accepted(self) -> None:
        effects = "\U0001F600\U0001F680\U0001F4A5 " * 1300  # astral: 2 units each
        card, verdict = self._tap_and_decide(effects[:4000])
        self.assertEqual(verdict, "accepted")
        units = len(card["text"].encode("utf-16-le")) // 2
        self.assertLessEqual(units, card_mod.MAX_CARD_TEXT)
        self.assertLess(len(card["text"]), units)  # code points < UTF-16 units

    def test_service_refuses_an_agent_approver_on_the_ingress_uid(self) -> None:
        """Finding 7: the network-facing ingress uid can never carry
        Reviewer standing — construction refuses."""

        policy = human_policy_raw(agent_uid=INGRESS_UID)
        with self.assertRaises(HostControlError):
            ApprovalService(
                journal=self.stack.journal,
                metadata_source=self.stack.metadata,
                clock=self.stack.clock,
                authorizer=PolicyApproverAuthorizer(parse_approver_policy(policy)),
                human_ingress=registration(),
            )

    def test_utf16_boundary_2000_emoji_card_stays_within_3500_units(self) -> None:
        """r2: effects of 2000 astral emoji = 2000 code points but 4000
        UTF-16 units. Measured with len() the effects bound would pass
        them untruncated and the card would exceed Telegram's limit (the
        fake rejects it, the card never delivers); measured in UTF-16 units
        the card is truncated to <= 3500 units and delivered."""

        effects = "\U0001F600" * 2000
        self.assertEqual((len(effects), len(effects.encode("utf-16-le")) // 2), (2000, 4000))
        card, verdict = self._tap_and_decide(effects)
        self.assertEqual(verdict, "accepted")
        self.assertLessEqual(card_mod.utf16_len(card["text"]), card_mod.MAX_CARD_TEXT)
        self.assertLessEqual(len(card["text"].encode("utf-16-le")) // 2, 3500)
        self.assertIn("[truncated]", card["text"])

    def test_send_message_reply_without_text_fails_closed(self) -> None:
        """r2: a sendMessage reply with no (or empty) text cannot open a
        card: there is no provider text to digest for the edited_card
        check. The ingress refuses with its closed 'unavailable' outcome
        and a 'shape' event (never a raw KeyError), and RequestHuman stays
        pending."""

        for fault in ("no_text", "empty_text"):
            with self.subTest(fault=fault):
                stack = build_human_stack(self.root / f"notext-{fault}")
                try:
                    stack.fake.faults["sendMessage.echo"] = fault
                    review = stack.open_review()
                    self.assertEqual(stack.request(review).state, "pending")
                    self.assertEqual(stack.state.open_cards(), [])
                    record = stack.journal.get_human_request_for_review(review.review_handle)
                    with self.assertRaises(PresentRefused) as caught:
                        stack.core.present(record.review_handle, record.plan_digest,
                                           json.dumps(record.card_summary()), record.nonce,
                                           record.expires_at)
                    self.assertEqual(caught.exception.kind, "unavailable")
                    self.assertIn("event=present_failed", stack.log_stream.getvalue())
                    self.assertIn("detail=shape", stack.log_stream.getvalue())
                finally:
                    stack.close()

    def test_closed_card_conflict_is_logged(self) -> None:
        """Finding 8: a Present replay for a card closed by a decision is a
        conflict AND leaves a present_refused event."""

        review, card = self.presented_card()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "accepted")
        record = self.request_record(review)
        with self.assertRaises(PresentRefused) as caught:
            self.stack.core.present(record.review_handle, record.plan_digest,
                                    json.dumps(record.card_summary()), record.nonce, record.expires_at)
        self.assertEqual(caught.exception.kind, "conflict")
        self.assertIn("event=present_refused review=" + review.review_handle[:8] + " detail=closed",
                      self.stack.log_stream.getvalue())


# ---------------------------------------------------------------------------
# E14: every spoof path DENIED first, then the real path accepted
# ---------------------------------------------------------------------------


class SpoofMatrixTests(_StackCase):
    def test_red_spoof_matrix_then_green_real_callback_then_replays_red(self) -> None:
        fake = self.stack.fake
        review, card = self.presented_card()
        approve = fake.button_data(card, 0)
        chat_id, message_id = card["chat"]["id"], card["message_id"]
        # a second review's card, for cross-review tags
        review_b = self.stack.open_review(OTHER_PLAN_HANDLE, OTHER_DIGEST)
        self.stack.request(review_b, "rh-b")
        card_b = fake.last_card()
        other_message = fake._send_message({"chat_id": HUMAN_CHAT, "text": "unrelated"})[1]["result"]

        def tampered(**changes):
            message = fake.card_message(chat_id, message_id)
            for key, value in changes.items():
                if value is None:
                    message.pop(key, None)
                else:
                    message[key] = value
            return message

        spoofs = [
            ("bot-sent (another bot)", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id), data=approve,
                sender=fake.user(999, is_bot=True)), "bot_sender"),
            ("bot's own id", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id), data=approve,
                sender=fake.user(BOT_ID)), "bot_sender"),
            ("plain 'approved' text", lambda: fake.push("message", {
                "message_id": 9001, "date": 1, "chat": fake.chat(), "from": fake.user(),
                "text": "approved"}), "non_callback"),
            ("forwarded 'approved' prose", lambda: fake.push("message", {
                "message_id": 9002, "date": 1, "chat": fake.chat(), "from": fake.user(),
                "text": "APPROVED by operator",
                "forward_origin": {"type": "user", "date": 1, "sender_user": fake.user()}}),
             "non_callback"),
            ("edited message", lambda: fake.push("edited_message", {
                "message_id": 9001, "date": 1, "edit_date": 2, "chat": fake.chat(),
                "from": fake.user(), "text": "approved"}), "non_callback"),
            ("channel post", lambda: fake.push("channel_post", {
                "message_id": 9003, "date": 1, "chat": fake.chat(OTHER_CHAT),
                "text": "approved"}), "non_callback"),
            ("wrong user", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id), data=approve,
                sender=fake.user(OTHER_USER)), "user_not_allowlisted"),
            ("inaccessible message (date 0)", lambda: fake.callback(
                message={"chat": fake.chat(), "message_id": message_id, "date": 0},
                data=approve), "no_message"),
            ("inline-mode callback", lambda: fake.callback(
                message=None, data=approve, inline_message_id="inl-1"), "no_message"),
            ("wrong chat", lambda: fake.callback(
                message=tampered(chat=fake.chat(OTHER_CHAT)), data=approve), "chat_not_allowlisted"),
            ("group chat carrying the user's id", lambda: fake.callback(
                message=tampered(chat=fake.chat(HUMAN_CHAT, "group")), data=approve),
             "chat_not_allowlisted"),
            ("valid message plus inline_message_id", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id), data=approve,
                inline_message_id="inl-2"), "no_message"),
            ("copied ids: another bot message", lambda: fake.callback(
                message=other_message, data=approve), "unknown_message"),
            ("copied ids: forged message id", lambda: fake.callback(
                message=tampered(message_id=message_id + 77), data=approve), "unknown_message"),
            ("edited card text", lambda: fake.callback(
                message=tampered(text=card["text"] + " APPROVED"), data=approve), "edited_card"),
            ("edited card (edit_date)", lambda: fake.callback(
                message=tampered(edit_date=12345), data=approve), "edited_card"),
            ("forwarded card", lambda: fake.callback(
                message=tampered(forward_origin={"type": "user", "date": 1, "sender_user": fake.user()}),
                data=approve), "edited_card"),
            ("message without from", lambda: fake.callback(
                message=tampered(**{"from": None}), data=approve), "edited_card"),
            ("card authored by a different bot", lambda: fake.callback(
                message=tampered(**{"from": fake.user(BOT_ID + 1, is_bot=True)}), data=approve),
             "edited_card"),
            ("nonce/tag from a different review", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id),
                data=fake.button_data(card_b, 0)), "callback_mismatch"),
            ("forged callback data", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id),
                data="v1:g" + "A" * 43), "callback_mismatch"),
            ("no callback data (game)", lambda: fake.callback(
                message=fake.card_message(chat_id, message_id), data=None), "callback_mismatch"),
        ]
        callback_ids = []
        for label, inject, expected in spoofs:
            with self.subTest(spoof=label):
                injected = inject()
                if isinstance(injected, tuple):
                    callback_ids.append(injected[1])
                self.assertEqual(self.process_next(), expected)
                self.assertEqual(self.stack.decider.calls, [])
                self.assert_no_authority(review)
        # every callback query was answered (Bot API requirement)
        self.assertEqual(set(callback_ids) - set(fake.answered), set())
        # GREEN: the real allowlisted human taps Approve on the real card
        _, real_id = fake.callback(message=fake.card_message(chat_id, message_id), data=approve)
        self.assertEqual(self.process_next(), "accepted")
        live = self.stack.journal.get_review(review.review_handle)
        self.assertEqual((live.state, live.authority_source, live.approver_subject),
                         ("granted", "human", "operator"))
        grant = self.stack.journal.get_grant_for_review(review.review_handle)
        attestation = self.stack.journal.get_attestation_for_review(review.review_handle)
        self.assertEqual(grant.source_event, attestation.attestation_handle)
        self.assertEqual(live.source_event, attestation.attestation_handle)
        self.assertEqual((attestation.provider_user_id, attestation.provider_chat_id,
                          attestation.provider_message_id),
                         (str(HUMAN_USER), str(HUMAN_CHAT), str(message_id)))
        self.assertIn(real_id, fake.answered)
        # the card was closed on the provider (empty keyboard edit)
        self.assertNotIn("reply_markup", fake.card_message(chat_id, message_id))
        # RED again: replays of every kind
        fake.callback(message=card, data=approve, callback_id=real_id)
        self.assertEqual(self.process_next(), "callback_replay")
        fake.callback(message=card, data=approve)
        self.assertEqual(self.process_next(), "card_closed")
        self.assertEqual(len(self.stack.decider.calls), 1)
        record = self.request_record(review)
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.human_decision(
                INGRESS_UID, review.review_handle, review.plan_digest, "granted",
                "src-replay", record.nonce, "a-different-key")
        self.assertEqual(caught.exception.reason_code, "human_request_closed")
        # review B is untouched by anything above
        self.assert_no_authority(review_b)

    def test_deny_button_records_a_human_denial(self) -> None:
        review, card = self.presented_card()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 1))
        self.assertEqual(self.process_next(), "accepted")
        live = self.stack.journal.get_review(review.review_handle)
        self.assertEqual((live.state, live.decision, live.authority_source), ("denied", "denied", "human"))
        self.assertIsNone(self.stack.journal.get_grant_for_review(review.review_handle))

    def test_stale_card_is_refused_at_both_layers(self) -> None:
        review, card = self.presented_card()
        self.stack.clock.advance(minutes=16)  # card ttl is 15 min
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "card_expired")
        record = self.request_record(review)
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.stack.service.human_decision(
                INGRESS_UID, review.review_handle, review.plan_digest, "granted",
                "src", record.nonce, "k")
        self.assertEqual(caught.exception.reason_code, "human_request_expired")
        self.assert_no_authority(review)
        self.assertEqual(self.stack.core.expire_cards(), 1)

    def test_callback_after_withdraw_is_refused_by_the_service(self) -> None:
        review, card = self.presented_card()
        record = self.request_record(review)
        with self.assertRaises(ApproverDenied):
            self.stack.service.withdraw_human_request("specialist", record.request_handle)
        withdrawn = self.stack.service.withdraw_human_request("root-admin", record.request_handle)
        self.assertEqual(withdrawn.state, "withdrawn")
        # the ingress card is still open (withdraw is service-side), so the
        # callback passes the provider checks — and the service refuses
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "accepted")
        self.assert_no_authority(review)
        self.assertEqual(self.stack.state.intents()[0].outcome, "refused")
        with self.assertRaises(HumanDecisionRefused):
            self.stack.service.withdraw_human_request("root-admin", record.request_handle)

    def test_callback_after_grant_revoke_mints_nothing(self) -> None:
        review, card = self.presented_card()
        approve = self.stack.fake.button_data(card, 0)
        self.stack.fake.callback(message=card, data=approve)
        self.assertEqual(self.process_next(), "accepted")
        grant = self.stack.journal.get_grant_for_review(review.review_handle)
        self.stack.service.revoke("root-admin", grant.approval_handle, "revoked_by_admin")
        self.stack.fake.callback(message=card, data=approve)
        self.assertEqual(self.process_next(), "card_closed")
        record = self.request_record(review)
        with self.assertRaises(HumanDecisionRefused):
            self.stack.service.human_decision(
                INGRESS_UID, review.review_handle, review.plan_digest, "granted",
                "src-2", record.nonce, "k-after-revoke")
        self.assertEqual(self.stack.journal.get_grant(grant.approval_handle).state, "revoked")
        self.assertEqual(self.stack.journal.counts().issued_grants, 0)


# ---------------------------------------------------------------------------
# HumanDecision: uid gate, single use, expiry, refusal after transitions
# ---------------------------------------------------------------------------


class HumanDecisionServiceTests(_StackCase):
    def setUp(self) -> None:
        super().setUp()
        self.review, _ = self.presented_card()
        self.record = self.request_record(self.review)

    def decide(self, uid=INGRESS_UID, *, nonce=None, key="k1", decision="granted",
               review=None, digest=None, source="src-1"):
        review = review or self.review
        return self.stack.service.human_decision(
            uid, review.review_handle, digest or review.plan_digest, decision,
            source, nonce or self.record.nonce, key)

    def test_uid_gate_denies_every_other_uid_even_with_correct_fields(self) -> None:
        for uid in (INGRESS_UID + 1, CONSUMER_UID, 0, 64010, -1, True):
            with self.subTest(uid=uid):
                with self.assertRaises(PolicyDenied) as caught:
                    self.decide(uid)
                self.assertEqual(caught.exception.reason_code, "subject_denied")
        self.assert_no_authority_left()
        self.assertEqual(self.decide().state, "granted")

    def assert_no_authority_left(self) -> None:
        self.assertEqual(self.stack.journal.get_review(self.review.review_handle).state, "open")

    def test_single_use_and_idempotent_replay(self) -> None:
        first = self.decide()
        replay = self.decide()
        self.assertTrue(replay.replayed)
        self.assertEqual((replay.state, replay.attestation_handle), (first.state, first.attestation_handle))
        with self.assertRaises(IdempotencyConflict):
            self.decide(decision="denied")
        with self.assertRaises(IdempotencyConflict):
            self.decide(source="src-other")
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.decide(key="k2")
        self.assertEqual(caught.exception.reason_code, "human_request_closed")
        counts = self.stack.journal.counts()
        self.assertEqual(counts.issued_grants, 1)

    def test_nonce_from_a_different_review_is_refused_and_does_not_burn(self) -> None:
        review_b = self.stack.open_review(OTHER_PLAN_HANDLE, OTHER_DIGEST)
        self.stack.request(review_b, "rh-b")
        record_b = self.request_record(review_b)
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.decide(nonce=record_b.nonce)
        self.assertEqual(caught.exception.reason_code, "human_nonce_mismatch")
        self.assertEqual(self.request_record(self.review).state, "presented")
        self.assertEqual(self.decide().state, "granted")
        self.assertEqual(self.request_record(review_b).state, "presented")

    def test_digest_and_argument_validation(self) -> None:
        with self.assertRaises(HumanDecisionRefused) as caught:
            self.decide(digest="0" * 64)
        self.assertEqual(caught.exception.reason_code, "digest_mismatch")
        for bad in ({"decision": "approved"}, {"nonce": "x" * 64}, {"source": "s" * 300}, {"key": ""}):
            with self.subTest(bad=bad):
                with self.assertRaises(HostControlError):
                    self.decide(**bad)
        self.assert_no_authority_left()

    def test_expired_review_refuses(self) -> None:
        self.stack.clock.advance(hours=2)
        with self.assertRaises(HumanDecisionRefused):
            self.decide()
        self.assertEqual(self.request_record(self.review).state, "expired")

    def test_refused_after_any_review_transition(self) -> None:
        # a family both an agent and the human may decide: the agent
        # decides first, then the human callback arrives
        policy = human_policy_raw(human_families=("account.create", "persona.install"))
        stack = build_human_stack(self.root / "t", policy_raw=policy)
        try:
            stack.metadata.plans["6" * 64] = human_plan("7" * 64, family="persona.install")
            review = stack.service.review("specialist", "6" * 64, "7" * 64, None).record
            stack.request(review)
            record = stack.journal.get_human_request_for_review(review.review_handle)
            stack.service.decide("specialist", review.review_handle, review.plan_digest,
                                 "denied", "rejected", "anchor", "agent-k")
            with self.assertRaises(HumanDecisionRefused) as caught:
                stack.service.human_decision(INGRESS_UID, review.review_handle, review.plan_digest,
                                             "granted", "src", record.nonce, "k")
            self.assertEqual(caught.exception.reason_code, "review_not_open")
        finally:
            stack.close()

    def test_review_get_extras_expose_the_approval_handle_never_the_nonce(self) -> None:
        before = json.loads(approval_wire.review_summary_json(
            self.stack.journal.get_review(self.review.review_handle),
            self.stack.service.review_extras(self.review.review_handle)))
        self.assertIsNone(before["approval_handle"])
        self.assertEqual(before["human_request"]["state"], "presented")
        self.decide()
        record = self.stack.service.review_get("specialist", self.review.review_handle)
        extras = self.stack.service.review_extras(self.review.review_handle)
        body = approval_wire.review_summary_json(record, extras)
        payload = json.loads(body)
        grant = self.stack.journal.get_grant_for_review(self.review.review_handle)
        self.assertEqual(payload["approval_handle"], grant.approval_handle)
        self.assertEqual(payload["human_request"]["state"], "decided")
        self.assertEqual(payload["authority_source"], "human")
        self.assertNotIn(self.record.nonce, body)
        self.assertNotIn(grant.nonce, body)
        self.assertNotIn("source_event", payload)

    def test_human_grant_binds_like_any_grant_once(self) -> None:
        self.decide()
        grant = self.stack.journal.get_grant_for_review(self.review.review_handle)
        binder = self.stack.service.as_binder(CONSUMER_UID)
        op = "9" * 64
        bound = binder.validate_and_bind(grant.approval_handle, PLAN_HANDLE, CANARY_DIGEST, op)
        self.assertEqual(bound.state, "bound")
        again = binder.validate_and_bind(grant.approval_handle, PLAN_HANDLE, CANARY_DIGEST, "8" * 64)
        self.assertEqual((again.state, again.reason_code), ("denied", "already_bound"))
        # the grant TTL is the tighter of the service TTL and the human
        # entry's policy ceiling (1800 s)
        from datetime import datetime as _dt
        ttl = _dt.fromisoformat(grant.expires_at) - _dt.fromisoformat(grant.issued_at)
        self.assertEqual(ttl, timedelta(seconds=1800))


# ---------------------------------------------------------------------------
# crash safety (ORG-052 §6)
# ---------------------------------------------------------------------------


class CrashRecoveryTests(_StackCase):
    def _reopen_service(self) -> ApprovalService:
        self.stack.journal.close()
        journal = ApprovalJournal.open(self.stack.root / "approval.sqlite3")
        self.stack.journal = journal
        self.stack.service = ApprovalService(
            journal=journal,
            metadata_source=self.stack.metadata,
            clock=self.stack.clock,
            authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
            handle_factory=self.stack.handles,
            registered_consumer_uids=frozenset((CONSUMER_UID,)),
            human_ingress=registration(),
        )
        return self.stack.service

    def test_kill_between_nonce_consume_and_commit_then_recover(self) -> None:
        review, _card = self.presented_card()
        record = self.request_record(review)
        self.stack.journal.close()
        spec = self.root / "crash-spec.json"
        spec.write_text(json.dumps({
            "journal": str(self.stack.root / "approval.sqlite3"),
            "now": self.stack.clock.now.isoformat(),
            "review_handle": review.review_handle,
            "digest": review.plan_digest,
            "source_event": "src-crash",
            "nonce": record.nonce,
            "key": "k-crash",
        }), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "tools.test_host_control_telegram_ingress_fixtures",
             "crash-child", str(spec)],
            cwd=REPO_ROOT, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 137, proc.stderr.decode()[-500:])
        service = self._reopen_service()
        # nothing committed: nonce unconsumed, no attestation, review open
        self.assertEqual(self.request_record(review).state, "presented")
        self.assertIsNone(self.stack.journal.get_attestation_for_review(review.review_handle))
        self.assertEqual(self.stack.journal.get_review(review.review_handle).state, "open")
        # recovery: the same intent decides exactly once
        first = service.human_decision(INGRESS_UID, review.review_handle, review.plan_digest,
                                       "granted", "src-crash", record.nonce, "k-crash")
        replay = service.human_decision(INGRESS_UID, review.review_handle, review.plan_digest,
                                        "granted", "src-crash", record.nonce, "k-crash")
        self.assertEqual(first.attestation_handle, replay.attestation_handle)
        with self.assertRaises(HumanDecisionRefused):
            service.human_decision(INGRESS_UID, review.review_handle, review.plan_digest,
                                   "granted", "src-x", record.nonce, "k-other")
        self.assertEqual(self.stack.journal.counts().issued_grants, 1)

    def test_reply_lost_after_commit_ingress_recovers_with_the_same_key(self) -> None:
        review, card = self.presented_card()
        real = self.stack.decider

        class CommitThenLoseReply:
            calls: list = []

            def human_decision(self, *args):
                self.calls.append(args)
                real.human_decision(*args)  # commits
                raise DecisionUnavailable()  # ...but the reply is lost

        self.stack.core.decider = CommitThenLoseReply()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "accepted")
        self.assertEqual([i.state for i in self.stack.state.intents()], ["pending"])
        self.assertEqual(self.stack.journal.get_review(review.review_handle).state, "granted")
        self.stack.core.decider = real
        # T-5518: the lost reply was a retryable failure, so the retry
        # waits out its first backoff step (not due yet, then due)
        self.assertEqual(self.stack.core.recover(), 0)
        self.stack.clock.advance(seconds=core_mod.DECISION_RETRY_INITIAL_SECONDS)
        self.assertEqual(self.stack.core.recover(), 1)
        intent = self.stack.state.intents()[0]
        self.assertEqual((intent.state, intent.outcome), ("done", "granted"))
        self.assertEqual(self.stack.journal.counts().issued_grants, 1)
        # the recovery call reused the ORIGINAL idempotency key
        self.assertEqual(real.calls[-1][5], intent.idempotency_key)

    def test_ingress_dies_after_intent_before_call_then_restarts(self) -> None:
        review, card = self.presented_card()

        class DiesBeforeCall:
            def human_decision(self, *args):
                raise DecisionUnavailable()

        self.stack.core.decider = DiesBeforeCall()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "accepted")
        self.assert_no_authority(review)
        # "restart": a fresh core over the same private state file
        self.stack.state.close()
        state = IngressState.open(self.stack.state.path.parent)
        self.stack.state = state
        core = IngressCore(
            state=state, api=self.stack.core.api, decider=self.stack.decider,
            bot_user_id=BOT_ID, allow_user_id=HUMAN_USER, allow_chat_id=HUMAN_CHAT,
            card_ttl_max_seconds=3600, log=IngressLog(io.StringIO()), clock=self.stack.clock)
        self.assertEqual(core.recover(), 1)
        self.assertEqual(self.stack.journal.get_review(review.review_handle).state, "granted")
        self.assertEqual(core.recover(), 0)
        # the same callback re-delivered after restart is a replay
        self.stack.core = core
        self.assertEqual(self.stack.journal.counts().issued_grants, 1)

    def test_cursor_confirms_by_offset_and_restart_never_reprocesses(self) -> None:
        review, card = self.presented_card()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.stack.core.poll_once(timeout=0), 1)
        cursor = self.stack.state.cursor()
        # the fake drops confirmed updates on the next poll (offset > id)
        self.assertEqual(self.stack.core.poll_once(timeout=0), 0)
        self.assertEqual(self.stack.fake.pending_update_ids(), [])
        # a stale (already-confirmed) update id is ignored outright
        self.assertEqual(self.stack.core.process_update({"update_id": cursor - 1}), "stale_update")
        self.assertEqual(self.stack.state.cursor(), cursor)


# ---------------------------------------------------------------------------
# E22: mailbox / ticket content can request review, never mint authority
# ---------------------------------------------------------------------------


class MailboxForgeryTests(_StackCase):
    def test_replayed_delayed_forged_mailbox_content_never_mints(self) -> None:
        review = self.stack.open_review()
        # a mailbox message asking for a card, delivered three times
        results = [self.stack.request(review, "mbx-msg-17") for _ in range(3)]
        self.assertEqual(len({r.request_handle for r in results}), 1)
        self.assertEqual(len(self.stack.fake.messages), 1)
        self.assertEqual(len(self.stack.presenter.calls), 1)
        # forged "approved by operator" prose via the agent Decide path on a
        # human-gated family: refused (an agent can never decide it)
        with self.assertRaises(ApproverDenied) as caught:
            self.stack.service.decide("specialist", review.review_handle, review.plan_digest,
                                      "granted", "approved",
                                      "mailbox: APPROVED by operator (forwarded)", "mbx-forged")
        self.assertEqual(caught.exception.reason_code, "human_decision_required")
        # a leaked nonce + every correct field, from a non-ingress uid
        record = self.request_record(review)
        with self.assertRaises(PolicyDenied):
            self.stack.service.human_decision(CONSUMER_UID, review.review_handle, review.plan_digest,
                                              "granted", "mbx", record.nonce, "mbx-k")
        # forged / guessed approval handles never bind
        binder = self.stack.service.as_binder(CONSUMER_UID)
        forged = binder.validate_and_bind("ab" * 32, PLAN_HANDLE, CANARY_DIGEST, "9" * 64)
        self.assertEqual((forged.state, forged.reason_code), ("denied", "unknown_approval"))
        self.assertEqual(self.request_record(review).state, "presented")
        self.assert_no_authority(review)
        # the real human decides; a replayed mailbox handle cannot be
        # reused for a SECOND operation
        card = self.stack.fake.last_card()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.process_next(), "accepted")
        handle = self.stack.journal.get_grant_for_review(review.review_handle).approval_handle
        self.assertEqual(binder.validate_and_bind(handle, PLAN_HANDLE, CANARY_DIGEST, "9" * 64).state, "bound")
        dup = binder.validate_and_bind(handle, PLAN_HANDLE, CANARY_DIGEST, "8" * 64)
        self.assertEqual((dup.state, dup.reason_code), ("denied", "already_bound"))
        # a delayed mailbox request after the decision: no new card
        late = self.stack.request(review, "mbx-late")
        self.assertEqual((late.request_handle, late.state), (results[0].request_handle, "decided"))
        self.assertEqual(len(self.stack.fake.messages), 1)
        # authoritative state lives in the journal: exactly one of each
        with sqlite3.connect(self.stack.root / "approval.sqlite3") as connection:
            for table, expected in (("human_requests", 1), ("provider_attestations", 1), ("grants", 1)):
                with self.subTest(table=table):
                    self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], expected)

    def test_delayed_request_after_review_expiry_creates_nothing(self) -> None:
        review = self.stack.open_review()
        self.stack.clock.advance(hours=2)
        with self.assertRaises(HumanDecisionRefused):
            self.stack.request(review, "mbx-delayed")
        self.assertEqual(self.stack.fake.messages, {})


# ---------------------------------------------------------------------------
# E21: leak canaries over every in-process sink
# ---------------------------------------------------------------------------


def scan(blob: str, forbidden: dict[str, str]) -> list[str]:
    return [label for label, value in forbidden.items() if value and value in blob]


class LeakCanaryTests(_StackCase):
    def test_no_token_nonce_digest_or_effects_in_logs_requests_or_results(self) -> None:
        review, card = self.presented_card()
        record = self.request_record(review)
        request_result = self.stack.request(review)
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0),
                                 sender=self.stack.fake.user(OTHER_USER))
        self.process_next()
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.process_next()
        # forced Bot API failures on every method
        for method in ("getUpdates", "answerCallbackQuery"):
            self.stack.fake.faults[method] = "500"
        with self.assertRaises(BotApiError):
            self.stack.core.poll_once(timeout=0)
        self.stack.fake.faults.clear()
        attestation = self.stack.journal.get_attestation_for_review(review.review_handle)
        grant = self.stack.journal.get_grant_for_review(review.review_handle)
        forbidden_everywhere = {
            "token": CANARY_TOKEN_TEXT,
            "nonce": record.nonce,
            "grant nonce": grant.nonce,
            "callback data": self.stack.fake.button_data(card, 0),
            "tag": self.stack.fake.button_data(card, 0)[4:],
            "attestation": attestation.attestation_handle,
        }
        log = self.stack.log_stream.getvalue()
        self.assertIn("event=decision_recorded", log)
        self.assertEqual(scan(log, {**forbidden_everywhere, "digest": CANARY_DIGEST,
                                    "effects": EFFECT_CANARY,
                                    "chat id": str(HUMAN_CHAT)}), [])
        self.assertFalse(leaks_token(log))
        # downstream requests to the provider: token only in the URL path,
        # never a body; the nonce/attestation nowhere; digest+effects only
        # inside the card text itself
        for request in self.stack.fake.requests:
            with self.subTest(method=request.method):
                body = request.body.decode()
                self.assertNotIn(CANARY_TOKEN_TEXT, body)
                self.assertEqual(scan(body + request.path, {
                    "nonce": record.nonce, "attestation": attestation.attestation_handle,
                    "grant nonce": grant.nonce}), [])
                if request.method != "sendMessage":
                    self.assertNotIn(EFFECT_CANARY, body)
        # public results the agent / ingress see
        public = json.dumps([
            dataclasses.asdict(request_result),
            json.loads(approval_wire.review_summary_json(
                self.stack.journal.get_review(review.review_handle),
                self.stack.service.review_extras(review.review_handle))),
        ])
        self.assertEqual(scan(public, {"nonce": record.nonce, "grant nonce": grant.nonce,
                                       "token": CANARY_TOKEN_TEXT,
                                       "attestation": attestation.attestation_handle}), [])
        # the ingress state file is private
        self.assertEqual(self.stack.state.path.stat().st_mode & 0o777, 0o600)

    def test_adapter_tool_output_never_carries_nonce_token_or_attestation(self) -> None:
        """The approver adapter's pure reply shaping (the exact
        structuredContent + text an agent sees) over the REAL service
        replies for request_human / review.get, and the error mapping of a
        human refusal. The live-adapter variant is in the dbus tier (it
        needs the pinned mcp 2.x runtime)."""

        from tools.approval_mcp_adapter import contract

        tools_by_name = {tool.name: tool for tool in contract.TOOLS}
        review, card = self.presented_card()
        record = self.request_record(review)
        result = self.stack.request(review)
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.process_next()
        attestation = self.stack.journal.get_attestation_for_review(review.review_handle)
        grant = self.stack.journal.get_grant_for_review(review.review_handle)
        human = contract.shape_result(tools_by_name["approval.request_human"],
                                      (result.request_handle, result.state))
        got = contract.shape_result(tools_by_name["approval.review.get"], (
            "granted",
            approval_wire.review_summary_json(
                self.stack.service.review_get("specialist", review.review_handle),
                self.stack.service.review_extras(review.review_handle))))
        refusal = contract.error_for_dbus_name(
            approval_wire.error_name_for(HumanDecisionRefused(reason_code="human_nonce_mismatch")),
            (approval_wire.error_body_for(HumanDecisionRefused(reason_code="human_nonce_mismatch")),),
        ).payload()
        outputs = contract.canonical_text([human, got, refusal])
        self.assertEqual(got["summary"]["approval_handle"], grant.approval_handle)
        self.assertEqual(scan(outputs, {"nonce": record.nonce, "grant nonce": grant.nonce,
                                        "token": CANARY_TOKEN_TEXT,
                                        "attestation": attestation.attestation_handle,
                                        "callback data": self.stack.fake.button_data(card, 0)}), [])

    def test_staged_bus_policy_grants_present_and_human_decision_to_one_account_each(self) -> None:
        from xml.etree import ElementTree

        def allows(path: Path) -> dict[str, list[dict]]:
            tree = ElementTree.parse(path).getroot()
            out: dict[str, list[dict]] = {}
            for policy in tree.findall("policy"):
                key = policy.get("user") or f"context={policy.get('context')}"
                out[key] = [dict(rule.attrib) for rule in policy.findall("allow")]
            return out

        telegram = allows(DIST / "org.pantheon.HostApprovalTelegram1.conf")
        present = [(user, rule) for user, rules in telegram.items() for rule in rules
                   if rule.get("send_member") == "Present"]
        self.assertEqual(present, [("pantheon-hostapproval", {
            "send_destination": ingress_wire.INGRESS_BUS_NAME,
            "send_interface": ingress_wire.INGRESS_INTERFACE,
            "send_member": "Present"})])
        owners = [user for user, rules in telegram.items() for rule in rules if "own" in rule]
        self.assertEqual(owners, ["pantheon-hostapproval-ingress"])
        self.assertEqual(sum(len(rules) for rules in telegram.values()), 2)
        approval = allows(DIST / "org.pantheon.HostApproval1.conf")
        human = [(user, rule["send_member"]) for user, rules in approval.items() for rule in rules
                 if rule.get("send_interface") == approval_wire.HUMAN_INGRESS_INTERFACE]
        self.assertEqual(human, [("pantheon-hostapproval-ingress", "HumanDecision")])
        unit = (DIST / "pantheon-host-approval-telegram-ingress.service").read_text(encoding="utf-8")
        self.assertIn("User=pantheon-hostapproval-ingress", unit)
        self.assertIn("BusName=org.pantheon.HostApprovalTelegram1", unit)

    def test_staged_artifacts_carry_no_secret_material(self) -> None:
        staged = [
            DIST / "pantheon-host-approval-telegram-ingress.service",
            DIST / "org.pantheon.HostApprovalTelegram1.conf",
            DIST / "pantheon-host-approval-telegram-ingress.json.template",
            DIST / "org.pantheon.HostApproval1.conf",
        ]
        for path in staged:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertFalse(leaks_token(text))
                self.assertEqual(scan(text, {"token": CANARY_TOKEN_TEXT, "mark": NONCE_MARK,
                                             "digest": CANARY_DIGEST}), [])
                self.assertNotRegex(text, r"[0-9a-f]{64}")
        unit = staged[0].read_text(encoding="utf-8")
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertEqual(exec_line.split()[1:], ["--config", "/etc/pantheon/host-approval-telegram/ingress.json"])
        self.assertNotIn("Environment", unit)
        self.assertNotIn("LoadCredential", unit)

    def test_every_bot_api_failure_path_never_discloses_the_token(self) -> None:
        """Gate 21 over EVERY failure path (review finding 2): transport,
        http_status, auth, not_ok, malformed. The exception, its repr, the
        client repr, last_error_detail and the ingress log line are all
        scanned for ANY 12-char fragment of the token's secret part."""

        for code, stream in failure_path_logs(mutated=False).items():
            with self.subTest(path=code):
                self.assertFalse(leaks_token(stream), stream)


# ---------------------------------------------------------------------------
# Bot API client + card contract
# ---------------------------------------------------------------------------


def failure_path_logs(*, mutated: bool) -> dict[str, str]:
    """Drive every Bot API failure path; return, per path, everything that
    path exposes (exception str/repr, client repr, last_error_detail and
    the ingress log line the core would write)."""

    exposed: dict[str, str] = {}
    live = FakeBotApi()
    dead = FakeBotApi()
    dead_base = dead.base_url
    dead.close()
    patch = (mock.patch.object(bot_api_mod, "_error_detail", lambda method, url, exc: url)
             if mutated else mock.patch.object(bot_api_mod, "_error_detail", bot_api_mod._error_detail))
    try:
        cases = {
            "transport": (dead_base, None),
            "http_status": (live.base_url, "500"),
            "auth": (live.base_url, "401"),
            "not_ok": (live.base_url, "not_ok"),
            "malformed": (live.base_url, "malformed"),
        }
        with patch:
            for code, (base, fault) in cases.items():
                live.faults.clear()
                if fault:
                    live.faults["getUpdates"] = fault
                client = BotApiClient(CANARY_TOKEN, base_url=base)
                stream = io.StringIO()
                try:
                    client.get_updates(offset=0, timeout=0)
                except BotApiError as exc:
                    assert exc.code == code, (code, exc.code)
                    assert exc.__cause__ is None and exc.__suppress_context__
                    IngressLog(stream).event("api_error", detail=client.last_error_detail or exc.code)
                    exposed[code] = "\n".join(
                        (str(exc), repr(exc), repr(client), client.last_error_detail or "",
                         stream.getvalue()))
                else:  # pragma: no cover
                    raise AssertionError(f"{code} path did not fail")
    finally:
        live.close()
    return exposed


class BotApiAndCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBotApi()
        self.client = BotApiClient(CANARY_TOKEN, base_url=self.fake.base_url)

    def tearDown(self) -> None:
        self.fake.close()

    def test_fail_closed_on_every_error_shape(self) -> None:
        for fault, code in (("500", "http_status"), ("not_ok", "not_ok"), ("no_ok_field", "not_ok"),
                            ("malformed", "malformed"), ("401", "auth")):
            with self.subTest(fault=fault):
                self.fake.faults["getMe"] = fault
                with self.assertRaises(BotApiError) as caught:
                    self.client.get_me()
                self.assertEqual(caught.exception.code, code)
        wrong = BotApiClient(b"1:wrong", base_url=self.fake.base_url)
        with self.assertRaises(BotApiError) as caught:
            wrong.get_me()
        self.assertEqual(caught.exception.code, "auth")

    def test_base_url_is_the_real_api_or_loopback_only(self) -> None:
        for base in ("https://evil.example", "http://127.0.0.1.evil:1", "http://10.0.0.1:80"):
            with self.subTest(base=base):
                with self.assertRaises(BotApiError):
                    BotApiClient(CANARY_TOKEN, base_url=base)
        self.assertEqual(bot_api_mod.TELEGRAM_API_BASE, "https://api.telegram.org")

    def test_environment_proxies_are_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                                          "ALL_PROXY": "http://127.0.0.1:9"}):
            client = BotApiClient(CANARY_TOKEN, base_url=self.fake.base_url)
            self.assertEqual(client.get_me()["id"], BOT_ID)

    def test_get_me_startup_fails_closed(self) -> None:
        self.assertEqual(learn_bot_user_id(self.client), BOT_ID)
        self.fake.faults["getMe"] = "500"
        with self.assertRaises(StartupRefused) as caught:
            learn_bot_user_id(self.client)
        self.assertEqual(caught.exception.code, "get_me_failed")
        del self.fake.faults["getMe"]
        self.fake.get_me_result = {"id": BOT_ID, "is_bot": False, "first_name": "x"}
        with self.assertRaises(StartupRefused) as caught:
            learn_bot_user_id(self.client)
        self.assertEqual(caught.exception.code, "get_me_not_a_bot")

    def test_card_bounds_and_callback_data_limit(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        expires = (now + timedelta(minutes=10)).isoformat()
        effects = ("line\x1b[31m\n" * 300)[:4000]
        summary = json.dumps({
            "schema": ingress_wire.CARD_SCHEMA, "review_handle": "a" * 64, "plan_digest": "b" * 64,
            "target_seat_id": "recovery", "operation_families": ["account.create"],
            "redacted_effects": effects, "expires_at": expires})
        fields = card_mod.parse_present("a" * 64, "b" * 64, summary, "c" * 64, expires,
                                        now=now, max_ttl_seconds=3600)
        text, markup = card_mod.render_card(fields)
        self.assertLessEqual(len(text), card_mod.MAX_CARD_TEXT)
        self.assertIn("[truncated]", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("c" * 64, text)
        for button in markup["inline_keyboard"][0]:
            self.assertEqual(len(button["callback_data"].encode()), 47)
        sent = self.client.send_message(chat_id=HUMAN_CHAT, text=text, reply_markup=markup)
        self.assertEqual(sent["text"], text)
        body = json.loads(self.fake.requests[-1].body)
        self.assertNotIn("parse_mode", body)

    def test_ingress_wire_copies_equal_the_approval_side(self) -> None:
        self.assertEqual(ingress_wire.INGRESS_BUS_NAME, approval_wire.TELEGRAM_INGRESS_BUS_NAME)
        self.assertEqual(ingress_wire.INGRESS_OBJECT_PATH, approval_wire.TELEGRAM_INGRESS_OBJECT_PATH)
        self.assertEqual(ingress_wire.INGRESS_INTERFACE, approval_wire.TELEGRAM_INGRESS_INTERFACE)
        self.assertEqual(ingress_wire.INGRESS_ERROR_NAMESPACE, approval_wire.TELEGRAM_INGRESS_ERROR_NAMESPACE)
        self.assertEqual(ingress_wire.APPROVAL_BUS_NAME, approval_wire.BUS_NAME)
        self.assertEqual(ingress_wire.APPROVAL_OBJECT_PATH, approval_wire.OBJECT_PATH)
        self.assertEqual(ingress_wire.HUMAN_INGRESS_INTERFACE, approval_wire.HUMAN_INGRESS_INTERFACE)
        self.assertEqual(ingress_wire.APPROVAL_ERROR_NAMESPACE, approval_wire.ERROR_NAMESPACE)
        self.assertEqual(ingress_wire.CARD_SCHEMA, approval_human.HUMAN_CARD_SCHEMA)
        self.assertEqual(ingress_wire.CARD_KEYS, approval_human.HUMAN_CARD_KEYS)
        self.assertEqual(ingress_wire.MAX_CARD_SUMMARY_LENGTH, approval_human.MAX_CARD_SUMMARY_LENGTH)
        # the Present summary bound equals the approval wire JSON limit
        self.assertEqual(ingress_wire.MAX_SUMMARY_JSON_BYTES, approval_wire._JSON_LIMIT)
        self.assertEqual(ingress_wire.MAX_SUMMARY_JSON_BYTES, 65536)


# ---------------------------------------------------------------------------
# credential isolation (gate 14) + lease + config
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_PREFIXES = (
    "tools.codex_telegram_bridge",
    "tools.codex_telegram_actions",
    "tools.harness_telegram",
    "tools.bringup_acceptance",
    "tools.hooks",
    "tools.mint_telegram_sticker_set",
    "codex_telegram",
    "harness.codex",
    "tools.mailbox",
    "mailbox",
    "tools.persona_installer",
    "tools.host_control.contracts",
    "tools.host_control.approval",
    "tools.host_control.journal",
    "tools.host_control.operations",
)


def _write_config(root: Path, *, token: bytes = CANARY_TOKEN, mode: int = 0o600) -> Path:
    cred_dir = root / "cred"
    cred_dir.mkdir(mode=0o700)
    cred_dir.chmod(0o700)
    token_path = cred_dir / "bot-token"
    token_path.write_bytes(token + b"\n")
    token_path.chmod(mode)
    state_dir = root / "state"
    state_dir.mkdir(mode=0o700)
    raw = {
        "schema": "pantheon-telegram-ingress-config/v1",
        "ingress_uid": os.geteuid(),
        "approval_uid": os.geteuid() + 1,
        "credential": {"credential_id": "telegram-approval-bot", "location": str(token_path),
                       "owner_gid": os.getegid()},
        "allowlist": {"user_id": HUMAN_USER, "chat_id": HUMAN_CHAT},
        "state_dir": str(state_dir),
        "card_ttl_max_seconds": 3600,
        "poll_timeout_seconds": 0,
    }
    config_path = root / "ingress.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config_path.chmod(0o644)
    return config_path


class IsolationAndLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.root.chmod(0o700)
        self.fake = FakeBotApi()

    def tearDown(self) -> None:
        self.fake.close()
        self._tmp.cleanup()

    def test_ingress_import_closure_excludes_every_agent_telegram_and_mailbox_module(self) -> None:
        probe = (
            "import sys, json\n"
            "import tools.host_control.telegram_ingress.main\n"
            "import tools.host_control.telegram_ingress.dbus_service\n"
            "print(json.dumps(sorted(sys.modules)))\n"
        )
        proc = subprocess.run([sys.executable, "-I", "-c", probe], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=120,
                              env={"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ.get("PATH", "")})
        # -I ignores PYTHONPATH; run from the repo root with sys.path[0]=''
        if proc.returncode != 0:
            proc = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        modules = json.loads(proc.stdout)
        leaked = [m for m in modules for p in FORBIDDEN_IMPORT_PREFIXES if m == p or m.startswith(p + ".")]
        self.assertEqual(leaked, [])
        self.assertIn("tools.credential_broker", modules)

    def test_source_imports_are_declared_closed(self) -> None:
        allowed = ("tools.credential_broker", "tools.host_control.telegram_ingress")
        for path in sorted(INGRESS_PKG.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for name in names:
                    if name.startswith("tools"):
                        with self.subTest(module=path.name, imports=name):
                            self.assertTrue(name.startswith(allowed), name)

    def test_no_existing_telegram_module_imports_the_ingress(self) -> None:
        candidates = [p for p in (REPO_ROOT / "tools").glob("*telegram*.py") if "telegram_ingress" not in p.name]
        candidates += list((REPO_ROOT / "harness" / "codex" / "codex_telegram").glob("*.py"))
        self.assertTrue(candidates)
        for path in candidates:
            with self.subTest(path=path.name):
                self.assertNotIn("telegram_ingress", path.read_text(encoding="utf-8"))

    def test_staged_paths_share_nothing_with_agent_telegram_integrations(self) -> None:
        template = json.loads((DIST / "pantheon-host-approval-telegram-ingress.json.template").read_text())
        ours = [template["credential"]["location"], template["state_dir"],
                "/etc/pantheon/host-approval-telegram"]
        existing = []
        for pattern in ("tools/systemd/*telegram*", "harness/codex/templates/*telegram*",
                        "hosts/*/systemd/*telegram*"):
            existing += [p.read_text(encoding="utf-8") for p in REPO_ROOT.glob(pattern)]
        existing += [p.read_text(encoding="utf-8") for p in (REPO_ROOT / "tools").glob("*telegram*.py") if "telegram_ingress" not in p.name]
        self.assertTrue(existing)
        blob = "\n".join(existing)
        for path in ours:
            with self.subTest(path=path):
                self.assertNotIn(path, blob)
                self.assertNotIn("host-approval-telegram", blob)

    def test_lease_reads_the_token_from_the_host_file_never_the_environment(self) -> None:
        config_path = _write_config(self.root)
        config = load_ingress_config(config_path, expected_owner_uid=os.geteuid())
        log = io.StringIO()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "999:ENVTOKENshouldneverbeusedXXXXXXXXXXXX"}):
            core = build_core(config, decider=RecordingDecider(), log=IngressLog(log),
                              api_base=self.fake.base_url)
        try:
            self.assertEqual(core.bot_user_id, BOT_ID)
            # the fake only answers the canary token; a request path using
            # the env token would have been refused as auth
            self.assertTrue(all(r.path.startswith(f"/bot{CANARY_TOKEN_TEXT}/") for r in self.fake.requests))
            audit = (config.state_dir / "credential-audit.jsonl").read_text()
            self.assertIn('"event":"issue"', audit)
            self.assertIn('"event":"revoke"', audit)
            self.assertNotIn(CANARY_TOKEN_TEXT, audit + log.getvalue())
        finally:
            core.state.close()

    def test_lease_refuses_a_readable_token_file_and_the_wrong_uid(self) -> None:
        config_path = _write_config(self.root, mode=0o640)
        config = load_ingress_config(config_path, expected_owner_uid=os.geteuid())
        with self.assertRaises(StartupRefused) as caught:
            build_core(config, decider=RecordingDecider(), log=IngressLog(io.StringIO()),
                       api_base=self.fake.base_url)
        self.assertEqual(caught.exception.code, "credential_unavailable")
        with self.assertRaises(StartupRefused) as caught:
            build_core(config, decider=RecordingDecider(), log=IngressLog(io.StringIO()),
                       api_base=self.fake.base_url, caller_uid=lambda: os.geteuid() + 7)
        self.assertEqual(caught.exception.code, "wrong_uid")
        self.assertEqual(self.fake.requests, [])

    def test_config_parser_is_strict(self) -> None:
        config_path = _write_config(self.root)
        raw = json.loads(config_path.read_text())
        self.assertEqual(parse_ingress_config(raw).allow_chat_id, HUMAN_CHAT)
        bad_cases = {
            "extra key (api base)": {**raw, "api_base": "https://evil.example"},
            "two humans": {**raw, "allowlist": [raw["allowlist"], raw["allowlist"]]},
            "float chat id": {**raw, "allowlist": {"user_id": HUMAN_USER, "chat_id": 1.5}},
            "bool user id": {**raw, "allowlist": {"user_id": True, "chat_id": HUMAN_CHAT}},
            "negative user id": {**raw, "allowlist": {"user_id": -5, "chat_id": HUMAN_CHAT}},
            "shared uid": {**raw, "approval_uid": raw["ingress_uid"]},
            "relative state dir": {**raw, "state_dir": "state"},
            "token in state dir": {**raw, "credential": {**raw["credential"],
                                                          "location": raw["state_dir"] + "/bot-token"}},
            "wrong schema": {**raw, "schema": "v0"},
            "group chat": {**raw, "allowlist": {"user_id": HUMAN_USER, "chat_id": OTHER_CHAT}},
            "another user's private chat": {**raw, "allowlist": {"user_id": HUMAN_USER,
                                                                 "chat_id": OTHER_USER}},
        }
        for label, candidate in bad_cases.items():
            with self.subTest(label):
                with self.assertRaises(IngressConfigError):
                    parse_ingress_config(candidate)
        # trust checks on the file itself
        with self.assertRaises(IngressConfigError):
            load_ingress_config(config_path, expected_owner_uid=os.geteuid() + 1)
        config_path.chmod(0o666)
        with self.assertRaises(IngressConfigError):
            load_ingress_config(config_path, expected_owner_uid=os.geteuid())
        config_path.chmod(0o644)
        link = self.root / "link.json"
        link.symlink_to(config_path)
        with self.assertRaises(IngressConfigError):
            load_ingress_config(link, expected_owner_uid=os.geteuid())
        config_path.write_text('{"schema": 1, "schema": 2}')
        with self.assertRaises(IngressConfigError):
            load_ingress_config(config_path, expected_owner_uid=os.geteuid())

    def test_registration_is_one_human_with_integer_provider_ids(self) -> None:
        with self.assertRaises(HostControlError):
            HumanIngressRegistration(uid=1, provider="email", human_subject="m",
                                     provider_user_id="1", provider_chat_id="1")
        with self.assertRaises(HostControlError):
            HumanIngressRegistration(uid=1, provider="telegram", human_subject="m",
                                     provider_user_id="1.0", provider_chat_id="1")
        with self.assertRaises(HostControlError):
            # a group chat (negative id, != user id) is never allowlisted
            HumanIngressRegistration(uid=1, provider="telegram", human_subject="m",
                                     provider_user_id="1", provider_chat_id="-1001234567890")
        self.assertEqual(HumanIngressRegistration(uid=1, provider="telegram", human_subject="m",
                                                  provider_user_id="42", provider_chat_id="42"
                                                  ).provider_chat_id, "42")


# ---------------------------------------------------------------------------
# mutation checks: each key guard is load-bearing
# ---------------------------------------------------------------------------


def _without(code: str, replacement=None, *, also: dict | None = None):
    """CALLBACK_CHECKS with ``code`` neutered (dropped, or swapped for a
    weakened ``replacement``); ``also`` neuters co-located backstops for
    guards the ingress deliberately enforces twice."""

    swaps = {code: replacement}
    swaps.update(also or {})
    checks = []
    for name, fn in CALLBACK_CHECKS:
        if name in swaps:
            if swaps[name] is not None:
                checks.append((name, swaps[name]))
            continue
        checks.append((name, fn))
    return tuple(checks)


def _lenient_tag(ctx) -> bool:
    data = ctx.cq.get("data")
    ctx.code = data[3] if isinstance(data, str) and len(data) > 3 and data[3] in "gd" else "g"
    return True


def _any_open_card(ctx) -> bool:
    cards = ctx.core.state.open_cards()
    ctx.card = cards[0] if cards else None
    return ctx.card is not None


class MutationTests(unittest.TestCase):
    """Neuter ONE guard; the matching spoof must then get through. A
    mutant that is still refused means the guard was not load-bearing
    (the test fails). The ingress checks run against a RecordingDecider
    so the service's defense in depth cannot mask a neutered check."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _spoof_verdict(self, code: str, inject, *, mutate: bool, replacement=None, clock_minutes=0,
                       also=None):
        checks = _without(code, replacement, also=also) if mutate else CALLBACK_CHECKS
        stack = build_human_stack(self.root / f"{code}-{mutate}",
                                  decider_factory=lambda _svc: RecordingDecider(), checks=checks)
        try:
            review = stack.open_review()
            stack.request(review)
            card = stack.fake.last_card()
            stack.clock.advance(minutes=clock_minutes)
            inject(stack, card)
            updates = stack.core.api.get_updates(offset=stack.state.cursor(), timeout=0)
            verdict = None
            for update in updates:
                verdict = stack.core.process_update(update)
            return verdict, len(stack.decider.calls)
        finally:
            stack.close()

    def test_each_ingress_match_guard_is_load_bearing(self) -> None:
        def cb(sender=None, mutate_message=None, data_index=0, data=None, callback_id=None, pre=None):
            def inject(stack, card):
                if pre is not None:
                    pre(stack, card)
                message = stack.fake.card_message(card["chat"]["id"], card["message_id"])
                if mutate_message:
                    mutate_message(message)
                stack.fake.callback(message=message,
                                    data=data or stack.fake.button_data(card, data_index),
                                    sender=sender, callback_id=callback_id)
            return inject

        def replay_pre(stack, card):
            # the same callback id was already seen (refused once as wrong user)
            stack.fake.callback(message=card, data=stack.fake.button_data(card, 0),
                                sender=stack.fake.user(OTHER_USER), callback_id="dup-1")
            for update in stack.core.api.get_updates(offset=stack.state.cursor(), timeout=0):
                stack.core.process_update(update)

        def closed_pre(stack, card):
            stack.state.begin()
            stack.state.close_card(stack.state.open_cards()[0].review_handle, "test")
            stack.state.commit()

        cases = {
            # a bot account presenting the allowlisted user id isolates the
            # is_bot guard from the user allowlist (the bot's OWN id is
            # refused by both; covered in the spoof matrix)
            "bot_sender": dict(inject=cb(sender=FakeBotApi.user(HUMAN_USER, is_bot=True))),
            "user_not_allowlisted": dict(inject=cb(sender=FakeBotApi.user(OTHER_USER))),
            # chat is enforced twice: the allowlist check AND the card
            # correlation key (chat id, message id); both are neutered
            "chat_not_allowlisted": dict(inject=cb(mutate_message=lambda m: m.update(chat=FakeBotApi.chat(OTHER_CHAT))),
                                         also={"unknown_message": _any_open_card}),
            "unknown_message": dict(inject=cb(mutate_message=lambda m: m.update(message_id=m["message_id"] + 5)),
                                    replacement=_any_open_card),
            "card_closed": dict(inject=cb(pre=closed_pre)),
            "callback_replay": dict(inject=cb(pre=replay_pre, callback_id="dup-1")),
            "edited_card": dict(inject=cb(mutate_message=lambda m: m.update(text=m["text"] + "!"))),
            "callback_mismatch": dict(inject=cb(data="v1:g" + "B" * 43), replacement=_lenient_tag),
            "card_expired": dict(inject=cb(), clock_minutes=16),
            "no_message": dict(inject=cb(mutate_message=lambda m: m.update(date=0))),
        }
        for code, spec in cases.items():
            with self.subTest(guard=code):
                real, real_calls = self._spoof_verdict(code, spec["inject"], mutate=False,
                                                       clock_minutes=spec.get("clock_minutes", 0))
                self.assertEqual((real, real_calls), (code, 0))
                mutant, mutant_calls = self._spoof_verdict(
                    code, spec["inject"], mutate=True, replacement=spec.get("replacement"),
                    clock_minutes=spec.get("clock_minutes", 0), also=spec.get("also"))
                self.assertEqual((mutant, mutant_calls), ("accepted", 1),
                                 f"neutering {code} did not let the spoof through")

    def _service_stack(self, name: str):
        stack = build_human_stack(self.root / name)
        review = stack.open_review()
        stack.request(review)
        return stack, review, stack.journal.get_human_request_for_review(review.review_handle)

    def test_service_uid_gate_is_load_bearing(self) -> None:
        stack, review, record = self._service_stack("uid")
        try:
            args = (review.review_handle, review.plan_digest, "granted", "s", record.nonce, "k")
            with self.assertRaises(PolicyDenied):
                stack.service.human_decision(CONSUMER_UID, *args)
            with mock.patch.object(ApprovalService, "_require_ingress_uid",
                                   lambda self, uid: self._human_ingress):
                self.assertEqual(stack.service.human_decision(CONSUMER_UID, *args).state, "granted")
        finally:
            stack.close()

    def test_service_nonce_match_is_load_bearing(self) -> None:
        stack, review, record = self._service_stack("nonce")
        try:
            args = (review.review_handle, review.plan_digest, "granted", "s")
            with self.assertRaises(HumanDecisionRefused):
                stack.service.human_decision(INGRESS_UID, *args, "e" * 64, "k")
            with mock.patch.object(service_mod, "_nonce_matches", lambda a, b: True):
                with self.assertRaises(HostControlError):
                    # the durable layer still refuses a wrong nonce: the
                    # journal's consume UPDATE is keyed on the stored nonce
                    # and the attestation's nonce must equal the record's
                    stack.service.human_decision(INGRESS_UID, *args, "e" * 64, "k")
            self.assertEqual(stack.journal.get_review(review.review_handle).state, "open")
        finally:
            stack.close()

    def test_service_single_use_layers(self) -> None:
        """Single use is enforced by four independent layers: the request
        state check, the review state check, the state+nonce-guarded
        consume UPDATE, and UNIQUE(callback_nonce/review/request) on the
        attestation. Neutering the first two still refuses a second
        decision (the durable layers hold); this proves the durable layer
        is the load-bearing one."""

        stack, review, record = self._service_stack("single")
        try:
            args = (review.review_handle, review.plan_digest, "granted", "s")
            stack.service.human_decision(INGRESS_UID, *args, record.nonce, "k1")
            live = stack.journal.get_human_request(record.request_handle)
            with mock.patch.object(ApprovalService, "_lazy_expire_human",
                                   lambda self, r: dataclasses.replace(r, state="presented")), \
                    mock.patch.object(ApprovalService, "_lazy_expire_review",
                                      lambda self, r: r):
                with self.assertRaises(HostControlError):
                    stack.service.human_decision(INGRESS_UID, *args, record.nonce, "k2")
            self.assertEqual(live.state, "decided")
            self.assertEqual(stack.journal.counts().issued_grants, 1)
        finally:
            stack.close()

    def test_service_expiry_check_is_load_bearing(self) -> None:
        stack, review, record = self._service_stack("expiry")
        try:
            stack.clock.advance(minutes=16)
            args = (review.review_handle, review.plan_digest, "granted", "s", record.nonce, "k")
            with self.assertRaises(HumanDecisionRefused):
                stack.service.human_decision(INGRESS_UID, *args)
        finally:
            stack.close()
        stack, review, record = self._service_stack("expiry-mutant")
        try:
            stack.clock.advance(minutes=16)
            args = (review.review_handle, review.plan_digest, "granted", "s", record.nonce, "k")
            with mock.patch.object(ApprovalService, "_is_past", staticmethod(lambda stamp, now: False)):
                self.assertEqual(stack.service.human_decision(INGRESS_UID, *args).state, "granted")
        finally:
            stack.close()

    def test_leak_redaction_is_load_bearing_on_every_failure_path(self) -> None:
        """M8: the error detail set to the token-bearing URL must turn the
        canary red on EVERY failure path, even through the log's 64-char
        detail truncation."""

        real = failure_path_logs(mutated=False)
        mutant = failure_path_logs(mutated=True)
        self.assertEqual(set(real), {"transport", "http_status", "auth", "not_ok", "malformed"})
        for code in real:
            with self.subTest(path=code):
                self.assertFalse(leaks_token(real[code]))
                self.assertTrue(leaks_token(mutant[code]), f"M8 survived on {code}")

    def test_card_author_and_echo_and_inline_guards_are_load_bearing(self) -> None:
        """M2 (card author must be the getMe bot id) and the inline /
        private-chat conditions: neutering the specific condition (via a
        patched predicate) lets the matching spoof through."""

        def run(code, mutant, inject):
            checks = tuple((n, mutant if n == code else f) for n, f in CALLBACK_CHECKS) if mutant else CALLBACK_CHECKS
            stack = build_human_stack(self.root / f"m-{code}-{bool(mutant)}",
                                      decider_factory=lambda _svc: RecordingDecider(), checks=checks)
            try:
                stack.request(stack.open_review())
                card = stack.fake.last_card()
                inject(stack, card)
                verdict = None
                for update in stack.core.api.get_updates(offset=stack.state.cursor(), timeout=0):
                    verdict = stack.core.process_update(update)
                return verdict
            finally:
                stack.close()

        def author_mutant(ctx):  # M2: drop ONLY the author-id comparison
            message = ctx.cq["message"]
            author = message.get("from")
            return (isinstance(author, dict) and author.get("is_bot") is True
                    and "edit_date" not in message and "forward_origin" not in message
                    and card_mod.text_digest(message.get("text", "")) == ctx.card.text_sha256)

        def inline_mutant(ctx):  # drop ONLY the inline_message_id condition
            message = ctx.cq.get("message")
            return (isinstance(message, dict) and isinstance(message.get("date"), int)
                    and message["date"] > 0 and isinstance(message.get("chat"), dict))

        def private_mutant(ctx):  # drop ONLY the chat type condition
            return ctx.cq["message"]["chat"]["id"] == ctx.core.allow_chat_id

        def other_bot(stack, card):
            message = stack.fake.card_message(card["chat"]["id"], card["message_id"])
            message["from"] = stack.fake.user(BOT_ID + 1, is_bot=True)
            stack.fake.callback(message=message, data=stack.fake.button_data(card, 0))

        def with_inline(stack, card):
            stack.fake.callback(message=stack.fake.card_message(card["chat"]["id"], card["message_id"]),
                                data=stack.fake.button_data(card, 0), inline_message_id="inl-9")

        def group_chat(stack, card):
            message = stack.fake.card_message(card["chat"]["id"], card["message_id"])
            message["chat"] = stack.fake.chat(HUMAN_CHAT, "group")
            stack.fake.callback(message=message, data=stack.fake.button_data(card, 0))

        for code, mutant, inject in (("edited_card", author_mutant, other_bot),
                                     ("no_message", inline_mutant, with_inline),
                                     ("chat_not_allowlisted", private_mutant, group_chat)):
            with self.subTest(guard=code):
                self.assertEqual(run(code, None, inject), code)
                self.assertEqual(run(code, mutant, inject), "accepted")

    def test_send_message_chat_echo_check_is_load_bearing(self) -> None:
        """A sendMessage reply echoing a DIFFERENT chat is refused (the card
        never opens). Neutering the echo predicate opens a card correlated
        to a message the allowlisted chat never received."""

        for mutated in (False, True):
            stack = build_human_stack(self.root / f"echo-{mutated}")
            try:
                stack.fake.faults["sendMessage.echo"] = "wrong_chat"
                review = stack.open_review()
                if mutated:
                    with mock.patch.object(core_mod, "_delivery_matches", lambda message, chat: True):
                        result = stack.request(review)
                    self.assertEqual(result.state, "presented")
                    self.assertEqual(len(stack.state.open_cards()), 1)
                else:
                    result = stack.request(review)
                    self.assertEqual(result.state, "pending")
                    self.assertEqual(stack.state.open_cards(), [])
                    self.assertIn("detail=shape", stack.log_stream.getvalue())
            finally:
                stack.close()


if __name__ == "__main__":
    unittest.main()
