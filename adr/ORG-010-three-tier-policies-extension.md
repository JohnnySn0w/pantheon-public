---
aliases: [ORG-010]
title: "Three-tier framework: Principles + Directives + Policies"
status: Ratified
date: 2026-05-10
build_disposition: built
---
# ORG-010 — Three-tier framework: Principles + Directives + Policies (ORG-002 extension)

**Status:** Ratified 2026-05-10 by the operator (Telegram msg_1190: "Ratify: adr 10, put directives and principles and policies inside of new /rulebook folder, file ticket for recovery to do spin updates"). Migration: rulebook/principles.md + rulebook/directives.md + new policies/ all live under `rulebook/`.
**HITL:** Ratified
**Date:** 2026-05-10
**Author:** Orchestrator (Orchestrator)
**Origin:** T-0173 — the operator msg_876 (2026-05-10): *"Making the hierarchy, principles, directives, policies. Policies being lowest, possibly HO authored?"*
**Extends:** ORG-002 (two-tier framework: Principles + Directives, ratified 2026-04-28).
**Justifies via:** Principles P1 (architecture over policy), P6 (operator comprehension); Directives D2 (hand-curated docs), D10 (context-frugality); lessons `org-churn-is-constant-methodology-must-be-concise` (the load-bearing why for layered authority + concise methodology). Empirical: same precedents that grounded ORG-002 (Anthropic Core Views vs Responsible Scaling Policy; military doctrine vs SOPs vs ROE; legal constitution vs statute vs regulation), now extended one tier deeper to capture operational-policy-shape that ORG-002's two-tier didn't have a structural home for.

## Context

ORG-002 established the two-tier framework: **Principles** (durable, operator-authored, immutable to workers per D2) + **Directives** (scoped/dated, operator-ratifies-HO-drafts, editable when re-evaluation trigger fires). The framework has held shape through D1-D10 ratification + multiple incident cycles + the b-pattern dispatch model.

Two practical gaps surfaced during T-0160 (SOP standard-bucket re-routing) and T-0173 (this ticket):

1. **`rulebook/policies/autonomy.md` + `rulebook/policies/trust-posture.md` are operational-policy-shape but lack a structural home.** They define how HO behaves: what HO auto-approves, what requires explicit ratification, what is never permitted. They sit BELOW directives in stature (don't carry re-evaluation triggers, don't have the full operator-authoring discipline applied) but ABOVE generic notes (they're load-bearing for HO behavior — `inbox_consumer.py`'s INJECT_PROMPT explicitly references `rulebook/policies/trust-posture.md`; persona briefs cite the autonomy bar).

   Today they live in `notes/` because there's no better bucket. T-0160 considered putting them under `sop/policy/` but they're not procedural — they're statements of authority calibration. The structural gap is real.

2. **HO-authoring autonomy needs an explicit calibrated tier.** The two-tier framework requires the operator ratification for every directive amendment. For operational policies — autonomy-bar wording, trust-calibration thresholds, HO-internal escalation criteria — the operator ratifies *at surface time* rather than *at every edit*; the policy file lands as HO-drafted, the operator reviews + ratifies, subsequent amendments by HO surface as ledger entries with D3-style escalation triggers if the operator disagrees.

   This is the **Anthropic precedent extended**: Core Views (Principles) → Responsible Scaling Policy (Directives) → Internal Operational Policies (Policies). The third tier is where ops-level calibration lives.

## Decision

### Add a third tier — **Policies** — between Directives and operational documentation.

**Naming**: "policies." Considered alternatives (dispositions, postures); policies matches the operator's framing in msg_876 + the legal/military/Anthropic precedent without overloading.

### Three-tier shape

| Tier | Source of will | Authoring | Editable | Re-evaluation discipline | Examples (today) |
|---|---|---|---|---|---|
| **Principles** | Durable | the operator authors | Immutable to workers (D2); ADR amendment to change | Re-evaluated if a principle proves structurally infeasible or contradicted by ratified directives | P1-P6 in `rulebook/principles.md` |
| **Directives** | Scoped + dated | the operator ratifies; HO drafts | Editable when re-evaluation trigger fires | Each directive carries a "Re-evaluate when" trigger (event or date) | D1-D10 in `rulebook/directives.md` |
| **Policies** (NEW) | Operational | HO authors | Editable by HO with the operator surface-time ratification | Inherit directive-shape "Re-evaluate when" triggers; lighter ratification cadence — the operator reviews at surface, not at every edit | `autonomy.md`, `trust-posture.md` (today in `notes/`; migrating to `policies/` per Implementation §3) |

### Authoring + ratification semantics for policies

- **HO authors** the initial policy file.
- **the operator ratifies at surface time** — when a new policy lands or a substantive amendment surfaces. Ratification sets the "Status: Ratified <date>" line.
- **Subsequent amendments by HO** get ledger-entry surfaces. If the operator wants per-edit review, he requests it via D3-style escalation; otherwise HO operates within the ratified policy's shape. Cap on amendment scope: cosmetic / wording / minor calibration is HO-direct; substantive change of authority surface (e.g., expanding what HO auto-approves) is full surface-and-ratify.
- **Re-evaluation triggers** mirror directives — every policy carries a "Re-evaluate when" line. Substrate changes (new model family, harness shape change, multi-account topology) are typical triggers.

### Why a tier (not just notes/)

The point is **calibrated authoring authority + structural traceability**. `notes/` is a generic catch-all; readers can't tell which entries are load-bearing for HO behavior vs which are session-state, design history, watchlist tracking. The policies tier names the load-bearing-for-HO-behavior entries explicitly + applies the appropriate authoring discipline.

This mirrors the lesson from `lessons/org-churn-is-constant-methodology-must-be-concise.md`: under context-degradation pressure, agents need *named, indexed, calibrated* references rather than generic prose. Policies tier names the operational-authority surface; agents read it once + cite by reference; HO authors with discipline that matches the stake.

## Implementation

### 1. File shape

**`policies/`** as a directory (parallel to `adr/` + the planned `sop/`), not `policies.md` as a single file. Each policy is substantive enough to warrant its own file:

```
policies/
├── README.md                        (spine + index + when-to-add)
├── autonomy.md                      (founding policy — migrated from notes/)
├── trust-posture.md                 (founding policy — migrated from notes/)
└── <future-policy>.md               (HO-authored under D2; per-policy ratification at surface time)
```

### 2. Per-policy file shape

```markdown
# <Policy name — distinctive one-line>

**Status:** Drafted / Ratified <date> / Superseded by <ID>
**Author:** Orchestrator (Orchestrator) [or attribution]
**Date:** YYYY-MM-DD
**Re-evaluate when:** <substrate / event / date trigger>
**Justifies via:** <principles + directives this policy operationalizes>

## Context

What surfaced the policy. The ops-level calibration question that needed an answer.

## Policy

The actual operational rules. Calibrated to ops-tier — concrete, enforceable, traceable.

## Boundary

What this policy does NOT cover. Explicit hand-off to neighboring policies, directives,
SOPs, or HO's discretion.

## Re-evaluate when

The trigger (event, date, or condition) that prompts a substantive amendment review.

## Related

Links to principles, directives, ADRs, lessons, SOPs, tickets.
```

### 3. Migration of `rulebook/policies/autonomy.md` + `rulebook/policies/trust-posture.md`

Per D10 ("references replace transclusion") + the operator msg_876 ("removing the information elsewhere, and replacing it with references"):

1. **Move** `rulebook/policies/autonomy.md` → `policies/autonomy.md` (with frontmatter retrofit per §2 above).
2. **Move** `rulebook/policies/trust-posture.md` → `policies/trust-posture.md` (same).
3. **Delete** the originals from `notes/` (no symlinks, no duplicates).
4. **Update cross-references** in:
   - `CLAUDE.md` (any path) — update path references.
   - persona briefs at `~/.claude/agents/*.md` (project-orchestrator, implementer, reviewer, researcher) — update path references.
   - `tools/inbox_consumer.py` INJECT_PROMPT (currently references `rulebook/policies/trust-posture.md`) — update path.
   - Any `tools/*.py` script that grep paths — update.
   - Any `lessons/` / `incidents/` / `bugs/` cross-references that name old paths.
   - Any `tickets/` open-or-recent-done that reference old paths (HO-direct sweep at migration time).
5. **`policies/README.md`** as the directory spine (mirrors `adr/`-style README + index).

### 4. ADR update

This ADR (ORG-010) is the structural amendment. ORG-002 stays as-is (the two-tier ratification was a load-bearing org event); ORG-010 extends without superseding. ORG-002's "Decision" section reads cleanly with a forward-pointer added: "Extended 2026-05-NN: see ORG-010 for the policies tier."

### 5. Sibling integration with T-0157 SOP framework + T-0160 standard-bucket re-routing

T-0160 considered putting `autonomy.md` + `trust-posture.md` in `sop/policy/`; that was the question that surfaced this ADR. **Resolution**: those documents do NOT go in `sop/`. SOPs are procedures (how to do X). Policies are calibrated authority surfaces (HO's operational rules). Different category; different tier.

`sop/standard/` + `sop/remediation/` is the ratified two-bucket SOP structure per T-0157+T-0160. `policies/` is the new third structural tier extending ORG-002.

### 6. Future-policy candidates (HO seeds; not exhaustive)

When these surface as load-bearing-for-HO-behavior with a calibrated authoring shape:

- **`policies/autonomy.md`** — ratified founding (above).
- **`policies/trust-posture.md`** — ratified founding (above).
- **Future**: HO-internal escalation criteria (when does HO bubble up to the operator vs absorb at HO-tier — currently codified across CLAUDE.md, T-0156 5-criteria list, D7); cross-account routing policy (Recovery / Specialist / Orchestrator ticket-comms patterns); first-fire posture for new mechanisms (when does new infra get supervised first-fire vs land-and-trust). All HO-authored under this ADR's authoring discipline.

## Re-evaluate when

- A new tier is needed below policies (operational-runbook-level that doesn't fit SOPs and shouldn't be policy). Trigger: surface during normal arc-execution.
- The operator's surface-time ratification cadence proves too loose (HO drifts from intent too often) or too tight (HO bandwidth burn on routine policy edits). Trigger: 30-day observation post-ratification.
- Substrate shift (new model family, new harness shape, multi-account topology change) that changes the load-bearing-for-HO-behavior surface in a way the current policies don't cover.

## Boundary — what this ADR does NOT cover

- **Per-policy authoring**: this ADR establishes the *tier*; per-policy authoring is HO-direct under D2 with surface-time the operator ratification. Migration of `autonomy.md` + `trust-posture.md` is in scope (§3); authoring net-new policies happens as need arises.
- **Re-architecting Principles or Directives**: those tiers are unchanged; ORG-010 extends without amending P1-P6 or D1-D10.
- **`sop/` structure**: T-0157 + T-0160 are the home; ORG-010 only resolves the open question about whether autonomy/trust-posture belong there (NO; they belong here).
- **Lessons + incidents + bugs catalogs**: those are seed/derivation/abstraction layers, not authority-surface; unchanged by this ADR.

## Cross-references

- **Principles + directives**: P1 (architecture over policy — direct lens), P6 (operator comprehension), D2 (hand-curated docs — applies to policies tier), D10 (context-frugality — references replace transclusion; the migration discipline).
- **ADRs**: ORG-002 (the framework this extends; unchanged), ORG-001 (ledger discipline), ORG-006 / ORG-009 (kino PWA — orthogonal but the policies tier informs how PWA-tier action authority calibrates).
- **Lessons**: `lessons/org-churn-is-constant-methodology-must-be-concise.md` (the why for layered authority + concise methodology); `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md` (sister lesson; structural enforcement at the dispatcher↔dispatchee boundary).
- **Incidents**: `2026-05-04-b9-team-pattern-first-run/` (where the original autonomy-bar calibration question surfaced as a structural fix); `2026-05-09-b33-ticket-state-sweep-multi-violation/` (where the three-REBOUND-budget bubble-up criteria — currently in trust-posture.md — were exercised).
- **Tickets**: T-0173 (this ADR's filing ticket), T-0160 (SOP standard-bucket re-routing — sibling framework migration), T-0157 (SOP folder framework), T-0158 (lessons folder framework), T-0022 (original trust-posture authoring ticket).
- **Telegram refs**: msg_876 (the operator's articulation: *"making the hierarchy, principles, directives, policies. Policies being lowest, possibly HO authored?"*), msg_826 (the load-bearing why — org-churn + concise methodology), msg_859 (D10 empirical-citation ratification).
- **External**: Anthropic Core Views + Responsible Scaling Policy + Internal Operational Policies precedent; military doctrine + SOPs + ROE three-tier; legal constitution + statute + regulation three-tier.
