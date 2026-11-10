"""The ``org.pantheon.HostBroker1`` system D-Bus service (ORG-052 Unit
3a, T-2912).

Thin transport, zero domain logic (locked input N1): every handler
resolves the live bus sender to a Unix uid, re-authorizes at the
transport boundary, calls exactly one landed domain method, and maps the
result or exception onto the closed wire contract in
``tools.host_control.wire``. No policy decisions, no state machines, no
journal access live here.

Identity is never a caller argument (ORG-052 §2 / locked N6): the
subject comes only from ``connection.get_unix_user(sender)`` resolved
through the root-owned ``SeatRegistry``. The transport gate is uniform —
Manager methods refuse unregistered uids and ApprovalSource methods
refuse any uid but the configured approval-service uid, both BEFORE any
other domain call, both as ``PolicyDenied("subject_denied")``. Bus
policy (staged under ``dist/``) is defence in depth and never the
perimeter: these refusals hold on a fully permissive bus (gate 10).

The dbus imports are guarded so a runner without dbus-python imports
this module safely; ``DBUS_AVAILABLE`` is the flag. On the canonical
host dbus-python IS present under system python3 — a skip there means
the guard broke, not that the environment is missing.

No ``main()`` in this unit: the entrypoint and its root-owned config
schema are the T-2920 install leg's (OQ6 disposition). Nothing here
connects to the real system bus; tests use private buses only.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator, Protocol

from tools.host_control import wire
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.contracts import (
    ApprovalUnavailable,
    HostControlError,
    PolicyDenied,
    RateLimited,
    RegistryError,
    SubjectContext,
)
from tools.host_control.execution_source import BrokerExecutionSource
from tools.host_control.operations import OperationService
from tools.host_control.planner import HostPlanService
from tools.host_control.ratelimit import BrokerRateLimits
from tools.host_control.registry import SeatRegistry

#: T-5644: ApprovalGet replies are deferred, so up to this many status queries
#: per authenticated uid may be in flight at once. The per-uid read budget
#: (30 burst, 5/s) bounds the RATE; this bounds the CONCURRENCY the approval
#: service sees (each holds a 10s client timeout). Over the cap the call is
#: refused with the existing RateLimited wire error, same message shape.
MAX_APPROVAL_GET_INFLIGHT_PER_UID = 4
MAX_APPROVAL_GET_INFLIGHT_TOTAL = 16

try:  # pragma: no cover - exercised implicitly on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False


class ApprovalStatusSource(Protocol):
    """Value-returning seam for ``ApprovalGet`` (locked input N3).

    Same shape discipline as ``ExecutorPort`` (AB-1) and
    ``PlanMetadataSource`` (AB-2): it is handed no journal, no
    connection, and no callback. The real implementation is
    ``tools.host_control.approval_client.ApprovalConsumerClient`` (T-2917
    5a; deferred there from T-2913 by its OQ6 disposition), a D-Bus
    client of ``HostApproval1.Consumer`` that also binds the handle to
    the calling subject; fakes supply it in tests. ``subject`` is the
    SERVER-derived caller (bus uid through the registry), never
    caller-asserted.

    Raises ``ApprovalUnavailable`` when no approval authority is
    reachable — fail closed, consistent with the AB-2 outage posture.
    """

    def approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
    ) -> tuple[str, str]: ...


class OperationSubmitter(Protocol):
    """The broker-internal ``OperationDriver`` seam (T-2917 5b): handlers
    only enqueue a durable operation handle; the driver thread does every
    blocking authorize/dispatch/collect step off the main loop."""

    failure: str | None

    def submit(self, operation_handle: str) -> None: ...


class UnavailableApprovalStatus:
    """Fail-closed fallback for a broker wired WITHOUT an approval
    authority (e.g. a test driver, or before the T-2920 entrypoint
    configures ``ApprovalConsumerClient``): ``ApprovalGet`` fails closed
    with the bounded ``ApprovalUnavailable`` wire error."""

    def approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
    ) -> tuple[str, str]:
        raise ApprovalUnavailable("approval authority is not available")


if DBUS_AVAILABLE:

    def _wire_error(exc: BaseException) -> "dbus.exceptions.DBusException":
        """Map any exception onto the closed, bounded wire contract."""

        return dbus.exceptions.DBusException(
            wire.error_body_for(exc),
            name=wire.error_name_for(exc),
        )

    class HostBroker1Object(dbus.service.Object):
        """One object at ``/org/pantheon/HostBroker1`` exporting the
        seven-method public ``Manager`` interface, the two-method
        uid-restricted ``ApprovalSource`` interface, and the one-method
        target-seat-restricted ``ExecutionSource`` interface (T-2917 5a,
        ORG-052 section 3). No signals, no
        properties (ORG-052 §4: clients poll ``OperationGet``; evidence
        cannot leak through a broadcast channel)."""

        def __init__(
            self,
            connection: "dbus.bus.BusConnection",
            *,
            registry: SeatRegistry,
            plan_service: HostPlanService,
            operation_service: OperationService,
            approval_source: BrokerApprovalSource,
            approval_status: ApprovalStatusSource,
            approval_uid: int,
            rate_limits: BrokerRateLimits | None = None,
            execution_source: BrokerExecutionSource | None = None,
            driver: OperationSubmitter | None = None,
        ) -> None:
            super().__init__(connection, wire.OBJECT_PATH)
            self._registry = registry
            self._plan_service = plan_service
            self._operation_service = operation_service
            self._approval_source = approval_source
            self._approval_status = approval_status
            self._approval_uid = int(approval_uid)
            # T-2917 ruling i1: always on; tests may inject specs/clock.
            self._rate_limits = (
                rate_limits if rate_limits is not None else BrokerRateLimits()
            )
            # None until the executor wiring (T-2917 5b / T-2920 entrypoint):
            # OperationSpec then refuses Unsupported after the uid gate.
            self._execution_source = execution_source
            # None keeps the 5a behaviour (Apply/Rollback PREPARE-only,
            # Verify Unsupported); the T-2920 entrypoint wires the driver.
            self._driver = driver
            # ORG-082 steps 3 + 5 (T-2918): the plan composer was built by the
            # daemon load (load_daemon_runtime), which captured the op-registry
            # digest and asserted the substrate-free dispatch table; a start
            # that would expose a substrate family never reaches this line.
            runtime = plan_service.runtime
            self.loaded_op_registry_digest = runtime.loaded_op_registry_digest
            self.dispatch_table = runtime.dispatch_table
            self.startup_record = runtime.startup_record()

        # ------------------------------------------------------------
        # transport-boundary identity (uniform, before any domain call)
        # ------------------------------------------------------------

        def _get_unix_user(self, sender: str) -> int:
            return int(self.connection.get_unix_user(sender))

        def _subject_uid(self, sender: str | None) -> int:
            if not sender:
                raise _wire_error(PolicyDenied("subject_denied"))
            try:
                return self._get_unix_user(sender)
            except Exception as exc:
                # the bus could not attest the caller's credentials —
                # fail closed as an authorization refusal
                raise _wire_error(PolicyDenied("subject_denied")) from exc

        def _manager_subject(self, sender: str | None) -> int:
            uid = self._subject_uid(sender)
            try:
                self._registry.subject_for_uid(uid)
            except RegistryError as exc:
                # An unregistered uid is an AUTHORIZATION refusal, not a
                # registry fault: RegistryDenied stays reserved for
                # genuine root-owned-config faults (PO-required change,
                # T-2912 plan approval).
                raise _wire_error(PolicyDenied("subject_denied")) from exc
            return uid

        def _manager_call(self, sender: str | None, method: str) -> int:
            """Identity FIRST (an unregistered uid gets the authorization
            refusal, never RateLimited), then the per-uid budget, then the
            caller may make its one domain call (T-2917 ruling i1)."""

            uid = self._manager_subject(sender)
            try:
                self._rate_limits.check(uid, method)
            except Exception as exc:
                raise _wire_error(exc) from exc
            return uid

        def _approval_caller(self, sender: str | None) -> int:
            # In-code uid restriction for the internal interface: bus
            # policy is defence in depth, never the perimeter (locked
            # input N7 / ORG-052 §5 and gate 10).
            uid = self._subject_uid(sender)
            if uid != self._approval_uid:
                raise _wire_error(PolicyDenied("subject_denied"))
            return uid

        def _require_live_driver(self) -> None:
            """Review n2: with a dead driver, refuse BEFORE prepare so no
            PREPARED record is written that nothing will drive."""

            if self._driver is not None and getattr(self._driver, "failure", None):
                raise dbus.exceptions.DBusException(
                    "operation driver unavailable",
                    name=wire.FAILED_ERROR,
                )

        @contextmanager
        def _mapped_errors(self) -> Iterator[None]:
            try:
                yield
            except dbus.exceptions.DBusException:
                raise
            except Exception as exc:
                raise _wire_error(exc) from exc

        # ------------------------------------------------------------
        # org.pantheon.HostBroker1.Manager — exactly the ratified seven
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="",
            out_signature="ss",
            sender_keyword="sender",
        )
        def Capabilities(self, sender: str | None = None) -> tuple[str, str]:
            uid = self._manager_call(sender, "Capabilities")
            with self._mapped_errors():
                result = self._plan_service.capabilities(uid)
                return (
                    str(result.contract_version),
                    wire.capabilities_json(result),
                )

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="sss",
            out_signature="sss",
            sender_keyword="sender",
        )
        def Plan(
            self,
            seat_id: str,
            manifest_revision: str,
            idempotency_key: str,
            sender: str | None = None,
        ) -> tuple[str, str, str]:
            uid = self._manager_call(sender, "Plan")
            with self._mapped_errors():
                result = self._plan_service.plan(
                    uid,
                    str(seat_id),
                    str(manifest_revision),
                    str(idempotency_key),
                )
                return (
                    result.plan_handle,
                    result.plan_digest,
                    result.summary_json,
                )

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="ssss",
            out_signature="s",
            sender_keyword="sender",
        )
        def Apply(
            self,
            plan_handle: str,
            plan_digest: str,
            idempotency_key: str,
            approval_handle: str,
            sender: str | None = None,
        ) -> str:
            uid = self._manager_call(sender, "Apply")
            self._require_live_driver()
            with self._mapped_errors():
                if self._driver is not None:
                    # Ruling B4: refuse (Unsupported) a plan holding a
                    # class this revision cannot execute BEFORE prepare,
                    # so no record is written and no grant can bind.
                    self._operation_service.require_apply_implemented(
                        uid,
                        str(plan_handle),
                        str(plan_digest),
                    )
                record, _replayed = self._operation_service.prepare_apply(
                    uid,
                    str(plan_handle),
                    str(plan_digest),
                    str(idempotency_key),
                    str(approval_handle),
                )
                if self._driver is not None:
                    self._driver.submit(record.operation_handle)
                return record.operation_handle

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="sss",
            out_signature="s",
            sender_keyword="sender",
        )
        def Verify(
            self,
            seat_id: str,
            plan_handle: str,
            idempotency_key: str,
            sender: str | None = None,
        ) -> str:
            # Identity first, so an unregistered uid still gets the
            # authorization refusal, not Unsupported.
            uid = self._manager_call(sender, "Verify")
            self._require_live_driver()
            if self._driver is not None:
                # T-2917 5b: the real verify path (policy-authorized,
                # read-only; rulings B2/B3).
                with self._mapped_errors():
                    record, _replayed = self._operation_service.prepare_verify(
                        uid,
                        str(seat_id),
                        str(plan_handle),
                        str(idempotency_key),
                    )
                    self._driver.submit(record.operation_handle)
                    return record.operation_handle
            # Without a driver: exposed but refusing (locked input N2),
            # and the domain is never touched.
            raise dbus.exceptions.DBusException(
                "verify needs the executor driver (T-2917); not wired in this broker",
                name=wire.UNSUPPORTED_ERROR,
            )

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="sss",
            out_signature="s",
            sender_keyword="sender",
        )
        def Rollback(
            self,
            receipt_handle: str,
            idempotency_key: str,
            approval_handle: str,
            sender: str | None = None,
        ) -> str:
            uid = self._manager_call(sender, "Rollback")
            self._require_live_driver()
            with self._mapped_errors():
                if self._driver is not None:
                    # T-2917 5d.2 (ruling T-2917:D15): the Apply pattern.
                    # Ruling B4 first: refuse (Unsupported) BEFORE prepare,
                    # so no record is written and no grant can bind.
                    self._operation_service.require_rollback_implemented(
                        uid,
                        str(receipt_handle),
                    )
                # Without a driver: PREPARE-only (T-2912 locked input N4).
                record, _replayed = self._operation_service.prepare_rollback(
                    uid,
                    str(receipt_handle),
                    str(idempotency_key),
                    str(approval_handle),
                )
                if self._driver is not None:
                    self._driver.submit(record.operation_handle)
                return record.operation_handle

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
        )
        def OperationGet(
            self,
            operation_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            uid = self._manager_call(sender, "OperationGet")
            with self._mapped_errors():
                record = self._operation_service.get_operation(
                    uid,
                    str(operation_handle),
                )
                return (
                    record.state,
                    wire.operation_json(
                        record,
                        compensation_handle=self._operation_service.compensation_handle(record),
                    ),
                )

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
            async_callbacks=("reply_cb", "error_cb"),
        )
        def ApprovalGet(
            self,
            approval_handle: str,
            sender: str | None = None,
            reply_cb: Callable[..., None] | None = None,
            error_cb: Callable[[BaseException], None] | None = None,
        ) -> None:
            # T-5644: a DEFERRED reply. The status query to the approval
            # service never blocks the GLib main loop, so the approval
            # service's PlanMetadata call-back into this broker is served
            # while a status query is in flight.
            #
            # Interleaving analysis (phase 1 = identity, budget, pre-check;
            # phase 2 = post-bind check in the reply handler). Before this
            # change the two phases were atomic against every other handler
            # because the loop was blocked; now any handler may run between
            # them, all on this same loop thread. Every journal write a
            # concurrent handler can make to ``operations`` (Apply, Rollback,
            # Verify insert rows; cancel, authorize, dispatch, completion
            # and manual_recovery UPDATE only ``state``, ``authority_record_id``,
            # ``operation_json`` and ``updated_at``) leaves the columns the
            # phase-2 predicate reads (subject_uid, subject_seat_id,
            # approval_handle, plan_handle) untouched, and no code path
            # DELETEs or re-keys an operations row. So the predicate is
            # monotone: true at phase 1 stays true at phase 2, and a row
            # that a concurrent Apply adds can only matter to a handle that
            # the caller's own subject binding already scopes. Phase 2
            # re-runs the FULL binding check on the live journal (never a
            # phase-1 result), so were a row ever to vanish the reply would
            # fail closed as UnknownHandle, the same text as an unknown
            # handle (gate 15). Operation state (cancelled, completed) is
            # not part of the answer, which is the grant's state from the
            # approval service.
            uid = self._manager_call(sender, "ApprovalGet")
            state = self._approval_get_state()
            if (
                state["total"] >= MAX_APPROVAL_GET_INFLIGHT_TOTAL
                or state["by_uid"].get(uid, 0) >= MAX_APPROVAL_GET_INFLIGHT_PER_UID
            ):
                raise _wire_error(RateLimited("read request budget exhausted; retry later"))
            state["total"] += 1
            state["by_uid"][uid] = state["by_uid"].get(uid, 0) + 1
            settled = False

            def settle() -> bool:
                """THE single release helper: frees the in-flight slot and
                arms the reply exactly once. False means a previous path
                already settled, so the caller must do nothing more."""

                nonlocal settled
                if settled:
                    return False
                settled = True
                state["total"] -= 1
                remaining = state["by_uid"].get(uid, 1) - 1
                if remaining > 0:
                    state["by_uid"][uid] = remaining
                else:
                    state["by_uid"].pop(uid, None)
                return True

            def fail(error: BaseException) -> None:
                if not settle():
                    return
                error_cb(
                    error
                    if isinstance(error, dbus.exceptions.DBusException)
                    else _wire_error(error)
                )

            def on_reply(reply: tuple[str, str]) -> None:
                if settled:
                    return
                if state["closed"]:
                    # late reply after close(): drop it. Nothing below may
                    # touch the (torn-down) journal or connection.
                    settle()
                    return
                try:
                    # Phase 2 re-resolves authority: the registry can reload
                    # between phases, so never reuse the phase-1 subject.
                    try:
                        current = self._registry.subject_for_uid(uid)
                    except RegistryError as exc:
                        raise _wire_error(PolicyDenied("subject_denied")) from exc
                    if current != subject:
                        raise _wire_error(PolicyDenied("subject_denied"))
                    result = self._approval_status.finish_approval_status(
                        current,
                        str(approval_handle),
                        reply,
                    )
                except Exception as exc:
                    fail(exc)
                    return
                if settle():
                    reply_cb(str(result[0]), str(result[1]))

            try:
                with self._mapped_errors():
                    subject = self._registry.subject_for_uid(uid)
                    begin = getattr(self._approval_status, "begin_approval_status", None)
                    if begin is not None:
                        begin(subject, str(approval_handle), on_reply, fail)
                    else:
                        # a source without the async seam (fakes, the
                        # fail-closed fallback): blocking call, replied inline
                        result = self._approval_status.approval_status(
                            subject,
                            str(approval_handle),
                        )
                        if settle():
                            reply_cb(str(result[0]), str(result[1]))
            except BaseException:
                settle()
                raise

        def close(self) -> None:
            """T-5644: broker shutdown. Pending ApprovalGet replies that
            arrive afterwards are dropped without touching the journal."""

            self._approval_get_state()["closed"] = True

        def _approval_get_state(self) -> dict:
            # Loop-thread only (no lock); created lazily so a bare test
            # instance needs no __init__.
            state = self.__dict__.get("_approval_get")
            if state is None:
                state = {"total": 0, "by_uid": {}, "closed": False}
                self.__dict__["_approval_get"] = state
            return state

        # ------------------------------------------------------------
        # org.pantheon.HostBroker1.ApprovalSource — approval uid ONLY
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.APPROVAL_SOURCE_INTERFACE,
            in_signature="s",
            out_signature="ssssssss",
            sender_keyword="sender",
        )
        def PlanMetadata(
            self,
            plan_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str, str, str, str, str, str, str]:
            self._approval_caller(sender)
            with self._mapped_errors():
                return self._approval_source.plan_metadata_wire(
                    str(plan_handle)
                )

        @dbus.service.method(
            wire.APPROVAL_SOURCE_INTERFACE,
            in_signature="ss",
            out_signature="ss",
            sender_keyword="sender",
        )
        def PlanSummary(
            self,
            plan_handle: str,
            expected_digest: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            self._approval_caller(sender)
            with self._mapped_errors():
                return self._approval_source.plan_summary(
                    str(plan_handle),
                    str(expected_digest),
                )

        # ------------------------------------------------------------
        # org.pantheon.HostBroker1.ExecutionSource — registered seat
        # executors ONLY, and only for their own target seat (T-2917 5a)
        # ------------------------------------------------------------

        @dbus.service.method(
            wire.EXECUTION_SOURCE_INTERFACE,
            in_signature="ss",
            out_signature="ssss",
            sender_keyword="sender",
        )
        def OperationSpec(
            self,
            operation_handle: str,
            expected_operation_digest: str,
            sender: str | None = None,
        ) -> tuple[str, str, str, str]:
            # Transport gate: a registered seat uid (the executor runs as
            # its seat's uid); the domain then requires that uid to be the
            # registry uid of the operation's TARGET seat.
            uid = self._manager_subject(sender)
            if self._execution_source is None:
                raise dbus.exceptions.DBusException(
                    "execution source is not configured",
                    name=wire.UNSUPPORTED_ERROR,
                )
            with self._mapped_errors():
                return self._execution_source.operation_spec(
                    uid,
                    str(operation_handle),
                    str(expected_operation_digest),
                )
