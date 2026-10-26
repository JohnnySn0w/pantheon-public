#!/usr/bin/env python3
"""Tamper-evident live acceptance recorder for T-2382 tests 55-59.

This helper never accepts caller-supplied verdict fields.  Operators identify
live resources and controller/provider artifacts by path, account, unit, and tmux
target; the helper parses their current state at observation time, appends a
hash-chained JSONL trace event, and validates exactly one acceptance leg.

The JSONL chain detects accidental edits and reordering.  It is not an
append-only storage system or a cryptographic signature; the evidence remains
trustworthy because each event is re-observed from the deployed account,
systemd user manager, tmux pane, process table, runtime-controller state, or
maildir boundary named by the event.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import pwd
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NoReturn, Sequence

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mailbox as pantheon_mailbox
from tools import runtime_controller
from tools.bringup_acceptance.recorder import (
    ZERO_HASH,
    _event_hash,
    _jsonable,
    write_json_evidence,
)
from tools.claude_quota_probe import (
    PROBE_SCHEMA as CLAUDE_QUOTA_PROBE_SCHEMA,
    PROMPT as CLAUDE_QUOTA_PROBE_PROMPT,
    probe_session_id as claude_probe_session_id,
    probe_tmux_target as claude_probe_tmux_target,
)
from tools.claude_statusline_capture import SCHEMA as CLAUDE_STATUSLINE_SCHEMA


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class AcceptanceRefused(RuntimeError):
    """Raised when a live leg would be unsafe or under-specified."""


class AcceptanceSkipped(RuntimeError):
    """Raised when a provider/live prerequisite is explicitly unavailable."""


class AcceptanceProbeDeferred(RuntimeError):
    """Requeue a mailbox record without treating an acceptance deferral as poison."""

    mailbox_transient = True


@dataclasses.dataclass(frozen=True)
class LiveLeg:
    name: str
    provider: Literal[
        "codex",
        "claude",
        "same-unit-swap",
        "forced-cap",
        "claude-batch-sdk",
    ]
    required_gates: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class EvidencePath:
    root: Path
    run_id: str
    leg: str


@dataclasses.dataclass(frozen=True)
class MailboxEvidence:
    producer_path: Path | None
    mailbox_new_path: Path
    runner_claim_path: Path
    mailbox_cur_path: Path
    message_id: str
    correlation_id: str | None
    source_sha256: str


@dataclasses.dataclass(frozen=True)
class RuntimeEvidence:
    persona: str
    unit: str
    tmux_target: str
    pane_id: str
    running_source_revision: str
    running_source_hash: str
    executable: str
    quota_source: str
    argv: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ReviewedSource:
    repo_root: Path
    branch: str
    head: str
    reviewed_revision: str
    clean: bool
    source_hashes: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class LegResult:
    leg: str
    status: Literal["passed"]
    reason: str | None
    mailbox: MailboxEvidence | None
    runtime: RuntimeEvidence | None
    evidence_files: tuple[Path, ...]


@dataclasses.dataclass(frozen=True)
class TraceEvent:
    schema: str
    leg: str
    phase: str
    sequence: int
    observed_at_ns: int
    previous_hash: str
    payload: Mapping[str, Any]
    event_hash: str


LEG_CODEX = "test_live_codex_interactive_tmux_quota_and_ack"
LEG_CLAUDE = "test_live_claude_interactive_tmux_quota_and_ack"
LEG_SWAP = "test_live_same_unit_planned_harness_swap_preserves_persona_and_tmux"
LEG_FORCED_CAP = "test_live_forced_cap_requeues_and_bootstraps_from_chronicle_receipt"
LEG_BATCH = "test_live_explicit_claude_batch_sdk_is_nondefault_and_separately_metered"

TRACE_SCHEMA = "pantheon-t2382-live-trace/v2"
SOURCE_HASH_PATHS = runtime_controller.RUNTIME_SOURCE_HASH_PATHS
UNIT_DIRECTIVES = (
    "Environment",
    "ExecStart",
    "Group",
    "Restart",
    "Type",
    "User",
    "WorkingDirectory",
)
MAILBOX_PHASES = ("mailbox_new", "runner_claim", "mailbox_cur")
MAX_QUOTA_AGE = timedelta(minutes=15)
MAX_FUTURE_SKEW = timedelta(minutes=1)
QUOTA_OVERLAY_SCHEMA = "pantheon-quota-acceptance-overlay/v1"
PROBE_LABEL_PREFIX = "t2382"
SYSTEMD_PROPERTIES = (
    "Result",
    "ExecMainStatus",
    "ExecMainCode",
    "MainPID",
    "ExecMainPID",
    "InvocationID",
    "FragmentPath",
    "ExecStart",
    "WorkingDirectory",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "ActiveState",
    "SubState",
)


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run_command(("git", "-C", str(repo_root), *args))


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceRefused(f"path is missing or not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest_hash(source_hashes: Mapping[str, str]) -> str:
    return runtime_controller._source_manifest_hash(source_hashes)


def _hash_files(paths: Sequence[Path]) -> str:
    if not paths:
        raise AcceptanceRefused("source hash requires at least one path")
    digest = hashlib.sha256()
    for path in paths:
        resolved = path.resolve()
        digest.update(str(resolved).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(resolved).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _read_json_mapping(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceRefused(f"{label} path is missing or not regular: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceRefused(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AcceptanceRefused(f"{label} must be a JSON object")
    return value


def _read_jsonl_objects(path: Path, *, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceRefused(f"{label} path is missing or not regular: {path}")
    values: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise AcceptanceRefused(f"{label} line {line_number} is not an object")
            values.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceRefused(f"{label} is not readable JSONL: {path}") from exc
    if not values:
        raise AcceptanceRefused(f"{label} is empty")
    return values


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceRefused(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceRefused(f"{label} timestamp is invalid: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AcceptanceRefused(f"{label} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None:
        raise AcceptanceRefused(f"{label} timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _account_home(account: str) -> Path:
    try:
        record = pwd.getpwnam(account)
    except KeyError as exc:
        raise AcceptanceRefused(f"unknown Unix account: {account}") from exc
    return Path(record.pw_dir).resolve()


def _account_tmux_command(account: str, *args: str) -> tuple[str, ...]:
    current = pwd.getpwuid(os.geteuid()).pw_name
    if account == current:
        return ("tmux", *args)
    return ("runuser", "--user", account, "--", "tmux", *args)


def _proc_text(proc_root: Path, pid: int, name: str) -> str:
    path = proc_root / str(pid) / name
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AcceptanceRefused(f"runtime pid lacks readable {name}: {pid}") from exc


def _proc_cwd(proc_root: Path, pid: int) -> str:
    try:
        return str((proc_root / str(pid) / "cwd").resolve(strict=True))
    except OSError as exc:
        raise AcceptanceRefused(f"runtime pid lacks cwd link: {pid}") from exc


def _proc_boot_time(proc_root: Path) -> datetime:
    try:
        for line in (proc_root / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), tz=timezone.utc)
    except (OSError, ValueError) as exc:
        raise AcceptanceRefused("procfs lacks readable boot time") from exc
    raise AcceptanceRefused("procfs lacks boot time")


def _proc_started_at(proc_root: Path, pid: int) -> datetime:
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        after_comm = raw.rsplit(") ", 1)[1]
        fields = after_comm.split()
        start_ticks = int(fields[19])
    except (OSError, IndexError, ValueError) as exc:
        raise AcceptanceRefused(f"runtime pid lacks process start tick: {pid}") from exc
    ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    return _proc_boot_time(proc_root) + timedelta(
        seconds=start_ticks / ticks_per_second
    )


def _runtime_payload(path: Path) -> dict[str, Any]:
    value = _read_json_mapping(path, label="runtime state")
    required = {
        "schema": "pantheon-runtime-state/v1",
        "persona": str,
        "harness": str,
        "profile": str,
        "executable": str,
        "tmux_target": str,
    }
    for key, expected in required.items():
        observed = value.get(key)
        if isinstance(expected, str):
            if observed != expected:
                raise AcceptanceRefused(f"runtime state {key} mismatch")
        elif not isinstance(observed, expected) or not observed:
            raise AcceptanceRefused(f"runtime state lacks {key}")
    _parse_utc(value.get("started_at"), label="runtime started_at")
    return value


def _transition_payload(path: Path) -> dict[str, Any]:
    value = _read_json_mapping(path, label="transition state")
    if value.get("schema") != "pantheon-runtime-transition/v1":
        raise AcceptanceRefused("transition state schema mismatch")
    for key in ("persona", "trigger", "phase", "outgoing_profile", "incoming_profile"):
        if not isinstance(value.get(key), str) or not value.get(key):
            raise AcceptanceRefused(f"transition state lacks {key}")
    return value


def _mail_record_identity(path: Path) -> tuple[str, str, str]:
    value = _read_json_mapping(path, label="mailbox record")
    message_id = value.get("id")
    payload = value.get("payload")
    correlation_id = payload.get("correlation_id") if isinstance(payload, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise AcceptanceRefused("mailbox record lacks id")
    if not isinstance(correlation_id, str) or not correlation_id:
        raise AcceptanceRefused("mailbox record lacks payload correlation_id")
    return message_id, correlation_id, _sha256_file(path)


def _runtime_evidence_from_payload(
    payload: Mapping[str, Any],
    *,
    reviewed_revision: str,
    deployed: Mapping[str, Any],
    quota_source: str,
) -> RuntimeEvidence:
    state = payload.get("runtime_state")
    if not isinstance(state, Mapping):
        raise AcceptanceRefused("runtime observation lacks controller state")
    argv = payload.get("argv")
    if not isinstance(argv, (tuple, list)) or not argv:
        raise AcceptanceRefused("runtime observation lacks live argv")
    for key in ("persona", "unit", "tmux_target", "pane_id", "executable"):
        if not isinstance(payload.get(key), str) or not payload.get(key):
            raise AcceptanceRefused(f"runtime observation lacks {key}")
    if payload.get("pane_dead") is not False:
        raise AcceptanceRefused("runtime observation does not identify a live pane")
    _require_runtime_scope_evidence(payload)
    running_revision, running_hash = _require_runtime_source_provenance(
        payload,
        deployed=deployed,
        reviewed_revision=reviewed_revision,
    )
    return RuntimeEvidence(
        persona=str(payload["persona"]),
        unit=str(payload["unit"]),
        tmux_target=str(payload["tmux_target"]),
        pane_id=str(payload["pane_id"]),
        running_source_revision=running_revision,
        running_source_hash=running_hash,
        executable=str(payload["executable"]),
        quota_source=quota_source,
        argv=tuple(str(item) for item in argv),
    )


def _require_runtime_scope_evidence(payload: Mapping[str, Any]) -> None:
    state = payload.get("runtime_state")
    if not isinstance(state, Mapping):
        raise AcceptanceRefused("runtime observation lacks controller state")
    scope_unit = state.get("runtime_scope_unit")
    if scope_unit != "pantheon-runtime-tmux.scope":
        raise AcceptanceRefused("runtime state does not name the canonical tmux scope")
    server_pid = payload.get("server_pid")
    server_uid = payload.get("server_process_uid")
    process_uid = payload.get("process_uid")
    if isinstance(server_pid, bool) or not isinstance(server_pid, int) or server_pid <= 0:
        raise AcceptanceRefused("runtime observation lacks a positive tmux server PID")
    if (
        isinstance(server_uid, bool)
        or not isinstance(server_uid, int)
        or server_uid < 0
        or isinstance(process_uid, bool)
        or not isinstance(process_uid, int)
        or process_uid < 0
        or server_uid != process_uid
    ):
        raise AcceptanceRefused("tmux server uid does not match runtime process uid")
    server_cgroup = payload.get("server_cgroup")
    if not isinstance(server_cgroup, (tuple, list)) or not server_cgroup or not all(
        isinstance(line, str) and line for line in server_cgroup
    ):
        raise AcceptanceRefused("runtime observation lacks tmux server cgroup evidence")
    if not any(
        scope_unit in line.partition("::")[2].split("/")
        for line in server_cgroup
    ):
        raise AcceptanceRefused("tmux server cgroup does not match runtime scope")


def _parse_unit_directives(text: str) -> dict[str, tuple[str, ...]]:
    directives: dict[str, list[str]] = {}
    continuation = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if continuation:
            line = continuation + line
            continuation = ""
        if line.endswith("\\"):
            continuation = line[:-1].rstrip() + " "
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in UNIT_DIRECTIVES:
            directives.setdefault(key, []).append(value.strip())
    return {key: tuple(value) for key, value in sorted(directives.items())}


def _read_cmdline(proc_root: Path, pid: int) -> tuple[str, ...]:
    cmdline = proc_root / str(pid) / "cmdline"
    if not cmdline.is_file():
        raise AcceptanceRefused(f"runtime pid lacks cmdline: {pid}")
    raw = cmdline.read_bytes()
    argv = tuple(item.decode("utf-8", "replace") for item in raw.split(b"\0") if item)
    if not argv:
        raise AcceptanceRefused(f"runtime pid has empty argv: {pid}")
    return argv


def _read_executable(proc_root: Path, pid: int) -> str:
    exe = proc_root / str(pid) / "exe"
    try:
        return str(exe.resolve(strict=True))
    except FileNotFoundError as exc:
        raise AcceptanceRefused(f"runtime pid lacks executable link: {pid}") from exc


def known_legs() -> Mapping[str, LiveLeg]:
    return {
        LEG_CODEX: LiveLeg(
            LEG_CODEX,
            "codex",
            ("T2382_LIVE_ACCEPTANCE", "T2382_LIVE_CODEX"),
        ),
        LEG_CLAUDE: LiveLeg(
            LEG_CLAUDE,
            "claude",
            ("T2382_LIVE_ACCEPTANCE", "T2382_LIVE_CLAUDE"),
        ),
        LEG_SWAP: LiveLeg(
            LEG_SWAP,
            "same-unit-swap",
            ("T2382_LIVE_ACCEPTANCE", "T2382_LIVE_SWAP"),
        ),
        LEG_FORCED_CAP: LiveLeg(
            LEG_FORCED_CAP,
            "forced-cap",
            ("T2382_LIVE_ACCEPTANCE", "T2382_LIVE_FORCED_CAP"),
        ),
        LEG_BATCH: LiveLeg(
            LEG_BATCH,
            "claude-batch-sdk",
            ("T2382_LIVE_ACCEPTANCE", "T2382_LIVE_CLAUDE_BATCH_SDK"),
        ),
    }


def parse_leg(name: str) -> LiveLeg:
    try:
        return known_legs()[name]
    except KeyError as exc:
        raise AcceptanceRefused(f"unknown T-2382 live acceptance leg: {name}") from exc


def validate_reviewed_revision(value: str) -> str:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise AcceptanceRefused("reviewed commit must be a full lowercase SHA-1")
    return value


def inspect_reviewed_source(repo_root: Path, reviewed_revision: str) -> ReviewedSource:
    reviewed_revision = validate_reviewed_revision(reviewed_revision)
    repo_root = repo_root.resolve()
    top = _run_git(repo_root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise AcceptanceRefused(top.stderr.strip() or "cannot inspect git top-level")
    observed_top = Path(top.stdout.strip()).resolve()
    if observed_top != repo_root:
        raise AcceptanceRefused(f"repo root mismatch: expected {repo_root}, got {observed_top}")

    branch = _run_git(repo_root, "branch", "--show-current")
    head = _run_git(repo_root, "rev-parse", "--verify", "HEAD")
    status = _run_git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    for result, label in ((branch, "branch"), (head, "HEAD"), (status, "status")):
        if result.returncode != 0:
            raise AcceptanceRefused(result.stderr.strip() or f"cannot inspect {label}")

    source_hashes = {
        relative: _sha256_file(repo_root / relative) for relative in SOURCE_HASH_PATHS
    }
    return ReviewedSource(
        repo_root=repo_root,
        branch=branch.stdout.strip(),
        head=head.stdout.strip(),
        reviewed_revision=reviewed_revision,
        clean=(status.stdout.strip() == ""),
        source_hashes=source_hashes,
    )


def require_clean_reviewed_source(source: ReviewedSource) -> None:
    if source.branch != "agent/T-2382":
        observed = source.branch or "<detached>"
        raise AcceptanceRefused(
            f"source branch mismatch: expected agent/T-2382, observed {observed}"
        )
    if source.head != source.reviewed_revision:
        raise AcceptanceRefused(
            f"source HEAD mismatch: reviewed {source.reviewed_revision}, observed {source.head}"
        )
    if not source.clean:
        raise AcceptanceRefused("source repo is not clean")


def make_evidence_path(root: Path, *, leg: str, run_id: str) -> EvidencePath:
    parse_leg(leg)
    if not run_id or any(char not in "0123456789TZ-" for char in run_id):
        raise AcceptanceRefused("run-id must be a bounded UTC timestamp token")
    evidence_root = (root / run_id / leg).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    return EvidencePath(root=evidence_root, run_id=run_id, leg=leg)


def require_live_gates(leg: LiveLeg, env: Mapping[str, str]) -> None:
    missing = tuple(
        gate for gate in leg.required_gates if env.get(gate) not in {"1", "true", "yes"}
    )
    if missing:
        raise AcceptanceSkipped(
            "missing explicit live gate(s): "
            + ", ".join(missing)
            + f"; justifies-via P1/P4: provider leg {leg.name} is operator-gated"
        )


class TraceRecorder:
    def __init__(self, evidence_root: Path, *, leg: str, run_id: str) -> None:
        evidence = make_evidence_path(evidence_root, leg=leg, run_id=run_id)
        self.leg = leg
        self.run_id = run_id
        self.root = evidence.root
        self.path = self.root / "trace.jsonl"

    def read(self) -> tuple[TraceEvent, ...]:
        if not self.path.exists():
            return ()

        previous_hash = ZERO_HASH
        events: list[TraceEvent] = []
        for expected_sequence, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines()
        ):
            document = json.loads(line)
            if document.get("schema") != TRACE_SCHEMA:
                raise AcceptanceRefused("trace schema mismatch")
            if document.get("leg") != self.leg:
                raise AcceptanceRefused("trace leg mismatch")
            if document.get("sequence") != expected_sequence:
                raise AcceptanceRefused("trace sequence gap or reorder")
            if document.get("previous_hash") != previous_hash:
                raise AcceptanceRefused("trace hash chain is broken")
            if document.get("event_hash") != _event_hash(document):
                raise AcceptanceRefused("trace event hash mismatch")
            previous_hash = str(document["event_hash"])
            events.append(
                TraceEvent(
                    schema=str(document["schema"]),
                    leg=str(document["leg"]),
                    phase=str(document["phase"]),
                    sequence=int(document["sequence"]),
                    observed_at_ns=int(document["observed_at_ns"]),
                    previous_hash=str(document["previous_hash"]),
                    payload=dict(document["payload"]),
                    event_hash=str(document["event_hash"]),
                )
            )
        return tuple(events)

    def append(self, phase: str, payload: Mapping[str, Any]) -> TraceEvent:
        events = self.read()
        previous_hash = events[-1].event_hash if events else ZERO_HASH
        document: dict[str, Any] = {
            "schema": TRACE_SCHEMA,
            "leg": self.leg,
            "phase": phase,
            "sequence": len(events),
            "observed_at_ns": time.time_ns(),
            "previous_hash": previous_hash,
            "payload": _jsonable(payload),
        }
        document["event_hash"] = _event_hash(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self.read()[-1]


def _validate_run_id_token(run_id: str) -> str:
    from tools.harness_adapter.provider_quota import valid_acceptance_run_id

    if not valid_acceptance_run_id(run_id):
        raise AcceptanceRefused("run-id must be a bounded UTC timestamp token")
    return run_id


def _acceptance_overlay_paths(
    *,
    state_dir: Path,
    overlay_path: Path,
    canonical_path: Path,
    run_id: str,
) -> tuple[Path, Path, Path]:
    run_id = _validate_run_id_token(run_id)
    state = state_dir.resolve()
    overlay = overlay_path.resolve()
    canonical = canonical_path.resolve()
    expected_overlay = state / "acceptance" / run_id / "quota-overlay.json"
    expected_canonical = state / "quota" / "claude-statusline.json"
    if overlay != expected_overlay:
        raise AcceptanceRefused("quota overlay path is outside the acceptance run")
    if canonical != expected_canonical:
        raise AcceptanceRefused("canonical Claude quota path does not match persona state")
    if overlay == canonical:
        raise AcceptanceRefused("quota overlay aliases canonical provider state")
    return state, overlay, canonical


def install_quota_overlay(
    *,
    state_dir: Path,
    overlay_path: Path,
    canonical_path: Path,
    run_id: str,
    resets_at: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Install a fixed 5% acceptance stimulus without modifying provider state."""

    _, overlay, canonical = _acceptance_overlay_paths(
        state_dir=state_dir,
        overlay_path=overlay_path,
        canonical_path=canonical_path,
        run_id=run_id,
    )
    if overlay.exists() or overlay.is_symlink():
        raise AcceptanceRefused("quota overlay already exists")
    canonical_hash = _sha256_file(canonical)
    observed_now = _aware_utc(
        now or datetime.now(timezone.utc), label="quota overlay installation"
    )
    reset = _aware_utc(resets_at, label="quota overlay reset")
    if reset <= observed_now:
        raise AcceptanceRefused("acceptance reset must be later than overlay installation")
    envelope = {
        "schema": QUOTA_OVERLAY_SCHEMA,
        "run_id": run_id,
        "created_at": observed_now.isoformat().replace("+00:00", "Z"),
        "canonical_path": str(canonical),
        "canonical_sha256_at_install": canonical_hash,
        "payload": {
            "observed_at": observed_now.isoformat().replace("+00:00", "Z"),
            "limits": {
                "weekly": {
                    "remaining_percent": 5.0,
                    "resets_at": reset.isoformat().replace("+00:00", "Z"),
                }
            },
        },
    }
    overlay.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_evidence(overlay, envelope)
    overlay.chmod(0o600)
    overlay_hash = _sha256_file(overlay)
    return {
        "schema": QUOTA_OVERLAY_SCHEMA,
        "run_id": run_id,
        "overlay_path": overlay,
        "overlay_sha256": overlay_hash,
        "canonical_path": canonical,
        "canonical_sha256_at_install": canonical_hash,
        "installed_at": envelope["created_at"],
        "resets_at": envelope["payload"]["limits"]["weekly"]["resets_at"],
    }


def remove_quota_overlay(
    *,
    state_dir: Path,
    overlay_path: Path,
    canonical_path: Path,
    run_id: str,
    expected_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove only the acceptance overlay whose exact bytes the caller observed."""

    _, overlay, canonical = _acceptance_overlay_paths(
        state_dir=state_dir,
        overlay_path=overlay_path,
        canonical_path=canonical_path,
        run_id=run_id,
    )
    envelope = _read_json_mapping(overlay, label="quota acceptance overlay")
    if envelope.get("schema") != QUOTA_OVERLAY_SCHEMA or envelope.get("run_id") != run_id:
        raise AcceptanceRefused("quota overlay identity mismatch")
    observed_hash = _sha256_file(overlay)
    if observed_hash != expected_sha256:
        raise AcceptanceRefused("quota overlay bytes changed before removal")
    overlay.unlink()
    directory = os.open(overlay.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    removed_at = _aware_utc(
        now or datetime.now(timezone.utc), label="quota overlay removal"
    )
    return {
        "schema": QUOTA_OVERLAY_SCHEMA,
        "run_id": run_id,
        "overlay_path": overlay,
        "overlay_sha256": observed_hash,
        "canonical_path": canonical,
        "canonical_sha256_after_remove": _sha256_file(canonical),
        "removed_at": removed_at.isoformat().replace("+00:00", "Z"),
    }


def _ack_records(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    values: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise AcceptanceRefused("mailbox acknowledgement entry is not an object")
            values.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceRefused("mailbox acknowledgement log is unreadable") from exc
    return tuple(values)


def _probe_gate_paths(gate_path: Path, run_id: str) -> tuple[Path, Path, Path]:
    gate = gate_path.resolve()
    if gate.parent.name != run_id or gate.parent.parent.name != "acceptance":
        raise AcceptanceRefused("probe gate path is outside the acceptance run")
    return gate, gate / "ready.json", gate / "release.json"


def consume_correlated_probe(
    *,
    inbox: Path,
    message_id: str,
    correlation_id: str,
    run_id: str,
    gate_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Claim exactly one acceptance record while deferring unrelated Recovery mail."""

    _validate_run_id_token(run_id)
    if not message_id or "/" in message_id or "\0" in message_id:
        raise AcceptanceRefused("probe message_id must be a maildir leaf")
    if not correlation_id or "/" in correlation_id or "\0" in correlation_id:
        raise AcceptanceRefused("probe correlation_id must be a bounded token")
    if not math.isfinite(timeout_seconds) or not 1.0 <= timeout_seconds <= 120.0:
        raise AcceptanceRefused("probe timeout must be between 1 and 120 seconds")
    mailbox_root = inbox.resolve()
    new_dir = mailbox_root / "new"
    cur_dir = mailbox_root / "cur"
    target = new_dir / message_id
    observed_id, observed_correlation, _ = _mail_record_identity(target)
    if (observed_id, observed_correlation) != (message_id, correlation_id):
        raise AcceptanceRefused("probe target identity mismatch")
    if tuple((mailbox_root / ".inflight").glob(f"*/{message_id}")) or (
        cur_dir / message_id
    ).exists():
        raise AcceptanceRefused("probe target exists outside new before claim")

    gate, ready_path, release_path = _probe_gate_paths(gate_path, run_id)
    gate.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ready_path.exists() or release_path.exists():
        raise AcceptanceRefused("probe gate contains stale markers")
    label = f"{PROBE_LABEL_PREFIX}:{run_id}:codex"
    before_cur = {path.name for path in cur_dir.iterdir() if path.is_file()}
    ack_path = mailbox_root / ".acks.jsonl"
    before_acks = _ack_records(ack_path)
    timed_out = False

    def _handler(record: Mapping[str, Any]) -> None:
        nonlocal timed_out
        payload = record.get("payload")
        record_correlation = (
            payload.get("correlation_id") if isinstance(payload, Mapping) else None
        )
        if record.get("id") != message_id or record_correlation != correlation_id:
            raise AcceptanceProbeDeferred("non-target acceptance record")
        write_json_evidence(
            ready_path,
            {
                "schema": "pantheon-t2382-mailbox-probe/v1",
                "run_id": run_id,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "label": label,
                "pid": os.getpid(),
                "uid": os.getuid(),
            },
        )
        ready_path.chmod(0o600)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if release_path.is_file() and not release_path.is_symlink():
                release = _read_json_mapping(release_path, label="probe release marker")
                if (
                    release.get("run_id") == run_id
                    and release.get("message_id") == message_id
                    and release.get("correlation_id") == correlation_id
                ):
                    return
                raise AcceptanceProbeDeferred("probe release identity mismatch")
            time.sleep(0.05)
        timed_out = True
        raise AcceptanceProbeDeferred("probe gate timed out")

    stats = pantheon_mailbox.consume(mailbox_root, _handler, label=label)
    after_cur = {path.name for path in cur_dir.iterdir() if path.is_file()}
    after_acks = _ack_records(ack_path)
    ack_delta = after_acks[len(before_acks) :]
    errors = tuple(stats.get("errors", ()))
    target_errors = tuple(item for item in errors if item.get("id") == message_id)
    non_target_errors = tuple(item for item in errors if item.get("id") != message_id)

    if timed_out:
        target_claims = tuple((mailbox_root / ".inflight").glob(f"*/{message_id}"))
        if (
            not target_errors
            or not (new_dir / message_id).is_file()
            or (cur_dir / message_id).exists()
            or target_claims
        ):
            raise AcceptanceRefused("timed-out probe did not requeue the target")
        return {
            "success": False,
            "reason": "timeout",
            "message_id": message_id,
            "correlation_id": correlation_id,
            "run_id": run_id,
            "label": label,
            "stats": stats,
        }

    if stats.get("skipped") or stats.get("processed") != [message_id] or target_errors:
        raise AcceptanceRefused("probe did not process exactly the target")
    if stats.get("quarantined") or stats.get("already_acked"):
        raise AcceptanceRefused("probe quarantined or raced a mailbox record")
    for item in non_target_errors:
        other = str(item.get("id", ""))
        if not other or not (new_dir / other).is_file() or (cur_dir / other).exists():
            raise AcceptanceRefused("probe did not requeue a deferred non-target")
        if tuple((mailbox_root / ".inflight").glob(f"*/{other}")):
            raise AcceptanceRefused("deferred non-target remains claimed")
    if after_cur - before_cur != {message_id}:
        raise AcceptanceRefused("probe cur delta is not exactly the target")
    if len(ack_delta) != 1:
        raise AcceptanceRefused("probe acknowledgement delta is not exactly one record")
    ack = ack_delta[0]
    if (
        ack.get("id") != message_id
        or ack.get("label") != label
        or ack.get("pid") != os.getpid()
        or ack.get("uid") != os.getuid()
    ):
        raise AcceptanceRefused("probe acknowledgement attribution mismatch")
    if (new_dir / message_id).exists() or tuple(
        (mailbox_root / ".inflight").glob(f"*/{message_id}")
    ):
        raise AcceptanceRefused("probe target remains queued or claimed after acknowledgement")
    return {
        "success": True,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "run_id": run_id,
        "label": label,
        "ready_path": ready_path,
        "release_path": release_path,
        "ack": ack,
        "stats": stats,
    }


def initialize_trace(recorder: TraceRecorder, *, operator: str, purpose: str) -> TraceEvent:
    return recorder.append(
        "init",
        {
            "operator": operator,
            "purpose": purpose,
            "run_id": recorder.root.parent.name,
            "evidence_root": recorder.root,
            "pid": os.getpid(),
            "cwd": Path.cwd(),
        },
    )


def observe_deployed_source(
    recorder: TraceRecorder,
    *,
    deployed_repo: Path,
    reviewed_revision: str,
    installed_unit: Path,
    source_unit_relative: str,
    unit: str,
    runner: CommandRunner = _run_command,
) -> TraceEvent:
    reviewed_revision = validate_reviewed_revision(reviewed_revision)
    deployed_repo = deployed_repo.resolve()
    top = runner(("git", "-C", str(deployed_repo), "rev-parse", "--show-toplevel"))
    head = runner(("git", "-C", str(deployed_repo), "rev-parse", "--verify", "HEAD"))
    status = runner(
        ("git", "-C", str(deployed_repo), "status", "--porcelain=v1", "--untracked-files=all")
    )
    if top.returncode != 0:
        raise AcceptanceRefused(top.stderr.strip() or "cannot inspect deployed git top-level")
    if Path(top.stdout.strip()).resolve() != deployed_repo:
        raise AcceptanceRefused("deployed repo path is not its git top-level")
    if head.returncode != 0:
        raise AcceptanceRefused(head.stderr.strip() or "cannot inspect deployed HEAD")
    if status.returncode != 0:
        raise AcceptanceRefused(status.stderr.strip() or "cannot inspect deployed status")
    if status.stdout.strip():
        raise AcceptanceRefused("deployed repo is not clean")
    deployed_head = head.stdout.strip()
    if deployed_head != reviewed_revision:
        raise AcceptanceRefused(
            f"deployed HEAD mismatch: reviewed {reviewed_revision}, observed {deployed_head}"
        )

    installed_text = installed_unit.read_text(encoding="utf-8")
    source_unit = (deployed_repo / source_unit_relative).resolve()
    source_text = source_unit.read_text(encoding="utf-8")
    installed_directives = _parse_unit_directives(installed_text)
    source_directives = _parse_unit_directives(source_text)
    if installed_directives != source_directives:
        raise AcceptanceRefused("installed unit semantic directives differ from deployed source")
    installed_hash = _sha256_file(installed_unit)
    source_hash = _sha256_file(source_unit)
    if installed_hash != source_hash:
        raise AcceptanceRefused("installed unit bytes differ from deployed source")

    source_hashes = {
        relative: _sha256_file(deployed_repo / relative) for relative in SOURCE_HASH_PATHS
    }
    return recorder.append(
        "deployed_source",
        {
            "deployed_repo": deployed_repo,
            "deployed_head": deployed_head,
            "deployed_source_hash": _source_manifest_hash(source_hashes),
            "source_hashes": source_hashes,
            "deployed_clean": True,
            "installed_unit": installed_unit.resolve(),
            "installed_unit_sha256": installed_hash,
            "source_unit": source_unit,
            "source_unit_sha256": source_hash,
            "unit_directives": installed_directives,
            "unit": unit,
        },
    )


def observe_service_status(
    recorder: TraceRecorder,
    *,
    account: str,
    unit: str,
    deployed_repo: Path,
    installed_unit: Path,
    persona: str,
    runner: CommandRunner = _run_command,
) -> TraceEvent:
    command: list[str] = [
        "systemctl", "--user", f"--machine={account}@.host", "show", unit
    ]
    command.extend(f"--property={name}" for name in SYSTEMD_PROPERTIES)
    command.append("--no-pager")
    result = runner(tuple(command))
    if result.returncode != 0:
        raise AcceptanceRefused(result.stderr.strip() or f"cannot inspect systemd unit {unit}")
    values = _parse_key_value_lines(result.stdout)
    missing = [name for name in SYSTEMD_PROPERTIES if name not in values]
    if missing:
        raise AcceptanceRefused("systemd observation lacks: " + ", ".join(missing))
    deployed_repo = deployed_repo.resolve()
    installed_unit = installed_unit.resolve()
    if Path(values["FragmentPath"]).resolve() != installed_unit:
        raise AcceptanceRefused("systemd FragmentPath is not the installed reviewed unit")
    if Path(values["WorkingDirectory"]).resolve() != deployed_repo:
        raise AcceptanceRefused("systemd WorkingDirectory is not the deployed repo")
    entrypoint = deployed_repo / "tools" / "persona_bringup.py"
    if str(entrypoint) not in values["ExecStart"] or f"--persona {persona}" not in values["ExecStart"]:
        raise AcceptanceRefused("systemd ExecStart is not bound to deployed persona controller")
    return recorder.append(
        "service_status",
        {
            "account": account,
            "unit": unit,
            **values,
        },
    )


def observe_runtime_state(
    recorder: TraceRecorder,
    *,
    account: str,
    provider: str,
    persona: str,
    unit: str,
    tmux_target: str,
    runtime_state_path: Path,
    proc_root: Path = Path("/proc"),
    runner: CommandRunner = _run_command,
) -> TraceEvent:
    tmux = runner(
        _account_tmux_command(
            account,
            "display-message", "-p", "-t", tmux_target,
            "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}",
        )
    )
    if tmux.returncode != 0:
        raise AcceptanceRefused(tmux.stderr.strip() or f"cannot inspect tmux target {tmux_target}")
    fields = tmux.stdout.rstrip("\n").split("\t")
    if len(fields) != 4 or not fields[0] or not fields[1].isdigit():
        raise AcceptanceRefused("tmux observation did not identify one live pane")
    pane_id, pid_raw, pane_dead, pane_command = fields
    if pane_dead != "0":
        raise AcceptanceRefused("tmux pane is dead")
    pid = int(pid_raw)
    account_uid = pwd.getpwnam(account).pw_uid
    try:
        process_uid = (proc_root / str(pid)).stat().st_uid
    except OSError as exc:
        raise AcceptanceRefused(f"runtime pid is not inspectable: {pid}") from exc
    if process_uid != account_uid:
        raise AcceptanceRefused("tmux pane process is not owned by the bound account")
    runtime = _runtime_payload(runtime_state_path)
    if runtime["persona"] != persona or runtime["harness"] != provider:
        raise AcceptanceRefused("runtime state persona/provider mismatch")
    if runtime["tmux_target"] != tmux_target:
        raise AcceptanceRefused("runtime state tmux target mismatch")
    scope_unit = runtime.get("runtime_scope_unit")
    if scope_unit != "pantheon-runtime-tmux.scope":
        raise AcceptanceRefused("runtime state does not name the canonical tmux scope")
    server = runner(
        _account_tmux_command(
            account,
            "display-message", "-p", "-t", tmux_target, "#{pid}",
        )
    )
    server_pid_raw = server.stdout.strip()
    if server.returncode != 0 or not server_pid_raw.isdigit():
        raise AcceptanceRefused("tmux observation did not identify the server PID")
    server_pid = int(server_pid_raw)
    try:
        server_uid = (proc_root / str(server_pid)).stat().st_uid
    except OSError as exc:
        raise AcceptanceRefused(
            f"tmux server pid is not inspectable: {server_pid}"
        ) from exc
    if server_uid != account_uid:
        raise AcceptanceRefused("tmux server is not owned by the bound account")
    server_cgroup = tuple(
        _proc_text(proc_root, server_pid, "cgroup").splitlines()
    )
    if not any(
        scope_unit in line.partition("::")[2].split("/")
        for line in server_cgroup
    ):
        raise AcceptanceRefused("tmux server cgroup does not match runtime scope")
    argv = _read_cmdline(proc_root, pid)
    executable = _read_executable(proc_root, pid)
    process_started_at = _proc_started_at(proc_root, pid)
    expected = str(Path(str(runtime["executable"])).resolve())
    if expected not in "\0".join(argv) and Path(executable).resolve() != Path(expected):
        raise AcceptanceRefused("live pane process is not the runtime-state executable")
    if provider == "claude":
        mode_positions = [
            index for index, value in enumerate(argv) if value == "--permission-mode"
        ]
        if len(mode_positions) != 1:
            raise AcceptanceRefused(
                "Claude runtime must have exactly one explicit --permission-mode"
            )
        mode_index = mode_positions[0]
        if mode_index + 1 >= len(argv) or argv[mode_index + 1] != "bypassPermissions":
            raise AcceptanceRefused(
                "Claude runtime permission mode is not explicit bypassPermissions"
            )
    return recorder.append(
        "runtime_state",
        {
            "account": account,
            "provider": provider,
            "persona": persona,
            "unit": unit,
            "tmux_target": tmux_target,
            "pane_id": pane_id,
            "pid": pid,
            "process_uid": process_uid,
            "pane_dead": False,
            "pane_current_command": pane_command,
            "server_pid": server_pid,
            "server_process_uid": server_uid,
            "server_cgroup": server_cgroup,
            "argv": argv,
            "executable": executable,
            "process_started_at": process_started_at.isoformat().replace("+00:00", "Z"),
            "cwd": _proc_cwd(proc_root, pid),
            "cgroup": tuple(_proc_text(proc_root, pid, "cgroup").splitlines()),
            "runtime_state_path": runtime_state_path.resolve(),
            "runtime_state_sha256": _sha256_file(runtime_state_path),
            "runtime_state": runtime,
        },
    )


def observe_quota(
    recorder: TraceRecorder,
    *,
    provider: str,
    quota_source: str,
    profile: str,
    meter: str,
    quota_state_path: Path,
    now: datetime | None = None,
) -> TraceEvent:
    if quota_state_path.is_symlink() or not quota_state_path.is_file():
        raise AcceptanceRefused(f"quota state path is missing or not regular: {quota_state_path}")
    raw = quota_state_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceRefused("quota state is not provider JSON") from exc
    if not isinstance(payload, dict):
        raise AcceptanceRefused("quota state must be a JSON object")
    from tools.harness_adapter.provider_quota import (
        parse_claude_statusline_observation,
        parse_codex_app_server_quota_observation,
    )

    observed_now = _aware_utc(
        now or datetime.now(timezone.utc), label="quota observation"
    )
    overlay_evidence: dict[str, Any] = {}
    if provider == "claude" and quota_source == "statusline":
        snapshot = parse_claude_statusline_observation(
            payload, profile=profile, now=observed_now, max_age=MAX_QUOTA_AGE
        )
    elif provider == "claude" and quota_source == "acceptance-overlay":
        run_id = recorder.root.parent.name
        if payload.get("schema") != QUOTA_OVERLAY_SCHEMA or payload.get("run_id") != run_id:
            raise AcceptanceRefused("quota overlay schema or run identity mismatch")
        inner = payload.get("payload")
        canonical_raw = payload.get("canonical_path")
        canonical_hash = payload.get("canonical_sha256_at_install")
        if not isinstance(inner, Mapping):
            raise AcceptanceRefused("quota overlay lacks provider-shaped payload")
        if not isinstance(canonical_raw, str) or not isinstance(canonical_hash, str):
            raise AcceptanceRefused("quota overlay lacks canonical provenance")
        canonical = Path(canonical_raw)
        if canonical.is_symlink() or not canonical.is_file():
            raise AcceptanceRefused("quota overlay canonical path is not regular")
        current_canonical_hash = _sha256_file(canonical)
        if current_canonical_hash != canonical_hash:
            raise AcceptanceRefused("canonical quota changed after overlay installation")
        if quota_state_path.resolve() == canonical.resolve():
            raise AcceptanceRefused("acceptance overlay aliases canonical quota state")
        if quota_state_path.parent.name != run_id or quota_state_path.parent.parent.name != "acceptance":
            raise AcceptanceRefused("quota overlay is outside the acceptance run")
        overlay_hash = _sha256_bytes(raw.encode("utf-8"))
        overlay_evidence = {
            "fixture": True,
            "fixture_run_id": run_id,
            "underlying_source": "statusline",
            "overlay_sha256": overlay_hash,
            "canonical_path": canonical.resolve(),
            "canonical_sha256_at_install": canonical_hash,
        }
        snapshot = parse_claude_statusline_observation(
            inner,
            profile=profile,
            now=observed_now,
            max_age=MAX_QUOTA_AGE,
            extra_evidence={
                "source": "acceptance-overlay",
                "underlying_source": "statusline",
                "fixture": "true",
                "run_id": run_id,
                "overlay_sha256": overlay_hash,
                "canonical_sha256_at_install": canonical_hash,
            },
        )
    elif provider == "codex" and quota_source == "app-server":
        observed_at = _parse_utc(payload.get("observed_at"), label="Codex quota")
        snapshot = parse_codex_app_server_quota_observation(
            payload,
            profile=profile,
            observed_at=observed_at,
            now=observed_now,
            max_age=MAX_QUOTA_AGE,
        )
    else:
        raise AcceptanceRefused("quota provider/source pair is unsupported")
    if snapshot.freshness != "fresh":
        raise AcceptanceRefused("quota observation is not fresh")
    if snapshot.observed_at > observed_now + MAX_FUTURE_SKEW:
        raise AcceptanceRefused("quota observation is in the future")
    weekly_names = {
        "week", "weekly", "weekly-quota", "weekly_remaining", "weekly-remaining",
        "7d", "7-day", "seven-day",
    }
    weekly = next(
        (
            item for item in snapshot.windows
            if item.name.strip().lower().replace(" ", "-") in weekly_names
        ),
        None,
    )
    if weekly is None:
        raise AcceptanceRefused("quota observation lacks a weekly window")
    return recorder.append(
        "quota",
        {
            "provider": provider,
            "quota_source": quota_source,
            "profile": profile,
            "meter": meter,
            "quota_state_path": quota_state_path.resolve(),
            "quota_state_sha256": _sha256_bytes(raw.encode("utf-8")),
            "remaining_percent": weekly.remaining_percent,
            "fresh_observed_at": snapshot.observed_at.isoformat(),
            "freshness": snapshot.freshness,
            "weekly_resets_at": weekly.resets_at.isoformat(),
            **overlay_evidence,
        },
    )


def observe_quota_overlay(
    recorder: TraceRecorder,
    *,
    phase: Literal["quota_overlay_installed", "quota_overlay_removed"],
    overlay_path: Path,
    canonical_path: Path,
    run_id: str,
) -> TraceEvent:
    if run_id != recorder.root.parent.name:
        raise AcceptanceRefused("quota overlay run does not match trace run")
    overlay = overlay_path.resolve()
    canonical = canonical_path.resolve()
    if overlay == canonical:
        raise AcceptanceRefused("quota overlay aliases canonical state")
    if canonical.is_symlink() or not canonical.is_file():
        raise AcceptanceRefused("canonical quota path is not a regular file")
    if phase == "quota_overlay_installed":
        envelope = _read_json_mapping(overlay, label="quota acceptance overlay")
        if envelope.get("schema") != QUOTA_OVERLAY_SCHEMA or envelope.get("run_id") != run_id:
            raise AcceptanceRefused("quota overlay identity mismatch")
        if Path(str(envelope.get("canonical_path", ""))).resolve() != canonical:
            raise AcceptanceRefused("quota overlay canonical path mismatch")
        canonical_hash = _sha256_file(canonical)
        if envelope.get("canonical_sha256_at_install") != canonical_hash:
            raise AcceptanceRefused("quota overlay canonical hash mismatch")
        payload = {
            "schema": QUOTA_OVERLAY_SCHEMA,
            "run_id": run_id,
            "overlay_path": overlay,
            "overlay_sha256": _sha256_file(overlay),
            "canonical_path": canonical,
            "canonical_sha256_at_install": canonical_hash,
            "created_at": envelope.get("created_at"),
        }
        _parse_utc(payload["created_at"], label="quota overlay created_at")
    elif phase == "quota_overlay_removed":
        if overlay.exists() or overlay.is_symlink():
            raise AcceptanceRefused("quota overlay still exists at removal observation")
        installed = _last(recorder.read(), "quota_overlay_installed").payload
        if installed.get("run_id") != run_id:
            raise AcceptanceRefused("removed overlay run differs from installation")
        if Path(str(installed.get("overlay_path", ""))).resolve() != overlay:
            raise AcceptanceRefused("removed overlay path differs from installation")
        if Path(str(installed.get("canonical_path", ""))).resolve() != canonical:
            raise AcceptanceRefused("removed overlay canonical path differs from installation")
        payload = {
            "schema": QUOTA_OVERLAY_SCHEMA,
            "run_id": run_id,
            "overlay_path": overlay,
            "overlay_sha256": installed.get("overlay_sha256"),
            "canonical_path": canonical,
            "canonical_sha256_after_remove": _sha256_file(canonical),
            "removed": True,
        }
    else:
        raise AcceptanceRefused(f"unsupported quota overlay phase: {phase}")
    return recorder.append(phase, payload)


def observe_primary_quota_probe(
    recorder: TraceRecorder,
    *,
    result_path: Path,
    canonical_path: Path,
) -> TraceEvent:
    """Bind the successful scratch-turn receipt to current provider quota bytes."""

    result = _read_json_mapping(result_path, label="primary quota probe result")
    if result.get("schema") != CLAUDE_QUOTA_PROBE_SCHEMA:
        raise AcceptanceRefused("primary quota probe result schema mismatch")
    if (
        result.get("status") != "observed"
        or result.get("cap_sentinel") is not None
        or result.get("pong_observed") is not True
    ):
        raise AcceptanceRefused("primary quota probe did not produce a healthy observation")
    if result.get("run_id") != recorder.run_id:
        raise AcceptanceRefused("primary quota probe run identity mismatch")
    canonical = canonical_path.resolve()
    if result.get("statusline_path") != str(canonical):
        raise AcceptanceRefused("primary quota probe status-line path mismatch")
    canonical_payload = _read_json_mapping(canonical, label="primary quota observation")
    if canonical_payload.get("schema") != CLAUDE_STATUSLINE_SCHEMA:
        raise AcceptanceRefused("primary quota observation schema mismatch")
    submitted_at = _parse_utc(result.get("submitted_at"), label="primary probe submitted_at")
    completed_at = _parse_utc(result.get("completed_at"), label="primary probe completed_at")
    observed_at = _parse_utc(
        canonical_payload.get("observed_at"), label="primary quota observed_at"
    )
    if not submitted_at <= observed_at <= completed_at:
        raise AcceptanceRefused("primary quota observation is outside the probe turn")
    if result.get("session_id") != canonical_payload.get("session_id"):
        raise AcceptanceRefused("primary quota observation session identity mismatch")
    if result.get("prompt_sha256") != hashlib.sha256(
        CLAUDE_QUOTA_PROBE_PROMPT.encode("utf-8")
    ).hexdigest():
        raise AcceptanceRefused("primary quota probe prompt identity mismatch")
    canonical_hash = _sha256_file(canonical)
    if not canonical_hash or result.get("statusline_sha256") != canonical_hash:
        raise AcceptanceRefused("primary quota probe bytes changed after capture")
    return recorder.append(
        "primary_quota_probe",
        {
            "result_path": result_path.resolve(),
            "result_sha256": _sha256_file(result_path),
            "result": result,
            "canonical_path": canonical,
            "canonical_sha256": canonical_hash,
            "canonical_observed_at": canonical_payload["observed_at"],
        },
    )


def observe_planned_preparing(
    recorder: TraceRecorder,
    *,
    transition_state_path: Path,
) -> TraceEvent:
    transition = _transition_payload(transition_state_path)
    quota = transition.get("quota_evidence")
    if (
        transition.get("trigger") != "planned-quota"
        or transition.get("phase") != "preparing"
        or transition.get("handoff_requested") is not True
    ):
        raise AcceptanceRefused("planned transition is not preparing a requested handoff")
    if not isinstance(quota, Mapping):
        raise AcceptanceRefused("planned transition lacks quota evidence")
    if (
        quota.get("source") != "acceptance-overlay"
        or str(quota.get("fixture", "false")).lower() != "true"
        or quota.get("run_id") != recorder.root.parent.name
    ):
        raise AcceptanceRefused("planned transition is not bound to this acceptance overlay")
    return recorder.append(
        "planned_preparing",
        {
            "transition_state_path": transition_state_path.resolve(),
            "transition_state_sha256": _sha256_file(transition_state_path),
            "transition": transition,
        },
    )


def observe_mailbox_phase(
    recorder: TraceRecorder,
    *,
    phase: Literal["mailbox_new", "runner_claim", "mailbox_cur"],
    mailbox_root: Path,
    message_id: str,
    claim_id: str | None = None,
    correlation_id: str | None = None,
) -> TraceEvent:
    if "/" in message_id or "\0" in message_id or not message_id:
        raise AcceptanceRefused("message_id must be a maildir leaf")
    if not correlation_id or "/" in correlation_id or "\0" in correlation_id:
        raise AcceptanceRefused("correlation_id must be a nonempty bounded token")
    mailbox_root = mailbox_root.resolve()
    new_path = mailbox_root / "new" / message_id
    cur_path = mailbox_root / "cur" / message_id
    claim_root = mailbox_root / ".inflight"
    claim_matches = tuple(sorted(claim_root.glob(f"*/{message_id}"))) if claim_root.exists() else ()

    if phase == "mailbox_new":
        observed_path = new_path
        if not new_path.is_file():
            raise AcceptanceRefused("mailbox new phase missing source message")
        if claim_matches or cur_path.exists():
            raise AcceptanceRefused("mailbox new phase has conflicting claim or cur copy")
    elif phase == "runner_claim":
        if claim_id is None:
            raise AcceptanceRefused("runner_claim phase requires claim_id")
        observed_path = mailbox_root / ".inflight" / claim_id / message_id
        if tuple(path.resolve() for path in claim_matches) != (observed_path.resolve(),):
            raise AcceptanceRefused("runner_claim requires exactly one matching inflight message")
        if new_path.exists() or cur_path.exists():
            raise AcceptanceRefused("runner_claim phase has conflicting new or cur copy")
    elif phase == "mailbox_cur":
        observed_path = cur_path
        if not cur_path.is_file():
            raise AcceptanceRefused("mailbox cur phase missing acknowledgement message")
        if new_path.exists() or claim_matches:
            raise AcceptanceRefused("mailbox cur phase has conflicting new or claim copy")
    else:
        raise AcceptanceRefused(f"unsupported mailbox phase: {phase}")

    return recorder.append(
        phase,
        {
            "mailbox_root": mailbox_root,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "claim_id": claim_id,
            "path": observed_path.resolve(),
            "sha256": _sha256_file(observed_path),
            "new_exists": new_path.exists(),
            "claim_count": len(claim_matches),
            "cur_count": 1 if cur_path.exists() else 0,
        },
    )


def observe_runtime_mailbox_result(
    recorder: TraceRecorder,
    *,
    source_mailbox: Path,
    reply_mailbox: Path,
    message_id: str,
    correlation_id: str,
    account: str,
    persona: str,
    harness: str,
    expected_message: str,
    controller_evidence_path: Path,
) -> TraceEvent:
    """Bind an actual interactive runtime submission to its ack and reply.

    Unlike ``consume_correlated_probe``, this observer never claims or acks mail.
    It runs only after the persona TUI has handled the record through the normal
    wake path, and accepts a result only when the controller submission, source
    ack, and correlated persona reply agree on one identity.
    """

    for label, value in (
        ("message_id", message_id),
        ("correlation_id", correlation_id),
        ("persona", persona),
        ("harness", harness),
    ):
        if not value or "/" in value or "\0" in value:
            raise AcceptanceRefused(f"runtime mailbox {label} is not a bounded token")
    if not expected_message or len(expected_message.encode("utf-8")) > 4096:
        raise AcceptanceRefused("runtime mailbox expected message is empty or oversized")

    source_root = source_mailbox.resolve()
    reply_root = reply_mailbox.resolve()
    source_path = source_root / "cur" / message_id
    observed_id, observed_correlation, source_hash = _mail_record_identity(source_path)
    if (observed_id, observed_correlation) != (message_id, correlation_id):
        raise AcceptanceRefused("runtime mailbox source identity mismatch")
    if (source_root / "new" / message_id).exists() or tuple(
        (source_root / ".inflight").glob(f"*/{message_id}")
    ):
        raise AcceptanceRefused("runtime mailbox source is not durably acknowledged")

    try:
        expected_uid = pwd.getpwnam(account).pw_uid
    except KeyError as exc:
        raise AcceptanceRefused(f"unknown runtime mailbox account: {account}") from exc
    matching_acks = tuple(
        record
        for record in _ack_records(source_root / ".acks.jsonl")
        if record.get("id") == message_id
    )
    if len(matching_acks) != 1:
        raise AcceptanceRefused("runtime mailbox source lacks one attributable acknowledgement")
    ack = matching_acks[0]
    if (
        ack.get("uid") != expected_uid
        or isinstance(ack.get("pid"), bool)
        or not isinstance(ack.get("pid"), int)
        or int(ack["pid"]) <= 0
    ):
        raise AcceptanceRefused("runtime mailbox acknowledgement actor mismatch")

    controller_records = _read_jsonl_objects(
        controller_evidence_path.resolve(), label="runtime mailbox controller evidence"
    )
    accepted = tuple(
        record
        for record in controller_records
        if record.get("event") == "runtime_submission_accepted"
        and record.get("persona") == persona
        and record.get("harness") == harness
        and record.get("message_id") == message_id
        and record.get("correlation_id") == correlation_id
    )
    if len(accepted) != 1:
        raise AcceptanceRefused("runtime mailbox lacks one matching accepted submission")
    accepted_at = _parse_utc(
        accepted[0].get("recorded_at"), label="runtime mailbox submission"
    )
    try:
        ack_at = datetime.fromtimestamp(int(ack["ts_ns"]) / 1_000_000_000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise AcceptanceRefused("runtime mailbox acknowledgement timestamp is invalid") from exc
    if ack_at < accepted_at:
        raise AcceptanceRefused("runtime mailbox acknowledgement predates submission")

    reply_candidates: list[Path] = []
    for folder in ("new", "cur"):
        directory = reply_root / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            value = _read_json_mapping(path, label="runtime mailbox reply")
            payload = value.get("payload")
            if (
                isinstance(payload, Mapping)
                and payload.get("kind") == "persona_result"
                and payload.get("correlation_id") == correlation_id
            ):
                reply_candidates.append(path)
    if len(reply_candidates) != 1:
        raise AcceptanceRefused("runtime mailbox lacks one correlated persona_result")
    reply_path = reply_candidates[0]
    reply = _read_json_mapping(reply_path, label="runtime mailbox reply")
    payload = reply.get("payload")
    assert isinstance(payload, Mapping)
    expected_reply = {
        "persona": persona,
        "harness": harness,
        "status": "completed",
        "message": expected_message,
    }
    if reply.get("sender") != persona or any(
        payload.get(key) != value for key, value in expected_reply.items()
    ):
        raise AcceptanceRefused("runtime mailbox persona_result content mismatch")
    try:
        reply_at = datetime.fromtimestamp(int(reply["ts_ns"]) / 1_000_000_000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise AcceptanceRefused("runtime mailbox reply timestamp is invalid") from exc
    if reply_at < accepted_at:
        raise AcceptanceRefused("runtime mailbox reply predates submission")

    return recorder.append(
        "runtime_mailbox_result",
        {
            "source_mailbox": source_root,
            "reply_mailbox": reply_root,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "account": account,
            "persona": persona,
            "harness": harness,
            "source_path": source_path,
            "source_sha256": source_hash,
            "ack": ack,
            "controller_evidence_path": controller_evidence_path.resolve(),
            "controller_evidence_sha256": _sha256_file(controller_evidence_path.resolve()),
            "accepted_submission": accepted[0],
            "reply_path": reply_path.resolve(),
            "reply_sha256": _sha256_file(reply_path),
            "reply_id": reply.get("id"),
            "expected_message_sha256": hashlib.sha256(
                expected_message.encode("utf-8")
            ).hexdigest(),
        },
    )


def observe_swap_phase(
    recorder: TraceRecorder,
    *,
    phase: Literal["swap_before", "swap_after", "swap_restore"],
    account: str,
    provider: str,
    persona: str,
    unit: str,
    tmux_target: str,
    runtime_state_path: Path,
    transition_state_path: Path | None = None,
    controller_evidence_path: Path | None = None,
    proc_root: Path = Path("/proc"),
    runner: CommandRunner = _run_command,
) -> TraceEvent:
    runtime = observe_runtime_state(
        recorder,
        account=account,
        provider=provider,
        persona=persona,
        unit=unit,
        tmux_target=tmux_target,
        runtime_state_path=runtime_state_path,
        proc_root=proc_root,
        runner=runner,
    ).payload
    payload: dict[str, Any] = {"runtime": runtime}
    if transition_state_path is not None:
        payload["transition_state_path"] = transition_state_path.resolve()
        payload["transition_state_sha256"] = _sha256_file(transition_state_path)
        payload["transition"] = _transition_payload(transition_state_path)
    if controller_evidence_path is not None:
        payload["controller_evidence_path"] = controller_evidence_path.resolve()
        payload["controller_evidence_sha256"] = _sha256_file(controller_evidence_path)
        payload["controller_evidence"] = _read_jsonl_objects(
            controller_evidence_path, label="controller evidence"
        )
    return recorder.append(phase, payload)


def observe_forced_cap_phase(
    recorder: TraceRecorder,
    *,
    phase: Literal[
        "forced_before",
        "forced_cap",
        "message_requeued",
        "chronicle_receipt",
        "receipt_read_ack",
        "fallback_retry",
    ],
    message_id: str,
    correlation_id: str,
    account: str | None = None,
    provider: str | None = None,
    persona: str | None = None,
    unit: str | None = None,
    tmux_target: str | None = None,
    runtime_state_path: Path | None = None,
    transition_state_path: Path | None = None,
    controller_evidence_path: Path | None = None,
    mailbox_root: Path | None = None,
    receipt_path: Path | None = None,
    retired_pid: int | None = None,
    proc_root: Path = Path("/proc"),
    runner: CommandRunner = _run_command,
) -> TraceEvent:
    payload: dict[str, Any] = {
        "message_id": message_id,
        "correlation_id": correlation_id,
    }
    runtime_args = (account, provider, persona, unit, tmux_target, runtime_state_path)
    if any(item is not None for item in runtime_args):
        if any(item is None for item in runtime_args):
            raise AcceptanceRefused("forced runtime observation arguments must be complete")
        payload["runtime"] = observe_runtime_state(
            recorder,
            account=str(account),
            provider=str(provider),
            persona=str(persona),
            unit=str(unit),
            tmux_target=str(tmux_target),
            runtime_state_path=Path(runtime_state_path),
            proc_root=proc_root,
            runner=runner,
        ).payload
    if transition_state_path is not None:
        payload["transition_state_path"] = transition_state_path.resolve()
        payload["transition_state_sha256"] = _sha256_file(transition_state_path)
        payload["transition"] = _transition_payload(transition_state_path)
    if controller_evidence_path is not None:
        payload["controller_evidence_path"] = controller_evidence_path.resolve()
        payload["controller_evidence_sha256"] = _sha256_file(controller_evidence_path)
        payload["controller_evidence"] = _read_jsonl_objects(
            controller_evidence_path, label="controller evidence"
        )
    if mailbox_root is not None:
        record_path = mailbox_root.resolve() / "new" / message_id
        observed_message, observed_correlation, observed_hash = _mail_record_identity(record_path)
        if (observed_message, observed_correlation) != (message_id, correlation_id):
            raise AcceptanceRefused("requeued mailbox message/correlation identity mismatch")
        inflight = tuple((mailbox_root.resolve() / ".inflight").glob(f"*/{message_id}"))
        if inflight or (mailbox_root.resolve() / "cur" / message_id).exists():
            raise AcceptanceRefused("requeued message also exists in inflight or cur")
        payload.update(
            {
                "requeued_path": record_path,
                "requeued_sha256": observed_hash,
                "inflight_count": len(inflight),
                "cur_exists": False,
            }
        )
    if receipt_path is not None:
        payload["receipt_path"] = receipt_path.resolve()
        payload["receipt_sha256"] = _sha256_file(receipt_path)
        receipt = _read_json_mapping(receipt_path, label="forced failover receipt")
        payload["receipt"] = receipt
        chronicle_path = Path(str(receipt.get("chronicle_entry", "")))
        payload["chronicle_path"] = chronicle_path.resolve()
        payload["chronicle_sha256"] = _sha256_file(chronicle_path)
    if retired_pid is not None:
        payload["retired_pid"] = retired_pid
        payload["retired_pid_absent"] = not (proc_root / str(retired_pid)).exists()
    return recorder.append(phase, payload)


def observe_batch_result(
    recorder: TraceRecorder,
    *,
    result_path: Path,
    manifest_path: Path,
) -> TraceEvent:
    if result_path.is_symlink() or not result_path.is_file():
        raise AcceptanceRefused(f"batch result path is missing or not regular: {result_path}")
    result = _read_json_mapping(result_path, label="batch result")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AcceptanceRefused("cannot parse instance manifest") from exc
    if not isinstance(manifest, dict):
        raise AcceptanceRefused("instance manifest must be a mapping")
    runtime = manifest.get("harness_runtime")
    harnesses = runtime.get("harnesses") if isinstance(runtime, dict) else None
    claude = harnesses.get("claude") if isinstance(harnesses, dict) else None
    profiles = claude.get("profiles") if isinstance(claude, dict) else None
    batch_profile = profiles.get("batch-sdk") if isinstance(profiles, dict) else None
    interactive_name = claude.get("default_profile") if isinstance(claude, dict) else None
    interactive_profile = profiles.get(interactive_name) if isinstance(profiles, dict) else None
    if not isinstance(batch_profile, dict) or not isinstance(interactive_profile, dict):
        raise AcceptanceRefused("manifest lacks Claude batch and interactive profiles")
    binding = result.get("binding")
    launch = binding.get("launch") if isinstance(binding, dict) else None
    provider_result = result.get("result")
    if not isinstance(launch, dict) or not isinstance(provider_result, dict):
        raise AcceptanceRefused("batch result lacks binding launch or provider result")
    argv = provider_result.get("command_argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise AcceptanceRefused("batch provider result lacks command argv")
    return recorder.append(
        "batch_result",
        {
            "provider": binding.get("selected_harness") if isinstance(binding, dict) else None,
            "profile": launch.get("profile"),
            "meter": launch.get("meter"),
            "mode": launch.get("mode"),
            "executable": launch.get("executable"),
            "provider_result": provider_result,
            "argv": argv,
            "interactive_default_profile": interactive_name,
            "interactive_default_mode": interactive_profile.get("mode"),
            "batch_manifest": batch_profile,
            "manifest_path": manifest_path.resolve(),
            "manifest_sha256": _sha256_file(manifest_path),
            "result_path": result_path.resolve(),
            "result_sha256": _sha256_file(result_path),
        },
    )


def _events(events: Sequence[TraceEvent], phase: str) -> tuple[TraceEvent, ...]:
    return tuple(event for event in events if event.phase == phase)


def _last(events: Sequence[TraceEvent], phase: str) -> TraceEvent:
    matches = _events(events, phase)
    if not matches:
        raise AcceptanceRefused(f"missing required trace phase: {phase}")
    return matches[-1]


def _require_order(events: Sequence[TraceEvent], phases: Sequence[str]) -> None:
    positions = []
    for phase in phases:
        matches = _events(events, phase)
        if not matches:
            raise AcceptanceRefused(f"missing required trace phase: {phase}")
        positions.append(matches[-1].sequence)
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise AcceptanceRefused("trace phases are out of required order")


def _require_deployed(events: Sequence[TraceEvent], reviewed_revision: str) -> Mapping[str, Any]:
    deployed = _last(events, "deployed_source").payload
    if deployed.get("deployed_head") != reviewed_revision:
        raise AcceptanceRefused(
            f"deployed HEAD mismatch: reviewed {reviewed_revision}, observed {deployed.get('deployed_head')}"
        )
    if not deployed.get("deployed_source_hash") or not deployed.get("unit_directives"):
        raise AcceptanceRefused("deployed source evidence is incomplete")
    return deployed


def _require_service_success(events: Sequence[TraceEvent]) -> None:
    service = _last(events, "service_status").payload
    if service.get("Result") != "success" or str(service.get("ExecMainStatus")) != "0":
        raise AcceptanceRefused("service status is not Result=success ExecMainStatus=0")
    if str(service.get("ExecMainCode")) != "1":
        raise AcceptanceRefused("service main process has not structurally exited")
    try:
        exit_tick = int(str(service.get("ExecMainExitTimestampMonotonic", "0")))
    except ValueError as exc:
        raise AcceptanceRefused("service exit timestamp is invalid") from exc
    if exit_tick <= 0:
        raise AcceptanceRefused("service main process has no completed exit timestamp")
    if not service.get("InvocationID") or not service.get("FragmentPath"):
        raise AcceptanceRefused("service provenance is incomplete")


def _require_mailbox_sequence(events: Sequence[TraceEvent]) -> MailboxEvidence:
    _require_order(events, MAILBOX_PHASES)
    new = _last(events, "mailbox_new").payload
    claim = _last(events, "runner_claim").payload
    cur = _last(events, "mailbox_cur").payload

    hashes = {new.get("sha256"), claim.get("sha256"), cur.get("sha256")}
    message_ids = {new.get("message_id"), claim.get("message_id"), cur.get("message_id")}
    correlations = {
        new.get("correlation_id"), claim.get("correlation_id"), cur.get("correlation_id")
    }
    if len(hashes) != 1 or len(message_ids) != 1 or len(correlations) != 1 or None in correlations or "" in correlations:
        raise AcceptanceRefused(
            "mailbox phase byte, message, or correlation identity mismatch"
        )
    if new.get("claim_count") or new.get("cur_count"):
        raise AcceptanceRefused("mailbox new phase had conflicting locations")
    if claim.get("new_exists") or int(claim.get("cur_count", -1)) != 0:
        raise AcceptanceRefused("runner claim observed while source existed outside claim")
    if cur.get("new_exists") or int(cur.get("claim_count", -1)) != 0:
        raise AcceptanceRefused("cur acknowledgement observed while new/ or claim copy still exists")
    if int(cur.get("cur_count", 0)) != 1:
        raise AcceptanceRefused("cur acknowledgement count must be exactly one")

    return MailboxEvidence(
        producer_path=None,
        mailbox_new_path=Path(str(new["path"])),
        runner_claim_path=Path(str(claim["path"])),
        mailbox_cur_path=Path(str(cur["path"])),
        message_id=str(new["message_id"]),
        correlation_id=new.get("correlation_id"),
        source_sha256=str(new["sha256"]),
    )


def _require_runtime(
    events: Sequence[TraceEvent],
    *,
    provider: str,
    reviewed_revision: str,
    deployed: Mapping[str, Any],
) -> RuntimeEvidence:
    runtime = _last(events, "runtime_state").payload
    if runtime.get("provider") != provider:
        raise AcceptanceRefused(f"runtime provider mismatch: expected {provider}")
    if runtime.get("pane_dead") is not False or not runtime.get("pane_id"):
        raise AcceptanceRefused("runtime evidence does not show one live tmux pane")
    state = runtime.get("runtime_state")
    if not isinstance(state, Mapping) or state.get("harness") != provider:
        raise AcceptanceRefused("runtime controller state is missing or mismatched")
    _require_runtime_scope_evidence(runtime)
    service = _last(events, "service_status").payload
    if runtime.get("account") != service.get("account") or runtime.get("unit") != service.get("unit"):
        raise AcceptanceRefused("runtime account/unit does not match the observed service invocation")
    if not service.get("ExecMainStartTimestamp") or not service.get("ExecMainExitTimestamp"):
        raise AcceptanceRefused("service invocation lacks wall-clock provenance")
    running_revision, running_hash = _require_runtime_source_provenance(
        runtime,
        deployed=deployed,
        reviewed_revision=reviewed_revision,
    )
    quota = _last(events, "quota").payload
    return RuntimeEvidence(
        persona=str(runtime["persona"]),
        unit=str(runtime["unit"]),
        tmux_target=str(runtime["tmux_target"]),
        pane_id=str(runtime["pane_id"]),
        running_source_revision=running_revision,
        running_source_hash=running_hash,
        executable=str(runtime["executable"]),
        quota_source=str(quota["quota_source"]),
        argv=tuple(str(item) for item in runtime.get("argv", ())),
    )


def _require_quota(events: Sequence[TraceEvent], *, provider: str, source: str, meter: str) -> None:
    quota = _last(events, "quota").payload
    if quota.get("provider") != provider or quota.get("quota_source") != source:
        raise AcceptanceRefused("missing or wrong quota observation")
    if quota.get("meter") != meter:
        raise AcceptanceRefused("quota meter evidence does not match expected meter")
    if quota.get("freshness") != "fresh":
        raise AcceptanceRefused("quota observation is not fresh")
    if quota.get("remaining_percent") is None or not quota.get("fresh_observed_at"):
        raise AcceptanceRefused("quota observation is missing fresh meter state")
    _parse_utc(quota.get("fresh_observed_at"), label="quota trace")


def _require_controller_sequence(
    records: Sequence[Mapping[str, Any]],
    *,
    transition_id: str,
    events: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    relevant = tuple(
        record for record in records if record.get("transition_id") == transition_id
    )
    if not relevant:
        raise AcceptanceRefused("controller evidence lacks the observed transition")
    if any(
        record.get("schema") != "pantheon-runtime-controller-evidence/v1"
        for record in relevant
    ):
        raise AcceptanceRefused("controller evidence schema mismatch")
    selected: list[Mapping[str, Any]] = []
    cursor = 0
    for event in events:
        for index in range(cursor, len(relevant)):
            if relevant[index].get("event") == event:
                selected.append(relevant[index])
                cursor = index + 1
                break
        else:
            raise AcceptanceRefused(f"controller evidence lacks ordered event: {event}")
    return tuple(selected)


def _require_runtime_source_provenance(
    runtime_payload: Mapping[str, Any],
    *,
    deployed: Mapping[str, Any],
    reviewed_revision: str,
) -> tuple[str, str]:
    state = runtime_payload.get("runtime_state")
    if not isinstance(state, Mapping):
        raise AcceptanceRefused("runtime observation lacks controller state")
    revision = state.get("controller_source_revision")
    source_hashes = state.get("controller_source_hashes")
    manifest_hash = state.get("controller_source_manifest_sha256")
    if not isinstance(revision, str) or not revision:
        raise AcceptanceRefused("runtime state lacks controller source revision")
    if not isinstance(source_hashes, Mapping) or not source_hashes:
        raise AcceptanceRefused("runtime state lacks controller source hashes")
    if not isinstance(manifest_hash, str) or not manifest_hash:
        raise AcceptanceRefused("runtime state lacks controller source manifest hash")
    if revision != reviewed_revision:
        raise AcceptanceRefused("runtime controller source revision mismatch")
    deployed_hashes = deployed.get("source_hashes")
    if not isinstance(deployed_hashes, Mapping) or not deployed_hashes:
        raise AcceptanceRefused("deployed source evidence lacks source hashes")
    normalized_hashes = {str(key): str(value) for key, value in source_hashes.items()}
    normalized_deployed = {
        str(key): str(value) for key, value in deployed_hashes.items()
    }
    if normalized_hashes != normalized_deployed:
        raise AcceptanceRefused("runtime controller source hash set mismatch")
    expected_manifest = _source_manifest_hash(normalized_hashes)
    if manifest_hash != expected_manifest or manifest_hash != deployed.get(
        "deployed_source_hash"
    ):
        raise AcceptanceRefused("runtime controller source manifest hash mismatch")

    boundary = _parse_utc(
        state.get("runtime_start_boundary_at"), label="runtime start boundary"
    )
    observed = _parse_utc(
        state.get("runtime_start_observed_at"), label="runtime start observed"
    )
    process_started = _parse_utc(
        runtime_payload.get("process_started_at"), label="runtime process start"
    )
    if observed < boundary:
        raise AcceptanceRefused("runtime observed start precedes start boundary")
    if process_started < boundary:
        raise AcceptanceRefused("runtime process predates controller start boundary")
    if process_started > observed + MAX_FUTURE_SKEW:
        raise AcceptanceRefused("runtime process start is not near observed runtime start")
    return revision, manifest_hash


def _finalize_codex(events: Sequence[TraceEvent], reviewed: str) -> tuple[MailboxEvidence, RuntimeEvidence]:
    deployed = _require_deployed(events, reviewed)
    _require_service_success(events)
    _require_quota(events, provider="codex", source="app-server", meter="subscription")
    mailbox = _require_mailbox_sequence(events)
    runtime = _require_runtime(
        events,
        provider="codex",
        reviewed_revision=reviewed,
        deployed=deployed,
    )
    forbidden = {"exec", "app-server"}
    if forbidden.intersection(runtime.argv):
        raise AcceptanceRefused("Codex live pane is not the default interactive CLI")
    return mailbox, runtime


def _finalize_claude(events: Sequence[TraceEvent], reviewed: str) -> tuple[MailboxEvidence, RuntimeEvidence]:
    deployed = _require_deployed(events, reviewed)
    _require_service_success(events)
    _require_quota(events, provider="claude", source="statusline", meter="subscription")
    mailbox = _require_mailbox_sequence(events)
    runtime = _require_runtime(
        events,
        provider="claude",
        reviewed_revision=reviewed,
        deployed=deployed,
    )
    if "-p" in runtime.argv:
        raise AcceptanceRefused("interactive Claude runtime argv contains forbidden -p")
    if "claude" not in Path(runtime.argv[0]).name:
        raise AcceptanceRefused("interactive Claude runtime argv does not invoke claude")
    return mailbox, runtime


def _finalize_swap(events: Sequence[TraceEvent], reviewed: str) -> tuple[None, RuntimeEvidence]:
    deployed = _require_deployed(events, reviewed)
    _require_service_success(events)
    quota = _last(events, "quota").payload
    _require_order(
        events,
        (
            "swap_before",
            "quota_overlay_installed",
            "quota",
            "planned_preparing",
            "quota_overlay_removed",
            "swap_after",
            "runtime_mailbox_result",
            "primary_quota_probe",
            "swap_restore",
        ),
    )
    lifecycle_phases = (
        "swap_before",
        "quota_overlay_installed",
        "quota",
        "planned_preparing",
        "quota_overlay_removed",
        "swap_after",
        "runtime_mailbox_result",
        "primary_quota_probe",
        "swap_restore",
    )
    duplicated = tuple(phase for phase in lifecycle_phases if len(_events(events, phase)) != 1)
    if duplicated:
        raise AcceptanceRefused(
            "swap trace lifecycle phase count is not exactly one: "
            + ", ".join(duplicated)
        )
    installed = _last(events, "quota_overlay_installed").payload
    removed = _last(events, "quota_overlay_removed").payload
    preparing = _last(events, "planned_preparing").payload
    if quota.get("quota_source") != "acceptance-overlay" or quota.get("fixture") is not True:
        raise AcceptanceRefused("planned swap quota is not an explicit acceptance fixture")
    overlay_identity = {
        (installed.get("run_id"), installed.get("overlay_sha256")),
        (quota.get("fixture_run_id"), quota.get("overlay_sha256")),
        (removed.get("run_id"), removed.get("overlay_sha256")),
    }
    if len(overlay_identity) != 1 or (None, None) in overlay_identity:
        raise AcceptanceRefused("quota overlay lifecycle identity mismatch")
    transition_preparing = preparing.get("transition")
    if not isinstance(transition_preparing, Mapping):
        raise AcceptanceRefused("planned preparing event lacks transition state")
    preparing_quota = transition_preparing.get("quota_evidence")
    if not isinstance(preparing_quota, Mapping):
        raise AcceptanceRefused("planned preparing event lacks quota evidence")
    if (
        preparing_quota.get("run_id") != installed.get("run_id")
        or preparing_quota.get("overlay_sha256") != installed.get("overlay_sha256")
        or preparing_quota.get("source") != "acceptance-overlay"
        or str(preparing_quota.get("fixture", "false")).lower() != "true"
    ):
        raise AcceptanceRefused("planned transition is not bound to installed overlay")
    if quota.get("freshness") != "fresh":
        raise AcceptanceRefused("planned swap lacks a fresh quota observation")
    try:
        remaining = float(quota["remaining_percent"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceRefused("planned swap quota remaining percent is invalid") from exc
    if not math.isfinite(remaining) or remaining != 5.0:
        raise AcceptanceRefused("planned swap was not triggered at the fixed 5% reserve")
    phase_payloads = tuple(_last(events, phase).payload for phase in ("swap_before", "swap_after", "swap_restore"))
    runtimes: list[Mapping[str, Any]] = []
    for item in phase_payloads:
        runtime = item.get("runtime")
        if not isinstance(runtime, Mapping):
            raise AcceptanceRefused("swap phase lacks a live runtime observation")
        runtimes.append(runtime)
    before, after, restore = runtimes
    for item in runtimes:
        _require_runtime_scope_evidence(item)
    for item in (after, restore):
        for key in ("account", "persona", "unit", "tmux_target", "pane_id"):
            if item.get(key) != before.get(key):
                raise AcceptanceRefused("swap did not preserve account/persona/unit/tmux/pane")
    if before.get("provider") == after.get("provider"):
        raise AcceptanceRefused("swap did not change provider from target A to target B")
    if restore.get("provider") != before.get("provider"):
        raise AcceptanceRefused("swap did not restore target A")
    if len({before.get("pid"), after.get("pid"), restore.get("pid")}) != 3:
        raise AcceptanceRefused("swap did not replace each runtime process")

    roundtrip = _last(events, "runtime_mailbox_result").payload
    for key, value in {
        "account": after.get("account"),
        "persona": after.get("persona"),
        "harness": after.get("provider"),
    }.items():
        if roundtrip.get(key) != value:
            raise AcceptanceRefused(f"runtime mailbox result mismatches fallback {key}")
    if not all(
        roundtrip.get(key)
        for key in (
            "message_id",
            "correlation_id",
            "source_sha256",
            "reply_sha256",
            "controller_evidence_sha256",
            "expected_message_sha256",
        )
    ):
        raise AcceptanceRefused("runtime mailbox result evidence is incomplete")

    planned = phase_payloads[1].get("transition")
    reset = phase_payloads[2].get("transition")
    if not isinstance(planned, Mapping) or not isinstance(reset, Mapping):
        raise AcceptanceRefused("swap lacks controller transition state")
    expected_planned = {
        "persona": before.get("persona"),
        "trigger": "planned-quota",
        "phase": "completed",
        "outgoing_harness": before.get("provider"),
        "incoming_harness": after.get("provider"),
        "runtime_swapped": True,
        "successor_read_ack": True,
    }
    expected_reset = {
        "persona": before.get("persona"),
        "trigger": "quota-reset",
        "phase": "completed",
        "outgoing_harness": after.get("provider"),
        "incoming_harness": restore.get("provider"),
        "runtime_swapped": True,
        "successor_read_ack": True,
        "fallback_active": False,
    }
    for label, transition, expected in (
        ("planned", planned, expected_planned),
        ("reset", reset, expected_reset),
    ):
        mismatched = [key for key, value in expected.items() if transition.get(key) != value]
        if mismatched:
            raise AcceptanceRefused(f"{label} swap transition mismatch: {', '.join(mismatched)}")
        if not transition.get("successor_read_evidence") or not transition.get("chronicle_evidence"):
            raise AcceptanceRefused(f"{label} swap lacks structural handoff/read evidence")
    recovery = reset.get("quota_evidence")
    if not isinstance(recovery, Mapping) or not recovery:
        raise AcceptanceRefused("quota-reset transition lacks fresh recovery evidence")
    if recovery.get("probe_source") != "statusline":
        raise AcceptanceRefused("quota-reset recovery is not provider statusline evidence")
    if str(recovery.get("probe_fixture", "false")).lower() == "true" or (
        recovery.get("probe_source") == "acceptance-overlay"
        or recovery.get("probe_underlying_source") == "acceptance-overlay"
    ):
        raise AcceptanceRefused("quota-reset recovery still carries fixture evidence")
    if str(recovery.get("probe_authoritative_limit", "true")).lower() != "false":
        raise AcceptanceRefused("quota-reset recovery carries an authoritative limit")
    if recovery.get("probe_freshness") != "fresh":
        raise AcceptanceRefused("quota-reset recovery is not fresh")
    primary_probe = _last(events, "primary_quota_probe").payload
    probe_result = primary_probe.get("result")
    if not isinstance(probe_result, Mapping):
        raise AcceptanceRefused("primary quota probe lacks a structural result")
    expected_probe_target = claude_probe_tmux_target(
        str(before.get("persona")), str(probe_result.get("run_id"))
    )
    expected_probe_session = claude_probe_session_id(
        str(before.get("account")),
        str(before.get("persona")),
        str(probe_result.get("run_id")),
    )
    for key, value in {
        "account": before.get("account"),
        "persona": before.get("persona"),
        "tmux_target": expected_probe_target,
        "session_id": expected_probe_session,
    }.items():
        if probe_result.get(key) != value:
            raise AcceptanceRefused(f"primary quota probe {key} identity mismatch")
    if recovery.get("probe_path") != primary_probe.get("canonical_path"):
        raise AcceptanceRefused("quota-reset recovery path is not the probed provider path")
    if recovery.get("probe_sha256") != primary_probe.get("canonical_sha256"):
        raise AcceptanceRefused("quota-reset recovery bytes are not the probed provider bytes")
    probe_observed = _parse_utc(
        recovery.get("probe_observed_at"), label="quota-reset probe observed_at"
    )
    canonical_observed = _parse_utc(
        primary_probe.get("canonical_observed_at"),
        label="primary quota canonical observed_at",
    )
    if probe_observed != canonical_observed:
        raise AcceptanceRefused("quota-reset probe timestamp differs from probed bytes")
    reset_passed = _parse_utc(
        recovery.get("reset_passed_at"), label="quota-reset passed_at"
    )
    if probe_observed < reset_passed:
        raise AcceptanceRefused("quota-reset probe predates the persisted reset")
    try:
        recovery_remaining = float(recovery["probe_weekly_remaining_percent"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceRefused("quota-reset recovery lacks weekly remaining evidence") from exc
    if not math.isfinite(recovery_remaining) or recovery_remaining <= 5.0:
        raise AcceptanceRefused("quota-reset recovery has not cleared the 5% reserve")
    return None, _runtime_evidence_from_payload(
        restore,
        reviewed_revision=reviewed,
        deployed=deployed,
        quota_source=str(quota.get("quota_source", "unknown")),
    )


def _finalize_forced_cap(events: Sequence[TraceEvent], reviewed: str) -> tuple[MailboxEvidence, RuntimeEvidence]:
    deployed = _require_deployed(events, reviewed)
    _require_service_success(events)
    _require_order(
        events,
        (
            "mailbox_new",
            "forced_before",
            "forced_cap",
            "message_requeued",
            "chronicle_receipt",
            "receipt_read_ack",
            "fallback_retry",
            "mailbox_cur",
        ),
    )
    claims = _events(events, "runner_claim")
    if len(claims) != 2:
        raise AcceptanceRefused("forced-cap acceptance requires original and retry claims")
    new = _last(events, "mailbox_new")
    if not (new.sequence < claims[0].sequence < _last(events, "forced_before").sequence):
        raise AcceptanceRefused("authoritative cap was not observed after the original claim")
    if not (_last(events, "fallback_retry").sequence < claims[1].sequence < _last(events, "mailbox_cur").sequence):
        raise AcceptanceRefused("fallback retry claim/cur ordering is invalid")

    forced_events = tuple(
        _last(events, phase).payload
        for phase in (
            "forced_before",
            "forced_cap",
            "message_requeued",
            "chronicle_receipt",
            "receipt_read_ack",
            "fallback_retry",
        )
    )
    identities = {
        (item.get("message_id"), item.get("correlation_id")) for item in forced_events
    }
    mailbox_identity = (
        _last(events, "mailbox_new").payload.get("message_id"),
        _last(events, "mailbox_new").payload.get("correlation_id"),
    )
    if len(identities) != 1 or next(iter(identities)) != mailbox_identity:
        raise AcceptanceRefused("forced-cap message/correlation identity changed")

    before, cap, requeued, receipt_event, read_ack, retry = forced_events
    before_runtime = before.get("runtime")
    cap_runtime = cap.get("runtime")
    retry_runtime = retry.get("runtime")
    if not all(isinstance(item, Mapping) for item in (before_runtime, cap_runtime, retry_runtime)):
        raise AcceptanceRefused("forced-cap path lacks live outgoing/fallback runtime proof")
    assert isinstance(before_runtime, Mapping)
    assert isinstance(cap_runtime, Mapping)
    assert isinstance(retry_runtime, Mapping)
    for item in (before_runtime, cap_runtime, retry_runtime):
        _require_runtime_scope_evidence(item)
    for item in (cap_runtime, retry_runtime):
        for key in ("account", "persona", "unit", "tmux_target", "pane_id"):
            if item.get(key) != before_runtime.get(key):
                raise AcceptanceRefused("forced-cap failover did not preserve runtime identity")
    if cap_runtime.get("provider") == before_runtime.get("provider"):
        raise AcceptanceRefused("forced-cap path did not replace the capped provider")
    if retry_runtime.get("provider") != cap_runtime.get("provider"):
        raise AcceptanceRefused("forced-cap retry did not use the fallback provider")
    if cap.get("retired_pid") != before_runtime.get("pid") or cap.get("retired_pid_absent") is not True:
        raise AcceptanceRefused("capped runtime process was not structurally retired")

    cap_transition = cap.get("transition")
    ack_transition = read_ack.get("transition")
    if not isinstance(cap_transition, Mapping) or not isinstance(ack_transition, Mapping):
        raise AcceptanceRefused("forced-cap path lacks controller transition state")
    for key, value in {
        "trigger": "forced-cap",
        "breaker_open": True,
        "runtime_swapped": True,
        "outgoing_harness": before_runtime.get("provider"),
        "incoming_harness": cap_runtime.get("provider"),
    }.items():
        if cap_transition.get(key) != value:
            raise AcceptanceRefused(f"forced-cap controller state mismatch: {key}")
    if cap_transition.get("phase") not in {"awaiting-successor-read", "completed"}:
        raise AcceptanceRefused("forced-cap controller is not at the successor-read barrier")
    if ack_transition.get("phase") != "completed" or ack_transition.get("successor_read_ack") is not True:
        raise AcceptanceRefused("successor did not structurally acknowledge chronicle read")
    if not ack_transition.get("successor_read_evidence"):
        raise AcceptanceRefused("forced-cap read acknowledgement lacks evidence")

    receipt = receipt_event.get("receipt")
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "pantheon-forced-failover/v1":
        raise AcceptanceRefused("forced-cap receipt schema is absent")
    for key, value in {"message_id": mailbox_identity[0], "correlation_id": mailbox_identity[1]}.items():
        if receipt.get(key) != value:
            raise AcceptanceRefused(f"forced-cap receipt {key} mismatch")
    if not receipt_event.get("chronicle_sha256"):
        raise AcceptanceRefused("forced-cap chronicle entry is absent")
    transition_id = cap_transition.get("transition_id")
    if not isinstance(transition_id, str) or not transition_id:
        raise AcceptanceRefused("forced-cap transition lacks transition_id")
    if receipt.get("transition_id") != transition_id:
        raise AcceptanceRefused("forced-cap receipt transition_id mismatch")
    controller_records = retry.get("controller_evidence")
    if not isinstance(controller_records, list):
        raise AcceptanceRefused("forced-cap retry lacks controller evidence log")
    ordered = _require_controller_sequence(
        controller_records,
        transition_id=transition_id,
        events=(
            "breaker_opened",
            "forced_evidence_durable",
            "runtime_replaced",
            "runtime_submission_attempt",
            "runtime_submission_accepted",
            "successor_read_acknowledged",
        ),
    )
    breaker = ordered[0]
    breaker_index = controller_records.index(breaker)
    after_breaker = controller_records[breaker_index + 1 :]
    capped_profile = breaker.get("capped_profile")
    capped_harness = breaker.get("outgoing_harness")
    forbidden = [
        record
        for record in after_breaker
        if record.get("event") in {"runtime_submission_attempt", "runtime_submission_accepted"}
        and record.get("profile") == capped_profile
        and record.get("harness") == capped_harness
    ]
    if forbidden:
        raise AcceptanceRefused("capped provider has a post-breaker submission")
    fallback_accepts = [
        record
        for record in after_breaker
        if record.get("event") == "runtime_submission_accepted"
        and record.get("profile") == cap_transition.get("incoming_profile")
        and record.get("harness") == cap_transition.get("incoming_harness")
        and record.get("message_id") == mailbox_identity[0]
        and record.get("correlation_id") == mailbox_identity[1]
    ]
    if not fallback_accepts:
        raise AcceptanceRefused("fallback submission is absent from controller evidence")
    if requeued.get("requeued_sha256") != claims[0].payload.get("sha256"):
        raise AcceptanceRefused("requeued forced-cap message bytes changed")
    cap = _last(events, "forced_cap").payload
    mailbox = _require_mailbox_sequence(events)
    return mailbox, _runtime_evidence_from_payload(
        retry_runtime,
        reviewed_revision=reviewed,
        deployed=deployed,
        quota_source="authoritative-cap",
    )


def _positive_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AcceptanceRefused(f"batch provider billing lacks {label}")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise AcceptanceRefused(f"batch provider billing {label} is not positive")
    return parsed


def _require_batch_provider_billing(
    provider_result: Mapping[str, Any],
) -> Mapping[str, Any]:
    billing = provider_result.get("provider_billing")
    if not isinstance(billing, Mapping):
        raise AcceptanceRefused("batch provider result lacks provider billing evidence")
    if billing.get("provider") not in {"claude", "claude-code"}:
        raise AcceptanceRefused("batch provider billing is not Claude")
    if billing.get("api_key_source") != "ANTHROPIC_API_KEY":
        raise AcceptanceRefused("batch provider billing did not use ANTHROPIC_API_KEY")
    _positive_float(billing.get("total_cost_usd"), label="total_cost_usd")
    token_total = 0
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = billing.get(key)
        if isinstance(value, bool):
            raise AcceptanceRefused("batch provider billing usage is invalid")
        if isinstance(value, int):
            token_total += value
    if token_total <= 0:
        raise AcceptanceRefused("batch provider billing lacks positive usage")
    source_events = billing.get("source_events")
    if not isinstance(source_events, (list, tuple)):
        raise AcceptanceRefused("batch provider billing lacks source events")
    if {"system/init", "result"} - {str(item) for item in source_events}:
        raise AcceptanceRefused("batch provider billing lacks required provider events")
    return billing


def _require_batch_command_binding(batch: Mapping[str, Any]) -> tuple[str, ...]:
    provider_result = batch.get("provider_result")
    if not isinstance(provider_result, Mapping):
        raise AcceptanceRefused("batch leg lacks a real provider result")
    argv = provider_result.get("command_argv")
    if not isinstance(argv, (tuple, list)) or not argv:
        raise AcceptanceRefused("batch provider result lacks command argv")
    if not all(isinstance(item, str) and item for item in argv):
        raise AcceptanceRefused("batch provider result command argv is invalid")
    if tuple(argv) != tuple(batch.get("argv", ())):
        raise AcceptanceRefused("batch command/result binding mismatch")
    executable = batch.get("executable")
    if not isinstance(executable, str) or not executable:
        raise AcceptanceRefused("batch launch executable is missing")
    if argv[0] != executable:
        raise AcceptanceRefused("batch command argv executable mismatch")
    if "-p" not in argv:
        raise AcceptanceRefused("batch command argv is not Claude headless")
    try:
        output_index = tuple(argv).index("--output-format")
    except ValueError as exc:
        raise AcceptanceRefused("batch command argv lacks stream-json output") from exc
    if len(argv) <= output_index + 1 or argv[output_index + 1] != "stream-json":
        raise AcceptanceRefused("batch command argv lacks stream-json output")
    if "--verbose" not in argv:
        raise AcceptanceRefused("batch command argv lacks verbose provider events")
    return tuple(argv)


def _finalize_batch(events: Sequence[TraceEvent], reviewed: str) -> tuple[None, RuntimeEvidence]:
    deployed = _require_deployed(events, reviewed)
    batch = _last(events, "batch_result").payload
    if batch.get("provider") != "claude":
        raise AcceptanceRefused("batch leg must be Claude")
    batch_manifest = batch.get("batch_manifest")
    if not isinstance(batch_manifest, Mapping):
        raise AcceptanceRefused("batch leg lacks manifest profile evidence")
    if batch.get("profile") != "batch-sdk" or batch.get("meter") != "sdk-credit":
        raise AcceptanceRefused("batch leg lacks explicit batch-sdk/sdk-credit evidence")
    if batch.get("mode") != "batch":
        raise AcceptanceRefused("batch leg must be mode=batch")
    for key in ("mode", "meter", "executable"):
        if batch.get(key) != batch_manifest.get(key):
            raise AcceptanceRefused("batch binding does not match manifest profile")
    if batch.get("interactive_default_profile") == batch.get("profile"):
        raise AcceptanceRefused("Claude batch profile is the interactive default")
    provider_result = batch.get("provider_result")
    if not isinstance(provider_result, Mapping):
        raise AcceptanceRefused("batch leg lacks a real provider result")
    argv = _require_batch_command_binding(batch)
    if provider_result.get("status") != "completed" or provider_result.get("returncode") != 0:
        raise AcceptanceRefused("batch provider result was not structurally completed")
    if provider_result.get("harness") not in {"claude", "claude-code"}:
        raise AcceptanceRefused("batch provider result is not Claude")
    if batch.get("interactive_default_profile") != "interactive-subscription" or batch.get("interactive_default_mode") != "interactive":
        raise AcceptanceRefused("Claude interactive subscription is not the manifest default")
    _require_batch_provider_billing(provider_result)
    if not batch.get("result_sha256"):
        raise AcceptanceRefused("batch leg lacks actual result evidence")
    return None, RuntimeEvidence(
        persona="claude-batch-sdk",
        unit=str(deployed["unit"]),
        tmux_target="batch-sdk:explicit",
        pane_id="batch-sdk",
        running_source_revision=reviewed,
        running_source_hash=str(deployed["deployed_source_hash"]),
        executable=str(batch.get("executable")),
        quota_source="ANTHROPIC_API_KEY",
        argv=tuple(str(item) for item in argv),
    )


def finalize_trace(
    *,
    leg_name: str,
    reviewed_revision: str,
    evidence_root: Path,
    run_id: str,
) -> LegResult:
    leg = parse_leg(leg_name)
    reviewed_revision = validate_reviewed_revision(reviewed_revision)
    recorder = TraceRecorder(evidence_root, leg=leg.name, run_id=run_id)
    events = recorder.read()
    if not events:
        raise AcceptanceRefused("cannot finalize without helper-recorded trace phases")
    _require_order(events, ("init", "deployed_source", "service_status"))
    init = _last(events, "init").payload
    if init.get("run_id") != run_id or Path(str(init.get("evidence_root"))).resolve() != recorder.root:
        raise AcceptanceRefused("trace init does not match this evidence run; copied evidence refused")

    if leg.name == LEG_CODEX:
        mailbox, runtime = _finalize_codex(events, reviewed_revision)
    elif leg.name == LEG_CLAUDE:
        mailbox, runtime = _finalize_claude(events, reviewed_revision)
    elif leg.name == LEG_SWAP:
        mailbox, runtime = _finalize_swap(events, reviewed_revision)
    elif leg.name == LEG_FORCED_CAP:
        mailbox, runtime = _finalize_forced_cap(events, reviewed_revision)
    elif leg.name == LEG_BATCH:
        mailbox, runtime = _finalize_batch(events, reviewed_revision)
    else:
        raise AcceptanceRefused(f"unsupported leg: {leg.name}")

    result = LegResult(
        leg=leg.name,
        status="passed",
        reason=None,
        mailbox=mailbox,
        runtime=runtime,
        evidence_files=(recorder.path, recorder.root / "result.json"),
    )
    write_json_evidence(recorder.root / "result.json", _jsonable(result))
    return result


def run_leg(args: argparse.Namespace, env: Mapping[str, str]) -> LegResult:
    leg = parse_leg(args.leg)
    require_live_gates(leg, env)
    return finalize_trace(
        leg_name=leg.name,
        reviewed_revision=args.reviewed_revision,
        evidence_root=Path(args.evidence_root),
        run_id=args.run_id,
    )


def skip(reason: str) -> NoReturn:
    raise AcceptanceSkipped(reason)


def refuse(reason: str) -> NoReturn:
    raise AcceptanceRefused(reason)


def _recorder_from_args(args: argparse.Namespace) -> TraceRecorder:
    return TraceRecorder(Path(args.evidence_root), leg=args.leg, run_id=args.run_id)


def _add_common(target: argparse.ArgumentParser) -> None:
    target.add_argument("--leg", required=True, choices=tuple(known_legs()))
    target.add_argument("--run-id", required=True)
    target.add_argument(
        "--evidence-root",
        default="/home/codex/.local/state/pantheon/t2382-live-proof",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    _add_common(init)
    init.add_argument("--operator", required=True)
    init.add_argument("--purpose", required=True)

    observe = sub.add_parser("observe")
    _add_common(observe)
    observe.add_argument("--phase", required=True)
    observe.add_argument("--reviewed-revision")
    observe.add_argument("--deployed-repo")
    observe.add_argument("--installed-unit")
    observe.add_argument("--source-unit-relative")
    observe.add_argument("--unit")
    observe.add_argument("--account")
    observe.add_argument("--provider")
    observe.add_argument("--persona")
    observe.add_argument("--tmux-target")
    observe.add_argument("--runtime-state-path")
    observe.add_argument("--transition-state-path")
    observe.add_argument("--controller-evidence-path")
    observe.add_argument("--proc-root", default="/proc")
    observe.add_argument("--quota-source")
    observe.add_argument("--profile")
    observe.add_argument("--meter")
    observe.add_argument("--quota-state-path")
    observe.add_argument("--overlay-path")
    observe.add_argument("--canonical-path")
    observe.add_argument("--mailbox-root")
    observe.add_argument("--message-id")
    observe.add_argument("--claim-id")
    observe.add_argument("--correlation-id")
    observe.add_argument("--receipt-path")
    observe.add_argument("--result-path")
    observe.add_argument("--manifest-path")
    observe.add_argument("--probe-result-path")
    observe.add_argument("--reply-mailbox")
    observe.add_argument("--expected-message")
    observe.add_argument("--retired-pid", type=int)

    finalize = sub.add_parser("finalize")
    _add_common(finalize)
    finalize.add_argument("--reviewed-revision", required=True)

    overlay = sub.add_parser("quota-overlay")
    overlay.add_argument("action", choices=("install", "remove"))
    overlay.add_argument("--state-dir", type=Path, required=True)
    overlay.add_argument("--overlay-path", type=Path, required=True)
    overlay.add_argument("--canonical-path", type=Path, required=True)
    overlay.add_argument("--run-id", required=True)
    overlay.add_argument("--resets-at")
    overlay.add_argument("--expected-sha256")

    probe = sub.add_parser("consume-probe")
    probe.add_argument("--inbox", type=Path, required=True)
    probe.add_argument("--message-id", required=True)
    probe.add_argument("--correlation-id", required=True)
    probe.add_argument("--run-id", required=True)
    probe.add_argument("--gate-path", type=Path, required=True)
    probe.add_argument("--timeout-seconds", type=float, default=60.0)

    return parser


def _require_args(args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) in {None, ""}]
    if missing:
        raise AcceptanceRefused("missing observe argument(s): " + ", ".join(missing))


def _dispatch_observe(args: argparse.Namespace) -> TraceEvent:
    recorder = _recorder_from_args(args)
    if args.phase == "deployed_source":
        _require_args(
            args,
            "deployed_repo",
            "reviewed_revision",
            "installed_unit",
            "source_unit_relative",
            "unit",
        )
        return observe_deployed_source(
            recorder,
            deployed_repo=Path(args.deployed_repo),
            reviewed_revision=args.reviewed_revision,
            installed_unit=Path(args.installed_unit),
            source_unit_relative=args.source_unit_relative,
            unit=args.unit,
        )
    if args.phase == "service_status":
        _require_args(
            args, "account", "unit", "deployed_repo", "installed_unit", "persona"
        )
        return observe_service_status(
            recorder,
            account=args.account,
            unit=args.unit,
            deployed_repo=Path(args.deployed_repo),
            installed_unit=Path(args.installed_unit),
            persona=args.persona,
        )
    if args.phase == "runtime_state":
        _require_args(
            args,
            "account",
            "provider",
            "persona",
            "unit",
            "tmux_target",
            "runtime_state_path",
        )
        return observe_runtime_state(
            recorder,
            account=args.account,
            provider=args.provider,
            persona=args.persona,
            unit=args.unit,
            tmux_target=args.tmux_target,
            runtime_state_path=Path(args.runtime_state_path),
        )
    if args.phase == "quota":
        _require_args(args, "provider", "quota_source", "profile", "meter", "quota_state_path")
        return observe_quota(
            recorder,
            provider=args.provider,
            quota_source=args.quota_source,
            profile=args.profile,
            meter=args.meter,
            quota_state_path=Path(args.quota_state_path),
        )
    if args.phase in {"quota_overlay_installed", "quota_overlay_removed"}:
        _require_args(args, "overlay_path", "canonical_path")
        return observe_quota_overlay(
            recorder,
            phase=args.phase,
            overlay_path=Path(args.overlay_path),
            canonical_path=Path(args.canonical_path),
            run_id=args.run_id,
        )
    if args.phase == "planned_preparing":
        _require_args(args, "transition_state_path")
        return observe_planned_preparing(
            recorder,
            transition_state_path=Path(args.transition_state_path),
        )
    if args.phase == "primary_quota_probe":
        _require_args(args, "probe_result_path", "canonical_path")
        return observe_primary_quota_probe(
            recorder,
            result_path=Path(args.probe_result_path),
            canonical_path=Path(args.canonical_path),
        )
    if args.phase == "runtime_mailbox_result":
        _require_args(
            args,
            "mailbox_root",
            "reply_mailbox",
            "message_id",
            "correlation_id",
            "account",
            "persona",
            "provider",
            "expected_message",
            "controller_evidence_path",
        )
        return observe_runtime_mailbox_result(
            recorder,
            source_mailbox=Path(args.mailbox_root),
            reply_mailbox=Path(args.reply_mailbox),
            message_id=args.message_id,
            correlation_id=args.correlation_id,
            account=args.account,
            persona=args.persona,
            harness=args.provider,
            expected_message=args.expected_message,
            controller_evidence_path=Path(args.controller_evidence_path),
        )
    if args.phase in MAILBOX_PHASES:
        _require_args(args, "mailbox_root", "message_id")
        return observe_mailbox_phase(
            recorder,
            phase=args.phase,
            mailbox_root=Path(args.mailbox_root),
            message_id=args.message_id,
            claim_id=args.claim_id,
            correlation_id=args.correlation_id,
        )
    if args.phase in {"swap_before", "swap_after", "swap_restore"}:
        _require_args(
            args,
            "account",
            "provider",
            "persona",
            "unit",
            "tmux_target",
            "runtime_state_path",
        )
        return observe_swap_phase(
            recorder,
            phase=args.phase,
            account=args.account,
            provider=args.provider,
            persona=args.persona,
            unit=args.unit,
            tmux_target=args.tmux_target,
            runtime_state_path=Path(args.runtime_state_path),
            transition_state_path=Path(args.transition_state_path)
            if args.transition_state_path else None,
            controller_evidence_path=Path(args.controller_evidence_path)
            if args.controller_evidence_path else None,
            proc_root=Path(args.proc_root),
        )
    if args.phase in {
        "forced_before",
        "forced_cap",
        "message_requeued",
        "chronicle_receipt",
        "receipt_read_ack",
        "fallback_retry",
    }:
        _require_args(args, "message_id", "correlation_id")
        return observe_forced_cap_phase(
            recorder,
            phase=args.phase,
            message_id=args.message_id,
            correlation_id=args.correlation_id,
            account=args.account,
            provider=args.provider,
            persona=args.persona,
            unit=args.unit,
            tmux_target=args.tmux_target,
            runtime_state_path=Path(args.runtime_state_path) if args.runtime_state_path else None,
            transition_state_path=Path(args.transition_state_path)
            if args.transition_state_path else None,
            controller_evidence_path=Path(args.controller_evidence_path)
            if args.controller_evidence_path else None,
            mailbox_root=Path(args.mailbox_root) if args.mailbox_root else None,
            receipt_path=Path(args.receipt_path) if args.receipt_path else None,
            retired_pid=args.retired_pid,
            proc_root=Path(args.proc_root),
        )
    if args.phase == "batch_result":
        _require_args(
            args,
            "result_path",
            "manifest_path",
        )
        return observe_batch_result(
            recorder,
            result_path=Path(args.result_path),
            manifest_path=Path(args.manifest_path),
        )
    raise AcceptanceRefused(f"unsupported observe phase: {args.phase}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            event = initialize_trace(
                _recorder_from_args(args),
                operator=args.operator,
                purpose=args.purpose,
            )
            print(json.dumps(_jsonable(event), sort_keys=True))
        elif args.command == "observe":
            event = _dispatch_observe(args)
            print(json.dumps(_jsonable(event), sort_keys=True))
        elif args.command == "finalize":
            result = run_leg(args, os.environ)
            print(json.dumps(_jsonable(result), sort_keys=True))
        elif args.command == "quota-overlay":
            if args.action == "install":
                if not args.resets_at:
                    raise AcceptanceRefused("quota-overlay install requires --resets-at")
                result = install_quota_overlay(
                    state_dir=args.state_dir,
                    overlay_path=args.overlay_path,
                    canonical_path=args.canonical_path,
                    run_id=args.run_id,
                    resets_at=_parse_utc(args.resets_at, label="overlay reset"),
                )
            else:
                if not args.expected_sha256:
                    raise AcceptanceRefused(
                        "quota-overlay remove requires --expected-sha256"
                    )
                result = remove_quota_overlay(
                    state_dir=args.state_dir,
                    overlay_path=args.overlay_path,
                    canonical_path=args.canonical_path,
                    run_id=args.run_id,
                    expected_sha256=args.expected_sha256,
                )
            print(json.dumps(_jsonable(result), sort_keys=True))
        elif args.command == "consume-probe":
            result = consume_correlated_probe(
                inbox=args.inbox,
                message_id=args.message_id,
                correlation_id=args.correlation_id,
                run_id=args.run_id,
                gate_path=args.gate_path,
                timeout_seconds=args.timeout_seconds,
            )
            print(json.dumps(_jsonable(result), sort_keys=True))
            if not result.get("success"):
                return 4
        else:
            parser.error(f"unknown command: {args.command}")
    except AcceptanceSkipped as exc:
        print(f"SKIP: {exc}", file=sys.stderr)
        return 77
    except AcceptanceRefused as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
