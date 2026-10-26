#!/usr/bin/env python3
"""Mint Telegram sticker set(s) from assets/stickers/manifest.json (T-1002).

Token is read from env STICKER_BOT_TOKEN (never stored/committed). Owner user_id
from env OWNER_USER_ID (default the operator 424242424 — must have /start-ed the bot).
Structure from argv[1]: 'combined' (one Pantheon set, default), 'per-agent'
(one set per agent), or 'append-combined' (add manifest entries with blank
file_id values to the existing combined set). Writes the returned file_ids +
real set_name back into the manifest. Telegram rejects duplicate set names, so
re-running 'combined' over an existing set errors loudly — use
'append-combined' for later additions, or deleteStickerSet / rename first.

First mint: the combined `pantheon_pack_by_panethon_codex_bot` (24 stickers),
2026-06-09.

**Cross-bot file_id note (Telegram Bot API contract + practical observation).**
getStickerSet file_ids are formally per-bot: a different sending bot MAY have to
re-fetch the set's file_ids under its own token (T-1024 / HO). In practice for
this fleet (verified 2026-08-28, T-3319): c_swarm_bot successfully sent Consultant's
pantheon_codex_bot-minted stickers using the committed manifest file_ids with
no re-mint. So the caution above is API-contract-accurate but over-tight for
the current bot pair — cite it correctly to anyone else, but sending off the
committed manifest just works today. If a third bot ever joins, re-verify on
that pair before assuming the same shortcut. See
assets/stickers/README.md#working-invocation for the working curl pattern.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "assets" / "stickers" / "manifest.json"
STICKERS_DIR = REPO / "assets" / "stickers"
DEFAULT_OWNER = "424242424"

EMOJI = {"idle": "\U0001F916", "shipped": "\U0001F680", "approved": "✅",
         "investigating": "\U0001F50D", "thinking": "\U0001F914", "wedged": "⚠️",
         "waypoint": "\U0001F9ED", "verified": "✅", "received": "✉️",
         "boundary": "\U0001F6E1", "waiting": "\U0001F3D5"}
REACTION_ORDER = ["idle", "shipped", "approved", "investigating", "thinking", "wedged",
                  "waypoint", "verified", "received", "boundary", "waiting"]
AGENT_ORDER = ["orchestrator", "specialist", "recovery", "codex", "archivist", "consultant"]
OPERATOR_ORDER = ["thoughtful", "knowing", "wanderer", "focus", "tangled", "serene"]


@dataclass(frozen=True)
class StickerEntry:
    owner_key: str
    reaction: str
    emoji: str
    path: Path
    manifest_ref: dict[str, Any]


def api_url(token: str, method: str, *, api_base: str = "https://api.telegram.org") -> str:
    return f"{api_base}/bot{token}/{method}"


def call(token: str, method: str, fields: Mapping[str, Any] | None = None, files: Mapping[str, Path] | None = None) -> dict[str, Any]:
    """POST via curl; returns parsed JSON dict. fields: {k:v}; files: {k:path}."""
    cmd = ["curl", "-sS", api_url(token, method)]
    for k, v in (fields or {}).items():
        cmd += ["-F", f"{k}={v}"]
    for k, p in (files or {}).items():
        cmd += ["-F", f"{k}=@{p}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {"ok": False, "_raw": out.stdout, "_err": out.stderr}


def die(msg, resp=None):
    print(f"FATAL: {msg}")
    if resp is not None:
        print("  response:", json.dumps(resp)[:500])
    sys.exit(1)


def mint_one(token, owner, name, title, entries, manifest):
    """entries: [(agent, reaction, abs_png_path, manifest_ref)]. Creates the set,
    reads it back, writes canonical file_ids into the referenced manifest dicts."""
    input_stickers = []
    for agent, reaction, path, _ref in entries:
        r = call(token, "uploadStickerFile", fields={"user_id": owner, "sticker_format": "static"},
                 files={"sticker": Path(path)})
        if not r.get("ok"):
            die(f"uploadStickerFile failed for {agent}/{reaction}", r)
        input_stickers.append({"sticker": r["result"]["file_id"], "format": "static",
                               "emoji_list": [EMOJI[reaction]]})
    r = call(token, "createNewStickerSet", fields={
        "user_id": owner, "name": name, "title": title,
        "sticker_type": "regular", "stickers": json.dumps(input_stickers)})
    if not r.get("ok"):
        die(f"createNewStickerSet failed for {name}", r)
    r = call(token, "getStickerSet", fields={"name": name})
    if not r.get("ok"):
        die(f"getStickerSet failed for {name}", r)
    got = r["result"]["stickers"]
    if len(got) != len(entries):
        print(f"WARN: {name} has {len(got)} stickers, expected {len(entries)}")
    for (_a, _rx, _p, ref), st in zip(entries, got):
        ref["file_id"] = st["file_id"]
    return f"https://t.me/addstickers/{name}"


def append_combined(token, owner, manifest):
    """Add entries with blank file_id values to the existing combined set."""
    entries = []
    for agent in AGENT_ORDER:
        for agent_name, reaction, path, ref in entries_for_agent(manifest, agent):
            if not ref.get("file_id"):
                entries.append((agent_name, reaction, path, ref))
    if not entries:
        die("append-combined found no manifest entries with blank file_id")

    set_names = {manifest[agent]["set_name"] for agent, *_rest in entries}
    if len(set_names) != 1:
        die(f"append-combined expected one set_name, got {sorted(set_names)}")
    name = set_names.pop()

    for agent, reaction, path, _ref in entries:
        r = call(token, "uploadStickerFile", fields={"user_id": owner, "sticker_format": "static"},
                 files={"sticker": Path(path)})
        if not r.get("ok"):
            die(f"uploadStickerFile failed for {agent}/{reaction}", r)
        sticker = {"sticker": r["result"]["file_id"], "format": "static",
                   "emoji_list": [EMOJI[reaction]]}
        r = call(token, "addStickerToSet", fields={
            "user_id": owner, "name": name, "sticker": json.dumps(sticker)})
        if not r.get("ok"):
            die(f"addStickerToSet failed for {agent}/{reaction}", r)

    r = call(token, "getStickerSet", fields={"name": name})
    if not r.get("ok"):
        die(f"getStickerSet failed for {name}", r)
    got = r["result"]["stickers"]
    if len(got) < len(entries):
        die(f"{name} returned fewer stickers than just appended", r)
    for (_a, _rx, _p, ref), st in zip(entries, got[-len(entries):]):
        ref["file_id"] = st["file_id"]
    return f"https://t.me/addstickers/{name}"


def entries_for_agent(manifest, agent):
    out = []
    by_reaction = {s["reaction"]: s for s in manifest[agent]["stickers"]}
    for reaction in REACTION_ORDER:
        s = by_reaction.get(reaction)
        if s:
            out.append((agent, reaction, STICKERS_DIR / s["file"], s))
    return out


def operator_set_name(bot_username: str) -> str:
    name = f"operator_after_midnight_by_{bot_username}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        die(f"derived the operator set name violates Bot API character/length rules: {name}")
    if "__" in name:
        die(f"derived the operator set name contains consecutive underscores: {name}")
    if not name.lower().endswith(f"_by_{bot_username.lower()}"):
        die(f"derived the operator set name does not end with bot suffix: {name}")
    return name


def entries_for_operator(manifest: Mapping[str, Any]) -> list[StickerEntry]:
    operator = manifest["operator"]
    by_reaction = {s["reaction"]: s for s in operator["stickers"]}
    out: list[StickerEntry] = []
    for reaction in OPERATOR_ORDER:
        sticker = by_reaction[reaction]
        out.append(
            StickerEntry(
                "operator",
                reaction,
                sticker["emoji"],
                STICKERS_DIR / sticker["file"],
                sticker,
            )
        )
    return out


def assert_standalone_operator_target(name: str, entries: Sequence[StickerEntry]) -> None:
    if "pantheon_pack_by_" in name:
        die(f"the operator standalone mint refused Pantheon set name: {name}")
    if not entries:
        die("the operator standalone mint found no entries")
    for entry in entries:
        if entry.owner_key != "operator":
            die(f"the operator standalone mint received non-operator entry: {entry.owner_key}/{entry.reaction}")
        if "pantheon_pack_by_" in str(entry.manifest_ref.get("set_name", "")):
            die(f"the operator standalone mint refused Pantheon manifest target for {entry.reaction}")


def mint_operator_standalone(manifest: dict[str, Any], *, token: str, owner: str = DEFAULT_OWNER) -> str:
    me = call(token, "getMe")
    if not me.get("ok"):
        die("getMe failed — token bad?", me)
    bot = me["result"]["username"]
    name = operator_set_name(bot)
    entries = entries_for_operator(manifest)
    assert_standalone_operator_target(name, entries)

    input_stickers = []
    for entry in entries:
        r = call(token, "uploadStickerFile", fields={"user_id": owner, "sticker_format": "static"},
                 files={"sticker": entry.path})
        if not r.get("ok"):
            die(f"uploadStickerFile failed for operator/{entry.reaction}", r)
        input_stickers.append({"sticker": r["result"]["file_id"], "format": "static",
                               "emoji_list": [entry.emoji]})

    r = call(token, "createNewStickerSet", fields={
        "user_id": owner, "name": name, "title": manifest["operator"]["title"],
        "sticker_type": "regular", "stickers": json.dumps(input_stickers)})
    if not r.get("ok"):
        die(f"createNewStickerSet failed for {name}", r)

    r = call(token, "getStickerSet", fields={"name": name})
    if not r.get("ok"):
        die(f"getStickerSet failed for {name}", r)
    got = r["result"]["stickers"]
    if len(got) != len(entries):
        die(f"{name} has {len(got)} stickers, expected {len(entries)}", r)
    manifest["operator"]["set_name"] = name
    for entry, sticker in zip(entries, got):
        entry.manifest_ref["file_id"] = sticker["file_id"]
        entry.manifest_ref["file_unique_id"] = sticker.get("file_unique_id", "")
    return f"https://t.me/addstickers/{name}"


def main(argv: list[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    structure = (argv[0] if argv else "combined").lower()
    token = os.environ["STICKER_BOT_TOKEN"].strip()
    owner = os.environ.get("OWNER_USER_ID", DEFAULT_OWNER).strip()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    links = []

    if structure == "operator":
        print(f"owner user_id: {DEFAULT_OWNER} | structure: {structure}")
        link = mint_operator_standalone(manifest, token=token, owner=DEFAULT_OWNER)
        links.append(f"operator: {link}")
        print(f"  minted {manifest['operator']['set_name']} -> {link}")
    else:
        me = call(token, "getMe")
        if not me.get("ok"):
            die("getMe failed — token bad?", me)
        bot = me["result"]["username"]
        print(f"bot: @{bot} | owner user_id: {owner} | structure: {structure}")

    if structure == "per-agent":
        for agent in AGENT_ORDER:
            name = f"pantheon_{agent}_by_{bot}"
            link = mint_one(token, owner, name, f"Pantheon — {agent.capitalize()}",
                            entries_for_agent(manifest, agent), manifest)
            manifest[agent]["set_name"] = name
            links.append(f"{agent}: {link}")
            print(f"  minted {name} -> {link}")
    elif structure == "append-combined":
        link = append_combined(token, owner, manifest)
        links.append(f"combined append: {link}")
        print(f"  appended missing stickers -> {link}")
    elif structure != "operator":  # combined
        name = f"pantheon_pack_by_{bot}"
        all_entries = [e for a in AGENT_ORDER for e in entries_for_agent(manifest, a)]
        link = mint_one(token, owner, name, "Pantheon", all_entries, manifest)
        for agent in AGENT_ORDER:
            manifest[agent]["set_name"] = name
        links.append(f"combined: {link}")
        print(f"  minted {name} ({len(all_entries)} stickers) -> {link}")

    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("\nmanifest updated. LINKS:")
    for l in links:
        print("  " + l)


if __name__ == "__main__":
    main()
