#!/usr/bin/env python3
"""Urgent-mail-inject hook — the "inject while working" leg of severity=urgent
(T-1641, ORG-026 Phase 2; closes T-2537's working-mid-arc delivery gap).

Locked design (notes/2026-07-08-mailbox-severity-design-resolution.md
§"Urgent path" item 1): on an urgent send, `tools/mailbox.py` drops a cheap
`inbox/.urgent_pending` marker (sender+subject+msgid) in the recipient's inbox.
This PreToolUse hook stats that marker on EVERY tool call — matcher `*`, broad by
design per [[feedback_hooks_broad_by_default]], because the whole point is
preempting a busy agent at its NEXT tool-call boundary, whichever tool that is —
and if present, injects a one-line "drain now" nudge via `additionalContext` (the
identical channel recall-feedback.py already proves fleet-reliable), then CLEARS
the marker. Clear-on-injection is load-bearing: it is what makes this exactly one
mid-turn nudge per urgent send, never a per-tool-call storm.

BUILD-NOT-ACTIVATE (T-1641): this file is version-controlled and tested but NOT
wired into any `~/.claude/settings.json` and NOT added to HOOK_ALLOWLIST in
tools/context_fill_monitor.py — both are a separate, operator-coordinated
supervised-first-fire step (see tools/hooks/README.md). It ALSO self-checks
`PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED` (belt-and-suspenders: a no-op even if
someone hand-wires it before that flag flips) — same env var mailbox.py's
`urgent_phase2_enabled()` gates on. The name is duplicated here rather than
imported: this hook fires on every tool call once wired, so the common path (flag
off, or marker absent) must cost an env read + one stat, not a mailbox_wake/
AGENT_PANE import; test_urgent_mail_inject.py asserts the two stay in sync.

Design invariants (mirrors recall-feedback.py):
  * FAIL-OPEN. Any error, missing field, unreadable/corrupt marker, or
    unresolvable inbox -> emit nothing, exit 0, let the tool run. This hook must
    NEVER block or break a tool call.
  * Harness asymmetry (documented at mailbox.send(), repeated here): this is a
    Claude-Code PreToolUse hook. codex has no PreToolUse parity and structurally
    cannot run it regardless of wiring — it is served by the escalation ladder
    (tools/urgent_escalation.py) and mailbox_wake's debounce-bypass instead.

Wiring (the interactive step, NOT done by building this file): add to
~/.claude/settings.json as a PreToolUse hook with matcher "*", AND add this
basename to HOOK_ALLOWLIST in tools/context_fill_monitor.py (per the wire-hook
discipline). T-1641 deliberately does neither — see tools/hooks/README.md.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

# Kept in exact sync with mailbox._URGENT_PHASE2_ENV — see the module docstring
# for why this is a hardcoded copy rather than an import.
_URGENT_PHASE2_ENV = "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED"

# Must match mailbox.py's marker filename (T-1641 component 1).
_MARKER_NAME = ".urgent_pending"

# Test/operator override for which inbox to check — mirrors recall-feedback.py's
# RECALL_MEMORY_DIR pattern.
_INBOX_OVERRIDE_ENV = "URGENT_MAIL_INBOX"


def _flag_enabled() -> bool:
    return os.environ.get(_URGENT_PHASE2_ENV, "").strip().lower() in ("1", "true", "yes")


def _resolve_inbox():
    """This session's own inbox root (a Path), or None if it cannot be
    determined. Checked ONLY when the flag is on (see main()) — the mailbox_wake
    import this needs is not worth paying on the (overwhelmingly common,
    pre-activation) flag-off path.

    Derives the persona from the unix user via mailbox_wake.AGENT_PANE's home-dir
    map — the same "unix user -> persona via a table" shape
    hook_allowlist_lint.local_agent() uses via AGENT_CONFIG, but reusing
    mailbox_wake (deliberately import-light per its own docstring) instead of the
    much heavier context_fill_monitor.
    """
    override = os.environ.get(_INBOX_OVERRIDE_ENV, "").strip()
    if override:
        return Path(override)
    try:
        tools_dir = str(Path(__file__).resolve().parent.parent)
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import mailbox_wake as mw  # noqa: PLC0415 (lazy by design, see above)
    except Exception:
        return None
    user = getpass.getuser()
    for persona, cfg in mw.AGENT_PANE.items():
        if Path(cfg.get("home", "")).name == user:
            return mw.MAILBOX_HUB / persona
    return None


def _clear(marker: Path) -> None:
    try:
        marker.unlink()
    except OSError:
        pass


def _emit(context: str) -> None:
    """Emit a non-blocking PreToolUse context injection and exit 0.

    No permissionDecision (T-4650): additionalContext alone reaches Claude and the call
    continues through the normal permission flow. An "allow" here approved whichever
    tool call the nudge happened to land on, past the permission prompt and, in auto
    mode, the classifier, and how it combines with another hook's deny is undocumented."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


def _allow() -> None:
    """No output -> defer to normal flow (tool runs)."""
    sys.exit(0)


def build_nudge(sender: str, subject: str) -> str:
    return f"🚨 URGENT inter-agent mail from {sender}: {subject} — drain now."


def main() -> None:
    # Read+discard stdin regardless of outcome: PreToolUse hooks are expected to
    # consume their input, and a malformed/absent payload is not itself a reason
    # to skip the urgent check (unlike recall-feedback.py, this hook does not
    # need anything FROM the tool call — it only cares whether urgent mail is
    # pending, not which tool is about to run).
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        if not _flag_enabled():
            _allow()
            return
        inbox = _resolve_inbox()
        if inbox is None:
            _allow()
            return
        marker = inbox / _MARKER_NAME
        if not marker.exists():
            _allow()
            return
        try:
            rec = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt/unreadable marker: clear it so it cannot wedge every future
            # tool call, and fail open on THIS call.
            _clear(marker)
            _allow()
            return
        sender = rec.get("sender") or "?"
        subject = rec.get("subject") or "(no subject)"
        # Clear BEFORE emitting: exactly one nudge per urgent send, never a
        # per-tool-call storm, and a crash between clear and emit still leaves
        # the marker gone (fail toward silence-once, not toward re-storming).
        _clear(marker)
        _emit(build_nudge(sender, subject))
    except Exception:
        # FAIL-OPEN — never let this hook break a tool call.
        _allow()


if __name__ == "__main__":
    main()
