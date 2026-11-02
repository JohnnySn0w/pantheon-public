"""Root-owned approval daemon configuration (T-2920 U2; ORG-052 section 5).

Schema ``pantheon-host-approval-config/v1``: closed (unknown and missing keys
refused, the nested ``human_ingress`` object too), versioned, strictly parsed,
read ONLY through ``RootOwnedFileLoader``. House style: ``executor/config.py``.

operator-only values appear here ONLY as references: the approver identities
live in the root-owned approver policy (a path), the human is a policy subject
name plus provider ids, and the bot token never reaches this service at all
(the separately supervised ingress leases it through its own config). The
default path is PROVISIONAL pending the T-2920 name ratification (beside the
provisional ``/etc/pantheon/host-approval/approver-policy.json``).

``human_ingress`` builds the ``HumanIngressRegistration``
(``approval/human.py``): provider ``telegram`` is fixed in code, the ids are
decimal strings, and the registration itself refuses a chat id that is not the
user's private chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.host_control.approval.human import HumanIngressRegistration
from tools.host_control.contracts import HostControlError, RegistryError
from tools.host_control.daemon_config import (
    MAX_CONFIG_BYTES,
    absolute_path,
    closed_keys,
    monitor_uids,
    require_file_inside,
    require_state_outside,
    trusted_roots,
    uid_value,
)
from tools.host_control.trusted_files import RootOwnedFileLoader

APPROVAL_CONFIG_SCHEMA: Final = "pantheon-host-approval-config/v1"
DEFAULT_APPROVAL_CONFIG_PATH: Final = Path("/etc/pantheon/host-approval/config.json")
APPROVAL_CONFIG_KEYS: Final = frozenset(
    (
        "schema",
        "trusted_roots",
        "approver_policy_path",
        "journal_path",
        "broker_uid",
        "monitor_uids",
        "human_ingress",
    )
)
HUMAN_INGRESS_KEYS: Final = frozenset(
    ("ingress_uid", "human_subject", "provider_user_id", "provider_chat_id")
)
HUMAN_INGRESS_PROVIDER: Final = "telegram"


@dataclass(frozen=True)
class ApprovalConfig:
    trusted_roots: tuple[Path, ...]
    approver_policy_path: Path
    journal_path: Path
    broker_uid: int
    monitor_uids: frozenset[int]
    human_ingress: HumanIngressRegistration


def parse_approval_config(raw: object) -> ApprovalConfig:
    raw = closed_keys(raw, APPROVAL_CONFIG_KEYS, frozenset(), "approval config")
    if raw["schema"] != APPROVAL_CONFIG_SCHEMA:
        raise RegistryError("unsupported approval config schema")
    roots = trusted_roots(raw["trusted_roots"])
    policy_path = absolute_path(raw["approver_policy_path"], "approver policy path")
    journal_path = absolute_path(raw["journal_path"], "journal path")
    require_file_inside(policy_path, roots, "approver policy path")
    require_state_outside(journal_path, roots, "journal path")
    broker_uid = uid_value(raw["broker_uid"], "broker uid")
    monitors = monitor_uids(raw["monitor_uids"])
    ingress = closed_keys(raw["human_ingress"], HUMAN_INGRESS_KEYS, frozenset(), "human ingress")
    ingress_uid = uid_value(ingress["ingress_uid"], "human ingress uid")
    if ingress_uid == broker_uid:
        raise RegistryError("human ingress uid must not be the broker uid")
    for key in ("human_subject", "provider_user_id", "provider_chat_id"):
        if not isinstance(ingress[key], str):
            raise RegistryError(f"invalid human ingress {key}")
    try:
        registration = HumanIngressRegistration(
            uid=ingress_uid,
            provider=HUMAN_INGRESS_PROVIDER,
            human_subject=ingress["human_subject"],
            provider_user_id=ingress["provider_user_id"],
            provider_chat_id=ingress["provider_chat_id"],
        )
    except HostControlError as exc:
        raise RegistryError(f"invalid human ingress: {exc}") from None
    return ApprovalConfig(
        trusted_roots=roots,
        approver_policy_path=policy_path,
        journal_path=journal_path,
        broker_uid=broker_uid,
        monitor_uids=monitors,
        human_ingress=registration,
    )


def load_approval_config(path: Path, *, files: RootOwnedFileLoader) -> ApprovalConfig:
    return parse_approval_config(files.read_json(path, max_bytes=MAX_CONFIG_BYTES))
