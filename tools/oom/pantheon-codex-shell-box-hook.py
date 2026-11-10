#!/usr/bin/env python3
"""pantheon-codex-shell-box-hook.py -- codex PreToolUse hook: run every Bash command in the shell-box (T-5480).

Intent: box every codex-runtime shell child in a per-command memory-bounded systemd scope with
oom_score_adj 0, as claude seats already are (CLAUDE_CODE_SHELL_PREFIX, T-5454), so a codex
fallback seat cannot exhaust host memory (ORG-104, T-5222). Codex has no config key that wraps
the shell, but a PreToolUse hook may rewrite the command (updatedInput, permissionDecision allow).

Rewrite: <shell-box> <original>, both shlex-quoted; codex runs it as `<shell> -lc "<NEW>"`, and the
box's single-argument path runs `/bin/bash -c "$1"`, the same contract claude uses. The box is the
sibling pantheon-shell-box.sh of this file, resolved absolute.

FAIL-OPEN, and why exit 2 and stderr matter. Verified against codex rust-v0.147.0
(hooks/src/events/pre_tool_use.rs parse_completed): exit 0 + empty stdout = no rewrite; a hook
error, timeout, other non-zero exit, or exit-0 invalid JSON is a non-blocking failure; but exit 2
WITH stderr text BLOCKS the command. So every path that is not a rewrite ends in exit 0 with empty
stdout AND empty stderr, and nothing here ever exits 2. A missing/non-executable box logs one line
to ~/.local/state/pantheon/shell-box-fallback.log (the wrapper's own log) and runs unboxed.
PANTHEON_SHELL_BOX=off is honoured by the wrapper itself, not here (one implementation of the knob).

Stdlib only; run as `/usr/bin/python3 -I <path>`.
"""
import json
import os
import shlex
import sys
import time


def _log_unboxed(why):
    try:
        state = os.path.join(os.path.expanduser("~"), ".local", "state", "pantheon")
        os.makedirs(state, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(os.path.join(state, "shell-box-fallback.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} pantheon-codex-shell-box-hook: {why}; running this command UNBOXED (no memory cap)\n")
    except Exception:
        pass


def rewrite(raw):
    """The hook's stdout for one event (a JSON string), or None for 'leave the command alone'."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    box = os.path.join(os.path.dirname(os.path.realpath(os.path.abspath(__file__))), "pantheon-shell-box.sh")
    quoted = shlex.quote(box)
    if command.startswith(quoted + " "):
        return None  # already boxed
    if not (os.path.isfile(box) and os.access(box, os.X_OK)):
        _log_unboxed(f"shell-box {box} missing or not executable")
        return None
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": quoted + " " + shlex.quote(command)},
    }})


def main():
    try:
        out = rewrite(sys.stdin.read())
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - fail-open: never block, never exit 2, never write stderr
        pass
    return 0


if __name__ == "__main__":
    code = main()
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)
