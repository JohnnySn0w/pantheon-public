"""tools/sticker_mcp.py — MCP stdio server: send a Pantheon persona sticker (T-1024).

ARC-INTENT: give every agent a native tool call for sending its own state sticker
(idle/thinking/investigating/approved/shipped/wedged) to Telegram, so stickers
become ambient status language (T-1002 / T-1037) and stop being a shell ceremony.
The operator asked to close the sticker arc out (via specialist, TG 4120-4122).

WHY A SEPARATE SERVER, NOT THE TELEGRAM PLUGIN (T-1024's original shape):
  - The vendored plugin cache is overwritten on plugin updates (0.0.6 -> 0.0.7
    already did that), and its files are content-pinned (T-4651), so an in-place
    edit would raise a drift alert and then be lost.
  - sendSticker is a plain outbound Bot API call. It never polls getUpdates, so
    it cannot cause the single-poller 409 that makes the live bridge
    sensitive to change.
  - It reuses tools/send_sticker.py (manifest -> file_id -> sendSticker), so
    there is one sending path, already tested.

TOOL: send_sticker(chat_id, reaction, reply_to?)
  - persona = the CALLER'S persona from $USER (fail closed, same map as the
    mailbox server). An agent sends its own stickers only.
  - token   = the caller's own bot token from ~/.claude/channels/telegram/.env
    (override: STICKER_MCP_TOKEN_FILE). It is never returned or logged.
  - reaction must exist for that persona in assets/stickers/manifest.json; the
    error lists the available ones.

PROTOCOL: MCP 2024-11-05 over stdio, server name "sticker", using the `mcp` SDK
already installed for mailbox_mcp.py. No new dependencies.

REGISTRATION (per agent user, same shape as the mailbox server; takes effect
on the agent's next session start):
    "sticker": {"type": "stdio", "command": "python3",
                "args": ["<home>/pantheon-config/tools/sticker_mcp.py"]}
in that user's ~/.claude.json mcpServers (project scope), or
`claude mcp add sticker -- python3 <home>/pantheon-config/tools/sticker_mcp.py`
run from a session that does NOT have the Telegram poller live.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp import types  # noqa: E402

try:
    from tools import send_sticker as _ss  # noqa: E402
    from tools.agent_role_map import account_persona_map  # noqa: E402
except ImportError:
    import send_sticker as _ss  # type: ignore[no-redef]  # noqa: E402
    from agent_role_map import account_persona_map  # type: ignore[no-redef]  # noqa: E402

def _caller_persona() -> str:
    """The invoking agent's persona from $USER, failing CLOSED (as mailbox_mcp).

    The map is read per call, not snapshotted at import: a long-lived server then follows
    account changes, and there is no module-level snapshot to register with the
    test_account_persona_map site guard."""
    user = os.environ.get("USER", "")
    user_to_persona = account_persona_map()
    persona = user_to_persona.get(user)
    if persona is None:
        raise ValueError(
            f"Unrecognized $USER {user!r}: cannot resolve a Pantheon persona; "
            f"launch as a known agent user ({', '.join(sorted(user_to_persona))})."
        )
    return persona


def _manifest_path() -> Path:
    return Path(os.environ.get("STICKER_MCP_MANIFEST", str(_ss.DEFAULT_MANIFEST)))


def _token_file() -> Path:
    return Path(os.environ.get("STICKER_MCP_TOKEN_FILE", str(_ss.DEFAULT_TOKEN_FILE)))


_SEND_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Telegram chat id: the inbound <channel> chat_id, or 424242424 for the operator."},
        "reaction": {"type": "string", "description": "Which of your stickers: idle, thinking, investigating, approved, shipped or wedged (some personas have extras)."},
        "reply_to": {"type": "integer", "minimum": 1, "description": "Optional message_id to thread the sticker under."},
    },
    "required": ["chat_id", "reaction"],
    "additionalProperties": False,
}

app = Server("sticker", instructions=(
    "send_sticker posts one of YOUR persona's state stickers to a Telegram chat. "
    "Use it to mark a state change (approved, shipped, investigating, wedged...) alongside, "
    "not instead of, a text reply."))


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(
        name="send_sticker",
        description=(
            "Send one of your own Pantheon persona stickers to a Telegram chat. Your persona and bot "
            "come from $USER. Reactions: idle, thinking, investigating, approved, shipped, wedged "
            "(an unknown reaction errors with your available list). Returns the sent message_id."),
        inputSchema=_SEND_SCHEMA,
    )]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "send_sticker":
        raise ValueError(f"Unknown tool: {name!r}")
    persona = _caller_persona()
    chat_id = str(arguments.get("chat_id", "")).strip()
    reaction = str(arguments.get("reaction", "")).strip()
    if not chat_id or not reaction:
        raise ValueError("chat_id and reaction are required")
    reply_to = arguments.get("reply_to")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to < 1):
        raise ValueError("reply_to must be a positive integer message_id")
    try:
        resp = await asyncio.to_thread(
            _ss.send_sticker, persona, reaction, chat_id,
            token_file=_token_file(), manifest_path=_manifest_path(), reply_to=reply_to)
    except RuntimeError as exc:
        # Manifest/token problems. _resolve_file_id's message already lists the
        # available reactions; never include the token (send_sticker never puts it in errors).
        raise ValueError(str(exc)) from None
    if resp.get("ok") is not True:
        raise ValueError(f"Telegram refused the sticker: {resp.get('description') or 'unknown error'}")
    msg_id = (resp.get("result") or {}).get("message_id")
    return [types.TextContent(type="text", text=json.dumps({"sent": True, "persona": persona, "reaction": reaction, "message_id": msg_id}))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
