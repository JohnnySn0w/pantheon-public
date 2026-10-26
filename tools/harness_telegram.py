"""tools/harness_telegram.py — /harness command surface for Telegram (T-2967 BT4).

Per notes/2026-08-18-t2953-phase1-spec.md §Telegram bot wiring: "Existing
telegram-bot command surface adds `/harness ...` commands mirroring the CLI,
with the same argument shape. Gated to the operator allowlist ... "

Structural finding (T-2967 plan, confirmed by grep across tools/ and
channels/): there is no live, in-repo slash-command dispatcher this module
plugs into. `channels/telegram/` does not exist inside pantheon-config; the
real allowlist lives at `/home/claude/.claude/channels/telegram/access.json`
(a different git root — plugin runtime state, not application code), and
Orchestrator's own Telegram interaction is the live Claude session itself
interpreting `<channel source="telegram">` tags and calling the `reply` MCP
tool directly, not a separate editable bot process. `codex_telegram_bridge.py`
COMMAND_SHEET_SCHEMA is an operator-run migration runbook checklist, not a
command router either.

So this module is the actual "command surface": pure, testable parsing +
auth-gating + reason-prompt logic that ANY future caller (Orchestrator's own live
interpretation loop, or a future harness-bound receiver) invokes. Wiring a
live caller into that loop is out of this module's scope — it is either an
operator-instruction / CLAUDE.md-adjacent change (hand-curated, not workers'
to write) or Phase-3-adjacent HO-owned work once a persona is actually on the
harness controller. See T-2967 final report "deviations" for the explicit
flag.

Reuses tools.harness.build_parser()/validate_args() so the telegram argument
shape can never drift from the CLI's. That includes the ORG-106 L1 (T-5524)
seat verbs `pause`, `sleep` and `wake` (ORG-106 L3b adds sleep): all are mutating, so a missing --reason yields a
ReasonPrompt, and a bad --for / --owner / --ticket yields a ParseError.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
from pathlib import Path
from typing import Any

from tools import harness
from tools import harness_state as hs

# The real plugin-runtime allowlist file (outside this repo — see module
# docstring). Callers may override via the `access_file` parameter; tests
# never touch this path.
DEFAULT_ACCESS_FILE = Path("/home/claude/.claude/channels/telegram/access.json")

# Recognizes "/harness" and Telegram's group-chat "/harness@botname" form.
_COMMAND_PREFIX_RE = re.compile(r"^/harness(?:@\w+)?(?:\s+|$)")


@dataclasses.dataclass(frozen=True)
class AuthRefusal:
    message: str


@dataclasses.dataclass(frozen=True)
class ReasonPrompt:
    message: str
    command: str


@dataclasses.dataclass(frozen=True)
class ParseError:
    message: str


@dataclasses.dataclass(frozen=True)
class ParsedInvocation:
    argv: list[str]
    args: Any  # argparse.Namespace


def load_allow_from(access_file: Path | None = None) -> frozenset[int]:
    """Read the operator allowlist. Missing/malformed file -> empty set (fail-closed)."""
    path = access_file if access_file is not None else DEFAULT_ACCESS_FILE
    if not path.is_file():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    raw = data.get("allowFrom", [])
    if not isinstance(raw, list):
        return frozenset()
    result: set[int] = set()
    for item in raw:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return frozenset(result)


def _strip_command_prefix(text: str) -> str | None:
    match = _COMMAND_PREFIX_RE.match(text.strip())
    if not match:
        return None
    return text.strip()[match.end():]


def parse_harness_message(
    text: str,
    sender_id: int,
    *,
    allow_from: frozenset[int] | set[int],
) -> AuthRefusal | ReasonPrompt | ParseError | ParsedInvocation | None:
    """Parse a raw Telegram message into a harness CLI invocation, or a refusal/prompt.

    Returns None if `text` isn't a /harness command at all (caller should
    ignore it — this isn't ours to handle). Otherwise:

      - AuthRefusal   — sender_id not in allow_from. Checked BEFORE parsing,
                         so a stranger never gets command-shape feedback.
      - ReasonPrompt   — recognized, allowed, but a mutating verb is missing
                         --reason. Caller should ask the sender to resend
                         with a reason rather than silently failing.
      - ParseError     — recognized, allowed, but otherwise malformed
                         (unknown verb, bad persona/runtime, bad quoting).
      - ParsedInvocation — ready to execute via execute_parsed_invocation()
                         (or `tools.harness.main(result.argv, ...)` directly).
    """
    remainder = _strip_command_prefix(text)
    if remainder is None:
        return None

    if sender_id not in allow_from:
        return AuthRefusal(
            message="not authorized: harness overrides are restricted to the allowlisted operator(s)."
        )

    try:
        argv = shlex.split(remainder)
    except ValueError as exc:
        return ParseError(message=f"could not parse command: {exc}")

    if not argv:
        return ParseError(
            message="usage: /harness <persona> <status|restart|clear-cooldown|hold|clear-hold|pause|sleep|wake|quarantine> ..."
        )

    # T-5069: shares the CLI parser, so telegram restart inherits the handoff-first step; if this is ever wired to a live bot, give restart a shorter handoff timeout (the handler would block up to 300s).
    parser = harness.build_parser()
    try:
        args = parser.parse_args(argv)
        harness.validate_args(args)
    except harness.MissingReasonError as exc:
        return ReasonPrompt(
            message=f"'{exc.command}' requires a reason — resend with --reason \"<text>\".",
            command=exc.command,
        )
    except (harness.HarnessCliError, hs.HarnessStateError) as exc:
        return ParseError(message=f"harness: {exc}")

    return ParsedInvocation(argv=list(argv), args=args)


def execute_parsed_invocation(
    parsed: ParsedInvocation,
    *,
    root: Path | None = None,
    ledger_dir: Path | None = None,
) -> int:
    """Run a ParsedInvocation through the same code path as the CLI.

    Thin delegation to tools.harness.main so a future live caller doesn't
    have to re-derive argv handling; identical to invoking the CLI by hand
    with `result.argv`.
    """
    return harness.main(parsed.argv, root=root, ledger_dir=ledger_dir)
