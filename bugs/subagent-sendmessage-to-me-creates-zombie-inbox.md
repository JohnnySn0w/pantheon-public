---
symptom: "Subagent calls SendMessage({to: 'me', message: {...}}) — typically a task_assignment JSON meant for its own TodoWrite state — and the harness silently creates a new inbox file at ~/.claude/teams/session-<id>/inboxes/me.json owned by nothing, holding the message forever. Same for other invalid recipient strings (e.g. name-typos, self-references). No error to the subagent; no delivery to any real recipient; the message just accumulates in a zombie inbox."
fix: "Never use string 'me' (or any self-referential string) as a SendMessage recipient. Self-state goes to TodoWrite/TaskCreate, not SendMessage. If you need to signal your spawner, use the exact spawner name (usually 'team-lead') — verify against ListAgents output first if unsure. Brief this rule in every dispatch that goes to a subagent."
domain: pantheon, claude-code-harness, agent-teams, sendmessage, zombie-inbox, subagent-dispatch
---

# Subagent SendMessage to 'me' (or any invalid recipient) creates zombie inbox

## Symptom

A subagent (typically an implementer or reviewer, but any Agent-tool spawn) calls:

```
SendMessage({to: "me", message: {"type": "task_assignment", ...}})
```

The `SendMessage` returns `{success: true, msg_id: "..."}` — no error. The harness silently CREATES a new inbox file at `~/.claude/teams/session-<id>/inboxes/me.json` and stores the message there. No agent is listening on that inbox. The message sits forever. The subagent believes its send worked; the spawner never sees it.

Repeated by other invalid recipient strings — case-typos, self-references, hallucinated peer names. Any string that isn't a live team-member becomes a new zombie inbox file.

## Observed cases

2026-08-22 arc (T-3020 BT5 + BT4 impl subagents):
- `T-3020-BT5-label-verb-backfill` sent a `task_assignment` JSON to `to: "me"` — created `me.json` with the message stuck inside. 881 bytes, 1 message, permanent unless manually deleted.
- `T-3020-BT4-validate-hook-detector` sent to `to: "t-3020-bt4-impl"` (name-collapsed / hallucinated self-reference) — created `t-3020-bt4-impl.json`. 1156 bytes, 1 message, permanent.

Both were self-management messages the subagent probably meant for TodoWrite/TaskCreate, not SendMessage.

## Verification

```
ls -la ~/.claude/teams/session-<id>/inboxes/ | grep -v .json$ -v
# Look for files matching lowercase 'me.json' or persona-name-typos
python3 -c "import json; print(json.load(open('~/.claude/teams/session-<id>/inboxes/me.json')))"
# If it exists + contains messages, they're stuck
```

Cross-check ListAgents from the sending subagent — anything sent to a name NOT in that list becomes a zombie file.

## Fix

**Subagent-side (dispatch-brief rule):** every dispatch to a subagent should include:

> "For SELF-state (task-list, notes), use TodoWrite/TaskCreate, NOT SendMessage. For messages to me (the spawner), use `to: 'team-lead'` — that's my exact name. For messages to peer subagents, get their exact name from ListAgents. Never SendMessage to `'me'` or any self-referential string — the harness silently creates a zombie inbox."

**Cleanup-side:** delete the zombie inbox files after the arc completes:

```
# Identify:
ls ~/.claude/teams/session-<id>/inboxes/ | grep -iE '^(me|self|orchestrator|<any-typo>)\.json$'

# Remove after verifying content is stuck (no live recipient):
rm ~/.claude/teams/session-<id>/inboxes/me.json
rm ~/.claude/teams/session-<id>/inboxes/<typo>.json
```

## Why the harness is quiet

The harness `SendMessage` implementation appears to auto-provision any recipient inbox that doesn't exist, on the theory that agents might come online later. That's the wrong default for a string like `'me'` (semantically self-referential, never a real recipient) — an unknown-recipient error would surface the bug at send-time instead of silently accumulating cruft.

## Related

- `bugs/nested-subagent-completion-notification-routes-to-main-not-spawner.md` — different failure mode (harness routing) but same class family (subagent-messaging misdirection).
- `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` — the wake-hook drop; distinct mechanism.
- `bugs/subagents-cannot-spawn-named-teammates-flat-roster-wake-drop.md` — spawn-side cousin.
- T-3074 (zombie-teammate cleanup umbrella tracking).
