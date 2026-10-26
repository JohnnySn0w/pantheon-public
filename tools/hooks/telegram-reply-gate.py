#!/usr/bin/env python3
"""Telegram reply gate — Stop hook enforcing "answer the channel you were asked on".

THE FAILURE THIS EXISTS FOR (T-2188, operator-directed via archivist, TG 206/211/212):
a turn arrives from Telegram, the agent does the work and writes the answer to its
TRANSCRIPT, and the turn ends. The transcript is invisible to the sender, so from
The operator's side the agent simply went dark. archivist did this twice in three days
WITH a hard-rule pin in its CLAUDE.md. A pin is a reminder; this is a mechanism.

CONTRACT (verified against code.claude.com/docs/en/hooks 2026-07-28, not assumed):
  stdin  : {"session_id", "transcript_path", "cwd", "hook_event_name": "Stop", ...}
           NOTE: no `stop_hook_active` field exists in the current contract, so a
           Stop hook CANNOT rely on the harness to break a block loop. We bound it
           ourselves with the marker dir below. This is load-bearing.
  block  : {"decision": "block", "reason": "..."}   <- TOP-LEVEL, not the PreToolUse
           hookSpecificOutput/permissionDecision shape. Getting this wrong fails OPEN
           (unknown keys are ignored), which is the safe direction but silently
           disables the gate — test_telegram_reply_gate.py pins the exact shape.
  allow  : exit 0 with no stdout.

DESIGN INVARIANTS:
  - FAIL-OPEN, ALWAYS. Any error, missing field, unreadable/short transcript, parse
    failure -> allow the stop. A Stop hook that fails closed wedges the session with
    nobody at the keyboard, and this one runs on EVERY turn end on EVERY daemon.
  - BOUNDED: blocks at most ONCE per distinct inbound message (sha1 marker, 10-min
    prune). An agent that genuinely cannot reply (telegram down, tool unavailable)
    must be able to end its turn on the second attempt.
    Ref: feedback_bounded_repeating_remediation.
  - CHEAP: reads only the TAIL of the transcript (TAIL_BYTES). These files reach tens
    of MB (38MB measured on exocortex 2026-07-28); re-reading that on every turn end
    across seven daemons is not acceptable. If the answer is not in the tail window we
    fail open rather than escalate the read.
  - NARROW: only fires when the latest real user turn came from Telegram. Mailbox-,
    cron- and CLI-driven turns are untouched.
"""
import sys, os, json, glob, time, hashlib, re

TAIL_BYTES = 512 * 1024          # transcript tail window; see CHEAP invariant
MARKER_DIR = "~/.claude/hooks/.telegram_reply_markers"
MARKER_TTL = 600                 # 10 min, matching act-vs-ask-gate.py
CHANNEL_RE = re.compile(r"<channel\b[^>]*\bsource\s*=\s*[\"'][^\"']*telegram", re.I)


def allow():
    """Exit 0, no stdout = defer to normal flow (the turn ends)."""
    sys.exit(0)


def _is_reply_tool(name):
    """Match the telegram reply tool by SUFFIX, not exact id.

    Plugin-qualified ids differ per persona/install
    (mcp__plugin_telegram_telegram__reply, mcp__telegram__reply, ...), so an exact
    match would silently disable the gate on any daemon whose id differs — the
    failure mode being that the gate looks installed and enforces nothing.
    """
    n = (name or "").lower()
    return "telegram" in n and n.endswith("__reply")


def _iter_tail_records(path):
    """Yield parsed JSONL records from the tail window, oldest-first.

    Drops the first physical line: a byte-offset seek almost always lands
    mid-record, and a partial line is not valid JSON.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()          # discard partial record
        raw = fh.read()
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue               # tolerate any single malformed record


def _content_parts(rec):
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _is_real_user_turn(rec):
    """True only for an actual human/channel turn.

    tool_result records ALSO carry role="user" (verified on a live transcript
    2026-07-28). Treating one as the latest user turn would compare against the
    wrong point in the conversation and let a genuinely-dropped reply through.
    """
    if rec.get("type") == "user" or (rec.get("message") or {}).get("role") == "user":
        parts = _content_parts(rec)
        if parts and all(p.get("type") == "tool_result" for p in parts if isinstance(p, dict)):
            return False
        return True
    return False


def _text_of(rec):
    out = []
    for p in _content_parts(rec):
        if isinstance(p, dict) and p.get("type") == "text":
            out.append(p.get("text") or "")
        elif isinstance(p, str):
            out.append(p)
    return "\n".join(out)


def _replied_after(records, idx):
    """Did any assistant turn after `idx` call the telegram reply tool?"""
    for rec in records[idx + 1:]:
        if rec.get("type") == "assistant" or (rec.get("message") or {}).get("role") == "assistant":
            for p in _content_parts(rec):
                if isinstance(p, dict) and p.get("type") == "tool_use" and _is_reply_tool(p.get("name")):
                    return True
    return False


def _already_blocked_once(key):
    """Marker bounding. Returns True if we've already blocked this message.

    Any failure here returns True (=> allow), because the bounding mechanism
    failing must not become a way to block forever.
    """
    try:
        mdir = os.path.expanduser(MARKER_DIR)
        os.makedirs(mdir, exist_ok=True)
        now = time.time()
        for f in glob.glob(os.path.join(mdir, "*")):
            try:
                if now - os.path.getmtime(f) > MARKER_TTL:
                    os.remove(f)
            except OSError:
                pass
        mark = os.path.join(mdir, hashlib.sha1(key.encode("utf-8")).hexdigest())
        if os.path.exists(mark):
            try:
                os.remove(mark)     # deliberate re-stop -> let it through
            except OSError:
                pass
            return True
        open(mark, "w").close()
        return False
    except Exception:
        return True


REASON = (
    "TELEGRAM REPLY GATE — this turn was triggered by a Telegram message and is "
    "ending without calling the telegram reply tool. Your transcript is NOT visible "
    "to the sender; from their side you went silent. Send the substantive answer via "
    "the reply tool with the inbound chat_id, then end the turn. If you genuinely "
    "cannot reply (tool unavailable, or the turn truly needed no response), just end "
    "the turn again — this gate blocks only once per message and will not fire twice."
)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        allow()

    path = data.get("transcript_path") or ""
    if not path:
        # Fall back to the documented layout; fail open if we cannot construct it.
        sid, cwd = data.get("session_id"), data.get("cwd")
        if not sid or not cwd:
            allow()
        mangled = cwd.replace("/", "-")
        path = os.path.expanduser(f"~/.claude/projects/{mangled}/{sid}.jsonl")

    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        allow()

    try:
        records = list(_iter_tail_records(path))
    except Exception:
        allow()

    last_user = None
    for i, rec in enumerate(records):
        if _is_real_user_turn(rec):
            last_user = i
    if last_user is None:
        allow()                     # tail window held no user turn -> not our case

    if not CHANNEL_RE.search(_text_of(records[last_user])):
        allow()                     # not a telegram-inbound turn

    if _replied_after(records, last_user):
        allow()                     # reply already sent

    key = (data.get("session_id") or "") + "|" + _text_of(records[last_user])[:2000]
    if _already_blocked_once(key):
        allow()

    print(json.dumps({"decision": "block", "reason": REASON}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        allow()
