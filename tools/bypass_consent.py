#!/usr/bin/env python3
"""Pre-accept Claude Code's one-time bypass-permissions warning dialog (T-4935).

Intent: seats launched with ``--permission-mode bypassPermissions`` must never
stall on the interactive "WARNING ... Bypass Permissions mode" dialog (its
preselected choice is "No, exit" and restart tooling never presses Enter).

Evidence (Claude Code 2.1.296 binary): the dialog gate is
``!hasConsent() && !globalConfig.bypassPermissionsModeAccepted`` where
``hasConsent()`` reads ONLY ``policySettings`` and ``userSettings``. The
``--settings`` flag lands in ``flagSettings`` and ``.claude/settings.local.json``
in ``localSettings``; neither counts (the binary's own help text says so). So the
key must live in the account's user-scope ``<claude-dir>/settings.json``. This
module idempotently merges ``skipDangerousModePermissionPrompt: true`` there.

Fail-open by design: a missing/corrupt/unwritable settings file never blocks a
launch and a corrupt file is never overwritten.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

KEY = "skipDangerousModePermissionPrompt"

# Shell-embeddable form of ensure_bypass_consent for the tracked supervise
# scripts (they run as the seat's own unix user and are copied to live paths, so
# they cannot import repo code). Single source: test_bypass_consent asserts every
# script embeds this exact text, and executes it against a temp dir.
SHELL_SNIPPET = (
    "python3 -c 'import json,os,sys;"
    'd=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude");'
    'p=os.path.join(d,"settings.json");'
    "s=json.load(open(p)) if os.path.exists(p) else {};"
    'sys.exit(0) if s.get("skipDangerousModePermissionPrompt") is True else None;'
    "os.makedirs(d,exist_ok=True);"
    's["skipDangerousModePermissionPrompt"]=True;t=p+".tmp";'
    'json.dump(s,open(t,"w"),indent=2);os.replace(t,p)\' >/dev/null 2>&1 || true'
)


def ensure_bypass_consent(claude_dir: Path) -> bool:
    """Merge the consent key into ``<claude_dir>/settings.json``.

    Returns True when the key is present afterwards, False if it could not be
    ensured (corrupt JSON, non-object root, OS error). Never raises.
    """
    path = Path(claude_dir) / "settings.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            return False
        if data.get(KEY) is True:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        data[KEY] = True
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    )
    return 0 if ensure_bypass_consent(target) else 1


if __name__ == "__main__":
    sys.exit(main())
