# HO-absent-operator-keep-working

**Status:** Ratified 2026-05-11 alongside ORG-011 (the operator msg_1305 + msg_1307 — all open questions closed; cadence 15min, await-state phrases OK but Recovery's judgement supersedes, orchestrator-only initially per ORG-011 final ratification). Authoritative for operator/HO citation. Authored per msg_1296 + msg_1298 — see ADR ORG-011 §SOP layer for the explicit pairing.

This is the prescriptive operational standard for HO behavior when the operator is absent for an extended period. The companion architecture (Recovery-side activity-monitor with two-predicate fire) lives in [ADR ORG-011](../../adr/ORG-011-stays-working-under-silence.md); this SOP is the role-defining operational sequence HO operates under regardless of whether the architectural monitor has fired.

## When to use this

Apply continuously during HO's awake-state. The SOP enforces an invariant: **HO is idle-eligible only when EITHER (a) the queue is empty AND no operator-shelving is pending, OR (b) every remaining queue item genuinely needs the operator's explicit input** (not "easier if he weighed in" — actually blocked on his decision-power per the autonomy bar).

Concretely fires whenever:

- HO finishes a turn or completes an item AND the next move is about to land.
- HO is about to send a Telegram message that contains await-state language ("waiting on", "pausing on", "ready when you", "no further chain-dispatch until", "deferring to you", "awaiting your") AND the operator's last inbound is >15min ago.
- The activity-monitor (ORG-011 Path 1.b) fires a tmux-poke into HO's input.

## Procedure

### 1. End-of-turn queue check (the smell test)

Before declaring a turn complete, run the queue check:

a. List all HO-target tickets across `tickets/inbox/` + `tickets/active/`.
b. Filter out: (i) tickets where target is not HO, (ii) tickets gated by frontmatter — an unreached `not_before:` calendar gate or an unresolved `depends_on:` dep (ORG-019 retired the `blocked/` dir; blocks are frontmatter-only now), (iii) tickets whose body explicitly names "needs the operator's <X>" where X is a decision only the operator can make (autonomy bar test: would HO be within auto-approve scope to decide X? If yes, the ticket is NOT operator-gated for this filter).
c. If the filtered set is non-empty: that's the next item. Pull it. Do NOT declare idle.
d. Also check: any BACKLOG item whose "Pending" / "What ships" list contains an HO-autonomous step that hasn't been done? Pull it.

### 2. operator-absent shelve-and-swap

If during normal work HO hits a blocker that requires the operator's input (e.g., ratification of a design choice, decision on a tradeoff that exceeds the autonomy bar):

a. **Record progress** on the blocker:
   - If the blocker is a ticket: update its `## Transition log` with a one-line entry naming what was reached + the question for the operator + ledger entry id (if applicable).
   - If the blocker is mid-implementation: commit work-in-progress to a feat branch + a short notes/ scratch file naming the resume-point. Do not lose mental context.
   - If the blocker is mid-cycle (team dispatch in flight, etc.): note in the orchestrator's brief / TaskList that the cycle is paused-pending-operator-input.

b. **Shelve the item** structurally:
   - For tickets: a ticket blocked on the operator's decision is `retarget`ed to his lane — `ticket.py retarget <id> operator` (record the question in the transition log first). For a calendar/dep gate (not an operator-decision), `ticket.py block <id> --not-before <date>` or `--depends-on <T-NNNN>` sets the gate IN PLACE — ORG-019 retired the `blocked/` dir (no move; the auto-consumer + grooming honor the frontmatter gate).
   - For BACKLOG items: note in BACKLOG.md "Status:" line that the next step is operator-gated.
   - For in-flight implementer/orchestrator work: SendMessage to the worker with "pause; will resume after the operator ratifies <X>" (don't shut down the worker — they stay alive for the resume).

c. **Surface to the operator** via Telegram bridge:
   - One tight message naming what was done + what's awaiting his input + what the surrounding queue looks like (so he knows shelving exists, not silence).
   - Do NOT close the conversation with "let me know when you're back." That's the await-state language pattern.

d. **Swap to next completable** (the SOP's load-bearing step):
   - Re-run the queue check from step 1.
   - Pick the next HO-actionable item from the filtered set.
   - Work it through to completion or its own blocker.

e. **Repeat** until either:
   - The queue is empty AND no shelved-blockers are pending the operator.
   - Every remaining queue item genuinely needs the operator (the test is real operator-gating, not preference).

### 3. Terminal-state behavior

Only when step 2(e)'s terminal condition is genuinely reached: HO may emit a tight summary of the shelved-blockers + the empty-otherwise queue + go idle. The summary is NOT "I'm waiting" — it's an inventory of what's pending his attention.

Example terminal Telegram:

> Queue exhausted of HO-actionable items. Awaiting your input on: ADR-011 (cadence + signals), T-0203 (sub-spec). Shelved (retargeted to your lane / dep-gated): T-0220 (Adaptive-Finish prototype, depends_on ADR-011). No further dispatchable work without your call on those. Will resume when you engage.

Example NON-terminal (anti-pattern Telegram, which msg_1289 was):

> No further chain-dispatch from me until you engage.

The difference: terminal names the specific items + the shelving state; anti-pattern is a vague gesture toward inaction.

## DECISIONS (SOP-tier)

- **End-of-turn queue check is mandatory, not best-effort** — justifies via msg_1296 ("you should be working on things the entire time, until either the backlog is empty, or every remaining item needs my explicit input"); the rule is unconditional within its threshold.
- **Block-via-ticket.py over inline note** — justifies via D9 (`depends_on`/`not_before` gates are set in the ticket frontmatter — ORG-019; auto-consumer + grooming digest both honor the frontmatter gate; inline notes don't propagate to the org-wide tooling).
- **"Pull next completable" defined by autonomy-bar filter, not by HO preference** — justifies via D8 + msg_1271 calibrate-higher (the bar for "is this operator-gated?" is the auto-approve scope at `rulebook/policies/trust-posture.md`; HO doesn't get to shelve items that are actually within autonomy).
- **Terminal Telegram inventories the shelved set + the queue, not a vague await** — justifies via D9 (categorical answer + format + timing) + D5 (literal state, not paraphrase) + msg_1296 (the await-state pattern is what we're explicitly retiring).
- **SOP cited by name in the activity-monitor's P1.b poke prompt** — justifies via D7 (don't rely on inheritance-by-reference; the poke lands HO directly into this document by path).

## Cross-references

- **ADR ORG-011** (revised 2026-05-11) — the architecture this SOP pairs with.
- **`rulebook/policies/trust-posture.md`** — the autonomy bar that defines "genuinely needs the operator's input."
- **`feedback_drift_to_passive_smell_test.md`** (memory pin) — the prose discipline that fires the queue-check on every turn while ORG-011 Path 1 is in flight.
- **D8 (HO self-discipline)** — the directive this SOP operationalizes.
- **D1 (the operator's bandwidth)** — the WHY: idle HO blocks the entire org; the SOP keeps the chain absorbing variance instead of routing it all upward.
- **Lessons:** [`lessons/structural-enforcement-retires-the-prose-rule.md`](../../lessons/structural-enforcement-retires-the-prose-rule.md) (frames the prose-vs-structure layering); [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (the antecedent the SOP layers against).
- **Telegram refs:** msg_1289 (anti-pattern instance — "no further chain-dispatch from me until you engage"), msg_1294 (the operator's catch), msg_1296 (the rule), msg_1298 (the SOP-not-memory framing correction).
