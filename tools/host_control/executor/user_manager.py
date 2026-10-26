"""The target user manager port, as the executor sees it (T-2917 5b/5c.1; ruling B10).

The executor reaches its own seat's user manager through this port and
never through the operator command line and its private control socket
(the founding-incident path, ORG-052 gate 39). Since 5c.1 the port itself
lives in the neutral module ``tools.user_systemd``; this module re-exports
it for the executor and keeps the fail-closed default. The real user-bus
``org.freedesktop.systemd1`` adapter and the E39 proof land in 5c.2.
"""

from __future__ import annotations

from typing import Mapping

from tools.host_control.contracts import HostControlError
from tools.user_systemd import (
    IS_ENABLED_ZERO_EXIT_STATES,
    Elapse,
    ExitPolicy,
    ManagerState,
    ReadPurpose,
    SnapshotSource,
    UnitFileEnablement,
    UnitSnapshot,
    UserManagerCallFailed,
    UserManagerCallOutcome,
    UserManagerError,
    UserManagerPort,
    UserManagerProtocolError,
    UserManagerTimeout,
)
from tools.user_systemd import UserManagerUnavailable as _PortUserManagerUnavailable

__all__ = [
    "IS_ENABLED_ZERO_EXIT_STATES",
    "Elapse",
    "ExitPolicy",
    "ManagerState",
    "ReadPurpose",
    "SnapshotSource",
    "UnavailableUserManager",
    "UnitFileEnablement",
    "UnitSnapshot",
    "UserManagerCallFailed",
    "UserManagerCallOutcome",
    "UserManagerError",
    "UserManagerPort",
    "UserManagerProtocolError",
    "UserManagerTimeout",
    "UserManagerUnavailable",
]


class UserManagerUnavailable(HostControlError, _PortUserManagerUnavailable):
    """The target user manager could not be reached; fail closed.

    Both a HostControlError (so the engine's ``except HostControlError``
    still catches it) and the port's UserManagerUnavailable."""


_NOT_CONFIGURED = "user manager port is not configured"


class UnavailableUserManager:
    """Fail-closed default until the 5c.2 systemd1 user-bus adapter. Every
    port method raises UserManagerUnavailable."""

    transport = "unavailable"
    address: str | None = None
    address_source = "n/a"

    # port reads
    def manager_state(self) -> ManagerState:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def manager_environment(self) -> int:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def read_unit(self, unit: str, purpose: ReadPurpose, *, timeout: float | None = None) -> UnitSnapshot:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def unit_file_enablement(self, unit: str, *, timeout: float | None = None) -> UnitFileEnablement:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def environment_file_declared(self, unit: str) -> bool:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    # port writes
    def reload(self) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def reset_failed_unit(self, unit: str) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def start_unit(self, unit: str, *, wait: bool, timeout: float | None = None) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def stop_unit(self, unit: str, *, timeout: float | None = None) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def enable_now(self, unit: str) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def disable_now(self, unit: str) -> UserManagerCallOutcome:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    # 5b read-side signatures, kept so nothing written against 5b breaks
    def unit_properties(self, unit: str, names: tuple[str, ...]) -> Mapping[str, str]:
        raise UserManagerUnavailable(_NOT_CONFIGURED)

    def unit_file_state(self, unit: str) -> str:
        raise UserManagerUnavailable(_NOT_CONFIGURED)
