---
aliases: [ORG-062]
id: ORG-062
title: "Memory substrate evaluation methodology and build sequence"
status: Draft (Autonomous — Specialist-authored under the operator's direction, Telegram 1186; canonical-written by orchestrator; awaiting the operator ratification — extends operator-ratified ORG-058)
date: 2026-08-03
# re_evaluate: machine-checkable mirror of the "## Re-evaluate when" section,
# consumed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk.
# type: calendar|metric|event ; fires: auto-ticket (deterministic only) | surface.
# Reference schema — a deterministic entry that WOULD auto-file looks like:
#   - {id: R9, type: calendar, when: "2026-12-01", fires: auto-ticket}
#   - {id: R9, type: metric,   when: "fp_mark_count >= 500", fires: auto-ticket}
#   - {id: R9, type: event,    when: "resolves:T-2518", fires: auto-ticket}
# ORG-062:R1–R4 are all fuzzy events (not machine-decidable) → surface-only.
re_evaluate:
  - id: R1
    type: event
    when: "PMB-1 is first built and run"
    fires: surface
    text: "PMB-1 first built/run — its Track A/B curves may revise build order or the Track B threshold"
  - id: R2
    type: event
    when: "any tier fails its PMB-1 gate"
    fires: surface
    text: "any tier fails its PMB-1 gate — the pattern, not just the instance, needs reconsideration"
  - id: R3
    type: event
    when: "ORG-058:R7 fires"
    fires: surface
    text: "ORG-058:R7 fires — a production harness exposes serializable native memory with governance controls"
  - id: R4
    type: event
    when: "an independent (non-vendor) measurement overturns a [V] score"
    fires: surface
    text: "an independent measurement overturns a [V] score this ADR declined to use"
---

# ORG-062 — Memory substrate evaluation methodology and build sequence (extends ORG-058)

**Status:** **Draft / Autonomous — awaiting the operator ratification.** Authored by Specialist
(claude-research) at the operator's explicit direction (Telegram 1186, 2026-08-03); canonical
`adr/` write by orchestrator per ORG-012 (Specialist's seat cannot write `adr/`) and the T-2399
draft-in-episodic → canonical-adr precedent. **HITL:** agent-authored; ratification
pending because this extends an operator-ratified vault ADR (ORG-058).
**Extends:** ORG-058 (Exocortex-centered memory and learning lifecycle).
**Composes with:** ORG-045 (VSM), ORG-050/051 (security/secret lifecycle), and the
provider-independence goal.
**Tracking:** T-2512 (this ADR), T-2425 (evidence base), T-2515 (canonical-write + ratify routing).
**Evidence:** the two commissioned episodic syntheses of 2026-08-03 —
`knowledge/episodic/2026-08-03-agent-memory-evidence-synthesis/` and
`knowledge/episodic/2026-08-03-memory-architectures-what-to-build/`.

## Context

ORG-058 defined the memory *lifecycle and envelope* but deliberately scoped out
storage-engine selection and left an open evaluation surface: its §6 requires that
"memory-substrate evaluation must exercise" a named minimum, and R7 gates re-evaluation
of native memory on a production harness exposing serializable native memory with
governance controls. ORG-058 did not say *how* a substrate is evaluated, *in what order*
the memory organ is built, or *what the current evidence supports*.

Two commissioned syntheses (T-2425, T-2512) now supply that. Their load-bearing
conclusions:

- **Native/latent memory is not a replacement for external memory** — the Metis authors
  say so; measured, on the external benchmark native memory scored *below* plain RAG, and
  storing irrelevant memory cost −22pt strict instruction-following (T-2425 §1.3, §1.6).
- **No architecture dominates; match to workload** — so the right unit of decision is a
  *lifecycle + swappable substrate*, not a product pick (T-2512 §1). This corroborates
  ORG-058's storage-agnostic posture and the provider-independence goal.
- **The governance primitives ORG-058's envelope requires are the recognised open
  problem** — no published architecture covers all nine; post-deletion verification is
  absent field-wide (T-2425 §4.2). Yet the *provenance/supersession/audit* half is built
  and measured (Zep bi-temporal graphs, Tiermem provenance-backed tiering, MOSS
  auditability; T-2512 §2–3).
- **Pantheon's git/ledger/ticket substrate already implements the governance primitives
  these systems are adding**, and is behind only on retrieval/consolidation/semantic-
  indexing (T-2512 §4).
- **Vendor benchmark scores are not decision inputs** — they are self-reported and
  confound-laden (MemDelta: embedding-swap alone moves +6.2pp; Mem0 ties matched RAG at
  ~50× cost). Architectures are inspectable; scores are not (T-2425 §2.2–2.3).

## Decision

### 1. The substrate evaluation gate is PMB-1

Any candidate memory substrate or tier is evaluated by **PMB-1** (defined in the T-2425
report §6): a five-track benchmark — (A) ORG-058 §6 operational conformance, (B) a
**blocking** capability-regression gate, (C) governance-envelope conformance, (D)
adversarial, (E) Bridge/Exocortex workload realism — built with MemDelta-derived controls
(matched-RAG baseline, full-context ceiling, random-retrieval control, ≥2 model families,
write-path cost, amnesiac floor control on Forget). **No tier ships without passing its
PMB-1 gate.** A substrate that is accurate but fails Track C (governance) or Track B
(>5-pt strict-IFEval regression at active stage) is disqualified for any authority-bearing
or orchestration role regardless of accuracy.

### 2. The memory organ is built additively over the governed substrate, in dependency order

The build is a **layer added over** the existing git/ledger/ticket substrate, which
remains the provenance, audit, supersession, and authority baseline — not a product that
re-implements those more weakly. Sequence:

1. **Episodic retrieval tier** — provenance-backed hot/warm/cold tiering (Tiermem pattern)
   indexing what we already store (tickets, reports, ledger, memory), each compressed
   memory keeping a backward link to source. Gate: PMB-1 Track A1/A-retrieve + E3
   (purpose-bounded retrieval).
2. **Semantic/relational tier** — bi-temporal validity edges (Zep/Graphiti pattern):
   never overwrite; close-and-supersede with (valid-from, valid-to) + ingestion time — the
   same discipline as the append-only ledger. This is where ORG-058's
   confirm/correct/supersede cycle and R2 live. Gate: PMB-1 Track A2/A9 + C5.
3. **Consolidation as idle-time compute** — reviewed consolidation (Letta sleep-time
   pattern) on the watch cadence / quiet windows the fleet already runs, honouring
   ORG-058 §6's review gates. Gate: PMB-1 Track B + C.

### 3. Native memory and self-evolving world models are watch-only

Native/latent memory (Metis class) and self-evolving world-model substrates (MemOS class)
are **not built** under this ADR. They trip ORG-058 §6's "identity cannot be silently
learned" gate and R7's serializable-with-governance precondition, and carry the measured
−22pt instruction-following cost. They are re-evaluated only on **ORG-058:R7**, and only
via PMB-1 Track C (paper/API inspection), which on current evidence eliminates them before
any GPU is committed.

### 4. Evidence-bounded posture on claims

Substrate decisions separate **[M] measured / [V] vendor / [R] roadmap** (T-2425
discipline). Vendor benchmark scores are recorded as [V] and never used as a decision
input; only independently-measured or architecture-inspectable facts gate a decision.

## Consequences

- The Exocortex build has a disciplined, falsifiable acceptance path: build a tier, prove
  it on PMB-1, then ship it — no substrate is committed before passing a test it could
  fail.
- ORG-058's storage-agnostic posture is operationalised without lowering ORG-058's
  altitude: the policy stays in ORG-058, the evaluation/build detail lives here.
- The governed substrate is affirmed as the memory baseline; the field's provenance/audit
  designs are copied *into* it, not swapped *for* it.
- Native-memory adoption is deferred with an explicit, evidence-linked re-open condition
  rather than a flat rejection.

## Re-evaluate when

- **ORG-062:R1** — PMB-1 is first built and run (its Track A/B curves may revise the build
  order or the Track B threshold);
- **ORG-062:R2** — any tier fails its PMB-1 gate (the pattern, not just the instance, needs
  reconsideration);
- **ORG-062:R3** — ORG-058:R7 fires (a production harness exposes serializable native
  memory with governance controls);
- **ORG-062:R4** — an independent (non-vendor) measurement overturns a [V] score this ADR
  declined to use, changing the build-vs-watch call for a substrate.

## DECISIONS

- **PMB-1 is the substrate gate, with a blocking governance + capability-regression track
  — serves epistemic and operational safety over headline accuracy — justifies via P1,
  ORG-050, and D1 (bandwidth: a failing substrate is caught before it ships, not after).**
- **The memory organ is additive over the governed substrate, not a replacement — serves
  continuity and provider-independence — justifies via ORG-058, ORG-045, and the
  resilience goal.**
- **Native memory is watch-only pending R7 — serves human authority over identity and
  measured instruction-following safety — justifies via ORG-058 §6, P4, and D3.**
- **Vendor scores are never decision inputs — serves truthful evaluation — justifies via
  P1 and the verification-tier discipline.**

## §6 — Recommended companion amendment to ORG-058:R7 (orchestrator disposition — routed to the operator, NOT applied)

Specialist flagged, and orchestrator concurs, that ORG-058:R7 would be sharpened by tying its fuzzy
"governance controls" phrase to the concrete PMB-1 gate this ADR defines. R7 currently
reads:

> **ORG-058:R7** — a production harness exposes serializable native memory with export,
> deletion, audit, and authority controls, making it eligible for evaluation as an
> Exocortex acceleration layer rather than a replacement.

Recommended sharpening — append: *"…and passes PMB-1 Track C governance conformance,
including verified deletion (C3+C4)."*

**orchestrator disposition (the T-2515 decision Specialist routed up):**

- **Endorsed on the merits.** It converts a fuzzy re-open precondition into a falsifiable,
  testable one, consistent with this ADR's whole evidence-bounded thrust. Good ADR hygiene.
- **NOT applied to ORG-058 here, and not applicable before this ADR ratifies.** Two reasons,
  and they compound: (1) it modifies **operator-ratified** ORG-058 text, which is a
  ratification action reserved to the operator, not a canonical-write edit for orchestrator to make;
  (2) it **forward-references PMB-1**, which is *defined in this ADR* — a ratified ADR
  cannot be sharpened to depend on an unratified one. The sharpening is therefore
  intrinsically **coupled to ORG-062 ratification**: PMB-1 must exist (ORG-062 ratified)
  before R7 can name it.
- **Routing:** forwarded to the operator as a *recommended companion amendment*, decidable in
  the same ratification action as ORG-062 — if he ratifies ORG-062, he may ratify the R7
  edit concurrently (PMB-1 then exists). ORG-058's ratified text is left untouched until
  the operator rules. If the operator ratifies ORG-062 but declines the R7 edit, ORG-062 stands
  unchanged and R7 keeps its current wording (ORG-062:R3 already references R7 as-is).
