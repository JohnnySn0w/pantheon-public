"""Root-owned broker daemon configuration (T-2920 U2; ORG-052 section 3).

Schema ``pantheon-host-broker-config/v1``: closed (unknown and missing keys
refused), versioned, strictly parsed, read ONLY through ``RootOwnedFileLoader``
(root-owned, not group/world writable, no symlink anywhere on the path, no
duplicate JSON keys). House style: ``executor/config.py``.

The default path is PROVISIONAL pending the T-2920 name ratification (beside the
provisional ``/etc/pantheon/host-broker/seat-registry.json``). The broker holds
no secret, so the schema carries none: identities are uids, everything else is
a path to another root-owned file.

``framework_root`` is the CONCRETE versioned framework copy
(``/usr/local/lib/pantheon/framework/<rev>``, HO ruling Q6), never the atomic
``active`` symlink: the root-owned loader opens every component O_NOFOLLOW, and
the registered plans are bound to one revision. A final component named
``active`` is refused here, before any file is opened.

``compensate`` (T-4964, HO ruling Q4) is the one optional key: absent means OFF.
It turns on only after gate-26/30 fault injection on a disposable host.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.host_control.contracts import RegistryError
from tools.host_control.daemon_config import (
    MAX_CONFIG_BYTES,
    absolute_path,
    bounded_text,
    closed_keys,
    inside_roots,
    monitor_uids,
    require_file_inside,
    require_state_outside,
    trusted_roots,
    uid_value,
)
from tools.host_control.trusted_files import RootOwnedFileLoader

BROKER_CONFIG_SCHEMA: Final = "pantheon-host-broker-config/v1"
DEFAULT_BROKER_CONFIG_PATH: Final = Path("/etc/pantheon/host-broker/config.json")
BROKER_CONFIG_REQUIRED_KEYS: Final = frozenset(
    (
        "schema",
        "trusted_roots",
        "registry_path",
        "policy_path",
        "framework_root",
        "journal_path",
        "approval_uid",
        "monitor_uids",
        "host_operation_families",
        "plan_ttl_seconds",
        "broker_revision",
    )
)
BROKER_CONFIG_OPTIONAL_KEYS: Final = frozenset(("compensate",))
MIN_PLAN_TTL_SECONDS: Final = 60
MAX_PLAN_TTL_SECONDS: Final = 86400
MAX_OPERATION_FAMILIES: Final = 32
FRAMEWORK_ACTIVE_LINK: Final = "active"


@dataclass(frozen=True)
class BrokerConfig:
    trusted_roots: tuple[Path, ...]
    registry_path: Path
    policy_path: Path
    framework_root: Path
    journal_path: Path
    approval_uid: int
    monitor_uids: frozenset[int]
    host_operation_families: frozenset[str]
    plan_ttl_seconds: int
    broker_revision: str
    compensate: bool = False


def parse_broker_config(raw: object) -> BrokerConfig:
    raw = closed_keys(raw, BROKER_CONFIG_REQUIRED_KEYS, BROKER_CONFIG_OPTIONAL_KEYS, "broker config")
    if raw["schema"] != BROKER_CONFIG_SCHEMA:
        raise RegistryError("unsupported broker config schema")
    roots = trusted_roots(raw["trusted_roots"])
    registry_path = absolute_path(raw["registry_path"], "registry path")
    policy_path = absolute_path(raw["policy_path"], "policy path")
    framework_root = absolute_path(raw["framework_root"], "framework root")
    journal_path = absolute_path(raw["journal_path"], "journal path")
    require_file_inside(registry_path, roots, "registry path")
    require_file_inside(policy_path, roots, "policy path")
    if not inside_roots(framework_root, roots):
        raise RegistryError("framework root is outside trusted roots")
    if framework_root.name == FRAMEWORK_ACTIVE_LINK:
        # U1 reconcile point: the concrete <rev> copy, never the symlink
        raise RegistryError("framework root must be the concrete revision, not the active link")
    require_state_outside(journal_path, roots, "journal path")
    require_state_outside(journal_path, (framework_root,), "journal path")
    approval_uid = uid_value(raw["approval_uid"], "approval uid")
    monitors = monitor_uids(raw["monitor_uids"])
    families_raw = raw["host_operation_families"]
    if not isinstance(families_raw, list) or not families_raw or len(families_raw) > MAX_OPERATION_FAMILIES:
        raise RegistryError("invalid host operation families")
    families = tuple(bounded_text(item, "host operation family") for item in families_raw)
    if len(set(families)) != len(families):
        raise RegistryError("duplicate host operation family")
    ttl = raw["plan_ttl_seconds"]
    if (
        not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or not MIN_PLAN_TTL_SECONDS <= ttl <= MAX_PLAN_TTL_SECONDS
    ):
        raise RegistryError("invalid plan ttl")
    compensate = raw.get("compensate", False)
    if not isinstance(compensate, bool):
        raise RegistryError("invalid compensate flag")
    return BrokerConfig(
        trusted_roots=roots,
        registry_path=registry_path,
        policy_path=policy_path,
        framework_root=framework_root,
        journal_path=journal_path,
        approval_uid=approval_uid,
        monitor_uids=monitors,
        host_operation_families=frozenset(families),
        plan_ttl_seconds=ttl,
        broker_revision=bounded_text(raw["broker_revision"], "broker revision"),
        compensate=compensate,
    )


def load_broker_config(path: Path, *, files: RootOwnedFileLoader) -> BrokerConfig:
    return parse_broker_config(files.read_json(path, max_bytes=MAX_CONFIG_BYTES))
