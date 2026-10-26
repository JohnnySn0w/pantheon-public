"""Root-owned executor configuration (T-2917 5b, ruling B5).

PROVISIONAL: the schema ``pantheon-host-executor-config/v1`` and the default
path are minted here so the executor can start and enforce its two-sided
identity checks; T-2920 owns the final install path and schema. The file is
read ONLY through ``RootOwnedFileLoader`` (root-owned, not group/world
writable, no symlink anywhere on the path).

T-2917 5d.3 (ruling T-2917:D19, PROVISIONAL until T-2920): the v1 schema
gains ``mailbox_hub``, the absolute mailbox hub the executor's mailbox
probes are confined to. It is the ONLY source of the hub (never the
environment, never the plan alone); the staged unit grants
``ReadWritePaths=@MAILBOX_HUB@`` for it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.host_control.contracts import RegistryError
from tools.host_control.trusted_files import RootOwnedFileLoader

EXECUTOR_CONFIG_SCHEMA: Final = "pantheon-host-executor-config/v1"
DEFAULT_CONFIG_PATH: Final = Path("/etc/pantheon/host-executor/config.json")
# T-2917:D32: system trees the mailbox hub may never equal, contain or sit
# inside (compared by path components, never by string prefix)
SYSTEM_TREES: Final = tuple(
    Path(item)
    for item in (
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/boot",
        "/var/lib/systemd",
        "/proc",
        "/sys",
        "/dev",
        "/run/systemd",
    )
)
_KEYS: Final = frozenset(
    ("schema", "registry_path", "trusted_roots", "broker_uid", "framework_root", "mailbox_hub")
)


def _absolute(value: object, context: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise RegistryError(f"{context} must be absolute")
    # T-2917 MR-2 fixup 2 (D32): POSIX normpath KEEPS exactly two leading
    # slashes, so "//etc" passed the equality check while pathlib compares it
    # unequal to "/etc" (the kernel resolves both to /etc)
    if value.startswith("//") or os.path.normpath(value) != value or ".." in Path(value).parts:
        raise RegistryError(f"{context} must be normalized")
    return Path(value)


@dataclass(frozen=True)
class ExecutorConfig:
    registry_path: Path
    trusted_roots: tuple[Path, ...]
    broker_uid: int
    # The root-owned registry copy of the framework: the ONLY place the
    # repo-sourced checks read (HO ruling T-4861: never a seat checkout).
    framework_root: Path
    # T-2917 5d.3 (T-2917:D19): the hub mailbox probes are confined to
    mailbox_hub: Path


def parse_executor_config(raw: object) -> ExecutorConfig:
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise RegistryError("invalid executor config fields")
    if raw["schema"] != EXECUTOR_CONFIG_SCHEMA:
        raise RegistryError("unsupported executor config schema")
    roots_raw = raw["trusted_roots"]
    if not isinstance(roots_raw, list) or not roots_raw or len(roots_raw) > 8:
        raise RegistryError("invalid executor trusted roots")
    roots = tuple(_absolute(item, "trusted root") for item in roots_raw)
    if len(set(roots)) != len(roots):
        raise RegistryError("duplicate executor trusted root")
    broker_uid = raw["broker_uid"]
    if not isinstance(broker_uid, int) or isinstance(broker_uid, bool) or broker_uid < 0:
        raise RegistryError("invalid broker uid")
    registry_path = _absolute(raw["registry_path"], "registry path")
    framework_root = _absolute(raw["framework_root"], "framework root")
    mailbox_hub = _absolute(raw["mailbox_hub"], "mailbox hub")
    if mailbox_hub == Path("/"):
        raise RegistryError("mailbox hub must not be the filesystem root")
    # T-2917:D32: the hub's write surface is disjoint from every root-owned
    # input of the executor and from the system trees
    for other, what in (
        *((root, "a trusted root") for root in roots),
        (framework_root, "the framework root"),
        (registry_path, "the registry path"),
    ):
        if paths_overlap(mailbox_hub, other):
            raise RegistryError(f"mailbox hub overlaps {what}")
    for tree in SYSTEM_TREES:
        # ``/`` contains every path: for it only equality is a refusal
        # (handled above); every other tree is refused equal, containing or
        # contained
        if tree != Path("/") and paths_overlap(mailbox_hub, tree):
            raise RegistryError("mailbox hub overlaps a system tree")

    def inside(path: Path) -> bool:
        return any(path == root or root in path.parents for root in roots)

    if not inside(registry_path) or registry_path in roots:
        raise RegistryError("registry path is outside trusted roots")
    if not inside(framework_root):
        raise RegistryError("framework root is outside trusted roots")
    return ExecutorConfig(
        registry_path=registry_path,
        trusted_roots=roots,
        broker_uid=broker_uid,
        framework_root=framework_root,
        mailbox_hub=mailbox_hub,
    )


def paths_overlap(first: Path, second: Path) -> bool:
    """Equal, or one contains the other, by PATH COMPONENTS (``/etc2`` does
    not overlap ``/etc``; ``/etc/x`` does)."""

    return first == second or second in first.parents or first in second.parents


def require_hub_without_symlinks(hub: Path, *, lstat=os.lstat) -> None:
    """T-2917:D33, the entrypoint's pre-connection check (config parse stays
    pure): lstat every EXISTING component of the hub from ``/`` downward and
    refuse a symlink anywhere. A hub that does not exist yet has its existing
    prefix checked; any other lstat failure refuses (fail closed)."""

    current = Path("/")
    for component in hub.parts[1:]:
        current = current / component
        try:
            value = lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RegistryError("mailbox hub component cannot be inspected") from exc
        if stat.S_ISLNK(value.st_mode):
            raise RegistryError("mailbox hub path contains a symlink component")


def require_hub_outside_seat_root(config: ExecutorConfig, seat_root: Path) -> None:
    """T-2917:D19/D32: the hub is not inside the seat root (nor the seat root
    inside the hub): the two write surfaces stay disjoint."""

    if paths_overlap(config.mailbox_hub, seat_root):
        raise RegistryError("mailbox hub overlaps the seat root")


def load_executor_config(path: Path, *, files: RootOwnedFileLoader) -> ExecutorConfig:
    return parse_executor_config(files.read_json(path, max_bytes=64 * 1024))
