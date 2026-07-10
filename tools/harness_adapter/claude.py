"""ClaudeHarnessAdapter — concrete binding for the claude-code CLI harness.

Implements PantheonHarnessAdapter for claude-code (the primary Pantheon harness:
Orchestrator/HO, Specialist, Recovery).  All 7 generalized + 2 bridge surfaces are implemented
with real key/filename translations.

Not-abstracted surfaces (sandbox_config, profiles_config, app_server_config,
cloud_config, doctor_config) are documented with a note that policy is handled
at the org tier for claude-code (Unix asymmetry + autonomy bar + bypass-permissions
flag), not via in-harness config knobs.

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i)
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      notes.md §2 (capability map)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.harness_adapter.base import PantheonHarnessAdapter


class ClaudeHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for claude-code (Orchestrator / Specialist / Recovery instances).

    Args:
        role_domain: ORG-027 instance role-key (e.g. "orchestrator").
        claude_home: path to the claude user home; defaults to Path.home().
                     Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "claude"

    def __init__(self, role_domain: str, claude_home: Path | None = None) -> None:
        super().__init__(role_domain)
        self._claude_home = claude_home if claude_home is not None else Path.home()

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return "CLAUDE.md" — the claude-code standing-prompt filename."""
        return "CLAUDE.md"

    def mcp_server_config_path(self) -> Path:
        """Return ~/.claude/settings.json — where mcpServers is declared."""
        return self._claude_home / ".claude" / "settings.json"

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
        return ["claude", "-p", prompt]

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
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Inject inbound turn via the claude/channel EXPERIMENTAL MCP capability.

        BRIDGE SURFACE — mechanism: server pushes via the ``claude/channel`` MCP
        endpoint into the active session.  This capability is claude-code-specific
        and does NOT generalize to other harnesses (codex does not implement it;
        live-verified T-0714).

        At skeleton tier this is a documented no-op stub.  Runtime wiring is
        handled by the channel bridge (pantheon-bridge, now archived) and the
        ``mcp__plugin_telegram_telegram__reply`` plugin for inbound delivery.
        """
        # Skeleton stub — no-op.  Runtime bridge wiring is out of skeleton scope.
        return None

    def approval_defaults(self) -> dict[str, Any]:
        """Return claude-code unattended-mode defaults.

        claude-code operates under --dangerously-skip-permissions at org-policy tier
        (Unix asymmetry + ratification gates + autonomy bar).

        Shape:
            {"trust": "bypass-permissions",
             "flag": "--dangerously-skip-permissions"}
        """
        return {
            "trust": "bypass-permissions",
            "flag": "--dangerously-skip-permissions",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (documented per-harness attributes)
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """claude-code has no native sandbox_mode knob.

        Policy lives at the org tier (Unix asymmetry + file-ownership write-scope +
        ratification + autonomy bar + --dangerously-skip-permissions flag), mirroring
        the approved ORG-031 §ii posture.  No in-harness sandbox config to expose.
        """
        return {
            "harness": "claude",
            "mechanism": "org-tier",
            "description": (
                "Policy lives at org tier: Unix asymmetry + file-ownership write-scope "
                "+ ratification gates + autonomy bar. No in-harness sandbox-mode knob."
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
        """claude-code has no app-server / remote-control analog.

        Returns {"supported": False, "reason": "no claude-code analog"} so consumers
        can distinguish "no analog" from "configured-but-empty".
        """
        return {
            "harness": "claude",
            "supported": False,
            "reason": "no claude-code analog",
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
        """claude-code has no ``doctor`` command.

        Pantheon monitors claude health via JSONL session log parsing
        (context_fill_monitor.py, ho_resumer.py, etc.) rather than a
        structured doctor subcommand.
        """
        return {
            "harness": "claude",
            "mechanism": "JSONL session log parsing",
            "tools": ["context_fill_monitor.py", "ho_resumer.py"],
            "description": (
                "No 'doctor' subcommand. Health monitored via JSONL session log "
                "parsing at tools/context_fill_monitor.py and tools/ho_resumer.py."
            ),
        }
