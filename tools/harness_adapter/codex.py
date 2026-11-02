"""CodexHarnessAdapter — concrete binding for the OpenAI Codex CLI harness.

Implements PantheonHarnessAdapter for codex (the first non-claude Pantheon daemon,
stood up 2026-06-02T01:05Z per ledger, onboarded per ORG-031).

CRITICAL — session/recovery surface (harvest finding #2):
    Current Codex supports explicit interactive and headless resume. The typed
    binding uses structural JSONL and explicit session IDs. Remaining differences:

      - Storage: SQLite files (~/.codex/*.sqlite), NOT JSONL; no message.usage.
      - Resume mechanism and command shape differ from Claude.
      - Frame format: codex TUI frame does not match ``╭─╮``/``❯`` sentinels →
        context-fill-monitor, restart-drain, inbox-consumer, ho-resumer all
        no-op or time out.

    Do NOT arm Claude-specific recovery monitors against Codex.

Binding deltas encoded from Recovery harvest
(recovery-bringup-gap-harvest-2026-06-09.md):
  - A2 mcp_server_config_path: TOML, NOT JSON; streamable-HTTP-only (SSE→shim
    required: T-0714 FINDING 1); cross-user perms apply (T-0714 FINDING 2).
  - A7 auth_status: OAuth-vs-key distinction; OAuth unlocks keyless image_gen.
  - B1 inbound: durable maildir receipt plus harness-specific wake/resume.
  - B2 approval_defaults: approval_policy × sandbox_mode × per-MCP-tool
    approval_mode (3 orthogonal axes; the MCP-tool gate is independent of
    approval_policy — T-1033 live find); trust_level alone does NOT silence
    prompts (live-proven).

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i), §Decision (ii)
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      recovery-bringup-gap-harvest-2026-06-09.md
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from tools.harness_adapter._common import (
    deliver_to_mailbox,
    finalize_events,
    parse_json_object,
)
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.types import (
    AuthDescriptor,
    CommandSpec,
    DeliveryReceipt,
    DiagnosticObservation,
    ExecutionRequest,
    ExecutionResult,
    HarnessLaunchConfig,
    HarnessCapabilities,
    HarnessEvent,
    HarnessSurfaceDescriptors,
    HookCapability,
    InboundRequest,
    PostStartAssessment,
    PostStartPaneSample,
    McpDescriptor,
    MemoryDescriptor,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)
from tools.harness_permission_posture import (
    CODEX_NATIVE_EXECUTION_BOUNDARY_KEY,
    CODEX_NATIVE_PERMISSION_POSTURE_KEY,
    PermissionPostureError,
    codex_permission_native_overrides,
    codex_permission_native_sandbox,
    validate_codex_permission_native,
)
from tools import codex_schema_contract as codex_appserver_contract
from tools.harness_adapter.provider_quota import (
    CodexAppServerQuotaObserver,
    read_codex_app_server_hooks,
    read_codex_app_server_rate_limits,
)


_APP_SERVER_BINDINGS = Path(__file__).resolve().parents[2] / "harness/codex/appserver_seats.json"
_APP_SERVER_RUNTIME = Path(__file__).resolve().parents[2] / "harness/codex/appserver_supervise.py"
_APP_SERVER_ROUTE_REL = Path(".config/pantheon/codex-appserver-route.json")
_APP_SERVER_READY_SCHEMA = "pantheon-codex-appserver-input-ready/v1"
_APP_SERVER_READY_KEYS = frozenset({"schema", "persona", "thread_id", "launch_nonce"})


# ---------------------------------------------------------------------------
# T-4862: codex usage-limit banner classifier.
#
# Codex has no structural cap signal in its TUI, only the rendered banner
# "You've hit your usage limit ... try again at Oct 14th 4:13 AM".
# The parsed instant is a HINT for when to look again, never a trusted reset.
#
# Residual risk (accepted, bounded -- same posture as the claude T-4047
# decision): the banner is matched only inside the pane's BOTTOM REGION
# (trailing _CODEX_CAP_BOTTOM_LINES lines), and the mid-session caller
# additionally requires two consecutive samples with an identical
# render-span hash. Prose that quotes the banner verbatim as the LAST lines of
# the pane, across two polls, can still classify; a quote followed by further
# output cannot. Post-start classification is baseline-diffed, so a banner
# already in scrollback at launch does not classify.
# ---------------------------------------------------------------------------
_CODEX_CAP_BOTTOM_LINES = 20
_CODEX_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CODEX_CAP_RE = re.compile(r"you['\u2019]ve hit your usage limit", re.IGNORECASE)
_CODEX_RESET_RE = re.compile(
    r"try again at\s+([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?,?\s+"
    r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


@dataclass(frozen=True)
class CodexUsageLimit:
    cap_kind: str
    reset_at: datetime | None
    reset_source: str | None
    render_span_sha256: str


def _codex_bottom_region(text: str) -> str:
    normalized = _CODEX_ANSI_RE.sub("", text).replace("\r", "").rstrip()
    return "\n".join(normalized.splitlines()[-_CODEX_CAP_BOTTOM_LINES:])


def _parse_codex_reset(region: str, *, now: datetime, tz: tzinfo | None) -> datetime | None:
    match = _CODEX_RESET_RE.search(region)
    if match is None:
        return None
    month = _MONTHS.get(match.group(1)[:3].lower())
    if month is None:
        return None
    zone = tz or datetime.now().astimezone().tzinfo or timezone.utc
    try:
        hour12 = int(match.group(4))
        if not 1 <= hour12 <= 12:
            return None
        hour = hour12 % 12 + (12 if match.group(6).lower() == "pm" else 0)
        day, minute = int(match.group(2)), int(match.group(5))
        year = int(match.group(3)) if match.group(3) else now.astimezone(zone).year
        candidate = datetime(year, month, day, hour, minute, tzinfo=zone)
        if not match.group(3) and candidate < now - timedelta(days=1):
            candidate = datetime(year + 1, month, day, hour, minute, tzinfo=zone)
    except (ValueError, OverflowError):
        return None
    return candidate.astimezone(timezone.utc)


def classify_codex_usage_limit(text: str, *, now: datetime, tz: tzinfo | None = None) -> CodexUsageLimit | None:
    """Classify a pane sample as a codex usage-limit banner, or None."""
    if not text:
        return None
    region = _codex_bottom_region(text)
    if _CODEX_CAP_RE.search(region) is None:
        return None
    reset_at = _parse_codex_reset(region, now=now, tz=tz)
    return CodexUsageLimit(
        cap_kind="usage-limit",
        reset_at=reset_at,
        reset_source="parsed" if reset_at is not None else None,
        render_span_sha256=hashlib.sha256(region.encode("utf-8")).hexdigest(),
    )


class CodexHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for the OpenAI Codex CLI daemon.

    Args:
        role_domain: ORG-027 instance role-key for the target codex agent.
        codex_home: path to the codex user home; defaults to Path.home().
                    Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "codex"

    def __init__(
        self,
        role_domain: str,
        codex_home: Path | None = None,
        mailbox_path: Path | None = None,
        launch: HarnessLaunchConfig | None = None,
    ) -> None:
        super().__init__(role_domain, launch=launch)
        self._codex_home = codex_home if codex_home is not None else Path.home()
        self._mailbox_path = mailbox_path
        self._appserver_readiness: tuple[Path, str, str, str] | None = None

    # -------------------------------------------------------------------------
    # Typed lifecycle contract
    # -------------------------------------------------------------------------

    def capabilities(self) -> HarnessCapabilities:
        hook_names = (
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PreCompact",
            "PostCompact",
            "UserPromptSubmit",
            "SubagentStop",
            "Stop",
            "SessionStart",
            "SubagentStart",
            "SessionEnd",
        )
        hooks = {
            name: HookCapability(
                event=name,
                matchers=name
                in {"PreToolUse", "PermissionRequest", "PostToolUse"},
                can_block=name
                in {
                    "PreToolUse",
                    "PermissionRequest",
                    "UserPromptSubmit",
                    "SubagentStop",
                    "Stop",
                },
                can_continue=name in {"SubagentStop", "Stop"},
                handler_types=("command",),
            )
            for name in hook_names
        }
        return HarnessCapabilities(
            harness="codex",
            session_resume_by_id=True,
            session_resume_last=True,
            structured_progress=True,
            structured_output_schema=True,
            local_generated_memory=True,
            native_stop_continuation=True,
            inbound_mechanisms=("maildir", "poll-and-inject"),
            hook_events=hooks,
        )

    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        argv = [str(self.executable), "exec", "--json"]
        if request.ephemeral:
            argv.append("--ephemeral")
        model = request.model or (self.launch.model if self.launch else None)
        if model:
            argv.extend(["--model", model])
        self._append_native_args(argv)
        if request.output_schema:
            argv.extend(["--output-schema", str(request.output_schema)])
        argv.append("-")
        return CommandSpec(
            argv=tuple(argv),
            stdin=request.prompt,
            cwd=request.cwd,
            env={"CODEX_HOME": str(self._codex_home / ".codex")},
        )

    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        cwd = session.cwd if session.cwd is not None else Path.cwd()
        argv = [str(self.executable), "exec", "resume", "--json", session.session_id]
        self._append_native_args(argv)
        if prompt is not None:
            argv.append("-")
        return CommandSpec(
            argv=tuple(argv),
            stdin=prompt,
            cwd=cwd,
            env={"CODEX_HOME": str(self._codex_home / ".codex")},
        )

    def parse_event(self, line: str) -> HarnessEvent | None:
        raw, parse_error = parse_json_object(line)
        if parse_error:
            return HarnessEvent(
                harness="codex",
                kind="error",
                session_id=None,
                message=parse_error,
                usage=None,
                raw={"line": line.rstrip("\n"), "category": "provider"},
            )
        if raw is None:
            return None

        native_type = raw.get("type")
        thread_id = raw.get("thread_id")
        session_id = thread_id if isinstance(thread_id, str) else None
        usage = _codex_usage(raw.get("usage"))
        message: str | None = None
        kind = "progress"

        if native_type == "thread.started":
            kind = "session_started"
        elif native_type == "turn.started":
            kind = "turn_started"
        elif native_type == "turn.completed":
            kind = "turn_completed"
        elif native_type == "turn.failed":
            kind = "turn_failed"
            error = raw.get("error")
            if isinstance(error, dict):
                raw_message = error.get("message")
                message = raw_message if isinstance(raw_message, str) else str(error)
            elif error is not None:
                message = str(error)
        elif native_type in {"item.started", "item.completed"}:
            item = raw.get("item")
            item = item if isinstance(item, dict) else {}
            item_type = item.get("type")
            item_id = item.get("id")
            if item_type == "agent_message":
                raw_text = item.get("text")
                message = raw_text if isinstance(raw_text, str) else None
                kind = "message_completed" if native_type == "item.completed" else "progress"
            elif item_type in {"collaboration_tool_call", "subagent"}:
                kind = (
                    "subagent_completed"
                    if native_type == "item.completed"
                    else "subagent_started"
                )
            else:
                kind = "tool_completed" if native_type == "item.completed" else "tool_started"
            if message is None and isinstance(item_id, str):
                message = item_id
        elif native_type in {"error", "stream_error"}:
            kind = "error"
            raw_message = raw.get("message") or raw.get("error")
            message = str(raw_message) if raw_message is not None else "codex error event"

        return HarnessEvent(
            harness="codex",
            kind=kind,
            session_id=session_id,
            message=message,
            usage=usage,
            raw=raw,
        )

    def finalize(
        self,
        events: Iterable[HarnessEvent],
        *,
        returncode: int,
        stderr: str,
    ) -> ExecutionResult:
        return finalize_events(
            "codex",
            events,
            returncode=returncode,
            stderr=stderr,
        )

    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        return deliver_to_mailbox(
            "codex",
            request,
            default_inbox=self._mailbox_path,
            default_sender="harness-adapter",
        )

    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        return HarnessEvent(
            harness="codex",
            kind="error",
            session_id=None,
            message=f"{type(exc).__name__}: {exc}",
            usage=None,
            raw={"category": "config", "retryable": False},
        )

    def validate_launch_config(self) -> tuple[DiagnosticObservation, ...]:
        if self.launch is None:
            return (
                DiagnosticObservation(
                    check_id="codex.runtime_profile",
                    status="warn",
                    summary="legacy adapter has no immutable runtime profile",
                    authoritative=True,
                    evidence={"profile": "", "legacy": "true"},
                ),
            )
        allowed_native = {
            "sandbox",
            "config_overrides",
            CODEX_NATIVE_PERMISSION_POSTURE_KEY,
            CODEX_NATIVE_EXECUTION_BOUNDARY_KEY,
        }
        unknown = sorted(set(self.launch.native) - allowed_native)
        status = "fail" if unknown else "pass"
        return (
            DiagnosticObservation(
                check_id="codex.runtime_profile",
                status=status,
                summary=(
                    "codex runtime profile is valid"
                    if status == "pass"
                    else "codex runtime profile contains unknown native keys"
                ),
                authoritative=True,
                evidence={
                    "harness": self.launch.harness,
                    "profile": self.launch.profile,
                    "mode": self.launch.mode,
                    "meter": self.launch.meter,
                    "quota_source": self.launch.quota_source,
                    "unknown_native_keys": ",".join(unknown),
                    "native_namespace": "codex",
                },
            ),
        )

    def describe_surfaces(self, cwd: Path) -> HarnessSurfaceDescriptors:
        standing = self.standing_prompt_filename()
        discovered = _find_upward(cwd, standing)
        return HarnessSurfaceDescriptors(
            standing=StandingInstructionsDescriptor(
                filename=standing,
                discovered=(discovered,) if discovered else (),
                load_order=("cwd-upward", "codex-home"),
                authoritative=True,
                evidence={"harness": "codex", "cwd": str(cwd)},
            ),
            auth=self.auth_descriptor(),
            memory=MemoryDescriptor(
                available=True,
                enabled=False,
                readable=None,
                writable=None,
                evidence={
                    "harness": "codex",
                    "enabled_by_adapter": "false",
                    "claim": "descriptor-only",
                },
            ),
            mcp=McpDescriptor(
                config_paths=(self.mcp_server_config_path(),),
                configured_servers=(),
                scope=("user",),
                authoritative=False,
                evidence={"harness": "codex", "native_format": "toml"},
            ),
            roles=RoleDescriptor(
                config_paths=(self._codex_home / ".codex" / "config.toml",),
                configured_roles=(),
                delegation_modes=("multi_agent.spawn_agent",),
                authoritative=False,
                evidence={"harness": "codex", "schema": "codex-config-toml"},
            ),
        )

    def auth_descriptor(self) -> AuthDescriptor:
        return AuthDescriptor(
            mechanisms=("oauth", "api-key"),
            material_paths=(self._codex_home / ".codex" / "auth.json",),
            observed_state="unverified",
            authoritative=False,
            evidence={
                "harness": "codex",
                "descriptor_only": "true",
                "live_probe": "not-run",
            },
        )

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        # Route selection is per launch. A prior app-server generation must never
        # make a later bare launch on this adapter poll its stale receipt.
        self._appserver_readiness = None
        appserver = self._controller_appserver_launch(target, cwd=cwd)
        if appserver is not None:
            return appserver
        argv = [str(self.executable)]
        model = self.launch.model if self.launch else None
        if model:
            argv.extend(["--model", model])
        self._append_native_args(argv)
        self._append_writable_root_args(argv)
        # Wedge #6 (T-2597 wedge-map): --dangerously-bypass-hook-trust is a
        # CLI-only flag (codex-rs/cli/src/main.rs:1994,3048), NOT a
        # config.toml key -- it cannot be pre-seeded like the trust/update/
        # migration levers below, so it must be appended on every
        # interactive launch. Already documented in approval_defaults()/
        # sandbox_config but was previously dead documentation: neither
        # _append_native_args (sandbox + config_overrides only) nor anything
        # else in this method actually appended it. Currently inert only
        # because ~/.codex/hooks.json doesn't exist; goes live the moment
        # one does (startup-hooks-trust review screen, wedge #6).
        argv.append("--dangerously-bypass-hook-trust")
        return CommandSpec(
            argv=tuple(argv),
            stdin=None,
            cwd=cwd,
            env={
                "CODEX_HOME": str(self._codex_home / ".codex"),
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
        )

    def _controller_appserver_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec | None:
        """Select the controller-owned route only for a valid explicit switch."""
        switch_path = self._codex_home / _APP_SERVER_ROUTE_REL
        try:
            switch = json.loads(switch_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        if type(switch) is not dict or switch.get("enabled") is not True:
            return None
        try:
            bindings = json.loads(_APP_SERVER_BINDINGS.read_text(encoding="utf-8"))["seats"]
            seat = switch["seat"]
            binding = bindings[seat]
            raw_state = str(switch["state_dir"])
            state_dir = raw_state.replace("~/", str(self._codex_home) + "/", 1)
            socket_path = Path(str(switch["socket"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid enabled app-server route {switch_path}: {exc}") from exc
        if binding.get("account") != self._codex_home.name:
            raise ValueError(
                f"app-server seat {seat!r} belongs to account {binding.get('account')!r}, "
                f"not {self._codex_home.name!r}"
            )
        binding_cwd = codex_appserver_contract.resolve_binding_cwd(
            binding, instance_root=_APP_SERVER_BINDINGS.parents[2]
        )
        expected = {
            "codex_bin": str(self.executable),
            "cwd": str(cwd),
            "model": self.launch.model if self.launch else None,
        }
        if self.launch is not None:
            sandbox = codex_permission_native_sandbox(self.launch.native)
            if sandbox is None:
                sandbox = self.launch.native.get("sandbox")
            overrides = codex_permission_native_overrides(self.launch.native)
            raw_overrides = self.launch.native.get("config_overrides")
            if isinstance(raw_overrides, dict):
                overrides = {**overrides, **raw_overrides}
            expected["sandbox"] = sandbox
            expected["approval_policy"] = overrides.get("approval_policy")
        for key, value in expected.items():
            bound = binding_cwd if key == "cwd" else binding.get(key)
            if bound != value:
                raise ValueError(
                    f"app-server seat {seat!r} {key}={bound!r} does not match runtime {value!r}"
                )
        if socket_path.name != "as.sock" or not socket_path.is_absolute():
            raise ValueError(f"app-server public socket must be an absolute as.sock path: {socket_path}")
        argv = [
            sys.executable, str(_APP_SERVER_RUNTIME),
            "--controller-owned", "--manage-writer",
            "--persona", seat,
            "--account", str(binding["account"]),
            "--codex-bin", str(binding["codex_bin"]),
            "--codex-home", str(self._codex_home / ".codex"),
            "--rundir", str(socket_path.parent),
            "--state-dir", state_dir,
            "--sandbox", str(binding["sandbox"]),
            "--approval-policy", str(binding["approval_policy"]),
            "--model", str(binding["model"]),
            "--cwd", binding_cwd,
            "--route-switch", str(switch_path),
        ]
        # Keep the random token safe as a separate argv value at both the
        # controller->supervisor and supervisor->writer boundaries.  A raw
        # token_urlsafe value may begin with "-", which argparse treats as an
        # option and rejects before either process can publish readiness.
        launch_nonce = "n_" + secrets.token_urlsafe(32)
        receipt_path = Path(state_dir) / "input-ready.json"
        try:
            thread_doc = json.loads((Path(state_dir) / "thread.json").read_text(encoding="utf-8"))
            expected_thread = thread_doc["thread_id"]
            if type(expected_thread) is not str or not expected_thread:
                raise ValueError("invalid recorded thread")
            receipt_path.unlink(missing_ok=True)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"cannot prepare app-server readiness receipt: {exc}") from exc
        self._appserver_readiness = (receipt_path, seat, expected_thread, launch_nonce)
        argv += ["--readiness-receipt", str(receipt_path), "--launch-nonce", launch_nonce]
        return CommandSpec(
            argv=tuple(argv), stdin=None, cwd=cwd,
            env={
                "CODEX_HOME": str(self._codex_home / ".codex"),
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
        )

    def appserver_input_readiness_enabled(self) -> bool:
        return self._appserver_readiness is not None

    def appserver_input_ready(self) -> bool:
        """Validate the exact current-generation writer receipt. Never trusts pixels."""
        if self._appserver_readiness is None:
            return False
        path, persona, expected_thread, nonce = self._appserver_readiness
        fd = -1
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                return False
            raw = os.read(fd, 4097)
            if len(raw) > 4096 or len(raw) != info.st_size:
                return False
            doc = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            return False
        finally:
            if fd >= 0:
                os.close(fd)
        return (
            type(doc) is dict
            and frozenset(doc) == _APP_SERVER_READY_KEYS
            and type(doc.get("schema")) is str
            and doc["schema"] == _APP_SERVER_READY_SCHEMA
            and type(doc.get("persona")) is str
            and doc["persona"] == persona
            and type(doc.get("thread_id")) is str
            and doc["thread_id"] == expected_thread
            and type(doc.get("launch_nonce")) is str
            and doc["launch_nonce"] == nonce
        )

    def assess_post_start_readiness(
        self,
        *,
        harness: str,
        profile: str,
        baseline: PostStartPaneSample,
        samples: Iterable[PostStartPaneSample],
        now: datetime | None = None,
    ) -> PostStartAssessment:
        """T-4862: fail closed on a codex usage-limit banner that appeared AFTER
        launch (baseline-diffed); otherwise defer to the generic assessment."""
        sample_tuple = tuple(samples)
        resolved_now = now if now is not None else datetime.now(timezone.utc)
        baseline_hit = classify_codex_usage_limit(baseline.text, now=resolved_now)
        for sample in sample_tuple:
            if sample.sha256 == baseline.sha256:
                continue
            hit = classify_codex_usage_limit(sample.text, now=resolved_now)
            if hit is None or (baseline_hit is not None and hit.render_span_sha256 == baseline_hit.render_span_sha256):
                continue
            return PostStartAssessment(
                schema="pantheon-harness-post-start-assessment/v1",
                harness=harness,
                profile=profile,
                status="subscription-cap",
                cause="codex-usage-limit-banner",
                pattern_id="codex-usage-limit-banner",
                baseline_sha256=baseline.sha256,
                capture_count=len(sample_tuple),
                changed_capture_indices=tuple(
                    item.capture_index for item in sample_tuple if item.sha256 != baseline.sha256
                ),
                cap_kind=hit.cap_kind,
                reset_at=hit.reset_at,
            )
        return super().assess_post_start_readiness(
            harness=harness, profile=profile, baseline=baseline, samples=sample_tuple, now=now
        )

    def quota_observer(self, state_dir: Path) -> CodexAppServerQuotaObserver:
        del state_dir
        profile = self.launch.profile if self.launch else "legacy"
        return CodexAppServerQuotaObserver(
            lambda: read_codex_app_server_rate_limits(
                self.executable,
                codex_home=self._codex_home / ".codex",
            ),
            profile=f"codex/{profile}",
        )

    def _append_native_args(self, argv: list[str]) -> None:
        if self.launch is None:
            return
        try:
            validate_codex_permission_native(self.launch.native, role="adapter")
        except PermissionPostureError as exc:
            raise ValueError(str(exc)) from exc
        try:
            sandbox = codex_permission_native_sandbox(self.launch.native)
        except PermissionPostureError as exc:
            raise ValueError(str(exc)) from exc
        if sandbox is None:
            sandbox = self.launch.native.get("sandbox")
        if isinstance(sandbox, str):
            argv.extend(["--sandbox", sandbox])
        try:
            posture_overrides = codex_permission_native_overrides(self.launch.native)
        except PermissionPostureError as exc:
            raise ValueError(str(exc)) from exc
        raw_overrides = self.launch.native.get("config_overrides")
        overrides: dict[str, Any] = dict(posture_overrides)
        if isinstance(raw_overrides, dict):
            overrides.update(raw_overrides)
        if overrides:
            for key, value in sorted(overrides.items()):
                argv.extend(["--config", f"{key}={_toml_scalar(value)}"])

    def _append_writable_root_args(self, argv: list[str]) -> None:
        if self.launch is None:
            return
        for root in self.launch.writable_roots:
            argv.extend(["--add-dir", str(root)])

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return "AGENTS.md" — the codex-native standing-prompt filename.

        Codex auto-loads AGENTS.md from the working directory (dir-scoped).
        Stronger levers: model_instructions_file (full base-prompt replacement),
        developer_instructions.  Degrades when git is not detected (run with
        ``codex -C <repo>`` or ``--skip-git-repo-check`` to avoid scope degradation).

        Note: the running daemon as of 2026-06-09 has NO AGENTS.md or
        model_instructions_file set (stock defaults); standing prompt is an open
        deployment gap (Recovery harvest §A1).
        """
        return "AGENTS.md"

    def mcp_server_config_path(self) -> Path:
        """Return ~/.codex/config.toml — where [mcp_servers.<id>] tables are declared.

        BINDING DELTA from claude (TWO non-mechanical differences):
          (a) Transport: ``--url`` is streamable-HTTP ONLY.  The Pantheon bridge
              serves legacy SSE; codex required a stdio shim (T-0714 FINDING 1).
          (b) Cross-user perms: /home/claude is 0750; codex (uid 1003) cannot
              traverse.  MCP binary must be deployed to a codex-owned path
              (T-0714 FINDING 2).

        Callers must account for these binding deltas; mechanical key-name
        translation alone is insufficient for MCP wiring.
        """
        return self._codex_home / ".codex" / "config.toml"

    def hooks_config_path(self) -> Path:
        """Return ~/.codex/hooks.json — the codex-native hooks config file.

        Codex supports hooks inline in config.toml as [[hooks.<Event>]] tables OR
        in a separate hooks.json.  This binding returns the standalone file path.
        The current event catalog is returned by ``capabilities()``. This path
        does not imply that every hook is configured or trusted.
        """
        return self._codex_home / ".codex" / "hooks.json"

    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        """Return a codex spawn_agent descriptor for the given role.

        Codex uses native multi_agent (features.multi_agent=true, live-verified):
        spawn_agent / send_input / wait_agent / resume_agent / close_agent.
        Roles map to [agents.<name>] tables in config.toml with per-role config_file.
        Bounds are first-class: agents.max_depth / max_threads /
        job_max_runtime_seconds.

        Persona worker-configs (implementer/reviewer) BUILT + live-verified
        2026-06-09 (T-1045 / Recovery; agents/implementer.toml + agents/reviewer.toml +
        AGENTS.md committed to codex-config). Full cycle proven: root spawn_agent
        (implementer role) -> codex spawned a distinct-identity worker (gpt-5.5 high
        from [agents.implementer]) that received the Pantheon implementer system
        prompt, did real committed file/git work, and was collected via wait_agent.
        This is the ORG-031 proof that codex maps cleanly onto the dispatch pattern.

        Two load-bearing spawn learnings (T-1045):
          - The [agents.<role>] config_file MUST set ``model`` explicitly. Omitting
            it (relying on inherit) fails the spawn: "multi-agent spawn rejected role
            fork, then failed child model resolution". Setting model=gpt-5.5 unblocked.
          - The spawn must be FRESH (not a full-history fork) when overriding role.
          - Role discipline attaches via ``developer_instructions`` inside the
            config_file (= the role system prompt); shared base discipline lives in
            ~/.codex/AGENTS.md. [agents.<name>] itself takes only config_file +
            description (no per-agent instructions key).

        No TeamCreate analog — codex spawns per-agent, not per-team.

        Shape (skeleton stub; not wired to real subprocess):
            {"harness": "codex", "role": role, "multi_agent": True,
             "spawn_tool": "spawn_agent", "config_overrides": config_overrides}
        """
        return {
            "harness": "codex",
            "role": role,
            "multi_agent": True,
            "spawn_tool": "spawn_agent",
            "config_overrides": config_overrides,
        }

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Return the current ``codex exec resume <id>`` descriptor.

        The resume-by-id concept generalizes; storage and surrounding recovery
        mechanics still do not:

          - Storage: ~/.codex/*.sqlite (SQLite), NOT JSONL; no message.usage.
          - Mechanism: ``codex exec resume <id>`` for headless continuation
            rather than Claude's flag shape.
          - Frame sentinels: codex TUI frame does not match the ``╭─╮``/``❯``
            patterns that context-fill-monitor, restart-drain, inbox-consumer, and
            ho-resumer depend on.

        Do not arm Claude TUI/session monitors against Codex merely because
        explicit resume exists.

        The legacy dict shape is retained for callers that have not moved to
        ``build_resume`` yet.
        """
        return {
            "harness": "codex",
            "cli_args": ["exec", "resume", "--json", session_id],
            "session_id": session_id,
        }

    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """Return ["codex", "exec", prompt] — the codex headless execution args.

        ``codex exec`` (alias: ``codex e``) is the ``claude -p`` analog.

        Args:
            prompt: the prompt string to pass to ``codex exec``.
            **kwargs: reserved for future harness-specific flags.

        Returns:
            CLI args list suitable for subprocess.run().

        Note: if any MCP server is configured with ``required=true``, ``codex exec``
        will EXIT on MCP-start failure (vs the interactive TUI's graceful-degrade).
        Callers must ensure required MCPs are reachable before running headless.
        """
        return [str(self.executable), "exec", prompt]

    def auth_status(self) -> dict[str, Any]:
        """Return auth state dict for codex.

        BINDING DELTA from claude — OAuth-vs-API-key distinction has capability
        consequences the mechanical map hides:

          - The running daemon uses the operator's ChatGPT-Plus OAuth session (device-code
            flow), NOT an API key.  Auth stored at ~/.codex/auth.json (0600).
          - OAuth session unlocks built-in multimodal tools (e.g. image_gen) keyless
            off the ChatGPT-Plus quota; the API-key path may NOT expose these tools.
            (Recovery harvest §A7; undocumented in any SOP/ADR as of 2026-06-09.)

        Shape:
            {"harness": "codex",
             "key_source": "oauth",
             "auth_file": "<codex_home>/.codex/auth.json",
             "oauth_note": "OAuth session unlocks built-in tools (e.g. image_gen) ..."}
        """
        return {
            "harness": "codex",
            "key_source": "oauth",
            "auth_file": str(self._codex_home / ".codex" / "auth.json"),
            "observed_state": self.auth_descriptor().observed_state,
            "descriptor_only": True,
            "oauth_note": (
                "OAuth session (ChatGPT-Plus device-code flow) unlocks built-in "
                "multimodal tools (e.g. image_gen) keyless off the Plus quota.  "
                "The API-key path may NOT expose these tools (Recovery harvest §A7)."
            ),
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Compatibility shim over durable maildir delivery.

        BRIDGE SURFACE — this is the central proven harness-specific finding:

          - OUTBOUND works: codex MCP tool-call → curl sendMessage → 200 → device
            (live-verified T-0714).
          - INBOUND does NOT generalize: claude inbound rides
            ``capabilities.experimental.claude/channel`` which codex does NOT
            implement.  Live test (T-0714): Telegram messages reached the bridge
            SSE session but never appeared as a turn in the codex pane.
          - Codex inbound = poll-and-inject via ``tmux send-keys`` (live kick) or
            ``codex exec`` per-inbound.  No Telegram plugin in codex's marketplace;
            not turnkey (SOP §12.4).

        Durable queueing is structural delivery, not a claim that a pane has
        acknowledged the turn.  New callers should use ``deliver_inbound`` and
        inspect its receipt.
        """
        request = InboundRequest(
            message=message,
            correlation_id=str(kwargs.pop("correlation_id", "legacy-inbound")),
            source=str(kwargs.pop("source", "legacy")),
            reply_route={str(key): str(value) for key, value in kwargs.items()},
        )
        self.deliver_inbound(request)
        return None
    def approval_defaults(self) -> dict[str, Any]:
        """Return codex unattended-mode approval and hook-trust defaults.

        THREE orthogonal axes — ALL must be pre-answered at bringup or the daemon
        wedges on a tool call:

          (1) approval_policy="never" — silences model-generated *shell/command*
              prompts ONLY. ``trust_level`` alone does NOT silence prompts
              (live-proven, Recovery harvest §B2 + §3).
          (2) sandbox_mode="danger-full-access" — ORG-031 §(ii) as amended
              (the operator TG 11733/11741/11742, T-4929): every harness runs fully
              unlocked; the unix account is the security boundary, not the
              harness sandbox. No workspace-write sub-config applies.
          (3) [mcp_servers.<srv>.tools.<tool>] approval_mode="approve" — a SEPARATE
              per-MCP-tool gate, INDEPENDENT of approval_policy. Live find
              2026-06-09 (T-1033): under approval_policy="never", a freshly-
              registered MCP tool STILL prompts on first use (codex wedged on
              "Allow the telegram MCP server to run tool 'reply'?"). Must be
              pre-set per tool (or "Always allow" once in-TUI, which persists
              exactly that one tool). Claude has no exact analog.

        Hook trust: new hooks also require explicit /hooks trust (or managed
        requirements.toml, or --dangerously-bypass-hook-trust).  Pre-answered
        at bringup per the wedge-bypass script.

        Shape (ORG-031 §(ii) as amended + the per-tool MCP gate):
            {"approval_policy": "never",
             "sandbox_mode": "danger-full-access",
             "mcp_tool_approval": "approve",  # per [mcp_servers.*.tools.*]
             "hook_trust": "--dangerously-bypass-hook-trust"}
        """
        return {
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
            # Per-MCP-tool gate, independent of approval_policy (T-1033 live find):
            # set [mcp_servers.<srv>.tools.<tool>] approval_mode="approve" per tool.
            "mcp_tool_approval": "approve",
            "hook_trust": "--dangerously-bypass-hook-trust",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (per-harness documented attributes)
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """Codex sandbox and approval policy config (ORG-031 §(ii) as amended).

        approval_policy × sandbox_mode (2 orthogonal axes). Full-unlock default
        per the operator TG 11733/11742 (T-4929); see HarnessAdapter.approval_defaults
        for the principle. ``sandbox_workspace_write`` only applies to
        workspace-write and is not part of the default.

        Ratified config:
            approval_policy="never"
            sandbox_mode="danger-full-access"

        Backup of pre-unattended config at: ~/.codex/config.toml.bak.preunattended
        """
        return {
            "harness": "codex",
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
            "config_backup": "~/.codex/config.toml.bak.preunattended",
            "ratified_by": "ORG-031 §(ii) as amended; the operator TG 11733/11742",
        }

    @property
    def profiles_config(self) -> dict[str, Any]:
        """Codex profile layering config.

        ``codex -p <name>`` layers $CODEX_HOME/<name>.config.toml +
        [permissions.<name>].  No claude-code analog.
        """
        return {
            "harness": "codex",
            "mechanism": "-p <name> layers $CODEX_HOME/<name>.config.toml + [permissions.<name>]",
            "description": (
                "Profile layering via -p <name>; no claude-code analog. "
                "Each profile can extend base config with additional permissions."
            ),
        }

    @property
    def app_server_config(self) -> dict[str, Any]:
        """Codex app-server / remote-control / exec-server config.

        Experimental JSON-RPC daemons; no claude-code analog.
        """
        return {
            "harness": "codex",
            "supported": True,
            "surfaces": ["app-server", "remote-control", "exec-server"],
            "description": (
                "Experimental JSON-RPC daemon surfaces. No claude-code analog. "
                "Not currently used in Pantheon; documented for completeness."
            ),
        }

    @property
    def cloud_config(self) -> dict[str, Any]:
        """Codex cloud-task ingest config.

        Codex cloud-task ingest surface; no claude-code analog.
        """
        return {
            "harness": "codex",
            "supported": True,
            "description": (
                "Codex cloud-task ingest surface. No claude-code analog. "
                "Not currently used in Pantheon; documented for completeness."
            ),
        }

    @property
    def doctor_config(self) -> dict[str, Any]:
        """Codex doctor / health-check config.

        ``codex doctor`` provides structured env/runtime/install/search health output.

        Note: ``codex doctor`` reports a git warning when repo is not detected in
        the home dir; run with ``codex -C <repo>`` or ``--skip-git-repo-check``
        to avoid scope degradation (Recovery harvest §C).

        An org-tier codex-doctor-equivalent for Pantheon is an open follow-up
        (Recovery harvest §D5; target=recovery, unfiled).
        """
        return {
            "harness": "codex",
            "command": "codex doctor",
            "description": (
                "Structured env/runtime/install/search health output. "
                "Run with 'codex -C <repo>' or '--skip-git-repo-check' to avoid "
                "git-scope degradation (Recovery harvest §C). "
                "Org-tier Pantheon health equiv is an open follow-up (Recovery harvest §D5)."
            ),
        }


def _codex_usage(value: Any) -> Usage | None:
    if not isinstance(value, dict):
        return None
    return Usage(
        input_tokens=_optional_int(value.get("input_tokens")),
        cached_input_tokens=_optional_int(value.get("cached_input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        reasoning_output_tokens=_optional_int(value.get("reasoning_output_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ensure_project_trusted(codex_home: Path, cwd: Path) -> bool:
    """Idempotently mark ``cwd`` as a codex-trusted project directory.

    codex's ``trust_level`` (per-project, under ``[projects."<cwd>"]`` in
    ``~/.codex/config.toml``) gates the one-time first-run "Do you trust the
    contents of this directory?" startup modal specifically -- a SEPARATE,
    orthogonal axis from ``approval_policy`` (which gates ongoing per-tool
    approval prompts during a session and is already pre-answered
    ``"never"`` in every codex role's manifest native config; live-verified
    distinction: ``knowledge/episodic/2026-06-04-codex-harness-abstraction-
    synthesis/recovery-bringup-gap-harvest-2026-06-09.md``). Pre-establishing
    trust here means the real bringup path
    (``runtime_controller._start_runtime``) never needs to dismiss a trust
    dialog by keystroke -- see ``tools.codex_modal`` for the keystroke-risk
    half of first-run-modal safety (the drifty update-available modal, which
    has no config lever). Trust-dialog-gates-on-trust_level is live-probed:
    ``bugs/harness-swap-bench-second-claude-live-poller-blank-pane.md``.

    Never overwrites an EXISTING ``[projects."<cwd>"]`` table, even one
    without ``trust_level`` or with a value other than ``"trusted"`` -- an
    operator's prior explicit choice (including an explicit distrust) is
    left alone; only a project with NO entry at all gets a fresh trusted
    block appended. Refuses (raises) rather than blind-appending into a
    config.toml it cannot parse -- never risk corrupting or duplicating a
    table in unknown content.

    Pure config-file mutation -- never touches a live pane. Returns True iff
    ``config.toml`` was modified.
    """
    config_path = codex_home / "config.toml"
    cwd_key = str(cwd)
    try:
        existing_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = ""
    try:
        parsed = tomllib.loads(existing_text) if existing_text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"codex config.toml at {config_path} is not valid TOML -- refusing to "
            "blind-append a trust entry into unparseable content"
        ) from exc
    projects = parsed.get("projects")
    if isinstance(projects, dict) and cwd_key in projects:
        return False
    codex_home.mkdir(parents=True, exist_ok=True)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[projects.{_toml_scalar(cwd_key)}]\ntrust_level = "trusted"\n')
    return True


_PROJECT_TABLE_HEADER_RE = re.compile(r'^\[projects\.(.+)\]\s*$')


def _prune_stale_project_entries(text: str) -> tuple[str, int]:
    """Pure text-in/text-out core of the project-trust GC pass (T-2600).

    ``ensure_project_trusted`` appends a fresh ``[projects."<cwd>"]`` table
    per bringup cwd and never prunes -- disposable bench-run cwds (mostly
    ``/tmp/tmp*``) accumulate dead entries forever. This removes ONLY
    ``[projects."<path>"]`` table blocks whose ``<path>`` no longer exists
    as a directory on disk. A deleted directory can never legitimately need
    its trust entry again -- codex re-establishes trust on next real use of
    a real path -- so this is safe; an entry whose path still exists is
    NEVER touched, even one with a non-``"trusted"`` value or extra
    hand-added keys (an operator's explicit prior choice is out of scope
    for a dead-path GC).

    Everything else in *text* -- every other table (``[sandbox_workspace_
    write]``, ``[tui.*]``, etc.), every root-scope key (``check_for_update_
    on_startup`` and friends), and the formatting of surviving content -- is
    preserved byte-for-byte. This is deliberately NOT a
    parse-with-tomllib-then-write-back round trip: stdlib ``tomllib`` is
    read-only, and reaching for a TOML-writing library (``tomlkit`` /
    ``tomli_w``) to do a GC pass would be a new dependency for what's really
    a targeted deletion. Instead this mirrors ``_insert_root_level_toml_bool``
    / ``_insert_into_toml_table`` above: line-based text surgery, using
    ``tomllib`` only to (a) validate the input parses and (b) decode each
    project table's key correctly (TOML string-escaping is non-trivial;
    letting ``tomllib`` do it beats hand-rolling an unescaper).

    A project table's content is its header line plus every following
    non-blank line up to (not including) the next blank line or the next
    ``[``-starting table header, whichever comes first -- covering tables
    with extra operator-added keys, not just the fixed two-line shape this
    module itself writes. A single blank line immediately preceding a
    pruned table's header is removed along with it (that blank line was
    written as part of THIS entry's own append, per ``ensure_project_
    trusted``'s ``f'\\n[projects....'`` format) unless a preceding removed
    block already claimed it -- this keeps adjacent dead/dead and dead/live
    boundaries clean: no doubled blank lines, no lost separator for a
    surviving neighbor.

    Raises RuntimeError on unparseable *text* -- same posture as
    ``ensure_project_trusted``: never guess at which tables are dead inside
    content that doesn't even parse.

    Returns ``(text, 0)`` unchanged when there is nothing to prune --
    idempotent no-op, including when *text* has no ``[projects]`` table at
    all.
    """
    try:
        parsed = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            "codex config.toml text is not valid TOML -- refusing to guess "
            "which [projects.*] tables are dead in unparseable content"
        ) from exc
    projects = parsed.get("projects")
    if not isinstance(projects, dict) or not projects:
        return text, 0
    dead_keys = {key for key in projects if not Path(key).is_dir()}
    if not dead_keys:
        return text, 0

    lines = text.splitlines(keepends=True)
    keep = [True] * len(lines)
    pruned = 0
    index = 0
    while index < len(lines):
        match = _PROJECT_TABLE_HEADER_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        try:
            header_key = next(iter(tomllib.loads(lines[index] + "_x = 1\n")["projects"]))
        except (tomllib.TOMLDecodeError, KeyError, StopIteration):
            index += 1
            continue
        if header_key not in dead_keys:
            index += 1
            continue
        block_start = index
        if block_start > 0 and lines[block_start - 1].strip() == "" and keep[block_start - 1]:
            block_start -= 1
        block_end = index + 1
        while (
            block_end < len(lines)
            and lines[block_end].strip() != ""
            and not lines[block_end].lstrip().startswith("[")
        ):
            block_end += 1
        for i in range(block_start, block_end):
            keep[i] = False
        pruned += 1
        index = block_end
    if pruned == 0:
        return text, 0
    new_text = "".join(line for line, kept in zip(lines, keep) if kept)
    return new_text, pruned


def gc_stale_project_trust_entries(codex_home: Path) -> int:
    """Prune ``[projects."<path>"]`` entries whose path no longer exists.

    Standalone maintenance pass -- deliberately NOT called from
    ``ensure_project_trusted`` or anywhere on the bringup hot path (T-2600).
    Folding GC into that already-hot, per-bringup call would turn its
    documented no-op-on-repeat-call behavior into one that can mutate
    config.toml on every session start; that's a production-activation
    decision distinct from authoring and testing the GC logic itself, and is
    deliberately left to a separate follow-up (whether to wire this into
    ``ensure_project_trusted``, a periodic timer, or leave it manual/CLI-run
    via ``gc_trust_entries_cli`` below).

    Pure config-file mutation -- never touches a live pane. Returns the
    number of entries pruned; 0 (no write) if ``config.toml`` is absent, has
    no ``[projects]`` table, or has nothing stale -- idempotent, matching
    ``ensure_project_trusted``'s own no-write-when-unchanged contract.
    """
    config_path = codex_home / "config.toml"
    try:
        existing_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    new_text, pruned = _prune_stale_project_entries(existing_text)
    if pruned:
        config_path.write_text(new_text, encoding="utf-8")
    return pruned


def gc_trust_entries_cli(argv: list[str] | None = None) -> int:
    """Thin, manually-runnable consumer for ``gc_stale_project_trust_entries``.

    Ops/manual surface only -- guarded behind ``__main__`` below, never run
    as an import side effect. NOT wired into any bringup path or timer
    (T-2600 scope); periodic activation is a separate follow-up decision.
    """
    parser = argparse.ArgumentParser(
        description='Prune dead [projects."<path>"] trust entries from codex config.toml.',
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
        help="codex home directory containing config.toml (default: ~/.codex)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many entries WOULD be pruned; never writes",
    )
    args = parser.parse_args(argv)
    config_path = args.codex_home / "config.toml"

    if args.dry_run:
        try:
            existing_text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print("gc_trust_entries_cli: no config.toml found; nothing to prune")
            return 0
        _new_text, pruned = _prune_stale_project_entries(existing_text)
        noun = "entry" if pruned == 1 else "entries"
        print(f"gc_trust_entries_cli: would prune {pruned} stale project {noun} (dry-run, no write)")
        return 0

    pruned = gc_stale_project_trust_entries(args.codex_home)
    noun = "entry" if pruned == 1 else "entries"
    print(f"gc_trust_entries_cli: pruned {pruned} stale project {noun} from {config_path}")
    return 0


_BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    """Return *key* formatted as a syntactically valid TOML key.

    Bare (unquoted) if it matches TOML's bare-key charset (letters, digits,
    underscore, hyphen); quoted otherwise. Load-bearing for codex's own
    ``hide_gpt-5.1-codex-max_migration_prompt`` notice key: it contains a
    literal ``.``, which TOML would otherwise parse as dotted-key table
    nesting (``hide_gpt-5`` -> ``1-codex-max_migration_prompt``) rather than
    one flat key -- exactly the byte sequence the wedge-map's own citation
    shows quoted (``knowledge/episodic/2026-08-05-codex-startup-wedge-map/
    notes.md`` wedge #4).
    """
    if _BARE_TOML_KEY_RE.match(key):
        return key
    return _toml_scalar(key)


def _insert_root_level_toml_bool(existing_text: str, key: str, value: bool) -> str:
    """Insert ``key = value`` as a bare ROOT-scope TOML key into ``existing_text``.

    Unlike ``ensure_project_trusted``'s own append-at-end style (safe there
    because it appends a brand-new ``[table]``), a bare key appended after an
    existing ``[table]`` header would silently become a member of that table
    instead of root scope -- TOML scopes any key to whichever ``[table]``
    most recently preceded it. This walks the existing text for the FIRST
    line that opens a table (a line whose stripped form starts with ``[``)
    and inserts before it, so the key lands at root scope regardless of what
    tables already exist or the call order relative to other config-seed
    writers. Appends at file end (root scope, since no table exists yet) if
    there is no table header at all.

    Naive to a table-header-shaped byte sequence appearing mid-value (e.g. a
    multi-line array literal opening bracket alone at column 0) -- an
    accepted simplification for a text this module itself writes and that
    codex only otherwise appends simple scalar/table content to, mirroring
    ``ensure_project_trusted``'s own text-based (non-round-tripping) approach.
    """
    line = f"{_toml_key(key)} = {_toml_scalar(value)}\n"
    lines = existing_text.splitlines(keepends=True)
    for index, existing_line in enumerate(lines):
        if existing_line.lstrip().startswith("["):
            return "".join(lines[:index]) + line + "".join(lines[index:])
    prefix = existing_text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + line


def _insert_into_toml_table(
    existing_text: str, table_name: str, new_keys: dict[str, bool | str]
) -> str:
    """Insert ``new_keys`` into ``[table_name]`` within ``existing_text``.

    If ``[table_name]`` does not already appear as its own header line,
    appends a fresh ``[table_name]`` block (with ``new_keys``) at file end --
    always TOML-legal regardless of what precedes it. If the table already
    exists, inserts the new key lines into its body (right before the next
    table header, or at EOF if it is the last table) rather than declaring a
    duplicate header, which TOML forbids.

    ``table_name`` may itself be dotted (e.g. ``"notice.model_migrations"``)
    -- TOML's own dotted-table-header syntax, so the header line lands as a
    nested table without any special-casing here. ``new_keys`` values may be
    ``bool`` (T-2597's ``[notices]`` hide-key seeding) or ``str`` (T-2674's
    ``[notice.model_migrations]`` from-model -> to-model seeding); both are
    already handled generically by ``_toml_scalar``/``_toml_key``, including
    quoting dotted-looking keys like ``"gpt-5.1-codex-max"`` or
    ``"gpt-5.4"``.
    """
    body_lines = [f"{_toml_key(k)} = {_toml_scalar(v)}\n" for k, v in sorted(new_keys.items())]
    header = f"[{table_name}]"
    lines = existing_text.splitlines(keepends=True)
    header_index = next(
        (index for index, existing_line in enumerate(lines) if existing_line.strip() == header),
        None,
    )
    if header_index is None:
        prefix = existing_text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return f"{prefix}\n{header}\n" + "".join(body_lines)
    insert_at = header_index + 1
    while insert_at < len(lines) and not lines[insert_at].lstrip().startswith("["):
        insert_at += 1
    return "".join(lines[:insert_at]) + "".join(body_lines) + "".join(lines[insert_at:])


def ensure_update_check_disabled(codex_home: Path) -> bool:
    """Idempotently set ``check_for_update_on_startup = false`` at config.toml
    root scope.

    T-2597 wedge-map wedges #2 (blocking "Update available!" 1/2/3 picker)
    and #3 (non-blocking update banner): both read the same cached
    ``version.json`` via codex's ``get_upgrade_version`` (
    ``codex-rs/tui/src/updates.rs``), gated on this single top-level boolean
    (default ``true``, ``codex-rs/config/src/config_toml.rs:470-473``). This
    one key categorically retires both surfaces rather than papering over
    either -- the highest-value single fix in the wedge map. ``codex_modal``
    stays wired as fallback insurance (per the wedge-map's own
    recommendation) for a not-yet-migrated ``codex_home`` or a future
    version that adds a code path ignoring this flag.

    Never overwrites an existing explicit ``check_for_update_on_startup``
    value -- mirrors ``ensure_project_trusted``'s respect-prior-choice
    philosophy; an operator's earlier explicit choice (including re-enabling
    the check) is left alone.

    Pure config-file mutation -- never touches a live pane. Returns True iff
    ``config.toml`` was modified.
    """
    config_path = codex_home / "config.toml"
    try:
        existing_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = ""
    try:
        parsed = tomllib.loads(existing_text) if existing_text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"codex config.toml at {config_path} is not valid TOML -- refusing to "
            "blind-insert an update-check entry into unparseable content"
        ) from exc
    if "check_for_update_on_startup" in parsed:
        return False
    codex_home.mkdir(parents=True, exist_ok=True)
    new_text = _insert_root_level_toml_bool(existing_text, "check_for_update_on_startup", False)
    config_path.write_text(new_text, encoding="utf-8")
    return True


# T-2597 wedge #4: codex-rs's Notice struct hide_*_migration_prompt fields
# are a FIXED, compile-time enum -- NOT a generic per-model-slug template.
# Extracted 2026-08-05 via `strings -n 4 <vendor-binary> | grep -o
# 'hide_[a-zA-Z0-9_.-]*'` against the PINNED codex 0.146.0 rust binary
# (/usr/bin/codex is a Node wrapper, not the vendor binary itself; the real
# target is /usr/lib/node_modules/@openai/codex/node_modules/@openai/
# codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex). Corroborated
# by knowledge/episodic/2026-08-05-codex-startup-wedge-map/notes.md wedge #4
# (codex-rs/config/src/types.rs:816-833, codex-rs/tui/src/app.rs:148-149):
# these are the ONLY two migration keys this exact build recognizes. A NEW
# key appears any time OpenAI ships a new default-model migration -- re-run
# the same strings|grep recipe against the then-pinned binary when the
# executable version changes or the live model catalog churns.
_KNOWN_MODEL_MIGRATION_HIDE_KEYS: dict[str, str] = {
    "gpt-5.1": "hide_gpt5_1_migration_prompt",
    "gpt-5.1-codex-max": "hide_gpt-5.1-codex-max_migration_prompt",
}

# T-2674: model-upgrade presets that have NO hide_*_migration_prompt key in
# the pinned binary (see _KNOWN_MODEL_MIGRATION_HIDE_KEYS above) but CAN
# still be suppressed by pre-seeding the acknowledgment codex itself writes
# to config.toml's [notice.model_migrations] table on manual dismissal
# (verified live during T-2599's real-home wet-run, codex-cli 0.146.0:
# dismissing the modal writes "<from-model>" = "<to-model>"). Because that
# entry is what suppresses the modal after acknowledgment, pre-seeding it is
# equivalent by construction -- no separate wet-run needed for this ticket.
# Hardcoded, same shape/precedent as _KNOWN_MODEL_MIGRATION_HIDE_KEYS above --
# deriving the mapping from a live server-fetched cache at seed-time would be
# a fragile source for what is otherwise a static config write. Any active
# slug in NEITHER this dict nor _KNOWN_MODEL_MIGRATION_HIDE_KEYS is NOT
# covered here -- tools.codex_modal's keystroke fallback (T-2599) is the
# safety net for unknown/future presets.
_KNOWN_MODEL_MIGRATIONS: dict[str, str] = {
    "gpt-5.4": "gpt-5.6-terra",
    "gpt-5.4-mini": "gpt-5.6-luna",
}


def ensure_model_migration_prompts_hidden(
    codex_home: Path,
    models_cache_path: Path | None = None,
) -> bool:
    """Idempotently pre-seed migration-prompt suppression config for active,
    known model-migration presets.

    T-2597 wedge-map wedge #4: a blocking full-screen "switch model" modal
    fires when the CURRENT default model has a server-fetched ``ModelUpgrade``
    preset (cached in ``<codex_home>/models_cache.json``, NOT in the OSS
    repo) and the matching suppression config isn't set. There is no
    reliable signal, short of asking codex itself, for which model will
    actually be selected as "current default" at launch time
    (``models_cache.json`` carries no is-default flag) -- so rather than
    guess, this scans EVERY model in the cache for an active ``upgrade`` and
    seeds suppression for any whose slug is known, via ONE of two levers
    depending on which the pinned binary supports for that slug:

    - ``[notices]`` hide-keys (``_KNOWN_MODEL_MIGRATION_HIDE_KEYS``) --
      a fixed, compile-time set of ``hide_*_migration_prompt`` booleans.
    - ``[notice.model_migrations]`` acknowledgment entries
      (``_KNOWN_MODEL_MIGRATIONS``, T-2674) -- the SAME
      ``"<from-model>" = "<to-model>"`` entry codex itself writes on manual
      dismissal; pre-seeding it produces the identical suppressed state.
      Covers presets with no hide-key in the pinned binary (live-verified
      2026-08-05: gpt-5.4 -> gpt-5.6-terra and gpt-5.4-mini -> gpt-5.6-luna).

    Seeding either lever for a migration that turns out not to be in use is
    harmless (never touches a live pane); seeding all currently-active known
    ones correctly covers whichever model actually launches.

    KNOWN RESIDUAL GAP (documented, not fixed by this function): an active
    upgrade whose slug is in NEITHER ``_KNOWN_MODEL_MIGRATION_HIDE_KEYS`` nor
    ``_KNOWN_MODEL_MIGRATIONS`` has no suppression lever this function knows
    of -- ``tools.codex_modal``'s keystroke fallback (T-2599) is the safety
    net for that case (unknown/future presets, e.g. a new default-model
    migration OpenAI ships later).

    No-ops (returns False, never raises) if ``models_cache.json`` is absent
    or unparseable -- a brand-new ``codex_home`` predates any server-fetched
    cache and cannot be probed pre-launch; this narrow gap self-heals on the
    next relaunch once the cache exists. Never re-fetches the cache itself
    (read-only against whatever codex last wrote there) and never overwrites
    an existing explicit value for a key it would otherwise seed, in either
    table.

    Pure config-file mutation -- never touches a live pane. Returns True iff
    ``config.toml`` was modified.
    """
    cache_path = (
        models_cache_path if models_cache_path is not None else codex_home / "models_cache.json"
    )
    try:
        cache_text = cache_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        cache = json.loads(cache_text)
    except json.JSONDecodeError:
        return False
    models = cache.get("models") if isinstance(cache, dict) else None
    if not isinstance(models, list):
        return False
    active_slugs = {
        model.get("slug")
        for model in models
        if isinstance(model, dict)
        and isinstance(model.get("slug"), str)
        and isinstance(model.get("upgrade"), dict)
    }
    candidate_keys = {
        _KNOWN_MODEL_MIGRATION_HIDE_KEYS[slug]: True
        for slug in active_slugs
        if slug in _KNOWN_MODEL_MIGRATION_HIDE_KEYS
    }
    candidate_migrations = {
        slug: _KNOWN_MODEL_MIGRATIONS[slug]
        for slug in active_slugs
        if slug in _KNOWN_MODEL_MIGRATIONS
    }
    if not candidate_keys and not candidate_migrations:
        return False

    config_path = codex_home / "config.toml"
    try:
        existing_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = ""
    try:
        parsed = tomllib.loads(existing_text) if existing_text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"codex config.toml at {config_path} is not valid TOML -- refusing to "
            "blind-insert a notices entry into unparseable content"
        ) from exc
    existing_notices = parsed.get("notices")
    existing_notices = existing_notices if isinstance(existing_notices, dict) else {}
    new_keys = {k: v for k, v in candidate_keys.items() if k not in existing_notices}

    existing_notice_table = parsed.get("notice")
    existing_notice_table = existing_notice_table if isinstance(existing_notice_table, dict) else {}
    existing_migrations = existing_notice_table.get("model_migrations")
    existing_migrations = existing_migrations if isinstance(existing_migrations, dict) else {}
    new_migrations = {
        slug: to_model
        for slug, to_model in candidate_migrations.items()
        if slug not in existing_migrations
    }

    if not new_keys and not new_migrations:
        return False
    codex_home.mkdir(parents=True, exist_ok=True)
    new_text = existing_text
    if new_keys:
        new_text = _insert_into_toml_table(new_text, "notices", new_keys)
    if new_migrations:
        new_text = _insert_into_toml_table(new_text, "notice.model_migrations", new_migrations)
    config_path.write_text(new_text, encoding="utf-8")
    return True


WORKER_GUARD_MATCHER = "Bash|apply_patch"
# T-4395: Codex FAILS OPEN on a hook timeout (T-4321's live test), so this must sit well above
# the guard's own fail-closed clock (worker_guard_impl.DECIDE_DEADLINE_S, T-4389): the guard's
# deny then always wins the race. tools/test_worker_guard_shim.py pins the margin.
WORKER_GUARD_TIMEOUT_S = 30
# Bound on every app-server hooks/list this module runs, so a wedged app-server cannot hang a
# codex launch (prepare_codex_launch, reached from runtime_controller._start_runtime through
# CodexIntegration.prepare_launch, calls ensure_worker_guard_hook before launch; T-5011 PR-A).
_HOOKS_LIST_TIMEOUT_S = 15.0


def worker_guard_command(account_home: Path) -> str:
    """The PreToolUse worker-guard command for a seat (codex-config snippet).

    ``-I`` (T-4397 part A): an isolated interpreter ignores PYTHON* env vars and the user
    site-packages, so a worker-written usercustomize/sitecustomize/.pth cannot run before the
    guard. Decision-identical to the plain invocation (T-4395 smoke: 1040 payloads, 0 diffs).
    """
    return f"/usr/bin/python3 -I {account_home}/pantheon-config/tools/hooks/worker-guard.py"


def _legacy_worker_guard_commands(account_home: Path) -> tuple[str, ...]:
    """Earlier canonical guard commands (codex-config da8c863, T-4144/T-4160/T-4166), upgraded in place."""
    return (f"/usr/bin/python3 {account_home}/pantheon-config/tools/hooks/worker-guard.py",)


_WORKER_GUARD_BLOCK = """
# PreToolUse worker-guard (T-4144 / T-4166): the fleet's SUBAGENT denies on this
# seat's codex runtime. codex 0.147 ships multi_agent default-on and sets agent_type
# only on a spawned subagent's tool calls, so the main loop is never restricted.
# Seeded by tools.harness_adapter.codex.ensure_worker_guard_hook; source of truth is
# codex-config templates/config.toml.snippet. python3 -I (T-4397 A); timeout 30 sits
# above the guard's own fail-closed deadline, since a codex hook timeout fails open (T-4395).
[[hooks.PreToolUse]]
matcher = "Bash|apply_patch"

[[hooks.PreToolUse.hooks]]
type = "command"
command = {command}
timeout = 30
"""


def _worker_guard_log(message: str) -> None:
    """Loud, greppable trail on stderr (the controller's log) for every upgrade/restore (T-4395)."""
    print(f"[worker-guard-hook] {message}", file=sys.stderr, flush=True)


def _guard_positions(parsed: dict[str, Any], command: str) -> list[tuple[int, int]]:
    """(group, hook) indices of `command` under [[hooks.PreToolUse]]: codex's key suffix."""
    groups = (parsed.get("hooks") or {}).get("PreToolUse") or []
    return [
        (g, h)
        for g, group in enumerate(groups if isinstance(groups, list) else [])
        for h, hook in enumerate((group or {}).get("hooks") or [])
        if isinstance(hook, dict) and hook.get("command") == command
    ]


def _guard_hook(parsed: dict[str, Any], pos: tuple[int, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    group = parsed["hooks"]["PreToolUse"][pos[0]]
    return group, group["hooks"][pos[1]]


def _key_config_path(codex_home: Path) -> Path:
    """The config path inside codex's hook keys: the REALPATH of CODEX_HOME + "/config.toml".

    Live-verified on codex-cli 0.147 (T-4395 r1): a symlinked CODEX_HOME is resolved, a
    symlinked config.toml itself is not.
    """
    return Path(os.path.realpath(codex_home)) / "config.toml"


def _guard_key(codex_home: Path, pos: tuple[int, int]) -> str:
    """codex's hook key: position-based, NOT derived from the command (live-verified)."""
    return f"{_key_config_path(codex_home)}:pre_tool_use:{pos[0]}:{pos[1]}"


def _guard_definition_current(parsed: dict[str, Any], pos: tuple[int, int], command: str) -> bool:
    group, hook = _guard_hook(parsed, pos)
    return (hook.get("command") == command and hook.get("type") == "command"
            and hook.get("timeout") == WORKER_GUARD_TIMEOUT_S
            and group.get("matcher") == WORKER_GUARD_MATCHER)


def _stored_trusted_hash(parsed: dict[str, Any], key: str) -> str | None:
    entry = ((parsed.get("hooks") or {}).get("state") or {}).get(key)
    value = entry.get("trusted_hash") if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value else None


def _toml_inline(value: Any) -> str | None:
    if isinstance(value, (str, bool, int, float)):
        return _toml_scalar(value)
    if isinstance(value, list):
        items = [_toml_inline(v) for v in value]
        return None if any(i is None for i in items) else "[" + ", ".join(items) + "]"
    return None


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_scalar(key)


def _guard_definition_toml(parsed: dict[str, Any], pos: tuple[int, int]) -> str | None:
    """A standalone config holding EXACTLY the guard's definition as it sits in `parsed`: its
    group's own keys (the matcher) and the guard hook with every key verbatim (T-4395 r1: codex's
    currentHash covers the matcher and any extra hook key, e.g. statusMessage). Sibling hooks of
    the group are left out (the hash is per hook). None when a value can't be rendered exactly;
    the round-trip below proves the rendering."""
    group, hook = _guard_hook(parsed, pos)
    group_keys = {k: v for k, v in group.items() if k != "hooks"}
    lines = ["[[hooks.PreToolUse]]"]
    for table, items in ((None, group_keys), ("[[hooks.PreToolUse.hooks]]", hook)):
        if table:
            lines += ["", table]
        for k, v in items.items():
            rendered = _toml_inline(v)
            if rendered is None:
                return None
            lines.append(f"{_toml_key(k)} = {rendered}")
    text = "\n".join(lines) + "\n"
    [rt_group] = tomllib.loads(text)["hooks"]["PreToolUse"]
    rt_hooks = rt_group.pop("hooks")
    return text if (rt_group, rt_hooks) == (group_keys, [hook]) else None


def _precompute_guard_hash(definition: str, command: str, *, executable: Path, account_home: Path,
                           read_hooks) -> str | None:
    """codex's currentHash for `definition` (the EXACT guard definition about to be written),
    read from a SCRATCH CODEX_HOME (T-4395).

    Live-verified on codex-cli 0.147.0: the hash covers the hook definition (command, timeout,
    matcher, any extra hook key), not the config path, comments or the hook's position, so a
    scratch home holding that same definition yields the hash the real home will report.
    Reading it first lets the block and its trusted_hash land in ONE write: never a window in
    which the real home holds an untrusted/"modified" guard (codex skips those without
    --dangerously-bypass-hook-trust, i.e. fails open). None on any failure.
    """
    with tempfile.TemporaryDirectory(prefix="codex-wg-hash-") as tmp:
        scratch = Path(tmp)
        (scratch / "config.toml").write_text(definition, encoding="utf-8")
        try:
            hooks = read_hooks(executable, codex_home=scratch, cwd=account_home,
                               timeout=_HOOKS_LIST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best-effort read, never fatal to a launch
            _worker_guard_log(f"hash precompute failed: {exc!r}")
            return None
        prefix = f"{_key_config_path(scratch)}:pre_tool_use:"
        matches = [
            h for h in hooks or []
            if h.get("command") == command and h.get("eventName") == "preToolUse"
            and str(h.get("key") or "").startswith(prefix)
        ]
    digest = matches[0].get("currentHash") if len(matches) == 1 else None
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def _table_bounds(lines: list[str], index: int) -> tuple[int, int]:
    """[start, end) of the TOML table holding line `index`: from its header to the next header."""
    start = index
    while start > 0 and not lines[start].lstrip().startswith("["):
        start -= 1
    end = index + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    return start, end


def _key_line(line: str, name: str) -> Any:
    """The value of a one-line `name = value` TOML pair, or None when `line` isn't one."""
    if not re.match(rf"\s*{re.escape(name)}\s*=", line):
        return None
    try:
        return tomllib.loads(line.strip()).get(name)
    except tomllib.TOMLDecodeError:
        return None


def _set_line(lines: list[str], start: int, end: int, name: str, value: Any, *, after: int) -> bool:
    """Replace the single `name = ...` line in lines[start:end], or insert one after `after`."""
    hits = [i for i in range(start, end) if _key_line(lines[i], name) is not None]
    if len(hits) > 1:
        return False
    indent = re.match(r"\s*", lines[hits[0] if hits else after]).group(0) if hits else ""
    new = f"{indent}{name} = {_toml_scalar(value)}\n"
    if hits:
        lines[hits[0]] = new
    else:
        lines.insert(after + 1, new)
    return True


def _rewrite_guard_definition(
    text: str, parsed: dict[str, Any], pos: tuple[int, int], old_command: str, command: str,
) -> str | None:
    """The config text with the guard at `pos` rewritten IN PLACE (same key) to the canonical
    command, timeout and matcher (T-4395 r1: a narrower matcher is a weaker guard, so it is
    normalized), every other key kept. None unless the result parses to exactly the original
    document with only those values changed. A matcher change is refused when the group holds
    other hooks too (it would silently widen them)."""
    group, _hook = _guard_hook(parsed, pos)
    if group.get("matcher") != WORKER_GUARD_MATCHER and len(group.get("hooks") or []) != 1:
        return None
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    cmd_lines = [i for i, line in enumerate(lines) if _key_line(line, "command") == old_command]
    if len(cmd_lines) != 1:
        return None
    ci = cmd_lines[0]
    start, end = _table_bounds(lines, ci)
    indent = re.match(r"\s*", lines[ci]).group(0)
    lines[ci] = f"{indent}command = {_toml_scalar(command)}\n"
    if not _set_line(lines, start, end, "timeout", WORKER_GUARD_TIMEOUT_S, after=ci):
        return None
    group_headers = [i for i in range(start - 1, -1, -1) if lines[i].strip() == "[[hooks.PreToolUse]]"]
    if not group_headers:
        return None
    gh = group_headers[0]
    _gs, ge = _table_bounds(lines, gh)
    if not _set_line(lines, gh + 1, ge, "matcher", WORKER_GUARD_MATCHER, after=gh):
        return None
    new_text = "".join(lines)
    expected = copy.deepcopy(parsed)
    exp_group, exp_hook = _guard_hook(expected, pos)
    exp_hook["command"] = command
    exp_hook["timeout"] = WORKER_GUARD_TIMEOUT_S
    exp_group["matcher"] = WORKER_GUARD_MATCHER
    try:
        return new_text if tomllib.loads(new_text) == expected else None
    except tomllib.TOMLDecodeError:
        return None


def _set_trusted_hash(text: str, parsed: dict[str, Any], key: str, digest: str) -> str | None:
    """`text` with state[key].trusted_hash = digest (replacing any stale one, keeping sibling
    keys), or None unless that is provably the ONLY change."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = f"[hooks.state.{_toml_scalar(key)}]"
    headers = [i for i, line in enumerate(lines) if line.strip() == header]
    if len(headers) > 1:
        return None
    if headers:
        hs, he = _table_bounds(lines, headers[0])
        if not _set_line(lines, hs + 1, he, "trusted_hash", digest, after=hs):
            return None
        new_text = "".join(lines)
    else:
        new_text = "".join(lines).rstrip("\n") + f"\n\n{header}\ntrusted_hash = {_toml_scalar(digest)}\n"
    expected = copy.deepcopy(parsed)
    expected.setdefault("hooks", {}).setdefault("state", {}).setdefault(key, {})["trusted_hash"] = digest
    try:
        return new_text if tomllib.loads(new_text) == expected else None
    except tomllib.TOMLDecodeError:
        return None


# T-4428: a verified-marker in CODEX_HOME lets a launch skip the real home's hooks/list (median
# 1.47s, up to _HOOKS_LIST_TIMEOUT_S on a wedged app-server) when NOTHING codex's trust verdict
# depends on has changed since a hooks/list last said "current + trusted": config.toml's exact
# bytes (the definition AND its [hooks.state] trusted_hash), the realpath key, and the codex
# binary (a codex upgrade can change currentHash, turning a stored hash "modified"). It is only
# ever a reason to SKIP THE READ; any mismatch, a missing/unreadable/malformed marker or an
# exception falls back to the read, and so to the unchanged trust/repair logic.
_VERIFIED_MARKER_NAME = ".pantheon-worker-guard-verified.json"
_VERIFIED_MARKER_VERSION = 1


def _codex_binary_identity(executable: Path) -> dict[str, Any]:
    """Stat-only identity of the codex install (no subprocess). ctime_ns and the inode move on
    ANY replacement even if an installer preserves mtime; for the npm layout (bin/codex.js, a thin
    launcher whose bytes can outlive a version) the package.json holding the version and the
    pinned native-binary versions is hashed too. Raises when the executable can't be stat'ed."""
    real = os.path.realpath(executable)
    st = os.stat(real)
    identity: dict[str, Any] = {
        "path": str(executable), "realpath": real, "dev": st.st_dev, "ino": st.st_ino,
        "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
    }
    package_json = Path(real).parent.parent / "package.json"
    if package_json.is_file():
        identity["package_json_sha256"] = hashlib.sha256(package_json.read_bytes()).hexdigest()
    return identity


def _verified_marker_body(codex_home: Path, key: str, command: str, config_bytes: bytes,
                          executable: Path) -> dict[str, Any]:
    return {
        "version": _VERIFIED_MARKER_VERSION,
        "key": key,
        "command": command,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "codex": _codex_binary_identity(executable),
    }


def _verified_marker_hit(codex_home: Path, key: str, command: str, *, executable: Path) -> bool:
    """True only when the marker matches the CURRENT config bytes, key, command and binary."""
    try:
        config_bytes = (codex_home / "config.toml").read_bytes()
        expected = _verified_marker_body(codex_home, key, command, config_bytes, executable)
        stored = json.loads((codex_home / _VERIFIED_MARKER_NAME).read_text(encoding="utf-8"))
        return stored == expected
    except Exception:  # noqa: BLE001 - missing/unreadable/malformed: no hit, read instead
        return False


def _write_verified_marker(codex_home: Path, body: dict[str, Any]) -> None:
    """Atomic (temp + rename), 0600. Best-effort: a failure only costs the next launch a read."""
    tmp_name = None
    try:
        directory = Path(os.path.realpath(codex_home))
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_VERIFIED_MARKER_NAME}.", suffix=".tmp",
                                        dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, directory / _VERIFIED_MARKER_NAME)
        tmp_name = None
    except Exception as exc:  # noqa: BLE001 - never fatal to a launch
        _worker_guard_log(f"{codex_home}: verified-marker not written: {exc!r}")
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _read_real_guard(codex_home: Path, account_home: Path, key: str, *, executable: Path, read_hooks):
    """The real home's hooks/list entry for `key` ({} if absent); None if the read failed or timed out."""
    try:
        hooks = read_hooks(executable, codex_home=codex_home, cwd=account_home,
                           timeout=_HOOKS_LIST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - treated as "could not read"
        _worker_guard_log(f"hooks/list on {codex_home} raised {exc!r}")
        return None
    if hooks is None:
        return None
    return next((h for h in hooks if h.get("key") == key), {})


def ensure_worker_guard_hook(
    codex_home: Path,
    account_home: Path,
    *,
    executable: Path,
    read_hooks=read_codex_app_server_hooks,
    trust_marker: bool = True,
) -> bool:
    """Idempotently wire, UPGRADE and trust the worker-guard PreToolUse hook in ``config.toml``
    (T-4166; upgrade path T-4395 / T-4397 part A).

    Every seat with a codex runtime needs it (T-4160): codex 0.147 ships multi_agent
    default-on, so without the hook a failover can spawn codex subagents that hold nothing
    back. Skipped entirely when the seat's clone has no worker-guard.py (a PreToolUse hook whose
    script is missing could fail every main-loop tool call). Refuses (raises) on a config.toml
    it cannot parse. Hook keys use the REALPATH of CODEX_HOME, as codex does. Cases:

    * CURRENT guard (this command, timeout 30, matcher Bash|apply_patch): the real home's
      hooks/list (bounded) is asked; trusted -> no-op. Untrusted or "modified" (a stale hash):
      codex's own currentHash for the definition ON DISK is written as trusted_hash, the only
      change, in one write. T-4428: a "current + trusted" verdict records a verified-marker
      (sha256 of config.toml's bytes + key + command + codex binary identity); while all of those
      are unchanged the hooks/list read is skipped (``trust_marker=False`` always reads). A codex
      upgrade changes the binary identity, so a stale hash it causes is still read and repaired.
    * STALE guard (the pre-T-4395 command, or a stale timeout/matcher): rewritten IN PLACE (so
      its position-based key is unchanged) to the canonical command/timeout/matcher, every other
      key kept. The new hash is precomputed in a scratch CODEX_HOME holding EXACTLY that
      rewritten definition (the hash covers only the definition), then the definition AND its
      trusted_hash land in ONE atomic write. Codex never sees an untrusted/"modified" guard:
      no fail-open window. After the write the real home is re-read (bounded); unless it
      reports the guard trusted under that hash, the ORIGINAL bytes are restored, but only if
      the file still holds what was written (compare-and-swap; a concurrent edit is left alone
      and logged). A failed precompute, or an edit that isn't provably minimal, leaves the
      file untouched.
    * NO guard: the canonical block is appended with its precomputed trusted_hash in one write;
      if the hash can't be read the block is still wired (T-4166 best-effort: an untrusted hook
      runs under this adapter's --dangerously-bypass-hook-trust launch, and the parity axis
      codex-worker-guard-hook-trusted flags it).

    Every upgrade/repair/restore/refusal is logged to stderr. Returns True iff ``config.toml``
    was left modified.
    """
    if not (account_home / "pantheon-config" / "tools" / "hooks" / "worker-guard.py").is_file():
        return False
    config_path = codex_home / "config.toml"
    command = worker_guard_command(account_home)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    try:
        parsed = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"codex config.toml at {config_path} is not valid TOML -- refusing to "
            "blind-insert the worker-guard hook into unparseable content"
        ) from exc
    positions = [
        (pos, cmd)
        for cmd in (command, *_legacy_worker_guard_commands(account_home))
        for pos in _guard_positions(parsed, cmd)
    ]
    current = [pos for pos, _cmd in positions if _guard_definition_current(parsed, pos, command)]
    if current:
        return _trust_current_guard(codex_home, account_home, config_path, text, parsed, current[0],
                                    executable=executable, read_hooks=read_hooks,
                                    trust_marker=trust_marker)
    if positions:
        if len(positions) != 1:
            _worker_guard_log(f"{config_path}: {len(positions)} stale worker-guard hooks; refusing "
                              "to upgrade an ambiguous config (left untouched)")
            return False
        (pos, old_command), = positions
        return _upgrade_guard(codex_home, account_home, config_path, text, parsed, pos, old_command,
                              command, executable=executable, read_hooks=read_hooks)
    wired = (text.rstrip("\n") + "\n" if text.strip() else "") + _WORKER_GUARD_BLOCK.format(
        command=_toml_scalar(command)
    )
    wired_parsed = tomllib.loads(wired)
    (pos,) = _guard_positions(wired_parsed, command)
    definition = _guard_definition_toml(wired_parsed, pos)
    digest = definition and _precompute_guard_hash(definition, command, executable=executable,
                                                   account_home=account_home, read_hooks=read_hooks)
    trusted = digest and _set_trusted_hash(wired, wired_parsed, _guard_key(codex_home, pos), digest)
    _write_config_preserving_mode(config_path, trusted or wired)
    _worker_guard_log(f"{config_path}: wired the worker-guard "
                      + ("with its trusted_hash" if trusted else "UNTRUSTED (hash unreadable)"))
    return True


def _trust_current_guard(codex_home, account_home, config_path, text, parsed, pos, *,
                         executable, read_hooks, trust_marker: bool = True) -> bool:
    """A current definition: no-op when codex reports it trusted; otherwise (untrusted, or a
    STALE hash reading "modified") write codex's currentHash for the on-disk definition as its
    trusted_hash, the only change, in one write (T-4395 r1). The reported entry must be OUR
    command, and the write is a compare-and-swap against the bytes read at the start (r2), so a
    config edited between the read and the app-server call is never re-trusted blind."""
    key = _guard_key(codex_home, pos)
    command = _guard_hook(parsed, pos)[1]["command"]
    if trust_marker and _verified_marker_hit(codex_home, key, command, executable=executable):
        return False  # T-4428: nothing the verdict depends on changed since codex said trusted
    try:
        bytes_before = config_path.read_bytes()
    except OSError:
        bytes_before = None
    # The marker body (the binary identity included) is taken BEFORE the read: a verdict from
    # the old binary must never be recorded under an upgraded one's identity (T-4428 r2).
    body_before = None
    if bytes_before is not None:
        try:
            body_before = _verified_marker_body(codex_home, key, command, bytes_before, executable)
        except Exception:  # noqa: BLE001 - no body: no marker, the next launch reads
            body_before = None
    guard = _read_real_guard(codex_home, account_home, key, executable=executable, read_hooks=read_hooks)
    if not guard:
        _worker_guard_log(f"{config_path}: unverifiable, hooks/list "
                          + ("failed or timed out" if guard is None else f"reported no hook at {key}")
                          + "; left as is")
        return False
    if guard.get("command") != command:
        _worker_guard_log(f"{config_path}: re-trust REFUSED, codex reports {guard.get('command')!r} at "
                          f"{key}, not the guard (config changed under us?)")
        return False
    if guard.get("trustStatus") == "trusted":
        # Record the verdict only for the bytes AND binary codex actually judged: config bytes
        # and binary identity unchanged across the read, and still what this run parsed.
        if (body_before is not None and _file_holds(config_path, text)
                and _file_bytes(config_path) == bytes_before):
            try:
                body_after = _verified_marker_body(codex_home, key, command, bytes_before, executable)
            except Exception:  # noqa: BLE001
                body_after = None
            if body_after == body_before:
                _write_verified_marker(codex_home, body_before)
        return False
    digest = guard.get("currentHash")
    if not (isinstance(digest, str) and digest.startswith("sha256:")):
        return False
    new_text = _set_trusted_hash(text, parsed, key, digest)
    if new_text is None:
        _worker_guard_log(f"{config_path}: re-trust REFUSED, no provably minimal edit of {key}")
        return False
    if not _file_holds(config_path, text):
        _worker_guard_log(f"{config_path}: re-trust NOT WRITTEN, config.toml changed since it was "
                          "read (concurrent edit?); left as found")
        return False
    stale = _stored_trusted_hash(parsed, key)
    _write_config_preserving_mode(config_path, new_text)
    _worker_guard_log(f"{config_path}: {'REPAIRED stale' if stale else 'TRUSTED'} {key} "
                      f"(was {guard.get('trustStatus')}), trusted_hash {stale} -> {digest}")
    return True


def _file_holds(config_path: Path, expected: str) -> bool:
    try:
        return config_path.read_text(encoding="utf-8") == expected
    except FileNotFoundError:
        return expected == ""
    except OSError:
        return False


def _file_bytes(config_path: Path) -> bytes | None:
    try:
        return config_path.read_bytes()
    except OSError:
        return None


def _restore_if_unchanged(config_path: Path, written: str, original: str) -> bool:
    """Compare-and-swap restore: only when the file still holds exactly what we wrote."""
    if not _file_holds(config_path, written):
        _worker_guard_log(f"{config_path}: NOT RESTORED, config.toml changed since the upgrade "
                          "write (concurrent edit?); left as found -- check it by hand")
        return False
    _write_config_preserving_mode(config_path, original)
    return True


def _upgrade_guard(codex_home, account_home, config_path, text, parsed, pos, old_command, command, *,
                   executable, read_hooks) -> bool:
    """One-write upgrade of a stale guard + its trusted_hash, verified, else restored (T-4395)."""
    key = _guard_key(codex_home, pos)
    rewritten = _rewrite_guard_definition(text, parsed, pos, old_command, command)
    if rewritten is None:
        _worker_guard_log(f"{config_path}: upgrade REFUSED, no provably minimal in-place edit of "
                          f"{key}; left untouched")
        return False
    rewritten_parsed = tomllib.loads(rewritten)
    definition = _guard_definition_toml(rewritten_parsed, pos)
    digest = definition and _precompute_guard_hash(definition, command, executable=executable,
                                                   account_home=account_home, read_hooks=read_hooks)
    if not digest:
        _worker_guard_log(f"{config_path}: upgrade SKIPPED, new hash unreadable; the existing "
                          "guard is left untouched")
        return False
    new_text = _set_trusted_hash(rewritten, rewritten_parsed, key, digest)
    if new_text is None:
        _worker_guard_log(f"{config_path}: upgrade REFUSED, no provably minimal state edit of "
                          f"{key}; left untouched")
        return False
    old_hash = _stored_trusted_hash(parsed, key)
    _write_config_preserving_mode(config_path, new_text)
    _worker_guard_log(f"{config_path}: UPGRADED {key} to {command!r} timeout "
                      f"{WORKER_GUARD_TIMEOUT_S} matcher {WORKER_GUARD_MATCHER!r}, trusted_hash "
                      f"{old_hash} -> {digest}")
    guard = _read_real_guard(codex_home, account_home, key, executable=executable,
                             read_hooks=read_hooks)
    if guard and guard.get("trustStatus") == "trusted" and guard.get("currentHash") == digest:
        _worker_guard_log(f"{config_path}: verified {key} trusted under {digest}")
        if guard.get("enabled") is False:
            _worker_guard_log(f"{config_path}: WARNING {key} is DISABLED (enabled = false in its "
                              "state entry): trusted but it will NOT run -- fail-open until re-enabled")
        return True
    reason = "hooks/list failed or timed out" if guard is None else (
        f"trust={guard.get('trustStatus')} hash={guard.get('currentHash')}")
    if _restore_if_unchanged(config_path, new_text, text):
        _worker_guard_log(f"{config_path}: RESTORED the original config, could not verify the "
                          f"upgraded guard ({reason}); the previous guard is back in force")
        return False
    return True


def _is_codex_process(argv: list[str]) -> bool:
    heads = [os.path.basename(a) for a in argv[:2]]
    return (any(h in ("codex", "codex.js") for h in heads)
            or any("/@openai/codex/" in a for a in argv[:2]))


def live_codex_sessions(codex_home: Path, *, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Running codex processes of THIS account whose CODEX_HOME resolves to `codex_home`, each
    marked as started before or after config.toml's mtime (Recovery, T-4395 r1).

    A codex session keeps the hooks + trust it read at startup, so a home can be current ON DISK
    while a session started before the upgrade still runs the old block. Read-only and
    best-effort from /proc: process start = boot time + starttime ticks; a process whose
    environ/stat can't be read is counted in ``unreadable``. ``measurable`` is False when
    /proc itself can't be listed. Informational only: it never gates ``ok``.
    """
    config = Path(os.path.realpath(codex_home)) / "config.toml"
    want = os.path.realpath(codex_home)
    try:
        mtime = config.stat().st_mtime
    except OSError:
        mtime = None
    try:
        btime = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines()
                     if line.startswith("btime "))
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except (OSError, StopIteration, ValueError, IndexError):
        return {"measurable": False, "config_mtime": mtime, "sessions": [], "unreadable": 0}
    ticks = os.sysconf("SC_CLK_TCK")
    uid = os.getuid()
    sessions: list[dict[str, Any]] = []
    unreadable = 0
    for entry in sorted(entries, key=lambda p: int(p.name)):
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = [a for a in (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
            if not _is_codex_process(argv):
                continue
        except OSError:
            continue  # exited, or not ours: not a session of this account
        try:
            env = dict(
                kv.split("=", 1) for kv in (entry / "environ").read_bytes().decode("utf-8", "replace").split("\0")
                if "=" in kv
            )
            stat = (entry / "stat").read_text()
            start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        except (OSError, ValueError, IndexError):
            unreadable += 1
            continue
        home = env.get("CODEX_HOME") or os.path.join(env.get("HOME", ""), ".codex")
        if os.path.realpath(home) != want:
            continue
        started = btime + start_ticks / ticks
        sessions.append({
            "pid": int(entry.name),
            "argv0": argv[0] if argv else "",
            "started_at": started,
            "started_before_config_mtime": None if mtime is None else started < mtime,
        })
    return {"measurable": True, "config_mtime": mtime, "sessions": sessions, "unreadable": unreadable}


def worker_guard_hook_status(
    codex_home: Path,
    account_home: Path,
    *,
    executable: Path,
    read_hooks=read_codex_app_server_hooks,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Per-home rollout verdict for Recovery (T-4395): is the block current, enabled and codex-trusted?

    ``block_current``: a guard with this seat's current command, timeout 30 and the canonical
    matcher. ``hash_matches``: its stored trusted_hash equals codex's own currentHash.
    ``enabled``: codex's view (a disabled guard never runs, i.e. fails open). ``measurable`` is
    False when the app-server read failed (trust fields are then None). ``ok`` needs all of it,
    trustStatus "trusted", and no leftover legacy guard. That is the ON-DISK verdict;
    ``live_sessions`` (informational, never part of ``ok``) says which running codex sessions
    of this home started before the config's last write and so still run their startup snapshot.
    """
    config_path = codex_home / "config.toml"
    command = worker_guard_command(account_home)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    parsed = tomllib.loads(text) if text.strip() else {}
    positions = _guard_positions(parsed, command)
    legacy = [pos for cmd in _legacy_worker_guard_commands(account_home)
              for pos in _guard_positions(parsed, cmd)]
    pos = positions[0] if positions else None
    key = _guard_key(codex_home, pos) if pos else None
    block_current = bool(pos) and _guard_definition_current(parsed, pos, command)
    stored = _stored_trusted_hash(parsed, key) if key else None
    guard = _read_real_guard(codex_home, account_home, key, executable=executable,
                             read_hooks=read_hooks) if key else None
    measurable = not key or guard is not None
    trust = (guard or {}).get("trustStatus")
    current_hash = (guard or {}).get("currentHash")
    enabled = (guard or {}).get("enabled") if guard else None
    hash_matches = bool(stored) and stored == current_hash
    return {
        "config_path": str(config_path),
        "key": key,
        "command": command,
        "block_current": block_current,
        "legacy_present": bool(legacy),
        "stored_hash": stored,
        "current_hash": current_hash,
        "trust_status": trust,
        "enabled": enabled,
        "hash_matches": hash_matches,
        "measurable": measurable,
        "ok": (block_current and trust == "trusted" and enabled is True and hash_matches
               and not legacy),
        # ON DISK above; LIVE below (informational: a session keeps its startup snapshot).
        "live_sessions": live_codex_sessions(codex_home, proc_root=proc_root),
    }


def _write_config_preserving_mode(config_path: Path, text: str) -> None:
    """Atomic replace; keeps an existing file's mode, creates a new one 0600 (it can hold
    MCP env and per-project trust). Writes through a symlink, as the siblings' write_text does."""
    config_path = Path(os.path.realpath(config_path))
    mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(f".{config_path.name}.t4166.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, config_path)


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def prepare_codex_launch(binding: Any, *, cwd: Path) -> None:
    """Seed a codex runtime's config before its interactive launch (T-5011 PR-A).

    Moved verbatim out of ``runtime_controller._start_runtime`` (T-4924 plan
    §2.4), same order and arguments. The home is ``binding.account_home /
    ".codex"``, NEVER an adapter's ``codex_home``: that is what the controller
    always seeded, and the two differ on the codex seat (its manifest
    ``codex_home`` is the account home itself).
    """
    codex_home = binding.account_home / ".codex"
    # TRUST modal -> config, never a keystroke (T-2594). cwd is the ACTUAL
    # resolved launch cwd, never a hardcoded guess -- see
    # ensure_project_trusted's own docstring.
    ensure_project_trusted(codex_home, cwd)
    # Update-picker/banner suppression (T-2597 wedges #2/#3) -- same
    # config-seed path as the trust fix above.
    ensure_update_check_disabled(codex_home)
    # Model-migration prompt suppression for known-hideable active
    # presets (T-2597 wedge #4) -- see ensure_model_migration_prompts_
    # hidden's own docstring for the documented residual gap.
    ensure_model_migration_prompts_hidden(codex_home)
    # T-4166: codex 0.147 ships multi_agent default-on, so every codex runtime
    # (the codex seat and each Claude seat's fallback) gets the worker-guard
    # PreToolUse hook wired + trusted before launch -- see
    # ensure_worker_guard_hook's own docstring.
    ensure_worker_guard_hook(
        codex_home,
        binding.account_home,
        executable=Path(binding.launch.executable),
    )


if __name__ == "__main__":
    # Manual/ops entrypoint only (T-2600) -- `python3 -m tools.harness_adapter.codex
    # [--codex-home PATH] [--dry-run]`. NOT invoked on import; NOT wired into any
    # bringup path or timer.
    sys.exit(gc_trust_entries_cli())
