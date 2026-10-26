"""Approval-side delivery of human cards, retried until settled
(ORG-052 §5 "Human authority ingress" and §6 durability; T-5518).

The approval service hands a pending human request to the separately
supervised Telegram ingress with ``HostApprovalTelegram1.Ingress.Present``.
That call is asynchronous (it never blocks the service's single loop
thread, so ``HumanDecision`` and the Reviewer methods stay servable while
a card is in flight), and its failure is not final: ``HumanCardDelivery``
retries it itself with a bounded exponential backoff until the request is
``presented``, ``expired``, ``withdrawn`` or ``decided``, or the ingress
answers with a permanent refusal. Recovery therefore never depends on an
agent replaying ``RequestHuman``.

Idempotency: every attempt re-sends the STORED record (its own nonce and
digest, never regenerated), and the ingress dedups by review handle, so a
retry after a lost reply reaches the same single card. At most ONE
``Present`` is in flight per request: a ``RequestHuman`` replay or a
``resume`` that arrives while an attempt is in flight joins it, and a
retry timer that is overtaken by a newer attempt is ignored when it fires
(per-request generation counter).

Known residue (T-5518 r1 N3): a request withdrawn (or expired) while its
``Present`` is in flight can still leave that card on Telegram, because
the ingress has already sent it. Authority is unaffected: a tap on it
reaches ``HumanDecision``, which refuses it ``human_request_closed`` (or
``human_request_expired``), and the ingress closes the card with that
outcome. The late reply is recorded as nothing (the pending -> presented
transition no longer applies). There is no close-card call on the
ratified ``Present``-only ingress interface, and adding one would change
it, so no proactive close is attempted.

Threading: none. Every entry point, every presenter reply and every timer
runs on the service's one loop thread (the journal connection is
thread-affine), so the bookkeeping below needs no lock. The nonce-bearing
record never leaves the service through a public method (gate 28): this
class only sees the private ``load`` / ``complete`` callables the service
constructs it with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

PRESENT_RETRY_INITIAL_SECONDS = 2.0
PRESENT_RETRY_FACTOR = 2.0
PRESENT_RETRY_CAP_SECONDS = 60.0


@dataclass(frozen=True)
class PresentReply:
    """One settled ``Present`` attempt.

    ``state``/``message_ref`` are the wire slots on success; a failure
    has ``state == ""`` and says whether a retry can help (``False`` only
    for the ingress's permanent ``Invalid``/``Conflict`` refusals)."""

    state: str
    message_ref: str
    retryable: bool = True

    @classmethod
    def failed(cls, *, retryable: bool = True) -> "PresentReply":
        return cls("", "", retryable)


@dataclass(frozen=True)
class PresentCall:
    """The five ``Present`` arguments, rendered from the stored record."""

    review_handle: str
    expected_digest: str
    summary_json: str
    callback_nonce: str
    expires_at: str

    def args(self) -> tuple[str, str, str, str, str]:
        return (
            self.review_handle,
            self.expected_digest,
            self.summary_json,
            self.callback_nonce,
            self.expires_at,
        )

    def __repr__(self) -> str:  # never render the nonce
        return f"PresentCall(review={self.review_handle[:8]})"


class AsyncHumanCardPresenter(Protocol):
    """Non-blocking ``Present`` seam. ``on_done`` is called exactly once,
    later on the loop thread (or synchronously if the call could not even
    be sent). The real implementation is
    ``tools.host_control.approval.telegram_presenter.DbusTelegramPresenter``."""

    def begin_present(
        self,
        review_handle: str,
        expected_digest: str,
        summary_json: str,
        callback_nonce: str,
        expires_at: str,
        on_done: Callable[[PresentReply], None],
    ) -> None: ...


class DeliveryScheduler(Protocol):
    """Run ``callback`` once, ``delay_seconds`` from now, on the loop
    thread (GLib ``timeout_add`` in production; a manual fake in tests)."""

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> None: ...


# what a settled delivery reports to a waiter: (request_handle, state) or
# the error that stopped the service from answering
Settled = Callable[[tuple[str, str] | None, BaseException | None], None]


@dataclass
class _Slot:
    generation: int = 0
    in_flight: bool = False
    armed: bool = False
    failures: int = 0
    attempts: int = 0
    waiters: list[Settled] = field(default_factory=list)


def retry_delay(failures: int) -> float:
    """Backoff after the ``failures``-th consecutive failed attempt."""

    exponent = min(32, max(0, int(failures) - 1))  # no float overflow
    return min(
        PRESENT_RETRY_CAP_SECONDS,
        PRESENT_RETRY_INITIAL_SECONDS * PRESENT_RETRY_FACTOR**exponent,
    )


class HumanCardDelivery:
    """Per-request ``Present`` attempts with bounded retry.

    ``load(handle)`` returns ``(state, call)``: the request's current state
    after lazy expiry, and the ``PresentCall`` when (and only when) it is
    still ``pending``. ``complete(handle, reply)`` records a successful
    reply (the guarded pending -> presented transition) and returns the
    request's ``(handle, state)`` afterwards; it is also called for a
    failure so the waiter sees the live state."""

    def __init__(
        self,
        *,
        presenter: AsyncHumanCardPresenter,
        scheduler: DeliveryScheduler,
        load: Callable[[str], tuple[str, PresentCall | None]],
        complete: Callable[[str, PresentReply], tuple[str, str]],
    ) -> None:
        self._presenter = presenter
        self._scheduler = scheduler
        self._load = load
        self._complete = complete
        self._slots: dict[str, _Slot] = {}

    # -- observation (tests, a future Health1 surface) -----------------

    def attempts(self, request_handle: str) -> int:
        slot = self._slots.get(request_handle)
        return 0 if slot is None else slot.attempts

    def in_flight(self) -> int:
        return sum(1 for slot in self._slots.values() if slot.in_flight)

    def armed(self) -> int:
        return sum(1 for slot in self._slots.values() if slot.armed)

    # -- entry points ----------------------------------------------------

    def deliver(self, request_handle: str, on_settled: Settled | None = None) -> None:
        """Start (or join) an attempt now. A RequestHuman for a pending
        request lands here; an armed retry is overtaken, never doubled."""

        slot = self._slots.setdefault(request_handle, _Slot())
        if on_settled is not None:
            slot.waiters.append(on_settled)
        if slot.in_flight:
            return
        self._attempt(request_handle, slot)

    def resume(self, request_handle: str) -> None:
        """Re-arm delivery after a restart. Idempotent: a request with an
        attempt in flight or a retry armed is left alone."""

        slot = self._slots.get(request_handle)
        if slot is not None and (slot.in_flight or slot.armed):
            return
        self.deliver(request_handle)

    # -- internals ---------------------------------------------------------

    def _attempt(self, handle: str, slot: _Slot) -> None:
        slot.generation += 1
        slot.armed = False
        try:
            state, call = self._load(handle)
        except Exception as exc:  # journal busy/unavailable: try later
            # arm BEFORE settling (r1 N2): nothing after a reply may raise
            # into the caller's reply path and send a second reply
            self._arm(handle, slot)
            self._settle(slot, None, exc)
            return
        if call is None:
            # presented / expired / withdrawn / decided: nothing to send
            self._settle(slot, (handle, state), None)
            self._forget(handle, slot)
            return
        slot.in_flight = True
        slot.attempts += 1
        generation = slot.generation
        fired = [False]

        def on_done(reply: PresentReply) -> None:
            if fired[0]:
                return
            fired[0] = True
            self._on_reply(handle, generation, reply)

        try:
            self._presenter.begin_present(*call.args(), on_done=on_done)
        except Exception:
            on_done(PresentReply.failed())

    def _on_reply(self, handle: str, generation: int, reply: PresentReply) -> None:
        slot = self._slots.get(handle)
        if slot is None or slot.generation != generation or not slot.in_flight:
            return
        slot.in_flight = False
        try:
            settled = self._complete(handle, reply)
        except Exception as exc:
            self._arm(handle, slot)
            self._settle(slot, None, exc)
            return
        if settled[1] == "pending" and reply.retryable:
            self._arm(handle, slot)
        else:
            self._forget(handle, slot)
        self._settle(slot, settled, None)

    def _arm(self, handle: str, slot: _Slot) -> None:
        slot.failures += 1
        slot.armed = True
        generation = slot.generation

        def fire() -> None:
            current = self._slots.get(handle)
            if (
                current is None
                or current.generation != generation
                or current.in_flight
                or not current.armed
            ):
                return  # overtaken by a newer attempt
            self._attempt(handle, current)

        try:
            self._scheduler.call_later(retry_delay(slot.failures), fire)
        except Exception:
            # no timer could be armed: the request stays pending and is
            # re-armed by the next RequestHuman or resume (never raises
            # into a reply path)
            slot.armed = False

    @staticmethod
    def _forget(handle: str, slot: _Slot) -> None:
        # settled: no timer, no attempt; the slot stays (bounded: one per
        # human request) so attempt counts remain observable
        del handle
        slot.armed = False
        slot.failures = 0

    @staticmethod
    def _settle(
        slot: _Slot,
        settled: tuple[str, str] | None,
        error: BaseException | None,
    ) -> None:
        waiters, slot.waiters = slot.waiters, []
        for waiter in waiters:
            try:
                waiter(settled, error)
            except Exception:  # a failed reply must not stop delivery
                pass
