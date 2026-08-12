---
aliases: [ORG-073]
title: "Information-flow — retrieval-policy and injection layer"
status: "Proposed — canonicalized 2026-08-11 by Orchestrator from the Specialist-authored draft (Autonomous, under the operator's explicit direction, Telegram 2026-08-10); awaiting the operator's ratification. Extends operator-ratified ORG-058 and draft ORG-062."
date: 2026-08-10
author: Specialist (claude-research) — draft; Orchestrator (Orchestrator) — canonical placement (ORG-number + convention)
relates: ORG-058, ORG-062, ORG-065, ORG-017, ORG-035, ORG-012, ORG-044, T-2533, T-2425
build_disposition: needs-build
scoping_ticket: T-2816
re_evaluate:
  - id: R1
    type: event
    when: "the decision-recall-injection PoC (move 1) is first built and measured"
    fires: surface
    text: "PoC hit/precision numbers may revise the recency-vs-semantic default or the corpus scope"
  - id: R2
    type: event
    when: "resolves:T-2533"
    fires: surface
    text: "ACE / handoff detail-loss work lands — reconcile move 3 with the shipped mechanism"
---

# ORG-073 — Information-flow: retrieval-policy and injection layer

**Status:** **Proposed** — canonicalized 2026-08-11 by Orchestrator from the Specialist-authored draft (Autonomous, Specialist-authored under the operator's explicit direction, Telegram 2026-08-10). Awaiting the operator's ratification. **HITL: Proposed.**

**Source record:** `knowledge/episodic/2026-08-10-information-flow-optimization-study/` (the how-it-works-now / SOTA / what-we-can-do research this ADR decides on) and the the operator Telegram arc 2026-08-10. Draft preserved at `knowledge/episodic/2026-08-10-info-flow-adr-draft/`.

**Extends:** ORG-058 (exocortex memory classes + purpose-bounded retrieval — this ADR builds the *retrieval-policy + injection* layer ORG-058 names but does not implement) and ORG-062 (memory-substrate evaluation methodology — this composes with its build sequence). **Composes with:** ORG-012 (policy outflow), ORG-044 (digest-altitude), T-2533 (ACE / handoff detail-loss).

## Context

Recurring failure, operator-observed (the operator, 2026-08-10): personas act without knowledge that *exists in the repo* — an ADR or prior decision they should have applied but never traversed to in-session, and which was not in their handoff. Diagnosis (research-backed): this is a **retrieval/injection gap, not a storage gap.**

Root causes, mechanically grounded:
1. **Decisions rely on active traversal ("index-and-hope").** Our memory recall (`recall-feedback.py`, T-1090) is deliberately **exact-token-only** (file paths, `T-NNNN`, `ORG-NNN`, basenames) over `feedback`/`project` memories — fuzzy/semantic recall was declined as bloat (T-0672). The ADR/decision corpus is **not in any recall path**; discovery depends on the agent knowing to look. Literature confirms LLM agents that can't cheaply resolve an index guess from training priors rather than traverse.
2. **Compaction + handoff are lossy summarization.** Context rot (30–50% accuracy loss before the window fills) forces compaction; both intra-session compaction and cross-session handoff silently drop detail (T-2533/ACE).
3. **Triggered injections accumulate.** Content injected for one past turn is re-sent every subsequent turn, carrying token cost with no ongoing value.

ORG-058 specifies the memory model (classes, envelope, purpose-bounded retrieval) but not the operational retrieval/injection mechanism. This ADR decides that mechanism.

## Decision

Establish a **retrieval-policy and injection layer** as the operational realization of ORG-058's purpose-bounded retrieval. All mechanisms are git-durable, provenance-carrying (ORG-058 envelope), and human-ratified — audit-first is preserved.

### 1. Decision-recall injection (core)
Extend the recall-hook mechanism to cover the **decision corpus** (ADRs, ledger entries, designated notes), not just `feedback`/`project` memory pins.
- Each decision record carries a **trigger-description** in frontmatter (Skills-style: the description is the trigger, not documentation), so it is matched against current intent at near-zero per-entry cost.
- **Retrieval policy:** recency-window (Last_K, K≈3–5) as the default — empirically matches/beats full-history and semantic retrieval (arXiv 2604.03826) — **plus semantic retrieval reserved for non-sequential / cross-cutting** decisions where recency alone misses.
- **Substrate:** hybrid BM25 + vector over the existing git-tracked markdown. **Defer** a knowledge-graph layer until multi-hop provenance need is demonstrated (graph adds real ops burden).
- **Injection:** via the hook `additionalContext` channel (the same ingress `recall-feedback.py` uses), as a "recalled decision — verify it still applies" nudge, precision-capped and per-session deduped.

### 2. Push-at-write
On commit of a decision/ADR, a hook propagates a **pointer** (not the body) into the surfaces of affected personas (handoff/memory). Formalizes and per-persona-targets the ORG-012 policy-outflow pattern. This is the industry near-white-space fix for "record exists but never reached the agent" — push-at-write over pull-at-read.

### 3. Compaction detail-loss mitigation
- **Structured note-taking:** decisions are persisted to files at decision time, so compaction/handoff can never eat what was only-in-conversation.
- Prefer **incremental delta-updates to curated context** over naive re-summarization, to avoid "context collapse" (ACE; T-2533). The existing HANDOFF ordering discipline (importance-weighted, expendable-last) is the manual precursor; make the weighting structured.

### 4. Context editing / stale-injection eviction (per the operator, 2026-08-10)
Batched eviction of spent triggered-injections (old recalls, processed tool outputs) to reclaim tokens over a long session — the "context editing / tool-result clearing" pattern (~84% token savings reported on long tasks).
- **Cache discipline:** eviction invalidates the prompt cache from the earliest edited position forward, so evictions are **batched** (drop many at once, infrequently) to amortize the one-time uncached reprocess. Net-positive when the evicted volume is large and many turns remain.

### 5. Modular context (progressive disclosure)
Move from monolithic always-on files toward many small, independently-triggerable units, each carrying its own trigger-description (Tier-1 metadata always scanned → Tier-2 body on match → Tier-3 supporting files on reference). Consider size caps to force decomposition rather than rely on author discipline.

### 6. Provenance and freshness
Keep recency separate from semantic relevance (already done via `recent-policy-changes.md`). Use ORG-058's **supersession-lineage** envelope as the staleness signal so a superseded decision never injects as current. GraphRAG-style provenance deferred to §1's "defer knowledge-graph" trigger.

## Consequences
- Closes the operator-observed failure at its mechanism: relevant decisions surface at point-of-need without the agent knowing to look.
- Additive and reversible — every piece is a hook/file change over existing git-durable substrate; nothing rewrites the storage model.
- Cost: build + maintain a hybrid index; reflection/eviction tuning. Sequenced so move #1 (highest leverage) can PoC first and prove value before the rest.

## Build sequence
1. **Decision-recall-injection PoC** (move 1) — highest leverage, PoC-able over existing markdown with the recall-hook ingress. *(Build ticket: **T-2816**, the ADR's `scoping_ticket` — PMB-1-gated; its hit/precision numbers gate the rest.)*
2. Push-at-write (move 2) — formalize ORG-012 outflow per-persona.
3. Compaction/detail-loss (move 3) — converge with T-2533/ACE.
4. Context-editing eviction (move 4), modularization (move 5), provenance/freshness (move 6) — as the layer matures.

## Relationship to the unified memory/knowledge program (NOT a silo)
This ADR is the **retrieval-policy + injection application** ORG-058 names ("make retrieval purpose-bounded and context-frugal") but leaves unbuilt. It must plug into the existing program, not build parallel infrastructure:
- **ORG-058** (spine) — memory classes + envelope; this realizes its purpose-bounded retrieval. No change to the model.
- **ORG-062** (substrate + eval) — the decision-recall index (move #1) **rides ORG-062's substrate build** (episodic retrieval tier → semantic/relational tier), is **PMB-1-gated** (no tier ships without passing), and does **not** stand up a parallel index.
- **`eval-set-t2107`** (retrieval eval infra) — reuse its queries/pooling/judgments methodology to produce move #1's hit/precision numbers; no parallel eval harness.
- **ORG-065** (first-class dependency/lineage graph) — supplies the **provenance / GraphRAG** layer this ADR defers under §1; the "why was this retrieved" path is the ORG-065 typed edge set (ADR→obligation→ticket→evidence), so provenance is a query, not a rebuild.
- **ORG-017 / ORG-035** — knowledge-tree unification + memory portability are the foundations the corpus + index sit on.
- **T-2425 evidence** — measured that storing *irrelevant* memory costs **22pts of instruction-following**: a hard argument for the *precise, purpose-bounded* retrieval this ADR specifies over dumping context, and for PMB-1-gating the embedding/substrate choice (embedding-swap alone moved +6.2pp).
- **Sibling ADR** — the self-improvement / optimization ADR (GEPA + ACE) is the *learning* facet; this is the *retrieval* facet. Both spine on ORG-058.

No change to ORG-058's memory classes or envelope; this is the retrieval/injection track of ORG-062's build sequence.
