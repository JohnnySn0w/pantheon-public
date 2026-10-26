"""The seat's user service manager over the USER D-Bus (T-2917 5c.2;
ORG-052 gate 39, the founding-incident regression).

``DBusUserManager`` implements ``UserManagerPort`` against
``org.freedesktop.systemd1`` on the seat's own user bus. It never runs a
command-line client and never touches the manager's private control socket
or any bus named by the process environment:

* the bus address is DERIVED, never read: ``unix:path=<runtime_root>/<uid>/bus``
  with ``uid`` the REGISTRY uid of this executor's seat and ``runtime_root``
  fixed at ``/run/user``. ``runtime_root`` is the ONLY test override, and
  the executor entrypoint never passes it (ruling D7); evidence then
  labels the address ``test-override``;
* before the first call the socket must be a socket owned by the seat uid,
  the current owner of ``org.freedesktop.systemd1`` on that bus must be the
  seat uid (``GetConnectionUnixUser``), and every call goes to that owner's
  VERIFIED unique name, so a name swap cannot redirect a call;
* the connection is a private ``BusConnection`` to that one address; the
  environment-selected and system buses are never opened here.

Reads use ``LoadUnit`` plus per-property ``Properties.Get``; writes use the
Manager methods (``Reload``, ``ResetFailedUnit``, ``StartUnit``,
``StopUnit``, ``EnableUnitFiles``, ``DisableUnitFiles``) and wait for a
queued job through ``GetJob`` where the operator path waited (ruling D9
also admits the ``NeedDaemonReload``, ``EnvironmentFiles`` and ``Version``
reads). Native values go through ``tools.user_systemd.unit_snapshot_from_native``.

D4: ``environment_file_declared`` reads the TYPED ``EnvironmentFiles``
list. The operator path matched the substring ``EnvironmentFile`` anywhere
in the unit's file text, so the two differ when that substring sits only in
a comment or another directive's value: this transport answers False there.

Agreement of these calls and encodings with a REAL systemd user manager is
a live item for T-2924; every proof here is fixture evidence.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Callable, Final

from tools.user_systemd import (
    IS_ENABLED_ZERO_EXIT_STATES,
    ManagerState,
    ReadPurpose,
    UnitFileEnablement,
    UnitSnapshot,
    UserManagerCallFailed,
    UserManagerCallOutcome,
    UserManagerError,
    UserManagerProtocolError,
    UserManagerTimeout,
    environment_file_declared_from_native,
    unit_snapshot_from_native,
)
from tools.host_control.executor.user_manager import UserManagerUnavailable

DEFAULT_RUNTIME_ROOT: Final = Path("/run/user")
SYSTEMD1_NAME: Final = "org.freedesktop.systemd1"
MANAGER_PATH: Final = "/org/freedesktop/systemd1"
MANAGER_INTERFACE: Final = "org.freedesktop.systemd1.Manager"
UNIT_INTERFACE: Final = "org.freedesktop.systemd1.Unit"
SERVICE_INTERFACE: Final = "org.freedesktop.systemd1.Service"
TIMER_INTERFACE: Final = "org.freedesktop.systemd1.Timer"
PROPERTIES_INTERFACE: Final = "org.freedesktop.DBus.Properties"
BUS_DRIVER_NAME: Final = "org.freedesktop.DBus"
BUS_DRIVER_PATH: Final = "/org/freedesktop/DBus"
TRANSPORT: Final = "dbus"
ADDRESS_SOURCE_REGISTRY: Final = "registry-uid"
ADDRESS_SOURCE_TEST_OVERRIDE: Final = "test-override"
DEFAULT_CALL_TIMEOUT: Final = 30.0
JOB_POLL_INTERVAL: Final = 0.1
JOB_PATH_PREFIX: Final = "/org/freedesktop/systemd1/job/"
# T-2917 5d.1 (ruling T-2917:D11): the error name of a call refused because
# the bound execution deadline has passed (executor-internal; never a wire
# error name).
DEADLINE_REACHED: Final = "execution deadline reached"

PROPERTY_INTERFACES: Final = {
    "LoadState": UNIT_INTERFACE,
    "ActiveState": UNIT_INTERFACE,
    "SubState": UNIT_INTERFACE,
    "UnitFileState": UNIT_INTERFACE,
    "InvocationID": UNIT_INTERFACE,
    "Job": UNIT_INTERFACE,
    "NeedDaemonReload": UNIT_INTERFACE,
    "Result": SERVICE_INTERFACE,
    "ExecMainStatus": SERVICE_INTERFACE,
    "SuccessExitStatus": SERVICE_INTERFACE,
    "MainPID": SERVICE_INTERFACE,
    "NRestarts": SERVICE_INTERFACE,
    "NextElapseUSecRealtime": TIMER_INTERFACE,
    "NextElapseUSecMonotonic": TIMER_INTERFACE,
}

_TIMEOUT_ERRORS: Final = frozenset(
    (
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Timeout",
        "org.freedesktop.DBus.Error.TimedOut",
    )
)
_UNAVAILABLE_ERRORS: Final = frozenset(
    (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.NoServer",
        "org.freedesktop.DBus.Error.NoNetwork",
        "org.freedesktop.DBus.Error.AccessDenied",
    )
)
NO_SUCH_JOB: Final = "org.freedesktop.systemd1.NoSuchJob"
NO_SUCH_UNIT: Final = "org.freedesktop.systemd1.NoSuchUnit"
_UNIT_FILE_ABSENT_ERRORS: Final = frozenset(
    (NO_SUCH_UNIT, "org.freedesktop.DBus.Error.FileNotFound", "org.freedesktop.systemd1.LoadFailed")
)


def _native(value: object) -> object:
    """dbus-python value -> plain python (bool before int: dbus.Boolean is
    an int subclass; a byte array arrives as bytes with byte_arrays=True)."""

    import dbus

    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, dbus.Struct):
        return tuple(_native(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    raise UserManagerProtocolError(f"unexpected D-Bus value type {type(value).__name__}")


class DBusUserManager:
    """``UserManagerPort`` on the seat's user bus. See the module docstring."""

    transport = TRANSPORT

    def __init__(
        self,
        *,
        uid: int,
        runtime_root: Path | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[str], object] | None = None,
    ) -> None:
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise UserManagerUnavailable("invalid seat uid")
        self.uid = uid
        root = DEFAULT_RUNTIME_ROOT if runtime_root is None else Path(runtime_root)
        self.address_source = ADDRESS_SOURCE_REGISTRY if runtime_root is None else ADDRESS_SOURCE_TEST_OVERRIDE
        self.socket_path = root / str(uid) / "bus"
        self.address: str | None = f"unix:path={self.socket_path}"
        self._call_timeout = float(call_timeout)
        self._sleep = sleep
        self._clock = clock
        self._connect = connect
        self._connection: object | None = None
        self._owner: str | None = None
        self._bound_deadline: float | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    # ------------------------------------------------------------------
    # the hard per-execution deadline (T-2917 5d.1, ruling T-2917:D11)
    # ------------------------------------------------------------------

    def bind_deadline(self, deadline: float | None) -> None:
        """Executor-only (deliberately NOT on ``UserManagerPort``, so the
        installer's port parity is untouched): bind the execution deadline,
        on this adapter's clock, or clear it with None. While bound, every
        call (timed or not: Reload, ResetFailedUnit, the enable/disable
        sequences, manager reads, EnvironmentFiles) gets a timeout no longer
        than the time left, every job wait ends by it, and a call once it has
        passed is refused with ``UserManagerCallFailed(DEADLINE_REACHED)``."""

        self._bound_deadline = None if deadline is None else float(deadline)

    def _attest_timeout(self) -> float:
        if self._bound_deadline is None:
            return self._call_timeout
        return max(0.001, min(self._call_timeout, self._bound_deadline - self._clock()))

    def _clamped(self, timeout: float) -> float:
        if self._bound_deadline is None:
            return timeout
        remaining = self._bound_deadline - self._clock()
        if remaining <= 0:
            raise UserManagerCallFailed(DEADLINE_REACHED, "the execution deadline has passed")
        return min(timeout, remaining)

    # ------------------------------------------------------------------
    # connection and attestation
    # ------------------------------------------------------------------

    def _open(self) -> object:
        try:
            info = os.stat(self.socket_path)
        except OSError as exc:
            raise UserManagerUnavailable("seat user bus socket is absent") from exc
        if not stat.S_ISSOCK(info.st_mode):
            raise UserManagerUnavailable("seat user bus path is not a socket")
        if info.st_uid != self.uid:
            raise UserManagerUnavailable("seat user bus socket is not owned by the seat uid")
        if self._connect is not None:
            return self._connect(str(self.address))
        import dbus.bus

        return dbus.bus.BusConnection(str(self.address))

    def _attested(self) -> tuple[object, str]:
        if self._connection is not None and self._owner is not None:
            return self._connection, self._owner
        connection = self._open()
        try:
            owner = str(
                connection.call_blocking(  # type: ignore[attr-defined]
                    BUS_DRIVER_NAME, BUS_DRIVER_PATH, BUS_DRIVER_NAME,
                    "GetNameOwner", "s", (SYSTEMD1_NAME,), timeout=self._attest_timeout(),
                )
            )
            owner_uid = int(
                connection.call_blocking(  # type: ignore[attr-defined]
                    BUS_DRIVER_NAME, BUS_DRIVER_PATH, BUS_DRIVER_NAME,
                    "GetConnectionUnixUser", "s", (owner,), timeout=self._attest_timeout(),
                )
            )
        except Exception as exc:
            self._close(connection)
            raise UserManagerUnavailable("the seat user bus could not attest the manager") from exc
        if owner_uid != self.uid:
            self._close(connection)
            raise UserManagerUnavailable("the user manager on the seat bus is not owned by the seat uid")
        self._connection, self._owner = connection, owner
        return connection, owner

    @staticmethod
    def _close(connection: object) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        if self._connection is not None:
            self._close(self._connection)
        self._connection = None
        self._owner = None

    def _typed_error(self, exc: Exception) -> UserManagerError:
        name = ""
        get_name = getattr(exc, "get_dbus_name", None)
        if callable(get_name):
            name = str(get_name() or "")
        message = getattr(exc, "get_dbus_message", None)
        detail = str(message() if callable(message) else exc)
        if name in _TIMEOUT_ERRORS:
            return UserManagerTimeout(f"user manager call timed out: {name}")
        if name in _UNAVAILABLE_ERRORS:
            self.close()
            return UserManagerUnavailable(f"user manager unavailable: {name}")
        return UserManagerCallFailed(name or "org.freedesktop.DBus.Error.Failed", detail)

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: tuple[object, ...],
        *,
        timeout: float | None = None,
    ) -> object:
        import dbus.exceptions

        effective = self._clamped(self._call_timeout if timeout is None else max(0.001, float(timeout)))
        connection, owner = self._attested()
        self.calls.append((f"{interface}.{method}", args))
        try:
            reply = connection.call_blocking(  # type: ignore[attr-defined]
                owner,
                path,
                interface,
                method,
                signature,
                args,
                timeout=max(0.001, effective),
                byte_arrays=True,
            )
        except dbus.exceptions.DBusException as exc:
            raise self._typed_error(exc) from None
        return reply

    def _manager(self, method: str, signature: str, args: tuple[object, ...], *, timeout: float | None = None) -> object:
        return self._call(MANAGER_PATH, MANAGER_INTERFACE, method, signature, args, timeout=timeout)

    def _get(self, path: str, interface: str, name: str, *, timeout: float | None = None) -> object:
        return _native(self._call(path, PROPERTIES_INTERFACE, "Get", "ss", (interface, name), timeout=timeout))

    def _unit_path(self, unit: str, *, timeout: float | None = None) -> str:
        return str(self._manager("LoadUnit", "s", (unit,), timeout=timeout))

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def manager_state(self) -> ManagerState:
        system_state = self._get(MANAGER_PATH, MANAGER_INTERFACE, "SystemState")
        version = self._get(MANAGER_PATH, MANAGER_INTERFACE, "Version")
        if not isinstance(system_state, str) or not isinstance(version, str):
            raise UserManagerProtocolError("manager state properties are not strings")
        return ManagerState(True, system_state[:64], version[:64])

    def manager_environment(self) -> int:
        environment = self._get(MANAGER_PATH, MANAGER_INTERFACE, "Environment")
        if not isinstance(environment, list) or not all(isinstance(item, str) for item in environment):
            raise UserManagerProtocolError("manager Environment is not a string list")
        return len(environment)

    def read_unit(self, unit: str, purpose: ReadPurpose, *, timeout: float | None = None) -> UnitSnapshot:
        path = self._unit_path(unit, timeout=timeout)
        values = {
            name: self._get(path, PROPERTY_INTERFACES[name], name, timeout=timeout)
            for name in purpose.properties
        }
        return unit_snapshot_from_native(unit, purpose, values, transport=TRANSPORT)

    def unit_file_enablement(self, unit: str, *, timeout: float | None = None) -> UnitFileEnablement:
        try:
            state = self._manager("GetUnitFileState", "s", (unit,), timeout=timeout)
        except UserManagerCallFailed as exc:
            if exc.error_name in _UNIT_FILE_ABSENT_ERRORS:
                return UnitFileEnablement("not-found", False)
            raise
        if not isinstance(state, str):
            raise UserManagerProtocolError("GetUnitFileState did not return a string")
        return UnitFileEnablement(str(state), str(state) in IS_ENABLED_ZERO_EXIT_STATES)

    def environment_file_declared(self, unit: str) -> bool:
        # D4: the typed list, not a substring of the unit file text.
        path = self._unit_path(unit)
        return environment_file_declared_from_native(self._get(path, SERVICE_INTERFACE, "EnvironmentFiles"))

    def need_daemon_reload(self, unit: str) -> bool:
        snapshot = self.read_unit(unit, ReadPurpose.RELOAD_STATE)
        return snapshot.need_daemon_reload is True

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    @staticmethod
    def _refused(exc: UserManagerCallFailed, evidence: dict[str, str]) -> UserManagerCallOutcome:
        return UserManagerCallOutcome(
            ok=False,
            error_name=exc.error_name,
            detail=exc.detail,
            transport_evidence={**evidence, "error_name": exc.error_name},
        )

    def _wait_job(self, job_path: str, deadline: float) -> None:
        """Wait until the queued job is gone (``GetJob`` -> NoSuchJob), the
        way the operator client waited for a blocking start/stop."""

        if not job_path.startswith(JOB_PATH_PREFIX):
            raise UserManagerProtocolError("job path is not a systemd1 job")
        try:
            job_id = int(job_path[len(JOB_PATH_PREFIX):])
        except ValueError as exc:
            raise UserManagerProtocolError("job path has no job id") from exc
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise UserManagerTimeout("user manager job did not finish within its budget")
            try:
                self._manager("GetJob", "u", (job_id,), timeout=remaining)
            except UserManagerCallFailed as exc:
                if exc.error_name == NO_SUCH_JOB:
                    return
                raise
            self._sleep(min(JOB_POLL_INTERVAL, max(0.0, deadline - self._clock())))

    def _deadline(self, timeout: float | None) -> float:
        deadline = self._clock() + (self._call_timeout if timeout is None else float(timeout))
        if self._bound_deadline is not None:
            deadline = min(deadline, self._bound_deadline)
        return deadline

    def reload(self) -> UserManagerCallOutcome:
        try:
            self._manager("Reload", "", ())
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "Reload"})
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": "Reload"})

    def reset_failed_unit(self, unit: str) -> UserManagerCallOutcome:
        try:
            self._manager("ResetFailedUnit", "s", (unit,))
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "ResetFailedUnit"})
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": "ResetFailedUnit"})

    def start_unit(self, unit: str, *, wait: bool, timeout: float | None = None) -> UserManagerCallOutcome:
        deadline = self._deadline(timeout)
        try:
            job = str(self._manager("StartUnit", "ss", (unit, "replace"), timeout=deadline - self._clock()))
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "StartUnit"})
        if wait:
            self._wait_job(job, deadline)
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": "StartUnit", "waited": str(wait).lower()})

    def stop_unit(self, unit: str, *, timeout: float | None = None) -> UserManagerCallOutcome:
        deadline = self._deadline(timeout)
        try:
            job = str(self._manager("StopUnit", "ss", (unit, "replace"), timeout=deadline - self._clock()))
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "StopUnit"})
        self._wait_job(job, deadline)
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": "StopUnit", "waited": "true"})

    def enable_now(self, unit: str) -> UserManagerCallOutcome:
        deadline = self._deadline(None)
        try:
            reply = self._manager("EnableUnitFiles", "asbb", ([unit], False, False))
            changes = _native(reply[1]) if isinstance(reply, (list, tuple)) and len(reply) == 2 else None  # type: ignore[index]
            if not isinstance(changes, list):
                raise UserManagerProtocolError("EnableUnitFiles reply is not (carries_install_info, changes)")
            self._manager("Reload", "", ())
            job = str(self._manager("StartUnit", "ss", (unit, "replace"), timeout=deadline - self._clock()))
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "EnableUnitFiles+Reload+StartUnit"})
        self._wait_job(job, deadline)
        return UserManagerCallOutcome(
            ok=True,
            transport_evidence={"call": "EnableUnitFiles+Reload+StartUnit", "changes": str(len(changes))},
        )

    def disable_now(self, unit: str) -> UserManagerCallOutcome:
        deadline = self._deadline(None)
        try:
            self._manager("DisableUnitFiles", "asb", ([unit], False))
            self._manager("Reload", "", ())
            job = str(self._manager("StopUnit", "ss", (unit, "replace"), timeout=deadline - self._clock()))
        except UserManagerCallFailed as exc:
            return self._refused(exc, {"call": "DisableUnitFiles+Reload+StopUnit"})
        self._wait_job(job, deadline)
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": "DisableUnitFiles+Reload+StopUnit"})
