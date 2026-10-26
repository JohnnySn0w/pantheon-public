"""Strict parser + trusted loader for the root-owned ingress config
(T-2919).

The config names exactly ONE allowlisted human (user id + chat id; T-2919
Q5), the ingress and approval uids, the ORG-051 host-file credential
reference for the dedicated bot token, and the private state directory.
It carries NO secret and NO API base URL: the Bot API base is a compiled
constant (``bot_api.TELEGRAM_API_BASE``), so a config file can never
redirect the token anywhere else.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

CONFIG_SCHEMA = "pantheon-telegram-ingress-config/v1"
MAX_CONFIG_BYTES = 65536
_TOP_KEYS = frozenset(
    (
        "schema",
        "ingress_uid",
        "approval_uid",
        "credential",
        "allowlist",
        "state_dir",
        "card_ttl_max_seconds",
        "poll_timeout_seconds",
    )
)
_CREDENTIAL_KEYS = frozenset(("credential_id", "location", "owner_gid"))
_ALLOWLIST_KEYS = frozenset(("user_id", "chat_id"))
# Telegram ids fit in 52 significant bits (Bot API: Chat.id); keep ints.
_MAX_PROVIDER_ID = 2**52


class IngressConfigError(ValueError):
    """The ingress config is untrusted, malformed, or outside policy."""


@dataclass(frozen=True)
class IngressConfig:
    ingress_uid: int
    approval_uid: int
    credential_id: str
    credential_location: Path
    credential_owner_gid: int
    allow_user_id: int
    allow_chat_id: int
    state_dir: Path
    card_ttl_max_seconds: int
    poll_timeout_seconds: int


def _uid(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IngressConfigError(f"invalid {label}")
    return value


def _provider_id(value: object, label: str, *, allow_negative: bool) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IngressConfigError(f"invalid {label}")
    if abs(value) >= _MAX_PROVIDER_ID or value == 0:
        raise IngressConfigError(f"invalid {label}")
    if value < 0 and not allow_negative:
        raise IngressConfigError(f"invalid {label}")
    return value


def _abs_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise IngressConfigError(f"invalid {label}")
    path = Path(value)
    if ".." in path.parts:
        raise IngressConfigError(f"invalid {label}")
    return path


def _keys(raw: object, expected: frozenset[str], where: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != expected:
        raise IngressConfigError(f"invalid {where} keys")
    return raw


def parse_ingress_config(raw: object) -> IngressConfig:
    top = _keys(raw, _TOP_KEYS, "config")
    if top["schema"] != CONFIG_SCHEMA:
        raise IngressConfigError("unsupported config schema")
    ingress_uid = _uid(top["ingress_uid"], "ingress uid")
    approval_uid = _uid(top["approval_uid"], "approval uid")
    if ingress_uid == approval_uid or ingress_uid == 0:
        raise IngressConfigError("ingress uid must be dedicated")
    credential = _keys(top["credential"], _CREDENTIAL_KEYS, "credential")
    credential_id = credential["credential_id"]
    if (
        not isinstance(credential_id, str)
        or not 1 <= len(credential_id) <= 64
        or not all(ch.isalnum() or ch in "-_." for ch in credential_id)
    ):
        raise IngressConfigError("invalid credential id")
    location = _abs_path(credential["location"], "credential location")
    owner_gid = _uid(credential["owner_gid"], "credential owner gid")
    allowlist = _keys(top["allowlist"], _ALLOWLIST_KEYS, "allowlist")
    allow_user_id = _provider_id(
        allowlist["user_id"], "allowlisted user id", allow_negative=False
    )
    allow_chat_id = _provider_id(
        allowlist["chat_id"], "allowlisted chat id", allow_negative=True
    )
    if allow_chat_id != allow_user_id:
        # a PRIVATE chat only (review finding 6): in Telegram a private
        # chat's id equals the user's id; group ids differ (negative)
        raise IngressConfigError("allowlisted chat must be the user's private chat")
    state_dir = _abs_path(top["state_dir"], "state dir")
    if location.parent == state_dir or state_dir in location.parents:
        # the token never lives in the writable state directory
        raise IngressConfigError("credential must not live in the state dir")
    ttl = top["card_ttl_max_seconds"]
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= 86400:
        raise IngressConfigError("invalid card ttl")
    poll = top["poll_timeout_seconds"]
    if not isinstance(poll, int) or isinstance(poll, bool) or not 0 <= poll <= 5:
        raise IngressConfigError("invalid poll timeout")
    return IngressConfig(
        ingress_uid=ingress_uid,
        approval_uid=approval_uid,
        credential_id=credential_id,
        credential_location=location,
        credential_owner_gid=owner_gid,
        allow_user_id=allow_user_id,
        allow_chat_id=allow_chat_id,
        state_dir=state_dir,
        card_ttl_max_seconds=ttl,
        poll_timeout_seconds=poll,
    )


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise IngressConfigError("duplicate config key")
        result[key] = value
    return result


def load_ingress_config(
    path: Path,
    *,
    expected_owner_uid: int = 0,
) -> IngressConfig:
    """Read the config through a trust check: absolute path, no symlink
    follow, owned by ``expected_owner_uid`` (root in production), not
    group/other-writable, bounded size, duplicate-key-free JSON."""

    if not path.is_absolute():
        raise IngressConfigError("config path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise IngressConfigError("config is unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise IngressConfigError("config is not a regular file")
        if info.st_uid != expected_owner_uid:
            raise IngressConfigError("config has the wrong owner")
        if info.st_mode & 0o022:
            raise IngressConfigError("config is group/other-writable")
        data = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_CONFIG_BYTES:
        raise IngressConfigError("config is too large")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IngressConfigError("config is not valid JSON") from None
    return parse_ingress_config(raw)
