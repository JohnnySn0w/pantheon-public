# Ticket walk — the operator's desk-clear + review

**Status:** Drafted 2026-08-09 by Orchestrator (HO) per the operator (TG 7464 "we need to make a small sop for this kind of thing… Desk-clear + review basically" + TG 7469 defining the flow). Amended 2026-08-09 (TG 7487: no self-pickup mid-walk + 30-min AFK decay). Autonomous under msg-3733 (below-goal-level operational discipline; codifies an operator-defined procedure). The operator override-available.

**When:** The operator says **"let's walk tickets"** (or any clear equivalent — "ticket walk", "let's go through my tickets"). This is a joint interactive session to triage and clear his personal decision queue.

## The flow (operator-defined, TG 7469)

1. **HO clears its own desk first — via handoff.** Before the walk, offload in-flight work so HO can give the walk full attention:
   - Active build/design arcs → hand to a sub-orchestrator (`retarget` the ticket + a mailbox brief; see [`retask-dispatch.md`](retask-dispatch.md)). Pick the FREE seat (check both seats' active tickets; WIP cap serializes per persona).
   - Design threads that only HO+the operator can carry → park (annotate + leave in inbox).
   - State the cleared desk to the operator in one line ("kino + CI are with Mason, VSM parked") so he knows the board is clear.
   - "To whatever degree you can" — hand off what's delegable; keep what genuinely can't move.
   - **A sent handoff is not a landed handoff.** Consume-plane seats (mason/artisan) do NOT reliably self-drain their mailbox — the consume-runtime wake fires but WAKE ≠ DRAIN, and `mailbox-wake@<seat>` may be disabled, so a `retarget` + mailbox brief can sit unread indefinitely while the seat idles believing nothing is owed (T-2248 stale-premise class; observed 2026-08-09, mason sat on 3 undrained handoffs ~60min). Confirm the seat actually PICKS UP the work — watch for its ack, or ask Recovery to send-keys a wake into its pane (`suborch1-main:0` / `suborch2-main:0`). Do not report the desk "cleared" to the operator on the strength of a sent message alone.

2. **Walk his queue oldest-first, one at a time.** Scope is **`target=operator` tickets only** (his assigned queue — NOT the whole backlog; TG 7468). Pull inbox+active `target=operator`, sort ascending by ID (= oldest first). Present **one at a time**; do not batch, do not jump ahead.

3. **For each ticket, HO surfaces the RESERVED DECISION — HO does the triage legwork.** Read the ticket AND its transition log; identify *why it is the operator's* — the specific decision or input reserved to him. Present concisely:
   - what it is (one line),
   - **why it's on his desk** (the specific reserved decision — a fork, a ratify, a personal input, an irreversible/external call),
   - HO's **recommendation** with the trade-off.
   - **Do NOT reflexively reassign.** A `target=operator` ticket is on his desk *for a reason* — check the retarget note before recommending it go elsewhere. (Lesson T-1213, 2026-08-09: HO tried to punt an Echo ticket to a seat as "no decision needed"; the operator: "it was retargeted to me for a reason." The real reserved decision was a design fork HO had to surface, not dodge.)

4. **the operator decides; HO executes immediately + records on the ticket, then next.** Per his call: act / close / defer / **retarget to the real owner** / ratify. Annotate the decision on the ticket (ticket-lifecycle ops go direct-to-main). Then present the next oldest.

## Walk discipline (the operator, TG 7487)

- **HO does not pick up new work of its own mid-walk.** Once a walk is underway HO stays *on the walk* — it does not pull its own next ticket, kick off a new arc, or backfill the desk it just cleared in Step 1. Executing a decision the operator makes *during* the walk (e.g. dispatching the owner of a ticket he just retargeted) is part of the walk; starting unrelated HO-initiated work is not. Cleared, and kept clear, until the walk closes.
- **30-minute AFK decay → HO may close the walk itself.** If the operator goes **≥30 minutes without responding** during an active walk, treat him as AFK rather than deliberating: HO may close the walk on its own. On close, record where it stopped — the last-presented ticket and the next-oldest still queued — so the next walk resumes cleanly oldest-first. No need to hold the session open waiting on a reply that isn't coming; the walk is resumable by design.
- **No pause / wind-down prompts — the operator paces the walk.** HO presents oldest-first and, after each of the operator's decisions, presents the NEXT ticket — it does **not** ask "keep going or pause here?" / "next, or wind down?". The stop is the operator's to call, not a decision HO inserts; prompting him to quit is the passive-drift anti-pattern ([[feedback_drift_to_passive_smell_test]]) and reads as HO trying to end the session. (2026-08-09, TG 7516: the operator flagged an HO pause-prompt — "why are you implying a pause now rather than earlier?" — HO committed to drop them, then did it again one message later; hence this is written into the SOP, not left to remember.) The 30-min AFK decay above is the *only* HO-initiated end of a walk; short of that, keep presenting until the operator says stop.

## Fast-track vs surface (efficiency)

Most `target=operator` tickets carry a genuine reserved decision — surface it. But some are HO-owned or properly-gated maintenance that merely slipped into visibility (e.g. a `not_before`-gated quarterly re-eval): one-line **"handled/gated, moving on."** The discriminator is Step 3's question — *is there a decision only the operator can make here?* If no, fast-track; if yes, surface it with a recommendation. Never fast-track by assuming mis-parking without checking.

## What HO produces from a walk

- Each ticket left in a decided state (acted / closed / deferred / retargeted / ratified), decision recorded on the ticket.
- A brief digest is optional — the per-ticket annotations are the durable trail (P4).
- HO's desk restored to its normal arcs afterward (re-claim / resume handed-off work as the seats report back).
