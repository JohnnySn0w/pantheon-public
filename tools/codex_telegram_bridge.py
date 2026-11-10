#!/usr/bin/env python3
"""Target-local Codex Telegram bridge controls for T-2950.

This module deliberately keeps the live surface narrow: deploy verified
codex bridge artifacts (pinned harness/codex objects of pantheon-config
since T-4790; codex-config objects before), wrap the Telegram token without persisting it, and
perform receiver handoff through fixed operations instead of target LLM
cooperation.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import math
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Literal, Mapping, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import mailbox as pantheon_mailbox  # noqa: E402
from tools.codex_telegram_actions import (  # noqa: E402
    HEALTH_409_ERROR_PREFIX,
    PRIOR_RUNTIME_CHOICES,
    receiver_handoff_actions,
)
from tools.mailbox_writable_roots import (  # noqa: E402
    MailboxWritableRootError,
    resolve_mailbox_writable_roots,
    verify_maildir_writable_root,
)
from tools.harness_permission_posture import (  # noqa: E402
    PermissionPostureError,
    requires_unattended_telegram,
)
from tools.persona_apply_path import controlled_apply_environment  # noqa: E402


#: T-4790 (T-4750 C4b-3): the pin is pantheon-config's own harness/codex tree at
#: the C4b-2 merge 0f4d81d99, 13 paths (adds codex_telegram/persona_orientation.py,
#: which the poller imports; the previous 12-path pin at codex-config 1f84f64,
#: subdir "", could not import its poller). Approved manifest_sha256
#: e21ea6f9; see APPROVED_BRIDGE_MANIFEST_SHA256 in tools/test_codex_telegram_bridge.py.
CODEX_TELEGRAM_SOURCE_REVISION = "0f4d81d99b06837efbc0e836264f0bd85617e3b9"
CODEX_TELEGRAM_SOURCE_SUBDIR = "harness/codex"
CODEX_TELEGRAM_SOURCE_PATHS = (
    "codex_telegram/__init__.py",
    "codex_telegram/active_step.py",
    "codex_telegram/allowlist.py",
    "codex_telegram/codex_hooks.py",
    "codex_telegram/config.py",
    "codex_telegram/keepgoing.py",
    "codex_telegram/persona_orientation.py",
    "codex_telegram/poller.py",
    "codex_telegram/server.py",
    "codex_telegram/standing_instructions.py",
    "codex_telegram/submit_verify.py",
    "run_poller.py",
    "run_server.py",
)
CODEX_TELEGRAM_SOURCE_SHA256 = {
    "codex_telegram/__init__.py": "1471d4fbf3b6e95c5a796d8ec67afaecb4c99a3077836961b2b77c659573eb13",
    "codex_telegram/active_step.py": "e27c2c2dc4e72b9d9a825aa6de6580a684aecfe29f0e26afc8c2a49f66730304",
    "codex_telegram/allowlist.py": "ca802c955f258b781f1d23fbc73183e3c70077c1b231338e99182af1be6a472f",
    "codex_telegram/codex_hooks.py": "0cf372040124ffd816d12105d203f1ae5d80e738c1610608c8c2c0e552464abf",
    "codex_telegram/config.py": "f5f44bb64379129646ec99c38a033a36c243730f72e56efa9fda02f1b64d2bdd",
    "codex_telegram/keepgoing.py": "f84d768e750d8f855ae85dc52c118aeb7f9b98dc370929461eb0e14da5c4d7ce",
    "codex_telegram/persona_orientation.py": "c0552ba19e7bfc30ef1e2ba2d4adfbf2aed2940969a076c6c8993367a3d9a811",
    "codex_telegram/poller.py": "533484213902c12a3af1569572c8dafb84529d5b82017ecb40873da956e93078",
    "codex_telegram/server.py": "02bda3340d5dc4e0d64bd20b2514504188b6139ff3298ed3911ec1bdefdee38b",
    "codex_telegram/standing_instructions.py": "885f566c065db6c733cbce850cfd65322d9800e22a8e09ce74849b17ea83b118",
    "codex_telegram/submit_verify.py": "1f913de5d9c6eef8967f033642abb12664ba617e47a27556e0b445b5775be9d0",
    "run_poller.py": "4802329fad20ee1a30ecfd729803244ac858365b4ba6dce898e8dd64c0fd12ef",
    "run_server.py": "2c21a2d2a4df17e2704bddbc2eefa1ac453216d30be657c961be362f8affea74",
}
#: T-4770 (T-4750 C4b-1): the archive is built from the pantheon-config clone,
#: which holds codex-config's full history (T-4755 import). T-4790 (C4b-3) then
#: moved the pin itself into pantheon-config's harness/codex tree.
CODEX_TELEGRAM_SOURCE_REPO = "pantheon-config"
BRIDGE_SCHEMA = "pantheon-codex-telegram-bridge/v1"
COMMAND_SHEET_SCHEMA = "pantheon-codex-telegram-command-sheet/v2"
SOURCE_CURRENCY_SCHEMA = "pantheon-source-currency/v1"
TELEGRAM_PREFLIGHT_SCHEMA = "pantheon-codex-telegram-preflight/v1"
TOPOLOGY_SNAPSHOT_SCHEMA = "pantheon-receiver-topology-snapshot/v1"
INSTRUCTION_PROOF_SCHEMA = "pantheon-codex-instruction-proof/v1"
TELEGRAM_PREFLIGHT_TIMEOUT_SECONDS = 10.0
TELEGRAM_RESPONSE_LIMIT_BYTES = 65536
TELEGRAM_UNATTENDED_EVIDENCE_LIMIT_BYTES = 65536
TELEGRAM_UNATTENDED_PREPARE_SCHEMA = "pantheon-telegram-unattended-prepare/v1"
TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA = "pantheon-telegram-unattended-observation/v1"
TELEGRAM_UNATTENDED_RECEIPT_SCHEMA = "pantheon-telegram-unattended-tool-evidence/v1"
TELEGRAM_UNATTENDED_SAFE_STAMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TELEGRAM_UNATTENDED_CANARIES: Mapping[str, Mapping[str, str]] = {
    "20-telegram-text-canary": {
        "tool": "reply",
        "canary": "telegram-text-inbound-reply",
    },
    "21-telegram-document-canary": {
        "tool": "download_attachment",
        "canary": "telegram-document-download",
    },
    "22-telegram-photo-canary": {
        "tool": "download_attachment",
        "canary": "telegram-photo-download",
    },
}
TELEGRAM_ERROR_LIMIT = 240
CODEX_INSTRUCTION_DISCOVERY_TIMEOUT = "120s"
CODEX_INSTRUCTION_DISCOVERY_PROMPT = (
    "Read only your currently loaded standing instructions. Do not run commands, mutate files, "
    "contact the network, start Telegram polling, or reveal any secret values. Output at most "
    "8 bullets, each 20 words or fewer, confirming whether the instructions establish: "
    "Orchestrator/HO identity and authority; mandatory canonical /home/claude/CLAUDE.md read-before-action; "
    "Telegram reply mapping mcp__telegram__reply; canonical Orchestrator rules remain source-of-truth. "
    "If any item is absent or ambiguous, say so explicitly."
)
OPERATOR_CHAT_ID = 424242424
HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
TOKEN_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PRIVATE_KEY|BOT_TOKEN)", re.I)
URL_RE = re.compile(r"https?://\S+")
TMUX_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TMUX_PANE_RE = re.compile(r"^[0-9]+[.][0-9]+$")
CLAUDE_RECEIVER_TOPOLOGIES = frozenset({"runtime-owned", "systemd-user"})
CLAUDE_TOPOLOGY_PHASES = frozenset({"managed", "predecessor"})
TopologyPhase = Literal["managed", "predecessor"]
FORBIDDEN_INLINE_KEYS = {
    "value",
    "secret",
    "token",
    "password",
    "private_key",
    "contents",
    "auth_file",
    "copy_from",
    "environmentfile",
}
TELEGRAM_UNATTENDED_TOOL_APPROVALS = {
    "reply": "approve",
    "react": "approve",
    "edit_message": "approve",
    "download_attachment": "approve",
}


class BridgeError(RuntimeError):
    """Fail-closed bridge error."""


@dataclass(frozen=True)
class CodexTelegramSource:
    repo: str
    revision: str
    paths: tuple[str, ...]
    sha256: Mapping[str, str]
    owner_account: str | None = None
    source_root: Path | None = None
    #: Tree prefix of ``paths`` inside ``revision`` ("" = the commit's root, true
    #: for codex-config's own commits such as the historical 1f84f64 pin;
    #: "harness/codex" for the T-4790 pin). Archive member names stay the
    #: unprefixed ``paths`` either way (T-4770).
    subdir: str = ""

    def object_path(self, relative: str) -> str:
        """The ``git show <revision>:<object_path>`` spec for one deploy path."""
        return f"{self.subdir}/{relative}" if self.subdir else relative


def parse_source_subdir(raw: Any) -> str:
    """Validate a bridge ``source.subdir``: "" or a clean relative POSIX path."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise BridgeError("codex telegram source subdir must be a string")
    if raw == "":
        return ""
    parts = raw.split("/")
    if raw.startswith("/") or raw.endswith("/") or any(part in ("", ".", "..") for part in parts):
        raise BridgeError("codex telegram source subdir must be a clean relative path")
    return raw


def default_source_root(owner_account: str | None) -> Path:
    """The source clone the archive is built from when the binding names none."""
    return Path("/home") / (owner_account or "codex") / CODEX_TELEGRAM_SOURCE_REPO


def source_repo_root(source: CodexTelegramSource) -> Path:
    """The ``--source-repo`` for archive builds, derived from the binding."""
    return source.source_root or default_source_root(source.owner_account)


@dataclass(frozen=True)
class CodexTelegramBinding:
    persona: str
    account: str
    account_home: Path
    claude_receiver_topology: Literal["runtime-owned", "systemd-user"]
    predecessor_claude_receiver_topology: Literal["runtime-owned", "systemd-user"]
    claude_receiver_unit: str
    codex_receiver_unit: str
    tmux_session: str
    tmux_pane: str
    credential_env: Path
    state_dir: Path
    release_root: Path
    watchdog_units: tuple[str, ...]
    mcp_name: str
    source: CodexTelegramSource
    unattended_telegram_tools: bool = False
    # T-5038: a role may launch Claude from a configured executable (recovery runs
    # the root-owned /opt copy) instead of the npm path. None = npm path only.
    claude_executable: Path | None = None

    @property
    def claude_receiver_argv0s(self) -> frozenset[str]:
        npm = str(self.account_home / ".npm-global" / "bin" / "claude")
        if self.claude_executable is None:
            return frozenset({npm})
        return frozenset({npm, str(self.claude_executable)})

    @property
    def claude_pane_commands(self) -> frozenset[str]:
        if self.claude_executable is None:
            return frozenset({"claude"})
        return frozenset({"claude", self.claude_executable.name})

    @property
    def current_release(self) -> Path:
        return self.release_root / "current"

    @property
    def effective_pane(self) -> str:
        return _effective_pane(self.tmux_session, self.tmux_pane)


@dataclass(frozen=True)
class SourceHashReceipt:
    schema: str
    revision: str
    file_sha256: Mapping[str, str]
    manifest_sha256: str


@dataclass(frozen=True)
class McpRegistrationReceipt:
    schema: str
    name: str
    created: bool
    expected_sha256: str
    created_file: bool = False
    appended_suffix_sha256: str | None = None


@dataclass(frozen=True)
class HandoffReceipt:
    schema: str
    persona: str
    account: str
    direction: str
    phase: str
    ok: bool
    operations: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class ReceiverStateReceipt:
    schema: str
    persona: str
    account: str
    kind: str
    expected: str
    state: str
    ok: bool
    evidence: Mapping[str, str]
    error: str | None = None


@dataclass(frozen=True)
class UnitProbe:
    unit: str
    active_state: str
    sub_state: str
    main_pid: str


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    ppid: int
    args: str
    cwd: str = ""


@dataclass(frozen=True)
class TmuxPaneSnapshot:
    exists: bool
    dead: bool
    pane_pid: int
    command: str
    server_pid: int = 0


@dataclass(frozen=True)
class TelegramPreflightReceipt:
    schema: str
    persona: str
    account: str
    ok: bool
    webhook_active: bool
    checked_at: str
    methods: tuple[str, ...]
    bot_id: int | None = None
    bot_username: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SourceCurrencyReceipt:
    schema: str
    ok: bool
    repo: str
    expected_head: str
    actual_head: str
    clean: bool
    status_line_count: int
    status_sha256: str
    checked_at: str
    error: str | None = None


@dataclass(frozen=True)
class CommandSheetStep:
    step_id: str
    type: Literal["command", "operator-gate"]
    phase: str
    command: str | None = None
    gate: Mapping[str, Any] | None = None


def _archive_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def handoff_actions(
    binding: CodexTelegramBinding,
    direction: str,
    phase: str,
    *,
    prior_runtime: str = "live",
) -> tuple[str, ...]:
    return receiver_handoff_actions(
        claude_receiver_topology=binding.claude_receiver_topology,
        watchdog_units=binding.watchdog_units,
        direction=direction,
        phase=phase,
        prior_runtime=prior_runtime,  # type: ignore[arg-type]
    )


class FakeTelegramClient:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]):
        self.responses = dict(responses)
        self.calls: list[str] = []

    def call(self, method: str) -> Mapping[str, Any]:
        self.calls.append(method)
        return self.responses.get(method, {"ok": True, "result": {}})


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_expected_revision(value: str) -> str:
    if not HEX_SHA_RE.fullmatch(value):
        raise BridgeError("expected pantheon revision must be a lowercase 40-hex SHA")
    return value


def _bounded_error(value: str, *, limit: int = TELEGRAM_ERROR_LIMIT) -> str:
    cleaned = " ".join(value.replace("\x00", "").split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _sanitize_telegram_error(exc: BaseException, *, token: str = "") -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return _bounded_error(f"HTTPError: http status {exc.code}")
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason)
        if token:
            reason = reason.replace(token, "[REDACTED]")
        reason = URL_RE.sub("[REDACTED_URL]", reason)
        reason = _bounded_error(reason)
        return _bounded_error(f"URLError: {reason}")
    text = f"{type(exc).__name__}: {exc}"
    if token:
        text = text.replace(token, "[REDACTED]")
    text = URL_RE.sub("[REDACTED_URL]", text)
    return _bounded_error(text)


def _telegram_api_url(token: str, method: Literal["getWebhookInfo", "getMe"]) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _assert_telegram_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.telegram.org":
        raise BridgeError("telegram api URL is not the approved HTTPS host")


def _read_limited_response(response: Any, *, limit: int = TELEGRAM_RESPONSE_LIMIT_BYTES) -> bytes:
    content = response.read(limit + 1)
    if len(content) > limit:
        raise BridgeError("telegram response exceeds bounded preflight limit")
    return content


def _require_telegram_envelope(payload: Any, *, method: str, token: str = "") -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise BridgeError(f"telegram {method} response is not a JSON object")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise BridgeError(f"telegram {method} response has invalid ok field")
    if not ok:
        description = payload.get("description")
        if not isinstance(description, str):
            description = "telegram returned ok:false"
        if token:
            description = description.replace(token, "[REDACTED]")
        description = URL_RE.sub("[REDACTED_URL]", description)
        raise BridgeError(f"telegram {method} returned ok:false: {_bounded_error(description)}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise BridgeError(f"telegram {method} response has invalid result field")
    return payload


def _bot_identity(result: Mapping[str, Any]) -> tuple[int, str | None]:
    bot_id = result.get("id")
    if type(bot_id) is not int:
        raise BridgeError("telegram getMe result has invalid bot id")
    username = result.get("username")
    if username is None:
        return bot_id, None
    if not isinstance(username, str):
        raise BridgeError("telegram getMe result has invalid username")
    if not TELEGRAM_USERNAME_RE.fullmatch(username) or not username.lower().endswith("bot"):
        return bot_id, None
    return bot_id, username


def _receipt_mapping(receipt: Any) -> Mapping[str, Any]:
    payload = dataclasses.asdict(receipt)
    if isinstance(payload.get("methods"), tuple):
        payload["methods"] = list(payload["methods"])
    return payload


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "telegram redirect refused", headers, fp)


class TelegramApiClient:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = TELEGRAM_PREFLIGHT_TIMEOUT_SECONDS,
        opener: Any | None = None,
    ) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.opener = opener if opener is not None else urllib.request.build_opener(NoRedirectHandler)

    def call(self, method: Literal["getWebhookInfo", "getMe"]) -> Mapping[str, Any]:
        if method not in {"getWebhookInfo", "getMe"}:
            raise BridgeError("unsupported telegram preflight method")
        url = _telegram_api_url(self.token, method)
        _assert_telegram_url(url)
        request = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _assert_telegram_url(final_url)
                payload = json.loads(_read_limited_response(response).decode("utf-8"))
        except Exception as exc:
            raise BridgeError(_sanitize_telegram_error(exc, token=self.token)) from exc
        return _require_telegram_envelope(payload, method=method, token=self.token)


def _forbid_inline_credentials(value: Any, path: str = "codex_telegram_bridge") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_INLINE_KEYS:
                raise BridgeError(f"{path}.{key}: inline credential-like key is forbidden")
            _forbid_inline_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbid_inline_credentials(item, f"{path}[{index}]")


def validate_bridge_block(raw: Mapping[str, Any]) -> None:
    parse_bridge_block(raw)


def _parse_claude_receiver_topology(value: Any) -> Literal["runtime-owned", "systemd-user"]:
    if value not in CLAUDE_RECEIVER_TOPOLOGIES:
        raise BridgeError("claude_receiver_topology must be explicit runtime-owned or systemd-user")
    return value  # type: ignore[return-value]


def _parse_predecessor_claude_receiver_topology(
    value: Any,
    *,
    managed: Literal["runtime-owned", "systemd-user"],
) -> Literal["runtime-owned", "systemd-user"]:
    if value is None:
        return managed
    if value not in CLAUDE_RECEIVER_TOPOLOGIES:
        raise BridgeError("predecessor_claude_receiver_topology must be runtime-owned or systemd-user")
    return value  # type: ignore[return-value]


def _parse_tmux_session(value: Any) -> str:
    if not isinstance(value, str) or not TMUX_SESSION_RE.fullmatch(value):
        raise BridgeError("tmux_session must be an explicit safe tmux session name")
    return value


def _parse_tmux_pane(value: Any) -> str:
    if not isinstance(value, str) or not TMUX_PANE_RE.fullmatch(value):
        raise BridgeError("tmux_pane must be an explicit window.pane target")
    return value


def _effective_pane(tmux_session: str, tmux_pane: str) -> str:
    return f"{_parse_tmux_session(tmux_session)}:{_parse_tmux_pane(tmux_pane)}"


def parse_bridge_block(raw: Mapping[str, Any]) -> tuple[CodexTelegramSource, Mapping[str, Mapping[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise BridgeError("persona_installer.codex_telegram_bridge must be a mapping")
    _forbid_inline_credentials(raw)
    if raw.get("schema") != BRIDGE_SCHEMA:
        raise BridgeError("unsupported codex_telegram_bridge.schema")
    if raw.get("operator_chat_id") != OPERATOR_CHAT_ID:
        raise BridgeError("codex_telegram_bridge.operator_chat_id must be the operator integer id")
    source_raw = raw.get("source")
    personas = raw.get("personas")
    if not isinstance(source_raw, Mapping) or not isinstance(personas, Mapping):
        raise BridgeError("codex_telegram_bridge source/personas missing")
    paths = tuple(source_raw.get("paths", ()))
    hashes = source_raw.get("sha256")
    if source_raw.get("repo") != CODEX_TELEGRAM_SOURCE_REPO:
        raise BridgeError(f"codex telegram source repo must be {CODEX_TELEGRAM_SOURCE_REPO}")
    if source_raw.get("revision") != CODEX_TELEGRAM_SOURCE_REVISION:
        raise BridgeError("codex telegram source revision does not match T-2950 pin")
    if paths != CODEX_TELEGRAM_SOURCE_PATHS:
        raise BridgeError("codex telegram source paths do not match approved deploy set")
    if hashes != CODEX_TELEGRAM_SOURCE_SHA256:
        raise BridgeError("codex telegram source hashes do not match approved pin")
    subdir = parse_source_subdir(source_raw.get("subdir"))
    if subdir != CODEX_TELEGRAM_SOURCE_SUBDIR:
        raise BridgeError("codex telegram source subdir does not match approved pin")
    owner_account = source_raw.get("owner_account")
    if owner_account is not None and not (isinstance(owner_account, str) and owner_account):
        raise BridgeError("codex telegram source owner_account must be a non-empty string")
    source_root = source_raw.get("source_root")
    if source_root is not None:
        if not isinstance(source_root, str) or not Path(source_root).is_absolute():
            raise BridgeError("codex telegram source_root must be an absolute path")
    source = CodexTelegramSource(
        CODEX_TELEGRAM_SOURCE_REPO,
        CODEX_TELEGRAM_SOURCE_REVISION,
        paths,
        dict(hashes),
        owner_account=owner_account,
        source_root=Path(source_root) if source_root is not None else None,
        subdir=subdir,
    )
    if not personas:
        raise BridgeError("codex telegram personas must not be empty")
    for persona, raw_persona in personas.items():
        if not isinstance(persona, str) or not persona:
            raise BridgeError("codex telegram persona names must be non-empty strings")
        if not isinstance(raw_persona, Mapping):
            raise BridgeError(f"codex telegram persona {persona} must be a mapping")
        topology = _parse_claude_receiver_topology(raw_persona.get("claude_receiver_topology"))
        _parse_predecessor_claude_receiver_topology(
            raw_persona.get("predecessor_claude_receiver_topology"),
            managed=topology,
        )
        _parse_tmux_session(raw_persona.get("tmux_session"))
        _parse_tmux_pane(raw_persona.get("tmux_pane"))
    return source, personas  # type: ignore[return-value]


def binding_from_manifest(manifest: Any, persona: str, *, root: Path = Path("/")) -> CodexTelegramBinding:
    source, personas = parse_bridge_block(manifest.installer.get("codex_telegram_bridge"))
    if persona not in personas:
        raise BridgeError(f"codex telegram bridge has no persona binding for {persona!r}")
    role_binding = manifest.binding(persona)
    raw = personas[persona]
    if raw.get("account") != role_binding.account:
        raise BridgeError("codex telegram account guard mismatch")
    if not isinstance(raw.get("claude_receiver_unit"), str) or not raw.get("claude_receiver_unit"):
        raise BridgeError("claude_receiver_unit must be a non-empty string")
    if raw.get("codex_receiver_unit") != "codex-telegram-poller.service":
        raise BridgeError("unexpected Codex Telegram receiver unit")
    topology = _parse_claude_receiver_topology(raw.get("claude_receiver_topology"))
    predecessor_topology = _parse_predecessor_claude_receiver_topology(
        raw.get("predecessor_claude_receiver_topology"),
        managed=topology,
    )
    watchdog = tuple(raw.get("watchdog_units", ()))
    if not all(isinstance(unit, str) and unit for unit in watchdog):
        raise BridgeError("watchdog_units must be non-empty strings")
    home = (root / "home" / role_binding.account) if root != Path("/") else (Path("/home") / role_binding.account)
    unattended_telegram_tools = _manifest_role_requires_unattended_telegram(
        manifest,
        role=str(role_binding.role),
    )
    return CodexTelegramBinding(
        persona=persona,
        account=role_binding.account,
        account_home=home,
        claude_receiver_topology=topology,
        predecessor_claude_receiver_topology=predecessor_topology,
        claude_receiver_unit=str(raw["claude_receiver_unit"]),
        codex_receiver_unit=str(raw["codex_receiver_unit"]),
        tmux_session=_parse_tmux_session(raw.get("tmux_session")),
        tmux_pane=_parse_tmux_pane(raw.get("tmux_pane")),
        credential_env=home / str(raw["credential_env"]),
        state_dir=home / str(raw["state_dir"]),
        release_root=home / str(raw["release_root"]),
        watchdog_units=watchdog,
        mcp_name=str(raw.get("mcp_name", "telegram")),
        source=source,
        unattended_telegram_tools=unattended_telegram_tools,
        claude_executable=_manifest_role_claude_executable(manifest, role=str(role_binding.role)),
    )


def _manifest_role_claude_executable(manifest: Any, *, role: str) -> Path | None:
    runtime = getattr(manifest, "harness_runtime", {})
    if not isinstance(runtime, Mapping):
        return None
    roles = runtime.get("roles")
    if not isinstance(roles, Mapping):
        return None
    role_raw = roles.get(role)
    if not isinstance(role_raw, Mapping):
        return None
    claude_raw = role_raw.get("claude")
    if not isinstance(claude_raw, Mapping):
        return None
    executable = claude_raw.get("executable")
    if executable is None:
        return None
    if not isinstance(executable, str) or not executable.startswith("/") or "{" in executable:
        raise BridgeError("claude executable must be an absolute literal path")
    return Path(executable)


def _manifest_role_requires_unattended_telegram(manifest: Any, *, role: str) -> bool:
    runtime = getattr(manifest, "harness_runtime", {})
    if not isinstance(runtime, Mapping):
        return False
    roles = runtime.get("roles")
    if not isinstance(roles, Mapping):
        return False
    role_raw = roles.get(role)
    if not isinstance(role_raw, Mapping):
        return False
    codex_raw = role_raw.get("codex")
    if not isinstance(codex_raw, Mapping):
        return False
    try:
        return requires_unattended_telegram(codex_raw)
    except PermissionPostureError as exc:
        raise BridgeError(str(exc)) from exc


def load_telegram_token(env_path: Path, *, expected_uid: int, expected_mode: int = 0o600) -> str:
    try:
        st = env_path.stat()
    except OSError as exc:
        raise BridgeError("telegram env source missing") from exc
    if st.st_uid != expected_uid:
        raise BridgeError("telegram env source owner mismatch")
    if stat.S_IMODE(st.st_mode) != expected_mode:
        raise BridgeError("telegram env source mode mismatch")
    assignments: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise BridgeError("telegram env source contains unsupported syntax")
        key, value = line.split("=", 1)
        if key.strip() != "TELEGRAM_BOT_TOKEN":
            raise BridgeError("telegram env source contains unsupported key")
        value = value.strip()
        if not value or any(ch.isspace() for ch in value) or value[0] in {'"', "'"}:
            raise BridgeError("telegram token is malformed")
        assignments.append(value)
    if len(assignments) != 1:
        raise BridgeError("telegram env source must contain exactly one token")
    token = assignments[0]
    if not TOKEN_RE.fullmatch(token):
        raise BridgeError("telegram token is malformed")
    return token


def child_environment(token: str, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    base = dict(os.environ if inherited is None else inherited)
    scrubbed = {key: value for key, value in base.items() if not TOKEN_KEY_RE.search(key)}
    scrubbed["TELEGRAM_BOT_TOKEN"] = token
    return scrubbed


def approved_entrypoint(binding: CodexTelegramBinding, mode: Literal["poller", "server"]) -> Path:
    name = "run_poller.py" if mode == "poller" else "run_server.py"
    path = binding.current_release / name
    resolved = path.resolve()
    release = binding.current_release.resolve()
    if not resolved.is_relative_to(release) or not path.is_file():
        raise BridgeError("approved Codex Telegram entrypoint is unavailable")
    return path


def exec_mode(binding: CodexTelegramBinding, mode: Literal["poller", "server"]) -> None:
    token = load_telegram_token(binding.credential_env, expected_uid=os.geteuid())
    env = child_environment(token)
    os.execvpe(sys.executable, (sys.executable, str(approved_entrypoint(binding, mode))), env)


def credential_check_receipt(binding: CodexTelegramBinding, *, expected_uid: int | None = None) -> Mapping[str, Any]:
    expected = expected_uid
    if expected is None:
        try:
            expected = pwd.getpwnam(binding.account).pw_uid
        except KeyError as exc:
            raise BridgeError("target account is unavailable") from exc
    token = load_telegram_token(binding.credential_env, expected_uid=expected)
    return {
        "schema": "pantheon-codex-telegram-credential-check/v1",
        "persona": binding.persona,
        "account": binding.account,
        "credential_env": str(binding.credential_env),
        "owner_uid": str(expected),
        "mode": "0600",
        "key": "TELEGRAM_BOT_TOKEN",
        "token_present": bool(token),
        "token_value": "omitted",
    }


def compute_source_hashes(source_root: Path, source: CodexTelegramSource) -> SourceHashReceipt:
    hashes: dict[str, str] = {}
    for relative in source.paths:
        path = source_root / source.object_path(relative)
        if not path.is_file():
            raise BridgeError(f"source path missing: {relative}")
        hashes[relative] = _sha256_bytes(path.read_bytes())
    return SourceHashReceipt(
        schema="pantheon-codex-telegram-source-receipt/v1",
        revision=source.revision,
        file_sha256=hashes,
        manifest_sha256=_sha256_bytes(_canonical_json(hashes).encode("utf-8")),
    )


def _git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BridgeError((result.stderr or result.stdout).strip() or "git command failed")
    return result.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"git command failed").decode("utf-8", "replace")
        raise BridgeError(detail.strip())
    return result.stdout


def verify_git_object_at_pin(repo: Path, source: CodexTelegramSource) -> None:
    commit = _git_text(repo, "rev-parse", f"{source.revision}^{{commit}}").strip()
    if commit != source.revision:
        raise BridgeError("codex telegram source pinned object does not resolve exactly")


def create_verified_source_archive(
    source_repo: Path,
    output: Path,
    source: CodexTelegramSource,
) -> SourceHashReceipt:
    verify_git_object_at_pin(source_repo, source)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w") as archive:
            for relative in source.paths:
                content = _git_bytes(source_repo, "show", f"{source.revision}:{source.object_path(relative)}")
                digest = _sha256_bytes(content)
                if digest != source.sha256[relative]:
                    raise BridgeError(f"pinned object hash mismatch: {relative}")
                info = tarfile.TarInfo(relative)
                info.size = len(content)
                info.mode = 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
        receipt = verify_archive(Path(temporary), source)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return receipt


def source_check_receipt(
    source: CodexTelegramSource,
    *,
    source_repo: Path | None = None,
    archive_path: Path | None = None,
    stage_binding: CodexTelegramBinding | None = None,
) -> Mapping[str, Any]:
    if source_repo is None and archive_path is None:
        raise BridgeError("source-check requires --source-repo or --archive")
    generated = False
    receipt: SourceHashReceipt
    if source_repo is not None and archive_path is not None:
        receipt = create_verified_source_archive(source_repo, archive_path, source)
        generated = True
    elif archive_path is not None:
        receipt = verify_archive(archive_path, source)
    else:
        verify_git_object_at_pin(source_repo, source)
        receipt = SourceHashReceipt(
            schema="pantheon-codex-telegram-source-receipt/v1",
            revision=source.revision,
            file_sha256={
                relative: _sha256_bytes(_git_bytes(source_repo, "show", f"{source.revision}:{source.object_path(relative)}"))
                for relative in source.paths
            },
            manifest_sha256="",
        )
        if dict(receipt.file_sha256) != dict(source.sha256):
            raise BridgeError("pinned object hashes do not match manifest")
        receipt = dataclasses.replace(
            receipt,
            manifest_sha256=_sha256_bytes(_canonical_json(receipt.file_sha256).encode("utf-8")),
        )
    staged: Mapping[str, Any] | None = None
    if stage_binding is not None:
        if archive_path is None:
            raise BridgeError("staging requires --archive")
        staged = bridge_stage(archive_path, stage_binding)
    result: dict[str, Any] = {
        "schema": "pantheon-codex-telegram-source-check/v1",
        "revision": receipt.revision,
        "paths": list(source.paths),
        "file_sha256": dict(receipt.file_sha256),
        "manifest_sha256": receipt.manifest_sha256,
        "archive_generated": generated,
    }
    if archive_path is not None:
        result["archive_path"] = str(archive_path)
        result["archive_sha256"] = _archive_sha256(archive_path)
    if staged is not None:
        result["stage"] = staged
    return result


def _open_archive(archive: Path | bytes) -> tarfile.TarFile:
    if isinstance(archive, (bytes, bytearray)):
        return tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
    return tarfile.open(archive, "r:*")


def _check_member_path(name: str) -> None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise BridgeError("source archive contains escaping path")


def verify_archive(archive: Path | bytes, source: CodexTelegramSource) -> SourceHashReceipt:
    """Verify an archive (a path, or bytes already read) against the pinned source.
    stage_archive_release passes BYTES so that what is verified is exactly what is
    extracted (T-4860)."""
    with _open_archive(archive) as tar:
        members = [item for item in tar.getmembers() if item.isfile()]
        names = [item.name for item in members]
        for name in names:
            _check_member_path(name)
        if set(names) != set(source.paths):
            raise BridgeError("source archive path set does not match manifest")
        hashes = {}
        for member in members:
            handle = tar.extractfile(member)
            if handle is None:
                raise BridgeError("source archive member unreadable")
            hashes[member.name] = _sha256_bytes(handle.read())
    if hashes != source.sha256:
        raise BridgeError("source archive hashes do not match manifest")
    return SourceHashReceipt(
        schema="pantheon-codex-telegram-source-receipt/v1",
        revision=source.revision,
        file_sha256=hashes,
        manifest_sha256=_sha256_bytes(_canonical_json(hashes).encode("utf-8")),
    )


def _release_tree_matches(release_dir: Path, source: CodexTelegramSource) -> bool:
    return all(
        (release_dir / relative).is_file()
        and _sha256_bytes((release_dir / relative).read_bytes()) == source.sha256[relative]
        for relative in source.paths
    )


def _remove_owned_release_dir(release_dir: Path, source: CodexTelegramSource) -> None:
    if release_dir.is_dir() and _release_tree_matches(release_dir, source):
        shutil.rmtree(release_dir)


def _fault(name: str, fault_after: str | None) -> None:
    if fault_after == name:
        raise BridgeError(f"fault injected after {name}")


def stage_archive_release(archive_path: Path, binding: CodexTelegramBinding, *, fault_after: str | None = None) -> Mapping[str, Any]:
    # T-4860: read the archive ONCE, and verify and extract that same buffer. Re-opening
    # the path to extract left a window in which another local account could swap a
    # predictable /tmp archive after verification (unpinned code, pinned-hash evidence).
    data = archive_path.read_bytes()
    receipt = verify_archive(data, binding.source)
    release_dir = binding.release_root / "releases" / binding.source.revision
    prior = None
    if binding.current_release.is_symlink():
        prior = os.readlink(binding.current_release)
    (binding.release_root / "releases").mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(binding.release_root)))
    promoted = False
    allowlist = binding.state_dir / "allowlist.json"
    allow_before = _file_snapshot(allowlist)
    current_before = _file_snapshot(binding.current_release)
    try:
        with _open_archive(data) as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                _check_member_path(member.name)  # re-checked at the write, not only at verify
                target = tmp / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise BridgeError("source archive member unreadable")
                target.write_bytes(handle.read())
                target.chmod(0o644)
        if release_dir.exists():
            if not _release_tree_matches(release_dir, binding.source):
                raise BridgeError("existing release directory does not match pinned hashes")
        else:
            os.replace(tmp, release_dir)
            promoted = True
            _fault("release-promote", fault_after)
        binding.state_dir.mkdir(parents=True, exist_ok=True)
        binding.state_dir.chmod(0o700)
        allowlist.write_text(render_allowlist(), encoding="utf-8")
        allowlist.chmod(0o600)
        _fault("release-allowlist", fault_after)
        link_tmp = binding.release_root / f".current-{os.getpid()}"
        if link_tmp.exists() or link_tmp.is_symlink():
            link_tmp.unlink()
        link_tmp.symlink_to(release_dir)
        os.replace(link_tmp, binding.current_release)
        _fault("release-current", fault_after)
    except Exception:
        _restore_file_snapshot(binding.current_release, current_before, None)
        _restore_file_snapshot(allowlist, allow_before, None)
        if promoted:
            _remove_owned_release_dir(release_dir, binding.source)
        raise
    finally:
        if not promoted and tmp.exists():
            shutil.rmtree(tmp)
    return {
        "schema": "pantheon-codex-telegram-release/v1",
        "revision": binding.source.revision,
        "source_manifest_sha256": receipt.manifest_sha256,
        "prior_current": prior,
        "current": str(release_dir),
        "allowlist_sha256": _sha256_bytes(render_allowlist().encode("utf-8")),
    }


def _unit_inactive_no_pid(probe: UnitProbe) -> bool:
    return probe.active_state in {"inactive", "failed"} and probe.main_pid in {"", "0"}


def _parse_process_args(args: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(args))
    except ValueError:
        return ()


def _process_snapshot_from_line(line: str) -> ProcessSnapshot | None:
    parts = line.strip().split(None, 2)
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    if len(parts) >= 3 and parts[1].isdigit():
        return ProcessSnapshot(int(parts[0]), int(parts[1]), parts[2])
    return ProcessSnapshot(int(parts[0]), 0, " ".join(parts[1:]))


def _is_exact_claude_receiver(binding: CodexTelegramBinding, process: ProcessSnapshot) -> bool:
    argv = _parse_process_args(process.args)
    return (
        bool(argv)
        and argv[0] in binding.claude_receiver_argv0s
        and any(
            item == "--channels" and index + 1 < len(argv) and argv[index + 1] == "plugin:telegram@claude-plugins-official"
            for index, item in enumerate(argv)
        )
    )


def _is_shell_box_wrapper(process: ProcessSnapshot) -> bool:
    """T-5481: the CLAUDE_CODE_SHELL_PREFIX wrapper (`bash .../pantheon-shell-box.sh <cmd...>`)
    carries the plugin's bun command line in its own args, so a substring match would count it
    as a second launcher. Its boxed child is the real launcher."""
    return any(token.endswith("/pantheon-shell-box.sh") for token in _parse_process_args(process.args)[:2])


def _is_plugin_launcher(binding: CodexTelegramBinding, process: ProcessSnapshot) -> bool:
    args = process.args
    if _is_shell_box_wrapper(process):
        return False
    return (
        str(binding.account_home / ".claude") in args
        and "claude-plugins-official" in args
        and "telegram" in args
        and "bun" in args
    )


def _plugin_root_fragment(binding: CodexTelegramBinding) -> str:
    return str(binding.account_home / ".claude" / "plugins")


def _is_plugin_server(binding: CodexTelegramBinding, process: ProcessSnapshot, *, plugin_roots: frozenset[int] = frozenset(), processes_by_pid: Mapping[int, ProcessSnapshot] | None = None) -> bool:
    if "server.ts" not in process.args:
        return False
    if processes_by_pid is not None and _has_plugin_ancestor(process, plugin_roots, processes_by_pid):
        return True
    rooted = process.cwd.startswith(_plugin_root_fragment(binding))
    return rooted and "claude-plugins-official" in process.cwd and "telegram" in process.cwd


def _descendant_pids(root_pid: int, processes: Sequence[ProcessSnapshot]) -> frozenset[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process.ppid, []).append(process.pid)
    seen: set[int] = set()
    stack = list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return frozenset(seen)


def _has_plugin_ancestor(process: ProcessSnapshot, plugin_roots: frozenset[int], processes_by_pid: Mapping[int, ProcessSnapshot]) -> bool:
    parent = process.ppid
    seen: set[int] = set()
    while parent and parent not in seen:
        if parent in plugin_roots:
            return True
        seen.add(parent)
        parent = processes_by_pid.get(parent, ProcessSnapshot(0, 0, "")).ppid
    return False


def _plugin_descendants(binding: CodexTelegramBinding, processes: Sequence[ProcessSnapshot], *, pane_pid: int) -> tuple[tuple[ProcessSnapshot, ...], tuple[ProcessSnapshot, ...]]:
    descendants = _descendant_pids(pane_pid, processes) if pane_pid else frozenset()
    by_pid = {process.pid: process for process in processes}
    plugin_roots = frozenset(process.pid for process in processes if _is_plugin_launcher(binding, process))
    launchers = []
    servers = []
    for process in processes:
        if process.pid not in descendants:
            continue
        if _is_plugin_launcher(binding, process):
            launchers.append(process)
        elif _is_plugin_server(binding, process, plugin_roots=plugin_roots, processes_by_pid=by_pid):
            servers.append(process)
    return tuple(launchers), tuple(servers)


def _is_direct_spawn_plugin_launcher(process: ProcessSnapshot) -> bool:
    # T-4461: `<bun> run --cwd <plugin> --silent server.ts` (recovery's T-2624 .mcp.json) IS the
    # server; upstream's `... --silent start` wrapper forks a separate `bun server.ts` child.
    return "server.ts" in _parse_process_args(process.args)


def _plugin_group_shape(
    launchers: Sequence[ProcessSnapshot],
    servers: Sequence[ProcessSnapshot],
) -> tuple[str, str]:
    """Return ("direct-spawn"|"wrapper", "") for a complete group, else ("", "duplicate"|"not present")."""
    if len(launchers) > 1 or len(servers) > 1:
        return "", "duplicate"
    if len(launchers) != 1:
        return "", "not present"
    if _is_direct_spawn_plugin_launcher(launchers[0]):
        return ("", "duplicate") if servers else ("direct-spawn", "")
    return ("wrapper", "") if servers else ("", "not present")


def _plugin_remnants(binding: CodexTelegramBinding, processes: Sequence[ProcessSnapshot]) -> tuple[ProcessSnapshot, ...]:
    by_pid = {process.pid: process for process in processes}
    plugin_roots = frozenset(process.pid for process in processes if _is_plugin_launcher(binding, process))
    return tuple(
        process
        for process in processes
        if _is_plugin_launcher(binding, process)
        or _is_plugin_server(binding, process, plugin_roots=plugin_roots, processes_by_pid=by_pid)
    )


def _any_claude_tmux_pane(binding: CodexTelegramBinding, tmux: TmuxPaneSnapshot) -> bool:
    return tmux.exists and not tmux.dead and tmux.command in binding.claude_pane_commands and tmux.pane_pid > 0


def _cgroup_path_components(path: str) -> frozenset[str]:
    if not path.startswith("/"):
        raise BridgeError("cgroup path is not absolute")
    return frozenset(component for component in path.split("/") if component)


def _cgroup_contains_unit_component(raw: str, unit: str) -> bool:
    if not raw.strip():
        raise BridgeError("cgroup evidence is empty")
    matched = False
    for line in raw.splitlines():
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 3:
            raise BridgeError("cgroup evidence is malformed")
        path = fields[2]
        if not path.startswith("/"):
            raise BridgeError("cgroup path is not absolute")
        if unit in _cgroup_path_components(path):
            matched = True
    return matched


def _runtime_owned_base_evidence(
    binding: CodexTelegramBinding,
    *,
    unit_probe: UnitProbe,
    unit_enabled: str,
    tmux: TmuxPaneSnapshot,
    processes: Sequence[ProcessSnapshot],
) -> dict[str, str]:
    del binding
    return {
        "topology": "runtime-owned",
        "dormant_unit_active_state": unit_probe.active_state,
        "dormant_unit_enabled": unit_enabled,
        "dormant_unit_main_pid": unit_probe.main_pid or "0",
        "tmux_session": "present" if tmux.exists else "absent",
        "tmux_pane_dead": str(tmux.dead).lower(),
        "tmux_pane_pid": str(tmux.pane_pid),
        "tmux_command": tmux.command,
        "process_count": str(len(processes)),
    }


def _evaluate_runtime_owned_claude_state(
    binding: CodexTelegramBinding,
    *,
    expected: Literal["present", "absent"],
    unit_probe: UnitProbe,
    unit_enabled: str,
    tmux: TmuxPaneSnapshot,
    processes: Sequence[ProcessSnapshot],
) -> Mapping[str, str]:
    if not _unit_inactive_no_pid(unit_probe) or unit_enabled == "enabled":
        raise BridgeError("runtime-owned Claude receiver has active/enabled dormant systemd supervisor")
    claude_processes = tuple(process for process in processes if _is_exact_claude_receiver(binding, process))
    plugin_roots = tuple(process for process in processes if _is_plugin_launcher(binding, process))
    plugin_remnants = _plugin_remnants(binding, processes)
    pane_is_claude = _any_claude_tmux_pane(binding, tmux)
    plugin_launchers, plugin_servers = _plugin_descendants(binding, processes, pane_pid=tmux.pane_pid if pane_is_claude else 0)
    anchored_group = set(plugin_launchers) | set(plugin_servers)
    orphan_plugins = tuple(process for process in plugin_remnants if process not in anchored_group)
    evidence = _runtime_owned_base_evidence(
        binding,
        unit_probe=unit_probe,
        unit_enabled=unit_enabled,
        tmux=tmux,
        processes=processes,
    )
    evidence.update(
        {
            "state": expected,
            "claude_receiver_process_count": str(len(claude_processes)),
            "plugin_launcher_descendant_count": str(len(plugin_launchers)),
            "plugin_server_descendant_count": str(len(plugin_servers)),
            "plugin_orphan_count": str(len(orphan_plugins)),
            "plugin_remnant_count": str(len(plugin_remnants)),
        }
    )
    if expected == "present":
        if not pane_is_claude:
            raise BridgeError("runtime-owned Claude tmux pane is not present")
        if not claude_processes:
            if plugin_launchers or plugin_servers:
                raise BridgeError("runtime-owned Claude receiver process is not uniquely anchored to tmux")
            raise BridgeError("runtime-owned Claude receiver process is not present")
        if len(claude_processes) != 1 or claude_processes[0].pid != tmux.pane_pid:
            raise BridgeError("runtime-owned Claude receiver process is not uniquely anchored to tmux")
        shape, shape_error = _plugin_group_shape(plugin_launchers, plugin_servers)
        if shape_error:
            raise BridgeError(f"runtime-owned Claude Telegram plugin group is {shape_error}")
        if orphan_plugins:
            raise BridgeError("runtime-owned Claude Telegram plugin has orphan supervisor")
        evidence["plugin_group_shape"] = shape
        return evidence
    if claude_processes or plugin_roots or plugin_remnants or pane_is_claude:
        raise BridgeError("runtime-owned Claude receiver still present")
    return evidence


def _evaluate_systemd_user_claude_state(
    binding: CodexTelegramBinding,
    *,
    expected: Literal["present", "absent"],
    unit_probe: UnitProbe,
    tmux: TmuxPaneSnapshot,
    processes: Sequence[ProcessSnapshot],
    unit_main_cgroup: str = "",
    tmux_server_cgroup: str = "",
) -> Mapping[str, str]:
    active = unit_probe.active_state == "active" and unit_probe.main_pid not in {"", "0"}
    claude_processes = tuple(process for process in processes if _is_exact_claude_receiver(binding, process))
    plugin_remnants = _plugin_remnants(binding, processes)
    unit_pid = int(unit_probe.main_pid) if unit_probe.main_pid.isdigit() else 0
    server_descendants = _descendant_pids(tmux.server_pid, processes) if tmux.server_pid else frozenset()
    anchored = tuple(process for process in claude_processes if process.pid == tmux.pane_pid and process.pid in server_descendants)
    plugin_launchers, plugin_servers = _plugin_descendants(binding, processes, pane_pid=tmux.pane_pid if _any_claude_tmux_pane(binding, tmux) else 0)
    anchored_group = set(plugin_launchers) | set(plugin_servers)
    orphan_plugins = tuple(process for process in plugin_remnants if process not in anchored_group)
    if _any_claude_tmux_pane(binding, tmux):
        if len(claude_processes) != 1 or tmux.pane_pid != claude_processes[0].pid:
            raise BridgeError("systemd Claude receiver has ambiguous tmux second supervisor")
        if tmux.pane_pid not in server_descendants:
            raise BridgeError("systemd Claude receiver pane is not anchored to tmux server")
    evidence = {
        "topology": "systemd-user",
        "state": expected,
        "unit_active_state": unit_probe.active_state,
        "unit_main_pid": unit_probe.main_pid or "0",
        "unit_main_cgroup_match": "false",
        "tmux_server_pid": str(tmux.server_pid),
        "tmux_server_cgroup_match": "false",
        "claude_receiver_process_count": str(len(claude_processes)),
        "plugin_launcher_descendant_count": str(len(plugin_launchers)),
        "plugin_server_descendant_count": str(len(plugin_servers)),
        "plugin_orphan_count": str(len(orphan_plugins)),
        "plugin_remnant_count": str(len(plugin_remnants)),
        "anchored_claude_receiver_process_count": str(len(anchored)),
        "tmux_pane_pid": str(tmux.pane_pid),
        "tmux_command": tmux.command,
    }
    if expected == "present":
        if not active:
            raise BridgeError("claude receiver unit is not active")
        if unit_pid <= 0:
            raise BridgeError("claude receiver unit main PID is invalid")
        if not _cgroup_contains_unit_component(unit_main_cgroup, binding.claude_receiver_unit):
            raise BridgeError("claude receiver unit main PID is outside service cgroup")
        evidence["unit_main_cgroup_match"] = "true"
        if tmux.server_pid <= 0:
            raise BridgeError("systemd Claude tmux server PID is invalid")
        if not _cgroup_contains_unit_component(tmux_server_cgroup, binding.claude_receiver_unit):
            raise BridgeError("systemd Claude tmux server is outside service cgroup")
        evidence["tmux_server_cgroup_match"] = "true"
        if not _any_claude_tmux_pane(binding, tmux):
            raise BridgeError("systemd Claude tmux pane is not present")
        if len(claude_processes) != 1:
            raise BridgeError("claude receiver process is not unique")
        if len(anchored) != 1:
            raise BridgeError("claude receiver process is not anchored to tmux server")
        shape, shape_error = _plugin_group_shape(plugin_launchers, plugin_servers)
        if shape_error:
            raise BridgeError(f"systemd Claude Telegram plugin group is {shape_error}")
        if orphan_plugins:
            raise BridgeError("systemd Claude Telegram plugin has orphan supervisor")
        evidence["plugin_group_shape"] = shape
        return evidence
    if active or claude_processes or plugin_remnants or _any_claude_tmux_pane(binding, tmux):
        raise BridgeError("claude receiver still present")
    return evidence


def _runtime_owned_error_is_transient(message: str) -> bool:
    transient_fragments = (
        "tmux pane is not present",
        "receiver process is not present",
        "plugin group is not present",
        "receiver still present",
    )
    hard_fragments = (
        "active/enabled dormant systemd supervisor",
        "not uniquely anchored",
        "plugin group is duplicate",
        "orphan supervisor",
    )
    if any(fragment in message for fragment in hard_fragments):
        return False
    return any(fragment in message for fragment in transient_fragments)


#: T-4184: what the codex poller logs on a getUpdates 409 (codex-config
#: codex_telegram/poller.py: log.error("409 Conflict — another getUpdates poller
#: holds the token; ...")). Matched instead of a bare "409".
CODEX_POLLER_CONFLICT_MARKER = "409 Conflict"
#: Two or more conflict lines in the current run = sustained (unchanged semantics).
CODEX_POLLER_CONFLICT_THRESHOLD = 2


class LocalReceiverBackend:
    def __init__(self, *, timeout_seconds: int = 90, proc_root: Path = Path("/proc")):
        self.timeout_seconds = timeout_seconds
        self.proc_root = proc_root

    def _run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._run(("systemctl", "--user", *args))
        if result.returncode != 0:
            raise BridgeError(f"systemctl {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def _stop_unit_tolerating_not_found(self, unit: str) -> None:
        """`systemctl --user stop`, except a unit that does not exist on this
        account (LoadState=not-found; `stop` exits 5) counts as already
        stopped (T-4158): there is nothing to stop, and the absence probe that
        follows (ActiveState/MainPID plus a process scan) still decides. Any
        other stop failure raises, as `_systemctl` always has."""
        result = self._run(("systemctl", "--user", "stop", unit))
        if result.returncode == 0:
            return
        load = self._run(("systemctl", "--user", "show", unit, "-p", "LoadState", "--value"))
        if load.returncode == 0 and load.stdout.strip() == "not-found":
            return
        raise BridgeError(f"systemctl stop {unit} failed: {result.stderr.strip()}")

    def _unit_probe(self, unit: str) -> UnitProbe:
        result = self._systemctl("show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID")
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return UnitProbe(
            unit=unit,
            active_state=values.get("ActiveState", ""),
            sub_state=values.get("SubState", ""),
            main_pid=values.get("MainPID", ""),
        )

    def _unit_enabled_state(self, unit: str) -> str:
        result = self._run(("systemctl", "--user", "is-enabled", unit))
        value = result.stdout.strip()
        if value:
            return value
        return "enabled" if result.returncode == 0 else "disabled"

    def _require_current_account(self, binding: CodexTelegramBinding) -> None:
        current = pwd.getpwuid(os.geteuid()).pw_name
        if current != binding.account:
            raise BridgeError("receiver handoff backend must run inside target account")

    def _tmux_absent(self, binding: CodexTelegramBinding) -> bool:
        return self._run(("tmux", "has-session", "-t", binding.tmux_session)).returncode != 0

    def _tmux_pane_snapshot(self, binding: CodexTelegramBinding) -> TmuxPaneSnapshot:
        result = self._run(
            (
                "tmux",
                "display-message",
                "-p",
                "-t",
                f"{binding.tmux_session}:{binding.tmux_pane}",
                "#{pid}\t#{pane_dead}\t#{pane_pid}\t#{pane_current_command}",
            )
        )
        if result.returncode != 0:
            return TmuxPaneSnapshot(False, True, 0, "")
        lines = result.stdout.rstrip("\n").splitlines()
        if len(lines) != 1:
            raise BridgeError("tmux target returned ambiguous pane evidence")
        fields = lines[0].split("\t")
        if len(fields) != 4:
            raise BridgeError("tmux target returned malformed pane evidence")
        server_pid, dead, pane_pid, command = fields
        if not server_pid.isdigit() or not pane_pid.isdigit():
            raise BridgeError("tmux target returned nonnumeric pane evidence")
        return TmuxPaneSnapshot(True, dead == "1", int(pane_pid), command, int(server_pid))

    def _pid_cgroup_text(self, pid: int) -> str:
        if pid <= 0:
            raise BridgeError("cgroup PID is invalid")
        path = self.proc_root / str(pid) / "cgroup"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BridgeError("cgroup evidence is unreadable") from exc
        if not content.strip():
            raise BridgeError("cgroup evidence is empty")
        return content

    def _process_matches(self, binding: CodexTelegramBinding, argv: str, *, kind: str) -> bool:
        if kind == "claude":
            process = _process_snapshot_from_line(argv)
            if process is None:
                return False
            return _is_exact_claude_receiver(binding, process) or _is_plugin_launcher(binding, process)
        if kind == "codex":
            return (
                "codex_telegram_bridge.py" in argv
                and "exec" in argv
                and "--mode" in argv
                and "poller" in argv
            ) or str(binding.current_release / "run_poller.py") in argv
        return False

    def _matching_processes(self, binding: CodexTelegramBinding, *, kind: str) -> list[str]:
        result = self._run(("ps", "-u", binding.account, "-o", "pid=,args="))
        if result.returncode != 0:
            raise BridgeError("account process probe failed")
        matches = []
        for line in result.stdout.splitlines():
            if self._process_matches(binding, line, kind=kind):
                matches.append(line.strip())
        return matches

    def _process_snapshots(self, binding: CodexTelegramBinding) -> tuple[ProcessSnapshot, ...]:
        result = self._run(("ps", "-u", binding.account, "-o", "pid=,ppid=,args="))
        if result.returncode != 0:
            raise BridgeError("account process probe failed")
        snapshots = []
        for line in result.stdout.splitlines():
            snapshot = _process_snapshot_from_line(line)
            if snapshot is not None:
                cwd = ""
                try:
                    cwd = os.readlink(Path("/proc") / str(snapshot.pid) / "cwd")
                except OSError:
                    pass
                snapshots.append(dataclasses.replace(snapshot, cwd=cwd))
        return tuple(snapshots)

    def _watchdog_unit(self, binding: CodexTelegramBinding, suffix: str) -> str:
        matches = [unit for unit in binding.watchdog_units if unit.endswith(suffix)]
        if len(matches) != 1:
            raise BridgeError(f"watchdog {suffix} unit is not declared")
        return matches[0]

    def _topology_snapshot_path(self, binding: CodexTelegramBinding) -> Path:
        return binding.state_dir / "receipts" / "receiver-topology-snapshot.json"

    def _topology_snapshot_identity(self, binding: CodexTelegramBinding) -> dict[str, str]:
        return {
            "schema": TOPOLOGY_SNAPSHOT_SCHEMA,
            "persona": binding.persona,
            "account": binding.account,
            "claude_receiver_topology": binding.claude_receiver_topology,
        }

    def _read_topology_snapshot(self, binding: CodexTelegramBinding, *, require_existing: bool = False) -> dict[str, Any]:
        path = self._topology_snapshot_path(binding)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if require_existing:
                raise BridgeError("receiver topology snapshot missing")
            return self._topology_snapshot_identity(binding) | {"complete": True, "units": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("receiver topology snapshot is unreadable") from exc
        identity = self._topology_snapshot_identity(binding)
        if not isinstance(raw, dict) or any(raw.get(key) != value for key, value in identity.items()):
            raise BridgeError("receiver topology snapshot identity mismatch")
        units = raw.get("units")
        if not isinstance(units, dict):
            raise BridgeError("receiver topology snapshot units missing")
        return raw

    def _write_topology_snapshot(self, binding: CodexTelegramBinding, payload: Mapping[str, Any]) -> None:
        path = self._topology_snapshot_path(binding)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        _atomic_write_text(path, content, 0o600)

    def _snapshot_unit(self, unit: str) -> dict[str, str]:
        probe = self._unit_probe(unit)
        return {
            "active_state": probe.active_state,
            "sub_state": probe.sub_state,
            "main_pid": probe.main_pid or "0",
            "enabled": self._unit_enabled_state(unit),
        }

    def _snapshot_units(self, binding: CodexTelegramBinding, *, units: Sequence[str]) -> None:
        payload = self._read_topology_snapshot(binding)
        unit_states = {} if payload.get("complete") is True else dict(payload.get("units", {}))
        for unit in units:
            if unit not in unit_states:
                unit_states[unit] = self._snapshot_unit(unit)
        updated = {
            **self._topology_snapshot_identity(binding),
            "complete": False,
            "updated_at": _utc_timestamp(),
            "units": unit_states,
        }
        self._write_topology_snapshot(binding, updated)

    def _snapshot_dormant_claude_unit(self, binding: CodexTelegramBinding) -> None:
        snapshot = self._snapshot_unit(binding.claude_receiver_unit)
        if binding.claude_receiver_topology == "runtime-owned":
            probe = UnitProbe(
                binding.claude_receiver_unit,
                snapshot["active_state"],
                snapshot["sub_state"],
                snapshot["main_pid"],
            )
            if not _unit_inactive_no_pid(probe) or snapshot["enabled"] == "enabled":
                raise BridgeError("runtime-owned dormant Claude receiver unit is active/enabled before cutover")
        self._snapshot_units(binding, units=(binding.claude_receiver_unit,))

    def _phase_binding(self, binding: CodexTelegramBinding, topology_phase: TopologyPhase) -> CodexTelegramBinding:
        if topology_phase == "managed":
            return binding
        return dataclasses.replace(
            binding,
            claude_receiver_topology=binding.predecessor_claude_receiver_topology,
        )

    def _claude_state_once(
        self,
        binding: CodexTelegramBinding,
        expected: Literal["present", "absent"],
        *,
        topology_phase: TopologyPhase = "managed",
    ) -> Mapping[str, str]:
        phase_binding = self._phase_binding(binding, topology_phase)
        probe = self._unit_probe(phase_binding.claude_receiver_unit)
        tmux = self._tmux_pane_snapshot(phase_binding)
        processes = self._process_snapshots(phase_binding)
        if phase_binding.claude_receiver_topology == "runtime-owned":
            evidence = _evaluate_runtime_owned_claude_state(
                phase_binding,
                expected=expected,
                unit_probe=probe,
                unit_enabled=self._unit_enabled_state(phase_binding.claude_receiver_unit),
                tmux=tmux,
                processes=processes,
            )
        else:
            unit_main_cgroup = ""
            tmux_server_cgroup = ""
            if expected == "present":
                unit_pid = int(probe.main_pid) if probe.main_pid.isdigit() else 0
                unit_main_cgroup = self._pid_cgroup_text(unit_pid)
                tmux_server_cgroup = self._pid_cgroup_text(tmux.server_pid)
            evidence = _evaluate_systemd_user_claude_state(
                phase_binding,
                expected=expected,
                unit_probe=probe,
                tmux=tmux,
                processes=processes,
                unit_main_cgroup=unit_main_cgroup,
                tmux_server_cgroup=tmux_server_cgroup,
            )
        return {
            **evidence,
            "topology_phase": topology_phase,
            "managed_claude_receiver_topology": binding.claude_receiver_topology,
            "predecessor_claude_receiver_topology": binding.predecessor_claude_receiver_topology,
        }

    def _require_claude_state(
        self,
        binding: CodexTelegramBinding,
        *,
        expected: Literal["present", "absent"],
        topology_phase: TopologyPhase = "managed",
        wait: bool = True,
    ) -> Mapping[str, str]:
        phase_binding = self._phase_binding(binding, topology_phase)
        # T-6016: wait=False is a single-shot read for callers holding the runtime-operation lock.
        if not wait or phase_binding.claude_receiver_topology != "runtime-owned":
            return self._claude_state_once(binding, expected, topology_phase=topology_phase)
        deadline = time.monotonic() + float(self.timeout_seconds)
        last: BridgeError | None = None
        while True:
            try:
                return self._claude_state_once(binding, expected, topology_phase=topology_phase)
            except BridgeError as exc:
                if not _runtime_owned_error_is_transient(str(exc)):
                    raise
                last = exc
            if time.monotonic() >= deadline:
                raise BridgeError(f"runtime-owned Claude receiver {expected} proof timed out: {last}") from last
            time.sleep(0.05)

    def _require_absent(
        self,
        binding: CodexTelegramBinding,
        *,
        kind: str,
        topology_phase: TopologyPhase = "managed",
        wait: bool = True,
    ) -> Mapping[str, str]:
        if kind == "claude":
            return self._require_claude_state(binding, expected="absent", topology_phase=topology_phase, wait=wait)
        unit = binding.claude_receiver_unit if kind == "claude" else binding.codex_receiver_unit
        probe = self._unit_probe(unit)
        inactive = probe.active_state in {"inactive", "failed"} and probe.main_pid in {"", "0"}
        tmux_absent = True if kind == "codex" else self._tmux_absent(binding)
        processes = self._matching_processes(binding, kind=kind)
        if not inactive or not tmux_absent or processes:
            raise BridgeError(f"{kind} receiver still present")
        return {"kind": kind, "expected": "absent"}

    def _require_present(
        self,
        binding: CodexTelegramBinding,
        *,
        kind: str,
        topology_phase: TopologyPhase = "managed",
        wait: bool = True,
    ) -> Mapping[str, str]:
        if kind == "claude":
            return self._require_claude_state(binding, expected="present", topology_phase=topology_phase, wait=wait)
        unit = binding.claude_receiver_unit if kind == "claude" else binding.codex_receiver_unit
        probe = self._unit_probe(unit)
        if probe.active_state != "active" or probe.main_pid in {"", "0"}:
            raise BridgeError(f"{kind} receiver unit is not active")
        return {"kind": kind, "expected": "present", "unit_active_state": probe.active_state, "unit_main_pid": probe.main_pid or "0"}

    def _restore_units(self, binding: CodexTelegramBinding, *, units_to_restore: Sequence[str]) -> None:
        payload = self._read_topology_snapshot(binding, require_existing=True)
        if payload.get("complete") is True:
            raise BridgeError("receiver topology snapshot is already complete")
        units = payload.get("units")
        if not isinstance(units, Mapping):
            raise BridgeError("receiver topology snapshot units missing")
        for unit in units_to_restore:
            raw = units.get(unit)
            if not isinstance(raw, Mapping):
                raise BridgeError("receiver unit snapshot missing")
            enabled = str(raw.get("enabled", "disabled"))
            active = str(raw.get("active_state", "inactive"))
            if enabled == "enabled":
                self._systemctl("enable", unit)
            else:
                self._systemctl("disable", unit)
            if active == "active":
                self._systemctl("start", unit)
            else:
                self._systemctl("stop", unit)

    def _restore_watchdog_units(self, binding: CodexTelegramBinding) -> None:
        self._restore_units(binding, units_to_restore=binding.watchdog_units)

    def _restore_claude_receiver(self, binding: CodexTelegramBinding) -> None:
        if binding.claude_receiver_topology == "runtime-owned":
            snapshot = self._read_topology_snapshot(binding, require_existing=True).get("units", {}).get(binding.claude_receiver_unit)
            if isinstance(snapshot, Mapping) and (snapshot.get("enabled") == "enabled" or snapshot.get("active_state") == "active"):
                raise BridgeError("runtime-owned dormant Claude receiver restore would start/enable unit")
        self._restore_units(binding, units_to_restore=(binding.claude_receiver_unit,))

    def _unit_active_enter_epoch(self, unit: str) -> float:
        """Wall-clock epoch of `unit`'s current activation (T-4184), derived
        from ActiveEnterTimestampMonotonic (format-free, same clock as
        time.monotonic()). Raises when the unit has no current activation."""
        result = self._systemctl("show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value")
        raw = result.stdout.strip()
        if not raw.isdigit() or int(raw) <= 0:
            raise BridgeError(f"{unit} has no current activation timestamp")
        return time.time() - (time.monotonic() - int(raw) / 1_000_000)

    def _codex_poller_conflicts_since_start(self, binding: CodexTelegramBinding) -> int:
        """T-4184: count getUpdates 409 Conflict lines the codex poller logged
        during its CURRENT run. Bounded by the unit's activation time (earlier
        runs' 409s, e.g. from a resolved double-poller incident, never count),
        message bodies only (`-o cat`, so a journal PID such as 140923 cannot
        match), and matched on the poller's conflict text
        (`CODEX_POLLER_CONFLICT_MARKER`), not a bare "409"."""
        since = math.ceil(self._unit_active_enter_epoch(binding.codex_receiver_unit))
        journal = self._run(
            (
                "journalctl", "--user", "-u", binding.codex_receiver_unit,
                "--since", f"@{since}", "-o", "cat", "-n", "200", "--no-pager",
            )
        )
        if journal.returncode != 0:
            return 0
        return sum(1 for line in journal.stdout.splitlines() if CODEX_POLLER_CONFLICT_MARKER in line)

    def receiver_topology_snapshot_pending(self, binding: CodexTelegramBinding) -> bool:
        """Read-only (T-4178): True when a claude-to-codex pre phase left an
        INCOMPLETE receiver topology snapshot, i.e. there is saved unit state
        that a codex-to-claude post phase must restore and complete. A missing
        snapshot, or one already completed, is False: the post phase would
        refuse on it, and there is nothing to restore. An unreadable or
        foreign-identity snapshot raises BridgeError (never guessed)."""
        self._require_current_account(binding)
        return self._read_topology_snapshot(binding).get("complete") is not True

    def _complete_topology_snapshot(self, binding: CodexTelegramBinding) -> None:
        payload = self._read_topology_snapshot(binding, require_existing=True)
        updated = dict(payload)
        updated["complete"] = True
        updated["completed_at"] = _utc_timestamp()
        self._write_topology_snapshot(binding, updated)

    def execute(self, action: str, binding: CodexTelegramBinding) -> Mapping[str, str]:
        self._require_current_account(binding)
        if action == "snapshot-watchdog-units":
            self._snapshot_units(binding, units=binding.watchdog_units)
        elif action == "disable-watchdog-timer":
            self._systemctl("disable", "--now", self._watchdog_unit(binding, ".timer"))
        elif action == "stop-watchdog-service":
            self._systemctl("disable", "--now", self._watchdog_unit(binding, ".service"))
        elif action == "snapshot-dormant-claude-unit":
            self._snapshot_dormant_claude_unit(binding)
        elif action == "snapshot-claude-receiver":
            self._snapshot_units(binding, units=(binding.claude_receiver_unit,))
        elif action == "disable-dormant-claude-receiver":
            self._systemctl("disable", binding.claude_receiver_unit)
        elif action == "stop-dormant-claude-receiver":
            self._systemctl("stop", binding.claude_receiver_unit)
        elif action == "disable-claude-receiver":
            self._systemctl("disable", binding.claude_receiver_unit)
        elif action == "stop-claude-receiver":
            self._systemctl("stop", binding.claude_receiver_unit)
        elif action == "probe-claude-absent":
            self._require_absent(binding, kind="claude")
        elif action == "stop-codex-poller":
            self._stop_unit_tolerating_not_found(binding.codex_receiver_unit)
        elif action == "probe-codex-absent":
            self._require_absent(binding, kind="codex")
        elif action == "start-codex-poller":
            self._systemctl("start", binding.codex_receiver_unit)
        elif action == "probe-codex-present":
            self._require_present(binding, kind="codex")
        elif action == "health-codex-no-409":
            self._require_present(binding, kind="codex")
            conflicts = self._codex_poller_conflicts_since_start(binding)
            if conflicts >= CODEX_POLLER_CONFLICT_THRESHOLD:
                raise BridgeError(
                    f"{HEALTH_409_ERROR_PREFIX}: {conflicts} getUpdates 409 Conflict lines "
                    "since the poller's current start"
                )
        elif action == "enable-claude-receiver":
            self._systemctl("enable", binding.claude_receiver_unit)
        elif action == "start-claude-receiver":
            self._systemctl("start", binding.claude_receiver_unit)
        elif action == "restore-claude-receiver":
            self._restore_claude_receiver(binding)
        elif action == "probe-claude-present":
            self._require_present(binding, kind="claude")
        elif action == "enable-watchdog-service":
            self._systemctl("enable", self._watchdog_unit(binding, ".service"))
        elif action == "start-watchdog-service":
            self._systemctl("start", self._watchdog_unit(binding, ".service"))
        elif action == "enable-watchdog-timer":
            self._systemctl("enable", self._watchdog_unit(binding, ".timer"))
        elif action == "start-watchdog-timer":
            self._systemctl("start", self._watchdog_unit(binding, ".timer"))
        elif action == "restore-watchdog-units":
            self._restore_watchdog_units(binding)
        elif action == "complete-receiver-topology-snapshot":
            self._complete_topology_snapshot(binding)
        else:
            raise BridgeError(f"unsupported receiver handoff action {action!r}")
        return {"action": action, "ok": "true"}


def render_unit(binding: CodexTelegramBinding) -> str:
    return "\n".join(
        [
            "# Managed by pantheon persona installer",
            "[Unit]",
            "Description=Pantheon Codex Telegram poller",
            "After=network-online.target",
            "Wants=network-online.target",
            # T-6352: bound a sustained failure to one latched failed state.
            "StartLimitIntervalSec=3600",
            "StartLimitBurst=30",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=CODEX_TG_PANE={binding.effective_pane}",
            "ExecStart=/usr/bin/python3 %h/pantheon-config/tools/codex_telegram_bridge.py exec --mode poller",
            "Restart=always",
            "RestartSec=5",
            # T-6352: exponential backoff (5..120s); NRestarts is cumulative.
            "RestartSteps=5",
            "RestartMaxDelaySec=120",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def render_allowlist() -> str:
    return json.dumps({"allow": [OPERATOR_CHAT_ID]}, sort_keys=True) + "\n"


def expected_mcp_snippet(binding: CodexTelegramBinding) -> str:
    snippet = (
        f"[mcp_servers.{binding.mcp_name}]\n"
        f"command = \"python3\"\n"
        f"args = [\"{binding.account_home}/pantheon-config/tools/codex_telegram_bridge.py\", \"exec\", \"--mode\", \"server\"]\n"
    )
    if codex_binding_requires_unattended_telegram(binding):
        snippet += "\n" + _render_telegram_tool_approval_tables(binding.mcp_name)
    return snippet


def codex_binding_requires_unattended_telegram(binding: CodexTelegramBinding) -> bool:
    return binding.unattended_telegram_tools


def _render_telegram_tool_approval_tables(mcp_name: str) -> str:
    lines: list[str] = []
    for tool, approval_mode in TELEGRAM_UNATTENDED_TOOL_APPROVALS.items():
        lines.extend(
            [
                f"[mcp_servers.{mcp_name}.tools.{tool}]",
                f"approval_mode = \"{approval_mode}\"",
                "",
            ]
        )
    return "\n".join(lines)


def _expected_mcp_table(binding: CodexTelegramBinding) -> Mapping[str, Any]:
    return tomllib.loads(expected_mcp_snippet(binding))["mcp_servers"][binding.mcp_name]


def mcp_registration_compatibility(
    data: Mapping[str, Any],
    *,
    mcp_name: str,
    expected_table: Mapping[str, Any],
) -> bool | None:
    servers = data.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, Mapping):
        return False
    existing = servers.get(mcp_name)
    if existing is None:
        return None
    if not isinstance(existing, Mapping):
        return False
    for key in ("command", "args"):
        if key not in existing or existing.get(key) != expected_table.get(key):
            return False
    expected_tools = expected_table.get("tools")
    if expected_tools is None:
        return True
    if not isinstance(expected_tools, Mapping):
        return False
    if "default_tools_approval_mode" in existing or "approval_mode" in existing:
        return False
    existing_tools = existing.get("tools")
    if not isinstance(existing_tools, Mapping):
        return False
    if set(existing_tools) != set(expected_tools):
        return False
    for tool, expected_tool in expected_tools.items():
        existing_tool = existing_tools.get(tool)
        if not isinstance(existing_tool, Mapping) or not isinstance(expected_tool, Mapping):
            return False
        if existing_tool.get("approval_mode") != expected_tool.get("approval_mode"):
            return False
    return True


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    detail: str


def receiver_readiness(
    binding: CodexTelegramBinding,
    *,
    managed_marker: str,
    poller_unit: Path | None = None,
    codex_config: Path | None = None,
) -> list[ReadinessCheck]:
    """T-5451: is the codex Telegram receiver installed well enough for a claude->codex
    swap to hand comms over? The ONE shared definition, used by the runtime controller
    (mid-session cap swap gate) and `persona_installer_verify_seat` (post-install check).

    Pure file inspection, no side effects. Paths default to the binding's own account
    home; verify-seat passes the plan's resolved targets. `managed_marker` is the
    installer banner (passed in: this module cannot import persona_installer).
    """
    unit = poller_unit or (binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit)
    try:
        banner = unit.is_file() and unit.read_text(encoding="utf-8", errors="replace").startswith(f"# {managed_marker}")
    except OSError:
        banner = False
    checks = [ReadinessCheck("poller-unit-installed", bool(banner), str(unit))]

    current = binding.current_release
    if current.is_symlink():
        target = Path(os.readlink(current))
        checks.append(ReadinessCheck(
            "release-current-pinned", target.name == binding.source.revision,
            f"current -> {target}; pinned {binding.source.revision}"))
    else:
        checks.append(ReadinessCheck("release-current-pinned", False, f"{current} is not a symlink"))

    config = codex_config or (binding.account_home / ".codex" / "config.toml")
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        checks.append(ReadinessCheck("codex-mcp-registered", False, f"codex config unreadable: {exc}"))
    else:
        verdict = mcp_registration_compatibility(
            data, mcp_name=binding.mcp_name, expected_table=_expected_mcp_table(binding))
        checks.append(ReadinessCheck(
            "codex-mcp-registered", verdict is True,
            {True: "registered", False: "differs from expected", None: "absent"}[verdict]))
    return checks


def ensure_mcp_registration(path: Path, binding: CodexTelegramBinding) -> McpRegistrationReceipt:
    expected = expected_mcp_snippet(binding)
    digest = _sha256_bytes(expected.encode("utf-8"))
    if path.exists():
        current = path.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(current)
        except tomllib.TOMLDecodeError as exc:
            raise BridgeError("existing Codex config is not valid TOML") from exc
        match = mcp_registration_compatibility(
            data,
            mcp_name=binding.mcp_name,
            expected_table=_expected_mcp_table(binding),
        )
        if match is True:
            return McpRegistrationReceipt("pantheon-codex-mcp-registration/v1", binding.mcp_name, False, digest, False, None)
        if match is False:
            raise BridgeError("existing Codex MCP registration does not match T-2950 bridge")
        suffix = ("" if current.endswith("\n") else "\n") + "\n" + expected
        path.write_text(current + suffix, encoding="utf-8")
        path.chmod(0o600)
        return McpRegistrationReceipt(
            "pantheon-codex-mcp-registration/v1",
            binding.mcp_name,
            True,
            digest,
            False,
            _sha256_bytes(suffix.encode("utf-8")),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")
    path.chmod(0o600)
    return McpRegistrationReceipt(
        "pantheon-codex-mcp-registration/v1",
        binding.mcp_name,
        True,
        digest,
        True,
        None,
    )


def rollback_mcp_registration(path: Path, binding: CodexTelegramBinding, receipt: McpRegistrationReceipt) -> None:
    if not receipt.created or not path.exists():
        return
    expected = expected_mcp_snippet(binding)
    current = path.read_text(encoding="utf-8")
    if receipt.created_file and current == expected:
        path.unlink()
        return
    suffixes = (("\n\n" + expected), ("\n" + expected))
    for suffix in suffixes:
        if receipt.appended_suffix_sha256 == _sha256_bytes(suffix.encode("utf-8")) and current.endswith(suffix):
            path.write_text(current[: -len(suffix)], encoding="utf-8")
            return


def _file_snapshot(path: Path) -> Mapping[str, str]:
    if path.is_symlink():
        target = os.readlink(path)
        return {"state": "symlink", "target": target, "fingerprint": _sha256_bytes(target.encode("utf-8"))}
    if not path.exists():
        return {"state": "absent"}
    if path.is_file():
        raw = path.read_bytes()
        return {"state": "file", "content": raw.decode("utf-8"), "fingerprint": _sha256_bytes(raw)}
    return {"state": "other"}


def _restore_file_snapshot(path: Path, before: Mapping[str, str], expected_after: str | None) -> None:
    current = _file_snapshot(path)
    if expected_after is not None and current.get("fingerprint") != expected_after:
        return
    if before.get("state") == "absent":
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    if before.get("state") == "file":
        path.write_text(str(before["content"]), encoding="utf-8")
        return
    if before.get("state") == "symlink":
        tmp = path.parent / f".{path.name}.rollback-{os.getpid()}"
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(str(before["target"]))
        os.replace(tmp, path)


def _atomic_write_text(path: Path, content: str, mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(content.encode("utf-8"))


def _assert_codex_disabled_first(binding: CodexTelegramBinding) -> Mapping[str, str]:
    backend = LocalReceiverBackend()
    backend._require_current_account(binding)
    probe = backend._unit_probe(binding.codex_receiver_unit)
    enabled = backend._run(("systemctl", "--user", "is-enabled", binding.codex_receiver_unit))
    unit = backend._run(("systemctl", "--user", "cat", binding.codex_receiver_unit))
    if enabled.returncode == 0:
        raise BridgeError("codex telegram receiver is enabled before cutover")
    if probe.active_state not in {"inactive", "failed"} or probe.main_pid not in {"", "0"}:
        raise BridgeError("codex telegram receiver is active before cutover")
    if unit.returncode == 0 and "EnvironmentFile" in unit.stdout:
        raise BridgeError("codex telegram unit must not embed an EnvironmentFile")
    return {
        "active": probe.active_state,
        "enabled": enabled.stdout.strip() or "disabled",
        "main_pid": probe.main_pid or "0",
        "environment_file": "absent",
    }


def bridge_stage(archive_path: Path, binding: CodexTelegramBinding, *, fault_after: str | None = None) -> Mapping[str, Any]:
    rollbacks: list[tuple[str, Any]] = []
    completed: list[str] = []
    release_dir = binding.release_root / "releases" / binding.source.revision
    release_existed = release_dir.exists()
    try:
        allowlist_path = binding.state_dir / "allowlist.json"
        allow_before = _file_snapshot(allowlist_path)
        current_before = _file_snapshot(binding.current_release)
        release = stage_archive_release(archive_path, binding, fault_after=fault_after)
        current_after = _file_snapshot(binding.current_release)
        rollbacks.append(("snapshot", binding.current_release, current_before, current_after.get("fingerprint")))
        if not release_existed:
            rollbacks.append(("release-dir", release_dir))
        completed.append("verified-archive-release")

        allow_sha = _atomic_write_text(allowlist_path, render_allowlist(), 0o600)
        rollbacks.append(("snapshot", allowlist_path, allow_before, allow_sha))
        completed.append("allowlist")
        _fault("bridge-allowlist", fault_after)

        unit_path = binding.account_home / ".config" / "systemd" / "user" / binding.codex_receiver_unit
        unit_before = _file_snapshot(unit_path)
        unit_sha = _atomic_write_text(unit_path, render_unit(binding), 0o644)
        rollbacks.append(("snapshot", unit_path, unit_before, unit_sha))
        completed.append("unit-write")
        _fault("bridge-unit", fault_after)

        reload_result = subprocess.run(("systemctl", "--user", "daemon-reload"), check=False, capture_output=True, text=True)
        if reload_result.returncode != 0:
            raise BridgeError((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
        completed.append("daemon-reload")
        _fault("bridge-daemon-reload", fault_after)

        mcp_path = binding.account_home / ".codex" / "config.toml"
        mcp_receipt = ensure_mcp_registration(mcp_path, binding)
        rollbacks.append(("mcp", mcp_path, mcp_receipt))
        completed.append("mcp-registration")
        _fault("bridge-mcp", fault_after)

        disabled = _assert_codex_disabled_first(binding)
        completed.append("disabled-first")
        _fault("bridge-disabled-first", fault_after)
        receipt_path = binding.state_dir / "receipts" / f"stage-{binding.source.revision}.json"
        completed.append("receipt")
        receipt = {
            "schema": "pantheon-codex-telegram-bridge-stage/v1",
            "persona": binding.persona,
            "account": binding.account,
            "ok": True,
            "operations": completed,
            "revision": binding.source.revision,
            "file_sha256": dict(binding.source.sha256),
            "release": release,
            "effective_pane": binding.effective_pane,
            "allowlist_sha256": allow_sha,
            "unit_sha256": unit_sha,
            "mcp": dataclasses.asdict(mcp_receipt),
            "disabled_first": disabled,
        }
        receipt_content = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        receipt_before = _file_snapshot(receipt_path)
        receipt_sha = _atomic_write_text(receipt_path, receipt_content, 0o600)
        rollbacks.append(("snapshot", receipt_path, receipt_before, receipt_sha))
        receipt["operations"] = completed
        receipt["receipt_path"] = str(receipt_path)
        receipt["receipt_sha256"] = receipt_sha
        return receipt
    except Exception:
        for item in reversed(rollbacks):
            if item[0] == "mcp":
                _kind, path, receipt = item
                rollback_mcp_registration(path, binding, receipt)
            elif item[0] == "snapshot":
                _kind, path, before, after = item
                _restore_file_snapshot(path, before, after)
            elif item[0] == "release-dir":
                _kind, path = item
                _remove_owned_release_dir(path, binding.source)
        raise


def redact_secrets(value: Any, secret_values: Collection[str]) -> Any:
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, Mapping):
        return {str(key): redact_secrets(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, secret_values) for item in value]
    if dataclasses.is_dataclass(value):
        return redact_secrets(dataclasses.asdict(value), secret_values)
    return value


def telegram_preflight(client: Any) -> Mapping[str, Any]:
    webhook = client.call("getWebhookInfo")
    result = webhook.get("result") if isinstance(webhook, Mapping) else None
    if isinstance(result, Mapping) and result.get("url"):
        raise BridgeError("telegram webhook is active; refusing long polling")
    me = client.call("getMe")
    return {"webhook": webhook, "me": me}


def telegram_preflight_receipt(
    binding: CodexTelegramBinding,
    *,
    client: Any | None = None,
    timeout_seconds: float = TELEGRAM_PREFLIGHT_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    methods: list[str] = []
    token = ""
    try:
        if client is None:
            token = load_telegram_token(binding.credential_env, expected_uid=os.geteuid())
            client = TelegramApiClient(token, timeout_seconds=timeout_seconds)
        methods.append("getWebhookInfo")
        webhook = client.call("getWebhookInfo")
        webhook_result = webhook.get("result") if isinstance(webhook, Mapping) else None
        if not isinstance(webhook_result, Mapping):
            raise BridgeError("telegram getWebhookInfo response has invalid result field")
        if webhook_result.get("url"):
            return _receipt_mapping(
                TelegramPreflightReceipt(
                    schema=TELEGRAM_PREFLIGHT_SCHEMA,
                    persona=binding.persona,
                    account=binding.account,
                    ok=False,
                    webhook_active=True,
                    checked_at=_utc_timestamp(),
                    methods=tuple(methods),
                    error="telegram webhook is active; refusing long polling",
                )
            )
        methods.append("getMe")
        me = client.call("getMe")
        me_result = me.get("result") if isinstance(me, Mapping) else None
        if not isinstance(me_result, Mapping):
            raise BridgeError("telegram getMe response has invalid result field")
        bot_id, username = _bot_identity(me_result)
        return _receipt_mapping(
            TelegramPreflightReceipt(
                schema=TELEGRAM_PREFLIGHT_SCHEMA,
                persona=binding.persona,
                account=binding.account,
                ok=True,
                webhook_active=False,
                checked_at=_utc_timestamp(),
                methods=tuple(methods),
                bot_id=bot_id,
                bot_username=username,
            )
        )
    except Exception as exc:
        return _receipt_mapping(
            TelegramPreflightReceipt(
                schema=TELEGRAM_PREFLIGHT_SCHEMA,
                persona=binding.persona,
                account=binding.account,
                ok=False,
                webhook_active=False,
                checked_at=_utc_timestamp(),
                methods=tuple(methods),
                error=_sanitize_telegram_error(exc, token=token),
            )
        )


def source_currency_receipt(repo: Path, *, expected_head: str) -> Mapping[str, Any]:
    expected = validate_expected_revision(expected_head)
    actual = ""
    status_text = ""
    try:
        head = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
        )
        if head.returncode != 0:
            raise BridgeError("source-currency failed to inspect HEAD")
        actual = head.stdout.strip()
        status = subprocess.run(
            ("git", "-C", str(repo), "status", "--porcelain=v1"),
            check=False,
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            raise BridgeError("source-currency failed to inspect status")
        status_text = status.stdout
        clean = status_text == ""
        ok = actual == expected and clean
        error = None
        if actual != expected:
            error = "target checkout HEAD does not match expected revision"
        elif not clean:
            error = "target checkout is dirty"
        return dataclasses.asdict(
            SourceCurrencyReceipt(
                schema=SOURCE_CURRENCY_SCHEMA,
                ok=ok,
                repo=str(repo),
                expected_head=expected,
                actual_head=actual,
                clean=clean,
                status_line_count=0 if clean else len(status_text.splitlines()),
                status_sha256=_sha256_bytes(status_text.encode("utf-8")),
                checked_at=_utc_timestamp(),
                error=error,
            )
        )
    except Exception as exc:
        return dataclasses.asdict(
            SourceCurrencyReceipt(
                schema=SOURCE_CURRENCY_SCHEMA,
                ok=False,
                repo=str(repo),
                expected_head=expected,
                actual_head=actual,
                clean=False,
                status_line_count=0 if not status_text else len(status_text.splitlines()),
                status_sha256=_sha256_bytes(status_text.encode("utf-8")),
                checked_at=_utc_timestamp(),
                error=_bounded_error(f"{type(exc).__name__}: {exc}"),
            )
        )


def precutover_mailbox_probe(persona: str, mailbox_root: Path) -> Mapping[str, Any]:
    tmp = mailbox_root / persona / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    payload = f"t2950-delivery-{persona}-{time.time_ns()}".encode("utf-8")
    path = tmp / f"t2950-{hashlib.sha256(payload).hexdigest()}.json"
    path.write_bytes(payload)
    return {
        "schema": "pantheon-codex-telegram-mailbox-probe/v1",
        "persona": persona,
        "durable_delivery": path.is_file(),
        "consumption_verified": False,
        "sha256": _sha256_bytes(payload),
    }


def _rooted_path(root: Path, absolute: Path) -> Path:
    if not absolute.is_absolute():
        raise BridgeError(f"path must be absolute: {absolute}")
    # "//"-prefixed paths survive normpath unchanged (POSIX) but name the same
    # file as "/"; refused as a typed error, not relative_to's ValueError (T-4963)
    if str(absolute).startswith("//") or str(root).startswith("//"):
        raise BridgeError(f"path must not start with '//': {absolute}")
    if root == Path("/"):
        return absolute
    return root / absolute.relative_to("/")


def _load_manifest_document(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeError(f"{path}: cannot read manifest: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise BridgeError(f"{path}: manifest root must be a mapping")
    return raw


def _mailbox_roundtrip_context(
    *,
    manifest_path: Path,
    persona: str,
    reply_persona: str,
    root: Path,
) -> Mapping[str, Any]:
    manifest = _load_manifest(manifest_path)
    binding = manifest.binding(persona)
    _require_effective_account(binding.account)
    document = _load_manifest_document(manifest_path)
    mailbox = manifest.installer.get("mailbox")
    if not isinstance(mailbox, Mapping):
        raise BridgeError("persona_installer.mailbox missing")
    hub_raw = mailbox.get("hub")
    if not isinstance(hub_raw, str) or not Path(hub_raw).is_absolute():
        raise BridgeError("persona_installer.mailbox.hub must be absolute")
    mailbox_hub = _rooted_path(root, Path(hub_raw))
    bound_inbox = mailbox_hub / persona
    reply_inbox = mailbox_hub / reply_persona
    try:
        roots = resolve_mailbox_writable_roots(
            document,
            role=binding.role,
            harness="codex",
            bound_persona=persona,
            bound_mailbox=bound_inbox,
            mailbox_hub=mailbox_hub,
        )
    except MailboxWritableRootError as exc:
        raise BridgeError(str(exc)) from exc
    declared = {item.path for item in roots}
    if bound_inbox not in declared:
        raise BridgeError("bound mailbox is not declared as a Codex writable root")
    if reply_inbox not in declared:
        raise BridgeError("reply inbox is not declared as a Codex writable root")
    try:
        verify_maildir_writable_root(bound_inbox)
        verify_maildir_writable_root(reply_inbox)
    except MailboxWritableRootError as exc:
        raise BridgeError(str(exc)) from exc
    account_home = _rooted_path(root, Path("/home") / binding.account)
    state_root = account_home / ".local" / "state" / "pantheon" / "personas"
    return {
        "binding": binding,
        "bound_inbox": bound_inbox,
        "reply_inbox": reply_inbox,
        "state_root": state_root,
    }


def _roundtrip_dir(state_root: Path, persona: str) -> Path:
    return state_root / persona / "mailbox-roundtrip"


def _safe_correlation_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value):
        raise BridgeError("mailbox roundtrip correlation id must be a safe token")
    return value


def _roundtrip_state_path(state_root: Path, persona: str, correlation_id: str) -> Path:
    safe = _safe_correlation_id(correlation_id)
    return _roundtrip_dir(state_root, persona) / f"{safe}.json"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json_file(path: Path, *, limit: int = 65536) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BridgeError(f"cannot read {path.name}: {exc}") from exc
    if len(raw) > limit:
        raise BridgeError(f"{path.name} exceeds bounded JSON size")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise BridgeError(f"{path.name} must contain a JSON object")
    return data


def mailbox_roundtrip_prepare(
    *,
    manifest_path: Path,
    persona: str,
    reply_persona: str,
    correlation_id: str,
    root: Path = Path("/"),
) -> Mapping[str, Any]:
    ctx = _mailbox_roundtrip_context(
        manifest_path=manifest_path,
        persona=persona,
        reply_persona=reply_persona,
        root=root,
    )
    bound_inbox = ctx["bound_inbox"]
    reply_inbox = ctx["reply_inbox"]
    state_root = ctx["state_root"]
    nonce = uuid.uuid4().hex
    prepared_at = _utc_timestamp()
    payload = {
        "kind": "t2953_mailbox_roundtrip",
        "nonce": nonce,
        "correlation_id": correlation_id,
        "reply_inbox": str(reply_inbox),
        "reply_persona": reply_persona,
        "required_reply_kind": "t2953_mailbox_roundtrip_reply",
    }
    message_id = pantheon_mailbox.send(
        str(bound_inbox),
        payload,
        "codex-acceptance",
        subject=f"t2953-mailbox:{correlation_id}",
        dedupe_pending=False,
    )
    source_path = bound_inbox / "new" / message_id
    if not source_path.is_file():
        raise BridgeError("roundtrip source message was not delivered to new")
    source_record = _read_json_file(source_path)
    if source_record.get("id") != message_id:
        raise BridgeError("roundtrip source message id mismatch")
    state = {
        "schema": "pantheon-mailbox-roundtrip/v1",
        "persona": persona,
        "reply_persona": reply_persona,
        "correlation_id": correlation_id,
        "nonce": nonce,
        "nonce_sha256": _sha256_bytes(nonce.encode("utf-8")),
        "source_message_id": message_id,
        "bound_inbox": str(bound_inbox),
        "reply_inbox": str(reply_inbox),
        "prepared_at": prepared_at,
        "source_record_sha256": _sha256_bytes(json.dumps(source_record, sort_keys=True).encode("utf-8")),
    }
    state_path = _roundtrip_state_path(state_root, persona, correlation_id)
    _write_json_atomic(state_path, state)
    return {
        "schema": "pantheon-mailbox-roundtrip-prepare/v1",
        "ok": True,
        "persona": persona,
        "reply_persona": reply_persona,
        "correlation_id": correlation_id,
        "nonce": nonce,
        "nonce_sha256": state["nonce_sha256"],
        "source_message_id": message_id,
        "source_in_new": True,
        "state_path": str(state_path),
    }


def _matching_ack(path: Path, message_id: str) -> Mapping[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError(f"cannot read ack record: {exc}") from exc
    matches: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError("ack record is not valid JSONL") from exc
        if isinstance(record, Mapping) and record.get("id") == message_id:
            matches.append(record)
    if len(matches) > 1:
        raise BridgeError("multiple ack records matched roundtrip message")
    return matches[0] if matches else None


def _parse_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BridgeError(f"{field} must be a UTC timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BridgeError(f"{field} must be a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BridgeError(f"{field} must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise BridgeError(f"{field} must be UTC")
    return parsed.astimezone(timezone.utc)


def _controller_submission_matches(
    *, state_root: Path, persona: str, message_id: str, correlation_id: str, prepared_at: str
) -> Mapping[str, Any]:
    prepared = _parse_utc_timestamp(prepared_at, "prepared_at")
    path = state_root / persona / "controller-evidence.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BridgeError(f"controller evidence missing: {exc}") from exc
    matches: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError("controller evidence is not valid JSONL") from exc
        if not isinstance(record, Mapping):
            continue
        if not (
            record.get("schema") == "pantheon-runtime-controller-evidence/v1"
            and record.get("event") == "runtime_submission_accepted"
            and record.get("persona") == persona
            and record.get("message_id") == message_id
            and record.get("correlation_id") == correlation_id
        ):
            continue
        recorded = _parse_utc_timestamp(record.get("recorded_at"), "controller recorded_at")
        if recorded >= prepared:
            matches.append(record)
    if len(matches) != 1:
        raise BridgeError("controller wake/submission evidence is absent or ambiguous")
    return matches[0]


def _matching_reply(
    *, reply_inbox: Path, persona: str, reply_persona: str, correlation_id: str, nonce: str
) -> tuple[str, Mapping[str, Any]]:
    matches: list[tuple[str, Mapping[str, Any]]] = []
    try:
        candidates = sorted(path for path in (reply_inbox / "new").iterdir() if path.is_file())
    except OSError as exc:
        raise BridgeError(f"reply inbox is not readable: {exc}") from exc
    for path in candidates:
        record = _read_json_file(path)
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            record.get("sender") == persona
            and payload.get("kind") == "t2953_mailbox_roundtrip_reply"
            and payload.get("recipient") == reply_persona
            and payload.get("correlation_id") == correlation_id
            and payload.get("nonce") == nonce
        ):
            matches.append((path.name, record))
    if len(matches) != 1:
        raise BridgeError("roundtrip reply evidence is absent or ambiguous")
    return matches[0]


def mailbox_roundtrip_verify(
    *,
    manifest_path: Path,
    persona: str,
    reply_persona: str,
    correlation_id: str,
    root: Path = Path("/"),
) -> Mapping[str, Any]:
    ctx = _mailbox_roundtrip_context(
        manifest_path=manifest_path,
        persona=persona,
        reply_persona=reply_persona,
        root=root,
    )
    bound_inbox = ctx["bound_inbox"]
    reply_inbox = ctx["reply_inbox"]
    state_root = ctx["state_root"]
    state_path = _roundtrip_state_path(state_root, persona, correlation_id)
    state = _read_json_file(state_path)
    if (
        state.get("schema") != "pantheon-mailbox-roundtrip/v1"
        or state.get("persona") != persona
        or state.get("reply_persona") != reply_persona
        or state.get("correlation_id") != correlation_id
    ):
        raise BridgeError("mailbox roundtrip state does not match request")
    message_id = str(state.get("source_message_id", ""))
    nonce = str(state.get("nonce", ""))
    prepared_at = str(state.get("prepared_at", ""))
    if not message_id or not nonce or not prepared_at:
        raise BridgeError("mailbox roundtrip state is incomplete")
    if (bound_inbox / "new" / message_id).exists():
        raise BridgeError("source message is still unread in new")
    cur_path = bound_inbox / "cur" / message_id
    source_record = _read_json_file(cur_path)
    payload = source_record.get("payload")
    if not isinstance(payload, Mapping):
        raise BridgeError("source cur record payload is invalid")
    if payload.get("nonce") != nonce or payload.get("correlation_id") != correlation_id:
        raise BridgeError("source cur record does not match nonce/correlation")
    ack = _matching_ack(bound_inbox / ".acks.jsonl", message_id)
    if ack is None:
        raise BridgeError("source message ack record is missing")
    controller = _controller_submission_matches(
        state_root=state_root,
        persona=persona,
        message_id=message_id,
        correlation_id=correlation_id,
        prepared_at=prepared_at,
    )
    reply_id, reply = _matching_reply(
        reply_inbox=reply_inbox,
        persona=persona,
        reply_persona=reply_persona,
        correlation_id=correlation_id,
        nonce=nonce,
    )
    return {
        "schema": "pantheon-mailbox-roundtrip-verify/v1",
        "ok": True,
        "persona": persona,
        "reply_persona": reply_persona,
        "correlation_id": correlation_id,
        "nonce_sha256": _sha256_bytes(nonce.encode("utf-8")),
        "source_message_id": message_id,
        "source_in_cur": True,
        "ack_present": True,
        "controller_wake_event": "runtime_submission_accepted",
        "controller_evidence_sha256": _sha256_bytes(json.dumps(controller, sort_keys=True).encode("utf-8")),
        "reply_message_id": reply_id,
        "reply_record_sha256": _sha256_bytes(json.dumps(reply, sort_keys=True).encode("utf-8")),
    }


def offset_restart_receipt(before: int, updates: Sequence[int]) -> Mapping[str, Any]:
    delivered = [item for item in updates if item > before]
    return {
        "schema": "pantheon-codex-telegram-offset-restart/v1",
        "before_offset": before,
        "delivered_update_ids": delivered,
        "delivered_once": len(delivered) == len(set(delivered)),
        "after_offset": max([before] + delivered),
    }


def _default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "instance.yaml"


def _load_manifest(path: Path) -> Any:
    from tools.persona_installer import Manifest

    # lenient: the poller/MCP server/stage commands run live; a flip-key violation is not theirs to enforce
    return Manifest.load(path, strict_flip_keys=False)


def binding_for_current_account(
    manifest_path: Path,
    persona: str | None = None,
    *,
    root: Path = Path("/"),
) -> CodexTelegramBinding:
    manifest = _load_manifest(manifest_path)
    current = pwd.getpwuid(os.geteuid()).pw_name
    if persona is not None:
        binding = manifest.codex_telegram_binding(persona, root=root)
        if binding.account != current:
            raise BridgeError("requested persona does not match current account")
        return binding
    _source, personas = parse_bridge_block(manifest.installer.get("codex_telegram_bridge"))
    for candidate in sorted(personas):
        binding = manifest.codex_telegram_binding(candidate, root=root)
        if binding.account == current:
            return binding
    raise BridgeError("current account is not an approved Codex Telegram persona")


def _current_account_name() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _require_effective_account(expected_account: str) -> None:
    current = _current_account_name()
    if current != expected_account:
        raise BridgeError("current account does not match manifest binding")


def _resolve_proof_root(root: Path) -> Path:
    if not root.is_absolute():
        raise BridgeError("instruction proof root must be absolute")
    return root.resolve(strict=False)


def _instruction_source_proof(
    persona: str,
    *,
    manifest_path: Path,
    repo_root: Path,
    root: Path,
) -> Mapping[str, Any]:
    from tools.persona_installer import codex_standing_instructions_reference

    proof_root = _resolve_proof_root(root)
    manifest = _load_manifest(manifest_path)
    binding = manifest.codex_telegram_binding(persona, root=proof_root)
    _require_effective_account(binding.account)
    reference = codex_standing_instructions_reference(manifest, persona, root=proof_root)
    if reference is None:
        raise BridgeError(f"codex standing instructions missing for {persona!r}")
    source_rel = str(reference.source)
    commit = reference.source_commit
    cat_file = subprocess.run(
        ("git", "-C", str(repo_root), "cat-file", "-e", f"{commit}^{{commit}}"),
        check=False,
        capture_output=True,
        text=True,
    )
    if cat_file.returncode != 0:
        raise BridgeError("codex standing instructions source commit missing")
    show = subprocess.run(
        ("git", "-C", str(repo_root), "show", f"{commit}:{source_rel}"),
        check=False,
        capture_output=True,
    )
    if show.returncode != 0:
        raise BridgeError("codex standing instructions source object missing")
    object_sha = _sha256_bytes(show.stdout)
    source_path = (repo_root / reference.source).resolve(strict=False)
    if not source_path.is_relative_to(repo_root.resolve(strict=False)):
        raise BridgeError("codex standing instructions source escaped repo")
    try:
        worktree_bytes = source_path.read_bytes()
    except OSError as exc:
        raise BridgeError("codex standing instructions source worktree missing") from exc
    worktree_sha = _sha256_bytes(worktree_bytes)
    if object_sha != reference.source_sha256 or worktree_sha != reference.source_sha256:
        raise BridgeError("codex standing instructions source hash mismatch")
    text = worktree_bytes.decode("utf-8")
    if "mcp__telegram__reply" not in text:
        raise BridgeError("codex standing instructions source missing telegram reply identifier")
    return {
        "schema": INSTRUCTION_PROOF_SCHEMA,
        "kind": "source",
        "ok": True,
        "persona": persona,
        "account": binding.account,
        "manifest": str(manifest_path),
        "root": str(proof_root),
        "source": str(reference.source),
        "source_commit": reference.source_commit,
        "source_sha256": reference.source_sha256,
        "object_sha256": object_sha,
        "worktree_sha256": worktree_sha,
        "required_identifier": "mcp__telegram__reply",
    }


def _instruction_target_proof(
    persona: str,
    *,
    manifest_path: Path,
    root: Path,
) -> Mapping[str, Any]:
    from tools.persona_installer import codex_standing_instructions_reference

    proof_root = _resolve_proof_root(root)
    manifest = _load_manifest(manifest_path)
    binding = manifest.codex_telegram_binding(persona, root=proof_root)
    _require_effective_account(binding.account)
    reference = codex_standing_instructions_reference(manifest, persona, root=proof_root)
    if reference is None:
        raise BridgeError(f"codex standing instructions missing for {persona!r}")
    try:
        target_bytes = reference.target.read_bytes()
    except OSError as exc:
        raise BridgeError("codex standing instructions target missing") from exc
    target_sha = _sha256_bytes(target_bytes)
    if target_sha != reference.source_sha256:
        raise BridgeError("codex standing instructions target hash mismatch")
    try:
        target_text = target_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError("codex standing instructions target is not utf-8") from exc
    if "mcp__telegram__reply" not in target_text:
        raise BridgeError("codex standing instructions target missing telegram reply identifier")
    override_state = "absent"
    if reference.override_target.exists():
        try:
            override_size = reference.override_target.stat().st_size
        except OSError as exc:
            raise BridgeError("codex standing instructions override cannot be read") from exc
        if override_size != 0:
            raise BridgeError("codex standing instructions override is not empty")
        override_state = "empty"
    return {
        "schema": INSTRUCTION_PROOF_SCHEMA,
        "kind": "target",
        "ok": True,
        "persona": persona,
        "account": binding.account,
        "manifest": str(manifest_path),
        "root": str(proof_root),
        "target": str(reference.target),
        "target_sha256": target_sha,
        "override_target": str(reference.override_target),
        "override_state": override_state,
        "required_identifier": "mcp__telegram__reply",
    }


def _sudo_bash(account: str, inner: str, *, trailing_arg: str | None = None) -> str:
    command = shlex.join(("sudo", "-iu", account, "bash", "-lc", inner))
    # trailing_arg is a raw (operator-shell expanded) positional for the inner shell: $1.
    return command if trailing_arg is None else f"{command} _ {trailing_arg}"


# T-5066: archives are staged in private mktemp dirs, never at a fixed /tmp path.
_STAGE_SRC_VAR = "CODEX_TG_SRC_DIR"
_STAGE_DST_VAR = "CODEX_TG_DST_DIR"


def _staged_archive_refs(persona: str) -> tuple[str, str]:
    """Operator-shell references (expanded at paste time) for the source and target archives."""
    stem = f"codex-telegram-{persona}-{CODEX_TELEGRAM_SOURCE_REVISION}"
    return f'"${_STAGE_SRC_VAR}/{stem}.codex.tar"', f'"${_STAGE_DST_VAR}/{stem}.tar"'


def _staging_dirs_prefix(target_account: str, *, source_account: str | None = None) -> str:
    """mktemp -d (0700, unpredictable) dirs, each owned by the user who writes into it.

    The source dir belongs to the operator who runs source-check; root's install reads it.
    The target dir belongs to the seat account, which reads it for bridge-stage; root's
    install -o writes it. No other local user can create, swap or traverse either dir.
    """
    # T-5068: when a different account writes the source archive (orchestrator: the codex
    # clone owner), that account must own the dir; otherwise the operator does.
    src_maker = "mktemp -d" if source_account is None else f"sudo -u {shlex.quote(source_account)} mktemp -d"
    return (
        f"{_STAGE_SRC_VAR}=$({src_maker}) && "
        f"{_STAGE_DST_VAR}=$(sudo -u {shlex.quote(target_account)} mktemp -d) && "
    )


def _staging_cleanup_suffix(*, source_account: str | None = None) -> str:
    src_rm = "rm" if source_account is None else "sudo rm"  # dir owned by another account
    return (
        f' && {src_rm} -rf -- "${{{_STAGE_SRC_VAR}:?}}"'
        f' && sudo rm -rf -- "${{{_STAGE_DST_VAR}:?}}"'
        f" && unset {_STAGE_SRC_VAR} {_STAGE_DST_VAR}"
    )


def _staged_install_command(source_gate: str, account: str, source_ref: str, target_ref: str) -> str:
    install = shlex.join(("sudo", "install", "-o", account, "-g", account, "-m", "0600"))
    return f"{source_gate} && {install} {source_ref} {target_ref}"


def _source_currency_inner(bridge_tool: Path, repo: Path, expected_head: str) -> str:
    argv = (
        "python3",
        str(bridge_tool),
        "source-currency",
        "--repo",
        str(repo),
        "--expected-head",
        expected_head,
        "--json",
    )
    return shlex.join(argv)


def _target_account(account: str):
    try:
        return pwd.getpwnam(account)
    except KeyError as exc:
        raise BridgeError(f"target account {account!r} not found") from exc


def _target_account_environment(account: str, account_home: Path) -> Mapping[str, str]:
    user = _target_account(account)
    return controlled_apply_environment(account_home, user.pw_uid)


def _target_account_env_prefix(account: str, account_home: Path) -> tuple[str, ...]:
    env = _target_account_environment(account, account_home)
    return tuple(f"{key}={value}" for key, value in env.items())


def _target_account_shell_command(account: str, account_home: Path, command: str, *, forward_arg: bool = False) -> str:
    wrapped = shlex.join(
        (
            "/usr/bin/env",
            *_target_account_env_prefix(account, account_home),
            "/bin/bash",
            "-c",
            command,
        )
    )
    # forward_arg: hand the login shell's $1 on to the command's own $1 (T-5066).
    return f'{wrapped} _ "$1"' if forward_arg else wrapped


def _target_account_argv_command(account: str, account_home: Path, argv: Sequence[str | Path]) -> str:
    return shlex.join(
        (
            "/usr/bin/env",
            *_target_account_env_prefix(account, account_home),
            *(str(item) for item in argv),
        )
    )


_REPO_MODULE_ENTRYPOINTS = frozenset(
    {
        "tools.runtime_controller",
        "tools.bringup_acceptance.harness_codex_ready",
        "tools.bringup_acceptance.harness_codex_rc_ready",
    }
)


def _repo_module_command(
    repo: Path,
    module: str,
    *argv: str | Path,
    env: Sequence[str] = (),
) -> str:
    if not repo.is_absolute():
        raise BridgeError("repo module command requires an absolute repo path")
    if module not in _REPO_MODULE_ENTRYPOINTS:
        raise BridgeError(f"unsupported repo module entrypoint: {module!r}")
    command = ["python3", "-m", module, *(str(item) for item in argv)]
    if env:
        command = ["/usr/bin/env", *env, *command]
    return f"{shlex.join(('cd', str(repo)))} && {shlex.join(command)}"


def _target_gate(account: str, account_home: Path, bridge_tool: Path, repo: Path, expected_head: str) -> str:
    inner = _target_account_shell_command(
        account,
        account_home,
        f"set -euo pipefail; {_source_currency_inner(bridge_tool, repo, expected_head)}",
    )
    return _sudo_bash(account, inner)


def _gated_target_command(account: str, account_home: Path, bridge_tool: Path, repo: Path, expected_head: str, command: str, *, trailing_arg: str | None = None) -> str:
    compound = f"set -euo pipefail; {_source_currency_inner(bridge_tool, repo, expected_head)} && {command}"
    inner = _target_account_shell_command(account, account_home, compound, forward_arg=trailing_arg is not None)
    return _sudo_bash(account, inner, trailing_arg=trailing_arg)


def _operator_command(swap_tool: Path, state_root: Path, persona: str, destination: str, correlation_id: str) -> str:
    return shlex.join(
        (
            "/usr/bin/env",
            f"PANTHEON_PERSONA_STATE_DIR={state_root}",
            "python3",
            str(swap_tool),
            "--persona",
            persona,
            "--destination",
            destination,
            "--trigger",
            "operator",
            "--correlation-id",
            correlation_id,
        )
    )


def _operator_gate_compound(
    account: str,
    account_home: Path,
    bridge_tool: Path,
    swap_tool: Path,
    repo: Path,
    state_root: Path,
    *,
    persona: str,
    destination: str,
    correlation_id: str,
    expected_head: str,
) -> str:
    operator = _operator_command(swap_tool, state_root, persona, destination, correlation_id)
    gate = shlex.join(
        (
            "python3",
            str(swap_tool),
            "--completion-gate",
            "--expect-correlation-id",
            correlation_id,
            "--expect-destination",
            destination,
        )
    )
    compound = f"set -euo pipefail; {_source_currency_inner(bridge_tool, repo, expected_head)} && {operator} | {gate}"
    return _sudo_bash(account, _target_account_shell_command(account, account_home, compound))


def amendment_i_remediation(persona: str, binding: CodexTelegramBinding, *, expected_pantheon_revision: str) -> Mapping[str, Any]:
    expected = validate_expected_revision(expected_pantheon_revision)
    account = binding.account
    repo = binding.account_home / "pantheon-config"
    bridge_tool = repo / "tools" / "codex_telegram_bridge.py"
    source_archive, target_archive = _staged_archive_refs(persona)
    snapshot_dir = binding.state_dir / "receipts" / "amendment-i-pane-restage"
    offset_path = binding.state_dir / "poller.offset"
    source_gate = _target_gate(account, binding.account_home, bridge_tool, repo, expected)
    install_archive = _staged_install_command(source_gate, account, source_archive, target_archive)
    def receiver(kind: str, expected_state: str, *, topology_phase: TopologyPhase | None = None) -> str:
        argv = [
            "python3",
            str(bridge_tool),
            "receiver-state",
            "--persona",
            persona,
            "--kind",
            kind,
            "--expected",
            expected_state,
        ]
        if topology_phase is not None:
            argv.extend(("--topology-phase", topology_phase))
        argv.append("--json")
        return shlex.join(argv)
    snapshot = (
        f"snapshot_dir={shlex.quote(str(snapshot_dir))}; "
        f"unit={shlex.quote(binding.codex_receiver_unit)}; "
        f"offset={shlex.quote(str(offset_path))}; "
        'install -d -m 0700 "$snapshot_dir"; '
        'systemctl --user cat "$unit" > "$snapshot_dir/unit.before"; '
        'sha256sum "$snapshot_dir/unit.before" > "$snapshot_dir/unit.before.sha256"; '
        'if [ -f "$offset" ]; then '
        'cp -p "$offset" "$snapshot_dir/poller.offset.before"; '
        'sha256sum "$snapshot_dir/poller.offset.before" > "$snapshot_dir/poller.offset.before.sha256"; '
        'else : > "$snapshot_dir/poller.offset.absent"; fi'
    )
    startup_pane = (
        f"pid=$(systemctl --user show -p MainPID --value {shlex.quote(binding.codex_receiver_unit)}); "
        'test "$pid" != 0; '
        f"tr '\\0' '\\n' < /proc/$pid/environ | grep -Fx {shlex.quote('CODEX_TG_PANE=' + binding.effective_pane)}"
    )
    no_409 = (
        f"count=$(journalctl --user -u {shlex.quote(binding.codex_receiver_unit)} -n 80 --no-pager | "
        "grep -c '409' || true); test \"$count\" -lt 2"
    )
    steps = [
        _command_step(
            "ai-01-source-archive",
            "amendment-i-preflight",
            _staging_dirs_prefix(account)
            + shlex.join(
                (
                    "python3",
                    "/home/codex/pantheon-config/tools/codex_telegram_bridge.py",
                    "source-check",
                    "--persona",
                    persona,
                    "--source-repo",
                    str(source_repo_root(binding.source)),
                    "--archive",
                )
            )
            + f" {source_archive}",
        ),
        _command_step("ai-02-install-archive", "amendment-i-preflight", install_archive),
        _command_step("ai-03-snapshot-unit-offset", "amendment-i-preflight", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, snapshot)),
        _command_step("ai-04-stop-codex", "amendment-i-restage", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, shlex.join(("systemctl", "--user", "stop", binding.codex_receiver_unit)))),
        _command_step("ai-05-prove-codex-absent", "amendment-i-restage", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("codex", "absent"))),
        _command_step("ai-06-prove-claude-absent", "amendment-i-restage", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("claude", "absent"))),
        _command_step(
            "ai-07-bridge-stage-disabled",
            "amendment-i-restage",
            _gated_target_command(
                account,
                binding.account_home,
                bridge_tool,
                repo,
                expected,
                shlex.join(("python3", str(bridge_tool), "bridge-stage", "--persona", persona, "--archive")) + ' "$1" --json',
                trailing_arg=target_archive,
            )
            + _staging_cleanup_suffix(),
        ),
        _command_step("ai-08-daemon-reload", "amendment-i-restart", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, shlex.join(("systemctl", "--user", "daemon-reload")))),
        _command_step("ai-09-start-codex", "amendment-i-restart", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, shlex.join(("systemctl", "--user", "start", binding.codex_receiver_unit)))),
        _command_step("ai-10-prove-codex-present", "amendment-i-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("codex", "present"))),
        _command_step("ai-11-prove-claude-absent", "amendment-i-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("claude", "absent"))),
        _command_step("ai-12-prove-startup-pane", "amendment-i-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, startup_pane)),
        _command_step("ai-13-prove-no-sustained-409", "amendment-i-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, no_409)),
    ]
    return {
        "schema": "pantheon-codex-telegram-amendment-i-remediation/v1",
        "persona": persona,
        "account": account,
        "expected_pantheon_revision": expected,
        "source_revision": CODEX_TELEGRAM_SOURCE_REVISION,
        "effective_pane": binding.effective_pane,
        "snapshot_dir": str(snapshot_dir),
        "source_archive": source_archive,
        "target_archive": target_archive,
        "steps": steps,
        "commands": [step["command"] for step in steps if step["type"] == "command"],
        "rollback": {
            "unit_restore": "operator evidence only: restore prior unit bytes from snapshot if acceptance fails",
            "receiver_process": "prior wrong-pane unit restoration proves receiver process state only, not Telegram delivery restored",
        },
        "secret_values": "omitted",
    }


def _source_remediation_inner_commands(repo: Path, *, expected_head: str) -> tuple[str, ...]:
    expected = validate_expected_revision(expected_head)
    git = ("git", "-C", str(repo))
    remote_main = "refs/remotes/origin/main"
    return (
        shlex.join((*git, "rev-parse", "HEAD")),
        f'test -z "$({shlex.join((*git, "status", "--porcelain=v1"))})"',
        shlex.join((*git, "fetch", "origin", f"+main:{remote_main}")),
        f'test "$({shlex.join((*git, "rev-parse", "--verify", f"{remote_main}^{{commit}}"))})" = {shlex.quote(expected)}',
        shlex.join((*git, "merge", "--ff-only", expected)),
        f'test "$({shlex.join((*git, "rev-parse", "HEAD"))})" = {shlex.quote(expected)}',
        f'test -z "$({shlex.join((*git, "status", "--porcelain=v1"))})"',
    )


def source_remediation(account: str, repo: Path, *, expected_head: str) -> Mapping[str, Any]:
    expected = validate_expected_revision(expected_head)

    def target(inner: str) -> str:
        return _sudo_bash(account, f"set -euo pipefail; {inner}")

    commands = tuple(target(command) for command in _source_remediation_inner_commands(repo, expected_head=expected))
    return {
        "schema": "pantheon-source-remediation/v1",
        "account": account,
        "repo": str(repo),
        "expected_pantheon_revision": expected,
        "commands": commands,
        "auto_run": False,
    }


def _command_step(step_id: str, phase: str, command: str) -> Mapping[str, Any]:
    return dataclasses.asdict(CommandSheetStep(step_id=step_id, type="command", phase=phase, command=command))


def _operator_gate(step_id: str, phase: str, gate: Mapping[str, Any]) -> Mapping[str, Any]:
    return dataclasses.asdict(CommandSheetStep(step_id=step_id, type="operator-gate", phase=phase, gate=gate))


def _unattended_telegram_evidence_command(
    bridge_tool: Path,
    *,
    persona: str,
    stamp: str,
    manifest_path: Path,
    root: Path,
    step_id: str,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
) -> str:
    correlation_id = telegram_unattended_correlation_id(stamp, step_id)
    argv: list[str | Path] = [
        "python3",
        bridge_tool,
        "telegram-unattended-evidence",
        "--action",
        "record",
        "--persona",
        persona,
        "--stamp",
        stamp,
        "--step-id",
        step_id,
        "--tool",
        tool,
        "--canary",
        canary_kind,
        "--observed-correlation-id",
        correlation_id,
        "--result-status",
        "success",
        "--permission-prompt-observed",
        "false",
        "--ui-approval-clicked",
        "false",
        "--invocation-tool",
        tool,
        "--manifest",
        manifest_path,
        "--json",
    ]
    if root != Path("/"):
        argv.extend(("--root", root))
    return shlex.join(tuple(str(item) for item in argv))


def _unattended_telegram_prepare_command(
    bridge_tool: Path,
    *,
    persona: str,
    stamp: str,
    manifest_path: Path,
    root: Path,
) -> str:
    argv: list[str | Path] = [
        "python3",
        bridge_tool,
        "telegram-unattended-evidence",
        "--action",
        "prepare",
        "--persona",
        persona,
        "--stamp",
        stamp,
        "--manifest",
        manifest_path,
        "--json",
    ]
    if root != Path("/"):
        argv.extend(("--root", root))
    return shlex.join(tuple(str(item) for item in argv))


def _telegram_canary_operator_gate(
    *,
    step_id: str,
    phase: str,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
    stamp: str,
) -> Mapping[str, Any]:
    correlation_id = telegram_unattended_correlation_id(stamp, step_id)
    return _operator_gate(
        f"{step_id}-operator-action",
        phase,
        {
            "kind": "telegram-unattended-canary-operator-action",
            "runtime": "codex",
            "validator_step_id": step_id,
            "tool": tool,
            "canary": canary_kind,
            "correlation_id": correlation_id,
            "action": (
                "perform the Telegram canary action with the exact correlation token "
                f"{correlation_id}; write ONLY the target runtime observation JSON to the prepared "
                f"runtime-observations/{step_id}.json path using the prepare nonce/path from step "
                "19a; MUST NOT write the operator receipt or operator-receipts path; then run "
                f"validator step {step_id}"
            ),
            "hard_stop_and_rollback_if": [
                "any Codex MCP permission prompt appears",
                "any UI Allow or Always approval is needed or clicked",
                "the observed result is not success",
                "target writes the operator receipt surface instead of the runtime observation surface",
            ],
        },
    )


def telegram_unattended_correlation_id(stamp: str, step_id: str) -> str:
    return f"t2953-telegram-{stamp}-{step_id}"


def _safe_operator_stamp(stamp: str) -> str:
    if not TELEGRAM_UNATTENDED_SAFE_STAMP_RE.fullmatch(stamp):
        raise BridgeError("Telegram evidence stamp must be safe [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    return stamp


def _telegram_evidence_base(binding: CodexTelegramBinding) -> Path:
    return binding.account_home / ".local" / "state" / "pantheon" / "t2953-telegram-evidence"


def _telegram_evidence_root(binding: CodexTelegramBinding, stamp: str) -> Path:
    safe_stamp = _safe_operator_stamp(stamp)
    base = _telegram_evidence_base(binding)
    root = base / safe_stamp
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise BridgeError("Telegram evidence root escaped bound account home") from exc
    return root


def _telegram_runtime_observations_dir(root: Path) -> Path:
    return root / "runtime-observations"


def _telegram_operator_receipts_dir(root: Path) -> Path:
    return root / "operator-receipts"


def _telegram_runtime_observation_path(root: Path, step_id: str) -> Path:
    return _telegram_runtime_observations_dir(root) / f"{step_id}.json"


def _telegram_operator_receipt_path(root: Path, step_id: str) -> Path:
    return _telegram_operator_receipts_dir(root) / f"{step_id}.json"


def _telegram_evidence_expected_uid(binding: CodexTelegramBinding) -> int:
    try:
        return pwd.getpwnam(binding.account).pw_uid
    except KeyError:
        return os.geteuid()


def _require_secure_dir(path: Path, *, expected_uid: int, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeError(f"{label} missing or unreadable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BridgeError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise BridgeError(f"{label} must be a directory")
    if info.st_uid != expected_uid:
        raise BridgeError(f"{label} owner mismatch")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise BridgeError(f"{label} mode must be 0700")
    return info


def _require_secure_file(path: Path, *, root: Path, expected_uid: int, label: str) -> os.stat_result:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BridgeError(f"{label} escaped Telegram evidence root") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeError(f"{label} missing or unreadable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BridgeError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise BridgeError(f"{label} must be a regular file")
    if info.st_uid != expected_uid:
        raise BridgeError(f"{label} owner mismatch")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BridgeError(f"{label} mode must be 0600")
    return info


def _write_json_file_exclusive(path: Path, payload: Mapping[str, Any]) -> bytes:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
    if len(raw) > TELEGRAM_UNATTENDED_EVIDENCE_LIMIT_BYTES:
        raise BridgeError("unattended Telegram evidence file exceeds bounded size")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BridgeError(f"{path.name} already exists in Telegram evidence root") from exc
    except OSError as exc:
        raise BridgeError(f"{path.name} cannot be created: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
    return raw


def _make_secure_dir_exclusive(path: Path, *, expected_uid: int, label: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise BridgeError(f"{label} already exists") from exc
    except OSError as exc:
        raise BridgeError(f"{label} cannot be created: {exc}") from exc
    _require_secure_dir(path, expected_uid=expected_uid, label=label)


def _read_secure_json_file(path: Path, *, root: Path, expected_uid: int, label: str) -> tuple[Mapping[str, Any], bytes]:
    _require_secure_file(path, root=root, expected_uid=expected_uid, label=label)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BridgeError(f"{label} unreadable: {exc}") from exc
    if len(raw) > TELEGRAM_UNATTENDED_EVIDENCE_LIMIT_BYTES:
        raise BridgeError(f"{label} exceeds bounded size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise BridgeError(f"{label} must be a JSON object")
    return payload, raw


def _telegram_evidence_step_spec(
    step_id: str,
    *,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
) -> None:
    spec = TELEGRAM_UNATTENDED_CANARIES.get(step_id)
    if spec is None:
        raise BridgeError("unattended Telegram evidence step_id is not declared")
    if spec.get("tool") != tool or spec.get("canary") != canary_kind:
        raise BridgeError("unattended Telegram evidence step/tool/canary mismatch")


def validate_unattended_tool_invocation_evidence(
    evidence: Mapping[str, Any],
    *,
    step_id: str,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
    expected_correlation_id: str,
    prepare_nonce: str,
) -> Mapping[str, Any]:
    if evidence.get("schema") != TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA:
        raise BridgeError("unattended Telegram evidence schema mismatch")
    if evidence.get("prepare_nonce") != prepare_nonce:
        raise BridgeError("unattended Telegram evidence prepare nonce mismatch")
    if evidence.get("step_id") != step_id:
        raise BridgeError("unattended Telegram evidence step_id mismatch")
    if evidence.get("tool") != tool:
        raise BridgeError("unattended Telegram evidence tool mismatch")
    if evidence.get("canary") != canary_kind:
        raise BridgeError("unattended Telegram evidence canary mismatch")
    if evidence.get("result_status") != "success":
        raise BridgeError("unattended Telegram evidence result is not success")
    if evidence.get("correlation_id") != expected_correlation_id:
        raise BridgeError("unattended Telegram evidence correlation_id mismatch")
    if evidence.get("permission_prompt_observed") is not False:
        raise BridgeError("unattended Telegram evidence observed a permission prompt")
    if evidence.get("ui_approval_clicked") is not False:
        raise BridgeError("unattended Telegram evidence used UI approval")
    invocation = evidence.get("invocation")
    if not isinstance(invocation, Mapping) or invocation.get("tool") != tool:
        raise BridgeError("unattended Telegram evidence invocation missing exact tool")
    return evidence


def _operator_assertion_observation(
    *,
    prepare_nonce: str,
    step_id: str,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
    observed_correlation_id: str,
    result_status: str,
    permission_prompt_observed: bool,
    ui_approval_clicked: bool,
    invocation_tool: str,
) -> Mapping[str, Any]:
    return {
        "schema": TELEGRAM_UNATTENDED_OBSERVATION_SCHEMA,
        "prepare_nonce": prepare_nonce,
        "step_id": step_id,
        "tool": tool,
        "canary": canary_kind,
        "result_status": result_status,
        "correlation_id": observed_correlation_id,
        "permission_prompt_observed": permission_prompt_observed,
        "ui_approval_clicked": ui_approval_clicked,
        "invocation": {"tool": invocation_tool},
    }


def telegram_unattended_evidence_prepare(
    binding: CodexTelegramBinding,
    *,
    stamp: str,
) -> Mapping[str, Any]:
    root = _telegram_evidence_root(binding, stamp)
    base = _telegram_evidence_base(binding)
    expected_uid = _telegram_evidence_expected_uid(binding)
    if not base.exists():
        base.mkdir(parents=True, mode=0o700)
    _require_secure_dir(base, expected_uid=expected_uid, label="Telegram evidence base")
    try:
        os.mkdir(root, 0o700)
    except FileExistsError as exc:
        raise BridgeError("Telegram evidence stamp root already exists") from exc
    except OSError as exc:
        raise BridgeError(f"Telegram evidence stamp root cannot be created: {exc}") from exc
    _require_secure_dir(root, expected_uid=expected_uid, label="Telegram evidence stamp root")
    runtime_dir = _telegram_runtime_observations_dir(root)
    operator_dir = _telegram_operator_receipts_dir(root)
    _make_secure_dir_exclusive(runtime_dir, expected_uid=expected_uid, label="Telegram runtime observations directory")
    _make_secure_dir_exclusive(operator_dir, expected_uid=expected_uid, label="Telegram operator receipts directory")
    nonce = secrets.token_urlsafe(18)
    steps = {
        step_id: {
            **dict(spec),
            "correlation_id": telegram_unattended_correlation_id(stamp, step_id),
            "runtime_observation_path": str(_telegram_runtime_observation_path(root, step_id)),
            "operator_receipt_path": str(_telegram_operator_receipt_path(root, step_id)),
        }
        for step_id, spec in TELEGRAM_UNATTENDED_CANARIES.items()
    }
    payload = {
        "schema": TELEGRAM_UNATTENDED_PREPARE_SCHEMA,
        "ok": True,
        "persona": binding.persona,
        "account": binding.account,
        "stamp": stamp,
        "nonce": nonce,
        "runtime_observations_dir": str(runtime_dir),
        "operator_receipts_dir": str(operator_dir),
        "steps": steps,
    }
    raw = _write_json_file_exclusive(root / "prepare.json", payload)
    _require_secure_file(root / "prepare.json", root=root, expected_uid=expected_uid, label="Telegram evidence prepare")
    return {
        "schema": TELEGRAM_UNATTENDED_PREPARE_SCHEMA,
        "ok": True,
        "persona": binding.persona,
        "account": binding.account,
        "stamp": stamp,
        "root": str(root),
        "nonce": nonce,
        "prepare_path": str(root / "prepare.json"),
        "prepare_sha256": _sha256_bytes(raw),
        "runtime_observations_dir": str(runtime_dir),
        "operator_receipts_dir": str(operator_dir),
        "steps": steps,
    }


def telegram_unattended_evidence_record(
    binding: CodexTelegramBinding,
    *,
    stamp: str,
    step_id: str,
    tool: Literal["reply", "download_attachment"],
    canary_kind: str,
    observed_correlation_id: str,
    result_status: str,
    permission_prompt_observed: bool,
    ui_approval_clicked: bool,
    invocation_tool: str,
) -> Mapping[str, Any]:
    _telegram_evidence_step_spec(step_id, tool=tool, canary_kind=canary_kind)
    root = _telegram_evidence_root(binding, stamp)
    expected_uid = _telegram_evidence_expected_uid(binding)
    _require_secure_dir(_telegram_evidence_base(binding), expected_uid=expected_uid, label="Telegram evidence base")
    _require_secure_dir(root, expected_uid=expected_uid, label="Telegram evidence stamp root")
    runtime_dir = _telegram_runtime_observations_dir(root)
    operator_dir = _telegram_operator_receipts_dir(root)
    _require_secure_dir(runtime_dir, expected_uid=expected_uid, label="Telegram runtime observations directory")
    _require_secure_dir(operator_dir, expected_uid=expected_uid, label="Telegram operator receipts directory")
    prepare, _prepare_raw = _read_secure_json_file(
        root / "prepare.json",
        root=root,
        expected_uid=expected_uid,
        label="Telegram evidence prepare",
    )
    if prepare.get("schema") != TELEGRAM_UNATTENDED_PREPARE_SCHEMA:
        raise BridgeError("Telegram evidence prepare schema mismatch")
    if prepare.get("stamp") != stamp or prepare.get("persona") != binding.persona:
        raise BridgeError("Telegram evidence prepare binding mismatch")
    nonce = prepare.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise BridgeError("Telegram evidence prepare nonce missing")
    expected_correlation_id = telegram_unattended_correlation_id(stamp, step_id)
    payload = _operator_assertion_observation(
        prepare_nonce=nonce,
        step_id=step_id,
        tool=tool,
        canary_kind=canary_kind,
        observed_correlation_id=observed_correlation_id,
        result_status=result_status,
        permission_prompt_observed=permission_prompt_observed,
        ui_approval_clicked=ui_approval_clicked,
        invocation_tool=invocation_tool,
    )
    validate_unattended_tool_invocation_evidence(
        payload,
        step_id=step_id,
        tool=tool,
        canary_kind=canary_kind,
        expected_correlation_id=expected_correlation_id,
        prepare_nonce=nonce,
    )
    runtime_path = _telegram_runtime_observation_path(root, step_id)
    legacy_path = root / f"{step_id}.json"
    if legacy_path.exists() or legacy_path.is_symlink():
        if not runtime_path.exists() and not runtime_path.is_symlink():
            raise BridgeError("legacy Telegram evidence path is non-authoritative and runtime observation is missing")
    runtime_observation, runtime_raw = _read_secure_json_file(
        runtime_path,
        root=root,
        expected_uid=expected_uid,
        label="Telegram evidence observation",
    )
    validate_unattended_tool_invocation_evidence(
        runtime_observation,
        step_id=step_id,
        tool=tool,
        canary_kind=canary_kind,
        expected_correlation_id=expected_correlation_id,
        prepare_nonce=nonce,
    )
    if dict(runtime_observation) != dict(payload):
        raise BridgeError("unattended Telegram evidence target observation and operator assertions mismatch")
    receipt_path = _telegram_operator_receipt_path(root, step_id)
    receipt_payload = {
        "schema": TELEGRAM_UNATTENDED_RECEIPT_SCHEMA,
        "ok": True,
        "persona": binding.persona,
        "account": binding.account,
        "stamp": stamp,
        "step_id": step_id,
        "tool": tool,
        "canary": canary_kind,
        "correlation_id": expected_correlation_id,
        "runtime_observation_path": str(runtime_path),
        "runtime_observation_sha256": _sha256_bytes(runtime_raw),
        "operator_receipt_path": str(receipt_path),
    }
    written_raw = _write_json_file_exclusive(receipt_path, receipt_payload)
    reread, reread_raw = _read_secure_json_file(
        receipt_path,
        root=root,
        expected_uid=expected_uid,
        label="Telegram operator receipt",
    )
    if dict(reread) != receipt_payload:
        raise BridgeError("Telegram operator receipt reread mismatch")
    return {
        **receipt_payload,
        "ok": True,
        "operator_receipt_sha256": _sha256_bytes(reread_raw),
        "written_sha256": _sha256_bytes(written_raw),
        "evidence_path": str(runtime_path),
        "evidence_sha256": _sha256_bytes(runtime_raw),
    }


def _codex_instruction_discovery_command(repo: Path) -> str:
    return shlex.join(
        (
            "/usr/bin/timeout",
            CODEX_INSTRUCTION_DISCOVERY_TIMEOUT,
            "/usr/bin/codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            str(repo),
            "--color",
            "never",
            CODEX_INSTRUCTION_DISCOVERY_PROMPT,
        )
    )


def _instruction_proof_command(
    bridge_tool: Path,
    *,
    persona: str,
    manifest_path: Path,
    root: Path,
    proof: Literal["source", "target"],
) -> str:
    subcommand = "instruction-source-proof" if proof == "source" else "instruction-target-proof"
    return shlex.join(
        (
            "python3",
            str(bridge_tool),
            subcommand,
            "--persona",
            persona,
            "--manifest",
            str(manifest_path),
            "--root",
            str(root),
            "--json",
        )
    )


def _legacy_command_sheet(
    persona: str,
    *,
    expected_pantheon_revision: str,
    manifest_path: Path | None = None,
    root: Path = Path("/"),
) -> Mapping[str, Any]:
    expected = validate_expected_revision(expected_pantheon_revision)
    manifest = _load_manifest(manifest_path or _default_manifest_path())
    binding = manifest.codex_telegram_binding(persona, root=root)
    account = binding.account
    home = binding.account_home
    repo = home / "pantheon-config"
    state_root = home / ".local" / "state" / "pantheon" / "personas"
    bridge_tool = home / "pantheon-config" / "tools" / "codex_telegram_bridge.py"
    swap_tool = home / "pantheon-config" / "tools" / "proactive_swap_init.py"
    source_archive, target_archive = _staged_archive_refs(persona)
    to_codex = f"t2950-{persona}-to-codex"
    to_claude = f"t2950-{persona}-to-claude"
    codex_operator = _operator_command(swap_tool, state_root, persona, "codex/interactive-chatgpt", to_codex)
    claude_operator = _operator_command(swap_tool, state_root, persona, "claude/interactive-subscription", to_claude)
    source_gate = _target_gate(account, home, bridge_tool, repo, expected)
    install = _staged_install_command(source_gate, account, source_archive, target_archive)
    codex_consume = shlex.join(("/usr/bin/env", f"PANTHEON_PERSONA_STATE_DIR={state_root}", "systemctl", "--user", "start", f"pantheon-persona-consume@{persona}.service"))
    steps = [
        _command_step("01-credential-check", "preflight", _sudo_bash(account, _target_account_shell_command(account, home, f"set -euo pipefail; {shlex.join(('python3', str(bridge_tool), 'credential-check', '--persona', persona, '--json'))}"))),
        _command_step("02-telegram-preflight", "preflight", _sudo_bash(account, _target_account_shell_command(account, home, f"set -euo pipefail; {shlex.join(('python3', str(bridge_tool), 'telegram-preflight', '--persona', persona, '--json'))}"))),
        _operator_gate("03-precutover-mailbox-delivery", "preflight", {"kind": "mailbox-durable-delivery", "runtime": "precutover", "consumption_verified": False}),
        _command_step("04-source-archive", "stage", _staging_dirs_prefix(account) + shlex.join(("python3", "/home/codex/pantheon-config/tools/codex_telegram_bridge.py", "source-check", "--persona", persona, "--source-repo", str(source_repo_root(binding.source)), "--archive")) + f" {source_archive}"),
        _command_step("05-install-archive", "stage", install),
        _command_step("06-bridge-stage", "stage", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, shlex.join(("python3", str(bridge_tool), "bridge-stage", "--persona", persona, "--archive")) + ' "$1" --json', trailing_arg=target_archive) + _staging_cleanup_suffix()),
        _command_step("07-swap-to-codex", "codex-cutover", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, codex_operator)),
        _command_step("08-start-codex-consume", "codex-cutover", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, codex_consume)),
        _command_step("09-gate-codex-completed", "codex-cutover", _operator_gate_compound(account, home, bridge_tool, swap_tool, repo, state_root, persona=persona, destination="codex/interactive-chatgpt", correlation_id=to_codex, expected_head=expected)),
        _operator_gate("10-telegram-text-canary", "codex-acceptance", {"kind": "telegram-text-inbound-reply", "runtime": "codex"}),
        _operator_gate("11-telegram-document-canary", "codex-acceptance", {"kind": "telegram-document-download", "runtime": "codex"}),
        _operator_gate("12-telegram-photo-canary", "codex-acceptance", {"kind": "telegram-photo-download", "runtime": "codex"}),
        _operator_gate("13-mailbox-codex-consume-reply-wake", "codex-acceptance", {"kind": "mailbox-consume-reply-wake", "runtime": "codex"}),
        _operator_gate("14-restart-offset-preservation", "codex-acceptance", {"kind": "poller-restart-offset-next-update-once", "runtime": "codex"}),
        _command_step("15-swap-to-claude", "restore", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, claude_operator)),
        _command_step("16-start-claude-consume", "restore", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, codex_consume)),
        _command_step("17-gate-claude-completed", "restore", _operator_gate_compound(account, home, bridge_tool, swap_tool, repo, state_root, persona=persona, destination="claude/interactive-subscription", correlation_id=to_claude, expected_head=expected)),
        _operator_gate("18-restore-receiver-state", "restore-acceptance", {"kind": "receiver-state", "codex": "absent", "claude": "present"}),
        _operator_gate("19-restore-mailbox-delivery", "restore-acceptance", {"kind": "mailbox-durable-delivery", "runtime": "claude-or-external-while-capped"}),
    ]
    commands = [step["command"] for step in steps if step["type"] == "command"]
    return {
        "schema": COMMAND_SHEET_SCHEMA,
        "persona": persona,
        "account": account,
        "expected_pantheon_revision": expected,
        "source_revision": CODEX_TELEGRAM_SOURCE_REVISION,
        "source_remediation": source_remediation(account, repo, expected_head=expected),
        "amendment_i_remediation": amendment_i_remediation(persona, binding, expected_pantheon_revision=expected),
        "steps": steps,
        "commands": commands,
        "commands_authoritative": False,
        "completion_gate": {"retry_safe_same_command": True, "blind_loop": False},
        "secret_values": "omitted",
    }


def command_sheet(
    persona: str,
    *,
    expected_pantheon_revision: str,
    manifest_path: Path | None = None,
    root: Path = Path("/"),
    telegram_evidence_stamp: str = "<operator-stamp>",
) -> Mapping[str, Any]:
    if persona in {"specialist", "recovery"}:
        return _legacy_command_sheet(
            persona,
            expected_pantheon_revision=expected_pantheon_revision,
            manifest_path=manifest_path,
            root=root,
        )
    expected = validate_expected_revision(expected_pantheon_revision)
    effective_manifest_path = manifest_path or _default_manifest_path()
    manifest = _load_manifest(effective_manifest_path)
    binding = manifest.codex_telegram_binding(persona, root=root)
    from tools.persona_installer import codex_standing_instructions_reference

    instructions = codex_standing_instructions_reference(manifest, persona, root=root)
    if instructions is None:
        raise BridgeError(f"codex standing instructions missing for {persona!r}")
    account = binding.account
    source_account = binding.source.owner_account or "codex"
    source_root = source_repo_root(binding.source)
    home = binding.account_home
    repo = home / "pantheon-config"
    state_root = home / ".local" / "state" / "pantheon" / "personas"
    bridge_tool = home / "pantheon-config" / "tools" / "codex_telegram_bridge.py"
    swap_tool = home / "pantheon-config" / "tools" / "proactive_swap_init.py"
    apply_tool = repo / "tools" / "t2611_recovery_persona_apply.py"
    cutover_tool = repo / "tools" / "persona_cutover.sh"
    source_archive, target_archive = _staged_archive_refs(persona)  # T-5068: private mktemp dirs
    to_claude = f"t2953-{persona}-to-claude"
    claude_operator = _operator_command(swap_tool, state_root, persona, "claude/interactive-subscription", to_claude)
    source_gate = _target_gate(account, home, bridge_tool, repo, expected)
    install = _staged_install_command(source_gate, account, source_archive, target_archive)
    def receiver(kind: str, expected_state: str, *, topology_phase: TopologyPhase | None = None) -> str:
        argv = [
            "python3",
            str(bridge_tool),
            "receiver-state",
            "--persona",
            persona,
            "--kind",
            kind,
            "--expected",
            expected_state,
        ]
        if topology_phase is not None:
            argv.extend(("--topology-phase", topology_phase))
        argv.append("--json")
        return shlex.join(argv)
    instruction_source_check = _instruction_proof_command(
        bridge_tool,
        persona=persona,
        manifest_path=repo / "instance.yaml",
        root=root,
        proof="source",
    )
    stage1_dry = shlex.join(("bash", str(cutover_tool), "--persona", persona, "--stamp", "<operator-stamp>", "--manifest", str(repo / "instance.yaml")))
    stage_disabled = shlex.join(("python3", str(apply_tool), "stage-disabled", "--manifest", str(repo / "instance.yaml"), "--persona", persona, "--harness", "claude", "--repo-root", str(repo), "--stamp", "<operator-stamp>", "--codex-telegram-archive-dir")) + ' "$1"'
    stage1_execute = shlex.join(("bash", str(cutover_tool), "--persona", persona, "--stamp", "<operator-stamp>", "--manifest", str(repo / "instance.yaml"), "--execute"))
    instruction_discovery = _codex_instruction_discovery_command(repo)
    initial_viability = _repo_module_command(
        repo,
        "tools.runtime_controller",
        "initial-viability",
        "--persona",
        persona,
        "--state-root",
        str(state_root),
        "--expect-selected",
        "codex/interactive-chatgpt",
        "--expect-failed",
        "claude/interactive-subscription",
        "--expect-failure-class",
        "subscription-cap",
        "--expect-failure-class",
        "operational-launch",
        "--expect-failure-class",
        "operational-readiness",
        "--require-source-provenance",
        "--require-receiver-receipt",
    )
    ready = _repo_module_command(
        repo,
        "tools.bringup_acceptance.harness_codex_ready",
        "--work-root",
        home / ".local" / "state" / "pantheon" / "t2953-codex-ready",
        "--account",
        account,
        "--persona",
        persona,
        "--codex-executable",
        "/usr/bin/codex",
    )
    rc_ready = _repo_module_command(
        repo,
        "tools.bringup_acceptance.harness_codex_rc_ready",
        "--work-root",
        home / ".local" / "state" / "pantheon" / "t2953-codex-rc-ready",
        "--account",
        account,
        "--persona",
        persona,
        "--codex-executable",
        "/usr/bin/codex",
    )
    mailbox_correlation = f"t2953-{persona}-mailbox-<operator-stamp>"
    mailbox_prepare = shlex.join(
        (
            "python3",
            str(bridge_tool),
            "mailbox-roundtrip-prepare",
            "--persona",
            persona,
            "--reply-persona",
            "codex",
            "--correlation-id",
            mailbox_correlation,
            "--manifest",
            str(repo / "instance.yaml"),
            "--root",
            str(root),
            "--json",
        )
    )
    mailbox_verify = shlex.join(
        (
            "python3",
            str(bridge_tool),
            "mailbox-roundtrip-verify",
            "--persona",
            persona,
            "--reply-persona",
            "codex",
            "--correlation-id",
            mailbox_correlation,
            "--manifest",
            str(repo / "instance.yaml"),
            "--root",
            str(root),
            "--json",
        )
    )
    steps = [
        _command_step("01-credential-check", "preflight", _sudo_bash(account, _target_account_shell_command(account, home, f"set -euo pipefail; {shlex.join(('python3', str(bridge_tool), 'credential-check', '--persona', persona, '--json'))}"))),
        _command_step("02-telegram-preflight", "preflight", _sudo_bash(account, _target_account_shell_command(account, home, f"set -euo pipefail; {shlex.join(('python3', str(bridge_tool), 'telegram-preflight', '--persona', persona, '--json'))}"))),
        _operator_gate("03-precutover-mailbox-delivery", "preflight", {"kind": "mailbox-durable-delivery", "runtime": "precutover", "consumption_verified": False}),
        _command_step("04-instruction-source-check", "stage", _sudo_bash(account, _target_account_shell_command(account, home, f"set -euo pipefail; {instruction_source_check}"))),
        _command_step(
            "05-source-archive",
            "stage",
            _staging_dirs_prefix(account, source_account=source_account)
            + _sudo_bash(
                source_account,
                "set -euo pipefail; "
                + shlex.join(('python3', str(Path('/home') / source_account / 'pantheon-config' / 'tools' / 'codex_telegram_bridge.py'), 'source-check', '--persona', persona, '--source-repo', str(source_root), '--archive'))
                + ' "$1" --json',
                trailing_arg=source_archive,
            ),
        ),
        _command_step("06-install-archive", "stage", install),
        _command_step("07-stage1-cutover-dry-run", "stage1-controller-claude", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, stage1_dry)),
        _command_step("08-stage-disabled-instructions-and-bridge", "stage1-controller-claude", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, stage_disabled, trailing_arg=f'"${_STAGE_DST_VAR}"')),
        _command_step("09-postinstall-instruction-target", "stage1-postinstall-proof", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, _instruction_proof_command(bridge_tool, persona=persona, manifest_path=repo / "instance.yaml", root=root, proof="target"))),
        _command_step("10-codex-instruction-discovery", "stage1-postinstall-proof", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, instruction_discovery)),
        _operator_gate(
            "11-gate-codex-instruction-discovery",
            "stage1-postinstall-proof",
            {
                "kind": "codex-instruction-discovery-human-confirmation",
                "runtime": "pre-codex-swap",
                "confirmation_required": True,
                "source": "10-codex-instruction-discovery stdout",
                "expected_facts": [
                    "Orchestrator/HO identity and authority",
                    "mandatory canonical /home/claude/CLAUDE.md read-before-action",
                    "Telegram reply mapping mcp__telegram__reply",
                    "canonical Orchestrator rules remain source-of-truth",
                ],
            },
        ),
        _command_step("12-prove-codex-absent", "stage1-postinstall-proof", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("codex", "absent"))),
        _command_step("13-gate-claude-present", "stage1-postinstall-proof", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("claude", "present", topology_phase="predecessor"))),
        _command_step("14-stage1-cutover-execute", "codex-cutover", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, stage1_execute) + _staging_cleanup_suffix(source_account=source_account)),
        _command_step("15-gate-initial-viability", "codex-cutover", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, initial_viability)),
        _command_step("16-prove-claude-absent", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("claude", "absent", topology_phase="managed"))),
        _command_step("17-prove-codex-present", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, receiver("codex", "present"))),
        _command_step("18-codex-ready-real-cli", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, ready)),
        _command_step("19-codex-rc-ready-real-cli", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, rc_ready)),
        _command_step(
            "19a-telegram-unattended-evidence-prepare",
            "codex-acceptance",
            _gated_target_command(
                account,
                binding.account_home,
                bridge_tool,
                repo,
                expected,
                _unattended_telegram_prepare_command(
                    bridge_tool,
                    persona=persona,
                    stamp=telegram_evidence_stamp,
                    manifest_path=repo / "instance.yaml",
                    root=root,
                ),
            ),
        ),
        _telegram_canary_operator_gate(
            step_id="20-telegram-text-canary",
            phase="codex-acceptance",
            tool="reply",
            canary_kind="telegram-text-inbound-reply",
            stamp=telegram_evidence_stamp,
        ),
        _command_step(
            "20-telegram-text-canary",
            "codex-acceptance",
            _gated_target_command(
                account,
                binding.account_home,
                bridge_tool,
                repo,
                expected,
                _unattended_telegram_evidence_command(
                    bridge_tool,
                    persona=persona,
                    stamp=telegram_evidence_stamp,
                    manifest_path=repo / "instance.yaml",
                    root=root,
                    step_id="20-telegram-text-canary",
                    tool="reply",
                    canary_kind="telegram-text-inbound-reply",
                ),
            ),
        ),
        _telegram_canary_operator_gate(
            step_id="21-telegram-document-canary",
            phase="codex-acceptance",
            tool="download_attachment",
            canary_kind="telegram-document-download",
            stamp=telegram_evidence_stamp,
        ),
        _command_step(
            "21-telegram-document-canary",
            "codex-acceptance",
            _gated_target_command(
                account,
                binding.account_home,
                bridge_tool,
                repo,
                expected,
                _unattended_telegram_evidence_command(
                    bridge_tool,
                    persona=persona,
                    stamp=telegram_evidence_stamp,
                    manifest_path=repo / "instance.yaml",
                    root=root,
                    step_id="21-telegram-document-canary",
                    tool="download_attachment",
                    canary_kind="telegram-document-download",
                ),
            ),
        ),
        _telegram_canary_operator_gate(
            step_id="22-telegram-photo-canary",
            phase="codex-acceptance",
            tool="download_attachment",
            canary_kind="telegram-photo-download",
            stamp=telegram_evidence_stamp,
        ),
        _command_step(
            "22-telegram-photo-canary",
            "codex-acceptance",
            _gated_target_command(
                account,
                binding.account_home,
                bridge_tool,
                repo,
                expected,
                _unattended_telegram_evidence_command(
                    bridge_tool,
                    persona=persona,
                    stamp=telegram_evidence_stamp,
                    manifest_path=repo / "instance.yaml",
                    root=root,
                    step_id="22-telegram-photo-canary",
                    tool="download_attachment",
                    canary_kind="telegram-photo-download",
                ),
            ),
        ),
        _command_step("23a-mailbox-codex-roundtrip-prepare", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, mailbox_prepare)),
        _operator_gate(
            "23b-mailbox-codex-consume-reply-wake",
            "codex-acceptance",
            {
                "kind": "mailbox-consume-reply-wake",
                "runtime": "codex",
                "source": "23a-mailbox-codex-roundtrip-prepare stdout",
                "human_tui_boundary": True,
                "expected": [
                    "controller wake/submission for exact message_id and correlation_id",
                    "TUI moves exact source message from new to cur",
                    "ack record exists for exact source message id",
                    "reply lands in manifest-declared codex inbox/new",
                ],
            },
        ),
        _command_step("23c-mailbox-codex-roundtrip-verify", "codex-acceptance", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, mailbox_verify)),
        _operator_gate("24-restart-offset-preservation", "codex-acceptance", {"kind": "poller-restart-offset-next-update-once", "runtime": "codex"}),
        _command_step("25-swap-to-claude", "restore", _gated_target_command(account, binding.account_home, bridge_tool, repo, expected, claude_operator)),
        _command_step("26-gate-claude-completed", "restore", _operator_gate_compound(account, home, bridge_tool, swap_tool, repo, state_root, persona=persona, destination="claude/interactive-subscription", correlation_id=to_claude, expected_head=expected)),
        _operator_gate("27-restore-receiver-state", "restore-acceptance", {"kind": "receiver-state", "codex": "absent", "claude": "present", "claude_topology_phase": "predecessor"}),
        _operator_gate("28-restore-mailbox-delivery", "restore-acceptance", {"kind": "mailbox-durable-delivery", "runtime": "claude-or-external-while-capped"}),
    ]
    commands = [step["command"] for step in steps if step["type"] == "command"]
    return {
        "schema": COMMAND_SHEET_SCHEMA,
        "persona": persona,
        "account": account,
        "expected_pantheon_revision": expected,
        "source_revision": CODEX_TELEGRAM_SOURCE_REVISION,
        "source_remediation": source_remediation(account, repo, expected_head=expected),
        "amendment_i_remediation": amendment_i_remediation(persona, binding, expected_pantheon_revision=expected),
        "steps": steps,
        "commands": commands,
        "commands_authoritative": False,
        "completion_gate": {"retry_safe_same_command": True, "blind_loop": False},
        "secret_values": "omitted",
    }


def _parse_evidence_bool(value: str) -> bool:
    if value == "false":
        return False
    if value == "true":
        return True
    raise argparse.ArgumentTypeError("value must be true or false")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exec_parser = sub.add_parser("exec")
    exec_parser.add_argument("--mode", choices=("poller", "server"), required=True)
    exec_parser.add_argument("--manifest", type=Path, default=_default_manifest_path())
    exec_parser.add_argument("--persona", default=None)
    credential = sub.add_parser("credential-check")
    credential.add_argument("--manifest", type=Path, default=_default_manifest_path())
    credential.add_argument("--persona", required=True)
    credential.add_argument("--json", action="store_true")
    telegram_check = sub.add_parser("telegram-preflight")
    telegram_check.add_argument("--manifest", type=Path, default=_default_manifest_path())
    telegram_check.add_argument("--persona", required=True)
    telegram_check.add_argument("--timeout-seconds", type=float, default=TELEGRAM_PREFLIGHT_TIMEOUT_SECONDS)
    telegram_check.add_argument("--json", action="store_true")
    source_currency = sub.add_parser("source-currency")
    source_currency.add_argument("--repo", type=Path, required=True)
    source_currency.add_argument("--expected-head", required=True)
    source_currency.add_argument("--json", action="store_true")
    source_check = sub.add_parser("source-check")
    source_check.add_argument("--manifest", type=Path, default=_default_manifest_path())
    source_check.add_argument("--persona", required=True)
    source_check.add_argument("--source-repo", type=Path)
    source_check.add_argument("--archive", type=Path)
    source_check.add_argument("--stage", action="store_true")
    source_check.add_argument("--json", action="store_true")
    source_proof = sub.add_parser("instruction-source-proof")
    source_proof.add_argument("--manifest", type=Path, required=True)
    source_proof.add_argument("--root", type=Path, required=True)
    source_proof.add_argument("--persona", required=True)
    source_proof.add_argument("--json", action="store_true")
    target_proof = sub.add_parser("instruction-target-proof")
    target_proof.add_argument("--manifest", type=Path, required=True)
    target_proof.add_argument("--root", type=Path, required=True)
    target_proof.add_argument("--persona", required=True)
    target_proof.add_argument("--json", action="store_true")
    bridge_stage_parser = sub.add_parser("bridge-stage")
    bridge_stage_parser.add_argument("--manifest", type=Path, default=_default_manifest_path())
    bridge_stage_parser.add_argument("--persona", required=True)
    bridge_stage_parser.add_argument("--archive", type=Path, required=True)
    bridge_stage_parser.add_argument("--json", action="store_true")
    handoff = sub.add_parser("handoff")
    handoff.add_argument("--persona", required=True)
    handoff.add_argument("--direction", required=True)
    handoff.add_argument("--phase", required=True)
    handoff.add_argument("--reason", required=True)
    handoff.add_argument("--timeout-seconds", type=int, default=90)
    # T-4184: "unproven" = the prior runtime may be dead or capped (controller
    # codex launch); see codex_telegram_actions.PriorRuntime.
    handoff.add_argument("--prior-runtime", choices=PRIOR_RUNTIME_CHOICES, default="live")
    handoff.add_argument("--manifest", type=Path, default=_default_manifest_path())
    handoff.add_argument("--json", action="store_true")
    receiver_state = sub.add_parser("receiver-state")
    receiver_state.add_argument("--persona", required=True)
    receiver_state.add_argument("--kind", choices=("claude", "codex"), required=True)
    receiver_state.add_argument("--expected", choices=("present", "absent"), required=True)
    receiver_state.add_argument("--topology-phase", choices=("managed", "predecessor"), default=None)
    receiver_state.add_argument("--manifest", type=Path, default=_default_manifest_path())
    receiver_state.add_argument("--no-wait", action="store_true", help="single-shot probe; never poll (T-6016)")
    receiver_state.add_argument("--json", action="store_true")
    snapshot_state = sub.add_parser("topology-snapshot-state")
    snapshot_state.add_argument("--persona", required=True)
    snapshot_state.add_argument("--manifest", type=Path, default=_default_manifest_path())
    snapshot_state.add_argument("--json", action="store_true")
    telegram_evidence = sub.add_parser("telegram-unattended-evidence")
    telegram_evidence.add_argument("--action", choices=("prepare", "record"), required=True)
    telegram_evidence.add_argument("--persona", required=True)
    telegram_evidence.add_argument("--stamp", required=True)
    telegram_evidence.add_argument("--step-id", choices=tuple(TELEGRAM_UNATTENDED_CANARIES), default=None)
    telegram_evidence.add_argument("--tool", choices=("reply", "download_attachment"), default=None)
    telegram_evidence.add_argument("--canary", default=None)
    telegram_evidence.add_argument("--observed-correlation-id", default=None)
    telegram_evidence.add_argument("--result-status", choices=("success", "failure"), default=None)
    telegram_evidence.add_argument("--permission-prompt-observed", type=_parse_evidence_bool, default=None)
    telegram_evidence.add_argument("--ui-approval-clicked", type=_parse_evidence_bool, default=None)
    telegram_evidence.add_argument("--invocation-tool", choices=("reply", "react", "edit_message", "download_attachment", "delete_message"), default=None)
    telegram_evidence.add_argument("--manifest", type=Path, default=_default_manifest_path())
    telegram_evidence.add_argument("--root", type=Path, default=Path("/"))
    telegram_evidence.add_argument("--json", action="store_true")
    mailbox_prepare = sub.add_parser("mailbox-roundtrip-prepare")
    mailbox_prepare.add_argument("--persona", required=True)
    mailbox_prepare.add_argument("--reply-persona", required=True)
    mailbox_prepare.add_argument("--correlation-id", required=True)
    mailbox_prepare.add_argument("--manifest", type=Path, default=_default_manifest_path())
    mailbox_prepare.add_argument("--root", type=Path, default=Path("/"))
    mailbox_prepare.add_argument("--json", action="store_true")
    mailbox_verify = sub.add_parser("mailbox-roundtrip-verify")
    mailbox_verify.add_argument("--persona", required=True)
    mailbox_verify.add_argument("--reply-persona", required=True)
    mailbox_verify.add_argument("--correlation-id", required=True)
    mailbox_verify.add_argument("--manifest", type=Path, default=_default_manifest_path())
    mailbox_verify.add_argument("--root", type=Path, default=Path("/"))
    mailbox_verify.add_argument("--json", action="store_true")
    sheet = sub.add_parser("command-sheet")
    sheet.add_argument("--persona", required=True)
    sheet.add_argument("--expected-pantheon-revision", required=True)
    args = parser.parse_args(argv)
    if args.command == "exec":
        binding = binding_for_current_account(args.manifest, args.persona)
        exec_mode(binding, args.mode)
        return 70
    if args.command == "credential-check":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            print(json.dumps(credential_check_receipt(binding), sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "telegram-preflight":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            receipt = telegram_preflight_receipt(binding, timeout_seconds=args.timeout_seconds)
            print(json.dumps(receipt, sort_keys=True))
            return 0 if receipt.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": _bounded_error(f"{type(exc).__name__}: {exc}")}, sort_keys=True))
            return 1
    if args.command == "source-currency":
        try:
            receipt = source_currency_receipt(args.repo, expected_head=args.expected_head)
            print(json.dumps(receipt, sort_keys=True))
            return 0 if receipt.get("ok") else 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": _bounded_error(f"{type(exc).__name__}: {exc}")}, sort_keys=True))
            return 1
    if args.command == "source-check":
        try:
            manifest = _load_manifest(args.manifest)
            source, _personas = parse_bridge_block(manifest.installer.get("codex_telegram_bridge"))
            binding = (
                binding_for_current_account(args.manifest, args.persona)
                if args.stage
                else None
            )
            receipt = source_check_receipt(
                source,
                source_repo=args.source_repo,
                archive_path=args.archive,
                stage_binding=binding,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "instruction-source-proof":
        try:
            receipt = _instruction_source_proof(
                args.persona,
                manifest_path=args.manifest,
                repo_root=args.manifest.parent,
                root=args.root,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "instruction-target-proof":
        try:
            receipt = _instruction_target_proof(args.persona, manifest_path=args.manifest, root=args.root)
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "bridge-stage":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            print(json.dumps(bridge_stage(args.archive, binding), sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "handoff":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            backend = LocalReceiverBackend(timeout_seconds=args.timeout_seconds)
            operations = []
            actions = handoff_actions(binding, args.direction, args.phase, prior_runtime=args.prior_runtime)
            try:
                for action in actions:
                    backend.execute(action, binding)
                    operations.append(action)
            except Exception:
                if args.phase == "pre" and ("snapshot-watchdog-units" in operations or "snapshot-claude-receiver" in operations):
                    try:
                        if "snapshot-claude-receiver" in operations:
                            backend.execute("restore-claude-receiver", binding)
                        if "snapshot-watchdog-units" in operations:
                            backend.execute("restore-watchdog-units", binding)
                        backend.execute("complete-receiver-topology-snapshot", binding)
                    except Exception as restore_exc:
                        raise BridgeError(f"receiver handoff failed and pre-restore failed: {restore_exc}") from restore_exc
                raise
            receipt = HandoffReceipt(
                schema="pantheon-codex-telegram-handoff/v1",
                persona=binding.persona,
                account=binding.account,
                direction=args.direction,
                phase=args.phase,
                ok=True,
                operations=tuple(operations),
            )
            print(json.dumps(dataclasses.asdict(receipt), sort_keys=True))
            return 0
        except Exception as exc:
            receipt = HandoffReceipt(
                schema="pantheon-codex-telegram-handoff/v1",
                persona=args.persona,
                account=pwd.getpwuid(os.geteuid()).pw_name,
                direction=args.direction,
                phase=args.phase,
                ok=False,
                operations=(),
                error=f"{type(exc).__name__}: {exc}",
            )
            print(json.dumps(redact_secrets(dataclasses.asdict(receipt), ()), sort_keys=True))
            return 1
    if args.command == "topology-snapshot-state":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            pending = LocalReceiverBackend().receiver_topology_snapshot_pending(binding)
            print(json.dumps({
                "schema": "pantheon-codex-telegram-topology-snapshot-state/v1",
                "persona": binding.persona,
                "account": binding.account,
                "ok": True,
                "pending": pending,
            }, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({
                "schema": "pantheon-codex-telegram-topology-snapshot-state/v1",
                "persona": args.persona,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, sort_keys=True))
            return 1
    if args.command == "receiver-state":
        try:
            binding = binding_for_current_account(args.manifest, args.persona)
            backend = LocalReceiverBackend()
            if args.kind == "codex" and args.topology_phase is not None:
                raise BridgeError("topology phase applies only to Claude receiver-state proof")
            topology_phase: TopologyPhase = args.topology_phase or "managed"
            if args.expected == "present":
                evidence = backend._require_present(binding, kind=args.kind, topology_phase=topology_phase, wait=not args.no_wait)
            else:
                evidence = backend._require_absent(binding, kind=args.kind, topology_phase=topology_phase, wait=not args.no_wait)
            receipt = ReceiverStateReceipt(
                schema="pantheon-codex-telegram-receiver-state/v1",
                persona=binding.persona,
                account=binding.account,
                kind=args.kind,
                expected=args.expected,
                state=args.expected,
                ok=True,
                evidence=evidence,
            )
            print(json.dumps(dataclasses.asdict(receipt), sort_keys=True))
            return 0
        except Exception as exc:
            receipt = ReceiverStateReceipt(
                schema="pantheon-codex-telegram-receiver-state/v1",
                persona=args.persona,
                account=pwd.getpwuid(os.geteuid()).pw_name,
                kind=args.kind,
                expected=args.expected,
                state="ambiguous",
                ok=False,
                evidence={},
                error=f"{type(exc).__name__}: {exc}",
            )
            print(json.dumps(redact_secrets(dataclasses.asdict(receipt), ()), sort_keys=True))
            return 1
    if args.command == "telegram-unattended-evidence":
        try:
            binding = binding_for_current_account(args.manifest, args.persona, root=args.root)
            if args.action == "prepare":
                receipt = telegram_unattended_evidence_prepare(binding, stamp=args.stamp)
            else:
                required = {
                    "step_id": args.step_id,
                    "tool": args.tool,
                    "canary": args.canary,
                    "observed_correlation_id": args.observed_correlation_id,
                    "result_status": args.result_status,
                    "permission_prompt_observed": args.permission_prompt_observed,
                    "ui_approval_clicked": args.ui_approval_clicked,
                    "invocation_tool": args.invocation_tool,
                }
                missing = [key for key, value in required.items() if value is None]
                if missing:
                    raise BridgeError("telegram-unattended-evidence record missing: " + ", ".join(sorted(missing)))
                receipt = telegram_unattended_evidence_record(
                    binding,
                    stamp=args.stamp,
                    step_id=args.step_id,
                    tool=args.tool,
                    canary_kind=args.canary,
                    observed_correlation_id=args.observed_correlation_id,
                    result_status=args.result_status,
                    permission_prompt_observed=args.permission_prompt_observed,
                    ui_approval_clicked=args.ui_approval_clicked,
                    invocation_tool=args.invocation_tool,
                )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
            return 1
    if args.command == "mailbox-roundtrip-prepare":
        try:
            receipt = mailbox_roundtrip_prepare(
                manifest_path=args.manifest,
                persona=args.persona,
                reply_persona=args.reply_persona,
                correlation_id=args.correlation_id,
                root=args.root,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": _bounded_error(f"{type(exc).__name__}: {exc}")}, sort_keys=True))
            return 1
    if args.command == "mailbox-roundtrip-verify":
        try:
            receipt = mailbox_roundtrip_verify(
                manifest_path=args.manifest,
                persona=args.persona,
                reply_persona=args.reply_persona,
                correlation_id=args.correlation_id,
                root=args.root,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": _bounded_error(f"{type(exc).__name__}: {exc}")}, sort_keys=True))
            return 1
    if args.command == "command-sheet":
        print(
            json.dumps(
                command_sheet(args.persona, expected_pantheon_revision=args.expected_pantheon_revision),
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
