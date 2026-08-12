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
import json
import re
import sys
import tomllib
from collections.abc import Iterable
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
    McpDescriptor,
    MemoryDescriptor,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)
from tools.harness_adapter.provider_quota import (
    CodexAppServerQuotaObserver,
    read_codex_app_server_rate_limits,
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
        allowed_native = {"sandbox", "config_overrides"}
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

    def build_interactive_launch(self, target: TmuxTarget) -> CommandSpec:
        argv = [str(self.executable)]
        model = self.launch.model if self.launch else None
        if model:
            argv.extend(["--model", model])
        self._append_native_args(argv)
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
            cwd=Path.cwd(),
            env={
                "CODEX_HOME": str(self._codex_home / ".codex"),
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
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
        sandbox = self.launch.native.get("sandbox")
        if isinstance(sandbox, str):
            argv.extend(["--sandbox", sandbox])
        overrides = self.launch.native.get("config_overrides")
        if isinstance(overrides, dict):
            for key, value in sorted(overrides.items()):
                argv.extend(["--config", f"{key}={_toml_scalar(value)}"])

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
          (2) sandbox_mode="workspace-write" + network_access=true — ORG-031 §ii.
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

        Shape (ORG-031 §ii ratified config + the per-tool MCP gate):
            {"approval_policy": "never",
             "sandbox_mode": "workspace-write",
             "sandbox_workspace_write": {"network_access": True},
             "mcp_tool_approval": "approve",  # per [mcp_servers.*.tools.*]
             "hook_trust": "--dangerously-bypass-hook-trust"}
        """
        return {
            "approval_policy": "never",
            "sandbox_mode": "workspace-write",
            "sandbox_workspace_write": {
                "network_access": True,
            },
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
        """Codex sandbox and approval policy config (ORG-031 §ii, live-verified).

        approval_policy × sandbox_mode (2 orthogonal axes) +
        sandbox_workspace_write.{writable_roots, network_access}.

        Ratified config (ORG-031 §ii):
            approval_policy="never"
            sandbox_mode="workspace-write"
            [sandbox_workspace_write]
            network_access=true

        Backup of pre-unattended config at: ~/.codex/config.toml.bak.preunattended
        """
        return {
            "harness": "codex",
            "approval_policy": "never",
            "sandbox_mode": "workspace-write",
            "sandbox_workspace_write": {"network_access": True},
            "config_backup": "~/.codex/config.toml.bak.preunattended",
            "ratified_by": "ORG-031 §ii",
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


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


if __name__ == "__main__":
    # Manual/ops entrypoint only (T-2600) -- `python3 -m tools.harness_adapter.codex
    # [--codex-home PATH] [--dry-run]`. NOT invoked on import; NOT wired into any
    # bringup path or timer.
    sys.exit(gc_trust_entries_cli())
