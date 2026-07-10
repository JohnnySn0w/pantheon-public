---
symptom: "SendMessage with content placed in `summary` parameter and `message` body left empty/meta — recipient (e.g., orchestrator) doesn't see the actual content; only sees the one-line preview shown in idle notifications"
fix: "in dispatch briefs, include literal SendMessage parameter framing: `your reply must use SendMessage with message=<full content inline> and summary=<one-line preview>`. The `message` parameter is what the recipient reads; `summary` is metadata only (preview shown in UI notifications, not delivered as content)."
domain: pantheon, sendmessage, plan-first-dispatch
---


> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) — the live dispatch guidance these symptoms taught now lives in that checklist (ORG-012 supersedure-trail, T-0833 D-1). This entry remains the recorded symptom + diagnosis; act on the checklist for current guidance.

## Symptom

Dispatched implementer returns plan-first via SendMessage. The `message` body contains only meta-description ("13 DECISIONS, --resume in (f), 19-token list in (a)") while claiming "plan submitted inline in my reply above." The actual plan content is in the `summary` parameter, not `message`.

Recipient (orchestrator or HO) sees only a one-line idle-notification preview matching the `summary`; the orchestrator's mental model expects to read the plan body but it isn't there.

First clarification ("messages between teammates arrive as discrete inbox entries, not a shared scrollback") doesn't always unstick. Second pass succeeds only after explicit instruction to paste plan body as the literal `message` parameter.

## Cause

The agent's mental model conflates "what I'm describing" with "the message content." In SendMessage:
- `message`: literal content delivered to the recipient
- `summary`: one-line preview shown in UI notifications, NOT delivered as readable content

Without explicit framing in the brief, agents put the description-of-what-they're-sending in `summary` (which "feels" like the right home for "here's what this is") and leave `message` empty or with meta-pointer text. The discrete-inbox-entry model is non-obvious to fresh-context dispatchees.

## Fix

Brief-quality discipline — every plan-first dispatch brief includes literal SendMessage parameter framing:

> Your reply must use SendMessage with `message=<full plan body inline>` and `summary=<one-line preview>`. The `message` parameter is what I read; `summary` is metadata only (preview shown in UI, not delivered).

First-failure correction phrasing:

> I do NOT have the plan body. Messages between teammates arrive as discrete inbox entries, not a shared scrollback. Paste the full plan body as the literal `message` parameter.

**Two-failure threshold**: if two clarifications don't unstick the delivery format, bubble to HO before round 3. Pattern resembles sticky-belief drift; same compaction-resilient remediation applies (fresh context, explicit literal-tool-call template in brief).

**Don't burn REBOUND budget** on delivery-format failures — it's tool-usage misunderstanding, not plan-content rebound.

## Discovered

- 2026-05-07 in b16 cycle. Implementer sent two consecutive SendMessages where `message` body contained only summary-class meta-description while claiming plan was inline. First clarification didn't unstick; second pass succeeded only after explicit literal-tool-call framing.
- Different shape from b15 sticky-belief on Q3 inversion (content drift), but same orchestration principle: explicit operational framing > inheritance-by-reference, especially around tool-call mechanics that look obvious to the dispatcher.

## Pairs with

- `bugs/team-sendmessage-system-name-not-personal-name.md` — adjacent SendMessage gotcha (routing).
- `bugs/teamcreate-orchestrator-restricted-tool-inventory.md` — adjacent dispatch-pattern context.
- `incidents/2026-05-04-b9-team-pattern-first-run.md` — origin retrospective for team-pattern dispatch shape.

## Keywords

pantheon, sendmessage, message body, summary preview, plan-first, dispatch brief, parameter framing, body vs summary, tool-call mechanics, b16, sticky belief, fresh context redispatch
