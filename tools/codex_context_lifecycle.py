#!/usr/bin/env python3
"""Codex context lifecycle primitives for T-2957.

This module is deliberately dark: it reads rollout JSONL and local evidence,
computes typed decisions, writes only explicitly requested local handoff/ledger
files, and never attaches to the Codex app-server, tmux, Telegram, mailbox
consumers, systemd, or any live restart surface.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal


PINNED_CODEX_CLI_VERSION = "0.147.0"
CODEX_CONTEXT_SCHEMA_VERSION = 1
CODEX_CONTEXT_NAMESPACE = "codex-context"
_ATTRIBUTION_TOLERANCE = timedelta(seconds=30)
_FORBIDDEN_FIELD_RE = re.compile(
    r"(base_instructions|prompt|message|content|auth|credential|secret)",
    re.IGNORECASE,
)
_SESSION_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session>.+)\.jsonl$"
)
_TICKET_RE = re.compile(r"^T-\d+$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ROOT_SOURCE_VALUE = "cli"
_ROOT_THREAD_SOURCE = "user"
_TEST_RACE_BARRIER: Any = None
_SUBAGENT_KEYS = frozenset(
    {
        "parent_thread_id",
        "agent_path",
        "agent_nickname",
        "forked_from_id",
        "agent_role",
        "multi_agent_version",
        "subagent_history_start_ordinal",
    }
)


class ThresholdState(Enum):
    OK = "ok"
    WARN = "warn"
    DEFERRED = "deferred"
    RESET_READY = "reset_ready"
    BLOCKED = "blocked"
    FAIL_CLOSED = "fail_closed"


class GateState(Enum):
    CLEAR = "clear"
    NON_INTERRUPTIBLE = "non_interruptible"
    BLOCKED = "blocked"


class DecisionAction(Enum):
    OBSERVE = "observe"
    WARN = "warn"
    WRITE_HANDOFF = "write_handoff"
    RESET_READY = "reset_ready"
    BLOCK_RESET = "block_reset"
    COMPACTION_RECEIPT = "compaction_receipt"


class ReceiptKind(Enum):
    HANDOFF = "handoff"
    COMPACTION = "compaction"
    RESTART_RESUME = "restart_resume"
    REPLAY = "replay"


class ReplayDisposition(Enum):
    COMMITTED = "committed"
    PRIOR_RECEIPT = "prior_receipt"
    REJECTED = "rejected"


class CodexLifecycleError(Exception):
    """Base error for fail-closed lifecycle decisions."""


class RolloutAttributionError(CodexLifecycleError):
    """Rollout source does not prove it is the hinted root Codex session."""


class RolloutMeasurementError(CodexLifecycleError):
    """Rollout token measurement is absent, stale, or malformed."""


class ProductionGateError(CodexLifecycleError):
    """Production transition gate is absent, stale, malformed, or non-clear."""


class HandoffValidationError(CodexLifecycleError):
    """Handoff or handoff receipt is absent, incomplete, or non-durable."""


class RestartCorrelationError(CodexLifecycleError):
    """Restart resume evidence does not correlate."""


@dataclass(frozen=True)
class Thresholds:
    warn_enter: float = 0.70
    warn_exit: float = 0.65
    handoff_enter: float = 0.80
    handoff_exit: float = 0.75
    reset_enter: float = 0.90
    reset_exit: float = 0.85


@dataclass(frozen=True)
class RolloutContextWindow:
    window_id: str


@dataclass(frozen=True)
class RolloutSessionMeta:
    id: str
    session_id: str
    cli_version: str
    context_window: RolloutContextWindow
    cwd: str
    history_mode: str
    model_provider: str
    originator: str
    source: str
    thread_source: str
    timestamp: datetime


@dataclass(frozen=True)
class DaemonHint:
    daemon_instance_id: str
    session_id: str
    rollout_path: Path
    started_at: datetime
    observed_at: datetime
    process_id: int | None = None
    tmux_session: str = "codex-session"
    max_age_seconds: int = 600


@dataclass(frozen=True)
class TokenUsage:
    total_tokens: int
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass(frozen=True)
class CodexContextMeasurement:
    schema_version: int
    measurement_id: str
    source: Path
    source_sha256: str
    daemon_instance_id: str
    session_id: str
    token_ordinal: int
    latest_compaction_ordinal: int | None
    measured_at: datetime
    current_tokens: int
    lifetime_tokens: int
    window_tokens: int
    fill_ratio: float
    cli_version: str
    stale: bool
    valid: bool
    fail_reason: str = ""


@dataclass(frozen=True)
class CodexContextState:
    threshold_state: ThresholdState
    last_measurement_id: str = ""
    last_fill_ratio: float = 0.0
    last_token_ordinal: int = -1
    last_seen_compaction_ordinal: int = -1
    last_processed_compaction_ordinal: int = -1
    handoff_id: str = ""
    reset_resume_idempotency_key: str = ""


@dataclass(frozen=True)
class ActiveStepSnapshot:
    step_id: str
    summary: str
    chat_id: str
    status: Literal["active", "waiting"]
    started_at: datetime
    updated_at: datetime
    note: str = ""


@dataclass(frozen=True)
class ProjectSnapshot:
    ticket_id: str
    worktree: Path
    branch: str
    commit: str


@dataclass(frozen=True)
class PersonaSnapshot:
    persona: str
    account: str
    harness: Literal["codex"]
    source: str


@dataclass(frozen=True)
class SubagentSnapshot:
    task_name: str
    agent_path: str
    state: Literal["running", "completed", "blocked", "unknown"]
    worktree: Path | None
    mailbox: Path | None
    last_seen_at: datetime | None


@dataclass(frozen=True)
class MailboxOffsetSnapshot:
    mailbox: Path
    cursor_kind: Literal["maildir"]
    new_count: int
    cur_count: int
    last_seen_name: str | None
    digest: str


@dataclass(frozen=True)
class TelegramSpoolEntry:
    update_id: int
    path: Path
    wrapped_sha256: str
    enqueued_at: datetime | None


@dataclass(frozen=True)
class TelegramSnapshot:
    offset: int | None
    pending: tuple[TelegramSpoolEntry, ...]


@dataclass(frozen=True)
class CodexProductionGate:
    schema_version: int
    gate_id: str
    operation_id: str
    state: GateState
    authority: str
    source_identity: str
    updated_at: datetime
    expiry: datetime
    integrity: str
    version: int


@dataclass(frozen=True)
class CodexContextHandoff:
    schema_version: int
    handoff_id: str
    created_at: datetime
    daemon_instance_id: str
    session_id: str
    measurement_id: str
    current_tokens: int
    window_tokens: int
    threshold_state: ThresholdState
    active_step: ActiveStepSnapshot | None
    project: ProjectSnapshot
    persona: PersonaSnapshot
    subagents: tuple[SubagentSnapshot, ...]
    mailbox_offsets: tuple[MailboxOffsetSnapshot, ...]
    telegram: TelegramSnapshot
    kick_id: str
    resume_idempotency_key: str
    complete: bool


@dataclass(frozen=True)
class HandoffReceipt:
    schema_version: int
    handoff_id: str
    measurement_id: str
    daemon_instance_id: str
    path: Path
    bytes_written: int
    sha256: str
    durable: bool
    validated: bool


@dataclass(frozen=True)
class DurableHandoffEvidence:
    path: Path
    receipt: HandoffReceipt
    expected_sha256: str
    expected_handoff_id: str
    expected_measurement_id: str
    expected_daemon_instance_id: str


@dataclass(frozen=True)
class ResetGates:
    production_gate: CodexProductionGate | None
    handoff_evidence: DurableHandoffEvidence | None
    automatic_reset_enabled: bool = False


@dataclass(frozen=True)
class CodexContextDecision:
    prior_state: ThresholdState
    next_state: ThresholdState
    action: DecisionAction
    reset_ready: bool
    should_write_handoff: bool
    receipt: ReceiptKind | None
    reason: str
    measurement_id: str
    compaction_ordinal: int | None = None


@dataclass(frozen=True)
class DaemonInstance:
    daemon_instance_id: str
    session_id: str
    process_id: int | None
    started_at: datetime
    account: str = "codex"
    tmux_session: str = "codex-session"


@dataclass(frozen=True)
class KickReceipt:
    kick_id: str
    daemon_instance_id_before: str
    target_session_id: str
    delivered_at: datetime
    resume_idempotency_key: str


@dataclass(frozen=True)
class RestartResumeReceipt:
    schema_version: int
    receipt_id: str
    before_daemon_instance_id: str
    after_daemon_instance_id: str
    handoff_id: str
    kick_id: str
    resume_idempotency_key: str
    mailbox_offsets: tuple[MailboxOffsetSnapshot, ...]
    telegram_offset: int | None
    resumed: bool
    reason: str


@dataclass(frozen=True)
class ResumeLedgerEntry:
    schema_version: int
    resume_idempotency_key: str
    receipt: RestartResumeReceipt
    handoff_sha256: str
    committed_at: datetime


@dataclass(frozen=True)
class ReplayResult:
    disposition: ReplayDisposition
    receipt: RestartResumeReceipt | None
    reason: str


def _parse_dt(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dt_json(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _one_line(value: Any, *, field: str, max_len: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be empty")
    if "\n" in stripped or "\r" in stripped:
        raise ValueError(f"{field} must be one line")
    if len(stripped) > max_len:
        raise ValueError(f"{field} exceeds {max_len} characters")
    return stripped


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _path(value: Any, *, field: str, absolute: bool = False) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{field} must be a path")
    path = Path(value)
    if absolute and not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return path


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else value


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return _dt_json(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, tuple):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    return obj


def _canonical_json_bytes(obj: Any) -> bytes:
    return (
        json.dumps(_to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _contains_forbidden_key(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _FORBIDDEN_FIELD_RE.search(str(key)):
                return True
            if _contains_forbidden_key(value):
                return True
    elif isinstance(obj, list):
        return any(_contains_forbidden_key(v) for v in obj)
    return False


def rollout_filename_session_id(source: Path) -> str:
    match = _SESSION_FILENAME_RE.match(source.name)
    if not match:
        raise RolloutAttributionError("rollout filename does not match Codex rollout pattern")
    return match.group("session")


def parse_rollout_session_meta(
    record: dict, *, source: Path, daemon_hint: DaemonHint
) -> RolloutSessionMeta:
    if not isinstance(record, dict):
        raise RolloutAttributionError("session metadata record is malformed")
    if record.get("ordinal") != 0 or record.get("type") != "session_meta":
        raise RolloutAttributionError("ordinal zero must be top-level session_meta")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise RolloutAttributionError("session metadata payload is malformed")
    for key in (
        "id",
        "session_id",
        "cli_version",
        "context_window",
        "cwd",
        "history_mode",
        "model_provider",
        "originator",
        "source",
        "thread_source",
        "timestamp",
    ):
        if key not in payload:
            raise RolloutAttributionError(f"session metadata missing {key}")
    context_window = payload.get("context_window")
    if not isinstance(context_window, dict):
        raise RolloutAttributionError("session metadata context_window must be an object")
    window_id = _one_line(context_window.get("window_id"), field="context_window.window_id")
    try:
        meta = RolloutSessionMeta(
            id=_one_line(payload.get("id"), field="id", max_len=128),
            session_id=_one_line(payload.get("session_id"), field="session_id", max_len=128),
            cli_version=_one_line(payload.get("cli_version"), field="cli_version", max_len=32),
            context_window=RolloutContextWindow(window_id=window_id),
            cwd=_one_line(payload.get("cwd"), field="cwd", max_len=1000),
            history_mode=_one_line(payload.get("history_mode"), field="history_mode", max_len=64),
            model_provider=_one_line(payload.get("model_provider"), field="model_provider", max_len=64),
            originator=_one_line(payload.get("originator"), field="originator", max_len=64),
            source=_one_line(payload.get("source"), field="source", max_len=64),
            thread_source=_one_line(payload.get("thread_source"), field="thread_source", max_len=64),
            timestamp=_parse_dt(payload.get("timestamp"), field="timestamp"),
        )
    except ValueError as exc:
        raise RolloutAttributionError("session metadata is malformed") from exc
    validate_rollout_attribution(meta, source, daemon_hint, payload=payload)
    return meta


def validate_rollout_attribution(
    meta: RolloutSessionMeta,
    source: Path,
    daemon_hint: DaemonHint,
    *,
    payload: dict | None = None,
) -> None:
    if source.resolve() != daemon_hint.rollout_path.resolve():
        raise RolloutAttributionError("rollout path does not match daemon hint")
    filename_session = rollout_filename_session_id(source)
    if meta.id != meta.session_id or meta.id != filename_session:
        raise RolloutAttributionError("rollout session id does not match filename")
    if meta.session_id != daemon_hint.session_id:
        raise RolloutAttributionError("rollout session id does not match daemon hint")
    if meta.cli_version != PINNED_CODEX_CLI_VERSION:
        raise RolloutAttributionError("unsupported Codex CLI version")
    if meta.history_mode != "paginated":
        raise RolloutAttributionError("unexpected root history mode")
    if meta.model_provider != "openai":
        raise RolloutAttributionError("unexpected model provider")
    if meta.originator != "codex-tui":
        raise RolloutAttributionError("unexpected originator")
    if meta.source != _ROOT_SOURCE_VALUE:
        raise RolloutAttributionError("unexpected root source")
    if meta.thread_source != _ROOT_THREAD_SOURCE:
        raise RolloutAttributionError("unexpected root thread source")
    if payload and any(key in payload for key in _SUBAGENT_KEYS):
        raise RolloutAttributionError("subagent metadata cannot be used as root rollout")
    lower = daemon_hint.started_at.astimezone(timezone.utc) - _ATTRIBUTION_TOLERANCE
    upper = daemon_hint.observed_at.astimezone(timezone.utc) + _ATTRIBUTION_TOLERANCE
    if meta.timestamp < lower or meta.timestamp > upper:
        raise RolloutAttributionError("rollout metadata timestamp is outside daemon window")


def _token_usage(data: dict, *, field: str) -> TokenUsage:
    if not isinstance(data, dict):
        raise RolloutMeasurementError(f"{field} must be an object")
    return TokenUsage(
        total_tokens=_non_negative_int(data.get("total_tokens"), field=f"{field}.total_tokens"),
        input_tokens=int(data.get("input_tokens", 0) or 0),
        cached_input_tokens=int(data.get("cached_input_tokens", 0) or 0),
        cache_write_input_tokens=int(data.get("cache_write_input_tokens", 0) or 0),
        output_tokens=int(data.get("output_tokens", 0) or 0),
        reasoning_output_tokens=int(data.get("reasoning_output_tokens", 0) or 0),
    )


def _iter_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RolloutMeasurementError("rollout source is unreadable") from exc
    records: list[dict] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RolloutMeasurementError(f"rollout line {line_no} is malformed JSON") from exc
        if not isinstance(record, dict):
            raise RolloutMeasurementError(f"rollout line {line_no} is not an object")
        records.append(record)
    if not records:
        raise RolloutMeasurementError("rollout source has no records")
    return records


def read_codex_rollout_measurement(
    source: Path, *, daemon_hint: DaemonHint
) -> CodexContextMeasurement:
    records = _iter_jsonl(source)
    meta = parse_rollout_session_meta(records[0], source=source, daemon_hint=daemon_hint)
    latest_token: tuple[int, datetime, TokenUsage, TokenUsage, int] | None = None
    latest_compaction: int | None = None
    for record in records:
        ordinal = record.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            continue
        if record.get("type") == "compacted":
            latest_compaction = ordinal if latest_compaction is None else max(latest_compaction, ordinal)
            continue
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            raise RolloutMeasurementError("token_count info is malformed")
        last = _token_usage(info.get("last_token_usage"), field="last_token_usage")
        total = _token_usage(info.get("total_token_usage"), field="total_token_usage")
        window = _positive_int(info.get("model_context_window"), field="model_context_window")
        if total.total_tokens < last.total_tokens:
            raise RolloutMeasurementError("lifetime token total is below current token total")
        measured_at = _parse_dt(record.get("timestamp"), field="token_count.timestamp")
        latest_token = (ordinal, measured_at, last, total, window)
    if latest_token is None:
        raise RolloutMeasurementError("rollout has no token_count measurement")
    ordinal, measured_at, last, total, window = latest_token
    age = daemon_hint.observed_at.astimezone(timezone.utc) - measured_at
    stale = age.total_seconds() > daemon_hint.max_age_seconds or age < -_ATTRIBUTION_TOLERANCE
    if stale:
        raise RolloutMeasurementError("token_count measurement is stale")
    raw = source.read_bytes()
    measurement_id = _sha256_bytes(
        f"{daemon_hint.daemon_instance_id}:{meta.session_id}:{ordinal}:{last.total_tokens}:{window}".encode()
    )[:32]
    return CodexContextMeasurement(
        schema_version=CODEX_CONTEXT_SCHEMA_VERSION,
        measurement_id=measurement_id,
        source=source,
        source_sha256=_sha256_bytes(raw),
        daemon_instance_id=daemon_hint.daemon_instance_id,
        session_id=meta.session_id,
        token_ordinal=ordinal,
        latest_compaction_ordinal=latest_compaction,
        measured_at=measured_at,
        current_tokens=last.total_tokens,
        lifetime_tokens=total.total_tokens,
        window_tokens=window,
        fill_ratio=last.total_tokens / window,
        cli_version=meta.cli_version,
        stale=False,
        valid=True,
    )


def read_codex_production_gate(path: Path, *, now: datetime | None = None) -> CodexProductionGate:
    try:
        data = _read_json(path)
    except Exception as exc:
        raise ProductionGateError("production gate is missing or unreadable") from exc
    if not isinstance(data, dict):
        raise ProductionGateError("production gate is malformed")
    try:
        schema_version = _positive_int(data.get("schema_version"), field="schema_version")
        state = GateState(_one_line(data.get("state"), field="state"))
        gate = CodexProductionGate(
            schema_version=schema_version,
            gate_id=_one_line(data.get("gate_id"), field="gate_id", max_len=128),
            operation_id=_one_line(data.get("operation_id"), field="operation_id", max_len=128),
            state=state,
            authority=_one_line(data.get("authority"), field="authority", max_len=128),
            source_identity=_one_line(data.get("source_identity"), field="source_identity", max_len=256),
            updated_at=_parse_dt(data.get("updated_at"), field="updated_at"),
            expiry=_parse_dt(data.get("expiry"), field="expiry"),
            integrity=_one_line(data.get("integrity"), field="integrity", max_len=128),
            version=_positive_int(data.get("version"), field="version"),
        )
    except ValueError as exc:
        raise ProductionGateError("production gate is malformed") from exc
    if gate.schema_version != CODEX_CONTEXT_SCHEMA_VERSION:
        raise ProductionGateError("unsupported production gate schema")
    check_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if gate.expiry <= check_time:
        raise ProductionGateError("production gate is expired")
    if gate.updated_at > check_time + _ATTRIBUTION_TOLERANCE:
        raise ProductionGateError("production gate timestamp is in the future")
    return gate


def _validate_production_clear(gate: CodexProductionGate | None) -> None:
    if gate is None:
        raise ProductionGateError("production gate missing")
    if gate.state is not GateState.CLEAR:
        raise ProductionGateError("production gate is not clear")


def _validate_active_step(step: ActiveStepSnapshot | None) -> None:
    if step is None:
        return
    _one_line(step.step_id, field="active_step.step_id", max_len=128)
    _one_line(step.summary, field="active_step.summary", max_len=500)
    chat_id = _one_line(step.chat_id, field="active_step.chat_id", max_len=64)
    if not chat_id.lstrip("-").isdigit():
        raise HandoffValidationError("active_step.chat_id must be numeric")
    if step.status not in ("active", "waiting"):
        raise HandoffValidationError("active_step.status is invalid")
    _parse_dt(_dt_json(step.started_at), field="active_step.started_at")
    _parse_dt(_dt_json(step.updated_at), field="active_step.updated_at")
    if "\n" in step.note or "\r" in step.note:
        raise HandoffValidationError("active_step.note must be one line")


def _validate_handoff_object(handoff: CodexContextHandoff) -> None:
    if handoff.schema_version != CODEX_CONTEXT_SCHEMA_VERSION:
        raise HandoffValidationError("unsupported handoff schema")
    if not handoff.complete:
        raise HandoffValidationError("handoff is incomplete")
    _one_line(handoff.handoff_id, field="handoff_id", max_len=128)
    _one_line(handoff.daemon_instance_id, field="daemon_instance_id", max_len=128)
    _one_line(handoff.session_id, field="session_id", max_len=128)
    _one_line(handoff.measurement_id, field="measurement_id", max_len=128)
    if handoff.current_tokens < 0 or handoff.window_tokens <= 0:
        raise HandoffValidationError("handoff token counts are invalid")
    if not _TICKET_RE.fullmatch(handoff.project.ticket_id):
        raise HandoffValidationError("project ticket id is invalid")
    if not handoff.project.worktree.is_absolute():
        raise HandoffValidationError("project worktree must be absolute")
    if not _HEX_RE.fullmatch(handoff.project.commit):
        raise HandoffValidationError("project commit is invalid")
    if handoff.persona.harness != "codex":
        raise HandoffValidationError("persona harness must be codex")
    _one_line(handoff.persona.persona, field="persona.persona")
    _one_line(handoff.persona.account, field="persona.account")
    _one_line(handoff.persona.source, field="persona.source")
    _validate_active_step(handoff.active_step)
    seen_subagents: set[tuple[str, str]] = set()
    for subagent in handoff.subagents:
        key = (subagent.task_name, subagent.agent_path)
        if key in seen_subagents:
            raise HandoffValidationError("duplicate subagent snapshot")
        seen_subagents.add(key)
        _one_line(subagent.task_name, field="subagent.task_name")
        _one_line(subagent.agent_path, field="subagent.agent_path")
        if subagent.state not in ("running", "completed", "blocked", "unknown"):
            raise HandoffValidationError("subagent state is invalid")
        if subagent.worktree is not None and not subagent.worktree.is_absolute():
            raise HandoffValidationError("subagent worktree must be absolute")
        if subagent.mailbox is not None and not subagent.mailbox.is_absolute():
            raise HandoffValidationError("subagent mailbox must be absolute")
    seen_mailboxes: set[Path] = set()
    for mailbox in handoff.mailbox_offsets:
        if mailbox.mailbox in seen_mailboxes:
            raise HandoffValidationError("duplicate mailbox snapshot")
        seen_mailboxes.add(mailbox.mailbox)
        if not mailbox.mailbox.is_absolute():
            raise HandoffValidationError("mailbox path must be absolute")
        if mailbox.cursor_kind != "maildir" or mailbox.new_count < 0 or mailbox.cur_count < 0:
            raise HandoffValidationError("mailbox offset is invalid")
        if not _HEX_RE.fullmatch(mailbox.digest):
            raise HandoffValidationError("mailbox digest is invalid")
    if handoff.telegram.offset is not None and handoff.telegram.offset < 0:
        raise HandoffValidationError("telegram offset is invalid")
    last_update = -1
    seen_updates: set[int] = set()
    for entry in handoff.telegram.pending:
        if entry.update_id in seen_updates or entry.update_id <= last_update:
            raise HandoffValidationError("telegram pending entries must be unique and ordered")
        seen_updates.add(entry.update_id)
        last_update = entry.update_id
        if not entry.path.is_absolute():
            raise HandoffValidationError("telegram spool path must be absolute")
        if not _HEX_RE.fullmatch(entry.wrapped_sha256):
            raise HandoffValidationError("telegram spool digest is invalid")
    payload = _to_jsonable(handoff)
    if _contains_forbidden_key(payload):
        raise HandoffValidationError("handoff contains forbidden field names")


def _active_step_from(data: Any) -> ActiveStepSnapshot | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise HandoffValidationError("active_step is malformed")
    return ActiveStepSnapshot(
        step_id=_one_line(data.get("step_id"), field="active_step.step_id"),
        summary=_one_line(data.get("summary"), field="active_step.summary"),
        chat_id=_one_line(data.get("chat_id"), field="active_step.chat_id"),
        status=data.get("status"),
        started_at=_parse_dt(data.get("started_at"), field="active_step.started_at"),
        updated_at=_parse_dt(data.get("updated_at"), field="active_step.updated_at"),
        note=str(data.get("note", "")),
    )


def _required_json_bool(data: dict[str, Any], key: str, *, error_cls: type[CodexLifecycleError]) -> bool:
    if key not in data or type(data[key]) is not bool:
        raise error_cls(f"{key} must be a JSON boolean")
    return data[key]


def _handoff_from_dict(data: Any) -> CodexContextHandoff:
    if not isinstance(data, dict):
        raise HandoffValidationError("handoff is malformed")
    try:
        project = data["project"]
        persona = data["persona"]
        telegram = data["telegram"]
        complete = _required_json_bool(data, "complete", error_cls=HandoffValidationError)
        handoff = CodexContextHandoff(
            schema_version=_positive_int(data.get("schema_version"), field="schema_version"),
            handoff_id=_one_line(data.get("handoff_id"), field="handoff_id"),
            created_at=_parse_dt(data.get("created_at"), field="created_at"),
            daemon_instance_id=_one_line(data.get("daemon_instance_id"), field="daemon_instance_id"),
            session_id=_one_line(data.get("session_id"), field="session_id"),
            measurement_id=_one_line(data.get("measurement_id"), field="measurement_id"),
            current_tokens=_non_negative_int(data.get("current_tokens"), field="current_tokens"),
            window_tokens=_positive_int(data.get("window_tokens"), field="window_tokens"),
            threshold_state=ThresholdState(_one_line(data.get("threshold_state"), field="threshold_state")),
            active_step=_active_step_from(data.get("active_step")),
            project=ProjectSnapshot(
                ticket_id=_one_line(project.get("ticket_id"), field="project.ticket_id"),
                worktree=_path(project.get("worktree"), field="project.worktree"),
                branch=_one_line(project.get("branch"), field="project.branch"),
                commit=_one_line(project.get("commit"), field="project.commit"),
            ),
            persona=PersonaSnapshot(
                persona=_one_line(persona.get("persona"), field="persona.persona"),
                account=_one_line(persona.get("account"), field="persona.account"),
                harness=persona.get("harness"),
                source=_one_line(persona.get("source"), field="persona.source"),
            ),
            subagents=tuple(
                SubagentSnapshot(
                    task_name=_one_line(item.get("task_name"), field="subagent.task_name"),
                    agent_path=_one_line(item.get("agent_path"), field="subagent.agent_path"),
                    state=item.get("state"),
                    worktree=(
                        _path(item.get("worktree"), field="subagent.worktree")
                        if item.get("worktree") is not None
                        else None
                    ),
                    mailbox=(
                        _path(item.get("mailbox"), field="subagent.mailbox")
                        if item.get("mailbox") is not None
                        else None
                    ),
                    last_seen_at=(
                        _parse_dt(item.get("last_seen_at"), field="subagent.last_seen_at")
                        if item.get("last_seen_at") is not None
                        else None
                    ),
                )
                for item in data.get("subagents", [])
            ),
            mailbox_offsets=tuple(
                MailboxOffsetSnapshot(
                    mailbox=_path(item.get("mailbox"), field="mailbox.mailbox"),
                    cursor_kind=item.get("cursor_kind"),
                    new_count=_non_negative_int(item.get("new_count"), field="mailbox.new_count"),
                    cur_count=_non_negative_int(item.get("cur_count"), field="mailbox.cur_count"),
                    last_seen_name=item.get("last_seen_name"),
                    digest=_one_line(item.get("digest"), field="mailbox.digest"),
                )
                for item in data.get("mailbox_offsets", [])
            ),
            telegram=TelegramSnapshot(
                offset=(
                    _non_negative_int(telegram.get("offset"), field="telegram.offset")
                    if telegram.get("offset") is not None
                    else None
                ),
                pending=tuple(
                    TelegramSpoolEntry(
                        update_id=_non_negative_int(item.get("update_id"), field="telegram.update_id"),
                        path=_path(item.get("path"), field="telegram.path"),
                        wrapped_sha256=_one_line(item.get("wrapped_sha256"), field="telegram.wrapped_sha256"),
                        enqueued_at=(
                            _parse_dt(item.get("enqueued_at"), field="telegram.enqueued_at")
                            if item.get("enqueued_at") is not None
                            else None
                        ),
                    )
                    for item in telegram.get("pending", [])
                ),
            ),
            kick_id=_one_line(data.get("kick_id"), field="kick_id"),
            resume_idempotency_key=_one_line(
                data.get("resume_idempotency_key"), field="resume_idempotency_key"
            ),
            complete=complete,
        )
    except (KeyError, AttributeError, ValueError) as exc:
        raise HandoffValidationError("handoff is malformed") from exc
    _validate_handoff_object(handoff)
    return handoff


def validate_complete_handoff(path: Path) -> CodexContextHandoff:
    try:
        data = _read_json(path)
    except Exception as exc:
        raise HandoffValidationError("handoff is missing or unreadable") from exc
    return _handoff_from_dict(data)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: Any) -> tuple[int, str]:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    data = _canonical_json_bytes(payload)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return len(data), _sha256_bytes(data)


def write_atomic_context_handoff(path: Path, handoff: CodexContextHandoff) -> HandoffReceipt:
    _validate_handoff_object(handoff)
    bytes_written, digest = _atomic_write_json(path, handoff)
    validated = validate_complete_handoff(path)
    if validated != handoff or _file_sha256(path) != digest:
        raise HandoffValidationError("durable handoff validation mismatch")
    return HandoffReceipt(
        schema_version=CODEX_CONTEXT_SCHEMA_VERSION,
        handoff_id=handoff.handoff_id,
        measurement_id=handoff.measurement_id,
        daemon_instance_id=handoff.daemon_instance_id,
        path=path,
        bytes_written=bytes_written,
        sha256=digest,
        durable=True,
        validated=True,
    )


def validate_durable_handoff_evidence(evidence: DurableHandoffEvidence) -> CodexContextHandoff:
    if not evidence.receipt.durable or not evidence.receipt.validated:
        raise HandoffValidationError("handoff receipt is not durable and validated")
    if evidence.receipt.path != evidence.path:
        raise HandoffValidationError("handoff receipt path mismatch")
    if evidence.receipt.sha256 != evidence.expected_sha256:
        raise HandoffValidationError("handoff receipt digest mismatch")
    if _file_sha256(evidence.path) != evidence.receipt.sha256:
        raise HandoffValidationError("handoff file digest mismatch")
    handoff = validate_complete_handoff(evidence.path)
    if (
        handoff.handoff_id != evidence.expected_handoff_id
        or handoff.handoff_id != evidence.receipt.handoff_id
    ):
        raise HandoffValidationError("handoff id evidence mismatch")
    if (
        handoff.measurement_id != evidence.expected_measurement_id
        or handoff.measurement_id != evidence.receipt.measurement_id
    ):
        raise HandoffValidationError("measurement id evidence mismatch")
    if (
        handoff.daemon_instance_id != evidence.expected_daemon_instance_id
        or handoff.daemon_instance_id != evidence.receipt.daemon_instance_id
    ):
        raise HandoffValidationError("daemon id evidence mismatch")
    return handoff


def _durable_handoff_ok(gates: ResetGates, measurement: CodexContextMeasurement) -> bool:
    if gates.handoff_evidence is None:
        return False
    try:
        handoff = validate_durable_handoff_evidence(gates.handoff_evidence)
    except HandoffValidationError:
        return False
    return (
        handoff.measurement_id == measurement.measurement_id
        and handoff.daemon_instance_id == measurement.daemon_instance_id
        and handoff.session_id == measurement.session_id
        and handoff.complete
    )


def decide_codex_context_state(
    measurement: CodexContextMeasurement, prior: CodexContextState, gates: ResetGates
) -> CodexContextDecision:
    if not measurement.valid or measurement.stale:
        return CodexContextDecision(
            prior_state=prior.threshold_state,
            next_state=ThresholdState.FAIL_CLOSED,
            action=DecisionAction.BLOCK_RESET,
            reset_ready=False,
            should_write_handoff=False,
            receipt=None,
            reason=measurement.fail_reason or "invalid measurement",
            measurement_id=measurement.measurement_id,
        )
    if (
        measurement.latest_compaction_ordinal is not None
        and measurement.latest_compaction_ordinal > prior.last_processed_compaction_ordinal
    ):
        return CodexContextDecision(
            prior_state=prior.threshold_state,
            next_state=prior.threshold_state,
            action=DecisionAction.COMPACTION_RECEIPT,
            reset_ready=False,
            should_write_handoff=False,
            receipt=ReceiptKind.COMPACTION,
            reason="new compaction event observed",
            measurement_id=measurement.measurement_id,
            compaction_ordinal=measurement.latest_compaction_ordinal,
        )
    thresholds = Thresholds()
    fill = measurement.fill_ratio
    state = prior.threshold_state
    if state is ThresholdState.OK:
        if fill >= thresholds.warn_enter:
            return _decision(prior, ThresholdState.WARN, DecisionAction.WARN, measurement, "warning threshold crossed")
        return _decision(prior, ThresholdState.OK, DecisionAction.OBSERVE, measurement, "below warning threshold")
    if state is ThresholdState.WARN:
        if fill < thresholds.warn_exit:
            return _decision(prior, ThresholdState.OK, DecisionAction.OBSERVE, measurement, "below warning exit")
        if fill >= thresholds.handoff_enter:
            return _decision(
                prior,
                ThresholdState.DEFERRED,
                DecisionAction.WRITE_HANDOFF,
                measurement,
                "handoff threshold crossed",
                should_write_handoff=True,
            )
        return _decision(prior, ThresholdState.WARN, DecisionAction.WARN, measurement, "warning threshold held")
    if state is ThresholdState.RESET_READY and fill < thresholds.reset_exit:
        return _decision(prior, ThresholdState.DEFERRED, DecisionAction.OBSERVE, measurement, "below reset exit")
    if state is ThresholdState.BLOCKED and fill < thresholds.reset_exit:
        return _decision(prior, ThresholdState.DEFERRED, DecisionAction.OBSERVE, measurement, "below reset exit")
    if state is ThresholdState.DEFERRED and fill < thresholds.handoff_exit:
        return _decision(prior, ThresholdState.WARN, DecisionAction.WARN, measurement, "below handoff exit")
    if state in (ThresholdState.DEFERRED, ThresholdState.RESET_READY, ThresholdState.BLOCKED):
        if fill >= thresholds.reset_enter:
            try:
                _validate_production_clear(gates.production_gate)
            except ProductionGateError as exc:
                return _decision(
                    prior,
                    ThresholdState.BLOCKED,
                    DecisionAction.BLOCK_RESET,
                    measurement,
                    str(exc),
                )
            if not _durable_handoff_ok(gates, measurement):
                return _decision(
                    prior,
                    ThresholdState.BLOCKED,
                    DecisionAction.BLOCK_RESET,
                    measurement,
                    "durable handoff evidence missing or invalid",
                )
            return _decision(
                prior,
                ThresholdState.RESET_READY,
                DecisionAction.RESET_READY,
                measurement,
                "reset ready; automatic reset remains disabled",
                reset_ready=True,
            )
        return _decision(
            prior,
            ThresholdState.DEFERRED,
            DecisionAction.WRITE_HANDOFF,
            measurement,
            "handoff state held",
            should_write_handoff=True,
        )
    return _decision(prior, ThresholdState.FAIL_CLOSED, DecisionAction.BLOCK_RESET, measurement, "unknown prior state")


def _decision(
    prior: CodexContextState,
    next_state: ThresholdState,
    action: DecisionAction,
    measurement: CodexContextMeasurement,
    reason: str,
    *,
    reset_ready: bool = False,
    should_write_handoff: bool = False,
) -> CodexContextDecision:
    return CodexContextDecision(
        prior_state=prior.threshold_state,
        next_state=next_state,
        action=action,
        reset_ready=reset_ready,
        should_write_handoff=should_write_handoff,
        receipt=None,
        reason=reason,
        measurement_id=measurement.measurement_id,
    )


def correlate_restart_resume(
    before: DaemonInstance,
    handoff: CodexContextHandoff,
    kick: KickReceipt,
    after: DaemonInstance,
) -> RestartResumeReceipt:
    _validate_handoff_object(handoff)
    if handoff.daemon_instance_id != before.daemon_instance_id:
        raise RestartCorrelationError("handoff does not match before daemon")
    if before.daemon_instance_id == after.daemon_instance_id:
        raise RestartCorrelationError("after daemon must differ from before daemon")
    if kick.daemon_instance_id_before != before.daemon_instance_id:
        raise RestartCorrelationError("kick does not match before daemon")
    if kick.target_session_id != handoff.session_id or kick.target_session_id != before.session_id:
        raise RestartCorrelationError("kick target does not match session")
    if kick.resume_idempotency_key != handoff.resume_idempotency_key:
        raise RestartCorrelationError("kick idempotency key does not match handoff")
    if after.started_at <= before.started_at:
        raise RestartCorrelationError("after daemon must start after before daemon")
    receipt_id = _sha256_bytes(
        f"{before.daemon_instance_id}:{after.daemon_instance_id}:{handoff.handoff_id}:{kick.kick_id}".encode()
    )[:32]
    return RestartResumeReceipt(
        schema_version=CODEX_CONTEXT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        before_daemon_instance_id=before.daemon_instance_id,
        after_daemon_instance_id=after.daemon_instance_id,
        handoff_id=handoff.handoff_id,
        kick_id=kick.kick_id,
        resume_idempotency_key=handoff.resume_idempotency_key,
        mailbox_offsets=handoff.mailbox_offsets,
        telegram_offset=handoff.telegram.offset,
        resumed=True,
        reason="restart resume correlated",
    )


def _resume_receipt_from_dict(data: Any) -> RestartResumeReceipt:
    if not isinstance(data, dict):
        raise RestartCorrelationError("resume receipt is malformed")
    try:
        resumed = _required_json_bool(data, "resumed", error_cls=RestartCorrelationError)
        if resumed is not True:
            raise RestartCorrelationError("resume receipt must be resumed")
        offsets = tuple(
            MailboxOffsetSnapshot(
                mailbox=Path(item["mailbox"]),
                cursor_kind=item["cursor_kind"],
                new_count=item["new_count"],
                cur_count=item["cur_count"],
                last_seen_name=item.get("last_seen_name"),
                digest=item["digest"],
            )
            for item in data.get("mailbox_offsets", [])
        )
        return RestartResumeReceipt(
            schema_version=data["schema_version"],
            receipt_id=data["receipt_id"],
            before_daemon_instance_id=data["before_daemon_instance_id"],
            after_daemon_instance_id=data["after_daemon_instance_id"],
            handoff_id=data["handoff_id"],
            kick_id=data["kick_id"],
            resume_idempotency_key=data["resume_idempotency_key"],
            mailbox_offsets=offsets,
            telegram_offset=data.get("telegram_offset"),
            resumed=resumed,
            reason=data["reason"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RestartCorrelationError("resume receipt is malformed") from exc


def _ledger_entry_from_dict(data: Any) -> ResumeLedgerEntry:
    if not isinstance(data, dict):
        raise RestartCorrelationError("resume ledger is malformed")
    try:
        return ResumeLedgerEntry(
            schema_version=data["schema_version"],
            resume_idempotency_key=data["resume_idempotency_key"],
            receipt=_resume_receipt_from_dict(data["receipt"]),
            handoff_sha256=data["handoff_sha256"],
            committed_at=_parse_dt(data["committed_at"], field="committed_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RestartCorrelationError("resume ledger is malformed") from exc


def _resume_ledger_lock_dir(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _classify_existing_resume_ledger(
    path: Path, entry: ResumeLedgerEntry, handoff_sha: str
) -> ReplayResult | None:
    if not path.exists():
        return None
    prior = _ledger_entry_from_dict(_read_json(path))
    if (
        prior.resume_idempotency_key == entry.resume_idempotency_key
        and prior.receipt == entry.receipt
        and prior.handoff_sha256 == handoff_sha
    ):
        return ReplayResult(
            disposition=ReplayDisposition.PRIOR_RECEIPT,
            receipt=prior.receipt,
            reason="resume already committed",
        )
    return ReplayResult(
        disposition=ReplayDisposition.REJECTED,
        receipt=None,
        reason="resume ledger conflict",
    )


def _acquire_resume_ledger_lock(path: Path, entry: ResumeLedgerEntry) -> Path | None:
    lock_dir = _resume_ledger_lock_dir(path)
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.mkdir(lock_dir, 0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        return None
    owner = {
        "schema_version": CODEX_CONTEXT_SCHEMA_VERSION,
        "resume_idempotency_key": entry.resume_idempotency_key,
        "receipt_id": entry.receipt.receipt_id,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        _atomic_write_json(lock_dir / "owner.json", owner)
    except Exception:
        _release_resume_ledger_lock(lock_dir)
        raise
    return lock_dir


def _release_resume_ledger_lock(lock_dir: Path) -> None:
    try:
        (lock_dir / "owner.json").unlink()
    except FileNotFoundError:
        pass
    try:
        lock_dir.rmdir()
    except (FileNotFoundError, OSError):
        pass


def commit_restart_resume_once(
    path: Path,
    receipt: RestartResumeReceipt,
    handoff: CodexContextHandoff,
    *,
    expected_offsets: tuple[MailboxOffsetSnapshot, ...],
    expected_telegram: TelegramSnapshot,
) -> ReplayResult:
    _validate_handoff_object(handoff)
    if receipt.handoff_id != handoff.handoff_id:
        raise RestartCorrelationError("receipt handoff id mismatch")
    if receipt.resume_idempotency_key != handoff.resume_idempotency_key:
        raise RestartCorrelationError("receipt idempotency mismatch")
    if receipt.before_daemon_instance_id != handoff.daemon_instance_id:
        raise RestartCorrelationError("receipt before daemon mismatch")
    if tuple(receipt.mailbox_offsets) != tuple(expected_offsets):
        raise RestartCorrelationError("receipt mailbox offsets mismatch")
    if receipt.telegram_offset != expected_telegram.offset:
        raise RestartCorrelationError("receipt telegram offset mismatch")
    handoff_sha = _sha256_bytes(_canonical_json_bytes(handoff))
    entry = ResumeLedgerEntry(
        schema_version=CODEX_CONTEXT_SCHEMA_VERSION,
        resume_idempotency_key=receipt.resume_idempotency_key,
        receipt=receipt,
        handoff_sha256=handoff_sha,
        committed_at=datetime.now(timezone.utc),
    )
    prior_result = _classify_existing_resume_ledger(path, entry, handoff_sha)
    if prior_result is not None:
        return prior_result
    barrier = _TEST_RACE_BARRIER
    if barrier is not None:
        barrier.wait(timeout=5)
    lock_dir = _acquire_resume_ledger_lock(path, entry)
    if lock_dir is None:
        prior_result = _classify_existing_resume_ledger(path, entry, handoff_sha)
        if prior_result is not None:
            return prior_result
        return ReplayResult(
            disposition=ReplayDisposition.REJECTED,
            receipt=None,
            reason="resume ledger locked or in progress",
        )
    try:
        prior_result = _classify_existing_resume_ledger(path, entry, handoff_sha)
        if prior_result is not None:
            return prior_result
        _atomic_write_json(path, entry)
    finally:
        _release_resume_ledger_lock(lock_dir)
    return ReplayResult(
        disposition=ReplayDisposition.COMMITTED,
        receipt=receipt,
        reason="resume committed",
    )


def production_gate_path(state_dir: Path, *, namespace: str = CODEX_CONTEXT_NAMESPACE) -> Path:
    return state_dir / namespace / "production-gate.json"


def prior_state_path(state_dir: Path, *, namespace: str = CODEX_CONTEXT_NAMESPACE) -> Path:
    return state_dir / namespace / "prior-state.json"


def resume_ledger_path(state_dir: Path, *, namespace: str = CODEX_CONTEXT_NAMESPACE) -> Path:
    return state_dir / namespace / "restart-resume-ledger.json"


def evaluate_codex_context_lifecycle(
    source: Path,
    prior: CodexContextState,
    gates: ResetGates,
    *,
    daemon_hint: DaemonHint,
) -> CodexContextDecision:
    measurement = read_codex_rollout_measurement(source, daemon_hint=daemon_hint)
    return decide_codex_context_state(measurement, prior, gates)


def sanitize_rollout_to_file(source: Path, dest: Path) -> int:
    """Copy only metadata discriminators, compaction markers, and token usage."""
    records = _iter_jsonl(source)
    out: list[dict] = []
    for idx, record in enumerate(records):
        rtype = record.get("type")
        if idx == 0 and rtype == "session_meta":
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise RolloutAttributionError("session metadata payload is malformed")
            allowed_payload = {
                key: payload[key]
                for key in (
                    "id",
                    "session_id",
                    "cli_version",
                    "context_window",
                    "cwd",
                    "history_mode",
                    "model_provider",
                    "originator",
                    "source",
                    "thread_source",
                    "timestamp",
                )
                if key in payload
            }
            for key in sorted(_SUBAGENT_KEYS):
                if key in payload:
                    allowed_payload[key] = payload[key]
            out.append(
                {
                    "timestamp": record.get("timestamp"),
                    "ordinal": record.get("ordinal"),
                    "type": "session_meta",
                    "payload": allowed_payload,
                }
            )
        elif rtype == "compacted":
            out.append(
                {
                    "timestamp": record.get("timestamp"),
                    "ordinal": record.get("ordinal"),
                    "type": "compacted",
                }
            )
        elif rtype == "event_msg":
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    out.append(
                        {
                            "timestamp": record.get("timestamp"),
                            "ordinal": record.get("ordinal"),
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": info.get("last_token_usage"),
                                    "total_token_usage": info.get("total_token_usage"),
                                    "model_context_window": info.get("model_context_window"),
                                },
                            },
                        }
                    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as handle:
        for record in out:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")
    return len(out)


def _load_prior(path: Path | None) -> CodexContextState:
    if path is None or not path.exists():
        return CodexContextState(threshold_state=ThresholdState.OK)
    data = _read_json(path)
    return CodexContextState(
        threshold_state=ThresholdState(data.get("threshold_state", "ok")),
        last_measurement_id=data.get("last_measurement_id", ""),
        last_fill_ratio=float(data.get("last_fill_ratio", 0.0)),
        last_token_ordinal=int(data.get("last_token_ordinal", -1)),
        last_seen_compaction_ordinal=int(data.get("last_seen_compaction_ordinal", -1)),
        last_processed_compaction_ordinal=int(data.get("last_processed_compaction_ordinal", -1)),
        handoff_id=data.get("handoff_id", ""),
        reset_resume_idempotency_key=data.get("reset_resume_idempotency_key", ""),
    )


def _load_receipt(path: Path) -> HandoffReceipt:
    data = _read_json(path)
    return HandoffReceipt(
        schema_version=data["schema_version"],
        handoff_id=data["handoff_id"],
        measurement_id=data["measurement_id"],
        daemon_instance_id=data["daemon_instance_id"],
        path=Path(data["path"]),
        bytes_written=data["bytes_written"],
        sha256=data["sha256"],
        durable=data["durable"],
        validated=data["validated"],
    )


def _json_print(obj: Any) -> None:
    print(json.dumps(_to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _cmd_inspect(args: argparse.Namespace) -> int:
    source = Path(args.rollout)
    try:
        hint = DaemonHint(
            daemon_instance_id=args.daemon_instance_id,
            session_id=args.session_id,
            rollout_path=source,
            started_at=_parse_dt(args.daemon_started_at, field="daemon_started_at"),
            observed_at=_parse_dt(args.observed_at, field="observed_at"),
            process_id=args.process_id,
            max_age_seconds=args.max_age_seconds,
        )
        measurement = read_codex_rollout_measurement(source, daemon_hint=hint)
        prior = _load_prior(Path(args.prior_state) if args.prior_state else None)
        production_gate = None
        if args.production_gate and Path(args.production_gate).exists():
            production_gate = read_codex_production_gate(Path(args.production_gate), now=hint.observed_at)
        evidence = None
        if args.handoff_path and args.handoff_receipt and Path(args.handoff_receipt).exists():
            receipt = _load_receipt(Path(args.handoff_receipt))
            evidence = DurableHandoffEvidence(
                path=Path(args.handoff_path),
                receipt=receipt,
                expected_sha256=receipt.sha256,
                expected_handoff_id=receipt.handoff_id,
                expected_measurement_id=receipt.measurement_id,
                expected_daemon_instance_id=receipt.daemon_instance_id,
            )
        decision = decide_codex_context_state(
            measurement,
            prior,
            ResetGates(production_gate=production_gate, handoff_evidence=evidence),
        )
    except FileNotFoundError:
        return 10
    except RolloutAttributionError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 11
    except RolloutMeasurementError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 12
    except HandoffValidationError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 13
    except ProductionGateError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 14
    if args.json:
        _json_print(
            {
                "measurement": measurement,
                "decision": decision,
                "reset_ready": decision.reset_ready,
                "paths_checked": {
                    "rollout": source,
                    "prior_state": args.prior_state,
                    "production_gate": args.production_gate,
                    "handoff_path": args.handoff_path,
                    "handoff_receipt": args.handoff_receipt,
                },
            }
        )
    return 0


def _cmd_validate_handoff(args: argparse.Namespace) -> int:
    try:
        handoff = validate_complete_handoff(Path(args.handoff_path))
        receipt = _load_receipt(Path(args.handoff_receipt))
        evidence = DurableHandoffEvidence(
            path=Path(args.handoff_path),
            receipt=receipt,
            expected_sha256=receipt.sha256,
            expected_handoff_id=receipt.handoff_id,
            expected_measurement_id=receipt.measurement_id,
            expected_daemon_instance_id=receipt.daemon_instance_id,
        )
        validate_durable_handoff_evidence(evidence)
    except HandoffValidationError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 13
    if args.json:
        _json_print({"valid": True, "handoff_id": handoff.handoff_id, "receipt": receipt})
    return 0


def _cmd_sanitize_rollout(args: argparse.Namespace) -> int:
    try:
        count = sanitize_rollout_to_file(Path(args.source), Path(args.dest))
    except CodexLifecycleError as exc:
        if args.json:
            _json_print({"error": type(exc).__name__, "detail": str(exc)})
        return 12
    if args.json:
        _json_print({"records_written": count, "dest": Path(args.dest)})
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dark read-only Codex context lifecycle tools.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--rollout", required=True)
    inspect.add_argument("--daemon-instance-id", required=True)
    inspect.add_argument("--session-id", required=True)
    inspect.add_argument("--daemon-started-at", required=True)
    inspect.add_argument("--observed-at", required=True)
    inspect.add_argument("--process-id", type=int, default=None)
    inspect.add_argument("--max-age-seconds", type=int, default=600)
    inspect.add_argument("--state-dir", default=None)
    inspect.add_argument("--prior-state", default=None)
    inspect.add_argument("--production-gate", default=None)
    inspect.add_argument("--handoff-path", default=None)
    inspect.add_argument("--handoff-receipt", default=None)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=_cmd_inspect)

    validate = sub.add_parser("validate-handoff")
    validate.add_argument("--handoff-path", required=True)
    validate.add_argument("--handoff-receipt", required=True)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=_cmd_validate_handoff)

    sanitize = sub.add_parser("sanitize-rollout")
    sanitize.add_argument("--source", required=True)
    sanitize.add_argument("--dest", required=True)
    sanitize.add_argument("--json", action="store_true")
    sanitize.set_defaults(func=_cmd_sanitize_rollout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
