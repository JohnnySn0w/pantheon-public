---
symptom: "SendMessage from a project-orchestrator to the team-lead routed to the personal name (`orchestrator` / `specialist` / `recovery`) sends successfully but lands in a separate inbox bucket, NOT the lead's auto-delivery channel — recipient has to read off disk and ask for resend"
fix: "address SendMessage `to:` by the system identifier (`team-lead`, `orchestrator`, `implementer`) — never the personal name. Personal names are stylistic; system addresses are routing identifiers."
domain: pantheon, sendmessage, team-routing
---


> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) — the live dispatch guidance these symptoms taught now lives in that checklist (ORG-012 supersedure-trail, T-0833 D-1). This entry remains the recorded symptom + diagnosis; act on the checklist for current guidance.

## Symptom

Project-orchestrator sends a status update to HO via:

```python
SendMessage(to="orchestrator", message="...", summary="...")
```

Tool returns success. Message does not appear in HO's auto-delivered conversation turns. Lead has to read off disk via `cat ~/.claude/teams/<team>/inboxes/<personal-name>.json` and ask for a resend addressed to the system identifier.

## Cause

Team members have both a **personal name** (carried across sessions, e.g., `orchestrator`, `specialist`, `recovery` — set in user-scope memory) and a **system address** (the routing identifier, set by `TeamCreate` + `Agent` spawn — usually `team-lead`, `orchestrator`, `implementer`, etc.). They are the same entity in conversation but the team-mailbox routing layer only auto-delivers to the system address.

Personal-name messages still send successfully — the routing layer creates a separate inbox file under the personal name and silently keeps it there. Auto-delivery to the team lead's conversation pipe doesn't fire.

## Fix

Use the system address in `SendMessage.to:`:

```python
# Correct:
SendMessage(to="team-lead", message="...", summary="...")

# Wrong:
SendMessage(to="orchestrator", message="...", summary="...")
```

Find the system address by reading the team config:

```bash
cat ~/.claude/teams/<team_name>/config.json | jq '.members[].name'
```

In dispatch-brief prose, the lead may be referenced by personal name (Orchestrator, Specialist, Recovery) — that's stylistic. In your own logs / internal reasoning / ledger entries you can use the personal name. **Only the SendMessage `to:` field is sensitive.**

## Discovered

- 2026-05-04 in b9-inbox-consumer team (Pantheon's first real team-pattern run). First check-in to "orchestrator" landed in a separate bucket; lead had to read off disk and asked for a resend. Lead clarified: "the 'orchestrator' name in the brief was a personal-name reference; the system address for routing is `team-lead`."
- Confirmed across every team-pattern cycle since.

## Pairs with

- `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md` — system address `team-lead` itself can be pre-allocated; check before assuming.
- `bugs/teamcreate-orchestrator-restricted-tool-inventory.md` — same dispatch-pattern context.
- `bugs/sendmessage-body-delivers-summary-preview-only.md` — adjacent SendMessage gotcha (parameter shape).
- `incidents/2026-05-04-b9-team-pattern-first-run.md` — origin retrospective.

## Keywords

pantheon, sendmessage, team routing, personal name, system address, system name, team-lead, orchestrator name, implementer name, inbox bucket, separate inbox, silent misroute, auto-delivery, b9
