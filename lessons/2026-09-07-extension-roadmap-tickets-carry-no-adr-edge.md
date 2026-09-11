# Extension/roadmap tickets carry no ADR edge — informs: is ticket-to-ticket, not a decorative ADR reference

**Date:** 2026-09-07 · **Source:** T-3487 (bare-whole-ADR informs: design pass; filed from T-3477 impl surface) · **Tier:** ORG-065 typed-graph discipline · **Ticket:** T-3487

## The shape

An extension/roadmap ticket (canonical case: T-1626 — "echo: multi-action per utterance") *extends beyond* an ADR (ADR-231) rather than implementing an obligation within it. The Ask surfaced during the T-3477 walk asked whether such a ticket should carry a bare-whole-ADR `informs: [ADR-231#E0]` edge to be graph-legible.

## The composition trap

`detect_unverified_elements` (`tools/ticket_lint.py`) consults three graphs — `verifies` / `implements` / `ratifies` — and applies the `E0` sentinel exclusion only to `ratifies`. `informs:` is not consulted. If T-1626 carried `informs: [ADR-231#E0]` **and** ADR-231 minted an `E0` element, that element would fall through to a permanent false-positive `unverified_element` finding: no verifies, no implements, no ratifies → true-leak branch. Same composition shape as the T-2896 lesson and the T-3476 ratifies-only-E0 fix the operator ratified on TG 6734.

## The decision

**No edge.** Extension/roadmap tickets — the T-3477 census "tangential-tickets" bucket — carry their ADR relationship in **labels** (e.g. `adr-231`) and **prose** (Ask/Context), not as a typed edge.

Reasoning:

1. **`informs:` was scoped narrowly by ORG-065 §3.2** as advisory-notify (soft dep) vs `depends_on`'s hard gate — ticket-to-ticket semantics for non-gating relationships. It was not conceived as a ticket-to-ADR decorative reference; overloading it that way multiplies edge-type semantics without a real load.
2. **A "whole-ADR informational" node kind adds surface for a scenario that isn't yet actual.** ADR-231 does not currently mint an E0; the false-positive is hypothetical. Under P7 (minimal footprint) + P1 (architecture over policy), the cheap protective action is to not add a new node kind or extend `detect_unverified_elements` to consult a fourth graph.
3. **Labels + prose already carry the relationship.** T-1626's `adr-231` label + Ask body is fully sufficient for the reader; the typed graph is not the only legibility surface, and forcing every ADR-adjacent ticket to have an edge conflates "relates to" with "implements/verifies/ratifies."

## When it does NOT apply

- **Actual implementer of an ADR obligation** → `implements: [ADR-NNN#Ek]` (or `#Ek.Cm`). Unchanged.
- **Proof/receipt of an obligation** → `verifies:`. Unchanged.
- **Ratifying a committed file (ADR ratification, plan-doc ratification)** → `ratifies:` with `ratifiable_files:`. Per T-3476.
- **Advisory soft-dep on another ticket** → `informs: [T-NNNN]`. Ticket-to-ticket, unchanged.

## Corollary

The T-3477 census tangential-tickets bucket is now the codified resolve for this shape. Future workers hitting the same question — "should this extension/roadmap ticket carry a bare-whole-ADR edge?" — read this lesson and land no-edge. T-1626's exclusion from T-3477's edge-adding pass is durable.

## What this does not commit us to

A future substantive case — an extension ticket whose relationship to an ADR is load-bearing enough that graph-visibility would prevent an actual failure (not a hypothetical) — can re-open the question with the concrete failure in hand. This lesson closes the *speculative* design; it does not preclude an evidence-driven re-open.
