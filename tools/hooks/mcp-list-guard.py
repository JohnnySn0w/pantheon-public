#!/usr/bin/env python3
"""PreToolUse hook — refuse `claude mcp list` when THIS account's telegram poller is live (T-0894 prong A / T-0996).

The single-poller-409 failure mode: a transient `claude` that initializes the
telegram plugin (chiefly `claude mcp list`) opens a SECOND getUpdates poll on the
same bot → 409 → the persistent inbound poller exits → the agent goes comms-dark
(the 2026-06-06/07 Orchestrator incident). The discipline is "never `claude mcp list` a
live bot; diagnose via `ps` + `curl getMe`." This hook ENFORCES that structurally.

Scope: checks the CURRENT account's own bridge (`~/.claude/channels/telegram/
bot.pid` → a live `bun ... server.ts`). `claude mcp list` in this account would
load this account's plugin and poll this account's bot, so the relevant poller is
the local one — no cross-account/sudo needed. Self-contained + FAIL-OPEN: any
error or ambiguity → allow (a guard must never brick a session's tooling).

Wire as a PreToolUse hook with matcher "Bash" in each agent's settings.json
(deferred careful per-agent deploy — see T-0996). Modeled on act-vs-ask-gate.py.

Match scope (WIDENED by T-1623; was `claude mcp list` only): ANY transient
`claude` invocation with the plugin enabled steals the telegram token (the
ratified no-second-claude rule — `--bare` included; self-inflicted twice), so
the guard now blocks `claude` in COMMAND POSITION (start of command / after
; && || | backtick or subshell-open). Quoted spans are stripped before
matching, which fixes the T-1623 false-positive class: the guard once fired on
the STRING naming the subcommand inside a quoted ticket-body argument — a
mention is not an invocation. Exempted safe shapes: --version/--help/-h (no
session, no plugin) and --print/-p WITH --safe-mode (the veneration resurrect
design — plugin-less by construction). `sudo -u claude …` does not match
(username argument, not a command). Blocking still requires a CONFIRMED-live
local poller.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Quoted spans are stripped before matching: a quoted mention of a claude
# command is prose/an argument, not an invocation (the T-1623 false positive).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# `claude` (bare or path-invoked) in COMMAND position: start-of-string or after
# a shell separator/substitution opener, optionally via `command`/`exec`/env
# assignments. Captures the rest of that command segment for the safe-flag test.
_CLAUDE_CMD_RE = re.compile(
    r"(?:^|[;&|`\n(])\s*"                   # command position
    r"(?:command\s+|exec\s+)?"              # common wrappers
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"   # env assignments
    r"(?:\S*/)?claude(?=\s|[;&|`)\n]|$)"    # bare or path-invoked; full token only
                                            # (claude-bot-supervise.sh must NOT match)
    r"([^;&|`\n]*)"                         # args until the next separator
)

# Safe invocation shapes (tested against the matched segment's args):
_SAFE_ARGS_RE = re.compile(r"--version\b|--help\b|(?:^|\s)-h\b")
_PRINT_RE = re.compile(r"--print\b|(?:^|\s)-p\b")
_SAFE_MODE_RE = re.compile(r"--safe-mode\b")

_BOT_PID = Path.home() / ".claude" / "channels" / "telegram" / "bot.pid"


def command_targets_mcp_list(command: str) -> bool:
    """True iff the bash command INVOKES a plugin-initializing `claude` (T-1623:
    any second claude with the plugin enabled steals the token — not just the
    mcp-list subcommand). Quoted mentions don't count; safe shapes exempted.
    (Name kept for wiring/test compatibility.)"""
    if not command:
        return False
    stripped = _QUOTED_RE.sub(" ", command)
    for m in _CLAUDE_CMD_RE.finditer(stripped):
        args = m.group(1) or ""
        if _SAFE_ARGS_RE.search(args):
            continue  # --version/--help: no session, no plugin
        if _PRINT_RE.search(args) and _SAFE_MODE_RE.search(args):
            continue  # headless safe-mode: plugin-less by construction
        return True
    return False


def own_poller_live() -> bool:
    """True iff this account's telegram bridge poller is live (bot.pid → live bun
    server.ts). FAIL-OPEN inverse: any uncertainty returns False (→ allow), so a
    flaky check never blocks legitimately. Only a CONFIRMED-live poller blocks."""
    try:
        if not _BOT_PID.exists():
            return False
        pid = int(_BOT_PID.read_text(encoding="utf-8").split()[0].strip())
        os.kill(pid, 0)  # raises if dead / not ours
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        return "bun" in cmdline and "server.ts" in cmdline
    except Exception:
        return False


def decide(data: dict, poller_live: bool) -> dict | None:
    """Return a deny-payload dict to BLOCK, or None to allow.

    Blocks only when ALL hold: the tool is Bash, the command invokes
    `claude mcp list`, and this account's poller is confirmed live.
    """
    if data.get("tool_name") != "Bash":
        return None
    command = (data.get("tool_input") or {}).get("command") or ""
    if not command_targets_mcp_list(command):
        return None
    if not poller_live:
        return None
    reason = (
        "BLOCKED: launching a second `claude` while this account's Telegram "
        "poller is LIVE. Any transient claude with the plugin enabled opens a "
        "second getUpdates poll on the same bot → 409 → the live inbound poller "
        "EXITS and the agent goes comms-dark (the 2026-06-06/07 bridge-death "
        "incident; the ratified no-second-claude rule). Diagnose bridges with "
        "`ps` (the `bun server.ts` child) + `curl .../getMe`. Safe shapes that "
        "pass: --version/--help, and --print WITH --safe-mode. "
        "(T-0894/T-0996/T-1623 guard.)"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # fail-open
    try:
        payload = decide(data, own_poller_live())
    except Exception:
        sys.exit(0)  # fail-open
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
