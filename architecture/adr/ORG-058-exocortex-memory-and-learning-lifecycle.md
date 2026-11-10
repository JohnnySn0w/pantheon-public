---
aliases: [ORG-058]
id: ORG-058
title: "Exocortex-centered memory and learning lifecycle"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2418
# elements: — ORG-065 machine index (T-3205 Tranche B whole-ADR policy
# call: the nine T-2413..T-2421 "elaborate ORG-0NN into a
# program/implementation plan" tickets each plan a WHOLE ADR, not one
# numbered section — see T-3205 DECISIONS for the edge-per-section-vs-
# whole-ADR-element reasoning). E0 is a single reserved whole-document
# element (zero cannot collide with any native numbering, which starts
# at 1, and sorts first) — the obligation genuinely is "plan the entire
# ADR". A future per-section elementization of this ADR (if ever
# performed) can add its own native-numbered elements (E1, E2, ...)
# without touching E0. Added by HO; no obligation content changed.
elements:
  - {id: E0, text: "ADR as a whole: Exocortex-centered memory and learning lifecycle — whole-document element for plan/elaboration tickets (ORG-065 pass-2 policy, T-3205)"}
---

# ORG-058 — Exocortex-centered memory and learning lifecycle

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message 665; T-2399).** HITL/tracking provenance and the 2026-08-30 trust-floor amendment trail (Consultant, D3): `notes/2026-10-08-org-058-review-history.md`.

**Amendment (2026-10-05, T-4275):** The lifecycle transition map (plan report §5; `memory_envelope.LIFECYCLE_TRANSITIONS`) gains the edge `RETAINED_SOURCE_REF → FORGOTTEN_DERIVED`. It was proposed by archivist and written under D2, and ACCEPTED by the operator on review (Telegram 630, 2026-10-07; the ADR human-review gate per #738); ledger `2026-10-05T06:54:12Z-t4275-org058-source-ref-forget-decision`. Before this change, a subject-initiated forget of a source-ref memory could only exit to DECAYED, which means "expired by policy". FORGOTTEN_DERIVED means that the *memory artifact* was forgotten at the subject's request, while the canonical source is untouched. A source-ref memory is a derived reference, not the source. No new terminal state was added: the transition's FROM state in the append-only log already records source-ref vs derived. No migration is needed. Policy expiry still goes to DECAYED. This amendment does not alter the ratified status.

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
behavior. This is an evidence-bounded posture: *Mason: Memory Foundation Model*
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

Every durable or derived memory carries, for each independently usable claim, an immutable inherited trust floor and an effective trust floor under a named, digest-bound trust policy. The inherited floor is the conservative meet of every contributing source, never an invented order or model confidence. Integrity, epistemic confidence, privacy/retrieval permission, and action authority remain separate dimensions. Normative schema: `sop/standard/memory-envelope-spec.md` §1.

### 5. Make retrieval purpose-bounded and context-frugal

Callers ask for a purpose, subject scope, recency/depth, and token/data budget.
The memory organ returns a small evidence-bearing view, not the entire archive.
Retrieval respects the caller's persona/seed authority and the subject's privacy
rules. The Bridge can answer conversationally without giving every backend full
personal history.

A `MemoryView` exposes each item's lineage, trust floors, adjudication receipts, contradictions, and a purpose-derived use disposition. Retrieval may withhold or qualify content below a purpose's floor but never hides the floor from a consumer that can use the content, and no integrity level is action authority. Normative contract: `sop/standard/memory-envelope-spec.md` §2.

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

Consolidation never raises an inherited trust floor. An effective floor that differs requires a separately authorized adjudication, recorded in a digest-bound receipt that points to authority but is not itself authority; self-adjudication is barred unless the named policy permits it. Normative receipt schema: `sop/standard/memory-envelope-spec.md` §3.

The system records whether retrieved memory improved the outcome. Unused or
harmful memories decay, are corrected, or are removed according to policy.

Memory-substrate evaluation must exercise, at minimum:

- remember, update, forget, and reflect operations;
- multi-entity binding, interference, and selective forgetting;
- memory pollution and leakage into irrelevant turns;
- long-horizon capacity decay and contradictory-evidence handling; and
- general capability and instruction-following regression after memory
  activation.

These criteria adapt the measured task and limitation surfaces in Mason
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
- **ORG-058:R7** — a production harness exposes serializable native memory with export, deletion, audit, and authority controls, and passes [[ORG-062]] PMB-1 Track C governance conformance, including verified deletion (C3+C4), making it eligible for evaluation as an Exocortex acceleration layer rather than a replacement. *(Amended 2026-10-07 by the operator, Telegram 11289, the ORG-062 §6 companion amendment ratified on the T-2517 walk.)*
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
