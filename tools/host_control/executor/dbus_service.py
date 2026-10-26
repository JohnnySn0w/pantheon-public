"""``org.pantheon.HostExecutor1.Executor`` (T-2917 5b; ORG-052 section 3).

Thread model (the call-back deadlock constraint): the GLib main loop thread
only runs the two handlers below, which take a lock, update the bounded
execution table and return. ``Execute`` returns its execution handle
IMMEDIATELY; the work runs on one worker thread, which fetches the spec
from the broker over a SEPARATE bus connection, so the broker can serve
``OperationSpec`` while its own driver thread waits on ``Execute``/
``ExecutionGet`` and nothing here re-enters the main loop. The worker uses
the service connection for exactly one thing: the bus-daemon attestation
call (``GetConnectionUnixUser``) that re-checks the broker owner's uid
right before the fetch; it never serves, emits or fetches on it, and never
touches GLib. It publishes its result under the lock.

Identity, two-sided (gate 19 executor half; bus policy is defence in
depth, never the perimeter):

* every call must come from the configured broker uid, AND the current
  owner of ``org.pantheon.HostBroker1`` must itself be the broker uid
  (``Denied`` otherwise). The caller need not BE that owner connection:
  the broker's driver calls from its own outbound connection so its main
  loop stays free to serve ``OperationSpec``;
* the spec is fetched from the owner's VERIFIED unique name, not the
  well-known name, so a name swap mid-flight cannot redirect it, and the
  owner's uid is re-checked in the worker right before the fetch; the
  spec's target must resolve through the registry to this executor's own
  uid.

At most one execution runs at a time: a duplicate ``(handle, digest)``
gets the same execution handle (running or finished); the same handle
with another digest is ``Denied`` WHILE that handle runs; a different
handle while one runs is ``Busy``. Since T-2917 5d.2 (ruling T-2917:D13)
the table is keyed by ``(handle, digest)``: once an operation's execution
has finished, a different digest for it (the broker's read-only
``reconcile`` probe) starts its own execution. A ``refused`` entry did
nothing, so an identical Execute after it starts afresh (the broker's
serve-once rule still decides whether the spec is handed out).

Outcome states (T-2917 5d.1): a refusal BEFORE the engine runs (the broker
identity re-check, the spec fetch, the spec parse) is ``refused``; the
engine's own outcome is ``succeeded`` or ``failed``.

Hard deadline backstop (ruling T-2917:D11): every execution arms a watchdog
at the engine's run budget + ``HARD_DEADLINE_GRACE_SECONDS``. If the run is
still going when it fires, the executor process exits with
``EXIT_HARD_DEADLINE``; it never fabricates a result for a run it cannot
account for (the broker then sees ``UnknownExecution`` and reconciles).
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Protocol

from tools.host_control.contracts import HostControlError
from tools.host_control.executor.engine import ExecutorEngine, parse_spec
from tools.host_control.executor.identity import ExecutorIdentity
from tools.host_control.executor.wire import (
    EXIT_HARD_DEADLINE,
    HARD_DEADLINE_GRACE_SECONDS,
    ExecutionResult,
    ExecutorBusy,
    ExecutorDenied,
    InvalidSpec,
    UnknownExecution,
    encode_result,
    error_body_for,
    error_name_for,
    is_execution_handle,
)
from tools.host_control.registry import SeatRegistry

BROKER_BUS_NAME = "org.pantheon.HostBroker1"
BROKER_OBJECT_PATH = "/org/pantheon/HostBroker1"
# transcribed from tools.host_control.wire (the executor imports nothing
# that imports the broker journal); a test pins it
EXECUTION_SOURCE_INTERFACE = "org.pantheon.HostBroker1.ExecutionSource"
DEFAULT_RETAINED_EXECUTIONS = 16


class SpecSource(Protocol):
    """Executor -> broker ``OperationSpec`` over a dedicated connection."""

    def operation_spec(
        self,
        broker_unique_name: str,
        operation_handle: str,
        expected_digest: str,
    ) -> tuple[str, str, str, str]: ...


class Cancellable(Protocol):
    def cancel(self) -> None: ...


def _arm_daemon_timer(seconds: float, callback: Callable[[], None]) -> Cancellable:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.name = "host-executor-watchdog"
    timer.start()
    return timer


class BusIdentity(Protocol):
    """Bus-daemon attestation seams (injectable for tests)."""

    def unix_user(self, unique_name: str) -> int: ...

    def name_owner(self, well_known_name: str) -> str: ...


@dataclass
class _Execution:
    execution_handle: str
    operation_handle: str
    spec_digest: str
    state: str = "running"
    result_json: str = ""
    done: threading.Event = field(default_factory=threading.Event)


class ExecutionTable:
    """Lock-guarded, bounded table of executions (pure; no dbus)."""

    def __init__(
        self,
        *,
        handle_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        retain: int = DEFAULT_RETAINED_EXECUTIONS,
    ) -> None:
        self._lock = threading.Lock()
        self._handle_factory = handle_factory
        self._retain = retain
        self._by_execution: "OrderedDict[str, _Execution]" = OrderedDict()
        self._by_operation: dict[tuple[str, str], str] = {}

    def submit(self, operation_handle: str, spec_digest: str) -> tuple[_Execution, bool]:
        with self._lock:
            key = (operation_handle, spec_digest)
            existing_handle = self._by_operation.get(key)
            if existing_handle is not None:
                existing = self._by_execution[existing_handle]
                if existing.state != "refused":
                    return existing, False
                # a refused entry touched nothing: start afresh
                del self._by_execution[existing_handle]
                del self._by_operation[key]
            if any(
                item.state == "running" and item.operation_handle == operation_handle
                for item in self._by_execution.values()
            ):
                raise ExecutorDenied("operation already executing under another digest")
            if any(item.state == "running" for item in self._by_execution.values()):
                raise ExecutorBusy("another execution is running")
            handle = self._handle_factory()
            if not is_execution_handle(handle):
                raise HostControlError("invalid generated execution handle")
            entry = _Execution(handle, operation_handle, spec_digest)
            self._by_execution[handle] = entry
            self._by_operation[key] = handle
            while len(self._by_execution) > self._retain:
                oldest_handle, oldest = next(iter(self._by_execution.items()))
                if oldest.state == "running":
                    break
                del self._by_execution[oldest_handle]
                self._by_operation.pop((oldest.operation_handle, oldest.spec_digest), None)
            return entry, True

    def finish(self, execution_handle: str, state: str, result_json: str) -> None:
        with self._lock:
            entry = self._by_execution[execution_handle]
            entry.state = state
            entry.result_json = result_json
            entry.done.set()

    def get(self, execution_handle: str) -> tuple[str, str]:
        if not is_execution_handle(execution_handle):
            raise UnknownExecution("unknown execution")
        with self._lock:
            entry = self._by_execution.get(execution_handle)
            if entry is None:
                raise UnknownExecution("unknown execution")
            return entry.state, entry.result_json

    def running(self) -> bool:
        with self._lock:
            return any(item.state == "running" for item in self._by_execution.values())

    def wait(self, execution_handle: str, timeout: float) -> bool:
        with self._lock:
            entry = self._by_execution.get(execution_handle)
        return bool(entry and entry.done.wait(timeout))


class ExecutorService:
    """Transport-free core of the executor service: the caller gates, the
    execution table and the worker. The dbus object below is a thin shell."""

    def __init__(
        self,
        *,
        identity: ExecutorIdentity,
        registry: SeatRegistry,
        broker_uid: int,
        engine: ExecutorEngine,
        spec_source: SpecSource,
        bus: BusIdentity,
        table: ExecutionTable | None = None,
        start_worker: Callable[[Callable[[], None]], None] | None = None,
        watchdog: Callable[[float, Callable[[], None]], Cancellable] = _arm_daemon_timer,
        exit: Callable[[int], object] = os._exit,
    ) -> None:
        self._watchdog = watchdog
        self._exit = exit
        self.identity = identity
        self._registry = registry
        self._broker_uid = int(broker_uid)
        self._engine = engine
        self._spec_source = spec_source
        self._bus = bus
        self.table = table or ExecutionTable()
        self._start_worker = start_worker or _start_daemon_thread

    def authenticate_broker(self, sender: str | None) -> str:
        """The caller must be the broker uid AND the broker's well-known
        name must be owned by the broker uid; returns the owner's verified
        unique name (where the spec is fetched from)."""

        if not sender:
            raise ExecutorDenied("caller unknown")
        try:
            uid = int(self._bus.unix_user(sender))
            owner = str(self._bus.name_owner(BROKER_BUS_NAME))
            owner_uid = int(self._bus.unix_user(owner))
        except Exception as exc:
            raise ExecutorDenied("bus could not attest the caller") from exc
        if uid != self._broker_uid:
            raise ExecutorDenied("caller is not the broker uid")
        if owner_uid != self._broker_uid:
            raise ExecutorDenied("the broker name is not owned by the broker uid")
        return owner

    def execute(self, sender: str | None, operation_handle: str, spec_digest: str) -> str:
        broker = self.authenticate_broker(sender)
        if not is_execution_handle(operation_handle) or not is_execution_handle(spec_digest):
            raise InvalidSpec("invalid operation handle or digest")
        entry, created = self.table.submit(operation_handle, spec_digest)
        if created:
            self._start_worker(lambda: self._run(entry, broker))
        return entry.execution_handle

    def execution_get(self, sender: str | None, execution_handle: str) -> tuple[str, str]:
        self.authenticate_broker(sender)
        return self.table.get(execution_handle)

    def watchdog_seconds(self) -> float:
        return float(self._engine.run_budget) + HARD_DEADLINE_GRACE_SECONDS

    def _deadline_breached(self, entry: _Execution) -> None:
        """Watchdog callback: the run outlived budget + grace. Exit the
        process (never fabricate a result); a run that already finished is
        left alone."""

        if entry.done.is_set():
            return
        try:
            print(
                "host-executor: hard execution deadline breached; exiting",
                file=sys.stderr,
            )
        except Exception:
            pass
        self._exit(EXIT_HARD_DEADLINE)

    def _run(self, entry: _Execution, broker_unique_name: str) -> None:
        timer = self._watchdog(self.watchdog_seconds(), lambda: self._deadline_breached(entry))
        state, result = self._execute(entry, broker_unique_name)
        try:
            encoded = encode_result(
                operation_handle=entry.operation_handle,
                spec_digest=entry.spec_digest,
                result=result,
            )
        except Exception:
            # The run cannot be accounted for: publish nothing and leave the
            # watchdog armed (the process exits at the hard deadline and the
            # broker reconciles); never a fabricated result.
            try:
                print("host-executor: execution result could not be encoded", file=sys.stderr)
            except Exception:
                pass
            return
        timer.cancel()
        self.table.finish(entry.execution_handle, state, encoded)

    def _execute(self, entry: _Execution, broker_unique_name: str) -> tuple[str, ExecutionResult]:
        try:
            if int(self._bus.unix_user(broker_unique_name)) != self._broker_uid:
                raise ExecutorDenied("broker identity changed before the spec fetch")
            schema, kind, returned, spec_json = self._spec_source.operation_spec(
                broker_unique_name,
                entry.operation_handle,
                entry.spec_digest,
            )
            parsed = parse_spec(
                schema_version=schema,
                execution_kind=kind,
                returned_digest=returned,
                spec_json=spec_json,
                expected_digest=entry.spec_digest,
                operation_handle=entry.operation_handle,
                identity=self.identity,
                registry=self._registry,
            )
        except Exception as exc:  # a pre-engine refusal: nothing was touched
            return "refused", ExecutionResult(
                ok=False,
                mutations=(),
                evidence=(),
                detail=f"refused before execution: {error_name_for(exc).rsplit('.', 1)[-1]}: {error_body_for(exc)}",
            )
        try:
            result = self._engine.run(parsed)
        except Exception as exc:  # the engine keeps what it committed (N3)
            result = ExecutionResult(
                ok=False,
                mutations=(),
                evidence=(),
                detail=f"execution aborted: {error_name_for(exc).rsplit('.', 1)[-1]}: {error_body_for(exc)}",
            )
        return ("succeeded" if result.ok else "failed"), result


def _start_daemon_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="host-executor-worker", daemon=True).start()


try:  # pragma: no cover - exercised on every import
    import dbus
    import dbus.service

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    DBUS_AVAILABLE = False


if DBUS_AVAILABLE:
    from tools.host_control.executor.wire import EXECUTOR_INTERFACE, OBJECT_PATH

    def _wire_error(exc: BaseException) -> "dbus.exceptions.DBusException":
        return dbus.exceptions.DBusException(error_body_for(exc), name=error_name_for(exc))

    class DBusBusIdentity:
        """Attestation through the bus daemon of the given connection."""

        def __init__(self, connection: "dbus.bus.BusConnection") -> None:
            self._connection = connection

        def unix_user(self, unique_name: str) -> int:
            return int(self._connection.get_unix_user(unique_name))

        def name_owner(self, well_known_name: str) -> str:
            return str(
                self._connection.call_blocking(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "GetNameOwner",
                    "s",
                    (well_known_name,),
                )
            )

    class DBusSpecSource:
        """``OperationSpec`` on a connection made by the factory IN THE
        WORKER THREAD (never the service connection)."""

        def __init__(self, connection_factory: Callable[[], object], *, timeout: float = 30.0) -> None:
            self._connection_factory = connection_factory
            self._timeout = float(timeout)

        def operation_spec(
            self,
            broker_unique_name: str,
            operation_handle: str,
            expected_digest: str,
        ) -> tuple[str, str, str, str]:
            connection = self._connection_factory()
            reply = connection.call_blocking(
                broker_unique_name,
                BROKER_OBJECT_PATH,
                EXECUTION_SOURCE_INTERFACE,
                "OperationSpec",
                "ss",
                (operation_handle, expected_digest),
                timeout=self._timeout,
            )
            if not isinstance(reply, (tuple, list)) or len(reply) != 4:
                raise InvalidSpec("malformed OperationSpec reply")
            return tuple(str(item) for item in reply)  # type: ignore[return-value]

    class HostExecutor1Object(dbus.service.Object):
        """One object at ``/org/pantheon/HostExecutor1`` exporting the
        two-method ``Executor`` interface. No signals, no properties."""

        def __init__(self, connection: "dbus.bus.BusConnection", *, service: ExecutorService) -> None:
            super().__init__(connection, OBJECT_PATH)
            self._service = service

        @dbus.service.method(
            EXECUTOR_INTERFACE,
            in_signature="ss",
            out_signature="s",
            sender_keyword="sender",
        )
        def Execute(self, operation_handle: str, expected_spec_digest: str, sender: str | None = None) -> str:
            try:
                return self._service.execute(sender, str(operation_handle), str(expected_spec_digest))
            except Exception as exc:
                raise _wire_error(exc) from exc

        @dbus.service.method(
            EXECUTOR_INTERFACE,
            in_signature="s",
            out_signature="ss",
            sender_keyword="sender",
        )
        def ExecutionGet(self, execution_handle: str, sender: str | None = None) -> tuple[str, str]:
            try:
                return self._service.execution_get(sender, str(execution_handle))
            except Exception as exc:
                raise _wire_error(exc) from exc
