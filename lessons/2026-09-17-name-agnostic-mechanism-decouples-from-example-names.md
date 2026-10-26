# Name-agnostic mechanism decouples from ADR example names — the ratify-merge doesn't gate on the naming

**Date:** 2026-09-17 · **Source:** ORG-088 rev-2 arc (the operator TG 10080 → orchestrator analysis → TG 10082 option-1 endorsement) · **Tier:** HO ratify-walk discipline · **Ticket:** T-3792

## The shape

An ADR ratifies a MECHANISM (e.g. "pull-time role-membership filter") that reads a data source (e.g. `functional_roles(persona)` from the L3 PersonaDefinition source) to make its decision. The ADR's PROSE uses concrete example names (e.g. `role:orchestrator`, `role:sub_orchestrator`) to illustrate the mechanism. The mechanism-vs-names split is delegated in a specific decision (rev-2 D-2: "Membership owned by L3 PersonaDefinition; instance.yaml only projects").

An adjacent open ontology question exists (canonical case: T-3778 role-name namespace inventory — the 4-namespace collision the operator wants to sit down and settle). It concerns exactly the example names the ADR uses.

The reflex when the operator surfaces the ontology-first instinct: **hold everything until the naming lands**. The analysis: **check whether the mechanism reads the names or bakes them**. If the mechanism reads them at runtime through a delegation-tier source, the mechanism is *name-agnostic* and the ratify-merge doesn't gate on the ontology; only the ADR prose *examples* would refresh on top of a future rename.

## The decision framing

When the operator raises an ontology-first concern on an ADR ratification:

1. **Locate the mechanism's decision-input surface.** Is it a runtime read (function call, config load, membership predicate) or a hard-coded literal (case-match, hard-wired constant)?
2. **If runtime read via a delegation-tier source:** the mechanism is name-agnostic. A rename is a data edit at the source; the mechanism keeps working. Ratify + merge can proceed on option-1 parallel-path; ontology work updates the source; ADR *example* prose refreshes on top later without a rev-3 (freeze-at-ratification permits prose-refresh-following-source-of-truth-update as a data-follow, not hygiene).
3. **If hard-coded literal:** mechanism IS name-bound; hold the ratify until the naming settles, or restructure the mechanism to read from delegation-tier before ratifying.

For ORG-088 rev-2: D-2 delegated role membership to L3 PersonaDefinition. `functional_roles(persona)` is a runtime read from that source. Renaming `orchestrator` → whatever the ontology settles on is a data edit at the L3 source; the pull-time filter doesn't care what the string looks like. Option-1 parallel-path was the correct call.

## The trap this closes

**Conflating ADR example-name concreteness with mechanism name-binding.** An ADR that uses concrete role names in examples *looks* name-committed, especially when a rename question is live. The operator's reflex to hold the merge is right IF the mechanism is name-bound, but wrong IF the mechanism reads names from a delegation-tier. The analysis in step 1 above resolves which case applies before over-holding.

The reverse trap — merging a name-bound mechanism whose examples are about to be renamed — creates a rev-3 that has to update both the mechanism AND the examples in the same amendment. Cheaper to hold in that case.

## Reasoning

1. **The freeze-at-ratification pin** bars post-ratify hygiene edits, but permits data-follow refreshes when the source of truth changes (a rename lands in L3, the ADR examples following that rename is a substantive update, not hygiene).
2. **D-N delegation-tier decisions in ADRs** exist precisely to keep mechanism concerns separate from naming concerns. When a decision like D-2 explicitly says "membership owned by L3 source," the ADR itself is scoped to the mechanism side of that split.
3. **the operator's operator-instinct + agent-analysis** working together caught this — his TG 10080 instinct to hold, corrected by my mechanism-analysis reply, landed as his TG 10082 option-1 endorsement. The lesson isn't "don't listen to the operator's hold-instinct"; it's "surface the mechanism-vs-name analysis when the instinct arises, so the operator can decide informed of the split."

## When it does NOT apply

- **ADRs that ratify names as part of the substance** (e.g. an ADR whose core decision *is* the naming choice). These are naming decisions themselves — the ontology question can't be separated from the ratify.
- **Mechanisms that hard-code names** (case-match, literal-string dispatch). No delegation-tier means the rename touches the mechanism, not just examples.
- **Public-facing surfaces where the example NAMES are load-bearing** (docs, external-integration contract). If the "example" name is actually the contract surface, treat it as part of the substance.

## Corollary

Future ratify-walks that surface an adjacent ontology-first instinct should apply the 3-step framing above BEFORE recommending a hold. The instinct is often right; the analysis reveals when it's the parallel-path scenario instead.

## What this does not commit us to

An ontology change that touches mechanism call-sites (renaming `functional_roles` itself, or changing the shape of what it returns) IS mechanism-affecting and the parallel-path shortcut doesn't apply. This lesson closes the *string-example-only* case, not the *interface-signature* case.

## Re-evaluate when

- A second instance of this shape appears (canonical or with sharpened test — promotes from "pattern named once" toward directive/SOP if load-bearing across arcs).
- A rename downstream of an option-1 parallel merge causes a rev-3 that touches more than just the ADR prose examples — that would invalidate step 2's data-follow framing and prompt narrower guidance.
