# End-of-work notes consolidation

**Status:** Ratified 2026-07-11 (the operator, TG 5622: "make sure to update all information into notes! I think that'd be a good end of work sop"). Drafted same-day by HO per ORG-012 (HO drafts SOPs). The 07-11 SentientHome visit-2 close-out is the reference execution.

## What this is

At the end of any work arc — a live-support session, a deploy, a client visit, a feature push, a negotiation-adjacent day — every durable fact produced by the arc gets written into its home surface before the arc is declared closed. "In my head," "in the chat scrollback," and "in the session transcript" are not homes: sessions restart, scrollback archives, transcripts compact. The test is: **could a fresh session (or Recovery, or the operator cold) reconstruct today's state from the repos alone?**

## When it fires

- An arc reaches its natural close (deploy wrapped, tests green, meeting ended, clock stopped).
- The operator signals stand-down ("we're good for today", "stop the clock", topic parked).
- Session end / HANDOFF refresh for any session that did substantive work.
- Before switching gears to a different project mid-session (close the notes of the old arc first — the gear-switch is a mini session-end).

## The checklist (per surface, skip what genuinely didn't change)

1. **Project status doc** (e.g. `sentient-internal/STATUS.md`): done/in-flight/remaining moved to reflect reality; side-notes from conversations captured (client mentions, parked ideas).
2. **Hours / billing ledger** (client work only): engaged block logged with start/stop, rounding rule applied, invoiced-vs-accrued kept distinct. Clock-stop time comes from the operator; start time confirmed, not assumed.
3. **HANDOFF.md**: current state, pending tails with WHO owns each, staged-but-not-executed work named with exact paths/commands, gotchas learned today (they are cheapest to write while fresh). **And PRUNE while you're there** (T-1771): compress completed-arc sections already safely landed in git/ledger/tickets to one-line pointers — HANDOFF is the largest per-session auto-read surface and grows within-session; removing landed detail is as load-bearing as adding fresh detail (Specialist's first application cut 28%).
   - **The threshold is 45,000 bytes** (T-2166). "Periodically prune" was unfalsifiable, so here is the number: `stat -c %s ~/HANDOFF.md`. Past it, a single Read truncates and the session follows a handoff it only partly received. Measured cost: 108,540 bytes delivered as 158 of 466 lines — 66% lost, including the entire durable-pointer section. `tools/hooks/handoff-auto-read.sh` now warns at this threshold before the read, but the warning is a backstop for a prune you should have already done. (Guard in bytes; the cap's exact char-vs-byte semantics are unsettled, so 45,000 is deliberately conservative — and on dense prose with em-dashes and emoji, bytes overstate characters by ~1.2%, which is why hand-trimming to a character target undershoots.)
   - **Order durable-first, archive last** — this is the half that matters even at 9KB. Newest-first is right for recency but puts the PERMANENT content in the tail, and truncation only ever eats the tail: recency-order and durability-order conflict. Put KEY POINTERS / standing gotchas at the top and the one-line pointer archive at the bottom, explicitly marked as the prune surface. This also decides what survives a READER who skims, so it pays independently of the cap and independently of file size.
4. **Ledger** (`pantheon-config/ledger/`): arc entry with rationale + justifies_via; append→commit→push per the standing order.
5. **Tickets**: executed tickets resolved (not left in inbox), partially-done tickets annotated with what actually happened, new owed items filed (a promise made in chat IS an owed item — file it before it evaporates), gates expressed in frontmatter per ORG-019.
6. **Reusable gotchas** → `bugs/` / `lessons/` / memory pins per ORG-012 tiering. One-liner test: "will some future session pay this cost again if I don't write it?"
7. **Reference docs**: creds/URLs/part numbers/procedures that surfaced today land in the project's reference surface (e.g. SITES-AND-NETWORKS, procedures/), never only in chat.

## Discipline notes

- Write-as-you-go beats batch-at-end: the reference execution updated STATUS/ledger/tickets DURING the arc, so "end of work" was a 5-minute sweep, not an hour of reconstruction. Batch-at-end is the fallback, not the pattern.
- The sweep is also the moment the keep-working queue check fires (see `ho-absent-operator-keep-working.md`) — notes-close and next-item-pull are the same end-of-arc reflex.
- Client-facing surfaces get the sanitize-to-client export step (SentientHome two-repo rule); this SOP covers the internal write-down, not the client export.
