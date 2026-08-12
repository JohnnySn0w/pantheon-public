"""Deterministic Pantheon runtime controller for persistent persona harnesses.

The controller owns process/tmux lifecycle and transition evidence.  It never
asks a model whether a quota decision is safe and never treats terminal pixels
as completion, acknowledgement, health, or quota authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, TYPE_CHECKING

from tools import codex_modal
from tools import graveyard
from tools.harness_adapter.codex import (
    ensure_model_migration_prompts_hidden,
    ensure_project_trusted,
    ensure_update_check_disabled,
)
from tools.harness_adapter.quota import next_weekly_reset
from tools.harness_adapter.types import (
    DeliveryReceipt,
    ExecutionRequest,
    HarnessLaunchConfig,
    PostStartCommand,
    TmuxTarget,
)
from tools.submit_verify import ready_for_submission

if TYPE_CHECKING:
    from tools.harness_adapter.base import PantheonHarnessAdapter
    from tools.persona_bringup import PersonaBinding


DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "pantheon" / "personas"
SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"
TMUX_RUNTIME_SCOPE_UNIT = "pantheon-runtime-tmux.scope"
RUNTIME_START_TIMEOUT_SECONDS = 8.0
RUNTIME_START_POLL_SECONDS = 0.1
RUNTIME_INPUT_READY_TIMEOUT_SECONDS = 20.0
RUNTIME_INPUT_READY_POLL_SECONDS = 0.25
POST_START_TIMEOUT_SECONDS = 5.0
RUNTIME_SOURCE_HASH_PATHS = (
    "instance.yaml",
    "tools/claude_quota_probe.py",
    "tools/claude_statusline_capture.py",
    "tools/graveyard.py",
    "tools/mailbox_wake.py",
    "tools/persona_bringup.py",
    "tools/runtime_controller.py",
    "tools/submit_verify.py",
    "tools/harness_adapter/base.py",
    "tools/harness_adapter/claude.py",
    "tools/harness_adapter/codex.py",
    "tools/harness_adapter/delivery.py",
    "tools/harness_adapter/diagnostics.py",
    "tools/harness_adapter/provider_quota.py",
    "tools/harness_adapter/quota.py",
    "tools/harness_adapter/runner.py",
    "tools/harness_adapter/types.py",
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/t2382_live_acceptance.py",
    "tools/run_t2382_live_acceptance.sh",
    "tools/test_t2382_live_acceptance.py",
)


@dataclass(frozen=True)
class RuntimeHandle:
    profile: HarnessLaunchConfig
    tmux: TmuxTarget
    session_id: str | None
    started_at: datetime


@dataclass(frozen=True)
class RuntimeSourceProvenance:
    revision: str
    source_hashes: Mapping[str, str]
    source_manifest_sha256: str
    runtime_start_boundary_at: datetime
    runtime_start_observed_at: datetime


@dataclass(frozen=True)
class RuntimeStart:
    scope_unit: str
    created_scope: bool


@dataclass(frozen=True)
class TmuxRuntimeContext:
    """Optional non-default tmux socket/scope target for the runtime plumbing.

    Absent (``None``) everywhere the controller operates against the default
    tmux socket and the ``TMUX_RUNTIME_SCOPE_UNIT`` singleton scope -- every
    existing caller is unaffected. Only off-prod/test call sites that need a
    disposable server construct one explicitly (T-2589): a faithful
    claude<->codex runtime swap can then be exercised on a disposable tmux
    scope instead of the one production default-socket server.
    """

    socket: str | None = None
    scope_unit: str = TMUX_RUNTIME_SCOPE_UNIT

    def tmux_socket_args(self) -> tuple[str, ...]:
        return ("-L", self.socket) if self.socket else ()


def _context_kwargs(context: TmuxRuntimeContext | None) -> dict[str, TmuxRuntimeContext]:
    """Forward ``context`` only when the caller actually supplied one.

    Several existing tests assert exact mock call signatures (e.g.
    ``assert_called_once_with(binding, mock.ANY, replace=False)``) that must
    not gain an unexpected ``context=None`` kwarg -- this keeps every
    existing call site byte-identical while still letting T-2589 callers
    that DO pass a context have it reach the tmux/systemd-run argv.
    """
    return {"context": context} if context is not None else {}


@dataclass(frozen=True)
class RuntimeInputReadiness:
    """Outcome of polling a runtime pane for input readiness (T-2382).

    ``pane_sha256`` is a hash of the captured pane, never the raw text —
    controller evidence never inlines raw terminal content.
    """

    ready: bool
    pane_sha256: str
    reason: str


@dataclass(frozen=True)
class SwapReceipt:
    trigger: Literal["operator", "planned-quota", "quota-reset", "forced-cap"]
    outgoing_profile: str
    incoming_profile: str
    handoff_kind: Literal["model-authored", "forced-factual"]
    handoff_or_graveyard: Path
    completed_at: datetime


class ProviderQuotaExhausted(RuntimeError):
    """Authoritative provider-cap signal; mailbox retries it without DLQ count."""

    mailbox_transient = True

    def __init__(
        self,
        message: str,
        *,
        profile: str | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.profile = profile
        self.provider_error = provider_error or message


class RuntimeTransitionPending(RuntimeError):
    """A durable transition barrier prevented this record from submission."""

    mailbox_transient = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_root(state_root: Path | None = None) -> Path:
    if state_root is not None:
        return state_root
    configured = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    return Path(configured) if configured else DEFAULT_STATE_ROOT


def _persona_state(persona: str, state_root: Path | None = None) -> Path:
    return _state_root(state_root) / persona / "runtime-controller"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"source provenance path is missing or not regular: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest_hash(source_hashes: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical_bytes(source_hashes)).hexdigest()


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repo_root), *args),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _controller_source_provenance(
    *,
    runtime_start_boundary_at: datetime,
    runtime_start_observed_at: datetime,
    repo_root: Path | None = None,
) -> RuntimeSourceProvenance:
    candidate = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    top = _run_git(candidate, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(top.stderr.strip() or "cannot inspect controller git top-level")
    observed_top = Path(top.stdout.strip()).resolve()
    if observed_top != candidate:
        raise RuntimeError(
            f"controller source root mismatch: expected {candidate}, got {observed_top}"
        )

    revision = _run_git(candidate, "rev-parse", "--verify", "HEAD")
    status = _run_git(
        candidate, "status", "--porcelain=v1", "--untracked-files=all"
    )
    for result, label in ((revision, "HEAD"), (status, "status")):
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"cannot inspect controller {label}")
    if status.stdout.strip():
        raise RuntimeError("controller source provenance requires a clean git worktree")

    observed_revision = revision.stdout.strip()
    if len(observed_revision) != 40 or any(
        char not in "0123456789abcdef" for char in observed_revision
    ):
        raise RuntimeError("controller source revision is not a full lowercase SHA-1")
    source_hashes = {
        relative: _sha256_file(candidate / relative)
        for relative in RUNTIME_SOURCE_HASH_PATHS
    }
    return RuntimeSourceProvenance(
        revision=observed_revision,
        source_hashes=source_hashes,
        source_manifest_sha256=_source_manifest_hash(source_hashes),
        runtime_start_boundary_at=runtime_start_boundary_at,
        runtime_start_observed_at=runtime_start_observed_at,
    )


def _target_text(target: TmuxTarget) -> str:
    pane = str(target.pane)
    if ":" in pane:
        return pane
    return f"{target.session}:{target.window}.{pane}"


def runtime_target_text(target: TmuxTarget) -> str:
    """Return the stable operator-visible tmux target for a runtime profile."""

    return _target_text(target)


def _evidence_log_path(
    persona: str,
    state_root: Path | None = None,
    *,
    name: str = "controller-evidence.jsonl",
) -> Path:
    return _persona_state(persona, state_root) / name


def _append_controller_evidence(
    persona: str,
    event: str,
    payload: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> None:
    transition = transition_state(persona, state_root=state_root)
    record = {
        "schema": "pantheon-runtime-controller-evidence/v1",
        "event": event,
        "persona": persona,
        "recorded_at": _iso(_utc_now()),
        "transition_id": transition.get("transition_id"),
        **payload,
    }
    _append_jsonl(_evidence_log_path(persona, state_root), record)


def _new_transition_id() -> str:
    return uuid.uuid4().hex


def _successor_read_evidence(evidence: Path, *, transition_id: str) -> str:
    return f"hook:runtime-transfer:{transition_id}:{evidence.name}"


def _run_tmux(
    *args: str,
    check: bool = False,
    context: TmuxRuntimeContext | None = None,
) -> subprocess.CompletedProcess[str]:
    socket_args = context.tmux_socket_args() if context is not None else ()
    return subprocess.run(
        ["tmux", *socket_args, *args],
        check=check,
        capture_output=True,
        text=True,
        shell=False,
    )


def _runtime_command_argv(
    argv: tuple[str, ...], env: dict[str, str] | Any
) -> tuple[str, ...]:
    environment = tuple(f"{key}={value}" for key, value in sorted(dict(env).items()))
    return ("/usr/bin/env", "--", *environment, *argv)


def _runtime_payload(
    binding: "PersonaBinding",
    *,
    session_id: str | None,
    started_at: datetime,
    runtime_scope_unit: str = TMUX_RUNTIME_SCOPE_UNIT,
    source_provenance: RuntimeSourceProvenance | None = None,
) -> dict[str, Any]:
    launch = binding.launch
    target = binding.controller.tmux
    payload: dict[str, Any] = {
        "schema": "pantheon-runtime-state/v1",
        "persona": binding.persona,
        "role": binding.role,
        "harness": launch.harness,
        "profile": launch.profile,
        "executable": str(launch.executable),
        "model": launch.model,
        "tmux_target": _target_text(target),
        "runtime_scope_unit": runtime_scope_unit,
        "session_id": session_id,
        "started_at": _iso(started_at),
        "last_structural_event": "runtime_started",
        "last_structural_event_at": _iso(started_at),
    }
    if source_provenance is not None:
        payload.update(
            {
                "controller_source_revision": source_provenance.revision,
                "controller_source_hashes": dict(source_provenance.source_hashes),
                "controller_source_manifest_sha256": (
                    source_provenance.source_manifest_sha256
                ),
                "runtime_start_boundary_at": _iso(
                    source_provenance.runtime_start_boundary_at
                ),
                "runtime_start_observed_at": _iso(
                    source_provenance.runtime_start_observed_at
                ),
            }
        )
    return payload


def _handle_from_state(binding: "PersonaBinding", state: dict[str, Any]) -> RuntimeHandle:
    raw_started = state.get("started_at")
    try:
        started = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
    except ValueError:
        started = _utc_now()
    raw_session = state.get("session_id")
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=raw_session if isinstance(raw_session, str) else None,
        started_at=started,
    )


def _state_matches(
    binding: "PersonaBinding",
    state: dict[str, Any],
    *,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    launch = binding.launch
    expected_scope_unit = context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT
    return all(
        (
            state.get("persona") == binding.persona,
            state.get("role") == binding.role,
            state.get("harness") == launch.harness,
            state.get("profile") == launch.profile,
            state.get("executable") == str(launch.executable),
            state.get("model") == launch.model,
            state.get("tmux_target") == _target_text(binding.controller.tmux),
            state.get("runtime_scope_unit") == expected_scope_unit,
        )
    )


def _pane_exists(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message", "-p", "-t", _target_text(target), "#{pane_dead}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and result.stdout.strip() == "0"


def _target_known(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message", "-p", "-t", _target_text(target), "#{pane_id}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _session_exists(session: str, *, context: TmuxRuntimeContext | None = None) -> bool:
    return _run_tmux("has-session", "-t", session, **_context_kwargs(context)).returncode == 0


def _window_exists(target: TmuxTarget, *, context: TmuxRuntimeContext | None = None) -> bool:
    result = _run_tmux(
        "display-message",
        "-p",
        "-t",
        f"{target.session}:{target.window}",
        "#{window_id}",
        **_context_kwargs(context),
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _default_tmux_server_pid(*, context: TmuxRuntimeContext | None = None) -> int | None:
    """Return the default-socket tmux server PID without starting a server."""

    result = _run_tmux("-N", "list-sessions", "-F", "#{pid}", **_context_kwargs(context))
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).lower()
        no_server = (
            detail.startswith("no server running on ")
            or (
                detail.startswith("error connecting to ")
                and "no such file or directory" in detail
            )
        )
        if no_server:
            return None
        raise RuntimeError(detail or "cannot inspect default tmux server")
    raw_pids = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not raw_pids:
        raise RuntimeError("default tmux server returned no session PID evidence")
    if len(raw_pids) != 1:
        raise RuntimeError("default tmux socket reported multiple server PIDs")
    raw_pid = next(iter(raw_pids))
    if not raw_pid.isdigit() or int(raw_pid) <= 0:
        raise RuntimeError("default tmux server returned malformed PID evidence")
    return int(raw_pid)


def _pid_in_scope(pid: int, scope_unit: str, *, proc_root: Path = Path("/proc")) -> bool:
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(scope_unit in line.partition("::")[2].split("/") for line in lines)


def _run_systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _scope_state(scope_unit: str) -> tuple[str, str]:
    result = _run_systemctl_user(
        "show", "--property=LoadState", "--property=ActiveState", scope_unit
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "scope inspection failed"
        raise RuntimeError(detail)
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    return values.get("LoadState", ""), values.get("ActiveState", "")


def _prepare_runtime_scope(scope_unit: str) -> None:
    load_state, active_state = _scope_state(scope_unit)
    if active_state == "active":
        raise RuntimeError("runtime tmux scope is active without its expected server")
    if active_state == "failed" or load_state == "loaded":
        result = _run_systemctl_user("reset-failed", scope_unit)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "scope reset failed"
            raise RuntimeError(detail)


def _scope_launch_path(account_home: Path) -> str:
    """PATH override for the ``systemd-run --scope`` control-plane launch.

    ``--scope`` inherits the caller's execution environment (confirmed
    against ``systemd-run(1)`` and empirically: a live ``--setenv=PATH=``
    scope launch prints exactly the overridden value, with other inherited
    vars such as ``HOME`` passing through untouched). The persona-consume
    service's own PATH has no ``~/.bun/bin``, so any bun-based MCP plugin
    (e.g. telegram) hits ENOENT (T-2624 class). Replacing PATH here --
    rather than prepending onto whatever the caller inherited -- matches
    the existing project convention for this exact bug class (see
    ``persona_cutover_resolve.py``'s ``apply_path``). Persona-general by
    construction: derived only from *account_home*, never hardcoded to one
    account.
    """
    return (
        f"{account_home}/.bun/bin:{account_home}/.npm-global/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )


def _start_new_server_in_scope(
    target: TmuxTarget,
    *,
    cwd: Path,
    command_argv: tuple[str, ...],
    scope_unit: str,
    account_home: Path,
    context: TmuxRuntimeContext | None = None,
) -> subprocess.CompletedProcess[str]:
    socket_args = context.tmux_socket_args() if context is not None else ()
    return subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={scope_unit}",
            f"--setenv=PATH={_scope_launch_path(account_home)}",
            "tmux",
            *socket_args,
            "new-session",
            "-d",
            "-s",
            target.session,
            "-n",
            target.window,
            "-c",
            str(cwd),
            *command_argv,
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _stop_created_scope(scope_unit: str) -> None:
    _run_systemctl_user("stop", scope_unit)
    _run_systemctl_user("reset-failed", scope_unit)


def _wait_for_live_runtime(
    target: TmuxTarget,
    scope_unit: str,
    *,
    timeout: float = RUNTIME_START_TIMEOUT_SECONDS,
    context: TmuxRuntimeContext | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _pane_exists(target, **_context_kwargs(context)):
            try:
                server_pid = _default_tmux_server_pid(**_context_kwargs(context))
            except RuntimeError:
                server_pid = None
            if server_pid is not None and _pid_in_scope(server_pid, scope_unit):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RUNTIME_START_POLL_SECONDS)


def _start_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    replace: bool,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeStart:
    target = binding.controller.tmux
    spec = adapter.build_interactive_launch(target)
    if binding.launch.harness == "codex":
        # TRUST modal -> config, never a keystroke (T-2594). spec.cwd is the
        # ACTUAL resolved launch cwd, never a hardcoded guess -- see
        # tools.harness_adapter.codex.ensure_project_trusted's own docstring.
        ensure_project_trusted(binding.account_home / ".codex", spec.cwd)
        # Update-picker/banner suppression (T-2597 wedges #2/#3) -- same
        # config-seed path as the trust fix above.
        ensure_update_check_disabled(binding.account_home / ".codex")
        # Model-migration prompt suppression for known-hideable active
        # presets (T-2597 wedge #4) -- see ensure_model_migration_prompts_
        # hidden's own docstring for the documented residual gap.
        ensure_model_migration_prompts_hidden(binding.account_home / ".codex")
    command_argv = _runtime_command_argv(spec.argv, spec.env)
    scope_unit = context.scope_unit if context is not None else TMUX_RUNTIME_SCOPE_UNIT
    server_pid = _default_tmux_server_pid(**_context_kwargs(context))
    if server_pid is None:
        if replace:
            raise RuntimeError("cannot replace a tmux target without a live server")
        _prepare_runtime_scope(scope_unit)
        result = _start_new_server_in_scope(
            target,
            cwd=spec.cwd,
            command_argv=command_argv,
            scope_unit=scope_unit,
            account_home=binding.account_home,
            **_context_kwargs(context),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "tmux launch failed"
            raise RuntimeError(detail)
        return RuntimeStart(scope_unit=scope_unit, created_scope=True)
    if not _pid_in_scope(server_pid, scope_unit):
        raise RuntimeError(
            "default tmux server is not owned by the Pantheon runtime scope"
        )
    if replace:
        result = _run_tmux(
            "respawn-pane",
            "-k",
            "-t",
            _target_text(target),
            "-c",
            str(spec.cwd),
            *command_argv,
            **_context_kwargs(context),
        )
    elif _target_known(target, **_context_kwargs(context)):
        result = _run_tmux(
            "respawn-pane",
            "-k",
            "-t",
            _target_text(target),
            "-c",
            str(spec.cwd),
            *command_argv,
            **_context_kwargs(context),
        )
    elif _session_exists(target.session, **_context_kwargs(context)):
        if _window_exists(target, **_context_kwargs(context)):
            raise RuntimeError(
                "stable tmux window exists but its configured pane target is missing"
            )
        result = _run_tmux(
            "new-window",
            "-d",
            "-t",
            target.session,
            "-n",
            target.window,
            "-c",
            str(spec.cwd),
            *command_argv,
            **_context_kwargs(context),
        )
    else:
        result = _run_tmux(
            "new-session",
            "-d",
            "-s",
            target.session,
            "-n",
            target.window,
            "-c",
            str(spec.cwd),
            *command_argv,
            **_context_kwargs(context),
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "tmux launch failed"
        raise RuntimeError(detail)
    return RuntimeStart(scope_unit=scope_unit, created_scope=False)


def _verify_started_runtime(
    binding: "PersonaBinding",
    started: RuntimeStart,
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> None:
    if _wait_for_live_runtime(
        binding.controller.tmux, started.scope_unit, **_context_kwargs(context)
    ):
        return
    if started.created_scope:
        _stop_created_scope(started.scope_unit)
    _append_controller_evidence(
        binding.persona,
        "runtime_start_failed",
        {
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "tmux_target": _target_text(binding.controller.tmux),
            "runtime_scope_unit": started.scope_unit,
            "created_scope": started.created_scope,
            "reason": "runtime pane/server scope verification timed out",
        },
        state_root=state_root,
    )
    raise RuntimeError("runtime failed live tmux server scope verification")


def _capture_visible_runtime_pane(
    target: TmuxTarget, *, context: TmuxRuntimeContext | None = None
) -> str:
    """Capture the VISIBLE pane only — no scrollback.

    ``capture-pane -p`` with no ``-S``/``-E`` range defaults to the visible
    screen, which is exactly what a readiness check needs: scrollback can
    hold an idle prompt from a PRIOR turn while the live composer is still
    mid-draw, and matching against it would false-green.
    """
    result = _run_tmux(
        "capture-pane", "-p", "-t", _target_text(target), **_context_kwargs(context)
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _wait_for_runtime_input_ready(
    target: TmuxTarget,
    *,
    harness: str = "claude",
    timeout: float = RUNTIME_INPUT_READY_TIMEOUT_SECONDS,
    poll_interval: float = RUNTIME_INPUT_READY_POLL_SECONDS,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeInputReadiness:
    """Poll the runtime pane until it accepts input or the deadline passes.

    Mirrors _wait_for_live_runtime's monotonic-deadline poll shape. Evidence
    always carries a hash of the captured pane, never the raw text.

    T-2594: when ``harness == "codex"``, also dismisses codex's
    update-available modal (at most once) via the single shared
    ``tools.codex_modal`` path -- the SAME precise, cursor-reverified
    navigation ``tools.bringup_acceptance.harness_codex_ready`` proved live.
    T-2599: same treatment for codex's blocking model-migration modal (at
    most once) -- a SEPARATE modal that can appear later in the same
    startup sequence (codex runs the update check before app-server start,
    the migration prompt after bootstrap; see ``codex_modal``'s own module
    docstring), so both dismissal branches share one poll loop rather than
    being mutually exclusive. Structurally codex-gated: for any other
    harness (``"claude"`` by default) ``codex_gated`` is False, so NEITHER
    modal-check branch's body -- including the ``send_keys`` closures below
    -- is ever entered, and ``_run_tmux(..., "send-keys", ...)`` is never
    reachable from this function. A claude binding cannot keystroke a pane
    through this path.
    """
    codex_gated = harness == "codex"
    deadline = time.monotonic() + timeout
    dismissed_update_modal = False
    dismissed_migration_modal = False
    pane_text = ""
    while True:
        pane_text = _capture_visible_runtime_pane(target, **_context_kwargs(context))
        pane_sha256 = hashlib.sha256(pane_text.encode("utf-8")).hexdigest()
        if ready_for_submission(pane_text):
            return RuntimeInputReadiness(
                ready=True,
                pane_sha256=pane_sha256,
                reason="runtime input readiness confirmed",
            )

        def _send_key(key: str) -> None:
            _run_tmux(
                "send-keys", "-t", _target_text(target), key,
                **_context_kwargs(context),
            )

        def _recapture() -> str:
            return _capture_visible_runtime_pane(target, **_context_kwargs(context))

        if (
            codex_gated
            and not dismissed_update_modal
            and codex_modal.is_codex_update_modal(pane_text)
        ):
            try:
                codex_modal.dismiss_codex_update_modal(
                    capture=_recapture, send_keys=_send_key, sleep=time.sleep,
                )
            except codex_modal.ModalNavigationFailed as exc:
                return RuntimeInputReadiness(
                    ready=False,
                    pane_sha256=pane_sha256,
                    reason=f"codex update-modal dismissal failed: {exc}",
                )
            dismissed_update_modal = True
            continue
        if (
            codex_gated
            and not dismissed_migration_modal
            and codex_modal.is_codex_model_migration_modal(pane_text)
        ):
            try:
                codex_modal.dismiss_codex_model_migration_modal(
                    capture=_recapture, send_keys=_send_key, sleep=time.sleep,
                )
            except codex_modal.ModalNavigationFailed as exc:
                return RuntimeInputReadiness(
                    ready=False,
                    pane_sha256=pane_sha256,
                    reason=f"codex model-migration-modal dismissal failed: {exc}",
                )
            dismissed_migration_modal = True
            continue
        if time.monotonic() >= deadline:
            return RuntimeInputReadiness(
                ready=False,
                pane_sha256=pane_sha256,
                reason="runtime input readiness timed out",
            )
        time.sleep(poll_interval)


def _verify_runtime_input_ready(
    binding: "PersonaBinding",
    started: RuntimeStart,
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> None:
    readiness = _wait_for_runtime_input_ready(
        binding.controller.tmux, harness=binding.launch.harness, **_context_kwargs(context)
    )
    if readiness.ready:
        return
    if started.created_scope:
        _stop_created_scope(started.scope_unit)
    _append_controller_evidence(
        binding.persona,
        "runtime_input_not_ready",
        {
            "harness": binding.launch.harness,
            "profile": binding.launch.profile,
            "tmux_target": _target_text(binding.controller.tmux),
            "runtime_scope_unit": started.scope_unit,
            "created_scope": started.created_scope,
            "pane_sha256": readiness.pane_sha256,
            "reason": readiness.reason,
        },
        state_root=state_root,
    )
    raise RuntimeError("runtime failed input readiness verification")


def _clear_stale_runtime_state(persona: str, *, state_root: Path | None) -> None:
    """Overwrite runtime.json with an empty record.

    Called only from the _replace_runtime failure path: a failed replacement
    means the respawn/respawn-pane already tore down the prior pane, so the
    OLD runtime.json now describes a runtime that no longer exists. Clearing
    it fail-closed means a later ensure_runtime never inherits stale
    "healthy" metadata instead of starting fresh.
    """
    _atomic_json(_persona_state(persona, state_root) / "runtime.json", {})


def _run_post_start_commands(
    binding: "PersonaBinding",
    *,
    state_root: Path | None,
) -> None:
    """Run the binding's declared post-runtime-start commands (T-2606 / PRE-4).

    Called once per actual runtime start -- first start AND every restart
    alike, since the caller (``ensure_runtime``) only reaches this from the
    branch that just called ``_start_runtime`` -- never on a plain reuse of
    an already-live pane. Persona-neutral: every command, and whether the
    binding has any at all, comes from the manifest via
    ``binding.controller.post_start``; this function holds no Recovery/persona
    hardcoding. An empty ``post_start`` (today's default for every role)
    makes this a no-op loop -- zero subprocess calls, zero evidence writes,
    byte-identical to bringup before this hook existed.

    FAIL-OPEN BY CONTRACT (matches recovery-bot-supervise.sh's ``|| true`` on
    both the pane-log tap and the backgrounded boot-kick it replaces): any
    exception raised while substituting, launching, or waiting on a command
    is caught here and recorded as evidence, never re-raised -- a broken
    post-start command must not fail bringup or kill the runtime that was
    just verified live and input-ready. Foreground commands are additionally
    bounded by ``POST_START_TIMEOUT_SECONDS`` -- a hang is not just an error
    recovery-bot-supervise.sh never had to guard against, since the SPOF guard
    this hook exists to preserve must not itself become a SPOF. Background
    commands are launched via ``Popen`` and never waited on, matching
    recovery_boot_kick.sh's backgrounded invocation today.
    """
    if not binding.controller.post_start:
        return
    target_text = _target_text(binding.controller.tmux)
    for command in binding.controller.post_start:
        try:
            argv = [
                token.format(
                    persona=binding.persona,
                    tmux_target=target_text,
                    account_home=str(binding.account_home),
                )
                for token in command.argv
            ]
        except (KeyError, ValueError) as exc:
            _append_controller_evidence(
                binding.persona,
                "post_start_command_skipped",
                {
                    "argv_template": " ".join(command.argv),
                    "reason": f"substitution failed: {type(exc).__name__}: {exc}",
                },
                state_root=state_root,
            )
            continue
        try:
            if command.background:
                subprocess.Popen(
                    argv,
                    cwd=binding.account_home,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                outcome = "launched_background"
            else:
                result = subprocess.run(
                    argv,
                    cwd=binding.account_home,
                    capture_output=True,
                    text=True,
                    timeout=POST_START_TIMEOUT_SECONDS,
                    check=False,
                )
                outcome = "ok" if result.returncode == 0 else f"exit_{result.returncode}"
        except Exception as exc:  # noqa: BLE001 -- fail-open by contract, see docstring
            outcome = f"error: {type(exc).__name__}: {exc}"
        _append_controller_evidence(
            binding.persona,
            "post_start_command",
            {
                "argv": " ".join(argv),
                "background": str(command.background).lower(),
                "outcome": outcome,
            },
            state_root=state_root,
        )


def ensure_runtime(
    binding: "PersonaBinding",
    adapter: "PantheonHarnessAdapter",
    *,
    state_root: Path | None = None,
) -> RuntimeHandle:
    """Reuse an exact matching live pane or start the selected interactive profile."""

    transition = transition_state(binding.persona, state_root=state_root)
    if (
        transition.get("breaker_open") is True
        and transition.get("capped_profile") == binding.launch.profile
    ):
        _append_controller_evidence(
            binding.persona,
            "breaker_blocked_capped_runtime",
            {
                "harness": binding.launch.harness,
                "provider": binding.launch.harness,
                "profile": binding.launch.profile,
                "capped_profile": transition.get("capped_profile"),
                "tmux_target": _target_text(binding.controller.tmux),
            },
            state_root=state_root,
        )
        raise ProviderQuotaExhausted(
            f"runtime profile {binding.launch.profile} has an open quota breaker",
            profile=binding.launch.profile,
        )
    state_path = _persona_state(binding.persona, state_root) / "runtime.json"
    state = _read_json(state_path)
    live = _pane_exists(binding.controller.tmux)
    if live:
        if not state or not _state_matches(binding, state):
            raise RuntimeError(
                "live tmux pane does not match the selected persona runtime profile"
            )
        server_pid = _default_tmux_server_pid()
        if server_pid is None or not _pid_in_scope(
            server_pid, str(state["runtime_scope_unit"])
        ):
            raise RuntimeError("live tmux server is not bound to its recorded scope")
        return _handle_from_state(binding, state)
    if state and _state_matches(binding, state):
        # A dead pane cannot inherit health from matching persisted metadata.
        state = {}
    boundary = _utc_now()
    runtime_start = _start_runtime(binding, adapter, replace=False)
    _verify_started_runtime(binding, runtime_start, state_root=state_root)
    _verify_runtime_input_ready(binding, runtime_start, state_root=state_root)
    _run_post_start_commands(binding, state_root=state_root)
    started = _utc_now()
    provenance = _controller_source_provenance(
        runtime_start_boundary_at=boundary,
        runtime_start_observed_at=started,
    )
    _atomic_json(
        state_path,
        _runtime_payload(
            binding,
            session_id=None,
            started_at=started,
            runtime_scope_unit=runtime_start.scope_unit,
            source_provenance=provenance,
        ),
    )
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=started,
    )


def recorded_runtime_handle(
    binding: "PersonaBinding",
    *,
    state_root: Path | None = None,
) -> RuntimeHandle:
    """Describe the selected runtime without starting or submitting to it.

    Forced-cap handling needs the last structural session identifier but must
    not invoke a provider that has already returned an authoritative limit.
    Missing state therefore produces a conservative synthetic handle; present
    state must match the manifest-selected runtime exactly.
    """

    state = _read_json(_persona_state(binding.persona, state_root) / "runtime.json")
    if state:
        if not _state_matches(binding, state):
            raise RuntimeError(
                "recorded runtime does not match the selected persona runtime profile"
            )
        return _handle_from_state(binding, state)
    return RuntimeHandle(
        profile=binding.launch,
        tmux=binding.controller.tmux,
        session_id=None,
        started_at=_utc_now(),
    )


def submit_to_runtime(
    handle: RuntimeHandle,
    request: ExecutionRequest,
    *,
    persona: str | None = None,
    state_root: Path | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    context: TmuxRuntimeContext | None = None,
) -> DeliveryReceipt:
    """Transport a prompt into the pane without claiming turn completion/ack."""

    harness = "claude-code" if handle.profile.harness == "claude" else "codex"
    evidence_context = {
        "harness": handle.profile.harness,
        "provider": handle.profile.harness,
        "profile": handle.profile.profile,
        "tmux_target": _target_text(handle.tmux),
        "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        "message_id": message_id,
        "correlation_id": correlation_id,
    }
    if persona is not None:
        transition = transition_state(persona, state_root=state_root)
        if (
            transition.get("breaker_open") is True
            and transition.get("capped_profile") == handle.profile.profile
        ):
            _append_controller_evidence(
                persona,
                "runtime_submission_blocked",
                {
                    **evidence_context,
                    "capped_profile": transition.get("capped_profile"),
                },
                state_root=state_root,
            )
            raise ProviderQuotaExhausted(
                f"runtime profile {handle.profile.profile} has an open quota breaker",
                profile=handle.profile.profile,
            )
        _append_controller_evidence(
            persona,
            "runtime_submission_attempt",
            evidence_context,
            state_root=state_root,
        )
    helper_argv = [str(SEND_KEYS_HELPER), "--harness", harness]
    if context is not None and context.socket:
        helper_argv += ["--socket", context.socket]
    helper_argv += [_target_text(handle.tmux), request.prompt]
    result = subprocess.run(
        helper_argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "prompt submission was not verified"
        if persona is not None:
            _append_controller_evidence(
                persona,
                "runtime_submission_failed",
                {
                    **evidence_context,
                    "returncode": result.returncode,
                    "detail": detail[:1000],
                },
                state_root=state_root,
            )
        raise RuntimeError(detail)
    if persona is not None:
        _append_controller_evidence(
            persona,
            "runtime_submission_accepted",
            {
                **evidence_context,
                "returncode": result.returncode,
            },
            state_root=state_root,
        )
    return DeliveryReceipt(
        harness=handle.profile.harness,
        correlation_id="",
        state="accepted",
        mechanism="tmux-submit-verify",
        retryable=False,
        evidence={
            "tmux_target": _target_text(handle.tmux),
            "transport_verified": "true",
            "turn_started": "unverified",
            "acked": "false",
        },
    )


def new_drain_id() -> str:
    return uuid.uuid4().hex


def _transition_path(persona: str, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "transition.json"


def transition_state(persona: str, *, state_root: Path | None = None) -> dict[str, Any]:
    return _read_json(_transition_path(persona, state_root))


def clear_profile_breaker(
    persona: str,
    profile: str,
    *,
    fresh_probe_evidence: str,
    reset_passed: bool,
    state_root: Path | None = None,
) -> None:
    """Close a cap breaker only after reset and a fresh authoritative probe."""

    if not reset_passed or not fresh_probe_evidence:
        raise ValueError("breaker reset requires passed reset time and fresh probe evidence")
    path = _transition_path(persona, state_root)
    state = _read_json(path)
    if state.get("capped_profile") != profile:
        raise ValueError(f"profile {profile!r} is not the recorded capped profile")
    state["breaker_open"] = False
    state["breaker_cleared_at"] = _iso(_utc_now())
    state["breaker_clear_evidence"] = fresh_probe_evidence
    _atomic_json(path, state)


def assert_drain_allowed(
    persona: str,
    drain_id: str,
    *,
    state_root: Path | None = None,
) -> None:
    """Fail transiently before provider invocation when a transition bars a drain."""

    state = transition_state(persona, state_root=state_root)
    phase = state.get("phase")
    if phase in {"preparing", "awaiting-successor-read"}:
        raise RuntimeTransitionPending(f"{persona} runtime transition is {phase}")
    if phase == "completed" and state.get("barred_drain_id") == drain_id:
        raise RuntimeTransitionPending(
            f"mailbox drain {drain_id} was claimed before the runtime transition"
        )


def _begin_transition(
    persona: str,
    *,
    trigger: str,
    outgoing_profile: str,
    incoming_profile: str,
    outgoing_harness: str | None = None,
    incoming_harness: str | None = None,
    drain_id: str | None,
    state_root: Path | None,
) -> dict[str, Any]:
    current = transition_state(persona, state_root=state_root)
    if current.get("phase") in {"preparing", "awaiting-successor-read"}:
        return current
    payload = {
        "schema": "pantheon-runtime-transition/v1",
        "transition_id": _new_transition_id(),
        "persona": persona,
        "trigger": trigger,
        "outgoing_profile": outgoing_profile,
        "incoming_profile": incoming_profile,
        "outgoing_harness": outgoing_harness,
        "incoming_harness": incoming_harness,
        "barred_drain_id": drain_id,
        "phase": "preparing",
        "runtime_swapped": False,
        "successor_read_ack": False,
        "started_at": _iso(_utc_now()),
    }
    _atomic_json(_transition_path(persona, state_root), payload)
    _append_controller_evidence(
        persona,
        "transition_started",
        {
            "trigger": trigger,
            "outgoing_harness": outgoing_harness,
            "outgoing_profile": outgoing_profile,
            "incoming_harness": incoming_harness,
            "incoming_profile": incoming_profile,
            "barred_drain_id": drain_id,
        },
        state_root=state_root,
    )
    return payload


def acknowledge_successor_read(
    persona: str,
    *,
    evidence: str,
    state_root: Path | None = None,
) -> None:
    """Record structural successor-read evidence; terminal pixels are rejected."""

    if not evidence or evidence.startswith("pane:"):
        raise ValueError("successor read acknowledgement requires structural evidence")
    path = _transition_path(persona, state_root)
    state = _read_json(path)
    if state.get("phase") != "awaiting-successor-read":
        raise RuntimeError("no successor read acknowledgement is pending")
    expected = state.get("expected_successor_read_evidence")
    if isinstance(expected, str) and expected and evidence != expected:
        raise ValueError("successor read acknowledgement does not match transition target")
    state["successor_read_ack"] = True
    state["successor_read_evidence"] = evidence
    state["phase"] = "completed"
    state["completed_at"] = _iso(_utc_now())
    _atomic_json(path, state)
    _append_controller_evidence(
        persona,
        "successor_read_acknowledged",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": state.get("incoming_harness"),
            "incoming_profile": state.get("incoming_profile"),
            "handoff_or_graveyard": state.get("handoff_or_graveyard"),
            "successor_read_evidence": evidence,
        },
        state_root=state_root,
    )


def _graveyard_root() -> Path | None:
    value = os.environ.get("PANTHEON_GRAVEYARD_ROOT")
    return Path(value) if value else None


def _strict_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Write a forced-failover receipt with the same fsync boundary as state."""

    _atomic_json(path, payload)
    check = _read_json(path)
    if check != payload:
        raise graveyard.GraveyardDurabilityError(
            "forced-failover receipt readability verification failed"
        )


def _forced_receipt_history_path(persona: str, state_root: Path | None = None) -> Path:
    return _persona_state(persona, state_root) / "forced-failover-history.jsonl"


def _append_forced_receipt_history(
    persona: str,
    receipt: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> None:
    _append_jsonl(_forced_receipt_history_path(persona, state_root), receipt)


def _read_forced_receipt(path: Path) -> dict[str, Any]:
    receipt = _read_json(path)
    if not receipt:
        raise graveyard.GraveyardDurabilityError("forced-failover receipt is unreadable")
    return receipt


def _completed_forced_receipt_matches(
    state: dict[str, Any],
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str,
    correlation_id: str,
) -> bool:
    receipt_ref = state.get("forced_receipt")
    if not isinstance(receipt_ref, str) or not receipt_ref:
        return False
    try:
        receipt = _read_forced_receipt(Path(receipt_ref))
    except graveyard.GraveyardDurabilityError:
        return False
    expected = {
        "schema": "pantheon-forced-failover/v1",
        "persona": target.persona,
        "transition_id": state.get("transition_id"),
        "previous_harness": current.profile.harness,
        "previous_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "graveyard_entry": state.get("handoff_or_graveyard"),
    }
    return all(receipt.get(key) == value for key, value in expected.items())


def _completed_forced_transition_matches(
    state: dict[str, Any],
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str,
    correlation_id: str,
) -> bool:
    return all(
        (
            state.get("phase") == "completed",
            state.get("successor_read_ack") is True,
            state.get("trigger") == "forced-cap",
            state.get("handoff_kind") == "forced-factual",
            state.get("outgoing_harness") == current.profile.harness,
            state.get("outgoing_profile") == current.profile.profile,
            state.get("incoming_harness") == target.launch.harness,
            state.get("incoming_profile") == target.launch.profile,
            state.get("message_id") == message_id,
            state.get("correlation_id") == correlation_id,
            isinstance(state.get("handoff_or_graveyard"), str),
            _completed_forced_receipt_matches(
                state,
                current,
                target,
                message_id=message_id,
                correlation_id=correlation_id,
            ),
        )
    )


def _record_successor_target(
    persona: str,
    state: dict[str, Any],
    evidence: Path,
    *,
    state_root: Path | None,
) -> dict[str, Any]:
    transition_id = str(state.get("transition_id") or _new_transition_id())
    state["transition_id"] = transition_id
    state["expected_successor_read_evidence"] = _successor_read_evidence(
        evidence,
        transition_id=transition_id,
    )
    state["successor_read_target"] = str(evidence)
    _atomic_json(_transition_path(persona, state_root), state)
    _append_controller_evidence(
        persona,
        "successor_read_target_recorded",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": state.get("incoming_harness"),
            "incoming_profile": state.get("incoming_profile"),
            "handoff_or_graveyard": str(evidence),
            "expected_successor_read_evidence": state[
                "expected_successor_read_evidence"
            ],
        },
        state_root=state_root,
    )
    return state


def _existing_graveyard(
    persona: str,
    *,
    expected_session_id: str | None,
) -> Path | None:
    root = _graveyard_root()
    entry = graveyard.latest(persona, root=root)
    if entry is None:
        return None
    if expected_session_id is not None and entry.get("uuid") != expected_session_id:
        return None
    path = graveyard.snapshot_path(persona, entry, root=root)
    if path is None or not path.is_file():
        return None
    try:
        if not path.read_text(encoding="utf-8").strip():
            return None
    except OSError:
        return None
    return path


def _replace_runtime(
    target: "PersonaBinding",
    *,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> RuntimeHandle:
    from tools.persona_bringup import build_adapter

    adapter = build_adapter(target, harness_home=target.account_home)
    boundary = _utc_now()
    runtime_start = _start_runtime(
        target,
        adapter,
        replace=_target_known(target.controller.tmux, **_context_kwargs(context)),
        **_context_kwargs(context),
    )
    _verify_started_runtime(
        target, runtime_start, state_root=state_root, **_context_kwargs(context)
    )
    try:
        _verify_runtime_input_ready(
            target, runtime_start, state_root=state_root, **_context_kwargs(context)
        )
    except RuntimeError:
        _clear_stale_runtime_state(target.persona, state_root=state_root)
        raise
    started = _utc_now()
    provenance = _controller_source_provenance(
        runtime_start_boundary_at=boundary,
        runtime_start_observed_at=started,
    )
    _atomic_json(
        _persona_state(target.persona, state_root) / "runtime.json",
        _runtime_payload(
            target,
            session_id=None,
            started_at=started,
            runtime_scope_unit=runtime_start.scope_unit,
            source_provenance=provenance,
        ),
    )
    return RuntimeHandle(
        profile=target.launch,
        tmux=target.controller.tmux,
        session_id=None,
        started_at=started,
    )


def _prompt_successor(
    handle: RuntimeHandle,
    evidence: Path,
    *,
    persona: str,
    state_root: Path | None,
    context: TmuxRuntimeContext | None = None,
) -> None:
    state_arg = _state_root(state_root)
    state = transition_state(persona, state_root=state_root)
    evidence_receipt = state.get("expected_successor_read_evidence")
    if not isinstance(evidence_receipt, str) or not evidence_receipt:
        evidence_receipt = f"hook:runtime-transfer:{evidence.name}"
    submit_to_runtime(
        handle,
        ExecutionRequest(
            prompt=(
                "Runtime transfer: read the continuity evidence at "
                f"{evidence}. After reading it, record the structural receipt "
                "with this exact command before retrying the pending mailbox "
                "record:\n"
                f"python3 {Path(__file__).resolve()} ack-read --persona "
                f"{shlex.quote(persona)} --state-root {shlex.quote(str(state_arg))} "
                f"--evidence {shlex.quote(evidence_receipt)}"
            ),
            cwd=Path.cwd(),
        ),
        persona=persona,
        state_root=state_root,
        message_id=(
            str(state["message_id"]) if isinstance(state.get("message_id"), str) else None
        ),
        correlation_id=(
            str(state["correlation_id"])
            if isinstance(state.get("correlation_id"), str)
            else None
        ),
        **_context_kwargs(context),
    )
    _append_controller_evidence(
        persona,
        "successor_prompted",
        {
            "trigger": state.get("trigger"),
            "outgoing_harness": state.get("outgoing_harness"),
            "outgoing_profile": state.get("outgoing_profile"),
            "incoming_harness": handle.profile.harness,
            "incoming_profile": handle.profile.profile,
            "handoff_or_graveyard": str(evidence),
            "expected_successor_read_evidence": evidence_receipt,
        },
        state_root=state_root,
    )


def planned_swap(
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    message_id: str | None,
    drain_id: str | None = None,
    handoff_path: Path | None = None,
    primary_resets_at: str | None = None,
    quota_evidence: dict[str, str] | None = None,
    trigger: Literal["planned-quota", "quota-reset"] = "planned-quota",
    state_root: Path | None = None,
    context: TmuxRuntimeContext | None = None,
) -> SwapReceipt:
    """Perform or resume a cooperative swap; finish only after read acknowledgement."""

    persona = target.persona
    state = transition_state(persona, state_root=state_root)
    if (
        state.get("phase") == "completed"
        and state.get("successor_read_ack")
        and state.get("trigger") == trigger
        and state.get("outgoing_harness") == current.profile.harness
        and state.get("outgoing_profile") == current.profile.profile
        and state.get("incoming_harness") == target.launch.harness
        and state.get("incoming_profile") == target.launch.profile
    ):
        evidence = Path(str(state["handoff_or_graveyard"]))
        return SwapReceipt(
            trigger=trigger,
            outgoing_profile=str(state["outgoing_profile"]),
            incoming_profile=str(state["incoming_profile"]),
            handoff_kind="model-authored",
            handoff_or_graveyard=evidence,
            completed_at=datetime.fromisoformat(
                str(state["completed_at"]).replace("Z", "+00:00")
            ),
        )

    _begin_transition(
        persona,
        trigger=trigger,
        outgoing_profile=current.profile.profile,
        incoming_profile=target.launch.profile,
        outgoing_harness=current.profile.harness,
        incoming_harness=target.launch.harness,
        drain_id=drain_id,
        state_root=state_root,
    )
    state = transition_state(persona, state_root=state_root)
    expected_transition = {
        "trigger": trigger,
        "outgoing_harness": current.profile.harness,
        "outgoing_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
    }
    mismatches = {
        key: (state.get(key), expected)
        for key, expected in expected_transition.items()
        if state.get(key) != expected
    }
    if mismatches:
        raise RuntimeTransitionPending(
            "another runtime transition is already active: "
            + ", ".join(
                f"{key}={actual!r} (requested {expected!r})"
                for key, (actual, expected) in sorted(mismatches.items())
            )
        )
    if primary_resets_at:
        state["primary_resets_at"] = primary_resets_at
    if quota_evidence:
        state["quota_evidence"] = dict(quota_evidence)
    _atomic_json(_transition_path(persona, state_root), state)
    source = handoff_path or target.account_home / "HANDOFF.md"
    if not state.get("handoff_requested"):
        baseline_text = ""
        baseline_mtime_ns: int | None = None
        try:
            baseline_text = source.read_text(encoding="utf-8")
            baseline_mtime_ns = source.stat().st_mtime_ns
        except OSError:
            pass
        submit_to_runtime(
            current,
            ExecutionRequest(
                prompt=(
                    "Prepare a concise runtime-transfer handoff at "
                    f"{source}. Do not stop until the handoff is durably written."
                ),
                cwd=target.account_home,
            ),
            persona=persona,
            state_root=state_root,
            message_id=message_id,
            **_context_kwargs(context),
        )
        state["handoff_requested"] = True
        state["handoff_baseline_sha256"] = hashlib.sha256(
            baseline_text.encode("utf-8")
        ).hexdigest()
        state["handoff_baseline_mtime_ns"] = baseline_mtime_ns
        _atomic_json(_transition_path(persona, state_root), state)
        raise RuntimeTransitionPending("fresh model-authored handoff was requested")
    try:
        handoff_text = source.read_text(encoding="utf-8")
        handoff_mtime_ns = source.stat().st_mtime_ns
    except OSError as exc:
        raise RuntimeTransitionPending("verified model-authored handoff is unavailable") from exc
    if not handoff_text.strip():
        raise RuntimeTransitionPending("verified model-authored handoff is empty")
    handoff_sha256 = hashlib.sha256(handoff_text.encode("utf-8")).hexdigest()
    if (
        handoff_sha256 == state.get("handoff_baseline_sha256")
        and handoff_mtime_ns == state.get("handoff_baseline_mtime_ns")
    ):
        raise RuntimeTransitionPending("model-authored handoff has not changed since request")
    stamp = _iso(_utc_now())
    archive = graveyard.archive_required(
        persona,
        handoff_text,
        jsonl_uuid=message_id or "planned-runtime-swap",
        reason="planned-runtime-swap",
        ts=stamp,
        root=_graveyard_root(),
    )
    replacement = _replace_runtime(
        target, state_root=state_root, **_context_kwargs(context)
    )
    state = transition_state(persona, state_root=state_root)
    state.update(
        {
            "phase": "awaiting-successor-read",
            "handoff_kind": "model-authored",
            "handoff_or_graveyard": str(archive),
            "runtime_swapped": True,
            "fallback_active": trigger == "planned-quota",
            "message_id": message_id,
        }
    )
    _record_successor_target(persona, state, archive, state_root=state_root)
    _append_controller_evidence(
        persona,
        "runtime_replaced",
        {
            "trigger": trigger,
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "handoff_or_graveyard": str(archive),
        },
        state_root=state_root,
    )
    _prompt_successor(
        replacement,
        archive,
        persona=persona,
        state_root=state_root,
        **_context_kwargs(context),
    )
    raise RuntimeTransitionPending("successor has not acknowledged the handoff yet")


def forced_swap(
    current: RuntimeHandle,
    target: "PersonaBinding",
    *,
    error: ProviderQuotaExhausted,
    message_id: str,
    correlation_id: str,
    drain_id: str | None = None,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> SwapReceipt:
    """Fail closed on authoritative cap and resume only after factual read ack."""

    persona = target.persona
    state = transition_state(persona, state_root=state_root)
    if _completed_forced_transition_matches(
        state,
        current,
        target,
        message_id=message_id,
        correlation_id=correlation_id,
    ):
        evidence = Path(str(state["handoff_or_graveyard"]))
        _append_controller_evidence(
            persona,
            "completed_forced_transition_reused",
            {
                "outgoing_harness": current.profile.harness,
                "outgoing_profile": current.profile.profile,
                "incoming_harness": target.launch.harness,
                "incoming_profile": target.launch.profile,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "handoff_or_graveyard": str(evidence),
            },
            state_root=state_root,
        )
        return SwapReceipt(
            trigger="forced-cap",
            outgoing_profile=str(state["outgoing_profile"]),
            incoming_profile=str(state["incoming_profile"]),
            handoff_kind="forced-factual",
            handoff_or_graveyard=evidence,
            completed_at=datetime.fromisoformat(
                str(state["completed_at"]).replace("Z", "+00:00")
            ),
        )

    state = _begin_transition(
        persona,
        trigger="forced-cap",
        outgoing_profile=current.profile.profile,
        incoming_profile=target.launch.profile,
        outgoing_harness=current.profile.harness,
        incoming_harness=target.launch.harness,
        drain_id=drain_id,
        state_root=state_root,
    )
    expected_transition = {
        "trigger": "forced-cap",
        "outgoing_harness": current.profile.harness,
        "outgoing_profile": current.profile.profile,
        "incoming_harness": target.launch.harness,
        "incoming_profile": target.launch.profile,
    }
    mismatches = {
        key: (state.get(key), expected)
        for key, expected in expected_transition.items()
        if state.get(key) != expected
    }
    if mismatches:
        raise RuntimeTransitionPending(
            "another runtime transition is already active: "
            + ", ".join(
                f"{key}={actual!r} (requested {expected!r})"
                for key, (actual, expected) in sorted(mismatches.items())
            )
        )
    state["message_id"] = message_id
    state["correlation_id"] = correlation_id
    state["breaker_open"] = True
    state["capped_profile"] = current.profile.profile
    if not state.get("primary_resets_at"):
        # A forced cap has no provider-observed reset window to record (the
        # cap already fired) -- unlike planned_swap's caller-supplied value,
        # so this is computed from the primary harness's weekly schedule.
        # First-write-wins: a retry of this same in-flight transition must
        # not redrift the recorded reset against a later "now".
        state["primary_resets_at"] = _iso(
            next_weekly_reset(current.profile.harness, now=now or _utc_now())
        )
    _atomic_json(_transition_path(persona, state_root), state)
    _append_controller_evidence(
        persona,
        "breaker_opened",
        {
            "trigger": "forced-cap",
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "capped_profile": current.profile.profile,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "barred_drain_id": drain_id,
        },
        state_root=state_root,
    )

    try:
        evidence = _existing_graveyard(
            persona,
            expected_session_id=current.session_id,
        )
        model_authored_handoff = evidence is not None
        stamp = _iso(_utc_now())
        safe_provider_error = graveyard.scrub_credentials(error.provider_error)[:1000]
        if evidence is None:
            factual = (
                "# Forced runtime transfer\n\n"
                "Model-authored handoff unavailable because the provider returned "
                "an authoritative quota limit.\n\n"
                f"- persona: {persona}\n"
                f"- previous profile: {current.profile.profile}\n"
                f"- previous session: {current.session_id or 'unknown'}\n"
                f"- message id: {message_id}\n"
                f"- correlation id: {correlation_id}\n"
                f"- provider error: {safe_provider_error}\n"
            )
            evidence = graveyard.archive_required(
                persona,
                factual,
                jsonl_uuid=current.session_id or message_id,
                reason="forced-provider-cap",
                ts=stamp,
                root=_graveyard_root(),
            )
        receipt_path = _persona_state(persona, state_root) / "forced-failover.json"
        receipt = {
            "schema": "pantheon-forced-failover/v1",
            "persona": persona,
            "transition_id": state.get("transition_id"),
            "previous_harness": current.profile.harness,
            "previous_profile": current.profile.profile,
            "previous_session_id": current.session_id,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "graveyard_entry": str(evidence),
            "message_id": message_id,
            "correlation_id": correlation_id,
            "ticket_or_step_id": None,
            "repository_evidence": {},
            "last_lifecycle_event": _read_json(
                _persona_state(persona, state_root) / "runtime.json"
            ).get("last_structural_event"),
            "provider_error": safe_provider_error,
            "model_authored_handoff": model_authored_handoff,
            "created_at": stamp,
        }
        _strict_receipt(receipt_path, receipt)
        _append_forced_receipt_history(persona, receipt, state_root=state_root)
        _append_controller_evidence(
            persona,
            "forced_evidence_durable",
            {
                "outgoing_harness": current.profile.harness,
                "outgoing_profile": current.profile.profile,
                "incoming_harness": target.launch.harness,
                "incoming_profile": target.launch.profile,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "forced_receipt": str(receipt_path),
                "handoff_or_graveyard": str(evidence),
                "model_authored_handoff": model_authored_handoff,
            },
            state_root=state_root,
        )
    except Exception as exc:
        state = transition_state(persona, state_root=state_root)
        state["phase"] = "durability-failed"
        state["breaker_open"] = True
        state["durability_error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(_transition_path(persona, state_root), state)
        raise ProviderQuotaExhausted(
            "forced failover evidence is not durable; fallback remains unused",
            profile=current.profile.profile,
            provider_error=error.provider_error,
        ) from exc

    replacement = _replace_runtime(target, state_root=state_root)
    state = transition_state(persona, state_root=state_root)
    state.update(
        {
            "phase": "awaiting-successor-read",
            "handoff_kind": "forced-factual",
            "handoff_or_graveyard": str(evidence),
            "runtime_swapped": True,
            "fallback_active": True,
            "forced_receipt": str(receipt_path),
            "message_id": message_id,
            "correlation_id": correlation_id,
        }
    )
    _record_successor_target(persona, state, evidence, state_root=state_root)
    _append_controller_evidence(
        persona,
        "runtime_replaced",
        {
            "trigger": "forced-cap",
            "outgoing_harness": current.profile.harness,
            "outgoing_profile": current.profile.profile,
            "incoming_harness": target.launch.harness,
            "incoming_profile": target.launch.profile,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "forced_receipt": str(receipt_path),
            "handoff_or_graveyard": str(evidence),
        },
        state_root=state_root,
    )
    _prompt_successor(
        replacement,
        evidence,
        persona=persona,
        state_root=state_root,
    )
    raise RuntimeTransitionPending("successor has not acknowledged graveyard evidence yet")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ack = subparsers.add_parser("ack-read")
    ack.add_argument("--persona", required=True)
    ack.add_argument("--state-root", type=Path, default=None)
    ack.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    if args.command == "ack-read":
        acknowledge_successor_read(
            args.persona,
            evidence=args.evidence,
            state_root=args.state_root,
        )
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
