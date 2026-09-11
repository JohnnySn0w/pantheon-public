#!/usr/bin/env python3
"""PreToolUse hook — structural candidate-mode isolation for ORG-057 harness
switches (T-3177).

Same mechanism shape as ``worker-guard.py`` (T-1345): a fail-open,
default-ALLOW PreToolUse hook that blocks a specific dangerous-action catalog
structurally rather than by prompt discipline. DIFFERENT threat model and
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

FAIL-OPEN on parse ambiguity (can't tell what the command is -> don't guess).
FAIL-CLOSED (deny) on marker-active + pattern-match.

Wire via a candidate-scoped settings fragment
(`continuity_switch_coordinator._write_candidate_guard_settings`) with matcher
"Bash|Edit|Write|MultiEdit|NotebookEdit", exactly as worker-guard.py is wired.
"""

from __future__ import annotations

import json
import re
import sys

CANDIDATE_MARKER_ENV = "PANTHEON_CONTINUITY_CANDIDATE"

# --- Bash patterns blocked while the candidate marker is active ---

_MAILBOX_ACTION = re.compile(r"\bmailbox(_consume|_wake|_ack)?\.py\b.*\b(consume|ack|deliver|send)\b", re.IGNORECASE)
_TELEGRAM_BOT_API = re.compile(r"api\.telegram\.org/bot", re.IGNORECASE)
_TICKET_WRITE_VERB = re.compile(
    r"\bticket\.py\b.*\b(file|claim|resolve|annotate|block|move|note|close|unblock)\b", re.IGNORECASE
)
_LEDGER_WRITE = re.compile(r"(^|[\s/])ledger/\d{4}-W\d{2}\b.*\.jsonl", re.IGNORECASE)
_CREDENTIAL_READ = re.compile(
    r"\.ssh/id_|\.claude/channels/telegram/\.env|\.git-credentials|GIT_SSH_COMMAND", re.IGNORECASE
)
_GIT_PUSH = re.compile(r"\bgit\b.*\bpush\b", re.IGNORECASE)

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
        if _MAILBOX_ACTION.search(command):
            return _reason("mailbox consumption/acknowledgement")
        if _TELEGRAM_BOT_API.search(command):
            return _reason("direct Telegram Bot API contact")
        if _TICKET_WRITE_VERB.search(command):
            return _reason("a ticket lifecycle mutation")
        if _LEDGER_WRITE.search(command):
            return _reason("a ledger write")
        if _GIT_PUSH.search(command):
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


def main() -> None:
    import os

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # fail-open
    try:
        candidate_active = os.environ.get(CANDIDATE_MARKER_ENV) == "1"
        payload = decide(data, candidate_active=candidate_active)
    except Exception:
        sys.exit(0)  # fail-open
    if payload is not None:
        print(json.dumps(payload))
    sys.exit(0)


if __name__ == "__main__":
    main()
