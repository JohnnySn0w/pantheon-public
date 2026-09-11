---
aliases: [ORG-092]
id: ORG-092
title: "RFC tier — the design specification layer between ADR decisions and implementer-hands-on-code"
status: Ratified 2026-09-06 by the operator via Telegram 9138 ("next" — implicit close after 9136 "yeah we'll skip it" resolved the layout siblings-vs-architecture-umbrella question, the only outstanding design decision on this ADR body). Direction earlier ratified TG 9127-9129 (2026-09-06T~14:30Z) during the 5-ratify desk walk-through when T-3452's `sop/standard/continuity-lifecycle.md` was recognized as a design/architecture doc masquerading as SOP.
date: 2026-09-06
author: Orchestrator (HO)
appetite: 5
build_disposition: partly-built
scoping_ticket: T-3463
supersedes: []
extends: [ORG-001, ORG-041]
relates: [ORG-001, ORG-002, ORG-041, ORG-054, ORG-078, ORG-089, ORG-090, T-3452, T-3463]
labels: [doc-taxonomy, akm, design-spec, rfc, methodology, ratification-tier]
ratifiable_files: [pantheon-config:architecture/adr/ORG-092-rfc-tier-design-specification.md, pantheon-config:architecture/README.md, pantheon-config:architecture/rfc/README.md, pantheon-config:architecture/rfc/TEMPLATE.md, pantheon-config:architecture/rfc/RFC-001-persona-continuity-lifecycle.md]
---

# ORG-092 — RFC tier: design specification between ADR decisions and code

**Status:** Ratified 2026-09-06 by the operator via Telegram 9138. **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Ratify-required. **Related:** [[ORG-001]] (ledger format), [[ORG-041]] (ADR ratification loop), [[ORG-054]] (project ontology), [[T-3452]] (misclassification incident that surfaced the gap), [[T-3463]] (this arc).

**Appetite:** 5/10

## Context

Pantheon's current doc taxonomy has five artifact classes:

- **Rulebook** (`rulebook/principles.md`, `rulebook/directives.md`, `rulebook/policies/`) — the ratified rules.
- **ADR** (`adr/`) — architecture decision records: singular decisions with rationale + alternatives + consequences, ratified by the operator.
- **SOP** (`sop/standard/`, `sop/remediation/`) — operational how-tos: step-by-step procedures HO or workers run at a decision point or when something breaks.
- **Notes / knowledge / chronicle / ledger / bugs** — reference material + audit trail + gotcha catalog.
- **Tickets / plans** — work-tracking + code-ready plans scoped to an arc.

What's missing is the layer **between an ADR's "we decided X" and an implementer's "I'm writing code Y."** ADRs are broad-strokes — they name the choice, the alternatives, the consequences. SOPs are procedures — day-to-day operational discipline. Neither is the right home for the artifact that says "here is the interface, the state model, the invariants, the acceptance shape, the drift-refusal contracts, the rollback discipline" for the ADR's decision. That artifact is a **design specification**.

The 2026-09-06 desk walk-through surfaced the gap concretely: `sop/standard/continuity-lifecycle.md` (T-3175 D2 landing) was filed at the operator's desk as an SOP ratify, but the operator correctly recognized it as a design/architecture doc — it opens with "Ratified locks (invariant across every state below)" and describes seven lifecycle states with preconditions, ordered steps, emitted evidence, and rollback triggers. That is not a "how-to run this procedure at a decision point"; that is a design specification of the ORG-057 continuity substrate.

The operator's framing: *"we're only doing part of the AKM breakout"* (Architecture Knowledge Management — the practice of formally managing architecture knowledge beyond just decision records).

## Corpus grounding

The layer this ADR names has two established names in modern software engineering practice, same shape either way:

- **"Design Doc"** (Google engineering culture, industry-dominant since ~2010; canonical write-up: Malte Ubl, *Design Docs at Google*). Typical sections: Context / Goals / Non-goals / Design / Alternatives Considered / Cross-cutting concerns (security, testing, deployment, observability). ~5–15 pages. Reviewed before implementation starts.
- **"RFC"** (Rust core, Squarespace, IETF/PEP tradition — canonical template: `github.com/rust-lang/rfcs`). Sections: Summary / Motivation / Guide-level explanation / Reference-level explanation / Drawbacks / Alternatives / Prior art / Unresolved questions / Future possibilities.

Adjacent literature that establishes the ADR-vs-RFC-vs-code layering:

- **Bass, Clements & Kazman, *Software Architecture in Practice*** — Architecture Documentation chapter distinguishes decision records (ADRs), views (perspectives), and detailed designs (implementable specs) as separate first-class artifacts.
- **ISO/IEC/IEEE 42010:2011** — "Architecture description" standard: formalizes viewpoints + views + models as the layer between decisions and code.
- **Parnas 1985, *The modular structure of complex systems*** — information-hiding: the design specification's job is to expose the interface + hide the implementation choices the ADR left underspecified.
- **IEEE 1016** — Software Design Description standard (bureaucratic ancestor of modern design docs).
- **Fowler, *Who Needs an Architect?* (2003)** — establishes design-doc purpose against architecture broad-strokes and against code.

The pattern is well-trodden; the only novelty here is Pantheon's specific integration with our existing ADR/SOP/ratify conventions.

## Decision

Establish **RFC** as a first-class Pantheon doc-class, sibling to ADR and SOP.

### Naming + location

- Directory: `architecture/rfc/` (moved from `rfc/` at repo root under the ORG-094 umbrella-unification arc, 2026-09-06 T-3472; the ORG-092 landing shipped rfc/ at repo root — that was the original decision, superseded by ORG-094 without changing the ratification model or the tier definition).
- File naming: `RFC-NNN-<kebab-case-title>.md` (three-digit zero-padded numbering, starting at RFC-001).
- Numbering is monotonic across the RFC series (not per-ADR-namespaced), same shape as ADR's ORG-NNN.
- The RFC is a **specification**, not a decision — one RFC may implement one ADR, extend an ADR family, or span multiple ADRs. The `implements:` frontmatter edge (per [[ORG-065]]) names the ADRs the RFC specifies.

### What goes in an RFC vs an ADR vs an SOP

| Class | Question it answers | Typical size | Ratify tier |
|---|---|---|---|
| **ADR** | Which alternative did we pick, and why? | ~200-500 lines | the operator |
| **RFC** | How does the picked alternative work in detail — interfaces, invariants, state model, acceptance shape? | ~300-1000 lines | the operator |
| **SOP** | Given the system exists, how does an operator/orchestrator run this procedure? | ~50-300 lines | HO (D2 authoring) |

**Test for classification:** if you can complete a code-turn on the ADR alone without needing to invent interfaces, invariants, or acceptance shapes, no RFC is required (small arcs, minor extensions). If the ADR intentionally leaves interface/invariant/state-model detail underspecified because it belongs at a lower altitude, an RFC bridges the gap.

**Test for SOP-vs-RFC:** if the artifact describes state models, invariants, lifecycle states, protocol shapes, or type contracts — it's an RFC. If it describes ordered operator/orchestrator actions at a decision point or during remediation — it's an SOP. When in doubt, RFC. The 2026-09-06 misclassification bar is: architecture-shaped artifacts (invariants + states + evidence contracts) are RFCs, not SOPs.

### RFC template

`rfc/TEMPLATE.md` (landed with this ADR) mirrors Rust's RFC template with Pantheon-specific integration:

1. **Frontmatter** — id, title, status, date, author, appetite, scoping_ticket, implements (ADR refs), extends, supersedes, relates, labels, ratifiable_files.
2. **Summary** (1-2 paragraphs) — what this RFC specifies, at the level a stakeholder skims.
3. **Motivation** — what the ADR left underspecified and why filling the gap matters now.
4. **Guide-level explanation** — the concept, invariants, and shape a reader needs to build a mental model. No code, no interfaces yet.
5. **Reference-level explanation** — the implementable spec: interfaces, invariants, state models, error contracts, acceptance shape. This is the load-bearing section.
6. **Drawbacks** — costs of doing this at all (adoption burden, blast-radius on legacy, etc.).
7. **Rationale and alternatives** — what other shapes were considered and why rejected. If the ADR already made these calls at the decision level, this section is short; if the RFC introduces sub-choices, it justifies them here.
8. **Prior art** — external systems, papers, or Pantheon precedents this RFC draws from.
9. **Unresolved questions** — what this RFC deliberately leaves for a future RFC or a implementer plan.
10. **Future possibilities** — natural extensions.

The template is a starting point; RFCs may omit sections that don't apply (e.g. RFC-001 has no meaningful "Drawbacks" since the substrate exists and the RFC only formalizes the spec).

### Review discipline

- **HO authors the draft.** RFC drafting is D2 authoring (HO-owned, same as ADR drafts). Workers can be dispatched for research/investigation feeding into an RFC, but HO holds the pen on the ratifiable body.
- **Peer-review before the operator ratify.** RFCs at appetite ≥ 5 get a reviewer subagent pre-flight against the same axes ADRs use ([[ORG-090]] severity taxonomy applies) before landing in the the operator ratify queue. RFCs at appetite < 5 (small scope, tight spec) go direct to the operator ratify per HO judgment.
- **the operator ratifies.** RFCs are architecture-committing (they define interfaces and invariants that constrain implementation), so the operator ratifies each. Status flips **Draft → Ratified** on his verdict, same shape as ADR.
- **Amendments.** Post-ratify changes to RFC body follow the same amendment discipline as ADRs (per [[ORG-041]]): editorial cross-reference updates don't need re-ratify; substantive changes to interfaces / invariants / state model do.

### `ratifiable_files:` frontmatter

RFCs use `ratifiable_files:` per [[ORG-078]] convention. The RFC file itself is always ratifiable; multi-file RFCs (spanning schema files + doc) may list all files under one ratify surface.

## Migration path

**Immediate (this arc, T-3463):**

1. Land `rfc/README.md` (index + naming convention + review discipline pointer to this ADR).
2. Land `rfc/TEMPLATE.md`.
3. Migrate `sop/standard/continuity-lifecycle.md` → `rfc/RFC-001-persona-continuity-lifecycle.md` via `git mv` (preserve history). Fix the false "Ratified 2026-09-06 by the operator" status-line bug in the process. Body content stays intact; only classification, location, and status-line change.
4. Add `## RFC ledger` section to `adr/README.md` cross-referencing the new tier (or a similar pointer in the top-level README).
5. `T-3452` stays closed per the operator's original "SOP tbqh" verdict at the classification he was ruling on. The continuity-lifecycle content's ratify need moves to RFC-001's ratifiable surface (bundled with this ADR, one ratify).

**Follow-up (separate ticket, this arc files it as `--informs T-3463`):**

Audit `sop/standard/` for other artifacts that are secretly design specifications. Candidates to sanity-check on classification-shape:
- Any SOP whose opening section is "Invariants" or "Locks" rather than "When it fires" / "Steps."
- Any SOP whose body is dominated by state models, protocol descriptions, or type contracts rather than ordered actions.
- Any SOP whose scoping_ticket is a substrate arc (T-2417, T-2953, T-3029 class) rather than a workflow arc.

Migrations from that audit each go through their own claim → RFC-tier landing → the operator ratify cycle. HO judges per-artifact; expected count is 0–3 (initial sample of 4 recent SOPs found 1 misclassified out of 4).

## Consequences

**Immediate:**

- One more artifact class to reason about when drafting or classifying documents. Naming test at Section §Test-for-classification is the discipline for staying in the right lane.
- The operator's ratify queue picks up RFCs as a new stream (RFC ratifies formatted like ADR ratifies — same brevity, same surface).
- The `T-2417 implementation-plan.md` and other plan artifacts don't retroactively become RFCs — plans live in `tickets/plans/` and are code-ready implementation guides, not ratifiable specifications. RFCs and plans complement: RFC is the ratified spec; plan is the code-ready lift from the RFC.

**Downstream:**

- Future substrate arcs (ORG-057-class, ORG-091-class) that need detailed specification will produce ADR + RFC pairs rather than ADR + SOP (which is what T-2417 originally produced by miscategorization).
- The tier makes it easier to bring in external work (Google Design Docs, RFC-shaped external specs) into Pantheon without a naming mismatch.
- The AKM gap the operator surfaced is addressed for the decision + specification pair; Architecture Knowledge Management also covers views/perspectives (Bass/Clements/Kazman chapter, ISO 42010 viewpoints) which we may separately expand later if the need arises. Not scoped to this ADR.

**Non-goals of this ADR:**

- Does NOT redefine what an ADR is (ADR-tier conventions remain per [[ORG-001]] + [[ORG-041]]).
- Does NOT change SOP semantics (SOPs remain operational how-tos per current convention).
- Does NOT establish an "architecture views" tier — that's a separate potential expansion not included here.
- Does NOT retroactively re-classify existing SOPs at scale — the follow-up audit is candidate-sample-only.

## Rejected alternatives

- **"Fold RFCs into ADRs" (single-tier).** Google Design Docs and Rust RFCs exist as separate classes from ADR-shaped artifacts specifically because the questions they answer are different (which alternative? vs. how does it work in detail?). Collapsing them loses the discipline.
- **"Fold into `notes/` or `knowledge/`".** Those directories are reference material and free-form. RFCs need ratify tier + amendment discipline + `ratifiable_files:` convention — a first-class artifact class, not an unstructured note.
- **"Call them 'Design Docs'".** Considered. Reasons for picking RFC: (a) 3-letter acronym parallels ADR, cleaner in ledger/ticket references; (b) Rust's template is the cleanest modern reference we can point implementers at; (c) "Design Doc" is industry-safe but overloaded (some orgs use it for one-pagers, others for 30-pagers). RFC is more specific.

## Prior art

- Rust RFC process (`github.com/rust-lang/rfcs`).
- Python PEP process (`python.org/peps/`).
- Google Design Docs (Malte Ubl's public write-up).
- Squarespace RFC process (internal, publicly discussed).
- Bass, Clements & Kazman, *Software Architecture in Practice* — establishes decision-record / view / design-doc layering.
- ISO/IEC/IEEE 42010:2011 — Architecture description.

## Unresolved

- Whether small RFCs (appetite < 5) should still get a reviewer subagent pre-flight, or whether HO-direct-to-operator is sufficient. Current rule (appetite ≥ 5 gets pre-flight) is a heuristic that may need calibration after 2–3 real RFCs land.
- Whether an "architecture views" tier is worth adding (Bass/Clements/Kazman's deployment/security/module-decomposition views as first-class artifacts). Not proposed here; watch whether the pattern emerges organically.
