---
symptom: "A named subagent / teammate reports 'Message sent to team-lead' (SendMessage returned success:true), its own inbox file shows the inbound consumed ([]), team-lead.json stays [] and no <teammate-message> reaches the lead for minutes (observed: ~6 min lag, delivered at a later turn boundary — DELAYED, not dropped, in the 2026-08-25 case). Until it lands the answer exists only in the subagent's transcript."
fix: "Do not re-ask and do not conclude 'dropped' inside ~10 min — teammate->lead delivery lands at the lead's next turn boundary and can lag several minutes. If you cannot wait, recover the payload from the subagent transcript: ~/.claude/projects/<cwd-slug>/<session-id>/subagents/agent-<id>.jsonl — scan assistant entries for a content block of type tool_use with name SendMessage and print input.message. Expect the real delivery to arrive later too (dedupe). For in-process Agent-tool subagents the final text arrives as the task-notification result instead — this recipe is for the teammate spawn shape."
domain: pantheon, claude-code-harness, agent-teams, sendmessage, wake-drop, subagent-transcript, recovery
---

# Teammate reply dropped — recover the payload from the subagent transcript

## Symptom

CC 2.1.243, 2026-08-25, HO session. A `claude-code-guide` agent spawned with `Agent(name="cc-guide-teams-lifecycle")` registered as a teammate (member in `~/.claude/teams/session-<id>/config.json`, `inboxes/` created), received a question via `SendMessage`, and answered with its own `SendMessage({to: "team-lead", …})`. Its transcript's last assistant text: *"Message sent to team-lead."*

On the lead side: `inboxes/cc-guide-teams-lifecycle.json` = `[]` (inbound consumed), `inboxes/team-lead.json` = `[]`, and no `<teammate-message>` arrived for ~6 minutes — only the earlier `idle_notification` did. **Correction (same session, 05:00Z):** the reply DID arrive, as a normal `<teammate-message>` plus a second `idle_notification`, at a later turn boundary ~6 min after the send. So the observed shape is *delayed delivery at turn boundaries*, not a permanent drop — the permanent-drop case remains `bugs/team-pattern-sendmessage-intermittent-wake-failure.md`. The transcript recipe below is still the right move when you cannot wait, but expect the late delivery too and dedupe.

## Verification

```
ls -la ~/.claude/teams/session-<id>/inboxes/          # both files 2 bytes ("[]")
ls ~/.claude/projects/<cwd-slug>/<session-id>/subagents/   # agent-<name>-<hash>.jsonl exists
```

## Fix / recovery recipe

```
python3 - "$T" <<'PY'
import sys, json
for line in open(sys.argv[1], encoding="utf-8"):
    try: d = json.loads(line)
    except Exception: continue
    if d.get("type") != "assistant": continue
    for block in d.get("message", {}).get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SendMessage":
            inp = block.get("input", {})
            print("TO:", inp.get("to"), "| SUMMARY:", inp.get("summary"))
            print(inp.get("message"))
PY
```

with `T=~/.claude/projects/<cwd-slug>/<session-id>/subagents/agent-<name>-<hash>.jsonl`. Reading only the `tool_use` blocks keeps the lead's context small; never `cat` the whole transcript.

Cheap wake signal while waiting (harness-tracked, no pipes in the background command): poll `team-lead.json` for a parsed message count > 0 — but note the file is rewritten on delivery attempts, so size-only checks fire spuriously.

## Why it matters

Without the recipe the lead either re-asks (a second full run) or concludes the agent idled without answering. The transcript is the durable record; the inbox is not.

## Related

- `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` — the drop mechanism.
- `bugs/subagent-idles-without-sending-final-report.md` — the opposite failure (no send at all).
- `bugs/subagent-sendmessage-to-me-creates-zombie-inbox.md` — misaddressed sends.
- T-3078 (wake-drop mitigation stack), T-3074 (zombie teammates; carries the 2026-08-25 spawn-shape evidence: in-process Agent-tool subagents do not register in `config.json`/`inboxes/`, teammate-shaped agents do).
