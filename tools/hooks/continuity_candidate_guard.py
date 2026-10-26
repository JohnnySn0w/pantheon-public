#!/usr/bin/env python3
"""PreToolUse hook — structural candidate-mode isolation for ORG-057 harness
switches (T-3177).

Same mechanism shape as ``worker-guard.py`` (T-1345): a default-ALLOW
PreToolUse hook (fail-closed on its own failures since T-4392 phase 2d, see
below) that blocks a specific dangerous-action catalog structurally rather than by prompt discipline. DIFFERENT threat model and
DIFFERENT trigger: worker-guard keys off `agent_type` (a Claude-Code-subagent
signal); this guard keys off the ``PANTHEON_CONTINUITY_CANDIDATE`` environment
variable, which `tools/continuity_switch_coordinator.py` sets ONLY for a
candidate persona embodiment under ORG-057 verification (a full harness
session, not a subagent).

Threat model: DRIFT, not an adversary — the candidate is a good-faith
embodiment of the persona under verification; the guard exists to make the
"structurally disabled" claim (RFC-001 `rfc/RFC-001-persona-continuity-lifecycle.md` §Harness switch:
"Prompt discipline is not sufficient. The adapter / controller launch surface
omits or denies these capabilities until commit.") true regardless of what the
model attempts, not to defeat deliberate evasion.

Blocks exactly the SOP's structural-disable list for candidate mode:
mailbox consumption/acknowledgement, Telegram receive/reply, ticket lifecycle
mutation, ledger/canonical-record writes, and direct credential reads that
would let the candidate act outside a named verification probe. Everything
else (reading files, running tests, ordinary Bash) passes through unchanged.

FAIL-CLOSED (deny) on marker-active + pattern-match. Since T-4392 phase 2d the
HOOK is fail-closed too (was fail-open): with the marker set, main() runs through
tools/hooks/_failclosed.py, so an exception, an unparseable/ambiguous payload,
the 15s deadline from process start, or a Bash command over MAX_COMMAND_CHARS
DENIES (exit 2, one stderr line naming the cause). Without the marker the hook
still exits 0 at once. Break-glass is the helper's.

Wire via a candidate-scoped settings fragment
(`continuity_switch_coordinator._write_candidate_guard_settings`) with matcher
"Bash|Edit|Write|MultiEdit|NotebookEdit", exactly as worker-guard.py is wired.
"""

from __future__ import annotations

import os
import re
import sys

CANDIDATE_MARKER_ENV = "PANTHEON_CONTINUITY_CANDIDATE"

# --- Bash patterns blocked while the candidate marker is active ---
#
# Four rules have the shape "token A, then token B later on the SAME line". They were single
# regexes `A.*B` (`.` stops at a newline), which re.search runs in O(n^2): every A
# occurrence scans to its line's end and backtracks (32 KB of `git ` took 5.7s). Since
# T-4392 phase 2d each is a pair (A, B) run through _a_then_b below, which is linear and
# gives the SAME verdict (tools/hooks/test_continuity_candidate_guard.py keeps the old
# regexes verbatim as oracles and runs a differential against them).

_MAILBOX_SCRIPT = re.compile(r"\bmailbox(_consume|_wake|_ack)?\.py\b", re.IGNORECASE)
_MAILBOX_VERB = re.compile(r"\b(consume|ack|deliver|send)\b", re.IGNORECASE)
_TELEGRAM_BOT_API = re.compile(r"api\.telegram\.org/bot", re.IGNORECASE)
_TICKET_SCRIPT = re.compile(r"\bticket\.py\b", re.IGNORECASE)
_TICKET_WRITE_VERB = re.compile(
    r"\b(file|claim|resolve|annotate|block|move|note|close|unblock)\b", re.IGNORECASE
)
_LEDGER_WEEK = re.compile(r"(^|[\s/])ledger/\d{4}-W\d{2}\b", re.IGNORECASE)
_JSONL = re.compile(r"\.jsonl", re.IGNORECASE)
_CREDENTIAL_READ = re.compile(
    r"\.ssh/id_|\.claude/channels/telegram/\.env|\.git-credentials|GIT_SSH_COMMAND", re.IGNORECASE
)
_GIT_WORD = re.compile(r"\bgit\b", re.IGNORECASE)
_PUSH_WORD = re.compile(r"\bpush\b", re.IGNORECASE)


def _a_then_b(first: re.Pattern, second: re.Pattern, text: str) -> bool:
    """True iff `re.search(first + ".*" + second, text)` would match, in O(n).

    `.*` never crosses a newline, so the old regex asks: is there an A match and a B match
    that starts at or after A's end, with no newline in between? A matches cannot overlap
    (each starts at a word boundary or a separator that cannot occur inside another), so
    finditer sees them all, in order of start AND end. The first A on a line ends earliest,
    so its window [end, line end) contains every later A's window on that line: only the
    first A per line is checked, and each line's window is searched at most once.
    B.search(text, pos, endpos) evaluates `\\b` against the REAL previous character at pos
    (sre keeps the string's true beginning), and at endpos, which is always a newline or
    the true end, `\\b` sees a non-word character either way, exactly as the old `.*` did."""
    checked_to = -1
    for match in first.finditer(text):
        start = match.end()
        if start <= checked_to:
            continue  # same line as an A already checked, with a narrower window
        newline = text.find("\n", start)
        line_end = len(text) if newline < 0 else newline
        if second.search(text, start, line_end):
            return True
        checked_to = line_end
    return False

# --- Edit/Write path patterns blocked while the candidate marker is active ---

_PROTECTED_WRITE_PATH = re.compile(
    r"(^|/)(ledger|tickets|adr|mailbox|knowledge/watch)(/|$)", re.IGNORECASE
)


def _reason(what: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED (continuity-candidate-guard, T-3177): a candidate persona "
                f"embodiment under ORG-057 verification attempted {what}. Candidate mode "
                "structurally disables mailbox/Telegram/ticket/ledger/external action "
                "until the coordinator commits the switch. This is not a policy refusal "
                "you can route around — the capability is not wired for this embodiment "
                "yet."
            ),
        }
    }


def decide(data: dict, *, candidate_active: bool | None = None) -> dict | None:
    """Return a deny-payload to BLOCK, or None to ALLOW.

    `candidate_active` is injectable for tests; production `main()` derives it
    from the ``PANTHEON_CONTINUITY_CANDIDATE`` environment variable of the hook
    subprocess (set only by the coordinator's candidate-scoped settings
    fragment, never by the model).
    """

    if not candidate_active:
        return None  # not a candidate embodiment -- this guard does not apply

    tool_name = data.get("tool_name")

    if tool_name == "Bash":
        command = (data.get("tool_input") or {}).get("command") or ""
        if not command:
            return None
        if _a_then_b(_MAILBOX_SCRIPT, _MAILBOX_VERB, command):
            return _reason("mailbox consumption/acknowledgement")
        if _TELEGRAM_BOT_API.search(command):
            return _reason("direct Telegram Bot API contact")
        if _a_then_b(_TICKET_SCRIPT, _TICKET_WRITE_VERB, command):
            return _reason("a ticket lifecycle mutation")
        if _a_then_b(_LEDGER_WEEK, _JSONL, command):
            return _reason("a ledger write")
        if _a_then_b(_GIT_WORD, _PUSH_WORD, command):
            return _reason("a git push (external action)")
        if _CREDENTIAL_READ.search(command):
            return _reason("a credential file read not required for the verification probe")
        return None

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and _PROTECTED_WRITE_PATH.search(path):
            return _reason("a canonical-record write (ledger / tickets / adr / mailbox)")
        return None

    return None


# The fail-closed runner checks this cap on every Bash call, before decide() sees it. The
# coordinator wires this hook into the candidate account's canonical settings.json
# (continuity_switch_coordinator._write_candidate_guard_settings), so it gates the candidate
# session's MAIN THREAD and every subagent, not an agent_type. So the cap is the main-thread
# 262,144 of mcp-list-guard and background-pipe-gate (room for heredocs), not the helper's
# 65,536 default. The cap applies to the Bash command only: Edit/Write/MultiEdit/NotebookEdit
# content is never scanned, and decide() reads only their path.
MAX_COMMAND_CHARS = 262_144


def _decide_from_env(data: dict) -> dict | None:
    """decide() with the marker read from THIS process's environment (the runner passes one
    argument). The wired command sets the marker itself, so the candidate cannot unset it."""
    return decide(data, candidate_active=os.environ.get(CANDIDATE_MARKER_ENV) == "1")


def main() -> None:
    """Run the guard FAIL-CLOSED (T-4392 phase 2d) through tools/hooks/_failclosed.py.

    decide() is unchanged. What changed is the HOOK-level failure mode: an exception, an
    ambiguous payload, the 15s deadline from process start, or a Bash command over
    MAX_COMMAND_CHARS now DENIES (exit 2, stderr names the cause) instead of exiting 0. The
    helper is imported from THIS file's real directory, first on sys.path (a decoy on
    PYTHONPATH cannot win); if that import fails, the guard denies too, since an ImportError
    would exit 1, which fails open."""
    # No marker = not wired (the wired command always sets it=1): the documented contract is "does nothing".
    if os.environ.get(CANDIDATE_MARKER_ENV) != "1":
        sys.exit(0)
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
            "BLOCKED (continuity-candidate-guard, fail-closed, T-4392): cannot import _failclosed "
            f"({exc!r}). {hint}\n"
        )
        sys.exit(2)
    _failclosed.run("continuity-candidate-guard", _decide_from_env, max_command_chars=MAX_COMMAND_CHARS)


if __name__ == "__main__":
    main()
