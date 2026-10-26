"""Ingress core logic: present one card, validate provider callbacks,
submit one human decision (ORG-052 §5, T-2919).

Transport-free: the Bot API client, the ``HumanDecision`` caller, the
clock, and the log sink are injected. The D-Bus runner
(``dbus_service.py``) and the entry point (``main.py``) wire the real
ones; tests wire the loopback fake Bot API and recording deciders.

Callback acceptance (gate 14) is an ORDERED, table-driven check list
(``CALLBACK_CHECKS``): the first failing check names the refusal. The
table is data so the mutation suite can neuter exactly one check and
prove the matching spoof then gets through (each check is load-bearing).
Defense in depth: the approval service independently re-checks the
nonce, the expiry, the review state, and the caller uid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, TextIO

from tools.host_control.telegram_ingress import card as card_mod
from tools.host_control.telegram_ingress.bot_api import BotApiClient, BotApiError
from tools.host_control.telegram_ingress.state import Card, IngressState, Intent

LOG_EVENTS = frozenset(
    (
        "started",
        "present_sent",
        "present_replayed",
        "present_refused",
        "present_failed",
        "non_callback_ignored",
        "callback_refused",
        "callback_accepted",
        "decision_recorded",
        "decision_refused",
        "decision_unavailable",
        "decision_outcome_mismatch",
        "recovered",
        "api_error",
    )
)


class IngressLog:
    """Bounded event log. Structure is the redaction: the only fields are
    a closed event code, an 8-hex review prefix, and a short closed
    detail. Free text, payloads, callback data, card text, ids, and
    tokens have no slot."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def event(
        self,
        event: str,
        *,
        review: str | None = None,
        detail: str | None = None,
    ) -> None:
        if event not in LOG_EVENTS:
            event = "unknown"
        parts = [f"telegram-ingress event={event}"]
        if review:
            parts.append(f"review={review[:8]}")
        if detail:
            parts.append(f"detail={str(detail)[:64]}".replace("\n", " "))
        self._stream.write(" ".join(parts) + "\n")
        self._stream.flush()


class DecisionRefused(Exception):
    """The approval service definitively refused (closed reason body)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason[:64]
        super().__init__(self.reason)


class DecisionUnavailable(Exception):
    """The approval service could not be reached; retry the SAME intent."""


# Approval-service refusals that mean "not yet", not "never": the intent
# stays pending and is retried with the SAME idempotency key.
RETRYABLE_REFUSALS = frozenset(("human_request_not_presented",))

# T-5518: retry of a pending intent (decision_unavailable or a retryable
# refusal) backs off exponentially to a cap and stops at card expiry; the
# retry is logged only on attempts 1, 2, 4, 8, ... (log2-bounded).
DECISION_RETRY_INITIAL_SECONDS = 1.0
DECISION_RETRY_FACTOR = 2.0
DECISION_RETRY_CAP_SECONDS = 30.0
# Past card expiry (T-5518 r1 B1): one final idempotent call, then retries
# only while the service is unreachable, bounded by both of these. 120 s
# covers the 15 s HumanDecision timeout four times over plus the 1-8 s
# backoff steps between the 5 calls; an approval-service restart fits in
# it. Exhausted -> the intent ends 'unknown', never 'expired'.
DECISION_EXPIRY_GRACE_SECONDS = 120.0
DECISION_EXPIRY_MAX_ATTEMPTS = 5


def decision_retry_delay(failures: int) -> float:
    """Backoff after the ``failures``-th consecutive retryable failure."""

    exponent = min(32, max(0, int(failures) - 1))  # no float overflow
    return min(
        DECISION_RETRY_CAP_SECONDS,
        DECISION_RETRY_INITIAL_SECONDS * DECISION_RETRY_FACTOR**exponent,
    )


def _log_attempt(failures: int) -> bool:
    return failures >= 1 and failures & (failures - 1) == 0


class HumanDecider(Protocol):
    """Synchronous ``HumanDecision`` seam (in-process composition).

    A decider MAY instead provide ``begin_human_decision(*same args,
    on_done)``, calling ``on_done(result, error)`` exactly once, later,
    on the loop thread (``result`` the ``(state, attestation)`` slots, or
    ``error`` a ``DecisionRefused``/``DecisionUnavailable``). The D-Bus
    client does, so a decision in flight never blocks the ingress loop."""

    def human_decision(
        self,
        review_handle: str,
        expected_digest: str,
        decision: str,
        source_event_handle: str,
        callback_nonce: str,
        idempotency_key: str,
    ) -> tuple[str, str]: ...


class PresentRefused(Exception):
    """A ``Present`` call outside the closed contract (or a conflict)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


@dataclass
class _Ctx:
    core: "IngressCore"
    update: dict
    cq: dict
    now: datetime
    card: Card | None = None
    code: str | None = None
    tag: str | None = None


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_shape(ctx: _Ctx) -> bool:
    cq = ctx.cq
    sender = cq.get("from")
    return (
        isinstance(cq.get("id"), str)
        and 1 <= len(cq["id"]) <= 128
        and isinstance(sender, dict)
        and _int(sender.get("id"))
        and isinstance(sender.get("is_bot"), bool)
        and isinstance(cq.get("chat_instance"), str)
    )


def _check_not_bot(ctx: _Ctx) -> bool:
    sender = ctx.cq["from"]
    return sender["is_bot"] is False and sender["id"] != ctx.core.bot_user_id


def _check_user(ctx: _Ctx) -> bool:
    return ctx.cq["from"]["id"] == ctx.core.allow_user_id


def _check_message(ctx: _Ctx) -> bool:
    message = ctx.cq.get("message")
    # InaccessibleMessage is told apart by date == 0 (Bot API); inline
    # callbacks carry no message at all
    return (
        "inline_message_id" not in ctx.cq
        and isinstance(message, dict)
        and _int(message.get("message_id"))
        and _int(message.get("date"))
        and message["date"] > 0
        and isinstance(message.get("chat"), dict)
        and _int(message["chat"].get("id"))
    )


def _check_chat(ctx: _Ctx) -> bool:
    # a PRIVATE chat with the one allowlisted human only (review finding
    # 6): a group/supergroup/channel never carries authority
    chat = ctx.cq["message"]["chat"]
    return chat["id"] == ctx.core.allow_chat_id and chat.get("type") == "private"


def _check_not_replayed(ctx: _Ctx) -> bool:
    return not ctx.core.state.seen(ctx.cq["id"])


def _check_known_message(ctx: _Ctx) -> bool:
    message = ctx.cq["message"]
    ctx.card = ctx.core.state.card_for_message(
        message["chat"]["id"], message["message_id"]
    )
    return ctx.card is not None


def _check_card_open(ctx: _Ctx) -> bool:
    return ctx.card is not None and ctx.card.state == "open"


def _check_unedited(ctx: _Ctx) -> bool:
    message = ctx.cq["message"]
    author = message.get("from")
    return (
        isinstance(author, dict)
        and author.get("id") == ctx.core.bot_user_id
        and author.get("is_bot") is True
        and "edit_date" not in message
        and "forward_origin" not in message
        and isinstance(message.get("text"), str)
        and card_mod.text_digest(message["text"]) == ctx.card.text_sha256
    )


def _check_tag(ctx: _Ctx) -> bool:
    parsed = card_mod.parse_callback_data(ctx.cq.get("data"))
    if parsed is None:
        return False
    ctx.code, ctx.tag = parsed
    return card_mod.tag_matches(
        ctx.card.review_handle, ctx.card.nonce, ctx.code, ctx.tag
    )


def _check_not_expired(ctx: _Ctx) -> bool:
    expiry = datetime.fromisoformat(ctx.card.expires_at)
    return ctx.now < expiry


CallbackCheck = tuple[str, Callable[[_Ctx], bool]]
CALLBACK_CHECKS: tuple[CallbackCheck, ...] = (
    ("malformed_callback", _check_shape),
    ("bot_sender", _check_not_bot),
    ("user_not_allowlisted", _check_user),
    ("no_message", _check_message),
    ("chat_not_allowlisted", _check_chat),
    ("callback_replay", _check_not_replayed),
    ("unknown_message", _check_known_message),
    ("card_closed", _check_card_open),
    ("edited_card", _check_unedited),
    ("callback_mismatch", _check_tag),
    ("card_expired", _check_not_expired),
)


def _delivery_matches(message: object, allow_chat_id: int) -> bool:
    """The sendMessage reply must echo the allowlisted chat, an integer
    message id, and non-empty text (the text the card digest is taken
    from). A card is only ever correlated to a message the allowlisted
    chat actually received."""

    return (
        isinstance(message, dict)
        and _int(message.get("message_id"))
        and isinstance(message.get("chat"), dict)
        and message["chat"].get("id") == allow_chat_id
        and isinstance(message.get("text"), str)
        and bool(message["text"])
    )


def idempotency_key_for(callback_id: str, review_handle: str) -> str:
    return hashlib.sha256(
        f"tg-idem/v1|{callback_id}|{review_handle}".encode()
    ).hexdigest()


def source_event_for(bot_user_id: int, update_id: int, callback_id: str) -> str:
    return hashlib.sha256(
        f"tg-src/v1|telegram|{bot_user_id}|{update_id}|{callback_id}".encode()
    ).hexdigest()


class IngressCore:
    def __init__(
        self,
        *,
        state: IngressState,
        api: BotApiClient,
        decider: HumanDecider,
        bot_user_id: int,
        allow_user_id: int,
        allow_chat_id: int,
        card_ttl_max_seconds: int,
        log: IngressLog,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        checks: tuple[CallbackCheck, ...] = CALLBACK_CHECKS,
    ) -> None:
        self.state = state
        self.api = api
        self.decider = decider
        self.bot_user_id = int(bot_user_id)
        self.allow_user_id = int(allow_user_id)
        self.allow_chat_id = int(allow_chat_id)
        self.card_ttl_max_seconds = int(card_ttl_max_seconds)
        self.log = log
        self.clock = clock
        self.checks = checks
        # T-5518 retry bookkeeping, in memory, loop-thread only: intents
        # with a HumanDecision in flight (never sent twice concurrently),
        # and per intent (consecutive retryable failures, next due time)
        self._in_flight: set[str] = set()
        self._retry: dict[str, tuple[int, datetime]] = {}
        # calls made for an intent after its card expired (bounded grace)
        self._after_expiry: dict[str, int] = {}

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    # -- Present ----------------------------------------------------------

    def present(
        self,
        review_handle: str,
        expected_digest: str,
        summary_json: str,
        callback_nonce: str,
        expires_at: str,
    ) -> tuple[str, str]:
        """Deliver exactly ONE card per review. Identical replay returns
        the existing delivery; a different nonce/digest for a known
        review is a conflict (never a second card)."""

        try:
            fields = card_mod.parse_present(
                review_handle,
                expected_digest,
                summary_json,
                callback_nonce,
                expires_at,
                now=self._now(),
                max_ttl_seconds=self.card_ttl_max_seconds,
            )
        except card_mod.CardError:
            self.log.event("present_refused", detail="invalid")
            raise PresentRefused("invalid") from None
        existing = self.state.card_for_review(fields["review_handle"])
        if existing is not None:
            if (
                existing.nonce != fields["nonce"]
                or existing.plan_digest != fields["plan_digest"]
            ):
                self.log.event(
                    "present_refused", review=review_handle, detail="conflict"
                )
                raise PresentRefused("conflict")
            if existing.state == "open" or (
                existing.state == "closed"
                and existing.closed_reason == "decision_pending"
            ):
                # an identical replay (e.g. the approval side's Present
                # call timed out AFTER this card was sent, then a tap
                # arrived): report the existing delivery so the service
                # reaches 'presented' and the pending decision completes
                # (T-2919 review finding 1). A decision already waiting
                # on that delivery is retried at once (T-5518): the
                # service records 'presented' when this reply lands.
                self._retry_now(fields["review_handle"])
                self.log.event("present_replayed", review=review_handle)
                return ("presented", str(existing.message_id))
            if existing.state == "closed":
                self.log.event(
                    "present_refused", review=review_handle, detail="closed"
                )
                raise PresentRefused("conflict")
            # 'sending' left by a crash: resend (an orphaned earlier card,
            # if any, has an unknown message id and is refused as
            # unknown_message — inert)
        text, markup = card_mod.render_card(fields)
        if existing is None:
            self.state.begin()
            try:
                self.state.insert_sending_card(
                    fields["review_handle"],
                    fields["nonce"],
                    fields["plan_digest"],
                    fields["expires_at"].isoformat(),
                )
                self.state.commit()
            except BaseException:
                self.state.rollback()
                raise
        try:
            message = self.api.send_message(
                chat_id=self.allow_chat_id, text=text, reply_markup=markup
            )
        except BotApiError as exc:
            self.log.event(
                "present_failed", review=review_handle, detail=exc.code
            )
            raise PresentRefused("unavailable") from None
        if not _delivery_matches(message, self.allow_chat_id):
            self.log.event("present_failed", review=review_handle, detail="shape")
            raise PresentRefused("unavailable")
        message_id = message["message_id"]
        returned_text = message["text"]
        self.state.begin()
        try:
            # the edited_card check compares against the text Telegram
            # RETURNED (it may normalize, e.g. trim whitespace), never the
            # locally rendered string
            self.state.mark_card_open(
                fields["review_handle"],
                self.allow_chat_id,
                message_id,
                card_mod.text_digest(returned_text),
            )
            self.state.commit()
        except BaseException:
            self.state.rollback()
            raise
        self.log.event("present_sent", review=review_handle)
        return ("presented", str(message_id))

    # -- updates ------------------------------------------------------------

    def poll_once(self, *, timeout: int) -> int:
        try:
            updates = self.api.get_updates(
                offset=self.state.cursor(), timeout=timeout
            )
        except BotApiError as exc:
            self.log.event("api_error", detail=self.api.last_error_detail or exc.code)
            raise
        handled = 0
        for update in updates:
            if isinstance(update, dict) and _int(update.get("update_id")):
                self.process_update(update)
                handled += 1
        return handled

    def process_update(self, update: dict) -> str:
        """Handle ONE update; return the verdict code. The cursor moves
        past it in the same local transaction that records the verdict."""

        update_id = update["update_id"]
        if update_id < self.state.cursor():
            return "stale_update"
        cq = update.get("callback_query")
        if not isinstance(cq, dict):
            # text, forwarded "approved" prose, edited messages, channel
            # posts: none can ever reach HumanDecision
            self._advance(update_id)
            self.log.event("non_callback_ignored")
            return "non_callback"
        ctx = _Ctx(core=self, update=update, cq=cq, now=self._now())
        for code, check in self.checks:
            try:
                ok = check(ctx)
            except Exception:
                ok = False
            if not ok:
                return self._refuse(ctx, code)
        return self._accept(ctx)

    def _advance(self, update_id: int) -> None:
        self.state.begin()
        try:
            self.state.set_cursor(update_id + 1)
            self.state.commit()
        except BaseException:
            self.state.rollback()
            raise

    def _answer(self, cq: dict, text: str) -> None:
        # the Bot API requires answering EVERY callback query
        callback_id = cq.get("id")
        if not isinstance(callback_id, str):
            return
        try:
            self.api.answer_callback_query(callback_query_id=callback_id, text=text)
        except BotApiError as exc:
            self.log.event("api_error", detail=self.api.last_error_detail or exc.code)

    def _refuse(self, ctx: _Ctx, code: str) -> str:
        self.state.begin()
        try:
            if isinstance(ctx.cq.get("id"), str) and code != "callback_replay":
                self.state.mark_seen(ctx.cq["id"], f"refused:{code}")
            self.state.set_cursor(ctx.update["update_id"] + 1)
            self.state.commit()
        except BaseException:
            self.state.rollback()
            raise
        self.log.event(
            "callback_refused",
            review=None if ctx.card is None else ctx.card.review_handle,
            detail=code,
        )
        self._answer(ctx.cq, "Not accepted.")
        return code

    def _accept(self, ctx: _Ctx) -> str:
        card = ctx.card
        decision = card_mod.DECISION_BY_CODE[ctx.code]
        intent = Intent(
            idempotency_key=idempotency_key_for(ctx.cq["id"], card.review_handle),
            review_handle=card.review_handle,
            decision=decision,
            source_event=source_event_for(
                self.bot_user_id, ctx.update["update_id"], ctx.cq["id"]
            ),
            callback_id=ctx.cq["id"],
            state="pending",
            outcome=None,
        )
        # durable intent BEFORE the call: a crash after this point replays
        # the SAME idempotency key (never a second decision)
        self.state.begin()
        try:
            self.state.insert_intent(intent)
            self.state.mark_seen(ctx.cq["id"], "accepted")
            self.state.close_card(card.review_handle, "decision_pending")
            self.state.set_cursor(ctx.update["update_id"] + 1)
            self.state.commit()
        except BaseException:
            self.state.rollback()
            raise
        self.log.event("callback_accepted", review=card.review_handle)
        # answer at TAP time (T-5518 r1 N4): Telegram expects a prompt
        # answer and may reject a late one; the outcome is shown by the
        # card edit when the decision settles
        self._answer(ctx.cq, "Received.")
        self._submit(intent, card)
        return "accepted"

    def _submit(
        self,
        intent: Intent,
        card: Card,
        *,
        on_outcome: Callable[[str], None] | None = None,
    ) -> str:
        """Send ONE HumanDecision for a pending intent. Returns the
        outcome when the decider answered synchronously, else
        ``"in_flight"`` (an async decider answers later, on the loop
        thread; or a call for this intent is already in flight: an
        intent is never sent twice at once)."""

        key = intent.idempotency_key
        if key in self._in_flight:
            return "in_flight"
        self._in_flight.add(key)
        settled: list[str] = []

        def done(
            result: tuple[str, str] | None,
            error: BaseException | None,
        ) -> None:
            if settled:
                return  # exactly once
            outcome = self._complete(intent, card, result, error)
            settled.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)

        args = (
            intent.review_handle,
            card.plan_digest,
            intent.decision,
            intent.source_event,
            card.nonce,
            intent.idempotency_key,
        )
        begin = getattr(self.decider, "begin_human_decision", None)
        try:
            if begin is None:
                try:
                    result = self.decider.human_decision(*args)
                except (DecisionRefused, DecisionUnavailable) as exc:
                    done(None, exc)
                else:
                    done(result, None)
            else:
                begin(*args, on_done=done)
        except BaseException:
            if not settled:
                self._in_flight.discard(key)
            raise
        return settled[0] if settled else "in_flight"

    def _complete(
        self,
        intent: Intent,
        card: Card,
        result: tuple[str, str] | None,
        error: BaseException | None,
    ) -> str:
        key = intent.idempotency_key
        self._in_flight.discard(key)
        if error is not None and (
            not isinstance(error, DecisionRefused)
            or error.reason in RETRYABLE_REFUSALS
        ):
            # 'not yet': the service is unreachable, or has not yet
            # recorded the delivery (its Present call timed out after the
            # send). The intent stays pending. The card is already CLOSED
            # locally (decision_pending, so no second tap is accepted);
            # only the Telegram message is left untouched. recover()
            # retries with backoff; the service's own Present retry (or a
            # replay) records the delivery (review finding 1, T-5518).
            failures = self._retry.get(key, (0, None))[0] + 1
            self._retry[key] = (
                failures,
                self._now() + timedelta(seconds=decision_retry_delay(failures)),
            )
            if _log_attempt(failures):
                self.log.event(
                    "decision_unavailable",
                    review=intent.review_handle,
                    detail=getattr(error, "reason", None),
                )
            return "unavailable"
        if error is not None:
            # the service's own verdict on an expired request is final
            outcome = (
                "expired"
                if error.reason == "human_request_expired"  # type: ignore[attr-defined]
                else "refused"
            )
            self.log.event(
                "decision_refused",
                review=intent.review_handle,
                detail=error.reason,  # type: ignore[attr-defined]
            )
        else:
            state = result[0] if result else ""
            outcome = state if state in ("granted", "denied") else "refused"
            self.log.event(
                "decision_recorded", review=intent.review_handle, detail=outcome
            )
        self._finish(intent, card, outcome)
        return outcome

    def _finish(self, intent: Intent, card: Card, outcome: str) -> bool:
        """Durably settle a pending intent ONCE. A late or duplicate
        completion for an intent already finished is a no-op: the
        guarded update matches no row, and the card is not re-closed."""

        self._retry.pop(intent.idempotency_key, None)
        self._after_expiry.pop(intent.idempotency_key, None)
        self.state.begin()
        try:
            finished = self.state.finish_intent(intent.idempotency_key, outcome)
            if finished:
                self.state.set_closed_reason(
                    intent.review_handle, f"decided:{outcome}"
                )
            self.state.commit()
        except BaseException:
            self.state.rollback()
            raise
        if finished:
            self._close_on_provider(card, outcome)
            return True
        recorded = self.state.intent(intent.idempotency_key)
        if recorded is not None and recorded.outcome != outcome:
            # a late answer disagrees with what was recorded: keep the
            # record (single-use) but make the disagreement visible
            # (closed outcome codes only; never a nonce)
            self.log.event(
                "decision_outcome_mismatch",
                review=intent.review_handle,
                detail=f"{recorded.outcome}->{outcome}",
            )
        return False

    def _retry_now(self, review_handle: str) -> None:
        now = self._now()
        for intent in self.state.pending_intents():
            if intent.review_handle != review_handle:
                continue
            retry = self._retry.get(intent.idempotency_key)
            if retry is not None:
                self._retry[intent.idempotency_key] = (retry[0], now)

    def _close_on_provider(self, card: Card, outcome: str) -> None:
        if card.chat_id is None or card.message_id is None:
            return
        try:
            self.api.edit_message_text(
                chat_id=card.chat_id,
                message_id=card.message_id,
                text=card_mod.closed_text(outcome),
                reply_markup=card_mod.EMPTY_MARKUP,
            )
        except BotApiError as exc:
            self.log.event("api_error", detail=self.api.last_error_detail or exc.code)

    # -- recovery / housekeeping ----------------------------------------------

    def recover(self) -> int:
        """Re-submit every DUE pending intent with its ORIGINAL
        idempotency key (the approval service returns the original
        outcome if the first submission committed). Returns how many
        settled synchronously; an async decider's answers land later.

        Bounded (T-5518): an intent in flight is skipped; one that failed
        retryably waits out its backoff.

        Expiry NEVER closes an intent locally (T-5518 r1 B1): the service
        checks the idempotency key BEFORE expiry, so a decision it
        committed before the card expired is returned by a post-expiry
        resubmit of the same key. Past expiry the intent therefore gets a
        final idempotent call (due at once), then bounded retries while
        the service is unreachable: at most
        ``DECISION_EXPIRY_MAX_ATTEMPTS`` calls within
        ``DECISION_EXPIRY_GRACE_SECONDS`` after expiry. What the service
        returns is final (``human_request_expired`` finishes as
        ``expired``); if the grace runs out unanswered, the intent ends as
        ``unknown`` and the card says so — never as ``expired``. The grace
        only ends after this process made at least one post-expiry call,
        so an ingress restarted long after expiry still asks once.

        ``unknown`` is final for this ingress (``finish_intent`` matches
        pending rows only): the approval journal is authoritative for what
        was decided, and the card tells the human to check it."""

        done = 0
        now = self._now()
        for intent in self.state.pending_intents():
            card = self.state.card_for_review(intent.review_handle)
            if card is None:
                continue
            key = intent.idempotency_key
            if key in self._in_flight:
                continue  # its answer is on the way; never close under it
            retry = self._retry.get(key)
            expires = datetime.fromisoformat(card.expires_at)
            if now >= expires:
                calls = self._after_expiry.get(key, 0)
                # give up only after THIS process asked the service at
                # least once past expiry (r2 B1: a restart long after
                # expiry must still make the final idempotent call)
                if calls >= DECISION_EXPIRY_MAX_ATTEMPTS or (
                    calls >= 1
                    and now >= expires + timedelta(seconds=DECISION_EXPIRY_GRACE_SECONDS)
                ):
                    if self._finish(intent, card, "unknown"):
                        self.log.event(
                            "decision_refused",
                            review=intent.review_handle,
                            detail="status_unknown",
                        )
                    continue
                # the first post-expiry call is due at once; later ones
                # keep the backoff
                if calls and retry is not None and retry[1] > now:
                    continue
                self._after_expiry[key] = calls + 1
            elif retry is not None and retry[1] > now:
                continue
            review = intent.review_handle

            def recovered(outcome: str, review: str = review) -> None:
                if outcome != "unavailable":
                    self.log.event("recovered", review=review)

            outcome = self._submit(intent, card, on_outcome=recovered)
            if outcome not in ("unavailable", "in_flight"):
                done += 1
        return done

    def expire_cards(self) -> int:
        now = self._now()
        closed = 0
        for card in self.state.open_cards():
            if datetime.fromisoformat(card.expires_at) <= now:
                self.state.begin()
                try:
                    self.state.close_card(card.review_handle, "expired")
                    self.state.commit()
                except BaseException:
                    self.state.rollback()
                    raise
                self._close_on_provider(card, "expired")
                closed += 1
        return closed
