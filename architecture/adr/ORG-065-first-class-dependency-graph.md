---
aliases: [ORG-065]
title: First-class dependency graph — typed edges + ADR→obligation→ticket→evidence lineage with closure validation
status: Ratified 2026-08-11 by the operator (via kino PWA — T-2672 resolve + Telegram confirm TG7647). Directed 2026-08-08 (relayed via codex, confirmed TG7326), alongside the T-2169 closure-discipline ratification. Final DAG-acyclicity critique (TG7646) folded pre-ratify — §3.6.
date: 2026-08-08
author: Orchestrator (Orchestrator)
relates: ORG-003, ORG-013, ORG-015, ORG-019, ORG-022, ORG-034, ORG-038
build_disposition: needs-build
scoping_ticket: T-2840
# elements: — ORG-065 machine index of its own §3.1-3.6 ("Decision"),
# "Migration", and "Verification / acceptance" (4 named bullets) sections
# (T-2946 Tranche A, pair 5/8). Sequential in document order: E1-E6 ==
# §3.1-3.6; E7 == Migration; E8-E11 == the 4 verification bullets
# (Anti-leak/Lineage query/No regression/Acyclicity). Each entry's text
# opens with its native heading verbatim — that text is the audit
# anchor, not the E-number. Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "§3.1 Node types: adr · obligation (ADR element, first machine identity) · ticket · commit · test · evidence/observable — the lineage chain ADR -> obligation -> ticket -> commit/test/evidence"}
  - {id: E2, text: "§3.2 Edge type taxonomy: depends_on/parent_of/child_of (existing) plus new implements/verifies/supersedes/informs-vs-blocks, absorbing existing fields rather than replacing them"}
  - {id: E3, text: "§3.3 Closure validation: a node with implements: ADR-NNN#E1 cannot resolve unless E1's clauses each have a satisfying verifies edge — structurally subsumes the T-2167 prose discipline"}
  - {id: E4, text: "§3.4 Lineage as a query: any node traces to origin (ADR/obligation) and proof (commit/test/evidence); the ADR-element coverage audit becomes a graph query at element granularity"}
  - {id: E5, text: "§3.5 Binding refinements (codex ORG-041 review): 5 binding requirements — clause-level obligation identity, evidence-as-receipt not link, stable namespaced ids, evidence-authority for high-risk obligations, migration surfaces uncertainty never invents"}
  - {id: E6, text: "§3.6 Acyclicity is a binding invariant: the gating/structural edge set (depends_on/implements/verifies/parent_of/supersedes, per the T-2841 two-domain operative reading) must stay acyclic — rejected at edge insertion and flagged in the migration walk"}
  - {id: E7, text: "Migration (full walk, post-construction): one deliberate full-corpus pass maps every existing ticket's implicit edges onto the typed model after the validator is built, then flips the validator to gating — a bounded one-time arc, not backfill-on-touch"}
  - {id: E8, text: "Anti-leak (verification bullet): a re-run of the T-2163 scenario — a two-clause obligation with only one satisfying verifies edge — cannot close; the validator names the unsatisfied clause"}
  - {id: E9, text: "Lineage query (verification bullet): reproduces the ADR-element coverage check the T-2167 discipline does by hand, at element granularity, over the whole corpus"}
  - {id: E10, text: "No regression (verification bullet): existing depends_on cascade, ready, cycle detection, and PWA dependents behavior are unchanged — the typed graph is a superset"}
  - {id: E11, text: "Acyclicity (verification bullet, the DAG invariant, §3.6): an edge insertion that would create a cycle in the gating/structural edge set is rejected with the offending path named; the migration walk flags any pre-existing cycle before the validator gates"}
---
# ORG-065 — First-class dependency graph for the ticket / ADR / evidence lineage

**Status:** **Ratified 2026-08-11 by the operator** (via kino PWA — T-2672 resolve, note "Ratified. this will need some planning" — + Telegram confirm TG7647). Directed 2026-08-08 (relayed via the codex mailbox, confirmed on Telegram TG7326) at the same time he ratified T-2169 — the prose closure-discipline this ADR is the structural version of. His final critique (TG7646 — the graph must be an acyclic DAG) was folded before ratify (§3.6). **HITL: Ratified.** Design-first: this ADR fixes the model and the migration shape; it deliberately does **not** commit to tooling implementation — the buildout is a follow-up per the "this will need some planning" note, scoped by **T-2840** per the ADR→buildout planning SOP (`sop/standard/adr-to-buildout-planning.md`).

## Intent

Turn the dependency/lineage graph the ticket system **already runs implicitly** into a **first-class, typed, machine-checkable** graph — so that (a) any node traces to both its origin (which ADR/obligation) and its proof (which commit/test/evidence), and (b) a node cannot close until its obligations are actually satisfied. This serves traceability (P4) by making lineage a query rather than a grep, and serves structural-enforcement (P1) by moving the anti-leak guard from human discipline into the closure path. It is the structural counterpart to the T-2167/T-2169 prose discipline ("quote the element, report every observable"), which papers over the same gap by asking a human to read carefully at closure time.

## Context — the implicit graph we already run, and where it leaks

The repo already contains a substantial dependency graph. It is not modelled as a graph; it is **scattered across single-purpose frontmatter fields, `ticket.py` transition machinery, two lint modules, the grooming digest, and the PWA**, and — critically — **nothing validates that a node's obligations are met before it closes.**

**Edges that exist today** (source: `tools/ticket.py` schema `:94-127`):

| Field | Points at | Behavior today |
|---|---|---|
| `depends_on: [T-…]` | tickets | The one true dep edge — with behavior. Cascade-unblock on resolve (`ticket.py:2660-2760`), `ready` gate (`:3624`), cycle/stale detection (`ticket_lint.py:959`), PWA reverse-dependents (`ticketRoutes.ts:166`). |
| `parent_ticket: T-…` | one ticket | Lineage back-reference only. Set on spawn; **no cascade, no validation.** |
| `ratifiable_files: [<repo>:<path>]` | committed artifacts (ADRs/docs) | The ADR/doc→ticket ratification edge (ORG-015). Drives ratify-lift of the doc `**Status:**` line on resolve, and the ratify-coverage join (`ticket_lint.py:848,882`). Operates at **file** granularity. |
| `intent` (`action`/`ratify`/`noodle`) | classifies operator-target tickets | Gates which close verb applies; `ratify` requires `ratifiable_files`. |
| `serves` (free text, ORG-034) | an arc/goal | Stored only. **Not machine-consumed as an edge.** |
| `labels: adr-<NNN> + build` | an ADR file | Labels-as-edges: the join key for the grooming "ratified-unbuilt-ADR" check (`ticket_grooming.py:1766`). File granularity. |
| `not_before` (ORG-019) | calendar | Date gate, not a ticket edge. |

**The three load-bearing gaps:**

1. **No closure validation of obligations.** `_validate_operator_close` (`ticket.py:~2545`) checks only `target==operator` and that `intent` matches the verb. A parent resolves with open dependants by design (that is what cascade is for); a ratify ticket's `ratifiable_files` clauses are never checked as satisfied — the ratify-lift blindly rewrites the doc `Status:` line on resolve. The only obligation enforcement is a **creation-time** anti-dead-end gate (`:1622-1681`), not a closure-time one.

2. **Lineage is prose below file granularity.** ADR "elements" (E1..E5) are pure prose convention — there is **no `elements:` frontmatter and no machine model of them.** A ticket says which element it satisfies only in its resolve note. The just-ratified closure-discipline section (`sop/standard/ticket-flow.md:69-108`) is explicit that this is "documentation requirements at closure time, not tooling — deliberately." The only machine-readable ADR↔ticket links (`ratifiable_files` lift + `adr-<NNN>`/`build` label join) operate at **file/ticket** granularity, never **element** granularity — exactly the granularity where T-2163 leaked (ADR-211 E1 was a two-clause element; the child ticket did one clause, closed green, and the accounting abbreviated the element to its first clause; the gap surfaced in production two months later).

3. **The graph is scattered, not addressable.** There is no single edge vocabulary and no one place to ask "what does this node depend on, implement, ratify, verify?" — the edges live in different fields with different consumers, and `parent_ticket`/`serves`/labels carry no behavior.

## Decision

Model the existing implicit graph as a **first-class typed graph**, expressed in **frontmatter (single source, in-repo, audit-chain-preserving — D10/P1)**, with a validator that gates closure. Four parts, each mapped to the operator's directed coverage areas.

### 3.1 Node types (the lineage chain)

`adr` · `obligation` (an ADR element — first time given a machine identity) · `ticket` · `commit` · `test` · `evidence/observable`. Nodes are the artifacts that already exist; the new thing is giving ADR **obligations** an addressable identity (`ADR-NNN#E1`) so the chain **ADR → obligation → ticket → commit/test/evidence** is expressible end to end.

### 3.2 Edge type taxonomy (beyond the single `depends_on`)

A typed edge set that **absorbs the existing fields** rather than replacing them, plus the missing types:

- `depends_on` — hard gate (A blocked until B done). *Exists (behavior via ORG-013).*
- `parent_of` / `child_of` — decomposition lineage. *Exists (`parent_ticket`), gains a place in the taxonomy.*
- `implements` — ticket → ADR obligation (`ADR-NNN#E1`). **New; the element-granularity link that closes gap #2.**
- `ratifies` — ratify ticket → committed file diff. *Exists (`ratifiable_files`/ORG-015).*
- `verifies` — commit/test/evidence → obligation. **New; carries the "report every observable" half of T-2167.**
- `supersedes` — node → node it replaces. **New as a ticket edge** (today `supersede-ask` only edits a ticket's own body, `ticket.py:3374` — it is not a relationship).
- `informs` vs `blocks` — advisory notify vs hard gate (the ORG-019 gate distinction, made explicit so a soft dependency need not stall a ticket).

### 3.3 Closure validation (the load-bearing change)

A node **cannot close until its outbound obligation edges are satisfied.** Concretely: a ticket carrying `implements: ADR-NNN#E1` cannot resolve unless E1's clauses each have a satisfying `verifies` edge (a commit/test/evidence node), and the resolve surfaces the element text so the coverage is checkable against the obligation body, not a label. This **structurally subsumes** the T-2167 prose discipline: the graph forces reading the obligation because closure checks the *edge to the obligation*, not a human's recollection of it. `depends_on` closure is unchanged (cascade already handles it); only obligation/verify edges newly gate closure.

### 3.4 Lineage as a query

Any node → trace to origin (which ADR/obligation) and to proof (which commit/test/evidence). The ADR-element-coverage audit that T-2167 does by hand (and that `collect_ratify_coverage` does at file granularity) becomes a graph query at element granularity — "which obligations have no satisfying `verifies` edge?" is the leak detector, run continuously, not a one-off grep.

### 3.5 Binding refinements (codex ORG-041 review, 2026-08-11 — operator-discussed)

Five requirements are **binding before implementation** — without them the model *draws a graph*; with them it *prevents false closure*:

1. **Clause-level obligation identity.** Obligations are addressable at the CLAUSE, not just the element: `ADR-NNN#E1.C1`, `#E1.C2`, … so the validator proves *each clause* independently has a satisfying `verifies` edge instead of interpreting the element's prose. This is the exact T-2163 leak (a two-clause element the child ticket half-satisfied, closed green): clause identity makes each clause a checkable node. The `elements:` frontmatter block (resolved-decision 2) carries clause sub-IDs.
2. **Evidence is a receipt, not a link.** A `verifies` edge must point at an actual test/run **receipt** (a CI run id, a test result, a captured observable), NOT merely a commit link — a commit is where work happened, not proof it passed.
3. **Stable, namespaced identities** for commit/test/evidence nodes, so an edge resolves to exactly one artifact (mirrors the ledger-id uniqueness contract).
4. **Evidence-authority for high-risk obligations.** Only authorized CI/reviewer-produced evidence may satisfy a high-risk obligation — a self-asserted receipt cannot close a high-risk clause (mirrors the S3* authorship-independence contract, ORG-038).
5. **Migration surfaces uncertainty, never invents.** Where the corpus-walk's inferred `implements`/`verifies` edges are UNCERTAIN, surface the link for human review rather than silently committing it — a silently-invented lineage edge is worse than a missing one (it reads as proof).

### 3.6 Acyclicity is a binding invariant — the graph is a DAG (the operator, TG 7646)

The typed graph MUST remain acyclic across its **gating/structural** edge set — `depends_on`, the `implements`/`verifies` edges that gate closure (§3.3), `parent_of`, and `supersedes`. A cycle there is not cosmetic: it deadlocks the system — a `depends_on` loop makes nothing `ready`, an obligation/`verifies` loop makes closure-validation non-terminating (a node waits on proof that transitively waits on itself), and a `supersedes` loop corrupts replacement history. Enforced two ways: **(a) at edge insertion** — an edge that would close a cycle is rejected and the validator names the offending path, generalizing the three-color-DFS the existing `detect_dep_graph_issues` (`ticket_lint.py:959`) already runs for `depends_on` to the full typed set (not left as a property incidentally inherited from the legacy single-edge detector); **(b) in the migration walk** (§Migration) — the full-corpus pass runs cycle detection over the reconstructed typed graph and surfaces any loop for human resolution *before* the validator flips to gating. **Advisory `informs` edges are exempt** — they gate nothing, so a notify-both-ways relationship is legitimate and a cycle among them is harmless. This makes acyclicity a first-class check standing alongside closure-validation.

> **Operative clarification (2026-08-12, BT1 construction — T-2841; surfaced to the operator with redirect option).** Building this section against the live corpus falsified one literal reading: treating the enumerated edge set as a single unioned cycle-check graph flags **15 legitimate, years-old instances of the standard parent/child idiom** (a parent ticket `depends_on` its own spawned child, whose `parent_ticket` names the parent back — e.g. T-0173↔T-0191). That idiom is not a deadlock: `parent_ticket` is a lineage back-reference carrying **no cascade and no gating behavior** (this ADR's own §3.2 characterization) — the same no-behavior rationale that already exempts `informs`. The operative invariant as built is therefore: **`{depends_on, implements, verifies, supersedes}` form ONE unioned acyclicity domain** (each carries real cyclic-failure consequences named above, and cross-type cycles among them — e.g. A `implements` B while B `depends_on` A — are caught); **`parent_of` is checked as its own separate domain** (a pure-parent cycle still flags) but is never unioned with the rest. Reached by implementer/reviewer adversarial convergence (T-2841 transition log carries the full trail); **BT2/BT5 inherit this per-domain reading as the operative invariant — do not re-derive the union.** The operator CONFIRMED this reading 2026-08-12 (TG 7784, "keep it") after a plain-language walk-through of the two-domain split — it is the binding form and the redirect option is closed.

## What this builds ON (not over)

Five partial graph implementations already exist and this ADR **extends** them — it must not reinvent them:

1. `detect_dep_graph_issues` (`ticket_lint.py:959`) — a corpus `depends_on` graph builder with three-color-DFS cycle + stale-edge detection and a `DepGraphFinding` type. **The graph-builder to generalize to typed edges.**
2. PWA reverse-dependents + knock-on panel (`ticketRoutes.ts:166`, `TicketDetail.svelte:141`) — a rendered one-hop dependents view. **The render surface to extend to typed edges.**
3. ORG-013 cascade (`_cascade_unblock_dependants`) — atomic edge-mutation-on-resolve. **The precedent for behavior on an edge, extended to closure validation.**
4. Ratify coverage join (`collect_ratify_coverage`/`detect_orphan_ratifications`) + `adr-<NNN>`/`build` label join — the existing ADR↔ticket edges to fold into the taxonomy.
5. `adr_reeval_walk.py`'s structured `re_evaluate:` frontmatter — **the one precedent for machine-readable obligations in an ADR**; the model for `obligation` node identity.

## Alternatives considered

- **(A) Keep prose discipline only (status quo + the T-2169 ratification).** The measured leak rate is low — a corpus-wide grep for conjunction-bearing ADR elements found exactly two, one leaked and one clean. WHY NOT SUFFICIENT: it relies on a human reading the element body at every closure, offers no query surface, and does not scale as the ADR corpus grows. It is the right *stopgap* (already ratified) and the wrong *end state*.
- **(B) Type-tag `depends_on` without closure validation.** Cheaper, gives the taxonomy, but leaves the load-bearing gap (closure still doesn't check obligations) — it would model the leak without catching it.
- **(C) External graph DB / dedicated tool.** Over-engineered for a single-human org and breaks the in-repo audit chain (P4). Frontmatter-as-edges + an in-repo validator keeps the graph greppable, diff-able, and part of the ledger.
- **Chosen: frontmatter-typed edges + closure validator, generalizing the existing lint graph-builder** — minimal new surface, maximal reuse, closes the load-bearing gap.

## Migration (full walk, post-construction)

**Decided by the operator (2026-08-08): a full corpus walk done AFTER the system is built — do not stall the migration behind incremental backfill-on-touch.** Once the typed-graph model + validator are constructed, run one deliberate full-corpus migration pass that maps every existing ticket's implicit edges onto the typed model, then flip the validator to gating. This preserves audit-chain continuity (P4 — the walk is an explicit, reviewable pass, not a silent rewrite) while avoiding a long warn-only tail where half the corpus is typed and half isn't.

Field→edge mapping applied in the walk:

- `depends_on` → `depends_on` (unchanged).
- `parent_ticket` → `parent_of`/`child_of`.
- `ratifiable_files` → `ratifies`.
- `adr-<NNN>`+`build` labels → `implements` (ticket → ADR, refined to the specific obligation where the ADR carries `elements:`).
- `implements`/`verifies`/`supersedes` edges are inferred for the existing corpus during the walk (from resolve-note references, closure evidence, and supersede history) rather than left to accrete on-touch.

Sequencing: build the model + validator (warn-only during construction only) → full migration walk → validator gates. The walk is a bounded one-time arc, not an open-ended backfill.

## Verification / acceptance

- **Anti-leak:** a re-run of the T-2163 scenario — a two-clause obligation with only one satisfying `verifies` edge — cannot close; the validator names the unsatisfied clause.
- **Lineage query:** reproduces the ADR-element coverage check the T-2167 discipline does by hand, at element granularity, over the whole corpus.
- **No regression:** existing `depends_on` cascade, `ready`, cycle detection, and PWA dependents behavior are unchanged (the typed graph is a superset).
- **Acyclicity (the DAG invariant, §3.6):** an edge insertion that would create a cycle in the gating/structural edge set is rejected with the offending path named; the migration walk flags any pre-existing cycle for human resolution before the validator gates.

## Risks & tensions (P6 — named, not collapsed)

- **Over-formalization vs a measured-low leak rate (P7 / D1 / D12).** The prose guard already catches most of this at the cost of a sentence. Building typed-edge + closure-validation machinery for a defect class with one observed instance risks paying structural cost ahead of demonstrated need. **Poles:** structural enforcement (P1) vs cost-asymmetry (P7). **Rebalance trigger:** the leak rate rises, OR the lineage-query surface earns its keep on its own (e.g. "what's the evidence for ADR-X" becomes a routine question).
- **Migration burden** on the existing ticket corpus — bounded to a single one-time full walk after the model is built (the operator's 2026-08-08 call), not an open-ended backfill; the walk is an explicit reviewable pass, preserving audit-chain continuity (P4).
- **Schema growth** in frontmatter — mitigated by absorbing existing fields into the taxonomy rather than adding parallel ones.

## Resolved decisions (the operator, 2026-08-08 — leans confirmed)

1. **Edge storage:** extend ticket frontmatter with typed-edge fields (NOT a sidecar manifest) — D10/P1 single-source, in-repo audit. *(the operator: follow lean.)*
2. **Obligation identity:** add an optional `elements:` frontmatter block to ADRs (mirrors the `re_evaluate:` precedent) so `ADR-NNN#E1` anchors resolve. *(the operator: follow lean.)*
3. **Closure-gate scope:** only `implements`/`verifies` (obligation edges) gate closure; `depends_on` keeps its cascade semantics. *(the operator: "this should be sufficient for now.")*
4. **Relationship to ORG-003/013/015/019:** consolidate + extend, supersede none — this ADR names the graph they each built one edge of. *(the operator: yes.)*
5. **Clause-level identity + evidence-validity are BINDING** (codex ORG-041 review, operator-discussed 2026-08-11): obligations addressable at clause granularity (`E1.C1`); a `verifies` edge = a real receipt (not a commit link) with stable namespaced IDs; only authorized CI/reviewer evidence satisfies high-risk obligations; migration surfaces uncertain inferred links for human review. **See §3.5** — these are what turn the graph from descriptive into false-closure-preventing.
6. **The graph is a DAG — acyclicity is a BINDING invariant** (the operator, TG 7646, 2026-08-11): the gating/structural edge set (`depends_on`/`implements`/`verifies`/`parent_of`/`supersedes`) must stay acyclic — cycle detection (generalizing the existing three-color-DFS) rejects a cycle-creating edge at insertion and flags any loop in the migration walk before gating; advisory `informs` edges are exempt. A cycle deadlocks readiness and makes closure-validation non-terminating. **See §3.6.**

## Downstream deliverable — Kino graph-node view (the operator, 2026-08-08)

The operator directed a Kino PWA addition that renders the typed dependency graph as a **node view** (nodes = ADRs/obligations/tickets/evidence; typed edges between them) — the visual surface that makes the lineage query navigable, extending the existing one-hop reverse-dependents panel to the full typed graph. Filed as a separate build ticket; it lands after (or alongside) the graph model so it has typed edges to render.

## Relationship to prior ADRs

- **ORG-003** established the ticket system + `depends_on` as passive metadata — this ADR gives the metadata a graph.
- **ORG-013** put the first behavior on the dep edge (cascade) — this ADR generalizes "behavior on an edge" to closure validation.
- **ORG-015** defined `ratifiable_files` as the ADR/doc→ticket edge — folded in as `ratifies`.
- **ORG-019** collapsed blocking to `not_before`/`depends_on` — the `blocks`/`informs` distinction makes the gate-vs-advisory axis explicit.
- **ORG-022 / ORG-034 / ORG-038** — the `ratifiable` label pairing, the `serves` arc-intent field, and `retest_status` are adjacent edge-or-attribute mechanisms the taxonomy should reconcile (not re-implement).
