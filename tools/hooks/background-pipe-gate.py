#!/usr/bin/env python3
"""PreToolUse hook — block BACKGROUND Bash commands piped through head/tail/grep (T-1907).

The T-1686 gotcha (bit twice in one session): backgrounding
`long_thing 2>&1 | tail -20` makes the task's exit code TAIL's (0, so a failing
smoke reports success) and truncates the captured output to the last N lines
(so the evidence needed to enumerate `test result:` lines or find a hang is
destroyed before anyone can read it). Promotion candidate bbabb31f0a70 landed
via the T-0649 lessons→hooks pipeline; re-scoped from the detector's pre-commit
draft to a PreToolUse gate because the harm site is ephemeral agent commands,
not committed source.

Scope is deliberately narrow for precision:
  - ONLY fires on `run_in_background: true` Bash calls. Foreground pipes are
    legit read-time filtering and pass untouched.
  - A NAMED SET of exit-code-eating evidence-destroyers as a pipe target:
    head / tail / grep (the T-1686 originals) plus wc / sed / awk / cut /
    sort / uniq / jq (T-2328 — the hazard belongs to the pipe, not to the
    three filters the anecdote happened to name: absent pipefail, `$?` is the
    LAST command's, and `| wc -l` destroys MORE evidence than `| tail`, which
    the original set blocked). Still a named set, deliberately — not a
    blanket all-pipes block: `tee`, process substitution and legitimate
    streaming pass, and the fail-open/precision posture is unchanged.
  - Quoted spans are stripped first (mcp-list-guard precedent): a quoted
    mention of `| tail` is prose, not a pipe.
  - FAIL-OPEN: any error or ambiguity → allow. A guard must never brick a
    session's tooling.

Hard block, no retry-marker escape (mcp-list-guard model, not act-vs-ask): the
fix is always available — drop the pipe, redirect raw, filter at READ time.

Wire as a PreToolUse hook with matcher "Bash" in the agent's settings.json.
Listed in context_fill_monitor.HOOK_ALLOWLIST (same-change rule,
feedback_wire_hook_update_allowlist).
"""

from __future__ import annotations

import json
import re
import sys

# Quoted spans stripped before matching — a mention is not an invocation.
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# A real pipe (not ||), optionally |&, into a named filter (bare or
# path-invoked) in command position of the next segment. Set widened by T-2328;
# the trailing lookahead is what keeps tailscale/grepdiff/awkward/wcurl-style
# lookalikes out.
_PIPE_FILTER_RE = re.compile(
    r"(?<!\|)\|&?(?!\|)"                    # | or |& but not ||
    r"\s*(?:command\s+|exec\s+)?"           # common wrappers
    r"(?:\S*/)?(?:head|tail|grep|wc|sed|awk|cut|sort|uniq|jq)(?=\s|$|[;&|`)\n])"
)


def command_pipes_background_output(command: str) -> bool:
    """True iff the command pipes through head/tail/grep at launch."""
    if not command:
        return False
    stripped = _QUOTED_RE.sub(" ", command)
    return bool(_PIPE_FILTER_RE.search(stripped))


def decide(data: dict) -> dict | None:
    """Return a deny-payload dict to BLOCK, or None to allow.

    Blocks only when ALL hold: the tool is Bash, run_in_background is truthy,
    and the command pipes through head/tail/grep.
    """
    if data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input") or {}
    if not tool_input.get("run_in_background"):
        return None
    command = tool_input.get("command") or ""
    if not command_pipes_background_output(command):
        return None
    reason = (
        "BLOCKED: background command piped through an output filter "
        "(head/tail/grep/wc/sed/awk/cut/sort/uniq/jq) at launch. This eats "
        "the real exit code (absent pipefail the pipeline reports the "
        "filter's status, so a failing run reads as success) and usually "
        "destroys the evidence you will need later — `| wc -l` keeps one "
        "integer of an entire test run. Redirect RAW instead — drop the "
        "pipe, let the task capture full output, and filter at READ time "
        "(head/tail/grep/jq the log file afterwards). "
        "(T-1686 lesson via T-1907; set widened per T-2328.)"
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
        payload = decide(data)
    except Exception:
        sys.exit(0)  # fail-open
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
