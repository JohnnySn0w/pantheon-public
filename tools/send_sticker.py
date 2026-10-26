#!/usr/bin/env python3
"""Send a persona sticker via the Telegram Bot API (T-3319 Part B ceremony-reduction).

The reply tool doesn't take a sticker parameter, so today firing a sticker means
resolving the right file_id from assets/stickers/manifest.json and calling
`sendSticker` via curl / HTTPS. This helper wraps that in one command so the
ceremony is `python3 tools/send_sticker.py orchestrator approved <chat_id>` instead of
a manifest lookup + token dance + curl invocation.

Contract:
  send_sticker.py <persona> <reaction> <chat_id> [--token-file PATH] [--manifest PATH]

<persona>       one of the manifest's top-level keys (orchestrator/specialist/recovery/codex/
                archivist/consultant — same slugs the manifest uses)
<reaction>      the sticker's `reaction` field (idle/shipped/approved/
                investigating/thinking/wedged, or persona-specific extras)
<chat_id>       Telegram chat id — pass the value from the inbound <channel>
                block, or the operator's 424242424 for the operator DM
--token-file    the persona's bot .env; default ~/.claude/channels/telegram/.env
                (works for any persona invoking the helper from their own home)
--manifest      path to assets/stickers/manifest.json (default: repo-relative)

Exits 0 on Telegram OK, 1 on any error (manifest missing / persona-reaction
absent / token missing / API returned ok:false). Prints the Telegram
response so a caller can see message_id / errors.

Cross-bot caveat (README §working-invocation): the API formally rate-limits
file_ids per-bot, but the current fleet's c_swarm_bot ↔ pantheon_codex_bot pair
sends off the committed manifest without re-minting — verified 2026-08-28.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "assets" / "stickers" / "manifest.json"
DEFAULT_TOKEN_FILE = Path.home() / ".claude" / "channels" / "telegram" / ".env"
# Overridable so tests can point at a local stub instead of Telegram (T-1024).
API_BASE_ENV = "TELEGRAM_API_BASE"
DEFAULT_API_BASE = "https://api.telegram.org"


def _load_token(token_file: Path) -> str:
    """Read TELEGRAM_BOT_TOKEN from the .env file. Raises with a clear
    message on any failure — the helper exits 1 on catch."""
    try:
        raw = token_file.read_text()
    except OSError as exc:
        raise RuntimeError(f"could not read token file {token_file}: {exc}") from exc
    for line in raw.splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"TELEGRAM_BOT_TOKEN= line not found in {token_file}")


def _resolve_file_id(manifest: dict, persona: str, reaction: str) -> str:
    """Look up manifest[persona].stickers[?].file_id where reaction matches.
    Raises with a listing of available reactions on miss so the operator can
    fix the invocation without opening the manifest."""
    if persona not in manifest:
        raise RuntimeError(f"persona '{persona}' not in manifest; available: {sorted(manifest.keys())}")
    stickers = manifest[persona].get("stickers") or []
    for st in stickers:
        if st.get("reaction") == reaction and st.get("file_id"):
            return st["file_id"]
    available = sorted({s.get("reaction") for s in stickers if s.get("reaction") and s.get("file_id")})
    raise RuntimeError(
        f"reaction '{reaction}' not found for persona '{persona}' with a populated "
        f"file_id; available: {available}"
    )


def send_sticker(persona: str, reaction: str, chat_id: str,
                 *, token_file: Path = DEFAULT_TOKEN_FILE,
                 manifest_path: Path = DEFAULT_MANIFEST,
                 reply_to: int | None = None) -> dict:
    """Resolve manifest → curl sendSticker. Returns the parsed API response.
    Never mutates the manifest. Never logs the token."""
    manifest = json.loads(manifest_path.read_text())
    file_id = _resolve_file_id(manifest, persona, reaction)
    token = _load_token(token_file)
    base = os.environ.get(API_BASE_ENV, DEFAULT_API_BASE).rstrip("/")
    api_url = f"{base}/bot{token}/sendSticker"
    # Token lives in the URL only; not embedded in the args list beyond that,
    # and never printed by this helper on any code path.
    cmd = ["curl", "-sS", api_url, "-d", f"chat_id={chat_id}", "-d", f"sticker={file_id}"]
    if reply_to is not None:
        # Thread under an earlier message (T-1024 MCP tool's reply_to).
        cmd += ["-d", "reply_parameters=" + json.dumps({"message_id": int(reply_to)})]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # str(TimeoutExpired) includes the argv, and the argv carries the token in the URL:
        # report a token-free cause instead (T-1024 review).
        raise RuntimeError(f"sendSticker transport failed: {type(exc).__name__}") from None
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "_raw": r.stdout, "_err": r.stderr}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("persona", help="e.g. orchestrator, specialist, recovery, codex, archivist, consultant")
    p.add_argument("reaction", help="e.g. approved, shipped, investigating, thinking, wedged, idle")
    p.add_argument("chat_id", help="Telegram chat id (from inbound <channel> block, or 424242424 for the operator)")
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
                    help=f"path to persona's telegram .env (default {DEFAULT_TOKEN_FILE})")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                    help=f"path to sticker manifest (default {DEFAULT_MANIFEST})")
    args = p.parse_args(argv)
    try:
        resp = send_sticker(args.persona, args.reaction, args.chat_id,
                            token_file=args.token_file, manifest_path=args.manifest)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    ok = resp.get("ok") is True
    # Redact any accidental token echoes before printing. The Bot API never
    # includes the token in its own response, but a curl transport error
    # could conceivably surface stderr containing the URL.
    text = json.dumps(resp, indent=2)
    # Best-effort scrub: replace anything that looks like the token from the
    # env file if it ended up in the printed dict via _raw / _err.
    try:
        tok = _load_token(args.token_file)
        if tok and tok in text:
            text = text.replace(tok, "<TOKEN-REDACTED>")
    except Exception:
        pass
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
