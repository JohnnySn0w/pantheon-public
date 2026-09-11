"""Abstract base for the Pantheon harness adapter (ORG-031 §i).

The adapter surface is partitioned into three classes per ORG-031:

  1. **Generalized (7 surfaces)** — one abstract method each; concrete bindings
     perform mechanical per-harness key/filename translation.  These are the
     adapter's *body*: standing_prompt, mcp_servers, hooks, subagents, session,
     exec, auth.

  2. **Bridge surfaces (2 surfaces)** — genuinely harness-specific; each concrete
     binding provides a real implementation, not a thin key-name map.  These are
     the adapter's *real engineering*: inbound turn-injection and
     approval/hook-trust defaults.

  3. **NOT abstracted (5 surfaces)** — sandbox/approval, profiles, app-server,
     cloud, doctor.  First-party harness surfaces with load-bearing semantic
     differences; forcing a common call would hide what an operator needs to see.
     Represented here as @property on the base class that raise NotImplementedError
     with an explicit explanation rather than @abstractmethod.  Concrete subclasses
     override the properties that apply to them.  Per ORG-031:
     "partition-and-document beats false unification" (P1).

The adapter keys on the **role/domain axis** (ORG-027), not on persona identity.
Instantiate with ``role_domain="orchestrator"`` (or the appropriate
ORG-027 instance role-key for the target agent).

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i)
    adr/ORG-027-orchestrator-tier-with-domains-reclass.md
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      recovery-bringup-gap-harvest-2026-06-09.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

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
    InboundRequest,
    PostStartAssessment,
    PostStartPaneSample,
    ProviderCompletionAssessment,
    ProviderCompletionProbe,
    SessionHandle,
    TmuxTarget,
)
from tools.harness_adapter.quota import QuotaObserver


class PantheonHarnessAdapter(ABC):
    """Abstract adapter encoding the ORG-031 §i surface partition.

    Subclass for each supported harness (claude-code, codex, …).  Each subclass
    must set HARNESS_NAME as a class attribute and implement all @abstractmethod
    surfaces.

    Args:
        role_domain: ORG-027 instance role-key for the target agent
                     (e.g. "orchestrator", "specialist", "recovery").
                     NOT a persona name — the adapter is persona-agnostic.
    """

    #: Harness identifier string; set on each concrete subclass.
    HARNESS_NAME: str = ""

    def __init__(
        self,
        role_domain: str,
        *,
        launch: HarnessLaunchConfig | None = None,
    ) -> None:
        """Initialise the adapter with the target agent's role/domain axis key."""
        self.role_domain = role_domain
        self.launch = launch

    @property
    def executable(self) -> Path:
        """Return the executable this adapter will use as argv[0].

        Runtime-profile callers receive an immutable absolute path. Legacy
        callers without a launch config keep the historical bare command name.
        """
        if self.launch is not None:
            return self.launch.executable
        return Path(self.HARNESS_NAME)

    # -------------------------------------------------------------------------
    # Typed lifecycle contract (T-2372).
    #
    # This coexists with the original ORG-031 inventory below.  The inventory
    # describes configuration surfaces; these methods carry runtime evidence.
    # -------------------------------------------------------------------------

    @abstractmethod
    def capabilities(self) -> HarnessCapabilities:
        """Return verified native capabilities without claiming policy parity."""
        ...

    @abstractmethod
    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        """Build a shell-free headless invocation with prompt text on stdin."""
        ...

    @abstractmethod
    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        """Build a shell-free resume invocation for an explicit session."""
        ...

    @abstractmethod
    def parse_event(self, line: str) -> HarnessEvent | None:
        """Decode one native structured-output line."""
        ...

    @abstractmethod
    def finalize(
        self,
        events: Iterable[HarnessEvent],
        *,
        returncode: int,
        stderr: str,
    ) -> ExecutionResult:
        """Derive terminal outcome from events plus the process result."""
        ...

    @abstractmethod
    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        """Durably deliver an inbound request using this binding's bridge."""
        ...

    def session_handle(self, session_id: str, cwd: Path) -> SessionHandle:
        """Construct the explicit handle used by the generic runner."""
        return SessionHandle(
            session_id=session_id,
            name=None,
            cwd=cwd,
            resumable=True,
        )

    @abstractmethod
    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        """Map a process-spawn failure into a native-independent error event."""
        ...

    @abstractmethod
    def validate_launch_config(self) -> tuple[DiagnosticObservation, ...]:
        """Validate profile fields owned by the concrete harness binding."""
        ...

    @abstractmethod
    def describe_surfaces(self, cwd: Path) -> HarnessSurfaceDescriptors:
        """Return truthful descriptors for native config/auth/memory surfaces."""
        ...

    @abstractmethod
    def auth_descriptor(self) -> AuthDescriptor:
        """Return descriptor-only auth information without claiming a live probe."""
        ...

    @abstractmethod
    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        """Build the persistent interactive launch for the operator tmux plane."""
        ...

    @abstractmethod
    def quota_observer(self, state_dir: Path) -> QuotaObserver:
        """Return the provider-native quota evidence reader for this profile."""
        ...

    def assess_post_start_readiness(
        self,
        *,
        harness: str,
        profile: str,
        baseline: PostStartPaneSample,
        samples: Iterable[PostStartPaneSample],
    ) -> PostStartAssessment:
        """Assess provider UI after post-start hooks without serializing text."""
        sample_tuple = tuple(samples)
        return PostStartAssessment(
            schema="pantheon-harness-post-start-assessment/v1",
            harness=harness,
            profile=profile,
            status="unknown",
            cause=None,
            pattern_id=None,
            baseline_sha256=baseline.sha256,
            capture_count=len(sample_tuple),
            changed_capture_indices=tuple(
                sample.capture_index
                for sample in sample_tuple
                if sample.sha256 != baseline.sha256
            ),
        )

    def prepare_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        persona: str,
        account_home: Path,
        state_dir: Path,
        pane_baseline: PostStartPaneSample,
        submitted_at: datetime,
        nonce_marker: str,
        nonce_sha256: str,
    ) -> ProviderCompletionProbe | None:
        """Return a transient provider-completion probe, if this harness supports one."""

        del harness, profile, persona, account_home, state_dir, pane_baseline, submitted_at, nonce_marker, nonce_sha256
        return None

    def assess_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        probe: ProviderCompletionProbe | None,
        samples: Iterable[PostStartPaneSample],
    ) -> ProviderCompletionAssessment:
        """Assess interactive provider completion without serializing raw text."""

        sample_tuple = tuple(samples)
        return ProviderCompletionAssessment(
            schema="pantheon-harness-provider-completion/v1",
            harness=harness,
            profile=profile,
            status="not-applicable",
            cause=None,
            pattern_id=None,
            nonce_sha256=probe.nonce_sha256 if probe is not None else None,
            prompt_sha256=probe.prompt_sha256 if probe is not None else None,
            statusline_baseline_sha256=None,
            statusline_observed_sha256=None,
            capture_count=len(sample_tuple),
            changed_capture_indices=(),
        )

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces (7).
    # One abstract method each; mechanical per-harness key/filename translation.
    # -------------------------------------------------------------------------

    @abstractmethod
    def standing_prompt_filename(self) -> str:
        """Return the harness-native standing-prompt filename.

        claude-code: "CLAUDE.md"
        codex:       "AGENTS.md"

        The file is loaded automatically from the working directory (or the
        harness-specific fallback list) when the harness process starts.
        """
        ...

    @abstractmethod
    def mcp_server_config_path(self) -> Path:
        """Return the path to the harness-native MCP server configuration file.

        claude-code: ~/.claude.json (user/local) or project .mcp.json
        codex:       ~/.codex/config.toml ([mcp_servers.<id>] TOML table)

        Note (codex): transport is streamable-HTTP only — legacy SSE requires a
        stdio shim (T-0714 FINDING 1).  Cross-user permissions apply: the config
        file must be accessible from the harness user account (T-0714 FINDING 2).
        """
        ...

    @abstractmethod
    def hooks_config_path(self) -> Path:
        """Return the path to the harness-native hooks configuration file.

        claude-code: ~/.claude/settings.json  (hooks key inside settings)
        codex:       ~/.codex/hooks.json      (standalone file; also inline [[hooks.*]])

        Both harnesses provide command hooks, but their current event catalogs,
        matcher rules, and continuation semantics are described by
        ``capabilities()`` rather than assumed equivalent here.
        """
        ...

    @abstractmethod
    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        """Return a subagent spawn descriptor for the given role.

        Args:
            role: ORG-027 instance role-key (e.g. "specialist", "recovery").
            config_overrides: per-subagent config layer; merged on top of the
                              role's base config.

        Returns:
            A harness-specific subagent spawn descriptor dict.  The shape differs
            per harness:
              claude-code: Agent-tool invocation parameters (isolation, prompt, …)
              codex:       spawn_agent call parameters (features.multi_agent path)

        At skeleton tier this returns a documented stub dict; runtime execution
        is not wired in the skeleton.
        """
        ...

    @abstractmethod
    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Return a session-resume descriptor for the given session_id.

        The "resume by id" *concept* generalizes across harnesses; the *mechanism*
        and *storage format* are harness-specific and differ in load-bearing ways.

        claude-code: ``claude --resume <session_id>`` flag; sessions stored as JSONL
                     in ~/.claude/projects/; full recovery-monitor integration.
        codex:       ``codex exec resume <id>`` for headless continuation or
                     ``codex resume <id>`` for the interactive TUI. Storage and
                     recovery monitors remain harness-specific.

        Returns:
            A harness-specific session resume descriptor dict.
            Claude binding: {"harness": "claude", "cli_args": ["--resume", session_id]}
            Codex binding: {"harness": "codex",
                            "cli_args": ["exec", "resume", "--json", session_id]}
        """
        ...

    @abstractmethod
    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """Return the CLI args list for a non-interactive (headless) prompt execution.

        claude-code: ["claude", "-p", prompt]
        codex:       ["codex", "exec", prompt]  (alias: codex e <prompt>)

        Args:
            prompt: the prompt string to pass to the harness.
            **kwargs: harness-specific optional flags (e.g. model, timeout).

        Returns:
            A list of CLI argument strings suitable for subprocess.run().
            The skeleton returns the args list; callers are responsible for
            subprocess execution.

        Note (codex): ``required=true`` per-MCP causes ``codex exec`` to exit if a
        required MCP fails to start, unlike the interactive TUI's graceful-degrade.
        """
        ...

    @abstractmethod
    def auth_status(self) -> dict[str, Any]:
        """Return a dict describing the current auth state for this harness.

        "Delegated to harness CLI" generalizes across both harnesses; the mechanism
        and credential location differ:

        claude-code: ANTHROPIC_API_KEY env var / ~/.claude/settings.json
        codex:       OAuth session at ~/.codex/auth.json (0600) OR --with-api-key.
                     OAuth unlocks built-in multimodal tools (e.g. image_gen) that
                     the API-key path may not expose (Recovery harvest §A7; undocumented
                     in any SOP/ADR as of 2026-06-09).

        Returns:
            {"harness": str, "key_source": str, ...harness-specific fields}
        """
        ...

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces (2).
    # Genuine harness-specific implementations; not thin key-name maps.
    # These are the adapter's real engineering per ORG-031.
    # -------------------------------------------------------------------------

    @abstractmethod
    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Inject an inbound message as a new turn via the harness's native turn API.

        THIS IS A BRIDGE SURFACE — the mechanism is harness-specific and the
        implementations are NOT interchangeable.

        claude-code: ``claude/channel`` EXPERIMENTAL MCP capability; server pushes
                     the turn directly into the active session.
        codex:       durable maildir delivery followed by a Codex-specific wake
                     or headless-resume step. A durable queue receipt is not an
                     acknowledgment that a turn ran.

        Args:
            message: the inbound message string to inject.
            **kwargs: harness-specific delivery options.
        """
        ...

    @abstractmethod
    def approval_defaults(self) -> dict[str, Any]:
        """Return the harness-native unattended-mode approval and hook-trust defaults.

        THIS IS A BRIDGE SURFACE — the two harnesses use orthogonal approval
        mechanisms; the returned dict shapes are harness-specific.

        claude-code: {"permission_mode": "default",
                      "flag": "--permission-mode default",
                      "tool_approval": "scoped-permissions.allow"}
        codex:       {"approval_policy": "never",
                      "sandbox_mode": "workspace-write",
                      "sandbox_workspace_write": {"network_access": True},
                      "mcp_tool_approval": "approve", ...}
                     (ORG-031 §ii live-verified; ``trust_level`` alone does NOT silence
                     prompts. NOTE the precise truth (live find 2026-06-09, T-1033):
                     ``approval_policy="never"`` silences *shell/command* prompts but
                     NOT *MCP-tool* prompts — a freshly-registered MCP tool prompts on
                     first use even under ``never``. The separate per-tool gate
                     ``[mcp_servers.<srv>.tools.<tool>] approval_mode = "approve"``
                     must be pre-set per tool. Claude has no exact analog. This is
                     why approval lives in the configure-per-harness bucket, not a
                     unified surface.)

        Returns:
            A harness-specific config dict representing the settings that silence
            all unattended-mode approval prompts.
        """
        ...

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (5).
    # First-party harness surfaces with load-bearing semantic differences.
    # Represented as @property that raise NotImplementedError with an explanation
    # (NOT @abstractmethod — concrete subclasses override only what applies to them).
    # Per ORG-031: "partition-and-document beats false unification" (P1).
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """Per-harness sandbox and approval policy configuration.

        NOT ABSTRACTED — forcing a common call would hide load-bearing semantics:
          claude-code: native sandboxing plus permission modes/settings.
          codex:       approval_policy × sandbox_mode (2 orthogonal axes) +
                       sandbox_workspace_write.{writable_roots, network_access}.
                       ORG-031 §ii ratified: never + workspace-write + network_access=true.

        Concrete subclasses override this to return their per-harness config dict.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.sandbox_config is not abstracted "
            "per ORG-031 §Decision (i) — configure per-harness directly."
        )

    @property
    def profiles_config(self) -> dict[str, Any]:
        """Per-harness profile layering configuration.

        NOT ABSTRACTED:
          claude-code: per-instance settings.json (org-tier); no ``-p`` flag.
          codex:       ``--profile <name>`` layers $CODEX_HOME/<name>.config.toml
                       + [permissions.<name>]; no claude analog.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.profiles_config is not abstracted "
            "per ORG-031 §Decision (i) — configure per-harness directly."
        )

    @property
    def app_server_config(self) -> dict[str, Any]:
        """Per-harness app-server / remote-control / exec-server configuration.

        NOT ABSTRACTED:
          claude-code: remote-control surface.
          codex:       app-server / exec-server / remote-control surfaces.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.app_server_config is not abstracted "
            "per ORG-031 §Decision (i) — configure per-harness directly."
        )

    @property
    def cloud_config(self) -> dict[str, Any]:
        """Per-harness cloud / cloud-task ingest configuration.

        NOT ABSTRACTED:
          claude-code: no analog.
          codex:       codex cloud-task ingest surface.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.cloud_config is not abstracted "
            "per ORG-031 §Decision (i) — configure per-harness directly."
        )

    @property
    def doctor_config(self) -> dict[str, Any]:
        """Per-harness doctor / health-check configuration.

        NOT ABSTRACTED:
          claude-code: ``/doctor`` and safe-mode/config diagnostics.
          codex:       ``codex doctor --json`` structured checks.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.doctor_config is not abstracted "
            "per ORG-031 §Decision (i) — configure per-harness directly."
        )
