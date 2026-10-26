"""Harness integration registry for runtime-controller orchestration."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Literal, Mapping, Protocol, TYPE_CHECKING

from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.claude import ClaudeHarnessAdapter, prepare_claude_launch
from tools.harness_adapter.codex import CodexHarnessAdapter, prepare_codex_launch
from tools.harness_adapter.types import (
    HarnessLaunchConfig,
    ReceiverHookReceipt,
    ReceiverStateProof,
    RuntimeReadinessProof,
    SuccessorEvidenceContract,
)
from tools.codex_telegram_actions import PriorRuntime
from tools.codex_telegram_actions import receiver_handoff_actions as _telegram_receiver_handoff_actions

if TYPE_CHECKING:
    from tools.runtime_controller import RuntimeHandle, TmuxRuntimeContext


class HarnessIntegration(Protocol):
    name: str
    communications_managed: bool

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        ...

    def validate_launch_config(self, launch: HarnessLaunchConfig) -> None:
        ...

    def prepare_launch(self, binding: Any, adapter: PantheonHarnessAdapter, *, cwd: Path) -> None:
        """Harness-specific seeding before the interactive launch (T-5011)."""
        ...

    def prove_runtime_readiness(
        self,
        binding: Any,
        handle: "RuntimeHandle",
        *,
        state_root: Path,
        context: "TmuxRuntimeContext | None",
    ) -> RuntimeReadinessProof:
        ...

    def successor_evidence_contract(
        self,
        persona: str,
        transition_id: str,
        evidence: Path,
    ) -> SuccessorEvidenceContract:
        ...

    def validate_successor_ack(self, state: Mapping[str, Any], evidence: str) -> None:
        ...

    def successor_read_observed(
        self, *, account_home: Path, cwd: Path | None, since: datetime, evidence_path: str,
        successor_nonce: str, cached_transcript: Path | None = None,
        search_from: datetime | None = None,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        ...

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        ...

    def prove_receiver_state(
        self,
        binding: Any,
        expected: Literal["present", "absent"],
        *,
        backend: Any | None = None,
    ) -> ReceiverStateProof:
        ...

    def run_receiver_handoff(
        self,
        binding: Any,
        direction: str,
        phase: Literal["pre", "post"],
        *,
        backend: Any | None = None,
        reason: str,
        prior_runtime: PriorRuntime = "live",
    ) -> ReceiverHookReceipt:
        ...

    def compensate_receiver_handoff(
        self,
        previous: Any,
        target: Any,
        direction: str,
        *,
        backend: Any | None = None,
        reason: str,
    ) -> ReceiverHookReceipt:
        ...


_INTEGRATIONS: dict[str, HarnessIntegration] = {}
_ALIASES = {"claude-code": "claude"}


def normalize_harness(value: str) -> str:
    key = value.strip().lower()
    normalized = _ALIASES.get(key, key)
    if normalized not in _INTEGRATIONS:
        raise ValueError(f"unsupported harness {value!r}")
    return normalized


def configured_harness_names() -> frozenset[str]:
    return frozenset(_INTEGRATIONS)


def register_integration(item: HarnessIntegration) -> None:
    if not item.name:
        raise ValueError("harness integration name is required")
    _INTEGRATIONS[item.name] = item


@contextlib.contextmanager
def registered_integration(item: HarnessIntegration) -> Iterator[None]:
    previous = _INTEGRATIONS.get(item.name)
    register_integration(item)
    try:
        yield
    finally:
        if previous is None:
            _INTEGRATIONS.pop(item.name, None)
        else:
            _INTEGRATIONS[item.name] = previous


def integration_for(name: str) -> HarnessIntegration:
    return _INTEGRATIONS[normalize_harness(name)]


@dataclass(frozen=True)
class SessionTranscript:
    """Where a harness keeps one session's native transcript (T-5016). The
    session-end Chronicle duty records this as a POINTER (path + size + hash);
    it never copies the transcript."""

    path: Path
    session_id: str
    #: How many files qualified (modified since the session start). More than
    #: one means the pick is AMBIGUOUS: the duty keeps the pointer but does not
    #: let it name the archive stem (review MINOR 2).
    candidates: int = 1


def claude_project_slug(cwd: Path) -> str:
    """Claude Code's project-dir name for a launch cwd (every non-alphanumeric
    character becomes '-': /home/a/pantheon-config -> -home-a-pantheon-config)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _newest_session_file(
    root: Path, pattern: str, *, since: datetime | None, recursive: bool = True
) -> tuple[Path | None, int]:
    """(newest file under ``root`` matching ``pattern``, how many matched),
    counting only files modified at or after ``since`` when given. ``recursive``
    False matches the top level only (Claude keeps subagent transcripts in
    nested dirs). (None, 0) on an absent root or no match."""
    if not root.is_dir():
        return None, 0
    floor = since.timestamp() if since is not None else None
    newest: Path | None = None
    newest_mtime = -1.0
    count = 0
    for candidate in (root.rglob(pattern) if recursive else root.glob(pattern)):
        try:
            if not candidate.is_file():
                continue
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if floor is not None and mtime < floor:
            continue
        count += 1
        if mtime > newest_mtime:
            newest, newest_mtime = candidate, mtime
    return newest, count



#: T-5028 (h) review MINOR 1: a fire never reads more than this much transcript.
TRANSCRIPT_READ_CAP_BYTES = 4 * 1024 * 1024
#: Days of ~/.codex/sessions/YYYY/MM/DD dirs scanned before a rollout is cached.
CODEX_SESSION_DAYS_CAP = 14
#: Read-shaped shell commands: the path must be one of their whole arguments.
_SHELL_READERS = frozenset({"cat", "head", "sed", "less", "more", "nl", "bat", "batcat", "view"})
#: Claude Code tools that read a file through `file_path`.
_CLAUDE_FILE_READ_TOOLS = frozenset({"Read"})
_CODEX_EXIT_RE = re.compile(r"Process exited with code (-?\d+)")


def _parse_record_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class _Slice:
    records: tuple[tuple[int, dict[str, Any]], ...]  # (line number within the slice, record)
    start_byte: int
    end_byte: int
    sha256: str


def _bounded_slice(path: Path, *, cap: int | None = None) -> _Slice | None:
    """The last `cap` (default TRANSCRIPT_READ_CAP_BYTES) bytes of `path`, from the
    first full line, parsed. Hashes the slice it read, never the whole file
    (review MINOR 1)."""
    cap = TRANSCRIPT_READ_CAP_BYTES if cap is None else cap
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - cap)
            handle.seek(start)
            data = handle.read(cap)
    except OSError:
        return None
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
        start = size - len(data)
    records = []
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append((number, value))
    return _Slice(tuple(records), start, size, hashlib.sha256(data).hexdigest())


def _first_record(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            value = json.loads(handle.readline(1024 * 1024) or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _same_path(candidate: Any, evidence_path: str, *, cwd: str | None = None) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    if not os.path.isabs(candidate):
        if not cwd:
            return False
        candidate = os.path.join(cwd, candidate)
    return os.path.normpath(candidate) == os.path.normpath(evidence_path)


def _separator(token: str) -> bool:
    return bool(token) and all(ch in ";|&\n()" for ch in token) and token not in ("&>", "&>>")


_STDOUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})


def _segment_reads(words: list[str], evidence_path: str, *, cwd: str | None) -> bool:
    """One simple command: a whole-file read of `evidence_path` to stdout (delta
    review): stdout not redirected, no in-place sed, no zero-length head, no
    partial read that skips the start (tail, sed -n not from line 1)."""
    words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
    if not words or os.path.basename(words[0]) not in _SHELL_READERS:
        return False
    program, args = os.path.basename(words[0]), words[1:]
    for index, word in enumerate(args):
        # stdout redirected anywhere (> file, >> file, > /dev/null, &>): not a read
        # the model saw. `2>` (stderr only) is fine.
        if word in _STDOUT_REDIRECTS and not (index > 0 and args[index - 1] == "2"):
            return False
    operands = [w for w in args if w != "<" and w not in _STDOUT_REDIRECTS]
    if program == "sed":
        if any(w == "--in-place" or w.startswith("--in-place=")
               or (w.startswith("-") and not w.startswith("--") and "i" in w[1:]) for w in args):
            return False
        quiet = any(w in ("-n", "--quiet", "--silent") or (w.startswith("-") and not w.startswith("--") and "n" in w[1:])
                    for w in args)
        scripts = [w for w in operands if not w.startswith("-") and not _same_path(w, evidence_path, cwd=cwd)]
        if quiet and not all(re.fullmatch(r"p|1p|1,\S*p", w) for w in scripts):
            return False
    if program == "head":
        for index, word in enumerate(args):
            value = None
            if word in ("-n", "-c", "--lines", "--bytes") and index + 1 < len(args):
                value = args[index + 1]
            elif re.fullmatch(r"-[nc]\S+", word):
                value = word[2:]
            elif word.startswith(("--lines=", "--bytes=")):
                value = word.split("=", 1)[1]
            if value is not None and re.fullmatch(r"\+?0+", value):
                return False
    return any(_same_path(w, evidence_path, cwd=cwd) for w in operands if not w.startswith("-"))


def shell_reads_path(command: Any, evidence_path: str, *, cwd: str | None = None) -> bool:
    """T-5028 (h) review MAJOR: True only when a simple command in `command` is a
    read-shaped program (cat/head/sed/less/...) given `evidence_path` as a WHOLE
    argument (relative paths resolved against `cwd`) and reading it to stdout from
    the start (see _segment_reads). A mention, an echo, or a substring
    (``<path>.bak``) is not a read."""
    if isinstance(command, list):
        tokens = [str(item) for item in command]
    elif isinstance(command, str):
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return False
    else:
        return False
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if _separator(token):
            if _segment_reads(segment, evidence_path, cwd=cwd):
                return True
            segment = []
            continue
        segment.append(token)
    return False


def _observation(path: Path, piece: _Slice, *, read_line: int, turn_line: int, kind: str) -> dict[str, Any]:
    return {"transcript": str(path), "slice_sha256": piece.sha256, "slice_start_byte": piece.start_byte,
            "slice_end_byte": piece.end_byte, "read_line_in_slice": read_line,
            "completed_turn_line_in_slice": turn_line, "kind": kind, "observed_by": "controller-transcript"}


def _contains_nonce(value: Any, nonce: str) -> bool:
    """Whether native tool output contains the exact transition nonce."""
    if isinstance(value, str):
        return nonce in value
    if isinstance(value, list):
        return any(_contains_nonce(item, nonce) for item in value)
    if isinstance(value, dict):
        return any(_contains_nonce(item, nonce) for item in value.values())
    return False


def claude_successor_read(
    path: Path, evidence_path: str, *, since: datetime, successor_nonce: str,
) -> dict[str, Any] | None:
    """T-5028 (h), Claude Code transcript (shapes captured 2026-10-17): a
    non-sidechain assistant ``tool_use`` after `since` that READS the evidence
    (Read with file_path equal to it, or Bash running a read-shaped command on it),
    whose paired ``tool_result`` (tool_use_id) is not ``is_error: true`` and
    contains the transition nonce, then a later assistant record with
    ``stop_reason: end_turn``. Missing result or nonce: no ack."""
    piece = _bounded_slice(path)
    if piece is None:
        return None
    pending: dict[str, int] = {}
    read_line = None
    for number, record in piece.records:
        if record.get("isSidechain") is True:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        kind = record.get("type")
        if read_line is None:
            at = _parse_record_ts(record.get("timestamp"))
            if kind == "assistant" and at is not None and at >= since:
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    offset = tool_input.get("offset")
                    from_start = offset is None or (isinstance(offset, int) and offset <= 1)
                    reads = (
                        (block.get("name") in _CLAUDE_FILE_READ_TOOLS and from_start
                         and _same_path(tool_input.get("file_path"), evidence_path))
                        or (block.get("name") == "Bash" and shell_reads_path(tool_input.get("command"), evidence_path))
                    )
                    if reads and isinstance(block.get("id"), str):
                        pending[block["id"]] = number
            elif kind == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                        if block.get("is_error") is not True and _contains_nonce(
                            block.get("content"), successor_nonce
                        ):
                            read_line = number
                            break
            continue
        if kind == "assistant" and message.get("stop_reason") == "end_turn":
            return _observation(path, piece, read_line=read_line, turn_line=number, kind="claude-read+end-turn")
    return None


def _exit_from_header(text: str) -> int | None:
    """The exit line of the output HEADER only (before the first "Output:" line),
    so command output that prints the phrase cannot fake it (delta review NIT)."""
    header = re.split(r"(?m)^Output:", text, maxsplit=1)[0]
    match = _CODEX_EXIT_RE.search(header)
    return int(match.group(1)) if match else None


def _codex_exit_code(output: Any) -> int | None:
    """The exit code a codex function_call_output records: the captured text form
    ("Process exited with code N"), or a JSON form with metadata.exit_code.
    None when absent or unparseable (no ack)."""
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            output = decoded
        else:
            return _exit_from_header(output)
    if isinstance(output, dict):
        metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
        code = metadata.get("exit_code", output.get("exit_code"))
        if isinstance(code, int):
            return code
        text = output.get("output")
        if isinstance(text, str):
            return _exit_from_header(text)
    return None


def _codex_native_output(output: Any) -> str | None:
    """Extract only native stdout from a captured Codex function result.

    The observed text form is a controller wrapper whose native stdout starts
    after an exact ``Output:`` line. The existing structured form carries an
    integer exit code plus a top-level ``output`` payload. Wrapper headers and
    metadata are never evidence that the successor saw continuity bytes.
    """
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return _codex_native_output(decoded)
        marker = re.search(r"(?m)^Output:\r?\n", output)
        return output[marker.end():] if marker is not None else None
    if not isinstance(output, dict):
        return None
    native = output.get("output")
    if not isinstance(native, str):
        return None
    marker = re.search(r"(?m)^Output:\r?\n", native)
    if marker is not None:
        return native[marker.end():]
    metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
    exit_code = metadata.get("exit_code", output.get("exit_code"))
    return native if isinstance(exit_code, int) else None


def codex_successor_read(
    path: Path, evidence_path: str, *, since: datetime, successor_nonce: str,
) -> dict[str, Any] | None:
    """T-5028 (h), codex rollout (shapes captured 2026-10-17): a ``response_item``
    ``function_call`` (exec_command; `arguments` a JSON string with cmd/workdir)
    after `since` whose command READS the evidence as a whole argument, whose
    paired ``function_call_output`` (call_id) records exit code 0 and contains
    the transition nonce, then a later ``event_msg`` ``task_complete``. Missing
    or unparseable output or nonce: no ack."""
    piece = _bounded_slice(path)
    if piece is None:
        return None
    pending: dict[str, int] = {}
    read_line = None
    for number, record in piece.records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        at = _parse_record_ts(record.get("timestamp"))
        if at is None or at < since:
            continue
        if read_line is None:
            if record.get("type") != "response_item":
                continue
            if payload.get("type") == "function_call" and isinstance(payload.get("call_id"), str):
                try:
                    arguments = json.loads(payload.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(arguments, dict):
                    continue
                command = arguments.get("cmd", arguments.get("command"))
                workdir = arguments.get("workdir") if isinstance(arguments.get("workdir"), str) else None
                if shell_reads_path(command, evidence_path, cwd=workdir):
                    pending[payload["call_id"]] = number
            elif payload.get("type") == "function_call_output" and payload.get("call_id") in pending:
                if _codex_exit_code(payload.get("output")) == 0 and _contains_nonce(
                    _codex_native_output(payload.get("output")), successor_nonce
                ):
                    read_line = number
            continue
        if record.get("type") == "event_msg" and payload.get("type") == "task_complete":
            return _observation(path, piece, read_line=read_line, turn_line=number, kind="codex-read+task-complete")
    return None


def _codex_top_level_rollouts(
    account_home: Path, *, since: datetime, now: datetime | None = None, search_from: datetime | None = None,
    newer_than: float | None = None,
) -> list[Path]:
    """Rollouts modified since `since`, in the YYYY/MM/DD dirs from `since` to now
    (at most CODEX_SESSION_DAYS_CAP days; never a whole-tree rglob), that are the
    seat's own interactive thread: session_meta without parent_thread_id
    (subagent/worker threads) and whose source is not "exec" (codex exec runs)."""
    from datetime import timedelta, timezone

    root = account_home / ".codex" / "sessions"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    # Delta review: from one day before the prompt (a UTC-midnight crossing) or the
    # incoming runtime's recorded start if earlier (an already-live successor).
    first = since - timedelta(days=1)
    if search_from is not None and search_from < first:
        first = search_from
    day = first.astimezone(timezone.utc).date()
    floor = since.timestamp() if newer_than is None else max(since.timestamp(), newer_than)
    found = []
    for _ in range(CODEX_SESSION_DAYS_CAP):
        if day > current:
            break
        folder = root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        day += timedelta(days=1)
        if not folder.is_dir():
            continue
        for candidate in folder.glob("rollout-*.jsonl"):
            try:
                if not candidate.is_file() or candidate.stat().st_mtime < floor:
                    continue
                if newer_than is not None and candidate.stat().st_mtime <= newer_than:
                    continue
            except OSError:
                continue
            meta = _first_record(candidate)
            payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
            if meta.get("type") == "session_meta" and (
                payload.get("parent_thread_id") or payload.get("source") == "exec"
                or payload.get("originator") == "codex_exec"
            ):
                continue
            found.append(candidate)
    return found


_CODEX_ROLLOUT_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session>.+)$")


class _BaseIntegration:
    communications_managed = True

    def validate_launch_config(self, launch: HarnessLaunchConfig) -> None:
        if launch.harness != self.name:
            raise ValueError(f"{launch.harness!r} does not match {self.name!r}")

    def prepare_launch(self, binding: Any, adapter: PantheonHarnessAdapter, *, cwd: Path) -> None:
        """No pre-launch seeding by default (T-5011)."""
        del binding, adapter, cwd

    def session_transcript(
        self, *, account_home: Path, started_at: datetime | None, cwd: Path | None = None
    ) -> SessionTranscript | None:
        """The native transcript of the session that started at ``started_at``
        (T-5016). No transcript surface is known by default."""
        del account_home, started_at, cwd
        return None

    def successor_read_observed(
        self, *, account_home: Path, cwd: Path | None, since: datetime, evidence_path: str,
        successor_nonce: str, cached_transcript: Path | None = None,
        search_from: datetime | None = None,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        """T-5028 (h): (the transcript it resolved, structural evidence that the
        successor read `evidence_path` and finished a turn after `since` or None).
        `cached_transcript` is the path an earlier fire resolved (reused while it
        exists). (None, None) by default: such a harness gets only the timeout."""
        del account_home, cwd, since, evidence_path, successor_nonce, cached_transcript, search_from
        return None, None

    def prove_runtime_readiness(self, binding: Any, handle: "RuntimeHandle", *, state_root: Path, context: "TmuxRuntimeContext | None") -> RuntimeReadinessProof:
        del context
        state_path = state_root / str(getattr(binding, "persona")) / "runtime-controller" / "runtime.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return RuntimeReadinessProof(False, {"reason": f"runtime state unavailable: {type(exc).__name__}"})
        expected_target = f"{handle.tmux.session}:{handle.tmux.window}.{handle.tmux.pane}"
        matches = (
            state.get("harness") == handle.profile.harness
            and state.get("profile") == handle.profile.profile
            and state.get("tmux_target") == expected_target
            and state.get("last_structural_event") == "runtime_started"
        )
        return RuntimeReadinessProof(
            ready=matches,
            evidence={
                "harness": str(state.get("harness", "")),
                "profile": str(state.get("profile", "")),
                "tmux_target": str(state.get("tmux_target", "")),
                "last_structural_event": str(state.get("last_structural_event", "")),
                "state_path": str(state_path),
            },
        )

    def successor_evidence_contract(self, persona: str, transition_id: str, evidence: Path) -> SuccessorEvidenceContract:
        return SuccessorEvidenceContract(
            expected=f"hook:runtime-transfer:{transition_id}:{evidence.name}",
            target=str(evidence),
            evidence_kind="runtime-transfer",
        )

    def validate_successor_ack(self, state: Mapping[str, Any], evidence: str) -> None:
        expected = state.get("expected_successor_read_evidence")
        if isinstance(expected, str) and expected and evidence != expected:
            raise ValueError("successor read acknowledgement does not match transition target")
        if state.get("trigger") == "operator":
            # T-4732: the 2026-08-14 target-live recovery recorded the handoff only as
            # successor_read_target (chronicle_evidence null). The hash check below still binds it.
            target = state.get("chronicle_evidence") or state.get("successor_read_target")
            expected_sha256 = state.get("operator_handoff_sha256")
            if not isinstance(target, str) or not target:
                raise ValueError("operator handoff target is missing")
            if not isinstance(expected_sha256, str) or not expected_sha256:
                raise ValueError("operator handoff hash is missing")
            try:
                actual = hashlib.sha256(Path(target).read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError("operator handoff target is unavailable") from exc
            if actual != expected_sha256:
                raise ValueError("operator handoff target hash mismatch")

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        if backend is not None and hasattr(backend, "prove_receiver_state"):
            proof = backend.prove_receiver_state(binding, expected)
            if isinstance(proof, ReceiverStateProof):
                return proof
        raise RuntimeError("receiver state proof requires an integration-specific probe")


class ClaudeIntegration(_BaseIntegration):
    name = "claude"

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        return ClaudeHarnessAdapter(
            binding.role,
            claude_home=harness_home,
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )

    def prepare_launch(self, binding: Any, adapter: PantheonHarnessAdapter, *, cwd: Path) -> None:
        # Keyed on the binding's harness, not the adapter type, exactly as the
        # controller's harness branch was (T-5011 PR-A).
        del adapter
        prepare_claude_launch(binding, cwd=cwd)

    def session_transcript(
        self, *, account_home: Path, started_at: datetime | None, cwd: Path | None = None
    ) -> SessionTranscript | None:
        """Claude Code: ``~/.claude/projects/<slug>/<session-id>.jsonl`` (T-5016).
        Scoped to the launch cwd's project slug when known; top level only, so
        subagent transcripts (nested under the session dir) never qualify."""
        projects = account_home / ".claude" / "projects"
        if cwd is not None:
            newest, count = _newest_session_file(
                projects / claude_project_slug(cwd), "*.jsonl", since=started_at, recursive=False)
        else:
            newest, count = _newest_session_file(projects, "*/*.jsonl", since=started_at, recursive=False)
        return SessionTranscript(newest, newest.stem, count) if newest is not None else None

    def successor_read_observed(
        self, *, account_home: Path, cwd: Path | None, since: datetime, evidence_path: str,
        successor_nonce: str, cached_transcript: Path | None = None,
        search_from: datetime | None = None,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        """T-5028 (h): only an unambiguous (one qualifying) top-level transcript counts."""
        del search_from
        found = None
        if cached_transcript is not None and cached_transcript.is_file():
            # Delta review: one glob of the project dir; a NEWER transcript than the
            # cached one means the cache went stale (the successor restarted).
            found = self.session_transcript(account_home=account_home, started_at=since, cwd=cwd)
            stale = (found is not None and found.path != cached_transcript
                     and found.path.stat().st_mtime > cached_transcript.stat().st_mtime)
            if not stale:
                return cached_transcript, claude_successor_read(
                    cached_transcript, evidence_path, since=since, successor_nonce=successor_nonce
                )
            newer = self.session_transcript(
                account_home=account_home,
                started_at=datetime.fromtimestamp(cached_transcript.stat().st_mtime + 1e-6, tz=since.tzinfo),
                cwd=cwd,
            )
            found = newer
        else:
            found = self.session_transcript(account_home=account_home, started_at=since, cwd=cwd)
        if found is None or found.candidates != 1:
            return None, None
        return found.path, claude_successor_read(
            found.path, evidence_path, since=since, successor_nonce=successor_nonce
        )

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        if current.harness == target.harness:
            return None
        return f"{current.harness}-to-{target.harness}"

    def run_receiver_handoff(self, binding: Any, direction: str, phase: Literal["pre", "post"], *, backend: Any | None = None, reason: str, prior_runtime: PriorRuntime = "live") -> ReceiverHookReceipt:
        return _run_telegram_receiver_handoff(binding, direction, phase, backend=backend, reason=reason, prior_runtime=prior_runtime)

    def compensate_receiver_handoff(self, previous: Any, target: Any, direction: str, *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        del target
        return _run_telegram_receiver_handoff(previous, direction, "post", backend=backend, reason=reason)

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        return _prove_telegram_receiver_state(binding, expected, backend=backend)


class CodexIntegration(_BaseIntegration):
    name = "codex"

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        return CodexHarnessAdapter(
            binding.role,
            codex_home=harness_home,
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )

    def prepare_launch(self, binding: Any, adapter: PantheonHarnessAdapter, *, cwd: Path) -> None:
        # Keyed on the binding's harness, not the adapter type, exactly as the
        # controller's harness branch was (T-5011 PR-A).
        del adapter
        prepare_codex_launch(binding, cwd=cwd)

    def session_transcript(
        self, *, account_home: Path, started_at: datetime | None, cwd: Path | None = None
    ) -> SessionTranscript | None:
        """Codex: ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl``
        (the codex_context_lifecycle.py shape) (T-5016)."""
        del cwd
        newest, count = _newest_session_file(
            account_home / ".codex" / "sessions", "rollout-*.jsonl", since=started_at)
        if newest is None:
            return None
        match = _CODEX_ROLLOUT_RE.match(newest.stem)
        session_id = match.group("session") if match else newest.stem
        return SessionTranscript(newest, session_id, count)

    def successor_read_observed(
        self, *, account_home: Path, cwd: Path | None, since: datetime, evidence_path: str,
        successor_nonce: str, cached_transcript: Path | None = None,
        search_from: datetime | None = None,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        """T-5028 (h): only an unambiguous (one top-level) rollout counts."""
        del cwd
        if cached_transcript is not None and cached_transcript.is_file():
            # Delta review: only top-level rollouts NEWER than the cached one are
            # opened (their first line), within the bounded day dirs.
            newer = [item for item in _codex_top_level_rollouts(
                account_home, since=since, search_from=search_from,
                newer_than=cached_transcript.stat().st_mtime) if item != cached_transcript]
            if not newer:
                return cached_transcript, codex_successor_read(
                    cached_transcript, evidence_path, since=since, successor_nonce=successor_nonce
                )
            rollouts = newer
        else:
            rollouts = _codex_top_level_rollouts(account_home, since=since, search_from=search_from)
        if len(rollouts) != 1:
            return None, None
        return rollouts[0], codex_successor_read(
            rollouts[0], evidence_path, since=since, successor_nonce=successor_nonce
        )

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        if current.harness == target.harness:
            return None
        return f"{current.harness}-to-{target.harness}"

    def run_receiver_handoff(self, binding: Any, direction: str, phase: Literal["pre", "post"], *, backend: Any | None = None, reason: str, prior_runtime: PriorRuntime = "live") -> ReceiverHookReceipt:
        return _run_telegram_receiver_handoff(binding, direction, phase, backend=backend, reason=reason, prior_runtime=prior_runtime)

    def compensate_receiver_handoff(self, previous: Any, target: Any, direction: str, *, backend: Any | None = None, reason: str) -> ReceiverHookReceipt:
        del target
        return _run_telegram_receiver_handoff(previous, direction, "post", backend=backend, reason=reason)

    def prove_receiver_state(self, binding: Any, expected: Literal["present", "absent"], *, backend: Any | None = None) -> ReceiverStateProof:
        return _prove_telegram_receiver_state(binding, expected, backend=backend)


class HermesIntegration(_BaseIntegration):
    """Runtime-controller integration for hermes (T-5005 H1; registered in
    T-5011 PR-B). The adapter module is imported lazily: it imports this module.
    """

    name = "hermes"
    communications_managed = False  # no bot; receive-only mail (plan D-35/D-36)

    def build_adapter(self, binding: Any, *, harness_home: Path) -> PantheonHarnessAdapter:
        from tools.harness_adapter.hermes import HermesHarnessAdapter

        return HermesHarnessAdapter(
            binding.role,
            account_home=harness_home,
            mailbox_path=binding.mailbox,
            launch=binding.launch,
        )

    def prepare_launch(self, binding: Any, adapter: PantheonHarnessAdapter, *, cwd: Path) -> None:
        """The adapter's own hook (plan §2.2 step 2): the hermes adapter renders
        the seat files there and fails closed without render inputs."""
        adapter.prepare_launch(binding, cwd=cwd)

    def session_transcript(
        self, *, account_home: Path, started_at: datetime | None, cwd: Path | None = None
    ) -> SessionTranscript | None:
        """Hermes keeps its sessions under ``$HERMES_HOME`` (``~/.hermes``); the
        exact layout is the hermes release's, so take the newest top-level file
        under ``~/.hermes/sessions`` and report None when there is none (T-5016).
        Several qualifying files make the pick ambiguous (``candidates``)."""
        del cwd
        newest, count = _newest_session_file(
            account_home / ".hermes" / "sessions", "*", since=started_at, recursive=False)
        return SessionTranscript(newest, newest.stem, count) if newest is not None else None

    def classify_mid_session_cap(self, text: str, *, now: Any) -> Any:
        """Plan §3.2 gap 4: the exhausted-prepaid-balance render is the hermes
        mid-session cap (``cap_kind="prepaid-balance"``, no reset)."""
        from tools.harness_adapter.hermes import classify_hermes_mid_session_cap

        return classify_hermes_mid_session_cap(text, now=now)

    def successor_evidence_contract(self, persona: str, transition_id: str, evidence: Path) -> SuccessorEvidenceContract:
        del persona
        return SuccessorEvidenceContract(
            expected=f"mailbox:runtime-transfer:{transition_id}:{evidence.name}",
            target=str(evidence),
            evidence_kind="mailbox-runtime-transfer",
        )

    def receiver_direction(self, current: HarnessLaunchConfig, target: HarnessLaunchConfig) -> str | None:
        del current, target
        return None  # no Telegram receiver to hand off

    def prove_receiver_state(
        self,
        binding: Any,
        expected: Literal["present", "absent"],
        *,
        backend: Any | None = None,
    ) -> ReceiverStateProof:
        del binding, expected, backend
        return ReceiverStateProof("not-applicable", {"harness": "hermes", "reason": "no-receiver"})

    def run_receiver_handoff(
        self,
        binding: Any,
        direction: str,
        phase: Literal["pre", "post"],
        *,
        backend: Any | None = None,
        reason: str,
        prior_runtime: PriorRuntime = "live",
    ) -> ReceiverHookReceipt:
        del binding, backend, reason, prior_runtime
        return ReceiverHookReceipt(True, (), {"direction": direction, "phase": phase, "harness": "hermes"})

    def compensate_receiver_handoff(
        self,
        previous: Any,
        target: Any,
        direction: str,
        *,
        backend: Any | None = None,
        reason: str,
    ) -> ReceiverHookReceipt:
        del previous, target, backend, reason
        return ReceiverHookReceipt(True, (), {"direction": direction, "harness": "hermes"})


def _telegram_actions(binding: Any, direction: str, phase: str, *, prior_runtime: PriorRuntime = "live") -> tuple[str, ...]:
    communications = getattr(binding, "communications", None)
    if isinstance(communications, Mapping):
        watchdog_units = tuple(communications.get("watchdog_units", ()))
        topology = str(communications.get("claude_receiver_topology", "systemd-user"))
    else:
        watchdog_units = tuple(getattr(binding, "watchdog_units", ()))
        topology = str(getattr(binding, "claude_receiver_topology", "systemd-user"))
    return _telegram_receiver_handoff_actions(
        claude_receiver_topology=topology,
        watchdog_units=watchdog_units,
        direction=direction,
        phase=phase,
        prior_runtime=prior_runtime,
    )


def _restore_watchdog_after_failed_pre(binding: Any, actions: tuple[str, ...], completed: list[str], backend: Any) -> None:
    del actions
    restored = False
    if "snapshot-claude-receiver" in completed:
        backend.execute("restore-claude-receiver", binding)
        restored = True
    if "snapshot-watchdog-units" in completed:
        backend.execute("restore-watchdog-units", binding)
        restored = True
    if restored:
        backend.execute("complete-receiver-topology-snapshot", binding)


def receiver_handoff_actions(binding: Any, direction: str, phase: str, *, prior_runtime: PriorRuntime = "live") -> tuple[str, ...]:
    return _telegram_actions(binding, direction, phase, prior_runtime=prior_runtime)


def run_receiver_handoff_actions(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    backend: Any,
    prior_runtime: PriorRuntime = "live",
) -> ReceiverHookReceipt:
    operations = []
    actions = _telegram_actions(binding, direction, phase, prior_runtime=prior_runtime)
    try:
        for action in actions:
            backend.execute(action, binding)
            operations.append(action)
    except Exception:
        if phase == "pre":
            _restore_watchdog_after_failed_pre(binding, actions, operations, backend)
        raise
    return ReceiverHookReceipt(True, tuple(operations), {"direction": direction, "phase": phase})


def _run_telegram_receiver_handoff(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    backend: Any | None,
    reason: str,
    prior_runtime: PriorRuntime = "live",
) -> ReceiverHookReceipt:
    if backend is None:
        return _telegram_handoff_subprocess(binding, direction, phase, reason=reason, prior_runtime=prior_runtime)
    return run_receiver_handoff_actions(binding, direction, phase, backend=backend, prior_runtime=prior_runtime)


def _telegram_bridge_tool() -> Path:
    return Path(__file__).resolve().parents[1] / "codex_telegram_bridge.py"


def _telegram_subprocess(binding: Any, *args: str, timeout_seconds: int = 90) -> Mapping[str, Any]:
    result = subprocess.run(
        (sys.executable, str(_telegram_bridge_tool()), *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram bridge returned invalid JSON") from exc
    if result.returncode != 0 or payload.get("ok") is False:
        detail = payload.get("error") or result.stderr.strip() or "Telegram bridge command failed"
        raise RuntimeError(str(detail))
    del binding
    return payload


def _prove_telegram_receiver_state(
    binding: Any,
    expected: Literal["present", "absent"],
    *,
    backend: Any | None,
) -> ReceiverStateProof:
    if backend is not None and hasattr(backend, "prove_receiver_state"):
        proof = backend.prove_receiver_state(binding, expected)
        if isinstance(proof, ReceiverStateProof):
            return proof
    kind = str(getattr(getattr(binding, "launch", None), "harness", ""))
    if backend is not None:
        method = getattr(backend, "_require_present" if expected == "present" else "_require_absent", None)
        if method is not None:
            method(binding, kind=kind)
            return ReceiverStateProof(expected, {"kind": kind, "backend": type(backend).__name__})
    payload = _telegram_subprocess(
        binding,
        "receiver-state",
        "--persona",
        str(getattr(binding, "persona")),
        "--kind",
        kind,
        "--expected",
        expected,
        "--json",
    )
    state = str(payload.get("state", "ambiguous"))
    evidence_raw = payload.get("evidence")
    evidence = {str(key): str(value) for key, value in evidence_raw.items()} if isinstance(evidence_raw, Mapping) else {}
    return ReceiverStateProof(state, evidence)  # type: ignore[arg-type]


def _telegram_handoff_subprocess(
    binding: Any,
    direction: str,
    phase: Literal["pre", "post"],
    *,
    reason: str,
    prior_runtime: PriorRuntime = "live",
) -> ReceiverHookReceipt:
    # T-4184: the flag is passed only when it differs from the bridge default,
    # so every existing caller's argv is byte-identical.
    extra = () if prior_runtime == "live" else ("--prior-runtime", prior_runtime)
    payload = _telegram_subprocess(
        binding,
        "handoff",
        "--persona",
        str(getattr(binding, "persona")),
        "--direction",
        direction,
        "--phase",
        phase,
        "--reason",
        reason,
        *extra,
        "--json",
    )
    operations = tuple(str(item) for item in payload.get("operations", ()))
    return ReceiverHookReceipt(True, operations, {"direction": direction, "phase": phase})


register_integration(ClaudeIntegration())
register_integration(CodexIntegration())
register_integration(HermesIntegration())
