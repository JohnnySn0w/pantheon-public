"""Shared strict-parse helpers for the broker and approval daemon root configs
(T-2920 U2).

Shared substrate in the ``trusted_files`` sense: pure value checks importing
only ``RegistryError``, no journal, plan, operation or approval surface, so the
broker (``broker_config``) and the approval tree (``approval/config``) parse
their closed, versioned schemas by the same rules as the executor config
(``executor/config.py``: absolute, normalized, component-wise overlap) without
either importing the other's package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Iterable

from tools.host_control.contracts import RegistryError

MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_TRUSTED_ROOTS: Final = 8
MAX_MONITOR_UIDS: Final = 8


def closed_keys(raw: object, required: frozenset[str], optional: frozenset[str], what: str) -> dict:
    """``raw`` is an object whose keys are exactly ``required`` plus any subset
    of ``optional`` (closed schema: unknown and missing keys are refused)."""

    if not isinstance(raw, dict):
        raise RegistryError(f"invalid {what} fields")
    keys = set(raw)
    if not required <= keys or not keys <= (required | optional):
        raise RegistryError(f"invalid {what} fields")
    return raw


def absolute_path(value: object, context: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise RegistryError(f"{context} must be absolute")
    # POSIX normpath keeps exactly two leading slashes (executor D32 fixup)
    if value.startswith("//") or os.path.normpath(value) != value or ".." in Path(value).parts:
        raise RegistryError(f"{context} must be normalized")
    if value == "/":
        raise RegistryError(f"{context} must not be the filesystem root")
    return Path(value)


def uid_value(value: object, context: str) -> int:
    """A service-account uid: a non-bool int > 0. Root (uid 0) is refused:
    every uid these configs name (approval, broker, ingress, health monitor)
    is an unprivileged dedicated account (ORG-052 gate 19), and naming root
    would hand root a role on the bus surface."""

    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RegistryError(f"invalid {context}")
    return value


def trusted_roots(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_TRUSTED_ROOTS:
        raise RegistryError("invalid trusted roots")
    roots = tuple(absolute_path(item, "trusted root") for item in value)
    if len(set(roots)) != len(roots):
        raise RegistryError("duplicate trusted root")
    return roots


def monitor_uids(value: object) -> frozenset[int]:
    if not isinstance(value, list) or not value or len(value) > MAX_MONITOR_UIDS:
        raise RegistryError("invalid health monitor uids")
    uids = tuple(uid_value(item, "health monitor uid") for item in value)
    if len(set(uids)) != len(uids):
        raise RegistryError("duplicate health monitor uid")
    return frozenset(uids)


def paths_overlap(first: Path, second: Path) -> bool:
    """Equal, or one contains the other, by PATH COMPONENTS."""

    return first == second or second in first.parents or first in second.parents


def inside_roots(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def require_file_inside(path: Path, roots: tuple[Path, ...], what: str) -> None:
    """A root-owned FILE input sits under a trusted root (never equal to one)."""

    if not inside_roots(path, roots) or path in roots:
        raise RegistryError(f"{what} is outside trusted roots")


def require_state_outside(path: Path, roots: tuple[Path, ...], what: str) -> None:
    """Writable daemon state never overlaps a root-owned trusted root."""

    for root in roots:
        if paths_overlap(path, root):
            raise RegistryError(f"{what} overlaps a trusted root")


def bounded_text(value: object, context: str, *, limit: int = 128) -> str:
    """Printable ASCII, no spaces, 1..``limit`` characters."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
    ):
        raise RegistryError(f"invalid {context}")
    return value

