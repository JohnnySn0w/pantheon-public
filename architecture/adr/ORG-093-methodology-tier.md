---
aliases: [ORG-093]
id: ORG-093
title: "Engineering-practices tier — the third class in the standards stack (HO-adopted practice below the goal-level ratify bar; formerly 'methodology tier')"
status: 'Ratified 2026-09-06 by the operator via Telegram 9157 (2026-09-06T16:17:18Z, one-word "ratify" following TG 9156''s observation that the /architecture umbrella directory starts making more sense with 3 tiers — the umbrella question is a separate follow-up, not blocking this ratify). Direction ratified TG 9153 (2026-09-06T15:45:24Z): "make the methodology adr so we have a full coherent gapfill at the standards level. Everything else is a yes from me." Surfaced by the TG 9151-9153 exchange when the operator identified the amendment quagmire on `sop/standard/testing-depth-and-scope.md` (Amendments A-J) and asked why he is a blocker on iterative methodology exploration.'
date: 2026-09-06
author: Orchestrator (HO)
appetite: 4
build_disposition: partly-built
scoping_ticket: T-3468
supersedes: []
extends: [ORG-012, ORG-041, ORG-092]
relates: [ORG-012, ORG-041, ORG-089, ORG-092, T-3455, T-3466, T-3467, T-3468]
labels: [doc-taxonomy, akm, methodology, ratification-tier, d3-msg-3733]
ratifiable_files: [pantheon-config:architecture/adr/ORG-093-methodology-tier.md, pantheon-config:architecture/README.md, pantheon-config:architecture/engineering-practices/README.md, pantheon-config:architecture/engineering-practices/TEMPLATE.md]
---

# ORG-093 — Engineering-practices tier: HO-adopted practice surfaces below the goal-level ratify bar

**Tier renamed + relocated 2026-09-06 (T-3472 arc, ratified via ORG-094):** the tier established here was originally named *"methodology tier"* and lived at `notes/methodology/`. Renamed to *"engineering-practices tier"* and relocated to `architecture/engineering-practices/` for register alignment (the operator TG 9168: "engineering-practices works") + umbrella unification (ORG-094). The ADR body below preserves the original "methodology" language as the historical record of the tier concept; treat "methodology" and "engineering practices" as interchangeable throughout — same tier, renamed. The pointer contract and ratification model are unchanged; only the label + physical location move.

**Status:** Ratified 2026-09-06 by the operator via Telegram 9157. **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Ratify-required. **Related:** [[ORG-012]] (information-inflow bubble-up methodology), [[ORG-041]] (ADR ratification loop), [[ORG-089]] (deference heuristic — goal-level vs below), [[ORG-092]] (RFC tier — the sibling architecture-tier surface this ADR extends), [[T-3455]] (Amendment J — the trigger case), [[T-3468]] (this arc).

**Appetite:** 4/10

## Context

ORG-092 (ratified 2026-09-06) named two standards-tier surfaces:

- **RFC** (`rfc/`) — architecture-shape: state models, invariants, evidence contracts, protocol shapes. operator-ratified.
- **SOP** (`sop/standard/`, `sop/remediation/`) — procedure-shape: ordered operator actions at a decision point or during remediation. operator-ratified.

That taxonomy left a third class unnamed: **methodology** — the ongoing exploration and refinement of *how HO does its work*. Testing methodology, review methodology, dispatch methodology, verification methodology. These are neither architecture (they don't specify system state) nor procedure (they're not stepwise runbooks). They are practice discoveries HO adopts under D3/msg-3733 (principles-as-ratification, below-goal-level methodology adopted autonomously with the operator override).

Because methodology had no natural home, methodology discoveries landed as **amendments to the closest procedural SOP**. Empirical: `sop/standard/testing-depth-and-scope.md` accumulated Amendments A-J (2026-05 through 2026-09). A-D were genuine procedure changes (CI-portability, command-output discipline) that legitimately touched the SOP body and legitimately took the operator's ratify. E-J were methodology discoveries wearing procedural clothing — H (falsification-must-be-run), I (post-merge full-sweep), J (mutation-testing gate) — that inherited ratifiable-surface treatment purely because they had nowhere else to land.

The operator's TG 9151 diagnosis: *"if we're on amendment j on an adr, we're several feet into a quagmire … what prevents me from being a blocker on what sounds like an R&D guess n check effort? exploring the problem space is something I don't need to be around for."* Accurate. The gap is structural: no methodology-tier surface exists, so every methodology iteration becomes a ratify event.

## Corpus grounding

The below-goal-level HO-adopted pattern is not new — D3/msg-3733 (2026-05-04) ratified it as principle: *methodology is HO's domain, adopted with a ledger entry, the operator override always available, no routine ratify*. Multiple SOPs already invoke this framing in their status lines ("Adopted 2026-XX-XX by Orchestrator (HO) — autonomous per msg-3733 (principles-as-ratification; below-goal-level)"). What has been missing is the durable *surface* — the file location, the naming convention, the SOP-body pointer contract — that makes methodology adoption a first-class documentary act rather than an "amendment to whatever SOP happens to be nearby."

The prior art in adjacent industries points at the same shape:

- **Engineering guides / playbooks** — many organizations keep a distinct "engineering practices" or "playbook" surface separate from both architecture docs and step-by-step runbooks (Google's internal engineering-practices, ThoughtWorks Technology Radar).
- **Research notebooks / lab notes** — long-lived scientific traditions distinguish rigorous decisions (papers) from ongoing methodology refinement (lab notebooks). Pantheon's `notes/` already plays the lab-notebook role for one-off observations; a `methodology/` subdir gives long-lived methodology its own shelf.
- **Rust's RFC-vs-book distinction** — the RFC surface holds design decisions; The Book (and rustc dev-guide) holds evolving methodology and craft. Different rhythms, different bars.

## Decision

**Establish `methodology` as the third standards-tier class**, sibling to RFC (architecture) and SOP (procedure).

### Location + naming

- **Location:** `architecture/engineering-practices/` (relocated 2026-09-06 per ORG-094; original ORG-093 landing shipped at `notes/methodology/` as a subdirectory under existing `notes/`).
- **Naming:** `architecture/engineering-practices/YYYY-MM-DD-<slug>.md`.
- **Original rationale for the notes/methodology/ shape** (superseded by ORG-094 umbrella-unification): the shape was framed as *lab-notebook-adjacent*, not architecture-adjacent — grouping under `notes/` signaled epistemic register (evolving practice) rather than architecture-decision surfaces (adr/ rfc/). ORG-094 reversed this framing: the tier IS architecture-adjacent when the umbrella carries both normative and descriptive surfaces; `architecture/engineering-practices/` sits sibling to `architecture/adr/` + `architecture/rfc/` under one root.

### Ratification model

- **HO-adopted under D3/msg-3733.** No routine the operator ratify. The status line names the adopting persona + adoption date + originating ticket/observation.
- **the operator override always available.** He may raise any methodology to ratify tier by directive; HO acknowledges via a status-line update naming the override event.
- **Ledger entry required** on adoption — audit trail via ORG-001 / P4 stays intact.

### Cross-reference contract with SOPs

- **SOPs reference methodology pins by pointer.** SOP body says *"see `architecture/engineering-practices/YYYY-MM-DD-<slug>.md` for the methodology governing X"*. SOP body does NOT inline the methodology.
- **SOP body changes only when the PROCEDURE changes.** Methodology iteration does not force SOP-body edits. Base SOP ratifies once; methodology under it iterates HO-domain.
- **Discovery:** SOP index links to the methodology pins it references. Methodology dir has its own README listing every methodology pin with a one-line hook (same shape as adr/README.md, rfc/README.md).

### Graduation path

A methodology pin may (rarely) graduate:

- **To an SOP** — if the practice matures into a stable ordered procedure. Single ratify event (the operator); the methodology pin retires to a pointer at the ratified SOP.
- **To an RFC** — if the practice matures into an architecture-shape invariant/state-model. Single ratify event (the operator); the methodology pin retires to a pointer at the ratified RFC.
- **Or stays HO-domain forever.** Not every methodology needs to graduate. Persistent HO-domain methodology is a legitimate terminal state.

### Retroactive application to the T-3455 quagmire

Amendments H, I, J on `sop/standard/testing-depth-and-scope.md` extract to methodology pins:

- `architecture/engineering-practices/2026-07-28-falsification-must-be-run.md` (Amendment H)
- `architecture/engineering-practices/2026-08-27-full-sweep-merged-tree-before-landing.md` (Amendment I)
- `architecture/engineering-practices/2026-09-06-mutation-testing-gate.md` (Amendment J — folds T-3341's ruling + metis's deployment conditions + `tools/mutation_gate.py` graduation)

The SOP body's Amendment H/I/J sections retire to one-line pointers into `architecture/engineering-practices/`. Amendments A-D (real procedure changes) stay in the SOP body — they legitimately earned their ratify.

T-3455 (Amendment J ratify), T-3466 (SPLIT proposal for testing-depth-and-scope), and T-3467 (SPLIT proposal for ticket-flow) close as reshaped-under-methodology-tier: T-3455 becomes an HO-adopted notes/methodology pin (no the operator ratify), T-3466's split direction becomes moot (methodology extraction handles the mixed-tier problem), T-3467 similarly.

## Consequences

**Immediate:**

- HO methodology iteration (mutation-testing, falsification discipline, review lenses, dispatch practice) stops consuming the operator's ratify bandwidth via the SOP-amendment mechanism.
- Amendment quagmire pattern retires structurally: methodology has its own home.
- operator-visible ratify queue shrinks — only genuine architecture (RFC) or procedure (SOP) decisions surface for his gate.
- One new subdirectory in the tree (`architecture/engineering-practices/`). Zero deletions.

**Downstream:**

- Future methodology discoveries land directly in `architecture/engineering-practices/`, not as SOP amendments.
- Existing SOPs may see cleanup passes where methodology-inside-procedure gets extracted (T-3455 arc's H+I+J is the first, opt-in for others).
- The three-class standards stack is now complete: RFC (architecture-decides) + SOP (procedure-runs) + methodology (practice-evolves).

## Rejected alternatives

- **Top-level `methodology/` directory** — originally rejected (this ADR's landing), for a reason later reversed by ORG-094: the ORG-093 rationale was "visual weight of a top-level dir signals architecture-tier commitment, which is the opposite of the HO-domain-iteration shape methodology represents; subdir under `notes/` better matches the lab-notebook register." ORG-094 (2026-09-06 T-3472) reversed this by unifying normative + descriptive surfaces under one `architecture/` umbrella — resolving the visual-weight concern by nesting under `architecture/` rather than at repo root. The eventual location `architecture/engineering-practices/` is neither top-level nor buried; the tier concept is unchanged.
- **Numbered methodology IDs (M-NNN)** — rejected. Numbering signals operator-facing enumeration; methodology is HO-domain. Date-slug filenames match `notes/` convention and stay register-appropriate.
- **Keep the amendment mechanism, just document that it's HO-adopted below-goal-level** — rejected. The operator's TG 9151 diagnosis is that amendment-cruft on ratified surfaces IS the friction, regardless of whether individual amendments are formally HO-adopted vs operator-ratified. The doc-level pattern needs to change, not just the adoption tier.

## Prior art

- **[[ORG-012]]** — information-inflow bubble-up methodology. Names the tiers observations promote/demote across; this ADR adds the durable *surface* for methodology at the appropriate tier.
- **[[ORG-092]]** — RFC tier establishment. Same shape of decision (name a doc class, name its ratification model, name its cross-reference contract), applied to the third-tier gap ORG-092 did not name.
- **D3 / msg-3733** (2026-05-04) — the principle that methodology is HO-adopted below-goal-level. This ADR gives that principle a durable file location.

## Unresolved

- **`sop/standard/` audit follow-through (T-3465):** the audit surfaced testing-depth-and-scope + ticket-flow as SPLIT candidates. Under the methodology tier, testing-depth-and-scope's split need dissolves (methodology extraction handles it). ticket-flow's split need persists (it's genuinely architecture-half + procedure-half, not methodology). Handle post-ratify per-artifact.
- **Do amendments retire in one pass or incrementally?** Preferred: incrementally, opt-in per SOP as HO does cleanup work. No forced migration pass on all existing amended SOPs.
- **Does mnemosyne have her own methodology surface?** She's the second head with her own directory tree. Same shape applies (architecture/engineering-practices/ under her own repo/notes surface) — but this ADR's scope is the pantheon-config tree.
