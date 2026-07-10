"""CodexHarnessAdapter — concrete binding for the OpenAI Codex CLI harness.

Implements PantheonHarnessAdapter for codex (the first non-claude Pantheon daemon,
stood up 2026-06-02T01:05Z per ledger, onboarded per ORG-031).

CRITICAL — session/recovery surface (harvest finding #2):
    Codex's recovery layer is claude-coupled and DEFERRED (T-0822).  The
    ``resume_session`` method raises NotImplementedError explicitly rather than
    pretending parity.  Reasons:

      - Storage: SQLite files (~/.codex/*.sqlite), NOT JSONL; no message.usage.
      - Resume mechanism: ``codex resume --last`` SUBCOMMAND, not the
        ``claude --resume <id>`` FLAG that ho_resumer.py calls.
      - Frame format: codex TUI frame does not match ``╭─╮``/``❯`` sentinels →
        context-fill-monitor, restart-drain, inbox-consumer, ho-resumer all
        no-op or time out.

    Codex runs on the bare supervise loop (Restart=always); full recovery deferred.
    Do NOT arm the claude recovery monitors against codex.

Binding deltas encoded from Recovery harvest
(recovery-bringup-gap-harvest-2026-06-09.md):
  - A2 mcp_server_config_path: TOML, NOT JSON; streamable-HTTP-only (SSE→shim
    required: T-0714 FINDING 1); cross-user perms apply (T-0714 FINDING 2).
  - A7 auth_status: OAuth-vs-key distinction; OAuth unlocks keyless image_gen.
  - B1 inject_inbound: poll-and-inject only; claude/channel NOT implemented.
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

from pathlib import Path
from typing import Any

from tools.harness_adapter.base import PantheonHarnessAdapter


class CodexHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for the OpenAI Codex CLI daemon.

    Args:
        role_domain: ORG-027 instance role-key for the target codex agent.
        codex_home: path to the codex user home; defaults to Path.home().
                    Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "codex"

    def __init__(self, role_domain: str, codex_home: Path | None = None) -> None:
        super().__init__(role_domain)
        self._codex_home = codex_home if codex_home is not None else Path.home()

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
        10 events (vs 8 for claude-code): adds SubagentStart + Compact pair.
        Same JSON/stdin/stdout + permissionDecision/exit-2 model (~80% overlap).
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
        """NOT IMPLEMENTED — codex session recovery is deferred per T-0822.

        The resume-by-id *concept* generalizes; the *mechanism* does not:

          - Storage: ~/.codex/*.sqlite (SQLite), NOT JSONL; no message.usage.
          - Mechanism: ``codex resume --last`` SUBCOMMAND (not ``claude --resume <id>``
            FLAG); ho_resumer.py's flag-based call does not port to codex.
          - Frame sentinels: codex TUI frame does not match the ``╭─╮``/``❯``
            patterns that context-fill-monitor, restart-drain, inbox-consumer, and
            ho-resumer depend on.

        Codex runs on the BARE SUPERVISE LOOP (Restart=always) until T-0822 is
        resolved.  Do NOT arm the claude recovery monitors against codex.

        Raises:
            NotImplementedError: always, with a message citing T-0822 and the
                                 structural reasons.
        """
        raise NotImplementedError(
            "CodexHarnessAdapter.resume_session is deferred per T-0822.  "
            "Codex sessions use SQLite storage (not JSONL), resume via the "
            "'codex resume --last' subcommand (not the '--resume <id>' flag), "
            "and the TUI frame does not match claude sentinel patterns.  "
            "Codex runs on the bare supervise loop until T-0822 is resolved."
        )

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
        return ["codex", "exec", prompt]

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
        """NOT IMPLEMENTED at skeleton tier — codex inbound is poll-and-inject.

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

        Runtime wiring is outside the skeleton scope.  The concrete implementation
        will use tmux send-keys or codex exec to inject the message as a new turn.

        Raises:
            NotImplementedError: always at skeleton tier, with T-0714 citation.
        """
        raise NotImplementedError(
            "CodexHarnessAdapter.inject_inbound: codex inbound is poll-and-inject "
            "via tmux send-keys / codex exec.  The claude/channel MCP capability "
            "is NOT implemented by codex (T-0714).  Runtime wiring is outside the "
            "skeleton scope."
        )

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
