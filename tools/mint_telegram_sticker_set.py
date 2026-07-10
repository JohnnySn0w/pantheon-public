#!/usr/bin/env python3
"""Mint Telegram sticker set(s) from assets/stickers/manifest.json (T-1002).

Token is read from env STICKER_BOT_TOKEN (never stored/committed). Owner user_id
from env OWNER_USER_ID (default the operator 456474197 — must have /start-ed the bot).
Structure from argv[1]: 'combined' (one Pantheon set, default) or 'per-agent'
(one set per agent). Writes the returned file_ids + real set_name back into the
manifest. Telegram rejects duplicate set names, so re-running over an existing
set errors loudly — deleteStickerSet or rename first.

First mint: the combined `pantheon_pack_by_panethon_codex_bot` (24 stickers),
2026-06-09. NOTE: getStickerSet file_ids are per-bot — a different sending bot
must re-fetch the set's file_ids under its own token (T-1024 / HO).
"""
import json, os, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = f"{REPO}/assets/stickers/manifest.json"
STICKERS_DIR = f"{REPO}/assets/stickers"
TOKEN = os.environ["STICKER_BOT_TOKEN"].strip()
OWNER = os.environ.get("OWNER_USER_ID", "456474197").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

EMOJI = {"idle": "\U0001F916", "shipped": "\U0001F680", "approved": "✅",
         "investigating": "\U0001F50D", "thinking": "\U0001F914", "wedged": "⚠️"}
REACTION_ORDER = ["idle", "shipped", "approved", "investigating", "thinking", "wedged"]
AGENT_ORDER = ["orchestrator", "specialist", "recovery", "codex"]


def call(method, fields=None, files=None):
    """POST via curl; returns parsed JSON dict. fields: {k:v}; files: {k:path}."""
    cmd = ["curl", "-sS", f"{API}/{method}"]
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


def mint_one(name, title, entries, manifest):
    """entries: [(agent, reaction, abs_png_path, manifest_ref)]. Creates the set,
    reads it back, writes canonical file_ids into the referenced manifest dicts."""
    input_stickers = []
    for agent, reaction, path, _ref in entries:
        r = call("uploadStickerFile", fields={"user_id": OWNER, "sticker_format": "static"},
                 files={"sticker": path})
        if not r.get("ok"):
            die(f"uploadStickerFile failed for {agent}/{reaction}", r)
        input_stickers.append({"sticker": r["result"]["file_id"], "format": "static",
                               "emoji_list": [EMOJI[reaction]]})
    r = call("createNewStickerSet", fields={
        "user_id": OWNER, "name": name, "title": title,
        "sticker_type": "regular", "stickers": json.dumps(input_stickers)})
    if not r.get("ok"):
        die(f"createNewStickerSet failed for {name}", r)
    r = call("getStickerSet", fields={"name": name})
    if not r.get("ok"):
        die(f"getStickerSet failed for {name}", r)
    got = r["result"]["stickers"]
    if len(got) != len(entries):
        print(f"WARN: {name} has {len(got)} stickers, expected {len(entries)}")
    for (_a, _rx, _p, ref), st in zip(entries, got):
        ref["file_id"] = st["file_id"]
    return f"https://t.me/addstickers/{name}"


def main():
    structure = (sys.argv[1] if len(sys.argv) > 1 else "combined").lower()
    me = call("getMe")
    if not me.get("ok"):
        die("getMe failed — token bad?", me)
    bot = me["result"]["username"]
    print(f"bot: @{bot} | owner user_id: {OWNER} | structure: {structure}")

    manifest = json.load(open(MANIFEST))
    links = []

    def entries_for(agent):
        out = []
        by_reaction = {s["reaction"]: s for s in manifest[agent]["stickers"]}
        for reaction in REACTION_ORDER:
            s = by_reaction.get(reaction)
            if s:
                out.append((agent, reaction, f"{STICKERS_DIR}/{s['file']}", s))
        return out

    if structure == "per-agent":
        for agent in AGENT_ORDER:
            name = f"pantheon_{agent}_by_{bot}"
            link = mint_one(name, f"Pantheon — {agent.capitalize()}",
                            entries_for(agent), manifest)
            manifest[agent]["set_name"] = name
            links.append(f"{agent}: {link}")
            print(f"  minted {name} -> {link}")
    else:  # combined
        name = f"pantheon_pack_by_{bot}"
        all_entries = [e for a in AGENT_ORDER for e in entries_for(a)]
        link = mint_one(name, "Pantheon", all_entries, manifest)
        for agent in AGENT_ORDER:
            manifest[agent]["set_name"] = name
        links.append(f"combined: {link}")
        print(f"  minted {name} ({len(all_entries)} stickers) -> {link}")

    json.dump(manifest, open(MANIFEST, "w"), indent=2)
    open(MANIFEST, "a").write("\n")
    print("\nmanifest updated. LINKS:")
    for l in links:
        print("  " + l)


if __name__ == "__main__":
    main()
