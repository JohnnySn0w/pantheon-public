---
aliases: [ORG-058]
id: ORG-058
title: "Exocortex-centered memory and learning lifecycle"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2418
---

# ORG-058 — Exocortex-centered memory and learning lifecycle

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-035 and ORG-044. **Composes with:** Echo's corpus identity,
ORG-042 privacy separation, ORG-047 presenter identity, and ORG-050/051
security/secret lifecycle. **Scope:** memory classes and lifecycle, not storage
engine selection.

## Context

A companion that cannot retain, correct, retrieve, and reinterpret experience
does not develop. A system that stores every transcript forever without
provenance, privacy, or forgetting is not a trustworthy memory. Today memory is
split among harness files, tickets, ledgers, Echo events, persona notes, and the
Exocortex; their roles are not one learning lifecycle.

## Decision proposal

### 1. Exocortex is the durable memory organ, not the only storage system

The Exocortex owns the organism's memory model, indexes, consolidation,
relationship context, and retrieval policy. Source systems retain canonical
domain records when they are authoritative: Pantheon keeps decisions/tickets,
Echo keeps auditory/corpus events, services keep operational state. Exocortex
stores references, derived memories, and user-facing continuity—not duplicate
copies by default.

ORG-035's per-agent canonical stores remain the operational-memory baseline.
This direction neither activates its deferred shared multi-writer tier nor
resolves its reserved repository/visibility choice. Those require their named
trigger and a concrete access/privacy design before Exocortex can become a
cross-agent write surface.

ORG-044's digest-altitude rule remains the Pantheon-to-Exocortex default.
Low-level org process does not flood personal memory merely because it exists.

### 2. Separate memory classes

| Class | Purpose | Typical authority |
|---|---|---|
| working | Current conversation/task context; bounded and replaceable | active runtime/checkpoint |
| episodic | What happened, when, with provenance and perspective | source events plus Exocortex index |
| semantic | Consolidated facts, concepts, preferences, and world/user models | derived record with cited evidence |
| procedural | How the organism performs recurring work | SOP, skill, policy, or learned procedure with review |
| relational | Commitments, boundaries, preferences, shared history, and interaction calibration | user-visible Exocortex record |
| identity canon | Stable self/persona/role statements and values | explicitly governed canonical records |

Model weights and hidden session state are not memory classes under this
architecture.

Native or latent model memory and dynamic memory state may be used as a runtime
cache, compression layer, or learned-memory substrate. They do not become
canonical durable memory unless surfaced through the governed memory envelope
with provenance, authority, retention, correction, export, and forgetting
behavior. This is an evidence-bounded posture: *Metis: Memory Foundation Model*
(arXiv:2607.26760v1, §§2.1–2.5, PDF pp. 3–6) defines native memory state and
procedures; its training data defines memory-operation and auxiliary stress
surfaces (§§4.1–4.2, PDF pp. 13–16), and its controlled experiments report
performance on memory-operation tasks (§6.2, PDF pp. 20–21). Its
capacity/general-capability studies report long-horizon compression/interference
and regression risks (§§6.5–6.6, PDF pp. 22–23). The authors explicitly present
native memory as a complement rather than a complete replacement for external
memory (§8, PDF p. 27). Appendix language about persistent cognition or
self-evolution (PDF pp. 32–33) is speculative roadmap material, not demonstrated
capability.

### 3. Adopt one lifecycle with explicit authority transitions

```text
observe
  -> classify provenance, subject, sensitivity, and candidate class
  -> consent/authority gate
  -> retain in the canonical source or reject/expire
  -> consolidate into derived memory with evidence links
  -> retrieve a bounded view for a named purpose
  -> apply through a role/capability boundary
  -> evaluate usefulness, surprise, and harm
  -> confirm, correct, supersede, decay, forget, or re-consolidate
```

Observation is not automatic permission to retain. Retention is not automatic
promotion to semantic truth. Retrieval is not automatic authorization to act.

### 4. Require a memory envelope

Every durable or derived memory exposes, directly or through its canonical
record:

- stable id and memory class;
- source/provenance and observation time;
- subject(s), owner, audience, and privacy class;
- confidence and whether it is observed, asserted, inferred, or generated;
- supporting and contradicting references;
- retention/expiry and correction/forgetting behavior;
- authority required to retrieve and to act on it;
- supersession lineage and last consolidation time;
- purpose/use history sufficient to audit harmful repetition.

Sensitive payload stays in its authorized store; indexes reveal no more than
their caller may know.

### 5. Make retrieval purpose-bounded and context-frugal

Callers ask for a purpose, subject scope, recency/depth, and token/data budget.
The memory organ returns a small evidence-bearing view, not the entire archive.
Retrieval respects the caller's persona/seed authority and the subject's privacy
rules. The Bridge can answer conversationally without giving every backend full
personal history.

### 6. Learning is reviewed consolidation plus behavioral evaluation

The system may propose memories, procedures, preferences, and model updates
from experience. Promotion depends on class:

- factual/semantic consolidation requires evidence and contradiction handling;
- preferences and relational memories require appropriate user visibility and
  correction;
- procedures that affect authority, safety, or external action follow normal
  policy/ADR/review gates;
- low-risk interaction calibration may be applied experimentally with an
  expiry and observable rollback;
- identity-canon changes are goal/relationship-level, never inferred silently.

The system records whether retrieved memory improved the outcome. Unused or
harmful memories decay, are corrected, or are removed according to policy.

Memory-substrate evaluation must exercise, at minimum:

- remember, update, forget, and reflect operations;
- multi-entity binding, interference, and selective forgetting;
- memory pollution and leakage into irrelevant turns;
- long-horizon capacity decay and contradictory-evidence handling; and
- general capability and instruction-following regression after memory
  activation.

These criteria adapt the measured task and limitation surfaces in Metis
§§4.1–4.2 and §§6.5–6.6; they do not adopt its model as Pantheon's memory
architecture.

## Consequences

- Memory can deepen companionship without making every organ a personal-data
  warehouse.
- Different stores remain authoritative while the Exocortex supplies one
  coherent retrieval and consolidation surface.
- Forgetting, correction, provenance, and privacy become first-class
  capabilities rather than later compliance work.
- A capability navigator can query memory by purpose without loading the whole
  Exocortex into model context.
- Building the lifecycle requires data-class policy and user-visible controls
  before continuous ambient capture expands.

## Re-evaluate when

- **ORG-058:R1** — the Bridge uses memory across ten real conversations;
- **ORG-058:R2** — semantic consolidation contradicts a source record;
- **ORG-058:R3** — federation needs to share memory across seed trust domains;
- **ORG-058:R4** — a user requests global correction/erasure;
- **ORG-058:R5** — memory retrieval becomes a material latency/context bottleneck;
- **ORG-058:R6** — learned behavior changes identity or relationship expectations; or
- **ORG-058:R7** — a production harness exposes serializable native memory with export, deletion, audit, and authority controls, making it eligible for evaluation as an Exocortex acceleration layer rather than a replacement.
## DECISIONS

- **Native memory remains an optional governed substrate — serves experimentation
  without surrendering canonical continuity or source authority — justifies via
  P1, P4, and D11.** Current evidence supports complementary/hybrid evaluation,
  not replacement of the Exocortex lifecycle.

- **Exocortex as memory organ with source-system authority preserved — serves
  coherence without duplication — justifies via D10 and ORG-044.**
- **Retention, truth, retrieval, and action are separate gates — serves privacy
  and epistemic safety — justifies via P1 and ORG-050.**
- **Purpose-bounded retrieval — serves useful continuity within finite context —
  justifies via D10 and D1.**
- **Identity cannot be silently learned — serves human authority over the
  relationship — justifies via D3 and P4.**
