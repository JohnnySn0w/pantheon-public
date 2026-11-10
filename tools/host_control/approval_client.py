"""Broker-side D-Bus client of ``org.pantheon.HostApproval1.Consumer``
(ORG-052 Unit 5a, T-2917; the T-2913 OQ6 deferral).

Provides the real implementations of the two broker seams that were
fail-closed placeholders until now:

* ``ApprovalBinder`` (``operations.py``) via ``ValidateAndBind``
  (``"ssss" -> "ss"``), and
* ``ApprovalStatusSource`` (``dbus_service.py``) via ``ApprovalStatus``
  (``"s" -> "ss"``).

Gate 18 broker half: this module does NOT import the approval package.
The handful of approval-side wire names and closed codes it needs are
TRANSCRIBED below; a test pins each against the approval package so drift
fails loudly. The broker can therefore still never write approval state.

Subject binding for ``ApprovalGet`` (ADR section 4: "handle bound to
subject"; gate 15). HostApproval1's ``ApprovalStatus`` is broker-uid-only
and does not know the requesting subject, but since T-4875 (schema v2) its
payload carries the grant's ``plan_handle`` and ``plan_owner``. The broker
answers only when ALL of these hold, and otherwise raises the same
``UnknownHandle("unknown approval handle")`` as for a nonexistent handle:

1. the handle is 64 lowercase hex;
2. one of the caller's OWN broker operations carries the handle (cheap
   local pre-check, so a stranger never reaches the approval service);
3. the status payload is schema v2, and its ``plan_owner`` is the caller's
   own seat;
4. one of the caller's own operations carries the handle AND is an
   operation on the grant's ``plan_handle``.

Attaching a foreign handle to one's own throwaway Apply (the T-2917 5a
review S1 / P10 self-attach shape) fails 3 and 4: the grant is bound to
another seat's plan. A v1 or otherwise unbound payload fails closed with
``HostControlError``. The pre-Apply status gap (a granted handle not yet
carried by any operation) is unchanged and bubbled to the coordinator.

Outcome mapping is fail-closed and deliberately asymmetric (T-2911
override 2 / ORG-052 section 5):

* a closed ``BindDenied`` reason -> ``BindOutcome(denied)`` -> the broker
  terminates the operation ``refused``;
* an unknown/vanished plan (``UnknownHandle``) -> raised, the operation
  stays PREPARED-retryable and no grant is burned;
* outage / transport faults / ``MetadataUnavailable`` ->
  ``ApprovalUnavailable`` (fail closed for new and unbound work);
* anything else -> ``HostControlError`` (never a silent grant).
"""

from __future__ import annotations

import json
import re
from typing import Callable, Final, Protocol

from tools.host_control.contracts import (
    ApprovalUnavailable,
    BindOutcome,
    HostControlError,
    SubjectContext,
    UnknownHandle,
)

# --- transcribed from tools.host_control.approval.wire / .contracts -----
APPROVAL_BUS_NAME: Final = "org.pantheon.HostApproval1"
APPROVAL_OBJECT_PATH: Final = "/org/pantheon/HostApproval1"
CONSUMER_INTERFACE: Final = "org.pantheon.HostApproval1.Consumer"
_APPROVAL_ERROR_NAMESPACE: Final = "org.pantheon.HostApproval1.Error"
APPROVAL_UNKNOWN_HANDLE_ERROR: Final = f"{_APPROVAL_ERROR_NAMESPACE}.UnknownHandle"
APPROVAL_BIND_DENIED_ERROR: Final = f"{_APPROVAL_ERROR_NAMESPACE}.BindDenied"
APPROVAL_METADATA_UNAVAILABLE_ERROR: Final = (
    f"{_APPROVAL_ERROR_NAMESPACE}.MetadataUnavailable"
)
APPROVAL_STATUS_SCHEMA: Final = "pantheon-approval-status/v2"
GRANT_STATES: Final = frozenset(("issued", "bound", "revoked", "expired"))
BIND_DENIAL_REASON_CODES: Final = frozenset(
    (
        "unknown_approval",
        "plan_mismatch",
        "digest_mismatch",
        "metadata_digest_mismatch",
        "owner_mismatch",
        "performer_mismatch",
        "target_mismatch",
        "family_mismatch",
        "effect_mismatch",
        "manifest_revision_mismatch",
        "policy_revision_mismatch",
        "handoff_mismatch",
        "expired",
        "revoked",
        "already_bound",
        "operation_consumed",
    )
)
# ------------------------------------------------------------------------

# Bus/transport failures that mean "the authority is not reachable now".
TRANSPORT_UNAVAILABLE_ERRORS: Final = frozenset(
    (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.Timeout",
        "org.freedesktop.DBus.Error.TimedOut",
        "org.freedesktop.DBus.Error.NoServer",
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.LimitsExceeded",
    )
)

_HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
_STATUS_JSON_LIMIT = 65536
DEFAULT_CALL_TIMEOUT_SECONDS: Final = 10.0


class ApprovalHandleOwnership(Protocol):
    """Read-only seam onto the broker journal (``BrokerJournal`` satisfies
    it): does one of this subject's own operations carry the handle, and
    is such an operation on the grant's plan?"""

    def subject_references_approval(
        self,
        *,
        subject: SubjectContext,
        approval_handle: str,
    ) -> bool: ...

    def subject_operation_binds_plan(
        self,
        *,
        subject: SubjectContext,
        approval_handle: str,
        plan_handle: str,
    ) -> bool: ...


class ConsumerTransport(Protocol):
    """One fixed-signature blocking call onto HostApproval1.Consumer."""

    def call(self, method: str, signature: str, args: tuple[str, ...]) -> tuple[str, str]: ...


class ConsumerCallError(Exception):
    """A transport-tier failure carrying the D-Bus error name, bounded."""

    def __init__(self, name: str, body: str) -> None:
        super().__init__(name)
        self.name = str(name)[:255]
        self.body = str(body)[:200]


# ---------------------------------------------------------------------------
# pure, dbus-free mapping (fully unit-testable)
# ---------------------------------------------------------------------------


def bind_outcome_from_reply(reply: object) -> BindOutcome:
    if (
        not isinstance(reply, tuple)
        or len(reply) != 2
        or reply[0] != "bound"
        or not isinstance(reply[1], str)
        or _HEX_256.fullmatch(reply[1]) is None
    ):
        raise HostControlError("approval consumer returned a malformed bind reply")
    return BindOutcome(state="bound", authority_record_id=reply[1], reason_code="bound")


def bind_outcome_from_error(error: ConsumerCallError) -> BindOutcome:
    """Map a ValidateAndBind error. Returns a denial OUTCOME only for the
    closed BindDenied reasons; every other case RAISES (never a grant)."""

    if error.name == APPROVAL_BIND_DENIED_ERROR:
        if error.body in BIND_DENIAL_REASON_CODES:
            return BindOutcome(state="denied", authority_record_id=None, reason_code=error.body)
        raise HostControlError("approval consumer returned an unknown denial reason")
    raise _mapped_exception(error)


def _mapped_exception(error: ConsumerCallError) -> HostControlError:
    if error.name == APPROVAL_UNKNOWN_HANDLE_ERROR:
        return UnknownHandle("approval authority reports an unknown handle")
    if (
        error.name == APPROVAL_METADATA_UNAVAILABLE_ERROR
        or error.name in TRANSPORT_UNAVAILABLE_ERRORS
    ):
        return ApprovalUnavailable("approval authority is not available")
    return HostControlError("approval consumer call failed")


def status_from_reply(approval_handle: str, reply: object) -> tuple[str, str]:
    if not isinstance(reply, tuple) or len(reply) != 2:
        raise HostControlError("approval consumer returned a malformed status reply")
    state, payload = reply
    if state not in GRANT_STATES or not isinstance(payload, str):
        raise HostControlError("approval consumer returned an invalid status")
    if len(payload.encode("utf-8")) > _STATUS_JSON_LIMIT:
        raise HostControlError("approval status payload exceeds size limit")
    try:
        decoded = json.loads(payload)
    except ValueError as exc:
        raise HostControlError("approval status payload is not JSON") from exc
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != APPROVAL_STATUS_SCHEMA
        or decoded.get("approval_handle") != approval_handle
        or decoded.get("state") != state
    ):
        raise HostControlError("approval status payload does not match the request")
    return (state, payload)


def _binding_from_payload(payload: str) -> tuple[str, str]:
    """``(plan_handle, plan_owner)`` of an already-validated v2 payload;
    fail closed when either is missing or malformed."""

    decoded = json.loads(payload)
    plan_handle = decoded.get("plan_handle")
    plan_owner = decoded.get("plan_owner")
    if (
        not isinstance(plan_handle, str)
        or _HEX_256.fullmatch(plan_handle) is None
        or not isinstance(plan_owner, str)
        or not plan_owner
    ):
        raise HostControlError("approval status payload carries no plan binding")
    return (plan_handle, plan_owner)


# ---------------------------------------------------------------------------
# the client (implements ApprovalBinder and ApprovalStatusSource)
# ---------------------------------------------------------------------------


class ApprovalConsumerClient:
    def __init__(
        self,
        *,
        transport: ConsumerTransport,
        ownership: ApprovalHandleOwnership,
    ) -> None:
        self._transport = transport
        self._ownership = ownership

    # ApprovalBinder
    def validate_and_bind(
        self,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome:
        if not isinstance(approval_handle, str) or _HEX_256.fullmatch(approval_handle) is None:
            # Can never bind: HostApproval1 handles are 64 lowercase hex.
            # A closed denial, so the operation terminates refused instead
            # of retrying forever (no grant exists to burn).
            return BindOutcome(state="denied", authority_record_id=None, reason_code="unknown_approval")
        try:
            reply = self._transport.call(
                "ValidateAndBind",
                "ssss",
                (approval_handle, plan_handle, expected_digest, operation_handle),
            )
        except ConsumerCallError as error:
            return bind_outcome_from_error(error)
        return bind_outcome_from_reply(reply)

    # ApprovalStatusSource
    def _status_precheck(self, subject: SubjectContext, approval_handle: str) -> None:
        if (
            not isinstance(approval_handle, str)
            or _HEX_256.fullmatch(approval_handle) is None
            or not self._ownership.subject_references_approval(
                subject=subject,
                approval_handle=approval_handle,
            )
        ):
            # Indistinguishable from a nonexistent handle (gate 15).
            raise UnknownHandle("unknown approval handle")

    def _status_finish(
        self,
        subject: SubjectContext,
        approval_handle: str,
        reply: object,
    ) -> tuple[str, str]:
        state, payload = status_from_reply(approval_handle, reply)
        plan_handle, plan_owner = _binding_from_payload(payload)
        if plan_owner != subject.seat_id or not self._ownership.subject_operation_binds_plan(
            subject=subject,
            approval_handle=approval_handle,
            plan_handle=plan_handle,
        ):
            # T-4875: the grant is not bound to the caller's own plan (e.g.
            # a foreign handle attached to a throwaway Apply). Same text as
            # the nonexistent-handle refusal above (gate 15).
            raise UnknownHandle("unknown approval handle")
        return (state, payload)

    def approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
    ) -> tuple[str, str]:
        self._status_precheck(subject, approval_handle)
        try:
            reply = self._transport.call("ApprovalStatus", "s", (approval_handle,))
        except ConsumerCallError as error:
            raise _mapped_exception(error) from None
        return self._status_finish(subject, approval_handle, reply)

    def finish_approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
        reply: tuple[str, str],
    ) -> tuple[str, str]:
        """Phase 2 of the split status query: validate ``reply`` and apply
        the plan-binding check against the LIVE ownership seam."""

        return self._status_finish(subject, approval_handle, reply)

    def begin_approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
        on_reply: Callable[[tuple[str, str]], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """T-5644: phase 1 of a non-blocking status query, for a caller
        that must not block its main loop.

        The pre-check (and any synchronous send failure) RAISES to the
        caller exactly as :meth:`approval_status` does. Otherwise the
        request is sent and EXACTLY ONE of ``on_reply(raw_reply)`` or
        ``on_error(mapped_exception)`` is called later on the transport's
        dispatch context (the GLib main loop for the production transport).
        The raw reply is NOT validated here: the caller decides whether it
        is still live and then calls :meth:`finish_approval_status`, so a
        late reply never touches the journal on this client's behalf. A
        transport without ``call_async`` degrades to the blocking call,
        completed inline."""

        self._status_precheck(subject, approval_handle)
        call_async = getattr(self._transport, "call_async", None)
        if call_async is None:
            try:
                reply = self._transport.call("ApprovalStatus", "s", (approval_handle,))
            except ConsumerCallError as error:
                on_error(_mapped_exception(error))
            else:
                on_reply(reply)
            return

        def transport_error(error: ConsumerCallError) -> None:
            on_error(_mapped_exception(error))

        try:
            call_async("ApprovalStatus", "s", (approval_handle,), on_reply, transport_error)
        except ConsumerCallError as error:
            raise _mapped_exception(error) from None


class DBusConsumerTransport:
    """Production transport: a fresh blocking call per request through a
    connection factory, so an approval-service restart or bus reconnect
    needs no broker restart (TG 7236 restart-fixable). The factory is the
    only bus-address seam; production passes the system bus, tests a
    private bus."""

    def __init__(
        self,
        connection_factory: Callable[[], object],
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        main_loop_attached: bool = False,
    ) -> None:
        # T-5644: ``call_async`` is only sound on a connection attached to a
        # running main loop; on a blocking-only connection its handlers (and
        # its timeout) never fire. dbus-python cannot report the attachment,
        # so the wiring declares it, and ``call_async`` refuses otherwise.
        self._main_loop_attached = bool(main_loop_attached)
        self._connection_factory = connection_factory
        self._timeout = float(timeout)

    def call(self, method: str, signature: str, args: tuple[str, ...]) -> tuple[str, str]:
        import dbus  # guarded: only the production/test-bus path needs it

        try:
            connection = self._connection_factory()
            reply = connection.call_blocking(
                APPROVAL_BUS_NAME,
                APPROVAL_OBJECT_PATH,
                CONSUMER_INTERFACE,
                method,
                signature,
                args,
                timeout=self._timeout,
            )
        except dbus.exceptions.DBusException as exc:
            raise ConsumerCallError(
                exc.get_dbus_name() or "",
                exc.get_dbus_message() or "",
            ) from None
        if not isinstance(reply, (tuple, list)) or len(reply) != 2:
            raise ConsumerCallError("malformed", "malformed reply")
        return (str(reply[0]), str(reply[1]))

    def call_async(
        self,
        method: str,
        signature: str,
        args: tuple[str, ...],
        reply_handler: Callable[[tuple[str, str]], None],
        error_handler: Callable[[ConsumerCallError], None],
    ) -> None:
        """T-5644: non-blocking twin of :meth:`call`. Same connection
        factory, same timeout; the handlers run on the connection's main
        loop (the broker's GLib loop), so nothing here blocks it and no
        second thread touches the broker's journal. A synchronous failure
        to send raises ``ConsumerCallError`` like ``call`` does."""

        import dbus  # guarded: only the production/test-bus path needs it

        if not self._main_loop_attached:
            raise ConsumerCallError("main-loop-required", "async call needs a main-loop connection")

        def on_reply(*reply: object) -> None:
            if len(reply) != 2:
                error_handler(ConsumerCallError("malformed", "malformed reply"))
                return
            reply_handler((str(reply[0]), str(reply[1])))

        def on_error(exc: BaseException) -> None:
            get_name = getattr(exc, "get_dbus_name", None)
            get_message = getattr(exc, "get_dbus_message", None)
            error_handler(
                ConsumerCallError(
                    (get_name() if get_name else "") or "",
                    (get_message() if get_message else "") or "",
                )
            )

        try:
            connection = self._connection_factory()
            connection.call_async(
                APPROVAL_BUS_NAME,
                APPROVAL_OBJECT_PATH,
                CONSUMER_INTERFACE,
                method,
                signature,
                args,
                on_reply,
                on_error,
                timeout=self._timeout,
            )
        except dbus.exceptions.DBusException as exc:
            raise ConsumerCallError(
                exc.get_dbus_name() or "",
                exc.get_dbus_message() or "",
            ) from None
