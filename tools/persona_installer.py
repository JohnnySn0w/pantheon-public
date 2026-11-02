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
import difflib
import grp
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Protocol, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import codex_telegram_bridge  # noqa: E402  (needs sys.path bootstrap)
from tools.mcp_allowlist_registry import RegistryError, load_registry  # noqa: E402
from tools.harness_adapter.diagnostics import flip_key_violations  # noqa: E402
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
from tools import user_systemd as _us  # noqa: E402  (needs sys.path bootstrap)
from tools.user_systemd import ReadPurpose, UnitSnapshot  # noqa: E402
# T-5516: pure contract types and the executor-shared activation helpers live in
# dependency-closed modules (ORG-052 §1/§3); re-exported so every existing
# ``tools.persona_installer.X`` import keeps working with the same objects.
from tools.persona_installer_contracts import (  # noqa: E402,F401
    PrivilegeRequirement,
    ExecutionScope,
    Workflow,
    OperationKind,
    InstallerError,
    ManifestError,
    OperationFailed,
    ALLOWED_TARGET_ROOTS_KEY,
    _canonical_json,
    _sha256_bytes,
    _jsonable,
    InstallOperation,
    ExecutionContext,
    EXECUTION_CONTEXT_ALL_IN_ONE,
    OperatorAction,
    InstallPlan,
    PreflightResult,
    MutationRecord,
    OperationEvidence,
    FRAMEWORK_CODE_ARTIFACTS,
)
from tools.persona_installer_activation import (  # noqa: E402,F401
    MANAGED_MARKER,
    RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS,
    MAX_UNIT_FIRE_TIMEOUT_SECONDS,
    UNIT_FIRE_TIMEOUT_FORMULA,
    _claude_mcp_projection,
    _claude_mcp_expected,
    _claude_mcp_verdict,
    _runtime_budget_constants,
    _unit_fire_timeout_seconds,
    _canonical_decimal_int,
    UnitFireTimeoutBudget,
    UnitFireInitialViabilityDesired,
    _parse_unit_fire_timeout_budget,
    _canonical_json_string,
    _parse_canonical_bool,
    _parse_unit_fire_initial_viability_desired,
    TimerUnitState,
    TimerEnablementLinkProof,
    _consume_timer_template_unit,
    _consume_timer_template_path,
    _consume_timer_enablement_link_path,
    _lexically_normalize_absolute,
    _resolve_timer_enablement_readlink,
    _timer_enablement_link_evidence,
    _timer_enablement_link_matches_canonical,
    SYSTEMCTL_TRANSPORT,
    SYSTEMCTL_SHOW_STDOUT,
    StaleUnitFailureResetSnapshot,
    _stale_unit_failure_reset_typed,
    _classify_stale_unit_failure_reset_snapshot,
    _stale_unit_failure_reset_snapshot_evidence,
    _stale_unit_failure_reset_failure,
    _timer_unit_state_from_snapshot,
    _snapshot_fingerprint,
    LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS,
    LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS,
    UNIT_FIRE_POLL_INTERVAL_SECONDS,
    _unit_fire_invocation_digest,
    _is_installer_managed,
    # T-5608: the T-5473 residue-quarantine predicate, shared with the per-seat executor
    QUARANTINE_DIR_PREFIX,
    RESIDUE_RUNTIME_CONTROLLER,
    RESIDUE_PERSONA_ROOT,
    RESIDUE_STATE_ROOT_SUFFIXES,
    _QUARANTINE_CLASSES,
    _PERSONA_NAME,
    _quarantine_desired,
    _quarantine_class_base,
    _bootstrap_residue_entries,
    _RESIDUE_SAFE_ACTIVE_STATES,
    _runtime_liveness_refusal,
    _quarantine_stamp,
    _quarantine_dir_for,
    _quarantine_pairs,
    _quarantine_can_rollback,
    _residue_quarantine_check,
)

PLAN_SCHEMA = "pantheon-persona-install-plan/v1"
RECEIPT_SCHEMA = "pantheon-persona-install-receipt/v1"
ACTIVATION_OPERATION_KINDS = frozenset(
    {
        "daemon-reload",
        "stale-unit-failure-reset",
        "direct-fire",
        "unit-fire",
        "mailbox-probe",
        "timer-enable",
        "timer-observe",
        "seat-periodic-daemon-reload",
        "seat-periodic-timer-enable",
        "seat-periodic-timer-observe",
        "seat-service-enable-observe",
        "seat-service-decommission",
        # T-5473 / T-5474 (reviewer B3): phase Q runs AFTER the operator's teardown, so these are
        # activation ops: stage_disabled does not execute them, activate/all-in-one does, still
        # before the bootstrap fire.
        "seat-runtime-residue-quarantine",
        "seat-claude-workspace-trust",
    }
)
# T-5449: the seat-local residue kinds -- executed by the installer itself as the
# target account, never by the host_control executor. One list; the envelope tests
# import it rather than matching a kind-name prefix.
SEAT_LOCAL_RESIDUE_KINDS = frozenset(
    {
        "seat-periodic-unit-install",
        "seat-periodic-daemon-reload",
        "seat-periodic-timer-enable",
        "seat-periodic-timer-observe",
        "seat-service-enable-observe",
        "seat-service-decommission",
        # T-5454: shell-box containment wiring in the seat's own settings.json.
        "seat-claude-settings-env",
        # T-5474: phase-Q first-launch workspace trust, before the consume bootstrap fire.
        # (T-5473's residue quarantine is executor-admitted as files.seat_root since T-5608;
        # the installer's own backends still execute it in an installer flow.)
        "seat-claude-workspace-trust",
    }
)
# T-5454: Claude-seat memory containment (T-5222). Recovery's verified wiring: the seat's
# own ~/.claude/settings.json, top-level env.CLAUDE_CODE_SHELL_PREFIX = the absolute path
# of the seat clone's pantheon-shell-box.sh. Read at Claude process start.
SETTINGS_ENV_KIND = "seat-claude-settings-env"
SHELL_BOX_ENV_KEY = "CLAUDE_CODE_SHELL_PREFIX"
SHELL_BOX_RELATIVE = Path("pantheon-config") / "tools" / "oom" / "pantheon-shell-box.sh"
SHELL_BOX_PROVENANCE_KEY = "shell_box_containment"
# T-5473 / T-5474 (SOP hand-moving-an-established-seat §4 phase Q).
RESIDUE_QUARANTINE_KIND = "seat-runtime-residue-quarantine"
RESIDUE_QUARANTINE_OP_ID = "service.runtime-residue.quarantine"
WORKSPACE_TRUST_KIND = "seat-claude-workspace-trust"
WORKSPACE_TRUST_OP_ID = "harness.claude-workspace-trust"
TRUST_KEY = "hasTrustDialogAccepted"
LAYER_NAMES = {
    0: "host-substrate",
    1: "connective-substrate",
    2: "harness-runtime",
    3: "persona-overlay",
    4: "policy-governance",
}

class PlanRefused(InstallerError):
    """Apply or rollback was refused before an unsafe mutation."""


# T-4803 / ORG-050 "Required installer gates": a leakage scan over the RENDERED
# content of every operation. Secret-shaped bytes only; identity leakage is the
# mirror gate's concern (tools/mirror_leak_gate.py), so its patterns do not fit.
# Hits are reported by operation id and pattern NAME only, never the bytes.
_LEAKAGE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "telegram-bot-token",
        re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    ),
    (
        "api-key-prefix",
        re.compile(r"(?<![A-Za-z0-9])(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}"),
    ),
    (
        "pem-private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
)


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string reachable in ``value``, recursing through mappings
    (keys and values), lists, tuples, sets and frozensets."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_strings(item)


def _leakage_scan(plan: "InstallPlan") -> dict[str, tuple[str, ...]]:
    """Map operation id -> names of secret-shaped patterns in its rendered bytes.

    Recursively scans every string reachable under ``operation.desired``
    (``content``, ``expected`` and the rest, including nested dicts, lists and
    tuples and mapping keys): exactly what the backend would write or register.
    Never returns matched text.
    """
    hits: dict[str, tuple[str, ...]] = {}
    for operation in plan.operations:
        values = tuple(_iter_strings(operation.desired))
        names = tuple(
            name
            for name, pattern in _LEAKAGE_PATTERNS
            if any(pattern.search(value) for value in values)
        )
        if names:
            hits[operation.operation_id] = names
    return hits


def _leakage_refusal(hits: Mapping[str, tuple[str, ...]]) -> str:
    detail = "; ".join(f"{op_id}: {', '.join(names)}" for op_id, names in sorted(hits.items()))
    return f"leakage scan: secret-shaped content in rendered operations ({detail})"


# ORG-050 "Required installer gates": target-path check -- see ALLOWED_TARGET_ROOTS_KEY
# (tools/persona_installer_contracts.py, T-5516) for the gate's rule and roots.
# Read-only checks whose target is the installer's own framework checkout (a
# repo path, not host state), so they are exempt from the allowed-roots test.
# They are still `..`-checked.
_REPO_SOURCED_READONLY_KINDS = frozenset(
    {
        "source-check",
        "policy-check",
        "brief-check",
        "codex-standing-instructions-source-check",
    }
)


def _target_path_violations(
    operations: Sequence["InstallOperation"],
    allowed_roots: Sequence[str],
) -> list[str]:
    """Return 'op-id: reason' strings for targets failing the path gate."""
    roots = [Path(item) for item in allowed_roots if item]
    problems: list[str] = []
    for operation in operations:
        target = operation.target
        if not target.startswith("/"):
            continue  # unit names / harness names, not filesystem paths
        path = Path(target)
        if ".." in path.parts:
            problems.append(f"{operation.operation_id}: target path contains '..'")
            continue
        if operation.kind in _REPO_SOURCED_READONLY_KINDS:
            continue
        if not any(path == root or root in path.parents for root in roots):
            problems.append(
                f"{operation.operation_id}: target path outside allowed roots"
            )
    return problems


def _declared_roots_problem(plan: "InstallPlan", declared: Sequence[str]) -> str | None:
    """A plan's self-declared roots are untrusted input (a stored plan file can
    carry any set under a self-consistent digest). Bound them structurally."""
    # An empty account makes home resolve to /home (so a /home root would pass);
    # a '/' or '..' account walks the home out of /home/<account>.
    if not plan.account or "/" in plan.account or ".." in plan.account:
        return f"plan account {plan.account!r} is empty or contains '/' or '..'"
    # "//"-prefixed paths survive normpath unchanged (POSIX) and pathlib keeps
    # Path("//") != Path("/"), yet the kernel opens them as "/" (T-4963, D32)
    if str(plan.root).startswith("//"):
        return f"plan root {plan.root!r} starts with '//'"
    for item in declared:
        if item.startswith("//"):
            return f"declared root {item!r} starts with '//'"
    plan_root = Path(plan.root)
    home = plan_root / "home" / plan.account if plan.root != "/" else Path("/home") / plan.account
    for item in declared:
        path = Path(item)
        if not path.is_absolute() or ".." in path.parts:
            return f"declared root {item!r} is relative or contains '..'"
        if path == Path("/") or path == plan_root:
            return f"declared root {item!r} is the filesystem or plan root"
        if path in home.parents:
            return f"declared root {item!r} is an ancestor of the account home"
        if plan_root != Path("/") and plan_root not in path.parents:
            return f"declared root {item!r} lies outside the plan root"
    if home not in [Path(item) for item in declared]:
        return "declared roots omit the account home"
    return None


def _target_path_refusal(plan: "InstallPlan") -> str | None:
    declared = plan.provenance.get(ALLOWED_TARGET_ROOTS_KEY, "")
    if not declared:
        return "target path check: plan declares no allowed target roots"
    roots = declared.split(",")
    root_problem = _declared_roots_problem(plan, roots)
    if root_problem:
        return f"target path check: {root_problem}"
    problems = _target_path_violations(plan.operations, roots)
    if problems:
        return "target path check: " + "; ".join(problems)
    return None


def _derive_allowed_roots(manifest: "Manifest", account: str, root: Path) -> str:
    """The allowed target roots for ``account`` under ``root``, derived from the
    trusted manifest only. ``plan()`` records this value in provenance and
    ``_preflight_apply`` re-derives it, so the two cannot drift."""
    home = _rooted(root, Path("/home") / account)
    mailbox = manifest.installer.get("mailbox")
    hub = mailbox.get("hub") if isinstance(mailbox, dict) else None
    if not isinstance(hub, str) or not Path(hub).is_absolute():
        raise ManifestError("persona_installer.mailbox.hub must be absolute")
    return f"{home},{_rooted(root, Path(hub))}"


def _pinned_roots_refusal(plan: "InstallPlan", manifest: "Manifest | None") -> str | None:
    """Refuse unless the plan's declared roots equal the roots re-derived from the
    trusted manifest. Fail closed: no manifest, no apply (a skip-when-absent pin
    is a hole by construction)."""
    if manifest is None:
        return "target path check: no trusted manifest supplied; cannot pin allowed target roots"
    if plan.instance != manifest.instance_id:
        return "target path check: plan instance does not match the trusted manifest"
    try:
        binding = manifest.binding(plan.persona)
        derived = _derive_allowed_roots(manifest, binding.account, Path(plan.root))
    except ManifestError as exc:
        return f"target path check: cannot derive allowed roots from manifest: {exc}"
    if binding.account != plan.account:
        return "target path check: plan account does not match the trusted manifest"
    if plan.provenance.get(ALLOWED_TARGET_ROOTS_KEY, "") != derived:
        return "target path check: plan allowed roots differ from the manifest-derived roots"
    return None


def _realpath_containment_refusal(
    operation: "InstallOperation",
    allowed_roots: Sequence[str],
) -> str | None:
    """Execute-time complement to the lexical gate (T-4837): resolve symlinks in
    the target's PARENT directory and require the result to stay under a resolved
    allowed root. Same coverage as the lexical gate: absolute, non-repo-sourced
    targets. Fail closed when no roots were supplied.

    The final path component is deliberately NOT resolved. Writes land via
    mkstemp + os.replace and mkdir, and rollback unlinks; none of those follow a
    symlink in the last component, so a link there is replaced, not traversed.
    Resolving it would wrongly refuse legitimate replace-a-link cases."""
    target = operation.target
    if not target.startswith("/") or operation.kind in _REPO_SOURCED_READONLY_KINDS:
        return None
    roots = [os.path.realpath(item) for item in allowed_roots if item]
    if not roots:
        return f"{operation.operation_id}: no allowed roots supplied for realpath containment"
    parent = os.path.realpath(os.path.dirname(target))
    for root in roots:
        if parent == root or parent.startswith(root.rstrip("/") + "/"):
            return None
    return (
        f"{operation.operation_id}: target parent resolves outside allowed roots "
        f"({parent})"
    )


# T-4848: path-bearing fields in an operation's ``desired`` that a backend
# writes through (or that `systemctl` / `claude` write through on its behalf),
# beyond the op target. "entry": the entry itself is created/replaced/unlinked,
# so its PARENT is resolved. "dir": writes land inside it, so the whole path is
# resolved. Keyed by operation kind; every absolute-path desired value in a
# real plan must be here or in _READONLY_DESIRED_PATH_FIELDS (a test enforces
# this, so a new path field cannot silently bypass containment).
_DESIRED_PATH_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "timer-enable": (("enablement_link_path", "entry"), ("canonical_template_path", "entry")),
    "seat-periodic-timer-enable": (
        ("enablement_link_path", "entry"),
        ("canonical_template_path", "entry"),
    ),
    "seat-service-enable-observe": (
        ("enablement_link_path", "entry"),
        ("canonical_template_path", "entry"),
        ("persona_state_root", "dir"),
    ),
    "seat-service-decommission": (("unit_dir", "dir"), ("quarantine_dir", "dir")),
    "seat-runtime-residue-quarantine": (("persona_state_root", "dir"),),
    "timer-enablement-absence-check": (
        ("enablement_link_path", "entry"),
        ("canonical_template_path", "entry"),
    ),
    "claude-mcp-server-registration": (("home", "dir"), ("config_path", "entry")),
    "claude-mcp-server-check": (("home", "dir"), ("config_path", "entry")),
    "mailbox-probe": (("receive_tmp", "dir"), ("send_tmp", "dir")),
    "codex-telegram-source-check": (
        ("release_root", "dir"),
        ("state_dir", "dir"),
        ("account_home", "dir"),
    ),
    "unit-fire": (("account_home", "dir"), ("persona_state_root", "dir")),
}
# Absolute-path desired values that are only read or exec'd, never written.
_READONLY_DESIRED_PATH_FIELDS = frozenset(
    {
        "mailbox_hub",       # informational; the probe/root-check targets are checked
        "archive_path",      # tar read (the source archive)
        "source_root",       # bridge source read
        "credential_env",    # path recorded in the unit, not written by the installer
        "env_value",         # settings-env: prefix path Claude Code execs; never written by us
        "project_path",      # workspace-trust: a JSON object key in ~/.claude.json, not a written path
        "repo",              # direct-fire exec of the repo's own bringup script
        "manifest",          # direct-fire argument, read only
        "declared_path",     # source-check read
        "artifacts",         # source-check read (comma list)
        "bundle",            # policy-check read (comma list)
        "executable",        # hermes harness-check: stat/access only (T-5011 PR-B)
    }
)


def _path_containment_problem(
    label: str, raw: str, mode: str, allowed_roots: Sequence[str]
) -> str | None:
    """None when ``raw`` stays inside a resolved allowed root; else a reason.
    Empty ``raw`` means "field not set" and is skipped. Fail closed on no roots."""
    if not raw:
        return None
    if not raw.startswith("/"):
        return f"{label}: path {raw!r} is not absolute"
    roots = [os.path.realpath(item) for item in allowed_roots if item]
    if not roots:
        return f"{label}: no allowed roots supplied for realpath containment"
    resolved = os.path.realpath(raw if mode == "dir" else os.path.dirname(raw))
    for root in roots:
        if resolved == root or resolved.startswith(root.rstrip("/") + "/"):
            return None
    return f"{label}: path resolves outside allowed roots ({resolved})"


def _desired_path_refusal(
    operation: "InstallOperation", allowed_roots: Sequence[str]
) -> str | None:
    for key, mode in _DESIRED_PATH_FIELDS.get(operation.kind, ()):
        problem = _path_containment_problem(
            f"{operation.operation_id}.desired.{key}",
            str(operation.desired.get(key, "") or ""),
            mode,
            allowed_roots,
        )
        if problem:
            return problem
    return None


def _rollback_containment_refusal(
    mutation: "MutationRecord", allowed_roots: Sequence[str]
) -> str | None:
    """Rollback-time complement to execute-time containment: the mutation's
    target and the receipt-carried paths it will write or unlink must stay
    inside the allowed roots. A receipt is stored data, so its paths are as
    untrusted as a stored plan's. Fails closed without roots."""
    if not [item for item in allowed_roots if item]:
        return f"{mutation.operation_id}: no allowed roots supplied for rollback containment"
    checks: list[tuple[str, str, str]] = []
    if mutation.target.startswith("/") and mutation.kind not in _REPO_SOURCED_READONLY_KINDS:
        checks.append(("target", mutation.target, "entry"))
    for key, mode in (
        ("enablement_link_path", "entry"),
        ("canonical_template_path", "entry"),
        ("home", "dir"),
        ("persona_state_root", "dir"),  # T-5473: the quarantine receipt's rename base
    ):
        value = str(mutation.before.get(key, "") or "")
        if value:
            checks.append((f"before.{key}", value, mode))
    for key, raw, mode in checks:
        problem = _path_containment_problem(
            f"{mutation.operation_id}.{key}", raw, mode, allowed_roots
        )
        if problem:
            return problem
    return None


def _plan_allowed_roots(plan: "InstallPlan") -> tuple[str, ...]:
    declared = plan.provenance.get(ALLOWED_TARGET_ROOTS_KEY, "")
    return tuple(item for item in declared.split(",") if item)


def _clean_home_dry_run_backend() -> "SimulatedHostBackend":
    """A FRESH in-memory backend: an empty (clean) home, never live state."""
    return SimulatedHostBackend()


def _clean_home_dry_run(
    plan: "InstallPlan",
    scopes: frozenset[ExecutionScope] = frozenset({"all"}),
) -> None:
    """ORG-050 clean-home dry-run: rehearse the in-scope operations on an empty
    simulated home. Touches no real filesystem or host. Raises PlanRefused."""
    backend = _clean_home_dry_run_backend()
    try:
        for operation in plan.operations:
            if not _operation_in_scope(operation, scopes):
                continue
            result = backend.preflight(operation)
            if not result.ok:
                raise PlanRefused(
                    f"clean-home dry-run: {operation.operation_id}: "
                    f"{result.classification}: {result.detail}"
                )
            _, observed = backend.execute(operation, EXECUTION_CONTEXT_ALL_IN_ONE)
            if not observed.ok:
                raise PlanRefused(
                    f"clean-home dry-run: {operation.operation_id}: {observed.detail}"
                )
    except PlanRefused:
        raise
    except Exception as exc:
        raise PlanRefused(f"clean-home dry-run: {type(exc).__name__}: {exc}") from exc


def _receipt_audit_fields(plan: "InstallPlan") -> dict[str, str]:
    return {
        "host": socket.gethostname(),
        "harness": plan.selected_harness,
        "source": str(plan.provenance.get("source_revision", "unknown")),
    }


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


def _normalize_harness(value: str) -> str:
    aliases = {
        "claude": "claude",
        "claude-code": "claude",
        "codex": "codex",
        "hermes": "hermes",  # T-5011 PR-B
    }
    try:
        return aliases[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ManifestError(f"unsupported harness {value!r}") from exc


def _harness_check_desired(manifest: "Manifest", harness: str, home: Path) -> dict[str, str]:
    """The harness-check operation's desired state. claude/codex: unchanged
    (``{"harness": h}``; preflight looks the command up on PATH). hermes
    (T-5011 PR-B): its pinned venv binary is not on PATH, so the check names
    the default runtime profile's executable (``{account_home}`` resolved
    against ``home``); "" when the manifest declares none, which fails."""
    if harness != "hermes":
        return {"harness": harness}
    executable = ""
    harnesses = manifest.harness_runtime.get("harnesses") if isinstance(manifest.harness_runtime, Mapping) else None
    block = harnesses.get("hermes") if isinstance(harnesses, Mapping) else None
    if isinstance(block, Mapping):
        profiles = block.get("profiles")
        default = block.get("default_profile")
        profile = profiles.get(default) if isinstance(profiles, Mapping) and isinstance(default, str) else None
        template = profile.get("executable") if isinstance(profile, Mapping) else None
        if isinstance(template, str):
            executable = template.replace("{account_home}", str(home))
    return {"harness": harness, "executable": executable}


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

# Fail-closed compatibility view of the single machine authority.  The YAML is
# also consumed by the fleet audit; this name remains for existing provisioner
# and host-broker callers, but is no longer an independently edited registry.
def _load_mcp_allowlist(path: Path | None = None) -> Mapping[str, frozenset[str]]:
    path = path or REPO_ROOT / "tools" / "mcp_allowlist.yaml"
    try:
        registry = load_registry(path)
    except RegistryError as exc:
        raise ManifestError("MCP allowlist authority is malformed or unreadable") from exc
    return {name: server.seats for name, server in registry.servers.items()}


_CLAUDE_MCP_ALLOWLIST: Mapping[str, frozenset[str]] = _load_mcp_allowlist()

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


# ---- T-5454: shell-box wiring in the seat's ~/.claude/settings.json --------------------
# Other tools write this file too, so the op is merge-preserving (only
# env.CLAUDE_CODE_SHELL_PREFIX changes), atomic, and fails closed on anything it does not
# fully understand. Pure helpers here; both backends share them.

def _settings_env_parse(raw: bytes | None) -> dict[str, Any] | None:
    """The settings document, or None when the file is absent. Refuses unparseable
    JSON, duplicate keys (a round-trip would silently drop one), a non-object top
    level, and a non-object ``env``."""
    if raw is None:
        return None

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate object keys")
        return dict(pairs)

    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise OperationFailed(f"settings.json is not parseable JSON ({exc}); refusing to touch it") from exc
    if not isinstance(doc, dict):
        raise OperationFailed("settings.json top level is not an object; refusing to touch it")
    if "env" in doc and not isinstance(doc["env"], dict):
        raise OperationFailed("settings.json env is not an object; refusing to touch it")
    return doc


def _settings_env_verdict(doc: Mapping[str, Any] | None, key: str, value: str) -> tuple[str, str]:
    if doc is None:
        return "absent", "settings.json absent; will be created"
    env = doc.get("env")
    if not isinstance(env, Mapping) or key not in env:
        return "absent", f"env.{key} absent; will be added"
    if env[key] == value:
        return "matching", f"env.{key} already set to the expected value"
    return (
        "conflicting",
        f"env.{key} is already set to {json.dumps(env[key])}, expected {json.dumps(value)}; "
        "refusing to overwrite",
    )


def _settings_env_render(raw: bytes | None, doc: Mapping[str, Any]) -> bytes:
    """Re-dump keeping the file's own indent and trailing-newline state. Key order is
    the parsed order (dicts keep insertion order); only whitespace layout and number
    or escape spelling can differ from the original, which is what a JSON round-trip
    strictly costs."""
    text = raw.decode("utf-8") if raw else ""
    found = re.search(r"\n([ \t]+)\S", text)
    indent: Any = found.group(1) if found else 2
    out = json.dumps(doc, indent=indent, ensure_ascii=False)
    if raw is None or text.endswith("\n"):
        out += "\n"
    return out.encode("utf-8")


def _settings_env_with_key(raw: bytes | None, key: str, value: str) -> tuple[str, bytes | None]:
    """(classification, new bytes). New bytes are None when nothing needs to change."""
    doc = _settings_env_parse(raw)
    classification, detail = _settings_env_verdict(doc, key, value)
    if classification == "conflicting":
        raise OperationFailed(detail)
    if classification == "matching":
        return classification, None
    doc = {} if doc is None else doc
    doc.setdefault("env", {})[key] = value
    return classification, _settings_env_render(raw, doc)


def _settings_env_without_key(raw: bytes | None, key: str, value: str) -> bytes | None:
    """Bytes with ONLY ``env[key]`` removed, or None when there is nothing to remove."""
    doc = _settings_env_parse(raw)
    if doc is None or not isinstance(doc.get("env"), dict) or doc["env"].get(key) != value:
        return None
    del doc["env"][key]
    return _settings_env_render(raw, doc)


def _settings_env_can_rollback(mutation: "MutationRecord", raw: bytes | None) -> "PreflightResult":
    """Shared rollback-safety check: only a key THIS receipt added, and only while it
    still holds the value this receipt set."""
    if mutation.before.get("owned") != "true":
        return PreflightResult(True, "not-applied", "env key was not set by this receipt; left alone")
    key, value = mutation.before.get("env_key", ""), mutation.before.get("env_value", "")
    try:
        doc = _settings_env_parse(raw)
    except OperationFailed as exc:
        return PreflightResult(False, "drifted", str(exc))
    env = doc.get("env") if doc else None
    if not isinstance(env, Mapping) or key not in env:
        return PreflightResult(True, "not-applied", "env key already absent")
    if env[key] != value:
        return PreflightResult(False, "drifted", f"env.{key} changed after apply; not removing it")
    return PreflightResult(True, "owned", f"env.{key} matches the receipt")


def _settings_env_before(operation: "InstallOperation", raw: bytes | None, owned: bool) -> dict[str, str]:
    return {
        "state": "absent" if raw is None else "file",
        "owned": "true" if owned else "false",
        "env_key": operation.desired["env_key"],
        "env_value": operation.desired["env_value"],
    }


def _settings_env_load(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    """(raw, lstat) of the settings file; (None, None) when absent. Refuses a symlink
    or a non-regular file (an os.replace would silently swap the link out)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(st.st_mode):
        raise OperationFailed(f"{path} is not a regular file (symlink or special); refusing to touch it")
    try:
        return path.read_bytes(), st
    except OSError as exc:
        raise OperationFailed(f"{path} is unreadable: {exc}") from exc


def _settings_env_atomic_write(
    path: Path, new: bytes, original: bytes | None, st: Any
) -> None:
    """Temp file in the same dir, original mode and owner, fsync, compare-and-swap
    against the bytes we read, rename. A concurrent writer between our read and the
    rename aborts the write instead of being clobbered."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IMODE(st.st_mode) if st is not None else 0o600)
            if st is not None:
                made = os.fstat(handle.fileno())
                if (made.st_uid, made.st_gid) != (st.st_uid, st.st_gid):
                    try:
                        os.fchown(handle.fileno(), st.st_uid, st.st_gid)
                    except OSError as exc:
                        raise OperationFailed(f"cannot preserve owner of {path}: {exc}") from exc
            os.fsync(handle.fileno())
        try:
            current: bytes | None = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            raise OperationFailed(f"{path} changed while the installer was editing it; nothing written")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)




# ---- T-5474: first-launch workspace trust in ~/.claude.json ----

def _trust_parse(raw: bytes | None) -> dict[str, Any] | None:
    """The ~/.claude.json document, or None when absent. Same refusals as the settings parse
    (unparseable, duplicate keys, non-object top level) but no `env` rule: that is settings.json's."""
    if raw is None:
        return None

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate object keys")
        return dict(pairs)

    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise OperationFailed(f".claude.json is not parseable JSON ({exc}); refusing to touch it") from exc
    if not isinstance(doc, dict):
        raise OperationFailed(".claude.json top level is not an object; refusing to touch it")
    return doc


def _trust_prior(doc: Mapping[str, Any] | None, project: str) -> str:
    """absent-file | absent-projects | absent-entry | absent-key | false | true. Refuses a
    shape it does not understand rather than guess."""
    if doc is None:
        return "absent-file"
    if "projects" not in doc:
        return "absent-projects"
    projects = doc["projects"]
    if not isinstance(projects, Mapping):
        raise OperationFailed(".claude.json projects is not an object; refusing to touch it")
    if project not in projects:
        return "absent-entry"
    entry = projects[project]
    if not isinstance(entry, Mapping):
        raise OperationFailed(f".claude.json projects[{project}] is not an object; refusing to touch it")
    if TRUST_KEY not in entry:
        return "absent-key"
    if entry[TRUST_KEY] is True:
        return "true"
    if entry[TRUST_KEY] is False:
        return "false"
    raise OperationFailed(
        f".claude.json projects[{project}].{TRUST_KEY} is {json.dumps(entry[TRUST_KEY])}, not a boolean; refusing to overwrite"
    )


def _trust_with(raw: bytes | None, project: str) -> tuple[str, bytes | None]:
    """(prior state, new bytes); new bytes None when already true."""
    doc = _trust_parse(raw)
    prior = _trust_prior(doc, project)
    if prior == "true":
        return prior, None
    doc = {} if doc is None else doc
    doc.setdefault("projects", {}).setdefault(project, {})[TRUST_KEY] = True
    return prior, _settings_env_render(raw, doc)


def _trust_can_rollback(before: Mapping[str, str], raw: bytes | None) -> "PreflightResult":
    """Only what THIS receipt wrote, and only while it is still the op's write."""
    if before.get("owned") != "true":
        return PreflightResult(True, "not-applied", "workspace trust was not set by this receipt; left alone")
    project, prior = before.get("project_path", ""), before.get("prior", "")
    try:
        current = _trust_prior(_trust_parse(raw), project)
    except OperationFailed as exc:
        return PreflightResult(False, "drifted", str(exc))
    if current == "true":
        return PreflightResult(True, "owned", "workspace trust matches the receipt")
    if current.startswith("absent") or (current == "false" and prior == "false"):
        return PreflightResult(True, "not-applied", "workspace trust already reverted")
    return PreflightResult(False, "drifted", "workspace trust changed after apply; not touching it")


def _trust_without(raw: bytes | None, before: Mapping[str, str]) -> tuple[str, bytes | None]:
    """("noop"|"write"|"delete", bytes): remove ONLY what the op wrote. A container the op created
    is removed only when it is now empty; the file only when the op created it and `{}` remains."""
    project, prior = before["project_path"], before["prior"]
    doc = _trust_parse(raw)
    if doc is None or _trust_prior(doc, project) != "true":
        return "noop", None
    entry = doc["projects"][project]
    if prior == "false":
        entry[TRUST_KEY] = False
        return "write", _settings_env_render(raw, doc)
    del entry[TRUST_KEY]
    if prior in ("absent-entry", "absent-projects", "absent-file") and not entry:
        del doc["projects"][project]
        if prior in ("absent-projects", "absent-file") and not doc["projects"]:
            del doc["projects"]
            if prior == "absent-file" and not doc:
                return "delete", None
    return "write", _settings_env_render(raw, doc)


def _manifest_document(manifest: "Manifest") -> Mapping[str, Any]:
    # T-3637 purpose: yaml.safe_load of instance.yaml dominated test-suite time
    # (78 parses / 19s of a 22s slow-test single-run), so repeat calls must not
    # re-parse. T-4298 mechanism: agent_role_map's CONTENT-keyed parse cache
    # (CSafeLoader), which re-reads the file on every call and re-parses only
    # when its text changed. It replaces T-3637's (path, mtime_ns, size) key,
    # which served a stale document after a same-size rewrite inside one mtime
    # tick (the pattern T-4279 rejected; reproduced here at 57/200).
    # The returned document is SHARED with that cache, exactly as T-3637's was:
    # every reader (_runtime_candidate_refs_for_install, _post_start_command_count,
    # resolve_mailbox_writable_roots) only reads it; never mutate it.
    # Same exception TYPES and ManifestError mapping as before: OSError /
    # yaml.YAMLError -> "<path>: cannot reload manifest document: <exc>";
    # non-mapping -> "<path>: root must be a mapping"; UnicodeDecodeError
    # propagates. Diagnostic text may differ (CSafeLoader): a YAMLError's <exc>
    # names "<unicode string>", not the file path, and shows no caret snippet.
    from tools.agent_role_map import _parse_manifest

    path = manifest.path
    try:
        raw = _parse_manifest(path)
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{path}: cannot reload manifest document: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ManifestError(f"{path}: root must be a mapping")
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


# T-5447: the closed set of seat-core timers the installer may enable. A stem is
# either templated ("<stem>@<persona>.timer", template "<stem>@.timer") or a
# per-account singleton (the unit is its own template). Anything else is refused:
# the enable surface is an explicit allowlist, never an arbitrary unit.
SEAT_TIMER_TEMPLATED_STEMS = frozenset(
    {
        "pantheon-persona-consume",
        "pantheon-persona-quota",
        "pantheon-inbox-consumer",
        "pantheon-keep-going",
        "pantheon-mailbox-wake",
    }
)
SEAT_TIMER_SINGLETON_UNITS = frozenset({"pantheon-restart-drain.timer"})
_SEAT_TIMER_INSTANCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def _seat_timer_template_unit(timer_unit: str) -> str:
    if timer_unit in SEAT_TIMER_SINGLETON_UNITS:
        return timer_unit
    if timer_unit.endswith(".timer") and "@" in timer_unit:
        stem, _, rest = timer_unit.partition("@")
        instance = rest[: -len(".timer")]
        if stem in SEAT_TIMER_TEMPLATED_STEMS and _SEAT_TIMER_INSTANCE.match(instance):
            return f"{stem}@.timer"
    raise OperationFailed(f"timer enablement target is not an allowlisted seat timer: {timer_unit}")


def _seat_timer_template_path(account_home: Path, timer_unit: str) -> Path:
    return account_home / ".config" / "systemd" / "user" / _seat_timer_template_unit(timer_unit)


def _seat_timer_link_path(account_home: Path, timer_unit: str) -> Path:
    _seat_timer_template_unit(timer_unit)
    return account_home / ".config" / "systemd" / "user" / "timers.target.wants" / timer_unit


def _is_periodic_operation(operation: "InstallOperation") -> bool:
    return operation.kind in {"seat-periodic-timer-enable", "seat-periodic-timer-observe"}


def _is_periodic_mutation(mutation: "MutationRecord") -> bool:
    return mutation.kind == "seat-periodic-timer-enable"


# T-5448: runtime-controller bring-up (service enable + bounded observe) and the
# ho-resumer decommission. Both kinds are seat-local residue (outside the
# host_control envelope). The controller unit is Type=simple/Restart=always, so
# "verified" means: stays active/running with one stable MainPID and no restart
# across a bounded window, then the initial-viability record names the primary.
CONTROLLER_TEMPLATE_UNIT = "pantheon-runtime-controller@.service"
CONTROLLER_OBSERVE_WINDOW_SECONDS = 20  # 4x the unit's RestartSec=5
CONTROLLER_OBSERVE_INTERVAL_SECONDS = 2
# Closed list: the only units the decommission op may touch (T-5435 item 3).
HO_RESUMER_TIMER_UNIT = "pantheon-ho-resumer.timer"
HO_RESUMER_SERVICE_UNIT = "pantheon-ho-resumer.service"
HO_RESUMER_UNITS = (HO_RESUMER_TIMER_UNIT, HO_RESUMER_SERVICE_UNIT)
# T-5633: the closed vocabulary of an instance binding's ``legacy_units`` field. A
# binding that declares a name here has (or may be restored with) those legacy units,
# and only its plan carries their decommission op. Default empty: no op.
HO_RESUMER_LEGACY_UNIT_SET = "ho-resumer"
CODEX_KEEP_GOING_LEGACY_UNIT_SET = "codex-keep-going"
CODEX_KEEP_GOING_UNITS = ("codex-keep-going.timer", "codex-keep-going.service")
LEGACY_UNIT_SETS: Mapping[str, tuple[str, ...]] = {
    HO_RESUMER_LEGACY_UNIT_SET: HO_RESUMER_UNITS,
    CODEX_KEEP_GOING_LEGACY_UNIT_SET: CODEX_KEEP_GOING_UNITS,
}
SERVICE_RECOVERY_KINDS = frozenset({"timer-enable", "seat-service-enable-observe", "unit-fire"})


# T-5476: a seat-core periodic timer whose OnBootSec has already elapsed fires the moment it is
# armed (ActiveState=active, SubState=running). The observe tolerates that transient and polls.
PERIODIC_OBSERVE_WINDOW_SECONDS = 60
PERIODIC_OBSERVE_INTERVAL_SECONDS = 2
PERIODIC_TIMER_TRANSIENT_SUBSTATES = frozenset({"running", "elapsed"})
# T-5476: the session a bootstrap fire launched is stopped (SIGTERM) on rollback and must be
# confirmed gone within this bound, polled at the interval, before the containment key is reverted.
FIRE_SESSION_STOP_CONFIRM_SECONDS = 10
FIRE_SESSION_STOP_POLL_SECONDS = 1


def periodic_observe_prediction(operation: "InstallOperation") -> str:
    """The dry-run text for a periodic timer-observe op (T-5476)."""
    return (
        f"{operation.target} may fire on arm: active+running/elapsed is tolerated and polled up to "
        f"{operation.desired.get('observe_window_seconds', PERIODIC_OBSERVE_WINDOW_SECONDS)}s at "
        f"{operation.desired.get('observe_interval_seconds', PERIODIC_OBSERVE_INTERVAL_SECONDS)}s until "
        f"active+waiting with a finite next elapse; fails if it never waits or the run it triggered "
        f"({operation.desired.get('service', '?')}) fails"
    )


PANE_PID_FORMAT = "#{session_name}\t#{window_name}\t#{pane_index}\t#{pane_pid}"
PANE_PID_ABSENT = "absent"


def pane_pid_argv(target: str) -> list[str]:
    """T-5449: the one read-only tmux query used to read a pane pid."""
    return ["tmux", "display-message", "-p", "-t", target, PANE_PID_FORMAT]


def parse_pane_pid(returncode: int, stdout: str, target: str) -> str:
    """The pane pid for ``target`` ("<session>:<window>.<pane>"), or "absent" when
    tmux reports no pane OR reports a different pane's identity (tmux falls back to
    another pane for a missing target; T-5166)."""
    if returncode != 0:
        return PANE_PID_ABSENT
    fields = stdout.rstrip("\n").split("\t")
    if len(fields) != 4 or not fields[3].isdigit():
        return PANE_PID_ABSENT
    session, _, rest = target.partition(":")
    window, _, pane = rest.rpartition(".")
    if (session.lstrip("="), window, pane) != (fields[0], fields[1], fields[2]):
        return PANE_PID_ABSENT
    return fields[3]


def _controller_tmux_target(manifest: "Manifest", binding: "PersonaBinding") -> str:
    """T-5449: "<session>:<window>.<pane>" resolved by the controller's own config
    resolver, so the installer and the controller can never disagree on the pane."""
    from tools.harness_adapter.diagnostics import resolve_controller_config

    tmux = resolve_controller_config(
        _manifest_document(manifest), persona=binding.persona, role=binding.role
    ).tmux
    return f"{tmux.session}:{tmux.window}.{tmux.pane}"


def _controller_service_unit(persona: str) -> str:
    if not _SEAT_TIMER_INSTANCE.match(persona):
        raise OperationFailed(f"not a valid controller instance: {persona!r}")
    return f"pantheon-runtime-controller@{persona}.service"


def _controller_template_path(account_home: Path) -> Path:
    return account_home / ".config" / "systemd" / "user" / CONTROLLER_TEMPLATE_UNIT


def _controller_link_path(account_home: Path, unit: str) -> Path:
    # [Install] WantedBy=default.target
    return account_home / ".config" / "systemd" / "user" / "default.target.wants" / unit


def _is_service_enable_mutation(mutation: "MutationRecord") -> bool:
    return mutation.kind == "seat-service-enable-observe"


def _controller_values_from_snapshot(snap: "UnitSnapshot") -> dict[str, str]:
    """Render a SERVICE_BRINGUP_OBSERVE snapshot to the four canonical strings
    `_judge_controller_samples` compares. A missing or non-canonical MainPID or
    NRestarts raises OperationFailed."""
    values: dict[str, str] = {
        "ActiveState": snap.active_state or "",
        "SubState": snap.sub_state or "",
    }
    for key, value in (("MainPID", snap.main_pid), ("NRestarts", snap.n_restarts)):
        if value is None or isinstance(value, _us.NonCanonical):
            raise OperationFailed(f"controller show {key} is not a canonical integer")
        values[key] = str(value)
    return values


def _judge_controller_samples(
    samples: Sequence[Mapping[str, str]], *, adopted: bool = False
) -> None:
    """Raise unless every sample is active/running with the SAME non-zero MainPID
    and an unchanging NRestarts. An empty sample list fails.

    NRestarts must be 0 when this op started the controller. An adopted controller
    (already enabled before the op) may carry restarts from earlier in its life, so
    it is judged against the first sample instead (T-5478)."""
    if not samples:
        raise OperationFailed("controller observation produced no samples")
    first_pid = samples[0]["MainPID"]
    for index, sample in enumerate(samples):
        if sample["ActiveState"] != "active" or sample["SubState"] != "running":
            raise OperationFailed(
                f"controller not active/running at sample {index} "
                f"({sample['ActiveState']}/{sample['SubState']})"
            )
        if sample["MainPID"] == "0":
            raise OperationFailed(f"controller MainPID is zero at sample {index}")
        if sample["MainPID"] != first_pid:
            raise OperationFailed(
                f"controller MainPID changed {first_pid} -> {sample['MainPID']} at sample {index}"
            )
        baseline = samples[0]["NRestarts"] if adopted else "0"
        if sample["NRestarts"] != baseline:
            raise OperationFailed(
                f"controller NRestarts={sample['NRestarts']} at sample {index}"
                + (f" (baseline {baseline})" if adopted else "")
            )


def _controller_desired_int(operation: "InstallOperation", key: str) -> int:
    return _canonical_decimal_int(operation.desired.get(key), key, allow_zero=False)


def _desired_int_or(operation: "InstallOperation", key: str, default: int) -> int:
    raw = operation.desired.get(key)
    return default if raw is None else _canonical_decimal_int(raw, key, allow_zero=False)


def _legacy_units_for_operation(operation: "InstallOperation") -> tuple[str, ...]:
    raw = operation.desired.get("units", "")
    for units in LEGACY_UNIT_SETS.values():
        if raw == ",".join(units):
            return units
    raise OperationFailed("decommission unit list is not a closed legacy-unit set")


def _legacy_unit_paths(
    units: tuple[str, ...], unit_dir: Path, quarantine_dir: Path
) -> tuple[tuple[str, Path, Path], ...]:
    return tuple((name, unit_dir / name, quarantine_dir / name) for name in units)


def _legacy_units_for_receipt(names: list[str]) -> tuple[str, ...]:
    selected = set(names)
    if not selected:
        raise OperationFailed("decommission receipt names no units")
    for units in LEGACY_UNIT_SETS.values():
        if selected <= set(units):
            return units
    raise OperationFailed("decommission receipt names units outside one closed legacy-unit set")


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


STALE_UNIT_FAILURE_RESET_PROPERTIES = ReadPurpose.STALE_RESET.properties

# Historical per-site text strictness, keyed by read purpose (pre-5c.1 parsers,
# unchanged): strict-exact (stale: whitespace-only lines skipped; unit-file:
# only empty lines skipped), required-subset with value.strip() (unit-fire
# poll), and lenient key=value (the reads the pre-5c.1 _systemd_show served).
_SHOW_STRICT_EXACT_STRIP = "strict-exact-strip"
_SHOW_STRICT_EXACT = "strict-exact"
_SHOW_REQUIRED_SUBSET = "required-subset"
_SHOW_LENIENT = "lenient"
_SHOW_FAMILY: dict[ReadPurpose, str] = {
    ReadPurpose.STALE_RESET: _SHOW_STRICT_EXACT_STRIP,
    ReadPurpose.UNIT_FILE_ACTIVE: _SHOW_STRICT_EXACT,
    ReadPurpose.UNIT_FIRE_POLL: _SHOW_REQUIRED_SUBSET,
    ReadPurpose.SERVICE_SETTLE: _SHOW_LENIENT,
    ReadPurpose.SERVICE_OBSERVE: _SHOW_LENIENT,
    ReadPurpose.TIMER_ELAPSE: _SHOW_LENIENT,
    ReadPurpose.RECEIVER_ACTIVE: _SHOW_LENIENT,
    ReadPurpose.RELOAD_STATE: _SHOW_LENIENT,
    ReadPurpose.SERVICE_BRINGUP_OBSERVE: _SHOW_STRICT_EXACT,
}
_UNIT_FIRE_REQUIRED_PROPERTIES = ("InvocationID", "ActiveState", "Result", "ExecMainStatus")


def _show_protocol_error(reason: str) -> _us.UserManagerProtocolError:
    return _us.UserManagerProtocolError(f"show output {reason}", reason=reason)


def _systemctl_show_values(purpose: ReadPurpose, stdout: str) -> dict[str, str]:
    """Split `systemctl show` stdout with the purpose's historical strictness.
    Raises UserManagerProtocolError whose ``reason`` completes the historical
    "<subject> show output <reason>" refusal text."""

    allowed = purpose.properties
    family = _SHOW_FAMILY[purpose]
    values: dict[str, str] = {}
    if family in (_SHOW_STRICT_EXACT_STRIP, _SHOW_STRICT_EXACT):
        allowed_set = set(allowed)
        for line in stdout.splitlines():
            if (not line.strip()) if family == _SHOW_STRICT_EXACT_STRIP else line == "":
                continue
            if line.count("=") != 1:
                raise _show_protocol_error("is malformed")
            key, value = line.split("=", 1)
            if key not in allowed_set:
                raise _show_protocol_error("contains unexpected keys")
            if key in values:
                raise _show_protocol_error("contains duplicate keys")
            values[key] = value
        if set(values) != allowed_set:
            raise _show_protocol_error("missing required keys")
        return values
    if family == _SHOW_REQUIRED_SUBSET:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            if line.count("=") != 1:
                raise _show_protocol_error("is malformed")
            key, value = line.split("=", 1)
            if key in values:
                raise _show_protocol_error("contains duplicate keys")
            if key in allowed:
                values[key] = value.strip()
        for required in _UNIT_FIRE_REQUIRED_PROPERTIES:
            if required not in values:
                raise _show_protocol_error("missing required keys")
        return values
    lenient: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            lenient[key] = value
    return {key: value for key, value in lenient.items() if key in allowed}


def _systemctl_snapshot_from_stdout(unit: str, purpose: ReadPurpose, stdout: str) -> UnitSnapshot:
    """Typed conversion of one `systemctl show` stdout (text -> typed, never
    the reverse). The source digest is over the exact stdout bytes."""

    values = _systemctl_show_values(purpose, stdout)
    raw = stdout.encode("utf-8")
    source = _us.SnapshotSource(
        transport=SYSTEMCTL_TRANSPORT,
        encoding=SYSTEMCTL_SHOW_STDOUT,
        length=len(raw),
        sha256=_sha256_bytes(raw),
    )
    return _us.unit_snapshot_from_text_values(unit, purpose, values, source=source)


def _systemctl_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or result.stdout) or "").strip()


class SystemctlUserManager:
    """``UserManagerPort`` over the operator-run `systemctl --user` command line.

    ``run`` is called as ``run(argv)`` or ``run(argv, timeout=...)`` and must
    return a CompletedProcess; LocalHostBackend passes a proxy that resolves
    its own ``_run`` per call, so subclasses overriding ``_run`` still
    intercept every call. Each method emits exactly the argv (list or tuple,
    as historically built) the pre-5c.1 call site emitted, and passes a
    timeout only where that site did. Transport exceptions from ``run``
    (TimeoutExpired, OSError) propagate unchanged, as they always have.
    """

    transport = SYSTEMCTL_TRANSPORT
    address: str | None = None
    address_source = "n/a"

    def __init__(self, run: Any) -> None:
        self._run = run

    def _call(self, argv: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
        if timeout is None:
            return self._run(argv)
        return self._run(argv, timeout=timeout)

    @staticmethod
    def show_argv(unit: str, purpose: ReadPurpose) -> Sequence[str]:
        if purpose is ReadPurpose.UNIT_FILE_ACTIVE:
            argv_tuple: tuple[str, ...] = ("systemctl", "--user", "show", unit)
            for item in purpose.properties:
                argv_tuple += ("-p", item)
            return argv_tuple
        argv = ["systemctl", "--user", "show", unit]
        if purpose is ReadPurpose.STALE_RESET:
            argv.append("--all")
        for item in purpose.properties:
            argv.extend(("-p", item))
        return argv

    def _outcome(self, result: subprocess.CompletedProcess[str]) -> _us.UserManagerCallOutcome:
        ok = result.returncode == 0
        return _us.UserManagerCallOutcome(
            ok=ok,
            error_name="" if ok else "exit-status",
            detail="" if ok else _us._bounded(_systemctl_failure_detail(result)),
            transport_evidence=result,
        )

    # reads -------------------------------------------------------------

    def manager_state(self) -> _us.ManagerState:
        # No pre-5c.1 site reads the manager state over the command line, and a
        # pure refactor adds no new argv (T-2917 5c.1 review ruling (1)).
        raise NotImplementedError("SystemctlUserManager does not read the manager state")

    def manager_environment(self) -> int:
        result = self._call(("systemctl", "--user", "show-environment"), None)
        if result.returncode != 0:
            raise _us.UserManagerUnavailable(_systemctl_failure_detail(result), transport_evidence=result)
        return sum(1 for line in (result.stdout or "").splitlines() if line)

    def read_unit(
        self,
        unit: str,
        purpose: ReadPurpose,
        *,
        timeout: float | None = None,
    ) -> UnitSnapshot:
        result = self._call(self.show_argv(unit, purpose), timeout)
        family = _SHOW_FAMILY[purpose]
        # The stale and unit-fire sites normalised a missing stdout to "";
        # the unit-file and lenient sites read result.stdout directly.
        if family in (_SHOW_STRICT_EXACT_STRIP, _SHOW_REQUIRED_SUBSET):
            stdout = result.stdout or ""
        else:
            stdout = result.stdout
        if result.returncode != 0:
            raise _us.UserManagerCallFailed(
                "exit-status",
                _systemctl_failure_detail(result),
                transport_evidence=result,
            )
        try:
            return _systemctl_snapshot_from_stdout(unit, purpose, stdout)
        except _us.UserManagerProtocolError as exc:
            exc.transport_evidence = result
            raise

    def unit_file_enablement(self, unit: str, *, timeout: float | None = None) -> _us.UnitFileEnablement:
        result = self._call(("systemctl", "--user", "is-enabled", unit), timeout)
        return _us.UnitFileEnablement(state=(result.stdout or "").strip(), enabled=result.returncode == 0)

    def environment_file_declared(self, unit: str) -> bool:
        result = self._call(("systemctl", "--user", "cat", unit), None)
        return result.returncode == 0 and "EnvironmentFile" in result.stdout

    # writes ------------------------------------------------------------

    def reload(self) -> _us.UserManagerCallOutcome:
        return self._outcome(self._call(("systemctl", "--user", "daemon-reload"), None))

    def reset_failed_unit(self, unit: str) -> _us.UserManagerCallOutcome:
        return self._outcome(self._call(("systemctl", "--user", "reset-failed", unit), None))

    def start_unit(self, unit: str, *, wait: bool, timeout: float | None = None) -> _us.UserManagerCallOutcome:
        argv = ("systemctl", "--user", "start", unit) if wait else ("systemctl", "--user", "start", "--no-block", unit)
        return self._outcome(self._call(argv, timeout))

    def stop_unit(self, unit: str, *, timeout: float | None = None) -> _us.UserManagerCallOutcome:
        return self._outcome(self._call(("systemctl", "--user", "stop", unit), timeout))

    def enable_now(self, unit: str) -> _us.UserManagerCallOutcome:
        return self._outcome(self._call(("systemctl", "--user", "enable", "--now", unit), None))

    def disable_now(self, unit: str) -> _us.UserManagerCallOutcome:
        return self._outcome(self._call(("systemctl", "--user", "disable", "--now", unit), None))


def _completed(outcome: _us.UserManagerCallOutcome) -> subprocess.CompletedProcess[str]:
    """The CompletedProcess a SystemctlUserManager write carries as evidence."""

    return outcome.transport_evidence  # type: ignore[return-value]


def _stale_unit_failure_reset_view(snapshot: UnitSnapshot) -> StaleUnitFailureResetSnapshot:
    return StaleUnitFailureResetSnapshot(
        load_state=snapshot.load_state or "",
        active_state=snapshot.active_state or "",
        sub_state=snapshot.sub_state or "",
        result=snapshot.result or "",
        exec_main_status=_us.render_exec_status(snapshot.exec_main_status),
        invocation_id=snapshot.invocation_id or "",
        job=_us.render_job(snapshot.job_id),
        stdout_length=snapshot.source.length,
        stdout_sha256=snapshot.source.sha256,
    )


def _parse_stale_unit_failure_reset_show(stdout: str) -> StaleUnitFailureResetSnapshot:
    """Legacy-shaped wrapper over the typed STALE_RESET parse."""

    try:
        snapshot = _systemctl_snapshot_from_stdout("", ReadPurpose.STALE_RESET, stdout)
    except _us.UserManagerProtocolError as exc:
        raise OperationFailed(f"stale unit show output {exc.reason}") from None
    return _stale_unit_failure_reset_view(snapshot)


STALE_ADOPTED = "adopted-converged"
MAINTENANCE_REFUSAL = (
    "{persona} is in seat maintenance: the consume bootstrap fire would be skipped (T-5282) and "
    "launch nothing; exit maintenance first (python3 tools/notify_policy.py exit-maintenance {persona})"
)
# T-5475: operator-requested one-shot bypass of consume's seat-maintenance skip, bound into the plan
# digest by the unit-fire op's `maintenance_bypass` desired key (ABSENT when not requested).
MAINTENANCE_BYPASS_KEY = "maintenance_bypass"
MAINTENANCE_BYPASS_EVIDENCE = "maintenance_bypass_token"
MAINTENANCE_BYPASS_DEEP_SLEEP_REFUSAL = (
    "{persona} is in deep-sleep: consume ignores the maintenance bypass token during deep-sleep (it never "
    "bypasses deep-sleep), so --maintenance-bypass cannot bootstrap this seat; wake it first"
)
MAINTENANCE_BYPASS_PREFLIGHT_NOTE = "maintenance armed; one-shot bypass token will be written before the fire"
MAINTENANCE_BYPASS_TTL_SECONDS = 180  # SOP phase 4: now + 3 min (consume accepts up to 15 min)


def _maintenance_bypass_requested(operation: "InstallOperation") -> bool:
    return operation.desired.get(MAINTENANCE_BYPASS_KEY) == "true"


def _maintenance_gate(
    persona: str,
    *,
    in_maintenance: bool,
    deep_sleeping: "Callable[[], bool]",
    bypass: bool,
) -> str | None:
    """T-5475: the ONE maintenance verdict for a bootstrap fire, shared by both backends and by the
    dry run and execute. None == the fire may proceed. Not armed: proceed. Armed without the
    bypass: today's refusal, unchanged. Armed with the bypass: proceed, unless the seat is
    deep-sleeping (consume then ignores the token), consulted only in that case."""
    if not persona or not in_maintenance:
        return None
    if not bypass:
        return MAINTENANCE_REFUSAL.format(persona=persona)
    if deep_sleeping():
        return MAINTENANCE_BYPASS_DEEP_SLEEP_REFUSAL.format(persona=persona)
    return None


def _bypass_token_payload(persona: str, by: str, now: "datetime | None" = None) -> dict[str, str]:
    """The token handmove-seat writes: {"persona","expires": now+3min,"by"}. The clock is read at
    call time (UTC), never pinned."""
    from datetime import timedelta

    moment = now if now is not None else datetime.now(timezone.utc)
    expires = moment + timedelta(seconds=MAINTENANCE_BYPASS_TTL_SECONDS)
    return {"persona": persona, "expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), "by": by}


def _bypass_token_path(state_root: Path, persona: str) -> Path:
    from tools import persona_bringup

    return persona_bringup.maintenance_bypass_path(state_root, persona)


def _write_bypass_token(state_root: Path, persona: str, by: str) -> Path:
    """Write consume's one-shot token atomically (tmp, fsync, hard-link into place, 0600). Refuses to
    overwrite: an existing file is an unknown writer's token."""
    path = _bypass_token_path(state_root, persona)
    refusal = f"a maintenance bypass token already exists at {path}; refusing to overwrite it"
    if os.path.lexists(path):
        raise OperationFailed(refusal)
    state_root.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(_bypass_token_payload(persona, by)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # fails if a writer raced us in; never replaces
    except FileExistsError:
        raise OperationFailed(refusal) from None
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return path


def _remove_bypass_token(state_root: Path, persona: str) -> bool:
    """Idempotent; True when a file was removed."""
    try:
        _bypass_token_path(state_root, persona).unlink()
    except FileNotFoundError:
        return False
    return True


def _bypass_written(before: "Mapping[str, str]") -> bool:
    return before.get(MAINTENANCE_BYPASS_EVIDENCE) == "written"


def _stale_adoption_shape_refusal(pre: UnitSnapshot) -> str | None:
    """T-5471: None when ``pre`` has the converged-service shape (no further read needed):
    loaded, inactive/dead, Result=success, no queued job, empty or well-formed InvocationID."""
    if pre.load_state != "loaded":
        return f"load_state={pre.load_state}"
    if pre.active_state != "inactive" or pre.sub_state != "dead":
        return f"active={pre.active_state} sub={pre.sub_state}"
    if pre.result != "success":
        return f"result={pre.result}"
    if not _us.unit_fire_job_idle(pre.job_id):
        return f"job={_us.render_job(pre.job_id)}"
    invocation = pre.invocation_id or ""
    if invocation and not _us.valid_invocation_id(invocation):
        return "malformed InvocationID"
    return None


def _classify_stale_reset_for_install(
    snapshot: "UnitSnapshot | StaleUnitFailureResetSnapshot",
    success_exit: "Callable[[], Any]",
) -> str:
    """The installer's ONE stale-state classifier (dry run and execute, Local and Sim).

    ``_us.classify_stale_reset`` -- shared with the host_control broker and left untouched --
    answers first. Only its ``pre-existing loaded service`` refusal is reconsidered: a
    consume service that is loaded, inactive/dead, Result=success with an exit status inside
    the unit's success policy ({0} u SuccessExitStatus) is an already-converged seat
    (mason, T-2677): ADOPTED, no reset needed. Failed, active, running, queued or
    out-of-policy services still refuse. ``success_exit`` is read lazily (only for the
    adoptable shape)."""
    if isinstance(snapshot, StaleUnitFailureResetSnapshot):
        snapshot = _stale_unit_failure_reset_typed(snapshot)
    try:
        return _classify_stale_unit_failure_reset_snapshot(snapshot)
    except OperationFailed as exc:
        if str(exc) != _us.STALE_LOADED_REFUSAL:
            raise
        why = _stale_adoption_shape_refusal(snapshot)
        if why is None:
            policy = success_exit()
            if not _us.exit_status_within_policy(snapshot.exec_main_status, policy):
                why = (
                    f"exit status {_us.render_exec_status(snapshot.exec_main_status)} outside the "
                    f"success policy [{_us.render_success_exit(policy)}]"
                )
        if why is None:
            return STALE_ADOPTED
        raise OperationFailed(f"{exc}: not adoptable ({why})") from None


def _bootstrap_residue(persona_state_root: Path, persona: str) -> list[str]:
    """Existing phase-Q residue paths (SOP §4), relative to persona_state_root. Read-only."""
    root = persona_state_root
    return [
        str((Path(_quarantine_class_base(str(root), persona, cls)) / name).relative_to(root))
        for cls, name in _bootstrap_residue_entries(root, persona)
    ]


def _unit_fire_idle_prediction(classify: "Callable[[], str]") -> tuple[bool, str]:
    """T-5471: will the disabled first fire's pre-enqueue idle proof pass once the
    stale-unit-failure-reset gate has run? Answered with the SAME classifier that gate
    uses, so the dry run reports what execute will do. clean-noop and reset-required end
    idle (the reset clears the stale failed record); an adopted converged service passes
    the baseline-aware proof (its old InvocationID is recorded and ignored)."""

    try:
        classification = classify()
    except OperationFailed as exc:
        return False, f"consume service is not idle before disabled first fire: {exc}"
    return True, f"consume service predicted idle before disabled first fire ({classification})"


def _unit_fire_converged_baseline(snapshot: "UnitFirePollSnapshot") -> bool:
    """T-5471: a pre-enqueue snapshot with a PRIOR InvocationID is an acceptable baseline only
    when the service is a settled success: inactive/dead, Result=success, exit status inside
    its success policy, no queued job, well-formed id. (A ``failed`` service with an id is
    never idle.)"""
    return (
        snapshot.active_state == "inactive"
        and snapshot.substate == "dead"
        and snapshot.result == "success"
        and snapshot.invocation_id_valid
        and _us.unit_fire_job_idle(_unit_fire_poll_typed(snapshot).job_id)
        and _exec_status_within_success_policy(snapshot.exec_main_status, snapshot.success_exit_status)
    )


def _parse_systemctl_timer_show(stdout: str) -> TimerUnitState:
    """Legacy-shaped wrapper over the typed UNIT_FILE_ACTIVE parse plus the
    strict timer classifier."""

    try:
        snapshot = _systemctl_snapshot_from_stdout("", ReadPurpose.UNIT_FILE_ACTIVE, stdout)
    except _us.UserManagerProtocolError as exc:
        raise OperationFailed(f"timer show output {exc.reason}") from None
    return _timer_unit_state_from_snapshot(snapshot)


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
    if "personas" in raw:
        raise ManifestError(
            "persona_installer.quota_check.personas is retired: the planned-quota flip is fleet policy "
            "for every seat with a manifest fallback, not an allowlist (T-5446, the operator TG 12274)"
        )
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


_FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


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


# T-5447: seat-core periodic units (one installer run installs the whole seat).
RESTART_DRAIN_ENABLE_DROPIN = (
    "# ORG-020 Phase 1 \u2014 Orchestrator restart-drain enable flag "
    "(T-0469 re-fire 2026-05-22, post T-0534+T-0535)\n"
    "[Service]\n"
    "Environment=PANTHEON_RESTART_DRAIN_ENABLED=1\n"
)
_SYSTEMD_TEMPLATE_DIR = Path("tools") / "systemd"
# (op-name, template file stems; the timer-unit/service-unit pair is installed)
_SEAT_CORE_UNIT_FILES = (
    ("inbox-consumer", "pantheon-inbox-consumer@.service", "pantheon-inbox-consumer@.timer"),
    ("keep-going", "pantheon-keep-going@.service", "pantheon-keep-going@.timer"),
    ("restart-drain", "pantheon-restart-drain.service", "pantheon-restart-drain.timer"),
)
_UNIT_FAILURE_ALERT_TEMPLATE = "pantheon-unit-failure-alert@.service"


@dataclass(frozen=True)
class _SeatCorePeriodics:
    # (operation_id, unit filename, rendered content), in install order
    installs: tuple[tuple[str, str, str], ...]
    # (op-name, timer unit, service unit) for each timer to enable + observe
    timers: tuple[tuple[str, str, str], ...]


def _mailbox_wake_membership(manifest: "Manifest", persona: str) -> bool:
    """True when ``persona`` is on either mailbox wake list. The lists are read
    through mailbox_onboard's own parser (lazy import: mailbox_onboard imports
    this module), so the two stay single-sourced."""
    from tools import mailbox_onboard as _mo

    mailbox = manifest.installer.get("mailbox")
    if not isinstance(mailbox, dict):
        return False
    try:
        primary = _mo._persona_list_field(mailbox, "wake_timer_personas")
        secondary = _mo._persona_list_field(mailbox, "wake_timer_secondary_personas")
    except _mo.OnboardError as exc:
        raise ManifestError(str(exc)) from None
    return persona in primary or persona in secondary


def _keep_going_planned(persona: str) -> bool:
    """T-5482: keep-going is not planned for personas keep_going.py deliberately
    exempts (_NO_KEEP_GOING: currently archivist; operator-directed,
    T-3811/T-3832). Codex personas are supported through the harness-aware
    submit profile and must receive the same installer-owned anti-idle leg.
    An armed timer for an exempt persona exit-2s at argparse.
    Lazy import: keep_going pulls agent_role_map at import time."""
    from tools import keep_going as _kg

    return persona not in _kg._NO_KEEP_GOING


def _seat_core_periodics(
    repo_root: Path,
    manifest: "Manifest",
    binding: "PersonaBinding",
    unit_banner: str,
    revision: str,
) -> _SeatCorePeriodics:
    def render(name: str) -> str:
        return unit_banner + (repo_root / _SYSTEMD_TEMPLATE_DIR / name).read_text(encoding="utf-8")

    installs: list[tuple[str, str, str]] = []
    timers: list[tuple[str, str, str]] = [
        (
            "quota",
            f"pantheon-persona-quota@{binding.persona}.timer",
            f"pantheon-persona-quota@{binding.persona}.service",
        )
    ]
    keep_going_planned = _keep_going_planned(binding.persona)
    for name, service, timer in _SEAT_CORE_UNIT_FILES:
        if name == "keep-going" and not keep_going_planned:
            continue
        installs.append((f"periodic.{name}.service-unit", service, render(service)))
        installs.append((f"periodic.{name}.timer-unit", timer, render(timer)))
        if name == "restart-drain":
            timer_unit, service_unit = timer, service
            installs.append(
                (
                    "periodic.restart-drain.dropin",
                    "pantheon-restart-drain.service.d/enable.conf",
                    unit_banner + RESTART_DRAIN_ENABLE_DROPIN,
                )
            )
        else:
            timer_unit = timer.replace("@.", f"@{binding.persona}.")
            service_unit = service.replace("@.", f"@{binding.persona}.")
        timers.append((name, timer_unit, service_unit))
    installs.append(
        (
            "periodic.unit-failure-alert.service-unit",
            _UNIT_FAILURE_ALERT_TEMPLATE,
            render(_UNIT_FAILURE_ALERT_TEMPLATE),
        )
    )
    if _mailbox_wake_membership(manifest, binding.persona):
        from tools import mailbox_onboard as _mo

        wake_banner = _mo._unit_banner(manifest.instance_id, binding.persona, revision)
        for suffix in ("service", "timer"):
            template = repo_root / _SYSTEMD_TEMPLATE_DIR / f"{_mo.WAKE_UNIT_STEM}@.{suffix}"
            installs.append(
                (
                    f"periodic.mailbox-wake.{suffix}-unit",
                    template.name,
                    _mo._rendered_unit(template, wake_banner).decode("utf-8"),
                )
            )
        timers.append(
            (
                "mailbox-wake",
                f"{_mo.WAKE_UNIT_STEM}@{binding.persona}.timer",
                f"{_mo.WAKE_UNIT_STEM}@{binding.persona}.service",
            )
        )
    return _SeatCorePeriodics(tuple(installs), tuple(timers))


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
    # T-5633: declared legacy unit sets (closed vocabulary, LEGACY_UNIT_SETS).
    legacy_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuotaCheckInstallConfig:
    cadence_minutes: int
    randomized_delay_seconds: int
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
    def from_bytes(cls, path: Path, raw: bytes, *, strict_flip_keys: bool = True) -> "Manifest":
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
        # T-5446 Phase A: a `false`/non-bool on a retired flip key is a manifest error here
        # (installer, verify-seat, CI); the running controller instead ignores it (T-5458).
        # strict_flip_keys=False is for RUNTIME callers (controller readiness, the codex
        # bridge, the broker): a bad flip key must never change what a live seat does.
        flip_violations = flip_key_violations(document) if strict_flip_keys else []
        if flip_violations:
            raise ManifestError(f"{path}: {'; '.join(flip_violations)}")
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
    def load(cls, path: Path, *, strict_flip_keys: bool = True) -> "Manifest":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ManifestError(f"{path}: cannot load manifest: {exc}") from exc
        return cls.from_bytes(path, raw, strict_flip_keys=strict_flip_keys)

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
            legacy_units = _parse_legacy_units(raw.get("legacy_units"), self.path, str(role))
            return PersonaBinding(
                role=str(role),
                persona=persona,
                account=account,
                configured_harness=_normalize_harness(harness),
                persona_brief_ref=persona_brief_ref,
                credential_refs=refs,
                legacy_units=legacy_units,
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


def _parse_legacy_units(raw: Any, path: Path, role: str) -> tuple[str, ...]:
    """T-5633: a binding's ``legacy_units`` -- absent, null or a list of distinct
    names from the closed LEGACY_UNIT_SETS vocabulary. Anything else is refused."""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{path}: {role}.legacy_units must be a list")
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ManifestError(f"{path}: {role}.legacy_units entries must be strings")
        if item not in LEGACY_UNIT_SETS:
            raise ManifestError(
                f"{path}: {role}.legacy_units: unknown legacy unit set {item!r} "
                f"(known: {', '.join(sorted(LEGACY_UNIT_SETS))})"
            )
        if item in seen:
            raise ManifestError(f"{path}: {role}.legacy_units: duplicate {item!r}")
        seen.append(item)
    return tuple(seen)


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


EXECUTION_CONTEXT_STAGE_PREFIX = ExecutionContext("stage-prefix")
EXECUTION_CONTEXT_STAGED_ACTIVATE = ExecutionContext("staged-activate")


def _context_with_roots(context: ExecutionContext, plan: "InstallPlan") -> ExecutionContext:
    """Bind a plan's (already manifest-pinned) allowed roots to a context."""
    declared = plan.provenance.get(ALLOWED_TARGET_ROOTS_KEY, "")
    roots = tuple(item for item in declared.split(",") if item)
    return dataclasses.replace(context, allowed_roots=roots)


def _operation_in_scope(
    operation: InstallOperation,
    scopes: frozenset[ExecutionScope],
) -> bool:
    return operation.execution_scope in scopes


STAGED_ACTIVATE_TIMER_REFUSAL = "timer already enabled at staged activation boundary"


def _staged_activate_enabled_timer_refusal(
    workflow: str, already_enabled: bool, observe_seen: bool
) -> str | None:
    """T-5472: the ONE staged-activate timer-enable boundary rule.

    Called by the dry-run preflight AND by execute in both backends, so they cannot drift.
    ``observe_seen`` is "connective.timer-observe" already in the backend's events (live
    legacy-convergence evidence seeded by an earlier preflight/execute); Sim has no such
    evidence and passes False."""
    if workflow == "staged-activate" and already_enabled and not observe_seen:
        return STAGED_ACTIVATE_TIMER_REFUSAL
    return None


def workflow_scopes(workflow: str) -> frozenset[ExecutionScope]:
    """The execution scopes ``_preflight_apply`` preflights under each workflow."""
    if workflow == "all-in-one":
        return frozenset({"all"})
    return frozenset({"all", "staged-cutover"})


def preflight_in_context(
    backend: HostBackend, operation: InstallOperation, context: ExecutionContext | None
) -> PreflightResult:
    """Preflight with the workflow when the backend's ``preflight`` accepts a ``context``
    parameter; otherwise (one-arg overrides) the plain form. Signature check only."""
    import inspect

    if context is not None and "context" in inspect.signature(backend.preflight).parameters:
        return backend.preflight(operation, context)
    return backend.preflight(operation)


def _brief_readability_preflight(operation: InstallOperation) -> PreflightResult:
    path = Path(operation.target)
    ok = path.is_file() and os.access(path, os.R_OK)
    return PreflightResult(
        ok,
        "persona",
        "persona brief present" if ok else "persona brief missing",
    )


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
    # ORG-050 audit record: source / host / harness (actor is above; the result
    # is `phase`). Additive and defaulted so receipts written before T-4809 load.
    host: str = ""
    harness: str = ""
    source: str = ""

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
            host=raw.get("host", ""),
            harness=raw.get("harness", ""),
            source=raw.get("source", ""),
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
    break_glass: bool = False


class HostBackend(Protocol):
    evidence_scope: str

    def preflight(
        self, operation: InstallOperation, context: ExecutionContext | None = None
    ) -> PreflightResult: ...

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

    def rollback_mutation(
        self,
        mutation: MutationRecord,
        *,
        allowed_roots: Sequence[str] = (),
    ) -> None: ...

    def rollback_intent(
        self,
        operation: InstallOperation,
    ) -> MutationRecord | None: ...

    def issued_timer_enable_recovery_mutation(
        self,
        operation: InstallOperation,
        evidence: Mapping[str, str],
    ) -> MutationRecord | None: ...


class ReceiptSink(Protocol):
    """Receipt persistence, split out of ``HostBackend`` (ORG-052 Unit 5a,
    T-2917).

    A backend performs host work and returns typed mutation/evidence
    records; it never owns receipt persistence. On the operator-run local
    path the installer writes receipts through a ``FileReceiptStore``; on
    the broker path the broker journal is the only receipt owner and the
    executor (the backend) has no sink at all (ORG-052 section 6).
    """

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None: ...


def default_receipt_sink(backend: HostBackend) -> ReceiptSink:
    """The receipt sink the installer uses when the caller names none.

    Closed and explicit rather than duck-typed: the simulated backend
    exposes its in-memory sink (so ordering proofs still observe receipt
    events interleaved with operation events), the local backend writes
    planned receipt files, and any other backend must be handed a sink.
    """

    if isinstance(backend, SimulatedHostBackend):
        return backend.receipt_sink
    if isinstance(backend, LocalHostBackend):
        return FileReceiptStore()
    raise InstallerError(
        f"no receipt sink for backend {type(backend).__name__}; pass receipt_sink"
    )


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
    # T-4298: agent_role_map's content-keyed parse cache (CSafeLoader) instead of an
    # uncached pure-Python safe_load. Same freshness (the file is re-read on EVERY
    # call; re-parsed only when its text changed) and the same exceptions
    # (OSError / yaml.YAMLError -> None below; UnicodeDecodeError propagates, as
    # before). The document is SHARED with that cache: only read below, never mutated.
    from tools.agent_role_map import _parse_manifest

    try:
        raw = _parse_manifest(manifest.path)
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


LEGACY_CODEX_STANDING_SCHEMA = "pantheon-codex-standing-instructions/v1"
HARNESS_STANDING_SCHEMA = "pantheon-harness-standing-instructions/v1"
#: The harnesses whose standing instructions something installs: codex (this
#: installer) and hermes (prepare_hermes_launch via hermes_render_inputs).
STANDING_INSTRUCTION_HARNESSES = ("codex", "hermes")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def standing_instruction_entries(
    installer: Mapping[str, Any],
    persona: str,
    *,
    label: str = "manifest",
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    """A persona's raw standing-instruction entries, keyed by harness (T-5011 PR-D;
    T-4924 plan §2.5 gap 4).

    Two manifest keys under ``persona_installer``:

    * ``harness_standing_instructions`` (schema
      ``pantheon-harness-standing-instructions/v1``): ``personas.<p>.<harness>``
      is one entry or a list of entries; an entry may carry
      ``extract: {heading: ...}`` (the hermes SOUL.md region).
    * the legacy ``codex_standing_instructions`` (schema
      ``pantheon-codex-standing-instructions/v1``): ``personas.<p>`` is one
      entry, read as ``{codex: entry}``.

    Both keys naming the same (persona, harness) is a ManifestError, never a
    silent precedence. Pure: no I/O, no field validation beyond the shape (the
    per-harness readers validate fields).
    """
    found: dict[str, tuple[Mapping[str, Any], ...]] = {}
    legacy = installer.get("codex_standing_instructions")
    if legacy is not None:
        if not isinstance(legacy, Mapping) or legacy.get("schema") != LEGACY_CODEX_STANDING_SCHEMA:
            raise ManifestError(f"{label}: unsupported codex_standing_instructions schema")
        personas = legacy.get("personas")
        item = personas.get(persona) if isinstance(personas, Mapping) else None
        if item is not None:
            if not isinstance(item, Mapping):
                raise ManifestError(f"{label}: codex_standing_instructions persona must be a mapping")
            found["codex"] = (item,)
    generic = installer.get("harness_standing_instructions")
    if generic is not None:
        if not isinstance(generic, Mapping) or generic.get("schema") != HARNESS_STANDING_SCHEMA:
            raise ManifestError(f"{label}: unsupported harness_standing_instructions schema")
        personas = generic.get("personas")
        per_harness = personas.get(persona) if isinstance(personas, Mapping) else None
        if per_harness is not None:
            if not isinstance(per_harness, Mapping):
                raise ManifestError(f"{label}: harness_standing_instructions persona must be a harness mapping")
            for harness_raw, value in per_harness.items():
                harness = _normalize_harness(str(harness_raw))
                if harness not in STANDING_INSTRUCTION_HARNESSES:
                    # r1 LOW: nothing installs a claude entry; refuse, never ignore.
                    raise ManifestError(
                        f"{label}: harness_standing_instructions.{persona}.{harness}: only "
                        f"{', '.join(STANDING_INSTRUCTION_HARNESSES)} standing instructions are installed"
                    )
                items = tuple(value) if isinstance(value, list) else (value,)
                if not items or not all(isinstance(item, Mapping) for item in items):
                    raise ManifestError(
                        f"{label}: harness_standing_instructions.{persona}.{harness} must be an entry or a list of entries"
                    )
                if harness in found:
                    raise ManifestError(
                        f"{label}: standing instructions for ({persona}, {harness}) are declared twice "
                        "(codex_standing_instructions and harness_standing_instructions)"
                    )
                for item in items:
                    extract = item.get("extract")
                    if extract is not None and (
                        not isinstance(extract, Mapping)
                        or set(extract) != {"heading"}
                        or not isinstance(extract.get("heading"), str)
                        or not extract["heading"].strip()
                    ):
                        raise ManifestError(f"{label}: standing instructions extract must be {{heading: <text>}}")
                found[harness] = items
    return found


def _codex_standing_instructions_reference(
    manifest: Manifest,
    binding: PersonaBinding,
    *,
    root: Path = Path("/"),
) -> CodexStandingInstructionsReference | None:
    """The persona's codex standing-instruction reference, from either manifest
    key (``standing_instruction_entries``). Field checks and messages are the
    pre-T-5011 ones, so a legacy entry resolves byte-identically."""
    items = standing_instruction_entries(manifest.installer, binding.persona, label=str(manifest.path)).get("codex")
    if items is None:
        return None
    if len(items) != 1:
        raise ManifestError(f"{manifest.path}: codex standing instructions take exactly one entry")
    persona_raw = items[0]
    if persona_raw.get("extract") is not None:
        raise ManifestError(f"{manifest.path}: codex standing instructions install the whole source (no extract)")
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


#: Account-home-relative hermes standing targets (hermes_render: seat/.hermes.md,
#: .hermes/SOUL.md); a test pins them to HermesRenderTarget.install_path.
HERMES_PROJECT_CONTEXT_TARGET = "seat/.hermes.md"
HERMES_IDENTITY_TARGET = ".hermes/SOUL.md"


def hermes_render_inputs(
    installer: Mapping[str, Any],
    persona: str,
    *,
    account_home: Path,
    repo_root: Path,
    defaults_path: Path,
    label: str = "manifest",
) -> Any:
    """Build ``hermes_render.HermesRenderInputs`` for ``persona`` from its
    ``harness_standing_instructions`` hermes entries (T-5011 PR-D; the H4
    manifest wiring calls this). Pure: no file is read.

    Exactly one whole-source entry targeting ``seat/.hermes.md``, plus at most
    one ``extract: {heading}`` entry targeting ``.hermes/SOUL.md`` from the SAME
    source and sha (the renderer derives SOUL.md from that one pinned source).
    Returns None when the persona declares no hermes entries.
    """
    from tools.harness_adapter.hermes_render import IDENTITY_HEADING, HermesRenderInputs

    items = standing_instruction_entries(installer, persona, label=label).get("hermes")
    if items is None:
        return None
    whole = [item for item in items if item.get("extract") is None]
    extracts = [item for item in items if item.get("extract") is not None]
    if len(whole) != 1 or len(extracts) > 1:
        raise ManifestError(f"{label}: hermes standing instructions need one whole-source entry and at most one extract")
    main = whole[0]
    for field_name in ("source", "source_sha256", "source_commit", "target"):
        if not isinstance(main.get(field_name), str) or not main.get(field_name):
            raise ManifestError(f"{label}: hermes standing instructions.{field_name} missing")
    if not _SHA256_RE.match(str(main["source_sha256"])):
        raise ManifestError(f"{label}: hermes standing instructions.source_sha256 must be 64 lowercase hex")
    if main["target"] != HERMES_PROJECT_CONTEXT_TARGET:
        raise ManifestError(f"{label}: hermes project context target must be {HERMES_PROJECT_CONTEXT_TARGET}")
    heading = IDENTITY_HEADING
    if extracts:
        soul = extracts[0]
        if soul.get("target") != HERMES_IDENTITY_TARGET:
            raise ManifestError(f"{label}: hermes identity extract target must be {HERMES_IDENTITY_TARGET}")
        if (soul.get("source"), soul.get("source_sha256"), soul.get("source_commit")) != (
            main["source"], main["source_sha256"], main["source_commit"]
        ):
            raise ManifestError(f"{label}: hermes identity extract must come from the project-context source")
        heading = str(soul["extract"]["heading"])
    source = _manifest_relative_path(main["source"], "harness_standing_instructions.source")
    return HermesRenderInputs(
        persona=persona,
        account_home=account_home,
        source_path=repo_root / source,
        source_sha256=str(main["source_sha256"]),
        source_commit=str(main["source_commit"]),
        defaults_path=defaults_path,
        identity_heading=heading,
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
    # Codex entries only (from either manifest key). hermes entries install no
    # files here: prepare_hermes_launch renders them from hermes_render_inputs
    # (T-5011 PR-D); other harnesses declare none.
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
        codex_telegram_archive_dir: Path | None = None,
        maintenance_bypass: bool = False,
        *,
        source_revision: str | None = None,
    ) -> InstallPlan:
        # T-5475: maintenance_bypass adds ONE desired key to the unit-fire op (absent when False, so
        # every default plan and digest is unchanged).
        # T-5091 U1: source_revision is the pure plan seam for the germinate harness-manager
        # plan_install adapter. None (the default) keeps the _git_head call, so every live plan
        # and digest is unchanged; a given value must be a full 40-hex commit id and replaces
        # the only process-spawning call in plan().
        if source_revision is not None and not _FULL_COMMIT_RE.fullmatch(source_revision):
            raise ManifestError(
                f"source_revision must be a full 40-hex commit id: {source_revision!r}"
            )
        # T-5068: operator-supplied private staging dir for the codex-telegram source
        # archive; None keeps the legacy fixed /tmp path (back-compat for unmigrated callers).
        if codex_telegram_archive_dir is not None and not codex_telegram_archive_dir.is_absolute():
            raise ManifestError(
                f"codex-telegram archive dir must be absolute: {codex_telegram_archive_dir}"
            )
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
        audit_raw = config.get("mcp_allowlist_audit")
        audit_config: tuple[str, Path, Path, Path] | None = None
        if audit_raw is not None:
            expected = {"owner_persona", "activation", "authority", "service_template", "timer_template"}
            if not isinstance(audit_raw, Mapping) or set(audit_raw) != expected:
                raise ManifestError("persona_installer.mcp_allowlist_audit has invalid fields")
            audit_owner = audit_raw.get("owner_persona")
            if not isinstance(audit_owner, str) or audit_raw.get("activation") != "operator-managed":
                raise ManifestError("persona_installer.mcp_allowlist_audit owner/activation is invalid")
            manifest.binding(audit_owner)
            audit_authority = _manifest_relative_path(
                audit_raw.get("authority"), "persona_installer.mcp_allowlist_audit.authority"
            )
            audit_service = _manifest_relative_path(
                audit_raw.get("service_template"), "persona_installer.mcp_allowlist_audit.service_template"
            )
            audit_timer = _manifest_relative_path(
                audit_raw.get("timer_template"), "persona_installer.mcp_allowlist_audit.timer_template"
            )
            for source in (audit_authority, audit_service, audit_timer):
                if not (self.repo_root / source).is_file():
                    raise ManifestError(f"MCP allowlist audit source missing: {source}")
            if audit_authority != Path("tools/mcp_allowlist.yaml"):
                raise ManifestError("persona_installer.mcp_allowlist_audit must use the canonical authority")
            audit_config = (audit_owner, audit_authority, audit_service, audit_timer)
        policy_bundle = tuple(
            _manifest_relative_path(item, "persona_installer.policy.bundle[]")
            for item in policy.get("bundle", ())
        )
        if not policy_bundle:
            raise ManifestError("persona_installer.policy.bundle must not be empty")

        base_provenance = _provenance(manifest, binding, selected_harness)
        artifacts = (
            *(Path(item) for item in FRAMEWORK_CODE_ARTIFACTS),
            service_relative,
            timer_relative,
            quota_config.service_template,
            quota_config.timer_template,
            *((audit_config[2], audit_config[3]) if audit_config else ()),
        )
        base_provenance = dict(base_provenance) | {
            "declared_revision": base_provenance["source_revision"],
            "source_revision": (
                _git_head(self.repo_root) if source_revision is None else source_revision
            ),
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
        audit_service_content = (
            unit_banner + (self.repo_root / audit_config[2]).read_text(encoding="utf-8")
            if audit_config and binding.persona == audit_config[0] else None
        )
        audit_timer_content = (
            unit_banner + (self.repo_root / audit_config[3]).read_text(encoding="utf-8")
            if audit_config and binding.persona == audit_config[0] else None
        )
        periodics = _seat_core_periodics(
            self.repo_root,
            manifest,
            binding,
            unit_banner,
            base_provenance["source_revision"],
        )
        controller_content = unit_banner + (
            self.repo_root / _SYSTEMD_TEMPLATE_DIR / CONTROLLER_TEMPLATE_UNIT
        ).read_text(encoding="utf-8")
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

        # T-5454: containment covers the claude runtime, so any claude candidate
        # (primary or fallback) makes the seat applicable; codex-only seats do not.
        claude_seat = "claude" in _runtime_candidate_harnesses_for_install(
            manifest, binding, selected_harness
        )
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
                _harness_check_desired(manifest, selected_harness, home),
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
                (codex_telegram_archive_dir or Path("/tmp"))
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
                            "source_subdir": bridge_binding.source.subdir,
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
                *(
                    [
                        _content_operation(
                            operation_id="mcp-allowlist-audit.service-unit",
                            layer=1,
                            kind="user-unit-install",
                            target=unit_dir / "pantheon-mcp-allowlist-audit.service",
                            content=audit_service_content,
                            owner=binding.account,
                            privilege="privileged-helper",
                            provenance=_provenance(manifest, binding, selected_harness, fs_cap),
                        ),
                        _content_operation(
                            operation_id="mcp-allowlist-audit.timer-unit",
                            layer=1,
                            kind="user-unit-install",
                            target=unit_dir / "pantheon-mcp-allowlist-audit.timer",
                            content=audit_timer_content,
                            owner=binding.account,
                            privilege="privileged-helper",
                            provenance=_provenance(manifest, binding, selected_harness, fs_cap),
                        ),
                    ]
                    if audit_service_content is not None and audit_timer_content is not None
                    else []
                ),
                *(
                    _content_operation(
                        operation_id=operation_id,
                        layer=1,
                        kind="seat-periodic-unit-install",
                        target=unit_dir / filename,
                        content=content,
                        owner=binding.account,
                        privilege="privileged-helper",
                        provenance=_provenance(
                            manifest, binding, selected_harness, fs_cap
                        ),
                    )
                    for operation_id, filename, content in periodics.installs
                ),
                _content_operation(
                    operation_id="service.runtime-controller.unit",
                    layer=1,
                    kind="seat-periodic-unit-install",
                    target=unit_dir / CONTROLLER_TEMPLATE_UNIT,
                    content=controller_content,
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
                # T-5454: shell-box containment, BEFORE the first Claude launch (the
                # consume bootstrap fire). Seat-local residue: in a broker flow residue
                # runs after admitted ops and the planner forbids admitted ops from
                # depending on it, so only the seat-local apply path holds this order.
                *(
                    [
                        op(
                            "harness.claude-shell-box",
                            1,
                            SETTINGS_ENV_KIND,
                            str(home / ".claude" / "settings.json"),
                            {
                                "env_key": SHELL_BOX_ENV_KEY,
                                "env_value": str(home / SHELL_BOX_RELATIVE),
                            },
                            "operator-required",
                            fs_cap,
                        )
                    ]
                    if claude_seat
                    else []
                ),
                # T-5474: first-launch workspace trust, before ANY claude launch (direct-fire
                # boots claude too). Gate predicate = the T-5471 blocker's: selected harness claude.
                *(
                    [
                        op(
                            WORKSPACE_TRUST_OP_ID,
                            1,
                            WORKSPACE_TRUST_KIND,
                            str(home / ".claude.json"),
                            {
                                "project_path": str(home / "pantheon-config"),
                                "key": TRUST_KEY,
                            },
                            "operator-required",
                            fs_cap,
                        )
                    ]
                    if selected_harness == "claude"
                    else []
                ),
                # T-5473: phase-Q residue quarantine. An ACTIVATION op (ACTIVATION_OPERATION_KINDS):
                # stage_disabled never runs it; activate / all-in-one runs it AFTER the operator's
                # teardown and before the bootstrap fire. Runs only where unit-fire is a real
                # bootstrap fire (consume timer not enabled) AND the old runtime is not live
                # (controller / consume service inactive); otherwise a recorded no-op or a refusal.
                op(
                    RESIDUE_QUARANTINE_OP_ID,
                    1,
                    RESIDUE_QUARANTINE_KIND,
                    str(home / ".local" / "state" / "pantheon" / "personas" / binding.persona),
                    {
                        "persona_state_root": str(home / ".local" / "state" / "pantheon" / "personas"),
                        "persona": binding.persona,
                        "timer": consume_timer,
                    },
                    "operator-required",
                    manager_cap,
                ),
                # T-5448: EARLY, before the consume bootstrap fire: a legacy
                # ho-resumer tick must not `claude --resume` a second claude
                # (Telegram poller theft) while the seat is being installed.
                # T-5633: planned only where the binding declares the legacy
                # ho-resumer unit set (legacy_units); a must-run-before-launch kind.
                *[
                    op(
                        f"service.{legacy_set}.decommission",
                        1,
                        "seat-service-decommission",
                        binding.account,
                        {
                            "units": ",".join(LEGACY_UNIT_SETS[legacy_set]),
                            "unit_dir": str(unit_dir),
                            "quarantine_dir": str(
                                state_dir / binding.persona / "quarantine" / legacy_set
                            ),
                        },
                        "operator-required",
                        manager_cap,
                    )
                    for legacy_set in binding.legacy_units
                ],
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
                    )
                    # T-5476: the pane the fire launches; rollback stops the session this receipt
                    # launched there, matched on identity, before it reverts the containment key
                    | {"tmux_target": _controller_tmux_target(manifest, binding)}
                    # T-5473/T-5474: ops that clear the bootstrap blockers before this fire; the
                    # dry run reads them to predict clear (execute re-checks for real).
                    | {"residue_cleared_by": RESIDUE_QUARANTINE_OP_ID}
                    | ({"trust_set_by": WORKSPACE_TRUST_OP_ID} if selected_harness == "claude" else {})
                    | ({MAINTENANCE_BYPASS_KEY: "true"} if maintenance_bypass else {}),
                    "operator-required",
                    manager_cap,
                ),
                # T-5448: the controller attaches to the pane the fire created.
                # Residue op ordered AFTER an admitted op (allowed); no admitted
                # op may name it. The consume timer-enable stays admitted.
                op(
                    "service.runtime-controller.enable-observe",
                    1,
                    "seat-service-enable-observe",
                    _controller_service_unit(binding.persona),
                    {
                        "after": "connective.unit-fire",
                        "persona": binding.persona,
                        "expect_selected": _runtime_candidate_refs_for_install(
                            manifest, binding, selected_harness
                        )[0],
                        "require_source_provenance": "true",
                        "tmux_target": _controller_tmux_target(manifest, binding),
                        "observe_window_seconds": str(CONTROLLER_OBSERVE_WINDOW_SECONDS),
                        "observe_interval_seconds": str(CONTROLLER_OBSERVE_INTERVAL_SECONDS),
                        "persona_state_root": str(
                            home / ".local" / "state" / "pantheon" / "personas"
                        ),
                        "enablement_link_path": str(
                            _controller_link_path(home, _controller_service_unit(binding.persona))
                        ),
                        "canonical_template_path": str(_controller_template_path(home)),
                    },
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
                op(
                    "periodic.daemon-reload",
                    1,
                    "seat-periodic-daemon-reload",
                    binding.account,
                    {"scope": "seat-core-periodics"},
                    "operator-required",
                    manager_cap,
                ),
            ]
        )
        for periodic_name, timer_unit, service_unit in periodics.timers:
            operations.append(
                op(
                    f"periodic.{periodic_name}.timer-enable",
                    1,
                    "seat-periodic-timer-enable",
                    timer_unit,
                    {
                        "after": (
                            "connective.timer-observe,periodic.daemon-reload,"
                            "service.runtime-controller.enable-observe"
                        ),
                        "enablement_link_path": str(_seat_timer_link_path(home, timer_unit)),
                        "canonical_template_path": str(_seat_timer_template_path(home, timer_unit)),
                        # T-5476: the service this timer triggers; its pre-arm InvocationID is the
                        # baseline the observe judges a failure against (a stale failure is not ours)
                        "service": service_unit,
                    },
                    "operator-required",
                    manager_cap,
                )
            )
            operations.append(
                op(
                    f"periodic.{periodic_name}.timer-observe",
                    1,
                    "seat-periodic-timer-observe",
                    timer_unit,
                    {
                        "service": service_unit,
                        "next_elapse": "finite",
                        # T-5476: a timer whose OnBootSec already elapsed fires on arm (active+running);
                        # tolerated, polled until active+waiting
                        "observe_window_seconds": str(PERIODIC_OBSERVE_WINDOW_SECONDS),
                        "observe_interval_seconds": str(PERIODIC_OBSERVE_INTERVAL_SECONDS),
                    },
                    "operator-required",
                    manager_cap,
                )
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

        built = InstallPlan.create(
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
                SHELL_BOX_PROVENANCE_KEY: (
                    "applicable"
                    if claude_seat
                    else "not-applicable: no claude runtime candidate"
                ),
                "receipt_dir": str(state_dir / binding.persona / "receipts"),
                ALLOWED_TARGET_ROOTS_KEY: _derive_allowed_roots(manifest, binding.account, root),
            },
        )
        leaks = _leakage_scan(built)
        if leaks:
            raise PlanRefused(_leakage_refusal(leaks))
        path_refusal = _target_path_refusal(built)
        if path_refusal:
            raise PlanRefused(path_refusal)
        return built

    def apply(
        self,
        plan: InstallPlan,
        approved_digest: str,
        backend: HostBackend,
        actor: ActorContext,
        *,
        manifest: Manifest | None = None,
        receipt_sink: ReceiptSink | None = None,
    ) -> ApplyReport:
        sink = receipt_sink if receipt_sink is not None else default_receipt_sink(backend)
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all"}),
                manifest=manifest,
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
                **_receipt_audit_fields(plan),
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
                    sink.store_receipt(plan, staged_receipt)
                    staged = True
                mutation, observed = backend.execute(
                    operation,
                    _context_with_roots(EXECUTION_CONTEXT_ALL_IN_ONE, plan),
                )
                mutations.append(mutation)
                evidence.append(observed)
                if not observed.ok:
                    raise OperationFailed(
                        f"{operation.operation_id}: {observed.detail}"
                    )
            receipt = make_receipt("complete", mutations, evidence)
            sink.store_receipt(plan, receipt)
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
                and current_operation.kind in SERVICE_RECOVERY_KINDS
                and current_operation.operation_id not in returned_ids
                and isinstance(exc, OperationFailed)
                and (
                    exc.evidence.get("enable_command_issued") == "true"
                    or bool(exc.evidence.get("fire_session_identity"))
                )
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
                    backend.rollback_mutation(mutation, allowed_roots=_plan_allowed_roots(plan))
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
                    sink.store_receipt(plan, failed_receipt)
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
            **_receipt_audit_fields(plan),
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
        manifest: Manifest | None = None,
        receipt_sink: ReceiptSink | None = None,
    ) -> ApplyReport:
        sink = receipt_sink if receipt_sink is not None else default_receipt_sink(backend)
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all", "staged-cutover"}),
                manifest=manifest,
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
                    _context_with_roots(EXECUTION_CONTEXT_STAGE_PREFIX, plan),
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
            sink.store_receipt(plan, receipt)
            return ApplyReport("applied" if any(item.changed for item in mutations) else "noop", sum(item.changed for item in mutations), receipt)
        except Exception as exc:
            rollback_notes: list[str] = []
            for mutation in reversed([item for item in mutations if item.changed]):
                try:
                    check = backend.can_rollback(mutation)
                    if not check.ok:
                        rollback_notes.append(f"{mutation.operation_id}: rollback refused: {check.detail}")
                        continue
                    backend.rollback_mutation(mutation, allowed_roots=_plan_allowed_roots(plan))
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
        *,
        manifest: Manifest | None = None,
        receipt_sink: ReceiptSink | None = None,
    ) -> ApplyReport:
        sink = receipt_sink if receipt_sink is not None else default_receipt_sink(backend)
        try:
            self._preflight_apply(
                plan,
                approved_digest,
                backend,
                actor,
                scopes=frozenset({"all", "staged-cutover"}),
                manifest=manifest,
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
                    _context_with_roots(EXECUTION_CONTEXT_STAGED_ACTIVATE, plan),
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
            sink.store_receipt(plan, receipt)
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
                and current_operation.kind in SERVICE_RECOVERY_KINDS
                and current_operation.operation_id not in executed_ids
                and isinstance(exc, OperationFailed)
                and (
                    exc.evidence.get("enable_command_issued") == "true"
                    or bool(exc.evidence.get("fire_session_identity"))
                )
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
                    backend.rollback_mutation(mutation, allowed_roots=_plan_allowed_roots(plan))
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
                sink.store_receipt(plan, failed_receipt)
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
        manifest: "Manifest | None" = None,
    ) -> None:
        if plan.schema != PLAN_SCHEMA:
            raise PlanRefused(f"unsupported plan schema {plan.schema!r}")
        if not plan.validate_digest():
            raise PlanRefused("plan payload does not match its embedded digest")
        if approved_digest != plan.digest:
            raise PlanRefused("approved digest does not match the plan")
        leaks = _leakage_scan(plan)
        if leaks:
            raise PlanRefused(_leakage_refusal(leaks))
        path_refusal = _target_path_refusal(plan) or _pinned_roots_refusal(plan, manifest)
        if path_refusal:
            raise PlanRefused(path_refusal)
        _clean_home_dry_run(plan, scopes)
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

    def _rollback_pin_refusal(
        self,
        receipt: ApplyReceipt,
        plan: "InstallPlan | None",
        manifest: "Manifest | None",
        break_glass: bool,
    ) -> str | None:
        """T-4848: rollback is pinned like apply. The receipt must belong to the
        plan; the plan's roots must be structurally sound; and they must equal the
        roots re-derived from the trusted manifest. ``break_glass`` (incident
        recovery when the manifest is itself the broken thing) skips ONLY the
        manifest re-derivation comparison; every manifest-free check still runs."""
        if plan is None:
            return "rollback pin: no plan supplied; cannot bound rollback paths"
        if not plan.validate_digest():
            return "rollback pin: plan payload does not match its embedded digest"
        if receipt.plan_digest != plan.digest:
            return "rollback pin: receipt plan digest does not match the plan"
        for name in ("instance", "persona", "role", "account"):
            if getattr(receipt, name) != getattr(plan, name):
                return f"rollback pin: receipt {name} does not match the plan"
        path_refusal = _target_path_refusal(plan)
        if path_refusal:
            return path_refusal
        if manifest is not None or not break_glass:
            return _pinned_roots_refusal(plan, manifest)
        return None

    def rollback(
        self,
        receipt: ApplyReceipt,
        backend: HostBackend,
        actor: ActorContext,
        *,
        plan: "InstallPlan | None" = None,
        manifest: "Manifest | None" = None,
        break_glass: bool = False,
    ) -> RollbackReport:
        if receipt.schema != RECEIPT_SCHEMA:
            return RollbackReport("refused", 0, ("unsupported receipt schema",))
        if actor.instance != receipt.instance:
            return RollbackReport("refused", 0, ("actor instance mismatch",))
        pin_refusal = self._rollback_pin_refusal(receipt, plan, manifest, break_glass)
        if pin_refusal:
            return RollbackReport("refused", 0, (pin_refusal,))
        assert plan is not None
        allowed_roots = _plan_allowed_roots(plan)
        unpinned = manifest is None
        if unpinned:
            print(
                "BREAK-GLASS: rollback without manifest pin "
                "(manifest unavailable; manifest-free containment checks still enforced)",
                file=sys.stderr,
            )
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
                backend.rollback_mutation(mutation, allowed_roots=allowed_roots)
                restored += 1
        except Exception as exc:
            return RollbackReport("failed", restored, (str(exc),), unpinned)
        return RollbackReport("rolled-back" if restored else "noop", restored, (), unpinned)


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
        repo=codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_REPO,
        revision=operation.desired["revision"],
        paths=paths,
        sha256=hashes,
        # T-4790 (T-4750 C4b-3): every source rebuilt from an operation carries the
        # pinned tree prefix, not only the binding (the preflight source-check too).
        subdir=_bridge_source_subdir_from_operation(operation),
    )
    if source.revision != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_REVISION:
        raise OperationFailed(f"{operation.operation_id}: source revision is not approved")
    if source.subdir != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_SUBDIR:
        raise OperationFailed(f"{operation.operation_id}: source subdir is not approved")
    if source.paths != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_PATHS:
        raise OperationFailed(f"{operation.operation_id}: source paths are not approved")
    if dict(source.sha256) != codex_telegram_bridge.CODEX_TELEGRAM_SOURCE_SHA256:
        raise OperationFailed(f"{operation.operation_id}: source hashes are not approved")
    return source


def _bridge_source_subdir_from_operation(operation: InstallOperation) -> str:
    try:
        return codex_telegram_bridge.parse_source_subdir(operation.desired.get("source_subdir", ""))
    except codex_telegram_bridge.BridgeError as exc:
        raise OperationFailed(f"{operation.operation_id}: {exc}") from exc


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
    """Return the systemd clock basis with a finite scheduled next elapse.

    Legacy-shaped wrapper (T-2917 5c.1): converts the text values to typed
    Elapse values and applies tools.user_systemd.finite_next_elapse_basis."""

    def elapse(key: str) -> _us.Elapse | None:
        return _us.elapse_from_text(values[key]) if key in values else None

    return _us.finite_next_elapse_basis(
        elapse("NextElapseUSecRealtime"),
        elapse("NextElapseUSecMonotonic"),
    )


def _exec_status_text_value(value: str | None) -> _us.ExecStatus | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _us.exec_status_from_text(value)
    return _us.NonCanonical(str(value))


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
    A missing or non-numeric ``ExecMainStatus`` is not in policy (returns False).

    T-2917 5c.1: legacy-shaped wrapper over the typed
    tools.user_systemd.exit_status_within_policy."""
    return _us.exit_status_within_policy(
        _exec_status_text_value(exec_main_status),
        _us.exit_policy_from_text(success_exit_status or ""),
    )


_LEGACY_CONVERGENCE_SERVICE_IN_FLIGHT_STATES = _us.SERVICE_IN_FLIGHT_STATES


def _legacy_convergence_service_in_flight(values: Mapping[str, str]) -> bool:
    """True if a consume SERVICE sample looks mid-fire rather than settled.

    T-3211: the oneshot's own transition sequence is inactive -> activating ->
    active -> deactivating -> inactive; any of the three non-terminal
    ActiveState values, or a non-empty Job (systemd queues the pending
    transition there before ActiveState itself updates), means the sample
    landed inside that window rather than after it -- re-sample rather than
    reject outright.

    T-2917 5c.1: legacy-shaped wrapper over tools.user_systemd.service_in_flight.
    """
    job = values.get("Job")
    return _us.service_in_flight(
        values.get("ActiveState", ""),
        _us.job_from_text(job) if job is not None else None,
    )


def _valid_systemd_invocation_id(value: str) -> bool:
    return _us.valid_invocation_id(value)


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


def _unit_fire_poll_view(snapshot: UnitSnapshot) -> UnitFirePollSnapshot:
    """The string view of a typed UNIT_FIRE_POLL snapshot that the unit-fire
    diagnostics and evidence carry. Lossless: every text value either became
    a typed value that renders back to the same text or stayed NonCanonical."""

    return UnitFirePollSnapshot(
        active_state=snapshot.active_state or "",
        substate=snapshot.sub_state or "",
        result=snapshot.result or "",
        exec_main_status=_us.render_exec_status(snapshot.exec_main_status),
        success_exit_status=_us.render_success_exit(snapshot.success_exit),
        invocation_id=snapshot.invocation_id or "",
        invocation_id_valid=snapshot.invocation_id_valid,
        job=_us.render_job(snapshot.job_id),
        stdout_length=snapshot.source.length,
        stdout_sha256=snapshot.source.sha256,
    )


def _unit_fire_poll_typed(snapshot: UnitFirePollSnapshot) -> UnitSnapshot:
    """Typed form of a unit-fire string view, for the typed classifiers."""

    return _us.unit_snapshot_from_text_values(
        "",
        ReadPurpose.UNIT_FIRE_POLL,
        {
            "InvocationID": snapshot.invocation_id,
            "ActiveState": snapshot.active_state,
            "SubState": snapshot.substate,
            "Result": snapshot.result,
            "ExecMainStatus": snapshot.exec_main_status,
            "SuccessExitStatus": snapshot.success_exit_status,
            "Job": snapshot.job,
        },
        source=_us.SnapshotSource(
            transport=SYSTEMCTL_TRANSPORT,
            encoding=SYSTEMCTL_SHOW_STDOUT,
            length=snapshot.stdout_length,
            sha256=snapshot.stdout_sha256,
        ),
    )


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
    # T-2917 5c.1: legacy-shaped wrapper over tools.user_systemd.unit_fire_has_failed.
    return _us.unit_fire_has_failed(_unit_fire_poll_typed(snapshot))


def _strip_unit_banner(content: str) -> str:
    lines = content.splitlines(keepends=True)
    if lines and lines[0].startswith(f"# {MANAGED_MARKER}"):
        lines = lines[1:]
        if lines and lines[0].startswith("# instance="):
            lines = lines[1:]
    return "".join(lines)


def _bounded_diff_digest(existing: str, wanted: str) -> str:
    """T-5447: a short, bounded description of how a hand-installed unit differs
    from the template -- sha prefixes plus at most 8 changed lines of <=120 chars."""
    changed = [
        line
        for line in difflib.unified_diff(
            existing.splitlines(), wanted.splitlines(), "existing", "template", n=0, lineterm=""
        )
        if line[:1] in "+-" and not line.startswith(("---", "+++"))
    ]
    shown = "|".join(line[:120] for line in changed[:8])
    more = f" (+{len(changed) - 8} more)" if len(changed) > 8 else ""
    return (
        f"existing={_sha256_bytes(existing.encode('utf-8'))[:12]} "
        f"template={_sha256_bytes(wanted.encode('utf-8'))[:12]} diff[{shown}]{more}"
    )


def _unmanaged_file_preflight(existing: str, operation: "InstallOperation") -> "PreflightResult":
    """An existing regular file that is neither the desired bytes nor installer-managed.

    T-5447: a hand-installed UNIT whose bytes equal the template (ignoring the
    absent managed banner) is adopted -- the write adds the banner and the receipt
    snapshot (bytes + mode) is its inverse. Anything else is refused with a bounded
    diff digest. Only ``user-unit-install`` adopts; every other kind keeps the
    plain refusal."""
    if operation.kind not in {"user-unit-install", "seat-periodic-unit-install"}:
        return PreflightResult(False, "conflicting", "unknown regular file")
    wanted = _strip_unit_banner(_desired_content(operation).decode("utf-8"))
    if existing == wanted:
        return PreflightResult(
            True,
            "adoptable",
            "byte-identical unmanaged unit will be adopted (banner added; original bytes kept for rollback)",
        )
    return PreflightResult(
        False,
        "conflicting",
        "unknown regular file; diff-digest: " + _bounded_diff_digest(existing, wanted),
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
        # Receipt persistence is not a backend responsibility (T-2917 5a);
        # the simulated in-memory sink is a separate object that records
        # into this backend's event log so ordering proofs still observe
        # receipt writes interleaved with operation events.
        self.receipt_sink: ReceiptSink = SimulatedReceiptSink(self)
        self.timer_enabled = False
        # T-5476: the session a simulated bootstrap fire launches (off by default)
        self.fire_launches_session = False
        self.fire_session_alive = False
        self.fire_session_stop_confirms = True
        self.fire_session_foreign = False  # a live session whose identity no longer matches the receipt
        self.fail_after_fire_launch = False
        # T-5447: seat-core periodic timers enabled in this simulated manager
        self.enabled_timers: set[str] = set()
        # T-5448: simulated controller services + injectable observe faults
        self.enabled_services: set[str] = set()
        self.controller_fault: str | None = None  # "inactive" | "pid-change" | "restart"
        self.controller_viability_ok = True
        self.converged = False
        # T-5471: the simulated consume service's live unit state. None == the fresh-install
        # shape (not-found, clean). Classified by the SAME function Local uses.
        self.consume_service: StaleUnitFailureResetSnapshot | None = None
        self.consume_success_exit_status = ""
        # T-5471: a seeded bootstrap blocker (maintenance / trust / residue reason) -- the
        # simulated twin of LocalHostBackend._unit_fire_bootstrap_blocker.
        self.bootstrap_blocker: str | None = None
        # T-5475: simulated seat maintenance / deep-sleep, the one-shot bypass token (its JSON text,
        # or None), whether the simulated consume reads it, and a lapse between gate and write.
        self.in_maintenance = False
        self.deep_sleeping = False
        self.bypass_token: str | None = None
        self.consume_reads_token = True
        self.maintenance_lapses_before_write = False
        # T-5473/T-5474: opt-in entries-derived residue/trust model for the blocker twin, and
        # the simulated execute-time quarantine stamp.
        self.model_bootstrap_state = False
        # reviewer B1: simulated unit ActiveStates for the quarantine's liveness gate
        self.unit_active_states: dict[str, str] = {}
        self.unreadable_units: set[str] = set()
        self.quarantine_stamp = "20000101T000000Z"

    def seed_consume_service(
        self,
        *,
        load_state: str,
        active_state: str,
        sub_state: str,
        result: str,
        exec_main_status: str,
        invocation_id: str = "",
        job: str = "",
        success_exit_status: str = "",
    ) -> None:
        """Test seam: the consume service's state before the install ran."""
        self.consume_service = StaleUnitFailureResetSnapshot(
            load_state=load_state,
            active_state=active_state,
            sub_state=sub_state,
            result=result,
            exec_main_status=exec_main_status,
            invocation_id=invocation_id,
            job=job,
            stdout_length=0,
            stdout_sha256="",
        )
        self.consume_success_exit_status = success_exit_status

    def _sim_classify(self) -> str:
        return _classify_stale_reset_for_install(
            self._consume_view(),
            lambda: _us.exit_policy_from_text(self.consume_success_exit_status),
        )

    def _consume_view(self) -> StaleUnitFailureResetSnapshot:
        if self.consume_service is not None:
            return self.consume_service
        return StaleUnitFailureResetSnapshot(
            load_state="not-found", active_state="inactive", sub_state="dead", result="success",
            exec_main_status="0", invocation_id="", job="", stdout_length=0, stdout_sha256="",
        )

    def seed_enabled_timer(self, timer: str, link: str, canonical: str) -> None:
        """Test seam: a periodic timer already enabled before the install ran."""
        self.enabled_timers.add(timer)
        self.entries[link] = {
            "state": "symlink",
            "target": canonical,
            "fingerprint": _sha256_bytes(canonical.encode("utf-8")),
        }

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

    def preflight(
        self, operation: InstallOperation, context: ExecutionContext | None = None
    ) -> PreflightResult:
        if operation.kind in self.unavailable_kinds:
            return PreflightResult(False, "unavailable", "backend kind unavailable")
        if operation.kind == "seat-periodic-timer-enable":
            return self._periodic_preflight(operation)
        if operation.kind == "seat-periodic-timer-observe":
            return PreflightResult(True, "transient-tolerant", periodic_observe_prediction(operation))
        if operation.kind == "seat-service-enable-observe":
            return self._service_preflight(operation)
        if operation.kind == "seat-service-decommission":
            return self._decommission_preflight(operation)
        if operation.kind == SETTINGS_ENV_KIND:
            return self._settings_env_preflight(operation)
        if operation.kind == WORKSPACE_TRUST_KIND:
            return self._workspace_trust_preflight(operation)
        if operation.kind == RESIDUE_QUARANTINE_KIND:
            return self._residue_quarantine_preflight(operation)
        if operation.kind == "stale-unit-failure-reset":
            try:
                classification = self._sim_classify()
            except OperationFailed as exc:
                return PreflightResult(False, "refused", str(exc))
            return PreflightResult(True, classification, f"consume service stale state: {classification}")
        if operation.kind == "unit-fire":
            try:
                _parse_unit_fire_timeout_budget(operation)
                _parse_unit_fire_initial_viability_desired(operation)
            except OperationFailed as exc:
                return PreflightResult(False, "unit-fire", str(exc))
            if not self.timer_enabled:
                blocker = self._sim_bootstrap_blocker(operation, preflight=True)
                if blocker:
                    return PreflightResult(False, "conflicting", blocker)
                idle, detail = _unit_fire_idle_prediction(self._sim_classify)
                if not idle:
                    return PreflightResult(False, "conflicting", detail)
                if _maintenance_bypass_requested(operation) and self.in_maintenance:
                    return PreflightResult(True, "maintenance-bypass", MAINTENANCE_BYPASS_PREFLIGHT_NOTE)
        if operation.kind == "mkdir":
            state = self._snapshot(operation.target)
            if state["state"] in {"absent", "directory"}:
                return PreflightResult(True, state["state"], "safe directory state")
            return PreflightResult(False, "conflicting", "non-directory exists")
        if operation.kind in {"atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
            return _unmanaged_file_preflight(content, operation)
        if operation.kind == "timer-enablement-absence-check":
            state = self._snapshot(operation.target)
            if state["state"] == "absent":
                return PreflightResult(True, "absent", "consume timer enablement link is absent")
            return PreflightResult(False, "conflicting", "consume timer enablement link already exists")
        if operation.kind == "timer-enable":
            refusal = _staged_activate_enabled_timer_refusal(
                context.workflow if context is not None else "", self.timer_enabled, False
            )
            if refusal:
                return PreflightResult(False, "conflicting", refusal)
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
        if _is_periodic_operation(operation):
            return self._execute_periodic(operation)
        if operation.kind == "seat-service-enable-observe":
            return self._execute_service_enable(operation)
        if operation.kind == "seat-service-decommission":
            return self._execute_decommission(operation)
        if operation.kind == SETTINGS_ENV_KIND:
            return self._execute_settings_env(operation)
        if operation.kind == WORKSPACE_TRUST_KIND:
            return self._execute_workspace_trust(operation)
        if operation.kind == RESIDUE_QUARANTINE_KIND:
            return self._execute_residue_quarantine(operation)
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
        elif operation.kind in {"atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
            view = self._consume_view()
            classification = self._sim_classify()  # raises like Local
            before = {"state": classification}
            observed = {
                "classification": {"clean-noop": "noop", STALE_ADOPTED: "adopted"}.get(classification, "reset"),
                "pre_state": classification,
                "post_state": "clean-noop",
                "pre_load_state": view.load_state,
                "pre_active_state": view.active_state,
                "pre_sub_state": view.sub_state,
                "pre_result": view.result,
                "pre_exec_main_status": view.exec_main_status,
            }
            detail = "consume service stale failure state is absent"
            if classification == "reset-required":
                self.consume_service = None  # reset-failed clears the stale record
            if classification == STALE_ADOPTED:
                observed["post_state"] = STALE_ADOPTED
        if operation.kind == "timer-enable":
            link = operation.desired.get("enablement_link_path", "")
            canonical = operation.desired.get("canonical_template_path", "")
            before = {
                "state": "enabled" if self.timer_enabled else "disabled",
            }
            changed = not self.timer_enabled
            refusal = _staged_activate_enabled_timer_refusal(context.workflow, self.timer_enabled, False)
            if refusal:
                raise OperationFailed(refusal)
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
            if not self.timer_enabled:
                # T-5471: the same gates the local execute runs before the enqueue
                blocker = self._sim_bootstrap_blocker(operation, preflight=False)
                if blocker:
                    raise OperationFailed(blocker)
                idle, detail = _unit_fire_idle_prediction(self._sim_classify)
                if not idle:
                    raise OperationFailed(detail)
            # T-5475: the simulated twin of the local one-shot bypass token (a token is its JSON text)
            bypass_state = "none"
            bypass_persona = operation.provenance.get("persona", "")
            if not self.timer_enabled and _maintenance_bypass_requested(operation):
                if self.in_maintenance and not self.maintenance_lapses_before_write:
                    if self.bypass_token is not None:
                        raise OperationFailed(
                            "a maintenance bypass token already exists; refusing to overwrite it"
                        )
                    self.bypass_token = _canonical_json(
                        _bypass_token_payload(bypass_persona, f"persona_installer {operation.operation_id}")
                    )
                    self.events.append(f"bypass-token-written:{operation.operation_id}")
                    # consume unlinks what it reads; a token it did not read is removed here, as the
                    # local finally block does
                    reads = self.consume_reads_token
                    self.bypass_token = None
                    if not reads:
                        raise OperationFailed(
                            "consume did not read the maintenance bypass token",
                            evidence={MAINTENANCE_BYPASS_EVIDENCE: "survived-removed"},
                        )
                    bypass_state = "consumed"
                else:
                    bypass_state = "not-needed-maintenance-lapsed"
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
            if self.fire_launches_session and not self.timer_enabled:
                self.fire_session_alive = True
                fire_identity = {
                    "launched": "true",
                    "tmux_target": operation.desired.get("tmux_target", "sim:main.0"),
                    "pane_pid": "4242",
                    "pane_start_ticks": "1",
                    "runtime_started_at": "2026-01-01T00:00:00Z",
                    "invocation_id": "0" * 32,
                }
                if self.fail_after_fire_launch:
                    confirmed = self.fire_session_stop_confirms
                    self.events.append(f"fire-session-stop:{operation.operation_id}")
                    if confirmed:
                        self.fire_session_alive = False
                    raise OperationFailed(
                        "injected failure after fire launch",
                        evidence={
                            "fire_session_match": "matched",
                            "fire_session_pid": "4242",
                            "fire_session_stopped": str(confirmed).lower(),
                            **({} if confirmed else {"fire_session_identity": _canonical_json(fire_identity)}),
                        },
                    )
                changed = True
                before = fire_identity
                observed["fire_session_pid"] = "4242"
            if bypass_state != "none":
                observed[MAINTENANCE_BYPASS_EVIDENCE] = bypass_state
            if bypass_state == "consumed":
                changed = True
                before = {
                    **(before if before.get("launched") == "true" else {}),
                    MAINTENANCE_BYPASS_EVIDENCE: "written",
                    "persona": bypass_persona,
                    "persona_state_root": str(viability.persona_state_root),
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
        if operation.kind == "unit-fire" and changed:
            after_fingerprint = "fire-launched"
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

    # ---- T-5447: seat-core periodic timers (timer-enable + observe only) ----

    def _periodic_preflight(self, operation: InstallOperation) -> PreflightResult:
        link = operation.desired.get("enablement_link_path", "")
        canonical = operation.desired.get("canonical_template_path", "")
        state = self._snapshot(link) if link else {"state": "absent"}
        if operation.target in self.enabled_timers:
            if state["state"] not in {"absent", "symlink"} or (
                state["state"] == "symlink" and state.get("target") != canonical
            ):
                return PreflightResult(False, "conflicting", "enabled timer link is not canonical")
            return PreflightResult(True, "enabled", "timer already enabled; tolerated, not receipt-owned")
        if state["state"] != "absent":
            return PreflightResult(False, "conflicting", "timer enablement link already exists")
        return PreflightResult(True, "absent", "timer will be enabled")

    def _execute_periodic(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        before: dict[str, str] = {"state": "not-applicable"}
        changed = False
        ok = True
        detail = "periodic timer operation passed"
        observed: dict[str, str] = {}
        if operation.kind == "seat-periodic-timer-enable":
            required = {
                item for item in operation.desired.get("after", "").split(",") if item
            }
            gate = required.issubset(self.events)
            preflight = self._periodic_preflight(operation)
            if not preflight.ok:
                raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
            link = operation.desired["enablement_link_path"]
            canonical = operation.desired["canonical_template_path"]
            if operation.target in self.enabled_timers:
                before = {"state": "enabled", "owned": "false"}
            else:
                before = {
                    "state": "disabled",
                    "owned": "true",
                    "enablement_link_path": link,
                    "canonical_template_path": canonical,
                }
                self.entries[link] = {
                    "state": "symlink",
                    "target": canonical,
                    "fingerprint": _sha256_bytes(canonical.encode("utf-8")),
                }
                self.enabled_timers.add(operation.target)
                changed = True
            ok = gate
            detail = "periodic timer enabled" if ok else "periodic timer ordering gate failed"
            observed = {"timer_enabled": "true", "enablement_link_path": link}
        else:
            ok = operation.target in self.enabled_timers and self.finite_next_elapse
            detail = (
                "periodic timer armed with a finite next elapse"
                if ok
                else "periodic timer observation incomplete"
            )
            observed = {"next_elapse_finite": str(self.finite_next_elapse).lower()}
            if ok:
                observed.update(
                    {
                        "observe_window_seconds": operation.desired.get(
                            "observe_window_seconds", str(PERIODIC_OBSERVE_WINDOW_SECONDS)
                        ),
                        "observe_interval_seconds": operation.desired.get(
                            "observe_interval_seconds", str(PERIODIC_OBSERVE_INTERVAL_SECONDS)
                        ),
                        "transient_tolerant": "true",
                    }
                )
        self.events.append(operation.operation_id)
        return (
            MutationRecord(
                operation.operation_id,
                operation.kind,
                operation.target,
                operation.owner,
                changed,
                before,
                "timer-enabled" if operation.kind == "seat-periodic-timer-enable" else None,
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, ok, detail, observed
            ),
        )

    # ---- T-5448: runtime-controller bring-up + ho-resumer decommission ----

    def _service_preflight(self, operation: InstallOperation) -> PreflightResult:
        link = operation.desired.get("enablement_link_path", "")
        canonical = operation.desired.get("canonical_template_path", "")
        state = self._snapshot(link) if link else {"state": "absent"}
        if operation.target in self.enabled_services:
            if state["state"] not in {"absent", "symlink"} or (
                state["state"] == "symlink" and state.get("target") != canonical
            ):
                return PreflightResult(False, "conflicting", "enabled controller link is not canonical")
            return PreflightResult(True, "enabled", "controller already enabled; tolerated, not receipt-owned")
        if state["state"] != "absent":
            return PreflightResult(False, "conflicting", "controller enablement link already exists")
        return PreflightResult(True, "absent", "controller will be enabled")

    def _sim_controller_samples(self) -> list[dict[str, str]]:
        base = {"ActiveState": "active", "SubState": "running", "MainPID": "4242", "NRestarts": "0"}
        samples = [dict(base) for _ in range(CONTROLLER_OBSERVE_WINDOW_SECONDS // CONTROLLER_OBSERVE_INTERVAL_SECONDS + 1)]
        if self.controller_fault == "inactive":
            samples[-1].update(ActiveState="activating", SubState="auto-restart")
        elif self.controller_fault == "pid-change":
            samples[-1]["MainPID"] = "4343"
        elif self.controller_fault == "restart":
            samples[-1]["NRestarts"] = "1"
        return samples

    def _execute_service_enable(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        gate = "connective.unit-fire" in self.events
        if not gate:
            raise OperationFailed(f"{operation.operation_id}: ordered before the consume bootstrap fire")
        preflight = self._service_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        link = operation.desired["enablement_link_path"]
        canonical = operation.desired["canonical_template_path"]
        issued: dict[str, str] = {}
        if operation.target in self.enabled_services:
            before = {"state": "enabled", "owned": "false"}
            changed = False
        else:
            before = {
                "state": "disabled",
                "owned": "true",
                "enablement_link_path": link,
                "canonical_template_path": canonical,
            }
            self.entries[link] = {
                "state": "symlink",
                "target": canonical,
                "fingerprint": _sha256_bytes(canonical.encode("utf-8")),
            }
            self.enabled_services.add(operation.target)
            issued = {"enable_command_issued": "true", "enable_command_unit": operation.target}
            changed = True
        try:
            _judge_controller_samples(self._sim_controller_samples(), adopted=not changed)
            if not self.controller_viability_ok:
                raise OperationFailed(
                    f"initial-viability is not selected={operation.desired['expect_selected']}"
                )
        except OperationFailed as exc:
            raise OperationFailed(str(exc), evidence={**issued, **dict(exc.evidence)}) from exc
        self.events.append(operation.operation_id)
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, before, "service-enabled",
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, True,
                "controller enabled and observed stable",
                {"controller_active": "true", "controller_nrestarts": "0", "controller_pre_start_pane_pid": PANE_PID_ABSENT,
                 "initial_viability_selected": operation.desired["expect_selected"]},
            ),
        )

    def _sim_decommission_paths(self, operation: InstallOperation) -> list[tuple[str, str, str]]:
        units = _legacy_units_for_operation(operation)
        unit_dir = operation.desired["unit_dir"].rstrip("/")
        quarantine = operation.desired["quarantine_dir"].rstrip("/")
        return [(name, f"{unit_dir}/{name}", f"{quarantine}/{name}") for name in units]

    def _decommission_preflight(self, operation: InstallOperation) -> PreflightResult:
        try:
            paths = self._sim_decommission_paths(operation)
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        for name, src, quarantined in paths:
            if self._snapshot(src)["state"] != "absent" and self._snapshot(quarantined)["state"] != "absent":
                return PreflightResult(False, "conflicting", f"{name}: quarantine destination already exists")
        return PreflightResult(True, "ready", "legacy-unit decommission is safe to apply")

    def _execute_decommission(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        preflight = self._decommission_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        present = [
            (n, s, q) for n, s, q in self._sim_decommission_paths(operation)
            if self._snapshot(s)["state"] != "absent"
        ]
        unit_dir = operation.desired["unit_dir"].rstrip("/")
        if not present:
            before: dict[str, str] = {"state": "absent"}
            changed = False
        else:
            timer_units = [name for name, _, _ in present if name.endswith(".timer")]
            was_enabled = any(name in self.enabled_timers for name in timer_units)
            for timer_unit in timer_units:
                self.enabled_timers.discard(timer_unit)
                self.entries.pop(f"{unit_dir}/timers.target.wants/{timer_unit}", None)
            for name, src, quarantined in present:
                self.entries[quarantined] = self.entries.pop(src)  # move, never delete
            before = {
                "state": "present",
                "moved": ",".join(n for n, _, _ in present),
                "timer_was_enabled": str(was_enabled).lower(),
                "timer_was_active": str(was_enabled).lower(),
                "unit_dir": operation.desired["unit_dir"],
                "quarantine_dir": operation.desired["quarantine_dir"],
            }
            changed = True
        self.events.append(operation.operation_id)
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, before, "decommissioned",
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, True,
                "legacy units decommissioned" if changed else "legacy units absent",
                {"legacy_units": "quarantined" if changed else "absent"},
            ),
        )

    # ---- T-5473: phase-Q residue quarantine (simulated twin; same shared helpers) ----
    def _bootstrap_fire_pending(self) -> bool:
        """Twin of LocalHostBackend._bootstrap_fire_pending: the disabled-timer branch."""
        return not self.timer_enabled

    def _sim_lexists(self, path: str) -> bool:
        return self._snapshot(path)["state"] != "absent"

    def _sim_residue_entries(self, root: str, persona: str) -> list[tuple[str, str]]:
        import fnmatch

        found: list[tuple[str, str]] = []
        for cls, patterns in (
            ("runtime-controller", RESIDUE_RUNTIME_CONTROLLER),
            ("persona-root", RESIDUE_PERSONA_ROOT),
        ):
            base = _quarantine_class_base(root, persona, cls)
            for pattern in patterns:
                if "*" in pattern:
                    found.extend(
                        (cls, key[len(base) + 1:])
                        for key in sorted(self.entries)
                        if key.startswith(base + "/")
                        and "/" not in key[len(base) + 1:]
                        and fnmatch.fnmatch(key[len(base) + 1:], pattern)
                        and self._sim_lexists(key)
                    )
                elif self._sim_lexists(f"{base}/{pattern}"):
                    found.append((cls, pattern))
        for suffix in RESIDUE_STATE_ROOT_SUFFIXES:
            if self._sim_lexists(f"{root}/{persona}{suffix}"):
                found.append(("state-root", f"{persona}{suffix}"))
        return found

    def _sim_move_tree(self, src: str, dst: str) -> None:
        for key in [k for k in self.entries if k == src or k.startswith(src + "/")]:
            self.entries[dst + key[len(src):]] = self.entries.pop(key)

    def _residue_quarantine_preflight(self, operation: InstallOperation) -> PreflightResult:
        try:
            root, persona, _ = _quarantine_desired(operation)
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        if not self._bootstrap_fire_pending():
            return PreflightResult(
                True, "converged",
                "consume timer already enabled: not a bootstrap fire, runtime state left in place",
            )
        live = _runtime_liveness_refusal(
            {
                unit: (None if unit in self.unreadable_units else self.unit_active_states.get(unit, "inactive"))
                for unit in (_controller_service_unit(persona), f"pantheon-persona-consume@{persona}.service")
            }
        )
        if live:
            return PreflightResult(False, "conflicting", live)
        entries = self._sim_residue_entries(root, persona)
        if not entries:
            return PreflightResult(True, "absent", "no control-plane residue to quarantine")
        names = ", ".join(f"{cls}/{name}" for cls, name in entries)
        return PreflightResult(
            True, "ready",
            f"would move {len(entries)} path(s) into {root}/{persona}/{QUARANTINE_DIR_PREFIX}<stamp>: {names}",
        )

    def _execute_residue_quarantine(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        preflight = self._residue_quarantine_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        root, persona, _ = _quarantine_desired(operation)
        changed, before, observed = False, {"state": "absent"}, {"residue": "absent"}
        if preflight.classification == "converged":
            before, observed = {"state": "skipped", "reason": preflight.detail}, {"residue": "left-in-place"}
        elif preflight.classification == "ready":
            entries = self._sim_residue_entries(root, persona)
            stamp = self.quarantine_stamp
            qdir = _quarantine_dir_for(root, persona, stamp)
            for cls, name in entries:
                self._sim_move_tree(f"{_quarantine_class_base(root, persona, cls)}/{name}", f"{qdir}/{cls}/{name}")
            listing = json.dumps([[cls, name] for cls, name in entries], separators=(",", ":"))
            changed = True
            before = {"state": "present", "persona_state_root": root, "persona": persona,
                      "stamp": stamp, "moved": listing}
            observed = {"residue": "quarantined", "quarantine_dir": qdir, "moved": listing}
        self.events.append(operation.operation_id)
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, before, "quarantined",
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, True, preflight.detail, observed
            ),
        )

    def _rollback_residue_quarantine(self, mutation: MutationRecord) -> None:
        check = _quarantine_can_rollback(mutation.before, self._sim_lexists, mutation.target)
        if not check.ok:
            raise OperationFailed(check.detail)
        if check.classification == "owned":
            for quarantined, original in _quarantine_pairs(mutation.before, mutation.target):
                self._sim_move_tree(quarantined, original)
        self.events.append(f"rollback:{mutation.operation_id}")

    # ---- T-5474: first-launch workspace trust (simulated twin) ----
    def _workspace_trust_project(self, operation: InstallOperation) -> str:
        project = operation.desired.get("project_path", "")
        if not os.path.isabs(project) or operation.desired.get("key") != TRUST_KEY:
            raise OperationFailed("workspace trust: desired project_path/key are malformed")
        return project

    def _workspace_trust_preflight(self, operation: InstallOperation) -> PreflightResult:
        try:
            project = self._workspace_trust_project(operation)
            prior = _trust_prior(_trust_parse(self._settings_env_raw(operation.target)), project)
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        if prior == "true":
            return PreflightResult(True, "matching", f"projects[{project}].{TRUST_KEY} already true")
        return PreflightResult(True, "absent" if prior != "false" else "false", f"will set projects[{project}].{TRUST_KEY} = true ({prior})")

    def _execute_workspace_trust(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        preflight = self._workspace_trust_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        project = self._workspace_trust_project(operation)
        raw = self._settings_env_raw(operation.target)
        prior, new = _trust_with(raw, project)
        if new is not None:
            self._settings_env_store(operation.target, new)
        self.events.append(operation.operation_id)
        changed = new is not None
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed,
                {"state": "absent" if raw is None else "file", "owned": "true" if changed else "false",
                 "project_path": project, "prior": prior},
                _sha256_bytes(b"true"),
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, True, preflight.detail,
                {"trust_state": "set" if changed else "matching", "prior": prior},
            ),
        )

    def _rollback_workspace_trust(self, mutation: MutationRecord) -> None:
        raw = self._settings_env_raw(mutation.target)
        check = _trust_can_rollback(mutation.before, raw)
        if not check.ok:
            raise OperationFailed(check.detail)
        if check.classification == "owned":
            action, new = _trust_without(raw, mutation.before)
            if action == "write" and new is not None:
                self._settings_env_store(mutation.target, new)
            elif action == "delete":
                self.entries.pop(mutation.target, None)
        self.events.append(f"rollback:{mutation.operation_id}")

    def _sim_bootstrap_blocker(self, operation: InstallOperation, *, preflight: bool) -> str | None:
        """Twin of LocalHostBackend._unit_fire_bootstrap_blocker's trust/residue half. The seeded
        string always wins; the entries-derived model is opt-in (model_bootstrap_state)."""
        if self.bootstrap_blocker:
            return self.bootstrap_blocker
        gate = _maintenance_gate(
            operation.provenance.get("persona", ""),
            in_maintenance=self.in_maintenance,
            deep_sleeping=lambda: self.deep_sleeping,
            bypass=_maintenance_bypass_requested(operation),
        )
        if gate:
            return gate
        if not self.model_bootstrap_state:
            return None
        viability = _parse_unit_fire_initial_viability_desired(operation)
        persona = operation.provenance.get("persona", "")
        if operation.provenance.get("selected_harness") == "claude" and not (
            preflight and operation.desired.get("trust_set_by")
        ):
            clone = viability.account_home / "pantheon-config"
            try:
                trusted = _trust_prior(
                    _trust_parse(self._settings_env_raw(str(viability.account_home / ".claude.json"))),
                    str(clone),
                ) == "true"
            except OperationFailed:
                trusted = False
            if not trusted:
                return f"workspace trust missing: projects[{clone}].{TRUST_KEY} is not true"
        if not (preflight and operation.desired.get("residue_cleared_by")):
            residue = self._sim_residue_entries(str(viability.persona_state_root), persona)
            if residue:
                return "control-plane residue would block the bootstrap: " + ", ".join(n for _, n in residue)
        return None

    # ---- T-5454: shell-box wiring in the seat's settings.json ----
    def _settings_env_raw(self, target: str) -> bytes | None:
        entry = self._snapshot(target)
        if entry["state"] == "absent":
            return None
        if entry["state"] != "file":
            raise OperationFailed(f"{target} is not a regular file; refusing to touch it")
        return base64.b64decode(entry["content_b64"])

    def _settings_env_store(self, target: str, raw: bytes) -> None:
        old = dict(self.entries.get(target, {}))
        old.update(
            {
                "state": "file",
                "content_b64": base64.b64encode(raw).decode("ascii"),
                "fingerprint": _sha256_bytes(raw),
            }
        )
        old.setdefault("mode", "0600")
        self.entries[target] = old

    def _settings_env_preflight(self, operation: InstallOperation) -> PreflightResult:
        try:
            doc = _settings_env_parse(self._settings_env_raw(operation.target))
            cls, detail = _settings_env_verdict(
                doc, operation.desired["env_key"], operation.desired["env_value"]
            )
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        return PreflightResult(cls != "conflicting", cls, detail)

    def _execute_settings_env(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, OperationEvidence]:
        preflight = self._settings_env_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        raw = self._settings_env_raw(operation.target)
        key, value = operation.desired["env_key"], operation.desired["env_value"]
        _, new = _settings_env_with_key(raw, key, value)
        if new is not None:
            self._settings_env_store(operation.target, new)
        self.events.append(operation.operation_id)
        changed = new is not None
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, _settings_env_before(operation, raw, changed),
                _sha256_bytes(value.encode("utf-8")),
            ),
            OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, True,
                preflight.detail, {"env_key": key, "env_state": "set" if changed else "matching"},
            ),
        )

    def _service_can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.kind == "seat-service-decommission":
            if mutation.before.get("state") != "present":
                return PreflightResult(True, "not-applied", "nothing was decommissioned")
            unit_dir = mutation.before["unit_dir"].rstrip("/")
            quarantine = mutation.before["quarantine_dir"].rstrip("/")
            names = [name for name in mutation.before.get("moved", "").split(",") if name]
            try:
                _legacy_units_for_receipt(names)
            except OperationFailed as exc:
                return PreflightResult(False, "malformed", str(exc))
            for name in names:
                if self._snapshot(f"{unit_dir}/{name}")["state"] != "absent":
                    return PreflightResult(False, "drifted", f"{name}: unit path re-created after receipt")
                if self._snapshot(f"{quarantine}/{name}")["state"] == "absent":
                    return PreflightResult(False, "drifted", f"{name}: quarantined copy is gone")
            return PreflightResult(True, "owned", "quarantined units match the receipt")
        if mutation.before.get("owned") != "true":
            return PreflightResult(
                True, "not-applied", "pre-existing enablement is not receipt-owned; left alone"
            )
        link = mutation.before.get("enablement_link_path", "")
        state = self._snapshot(link)
        if state["state"] == "absent" and mutation.target not in self.enabled_services:
            return PreflightResult(True, "not-applied", "controller already disabled")
        if state["state"] != "symlink" or state.get("target") != mutation.before.get("canonical_template_path"):
            return PreflightResult(False, "drifted", "controller enablement link changed after receipt")
        return PreflightResult(True, "owned", "controller enablement link matches receipt")

    def _service_rollback(self, mutation: MutationRecord) -> None:
        if mutation.kind == "seat-service-decommission":
            if mutation.before.get("state") == "present":
                unit_dir = mutation.before["unit_dir"].rstrip("/")
                quarantine = mutation.before["quarantine_dir"].rstrip("/")
                for name in mutation.before["moved"].split(","):
                    self.entries[f"{unit_dir}/{name}"] = self.entries.pop(f"{quarantine}/{name}")
                if mutation.before.get("timer_was_enabled") == "true":
                    timer_unit = next(
                        name for name in mutation.before["moved"].split(",") if name.endswith(".timer")
                    )
                    self.enabled_timers.add(timer_unit)
                    canonical = f"{unit_dir}/{timer_unit}"
                    self.entries[f"{unit_dir}/timers.target.wants/{timer_unit}"] = {
                        "state": "symlink",
                        "target": canonical,
                        "fingerprint": _sha256_bytes(canonical.encode("utf-8")),
                    }
        elif mutation.before.get("owned") == "true":
            self.entries.pop(mutation.before.get("enablement_link_path", ""), None)
            self.enabled_services.discard(mutation.target)
        self.events.append(f"rollback:{mutation.operation_id}")

    def _periodic_can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.before.get("owned") != "true":
            return PreflightResult(
                True, "not-applied", "pre-existing enablement is not receipt-owned; left alone"
            )
        link = mutation.before.get("enablement_link_path", "")
        state = self._snapshot(link)
        if state["state"] == "absent" and mutation.target not in self.enabled_timers:
            return PreflightResult(True, "not-applied", "timer already disabled")
        if state["state"] != "symlink" or state.get("target") != mutation.before.get(
            "canonical_template_path"
        ):
            return PreflightResult(False, "drifted", "timer enablement link changed after receipt")
        return PreflightResult(True, "owned", "timer enablement link matches receipt")

    def verify_operation(
        self,
        operation: InstallOperation,
        *,
        staged_evidence: OperationEvidence | None = None,
    ) -> OperationEvidence:
        ok = operation.operation_id in self.events
        if operation.kind == "seat-service-enable-observe":
            ok = operation.target in self.enabled_services
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, ok,
                "controller state verified" if ok else "controller not enabled",
            )
        if operation.kind == SETTINGS_ENV_KIND:
            try:
                doc = _settings_env_parse(self._settings_env_raw(operation.target))
                cls, detail = _settings_env_verdict(
                    doc, operation.desired["env_key"], operation.desired["env_value"]
                )
            except OperationFailed as exc:
                cls, detail = "conflicting", str(exc)
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, cls == "matching", detail
            )
        if operation.kind == WORKSPACE_TRUST_KIND:
            try:
                prior = _trust_prior(
                    _trust_parse(self._settings_env_raw(operation.target)),
                    self._workspace_trust_project(operation),
                )
                ok, detail = prior == "true", f"workspace trust state: {prior}"
            except OperationFailed as exc:
                ok, detail = False, str(exc)
            return OperationEvidence(operation.operation_id, operation.layer, operation.kind, ok, detail)
        if operation.kind == "seat-service-decommission":
            paths = self._sim_decommission_paths(operation)
            ok = all(self._snapshot(src)["state"] == "absent" for _, src, _ in paths)
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, ok,
                "ho-resumer units absent from the unit dir" if ok else "ho-resumer units still present",
            )
        if _is_periodic_operation(operation):
            if operation.kind == "seat-periodic-timer-enable":
                ok = operation.target in self.enabled_timers
            return OperationEvidence(
                operation.operation_id,
                operation.layer,
                operation.kind,
                ok,
                "periodic timer state verified" if ok else "periodic timer state absent",
            )
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
        if mutation.kind == "unit-fire":
            if not self.fire_session_alive:
                return PreflightResult(True, "not-applied", "fire-launched session is already gone")
            if self.fire_session_foreign:
                return PreflightResult(False, "drifted", "a live session at the fire's target does not match the receipt")
            return PreflightResult(True, "owned", "fire-launched session matches the receipt")
        if mutation.kind == SETTINGS_ENV_KIND:
            try:
                raw = self._settings_env_raw(mutation.target)
            except OperationFailed as exc:
                return PreflightResult(False, "drifted", str(exc))
            return _settings_env_can_rollback(mutation, raw)
        if mutation.kind == WORKSPACE_TRUST_KIND:
            try:
                raw = self._settings_env_raw(mutation.target)
            except OperationFailed as exc:
                return PreflightResult(False, "drifted", str(exc))
            return _trust_can_rollback(mutation.before, raw)
        if mutation.kind == RESIDUE_QUARANTINE_KIND:
            return _quarantine_can_rollback(mutation.before, self._sim_lexists, mutation.target)
        if _is_service_enable_mutation(mutation) or mutation.kind == "seat-service-decommission":
            return self._service_can_rollback(mutation)
        if _is_periodic_mutation(mutation):
            return self._periodic_can_rollback(mutation)
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

    def rollback_mutation(
        self,
        mutation: MutationRecord,
        *,
        allowed_roots: Sequence[str] = (),
    ) -> None:
        if mutation.kind == "unit-fire":
            if _bypass_written(mutation.before):
                self.bypass_token = None  # T-5475: only because the receipt records that we wrote it
            if not self.fire_session_alive:
                return
            if self.fire_session_foreign:
                raise OperationFailed("fire-launched session does not match the receipt; left untouched")
            self.events.append(f"rollback-stop-session:{mutation.operation_id}")
            if not self.fire_session_stop_confirms:
                raise OperationFailed("fire-launched session still alive 10s after SIGTERM; containment key kept")
            self.fire_session_alive = False
            return
        if mutation.kind == SETTINGS_ENV_KIND:
            if self.fire_session_alive:
                raise OperationFailed("containment key kept: fire-launched session is still running")
            raw = self._settings_env_raw(mutation.target)
            check = _settings_env_can_rollback(mutation, raw)
            if not check.ok:
                raise OperationFailed(check.detail)
            if check.classification == "owned":
                new = _settings_env_without_key(
                    raw, mutation.before["env_key"], mutation.before["env_value"]
                )
                if new is not None:
                    self._settings_env_store(mutation.target, new)
            self.events.append(f"rollback:{mutation.operation_id}")
            return
        if mutation.kind == WORKSPACE_TRUST_KIND:
            self._rollback_workspace_trust(mutation)
            return
        if mutation.kind == RESIDUE_QUARANTINE_KIND:
            self._rollback_residue_quarantine(mutation)
            return
        if _is_service_enable_mutation(mutation) or mutation.kind == "seat-service-decommission":
            self._service_rollback(mutation)
            return
        if _is_periodic_mutation(mutation):
            # only a link/enablement THIS receipt created is undone
            if mutation.before.get("owned") == "true":
                self.entries.pop(mutation.before.get("enablement_link_path", ""), None)
                self.enabled_timers.discard(mutation.target)
            self.events.append(f"rollback:{mutation.operation_id}")
            return
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
        if operation.kind == "unit-fire":
            try:
                identity = json.loads(evidence.get("fire_session_identity", ""))
            except ValueError:
                return None
            if not isinstance(identity, dict):
                return None
            return MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                True, {str(k): str(v) for k, v in identity.items()}, "fire-launched",
            )
        if operation.kind == "seat-service-enable-observe":
            link = operation.desired.get("enablement_link_path", "")
            canonical = operation.desired.get("canonical_template_path", "")
            state = self._snapshot(link) if link else {"state": "absent"}
            if (
                evidence.get("enable_command_issued") != "true"
                or operation.target not in self.enabled_services
                or state.get("state") != "symlink"
                or state.get("target") != canonical
            ):
                return None
            return MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner, True,
                {
                    "state": "disabled",
                    "owned": "true",
                    "enablement_link_path": link,
                    "canonical_template_path": canonical,
                },
                "service-enabled",
            )
        if (
            operation.kind != "timer-enable"
            or evidence.get("enable_command_issued") != "true"
        ):
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


class SimulatedReceiptSink:
    """In-memory receipt sink bound to one ``SimulatedHostBackend``."""

    def __init__(self, backend: SimulatedHostBackend) -> None:
        self._backend = backend

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        self._backend.receipts[receipt.receipt_id] = receipt.to_dict()
        self._backend.receipt_events.append(f"receipt:{receipt.phase}")
        self._backend.events.append(f"receipt:{receipt.phase}")


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
        # T-5476
        self._periodic_service_baselines: dict[str, str] = {}
        self._fire_launched_sessions: list[dict[str, str]] = []

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

    @property
    def user_manager(self) -> SystemctlUserManager:
        """The seat's user manager behind the typed port (T-2917 5c.1).

        Created lazily and proxying DYNAMICALLY to ``self._run`` on every call,
        so a subclass overriding ``_run`` (every test fake) still intercepts
        every user-manager command. ``timeout`` is forwarded only when given,
        exactly as each pre-5c.1 call site did."""

        manager = self.__dict__.get("_user_manager")
        if manager is None:

            def run(argv: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
                if timeout is None:
                    return self._run(argv)
                return self._run(argv, timeout=timeout)

            manager = SystemctlUserManager(run)
            self.__dict__["_user_manager"] = manager
        return manager

    def _unit_fire_port_call(
        self,
        call: Any,
        *,
        phase: str,
        classification: str,
        start_count: int,
        cleanup_state: str,
    ) -> Any:
        try:
            return call()
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
        try:
            snapshot = self._unit_fire_port_call(
                lambda: self.user_manager.read_unit(unit, ReadPurpose.UNIT_FIRE_POLL, timeout=timeout),
                phase=phase,
                classification=classification,
                start_count=start_count,
                cleanup_state=cleanup_state,
            )
        except _us.UserManagerCallFailed as exc:
            result = exc.transport_evidence
            stdout = result.stdout or ""
            raise OperationFailed((result.stderr or stdout).strip() or "unit-fire state unavailable") from None
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"unit-fire show output {exc.reason}") from None
        return _unit_fire_poll_view(snapshot)

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
        self._baseline_invocation = ""
        if not _us.unit_fire_idle_before_enqueue(_unit_fire_poll_typed(snapshot)):
            if _unit_fire_converged_baseline(snapshot):
                # T-5471: an already-converged consume service carries its PRIOR
                # InvocationID. Record it; the capture ignores exactly this id.
                self._baseline_invocation = snapshot.invocation_id
                return snapshot
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
        stop = _completed(
            self._unit_fire_port_call(
                lambda: self.user_manager.stop_unit(unit, timeout=timeout),
                phase="cleanup",
                classification="unsafe-cleanup",
                start_count=1,
                cleanup_state="stop-timeout",
            )
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
        try:
            self.user_manager.manager_environment()
        except _us.UserManagerUnavailable as exc:
            result = exc.transport_evidence
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

    # ---- T-5447: seat-core periodic timers (timer-enable + observe only) ----

    def _periodic_paths_from_desired(self, operation: InstallOperation) -> tuple[Path, Path]:
        raw_template = operation.desired.get("canonical_template_path")
        raw_link = operation.desired.get("enablement_link_path")
        if not raw_template or not raw_link:
            raise OperationFailed("timer enablement desired paths missing")
        return self._periodic_checked_paths(operation.target, raw_link, raw_template)

    def _periodic_checked_paths(self, timer: str, raw_link: str, raw_template: str) -> tuple[Path, Path]:
        canonical = _lexically_normalize_absolute(Path(raw_template))
        link = _lexically_normalize_absolute(Path(raw_link))
        try:
            account_home = canonical.parents[3]
        except IndexError as exc:
            raise OperationFailed("timer enablement canonical template path is malformed") from exc
        if canonical != _seat_timer_template_path(account_home, timer):
            raise OperationFailed("timer enablement canonical template path does not match account-derived path")
        if link != _seat_timer_link_path(account_home, timer):
            raise OperationFailed("timer enablement link path does not match account-derived path")
        return link, canonical

    def _require_periodic_link_matches_receipt(self, mutation: MutationRecord) -> TimerEnablementLinkProof:
        raw_link = mutation.before.get("enablement_link_path")
        raw_template = mutation.before.get("canonical_template_path")
        if not raw_link or not raw_template:
            raise OperationFailed("timer enablement receipt lacks link ownership evidence")
        link, canonical = self._periodic_checked_paths(mutation.target, raw_link, raw_template)
        proof = _timer_enablement_link_proof(link, canonical)
        if proof.snapshot_state == "absent":
            return proof
        if not _timer_enablement_link_matches_canonical(proof):
            raise OperationFailed("timer enablement link does not resolve to the receipt canonical template")
        recorded = mutation.before.get("enablement_link_actual_readlink", "")
        if recorded and proof.actual_readlink != recorded:
            raise OperationFailed("timer enablement link readlink bytes changed after receipt")
        return proof

    def _execute_periodic_enable(self, operation: InstallOperation) -> tuple[MutationRecord, dict[str, str]]:
        link, canonical = self._periodic_paths_from_desired(operation)
        self._capture_periodic_service_baseline(operation)
        timer_state = self._timer_unit_state(operation.target)
        if timer_state.classification == "not-found":
            raise OperationFailed("timer unit is not loaded")
        proof = _timer_enablement_link_proof(link, canonical)
        if timer_state.classification == "enabled":
            # tolerated, NOT receipt-owned: rollback leaves it alone
            if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                raise OperationFailed("enabled timer link is not canonical")
            before = {"state": "enabled", "owned": "false"}
            changed = False
        else:
            if proof.snapshot_state != "absent":
                raise OperationFailed(f"{operation.operation_id}: timer enablement link already exists")
            result = _completed(self.user_manager.enable_now(operation.target))
            issued = {
                "enable_command_issued": "true",
                "enable_command_unit": operation.target,
                **_completed_process_bounded_evidence("enable", result),
            }
            if result.returncode != 0:
                raise OperationFailed(
                    (result.stderr or result.stdout).strip() or "timer enable command failed",
                    evidence=issued,
                )
            if operation.provenance.get("install_root") != "/" and not link.is_symlink():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(str(canonical))
            proof = _timer_enablement_link_proof(link, canonical)
            if not _timer_enablement_link_matches_canonical(proof):
                raise OperationFailed(
                    "timer enablement link did not resolve to the canonical template after enable",
                    evidence=issued,
                )
            before = {
                "state": "disabled",
                "owned": "true",
                "enablement_link_path": str(link),
                "canonical_template_path": str(canonical),
                "enablement_link_actual_readlink": proof.actual_readlink,
                "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
            }
            changed = True
        mutation = MutationRecord(
            operation.operation_id, operation.kind, operation.target, operation.owner,
            changed, before, "timer-enabled",
        )
        return mutation, {"timer_enabled": "true", **_timer_enablement_link_evidence("post", proof)}

    def _periodic_service_view(self, service: str) -> tuple[str, str, str]:
        try:
            snap = self._user_unit_read(service, ReadPurpose.SERVICE_OBSERVE)
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"periodic service show output {exc.reason}") from None
        return snap.active_state or "", snap.result or "", snap.invocation_id or ""

    def _capture_periodic_service_baseline(self, operation: InstallOperation) -> None:
        """T-5476: record the triggered service's InvocationID BEFORE the timer is armed, so the
        observe can tell a failure of the run the arm triggered from a stale failure. Nothing
        clears periodic services' failed state before observe (the stale-unit-failure-reset op
        targets the consume service only)."""
        service = operation.desired.get("service", "")
        if not service:
            return
        try:
            self.__dict__.setdefault("_periodic_service_baselines", {})[service] = self._periodic_service_view(service)[2]
        except OperationFailed:
            pass  # unreadable pre-arm: no baseline, the observe stays strict

    def _execute_periodic_observe(self, operation: InstallOperation) -> dict[str, str]:
        window = _desired_int_or(operation, "observe_window_seconds", PERIODIC_OBSERVE_WINDOW_SECONDS)
        interval = _desired_int_or(operation, "observe_interval_seconds", PERIODIC_OBSERVE_INTERVAL_SECONDS)
        service = operation.desired.get("service", "")
        baseline = self.__dict__.get("_periodic_service_baselines", {}).get(service)
        samples = 0
        transient = False
        stale_ignored = False
        last_timer = last_service = ""
        for index in range(window // interval + 1):
            if index:
                self._observe_sleep(interval)
            samples += 1
            timer_state = self._user_unit_read(operation.target, ReadPurpose.TIMER_ELAPSE)
            last_timer = f"{timer_state.active_state}/{timer_state.sub_state}"
            if service:
                active, result, invocation = self._periodic_service_view(service)
                last_service = f"{active}/{result or '-'}"
                failed = active == "failed" or (active == "inactive" and result not in {"", "success"})
                if failed:
                    if baseline is not None and invocation == baseline:
                        stale_ignored = True  # a failure from before this arm, not the run it triggered
                    else:
                        raise OperationFailed(
                            "periodic timer's triggered service failed",
                            evidence={"observe_samples": str(samples), "last_timer_state": last_timer,
                                      "last_service_state": last_service},
                        )
            if timer_state.active_state == "active" and timer_state.sub_state == "waiting":
                basis = _us.snapshot_finite_next_elapse_basis(timer_state)
                if basis is not None:
                    return {
                        "next_elapse_basis": basis,
                        "next_elapse_finite": "true",
                        "observe_samples": str(samples),
                        "observe_transient_seen": str(transient).lower(),
                        "observe_stale_service_failure_ignored": str(stale_ignored).lower(),
                    }
                continue  # waiting but not yet finite: a re-arm in progress, poll
            if timer_state.active_state in {"active", "activating"} and (
                timer_state.active_state == "activating"
                or timer_state.sub_state in PERIODIC_TIMER_TRANSIENT_SUBSTATES
            ):
                transient = True
                continue
            raise OperationFailed(
                f"periodic timer is {last_timer} after arm",
                evidence={"observe_samples": str(samples), "last_timer_state": last_timer,
                          "last_service_state": last_service},
            )
        raise OperationFailed(
            f"periodic timer did not reach active+waiting with a finite next elapse within {window}s "
            f"(last {last_timer})",
            evidence={"observe_samples": str(samples), "last_timer_state": last_timer,
                      "last_service_state": last_service},
        )

    def _rollback_periodic_enable(self, mutation: MutationRecord) -> None:
        if mutation.before.get("owned") != "true":
            return  # a tolerated pre-existing enablement is not ours to undo
        self._stop_unit_or_converged_not_loaded(mutation.target)
        proof = self._require_periodic_link_matches_receipt(mutation)
        if proof.snapshot_state == "symlink":
            proof.link_path.unlink()
        result = _completed(self.user_manager.disable_now(mutation.target))
        if result.returncode != 0:
            text = result.stderr or result.stdout
            if "not exist" not in text and "not loaded" not in text and "No such file" not in text:
                raise OperationFailed(text.strip() or "timer disable failed")
        reload_result = _completed(self.user_manager.reload())
        if reload_result.returncode != 0:
            raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
        link, canonical = self._periodic_checked_paths(
            mutation.target,
            mutation.before["enablement_link_path"],
            mutation.before["canonical_template_path"],
        )
        if _timer_enablement_link_proof(link, canonical).snapshot_state != "absent":
            raise OperationFailed("timer enablement link still exists after rollback")

    # ---- T-5448: runtime-controller bring-up + ho-resumer decommission ----

    def _observe_sleep(self, seconds: float) -> None:
        """Seam: tests replace this so the bounded window costs no wall clock."""
        time.sleep(seconds)

    def _service_checked_paths(self, unit: str, raw_link: str, raw_template: str) -> tuple[Path, Path]:
        if not raw_link or not raw_template:
            raise OperationFailed("controller enablement desired paths missing")
        canonical = _lexically_normalize_absolute(Path(raw_template))
        link = _lexically_normalize_absolute(Path(raw_link))
        try:
            account_home = canonical.parents[3]
        except IndexError as exc:
            raise OperationFailed("controller canonical template path is malformed") from exc
        if canonical != _controller_template_path(account_home):
            raise OperationFailed("controller canonical template path does not match account-derived path")
        if link != _controller_link_path(account_home, unit):
            raise OperationFailed("controller link path does not match account-derived path")
        return link, canonical

    def _service_paths_from_desired(self, operation: InstallOperation) -> tuple[Path, Path]:
        persona = operation.desired.get("persona", "")
        if operation.target != _controller_service_unit(persona):
            raise OperationFailed("controller unit does not match its persona")
        return self._service_checked_paths(
            operation.target,
            operation.desired.get("enablement_link_path", ""),
            operation.desired.get("canonical_template_path", ""),
        )

    def _controller_show(self, unit: str) -> dict[str, str]:
        """One bounded, read-only typed read (SERVICE_BRINGUP_OBSERVE) of the four
        properties the observe judges, rendered back to the canonical strings
        `_judge_controller_samples` compares. Fail-closed: a nonzero exit, a
        missing, duplicate or unexpected property, or a non-canonical MainPID or
        NRestarts all raise OperationFailed."""
        try:
            snap = self._user_unit_read(unit, ReadPurpose.SERVICE_BRINGUP_OBSERVE)
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"controller show output is not the expected property set: {exc}") from None
        return _controller_values_from_snapshot(snap)

    def _controller_viability(self, operation: InstallOperation) -> dict[str, str]:
        """The in-process equivalent of `runtime_controller initial-viability
        --expect-selected <ref> --require-source-provenance` (read-only; the same
        function the CLI wraps)."""
        from tools import runtime_controller

        persona = operation.desired["persona"]
        expected = operation.desired["expect_selected"]
        try:
            verdict = runtime_controller.validate_initial_viability_state(
                persona,
                state_root=Path(operation.desired["persona_state_root"]),
                expect_selected=expected,
                require_source_provenance=operation.desired.get("require_source_provenance") == "true",
            )
        except Exception as exc:  # fail closed on any reader error
            raise OperationFailed(f"initial-viability could not be evaluated: {exc}") from None
        if not isinstance(verdict, Mapping) or verdict.get("ok") is not True:
            errors = verdict.get("errors") if isinstance(verdict, Mapping) else None
            raise OperationFailed(f"initial-viability is not selected={expected}: {errors}")
        return {"initial_viability_selected": expected, "initial_viability_ok": "true"}

    def _pre_start_pane_pid(self, operation: InstallOperation) -> str:
        """T-5449: read-only tmux query of the seat pane's pid BEFORE the start, so
        verify-seat can later prove the pane was not respawned across the start."""
        target = operation.desired.get("tmux_target", "")
        if not target:
            return PANE_PID_ABSENT
        try:
            result = self._run(pane_pid_argv(target))
        except (OSError, subprocess.TimeoutExpired):
            return PANE_PID_ABSENT
        return parse_pane_pid(result.returncode, result.stdout or "", target)

    # ---- T-5476: the session a bootstrap fire launches is owned by the receipt that launched it ----
    #
    # What the pane pid IS: tmux starts the pane command directly (no shell: several argv
    # words) as `/usr/bin/env -- K=V... <claude> ...` (runtime_controller._runtime_command_argv,
    # `new-session`/`respawn-pane` in _start_runtime). env execs its command, so the pane pid is
    # the claude process itself, not a shell or wrapper; SIGTERM reaches the runtime process.

    def _fire_wall_clock(self) -> datetime:
        """UTC wall clock. runtime.json's started_at is also UTC wall time
        (runtime_controller._utc_now), so the two compare on one basis."""
        return datetime.now(timezone.utc)

    def _fire_pane_pid(self, target: str) -> str:
        if not target:
            return PANE_PID_ABSENT
        try:
            result = self._run(pane_pid_argv(target))
        except (OSError, subprocess.TimeoutExpired):
            return PANE_PID_ABSENT
        return parse_pane_pid(result.returncode, result.stdout or "", target)

    @staticmethod
    def _fire_runtime_json_path(operation: InstallOperation) -> Path:
        return (
            Path(operation.desired["persona_state_root"])
            / operation.provenance.get("persona", "")
            / "runtime-controller"
            / "runtime.json"
        )

    @staticmethod
    def _read_runtime_json(path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    def _proc_stat(self, pid: int) -> tuple[str, str] | None:
        """(state, starttime ticks) from /proc/<pid>/stat, or None when the process is gone."""
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            rest = raw.rsplit(")", 1)[1].split()
            return rest[0], rest[19]
        except (OSError, IndexError):
            return None

    def _session_alive(self, pid: int, start_ticks: str) -> bool:
        """Alive means the SAME process (pid + start time) still runs; a zombie or a reused pid is gone."""
        stat_ = self._proc_stat(pid)
        if stat_ is None or stat_[0] in {"Z", "X"}:
            return False
        return not start_ticks or stat_[1] == start_ticks

    def _signal_pid(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def _fire_launch_identity(
        self,
        operation: InstallOperation,
        *,
        pre_pane_pid: str,
        fire_wall_start: datetime | None,
        invocation_id: str,
    ) -> dict[str, str] | None:
        """The identity of the session THIS fire launched, or None when it cannot be proven ours:
        a pane existed before the fire, there is no pane now, or runtime.json does not record a
        start at this target at or after the fire began (wall clock both sides)."""
        target = operation.desired.get("tmux_target", "")
        if not target or pre_pane_pid != PANE_PID_ABSENT or fire_wall_start is None:
            return None
        pane = self._fire_pane_pid(target)
        if not pane.isdigit():
            return None
        path = self._fire_runtime_json_path(operation)
        record = self._read_runtime_json(path)
        started_text = str(record.get("started_at", ""))
        try:
            started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if started.tzinfo is None or record.get("tmux_target") != target or started < fire_wall_start:
            return None
        stat_ = self._proc_stat(int(pane))
        return {
            "launched": "true",
            "persona": operation.provenance.get("persona", ""),
            "invocation_id": invocation_id,
            "tmux_target": target,
            "pane_pid": pane,
            "pane_start_ticks": stat_[1] if stat_ else "",
            "runtime_started_at": started_text,
            "runtime_scope_unit": str(record.get("runtime_scope_unit", "")),
            "runtime_json_path": str(path),
        }

    def _stop_matched_session(self, identity: Mapping[str, str]) -> bool:
        """SIGTERM the matched runtime process, then confirm it gone within the bound. No SIGKILL."""
        pid = int(identity["pane_pid"])
        ticks = identity.get("pane_start_ticks", "")
        if pid <= 1:
            return False
        try:
            self._signal_pid(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        for index in range(FIRE_SESSION_STOP_CONFIRM_SECONDS // FIRE_SESSION_STOP_POLL_SECONDS + 1):
            if index:
                self._observe_sleep(FIRE_SESSION_STOP_POLL_SECONDS)
            if not self._session_alive(pid, ticks):
                return True
        return False

    def _stop_fire_launched_session(
        self,
        operation: InstallOperation,
        *,
        pre_pane_pid: str,
        fire_wall_start: datetime | None,
        invocation_id: str,
    ) -> dict[str, str]:
        """A fire that fails after its launch stops what it launched (matched, SIGTERM, confirmed)."""
        target = operation.desired.get("tmux_target", "")
        if not target or pre_pane_pid != PANE_PID_ABSENT:
            return {}
        pane = self._fire_pane_pid(target)
        if not pane.isdigit():
            return {}
        identity = self._fire_launch_identity(
            operation, pre_pane_pid=pre_pane_pid, fire_wall_start=fire_wall_start,
            invocation_id=invocation_id,
        )
        if identity is None:
            stat_ = self._proc_stat(int(pane))
            self.__dict__.setdefault("_fire_launched_sessions", []).append(
                {"pane_pid": pane, "pane_start_ticks": stat_[1] if stat_ else "", "tmux_target": target}
            )
            return {
                "fire_session_match": "unmatched",
                "fire_session_stopped": "false",
                "fire_session_pid": pane,
            }
        self.__dict__.setdefault("_fire_launched_sessions", []).append(identity)
        confirmed = self._stop_matched_session(identity)
        evidence = {
            "fire_session_match": "matched",
            "fire_session_pid": identity["pane_pid"],
            "fire_session_stopped": str(confirmed).lower(),
        }
        if not confirmed:
            evidence["fire_session_identity"] = _canonical_json(identity)
        return evidence

    def _fire_session_state(self, before: Mapping[str, str]) -> str:
        """gone | match | mismatch for a receipt-recorded fire-launched session. Touching it
        requires id (runtime started_at), pid (+ start time) and target to ALL still match."""
        if "pane_pid" not in before and _bypass_written(before):
            return "gone"  # T-5475: a bypass-only receipt (the fire launched nothing we own)
        try:
            pid = int(before["pane_pid"])
        except (KeyError, ValueError):
            return "mismatch"
        if not self._session_alive(pid, before.get("pane_start_ticks", "")):
            return "gone"
        record = self._read_runtime_json(Path(before.get("runtime_json_path", "")))
        if (
            self._fire_pane_pid(before.get("tmux_target", "")) == before["pane_pid"]
            and record.get("tmux_target") == before.get("tmux_target")
            and record.get("started_at") == before.get("runtime_started_at")
        ):
            return "match"
        return "mismatch"

    def _fire_sessions_alive(self) -> list[dict[str, str]]:
        alive = []
        for item in self.__dict__.get("_fire_launched_sessions", []):
            try:
                if self._session_alive(int(item["pane_pid"]), item.get("pane_start_ticks", "")):
                    alive.append(item)
            except (KeyError, ValueError):
                alive.append(item)
        return alive

    def _fire_launch_can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        state = self._fire_session_state(mutation.before)
        if state == "gone":
            return PreflightResult(True, "not-applied", "fire-launched session is already gone")
        if state == "match":
            return PreflightResult(True, "owned", "fire-launched session matches the receipt (id, pid, target)")
        return PreflightResult(
            False, "drifted",
            "a live session at the fire's target does not match the receipt (id/pid/target); left untouched",
        )

    def _rollback_fire_launch(self, mutation: MutationRecord) -> None:
        # T-5475: the token is one-shot; the only thing to undo is a leftover THIS receipt wrote.
        # The path is derived (containment-checked persona_state_root + persona), never read from
        # the receipt, and a receipt that does not record a write leaves any token alone.
        if _bypass_written(mutation.before):
            persona = mutation.before.get("persona", "")
            if not persona or Path(persona).name != persona or persona in {".", ".."}:
                raise OperationFailed("bypass token cleanup: receipt persona is not a plain name; left untouched")
            _remove_bypass_token(
                Path(mutation.before["persona_state_root"]), mutation.before["persona"]
            )
        state = self._fire_session_state(mutation.before)
        if state == "gone":
            return
        pid = mutation.before.get("pane_pid", "?")
        if state != "match":
            raise OperationFailed(
                f"fire-launched session pid {pid} does not match the receipt (id/pid/target); "
                "left untouched, containment key kept",
                evidence={"fire_session_match": "mismatch", "fire_session_pid": pid},
            )
        if not self._stop_matched_session(mutation.before):
            raise OperationFailed(
                f"fire-launched session pid {pid} still alive "
                f"{FIRE_SESSION_STOP_CONFIRM_SECONDS}s after SIGTERM; containment key kept",
                evidence={"fire_session_match": "matched", "fire_session_pid": pid,
                          "fire_session_stopped": "false"},
            )

    def _live_session_key_block(self) -> PreflightResult | None:
        """T-5476: never revert the containment key while a session this backend launched lives."""
        alive = self._fire_sessions_alive()
        if not alive:
            return None
        pids = ",".join(item.get("pane_pid", "?") for item in alive)
        return PreflightResult(
            False, "live-session",
            f"containment key kept: fire-launched session pid {pids} is still running",
        )

    def _execute_service_enable_observe(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, dict[str, str]]:
        link, canonical = self._service_paths_from_desired(operation)
        window = _controller_desired_int(operation, "observe_window_seconds")
        interval = _controller_desired_int(operation, "observe_interval_seconds")
        reload_result = _completed(self.user_manager.reload())
        if reload_result.returncode != 0:
            raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
        state = self._timer_unit_state(operation.target)
        if state.classification == "not-found":
            raise OperationFailed("controller unit is not loaded")
        proof = _timer_enablement_link_proof(link, canonical)
        issued: dict[str, str] = {}
        pre_start_pane_pid = self._pre_start_pane_pid(operation)
        if state.classification == "enabled":
            # tolerated, NOT receipt-owned: observed like any other, never disabled by us
            if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                raise OperationFailed("enabled controller link is not canonical")
            before = {"state": "enabled", "owned": "false"}
            changed = False
        else:
            if proof.snapshot_state != "absent":
                raise OperationFailed(f"{operation.operation_id}: controller enablement link already exists")
            result = _completed(self.user_manager.enable_now(operation.target))
            issued = {
                "enable_command_issued": "true",
                "enable_command_unit": operation.target,
                **_completed_process_bounded_evidence("enable", result),
            }
            if result.returncode != 0:
                raise OperationFailed(
                    (result.stderr or result.stdout).strip() or "controller enable command failed",
                    evidence=issued,
                )
            if operation.provenance.get("install_root") != "/" and not link.is_symlink():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(str(canonical))
            proof = _timer_enablement_link_proof(link, canonical)
            if not _timer_enablement_link_matches_canonical(proof):
                raise OperationFailed(
                    "controller enablement link did not resolve to the canonical template after enable",
                    evidence=issued,
                )
            before = {
                "state": "disabled",
                "owned": "true",
                "enablement_link_path": str(link),
                "canonical_template_path": str(canonical),
                "enablement_link_actual_readlink": proof.actual_readlink,
                "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
            }
            changed = True
        try:
            samples = []
            for index in range(window // interval + 1):
                if index:
                    self._observe_sleep(interval)
                samples.append(self._controller_show(operation.target))
            _judge_controller_samples(samples, adopted=not changed)
            viability = self._controller_viability(operation)
        except OperationFailed as exc:
            raise OperationFailed(str(exc), evidence={**issued, **dict(exc.evidence)}) from exc
        mutation = MutationRecord(
            operation.operation_id, operation.kind, operation.target, operation.owner,
            changed, before, "service-enabled",
        )
        return mutation, {
            "controller_active": "true",
            "controller_main_pid": samples[0]["MainPID"],
            "controller_nrestarts": samples[-1]["NRestarts"],
            "controller_pre_start_pane_pid": pre_start_pane_pid,
            "controller_samples": str(len(samples)),
            **viability,
            **_timer_enablement_link_evidence("post", proof),
        }

    def _rollback_service_enable(self, mutation: MutationRecord) -> None:
        if mutation.before.get("owned") != "true":
            return  # a tolerated pre-existing enablement is not ours to undo
        link, canonical = self._service_checked_paths(
            mutation.target,
            mutation.before.get("enablement_link_path", ""),
            mutation.before.get("canonical_template_path", ""),
        )
        proof = _timer_enablement_link_proof(link, canonical)
        if proof.snapshot_state not in {"absent", "symlink"} or (
            proof.snapshot_state == "symlink" and not _timer_enablement_link_matches_canonical(proof)
        ):
            raise OperationFailed("controller enablement link no longer matches the receipt")
        result = _completed(self.user_manager.disable_now(mutation.target))
        if result.returncode != 0:
            text = result.stderr or result.stdout
            if "not exist" not in text and "not loaded" not in text and "No such file" not in text:
                raise OperationFailed(text.strip() or "controller disable failed")
        if proof.snapshot_state == "symlink" and link.is_symlink():
            link.unlink()
        reload_result = _completed(self.user_manager.reload())
        if reload_result.returncode != 0:
            raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")

    def _service_can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.before.get("owned") != "true":
            return PreflightResult(
                True, "not-applied", "pre-existing enablement is not receipt-owned; left alone"
            )
        try:
            link, canonical = self._service_checked_paths(
                mutation.target,
                mutation.before.get("enablement_link_path", ""),
                mutation.before.get("canonical_template_path", ""),
            )
            proof = _timer_enablement_link_proof(link, canonical)
            state = self._timer_unit_state(mutation.target)
        except OperationFailed as exc:
            return PreflightResult(False, "drifted", str(exc))
        if proof.snapshot_state == "symlink" and not _timer_enablement_link_matches_canonical(proof):
            return PreflightResult(False, "drifted", "controller enablement link changed after receipt")
        if proof.snapshot_state == "symlink" or state.classification == "enabled":
            return PreflightResult(True, "owned", "controller enablement matches receipt")
        return PreflightResult(True, "not-applied", "controller already disabled")

    def _issued_service_enable_recovery(
        self, operation: InstallOperation, evidence: Mapping[str, str]
    ) -> MutationRecord | None:
        """After a failed observe, the enable command HAS run: build the owned
        mutation so the engine's rollback disables it."""
        if evidence.get("enable_command_issued") != "true":
            return None
        try:
            link, canonical = self._service_paths_from_desired(operation)
            proof = _timer_enablement_link_proof(link, canonical)
        except OperationFailed:
            return None
        if not _timer_enablement_link_matches_canonical(proof):
            return None
        return MutationRecord(
            operation.operation_id, operation.kind, operation.target, operation.owner, True,
            {
                "state": "disabled",
                "owned": "true",
                "enablement_link_path": str(link),
                "canonical_template_path": str(canonical),
                "enablement_link_actual_readlink": proof.actual_readlink,
                "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256,
            },
            "service-enabled",
        )

    def _decommission_paths(self, operation: InstallOperation) -> list[tuple[str, Path, Path]]:
        units = _legacy_units_for_operation(operation)
        return list(
            _legacy_unit_paths(
                units,
                _lexically_normalize_absolute(Path(operation.desired["unit_dir"])),
                _lexically_normalize_absolute(Path(operation.desired["quarantine_dir"])),
            )
        )

    # ---- T-5454: shell-box wiring in the seat's settings.json ----
    def _settings_env_preflight(self, operation: InstallOperation) -> PreflightResult:
        """Read-only. Also fail-closed on a prefix target this account cannot exec: a
        prefix pointing at a missing wrapper would break every Bash, hook and MCP launch
        on the seat (stricter than Recovery's note, HO-approved)."""
        key, value = operation.desired["env_key"], operation.desired["env_value"]
        try:
            raw, _ = _settings_env_load(Path(operation.target))
            cls, detail = _settings_env_verdict(_settings_env_parse(raw), key, value)
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        if cls == "conflicting":
            return PreflightResult(False, cls, detail)
        if not (os.path.isfile(value) and os.access(value, os.X_OK)):
            return PreflightResult(
                False,
                "missing-prefix-target",
                f"{value} is missing or not executable by this account; "
                "pull or clone pantheon-config into the seat home first",
            )
        return PreflightResult(True, cls, detail)

    def _execute_settings_env(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, dict[str, str]]:
        preflight = self._settings_env_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        path = Path(operation.target)
        key, value = operation.desired["env_key"], operation.desired["env_value"]
        raw, st = _settings_env_load(path)
        _, new = _settings_env_with_key(raw, key, value)
        if new is not None:
            _settings_env_atomic_write(path, new, raw, st)
        changed = new is not None
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, _settings_env_before(operation, raw, changed),
                _sha256_bytes(value.encode("utf-8")),
            ),
            {"env_key": key, "env_state": "set" if changed else "matching"},
        )

    def _rollback_settings_env(self, mutation: MutationRecord) -> None:
        path = Path(mutation.target)
        raw, st = _settings_env_load(path)
        check = _settings_env_can_rollback(mutation, raw)
        if not check.ok:
            raise OperationFailed(check.detail)
        if check.classification != "owned":
            return
        new = _settings_env_without_key(
            raw, mutation.before["env_key"], mutation.before["env_value"]
        )
        if new is not None:
            _settings_env_atomic_write(path, new, raw, st)

    # ---- T-5473: phase-Q residue quarantine ----
    def _bootstrap_fire_pending(self, timer: str) -> bool:
        """THE predicate: is the consume unit-fire an actual bootstrap fire? Same read as the
        disabled-timer branch of `connective.unit-fire` (preflight and execute): timer not enabled."""
        try:
            return not self.user_manager.unit_file_enablement(timer).enabled
        except _us.UserManagerError as exc:
            raise OperationFailed(f"cannot read the consume timer enablement: {exc}") from exc

    def _runtime_liveness(self, persona: str) -> dict[str, str | None]:
        """Read-only user-manager show (SERVICE_OBSERVE) of the units that mean a live runtime:
        the runtime controller and the consume service. None == unreadable."""
        states: dict[str, str | None] = {}
        for unit in (_controller_service_unit(persona), f"pantheon-persona-consume@{persona}.service"):
            try:
                states[unit] = self._user_unit_read(unit, ReadPurpose.SERVICE_OBSERVE).active_state
            except (OperationFailed, _us.UserManagerError):
                states[unit] = None
        return states

    def _residue_quarantine_preflight(self, operation: InstallOperation) -> PreflightResult:
        """Read-only; execute calls this first so both answer from the same code. T-5608: the
        body is the shared ``_residue_quarantine_check`` (the executor runs the same one)."""
        return _residue_quarantine_check(
            operation,
            bootstrap_fire_pending=self._bootstrap_fire_pending,
            liveness=self._runtime_liveness,
            entries=lambda root, persona: _bootstrap_residue_entries(Path(root), persona),
        )

    def _execute_residue_quarantine(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, dict[str, str]]:
        preflight = self._residue_quarantine_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")

        def record(changed: bool, before: dict[str, str]) -> MutationRecord:
            return MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed, before, "quarantined",
            )

        if preflight.classification == "converged":
            return record(False, {"state": "skipped", "reason": preflight.detail}), {"residue": "left-in-place"}
        root, persona, _ = _quarantine_desired(operation)
        entries = _bootstrap_residue_entries(Path(root), persona)
        if not entries:
            return record(False, {"state": "absent"}), {"residue": "absent"}
        stamp = _quarantine_stamp()
        qdir = _quarantine_dir_for(root, persona, stamp)
        if os.path.lexists(qdir):
            raise OperationFailed(f"{operation.operation_id}: quarantine dir {qdir} already exists")
        moved: list[tuple[str, str]] = []
        try:
            for cls, name in entries:
                src = f"{_quarantine_class_base(root, persona, cls)}/{name}"
                dst = f"{qdir}/{cls}/{name}"
                Path(dst).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(src, dst)  # move, never delete
                moved.append((src, dst))
        except OSError as exc:
            for src, dst in reversed(moved):
                os.rename(dst, src)
            raise OperationFailed(f"{operation.operation_id}: quarantine move failed, restored: {exc}") from exc
        listing = json.dumps([[cls, name] for cls, name in entries], separators=(",", ":"))
        before = {
            "state": "present", "persona_state_root": root, "persona": persona,
            "stamp": stamp, "moved": listing,
        }
        return record(True, before), {"residue": "quarantined", "quarantine_dir": qdir, "moved": listing}

    def _rollback_residue_quarantine(self, mutation: MutationRecord) -> None:
        check = _quarantine_can_rollback(mutation.before, os.path.lexists, mutation.target)
        if not check.ok:
            raise OperationFailed(check.detail)
        if check.classification != "owned":
            return
        done: list[tuple[str, str]] = []
        try:
            for quarantined, original in _quarantine_pairs(mutation.before, mutation.target):
                os.rename(quarantined, original)
                done.append((quarantined, original))
        except OSError as exc:
            for quarantined, original in reversed(done):
                os.rename(original, quarantined)
            raise OperationFailed(f"quarantine restore failed, nothing left half-restored: {exc}") from exc
        qdir = Path(_quarantine_dir_for(
            mutation.before["persona_state_root"], mutation.before["persona"], mutation.before["stamp"]
        ))
        for cls in _QUARANTINE_CLASSES:  # only emptied dirs; rmdir refuses anything non-empty
            try:
                (qdir / cls).rmdir()
            except OSError:
                pass
        try:
            qdir.rmdir()
        except OSError:
            pass

    # ---- T-5474: first-launch workspace trust ----
    def _workspace_trust_project(self, operation: InstallOperation) -> str:
        project = operation.desired.get("project_path", "")
        if not os.path.isabs(project) or operation.desired.get("key") != TRUST_KEY:
            raise OperationFailed("workspace trust: desired project_path/key are malformed")
        return project

    def _workspace_trust_preflight(self, operation: InstallOperation) -> PreflightResult:
        try:
            project = self._workspace_trust_project(operation)
            raw, _ = _settings_env_load(Path(operation.target))
            prior = _trust_prior(_trust_parse(raw), project)
        except OperationFailed as exc:
            return PreflightResult(False, "conflicting", str(exc))
        if prior == "true":
            return PreflightResult(True, "matching", f"projects[{project}].{TRUST_KEY} already true")
        return PreflightResult(True, "absent" if prior != "false" else "false", f"will set projects[{project}].{TRUST_KEY} = true ({prior})")

    def _execute_workspace_trust(
        self, operation: InstallOperation
    ) -> tuple[MutationRecord, dict[str, str]]:
        preflight = self._workspace_trust_preflight(operation)
        if not preflight.ok:
            raise OperationFailed(f"{operation.operation_id}: {preflight.detail}")
        project = self._workspace_trust_project(operation)
        path = Path(operation.target)
        raw, st = _settings_env_load(path)
        prior, new = _trust_with(raw, project)
        if new is not None:
            _settings_env_atomic_write(path, new, raw, st)
        changed = new is not None
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                changed,
                {"state": "absent" if raw is None else "file", "owned": "true" if changed else "false",
                 "project_path": project, "prior": prior},
                _sha256_bytes(b"true"),
            ),
            {"trust_state": "set" if changed else "matching", "prior": prior},
        )

    def _rollback_workspace_trust(self, mutation: MutationRecord) -> None:
        path = Path(mutation.target)
        raw, st = _settings_env_load(path)
        check = _trust_can_rollback(mutation.before, raw)
        if not check.ok:
            raise OperationFailed(check.detail)
        if check.classification != "owned":
            return
        action, new = _trust_without(raw, mutation.before)
        if action == "write" and new is not None:
            _settings_env_atomic_write(path, new, raw, st)
        elif action == "delete":
            # KNOWN LIMITATION (T-5474 review): between this re-read/compare and the unlink a
            # claude could write the file and that write would be lost. Accepted: it needs the op
            # to have created the file AND a write inside microseconds while rollback runs, and
            # unlink has no compare-and-delete primitive.
            current, _ = _settings_env_load(path)
            if current != raw:
                raise OperationFailed(f"{path} changed while the installer was editing it; nothing removed")
            os.unlink(path)

    def _execute_service_decommission(self, operation: InstallOperation) -> tuple[MutationRecord, dict[str, str]]:
        paths = self._decommission_paths(operation)
        present = [(n, s, q) for n, s, q in paths if os.path.lexists(s)]
        for name, src, quarantined in present:
            if os.path.lexists(quarantined):
                raise OperationFailed(f"{name}: quarantine destination already exists")
        if not present:
            return (
                MutationRecord(
                    operation.operation_id, operation.kind, operation.target, operation.owner,
                    False, {"state": "absent"}, "decommissioned",
                ),
                {"legacy_units": "absent"},
            )
        was_enabled = was_active = False
        timer_units = [n for n, _, _ in present if n.endswith(".timer")]
        service_units = [n for n, _, _ in present if n.endswith(".service")]
        if timer_units:
            timer_unit = timer_units[0]
            timer_state = self._timer_unit_state(timer_unit)
            was_enabled = timer_state.classification == "enabled"
            was_active = timer_state.active_state == "active"
            result = _completed(self.user_manager.disable_now(timer_unit))
            if result.returncode != 0:
                text = result.stderr or result.stdout
                if "not exist" not in text and "not loaded" not in text and "No such file" not in text:
                    raise OperationFailed(text.strip() or "legacy timer disable failed")
        for service_unit in service_units:
            self._stop_unit_or_converged_not_loaded(service_unit)
        quarantine_dir = paths[0][2].parent
        quarantine_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        moved = []
        for name, src, quarantined in present:
            shutil.move(str(src), str(quarantined))  # move, never delete
            moved.append(name)
        reload_result = _completed(self.user_manager.reload())
        if reload_result.returncode != 0:
            raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
        before = {
            "state": "present",
            "moved": ",".join(moved),
            "timer_was_enabled": str(was_enabled).lower(),
            "timer_was_active": str(was_active).lower(),
            "unit_dir": operation.desired["unit_dir"],
            "quarantine_dir": operation.desired["quarantine_dir"],
        }
        return (
            MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                True, before, "decommissioned",
            ),
            {"legacy_units": "quarantined", "moved": ",".join(moved)},
        )

    def _decommission_moved(self, mutation: MutationRecord) -> list[tuple[str, Path, Path]]:
        names = [n for n in mutation.before.get("moved", "").split(",") if n]
        units = _legacy_units_for_receipt(names)
        return [
            item
            for item in _legacy_unit_paths(
                units,
                _lexically_normalize_absolute(Path(mutation.before["unit_dir"])),
                _lexically_normalize_absolute(Path(mutation.before["quarantine_dir"])),
            )
            if item[0] in names
        ]

    def _decommission_can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if mutation.before.get("state") != "present":
            return PreflightResult(True, "not-applied", "nothing was decommissioned")
        try:
            moved = self._decommission_moved(mutation)
        except (OperationFailed, KeyError) as exc:
            return PreflightResult(False, "malformed", str(exc))
        for name, src, quarantined in moved:
            if os.path.lexists(src):
                return PreflightResult(False, "drifted", f"{name}: unit path re-created after receipt")
            if not os.path.lexists(quarantined):
                return PreflightResult(False, "drifted", f"{name}: quarantined copy is gone")
        return PreflightResult(True, "owned", "quarantined units match the receipt")

    def _rollback_service_decommission(self, mutation: MutationRecord) -> None:
        if mutation.before.get("state") != "present":
            return
        check = self._decommission_can_rollback(mutation)
        if not check.ok:
            raise OperationFailed(check.detail)
        for name, src, quarantined in self._decommission_moved(mutation):
            shutil.move(str(quarantined), str(src))
        reload_result = _completed(self.user_manager.reload())
        if reload_result.returncode != 0:
            raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
        if mutation.before.get("timer_was_enabled") == "true":
            timer_unit = next(
                name for name in mutation.before["moved"].split(",") if name.endswith(".timer")
            )
            result = _completed(self.user_manager.enable_now(timer_unit))
            if result.returncode != 0:
                raise OperationFailed((result.stderr or result.stdout).strip() or "legacy timer re-enable failed")

    def _timer_unit_state(self, unit: str) -> TimerUnitState:
        try:
            snapshot = self.user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE)
        except _us.UserManagerCallFailed as exc:
            result = exc.transport_evidence
            raise OperationFailed((result.stderr or result.stdout).strip() or "timer state unavailable") from None
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"timer show output {exc.reason}") from None
        return _timer_unit_state_from_snapshot(snapshot)

    def _user_unit_read(self, unit: str, purpose: ReadPurpose) -> UnitSnapshot:
        """Lenient typed read (the pre-5c.1 ``_systemd_show`` sites): a nonzero
        exit raises OperationFailed with the bare stderr/stdout text."""

        try:
            return self.user_manager.read_unit(unit, purpose)
        except _us.UserManagerCallFailed as exc:
            result = exc.transport_evidence
            raise OperationFailed((result.stderr or result.stdout).strip()) from None

    def _legacy_convergence_evidence(
        self,
        operation: InstallOperation,
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """Probe whether an already-enabled consume timer's LIVE shape
        satisfies the invariants the disabled-first-fire ->
        timer-observe apply path proves: link canonicality; timer
        active+waiting+finite-next-elapse; service inactive+success
        (see timer-observe's own apply at its _user_unit_read calls near
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
            service_state = self._user_unit_read(service, ReadPurpose.SERVICE_SETTLE)
            if _us.snapshot_service_in_flight(service_state):
                service_state = self._await_legacy_convergence_service_settle(service, service_state)
            timer_state = self._user_unit_read(timer, ReadPurpose.TIMER_ELAPSE)
        except OperationFailed:
            return None
        if timer_state.active_state != "active" or timer_state.sub_state != "waiting":
            return None
        next_elapse_basis = _us.snapshot_finite_next_elapse_basis(timer_state)
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
            service_state.active_state != "inactive"
            or service_state.result != "success"
            or not _us.exit_status_within_policy(
                service_state.exec_main_status,
                service_state.success_exit,
            )
        ):
            return None
        current_invocation = service_state.invocation_id or ""
        unit_fire_evidence = {
            "invocation_id": current_invocation,
            "active_state": service_state.active_state or "",
            "result": service_state.result or "",
            "exec_main_status": _us.render_exec_status(service_state.exec_main_status),
            # T-3107: carry the success policy into the evidence so the hybrid
            # verify() acceptance gate admits a legacy-converged in-policy non-zero
            # exit (recovery's idle-exit-5) on the re-verify path too, consistent with
            # this probe's own preflight admit. Without it that gate defaults the
            # policy to "" and rejects any non-zero exit.
            "success_exit_status": _us.render_success_exit(service_state.success_exit),
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
        initial: UnitSnapshot,
    ) -> UnitSnapshot:
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
        state = initial
        while _us.snapshot_service_in_flight(state) and self._unit_fire_monotonic() < deadline:
            self._unit_fire_sleep(LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS)
            state = self._user_unit_read(service, ReadPurpose.SERVICE_SETTLE)
        return state

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

    def _unit_inactive_dead_snapshot(self, unit: str) -> UnitSnapshot:
        try:
            return self.user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE)
        except _us.UserManagerCallFailed as exc:
            result = exc.transport_evidence
            raise OperationFailed((result.stderr or result.stdout).strip() or f"{unit} state unavailable") from None
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"{unit} show output {exc.reason}") from None

    @staticmethod
    def _unit_presence_state(snapshot: UnitSnapshot) -> TimerUnitState:
        return TimerUnitState(
            classification=_us.classify_unit_file_presence(snapshot),
            load_state=snapshot.load_state or "",
            unit_file_state=snapshot.unit_file_state or "",
            active_state=snapshot.active_state or "",
            sub_state=snapshot.sub_state or "",
        )

    def _unit_inactive_dead(self, unit: str) -> TimerUnitState:
        return self._unit_presence_state(self._unit_inactive_dead_snapshot(unit))

    def _prove_unit_inactive_dead(self, unit: str) -> TimerUnitState:
        snapshot = self._unit_inactive_dead_snapshot(unit)
        state = self._unit_presence_state(snapshot)
        if not _us.proves_inactive_dead(snapshot):
            raise OperationFailed(
                f"{unit} did not prove inactive/dead: "
                f"LoadState={state.load_state} UnitFileState={state.unit_file_state} "
                f"ActiveState={state.active_state} SubState={state.sub_state}"
            )
        return state

    def _prove_unit_notfound_inactive_dead(self, unit: str) -> TimerUnitState:
        snapshot = self._unit_inactive_dead_snapshot(unit)
        state = self._unit_presence_state(snapshot)
        if not _us.proves_notfound_inactive_dead(snapshot):
            raise OperationFailed(
                f"{unit} did not prove not-found/inactive/dead: "
                f"LoadState={state.load_state} UnitFileState={state.unit_file_state} "
                f"ActiveState={state.active_state} SubState={state.sub_state}"
            )
        return state

    def _stop_unit_or_converged_not_loaded(self, unit: str) -> TimerUnitState:
        stopped = _completed(self.user_manager.stop_unit(unit))
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
        stopped = _completed(self.user_manager.stop_unit(unit))
        if stopped.returncode != 0:
            raise OperationFailed((stopped.stderr or stopped.stdout).strip() or f"{unit} stop failed")
        return self._prove_unit_inactive_dead(unit)

    def preflight(
        self, operation: InstallOperation, context: ExecutionContext | None = None
    ) -> PreflightResult:
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
        if operation.kind in {"atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
            return _unmanaged_file_preflight(content, operation)
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
        if operation.kind == "harness-check" and operation.target == "hermes":
            # T-5011 PR-B: the profile's executable, never a PATH lookup of "codex".
            executable = str(operation.desired.get("executable", ""))
            ok = bool(executable) and os.path.isfile(executable) and os.access(executable, os.X_OK)
            return PreflightResult(
                ok,
                "harness",
                f"hermes executable {executable or '<undeclared>'} "
                + ("found" if ok else "missing or not executable"),
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
        if operation.kind == "seat-periodic-timer-observe":
            # pure prediction text: needs neither the systemd manager nor the account gate
            return PreflightResult(True, "transient-tolerant", periodic_observe_prediction(operation))
        if operation.kind == "unit-fire":
            try:
                _parse_unit_fire_timeout_budget(operation)
                _parse_unit_fire_initial_viability_desired(operation)
            except OperationFailed as exc:
                return PreflightResult(False, "unit-fire", str(exc))
        account = self._current_account_only(operation.owner)
        if not account.ok:
            return account
        if operation.kind == SETTINGS_ENV_KIND:
            # file-only: no user manager is needed to edit settings.json
            return self._settings_env_preflight(operation)
        if operation.kind == WORKSPACE_TRUST_KIND:
            return self._workspace_trust_preflight(operation)  # file-only, like settings-env
        manager = self._manager_ready()
        if not manager.ok:
            return manager
        if operation.kind == "stale-unit-failure-reset":
            return self._stale_reset_preflight(operation)
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
            enabled = self.user_manager.unit_file_enablement(operation.desired["timer"])
            if enabled.enabled and "connective.timer-observe" not in self.events:
                evidence = self._legacy_convergence_evidence(operation)
                if evidence is None:
                    return PreflightResult(
                        False,
                        "conflicting",
                        "timer is already enabled without this backend's convergence evidence",
                    )
                self._seed_legacy_convergence_events(*evidence)
            elif not enabled.enabled:
                # T-5471: execute runs the bootstrap gates and the pre-enqueue idle proof on
                # a disabled timer; the dry run answers them from the same live reads and
                # the same functions.
                self._blocker_preflight_mode = True
                try:
                    blocker = self._unit_fire_bootstrap_blocker(operation)
                finally:
                    self._blocker_preflight_mode = False
                if blocker:
                    return PreflightResult(False, "conflicting", blocker)
                idle, detail = _unit_fire_idle_prediction(
                    lambda: self._stale_reset_read_classify(operation.target)[1]
                )
                if not idle:
                    return PreflightResult(False, "conflicting", detail)
                persona = operation.provenance.get("persona", "")
                if _maintenance_bypass_requested(operation) and persona and self._seat_in_maintenance(persona):
                    return PreflightResult(True, "maintenance-bypass", MAINTENANCE_BYPASS_PREFLIGHT_NOTE)
        if operation.kind == "seat-periodic-timer-enable":
            try:
                link, canonical = self._periodic_paths_from_desired(operation)
                timer_state = self._timer_unit_state(operation.target)
                proof = _timer_enablement_link_proof(link, canonical)
                if timer_state.classification == "enabled":
                    if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                        return PreflightResult(False, "conflicting", "enabled timer link does not resolve to canonical template")
                    return PreflightResult(True, "enabled", "timer already enabled; tolerated, not receipt-owned")
                if proof.snapshot_state != "absent":
                    return PreflightResult(False, "conflicting", "timer enablement link already exists")
            except OperationFailed as exc:
                return PreflightResult(False, "conflicting", str(exc))
            return PreflightResult(True, "absent", "timer will be enabled")
        if operation.kind == "seat-service-enable-observe":
            try:
                link, canonical = self._service_paths_from_desired(operation)
                # filesystem-only: the unit may not be installed/loaded yet at
                # plan-wide preflight, so the manager is consulted at execute time
                proof = _timer_enablement_link_proof(link, canonical)
                if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                    return PreflightResult(False, "conflicting", "controller enablement link is not canonical")
            except OperationFailed as exc:
                return PreflightResult(False, "conflicting", str(exc))
            return PreflightResult(True, "absent", "controller will be enabled (or is already enabled and tolerated)")
        if operation.kind == RESIDUE_QUARANTINE_KIND:
            return self._residue_quarantine_preflight(operation)
        if operation.kind == "seat-service-decommission":
            try:
                for name, src, quarantined in self._decommission_paths(operation):
                    if os.path.lexists(src) and os.path.lexists(quarantined):
                        return PreflightResult(False, "conflicting", f"{name}: quarantine destination already exists")
            except OperationFailed as exc:
                return PreflightResult(False, "conflicting", str(exc))
            return PreflightResult(True, "ready", "legacy-unit decommission is safe to apply")
        if operation.kind == "timer-enable":
            try:
                link, canonical = self._timer_enablement_paths_from_desired(operation)
                timer_state = self._timer_unit_state(operation.target)
                refusal = _staged_activate_enabled_timer_refusal(
                    context.workflow if context is not None else "",
                    timer_state.classification == "enabled",
                    "connective.timer-observe" in self.events,
                )
                if refusal:
                    return PreflightResult(False, "conflicting", refusal)
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
            receiver = self._user_unit_read(operation.target, ReadPurpose.RECEIVER_ACTIVE)
            enabled = self.user_manager.unit_file_enablement(operation.target)
            environment_file = self.user_manager.environment_file_declared(operation.target)
            if enabled.enabled:
                return PreflightResult(False, "conflicting", "codex telegram receiver is enabled before cutover")
            if not _us.receiver_idle(receiver.active_state, receiver.main_pid):
                return PreflightResult(False, "conflicting", "codex telegram receiver is active before cutover")
            if environment_file:
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

    def _seat_in_maintenance(self, persona: str) -> bool:
        """The predicate consume itself uses to skip a fire (T-5282): fail-open."""
        from tools import persona_bringup

        return bool(persona_bringup._seat_in_maintenance(persona))

    def _seat_deep_sleeping(self, persona: str) -> bool:
        """T-5475: consume's own deep-sleep predicate (read-only, fail-open): the bypass never
        overrides it."""
        from tools import persona_bringup

        return bool(persona_bringup._seat_deep_sleeping(persona))

    def _unit_fire_bootstrap_blocker(self, operation: InstallOperation) -> str | None:
        """T-5471: reasons a DISABLED-timer consume bootstrap fire cannot launch the runtime.
        One function, called by the dry-run preflight and again by execute right before the
        enqueue, so both report the same thing. Read-only.

        * seat maintenance: consume skips an armed seat (T-5282); the hand-move SOP writes a
          one-shot bypass token, the installer refuses and lets the operator decide.
        * workspace trust: a claude seat whose ~/.claude.json lacks
          projects[<clone>].hasTrustDialogAccepted blocks on the trust modal at first launch.
        * control-plane residue (SOP phase Q, minus the operator holds): stale records that
          block or mis-steer the bootstrap; the operator quarantines them (move, never delete).
        """
        persona = operation.provenance.get("persona", "")
        # T-5475: armed maintenance refuses unless the operator asked for the one-shot bypass
        # (digest-bound); deep-sleep is never bypassed. Shared with the simulated twin.
        maintenance = _maintenance_gate(
            persona,
            in_maintenance=bool(persona and self._seat_in_maintenance(persona)),
            deep_sleeping=lambda: self._seat_deep_sleeping(persona),
            bypass=_maintenance_bypass_requested(operation),
        )
        if maintenance:
            return maintenance
        try:
            viability = _parse_unit_fire_initial_viability_desired(operation)
        except OperationFailed as exc:
            return str(exc)
        # T-5473/T-5474: in the DRY RUN only, a blocker the plan's own earlier op clears is
        # predicted clear (optimistic: the backend sees one op, not the scoped op set). Execute
        # calls with preflight=False and re-checks for real.
        # The mode is an instance flag (set only around the dry-run call) so the method keeps
        # its one-argument signature: subclasses and test doubles override it that way.
        preflight = getattr(self, "_blocker_preflight_mode", False)
        trust_cleared = preflight and bool(operation.desired.get("trust_set_by"))
        residue_cleared = preflight and bool(operation.desired.get("residue_cleared_by"))
        if operation.provenance.get("selected_harness") == "claude" and not trust_cleared:
            clone = viability.account_home / "pantheon-config"
            trusted = False
            try:
                document = json.loads((viability.account_home / ".claude.json").read_text(encoding="utf-8"))
                project = (document.get("projects") or {}).get(str(clone)) or {}
                trusted = project.get("hasTrustDialogAccepted") is True
            except (OSError, ValueError, AttributeError):
                trusted = False
            if not trusted:
                return (
                    f"workspace trust missing: ~/.claude.json projects[{clone}].hasTrustDialogAccepted is not "
                    "true, so the first launch would block on the trust modal"
                )
        residue = [] if residue_cleared else _bootstrap_residue(viability.persona_state_root, persona)
        if residue:
            return (
                "control-plane residue would block the bootstrap (quarantine per the hand-move SOP phase Q, "
                "move never delete, keep overrides/ holds, controller-evidence.jsonl and "
                "runtime-operation.lock): " + ", ".join(residue)
            )
        return None

    def _stale_reset_success_exit(self, unit: str) -> "Callable[[], Any]":
        """Lazy read of the unit's SuccessExitStatus (SERVICE_OBSERVE, read-only): the
        STALE_RESET property tuple is shared with the broker and is not widened."""

        def read() -> Any:
            try:
                return self._user_unit_read(unit, ReadPurpose.SERVICE_OBSERVE).success_exit
            except _us.UserManagerProtocolError as exc:
                raise OperationFailed(f"stale unit show output {exc.reason}") from None

        return read

    def _stale_reset_read_classify(self, unit: str) -> tuple[UnitSnapshot, str]:
        pre = self._stale_unit_failure_reset_show(unit)
        return pre, _classify_stale_reset_for_install(pre, self._stale_reset_success_exit(unit))

    def _stale_reset_preflight(self, operation: InstallOperation) -> PreflightResult:
        """T-5471: the dry-run twin of ``_execute_stale_unit_failure_reset`` -- the same
        read-only ``systemctl --user show`` and the SAME classifier, no reset."""
        try:
            _pre, classification = self._stale_reset_read_classify(operation.target)
        except OperationFailed as exc:
            return PreflightResult(False, "refused", str(exc))
        return PreflightResult(True, classification, f"consume service stale state: {classification}")

    def _stale_unit_failure_reset_show(
        self,
        unit: str,
    ) -> UnitSnapshot:
        try:
            return self.user_manager.read_unit(unit, ReadPurpose.STALE_RESET)
        except _us.UserManagerCallFailed as exc:
            result = exc.transport_evidence
            stdout = result.stdout or ""
            raise _stale_unit_failure_reset_failure(
                (result.stderr or stdout).strip() or "stale unit state unavailable"
            ) from None
        except _us.UserManagerProtocolError as exc:
            raise _stale_unit_failure_reset_failure(f"stale unit show output {exc.reason}") from exc

    def _execute_stale_unit_failure_reset(
        self,
        operation: InstallOperation,
    ) -> tuple[MutationRecord, OperationEvidence]:
        pre = self._stale_unit_failure_reset_show(operation.target)
        try:
            classification = _classify_stale_reset_for_install(
                pre, self._stale_reset_success_exit(operation.target)
            )
        except OperationFailed as exc:
            raise _stale_unit_failure_reset_failure(str(exc), pre=pre) from exc
        evidence = _stale_unit_failure_reset_snapshot_evidence("pre", pre)
        if classification == STALE_ADOPTED:
            # T-5471: an already-converged consume service. Nothing to reset, nothing owned.
            before = {"state": STALE_ADOPTED}
            evidence.update(
                {
                    "classification": "adopted",
                    "pre_state": STALE_ADOPTED,
                    "post_state": STALE_ADOPTED,
                }
            )
        elif classification == "clean-noop":
            before = {"state": "clean-noop"}
            evidence.update(
                {
                    "classification": "noop",
                    "pre_state": "clean-noop",
                    "post_state": "clean-noop",
                }
            )
        else:
            reset = _completed(self.user_manager.reset_failed_unit(operation.target))
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
        containment = _realpath_containment_refusal(
            operation, context.allowed_roots
        ) or _desired_path_refusal(operation, context.allowed_roots)
        if containment:
            raise OperationFailed(f"target path check: {containment}")
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
        elif operation.kind in {"atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
            receiver = self._user_unit_read(operation.target, ReadPurpose.RECEIVER_ACTIVE)
            enabled = self.user_manager.unit_file_enablement(operation.target)
            environment_file = self.user_manager.environment_file_declared(operation.target)
            enabled_state = enabled.state if enabled.state else "disabled"
            if enabled.enabled:
                raise OperationFailed("codex telegram receiver is enabled before cutover")
            if not _us.receiver_idle(receiver.active_state, receiver.main_pid):
                raise OperationFailed("codex telegram receiver is active before cutover")
            if environment_file:
                raise OperationFailed("codex telegram unit must not embed an EnvironmentFile")
            observed = {
                "active": receiver.active_state or "",
                "enabled": enabled_state,
                "main_pid": _us.render_main_pid(receiver.main_pid) or "0",
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
        elif operation.kind in {"daemon-reload", "seat-periodic-daemon-reload"}:
            result = _completed(self.user_manager.reload())
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
            fire_target = operation.desired.get("tmux_target", "")
            pre_pane_pid = PANE_PID_ABSENT
            fire_wall_start: datetime | None = None
            invocation_id = ""
            start_time = self._unit_fire_monotonic()
            total_deadline = start_time + budget.seconds
            teardown_margin = RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS
            observation_deadline = total_deadline - teardown_margin
            if observation_deadline <= start_time:
                raise OperationFailed("unit-fire timeout budget cannot preserve teardown reserve")
            enabled = self.user_manager.unit_file_enablement(
                timer,
                timeout=self._remaining_timeout_seconds(observation_deadline),
            )
            if enabled.enabled and "connective.timer-observe" in self.events:
                observed = dict(self.events["connective.unit-fire"].evidence)
            elif enabled.enabled:
                raise OperationFailed("timer was enabled before disabled first fire")
            else:
                blocker = self._unit_fire_bootstrap_blocker(operation)
                if blocker:
                    raise OperationFailed(blocker)
                self._baseline_invocation = ""
                last_snapshot: UnitFirePollSnapshot | None = None
                # T-5475: the one-shot bypass token (only when the operator asked, digest-bound)
                bypass_requested = _maintenance_bypass_requested(operation)
                bypass_persona = operation.provenance.get("persona", "")
                bypass_state = "none"
                try:
                    baseline = self._prove_unit_fire_pre_enqueue_idle(
                        operation.target,
                        timeout=self._remaining_timeout_seconds(observation_deadline),
                    )
                    # T-5476: ownership proof for the session this fire may launch. A pane that
                    # already exists is not ours; the wall-clock start is compared with
                    # runtime.json's wall-clock started_at (same basis: UTC wall time).
                    pre_pane_pid = self._fire_pane_pid(fire_target)
                    fire_wall_start = self._fire_wall_clock()
                    if bypass_requested:
                        # re-check the arm right before the write (SOP phase 4 step 1); a lapsed window
                        # means a normal fire and no token. Written => this run owns it (finally).
                        if self._seat_in_maintenance(bypass_persona):
                            _write_bypass_token(
                                viability.persona_state_root, bypass_persona,
                                f"persona_installer {operation.operation_id}",
                            )
                            bypass_state = "written"
                        else:
                            bypass_state = "not-needed-maintenance-lapsed"
                    start = _completed(
                        self.user_manager.start_unit(
                            operation.target,
                            wait=False,
                            timeout=self._remaining_timeout_seconds(observation_deadline),
                        )
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
                        if (
                            self._baseline_invocation
                            and last_snapshot.invocation_id == self._baseline_invocation
                            and last_snapshot.active_state == "inactive"
                        ):
                            # T-5471: still the PRIOR run's id on an inactive unit: the enqueued
                            # start has not begun. Not a capture, not a completion; keep polling.
                            self._sleep_until_next_unit_fire_poll(observation_deadline)
                            continue
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
                    if bypass_state == "written":
                        # consume unlinks a token it reads (valid or not); one that survived the fire
                        # was never read, so the fire proved nothing about the bypass (SOP step 4)
                        if _remove_bypass_token(viability.persona_state_root, bypass_persona):
                            bypass_state = "survived"
                            raise OperationFailed(
                                "consume did not read the maintenance bypass token",
                                evidence={MAINTENANCE_BYPASS_EVIDENCE: "survived-removed"},
                            )
                        bypass_state = "consumed"
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
                    launch = self._fire_launch_identity(
                        operation, pre_pane_pid=pre_pane_pid, fire_wall_start=fire_wall_start,
                        invocation_id=invocation_id,
                    )
                    if launch:
                        self.__dict__.setdefault("_fire_launched_sessions", []).append(launch)
                        mutation = MutationRecord(
                            operation.operation_id, operation.kind, operation.target, operation.owner,
                            True, launch, "fire-launched",
                        )
                        observed["fire_session_pid"] = launch["pane_pid"]
                        observed["fire_session_target"] = launch["tmux_target"]
                    else:
                        observed["fire_session"] = "none-owned"
                    if bypass_requested:
                        observed[MAINTENANCE_BYPASS_EVIDENCE] = bypass_state
                    if bypass_state == "consumed":
                        # the receipt records that THIS run wrote the token; rollback keys off it
                        marker = {
                            MAINTENANCE_BYPASS_EVIDENCE: "written",
                            "persona": bypass_persona,
                            "persona_state_root": str(viability.persona_state_root),
                        }
                        if launch:
                            mutation = MutationRecord(
                                operation.operation_id, operation.kind, operation.target, operation.owner,
                                True, {**launch, **marker}, "fire-launched",
                            )
                        else:
                            mutation = MutationRecord(
                                operation.operation_id, operation.kind, operation.target, operation.owner,
                                True, marker, "bypass-token-written",
                            )
                except OperationFailed as exc:
                    if started:
                        cleanup_failure: OperationFailed | None = None
                        try:
                            cleanup = self._stop_and_prove_unit_fire_inactive(
                                operation.target,
                                total_deadline=total_deadline,
                            )
                            cleanup_state = cleanup.active_state
                        except OperationFailed as cleanup_exc:
                            cleanup_failure = cleanup_exc
                        # T-5476: whatever the fire launched is stopped too, matched on identity
                        session_evidence = self._stop_fire_launched_session(
                            operation, pre_pane_pid=pre_pane_pid, fire_wall_start=fire_wall_start,
                            invocation_id=invocation_id,
                        )
                        if cleanup_failure is not None:
                            failure = (
                                cleanup_failure
                                if cleanup_failure.evidence
                                else _unit_fire_operation_failed(
                                    str(cleanup_failure),
                                    phase="cleanup",
                                    classification="unsafe-cleanup",
                                    snapshot=last_snapshot,
                                    start_count=start_count,
                                    cleanup_state="unsafe-active",
                                )
                            )
                            if session_evidence:
                                failure = OperationFailed(
                                    str(failure), evidence={**failure.evidence, **session_evidence}
                                )
                            raise failure from (exc if cleanup_failure.evidence else cleanup_failure)
                        if exc.evidence or session_evidence:
                            evidence = dict(exc.evidence)
                            evidence["cleanup_state"] = cleanup_state
                            evidence.update(session_evidence)
                            raise OperationFailed(str(exc), evidence=evidence) from exc
                    raise
                finally:
                    # T-5475: never leave a token behind on any path; only one THIS run wrote
                    if bypass_state == "written":
                        _remove_bypass_token(viability.persona_state_root, bypass_persona)
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
        elif operation.kind == "seat-periodic-timer-enable":
            mutation, observed = self._execute_periodic_enable(operation)
        elif operation.kind == "seat-periodic-timer-observe":
            observed = self._execute_periodic_observe(operation)
        elif operation.kind == "seat-service-enable-observe":
            mutation, observed = self._execute_service_enable_observe(operation)
        elif operation.kind == "seat-service-decommission":
            mutation, observed = self._execute_service_decommission(operation)
        elif operation.kind == SETTINGS_ENV_KIND:
            mutation, observed = self._execute_settings_env(operation)
        elif operation.kind == WORKSPACE_TRUST_KIND:
            mutation, observed = self._execute_workspace_trust(operation)
        elif operation.kind == RESIDUE_QUARANTINE_KIND:
            mutation, observed = self._execute_residue_quarantine(operation)
        elif operation.kind == "timer-enable":
            link, canonical = self._timer_enablement_paths_from_desired(operation)
            timer_state = self._timer_unit_state(operation.target)
            if timer_state.classification == "not-found":
                raise OperationFailed("timer unit is not loaded")
            before_enabled = timer_state.classification == "enabled"
            before: dict[str, str] = {"state": "enabled" if before_enabled else "disabled"}
            refusal = _staged_activate_enabled_timer_refusal(
                context.workflow, before_enabled, "connective.timer-observe" in self.events
            )
            if refusal:
                raise OperationFailed(refusal)
            if not before_enabled:
                self._require_timer_enablement_link_absent(
                    operation.owner,
                    operation.target,
                    link_path=link,
                    canonical_template_path=canonical,
                    context=operation.operation_id,
                )
                result = _completed(self.user_manager.enable_now(operation.target))
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
            service_state = self._user_unit_read(service, ReadPurpose.SERVICE_OBSERVE)
            current = service_state.invocation_id or ""
            if current and current != first_invocation:
                raise OperationFailed("consume service InvocationID changed after timer arm")
            # T-3107: hybrid exit-status invariant (Result==success AND exit in
            # {0} u SuccessExitStatus) via the shared helper, matching the
            # legacy-convergence probe -- a consume service whose success policy
            # maps a benign non-zero exit is accepted on the active first-fire
            # path too.
            if (
                service_state.active_state != "inactive"
                or service_state.result != "success"
                or not _us.exit_status_within_policy(
                    service_state.exec_main_status,
                    service_state.success_exit,
                )
            ):
                raise OperationFailed("consume service is not inactive successful after timer arm")
            timer_state = self._user_unit_read(operation.target, ReadPurpose.TIMER_ELAPSE)
            if timer_state.active_state != "active" or timer_state.sub_state != "waiting":
                raise OperationFailed("timer is not active and waiting after arm")
            next_elapse_basis = _us.snapshot_finite_next_elapse_basis(timer_state)
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
        if operation.kind in {"mkdir", "atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install"}:
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
        if operation.kind == SETTINGS_ENV_KIND:
            try:
                raw, _ = _settings_env_load(Path(operation.target))
                cls, detail = _settings_env_verdict(
                    _settings_env_parse(raw),
                    operation.desired["env_key"],
                    operation.desired["env_value"],
                )
            except OperationFailed as exc:
                cls, detail = "conflicting", str(exc)
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, cls == "matching", detail
            )
        if operation.kind == WORKSPACE_TRUST_KIND:
            try:
                raw, _ = _settings_env_load(Path(operation.target))
                prior = _trust_prior(_trust_parse(raw), self._workspace_trust_project(operation))
                ok, detail = prior == "true", f"workspace trust state: {prior}"
            except OperationFailed as exc:
                ok, detail = False, str(exc)
            return OperationEvidence(operation.operation_id, operation.layer, operation.kind, ok, detail)
        if operation.kind == RESIDUE_QUARANTINE_KIND:
            # Structural only: the post-bootstrap verify legitimately finds a fresh runtime.json,
            # so "no residue" would false-fail. The receipt's own invariant is that the quarantined
            # copies still exist.
            try:
                _quarantine_desired(operation)
                moved = json.loads((staged_evidence.evidence.get("moved") if staged_evidence else "") or "[]")
                qdir = (staged_evidence.evidence.get("quarantine_dir") if staged_evidence else "") or ""
                missing = [n for c, n in moved if not os.path.lexists(f"{qdir}/{c}/{n}")]
                ok = not missing
                detail = "quarantined copies present" if ok else f"quarantined copies missing: {missing}"
            except (OperationFailed, ValueError, TypeError) as exc:
                ok, detail = False, str(exc)
            return OperationEvidence(operation.operation_id, operation.layer, operation.kind, ok, detail)
        if operation.kind == "seat-service-decommission":
            try:
                ok = not any(os.path.lexists(src) for _, src, _ in self._decommission_paths(operation))
                detail = "ho-resumer units absent from the unit dir" if ok else "ho-resumer units still present"
            except OperationFailed as exc:
                ok, detail = False, str(exc)
            return OperationEvidence(operation.operation_id, operation.layer, operation.kind, ok, detail)
        if operation.kind == "seat-service-enable-observe":
            if operation.operation_id in self.events:
                return self.events[operation.operation_id]
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, False,
                "live execution evidence absent",
            )
        if _is_periodic_operation(operation):
            if operation.kind == "seat-periodic-timer-enable":
                try:
                    link, canonical = self._periodic_paths_from_desired(operation)
                    enabled = self._timer_unit_state(operation.target).classification == "enabled"
                    proof = _timer_enablement_link_proof(link, canonical)
                    ok = enabled and (
                        proof.snapshot_state == "absent" or _timer_enablement_link_matches_canonical(proof)
                    )
                    detail = "periodic timer enabled" if ok else "periodic timer not enabled or link not canonical"
                except OperationFailed as exc:
                    ok, detail = False, str(exc)
                return OperationEvidence(operation.operation_id, operation.layer, operation.kind, ok, detail)
            if operation.operation_id in self.events:
                return self.events[operation.operation_id]
            return OperationEvidence(
                operation.operation_id, operation.layer, operation.kind, False,
                "live execution evidence absent",
            )
        if operation.operation_id in self.events:
            return self.events[operation.operation_id]
        if operation.kind in {
            "stale-unit-failure-reset",
            "daemon-reload",
            "seat-periodic-daemon-reload",
            "seat-periodic-timer-enable",
            "seat-periodic-timer-observe",
            "seat-service-enable-observe",
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
        if mutation.kind == "unit-fire":
            return self._fire_launch_can_rollback(mutation)
        if _is_service_enable_mutation(mutation):
            return self._service_can_rollback(mutation)
        if mutation.kind == SETTINGS_ENV_KIND:
            try:
                raw, _ = _settings_env_load(Path(mutation.target))
            except OperationFailed as exc:
                return PreflightResult(False, "drifted", str(exc))
            return _settings_env_can_rollback(mutation, raw)
        if mutation.kind == WORKSPACE_TRUST_KIND:
            try:
                raw, _ = _settings_env_load(Path(mutation.target))
            except OperationFailed as exc:
                return PreflightResult(False, "drifted", str(exc))
            return _trust_can_rollback(mutation.before, raw)
        if mutation.kind == RESIDUE_QUARANTINE_KIND:
            return _quarantine_can_rollback(mutation.before, os.path.lexists, mutation.target)
        if mutation.kind == "seat-service-decommission":
            return self._decommission_can_rollback(mutation)
        if _is_periodic_mutation(mutation):
            if mutation.before.get("owned") != "true":
                return PreflightResult(
                    True, "not-applied", "pre-existing enablement is not receipt-owned; left alone"
                )
            try:
                proof = self._require_periodic_link_matches_receipt(mutation)
                state = self._timer_unit_state(mutation.target)
            except OperationFailed as exc:
                return PreflightResult(False, "drifted", str(exc))
            if proof.snapshot_state == "symlink" or state.classification == "enabled":
                return PreflightResult(True, "owned", "timer enablement matches receipt")
            return PreflightResult(True, "not-applied", "timer already disabled")
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

    def rollback_mutation(
        self,
        mutation: MutationRecord,
        *,
        allowed_roots: Sequence[str] = (),
    ) -> None:
        containment = _rollback_containment_refusal(mutation, allowed_roots)
        if containment:
            raise OperationFailed(f"target path check: {containment}")
        if mutation.kind == "unit-fire":
            self._rollback_fire_launch(mutation)
            return
        if _is_periodic_mutation(mutation):
            self._rollback_periodic_enable(mutation)
            return
        if _is_service_enable_mutation(mutation):
            self._rollback_service_enable(mutation)
            return
        if mutation.kind == "seat-service-decommission":
            self._rollback_service_decommission(mutation)
            return
        if mutation.kind == SETTINGS_ENV_KIND:
            blocked = self._live_session_key_block()
            if blocked is not None:
                raise OperationFailed(blocked.detail)
            self._rollback_settings_env(mutation)
            return
        if mutation.kind == WORKSPACE_TRUST_KIND:
            self._rollback_workspace_trust(mutation)
            return
        if mutation.kind == RESIDUE_QUARANTINE_KIND:
            self._rollback_residue_quarantine(mutation)
            return
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
                result = _completed(self.user_manager.disable_now(mutation.target))
                if result.returncode != 0:
                    self._prove_unit_inactive_dead(mutation.target)
                    self._prove_unit_inactive_dead(service)
                    missing_unit = (result.stderr or result.stdout)
                    if "not exist" not in missing_unit and "not loaded" not in missing_unit and "No such file" not in missing_unit:
                        raise OperationFailed(missing_unit.strip() or "timer disable failed")
                reload_result = _completed(self.user_manager.reload())
                if reload_result.returncode != 0:
                    raise OperationFailed((reload_result.stderr or reload_result.stdout).strip() or "daemon-reload failed")
                reset_result = _completed(self.user_manager.reset_failed_unit(mutation.target))
                if reset_result.returncode != 0:
                    raise OperationFailed((reset_result.stderr or reset_result.stdout).strip() or "timer reset-failed failed")
                service_reset = _completed(self.user_manager.reset_failed_unit(service))
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
            result = _completed(self.user_manager.enable_now(mutation.target))
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
        if operation.kind == "seat-service-enable-observe":
            return self._issued_service_enable_recovery(operation, evidence)
        if operation.kind == "unit-fire":
            try:
                identity = json.loads(evidence.get("fire_session_identity", ""))
            except ValueError:
                return None
            if not isinstance(identity, dict) or identity.get("launched") != "true":
                return None
            return MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                True, {str(k): str(v) for k, v in identity.items()}, "fire-launched",
            )
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


class FileReceiptStore:
    """Operator-run local receipt sink: one 0600 JSON file per receipt in
    the plan's contained ``receipt_dir``.

    Moved verbatim out of ``LocalHostBackend.store_receipt`` (T-2917 5a);
    the written bytes are pinned by a golden test so the split cannot
    drift the on-disk receipt format.
    """

    def store_receipt(self, plan: InstallPlan, receipt: ApplyReceipt) -> None:
        problem = _path_containment_problem(
            "receipt_dir", plan.provenance["receipt_dir"], "dir", _plan_allowed_roots(plan)
        )
        if problem:
            raise OperationFailed(f"target path check: {problem}")
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


def _seat_verbs(parser: argparse.ArgumentParser, subparsers: Any) -> None:
    """T-5449: apply / rollback / verify-seat. Registered here, implemented in
    persona_installer_apply / persona_installer_verify_seat (lazy import: those
    modules import this one)."""
    default_manifest = Path(__file__).resolve().parent.parent / "instance.yaml"
    apply_p = subparsers.add_parser(
        "apply",
        help="seat-local apply as the target account (DRY unless --execute)",
        description=(
            "Run as the target account (from its own clone, as that user). Without "
            "--execute: build the plan, run every operation's read-only preflight against "
            "this host, print the plan digest and a verdict per operation, exit non-zero if "
            "anything would refuse. With --execute --plan-digest <digest printed by the dry "
            "run>: perform exactly that plan. Workflows: all-in-one (default); for a seat "
            "that is ALREADY LIVE prefer stage-prefix then staged-activate (leaves a "
            "rollback point and a window before the consume fire); there is no "
            "auto-detection."
        ),
    )
    apply_p.add_argument("--manifest", type=Path, default=default_manifest)
    apply_p.add_argument("--persona", required=True)
    apply_p.add_argument("--harness")
    apply_p.add_argument("--workflow", choices=("all-in-one", "stage-prefix", "staged-activate", "reconcile-keep-going"),
                         default="all-in-one")
    apply_p.add_argument("--execute", action="store_true")
    apply_p.add_argument("--plan-digest", help="digest printed by the dry run (required with --execute)")
    apply_p.add_argument("--receipt", help="staged receipt id (staged-activate only)")
    apply_p.add_argument(
        "--maintenance-bypass", action="store_true",
        help=(
            "T-5475: bootstrap a seat that is in maintenance by writing consume's one-shot bypass token "
            "around the first fire (verified consumed). Part of the plan digest: give it on EVERY verb "
            "whose plan is digest-bound (dry run, --execute, stage-prefix, staged-activate). Never "
            "overrides deep-sleep. Default: refuse while armed."
        ),
    )
    rollback_p = subparsers.add_parser("rollback", help="roll back an apply by receipt id")
    rollback_p.add_argument("--manifest", type=Path, default=default_manifest)
    rollback_p.add_argument("--persona", required=True)
    rollback_p.add_argument("--receipt", required=True)
    rollback_p.add_argument("--break-glass-no-manifest", action="store_true",
                            help="incident recovery: never load the manifest; account and receipt dir "
                                 "come from the effective user and the digest-checked sidecar plan")
    rollback_p.add_argument("--receipt-dir", type=Path, default=None,
                            help="with --break-glass-no-manifest: non-default receipt dir "
                                 "(must stay inside the effective account's home)")
    verify_p = subparsers.add_parser(
        "verify-seat",
        help="read-only post-install check for one seat; JSON report, non-zero on any failure",
    )
    verify_p.add_argument("--manifest", type=Path, default=default_manifest)
    verify_p.add_argument("--persona", required=True)


def _run_seat_verb(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from tools import persona_installer_apply as _apply

    try:
        if args.command == "verify-seat":
            from tools import persona_installer_verify_seat as _verify

            report = _verify.verify_seat(args.manifest, args.persona)
            _print_json(report)
            return 0 if report["ok"] else 1
        if args.command == "rollback":
            if args.receipt_dir is not None and not args.break_glass_no_manifest:
                parser.error("--receipt-dir requires --break-glass-no-manifest")
            code, summary = _apply.rollback_receipt(
                args.manifest, args.persona, args.receipt,
                break_glass=args.break_glass_no_manifest, receipt_dir=args.receipt_dir,
            )
        elif args.execute:
            if not args.plan_digest:
                parser.error("--execute requires --plan-digest <digest printed by the dry run>")
            code, summary = _apply.execute(
                args.manifest, args.persona, plan_digest=args.plan_digest,
                workflow=args.workflow, receipt_id=args.receipt, harness=args.harness,
                maintenance_bypass=args.maintenance_bypass,
            )
        else:
            code, summary = _apply.dry_run(
                args.manifest, args.persona, workflow=args.workflow, harness=args.harness,
                maintenance_bypass=args.maintenance_bypass,
            )
    except InstallerError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    _print_json(summary)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    _seat_verbs(parser, subparsers)
    for name in ("plan", "simulate"):
        child = subparsers.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--persona", required=True)
        child.add_argument("--harness")
        child.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args(argv)
    if args.command in {"apply", "rollback", "verify-seat"}:
        return _run_seat_verb(args, parser)
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
        applied = installer.apply(plan, plan.digest, backend, actor, manifest=manifest)
        verified = installer.verify(plan, backend)
        _print_json({"plan": plan, "apply": applied, "verify": verified})
        return 0 if applied.status in {"applied", "noop"} and verified.status == "verified" else 1
    except InstallerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
