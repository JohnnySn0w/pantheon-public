---
aliases: [ORG-074]
title: "Self-improvement / optimization layer — human-gated prompt (GEPA) and context (ACE) improvement"
status: "Proposed — canonicalized 2026-08-11 by Orchestrator from the Specialist-authored draft (Autonomous, under the operator's explicit direction, Telegram 2026-08-10); awaiting the operator's ratification. Extends operator-ratified ORG-058 (the learning half of its memory-AND-learning lifecycle)."
date: 2026-08-10
author: Specialist (claude-research) — draft; Orchestrator (Orchestrator) — canonical placement (ORG-number + convention)
relates: ORG-058, ORG-062, ORG-065, ORG-017, ORG-035, ORG-073, T-2425, T-0649, T-2533
build_disposition: needs-build
scoping_ticket: T-2823
re_evaluate:
  - id: R1
    type: event
    when: "the GEPA persona PoC (T-2809) lands with before/after numbers"
    fires: surface
    text: "PoC numbers may revise per-component scope, reflector-tier choice, or the human-gate shape"
  - id: R2
    type: event
    when: "a multi-agent prompt optimizer (MASPO-class) reaches production maturity"
    fires: surface
    text: "revisit whole-org optimization — deliberately deferred here because naive multi-agent GEPA is unreliable"
---

# ORG-074 — Self-improvement / optimization layer

**Status:** **Proposed** — canonicalized 2026-08-11 by Orchestrator from the Specialist-authored draft (Autonomous, Specialist-authored under the operator's explicit direction, Telegram 2026-08-10). Awaiting the operator's ratification. **HITL: Proposed.**

**Source record:** `knowledge/episodic/2026-08-10-gepa-verification/` (GEPA verified numbers + SOTA + adopt-vs-Hermes) and `knowledge/episodic/2026-08-10-information-flow-optimization-study/` (ACE / context-collapse). The operator Telegram arc 2026-08-10. Draft preserved at `knowledge/episodic/2026-08-10-self-improvement-adr-draft/`.

**Extends:** ORG-058 — this ADR builds the **learning half** of ORG-058's "memory *and* learning lifecycle," which ORG-058 names but does not mechanize. **Composes with:** ORG-062 (uses PMB-1 / eval methodology to measure improvement; ACE's context store rides ORG-062's substrate), ORG-073 (info-flow retrieval/injection — sibling; retrieval gets the right *existing* knowledge, this ADR *improves* the prompts/context).

## Context

ORG-058 defines a lifecycle that ends in "evaluate usefulness … confirm, correct, supersede, decay, forget, or re-consolidate" — i.e. **learning**. But the org has no *mechanism* for how its prompts and contexts actually improve from experience; today it happens by human judgment (feedback memories, retros, handoff lessons). Two mature, complementary techniques mechanize exactly this — and both must stay human-gated to preserve audit-first.

## Decision

Adopt two complementary, **human-gated** optimization mechanisms as the org's learning layer. Neither auto-applies; both propose, a human (or the ratification loop) ratifies, and every change is provenance-tracked (ORG-058 envelope).

### 1. GEPA — offline reflective PROMPT optimization
Optimize the docs that are *run and measurable* — persona system prompts, SOPs, tool-descriptions — offline against an eval/reward, producing an improved prompt artifact deployed to *future* sessions after human ratification.
- **Per-component only.** Naive multi-agent GEPA is empirically unreliable (MAS-PromptBench: +24 to −16pp); the fix (MASPO) is too immature to adopt (see R2).
- **Requires an eval set = "tests for docs."** Building that eval set is the real work *and* a reusable asset: once a persona/SOP has tests, any change (GEPA or human) can be measured. That value exists even if GEPA is never run.
- Does **not** apply to declarative decision docs (ADRs, principles) — those aren't executed against a reward.

### 2. ACE — continual CONTEXT/playbook curation
Curate an agent's context/playbook via incremental **delta-updates** (Generator/Reflector/Curator), instead of monolithic re-summarization — directly countering "context collapse" / silent detail-loss (the failure behind lossy compaction + handoffs; relates to T-2533 and ORG-073's move #3).
- Human-gated integration into curated context; no auto-mutation of canonical records.
- ACE's curated context store rides ORG-062's memory substrate (not a parallel store) and carries the ORG-058 envelope.

### 3. This is the learning half of ORG-058
GEPA improves the *procedural/instructional* text; ACE curates the *accumulated experiential* context. Together they realize ORG-058's "evaluate → confirm/correct/supersede/re-consolidate" as a running mechanism rather than a manual habit.

### 4. Complementary, not competing — three stacking layers
- **GEPA** → a better PROMPT (offline, reward-driven).
- **ACE** → a better living CONTEXT (online, experience-driven).
- **Info-flow retrieval (ORG-073)** → the right EXISTING decision, just-in-time.
All three compose; each is proven as a separate, human-gated PoC; none is coupled to the others.

### 5. Measurement rides the existing program
Improvement is measured with the same evaluation methodology the memory program already defines — PMB-1 (ORG-062) and the retrieval/eval infrastructure from `eval-set-t2107` (queries/pooling/judgments). No parallel eval harness.

### 6. Incorporate (do NOT replace) the existing consolidation/promotion loop
Pantheon already runs a learning loop — the scheduled comb: retro walks, throughline detection, and `promote_lessons` (weekly cron) that bubbles bug-reports/incidents/feedback into `lessons/`, then proffers the deterministic-guard-shaped subset to `promotion-candidates/` for recovery review. **This IS ORG-058's consolidation/reflection pass** — the promotion ladder episodic (reports/incidents) → semantic (`lessons/`) → procedural (guards/SOPs) → durable (directives/principles). GEPA/ACE **mechanize and augment this same loop** (comb throughline-detection ≈ ACE Reflector; promotion ≈ ACE Curator; GEPA can optimize the personas that do the combing/reviewing) — they do not replace it, and the human enforcement-promotion gate stays.

**Measured state (2026-08-10):** tier-1 (events→`lessons/`) is healthy (49 lessons, steady authoring). Tier-2 (lessons→committed guards) is the constraint: of 18 candidates ever surfaced, **2 landed (~11%), 8 declined, 8 pending-review**. Low promotion is partly by design (strict promotability: deterministic + mechanically-checkable + recurrent), but the bottleneck is **review-gate throughput**, not proffering.

**Traceability gap (the operator, 2026-08-10 — validated):** the `promotion-candidates/` ledger records outcome (`status`) + fingerprint but **no decision timestamp** (fields: `check_kind, source, status, title`), and **both `--land` and `--decline` DELETE the candidate file** — so when a decision was made and the candidate's content/rationale are recoverable only by commit archaeology. This violates P4 (traceability). The mechanized loop must **timestamp decisions + preserve landed/declined records** (tie into ORG-065's first-class lineage graph so decision provenance is a query, not a grep). The non-ticketed "recovery reviews at leisure" design is intentional (a `target=recovery` ticket would auto-fire and defeat the human gate) — which makes fixing the ledger-level traceability *more* important, not less. *(Concrete fix ticket filed alongside this draft.)*

## Consequences
- Mechanizes learning that is currently manual, under human-gated / audit-first control.
- Additive: GEPA is a standalone library at the optimization layer; ACE rides the ORG-062 substrate. No storage-model rewrite.
- Strong supporting evidence (our own T-2425): storing irrelevant memory costs **22pts of instruction-following** — a hard argument for *precise* improvement/curation over indiscriminate accumulation, reinforcing the human-gate + purpose-bounded posture.

## Build sequence
1. **GEPA persona PoC** — T-2809 (one persona, human-gated, per-component, guardrailed; doubles as building our first doc-test-suite).
2. **ACE eval + PoC** — T-2819 (context/playbook curation, human-gated).
3. Wider adoption gated on PoC results + PMB-1.

## Relationship to the unified memory/knowledge program
Spine: **ORG-058**. Substrate + eval: **ORG-062** (PMB-1) + `eval-set-t2107`. Graph/provenance: **ORG-065**. Foundations: **ORG-017 / ORG-035**. Sibling application: **ORG-073** (info-flow **retrieval/injection**). This ADR is the **learning** facet; none of these are silos.
