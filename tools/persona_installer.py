#!/usr/bin/env python3
"""Manifest-driven, fail-closed Pantheon persona installer.

The installer is deliberately separate from the runtime harness adapter.  It
builds a typed, digest-addressed plan and applies it through a backend that
implements known operation kinds.  There is no generic shell operation.

Production activation is intentionally conservative: the local backend only
operates on the current Unix account and requires a reachable user-systemd
manager.  Cross-account provisioning remains an explicit operator action.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import grp
import hashlib
import json
import math
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import codex_telegram_bridge  # noqa: E402  (needs sys.path bootstrap)
from tools.codex_telegram_actions import receiver_handoff_actions  # noqa: E402
from tools.harness_permission_posture import (  # noqa: E402
    PermissionPostureError,
    requires_unattended_telegram,
    validate_harness_runtime_permission_postures,
)
from tools.mailbox_writable_roots import (  # noqa: E402
    MailboxWritableRootError,
    resolve_mailbox_writable_roots,
    verify_maildir_writable_root,
)
from tools.persona_brief import (  # noqa: E402  (needs sys.path bootstrap)
    PersonaBriefError,
    resolve_persona_brief_path,
    validate_persona_brief_ref,
)

PLAN_SCHEMA = "pantheon-persona-install-plan/v1"
RECEIPT_SCHEMA = "pantheon-persona-install-receipt/v1"
MANAGED_MARKER = "Managed by pantheon persona installer"
RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS = 60
MAX_UNIT_FIRE_TIMEOUT_SECONDS = 1799
UNIT_FIRE_TIMEOUT_FORMULA = (
    "candidates*(runtime_start+input_ready+provider_completion+"
    "post_start_count*post_start+post_start_assessment)+teardown_margin"
)
ACTIVATION_OPERATION_KINDS = frozenset(
    {
        "daemon-reload",
        "stale-unit-failure-reset",
        "direct-fire",
        "unit-fire",
        "mailbox-probe",
        "timer-enable",
        "timer-observe",
    }
)
LAYER_NAMES = {
    0: "host-substrate",
    1: "connective-substrate",
    2: "harness-runtime",
    3: "persona-overlay",
    4: "policy-governance",
}

PrivilegeRequirement = Literal[
    "unprivileged",
    "privileged-helper",
    "operator-required",
]
ExecutionScope = Literal["all", "staged-cutover"]
Workflow = Literal["all-in-one", "stage-prefix", "staged-activate"]
OperationKind = Literal[
    "identity-check",
    "source-check",
    "harness-check",
    "brief-check",
    "policy-check",
    "mkdir",
    "atomic-write",
    "user-unit-install",
    "manager-check",
    "daemon-reload",
    "stale-unit-failure-reset",
    "direct-fire",
    "unit-fire",
    "mailbox-probe",
    "mailbox-writable-root-check",
    "timer-enablement-absence-check",
    "timer-enable",
    "timer-observe",
    "codex-telegram-source-check",
    "codex-telegram-disabled-first",
    "codex-telegram-mcp-check",
    "codex-standing-instructions-source-check",
    "codex-standing-instructions-install",
    "codex-standing-instructions-target-check",
    "codex-standing-instructions-override-check",
    "claude-mcp-server-check",
    "claude-mcp-server-registration",
]


class InstallerError(RuntimeError):
    """Base class for a bounded installer failure."""


class ManifestError(InstallerError):
    """The trusted manifest does not satisfy the installer schema."""


class PlanRefused(InstallerError):
    """Apply or rollback was refused before an unsafe mutation."""


class OperationFailed(InstallerError):
    """A typed operation failed during execution."""

    def __init__(self, message: str, *, evidence: Mapping[str, str] | None = None):
        super().__init__(message)
        self.evidence = dict(evidence or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _completed_process_bounded_evidence(
    prefix: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, str]:
    stdout = (result.stdout or "").encode("utf-8")
    stderr = (result.stderr or "").encode("utf-8")
    return {
        f"{prefix}_returncode": str(result.returncode),
        f"{prefix}_stdout_length": str(len(stdout)),
        f"{prefix}_stdout_sha256": _sha256_bytes(stdout),
        f"{prefix}_stderr_length": str(len(stderr)),
        f"{prefix}_stderr_sha256": _sha256_bytes(stderr),
    }


def _jsonable(value: Any) -> Any:
    operation_cls = globals().get("InstallOperation")
    if operation_cls is not None and isinstance(value, operation_cls):
        result = {
            item.name: _jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.name != "execution_scope"
        }
        if value.execution_scope != "all":
            result["execution_scope"] = value.execution_scope
        return result
    if dataclasses.is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _normalize_harness(value: str) -> str:
    aliases = {
        "claude": "claude",
        "claude-code": "claude",
        "codex": "codex",
    }
    try:
        return aliases[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ManifestError(f"unsupported harness {value!r}") from exc


def _manifest_relative_path(value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{field_name} must stay within its declared root")
    return path


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ManifestError(f"{field_name} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# Claude-Code-side (user-scope) MCP server provisioning (T-3559).
#
# D1 = USER-SCOPE: servers are provisioned at Claude Code's user scope
# (`~/.claude.json` top-level `mcpServers`, cwd-independent) — verified target of
# `claude mcp add-json --scope user`. This resolves the specialist empty-surface bug
# (entries were keyed to the wrong per-project cwd).
#
# D2 = CLI-WRITER: the registration op SHELLS OUT to the `claude` CLI
# (`claude mcp add-json/remove --scope user`) — Claude's own race-free writer —
# rather than doing a read-modify-write of the live high-churn `~/.claude.json`.
# Reads (the check op + preflight) parse the file directly, which is race-free
# (writes land via os.replace, atomic).
# ---------------------------------------------------------------------------

CLAUDE_MCP_SERVERS_SCHEMA = "pantheon-claude-mcp-servers/v1"

# Fail-closed allowlist gate — rulebook/policies/mcp-adoption.md §2 (T-3275,
# landed commit 0750b5e4d). A server absent from this table is not permitted on
# any seat: the question "may seat X run server Y" is answered by set-membership,
# not per-adoption judgment (P1). The per-server frozenset restricts which
# personas may carry it. Adding a row is a reserved (operator-ratified) action.
_CLAUDE_MCP_ALLOWLIST: Mapping[str, frozenset[str]] = {
    "mailbox": frozenset({"orchestrator", "specialist", "mnemosyne", "metis", "atlas"}),
    "pantheon-mcp-codex": frozenset({"orchestrator"}),
}

# Inline-secret gate — an env key whose name reads as a credential is rejected at
# parse time (mcp-adoption.md §5: secrets come from the seat's secret path, never
# inline). Mirrors codex_telegram_bridge.FORBIDDEN_INLINE_KEYS.
_CLAUDE_MCP_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "access_token",
        "client_secret",
        "credential",
        "credentials",
        "private_key",
        "bot_token",
    }
)


def _claude_mcp_projection(server: Mapping[str, Any]) -> dict[str, Any]:
    """Project a server config to the comparable {type,command,args,env} keys,
    defaulting optional fields so an equality compare is stable regardless of
    which writer (installer vs a live Claude session) produced the entry."""
    return {
        "type": server.get("type", "stdio"),
        "command": server.get("command"),
        "args": list(server.get("args", []) or []),
        "env": {str(k): v for k, v in dict(server.get("env", {}) or {}).items()},
    }


def _claude_mcp_expected(server: Mapping[str, Any]) -> str:
    return _canonical_json(_claude_mcp_projection(server))


def _parse_claude_mcp_server(
    raw: Any, *, persona: str, name: str
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ManifestError(f"claude_mcp_servers server {name!r} must be a mapping")
    if raw.get("type") != "stdio":
        raise ManifestError(
            f"claude_mcp_servers server {name!r} type must be 'stdio'"
        )
    command = raw.get("command")
    if not isinstance(command, str) or not command:
        raise ManifestError(
            f"claude_mcp_servers server {name!r} command must be a non-empty string"
        )
    args = raw.get("args", [])
    if not isinstance(args, (list, tuple)) or not all(
        isinstance(item, str) and item for item in args
    ):
        raise ManifestError(
            f"claude_mcp_servers server {name!r} args must be non-empty strings"
        )
    env = raw.get("env", {})
    if not isinstance(env, Mapping):
        raise ManifestError(
            f"claude_mcp_servers server {name!r} env must be a mapping"
        )
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ManifestError(
                f"claude_mcp_servers server {name!r} env must map strings to strings"
            )
        if key.lower() in _CLAUDE_MCP_FORBIDDEN_ENV_KEYS:
            raise ManifestError(
                f"claude_mcp_servers server {name!r} env {key!r}: "
                "inline credential-like key is forbidden"
            )
    return {"type": "stdio", "command": command, "args": list(args), "env": dict(env)}


def parse_claude_mcp_block(raw: Any) -> dict[str, dict[str, Any]]:
    """Parse persona_installer.claude_mcp_servers with a fail-closed allowlist
    gate. Mirrors codex_telegram_bridge.parse_bridge_block: rejects a non-mapping
    block, a missing/unknown schema, a non-'user' scope, a non-allowlisted server,
    a persona not allowlisted for a server, and inline credential-like env keys."""
    if not isinstance(raw, Mapping):
        raise ManifestError("persona_installer.claude_mcp_servers must be a mapping")
    if raw.get("schema") != CLAUDE_MCP_SERVERS_SCHEMA:
        raise ManifestError("unsupported claude_mcp_servers.schema")
    personas = raw.get("personas")
    if not isinstance(personas, Mapping) or not personas:
        raise ManifestError("claude_mcp_servers.personas missing")
    parsed: dict[str, dict[str, Any]] = {}
    for persona, raw_persona in personas.items():
        if not isinstance(persona, str) or not persona:
            raise ManifestError(
                "claude_mcp_servers persona names must be non-empty strings"
            )
        if not isinstance(raw_persona, Mapping):
            raise ManifestError(
                f"claude_mcp_servers persona {persona!r} must be a mapping"
            )
        account = raw_persona.get("account")
        if not isinstance(account, str) or not account:
            raise ManifestError(
                f"claude_mcp_servers persona {persona!r} account must be a non-empty string"
            )
        if raw_persona.get("scope") != "user":
            raise ManifestError(
                f"claude_mcp_servers persona {persona!r} scope must be 'user'"
            )
        servers = raw_persona.get("servers")
        if not isinstance(servers, Mapping) or not servers:
            raise ManifestError(
                f"claude_mcp_servers persona {persona!r} servers missing"
            )
        parsed_servers: dict[str, Any] = {}
        for name, server in servers.items():
            if not isinstance(name, str) or not name:
                raise ManifestError(
                    "claude_mcp_servers server names must be non-empty strings"
                )
            allowed = _CLAUDE_MCP_ALLOWLIST.get(name)
            if allowed is None:
                raise ManifestError(
                    f"claude_mcp_servers server {name!r} is not on the mcp-adoption allowlist"
                )
            if persona not in allowed:
                raise ManifestError(
                    f"claude_mcp_servers server {name!r} is not allowlisted for persona {persona!r}"
                )
            parsed_servers[name] = _parse_claude_mcp_server(
                server, persona=persona, name=name
            )
        parsed[persona] = {
            "account": account,
            "scope": "user",
            "servers": parsed_servers,
        }
    return parsed


def validate_claude_mcp_block(raw: Any) -> None:
    parse_claude_mcp_block(raw)


def _read_claude_user_mcp_servers(path: Any) -> Mapping[str, Any]:
    """Race-free read of the user-scope `mcpServers` map from ~/.claude.json.
    Reading is safe against a live session: writes land via os.replace (atomic),
    so a read observes a whole prior-or-next document, never a torn one."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperationFailed(f"cannot read Claude MCP config {path}: {exc}")
    if not isinstance(document, Mapping):
        raise OperationFailed(f"Claude MCP config {path} root is not a mapping")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, Mapping):
        raise OperationFailed(f"Claude MCP config {path} mcpServers is not a mapping")
    return servers


def _sim_claude_document(snapshot: Mapping[str, str]) -> dict[str, Any]:
    """Decode the SimulatedHostBackend's in-memory ~/.claude.json entry to a
    JSON document (empty dict when absent/blank/malformed)."""
    if snapshot.get("state") != "file":
        return {}
    content = base64.b64decode(snapshot.get("content_b64", "")).decode("utf-8", "replace")
    if not content.strip():
        return {}
    try:
        document = json.loads(content)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _sim_claude_servers(snapshot: Mapping[str, str]) -> Mapping[str, Any]:
    servers = _sim_claude_document(snapshot).get("mcpServers", {})
    return servers if isinstance(servers, Mapping) else {}


def _claude_mcp_verdict(servers: Mapping[str, Any], operation: "InstallOperation") -> str:
    """Tri-state drift verdict for one expected server against the live map:
    'absent' (register), 'matching' (no-op), or 'conflicting' (fail-closed)."""
    name = operation.desired["name"]
    if name not in servers:
        return "absent"
    current = servers[name]
    if not isinstance(current, Mapping):
        return "conflicting"
    if _claude_mcp_expected(current) == operation.desired["expected"]:
        return "matching"
    return "conflicting"


_CLAUDE_MCP_VERDICT_DETAIL = {
    "absent": "server absent — registration would add it",
    "matching": "server already matches manifest",
    "conflicting": "server present but differs from manifest",
}


def _claude_mcp_registration_can_rollback(
    mutation: "MutationRecord", servers: Mapping[str, Any]
) -> "PreflightResult":
    """Shared rollback-safety check for a claude-mcp-server-registration receipt.
    A registration this op did not add is not-applied; one it added is owned iff
    the live server still matches the receipt, else drifted."""
    if mutation.before.get("added") != "true":
        return PreflightResult(True, "not-applied", "server already present pre-apply")
    name = mutation.before.get("name")
    current = servers.get(name)
    if isinstance(current, Mapping) and _claude_mcp_expected(current) == mutation.before.get(
        "expected"
    ):
        return PreflightResult(True, "owned", "registered Claude MCP server matches receipt")
    return PreflightResult(False, "drifted", "registered Claude MCP server changed after apply")


def _runtime_budget_constants() -> dict[str, float]:
    from tools import runtime_controller

    return {
        "runtime_start": float(runtime_controller.RUNTIME_START_TIMEOUT_SECONDS),
        "input_ready": float(runtime_controller.RUNTIME_INPUT_READY_TIMEOUT_SECONDS),
        "provider_completion": float(runtime_controller.PROVIDER_COMPLETION_TIMEOUT_SECONDS),
        "post_start": float(runtime_controller.POST_START_TIMEOUT_SECONDS),
        "post_start_assessment": (
            float(runtime_controller.POST_START_ASSESSMENT_CAPTURE_COUNT)
            * float(runtime_controller.POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS)
        ),
    }


def _manifest_document(manifest: "Manifest") -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(manifest.path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{manifest.path}: cannot reload manifest document: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{manifest.path}: root must be a mapping")
    return raw


def _harness_profile_from_document(
    document: Mapping[str, Any],
    role: str,
    harness: str,
) -> str:
    runtime = document.get("harness_runtime", {})
    if not isinstance(runtime, Mapping):
        return "default"
    role_runtime = runtime.get("roles", {})
    if not isinstance(role_runtime, Mapping):
        return "default"
    role_entry = role_runtime.get(role, {})
    if not isinstance(role_entry, Mapping):
        role_entry = {}
    harness_entry = role_entry.get(harness, {})
    if isinstance(harness_entry, Mapping) and isinstance(harness_entry.get("profile"), str):
        return str(harness_entry["profile"])
    harnesses = runtime.get("harnesses", {})
    if isinstance(harnesses, Mapping):
        configured = harnesses.get(harness, {})
        if isinstance(configured, Mapping) and isinstance(configured.get("default_profile"), str):
            return str(configured["default_profile"])
    return "default"


def _runtime_candidate_refs_for_install(
    manifest: "Manifest",
    binding: "PersonaBinding",
    selected_harness: str,
) -> tuple[str, ...]:
    document = _manifest_document(manifest)
    primary = f"{selected_harness}/{_harness_profile_from_document(document, binding.role, selected_harness)}"
    refs: list[str] = [primary]
    runtime = document.get("harness_runtime", {})
    if isinstance(runtime, Mapping):
        roles = runtime.get("roles", {})
        role_entry = roles.get(binding.role, {}) if isinstance(roles, Mapping) else {}
        fallbacks = role_entry.get("fallbacks", ()) if isinstance(role_entry, Mapping) else ()
        if isinstance(fallbacks, Sequence) and not isinstance(fallbacks, (str, bytes)):
            refs.extend(str(item) for item in fallbacks if isinstance(item, str) and item)
    ordered: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        ordered.append(ref)
    return tuple(ordered or (primary,))


def _runtime_candidate_harnesses_for_install(
    manifest: "Manifest",
    binding: "PersonaBinding",
    selected_harness: str,
) -> tuple[str, ...]:
    harnesses: list[str] = []
    seen: set[str] = set()
    for reference in _runtime_candidate_refs_for_install(manifest, binding, selected_harness):
        harness = reference.split("/", 1)[0]
        if not harness or harness in seen:
            continue
        seen.add(harness)
        harnesses.append(harness)
    return tuple(harnesses)


def _post_start_command_count(
    manifest: "Manifest",
    binding: "PersonaBinding",
) -> int:
    document = _manifest_document(manifest)
    runtime = document.get("harness_runtime", {})
    if not isinstance(runtime, Mapping):
        return 0
    roles = runtime.get("roles", {})
    role_entry = roles.get(binding.role, {}) if isinstance(roles, Mapping) else {}
    if not isinstance(role_entry, Mapping):
        return 0
    post_start = role_entry.get("post_start", ())
    if post_start in (None, ()):
        return 0
    if not isinstance(post_start, Sequence) or isinstance(post_start, (str, bytes)):
        raise ManifestError(f"{manifest.path}: harness_runtime.roles.{binding.role}.post_start must be a list")
    return len(post_start)


def _unit_fire_timeout_seconds(
    *,
    candidate_count: int,
    post_start_count: int,
) -> int:
    if candidate_count < 1:
        raise ManifestError("unit-fire timeout candidate count must be >= 1")
    if post_start_count < 0:
        raise ManifestError("unit-fire timeout post_start count must be >= 0")
    constants = _runtime_budget_constants()
    per_candidate = (
        constants["runtime_start"]
        + constants["input_ready"]
        + constants["provider_completion"]
        + (post_start_count * constants["post_start"])
        + constants["post_start_assessment"]
    )
    timeout = math.ceil(
        (candidate_count * per_candidate)
        + RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS
    )
    if timeout <= 0 or timeout > MAX_UNIT_FIRE_TIMEOUT_SECONDS:
        raise ManifestError(
            f"derived unit-fire timeout {timeout}s is outside the bounded client range"
        )
    return timeout


def _unit_fire_timeout_desired(
    manifest: "Manifest",
    binding: "PersonaBinding",
    selected_harness: str,
) -> dict[str, str]:
    refs = _runtime_candidate_refs_for_install(manifest, binding, selected_harness)
    post_start_count = _post_start_command_count(manifest, binding)
    timeout = _unit_fire_timeout_seconds(
        candidate_count=len(refs),
        post_start_count=post_start_count,
    )
    return {
        "unit_fire_timeout_seconds": str(timeout),
        "unit_fire_timeout_candidate_count": str(len(refs)),
        "unit_fire_timeout_post_start_count": str(post_start_count),
        "unit_fire_timeout_formula": UNIT_FIRE_TIMEOUT_FORMULA,
    }


def _unit_fire_initial_viability_desired(
    manifest: "Manifest",
    binding: "PersonaBinding",
    selected_harness: str,
    *,
    account_home: Path,
    bridge_binding: codex_telegram_bridge.CodexTelegramBinding | None,
) -> dict[str, str]:
    refs = _runtime_candidate_refs_for_install(manifest, binding, selected_harness)
    receiver_operations: dict[str, list[str]] = {}
    if bridge_binding is not None:
        for ref in refs:
            receiver_operations[ref] = (
                list(
                    receiver_handoff_actions(
                        claude_receiver_topology=bridge_binding.claude_receiver_topology,
                        watchdog_units=bridge_binding.watchdog_units,
                        direction="claude-to-codex",
                        phase="post",
                    )
                )
                if ref.partition("/")[0] == "codex"
                else []
            )
    return {
        "account_home": str(account_home),
        "persona_state_root": str(account_home / ".local" / "state" / "pantheon" / "personas"),
        "allowed_runtime_refs": json.dumps(list(refs), sort_keys=True, separators=(",", ":")),
        "require_invocation_receipt": "true",
        "invocation_receipt_schema": "pantheon-runtime-initial-viability-invocation/v1",
        "receiver_receipt_required": str(bridge_binding is not None).lower(),
        "receiver_operations_by_runtime": json.dumps(receiver_operations, sort_keys=True, separators=(",", ":")),
        "communications_kind": "telegram" if bridge_binding is not None else "none",
        "claude_receiver_topology": (
            bridge_binding.claude_receiver_topology if bridge_binding is not None else ""
        ),
        "predecessor_claude_receiver_topology": (
            bridge_binding.predecessor_claude_receiver_topology if bridge_binding is not None else ""
        ),
        "codex_receiver_unit": (
            bridge_binding.codex_receiver_unit if bridge_binding is not None else ""
        ),
    }


def _canonical_decimal_int(raw: Any, field_name: str, *, allow_zero: bool) -> int:
    if not isinstance(raw, str) or not raw.isdecimal():
        raise OperationFailed(f"{field_name} must be a canonical decimal integer")
    if raw != "0" and raw.startswith("0"):
        raise OperationFailed(f"{field_name} must not contain leading zeroes")
    value = int(raw)
    if value == 0 and not allow_zero:
        raise OperationFailed(f"{field_name} must be positive")
    if value < 0:
        raise OperationFailed(f"{field_name} must be non-negative")
    return value


@dataclass(frozen=True)
class UnitFireTimeoutBudget:
    seconds: int
    candidate_count: int
    post_start_count: int
    formula: str


@dataclass(frozen=True)
class UnitFireInitialViabilityDesired:
    account_home: Path
    persona_state_root: Path
    allowed_runtime_refs: tuple[str, ...]
    require_invocation_receipt: bool
    receiver_required: bool
    receiver_operations_by_runtime: Mapping[str, tuple[str, ...]]


def _parse_unit_fire_timeout_budget(operation: "InstallOperation") -> UnitFireTimeoutBudget:
    seconds = _canonical_decimal_int(
        operation.desired.get("unit_fire_timeout_seconds"),
        "unit_fire_timeout_seconds",
        allow_zero=False,
    )
    if seconds > MAX_UNIT_FIRE_TIMEOUT_SECONDS:
        raise OperationFailed("unit_fire_timeout_seconds is outside the bounded client range")
    candidate_count = _canonical_decimal_int(
        operation.desired.get("unit_fire_timeout_candidate_count"),
        "unit_fire_timeout_candidate_count",
        allow_zero=False,
    )
    post_start_count = _canonical_decimal_int(
        operation.desired.get("unit_fire_timeout_post_start_count"),
        "unit_fire_timeout_post_start_count",
        allow_zero=True,
    )
    formula = operation.desired.get("unit_fire_timeout_formula")
    if formula != UNIT_FIRE_TIMEOUT_FORMULA:
        raise OperationFailed("unit_fire_timeout_formula does not match the canonical formula")
    expected = _unit_fire_timeout_seconds(
        candidate_count=candidate_count,
        post_start_count=post_start_count,
    )
    if seconds != expected:
        raise OperationFailed("unit-fire timeout seconds do not match the declared counts")
    return UnitFireTimeoutBudget(
        seconds=seconds,
        candidate_count=candidate_count,
        post_start_count=post_start_count,
        formula=formula,
    )


def _canonical_json_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise OperationFailed(f"{field_name} must be a canonical JSON string")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OperationFailed(f"{field_name} must be valid JSON") from exc
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != value:
        raise OperationFailed(f"{field_name} must use canonical JSON encoding")
    return value


def _parse_canonical_bool(raw: Any, field_name: str) -> bool:
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise OperationFailed(f"{field_name} must be canonical true/false")


def _parse_unit_fire_initial_viability_desired(operation: "InstallOperation") -> UnitFireInitialViabilityDesired:
    try:
        account_home = Path(str(operation.desired["account_home"]))
        state_root = Path(str(operation.desired["persona_state_root"]))
    except KeyError as exc:
        raise OperationFailed(f"{exc.args[0]} missing from unit-fire desired") from exc
    if not account_home.is_absolute() or not state_root.is_absolute():
        raise OperationFailed("unit-fire account/state paths must be absolute")
    try:
        if not state_root.resolve().is_relative_to(account_home.resolve()):
            raise OperationFailed("persona state root escapes account home")
    except OSError as exc:
        raise OperationFailed(f"persona state root cannot be resolved: {exc}") from exc
    raw_refs = _canonical_json_string(operation.desired.get("allowed_runtime_refs"), "allowed_runtime_refs")
    refs = json.loads(raw_refs)
    if (
        not isinstance(refs, list)
        or not refs
        or not all(isinstance(item, str) and item for item in refs)
        or len(set(refs)) != len(refs)
    ):
        raise OperationFailed("allowed_runtime_refs must be a nonempty deduplicated string list")
    require_invocation = _parse_canonical_bool(
        operation.desired.get("require_invocation_receipt"),
        "require_invocation_receipt",
    )
    receiver_required = _parse_canonical_bool(
        operation.desired.get("receiver_receipt_required"),
        "receiver_receipt_required",
    )
    raw_receiver_operations = _canonical_json_string(
        operation.desired.get("receiver_operations_by_runtime"),
        "receiver_operations_by_runtime",
    )
    receiver_operations = json.loads(raw_receiver_operations)
    if not isinstance(receiver_operations, dict):
        raise OperationFailed("receiver_operations_by_runtime must be a mapping")
    parsed_receiver_operations: dict[str, tuple[str, ...]] = {}
    for key, value in receiver_operations.items():
        if not isinstance(key, str) or key not in refs:
            raise OperationFailed("receiver operation expectation names an undeclared runtime")
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise OperationFailed("receiver operation expectation must be a string list")
        parsed_receiver_operations[key] = tuple(value)
    schema = operation.desired.get("invocation_receipt_schema")
    if schema != "pantheon-runtime-initial-viability-invocation/v1":
        raise OperationFailed("invocation receipt schema mismatch")
    return UnitFireInitialViabilityDesired(
        account_home=account_home,
        persona_state_root=state_root,
        allowed_runtime_refs=tuple(refs),
        require_invocation_receipt=require_invocation,
        receiver_required=receiver_required,
        receiver_operations_by_runtime=parsed_receiver_operations,
    )


@dataclass(frozen=True)
class TimerUnitState:
    classification: Literal["enabled", "disabled", "not-found"]
    load_state: str
    unit_file_state: str
    active_state: str
    sub_state: str


@dataclass(frozen=True)
class TimerEnablementLinkProof:
    link_path: Path
    canonical_template_path: Path
    snapshot_state: str
    actual_readlink: str
    resolved_target: Path | None
    actual_readlink_sha256: str
    canonical_template_path_sha256: str
    resolved_target_sha256: str


def _consume_timer_template_unit(timer_unit: str) -> str:
    if not timer_unit.startswith("pantheon-persona-consume@") or not timer_unit.endswith(".timer"):
        raise OperationFailed("timer enablement target is not a consume timer")
    return "pantheon-persona-consume@.timer"


def _consume_timer_template_path(account_home: Path, timer_unit: str) -> Path:
    return account_home / ".config" / "systemd" / "user" / _consume_timer_template_unit(timer_unit)


def _consume_timer_enablement_link_path(account_home: Path, timer_unit: str) -> Path:
    if not timer_unit.startswith("pantheon-persona-consume@") or not timer_unit.endswith(".timer"):
        raise OperationFailed("timer enablement target is not a consume timer")
    return account_home / ".config" / "systemd" / "user" / "timers.target.wants" / timer_unit


def _lexically_normalize_absolute(path: Path) -> Path:
    return Path(os.path.normpath(str(path)))


def _resolve_timer_enablement_readlink(link_path: Path, actual_readlink: str) -> Path:
    target = Path(actual_readlink)
    if target.is_absolute():
        return _lexically_normalize_absolute(target)
    return _lexically_normalize_absolute(link_path.parent / target)


def _timer_enablement_link_proof(
    link_path: Path,
    canonical_template_path: Path,
) -> TimerEnablementLinkProof:
    canonical = _lexically_normalize_absolute(canonical_template_path)
    if link_path.is_symlink():
        actual = os.readlink(link_path)
        resolved = _resolve_timer_enablement_readlink(link_path, actual)
        return TimerEnablementLinkProof(
            link_path=link_path,
            canonical_template_path=canonical,
            snapshot_state="symlink",
            actual_readlink=actual,
            resolved_target=resolved,
            actual_readlink_sha256=_sha256_bytes(actual.encode("utf-8")),
            canonical_template_path_sha256=_sha256_bytes(str(canonical).encode("utf-8")),
            resolved_target_sha256=_sha256_bytes(str(resolved).encode("utf-8")),
        )
    state = "absent" if not link_path.exists() else _symlink_snapshot(link_path)["state"]
    return TimerEnablementLinkProof(
        link_path=link_path,
        canonical_template_path=canonical,
        snapshot_state=state,
        actual_readlink="",
        resolved_target=None,
        actual_readlink_sha256="",
        canonical_template_path_sha256=_sha256_bytes(str(canonical).encode("utf-8")),
        resolved_target_sha256="",
    )


def _timer_enablement_link_evidence(prefix: str, proof: TimerEnablementLinkProof) -> dict[str, str]:
    return {
        f"{prefix}_enablement_link_path": str(proof.link_path),
        f"{prefix}_canonical_template_path": str(proof.canonical_template_path),
        f"{prefix}_enablement_link_state": proof.snapshot_state,
        f"{prefix}_enablement_link_actual_readlink": proof.actual_readlink,
        f"{prefix}_enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
        f"{prefix}_enablement_link_resolved_target": str(proof.resolved_target or ""),
        f"{prefix}_enablement_link_resolved_target_sha256": proof.resolved_target_sha256,
        f"{prefix}_canonical_template_path_sha256": proof.canonical_template_path_sha256,
    }


def _timer_enablement_link_matches_canonical(proof: TimerEnablementLinkProof) -> bool:
    return (
        proof.snapshot_state == "symlink"
        and proof.resolved_target is not None
        and _lexically_normalize_absolute(proof.resolved_target)
        == _lexically_normalize_absolute(proof.canonical_template_path)
    )


def _timer_enablement_absence_admissible_classifications(
    staged_evidence: OperationEvidence | None,
) -> frozenset[str]:
    """T-3611: the timer-enablement-absence-check's post-condition invariant
    is 'absent' by default (verify_operation checks a POST-condition, not
    preflight()'s input-admission classification -- see T-3611 ticket
    Reassessment). 'converged-legacy' is only additionally admissible when
    the STAGED RECEIPT's own evidence for this operation_id proves the
    enablement link already existed -- and was already accepted via the
    live-convergence probe -- at STAGE time (``pre_enablement_link_state``
    not absent, ``ok`` True). That staged evidence is the one channel that
    legitimately survives the stage_disabled -> activate_staged process
    boundary in production (each is a separate CLI invocation constructing
    a fresh backend; in-memory ``self.events`` does not survive it, unlike
    the test fixtures that happen to share one backend instance across both
    calls). A staged evidence showing 'absent' (the enablement link did not
    exist yet at stage time) must NOT be widened -- that is exactly the
    dangling/TOCTOU shape (something enabled the timer AFTER staging, before
    activation) that this check exists to catch.
    """
    admissible = {"absent"}
    if (
        staged_evidence is not None
        and staged_evidence.ok
        and staged_evidence.evidence.get("pre_enablement_link_state") not in (None, "absent")
    ):
        admissible.add("converged-legacy")
    return frozenset(admissible)


STALE_UNIT_FAILURE_RESET_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "InvocationID",
    "Job",
)


@dataclass(frozen=True)
class StaleUnitFailureResetSnapshot:
    load_state: str
    active_state: str
    sub_state: str
    result: str
    exec_main_status: str
    invocation_id: str
    job: str
    stdout_length: int
    stdout_sha256: str


def _parse_stale_unit_failure_reset_show(stdout: str) -> StaleUnitFailureResetSnapshot:
    values: dict[str, str] = {}
    allowed = set(STALE_UNIT_FAILURE_RESET_PROPERTIES)
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line.count("=") != 1:
            raise OperationFailed("stale unit show output is malformed")
        key, value = line.split("=", 1)
        if key not in allowed:
            raise OperationFailed("stale unit show output contains unexpected keys")
        if key in values:
            raise OperationFailed("stale unit show output contains duplicate keys")
        values[key] = value
    if set(values) != allowed:
        raise OperationFailed("stale unit show output missing required keys")
    raw = stdout.encode("utf-8")
    return StaleUnitFailureResetSnapshot(
        load_state=values["LoadState"],
        active_state=values["ActiveState"],
        sub_state=values["SubState"],
        result=values["Result"],
        exec_main_status=values["ExecMainStatus"],
        invocation_id=values["InvocationID"],
        job=values["Job"],
        stdout_length=len(raw),
        stdout_sha256=_sha256_bytes(raw),
    )


def _canonical_positive_decimal(value: str) -> bool:
    return value.isdigit() and value != "0" and str(int(value)) == value


def _classify_stale_unit_failure_reset_snapshot(
    snapshot: StaleUnitFailureResetSnapshot,
) -> Literal["clean-noop", "reset-required"]:
    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "inactive"
        and snapshot.sub_state == "dead"
        and snapshot.result == "success"
        and snapshot.exec_main_status == "0"
        and snapshot.invocation_id == ""
        and snapshot.job == ""
    ):
        return "clean-noop"
    if snapshot.load_state == "loaded":
        raise OperationFailed("pre-existing loaded service")
    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "failed"
        and snapshot.sub_state == "failed"
        and snapshot.result == "exit-code"
        and _canonical_positive_decimal(snapshot.exec_main_status)
        and _valid_systemd_invocation_id(snapshot.invocation_id)
        and snapshot.job == ""
    ):
        return "reset-required"
    raise OperationFailed("unsupported stale unit state")


def _stale_unit_failure_reset_snapshot_evidence(
    prefix: str,
    snapshot: StaleUnitFailureResetSnapshot,
) -> dict[str, str]:
    invocation_state = "present" if snapshot.invocation_id else "empty"
    return {
        f"{prefix}_load_state": snapshot.load_state,
        f"{prefix}_active_state": snapshot.active_state,
        f"{prefix}_sub_state": snapshot.sub_state,
        f"{prefix}_result": snapshot.result,
        f"{prefix}_exec_main_status": snapshot.exec_main_status,
        f"{prefix}_invocation_id_state": invocation_state,
        f"{prefix}_invocation_id_valid": str(
            _valid_systemd_invocation_id(snapshot.invocation_id)
        ).lower(),
        f"{prefix}_invocation_id_sha256": _unit_fire_invocation_digest(
            snapshot.invocation_id
        ),
        f"{prefix}_job": snapshot.job,
        f"{prefix}_stdout_length": str(snapshot.stdout_length),
        f"{prefix}_stdout_sha256": snapshot.stdout_sha256,
    }


def _stale_unit_failure_reset_failure(
    detail: str,
    *,
    pre: StaleUnitFailureResetSnapshot | None = None,
    post: StaleUnitFailureResetSnapshot | None = None,
    reset_returncode: int | None = None,
    reset_stdout: str = "",
    reset_stderr: str = "",
) -> OperationFailed:
    evidence: dict[str, str] = {"classification": "refused"}
    if pre is not None:
        evidence.update(_stale_unit_failure_reset_snapshot_evidence("pre", pre))
    if post is not None:
        evidence.update(_stale_unit_failure_reset_snapshot_evidence("post", post))
    if reset_returncode is not None:
        evidence["reset_returncode"] = str(reset_returncode)
        evidence["reset_stdout_length"] = str(len(reset_stdout.encode("utf-8")))
        evidence["reset_stdout_sha256"] = _sha256_bytes(reset_stdout.encode("utf-8"))
        evidence["reset_stderr_length"] = str(len(reset_stderr.encode("utf-8")))
        evidence["reset_stderr_sha256"] = _sha256_bytes(reset_stderr.encode("utf-8"))
    return OperationFailed(detail, evidence=evidence)


def _parse_systemctl_timer_show(stdout: str) -> TimerUnitState:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if line == "":
            continue
        if line.count("=") != 1:
            raise OperationFailed("timer show output is malformed")
        key, value = line.split("=", 1)
        if key not in {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
            raise OperationFailed("timer show output contains unexpected keys")
        if key in values:
            raise OperationFailed("timer show output contains duplicate keys")
        values[key] = value
    if set(values) != {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
        raise OperationFailed("timer show output missing required keys")
    load_state = values["LoadState"]
    unit_file_state = values["UnitFileState"]
    active_state = values["ActiveState"]
    sub_state = values["SubState"]
    if load_state == "loaded" and unit_file_state in {"enabled", "disabled"}:
        classification: Literal["enabled", "disabled"] = (
            "enabled" if unit_file_state == "enabled" else "disabled"
        )
        return TimerUnitState(
            classification=classification,
            load_state=load_state,
            unit_file_state=unit_file_state,
            active_state=active_state,
            sub_state=sub_state,
        )
    if load_state == "not-found" and unit_file_state in {"", "not-found"}:
        return TimerUnitState(
            classification="not-found",
            load_state=load_state,
            unit_file_state=unit_file_state,
            active_state=active_state,
            sub_state=sub_state,
        )
    raise OperationFailed("timer show output has unsupported load/unit-file state")


def _quota_check_install_config(
    manifest: Manifest,
    *,
    repo_root: Path,
) -> QuotaCheckInstallConfig:
    raw = manifest.installer.get("quota_check")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ManifestError("persona_installer.quota_check must be a mapping")
    cadence = _positive_int(raw.get("cadence_minutes", 30), "persona_installer.quota_check.cadence_minutes")
    randomized = _positive_int(
        raw.get("randomized_delay_seconds", 300),
        "persona_installer.quota_check.randomized_delay_seconds",
    )
    personas = raw.get("personas", ["specialist", "recovery"])
    if not (
        isinstance(personas, list)
        and all(isinstance(item, str) and item for item in personas)
    ):
        raise ManifestError("persona_installer.quota_check.personas must be a list of persona strings")
    service = _manifest_relative_path(
        raw.get("service_template", "tools/systemd/pantheon-persona-quota@.service"),
        "persona_installer.quota_check.service_template",
    )
    timer = _manifest_relative_path(
        raw.get("timer_template", "tools/systemd/pantheon-persona-quota@.timer"),
        "persona_installer.quota_check.timer_template",
    )
    for source in (repo_root / service, repo_root / timer):
        if not source.is_file():
            raise ManifestError(f"framework template missing: {source}")
    return QuotaCheckInstallConfig(
        cadence_minutes=cadence,
        randomized_delay_seconds=randomized,
        personas=frozenset(personas),
        service_template=service,
        timer_template=timer,
    )


def _render_quota_timer_template(
    template_text: str,
    *,
    cadence_minutes: int,
    randomized_delay_seconds: int,
) -> str:
    return template_text.format(
        quota_cadence_minutes=cadence_minutes,
        quota_randomized_delay_seconds=randomized_delay_seconds,
    )


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo_root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "unversioned"
    return result.stdout.strip()


def _artifact_digest(repo_root: Path, artifacts: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(artifacts, key=str):
        path = (repo_root / relative).resolve()
        if not path.is_relative_to(repo_root.resolve()) or not path.is_file():
            raise ManifestError(f"framework artifact missing or escaped root: {relative}")
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ActorContext:
    uid: int
    persona: str
    role: str
    instance: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class CredentialReference:
    reference_id: str
    kind: str
    provider: str
    owner: str
    scope: tuple[str, ...]
    rotation_strategy: str
    revocation_plan: str
    location: str | None = None
    direct_use_justification: str | None = None


@dataclass(frozen=True)
class PersonaBinding:
    role: str
    persona: str
    account: str
    configured_harness: str
    persona_brief_ref: str | None = None
    credential_refs: tuple[CredentialReference, ...] = ()


@dataclass(frozen=True)
class QuotaCheckInstallConfig:
    cadence_minutes: int
    randomized_delay_seconds: int
    personas: frozenset[str]
    service_template: Path
    timer_template: Path


@dataclass(frozen=True)
class CodexStandingInstructionsReference:
    persona: str
    account: str
    source_repo: str
    source: Path
    source_commit: str
    source_sha256: str
    target: Path
    override_target: Path
    override_must_be_absent: bool
    forbidden_source_paths: tuple[Path, ...]
    identity: str
    canonical_source_of_truth: str


@dataclass(frozen=True)
class Manifest:
    path: Path
    version: int
    instance_id: str
    installer: Mapping[str, Any]
    roles: Mapping[str, Mapping[str, Any]]
    harness_runtime: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_bytes(cls, path: Path, raw: bytes) -> "Manifest":
        try:
            document = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ManifestError(f"{path}: cannot load manifest: {exc}") from exc
        if not isinstance(document, dict):
            raise ManifestError(f"{path}: root must be a mapping")
        version = document.get("manifest_version")
        if not isinstance(version, int) or version < 2:
            raise ManifestError(f"{path}: manifest_version must be an integer >= 2")
        instance = document.get("instance")
        roles = document.get("roles")
        installer = document.get("persona_installer")
        harness_runtime = document.get("harness_runtime", {})
        if not isinstance(instance, dict) or not isinstance(instance.get("id"), str):
            raise ManifestError(f"{path}: instance.id missing")
        if not isinstance(roles, dict) or not roles:
            raise ManifestError(f"{path}: roles mapping missing")
        if not isinstance(harness_runtime, dict):
            raise ManifestError(f"{path}: harness_runtime must be a mapping")
        try:
            validate_harness_runtime_permission_postures(harness_runtime)
        except PermissionPostureError as exc:
            raise ManifestError(f"{path}: {exc}") from exc
        if not isinstance(installer, dict):
            raise ManifestError(f"{path}: persona_installer mapping missing")
        if installer.get("schema") != "pantheon-persona-install/v1":
            raise ManifestError(f"{path}: unsupported persona_installer.schema")
        security = installer.get("security")
        if not isinstance(security, dict):
            raise ManifestError(f"{path}: persona_installer.security missing")
        if security.get("credential_mode") != "references-only":
            raise ManifestError(f"{path}: credential mode must be references-only")
        if security.get("prohibit_auth_copy") is not True:
            raise ManifestError(f"{path}: prohibit_auth_copy must be true")
        bridge = installer.get("codex_telegram_bridge")
        if bridge is not None:
            try:
                codex_telegram_bridge.validate_bridge_block(bridge)
            except codex_telegram_bridge.BridgeError as exc:
                raise ManifestError(f"{path}: {exc}") from exc
        claude_mcp = installer.get("claude_mcp_servers")
        if claude_mcp is not None:
            try:
                validate_claude_mcp_block(claude_mcp)
            except ManifestError as exc:
                raise ManifestError(f"{path}: {exc}") from exc
        return cls(
            path=path,
            version=version,
            instance_id=instance["id"],
            installer=installer,
            roles=roles,
            harness_runtime=harness_runtime,
        )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ManifestError(f"{path}: cannot load manifest: {exc}") from exc
        return cls.from_bytes(path, raw)

    def binding(self, persona: str) -> PersonaBinding:
        for role, raw in self.roles.items():
            if not isinstance(raw, dict) or raw.get("persona") != persona:
                continue
            account = raw.get("account")
            harness = raw.get("harness")
            if not isinstance(account, str) or not account:
                raise ManifestError(f"{self.path}: {role}.account missing")
            if not isinstance(harness, str) or not harness:
                raise ManifestError(f"{self.path}: {role}.harness missing")
            refs = _parse_credential_refs(raw.get("credential_refs", ()), self.path, role)
            try:
                persona_brief_ref = validate_persona_brief_ref(
                    raw.get("persona_brief_ref"),
                    field=f"{role}.persona_brief_ref",
                )
            except PersonaBriefError as exc:
                raise ManifestError(f"{self.path}: {exc}") from exc
            return PersonaBinding(
                role=str(role),
                persona=persona,
                account=account,
                configured_harness=_normalize_harness(harness),
                persona_brief_ref=persona_brief_ref,
                credential_refs=refs,
            )
        raise ManifestError(f"{self.path}: persona {persona!r} is not bound")

    def codex_telegram_source(self) -> codex_telegram_bridge.CodexTelegramSource:
        try:
            source, _ = codex_telegram_bridge.parse_bridge_block(
                self.installer.get("codex_telegram_bridge")
            )
        except codex_telegram_bridge.BridgeError as exc:
            raise ManifestError(f"{self.path}: {exc}") from exc
        return source

    def codex_telegram_binding(
        self,
        persona: str,
        *,
        root: Path = Path("/"),
    ) -> codex_telegram_bridge.CodexTelegramBinding:
        try:
            return codex_telegram_bridge.binding_from_manifest(
                self,
                persona,
                root=root,
            )
        except codex_telegram_bridge.BridgeError as exc:
            raise ManifestError(f"{self.path}: {exc}") from exc

    def claude_mcp_servers_for(self, persona: str, *, account: str) -> dict[str, Any]:
        """Return the parsed, account-guarded claude_mcp_servers block for a
        persona, or ``{}`` when the manifest carries no such block or the persona
        is not listed. Raises ManifestError on an account-guard mismatch."""
        raw = self.installer.get("claude_mcp_servers")
        if raw is None:
            return {}
        parsed = parse_claude_mcp_block(raw)
        persona_block = parsed.get(persona)
        if persona_block is None:
            return {}
        if persona_block["account"] != account:
            raise ManifestError(
                f"{self.path}: claude_mcp_servers persona {persona!r} account "
                f"{persona_block['account']!r} does not match bound account {account!r}"
            )
        return persona_block


def _manifest_role_codex_block(manifest: Manifest, *, role: str) -> Mapping[str, Any]:
    roles = manifest.harness_runtime.get("roles")
    if not isinstance(roles, Mapping):
        return {}
    role_raw = roles.get(role)
    if not isinstance(role_raw, Mapping):
        return {}
    codex_raw = role_raw.get("codex")
    if not isinstance(codex_raw, Mapping):
        return {}
    return codex_raw


def manifest_role_requires_unattended_telegram(manifest: Manifest, *, role: str) -> bool:
    return requires_unattended_telegram(_manifest_role_codex_block(manifest, role=role))


_FORBIDDEN_CREDENTIAL_KEYS = {
    "value",
    "secret",
    "token",
    "password",
    "private_key",
    "contents",
    "auth_file",
    "copy_from",
    "template",
    "shell",
    "command",
}


def _parse_credential_refs(
    raw: Any,
    path: Path,
    role: str,
) -> tuple[CredentialReference, ...]:
    if raw in (None, ()):
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{path}: {role}.credential_refs must be a list")
    result: list[CredentialReference] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{path}: {role}.credential_refs[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where} must be a mapping")
        forbidden = sorted(_FORBIDDEN_CREDENTIAL_KEYS.intersection(item))
        if forbidden:
            raise ManifestError(f"{where} contains forbidden value/copy keys: {forbidden}")
        required = ("id", "kind", "provider", "owner", "scope", "rotation")
        missing = [key for key in required if key not in item]
        if missing:
            raise ManifestError(f"{where} missing {missing}")
        scope = item["scope"]
        rotation = item["rotation"]
        if not isinstance(scope, list) or not scope or not all(
            isinstance(entry, str) and entry for entry in scope
        ):
            raise ManifestError(f"{where}.scope must be a non-empty string list")
        if not isinstance(rotation, dict):
            raise ManifestError(f"{where}.rotation must be a mapping")
        strategy = rotation.get("strategy")
        revoke = rotation.get("revoke")
        if not isinstance(strategy, str) or not strategy:
            raise ManifestError(f"{where}.rotation.strategy missing")
        if not isinstance(revoke, str) or not revoke:
            raise ManifestError(f"{where}.rotation.revoke missing")
        provider = item["provider"]
        justification = item.get("direct_use_justification")
        if provider != "broker" and not (
            isinstance(justification, str) and justification.strip()
        ):
            raise ManifestError(
                f"{where}: direct provider requires direct_use_justification"
            )
        string_fields = ("id", "kind", "provider", "owner")
        if not all(isinstance(item[key], str) and item[key] for key in string_fields):
            raise ManifestError(f"{where}: id/kind/provider/owner must be non-empty strings")
        if item["id"] in seen_ids:
            raise ManifestError(f"{where}: duplicate credential reference id")
        seen_ids.add(item["id"])
        location = item.get("location")
        if location is not None and not isinstance(location, str):
            raise ManifestError(f"{where}.location must be a string")
        result.append(
            CredentialReference(
                reference_id=item["id"],
                kind=item["kind"],
                provider=provider,
                owner=item["owner"],
                scope=tuple(scope),
                rotation_strategy=strategy,
                revocation_plan=revoke,
                location=location,
                direct_use_justification=justification,
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class InstallOperation:
    operation_id: str
    layer: int
    kind: OperationKind
    target: str
    precondition: Mapping[str, str]
    desired: Mapping[str, str]
    inverse: Mapping[str, str] | None
    privilege: PrivilegeRequirement
    owner: str
    provenance: Mapping[str, str]
    execution_scope: ExecutionScope = "all"


@dataclass(frozen=True)
class ExecutionContext:
    workflow: Workflow


EXECUTION_CONTEXT_ALL_IN_ONE = ExecutionContext("all-in-one")
EXECUTION_CONTEXT_STAGE_PREFIX = ExecutionContext("stage-prefix")
EXECUTION_CONTEXT_STAGED_ACTIVATE = ExecutionContext("staged-activate")


def _operation_in_scope(
    operation: InstallOperation,
    scopes: frozenset[ExecutionScope],
) -> bool:
    return operation.execution_scope in scopes


@dataclass(frozen=True)
class OperatorAction:
    action_id: str
    layer: int
    target: str
    reason: str
    required_capability: str


@dataclass(frozen=True)
class InstallPlan:
    schema: str
    manifest_version: int
    instance: str
    role: str
    persona: str
    account: str
    configured_harness: str
    selected_harness: str
    root: str
    operations: tuple[InstallOperation, ...]
    operator_actions: tuple[OperatorAction, ...]
    provenance: Mapping[str, str]
    digest: str

    def payload(self) -> Mapping[str, Any]:
        value = _jsonable(self)
        value.pop("digest", None)
        return value

    def to_dict(self) -> Mapping[str, Any]:
        return _jsonable(self)

    @classmethod
    def create(cls, **kwargs: Any) -> "InstallPlan":
        candidate = cls(digest="", **kwargs)
        digest = _sha256_bytes(_canonical_json(candidate.payload()).encode("utf-8"))
        return dataclasses.replace(candidate, digest=digest)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InstallPlan":
        operations = tuple(InstallOperation(**item) for item in raw["operations"])
        actions = tuple(OperatorAction(**item) for item in raw["operator_actions"])
        return cls(
            schema=raw["schema"],
            manifest_version=raw["manifest_version"],
            instance=raw["instance"],
            role=raw["role"],
            persona=raw["persona"],
            account=raw["account"],
            configured_harness=raw["configured_harness"],
            selected_harness=raw["selected_harness"],
            root=raw["root"],
            operations=operations,
            operator_actions=actions,
            provenance=raw["provenance"],
            digest=raw["digest"],
        )

    def validate_digest(self) -> bool:
        expected = _sha256_bytes(_canonical_json(self.payload()).encode("utf-8"))
        return expected == self.digest


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    classification: str
    detail: str


def _brief_readability_preflight(operation: InstallOperation) -> PreflightResult:
    path = Path(operation.target)
    ok = path.is_file() and os.access(path, os.R_OK)
    return PreflightResult(
        ok,
        "persona",
        "persona brief present" if ok else "persona brief missing",
    )


@dataclass(frozen=True)
class MutationRecord:
    operation_id: str
    kind: str
    target: str
    owner: str
    changed: bool
    before: Mapping[str, str]
    after_fingerprint: str | None


@dataclass(frozen=True)
class OperationEvidence:
    operation_id: str
    layer: int
    kind: str
    ok: bool
    detail: str
    evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyReceipt:
    schema: str
    receipt_id: str
    phase: Literal["staged", "complete", "failed"]
    plan_digest: str
    instance: str
    persona: str
    role: str
    account: str
    actor: Mapping[str, str]
    mutations: tuple[MutationRecord, ...]
    evidence: tuple[OperationEvidence, ...]
    created_at: str

    def to_dict(self) -> Mapping[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApplyReceipt":
        return cls(
            schema=raw["schema"],
            receipt_id=raw["receipt_id"],
            phase=raw.get("phase", "complete"),
            plan_digest=raw["plan_digest"],
            instance=raw["instance"],
            persona=raw["persona"],
            role=raw["role"],
            account=raw["account"],
            actor=raw["actor"],
            mutations=tuple(MutationRecord(**item) for item in raw["mutations"]),
            evidence=tuple(OperationEvidence(**item) for item in raw["evidence"]),
            created_at=raw["created_at"],
        )


@dataclass(frozen=True)
class ApplyReport:
    status: Literal["applied", "noop", "refused", "failed"]
    changed: int
    receipt: ApplyReceipt | None
    errors: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyLayer:
    layer: int
    name: str
    ok: bool
    checks: tuple[OperationEvidence, ...]


@dataclass(frozen=True)
class VerifyReport:
    status: Literal["verified", "failed"]
    layers: tuple[VerifyLayer, ...]
    provenance: Mapping[str, str]
    evidence_scope: str
    live_acceptance: bool


@dataclass(frozen=True)
class RollbackReport:
    status: Literal["rolled-back", "noop", "refused", "failed"]
    restored: int
    errors: tuple[str, ...] = ()


class HostBackend(Protocol):
    evidence_scope: str

    def preflight(self, operation: InstallOperation) -> PreflightResult: ...

    def execute(
        self,
        operation: InstallOperation,
        context: ExecutionContext,
    ) -> tuple[MutationRecord, OperationEvidence]: ...

    def verify_operation(
        self,
        operation: InstallOperation,
        *,
        staged_evidence: OperationEvidence | None = None,
    ) -> OperationEvidence: ...

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult: ...

    def rollback_mutation(self, mutation: MutationRecord) -> None: ...

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None: ...

    def issued_timer_enable_recovery_mutation(
        self,
        operation: InstallOperation,
        evidence: Mapping[str, str],
    ) -> MutationRecord | None: ...

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None: ...


def _provenance(
    manifest: Manifest,
    binding: PersonaBinding,
    selected_harness: str,
    capability: str = "",
) -> dict[str, str]:
    result = {
        "manifest": str(manifest.path),
        "manifest_version": str(manifest.version),
        "instance": manifest.instance_id,
        "role": binding.role,
        "persona": binding.persona,
        "account": binding.account,
        "configured_harness": binding.configured_harness,
        "selected_harness": selected_harness,
        "source_revision": str(
            manifest.installer.get("framework", {}).get("revision", "unknown")
        ),
    }
    if capability:
        result["required_capability"] = capability
    return result


def _rooted(root: Path, absolute: Path) -> Path:
    if not absolute.is_absolute():
        raise ManifestError(f"installer target must be absolute: {absolute}")
    if root == Path("/"):
        return absolute
    return root / absolute.relative_to("/")


def _resolved_persona_brief_path(repo_root: Path, account_home: Path, binding: PersonaBinding) -> Path:
    try:
        return resolve_persona_brief_path(
            repo_root=repo_root,
            account_home=account_home,
            persona=binding.persona,
            persona_brief_ref=binding.persona_brief_ref,
        )
    except PersonaBriefError as exc:
        raise ManifestError(f"{binding.role}.persona_brief_ref invalid: {exc}") from exc


def _content_operation(
    *,
    operation_id: str,
    layer: int,
    kind: OperationKind,
    target: Path,
    content: str,
    owner: str,
    privilege: PrivilegeRequirement,
    provenance: Mapping[str, str],
) -> InstallOperation:
    return InstallOperation(
        operation_id=operation_id,
        layer=layer,
        kind=kind,
        target=str(target),
        precondition={"state": "absent-matching-or-managed"},
        desired={
            "content": content,
            "sha256": _sha256_bytes(content.encode("utf-8")),
            "mode": "0644",
        },
        inverse={"state": "receipt-snapshot"},
        privilege=privilege,
        owner=owner,
        provenance=provenance,
    )


def _role_runtime_references(manifest: Manifest, binding: PersonaBinding) -> frozenset[str]:
    try:
        raw = yaml.safe_load(manifest.path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raw = None
    runtime = raw.get("harness_runtime") if isinstance(raw, Mapping) else None
    roles = runtime.get("roles") if isinstance(runtime, Mapping) else None
    role = roles.get(binding.role) if isinstance(roles, Mapping) else None
    refs: set[str] = set()
    if isinstance(role, Mapping):
        fallbacks = role.get("fallbacks", [])
        if isinstance(fallbacks, list):
            refs.update(str(item) for item in fallbacks if isinstance(item, str))
    return frozenset(refs)


def _codex_bridge_required(
    manifest: Manifest,
    binding: PersonaBinding,
    *,
    selected_harness: str,
) -> bool:
    if manifest.installer.get("codex_telegram_bridge") is None:
        return False
    try:
        _source, personas = codex_telegram_bridge.parse_bridge_block(
            manifest.installer.get("codex_telegram_bridge")
        )
    except codex_telegram_bridge.BridgeError as exc:
        raise ManifestError(f"{manifest.path}: {exc}") from exc
    has_persona = binding.persona in personas
    has_codex = selected_harness == "codex" or any(
        reference.startswith("codex/") for reference in _role_runtime_references(manifest, binding)
    )
    return has_persona and has_codex


def _codex_standing_instructions_reference(
    manifest: Manifest,
    binding: PersonaBinding,
    *,
    root: Path = Path("/"),
) -> CodexStandingInstructionsReference | None:
    raw = manifest.installer.get("codex_standing_instructions")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or raw.get("schema") != "pantheon-codex-standing-instructions/v1":
        raise ManifestError(f"{manifest.path}: unsupported codex_standing_instructions schema")
    personas = raw.get("personas")
    persona_raw = personas.get(binding.persona) if isinstance(personas, Mapping) else None
    if persona_raw is None:
        return None
    if not isinstance(persona_raw, Mapping):
        raise ManifestError(f"{manifest.path}: codex_standing_instructions persona must be a mapping")
    source = _manifest_relative_path(persona_raw.get("source"), "codex_standing_instructions.source")
    target = _manifest_relative_path(persona_raw.get("target"), "codex_standing_instructions.target")
    override_target = _manifest_relative_path(
        persona_raw.get("override_target"),
        "codex_standing_instructions.override_target",
    )
    forbidden_raw = persona_raw.get("forbidden_source_paths", [])
    if not isinstance(forbidden_raw, list) or not all(isinstance(item, str) and item for item in forbidden_raw):
        raise ManifestError(f"{manifest.path}: forbidden_source_paths must be a string list")
    home = _rooted(root, Path("/home") / binding.account)
    string_fields = (
        "source_repo",
        "source_commit",
        "source_sha256",
        "identity",
        "canonical_source_of_truth",
    )
    for field_name in string_fields:
        if not isinstance(persona_raw.get(field_name), str) or not persona_raw.get(field_name):
            raise ManifestError(f"{manifest.path}: codex_standing_instructions.{field_name} missing")
    if persona_raw.get("source_sha256") and len(str(persona_raw["source_sha256"])) != 64:
        raise ManifestError(f"{manifest.path}: codex_standing_instructions.source_sha256 must be sha256")
    return CodexStandingInstructionsReference(
        persona=binding.persona,
        account=binding.account,
        source_repo=str(persona_raw["source_repo"]),
        source=source,
        source_commit=str(persona_raw["source_commit"]),
        source_sha256=str(persona_raw["source_sha256"]),
        target=home / target,
        override_target=home / override_target,
        override_must_be_absent=persona_raw.get("override_must_be_absent") is True,
        forbidden_source_paths=tuple(Path(item) for item in forbidden_raw),
        identity=str(persona_raw["identity"]),
        canonical_source_of_truth=str(persona_raw["canonical_source_of_truth"]),
    )


def codex_standing_instructions_reference(
    manifest: Manifest,
    persona: str,
    *,
    root: Path = Path("/"),
) -> CodexStandingInstructionsReference | None:
    """Resolve a persona's manifest-declared Codex instruction reference."""
    binding = manifest.binding(persona)
    return _codex_standing_instructions_reference(manifest, binding, root=root)


def _read_verified_standing_instructions(
    reference: CodexStandingInstructionsReference,
    *,
    repo_root: Path,
) -> str:
    source_path = (repo_root / reference.source).resolve()
    if any(source_path == forbidden.resolve() for forbidden in reference.forbidden_source_paths):
        raise ManifestError("codex standing instructions source path is forbidden")
    if not source_path.is_file() or not source_path.is_relative_to(repo_root.resolve()):
        raise ManifestError("codex standing instructions source is missing or escaped repo")
    content = source_path.read_bytes()
    if _sha256_bytes(content) != reference.source_sha256:
        raise ManifestError("codex standing instructions source hash mismatch")
    return content.decode("utf-8")


def _standing_instruction_operations(
    manifest: Manifest,
    binding: PersonaBinding,
    selected_harness: str,
    *,
    root: Path,
    provenance: Mapping[str, str],
    repo_root: Path,
) -> tuple[InstallOperation, ...]:
    del selected_harness
    reference = _codex_standing_instructions_reference(manifest, binding, root=root)
    if reference is None:
        return ()
    content = _read_verified_standing_instructions(reference, repo_root=repo_root)
    owner = binding.account
    source_target = str(repo_root / reference.source)
    base_provenance = dict(provenance) | {"required_capability": f"host.files:{owner}"}
    operations = [
        InstallOperation(
            operation_id="codex-standing-instructions.source-check",
            layer=2,
            kind="codex-standing-instructions-source-check",
            target=source_target,
            precondition={"state": "source-sha-must-match"},
            desired={
                "source_repo": reference.source_repo,
                "source": str(reference.source),
                "source_commit": reference.source_commit,
                "source_sha256": reference.source_sha256,
                "identity": reference.identity,
            },
            inverse=None,
            privilege="unprivileged",
            owner=owner,
            provenance=base_provenance,
        ),
        _content_operation(
            operation_id="codex-standing-instructions.install",
            layer=2,
            kind="codex-standing-instructions-install",
            target=reference.target,
            content=content,
            owner=owner,
            privilege="privileged-helper",
            provenance=base_provenance,
        ),
        InstallOperation(
            operation_id="codex-standing-instructions.target-check",
            layer=2,
            kind="codex-standing-instructions-target-check",
            target=str(reference.target),
            precondition={"after": "codex-standing-instructions.install"},
            desired={"sha256": reference.source_sha256},
            inverse=None,
            privilege="operator-required",
            owner=owner,
            provenance=base_provenance,
        ),
    ]
    if reference.override_must_be_absent:
        operations.append(
            InstallOperation(
                operation_id="codex-standing-instructions.override-check",
                layer=2,
                kind="codex-standing-instructions-override-check",
                target=str(reference.override_target),
                precondition={"after": "codex-standing-instructions.install"},
                desired={"state": "absent-or-empty"},
                inverse=None,
                privilege="operator-required",
                owner=owner,
                provenance=base_provenance,
            )
        )
    return tuple(operations)


class PersonaInstaller:
    def __init__(self, repo_root: Path = REPO_ROOT):
        self.repo_root = repo_root.resolve()

    def plan(
        self,
        manifest: Manifest,
        persona: str,
        harness_override: str | None = None,
        root: Path = Path("/"),
    ) -> InstallPlan:
        binding = manifest.binding(persona)
        selected_harness = _normalize_harness(
            harness_override or binding.configured_harness
        )
        root = root.resolve()
        config = manifest.installer
        paths = config.get("paths")
        mailbox = config.get("mailbox")
        systemd = config.get("systemd")
        policy = config.get("policy")
        lifecycle = config.get("lifecycle")
        if not all(isinstance(item, dict) for item in (paths, mailbox, systemd, policy, lifecycle)):
            raise ManifestError(f"{manifest.path}: installer sections missing")

        home = _rooted(root, Path("/home") / binding.account)
        repo_relative = _manifest_relative_path(
            paths.get("repo"),
            "persona_installer.paths.repo",
        )
        unit_relative = _manifest_relative_path(
            paths.get("unit_dir"),
            "persona_installer.paths.unit_dir",
        )
        state_relative = _manifest_relative_path(
            paths.get("state_dir"),
            "persona_installer.paths.state_dir",
        )
        unit_dir = home / unit_relative
        state_dir = home / state_relative
        persona_brief = _resolved_persona_brief_path(self.repo_root, home, binding)
        mailbox_path = mailbox.get("hub")
        if not isinstance(mailbox_path, str) or not Path(mailbox_path).is_absolute():
            raise ManifestError("persona_installer.mailbox.hub must be absolute")
        mailbox_hub = _rooted(root, Path(mailbox_path))
        probe_peer = str(mailbox.get("probe_peer", "orchestrator"))
        if probe_peer == binding.persona:
            probe_peer = "recovery"
        service_relative = _manifest_relative_path(
            systemd.get("service_template"),
            "persona_installer.systemd.service_template",
        )
        timer_relative = _manifest_relative_path(
            systemd.get("timer_template"),
            "persona_installer.systemd.timer_template",
        )
        service_template = self.repo_root / service_relative
        timer_template = self.repo_root / timer_relative
        for source in (service_template, timer_template):
            if not source.is_file():
                raise ManifestError(f"framework template missing: {source}")
        quota_config = _quota_check_install_config(manifest, repo_root=self.repo_root)
        policy_bundle = tuple(
            _manifest_relative_path(item, "persona_installer.policy.bundle[]")
            for item in policy.get("bundle", ())
        )
        if not policy_bundle:
            raise ManifestError("persona_installer.policy.bundle must not be empty")

        base_provenance = _provenance(manifest, binding, selected_harness)
        artifacts = (
            Path("tools/persona_installer.py"),
            service_relative,
            timer_relative,
            quota_config.service_template,
            quota_config.timer_template,
        )
        base_provenance = dict(base_provenance) | {
            "declared_revision": base_provenance["source_revision"],
            "source_revision": _git_head(self.repo_root),
            "source_artifact_sha256": _artifact_digest(
                self.repo_root,
                artifacts,
            ),
        }
        unit_banner = (
            f"# {MANAGED_MARKER}\n"
            f"# instance={manifest.instance_id} role={binding.role} "
            f"persona={binding.persona} harness={selected_harness} "
            f"revision={base_provenance['source_revision']}\n"
        )
        service_content = unit_banner + service_template.read_text(encoding="utf-8")
        timer_content = unit_banner + timer_template.read_text(encoding="utf-8")
        quota_service_content = unit_banner + (
            self.repo_root / quota_config.service_template
        ).read_text(encoding="utf-8")
        quota_timer_content = unit_banner + _render_quota_timer_template(
            (self.repo_root / quota_config.timer_template).read_text(encoding="utf-8"),
            cadence_minutes=quota_config.cadence_minutes,
            randomized_delay_seconds=quota_config.randomized_delay_seconds,
        )
        binding_content = json.dumps(
            {
                "managed_by": "pantheon-persona-installer",
                "schema": "pantheon-persona-binding/v1",
                "instance": manifest.instance_id,
                "role": binding.role,
                "persona": binding.persona,
                "account": binding.account,
                "configured_harness": binding.configured_harness,
                "selected_harness": selected_harness,
                "credential_refs": [
                    {
                        "id": ref.reference_id,
                        "kind": ref.kind,
                        "provider": ref.provider,
                        "owner": ref.owner,
                        "scope": list(ref.scope),
                        "location": ref.location,
                        "rotation": {
                            "strategy": ref.rotation_strategy,
                            "revoke": ref.revocation_plan,
                        },
                        "direct_use_justification": ref.direct_use_justification,
                    }
                    for ref in binding.credential_refs
                ],
                "provenance": base_provenance,
            },
            sort_keys=True,
            indent=2,
        ) + "\n"

        identity_cap = f"host.identity:{binding.account}"
        fs_cap = f"host.files:{binding.account}"
        manager_cap = f"systemd.user:{binding.account}"
        mailbox_cap = f"mailbox.probe:{binding.account}"

        def op(
            operation_id: str,
            layer: int,
            kind: OperationKind,
            target: str,
            desired: Mapping[str, str],
            privilege: PrivilegeRequirement,
            capability: str = "",
            inverse: Mapping[str, str] | None = None,
            execution_scope: ExecutionScope = "all",
        ) -> InstallOperation:
            return InstallOperation(
                operation_id=operation_id,
                layer=layer,
                kind=kind,
                target=target,
                precondition={"state": "must-pass"},
                desired=desired,
                inverse=inverse,
                privilege=privilege,
                owner=binding.account,
                provenance=_provenance(
                    manifest,
                    binding,
                    selected_harness,
                    capability,
                ),
                execution_scope=execution_scope,
            )

        consume_timer = f"pantheon-persona-consume@{binding.persona}.timer"
        consume_timer_link = _consume_timer_enablement_link_path(home, consume_timer)
        consume_timer_template = _consume_timer_template_path(home, consume_timer)
        operations: list[InstallOperation] = [
            op(
                "connective.timer-enablement-absent",
                0,
                "timer-enablement-absence-check",
                str(consume_timer_link),
                {
                    "timer": consume_timer,
                    "canonical_template_path": str(consume_timer_template),
                    "state": "absent",
                },
                "operator-required",
                manager_cap,
                execution_scope="staged-cutover",
            ),
            op(
                "host.identity",
                0,
                "identity-check",
                binding.account,
                {
                    "account": binding.account,
                    "mailbox_group": str(mailbox.get("group")),
                    "linger": "enabled",
                    "user_manager": "active",
                },
                "operator-required",
                identity_cap,
            ),
            op(
                "framework.source",
                1,
                "source-check",
                str(self.repo_root),
                {
                    "declared_path": str(repo_relative),
                    "revision": base_provenance["source_revision"],
                    "artifact_sha256": base_provenance[
                        "source_artifact_sha256"
                    ],
                    "artifacts": ",".join(str(item) for item in artifacts),
                },
                "unprivileged",
            ),
            op(
                "harness.command",
                2,
                "harness-check",
                selected_harness,
                {"harness": selected_harness},
                "unprivileged",
            ),
            op(
                "persona.brief",
                3,
                "brief-check",
                str(persona_brief),
                {
                    "persona": binding.persona,
                    "source": "account-home-ref" if binding.persona_brief_ref else "repo-default",
                },
                "unprivileged",
            ),
            op(
                "policy.bundle",
                4,
                "policy-check",
                str(self.repo_root),
                {
                    "bundle": ",".join(
                        str(item) for item in policy_bundle
                    )
                },
                "unprivileged",
            ),
        ]

        directory_specs = (
            ("connective.config-dir", home / ".config", "0755"),
            ("connective.systemd-dir", home / ".config" / "systemd", "0755"),
            ("connective.unit-dir", unit_dir, "0755"),
            ("connective.local-dir", home / ".local", "0755"),
            ("connective.local-state-dir", home / ".local" / "state", "0755"),
            (
                "connective.pantheon-state-dir",
                home / ".local" / "state" / "pantheon",
                "0700",
            ),
            ("connective.installer-state-dir", state_dir, "0700"),
            ("connective.state-dir", state_dir / binding.persona, "0700"),
            (
                "connective.receipt-dir",
                state_dir / binding.persona / "receipts",
                "0700",
            ),
        )
        seen_directories: set[str] = set()
        for operation_id, directory, mode in directory_specs:
            target = str(directory)
            if target in seen_directories:
                continue
            seen_directories.add(target)
            operations.append(
                op(
                    operation_id,
                    1,
                    "mkdir",
                    target,
                    {"mode": mode},
                    "privileged-helper",
                    fs_cap,
                    {"state": "remove-if-created-and-empty"},
                )
            )

        mailbox_root_seen: set[str] = set()
        manifest_document = _manifest_document(manifest)
        for candidate_harness in _runtime_candidate_harnesses_for_install(
            manifest, binding, selected_harness
        ):
            try:
                writable_roots = resolve_mailbox_writable_roots(
                    manifest_document,
                    role=binding.role,
                    harness=candidate_harness,
                    bound_persona=binding.persona,
                    bound_mailbox=mailbox_hub / binding.persona,
                    mailbox_hub=mailbox_hub,
                    root=Path("/"),
                )
            except MailboxWritableRootError as exc:
                raise ManifestError(f"{manifest.path}: {exc}") from exc
            for root_index, writable_root in enumerate(writable_roots):
                root_target = str(writable_root.path)
                if root_target in mailbox_root_seen:
                    raise ManifestError(
                        f"{manifest.path}: duplicate mailbox writable root target {root_target}"
                    )
                mailbox_root_seen.add(root_target)
                operations.append(
                    op(
                        f"mailbox-writable-root.{candidate_harness}.{root_index}",
                        1,
                        "mailbox-writable-root-check",
                        root_target,
                        {
                            "harness": candidate_harness,
                            "ref_kind": writable_root.ref.kind,
                            "ref_persona": writable_root.ref.persona or binding.persona,
                            "mailbox_hub": str(mailbox_hub),
                            "maildir_subdirs": "tmp,new,cur",
                            "transient_probe": "o-excl-create-unlink",
                        },
                        "operator-required",
                        mailbox_cap,
                    )
                )

        bridge_binding = None
        if _codex_bridge_required(manifest, binding, selected_harness=selected_harness):
            bridge_binding = manifest.codex_telegram_binding(binding.persona, root=root)
            bridge_cap = f"codex-telegram.bridge:{binding.account}"
            archive_path = _rooted(
                root,
                Path("/tmp")
                / f"codex-telegram-{binding.persona}-{bridge_binding.source.revision}.tar",
            )
            bridge_dirs = (
                ("codex-telegram.state-dir", bridge_binding.state_dir, "0700"),
                ("codex-telegram.release-root", bridge_binding.release_root, "0755"),
            )
            for operation_id, directory, mode in bridge_dirs:
                operations.append(
                    op(
                        operation_id,
                        1,
                        "mkdir",
                        str(directory),
                        {"mode": mode},
                        "privileged-helper",
                        fs_cap,
                        {"state": "remove-if-created-and-empty"},
                    )
                )
            allowlist_content = codex_telegram_bridge.render_allowlist()
            operations.extend(
                [
                    op(
                        "codex-telegram.source-archive",
                        1,
                        "codex-telegram-source-check",
                        str(bridge_binding.current_release),
                        {
                            "revision": bridge_binding.source.revision,
                            "paths": json.dumps(list(bridge_binding.source.paths), sort_keys=True),
                            "sha256": json.dumps(dict(bridge_binding.source.sha256), sort_keys=True),
                            "archive_path": str(archive_path),
                            "archive_requires_head": bridge_binding.source.revision,
                            "account_home": str(bridge_binding.account_home),
                            "credential_env": str(bridge_binding.credential_env),
                            "state_dir": str(bridge_binding.state_dir),
                            "release_root": str(bridge_binding.release_root),
                            "claude_receiver_topology": bridge_binding.claude_receiver_topology,
                            "predecessor_claude_receiver_topology": bridge_binding.predecessor_claude_receiver_topology,
                            "claude_receiver_unit": bridge_binding.claude_receiver_unit,
                            "codex_receiver_unit": bridge_binding.codex_receiver_unit,
                            "tmux_session": bridge_binding.tmux_session,
                            "tmux_pane": bridge_binding.tmux_pane,
                            "effective_pane": bridge_binding.effective_pane,
                            "watchdog_units": json.dumps(list(bridge_binding.watchdog_units), sort_keys=True),
                            "mcp_name": bridge_binding.mcp_name,
                            "unattended_telegram_tools": str(bridge_binding.unattended_telegram_tools).lower(),
                            "source_account": bridge_binding.source.owner_account or "",
                            "source_root": str(bridge_binding.source.source_root or ""),
                        },
                        "operator-required",
                        bridge_cap,
                    ),
                    InstallOperation(
                        operation_id="codex-telegram.allowlist",
                        layer=1,
                        kind="atomic-write",
                        target=str(bridge_binding.state_dir / "allowlist.json"),
                        precondition={"state": "absent-matching-or-managed"},
                        desired={
                            "content": allowlist_content,
                            "sha256": _sha256_bytes(allowlist_content.encode("utf-8")),
                            "mode": "0600",
                        },
                        inverse={"state": "receipt-snapshot"},
                        privilege="privileged-helper",
                        owner=binding.account,
                        provenance=_provenance(manifest, binding, selected_harness, fs_cap),
                    ),
                    _content_operation(
                        operation_id="codex-telegram.unit-install",
                        layer=1,
                        kind="user-unit-install",
                        target=unit_dir / bridge_binding.codex_receiver_unit,
                        content=unit_banner + codex_telegram_bridge.render_unit(bridge_binding),
                        owner=binding.account,
                        privilege="privileged-helper",
                        provenance=_provenance(manifest, binding, selected_harness, fs_cap),
                    ),
                    op(
                        "codex-telegram.mcp-registration",
                        2,
                        "codex-telegram-mcp-check",
                        str(bridge_binding.account_home / ".codex" / "config.toml"),
                        {
                            "name": bridge_binding.mcp_name,
                            "expected": codex_telegram_bridge.expected_mcp_snippet(bridge_binding),
                            "create_if_absent": "true",
                            "fail_on_mismatch": "true",
                        },
                        "operator-required",
                        bridge_cap,
                    ),
                    op(
                        "codex-telegram.disabled-first",
                        1,
                        "codex-telegram-disabled-first",
                        bridge_binding.codex_receiver_unit,
                        {"active": "inactive", "enabled": "disabled", "environment_file": "forbidden"},
                        "operator-required",
                        manager_cap,
                    ),
                ]
            )

        claude_mcp_block = manifest.claude_mcp_servers_for(
            binding.persona, account=binding.account
        )
        if claude_mcp_block:
            claude_mcp_cap = f"claude.mcp:{binding.account}"
            claude_config_path = home / ".claude.json"
            for server_name, server_cfg in sorted(
                claude_mcp_block["servers"].items()
            ):
                expected = _claude_mcp_expected(server_cfg)
                desired = {
                    "name": server_name,
                    "scope": claude_mcp_block["scope"],
                    "account": binding.account,
                    "home": str(home),
                    "config_path": str(claude_config_path),
                    "expected": expected,
                    # Exact JSON handed to `claude mcp add-json --scope user`;
                    # identical to the canonical projection used for compare.
                    "config_json": expected,
                }
                operations.append(
                    op(
                        f"claude-mcp.check.{server_name}",
                        2,
                        "claude-mcp-server-check",
                        str(claude_config_path),
                        dict(desired),
                        "operator-required",
                        claude_mcp_cap,
                    )
                )
                operations.append(
                    op(
                        f"claude-mcp.register.{server_name}",
                        3,
                        "claude-mcp-server-registration",
                        str(claude_config_path),
                        dict(desired),
                        "operator-required",
                        claude_mcp_cap,
                    )
                )

        operations.extend(
            _standing_instruction_operations(
                manifest,
                binding,
                selected_harness,
                root=root,
                provenance=_provenance(manifest, binding, selected_harness, fs_cap),
                repo_root=self.repo_root,
            )
        )

        operations.extend(
            [
                _content_operation(
                    operation_id="persona.binding",
                    layer=3,
                    kind="atomic-write",
                    target=state_dir / binding.persona / "binding.json",
                    content=binding_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="connective.service-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-consume@.service",
                    content=service_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="connective.timer-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-consume@.timer",
                    content=timer_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="quota.service-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-quota@.service",
                    content=quota_service_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                _content_operation(
                    operation_id="quota.timer-unit",
                    layer=1,
                    kind="user-unit-install",
                    target=unit_dir / "pantheon-persona-quota@.timer",
                    content=quota_timer_content,
                    owner=binding.account,
                    privilege="privileged-helper",
                    provenance=_provenance(
                        manifest, binding, selected_harness, fs_cap
                    ),
                ),
                op(
                    "host.user-manager",
                    0,
                    "manager-check",
                    binding.account,
                    {"manager_uid": binding.account},
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.daemon-reload",
                    1,
                    "daemon-reload",
                    binding.account,
                    {"timer_state": "disabled"},
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "harness.direct-fire",
                    2,
                    "direct-fire",
                    binding.persona,
                    {
                        "harness": selected_harness,
                        "manifest": str(manifest.path),
                        "repo": str(self.repo_root),
                    },
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.unit-fire",
                    1,
                    "unit-fire",
                    f"pantheon-persona-consume@{binding.persona}.service",
                    {
                        "timer": f"pantheon-persona-consume@{binding.persona}.timer",
                        "timer_state": "disabled",
                        "result": "success",
                        "exec_main_status": "0",
                    }
                    | _unit_fire_timeout_desired(manifest, binding, selected_harness)
                    | _unit_fire_initial_viability_desired(
                        manifest,
                        binding,
                        selected_harness,
                        account_home=home,
                        bridge_binding=bridge_binding,
                    ),
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.mailbox-probe",
                    1,
                    "mailbox-probe",
                    str(mailbox_hub / binding.persona),
                    {
                        "receive_tmp": str(mailbox_hub / binding.persona / "tmp"),
                        "send_tmp": str(mailbox_hub / probe_peer / "tmp"),
                        "receive": "required",
                        "send": "required",
                        "durable_round_trip": "required",
                    },
                    "operator-required",
                    mailbox_cap,
                ),
                op(
                    "connective.timer-enable",
                    1,
                    "timer-enable",
                    consume_timer,
                    {
                        "after": "direct-fire,unit-fire,mailbox-probe",
                        "enablement_link_path": str(consume_timer_link),
                        "canonical_template_path": str(consume_timer_template),
                    },
                    "operator-required",
                    manager_cap,
                ),
                op(
                    "connective.timer-observe",
                    1,
                    "timer-observe",
                    f"pantheon-persona-consume@{binding.persona}.timer",
                    {
                        "service": (
                            f"pantheon-persona-consume@{binding.persona}.service"
                        ),
                        "next_elapse": "finite-after-inactive",
                        "unit_fire_invocation": "same-service-invocation",
                        "second_consume": "absent",
                        "mailbox_effect": "correlated",
                    },
                    "operator-required",
                    manager_cap,
                ),
            ]
        )
        if bridge_binding is not None:
            bridge_reload = op(
                "codex-telegram.daemon-reload",
                1,
                "daemon-reload",
                binding.account,
                {"unit": bridge_binding.codex_receiver_unit, "start": "forbidden", "enable": "forbidden"},
                "operator-required",
                manager_cap,
            )
            target_operation_id = (
                "codex-telegram.disabled-first"
                if selected_harness == "codex"
                else "connective.daemon-reload"
            )
            for index, operation in enumerate(operations):
                if operation.operation_id == target_operation_id:
                    operations.insert(index, bridge_reload)
                    break
            else:
                operations.append(bridge_reload)
        stale_reset = op(
            "connective.stale-unit-failure-reset",
            1,
            "stale-unit-failure-reset",
            f"pantheon-persona-consume@{binding.persona}.service",
            {
                "load_state": "not-found",
                "active_state": "inactive",
                "sub_state": "dead",
                "result": "success",
                "exec_main_status": "0",
            },
            "operator-required",
            manager_cap,
        )
        activation_insert_before = {
            "codex-telegram.daemon-reload",
            "connective.service-unit",
        }
        for index, operation in enumerate(operations):
            if operation.operation_id in activation_insert_before:
                operations.insert(index, stale_reset)
                break
        else:
            operations.append(stale_reset)
        operations = [
            dataclasses.replace(
                operation,
                provenance=(
                    dict(operation.provenance)
                    | base_provenance
                    | {"install_root": str(root)}
                ),
            )
            for operation in operations
        ]

        actions = [
            OperatorAction(
                action_id="operator.host-identity",
                layer=0,
                target=binding.account,
                reason=(
                    "create/verify the Unix account, linger, user manager, and "
                    "pantheon-mailbox membership without a generic root shell"
                ),
                required_capability=identity_cap,
            ),
            OperatorAction(
                action_id="operator.user-systemd",
                layer=1,
                target=binding.account,
                reason=(
                    "enter the target login/user-manager context and supervise "
                    "disabled first fire before timer activation"
                ),
                required_capability=manager_cap,
            ),
            OperatorAction(
                action_id="operator.mailbox-acl",
                layer=1,
                target=str(mailbox_hub),
                reason="grant and verify both receive and sibling-send ACL directions",
                required_capability=mailbox_cap,
            ),
        ]
        for ref in binding.credential_refs:
            actions.append(
                OperatorAction(
                    action_id=f"operator.credential.{ref.reference_id}",
                    layer=2,
                    target=ref.reference_id,
                    reason=(
                        "resolve provider metadata and verify scope/rotation "
                        "without reading or copying a secret value"
                    ),
                    required_capability=f"credential.check:{ref.reference_id}",
                )
            )

        return InstallPlan.create(
            schema=PLAN_SCHEMA,
            manifest_version=manifest.version,
            instance=manifest.instance_id,
            role=binding.role,
            persona=binding.persona,
            account=binding.account,
            configured_harness=binding.configured_harness,
            selected_harness=selected_harness,
            root=str(root),
            operations=tuple(operations),
            operator_actions=tuple(actions),
            provenance=base_provenance
            | {
                "installer": "tools/persona_installer.py",
                "receipt_dir": str(state_dir / binding.persona / "receipts"),
            },
        )

    def apply(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
    ) -> ApplyReport:
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all"}),
            )
        except PlanRefused as exc:
            return ApplyReport("refused", 0, None, (str(exc),))

        mutations: list[MutationRecord] = []
        evidence: list[OperationEvidence] = []
        receipt_id = (
            f"{int(time.time())}-{plan.digest[:16]}-{uuid.uuid4().hex[:8]}"
        )
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        staged = False
        pending_intents: tuple[MutationRecord, ...] = ()

        def make_receipt(
            phase: Literal["staged", "complete", "failed"],
            receipt_mutations: Sequence[MutationRecord],
            receipt_evidence: Sequence[OperationEvidence],
        ) -> ApplyReceipt:
            return ApplyReceipt(
                schema=RECEIPT_SCHEMA,
                receipt_id=receipt_id,
                phase=phase,
                plan_digest=plan.digest,
                instance=plan.instance,
                persona=plan.persona,
                role=plan.role,
                account=plan.account,
                actor={
                    "uid": str(actor.uid),
                    "persona": actor.persona,
                    "role": actor.role,
                    "instance": actor.instance,
                    "capabilities": ",".join(sorted(actor.capabilities)),
                },
                mutations=tuple(receipt_mutations),
                evidence=tuple(receipt_evidence),
                created_at=created_at,
            )

        current_operation: InstallOperation | None = None
        try:
            for operation in plan.operations:
                if not _operation_in_scope(operation, frozenset({"all"})):
                    continue
                current_operation = operation
                if not staged and operation.kind in ACTIVATION_OPERATION_KINDS:
                    pending_intents = tuple(
                        intent
                        for planned in plan.operations
                        if planned.kind in ACTIVATION_OPERATION_KINDS
                        for intent in (backend.rollback_intent(planned),)
                        if intent is not None
                    )
                    staged_receipt = make_receipt(
                        "staged",
                        tuple(mutations) + pending_intents,
                        evidence,
                    )
                    backend.store_receipt(plan, staged_receipt)
                    staged = True
                mutation, observed = backend.execute(
                    operation,
                    EXECUTION_CONTEXT_ALL_IN_ONE,
                )
                mutations.append(mutation)
                evidence.append(observed)
                if not observed.ok:
                    raise OperationFailed(
                        f"{operation.operation_id}: {observed.detail}"
                    )
            receipt = make_receipt("complete", mutations, evidence)
            backend.store_receipt(plan, receipt)
        except Exception as exc:
            if isinstance(exc, OperationFailed) and exc.evidence and current_operation is not None:
                evidence.append(
                    OperationEvidence(
                        current_operation.operation_id,
                        current_operation.layer,
                        current_operation.kind,
                        False,
                        str(exc),
                        exc.evidence,
                    )
                )
            returned_ids = {item.operation_id for item in mutations}
            failure_intents: tuple[MutationRecord, ...] = ()
            if (
                current_operation is not None
                and current_operation.kind == "timer-enable"
                and current_operation.operation_id not in returned_ids
                and isinstance(exc, OperationFailed)
                and exc.evidence.get("enable_command_issued") == "true"
            ):
                try:
                    intent = backend.issued_timer_enable_recovery_mutation(
                        current_operation,
                        exc.evidence,
                    )
                    if intent is not None and intent.changed:
                        failure_intents = (intent,)
                except Exception:
                    failure_intents = ()
            returned_or_failed_ids = returned_ids | {item.operation_id for item in failure_intents}
            effective_mutations = tuple(mutations) + failure_intents + tuple(
                item
                for item in pending_intents
                if item.operation_id not in returned_or_failed_ids
            )
            rollback_notes: list[str] = []
            for mutation in reversed(
                [item for item in effective_mutations if item.changed]
            ):
                try:
                    check = backend.can_rollback(mutation)
                    if not check.ok:
                        rollback_notes.append(
                            f"{mutation.operation_id}: rollback refused: {check.detail}"
                        )
                        continue
                    backend.rollback_mutation(mutation)
                    rollback_notes.append(f"{mutation.operation_id}: restored")
                except Exception as rollback_exc:
                    rollback_notes.append(
                        f"{mutation.operation_id}: rollback failed: {rollback_exc}"
                    )
            failed_receipt = (
                make_receipt("failed", effective_mutations, evidence)
                if staged
                else None
            )
            if failed_receipt is not None:
                try:
                    backend.store_receipt(plan, failed_receipt)
                except Exception as receipt_exc:
                    rollback_notes.append(
                        f"failed receipt persistence: {receipt_exc}"
                    )
            return ApplyReport(
                "failed",
                sum(item.changed for item in effective_mutations),
                failed_receipt,
                (f"{type(exc).__name__}: {exc}",),
                tuple(rollback_notes),
            )
        changed = sum(item.changed for item in mutations)
        return ApplyReport("applied" if changed else "noop", changed, receipt)

    def _make_apply_receipt(
        self,
        plan: InstallPlan,
        actor: ActorContext,
        *,
        receipt_id: str,
        phase: Literal["staged", "complete", "failed"],
        mutations: Sequence[MutationRecord],
        evidence: Sequence[OperationEvidence],
        created_at: str | None = None,
    ) -> ApplyReceipt:
        return ApplyReceipt(
            schema=RECEIPT_SCHEMA,
            receipt_id=receipt_id,
            phase=phase,
            plan_digest=plan.digest,
            instance=plan.instance,
            persona=plan.persona,
            role=plan.role,
            account=plan.account,
            actor={
                "uid": str(actor.uid),
                "persona": actor.persona,
                "role": actor.role,
                "instance": actor.instance,
                "capabilities": ",".join(sorted(actor.capabilities)),
            },
            mutations=tuple(mutations),
            evidence=tuple(evidence),
            created_at=created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def _activation_index(self, plan: InstallPlan) -> int:
        for index, operation in enumerate(plan.operations):
            if operation.kind in ACTIVATION_OPERATION_KINDS:
                return index
        return len(plan.operations)

    def _activation_rollback_intents(
        self,
        plan: InstallPlan,
        backend: HostBackend,
    ) -> tuple[MutationRecord, ...]:
        return tuple(
            intent
            for operation in plan.operations[self._activation_index(plan) :]
            if operation.kind in ACTIVATION_OPERATION_KINDS
            for intent in (backend.rollback_intent(operation),)
            if intent is not None
        )

    def _staged_prefix_operations(self, plan: InstallPlan) -> tuple[InstallOperation, ...]:
        return tuple(plan.operations[: self._activation_index(plan)])

    def _validated_staged_mutations(
        self,
        plan: InstallPlan,
        receipt: ApplyReceipt,
        backend: HostBackend,
    ) -> tuple[MutationRecord, ...]:
        prefix_operations = self._staged_prefix_operations(plan)
        prefix_ids = {operation.operation_id for operation in prefix_operations}
        mutations_by_id = {mutation.operation_id: mutation for mutation in receipt.mutations}
        return tuple(
            mutations_by_id[operation.operation_id]
            for operation in prefix_operations
        )

    def stage_disabled(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
        *,
        receipt_id: str | None = None,
    ) -> ApplyReport:
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all", "staged-cutover"}),
            )
        except PlanRefused as exc:
            return ApplyReport("refused", 0, None, (str(exc),))
        activation_index = self._activation_index(plan)
        mutations: list[MutationRecord] = []
        evidence: list[OperationEvidence] = []
        receipt_id = receipt_id or f"{int(time.time())}-{plan.digest[:16]}-{uuid.uuid4().hex[:8]}"
        try:
            for operation in plan.operations[:activation_index]:
                if not _operation_in_scope(operation, frozenset({"all", "staged-cutover"})):
                    continue
                if operation.kind in ACTIVATION_OPERATION_KINDS:
                    raise OperationFailed(f"{operation.operation_id}: activation operation in stage prefix")
                mutation, observed = backend.execute(
                    operation,
                    EXECUTION_CONTEXT_STAGE_PREFIX,
                )
                mutations.append(mutation)
                evidence.append(observed)
                if not observed.ok:
                    raise OperationFailed(f"{operation.operation_id}: {observed.detail}")
            pending_intents = self._activation_rollback_intents(plan, backend)
            receipt = self._make_apply_receipt(
                plan,
                actor,
                receipt_id=receipt_id,
                phase="staged",
                mutations=tuple(mutations) + pending_intents,
                evidence=evidence,
            )
            backend.store_receipt(plan, receipt)
            return ApplyReport("applied" if any(item.changed for item in mutations) else "noop", sum(item.changed for item in mutations), receipt)
        except Exception as exc:
            rollback_notes: list[str] = []
            for mutation in reversed([item for item in mutations if item.changed]):
                try:
                    check = backend.can_rollback(mutation)
                    if not check.ok:
                        rollback_notes.append(f"{mutation.operation_id}: rollback refused: {check.detail}")
                        continue
                    backend.rollback_mutation(mutation)
                    rollback_notes.append(f"{mutation.operation_id}: restored")
                except Exception as rollback_exc:
                    rollback_notes.append(f"{mutation.operation_id}: rollback failed: {rollback_exc}")
            return ApplyReport("failed", sum(item.changed for item in mutations), None, (f"{type(exc).__name__}: {exc}",), tuple(rollback_notes))

    def _validate_staged_receipt(
        self,
        plan: InstallPlan,
        receipt: ApplyReceipt,
        backend: HostBackend,
        actor: ActorContext,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        if receipt.schema != RECEIPT_SCHEMA:
            errors.append("unsupported receipt schema")
        if receipt.phase != "staged":
            errors.append("receipt phase is not staged")
        if receipt.plan_digest != plan.digest:
            errors.append("receipt plan digest mismatch")
        for field in ("instance", "persona", "role", "account"):
            if getattr(receipt, field) != getattr(plan, field):
                errors.append(f"receipt {field} mismatch")
        expected_actor = {
            "uid": str(actor.uid),
            "persona": actor.persona,
            "role": actor.role,
            "instance": actor.instance,
            "capabilities": ",".join(sorted(actor.capabilities)),
        }
        if dict(receipt.actor) != expected_actor:
            errors.append("receipt actor mismatch")
        prefix_operations = self._staged_prefix_operations(plan)
        prefix_ids = {operation.operation_id for operation in prefix_operations}
        expected_intents = self._activation_rollback_intents(plan, backend)
        mutation_counts: dict[str, int] = {}
        for mutation in receipt.mutations:
            mutation_counts[mutation.operation_id] = mutation_counts.get(mutation.operation_id, 0) + 1
        evidence_counts: dict[str, int] = {}
        for item in receipt.evidence:
            evidence_counts[item.operation_id] = evidence_counts.get(item.operation_id, 0) + 1
        mutations_by_id = {mutation.operation_id: mutation for mutation in receipt.mutations}
        evidence_by_id = {item.operation_id: item for item in receipt.evidence}
        for operation in prefix_operations:
            operation_id = operation.operation_id
            if mutation_counts.get(operation_id, 0) != 1:
                errors.append(f"{operation_id}: staged mutation missing or duplicated")
                continue
            mutation = mutations_by_id[operation_id]
            if mutation.kind != operation.kind:
                errors.append(f"{operation_id}: staged mutation kind mismatch")
            if mutation.target != operation.target:
                errors.append(f"{operation_id}: staged mutation target mismatch")
            if mutation.owner != operation.owner:
                errors.append(f"{operation_id}: staged mutation owner mismatch")
            if evidence_counts.get(operation_id, 0) != 1:
                errors.append(f"{operation_id}: staged evidence missing or duplicated")
            else:
                observed = evidence_by_id[operation_id]
                if observed.kind != operation.kind:
                    errors.append(f"{operation_id}: staged evidence kind mismatch")
                if not observed.ok:
                    errors.append(f"{operation_id}: staged evidence is not ok")
            current = backend.verify_operation(
                operation,
                staged_evidence=evidence_by_id.get(operation_id),
            )
            if not current.ok:
                errors.append(f"{operation_id}: staged invariant failed: {current.detail}")
        expected_intent_ids = {intent.operation_id for intent in expected_intents}
        unexpected_evidence = sorted(set(evidence_counts) - prefix_ids)
        if unexpected_evidence:
            errors.append(f"unexpected staged evidence ids: {unexpected_evidence}")
        receipt_intents = tuple(
            mutation
            for mutation in receipt.mutations
            if mutation.operation_id not in prefix_ids
        )
        unexpected_mutation_ids = sorted(
            set(mutation_counts) - prefix_ids - expected_intent_ids
        )
        if unexpected_mutation_ids:
            errors.append(f"unexpected staged mutation ids: {unexpected_mutation_ids}")
        if receipt_intents != expected_intents:
            errors.append("activation rollback intents mismatch")
        for mutation in receipt.mutations:
            if mutation.operation_id not in prefix_ids:
                continue
            if mutation.before.get("state") == "not-applicable":
                continue
            check = backend.can_rollback(mutation)
            if not check.ok:
                errors.append(f"{mutation.operation_id}: staged target invalid: {check.detail}")
            elif mutation.changed and check.classification == "not-applied":
                errors.append(f"{mutation.operation_id}: staged target no longer applied")
        return tuple(errors)

    def activate_staged(
        self,
        plan: InstallPlan,
        approved_digest: str,
        staged_receipt: ApplyReceipt,
        backend: HostBackend,
        actor: ActorContext,
    ) -> ApplyReport:
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all", "staged-cutover"}),
            )
        except PlanRefused as exc:
            return ApplyReport("refused", 0, None, (str(exc),))
        validation_errors = self._validate_staged_receipt(plan, staged_receipt, backend, actor)
        if validation_errors:
            return ApplyReport("refused", 0, None, validation_errors)
        staged_mutations = self._validated_staged_mutations(plan, staged_receipt, backend)
        mutations: list[MutationRecord] = []
        evidence: list[OperationEvidence] = []
        activation_index = self._activation_index(plan)
        receipt_id = staged_receipt.receipt_id
        current_operation: InstallOperation | None = None
        try:
            for operation in plan.operations[activation_index:]:
                current_operation = operation
                mutation, observed = backend.execute(
                    operation,
                    EXECUTION_CONTEXT_STAGED_ACTIVATE,
                )
                mutations.append(mutation)
                evidence.append(observed)
                if not observed.ok:
                    raise OperationFailed(f"{operation.operation_id}: {observed.detail}")
            combined_mutations = staged_mutations + tuple(
                item
                for item in mutations
                if item.operation_id not in {prior.operation_id for prior in staged_mutations}
            )
            receipt = self._make_apply_receipt(
                plan,
                actor,
                receipt_id=receipt_id,
                phase="complete",
                mutations=combined_mutations,
                evidence=tuple(staged_receipt.evidence) + tuple(evidence),
                created_at=staged_receipt.created_at,
            )
            backend.store_receipt(plan, receipt)
            return ApplyReport("applied" if any(item.changed for item in mutations) else "noop", sum(item.changed for item in combined_mutations), receipt)
        except Exception as exc:
            if isinstance(exc, OperationFailed) and exc.evidence and current_operation is not None:
                evidence.append(
                    OperationEvidence(
                        current_operation.operation_id,
                        current_operation.layer,
                        current_operation.kind,
                        False,
                        str(exc),
                        exc.evidence,
                    )
                )
            staged_ids = {item.operation_id for item in staged_mutations}
            failure_intents: tuple[MutationRecord, ...] = ()
            executed_ids = {item.operation_id for item in mutations}
            if (
                current_operation is not None
                and current_operation.kind == "timer-enable"
                and current_operation.operation_id not in executed_ids
                and isinstance(exc, OperationFailed)
                and exc.evidence.get("enable_command_issued") == "true"
            ):
                try:
                    intent = backend.issued_timer_enable_recovery_mutation(
                        current_operation,
                        exc.evidence,
                    )
                    if intent is not None and intent.changed:
                        failure_intents = (intent,)
                except Exception:
                    failure_intents = ()
            combined_mutations = staged_mutations + tuple(
                item
                for item in mutations
                if item.operation_id not in staged_ids
            ) + tuple(
                item
                for item in failure_intents
                if item.operation_id not in staged_ids
            )
            executed_activation_ids = executed_ids | {item.operation_id for item in failure_intents}
            staged_prefix_ids = {
                operation.operation_id
                for operation in self._staged_prefix_operations(plan)
            }
            rollback_notes: list[str] = []
            rollback_candidates = [
                item
                for item in combined_mutations
                if item.changed
                and (
                    item.operation_id in staged_prefix_ids
                    or item.operation_id in executed_activation_ids
                )
            ]
            for mutation in reversed(rollback_candidates):
                try:
                    check = backend.can_rollback(mutation)
                    if not check.ok:
                        rollback_notes.append(f"{mutation.operation_id}: rollback refused: {check.detail}")
                        continue
                    backend.rollback_mutation(mutation)
                    rollback_notes.append(f"{mutation.operation_id}: restored")
                except Exception as rollback_exc:
                    rollback_notes.append(f"{mutation.operation_id}: rollback failed: {rollback_exc}")
            failed_receipt = self._make_apply_receipt(
                plan,
                actor,
                receipt_id=receipt_id,
                phase="failed",
                mutations=combined_mutations,
                evidence=tuple(staged_receipt.evidence) + tuple(evidence),
                created_at=staged_receipt.created_at,
            )
            try:
                backend.store_receipt(plan, failed_receipt)
            except Exception as receipt_exc:
                rollback_notes.append(f"failed receipt persistence: {receipt_exc}")
            return ApplyReport("failed", sum(item.changed for item in combined_mutations), failed_receipt, (f"{type(exc).__name__}: {exc}",), tuple(rollback_notes))

    def _preflight_apply(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
        *,
        scopes: frozenset[ExecutionScope],
    ) -> None:
        if plan.schema != PLAN_SCHEMA:
            raise PlanRefused(f"unsupported plan schema {plan.schema!r}")
        if not plan.validate_digest():
            raise PlanRefused("plan payload does not match its embedded digest")
        if approved_digest != plan.digest:
            raise PlanRefused("approved digest does not match the plan")
        if actor.instance != plan.instance:
            raise PlanRefused("actor instance does not match plan instance")
        for action in plan.operator_actions:
            if action.required_capability not in actor.capabilities:
                raise PlanRefused(
                    f"{action.action_id}: operator action is incomplete or actor "
                    f"lacks capability {action.required_capability!r}"
                )
        for operation in plan.operations:
            if not _operation_in_scope(operation, scopes):
                continue
            capability = operation.provenance.get("required_capability", "")
            if operation.privilege != "unprivileged" and capability not in actor.capabilities:
                raise PlanRefused(
                    f"{operation.operation_id}: actor lacks capability {capability!r}"
                )
            result = backend.preflight(operation)
            if not result.ok:
                raise PlanRefused(
                    f"{operation.operation_id}: {result.classification}: {result.detail}"
                )

    def verify(
        self,
        plan: InstallPlan,
        backend: HostBackend,
    ) -> VerifyReport:
        checks = tuple(
            backend.verify_operation(item)
            for item in plan.operations
            if _operation_in_scope(item, frozenset({"all"}))
        )
        layers = tuple(
            VerifyLayer(
                layer=layer,
                name=LAYER_NAMES[layer],
                ok=all(item.ok for item in checks if item.layer == layer),
                checks=tuple(item for item in checks if item.layer == layer),
            )
            for layer in sorted(LAYER_NAMES)
        )
        all_ok = all(item.ok for item in checks)
        by_id = {item.operation_id: item for item in checks}
        unit_evidence = by_id.get("connective.unit-fire")
        mailbox_evidence = by_id.get("connective.mailbox-probe")
        timer_evidence = by_id.get("connective.timer-observe")
        acceptance_evidence = (
            unit_evidence is not None
            and unit_evidence.evidence.get("result") == "success"
            and _exec_status_within_success_policy(
                unit_evidence.evidence.get("exec_main_status", ""),
                unit_evidence.evidence.get("success_exit_status", ""),
            )
            and mailbox_evidence is not None
            and mailbox_evidence.evidence.get("receive") == "true"
            and mailbox_evidence.evidence.get("send") == "true"
            and mailbox_evidence.evidence.get("durable_round_trip") == "true"
            and timer_evidence is not None
            and timer_evidence.evidence.get("next_elapse_finite") == "true"
            and timer_evidence.evidence.get("first_fire_invocation_observed") == "true"
            and timer_evidence.evidence.get("second_consume_absent") == "true"
            and timer_evidence.evidence.get("mailbox_effect_correlated") == "true"
            and bool(timer_evidence.evidence.get("mailbox_probe_sha256"))
            and bool(timer_evidence.evidence.get("initial_viability_receipt_sha256"))
        )
        live_acceptance = (
            all_ok
            and backend.evidence_scope == "live"
            and acceptance_evidence
        )
        return VerifyReport(
            status="verified" if all_ok else "failed",
            layers=layers,
            provenance=plan.provenance,
            evidence_scope=backend.evidence_scope,
            live_acceptance=live_acceptance,
        )

    def rollback(
        self,
        receipt: ApplyReceipt,
        backend: HostBackend,
        actor: ActorContext,
    ) -> RollbackReport:
        if receipt.schema != RECEIPT_SCHEMA:
            return RollbackReport("refused", 0, ("unsupported receipt schema",))
        if actor.instance != receipt.instance:
            return RollbackReport("refused", 0, ("actor instance mismatch",))
        capability = f"host.files:{receipt.account}"
        if capability not in actor.capabilities:
            return RollbackReport(
                "refused",
                0,
                (f"actor lacks capability {capability!r}",),
            )
        changed = tuple(item for item in receipt.mutations if item.changed)
        checks: list[tuple[MutationRecord, PreflightResult]] = []
        failures = []
        for mutation in changed:
            result = backend.can_rollback(mutation)
            checks.append((mutation, result))
            if not result.ok:
                failures.append(
                    f"{mutation.operation_id}: {result.classification}: {result.detail}"
                )
        if failures:
            return RollbackReport("refused", 0, tuple(failures))
        restored = 0
        try:
            for mutation, result in reversed(checks):
                if result.classification == "not-applied":
                    continue
                backend.rollback_mutation(mutation)
                restored += 1
        except Exception as exc:
            return RollbackReport("failed", restored, (str(exc),))
        return RollbackReport("rolled-back" if restored else "noop", restored)


def _snapshot(path: Path) -> dict[str, str]:
    if not path.exists() and not path.is_symlink():
        return {"state": "absent"}
    if path.is_dir():
        return {"state": "directory", "mode": f"{path.stat().st_mode & 0o7777:04o}"}
    if path.is_file():
        content = path.read_bytes()
        return {
            "state": "file",
            "content_b64": base64.b64encode(content).decode("ascii"),
            "fingerprint": _sha256_bytes(content),
            "mode": f"{path.stat().st_mode & 0o7777:04o}",
        }
    return {"state": "other"}


def _snapshot_fingerprint(snapshot: Mapping[str, str]) -> str | None:
    state = snapshot.get("state")
    if state == "absent":
        return None
    if state == "symlink":
        return snapshot.get("fingerprint")
    if state == "directory":
        return "directory"
    return snapshot.get("fingerprint") or state


def _symlink_snapshot(path: Path) -> dict[str, str]:
    if path.is_symlink():
        target = os.readlink(path)
        return {
            "state": "symlink",
            "target": target,
            "fingerprint": _sha256_bytes(target.encode("utf-8")),
        }
    if not path.exists():
        return {"state": "absent"}
    return _snapshot(path)


def _desired_content(operation: InstallOperation) -> bytes:
    value = operation.desired.get("content")
    if value is None:
        raise OperationFailed(f"{operation.operation_id}: content missing")
    return value.encode("utf-8")


def _desired_json(operation: InstallOperation, key: str) -> Any:
    try:
        return json.loads(operation.desired[key])
    except (KeyError, json.JSONDecodeError) as exc:
        raise OperationFailed(f"{operation.operation_id}: invalid {key}") from exc


def _bridge_source_from_operation(
    operation: InstallOperation,
) -> codex_telegram_bridge.CodexTelegramSource:
    paths = tuple(str(item) for item in _desired_json(operation, "paths"))
    hashes = {str(key): str(value) for key, value in _desired_json(operation, "sha256").items()}
    source = codex_telegram_bridge.CodexTelegramSource(
        repo="codex-config",
        revision=operation.desired["revision"],
        paths=paths,
        sha256=hashes,
    )
    if source.revision != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_REVISION:
        raise OperationFailed(f"{operation.operation_id}: source revision is not approved")
    if source.paths != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_PATHS:
        raise OperationFailed(f"{operation.operation_id}: source paths are not approved")
    if dict(source.sha256) != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_SHA256:
        raise OperationFailed(f"{operation.operation_id}: source hashes are not approved")
    return source


def _bridge_archive_path_from_operation(operation: InstallOperation) -> Path:
    raw = operation.desired.get("archive_path")
    if not raw:
        raise OperationFailed(f"{operation.operation_id}: archive_path missing")
    path = Path(raw)
    if not path.is_absolute():
        raise OperationFailed(f"{operation.operation_id}: archive_path must be absolute")
    return path


def _bridge_binding_from_operation(
    operation: InstallOperation,
) -> codex_telegram_bridge.CodexTelegramBinding:
    persona = operation.provenance.get("persona")
    if not persona:
        raise OperationFailed(f"{operation.operation_id}: unsupported bridge persona")
    tmux_session = codex_telegram_bridge._parse_tmux_session(operation.desired.get("tmux_session"))
    tmux_pane = codex_telegram_bridge._parse_tmux_pane(operation.desired.get("tmux_pane"))
    effective_pane = codex_telegram_bridge._effective_pane(tmux_session, tmux_pane)
    if operation.desired.get("effective_pane") != effective_pane:
        raise OperationFailed(f"{operation.operation_id}: effective pane does not match tmux session and pane")
    return codex_telegram_bridge.CodexTelegramBinding(
        persona=persona,
        account=operation.owner,
        account_home=Path(operation.desired["account_home"]),
        claude_receiver_topology=codex_telegram_bridge._parse_claude_receiver_topology(
            operation.desired.get("claude_receiver_topology")
        ),
        predecessor_claude_receiver_topology=codex_telegram_bridge._parse_predecessor_claude_receiver_topology(
            operation.desired.get("predecessor_claude_receiver_topology"),
            managed=codex_telegram_bridge._parse_claude_receiver_topology(
                operation.desired.get("claude_receiver_topology")
            ),
        ),
        claude_receiver_unit=operation.desired["claude_receiver_unit"],
        codex_receiver_unit=operation.desired["codex_receiver_unit"],
        tmux_session=tmux_session,
        tmux_pane=tmux_pane,
        credential_env=Path(operation.desired["credential_env"]),
        state_dir=Path(operation.desired["state_dir"]),
        release_root=Path(operation.desired["release_root"]),
        watchdog_units=tuple(str(item) for item in _desired_json(operation, "watchdog_units")),
        mcp_name=operation.desired["mcp_name"],
        source=dataclasses.replace(
            _bridge_source_from_operation(operation),
            owner_account=operation.desired.get("source_account") or None,
            source_root=Path(operation.desired["source_root"]) if operation.desired.get("source_root") else None,
        ),
        unattended_telegram_tools=operation.desired.get("unattended_telegram_tools") == "true",
    )


def _mcp_existing_matches(content: str, operation: InstallOperation) -> bool | None:
    try:
        data = codex_telegram_bridge.tomllib.loads(content)
        expected = codex_telegram_bridge.tomllib.loads(operation.desired["expected"])
    except codex_telegram_bridge.tomllib.TOMLDecodeError as exc:
        raise OperationFailed("existing Codex config is not valid TOML") from exc
    return codex_telegram_bridge.mcp_registration_compatibility(
        data,
        mcp_name=operation.desired["name"],
        expected_table=expected.get("mcp_servers", {}).get(operation.desired["name"], {}),
    )


def _finite_next_elapse_basis(values: Mapping[str, str]) -> str | None:
    """Return the systemd clock basis with a finite scheduled next elapse."""

    invalid = {"", "0", "n/a", "infinity"}
    for basis, key in (
        ("realtime", "NextElapseUSecRealtime"),
        ("monotonic", "NextElapseUSecMonotonic"),
    ):
        if values.get(key, "").strip().lower() not in invalid:
            return basis
    return None


def _exec_status_within_success_policy(exec_main_status: str, success_exit_status: str) -> bool:
    """True if a service's last exit code is within its configured success policy.

    T-3106: the converged-legacy / timer-observe exit-status invariant. An exit is
    in policy when it is 0 OR one of the unit's ``SuccessExitStatus`` exit codes
    (``systemctl show -p SuccessExitStatus`` -- e.g. recovery's consume service is
    ``SuccessExitStatus=3 4 5`` and exits 5 on an idle cycle). Trusting only
    ``Result==success`` is not enough: ``systemctl reset-failed`` clears ``Result``
    to success but leaves ``ExecMainStatus`` stale, so a genuinely-failed service
    can read ``Result=success`` with an out-of-policy exit code until its next fire.
    Re-deriving membership against ``SuccessExitStatus`` rejects that reset-failed-
    masked failure while still admitting a real mapped-success exit.

    ``success_exit_status`` tokens may name signals (``SIGTERM`` ...) as well as exit
    codes; only exit codes are matched here -- a converged consume oneshot has
    ``ExecMainCode=exited`` (its ``ExecMainStatus`` is an exit code, never a signal),
    and a signal kill would already fail the ``Result==success`` / inactive gates.
    A missing or non-numeric ``ExecMainStatus`` is not in policy (returns False)."""
    try:
        code = int(exec_main_status)
    except (TypeError, ValueError):
        return False
    if code == 0:
        return True
    allowed: set[int] = set()
    for token in (success_exit_status or "").split():
        try:
            allowed.add(int(token))
        except ValueError:
            continue  # signal name (SIGxxx) -- not an exit code, skip
    return code in allowed


# T-3211: the legacy-convergence probe's in-flight-service settle window. The
# consume oneshot's longest observed run is ~3s; 15s is ~5x that, giving a
# comfortable margin over transient scheduling jitter. The consume timer's own
# period is ~150s (OnUnitInactiveSec=2min + RandomizedDelaySec=20s), so 15s is
# ~1/10 of that period -- the settle window can never straddle two consecutive
# fires, and the preflight budget stays small.
LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS = 15.0
LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS = 0.75

_LEGACY_CONVERGENCE_SERVICE_IN_FLIGHT_STATES = frozenset({"activating", "active", "deactivating"})


def _legacy_convergence_service_in_flight(values: Mapping[str, str]) -> bool:
    """True if a consume SERVICE sample looks mid-fire rather than settled.

    T-3211: the oneshot's own transition sequence is inactive -> activating ->
    active -> deactivating -> inactive; any of the three non-terminal
    ActiveState values, or a non-empty Job (systemd queues the pending
    transition there before ActiveState itself updates), means the sample
    landed inside that window rather than after it -- re-sample rather than
    reject outright.
    """
    if values.get("ActiveState", "") in _LEGACY_CONVERGENCE_SERVICE_IN_FLIGHT_STATES:
        return True
    return bool(values.get("Job", "").strip())


def _valid_systemd_invocation_id(value: str) -> bool:
    return len(value) == 32 and value.islower() and all(item in "0123456789abcdef" for item in value)


UNIT_FIRE_POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class UnitFirePollSnapshot:
    active_state: str
    substate: str
    result: str
    exec_main_status: str
    success_exit_status: str
    invocation_id: str
    invocation_id_valid: bool
    job: str
    stdout_length: int
    stdout_sha256: str


def _unit_fire_invocation_digest(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8")) if value else ""


def _unit_fire_bounded_diagnostics(
    *,
    phase: str,
    classification: str,
    snapshot: UnitFirePollSnapshot | None,
    start_count: int,
    cleanup_state: str,
) -> dict[str, str]:
    invocation = snapshot.invocation_id if snapshot else ""
    return {
        "phase": phase,
        "classification": classification,
        "active_state": snapshot.active_state if snapshot else "",
        "substate": snapshot.substate if snapshot else "",
        "result": snapshot.result if snapshot else "",
        "exec_main_status": snapshot.exec_main_status if snapshot else "",
        "invocation_id_length": str(len(invocation)),
        "invocation_id_sha256": _unit_fire_invocation_digest(invocation),
        "stdout_length": str(snapshot.stdout_length if snapshot else 0),
        "stdout_sha256": snapshot.stdout_sha256 if snapshot else "",
        "start_count": str(start_count),
        "cleanup_state": cleanup_state,
    }


def _unit_fire_operation_failed(
    message: str,
    *,
    phase: str,
    classification: str,
    snapshot: UnitFirePollSnapshot | None,
    start_count: int,
    cleanup_state: str,
) -> OperationFailed:
    evidence = _unit_fire_bounded_diagnostics(
        phase=phase,
        classification=classification,
        snapshot=snapshot,
        start_count=start_count,
        cleanup_state=cleanup_state,
    )
    detail = f"{message}; unit_fire_diagnostics={_canonical_json(evidence)}"
    return OperationFailed(detail, evidence=evidence)


def _unit_fire_snapshot_has_failed(snapshot: UnitFirePollSnapshot) -> bool:
    # T-3106/T-3107: an exit is a failure only if it is non-empty AND outside the
    # unit's success policy ({0} u SuccessExitStatus). An empty exec status (no exit
    # yet) or an in-policy exit (0 or a SuccessExitStatus-mapped code, e.g. recovery's
    # consume service exiting 5 on an idle fire) is NOT a failure -- matches the
    # hybrid invariant the admit gates use, rather than a hardcoded ExecMainStatus==0.
    return (
        snapshot.active_state == "failed"
        or (snapshot.result not in {"", "success"})
        or (
            snapshot.exec_main_status != ""
            and not _exec_status_within_success_policy(
                snapshot.exec_main_status, snapshot.success_exit_status
            )
        )
    )


def _is_installer_managed(content: str) -> bool:
    lines = content.splitlines()
    if lines and lines[0] == f"# {MANAGED_MARKER}":
        return True
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(raw, dict)
        and raw.get("managed_by") == "pantheon-persona-installer"
    )


class SimulatedHostBackend:
    """Deterministic same-process backend for ordering and rollback proofs."""

    def __init__(
        self,
        *,
        evidence_scope: str = "fixture",
        fail_operation: str | None = None,
        unavailable_kinds: Sequence[str] = (),
        mailbox_send: bool = True,
        mailbox_receive: bool = True,
        finite_next_elapse: bool = True,
        observed_timed_fire: bool = True,
    ):
        self.evidence_scope = evidence_scope
        self.fail_operation = fail_operation
        self.unavailable_kinds = frozenset(unavailable_kinds)
        self.mailbox_send = mailbox_send
        self.mailbox_receive = mailbox_receive
        self.finite_next_elapse = finite_next_elapse
        self.observed_timed_fire = observed_timed_fire
        self.entries: dict[str, dict[str, str]] = {}
        self.events: list[str] = []
        self.receipts: dict[str, Mapping[str, Any]] = {}
        self.receipt_events: list[str] = []
        self.timer_enabled = False
        self.converged = False

    def seed_file(self, target: str, content: str, mode: str = "0644") -> None:
        raw = content.encode("utf-8")
        self.entries[target] = {
            "state": "file",
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "fingerprint": _sha256_bytes(raw),
            "mode": mode,
        }

    def drift(self, target: str, content: str) -> None:
        self.seed_file(target, content)

    def _snapshot(self, target: str) -> dict[str, str]:
        return dict(self.entries.get(target, {"state": "absent"}))

    def preflight(self, operation: InstallOperation) -> PreflightResult:
        if operation.kind in self.unavailable_kinds:
            return PreflightResult(False, "unavailable", "backend kind unavailable")
        if operation.kind == "unit-fire":
            try:
                _parse_unit_fire_timeout_budget(operation)
                _parse_unit_fire_initial_viability_desired(operation)
            except OperationFailed as exc:
                return PreflightResult(False, "unit-fire", str(exc))
        if operation.kind == "mkdir":
            state = self._snapshot(operation.target)
            if state["state"] in {"absent", "directory"}:
                return PreflightResult(True, state["state"], "safe directory state")
            return PreflightResult(False, "conflicting", "non-directory exists")
        if operation.kind in {"atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            state = self._snapshot(operation.target)
            if state["state"] == "absent":
                return PreflightResult(True, "absent", "will create managed file")
            if state["state"] != "file":
                return PreflightResult(False, "conflicting", "target is not a regular file")
            desired = operation.desired["sha256"]
            if state.get("fingerprint") == desired:
                return PreflightResult(True, "matching", "desired content already installed")
            content = base64.b64decode(state["content_b64"]).decode("utf-8", "replace")
            if _is_installer_managed(content):
                return PreflightResult(True, "managed", "installer-owned file may converge")
            return PreflightResult(False, "conflicting", "unknown regular file")
        if operation.kind == "timer-enablement-absence-check":
            state = self._snapshot(operation.target)
            if state["state"] == "absent":
                return PreflightResult(True, "absent", "consume timer enablement link is absent")
            return PreflightResult(False, "conflicting", "consume timer enablement link already exists")
        if operation.kind == "timer-enable":
            link = operation.desired.get("enablement_link_path", "")
            state = self._snapshot(link) if link else {"state": "absent"}
            if self.timer_enabled:
                if state["state"] not in {"absent", "symlink"}:
                    return PreflightResult(False, "conflicting", "enabled timer link is not canonical")
            elif link and state["state"] != "absent":
                return PreflightResult(False, "conflicting", "consume timer enablement link already exists")
        if operation.kind == "codex-telegram-mcp-check":
            state = self._snapshot(operation.target)
            expected = _sha256_bytes(operation.desired["expected"].encode("utf-8"))
            if state["state"] == "absent":
                return PreflightResult(True, "absent", "will create MCP registration")
            if state["state"] == "file" and state.get("fingerprint") == expected:
                return PreflightResult(True, "matching", "MCP registration already matches")
            if state["state"] == "file":
                content = base64.b64decode(state["content_b64"]).decode("utf-8", "replace")
                match = _mcp_existing_matches(content, operation)
                if match is True:
                    return PreflightResult(True, "matching", "MCP registration already matches")
                if match is None:
                    return PreflightResult(True, "absent", "will append MCP registration table")
            return PreflightResult(False, "conflicting", "MCP registration mismatch")
        if operation.kind == "claude-mcp-server-check":
            verdict = _claude_mcp_verdict(
                _sim_claude_servers(self._snapshot(operation.target)), operation
            )
            # Read-only audit: always available, verdict rides the classification.
            return PreflightResult(True, verdict, _CLAUDE_MCP_VERDICT_DETAIL[verdict])
        if operation.kind == "claude-mcp-server-registration":
            verdict = _claude_mcp_verdict(
                _sim_claude_servers(self._snapshot(operation.target)), operation
            )
            if verdict == "conflicting":
                return PreflightResult(
                    False, "conflicting", "existing Claude MCP server differs from manifest"
                )
            if verdict == "matching":
                return PreflightResult(
                    True, "matching", "Claude MCP server already registered"
                )
            return PreflightResult(True, "absent", "will register Claude MCP server via CLI")
        if operation.kind in {
            "codex-telegram-source-check",
            "codex-telegram-disabled-first",
            "codex-standing-instructions-source-check",
            "codex-standing-instructions-target-check",
            "codex-standing-instructions-override-check",
            "mailbox-writable-root-check",
        }:
            return PreflightResult(True, "check", "codex telegram typed operation available")
        return PreflightResult(True, "check", "typed operation available")

    def execute(
        self,
        operation: InstallOperation,
        context: ExecutionContext,
    ) -> tuple[MutationRecord, OperationEvidence]:
        if operation.operation_id == self.fail_operation:
            raise OperationFailed("injected operation failure")
        if operation.operation_id == "connective.timer-enablement-absent" and self.events:
            self.events.clear()
        before = self._snapshot(operation.target)
        changed = False
        ok = True
        detail = "typed operation passed"
        observed: dict[str, str] = {}
        if operation.kind == "mkdir":
            if before["state"] == "absent":
                self.entries[operation.target] = {
                    "state": "directory",
                    "mode": operation.desired.get("mode", "0755"),
                }
                changed = True
        elif operation.kind in {"atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            raw = _desired_content(operation)
            if before.get("fingerprint") != _sha256_bytes(raw):
                self.entries[operation.target] = {
                    "state": "file",
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "fingerprint": _sha256_bytes(raw),
                    "mode": operation.desired.get("mode", "0644"),
                }
                changed = True
        elif operation.kind == "codex-telegram-mcp-check":
            raw = operation.desired["expected"].encode("utf-8")
            if before["state"] == "absent":
                self.entries[operation.target] = {
                    "state": "file",
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "fingerprint": _sha256_bytes(raw),
                    "mode": "0600",
                }
                changed = True
            elif before.get("fingerprint") != _sha256_bytes(raw):
                content = base64.b64decode(before["content_b64"]).decode("utf-8", "replace")
                match = _mcp_existing_matches(content, operation)
                if match is False:
                    raise OperationFailed("existing Codex MCP registration mismatches")
                if match is True:
                    changed = False
                else:
                    updated = content + ("\n" if content and not content.endswith("\n") else "") + "\n" + operation.desired["expected"]
                    raw = updated.encode("utf-8")
                    self.entries[operation.target] = {
                        "state": "file",
                        "content_b64": base64.b64encode(raw).decode("ascii"),
                        "fingerprint": _sha256_bytes(raw),
                        "mode": before.get("mode", "0600"),
                    }
                    changed = True
        elif operation.kind == "claude-mcp-server-check":
            # Read-only audit: record the verdict, never mutate.
            verdict = _claude_mcp_verdict(_sim_claude_servers(before), operation)
            before = {"state": "not-applicable"}
            observed = {
                "name": operation.desired["name"],
                "scope": operation.desired["scope"],
                "verdict": verdict,
            }
            detail = f"Claude MCP audit verdict for {operation.desired['name']}: {verdict}"
        elif operation.kind == "claude-mcp-server-registration":
            document = _sim_claude_document(before)
            servers = document.get("mcpServers", {})
            servers = servers if isinstance(servers, dict) else {}
            verdict = _claude_mcp_verdict(servers, operation)
            name = operation.desired["name"]
            if verdict == "conflicting":
                raise OperationFailed("existing Claude MCP server differs from manifest")
            if verdict == "matching":
                changed = False
                before = {
                    "state": "claude-mcp-server",
                    "added": "false",
                    "name": name,
                    "expected": operation.desired["expected"],
                }
            else:
                document.setdefault("mcpServers", {})[name] = json.loads(
                    operation.desired["config_json"]
                )
                raw = json.dumps(document).encode("utf-8")
                self.entries[operation.target] = {
                    "state": "file",
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "fingerprint": _sha256_bytes(raw),
                    "mode": "0600",
                }
                changed = True
                before = {
                    "state": "claude-mcp-server",
                    "added": "true",
                    "name": name,
                    "expected": operation.desired["expected"],
                }
            observed = {
                "name": name,
                "scope": operation.desired["scope"],
                "registered": "true",
            }
        elif operation.kind == "timer-enablement-absence-check":
            before = {"state": "not-applicable"}
            state = self._snapshot(operation.target)
            if state["state"] != "absent":
                raise OperationFailed(f"{operation.operation_id}: consume timer enablement link already exists")
            observed = {
                "enablement_link_path": operation.target,
                "canonical_template_path": operation.desired["canonical_template_path"],
                "enablement_link_state": state["state"],
                "historical_boundary_absent": "true",
            }
        elif operation.kind == "codex-telegram-source-check":
            _bridge_source_from_operation(operation)
            _bridge_archive_path_from_operation(operation)
            release_target = str(Path(operation.desired["release_root"]) / "releases" / operation.desired["revision"])
            desired_pointer = {
                "state": "symlink",
                "target": release_target,
                "fingerprint": _sha256_bytes(release_target.encode("utf-8")),
            }
            if before != desired_pointer:
                self.entries[operation.target] = desired_pointer
                changed = True
        elif operation.kind == "codex-telegram-disabled-first":
            before = {"state": "not-applicable"}
        elif operation.kind == "codex-standing-instructions-source-check":
            before = {"state": "not-applicable"}
            observed = {
                "source": operation.desired["source"],
                "source_commit": operation.desired["source_commit"],
                "source_sha256": operation.desired["source_sha256"],
                "pre_mutation_only": "true",
            }
        elif operation.kind == "codex-standing-instructions-target-check":
            before = {"state": "not-applicable"}
            installed = self._snapshot(operation.target)
            observed = {
                "target_sha256": installed.get("fingerprint", ""),
                "expected_sha256": operation.desired["sha256"],
                "after_install": str("codex-standing-instructions.install" in self.events).lower(),
            }
            if installed.get("fingerprint") != operation.desired["sha256"]:
                ok = False
                detail = "Codex standing instructions target hash mismatch"
        elif operation.kind == "codex-standing-instructions-override-check":
            before = {"state": "not-applicable"}
            override = self._snapshot(operation.target)
            ok = override["state"] == "absent" or (
                override["state"] == "file"
                and base64.b64decode(override.get("content_b64", "")).strip() == b""
            )
            observed = {"override_state": override["state"], "after_install": str("codex-standing-instructions.install" in self.events).lower()}
            detail = "Codex override absent or empty" if ok else "Codex override file is non-empty"
        elif operation.kind == "mailbox-writable-root-check":
            before = {"state": "not-applicable"}
            observed = {
                "root": operation.target,
                "harness": operation.desired.get("harness", ""),
                "ref_kind": operation.desired.get("ref_kind", ""),
                "ref_persona": operation.desired.get("ref_persona", ""),
                "maildir_subdirs": operation.desired.get("maildir_subdirs", ""),
                "transient_probe": operation.desired.get("transient_probe", ""),
            }
            detail = "mailbox writable root probe passed"
        elif operation.kind == "stale-unit-failure-reset":
            before = {"state": "clean-noop"}
            observed = {
                "classification": "noop",
                "pre_state": "clean-noop",
                "post_state": "clean-noop",
                "pre_load_state": "not-found",
                "pre_active_state": "inactive",
                "pre_sub_state": "dead",
                "pre_result": "success",
                "pre_exec_main_status": "0",
            }
            detail = "consume service stale failure state is absent"
        if operation.kind == "timer-enable":
            link = operation.desired.get("enablement_link_path", "")
            canonical = operation.desired.get("canonical_template_path", "")
            before = {
                "state": "enabled" if self.timer_enabled else "disabled",
            }
            changed = not self.timer_enabled
            if context.workflow == "staged-activate" and self.timer_enabled:
                raise OperationFailed("timer already enabled at staged activation boundary")
            if changed and link and canonical:
                if self._snapshot(link)["state"] != "absent":
                    raise OperationFailed(f"{operation.operation_id}: consume timer enablement link already exists")
                before.update(
                    {
                        "activation_intent": "false",
                        "owned": "true",
                        "enablement_link_state": "absent",
                        "enablement_link_path": link,
                        "canonical_template_path": canonical,
                        "canonical_template_path_sha256": _sha256_bytes(canonical.encode("utf-8")),
                        "enablement_link_actual_readlink": canonical,
                        "enablement_link_actual_readlink_sha256": _sha256_bytes(canonical.encode("utf-8")),
                        "enablement_link_resolved_target": canonical,
                        "enablement_link_resolved_target_sha256": _sha256_bytes(canonical.encode("utf-8")),
                    }
                )
                self.entries[link] = {
                    "state": "symlink",
                    "target": canonical,
                    "fingerprint": _sha256_bytes(canonical.encode("utf-8")),
                }
            elif link and canonical:
                state = self._snapshot(link)
                if state["state"] not in {"absent", "symlink"}:
                    raise OperationFailed("enabled timer link is not canonical")
                if state["state"] == "symlink" and state.get("target") != canonical:
                    raise OperationFailed("enabled timer link is not canonical")
            self.timer_enabled = True
        self.events.append(operation.operation_id)
        if operation.kind == "unit-fire":
            budget = _parse_unit_fire_timeout_budget(operation)
            viability = _parse_unit_fire_initial_viability_desired(operation)
            selected = json.loads(operation.desired["allowed_runtime_refs"])[0]
            selected_harness, _, selected_profile = selected.partition("/")
            ok = self.converged or "connective.timer-enable" not in self.events
            detail = "oneshot inactive with Result=success and ExecMainStatus=0"
            observed = {
                "invocation_id": "0" * 32,
                "active_state": "inactive",
                "result": "success",
                "exec_main_status": "0",
                "timer_enabled": "false",
                "unit_fire_timeout_seconds": str(budget.seconds),
                "unit_fire_timeout_candidate_count": str(budget.candidate_count),
                "unit_fire_timeout_post_start_count": str(budget.post_start_count),
                "selected_runtime": selected,
                "selected_harness": selected_harness,
                "selected_profile": selected_profile,
                "provider_completion_status": (
                    "provider-completed" if selected_harness == "claude" else "not-applicable"
                ),
                "initial_viability_receipt_sha256": _sha256_bytes(
                    _canonical_json(
                        {
                            "persona": operation.provenance["persona"],
                            "invocation_id": "0" * 32,
                            "allowed_runtime_refs": list(viability.allowed_runtime_refs),
                        }
                    ).encode("utf-8")
                ),
            }
        elif operation.kind == "mailbox-probe":
            ok = self.mailbox_send and self.mailbox_receive
            detail = (
                "mailbox receive/send durable round trip passed"
                if ok
                else "mailbox ACL direction failed"
            )
            observed = {
                "receive": str(self.mailbox_receive).lower(),
                "send": str(self.mailbox_send).lower(),
                "durable_round_trip": str(ok).lower(),
                "sha256": _sha256_bytes(b"simulated-mailbox-round-trip"),
            }
        elif operation.kind == "timer-enable":
            prerequisites = {
                "harness.direct-fire",
                "connective.unit-fire",
                "connective.mailbox-probe",
            }
            ok = prerequisites.issubset(self.events)
            detail = (
                "timer enabled after all first-fire gates"
                if ok
                else "timer ordering gate failed"
            )
        elif operation.kind == "timer-observe":
            unit_fire = self.events.index("connective.unit-fire") if "connective.unit-fire" in self.events else -1
            ok = self.finite_next_elapse and unit_fire >= 0
            mailbox_probe = next(
                (
                    item
                    for item in reversed(self.events)
                    if item == "connective.mailbox-probe"
                ),
                "",
            )
            detail = (
                "finite next elapse and first fire remains the only consume invocation"
                if ok
                else "timer observation incomplete"
            )
            first_fire = "0" * 32
            observed = {
                "next_elapse_finite": str(self.finite_next_elapse).lower(),
                "first_fire_invocation_observed": str(unit_fire >= 0).lower(),
                "second_consume_absent": str(unit_fire >= 0).lower(),
                "mailbox_effect_correlated": str(
                    unit_fire >= 0 and bool(mailbox_probe)
                ).lower(),
                "mailbox_probe_sha256": _sha256_bytes(
                    b"simulated-mailbox-round-trip"
                ),
                "unit_fire_invocation_id": first_fire,
                "initial_viability_receipt_sha256": _sha256_bytes(first_fire.encode("utf-8")),
            }
            if ok:
                self.converged = True
        elif operation.kind == "codex-telegram-source-check":
            observed = {
                "revision": operation.desired["revision"],
                "archive_verified": "true",
                "release_staged": "true",
                "claude_receiver_topology": operation.desired["claude_receiver_topology"],
                "predecessor_claude_receiver_topology": operation.desired["predecessor_claude_receiver_topology"],
                "tmux_pane": operation.desired["tmux_pane"],
                "effective_pane": operation.desired["effective_pane"],
            }
            detail = "codex telegram archive verified and staged"
        elif operation.kind == "codex-telegram-disabled-first":
            observed = {
                "active": "inactive",
                "enabled": "disabled",
                "environment_file": "absent",
            }
            detail = "codex telegram receiver is disabled before cutover"
        elif operation.kind == "codex-telegram-mcp-check":
            observed = {
                "name": operation.desired["name"],
                "registered": "true",
                "expected_sha256": _sha256_bytes(operation.desired["expected"].encode("utf-8")),
            }
            detail = "codex telegram MCP registration matches expected stanza"
        elif operation.kind == "codex-standing-instructions-source-check":
            detail = "Codex standing instructions source path, commit, and hash verified"
        elif operation.kind == "codex-standing-instructions-target-check":
            detail = detail if not ok else "Codex standing instructions target verified after install"
        elif operation.kind == "codex-standing-instructions-override-check":
            detail = detail if not ok else "Codex override precedence checked after install"
        after = self._snapshot(operation.target)
        after_fingerprint = _snapshot_fingerprint(after)
        if operation.kind == "timer-enable":
            after_fingerprint = "timer-enabled"
        if operation.kind in {
            "codex-telegram-disabled-first",
            "codex-standing-instructions-source-check",
            "codex-standing-instructions-target-check",
            "codex-standing-instructions-override-check",
            "mailbox-writable-root-check",
            "stale-unit-failure-reset",
        }:
            after_fingerprint = "verified"
        if operation.kind == "stale-unit-failure-reset":
            after_fingerprint = "evidence-only"
        return (
            MutationRecord(
                operation_id=operation.operation_id,
                kind=operation.kind,
                target=operation.target,
                owner=operation.owner,
                changed=changed,
                before=before,
                after_fingerprint=after_fingerprint,
            ),
            OperationEvidence(
                operation_id=operation.operation_id,
                layer=operation.layer,
                kind=operation.kind,
                ok=ok,
                detail=detail,
                evidence=observed,
            ),
        )

    def verify_operation(
        self,
        operation: InstallOperation,
        *,
        staged_evidence: OperationEvidence | None = None,
    ) -> OperationEvidence:
        ok = operation.operation_id in self.events
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            result = self.preflight(operation)
            ok = result.ok and result.classification in {"directory", "matching"}
            detail = result.detail if ok else "operation desired state absent or drifted"
        elif operation.kind == "codex-telegram-mcp-check":
            result = self.preflight(operation)
            ok = result.ok and result.classification == "matching"
            detail = result.detail if ok else "MCP registration desired state absent or drifted"
        elif operation.kind == "claude-mcp-server-check":
            verdict = _claude_mcp_verdict(
                _sim_claude_servers(self._snapshot(operation.target)), operation
            )
            ok = True
            detail = f"Claude MCP audit verdict for {operation.desired['name']}: {verdict}"
        elif operation.kind == "claude-mcp-server-registration":
            result = self.preflight(operation)
            ok = result.ok and result.classification == "matching"
            detail = (
                result.detail
                if ok
                else "Claude MCP server absent or drifted after registration"
            )
        elif operation.kind == "codex-standing-instructions-target-check":
            installed = self._snapshot(operation.target)
            ok = (
                installed["state"] == "file"
                and installed.get("fingerprint") == operation.desired["sha256"]
            )
            detail = "Codex standing instructions target hash matches" if ok else "Codex standing instructions target hash mismatch"
        elif operation.kind == "codex-standing-instructions-override-check":
            override = self._snapshot(operation.target)
            ok = override["state"] == "absent" or (
                override["state"] == "file"
                and base64.b64decode(override.get("content_b64", "")).strip() == b""
            )
            detail = "Codex override absent or empty" if ok else "Codex override file is non-empty"
        elif operation.kind == "codex-telegram-source-check":
            archive_known = bool(operation.desired.get("archive_path"))
            current = self._snapshot(operation.target)
            expected_target = str(Path(operation.desired["release_root"]) / "releases" / operation.desired["revision"])
            ok = (
                archive_known
                and current.get("state") == "symlink"
                and current.get("target") == expected_target
            )
            detail = "codex telegram source archive and release pointer verified" if ok else "codex telegram source archive or release pointer drifted"
        elif operation.kind == "codex-telegram-disabled-first":
            ok = operation.operation_id in self.events
            detail = "codex telegram receiver disabled-first evidence present" if ok else "codex telegram disabled-first evidence absent"
        elif operation.kind == "mailbox-writable-root-check":
            result = self.preflight(operation)
            ok = result.ok
            detail = result.detail if ok else "mailbox writable root invariant failed"
        elif operation.kind == "timer-enablement-absence-check":
            if operation.operation_id in self.events and "connective.timer-enable" in self.events:
                ok = True
                detail = "historical consume timer enablement absence boundary retained"
            else:
                result = self.preflight(operation)
                admissible_classifications = _timer_enablement_absence_admissible_classifications(
                    staged_evidence
                )
                ok = result.ok and result.classification in admissible_classifications
                detail = result.detail
        else:
            detail = (
                "verified from deterministic backend state"
                if ok
                else "operation evidence absent"
            )
        return OperationEvidence(
            operation_id=operation.operation_id,
            layer=operation.layer,
            kind=operation.kind,
            ok=ok,
            detail=detail,
        )

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.kind == "timer-enable":
            before_enabled = mutation.before.get("state") == "enabled"
            link = mutation.before.get("enablement_link_path", "")
            if link:
                state = self._snapshot(link)
                if state["state"] not in {"absent", "symlink"}:
                    return PreflightResult(False, "drifted", "timer enablement link is not removable")
                if state["state"] == "symlink":
                    target = state.get("target", "")
                    if (
                        mutation.before.get("owned") != "true"
                        or target != mutation.before.get("enablement_link_actual_readlink", "")
                        or target != mutation.before.get("canonical_template_path", "")
                    ):
                        return PreflightResult(False, "drifted", "timer enablement link changed after receipt")
                    return PreflightResult(True, "owned", "timer enablement link matches receipt")
            if self.timer_enabled == before_enabled:
                return PreflightResult(
                    True,
                    "not-applied",
                    "timer still matches the write-ahead state",
                )
            if not self.timer_enabled:
                return PreflightResult(False, "drifted", "timer is no longer enabled")
            return PreflightResult(True, "owned", "timer enable state matches receipt")
        if mutation.kind == "claude-mcp-server-check":
            return PreflightResult(True, "not-applied", "read-only audit op made no change")
        if mutation.kind == "claude-mcp-server-registration":
            return _claude_mcp_registration_can_rollback(
                mutation, _sim_claude_servers(self._snapshot(mutation.target))
            )
        current = _snapshot_fingerprint(self._snapshot(mutation.target))
        before = _snapshot_fingerprint(mutation.before)
        if current == before:
            return PreflightResult(
                True,
                "not-applied",
                "target already matches the pre-apply receipt state",
            )
        if current != mutation.after_fingerprint:
            return PreflightResult(False, "drifted", "post-apply fingerprint changed")
        return PreflightResult(True, "owned", "post-apply fingerprint matches receipt")

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if mutation.kind == "timer-enable":
            link = mutation.before.get("enablement_link_path", "")
            if link and mutation.before.get("owned") == "true":
                self.entries.pop(link, None)
            self.timer_enabled = mutation.before.get("state") == "enabled"
            self.converged = False
            self.events.append(f"rollback:{mutation.operation_id}")
            return
        if mutation.kind == "claude-mcp-server-check":
            self.events.append(f"rollback:{mutation.operation_id}")
            return
        if mutation.kind == "claude-mcp-server-registration":
            if mutation.before.get("added") == "true":
                document = _sim_claude_document(self._snapshot(mutation.target))
                servers = document.get("mcpServers", {})
                if isinstance(servers, dict):
                    servers.pop(mutation.before.get("name"), None)
                raw = json.dumps(document).encode("utf-8")
                self.entries[mutation.target] = {
                    "state": "file",
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "fingerprint": _sha256_bytes(raw),
                    "mode": "0600",
                }
            self.events.append(f"rollback:{mutation.operation_id}")
            return
        before = dict(mutation.before)
        if before.get("state") == "absent":
            self.entries.pop(mutation.target, None)
        else:
            self.entries[mutation.target] = before
        self.events.append(f"rollback:{mutation.operation_id}")

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable":
            return None
        link = operation.desired.get("enablement_link_path", "")
        canonical = operation.desired.get("canonical_template_path", "")
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=False,
            before={
                "state": "enabled" if self.timer_enabled else "disabled",
                "activation_intent": "true",
                "owned": "false",
                "enablement_link_state": "absent",
                "enablement_link_path": link,
                "canonical_template_path": canonical,
            },
            after_fingerprint="timer-enabled",
        )

    def issued_timer_enable_recovery_mutation(
        self,
        operation: InstallOperation,
        evidence: Mapping[str, str],
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable" or evidence.get("enable_command_issued") != "true":
            return None
        link = operation.desired.get("enablement_link_path", "")
        canonical = operation.desired.get("canonical_template_path", "")
        if not self.timer_enabled or not link or not canonical:
            return None
        state = self._snapshot(link)
        if state.get("state") != "symlink" or state.get("target") != canonical:
            return None
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=True,
            before={
                "state": "disabled",
                "activation_intent": "false",
                "owned": "true",
                "enablement_link_state": "absent",
                "enablement_link_path": link,
                "canonical_template_path": canonical,
                "canonical_template_path_sha256": _sha256_bytes(canonical.encode("utf-8")),
                "enablement_link_actual_readlink": canonical,
                "enablement_link_actual_readlink_sha256": _sha256_bytes(canonical.encode("utf-8")),
                "enablement_link_resolved_target": canonical,
                "enablement_link_resolved_target_sha256": _sha256_bytes(canonical.encode("utf-8")),
            },
            after_fingerprint="timer-enabled",
        )

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        self.receipts[receipt.receipt_id] = receipt.to_dict()
        self.receipt_events.append(f"receipt:{receipt.phase}")
        self.events.append(f"receipt:{receipt.phase}")


class LocalHostBackend:
    """Fixed-argv backend for the current account's real user manager.

    This backend never changes Unix users, groups, ACLs, credentials, or policy.
    Those remain typed operator actions.  It refuses a target account other than
    the current uid and refuses an unreachable user bus before any mutation.
    """

    evidence_scope = "live"

    def __init__(self, *, command_timeout: int = 30, timed_fire_timeout: int = 180):
        self.command_timeout = command_timeout
        self.timed_fire_timeout = timed_fire_timeout
        self.events: dict[str, OperationEvidence] = {}
        self._baseline_invocation = ""

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout or self.command_timeout,
        )

    def _run_claude_mcp(
        self,
        args: Sequence[str],
        *,
        home: str,
        phase: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run `claude mcp <args>` — Claude Code's own race-free writer (D2) — with
        HOME pinned to the target account so `--scope user` lands in that account's
        ~/.claude.json. This is a SPECIFIC typed subprocess op running a SPECIFIC
        command, not a generic shell op. Fail-closed: a nonzero exit or a failure to
        spawn raises OperationFailed with bounded (hashed) stdout/stderr evidence."""
        argv = ["claude", "mcp", *args]
        env = {**os.environ, "HOME": home}
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperationFailed(f"claude mcp {phase} failed to run: {exc}")
        if result.returncode != 0:
            raise OperationFailed(
                (result.stderr or result.stdout or "").strip()
                or f"claude mcp {phase} exited {result.returncode}",
                evidence=_completed_process_bounded_evidence(f"claude_mcp_{phase}", result),
            )
        return result

    def _run_claude_mcp_remove(
        self,
        name: str,
        *,
        home: str,
        scope: str,
    ) -> subprocess.CompletedProcess[str] | None:
        """Run `claude mcp remove --scope <scope> <name>` for rollback. Returns the
        CompletedProcess (does NOT raise on nonzero — the caller tolerates an
        already-absent server so rollback stays idempotent). Returns None if the
        subprocess could not be spawned at all."""
        argv = ["claude", "mcp", "remove", "--scope", scope, name]
        env = {**os.environ, "HOME": home}
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _unit_fire_monotonic(self) -> float:
        return time.monotonic()

    def _unit_fire_sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _remaining_timeout_seconds(self, deadline: float) -> float:
        remaining = deadline - self._unit_fire_monotonic()
        if remaining <= 0:
            raise OperationFailed("unit-fire budget exhausted")
        return remaining

    def _run_unit_fire_subprocess(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        phase: str,
        classification: str,
        start_count: int,
        cleanup_state: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise _unit_fire_operation_failed(
                "unit-fire subprocess timed out",
                phase=phase,
                classification=classification,
                snapshot=None,
                start_count=start_count,
                cleanup_state=cleanup_state,
            ) from exc

    def _unit_fire_show(
        self,
        unit: str,
        *,
        timeout: float,
        phase: str,
        classification: str,
        start_count: int,
        cleanup_state: str,
    ) -> UnitFirePollSnapshot:
        argv = ["systemctl", "--user", "show", unit]
        for item in ("InvocationID", "ActiveState", "SubState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"):
            argv.extend(("-p", item))
        result = self._run_unit_fire_subprocess(
            argv,
            timeout=timeout,
            phase=phase,
            classification=classification,
            start_count=start_count,
            cleanup_state=cleanup_state,
        )
        stdout = result.stdout or ""
        if result.returncode != 0:
            raise OperationFailed((result.stderr or stdout).strip() or "unit-fire state unavailable")
        values: dict[str, str] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            if line.count("=") != 1:
                raise OperationFailed("unit-fire show output is malformed")
            key, value = line.split("=", 1)
            if key in values:
                raise OperationFailed("unit-fire show output contains duplicate keys")
            if key in {"InvocationID", "ActiveState", "SubState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"}:
                values[key] = value.strip()
        for required in ("InvocationID", "ActiveState", "Result", "ExecMainStatus"):
            if required not in values:
                raise OperationFailed("unit-fire show output missing required keys")
        invocation_id = values.get("InvocationID", "")
        return UnitFirePollSnapshot(
            active_state=values.get("ActiveState", ""),
            substate=values.get("SubState", ""),
            result=values.get("Result", ""),
            exec_main_status=values.get("ExecMainStatus", ""),
            success_exit_status=values.get("SuccessExitStatus", ""),
            invocation_id=invocation_id,
            invocation_id_valid=_valid_systemd_invocation_id(invocation_id),
            job=values.get("Job", ""),
            stdout_length=len(stdout.encode("utf-8")),
            stdout_sha256=_sha256_bytes(stdout.encode("utf-8")),
        )

    def _prove_unit_fire_pre_enqueue_idle(
        self,
        unit: str,
        *,
        timeout: float,
    ) -> UnitFirePollSnapshot:
        snapshot = self._unit_fire_show(
            unit,
            timeout=timeout,
            phase="pre-enqueue",
            classification="state-unavailable",
            start_count=0,
            cleanup_state="not-owned",
        )
        job = snapshot.job.strip()
        no_job = job in {"", "0", "n/a", "none", "None"}
        if snapshot.active_state not in {"inactive", "failed"} or snapshot.invocation_id or not no_job:
            raise _unit_fire_operation_failed(
                "consume service is not idle before disabled first fire",
                phase="pre-enqueue",
                classification="pre-existing-invocation",
                snapshot=snapshot,
                start_count=0,
                cleanup_state="not-owned",
            )
        return snapshot

    def _poll_unit_fire_snapshot(
        self,
        unit: str,
        *,
        observation_deadline: float,
        phase: str,
        classification: str,
        start_count: int,
        cleanup_state: str,
    ) -> UnitFirePollSnapshot:
        return self._unit_fire_show(
            unit,
            timeout=self._remaining_timeout_seconds(observation_deadline),
            phase=phase,
            classification=classification,
            start_count=start_count,
            cleanup_state=cleanup_state,
        )

    def _sleep_until_next_unit_fire_poll(self, deadline: float) -> None:
        remaining = deadline - self._unit_fire_monotonic()
        if remaining <= 0:
            return
        self._unit_fire_sleep(min(UNIT_FIRE_POLL_INTERVAL_SECONDS, remaining))

    def _stop_and_prove_unit_fire_inactive(
        self,
        unit: str,
        *,
        total_deadline: float,
    ) -> UnitFirePollSnapshot:
        timeout = self._remaining_timeout_seconds(total_deadline)
        stop = self._run_unit_fire_subprocess(
            ("systemctl", "--user", "stop", unit),
            timeout=timeout,
            phase="cleanup",
            classification="unsafe-cleanup",
            start_count=1,
            cleanup_state="stop-timeout",
        )
        if stop.returncode != 0:
            raise OperationFailed((stop.stderr or stop.stdout).strip() or "failed to stop unit-fire service")
        last: UnitFirePollSnapshot | None = None
        while self._unit_fire_monotonic() < total_deadline:
            try:
                last = self._unit_fire_show(
                    unit,
                    timeout=self._remaining_timeout_seconds(total_deadline),
                    phase="cleanup",
                    classification="unsafe-cleanup",
                    start_count=1,
                    cleanup_state="show-timeout",
                )
            except OperationFailed as exc:
                if exc.evidence:
                    raise exc
                raise _unit_fire_operation_failed(
                    str(exc),
                    phase="cleanup",
                    classification="unsafe-cleanup",
                    snapshot=None,
                    start_count=1,
                    cleanup_state="unavailable",
                ) from exc
            if last.active_state == "inactive":
                return last
            if last.active_state == "failed":
                raise _unit_fire_operation_failed(
                    "unit-fire cleanup proved failed state instead of inactive",
                    phase="cleanup",
                    classification="unsafe-cleanup",
                    snapshot=last,
                    start_count=1,
                    cleanup_state="failed",
                )
            self._sleep_until_next_unit_fire_poll(total_deadline)
        raise _unit_fire_operation_failed(
            "unit-fire cleanup could not prove inactive",
            phase="cleanup",
            classification="unsafe-cleanup",
            snapshot=last,
            start_count=1,
            cleanup_state="unsafe-active",
        )

    def _current_account_only(self, account: str) -> PreflightResult:
        try:
            target = pwd.getpwnam(account)
        except KeyError:
            return PreflightResult(False, "missing", "Unix account does not exist")
        if target.pw_uid != os.geteuid():
            return PreflightResult(
                False,
                "operator-required",
                "local backend must run inside the target account",
            )
        return PreflightResult(True, "current-account", f"uid={target.pw_uid}")

    def _identity_ready(self, operation: InstallOperation) -> PreflightResult:
        account = self._current_account_only(operation.owner)
        if not account.ok:
            return account
        user = pwd.getpwnam(operation.owner)
        group_name = operation.desired["mailbox_group"]
        try:
            group = grp.getgrnam(group_name)
        except KeyError:
            return PreflightResult(
                False,
                "missing",
                f"mailbox group {group_name!r} does not exist",
            )
        group_ids = os.getgrouplist(operation.owner, user.pw_gid)
        if group.gr_gid not in group_ids:
            return PreflightResult(
                False,
                "missing",
                f"{operation.owner} is not a member of {group_name}",
            )
        linger = self._run(
            ("loginctl", "show-user", operation.owner, "-p", "Linger", "--value")
        )
        if linger.returncode != 0 or linger.stdout.strip().lower() != "yes":
            return PreflightResult(False, "inactive", "login linger is not enabled")
        manager = self._run(("systemctl", "is-active", f"user@{user.pw_uid}.service"))
        if manager.returncode != 0 or manager.stdout.strip() != "active":
            return PreflightResult(
                False,
                "inactive",
                f"user@{user.pw_uid}.service is not active",
            )
        return PreflightResult(
            True,
            "identity",
            f"uid={user.pw_uid} group={group_name} linger=yes user-manager=active",
        )

    def _manager_ready(self) -> PreflightResult:
        result = self._run(("systemctl", "--user", "show-environment"))
        if result.returncode != 0:
            return PreflightResult(
                False,
                "unavailable",
                (result.stderr or result.stdout).strip() or "user manager unavailable",
            )
        return PreflightResult(True, "active", "target user manager reachable")

    def _timer_enablement_paths_from_desired(
        self,
        operation: InstallOperation,
    ) -> tuple[Path, Path]:
        timer = operation.desired.get("timer") if operation.kind == "timer-enablement-absence-check" else operation.target
        if not isinstance(timer, str) or not timer:
            raise OperationFailed("timer enablement desired timer missing")
        raw_template = operation.desired.get("canonical_template_path")
        raw_link = operation.desired.get("enablement_link_path") or operation.target
        if not raw_template or not raw_link:
            raise OperationFailed("timer enablement desired paths missing")
        canonical = _lexically_normalize_absolute(Path(raw_template))
        link = _lexically_normalize_absolute(Path(raw_link))
        try:
            account_home = canonical.parents[3]
        except IndexError as exc:
            raise OperationFailed("timer enablement canonical template path is malformed") from exc
        if canonical != _consume_timer_template_path(account_home, timer):
            raise OperationFailed("timer enablement canonical template path does not match account-derived path")
        if link != _consume_timer_enablement_link_path(account_home, timer):
            raise OperationFailed("timer enablement link path does not match account-derived path")
        return link, canonical

    def _timer_enablement_paths_from_mutation(
        self,
        mutation: MutationRecord,
    ) -> tuple[Path, Path]:
        raw_link = mutation.before.get("enablement_link_path")
        raw_template = mutation.before.get("canonical_template_path")
        if not raw_link or not raw_template:
            raise OperationFailed("timer enablement receipt lacks link ownership evidence")
        link = _lexically_normalize_absolute(Path(raw_link))
        canonical = _lexically_normalize_absolute(Path(raw_template))
        try:
            account_home = canonical.parents[3]
        except IndexError as exc:
            raise OperationFailed("timer enablement receipt canonical path is malformed") from exc
        if canonical != _consume_timer_template_path(account_home, mutation.target):
            raise OperationFailed("timer enablement receipt canonical path does not match account-derived path")
        if link != _consume_timer_enablement_link_path(account_home, mutation.target):
            raise OperationFailed("timer enablement receipt link path does not match account-derived path")
        return link, canonical

    def _require_timer_enablement_link_absent(
        self,
        owner: str,
        timer_unit: str,
        *,
        link_path: Path,
        canonical_template_path: Path,
        context: str,
    ) -> TimerEnablementLinkProof:
        del owner, timer_unit
        proof = _timer_enablement_link_proof(link_path, canonical_template_path)
        if proof.snapshot_state != "absent":
            raise OperationFailed(f"{context}: consume timer enablement link already exists")
        return proof

    def _require_timer_enablement_link_matches_receipt(
        self,
        mutation: MutationRecord,
    ) -> TimerEnablementLinkProof:
        link, canonical = self._timer_enablement_paths_from_mutation(mutation)
        proof = _timer_enablement_link_proof(link, canonical)
        if proof.snapshot_state == "absent":
            return proof
        if proof.snapshot_state != "symlink":
            raise OperationFailed("timer enablement link is not a symlink")
        if not _timer_enablement_link_matches_canonical(proof):
            raise OperationFailed("timer enablement link does not resolve to the receipt canonical template")
        recorded = mutation.before.get("enablement_link_actual_readlink", "")
        if recorded and proof.actual_readlink != recorded:
            raise OperationFailed("timer enablement link readlink bytes changed after receipt")
        recorded_sha = mutation.before.get("enablement_link_actual_readlink_sha256", "")
        if recorded_sha and proof.actual_readlink_sha256 != recorded_sha:
            raise OperationFailed("timer enablement link readlink digest changed after receipt")
        return proof

    def _remove_receipt_owned_timer_enablement_link(
        self,
        mutation: MutationRecord,
    ) -> None:
        proof = self._require_timer_enablement_link_matches_receipt(mutation)
        if proof.snapshot_state == "absent":
            return
        proof.link_path.unlink()
        absent = _timer_enablement_link_proof(proof.link_path, proof.canonical_template_path)
        if absent.snapshot_state != "absent":
            raise OperationFailed("timer enablement link removal did not converge")

    def _timer_unit_state(self, unit: str) -> TimerUnitState:
        result = self._run(
            (
                "systemctl",
                "--user",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "UnitFileState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
            )
        )
        if result.returncode != 0:
            raise OperationFailed((result.stderr or result.stdout).strip() or "timer state unavailable")
        return _parse_systemctl_timer_show(result.stdout)

    def _legacy_convergence_evidence(
        self,
        operation: InstallOperation,
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """Probe whether an already-enabled consume timer's LIVE shape
        satisfies the invariants the disabled-first-fire ->
        timer-observe apply path proves: link canonicality; timer
        active+waiting+finite-next-elapse; service inactive+success
        (see timer-observe's own apply at _systemd_show calls near
        "consume service is not inactive successful after timer arm" /
        "timer is not active and waiting after arm" / "next timer elapse is
        not finite after inactivity"). T-3105/T-3106: the exit-status
        invariant is Result=="success" AND the exit code within the unit's
        success policy ({0} u SuccessExitStatus) via
        _exec_status_within_success_policy -- a consume service may map benign
        skip codes to success via SuccessExitStatus (recovery's is 3 4 5, exits 5
        idle), and re-deriving membership (rather than the T-3087
        ExecMainStatus=="0" hardcode, or T-3105's Result-only trust) both
        admits that mapped-success exit AND rejects a reset-failed-masked
        out-of-policy failure whose Result was cleared to success while
        ExecMainStatus stayed stale. NOTE: the active first-fire / fresh-install
        path still hardcodes exit-0 at THREE sites (the "consume service is not
        inactive successful after timer arm" gate, the disabled-first-fire
        completion gate, and verify()'s acceptance aggregation); making the
        whole fresh path SuccessExitStatus-aware needs snapshot/evidence
        plumbing plus a semantic call on whether an idle first fire (exit 5)
        counts as a successful first fire -- tracked as a fleet-wide follow-up,
        NOT closed here. This T-3106 change hardens the legacy-convergence probe
        only. Lets a pre-existing enablement this backend never drove
        (e.g. a bespoke legacy install) be admitted rather than rejected as
        drift.

        Returns None if any invariant fails -- callers must keep rejecting
        in that case. Fields this backend cannot honestly reconstruct from a
        live probe (the historical first-fire InvocationID; the mailbox-
        probe correlation, which requires an actual probe hash that was
        never captured for a legacy timer) are marked "legacy-unknown" or
        left empty rather than fabricated -- see T-3087 DECISIONS for why
        that's not relaxed even though it means verify()'s acceptance-
        evidence gate (3006-3021, untouched by this patch) will not pass
        for a legacy-admitted plan if invoked against this same process
        (the legacy-unknown mailbox/SHA fields, and now also the recorded
        exit status when it is a mapped-success non-zero code).
        """
        timer = operation.desired.get("timer")
        if not isinstance(timer, str) or not timer.endswith(".timer"):
            return None
        try:
            service = self._consume_service_for_timer(timer)
        except OperationFailed:
            return None
        try:
            link, canonical = self._timer_enablement_paths_from_desired(operation)
        except OperationFailed:
            try:
                account_home = Path(pwd.getpwnam(operation.owner).pw_dir)
            except KeyError:
                return None
            link = _consume_timer_enablement_link_path(account_home, timer)
            canonical = _consume_timer_template_path(account_home, timer)
        proof = _timer_enablement_link_proof(link, canonical)
        if not _timer_enablement_link_matches_canonical(proof):
            return None
        try:
            # T-3211: sample the SERVICE first and settle it (if in-flight)
            # BEFORE sampling the TIMER. Do not sample the timer first and
            # reuse it across a settle wait: for an OnUnitInactiveSec timer,
            # NextElapseUSec* is computed from the unit's LAST inactivation,
            # so a sample taken WHILE the service is still running can
            # legitimately read no finite next elapse at all (systemd has not
            # yet recomputed it against a new inactivation) -- an earlier
            # timer sample is not safe to rely on across the wait. This is
            # confirmed, not assumed: recovery's ticket log (T-3211, "MID-FIRE
            # TIMER CAPTURE", 0.2s-resolution poll through a real fire) shows
            # the TIMER reading ActiveState=active SubState=running
            # NextElapseUSecMonotonic=infinity while the consume SERVICE was
            # activating, settling to SubState=waiting with a finite
            # monotonic elapse only once the service went inactive -- a
            # pre-settle timer sample fails BOTH the waiting check and
            # _finite_next_elapse_basis ("infinity" is in its invalid set).
            # Sampling the timer only after the service has settled (or the
            # window is exhausted) means the timer checks below always see a
            # post-settle snapshot.
            service_values = self._systemd_show(
                service,
                ("InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"),
            )
            if _legacy_convergence_service_in_flight(service_values):
                service_values = self._await_legacy_convergence_service_settle(service, service_values)
            timer_values = self._systemd_show(
                timer,
                ("ActiveState", "SubState", "NextElapseUSecRealtime", "NextElapseUSecMonotonic"),
            )
        except OperationFailed:
            return None
        if timer_values.get("ActiveState") != "active" or timer_values.get("SubState") != "waiting":
            return None
        next_elapse_basis = _finite_next_elapse_basis(timer_values)
        if next_elapse_basis is None:
            return None
        # T-3105/T-3106: the exit-status invariant is Result=="success" AND the exit
        # code within the unit's success policy ({0} u SuccessExitStatus). A consume
        # service may map benign skip codes to success via SuccessExitStatus (recovery's
        # pantheon-persona-consume@recovery.service is configured SuccessExitStatus=3 4 5
        # and exits 5 on an idle cycle) -- a hardcoded ExecMainStatus=="0" (T-3087)
        # spuriously rejected that converged legacy timer. But trusting Result alone
        # (T-3105) let a reset-failed-masked failure through: `systemctl reset-failed`
        # clears Result to success yet leaves ExecMainStatus stale, and it is applied
        # to consume units, so a genuinely out-of-policy failure could read
        # Result=success at a point-in-time cutover probe. Re-deriving membership
        # against SuccessExitStatus (T-3106) admits recovery's exit-5 while rejecting a
        # reset-masked exit-1. ActiveState, Result, link canonicality, timer
        # active+waiting, and finite next-elapse are unchanged.
        if (
            service_values.get("ActiveState") != "inactive"
            or service_values.get("Result") != "success"
            or not _exec_status_within_success_policy(
                service_values.get("ExecMainStatus", ""),
                service_values.get("SuccessExitStatus", ""),
            )
        ):
            return None
        current_invocation = service_values.get("InvocationID", "")
        unit_fire_evidence = {
            "invocation_id": current_invocation,
            "active_state": service_values.get("ActiveState", ""),
            "result": service_values.get("Result", ""),
            "exec_main_status": service_values.get("ExecMainStatus", ""),
            # T-3107: carry the success policy into the evidence so the hybrid
            # verify() acceptance gate admits a legacy-converged in-policy non-zero
            # exit (recovery's idle-exit-5) on the re-verify path too, consistent with
            # this probe's own preflight admit. Without it that gate defaults the
            # policy to "" and rejects any non-zero exit.
            "success_exit_status": service_values.get("SuccessExitStatus", ""),
            "post_exit_invocation_id_state": "legacy-unknown",
            "timer_enabled": "true",
            **_timer_enablement_link_evidence("legacy", proof),
        }
        timer_observe_evidence = {
            "next_elapse_basis": next_elapse_basis,
            "next_elapse_finite": "true",
            "first_fire_invocation_observed": "true",
            "second_consume_absent": "true",
            "mailbox_effect_correlated": "legacy-unknown",
            "mailbox_probe_sha256": "",
            "unit_fire_invocation_id": current_invocation,
            "post_exit_invocation_id_state": "legacy-unknown",
            "initial_viability_receipt_sha256": "",
        }
        return unit_fire_evidence, timer_observe_evidence

    def _await_legacy_convergence_service_settle(
        self,
        service: str,
        initial: Mapping[str, str],
    ) -> dict[str, str]:
        """Re-sample the consume SERVICE while it is mid-fire, bounded by
        LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS (T-3211).

        A legacy-convergence probe samples the live systemd shape at a single
        instant; if that instant lands during the consume oneshot's own
        activating/active/deactivating window (or with a queued Job), the
        service has not yet reported its settled InvocationID/Result/
        ExecMainStatus and a single-sample probe would spuriously reject a
        seat whose timer is, in fact, converged. Poll at
        LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS via the
        class's time seams (so tests can fake both) until the service is no
        longer in flight or the window is exhausted.

        Returns the LAST sampled values regardless of outcome -- settled
        success, settled failure, or still in flight when the window closes.
        The caller's existing inactive+success+in-policy check (unchanged)
        rejects the latter two identically to how it already rejects any
        non-converged sample today; this method does not duplicate that
        decision.
        """
        deadline = self._unit_fire_monotonic() + LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS
        values = dict(initial)
        while _legacy_convergence_service_in_flight(values) and self._unit_fire_monotonic() < deadline:
            self._unit_fire_sleep(LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS)
            values = self._systemd_show(
                service,
                ("InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"),
            )
        return values

    def _seed_legacy_convergence_events(
        self,
        unit_fire_evidence: Mapping[str, str],
        timer_observe_evidence: Mapping[str, str],
    ) -> None:
        detail = "existing canonical enablement matches converged shape"
        self.events["connective.unit-fire"] = OperationEvidence(
            "connective.unit-fire", 1, "unit-fire", True, detail, dict(unit_fire_evidence)
        )
        self.events["connective.timer-observe"] = OperationEvidence(
            "connective.timer-observe", 1, "timer-observe", True, detail, dict(timer_observe_evidence)
        )

    def _consume_service_for_timer(self, timer_unit: str) -> str:
        if not timer_unit.endswith(".timer"):
            raise OperationFailed("timer rollback target is not a timer unit")
        return timer_unit[: -len(".timer")] + ".service"

    def _unit_inactive_dead(self, unit: str) -> TimerUnitState:
        result = self._run(
            (
                "systemctl",
                "--user",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "UnitFileState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
            )
        )
        if result.returncode != 0:
            raise OperationFailed((result.stderr or result.stdout).strip() or f"{unit} state unavailable")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line == "":
                continue
            if line.count("=") != 1:
                raise OperationFailed(f"{unit} show output is malformed")
            key, value = line.split("=", 1)
            if key not in {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
                raise OperationFailed(f"{unit} show output contains unexpected keys")
            if key in values:
                raise OperationFailed(f"{unit} show output contains duplicate keys")
            values[key] = value
        if set(values) != {"LoadState", "UnitFileState", "ActiveState", "SubState"}:
            raise OperationFailed(f"{unit} show output missing required keys")
        classification: Literal["enabled", "disabled", "not-found"]
        if values["LoadState"] == "not-found":
            classification = "not-found"
        else:
            classification = "enabled" if values["UnitFileState"] == "enabled" else "disabled"
        return TimerUnitState(
            classification=classification,
            load_state=values["LoadState"],
            unit_file_state=values["UnitFileState"],
            active_state=values["ActiveState"],
            sub_state=values["SubState"],
        )

    def _prove_unit_inactive_dead(self, unit: str) -> TimerUnitState:
        state = self._unit_inactive_dead(unit)
        if state.active_state != "inactive" or state.sub_state != "dead":
            raise OperationFailed(
                f"{unit} did not prove inactive/dead: "
                f"LoadState={state.load_state} UnitFileState={state.unit_file_state} "
                f"ActiveState={state.active_state} SubState={state.sub_state}"
            )
        return state

    def _prove_unit_notfound_inactive_dead(self, unit: str) -> TimerUnitState:
        state = self._unit_inactive_dead(unit)
        if (
            state.load_state != "not-found"
            or state.unit_file_state not in {"", "not-found"}
            or state.active_state != "inactive"
            or state.sub_state != "dead"
        ):
            raise OperationFailed(
                f"{unit} did not prove not-found/inactive/dead: "
                f"LoadState={state.load_state} UnitFileState={state.unit_file_state} "
                f"ActiveState={state.active_state} SubState={state.sub_state}"
            )
        return state

    def _stop_unit_or_converged_not_loaded(self, unit: str) -> TimerUnitState:
        stopped = self._run(("systemctl", "--user", "stop", unit))
        if stopped.returncode == 0:
            return self._prove_unit_inactive_dead(unit)
        try:
            return self._prove_unit_notfound_inactive_dead(unit)
        except OperationFailed as exc:
            evidence = _completed_process_bounded_evidence("stop", stopped)
            raise OperationFailed(
                f"{unit} stop failed and did not converge to not-found/inactive/dead: {exc}",
                evidence=evidence,
            ) from exc

    def _stop_and_prove_inactive_dead(self, unit: str) -> TimerUnitState:
        stopped = self._run(("systemctl", "--user", "stop", unit))
        if stopped.returncode != 0:
            raise OperationFailed((stopped.stderr or stopped.stdout).strip() or f"{unit} stop failed")
        return self._prove_unit_inactive_dead(unit)

    def preflight(self, operation: InstallOperation) -> PreflightResult:
        if operation.kind == "brief-check":
            return _brief_readability_preflight(operation)
        if operation.provenance.get("install_root") != "/":
            return PreflightResult(
                False,
                "operator-required",
                "local backend activates only a root=/ plan",
            )
        if operation.kind == "mkdir":
            path = Path(operation.target)
            if not path.exists():
                return PreflightResult(True, "absent", "will create directory")
            if path.is_dir():
                return PreflightResult(True, "directory", "directory already exists")
            return PreflightResult(False, "conflicting", "non-directory exists")
        if operation.kind in {"atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            path = Path(operation.target)
            snap = _snapshot(path)
            if snap["state"] == "absent":
                return PreflightResult(True, "absent", "will create managed file")
            if snap["state"] != "file":
                return PreflightResult(False, "conflicting", "target is not a regular file")
            if snap.get("fingerprint") == operation.desired["sha256"]:
                return PreflightResult(True, "matching", "desired content already installed")
            content = base64.b64decode(snap["content_b64"]).decode("utf-8", "replace")
            if _is_installer_managed(content):
                return PreflightResult(True, "managed", "installer-owned file may converge")
            return PreflightResult(False, "conflicting", "unknown regular file")
        if operation.kind == "identity-check":
            return self._identity_ready(operation)
        if operation.kind == "source-check":
            source = Path(operation.target)
            artifact_paths = tuple(
                Path(item)
                for item in operation.desired.get("artifacts", "").split(",")
                if item
            )
            try:
                ok = (
                    source.is_dir()
                    and _git_head(source) == operation.desired.get("revision")
                    and _artifact_digest(source, artifact_paths)
                    == operation.desired.get("artifact_sha256")
                )
            except ManifestError:
                ok = False
            return PreflightResult(
                ok,
                "source",
                (
                    "framework revision and artifact digest match"
                    if ok
                    else "framework source drifted from reviewed plan"
                ),
            )
        if operation.kind == "policy-check":
            bundle = [entry for entry in operation.desired.get("bundle", "").split(",") if entry]
            missing = [entry for entry in bundle if not (Path(operation.target) / entry).exists()]
            return PreflightResult(
                not missing,
                "policy",
                "policy bundle present" if not missing else f"missing {missing}",
            )
        if operation.kind == "harness-check":
            command = "claude" if operation.target == "claude" else "codex"
            ok = shutil.which(command) is not None
            return PreflightResult(
                ok,
                "harness",
                f"{command} found" if ok else f"{command} missing",
            )
        # claude-mcp-server-* are pure ~/.claude.json reads at preflight (the
        # register WRITE happens at execute via a HOME-pinned `claude mcp add-json`,
        # guarded there by OS permissions + a fail-closed re-read). They need
        # NEITHER the systemd manager gate NOR the current-account gate below, so
        # they early-return here alongside mkdir/atomic-write rather than falling
        # through to gates that don't apply (and that fail on a CI runner with no
        # systemd user session / no matching persona account).
        if operation.kind == "claude-mcp-server-check":
            try:
                verdict = _claude_mcp_verdict(
                    _read_claude_user_mcp_servers(operation.target), operation
                )
            except OperationFailed as exc:
                return PreflightResult(False, "unreadable", str(exc))
            # Read-only audit: always available, verdict rides the classification.
            return PreflightResult(True, verdict, _CLAUDE_MCP_VERDICT_DETAIL[verdict])
        if operation.kind == "claude-mcp-server-registration":
            try:
                verdict = _claude_mcp_verdict(
                    _read_claude_user_mcp_servers(operation.target), operation
                )
            except OperationFailed as exc:
                return PreflightResult(False, "unreadable", str(exc))
            if verdict == "conflicting":
                return PreflightResult(
                    False, "conflicting", "existing Claude MCP server differs from manifest"
                )
            if verdict == "matching":
                return PreflightResult(
                    True, "matching", "Claude MCP server already registered"
                )
            return PreflightResult(True, "absent", "will register Claude MCP server via CLI")
        if operation.kind == "unit-fire":
            try:
                _parse_unit_fire_timeout_budget(operation)
                _parse_unit_fire_initial_viability_desired(operation)
            except OperationFailed as exc:
                return PreflightResult(False, "unit-fire", str(exc))
        account = self._current_account_only(operation.owner)
        if not account.ok:
            return account
        manager = self._manager_ready()
        if not manager.ok:
            return manager
        if operation.kind == "timer-enablement-absence-check":
            try:
                link, canonical = self._timer_enablement_paths_from_desired(operation)
                proof = _timer_enablement_link_proof(link, canonical)
                if proof.snapshot_state != "absent":
                    evidence = self._legacy_convergence_evidence(operation)
                    if evidence is None:
                        raise OperationFailed(f"{operation.operation_id}: consume timer enablement link already exists")
                    self._seed_legacy_convergence_events(*evidence)
                    return PreflightResult(True, "converged-legacy", "existing canonical enablement matches converged shape")
            except OperationFailed as exc:
                return PreflightResult(False, "conflicting", str(exc))
            return PreflightResult(True, "absent", "consume timer enablement link is absent")
        if operation.kind == "unit-fire":
            enabled = self._run(
                ("systemctl", "--user", "is-enabled", operation.desired["timer"])
            )
            if enabled.returncode == 0 and "connective.timer-observe" not in self.events:
                evidence = self._legacy_convergence_evidence(operation)
                if evidence is None:
                    return PreflightResult(
                        False,
                        "conflicting",
                        "timer is already enabled without this backend's convergence evidence",
                    )
                self._seed_legacy_convergence_events(*evidence)
        if operation.kind == "timer-enable":
            try:
                link, canonical = self._timer_enablement_paths_from_desired(operation)
                timer_state = self._timer_unit_state(operation.target)
                proof = _timer_enablement_link_proof(link, canonical)
                if timer_state.classification == "enabled":
                    if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                        return PreflightResult(False, "conflicting", "enabled timer link does not resolve to canonical template")
                else:
                    self._require_timer_enablement_link_absent(
                        operation.owner,
                        operation.target,
                        link_path=link,
                        canonical_template_path=canonical,
                        context=operation.operation_id,
                    )
            except OperationFailed as exc:
                return PreflightResult(False, "conflicting", str(exc))
        if operation.kind == "mailbox-writable-root-check":
            try:
                verify_maildir_writable_root(Path(operation.target))
            except MailboxWritableRootError as exc:
                return PreflightResult(False, "mailbox-writable-root", str(exc))
            return PreflightResult(True, "check", "mailbox writable root is usable")
        if operation.kind == "codex-telegram-source-check":
            try:
                source = _bridge_source_from_operation(operation)
                codex_telegram_bridge.verify_archive(_bridge_archive_path_from_operation(operation), source)
            except Exception as exc:
                return PreflightResult(False, "source-archive", str(exc))
            return PreflightResult(True, "source-archive", "pinned source archive is readable and verified")
        if operation.kind == "codex-telegram-mcp-check":
            path = Path(operation.target)
            snap = _snapshot(path)
            if snap["state"] == "absent":
                return PreflightResult(True, "absent", "will create MCP registration")
            if snap["state"] == "file":
                try:
                    match = _mcp_existing_matches(path.read_text(encoding="utf-8"), operation)
                except OperationFailed as exc:
                    return PreflightResult(False, "conflicting", str(exc))
                if match is True:
                    return PreflightResult(True, "matching", "MCP registration already matches")
                if match is None:
                    return PreflightResult(True, "absent", "will append MCP registration table")
            return PreflightResult(False, "conflicting", "MCP registration mismatch")
        if operation.kind == "codex-telegram-disabled-first":
            values = self._systemd_show(
                operation.target,
                ("ActiveState", "MainPID"),
            )
            enabled = self._run(("systemctl", "--user", "is-enabled", operation.target))
            unit = self._run(("systemctl", "--user", "cat", operation.target))
            active_state = values.get("ActiveState", "")
            main_pid = values.get("MainPID", "")
            if enabled.returncode == 0:
                return PreflightResult(False, "conflicting", "codex telegram receiver is enabled before cutover")
            if active_state not in {"inactive", "failed"} or main_pid not in {"", "0"}:
                return PreflightResult(False, "conflicting", "codex telegram receiver is active before cutover")
            if unit.returncode == 0 and "EnvironmentFile" in unit.stdout:
                return PreflightResult(False, "conflicting", "codex telegram unit must not embed an EnvironmentFile")
            return PreflightResult(True, "disabled", "codex telegram receiver is disabled before cutover")
        if operation.kind == "codex-standing-instructions-source-check":
            path = Path(operation.target)
            try:
                ok = (
                    path.is_file()
                    and _sha256_bytes(path.read_bytes()) == operation.desired["source_sha256"]
                )
                commit = operation.desired.get("source_commit", "")
                if ok and commit:
                    result = self._run(("git", "-C", str(path.parents[1]), "cat-file", "-e", f"{commit}^{{commit}}"))
                    ok = result.returncode == 0
            except OSError:
                ok = False
            return PreflightResult(
                ok,
                "source",
                "Codex standing instructions source path, commit, and hash match"
                if ok
                else "Codex standing instructions source proof failed",
            )
        if operation.kind == "codex-standing-instructions-target-check":
            return manager
        if operation.kind == "codex-standing-instructions-override-check":
            path = Path(operation.target)
            try:
                ok = not path.exists() or not path.read_text(encoding="utf-8").strip()
            except OSError:
                ok = False
            return PreflightResult(
                ok,
                "absent-or-empty" if ok else "conflicting",
                "Codex override absent or empty" if ok else "Codex override file is non-empty",
            )
        return manager

    def _write_file(self, operation: InstallOperation) -> MutationRecord:
        path = Path(operation.target)
        before = _snapshot(path)
        raw = _desired_content(operation)
        changed = before.get("fingerprint") != _sha256_bytes(raw)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, int(operation.desired.get("mode", "0644"), 8))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        after = _snapshot(path)
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=changed,
            before=before,
            after_fingerprint=_snapshot_fingerprint(after),
        )

    def _simple_mutation(self, operation: InstallOperation) -> MutationRecord:
        path = Path(operation.target)
        before = _snapshot(path)
        changed = False
        if operation.kind == "mkdir" and before["state"] == "absent":
            path.mkdir()
            os.chmod(path, int(operation.desired.get("mode", "0755"), 8))
            changed = True
        after = _snapshot(path)
        return MutationRecord(
            operation.operation_id,
            operation.kind,
            operation.target,
            operation.owner,
            changed,
            before,
            _snapshot_fingerprint(after),
        )

    def _systemd_show(self, unit: str, properties: Sequence[str]) -> dict[str, str]:
        argv = ["systemctl", "--user", "show", unit]
        for item in properties:
            argv.extend(("-p", item))
        result = self._run(argv)
        if result.returncode != 0:
            raise OperationFailed((result.stderr or result.stdout).strip())
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def _stale_unit_failure_reset_show(
        self,
        unit: str,
    ) -> StaleUnitFailureResetSnapshot:
        argv = ["systemctl", "--user", "show", unit, "--all"]
        for item in STALE_UNIT_FAILURE_RESET_PROPERTIES:
            argv.extend(("-p", item))
        result = self._run(argv)
        stdout = result.stdout or ""
        if result.returncode != 0:
            raise _stale_unit_failure_reset_failure(
                (result.stderr or stdout).strip() or "stale unit state unavailable"
            )
        try:
            return _parse_stale_unit_failure_reset_show(stdout)
        except OperationFailed as exc:
            if exc.evidence:
                raise
            raise _stale_unit_failure_reset_failure(str(exc)) from exc

    def _execute_stale_unit_failure_reset(
        self,
        operation: InstallOperation,
    ) -> tuple[MutationRecord, OperationEvidence]:
        pre = self._stale_unit_failure_reset_show(operation.target)
        try:
            classification = _classify_stale_unit_failure_reset_snapshot(pre)
        except OperationFailed as exc:
            raise _stale_unit_failure_reset_failure(str(exc), pre=pre) from exc
        evidence = _stale_unit_failure_reset_snapshot_evidence("pre", pre)
        if classification == "clean-noop":
            before = {"state": "clean-noop"}
            evidence.update(
                {
                    "classification": "noop",
                    "pre_state": "clean-noop",
                    "post_state": "clean-noop",
                }
            )
        else:
            reset = self._run(
                ("systemctl", "--user", "reset-failed", operation.target)
            )
            if reset.returncode != 0:
                raise _stale_unit_failure_reset_failure(
                    (reset.stderr or reset.stdout).strip() or "stale unit reset-failed failed",
                    pre=pre,
                    reset_returncode=reset.returncode,
                    reset_stdout=reset.stdout or "",
                    reset_stderr=reset.stderr or "",
                )
            post = self._stale_unit_failure_reset_show(operation.target)
            try:
                post_classification = _classify_stale_unit_failure_reset_snapshot(post)
            except OperationFailed as exc:
                raise _stale_unit_failure_reset_failure(str(exc), pre=pre, post=post) from exc
            if post_classification != "clean-noop":
                raise _stale_unit_failure_reset_failure(
                    "stale unit reset-failed left residual state",
                    pre=pre,
                    post=post,
                )
            before = {"state": "reset-required"}
            evidence.update(_stale_unit_failure_reset_snapshot_evidence("post", post))
            evidence.update(
                {
                    "classification": "reset",
                    "pre_state": "reset-required",
                    "post_state": "clean-noop",
                }
            )
        mutation = MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=False,
            before=before,
            after_fingerprint="evidence-only",
        )
        observed = OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            True,
            "consume service stale failure reset gate passed",
            evidence,
        )
        self.events[operation.operation_id] = observed
        return mutation, observed

    def _mailbox_probe(self, operation: InstallOperation) -> Mapping[str, str]:
        receive = Path(operation.desired["receive_tmp"])
        send = Path(operation.desired["send_tmp"])
        payload = (
            f"pantheon-installer-probe:{operation.provenance['instance']}:"
            f"{operation.provenance['persona']}:{uuid.uuid4()}"
        ).encode("utf-8")
        digest = _sha256_bytes(payload)
        written: list[Path] = []
        try:
            for directory, direction in ((receive, "receive"), (send, "send")):
                name = f".installer-probe-{uuid.uuid4().hex}"
                path = directory / name
                with path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                written.append(path)
                if _sha256_bytes(path.read_bytes()) != digest:
                    raise OperationFailed(f"mailbox {direction} round-trip hash mismatch")
            return {
                "receive": "true",
                "send": "true",
                "durable_round_trip": "true",
                "sha256": digest,
            }
        finally:
            for path in written:
                path.unlink(missing_ok=True)

    def execute(
        self,
        operation: InstallOperation,
        context: ExecutionContext,
    ) -> tuple[MutationRecord, OperationEvidence]:
        mutation = MutationRecord(
            operation.operation_id,
            operation.kind,
            operation.target,
            operation.owner,
            False,
            {"state": "not-applicable"},
            None,
        )
        observed: dict[str, str] = {}
        if operation.kind == "mkdir":
            mutation = self._simple_mutation(operation)
        elif operation.kind in {"atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            mutation = self._write_file(operation)
        elif operation.kind == "stale-unit-failure-reset":
            return self._execute_stale_unit_failure_reset(operation)
        elif operation.kind == "codex-telegram-source-check":
            binding = _bridge_binding_from_operation(operation)
            before = _symlink_snapshot(binding.current_release)
            receipt = codex_telegram_bridge.stage_archive_release(_bridge_archive_path_from_operation(operation), binding)
            after = _symlink_snapshot(binding.current_release)
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                str(binding.current_release),
                operation.owner,
                _snapshot_fingerprint(before) != _snapshot_fingerprint(after),
                before,
                _snapshot_fingerprint(after),
            )
            observed = {
                "revision": str(receipt["revision"]),
                "source_manifest_sha256": str(receipt["source_manifest_sha256"]),
                "allowlist_sha256": str(receipt["allowlist_sha256"]),
                "current": str(receipt["current"]),
                "claude_receiver_topology": operation.desired["claude_receiver_topology"],
                "predecessor_claude_receiver_topology": operation.desired["predecessor_claude_receiver_topology"],
                "tmux_pane": operation.desired["tmux_pane"],
                "effective_pane": operation.desired["effective_pane"],
            }
        elif operation.kind == "codex-telegram-mcp-check":
            path = Path(operation.target)
            before_snapshot = _snapshot(path)
            expected = operation.desired["expected"]
            if path.exists():
                current = path.read_text(encoding="utf-8")
                match = _mcp_existing_matches(current, operation)
                if match is False:
                    raise OperationFailed("existing Codex MCP registration mismatches")
                if match is None:
                    suffix = ("" if current.endswith("\n") else "\n") + "\n" + expected
                    path.write_text(current + suffix, encoding="utf-8")
                    before = {
                        "state": "mcp-registration",
                        "created_file": "false",
                        "prior_fingerprint": before_snapshot.get("fingerprint", ""),
                        "appended_suffix": suffix,
                        "appended_suffix_sha256": _sha256_bytes(suffix.encode("utf-8")),
                    }
                else:
                    before = {
                        "state": "mcp-registration",
                        "created_file": "false",
                        "prior_fingerprint": before_snapshot.get("fingerprint", ""),
                        "appended_suffix": "",
                        "appended_suffix_sha256": "",
                    }
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(expected)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                before = {
                    "state": "mcp-registration",
                    "created_file": "true",
                    "prior_fingerprint": "",
                    "appended_suffix": "",
                    "appended_suffix_sha256": "",
                }
            after = _snapshot(path)
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                before.get("created_file") == "true" or bool(before.get("appended_suffix")),
                before,
                _snapshot_fingerprint(after),
            )
            observed = {
                "name": operation.desired["name"],
                "registered": "true",
                "expected_sha256": _sha256_bytes(expected.encode("utf-8")),
            }
        elif operation.kind == "claude-mcp-server-check":
            # Read-only audit — parse the live map (race-free), record the verdict,
            # never write.
            verdict = _claude_mcp_verdict(
                _read_claude_user_mcp_servers(operation.target), operation
            )
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                False,
                {"state": "not-applicable"},
                None,
            )
            observed = {
                "name": operation.desired["name"],
                "scope": operation.desired["scope"],
                "verdict": verdict,
            }
        elif operation.kind == "claude-mcp-server-registration":
            name = operation.desired["name"]
            home = operation.desired["home"]
            verdict = _claude_mcp_verdict(
                _read_claude_user_mcp_servers(operation.target), operation
            )
            if verdict == "conflicting":
                # Fail-closed refuse — never clobber a differing live entry.
                raise OperationFailed(
                    f"existing Claude MCP server {name!r} differs from manifest"
                )
            if verdict == "matching":
                added = False
                observed = {"name": name, "scope": operation.desired["scope"], "registered": "true"}
            else:
                self._run_claude_mcp(
                    [
                        "add-json",
                        "--scope",
                        operation.desired["scope"],
                        name,
                        operation.desired["config_json"],
                    ],
                    home=home,
                    phase="add_json",
                )
                added = True
                observed = {"name": name, "scope": operation.desired["scope"], "registered": "true"}
            after = _read_claude_user_mcp_servers(operation.target)
            if _claude_mcp_verdict(after, operation) != "matching":
                raise OperationFailed(
                    f"Claude MCP server {name!r} not present/matching after registration"
                )
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                added,
                {
                    "state": "claude-mcp-server",
                    "added": "true" if added else "false",
                    "name": name,
                    "home": home,
                    "scope": operation.desired["scope"],
                    "expected": operation.desired["expected"],
                },
                None,
            )
        elif operation.kind == "codex-telegram-disabled-first":
            values = self._systemd_show(
                operation.target,
                ("ActiveState", "MainPID"),
            )
            enabled = self._run(("systemctl", "--user", "is-enabled", operation.target))
            unit = self._run(("systemctl", "--user", "cat", operation.target))
            active_state = values.get("ActiveState", "")
            main_pid = values.get("MainPID", "")
            enabled_state = enabled.stdout.strip() if enabled.stdout.strip() else "disabled"
            if enabled.returncode == 0:
                raise OperationFailed("codex telegram receiver is enabled before cutover")
            if active_state not in {"inactive", "failed"} or main_pid not in {"", "0"}:
                raise OperationFailed("codex telegram receiver is active before cutover")
            if unit.returncode == 0 and "EnvironmentFile" in unit.stdout:
                raise OperationFailed("codex telegram unit must not embed an EnvironmentFile")
            observed = {
                "active": active_state,
                "enabled": enabled_state,
                "main_pid": main_pid or "0",
                "environment_file": "absent",
            }
        elif operation.kind == "codex-standing-instructions-source-check":
            path = Path(operation.target)
            actual = _sha256_bytes(path.read_bytes())
            if actual != operation.desired["source_sha256"]:
                raise OperationFailed("Codex standing instructions source hash mismatch")
            observed = {
                "source": operation.desired["source"],
                "source_commit": operation.desired["source_commit"],
                "source_sha256": actual,
                "pre_mutation_only": "true",
            }
        elif operation.kind == "codex-standing-instructions-target-check":
            path = Path(operation.target)
            actual = _sha256_bytes(path.read_bytes())
            if actual != operation.desired["sha256"]:
                raise OperationFailed("Codex standing instructions target hash mismatch")
            observed = {
                "target_sha256": actual,
                "expected_sha256": operation.desired["sha256"],
                "after_install": str("codex-standing-instructions.install" in self.events).lower(),
            }
        elif operation.kind == "codex-standing-instructions-override-check":
            path = Path(operation.target)
            if path.exists() and path.read_text(encoding="utf-8").strip():
                raise OperationFailed("Codex override file is non-empty")
            observed = {
                "override_state": "absent" if not path.exists() else "empty",
                "after_install": str("codex-standing-instructions.install" in self.events).lower(),
            }
        elif operation.kind == "mailbox-writable-root-check":
            before = {"state": "not-applicable"}
            try:
                observed = dict(verify_maildir_writable_root(Path(operation.target)))
            except MailboxWritableRootError as exc:
                raise OperationFailed(str(exc)) from exc
            observed.update(
                {
                    "harness": operation.desired.get("harness", ""),
                    "ref_kind": operation.desired.get("ref_kind", ""),
                    "ref_persona": operation.desired.get("ref_persona", ""),
                }
            )
        elif operation.kind == "daemon-reload":
            result = self._run(("systemctl", "--user", "daemon-reload"))
            if result.returncode != 0:
                raise OperationFailed((result.stderr or result.stdout).strip())
        elif operation.kind == "direct-fire":
            result = self._run(
                (
                    "python3",
                    str(Path(operation.desired["repo"]) / "tools" / "persona_bringup.py"),
                    "diagnose",
                    "--manifest",
                    operation.desired["manifest"],
                    "--persona",
                    operation.target,
                    "--harness",
                    operation.desired["harness"],
                )
            )
            if result.returncode != 0:
                raise OperationFailed(
                    (result.stderr or result.stdout).strip() or "direct boot smoke failed"
                )
        elif operation.kind == "unit-fire":
            budget = _parse_unit_fire_timeout_budget(operation)
            viability = _parse_unit_fire_initial_viability_desired(operation)
            timer = operation.desired["timer"]
            started = False
            start_count = 0
            start_time = self._unit_fire_monotonic()
            total_deadline = start_time + budget.seconds
            teardown_margin = RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS
            observation_deadline = total_deadline - teardown_margin
            if observation_deadline <= start_time:
                raise OperationFailed("unit-fire timeout budget cannot preserve teardown reserve")
            enabled = self._run(
                ("systemctl", "--user", "is-enabled", timer),
                timeout=self._remaining_timeout_seconds(observation_deadline),
            )
            if enabled.returncode == 0 and "connective.timer-observe" in self.events:
                observed = dict(self.events["connective.unit-fire"].evidence)
            elif enabled.returncode == 0:
                raise OperationFailed("timer was enabled before disabled first fire")
            else:
                last_snapshot: UnitFirePollSnapshot | None = None
                try:
                    baseline = self._prove_unit_fire_pre_enqueue_idle(
                        operation.target,
                        timeout=self._remaining_timeout_seconds(observation_deadline),
                    )
                    start = self._run(
                        ("systemctl", "--user", "start", "--no-block", operation.target),
                        timeout=self._remaining_timeout_seconds(observation_deadline),
                    )
                    start_count = 1
                    started = True
                    if start.returncode != 0:
                        raise _unit_fire_operation_failed(
                            (start.stderr or start.stdout).strip() or "disabled first fire enqueue failed",
                            phase="enqueue",
                            classification="start-failed",
                            snapshot=baseline,
                            start_count=start_count,
                            cleanup_state="pending",
                        )
                    invocation_id = ""
                    capture_attempts = 0
                    while self._unit_fire_monotonic() < observation_deadline:
                        try:
                            last_snapshot = self._poll_unit_fire_snapshot(
                                operation.target,
                                observation_deadline=observation_deadline,
                                phase="capture",
                                classification="state-unavailable",
                                start_count=start_count,
                                cleanup_state="pending",
                            )
                        except OperationFailed as exc:
                            raise _unit_fire_operation_failed(
                                str(exc),
                                phase="capture",
                                classification="state-unavailable",
                                snapshot=None,
                                start_count=start_count,
                                cleanup_state="pending",
                            ) from exc
                        capture_attempts += 1
                        if _unit_fire_snapshot_has_failed(last_snapshot):
                            raise _unit_fire_operation_failed(
                                "disabled first fire entered failed state before InvocationID capture",
                                phase="capture",
                                classification="unit-failed",
                                snapshot=last_snapshot,
                                start_count=start_count,
                                cleanup_state="pending",
                            )
                        if last_snapshot.invocation_id:
                            if not last_snapshot.invocation_id_valid:
                                raise _unit_fire_operation_failed(
                                    "disabled first fire returned malformed InvocationID",
                                    phase="capture",
                                    classification="malformed-invocation-id",
                                    snapshot=last_snapshot,
                                    start_count=start_count,
                                    cleanup_state="pending",
                                )
                            if last_snapshot.active_state not in {"activating", "active"}:
                                raise _unit_fire_operation_failed(
                                    "disabled first fire InvocationID was not captured while active",
                                    phase="capture",
                                    classification="missing-active-invocation-id",
                                    snapshot=last_snapshot,
                                    start_count=start_count,
                                    cleanup_state="pending",
                                )
                            invocation_id = last_snapshot.invocation_id
                            break
                        if last_snapshot.active_state == "inactive":
                            raise _unit_fire_operation_failed(
                                "disabled first fire completed before InvocationID capture",
                                phase="capture",
                                classification="missing-invocation-id",
                                snapshot=last_snapshot,
                                start_count=start_count,
                                cleanup_state="pending",
                            )
                        self._sleep_until_next_unit_fire_poll(observation_deadline)
                    if not invocation_id:
                        raise _unit_fire_operation_failed(
                            "disabled first fire timed out before InvocationID capture",
                            phase="capture",
                            classification="capture-timeout",
                            snapshot=last_snapshot,
                            start_count=start_count,
                            cleanup_state="pending",
                        )
                    completion: UnitFirePollSnapshot | None = None
                    while self._unit_fire_monotonic() < observation_deadline:
                        try:
                            completion = self._poll_unit_fire_snapshot(
                                operation.target,
                                observation_deadline=observation_deadline,
                                phase="completion",
                                classification="state-unavailable",
                                start_count=start_count,
                                cleanup_state="pending",
                            )
                        except OperationFailed as exc:
                            raise _unit_fire_operation_failed(
                                str(exc),
                                phase="completion",
                                classification="state-unavailable",
                                snapshot=None,
                                start_count=start_count,
                                cleanup_state="pending",
                            ) from exc
                        if _unit_fire_snapshot_has_failed(completion):
                            raise _unit_fire_operation_failed(
                                "disabled first fire completed unsuccessfully",
                                phase="completion",
                                classification="completion-failed",
                                snapshot=completion,
                                start_count=start_count,
                                cleanup_state="pending",
                            )
                        if completion.invocation_id:
                            if not completion.invocation_id_valid:
                                raise _unit_fire_operation_failed(
                                    "disabled first fire returned malformed post-exit InvocationID",
                                    phase="completion",
                                    classification="malformed-post-exit-invocation-id",
                                    snapshot=completion,
                                    start_count=start_count,
                                    cleanup_state="pending",
                                )
                            if completion.invocation_id != invocation_id:
                                raise _unit_fire_operation_failed(
                                    "disabled first fire InvocationID changed before completion",
                                    phase="completion",
                                    classification="changed-invocation-id",
                                    snapshot=completion,
                                    start_count=start_count,
                                    cleanup_state="pending",
                                )
                        if completion.active_state == "inactive":
                            break
                        self._sleep_until_next_unit_fire_poll(observation_deadline)
                    if completion is None or completion.active_state != "inactive":
                        raise _unit_fire_operation_failed(
                            "disabled first fire timed out before completion",
                            phase="completion",
                            classification="completion-timeout",
                            snapshot=completion,
                            start_count=start_count,
                            cleanup_state="pending",
                        )
                    if completion.result != "success" or not _exec_status_within_success_policy(
                        completion.exec_main_status, completion.success_exit_status
                    ):
                        raise _unit_fire_operation_failed(
                            "disabled first fire completed unsuccessfully",
                            phase="completion",
                            classification="completion-failed",
                            snapshot=completion,
                            start_count=start_count,
                            cleanup_state="pending",
                        )
                    if viability.require_invocation_receipt and not _valid_systemd_invocation_id(invocation_id):
                        raise OperationFailed("disabled first fire returned malformed InvocationID")
                    from tools import runtime_controller

                    receipt = runtime_controller.validate_initial_viability_invocation_receipt(
                        operation.provenance["persona"],
                        invocation_id=invocation_id,
                        state_root=viability.persona_state_root,
                        allowed_runtime_refs=viability.allowed_runtime_refs,
                        require_receiver_receipt=viability.receiver_required,
                        receiver_operations_by_runtime=viability.receiver_operations_by_runtime,
                    )
                    observed = {
                        "invocation_id": invocation_id,
                        "active_state": completion.active_state,
                        "result": completion.result,
                        "exec_main_status": completion.exec_main_status,
                        "success_exit_status": completion.success_exit_status,
                        "post_exit_invocation_id_state": "same" if completion.invocation_id else "empty",
                        "pre_enqueue_active_state": baseline.active_state,
                        "pre_enqueue_job": baseline.job,
                        "capture_active_state": last_snapshot.active_state if last_snapshot else "",
                        "capture_attempts": str(capture_attempts),
                        "timer_enabled": "false",
                        "unit_fire_timeout_seconds": str(budget.seconds),
                        "unit_fire_timeout_candidate_count": str(budget.candidate_count),
                        "unit_fire_timeout_post_start_count": str(budget.post_start_count),
                        "selected_runtime": str(receipt["selected_runtime"]),
                        "selected_harness": str(receipt["selected_harness"]),
                        "selected_profile": str(receipt["selected_profile"]),
                        "provider_completion_status": str(receipt["provider_completion_status"]),
                        "initial_viability_receipt_sha256": str(receipt["invocation_receipt_sha256"]),
                    }
                except OperationFailed as exc:
                    if started:
                        try:
                            cleanup = self._stop_and_prove_unit_fire_inactive(
                                operation.target,
                                total_deadline=total_deadline,
                            )
                            cleanup_state = cleanup.active_state
                        except OperationFailed as cleanup_exc:
                            if cleanup_exc.evidence:
                                raise cleanup_exc from exc
                            raise _unit_fire_operation_failed(
                                str(cleanup_exc),
                                phase="cleanup",
                                classification="unsafe-cleanup",
                                snapshot=last_snapshot,
                                start_count=start_count,
                                cleanup_state="unsafe-active",
                            ) from cleanup_exc
                        if exc.evidence:
                            evidence = dict(exc.evidence)
                            evidence["cleanup_state"] = cleanup_state
                            raise OperationFailed(str(exc), evidence=evidence) from exc
                    raise
        elif operation.kind == "mailbox-probe":
            observed = dict(self._mailbox_probe(operation))
        elif operation.kind == "timer-enablement-absence-check":
            link, canonical = self._timer_enablement_paths_from_desired(operation)
            proof = _timer_enablement_link_proof(link, canonical)
            if proof.snapshot_state != "absent":
                evidence = self._legacy_convergence_evidence(operation)
                if evidence is None:
                    raise OperationFailed(f"{operation.operation_id}: consume timer enablement link already exists")
                self._seed_legacy_convergence_events(*evidence)
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                False,
                {"state": "not-applicable"},
                "verified",
            )
            observed = {
                **_timer_enablement_link_evidence("pre", proof),
                "historical_boundary_absent": "true",
            }
        elif operation.kind == "timer-enable":
            link, canonical = self._timer_enablement_paths_from_desired(operation)
            timer_state = self._timer_unit_state(operation.target)
            if timer_state.classification == "not-found":
                raise OperationFailed("timer unit is not loaded")
            before_enabled = timer_state.classification == "enabled"
            before: dict[str, str] = {"state": "enabled" if before_enabled else "disabled"}
            if (
                context.workflow == "staged-activate"
                and before_enabled
                and "connective.timer-observe" not in self.events
            ):
                raise OperationFailed("timer already enabled at staged activation boundary")
            if not before_enabled:
                self._require_timer_enablement_link_absent(
                    operation.owner,
                    operation.target,
                    link_path=link,
                    canonical_template_path=canonical,
                    context=operation.operation_id,
                )
                result = self._run(
                    ("systemctl", "--user", "enable", "--now", operation.target)
                )
                issued_evidence = {
                    "enable_command_issued": "true",
                    "enable_command_unit": operation.target,
                    **_completed_process_bounded_evidence("enable", result),
                }
                if result.returncode != 0:
                    raise OperationFailed(
                        (result.stderr or result.stdout).strip() or "timer enable command failed",
                        evidence=issued_evidence,
                    )
                if operation.provenance.get("install_root") != "/" and not link.is_symlink():
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(str(canonical))
                proof = _timer_enablement_link_proof(link, canonical)
                if not _timer_enablement_link_matches_canonical(proof):
                    raise OperationFailed(
                        "timer enablement link did not resolve to the canonical template after enable",
                        evidence=issued_evidence,
                    )
                before.update(
                    {
                        "activation_intent": "false",
                        "owned": "true",
                        "enablement_link_state": "absent",
                        "enablement_link_path": str(link),
                        "canonical_template_path": str(canonical),
                        "canonical_template_path_sha256": proof.canonical_template_path_sha256,
                        "enablement_link_actual_readlink": proof.actual_readlink,
                        "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
                        "enablement_link_resolved_target": str(proof.resolved_target or ""),
                        "enablement_link_resolved_target_sha256": proof.resolved_target_sha256,
                    }
                )
                observed = {
                    "timer_enabled": "true",
                    **_timer_enablement_link_evidence("post", proof),
                }
            else:
                proof = _timer_enablement_link_proof(link, canonical)
                if proof.snapshot_state not in {"absent", "symlink"}:
                    raise OperationFailed("enabled timer link is not canonical")
                if proof.snapshot_state == "symlink" and not _timer_enablement_link_matches_canonical(proof):
                    raise OperationFailed("enabled timer link is not canonical")
                observed = {
                    "timer_enabled": "true",
                    **_timer_enablement_link_evidence("post", proof),
                }
            mutation = MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                not before_enabled,
                before,
                "timer-enabled",
            )
        elif operation.kind == "timer-observe":
            if "connective.timer-observe" in self.events:
                observed = dict(self.events["connective.timer-observe"].evidence)
                evidence = OperationEvidence(
                    operation.operation_id,
                    operation.layer,
                    operation.kind,
                    True,
                    "existing live convergence evidence retained",
                    observed,
                )
                self.events[operation.operation_id] = evidence
                return mutation, evidence
            service = operation.desired["service"]
            unit_fire = self.events.get("connective.unit-fire")
            if unit_fire is None:
                raise OperationFailed("timer observation lacks disabled first-fire evidence")
            first_invocation = unit_fire.evidence.get("invocation_id", "")
            receipt_sha = unit_fire.evidence.get("initial_viability_receipt_sha256", "")
            if not _valid_systemd_invocation_id(first_invocation) or not receipt_sha:
                raise OperationFailed("timer observation lacks immutable first-fire receipt evidence")
            service_values = self._systemd_show(
                service,
                ("InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus"),
            )
            current = service_values.get("InvocationID", "")
            if current and current != first_invocation:
                raise OperationFailed("consume service InvocationID changed after timer arm")
            # T-3107: hybrid exit-status invariant (Result==success AND exit in
            # {0} u SuccessExitStatus) via the shared helper, matching the
            # legacy-convergence probe -- a consume service whose success policy
            # maps a benign non-zero exit is accepted on the active first-fire
            # path too.
            if (
                service_values.get("ActiveState") != "inactive"
                or service_values.get("Result") != "success"
                or not _exec_status_within_success_policy(
                    service_values.get("ExecMainStatus", ""),
                    service_values.get("SuccessExitStatus", ""),
                )
            ):
                raise OperationFailed("consume service is not inactive successful after timer arm")
            timer = self._systemd_show(
                operation.target,
                ("ActiveState", "SubState", "NextElapseUSecRealtime", "NextElapseUSecMonotonic"),
            )
            if timer.get("ActiveState") != "active" or timer.get("SubState") != "waiting":
                raise OperationFailed("timer is not active and waiting after arm")
            next_elapse_basis = _finite_next_elapse_basis(timer)
            if next_elapse_basis is None:
                raise OperationFailed("next timer elapse is not finite after inactivity")
            mailbox_probe = self.events.get("connective.mailbox-probe")
            probe_sha = (
                mailbox_probe.evidence.get("sha256", "") if mailbox_probe else ""
            )
            if not probe_sha:
                raise OperationFailed(
                    "timer arm lacks correlated durable mailbox-probe evidence"
                )
            observed = {
                "next_elapse_basis": next_elapse_basis,
                "next_elapse_finite": "true",
                "first_fire_invocation_observed": "true",
                "second_consume_absent": "true",
                "mailbox_effect_correlated": "true",
                "mailbox_probe_sha256": probe_sha,
                "unit_fire_invocation_id": first_invocation,
                "post_exit_invocation_id_state": "same" if current else "empty",
                "initial_viability_receipt_sha256": receipt_sha,
            }
        detail = "typed local operation passed"
        evidence = OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            True,
            detail,
            observed,
        )
        self.events[operation.operation_id] = evidence
        return mutation, evidence

    def verify_operation(
        self,
        operation: InstallOperation,
        *,
        staged_evidence: OperationEvidence | None = None,
    ) -> OperationEvidence:
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install", "codex-standing-instructions-install"}:
            result = self.preflight(operation)
            ok = result.ok and result.classification in {"directory", "matching"}
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                result.detail,
            )
        if operation.kind == "claude-mcp-server-check":
            verdict = _claude_mcp_verdict(
                _read_claude_user_mcp_servers(operation.target), operation
            )
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                True,
                f"Claude MCP audit verdict for {operation.desired['name']}: {verdict}",
                {
                    "name": operation.desired["name"],
                    "scope": operation.desired["scope"],
                    "verdict": verdict,
                },
            )
        if operation.kind == "claude-mcp-server-registration":
            verdict = _claude_mcp_verdict(
                _read_claude_user_mcp_servers(operation.target), operation
            )
            ok = verdict == "matching"
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                "Claude MCP server registered and matches manifest"
                if ok
                else "Claude MCP server absent or drifted after registration",
                {"name": operation.desired["name"], "verdict": verdict},
            )
        if operation.kind == "codex-standing-instructions-target-check":
            path = Path(operation.target)
            try:
                ok = (
                    path.is_file()
                    and _sha256_bytes(path.read_bytes()) == operation.desired["sha256"]
                )
            except OSError:
                ok = False
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                "Codex standing instructions target hash matches"
                if ok
                else "Codex standing instructions target hash mismatch",
            )
        if operation.kind == "codex-telegram-source-check":
            result = self.preflight(operation)
            current = _symlink_snapshot(Path(operation.target))
            expected_target = str(Path(operation.desired["release_root"]) / "releases" / operation.desired["revision"])
            ok = result.ok and current.get("state") == "symlink" and current.get("target") == expected_target
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                "codex telegram source archive and release pointer verified"
                if ok
                else "codex telegram source archive or release pointer drifted",
            )
        if operation.kind == "mailbox-writable-root-check":
            result = self.preflight(operation)
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                result.ok,
                result.detail,
                {
                    "root": operation.target,
                    "harness": operation.desired.get("harness", ""),
                    "ref_kind": operation.desired.get("ref_kind", ""),
                    "ref_persona": operation.desired.get("ref_persona", ""),
                    "maildir_subdirs": operation.desired.get("maildir_subdirs", ""),
                    "transient_probe": operation.desired.get("transient_probe", ""),
                },
            )
        if operation.kind == "timer-enablement-absence-check":
            if (
                operation.operation_id in self.events
                and "connective.timer-enable" in self.events
                and self.events[operation.operation_id].evidence.get("historical_boundary_absent") == "true"
            ):
                prior = self.events[operation.operation_id]
                return OperationEvidence(
                    operation.operation_id,
                    operation.layer,
                    operation.kind,
                    True,
                    "historical consume timer enablement absence boundary retained",
                    dict(prior.evidence),
                )
            result = self.preflight(operation)
            try:
                link, canonical = self._timer_enablement_paths_from_desired(operation)
                proof = _timer_enablement_link_proof(link, canonical)
                evidence = _timer_enablement_link_evidence("verify", proof)
            except OperationFailed:
                evidence = {}
            admissible_classifications = _timer_enablement_absence_admissible_classifications(
                staged_evidence
            )
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                result.ok and result.classification in admissible_classifications,
                result.detail,
                evidence,
            )
        if operation.operation_id in self.events:
            return self.events[operation.operation_id]
        if operation.kind in {
            "stale-unit-failure-reset",
            "daemon-reload",
            "direct-fire",
            "unit-fire",
            "mailbox-probe",
            "timer-enable",
            "timer-observe",
        }:
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                False,
                "live execution evidence absent",
            )
        result = self.preflight(operation)
        return OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            result.ok,
            result.detail,
        )

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.kind == "timer-enable":
            before_state = mutation.before.get("state")
            if before_state not in {"enabled", "disabled"}:
                return PreflightResult(False, "malformed", "timer receipt before-state is unsupported")
            link_proof: TimerEnablementLinkProof | None = None
            has_link_receipt = bool(mutation.before.get("enablement_link_path") or mutation.before.get("canonical_template_path"))
            if before_state == "disabled" and has_link_receipt:
                try:
                    link_proof = self._require_timer_enablement_link_matches_receipt(mutation)
                except OperationFailed as exc:
                    return PreflightResult(False, "drifted", str(exc))
            try:
                timer_state = self._timer_unit_state(mutation.target)
            except OperationFailed as exc:
                return PreflightResult(False, "unavailable", str(exc))
            current = timer_state.classification
            timer_inactive = timer_state.active_state == "inactive" and timer_state.sub_state == "dead"
            receipt_owned_link_present = link_proof is not None and link_proof.snapshot_state == "symlink"
            removed_but_active = (
                before_state == "disabled"
                and current == "not-found"
                and not timer_inactive
            )
            if before_state == "disabled" and receipt_owned_link_present:
                return PreflightResult(True, "owned", "timer enablement link matches receipt")
            if current == before_state and timer_inactive:
                return PreflightResult(
                    True,
                    "not-applied",
                    "timer still matches the write-ahead state",
                )
            if before_state == "disabled" and current == "not-found" and timer_inactive:
                return PreflightResult(
                    True,
                    "not-applied",
                    "timer still matches the write-ahead state",
                )
            if removed_but_active:
                return PreflightResult(True, "owned", "timer unit file is removed but receipt-owned timer is active")
            if before_state == "enabled" and current in {"disabled", "not-found"}:
                return PreflightResult(False, "drifted", "timer no longer matches enabled write-ahead state")
            if before_state == "disabled" and current != "enabled":
                return PreflightResult(False, "drifted", "timer is not in the receipt-owned enabled state")
            return PreflightResult(True, "owned", "timer enable state matches receipt")
        if mutation.kind == "codex-telegram-source-check":
            current = _snapshot_fingerprint(_symlink_snapshot(Path(mutation.target)))
            before = _snapshot_fingerprint(mutation.before)
            if current == before:
                return PreflightResult(
                    True,
                    "not-applied",
                    "release pointer already matches the pre-apply receipt state",
                )
            if current != mutation.after_fingerprint:
                return PreflightResult(False, "drifted", "release pointer changed after apply")
            return PreflightResult(True, "owned", "release pointer matches receipt")
        if mutation.kind == "codex-telegram-mcp-check":
            current = _snapshot_fingerprint(_snapshot(Path(mutation.target)))
            if current == mutation.before.get("prior_fingerprint"):
                return PreflightResult(True, "not-applied", "MCP config already matches pre-apply state")
            if current != mutation.after_fingerprint:
                return PreflightResult(False, "drifted", "MCP config changed after apply")
            return PreflightResult(True, "owned", "MCP config matches receipt")
        if mutation.kind == "claude-mcp-server-check":
            return PreflightResult(True, "not-applied", "read-only audit op made no change")
        if mutation.kind == "claude-mcp-server-registration":
            try:
                servers = _read_claude_user_mcp_servers(mutation.target)
            except OperationFailed as exc:
                return PreflightResult(False, "unreadable", str(exc))
            return _claude_mcp_registration_can_rollback(mutation, servers)
        current = _snapshot_fingerprint(_snapshot(Path(mutation.target)))
        before = _snapshot_fingerprint(mutation.before)
        if current == before:
            return PreflightResult(
                True,
                "not-applied",
                "target already matches the pre-apply receipt state",
            )
        if current != mutation.after_fingerprint:
            return PreflightResult(False, "drifted", "post-apply fingerprint changed")
        return PreflightResult(True, "owned", "post-apply fingerprint matches receipt")

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if mutation.kind == "claude-mcp-server-check":
            return
        if mutation.kind == "claude-mcp-server-registration":
            if mutation.before.get("added") != "true":
                return
            name = mutation.before.get("name")
            home = mutation.before.get("home", "")
            scope = mutation.before.get("scope", "user")
            result = self._run_claude_mcp_remove(name, home=home, scope=scope)
            if result is not None and result.returncode != 0:
                text = (result.stderr or result.stdout or "")
                # Tolerate an already-absent server (idempotent rollback).
                if "not found" not in text.lower() and "no mcp server" not in text.lower():
                    raise OperationFailed(
                        text.strip() or f"claude mcp remove {name!r} failed",
                        evidence=_completed_process_bounded_evidence("claude_mcp_remove", result),
                    )
            return
        if mutation.kind == "timer-enable":
            desired_enabled = mutation.before.get("state") == "enabled"
            if not desired_enabled:
                if mutation.before.get("activation_intent") == "true" or mutation.before.get("owned") == "false":
                    raise OperationFailed("staged timer enablement intent is not cleanup authority")
                service = self._consume_service_for_timer(mutation.target)
                self._stop_unit_or_converged_not_loaded(mutation.target)
                self._stop_unit_or_converged_not_loaded(service)
                self._prove_unit_inactive_dead(service)
                self._prove_unit_inactive_dead(mutation.target)
                if mutation.before.get("enablement_link_path") or mutation.before.get("canonical_template_path"):
                    self._remove_receipt_owned_timer_enablement_link(mutation)
                result = self._run(("systemctl", "--user", "disable", "--now", mutation.target))
                if result.returncode != 0:
                    self._prove_unit_inactive_dead(mutation.target)
                    self._prove_unit_inactive_dead(service)
                    missing_unit = (result.stderr or result.stdout)
                    if "not exist" not in missing_unit and "not loaded" not in missing_unit and "No such file" not in missing_unit:
                        raise OperationFailed(missing_unit.strip() or "timer disable failed")
                reload_result = self._run(("systemctl", "--user", "daemon-reload"))
                if reload_result.returncode != 0:
                    raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
                reset_result = self._run(("systemctl", "--user", "reset-failed", mutation.target))
                if reset_result.returncode != 0:
                    raise OperationFailed((reset_result.stderr or reset_result.stdout).strip() or "timer reset-failed failed")
                service_reset = self._run(("systemctl", "--user", "reset-failed", service))
                if service_reset.returncode != 0:
                    raise OperationFailed((service_reset.stderr or service_reset.stdout).strip() or "consume service reset-failed failed")
                if mutation.before.get("enablement_link_path") or mutation.before.get("canonical_template_path"):
                    link, canonical = self._timer_enablement_paths_from_mutation(mutation)
                    absent = _timer_enablement_link_proof(link, canonical)
                    if absent.snapshot_state != "absent":
                        raise OperationFailed("timer enablement link still exists after rollback")
                self._prove_unit_inactive_dead(mutation.target)
                self._prove_unit_inactive_dead(service)
                return
            argv = (
                ("systemctl", "--user", "enable", "--now", mutation.target)
            )
            result = self._run(argv)
            if result.returncode != 0:
                raise OperationFailed((result.stderr or result.stdout).strip())
            return
        if mutation.kind == "codex-telegram-source-check":
            path = Path(mutation.target)
            before = mutation.before
            if before.get("state") == "absent":
                if path.is_symlink():
                    path.unlink()
                return
            if before.get("state") == "symlink":
                link_tmp = path.parent / f".{path.name}.rollback-{os.getpid()}"
                if link_tmp.exists() or link_tmp.is_symlink():
                    link_tmp.unlink()
                link_tmp.symlink_to(before["target"])
                os.replace(link_tmp, path)
                return
        if mutation.kind == "codex-telegram-mcp-check":
            path = Path(mutation.target)
            before = mutation.before
            if before.get("created_file") == "true":
                path.unlink(missing_ok=True)
                return
            current = path.read_text(encoding="utf-8")
            suffix = before.get("appended_suffix", "")
            if (
                suffix
                and before.get("appended_suffix_sha256") == _sha256_bytes(suffix.encode("utf-8"))
                and current.endswith(suffix)
            ):
                path.write_text(current[: -len(suffix)], encoding="utf-8")
                return
        path = Path(mutation.target)
        before = mutation.before
        if before.get("state") == "absent":
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    # Durable receipts or user-created state are preserved. A
                    # rollback never recursively deletes a non-empty directory.
                    return
            else:
                path.unlink(missing_ok=True)
            return
        if before.get("state") == "directory":
            os.chmod(path, int(before.get("mode", "0755"), 8))
            return
        if before.get("state") == "file":
            raw = base64.b64decode(before["content_b64"])
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, int(before.get("mode", "0644"), 8))
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable":
            return None
        try:
            timer_state = self._timer_unit_state(operation.target)
            link, canonical = self._timer_enablement_paths_from_desired(operation)
        except OperationFailed as exc:
            raise OperationFailed(f"{operation.operation_id}: rollback intent timer state unavailable: {exc}") from exc
        enabled = timer_state.classification == "enabled"
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=False,
            before={
                "state": "enabled" if enabled else "disabled",
                "activation_intent": "true",
                "owned": "false",
                "enablement_link_state": "absent",
                "enablement_link_path": str(link),
                "canonical_template_path": str(canonical),
            },
            after_fingerprint="timer-enabled",
        )

    def issued_timer_enable_recovery_mutation(
        self,
        operation: InstallOperation,
        evidence: Mapping[str, str],
    ) -> MutationRecord | None:
        if operation.kind != "timer-enable" or evidence.get("enable_command_issued") != "true":
            return None
        try:
            timer_state = self._timer_unit_state(operation.target)
            link, canonical = self._timer_enablement_paths_from_desired(operation)
            proof = _timer_enablement_link_proof(link, canonical)
        except OperationFailed:
            return None
        if timer_state.classification != "enabled" or not _timer_enablement_link_matches_canonical(proof):
            return None
        before = {
            "state": "disabled",
            "activation_intent": "false",
            "owned": "true",
            "enablement_link_state": "absent",
            "enablement_link_path": str(link),
            "canonical_template_path": str(canonical),
            "canonical_template_path_sha256": proof.canonical_template_path_sha256,
            "enablement_link_actual_readlink": proof.actual_readlink,
            "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
            "enablement_link_resolved_target": str(proof.resolved_target or ""),
            "enablement_link_resolved_target_sha256": proof.resolved_target_sha256,
        }
        return MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=True,
            before=before,
            after_fingerprint="timer-enabled",
        )

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        directory = Path(plan.provenance["receipt_dir"])
        if not directory.is_dir():
            raise OperationFailed("planned receipt directory is missing")
        path = directory / f"{receipt.receipt_id}.json"
        content = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".receipt.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _all_plan_capabilities(plan: InstallPlan) -> frozenset[str]:
    operation_capabilities = {
        operation.provenance["required_capability"]
        for operation in plan.operations
        if operation.provenance.get("required_capability")
    }
    action_capabilities = {
        action.required_capability for action in plan.operator_actions
    }
    return frozenset(operation_capabilities | action_capabilities)


def _print_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "simulate"):
        child = subparsers.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--persona", required=True)
        child.add_argument("--harness")
        child.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args(argv)
    try:
        manifest = Manifest.load(args.manifest)
        installer = PersonaInstaller()
        plan = installer.plan(
            manifest,
            args.persona,
            harness_override=args.harness,
            root=args.root,
        )
        if args.command == "plan":
            _print_json(plan)
            return 0
        backend = SimulatedHostBackend()
        actor = ActorContext(
            uid=os.geteuid(),
            persona=args.persona,
            role=plan.role,
            instance=plan.instance,
            capabilities=_all_plan_capabilities(plan),
        )
        applied = installer.apply(plan, plan.digest, backend, actor)
        verified = installer.verify(plan, backend)
        _print_json({"plan": plan, "apply": applied, "verify": verified})
        return 0 if applied.status in {"applied", "noop"} and verified.status == "verified" else 1
    except InstallerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
