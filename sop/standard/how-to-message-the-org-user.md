# How to message the org user (the operator)

## When to use this

Every Telegram or PWA report from a direct-to-operator Pantheon agent (Orchestrator / Recovery / Specialist) to the org user. Project orchestrators and workers **do not** message the operator directly — they route through their dispatching orchestrator per `rulebook/policies/trust-posture.md` §What-is-never-permitted ("subagent → user direct communication"); when a project-orchestrator's surface needs to reach the operator, it does so via HO. This SOP governs the direct-channel agents (Orchestrator/Recovery/Specialist); workers + project-orchs follow it transitively via their orchestrator's outbound message.

Applies whenever a direct-to-operator agent is the sender; does not apply to in-tool prose (commit messages, ledger entries, ticket bodies, ADRs) which serve a different audience.

## Procedure

### Default mode — tight ack

The operator's framing of his Pantheon role (msg_1048): three things — **tickets go in, may be asked on certain specifics, product comes out**. Default-mode messages are about state-change confirmation, not narration. Strip everything that isn't load-bearing for "did the ticket move?"

| Event | Target message shape |
|---|---|
| Ticket filed on the operator's request | `Filed T-NNNN — <one-line title>.` |
| Ticket resolved (you/HO closed it) | `T-NNNN done.` + one-line operational impact if non-obvious |
| Multiple tickets filed together | Bulleted list: `T-NNNN — <3-5 word title>` per line |
| Incident handled silently | `<one-line what happened> → <one-line action taken>.` |
| Status check (the operator asked "what's up?") | 1-3 lines, user-facing level not implementation tier |
| Auto-resolve fired | `<one-line what was resolved>.` (post-T-0142 / T-0201) |

### Exception mode — calling a shot

When the operator is being asked to make a decision, include enough technical context to support the call — but only enough. The goal is "the operator can decide" not "the operator understands the implementation" and not "the operator has to look up 5 tickets and 4 ADRs to parse the ask."

- State the decision clearly: "Pick (a) X or (b) Y?"
- Trade-offs in one line per option.
- Recommend a pick if you have one.
- Code/file-level detail is appropriate if the call is code/file-level.
- Stop there. Don't narrate the journey.
- **De-reference ticket IDs, ADR numbers, and methodology-jargon into plain-language framing.** IDs (T-NNNN) go at the END as pointers ("Full detail at `<path>`"), not as main-content references. ADR numbers (ORG-NNN) belong inside the ticket body/commit message, not the chat surface. Methodology terminology ("three-lens review", "H0–H7 probes", named-lens jargon) belongs in the ticket / SOP, not the chat. Per the operator TG 8851 (2026-08-31) — even within a live thread where the operator already engaged the frame, ID-heavy referencing forces a context-switch per token.

### Never include in default messages

- Test counts (`145+58 tests green`).
- Commit SHAs (unless he's asked to merge or you're handing off a branch).
- DECISIONS section recaps from commit messages.
- File-by-file change lists.
- Cycle metrics (REBOUND counts, wall-clock min, scope_loc_delta).
- Implementation tracebacks / stack-trace excerpts.
- Architectural justifications for routine work.

If the technical depth would be useful to YOU as the agent for the audit chain, put it in the ticket / commit / ledger — not in the chat message.

## Common failure modes

- **Wall-of-text completion notice.** "T-NNNN done. <wall of tests, commit hash, DECISIONS recap, files changed>" — phone-hostile, signal buried under noise. Strip to "T-NNNN done." + one-line impact at most.
- **Pre-emptive justification.** "I picked option B because P5 + D6 + ..." when the operator didn't ask why. Justification belongs in the commit message / DECISIONS section; the chat just needs the outcome.
- **Batch enumeration over-detail.** Listing 5 tickets with full asks. Reduce to 5 lines of `T-NNNN — title` per item.
- **Calling-a-shot disguised as a status update.** If you're actually asking the operator to decide, FRAME it as a decision ("Pick (a) or (b)?"), don't bury the ask inside a status report.
- **Apology + technical narrative.** "Sorry, the build broke because <traceback>." Skip the apology, skip the traceback. State the user-facing impact + your action: "PWA tickets pane was blank; rebuilt + redeployed. Live now."
- **ID-heavy or jargon-heavy exception-mode ask.** When surfacing a decision, sentences dense with ticket IDs (T-NNNN cross-refs), ADR numbers (ORG-NNN), or methodology names ("three-lens review", "H0-H7 probes") force the operator to context-switch per token. De-reference into plain framing; leave IDs as end-pointers ("Full detail at `<path>`"). Per the operator TG 8851 (2026-08-31): "this is very jargon heavy. Is there a way to de-reference the id work and say it more plainly/broad stroke?" Bad: "Filed T-3349 (ratify ask) + T-3350 (impl gated) for Cairn's T-2450 proposal against ORG-057/ORG-061; three-lens review green." Good: "Cairn delivered a proposal for how a restored persona should prove it remembers the operator relationship correctly. Reviewed it three ways — all green. Asking for ratify or redirect. Full detail at `tickets/plans/T-2450-...md`."

## Boundary

- **Applies**: Telegram + PWA reports from agents to the org user (the operator primarily; future external users per T-0200 contracting-firm exploration follow the same shape).
- **Does NOT apply**: commit messages, ledger entries, ticket bodies, ADRs, lessons, incidents — those keep their full technical depth as audit-chain / future-agent context.
- **Does NOT apply**: conversation where the operator's driving exploration ("what could we do about X?") — match his tier, technical depth is welcome when he's inviting it.
- **Does NOT apply**: inbox-consumer or activity-monitor injection prompts (those go to agent panes, not the operator's phone).

## Cross-references

- `rulebook/directives.md` §D1 (bandwidth preservation) — this SOP is D1's chat-channel calibration.
- The operator directive msg_1048 (2026-05-11) — origin: "I really only need to know 3 things about the function of pantheon. Tickets go in, I may be asked on certain specifics, and then product comes out. ... I don't need messages to have technical details unless I'm being asked to call a shot."
- The operator directive msg_1051 (2026-05-11) — clarifier: "I think this should be an sop not a memory. 'how do i message operator or the org user'" — promoted from Recovery-memory tier to org-wide SOP tier.
- The operator directive msg_584/585 (2026-05-05) — earlier ratification of ticket-filing-summary discipline ("Please be more laconic, less explanatory ... when you're describing them to me in this channel"). This SOP extends that to completions, incidents, and status reports.
- `sop/standard/agent-role-map.md` — persona-name ↔ role-id mapping (informs who can speak directly to the operator per which role).

## Status

**Ratified 2026-05-11 by the operator (msg_1053).** Authored by Recovery at the operator's directive msg_1048 + msg_1051; ratified with one wording fix per msg_1053 (project orchestrators + workers don't message the operator direct — only Orchestrator/Recovery/Specialist do, via §When-to-use scope).
