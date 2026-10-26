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
    streaming pass, and the precision posture is unchanged.
  - Quoted spans are stripped first (mcp-list-guard precedent): a quoted
    mention of `| tail` is prose, not a pipe.
  - FAIL-CLOSED since T-4392 phase 2c (was fail-open): main() runs through
    tools/hooks/_failclosed.py, so an exception, an unparseable/ambiguous
    payload, the 15s deadline from process start, or a Bash command over
    MAX_COMMAND_CHARS DENIES (exit 2, one stderr line naming the cause).
    The verdict logic itself is unchanged; break-glass is the helper's.

Hard block, no retry-marker escape (mcp-list-guard model, not act-vs-ask): the
fix is always available — drop the pipe, redirect raw, filter at READ time.

Wire as a PreToolUse hook with matcher "Bash" in the agent's settings.json.
Listed in context_fill_monitor.HOOK_ALLOWLIST (same-change rule,
feedback_wire_hook_update_allowlist).
"""

from __future__ import annotations

import bisect
import os
import re
import sys

# Quoted spans stripped before matching — a mention is not an invocation.
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# What is blocked: a real pipe (not ||), optionally |&, into a named filter (bare
# or path-invoked) in command position of the next segment. Set widened by
# T-2328; the trailing lookahead is what keeps tailscale/grepdiff/awkward/wcurl-
# style lookalikes out. Until T-4392 phase 2c this was ONE regex:
#
#   (?<!\|)\|&?(?!\|)\s*(?:command\s+|exec\s+)?(?:\S*/)?(?:NAMES)(?=\s|$|[;&|`)\n])
#
# which is QUADRATIC: at every pipe the greedy `\S*` runs to the end of the
# whitespace-free stretch and backtracks through each `/`, and every further `|`
# inside that stretch restarts the same scan (`'|/'*n`: 2.7s CPU at 16 KB).
# _pipes_into_filter() below computes EXACTLY the same yes/no in linear time;
# tools/test_background_pipe_gate.py keeps that regex verbatim as the oracle.
_NAME_AT = r"(?:head|tail|grep|wc|sed|awk|cut|sort|uniq|jq)(?=\s|$|[;&|`)\n])"
_NAME_AT_RE = re.compile(_NAME_AT)
_SLASH_NAME_RE = re.compile("/" + _NAME_AT)      # a `/` the path form can end at
_WS_RUN_RE = re.compile(r"\s*")
_WS_CHAR_RE = re.compile(r"\s")
_WRAPPER_RE = re.compile(r"command\s+|exec\s+")  # common wrappers


def _pipes_into_filter(text: str) -> bool:
    """The old regex's `.search(text)` truth value, in linear time.

    For each `|` not preceded by `|`, the regex may continue after `|` (unless `||`
    follows) or after `|&` (unless `|` follows). From there `\\s*` and the optional
    wrapper's `\\s+` can only usefully consume their WHOLE whitespace run (stopping
    short leaves a whitespace char, which neither a name nor `\\S*/` can start
    with), giving at most two positions q. At q the regex matches iff a name (plus
    boundary) starts at q, or some `/`+name starts at or after q with no
    whitespace in between (that is `\\S*/`). The `/`+name positions are found once;
    the next-whitespace lookup is cached, and the q values only move forward, so
    every character is scanned a bounded number of times."""
    if "|" not in text:
        return False
    n = len(text)
    slash_names = None
    ws_cache = [-1, -1]  # [queried from, first whitespace at or after it (or n)]

    def next_ws(q):
        if not ws_cache[0] <= q <= ws_cache[1]:
            m = _WS_CHAR_RE.search(text, q)
            ws_cache[0], ws_cache[1] = q, (m.start() if m else n)
        return ws_cache[1]

    def filter_at(q):
        nonlocal slash_names
        if q >= n:
            return False
        if _NAME_AT_RE.match(text, q):
            return True
        if slash_names is None:
            slash_names = [m.start() for m in _SLASH_NAME_RE.finditer(text)]
        j = bisect.bisect_left(slash_names, q)
        return j < len(slash_names) and slash_names[j] < next_ws(q)

    p = text.find("|")
    while p != -1:
        if p == 0 or text[p - 1] != "|":
            after = text[p + 1] if p + 1 < n else ""
            starts = []
            if after != "|":
                starts.append(p + 1)
            if after == "&" and (text[p + 2] if p + 2 < n else "") != "|":
                starts.append(p + 2)
            for a in starts:
                q = _WS_RUN_RE.match(text, a).end()
                if filter_at(q):
                    return True
                w = _WRAPPER_RE.match(text, q)
                if w and filter_at(w.end()):
                    return True
        p = text.find("|", p + 1)
    return False


def command_pipes_background_output(command: str) -> bool:
    """True iff the command pipes through head/tail/grep at launch."""
    if not command:
        return False
    stripped = _QUOTED_RE.sub(" ", command)
    return _pipes_into_filter(stripped)


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


# The fail-closed runner checks this cap on EVERY Bash call, before decide() sees
# it, and this hook is not agent_type-gated: it runs on the main thread of every
# seat, foreground calls included. So the cap is mcp-list-guard's main-thread
# 262,144 (room for orchestrator heredocs), not the helper's 65,536 default. The
# linear scan's worst shape costs ~0.5s CPU here, well inside the 15s budget;
# tools/test_background_pipe_gate_failclosed.py pins it.
MAX_COMMAND_CHARS = 262_144


def main() -> None:
    """Run the gate FAIL-CLOSED (T-4392 phase 2c) through tools/hooks/_failclosed.py.

    decide() is unchanged. What changed is the HOOK-level failure mode: an exception, an
    ambiguous payload, the 15s deadline from process start, or a command over
    MAX_COMMAND_CHARS now DENIES (exit 2, stderr names the cause) instead of exiting 0. The
    helper is imported from THIS file's real directory, first on sys.path (a decoy on
    PYTHONPATH cannot win); if that import fails, the gate denies too, since an ImportError
    would exit 1, which fails open."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import _failclosed
    except BaseException as exc:  # noqa: BLE001 - an import failure must DENY
        missing = isinstance(exc, FileNotFoundError) or (
            isinstance(exc, ModuleNotFoundError) and exc.name == "_failclosed")
        if missing:
            hint = ("The hook's directory lacks _failclosed.py: run the hook from the "
                    "pantheon-config clone, or unwire it via the Edit tool.")
        else:
            hint = (f"the hook's _failclosed.py failed to import ({type(exc).__name__}): "
                    "fix it, or unwire the hook via the Edit tool.")
        sys.stderr.write(
            "BLOCKED (background-pipe-gate, fail-closed, T-4392): cannot import _failclosed "
            f"({exc!r}). {hint}\n"
        )
        sys.exit(2)
    _failclosed.run("background-pipe-gate", decide, max_command_chars=MAX_COMMAND_CHARS)


if __name__ == "__main__":
    main()
