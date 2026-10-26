---
aliases: [ORG-081]
id: ORG-081
title: "System registry — validated data plane for components and per-leg coverage"
status: Ratified 2026-08-26 by the operator via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle at goal-level authority. Frozen ratify candidate at v15 (commit 845aa519a). Companion to [[ORG-080]] (F3); the engine reads this registry. Revision history at §Revision history.
date: 2026-08-26
author: Orchestrator (Orchestrator)
steward: orchestrator
relates: ORG-046, ORG-052, ORG-053, ORG-055, ORG-063, ORG-064, ORG-065, ORG-068, ORG-076, ORG-079, ORG-080
build_disposition: needs-build
scoping_ticket: T-2667
no_registry_row_waiver: "Schema-level ADR defining the registry structure itself; registers no components. Component rows are added by the ADRs that name them ([[ORG-080]] F4 names the day-one components; project-organ ADRs name their organs). This ADR's own build tranche (T-2667 tranche a) lands the validator that enforces D-10."
---

# ORG-081 — System registry

**Status:** **Ratified 2026-08-26 by the operator via Telegram 8612** as the ORG-080 + ORG-081 + ORG-082 bundle. **Authority: goal-level** (per the operator TG 8553 2026-08-26). Frozen ratify candidate at v15 (commit 845aa519a). Companion to [[ORG-080]] (F3); the engine reads this registry. See §Revision history for the fold-chain.

**Related:** [[ORG-080]] (germination engine — the reader), [[ORG-068]] (lifecycle contract — this replaces the prose coverage matrix), [[ORG-064]] (scheduler — already has a `system:` field anticipating this key), [[ORG-076]] (health plane — the collector is a registered component here), [[ORG-052]] (Action Broker — a registered component; its typed-op registry is disjoint from the substrate ops per ORG-080 F2), [[ORG-053]] (SSEED — the components register every organ and seat), [[ORG-046]] (manifest integrity — external-repo components register via `pantheon-component.yaml` at repo root), [[ORG-079]] (persona-stack invariants — feed the harness-manager component's `verify` block).

## Intent

Give every Pantheon component — and its per-leg lifecycle coverage — one **validated data plane** the engine reads. Retire the hand-maintained [[ORG-068]] prose coverage matrix. Make "who is stewarding what leg, with what evidence" a data query with a closed schema, not a running conversation.

## Context

Today the coverage of the five [[ORG-068]] lifecycle legs across ~11 systems lives as a prose table inside `adr/ORG-068-universal-system-lifecycle-contract.md`. Every steward-refinement (Consultant on credential-broker, 2026-08-12; the ORG-076 arc-restart flags) requires a hand-edit; nothing reads the table; two of three T-2950 halts were preflight-catchable but not caught because no code walked the matrix at plan time. [[ORG-064]] already anticipated the fix: its schedule entries carry a `system:` field intended to join against an eventual registry key. [[ORG-080]] makes the join load-bearing — the engine cannot compose a plan without asking the registry "what components exist, in what dependency order, with what per-leg status."

The design draft's §3.5 chose the shape: own directory (same reasoning [[ORG-064]] used to keep `schedules/` out of `instance.yaml`), one record per component, closed status enum with evidence rules a validator enforces, a render tool that produces the matrix on demand. This ADR ratifies that shape.

## Decision

### 1. Home + shape

`registry/systems.yaml` — its own top-level directory in `pantheon-config`, sibling to `schedules/`, `credentials/`, `adr/`. One list of `systems`; **two discriminated record shapes**, keyed by the required `kind:` field.

**One authority per field.** The registry does **not** redeclare the component contract's DAG, privilege, gates, interactions, emitter shape, invariant list, or reserved-authority fields. It holds `contract_digest` (an exact reference to the component contract module authored per [[ORG-080]] §Component contract) plus coverage/adoption/evidence fields. Any digest mismatch at `plan` is a refusal. External-repo components (which declare their contract in `pantheon-component.yaml` at the repo root) additionally require a `contract.repo_revision` pin, and drift between pin and registered digest is a refusal.

Schema version pinned at the top:

```yaml
schema: pantheon-system-registry/v0
systems:
  # ─────────── component record (kind: component) ───────────
  - kind: component                                 # required discriminator
    id: <string; matches [[ORG-080]] component.id>  # authority: [[ORG-080]] component contract module
    title: <human string>
    steward: <persona name>                         # MUST equal the component contract's steward field exactly (digest-bound mirror); validator refuses mismatch at plan. No transition-window carve-out. The [[ORG-080]] contract module is the sole authority.
    contract:
      repo: <repo id>                               # pantheon-config for internal components; <project-repo> for external
      module: <path to the component contract module>
      digest: sha256:<hex>                          # must match the component module's on-disk digest at `plan` time
      repo_revision: <sha>                          # required when repo != pantheon-config; pin against the repo's ratified revision
    adr: [<ORG-id>, …]                              # ratified ADRs the component implements (informational — the contract is authoritative)
    contract_adoption: not_adopted | partial | adopted     # typed predicates per §3
    adoption_evidence:                              # required when contract_adoption in {partial, adopted}
      plan:              <evidence ref>             #   e.g. "test_harness_manager_plan_deterministic" or "T-2917 receipt"
      apply:             <evidence ref>
      receipt:           <evidence ref>
      rollback:          <evidence ref>
      crash_contract:    <evidence ref>             #   proves stageable OR write-ahead-inverse-durable for every mutating op ([[ORG-080]] D-X)
      gates:             <evidence ref>
      emitter:           <evidence ref>
      # `adopted` requires all seven to be present + evidence-covered; `partial` requires at least `plan_install` + `plan_remove` methods AND receipt-schema conformance + typed adoption_gaps entries for every absent predicate from the closed set below.
    adoption_gaps:                                  # typed gap collection for `partial` — one entry per absent PREDICATE (closed 16-key set, not just missing evidence fields)
      # required when contract_adoption == partial AND at least one of the closed predicate set below is absent.
      # keyed by predicate name (closed enum — free-form keys are validation errors); each entry is {gap: <string>, ticket: T-NNNN}.
      # forbidden when contract_adoption in {not_adopted, adopted} (validator refuses).
      #
      # CLOSED PREDICATE KEY SET, CARDINALITY 16:
      #   plan_install, plan_configure, plan_update, plan_repair, plan_remove   (the five callable lifecycle `plan_*` methods)
      #   verify_contract                                                        (declarative — the leg declares its invariant_list_ref per [[ORG-080]] §Component contract; there is no callable `plan_verify`)
      #   adoption_evidence.plan, adoption_evidence.apply, adoption_evidence.receipt,
      #     adoption_evidence.rollback, adoption_evidence.crash_contract, adoption_evidence.gates, adoption_evidence.emitter   (the seven evidence fields)
      #   crash_contract, sandbox, receipt_schema                                (the three cross-cutting predicates)
      # Total: 5 + 1 + 7 + 3 = 16 legal keys.
      # For `partial`, `plan_install` and `plan_remove` are MANDATORY (§3 partial predicate list) and therefore MUST NOT appear as gap entries; the gap-eligible upper bound on any single `partial` component is thus at most 14. The closed-set cardinality (the domain of legal `adoption_gaps` keys) remains 16.
      # Any adoption_gaps key not in this list is a validation error.
      # example:
      #   crash_contract:        {gap: "action-broker.grant.ensure lacks kill-injection test", ticket: T-2934}
      #   sandbox:               {gap: "planner sandbox profile not yet enforced under CI", ticket: T-2919}
      #   adoption_evidence.gates: {gap: "authority-transfer approval-registry not wired", ticket: T-2917}
      #   plan_repair:           {gap: "component has no plan_repair method yet", ticket: T-3050}
      # rule: every absent predicate from the closed set MUST have exactly one corresponding adoption_gaps entry. Missing entry → validation refuses. Extra entry (a predicate that IS present listed as a gap) → validation refuses. This turns "partial with a gap" into a checkable schema.
    legs:                                           # present when contract_adoption != not_adopted; per-instance CONTRACT COVERAGE (static, git-tracked)
      install:   {status: <enum>, …}
      configure: {status: <enum>, …}
      health:    {status: <enum>, …}                # predicate is emitter-install + invariant-list-digest-bound + collector-attachment/alert-route, per [[ORG-080]] §Health-plane seam
      update:    {status: <enum>, …}
      remove:    {status: <enum>, …}
    legacy_coverage: {…}                            # present ONLY when contract_adoption == not_adopted; evidence of prior art, never a claim of adoption (F4)
    note: <string>                                  # optional prose

  # ─────────── external-plane record (kind: external-plane) ───────────
  - kind: external-plane                            # discriminated union — DIFFERENT required fields from `kind: component`
    id: <string>                                    # e.g. health-plane
    title: <human string>
    steward: <persona name>
    adr: [<ORG-id>, …]                              # the ADRs describing the external plane
    tracking: {design: T-…, build: T-…, …}          # required for external-plane rows (name the tickets that carry this obligation)
    relationship: <string>                          # required — one line naming how the plane relates to the engine (e.g. "engine installs its emitters and the collector; observation is not the engine's job")
    note: <string>                                  # optional prose
    # NOTE: external-plane rows MUST NOT carry contract/contract_adoption/legs/adoption_evidence — the validator refuses.
```

**Scheduler id migration + binding-node id policy.** Registry `id` bytes are canonical everywhere and must match [[ORG-064]]'s scheduler entry `system:` field. Current schedules use human row titles (`"Harness Manager — persona seats: install/launch/supervise"` etc. — see `schedules/global.yaml:50`, `schedules/orchestrator.yaml:38`, `schedules/specialist.yaml:16`); the validator accepts those as legacy aliases ONLY during a bounded one-time migration (per-alias entry in a migration file, expiry date), never as an ongoing pattern. After migration cutover the alias file empties and legacy strings are validation errors. The synthesized `<target>-seat-binding` nodes ([[ORG-080]] §Harness Manager R9) are **ephemeral plan-node ids**, not registry ids — they are not registered rows; their receipts are written under the parent component's binding leg.

> Note (2026-10-17, Orchestrator, T-5105): cut over. `schedules/*.yaml` `system:` fields now carry registry ids and the migration file is retired; a legacy title is a validation error.

### 2. Closed status enum (schema-validated, evidence rules enforced)

- **`covered`** — requires `evidence:` (receipt id, ORG-065 obligation, or documented proof).
- **`partial`** — requires `gap:` (what's missing) **plus** `ticket:` (the open build ticket that closes it).
- **`missing`** — requires `gap:` **plus** `ticket:`.
- **`partial_by_design`** — requires `waiver:` (a named rationale for why the leg intentionally does not converge — e.g. [[ORG-052]]'s update leg that never auto-pulls a privileged UID's own executables).
- **`not_applicable`** — requires `waiver:` (a named rationale for why the leg cannot exist — e.g. `pantheon-config` self-`remove` is dissolving the org, an operator act with no legitimate tool caller).

Enum bytes are **exact**. `partial-by-design`, `n-a`, or any free-form alternate is a validation error. The validator refuses a landing that violates any of the above rules.

### 3. Adoption is a separate field from coverage (F4 locked)

`contract_adoption: not_adopted | partial | adopted` records whether the component implements the [[ORG-080]] §Component-contract itself (typed operations, sandboxed data-only planner, staged apply / write-ahead-inverse-durable crash contracts, receipts, rollback, gates, emitter with invariant-list-digest binding). While `not_adopted`, whatever the component's existing scripts prove is kept under `legacy_coverage:` — evidence of prior art, never a claim of contract adoption. A leg with a passing script but no `plan_*` method is `legacy_coverage.install.status: covered`, not `legs.install.status: covered`.

**Typed predicates.** The three enum values are not free-form:

- **`not_adopted`** — component ships no `plan_*` methods against the contract module. `legs:` MUST be absent; `legacy_coverage:` MAY be present.
- **`partial`** — the component ships at least `plan_install` + `plan_remove` methods AND writes receipts against the pantheon-receipt/v1 schema, but at least one of the **closed 16-predicate set** (§1 `adoption_gaps` schema; cardinality 16 = 5 callable `plan_*` + 1 declarative `verify_contract` + 7 `adoption_evidence.*` + 3 cross-cutting) is absent: {`plan_configure`, `plan_update`, `plan_repair`, `verify_contract`, any of the seven `adoption_evidence.*` fields, `crash_contract` on every mutating op, `sandbox` boundary, or `receipt_schema` conformance}. `plan_install` and `plan_remove` are mandatory for `partial` and therefore never appear as gap entries — the gap-eligible upper bound on any single `partial` component is at most 14. `adoption_evidence` MUST list which fields are present. The `adoption_gaps:` mapping must carry exactly one entry per absent predicate from the closed set, not just per absent evidence field. Non-leg predicates go in `adoption_gaps:`, NOT in a leg cell (sandbox / gates / crash_contract / receipt-schema gaps do not necessarily belong to one lifecycle leg; the leg cells stay for per-leg coverage, adoption_gaps stays for adoption-predicate gaps). `legacy_coverage:` MAY still be present for legs the contract does not yet cover. **`verify_contract`** is declarative rather than callable — it names the requirement that the component declare its `verify` `invariant_list_ref` per [[ORG-080]] §Component contract, digest-bound to the emitter's `invariant_list_ref`; there is no `plan_verify()` method in the contract, so the predicate reflects the interface [[ORG-080]] actually defines.
- **`adopted`** — all seven `adoption_evidence` predicates are present + covered (plan / apply / receipt / rollback / crash_contract / gates / emitter), every mutating op declares a `crash_contract`, the sandboxed planner boundary passes the CI acceptance ([[ORG-080]] §Base engine mechanics testability), and every leg cell is `covered` OR carries an explicit waiver. `legacy_coverage:` MUST be absent.

The validator refuses any inconsistency: `adopted` with a missing predicate; `partial` with no gap+ticket for a missing surface; `not_adopted` with `legs:` present.

This distinction is what keeps the day-one components (host-substrate, mailbox, harness-manager, scheduler, action-broker, credential-broker, pantheon-config, health-collector) legible against the project organs (echo, kino, watch, memory, presence, passport) whose adoption lands on their own tickets.

### 4. Health field records the installer's health-plane obligation, not the runtime verdict

The `legs.health:` cell records whether the component's obligation toward the health plane is met — the installer's obligation per D-J. It does **not** record the health plane's verdict on the component — that lives in the [[ORG-076]] episode store and is a different vocabulary. The qualifier is load-bearing: an "emitter installed + reporting `unhealthy`" fact promotes `legs.health.status` to `covered` **only after all three wiring predicates in the three-part rule below have been proven** — emitter install AND the invariant-list binding AND the collector attachment with proven alert delivery. An orphan emitter that reports `unhealthy` without proven collector attachment stays at `partial` per those three predicates. The "component is unhealthy right now" fact still belongs to [[ORG-076]], not here.

**Three-part predicate for `legs.health.status: covered`.** Emitter-install alone does NOT satisfy the unchanged [[ORG-068]] E9.C3 obligation ("failure is observable, alerting wired"). The cell promotes to `covered` only when all three are proven:

1. **Emitter installed + reporting** (unit + `OnFailure=` leaf + D-Bus exporter registered; the emitter's `Get` returns a valid property set).
2. **Invariant-list binding** (the emitter's `invariant_list_ref` and the component's `verify` `invariant_list_ref` resolve to the same `<id>@<digest>`; the validator refuses drift — the single-vocabulary rule is machine-enforced, not a prose pointer).
3. **Collector attachment + proven alert delivery** (the health-collector is subscribed to this emitter AND at least one unhealthy transition has been observed at the operator surface, providing evidence the alert path delivers). If (3) is not yet proven, the cell is `partial` with `gap: "collector attachment / alert delivery unproven" + ticket: <id>`; explicit `partial_by_design` requires a `waiver:` that acknowledges the ratified E9.C3 obligation is not being covered.

An orphan emitter with no subscription cannot promote the cell to `covered`. The companion amendment to [[ORG-068]] makes this predicate normative in the lifecycle contract itself.

### 5. Render tool + validator (build children)

Two tools land under `tools/germinate/registry/`:

- **`render.py`** — reads `registry/systems.yaml`, produces the [[ORG-068]] matrix (same rows, same column order) as markdown. `germinate registry render` prints it; a CI job commits the rendered view to `docs/system-coverage.md` (or similar) so a reader without the tool has a static copy. The hand-edited matrix in [[ORG-068]] retires; that section becomes a pointer.
- **`validate.py`** — enforces the schema + closed enum + evidence rules. Runs in CI on every PR touching `registry/systems.yaml` **and** on every PR touching `adr/ORG-*.md` (so that a new ADR that requires a new registry row cannot land without the row). A validation failure is a landing block.

**Machine-readable ADR ↔ registry link.** The claim above ("run the validator when an ADR changes" catches missing rows) is only enforceable if the validator has a machine-readable way to know which components an ADR names. Enforced via **two required ADR-frontmatter fields**:

- **`registers_components: [<component-id>, …]`** — the ADR asserts that these component ids MUST have registry rows for it to be admissible. Empty list means the ADR names no components.
- **`no_registry_row_waiver: <string>`** — mutually exclusive with `registers_components`; a named rationale for why a decision-level ADR (e.g. an amendment to a principle) does not need to register any component.

The validator refuses an `adr/ORG-*.md` landing that carries neither field OR that names a `registers_components: [<id>]` not present in `registry/systems.yaml`. This turns "the validator can detect missing rows" from a wish into a structural check.

Both tools follow the `memory_index.py` mould (~60–200 lines, one purpose, deterministic output).

### 6. External-plane rows for legibility

**Discriminated union, not an optional flag.** The two record shapes above (§1 `kind: component` vs `kind: external-plane`) are a **discriminated union**, not one record type with optional fields. The validator picks the shape from the required `kind:` field and enforces exact field sets per shape:

- **`kind: component`** — requires `id`, `title`, `steward`, `contract:{repo,module,digest[,repo_revision]}`, `contract_adoption`, and — when adoption is non-`not_adopted` — `adoption_evidence` and `legs`. Refuses `tracking:` and `relationship:` (those are external-plane-only).
- **`kind: external-plane`** — requires `id`, `title`, `steward`, `adr`, `tracking`, `relationship`. Refuses `contract`, `contract_adoption`, `adoption_evidence`, `legs`, `legacy_coverage` (those are component-only).

`health-plane` is the paradigm external-plane row: every component emits into it, but the engine's only obligations toward it are installing each emitter and installing the collector (which IS a `kind: component` row). The discriminated union prevents both (a) the validator rejecting a legitimate paradigm row for missing component-only fields, and (b) the validator silently accepting a malformed component row that carries an `external-plane` flag.

### 7. External-repo components register via `pantheon-component.yaml`

A component whose engine lives outside `pantheon-config` (echo, kino, watch, presence, passport, and any future project organ) declares itself via `pantheon-component.yaml` at the repo root ([[ORG-046]] shape). The registry's `contract.repo` field names the repo; the registry's validator resolves the file against the repo's pinned revision in `repos.yaml` before accepting the row. Circular-registration is refused (a component registering itself while its repo's revision is unpinned is a validation error).

## Consequences

**Positive:**
- The [[ORG-068]] matrix stops rotting: it becomes a rendered view of a validated data source.
- The engine can plan without asking humans "what components exist" — the DAG is data.
- Steward-refinements (Consultant on credential-broker; ORG-076 §7 kino operator plane) land as `git diff` on one file, not a paragraph rewrite of an ADR.
- The distinction between `contract_adoption: not_adopted` + `legacy_coverage:` vs `contract_adoption: adopted` + `legs:` makes "how much of this system has actually adopted the contract" a first-class query, retiring the ambiguity that let optimistic prose paper over gaps.
- CI-enforced closed enum + evidence rules mean the fixture-vs-registry divergence (design draft's Appendix A pre-validation caveat) becomes a landing block after the validator lands, not a rolling conversation.

**Negative:**
- Every existing [[ORG-068]] row must be re-expressed as YAML with evidence refs or gap+ticket pairs; the illustrative Appendix A of the design draft is a starting point, not a validator-clean sample.
- Adding `pantheon-component.yaml` to each external project repo is a coordinated change with the project stewards (echo → artisan; watch → specialist; kino → orchestrator; presence/passport pending).
- The render tool must be run (or CI'd) on every registry edit to keep the rendered matrix in sync; drift between the yaml and the rendered markdown is a smell the CI job must catch.

**Deferred:**
- Whether the `evidence:` string is a free-form pointer or a structured ref (receipt id vs [[ORG-065]] obligation id vs ticket id vs commit sha). The v0 schema accepts strings; a v1 may tighten.
- Whether the render tool emits a single `docs/system-coverage.md` or one file per component. Lean: single file, matches [[ORG-068]]'s current shape.
- Whether legacy_coverage cells are also validated (evidence-required, etc.) or accepted as-is. Lean: validated with the same rules as `legs`, since a `legacy_coverage.install.status: covered` claim is still a claim.

**Open questions:**
- Cross-instance registration when SSEED gains a second head (Archivist / exocortex): does each seed carry its own registry, or is there a federated registry? Not for this ADR — flagged when it becomes concrete.
- `bindings:` interaction with the scheduler's own `system:` join field ([[ORG-064]]): confirm the key is the same string (`scheduler-seat-binding` vs `scheduler`) — settle at build (a).

## Build plan

Under T-2667 build tranche (a) (design draft §6). Sequence:
1. Land this ADR + [[ORG-068]] amendment together.
2. Write `registry/systems.yaml` with the current 11-row [[ORG-068]] matrix expressed against this schema. This IS the retrofit; the design draft's Appendix A is the starting point.
3. Build `tools/germinate/registry/validate.py` + wire it into CI on every PR touching `registry/systems.yaml` or `adr/ORG-*.md`.
4. Build `tools/germinate/registry/render.py` + wire the CI job that writes the rendered view.
5. Amend [[ORG-068]] in place: matrix section becomes a pointer to the rendered view + a link to `registry/systems.yaml`.
6. File `pantheon-component.yaml` in each external project repo (artisan / specialist / orchestrator / …) as those adoption tickets land.

## DECISIONS

- **D-1. Registry is data; the matrix is rendered; status values are a closed enum whose evidence rules the schema validates — F3 of [[ORG-080]], P1 (structure over hand-edited prose), P4 (evidence refs feed [[ORG-065]]).** Same decision letter as [[ORG-080]] D-D; recorded here because this ADR is where the schema and validator live.
- **D-2. `contract_adoption` is a separate field from `legs.*.status` — F4 locked.** A component's existing scripts stay under `legacy_coverage:` until adoption; conflating the two would let prior art masquerade as contract compliance.
- **D-3. Health field records the installer's health-plane obligation, not the runtime verdict — D-J of [[ORG-080]], P1.** The `legs.health:` cell records whether the component has met its installer-side obligation toward the health plane (the three predicates in D-11); it does NOT record the runtime verdict "component is healthy right now" (that lives in [[ORG-076]]). Two vocabularies on the same row is where drift starts.
- **D-4. Own directory (`registry/`), not embedded in `instance.yaml` — [[ORG-064]]'s precedent (schedules/ out of instance.yaml).** Registry is data that changes on component work; instance.yaml is identity/topology that changes on seat work. Different edit cadences, different reviewers.
- **D-5. External-plane rows exist for legibility, not execution — new pattern.** A row with `kind: external-plane` is honestly labeled as non-participating; the render tool surfaces it without lying about DAG participation. The alternative (omit them entirely) hides real obligations.
- **D-6. External-repo components register via `pantheon-component.yaml` at repo root — [[ORG-046]] manifest integrity shape.** Consistent with how project repos already declare themselves to `repos.yaml`.
- **D-7. Registry is contract-coverage only; live run state lives in a separate root/seat-owned store.** The registry (this ADR's `registry/systems.yaml`) is git-tracked, one writer (git commits), authoritative for `contract_adoption` + per-leg coverage evidence. The **run-state store** ([[ORG-080]] §Base engine mechanics + D-Z, root/seat-uid-owned, mode 0700/0600, one writer per component: the executor for that run) is authoritative for parked runs, HALTED markers, `rollback_incomplete` markers, and active-run manifests. A join is display-only; no runtime writer ever mutates this ADR's yaml. **justified-via P1, P4, [[ORG-050]] rollback controls, mirror of [[ORG-080]] D-Z.**
- **D-8. `contract_adoption` values carry typed evidence predicates via a closed 16-key set, not free-form prose.** `not_adopted` forbids `legs:`; `partial` requires at least `plan_install` + `plan_remove` methods + receipt-schema conformance + a named `gap:` + `ticket:` for **every absent predicate from the closed set** (§1 schema: the five callable `plan_*` methods, the declarative `verify_contract` predicate — [[ORG-080]] defines `verify` as an `invariant_list_ref` binding, NOT a `plan_verify()` method, so this predicate names the actual verify contract — the seven `adoption_evidence.*` fields, and the three cross-cutting predicates `crash_contract` / `sandbox` / `receipt_schema` — **16 keys total, 5 + 1 + 7 + 3**; `plan_install` and `plan_remove` are mandatory for `partial` and therefore never appear as gap entries, so the gap-eligible upper bound on any single `partial` component is at most 14, but the closed-set cardinality — the domain of legal `adoption_gaps` keys — is 16); `adopted` requires all seven `adoption_evidence` fields present + evidence-covered + every mutating op declaring a `crash_contract` + the sandboxed planner passing CI acceptance + every leg cell `covered` or explicitly waived. The validator refuses `adopted` with a missing predicate, `partial` without gap+ticket for a missing surface from the closed set, `partial` with a free-form gap key, and `not_adopted` with `legs:` present. **justified-via P1, P4, [[ORG-080]] §Component contract, mirrors [[ORG-080]] D-AA field-authority rule.**
  > **Erratum, ORG-081 D-8 (2026-10-17).** D-8 says a `partial` component must conform to the receipt schema. Its own arithmetic (16 predicates, minus `plan_install` and `plan_remove`, leaving 14 gap-eligible keys) counts `receipt_schema` as a key that may appear as a gap. The validator follows the arithmetic. This is not a false green: the contract loader always requires `receipts.schema`.
- **D-9. One authority per field; registry mirrors the component contract by `contract_digest`; drift is a `plan` refusal.** The registry does NOT redeclare DAG / privilege / gates / interactions / emitter / invariant-list bytes; it holds `contract.digest` (SHA-256 of the component contract module's **canonical serialization** per [[ORG-080]] §Component contract, computed with the `contract_digest` field replaced by the fixed sentinel `"SHA256_DIGEST_PLACEHOLDER"` — not self-referential) and — for external components — `contract.repo_revision` pinning against the repo's ratified revision. The `steward:` field is likewise a **digest-bound mirror** of the contract's `steward`; validator refuses mismatch at plan, no transition-window carve-out. Any mismatch at `plan` time refuses. **justified-via P1, P4, [[ORG-050]] §§46-69, [[ORG-052]] acceptance 17, mirrors [[ORG-080]] D-AA.**
- **D-10. ADR ↔ registry link is machine-readable via two required frontmatter fields.** Every `adr/ORG-*.md` carries either `registers_components: [<id>, …]` (names the component ids that must exist as `kind: component` rows) OR `no_registry_row_waiver: <string>` (mutually exclusive; a named rationale for a decision-level ADR that names no components). The validator refuses a landing carrying neither field OR naming a component id absent from the registry. This turns the "run the validator on ADR changes catches missing rows" claim from a wish into a structural check. **justified-via P1, P4.**
- **D-11. `legs.health.status: covered` requires three predicates simultaneously, not emitter-install alone.** The cell promotes to `covered` only when: (a) the emitter unit is installed with `OnFailure=` + D-Bus exporter registered and its `Get` returns a valid property set; AND (b) the emitter's `invariant_list_ref` and the component's `verify` `invariant_list_ref` resolve to the same `<id>@<digest>` (drift is a validation refusal — the single-vocabulary rule is machine-enforced, not a prose pointer); AND (c) the health-collector is subscribed AND at least one unhealthy transition has been observed at the operator surface (proven alert delivery). Missing (c) → `partial` + `gap:` + `ticket:`. Explicit `partial_by_design` requires a `waiver:` that acknowledges the ratified [[ORG-068]] E9.C3 obligation is not being covered. An orphan emitter cannot promote the cell. **justified-via P1, [[ORG-068]] E9.C3 unchanged, [[ORG-076]] §6, mirrors [[ORG-080]] D-AB and the [[ORG-068]] amendment.**
- **D-12. Two record shapes are a discriminated union keyed on required `kind:`, not one shape with optional fields.** `kind: component` requires `contract`, `contract_adoption`, and (when non-`not_adopted`) `adoption_evidence` + `legs`; it refuses `tracking:` / `relationship:`. `kind: external-plane` requires `adr`, `tracking`, `relationship`; it refuses `contract`, `contract_adoption`, `adoption_evidence`, `legs`, `legacy_coverage`. The validator picks the shape from `kind:` and enforces exact field sets — preventing both a paradigm row failing component-only validation and a malformed component silently passing under an external-plane flag. **justified-via P1.**
- **D-13. Registry `id` bytes are canonical; scheduler legacy `system:` titles are aliased only through a bounded one-time migration file, then retired.** Post-migration, the human titles in `schedules/{global,orchestrator,specialist}.yaml:*` are validation errors. Synthesized `<target>-seat-binding` nodes are ephemeral plan-node ids (not registry ids); receipts attach to the parent component's binding leg. **justified-via P1, [[ORG-064]] §1a.**

## Re-evaluate when

- **R1** — a second seed (Archivist / exocortex, [[ORG-042]]) stands up and needs its own registry: federated registry or per-seed registries?
- **R2** — a component genuinely cannot express its per-leg status in the closed enum (e.g. a leg whose "coverage" is conditional on external state not represented in the schema). Extend the enum before accepting a free-form status.
- **R3** — the CI validator or render tool disagrees with a human reviewer on whether a landing is clean; that is either a schema bug (extend the enum) or a rule bug (tighten the validator).
- **R4** — the [[ORG-064]] scheduler `system:` field join key drifts from this registry's `id`. That's a build-integration bug the CI validator should catch; if it doesn't, extend the validator.
- **R5** — the `contract_digest` mirror rule (D-9) surfaces a legitimate case where a component wants a registry-only field the contract module does not carry (e.g. deployment-instance overrides). Rather than reintroducing per-field duplication, extend the registry schema with an explicit `overrides:` block whose predicate names why the override is legitimate and what the reviewer verifies.
- **R6** — the `registers_components:` / `no_registry_row_waiver:` frontmatter (D-10) grows to a size where the reverse index becomes hard to maintain by hand; add a `tools/germinate/registry/link_index.py` render that keeps a machine-generated reverse index in sync.
- **R7** — an interaction between D-11's three-part health predicate and a component whose emitter legitimately has no collector subscriber (e.g. a bootstrap-only diagnostic emitter) forces `partial_by_design` + `waiver:` on every landing; decide whether the schema needs a fourth predicate case (bootstrap-diagnostic emitters) or whether the waiver pattern stays.

## Revision history

Fold arc across 14 rounds, all 2026-08-26. Each round appears as a git commit on `main`; the sequence is preserved there for audit. High-level shape:

- **v1–v5** — Substantive schema rounds through Consultant's ORG-041 spec-review: discriminated-union record shapes (v2, `kind: component` vs `kind: external-plane`); typed `contract_adoption` predicates + `adoption_gaps` closed key set (v2); ADR ↔ registry frontmatter link (v2); registry-mirrors-contract-by-digest rule + digest-bound `steward` (v2/v3); §4/D-3 opening qualification for three-part health predicate (v4); closed-predicate-set cardinality corrected 13 → 16 across schema comment / §3 / D-8 (v5).
- **v6–v13** — Status-only bumps for bundle-version alignment with [[ORG-080]]'s fold arc through Consultant's REBOUND rounds. No schema changes in this ADR; the substantive work landed on [[ORG-080]] (T-3268 v7 BLOCKs, T-3269 v8 BLOCKs, T-3270 v9 BLOCKs, T-3272 v10 CARVE, T-3284 v11/v12/v13 architectural REBOUND folds).
- **v14 — the operator editorial pass** (TG 8604): stripped revision-history/process scaffolding across all three bundle ADRs; frontmatter status collapsed to one sentence; per-line "v2 (T-3261 FIX-SOON-N):" YAML/prose annotations retired; per-fold signature blocks consolidated into this section. No semantic change to the schema, predicates, validator rules, or decisions.
- **v15 — the operator's external-reviewer final-review pass** (TG 8609). Two ORG-081 fixes aligning the schema to the interface [[ORG-080]] actually defines: (a) the closed 16-predicate set renames `plan_verify` → `verify_contract` (declarative — [[ORG-080]] `verify` is an `invariant_list_ref` binding, not a callable `plan_verify()`); cardinality decomposition updated from "6 plan_* + 7 evidence + 3 cross-cutting" to "5 callable plan_* + 1 declarative verify_contract + 7 evidence + 3 cross-cutting"; total 16 unchanged; (b) the YAML schema comment's stale "at least `plan`+`receipt`" gloss aligned to the D-8 wording ("at least `plan_install` + `plan_remove` methods AND receipt-schema conformance + typed adoption_gaps entries"). No architectural change; both edits close cross-surface mismatches the reviewer identified in the v14 bundle.
