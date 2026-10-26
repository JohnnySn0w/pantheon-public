"""PreToolUse hook: keep agents away from the sudo password file (T-5331).

On 2026-10-20 a sed meant to show key names printed the bare password from root.env into
an HO transcript. The operator approved this pair (TG 12178-12181):

  * tools/pantheon-sudo pipes the file into `sudo -S` itself, so no agent command needs to
    name it;
  * this hook refuses
      - any Bash command that mentions root.env, and
      - any `sudo -S` / `sudo --stdin` in the agent's command text,
    plus any file tool (Read, Grep, Glob, Edit, Write, MultiEdit, NotebookEdit) whose
    target path names root.env (content that only mentions it is fine).

Plain `sudo <cmd>` is left alone: it needs the password, which only the wrapper supplies,
so it fails at the prompt. Scripts that read the file internally (persona_cutover.sh and
the like) keep working, because the hook sees only the agent's own command text.

Threat model (ORG-105 sec.4): an accidental leak into a transcript by a well-meaning agent.
This is a text check, so a deliberate workaround can beat it; that is out of scope.

Applies to the main loop and subagents alike. FAIL-CLOSED via _failclosed.py, like the
other security-gating hooks. Wire as PreToolUse, matcher
"Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit", on seats that hold a root.env
(today: the claude seat). Listed in context_fill_monitor.HOOK_ALLOWLIST.
"""
from __future__ import annotations

import os
import re
import sys

MAX_COMMAND_CHARS = 65_536

_PW_FILE_RE = re.compile(r"root\.env", re.IGNORECASE)
# `sudo` followed, within the same simple command, by a short-option cluster holding S
# (-S, -kS, -Su) or by --stdin.
_SUDO_STDIN_RE = re.compile(
    r"(?:^|[\s;&|(`])sudo\b[^;&|\n`)]*?\s(?:-[A-Za-z]*S[A-Za-z]*|--stdin)(?=\s|$|=)"
)
_FILE_TOOLS = {"Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit"}

_WRAPPER = "/home/claude/pantheon-config/tools/pantheon-sudo"


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(data: dict) -> dict | None:
    """Return a deny payload to BLOCK, or None to allow."""
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if tool == "Bash":
        command = tool_input.get("command") or ""
        if _PW_FILE_RE.search(command):
            return _deny(
                "BLOCKED (root-env-guard, T-5331): this command names the sudo password file. "
                f"Run sudo through {_WRAPPER} <command> instead; it reads the file itself. "
                "To search text for the file's name, spell it root[.]env."
            )
        if _SUDO_STDIN_RE.search(command):
            return _deny(
                "BLOCKED (root-env-guard, T-5331): `sudo -S` / `--stdin` is reserved for the "
                f"wrapper. Use {_WRAPPER} <command>."
            )
        return None
    if tool in _FILE_TOOLS:
        # Only fields that NAME a file: content that merely mentions the name (docs, this
        # hook's own source) must stay editable.
        paths = [tool_input.get(k) for k in ("file_path", "path", "notebook_path")]
        if tool == "Glob":
            paths.append(tool_input.get("pattern"))
        if any(isinstance(v, str) and _PW_FILE_RE.search(v) for v in paths):
            return _deny(
                "BLOCKED (root-env-guard, T-5331): file tools may not read, search or write "
                f"the sudo password file. Use {_WRAPPER} <command> for sudo."
            )
    return None


def main() -> None:
    """Run fail-closed through tools/hooks/_failclosed.py (same shape as background-pipe-gate)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import _failclosed
    except BaseException as exc:  # noqa: BLE001 - an import failure must DENY
        sys.stderr.write(
            "BLOCKED (root-env-guard, fail-closed, T-4392): cannot import _failclosed "
            f"({exc!r}). Run the hook from the pantheon-config clone, or unwire it via the "
            "Edit tool.\n"
        )
        sys.exit(2)
    _failclosed.run("root-env-guard", decide, max_command_chars=MAX_COMMAND_CHARS)


if __name__ == "__main__":
    main()
