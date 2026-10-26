#!/usr/bin/env python3
"""T-2693: give each old-hub path in a per-account config a new-hub twin, fail-closed.

Run AS the account that owns the config (the access check has to be that account's):

    python3 tools/t2693_config_roots.py codex-toml ~/.codex/config.toml
    python3 tools/t2693_config_roots.py claude-settings ~/.claude/settings.local.json

codex-toml: every "/home/the-ancestor/mailbox/<p>" entry in the file gets
"/home/post-office/mailbox/<p>" inserted on the line BEFORE it, with its own trailing
comma, so the edit is valid whether or not the old entry is the array's last element.
The old roots stay until phase 7: the old recipient dirs keep existing, and bwrap
stats every writable root, so a root that does not exist bricks EVERY codex shell
command. Postconditions: the file parses as TOML, every old root has its twin, and
every new root is a directory this account can write. Any failure restores the backup
and exits 1.

claude-settings: every permissions.allow rule naming the old hub gets a twin naming
the new hub.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib

OLD = "/home/the-ancestor/mailbox/"
NEW = "/home/post-office/mailbox/"


def add_toml_twins(text: str) -> tuple[str, list[str]]:
    """Return (new_text, personas). Idempotent: an existing twin is not duplicated."""
    personas = list(dict.fromkeys(re.findall(r'"' + re.escape(OLD) + r'([A-Za-z0-9_-]+)"', text)))
    for p in personas:
        twin = f'"{NEW}{p}"'
        if twin in text:
            continue
        text = re.sub(r'(?m)^([ \t]*)("' + re.escape(OLD) + re.escape(p) + r'")',
                      lambda m: f'{m.group(1)}{twin},  # T-2693 new hub\n{m.group(1)}{m.group(2)}',
                      text, count=1)
    return text, personas


def _all_strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for x in node for s in _all_strings(x)]
    if isinstance(node, dict):
        return [s for x in node.values() for s in _all_strings(x)]
    return []


def check_toml(text: str, personas: list[str], *, check_access: bool = True) -> list[str]:
    """Postcondition problems (empty list = OK)."""
    try:
        values = _all_strings(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        return [f"edited file does not parse as TOML: {exc}"]
    problems = []
    if not personas:
        problems.append("no old-hub roots found (layout drift?) -- refusing to report success")
    for p in personas:
        new = NEW + p
        if new not in values:
            problems.append(f"missing twin {new}")
        elif check_access and not (os.path.isdir(new) and os.access(new, os.W_OK | os.X_OK)):
            problems.append(f"new root {new} is not a writable directory for this account")
    return problems


def codex_toml(path: str) -> int:
    original = open(path).read()
    text, personas = add_toml_twins(original)
    problems = check_toml(text, personas)
    if problems:
        print(f"[t2693] {path}: NOT changed: " + "; ".join(problems), file=sys.stderr)
        return 1
    if text == original:
        print(f"[t2693] {path}: already has every twin ({' '.join(personas)})")
        return 0
    backup = path + ".bak.t2693"
    shutil.copy2(path, backup)
    with open(path, "w") as fh:
        fh.write(text)
    again = check_toml(open(path).read(), personas)
    if again:
        shutil.copy2(backup, path)
        print(f"[t2693] {path}: RESTORED from backup: " + "; ".join(again), file=sys.stderr)
        return 1
    print(f"[t2693] {path}: added new-hub twins for {' '.join(personas)} (backup {backup})")
    return 0


def claude_settings(path: str) -> int:
    data = json.load(open(path))
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    added = [r.replace(OLD, NEW) for r in allow if OLD in r and r.replace(OLD, NEW) not in allow]
    if not added:
        print(f"[t2693] {path}: nothing to add")
        return 0
    shutil.copy2(path, path + ".bak.t2693")
    allow.extend(added)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    json.load(open(path))   # postcondition: still valid JSON
    print(f"[t2693] {path}: added {len(added)} rule(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("codex-toml", "claude-settings"):
        print(__doc__, file=sys.stderr)
        sys.exit(64)
    sys.exit(codex_toml(sys.argv[2]) if sys.argv[1] == "codex-toml" else claude_settings(sys.argv[2]))
