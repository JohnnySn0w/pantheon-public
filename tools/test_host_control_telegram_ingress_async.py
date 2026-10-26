"""T-5518: non-blocking approval <-> ingress calls, bounded retry, and the
approval-side Present retry — in-process tier (stdlib only; runs in the
main ci.yml tools suite AND the dbus-tiers glob, never skips).

Intent (T-2652 / ORG-052 §5-§6, pre-T-2920): neither service's loop
blocks on the other, retryable refusals back off with a bound, and a
presented-but-unrecorded card recovers WITHOUT a RequestHuman replay —
while nonce and decision stay single-use.

The real D-Bus mechanism (dbus-python async reply/error handlers and the
deferred RequestHuman reply) is proven over a private bus in
tools/test_host_control_telegram_ingress_dbus.py; here the same core and
service logic runs against scripted async seams and a manual scheduler
on the fixture clock, so a 15-minute window is simulated exactly.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tools.host_control.approval import presentation as presentation_mod
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    PolicyApproverAuthorizer,
    parse_approver_policy,
)
from tools.host_control.approval.presentation import PresentReply
from tools.host_control.approval.service import ApprovalService
from tools.host_control.contracts import HostControlError
from tools.host_control.telegram_ingress import core as core_mod
from tools.host_control.telegram_ingress.core import (
    DecisionRefused,
    DecisionUnavailable,
)
from tools.test_host_control_telegram_ingress_fixtures import (
    CONSUMER_UID,
    INGRESS_UID,
    HeldDecider,
    InProcessDecider,
    build_human_stack,
    human_policy_raw,
    registration,
)

TICK = timedelta(milliseconds=250)


class _StackCase(unittest.TestCase):
    ASYNC_DELIVERY = True

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="t5518-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.stack = build_human_stack(
            Path(self._tmp) / "stack", async_delivery=self.ASYNC_DELIVERY
        )
        self.addCleanup(self.stack.close)

    def record(self, review):
        return self.stack.journal.get_human_request_for_review(review.review_handle)

    def request_and_deliver(self, review, key: str = "rh-1"):
        """RequestHuman as the D-Bus handler runs it: the domain call,
        then (when pending) the deferred delivery; returns what the
        caller would be replied with (None until settled)."""

        result = self.stack.service.request_human(
            "specialist", review.review_handle, review.plan_digest, key
        )
        settled: list = []
        if result.state == "pending":
            deferred = self.stack.service.deliver_human_request(
                result.request_handle, lambda outcome, error: settled.append((outcome, error))
            )
            self.assertTrue(deferred)
        else:
            settled.append(((result.request_handle, result.state), None))
        return settled

    def tap(self, card) -> None:
        self.stack.fake.callback(message=card, data=self.stack.fake.button_data(card, 0))
        self.assertEqual(self.stack.core.poll_once(timeout=0), 1)

    def attestations(self) -> int:
        return len(
            self.stack.journal._require_open()
            .execute("SELECT 1 FROM provider_attestations")
            .fetchall()
        )

    def tick(self) -> None:
        """One simulated loop tick on both sides."""

        self.stack.clock.advance(milliseconds=250)
        self.stack.scheduler.run_due()
        self.stack.core.expire_cards()
        self.stack.core.recover()


# ---------------------------------------------------------------------------
# approval side: HumanCardDelivery through the service
# ---------------------------------------------------------------------------


class ApprovalSidePresentRetryTests(_StackCase):
    def test_lost_present_then_tap_then_no_replay_completes_once_via_service_retry(self) -> None:
        """Ask 5(2): the T-2919 r2 stall. Present is serviced by the
        ingress but its reply is lost; the human taps; NOBODY replays
        RequestHuman. The service's own retry reaches 'presented' and the
        decision lands exactly once."""

        stack = self.stack
        stack.presenter.mode = "lost"
        review = stack.open_review()
        settled = self.request_and_deliver(review)
        self.assertEqual(settled[0][0][1], "pending")  # the caller hears pending
        self.assertEqual(self.record(review).state, "pending")
        self.assertEqual(len(stack.fake.messages), 1)  # ...but the card WAS sent
        card = stack.fake.last_card()
        self.tap(card)
        # retryable: intent pending, card closed locally (decision_pending)
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        row = stack.state.card_for_review(review.review_handle)
        self.assertEqual((row.state, row.closed_reason), ("closed", "decision_pending"))
        self.assertIn("detail=human_request_not_presented", stack.log_stream.getvalue())
        stack.presenter.mode = "ok"
        for _ in range(int(presentation_mod.PRESENT_RETRY_INITIAL_SECONDS / 0.25) + 4):
            self.tick()
        live = stack.journal.get_review(review.review_handle)
        self.assertEqual((live.state, live.authority_source), ("granted", "human"))
        self.assertEqual(self.record(review).state, "decided")
        self.assertEqual(stack.journal.counts().issued_grants, 1)
        self.assertEqual(self.attestations(), 1)
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "granted")])
        self.assertEqual(len(stack.fake.messages), 1)  # the retry replayed; no 2nd card
        self.assertEqual(len(stack.presenter.calls), 2)
        self.assertIn("event=present_replayed", stack.log_stream.getvalue())
        # quiescent afterwards: nothing armed, nothing more sent
        for _ in range(400):
            self.tick()
        self.assertEqual(len(stack.presenter.calls), 2)
        self.assertEqual(self.attestations(), 1)

    def test_present_retry_backs_off_to_the_cap_and_stops_at_expiry(self) -> None:
        stack = self.stack
        stack.presenter.mode = "down"
        review = stack.open_review()
        self.request_and_deliver(review)
        expires = datetime.fromisoformat(self.record(review).expires_at)
        window = (expires - stack.clock()) + timedelta(minutes=5)
        for _ in range(int(window / TICK)):
            self.tick()
        self.assertEqual(self.record(review).state, "expired")
        calls = len(stack.presenter.calls)
        # the schedule is exactly 2, 4, 8, 16, 32, then 60 s to the cap
        expected = [presentation_mod.retry_delay(n) for n in range(1, calls)]
        self.assertEqual(stack.scheduler.delays[: calls - 1], expected)
        self.assertEqual(expected[:6], [2.0, 4.0, 8.0, 16.0, 32.0, 60.0])
        # a 15-minute card: 6 attempts to reach the cap, then one a minute
        self.assertLessEqual(calls, 21)
        self.assertGreaterEqual(calls, 15)
        self.assertEqual(stack.scheduler.timers, [])  # stopped, nothing armed
        for _ in range(400):
            self.tick()
        self.assertEqual(len(stack.presenter.calls), calls)

    def test_present_retry_stops_once_the_request_is_withdrawn(self) -> None:
        stack = self.stack
        stack.presenter.mode = "down"
        review = stack.open_review()
        self.request_and_deliver(review)

        class _Allow:
            allowed = True

        stack.service._authorizer.may_withdraw = lambda subject: _Allow()
        stack.service.withdraw_human_request("admin", self.record(review).request_handle)
        stack.presenter.mode = "ok"
        for _ in range(400):
            self.tick()
        self.assertEqual(self.record(review).state, "withdrawn")
        self.assertEqual(len(stack.presenter.calls), 1)  # the armed retry found it closed
        self.assertEqual(stack.fake.messages, {})

    def test_a_permanent_ingress_refusal_is_not_retried(self) -> None:
        stack = self.stack
        sent: list = []

        class Refusing:
            def begin_present(self, *args, on_done):
                sent.append(args)
                on_done(PresentReply.failed(retryable=False))

        stack.service._delivery._presenter = Refusing()
        review = stack.open_review()
        self.request_and_deliver(review)
        for _ in range(400):
            self.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.record(review).state, "pending")

    def test_resume_is_idempotent_and_never_doubles_an_in_flight_present(self) -> None:
        stack = self.stack
        stack.presenter.mode = "hold"
        review = stack.open_review()
        # a pending record with no delivery yet (as after a restart)
        result = stack.service.request_human("specialist", review.review_handle, review.plan_digest, "k")
        self.assertEqual(result.state, "pending")
        self.assertEqual(stack.service.resume_human_deliveries(), 1)
        self.assertEqual(stack.service.resume_human_deliveries(), 1)
        self.assertEqual(len(stack.presenter.calls), 1)  # one in flight, not two
        self.assertEqual(stack.service._delivery.in_flight(), 1)
        # a RequestHuman replay while in flight JOINS that attempt
        joined = self.request_and_deliver(review, "k")
        self.assertEqual(joined, [])
        self.assertEqual(len(stack.presenter.calls), 1)
        stack.presenter.release()
        self.assertEqual(joined[0][0][1], "presented")
        self.assertEqual(self.record(review).state, "presented")
        self.assertEqual(stack.service._delivery.in_flight(), 0)
        # resume on a presented record sends nothing
        self.assertEqual(stack.service.resume_human_deliveries(), 0)
        self.assertEqual(len(stack.presenter.calls), 1)

    def test_resume_while_a_retry_is_armed_does_not_add_a_second_attempt(self) -> None:
        stack = self.stack
        stack.presenter.mode = "down"
        review = stack.open_review()
        self.request_and_deliver(review)
        self.assertEqual(stack.service._delivery.armed(), 1)
        stack.service.resume_human_deliveries()
        stack.service.resume_human_deliveries()
        self.assertEqual(len(stack.presenter.calls), 1)
        self.assertEqual(len(stack.scheduler.timers), 1)
        # an explicit RequestHuman overtakes the armed timer (one attempt),
        # and the stale timer is ignored when it fires
        stack.presenter.mode = "hold"
        self.request_and_deliver(review, "rh-2")
        self.assertEqual(len(stack.presenter.calls), 2)
        stack.clock.advance(seconds=presentation_mod.PRESENT_RETRY_CAP_SECONDS)
        stack.scheduler.run_due()
        self.assertEqual(len(stack.presenter.calls), 2)
        self.assertEqual(stack.service._delivery.in_flight(), 1)
        stack.presenter.release()
        self.assertEqual(self.record(review).state, "presented")

    def test_async_presenter_configuration_is_closed(self) -> None:
        kwargs = dict(
            journal=self.stack.journal,
            metadata_source=self.stack.metadata,
            clock=self.stack.clock,
        )
        with self.assertRaises(HostControlError):
            ApprovalService(card_presenter=self.stack.presenter, **kwargs)
        with self.assertRaises(HostControlError):
            ApprovalService(
                card_presenter=self.stack.presenter,
                delivery_scheduler=self.stack.scheduler,
                presenter=self.stack.presenter,
                **kwargs,
            )

    def test_present_call_never_renders_the_nonce(self) -> None:
        review = self.stack.open_review()
        self.request_and_deliver(review)
        nonce = self.record(review).nonce
        load = self.stack.service._delivery_load(self.record(review).request_handle)
        self.assertNotIn(nonce, repr(load))


# ---------------------------------------------------------------------------
# ingress side: async HumanDecision, in-flight dedupe, bounded backoff
# ---------------------------------------------------------------------------


class AlwaysUnavailable:
    def __init__(self) -> None:
        self.calls = 0

    def human_decision(self, *args):
        self.calls += 1
        raise DecisionUnavailable()


class IngressBackoffTests(_StackCase):
    ASYNC_DELIVERY = False  # the presenter seam is irrelevant here

    def presented_card(self):
        review = self.stack.open_review()
        self.assertEqual(self.stack.request(review).state, "presented")
        return review, self.stack.fake.last_card()

    def test_retry_is_bounded_logged_log2_and_ends_unknown_after_the_expiry_grace(self) -> None:
        stack = self.stack
        review, card = self.presented_card()
        decider = AlwaysUnavailable()
        stack.core.decider = decider
        self.tap(card)
        expires = datetime.fromisoformat(self.record(review).expires_at)
        ticks = int(((expires - stack.clock()) + timedelta(minutes=5)) / TICK)
        for _ in range(ticks):
            stack.clock.advance(milliseconds=250)
            stack.core.expire_cards()
            stack.core.recover()
        # a 15-minute window at 250 ms ticks was 3600+ ticks: the old code
        # called (and logged) on every one
        self.assertGreaterEqual(ticks, 3600)
        # pre-expiry backoff (<= 40) plus at most the post-expiry cap
        self.assertLessEqual(decider.calls, 40 + core_mod.DECISION_EXPIRY_MAX_ATTEMPTS)
        self.assertGreaterEqual(decider.calls, 30)
        logged = stack.log_stream.getvalue().count("event=decision_unavailable")
        self.assertLessEqual(logged, 6)  # attempts 1, 2, 4, 8, 16, 32
        # the service never answered: the outcome is UNKNOWN, never expired
        intents = stack.state.intents()
        self.assertEqual([(i.state, i.outcome) for i in intents], [("done", "unknown")])
        self.assertIn("detail=status_unknown", stack.log_stream.getvalue())
        row = stack.state.card_for_review(review.review_handle)
        self.assertEqual(row.closed_reason, "decided:unknown")
        closed = stack.fake.card_message(card["chat"]["id"], card["message_id"])
        self.assertNotIn("reply_markup", closed)
        self.assertIn("status unknown", closed["text"])
        calls = decider.calls
        for _ in range(400):
            stack.clock.advance(milliseconds=250)
            stack.core.recover()
        self.assertEqual(decider.calls, calls)  # nothing after the grace

    def test_retry_delay_schedule(self) -> None:
        self.assertEqual(
            [core_mod.decision_retry_delay(n) for n in range(1, 8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_present_is_served_and_nothing_is_resent_while_a_decision_is_in_flight(self) -> None:
        stack = self.stack
        review, card = self.presented_card()
        held = HeldDecider(stack.decider)
        stack.core.decider = held
        self.tap(card)
        self.assertEqual(len(held.calls), 1)
        _cb = stack.fake.answered[:]
        for _ in range(200):
            stack.clock.advance(milliseconds=250)
            self.assertEqual(stack.core.recover(), 0)
        self.assertEqual(len(held.calls), 1)  # in flight: never sent twice
        # the single in-flight guard, directly: a second submission of the
        # same intent (tap path or recovery path) is refused while in flight
        intent = stack.state.intents()[0]
        card_row = stack.state.card_for_review(review.review_handle)
        self.assertEqual(stack.core._submit(intent, card_row), "in_flight")
        self.assertEqual(len(held.calls), 1)
        # the ingress still serves Present for another review meanwhile
        other = stack.service.review("specialist", "4" * 64, "5" * 64, None).record
        self.assertEqual(stack.request(other, "rh-other").state, "presented")
        self.assertEqual(stack.fake.answered, _cb)  # answered ONCE, at tap time (N4)
        self.assertEqual(held.release_all(), 1)
        self.assertEqual(stack.journal.get_review(review.review_handle).state, "granted")
        self.assertEqual(stack.fake.answered, _cb)
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "granted")])

    def test_a_late_or_duplicate_completion_on_a_finished_intent_is_a_no_op(self) -> None:
        stack = self.stack
        review, card = self.presented_card()
        held = HeldDecider(stack.decider)
        stack.core.decider = held
        self.tap(card)
        (_args, on_done) = held.pending[0]
        held.release_all()
        edits = [r.method for r in stack.fake.requests].count("editMessageText")
        answers = len(stack.fake.answered)
        # the same reply delivered again (exactly-once wrapper) ...
        on_done(("denied", "b" * 64), None)
        # ... and a completion from a second, overlapping submission
        intent = stack.state.intents()[0]
        card_row = stack.state.card_for_review(review.review_handle)
        stack.core._complete(intent, card_row, ("denied", "b" * 64), None)
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "granted")])
        self.assertEqual(stack.state.card_for_review(review.review_handle).closed_reason, "decided:granted")
        self.assertEqual([r.method for r in stack.fake.requests].count("editMessageText"), edits)
        self.assertEqual(len(stack.fake.answered), answers)
        # the durable guard itself: done is never overwritten
        stack.state.begin()
        self.assertFalse(stack.state.finish_intent(intent.idempotency_key, "denied"))
        stack.state.commit()
        self.assertEqual(stack.state.intents()[0].outcome, "granted")
        self.assertEqual(self.attestations(), 1)
        # r1 N5: the disagreeing late answer is visible, bounded, nonce-free
        log = stack.log_stream.getvalue()
        self.assertIn("event=decision_outcome_mismatch", log)
        self.assertIn("detail=granted->denied", log)
        self.assertNotIn(stack.journal.get_human_request_for_review(review.review_handle).nonce, log)

    def test_a_definitive_refusal_finishes_immediately_without_backoff(self) -> None:
        stack = self.stack
        review, card = self.presented_card()
        held = HeldDecider(stack.decider)
        held.answer_with = DecisionRefused("human_request_closed")
        stack.core.decider = held
        self.tap(card)
        held.release_all()
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "refused")])
        for _ in range(200):
            stack.clock.advance(milliseconds=250)
            stack.core.recover()
        self.assertEqual(len(held.calls), 1)


# ---------------------------------------------------------------------------
# T-5518 r1 B1: expiry never masks a committed decision
# ---------------------------------------------------------------------------


class CommitNowReplyLater:
    """Async decider (the r1 reviewer's interleave probe): the SERVICE
    commits at send time, the reply lands only on ``release``. ``lose``
    replaces the FIRST reply with ``DecisionUnavailable`` (a timeout after
    the commit); later calls are answered normally."""

    def __init__(self, service, *, lose: bool = False) -> None:
        self.inner = InProcessDecider(service, INGRESS_UID)
        self.lose = lose
        self.pending: list = []
        self.calls = 0

    def begin_human_decision(self, *args, on_done) -> None:
        self.calls += 1
        try:
            result, error = self.inner.human_decision(*args), None
        except (DecisionRefused, DecisionUnavailable) as exc:
            result, error = None, exc
        if self.lose and self.calls == 1:
            result, error = None, DecisionUnavailable()
        self.pending.append((on_done, result, error))

    def release(self) -> None:
        pending, self.pending = self.pending, []
        for on_done, result, error in pending:
            on_done(result, error)


class ExpiryNeverMasksACommittedDecisionTests(_StackCase):
    ASYNC_DELIVERY = False

    def stack_with(self, decider_factory):
        self.stack.close()
        root = Path(self._tmp) / f"b1-{secrets.token_hex(4)}"
        self.stack = build_human_stack(root, decider_factory=decider_factory)
        self.addCleanup(self.stack.close)
        return self.stack

    def tap_just_before_expiry(self, stack):
        review = stack.open_review()
        self.assertEqual(stack.request(review).state, "presented")
        expires = datetime.fromisoformat(self.record(review).expires_at)
        stack.clock.advance(seconds=(expires - stack.clock()).total_seconds() - 2)
        self.tap(stack.fake.last_card())
        return review

    def assert_ends_granted_everywhere(self, stack, review) -> None:
        self.assertEqual(stack.journal.get_review(review.review_handle).state, "granted")
        self.assertEqual(stack.journal.counts().issued_grants, 1)
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "granted")])
        self.assertEqual(stack.state.card_for_review(review.review_handle).closed_reason, "decided:granted")
        edits = [r for r in stack.fake.requests if r.method == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertIn("APPROVED", json.loads(edits[-1].body)["text"])
        self.assertNotIn("decision_outcome_mismatch", stack.log_stream.getvalue())

    def test_p1_reply_in_flight_across_expiry_lands_as_the_committed_outcome(self) -> None:
        holder: dict = {}
        stack = self.stack_with(lambda svc: holder.setdefault("d", CommitNowReplyLater(svc)))
        review = self.tap_just_before_expiry(stack)
        decider = holder["d"]
        stack.clock.advance(seconds=5)  # past expiry, reply not yet back
        stack.core.recover()
        # in flight: not closed under it, not resent
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        self.assertEqual(decider.calls, 1)
        decider.release()
        stack.core.recover()
        self.assert_ends_granted_everywhere(stack, review)
        self.assertEqual(decider.calls, 1)

    def test_p2_reply_lost_then_post_expiry_resubmit_returns_the_committed_outcome(self) -> None:
        holder: dict = {}
        stack = self.stack_with(
            lambda svc: holder.setdefault("d", CommitNowReplyLater(svc, lose=True)))
        review = self.tap_just_before_expiry(stack)
        decider = holder["d"]
        decider.release()  # the committed answer is lost: unavailable
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        stack.clock.advance(seconds=5)  # past expiry
        stack.core.recover()  # the final idempotent call (same key), due at once
        self.assertEqual(decider.calls, 2)
        decider.release()
        self.assert_ends_granted_everywhere(stack, review)

    def test_an_in_flight_decision_is_never_closed_even_past_the_grace(self) -> None:
        holder: dict = {}
        stack = self.stack_with(lambda svc: holder.setdefault("d", CommitNowReplyLater(svc)))
        review = self.tap_just_before_expiry(stack)
        decider = holder["d"]
        for _ in range(int(core_mod.DECISION_EXPIRY_GRACE_SECONDS / 0.25) + 40):
            stack.clock.advance(milliseconds=250)
            stack.core.recover()
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        self.assertEqual(decider.calls, 1)
        decider.release()
        self.assert_ends_granted_everywhere(stack, review)

    def test_p6_restart_long_after_expiry_still_asks_the_service_once(self) -> None:
        """r2 B1 (probe P6): the committed reply is lost, the ingress is
        down until expiry + 300 s (past the grace), then restarts with a
        fresh in-memory counter while the service is reachable. The first
        recover() must make the final idempotent call, not close as
        unknown with zero calls."""

        holder: dict = {}
        stack = self.stack_with(
            lambda svc: holder.setdefault("d", CommitNowReplyLater(svc, lose=True)))
        review = self.tap_just_before_expiry(stack)
        decider = holder["d"]
        decider.release()  # committed, reply lost
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        stack.clock.advance(seconds=300 + core_mod.DECISION_EXPIRY_GRACE_SECONDS)
        restarted = core_mod.IngressCore(
            state=stack.state, api=stack.core.api, decider=decider,
            bot_user_id=stack.core.bot_user_id, allow_user_id=stack.core.allow_user_id,
            allow_chat_id=stack.core.allow_chat_id,
            card_ttl_max_seconds=stack.core.card_ttl_max_seconds,
            log=stack.core.log, clock=stack.clock)
        stack.core = restarted
        restarted.recover()
        self.assertEqual(decider.calls, 2)  # the final call WAS made
        self.assertEqual([i.state for i in stack.state.intents()], ["pending"])
        decider.release()
        self.assert_ends_granted_everywhere(stack, review)

    def test_after_expiry_the_services_expired_verdict_is_final(self) -> None:
        class DownThenReal:
            def __init__(self, svc):
                self.inner = InProcessDecider(svc, INGRESS_UID)
                self.down = True
                self.calls = 0

            def human_decision(self, *args):
                self.calls += 1
                if self.down:
                    raise DecisionUnavailable()
                return self.inner.human_decision(*args)

        holder: dict = {}
        stack = self.stack_with(lambda svc: holder.setdefault("d", DownThenReal(svc)))
        review = self.tap_just_before_expiry(stack)
        decider = holder["d"]
        decider.down = False
        stack.clock.advance(seconds=5)  # never committed; now past expiry
        stack.core.recover()
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "expired")])
        self.assertEqual(stack.journal.get_review(review.review_handle).state, "open")
        self.assertEqual(stack.journal.counts().issued_grants, 0)
        self.assertIn("detail=human_request_expired", stack.log_stream.getvalue())

    def test_post_expiry_calls_are_capped_then_unknown(self) -> None:
        stack = self.stack
        review = stack.open_review()
        stack.request(review)
        decider = AlwaysUnavailable()
        stack.core.decider = decider
        expires = datetime.fromisoformat(self.record(review).expires_at)
        stack.clock.advance(seconds=(expires - stack.clock()).total_seconds() - 2)
        self.tap(stack.fake.last_card())
        before = decider.calls
        stack.clock.advance(seconds=3)  # just past expiry
        for _ in range(int(core_mod.DECISION_EXPIRY_GRACE_SECONDS / 0.25) + 40):
            stack.clock.advance(milliseconds=250)
            stack.core.recover()
        self.assertGreaterEqual(decider.calls - before, 1)  # a final call WAS made
        self.assertLessEqual(decider.calls - before, core_mod.DECISION_EXPIRY_MAX_ATTEMPTS)
        self.assertEqual([(i.state, i.outcome) for i in stack.state.intents()], [("done", "unknown")])


# ---------------------------------------------------------------------------
# T-5518 r1 N1/N2: the deferred RequestHuman reply is sent exactly once
# ---------------------------------------------------------------------------


class DeferredReplySingleSendTests(_StackCase):
    def test_a_settled_waiter_never_fires_again_when_a_later_retry_succeeds(self) -> None:
        stack = self.stack
        stack.presenter.mode = "down"
        review = stack.open_review()
        settled = self.request_and_deliver(review)
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0][0][1], "pending")
        stack.presenter.mode = "ok"
        for _ in range(40):
            self.tick()
        self.assertEqual(self.record(review).state, "presented")
        self.assertEqual(len(stack.presenter.calls), 2)
        self.assertEqual(len(settled), 1)  # exactly once, the first settle

    def test_a_scheduler_failure_cannot_double_reply(self) -> None:
        stack = self.stack

        class Raising:
            def call_later(self, delay_seconds, callback):
                raise RuntimeError("no timer")

        stack.service._delivery._scheduler = Raising()
        stack.presenter.mode = "down"
        review = stack.open_review()
        settled = self.request_and_deliver(review)  # must not raise
        self.assertEqual(len(settled), 1)
        self.assertEqual(stack.service._delivery.armed(), 0)
        # load failure path: the reply is an error, still exactly once
        original = stack.service._delivery._load

        def broken(handle):
            raise RuntimeError("journal busy")

        stack.service._delivery._load = broken
        second: list = []
        stack.service.deliver_human_request(
            self.record(review).request_handle, lambda o, e: second.append((o, e)))
        self.assertEqual(len(second), 1)
        self.assertIsNone(second[0][0])
        stack.service._delivery._load = original

    def test_request_human_handler_replies_once_when_the_scheduler_fails(self) -> None:
        """The same over the real D-Bus handler (dbus-python absent: the
        handler class does not exist, so only the delivery-level proof
        above applies)."""

        from tools.host_control.approval import dbus_service as approval_dbus

        if not approval_dbus.DBUS_AVAILABLE:
            return  # the delivery-level tests above carry the property
        stack = self.stack

        class Raising:
            def call_later(self, delay_seconds, callback):
                raise RuntimeError("no timer")

        stack.service._delivery._scheduler = Raising()
        stack.presenter.mode = "down"
        review = stack.open_review()
        replies: list = []
        handler = approval_dbus.HostApproval1Object.RequestHuman

        class Instance:
            _service = stack.service

            def _approver_subject(self, sender):
                return "specialist"

            _mapped_errors = approval_dbus.HostApproval1Object._mapped_errors

        handler(Instance(), review.review_handle, review.plan_digest, "rh-x",
                sender=":1.1",
                reply_cb=lambda *v: replies.append(("reply", v)),
                error_cb=lambda e: replies.append(("error", e)))
        self.assertEqual(len(replies), 1, replies)
        self.assertEqual(replies[0][0], "reply")


# ---------------------------------------------------------------------------
# concurrency: one nonce, one decision (the reviewer's race probe as a test)
# ---------------------------------------------------------------------------


class SingleUseUnderConcurrencyTests(unittest.TestCase):
    def test_parallel_human_decisions_on_one_nonce_decide_exactly_once(self) -> None:
        tmp = tempfile.mkdtemp(prefix="t5518-race-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp) / "stack"
        stack = build_human_stack(root)
        self.addCleanup(stack.close)
        review = stack.open_review()
        stack.request(review)
        record = stack.journal.get_human_request_for_review(review.review_handle)
        self.assertEqual(record.state, "presented")
        workers = 8
        barrier = threading.Barrier(workers)
        out: list = []
        lock = threading.Lock()

        def go(index: int) -> None:
            journal = ApprovalJournal.open(root / "approval.sqlite3")
            try:
                service = ApprovalService(
                    journal=journal,
                    metadata_source=stack.metadata,
                    clock=stack.clock,
                    authorizer=PolicyApproverAuthorizer(parse_approver_policy(human_policy_raw())),
                    handle_factory=lambda: secrets.token_hex(32),
                    registered_consumer_uids=frozenset((CONSUMER_UID,)),
                    human_ingress=registration(),
                )
                decider = InProcessDecider(service, INGRESS_UID)
                barrier.wait()
                try:
                    state, _att = decider.human_decision(
                        review.review_handle, review.plan_digest,
                        "granted" if index % 2 else "denied",
                        f"src-{index}", record.nonce, f"key-{index}")
                    verdict = ("ok", state)
                except (DecisionRefused, HostControlError) as exc:
                    verdict = ("refused", str(exc)[:40])
            finally:
                journal.close()
            with lock:
                out.append(verdict)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(out), workers)
        self.assertEqual(sum(1 for verdict in out if verdict[0] == "ok"), 1, out)
        rows = stack.journal._require_open().execute(
            "SELECT count(*) FROM provider_attestations").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(
            stack.journal.get_human_request_for_review(review.review_handle).state, "decided"
        )


if __name__ == "__main__":
    unittest.main()
