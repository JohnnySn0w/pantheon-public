---
aliases: [ORG-054]
id: ORG-054
title: "Project ontology and recursive upward justification"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2414
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
  - {id: E0, text: "ADR as a whole: Project ontology and recursive upward justification — whole-document element for plan/elaboration tickets (ORG-065 pass-2 policy, T-3205)"}
---

# ORG-054 — Project ontology and recursive upward justification

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-034. **Source:** the ORG-053 capture note. **Scope:** what a
project is and how work justifies upward; repository realization is ORG-055.

## Context

Pantheon currently has north stars, arc-intents, tickets, repositories, daemons,
and products, but “project” is used for several of these at once. A repository
can be mistaken for the project it packages; a long-running program can be
mistaken for its current backlog; and leaf decisions can satisfy P/D method
while their contribution to the intended whole remains implicit.

ORG-034 fixed the missing intent tier. The Grand Project now requires the same
clarity at project boundaries and a recursive link among organ projects.

## Decision proposal

### 1. Define a project as a bounded developmental system

A Pantheon project has these required fields, whether represented in one file
or resolved across canonical records:

| Field | Meaning |
|---|---|
| `project_id` | Stable logical identity independent of repo/path/name changes |
| `telos` | What change in the world or relationship the project exists to produce |
| `parent_goal_ref` | Optional parent north star or project telos; `null` for a top-level independent project |
| `boundary` | What is inside, outside, and deliberately not owned |
| `interfaces` | Capabilities provided and required at the boundary |
| `stewardship` | Accountable role and authority/ratification surface |
| `lifecycle` | Formation, plastic development, stable operation, replacement, split/merge, retirement |
| `evidence` | Health, user value, safety, and developmental contribution |
| `tensions` | Preserved poles and the triggers that may rebalance them |
| `operational_contract` | Deployment, data, security, cost, maintenance, and recovery obligations |

A project may be an organ of the Grand Project, supporting infrastructure, an
external project the system serves, or a top-level independent project in an
instance with no shared root. The classification and optional parentage are
explicit.

### 2. Make project identity independent of artifact topology

A project may occupy:

- one repository;
- several independently versioned repositories;
- a bounded component inside a larger repository;
- runtime services and data stores not represented by source alone; and
- canonical records in more than one project, linked by references.

Changing repository topology does not create or destroy the project unless its
telos/boundary changes. Conversely, placing unrelated work in one repository
does not make it one project.

### 3. Extend the ORG-034 chain recursively

The upward chain is:

```text
implementation decision
  -> ticket / dispatch arc-intent
  -> local project telos
  -> optional parent project telos (repeat as needed)
  -> optional instance root north star
```

P/D and project-specific principle identifiers continue to say **how** a
decision is made. The intent/telos chain says **why**. Leaf work cites the
nearest meaningful intent, not a ritual copy of a root. A chain terminates at
either an instance root or an independent top-level project telos; parent links
make the complete chain traversable when composition exists.

“Goal-neutral maintenance” remains valid under ORG-034, but it must name the
asset whose viability it preserves. Work that maps to no project or registered
goal follows the existing unmapped-work policy; it is not forced into the Grand
Project after the fact.

### 4. Separate developmental state from ticket state

Every project records one current developmental mode:

- **shaping:** problem and boundary are still being discovered;
- **infolding:** a concrete, plastic system is answering back and may reshape
  the design;
- **infilling:** established contracts make work primarily completion and
  optimization;
- **operating:** value and upkeep dominate development;
- **reorienting:** evidence or a tension trigger requires a deliberate change;
- **retiring/splitting/merging:** identity and obligations are being resolved.

These modes do not replace inbox/active/done tickets or release maturity. They
tell reviewers what kind of feedback should be expected and how expensive a
premature constraint may be.

### 5. Define project creation and closure

Creating a project is goal-level when it introduces a new telos. Before the
first implementation arc, it requires the minimum project record above. A
project joining a composed program also requires a parent-goal decision; a
standalone project records `parent_goal_ref: null`. Exploratory probes may
precede formation if labeled as such and bounded.

A project closes only when its obligations are transferred, retired, or proven
unnecessary. An empty backlog, archived repo, or stopped daemon alone is not
closure. A continuing organ with no active build work remains operating.

## Consequences

- Project formation precedes repository bootstrap. The project record and its
  optional parent/classification decision exist before repository initialization
  becomes the default first operation.
- Operationalization requires a distinct project-lifecycle SOP for formation,
  developmental-mode transitions, reorientation, split/merge, and
  obligation-aware closure; the repository-bootstrap SOP remains repository
  procedure rather than the definition of a project.
- Ticket, brief, and review surfaces must eventually carry a mechanically
  traversable `project_id` and nearest-intent reference where composition exists;
  rootless and independently multi-project instances remain valid.

- A small project catalogue can index stable identities and point to canonical
  goal, contract, repo, runtime, and evidence records.
- Tickets and ADRs gain a mechanically traversable purpose chain where
  composition exists, without repeating a root north star in every leaf or
  inventing one for rootless instances.
- Repo splits, harness swaps, and service migrations stop appearing as project
  identity events.
- Review can distinguish useful discovery during infolding from unjustified
  drift, and distinguish needed reliability during infilling from calcification.

## Re-evaluate when

- **ORG-054:R1** — the project catalogue has at least ten entries and the required fields prove too heavy or insufficient;
- **ORG-054:R2** — a project cannot be represented without multiple parent teloi;
- **ORG-054:R3** — independent organs develop genuinely conflicting root goals; or
- **ORG-054:R4** — the developmental modes do not predict different review behavior.
## DECISIONS

- **Define project by telos/boundary/lifecycle, not repo — serves durable organ
  identity — justifies via P1 and P3.** Source layout changes more often than
  purpose and cannot be the canonical identity.
- **Use optional recursive north-star links without adding a third artifact
  type — serves compatibility with both composed and independent projects —
  justifies via ORG-034 and D10.** North stars may nest; arc-intents remain
  operational, and a top-level project need not invent a parent.
- **Record developmental mode — serves the desire/system feedback loop —
  justifies via P6.** Plasticity and reliability are preserved as a tension
  rather than collapsed into one universal process.
