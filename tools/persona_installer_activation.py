"""Installer activation/verification helpers shared with the host executor (T-5516).

The unit-fire budget, timer-enablement link proof, stale-unit-failure-reset
classification and Claude MCP verdict helpers moved verbatim out of
``tools/persona_installer.py`` (ORG-052 \u00a71/\u00a73, T-2652) so
``tools.host_control.executor`` reuses them with installer parity without
importing the installer and its agent-facing Telegram/mailbox/harness closure.

Closed module set: the standard library, ``tools.persona_installer_contracts``
and ``tools.user_systemd`` only, including function-level imports (pinned by
``tools/test_host_control_import_closure.py``). ``tools.persona_installer``
re-exports every name.

The runtime budget values below are TRANSCRIBED from ``tools.runtime_controller``
(importing it would load the mailbox client and harness adapter into the
executor); a test pins them equal.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from tools.persona_installer_contracts import (
    InstallOperation,
    ManifestError,
    OperationFailed,
    _canonical_json,
    _sha256_bytes,
)
from tools import user_systemd as _us
from tools.user_systemd import ReadPurpose, UnitSnapshot

# Transcribed from tools.runtime_controller (T-5516): importing that module would
# load the mailbox client and harness adapter into the executor. A test pins it.
RUNTIME_START_TIMEOUT_SECONDS = 8.0
RUNTIME_INPUT_READY_TIMEOUT_SECONDS = 20.0
PROVIDER_COMPLETION_TIMEOUT_SECONDS = 90.0
POST_START_TIMEOUT_SECONDS = 5.0
POST_START_ASSESSMENT_CAPTURE_COUNT = 3
POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS = 0.25

MANAGED_MARKER = "Managed by pantheon persona installer"
RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS = 60
MAX_UNIT_FIRE_TIMEOUT_SECONDS = 1799
UNIT_FIRE_TIMEOUT_FORMULA = (
    "candidates*(runtime_start+input_ready+provider_completion+"
    "post_start_count*post_start+post_start_assessment)+teardown_margin"
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


def _runtime_budget_constants() -> dict[str, float]:
    return {
        "runtime_start": float(RUNTIME_START_TIMEOUT_SECONDS),
        "input_ready": float(RUNTIME_INPUT_READY_TIMEOUT_SECONDS),
        "provider_completion": float(PROVIDER_COMPLETION_TIMEOUT_SECONDS),
        "post_start": float(POST_START_TIMEOUT_SECONDS),
        "post_start_assessment": (
            float(POST_START_ASSESSMENT_CAPTURE_COUNT)
            * float(POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS)
        ),
    }


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
    normalized = os.path.normpath(str(path))
    # "//"-prefixed paths survive normpath unchanged (POSIX) but name the same
    # file as "/"; refused so no lexical compare can split them (T-4963, D32)
    if normalized.startswith("//"):
        raise OperationFailed(f"path has a '//' prefix: {path}")
    return Path(normalized)


def _resolve_timer_enablement_readlink(link_path: Path, actual_readlink: str) -> Path:
    target = Path(actual_readlink)
    if target.is_absolute():
        return _lexically_normalize_absolute(target)
    return _lexically_normalize_absolute(link_path.parent / target)


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


# T-2917 5c.1: the operator-run user-manager path is the `systemctl --user`
# command line, behind the neutral typed port in tools/user_systemd.py. The
# transport-specific names live here, never in the neutral module.
SYSTEMCTL_TRANSPORT = "systemctl"
SYSTEMCTL_SHOW_STDOUT = "systemctl-show-stdout"


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


def _stale_unit_failure_reset_typed(view: StaleUnitFailureResetSnapshot) -> UnitSnapshot:
    return _us.unit_snapshot_from_text_values(
        "",
        ReadPurpose.STALE_RESET,
        {
            "LoadState": view.load_state,
            "ActiveState": view.active_state,
            "SubState": view.sub_state,
            "Result": view.result,
            "ExecMainStatus": view.exec_main_status,
            "InvocationID": view.invocation_id,
            "Job": view.job,
        },
        source=_us.SnapshotSource(
            transport=SYSTEMCTL_TRANSPORT,
            encoding=SYSTEMCTL_SHOW_STDOUT,
            length=view.stdout_length,
            sha256=view.stdout_sha256,
        ),
    )


def _classify_stale_unit_failure_reset_snapshot(
    snapshot: UnitSnapshot | StaleUnitFailureResetSnapshot,
) -> Literal["clean-noop", "reset-required"]:
    """Accepts the typed snapshot or the legacy string view (the type
    ``_parse_stale_unit_failure_reset_show`` returns); the view is converted
    back to typed values losslessly, so both classify identically."""

    if isinstance(snapshot, StaleUnitFailureResetSnapshot):
        snapshot = _stale_unit_failure_reset_typed(snapshot)
    elif not isinstance(snapshot, UnitSnapshot):
        raise TypeError("stale classification needs a UnitSnapshot or StaleUnitFailureResetSnapshot")
    try:
        return _us.classify_stale_reset(snapshot)
    except _us.UnitStateRefused as exc:
        raise OperationFailed(str(exc)) from None


def _stale_unit_failure_reset_snapshot_evidence(
    prefix: str,
    snapshot: UnitSnapshot,
) -> dict[str, str]:
    invocation = snapshot.invocation_id or ""
    invocation_state = "present" if invocation else "empty"
    return {
        f"{prefix}_load_state": snapshot.load_state or "",
        f"{prefix}_active_state": snapshot.active_state or "",
        f"{prefix}_sub_state": snapshot.sub_state or "",
        f"{prefix}_result": snapshot.result or "",
        f"{prefix}_exec_main_status": _us.render_exec_status(snapshot.exec_main_status),
        f"{prefix}_invocation_id_state": invocation_state,
        f"{prefix}_invocation_id_valid": str(_us.valid_invocation_id(invocation)).lower(),
        f"{prefix}_invocation_id_sha256": _unit_fire_invocation_digest(invocation),
        f"{prefix}_job": _us.render_job(snapshot.job_id),
        **_us.snapshot_digest_evidence(prefix, snapshot.source),
    }


def _stale_unit_failure_reset_failure(
    detail: str,
    *,
    pre: UnitSnapshot | None = None,
    post: UnitSnapshot | None = None,
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


def _timer_unit_state_from_snapshot(snapshot: UnitSnapshot) -> TimerUnitState:
    try:
        classification = _us.classify_timer_unit_file(snapshot)
    except _us.UnitStateRefused as exc:
        raise OperationFailed(str(exc)) from None
    return TimerUnitState(
        classification=classification,
        load_state=snapshot.load_state or "",
        unit_file_state=snapshot.unit_file_state or "",
        active_state=snapshot.active_state or "",
        sub_state=snapshot.sub_state or "",
    )


def _snapshot_fingerprint(snapshot: Mapping[str, str]) -> str | None:
    state = snapshot.get("state")
    if state == "absent":
        return None
    if state == "symlink":
        return snapshot.get("fingerprint")
    if state == "directory":
        return "directory"
    return snapshot.get("fingerprint") or state


# T-3211: the legacy-convergence probe's in-flight-service settle window. The
# consume oneshot's longest observed run is ~3s; 15s is ~5x that, giving a
# comfortable margin over transient scheduling jitter. The consume timer's own
# period is ~150s (OnUnitInactiveSec=2min + RandomizedDelaySec=20s), so 15s is
# ~1/10 of that period -- the settle window can never straddle two consecutive
# fires, and the preflight budget stays small.
LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS = 15.0
LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS = 0.75

UNIT_FIRE_POLL_INTERVAL_SECONDS = 0.25


def _unit_fire_invocation_digest(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8")) if value else ""


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
