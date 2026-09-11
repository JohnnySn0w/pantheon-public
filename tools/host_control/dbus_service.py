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
from typing import Iterator, Protocol

from tools.host_control import wire
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.contracts import (
    ApprovalUnavailable,
    HostControlError,
    PolicyDenied,
    RegistryError,
    SubjectContext,
)
from tools.host_control.operations import OperationService
from tools.host_control.planner import HostPlanService
from tools.host_control.registry import SeatRegistry

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
    connection, and no callback — the real implementation lands in
    T-2913 as a D-Bus client of ``HostApproval1.Consumer``, and fakes
    supply it in tests. ``subject`` is the SERVER-derived caller
    (bus uid through the registry), never caller-asserted.

    Raises ``ApprovalUnavailable`` when no approval authority is
    reachable — fail closed, consistent with the AB-2 outage posture.
    """

    def approval_status(
        self,
        subject: SubjectContext,
        approval_handle: str,
    ) -> tuple[str, str]: ...


class UnavailableApprovalStatus:
    """Shipped default: the approval authority does not exist until
    T-2913 lands its service, so ``ApprovalGet`` fails closed with the
    bounded ``ApprovalUnavailable`` wire error."""

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
        seven-method public ``Manager`` interface and the two-method
        uid-restricted ``ApprovalSource`` interface. No signals, no
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
        ) -> None:
            super().__init__(connection, wire.OBJECT_PATH)
            self._registry = registry
            self._plan_service = plan_service
            self._operation_service = operation_service
            self._approval_source = approval_source
            self._approval_status = approval_status
            self._approval_uid = int(approval_uid)

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

        def _approval_caller(self, sender: str | None) -> int:
            # In-code uid restriction for the internal interface: bus
            # policy is defence in depth, never the perimeter (locked
            # input N7 / ORG-052 §5 and gate 10).
            uid = self._subject_uid(sender)
            if uid != self._approval_uid:
                raise _wire_error(PolicyDenied("subject_denied"))
            return uid

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
            uid = self._manager_subject(sender)
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
            uid = self._manager_subject(sender)
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
            uid = self._manager_subject(sender)
            with self._mapped_errors():
                # PREPARE-only in this unit (locked input N4): the
                # authorize step needs the approval service (T-2913) and
                # dispatch needs the executor (T-2917).
                record, _replayed = self._operation_service.prepare_apply(
                    uid,
                    str(plan_handle),
                    str(plan_digest),
                    str(idempotency_key),
                    str(approval_handle),
                )
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
            # Exposed but refusing (locked input N2): the method MUST
            # introspect as part of the ratified seven while T-2917 owns
            # the verify path. Identity first, so an unregistered uid
            # still gets the authorization refusal, not Unsupported.
            self._manager_subject(sender)
            try:
                self._operation_service.prepare_verify(
                    str(seat_id),
                    str(plan_handle),
                    str(idempotency_key),
                )
            except HostControlError as exc:
                raise dbus.exceptions.DBusException(
                    str(exc)[:200],
                    name=wire.UNSUPPORTED_ERROR,
                ) from exc
            except Exception as exc:
                raise _wire_error(exc) from exc
            # prepare_verify never returns today; if a future domain
            # change makes it return, the transport still refuses until
            # T-2917 deliberately rewires this handler.
            raise dbus.exceptions.DBusException(
                "verify is unsupported in this broker revision",
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
            uid = self._manager_subject(sender)
            with self._mapped_errors():
                # PREPARE-only in this unit (locked input N4).
                record, _replayed = self._operation_service.prepare_rollback(
                    uid,
                    str(receipt_handle),
                    str(idempotency_key),
                    str(approval_handle),
                )
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
            uid = self._manager_subject(sender)
            with self._mapped_errors():
                record = self._operation_service.get_operation(
                    uid,
                    str(operation_handle),
                )
                return (record.state, wire.operation_json(record))

        @dbus.service.method(
            wire.MANAGER_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
        )
        def ApprovalGet(
            self,
            approval_handle: str,
            sender: str | None = None,
        ) -> tuple[str, str]:
            uid = self._manager_subject(sender)
            with self._mapped_errors():
                subject = self._registry.subject_for_uid(uid)
                state, summary = self._approval_status.approval_status(
                    subject,
                    str(approval_handle),
                )
                return (str(state), str(summary))

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
