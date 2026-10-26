#!/usr/bin/env python3
"""telegram_inbound.py (T-4205) — when did the operator last message this account's agent?

Claude Code writes every telegram-plugin inbound into the session jsonl. Three
structural shapes carry one (verified on a live orchestrator jsonl, 2026-09-29):

  * {"type": "user", "message": {"content": "<channel source=\"plugin:telegram:telegram\" ... ts=\"...\">..."}}
      — the agent was idle, so the message became the turn itself;
  * {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "<channel ...>"}}
      — delivered mid-turn;
  * {"type": "queue-operation", "operation": "enqueue", "content": "<channel ...>"}
      — queued behind a busy turn.

Only a string that STARTS with the channel tag counts. Prose that merely quotes the
tag (system-prompt text, assistant turns, mcp_instructions_delta attachments) is
ignored, so a quoted example can never read as the operator being present. The tag's
`ts` attribute is the Telegram message time.

This replaces the decommissioned pantheon-bridge's `last_inbound_at` (HEALTH_URL,
127.0.0.1:8765, retired 2026-06-04). It reads files directly, as the calling account:
no sudo, so an agent's own consumer can read its own sessions.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

_SOURCE_ID = "plugin:telegram:telegram"
CHANNEL_TAG = f'<channel source="{_SOURCE_ID}"'
_OPEN_TAG_TS_RE = re.compile(r'^<channel source="plugin:telegram:telegram"[^>]*?\sts="([^"]+)"')
# Bounded tail read: a within-window inbound sits near the END of a file that was
# written within the window, so the last few MB always cover it.
TAIL_BYTES = 8 * 1024 * 1024
# A ts this far in the future is clock skew or garbage; never let it gate forever.
MAX_FUTURE_SKEW_SECONDS = 300


def _iso_to_unix(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _inbound_texts(obj: dict) -> list[str]:
    """The strings in one jsonl entry that can carry a real inbound (see module doc)."""
    kind = obj.get("type")
    if kind == "user":
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [b.get("text") for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return []
    if kind == "attachment":
        att = obj.get("attachment") or {}
        if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            return [att["prompt"]]
        return []
    if kind == "queue-operation" and obj.get("operation") == "enqueue":
        content = obj.get("content")
        return [content] if isinstance(content, str) else []
    return []


_OPEN_TAG_RE = re.compile(r'^<channel source="plugin:telegram:telegram"([^>]*)>', re.S)
_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)="([^"]*)"')
_CLOSE_TAG = "</channel>"


def parse_inbound_text(text: str) -> dict | None:
    """Parse one channel-tagged string into {"attrs": {...}, "body": str}, else None.

    Same starts-with-tag rule as the ts reader (quoted prose never parses). The body
    is the text between the open tag and the closing tag, stripped.
    """
    stripped = text.lstrip()
    m = _OPEN_TAG_RE.match(stripped)
    if not m:
        return None
    attrs = dict(_ATTR_RE.findall(m.group(1)))
    body = stripped[m.end():]
    end = body.rfind(_CLOSE_TAG)
    if end != -1:
        body = body[:end]
    return {"attrs": attrs, "body": body.strip()}


def inbound_messages_from_obj(obj: dict) -> list[dict]:
    """Every parsed telegram inbound carried by one decoded jsonl entry (public)."""
    out = []
    for text in _inbound_texts(obj):
        parsed = parse_inbound_text(text)
        if parsed is not None:
            out.append(parsed)
    return out


def inbound_ts_from_line(line: str) -> int | None:
    """Unix ts of the telegram inbound one jsonl line records, else None."""
    # Cheap prefilter before json.loads. On the RAW line the tag's quotes are
    # JSON-escaped (source=\"plugin:...\"), so match only the escape-free source id.
    if _SOURCE_ID not in line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    best: int | None = None
    for text in _inbound_texts(obj):
        m = _OPEN_TAG_TS_RE.match(text.lstrip())
        ts = _iso_to_unix(m.group(1)) if m else None
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def latest_inbound_ts_from_lines(lines: Iterable[str], now: float | None = None) -> int | None:
    """Max inbound ts across jsonl lines, ignoring implausibly-future timestamps."""
    ceiling = (time.time() if now is None else now) + MAX_FUTURE_SKEW_SECONDS
    best: int | None = None
    for line in lines:
        ts = inbound_ts_from_line(line)
        if ts is not None and ts <= ceiling and (best is None or ts > best):
            best = ts
    return best


def _tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 else lines  # drop the partial first line of a tail


def recent_inbound_ts(projects_root: Path, within_seconds: int, now: float | None = None) -> int | None:
    """Latest telegram-inbound ts found in session jsonls written within `within_seconds`.

    Session files are `<projects_root>/<project>/<session>.jsonl`. Subagent
    transcripts live deeper and are not scanned. A file not written within the
    window cannot hold an inbound from within the window, so only recent files are
    read. Returns None when there is none. Raises OSError only if `projects_root`
    itself is unreadable; unreadable single files are skipped.
    """
    now = time.time() if now is None else now
    cutoff = now - within_seconds
    best: int | None = None
    for project in projects_root.iterdir():
        try:
            if not project.is_dir():
                continue
            candidates = [p for p in project.glob("*.jsonl") if p.stat().st_mtime >= cutoff]
        except OSError:
            continue
        for path in candidates:
            try:
                ts = latest_inbound_ts_from_lines(_tail_lines(path), now=now)
            except OSError:
                continue
            if ts is not None and (best is None or ts > best):
                best = ts
    return best
