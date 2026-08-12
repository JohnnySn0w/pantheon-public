"""ClaudeHarnessAdapter — concrete binding for the claude-code CLI harness.

Implements PantheonHarnessAdapter for claude-code (the primary Pantheon harness:
Orchestrator/HO, Specialist, Recovery).  All 7 generalized + 2 bridge surfaces are implemented
with real key/filename translations.

Not-abstracted surfaces (sandbox_config, profiles_config, app_server_config,
cloud_config, doctor_config) remain provider-specific descriptions. They are
not forced into Codex-shaped policy.

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i)
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      notes.md §2 (capability map)
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Mapping

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
    McpDescriptor,
    MemoryDescriptor,
    ProviderBillingEvidence,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)
from tools.harness_adapter.quota import QuotaObserver
from tools.harness_adapter.provider_quota import (
    ClaudeAcceptanceQuotaOverlayObserver,
    ClaudeStatuslineQuotaObserver,
    UnavailableQuotaObserver,
    valid_acceptance_run_id,
)


class ClaudeHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for claude-code (Orchestrator / Specialist / Recovery instances).

    Args:
        role_domain: ORG-027 instance role-key (e.g. "orchestrator").
        claude_home: path to the claude user home; defaults to Path.home().
                     Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "claude"

    def __init__(
        self,
        role_domain: str,
        claude_home: Path | None = None,
        mailbox_path: Path | None = None,
        launch: HarnessLaunchConfig | None = None,
    ) -> None:
        super().__init__(role_domain, launch=launch)
        self._claude_home = claude_home if claude_home is not None else Path.home()
        self._mailbox_path = mailbox_path

    # -------------------------------------------------------------------------
    # Typed lifecycle contract
    # -------------------------------------------------------------------------

    def capabilities(self) -> HarnessCapabilities:
        hook_names = (
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PostToolUseFailure",
            "Notification",
            "UserPromptSubmit",
            "SessionStart",
            "SessionEnd",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PreCompact",
            "PostCompact",
            "TeammateIdle",
            "TaskCompleted",
            "ConfigChange",
            "WorktreeCreate",
            "WorktreeRemove",
            "InstructionsLoaded",
        )
        hooks = {
            name: HookCapability(
                event=name,
                matchers=name
                not in {
                    "UserPromptSubmit",
                    "SessionStart",
                    "SessionEnd",
                    "Stop",
                    "SubagentStart",
                    "SubagentStop",
                    "PreCompact",
                    "PostCompact",
                },
                can_block=name
                in {
                    "PreToolUse",
                    "PermissionRequest",
                    "UserPromptSubmit",
                    "Stop",
                    "SubagentStop",
                    "TaskCompleted",
                    "ConfigChange",
                    "WorktreeCreate",
                },
                can_continue=name in {"Stop", "SubagentStop"},
                handler_types=("command", "prompt", "agent"),
            )
            for name in hook_names
        }
        return HarnessCapabilities(
            harness="claude",
            session_resume_by_id=True,
            session_resume_last=True,
            structured_progress=True,
            structured_output_schema=True,
            local_generated_memory=True,
            native_stop_continuation=True,
            inbound_mechanisms=("channel", "maildir"),
            hook_events=hooks,
        )

    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        argv = [
            str(self.executable),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        model = request.model or (self.launch.model if self.launch else None)
        if model:
            argv.extend(["--model", model])
        if request.output_schema:
            argv.extend(["--json-schema", str(request.output_schema)])
        return CommandSpec(
            argv=tuple(argv),
            stdin=request.prompt,
            cwd=request.cwd,
        )

    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        cwd = session.cwd if session.cwd is not None else Path.cwd()
        return CommandSpec(
            argv=(
                str(self.executable),
                "-p",
                "--resume",
                session.session_id,
                "--output-format",
                "stream-json",
                "--verbose",
            ),
            stdin=prompt,
            cwd=cwd,
        )

    def parse_event(self, line: str) -> HarnessEvent | None:
        raw, parse_error = parse_json_object(line)
        if parse_error:
            return HarnessEvent(
                harness="claude",
                kind="error",
                session_id=None,
                message=parse_error,
                usage=None,
                raw={"line": line.rstrip("\n"), "category": "provider"},
            )
        if raw is None:
            return None

        native_type = raw.get("type")
        session_id = raw.get("session_id")
        session_id = session_id if isinstance(session_id, str) else None
        usage_raw = raw.get("usage")
        usage = _claude_usage(usage_raw, raw.get("total_cost_usd"))
        message: str | None = None
        kind = "progress"

        if native_type == "system" and raw.get("subtype") == "init":
            kind = "session_started"
        elif native_type == "assistant":
            content = raw.get("message")
            if isinstance(content, dict):
                message = _claude_text(content.get("content"))
            kind = "progress"
        elif native_type in {"tool_progress", "tool_use"}:
            kind = "tool_started"
        elif native_type == "tool_use_summary":
            kind = "tool_completed"
            summary = raw.get("summary")
            message = summary if isinstance(summary, str) else None
        elif native_type == "rate_limit_event":
            kind = "retry_scheduled"
            message = str(raw.get("message") or "rate limit event")
        elif native_type == "result":
            result = raw.get("result")
            message = result if isinstance(result, str) else None
            is_error = bool(raw.get("is_error")) or raw.get("subtype") not in {
                None,
                "success",
            }
            kind = "turn_failed" if is_error else "turn_completed"
        return HarnessEvent(
            harness="claude",
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
        captured = tuple(events)
        return finalize_events(
            "claude",
            captured,
            returncode=returncode,
            stderr=stderr,
            provider_billing=_claude_provider_billing(captured),
        )

    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        return deliver_to_mailbox(
            "claude",
            request,
            default_inbox=self._mailbox_path,
            default_sender="harness-adapter",
        )

    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        return HarnessEvent(
            harness="claude",
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
                    check_id="claude.runtime_profile",
                    status="warn",
                    summary="legacy adapter has no immutable runtime profile",
                    authoritative=True,
                    evidence={"profile": "", "legacy": "true"},
                ),
            )
        allowed_native = {"permission_mode", "channels"}
        unknown = sorted(set(self.launch.native) - allowed_native)
        status = "fail" if unknown else "pass"
        return (
            DiagnosticObservation(
                check_id="claude.runtime_profile",
                status=status,
                summary=(
                    "claude runtime profile is valid"
                    if status == "pass"
                    else "claude runtime profile contains unknown native keys"
                ),
                authoritative=True,
                evidence={
                    "harness": self.launch.harness,
                    "profile": self.launch.profile,
                    "mode": self.launch.mode,
                    "meter": self.launch.meter,
                    "quota_source": self.launch.quota_source,
                    "unknown_native_keys": ",".join(unknown),
                    "native_namespace": "claude",
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
                load_order=("cwd-upward", "claude-home"),
                authoritative=True,
                evidence={"harness": "claude", "cwd": str(cwd)},
            ),
            auth=self.auth_descriptor(),
            memory=MemoryDescriptor(
                available=True,
                enabled=False,
                readable=None,
                writable=None,
                evidence={
                    "harness": "claude",
                    "enabled_by_adapter": "false",
                    "claim": "descriptor-only",
                },
            ),
            mcp=McpDescriptor(
                config_paths=(self.mcp_server_config_path(), cwd / ".mcp.json"),
                configured_servers=(),
                scope=("user", "project"),
                authoritative=False,
                evidence={"harness": "claude", "native_format": "json"},
            ),
            roles=RoleDescriptor(
                config_paths=(self._claude_home / ".claude" / "settings.json",),
                configured_roles=(),
                delegation_modes=("agent-tool",),
                authoritative=False,
                evidence={"harness": "claude", "schema": "claude-native"},
            ),
        )

    def auth_descriptor(self) -> AuthDescriptor:
        return AuthDescriptor(
            mechanisms=("oauth", "api-key"),
            material_paths=(
                self._claude_home / ".claude" / ".credentials.json",
                self._claude_home / ".claude" / "settings.json",
            ),
            observed_state="unverified",
            authoritative=False,
            evidence={
                "harness": "claude",
                "descriptor_only": "true",
                "live_probe": "not-run",
            },
        )

    def build_interactive_launch(self, target: TmuxTarget) -> CommandSpec:
        argv = [str(self.executable)]
        channels = (
            self.launch.native.get("channels")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if isinstance(channels, str) and channels:
            argv.extend(["--channels", channels])
        model = self.launch.model if self.launch else None
        if model:
            argv.extend(["--model", model])
        permission_mode = (
            self.launch.native.get("permission_mode")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if permission_mode is None:
            permission_mode = "default"
        if isinstance(permission_mode, str):
            argv.extend(["--permission-mode", permission_mode])
        return CommandSpec(
            argv=tuple(argv),
            stdin=None,
            cwd=Path.cwd(),
            env={
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
        )

    def quota_observer(self, state_dir: Path) -> QuotaObserver:
        profile = self.launch.profile if self.launch else "legacy"
        run_id = os.environ.get("PANTHEON_ACCEPTANCE_RUN_ID")
        overlay = os.environ.get("PANTHEON_ACCEPTANCE_QUOTA_OVERLAY")
        if run_id is None and overlay is None:
            return ClaudeStatuslineQuotaObserver(
                state_dir / "quota" / "claude-statusline.json",
                profile=f"claude/{profile}",
            )
        if not _valid_acceptance_overlay_config(run_id, overlay):
            return UnavailableQuotaObserver(
                profile=f"claude/{profile}",
                reason="invalid-acceptance-overlay-config",
                evidence={
                    "provider": "claude",
                    "source": "acceptance-overlay",
                    "fixture": "true",
                    "run_id_present": str(run_id is not None).lower(),
                    "overlay_present": str(overlay is not None).lower(),
                },
            )
        assert run_id is not None
        assert overlay is not None
        canonical_path = state_dir / "quota" / "claude-statusline.json"
        return ClaudeAcceptanceQuotaOverlayObserver(
            Path(overlay),
            canonical_path=canonical_path,
            profile=f"claude/{profile}",
            run_id=run_id,
            state_dir=state_dir,
        )

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return "CLAUDE.md" — the claude-code standing-prompt filename."""
        return "CLAUDE.md"

    def mcp_server_config_path(self) -> Path:
        """Return ~/.claude.json — user/local MCP registrations live here."""
        return self._claude_home / ".claude.json"

    def hooks_config_path(self) -> Path:
        """Return ~/.claude/settings.json — hooks live in the same settings file."""
        return self._claude_home / ".claude" / "settings.json"

    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        """Return a claude-code Agent-tool spawn descriptor for the given role.

        Shape (skeleton stub; not wired to real subprocess):
            {"harness": "claude", "role": role, "isolation": "none",
             "config_overrides": config_overrides}

        Runtime wiring uses the Agent tool with isolation and prompt parameters;
        see the team dispatch pattern in feedback_implementer_dispatch_isolation_worktree_pattern.
        """
        return {
            "harness": "claude",
            "role": role,
            "isolation": "none",
            "config_overrides": config_overrides,
        }

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Return a claude --resume <session_id> descriptor.

        Shape: {"harness": "claude", "cli_args": ["--resume", session_id]}

        The session_id maps to a JSONL session file under
        ~/.claude/projects/<cwd-hash>/<session_id>.jsonl.
        The ho_resumer.py tool drives this surface at runtime.
        """
        return {
            "harness": "claude",
            "cli_args": ["--resume", session_id],
            "session_id": session_id,
        }

    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """Return ["claude", "-p", prompt] — the claude headless execution args.

        Args:
            prompt: the prompt string to pass to ``claude -p``.
            **kwargs: reserved for future harness-specific flags (model, etc.).

        Returns:
            CLI args list suitable for subprocess.run().
        """
        return [str(self.executable), "-p", prompt]

    def auth_status(self) -> dict[str, Any]:
        """Return auth state dict for claude-code.

        Shape:
            {"harness": "claude",
             "key_source": "ANTHROPIC_API_KEY",
             "settings_path": "<claude_home>/.claude/settings.json"}
        """
        return {
            "harness": "claude",
            "key_source": "ANTHROPIC_API_KEY",
            "settings_path": str(self.mcp_server_config_path()),
            "observed_state": self.auth_descriptor().observed_state,
            "descriptor_only": True,
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Compatibility shim over durable maildir delivery.

        BRIDGE SURFACE — mechanism: server pushes via the ``claude/channel`` MCP
        endpoint into the active session.  This capability is claude-code-specific
        and does NOT generalize to other harnesses (codex does not implement it;
        live-verified T-0714).

        ``deliver_inbound`` returns the receipt new callers should inspect.
        Queueing proves durability only; a separate Claude wake/channel bridge
        advances the receipt to submitted or acknowledged.
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
        """Return claude-code unattended-mode defaults.

        Pantheon keeps Claude Code in its explicit ``default`` permission mode
        and grants unattended actions through the account's scoped
        ``permissions.allow`` entries.  Bypass/dontAsk modes are security-posture
        changes and are not adapter defaults.

        Shape:
            {"permission_mode": "default",
             "flag": "--permission-mode default",
             "tool_approval": "scoped-permissions.allow"}
        """
        return {
            "permission_mode": "default",
            "flag": "--permission-mode default",
            "tool_approval": "scoped-permissions.allow",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (documented per-harness attributes)
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """Describe Claude Code's provider-specific sandbox and permission modes."""
        return {
            "harness": "claude",
            "mechanism": "native sandbox settings plus permission modes",
            "description": (
                "Claude Code has native sandbox configuration and permission modes. "
                "Pantheon's Unix-account boundary remains an additional org-tier layer."
            ),
        }

    @property
    def profiles_config(self) -> dict[str, Any]:
        """claude-code profiles are per-instance settings.json files (org-tier).

        No ``-p <profile>`` flag analog.  Each Pantheon agent (Orchestrator/Specialist/Recovery) has
        its own home-dir settings.json; profile separation is achieved via Unix-user
        separation.
        """
        return {
            "harness": "claude",
            "mechanism": "per-instance settings.json via Unix user separation",
            "description": (
                "No -p <profile> flag. Each agent has its own home-dir settings.json; "
                "profile separation is achieved via distinct Unix accounts."
            ),
        }

    @property
    def app_server_config(self) -> dict[str, Any]:
        """Describe Claude Code remote-control without equating it to Codex app-server."""
        return {
            "harness": "claude",
            "supported": True,
            "surfaces": ["remote-control"],
            "description": "Remote-control is native; its wire and policy are Claude-specific.",
        }

    @property
    def cloud_config(self) -> dict[str, Any]:
        """claude-code has no cloud-task ingest analog.

        Returns {"supported": False, "reason": "no claude-code analog"} so consumers
        can distinguish "no analog" from "configured-but-empty".
        """
        return {
            "harness": "claude",
            "supported": False,
            "reason": "no claude-code analog",
        }

    @property
    def doctor_config(self) -> dict[str, Any]:
        """Describe Claude's native diagnostics without claiming Codex equivalence."""
        return {
            "harness": "claude",
            "mechanism": "/doctor plus --safe-mode/config checks",
            "tools": ["/doctor", "--safe-mode"],
            "description": (
                "Native checks remain namespaced Claude diagnostics; Pantheon also "
                "uses JSONL/session and process checks."
            ),
        }


def _valid_acceptance_overlay_config(run_id: str | None, overlay: str | None) -> bool:
    if not valid_acceptance_run_id(run_id):
        return False
    if not isinstance(overlay, str) or not overlay.strip():
        return False
    return True


def _claude_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) or None


def _claude_usage(value: Any, total_cost: Any) -> Usage | None:
    if not isinstance(value, dict) and not isinstance(total_cost, (int, float)):
        return None
    value = value if isinstance(value, dict) else {}
    return Usage(
        input_tokens=_optional_int(value.get("input_tokens")),
        cached_input_tokens=_optional_int(
            value.get("cache_read_input_tokens")
            or value.get("cached_input_tokens")
        ),
        output_tokens=_optional_int(value.get("output_tokens")),
        cost_usd=float(total_cost) if isinstance(total_cost, (int, float)) else None,
    )


def _claude_provider_billing(
    events: tuple[HarnessEvent, ...],
) -> ProviderBillingEvidence | None:
    api_key_source: str | None = None
    result_usage: Usage | None = None
    total_cost_usd: float | None = None
    source_events: list[str] = []

    for event in events:
        if (
            event.raw.get("type") == "system"
            and event.raw.get("subtype") == "init"
        ):
            api_key_source = _claude_init_api_key_source(event.raw)
            if api_key_source is not None:
                source_events.append("system/init")
            break

    for event in reversed(events):
        if event.raw.get("type") != "result":
            continue
        result_usage = event.usage
        total_cost_usd = _optional_float(event.raw.get("total_cost_usd"))
        source_events.append("result")
        break

    if api_key_source is None and result_usage is None and total_cost_usd is None:
        return None
    return ProviderBillingEvidence(
        provider="claude",
        api_key_source=api_key_source,
        total_cost_usd=total_cost_usd,
        input_tokens=result_usage.input_tokens if result_usage else None,
        cached_input_tokens=(
            result_usage.cached_input_tokens if result_usage else None
        ),
        output_tokens=result_usage.output_tokens if result_usage else None,
        source_events=tuple(source_events),
    )


def _claude_init_api_key_source(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("apiKeySource")
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
