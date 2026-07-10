"""tools/ancestor_consume.py — the-ancestor daemon's mailbox dispatch loop (T-0765 #2 glue).

Ties the ORG-026 mailbox to the Veneration harness: the-ancestor consumes resume-requests
from its inbox and services each by resurrecting the named dead session, then delivers the
harvested HANDOFF back to the requester's mailbox (the T-0765 deliverable-5 write-back —
same substrate, no sudo-back, no group-writable-HANDOFF parallel path).

Invoked by the-ancestor's (channel-less) session when the mailbox-wake kick fires
(pantheon-mailbox-wake@ancestor). Drains authoritatively (readdir-is-truth via
mailbox.consume); the per-inbox single-consumer lease serializes concurrent drains.

Payload protocol (v1):
  request  : {"type": "resume_request", "agent": <name>, "dead_jsonl": <path>,
              "reply_inbox": <path?>}          # reply_inbox defaults to the agent's inbox
  delivery : {"type": "handoff_delivery", "agent": <name>, "handoff_text": <str>}
  failure  : {"type": "resume_failure",  "agent": <name>, "detail": <str>}

The requesting agent's own mailbox consumer writes a delivered handoff_text into its
/home/<agent>/HANDOFF.md. Handler is idempotent-safe (at-least-once ack): re-running a
resume is safe (it just re-harvests), and a duplicate delivery is overwrite-identical.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # tools.-first so discover-from-root and bare-run resolve one module each
    from tools import mailbox
    from tools.veneration import resurrect, Result, validate_request
except ImportError:
    import mailbox
    from veneration import resurrect, Result, validate_request

RESUME_REQUEST = "resume_request"
HANDOFF_DELIVERY = "handoff_delivery"
RESUME_FAILURE = "resume_failure"

_ANCESTOR_INBOX = "/home/the-ancestor/inbox"
_STAGING_DIR = "/home/the-ancestor/harvest"

# SECURITY: an explicit reply_inbox arrives from the SHARED mailbox payload (any peer can
# write it) and flows into mailbox.send → _ensure_subdirs, which creates directories at
# whatever path it is handed. Vet it to an absolute, traversal-free, safe-charset path
# at the payload boundary; an unsafe value is ignored in favor of the trusted derived
# inbox below. mailbox.send ALSO enforces this same guard at the sink (T-0906) — the two
# layers are intentional defense-in-depth: this one chooses a safe fallback, the sink one
# is the backstop for every other caller. Shared shape lives in mailbox.is_safe_inbox.
_safe_inbox = mailbox.is_safe_inbox

# agent → its inbox (default reply target). Mirrors the per-agent account map.
_AGENT_INBOX = {
    "orchestrator": "/home/claude/inbox",
    "specialist": "/home/claude-research/inbox",
    "recovery": "/home/claude-fallback/inbox",
}


def make_handler(*, resurrect_fn=resurrect, send_fn=None, staging_dir: str = _STAGING_DIR):
    """Build the mailbox handler. Side effects (resurrect, send, file read) are injected
    so the dispatch logic is unit-testable. send_fn defaults to mailbox.send."""
    send = send_fn or mailbox.send

    def handler(msg: dict) -> None:
        payload = msg.get("payload") or {}
        if payload.get("type") != RESUME_REQUEST:
            # Not ours to service — ack-and-ignore (the daemon only handles resume requests).
            print(f"[ancestor] ignoring non-resume message {msg.get('id')}", file=sys.stderr)
            return
        agent = payload.get("agent")
        dead_jsonl = payload.get("dead_jsonl")
        sender = msg.get("sender")
        # Best reply target: explicit reply_inbox > the named agent's inbox > the sender's.
        # The explicit value is untrusted, so accept it only if it passes _safe_inbox;
        # otherwise fall back to the trusted (constant) per-agent inbox map. This guarantees
        # fail()/send() below can only ever target a vetted path.
        explicit = payload.get("reply_inbox")
        reply_inbox = ((explicit if _safe_inbox(explicit) else None)
                       or _AGENT_INBOX.get(agent or "")
                       or _AGENT_INBOX.get(sender or ""))

        def fail(detail: str) -> None:
            # Never silently ack-drop a resume request: notify a discoverable inbox so the
            # requester learns it failed, instead of the message vanishing into cur/.
            if reply_inbox:
                send(reply_inbox, {"type": RESUME_FAILURE, "agent": agent, "detail": detail},
                     sender="ancestor")
                print(f"[ancestor] resume_failure for {agent!r} → {reply_inbox}: {detail}", file=sys.stderr)
            else:
                print(f"[ancestor] DROPPED {msg.get('id')} (no derivable reply inbox): {detail}",
                      file=sys.stderr)

        if not reply_inbox:
            print(f"[ancestor] DROPPED {msg.get('id')}: no reply inbox "
                  f"(agent={agent!r}, sender={sender!r})", file=sys.stderr)
            return
        if not agent or not dead_jsonl:
            fail("malformed resume_request (missing agent/dead_jsonl)")
            return
        # SECURITY: validate the untrusted (agent, dead_jsonl) at the trust boundary —
        # BEFORE agent is interpolated into the staging path and the mkdir/unlink side
        # effects fire below. resurrect() re-validates (defense in depth), but a crafted
        # agent (e.g. path traversal) would otherwise reach mkdir/unlink first. Same
        # boundary-validation principle the veneration shell-injection fix established.
        bad = validate_request(agent, dead_jsonl)
        if bad:
            fail(f"rejected: {bad}")
            return

        staging = Path(staging_dir) / f"{agent}-HANDOFF.md"
        staging.parent.mkdir(parents=True, exist_ok=True)
        # Clear any stale prior harvest so "content present after the run" is the witness —
        # never deliver leftover content from an earlier run/agent as if freshly harvested.
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            fail(f"could not clear staging {staging}: {exc}")
            return

        res: Result = resurrect_fn(agent, dead_jsonl, str(staging))
        if not res.ok:
            fail(res.detail or f"resume {res.status}")
            return

        try:
            text = staging.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            fail(f"harvested-but-unreadable: {exc}")
            return
        if not text.strip():
            fail("harvest reported ok but produced no handoff content (empty staging)")
            return
        send(reply_inbox, {"type": HANDOFF_DELIVERY, "agent": agent, "handoff_text": text},
             sender="ancestor")
        print(f"[ancestor] delivered harvested HANDOFF for {agent} → {reply_inbox}")

    return handler


def consume_once(inbox: str = _ANCESTOR_INBOX, **handler_kwargs) -> dict:
    """Drain the-ancestor's inbox once, dispatching each resume-request. Returns the
    mailbox.consume summary ({skipped, processed, errors})."""
    return mailbox.consume(inbox, make_handler(**handler_kwargs))


if __name__ == "__main__":
    summary = consume_once()
    print(f"[ancestor] consume: {summary}")
    raise SystemExit(0)
