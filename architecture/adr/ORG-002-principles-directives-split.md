---
aliases: [ORG-002]
title: "Two-tier framework: Principles + Directives"
status: Ratified 2026-04-28
date: 2026-04-28
# re_evaluate: machine-checkable mirror of this ADR's inline re-eval prose,
# consumed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk.
# type: calendar|metric|event ; fires: auto-ticket (deterministic only) | surface.
# Backfilled per T-2530 (inline-only trigger was previously uncaught).
re_evaluate:
  - id: R1
    type: event
    when: "worker reliability metrics support broader write access"
    fires: surface
    text: "old-P7 (hand-curated docs immutable to workers) was demoted to directive D2 because worker reliability is not validated for hand-curated content; re-evaluate when worker-reliability metrics support broader write access"
build_disposition: no-build-needed
---
# ORG-002 — Two-tier framework: Principles + Directives

**Status:** Ratified 2026-04-28 by the operator (Telegram).
**HITL:** Ratified.
**Date:** 2026-04-28.
**Justifies via:** Principle: Architecture over policy (was P2, now P1). Empirical: military doctrine (Auftragstaktik / Commander's Intent + ROE), Anthropic's own Core Views + Responsible Scaling Policy, Bridgewater Life vs Work Principles, Bungay's *Art of Action*, Ostrom's design-principles work.

## Context

ORG-001 established the ledger and a flat set of 9 principles (P1–P9). Two stress-test interactions on 2026-04-28 surfaced a structural problem:
1. Some "principles" (P1 preserve-bandwidth, P7 hand-curated-docs, P9 autonomy-bar) are operationally true *now* but obviously change as the org evolves. Calling them eternal "principles" sets up either silent edits (when reality moves and we quietly rewrite the principle) or stale text (when we don't).
2. Echo's "audience of one through phase 3" surfaced as a directive masquerading as a principle: time-bounded, lifecycle-scoped, with an explicit expiration trigger.

Research grounding (subagent sweep, 2026-04-28; ledger `2026-04-28T14:08:00Z-001`):
- Most well-run orgs use **two tiers minimum**: durable + scoped/operational. Even "flat" orgs (Amazon's 16) have an unwritten operational layer (interview rubrics).
- Convergence on **≤7-8 items in the durable tier** (Miller's 7±2; Ostrom's 8 design-principles ceiling for memorable governance; Benedict's Rule's 73 short chapters that distill to a small commitment set; Bridgewater's ~5 Life Principles).
- The scoped tier is **explicitly editable and dated** — military ROE (Rules of Engagement), legal regulation, monastic customary, **Anthropic's RSP**.
- "Values" as a tier is mostly theater (Lencioni HBR 2002; Basecamp/military skip; Netflix uses for hiring filters not decisions).
- "Appends" as a third tier doesn't appear in surviving frameworks. Additions go into the scoped tier.

The cleanest published precedent for our use case is **Anthropic's own pattern**: Core Views (durable) + Responsible Scaling Policy (scoped, dated, with re-evaluation triggers).

## Decision

### Adopt two tiers: **Principles + Directives.**

- **Principles** are durable, intended to outlive specific configurations of the org. Cap at ~7. Live in `rulebook/principles.md`.
- **Directives** are scoped, dated, and explicitly editable. Each names what it emanates from (a principle, or a named operational constraint), its scope, its effective date, and the trigger that prompts re-evaluation. Live in `rulebook/directives.md`.

Skip "Values" (theater). Skip "Appends" (a third tier the literature doesn't support). Skip a single "north star" (Bungay: only earns the tier when it actually narrows decisions; we don't have one that does).

### Renumber the surviving principles cleanly.

The migration cost of one rename is paid once; permanent gappy numbering would be paid forever. Old ledger entries (which reference the old numbering) are immutable per P4-old / P3-new (append-only) and remain valid in their original-numbering frame. The mapping table below is the canonical translation.

#### Mapping table

| Old | New | Status | Rationale |
|---|---|---|---|
| P1 — Preserve the operator's bandwidth | **D1** | Demoted to directive | Operationally true through phase 3; explicit expiration trigger ("workforce expands beyond the operator"). |
| P2 — Architecture over policy | **P1** | Renamed (durable) | Cited as "load-bearing for Echo's whole engineering posture." Stays principle. |
| P3 — Principles-first alignment | **P2** | Renamed (durable) | Sets the tone for everything else. Stays principle. |
| P4 — Decisions justify upward | **P3** | Renamed (durable) | Structural enforcement of alignment. Stays principle. |
| P5 — Traceability is non-negotiable | **P4** | Renamed (durable) | The audit chain is part of the value; stays principle. |
| P6 — Roles don't hop | **P5** | Renamed (durable) | Structural separation of responsibility. Stays principle. |
| P7 — Hand-curated docs immutable to workers | **D2** | Demoted to directive | Currently true given worker reliability isn't validated for hand-curated content. Re-evaluate when worker reliability metrics support broader write access. |
| P8 — Tensions preserved, not collapsed | **P6** | Renamed (durable) | Echo's core methodology, generalizes. Stays principle. |
| P9 — Autonomy bar | **D3** | Demoted to directive | The criteria themselves (ADR-worthy / principle-touching / etc.) are operational and will tighten/loosen as trust accumulates. |

After ORG-002: 6 durable principles (P1–P6) + 3 directives (D1–D3).

### Directive format

Every directive carries:
- **Emanates from:** principle id or named operational constraint.
- **Scope:** the conditions/configuration the directive is operationally true within.
- **Effective:** date the directive took force.
- **Re-evaluate:** the trigger condition that prompts revisiting.
- **The directive itself** (body).
- **How to apply** (operational guidance).

Re-evaluation is mandatory — directives that lack a re-evaluate trigger become invisible permanent rules, the failure mode this ADR exists to prevent.

### Worker behavior: justifies-via expands

Workers (per claude-config personas, B5-shipped) can justify-via either a principle (P1–P6) or a directive (D1–D3) or a named pattern. Most decisions justify-via a principle; operational decisions justify-via a directive. A decision that justifies-via a directive is still legitimate — directives carry derived authority from their parent principle (where one exists) or from a named operational constraint.

Reviewer pre-flight check #2 (DECISIONS section justifies-via) treats principle-references and directive-references as equally valid. No persona update needed; the existing "principle id or named pattern" wording absorbs the new vocabulary.

### Echo cascade

Echo's rulebook/principles.md (B4 in flight) restarts as **cohesive emanation, not parallel set**. Echo doesn't list "inherited Pantheon" + "Echo specializations" + "Echo-only" as parallel sections. It says: "Pantheon's principles apply uniformly here. Echo additionally has these specific principles + currently has these directives." Most of the iteration-1 categorization work survives; the OUTPUT shape changes.

## Consequences

**Positive:**
- Honesty about what's permanent vs scoped. Directives carry their own expiration logic; principles don't drift silently.
- AI-specific durability: agents get explicit dates and re-evaluation triggers on operational rules — they don't infer "this was for 2026 only" from context the way humans do.
- Cleaner principles set (6, well within the cognitive ceiling).
- Direct alignment with Anthropic's published Core Views + RSP pattern, which is the most relevant precedent given we're built on Anthropic infrastructure.
- The tiered structure makes Echo's reframing tractable — directives like "audience of one through phase 3" have a natural home now.

**Negative:**
- One-time renumbering cost. Old ledger entries reference old P-numbers; the mapping table is the canonical translation.
- New tier introduces a small cognitive surcharge ("is this a P or a D?"). Mitigated by the small total count (9 items across both tiers).
- Risk of directive-creep — adding too many directives to avoid the bar of writing a principle. Re-evaluation triggers are the structural defense; orchestrator reviews directive count quarterly.

**Open after this ADR:**
- Whether Pantheon should also adopt explicit "constraints" as a third element (named operational constraints that directives emanate from when no principle parent exists). Currently handled informally — the directive's "emanates from" field can name a constraint directly. Promote to first-class only if the constraint set grows enough to warrant.
- Finished one-shot follow-ups (the Echo cascade order) are in `notes/2026-10-08-org-002-review-history.md`.

## Related

- ORG-001 (ledger format) — schema absorbs both P-N and D-N references via the existing `justifies_via` field; no schema change needed.
- BACKLOG B4 (Echo principle onboarding) — restarts under new framing.
- BACKLOG B5 (worker persona update) — already shipped; no changes needed (persona absorbs new vocabulary via existing "principle id or named pattern" wording).
- claude-config@48e32c2 — worker personas remain unchanged; their format already covers principle/directive/pattern references uniformly.
