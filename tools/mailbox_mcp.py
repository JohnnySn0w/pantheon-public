"""tools/mailbox_mcp.py — MCP stdio server wrapping tools/mailbox.py (T-1112).

Exposes two tools so Pantheon agents use inter-agent messaging as a native
Claude Code tool call instead of the `python -c "..."` one-liner.

  mailbox_send    — deliver a message to a peer's inbox
  mailbox_consume — drain your own inbox and return messages as a JSON list

ARC-INTENT: send/consume UX improvement only. Wake is NOT changed — the
.path/.timer/SessionStart adapter (ORG-026 "no transport wakes an LLM" finding)
remains exactly as-is.

SCOPE: import mailbox.py; zero logic duplication; no new dependencies beyond the
`mcp` SDK already installed at /home/claude/.local/lib/python3.12/site-packages/.

PROTOCOL: MCP spec 2024-11-05 (stable), stdio transport. Server name: "mailbox".

SENDER IDENTITY: derived from $USER at request time via _USER_TO_PERSONA.
Identity is ADVISORY under our same-user trust model — $USER is a process-launch
environment value, NOT a kernel-enforced credential, so it is not a spoof barrier
against a determined same-host actor. What it DOES give us is fail-closed
behavior: an unrecognized $USER raises before any filesystem op (see
_caller_persona), so a misconfigured launch cannot send-as or drain an arbitrary
inbox.

MAILBOX HUB: /home/the-ancestor/mailbox/<persona> (ORG-026 Path A), matching
mailbox_wake.py's MAILBOX_HUB. Override with env MAILBOX_MCP_HUB for testing.

REGISTRATION (one-time, run as the target agent's unix user; production-enable
is an HO supervised step after merge — do NOT run without HO sign-off):

    claude mcp add mailbox -- python3 /home/claude/pantheon-config/tools/mailbox_mcp.py

For other agents, replace /home/claude with the agent's home directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap: support both `python3 tools/mailbox_mcp.py` (cwd=repo-root)
# and direct invocation from within tools/.
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import mailbox as _mailbox
    from _lease import Lease  # noqa: F401 — imported for lease-held detection via consume
except ImportError:
    # Fallback: parent dir (running from tools/).
    _dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_dir))
    import mailbox as _mailbox

# Stdlib-shadow guard: `mailbox` is ALSO a Python stdlib module. The bootstrap
# above normally wins (tools/ on sys.path[0]), but in a package-context import
# where the sys.path insert never fires, `import mailbox` could silently bind the
# stdlib module — which lacks send/consume/is_safe_inbox and would fail later with
# an opaque AttributeError. If we did not get OUR module, force-load it by path.
_LOCAL_MAILBOX = Path(__file__).resolve().parent / "mailbox.py"
if Path(getattr(_mailbox, "__file__", "")).resolve() != _LOCAL_MAILBOX.resolve():
    import importlib.util
    _spec = importlib.util.spec_from_file_location("mailbox", _LOCAL_MAILBOX)
    _mailbox = importlib.util.module_from_spec(_spec)
    sys.modules["mailbox"] = _mailbox
    _spec.loader.exec_module(_mailbox)

# ---------------------------------------------------------------------------
# MCP SDK imports.
# ---------------------------------------------------------------------------
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Mailbox hub path — override via MAILBOX_MCP_HUB for testing.
_DEFAULT_HUB = Path("/home/the-ancestor/mailbox")
MAILBOX_HUB: Path = Path(os.environ.get("MAILBOX_MCP_HUB", str(_DEFAULT_HUB)))

# Known recipients — validated so send cannot create arbitrary directories.
_KNOWN_RECIPIENTS: frozenset[str] = frozenset({"orchestrator", "specialist", "recovery"})

# Unix user → Pantheon persona. Mirrors mailbox_wake.py's AGENT_PANE keys.
_USER_TO_PERSONA: dict[str, str] = {
    "claude": "orchestrator",
    "claude-research": "specialist",
    "claude-fallback": "recovery",
    "codex": "codex",
}


def _caller_persona() -> str:
    """Resolve the invoking agent's persona from $USER, failing CLOSED.

    Identity is advisory ($USER is env, not a kernel credential — see module
    docstring). We fail closed: an unrecognized $USER raises rather than silently
    resolving to a hub/<raw-user> path, so a misconfigured launch cannot send-as
    or drain an arbitrary inbox. Raised ValueError crosses the MCP boundary as a
    tool error BEFORE any filesystem op.
    """
    user = os.environ.get("USER", "")
    persona = _USER_TO_PERSONA.get(user)
    if persona is None:
        raise ValueError(
            f"Unrecognized $USER {user!r}: cannot resolve a Pantheon persona. "
            "Mailbox identity is advisory (env-derived, same-user trust) and fails "
            f"closed — launch as a known agent user ({', '.join(sorted(_USER_TO_PERSONA))})."
        )
    return persona


# ---------------------------------------------------------------------------
# Tool input schemas.
# ---------------------------------------------------------------------------

_SEND_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "recipient": {
            "type": "string",
            "enum": sorted(_KNOWN_RECIPIENTS),
            "description": "Target Pantheon persona: orchestrator, specialist, or recovery.",
        },
        "subject": {
            "type": "string",
            "description": "One-line subject for the message.",
        },
        "body": {
            "type": "string",
            "description": "Message body (free text; will be stored as a JSON string payload).",
        },
    },
    "required": ["recipient", "subject", "body"],
    "additionalProperties": False,
}

_CONSUME_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Server definition.
# ---------------------------------------------------------------------------

app = Server("mailbox")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="mailbox_send",
            description=(
                "Deliver a message to another Pantheon agent's mailbox. "
                "Returns the message id (filename in the recipient's new/ dir). "
                "Sender identity is derived from $USER (advisory — same-user trust "
                "model, not a spoof barrier) and fails closed: an unrecognized user "
                "is rejected. Recipient must be one of: orchestrator, specialist, recovery."
            ),
            inputSchema=_SEND_SCHEMA,
        ),
        types.Tool(
            name="mailbox_consume",
            description=(
                "Drain your own inbox (identified by $USER) and return pending messages. "
                "On success returns a JSON list [{id, sender, subject, ts, payload}, ...]. "
                "IMPORTANT — at-MOST-once from the caller's view: each message is acked "
                "(moved new/ → cur/) BEFORE this call returns. If the tool result is lost "
                "(session reset mid-call, transport drop), those messages are already in "
                "cur/ and a later consume will NOT re-deliver them — they are consumed-but-"
                "undelivered, not recoverable by retry. Do not assume a retry re-reads them. "
                "Two non-list shapes: {\"skipped\": true} if another consumer holds the inbox "
                "lease; {\"partial\": true, \"messages\": [...], \"error\"|\"errors\": ...} if the "
                "drain failed partway (the messages already acked are returned so they are "
                "not silently lost)."
            ),
            inputSchema=_CONSUME_SCHEMA,
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "mailbox_send":
        return await _handle_send(arguments)
    if name == "mailbox_consume":
        return await _handle_consume(arguments)
    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Tool handlers.
# ---------------------------------------------------------------------------

async def _handle_send(args: dict) -> list[types.TextContent]:
    recipient = args.get("recipient", "")
    subject = args.get("subject", "")
    body = args.get("body", "")

    if recipient not in _KNOWN_RECIPIENTS:
        raise ValueError(
            f"Unknown recipient {recipient!r}. Must be one of: "
            + ", ".join(sorted(_KNOWN_RECIPIENTS))
        )

    inbox_path = MAILBOX_HUB / recipient
    if not _mailbox.is_safe_inbox(inbox_path):
        raise ValueError(f"Computed inbox path {inbox_path!r} failed safety check.")

    sender = _caller_persona()
    msg_id = _mailbox.send(
        recipient_inbox=inbox_path,
        payload=body,
        sender=sender,
        subject=subject,
    )
    return [types.TextContent(type="text", text=msg_id)]


async def _handle_consume(args: dict) -> list[types.TextContent]:  # noqa: ARG001
    persona = _caller_persona()
    inbox_path = MAILBOX_HUB / persona
    if not _mailbox.is_safe_inbox(inbox_path):
        raise ValueError(f"Computed inbox path {inbox_path!r} failed safety check.")

    collected: list[dict] = []

    def _collect(msg: dict) -> None:
        collected.append(msg)

    try:
        result = _mailbox.consume(inbox_path, _collect)
    except Exception as exc:  # noqa: BLE001 — must surface partial progress, not lose it
        # An exception escaping consume() (fs failure, corrupt message read, lease
        # internals) after some messages were already acked into cur/ would, as a
        # bare MCP error, strand `collected` — those messages are consumed but the
        # caller never sees them. Return what was acked plus an error marker.
        return [types.TextContent(type="text", text=json.dumps({
            "partial": True,
            "error": f"{type(exc).__name__}: {exc}",
            "messages": collected,
        }))]

    if result.get("skipped"):
        return [types.TextContent(
            type="text",
            text=json.dumps({"skipped": True, "reason": "inbox lease held by another consumer"}),
        )]

    # consume() records per-message handler failures in errors[] (those stay in
    # new/ for a later drain). _collect never raises, so this is normally empty;
    # if non-empty, surface it alongside the acked messages rather than dropping it.
    errors = result.get("errors") or []
    if errors:
        return [types.TextContent(type="text", text=json.dumps({
            "partial": True,
            "errors": errors,
            "messages": collected,
        }))]

    return [types.TextContent(type="text", text=json.dumps(collected))]


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        init_opts = app.create_initialization_options()
        await app.run(read_stream, write_stream, init_opts)


if __name__ == "__main__":
    asyncio.run(main())
