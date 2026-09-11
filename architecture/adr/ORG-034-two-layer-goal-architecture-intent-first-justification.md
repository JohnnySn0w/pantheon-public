---
aliases: [ORG-034]
id: ORG-034
title: "Two-layer goal architecture — arc-intents as the missing justification tier"
status: Adopted 2026-06-12 (operator-ratified live, Telegram msgs 3761–3767, "Make it so" msg 3767)
date: 2026-06-12
build_disposition: no-build-needed
---

# ORG-034 — Two-layer goal architecture: arc-intents as the missing justification tier

**Status:** Adopted 2026-06-12 (operator-ratified live: Telegram thread msgs 3761–3767, ratification "Make it so" msg 3767; design converged in-thread).
**Author:** Orchestrator (Orchestrator).
**Justifies via:** P3 (decisions justify upward — this fixes WHAT they justify to), P2 (principles-first alignment — re-grounded as intent-first), D1 (the operator reads why-it-matters, not process ids), P6 (the means/ends tension gets a named structure instead of silent capture).
**Re-evaluate:** 2026-09-12, OR when a goal-coverage audit shows the intent layer has itself become ceremony (intent lines going boilerplate), OR on next north-star change.

## Context — "is the tail wagging the dog?" (the operator, msg 3761)

The org's justification chain runs entirely on P/D ids: DECISIONS cite principles and directives, reviewers REBOUND on missing P/D cites, and the goals registry (`rulebook/goals.md`, itself only added 2026-06-11) sits beside the chain rather than inside it. Observed consequences: work framing drifts toward the operational layer (V6 research needed mid-arc re-steering onto the north stars); idle capacity defaults to process hygiene; agents optimize for process-compliance because **no goal-object exists at their working altitude to cite** — an implementer can't meaningfully cite "echo = a voice interface to plug anything into" for a downcast-arm ordering choice, so they cite D7.

The operator's diagnosis (msg 3764): **there are two types of goal at very different layers.** That is the root cause — the structure had layer one and the process layers, but no middle tier.

## Decision

### 1. Two named goal layers

- **North stars** (layer 1): terminal, few, slow-moving, **the operator's** — `rulebook/goals.md` North-star lines, verbatim his. Unchanged by this ADR.
- **Arc-intents** (layer 2): operational — what THIS arc/ticket/dispatch is for, in outcome language ("classifier failures surface distinctly in status surfaces", "installer never ships ahead of the daemon wire shape"). Derived, fast-moving, **agent-ownable at every tier**, each nested under a north star (directly, or via its parent arc's intent). Real-world template: commander's intent in mission command — every echelon states its own intent at its own altitude, nested under the higher one.

### 2. Intent-first justification (the chain inversion)

The justification chain becomes **ends-then-means**:

- **Work artifacts carry intent.** Every dispatch brief OPENS with its arc-intent line (one sentence: outcome + which north star or parent intent it serves). Tickets name what they serve in the Ask (a `serves:` frontmatter field is the structural follow-up — the existing `intent:` field is taken by action/ratify/noodle semantics).
- **DECISIONS cite intent first.** Material calls name the intent/goal served — or say **"goal-neutral maintenance"** honestly (hygiene, tooling, process work; legitimate, but named as such, never dressed up). P/D ids remain in DECISIONS as **the how** — methodology compliance — no longer carrying the why.
- **Conflict rule (the mission-command payoff):** when methodology and intent genuinely conflict mid-arc, intent binds and the deviation is FLAGGED (D6 handling unchanged) — P/Ds are the plan; intent is the mission. This is the licensed-deviation seam, not a methodology escape hatch: the flag is mandatory.
- **North stars get cited by ARCS, not tickets.** A ticket cites its arc-intent; the arc-intent cites the north star. Don't make implementers paste north stars into leaf DECISIONS — that's how the middle tier becomes ceremony.

### 3. The audit question changes

The standing review question (ledger reviews, weekly summaries, quarterly re-evals) gains: **"what fraction of this period's ledger served a named intent vs goal-neutral maintenance?"** — replacing artifact-throughline sweeps as the only health probe. No new tooling mandated now; the question is asked wherever ledger review already happens.

## Real-world grounding (per the operator's ask, msg 3763)

OKR systems (work traces to Objectives precisely because output/process metrics otherwise capture the org); mission command / Auftragstaktik (nested intent at every echelon — the strongest fit for autonomous agents at tiers); hoshin kanri (true-north cascade with alignment review; its known failure mode — lean cargo-culting, process theater without true north — is exactly the tail-wags-dog risk this ADR addresses). Common thread: **the goal is the first sentence of the work artifact, not a reference document beside it.**

## Implementation deltas (this ADR's commit + follow-ups)

1. `rulebook/directives.md` D7 — brief contract amended: briefs open with the arc-intent line; DECISIONS = intent-first + P/D-as-how. (This commit.)
2. `rulebook/goals.md` — header gains the two-layer note; north-star lines untouched. (This commit.)
3. `rulebook/README.md` — tier table updated (goals row was missing entirely; ratification-authority column updated to post-msg-3733 reality). (This commit.)
4. `pantheon-org-discipline` skill §2 (DECISIONS format) + §7 (plan shape) — intent-first wording. (claude-config, this arc.)
5. `~/CLAUDE.md` chain-of-justification section — one-line amendment. (claude-config, this arc.)
6. **Follow-up ticket:** `ticket.py` `serves:` frontmatter field + lint nudge (implementation-tier; filed at adoption).
7. Worker personas pick the format change up through the skill; no persona-file edits needed.

## What this is NOT

- Not a ceremony add: leaf tickets get ONE sentence; the "goal-neutral maintenance" escape is legitimate and expected for real fractions of work.
- Not a P/D demotion in authority: methodology still binds, reviewers still REBOUND on its violations — it just stops impersonating purpose.
- Not HO authoring north stars: layer 1 stays the operator's voice, per goals.md maintenance contract.
