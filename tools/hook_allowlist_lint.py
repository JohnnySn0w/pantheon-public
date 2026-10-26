#!/usr/bin/env python3
"""Hook-allowlist pairing lint (T-1102).

Catches the recurring T-0536 false-positive at its SOURCE: a hook wired into the
committing agent's ``~/.claude/settings.json`` whose basename is NOT in
``HOOK_ALLOWLIST`` (tools/context_fill_monitor.py). When that pairing is missed,
the runtime context_fill_monitor security checker fires a "possible injection"
alert for the legitimate hook — alert-fatigue on a security checker, which is the
real cost (the gap recurred 4× in one session: verify-startup-boot.sh + mcp-list-guard.py).

Design (T-1102 decision):
  - REUSE ``context_fill_monitor.check_hook_allowlist`` — the SAME predicate the
    runtime checker uses. Invariant: a green lint for agent X ⟹ the runtime
    T-0536 checker will not flag X. The lint and the checker can never disagree.
  - Option (a) "hard commit-time error" — but ADAPTED to the substrate: agent
    settings.json files are per-account and NOT repo-tracked, so a repo commit
    cannot see *other* agents' settings.json. We check the COMMITTING agent's OWN
    local settings.json (readable without sudo on its own account). Each agent's
    pre-commit guards that agent; the runtime checker remains the cross-agent net.
  - Option (c) preserved: deliberately does NOT auto-derive or auto-add the
    allowlist (it is the curated trust anchor) — it only FLAGS the missing entry.

Exit codes:
  0 = clean / committer is not a known agent / settings unreadable (fail-OPEN on
      our own read glitches and on any unexpected error — a lint bug must NEVER
      brick commits, mirroring the act-vs-ask gate's fail-open posture).
  1 = drift: one or more wired hooks are not allowlisted (hard error).
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.context_fill_monitor import (  # noqa: E402
    AGENT_CONFIG,
    HOOK_ALLOWLIST,
    PLUGIN_DEVIATION_PREFIX,
    PLUGIN_DIR_ALLOWLIST,
    check_hook_allowlist,
)


def local_agent(user: str | None = None) -> str | None:
    """Map a unix user to its agent name via AGENT_CONFIG ``sudo_user``; else None.

    None means the committer is not one of the standing agents — the lint then
    no-ops (we only guard an agent against its own allowlist).
    """
    user = user or getpass.getuser()
    for agent, cfg in AGENT_CONFIG.items():
        if cfg.get("sudo_user") == user:
            return agent
    return None


def lint(agent: str | None = None) -> list[str]:
    """Wired-but-not-allowlisted hook command(s) for ``agent`` (default: local).

    Returns [] when there is no local agent, the agent has no allowlist entry,
    settings are unreadable, or there is no drift. Reuses the runtime predicate.
    """
    agent = agent or local_agent()
    if agent is None or agent not in HOOK_ALLOWLIST:
        return []
    cfg = AGENT_CONFIG.get(agent)
    if not cfg:
        return []
    return check_hook_allowlist(agent, cfg)


def format_failure(agent: str, deviations: list[str]) -> str:
    allow = sorted(HOOK_ALLOWLIST.get(agent, set()))
    lines = "\n".join(f"    • {d}" for d in deviations)
    return (
        f"\nhook-allowlist pairing drift (T-1102) — commit refused for agent '{agent}'.\n\n"
        f"  These hook command(s) are wired in your ~/.claude/settings.json but their\n"
        f"  basename is NOT in HOOK_ALLOWLIST (tools/context_fill_monitor.py):\n"
        f"{lines}\n\n"
        f"  Currently allowlisted for {agent}: {allow}\n\n"
        f"  FIX: add the hook's basename to HOOK_ALLOWLIST['{agent}'] in\n"
        f"  tools/context_fill_monitor.py (curate it deliberately — do NOT auto-derive),\n"
        f"  stage that change, and re-commit. This prevents the runtime T-0536\n"
        f"  'possible injection' false-positive firing on your legitimate hook.\n"
        + (
            f"\n  {PLUGIN_DEVIATION_PREFIX.strip()} deviations (T-4577) are mods loaded via\n"
            f"  settings.json env: point the entry at your own clone's\n"
            f"  pantheon-config/tools/mods/<name> and add <name> to\n"
            f"  PLUGIN_DIR_ALLOWLIST['{agent}'] (currently "
            f"{sorted(PLUGIN_DIR_ALLOWLIST.get(agent, set()))}).\n"
            if any(d.startswith(PLUGIN_DEVIATION_PREFIX) for d in deviations) else ""
        )
        + "  (Emergency bypass: git commit --no-verify.)\n"
    )


def main() -> int:
    try:
        agent = local_agent()
        deviations = lint(agent)
    except Exception as exc:  # fail-OPEN: a lint crash must never brick commits
        print(
            f"hook_allowlist_lint: unexpected error {exc!r}; skipping (fail-open)",
            file=sys.stderr,
        )
        return 0
    if not deviations:
        return 0
    print(format_failure(agent, deviations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
