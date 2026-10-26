"""Broker-side client of the per-seat executor (T-2917 5b; ORG-052 sec. 3).

``DBusExecutorPort`` is the real ``ExecutorPort`` (``operations.py``): it
is handed records and returns a bounded execution reference; it holds no
journal and no append callback.

Identity (gate 19, broker half): before EVERY ``Execute`` and
``ExecutionGet`` the broker resolves the owner of the registry-derived
name ``org.pantheon.HostExecutor1.U<uid>`` and requires the bus-attested
uid of that owner to be the target seat's registry uid; it then calls the
verified UNIQUE name, so a name takeover between check and call cannot
redirect the request.

Error classes drive the operation driver's recovery (ruling B11):

* ``ExecutorUnavailable`` / ``ExecutorRefused`` / ``ExecutorIdentityMismatch``
  are DEFINITIVE: the request never started work, so a retry is safe;
* ``ExecutorAmbiguous`` (timeout, disconnect mid-call) may have started
  work: a mutating operation then waits for the 5d reconciler;
* ``ExecutorUnknownExecution``: the executor no longer holds the handle
  (e.g. restarted); same rule.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from tools.host_control.contracts import (
    HostControlError,
    OperationReceipt,
    OperationRecord,
    PolicyDenied,
    RegistryError,
    RollbackIntent,
)
from tools.host_control.execution_source import BrokerExecutionSource, spec_digest
from tools.host_control.health import HealthProbeResult
from tools.host_control.executor import wire as executor_wire
from tools.host_control.reconciler import ProbeReport, ReconcilePending
from tools.host_control.registry import SeatRegistry


class ExecutorUnavailable(HostControlError):
    """No executor owns the seat's name, or the bus cannot be reached."""


class ExecutorRefused(HostControlError):
    """The executor replied with a closed refusal (Busy, Denied, ...)."""

    def __init__(self, message: str, name: str = executor_wire.FAILED_ERROR) -> None:
        super().__init__(f"{name.rsplit('.', 1)[-1]}: {message}"[:200])
        self.name = name


class ExecutorIdentityMismatch(PolicyDenied):
    """The executor name is owned by a uid other than the seat's. The wire
    reason is always ``subject_denied`` whatever detail is passed."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("subject_denied")


class ExecutorAmbiguous(HostControlError):
    """The call may or may not have reached the executor."""


class ExecutorUnknownExecution(HostControlError):
    """The executor does not hold this execution handle."""


DEFINITIVE_DISPATCH_FAILURES = (ExecutorUnavailable, ExecutorRefused, ExecutorIdentityMismatch)

_NOT_DELIVERED = frozenset(
    (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.UnknownObject",
        "org.freedesktop.DBus.Error.UnknownMethod",
    )
)


class ExecutorCallError(Exception):
    def __init__(self, name: str, body: str) -> None:
        super().__init__(name)
        self.name = str(name)[:255]
        self.body = str(body)[:200]


class ExecutorTransport(Protocol):
    def name_owner(self, well_known_name: str) -> str: ...

    def unix_user(self, unique_name: str) -> int: ...

    def call(self, unique_name: str, method: str, signature: str, args: tuple[str, ...]) -> tuple: ...


@dataclass(frozen=True)
class CollectedResult:
    ok: bool
    result: executor_wire.ExecutionResult
    # T-2917 5d.1 (T-2917:D14): the executor refused BEFORE its engine ran
    # (state ``refused``). The driver classifies it from the journal; it is
    # never by itself proof that nothing changed.
    refused: bool = False


class DBusExecutorPort:
    def __init__(
        self,
        *,
        registry: SeatRegistry,
        execution_source: BrokerExecutionSource,
        transport: ExecutorTransport,
    ) -> None:
        self._registry = registry
        self._source = execution_source
        self._transport = transport

    def resolve(self, target_seat_id: str) -> str:
        """The verified unique name of the target seat's executor."""

        try:
            seat = self._registry.seat(target_seat_id)
        except RegistryError as exc:
            raise PolicyDenied("target_denied") from exc
        name = executor_wire.bus_name_for_uid(seat.uid)
        try:
            owner = self._transport.name_owner(name)
            uid = int(self._transport.unix_user(owner))
        except ExecutorCallError as exc:
            raise ExecutorUnavailable("seat executor is not available") from exc
        if uid != seat.uid:
            raise ExecutorIdentityMismatch()
        return owner

    def ready(self, operation: OperationRecord) -> bool:
        try:
            self.resolve(operation.target_seat_id)
        except HostControlError:
            return False
        return True

    def _call(self, unique: str, method: str, signature: str, args: tuple[str, ...]) -> tuple:
        try:
            return self._transport.call(unique, method, signature, args)
        except ExecutorCallError as exc:
            if exc.name == executor_wire.UNKNOWN_EXECUTION_ERROR:
                raise ExecutorUnknownExecution("executor does not hold the execution") from None
            if exc.name in executor_wire.CLOSED_ERROR_NAMES:
                raise ExecutorRefused(exc.body, name=exc.name) from None
            if exc.name in _NOT_DELIVERED:
                raise ExecutorUnavailable("seat executor is not available") from None
            raise ExecutorAmbiguous("executor call outcome unknown") from None

    # ExecutorPort
    def execute(
        self,
        operation: OperationRecord,
        staged_receipt: OperationReceipt,
        intents: tuple[RollbackIntent, ...],
    ) -> str:
        digest = self._source.expected_digest(operation)
        unique = self.resolve(operation.target_seat_id)
        reply = self._call(unique, "Execute", "ss", (operation.operation_handle, digest))
        handle = reply[0] if isinstance(reply, (tuple, list)) and len(reply) == 1 else reply
        if not executor_wire.is_execution_handle(handle):
            raise ExecutorAmbiguous("executor returned a malformed execution handle")
        return str(handle)

    def collect(self, operation: OperationRecord, execution_ref: str) -> CollectedResult | None:
        """None while running; the validated typed result once terminal."""

        spec = self._source.spec_for(operation)
        collected = self._collect(operation, execution_ref, spec)
        if collected is not None and operation.kind == "verify" and collected.result.mutations:
            raise HostControlError("verify execution returned mutation records")
        return collected

    # the read-only reconcile probe (T-2917 5d.2, ruling T-2917:D13)
    def execute_probe(self, operation: OperationRecord) -> str:
        """Execute the apply's ``reconcile`` spec, selected by its digest."""

        digest = self._source.reconcile_digest(operation)
        unique = self.resolve(operation.target_seat_id)
        reply = self._call(unique, "Execute", "ss", (operation.operation_handle, digest))
        handle = reply[0] if isinstance(reply, (tuple, list)) and len(reply) == 1 else reply
        if not executor_wire.is_execution_handle(handle):
            raise ExecutorAmbiguous("executor returned a malformed execution handle")
        return str(handle)

    def collect_probe(self, operation: OperationRecord, execution_ref: str) -> CollectedResult | None:
        collected = self._collect(operation, execution_ref, self._source.reconcile_spec_for(operation))
        if collected is not None and collected.result.mutations:
            raise HostControlError("reconcile probe returned mutation records")
        return collected

    def _collect(
        self,
        operation: OperationRecord,
        execution_ref: str,
        spec: Mapping[str, object],
    ) -> CollectedResult | None:
        unique = self.resolve(operation.target_seat_id)
        reply = self._call(unique, "ExecutionGet", "s", (execution_ref,))
        if not isinstance(reply, (tuple, list)) or len(reply) != 2:
            raise ExecutorAmbiguous("malformed ExecutionGet reply")
        state, raw = str(reply[0]), str(reply[1])
        if state not in executor_wire.EXECUTION_STATES:
            raise ExecutorAmbiguous("unknown execution state")
        if state == "running":
            return None
        kinds = {
            str(item["operation_id"]): str(item["kind"])
            for item in spec["operations"]  # type: ignore[union-attr]
        }
        result = executor_wire.decode_result(
            raw,
            operation_handle=operation.operation_handle,
            spec_digest=spec_digest(spec),
            operation_kinds=kinds,
        )
        if state == "refused":
            if result.mutations or result.evidence:
                raise HostControlError("refused execution returned records")
            return CollectedResult(ok=False, result=result, refused=True)
        ok = result.ok and state == "succeeded"
        return CollectedResult(ok=ok, result=result)


_PROBE_TRANSIENT = (
    ExecutorUnavailable,
    ExecutorRefused,
    ExecutorIdentityMismatch,
    ExecutorAmbiguous,
    ExecutorUnknownExecution,
)


class ExecutorReconcileProbe:
    """``ReconcileProbePort`` over the seat executor (T-2917 5d.2): runs
    the apply's read-only ``reconcile`` execution and waits, bounded, for
    its typed observations. Any transport-level failure is
    ``ReconcilePending`` (nothing decided); a probe the executor refused or
    could not finish is an INCOMPLETE report (never proof of a clean host)."""

    def __init__(
        self,
        *,
        port: DBusExecutorPort,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 300.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._port = port
        self._sleep = sleeper
        self._clock = clock
        self._timeout = float(timeout)
        self._poll = float(poll_interval)

    def probe(self, operation: OperationRecord) -> ProbeReport:
        try:
            reference = self._port.execute_probe(operation)
            deadline = self._clock() + self._timeout
            while True:
                collected = self._port.collect_probe(operation, reference)
                if collected is not None:
                    break
                if self._clock() >= deadline:
                    raise ReconcilePending("reconcile probe still running at its bound")
                self._sleep(self._poll)
        except _PROBE_TRANSIENT as exc:
            raise ReconcilePending(f"reconcile probe unavailable: {type(exc).__name__}") from exc
        if collected.refused:
            return ProbeReport(evidence=(), complete=False, detail=collected.result.detail[:200])
        return ProbeReport(
            evidence=collected.result.evidence,
            complete=collected.result.ok,
            detail=collected.result.detail[:200],
        )


# T-2917 5d.4: transcribed from approval_client.APPROVAL_BUS_NAME (a test
# pins the equality; this module imports nothing from the approval client)
APPROVAL_BUS_NAME = "org.pantheon.HostApproval1"
_REACHABILITY_REVISION = "T-2917-5d"


class ReachabilityTransport(Protocol):
    def name_owner(self, well_known_name: str) -> str: ...

    def unix_user(self, unique_name: str) -> int: ...

    def list_activatable_names(self) -> tuple[str, ...]: ...


class BusReachability:
    """``driver.ReachabilitySource`` over the bus (T-2917 5d.4, ruling
    T-2917:D21). Called ONLY from the driver thread (each call blocks).

    * executor, per registered seat: the owner of its registry-derived
      ``org.pantheon.HostExecutor1.U<uid>`` has the seat's uid, or the name
      is activatable: ready; absent and not activatable: degraded; owned by
      any other uid: unhealthy (an identity mismatch, never "down");
    * approval: ``org.pantheon.HostApproval1`` owned by ``approval_uid``:
      ready; absent: degraded; another uid: unhealthy.
    """

    def __init__(self, *, registry: SeatRegistry, transport: ReachabilityTransport, approval_uid: int) -> None:
        self._registry = registry
        self._transport = transport
        self._approval_uid = int(approval_uid)

    def _owner_uid(self, name: str) -> int | None:
        try:
            owner = self._transport.name_owner(name)
        except ExecutorCallError:
            return None
        return int(self._transport.unix_user(owner))

    @staticmethod
    def _result(status: str, reason: str = "") -> HealthProbeResult:
        return HealthProbeResult(status, reason[:200], _REACHABILITY_REVISION)  # type: ignore[arg-type]

    def executor_reachability(self) -> HealthProbeResult:
        activatable = frozenset(self._transport.list_activatable_names())
        mismatched: list[str] = []
        absent: list[str] = []
        for seat_id in self._registry.seat_ids():
            seat = self._registry.seat(seat_id)
            name = executor_wire.bus_name_for_uid(seat.uid)
            uid = self._owner_uid(name)
            if uid is None:
                if name not in activatable:
                    absent.append(seat_id)
            elif uid != seat.uid:
                mismatched.append(seat_id)
        if mismatched:
            return self._result("unhealthy", "executor identity mismatch for seats: " + ",".join(mismatched))
        if absent:
            return self._result("degraded", "executor absent and not activatable for seats: " + ",".join(absent))
        return self._result("ready")

    def approval_reachability(self) -> HealthProbeResult:
        uid = self._owner_uid(APPROVAL_BUS_NAME)
        if uid is None:
            return self._result("degraded", "approval service has no owner")
        if uid != self._approval_uid:
            return self._result("unhealthy", "approval service name owned by an unexpected uid")
        return self._result("ready")


class DBusExecutorTransport:
    """Blocking calls on a connection the factory makes in the CALLING
    thread (the broker's driver thread), never the service connection."""

    def __init__(self, connection_factory: Callable[[], object], *, timeout: float = 30.0) -> None:
        self._connection_factory = connection_factory
        self._timeout = float(timeout)
        self._connection: object | None = None

    def _bus(self) -> object:
        if self._connection is None:
            self._connection = self._connection_factory()
        return self._connection

    def _blocking(self, *args: object, timeout: float | None = None) -> object:
        import dbus

        try:
            return self._bus().call_blocking(*args, timeout=timeout or self._timeout)  # type: ignore[attr-defined]
        except dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name == "org.freedesktop.DBus.Error.Disconnected":
                self._connection = None
            raise ExecutorCallError(name, exc.get_dbus_message() or "") from None

    def name_owner(self, well_known_name: str) -> str:
        return str(
            self._blocking(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                "s",
                (well_known_name,),
            )
        )

    def unix_user(self, unique_name: str) -> int:
        return int(
            self._blocking(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixUser",
                "s",
                (unique_name,),
            )
        )

    def list_activatable_names(self) -> tuple[str, ...]:
        """T-2917 5d.4: the bus daemon's activatable names (reachability)."""

        reply = self._blocking(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListActivatableNames",
            "",
            (),
        )
        return tuple(str(item) for item in reply)  # type: ignore[union-attr]

    def call(self, unique_name: str, method: str, signature: str, args: tuple[str, ...]) -> tuple:
        reply = self._blocking(
            unique_name,
            executor_wire.OBJECT_PATH,
            executor_wire.EXECUTOR_INTERFACE,
            method,
            signature,
            args,
        )
        if isinstance(reply, (tuple, list)):
            return tuple(str(item) for item in reply)
        return (str(reply),)
