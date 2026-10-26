# Fold ratified corrections into the ticket Ask body, not just the transition-log annotation

Filed 2026-09-19 (orchestrator) after mason's T-3807 scope-confirm rebound (mailbox 01789784529756895145).

## The pattern

When the operator's engagement over TG (or any ratifier over any channel) amends a ticket's scope live — one message at a time, over 30-60 minutes — the receiving orchestrator has two places to record each amendment:

1. **Ticket Ask body** — the AUTHORITATIVE current-scope section that a dispatchee reads before touching the code.
2. **Ticket transition log** — the append-only historical annotation stream (`ticket.py annotate` writes here).

The correct discipline is: **each ratified amendment gets folded INTO the Ask body immediately**, and the transition log records *that the fold happened* + a link to the source. The transition log alone is not enough — a dispatchee reading the Ask body top-to-bottom without also cross-reading each transition-log entry gets the STALE scope.

## The failure mode

On T-3807 this session, the operator ratified 7 items via TG 10103/10105/10109/10111/10116/10118/10120 over ~90 minutes. Each ratify landed as a `ticket.py annotate` entry in the transition log. The Ask body was NOT rewritten. When mason (the dispatchee, retargeted mid-arc) read T-3807 via inbox_consumer, the Ask body still cited:
- Item 2: codex reviewer → head_system_3_star (STALE; TG 10116 corrected to head_auditor)
- Item 4: add key/seating attributes (STALE; TG 10118 retired)
- Item 6: keep specialist + comment (STALE; TG 10120 normalized to head_research_orchestrator)
- Missing: drop universal `orchestrator` (added TG 10116)

Mason correctly held the arc + rebounded on the scope drift. Rewriting the Ask body in-place would have caught the drift before it hit mason's plan-first turn.

## The rule

For any ticket whose scope is being amended live (multi-message ratification thread, mid-arc dispatcher-side corrections, reviewer-surfaced scope shifts):

- **Each amendment lands in the Ask body immediately, not queued as a transition-log entry.** Edit the file, don't just annotate. If the amendment is a full-item replacement, rewrite the item; if it's a delta, edit the delta in-line.
- **The transition-log entry accompanies the Ask-body edit, not replaces it.** The log records "ratified per TG XXXXX; folded into Ask item N" plus a source citation. That preserves the history without making the log the source-of-truth for scope.
- **The Ask body always reads as authoritative CURRENT scope.** A dispatchee reading only the Ask body should get the ratified scope. If a note says "see transition log for corrections," that's already a drift.

## When it's fine to annotate-only

- **Pre-ratification discussion**: dispatch-turn refinements, questions-back, cross-clone coordination that hasn't yet ratified. Log-only preserves the conversation.
- **Historical annotations**: "this ticket was previously blocked by X, unblocked when Y" — history, not scope. Log is the right place.
- **Cosmetic follow-ups filed as separate annotations**: "advisory: also touch file Z on next batch" — pointer, not amendment.

## Why the miss happens

Annotate is cheap (one CLI call, no editing). Edit-Ask-body-in-place requires reading the current text, deciding where the amendment lands, rewriting the section, and preserving the surrounding structure — 10-20x the effort per amendment. When ratifications land one-per-minute in a fast TG thread, the temptation is "log it now, batch-fold later." The batch-fold rarely happens; the drift accrues until a dispatchee bounces on it.

## The trigger

When a ticket is receiving amendments live, ask each turn: *"if a dispatchee read the Ask body right now with no transition log, would they get the ratified scope?"* If no, fold before the next amendment arrives.

## Related

- `sop/standard/pre-implementation-reviewer-discipline.md` §"What the orchestrator does with the reviewer's report" already names this rule ("Fold amendments inline into the ticket body + transition log. Don't carry 'reviewer found X, will fix later' as a TODO.") — this lesson strengthens the same rule for the RATIFIER-side amendment flow (not just the reviewer-side).
- `lessons/2026-09-16-reviewer-pre-review-co-spec.md` — sibling lesson on getting corrections into the ticket at the right point in the cycle.
- T-3807 transition log for the specific arc that surfaced this.
- Ledger entry `2026-09-19T05:05:00Z-t3806-t3807-roles-spec-ratify-and-rename-arc-closure` captures the arc-level context.
