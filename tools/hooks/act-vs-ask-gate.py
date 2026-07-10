#!/usr/bin/env python3
"""Act-vs-ask gate — PreToolUse hook on the Telegram reply tool.

When Recovery is about to send the operator a *permission-asking* question ("want me
to…", "should I…"), block the send ONCE to force the 3-prong act-vs-ask test
before it goes out. Re-sending the SAME text passes (one forced check per
distinct question — no infinite loop, since a deny that the model re-issues
verbatim is allowed the second time).

Design invariants:
  - FAIL-OPEN. Any error, missing field, or unparseable input -> allow the send.
    This hook must NEVER be able to brick Recovery's ability to message the operator.
  - Only gates permission-asking patterns; ordinary status/answer messages
    (even with a '?') pass untouched.
  - Stateless except a short-TTL marker dir keyed by message hash.

Wired in ~/.claude/settings.json as a PreToolUse hook with
matcher "mcp__plugin_telegram_telegram__reply". See pinned-rules/recovery.md §1.
"""
import sys, json, os, hashlib, time, glob


def allow():
    # No stdout + exit 0 = defer to normal flow (send proceeds).
    sys.exit(0)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        text = (data.get("tool_input") or {}).get("text") or ""
    except Exception:
        allow()

    if not text:
        allow()

    lc = text.lower()
    # Permission-asking patterns = the "ask instead of act" slip this gate exists for.
    patterns = (
        "want me to", "should i ", "shall i ", "do you want me", "ok to ",
        "is it ok", "is that ok", "want this filed", "want this done",
        "should we ", "want me to file", "shall i file", "do you want this",
    )
    if not any(p in lc for p in patterns):
        allow()

    # One-block-per-distinct-message via a short-TTL marker dir.
    try:
        mdir = os.path.expanduser("~/.claude/hooks/.ask_gate_markers")
        os.makedirs(mdir, exist_ok=True)
        now = time.time()
        for f in glob.glob(os.path.join(mdir, "*")):
            try:
                if now - os.path.getmtime(f) > 600:  # prune > 10 min
                    os.remove(f)
            except OSError:
                pass
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        mark = os.path.join(mdir, h)
        if os.path.exists(mark):
            # Already prompted for this exact message -> it's a deliberate re-send; allow.
            try:
                os.remove(mark)
            except OSError:
                pass
            allow()
        open(mark, "w").close()  # first sighting -> record, then block
    except Exception:
        allow()

    reason = (
        "ACT-VS-ASK GATE — this message asks the operator to approve an action. "
        "Run the 3-prong test on the underlying action: (1) irreversible? "
        "(2) external-facing? (3) novel org-level decision? If NONE trip, it is "
        "in-scope + reversible: CANCEL this question, DO the action, and report "
        "the result instead. If a prong genuinely trips (it is truly his call), "
        "re-send this exact message and it will pass."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        allow()
