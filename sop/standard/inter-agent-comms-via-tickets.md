# SOP — Inter-agent comms via ticket-target-flipping

**Status:** **Ratified 2026-06-07 by the operator.** Promoted to `sop/standard/` 2026-06-05 by Specialist (T-0832, PR-1 from the T-0780 sweep); Authored 2026-05-10 by Orchestrator (Orchestrator) per T-0180 + the operator msg_899; staged at `notes/staged-sop-inter-agent-comms-via-tickets.md` pending the T-0160 `sop/standard/` folder spin-up, which cleared ~25d ago — this promotion finishes the migration the original arc (T-0160 + T-0173) intended.
**Author:** Orchestrator (Orchestrator); promoted by Specialist.
**Date:** 2026-05-10 (authored); 2026-06-05 (promoted).
**Re-evaluate when:** the ticket-system schema changes substantively (e.g., a transition_log dedicated comms field gets added); when message-system replacements emerge (kino's PWA messaging surface may subsume some of this); when cross-account tooling shifts (Recovery's NOPASSWD-sudo shape; new agents added to the topology).

## Purpose

Pantheon agents (Orchestrator/HO, Recovery, Specialist, future) communicate primarily via Telegram (operator surface) + the ticket system (delayed structured queue). The ticket system supports more than file-once-act-once: it supports **back-and-forth conversation** through `target:` field flipping. This SOP codifies that pattern + the conventions that make it work cleanly.

Per the operator msg_899: *"The ticket system can also be used for communications. Back n forth could be done via swapping a ticket's assignment field. This should go in SOPs. Helps bridge the comms gap. Not only should orchestrator and you be able to file tickets for each other, but you should be able to more or less converse based on inbox reading timings."*

## When to use ticket-flipping vs file-new-ticket

- **File a fresh ticket** when: the topic is a discrete piece of work with a clear scope; multiple back-and-forth rounds aren't expected; the receiving agent will close the ticket after acting.
- **Flip target on an existing ticket** when: the topic is a multi-round exchange; the original ticket's body holds load-bearing context that shouldn't be re-stated; the conversation IS the work-product (status discussions, design exchanges, ratification rounds).

Examples this SOP covers:

- **HO files Recovery-target sub-ticket** (T-0184, T-0189) → Recovery implements + heads-up files (T-0186) → HO closes parent (T-0178). **Pattern: 1 work ticket + 1 heads-up ticket, 2 separate files.**
- **HO files Recovery-target ticket → Recovery replies via target-flip back to HO with a question → HO answers via target-flip back to Recovery → Recovery implements → HO closes.** Pattern: 1 ticket, 4 lifecycle transitions, target flipped 3 times. SOP-codified below.

Both patterns are valid; pick the one that matches the conversation shape.

## How to flip target — mechanics

1. **Edit the frontmatter `target:` field** of the ticket file directly (`tools/ticket.py` doesn't currently expose a `--target` flag for the lifecycle subcommands; direct edit is the path).
2. **Append a transition log entry** with the actor + timestamp + 1-line context: `2026-05-10T06:30:00Z [orchestrator → recovery] target flipped per <reason>`. The arrow notation makes the conversational shape readable.
3. **Append the message body** as a section at the bottom of the ticket. Use a heading like `## <Date> — <actor> — <one-line>`. Body is plain markdown; code-blocks fine; cross-refs to other tickets / commits fine.
4. **Move ticket to `inbox/`** if currently in `active/` so the receiving agent's inbox-consumer pulls it. (Use `tools/ticket.py block` or direct file move; lifecycle conventions to be tightened in a follow-up if needed.)
5. **Commit + push** the change. The receiving agent's inbox-consumer pulls within ~5 min via T-0118 / T-0120 pre-fire pull.

## Reading timing — the conversational cadence

Per the operator msg_899: *"converse based on inbox reading timings."*

- **Each agent has its own inbox-consumer timer** (Orchestrator/Recovery/Specialist all run `pantheon-inbox-consumer@<agent>.timer` per T-0102 rollout).
- **Default cadence**: 5 min. So a target-flip round-trip is ~5-10 min wall-clock minimum (one consumer cycle each direction).
- **Faster turn-around** is possible via cross-account tmux-inject if urgent (Recovery has NOPASSWD-sudo into Orchestrator + Specialist per T-0184 / T-0189 lineage); but that breaks the structural async-comms shape and should stay rare.
- **Slower OK** too: if an agent's busy on another arc, the ticket waits in their inbox until they get to it. The deferred-with-event-trigger discipline (`lessons/defer-with-trigger-pattern.md`) applies — `interactive-required` label parks the ticket until ready.

## Conversational shape — recommended pattern

For a multi-round HO ↔ Recovery exchange:

```
T-0XXX
  Initial filing — HO authors body, target=role:head_system_orchestrator, state=inbox
  → Recovery inbox-consumer pulls
  
  Recovery's reply — append "## <date> — recovery — <topic>" section,
  flip target=role:head_engineering_orchestrator, move state=inbox
  → HO inbox-consumer pulls
  
  HO's reply — append "## <date> — orchestrator — <topic>" section,
  flip target=role:head_system_orchestrator, move state=inbox
  → Recovery inbox-consumer pulls
  
  Resolution — Recovery acts (or files closing entry), state=done
```

Each turn appends; the ticket file becomes the conversation transcript. Audit-chain intact via the transition log.

## Conventions

- **Don't delete prior content** — the ticket is append-only by convention (mirrors `rulebook/directives.md` D2 hand-curated discipline; mirrors ledger append-only). If a prior message is wrong, append a correction; don't edit-in-place.
- **Use `## <date> — <actor> — <topic>` headings** for conversation sections so a markdown viewer renders the thread cleanly + grep-able by actor / topic / date.
- **Cross-references with relative-link discipline** — `[other ticket](../inbox/T-NNNN.md)` or absolute `tickets/done/T-NNNN.md` — same as transition-log conventions.
- **Reserve the `## Ask` body for the original work-spec.** Conversational rounds go BELOW Ask + Context + Transition log (or in a dedicated `## Conversation` section if rounds are heavy enough to deserve their own section).
- **Don't flip target for one-line "ack"** — those go in a Telegram nudge or a follow-up ticket's transition log, not a fresh round on an existing ticket. Flipping for one-line responses inflates ticket churn.
- **Resolve when the conversation reaches a decision** — both parties agree the work-spec is now actionable, OR the conversation determines no action is needed. Resolve via `tools/ticket.py resolve` with the disposition body summarizing the chain.

## When this SOP doesn't apply

- **Telegram-direct conversations with the operator** — via each agent's own per-agent Telegram self-poll surface (the shared bridge was decommissioned 2026-06-04, T-0767; kino's PWA chat remains a possible future home). Don't flip ticket targets at the operator for general comms (the operator's bandwidth is the constraint per D1; tickets are batch-comprehensible, Telegram is real-time).
- **Inside-team SendMessage** (within a session's implicit agent team — Agent-name spawns, ORG-036) — that's the intra-team comms surface (project-orchestrator ↔ implementer ↔ reviewer); ticket-flipping is the *inter-agent* (across separate Claude Code sessions / users) comms surface. Different mechanism for different scope.
- **One-shot HO-files-Recovery ticket where no reply is expected** — that's just a normal Recovery-target filing per the dispatch pattern. No flipping needed.
- **Synchronous urgent comms** — file the ticket if needed for audit chain, but if response time matters, Telegram-nudge the operator out-of-band rather than wait for inbox-consumer cadence.

## Cross-references

- **Principles + directives**: P4 (traceability — ticket-thread becomes the audit chain), P6 (operator comprehension — readable conversation shape), D1 (the operator's bandwidth — agent-to-agent comms via tickets reduces operator-routing burden), D9 (orchestrator response contract — categorical-answer-format-timing applies to ticket replies too), D10 (context-frugality — ticket transcript IS the context, no parallel docs).
- **ADRs**: ORG-003 (ticket system substrate); ORG-006 (kino PWA — long-term home for some inter-agent comms; some load-bearing comms stay structurally on tickets even post-kino).
- **Lessons**: [`defer-with-trigger-pattern.md`](../../lessons/defer-with-trigger-pattern.md) (sister; ticket-comms inherits the defer-with-trigger discipline; `interactive-required` parks a thread mid-conversation when timing demands), [`org-churn-is-constant-methodology-must-be-concise.md`](../../lessons/org-churn-is-constant-methodology-must-be-concise.md) (cousin; tickets-as-transcripts are minimum-traversal-to-precise-info compared to scattered Telegram threads).
- **Tickets** (founding usage examples): T-0184 (HO → Recovery dispatch; non-flipping pattern — separate heads-up ticket T-0186 closed the loop), T-0189 (HO → Recovery dispatch — same shape), T-0133 (HO → the operator surfacing — target=operator was the conversational surface; the operator's response landed via Telegram + transition-log update; mixed pattern), T-0173 (HO ↔ the operator ratification round; ticket-target stayed orchestrator, ratification happened via Telegram + transition log — alternative to flipping for ratification rounds).
- **Memory pins**: `project_pantheon_account_topology.md` (the three-distinct-unix-user shape that makes inter-agent comms a real concern; without per-agent identity the ticket-target-flip wouldn't disambiguate clearly).
- **Telegram refs**: msg_899 (the operator's articulation of this pattern as SOP-worthy), msg_1169 (autonomy expansion + "ticket system as delayed messaging system" framing — direct-ratification of this pattern's scope).
