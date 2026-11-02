"""Strict parser for Pantheon's single machine-authoritative MCP registry."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

import yaml


SCHEMA = "pantheon-mcp-allowlist/v1"
MAX_REGISTRY_BYTES = 1024 * 1024


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryServer:
    seats: frozenset[str]
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    servers: Mapping[str, RegistryServer]
    fingerprints: Mapping[str, Mapping[str, Any]]


def _plain(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def _relative(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not Path(value).is_absolute() and ".." not in Path(value).parts


def _plugin_key(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+", value) is not None


def _string_list(value: Any, *, nested: bool = False) -> bool:
    if not isinstance(value, list):
        return False
    if nested:
        return bool(value) and all(isinstance(row, list) and all(isinstance(v, str) for v in row) for row in value)
    return all(isinstance(v, str) for v in value)


def _fingerprint(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RegistryError("fingerprint must be a mapping")
    category = raw.get("category")
    if category not in {"claude", "plugin", "codex"} or raw.get("transport") != "stdio":
        raise RegistryError("fingerprint category or transport is invalid")
    if not isinstance(raw.get("command"), str) or not raw["command"] or "\n" in raw["command"]:
        raise RegistryError("fingerprint command is invalid")
    common = {"category", "transport", "command"}
    if category == "plugin":
        allowed = common | {"plugin", "argv_shapes"}
        if set(raw) != allowed or not _plugin_key(raw.get("plugin")) or not _string_list(raw.get("argv_shapes"), nested=True):
            raise RegistryError("plugin fingerprint shape is invalid")
    elif category == "codex":
        allowed = common | {"repo_script", "argv_tail"}
        if not set(raw).issubset(allowed) or set(raw) < (common | {"repo_script"}):
            raise RegistryError("codex fingerprint shape is invalid")
        if not _relative(raw.get("repo_script")) or ("argv_tail" in raw and not _string_list(raw["argv_tail"])):
            raise RegistryError("codex fingerprint values are invalid")
    else:
        mechanisms = [name for name in ("repo_script", "home_script", "argv_shapes") if name in raw]
        if len(mechanisms) != 1 or set(raw) != common | {mechanisms[0]}:
            raise RegistryError("claude fingerprint shape is invalid")
        if mechanisms[0] == "argv_shapes":
            if not _string_list(raw["argv_shapes"], nested=True):
                raise RegistryError("claude argv shapes are invalid")
        elif not _relative(raw[mechanisms[0]]):
            raise RegistryError("claude script path is invalid")
    return dict(raw)


def parse_registry(raw: Any) -> Registry:
    if not isinstance(raw, Mapping) or set(raw) != {"schema", "servers", "fingerprints"} or raw.get("schema") != SCHEMA:
        raise RegistryError("registry root is invalid")
    servers_raw, fingerprints_raw = raw["servers"], raw["fingerprints"]
    if not isinstance(servers_raw, Mapping) or not servers_raw or not isinstance(fingerprints_raw, Mapping):
        raise RegistryError("registry maps are invalid")
    fingerprints: dict[str, Mapping[str, Any]] = {}
    for name, profile in fingerprints_raw.items():
        if not _plain(name):
            raise RegistryError("fingerprint name is invalid")
        fingerprints[name] = _fingerprint(profile)
    servers: dict[str, RegistryServer] = {}
    referenced: set[str] = set()
    for name, item in servers_raw.items():
        if not _plain(name) or not isinstance(item, Mapping) or set(item) != {"seats", "profiles"}:
            raise RegistryError("server row is invalid")
        seats, profiles = item["seats"], item["profiles"]
        if (not isinstance(seats, list) or not all(_plain(v) for v in seats) or len(seats) != len(set(seats))
                or not isinstance(profiles, list) or not profiles or not all(_plain(v) for v in profiles)
                or len(profiles) != len(set(profiles)) or any(v not in fingerprints for v in profiles)):
            raise RegistryError("server membership or profiles are invalid")
        servers[name] = RegistryServer(frozenset(seats), tuple(profiles))
        referenced.update(profiles)
    if referenced != set(fingerprints):
        raise RegistryError("unreferenced or missing fingerprint")
    return Registry(servers, fingerprints)


def load_registry(path: Path) -> Registry:
    fd = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REGISTRY_BYTES:
            raise RegistryError("registry is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        after = os.fstat(fd)
        if (not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size > MAX_REGISTRY_BYTES):
            raise RegistryError("registry changed or is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = MAX_REGISTRY_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_REGISTRY_BYTES:
            raise RegistryError("registry is oversized")
        return parse_registry(yaml.safe_load(data))
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError("registry is unreadable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
