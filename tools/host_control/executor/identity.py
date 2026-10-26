"""Executor identity: which seat am I? (T-2917 5b; ORG-052 sections 2-3,
gate 19 executor half).

The ONLY inputs are ``getuid()`` and the root-owned seat registry. The unit
instance name, argv, environment and every request parameter are ignored
for identity: a mis-instantiated unit still resolves to the seat its uid is
registered for, and an unregistered uid refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tools.host_control.contracts import HostControlError, RegistryError, SeatRecord
from tools.host_control.executor.wire import bus_name_for_uid
from tools.host_control.registry import SeatRegistry


class ExecutorStartRefused(HostControlError):
    """The executor must not start (unregistered uid, inconsistent registry)."""


@dataclass(frozen=True)
class ExecutorIdentity:
    uid: int
    seat: SeatRecord
    bus_name: str
    # <install_root>/home/<account>: the same derivation the installer uses
    # for the account home it declares as an allowed target root.
    seat_root: Path


def seat_root_for(seat: SeatRecord) -> Path:
    home = Path("/home") / seat.account
    if seat.install_root == Path("/"):
        return home
    return seat.install_root / home.relative_to("/")


def resolve_executor_identity(
    *,
    getuid: Callable[[], int],
    registry: SeatRegistry,
) -> ExecutorIdentity:
    uid = getuid()
    try:
        subject = registry.subject_for_uid(uid)
        seat = registry.seat(subject.seat_id)
    except RegistryError as exc:
        raise ExecutorStartRefused("executor uid is not a registered seat") from exc
    if seat.uid != uid:
        raise ExecutorStartRefused("registry seat uid does not match the executor uid")
    return ExecutorIdentity(
        uid=uid,
        seat=seat,
        bus_name=bus_name_for_uid(seat.uid),
        seat_root=seat_root_for(seat),
    )
