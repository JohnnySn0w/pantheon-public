"""Headless `claude -p` subprocess wrapper (T-2809 Phase 2).

Structural, not caller-remembered: `build_claude_argv` UNCONDITIONALLY emits
`--setting-sources ""` and `--strict-mcp-config` on every argv it builds, and
NEVER emits `--bare`. There is no parameter that removes the isolation flags
and no code path that constructs an argv without going through this
function. This closes the failure mode in
`bugs/headless-claude-p-same-account-steals-telegram-poller.md`: any headless
`claude -p` on this seat that loads user-scope settings spawns a second
Telegram poller and steals the interactive session's binding.

Persona injection has two mechanisms, selected by which of `agents_json`/
`agent_name` vs `system_prompt` the caller supplies (mutually exclusive):

- `--agents <json> --agent <name>` (primary, per D11 fidelity decision):
  loads the candidate persona text as a real custom-agent system prompt —
  the closest available approximation to "reviewer.md as Claude actually
  loads a persona" for an isolated single-shot rollout.
- `--system-prompt <text>` (fallback): used only if the primary mechanism is
  found not to survive the isolation flags (see the live-smoke test in
  `tools/test_gepa_claude_executor.py::TestLiveAgentsPersonaMechanism`).
  Both paths keep the mandatory isolation flags — the fallback is a fidelity
  downgrade, never an isolation downgrade.

`run_claude()` is a single, stateless, shell-free subprocess call — no
`--resume`, no session reuse. Every rollout is independent by design (the
candidate persona changes across GEPA iterations, so there is nothing to
resume). Debuggability without `--resume` comes from `--keep-sessions` (opt
out of `--no-session-persistence`) plus the adapter layer's own
trajectory-capture contract (full argv + full prompt + full raw response),
per HO's addition (B) in the T-2809 Phase-2 plan approval.

No `gepa` import (see package docstring).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Sequence

# Prefer the resolved absolute path so subprocesses invoked from a systemd-run
# --user context (which inherits a minimal PATH) don't hit FileNotFoundError.
# Falls back to bare "claude" when not resolvable (e.g. in pure unit tests
# that mock subprocess).
CLAUDE_BINARY = (
    os.environ.get("PANTHEON_CLAUDE_BIN")
    or shutil.which("claude", path=f"{os.path.expanduser('~/.npm-global/bin')}:{os.environ.get('PATH', '')}")
    or "claude"
)


class ClaudeExecutorError(Exception):
    """Raised on subprocess failure: nonzero exit, timeout, or spawn error."""


def build_agents_json(persona_text: str, *, agent_name: str = "reviewer", description: str = "GEPA rollout candidate persona") -> str:
    """Build the `--agents` JSON payload embedding `persona_text` verbatim as the agent's prompt."""
    import json

    return json.dumps({agent_name: {"description": description, "prompt": persona_text}})


def build_claude_argv(
    *,
    model: str,
    prompt: str | None = None,
    agents_json: str | None = None,
    agent_name: str | None = None,
    system_prompt: str | None = None,
    keep_sessions: bool = False,
) -> list[str]:
    """Build the argv for one headless `claude -p` invocation.

    Isolation flags (`--setting-sources ""`, `--strict-mcp-config`) and the
    `--bare` prohibition are unconditional. `agents_json`/`agent_name` must
    both be given or both omitted, and are mutually exclusive with
    `system_prompt` — mixing persona-injection mechanisms is a caller error,
    not a silent precedence rule.
    """
    if (agents_json is None) != (agent_name is None):
        raise ValueError("agents_json and agent_name must be given together or both omitted")
    if agents_json is not None and system_prompt is not None:
        raise ValueError("agents_json/agent_name and system_prompt are mutually exclusive persona mechanisms")

    argv: list[str] = [
        CLAUDE_BINARY,
        "-p",
        "--model",
        model,
        "--setting-sources",
        "",
        "--strict-mcp-config",
    ]
    if not keep_sessions:
        argv.append("--no-session-persistence")
    if agents_json is not None:
        argv.extend(["--agents", agents_json, "--agent", agent_name])
    if system_prompt is not None:
        argv.extend(["--system-prompt", system_prompt])
    if prompt is not None:
        argv.append(prompt)
    return argv


def run_argv(argv: Sequence[str], *, timeout: float = 300.0, cwd: str | None = None) -> str:
    """Run a pre-built argv (from `build_claude_argv`) and return stdout text.

    Split out from `run_claude` so callers that need to CAPTURE the exact
    argv used (the adapter layer's trajectory-debuggability contract — see
    `adapter.py`) can build it once via `build_claude_argv`, keep a copy, and
    still go through this single subprocess-invocation code path. Raises
    ClaudeExecutorError on nonzero exit, timeout, or spawn failure — never
    returns a sentinel for those. The adapter layer (not this module) is
    responsible for catching this per-rollout and folding it into a
    tolerated 0.0-score example, per the GEPAAdapter contract's "never raise
    for individual example failures" rule.

    `cwd`: working directory for the subprocess. Load-bearing when the invoked
    persona (e.g. reviewer) has a main-path cwd guard that refuses to run from
    a canonical project root — the caller must pre-stage a /tmp worktree and
    pass its path here. Absent, inherits the parent's cwd.
    """
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeExecutorError(f"claude -p invocation failed: {type(exc).__name__}: {exc}") from exc

    if result.returncode != 0:
        raise ClaudeExecutorError(
            f"claude -p exited {result.returncode}: stderr={result.stderr.strip()!r}"
        )
    return result.stdout


def run_claude(
    *,
    prompt: str,
    model: str,
    agents_json: str | None = None,
    agent_name: str | None = None,
    system_prompt: str | None = None,
    keep_sessions: bool = False,
    timeout: float = 300.0,
    cwd: str | None = None,
) -> str:
    """Run one headless, isolated `claude -p` call and return its stdout text.

    Convenience wrapper: builds the argv then delegates to `run_argv`. Use
    `build_claude_argv` + `run_argv` directly when the caller needs to keep
    the exact argv (e.g. for trajectory capture).
    """
    argv = build_claude_argv(
        model=model,
        prompt=prompt,
        agents_json=agents_json,
        agent_name=agent_name,
        system_prompt=system_prompt,
        keep_sessions=keep_sessions,
    )
    return run_argv(argv, timeout=timeout, cwd=cwd)


def argv_repr(argv: Sequence[str]) -> str:
    """Human-readable argv rendering for trajectory capture / debugging."""
    return " ".join(argv)
