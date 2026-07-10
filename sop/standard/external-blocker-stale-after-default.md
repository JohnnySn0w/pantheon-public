# External-blocker tickets — default to `stale_after: 7d`

## When to use this

When filing or claiming a ticket whose acceptance is gated on an external
party's response (vendor maintainer, Anthropic support, GitHub upstream PR
reviewer, third-party API team, etc.).

External in this SOP means *outside the Pantheon org*. Sibling-agent
dependencies inside Pantheon (Recovery → Orchestrator, Orchestrator → Specialist, etc.) are
internal and use `depends_on:` chains, not this discipline.

## Procedure

### 1. Set explicit `stale_after` in front-matter

Default: 7 days. Tighter if the external party has a stated response
window (e.g., a vendor SLA promising 24h triage → 24h or 48h).

```
---
id: T-NNNN
stale_after: 7d
labels: [..., external-blocker]
---
```

The `external-blocker` label is conventional — surfaces in grooming digest
filters + makes the staleness expectation visible at a glance.

### 2. Note the blocker in the ticket body

Single line under the Ask:

> **Blocker:** awaiting <party> on <topic>; expected window <N days> per
> <source — vendor announcement / public roadmap / Telegram conversation>.

So future-readers (including the grooming digest, the agent that picks it
up, and the operator) know why staleness is tight.

### 3. On grooming-cron stale fire

`tools/ticket_grooming.py` flags tickets past their `stale_after` and
surfaces in the daily Telegram digest. When a stale-flag fires on an
external-blocker ticket:

- HO (or Recovery, for Recovery-owned tickets) checks whether the external response landed.
- **Response landed**: unblock + close OR file the verification sibling +
  close the parent. Standard close-out hygiene applies.
- **Response NOT yet landed but expected soon**: extend `stale_after` by
  the marginal time + append a transition-log line explaining why.
- **Response NOT yet landed and timeline slipping**: escalate to the operator
  for re-prioritization (drop the ticket, switch to a workaround,
  reach out manually, etc.).

### 4. Multiple-external-blocker chains

If a ticket has multiple external dependencies (e.g., j17 maintainer
response → Anthropic SDK behavior → kino-pwa upstream library), the
staleness is the SHORTEST of the expected windows, not the longest. The
shortest is the first opportunity to re-check + re-decide.

## Common failure modes

- **Inherited default of 30d or 60d from "feels long-running" ticket templates.**
  Today's precedent: T-0131 (60d) and T-0133 (30d) on j17 maintainer
  responses; both should have been 7d. Maintainer responded in ~2 days;
  both tickets sat post-acceptance-met for ~3-5 additional days because
  grooming never flagged them. Catch: always set `stale_after:` explicitly
  on external-blocker tickets; treat the absent-override case as a smell.
- **Stale-flag fires but no follow-through.** Grooming digest surfaces the
  flag, but no human or agent acts on it. The flag becomes noise.
  Mitigation: HO smell-test rotation includes "any external-blocker stale
  flags? if yes, act this cycle." Recovery grooming-cron's idempotency guard
  (no repeat append on re-fire) prevents log spam, but human inaction
  needs its own discipline beat.
- **External-blocker re-categorized internally without staleness update.**
  E.g., j17 maintainer fix lands → adapter-side verification work spawns →
  the new ticket inherits the parent's long stale_after. Catch: each new
  sibling/child ticket sets its own `stale_after` per its own blocker type
  (verification is internal → no special tight staleness needed; original
  external-blocker close-out frees the chain).

## Boundary

- Internal Pantheon agent-to-agent dependencies: not external. Use
  `depends_on:` chain instead. Grooming default (4h active
  per `tools/ticket_grooming.py`) is appropriate; `blocked/` state is
  retired per ORG-019 (2026-05-19).
- Calendar-blocker tickets ("re-evaluate 2026-06-01"): not external.
  Use `not_before:` field if available, OR set `stale_after:` to a date
  appropriate to the calendar window.
- Long-running implementation arcs that touch external libraries but
  aren't gated on a specific external response: not external in this
  sense. Use cycle-shape conventions.

## Cross-references

- T-0131 / T-0133 — the precedents that surfaced this gap. Both closed
  2026-05-13 with verification scope handed to T-0185; could have been
  closed 2026-05-10 if `stale_after: 7d` had been in place.
- `tools/ticket_grooming.py` — the cron that enforces this. Already
  supports per-ticket `stale_after:` front-matter override; this SOP
  codifies the convention for setting it.
- `sop/standard/auto-file-side-observations.md` — sibling discipline for
  filing tickets at all; this SOP is the "set the right staleness" pair.

---

**Status:** Drafted 2026-05-13 by Recovery per the operator msg_1209 + msg_1211
(ratifying Recovery msg_1210 diagnosis on T-0131 / T-0133 staleness mistakes).
Awaiting the operator ratification.
