"""Approval-side D-Bus client of ``HostApprovalTelegram1.Ingress.Present``
(ORG-052 §5, T-2919; non-blocking since T-5518).

Implements the service's ``AsyncHumanCardPresenter`` seam: one fixed
method, fixed signature ``"sssss" -> "ss"``, bounded timeout. The approval
service never holds the bot credential and never talks to Telegram; it
only hands the separately supervised ingress the bounded card fields
(rendered from the pending record and nothing else) plus the single-use
callback nonce, over a unicast private call that bus policy restricts to
the approval uid (staged ``org.pantheon.HostApprovalTelegram1.conf``).

The call is ASYNCHRONOUS (dbus-python ``reply_handler``/``error_handler``
on the service's GLib main loop): it never blocks the loop, so the
service keeps serving ``HumanDecision`` and the Reviewer methods while a
card is in flight. The two services can therefore never stall on each
other (T-5518; the T-2919 review finding 1(a)). The connection must be
attached to the GLib main loop (``DBusGMainLoop(set_as_default=True)``).

Every failure is reported as "not delivered" (fail closed: the record
stays pending) and is retryable, except the ingress's permanent
``Invalid``/``Conflict`` refusals; ``HumanCardDelivery`` owns the retry.
No exception text is logged here (the arguments carry the nonce).
"""

from __future__ import annotations

from typing import Callable

from tools.host_control.approval import wire
from tools.host_control.approval.presentation import PresentReply

PRESENT_TIMEOUT_SECONDS = 10.0
_PERMANENT_INGRESS_ERRORS = frozenset(
    (
        f"{wire.TELEGRAM_INGRESS_ERROR_NAMESPACE}.Invalid",
        f"{wire.TELEGRAM_INGRESS_ERROR_NAMESPACE}.Conflict",
    )
)


def _retryable(error: BaseException) -> bool:
    get_name = getattr(error, "get_dbus_name", None)
    name = get_name() if callable(get_name) else None
    return name not in _PERMANENT_INGRESS_ERRORS


class DbusTelegramPresenter:
    """``AsyncHumanCardPresenter`` over a dbus-python connection.

    ``connection_factory`` returns a connected bus (the system bus in
    production; a private bus in tests). It is called lazily per attempt
    so an ingress that starts after the approval service is reached
    without a restart (gate 6 posture)."""

    def __init__(
        self,
        connection_factory: Callable[[], object],
        *,
        timeout_seconds: float = PRESENT_TIMEOUT_SECONDS,
    ) -> None:
        self._connection_factory = connection_factory
        self._timeout = float(timeout_seconds)

    def begin_present(
        self,
        review_handle: str,
        expected_digest: str,
        summary_json: str,
        callback_nonce: str,
        expires_at: str,
        on_done: Callable[[PresentReply], None],
    ) -> None:
        fired = [False]

        def done(reply: PresentReply) -> None:
            if not fired[0]:
                fired[0] = True
                on_done(reply)

        def on_reply(state: object, message_ref: object) -> None:
            done(PresentReply(str(state), str(message_ref)))

        def on_error(error: BaseException) -> None:
            done(PresentReply.failed(retryable=_retryable(error)))

        try:
            connection = self._connection_factory()
            proxy = connection.get_object(  # type: ignore[attr-defined]
                wire.TELEGRAM_INGRESS_BUS_NAME,
                wire.TELEGRAM_INGRESS_OBJECT_PATH,
                introspect=False,
            )
            method = proxy.get_dbus_method(
                "Present", wire.TELEGRAM_INGRESS_INTERFACE
            )
            method(
                review_handle,
                expected_digest,
                summary_json,
                callback_nonce,
                expires_at,
                signature="sssss",
                timeout=self._timeout,
                reply_handler=on_reply,
                error_handler=on_error,
            )
        except Exception:
            # could not even send (no bus, no main loop): not delivered
            done(PresentReply.failed())


class GLibScheduler:
    """``DeliveryScheduler`` on the default GLib main context (the same
    loop that serves the service), so retries run on the loop thread."""

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> None:
        from gi.repository import GLib

        def fire() -> bool:
            callback()
            return False  # one shot

        GLib.timeout_add(max(0, int(delay_seconds * 1000)), fire)
