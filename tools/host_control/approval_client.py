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
subject"; gate 15): HostApproval1's ``ApprovalStatus`` is broker-uid-only
and does not know the requesting subject, and its payload names no plan or
owner. The broker therefore answers only for an approval handle that one
of the caller's OWN broker operations already carries, and reports any
other handle as unknown before contacting the approval service. This is
weaker than full binding: a caller that already HOLDS a 64-hex handle can
attach it to one of its own throwaway Apply operations and then read that
grant's state (the grant is not consumed). Real binding needs
HostApproval1's status reply to carry the owner or plan; that is an
approval-side follow-up (T-2917 5a review S1). The pre-Apply status gap is
bubbled to the coordinator.

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
APPROVAL_STATUS_SCHEMA: Final = "pantheon-approval-status/v1"
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
    it): does one of this subject's own operations carry the handle?"""

    def subject_references_approval(
        self,
        *,
        subject: SubjectContext,
        approval_handle: str,
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
    def approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
    ) -> tuple[str, str]:
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
        try:
            reply = self._transport.call("ApprovalStatus", "s", (approval_handle,))
        except ConsumerCallError as error:
            raise _mapped_exception(error) from None
        return status_from_reply(approval_handle, reply)


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
    ) -> None:
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
