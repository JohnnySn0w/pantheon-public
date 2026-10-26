"""D-Bus surface of the Telegram ingress (ORG-052 §5, T-2919).

Exports ``org.pantheon.HostApprovalTelegram1.Ingress.Present`` — ONE
method, ``"sssss" -> "ss"``, no signals, no properties — callable only
by the configured approval-service uid (rechecked here against the live
bus-derived uid; the staged bus policy is defence in depth). Also holds
the client of ``org.pantheon.HostApproval1.HumanIngress.HumanDecision``.

Runner shape: ONE thread. A GLib main loop serves ``Present`` and a GLib
timeout drives short ``getUpdates`` polls (bounded by the config's
``poll_timeout_seconds`` <= 5), so no state is shared across threads.

Cross-service calls never block that loop (T-5518): ``HumanDecision`` is
sent with dbus-python ``reply_handler``/``error_handler`` and its answer
is handled later on the same thread, so ``Present`` stays servable while
a decision is in flight; the approval service's ``Present`` client is
asynchronous too, so the two services can never stall on each other.

What CAN still block this loop, deliberately (T-5518 DECISION 3): the
Bot API calls, which stay on the loop thread so the private SQLite state
and the API client are only ever touched by one thread (no locks):

- one ``getUpdates`` long poll per tick, at most ``poll_timeout_seconds``
  (<= 5 s) plus the client's 10 s transport slack;
- each ``sendMessage`` / ``editMessageText`` / ``answerCallbackQuery``,
  at most 10 s apiece (one ``sendMessage`` inside a ``Present``; an
  answer and an edit per processed callback).

While the loop is blocked there, an incoming ``Present`` waits in the bus
queue and may time out on the approval side. Recovery from that is the
approval service's own bounded ``Present`` retry plus the idempotent
replay here (an identical ``Present`` for an open or decision_pending
card returns the existing delivery; never a second card).
"""

from __future__ import annotations

from typing import Callable

from tools.host_control.telegram_ingress import wire
from tools.host_control.telegram_ingress.bot_api import BotApiError
from tools.host_control.telegram_ingress.core import (
    DecisionRefused,
    DecisionUnavailable,
    IngressCore,
    PresentRefused,
)

try:  # pragma: no cover - exercised implicitly on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False

HUMAN_DECISION_TIMEOUT_SECONDS = 15.0
_PRESENT_ERRORS = {
    "invalid": (wire.INGRESS_INVALID_ERROR, "invalid"),
    "conflict": (wire.INGRESS_CONFLICT_ERROR, "conflict"),
    "unavailable": (wire.INGRESS_UNAVAILABLE_ERROR, "unavailable"),
}


class DbusHumanDecider:
    """Non-blocking ``HumanDecider`` over a dbus-python connection (the
    connection must be attached to the GLib main loop). ``on_done`` is
    called exactly once, on the loop thread."""

    def __init__(
        self,
        connection_factory: Callable[[], object],
        *,
        timeout_seconds: float = HUMAN_DECISION_TIMEOUT_SECONDS,
    ) -> None:
        self._connection_factory = connection_factory
        self._timeout = float(timeout_seconds)

    def begin_human_decision(
        self,
        review_handle: str,
        expected_digest: str,
        decision: str,
        source_event_handle: str,
        callback_nonce: str,
        idempotency_key: str,
        on_done: Callable[[tuple[str, str] | None, BaseException | None], None],
    ) -> None:
        fired = [False]

        def done(result, error) -> None:
            if not fired[0]:
                fired[0] = True
                on_done(result, error)

        def on_reply(state: object, attestation: object) -> None:
            done((str(state), str(attestation)), None)

        def on_error(error: BaseException) -> None:
            done(None, _decision_error(error))

        try:
            connection = self._connection_factory()
            proxy = connection.get_object(  # type: ignore[attr-defined]
                wire.APPROVAL_BUS_NAME,
                wire.APPROVAL_OBJECT_PATH,
                introspect=False,
            )
            method = proxy.get_dbus_method(
                "HumanDecision", wire.HUMAN_INGRESS_INTERFACE
            )
            method(
                review_handle,
                expected_digest,
                decision,
                source_event_handle,
                callback_nonce,
                idempotency_key,
                signature="ssssss",
                timeout=self._timeout,
                reply_handler=on_reply,
                error_handler=on_error,
            )
        except Exception:
            done(None, DecisionUnavailable())


def _decision_error(exc: BaseException) -> Exception:
    """Closed mapping: an approval-namespace error is a definitive refusal
    (bounded body); anything else (timeout, no owner) is unavailable."""

    name = ""
    body = ""
    if DBUS_AVAILABLE and isinstance(exc, dbus.exceptions.DBusException):
        name = exc.get_dbus_name() or ""
        body = (exc.get_dbus_message() or "")[:64]
    if name.startswith(wire.APPROVAL_ERROR_NAMESPACE + "."):
        return DecisionRefused(body or name.rsplit(".", 1)[-1])
    return DecisionUnavailable()


if DBUS_AVAILABLE:

    class IngressObject(dbus.service.Object):
        def __init__(
            self,
            connection: "dbus.bus.BusConnection",
            *,
            core: IngressCore,
            approval_uid: int,
        ) -> None:
            super().__init__(connection, wire.INGRESS_OBJECT_PATH)
            self._core = core
            self._approval_uid = int(approval_uid)

        def _approval_caller(self, sender: str | None) -> None:
            denied = dbus.exceptions.DBusException(
                "subject_denied", name=wire.INGRESS_POLICY_DENIED_ERROR
            )
            if not sender:
                raise denied
            try:
                uid = int(self.connection.get_unix_user(sender))
            except Exception:
                raise denied from None
            if uid != self._approval_uid:
                raise denied

        @dbus.service.method(
            wire.INGRESS_INTERFACE,
            in_signature="sssss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def Present(
            self,
            review_handle: str,
            expected_digest: str,
            summary_json: str,
            callback_nonce: str,
            expires_at: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            self._approval_caller(sender)
            try:
                return self._core.present(
                    str(review_handle),
                    str(expected_digest),
                    str(summary_json),
                    str(callback_nonce),
                    str(expires_at),
                )
            except PresentRefused as exc:
                name, body = _PRESENT_ERRORS.get(
                    exc.kind, _PRESENT_ERRORS["unavailable"]
                )
                raise dbus.exceptions.DBusException(body, name=name) from None
            except Exception:
                raise dbus.exceptions.DBusException(
                    "unavailable", name=wire.INGRESS_UNAVAILABLE_ERROR
                ) from None


def run_ingress(
    core: IngressCore,
    connection: object,
    *,
    approval_uid: int,
    poll_timeout_seconds: int,
    poll_interval_ms: int = 250,
    on_ready: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Export ``Present``, own the well-known name, recover pending
    intents, then poll until stopped. Returns a process exit code."""

    if not DBUS_AVAILABLE:  # pragma: no cover
        return 69
    from gi.repository import GLib

    obj = IngressObject(connection, core=core, approval_uid=approval_uid)
    name = dbus.service.BusName(  # noqa: F841 - held for the loop's life
        wire.INGRESS_BUS_NAME, bus=connection, do_not_queue=True
    )
    core.recover()
    core.log.event("started")
    loop = GLib.MainLoop()
    exit_code = {"value": 0}

    def tick() -> bool:
        if should_stop is not None and should_stop():
            loop.quit()
            return False
        try:
            core.expire_cards()
            core.poll_once(timeout=poll_timeout_seconds)
            core.recover()
        except BotApiError as exc:
            if exc.code == "auth":
                # fail closed; the supervisor restarts us and the restart
                # re-leases the credential once
                exit_code["value"] = 75
                loop.quit()
                return False
        except Exception:
            exit_code["value"] = 70
            loop.quit()
            return False
        return True

    GLib.timeout_add(poll_interval_ms, tick)
    if on_ready is not None:
        on_ready()
    loop.run()
    del obj
    return exit_code["value"]
